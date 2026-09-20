import cv2
import numpy as np
import pytest

from darktable_mcp.tools.lut_tools import (
    LutRootNotConfiguredError,
    compose_lut_compare_grid,
    effective_cell_width,
    parse_cube_header,
    resolve_lut_path,
    scan_lut_directory,
)


def test_unconfigured_root():
    with pytest.raises(LutRootNotConfiguredError, match="def_path is not set"):
        scan_lut_directory("")


def test_scan_is_sorted_relative_and_filters_extensions(tmp_path):
    (tmp_path / "looks").mkdir()
    for name in ("Z.CUBE", "a.3dl", "b.png", "ignored.txt"):
        (tmp_path / "looks" / name).write_text("")
    items = scan_lut_directory(str(tmp_path), "looks")
    assert [item["path"] for item in items] == ["looks/a.3dl", "looks/b.png", "looks/Z.CUBE"]
    assert [item["format"] for item in items] == ["3dl", "png", "cube"]


def test_rejects_traversal_absolute_and_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.cube"
    outside.write_text("")
    (root / "escape.cube").symlink_to(outside)
    for name in ("../outside.cube", str(outside), "escape.cube", r"..\outside.cube"):
        with pytest.raises(ValueError):
            resolve_lut_path(str(root), name)
    assert scan_lut_directory(str(root)) == []
    with pytest.raises(ValueError):
        scan_lut_directory(str(root), "..")


def test_resolve_file_and_missing_file(tmp_path):
    lut = tmp_path / "good.cube"
    lut.write_text("")
    assert resolve_lut_path(str(tmp_path), "good.cube") == lut
    with pytest.raises(FileNotFoundError):
        resolve_lut_path(str(tmp_path), "missing.cube")


def test_cube_header_parses_metadata_only(tmp_path):
    lut = tmp_path / "test.cube"
    lut.write_text(
        '# comment\nTITLE "Warm look" # trailing\n'
        'LUT_3D_SIZE 33\nDOMAIN_MIN 0 0 0\n0 0 0\nTITLE "not header"\n'
    )
    assert parse_cube_header(lut) == {"title": "Warm look", "size": 33}


def test_cube_header_tolerates_malformed_metadata(tmp_path):
    lut = tmp_path / "bad.cube"
    lut.write_text('LUT_3D_SIZE nope\nTITLE "unterminated\n0 0 0\n')
    assert parse_cube_header(lut) == {}


def test_grid_preserves_color_and_aspect_without_upscaling(tmp_path):
    path = tmp_path / "red.png"
    image = np.zeros((20, 40, 3), dtype=np.uint8)
    image[:, :] = (0, 0, 255)
    assert cv2.imwrite(str(path), image)
    entries = [
        {"label": "warm.cube", "image_path": str(path), "error": None},
        {"label": "missing.cube", "image_path": None, "error": "not found"},
    ]
    grid = compose_lut_compare_grid(entries, 2, 160, "dark")
    assert grid.dtype == np.uint8
    assert grid.shape[1] == 320
    assert np.count_nonzero(np.all(grid == (0, 0, 255), axis=2)) == 800
    assert np.any(grid[:, 160:] != 24)


def test_grid_handles_unreadable_preview(tmp_path):
    grid = compose_lut_compare_grid(
        [{"label": "x", "image_path": str(tmp_path / "gone"), "error": None}], 1, 160, "light"
    )
    assert grid.shape[1] == 160
    assert np.any(grid != 242)


def test_width_is_bounded_and_invalid_dimensions_rejected():
    assert effective_cell_width(4, 1000) == 600
    assert effective_cell_width(4, 480) == 480
    with pytest.raises(ValueError):
        effective_cell_width(0, 480)
    with pytest.raises(ValueError):
        effective_cell_width(4, -1)
