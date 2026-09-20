"""Tests for the main MCP server."""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from darktable_mcp.darktable.cli_wrapper import ExportResult
from darktable_mcp.server import DarktableMCPServer
from darktable_mcp.utils.viewport_coords import ViewportCoordinateError

# dev_current_image reply used wherever a viewport snapshot has to be bound to
# a concrete darkroom image (the id/filename from the 2026-07-26 bugreport).
IMAGE_13406 = {
    "has_image": True,
    "id": 13406,
    "filename": "20260724_0108.ARW",
    "path": "/photos/20260724_0108.ARW",
}


class TestDarktableMCPServer:
    """Test cases for DarktableMCPServer."""

    def test_server_initialization(self):
        server = DarktableMCPServer()
        assert server is not None
        assert hasattr(server, "app")

    def test_server_has_required_tools(self):
        server = DarktableMCPServer()
        expected_tools = {
            "import_from_camera",
            "export_images",
            "extract_previews",
            "apply_ratings_batch",
            "open_in_darktable",
            "view_photos",
            "get_contact_sheet",
            "rate_photos",
            "tag_photo",
            "set_photo_note",
            "get_photo_note",
            "list_collections",
            "list_photos_in_collection",
            "import_batch",
            "list_styles",
            "apply_preset",
            "open_image_in_darkroom",
            "navigate_photo",
            "get_current_image",
            "list_modules",
            "get_params",
            "set_params",
            "get_blend_params",
            "set_blend_params",
            "get_preview",
            "enable_module",
            "list_luts",
            "preview_lut",
            "compare_luts",
            "add_instance",
            "get_viewport",
            "set_viewport",
            "restore_viewport",
            "capture_viewport",
            "add_path_mask",
            "add_path_mask_in_viewport",
            "render_module_mask",
            "retouch_add_shape",
            "retouch_add_shape_in_viewport",
            "retouch_update_shape_in_viewport",
            "retouch_delete_shape",
            "retouch_delete_shapes",
            "retouch_list_shapes",
            "retouch_render_overlay",
            "mask_object",
            "mask_raster",
            "list_masks",
            "get_module_mask",
            "get_mask_geometry",
            "rename_mask",
            "delete_mask",
            "attach_mask",
            "detach_mask",
            "set_module_mask",
        }
        assert set(server.list_tools()) == expected_tools

    @pytest.mark.asyncio
    async def test_server_can_start(self):
        server = DarktableMCPServer()

        @asynccontextmanager
        async def fake_stdio():
            yield (AsyncMock(), AsyncMock())

        with (
            patch("darktable_mcp.server.stdio_server", fake_stdio),
            patch.object(server.app, "run", new=AsyncMock(return_value=None)) as mock_run,
        ):
            await server.start()
            mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_import_from_camera_handler():
    server = DarktableMCPServer()
    mock_tools = Mock()
    mock_tools.import_from_camera.return_value = (
        "Copied 5 file(s) from Nikon DSC D800E (usb:002,002)\nDestination: /tmp/import-2026-04-26"
    )
    server.camera_tools = mock_tools

    result = await server._handle_import_from_camera({"destination": "/tmp/import-2026-04-26"})

    assert len(result) == 1
    assert "Copied 5 file(s)" in result[0].text
    assert "Nikon DSC D800E" in result[0].text
    mock_tools.import_from_camera.assert_called_once_with({"destination": "/tmp/import-2026-04-26"})


def test_server_registers_import_from_camera_tool():
    server = DarktableMCPServer()
    tool_names = [t.name for t in server._tool_definitions()]
    assert "import_from_camera" in tool_names


@pytest.mark.asyncio
async def test_handle_view_photos_returns_formatted_list():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = [
        {"id": "1", "filename": "a.NEF", "path": "/photos/a.NEF", "rating": 5},
        {"id": "2", "filename": "b.NEF", "path": "/photos/b.NEF", "rating": 4},
    ]
    result = await server._handle_view_photos({"filter": "", "limit": 10})
    assert len(result) == 1
    text = result[0].text
    assert "a.NEF" in text
    assert "b.NEF" in text
    # The absolute file path must be in the formatted output so the agent
    # can pass it straight to export_images. Otherwise view_photos and
    # export_images don't compose.
    assert "/photos/a.NEF" in text
    assert "/photos/b.NEF" in text
    server.bridge.call.assert_called_once_with("view_photos", {"filter": "", "limit": 10})


@pytest.mark.asyncio
async def test_handle_view_photos_no_results_message():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = []
    result = await server._handle_view_photos({})
    assert "No photos" in result[0].text


@pytest.mark.asyncio
async def test_handle_view_photos_friendly_message_when_plugin_missing():
    from darktable_mcp.bridge.client import BridgePluginNotInstalledError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgePluginNotInstalledError("missing")
    result = await server._handle_view_photos({})
    assert "install-plugin" in result[0].text


@pytest.mark.asyncio
async def test_handle_view_photos_friendly_message_when_dt_not_running():
    from darktable_mcp.bridge.client import BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")
    result = await server._handle_view_photos({})
    assert "darktable" in result[0].text.lower()
    assert "open" in result[0].text.lower() or "running" in result[0].text.lower()


@pytest.mark.asyncio
async def test_handle_rate_photos_returns_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"updated": 3}
    result = await server._handle_rate_photos({"photo_ids": ["1", "2", "3"], "rating": 4})
    assert "3" in result[0].text
    assert "4" in result[0].text
    server.bridge.call.assert_called_once_with(
        "rate_photos", {"photo_ids": ["1", "2", "3"], "rating": 4}
    )


@pytest.mark.asyncio
async def test_handle_set_photo_note_saved():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"updated": 1}
    result = await server._handle_set_photo_note({"photo_id": "1", "note": "sharp, good crop"})
    assert "saved" in result[0].text.lower()
    assert "1" in result[0].text
    server.bridge.call.assert_called_once_with(
        "set_photo_note", {"photo_id": "1", "note": "sharp, good crop"}
    )


@pytest.mark.asyncio
async def test_handle_set_photo_note_missing_photo():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"updated": 0}
    result = await server._handle_set_photo_note({"photo_id": "999", "note": "x"})
    assert "not found" in result[0].text.lower()


@pytest.mark.asyncio
async def test_handle_get_photo_note_returns_text():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"found": True, "note": "sharp, good crop"}
    result = await server._handle_get_photo_note({"photo_id": "1"})
    assert result[0].text == "sharp, good crop"
    server.bridge.call.assert_called_once_with("get_photo_note", {"photo_id": "1"})


@pytest.mark.asyncio
async def test_handle_get_photo_note_missing_photo():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"found": False}
    result = await server._handle_get_photo_note({"photo_id": "999"})
    assert "not found" in result[0].text.lower()


@pytest.mark.asyncio
async def test_handle_import_batch_returns_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"imported": 12, "source_path": "/path/foo"}
    result = await server._handle_import_batch({"source_path": "/path/foo"})
    assert "Imported 12" in result[0].text
    assert "/path/foo" in result[0].text
    server.bridge.call.assert_called_once_with("import_batch", {"source_path": "/path/foo"})


@pytest.mark.asyncio
async def test_handle_import_batch_friendly_error_when_plugin_missing():
    from darktable_mcp.bridge.client import BridgePluginNotInstalledError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgePluginNotInstalledError("missing")
    result = await server._handle_import_batch({"source_path": "/x"})
    assert "install-plugin" in result[0].text


@pytest.mark.asyncio
async def test_handle_import_batch_friendly_error_when_dt_not_running():
    from darktable_mcp.bridge.client import BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")
    result = await server._handle_import_batch({"source_path": "/x"})
    assert "darktable" in result[0].text.lower()


@pytest.mark.asyncio
async def test_handle_list_styles_returns_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "styles": [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}],
        "count": 2,
    }
    result = await server._handle_list_styles({})
    text = result[0].text
    assert "2 styles installed" in text
    assert "alpha" in text
    assert "beta" in text


@pytest.mark.asyncio
async def test_handle_list_styles_empty():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"styles": [], "count": 0}
    result = await server._handle_list_styles({})
    assert "No styles" in result[0].text


@pytest.mark.asyncio
async def test_handle_list_styles_truncates_at_50():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "styles": [{"name": f"s{i}", "description": ""} for i in range(75)],
        "count": 75,
    }
    result = await server._handle_list_styles({})
    text = result[0].text
    assert "75 styles installed" in text
    assert "and 25 more" in text


@pytest.mark.asyncio
async def test_handle_apply_preset_returns_applied_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"applied": 3, "missed": [], "preset_name": "myStyle"}
    result = await server._handle_apply_preset(
        {
            "photo_ids": ["1", "2", "3"],
            "preset_name": "myStyle",
        }
    )
    text = result[0].text
    assert "myStyle" in text
    assert "3 photo" in text


@pytest.mark.asyncio
async def test_handle_apply_preset_reports_missed():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "applied": 1,
        "missed": ["999"],
        "preset_name": "myStyle",
    }
    result = await server._handle_apply_preset(
        {
            "photo_ids": ["1", "999"],
            "preset_name": "myStyle",
        }
    )
    text = result[0].text
    assert "999" in text
    assert "Missed" in text


@pytest.mark.asyncio
async def test_handle_apply_preset_friendly_error_when_dt_not_running():
    from darktable_mcp.bridge.client import BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")
    result = await server._handle_apply_preset(
        {
            "photo_ids": ["1"],
            "preset_name": "x",
        }
    )
    assert "darktable" in result[0].text.lower()


@pytest.mark.asyncio
async def test_handle_export_images_writes_side_file_and_short_summary(tmp_path):
    """`export_images` used to dump the full per-file map inline, blowing
    Claude's token budget at 400+ files. The handler now writes per-file
    results to a JSONL side file and returns only counts + the path."""
    import json

    server = DarktableMCPServer()
    fake_results = [
        ExportResult("/in/A.NEF", f"{tmp_path}/A.jpg", True, None),
        ExportResult("/in/B.NEF", f"{tmp_path}/B.jpg", True, None),
        ExportResult("/in/C.NEF", None, False, "Export failed: Boom"),
    ]
    server._cli = Mock()
    server._cli.batch_export.return_value = fake_results

    result = await server._handle_export_images(
        {
            "photo_ids": ["/in/A.NEF", "/in/B.NEF", "/in/C.NEF"],
            "output_path": str(tmp_path),
            "format": "jpeg",
            "quality": 95,
        }
    )
    text = result[0].text

    # Summary stays compact: counts + side file pointer + first error.
    assert "exported: 2" in text
    assert "failed: 1" in text
    assert ".export_images.jsonl" in text
    assert "Boom" in text  # first error gets a short snippet
    # Crucially, the per-file output map is NOT inlined.
    assert "/in/A.NEF" not in text
    assert "/in/B.NEF" not in text

    # Side file holds the full record, one JSON per line.
    side = tmp_path / ".export_images.jsonl"
    assert side.exists()
    lines = [json.loads(line) for line in side.read_text().splitlines()]
    assert len(lines) == 3
    assert {line["input"] for line in lines} == {r.input for r in fake_results}


@pytest.mark.asyncio
async def test_handle_export_images_validates_required_args():
    server = DarktableMCPServer()
    r = await server._handle_export_images({"photo_ids": ["/x"]})
    assert "output_path" in r[0].text
    r = await server._handle_export_images({"output_path": "/tmp/x"})
    assert "photo_ids" in r[0].text


@pytest.mark.asyncio
async def test_handle_export_images_forwards_xmp_paths(tmp_path):
    """Bugreport 2026-07-31: export_images silently exported the wrong
    duplicate/version because it never told darktable-cli which sidecar
    to use. xmp_paths must reach cli.batch_export, null entries -> None."""
    server = DarktableMCPServer()
    server._cli = Mock()
    server._cli.batch_export.return_value = [
        ExportResult("/in/A.NEF", f"{tmp_path}/A.jpg", True, None)
    ]

    await server._handle_export_images(
        {
            "photo_ids": ["/in/A.NEF"],
            "xmp_paths": ["/in/A_02.NEF.xmp"],
            "output_path": str(tmp_path),
            "format": "jpeg",
        }
    )

    call_kwargs = server._cli.batch_export.call_args.kwargs
    assert call_kwargs["xmp_paths"] == [Path("/in/A_02.NEF.xmp")]


@pytest.mark.asyncio
async def test_handle_export_images_rejects_mismatched_xmp_paths_length():
    server = DarktableMCPServer()
    server._cli = Mock()
    result = await server._handle_export_images(
        {
            "photo_ids": ["/in/A.NEF", "/in/B.NEF"],
            "xmp_paths": ["/in/A.NEF.xmp"],  # length 1, photo_ids length 2
            "output_path": "/tmp/out",
            "format": "jpeg",
        }
    )
    assert "must match" in result[0].text
    server._cli.batch_export.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_success():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "formid": 42,
        "algorithm": "heal",
        "wavelet_scale": 2,
        "radius": 0.02,
        "feather": 0.1,
        "opacity": 1.0,
    }
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "heal",
            "target": {"x": 0.4, "y": 0.3},
            "source": {"x": 0.35, "y": 0.3},
            "radius": 0.02,
            "wavelet_scale": 2,
        }
    )
    assert "formid=42" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_retouch_add_shape",
        {
            "op": "retouch",
            "instance": 0,
            "algorithm": "heal",
            "target": {"x": 0.4, "y": 0.3},
            "source": {"x": 0.35, "y": 0.3},
            "radius": 0.02,
            "feather": 0.0,
            "opacity": 1.0,
            "wavelet_scale": 2,
        },
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_requires_source():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "heal",
            "target": {"x": 0.4, "y": 0.3},
            "radius": 0.02,
        }
    )
    assert "source" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_blur_no_source_required():
    """2026-07-31 blur/fill batch: blur has no source point at all -- omitting
    it must NOT be rejected the way heal/clone's missing source is."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "formid": 42,
        "algorithm": "blur",
        "wavelet_scale": 3,
        "blur_type": "gaussian",
        "blur_radius": 8.0,
    }
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "blur",
            "target": {"x": 0.4, "y": 0.3},
            "radius": 0.05,
            "wavelet_scale": 3,
            "blur_type": "gaussian",
            "blur_radius": 8.0,
        }
    )
    assert "blur_type=gaussian" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_retouch_add_shape",
        {
            "op": "retouch",
            "instance": 0,
            "algorithm": "blur",
            "target": {"x": 0.4, "y": 0.3},
            "radius": 0.05,
            "feather": 0.0,
            "opacity": 1.0,
            "wavelet_scale": 3,
            "blur_type": "gaussian",
            "blur_radius": 8.0,
        },
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_fill_no_source_required():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "formid": 43,
        "algorithm": "fill",
        "fill_mode": "erase",
        "fill_brightness": 0.1,
    }
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "fill",
            "target": {"x": 0.6, "y": 0.7},
            "radius": 0.04,
            "fill_mode": "erase",
            "fill_brightness": 0.1,
        }
    )
    assert "fill_mode=erase" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_retouch_add_shape",
        {
            "op": "retouch",
            "instance": 0,
            "algorithm": "fill",
            "target": {"x": 0.6, "y": 0.7},
            "radius": 0.04,
            "feather": 0.0,
            "opacity": 1.0,
            "fill_mode": "erase",
            "fill_brightness": 0.1,
        },
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_rejects_non_circle():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "heal",
            "shape_type": "brush",
            "target": {"x": 0.4, "y": 0.3},
            "source": {"x": 0.35, "y": 0.3},
            "radius": 0.02,
        }
    )
    assert "circle" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_path_requires_points():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "heal",
            "shape_type": "path",
            "source": {"x": 0.35, "y": 0.3},
        }
    )
    assert "points" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_path_success():
    """2026-07-31 path batch: target/radius are NOT required for a path
    (a polygon has no single center/radius); points is forwarded instead."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "formid": 88,
        "algorithm": "heal",
        "shape_type": "path",
        "wavelet_scale": 0,
        "points": 3,
        "smooth": False,
    }
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "heal",
            "shape_type": "path",
            "source": {"x": 0.35, "y": 0.3},
            "points": [
                {"x": 0.1, "y": 0.1},
                {"x": 0.2, "y": 0.1},
                {"x": 0.15, "y": 0.2},
            ],
            "smooth": False,
        }
    )
    assert "shape_type=path" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_retouch_add_shape",
        {
            "op": "retouch",
            "instance": 0,
            "algorithm": "heal",
            "feather": 0.0,
            "opacity": 1.0,
            "source": {"x": 0.35, "y": 0.3},
            "shape_type": "path",
            "points": [
                {"x": 0.1, "y": 0.1},
                {"x": 0.2, "y": 0.1},
                {"x": 0.15, "y": 0.2},
            ],
            "smooth": False,
        },
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_ellipse_success():
    """2026-07-31 ellipse batch: shape_type='ellipse' forwards radius_b/rotation
    and reports them back."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "formid": 77,
        "algorithm": "heal",
        "shape_type": "ellipse",
        "wavelet_scale": 0,
        "radius_b": 0.02,
        "rotation": 30.0,
    }
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "heal",
            "shape_type": "ellipse",
            "target": {"x": 0.4, "y": 0.3},
            "source": {"x": 0.35, "y": 0.3},
            "radius": 0.04,
            "radius_b": 0.02,
            "rotation": 30.0,
        }
    )
    assert "shape_type=ellipse" in result[0].text
    assert "radius_b=0.02" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_retouch_add_shape",
        {
            "op": "retouch",
            "instance": 0,
            "algorithm": "heal",
            "target": {"x": 0.4, "y": 0.3},
            "radius": 0.04,
            "feather": 0.0,
            "opacity": 1.0,
            "source": {"x": 0.35, "y": 0.3},
            "shape_type": "ellipse",
            "radius_b": 0.02,
            "rotation": 30.0,
        },
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_propagates_c_error():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"error": "300 shape limit reached"}
    result = await server._handle_retouch_add_shape(
        {
            "algorithm": "clone",
            "target": {"x": 0.4, "y": 0.3},
            "source": {"x": 0.35, "y": 0.3},
            "radius": 0.02,
        }
    )
    assert "300 shape limit reached" in result[0].text


@pytest.mark.asyncio
async def test_handle_retouch_delete_shape_success():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"ok": True}
    result = await server._handle_retouch_delete_shape({"formid": 42})
    assert "42" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_retouch_delete_shape",
        {"op": "retouch", "instance": 0, "formid": 42},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_retouch_delete_shape_requires_formid():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_retouch_delete_shape({})
    assert "formid" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retouch_list_shapes_formats_shapes():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "module": "retouch",
        "instance": 0,
        "num_scales": 4,
        "curr_scale": 1,
        "merge_from_scale": 0,
        "shapes": [
            {
                "formid": 42,
                "algorithm": "heal",
                "shape_type": "circle",
                "target": {"x": 0.4, "y": 0.3},
                "source": {"x": 0.35, "y": 0.3},
                "radius": 0.02,
                "feather": 0.1,
                "wavelet_scale": 2,
            },
        ],
    }
    result = await server._handle_retouch_list_shapes({})
    text = result[0].text
    assert "shapes=1" in text
    assert "formid=42" in text
    assert "algorithm=heal" in text
    server.bridge.call.assert_called_once_with(
        "dev_retouch_list_shapes", {"op": "retouch", "instance": 0}, timeout=15.0
    )


@pytest.mark.asyncio
async def test_handle_retouch_list_shapes_shows_ellipse_geometry():
    """2026-07-31 ellipse step 2: an ellipse shape's radius_b/rotation must be
    visible in the report, not just circle's plain radius/feather."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "module": "retouch",
        "instance": 0,
        "num_scales": 0,
        "curr_scale": 0,
        "merge_from_scale": 0,
        "shapes": [
            {
                "formid": 77,
                "algorithm": "heal",
                "shape_type": "ellipse",
                "target": {"x": 0.5, "y": 0.4},
                "source": {"x": 0.55, "y": 0.4},
                "radius": 0.06,
                "radius_b": 0.02,
                "rotation": 30.0,
                "feather": 0.01,
                "wavelet_scale": 0,
            },
        ],
    }
    result = await server._handle_retouch_list_shapes({})
    text = result[0].text
    assert "shape_type=ellipse" in text
    assert "radius_b=0.02" in text
    assert "rotation=30.0" in text


@pytest.mark.asyncio
async def test_handle_capture_viewport_success():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": {"region": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}}},
        IMAGE_13406,
        {"path": "/tmp/preview.png", "width": 647, "height": 702},
        IMAGE_13406,
    ]
    result = await server._handle_capture_viewport({"return_image": False})
    text = result[0].text
    assert "snapshot_id=vp_" in text
    assert "647x702" in text
    assert "id=13406" in text
    assert len(server._viewport_snapshots) == 1
    snapshot = next(iter(server._viewport_snapshots.values()))
    assert snapshot["region"] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    assert snapshot["render"] == {"path": "/tmp/preview.png", "width": 647, "height": 702}
    # The snapshot must carry the image it is a picture of, so later mutating
    # calls can refuse to write to a different photo.
    assert snapshot["image"]["id"] == 13406
    # 2026-07-31 fix: capture_viewport must read the pixels from THIS
    # viewport's own pipe (dev_preview's viewport= param), and must NOT also
    # pass `region` -- dev->full.pipe's own backbuf is already exactly that
    # crop once zoomed, passing region again would double-crop it.
    preview_call = server.bridge.call.call_args_list[2]
    assert preview_call.args[0] == "dev_preview"
    assert preview_call.args[1] == {"max_w": 1400, "max_h": 1400, "viewport": "main"}


@pytest.mark.asyncio
async def test_handle_capture_viewport_preview2_reads_preview2_pipe():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"preview2": {"active": True, "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}}},
        IMAGE_13406,
        {"path": "/tmp/preview2.png", "width": 900, "height": 600},
        IMAGE_13406,
    ]
    result = await server._handle_capture_viewport({"viewport": "preview2", "return_image": False})
    assert "snapshot_id=vp_" in result[0].text
    preview_call = server.bridge.call.call_args_list[2]
    assert preview_call.args[1] == {"max_w": 1400, "max_h": 1400, "viewport": "preview2"}


@pytest.mark.asyncio
async def test_handle_get_preview_viewport_passthrough():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "status": "ok",
        "path": "/tmp/p.png",
        "width": 500,
        "height": 400,
        "viewport_source": "main",
    }
    result = await server._handle_get_preview({"viewport": "main"})
    first_call = server.bridge.call.call_args_list[0]
    assert first_call.args == ("dev_preview", {"max_w": 1024, "max_h": 1024, "viewport": "main"})
    assert first_call.kwargs == {"timeout": 20.0}
    assert "source: main" in result[-1].text


@pytest.mark.asyncio
async def test_handle_get_preview_omitted_viewport_unchanged():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "status": "ok",
        "path": "/tmp/p.png",
        "width": 500,
        "height": 400,
    }
    result = await server._handle_get_preview({})
    first_call = server.bridge.call.call_args_list[0]
    assert first_call.args == ("dev_preview", {"max_w": 1024, "max_h": 1024})
    assert first_call.kwargs == {"timeout": 20.0}
    assert "source: preview_pipe" in result[-1].text


@pytest.mark.asyncio
async def test_handle_get_preview_rejects_bad_viewport():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_get_preview({"viewport": "nope"})
    assert "viewport must be" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_capture_viewport_preview2_inactive():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "main": {"region": {"x": 0, "y": 0, "w": 1, "h": 1}},
        "preview2": {"active": False},
    }
    result = await server._handle_capture_viewport({"viewport": "preview2"})
    assert "viewport_not_active" in result[0].text


# ---- set_viewport / restore_viewport (2026-07-31 set-viewport-design) -----
#
# Fixture geometry chosen so the aspect-expand math is hand-verifiable:
# viewport_width/processed_width and viewport_height/processed_height are
# both exactly 0.5, so k = (viewport_w*proch)/(viewport_h*procw) == 1.0 --
# a square region request needs no expansion, a non-square one does, by a
# predictable amount.
_VP_MAIN_BASE = {
    "region": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.2},
    "scale": 0.5,
    "zoom_x": -0.07,
    "zoom_y": 0.11,
    "viewport_width": 1000,
    "viewport_height": 700,
    "processed_width": 2000,
    "processed_height": 1400,
}

_SET_VIEWPORT_PREVIOUS = {
    "zoom": 3,
    "zoom_label": "free",
    "closeup": 0,
    "zoom_x": -0.07,
    "zoom_y": 0.11,
    "scale": 0.5,
}


@pytest.mark.asyncio
async def test_handle_set_viewport_region_no_aspect_adjustment():
    server = DarktableMCPServer()
    server.bridge = Mock()
    # sequence: (1) dev_get_viewport [current state], (2) dev_set_viewport,
    # (3) dev_get_viewport [re-read achieved], (4) dev_preview [probe].
    server.bridge.call.side_effect = [
        {"main": dict(_VP_MAIN_BASE)},
        {
            "ok": True,
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "applied": {"zoom_x": -0.1, "zoom_y": -0.05, "scale": 2.5},
            "clamped": [],
            "pipe_ready": True,
            "waited_ms": 30,
        },
        {"main": {"region": {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.2}, "scale": 2.5}},
        {"path": "/tmp/probe.png", "width": 1000, "height": 1000},
    ]
    # square request (rw==rh) with k==1.0 -> no expansion needed.
    result = await server._handle_set_viewport(
        {
            "region": {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.2},
        }
    )
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["aspect_adjusted"] is False
    assert payload["achieved"]["region"] == {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.2}
    assert payload["achieved"]["renderable_px"] == {"w": 1000, "h": 1000}
    assert payload["pipe_ready"] is True
    assert payload["previous"] == _SET_VIEWPORT_PREVIOUS
    assert payload["clamped"] == []

    # verify the inverted region->zoom_x/zoom_y/scale math handed to the
    # bridge: center=(0.4,0.5) -> zoom_x=-0.1, zoom_y=0.0; required_h=rw=rh
    # =0.2 -> scale = viewport_h/(proch*0.2) = 700/(1400*0.2) = 2.5.
    set_call = server.bridge.call.call_args_list[1]
    assert set_call.args[0] == "dev_set_viewport"
    params = set_call.args[1]
    assert params["viewport"] == "main"
    assert params["scale"] == pytest.approx(2.5)
    assert params["zoom_x"] == pytest.approx(-0.1)
    assert params["zoom_y"] == pytest.approx(0.0)
    assert params["wait_for_pipe"] is True


@pytest.mark.asyncio
async def test_handle_set_viewport_region_aspect_expand():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": dict(_VP_MAIN_BASE)},
        {
            "ok": True,
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "applied": {"zoom_x": -0.1, "zoom_y": -0.05, "scale": 2.5},
            "clamped": [],
            "pipe_ready": True,
            "waited_ms": 30,
        },
        {"main": {"region": {"x": 0.3, "y": 0.35, "w": 0.2, "h": 0.2}, "scale": 2.5}},
        {"path": "/tmp/probe.png", "width": 900, "height": 900},
    ]
    # rw=0.2, rh=0.1 -- narrower than the image/viewport aspect (k=1), so the
    # HEIGHT must expand to 0.2 to keep the request fully visible without
    # cropping (requirement 2), centered on the same point.
    result = await server._handle_set_viewport(
        {
            "region": {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.1},
        }
    )
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["aspect_adjusted"] is True
    assert payload["requested_region"] == {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.1}

    set_call = server.bridge.call.call_args_list[1]
    params = set_call.args[1]
    # center_x=0.4,center_y=0.45 -> zoom_x=-0.1, zoom_y=-0.05; required_h=0.2
    # -> scale=700/(1400*0.2)=2.5
    assert params["scale"] == pytest.approx(2.5)
    assert params["zoom_x"] == pytest.approx(-0.1)
    assert params["zoom_y"] == pytest.approx(-0.05)


@pytest.mark.asyncio
async def test_handle_set_viewport_scale_keeps_current_pan():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": dict(_VP_MAIN_BASE)},
        {
            "ok": True,
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "applied": {"zoom_x": -0.07, "zoom_y": 0.11, "scale": 2.0},
            "clamped": [],
            "pipe_ready": True,
            "waited_ms": 10,
        },
        {"main": {"region": {"x": 0.2, "y": 0.3, "w": 0.25, "h": 0.25}, "scale": 2.0}},
        {"path": "/tmp/probe.png", "width": 800, "height": 800},
    ]
    result = await server._handle_set_viewport({"scale": 2.0})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    set_call = server.bridge.call.call_args_list[1]
    params = set_call.args[1]
    # a plain scale change must NOT move the pan.
    assert params["zoom_x"] == pytest.approx(_VP_MAIN_BASE["zoom_x"])
    assert params["zoom_y"] == pytest.approx(_VP_MAIN_BASE["zoom_y"])
    assert params["scale"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_handle_set_viewport_mode_fit_centers_pan():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": dict(_VP_MAIN_BASE)},
        {
            "ok": True,
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "applied": {"zoom_x": 0.0, "zoom_y": 0.0, "scale": 0.5},
            "clamped": [],
            "pipe_ready": True,
            "waited_ms": 5,
        },
        {"main": {"region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}, "scale": 0.5}},
        {"path": "/tmp/probe.png", "width": 1000, "height": 700},
    ]
    result = await server._handle_set_viewport({"mode": "fit"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    set_call = server.bridge.call.call_args_list[1]
    params = set_call.args[1]
    # fit_scale = min(viewport_w/procw, viewport_h/proch) = min(0.5, 0.5) = 0.5
    assert params["scale"] == pytest.approx(0.5)
    assert params["zoom_x"] == pytest.approx(0.0)
    assert params["zoom_y"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_handle_set_viewport_clamped_scale_reported():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": dict(_VP_MAIN_BASE)},
        {
            "ok": True,
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "applied": {"zoom_x": -0.07, "zoom_y": 0.11, "scale": 16.0},
            "clamped": [
                {
                    "field": "scale",
                    "requested": 99.0,
                    "clamped_to": 16.0,
                    "floor": 0.35,
                    "ceiling": 16.0,
                },
            ],
            "pipe_ready": True,
            "waited_ms": 40,
        },
        {"main": {"region": {"x": 0.4, "y": 0.4, "w": 0.03, "h": 0.03}, "scale": 16.0}},
        {"path": "/tmp/probe.png", "width": 800, "height": 800},
    ]
    result = await server._handle_set_viewport({"scale": 99.0})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    # requirement 6: report the clamp using set_params' own clamped-list
    # convention (field/requested/applied/min/max), not the C-side's
    # internal clamped_to/floor/ceiling names.
    assert payload["clamped"] == [
        {"field": "scale", "requested": 99.0, "applied": 16.0, "min": 0.35, "max": 16.0},
    ]


@pytest.mark.asyncio
async def test_handle_set_viewport_pipe_not_ready_is_reported_not_swallowed():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": dict(_VP_MAIN_BASE)},
        {
            "ok": True,
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "applied": {"zoom_x": -0.07, "zoom_y": 0.11, "scale": 2.0},
            "clamped": [],
            "pipe_ready": False,
            "waited_ms": 4000,
        },
        {"main": {"region": {"x": 0.2, "y": 0.3, "w": 0.25, "h": 0.25}, "scale": 2.0}},
        {"path": "/tmp/probe.png", "width": 800, "height": 800},
    ]
    result = await server._handle_set_viewport({"scale": 2.0})
    payload = json.loads(result[0].text)
    # ok=True (the call itself succeeded) but pipe_ready must surface the
    # timeout truthfully -- this is requirement 3's core guard: never a false
    # "ok" hiding a stale-pipe timeout.
    assert payload["ok"] is True
    assert payload["pipe_ready"] is False
    assert payload["waited_ms"] == 4000


@pytest.mark.asyncio
async def test_handle_set_viewport_preview2_inactive_no_state_change():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "main": dict(_VP_MAIN_BASE),
        "preview2": {"active": False},
    }
    result = await server._handle_set_viewport({"viewport": "preview2", "scale": 1.0})
    assert "viewport_not_active" in result[0].text
    # acceptance test 5: no state change -- only the read-only dev_get_viewport
    # probe was ever called, dev_set_viewport must never be reached.
    assert server.bridge.call.call_count == 1
    server.bridge.call.assert_called_once_with("dev_get_viewport", {}, timeout=15.0)


@pytest.mark.asyncio
async def test_handle_set_viewport_requires_exactly_one_of_region_scale_mode():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_set_viewport({})
    assert "exactly one of" in result[0].text
    result2 = await server._handle_set_viewport(
        {"region": {"x": 0, "y": 0, "w": 1, "h": 1}, "scale": 1.0}
    )
    assert "exactly one of" in result2[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_set_viewport_rejects_out_of_bounds_region():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"main": dict(_VP_MAIN_BASE)}
    result = await server._handle_set_viewport({"region": {"x": 0.9, "y": 0.0, "w": 0.5, "h": 0.2}})
    assert "region must be within" in result[0].text
    # the out-of-bounds region is caught after reading current state (needed
    # to know if preview2 is even active) but strictly BEFORE ever calling
    # dev_set_viewport -- no zoom/pan write is attempted for invalid input.
    server.bridge.call.assert_called_once_with("dev_get_viewport", {}, timeout=15.0)


@pytest.mark.asyncio
async def test_handle_set_viewport_min_render_px_across_not_pursued_when_unmet():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": dict(_VP_MAIN_BASE)},
        {
            "ok": True,
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "applied": {"zoom_x": -0.1, "zoom_y": 0.0, "scale": 2.5},
            "clamped": [],
            "pipe_ready": True,
            "waited_ms": 20,
        },
        {"main": {"region": {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.2}, "scale": 2.5}},
        {"path": "/tmp/probe.png", "width": 200, "height": 200},
    ]
    result = await server._handle_set_viewport(
        {
            "region": {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.2},
            "min_render_px_across": 800,
        }
    )
    payload = json.loads(result[0].text)
    assert payload["min_render_px_across_satisfied"] is False
    assert "note" in payload
    # exactly 4 bridge calls -- no extra "zoom in further" attempt.
    assert server.bridge.call.call_count == 4
    # the probe itself must use viewport=, not region= -- same fix as
    # capture_viewport (2026-07-31): full.pipe's backbuf IS already the
    # achieved-region crop once zoomed, passing region again double-crops.
    probe_call = server.bridge.call.call_args_list[3]
    assert probe_call.args[1] == {"max_w": 8192, "max_h": 8192, "viewport": "main"}


@pytest.mark.asyncio
async def test_handle_set_viewport_min_render_px_across_satisfied():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": dict(_VP_MAIN_BASE)},
        {
            "ok": True,
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "applied": {"zoom_x": -0.1, "zoom_y": 0.0, "scale": 2.5},
            "clamped": [],
            "pipe_ready": True,
            "waited_ms": 20,
        },
        {"main": {"region": {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.2}, "scale": 2.5}},
        # 2026-07-31 fix: once capture_viewport reads dev->full.pipe's own
        # backbuf, a zoomed-in region genuinely CAN reach a high pixel
        # count (up to the darkroom window's own size) -- unlike before,
        # this is now achievable, not permanently defeatist.
        {"path": "/tmp/probe.png", "width": 1000, "height": 1000},
    ]
    result = await server._handle_set_viewport(
        {
            "region": {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.2},
            "min_render_px_across": 800,
        }
    )
    payload = json.loads(result[0].text)
    assert payload["min_render_px_across_satisfied"] is True
    assert "note" not in payload


@pytest.mark.asyncio
async def test_handle_restore_viewport_success():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "viewport": "main",
        "pipe_ready": True,
        "waited_ms": 12,
    }
    result = await server._handle_restore_viewport({"previous": _SET_VIEWPORT_PREVIOUS})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    server.bridge.call.assert_called_once_with(
        "dev_restore_viewport",
        {
            "viewport": "main",
            "previous": _SET_VIEWPORT_PREVIOUS,
            "wait_for_pipe": True,
            "timeout_ms": 4000,
        },
        timeout=20.0,
    )


@pytest.mark.asyncio
async def test_handle_restore_viewport_requires_previous():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_restore_viewport({})
    assert "previous" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_restore_viewport_preview2_inactive():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"error": "viewport_not_active: preview2 window is not open"}
    result = await server._handle_restore_viewport(
        {
            "viewport": "preview2",
            "previous": _SET_VIEWPORT_PREVIOUS,
        }
    )
    assert "viewport_not_active" in result[0].text


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_in_viewport_success():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "preview2",
            "region": {"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.2},
            "render": {"path": "/tmp/snap.png", "width": 1000, "height": 1000},
            "image": IMAGE_13406,
        }
    )
    # Two-stage pipeline: dev_backtransform_point(target), dev_backtransform_point
    # (source), then dev_retouch_add_shape. Identity backtransform here (no
    # orientation/crop/lens-correction active) so the final mask-space values
    # equal the display-frame ones computed by the viewport affine step.
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"x": 0.5, "y": 0.3, "len1": 0.02},
        {"x": 0.52, "y": 0.3},
        {"ok": True, "formid": 99, "algorithm": "heal", "wavelet_scale": 0},
    ]
    result = await server._handle_retouch_add_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "algorithm": "heal",
            "target": {"x": 0.5, "y": 0.5},
            "source": {"x": 0.6, "y": 0.5},
            "radius": 0.1,
            "return_preview": False,
        }
    )
    text = result[0].text
    assert "formid=99" in text
    assert server.bridge.call.call_count == 4
    call_args_list = server.bridge.call.call_args_list
    assert call_args_list[0][0][0] == "dev_current_image"
    assert call_args_list[1][0][0] == "dev_backtransform_point"
    assert call_args_list[2][0][0] == "dev_backtransform_point"
    assert call_args_list[3][0][0] == "dev_retouch_add_shape"
    params = call_args_list[3][0][1]
    # Target 0.5,0.5 within region {x:0.4,y:0.2,w:0.2,h:0.2}:
    # display = 0.4+0.5*0.2=0.5, 0.2+0.5*0.2=0.3.
    # -> identity backtransform -> mask-frame == display-frame here.
    assert params["target"]["x"] == pytest.approx(0.5)
    assert params["target"]["y"] == pytest.approx(0.3)
    # radius 0.1 (viewport_normalized) * region.w 0.2 = 0.02 (display), identity -> 0.02 (mask)
    assert params["radius"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_in_viewport_blur_skips_source_backtransform():
    """2026-07-31 blur/fill batch: blur/fill have no source point, so the
    source leg of the two-stage viewport pipeline (dev_backtransform_point
    for source) must be skipped entirely -- only 3 bridge calls total
    (current_image, backtransform target, retouch_add_shape), not 4."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"x": 0.4, "y": 0.6, "len1": 0.05},
        {
            "ok": True,
            "formid": 55,
            "algorithm": "blur",
            "wavelet_scale": 3,
            "blur_type": "gaussian",
            "blur_radius": 8.0,
        },
    ]
    result = await server._handle_retouch_add_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "algorithm": "blur",
            "target": {"x": 0.4, "y": 0.6},
            "radius": 0.05,
            "wavelet_scale": 3,
            "blur_type": "gaussian",
            "blur_radius": 8.0,
            "return_preview": False,
        }
    )
    assert "blur_type=gaussian" in result[0].text
    assert server.bridge.call.call_count == 3
    call_args_list = server.bridge.call.call_args_list
    assert call_args_list[0][0][0] == "dev_current_image"
    assert call_args_list[1][0][0] == "dev_backtransform_point"
    assert call_args_list[2][0][0] == "dev_retouch_add_shape"
    params = call_args_list[2][0][1]
    assert "source" not in params
    assert params["blur_type"] == "gaussian"
    assert params["blur_radius"] == 8.0


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_in_viewport_path_backtransforms_each_node():
    """2026-07-31 path viewport step: a path has no single target/radius --
    each node goes through its own viewport-local -> display -> mask-frame
    backtransform (reusing _backtransform_polygon_to_mask_space), plus one
    extra call for source (heal/clone)."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"x": 0.11, "y": 0.11},
        {"x": 0.21, "y": 0.11},
        {"x": 0.16, "y": 0.21},
        {"x": 0.6, "y": 0.6},
        {"ok": True, "formid": 66, "algorithm": "heal", "shape_type": "path", "points": 3},
    ]
    result = await server._handle_retouch_add_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "algorithm": "heal",
            "shape_type": "path",
            "points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.1}, {"x": 0.15, "y": 0.2}],
            "source": {"x": 0.6, "y": 0.6},
            "return_preview": False,
        }
    )
    assert "shape_type=path" in result[0].text
    assert server.bridge.call.call_count == 6
    call_args_list = server.bridge.call.call_args_list
    assert call_args_list[0][0][0] == "dev_current_image"
    assert all(c[0][0] == "dev_backtransform_point" for c in call_args_list[1:5])
    assert call_args_list[5][0][0] == "dev_retouch_add_shape"
    params = call_args_list[5][0][1]
    assert params["points"] == [
        {"x": 0.11, "y": 0.11},
        {"x": 0.21, "y": 0.11},
        {"x": 0.16, "y": 0.21},
    ]
    assert params["source"] == {"x": 0.6, "y": 0.6}
    assert "target" not in params
    assert "radius" not in params


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_in_viewport_ellipse_backtransforms_radius_b():
    """2026-07-31 ellipse viewport step: radius_b needs its OWN
    backtransform_point call (its own len1 slot), separate from the main
    target+radius+feather call."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"x": 0.5, "y": 0.4, "len1": 0.06},
        {"x": 0.55, "y": 0.4},
        {"x": 0.5, "y": 0.4, "len1": 0.02},
        {
            "ok": True,
            "formid": 77,
            "algorithm": "heal",
            "shape_type": "ellipse",
            "radius_b": 0.02,
            "rotation": 30.0,
        },
    ]
    result = await server._handle_retouch_add_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "algorithm": "heal",
            "shape_type": "ellipse",
            "target": {"x": 0.5, "y": 0.4},
            "source": {"x": 0.55, "y": 0.4},
            "radius": 0.06,
            "radius_b": 0.02,
            "rotation": 30,
            "return_preview": False,
        }
    )
    assert "shape_type=ellipse" in result[0].text
    call_args_list = server.bridge.call.call_args_list
    assert call_args_list[0][0][0] == "dev_current_image"
    assert call_args_list[1][0][0] == "dev_backtransform_point"  # target+radius+feather
    assert call_args_list[2][0][0] == "dev_backtransform_point"  # source
    assert call_args_list[3][0][0] == "dev_backtransform_point"  # radius_b (its own call)
    assert call_args_list[4][0][0] == "dev_retouch_add_shape"
    params = call_args_list[4][0][1]
    assert params["radius_b"] == pytest.approx(0.02)
    assert params["rotation"] == 30.0


@pytest.mark.asyncio
async def test_handle_retouch_update_shape_in_viewport_path_replaces_points():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"x": 0.51, "y": 0.41},
        {"x": 0.56, "y": 0.41},
        {"x": 0.53, "y": 0.46},
        {"ok": True, "formid": 66, "shape_type": "path", "points": 3},
    ]
    result = await server._handle_retouch_update_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "formid": 66,
            "points": [{"x": 0.5, "y": 0.4}, {"x": 0.55, "y": 0.4}, {"x": 0.52, "y": 0.45}],
            "return_preview": False,
        }
    )
    assert "shape_type=path" in result[0].text
    call_args_list = server.bridge.call.call_args_list
    assert call_args_list[0][0][0] == "dev_current_image"
    assert all(c[0][0] == "dev_backtransform_point" for c in call_args_list[1:4])
    assert call_args_list[4][0][0] == "dev_retouch_update_shape"
    params = call_args_list[4][0][1]
    assert params["points"] == [
        {"x": 0.51, "y": 0.41},
        {"x": 0.56, "y": 0.41},
        {"x": 0.53, "y": 0.46},
    ]
    assert "target" not in params
    assert "radius" not in params


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_in_viewport_uses_backtransformed_coords():
    """Guards against silently regressing to the pre-fix (2026-07-25) bug:
    a caller-supplied point must be converted through dev_backtransform_point
    (pipe-input/mask frame), NOT written to retouch_add_shape using the raw
    display-frame value. A non-identity mock (simulating a portrait-orientation
    axis swap) proves the handler actually uses the backtransform's output."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"x": 0.9, "y": 0.1, "len1": 0.4},  # target: deliberately NOT equal to display-frame input
        {"x": 0.8, "y": 0.2},  # source: deliberately NOT equal to display-frame input
        {"ok": True, "formid": 7, "algorithm": "heal", "wavelet_scale": 0},
    ]
    await server._handle_retouch_add_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "algorithm": "heal",
            "target": {"x": 0.5, "y": 0.5},
            "source": {"x": 0.6, "y": 0.5},
            "radius": 0.1,
            "return_preview": False,
        }
    )
    params = server.bridge.call.call_args_list[3][0][1]
    assert params["target"] == {"x": 0.9, "y": 0.1}
    assert params["source"] == {"x": 0.8, "y": 0.2}
    assert params["radius"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_in_viewport_rejects_out_of_bounds():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    with pytest.raises(ViewportCoordinateError):
        await server._handle_retouch_add_shape_in_viewport(
            {
                "snapshot_id": snapshot_id,
                "algorithm": "heal",
                "target": {"x": 1.5, "y": 0.5},
                "source": {"x": 0.6, "y": 0.5},
                "radius": 0.05,
            }
        )
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retouch_add_shape_in_viewport_rejects_unknown_snapshot():
    server = DarktableMCPServer()
    server.bridge = Mock()
    with pytest.raises(ViewportCoordinateError):
        await server._handle_retouch_add_shape_in_viewport(
            {
                "snapshot_id": "vp_doesnotexist",
                "algorithm": "heal",
                "target": {"x": 0.5, "y": 0.5},
                "source": {"x": 0.6, "y": 0.5},
                "radius": 0.05,
            }
        )


@pytest.mark.asyncio
async def test_handle_retouch_update_shape_in_viewport_success():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"x": 0.5, "y": 0.5, "len1": 0.05},
        {"x": 0.6, "y": 0.5},
        {"ok": True, "algorithm": "heal", "wavelet_scale": 0, "opacity": 1.0},
    ]
    result = await server._handle_retouch_update_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "formid": 42,
            "target": {"x": 0.5, "y": 0.5},
            "source": {"x": 0.6, "y": 0.5},
            "radius": 0.05,
            "return_preview": False,
        }
    )
    assert "formid=42" in result[0].text
    assert server.bridge.call.call_count == 4
    call_args_list = server.bridge.call.call_args_list
    assert call_args_list[0][0][0] == "dev_current_image"
    assert call_args_list[1][0][0] == "dev_backtransform_point"
    assert call_args_list[2][0][0] == "dev_backtransform_point"
    assert call_args_list[3] == call(
        "dev_retouch_update_shape",
        {
            "op": "retouch",
            "instance": 0,
            "formid": 42,
            "target": {"x": 0.5, "y": 0.5},
            "source": {"x": 0.6, "y": 0.5},
            "radius": 0.05,
            "feather": 0.0,
        },
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_retouch_delete_shapes_batch():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"ok": True},  # delete formid 1 -> success
        {"error": "not found"},  # delete formid 2 -> error
        {"shapes": [{"formid": 2}]},  # re-check: formid 2 STILL listed -> genuinely failed
        {"ok": True},  # delete formid 3 -> success
    ]
    result = await server._handle_retouch_delete_shapes({"formids": [1, 2, 3]})
    text = result[0].text
    assert "deleted=[1, 3]" in text
    assert "failed=1" in text
    assert server.bridge.call.call_count == 4


@pytest.mark.asyncio
async def test_handle_retouch_delete_shapes_batch_idempotent_after_group_teardown():
    """Bugreport (2026-07-25): deleting the group's last remaining shape can
    tear down the shared mask group (mask_id cleared), so a LATER formid in
    the SAME batch errors "no shapes group" even though it's already gone
    too. This must be reported as deleted (idempotent), not a real failure --
    verified via a retouch_list_shapes re-check that confirms the formid is
    genuinely absent."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"ok": True},  # delete formid 1 -> success (tears down the group)
        {"error": "module has no shapes group"},  # delete formid 2 -> error (group now gone)
        {"shapes": []},  # re-check: formid 2 NOT listed -> already gone
    ]
    result = await server._handle_retouch_delete_shapes({"formids": [1, 2]})
    text = result[0].text
    assert "deleted=[1, 2]" in text
    assert "failed=0" in text
    assert server.bridge.call.call_count == 3


@pytest.mark.asyncio
async def test_handle_retouch_delete_shapes_requires_formids():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_retouch_delete_shapes({})
    assert "formids" in result[0].text
    server.bridge.call.assert_not_called()


COLLECTION_FIXTURE = [
    {
        "id": "1",
        "filename": "a.NEF",
        "path": "/photos/a.NEF",
        "rating": 0,
        "capture_time": "",
        "selected": False,
    },
    {
        "id": "2",
        "filename": "b.NEF",
        "path": "/photos/b.NEF",
        "rating": 0,
        "capture_time": "",
        "selected": False,
    },
    {
        "id": "3",
        "filename": "c.NEF",
        "path": "/photos/c.NEF",
        "rating": 0,
        "capture_time": "",
        "selected": False,
    },
]


def _bridge_router(responses):
    def _call(method, params=None, timeout=None):
        return responses[method]

    bridge = Mock()
    bridge.call = Mock(side_effect=_call)
    return bridge


@pytest.mark.asyncio
async def test_navigate_photo_next_opens_neighbor():
    server = DarktableMCPServer()
    server.bridge = _bridge_router(
        {
            "dev_current_image": {"has_image": True, "id": 1, "path": "/photos/a.NEF"},
            "get_collection_images": COLLECTION_FIXTURE,
            "open_darkroom": {"view": "darkroom", "path": "/photos/b.NEF"},
        }
    )
    result = await server._handle_navigate_photo({"direction": "next"})
    text = result[0].text
    assert "Opened image 2 in darkroom" in text
    assert "next photo: 2/3" in text
    assert "b.NEF" in text


@pytest.mark.asyncio
async def test_navigate_photo_previous_opens_neighbor():
    server = DarktableMCPServer()
    server.bridge = _bridge_router(
        {
            "dev_current_image": {"has_image": True, "id": 2, "path": "/photos/b.NEF"},
            "get_collection_images": COLLECTION_FIXTURE,
            "open_darkroom": {"view": "darkroom", "path": "/photos/a.NEF"},
        }
    )
    result = await server._handle_navigate_photo({"direction": "previous"})
    assert "Opened image 1 in darkroom" in result[0].text


@pytest.mark.asyncio
async def test_navigate_photo_stops_at_start():
    server = DarktableMCPServer()
    server.bridge = _bridge_router(
        {
            "dev_current_image": {"has_image": True, "id": 1, "path": "/photos/a.NEF"},
            "get_collection_images": COLLECTION_FIXTURE,
        }
    )
    result = await server._handle_navigate_photo({"direction": "previous"})
    assert "Already at the first photo" in result[0].text


@pytest.mark.asyncio
async def test_navigate_photo_stops_at_end():
    server = DarktableMCPServer()
    server.bridge = _bridge_router(
        {
            "dev_current_image": {"has_image": True, "id": 3, "path": "/photos/c.NEF"},
            "get_collection_images": COLLECTION_FIXTURE,
        }
    )
    result = await server._handle_navigate_photo({"direction": "next"})
    assert "Already at the last photo" in result[0].text


@pytest.mark.asyncio
async def test_navigate_photo_no_image_open():
    server = DarktableMCPServer()
    server.bridge = _bridge_router({"dev_current_image": {"has_image": False}})
    result = await server._handle_navigate_photo({"direction": "next"})
    assert "No image open in darkroom" in result[0].text


@pytest.mark.asyncio
async def test_navigate_photo_current_not_in_collection():
    server = DarktableMCPServer()
    server.bridge = _bridge_router(
        {
            "dev_current_image": {"has_image": True, "id": 999, "path": "/photos/z.NEF"},
            "get_collection_images": COLLECTION_FIXTURE,
        }
    )
    result = await server._handle_navigate_photo({"direction": "next"})
    assert "not in the currently open collection" in result[0].text


@pytest.mark.asyncio
async def test_navigate_photo_rejects_bad_direction():
    server = DarktableMCPServer()
    result = await server._handle_navigate_photo({"direction": "sideways"})
    assert "direction must be" in result[0].text


# ---- 2026-07-26 bugreport: snapshot/darkroom image binding ------------------


@pytest.mark.asyncio
async def test_retouch_add_shape_in_viewport_refuses_on_image_mismatch():
    """Core of the 2026-07-26 bugreport: every darkroom binding resolves
    against darktable's GLOBAL current image, so a snapshot taken on one photo
    would happily retouch whatever photo is open by the time the call lands.
    The write must be refused, not translated onto the wrong image."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        {"has_image": True, "id": 99999, "filename": "other.ARW"},
    ]
    result = await server._handle_retouch_add_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "algorithm": "heal",
            "target": {"x": 0.5, "y": 0.5},
            "source": {"x": 0.6, "y": 0.5},
            "radius": 0.1,
            "return_preview": False,
        }
    )
    text = result[0].text
    assert "image mismatch" in text
    assert "13406" in text and "99999" in text
    # Only the identity probe ran -- nothing was written.
    assert server.bridge.call.call_count == 1
    assert server.bridge.call.call_args_list[0][0][0] == "dev_current_image"


@pytest.mark.asyncio
async def test_retouch_update_shape_in_viewport_refuses_when_darkroom_closed():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [{"has_image": False, "id": -1}]
    result = await server._handle_retouch_update_shape_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "formid": 42,
            "target": {"x": 0.5, "y": 0.5},
            "source": {"x": 0.6, "y": 0.5},
            "radius": 0.05,
            "return_preview": False,
        }
    )
    assert "image mismatch" in result[0].text
    assert server.bridge.call.call_count == 1


@pytest.mark.asyncio
async def test_capture_viewport_aborts_when_image_changes_mid_capture():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"main": {"region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}}},
        IMAGE_13406,
        {"path": "/tmp/preview.png", "width": 100, "height": 100},
        {"has_image": True, "id": 99999, "filename": "other.ARW"},
    ]
    result = await server._handle_capture_viewport({"return_image": False})
    assert "changed while capturing" in result[0].text
    assert not server._viewport_snapshots


@pytest.mark.asyncio
async def test_render_snapshot_preview_flags_wrong_image():
    """The bugreport's most alarming symptom: a post-edit preview showing a
    completely different photo. dev_preview carries no image identity, so the
    server has to attach one and shout when it does not match."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"path": "/tmp/after.png", "width": 100, "height": 100},
        {"has_image": True, "id": 99999, "filename": "other.ARW"},
    ]
    _, line = server._render_snapshot_preview(
        {
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 100, "height": 100},
            "image": IMAGE_13406,
        }
    )
    assert "WARNING" in line
    assert "99999" in line and "13406" in line


@pytest.mark.asyncio
async def test_snapshot_pixels_alias_matches_viewport_pixels():
    """snapshot_pixels is the clearer name for the same grid (pixels of the
    capture_viewport render); both spellings must convert identically."""
    server = DarktableMCPServer()
    written = []
    for space in ("viewport_pixels", "snapshot_pixels"):
        server.bridge = Mock()
        snapshot_id = server._store_viewport_snapshot(
            {
                "viewport": "main",
                "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
                "render": {"path": "/tmp/snap.png", "width": 400, "height": 200},
                "image": IMAGE_13406,
            }
        )
        server.bridge.call.side_effect = [
            IMAGE_13406,
            {"x": 0.25, "y": 0.5, "len1": 0.05},
            {"x": 0.5, "y": 0.5},
            {"ok": True, "formid": 5, "algorithm": "heal", "wavelet_scale": 0},
        ]
        await server._handle_retouch_add_shape_in_viewport(
            {
                "snapshot_id": snapshot_id,
                "algorithm": "heal",
                "coordinate_space": space,
                "target": {"x": 100, "y": 100},
                "source": {"x": 200, "y": 100},
                "radius": 20,
                "return_preview": False,
            }
        )
        # the display-frame point handed to the backtransform is what matters
        written.append(server.bridge.call.call_args_list[1][0][1])
    assert written[0] == written[1]


@pytest.mark.asyncio
async def test_retouch_delete_shape_error_reports_darkroom_state():
    """ "no darkroom image loaded" only says dev->iop was NULL at that instant.
    Naming the image that IS open turns the bugreport's unexplained failure
    into a diagnosis."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {"error": "no darkroom image loaded"},
        {"has_image": True, "id": 99999, "filename": "other.ARW"},
    ]
    result = await server._handle_retouch_delete_shape({"formid": 42})
    text = result[0].text
    assert "no darkroom image loaded" in text
    assert "darkroom state right now" in text
    assert "99999" in text


# ---- retouch_render_overlay (2026-07-26) -----------------------------------
#
# The overlay exists because darktable's own mask overlay is cairo-painted onto
# the darkroom GUI widget (masks.c's dt_masks_events_post_expose), so it is
# absent from the pixelpipe backbuf capture_viewport encodes. These tests pin
# the two things that can silently lie: the display-frame -> render-pixel
# placement math, and the refusal to draw one image's shapes on another's
# snapshot.

SHAPE_42 = {
    "formid": 42,
    "algorithm": "heal",
    "shape_type": "circle",
    "target": {"x": 0.4, "y": 0.3},
    "source": {"x": 0.35, "y": 0.3},
    "radius": 0.02,
    "feather": 0.01,
    "opacity": 1.0,
    "wavelet_scale": 0,
    # display-frame values (dt.develop.transform_point); deliberately DIFFERENT
    # from the mask-frame ones above so a handler that plots the wrong field is
    # caught by the pixel assertions below.
    "target_display": {"x": 0.5, "y": 0.5},
    "source_display": {"x": 0.6, "y": 0.5},
    "radius_display": 0.05,
    "feather_display": 0.025,
}


def _write_test_render(path, width=400, height=200):
    from PIL import Image

    Image.new("RGB", (width, height), (30, 30, 30)).save(path, "PNG")
    return str(path)


def _overlay_server(tmp_path, shapes, region=None, width=400, height=200):
    server = DarktableMCPServer()
    server.bridge = Mock()
    render_path = _write_test_render(tmp_path / "snap.png", width, height)
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": region or {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": render_path, "width": width, "height": height},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"module": "retouch", "instance": 0, "has_display_frame": True, "shapes": shapes},
    ]
    return server, snapshot_id


@pytest.mark.asyncio
async def test_retouch_render_overlay_places_shapes_from_display_frame(tmp_path):
    server, snapshot_id = _overlay_server(tmp_path, [SHAPE_42])
    result = await server._handle_retouch_render_overlay(
        {
            "snapshot_id": snapshot_id,
            "return_image": False,
        }
    )
    text = result[-1].text
    assert "formid=42" in text
    # full-frame region, 400x200 render: display 0.5,0.5 -> 200,100 px;
    # radius 0.05 normalized against display WIDTH -> 0.05*400 = 20px.
    assert "target_px=[200.0, 100.0]" in text
    assert "source_px=[240.0, 100.0]" in text
    assert "radius_px=20.0" in text
    assert "feather_px=10.0" in text
    assert "source_target_distance_px=40.0" in text
    out = text.split("\n")[0].split(": ")[-1]
    assert os.path.isfile(out)


@pytest.mark.asyncio
async def test_retouch_render_overlay_honours_region_offset(tmp_path):
    """A zoomed viewport (region 0.4..0.6) must place a shape at display 0.5 in
    the MIDDLE of the render, not at 0.5 of it -- the same region math the
    write path uses, inverted."""
    server, snapshot_id = _overlay_server(
        tmp_path, [SHAPE_42], region={"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}
    )
    result = await server._handle_retouch_render_overlay(
        {
            "snapshot_id": snapshot_id,
            "return_image": False,
        }
    )
    text = result[-1].text
    # local = (0.5-0.4)/0.2 = 0.5 -> 200,100 px; radius 0.05/0.2 * 400 = 100px
    assert "target_px=[200.0, 100.0]" in text
    assert "radius_px=100.0" in text


@pytest.mark.asyncio
async def test_retouch_render_overlay_refuses_on_image_mismatch(tmp_path):
    server, snapshot_id = _overlay_server(tmp_path, [SHAPE_42])
    server.bridge.call.side_effect = [
        {"has_image": True, "id": 99999, "filename": "other.ARW"},
    ]
    result = await server._handle_retouch_render_overlay({"snapshot_id": snapshot_id})
    text = result[0].text
    assert "image mismatch" in text
    assert "13406" in text and "99999" in text
    # never reached the shape read -- nothing was drawn
    assert server.bridge.call.call_count == 1


@pytest.mark.asyncio
async def test_retouch_render_overlay_selected_mode_needs_formid(tmp_path):
    second = dict(SHAPE_42, formid=43, target_display={"x": 0.2, "y": 0.2})
    server, snapshot_id = _overlay_server(tmp_path, [SHAPE_42, second])
    result = await server._handle_retouch_render_overlay(
        {
            "snapshot_id": snapshot_id,
            "mode": "selected_shape",
            "return_image": False,
        }
    )
    assert "needs highlight_formid" in result[0].text


@pytest.mark.asyncio
async def test_retouch_render_overlay_reports_overlaps(tmp_path):
    """Two heal circles reaching into each other is a real retouch mistake (the
    second heal samples the first one's output), and far easier to report than
    to spot in the render."""
    near = dict(SHAPE_42, formid=43, target_display={"x": 0.55, "y": 0.5})
    server, snapshot_id = _overlay_server(tmp_path, [SHAPE_42, near])
    result = await server._handle_retouch_render_overlay(
        {
            "snapshot_id": snapshot_id,
            "return_image": False,
        }
    )
    text = result[-1].text
    assert "OVERLAPPING" in text
    assert "[42, 43]" in text


@pytest.mark.asyncio
async def test_retouch_render_overlay_mask_only_is_greyscale_alpha(tmp_path):
    from PIL import Image

    server, snapshot_id = _overlay_server(tmp_path, [SHAPE_42])
    result = await server._handle_retouch_render_overlay(
        {
            "snapshot_id": snapshot_id,
            "mode": "mask_only",
            "return_image": False,
        }
    )
    text = result[-1].text
    out = text.split("\n")[0].split(": ")[-1]
    img = Image.open(out)
    assert img.mode == "L"
    # inside the radius: fully masked; well outside radius+feather: nothing.
    assert img.getpixel((200, 100)) == 255
    assert img.getpixel((399, 199)) == 0


@pytest.mark.asyncio
async def test_retouch_render_overlay_skips_shapes_without_display_geometry(tmp_path):
    """A shape listed before any processed pipe existed (no *_display fields)
    must be reported as not drawable instead of being plotted from mask-frame
    numbers, which would put it in the wrong place."""
    bare = {
        "formid": 44,
        "algorithm": "clone",
        "shape_type": "circle",
        "target": {"x": 0.4, "y": 0.3},
        "radius": 0.02,
    }
    server, snapshot_id = _overlay_server(tmp_path, [SHAPE_42, bare])
    result = await server._handle_retouch_render_overlay(
        {
            "snapshot_id": snapshot_id,
            "return_image": False,
        }
    )
    text = result[-1].text
    assert "not drawable: [44]" in text
    assert "formid=42" in text


@pytest.mark.asyncio
async def test_retouch_list_shapes_reports_both_frames():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "module": "retouch",
        "instance": 0,
        "num_scales": 4,
        "curr_scale": 1,
        "merge_from_scale": 0,
        "has_display_frame": True,
        "shapes": [SHAPE_42],
    }
    result = await server._handle_retouch_list_shapes({})
    text = result[0].text
    assert "opacity=1.0" in text
    assert "display frame: target=(0.5,0.5)" in text
    assert "retouch_render_overlay" in text


@pytest.mark.asyncio
async def test_retouch_list_shapes_display_frame_unavailable_hint_covers_staleness():
    """2026-07-31 mask-image-size-race investigation: has_display_frame=False now
    also fires when dt.develop.retouch_list_shapes' C-side freshness check
    (_mask_pipe_is_fresh) rejects a mid-rebuild preview pipe, not just a
    never-rendered one. The C side omits *_display fields either way (same
    field, same boolean) -- this pins that the Python-facing hint text
    reflects BOTH causes so a caller doesn't misread a timing race as
    "image never opened"."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "module": "retouch",
        "instance": 0,
        "num_scales": 4,
        "curr_scale": 1,
        "merge_from_scale": 0,
        "has_display_frame": False,
        "shapes": [
            {
                "formid": 42,
                "algorithm": "heal",
                "shape_type": "circle",
                "target": {"x": 0.4, "y": 0.3},
                "source": {"x": 0.35, "y": 0.3},
                "radius": 0.02,
                "feather": 0.1,
                "wavelet_scale": 2,
            },
        ],
    }
    result = await server._handle_retouch_list_shapes({})
    text = result[0].text
    assert "display-frame values unavailable" in text
    assert "mid-refresh" in text
    assert "call get_preview once and list again" in text


# ---- list_luts / preview_lut / compare_luts (2026-07-26) -------------------

LUT3D_SNAPSHOT_FIELDS = {
    "filepath": "",
    "lutname": "",
    "colorspace": {
        "value": 0,
        "label": "DT_IOP_SRGB",
        "options": ["DT_IOP_SRGB", "DT_IOP_LIN_REC709"],
    },
    "interpolation": {
        "value": 0,
        "label": "DT_IOP_TETRAHEDRAL",
        "options": ["DT_IOP_TETRAHEDRAL", "DT_IOP_TRILINEAR"],
    },
}


def _lut3d_bridge_router(
    root_dir, preview_path="/run/cache-mcp/preview.png", blend_mask_mode=0, blend_opacity=100.0
):
    """A bridge.call side_effect that plays the real sequence
    _lut3d_preview_once drives: active_modules -> get_params -> [get_blend_params]
    -> set_params (apply) -> [set_blend_params] -> preview -> set_params
    (restore) -> [set_blend_params restore]. Routes purely on method name +
    fields content since dev_set_params/dev_set_blend_params are each called
    twice with different args but the same method name.

    blend_mask_mode/blend_opacity seed the module's PRIOR blend state (as
    dev_get_blend_params would report it) -- tests use this to simulate an
    already-masked instance (mask_mode not in (0,1), the opacity refusal
    case) or a plain never-blended one (the default, mask_mode=0)."""
    state = {"mask_mode": blend_mask_mode, "opacity": blend_opacity}

    def _router(method, params=None, timeout=None):
        params = params or {}
        if method == "dev_get_conf_string":
            return root_dir
        if method == "dev_active_modules":
            return {"modules": [{"op": "lut3d", "instance": 0, "enabled": False}]}
        if method == "dev_get_params":
            return {"op": "lut3d", "instance": 0, "fields": dict(LUT3D_SNAPSHOT_FIELDS)}
        if method == "dev_set_params":
            fields = params.get("fields", {})
            return {"ok": True, "applied": fields, "clamped": [], "unknown_fields": []}
        if method == "dev_get_blend_params":
            return {
                "op": "lut3d",
                "instance": 0,
                "opacity": state["opacity"],
                "mask_mode": state["mask_mode"],
                "blend_mode": 3,
            }
        if method == "dev_set_blend_params":
            fields = params.get("fields", {})
            if "opacity" in fields:
                state["opacity"] = fields["opacity"]
            if "enable_uniform_blend" in fields:
                state["mask_mode"] = 1 if fields["enable_uniform_blend"] else 0
            return {
                "ok": True,
                "op": "lut3d",
                "instance": 0,
                "opacity": state["opacity"],
                "mask_mode": state["mask_mode"],
                "blend_mode": 3,
            }
        if method == "dev_preview":
            return {"status": "ok", "path": preview_path, "width": 100, "height": 75}
        raise AssertionError(f"unexpected bridge call: {method}")

    return _router


@pytest.mark.asyncio
async def test_handle_list_luts_lists_files(tmp_path):
    (tmp_path / "FG").mkdir()
    (tmp_path / "FG" / "FGCineBasic.cube").write_text("")
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = str(tmp_path)
    result = await server._handle_list_luts({})
    text = result[0].text
    assert "FGCineBasic" in text
    assert "FG/FGCineBasic.cube" in text
    server.bridge.call.assert_called_once_with(
        "dev_get_conf_string",
        {"key": "plugins/darkroom/lut3d/def_path"},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_list_luts_not_configured():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = ""
    result = await server._handle_list_luts({})
    assert "def_path is not set" in result[0].text


@pytest.mark.asyncio
async def test_handle_preview_lut_applies_and_restores(tmp_path):
    (tmp_path / "FG").mkdir()
    (tmp_path / "FG" / "a.cube").write_text("")
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = _lut3d_bridge_router(str(tmp_path))

    with (
        patch("darktable_mcp.server._remap_bridge_path", side_effect=lambda p: p),
        patch("darktable_mcp.server._inline_image_content", return_value=None),
    ):
        result = await server._handle_preview_lut({"path": "FG/a.cube"})

    text = result[-1].text
    assert "ok=True" in text
    assert "restored" in text

    calls = server.bridge.call.call_args_list
    apply_call = next(
        c
        for c in calls
        if c.args[0] == "dev_set_params" and c.args[1]["fields"].get("filepath") == "FG/a.cube"
    )
    assert apply_call.args[1]["fields"]["enabled"] is True
    restore_call = calls[-1]
    assert restore_call.args[0] == "dev_set_params"
    # Restored to the snapshot taken before applying -- empty filepath,
    # disabled (the router's dev_active_modules said enabled=False).
    assert restore_call.args[1]["fields"]["filepath"] == ""
    assert restore_call.args[1]["fields"]["enabled"] is False


@pytest.mark.asyncio
async def test_handle_preview_lut_missing_file_never_touches_lut3d(tmp_path):
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = _lut3d_bridge_router(str(tmp_path))
    result = await server._handle_preview_lut({"path": "FG/missing.cube"})
    text = result[0].text
    assert "does not exist" in text
    # Only the conf-string lookup should have happened -- no set_params, no
    # active_modules, nothing applied to a nonexistent file.
    methods_called = [c.args[0] for c in server.bridge.call.call_args_list]
    assert methods_called == ["dev_get_conf_string"]


@pytest.mark.asyncio
async def test_handle_preview_lut_requires_path():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_preview_lut({})
    assert "path is required" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_compare_luts_mixed_success_and_missing_file(tmp_path):
    (tmp_path / "a.cube").write_text("")
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = _lut3d_bridge_router(str(tmp_path))

    with (
        patch("darktable_mcp.server._remap_bridge_path", side_effect=lambda p: p),
        patch("darktable_mcp.server._inline_image_content", return_value=None),
    ):
        result = await server._handle_compare_luts({"paths": ["a.cube", "missing.cube"]})

    response = json.loads(result[-1].text)
    assert response["requested"] == 2
    assert response["succeeded"] == 1
    assert response["failed"] == 1
    items_by_path = {item["path"]: item for item in response["items"]}
    assert items_by_path["a.cube"]["ok"] is True
    assert items_by_path["missing.cube"]["ok"] is False
    assert "does not exist" in items_by_path["missing.cube"]["error"]


@pytest.mark.asyncio
async def test_handle_compare_luts_requires_nonempty_paths():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_compare_luts({"paths": []})
    assert "non-empty" in result[0].text


# ---- get_blend_params / set_blend_params (2026-07-26) ----------------------


@pytest.mark.asyncio
async def test_handle_get_blend_params_returns_result():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "op": "lut3d",
        "instance": 0,
        "opacity": 100.0,
        "mask_mode": 0,
        "blend_mode": 3,
    }
    result = await server._handle_get_blend_params({"op": "lut3d"})
    assert "opacity" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_get_blend_params",
        {"op": "lut3d", "instance": 0},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_get_blend_params_requires_op():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_get_blend_params({})
    assert "op is required" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_set_blend_params_forwards_fields():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "op": "lut3d",
        "instance": 0,
        "opacity": 35.0,
        "mask_mode": 1,
        "blend_mode": 3,
    }
    result = await server._handle_set_blend_params(
        {
            "op": "lut3d",
            "opacity": 0.35,
            "enable_uniform_blend": True,
        }
    )
    assert "ok=True" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_set_blend_params",
        {"op": "lut3d", "instance": 0, "fields": {"opacity": 0.35, "enable_uniform_blend": True}},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_set_blend_params_requires_at_least_one_field():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_set_blend_params({"op": "lut3d"})
    assert "at least one of" in result[0].text
    server.bridge.call.assert_not_called()


# ---- add_instance fields={} (2026-07-27) -----------------------------------


@pytest.mark.asyncio
async def test_handle_add_instance_without_fields_omits_fields_from_call():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "op": "exposure",
        "instance": 1,
        "base_instance": 0,
        "multi_name": "",
    }
    result = await server._handle_add_instance({"op": "exposure"})
    assert "new instance=1" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_add_instance",
        {"op": "exposure"},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_add_instance_forwards_fields_and_reports_applied():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "op": "exposure",
        "instance": 1,
        "base_instance": 0,
        "multi_name": "",
        "fields_applied": {
            "ok": True,
            "applied": {"compensate_exposure_bias": False, "compensate_hilite_pres": False},
            "clamped": [],
            "unknown_fields": [],
        },
    }
    result = await server._handle_add_instance(
        {
            "op": "exposure",
            "fields": {"compensate_exposure_bias": False, "compensate_hilite_pres": False},
        }
    )
    server.bridge.call.assert_called_once_with(
        "dev_add_instance",
        {
            "op": "exposure",
            "fields": {"compensate_exposure_bias": False, "compensate_hilite_pres": False},
        },
        timeout=15.0,
    )
    assert "fields applied" in result[0].text
    assert "compensate_hilite_pres" in result[0].text


@pytest.mark.asyncio
async def test_handle_add_instance_rejects_non_dict_fields():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_add_instance({"op": "exposure", "fields": "nope"})
    assert "must be an object" in result[0].text
    server.bridge.call.assert_not_called()


# ---- mask group management: list_masks/get_module_mask/attach_mask/
# detach_mask/set_module_mask (2026-07-27) -----------------------------------


@pytest.mark.asyncio
async def test_handle_list_masks_wraps_bare_array():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = [
        {
            "formid": 42,
            "name": "circle 1",
            "type": "circle",
            "used_by": [{"op": "retouch", "instance": 0}],
        },
    ]
    result = await server._handle_list_masks({})
    response = json.loads(result[0].text)
    assert response["masks"][0]["formid"] == 42
    server.bridge.call.assert_called_once_with("dev_list_all_masks", {}, timeout=15.0)


@pytest.mark.asyncio
async def test_handle_list_masks_empty_result():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = []
    result = await server._handle_list_masks({})
    response = json.loads(result[0].text)
    assert response["masks"] == []


@pytest.mark.asyncio
async def test_handle_get_module_mask_merges_blend_params_and_shapes():
    server = DarktableMCPServer()
    server.bridge = Mock()

    def router(method, params, timeout=None):
        if method == "dev_get_blend_params":
            return {
                "op": "exposure",
                "instance": 1,
                "opacity": 100.0,
                "mask_mode": 2,
                "blend_mode": 3,
                "invert": False,
            }
        if method == "dev_list_masks":
            return [
                {
                    "mask_id": 42,
                    "type": "circle",
                    "opacity": 1.0,
                    "nb_points": 1,
                    "name": "circle 1",
                    "module": "exposure",
                    "instance": 1,
                    "operation": "union",
                    "invert": False,
                },
            ]
        raise AssertionError(f"unexpected bridge call: {method}")

    server.bridge.call.side_effect = router
    result = await server._handle_get_module_mask({"op": "exposure", "instance": 1})
    response = json.loads(result[0].text)
    assert response["opacity"] == 100.0
    assert response["shapes"][0]["mask_id"] == 42


@pytest.mark.asyncio
async def test_handle_get_module_mask_requires_op():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_get_module_mask({})
    assert "op is required" in result[0].text
    server.bridge.call.assert_not_called()


# ---- get_mask_geometry / rename_mask (2026-07-31) --------------------------
# Bugreport: agent creating several masks in one session had no way to tell
# them apart afterward except by formid ("orientation among masks"). Also
# closes a gap found while investigating: dt.develop.get_mask already
# existed in C (registered) but was never wired through the Lua bridge or
# an MCP tool -- completely unreachable until this fix.


@pytest.mark.asyncio
async def test_handle_get_mask_geometry_forwards_result():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "mask_id": 42,
        "type": "path",
        "name": "path #1",
        "points": [
            {
                "corner": [0.1, 0.2],
                "ctrl1": [0.1, 0.2],
                "ctrl2": [0.1, 0.2],
                "border": [0.02, 0.02],
                "state": 1,
            }
        ],
    }
    result = await server._handle_get_mask_geometry({"mask_id": 42})
    response = json.loads(result[0].text)
    assert response["mask_id"] == 42
    assert response["points"][0]["corner"] == [0.1, 0.2]
    server.bridge.call.assert_called_once_with("dev_get_mask", {"mask_id": 42}, timeout=15.0)


@pytest.mark.asyncio
async def test_handle_get_mask_geometry_requires_mask_id():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_get_mask_geometry({})
    assert "mask_id is required" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_get_mask_geometry_surfaces_error():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"error": "mask 99 not found in darkroom forms"}
    result = await server._handle_get_mask_geometry({"mask_id": 99})
    assert "not found" in result[0].text


@pytest.mark.asyncio
async def test_handle_rename_mask_reports_ok_and_readback_name():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"ok": True, "mask_id": 42, "name": "model body"}
    result = await server._handle_rename_mask({"mask_id": 42, "name": "model body"})
    text = result[0].text
    assert "ok=True" in text
    assert "model body" in text
    server.bridge.call.assert_called_once_with(
        "dev_rename_mask", {"mask_id": 42, "name": "model body"}, timeout=15.0
    )


@pytest.mark.asyncio
async def test_handle_rename_mask_requires_mask_id_and_name():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_rename_mask({"name": "x"})
    assert "mask_id is required" in result[0].text
    result = await server._handle_rename_mask({"mask_id": 42})
    assert "name is required" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_rename_mask_surfaces_error():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"error": "mask 99 not found in darkroom forms"}
    result = await server._handle_rename_mask({"mask_id": 99, "name": "x"})
    assert "not found" in result[0].text


# ---- delete_mask (2026-07-31) -- housekeeping gap: no way to permanently
# remove an orphan/unwanted mask (detach_mask only unwires it from one
# module, leaving it in dev->forms forever).


@pytest.mark.asyncio
async def test_handle_delete_mask_reports_ok():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"ok": True, "mask_id": 42}
    result = await server._handle_delete_mask({"mask_id": 42})
    text = result[0].text
    assert "ok=True" in text
    assert "42" in text
    server.bridge.call.assert_called_once_with("dev_delete_mask", {"mask_id": 42}, timeout=15.0)


@pytest.mark.asyncio
async def test_handle_delete_mask_requires_mask_id():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_delete_mask({})
    assert "mask_id is required" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_delete_mask_surfaces_error():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"error": "mask 99 not found in darkroom forms"}
    result = await server._handle_delete_mask({"mask_id": 99})
    assert "not found" in result[0].text


# ---- add_path_mask's optional name param (2026-07-31) -- chains
# rename_mask right after creation so a caller doesn't need a second
# round-trip call just to name the mask it just made.


@pytest.mark.asyncio
async def test_handle_add_path_mask_with_name_chains_rename():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = [
        {
            "ok": True,
            "formid": 42,
            "mask_id": 43,
            "points": 4,
            "opacity": 1.0,
            "feather": 0.02,
            "smooth": True,
        },
        {"ok": True, "mask_id": 42, "name": "model body"},
    ]
    result = await server._handle_add_path_mask(
        {
            "op": "exposure",
            "points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.1}, {"x": 0.2, "y": 0.2}],
            "name": "model body",
        }
    )
    text = result[0].text
    assert "model body" in text
    calls = server.bridge.call.call_args_list
    assert calls[1].args[0] == "dev_rename_mask"
    assert calls[1].args[1] == {"mask_id": 42, "name": "model body"}


@pytest.mark.asyncio
async def test_handle_add_path_mask_without_name_skips_rename():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "ok": True,
        "formid": 42,
        "mask_id": 43,
        "points": 4,
        "opacity": 1.0,
        "feather": 0.02,
        "smooth": True,
    }
    await server._handle_add_path_mask(
        {
            "op": "exposure",
            "points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.1}, {"x": 0.2, "y": 0.2}],
        }
    )
    methods_called = [c.args[0] for c in server.bridge.call.call_args_list]
    assert "dev_rename_mask" not in methods_called


# ---- add_path_mask_in_viewport (2026-07-31, delegated block C) -------------
#
# Mirrors retouch_add_shape_in_viewport's own two-stage pipeline (viewport-
# local -> display-frame locally, then display-frame -> mask-frame via
# _backtransform_polygon_to_mask_space, already shipped for mask_object) but
# for an N-point polygon instead of a single target/source pair.


@pytest.mark.asyncio
async def test_handle_add_path_mask_in_viewport_success():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    points = [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.3, "y": 0.4}]
    server.bridge.call.side_effect = [
        IMAGE_13406,
        {"x": 0.1, "y": 0.1},  # identity backtransform, one call per vertex
        {"x": 0.5, "y": 0.1},
        {"x": 0.3, "y": 0.4},
        {
            "ok": True,
            "mask_id": 501,
            "formid": 501,
            "points": 3,
            "opacity": 1.0,
            "feather": 0.02,
            "smooth": True,
        },
    ]
    result = await server._handle_add_path_mask_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "points": points,
            "return_preview": False,
        }
    )
    text = result[0].text
    assert "formid=501" in text
    assert server.bridge.call.call_count == 5
    calls = server.bridge.call.call_args_list
    assert calls[0].args[0] == "dev_current_image"
    for i in range(1, 4):
        assert calls[i].args[0] == "dev_backtransform_point"
    assert calls[4].args[0] == "dev_add_path_mask"
    written = calls[4].args[1]["points"]
    assert written == points  # identity region + identity backtransform
    assert "mask-frame (actual write) bbox" in text


@pytest.mark.asyncio
async def test_handle_add_path_mask_in_viewport_uses_backtransformed_coords():
    """Regression guard: a non-identity backtransform mock (simulating a
    portrait orientation swap) must show up in the written polygon -- proves
    the handler goes through _backtransform_polygon_to_mask_space rather than
    writing the raw display-frame points straight to dev_add_path_mask."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    points = [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.3, "y": 0.4}]
    backtransformed = [{"x": 0.9, "y": 0.05}, {"x": 0.8, "y": 0.15}, {"x": 0.7, "y": 0.25}]
    server.bridge.call.side_effect = (
        [IMAGE_13406]
        + backtransformed
        + [
            {
                "ok": True,
                "mask_id": 502,
                "formid": 502,
                "points": 3,
                "opacity": 1.0,
                "feather": 0.02,
                "smooth": True,
            },
        ]
    )
    await server._handle_add_path_mask_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "points": points,
            "return_preview": False,
        }
    )
    written = server.bridge.call.call_args_list[-1].args[1]["points"]
    assert written == backtransformed
    assert written != points


@pytest.mark.asyncio
async def test_handle_add_path_mask_in_viewport_rejects_out_of_bounds():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    with pytest.raises(ViewportCoordinateError):
        await server._handle_add_path_mask_in_viewport(
            {
                "snapshot_id": snapshot_id,
                "op": "exposure",
                "points": [{"x": 1.5, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.3, "y": 0.4}],
            }
        )
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_add_path_mask_in_viewport_rejects_unknown_snapshot():
    server = DarktableMCPServer()
    server.bridge = Mock()
    with pytest.raises(ViewportCoordinateError):
        await server._handle_add_path_mask_in_viewport(
            {
                "snapshot_id": "vp_doesnotexist",
                "op": "exposure",
                "points": [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.3, "y": 0.4}],
            }
        )


@pytest.mark.asyncio
async def test_handle_add_path_mask_in_viewport_rejects_too_few_points():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    result = await server._handle_add_path_mask_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "points": [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}],
        }
    )
    assert "at least 3" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_add_path_mask_in_viewport_refuses_on_image_mismatch():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        {"has_image": True, "id": 99999, "filename": "other.ARW"},
    ]
    result = await server._handle_add_path_mask_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "points": [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.3, "y": 0.4}],
        }
    )
    text = result[0].text
    assert "image mismatch" in text
    assert "13406" in text and "99999" in text
    # never reached the backtransform/write stage.
    assert server.bridge.call.call_count == 1


@pytest.mark.asyncio
async def test_handle_add_path_mask_in_viewport_forwards_feather_smooth_opacity():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    points = [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.3, "y": 0.4}]
    server.bridge.call.side_effect = (
        [IMAGE_13406]
        + points
        + [
            {
                "ok": True,
                "mask_id": 503,
                "formid": 503,
                "points": 3,
                "opacity": 0.5,
                "feather": 0.1,
                "smooth": False,
            },
        ]
    )
    await server._handle_add_path_mask_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "points": points,
            "opacity": 0.5,
            "feather": 0.1,
            "smooth": False,
            "return_preview": False,
        }
    )
    write_call = server.bridge.call.call_args_list[-1]
    assert write_call.args[1]["opacity"] == pytest.approx(0.5)
    assert write_call.args[1]["feather"] == pytest.approx(0.1)
    assert write_call.args[1]["smooth"] is False


@pytest.mark.asyncio
async def test_handle_add_path_mask_in_viewport_with_name_chains_rename():
    server = DarktableMCPServer()
    server.bridge = Mock()
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": "/tmp/snap.png", "width": 500, "height": 500},
            "image": IMAGE_13406,
        }
    )
    points = [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.3, "y": 0.4}]
    server.bridge.call.side_effect = (
        [IMAGE_13406]
        + points
        + [
            {
                "ok": True,
                "mask_id": 504,
                "formid": 504,
                "points": 3,
                "opacity": 1.0,
                "feather": 0.02,
                "smooth": True,
            },
            {"ok": True, "mask_id": 504, "name": "hand-picked area"},
        ]
    )
    result = await server._handle_add_path_mask_in_viewport(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "points": points,
            "name": "hand-picked area",
            "return_preview": False,
        }
    )
    text = result[0].text
    assert "hand-picked area" in text
    assert server.bridge.call.call_args_list[-1].args[0] == "dev_rename_mask"


# ---- render_module_mask (2026-07-31, delegated block C) --------------------
#
# The generic-mask counterpart to retouch_render_overlay's own tests above:
# darktable's mask overlay is painted on the GUI widget for every mask type,
# not just retouch's circles, so this is synthesized the same way from
# get_mask_geometry's node data, forward-transformed via dev_transform_point
# (already shipped, used here for the first time from the Python side).

PATH_NODE_CORNER = {
    "corner": {"x": 0.3, "y": 0.3},
    "ctrl1": {"x": 0.3, "y": 0.3},
    "ctrl2": {"x": 0.3, "y": 0.3},
    "border": {"x": 0.01, "y": 0.01},
    "state": 1,
}
PATH_NODE_SMOOTH = {
    "corner": {"x": 0.5, "y": 0.3},
    "ctrl1": {"x": 0.45, "y": 0.3},
    "ctrl2": {"x": 0.55, "y": 0.3},
    "border": {"x": 0.01, "y": 0.01},
    "state": 1,
}
PATH_NODE_3 = {
    "corner": {"x": 0.4, "y": 0.5},
    "ctrl1": {"x": 0.4, "y": 0.5},
    "ctrl2": {"x": 0.4, "y": 0.5},
    "border": {"x": 0.01, "y": 0.01},
    "state": 1,
}


def _module_mask_server(tmp_path, mask_refs, get_mask_replies, width=400, height=200, region=None):
    server = DarktableMCPServer()
    server.bridge = Mock()
    render_path = _write_test_render(tmp_path / "snap.png", width, height)
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": region or {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": render_path, "width": width, "height": height},
            "image": IMAGE_13406,
        }
    )
    return server, snapshot_id, [IMAGE_13406, mask_refs] + get_mask_replies


@pytest.mark.asyncio
async def test_handle_render_module_mask_draws_path_with_node_states(tmp_path):
    mask_refs = [
        {
            "mask_id": 61,
            "type": "path",
            "opacity": 1.0,
            "nb_points": 3,
            "name": "path #1",
            "module": "exposure",
            "instance": 0,
            "operation": "union",
            "invert": False,
        }
    ]
    get_mask = {
        "mask_id": 61,
        "type": "path",
        "name": "path #1",
        "points": [PATH_NODE_CORNER, PATH_NODE_SMOOTH, PATH_NODE_3],
    }
    server, snapshot_id, side_effect = _module_mask_server(
        tmp_path,
        mask_refs,
        [get_mask, {"x": 0.3, "y": 0.3}, {"x": 0.5, "y": 0.3}, {"x": 0.4, "y": 0.5}],
    )
    server.bridge.call.side_effect = side_effect
    result = await server._handle_render_module_mask(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "return_image": False,
        }
    )
    text = result[-1].text
    assert "formid=61" in text
    assert "type=path" in text
    assert "node_count=3" in text
    assert "corner_node_count=2" in text
    out = text.split("\n")[0].split(": ")[-1]
    assert os.path.isfile(out)


@pytest.mark.asyncio
async def test_handle_render_module_mask_draws_circle_with_transformed_radius(tmp_path):
    mask_refs = [
        {
            "mask_id": 62,
            "type": "circle",
            "opacity": 1.0,
            "nb_points": 1,
            "name": "circle #1",
            "module": "exposure",
            "instance": 0,
            "operation": "union",
            "invert": False,
        }
    ]
    get_mask = {
        "mask_id": 62,
        "type": "circle",
        "name": "circle #1",
        "points": [{"center": {"x": 0.5, "y": 0.5}, "radius": 0.02, "border": 0.01}],
    }
    server, snapshot_id, side_effect = _module_mask_server(
        tmp_path,
        mask_refs,
        [get_mask, {"x": 0.5, "y": 0.5, "len1": 0.05}],
    )
    server.bridge.call.side_effect = side_effect
    result = await server._handle_render_module_mask(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "return_image": False,
        }
    )
    text = result[-1].text
    assert "formid=62" in text
    assert "type=circle" in text
    # full-frame region, 400x200 render, display center 0.5,0.5 -> 200,100px;
    # radius normalized against display WIDTH 0.05*400=20 -> bbox spans it.
    assert "bbox_px=[180.0, 80.0, 220.0, 120.0]" in text


@pytest.mark.asyncio
async def test_handle_render_module_mask_refuses_on_image_mismatch(tmp_path):
    server = DarktableMCPServer()
    server.bridge = Mock()
    render_path = _write_test_render(tmp_path / "snap.png")
    snapshot_id = server._store_viewport_snapshot(
        {
            "viewport": "main",
            "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "render": {"path": render_path, "width": 400, "height": 200},
            "image": IMAGE_13406,
        }
    )
    server.bridge.call.side_effect = [
        {"has_image": True, "id": 99999, "filename": "other.ARW"},
    ]
    result = await server._handle_render_module_mask(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
        }
    )
    text = result[0].text
    assert "image mismatch" in text
    assert server.bridge.call.call_count == 1


@pytest.mark.asyncio
async def test_handle_render_module_mask_no_masks_attached(tmp_path):
    server, snapshot_id, side_effect = _module_mask_server(tmp_path, [], [])
    server.bridge.call.side_effect = side_effect
    result = await server._handle_render_module_mask(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
        }
    )
    assert "no drawn masks attached" in result[0].text


@pytest.mark.asyncio
async def test_handle_render_module_mask_skips_ellipse_as_unsupported(tmp_path):
    mask_refs = [
        {
            "mask_id": 63,
            "type": "ellipse",
            "opacity": 1.0,
            "nb_points": 1,
            "name": "ellipse #1",
            "module": "exposure",
            "instance": 0,
            "operation": "union",
            "invert": False,
        }
    ]
    get_mask = {
        "mask_id": 63,
        "type": "ellipse",
        "name": "ellipse #1",
        "points": [
            {
                "center": {"x": 0.5, "y": 0.5},
                "radius_a": 0.05,
                "radius_b": 0.02,
                "rotation": 0.0,
                "border": 0.01,
            }
        ],
    }
    server, snapshot_id, side_effect = _module_mask_server(tmp_path, mask_refs, [get_mask])
    server.bridge.call.side_effect = side_effect
    result = await server._handle_render_module_mask(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
        }
    )
    assert "no drawable shapes" in result[0].text
    assert "63" in result[0].text


@pytest.mark.asyncio
async def test_handle_render_module_mask_only_greyscale(tmp_path):
    from PIL import Image

    mask_refs = [
        {
            "mask_id": 64,
            "type": "circle",
            "opacity": 1.0,
            "nb_points": 1,
            "name": "circle #1",
            "module": "exposure",
            "instance": 0,
            "operation": "union",
            "invert": False,
        }
    ]
    get_mask = {
        "mask_id": 64,
        "type": "circle",
        "name": "circle #1",
        "points": [{"center": {"x": 0.5, "y": 0.5}, "radius": 0.02, "border": 0.0}],
    }
    server, snapshot_id, side_effect = _module_mask_server(
        tmp_path,
        mask_refs,
        [get_mask, {"x": 0.5, "y": 0.5, "len1": 0.05}],
    )
    server.bridge.call.side_effect = side_effect
    result = await server._handle_render_module_mask(
        {
            "snapshot_id": snapshot_id,
            "op": "exposure",
            "mode": "mask_only",
            "return_image": False,
        }
    )
    text = result[-1].text
    out = text.split("\n")[0].split(": ")[-1]
    img = Image.open(out)
    assert img.mode == "L"
    assert img.getpixel((200, 100)) == 255
    assert img.getpixel((399, 199)) == 0


@pytest.mark.asyncio
async def test_handle_attach_mask_forwards_args():
    server = DarktableMCPServer()
    server.bridge = Mock()

    def router(method, params, timeout=None):
        if method == "dev_attach_mask":
            return {
                "ok": True,
                "op": "exposure",
                "instance": 1,
                "formid": 42,
                "operation": "union",
                "mask_mode": 3,
            }
        if method == "dev_get_blend_params":
            return {
                "op": "exposure",
                "instance": 1,
                "opacity": 100.0,
                "mask_mode": 3,
                "blend_mode": 3,
                "invert": False,
            }
        raise AssertionError(f"unexpected bridge call: {method}")

    server.bridge.call.side_effect = router
    result = await server._handle_attach_mask({"op": "exposure", "instance": 1, "formid": 42})
    assert "formid=42" in result[0].text
    server.bridge.call.assert_any_call(
        "dev_attach_mask",
        {"op": "exposure", "instance": 1, "formid": 42, "operation": "union"},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_attach_mask_warns_if_drawn_mask_not_enabled():
    """Regression guard for the 2026-07-27 bugreport: attaching a shape wired
    the group reference but left mask_mode's DEVELOP_MASK_MASK bit off, so the
    mask existed but had zero visible effect until the user clicked the
    pencil icon by hand. attach_mask must verify via an INDEPENDENT
    get_blend_params read, not just trust its own reported mask_mode."""
    server = DarktableMCPServer()
    server.bridge = Mock()

    def router(method, params, timeout=None):
        if method == "dev_attach_mask":
            # Reports mask_mode=3 (bit set) but the independent verify read
            # disagrees -- the WARNING must come from the verify call, not
            # from trusting attach_mask's own claim.
            return {
                "ok": True,
                "op": "exposure",
                "instance": 1,
                "formid": 42,
                "operation": "union",
                "mask_mode": 3,
            }
        if method == "dev_get_blend_params":
            return {
                "op": "exposure",
                "instance": 1,
                "opacity": 100.0,
                "mask_mode": 0,
                "blend_mode": 3,
                "invert": False,
            }
        raise AssertionError(f"unexpected bridge call: {method}")

    server.bridge.call.side_effect = router
    result = await server._handle_attach_mask({"op": "exposure", "instance": 1, "formid": 42})
    assert "WARNING" in result[0].text
    assert "NOT enabled" in result[0].text


@pytest.mark.asyncio
async def test_handle_attach_mask_requires_formid():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_attach_mask({"op": "exposure"})
    assert "formid" in result[0].text
    server.bridge.call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_detach_mask_forwards_args():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"ok": True, "op": "exposure", "instance": 1, "formid": 42}
    result = await server._handle_detach_mask({"op": "exposure", "instance": 1, "formid": 42})
    assert "detached" in result[0].text
    server.bridge.call.assert_called_once_with(
        "dev_detach_mask",
        {"op": "exposure", "instance": 1, "formid": 42},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_handle_set_module_mask_attaches_new_and_detaches_missing():
    server = DarktableMCPServer()
    server.bridge = Mock()
    calls = []

    def router(method, params, timeout=None):
        calls.append((method, params))
        if method == "dev_list_masks":
            return [
                {"mask_id": 1, "operation": "union"},
                {"mask_id": 2, "operation": "union"},
            ]
        if method == "dev_attach_mask":
            return {"ok": True, "formid": params["formid"], "operation": params["operation"]}
        if method == "dev_detach_mask":
            return {"ok": True, "formid": params["formid"]}
        if method == "dev_get_blend_params":
            return {
                "op": "exposure",
                "instance": 1,
                "opacity": 100.0,
                "mask_mode": 3,
                "blend_mode": 3,
                "invert": False,
            }
        raise AssertionError(f"unexpected bridge call: {method}")

    server.bridge.call.side_effect = router
    result = await server._handle_set_module_mask(
        {
            "op": "exposure",
            "instance": 1,
            "shapes": [
                {"formid": 2, "operation": "union"},
                {"formid": 3, "operation": "difference"},
            ],
        }
    )
    text = result[0].text
    assert "attached=[3]" in text
    assert "detached=[1]" in text
    # formid=2 already present with the same operation -> untouched.
    detach_targets = [p["formid"] for m, p in calls if m == "dev_detach_mask"]
    attach_targets = [p["formid"] for m, p in calls if m == "dev_attach_mask"]
    assert detach_targets == [1]
    assert attach_targets == [3]


@pytest.mark.asyncio
async def test_handle_set_module_mask_reattaches_on_operation_change():
    server = DarktableMCPServer()
    server.bridge = Mock()
    calls = []

    def router(method, params, timeout=None):
        calls.append((method, params))
        if method == "dev_list_masks":
            return [{"mask_id": 1, "operation": "union"}]
        if method == "dev_attach_mask":
            return {"ok": True, "formid": params["formid"], "operation": params["operation"]}
        if method == "dev_detach_mask":
            return {"ok": True, "formid": params["formid"]}
        if method == "dev_get_blend_params":
            return {
                "op": "exposure",
                "instance": 1,
                "opacity": 100.0,
                "mask_mode": 3,
                "blend_mode": 3,
                "invert": False,
            }
        raise AssertionError(f"unexpected bridge call: {method}")

    server.bridge.call.side_effect = router
    result = await server._handle_set_module_mask(
        {
            "op": "exposure",
            "instance": 1,
            "shapes": [{"formid": 1, "operation": "difference"}],
        }
    )
    detach_targets = [p["formid"] for m, p in calls if m == "dev_detach_mask"]
    attach_targets = [(p["formid"], p["operation"]) for m, p in calls if m == "dev_attach_mask"]
    assert detach_targets == [1], "operation change must detach the stale link first"
    assert attach_targets == [(1, "difference")]
    assert "attached=[1]" in result[0].text
    assert "detached=[1]" in result[0].text


@pytest.mark.asyncio
async def test_handle_set_module_mask_forwards_opacity_and_invert():
    server = DarktableMCPServer()
    server.bridge = Mock()

    def router(method, params, timeout=None):
        if method == "dev_list_masks":
            return []
        if method == "dev_attach_mask":
            return {"ok": True, "formid": params["formid"], "operation": params["operation"]}
        if method == "dev_set_blend_params":
            return {
                "ok": True,
                "opacity": params["fields"]["opacity"],
                "invert": params["fields"]["invert"],
            }
        if method == "dev_get_blend_params":
            return {
                "op": "exposure",
                "instance": 1,
                "opacity": 40.0,
                "mask_mode": 3,
                "blend_mode": 3,
                "invert": True,
            }
        raise AssertionError(f"unexpected bridge call: {method}")

    server.bridge.call.side_effect = router
    result = await server._handle_set_module_mask(
        {
            "op": "exposure",
            "instance": 1,
            "shapes": [{"formid": 5}],
            "opacity": 40.0,
            "invert": True,
        }
    )
    assert "opacity=40.0" in result[0].text
    assert "invert=True" in result[0].text


@pytest.mark.asyncio
async def test_handle_set_module_mask_requires_shapes_list():
    server = DarktableMCPServer()
    server.bridge = Mock()
    result = await server._handle_set_module_mask({"op": "exposure", "shapes": "nope"})
    assert "shapes must be a list" in result[0].text
    server.bridge.call.assert_not_called()


# ---- preview_lut/compare_luts opacity (blend) integration (2026-07-26) -----


@pytest.mark.asyncio
async def test_handle_preview_lut_opacity_applies_and_restores_blend(tmp_path):
    (tmp_path / "a.cube").write_text("")
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = _lut3d_bridge_router(
        str(tmp_path), blend_mask_mode=0, blend_opacity=100.0
    )

    with (
        patch("darktable_mcp.server._remap_bridge_path", side_effect=lambda p: p),
        patch("darktable_mcp.server._inline_image_content", return_value=None),
    ):
        result = await server._handle_preview_lut({"path": "a.cube", "opacity": 0.35})

    assert "ok=True" in result[-1].text

    calls = server.bridge.call.call_args_list
    blend_calls = [c for c in calls if c.args[0] == "dev_set_blend_params"]
    assert len(blend_calls) == 2, "one apply, one restore"
    apply_fields = blend_calls[0].args[1]["fields"]
    assert apply_fields["opacity"] == pytest.approx(35.0)
    assert apply_fields["enable_uniform_blend"] is True
    restore_fields = blend_calls[1].args[1]["fields"]
    # Restored to the pre-call snapshot: opacity=100.0, mask_mode=0 -> disabled.
    assert restore_fields["opacity"] == pytest.approx(100.0)
    assert restore_fields["enable_uniform_blend"] is False


@pytest.mark.asyncio
async def test_handle_preview_lut_opacity_refused_when_already_masked(tmp_path):
    (tmp_path / "a.cube").write_text("")
    server = DarktableMCPServer()
    server.bridge = Mock()
    # mask_mode=3 (ENABLED|MASK) -- a drawn mask is already wired to this
    # instance; enable_uniform_blend's all-or-nothing write would clobber it.
    server.bridge.call.side_effect = _lut3d_bridge_router(
        str(tmp_path), blend_mask_mode=3, blend_opacity=80.0
    )

    result = await server._handle_preview_lut({"path": "a.cube", "opacity": 0.5})
    text = result[0].text
    assert "already has a mask configured" in text

    # Nothing should have been written -- get_blend_params was read-only, no
    # set_params/set_blend_params call ever happened.
    methods_called = [c.args[0] for c in server.bridge.call.call_args_list]
    assert "dev_set_params" not in methods_called
    assert "dev_set_blend_params" not in methods_called


# ---- mask_object: SAM2 polygon frame-mismatch fix (2026-07-27 bugreport) ----
# Bugreport: SAM2 body mask landed on a tree instead of the subject on a
# portrait ARW with an active `flip` module. Root cause: the sidecar
# segments dev_preview's render (PROCESSED/DISPLAY frame) but add_path_mask
# stores PIPE-INPUT/mask-frame points -- the exact mismatch already fixed
# for retouch's target/source (2026-07-25) but never applied to mask_object.


def _mask_object_bridge_router(polygon_len, backtransform_reply, new_instance=True):
    """Build a side_effect list for server.bridge.call matching
    _handle_mask_object's call order: dev_preview,
    dev_backtransform_point * polygon_len (runs BEFORE add_instance, so a
    bad backtransform never creates an orphan instance), [dev_add_instance],
    dev_add_path_mask, dev_set_params, dev_preview (final)."""
    calls = [{"path": "/tmp/preview.png"}]
    calls.extend([backtransform_reply] * polygon_len)
    if new_instance:
        calls.append({"instance": 1})
    calls.append({"ok": True, "mask_id": 555, "opacity": 1.0})
    calls.append({"applied": {"exposure": 0.2}})
    calls.append({"path": "/tmp/preview2.png"})
    return calls


@pytest.mark.asyncio
async def test_handle_mask_object_backtransforms_polygon_before_add_path_mask():
    """Guards against regressing to the pre-fix bug: mask_object must convert
    every sidecar polygon vertex through dev_backtransform_point before
    writing it to add_path_mask, not pass the sidecar's display-frame
    polygon straight through. Non-identity mock (simulating a portrait
    orientation swap) proves the handler actually uses the backtransformed
    output, not the raw sidecar value."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    sidecar_polygon = [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.2}, {"x": 0.2, "y": 0.4}]
    # Deliberately NOT equal to the sidecar input -- proves the handler uses
    # the backtransform's output rather than the raw display-frame polygon.
    backtransformed_reply = {"x": 0.9, "y": 0.05}
    server.bridge.call.side_effect = _mask_object_bridge_router(
        len(sidecar_polygon), backtransformed_reply
    )

    with patch(
        "darktable_mcp.server.run_segmentation",
        return_value={
            "polygon": sidecar_polygon,
            "bbox": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.2},
            "score": 0.86,
            "backend": "sam2",
        },
    ):
        result = await server._handle_mask_object(
            {
                "op": "exposure",
                "adjustment": {"exposure": 0.2},
                "box": {"x": 0.2, "y": 0.3, "w": 0.2, "h": 0.4},
            }
        )

    text = result[0].text
    assert "ok=True" in text

    calls = server.bridge.call.call_args_list
    methods_called = [c.args[0] for c in calls]
    assert methods_called.count("dev_backtransform_point") == len(sidecar_polygon)

    add_path_mask_call = next(c for c in calls if c.args[0] == "dev_add_path_mask")
    written_points = add_path_mask_call.args[1]["points"]
    assert len(written_points) == len(sidecar_polygon)
    for pt in written_points:
        assert pt == {"x": 0.9, "y": 0.05}
    # Never the raw sidecar polygon straight through.
    assert written_points != sidecar_polygon


@pytest.mark.asyncio
async def test_handle_mask_object_forwards_smooth_and_max_nodes():
    """smooth (default True) must reach dev_add_path_mask, and an explicit
    max_nodes must reach run_segmentation -- both were silently dropped
    before this (mask_object never forwarded either)."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    sidecar_polygon = [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.2}, {"x": 0.2, "y": 0.4}]
    server.bridge.call.side_effect = _mask_object_bridge_router(
        len(sidecar_polygon), {"x": 0.9, "y": 0.05}
    )

    with patch(
        "darktable_mcp.server.run_segmentation",
        return_value={
            "polygon": sidecar_polygon,
            "bbox": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.2},
            "score": 0.86,
            "backend": "sam2",
        },
    ) as mock_run_seg:
        await server._handle_mask_object(
            {
                "op": "exposure",
                "adjustment": {"exposure": 0.2},
                "box": {"x": 0.2, "y": 0.3, "w": 0.2, "h": 0.4},
                "smooth": False,
                "max_nodes": 96,
            }
        )

    assert mock_run_seg.call_args.kwargs["max_nodes"] == 96

    calls = server.bridge.call.call_args_list
    add_path_mask_call = next(c for c in calls if c.args[0] == "dev_add_path_mask")
    assert add_path_mask_call.args[1]["smooth"] is False


@pytest.mark.asyncio
async def test_handle_mask_object_with_name_chains_rename():
    """Optional name param -- NOT the same as `label` (the segmentation
    prompt) -- chains a dev_rename_mask call right after add_path_mask."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    sidecar_polygon = [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.2}, {"x": 0.2, "y": 0.4}]
    calls = [{"path": "/tmp/preview.png"}]
    calls.extend([{"x": 0.9, "y": 0.05}] * len(sidecar_polygon))
    calls.append({"instance": 1})
    calls.append({"ok": True, "formid": 42, "mask_id": 555, "opacity": 1.0})
    calls.append({"ok": True, "mask_id": 42, "name": "model body"})
    calls.append({"applied": {"exposure": 0.2}})
    calls.append({"path": "/tmp/preview2.png"})
    server.bridge.call.side_effect = calls

    with patch(
        "darktable_mcp.server.run_segmentation",
        return_value={
            "polygon": sidecar_polygon,
            "bbox": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.2},
            "score": 0.86,
            "backend": "sam2",
        },
    ):
        result = await server._handle_mask_object(
            {
                "op": "exposure",
                "adjustment": {"exposure": 0.2},
                "box": {"x": 0.2, "y": 0.3, "w": 0.2, "h": 0.4},
                "name": "model body",
            }
        )

    text = result[0].text
    assert "model body" in text
    call_list = server.bridge.call.call_args_list
    rename_call = next(c for c in call_list if c.args[0] == "dev_rename_mask")
    assert rename_call.args[1] == {"mask_id": 42, "name": "model body"}


@pytest.mark.asyncio
async def test_handle_mask_object_reports_both_frame_bboxes():
    """Output must clearly label which bbox is display-frame (what the
    sidecar segmented) vs mask-frame (what actually got written) -- the
    bugreport's core complaint was that it could not tell these apart."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    sidecar_polygon = [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.2}, {"x": 0.2, "y": 0.4}]
    server.bridge.call.side_effect = _mask_object_bridge_router(
        len(sidecar_polygon), {"x": 0.9, "y": 0.05}
    )

    with patch(
        "darktable_mcp.server.run_segmentation",
        return_value={
            "polygon": sidecar_polygon,
            "bbox": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.2},
            "score": 0.86,
            "backend": "sam2",
        },
    ):
        result = await server._handle_mask_object(
            {
                "op": "exposure",
                "adjustment": {"exposure": 0.2},
                "box": {"x": 0.2, "y": 0.3, "w": 0.2, "h": 0.4},
            }
        )

    text = result[0].text
    assert "bbox_display_frame=" in text
    assert "bbox_mask_frame=" in text
    # Mask-frame bbox must reflect the (non-identity) backtransformed points,
    # not the sidecar's display-frame bbox.
    assert '"x": 0.9' in text.replace("'", '"') or "0.9" in text


@pytest.mark.asyncio
async def test_handle_mask_object_backtransform_failure_no_orphan_instance():
    """If dev_backtransform_point errors mid-polygon, mask_object must fail
    cleanly with no add_path_mask call. The backtransform step runs BEFORE
    add_instance specifically so this failure mode can never create an
    orphan module instance -- assert add_instance is never even reached."""
    server = DarktableMCPServer()
    server.bridge = Mock()
    sidecar_polygon = [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.2}, {"x": 0.2, "y": 0.4}]
    server.bridge.call.side_effect = [
        {"path": "/tmp/preview.png"},
        {"x": 0.9, "y": 0.05},
        {"error": "no darkroom image loaded"},
    ]

    with patch(
        "darktable_mcp.server.run_segmentation",
        return_value={
            "polygon": sidecar_polygon,
            "bbox": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.2},
            "score": 0.86,
            "backend": "sam2",
        },
    ):
        result = await server._handle_mask_object(
            {
                "op": "exposure",
                "adjustment": {"exposure": 0.2},
                "box": {"x": 0.2, "y": 0.3, "w": 0.2, "h": 0.4},
            }
        )

    text = result[0].text
    assert "backtransform_point(polygon[1])" in text
    methods_called = [c.args[0] for c in server.bridge.call.call_args_list]
    assert "dev_add_instance" not in methods_called
    assert "dev_add_path_mask" not in methods_called
