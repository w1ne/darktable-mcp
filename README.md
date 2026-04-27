# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes a small set of darktable
operations to MCP clients (e.g. Claude Desktop, Claude Code). The AI lives
in the client; this server drives darktable.

## Status

**Implemented tools:**

Library operations (via Lua plugin — requires `darktable-mcp install-plugin` and an open darktable session):
- `view_photos` — Browse photos in the darktable library with filename/rating filters.
- `rate_photos` — Apply star ratings to photos in the library.

Camera ingest (headless):
- `import_from_camera` — Detect a connected camera (libgphoto2) and copy photos to a local directory

Vision-rating workflow (headless, file-based — no library required):
- `extract_previews` — Pull auto-rotated JPEG previews out of raw files (NEF/CR2/ARW/DNG/etc), with a tiered thumb pass and an EXIF summary per file. Designed for token-efficient visual rating loops in MCP clients.
- `apply_ratings_batch` — Write XMP sidecars (`xmp:Rating`) for a `{stem: rating}` batch, plus an append-only `ratings.jsonl` log so the history survives session resets.
- `open_in_darktable` — Launch the darktable GUI on a folder. The folder registers as a film roll on first launch and the XMP sidecars are picked up automatically. The lighttable opens already filtered via the official `darktable.gui.libs.filter` Lua API (rating + comparator) for exact ratings, open-bounded ranges (`rating_min` only or `rating_max` only), full range, and `[0, 5]` ("not rejected"). Arbitrary inner ranges (e.g. 2..4) fall through unfiltered with a hint.

Export:
- `export_images` — Export photos to JPEG/PNG/TIFF via `darktable-cli`

**Not yet implemented:**

The full library-aware tool set (browsing, rating, importing into the library, exposure adjustment, preset application) is deferred to iteration 2 — see the spec under `docs/superpowers/specs/` once written.

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

> Iteration 2 (spec: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md`) restored `view_photos` and `rate_photos` via a long-running Lua plugin. The remaining parked tools (`import_batch`, `adjust_exposure`, `apply_preset`) and the inner-range filter for `open_in_darktable` are deferred to a follow-up iteration — each is incremental once the bridge exists.

## Installation

```bash
# Base install — camera ingest, export, GUI launcher.
pip install darktable-mcp

# Optional: vision-rating workflow (extract_previews, apply_ratings_batch).
pip install 'darktable-mcp[vision]'
```

After installing, also install the darktable Lua plugin:

```bash
darktable-mcp install-plugin
```

This copies one Lua file into `~/.config/darktable/lua/` and adds a single `require` line to `~/.config/darktable/luarc`. Restart darktable for the plugin to load. The library tools (`view_photos`, `rate_photos`) require darktable to be open with the plugin loaded.

You also need `darktable` (with `darktable-cli`) installed and available
on your `PATH`. The `[vision]` extra pulls in `rawpy`, `Pillow`, and
`pyexiv2`, which in turn need `libraw` and `libexiv2` system packages.

**Quick test:** After installation, try:
```bash
# In Claude Desktop, you can now ask:
# "Pull a folder of raws off my camera" (uses import_from_camera)
# "Extract previews from ~/Pictures/import-2026-04-26" (uses extract_previews)
# "Open ~/Pictures/import-2026-04-26 in darktable, filtered to 5 stars" (uses open_in_darktable)
```

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

Library operations (via Lua plugin — see `darktable-mcp install-plugin`):
- `view_photos(filter?, rating_min?, limit?)` — Returns photos matching the filename substring and minimum star rating, up to `limit` (default 100). Each entry: `id`, `filename`, `path`, `rating`. Requires darktable to be open with the plugin loaded.
- `rate_photos(photo_ids, rating)` — Applies a star rating (-1=reject, 0=unrated, 1-5=stars) to the named photos. Returns updated count. Requires darktable to be open with the plugin loaded.

Camera ingest:
- `import_from_camera(destination?, camera_port?, timeout_seconds?)` — Detect a connected camera via libgphoto2 and copy all photos to a local directory. Returns the destination path.

Vision-rating workflow:
- `extract_previews(source_dir, output_dir?, max_dim?, thumb_dim?, overwrite?)` — Extract embedded JPEG previews from raws, auto-rotate via EXIF orientation, and write a tiered preview (default 1024px) plus a small first-pass thumb (default 384px). Returns per-file paths and an EXIF summary (ISO, shutter, focal, aperture, datetime).
- `apply_ratings_batch(source_dir, ratings, log?)` — Write XMP sidecars for a `{stem: rating}` batch (range -1..5, -1 = reject). Appends each rating to `<source_dir>/ratings.jsonl` for replay/audit.
- `open_in_darktable(source_dir, rating?, rating_min?, rating_max?)` — Launch darktable on a folder. The folder is registered as a film roll on first launch and XMP sidecars are picked up automatically. The lighttable opens already filtered via `darktable.gui.libs.filter.rating(...) + rating_comparator(...)` (the official Lua API) for exact ratings (EQ), `rating_min=N` (GEQ), `rating_max=N` (LEQ), full range (ALL), and `[0, 5]` (NOT_REJECT). Arbitrary inner ranges (e.g. 2..4) can't be expressed as a single comparator pair and fall through unfiltered with a hint.

Export:
- `export_images(photo_ids, output_path, format, quality?)` — Export photos to JPEG/PNG/TIFF via `darktable-cli`.

## Vision-rating workflow

When darktable's library doesn't yet know about your shoot — typically straight off a card or a freshly-copied folder — you can rate by vision before any import:

1. `extract_previews` writes auto-rotated JPEGs (and small thumbs) next to a small EXIF summary so an MCP client can iterate efficiently.
2. The client (e.g. Claude) reads previews, decides ratings, and calls `apply_ratings_batch` to write XMP sidecars alongside the raws.
3. `open_in_darktable` launches the GUI with the folder imported as a film roll. The lighttable opens already filtered via the Lua API for exact ratings, `rating_min` only, `rating_max` only, full range, and `[0, 5]`. Inner ranges (e.g. 2..4) need the new filtering panel and currently fall through with a hint.

No SQLite poking, no half-imported state, and no GUI launch until step 3.

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
