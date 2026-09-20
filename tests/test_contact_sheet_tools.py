"""Tests for contact_sheet_tools (get_contact_sheet's filter/sort/paginate/compose logic)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from darktable_mcp.tools.contact_sheet_tools import (
    compose_sheet,
    effective_thumb_width,
    filter_items,
    paginate,
    sort_items,
    validate_limit,
    validate_offset,
    write_sheet,
)


def _item(id_, filename, rating=0, selected=False, capture_time=""):
    return {
        "id": str(id_),
        "filename": filename,
        "path": f"/photos/{filename}",
        "rating": rating,
        "capture_time": capture_time,
        "selected": selected,
    }


ITEMS = [
    _item(1, "b_shot.arw", rating=0),
    _item(2, "a_shot.arw", rating=3),
    _item(3, "c_shot.arw", rating=-1),
    _item(4, "d_shot.arw", rating=0, selected=True),
    _item(5, "e_shot.arw", rating=5),
]


class TestValidation:
    def test_offset_rejects_negative(self):
        assert validate_offset(-1) == "INVALID_OFFSET"

    def test_offset_rejects_non_int(self):
        assert validate_offset("0") == "INVALID_OFFSET"
        assert validate_offset(True) == "INVALID_OFFSET"

    def test_offset_accepts_zero(self):
        assert validate_offset(0) is None

    def test_limit_rejects_out_of_range(self):
        assert validate_limit(0) == "INVALID_LIMIT"
        assert validate_limit(65) == "INVALID_LIMIT"

    def test_limit_accepts_boundaries(self):
        assert validate_limit(1) is None
        assert validate_limit(64) is None


class TestFilter:
    def test_unrated_excludes_rated_and_rejected(self):
        out = filter_items(ITEMS, "unrated")
        assert {i["id"] for i in out} == {"1", "4"}

    def test_rated_only_positive_stars(self):
        out = filter_items(ITEMS, "rated")
        assert {i["id"] for i in out} == {"2", "5"}

    def test_rejected_only_minus_one(self):
        out = filter_items(ITEMS, "rejected")
        assert {i["id"] for i in out} == {"3"}

    def test_selected(self):
        out = filter_items(ITEMS, "selected")
        assert {i["id"] for i in out} == {"4"}

    def test_all_returns_everything(self):
        assert len(filter_items(ITEMS, "all")) == len(ITEMS)

    def test_unknown_filter_raises(self):
        with pytest.raises(ValueError):
            filter_items(ITEMS, "bogus")


class TestSort:
    def test_filename_ascending(self):
        out = sort_items(ITEMS, "filename", "asc")
        assert [i["filename"] for i in out] == [
            "a_shot.arw",
            "b_shot.arw",
            "c_shot.arw",
            "d_shot.arw",
            "e_shot.arw",
        ]

    def test_filename_descending(self):
        out = sort_items(ITEMS, "filename", "desc")
        assert out[0]["filename"] == "e_shot.arw"

    def test_image_id_numeric_not_lexicographic(self):
        items = [_item(2, "x"), _item(10, "y"), _item(1, "z")]
        out = sort_items(items, "image_id", "asc")
        assert [i["id"] for i in out] == ["1", "2", "10"]

    def test_rating_sort(self):
        out = sort_items(ITEMS, "rating", "desc")
        assert out[0]["rating"] == 5


class TestPaginate:
    def test_first_page_has_more(self):
        page, next_offset, has_more = paginate(ITEMS, 0, 2)
        assert [i["id"] for i in page] == ["1", "2"]
        assert next_offset == 2
        assert has_more is True

    def test_last_page_no_more(self):
        page, next_offset, has_more = paginate(ITEMS, 4, 2)
        assert [i["id"] for i in page] == ["5"]
        assert next_offset is None
        assert has_more is False

    def test_offset_past_end_returns_empty(self):
        page, next_offset, has_more = paginate(ITEMS, 100, 25)
        assert page == []
        assert next_offset is None
        assert has_more is False

    def test_offset_applied_after_filter_and_sort(self):
        matching = sort_items(filter_items(ITEMS, "all"), "filename", "asc")
        page, _, _ = paginate(matching, 1, 1)
        assert page[0]["filename"] == "b_shot.arw"


class TestEffectiveThumbWidth:
    def test_requested_width_kept_when_it_fits(self):
        assert effective_thumb_width(5, 320) == 320

    def test_shrunk_to_respect_max_sheet_width(self):
        w = effective_thumb_width(8, 500)
        assert w < 500
        assert w * 8 + 9 * 14 <= 2400


class TestComposeSheet:
    def test_composes_without_crashing_and_marks_errors(self):
        page = ITEMS[:3]
        thumb_results = {
            "1": (None, "render failed"),
            "2": (None, "render failed"),
            "3": (None, "render failed"),
        }
        canvas = compose_sheet(
            page,
            thumb_results,
            columns=3,
            thumb_width=200,
            background="dark",
            include_filename=True,
            include_image_id=True,
            include_rating=True,
            include_sequence_number=True,
        )
        assert isinstance(canvas, np.ndarray)
        assert canvas.shape[1] > 0 and canvas.shape[0] > 0

    def test_write_sheet_produces_readable_jpeg(self, tmp_path: Path):
        canvas = np.zeros((50, 50, 3), dtype=np.uint8)
        out_path = tmp_path / "sheet.jpg"
        write_sheet(canvas, out_path)
        assert out_path.is_file()
        assert out_path.stat().st_size > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"columns": 0}, "INVALID_COLUMNS"),
        ({"columns": True}, "INVALID_COLUMNS"),
        ({"columns": 9}, "INVALID_COLUMNS"),
        ({"thumbnail_width": 0}, "INVALID_THUMBNAIL_WIDTH"),
        ({"thumbnail_width": "wide"}, "INVALID_THUMBNAIL_WIDTH"),
        ({"direction": "sideways"}, "INVALID_DIRECTION"),
        ({"background": "blue"}, "INVALID_BACKGROUND"),
    ],
)
async def test_contact_handler_rejects_bad_layout_before_bridge(arguments, code):
    import json
    from unittest.mock import Mock

    from darktable_mcp.server import DarktableMCPServer

    server = object.__new__(DarktableMCPServer)
    server.bridge = Mock()
    response = await server._handle_get_contact_sheet(arguments)
    assert json.loads(response[0].text)["error"] == code
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_contact_handler_remaps_collection_variant_paths(tmp_path, monkeypatch):
    import json
    from unittest.mock import Mock

    from darktable_mcp import server as module

    monkeypatch.setenv("DARKTABLE_MCP_RUN_DIR", str(tmp_path))
    server = object.__new__(module.DarktableMCPServer)
    server.bridge = Mock()
    original = {
        "id": "2",
        "path": "/run/photo.raw",
        "sidecar": "/run/photo_02.raw.xmp",
        "rating": 0,
    }
    server.bridge.call.return_value = [original]
    server._cli = object()
    server._download_url = Mock(return_value=None)
    render = Mock(return_value={"2": (None, "mock preview")})
    monkeypatch.setattr(module, "render_thumbnails", render)
    monkeypatch.setattr(module, "compose_sheet", Mock())
    monkeypatch.setattr(module, "write_sheet", Mock())
    monkeypatch.setattr(module, "_inline_image_content", Mock(return_value=None))
    response = await server._handle_get_contact_sheet({})
    item = render.call_args.args[1][0]
    assert item["path"] == str(tmp_path / "photo.raw")
    assert item["xmp_path"] == str(tmp_path / "photo_02.raw.xmp")
    assert original["path"] == "/run/photo.raw"
    assert json.loads(response[-1].text)["items"][0]["path"] == str(tmp_path / "photo.raw")
    assert json.loads(response[-1].text)["items"][0]["xmp_path"] == str(
        tmp_path / "photo_02.raw.xmp"
    )
