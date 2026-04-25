# Darktable MCP Server

A Model Context Protocol (MCP) server that enables Claude and other AI assistants to control darktable for photo management and editing.

## Features

- **Photo Library Management**: Browse, rate, import, and organize photos
- **Basic Photo Editing**: Adjust exposure, apply presets, crop images
- **Batch Operations**: Process multiple photos efficiently
- **Safe Integration**: Uses darktable's official Lua API
- **Easy Setup**: One-command installation

## Installation

```bash
pip install darktable-mcp
```

## Configuration

Add to your Claude Desktop configuration (`~/.claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "darktable": {
      "command": "python",
      "args": ["-m", "darktable_mcp"]
    }
  }
}
```

## Usage Examples

**Rate your best photos:**
> "Claude, rate my landscape photos from last week 4 stars"

**Batch photo editing:**
> "Claude, adjust exposure +0.5 on all underexposed sunset photos"

**Smart organization:**
> "Claude, import photos from my camera and organize by date"

## Requirements

- Python 3.8+
- darktable 4.0+
- Claude Desktop or compatible MCP client

## Available Tools

- `view_photos()` - Browse photo library with filters
- `rate_photos()` - Apply star ratings (1-5)
- `import_batch()` - Import from directories
- `adjust_exposure()` - Modify exposure settings
- `apply_preset()` - Apply editing presets
- `export_images()` - Export to various formats

## Contributing

Contributions welcome! Please read our contributing guidelines.

## License

MIT License - see LICENSE file for details.

## Keywords

darktable, MCP, model context protocol, photo editing, RAW processing, batch processing, AI photo management, Claude, photography workflow