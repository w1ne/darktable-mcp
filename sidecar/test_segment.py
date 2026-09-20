"""Tests for segment.py (T2.1).

Two tiers:

1. Pipeline tests (always run, no model weights needed) -- prove the real
   mask -> contour -> simplify -> normalize code path end to end against a
   synthetic, precomputed binary mask (an ellipse) via the StubEllipseModel.
   These are the tests that matter for "is the geometry code correct".

2. Real-SAM2 integration test -- runs the actual tiny SAM2 checkpoint against
   the bundled portrait fixture, if the checkpoint file is present. Skipped
   (not failed) if the checkpoint hasn't been downloaded, so this file is
   still runnable in an environment where the weights were skipped as
   impractical.

Run: .venv/bin/python3.12 -m pytest test_segment.py -v
"""

import os

import cv2
import numpy as np
import pytest

Image = pytest.importorskip("PIL.Image", reason="optional sidecar tests require Pillow")

from segment import (  # noqa: E402 - skip before importing optional sidecar dependencies
    _box_area_ok,
    largest_external_contour,
    load_model,
    normalize_polygon,
    segment,
    simplify_polygon,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKPOINT = os.path.join(HERE, "checkpoints", "sam2.1_hiera_tiny.pt")
MODEL_CFG = "configs/sam2.1/sam2.1_hiera_t.yaml"
SMALL_CHECKPOINT = os.path.join(HERE, "checkpoints", "sam2.1_hiera_small.pt")
SMALL_MODEL_CFG = "configs/sam2.1/sam2.1_hiera_s.yaml"
PORTRAIT = os.path.join(HERE, "fixtures", "portrait.jpg")

try:
    import transformers  # noqa: F401

    _TRANSFORMERS_AVAILABLE = True
except ImportError:
    _TRANSFORMERS_AVAILABLE = False


def _make_synthetic_image(tmp_path, w=400, h=300):
    """A plain gray canvas -- content doesn't matter for the stub backend,
    only image dimensions do (for normalization)."""
    img = Image.new("RGB", (w, h), color=(128, 128, 128))
    path = os.path.join(tmp_path, "synthetic.png")
    img.save(path)
    return path, w, h


# --------------------------------------------------------------------------
# 1. Pipeline tests against a synthetic/precomputed mask (stub backend)
# --------------------------------------------------------------------------


def test_stub_box_prompt_returns_polygon_matching_bbox(tmp_path):
    path, w, h = _make_synthetic_image(tmp_path)
    box = {"x": 0.2, "y": 0.3, "w": 0.4, "h": 0.35}

    out = segment(path, box=box, backend="stub", target_min=10, target_max=30)

    assert 10 <= len(out["polygon"]) <= 30
    for p in out["polygon"]:
        assert 0.0 <= p["x"] <= 1.0
        assert 0.0 <= p["y"] <= 1.0

    # bbox of the returned polygon must be close to the prompted box (an
    # ellipse inscribed in the box touches the box edges at its four
    # extrema, so the bboxes should match near-exactly).
    bb = out["bbox"]
    assert bb["x"] == pytest.approx(box["x"], abs=0.02)
    assert bb["y"] == pytest.approx(box["y"], abs=0.02)
    assert bb["w"] == pytest.approx(box["w"], abs=0.02)
    assert bb["h"] == pytest.approx(box["h"], abs=0.02)
    assert out["score"] == 1.0
    assert out["backend"] == "stub-ellipse"


def test_stub_point_prompt_centers_mask_on_click(tmp_path):
    path, w, h = _make_synthetic_image(tmp_path)
    out = segment(path, points=[{"x": 0.5, "y": 0.5}], backend="stub")
    bb = out["bbox"]
    cx = bb["x"] + bb["w"] / 2
    cy = bb["y"] + bb["h"] / 2
    assert cx == pytest.approx(0.5, abs=0.02)
    assert cy == pytest.approx(0.5, abs=0.02)


def test_simplify_reduces_point_count_and_preserves_area():
    """Direct unit test of the Douglas-Peucker step: build a many-point
    circular contour, simplify it, and check N -> M lands in range while
    IoU against the original polygon stays high (simplification isn't
    destroying the shape)."""
    h = w = 500
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (250, 250), 180, 1, thickness=-1)

    raw = largest_external_contour(mask.astype(bool))
    n_raw = len(raw)
    assert n_raw > 30  # a circle rasterized at this radius has hundreds of contour points

    simplified = simplify_polygon(raw, target_min=10, target_max=30)
    n_simplified = len(simplified)
    assert 10 <= n_simplified <= 30

    poly_mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(poly_mask, [simplified.reshape(-1, 1, 2).astype(np.int32)], 1)
    inter = np.logical_and(poly_mask.astype(bool), mask.astype(bool)).sum()
    union = np.logical_or(poly_mask.astype(bool), mask.astype(bool)).sum()
    iou = inter / union
    assert iou > 0.9

    # Recorded for the acceptance report: raw -> simplified node count.
    print(f"circle contour: {n_raw} raw points -> {n_simplified} simplified points, IoU={iou:.4f}")


def test_simplify_actually_uses_the_full_max_nodes_budget():
    """Regression guard for the 2026-07-27 bugreport: raising max_nodes had
    NO effect on the actual output (always ~11-12 nodes regardless of a
    180 cap) because the old algorithm returned as soon as the node count
    landed ANYWHERE inside [target_min, target_max] -- with target_min
    fixed at 10, the very first (coarse) binary-search guess almost always
    qualified and returned immediately. Raising max_nodes must now actually
    raise the result's node count, converging close to it (target_nodes
    defaults to target_max)."""
    h = w = 500
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (250, 250), 180, 1, thickness=-1)
    raw = largest_external_contour(mask.astype(bool))

    n_12 = len(simplify_polygon(raw, target_min=10, target_max=12))
    n_48 = len(simplify_polygon(raw, target_min=10, target_max=48))
    n_180 = len(simplify_polygon(raw, target_min=10, target_max=180))

    print(f"max_nodes=12 -> {n_12}, max_nodes=48 -> {n_48}, max_nodes=180 -> {n_180}")
    # Must land close to each requested cap, not collapse to the same
    # small number regardless of the cap (the actual pre-fix behavior).
    assert n_12 <= 12
    assert n_48 >= 40  # was returning ~11-12 here before the fix
    assert n_180 >= 150  # was returning ~11-12 here before the fix
    assert n_12 < n_48 < n_180


def test_simplify_target_nodes_overrides_target_max():
    """target_nodes lets a caller aim BELOW the max_nodes ceiling
    explicitly, independent of the max_nodes-defaults-to-target_nodes
    convenience default mask_object relies on."""
    h = w = 500
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (250, 250), 180, 1, thickness=-1)
    raw = largest_external_contour(mask.astype(bool))

    result = simplify_polygon(raw, target_min=10, target_max=180, target_nodes=20)
    assert 15 <= len(result) <= 25


def test_polygon_is_closed_ring_in_order():
    """cv2.findContours returns points already ordered around the boundary
    (a closed ring, first point not repeated at the end). Confirm that
    invariant holds through simplify + normalize, and that consecutive
    points don't jump erratically (rough monotonic angular progression
    around the centroid for a convex-ish shape)."""
    h = w = 300
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.ellipse(mask, (150, 150), (100, 60), 0, 0, 360, 1, thickness=-1)

    raw = largest_external_contour(mask.astype(bool))
    simplified = simplify_polygon(raw, 10, 30)
    polygon = normalize_polygon(simplified, w, h)

    cx = sum(p["x"] for p in polygon) / len(polygon)
    cy = sum(p["y"] for p in polygon) / len(polygon)
    angles = [np.arctan2(p["y"] - cy, p["x"] - cx) for p in polygon]
    # unwrap and check monotonic (allow either winding direction)
    diffs = np.diff(angles)
    # wrap diffs into (-pi, pi]
    diffs = (diffs + np.pi) % (2 * np.pi) - np.pi
    same_sign = all(d >= -1e-6 for d in diffs) or all(d <= 1e-6 for d in diffs)
    assert same_sign, "polygon points are not in consistent angular (boundary) order"


def test_missing_mask_raises():
    empty_mask = np.zeros((50, 50), dtype=bool)
    with pytest.raises(ValueError):
        largest_external_contour(empty_mask)


def test_multi_blob_mask_keeps_only_largest_external_contour():
    """Two disjoint blobs -> we must keep exactly the larger one (a path
    mask is a single closed contour; see segment.py module docstring)."""
    h = w = 300
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (80, 80), 40, 1, thickness=-1)  # small blob, area ~ pi*40^2
    cv2.circle(mask, (220, 220), 70, 1, thickness=-1)  # big blob, area ~ pi*70^2

    contour = largest_external_contour(mask.astype(bool))
    area = cv2.contourArea(contour.reshape(-1, 1, 2))
    # should match the big blob, not the small one
    assert area > 10000  # pi*70^2 ~= 15393; pi*40^2 ~= 5027


def test_segment_requires_at_least_one_prompt(tmp_path):
    path, _, _ = _make_synthetic_image(tmp_path)
    with pytest.raises(ValueError):
        segment(path, backend="stub")


def test_label_only_stub_backend_without_grounding_dino_is_inert(tmp_path):
    """use_grounding_dino=False preserves the pre-2026-07-27 behavior: a
    label-only prompt with no points/box just goes straight to the model
    with box=None (StubEllipseModel's own whole-image-ish fallback), no
    Grounding DINO call at all -- no network/weights needed for this path."""
    path, w, h = _make_synthetic_image(tmp_path)
    out = segment(path, label="anything", backend="stub", use_grounding_dino=False)
    assert "resolved_box" not in out


def test_box_area_ok_rejects_near_whole_frame_box():
    """Guards the max_area_frac safety net (resolve_label_to_box's
    docstring): a box covering ~98% of the frame -- the exact failure
    pattern found querying an absent object ("a car") on the portrait
    fixture -- must be rejected regardless of confidence score."""
    img_w, img_h = 960, 1431
    assert _box_area_ok(4.9, 6.5, 956.2, 1424.3, img_w, img_h, max_area_frac=0.85) is False


def test_box_area_ok_accepts_reasonably_scoped_box():
    """A normal single-object box (the real 'a face' detection from the
    portrait fixture, ~5% of frame area) must NOT be rejected."""
    img_w, img_h = 960, 1431
    assert _box_area_ok(337.8, 199.7, 564.9, 519.8, img_w, img_h, max_area_frac=0.85) is True


# --------------------------------------------------------------------------
# 2. Real SAM2 integration test (skipped if checkpoint not downloaded)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.exists(CHECKPOINT) or not os.path.exists(PORTRAIT),
    reason="real SAM2 checkpoint or portrait fixture not present locally; see README to fetch both",
)
def test_real_sam2_face_segmentation_on_portrait():
    model = load_model("sam2", checkpoint=CHECKPOINT, model_cfg=MODEL_CFG, device="cpu")

    # Box roughly bracketing the face in fixtures/portrait.jpg (960x1431,
    # Mona Lisa -- see README for how this fixture was chosen/fetched).
    box = {"x": 0.344, "y": 0.105, "w": 0.271, "h": 0.217}
    out = segment(PORTRAIT, box=box, backend="sam2", model=model, target_min=10, target_max=30)

    assert out["backend"] == "sam2"
    assert out["score"] is not None and out["score"] > 0.5
    assert 10 <= len(out["polygon"]) <= 30

    prompt_bb = (box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"])
    pred_bb = out["bbox"]
    pred = (pred_bb["x"], pred_bb["y"], pred_bb["x"] + pred_bb["w"], pred_bb["y"] + pred_bb["h"])
    ix0, iy0 = max(prompt_bb[0], pred[0]), max(prompt_bb[1], pred[1])
    ix1, iy1 = min(prompt_bb[2], pred[2]), min(prompt_bb[3], pred[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    area_prompt = (prompt_bb[2] - prompt_bb[0]) * (prompt_bb[3] - prompt_bb[1])
    area_pred = (pred[2] - pred[0]) * (pred[3] - pred[1])
    iou = inter / (area_prompt + area_pred - inter)

    print(
        f"real SAM2: score={out['score']:.3f} raw={out['num_points_raw']} "
        f"simplified={out['num_points_simplified']} bbox_iou_vs_prompt={iou:.3f}"
    )
    assert iou > 0.4  # face mask bbox should substantially overlap the prompted box


# --------------------------------------------------------------------------
# 3. Real Grounded-SAM integration tests (2026-07-27, skipped if transformers/
#    weights aren't present -- same auto-skip philosophy as the SAM2 test
#    above, not a hard CI requirement)
# --------------------------------------------------------------------------

_GROUNDED_SAM_SKIP = (
    not _TRANSFORMERS_AVAILABLE
    or not os.path.exists(SMALL_CHECKPOINT)
    or not os.path.exists(PORTRAIT)
)
_GROUNDED_SAM_SKIP_REASON = (
    "transformers not installed, or SAM2 small checkpoint/portrait fixture "
    "not present locally; see README/requirements.txt to fetch both"
)


@pytest.mark.skipif(_GROUNDED_SAM_SKIP, reason=_GROUNDED_SAM_SKIP_REASON)
def test_real_grounding_dino_resolves_face_label_on_portrait():
    """label-only ('a face', no points/box) must resolve to a real box via
    Grounding DINO, then segment through SAM2 -- not just pass label
    through inert. Checks the resolved_box lands on the actual face."""
    model = load_model("sam2", checkpoint=SMALL_CHECKPOINT, model_cfg=SMALL_MODEL_CFG, device="cpu")
    out = segment(
        PORTRAIT, label="a face", backend="sam2", model=model, target_min=10, target_max=48
    )

    assert out["backend"] == "sam2"
    assert "resolved_box" in out
    assert out["resolved_box"]["label"] == "a face"
    assert out["resolved_box"]["score"] > 0.5

    # Same rough face bracket as test_real_sam2_face_segmentation_on_portrait
    # -- the resolved box should substantially overlap it.
    prompt_bb = (0.344, 0.105, 0.344 + 0.271, 0.105 + 0.217)
    w, h = out["image_size"]["w"], out["image_size"]["h"]
    rb = out["resolved_box"]
    pred_bb = (rb["x0"] / w, rb["y0"] / h, rb["x1"] / w, rb["y1"] / h)
    ix0, iy0 = max(prompt_bb[0], pred_bb[0]), max(prompt_bb[1], pred_bb[1])
    ix1, iy1 = min(prompt_bb[2], pred_bb[2]), min(prompt_bb[3], pred_bb[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    area_prompt = (prompt_bb[2] - prompt_bb[0]) * (prompt_bb[3] - prompt_bb[1])
    area_pred = (pred_bb[2] - pred_bb[0]) * (pred_bb[3] - pred_bb[1])
    iou = inter / (area_prompt + area_pred - inter)
    print(f"grounding dino: score={rb['score']:.3f} iou_vs_known_face_box={iou:.3f}")
    assert iou > 0.4


@pytest.mark.skipif(_GROUNDED_SAM_SKIP, reason=_GROUNDED_SAM_SKIP_REASON)
def test_real_grounding_dino_rejects_absent_object_label():
    """An object that genuinely isn't in the image ('a helicopter' on a
    portrait) must raise, not silently produce a meaningless whole-image
    mask -- the exact regression this integration was built to avoid (see
    resolve_label_to_box's docstring for the score/area-fraction data this
    default is tuned against)."""
    model = load_model("sam2", checkpoint=SMALL_CHECKPOINT, model_cfg=SMALL_MODEL_CFG, device="cpu")
    with pytest.raises(ValueError, match="not found in image"):
        segment(PORTRAIT, label="a helicopter", backend="sam2", model=model)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "-s"]))
