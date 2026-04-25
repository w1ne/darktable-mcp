# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes a small set of darktable
operations to MCP clients (e.g. Claude Desktop, Claude Code). The AI lives in
the client; this server just shells out to `darktable-cli`.

## Status

Early alpha. Currently only `export_images` is wired through to `darktable-cli`.
The other tools are registered but return a "not yet implemented" message.

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

- `export_images(photo_ids, output_path, format, quality?)` — exports each
  source file via `darktable-cli` to the given output directory.

## Stubbed tools (registered, not implemented)

- `view_photos`, `rate_photos`, `import_batch`, `adjust_exposure`,
  `apply_preset`

## Usage example

> "Export these RAWs as JPEGs at quality 90 into ~/exports."

The AI client then calls `export_images` with the file paths, output
directory, format `jpeg`, and quality `90`.

## Requirements

- Python 3.8+
- darktable 4.0+ (with `darktable-cli` on `PATH`)
- An MCP-compatible client (Claude Desktop, Claude Code, etc.)

## Contributing

Contributions welcome. Adding a real implementation for any of the stubbed
tools is the most useful place to start.

## License

MIT — see `LICENSE`.

## Keywords

darktable, MCP, model context protocol, photo editing, RAW processing,
batch processing, photography workflow, Claude
