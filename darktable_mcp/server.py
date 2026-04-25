"""Main MCP server for darktable integration."""

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .darktable.cli_wrapper import CLIWrapper
from .darktable.library_db import LibraryDB
from .utils.errors import DarktableMCPError

logger = logging.getLogger(__name__)

ToolHandler = Callable[[Dict[str, Any]], Awaitable[List[TextContent]]]


class DarktableMCPServer:
    """MCP server for darktable photo management and editing."""

    def __init__(self) -> None:
        self.app: Server = Server("darktable-mcp")
        self._cli: Optional[CLIWrapper] = None
        self._library: Optional[LibraryDB] = None
        self._handler_map: Dict[str, ToolHandler] = self._build_handlers()
        self._setup_tools()

    @property
    def cli(self) -> CLIWrapper:
        if self._cli is None:
            self._cli = CLIWrapper()
        return self._cli

    @property
    def library(self) -> LibraryDB:
        if self._library is None:
            self._library = LibraryDB()
        return self._library

    def _setup_tools(self) -> None:
        @self.app.list_tools()
        async def list_tools() -> List[Tool]:
            return self._tool_definitions()

        @self.app.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            handler = self._handler_map.get(name)
            if handler is None:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
            try:
                return await handler(arguments)
            except DarktableMCPError as e:
                logger.error("Tool %s failed: %s", name, e)
                return [TextContent(type="text", text=f"Error: {e}")]
            except Exception as e:
                logger.exception("Tool %s crashed", name)
                return [TextContent(type="text", text=f"Tool {name} crashed: {e}")]

    def _tool_definitions(self) -> List[Tool]:
        return [
            Tool(
                name="view_photos",
                description=(
                    "Browse darktable's library with optional filters. "
                    "Returns id, rating (-1 = rejected, 0–5 = stars), and path."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "string",
                            "description": "Substring matched against folder or filename",
                        },
                        "rating_min": {"type": "integer", "minimum": 1, "maximum": 5},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                },
            ),
            Tool(
                name="rate_photos",
                description="Apply star ratings to photos (not yet implemented)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {"type": "array", "items": {"type": "string"}},
                        "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                        "filter": {"type": "string"},
                    },
                    "required": ["rating"],
                },
            ),
            Tool(
                name="import_batch",
                description="Import photos from directories (not yet implemented)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_path": {"type": "string"},
                        "recursive": {"type": "boolean"},
                        "copy": {"type": "boolean"},
                    },
                    "required": ["source_path"],
                },
            ),
            Tool(
                name="adjust_exposure",
                description="Adjust exposure settings for photos (not yet implemented)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {"type": "array", "items": {"type": "string"}},
                        "exposure_ev": {"type": "number", "minimum": -5.0, "maximum": 5.0},
                        "filter": {"type": "string"},
                    },
                    "required": ["exposure_ev"],
                },
            ),
            Tool(
                name="apply_preset",
                description="Apply editing presets to photos (not yet implemented)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {"type": "array", "items": {"type": "string"}},
                        "preset_name": {"type": "string"},
                        "filter": {"type": "string"},
                    },
                    "required": ["preset_name"],
                },
            ),
            Tool(
                name="export_images",
                description=(
                    "Export photos to JPEG/PNG/TIFF via darktable-cli. "
                    "Pass absolute file paths in photo_ids."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Absolute paths to source images",
                        },
                        "output_path": {"type": "string"},
                        "format": {"type": "string", "enum": ["jpeg", "png", "tiff"]},
                        "quality": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["photo_ids", "output_path", "format"],
                },
            ),
        ]

    def _build_handlers(self) -> Dict[str, ToolHandler]:
        return {
            "view_photos": self._handle_view_photos,
            "rate_photos": self._not_implemented("rate_photos"),
            "import_batch": self._not_implemented("import_batch"),
            "adjust_exposure": self._not_implemented("adjust_exposure"),
            "apply_preset": self._not_implemented("apply_preset"),
            "export_images": self._handle_export_images,
        }

    def list_tools(self) -> List[str]:
        """Tool names registered with the server (used by tests/introspection)."""
        return list(self._handler_map.keys())

    @staticmethod
    def _not_implemented(name: str) -> ToolHandler:
        async def handler(_: Dict[str, Any]) -> List[TextContent]:
            return [TextContent(
                type="text",
                text=f"{name} is not yet implemented.",
            )]
        return handler

    async def _handle_view_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
        rating_min = arguments.get("rating_min")
        filter_text = arguments.get("filter")
        limit = int(arguments.get("limit", 50))

        rows = self.library.view_photos(
            rating_min=rating_min,
            filter_text=filter_text,
            limit=limit,
        )
        if not rows:
            return [TextContent(type="text", text="No photos found.")]
        body = "\n".join(f"#{r.id}\trating={r.rating:>2}\t{r.path}" for r in rows)
        return [TextContent(type="text", text=body)]

    async def _handle_export_images(self, arguments: Dict[str, Any]) -> List[TextContent]:
        photo_ids = arguments.get("photo_ids") or []
        output_path = arguments.get("output_path")
        format_type = arguments.get("format", "jpeg")
        quality = int(arguments.get("quality", 95))

        if not output_path:
            return [TextContent(type="text", text="output_path is required")]
        if not photo_ids:
            return [TextContent(type="text", text="photo_ids must contain at least one path")]

        input_files = [Path(p) for p in photo_ids]
        results = self.cli.batch_export(
            input_files=input_files,
            output_dir=Path(output_path),
            format_type=format_type,
            quality=quality,
        )
        body = "\n".join(f"{src}: {status}" for src, status in results.items())
        return [TextContent(type="text", text=body or "No files processed")]

    async def start(self) -> None:
        """Run the MCP server over stdio."""
        async with stdio_server() as (read_stream, write_stream):
            await self.app.run(
                read_stream,
                write_stream,
                self.app.create_initialization_options(),
            )

    async def run(self) -> None:
        logger.info("Starting Darktable MCP Server (stdio)")
        await self.start()
