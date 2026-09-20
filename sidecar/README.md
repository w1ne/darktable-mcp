# Segmentation + matting sidecar (T2.1, T3.2)

Standalone Python service. Turns a point/box (and best-effort free-text
label) prompt into a **normalized polygon contour** ready to hand to
`darktable.develop.add_path_mask` (T2.2). Does not import or depend on any
darktable C code, and has no dependency on the rest of `darktable_mcp` --
it's a plain function/CLI, callable from the T2.3 orchestrator (or anything
else) as a subprocess or an in-process import.

Model: **SAM2** (`facebookresearch/sam2`, tiny checkpoint), running for
real, CPU-only, in this environment. See "Status: real or stubbed" below.

## Interface contract

### Input

```python
segment(
    image_path: str,
    points: list[{"x": float, "y": float, "label": int=1}] | None = None,  # normalized 0..1, label 1=fg/0=bg click
    box: {"x": float, "y": float, "w": float, "h": float} | None = None,    # normalized 0..1
    label: str | None = None,   # free-text hint, best-effort -- see caveat below
    backend: str = "sam2",      # or "stub" (see "Status" section)
    target_min: int = 10,
    target_max: int = 30,
) -> dict
```

At least one of `points`/`box`/`label` is required. **`label` is accepted
for interface symmetry with the future orchestrator but SAM2 has no
language tower -- it does nothing on its own.** T2.3 is expected to turn
"face"/"sky" into points/box via Claude vision *before* calling this
sidecar; passing only `label="face"` today falls back to a fixed-radius
disc around the image center (see `StubEllipseModel`/no-op behavior), which
is not useful for real segmentation. Treat `label` as documentation-of-intent
until T2.3 exists, not as a working text-to-mask path.

### Output

```json
{
  "polygon": [{"x": 0.427, "y": 0.106}, {"x": 0.364, "y": 0.154}, "... 9 more points ..."],
  "bbox": {"x": 0.364, "y": 0.101, "w": 0.218, "h": 0.245},
  "score": 0.756,
  "num_points_raw": 1228,
  "num_points_simplified": 11,
  "backend": "sam2",
  "image_size": {"w": 960, "h": 1431}
}
```

- `polygon`: normalized (0..1) `{x,y}` points, in boundary order, ~10-30
  nodes, closed implicitly (last point connects back to the first --
  matches darktable's own path-node convention, no repeated closing point).
- `bbox`: normalized bounding box of `polygon`.
- `score`: model confidence if the backend provides one (SAM2 does; the
  stub backend always reports `1.0` since there's no real uncertainty).
- `num_points_raw` / `num_points_simplified`: contour size before/after
  Douglas-Peucker, for observability.
- **Holes / multiple parts**: darktable's `DT_MASKS_PATH` form is a single
  closed contour. If the predicted mask has holes or several disjoint
  blobs, this service keeps only the **largest-area external contour** and
  silently drops the rest (`segment.py::largest_external_contour`). This is
  a deliberate simplification the caller should know about, not a bug.

### CLI

```bash
.venv/bin/python3.12 segment.py \
  --image fixtures/portrait.jpg \
  --box 0.344,0.105,0.271,0.217 \
  --backend sam2 \
  --checkpoint checkpoints/sam2.1_hiera_small.pt \
  --model-cfg configs/sam2.1/sam2.1_hiera_s.yaml
# prints the JSON contract above to stdout
```

Point prompts: `--point 0.5,0.4` (repeatable; optional third value is the
fg/bg label, default `1`).

## Pipeline (real, backend-agnostic)

```
prompt (points/box)
  -> SegmentationModel.predict()      binary mask HxW bool + score
  -> largest_external_contour()       cv2.findContours(RETR_EXTERNAL), pick max area
  -> simplify_polygon()               cv2.approxPolyDP, epsilon binary-searched
                                       to land the node count in [target_min, target_max]
  -> normalize_polygon()              pixel (x,y) -> 0..1 by image width/height
  -> polygon_bbox()                   normalized bbox of the result
```

All of this is real code, unit-tested independently of which segmentation
model produced the mask (`test_segment.py`, tier 1).

## Status: real or stubbed

**Real SAM2 is wired up and working, CPU-only, in this checkout.** It was
not stubbed -- the environment turned out to have what it needed (see
below), so both the pipeline *and* the model are real and tested.

- Model: `facebookresearch/sam2`, PyPI package `sam2==1.1.0`.
- Checkpoint: **SAM 2.1 Hiera-small** (one step up from the smallest
  official checkpoint, ~176MB), `checkpoints/sam2.1_hiera_small.pt`,
  fetched from
  `https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt`.
  Bumped from tiny 2026-07-27 (see `segmentation_tools.py`'s
  `DEFAULT_CHECKPOINT` comment for the honest caveat: this was NOT proven
  to improve quality on this repo's own portrait fixture -- SAM2's score is
  a per-checkpoint self-confidence, not an absolute cross-size metric --
  it's a bet on Meta's general benchmarks for harder/more ambiguous scenes).
  `checkpoints/sam2.1_hiera_tiny.pt` (~149MB) is also still present in this
  checkout if you want to compare or revert.
- Config: `configs/sam2.1/sam2.1_hiera_s.yaml` (bundled inside the `sam2`
  package, resolved via hydra).
- Torch: `2.5.1+cpu` / `torchvision 0.20.1+cpu` (CPU-only wheels, no CUDA).
- Runtime: model load + one image inference on the portrait fixture (below)
  takes **~6-10s** on an 8-core CPU box (comparable to tiny in local
  testing). Fine for an interactive/agentic loop.

### Why this needed its own Python, and how it was solved

The repo's system `python3` is 3.8.10. `torch>=2.5.1` (required by
`sam2`'s own `setup.py`) has no wheels for Python < 3.9, and current SAM2
in practice wants 3.10+. This machine already has [`uv`](https://astral.sh/uv)
installed with a cached CPython 3.12.13, so the sidecar gets its own venv
built on that, independent of the system interpreter and independent of the
`darktable-mcp` package's own `.venv` (which is also 3.12, but this sidecar
is a separate service on purpose -- see PLAN.md T2.1 "independent service").
No system packages were touched to make this work.

### The one function that swaps the model

`segment.py::load_model(backend, **kwargs)` is the single swap-in point:

```python
def load_model(backend: str = "sam2", **kwargs) -> SegmentationModel: ...
```

- `backend="sam2"` (default): real `SAM2Model`. Needs `checkpoint` +
  `model_cfg` (args, or `SAM2_CHECKPOINT` / `SAM2_MODEL_CFG` env vars).
- `backend="stub"`: `StubEllipseModel` -- a **deterministic, non-ML**
  ellipse-fitting stand-in used to test the contour/simplify/normalize
  pipeline in isolation from model weights/runtime (see `test_segment.py`
  tier 1). Swap to a different real model (SAM1, MODNet, a larger SAM2
  checkpoint, ...) by adding another `SegmentationModel` subclass and a
  branch here; nothing downstream needs to change.

### Enabling / re-enabling real SAM2 from scratch

```bash
cd darktable-mcp/sidecar

# 1. venv on a 3.10+ Python (uv fetches/caches the interpreter for you)
uv venv --python 3.12 .venv

# 2. CPU-only torch first, from its own index (see requirements.txt for why
#    this must be a separate step from step 3)
uv pip install --python .venv/bin/python3.12 \
  --index-url https://download.pytorch.org/whl/cpu \
  -r requirements-torch.txt

# 3. sam2 + opencv + numpy + pillow + pytest, from the default index
uv pip install --python .venv/bin/python3.12 -r requirements.txt

# 4. the small checkpoint (~176MB)
mkdir -p checkpoints
curl -sL -o checkpoints/sam2.1_hiera_small.pt \
  https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt

# 5. run it
.venv/bin/python3.12 segment.py --image fixtures/portrait.jpg \
  --box 0.344,0.105,0.271,0.217 \
  --checkpoint checkpoints/sam2.1_hiera_small.pt \
  --model-cfg configs/sam2.1/sam2.1_hiera_s.yaml
```

Larger/more accurate checkpoints (base+/large) are at the same
`dl.fbaipublicfiles.com/segment_anything_2/092824/` path under
`sam2.1_hiera_{base_plus,large}.pt`, paired with
`configs/sam2.1/sam2.1_hiera_{b+,l}.yaml`; the smallest, tiny, is at
`sam2.1_hiera_tiny.pt`/`sam2.1_hiera_t.yaml`. Small is the current default
(one step up from tiny -- see "Status" above for the honest caveat on
whether that actually helps) -- swap `--checkpoint`/`--model-cfg` for a
bigger or smaller one depending on whether quality or latency matters more
for a given use case.

If plain `pip` instead of `uv` is preferred: same two-step install (`pip
install --index-url https://download.pytorch.org/whl/cpu -r
requirements-torch.txt` then `pip install -r requirements.txt`), from
inside a 3.10+ virtualenv's `pip` (e.g. `python3.12 -m venv .venv`).

## Fixture used

`fixtures/portrait.jpg` -- the *Mona Lisa* (Leonardo da Vinci, public
domain), fetched from Wikimedia Commons:
`https://commons.wikimedia.org/wiki/Special:FilePath/Mona_Lisa%2C_by_Leonardo_da_Vinci%2C_from_C2RMF_retouched.jpg?width=640`,
960x1431 JPEG. A painted portrait rather than a photo, chosen because it's
unambiguously public domain (no likeness/privacy concern) and has a clear,
well-lit, high-contrast face region -- exactly the "clear segmentable
region" the task calls for. `mire1.cr2` under
`src-dt/src/tests/integration/images/` is a test chart (no segmentable
subject), so it was not used here.

## Acceptance results (quoted, from this checkout)

**1. Point/box prompt -> normalized polygon whose bbox matches the prompted region.**

Command: box prompt `x=0.344, y=0.105, w=0.271, h=0.217` (face region of the
portrait fixture) through the real SAM2 backend. First few polygon points
and full output:

```json
{
  "polygon": [
    {"x": 0.4270833333333333, "y": 0.10552061495457722},
    {"x": 0.36354166666666665, "y": 0.15373864430468204},
    {"x": 0.375, "y": 0.3137665967854647},
    "... 8 more points ..."
  ],
  "bbox": {"x": 0.36354166666666665, "y": 0.1013277428371768, "w": 0.2177083333333334, "h": 0.24458420684835777},
  "score": 0.755932629108429,
  "num_points_raw": 1228,
  "num_points_simplified": 11,
  "backend": "sam2",
  "image_size": {"w": 960, "h": 1431}
}
```

Node count: **11** (within the 10-30 target). Predicted bbox
`(0.364, 0.101)-(0.581, 0.346)` vs prompted box
`(0.344, 0.105)-(0.615, 0.322)` -- **bbox IoU = 0.729** (computed in
`test_real_sam2_face_segmentation_on_portrait`, asserted `> 0.4`).

**2. Simplification: raw N -> simplified M, area/IoU sane.**

From `test_simplify_reduces_point_count_and_preserves_area` (a rasterized
circle, radius 180px on a 500x500 canvas -- independent of any model):

```
circle contour: 1016 raw points -> 12 simplified points, IoU=0.9496
```

**1016 -> 12** nodes (well inside 10-30), simplified-polygon-vs-original-mask
IoU **0.95** (asserted `> 0.9`). On the real SAM2 face mask above:
**1228 -> 11** nodes, simplified-vs-raw-mask IoU **0.897** (manually
verified during development; the automated test checks the SAM2-vs-prompt
bbox IoU instead, to keep the test independent of exact SAM2 version
drift).

**3. Coordinates normalized 0..1, in order, closed contour.**

Every returned polygon point is asserted `0.0 <= x <= 1.0` and
`0.0 <= y <= 1.0` (`test_stub_box_prompt_returns_polygon_matching_bbox`).
Boundary ordering is asserted directly in
`test_polygon_is_closed_ring_in_order` (angular progression around the
centroid is monotonic in one winding direction -- i.e. no point-order
shuffling survives simplify+normalize). `cv2.findContours` already returns
a closed ring (no repeated first/last point), which is the convention
carried straight through to the output -- matching what
`add_path_mask` (T2.2) expects for path-mask nodes.

**4. Real SAM2 weights used -- model, checkpoint, real result.**

Model: **SAM2.1 Hiera-tiny** (`facebookresearch/sam2`, PyPI `sam2==1.1.0`),
checkpoint `checkpoints/sam2.1_hiera_tiny.pt` (149MB,
`sam2.1_hiera_tiny.pt` from Meta's public checkpoint bucket), config
`configs/sam2.1/sam2.1_hiera_t.yaml`, CPU inference (`torch==2.5.1+cpu`).
Real result on the portrait fixture: **score 0.756**, 1228 raw contour
points, 11 after simplification, bbox IoU vs the prompted face box 0.729
(all quoted above). This section documents the ORIGINAL tiny-checkpoint
integration test (`test_segment.py`'s dedicated tiny-checkpoint test, still
present and still passing); the server's own default checkpoint has since
moved to small (2026-07-27, see "Status" above) -- the two are independent,
tiny is still fully supported and tested here. Since real weights were used, the "if stubbed" fallback
steps aren't needed here, but are documented above ("Enabling / re-enabling
real SAM2 from scratch") since this exact setup can go stale or be
unavailable in a different environment -- the `backend="stub"` path
(`StubEllipseModel`) remains available and is exercised by 6 of the 8 tests
specifically so the geometry pipeline stays verifiable without any model
weights at all.

## Running the tests

```bash
cd darktable-mcp/sidecar
.venv/bin/python3.12 -m pytest test_segment.py -v -s
```

8 tests, all passing in this checkout (~9s, includes one real SAM2
inference). 7 of them need no model weights (pure geometry + the
deterministic stub backend); the 8th (`test_real_sam2_face_segmentation_on_portrait`)
auto-skips if `checkpoints/sam2.1_hiera_tiny.pt` or `fixtures/portrait.jpg`
isn't present, so this file is still fully runnable in an environment where
real SAM2 weights were skipped as impractical.

## Files

- `segment.py` -- the service: data model (`Point`/`Box`), the
  `SegmentationModel` interface + `SAM2Model`/`StubEllipseModel`
  implementations, `load_model()` swap-in factory, the contour/simplify/
  normalize pipeline functions, the `segment()` entrypoint, and a CLI.
- `matte.py` -- the matting service (T3.2, see section below): the
  `MattingModel` interface + `MODNetONNXModel`/`StubGradientModel`
  implementations, `load_model()` swap-in factory, `save_alpha`/`load_alpha`,
  the `matte()` entrypoint, and a CLI.
- `requirements.txt` / `requirements-torch.txt` -- pinned deps, two-step
  install (see comments in `requirements.txt` for why).
- `test_segment.py` -- pipeline tests (stub backend, no weights needed) +
  one real-SAM2 integration test (auto-skipped if weights aren't present).
- `test_matte.py` -- pipeline tests (stub backend, no weights needed) + one
  real-MODNet integration test (auto-skipped if the ONNX checkpoint isn't
  present).
- `checkpoints/sam2.1_hiera_tiny.pt` -- the real SAM2.1 tiny checkpoint
  (gitignored -- see `.gitignore`; re-fetch with the command in "Enabling
  real SAM2" above).
- `checkpoints/modnet_photographic_portrait_matting.onnx` -- the real MODNet
  checkpoint (gitignored; re-fetch with the command in "Enabling real
  MODNet" below).
- `fixtures/portrait.jpg` -- the Mona Lisa test fixture (see "Fixture used").

## What T2.3 needs to know

Call `segment.segment(image_path, points=..., box=..., model=<preloaded
SegmentationModel>)` and reuse one loaded model across calls in the same
process (loading takes ~2-3s; per-image inference is the remaining ~3-4s) --
don't call `load_model()` fresh per request if latency matters. The
`points`/`box` prompt is expected to already be in normalized 0..1
image-space, i.e. Claude vision's job (T2.3) is to look at the image and
produce those coordinates; this sidecar's job stops at
prompt -> polygon. The returned `polygon` list is exactly the node list
`add_path_mask` (T2.2) needs, already normalized and node-count-bounded.

---

# Matting sidecar (T3.2)

Standalone Python service, same independence contract as `segment.py`
above: no darktable C code, no dependency on the rest of `darktable_mcp`,
callable as a subprocess or an in-process import.

**Matting is not segmentation.** `segment.py` (T2.1, SAM2) answers "is this
pixel part of the object" with a mask that's binary-ish once you look at it
per-pixel (even where SAM2's raw logits are continuous, the object contour
downstream of it collapses back to a hard boundary -- that's what a path
mask *is*). `matte.py` (T3.2, MODNet) answers "how much of this pixel is
foreground", a **continuous 0..1 value per pixel**, which is what actually
looks right on hair strands, flyaway hairs, motion blur, glass, smoke -- a
polygon can only fake that with jagged steps. This module's output feeds the
**raster** mask path (T3.1's producer, wired up by T3.3), as opposed to
`segment.py`'s **vector** path-node output.

## Interface contract

### Input

```python
matte(
    image_path: str,
    out_path: str | None = None,     # default: "<image>.matte.png" next to the input
    fmt: str = "png16",              # "png16" (default) or "npy" -- see "Output format" below
    backend: str = "modnet",         # or "stub" (see "Status" section)
) -> dict
```

Unlike `segment()`, there is **no points/box/label prompt** -- MODNet is a
dense, whole-image portrait matting model; it doesn't need one. Give it a
picture, it hands back a full-resolution alpha for "the person" (or, for a
painted portrait like the fixture, the equivalent salient subject).

### Output

```json
{
  "alpha_path": "fixtures/portrait.matte.png",
  "format": "png16",
  "size": {"w": 960, "h": 1431},
  "backend": "modnet",
  "alpha_min": 0.0,
  "alpha_max": 1.0,
  "alpha_mean": 0.6182103753089905
}
```

- `alpha_path`: where the float alpha matte was written (see "Output format"
  below for what's actually in the file).
- `size`: **native** size, equal to the input image's pixel dimensions --
  the matte is always resized back up to this size internally (see
  Pipeline), so it is pixel-aligned to the input. Any further
  downscaling/resampling to fit a particular darktable pipeline stage is
  **T3.3's concern**, not this module's.
- `alpha_min`/`alpha_max`/`alpha_mean`: quick observability stats over the
  full array, computed before writing.
- `backend`: which `MattingModel` implementation actually ran.
- With `return_array=True` (in-process callers only, not the CLI) the dict
  also carries `alpha_array`, the raw `HxW float32` numpy array.

### CLI

```bash
.venv/bin/python3.12 matte.py \
  --image fixtures/portrait.jpg \
  --backend modnet \
  --checkpoint checkpoints/modnet_photographic_portrait_matting.onnx \
  --out /tmp/portrait_alpha.png
# prints the JSON contract above to stdout
```

## Output format: 16-bit PNG (default), .npy alternative

**Decision: default on-disk format is a single-channel 16-bit grayscale PNG**
(`save_alpha`/`load_alpha` in `matte.py`), alpha in `[0,1]` linearly scaled
to `[0, 65535]`.

Why 16-bit PNG and not 8-bit: an 8-bit matte (256 levels) visibly bands on a
slow hair-to-background alpha gradient once it's used as a per-pixel
multiplier in a color operation -- exactly the "soft edge" this whole task
is about preserving. 16-bit gives a round-trip tolerance of `1/65535 ≈
1.5e-5`, far below anything a raster-mask consumer needs to care about
(verified in `test_save_load_png16_roundtrip` and, on the real model output,
in `test_real_modnet_portrait_matte_is_soft_and_localized`).

Why PNG and not e.g. TIFF/EXR: lossless, ubiquitous, trivially read back by
Python (`PIL`) *and* by darktable/ImageMagick/GraphicsMagick-adjacent
tooling without an extra dependency -- T3.3 doesn't need a special image
library just to load the matte.

`fmt="npy"` is offered as an alternative that skips 16-bit quantization
entirely (`np.save`, exact float32 round trip, see
`test_save_load_npy_roundtrip`) -- useful for automated tests or a
pure-Python consumer; not required for T3.3, which is expected to load the
PNG the same way it loads any other raster mask input.

**Native size, not downscaled**: the matte is written at the *original*
input image's pixel dimensions (`size` in the output dict == input image
size). T3.3 is responsible for any resampling down to whatever resolution a
given darktable pipeline stage actually needs.

## Pipeline (real, backend-agnostic)

```
image (RGB)
  -> resize to MODNet's working resolution   _modnet_resize_dims(): shortest
                                              edge -> 512, both dims rounded
                                              down to a multiple of 32
                                              (official MODNet demo-script
                                              algorithm -- required because
                                              the encoder has 5 stride-2
                                              stages)
  -> normalize to [-1, 1]                    (x/127.5 - 1), mean=std=0.5
  -> MattingModel.predict()                  onnxruntime forward pass ->
                                              HxW float32 alpha, 0..1, at
                                              working resolution
  -> resize back to the ORIGINAL image size  bilinear -- this is what makes
                                              the output pixel-aligned to
                                              the input image
  -> clip to [0, 1]
  -> save_alpha()                            16-bit PNG (default) or .npy
```

## Model: MODNet (photographic portrait matting), ONNX, onnxruntime CPU

**Real MODNet is wired up and working, CPU-only, in this checkout** -- same
situation as SAM2 in the segmentation half of this README: the environment
had what it needed, so both the pipeline *and* the model are real and
tested; no stand-in was necessary in this environment (though one is
provided and documented below regardless, same policy as `segment.py`).

Model choice reasoning (per PLAN.md T3.2, "prefer the lightest that gives
real hair alpha CPU"):

- **MODNet** (Ke et al., *"Is a Green Screen Really Necessary for
  Real-Time Portrait Matting?"*) was picked over BiRefNet (much heavier,
  transformer-based, seconds-per-image even on GPU -- overkill for a CPU
  sidecar) and over robust-video-matting (RVM is built around a temporal/
  recurrent architecture across video frames; this task is single-image).
  MODNet is a single, light, real-time-oriented CNN purpose-built for
  portrait/hair alpha matting from one photo, with an official, freely
  downloadable checkpoint -- exactly the CPU-feasible sweet spot the task
  asked for.
- **Checkpoint**: `modnet_photographic_portrait_matting.onnx` (the official
  MODNet checkpoint, exported to ONNX), ~24.7MB, fetched from the
  `DavG25/modnet-pretrained-models` mirror on Hugging Face:
  `https://huggingface.co/DavG25/modnet-pretrained-models/resolve/main/models/modnet_photographic_portrait_matting.onnx`.
  Same model as the one distributed in MODNet's own GitHub repo
  (`ZHKKKe/MODNet`), just re-hosted somewhere directly `curl`-able (the
  original repo ships it via Google Drive, which needs an interactive
  confirm-token dance to script around).
- **Runtime**: `onnxruntime==1.27.0` (CPU execution provider) -- **no torch
  dependency for inference**, unlike the SAM2 backend. Model load + one
  full-resolution (960x1431) inference on the portrait fixture: **~0.77s
  wall clock** (see "Enabling real MODNet" below for the exact command).
  Lighter and faster than SAM2 in this checkout because it's a smaller CNN
  and ONNX Runtime skips torch's Python-level overhead entirely.

### The one function that swaps the model

`matte.py::load_model(backend, **kwargs)` is the single swap-in point,
mirroring `segment.py::load_model`:

```python
def load_model(backend: str = "modnet", **kwargs) -> MattingModel: ...
```

- `backend="modnet"` (default): real `MODNetONNXModel`. Needs `checkpoint`
  (arg, or `MODNET_CHECKPOINT` env var).
- `backend="stub"`: `StubGradientModel` -- a **deterministic, non-ML**
  radial-falloff stand-in used to test the resize/normalize/save-load
  pipeline in isolation from model weights/runtime (see `test_matte.py`
  tier 1). Swap to a different real model (BiRefNet, RVM, a different
  MODNet export/quantization, ...) by adding another `MattingModel`
  subclass and a branch here; nothing downstream (resize-back, save/load)
  needs to change.

### Enabling / re-enabling real MODNet from scratch

```bash
cd darktable-mcp/sidecar

# Reuses the same .venv as segment.py -- onnxruntime has no special index
# requirement, so it's just one more line in requirements.txt (no two-step
# dance like SAM2's torch install).
uv pip install --python .venv/bin/python3.12 -r requirements.txt

# the ONNX checkpoint (~24.7MB)
mkdir -p checkpoints
curl -sL -o checkpoints/modnet_photographic_portrait_matting.onnx \
  https://huggingface.co/DavG25/modnet-pretrained-models/resolve/main/models/modnet_photographic_portrait_matting.onnx

# run it
.venv/bin/python3.12 matte.py --image fixtures/portrait.jpg \
  --backend modnet \
  --checkpoint checkpoints/modnet_photographic_portrait_matting.onnx \
  --out /tmp/portrait_alpha.png
```

If plain `pip` instead of `uv` is preferred: `pip install -r
requirements.txt` from inside the same 3.10+ virtualenv used for
`segment.py` (or any Python >= 3.8 venv, actually -- `matte.py` itself has
no minimum-Python constraint beyond what `onnxruntime`/`numpy`/`opencv`/
`pillow` need; it only shares a venv with `segment.py` for convenience).

## Acceptance results (quoted, from this checkout)

**1. Float alpha matte, size, value range, soft-edge (intermediate-value)
evidence.**

Command: `matte("fixtures/portrait.jpg", backend="modnet",
checkpoint="checkpoints/modnet_photographic_portrait_matting.onnx")`.

```
size = 960 x 1431 (matches input image exactly)
alpha_min  = 0.0000
alpha_max  = 1.0000
alpha_mean = 0.6182
```

Histogram over all 1,373,760 pixels (10 bins):

```
0.0-0.1: 508203   0.1-0.2: 6361    0.2-0.3: 3931    0.3-0.4: 2904    0.4-0.5: 3113
0.5-0.6: 3347     0.6-0.7: 3177    0.7-0.8: 3446    0.8-0.9: 4934    0.9-1.0: 834344
```

**41,802 pixels (3.04%) have alpha strictly between 0.05 and 0.95** -- real
mass in the soft-edge band, not just a thresholded 0/1 mask. A binary
segmentation mask (SAM2 + contour, the T2.1 path) has essentially zero mass
there by construction (its "alpha" only exists as an artifact of how a hard
polygon anti-aliases when rasterized, nowhere near this magnitude) -- this
is exactly the "soft partial-coverage alpha" distinction PLAN.md T3.2 calls
out.

**2. Localization: subject/hair region high, background corner ~0.**

Face+hair box `x=[0.30,0.65], y=[0.03,0.36]` (normalized, expanded upward
from the T2.1 face-only box to include hair) vs. background top-left corner
`x=[0,0.1], y=[0,0.1]`:

```
face/hair box mean alpha: 0.7024  (shape 473 x 336)
bg corner mean alpha:     0.0000072
```

Difference **0.702** -- the matte is squarely localized to the subject, not
uniformly gray/noisy over the whole frame.

**3. Output format round-trips.**

```
roundtrip max_abs_diff = 7.63e-06
roundtrip mean_abs_diff = 3.18e-06
```

Both comfortably inside the 16-bit-PNG quantization tolerance (`1/65535 ≈
1.5e-5`) -- write to `png16`, read back, same float values. `.npy` round
trips exactly (`np.testing.assert_array_equal`, see
`test_save_load_npy_roundtrip`).

**4. Real model used -- name, checkpoint, CPU runtime.**

Model: **MODNet**, `modnet_photographic_portrait_matting.onnx` (official
checkpoint, ONNX export, ~24.7MB), `onnxruntime==1.27.0`
(`CPUExecutionProvider`). Full pipeline (image load + resize + inference +
resize-back + write PNG) on the 960x1431 portrait fixture: **~0.77s wall
clock** (`time .venv/bin/python3.12 matte.py --image fixtures/portrait.jpg
--backend modnet --checkpoint checkpoints/modnet_photographic_portrait_matting.onnx`).
Since real weights were used, the stand-in fallback isn't load-bearing here,
but `backend="stub"` (`StubGradientModel`) remains available and is
exercised by 5 of the 6 `test_matte.py` tests specifically so the I/O
pipeline stays verifiable without any model weights at all, same policy as
`segment.py`.

## Running the tests

```bash
cd darktable-mcp/sidecar
.venv/bin/python3.12 -m pytest test_matte.py -v -s
```

7 tests, all passing in this checkout (~0.8s total, includes one real
MODNet inference). 6 of them need no model weights (pure I/O + the
deterministic stub backend); the 7th
(`test_real_modnet_portrait_matte_is_soft_and_localized`) auto-skips if
`checkpoints/modnet_photographic_portrait_matting.onnx` or
`fixtures/portrait.jpg` isn't present.

## What T3.3 needs to know

Call `matte.matte(image_path, backend="modnet", model=<preloaded
MattingModel>)` and reuse one loaded model across calls in the same process
(loading is near-instant for the ONNX model, well under the SAM2 load time --
per-image inference on the portrait fixture is ~0.77s total including
load). The returned `alpha_path` is a 16-bit grayscale PNG at the input
image's native resolution -- read it with `matte.load_alpha(path)` (or
`PIL.Image.open(path)` directly, dividing by 65535) to get back a `HxW
float32 [0,1]` array. T3.3 is expected to feed that array (resampled to
whatever resolution the target darktable raster-mask stage needs -- this
module does not resample beyond matching the input image's own size) into
the T3.1 raster producer. `matte()` takes no points/box/label prompt --
MODNet is dense/whole-image; if a future need arises to matte just one of
several subjects in a frame, that would need either a different model or a
pre-crop step upstream of this module, neither of which exists yet.

### T3.3 status: done -- `mask_raster` MCP tool wired end to end

`darktable_mcp/tools/matting_tools.py` subprocesses this exact CLI (own
venv, cross-process, same pattern as `segmentation_tools.py`'s SAM2 call)
from `server.py`'s `mask_raster` tool. Notes for anyone touching either
side of this boundary:

- **No format conversion needed.** `save_alpha`'s default 16-bit grayscale
  PNG is fed to `iop/rasterfile.c` (T3.1's producer) UNCHANGED. libpng's
  `png_set_gray_to_rgb` (called by `dt_imageio_png_read_header`,
  `imageio_png.c`) expands a single-channel gray PNG to 3 identical RGB
  samples before rasterfile ever reads pixel bytes, so rasterfile's
  fixed `base = 3*k` (8-bit) / `base = 6*k` (16-bit) offsets land on the
  same value three times over -- exactly what `mode=ALL`'s `MAX(R,G,B)`
  needs. Verified empirically (PNG IHDR bit depth 16 / color type 0) and,
  more importantly, end to end through real darktable (see PLAN.md T3.3
  acceptance results): the matte drives a real, spatially-varying raster
  mask with no intermediate PFM conversion step.
- **Unique output path per call is load-bearing, not cosmetic.**
  `iop/rasterfile.c`'s mask cache (`dt_rasterfile_cache_t`) is keyed on
  `dt_hash(self->params, self->params_size)` + image id -- i.e. the
  `path`/`file` STRINGS in the params blob, never the file's bytes on disk.
  Reusing a filename across two different mattes would risk the pipe
  serving the FIRST matte's cached mask for the second call. `mask_raster`
  writes every matte to `mattes/matte-<uuid4>.png`, guaranteeing a cache
  miss (new hash) every time.
- **Matting input**: `mask_raster` feeds the actual open image file
  directly to `matte()` when it's a format PIL/`matte.py` reads natively
  (jpg/png/tif/bmp/webp); for RAW files it can't read directly (CR2/NEF/
  ARW/...), it renders a 4096x4096-capped full-resolution darkroom preview
  export first and mattes that instead (capped by the preview pipe's own
  resolution, not the sensor's native resolution -- a real limitation for
  hair-level detail on RAW sources, documented in `server.py`, not solved
  by this module).
