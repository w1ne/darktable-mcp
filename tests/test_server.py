"""Tests for the main MCP server."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

from darktable_mcp.server import DarktableMCPServer


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
            "rate_photos",
            "import_batch",
            "list_styles",
            "apply_preset",
        }
        assert set(server.list_tools()) == expected_tools

    @pytest.mark.asyncio
    async def test_server_can_start(self):
        server = DarktableMCPServer()

        @asynccontextmanager
        async def fake_stdio():
            yield (AsyncMock(), AsyncMock())

        with patch("darktable_mcp.server.stdio_server", fake_stdio), patch.object(
            server.app, "run", new=AsyncMock(return_value=None)
        ) as mock_run:
            await server.start()
            mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_import_from_camera_handler():
    server = DarktableMCPServer()
    mock_tools = Mock()
    mock_tools.import_from_camera.return_value = (
        "Copied 5 file(s) from Nikon DSC D800E (usb:002,002)\n"
        "Destination: /tmp/import-2026-04-26"
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
        {"id": "1", "filename": "a.NEF", "path": "/p", "rating": 5},
        {"id": "2", "filename": "b.NEF", "path": "/p", "rating": 4},
    ]
    result = await server._handle_view_photos({"filter": "", "limit": 10})
    assert len(result) == 1
    text = result[0].text
    assert "a.NEF" in text
    assert "b.NEF" in text
    server.bridge.call.assert_called_once_with(
        "view_photos", {"filter": "", "limit": 10}
    )


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
async def test_handle_import_batch_returns_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"imported": 12, "source_path": "/path/foo"}
    result = await server._handle_import_batch({"source_path": "/path/foo"})
    assert "Imported 12" in result[0].text
    assert "/path/foo" in result[0].text
    server.bridge.call.assert_called_once_with(
        "import_batch", {"source_path": "/path/foo"}
    )


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
    result = await server._handle_apply_preset({
        "photo_ids": ["1", "2", "3"], "preset_name": "myStyle",
    })
    text = result[0].text
    assert "myStyle" in text
    assert "3 photo" in text


@pytest.mark.asyncio
async def test_handle_apply_preset_reports_missed():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "applied": 1, "missed": ["999"], "preset_name": "myStyle",
    }
    result = await server._handle_apply_preset({
        "photo_ids": ["1", "999"], "preset_name": "myStyle",
    })
    text = result[0].text
    assert "999" in text
    assert "Missed" in text


@pytest.mark.asyncio
async def test_handle_apply_preset_friendly_error_when_dt_not_running():
    from darktable_mcp.bridge.client import BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")
    result = await server._handle_apply_preset({
        "photo_ids": ["1"], "preset_name": "x",
    })
    assert "darktable" in result[0].text.lower()
