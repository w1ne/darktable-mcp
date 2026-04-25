"""Tests for the main MCP server."""

import asyncio
import pytest
from unittest.mock import Mock, patch

from darktable_mcp.server import DarktableMCPServer


class TestDarktableMCPServer:
    """Test cases for DarktableMCPServer."""

    def test_server_initialization(self):
        """Test server can be initialized."""
        server = DarktableMCPServer()
        assert server is not None
        assert hasattr(server, 'app')

    def test_server_has_required_tools(self):
        """Test server registers all required tools."""
        server = DarktableMCPServer()

        expected_tools = [
            'view_photos',
            'rate_photos',
            'import_batch',
            'adjust_exposure',
            'apply_preset',
            'export_images'
        ]

        for tool_name in expected_tools:
            assert tool_name in server.list_tools()

    @pytest.mark.asyncio
    async def test_server_can_start(self):
        """Test server can start without errors."""
        server = DarktableMCPServer()

        with patch('darktable_mcp.server.stdio_server') as mock_serve:
            # Create a completed future
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            future.set_result(None)
            mock_serve.return_value = future

            # Should not raise exception
            await server.start()
            mock_serve.assert_called_once()