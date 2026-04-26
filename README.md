# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes a small set of darktable
operations to MCP clients (e.g. Claude Desktop, Claude Code). The AI lives
in the client; this server drives darktable.

## Status

**Implemented tools:**

Library operations (headless, via darktable Lua API):
- `view_photos` — Browse your darktable library with filtering and rating options
- `rate_photos` — Apply star ratings to photos already in your library
- `import_batch` — Import photos from a directory into the library
- `import_from_camera` — Detect a connected camera (libgphoto2) and copy photos to a local directory

Vision-rating workflow (headless, file-based — no library required):
- `extract_previews` — Pull auto-rotated JPEG previews out of raw files (NEF/CR2/ARW/DNG/etc), with a tiered thumb pass and an EXIF summary per file. Designed for token-efficient visual rating loops in MCP clients.
- `apply_ratings_batch` — Write XMP sidecars (`xmp:Rating`) for a `{stem: rating}` batch, plus an append-only `ratings.jsonl` log so the history survives session resets.
- `open_in_darktable` — Launch the darktable GUI on a folder. The folder registers as a film roll on first launch and the XMP sidecars are picked up automatically. Optional `rating` / `rating_min` / `rating_max` arguments are returned as a filter *hint* (with star label) — they don't pre-apply the lighttable filter, since darktable's filter-rule string format is too version-specific to set safely from a launcher.

GUI / export:
- `adjust_exposure` — Adjust exposure settings (opens darktable GUI for preview)
- `export_images` — Export photos to JPEG/PNG/TIFF via `darktable-cli`

**Not yet implemented:**
- `apply_preset` — Apply editing presets to photos (planned)

The vision-rating tools require an optional `[vision]` install (rawpy / Pillow / pyexiv2) so the base install stays light.

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
# Base install — library, import, export, GUI tools.
pip install darktable-mcp

# Optional: vision-rating workflow (extract_previews, apply_ratings_batch).
pip install 'darktable-mcp[vision]'
```

You also need `darktable` (with `darktable-cli`) installed and available
on your `PATH`. The `[vision]` extra pulls in `rawpy`, `Pillow`, and
`pyexiv2`, which in turn need `libraw` and `libexiv2` system packages.

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

Library:
- `view_photos(filter?, rating_min?, limit?)` — Browse photos in your darktable library. Filter by filename, minimum rating, or limit results.
- `rate_photos(photo_ids, rating)` — Apply 1-5 star ratings to specific photos by ID.
- `import_batch(source_path, recursive?)` — Import photos from a directory into the library.
- `import_from_camera(destination?, camera_port?, timeout_seconds?)` — Detect a connected camera via libgphoto2 and copy all photos to a local directory. Returns the destination path.

Vision-rating workflow:
- `extract_previews(source_dir, output_dir?, max_dim?, thumb_dim?, overwrite?)` — Extract embedded JPEG previews from raws, auto-rotate via EXIF orientation, and write a tiered preview (default 1024px) plus a small first-pass thumb (default 384px). Returns per-file paths and an EXIF summary (ISO, shutter, focal, aperture, datetime).
- `apply_ratings_batch(source_dir, ratings, log?)` — Write XMP sidecars for a `{stem: rating}` batch (range -1..5, -1 = reject). Appends each rating to `<source_dir>/ratings.jsonl` for replay/audit.
- `open_in_darktable(source_dir, rating?, rating_min?, rating_max?)` — Launch darktable on a folder. The folder is registered as a film roll on first launch and XMP sidecars are picked up automatically. Rating params are filter *hints* — they're echoed back with a star label so the caller knows which dropdown value to pick. (Pre-applying the filter via `--conf` is not reliable across darktable versions; we don't bet on it.)

GUI / export:
- `adjust_exposure(photo_ids, exposure_ev)` — Adjust exposure settings for photos. Opens darktable GUI to show preview.
- `export_images(photo_ids, output_path, format, quality?)` — Export photos to JPEG/PNG/TIFF via `darktable-cli`.

## Stubbed tools (registered, not implemented)

- `apply_preset(photo_ids, preset_name)` — Apply editing presets to photos. Planned for next release.

## Vision-rating workflow

When darktable's library doesn't yet know about your shoot — typically straight off a card or a freshly-copied folder — you can rate by vision before any import:

1. `extract_previews` writes auto-rotated JPEGs (and small thumbs) next to a small EXIF summary so an MCP client can iterate efficiently.
2. The client (e.g. Claude) reads previews, decides ratings, and calls `apply_ratings_batch` to write XMP sidecars alongside the raws.
3. `open_in_darktable` launches the GUI with the folder imported as a film roll. The rating arg returns a filter hint (e.g. `★★★★★`) telling the caller which value to pick in the lighttable's top filter bar.

No SQLite poking, no half-imported state, and no GUI launch until step 3.

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
