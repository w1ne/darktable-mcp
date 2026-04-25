# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes a small set of darktable
operations to MCP clients (e.g. Claude Desktop, Claude Code). The AI lives
in the client; this server drives darktable.

## Status

**Production ready for headless operations!** All core library tools are now implemented using darktable's headless Lua API.

**Implemented tools:**
- `view_photos` - Browse your darktable library with filtering and rating options (headless)
- `rate_photos` - Apply star ratings to photos in your library (headless) 
- `import_batch` - Import photos from directories (headless)
- `adjust_exposure` - Adjust exposure with live preview (opens darktable GUI)
- `export_images` - Export photos to JPEG/PNG/TIFF via darktable-cli

**Not yet implemented:**
- `apply_preset` - Apply editing presets to photos (planned for next release)

The headless integration uses darktable's native Lua API via `require("darktable")` to provide fast, invisible library operations while respecting all design principles.

## Design rules (no compromise)

1. **Use the provided darktable APIs only.** `darktable-cli` for export;
   the official Lua API for everything else. Do not read or write
   `library.db` directly under any circumstances.
2. **Tools that return data to the AI must be headless.** Spawning the
   darktable GUI to answer a query is unacceptable.
3. **GUI is only acceptable when the tool's purpose is to show the human
   user something** in the darktable editor. No such tool exists yet.
   When one is added, it is the only thing that may launch the GUI.

### Why some tools are parked

`darktable-cli` deliberately does not load the user's library, so it
cannot browse it. `darktable --lua` brings up the full GUI. That means
there is no headless, official-API path today for library reads or
writes. The honest answer is to not ship those tools yet.

The planned unblocker is a long-running darktable instance running a Lua
plugin that exposes a small RPC (e.g. unix socket); the MCP server talks
to that plugin. That keeps every rule above intact — official API,
headless after the user already has darktable open, no DB poking.

## Installation

```bash
pip install darktable-mcp
```

You also need `darktable` (with `darktable-cli`) installed and available
on your `PATH`.

**Quick test:** After installation, try:
```bash
# In Claude Desktop, you can now ask:
# "Show me my recent photos" (uses view_photos)
# "Rate these photos 4 stars" (uses rate_photos) 
# "Import photos from ~/Pictures/vacation" (uses import_batch)
```

**Note:** First run will auto-detect your darktable library location. Make sure you've opened darktable and imported some photos before using the MCP server.

## Configuration

Add to your Claude Desktop config:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "darktable": {
      "command": "darktable-mcp"
    }
  }
}
```

## Implemented tools

- `view_photos(filter?, rating_min?, limit?)` — Browse photos in your darktable library. Filter by filename, minimum rating, or limit results.
- `rate_photos(photo_ids, rating)` — Apply 1-5 star ratings to specific photos by ID.
- `import_batch(source_path, recursive?)` — Import photos from directories into your darktable library.
- `adjust_exposure(photo_ids, exposure_ev)` — Adjust exposure settings for photos. Opens darktable GUI to show preview.
- `export_images(photo_ids, output_path, format, quality?)` — Export photos to JPEG/PNG/TIFF via darktable-cli.

## Stubbed tools (registered, not implemented)

- `apply_preset(photo_ids, preset_name)` — Apply editing presets to photos. Planned for next release.

## Requirements

- Python 3.8+
- darktable 4.0+ (with `darktable-cli` on `PATH`)
- An MCP-compatible client (Claude Desktop, Claude Code, etc.)

## Troubleshooting

**"darktable setup error"** — Ensure darktable is installed and you've opened it at least once to create the library database.

**"Failed to parse photo data"** — Your darktable library may be corrupted or in an unexpected format. Try opening darktable directly to verify it works.

**"Library not found"** — The auto-detector couldn't find your darktable library. Common locations:
- Linux: `~/.config/darktable/library.db`
- macOS: `~/Library/Application Support/darktable/library.db`  
- Windows: `%APPDATA%\Local\darktable\library.db`

If your library is in a custom location, please open an issue.

**Tool timeouts** — Some operations may take longer on large libraries. This is normal for the first run or after major library changes.

## Contributing

Contributions welcome. The most useful starting point is the long-running
Lua-plugin + IPC integration that unblocks every parked tool. Any change
that reads or writes `library.db` directly will be rejected.

## License

MIT — see `LICENSE`.

## Keywords

darktable, MCP, model context protocol, photo editing, RAW processing,
batch processing, photography workflow, Claude
