# Viewport-relative retouch coordinates: design proposal

**Date:** 2026-07-25
**Status:** IMPLEMENTED (see Outcome below)
**Source:** bugreport "presna retus v oblasti zobrazenej v externom preview okne" (agent-authored spec, 14 sections)

## Outcome (2026-07-25)

Reporter approved with amendments: best-effort snapshot for v1, `require_target_inside_viewport`-equivalent containment (reject, not clamp) IN for v1, two-phase preview/commit OUT, radius must be documented as scaling by viewport render WIDTH only (never height/average).

One amendment past what the reporter and this doc agreed: the reporter's own recommendation to drop `retouch_update_shape_in_viewport` from v1 (delete+recreate is enough) was overridden by the project owner, who wanted a real in-place move (matching how dragging an existing shape already works in the darktable GUI). That reintroduces the one piece of C/Lua work this doc tried to avoid — `retouch_update_shape_cb` in `src-dt/src/lua/develop.c`, mirroring `circle.c`'s own drag-commit path (`dt_dev_add_masks_history_item` for geometry, not `dt_dev_add_history_item`). Shipped anyway since the UX case was clear.

All four tools (`capture_viewport`, `retouch_add_shape_in_viewport`, `retouch_update_shape_in_viewport`, `retouch_delete_shapes`) are implemented; see `CLAUDE.md`'s corresponding entry for file-level detail. Requires a `src-dt` rebuild (new C binding) + new `.deb`, still pending on the actual desktop deploy/test.

## Update: critical coordinate-frame bug found on first real test (agentic.12)

The first real desktop test (`capture_viewport("main")` -> `retouch_add_shape_in_viewport`) placed heal points on the chest instead of the neck — reproducing even with the FULL, uncropped `main` region, ruling out a region-math bug. Real cause: this design's `viewport_coords.py` only converts viewport-local coordinates into `get_viewport()`/`get_preview()`'s PROCESSED/display frame — it silently assumed that frame was the same one `retouch_add_shape`/`add_path_mask` store points in. It is not: darktable's masks (circle/path/brush/gradient/ellipse) are normalized against the PIPE-INPUT frame (near-raw, pre-crop/rotate/orientation), and these two frames diverge as soon as orientation/crop/rotate/lens-correction is active — orientation alone (a portrait EXIF tag) is enough, no crop needed.

This was section 11 of the *original* bugreport spec ("critical detail... does retouch use raw, post-orientation, or post-crop coordinates? MCP has to fully hide this abstraction") — flagged by the reporter, but incorrectly dismissed during design review as already covered by "existing tools share this frame by convention." It wasn't verified against live darktable, and it turned out false.

Fix: a second pipeline stage, a new C binding `dt.develop.backtransform_point` (mirrors what every masks GUI drag-commit handler already does — `dt_masks_get_image_size` + `dt_dev_distort_backtransform`), inserted between the existing display-frame conversion and the call into `retouch_add_shape`/`retouch_update_shape`. Also fixed a secondary, lower-priority bug from the same report: `retouch_delete_shapes` batch-deleting could false-negative ("no shapes group") when an earlier delete in the same batch tore down the shared mask group — fixed with an idempotency re-check. Full detail: `CLAUDE.md`'s "KRITICKY bug po realnom otestovani" entry and `PLAN.md`'s matching addendum. Rebuilt as `darktable-agentic.12` + `darktable-mcp.18`.

Known NOT fixed (flagged, out of scope for this bug): `add_path_mask`/`mask_object` have the same latent frame bug when a caller derives points from a cropped/rotated image's render directly — they just haven't hit it yet by coincidence.

## Summary

The reporter's spec is right about the core problem: `retouch_add_shape` takes full-image-normalized coordinates, but an agent working from a `get_preview(region=...)` crop has to do its own affine remap by hand, which is fragile (stale viewport, no atomic snapshot, easy off-by-one). We agree with the diagnosis and want to fix it. We do **not** propose building all 14 sections — most of the value is in 4 tools (the report's own section 12 "minimal scope"). This doc explains what we'd build, what we'd defer, and why, so the reporter can push back before we write code.

## What already exists (investigated, not assumed)

- `get_viewport()` returns a `region {x,y,w,h}` (normalized 0..1, top-left origin) for `main` and `preview2`, already in the same coordinate frame as `retouch_add_shape`'s `target`/`source`/`radius`. Real math lives in `_push_viewport()`, `src-dt/src/lua/develop.c:1317-1365`.
- `get_preview(region=...)` renders that same normalized frame to a PNG. Region validation/crop math: `develop.c:1729-1889`.
- `retouch_add_shape` writes `target`/`source`/`radius`/`feather` as full-image-normalized directly into `rt_forms[]` (the already-shipped "direct-write" bypass of the GUI resync path). `develop.c:2190-2320`.
- **No existing code converts a point from "local pixel position inside a rendered crop" back to full-image-normalized.** Every current tool either requires the caller to already have full-image-normalized coordinates, or (see `mask_object`) dodges the problem by always rendering full-frame.
- **No revision/generation/timestamp token is exposed anywhere at the MCP or Lua boundary.** `get_viewport()` does a fresh, uncached GUI-thread read every call — there's no snapshot concept today.

This confirms the reporter's section 3 and section 11 diagnosis: the transform is missing, and yes, the server (not the client) should own it.

## Proposed scope: 4 tools, pure Python, no C rebuild

1. **`capture_viewport(viewport, max_w?, max_h?)`**
   Calls existing `dev_get_viewport` + `dev_preview` bridge methods, returns a `snapshot_id` (opaque, server-side in-memory dict — not a real atomic transaction, see caveat below), `region`, and the render (path/dims).

2. **`retouch_add_shape_in_viewport(snapshot_id | viewport, target, source, radius, ..., coordinate_space)`**
   Accepts `viewport_normalized` or `viewport_pixels` coords relative to a captured snapshot (or a fresh `viewport` region if no snapshot given). Converts server-side:
   ```
   full_x = region.x + local_x * region.w        # local_x in 0..1
   full_r = local_r * region.w                   # radius scales by region width
   ```
   then calls the existing (unchanged) `retouch_add_shape` bridge path. Returns `resolved_image_coordinates` alongside the input, per report section 2.

3. **`retouch_update_shape_in_viewport(formid, ...)`** — same transform, targets an existing `formid`. Requires a `retouch_update_shape` Lua/C bridge method, which does not exist today (only add/delete/list) — this is new C+Lua surface, see below.

4. **`retouch_delete_shapes(formids: list[int])`** — batch delete. Today's `retouch_delete_shape` (singular) exists; batching it is a thin Python-side loop, no new bridge call needed unless we want atomicity (all-or-nothing), which we don't think is warranted here.

All four reuse `get_viewport`'s existing region math — no changes to `src-dt/src/lua/develop.c`'s coordinate model, just a new Python-side affine helper shared by all four tools (mirrors how `_push_viewport` is already shared between `main`/`preview2` in C).

## What we're deliberately NOT building (and why)

- **Two-phase `retouch_preview_shape` / `retouch_commit_proposal` (report section 8).** Nice for interactive human use, but for an agent the same effect is achieved today by: call `capture_viewport` → call `retouch_add_shape_in_viewport` → call `capture_viewport` again to inspect the result → `retouch_delete_shapes([formid])` if wrong. No new server-side "proposal" state needed; the agent already has the primitives to simulate this workflow itself.
- **Diff rendering / `changed_bbox` / `changed_pixel_count` (section 6).** Real value, but is its own separate feature (pixel-diff of two PNGs) orthogonal to the coordinate-transform problem this bugreport is actually about. Would rather scope that separately if wanted.
- **`require_target_inside_viewport` hard containment check (section 7).** Reasonable safety net; deferred only because it's a small independent addition we can bolt on after the base transform ships and gets used once. Flag if reporter wants it in v1.
- **Atomic C-side snapshot with a real revision token (section 3's "atomic snapshot" concern, section 11).** This is the one place we're pushing back hardest. A truly race-free `capture_viewport` needs a new combined C callback in `develop.c` (grab viewport state + trigger preview render in one GUI-thread transaction) plus exposing `dev->timestamp` as a token — real C code, a `darktable` rebuild, and a new `.deb`. Given this is a private single-user dev tool (no concurrent editors), we propose a **best-effort** snapshot instead: `capture_viewport` does two sequential bridge calls (`dev_get_viewport` then `dev_preview` using that region) and stamps the result with a hash of the region + a wall-clock timestamp as `revision`. If the user pans/zooms between those two calls (milliseconds apart, no human input event loop runs between them since both are synchronous bridge round-trips), the render and region could theoretically mismatch — but this requires the user to physically move the viewport during a single tool call, which is not a realistic scenario for this tool's actual usage pattern (agent-paced, not live human dragging). If reporter disagrees and wants the atomic guarantee for v1, say so and we'll scope the C work.
- **Four parallel coordinate spaces at every call site (`viewport_normalized`, `viewport_pixels`, `image_normalized`, `image_pixels`) — section 4.** We'll support `viewport_normalized` and `viewport_pixels` (the two the report itself says matter most for agent use, section 4 closing line). `image_normalized`/`image_pixels` add nothing new since `retouch_add_shape` already accepts full-image-normalized directly — an agent that already has full-image coordinates just calls the existing tool.
- **`viewport` as strict enum with `viewport_not_active` error struct, window dims, letterbox/DPR reporting (section 10).** `get_viewport` already returns `preview2: {active: false}` when the second window isn't open — we'll reuse that signal (return an error if the caller asks for a snapshot of an inactive `preview2`) rather than inventing a parallel status schema.

## New C/Lua surface required (small, unavoidable)

Only one genuinely new binding: `retouch_update_shape` (target/source/radius/opacity edit of an existing `formid`, writing into the same `rt_forms[]` slot found by formid instead of allocating a new one). Everything else in the 4-tool scope is Python-only, reusing bridge methods that already exist (`dev_get_viewport`, `dev_preview`, `dev_retouch_add_shape`, `dev_retouch_delete_shape`).

## Open questions for reporter

1. Is best-effort (non-atomic) snapshot acceptable for v1, or is the race window a real concern given actual usage (do you ever pan/zoom mid-tool-call, e.g. via a second automation)?
2. Do you need `retouch_update_shape_in_viewport` in v1, or would delete-and-recreate (already possible with tools 1/2/4 above) cover your actual workflow?
3. Is `require_target_inside_viewport` (hard containment check) a v1 must-have, or fine as a fast-follow?
4. Any objection to dropping the two-phase preview/commit flow in favor of "capture, add, capture again, delete-if-wrong" as the agent's own loop?

## If approved

Estimated surface: ~3 new MCP tools + 1 batch-ified existing one, 1 new Lua/C binding (`retouch_update_shape`), 1 shared Python transform helper, test updates to `tests/test_server.py`, `tests/test_honesty_pass_acceptance.py` (EXPECTED_TOOLS set), `tests/lua/test_dispatcher.lua`. Requires a `src-dt` rebuild + new `.deb` only for the `retouch_update_shape` C binding — everything else is Python-only and could ship in the existing `.deb` build pipeline used for the v1.0.16 fix.
