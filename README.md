# darktable-mcp

An MCP server that lets Claude and other MCP clients work with your darktable library: browse and rate photos, import from a camera, apply styles, export, and (with a patched darktable) edit module parameters and masks in the darkroom. It is for photographers who use darktable and want to drive it by chatting with an AI assistant.

The AI runs in your MCP client (Claude Desktop, Claude Code, or any other client). This server only drives darktable, using `darktable-cli` and darktable's Lua API. It never reads or writes `library.db` directly.

## What you can do

- Cull a shoot: extract previews from raw files, let the model rate them, write XMP sidecars, then open the folder in darktable filtered by rating.
- Work the library: search by filename and rating, tag, add notes, list collections, rate, apply styles (presets).
- Pull photos off a camera over USB with `gphoto2`, without overwriting files that share a name.
- Export JPEG, PNG or TIFF through `darktable-cli`, even while the darktable window is open.
- Edit the image open in the darkroom: read and set module parameters, blend settings, masks, retouch shapes, and compare LUTs. This part needs the [patched darktable build](https://github.com/rfordinal/darktable-agentic/tree/agentic-mcp) and does not work on stock darktable.

## Example prompts

- "Show me everything in the library rated 3 stars or more from the last import and make a contact sheet."
- "Extract previews from ~/Pictures/wedding, rate the sharp, well-exposed ones 4 and the rest 2, and open the 4s in darktable."
- "Import the card from my camera into ~/Pictures/import-today and tell me if any files were skipped."
- "Apply my 'film-warm' style to the selected photos, then export them as 2048px JPEGs to ~/Desktop/out."
- "Open this image in the darkroom, lift the shadows a bit, and preview three different LUTs."

## Install

Requirements: Python 3.10 or newer, darktable 4.0 or newer with `darktable-cli` on your `PATH`, and an MCP client. Linux is the main target; macOS works except for camera import. The plugin installer writes to `~/.config/darktable/`, so Windows is not supported, and `gphoto2` (needed for camera import) has no Windows build.

```bash
pip install 'git+https://github.com/w1ne/darktable-mcp'
# Optional: raw preview and sidecar rating tools (needs libraw and libexiv2)
pip install 'darktable-mcp[vision] @ git+https://github.com/w1ne/darktable-mcp'

# Install the Lua plugin into ~/.config/darktable/, then restart darktable
darktable-mcp install-plugin
```

The package is not on PyPI yet. Leave darktable open while you use the library tools: they talk to the running instance through the plugin.

### Claude Desktop

Add this to `claude_desktop_config.json` and restart Claude Desktop:

```json
{
  "mcpServers": {
    "darktable": {
      "command": "darktable-mcp"
    }
  }
}
```

The file lives at `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and `~/.config/Claude/claude_desktop_config.json` on Linux.

### Claude Code

```bash
claude mcp add darktable -- darktable-mcp
```

### Other MCP clients

Any client that can launch a stdio server can run the `darktable-mcp` command. For clients that only speak HTTP, run `darktable-mcp --http` (Streamable HTTP on `127.0.0.1:8787/mcp` by default; change with `--host`, `--port`, `--path`). Set `DTMCP_BEARER_TOKEN` to require a token. Set `DTMCP_PUBLIC_URL` to the external origin if you need download links for generated files; those links contain an access token, so treat them as private.

## Tools

The server exposes 54 tools. The main groups:

| Group | Tools |
|---|---|
| Library | `view_photos`, `get_contact_sheet`, `rate_photos`, `tag_photo`, `set_photo_note`, `get_photo_note`, `list_collections`, `list_photos_in_collection`, `import_batch`, `list_styles`, `apply_preset` |
| Camera and previews | `import_from_camera`, `extract_previews`, `apply_ratings_batch`, `open_in_darktable` |
| Export | `export_images` |
| Darkroom editing (patched darktable) | `open_image_in_darkroom`, `navigate_photo`, `get_current_image`, `list_modules`, `get_params`, `set_params`, `enable_module`, `add_instance`, `get_blend_params`, `set_blend_params` |
| Masks and retouch (patched darktable) | `list_masks`, `get_module_mask`, `get_mask_geometry`, `attach_mask`, `detach_mask`, `set_module_mask`, `add_path_mask`, `retouch_add_shape`, `retouch_list_shapes`, `mask_object`, `mask_raster`, and related |
| Preview and LUTs (patched darktable) | `get_viewport`, `set_viewport`, `get_preview`, `capture_viewport`, `list_luts`, `preview_lut`, `compare_luts` |

Subject segmentation (`mask_object`) uses SAM2 and MODNet through optional sidecar services, with a local GrabCut fallback for segmentation. See `darktable-mcp install-sidecar --help` and [docs/install/INSTALL-sidecar.md](docs/install/INSTALL-sidecar.md). The [editing tool guide](docs/agentic-fork.md) covers the darkroom tools in more detail.

`view_photos` browses the whole library by default; pass `scope="collection"` for the open lighttable collection. For duplicate versions of an image, pass the reported `sidecar` paths to `export_images` via `xmp_paths`. Export results report the real output paths, so do not infer filenames from input names.

## Troubleshooting

- **Library tools time out or say the bridge is not running.** darktable must be open, the plugin must be installed with `darktable-mcp install-plugin`, and darktable must have been restarted after that.
- **Darkroom tools report a missing `darktable.develop`.** You are on a stock darktable. Those tools need the [patched build](https://github.com/rfordinal/darktable-agentic/tree/agentic-mcp). Library, export and rating tools work on stock darktable.
- **Exported files have no edits.** Export reads edits from XMP sidecars. Turn on "write sidecar file for each image" in darktable's preferences.
- **A rating written by `apply_ratings_batch` does not show in darktable.** For photos already in the library, darktable trusts its database over the sidecar. In the lighttable, use "selected image(s) > read sidecar files".
- **`pip install` of the `[vision]` extra fails.** Install the system libraries `libraw` and `libexiv2` first.
- **Camera import finds nothing.** Install `gphoto2`, close anything else holding the camera (file managers often do), and check that the camera is in PTP or mass-storage mode.
- **`mcp` version errors.** This server needs `mcp>=2,<3`; 1.x does not work.

Run with `darktable-mcp --debug` for more logging.

## Tool details

**Camera ingest** (headless):

- `import_from_camera(destination?, camera_port?, timeout_seconds?)` — Detect a camera via libgphoto2 and copy photos to a local directory. Auto-merges hybrid setups (one card on PTP, the other mounted as USB Mass-Storage) into a single import — Nikon DSLRs in particular show up that way and the previous behavior silently halved the import.

  Files land **one subdirectory per camera folder or card, prefixed with the camera's identity** — never flat:

  ```
  <destination>/Nikon_D850_sn_30014567_store_00010001_DCIM_100NCD80/DSC_0001.NEF
  <destination>/Nikon_D850_sn_30014567_store_00020001_DCIM_100NCD80/DSC_0001.NEF   # same name, different photo
  <destination>/Canon_EOS_R6_EOS_DIGITAL_100EOSR6/IMG_0001.CR3
  <destination>/.import.log
  ```

  Camera filenames repeat across folders, across the two cards of a dual-slot body, and across *bodies* importing into the same destination — and the default destination `~/Pictures/import-YYYY-MM-DD/` is shared by every import on the same day. The old flat layout combined with gphoto2's `--skip-existing` silently dropped those duplicates; correctness no longer rests on that flag, which now only ever sees files the same run just wrote into its own private staging directory. Import the destination recursively.

  The serial number is read once per camera via `gphoto2 --get-config serialnumber` and omitted when the camera doesn't report one. On both paths a file that would collide with a *different* photo already on disk is written alongside it as `IMG_0001-2.CR3` and reported — never overwritten; sameness is judged on size plus the first and last 8 KB, and is only ever used to authorise a skip.

  Two bodies of the same model that report no serial share a destination subdirectory, and **both bodies' photos are kept there**. The PTP path downloads into a private staging area and, before skipping files the destination appears to already hold, re-checks a bounded sample of them against the bytes on disk — a second body fails that check and its folder is fetched in full. Re-running stays cheap: a body with a serial transfers nothing it already delivered, and one without transfers at most 3 files per folder. Any file the card lists that doesn't reach the destination is reported by name.

  **Residual limit:** that sample is bounded at 3 files per folder, so a second body whose sampled files are byte-identical to the first body's — in a folder holding more than 3 candidates — is still taken to be the same body. Folders with 3 or fewer candidates are checked exhaustively.

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

### Vision-rating workflow

When darktable's library doesn't yet know about your shoot — typically straight off a card — you can rate by vision before any import:

1. `extract_previews` writes auto-rotated JPEGs and an EXIF summary so the client can iterate efficiently.
2. The client reads previews, decides ratings, and calls `apply_ratings_batch` to write XMP sidecars next to the raws.
3. `open_in_darktable` launches the GUI with the folder as a film roll, lighttable filtered to the rating range you want.

No SQLite poking, no half-imported state, no GUI launch until step 3.

## Design rules

Use only the official darktable APIs: `darktable-cli` for export, the Lua API for everything else. No direct `library.db` reads or writes. Tools that return data to the AI must be headless; the GUI may launch only when the tool's purpose is to show the human something.

## Why the library tools need a plugin

`darktable-cli` doesn't load the user's library and `darktable --lua` brings up the full GUI, so there's no headless one-shot path for library reads/writes. Iteration 2 (spec: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md`) shipped a long-running Lua plugin loaded into the user's interactive darktable session, with a file-based JSON RPC bridge. The library tools (`view_photos`, `rate_photos`, `import_batch`, `list_styles`, `apply_preset`) all ride on it.

`adjust_exposure` was retired during iteration 3 — see `docs/superpowers/specs/2026-04-28-iter3-design.md`. The darktable Lua API in 9.6.0 exposes neither `image.modules` nor `image.history`, and `dt.gui.action` requires an active darkroom view (single-image, GUI-driven). The realistic future paths (pre-created `.dtstyle` exposure presets + `apply_preset`, or `darktable-cli --style` for export-only) are workable but not "set +N EV from Lua" tools.

## Notes on the MCP SDK

The SDK is pinned to `mcp>=2,<3`. Tools are registered through the mcp 2.x low-level `Server(on_list_tools=..., on_call_tool=...)` handlers. The tool schemas are handwritten and pinned by a golden-snapshot test, because the high-level API cannot express some of the bounds (for example the rating range on `apply_ratings_batch`), and the descriptions are what the calling model reads.

The editing tools build on Roman Fordinal's `agentic-mcp` fork; see [NOTICE.md](NOTICE.md). Unit and transport tests cover the integration; hardware camera and patched-darktable runtime checks are separate.

## Contributing

Contributions welcome. Any change that reads or writes `library.db` directly will be rejected.

## License

MIT, see `LICENSE`.
