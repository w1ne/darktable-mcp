# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes a small set of darktable
operations to MCP clients (e.g. Claude Desktop, Claude Code). The AI lives in
the client; this server drives darktable.

## Status

Early alpha. `view_photos` and `export_images` are wired through to real
darktable; the remaining tools are registered but return a "not yet
implemented" message.

## Design rule: headless by default, GUI only to show the human something

Tools whose purpose is to **return data to the AI client** must be headless.
Launching the darktable GUI for a query is a non-starter: it's slow,
single-instance, and disruptive to the user. So:

- **Headless paths used here:**
  - `darktable-cli` for export.
  - Read-only SQLite reads of `library.db` (via SQLite URI `mode=ro`) for
    library queries. `darktable-cli` cannot browse the user's library, and
    `darktable --lua` brings up the GUI — so the DB read is the only
    headless option for browsing.
- **GUI is only acceptable** when the tool's explicit purpose is to *show*
  the human user something in darktable's editor. No such tool exists yet.
  When one is added, it should be the only tool that spawns the GUI.
- **Writes to the library** must go through darktable's official APIs
  (Lua, or a future long-running plugin with an IPC channel). Do not write
  to `library.db`.

## Installation

```bash
pip install darktable-mcp
```

You also need `darktable` (with `darktable-cli`) installed and available on
your `PATH`.

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

If your `library.db` is in a non-standard location, set `DARKTABLE_LIBRARY`
to its absolute path.

## Implemented tools

- `view_photos(filter?, rating_min?, limit?)` — reads darktable's
  `library.db` read-only and returns id, rating (-1 = rejected, 0–5 =
  stars), and absolute path per row. Filter is a substring match against
  folder or filename.
- `export_images(photo_ids, output_path, format, quality?)` — exports each
  source file via `darktable-cli` to the given output directory.

## Stubbed tools (registered, not implemented)

- `rate_photos`, `import_batch`, `adjust_exposure`, `apply_preset`

## Usage example

> "Show me my 4-star photos from the 2025 folder, then export them as JPEGs
> at quality 90 into ~/exports."

The AI client calls `view_photos` to find candidates, then `export_images`
with the resulting file paths.

## Requirements

- Python 3.8+
- darktable 4.0+ (with `darktable-cli` on `PATH`)
- An MCP-compatible client (Claude Desktop, Claude Code, etc.)

## Contributing

Contributions welcome. Adding a real implementation for any of the stubbed
tools is the most useful place to start. Follow the design rule above.

## License

MIT — see `LICENSE`.

## Keywords

darktable, MCP, model context protocol, photo editing, RAW processing,
batch processing, photography workflow, Claude
