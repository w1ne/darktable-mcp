# dt_masks_get_image_size fallback race: investigation + fix task

**Date:** 2026-07-31
**Status:** DELEGATED — investigate + fix in an isolated worktree, report back before merge
**Source:** bugreport secondary item A: "retouch_list_shapes reports inconsistent
display frame values — data bug"

## The reported symptom

Same `formid`, same image, same crop, two `retouch_list_shapes` calls in one
session:

| call | mask frame target | reported display frame target |
|---|---|---|
| earlier | `(0.54146, 0.68080)` | `(0.71828, 0.40275)` |
| later | `(0.54146, 0.68080)` | `(0.68080, 0.45653)` |

The first is consistent with the active `crop` (`cx=0.0512, cy=0.0927`). The second
**ignores the crop** — it's the bare rotation: `display.x == mask.y` exactly,
`display.y ≈ 1 - mask.x`. Same input, same formula call site, different output
within one session, no error raised either time. That's the part that makes this a
real bug rather than a one-off misread: the SAME code path returned a correct value
once and a wrong one later.

## Hypothesis, grounded in the actual source (investigated, not assumed — verify
## before trusting it further, this is a hypothesis to test, not a confirmed root cause)

`_mask_point_to_display`/`_mask_len_to_display` (`src-dt/src/lua/develop.c:1593+`,
used by `retouch_list_shapes_cb`, `backtransform_point_cb`, `transform_point_cb`)
all take `wd, ht, iwidth, iheight` from the caller, which gets them from:

```c
// src-dt/src/develop/masks.h:1225
static inline void dt_masks_get_image_size(float *width, float *height,
                                           float *iwidth, float *iheight)
{
  const dt_develop_t *dev = darktable.develop;
  const dt_dev_pixelpipe_t *preview = dev->preview_pipe;
  const float iscale = preview->iscale > 0.f ? preview->iscale : 1.f;

  if(preview->processed_width > 0)
  {
    // PRIMARY path: dev->preview_pipe's own processed size.
    // preview_pipe renders the WHOLE image (the darkroom navigation
    // thumbnail) -- always full-frame, independent of main-view zoom/pan.
    *width = preview->processed_width;
    *height = preview->processed_height;
  }
  else if(dev->full.pipe && dev->full.pipe->processed_width > 0)
  {
    // FALLBACK path: dev->full.pipe's processed size / iscale.
    // dev->full.pipe is the MAIN CENTER-VIEW pipe -- when the user is
    // zoomed in (not "fit"), its processed_width/height can reflect only
    // the currently VISIBLE sub-region, not the full image.
    *width = dev->full.pipe->processed_width / iscale;
    *height = dev->full.pipe->processed_height / iscale;
  }
  else { /* backbuf fallback, third tier */ }

  *iwidth = preview->iwidth;   // always from preview_pipe, no fallback here
  *iheight = preview->iheight;
}
```

**The suspected bug**: the fallback branch fires whenever `preview_pipe-
>processed_width <= 0` at the exact moment this function is called (e.g. the
preview pipe is mid-reprocess, was just invalidated by an edit, or hasn't run yet
right after opening/switching an image). When it fires, `width`/`height` come from
`dev->full.pipe` instead — and if the user (or another MCP call) was zoomed in on
the main view at that moment, `full.pipe->processed_width/height` reflects the
VISIBLE SUB-REGION, not the full processed frame. `_mask_point_to_display` then
divides by these wrong (too-small, viewport-scoped) dimensions, producing a display-
frame value that looks like a bare rotation with no crop offset applied — matching
the reported symptom's second call exactly.

**This is plausible but UNCONFIRMED — your job is to confirm or refute it before
fixing anything.** Specifically verify:
1. Does `dev->full.pipe->processed_width/height` actually shrink to a sub-region
   size when zoomed in (not fit), or does it always represent the full processed
   frame regardless of zoom (in which case this hypothesis is wrong and the bug is
   elsewhere)? Read `dt_dev_process_image_job`/wherever `full.pipe->processed_width`
   gets set, and how it relates to `dt_dev_get_zoom_bounds`/the viewport region math
   documented in `_push_viewport`'s comment (`develop.c:1340-1379`).
2. Can you reproduce `preview_pipe->processed_width <= 0` on demand (e.g. right
   after `open_darkroom`, before any `get_preview`/`capture_viewport` call has run)
   and confirm the fallback branch actually fires and produces a wrong value in that
   window?
3. Is there a THIRD explanation you find during investigation that fits the data
   better? Don't force-fit the hypothesis above if the evidence points elsewhere.

## Blast radius warning — this is core darktable code, not agentic-specific

`dt_masks_get_image_size` is called from `circle.c`, `path.c`, `brush.c`,
`ellipse.c`, `gradient.c`, `object.c`, `group.c` — every mask type's GUI rendering,
not just the MCP bridge bindings. **Do not "fix" `dt_masks_get_image_size` itself**
unless you are confident the fix is correct for ALL of those call sites, most of
which this task has no way to exercise/test (they're GUI mouse-drag paths). A
narrower, safer fix scoped to the MCP-facing bindings specifically is very likely
the right call — see "Suggested fix shape" below.

## Suggested fix shape (narrow, not touching the shared function)

In the MCP-facing callers that currently trust `dt_masks_get_image_size`'s output
whenever the four values are positive (`backtransform_point_cb`,
`transform_point_cb`, `retouch_list_shapes_cb`'s `*_display` fields) — add an
explicit freshness check BEFORE calling it: confirm
`dev->preview_pipe && dev->preview_pipe->processed_width > 0` directly yourselves,
and if that's false, do NOT fall through to trusting whatever
`dt_masks_get_image_size` returns (even though it will return non-zero numbers via
its own fallback) — instead:
- For `retouch_list_shapes_cb`: omit the `*_display` fields for this call (same
  precedent the existing code already uses for genuinely unavailable dims — see the
  `have_display` boolean and its comment at `develop.c:2868-2874`, "*_display field
  is simply omitted rather than reported as a bogus 0" — extend that same
  philosophy to "available but possibly wrong", not just "unavailable").
- For `backtransform_point_cb`/`transform_point_cb`: return an explicit error
  (`"preview pipe not ready yet, retry"` or similar) instead of the current guard,
  which only checks `wd <= 0 || ht <= 0 || ...` (catches "unavailable", does NOT
  catch "available via fallback but wrong").

Consider whether forcing a synchronous preview-pipe refresh (mirroring
`get_preview`'s own wait-for-pipe mechanism — see the `set_viewport` design doc,
`2026-07-31-set-viewport-design.md`, "Wait-for-pipe mechanism" section, for exactly
where that code lives) is a better fix than erroring — that would make these
bindings ALWAYS correct instead of occasionally-erroring, at the cost of extra
latency per call. Your call; document the tradeoff you picked and why in your report.

## Explicitly NOT this task's job

- `set_viewport`/`restore_viewport`, `add_path_mask_in_viewport`/generic overlay —
  separate delegated tasks, touch the same `develop.c`; coordinate serially if
  branches land near each other.
- Any change to `dt_masks_get_image_size` itself, or to any of the GUI-facing mask
  type files (`circle.c`/`path.c`/etc.) that call it — out of scope, too wide a
  blast radius for this task's verification budget (see warning above).
- The two "silent bugs" (mask_object `mask_mode:0`, `set_module_mask` invert) —
  already verified NOT reproducing; nothing to do.

## Deliverables

- A clear writeup of what you found investigating the hypothesis above — CONFIRMED,
  REFUTED, or "found something else" — before any code changes. This task is
  investigation-first; don't jump to the fix in "Suggested fix shape" until you've
  verified the premise.
- If confirmed: the narrow fix scoped to the MCP-facing bindings (NOT
  `dt_masks_get_image_size` itself), with reasoning for why the blast radius is
  actually contained to those call sites.
- A regression test reproducing the failure mode, if you can construct one without
  a live darktable process (e.g. a targeted unit test isn't really possible for
  C-level pipe timing — if that's the conclusion, say so and describe the live-
  test procedure the main session should run instead: e.g. "open image, immediately
  (before any get_preview call) call retouch_list_shapes and check has_display_frame
  is false rather than a wrong value").
- Brace/paren balance check on `develop.c` after any edits.
- **Do NOT run `docker/build.sh`, `packaging-deb/*.sh`, or the docker bridge live
  test** — shared directories, will conflict with the other two parallel tasks.
  The main session does live verification + packaging + commit/push after
  reviewing all three branches.
- Report back: hypothesis confirmed/refuted/other, what you changed (if anything),
  and an EXACT live-test procedure the main session should run to confirm the fix
  (this task's whole premise is a timing race that unit tests can't fully cover).
