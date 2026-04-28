# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes darktable operations
to MCP clients (Claude Desktop, Claude Code, etc.). The AI lives in the
client; this server drives darktable.

## Tools

**Library operations** (require `darktable-mcp install-plugin` and an open darktable session):

- `view_photos(filter?, rating_min?, limit?)` — Browse the library by filename substring and minimum rating.
- `rate_photos(photo_ids, rating)` — Apply -1..5 star ratings (-1 = reject, 0 = unrated).
- `import_batch(source_path, recursive?)` — Register a folder as a film roll.

**Camera ingest** (headless):

- `import_from_camera(destination?, camera_port?, timeout_seconds?)` — Detect a camera via libgphoto2 and copy photos to a local directory.

**Vision-rating workflow** (headless, file-based — no library required, needs `[vision]` extra):

- `extract_previews(source_dir, output_dir?, max_dim?, thumb_dim?, overwrite?)` — Pull auto-rotated JPEG previews + small thumbs out of raws (NEF/CR2/ARW/DNG/...), with an EXIF summary per file.
- `apply_ratings_batch(source_dir, ratings, log?)` — Write XMP `xmp:Rating` sidecars for a `{stem: rating}` batch + an append-only `ratings.jsonl` log.
- `open_in_darktable(source_dir, rating?, rating_min?, rating_max?)` — Launch the GUI on a folder. Auto-registers as a film roll; pre-applies any rating filter (exact, ≥, ≤, or inner range) via `dt.gui.libs.collect.filter`.

**Export:**

- `export_images(photo_ids, output_path, format, quality?)` — Export to JPEG/PNG/TIFF via `darktable-cli`.

## Design rules

Use only the official darktable APIs: `darktable-cli` for export, the Lua API for everything else. No direct `library.db` reads or writes. Tools that return data to the AI must be headless; the GUI may launch only when the tool's purpose is to show the human something.

## Why some tools are parked

`darktable-cli` doesn't load the user's library and `darktable --lua` brings up the full GUI, so there's no headless one-shot path for library reads/writes. Iteration 2 (spec: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md`) shipped a long-running Lua plugin loaded into the user's interactive darktable session, with a file-based JSON RPC bridge. `view_photos`, `rate_photos`, and `import_batch` ride on it. Still parked: `apply_preset` and `adjust_exposure` — pending a Lua-API spike for `dt.styles` and image-history-stack manipulation.

## Installation

```bash
pip install darktable-mcp
# Optional: vision-rating workflow extras
pip install 'darktable-mcp[vision]'
# Install the Lua plugin into ~/.config/darktable/, then restart darktable
darktable-mcp install-plugin
```

You also need `darktable` (with `darktable-cli`) on `PATH`. The `[vision]` extra pulls in `rawpy`, `Pillow`, and `pyexiv2`, which need system `libraw` and `libexiv2`.

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

## Vision-rating workflow

When darktable's library doesn't yet know about your shoot — typically straight off a card — you can rate by vision before any import:

1. `extract_previews` writes auto-rotated JPEGs and an EXIF summary so the client can iterate efficiently.
2. The client reads previews, decides ratings, and calls `apply_ratings_batch` to write XMP sidecars next to the raws.
3. `open_in_darktable` launches the GUI with the folder as a film roll, lighttable filtered to the rating range you want.

No SQLite poking, no half-imported state, no GUI launch until step 3.

## Requirements

- Python 3.8+
- darktable 4.0+ (with `darktable-cli` on `PATH`)
- An MCP-compatible client (Claude Desktop, Claude Code, etc.)

## Contributing

Contributions welcome. The remaining parked tools (`apply_preset`, `adjust_exposure`) are the natural next step. Any change that reads or writes `library.db` directly will be rejected.

## License

MIT — see `LICENSE`.
