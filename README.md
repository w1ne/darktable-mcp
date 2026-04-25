# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes a small set of darktable
operations to MCP clients (e.g. Claude Desktop, Claude Code). The AI lives
in the client; this server drives darktable.

## Status

Early alpha. Only `export_images` is implemented today. Other tools are
registered for shape but return "not yet implemented". See the design
rules below for why — these aren't TODOs, they're parked until the right
integration lands.

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
  `apply_preset`. All blocked on the long-running-plugin work above.

## Requirements

- Python 3.8+
- darktable 4.0+ (with `darktable-cli` on `PATH`)
- An MCP-compatible client (Claude Desktop, Claude Code, etc.)

## Contributing

Contributions welcome. The most useful starting point is the long-running
Lua-plugin + IPC integration that unblocks every parked tool. Any change
that reads or writes `library.db` directly will be rejected.

## License

MIT — see `LICENSE`.

## Keywords

darktable, MCP, model context protocol, photo editing, RAW processing,
batch processing, photography workflow, Claude
