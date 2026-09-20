# Optional: installing the SAM2/MODNet sidecar (Ubuntu 24.04)

**Quick way:** run `darktable-mcp install-sidecar`. ONE command sets up
**both** halves in the same shared venv: SAM2 (torch-CPU + sam2, the
`mask_object` upgrade) AND MODNet (onnxruntime, the `mask_raster`
requirement) -- venv creation, pinned deps for both, both checkpoint
downloads, and env-var setup, idempotently, telling you clearly if `uv` is
missing or there's no network access. Run `darktable-mcp install-sidecar
--help` for options (`--target-dir`, `--force`, `--dry-run`,
`--skip-checkpoint` for the SAM2 checkpoint, `--skip-modnet-checkpoint` for
the MODNet checkpoint). Re-running it on an existing SAM2-only install adds
just the missing MODNet pieces (onnxruntime + checkpoint) without redoing
the SAM2 steps. The manual steps below are still here for reference /
troubleshooting / anyone who wants full control over each step.

This is an **optional, separate** install. Nothing in this section is
required for `darktable-agentic` or `darktable-mcp` to work. Without it,
`mask_object` still works out of the box via a bundled rough OpenCV
GrabCut fallback (see "Without the sidecar configured" below) -- this
sidecar only upgrades mask precision, it does not unlock functionality
that would otherwise be missing (with ONE exception: `mask_raster`, see
"mask_object vs. mask_raster: segmentation vs. matting" below -- that one
tool genuinely has no fallback and needs the sidecar's MODNet half).

- **`add_path_mask`** (attach a polygon you already have, in normalized 0..1
  image coordinates, to a module instance) works with **no sidecar at all**.
- Every other darktable-mcp tool (`view_photos`, `open_image_in_darkroom`,
  `list_modules`, `get_params`, `set_params`, `get_preview`, `enable_module`,
  `add_instance`, `get_viewport`, ...) also needs **no sidecar**.
- **`mask_object`** -- "mask this object for me" from a point/box/label
  prompt, without you supplying the polygon yourself -- uses the SAM2 half
  of the sidecar when present. Without it, `mask_object` still works: it
  falls back to a bundled, zero-install OpenCV GrabCut segmenter (rough tier
  -- see "Without the sidecar configured" below). Only if BOTH the sidecar
  and GrabCut fail does `mask_object` return an error, and even then nothing
  in darktable is touched -- no crash, no orphan module instance.
- **`mask_raster`** -- a matte-limited local edit with SOFT edges (hair,
  fur, fine detail), instead of `mask_object`'s hard drawn-polygon edge --
  uses the MODNet half of the sidecar and has **NO fallback**: a hard-edged
  segmenter cannot produce a soft matte, so without the sidecar this tool
  returns a clear error instead of silently degrading. See "mask_object vs.
  mask_raster" below.

Install this section if you want `mask_object`'s automatic segmentation to
use precise SAM2 masks instead of the rough GrabCut fallback (optional), OR
if you want to use `mask_raster` at all (required -- there is no fallback
for that one tool).

## mask_object vs. mask_raster: segmentation vs. matting

Both tools apply a local edit to part of an image, but they answer a
different question and the sidecar backs them with two different models:

- **`mask_object`** (SAM2, segmentation): "is this pixel part of the
  object" -- a closed polygon boundary, hard edge once rasterized. Good for
  faces, sky, most everyday objects with a clean silhouette. Has a
  zero-install fallback (GrabCut).
- **`mask_raster`** (MODNet, matting): "how much of this pixel is
  foreground" -- a continuous 0..1 alpha per pixel, soft edge. Use it
  instead of `mask_object` whenever the subject has fine edges a polygon
  would jag up: a hairline, flyaway strands, fur, motion blur, glass,
  smoke. Has **no fallback** -- a hard-edged segmenter cannot produce a
  soft matte, so a missing sidecar is a clear error, not a silent
  downgrade.

Both live in the same sidecar directory (`segment.py` + `matte.py`,
sharing one venv) and are installed together by the steps below /
`darktable-mcp install-sidecar`.

## What it is

A standalone Python service (`darktable-mcp/sidecar/segment.py`) that turns
a point/box prompt into a normalized polygon using **SAM2** (Meta's Segment
Anything Model 2, tiny checkpoint, CPU-only). It runs as a **subprocess in
its own venv**, invoked by the `darktable-mcp` server process on the host --
never bundled inside the `darktable-mcp` `.deb` (torch CPU wheels + a
~149MB model checkpoint are too heavy and the wrong shape for that package).

## 1. Prerequisites

- Ubuntu 24.04 (or any Linux the darktable-mcp server itself runs on).
- Python 3.10+ available to build a venv from (Ubuntu 24.04's stock
  `python3` is 3.12, which is fine). [`uv`](https://astral.sh/uv) is the
  quickest way to get a pinned 3.12 interpreter and manage the venv, but
  plain `python3 -m venv` + `pip` works too (see step 3's fallback).
- ~2GB free disk (torch CPU wheels + checkpoint + venv).
- Outbound network access for this install only (PyPI + Meta's public
  checkpoint bucket) -- not needed afterwards, and not needed by the
  `darktable-agentic`/`darktable-mcp` `.deb` installs themselves.

## 2. Get the sidecar source

The sidecar source (`sidecar/segment.py`, `requirements*.txt`) lives inside
the `darktable-mcp` source tree (fork of `w1ne/darktable-mcp`), **not** in
either `.deb` package. Get a copy of the `darktable-mcp/sidecar/` directory
from that source checkout (e.g. `git clone` the fork, or copy the directory
across) to wherever you want the sidecar to live on the target machine, e.g.
`/opt/darktable-mcp-sidecar/`.

## 3. Create the venv and install pinned deps (two-step, CPU-only torch)

```bash
cd /opt/darktable-mcp-sidecar   # wherever you placed sidecar/'s contents

# 1. venv on Python 3.10+ (uv fetches/caches a pinned interpreter for you)
uv venv --python 3.12 .venv

# 2. CPU-only torch FIRST, from its own index -- must be a separate step
#    from step 3 (see requirements.txt's own comments for why: sam2's
#    dependency resolution does not reliably pick CPU wheels if torch and
#    sam2 are installed in the same pip invocation from the default index)
uv pip install --python .venv/bin/python3.12 \
  --index-url https://download.pytorch.org/whl/cpu \
  -r requirements-torch.txt

# 3. sam2 + opencv + numpy + pillow + pytest, from the default index
uv pip install --python .venv/bin/python3.12 -r requirements.txt
```

No `uv`? Same two steps with plain `pip` inside a 3.10+ virtualenv:

```bash
python3.12 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu -r requirements-torch.txt
.venv/bin/pip install -r requirements.txt
```

## 4. Download the checkpoint(s)

```bash
mkdir -p checkpoints
curl -sL -o checkpoints/sam2.1_hiera_tiny.pt \
  https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt
```

This is the SAM 2.1 Hiera-tiny checkpoint (~149MB, smallest official SAM2
checkpoint -- chosen for CPU speed; ~6s per image on an 8-core box). Larger,
more accurate checkpoints are available at the same
`dl.fbaipublicfiles.com/segment_anything_2/092824/` path
(`sam2.1_hiera_{small,base_plus,large}.pt`, each paired with a matching
`configs/sam2.1/sam2.1_hiera_{s,b+,l}.yaml` config) if quality matters more
than latency for your use case -- swap the checkpoint/model-cfg values in
step 6 accordingly. This checkpoint is only needed for `mask_object`.

`mask_raster` needs a SECOND, separate checkpoint -- MODNet, not SAM2:

```bash
curl -sL -o checkpoints/modnet_photographic_portrait_matting.onnx \
  https://huggingface.co/DavG25/modnet-pretrained-models/resolve/main/models/modnet_photographic_portrait_matting.onnx
```

~24.7MB, ONNX format, runs via `onnxruntime` -- no torch/CUDA needed for
this half of the sidecar (it reuses the SAME venv from step 3, since
`onnxruntime` is already in `requirements.txt`). Skip this download if you
only want `mask_object`; skip the SAM2 checkpoint above if you only want
`mask_raster`.

## 5. Verify the sidecar works standalone

```bash
.venv/bin/python3.12 segment.py \
  --image fixtures/portrait.jpg \
  --box 0.344,0.105,0.271,0.217 \
  --backend sam2 \
  --checkpoint checkpoints/sam2.1_hiera_tiny.pt \
  --model-cfg configs/sam2.1/sam2.1_hiera_t.yaml
```

Should print a JSON object with a `"polygon"` list of ~10-30 normalized
`{x,y}` points and a `"score"`. If this works, the sidecar itself is fine
and any remaining problem is in step 6 (server not finding it).

For `mask_raster`, verify the MODNet half the same way:

```bash
.venv/bin/python3.12 matte.py \
  --image fixtures/portrait.jpg \
  --backend modnet \
  --checkpoint checkpoints/modnet_photographic_portrait_matting.onnx \
  --out /tmp/portrait_alpha.png
```

Should print a JSON object with `"alpha_path"`, `"size"`, and
`alpha_min`/`alpha_max`/`alpha_mean`. `/tmp/portrait_alpha.png` is the
16-bit grayscale alpha matte itself -- open it in any image viewer to
sanity-check it looks like a soft cutout of the subject.

## 6. Point the darktable-mcp server at the sidecar

The server reads environment variables, checked **before** the co-located
dev-checkout default (`<repo>/darktable-mcp/sidecar/.venv/bin/python3.12`,
which won't exist on a `.deb` install since the sidecar isn't bundled).
`segmentation_tools.py` (`mask_object`) and `matting_tools.py`
(`mask_raster`) share the SAME venv/python var but have separate
script-path vars, since `segment.py` and `matte.py` are different
entrypoint files:

- `DARKTABLE_MCP_SIDECAR_PYTHON` -- absolute path to the sidecar venv's
  `python3.12`, e.g. `/opt/darktable-mcp-sidecar/.venv/bin/python3.12`
  (used by BOTH `mask_object` and `mask_raster`)
- `DARKTABLE_MCP_SIDECAR_SEGMENT` -- absolute path to `segment.py` (used by
  `mask_object`), e.g. `/opt/darktable-mcp-sidecar/segment.py`
- `DARKTABLE_MCP_SIDECAR_MATTE` -- absolute path to `matte.py` (used by
  `mask_raster`), e.g. `/opt/darktable-mcp-sidecar/matte.py`
- `DARKTABLE_MCP_MODNET_CHECKPOINT` -- absolute path to the MODNet ONNX
  checkpoint (used by `mask_raster`), e.g.
  `/opt/darktable-mcp-sidecar/checkpoints/modnet_photographic_portrait_matting.onnx`
  -- falls back to the co-located dev-checkout default if unset, same
  pattern as the SAM2 checkpoint

Set these wherever `/usr/bin/darktable-mcp` gets launched from (your shell
profile, a systemd unit's `Environment=`, or the `env` block of your Claude
Desktop/Code MCP server registration), for example:

```bash
export DARKTABLE_MCP_SIDECAR_PYTHON=/opt/darktable-mcp-sidecar/.venv/bin/python3.12
export DARKTABLE_MCP_SIDECAR_SEGMENT=/opt/darktable-mcp-sidecar/segment.py
export DARKTABLE_MCP_SIDECAR_MATTE=/opt/darktable-mcp-sidecar/matte.py
export DARKTABLE_MCP_MODNET_CHECKPOINT=/opt/darktable-mcp-sidecar/checkpoints/modnet_photographic_portrait_matting.onnx
```

If a Claude MCP client config is used, an `env` block can be added alongside
the existing `command`/`args`:

```json
{
  "mcpServers": {
    "darktable": {
      "command": "/usr/bin/darktable-mcp",
      "args": [],
      "env": {
        "DARKTABLE_MCP_SIDECAR_PYTHON": "/opt/darktable-mcp-sidecar/.venv/bin/python3.12",
        "DARKTABLE_MCP_SIDECAR_SEGMENT": "/opt/darktable-mcp-sidecar/segment.py",
        "DARKTABLE_MCP_SIDECAR_MATTE": "/opt/darktable-mcp-sidecar/matte.py",
        "DARKTABLE_MCP_MODNET_CHECKPOINT": "/opt/darktable-mcp-sidecar/checkpoints/modnet_photographic_portrait_matting.onnx"
      }
    }
  }
}
```

Only set the SAM2 vars if you installed that checkpoint, only set the
MODNet vars if you installed that one -- each tool independently reports a
clear "not installed" error for its own half if its vars/checkpoint are
missing, it doesn't affect the other tool.

## 7. Try it

With the env vars set and darktable running with an image open in the
darkroom (see the main `INSTALL-ubuntu-24.04.md`), ask Claude something
like:

> Mask the sky in this photo and darken it a bit.

`mask_object` will grab a `get_preview` frame, prompt the sidecar with a
point/box on the subject, get a polygon back, add a new module instance,
attach the polygon as a path mask via `add_path_mask`, and apply your
requested adjustment -- all in one call.

For a soft-edged subject, ask instead:

> Warm up her hair a bit.

`mask_raster` will run the open image (or, for RAW files the matting model
can't read directly, a full-resolution darkroom preview export) through
MODNet to get a continuous alpha matte, write it to a fresh uniquely-named
file, load it into a new `rasterfile` module instance, wire a new instance
of your target module (e.g. `colorbalancergb`) to consume that raster mask
via `set_raster_source`, apply your adjustment, and render a fresh preview
-- all in one call, no point/box picking needed since MODNet is a dense,
whole-image matting model.

## Without the sidecar configured

**`mask_object`**: if `DARKTABLE_MCP_SIDECAR_PYTHON`/`DARKTABLE_MCP_SIDECAR_SEGMENT`
aren't set (or the SAM2 checkpoint is missing) and no co-located dev sidecar
is found, `mask_object` no longer fails -- it transparently falls back to
the bundled `darktable_mcp/tools/local_segment.py` OpenCV GrabCut segmenter
and still produces a valid, localized mask (see that module's docstring for
what "rough tier" means in practice: no learned object prior, no text
grounding, just an interactive color-model cut seeded by your point/box).
The `mask_object` result reports `backend: grabcut` in this case (vs
`backend: sam2` when the sidecar was used), so you always know which
quality tier you got.

Only if the GrabCut fallback ALSO fails (e.g. a genuinely empty/degenerate
prompt region) does `mask_object` return an error, of the shape:

```
mask_object: segmentation unavailable: SAM2 sidecar failed (segmentation
sidecar not installed/configured: sidecar python not found at
.../sidecar/.venv/bin/python3.12. ...); GrabCut fallback also failed
(<reason>)
```

**`mask_raster`**: if `DARKTABLE_MCP_SIDECAR_PYTHON`/`DARKTABLE_MCP_SIDECAR_MATTE`
aren't set (or the MODNet checkpoint is missing) and no co-located dev
sidecar is found, `mask_raster` returns an error immediately -- there is
**no fallback tier** for this tool (a hard-edged segmenter cannot produce a
soft matte, so degrading silently would defeat the reason this tool
exists):

```
mask_raster: matting sidecar not installed/configured: sidecar python not
found at .../sidecar/.venv/bin/python3.12. mask_raster needs the MODNet
matting sidecar (there is no lower-quality fallback -- a hard-edged
segmenter cannot produce a soft matte). Install it (see
dist/INSTALL-sidecar.md / sidecar/README.md 'Enabling real MODNet') ...
```

In both cases, no darktable state is touched (no module instance is
created before the matting/segmentation step runs) and the server process
itself stays up -- this is an expected, handled error path, not a crash.
`add_path_mask` and every other tool continue to work normally.
