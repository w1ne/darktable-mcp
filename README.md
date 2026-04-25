# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes a small set of darktable
operations to MCP clients (e.g. Claude Desktop, Claude Code). The AI lives in
the client; this server drives darktable through its provided APIs:
`darktable-cli` for export and the official Lua API for library reads.

## Status

Early alpha. `view_photos` and `export_images` are wired through to real
darktable; the remaining tools are registered but return a "not yet
implemented" message.

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

## Implemented tools

- `view_photos(filter?, rating_min?, limit?)` — runs a Lua script against
  darktable's `darktable.database` and returns id, rating (-1 = rejected,
  0–5 = stars), and absolute path per row. The filter is a substring match
  against the full image path.
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
tools is the most useful place to start. Stick to the provided APIs:
`darktable-cli` for export and the Lua API for everything else — do not read
or write `library.db` directly.

## License

MIT — see `LICENSE`.

## Keywords

darktable, MCP, model context protocol, photo editing, RAW processing,
batch processing, photography workflow, Claude
