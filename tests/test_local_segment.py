"""Tests for darktable_mcp/tools/local_segment.py's simplify_polygon --
mirrors sidecar/test_segment.py's regression guard for the 2026-07-27
bugreport (max_nodes had no real effect on the GrabCut fallback's output
either, same duplicated algorithm/bug as the SAM2 sidecar's version).
"""

import cv2
import numpy as np

from darktable_mcp.tools.local_segment import largest_external_contour, simplify_polygon


def _circle_contour(radius=180, canvas=500):
    mask = np.zeros((canvas, canvas), dtype=np.uint8)
    cv2.circle(mask, (canvas // 2, canvas // 2), radius, 1, thickness=-1)
    return largest_external_contour(mask.astype(bool))


def test_simplify_actually_uses_the_full_max_nodes_budget():
    raw = _circle_contour()

    n_12 = len(simplify_polygon(raw, target_min=10, target_max=12))
    n_48 = len(simplify_polygon(raw, target_min=10, target_max=48))
    n_180 = len(simplify_polygon(raw, target_min=10, target_max=180))

    assert n_12 <= 12
    assert n_48 >= 40  # was collapsing to ~11-12 here before the fix
    assert n_180 >= 150  # was collapsing to ~11-12 here before the fix
    assert n_12 < n_48 < n_180


def test_simplify_target_nodes_overrides_target_max():
    raw = _circle_contour()
    result = simplify_polygon(raw, target_min=10, target_max=180, target_nodes=20)
    assert 15 <= len(result) <= 25
