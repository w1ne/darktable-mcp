"""local_segment.py -- zero-install fallback segmenter for mask_object.

The SAM2 sidecar (`darktable-mcp/sidecar/segment.py`) needs its own venv
with torch CPU + a 149MB checkpoint (see dist/INSTALL-sidecar.md) -- an
optional, separate install most users won't have done yet. This module is
the "rough but works out of the box" tier: OpenCV's GrabCut, seeded from
the SAME point/box prompt mask_object already collects, running IN-PROCESS
inside this server (no subprocess, no extra venv, no network at runtime).

Its two runtime deps, `opencv-python-headless` and `numpy`, are ordinary
core dependencies of this package (see pyproject.toml) -- they land in the
darktable-mcp .deb's bundled venv at build time like `mcp`/`pydantic` do,
so nothing extra needs installing on the target machine.

Output contract is deliberately IDENTICAL to the sidecar's
(`sidecar/segment.py: segment()`), just with `"backend": "grabcut"`:

    {
      "polygon": [{"x":.., "y":..}, ...],
      "bbox": {"x":.., "y":.., "w":.., "h":..},
      "score": float | None,
      "num_points_raw": int,
      "num_points_simplified": int,
      "backend": "grabcut",
      "image_size": {"w": int, "h": int},
    }

so callers (`segmentation_tools.run_segmentation`, `mask_object`) can treat
either backend's result the same way downstream (contour already extracted,
simplified, normalized) -- see module docstring in `sidecar/segment.py` for
the pipeline this mirrors: mask -> largest external contour ->
Douglas-Peucker simplify -> normalize to 0..1.

Quality note: GrabCut has no learned object prior and no text grounding --
it is an interactive foreground/background color-model solver seeded by a
rect (+ optional point hints). It is the "rough" tier by design: it WILL
produce a valid, localized mask from a point/box prompt (this is what
mask_object needs to have *something* work zero-install), but it will not
match SAM2's precision on complex boundaries (hair, fine edges, low
contrast against the background). That's an accepted tradeoff, not a bug --
install the SAM2 sidecar (dist/INSTALL-sidecar.md, or run
`darktable-mcp install-sidecar`) when precise masks matter more than
zero-install friction.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

DEFAULT_TARGET_MIN = 10
# Raised from 30 -- a complex/non-convex silhouette (e.g. a human body in an
# unusual pose) loses real detail at 30 nodes; 48 gives Douglas-Peucker more
# room before it has to start cutting concave detail to hit the cap. Still
# overridable per-call (mask_object's optional max_nodes argument).
DEFAULT_TARGET_MAX = 48
DEFAULT_ITER_COUNT = 5
# Radius (as a fraction of min(width,height)) for the disc dropped around a
# points-only prompt's centroid when no box is given -- mirrors the sidecar
# StubEllipseModel's default_radius_frac so the two "no real localization
# beyond a point" fallbacks behave comparably.
DEFAULT_POINT_RADIUS_FRAC = 0.25


# --------------------------------------------------------------------------
# Mask -> contour -> simplify -> normalize (ported from sidecar/segment.py;
# duplicated rather than imported so this module has ZERO dependency on the
# sidecar/ directory, which normally does not exist on a packaged install --
# see build-deb-mcp.sh, which drops sidecar/ from the vendored source tree)
# --------------------------------------------------------------------------


def largest_external_contour(mask: np.ndarray) -> np.ndarray:
    """cv2.findContours + pick the largest-area external contour. Returns an
    (N, 2) int32 array of (x, y) pixel coordinates. Raises ValueError if the
    mask has no foreground pixels."""
    mask_u8 = (mask.astype(np.uint8)) * 255
    contours, _hierarchy = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise ValueError("mask has no foreground pixels; nothing to contour")
    largest = max(contours, key=cv2.contourArea)
    return largest.reshape(-1, 2).astype(np.int32)


def simplify_polygon(
    contour: np.ndarray,
    target_min: int = DEFAULT_TARGET_MIN,
    target_max: int = DEFAULT_TARGET_MAX,
    target_nodes: int | None = None,
    max_iter: int = 25,
) -> np.ndarray:
    """Douglas-Peucker simplification (cv2.approxPolyDP), epsilon binary-
    searched to converge the node count on `target_nodes` (defaults to
    target_max). See sidecar/segment.py's version of this function for the
    full rationale (2026-07-27 bugreport: the old "return at first hit
    anywhere in [target_min, target_max]" logic meant raising target_max
    had no effect -- the search stopped at the first, usually coarse,
    epsilon that happened to land in-range) -- logic here is intentionally
    identical."""
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
            lo = mid
        else:
            hi = mid
    return best


def normalize_polygon(points_px: np.ndarray, width: int, height: int) -> list[dict[str, float]]:
    """(N,2) pixel coords -> [{"x","y"}, ...] normalized 0..1."""
    return [{"x": float(px / width), "y": float(py / height)} for px, py in points_px]


def polygon_bbox(polygon_norm: list[dict[str, float]]) -> dict[str, float]:
    xs = [p["x"] for p in polygon_norm]
    ys = [p["y"] for p in polygon_norm]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


# --------------------------------------------------------------------------
# GrabCut seeding
# --------------------------------------------------------------------------


def _box_rect_px(box: dict[str, float], width: int, height: int, pad_frac: float = 0.02):
    """box (normalized x,y,w,h, top-left) -> clipped pixel rect
    (x0, y0, x1, y1), padded a little so GrabCut has some background margin
    around the box to learn a background color model from."""
    x0 = box["x"] * width
    y0 = box["y"] * height
    x1 = (box["x"] + box["w"]) * width
    y1 = (box["y"] + box["h"]) * height
    pad = pad_frac * min(width, height)
    rx0 = max(int(round(x0 - pad)), 0)
    ry0 = max(int(round(y0 - pad)), 0)
    rx1 = min(int(round(x1 + pad)), width)
    ry1 = min(int(round(y1 + pad)), height)
    return rx0, ry0, rx1, ry1


def _points_rect_px(points: list[dict[str, Any]], width: int, height: int):
    """No box given -- build a rect around the foreground (label=1) points'
    centroid, radius DEFAULT_POINT_RADIUS_FRAC * min(w,h), same spirit as
    StubEllipseModel's point-only fallback in the sidecar."""
    fg = [p for p in points if p.get("label", 1) == 1] or points
    xs = [p["x"] * width for p in fg]
    ys = [p["y"] * height for p in fg]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    r = DEFAULT_POINT_RADIUS_FRAC * min(width, height)
    rx0 = max(int(round(cx - r)), 0)
    ry0 = max(int(round(cy - r)), 0)
    rx1 = min(int(round(cx + r)), width)
    ry1 = min(int(round(cy + r)), height)
    return rx0, ry0, rx1, ry1


def _label_only_rect_px(width: int, height: int, margin_frac: float = 0.1):
    """No point/box, only a free-text `label` -- GrabCut (like SAM2, see
    SAM2Model docstring in sidecar/segment.py) has no text grounding, so
    there is no real localization signal to seed from. Best-effort: a
    generic centered rect, same non-answer any geometry-only fallback would
    give a label-only prompt."""
    mx = int(round(margin_frac * width))
    my = int(round(margin_frac * height))
    return mx, my, width - mx, height - my


def _seed_point_labels(
    mask: np.ndarray, points: list[dict[str, Any]], width: int, height: int
) -> None:
    """Stamp small certain-FG/BG discs at each prompt point on top of the
    rect-seeded mask, so explicit include/exclude points refine GrabCut's
    prior beyond the rect alone."""
    radius = max(3, int(round(0.01 * min(width, height))))
    for p in points:
        cx = int(round(p["x"] * width))
        cy = int(round(p["y"] * height))
        label = p.get("label", 1)
        value = cv2.GC_FGD if label == 1 else cv2.GC_BGD
        cv2.circle(mask, (cx, cy), radius, int(value), -1)


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def segment_grabcut(
    image_path: str,
    points: list[dict[str, Any]] | None = None,
    box: dict[str, float] | None = None,
    label: str | None = None,
    target_min: int = DEFAULT_TARGET_MIN,
    target_max: int = DEFAULT_TARGET_MAX,
    target_nodes: int | None = None,
    iter_count: int = DEFAULT_ITER_COUNT,
) -> dict[str, Any]:
    """Same contract/inputs as sidecar/segment.py's segment(), computed
    in-process via OpenCV GrabCut instead of a SAM2 subprocess.

    Raises ValueError on a genuinely unusable prompt/result (empty mask) --
    callers (segmentation_tools.run_segmentation) are expected to wrap this
    the same way they wrap the sidecar call.
    """
    if not points and not box and not label:
        raise ValueError("segment_grabcut() needs at least one of points, box, label")

    # cv2.imread (not PIL) -- keeps this module's only image-IO dependency
    # opencv-python-headless, which is bundled anyway; no need for Pillow
    # as an extra core dependency just for this. GrabCut works directly on
    # BGR, which is exactly what cv2.imread returns natively.
    image_bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise ValueError(f"could not read image (unsupported format or missing file): {image_path}")
    height, width = image_bgr.shape[:2]
    if height < 2 or width < 2:
        raise ValueError(f"image too small to segment: {width}x{height}")

    # Start everything as "probably background", then mark the seeded rect
    # as "probably foreground" -- GC_INIT_WITH_MASK, not GC_INIT_WITH_RECT,
    # so points can also stamp certain FG/BG on top (see _seed_point_labels).
    mask = np.full((height, width), cv2.GC_PR_BGD, dtype=np.uint8)

    if box:
        rx0, ry0, rx1, ry1 = _box_rect_px(box, width, height)
    elif points:
        rx0, ry0, rx1, ry1 = _points_rect_px(points, width, height)
    else:
        rx0, ry0, rx1, ry1 = _label_only_rect_px(width, height)

    if rx1 <= rx0 or ry1 <= ry0:
        raise ValueError("prompt resolved to an empty/degenerate region")
    mask[ry0:ry1, rx0:rx1] = cv2.GC_PR_FGD

    if points:
        _seed_point_labels(mask, points, width, height)

    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(image_bgr, mask, None, bgd_model, fgd_model, iter_count, cv2.GC_INIT_WITH_MASK)
    except cv2.error as exc:
        raise ValueError(f"grabcut failed: {exc}") from exc

    fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 1, 0).astype(np.uint8)
    if fg.sum() == 0:
        raise ValueError("grabcut produced an empty mask for this prompt")

    raw_contour = largest_external_contour(fg)
    simplified = simplify_polygon(
        raw_contour, target_min=target_min, target_max=target_max, target_nodes=target_nodes
    )
    polygon_norm = normalize_polygon(simplified, width, height)
    bbox_norm = polygon_bbox(polygon_norm)

    # Heuristic "confidence", NOT a model score (GrabCut has none): fraction
    # of the seeded rect GrabCut kept as foreground. A tight, confident cut
    # keeps most of the rect; a cut that collapsed to a sliver scores low.
    # Purely informational -- treat as a rough signal, unlike SAM2's real
    # per-mask IoU-calibrated score.
    rect_area = max((rx1 - rx0) * (ry1 - ry0), 1)
    rect_fg = int(fg[ry0:ry1, rx0:rx1].sum())
    score = min(1.0, rect_fg / rect_area)

    return {
        "polygon": polygon_norm,
        "bbox": bbox_norm,
        "score": float(score),
        "num_points_raw": int(len(raw_contour)),
        "num_points_simplified": int(len(simplified)),
        "backend": "grabcut",
        "image_size": {"w": int(width), "h": int(height)},
    }
