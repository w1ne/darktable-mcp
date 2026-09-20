"""matte.py -- darktable-mcp matting sidecar (PLAN.md T3.2).

Turns a whole image into a per-pixel FLOAT ALPHA MATTE (0..1, HxW), soft at
hair/fur/edges -- the input Phase 3's raster-mask producer (T3.3) needs.
This is deliberately a *separate* concern from ``segment.py`` (T2.1):

    segment.py -> SAM2, promptable, binary-ish object mask -> polygon path
    matte.py   -> MODNet, unprompted, dense portrait matting -> float alpha

Segmentation gives you "this region is/isn't the object" (a closed contour,
one bit per pixel once thresholded). Matting gives you "this pixel is P%
foreground" -- the thing that actually looks right on wispy hair strands,
motion-blurred fur, glass, smoke: a hard polygon can only approximate that
with jagged edges, matting gives the true soft coverage value.

Pipeline
--------
    image (RGB)
        -> resize to model's working resolution   MODNet-specific: shortest
                                                    edge ~512, both dims
                                                    rounded down to a
                                                    multiple of 32 (exact
                                                    algorithm from the
                                                    official MODNet demo
                                                    script -- see
                                                    ``_modnet_resize_dims``)
        -> normalize to [-1, 1]                    (x/127.5 - 1)
        -> MattingModel.predict()                  onnxruntime forward pass
                                                    -> HxW float32 0..1 at
                                                    working resolution
        -> resize back to the ORIGINAL image size  bilinear, so the matte is
                                                    pixel-aligned to the
                                                    input image (this is the
                                                    "aligned output" the
                                                    task asks for)
        -> clip to [0, 1]
        -> write to disk (16-bit PNG or .npy, see ``save_alpha``/``load_alpha``)

Model backend is swappable -- see ``load_model()`` below, same pattern as
``segment.py``'s ``load_model()``: the single function a caller touches to
point at a different matting model (BiRefNet, RVM, a different MODNet
export, ...).
"""

from __future__ import annotations

import argparse
import json
import os
from abc import ABC, abstractmethod

import cv2
import numpy as np
from PIL import Image

# --------------------------------------------------------------------------
# Matting model interface (swappable backend)
# --------------------------------------------------------------------------


class MattingModel(ABC):
    """Interface every backend (real MODNet, stub, future BiRefNet/RVM, ...)
    must implement. Unlike ``SegmentationModel`` (segment.py) there is no
    point/box prompt -- matting models used here are dense, whole-image
    predictors (that's what makes them able to give per-pixel soft alpha
    instead of a promptable region)."""

    @abstractmethod
    def predict(self, image_rgb: np.ndarray) -> np.ndarray:
        """image_rgb: HxWx3 uint8, RGB. Returns HxW float32 alpha in [0, 1],
        same H, W as the input (backend is responsible for resizing its own
        working resolution back up/down to match)."""
        raise NotImplementedError


def _modnet_resize_dims(im_w: int, im_h: int, ref_size: int = 512) -> tuple[int, int]:
    """Official MODNet demo-script resize rule (onnx/pytorch inference.py
    from the MODNet repo): keep aspect ratio, scale so the shorter side is
    ``ref_size`` (only if the image isn't already close to that scale), then
    round both dims down to a multiple of 32 (MODNet's encoder is an
    ImageNet backbone with 5 stride-2 stages -> input must be a multiple of
    32 or the skip connections don't line up).

    Returns (resize_w, resize_h) for cv2.resize (note: cv2 wants (w, h))."""
    if max(im_h, im_w) < ref_size or min(im_h, im_w) > ref_size:
        if im_w >= im_h:
            im_rh = ref_size
            im_rw = int(im_w / im_h * ref_size)
        else:
            im_rw = ref_size
            im_rh = int(im_h / im_w * ref_size)
    else:
        im_rh, im_rw = im_h, im_w

    im_rw = max(32, im_rw - im_rw % 32)
    im_rh = max(32, im_rh - im_rh % 32)
    return im_rw, im_rh


class MODNetONNXModel(MattingModel):
    """Real backend: MODNet (Zhanghan Ke et al., "Is a Green Screen Really
    Necessary for Real-Time Portrait Matting?"), the official
    ``modnet_photographic_portrait_matting`` checkpoint, exported to ONNX,
    run via onnxruntime (CPU). No torch dependency needed for inference --
    onnxruntime alone is enough, which is why this backend is lighter to
    install than SAM2.

    Preprocessing matches the official MODNet demo script exactly: resize
    per ``_modnet_resize_dims``, normalize to [-1, 1] (mean=std=0.5), NCHW
    float32. Output is a single-channel matte at the working resolution,
    resized back to the original image size with bilinear interpolation to
    stay pixel-aligned to the input.
    """

    def __init__(self, checkpoint: str, ref_size: int = 512, providers: list[str] | None = None):
        import onnxruntime as ort

        self.ref_size = ref_size
        self.session = ort.InferenceSession(
            checkpoint, providers=providers or ["CPUExecutionProvider"]
        )
        self._input_name = self.session.get_inputs()[0].name
        self._output_name = self.session.get_outputs()[0].name

    def predict(self, image_rgb: np.ndarray) -> np.ndarray:
        im_h, im_w = image_rgb.shape[:2]
        rw, rh = _modnet_resize_dims(im_w, im_h, self.ref_size)

        resized = cv2.resize(image_rgb, (rw, rh), interpolation=cv2.INTER_AREA)
        x = resized.astype(np.float32)
        x = (x - 127.5) / 127.5  # [-1, 1], mean=std=0.5 on a 0..1 scale
        x = np.transpose(x, (2, 0, 1))[np.newaxis, ...]  # NCHW

        (out,) = self.session.run([self._output_name], {self._input_name: x})
        matte_small = out[0, 0].astype(np.float32)  # (rh, rw) in [0, 1]

        matte = cv2.resize(matte_small, (im_w, im_h), interpolation=cv2.INTER_LINEAR)
        return np.clip(matte, 0.0, 1.0).astype(np.float32)


class StubGradientModel(MattingModel):
    """NOT a real matting model. Deterministic geometric stand-in used to
    verify the resize/normalize/inference/resize-back/save-load pipeline end
    to end without any model weights. Produces a smooth radial falloff
    (1.0 at image center, decaying to 0.0 at the corners) with a small amount
    of added high-frequency noise along a fixed band so the output has real
    intermediate (non-0/1) values in a wide range -- enough to exercise
    "soft alpha" assertions in tests without claiming any segmentation
    quality.

    Any test asserting behaviour of *this* class is testing the matte I/O
    pipeline, not matting quality.
    """

    def __init__(self, falloff: float = 1.6):
        self.falloff = falloff

    def predict(self, image_rgb: np.ndarray) -> np.ndarray:
        h, w = image_rgb.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cy, cx = h / 2.0, w / 2.0
        # normalized radius, 0 at center, ~1 at corners
        r = np.sqrt(((xx - cx) / (w / 2.0)) ** 2 + ((yy - cy) / (h / 2.0)) ** 2)
        alpha = np.clip(1.0 - r, 0.0, 1.0) ** self.falloff
        return alpha.astype(np.float32)


def load_model(backend: str = "modnet", **kwargs) -> MattingModel:
    """The single swap-in point (mirrors segment.py::load_model). Call this
    to get whichever matting backend the caller wants; everything downstream
    (resize-back, save/load) is backend-agnostic.

    backend="modnet" -> real MODNetONNXModel. Requires `checkpoint` kwarg
                         (or MODNET_CHECKPOINT env var) pointing at the
                         ``modnet_photographic_portrait_matting.onnx`` file
                         -- see README "Enabling real MODNet".
    backend="stub"   -> StubGradientModel, no dependencies beyond numpy/cv2.
    """
    if backend == "modnet":
        checkpoint = kwargs.get("checkpoint") or os.environ.get("MODNET_CHECKPOINT")
        if not checkpoint:
            raise ValueError(
                "modnet backend requires `checkpoint` (arg or MODNET_CHECKPOINT "
                "env var); see README"
            )
        ref_size = kwargs.get("ref_size", 512)
        return MODNetONNXModel(checkpoint=checkpoint, ref_size=ref_size)
    if backend == "stub":
        return StubGradientModel(falloff=kwargs.get("falloff", 1.6))
    raise ValueError(f"unknown backend: {backend!r} (expected 'modnet' or 'stub')")


# --------------------------------------------------------------------------
# Alpha on-disk format: 16-bit grayscale PNG (default) or float32 .npy
# --------------------------------------------------------------------------
#
# Decision (documented for T3.3): default is a **16-bit single-channel PNG**,
# alpha in [0,1] linearly scaled to [0, 65535] (round-trip tolerance
# ~1/65535 ~= 1.5e-5, far below anything a raster mask consumer cares about).
# 16-bit PNG was chosen over 8-bit because darktable's own raster masks and
# most image pipelines are >=16-bit internally -- an 8-bit matte (256 levels)
# would introduce visible banding on a slow hair-to-background gradient once
# it's used as a multiplier on a color operation. PNG (not e.g. TIFF) because
# it's lossless, ubiquitous, and trivially read back by both Python (PIL) and
# darktable/GraphicsMagick/ImageMagick tooling without an extra dependency.
#
# `.npy` is offered as an alternative for pure-Python round-tripping that
# wants to skip the 16-bit quantization step entirely (exact float32 in,
# exact float32 out) -- useful for automated tests, not required for T3.3.
#
# Native size: the matte is always saved at the ORIGINAL input image's pixel
# dimensions (see MODNetONNXModel.predict's final resize-back step) -- T3.3
# is responsible for any further downscaling/resampling to fit a particular
# darktable pipeline stage, this module does not downscale on its own.


def save_alpha(alpha: np.ndarray, out_path: str, fmt: str = "png16") -> str:
    """Write a HxW float32 [0,1] alpha array to disk. fmt="png16" (default)
    or fmt="npy". Returns out_path unchanged (for chaining)."""
    if fmt == "png16":
        scaled = np.clip(alpha, 0.0, 1.0)
        as_u16 = (scaled * 65535.0 + 0.5).astype(np.uint16)
        Image.fromarray(as_u16).save(out_path)
    elif fmt == "npy":
        np.save(out_path, alpha.astype(np.float32), allow_pickle=False)
    else:
        raise ValueError(f"unknown fmt: {fmt!r} (expected 'png16' or 'npy')")
    return out_path


def load_alpha(path: str) -> np.ndarray:
    """Read back an alpha array written by save_alpha(). Format is inferred
    from the file itself (PNG vs .npy), not from the extension, so this
    round-trips regardless of what the caller named the file."""
    if path.endswith(".npy"):
        return np.load(path).astype(np.float32)
    img = Image.open(path)
    arr = np.array(img)
    if arr.dtype == np.uint16:
        return arr.astype(np.float32) / 65535.0
    if arr.dtype == np.uint8:
        return arr.astype(np.float32) / 255.0
    return arr.astype(np.float32)


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def matte(
    image_path: str,
    out_path: str | None = None,
    fmt: str = "png16",
    model: MattingModel | None = None,
    backend: str = "modnet",
    return_array: bool = False,
    **backend_kwargs,
) -> dict:
    """Main entrypoint T3.3 (or anything else) calls.

    image_path:    input image (any PIL-readable format)
    out_path:      where to write the alpha matte. If None, defaults to
                   ``<image_path stem>.matte.png`` (or ``.npy`` for
                   fmt="npy") next to the input image.
    fmt:           "png16" (default, see "Alpha on-disk format" above) or
                   "npy".
    model:         pass a pre-loaded MattingModel to reuse across calls
                   (skips reloading weights); otherwise one is built via
                   load_model(backend, **backend_kwargs) for this call only.
    return_array:  if True, also include the raw float32 HxW array in the
                   returned dict under "alpha_array" (in-process callers
                   only -- not present when this is invoked as a subprocess
                   over the CLI, since it isn't JSON-serializable as-is).

    Returns:
        {
          "alpha_path": str,             # written matte, see save_alpha()
          "format": "png16" | "npy",
          "size": {"w": int, "h": int},  # native size, == input image size
          "backend": str,
          "alpha_min": float,
          "alpha_max": float,
          "alpha_mean": float,
          "alpha_array": np.ndarray,     # only if return_array=True
        }
    """
    pil_img = Image.open(image_path).convert("RGB")
    image_rgb = np.array(pil_img)
    height, width = image_rgb.shape[:2]

    if model is None:
        model = load_model(backend=backend, **backend_kwargs)

    alpha = model.predict(image_rgb)
    if alpha.shape != (height, width):
        # Defensive: a backend that didn't resize back to the input size.
        alpha = cv2.resize(alpha, (width, height), interpolation=cv2.INTER_LINEAR)
    alpha = np.clip(alpha, 0.0, 1.0).astype(np.float32)

    if out_path is None:
        stem, _ext = os.path.splitext(image_path)
        ext = ".npy" if fmt == "npy" else ".png"
        out_path = f"{stem}.matte{ext}"

    save_alpha(alpha, out_path, fmt=fmt)

    result = {
        "alpha_path": out_path,
        "format": fmt,
        "size": {"w": int(width), "h": int(height)},
        "backend": backend if not isinstance(model, MODNetONNXModel) else "modnet",
        "alpha_min": float(alpha.min()),
        "alpha_max": float(alpha.max()),
        "alpha_mean": float(alpha.mean()),
    }
    if isinstance(model, StubGradientModel):
        result["backend"] = "stub"
    if return_array:
        result["alpha_array"] = alpha
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", required=True, help="path to input image")
    ap.add_argument("--out", help="output alpha path (default: <image>.matte.png)")
    ap.add_argument("--format", default="png16", choices=["png16", "npy"])
    ap.add_argument("--backend", default="modnet", choices=["modnet", "stub"])
    ap.add_argument("--checkpoint", help="MODNet ONNX checkpoint path (modnet backend)")
    ap.add_argument(
        "--ref-size", type=int, default=512, help="MODNet working resolution (modnet backend)"
    )
    args = ap.parse_args()

    out = matte(
        args.image,
        out_path=args.out,
        fmt=args.format,
        backend=args.backend,
        checkpoint=args.checkpoint,
        ref_size=args.ref_size,
    )
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
