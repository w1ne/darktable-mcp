# set_viewport / restore_viewport: design + implementation task

**Date:** 2026-07-31
**Status:** DELEGATED — implement in an isolated worktree, report back before merge
**Source:** bugreport "writable viewport control for darktable-mcp" (agent-authored feature request, full spec pasted verbatim below investigation notes)

## Why this exists

`get_viewport`/`capture_viewport` are read-only. Render detail in `capture_viewport`
is capped by the darkroom's CURRENT GUI zoom, not by the `max_w`/`max_h` the caller
asks for — an agent cannot get more detail than whatever the human last happened to
have zoomed to. On a session with no human at the keyboard (headless/Wayland, no
`xdotool`/`ydotool` fallback), there is no way to zoom in at all. This pushes agents
onto `retouch_add_shape`/`add_path_mask`'s low-level mask-frame coordinates, hand-
deriving the transform empirically (place a mask, export, look where it landed,
repeat) — the reporter counted ~8 calls to reverse-engineer one crop's transform.
`set_viewport`/`restore_viewport` close this gap: a writable zoom/pan control so an
agent can zoom into whatever region it needs *before* calling `capture_viewport`.

## What already exists (investigated, not assumed)

**Read side**, `src-dt/src/lua/develop.c:1322-1451` (`_push_viewport`, wired to
`get_viewport`):
- `dt_dev_get_viewport_params(port, &zoom, &closeup, &zx, &zy)` — current zoom mode/
  closeup factor/center-relative pan.
- `dt_dev_get_zoom_scale(port, zoom, 1<<closeup, FALSE)` — effective device-px-per-
  processed-px scale for a given mode.
- `region` (the thing `capture_viewport` and this feature's `achieved.region` need)
  is derived, NOT stored directly, from a documented formula already in the C
  comment above `_push_viewport`:
  ```
  w = port->width  / (procw * scale)
  h = port->height / (proch * scale)
  center_x = 0.5 + zoom_x,  center_y = 0.5 + zoom_y     (zoom_x/zoom_y center-relative)
  top_left_x = center_x - w/2,  top_left_y = center_y - h/2
  clamp to [0,1], never exceed image edge
  ```
  **This formula is exactly what `set_viewport` must invert**: given a desired
  `region {x,y,w,h}`, solve for `zoom_x, zoom_y, scale` that make the read-side
  formula reproduce that region (or, when aspect doesn't match the window, the
  *expanded* region per requirement 2 below).

**Write side** — the report guessed function names (`dt_control_set_dev_zoom_scale`,
`dt_control_set_dev_zoom_x/y`) that **do not exist in this codebase**. The real
primitive, verified present and exported:
```c
// src-dt/src/develop/develop.h:458
void dt_dev_zoom_move(dt_dev_viewport_t *port,
                      dt_dev_zoom_t zoom,
                      const float scale,
                      const int closeup,
                      const float x,
                      const float y,
                      const gboolean constrain);
```
Body at `src-dt/src/develop/develop.c:3187`. Confirmed behavior for
`zoom == DT_ZOOM_POSITION`: sets `zoom_x = x; zoom_y = y` directly (absolute
center-relative position) — **this is the mode to use**, not `DT_ZOOM_FREE`
(the report's guess; `DT_ZOOM_FREE` is a stored *label* value read back by
`_zoom_name`, not an operation `dt_dev_zoom_move` branches on for setting an
arbitrary scale+position in one call — verify exactly what `scale`/`closeup` do in
the `DT_ZOOM_POSITION` branch before relying on it; read the rest of the function,
it's long, this task only quotes the first ~40 lines). It already handles
`port->pipe`-relative distortion transform correctly (see its own
`_dev_distort_transform_locked` call) — this is the real, exercised GUI code path
(bound to mouse-drag/scroll callbacks), not something to reimplement from scratch.

**Wait-for-pipe mechanism** — `get_preview`'s own implementation
(`develop.c:1729-1889`, `preview_cb`) already solves the EXACT problem requirement 3
describes ("must block until the pixelpipe has actually reprocessed"), for the
preview pipe specifically:
- `set_params -> dt_dev_add_history_item -> dt_dev_invalidate_all` sets
  `preview_pipe->status = DT_DEV_PIXELPIPE_DIRTY` and does `dev->timestamp++`
  synchronously on the GUI thread (develop.c:1744-1746).
- Every worker run stamps `pipe->input_timestamp = dev->timestamp` at its START, only
  reaching `DT_DEV_PIXELPIPE_VALID` at its END.
- A frame is fresh iff `status == DT_DEV_PIXELPIPE_VALID && input_timestamp >= want_ts`
  — `preview_cb` polls this (bounded, with a timeout that returns
  `{status="processing", stale_preview=...}` rather than blocking forever — the exact
  `pipe_ready:false`-on-timeout behavior requirement 3 asks for).
- There's also a generic `_run_on_gui_sync(fn, data)` helper (develop.c:1763-1805,
  `GMutex`/`GCond`-based) for "run this synchronously on the GUI thread and block the
  Lua thread without holding its lock" — the general pattern any new binding needing
  GUI-thread-then-wait semantics should reuse.

**Which pipe to watch**: zoom/pan changes affect `dev->full.pipe` (the darkroom
CENTER VIEW pipe, what `capture_viewport("main")` actually reads), NOT
`dev->preview_pipe` (the small always-full-frame navigation thumbnail — see the
`dt_masks_get_image_size` investigation task, which found `preview_pipe` does NOT
track zoom at all). Confirm which status/timestamp fields `dev->full.pipe` exposes
and whether `dt_dev_invalidate_all`'s dirty-marking covers it the same way it covers
`preview_pipe`, or whether a different invalidation call is needed after
`dt_dev_zoom_move`.

## Scope

Implement exactly the tool the report specifies — its semantics section is precise
and should be followed as written, not re-derived. Repeating the parts that matter
most because getting them wrong reintroduces the bug this tool exists to fix:

1. **`region` is the same frame `get_viewport().region`/`get_preview(region=...)` use**
   — full processed (post-crop) frame, normalized 0..1, top-left origin. State this
   explicitly in the tool description (this project has a running problem with
   under-documented coordinate frames — see the two sibling investigation tasks from
   the same bugreport; don't add a fourth ambiguous frame).
2. **Aspect mismatch: expand, centered, never crop.** Enlarge the requested region
   minimally so all of it stays visible; report `achieved.region` and
   `aspect_adjusted: true`.
3. **`wait_for_pipe: true` (default) must block until `dev->full.pipe` has actually
   reprocessed at the new scale** — this is the single most important requirement;
   see the wait-for-pipe mechanism above. Timeout -> `pipe_ready: false`, not a false
   "ok".
4. **`renderable_px` must be truthful** — compute it from the ACTUAL resulting pipe
   state after the wait, not from the request.
5. **Free zoom** (`DT_ZOOM_POSITION` per the investigation above, not stepping
   through fit/1:1/2:1 presets).
6. **Clamp scale to darktable's allowed range, report the clamp** (same `clamped`
   list convention `set_params` already uses).
7. **`restore_viewport({viewport, previous})`** — `previous` is whatever shape lets a
   later call put the port back exactly (the same `zoom`/`closeup`/`zoom_x`/`zoom_y`
   tuple `_push_viewport` already reads is almost certainly sufficient — no new state
   needs inventing).
8. **`preview2`**: `viewport_not_active` error if the second window isn't open, same
   convention `capture_viewport` already uses (check its existing check, mirror it).

Full request JSON/response shapes, acceptance tests, and failure modes to avoid: see
"## Original bugreport" below, verbatim. Implement exactly what it specifies.

## Explicitly NOT this task's job

- Anything about `retouch_list_shapes`'s inconsistent display-frame values, or
  `dt_masks_get_image_size`'s fallback behavior — separate delegated task
  (`2026-07-31-mask-get-image-size-race-investigation.md`), touches the SAME
  `develop.c` file; coordinate serially if both land near each other, don't assume
  the other task's findings.
- `add_path_mask_in_viewport`/generic mask overlay rendering — separate delegated
  task (`2026-07-31-add-path-mask-viewport-and-overlay-design.md`).
- Fixing the two "silent bugs" (mask_object `mask_mode:0`, `set_module_mask` invert)
  from the same bugreport — already verified NOT reproducing on the current build
  (see `CLAUDE.md`'s 2026-07-31 entries); the reporting session was on a stale
  install. Nothing to do there.

## Deliverables

- New C binding(s) in `src-dt/src/lua/develop.c` (likely `set_viewport_cb` +
  `restore_viewport_cb`, or one function handling both via a mode flag — your call),
  registered in `dt_lua_init_develop`.
- Lua wrappers in `darktable-mcp/darktable_mcp/lua/darktable_mcp.lua`
  (`dev_set_viewport`, `dev_restore_viewport`).
- MCP tools in `darktable-mcp/darktable_mcp/server.py` (`set_viewport`,
  `restore_viewport`), full schema + description per the report's spec.
- Brace/paren balance check on `develop.c` after edits (this project's established
  gate — compare against the pre-existing baseline noted in `CLAUDE.md`, don't
  assume 0 imbalance is required, DO make sure you haven't introduced NEW imbalance).
- Unit tests: Lua dispatcher stub tests (`tests/lua/test_dispatcher.lua`, run via
  `lua tests/lua/test_dispatcher.lua` from `darktable-mcp/`), Python handler tests
  (`tests/test_server.py`), `EXPECTED_TOOLS` updates in both `test_server.py` and
  `test_honesty_pass_acceptance.py`.
- **Do NOT run `docker/build.sh`, `packaging-deb/*.sh`, or the docker bridge live
  test** — those touch shared `install/`/`dist/`/`run/` directories outside your
  worktree and will conflict with the other two delegated tasks running in parallel.
  Write the C/Lua/Python code and the full test suite; the main session will do the
  live rebuild + docker-bridge verification + packaging + commit/push after
  reviewing all three tasks' branches.
- Report back: what you implemented, what you verified via unit tests, what's
  UNVERIFIED because it needs a live darktable process (be specific — e.g. "the
  exact scale/closeup semantics inside `dt_dev_zoom_move`'s `DT_ZOOM_POSITION`
  branch were read but not exercised live"), and any place the report's spec
  couldn't be followed exactly (with why).

## Original bugreport (verbatim, follow this spec exactly)

> ## Summary
>
> `get_viewport` and `capture_viewport` are read-only. There is no way for an agent to
> **change** the darkroom zoom/pan. This blocks the retouch workflow the server already
> implements correctly, and pushes agents onto the low-level `retouch_add_shape` /
> `add_path_mask` path where they compute coordinates by hand and miss.
>
> Requested: a write counterpart, `set_viewport`, plus `restore_viewport`.
>
> ## Why this is the blocking gap (evidence from a real session)
>
> The viewport-based retouch loop is well designed and complete:
>
> ```
> capture_viewport  ->  retouch_add_shape_in_viewport(coordinate_space="snapshot_pixels")
>                   ->  return_preview verifies  ->  retouch_update_shape_in_viewport
> ```
>
> `retouch_add_shape_in_viewport` even rejects (rather than clamps) out-of-bounds points
> and binds the snapshot to the darkroom image. That is the right design.
>
> **But its precondition is unreachable by the agent.** Render detail is capped by the
> current GUI zoom, not by the `max_w`/`max_h` the agent asks for:
>
> | observed | value |
> |---|---|
> | requested | `capture_viewport(max_w=1400, max_h=1400)` |
> | returned render | `900 x 620` |
> | returned region | `{x: 0.0, y: 0.0926, w: 1.0, h: 0.4603}` |
> | full image | `3528 x 5455` |
>
> The retouch target (a face) spans ~0.18 of image width = **635 px at full resolution**
> but only **~162 px in the snapshot**. Heal radii that need to be 14–18 px at full
> resolution become **3.6–4.6 px** in the snapshot — below usable placement precision.
>
> The agent cannot fix this, because raising the zoom requires GUI input. On a Wayland
> session there is no out-of-band fallback either (no `xdotool`/`ydotool`; darktable runs
> as a native Wayland client, so synthesized input is not possible).
>
> Consequence: the agent either stops and asks the human to zoom, or falls back to
> `retouch_add_shape` with hand-computed mask-frame coordinates. That fallback is the
> actual source of the recurring "the shapes land in the wrong place" reports.
>
> ## Requested tool: `set_viewport`
>
> Declarative, region-first. The agent states *what it wants to see*, not zoom mechanics.
>
> ```jsonc
> {
>   "name": "set_viewport",
>   "params": {
>     "viewport":  { "enum": ["main", "preview2"], "default": "main" },
>
>     // Exactly one of: region | scale | mode
>     "region":    { "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0 },  // of the FULL processed frame
>     "scale":     1.0,                                          // 1.0 = 100% (1 image px : 1 screen px)
>     "mode":      { "enum": ["fit", "fill", "100%", "200%"] },
>
>     // Optional: guarantee usable detail. Server zooms in further if needed
>     // to make the requested region render at >= this many px across.
>     "min_render_px_across": 900,
>
>     "wait_for_pipe": { "type": "boolean", "default": true }
>   }
> }
> ```
>
> ### Response
>
> ```jsonc
> {
>   "ok": true,
>   "viewport": "main",
>   "achieved": {
>     "region": { "x": 0.612, "y": 0.208, "w": 0.190, "h": 0.123 },
>     "scale": 1.0,
>     "renderable_px": { "w": 670, "h": 671 }   // what capture_viewport will now return
>   },
>   "requested_region": { "x": 0.60, "y": 0.21, "w": 0.18, "h": 0.12 },
>   "aspect_adjusted": true,
>   "pipe_ready": true,
>   "previous": { "zoom_x": -0.03, "zoom_y": 0.12, "scale": 0.171, "zoom_level": "fit" }
> }
> ```
>
> ## Semantics (please implement exactly)
>
> 1. **`region` is in the same frame `get_viewport().region` and `get_preview(region=...)`
>    use** — the full *processed* (post-crop) frame, normalized 0..1, origin top-left.
>    Do not introduce a third frame. Say so explicitly in the tool description.
>
> 2. **Aspect mismatch: expand, never crop.** If the requested region's aspect differs
>    from the window's, enlarge the region minimally (centred) so the whole requested
>    region stays visible. Report the result in `achieved.region` and set
>    `aspect_adjusted: true`. An agent that asked to see a region must not get part of
>    it silently cut off.
>
> 3. **`wait_for_pipe: true` must block until the pixelpipe has actually reprocessed at
>    the new scale.** This is the most important requirement. If `set_viewport` returns
>    before the new render exists, the immediately following `capture_viewport` returns a
>    stale low-resolution render and the agent places shapes against the wrong detail
>    level — reintroducing the exact bug this tool is meant to remove. If a bounded wait
>    times out, return `pipe_ready: false` rather than pretending success.
>
> 4. **`renderable_px` must be truthful.** It is what `capture_viewport` will actually
>    return at this zoom, so the agent can decide whether to zoom in further. Today the
>    agent has no way to learn this except by calling `capture_viewport` and comparing
>    against what it asked for.
>
> 5. **Use free zoom, do not snap to presets.** Set an explicit scale
>    (`DT_ZOOM_FREE` + `dt_control_set_dev_zoom_scale`, with `dt_control_set_dev_zoom_x/y`
>    for the centre) rather than stepping through fit/1:1/2:1. The MCP already reads
>    `zoom_x` / `zoom_y` / `scale`, so the write path is the same state.
>
>    **[Investigation note: those exact function names don't exist — see "What already
>    exists" above for the real primitive, `dt_dev_zoom_move` with `DT_ZOOM_POSITION`.
>    The INTENT (explicit scale, no preset-snapping) still applies.]**
>
> 6. **Clamp and report.** Clamp `scale` to darktable's allowed range and report the
>    clamp in the response (same convention as `set_params`' `clamped` list). Never
>    silently ignore a request.
>
> 7. **`previous` enables politeness.** Return enough state to restore the user's
>    original view, and accept it back:
>
>    ```
>    restore_viewport({ viewport: "main", previous: <the object returned earlier> })
>    ```
>
>    Agents should restore the human's view when they finish. Without this, every agent
>    run leaves the user's darkroom zoomed into a random detail.
>
> 8. **`preview2`**: error with `viewport_not_active` if the second window is not open,
>    consistent with `capture_viewport`.
>
> ## Failure modes to avoid
>
> - Returning before the pipe is ready (see 3). Silent and very hard for an agent to detect.
> - Snapping to the nearest zoom preset, so `achieved.region` differs from the request
>   without `aspect_adjusted` explaining why.
> - Cropping the requested region to fit the window aspect.
> - Reporting `renderable_px` from the *request* rather than the resulting pipe.
>
> ## Acceptance tests
>
> 1. `set_viewport({region:{x:0.6,y:0.21,w:0.18,h:0.12}, min_render_px_across:800})`
>    then `capture_viewport()` → returned render is >= 800 px across and its `region`
>    matches `achieved.region` within 1 px.
> 2. Same call on a portrait image whose crop is non-trivial (e.g. `crop` with
>    `cx=0.0512, cy=0.0927`) → `achieved.region` is expressed in post-crop coordinates
>    and visually contains the requested area.
> 3. `set_viewport(...)` → immediately `capture_viewport()` → the render is **not** the
>    pre-call resolution (regression test for requirement 3).
> 4. `set_viewport({scale: 99})` → response contains a `clamped` entry, `ok: true`.
> 5. `set_viewport({viewport:"preview2"})` with the second window closed →
>    `viewport_not_active` error, no state change.
> 6. `restore_viewport({previous})` → `get_viewport()` matches the pre-change state.
> 7. End-to-end: with no human interaction, an agent can
>    `set_viewport(face region)` → `capture_viewport` → `retouch_add_shape_in_viewport`
>    → `retouch_render_overlay` → `retouch_update_shape_in_viewport` → `restore_viewport`.
>    Test 7 is the point of the whole request.

Tests 1-6 can be written as unit tests against mocked bridge calls (Python) and stub
C responses (Lua dispatcher). Test 7 needs a live darktable process — flag it as
UNVERIFIED in your report; the main session will run it after merging.
