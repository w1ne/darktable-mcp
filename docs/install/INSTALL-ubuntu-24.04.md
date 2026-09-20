# Installing darktable-agentic + darktable-mcp on Ubuntu 24.04 (noble)

This gets you a working conversational photo-editing loop: darktable (patched
with the `darktable.develop` Lua bridge) + the darktable-mcp MCP server,
registered with Claude Desktop or Claude Code, driving real darkroom edits,
including object masks (see section 5, "Masks (Phase 2)") and soft-edged
matte masks (see section 5a, "Raster (matte) masks (Phase 3)") below.

The SAM2/MODNet sidecar used by the `mask_object`/`mask_raster` tools is an
**optional, separate** install, not covered by the two `.deb` files below --
see **`INSTALL-sidecar.md`** (next to this file) if you want it (`mask_raster`
has no fallback and always needs its MODNet half; `mask_object` only
upgrades from a bundled fallback). Everything else in this document,
including the `add_path_mask` masking tool, needs no sidecar.

Two packages, installed in order:

1. `darktable-agentic_5.8.0+agentic.4_amd64.deb` -- darktable itself, patched.
2. `darktable-mcp_1.0.4_amd64.deb` -- the Python MCP server that talks to it.

Both are plain `.deb` files with real `Depends:` lines -- `apt` pulls
everything else (GTK, LensFun, exiv2, python3, etc.) from the stock Ubuntu
24.04 repositories. No PPAs, no manual dependency installs.

(Note on `darktable-mcp`'s `Architecture:` field: as of 1.0.1 this package is
correctly labeled `amd64`, not `all` -- it bundles a venv with amd64-compiled
wheels, so it was never actually architecture-independent. If you have an
older `darktable-mcp_1.0.0_all.deb` installed, remove it first with
`sudo apt remove darktable-mcp` before installing 1.0.4. Older
`+agentic`/`+agentic.2`/`+agentic.3` and `darktable-mcp`
`1.0.0`/`1.0.1`/`1.0.2`/`1.0.3` `.deb` files are kept in `dist/` for
reference/rollback, version-suffixed; the pair described in this document --
`+agentic.4` / `1.0.4` -- is the current one. `+agentic.4` adds the Phase 3
raster/matte-mask C binding (`darktable.develop.set_raster_source`);
`darktable-mcp 1.0.4` adds the `mask_raster` tool that uses it (see section
5a below) -- previously in the source tree only, now packaged.)

## 1. Install darktable-agentic

```
sudo apt install ./darktable-agentic_5.8.0+agentic.4_amd64.deb
```

This package `Provides`/`Conflicts`/`Replaces` the stock `darktable` package,
so it cleanly supersedes any existing darktable install on the same system.
It puts the bridge plugin at `/usr/share/darktable/lua/darktable_mcp.lua`,
already on darktable's Lua `package.path` -- but darktable does not
auto-`require` arbitrary scripts, so you still need to opt in.

## 2. Enable the bridge

```
mkdir -p ~/.config/darktable
cat /usr/share/doc/darktable-agentic/luarc.snippet >> ~/.config/darktable/luarc
```

That snippet is one line:

```lua
require "darktable_mcp"
```

Appending it to your `luarc` tells darktable to load the bridge plugin at
startup (it does not touch or overwrite anything else already in your
`luarc`). Now just start darktable normally:

```
darktable
```

> **GOTCHA (read this if the bridge never comes up):** your `luarc` must
> contain **exactly** `require "darktable_mcp"`, nothing else on that line.
> A wrong or leftover `require` line from an older setup (for example a
> stale `require 'lua/mcp_export'` from a previous experiment, or any typo
> in the module name) errors out on that line and **aborts the rest of
> `luarc`** -- darktable's Lua init does not "skip and continue" past a
> failed `require`, it stops processing the file right there. Everything
> after the bad line, including a *correct* `require "darktable_mcp"` if it
> happens to come later, never runs. The bridge plugin silently never
> loads, and:
>
> - `~/.cache/darktable-mcp/` stays empty (no request/response files ever
>   appear).
> - the MCP server's tool calls all fail with "darktable not running, or
>   plugin not loaded" even though darktable itself is clearly running.
>
> **Verify it actually loaded** every time you touch `luarc`, before
> assuming anything else is wrong:
>
> ```
> darktable -d lua 2>&1 | grep -i mcp
> ```
>
> You must see a line containing `darktable-mcp bridge: ready`. If you see
> nothing (or a Lua error mentioning a *different* module name), open
> `~/.config/darktable/luarc` and check every line above and including your
> `require "darktable_mcp"` line for typos or leftovers from an older setup
> -- fix/remove the bad line and restart darktable.

You should see `darktable-mcp bridge: ready` in darktable's log output
(`darktable -d lua` if you want to watch for it explicitly, same command as
above). Leave darktable running -- the MCP server talks to it through a
small file-based JSON bridge under `$XDG_CACHE_HOME/darktable-mcp/`
(defaults to `~/.cache/darktable-mcp/`), so darktable has to be up whenever
you use the MCP tools.

## 3. Install darktable-mcp

```
sudo apt install ./darktable-mcp_1.0.4_amd64.deb
```

This installs:

- `/opt/darktable-mcp/venv/` -- a self-contained Python 3.12 venv with the
  `mcp` package, `pydantic`, and the `darktable_mcp` server code already
  installed. Nothing is downloaded at install time; `apt install` works
  offline once the `.deb` file itself is on disk.
- `/usr/bin/darktable-mcp` -- a small launcher script that execs the venv's
  server. This is the command you point Claude at.

Only dependency: `python3 (>= 3.12)` (Ubuntu 24.04 ships python3.12 as the
default `python3`, so this is already satisfied on any stock noble install).

Sanity check it's there:

```
$ /usr/bin/darktable-mcp --help
usage: darktable-mcp [-h] [--debug]

Darktable MCP Server (stdio)

options:
  -h, --help  show this help message and exit
  --debug     Enable debug logging
```

## 4. Register the MCP server with Claude

The server speaks MCP over stdio, so both Claude Desktop and Claude Code
just need to know the command to launch it: `/usr/bin/darktable-mcp`, no
arguments.

### Claude Desktop

Edit `~/.config/Claude/claude_desktop_config.json` (create it if it doesn't
exist yet) and add a `darktable` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "darktable": {
      "command": "/usr/bin/darktable-mcp",
      "args": []
    }
  }
}
```

(If the file already has other `mcpServers` entries, just add the
`"darktable": { ... }` block alongside them.) Restart Claude Desktop after
saving.

This exact block is also shipped in the package at
`/usr/share/doc/darktable-mcp/claude_desktop_config.snippet.json`.

### Claude Code

Either add the same block to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "darktable": {
      "command": "/usr/bin/darktable-mcp",
      "args": []
    }
  }
}
```

or register it from the CLI:

```
claude mcp add darktable /usr/bin/darktable-mcp
```

### Important: darktable must already be running

The develop tools (`open_image_in_darkroom`, `list_modules`, `get_params`,
`set_params`, `get_preview`, `enable_module`, `add_instance`,
`get_viewport`) all talk to a **running** darktable instance over the file
bridge -- the MCP server does not launch darktable for you. Before asking
Claude to edit a photo:

1. Start darktable (with the bridge enabled, per step 2 above).
2. `open_image_in_darkroom` will select and open the image in darktable's
   darkroom view for you, given an image ID (from `view_photos`) -- but
   darktable itself has to already be open. You are still the one driving
   darktable's window; the MCP tools just automate what you'd otherwise
   click through.

## 5. Masks (Phase 2, darktable-agentic.3+ / darktable-mcp 1.0.2+)

Two new tools add object/region masking on top of the parameter-editing
loop above:

- **`add_path_mask(op, instance, points, opacity=1.0)`** -- attaches a
  drawn (`DT_MASKS_PATH`) polygon mask to an existing module instance,
  restricting that instance's effect to inside the polygon. `points` is a
  list of `{x, y}` nodes in normalized 0..1 image coordinates (darktable's
  own path-node convention, closed implicitly -- no repeated closing
  point). This is the tool to reach for when **you already have a
  polygon** (e.g. a shape Claude worked out itself, or one from an
  external tool) and just want it applied. **Needs no sidecar, no extra
  install** -- it's pure darktable/Lua, present as soon as
  `darktable-agentic` is installed and the bridge (`darktable.develop`,
  now API v6) is loaded.

- **`mask_object(op, adjustment, points|box|label, opacity=1.0,
  new_instance=True)`** -- the natural-language-friendly version: "mask
  this object and adjust it" in one call. It (1) grabs a full-frame
  `get_preview`, (2) sends a point/box/label prompt on that preview to the
  **SAM2 segmentation sidecar** to get a polygon back, (3) optionally adds
  a new module instance, (4) calls `add_path_mask` with the returned
  polygon, (5) applies `adjustment` via `set_params` on the masked
  instance. Any failure at any step rolls back a just-created instance
  (no orphan module left behind) and reports a clear error instead of
  crashing.

  **`mask_object` needs the SAM2 sidecar; everything else in this document
  does not.** The sidecar is a separate, optional install (its own venv:
  torch CPU + a ~149MB checkpoint -- deliberately NOT bundled in the
  `darktable-mcp` `.deb`, which would otherwise balloon and drag in a
  dependency stack unrelated to most users' needs). See
  **`INSTALL-sidecar.md`** (next to this file) for the full setup. Without
  it configured, `mask_object` returns a message like:

  ```
  mask_object: segmentation sidecar not installed/configured: sidecar
  python not found at .../sidecar/.venv/bin/python3.12. Install the
  optional SAM2 sidecar (see dist/INSTALL-sidecar.md) and set
  DARKTABLE_MCP_SIDECAR_PYTHON (and DARKTABLE_MCP_SIDECAR_SEGMENT if it
  isn't at the default in-repo location), or pass sidecar_python
  explicitly. add_path_mask and every other tool work fine without it --
  only mask_object's auto-segmentation needs the sidecar.
  ```

  and touches nothing in darktable (no instance created, no orphan) -- the
  server process itself stays up.

  Once the sidecar is installed (see `INSTALL-sidecar.md`), point the
  server at it with two environment variables set wherever
  `/usr/bin/darktable-mcp` is launched from:
  `DARKTABLE_MCP_SIDECAR_PYTHON` (the sidecar venv's `python3.12`) and
  `DARKTABLE_MCP_SIDECAR_SEGMENT` (the sidecar's `segment.py`).

## 5a. Raster (matte) masks (Phase 3, darktable-agentic 5.8.0+agentic.4 / darktable-mcp 1.0.4)

One more tool, `mask_raster`, is the soft-edged counterpart to
`mask_object` above -- both apply a local edit to part of the image, but
they answer a different question:

- `mask_object` (section 5): "is this pixel part of the object" -- a hard
  polygon boundary. Good for faces, sky, most everyday objects.
- `mask_raster`: "how much of this pixel is foreground" -- a continuous
  0..1 alpha per pixel, soft edge. Use it instead whenever the subject has
  fine edges a polygon would jag up: a hairline, flyaway strands, fur,
  motion blur, glass, smoke.

**`mask_raster(op, adjustment, opacity=1.0, new_instance=True)`** acts on
whichever image is currently open in darkroom (no point/box picking
needed -- unlike `mask_object`, the matting model is dense/whole-image, not
promptable). It (1) runs the open image (or, for RAW files the matting
model can't read directly, a full-resolution darkroom preview export)
through the **MODNet matting sidecar** to get a continuous alpha matte,
(2) writes it to a freshly, uniquely-named file and loads it into a new
instance of the stock `rasterfile` module, (3) creates a new (or reuses
instance 0 of) the target module and applies `adjustment`, (4) wires the
target module's blend to consume `rasterfile`'s raster mask (the new
`darktable.develop.set_raster_source` Lua binding), (5) renders a fresh
preview. Any failure at any step rolls back every module instance created
so far (no orphan `rasterfile` or consumer instance left behind).

**`mask_raster` needs the MODNet half of the sidecar; it has NO
lower-quality fallback** (unlike `mask_object`'s GrabCut fallback -- a
hard-edged segmenter cannot produce a soft matte, so a missing sidecar is a
clear error instead of a silent downgrade). See **`INSTALL-sidecar.md`**,
"mask_object vs. mask_raster: segmentation vs. matting", for the MODNet
checkpoint download and the two extra environment variables
(`DARKTABLE_MCP_SIDECAR_MATTE`, `DARKTABLE_MCP_MODNET_CHECKPOINT`) --
`DARKTABLE_MCP_SIDECAR_PYTHON` is shared with `mask_object`'s SAM2 setup.
Without it configured, `mask_raster` returns a clear error and touches
nothing in darktable, same contract as `mask_object`'s sidecar-absent path.

## 6. New in the previous release (darktable-agentic.2 / darktable-mcp 1.0.1)

Four tools/arguments were added on top of the original `list_modules` /
`get_params` / `set_params` / `get_preview` loop:

- **`enable_module(op, instance=0, enabled)`** -- many modules ship OFF by
  default (`grain`, `sharpen`, `vignette`, `tonecurve`, ...) and produce no
  visible effect until enabled, even after `set_params` writes their
  parameters. Call this first (or use the `set_params` convenience below)
  before expecting to see anything from those modules. Example: `Claude,
  enable the grain module` -> `enable_module('grain', enabled=True)`.

- **`set_params(op, fields, enabled=...)` -- the `enabled` convenience** --
  `set_params` now accepts an optional `enabled: true/false` field inside
  its `fields`/arguments. When present, it's applied via `enable_module`
  *before* the rest of the parameter writes, so a module that ships off by
  default (grain, sharpen, tonecurve, ...) can be enabled and configured in
  a single call instead of two: e.g. `set_params('grain', {'enabled': True,
  'strength': 90})` turns grain on and sets its strength in one step.

- **`add_instance(op)`** -- adds a new masked/parametric instance of a
  module, mirroring the darkroom GUI's "new instance" action (the base step
  for local edits: a second `exposure` instance for a masked dodge/burn, a
  second `sharpen` for a specific area, etc). Returns the new instance
  number.

- **`get_viewport()`** -- read-only darkroom canvas zoom/pan state, for
  both the main window and (if open on a second monitor) the `preview2`
  window: `{'main': {...}, 'preview2': {'active': bool, ...}}`. Use it to
  know what part/zoom of the image the user is actually looking at before
  calling `get_preview` with a matching `region`.

- **`get_preview(..., region={x, y, w, h})`** -- `get_preview` now accepts
  an optional `region` argument (each of `x`, `y`, `w`, `h` normalized 0..1
  of the visible frame) to render just a sub-rectangle at full detail
  instead of the whole downscaled frame. This is the right tool for
  inspecting grain, sharpening, or noise, which are easy to miss when
  looking at a shrunk full-frame preview -- pair it with `get_viewport()`
  to match the region the user is currently zoomed into.

## 7. Try it

With darktable running and some photos already imported into its library
(File > Import, or `view_photos`/`import_batch` from Claude), try asking
Claude something like:

> Open image X in the darkroom, lift the exposure a bit, and show me a
> preview.

What should happen: Claude calls `view_photos` to find the image, then
`open_image_in_darkroom` to switch darktable into darkroom view on it, then
`get_params` on the `exposure` module to see the current value and its
min/max, then `set_params` to nudge `exposure` up, then `get_preview` to
fetch a PNG of the live in-memory edit and show it to you. darktable's own
window will visibly update as each `set_params` call lands (same pixelpipe,
no separate render). Ask for another tweak and it repeats the loop --
that's the conversational edit loop this whole stack exists for.

## Troubleshooting

**"darktable not running, or plugin not loaded. Open darktable and try
again."** (or a bridge timeout) -- almost always one of:

- darktable isn't running at all. Start it.
- Your `luarc` doesn't have `require "darktable_mcp"` in it (step 2). Check
  `~/.config/darktable/luarc`.
- darktable is running but hasn't finished starting up yet (Lua init runs
  fairly early, but on a slow disk/first launch it can take a few seconds).
  Wait a moment and retry.
- You're running darktable with a *different* `--configdir` than the one
  containing the `luarc` you edited (e.g. testing with `--configdir` for an
  isolated profile). Edit the `luarc` under that same configdir.

**"darktable-mcp plugin not installed. Run: darktable-mcp install-plugin"**
-- this specific message means darktable is reachable but the bridge
plugin file itself isn't found. Confirm
`/usr/share/darktable/lua/darktable_mcp.lua` exists (it's installed by the
`darktable-agentic` package, step 1) and that you actually installed that
package, not stock `darktable`.

**`get_preview` returns a path but nothing loads / file not found** -- on a
normal single-machine workstation install this shouldn't happen (server and
darktable share the same filesystem, so the path from the bridge is
directly readable). If you *are* running the MCP server somewhere unusual
relative to darktable (e.g. a container remap), the server supports a
`DARKTABLE_MCP_RUN_DIR` environment variable to rewrite bridge paths -- but
for a normal native install on this workstation, leave it unset.

**Python version errors from `/usr/bin/darktable-mcp`** -- the package
bundles its own venv built against python3.12, so this should be
self-contained. If you see an error about a missing `python3.12` shared
library, check `python3 --version` (should report 3.12.x on stock noble);
if it's genuinely older, `apt install python3` should already have pulled
in a compatible version as a package dependency.

**Nothing happens when you ask Claude to edit a photo** -- check whether
Claude's MCP config actually picked up the `darktable` server (Claude
Desktop/Code usually show connected MCP servers somewhere in their UI/logs)
and that you restarted the client after editing its config file.

**`mask_object: segmentation sidecar not installed/configured: ...`** --
expected and not a bug if you haven't set up the optional SAM2 sidecar. See
**`INSTALL-sidecar.md`** to install it, then set
`DARKTABLE_MCP_SIDECAR_PYTHON` / `DARKTABLE_MCP_SIDECAR_SEGMENT` wherever
`/usr/bin/darktable-mcp` is launched from. `add_path_mask` (and everything
else) works without the sidecar; only `mask_object`'s auto-segmentation
needs it.
