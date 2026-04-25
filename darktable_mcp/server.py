"""Main MCP server for darktable integration."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .utils.errors import DarktableMCPError

logger = logging.getLogger(__name__)


class DarktableMCPServer:
    """MCP server for darktable photo management and editing."""

    def __init__(self):
        """Initialize the darktable MCP server."""
        self.app = Server("darktable-mcp")
        self._setup_tools()

    def _setup_tools(self) -> None:
        """Register all available tools with the MCP server."""

        @self.app.list_tools()
        async def list_tools() -> List[Tool]:
            """List all available darktable tools."""
            return [
                Tool(
                    name="view_photos",
                    description="Browse photo library with optional filters",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "filter": {
                                "type": "string",
                                "description": "Filter criteria (e.g., 'landscape', 'portrait')"
                            },
                            "rating_min": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                                "description": "Minimum star rating"
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of photos to return"
                            }
                        }
                    }
                ),
                Tool(
                    name="rate_photos",
                    description="Apply star ratings to photos",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "photo_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of photo IDs to rate"
                            },
                            "rating": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                                "description": "Star rating (1-5)"
                            },
                            "filter": {
                                "type": "string",
                                "description": "Filter criteria to select photos"
                            }
                        },
                        "required": ["rating"]
                    }
                ),
                Tool(
                    name="import_batch",
                    description="Import photos from directories",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source_path": {
                                "type": "string",
                                "description": "Source directory path"
                            },
                            "recursive": {
                                "type": "boolean",
                                "description": "Search subdirectories recursively"
                            },
                            "copy": {
                                "type": "boolean",
                                "description": "Copy files vs. reference in place"
                            }
                        },
                        "required": ["source_path"]
                    }
                ),
                Tool(
                    name="adjust_exposure",
                    description="Adjust exposure settings for photos",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "photo_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of photo IDs to adjust"
                            },
                            "exposure_ev": {
                                "type": "number",
                                "minimum": -5.0,
                                "maximum": 5.0,
                                "description": "Exposure adjustment in EV stops"
                            },
                            "filter": {
                                "type": "string",
                                "description": "Filter criteria to select photos"
                            }
                        },
                        "required": ["exposure_ev"]
                    }
                ),
                Tool(
                    name="apply_preset",
                    description="Apply editing presets to photos",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "photo_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of photo IDs"
                            },
                            "preset_name": {
                                "type": "string",
                                "description": "Name of preset to apply"
                            },
                            "filter": {
                                "type": "string",
                                "description": "Filter criteria to select photos"
                            }
                        },
                        "required": ["preset_name"]
                    }
                ),
                Tool(
                    name="export_images",
                    description="Export photos to various formats",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "photo_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of photo IDs to export"
                            },
                            "output_path": {
                                "type": "string",
                                "description": "Output directory path"
                            },
                            "format": {
                                "type": "string",
                                "enum": ["jpeg", "png", "tiff"],
                                "description": "Export format"
                            },
                            "quality": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "description": "Export quality (1-100)"
                            }
                        },
                        "required": ["output_path", "format"]
                    }
                )
            ]

        @self.app.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            """Handle tool calls."""
            try:
                if name == "view_photos":
                    return await self._handle_view_photos(arguments)
                elif name == "rate_photos":
                    return await self._handle_rate_photos(arguments)
                elif name == "import_batch":
                    return await self._handle_import_batch(arguments)
                elif name == "adjust_exposure":
                    return await self._handle_adjust_exposure(arguments)
                elif name == "apply_preset":
                    return await self._handle_apply_preset(arguments)
                elif name == "export_images":
                    return await self._handle_export_images(arguments)
                else:
                    return [TextContent(
                        type="text",
                        text=f"Unknown tool: {name}"
                    )]

            except Exception as e:
                logger.error(f"Tool {name} failed: {e}")
                return [TextContent(
                    type="text",
                    text=f"Tool {name} failed: {str(e)}"
                )]

    def list_tools(self) -> List[str]:
        """Get list of available tool names."""
        return [
            "view_photos",
            "rate_photos",
            "import_batch",
            "adjust_exposure",
            "apply_preset",
            "export_images"
        ]

    async def _handle_view_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
        """Handle view_photos tool call."""
        return [TextContent(
            type="text",
            text="view_photos tool called - implementation pending"
        )]

    async def _handle_rate_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
        """Handle rate_photos tool call."""
        return [TextContent(
            type="text",
            text="rate_photos tool called - implementation pending"
        )]

    async def _handle_import_batch(self, arguments: Dict[str, Any]) -> List[TextContent]:
        """Handle import_batch tool call."""
        return [TextContent(
            type="text",
            text="import_batch tool called - implementation pending"
        )]

    async def _handle_adjust_exposure(self, arguments: Dict[str, Any]) -> List[TextContent]:
        """Handle adjust_exposure tool call."""
        return [TextContent(
            type="text",
            text="adjust_exposure tool called - implementation pending"
        )]

    async def _handle_apply_preset(self, arguments: Dict[str, Any]) -> List[TextContent]:
        """Handle apply_preset tool call."""
        return [TextContent(
            type="text",
            text="apply_preset tool called - implementation pending"
        )]

    async def _handle_export_images(self, arguments: Dict[str, Any]) -> List[TextContent]:
        """Handle export_images tool call."""
        return [TextContent(
            type="text",
            text="export_images tool called - implementation pending"
        )]

    async def start(self) -> None:
        """Start the MCP server via stdio."""
        await stdio_server(self.app)

    async def run(self, host: str = "localhost", port: int = 3000) -> None:
        """Run the MCP server."""
        logger.info(f"Starting Darktable MCP Server on {host}:{port}")
        await self.start()