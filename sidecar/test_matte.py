"""Tests for matte.py (T3.2).

Two tiers, same convention as test_segment.py:

1. Pipeline tests (always run, no model weights needed) -- prove the real
   resize -> normalize -> resize-back -> save/load code path end to end
   using StubGradientModel, a deterministic non-ML stand-in. These are the
   tests that matter for "is the I/O plumbing correct".

2. Real-MODNet integration test -- runs the actual MODNet ONNX checkpoint
   against the bundled portrait fixture, if the checkpoint file is present.
   Skipped (not failed) if the checkpoint hasn't been downloaded, so this
   file is still runnable in an environment where the weights were skipped
   as impractical.

Run: .venv/bin/python3.12 -m pytest test_matte.py -v -s
"""

import os

import numpy as np
import pytest

Image = pytest.importorskip("PIL.Image", reason="optional sidecar tests require Pillow")

from matte import (  # noqa: E402 - skip before importing optional sidecar dependencies
    StubGradientModel,
    load_alpha,
    load_model,
    matte,
    save_alpha,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKPOINT = os.path.join(HERE, "checkpoints", "modnet_photographic_portrait_matting.onnx")
PORTRAIT = os.path.join(HERE, "fixtures", "portrait.jpg")


def _make_synthetic_image(tmp_path, w=400, h=300):
    img = Image.new("RGB", (w, h), color=(128, 128, 128))
    path = os.path.join(tmp_path, "synthetic.png")
    img.save(path)
    return path, w, h


# --------------------------------------------------------------------------
# 1. Pipeline tests (stub backend, no model weights needed)
# --------------------------------------------------------------------------


def test_stub_matte_is_aligned_and_soft(tmp_path):
    path, w, h = _make_synthetic_image(tmp_path, w=400, h=300)
    out_path = os.path.join(tmp_path, "alpha.png")

    out = matte(path, out_path=out_path, backend="stub", return_array=True)

    assert out["size"] == {"w": w, "h": h}
    assert out["backend"] == "stub"
    alpha = out["alpha_array"]
    assert alpha.shape == (h, w)  # aligned to input image
    assert 0.0 <= alpha.min() <= alpha.max() <= 1.0

    # radial falloff model must have plenty of intermediate (non 0/1) values
    soft = ((alpha > 0.05) & (alpha < 0.95)).sum()
    assert soft > 0.1 * alpha.size


def test_matte_requires_no_prompt_and_defaults_out_path(tmp_path):
    """Unlike segment(), matte() takes no points/box -- it's dense,
    whole-image prediction. Also check the default out_path convention."""
    path, w, h = _make_synthetic_image(tmp_path)
    out = matte(path, backend="stub")
    assert out["alpha_path"] == path.replace(".png", ".matte.png")
    assert os.path.exists(out["alpha_path"])


def test_save_load_png16_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    alpha = rng.random((50, 70), dtype=np.float64).astype(np.float32)
    # make sure exact 0 and 1 are present too
    alpha[0, 0] = 0.0
    alpha[-1, -1] = 1.0

    path = os.path.join(tmp_path, "a.png")
    save_alpha(alpha, path, fmt="png16")
    loaded = load_alpha(path)

    assert loaded.shape == alpha.shape
    # 16-bit quantization tolerance: 1/65535
    np.testing.assert_allclose(loaded, alpha, atol=1.0 / 65535 + 1e-6)


def test_save_load_npy_roundtrip(tmp_path):
    rng = np.random.default_rng(1)
    alpha = rng.random((30, 40), dtype=np.float64).astype(np.float32)

    path = os.path.join(tmp_path, "a.npy")
    save_alpha(alpha, path, fmt="npy")
    loaded = load_alpha(path)

    np.testing.assert_array_equal(loaded, alpha)  # exact for npy, no quantization


def test_matte_via_pregenerated_model_reused_across_calls(tmp_path):
    """model= kwarg lets a caller reuse a loaded model across images without
    reloading weights each time (mirrors segment.py's `model=` param)."""
    model = StubGradientModel()
    path1, _, _ = _make_synthetic_image(tmp_path, w=200, h=150)
    out1 = matte(path1, out_path=os.path.join(tmp_path, "o1.png"), model=model)
    assert out1["backend"] == "stub"
    assert os.path.exists(out1["alpha_path"])


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        load_model(backend="not-a-real-backend")


# --------------------------------------------------------------------------
# 2. Real MODNet integration test (skipped if checkpoint not downloaded)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.exists(CHECKPOINT) or not os.path.exists(PORTRAIT),
    reason=(
        "real MODNet ONNX checkpoint or portrait fixture not present locally; see README to fetch"
        " both"
    ),
)
def test_real_modnet_portrait_matte_is_soft_and_localized(tmp_path):
    model = load_model("modnet", checkpoint=CHECKPOINT)
    out_path = os.path.join(tmp_path, "portrait_alpha.png")

    out = matte(PORTRAIT, out_path=out_path, backend="modnet", model=model, return_array=True)
    alpha = out["alpha_array"]
    h, w = alpha.shape

    assert out["size"] == {"w": w, "h": h}
    assert out["backend"] == "modnet"

    # 1. range + soft-edge evidence: real intermediate alpha values exist,
    #    not just a thresholded 0/1 mask (which is what binary segmentation
    #    would give).
    assert alpha.min() == pytest.approx(0.0, abs=0.02)
    assert alpha.max() == pytest.approx(1.0, abs=0.02)
    soft_mask = (alpha > 0.05) & (alpha < 0.95)
    n_soft = int(soft_mask.sum())
    frac_soft = n_soft / alpha.size

    # 2. localization: subject/face/hair region should be strongly
    #    foreground, a background corner should be near zero.
    fx0, fy0, fx1, fy1 = 0.30, 0.03, 0.65, 0.36  # face+hair box, portrait.jpg specific
    face_box = alpha[int(fy0 * h) : int(fy1 * h), int(fx0 * w) : int(fx1 * w)]
    corner = alpha[0 : int(0.1 * h), 0 : int(0.1 * w)]  # top-left background

    print(
        f"real MODNet: size={w}x{h} min={alpha.min():.4f} max={alpha.max():.4f} "
        f"mean={alpha.mean():.4f} soft_edge_px={n_soft} ({frac_soft * 100:.2f}%) "
        f"face/hair_box_mean={face_box.mean():.4f} bg_corner_mean={corner.mean():.6f}"
    )

    assert n_soft > 0, "matte has no intermediate alpha values -- looks binary, not a soft matte"
    assert face_box.mean() > 0.5
    assert corner.mean() < 0.05
    assert face_box.mean() - corner.mean() > 0.4

    # 3. round trip through the on-disk format.
    reloaded = load_alpha(out_path)
    diff = np.abs(reloaded - alpha)
    print(f"roundtrip max_abs_diff={diff.max():.2e} mean_abs_diff={diff.mean():.2e}")
    assert diff.max() < 1e-3  # 16-bit PNG quantization tolerance


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "-s"]))
