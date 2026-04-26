"""Main MCP server for darktable integration."""

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .darktable.cli_wrapper import CLIWrapper
from .tools.photo_tools import PhotoTools
from .tools.preview_tools import (
    apply_ratings_batch,
    extract_previews,
    format_extract_summary,
    format_ratings_summary,
)
from .utils.errors import DarktableMCPError

logger = logging.getLogger(__name__)

ToolHandler = Callable[[Dict[str, Any]], Awaitable[List[TextContent]]]


class DarktableMCPServer:
    """MCP server for darktable photo management and editing."""

    def __init__(self) -> None:
        self.app: Server = Server("darktable-mcp")
        self._cli: Optional[CLIWrapper] = None
        self._photo_tools: Optional[PhotoTools] = None
        self._handler_map: Dict[str, ToolHandler] = self._build_handlers()
        self._setup_tools()

    @property
    def cli(self) -> CLIWrapper:
        """Get CLI wrapper instance (lazy-loaded)."""
        if self._cli is None:
            self._cli = CLIWrapper()
        return self._cli

    @property
    def photo_tools(self) -> PhotoTools:
        """Get photo tools instance (lazy-loaded)."""
        if self._photo_tools is None:
            self._photo_tools = PhotoTools()
        return self._photo_tools

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
                description="Browse photos in your darktable library with filtering/rating",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "string",
                            "description": "Filter photos by filename",
                        },
                        "rating_min": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "Minimum star rating",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "default": 100,
                            "description": "Max photos to return",
                        },
                    },
                },
            ),
            Tool(
                name="rate_photos",
                description="Apply star ratings to photos in darktable library",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of photo IDs to rate",
                        },
                        "rating": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "Star rating to apply",
                        },
                    },
                    "required": ["photo_ids", "rating"],
                },
            ),
            Tool(
                name="import_batch",
                description="Import photos from directories into darktable library",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_path": {
                            "type": "string",
                            "description": "Path to directory containing photos",
                        },
                        "recursive": {
                            "type": "boolean",
                            "default": False,
                            "description": "Import from subdirectories",
                        },
                    },
                    "required": ["source_path"],
                },
            ),
            Tool(
                name="import_from_camera",
                description=(
                    "Use when a camera or memory card is physically connected. "
                    "Detects the camera via libgphoto2 and copies all photos "
                    "to a local directory. Returns the destination path so the "
                    "user can open darktable and choose 'import folder' to "
                    "register the photos in their library."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": (
                                "Target directory for copied files. "
                                "Default: ~/Pictures/import-YYYY-MM-DD/"
                            ),
                        },
                        "camera_port": {
                            "type": "string",
                            "description": (
                                "gphoto2 port string (e.g. 'usb:002,002'). "
                                "Required when multiple cameras are connected."
                            ),
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 60,
                            "description": (
                                "Subprocess timeout for the transfer. "
                                "Default: 3600 (1 hour). On timeout, re-run "
                                "the tool to resume — already-copied files "
                                "are skipped."
                            ),
                        },
                    },
                },
            ),
            Tool(
                name="adjust_exposure",
                description="Adjust exposure settings for photos (opens darktable GUI for preview)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of photo IDs to adjust",
                        },
                        "exposure_ev": {
                            "type": "number",
                            "minimum": -5.0,
                            "maximum": 5.0,
                            "description": "Exposure adjustment in EV",
                        },
                    },
                    "required": ["photo_ids", "exposure_ev"],
                },
            ),
            Tool(
                name="apply_preset",
                description="Apply editing presets to photos (not yet implemented)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "preset_name": {"type": "string"},
                        "filter": {"type": "string"},
                    },
                    "required": ["preset_name"],
                },
            ),
            Tool(
                name="extract_previews",
                description=(
                    "Extract auto-rotated JPEG previews from a directory of "
                    "raw files (NEF/CR2/ARW/DNG/etc) for vision-based rating. "
                    "Each preview is rotated upright via EXIF orientation and "
                    "resized to max_dim (default 1024). A smaller thumb_dim "
                    "(default 384) is also written for token-efficient "
                    "first-pass culling. Returns a list of items with preview "
                    "paths plus an EXIF summary (ISO, shutter, focal, "
                    "aperture, datetime) per file."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_dir": {
                            "type": "string",
                            "description": "Directory containing raw files",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": (
                                "Where to write JPEGs. "
                                "Default: <source_dir>/.previews/"
                            ),
                        },
                        "max_dim": {
                            "type": "integer",
                            "minimum": 256,
                            "maximum": 4096,
                            "default": 1024,
                            "description": "Longest-edge for the standard preview",
                        },
                        "thumb_dim": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 1024,
                            "default": 384,
                            "description": "Thumb longest-edge; 0 to skip",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "default": False,
                            "description": "Re-extract even if preview exists",
                        },
                    },
                    "required": ["source_dir"],
                },
            ),
            Tool(
                name="apply_ratings_batch",
                description=(
                    "Write XMP sidecars (xmp:Rating) for a batch of "
                    "{stem: rating} pairs. Sidecars sit next to the raw "
                    "files at <source_dir>/<stem>.<RAW_EXT>.xmp and are "
                    "picked up automatically by darktable on import. "
                    "Rating range: -1 (reject), 0 (unrated), 1-5 (stars). "
                    "Each rating is also appended to "
                    "<source_dir>/ratings.jsonl for replay/audit."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_dir": {
                            "type": "string",
                            "description": "Directory holding the raw files",
                        },
                        "ratings": {
                            "type": "object",
                            "description": (
                                "Map of file stem (e.g. 'DSC_1234') to "
                                "rating int in [-1, 5]"
                            ),
                            "additionalProperties": {
                                "type": "integer",
                                "minimum": -1,
                                "maximum": 5,
                            },
                        },
                        "log": {
                            "type": "boolean",
                            "default": True,
                            "description": "Append entries to ratings.jsonl",
                        },
                    },
                    "required": ["source_dir", "ratings"],
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
                        "format": {
                            "type": "string",
                            "enum": ["jpeg", "png", "tiff"],
                        },
                        "quality": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["photo_ids", "output_path", "format"],
                },
            ),
        ]

    def _build_handlers(self) -> Dict[str, ToolHandler]:
        return {
            "view_photos": self._handle_view_photos,
            "rate_photos": self._handle_rate_photos,
            "import_batch": self._handle_import_batch,
            "import_from_camera": self._handle_import_from_camera,
            "adjust_exposure": self._handle_adjust_exposure,
            "apply_preset": self._not_implemented("apply_preset"),
            "export_images": self._handle_export_images,
            "extract_previews": self._handle_extract_previews,
            "apply_ratings_batch": self._handle_apply_ratings_batch,
        }

    def list_tools(self) -> List[str]:
        """Tool names registered with the server (used by tests/introspection)."""
        return list(self._handler_map.keys())

    @staticmethod
    def _not_implemented(name: str) -> ToolHandler:
        async def handler(_: Dict[str, Any]) -> List[TextContent]:
            return [
                TextContent(
                    type="text",
                    text=f"{name} is not yet implemented.",
                )
            ]

        return handler

    async def _handle_view_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            photos = self.photo_tools.view_photos(arguments)
            if not photos:
                return [
                    TextContent(
                        type="text",
                        text="No photos found matching criteria",
                    )
                ]

            # Format results nicely
            result_lines = [f"Found {len(photos)} photos:"]
            for photo in photos:
                rating_stars = "⭐" * photo.get("rating", 0)
                result_lines.append(
                    f"ID: {photo['id']} | {photo['filename']} | Rating: {rating_stars}"
                )

            return [TextContent(type="text", text="\n".join(result_lines))]
        except Exception as e:
            logger.error("view_photos failed: %s", e)
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _handle_rate_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.photo_tools.rate_photos(arguments)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            logger.error("rate_photos failed: %s", e)
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _handle_import_batch(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.photo_tools.import_batch(arguments)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            logger.error("import_batch failed: %s", e)
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _handle_import_from_camera(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.photo_tools.import_from_camera(arguments)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            logger.error("import_from_camera failed: %s", e)
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _handle_adjust_exposure(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.photo_tools.adjust_exposure(arguments)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            logger.error("adjust_exposure failed: %s", e)
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _handle_export_images(self, arguments: Dict[str, Any]) -> List[TextContent]:
        photo_ids = arguments.get("photo_ids") or []
        output_path = arguments.get("output_path")
        format_type = arguments.get("format", "jpeg")
        quality = int(arguments.get("quality", 95))

        if not output_path:
            return [TextContent(type="text", text="output_path is required")]
        if not photo_ids:
            return [
                TextContent(
                    type="text",
                    text="photo_ids must contain at least one path",
                )
            ]

        input_files = [Path(p) for p in photo_ids]
        results = self.cli.batch_export(
            input_files=input_files,
            output_dir=Path(output_path),
            format_type=format_type,
            quality=quality,
        )
        body = "\n".join(f"{src}: {status}" for src, status in results.items())
        return [TextContent(type="text", text=body or "No files processed")]

    async def _handle_extract_previews(self, arguments: Dict[str, Any]) -> List[TextContent]:
        source_dir = arguments.get("source_dir")
        if not source_dir:
            return [TextContent(type="text", text="source_dir is required")]
        result = extract_previews(
            source_dir=source_dir,
            output_dir=arguments.get("output_dir"),
            max_dim=int(arguments.get("max_dim", 1024)),
            thumb_dim=int(arguments.get("thumb_dim", 384)),
            overwrite=bool(arguments.get("overwrite", False)),
        )
        body = format_extract_summary(result) + "\n\n" + json.dumps(result["items"], indent=2)
        return [TextContent(type="text", text=body)]

    async def _handle_apply_ratings_batch(self, arguments: Dict[str, Any]) -> List[TextContent]:
        source_dir = arguments.get("source_dir")
        ratings = arguments.get("ratings") or {}
        if not source_dir:
            return [TextContent(type="text", text="source_dir is required")]
        if not ratings:
            return [TextContent(type="text", text="ratings must be a non-empty map")]
        result = apply_ratings_batch(
            source_dir=source_dir,
            ratings=ratings,
            log=bool(arguments.get("log", True)),
        )
        return [TextContent(type="text", text=format_ratings_summary(result))]

    async def start(self) -> None:
        """Run the MCP server over stdio."""
        async with stdio_server() as (read_stream, write_stream):
            await self.app.run(
                read_stream,
                write_stream,
                self.app.create_initialization_options(),
            )

    async def run(self) -> None:
        """Run the MCP server using stdio transport."""
        logger.info("Starting Darktable MCP Server (stdio)")
        await self.start()
