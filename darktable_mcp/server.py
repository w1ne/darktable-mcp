"""Main MCP server for darktable integration."""

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .bridge.client import (
    Bridge,
    BridgeError,
    BridgePluginNotInstalledError,
    BridgeTimeoutError,
)
from .darktable.cli_wrapper import CLIWrapper
from .tools.camera_tools import CameraTools
from .tools.preview_tools import (
    apply_ratings_batch,
    extract_previews,
    format_extract_summary,
    format_open_summary,
    format_ratings_summary,
    open_in_darktable,
)
from .utils.errors import DarktableMCPError

logger = logging.getLogger(__name__)

ToolHandler = Callable[[Dict[str, Any]], Awaitable[List[TextContent]]]


class DarktableMCPServer:
    """MCP server for darktable photo management and editing."""

    def __init__(self) -> None:
        self.app: Server = Server("darktable-mcp")
        self._cli: Optional[CLIWrapper] = None
        self.camera_tools = CameraTools()
        self.bridge = Bridge()
        self._handler_map: Dict[str, ToolHandler] = self._build_handlers()
        self._setup_tools()

    @property
    def cli(self) -> CLIWrapper:
        """Get CLI wrapper instance (lazy-loaded)."""
        if self._cli is None:
            self._cli = CLIWrapper()
        return self._cli

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
                    "Browse photos in the user's darktable library. Filter by "
                    "filename substring, minimum star rating, or both. Returns "
                    "id, filename, absolute file path, and rating per match — "
                    "the path can be passed straight into export_images. "
                    "Requires darktable to be running with the darktable-mcp "
                    "Lua plugin installed (see darktable-mcp install-plugin)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "string",
                            "description": "Substring filter on filename (case-insensitive)",
                        },
                        "rating_min": {
                            "type": "integer",
                            "minimum": -1,
                            "maximum": 5,
                            "description": "Minimum star rating to include",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "default": 100,
                            "description": "Maximum number of photos to return",
                        },
                    },
                },
            ),
            Tool(
                name="rate_photos",
                description=(
                    "Apply a star rating to one or more photos in the user's "
                    "darktable library. Requires darktable to be running with "
                    "the darktable-mcp Lua plugin installed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of photo IDs (from view_photos)",
                        },
                        "rating": {
                            "type": "integer",
                            "minimum": -1,
                            "maximum": 5,
                            "description": "Star rating: -1=reject, 0=unrated, 1-5=stars",
                        },
                    },
                    "required": ["photo_ids", "rating"],
                },
            ),
            Tool(
                name="import_batch",
                description=(
                    "Register a folder as a film roll in the user's darktable "
                    "library. Useful when you've copied photos from a card or "
                    "external drive and want darktable to know about them. "
                    "Returns the count of newly-imported photos. Requires "
                    "darktable to be running with the darktable-mcp Lua plugin "
                    "installed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_path": {
                            "type": "string",
                            "description": "Absolute path to the folder of photos to import",
                        },
                        "recursive": {
                            "type": "boolean",
                            "default": True,
                            "description": "Recurse into subdirectories (default true)",
                        },
                    },
                    "required": ["source_path"],
                },
            ),
            Tool(
                name="list_styles",
                description=(
                    "List all darktable styles (presets) installed on the user's "
                    "system. Returns name and description for each. Required "
                    "discovery step before calling apply_preset, since style names "
                    "must match exactly. Requires darktable to be running with the "
                    "darktable-mcp Lua plugin installed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="apply_preset",
                description=(
                    "Apply a darktable style (preset) to one or more photos. The "
                    "preset_name must exactly match a style name from list_styles. "
                    "Returns counts of applied and missed photos. Requires "
                    "darktable to be running with the darktable-mcp Lua plugin "
                    "installed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "photo_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Photo IDs (from view_photos)",
                        },
                        "preset_name": {
                            "type": "string",
                            "description": "Style name (must match exactly; see list_styles)",
                        },
                    },
                    "required": ["photo_ids", "preset_name"],
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
                name="open_in_darktable",
                description=(
                    "Launch the darktable GUI on a folder. The folder is "
                    "registered as a film roll on first launch and XMP "
                    "sidecars are picked up automatically. The lighttable "
                    "opens already filtered via the official "
                    "`darktable.gui.libs.collect.filter` Lua API for any "
                    "rating spec: exact `rating=N`, `rating_min=N` (>=), "
                    "`rating_max=N` (<=), arbitrary `rating_min..rating_max` "
                    "inner ranges, or no filter at all."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_dir": {
                            "type": "string",
                            "description": "Folder containing the raw files",
                        },
                        "rating": {
                            "type": "integer",
                            "minimum": -1,
                            "maximum": 5,
                            "description": (
                                "Filter to exactly this rating "
                                "(-1=reject, 0=unrated, 1-5=stars)"
                            ),
                        },
                        "rating_min": {
                            "type": "integer",
                            "minimum": -1,
                            "maximum": 5,
                            "description": "Lower bound of a rating range",
                        },
                        "rating_max": {
                            "type": "integer",
                            "minimum": -1,
                            "maximum": 5,
                            "description": "Upper bound of a rating range",
                        },
                        "darktable_path": {
                            "type": "string",
                            "default": "darktable",
                            "description": "darktable executable (default: 'darktable' on PATH)",
                        },
                    },
                    "required": ["source_dir"],
                },
            ),
            Tool(
                name="export_images",
                description=(
                    "Export photos to JPEG/PNG/TIFF via darktable-cli. "
                    "Pass absolute file paths in photo_ids — the `path` field "
                    "from view_photos drops in directly."
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
            "import_from_camera": self._handle_import_from_camera,
            "export_images": self._handle_export_images,
            "extract_previews": self._handle_extract_previews,
            "apply_ratings_batch": self._handle_apply_ratings_batch,
            "open_in_darktable": self._handle_open_in_darktable,
            "view_photos": self._handle_view_photos,
            "rate_photos": self._handle_rate_photos,
            "import_batch": self._handle_import_batch,
            "list_styles": self._handle_list_styles,
            "apply_preset": self._handle_apply_preset,
        }

    def list_tools(self) -> List[str]:
        """Tool names registered with the server (used by tests/introspection)."""
        return list(self._handler_map.keys())

    async def _handle_import_from_camera(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.camera_tools.import_from_camera(arguments)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            logger.error("import_from_camera failed: %s", e)
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
        out_dir = Path(output_path)
        results = self.cli.batch_export(
            input_files=input_files,
            output_dir=out_dir,
            format_type=format_type,
            quality=quality,
        )
        # Stash per-file results in a JSONL side file. The full per-file map
        # blew Claude's token budget at 400+ files, so the response stays
        # short and the agent reads the side file when it actually wants
        # details.
        side_file = out_dir / ".export_images.jsonl"
        out_dir.mkdir(parents=True, exist_ok=True)
        ok = fail = 0
        first_error: Optional[str] = None
        with side_file.open("w") as fh:
            for src, status in results.items():
                is_error = status.startswith("Error") or "failed" in status.lower()
                fh.write(json.dumps({"input": src, "status": status}) + "\n")
                if is_error:
                    fail += 1
                    if first_error is None:
                        first_error = f"{src}: {status[:200]}"
                else:
                    ok += 1
        summary = [
            f"exported: {ok}, failed: {fail}",
            f"output_dir: {out_dir}",
            f"details: {side_file} (JSONL, one line per file: input, status)",
        ]
        if first_error:
            summary.append(f"first error: {first_error}")
        return [TextContent(type="text", text="\n".join(summary))]

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
        return [TextContent(type="text", text=format_extract_summary(result))]

    async def _handle_open_in_darktable(self, arguments: Dict[str, Any]) -> List[TextContent]:
        source_dir = arguments.get("source_dir")
        if not source_dir:
            return [TextContent(type="text", text="source_dir is required")]
        result = open_in_darktable(
            source_dir=source_dir,
            rating=arguments.get("rating"),
            rating_min=arguments.get("rating_min"),
            rating_max=arguments.get("rating_max"),
            darktable_path=arguments.get("darktable_path", "darktable"),
        )
        return [TextContent(type="text", text=format_open_summary(result))]

    async def _handle_view_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            photos = self.bridge.call("view_photos", arguments)
        except BridgePluginNotInstalledError:
            return [TextContent(
                type="text",
                text="darktable-mcp plugin not installed. Run: darktable-mcp install-plugin",
            )]
        except BridgeTimeoutError:
            return [TextContent(
                type="text",
                text="darktable not running, or plugin not loaded. Open darktable and try again.",
            )]
        except BridgeError as e:
            return [TextContent(type="text", text=f"Plugin error: {e}")]

        if not photos:
            return [TextContent(type="text", text="No photos found matching criteria")]
        # Surface the absolute file path so the agent can hand it straight to
        # export_images (which takes file paths in `photo_ids`). Without this
        # the two tools don't compose: view_photos returns IDs, export wants
        # paths, and the agent has no way to bridge the two.
        lines = [f"Found {len(photos)} photos:"]
        for p in photos:
            stars = "⭐" * (p.get("rating") or 0)
            path = p.get("path") or ""
            lines.append(f"ID: {p['id']} | {p['filename']} | Rating: {stars} | {path}")
        return [TextContent(type="text", text="\n".join(lines))]

    async def _handle_rate_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.bridge.call("rate_photos", arguments)
        except BridgePluginNotInstalledError:
            return [TextContent(
                type="text",
                text="darktable-mcp plugin not installed. Run: darktable-mcp install-plugin",
            )]
        except BridgeTimeoutError:
            return [TextContent(
                type="text",
                text="darktable not running, or plugin not loaded. Open darktable and try again.",
            )]
        except BridgeError as e:
            return [TextContent(type="text", text=f"Plugin error: {e}")]

        updated = result.get("updated", 0)
        return [TextContent(
            type="text",
            text=f"Updated {updated} photos with {arguments.get('rating')} stars",
        )]

    async def _handle_import_batch(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.bridge.call("import_batch", arguments)
        except BridgePluginNotInstalledError:
            return [TextContent(
                type="text",
                text="darktable-mcp plugin not installed. Run: darktable-mcp install-plugin",
            )]
        except BridgeTimeoutError:
            return [TextContent(
                type="text",
                text="darktable not running, or plugin not loaded. Open darktable and try again.",
            )]
        except BridgeError as e:
            return [TextContent(type="text", text=f"Plugin error: {e}")]

        imported = result.get("imported", 0)
        src = result.get("source_path", arguments.get("source_path", "?"))
        return [TextContent(
            type="text",
            text=f"Imported {imported} photos from {src}",
        )]

    async def _handle_list_styles(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.bridge.call("list_styles", arguments)
        except BridgePluginNotInstalledError:
            return [TextContent(
                type="text",
                text="darktable-mcp plugin not installed. Run: darktable-mcp install-plugin",
            )]
        except BridgeTimeoutError:
            return [TextContent(
                type="text",
                text="darktable not running, or plugin not loaded. Open darktable and try again.",
            )]
        except BridgeError as e:
            return [TextContent(type="text", text=f"Plugin error: {e}")]

        styles = result.get("styles", [])
        count = result.get("count", len(styles))
        if count == 0:
            return [TextContent(type="text", text="No styles installed.")]
        # Show count + first 50 names, with a hint if there are more.
        lines = [f"{count} styles installed:"]
        for s in styles[:50]:
            desc = s.get("description") or ""
            if desc:
                lines.append(f"  {s['name']} — {desc}")
            else:
                lines.append(f"  {s['name']}")
        if count > 50:
            lines.append(f"  ... and {count - 50} more (full list available; this is the first 50)")
        return [TextContent(type="text", text="\n".join(lines))]

    async def _handle_apply_preset(self, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = self.bridge.call("apply_preset", arguments)
        except BridgePluginNotInstalledError:
            return [TextContent(
                type="text",
                text="darktable-mcp plugin not installed. Run: darktable-mcp install-plugin",
            )]
        except BridgeTimeoutError:
            return [TextContent(
                type="text",
                text="darktable not running, or plugin not loaded. Open darktable and try again.",
            )]
        except BridgeError as e:
            return [TextContent(type="text", text=f"Plugin error: {e}")]

        applied = result.get("applied", 0)
        missed = result.get("missed", [])
        name = result.get("preset_name", arguments.get("preset_name", "?"))
        parts = [f"Applied '{name}' to {applied} photo(s)"]
        if missed:
            parts.append(f"Missed (image not in library): {', '.join(missed)}")
        return [TextContent(type="text", text="\n".join(parts))]

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
