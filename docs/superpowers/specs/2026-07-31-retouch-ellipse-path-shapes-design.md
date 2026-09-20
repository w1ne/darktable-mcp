# retouch ellipse + path shapes: design

**Date:** 2026-07-31
**Status:** SPEC — implement after review, not delegated (single-file C change, moderate risk)
**Source:** follow-up to bod #5 (blur/fill algorithms, shipped agentic.22/mcp 1.0.35) — the
remaining deferred half: `retouch_add_shape`/`update_shape`/`list_shapes` only build/read
`DT_MASKS_CIRCLE` forms; the retouch GUI itself also supports path/ellipse/brush.

## Why this exists

Circle-only retouch is fine for spot fixes (dust, small blemishes) but wrong for anything
elongated or irregular — a scar, a stray hair strand, a crease that isn't round. The GUI already
lets a user pick path/ellipse/brush for a retouch shape (`iop/retouch.c:1006-1013`,
`bt_path`/`bt_circle`/`bt_ellipse`/`bt_brush` toggle buttons); the MCP binding never exposed
that choice.

## What already exists (investigated, not assumed) — the pixel pipeline needs ZERO changes

This is the single most important finding and it shrinks the task a lot:

1. **`rt_process_forms` (`iop/retouch.c:3650+`) is already shape-agnostic.** It calls
   `dt_masks_get_mask(self, piece, form, &mask, ...)` — darktable's own generic mask
   rasterizer — regardless of `form->type`. Heal/clone/blur/fill dispatch
   (`iop/retouch.c:3792-3835`) branches on `p->rt_forms[index].algorithm`, never on shape type.
   **No `iop/retouch.c` change needed at all.**
2. **The source/delta calculation is already shape-agnostic too.**
   `rt_masks_get_transform_to_destination` (`iop/retouch.c:848-892`) explicitly branches
   `DT_MASKS_PATH`/`DT_MASKS_BRUSH`/`DT_MASKS_CIRCLE`/`DT_MASKS_ELLIPSE`, but all four branches
   do the exact same thing: `rt_masks_get_transform_to_destination` computes `dx,dy` from ONE
   reference point (`form->points->data`'s `corner` for path/brush, `center` for circle/ellipse
   — i.e. the FIRST node in the point list for path/brush) versus `form->source`. **`form->source`
   is a single absolute point for every shape type, including path** — not per-vertex, not a
   centroid. This is the load-bearing fact for the API design below: a path retouch shape takes
   exactly one `source` point, the same as circle, interpreted as "sample offset relative to the
   path's first node."
3. **Path point-building code already exists and is directly reusable.**
   `add_path_mask_cb` (`src/lua/develop.c:2853+`) already builds a `DT_MASKS_PATH` form from a
   flat `{x,y}` point list: reads points into a C array, computes a bbox-relative feather,
   optionally derives Catmull-Rom bezier control points for a smooth boundary
   (`_path_catmull_to_bezier`'s formula, inlined), and appends `dt_masks_point_path_t` nodes.
   The ONLY difference retouch needs: `dt_masks_create(DT_MASKS_PATH | DT_MASKS_CLONE)` (or
   `NON_CLONE` for blur/fill) instead of plain `DT_MASKS_PATH`, plus setting `form->source`
   (which generic `add_path_mask` never does — it has no source concept).
4. **Ellipse is a simple struct, no reuse needed, no existing code to crib beyond the pattern
   already used for circle**: `dt_masks_point_ellipse_t` (`develop/masks.h:154-161`) is
   `center[2]`, `radius[2]` (two independent radii, not one — an ellipse, not a circle-with-
   uniform-radius), `rotation` (degrees), `border` (single float, like circle's), `flags`
   (`DT_MASKS_ELLIPSE_EQUIDISTANT` = 0 default / `DT_MASKS_ELLIPSE_PROPORTIONAL` = 1 — border
   added as a constant width vs. scaled per-axis; default EQUIDISTANT matches circle's own
   border semantics, use that unless a caller asks otherwise).
5. **Coordinate-frame handling (mask-frame vs display-frame, viewport-relative capture) is
   already generalized to N-point geometry.** `_backtransform_polygon_to_mask_space`
   (`darktable-mcp/server.py`, written for `mask_object`/`add_path_mask_in_viewport`) already
   takes a list of display-frame `{x,y}` points and returns mask-frame points, one
   `dev_backtransform_point` bridge call per vertex. An ellipse is just 2 points through the
   same machinery (center, and center+radius-offset for each axis — same "two-point radius
   trick" `set_viewport`/circle retouch already uses via `dt.develop.backtransform_point`'s
   `len1`/`len2` args). A path is N points, already exactly what that helper does today.

**Net effect: no `iop/retouch.c` change, no new mask-rasterization code, no new coordinate-frame
math.** The work is entirely in the THREE existing Lua bindings
(`retouch_add_shape_cb`/`retouch_update_shape_cb`/`retouch_list_shapes_cb`, all in
`src/lua/develop.c`) generalizing their circle-only geometry read/write to branch on shape type,
plus the matching Lua wrapper (`darktable_mcp.lua`) and Python tool schema/handler
(`server.py`) changes.

## Scope

### 1. `shape_type` param on `retouch_add_shape` (and `retouch_add_shape_in_viewport`)

New optional `shape_type` (`"circle"` default, `"ellipse"`, `"path"`) alongside the existing
`algorithm`. C-side `retouch_add_shape_cb` branches on it:

- **circle** (existing, unchanged): `target{x,y}`, `radius`, `feather` as today.
- **ellipse**: new `target{x,y}` (center), `radius_a`, `radius_b` (two independent radii,
  normalized the same `mindim(iwidth,iheight)` convention circle's radius already uses —
  confirm by reading `ellipse.c`'s own normalization before committing to this, don't assume
  it matches circle's 1:1), `rotation` (degrees, default 0), `feather` (border, same convention
  as circle's `feather` today). `dt_masks_create(DT_MASKS_ELLIPSE | DT_MASKS_CLONE/NON_CLONE)`,
  build one `dt_masks_point_ellipse_t`.
- **path**: new `points` (array of `{x,y}`, min 3 — same validation `add_path_mask_cb` already
  does), `smooth` (optional bool, default true — same Catmull-Rom-or-corner choice
  `add_path_mask` exposes), `feather` (bbox-fraction convention, matching `add_path_mask_cb`'s
  existing `feather` semantic — NOTE this is a DIFFERENT feather convention than circle
  retouch's absolute-normalized `feather`; document this divergence explicitly in the tool
  description so it isn't a silent surprise). `target`/`radius` args are ignored (or rejected —
  decide during implementation which is less surprising; leaning toward rejecting an
  irrelevant arg rather than silently ignoring it, consistent with this codebase's existing
  "refuse over silently-wrong" pattern used elsewhere, e.g. LUT blend-opacity masked-refusal).
  `dt_masks_create(DT_MASKS_PATH | DT_MASKS_CLONE/NON_CLONE)`, reuse `add_path_mask_cb`'s
  point-building loop verbatim (extract to a shared static helper rather than copy-pasting —
  both call sites need identical bezier-handle math).
- **`source`** requirement is UNCHANGED by shape_type: still required for heal/clone, still
  optional/ignored for blur/fill, regardless of whether the shape is circle/ellipse/path (see
  finding #2 above — `form->source` is always a single point, never per-vertex).

### 2. `retouch_update_shape_cb` generalization

Currently hardcodes `dt_masks_point_circle_t *circle = form->points->data` and writes
`circle->center/radius/border` unconditionally (`develop.c:3550-3555` current HEAD). Needs to
branch on `form->type` the same way `retouch_list_shapes_cb` already partially does (see below)
and mutate the right struct fields in place. Ellipse: mutate `center/radius/rotation/border`
in place (single node, same "in-place move" semantics as circle). Path: **decide during
implementation** whether "update" replaces the ENTIRE point list (matching the "move, not a
patch" philosophy already documented for circle's target/source) or is out of scope for v1
(only circle/ellipse support update; path shapes must be deleted+recreated to move) — replacing
a whole polygon in place is more code (free old `GList`, rebuild) for a shape type that's
presumably repositioned less often than a spot circle. Leaning toward **deferring path support
in `retouch_update_shape` to a v2** if this meaningfully simplifies v1 — flag this trade-off
back to Roman before implementing rather than assuming either way.

### 3. `retouch_list_shapes_cb` generalization

Already has a `shape_type` branch (`develop.c` current HEAD, the `if(form->type &
DT_MASKS_CIRCLE) ... else { "unknown" }` block) — currently reports `"unknown"` for any
non-circle shape without crashing (defensive, already correct). Extend the `else` branch: add
`DT_MASKS_ELLIPSE` (report `center`/`radius_a`/`radius_b`/`rotation`/`border`, same
`*_display` dual-frame convention circle already gets via `_mask_point_to_display`/
`_mask_len_to_display` — ellipse needs the SAME two-point radius trick for `radius_a`/
`radius_b` independently, since an ellipse's two axes can transform to different display-frame
lengths under a non-uniform crop/aspect change) and `DT_MASKS_PATH` (report the full point list
— `corner`/`ctrl1`/`ctrl2`/`border`/`state` per node, mirroring `get_mask`'s existing geometry
read shape, since a path with 20+ nodes returned inline on every `retouch_list_shapes` call is
a lot of payload — consider whether path shapes should report a bbox + node COUNT by default
with full geometry behind an opt-in flag, matching the "avoid an enormous response" judgment
call already made for `add_path_mask_in_viewport`'s spec).

### 4. Python side (`server.py`, `darktable_mcp.lua`)

- `dev_retouch_add_shape` (Lua wrapper): forward `shape_type`/`radius_a`/`radius_b`/`rotation`/
  `points`/`smooth` alongside existing args. Validate shape-specific requirements client-side
  where cheap (e.g. `points` min-3 check) before the C round-trip, same pattern as existing
  validation.
- `retouch_add_shape`/`retouch_add_shape_in_viewport` MCP tool schemas: `shape_type` enum
  `["circle","ellipse","path"]`, new conditional properties (`radius_a`/`radius_b`/`rotation`
  for ellipse, `points`/`smooth` for path) — mirror how `blur_type`/`fill_mode` etc. were added
  as "only used when algorithm=X" optional properties in the blur/fill batch; same pattern for
  "only used when shape_type=Y".
- Viewport-relative path: `retouch_add_shape_in_viewport` needs a NEW per-point backtransform
  loop for `shape_type="path"` (reuse `_backtransform_polygon_to_mask_space` — already written,
  already generic over point count) instead of the current single-target-plus-single-source
  two-call pattern. Ellipse needs THREE backtransform calls (center, center+radius_a-offset,
  center+radius_b-offset) instead of circle's two (center, center+radius-offset) — write this
  as a small dedicated helper, don't bolt it onto `_backtransform_to_mask_space` which is
  currently circle-shaped (target+source+radius+feather) and would get harder to read carrying
  three geometry variants.

## Explicitly out of scope for this task

- **Brush** (`DT_MASKS_BRUSH`) — per-vertex variable border (`dt_masks_point_brush_t`'s
  `density`/`hardness`), which is meaningful for a mouse dragging a pressure-sensitive stroke
  and not obviously meaningful for an API caller to specify per-node. Revisit only if a real
  use case shows up; the ellipse/path pair covers "elongated" and "irregular outline", which is
  most of what circle-only currently can't do.
- **`iop/retouch.c` changes** — per finding #1/#2, none are needed. If implementation
  discovers this assumption is wrong for some edge case (e.g. `distort_mode` handling differs
  per shape type in a way not visible from the code read so far), STOP and report back rather
  than patching around it — this spec's entire risk reduction rests on the pixel pipeline
  being genuinely shape-agnostic already.
- **`retouch_render_overlay`** ellipse/path drawing — `retouch_overlay.py` currently only draws
  circles (`_shape_pixels`, target/radius/feather). Extending it to draw ellipses (rotated) and
  path polygons (bezier-aware, mirroring `add_path_mask`'s planned generic overlay from the
  companion spec `2026-07-31-add-path-mask-viewport-and-overlay-design.md`) is real follow-up
  work but not required for `retouch_add_shape` itself to function — flag as a fast-follow, not
  a blocker.

## Suggested implementation order (cheapest/lowest-risk first)

1. Ellipse create (single struct, no point-list complexity) — `retouch_add_shape_cb` only,
   circle-shaped code to copy.
2. Ellipse list/update — extend the existing branch, mirror circle's dual-frame reporting.
3. Path create — extract `add_path_mask_cb`'s point-building loop into a shared static helper,
   call it from `retouch_add_shape_cb` too.
4. Path list (geometry read) — decide the payload-size trade-off (full nodes vs. bbox+count)
   before writing code, not after.
5. Path update — decide in-place-replace vs. defer-to-v2 (see §2) before writing code.
6. Python/Lua wiring for whichever of the above landed in C.
7. Viewport-relative variants (`*_in_viewport`) last, once the non-viewport path is proven live
   against a real darktable (same order this session followed for circle blur/fill: raw tool
   first, viewport wrapper after).

Each numbered step is independently mergeable/testable — stop after any step and ship if time
runs out, rather than holding the whole thing for a single big-bang merge.
