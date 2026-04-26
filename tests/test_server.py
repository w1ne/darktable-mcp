"""Tests for the main MCP server."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

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
        expected_tools = [
            "view_photos",
            "rate_photos",
            "import_batch",
            "adjust_exposure",
            "apply_preset",
            "export_images",
        ]
        for tool_name in expected_tools:
            assert tool_name in server.list_tools()

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


class TestToolHandlers:
    """Test cases for tool handler integration."""

    @pytest.mark.asyncio
    async def test_view_photos_integration(self):
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.PhotoTools") as MockPhotoTools:
            mock_tools = MockPhotoTools.return_value
            mock_tools.view_photos.return_value = [{"id": "123", "filename": "test.jpg"}]

            # Patch the server's photo_tools instance
            server._photo_tools = mock_tools

            result = await server._handle_view_photos({"filter": "test", "limit": 10})

            assert len(result) == 1
            assert "test.jpg" in result[0].text
            mock_tools.view_photos.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_photos_integration(self):
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.PhotoTools") as MockPhotoTools:
            mock_tools = MockPhotoTools.return_value
            mock_tools.rate_photos.return_value = "Updated 2 photos with 4 stars"

            server._photo_tools = mock_tools

            result = await server._handle_rate_photos({"photo_ids": ["123", "456"], "rating": 4})

            assert len(result) == 1
            assert "Updated 2 photos" in result[0].text
            mock_tools.rate_photos.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_batch_integration(self):
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.PhotoTools") as MockPhotoTools:
            mock_tools = MockPhotoTools.return_value
            mock_tools.import_batch.return_value = "Imported 5 photos from /path/to/photos"

            server._photo_tools = mock_tools

            result = await server._handle_import_batch({"source_path": "/path/to/photos"})

            assert len(result) == 1
            assert "Imported 5 photos" in result[0].text
            mock_tools.import_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_adjust_exposure_integration(self):
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.PhotoTools") as MockPhotoTools:
            mock_tools = MockPhotoTools.return_value
            mock_tools.adjust_exposure.return_value = "Adjusted exposure for 2 photos by 1.5 EV"

            server._photo_tools = mock_tools

            result = await server._handle_adjust_exposure(
                {"photo_ids": ["123", "456"], "exposure_ev": 1.5}
            )

            assert len(result) == 1
            assert "Adjusted exposure" in result[0].text
            mock_tools.adjust_exposure.assert_called_once()

    @pytest.mark.asyncio
    async def test_view_photos_no_results(self):
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.PhotoTools") as MockPhotoTools:
            mock_tools = MockPhotoTools.return_value
            mock_tools.view_photos.return_value = []

            server._photo_tools = mock_tools

            result = await server._handle_view_photos({"filter": "nonexistent", "limit": 10})

            assert len(result) == 1
            assert "No photos found" in result[0].text


def test_all_tools_implemented():
    """Verify all tools are registered in the MCP server."""
    server = DarktableMCPServer()
    tools = server.list_tools()

    implemented_tools = [
        "view_photos",
        "rate_photos",
        "import_batch",
        "adjust_exposure",
        "export_images",
    ]
    stubbed_tools = ["apply_preset"]

    for tool in implemented_tools:
        assert tool in tools, f"Tool {tool} not found in server"

    for tool in stubbed_tools:
        assert tool in tools, f"Tool {tool} not found in server"


def test_all_imports_work():
    """Verify all core module imports work correctly."""
    from darktable_mcp.darktable.library_detector import LibraryDetector
    from darktable_mcp.darktable.lua_executor import LuaExecutor
    from darktable_mcp.server import DarktableMCPServer
    from darktable_mcp.tools.photo_tools import PhotoTools

    # All imports should work without errors
    assert DarktableMCPServer is not None
    assert LuaExecutor is not None
    assert LibraryDetector is not None
    assert PhotoTools is not None
