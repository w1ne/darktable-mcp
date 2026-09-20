> Historical guide from rfordinal/agentic-mcp at ff27e45. For current installation, MCP 2.x dependencies, and compatibility boundaries, use [our README](../README.md). Original package versions and deployment links below describe the fork.

# darktable-mcp

A copilot for your darkroom. Not an autopilot.

This is an [MCP](https://modelcontextprotocol.io/) server that puts an AI collaborator next to you in
a **live** darktable session. You talk about the photo that is open in front of you — what is not
working, what you are trying to get to, which of darktable's tools might get you there. The AI reads
the real module parameters out of your open darkroom, proposes a concrete step, applies it, renders
the result, and shows you. One small reversible move at a time, in your own darktable window, while
you watch it happen.

**You are still the editor.** This is not a service you hand a RAW to and get a finished JPEG back
from. The AI suggests, explains, and does the fiddly mechanical parts when you ask it to — building a
mask around a face, finding which module does the thing you just described in words, remembering what
a parameter's usable range is. Every change lands in your own history stack, visible immediately, and
you can veto it, redirect it, or Ctrl-Z it. If you stop talking, nothing else happens.

> **Screenshots below are placeholders.** The `docs/screenshots/*.png` files referenced in this
> README do not exist yet — they need to be captured from a live workstation session and dropped in.
> Each placeholder has a caption saying what the shot should show.

---

## What a collaborator is actually useful for

**It knows more of darktable than most of us do.** darktable has a genuinely deep toolset, and most
photographers work with a comfortable fraction of it — the modules we learned first. A conversation
partner that knows the whole surface can say "that halo is the local contrast module, not the tone
curve" or "for this you probably want the tone equalizer rather than pushing shadows in exposure", and
then show you what it looks like. That is the biggest practical win: not that the work gets done for
you, but that you end up using more of what darktable can actually do.

**You describe the outcome, not the control.** "Warm it up, but keep the shadows neutral" is one
sentence. Doing it is white balance plus color balance rgb plus knowing which of the four-way sliders
is shadows. You do not have to remember where a control lives, or what darktable calls the module that
does the thing you want — you say the thing you want, and the AI tells you which control it reached
for, so next time you know too.

**It can see the pixels, so it can have an opinion.** Every step is followed by a real render of the
live pipeline (`get_preview`), and the AI looks at it before saying anything. It can crop into the
region you are actually zoomed into (`get_viewport` + `get_preview(region=...)`) to judge grain,
sharpening, or a soft mask edge at real detail — the things that are invisible in a full-frame
thumbnail. So "that overshot, I'd pull it back to +0.4" is a remark about your image, not a guess.

**Masks are where it earns its keep.** Describing a region is much faster than drawing it. "Just her
face", "the sky above the ridgeline", "only the hair" become a segmented mask attached to a new masked
module instance — and the result is an ordinary darktable mask afterwards, with editable nodes. It
builds the rough shape; you refine it by hand if the edge is not right.

**Steps are small, bounded and reversible.** "A bit more." "Too much." "Now the same but only on the
sky." Each turn is one real parameter write, clamped to the field's actual min/max, with the clamp
reported back — so you get "that's already at max, +5 EV clamped to +3 EV" instead of a silent no-op,
and you always know exactly what changed.

**What it is not.** It does not finish a photo for you. It does not drag on-canvas handles, it does
not replace your eye, and it does not know your taste until you tell it. It will not go off and
process a folder unattended. You are the editor; it is not. It is fast and knowledgeable at the
mechanical part of "make it look like what I just said", and useful to argue with about what to try
next.

---

## Why this runs live, and not headless

Technically, most of this could have been a batch pipeline: point a script at a folder, let a model
decide the parameters, write the XMPs, done. It deliberately is not that.

The whole thing runs against a **real, open, interactive darktable window** because that is the design
goal, not a limitation. You see every parameter change land in your own darkroom, in your own history
stack, at the moment it happens. Nothing is prepared behind your back and presented as finished. You
can interrupt mid-thought, disagree, take the mouse and finish the mask yourself, or hit Ctrl-Z — the
AI is working *in your session*, on the same pixelpipe you are looking at, not on a copy somewhere
else.

That also keeps the collaboration honest about who did what: the history stack is the record. Every
entry the AI added is right there, named, in order, and removable.

---

## The tools are designed for a model, not for a person

Every tool here is shaped around how an AI reads a situation, not around how a human would want an API
to look. That distinction shows up everywhere once you start noticing it:

- Tool descriptions carry the *decision* the model has to make, not just the parameter list — when to
  reach for this tool instead of a neighbouring one, what the call will not do, what to check
  afterwards. They are prompt surface, so they are written like prompt surface.
- Responses are verbose on purpose. A call reports the coordinate frames it converted through, which
  image it acted on, and what to call next, because a model has no screen to glance at and no memory
  of the frame it was looking at two calls ago.
- Failures are diagnoses rather than status codes. "no darkroom image loaded" becomes "…and here is
  the image that IS open right now", because the terse version sends a model into guessing.
- Anything ambiguous gets renamed rather than documented. `viewport_pixels` became
  `snapshot_pixels` after exactly one reader took it to mean the darktable window instead of the
  returned render — a wrong reading that silently retouches the wrong spot is a design bug, not a
  reading-comprehension failure.
- Wrong input is refused, never quietly clamped or best-guessed, and refusals say what would have to
  change. A model will happily act on a plausible answer; the tool has to be the thing that does not.

That shape was not designed up front. It comes from running the tools against real models, watching
where they misread the result or lost the thread, and iterating on the wording, the reported fields and
the guard rails until the failures stopped. Several entries in this project's own bug log are exactly
that: not broken code, but a description or a response a model could reasonably misunderstand.

It also means the target keeps moving. What a strong model infers on its own, a weaker one needs
spelled out in the response; what one model treats as an obvious next step, another needs told. The
tools are tuned toward the level of capability and context handling they are actually used with, and
they get re-tuned as that changes. If you are reading a description and thinking it is over-explained
for a human, that is the intent — you are not the reader it was written for.

---

## What a session actually looks like

```
you:    open DSC_4471 in the darkroom
claude: [view_photos] [open_image_in_darkroom]
        Open. It's roughly a stop under, and white balance reads 5200K —
        cool for what looks like late afternoon light. Start with exposure?

you:    yeah, lift it a touch
claude: [get_params exposure]  currently 0.00 EV, range -3..+3
        [set_params exposure=+0.35] [get_preview]
        +0.35 EV. The window-frame highlights are still clean — have a look.

you:    a bit more
claude: [set_params exposure=+0.6] [get_preview]
        +0.6 EV. I'd stop about here; past this the window starts to blow.

you:    agreed. her face is still dark though
claude: Two ways: a masked exposure instance on just the face, or lift the
        shadows globally with the tone equalizer. The mask is more precise if
        you only want the face and not the whole foreground. Want me to try it?

you:    do the mask
claude: [get_preview]  picking a point on the cheek, x=0.42 y=0.31
        [mask_object op=exposure adjustment={exposure:+0.5}]
        Mask overlay and result attached — +0.5 EV inside the face only. The
        nodes are editable in darktable if the edge isn't where you want it.

you:    it's tight around the jaw, I'll nudge it myself
```

![placeholder: full-screen shot of darktable darkroom next to the Claude Desktop window, mid-conversation, with the AI's entries visible in darktable's history panel](docs/screenshots/session-overview.png)

*Should show: darktable darkroom on the left with a visibly edited RAW, the chat on the right with
the tool calls expanded, and darktable's history stack showing the AI's entries.*

---

## Concrete use cases

These are the flows the project was scoped and acceptance-tested against (journeys J1-J7 in the
project's internal acceptance test suite), described the way you would actually use them. Each one is a dialogue — the AI
proposes one step and shows you the result; you react.

### 1. "Lift the exposure a touch" — then "a bit more" (J1)

The core loop, and the pattern everything else follows. The AI reads the module's current value *and
its real bounds*, proposes a concrete number, applies it, renders, and hands it back to you to judge.

```json
{"name": "get_params", "arguments": {"op": "exposure"}}
{"name": "set_params", "arguments": {"op": "exposure", "fields": {"exposure": 0.35}}}
{"name": "get_preview", "arguments": {}}
```

`set_params` comes back with `applied` (what was actually written), `clamped` (every field that hit a
bound, with `requested`/`applied`/`min`/`max`) and `unknown_fields`. Ask for +5 EV and you are told it
clamped to +3, not left wondering why nothing moved.

![placeholder: before/after pair of the same RAW at 0.00 EV and +0.60 EV, with the chat turn that produced it](docs/screenshots/j1-exposure-chat.png)

*Should show: two previews side by side plus the "lift the exposure a touch" / "a bit more" turns.*

### 2. "This looks flat — what would give it some punch?" (J2)

A question, not an instruction. The AI can suggest a gentle S-curve on the tone curve, say why (and
what it will cost you in the shadows), then apply it so you can look:

```json
{"name": "set_params", "arguments": {
  "op": "tonecurve",
  "fields": {"enabled": true, "tonecurve": [{"x": 0.0, "y": 0.0}, {"x": 0.25, "y": 0.19},
                                            {"x": 0.75, "y": 0.81}, {"x": 1.0, "y": 1.0}]}}}
```

Array-valued parameters work like scalars here — tone curve nodes, color balance rgb rows, channel
mixer rows are read as arrays and written whole, with no per-module special-casing. The `enabled: true`
shortcut matters: modules that ship off (tonecurve, sharpen, grain, vignette) do nothing visible until
enabled, and `set_params` routes that in the same call. Afterwards the curve is a normal tone curve
you can grab and reshape.

![placeholder: before/after of a flat image and the same image after a chat-discussed S-curve, with darktable's tone curve widget showing the new node positions](docs/screenshots/j2-tonecurve.png)

*Should show: darktable's tone curve module panel with the AI-written nodes, plus the resulting
contrast change.*

### 3. "Her face is too dark — can you mask it?" (J3)

You describe the region; the AI picks a point on it from the preview, segments it into a polygon,
creates a *new masked instance* of the module (your global edit on instance 0 stays untouched),
attaches the polygon as a real darktable path mask, applies the adjustment inside it, and shows you
both the mask and the result.

```json
{"name": "mask_object", "arguments": {
  "op": "exposure",
  "adjustment": {"exposure": 0.6},
  "points": [{"x": 0.42, "y": 0.31, "label": 1}],
  "opacity": 1.0}}
```

It returns the mask overlay *and* the masked result as images, plus which backend did the
segmentation, so you can see what it selected before deciding whether you like it. Out of the box that
is a bundled OpenCV GrabCut segmenter — rough, but zero-install. Install the optional SAM2 sidecar and
it is used instead, with proper object boundaries. If anything fails, the just-created instance is
rolled back; you never get an orphan module in your history.

The mask is yours afterwards. Nudge the nodes, change the feather, delete it — it is an ordinary
darktable drawn mask.

Same tool, other asks: "darken the sky", "warm just the jacket", "pull the highlights back on the
window only".

![placeholder: portrait with the red mask overlay on the face, next to the resulting local brightening, plus the chat turn discussing it](docs/screenshots/j3-mask-object-face.png)

*Should show: the returned mask overlay image (red fill over the face), the masked result, and
darktable's own mask nodes visible on canvas.*

### 4. "A path mask will jag up her hair — anything better?" (J4)

Exactly the kind of question worth asking a collaborator. For fine edges — flyaway strands, fur,
motion blur, glass, smoke — `mask_raster` runs a MODNet matting model over the image to get a
continuous 0..1 alpha, loads it into a `rasterfile` instance, and wires a target module's blend to
consume it as a raster mask. No point picking; the matte is whole-image.

```json
{"name": "mask_raster", "arguments": {
  "op": "colorbalancergb",
  "adjustment": {"global_offset": 0.02, "shadows_C": 0.05},
  "opacity": 0.8}}
```

This one **requires the MODNet sidecar and has no fallback** — a hard-edged segmenter cannot fake a
soft matte, so it errors clearly rather than quietly degrading. It is also the newest and least
exercised part of the stack (see Status), so treat it as something to experiment with, not to rely on.

![placeholder: close crop of a hairline before/after a warm shift, showing the soft alpha edge rather than a hard polygon boundary](docs/screenshots/j4-mask-raster-hair.png)

*Should show: a 100% crop of hair edges, ideally with the alpha matte itself as a third panel.*

### 5. "This feels cold to me" (J5)

You say what bothers you; the AI names what it would change and by how much, then does it in steps you
can stop. "Warmer overall, and maybe a subtle teal in the shadows" is a `temperature` write followed by
a `colorbalancergb` write, each coalescing to one history entry.

```json
{"name": "set_params", "arguments": {"op": "temperature", "fields": {"temperature": 5800}}}
{"name": "set_params", "arguments": {"op": "colorbalancergb", "fields": {"shadows_H": 190.0, "shadows_C": 0.04}}}
```

![placeholder: before/after of a cold indoor shot warmed through conversation, with darktable's white balance module showing the new Kelvin value](docs/screenshots/j5-white-balance.png)

### 6. "Level the horizon and crop a touch tighter" (J6)

Geometry is reachable the same way, at parameter level: rotation angle and normalized crop margins on
the `crop`/`ashift`/`rotatepixels` modules, via the same `get_params`/`set_params` pair. This is
param-level control, not on-canvas handle dragging — good for "straighten by 1.2 degrees" and "tighten
to 4:5", and honestly worse than your own hands for "nudge it until it feels right". Use it for the
part that is arithmetic; keep the part that is taste.

### 7. "Show me what that changed" (J7, partial)

You can A/B any single module by toggling it and comparing renders — useful when you are not sure a
suggestion actually helped:

```json
{"name": "enable_module", "arguments": {"op": "colorbalancergb", "enabled": false}}
{"name": "get_preview", "arguments": {}}
{"name": "enable_module", "arguments": {"op": "colorbalancergb", "enabled": true}}
{"name": "get_preview", "arguments": {}}
```

A single-call "render the session's starting state" baseline is **not** implemented — `get_preview`
always renders the current live pipeline. Full-session before/after is still darktable's own
snapshot/history panel, which is where you would look anyway.

### 8. "There's a dust spot near the top right"

You point it out; the AI proposes a heal circle and a source region to sample from, applies it, and
shows you the crop. This is real local retouch, not a global module parameter — optionally on a
specific wavelet scale, so skin texture and base tones can be treated separately.

```json
{"name": "retouch_add_shape", "arguments": {
  "algorithm": "heal",
  "target": {"x": 0.61, "y": 0.22},
  "source": {"x": 0.66, "y": 0.24},
  "radius": 0.012,
  "feather": 0.004}}
```

`retouch_list_shapes` gives back the real shapes plus the module's wavelet-scale state (rather than the
raw 300-slot internal array), and `retouch_delete_shape` removes one by id — verified to restore the
pixels exactly, so "no, try a different source" costs nothing. `retouch_delete_shapes` takes a list
when a whole experiment needs undoing. Circle, ellipse, and path so far; brush is a later phase.

Heal/clone always rewrite the full pixel (texture *and* tone), which is wrong for something like a
wrinkle: a heal circle sized to the crease also flattens the shadow underneath it. `blur`/`fill` fix
that by acting on ONE wavelet scale instead of the base image — softening or erasing that scale's
texture while every other scale (and the tone/shadow it carries) is left alone. Neither takes a
`source` (there is nothing to sample from):

```json
{"name": "retouch_add_shape", "arguments": {
  "algorithm": "blur",
  "target": {"x": 0.52, "y": 0.41},
  "radius": 0.02,
  "wavelet_scale": 3,
  "blur_radius": 8}}
```

Picking that target as a fraction of the whole frame is awkward when you are both looking at a 3:1
zoom of one cheek. `capture_viewport` solves that: it snapshots the region you have on screen (the
main window, or `preview2` on a second monitor) and renders it, and returns a `snapshot_id`.

```json
{"name": "capture_viewport", "arguments": {"viewport": "preview2"}}
{"name": "retouch_add_shape_in_viewport", "arguments": {
  "snapshot_id": "vp_...",
  "algorithm": "heal",
  "coordinate_space": "snapshot_pixels",
  "target": {"x": 420, "y": 250},
  "source": {"x": 250, "y": 190},
  "radius": 10}}
```

Coordinates are then just pixels of the render you were shown, and the server does the rest of the
maths. There is more of it than you would expect: the viewport crop, then darktable's own gap between
the frame you see and the frame masks are stored in (they diverge as soon as orientation, crop, rotate
or lens correction is active, which for a portrait-orientation raw is always). Both stages are
reported back in the response, so a placement that lands wrong is debuggable instead of mysterious.

Two rules keep a mis-aimed call from quietly editing the wrong thing. A point outside the captured
region is rejected, never clamped to the nearest edge. And the snapshot is bound to the image that was
open when you took it: if darktable has moved to another photo since, the write is refused by id,
because a coordinate from one frame means nothing on another. `retouch_update_shape_in_viewport` moves
or resizes an existing shape in place, same conventions, same formid.

Numbers alone cannot tell you whether a heal circle actually covers the mark, whether its feather
spills onto a lip or an edge, or whether its source sits on clean texture instead of a second blemish.
`retouch_render_overlay` draws the shapes on the snapshot you already captured: target circle, feather
ring, source circle, the source-to-target link, labelled by formid — plus a report of any shapes that
overlap, which is a real mistake (the second heal then samples the first one's output).

```json
{"name": "retouch_render_overlay", "arguments": {
  "snapshot_id": "vp_...",
  "mode": "selected_shape",
  "highlight_formid": 1785057157}}
```

`mode` is `all_shapes`, `selected_shape`, `source_and_target`, or `mask_only` — the last one renders the
mask alpha itself, with darktable's own quadratic feather falloff, for judging coverage rather than
composition. The overlay is drawn by the server from the shape geometry, not screen-grabbed: darktable
paints its own overlay onto the GUI widget, so it exists in no buffer any tool can read. Drawing it
ourselves also means no focus juggling, no toggles left switched on for you to notice later, and
formid labels the GUI does not show. `retouch_list_shapes` reports each shape twice for the same
reason — once in darktable's mask storage frame (feed that straight back to an update call) and once
in the frame renders are actually in.

![placeholder: 100% crop showing a dust spot before and after a chat-discussed heal, with the retouch circle visible in darktable](docs/screenshots/retouch-heal-spot.png)

### 9. A second pair of eyes on a cull

You do not have to open frames one at a time to have a conversation about them. `get_contact_sheet`
renders one grid image (default 25 photos, 5 columns) from whatever collection is open in your
lighttable, labelled with filename, image id and rating. The AI looks at the sheet and tells you what
it sees — near-duplicates, the sharpest frame of a burst, the one where the eyes are open — and you
decide. Ratings, tags and notes are separate, explicit calls, so nothing is applied until you say so.

```json
{"name": "get_contact_sheet", "arguments": {"limit": 25, "columns": 5, "filter": "unrated"}}
{"name": "rate_photos", "arguments": {"photo_ids": ["412", "418", "423"], "rating": 3}}
{"name": "tag_photo",   "arguments": {"photo_ids": ["412"], "tags": ["portfolio", "cover-candidate"]}}
{"name": "set_photo_note", "arguments": {"photo_id": "412", "note": "Best of the sequence - eyes sharp, hands not clipped."}}
```

Contact sheets are strictly read-only: they never touch ratings, tags, history or the darkroom. Notes
land in the photo's Description field (`Xmp.dc.description`), so the AI's reasoning shows up in
darktable itself and survives in the sidecar — you can disagree with it later and still see what it
thought.

![placeholder: a generated 5x5 contact sheet with filename/id/rating labels, next to the chat turn where the AI points out keepers](docs/screenshots/contact-sheet-cull.png)

*Should show: an actual `get_contact_sheet` output image.*

### 10. Working through a shoot without reaching for the mouse

`navigate_photo` steps to the next/previous photo in the open collection — same ordering as the contact
sheet — and opens it in darkroom. Combined with the editing loop, "same idea on the next one, then show
me" is a single turn, and you are still looking at every frame as it comes up. It errors at either end
of the collection instead of wrapping around, so a walk through a shoot terminates cleanly.

### 11. Rating straight off the card, before importing anything

When darktable does not know about the shoot yet, there is a file-based path that needs no library and
no GUI: `extract_previews` pulls auto-rotated JPEGs plus EXIF out of the RAWs, the AI looks at them and
proposes ratings, `apply_ratings_batch` writes XMP sidecars next to the RAWs, and `open_in_darktable`
finally launches the GUI with the folder as a film roll, pre-filtered to the rating range you asked
for. No SQLite poking, no half-imported state. This is the one genuinely batch-shaped workflow here,
and it is deliberately confined to triage — before any editing decision — with the GUI as the last
step so you review the result yourself.

---

## Status and maturity

Honest version, per layer:

| Area | State |
| --- | --- |
| Library / cull / rate / tag / export / camera import | Working, inherited from upstream `w1ne/darktable-mcp` and extended |
| Scalar + array parameter editing, live preview (J1, J2, J5, J6) | Working, acceptance-tested against a live darktable |
| Object masks via path mask (J3) | Working. Zero-install GrabCut segmentation is rough; SAM2 sidecar is the quality tier |
| Retouch heal/clone/blur/fill, circle/ellipse/path | Verified on a live darktable against a fixture library, incl. exact-revert on delete. Brush not yet supported. Not yet exercised against a large real library |
| Raster / matte masks (J4) | Newest and least exercised. The raster-injection binding started as a research spike, and this path is not hardened |
| Before/after baseline render (J7) | Partial — per-module A/B only, no session-start baseline |
| Interactive mask node editing (move/insert/delete points) | Not shipped. Written but unbuilt/untested; not exposed as tools |
| Remote HTTP transport + bearer auth | Working and opt-in; stdio is the default |
| Packaged `.deb` install | Built and validated on one Ubuntu 24.04 amd64 workstation. Other platforms: build from source |

This is a working tool used by its author on real photographs, not a released product with a support
matrix. Expect to read error messages.

---

## Install

Two pieces have to be in place: **a patched darktable** (stock darktable cannot expose its live
darkroom state) and **this MCP server**.

### The easy path: Ubuntu 24.04, two .deb files

Prebuilt packages are attached to [GitHub Releases](https://github.com/rfordinal/darktable-mcp/releases)
(alongside step-by-step notes in
[`docs/install/INSTALL-ubuntu-24.04.md`](docs/install/INSTALL-ubuntu-24.04.md)). Install the newest pair, in order:

```bash
sudo apt install ./darktable-agentic_5.8.0+agentic.9_amd64.deb   # patched darktable
sudo apt install ./darktable-mcp_1.0.14_amd64.deb                # this server, venv included
```

`darktable-agentic` `Provides`/`Conflicts`/`Replaces` the stock `darktable` package, so it cleanly
supersedes an existing install. `darktable-mcp` ships its own Python 3.12 venv at
`/opt/darktable-mcp/venv/` and a launcher at `/usr/bin/darktable-mcp`; nothing is downloaded at
install time.

### From source (any other platform)

1. Build the patched darktable from
   [rfordinal/darktable-agentic](https://github.com/rfordinal/darktable-agentic) (branch
   `agentic-mcp`). Its README is the API reference for the `darktable.develop` Lua namespace this
   server drives.
2. Install this server from a checkout:

   ```bash
   git clone -b agentic-mcp https://github.com/rfordinal/darktable-mcp
   cd darktable-mcp
   uv venv --python 3.12 .venv
   uv pip install -p .venv/bin/python -e '.[vision]'   # [vision] = the card-side rating workflow
   ```

   Python 3.10+ is required (the `mcp` package). The `[vision]` extra pulls `rawpy`, `Pillow` and
   `pyexiv2`, which need system `libraw` and `libexiv2`. `darktable-cli` must be on `PATH` for
   exports.

### Enable the Lua bridge

The server talks to darktable through a small file-based JSON bridge under
`$XDG_CACHE_HOME/darktable-mcp/` (default `~/.cache/darktable-mcp/`). darktable has to load the
bridge plugin:

```bash
darktable-mcp install-plugin     # copies the plugin and adds require "darktable_mcp" to luarc
```

From the `.deb`, the plugin is already at `/usr/share/darktable/lua/darktable_mcp.lua`; just opt in:

```bash
mkdir -p ~/.config/darktable
cat /usr/share/doc/darktable-agentic/luarc.snippet >> ~/.config/darktable/luarc
```

Then start darktable and confirm the bridge came up:

```bash
darktable -d lua 2>&1 | grep -i mcp     # must print: darktable-mcp bridge: ready
```

**If that line is missing, nothing else will work.** The usual cause is a stale or misspelled
`require` line earlier in your `luarc` — darktable aborts the rest of the file at the first failing
`require`, so a *correct* `require "darktable_mcp"` after a bad line never runs, and every tool call
fails with "darktable not running, or plugin not loaded". Check `luarc` line by line.

**darktable must be running whenever you use the darkroom tools.** The server does not launch it for
you; `open_image_in_darkroom` switches an already-open darktable into darkroom view. That is by design
— see "Why this runs live, and not headless" above.

### Register with your MCP client

Claude Desktop (`~/.config/Claude/claude_desktop_config.json` on Linux,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

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

Claude Code:

```bash
claude mcp add darktable /usr/bin/darktable-mcp
```

From a source checkout instead, point `command` at the venv's interpreter with
`"args": ["-m", "darktable_mcp"]`.

### Optional: the SAM2 / MODNet sidecar

`mask_object` works without it (bundled GrabCut). `mask_raster` does not work without it. Full setup
is in [`docs/install/INSTALL-sidecar.md`](docs/install/INSTALL-sidecar.md) — its own venv with CPU-only torch plus
a ~149MB SAM2 checkpoint and a MODNet ONNX, deliberately not bundled in the `.deb`. There is also a
helper:

```bash
darktable-mcp install-sidecar
```

Then set these wherever the server is launched from (including in the client's `env` block):

```bash
export DARKTABLE_MCP_SIDECAR_PYTHON=/opt/darktable-mcp-sidecar/.venv/bin/python3.12
export DARKTABLE_MCP_SIDECAR_SEGMENT=/opt/darktable-mcp-sidecar/segment.py      # mask_object / SAM2
export DARKTABLE_MCP_SIDECAR_MATTE=/opt/darktable-mcp-sidecar/matte.py          # mask_raster / MODNet
export DARKTABLE_MCP_MODNET_CHECKPOINT=/opt/darktable-mcp-sidecar/checkpoints/modnet_photographic_portrait_matting.onnx
```

### Optional: remote / HTTP transport

stdio is the default and the recommended local setup. For a remote client, the same server speaks
Streamable HTTP:

```bash
DTMCP_BEARER_TOKEN=... DTMCP_PUBLIC_URL=https://your.host \
  darktable-mcp --http --host 127.0.0.1 --port 8787 --path /mcp
```

Put it behind a TLS reverse proxy with buffering off. `DTMCP_PUBLIC_URL` lets `get_preview` and
`export_images` hand back download URLs for renders too large to inline. Note this only changes where
the *client* runs, not the arrangement: darktable is still open in front of somebody.

---

## Tool reference

47 tools. Everything except the camera/preview-extraction group needs darktable running with the
bridge loaded. The signatures below are a map for a human reader; the descriptions the model actually
receives are longer and written for it (see "The tools are designed for a model, not for a person").

**Darkroom editing** (needs the patched darktable; act on whichever image is open in darkroom)

- `open_image_in_darkroom(image_id)` — open a photo in darkroom. Call this first.
- `get_current_image()` — which image is open (works even if you opened it by hand).
- `navigate_photo(direction, sort?, direction_order?)` — step to next/previous in the collection.
- `list_modules()` — active modules as `{op, instance, enabled}`.
- `get_params(op, instance?)` — a module's fields with `value`/`min`/`max`/`default`; arrays come back
  as arrays.
- `set_params(op, fields, instance?)` — write fields, commit to history, reprocess. Always clamps and
  reports it. Accepts an `enabled` convenience field.
- `enable_module(op, enabled, instance?)` — toggle a module (many ship off by default).
- `add_instance(op, fields?)` — new masked/parametric instance, returns its index. `fields` sets
  initial params (e.g. `compensate_exposure_bias`/`compensate_hilite_pres`: false for a local
  exposure dodge/burn) in the SAME history entry as the creation, instead of a separate later
  `set_params` call inheriting instance 0's values first.
- `get_preview(max_w?, max_h?, region?, return_image?, inline_max_dim?)` — render the live edit. No DB
  write. `region` renders a normalized sub-rectangle at full detail.
- `get_viewport()` — the darkroom canvas zoom/pan state, including a ready-to-use `region` you can
  pass straight to `get_preview`, so the AI can look at what you are looking at.
- `capture_viewport(viewport?, max_w?, max_h?, return_image?)` — snapshot region plus render of the
  `main` or `preview2` window, bound to the image open at capture time. Returns a `snapshot_id` for
  the `*_in_viewport` retouch tools. Snapshots expire after a few minutes.

**Masks and local edits**

- `mask_object(op, adjustment, points|box, opacity?, new_instance?)` — vision-picked object mask plus
  adjustment, in one call. SAM2 if configured, GrabCut otherwise.
- `mask_raster(op, adjustment, opacity?, new_instance?)` — soft alpha matte (MODNet) gating a local
  edit. Requires the matting sidecar.
- `add_path_mask(op, points, instance?, opacity?)` — low-level: attach a polygon you already have.
- `retouch_add_shape(algorithm, target?, source?, radius?, feather?, opacity?, wavelet_scale?,
  instance?, blur_type?, blur_radius?, fill_mode?, fill_color?, fill_brightness?, shape_type?,
  radius_b?, rotation?, points?, smooth?)` — heal/clone/blur/fill circle, ellipse, or path on the
  retouch module. `source` is required for heal/clone, ignored for blur/fill (they have none).
  `blur`/`fill` on a nonzero `wavelet_scale` act on that scale's texture only, leaving tone/shadow on
  the other scales untouched — heal/clone always rewrite the full pixel regardless of scale.
  `shape_type="ellipse"` treats `radius` as the first semi-axis and adds `radius_b` (second semi-axis,
  defaults to `radius`) and `rotation` (degrees). `shape_type="path"` ignores `target`/`radius`
  entirely and takes `points` (>=3 nodes) instead — same format as `add_path_mask`, and `feather`
  switches to that tool's bbox-fraction convention. `retouch_update_shape`/`retouch_list_shapes`
  support all three too (brush is the only one still missing).
- `retouch_add_shape_in_viewport(snapshot_id, algorithm, target?, source?, radius?, feather?,
  coordinate_space?, radius_space?, opacity?, wavelet_scale?, instance?, return_preview?, blur_type?,
  blur_radius?, fill_mode?, fill_color?, fill_brightness?, shape_type?, radius_b?, rotation?, points?,
  smooth?)` — the same heal/clone/blur/fill circle/ellipse/path, but placed in the coordinates of a
  `capture_viewport` render (each path node gets its own backtransform; an ellipse's `radius_b` gets
  its own extra one, since a non-uniform crop/aspect can scale the two axes differently).
  `coordinate_space` is `snapshot_normalized` (0..1 of the render) or `snapshot_pixels` (pixels of the
  render, not of the darktable window); the older `viewport_*` spellings still work. Refuses to write
  if darkroom has moved to a different image than the snapshot's.
- `retouch_update_shape_in_viewport(snapshot_id, formid, target?, source?, radius?, ..., points?)` —
  move or resize an existing shape in place, keeping its formid. `source` is required only if the
  shape's EFFECTIVE algorithm (the one given here, or its current one if `algorithm` is omitted) is
  heal/clone. `points` is required (not optional) to update a path — its geometry IS its point list,
  so there's no "keep current, just move" option the way circle/ellipse have — and rejected for any
  other shape type.
- `retouch_list_shapes(instance?)` / `retouch_delete_shape(formid, instance?)` /
  `retouch_delete_shapes(formids, instance?)`. `retouch_list_shapes` reports every shape in both
  coordinate frames (mask storage, and the frame previews render in) plus its opacity; a path reports
  its full node list (corner/control points/border) in the mask-storage frame only — no per-node
  display-frame copy, since a 20+ node polygon would mean 20+ extra coordinate-transform calls for a
  field the round-trip back into `retouch_update_shape` doesn't need.
- `retouch_render_overlay(snapshot_id, mode?, highlight_formid?, instance?, label_shapes?,
  return_image?)` — draw the shapes on a `capture_viewport` snapshot for visual verification.
  `mode` is `all_shapes`, `selected_shape`, `source_and_target` or `mask_only`. Read-only; refuses if
  darkroom has moved to a different image than the snapshot's.
- `list_masks()` — every drawn shape in the image (any type, any module), each with `used_by`
  (`{op, instance}` pairs already referencing it). Find a shape drawn by hand, or by another tool
  earlier in the session, before wiring it into a new instance.
- `get_module_mask(op, instance?)` — one module's full mask state: group opacity/mask_mode/
  blend_mode/invert plus each attached shape's boolean-combine operation and per-shape invert.
- `attach_mask(op, instance?, formid, operation?)` — wire an EXISTING shape (from `list_masks` or
  any mask-creating tool's return value) into a module's blend group WITHOUT copying it; the same
  shape can back several modules at once. `operation` is `union` (default), `intersection`,
  `difference`, or `exclusion`. Also turns the module's drawn-mask blend bit on (same effect as
  clicking the mask pencil icon) and verifies it stuck with a separate `get_blend_params` read.
- `detach_mask(op, instance?, formid)` — unwire a shape without deleting it; it stays available for
  `attach_mask` (this module or another). Detaching the last shape clears the module's mask
  entirely, mirroring the GUI's "no masks" action.
- `set_module_mask(op, instance?, shapes, opacity?, invert?)` — replace a module's ENTIRE shape list
  in one call (`shapes` is `[{formid, operation?}]`); diffs against what is currently attached,
  detaching/attaching/re-attaching only what changed. Optionally also sets overall opacity/invert.

**Library, culling, metadata**

- `view_photos(filter?, rating_min?, limit?, scope?)` — browse the open collection (or the whole
  library with `scope="library"`). Returns id, filename, absolute path, rating.
- `get_contact_sheet(offset?, limit?, columns?, filter?, sort?, direction?, thumbnail_width?, background?, include_*?)`
  — one grid image for visual culling, with paging metadata. Read-only.
- `rate_photos(photo_ids, rating)` — -1..5 (-1 reject, 0 unrated).
- `tag_photo(photo_ids, tags?, remove_tags?)` — attach/detach keywords, creating tags as needed.
- `set_photo_note(photo_id, note)` / `get_photo_note(photo_id)` — the photo's Description field.
- `list_collections(filter?)` / `list_photos_in_collection(collection, limit?)` — tags as
  Lightroom-ish collections.
- `import_batch(source_path, recursive?)` — register a folder as a film roll.
- `list_styles()` / `apply_preset(photo_ids, preset_name)` — darktable styles.

**Card and camera ingest** (headless, no library needed)

- `import_from_camera(destination?, camera_port?, timeout_seconds?)` — libgphoto2 copy. Merges hybrid
  setups where one card is on PTP and the other is mounted as USB mass storage (common on Nikon
  DSLRs).
- `extract_previews(source_dir, output_dir?, max_dim?, thumb_dim?, overwrite?)` — auto-rotated JPEG
  previews plus thumbs and EXIF from RAWs. Per-file detail goes to a `.jsonl` side file so a
  700-frame shoot does not blow the agent's context.
- `apply_ratings_batch(source_dir, ratings, log?)` — write XMP `xmp:Rating` sidecars from a
  `{stem: rating}` map.
- `open_in_darktable(source_dir, rating?, rating_min?, rating_max?)` — launch the GUI on a folder,
  pre-filtered by rating.

**Export**

- `export_images(photo_ids, output_path, format, quality?)` — JPEG/PNG/TIFF via `darktable-cli`, in an
  isolated config dir so it does not race your GUI's `library.db` lock. Per-file results in a
  `.jsonl` side file.

---

## Safety and non-destructive behavior

- **Nothing is committed behind your back.** Parameter writes go into the in-memory history stack and
  reprocess; they are not written to `library.db` or XMP until darktable saves normally. Ctrl-Z reverts
  the AI's changes like any other edit.
- **Nothing happens unprompted.** The AI acts only inside a turn you started. There is no background
  pass over your library, no scheduled processing, no queue.
- **`get_preview` never writes.** It grabs the live pipeline output; it is safe to call as often as you
  like.
- **Typed tools only.** The AI never gets raw Lua or shell evaluation. The Lua bridge dispatches from a
  fixed method whitelist; unknown methods error.
- **No direct database access, ever.** Only official darktable APIs: `darktable-cli` for export, the
  Lua API for everything else. Any contribution that reads or writes `library.db` directly will be
  rejected.
- **Edits from a snapshot stay on that photo.** Every darkroom binding resolves against whichever
  image darktable currently has open, so `capture_viewport` records the image id and the
  `*_in_viewport` retouch tools refuse the write when it no longer matches. Post-edit previews report
  which photo they are of, so a preview can never be silently mistaken for the wrong image.
- **Failed local edits roll back.** `mask_object` and `mask_raster` clean up any module instance they
  created if a later step fails — no orphan instances left in your history.
- **Working on a duplicate is still on you.** Live editing mutates the open image's real history stack.
  For experimental sessions, duplicate the image in darktable first.

---

## How it works (the short version)

```
MCP client (Claude Desktop / Claude Code / …)
   │  MCP over stdio (or Streamable HTTP)
   ▼
darktable-mcp server  (this repo, Python)
   │  file-based JSON-RPC under ~/.cache/darktable-mcp/
   ▼
darktable_mcp.lua bridge  (worker loop inside your running darktable)
   │  calls darktable.develop.*
   ▼
darktable.develop.* C bindings   ← github.com/rfordinal/darktable-agentic
   ▼
live darkroom pixelpipe on the open image — the one on your screen

SAM2 / MODNet sidecars  ← subprocess from this server, for masks
```

The darktable-side patch — a `darktable.develop` Lua namespace giving introspection-driven read/write
access to any module's parameters, plus masks and a live uncommitted preview grab — lives in
[**rfordinal/darktable-agentic**](https://github.com/rfordinal/darktable-agentic). Read that repo if
you want the low-level API; you do not need to in order to use this one.

Why the patch was necessary: stock darktable's Lua API exposes neither `image.modules` nor
`image.history`, and `dt.gui.action` only works with an active GUI darkroom view, which made an earlier
pure-Lua attempt a dead end. The C namespace is what turned "click a slider through the GUI automation
layer" into "read the real parameter, clamp against its real bounds, write it, reprocess".

Design constraint worth stating: there is no headless one-shot path for library reads/writes —
`darktable-cli` does not load your library and `darktable --lua` brings up the full GUI. Hence the
long-running plugin inside your interactive session plus a file bridge, rather than a CLI shell-out per
call. That constraint happens to align exactly with the design goal: the session you are sitting in
front of is the session being edited.

---

## Contributing

Contributions welcome. Three hard rules: no direct `library.db` reads or writes; tools that return data
to the AI must be headless (the GUI may only launch when showing the *human* something is the tool's
actual purpose); and nothing that produces a finished edit without the human watching it happen — the
point of this project is collaboration, not unattended processing.

## License

MIT, copyright w1ne — see [`LICENSE`](LICENSE), unchanged from upstream
[w1ne/darktable-mcp](https://github.com/w1ne/darktable-mcp). [`NOTICE.md`](NOTICE.md) records what was
kept from upstream and what was added here. (The sibling darktable fork,
[darktable-agentic](https://github.com/rfordinal/darktable-agentic), is GPL3 as darktable itself
requires — different repo, different license.)
