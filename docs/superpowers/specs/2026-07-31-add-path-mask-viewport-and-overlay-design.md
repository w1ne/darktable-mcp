# add_path_mask_in_viewport + generic mask overlay renderer: design + implementation task

**Date:** 2026-07-31
**Status:** DELEGATED — implement in an isolated worktree, report back before merge
**Source:** bugreport secondary item C: "add_path_mask has no viewport variant, and drawn
masks have no overlay renderer"

## Why this exists

`retouch` has the full see-then-place kit: `capture_viewport` ->
`retouch_add_shape_in_viewport` -> `retouch_render_overlay` -> `retouch_update_shape_in_viewport`.
Generic drawn masks (`add_path_mask`, and by extension `mask_object`'s internal path
write) have NEITHER a viewport-relative create call NOR a way to visually verify
placement. An agent positioning a path mask today must hand-derive the mask-storage-
frame transform (same class of bug already documented for retouch before its
viewport kit shipped — see `docs/superpowers/specs/2026-07-25-viewport-relative-
retouch-design.md` and its "critical coordinate-frame bug" update). Bring drawn
masks up to the same standard.

## What already exists (investigated, not assumed) — most of the hard work is done

This task is **smaller than it looks** because two of its three building blocks
already ship, from earlier work in this same session:

1. **Polygon backtransform, already written**: `_backtransform_polygon_to_mask_space`
   (`darktable-mcp/darktable_mcp/server.py:4265`) converts a list of DISPLAY-frame
   `{x,y}` points into the PIPE-INPUT/mask-frame points `add_path_mask` actually
   stores, one `dev_backtransform_point` bridge call per vertex. Written for
   `mask_object`'s own internal flow (`server.py:5830` onward) — reuse it verbatim,
   don't reimplement.
2. **Viewport-local -> display-frame, already written**: `viewport_point_to_image`/
   `viewport_radius_to_image` (`darktable-mcp/darktable_mcp/utils/viewport_coords.py`)
   — the exact Stage-1 conversion `retouch_add_shape_in_viewport` uses per point
   (`server.py:5185-5191`). A polygon is just N points through the same function.
3. **Mask geometry read-back, already shipped (mcp 1.0.31)**: `get_mask_geometry`
   (`dt.develop.get_mask`, wired via `dev_get_mask`) returns every path node's
   `corner`/`ctrl1`/`ctrl2`/`border`/`state` in mask-frame coordinates. This is
   the data source a generic overlay renderer needs — no new C required for the
   READ side either.
4. **The overlay-drawing/compositing code to mirror**: `retouch_overlay.py`
   (`darktable_mcp/tools/retouch_overlay.py`, used by `retouch_render_overlay`,
   `server.py:5582`) already does PIL-based drawing of shape geometry onto a
   captured snapshot render, with `highlight_formid`, `label_shapes`, multiple
   `mode`s. A generic version needs to draw PATH polygons (from `get_mask_geometry`,
   forward-transformed mask-frame -> display-frame -> viewport-local, the mirror
   image of building block #1/#2) instead of retouch's circles — same
   compose/label/highlight scaffolding, different geometry source and a Bezier-vs-
   corner-node rendering step `retouch_overlay.py` doesn't need (circles have no
   node/control-point distinction).

**Net effect: this task may be almost entirely Python, reusing existing helpers +
one existing C binding (`get_mask` for reading geometry back, already shipped) —
confirm during implementation whether ANY new C is actually needed (e.g. for a
forward mask-frame -> display-frame point transform for the overlay's read side;
check whether `dt.develop.transform_point`, mentioned in `develop.c`'s history
around `retouch_list_shapes_cb`'s `*_display` fields, already covers this before
assuming new C work).**

## Scope

### 1. `add_path_mask_in_viewport(snapshot_id, points, ...)`

Mirror `retouch_add_shape_in_viewport`'s exact structure (`server.py:5164-5283`):
- Same `capture_viewport` snapshot binding + same-image guard
  (`_snapshot_image_guard`, `server.py:4109`) — refuse if darkroom moved on.
- Stage 1: each `{x,y}` in `points` -> display-frame via `viewport_point_to_image`
  (loop, one call per vertex — a polygon, not a single point/radius like retouch).
- Stage 2: the whole display-frame polygon -> mask-frame via
  `_backtransform_polygon_to_mask_space` (already handles N-point polygons).
- Call existing `dev_add_path_mask` (unchanged) with the mask-frame polygon.
- Same `opacity`/`feather`/`smooth`/`name` passthrough `add_path_mask` already
  supports (see `add_path_mask`'s current schema, `server.py` — `name` shipped
  2026-07-31, reuse it here too for consistency).
- Response mirrors `retouch_add_shape_in_viewport`'s: report input/display-frame/
  mask-frame for each point (or at minimum a bbox of each, to avoid an enormous
  response for a 40+ node polygon — use judgment), plus a hint pointing at the
  overlay renderer from item 2 for verification.

### 2. Generic overlay renderer

Name it `render_module_mask(op, instance, snapshot_id, mode?)` (matches the
report's suggested signature) or fold it into `retouch_render_overlay` as a new
`shape_source` param if that's a cleaner fit once you're in the code — your call,
but pick ONE tool users need to learn, not both a generic-with-flag AND a separate
retouch-specific one going forward (retouch's existing tool can stay as its own
thing; don't break it).

- Reads the target module (op, instance)'s drawn masks via `list_masks` (per-module,
  already shipped) to get formids, then `get_mask_geometry` per formid for actual
  node coordinates.
- Forward-transforms mask-frame -> display-frame -> viewport-local (inverse of
  building blocks #1/#2 above) for each node.
- Draws polygons (respecting each node's smooth-vs-corner `state`, per
  `get_mask_geometry`'s docstring: `ctrl1`/`ctrl2` equal to `corner` = corner node,
  differing = smooth/Bezier) onto the snapshot render, same
  compose/label/highlight/mask_only conventions `retouch_overlay.py` already
  established.
- Same same-image guard as `retouch_render_overlay`.

## Explicitly NOT this task's job

- `set_viewport`/`restore_viewport` — separate delegated task
  (`2026-07-31-set-viewport-design.md`), touches the SAME `server.py`/
  `darktable_mcp.lua`; if both land near each other, coordinate serially rather
  than assuming the other task's exact diff.
- `dt_masks_get_image_size`'s fallback race — separate delegated task
  (`2026-07-31-mask-get-image-size-race-investigation.md`). If this task's forward-
  transform work depends on the SAME `dt_masks_get_image_size`-derived display
  dimensions and hits the same inconsistency, note it in your report rather than
  trying to fix it here.
- The two "silent bugs" (mask_object `mask_mode:0`, `set_module_mask` invert) —
  already verified NOT reproducing on the current build; nothing to do.

## Deliverables

- `add_path_mask_in_viewport` + `render_module_mask` (or your chosen consolidation)
  in `server.py`, tool schemas with descriptions that state the coordinate frame
  explicitly (this bugreport's item B is about exactly this class of ambiguity —
  don't reintroduce it in a new tool).
- Any new small C binding ONLY if you confirm (per the investigation note above)
  that no existing forward-transform primitive covers the overlay's read side —
  if you do add one, mirror `backtransform_point_cb`'s existing pattern
  (`develop.c`), check brace/paren balance against the established baseline, and
  say so clearly in your report (the main session needs to know whether a
  `src-dt` rebuild is required before merging this branch).
- Unit tests: Python handler tests (mocked bridge, following the existing
  `retouch_add_shape_in_viewport`/`retouch_render_overlay` test patterns in
  `tests/test_server.py`), Lua dispatcher stub tests if any new bridge method was
  added, `EXPECTED_TOOLS` updates in `test_server.py` + `test_honesty_pass_acceptance.py`.
- **Do NOT run `docker/build.sh`, `packaging-deb/*.sh`, or the docker bridge live
  test** — shared directories, will conflict with the other two parallel tasks.
  Write code + full unit test suite only; the main session does live verification
  + packaging + commit/push after reviewing all three branches.
- Report back: what got implemented, whether new C was actually needed (and why/
  why not), what's verified via unit tests vs. what needs a live darktable process
  to confirm (be specific about which claim is unverified).
