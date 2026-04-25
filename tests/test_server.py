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

        with patch("darktable_mcp.server.stdio_server", fake_stdio), \
             patch.object(server.app, "run", new=AsyncMock(return_value=None)) as mock_run:
            await server.start()
            mock_run.assert_called_once()
