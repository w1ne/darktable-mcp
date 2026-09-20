"""segment.py -- darktable-mcp segmentation sidecar (PLAN.md T2.1).

Turns a point/box/label prompt into a normalized polygon contour ready to
hand to ``darktable.develop.add_path_mask`` (T2.2). This module is a
standalone service: it does not import or touch any darktable C code, and
has no dependency on the rest of ``darktable_mcp``.

Pipeline
--------
    prompt (points / box / label)
        -> resolve_label_to_box()        label-ONLY prompt -> real box via
                                          Grounding DINO (2026-07-27,
                                          Grounded-SAM); skipped entirely if
                                          points/box were also given
        -> SegmentationModel.predict()   binary mask, HxW bool, plus a score
        -> largest external contour      cv2.findContours
        -> polygon simplification        cv2.approxPolyDP (Douglas-Peucker),
                                          binary-searched to ~10-48 nodes
        -> normalization                 pixel coords -> 0..1 by image size
        -> {polygon, bbox, score, resolved_box?, ...}

Multi-part / holes
-------------------
``add_path_mask`` (and darktable's ``DT_MASKS_PATH`` form in general) wants a
single closed contour. If the predicted mask has holes or several disjoint
blobs, this module keeps only the *largest-area external* contour and drops
the rest. That is a deliberate simplification, not a bug -- document it to
callers (T2.3) so "brighten her face" over a mask with an earring-shaped hole
still gets one clean outline.

Model backend is swappable -- see ``load_model()`` below, which is the single
function T2.3 (or a future add-on) needs to touch to point at a different
segmenter or a different SAM2 checkpoint.
"""

from __future__ import annotations

import argparse
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

# --------------------------------------------------------------------------
# Prompt / result data model
# --------------------------------------------------------------------------


class LabelNotFoundError(ValueError):
    """Grounding DINO ran successfully but found nothing matching `label`
    above its confidence/area-fraction thresholds (see resolve_label_to_box)
    -- a genuine, confident "not in this image" answer, not an
    infrastructure failure. A distinct type (not plain ValueError) so
    `main()` can recognize it and report it via the JSON contract's
    "error_type" field instead of an uncaught-exception traceback + nonzero
    exit -- the caller-side bridge (darktable_mcp's segmentation_tools.py)
    uses that marker to skip the GrabCut fallback for this specific case
    (see darktable_mcp/utils/errors.py's LabelNotFoundInImageError for why).
    """

    pass


@dataclass
class Point:
    """A single prompt point, normalized 0..1, with a SAM-style foreground/
    background label (1 = include, 0 = exclude)."""

    x: float
    y: float
    label: int = 1

    def to_px(self, width: int, height: int) -> tuple[float, float]:
        return self.x * width, self.y * height


@dataclass
class Box:
    """A prompt box, normalized 0..1, top-left + width/height."""

    x: float
    y: float
    w: float
    h: float

    def to_px_xyxy(self, width: int, height: int) -> tuple[float, float, float, float]:
        x0 = self.x * width
        y0 = self.y * height
        x1 = (self.x + self.w) * width
        y1 = (self.y + self.h) * height
        return x0, y0, x1, y1


@dataclass
class SegmentationResult:
    """Raw model output before the contour/simplify/normalize pipeline."""

    mask: np.ndarray  # HxW bool
    score: float | None = None
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Segmentation model interface (swappable backend)
# --------------------------------------------------------------------------


class SegmentationModel(ABC):
    """Interface every backend (real SAM2, stub, future MODNet, ...) must
    implement. Only ``predict`` is required downstream."""

    @abstractmethod
    def predict(
        self,
        image_rgb: np.ndarray,
        points: Sequence[Point] = (),
        box: Box | None = None,
        label: str | None = None,
    ) -> SegmentationResult:
        """image_rgb: HxWx3 uint8. points/box are normalized 0..1 (see Point/Box).
        label: free-text hint, best-effort, may be ignored by a backend that
        has no text grounding (SAM2 itself has none -- see README)."""
        raise NotImplementedError


class SAM2Model(SegmentationModel):
    """Real backend: Meta's Segment Anything 2, image predictor, point/box
    prompts. Text prompts are NOT natively supported by SAM2 (it has no
    language tower) -- the `label` argument THIS CLASS receives is always
    inert, by design (SAM2 itself has nothing to do with it). That's not
    the whole story anymore, though: `segment()` (below) may have already
    turned a label-only prompt into a real `box` via Grounding DINO
    (`resolve_label_to_box`, 2026-07-27) before ever calling this class's
    `predict()` -- so a label DOES end up mattering end-to-end, just never
    inside this class.

    Lazy-imports torch/sam2 so the rest of this module (contour/simplify/
    normalize + the stub backend) works even where SAM2 is not installed.
    """

    def __init__(self, checkpoint: str, model_cfg: str, device: str = "cpu"):
        import torch  # noqa: F401  (import guarded here on purpose)
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        self.device = device
        sam2_model = build_sam2(model_cfg, checkpoint, device=device)
        self.predictor = SAM2ImagePredictor(sam2_model)

    def predict(
        self,
        image_rgb: np.ndarray,
        points: Sequence[Point] = (),
        box: Box | None = None,
        label: str | None = None,
    ) -> SegmentationResult:
        h, w = image_rgb.shape[:2]
        self.predictor.set_image(image_rgb)

        point_coords = None
        point_labels = None
        if points:
            point_coords = np.array([p.to_px(w, h) for p in points], dtype=np.float32)
            point_labels = np.array([p.label for p in points], dtype=np.int32)

        box_xyxy = None
        if box is not None:
            box_xyxy = np.array(box.to_px_xyxy(w, h), dtype=np.float32)

        masks, scores, _logits = self.predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            box=box_xyxy,
            multimask_output=True,
        )
        best = int(np.argmax(scores))
        return SegmentationResult(
            mask=masks[best].astype(bool),
            score=float(scores[best]),
            raw={"backend": "sam2", "num_candidates": len(scores)},
        )


class StubEllipseModel(SegmentationModel):
    """NOT a real segmentation model. Deterministic geometric stand-in used
    to verify the contour -> simplify -> normalize pipeline end to end when
    real SAM2 weights are unavailable/impractical (see README). It fits an
    axis-aligned ellipse mask to the prompted box (or, if only points were
    given, to a fixed-radius disc around their centroid) and returns that as
    the "mask". Score is always 1.0 (no model uncertainty to report).

    Any test asserting behaviour of *this* class is testing geometry code,
    not segmentation quality.
    """

    def __init__(self, default_radius_frac: float = 0.2):
        self.default_radius_frac = default_radius_frac

    def predict(
        self,
        image_rgb: np.ndarray,
        points: Sequence[Point] = (),
        box: Box | None = None,
        label: str | None = None,
    ) -> SegmentationResult:
        h, w = image_rgb.shape[:2]
        mask = np.zeros((h, w), dtype=np.uint8)

        if box is not None:
            x0, y0, x1, y1 = box.to_px_xyxy(w, h)
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            ax, ay = max((x1 - x0) / 2.0, 1.0), max((y1 - y0) / 2.0, 1.0)
        elif points:
            xs = [p.x * w for p in points if p.label == 1]
            ys = [p.y * h for p in points if p.label == 1]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            r = self.default_radius_frac * min(w, h)
            ax = ay = r
        else:
            cx, cy = w / 2.0, h / 2.0
            ax = ay = self.default_radius_frac * min(w, h)

        cv2.ellipse(
            mask,
            center=(int(round(cx)), int(round(cy))),
            axes=(int(round(ax)), int(round(ay))),
            angle=0,
            startAngle=0,
            endAngle=360,
            color=1,
            thickness=-1,
        )
        return SegmentationResult(
            mask=mask.astype(bool), score=1.0, raw={"backend": "stub-ellipse"}
        )


def load_model(backend: str = "sam2", **kwargs) -> SegmentationModel:
    """The single swap-in point. Call this to get whichever backend the
    caller wants; everything downstream (contour/simplify/normalize) is
    backend-agnostic.

    backend="sam2"  -> real SAM2Model. Requires `checkpoint` + `model_cfg`
                        kwargs (or SAM2_CHECKPOINT / SAM2_MODEL_CFG env vars),
                        and the `sam2` + `torch` packages installed -- see
                        README "Enabling real SAM2".
    backend="stub"  -> StubEllipseModel, no dependencies beyond numpy/opencv.
    """
    if backend == "sam2":
        import os

        checkpoint = kwargs.get("checkpoint") or os.environ.get("SAM2_CHECKPOINT")
        model_cfg = kwargs.get("model_cfg") or os.environ.get("SAM2_MODEL_CFG")
        if not checkpoint or not model_cfg:
            raise ValueError(
                "sam2 backend requires checkpoint + model_cfg "
                "(args or SAM2_CHECKPOINT / SAM2_MODEL_CFG env vars); see README"
            )
        device = kwargs.get("device", "cpu")
        return SAM2Model(checkpoint=checkpoint, model_cfg=model_cfg, device=device)
    if backend == "stub":
        return StubEllipseModel(default_radius_frac=kwargs.get("default_radius_frac", 0.2))
    raise ValueError(f"unknown backend: {backend!r} (expected 'sam2' or 'stub')")


# --------------------------------------------------------------------------
# Grounded-SAM: text label -> box, via Grounding DINO (2026-07-27)
# --------------------------------------------------------------------------
#
# SAM2 has no language tower (see SAM2Model's docstring) -- `label` reaching
# it directly does nothing. This resolves `label` into a `Box` BEFORE
# SAM2Model.predict() ever runs, using Grounding DINO (a real text-grounded
# open-vocabulary detector) -- so a caller giving ONLY a text label (no
# points/box) gets an actual, real detection-driven prompt instead of a
# silently-ignored hint. Only used when the caller gave a label and nothing
# else (see segment()) -- an explicit points/box always takes priority and
# skips this entirely, so existing callers are unaffected.

_grounding_dino_cache: dict = {}


def _load_grounding_dino(model_id: str, device: str = "cpu"):
    """Lazy-load + cache (processor, model) for one model_id/device pair --
    loading is ~1-2s, not worth repeating per segment() call when a caller
    reuses this process (mirrors SAM2Model's own lazy torch/sam2 import)."""
    key = (model_id, device)
    if key not in _grounding_dino_cache:
        import torch  # noqa: F401  (import guarded here on purpose)
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        processor = AutoProcessor.from_pretrained(model_id)
        model = AutoModelForZeroShotObjectDetection.from_pretrained(model_id)
        model.eval()
        model.to(device)
        _grounding_dino_cache[key] = (processor, model)
    return _grounding_dino_cache[key]


DEFAULT_GROUNDING_DINO_MODEL = "IDEA-Research/grounding-dino-tiny"


def resolve_label_to_box(
    image_rgb: np.ndarray,
    label: str,
    model_id: str = DEFAULT_GROUNDING_DINO_MODEL,
    threshold: float = 0.5,
    text_threshold: float = 0.25,
    max_area_frac: float = 0.85,
    device: str = "cpu",
) -> dict | None:
    """Ground free-text `label` in `image_rgb` (HxWx3 uint8) via Grounding
    DINO. Returns the highest-scoring detection as a pixel-space box dict
    {"x0","y0","x1","y1","score"}, or None if nothing scored above
    `threshold` (or the best box was rejected by `max_area_frac`, see
    below). Caller decides what "nothing found" means (segment() raises
    rather than silently falling back to a meaningless whole-image mask --
    a wrong-but-confident box is more useful than no signal at all, but NO
    box is a real "not in this image" answer worth surfacing, not something
    to paper over).

    `threshold` tuned empirically on the portrait fixture: querying for
    objects NOT in the image scored anywhere from 0.31 ("a helicopter", "a
    mountain") up to 0.46-0.48 ("a dog", "a car") -- no single value below
    ~0.5 cleanly separated every absent-object query from the true positive
    ("a face" scored 0.76; "a person"/"hair" scored 0.72-0.79). 0.5 gave a
    clean separation on this one fixture but is NOT guaranteed to generalize
    -- this is a known characteristic of open-vocabulary detectors queried
    for something that isn't present, not a bug specific to this
    integration. `resolved_box` is always surfaced in segment()'s output
    specifically so a caller can sanity-check what actually got grounded
    before trusting the resulting mask, which matters more than any single
    threshold value.

    `max_area_frac` is a SECOND, independent safety net found during that
    same sweep: every false-positive box (the "absent object" queries above
    that scored close to threshold) covered 85-98% of the frame -- the
    detector's "I don't know, so I'll point at everything" tell. A real
    single object worth an isolated local-edit mask is essentially never
    ~the whole frame, so a box this large is rejected regardless of its
    score -- this catches a marginal false positive a confidence threshold
    alone might let through, and is a structurally different (not just a
    stricter version of the same) signal from the score.

    Grounding DINO's own convention wants the query phrase lowercased and
    period-terminated (e.g. "a person." not "a person" or "A person") --
    handled here so callers can pass a plain label like mask_object's.
    """
    import torch

    processor, model = _load_grounding_dino(model_id, device=device)
    image = Image.fromarray(image_rgb)
    query = label.strip().lower()
    if not query.endswith("."):
        query += "."

    inputs = processor(images=image, text=query, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)

    results = processor.post_process_grounded_object_detection(
        outputs,
        inputs.input_ids,
        threshold=threshold,
        text_threshold=text_threshold,
        target_sizes=[image.size[::-1]],
    )[0]

    scores = results["scores"]
    if len(scores) == 0:
        return None
    best = int(scores.argmax())
    x0, y0, x1, y1 = [float(v) for v in results["boxes"][best].tolist()]

    img_h, img_w = image_rgb.shape[:2]
    if not _box_area_ok(x0, y0, x1, y1, img_w, img_h, max_area_frac):
        return None

    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "score": float(scores[best])}


def _box_area_ok(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    img_w: int,
    img_h: int,
    max_area_frac: float,
) -> bool:
    """The max_area_frac guard from resolve_label_to_box's docstring, split
    out as a pure function so it's unit-testable without loading Grounding
    DINO weights (see test_segment.py)."""
    area_frac = ((x1 - x0) * (y1 - y0)) / (img_w * img_h)
    return area_frac <= max_area_frac


# --------------------------------------------------------------------------
# Mask -> contour -> simplify -> normalize pipeline (real, backend-agnostic)
# --------------------------------------------------------------------------


def largest_external_contour(mask: np.ndarray) -> np.ndarray:
    """cv2.findContours + pick the largest-area external contour. See module
    docstring: holes / secondary blobs are intentionally dropped -- a path
    mask is a single closed contour.

    Returns an (N, 2) int32 array of (x, y) pixel coordinates. Raises
    ValueError if the mask is empty (no foreground pixels)."""
    mask_u8 = (mask.astype(np.uint8)) * 255
    contours, _hierarchy = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise ValueError("mask has no foreground pixels; nothing to contour")
    largest = max(contours, key=cv2.contourArea)
    return largest.reshape(-1, 2).astype(np.int32)


def simplify_polygon(
    contour: np.ndarray,
    target_min: int = 10,
    target_max: int = 48,
    target_nodes: int | None = None,
    max_iter: int = 25,
) -> np.ndarray:
    """Douglas-Peucker simplification (cv2.approxPolyDP) with the epsilon
    binary-searched to converge the resulting node count on `target_nodes`
    (defaults to target_max -- see the 2026-07-27 bugreport below for why
    that's the right default). Returns an (M, 2) int32 array, M as close to
    target_nodes as achievable (or the raw contour unchanged if it already
    has <= target_max points -- nothing to simplify).

    Bugreport 2026-07-27: raising max_nodes (e.g. to 180, for a complex
    human-body silhouette) had NO effect -- the polygon still came back
    with only 11-12 nodes. Root cause: the PREVIOUS version of this
    function returned as soon as the node count landed ANYWHERE inside
    [target_min, target_max], and with target_min fixed at 10 and a wide
    range, the very FIRST binary-search midpoint (roughly the middle of
    the whole epsilon search space, i.e. a fairly aggressive epsilon)
    already collapsed a dense raw contour (hundreds-thousands of points)
    down to ~10-15 points, which satisfied "somewhere in [10, 180]" and
    returned immediately -- never exploring toward a SMALLER epsilon that
    would have kept more real detail. Fix: bisect epsilon to converge on
    target_nodes specifically (larger epsilon when the current approx has
    MORE points than target_nodes, smaller when it has FEWER), running the
    full max_iter budget and keeping whichever candidate seen came closest
    to target_nodes (preferring one inside [target_min, target_max] over
    one outside it) -- not stopping at the first "good enough" hit."""
    if target_nodes is None:
        target_nodes = target_max

    contour = contour.reshape(-1, 1, 2).astype(np.int32)
    n = len(contour)
    if n <= target_max:
        return contour.reshape(-1, 2)

    def _rank(m: int) -> tuple:
        in_range = target_min <= m <= target_max
        return (0 if in_range else 1, abs(m - target_nodes))

    perimeter = cv2.arcLength(contour, True)
    lo, hi = 0.0001 * perimeter, 0.2 * perimeter
    best = contour.reshape(-1, 2)
    best_rank = _rank(n)
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        approx = cv2.approxPolyDP(contour, mid, True).reshape(-1, 2)
        m = len(approx)
        rank = _rank(m)
        if rank < best_rank:
            best, best_rank = approx, rank
        if m == target_nodes:
            return approx
        if m > target_nodes:
            lo = mid  # too many points relative to target -> increase epsilon
        else:
            hi = mid  # too few points relative to target -> decrease epsilon
    return best


def normalize_polygon(points_px: np.ndarray, width: int, height: int) -> list[dict]:
    """(N,2) pixel coords -> list of {"x","y"} normalized 0..1, in order,
    ready for add_path_mask. Contour order from cv2 is already a closed ring
    (implicit closure: last point connects back to first); we do not repeat
    the first point at the end, matching darktable's own path-node list
    convention."""
    return [{"x": float(px / width), "y": float(py / height)} for px, py in points_px]


def polygon_bbox(polygon_norm: list[dict]) -> dict:
    xs = [p["x"] for p in polygon_norm]
    ys = [p["y"] for p in polygon_norm]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def segment(
    image_path: str,
    points: list[dict] | None = None,
    box: dict | None = None,
    label: str | None = None,
    model: SegmentationModel | None = None,
    backend: str = "sam2",
    target_min: int = 10,
    target_max: int = 48,
    target_nodes: int | None = None,
    use_grounding_dino: bool = True,
    grounding_dino_threshold: float = 0.5,
    **backend_kwargs,
) -> dict:
    """Main entrypoint T2.3 (or anything else) calls.

    points: list of {"x":.., "y":.., "label": 1|0}  (normalized 0..1; label
            optional, defaults to 1 = foreground click)
    box:    {"x":.., "y":.., "w":.., "h":..}         (normalized 0..1)
    label:  free-text hint (e.g. "the red car", "a face"). SAM2 itself has
            no text grounding (see SAM2Model docstring), so if points/box
            are ALSO given, label is passed through inert, best-effort
            only. If label is the ONLY prompt given (no points, no box),
            it is resolved into a real box via Grounding DINO
            (`resolve_label_to_box`, 2026-07-27) BEFORE the segmentation
            model ever runs -- an actual text-grounded detection, not a
            best-effort hint. Set use_grounding_dino=False to disable this
            (falls back to the old inert-label behavior; useful for a
            caller that already always supplies points/box, or to force
            the pre-Grounded-SAM code path).
    model:  pass a pre-loaded SegmentationModel to reuse across calls
            (skips reloading weights); otherwise one is built via
            load_model(backend, **backend_kwargs) for this call only.
    target_nodes: the node count simplify_polygon actually converges
            toward (see its docstring for the 2026-07-27 bugreport this
            fixes -- max_nodes alone was NOT enough to get more detail).
            Defaults to target_max when not given -- "as much detail as
            useful, up to the cap" is what a caller raising max_nodes
            almost always actually wants.
    grounding_dino_threshold: detection confidence floor (0..1) for the
            label->box resolution above; only used when it actually runs.

    Returns:
        {
          "polygon": [{"x":.., "y":..}, ...],   # ~target_min..target_max nodes
          "bbox": {"x":.., "y":.., "w":.., "h":..},
          "score": float | null,
          "num_points_raw": int,
          "num_points_simplified": int,
          "backend": str,
          "image_size": {"w": int, "h": int},
          "resolved_box": {"x0","y0","x1","y1","score","label"} | absent,
                           # only present when label->box resolution ran --
                           # the pixel-space box Grounding DINO found,
                           # BEFORE it became the box the segmentation
                           # model actually used (surfaced so a caller can
                           # tell "wrong text grounding" apart from "right
                           # box, wrong segmentation" -- same transparency
                           # principle as mask_object's bbox_display_frame/
                           # bbox_mask_frame).
        }

    Raises ValueError if label->box resolution runs and finds nothing above
    grounding_dino_threshold -- a real "not in this image" answer, not
    silently swallowed into a meaningless whole-image mask.
    """
    if not points and not box and not label:
        raise ValueError("segment() needs at least one of points, box, label")

    pil_img = Image.open(image_path).convert("RGB")
    image_rgb = np.array(pil_img)
    height, width = image_rgb.shape[:2]

    pts = [Point(**p) for p in points or []]
    bx = Box(**box) if box else None

    resolved_box: dict | None = None
    if label and not pts and bx is None and use_grounding_dino:
        box_px = resolve_label_to_box(image_rgb, label, threshold=grounding_dino_threshold)
        if box_px is None:
            raise LabelNotFoundError(
                f"label {label!r} not found in image by Grounding DINO "
                f"(score below {grounding_dino_threshold}) -- provide "
                "points/box manually instead, or lower "
                "grounding_dino_threshold if you're confident it's there"
            )
        bx = Box(
            x=box_px["x0"] / width,
            y=box_px["y0"] / height,
            w=(box_px["x1"] - box_px["x0"]) / width,
            h=(box_px["y1"] - box_px["y0"]) / height,
        )
        resolved_box = {**box_px, "label": label}

    if model is None:
        model = load_model(backend=backend, **backend_kwargs)

    result = model.predict(image_rgb, points=pts, box=bx, label=label)

    raw_contour = largest_external_contour(result.mask)
    simplified = simplify_polygon(
        raw_contour, target_min=target_min, target_max=target_max, target_nodes=target_nodes
    )
    polygon_norm = normalize_polygon(simplified, width, height)
    bbox_norm = polygon_bbox(polygon_norm)

    out = {
        "polygon": polygon_norm,
        "bbox": bbox_norm,
        "score": result.score,
        "num_points_raw": int(len(raw_contour)),
        "num_points_simplified": int(len(simplified)),
        "backend": result.raw.get("backend", backend),
        "image_size": {"w": int(width), "h": int(height)},
    }
    if resolved_box is not None:
        out["resolved_box"] = resolved_box
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_point(s: str) -> dict:
    parts = s.split(",")
    x, y = float(parts[0]), float(parts[1])
    label = int(parts[2]) if len(parts) > 2 else 1
    return {"x": x, "y": y, "label": label}


def _parse_box(s: str) -> dict:
    x, y, w, h = (float(v) for v in s.split(","))
    return {"x": x, "y": y, "w": w, "h": h}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", required=True, help="path to input image")
    ap.add_argument(
        "--point",
        action="append",
        default=[],
        help="normalized prompt point 'x,y[,label]' (label 1=fg default, 0=bg); repeatable",
    )
    ap.add_argument("--box", help="normalized prompt box 'x,y,w,h'")
    ap.add_argument(
        "--label",
        help=(
            "free-text hint (e.g. 'a face'). If given alone (no --point/"
            "--box), resolved into a real box via Grounding DINO before "
            "segmentation runs -- see --no-grounding-dino to disable that."
        ),
    )
    ap.add_argument("--backend", default="sam2", choices=["sam2", "stub"])
    ap.add_argument("--checkpoint", help="SAM2 checkpoint path (sam2 backend)")
    ap.add_argument("--model-cfg", help="SAM2 model config name/path (sam2 backend)")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--min-nodes", type=int, default=10)
    ap.add_argument("--max-nodes", type=int, default=48)
    ap.add_argument(
        "--target-nodes",
        type=int,
        default=None,
        help=(
            "node count to converge toward (default: --max-nodes -- see simplify_polygon's"
            " docstring)"
        ),
    )
    ap.add_argument(
        "--no-grounding-dino",
        action="store_true",
        help="disable label->box resolution; --label alone becomes inert again (old behavior)",
    )
    ap.add_argument(
        "--grounding-dino-threshold",
        type=float,
        default=0.5,
        help="detection confidence floor (0..1) for label->box resolution",
    )
    args = ap.parse_args()

    points = [_parse_point(p) for p in args.point]
    box = _parse_box(args.box) if args.box else None

    try:
        out = segment(
            args.image,
            points=points,
            box=box,
            label=args.label,
            backend=args.backend,
            checkpoint=args.checkpoint,
            model_cfg=args.model_cfg,
            device=args.device,
            target_min=args.min_nodes,
            target_max=args.max_nodes,
            target_nodes=args.target_nodes,
            use_grounding_dino=not args.no_grounding_dino,
            grounding_dino_threshold=args.grounding_dino_threshold,
        )
    except LabelNotFoundError as exc:
        # Exit 0 (not a crash/infra failure) with a JSON error contract the
        # caller-side bridge (darktable_mcp/tools/segmentation_tools.py)
        # recognizes via error_type to skip its GrabCut fallback -- see
        # LabelNotFoundError's docstring.
        print(json.dumps({"error": str(exc), "error_type": "label_not_found"}, indent=2))
        return
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
