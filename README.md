# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes darktable operations
to MCP clients (Claude Desktop, Claude Code, etc.). The AI lives in the
client; this server drives darktable.

## Tools

**Library operations** (require `darktable-mcp install-plugin` and an open darktable session):

- `view_photos(filter?, rating_min?, limit?)` — Browse the library by filename substring and minimum rating. Returns id, filename, **absolute file path**, and rating per match — the path drops straight into `export_images`'s `photo_ids`.
- `rate_photos(photo_ids, rating)` — Apply -1..5 star ratings (-1 = reject, 0 = unrated).
- `import_batch(source_path, recursive?)` — Register a folder as a film roll. `recursive=true` is honoured in Lua by walking the tree and importing each directory, so it works regardless of darktable's `recurse_directories` preference. `recursive=false` cannot *stop* darktable recursing, so the response says so (`recursive_honoured: false`) rather than claiming a mode it did not deliver. If darktable's background scan has not settled when the plugin answers, the count is reported as a floor, not a total.
- `list_styles()` — Enumerate installed darktable styles (presets), returning name + description per entry.
- `apply_preset(photo_ids, preset_name)` — Apply a named darktable style to one or more photos. Use `list_styles` first to discover exact names.

**Camera ingest** (headless):

- `import_from_camera(destination?, camera_port?, timeout_seconds?)` — Detect a camera via libgphoto2 and copy photos to a local directory. Auto-merges hybrid setups (one card on PTP, the other mounted as USB Mass-Storage) into a single import — Nikon DSLRs in particular show up that way and the previous behavior silently halved the import.

  Files land **one subdirectory per camera folder or card**, never flat:

  ```
  <destination>/store_00010001_DCIM_100NCD80/DSC_0001.NEF
  <destination>/store_00020001_DCIM_100NCD80/DSC_0001.NEF   # same name, different photo
  <destination>/.import.log
  ```

  Camera filenames repeat across folders and across the two cards of a dual-slot body. The old flat layout combined with `--skip-existing` silently dropped those duplicates. Import the destination recursively.

  `timeout_seconds` is an **overall budget for one camera**, shared across all its folders — not a per-folder timeout. Skipped files are counted and reported, and a post-flight shortfall (fewer files on disk than the camera said it held) is surfaced as a prominent `!! INCOMPLETE IMPORT` block, because that is the moment before someone formats the card.

**Vision-rating workflow** (headless, file-based — no library required, needs `[vision]` extra):

- `extract_previews(source_dir, output_dir?, max_dim?, thumb_dim?, overwrite?, max_workers?)` — Pull auto-rotated JPEG previews + small thumbs out of raws (NEF/CR2/ARW/DNG/...), with an EXIF summary per file. Per-file details (paths, EXIF, errors) land in `<output_dir>/.extract_previews.jsonl`; the tool response keeps only counts and the side-file path so 700+ NEFs don't overflow the agent's context. The scan is **recursive** and the output tree **mirrors the source tree**, so same-named raws in different subdirectories get distinct previews — read the path from each item rather than assuming `<output_dir>/<stem>.jpg`. Decoding runs on a thread pool (`max_workers`, default `min(8, cpu_count)`).
- `apply_ratings_batch(source_dir, ratings, log?, force?)` — Write XMP `xmp:Rating` sidecars for a `{stem: rating}` batch + an append-only `ratings.jsonl` log. Keys may be a bare stem or a source-relative path; a bare stem that matches raws in more than one subdirectory is rejected as ambiguous rather than resolved by guesswork.
- `open_in_darktable(source_dir, rating?, rating_min?, rating_max?)` — Launch the GUI on a folder. Auto-registers as a film roll; pre-applies any rating filter (exact, ≥, ≤, or inner range) via `dt.gui.libs.collect.filter`.

### Behaviour worth knowing

**Existing XMP sidecars are patched, never replaced.** `apply_ratings_batch` rewrites only the rating value and leaves every other byte intact, so darktable edit history survives. A sidecar with no recognisable rating is skipped with an error instead of being overwritten. `force=True` opts into wholesale replacement and **discards edit history** — it exists for the "reset these sidecars" case and nothing else.

**Ratings for photos darktable already knows need one manual step.** darktable prefers its own `library.db` over the sidecar for images already in the library, so writing a sidecar changes nothing on screen. When this is detected the summary warns and names the affected files; run *selected image(s) → read sidecar files* in the lighttable to pull them in.

**`open_in_darktable` no longer claims a launch it didn't get.** An already-open darktable is the *normal* state when the bridge-backed library tools are in use, and it holds the lock on `library.db`. What darktable then does is platform-dependent, and neither branch is a launch:

| | behaviour | reported as |
|---|---|---|
| Linux (session D-Bus present) | hands the folder to the running instance, child exits 0 | `handed_off_to_running_instance: true` — look at the open window |
| macOS (no session D-Bus) | handoff fails on a GLib assertion and the child **hangs forever** | `launched: false` with the reason; the hung child is killed |

The tool used to report a phantom pid in both cases. Detection reads what darktable *says*, not whether the process is alive — on macOS it stays alive indefinitely, so liveness proves nothing. Verified against darktable 5.6.0.

**Export:**

- `export_images(photo_ids, output_path, format, quality?, max_width?, max_height?)` — Export to JPEG/PNG/TIFF via `darktable-cli`. Runs in an isolated config dir under `$XDG_CACHE_HOME/darktable-mcp/cli-config/`, so exports work even when the GUI is open (no `database is locked` race against the user's `~/.config/darktable/library.db`). Files export in parallel, and the output is `stat`ed afterwards — darktable-cli can exit 0 without writing anything, which used to be reported as success. Per-file results land in `<output_path>/.export_images.jsonl`; the tool response is bounded — counts, side-file path, and the first error if any.

  Output names are **de-collided**: two inputs with the same stem from different folders no longer overwrite each other, so read the real path from the `output` field rather than assuming `<stem>.<format>`. Note darktable-cli picks the extension itself — `jpeg` writes `.jpg` and `tiff` writes `.tif`. Each parallel worker gets its own config dir, because concurrent `darktable-cli` processes sharing one contend for the same `library.db` and one of them silently writes nothing.

  **Sidecar caveat:** because the config dir is isolated from the GUI's, exports read develop settings from XMP sidecars only. If darktable's *write sidecar file for each image* preference is off, files export **without their edits** and darktable-cli still reports success.

## Design rules

Use only the official darktable APIs: `darktable-cli` for export, the Lua API for everything else. No direct `library.db` reads or writes. Tools that return data to the AI must be headless; the GUI may launch only when the tool's purpose is to show the human something.

## Why some tools are parked

`darktable-cli` doesn't load the user's library and `darktable --lua` brings up the full GUI, so there's no headless one-shot path for library reads/writes. Iteration 2 (spec: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md`) shipped a long-running Lua plugin loaded into the user's interactive darktable session, with a file-based JSON RPC bridge. The library tools (`view_photos`, `rate_photos`, `import_batch`, `list_styles`, `apply_preset`) all ride on it.

`adjust_exposure` was retired during iteration 3 — see `docs/superpowers/specs/2026-04-28-iter3-design.md`. The darktable Lua API in 9.6.0 exposes neither `image.modules` nor `image.history`, and `dt.gui.action` requires an active darkroom view (single-image, GUI-driven). The realistic future paths (pre-created `.dtstyle` exposure presets + `apply_preset`, or `darktable-cli --style` for export-only) are workable but not "set +N EV from Lua" tools.

## Installation

Not on PyPI yet — install from the repository:

```bash
pip install 'git+https://github.com/w1ne/darktable-mcp'
# Optional: vision-rating workflow extras
pip install 'darktable-mcp[vision] @ git+https://github.com/w1ne/darktable-mcp'
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

- Python 3.10+ (the floor comes from `mcp`, which requires 3.10)
- darktable 4.0+ (with `darktable-cli` on `PATH`)
- An MCP-compatible client (Claude Desktop, Claude Code, etc.)
- Linux, or macOS for the parts that don't need `gphoto2`. The plugin installer writes to `~/.config/darktable/`, which is where darktable keeps its config on Linux and macOS but **not** on Windows — `import_from_camera` also needs `gphoto2`, which has no Windows build.

The MCP SDK is pinned to `mcp>=1.9,<2`. mcp 2.0.0 removed the `@server.list_tools()` / `@server.call_tool()` decorators this server is built on; porting to the 2.x `MCPServer` API is open work.

## Contributing

Contributions welcome. Any change that reads or writes `library.db` directly will be rejected.

## License

MIT — see `LICENSE`.
