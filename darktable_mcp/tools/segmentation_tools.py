"""Bridge from the MCP server process to the SAM2 segmentation sidecar (T2.1),
with an in-process OpenCV GrabCut fallback (see local_segment.py) for the
common case where the sidecar isn't installed/reachable.

Selection order, every call:
    1. Try the SAM2 sidecar subprocess (`_run_sidecar`) -- if it is
       configured (env vars / co-located dev venv) AND runs successfully,
       its (higher-quality) polygon wins.
    2. If the sidecar is not configured, not reachable, times out, or
       fails in any other way, fall back to the local GrabCut segmenter
       (`local_segment.segment_grabcut`) -- zero-install, bundled in this
       package's own venv (opencv-python-headless + numpy are ordinary
       pyproject dependencies, not optional extras).
    3. Only if BOTH fail is a SegmentationServiceError raised to the
       caller (mask_object) -- the error message includes why each one
       failed.
Either path's result carries `"backend"` ("sam2" or "grabcut") so
mask_object can tell the caller which quality tier was actually used.

The sidecar (`darktable-mcp/sidecar/segment.py`) lives in its OWN uv venv
(Python 3.12 + torch/sam2), separate from both this package's `.venv` and
the darktable process. darktable itself runs inside the docker bridge
container (docker/run-dt-bridge.sh) which has no Python/torch at all -- only
`/opt/darktable` (read-only) and the bind-mounted `/run` cache dir. So
segmentation cannot run "inside darktable" by construction; it doesn't need
to. This MCP server process runs on the HOST (it's the thing polling
request-*.json/response-*.json against the container), so the natural, and
only practical, place to invoke the sidecar is right here: a subprocess to
the sidecar's own venv Python, on the host, passing it a preview PNG that
`get_preview` already wrote to a host-readable path (server.py's
`_remap_bridge_path` handles the container->host path swap for that PNG
before it ever reaches this module). No new IPC channel, no code sharing
across venvs -- just `subprocess.run([sidecar_venv_python, segment.py, ...])`
and parse its stdout JSON. This mirrors how CameraTools/CLIWrapper already
shell out to external tools (gphoto2, darktable-cli) elsewhere in this repo.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from ..utils.errors import LabelNotFoundInImageError, SegmentationServiceError
from . import local_segment

logger = logging.getLogger(__name__)

# darktable-mcp/darktable_mcp/tools/segmentation_tools.py -> darktable-mcp/
_MCP_ROOT = Path(__file__).resolve().parents[2]
SIDECAR_DIR = _MCP_ROOT / "sidecar"
SIDECAR_SCRIPT = SIDECAR_DIR / "segment.py"

# The sidecar's own venv (see sidecar/README.md "Enabling ... from scratch":
# `uv venv --python 3.12 .venv`), NOT darktable-mcp/.venv and NOT the system
# python3. This package deliberately does NOT bundle the sidecar (torch CPU
# + the 149MB checkpoint are too heavy / the wrong shape for this .deb -- see
# dist/INSTALL-sidecar.md), so on a packaged install SIDECAR_DIR normally
# does not exist at all and the two env vars below are the only way to point
# this process at wherever the sidecar venv actually got set up:
#   DARKTABLE_MCP_SIDECAR_PYTHON  -- path to the sidecar venv's python3.12
#   DARKTABLE_MCP_SIDECAR_SEGMENT -- path to the sidecar's segment.py
# Falls back to the co-located dev-checkout layout (sidecar/.venv/...) when
# unset, which is what the in-repo spike/tests use. Also overridable per-call
# via run_segmentation(sidecar_python=..., sidecar_script=...) (e.g. tests
# pointing at a nonexistent path to simulate "sidecar down" -- ACCEPTANCE
# T2.3-S2 / T2.5 "no sidecar configured").
ENV_SIDECAR_PYTHON = "DARKTABLE_MCP_SIDECAR_PYTHON"
ENV_SIDECAR_SEGMENT = "DARKTABLE_MCP_SIDECAR_SEGMENT"
DEFAULT_SIDECAR_PYTHON = SIDECAR_DIR / ".venv" / "bin" / "python3.12"
# Bumped from tiny -> small (2026-07-27): one step up on Meta's own
# tiny/small/base_plus/large quality-vs-speed curve. NOT locally proven to
# help -- a single local test on the bundled portrait fixture actually
# reported a LOWER self-confidence score for small (0.60) than tiny (0.76)
# on that one easy, well-lit, front-facing image. That's not a reliable
# quality signal either way: SAM2's score is a self-calibrated per-checkpoint
# confidence, not an absolute metric comparable across model sizes, and one
# easy fixture has no headroom to show a harder-scene improvement. The
# rationale for bumping anyway is Meta's published benchmarks (small/base+/
# large generally outperform tiny on complex/ambiguous scenes) and that the
# bugreport motivating this change was exactly that kind of hard scene
# (unusual body pose against a branchy background) -- not something this
# one fixture tests. Revert to hiera_tiny.pt/sam2.1_hiera_t.yaml (both here
# and in the literal fallback below) if small doesn't actually help in
# practice; runtime cost was comparable in the one local timing check (~6-10s
# either way on an 8-core CPU box, within measurement noise).
DEFAULT_CHECKPOINT = SIDECAR_DIR / "checkpoints" / "sam2.1_hiera_small.pt"
# Hydra config identifier bundled inside the installed `sam2` package (NOT a
# filesystem path relative to cwd) -- see sidecar/README.md CLI example.
DEFAULT_MODEL_CFG = "configs/sam2.1/sam2.1_hiera_s.yaml"

DEFAULT_TIMEOUT = 60.0


def _sidecar_python(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get(ENV_SIDECAR_PYTHON)
    if env:
        return Path(env)
    return DEFAULT_SIDECAR_PYTHON


def _sidecar_script(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get(ENV_SIDECAR_SEGMENT)
    if env:
        return Path(env)
    return SIDECAR_SCRIPT


def run_segmentation(
    image_path: str,
    *,
    points: list[dict[str, Any]] | None = None,
    box: dict[str, float] | None = None,
    label: str | None = None,
    backend: str = "sam2",
    checkpoint: str | None = None,
    model_cfg: str | None = None,
    sidecar_python: str | None = None,
    sidecar_script: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_grabcut_fallback: bool = True,
    min_nodes: int | None = None,
    max_nodes: int | None = None,
    target_nodes: int | None = None,
) -> dict[str, Any]:
    """Segment `image_path` for the given prompt, preferring the SAM2
    sidecar and falling back to the in-process GrabCut segmenter (see
    module docstring for the full selection order).

    Returns the (SAM2 or GrabCut) result's JSON contract verbatim:
        {"polygon": [{"x","y"}, ...], "bbox": {...}, "score": float|None,
         "num_points_raw", "num_points_simplified", "backend", "image_size"}
    `backend` is "sam2" or "grabcut" depending on which one actually ran --
    callers should surface it so the caller/user knows the quality tier.

    Raises SegmentationServiceError only if BOTH the sidecar AND the
    GrabCut fallback fail (or if `allow_grabcut_fallback=False` and only
    the sidecar was tried) -- callers must treat that as "no polygon, no
    side effects", never a half-populated result.

    `allow_grabcut_fallback=False` disables the fallback (e.g. a caller
    that specifically wants to test/force sidecar-only behavior); the
    default is True so mask_object always gets *something* zero-install.

    `min_nodes`/`max_nodes` override the Douglas-Peucker simplification
    target node count (both backends default to 10..48 -- see
    sidecar/segment.py and local_segment.py's DEFAULT_TARGET_MIN/MAX).
    Raise max_nodes for a complex/non-convex silhouette that's losing real
    shape detail at the default cap. `target_nodes` (defaults to max_nodes)
    is what the simplifier actually converges toward -- see
    simplify_polygon's docstring for the 2026-07-27 bugreport this fixes
    (raising max_nodes alone used to have no effect on the result).
    """
    if not points and not box and not label:
        raise SegmentationServiceError(
            "mask_object needs at least one of points/box/label to prompt "
            "the segmentation sidecar -- pick a point or box on the subject "
            "from a get_preview image first."
        )

    sidecar_error: Exception | None = None
    try:
        return _run_sidecar(
            image_path,
            points=points,
            box=box,
            label=label,
            backend=backend,
            checkpoint=checkpoint,
            model_cfg=model_cfg,
            sidecar_python=sidecar_python,
            sidecar_script=sidecar_script,
            timeout=timeout,
            min_nodes=min_nodes,
            max_nodes=max_nodes,
            target_nodes=target_nodes,
        )
    except LabelNotFoundInImageError:
        # Grounding DINO gave a real, confident "not in this image" answer
        # -- GrabCut has no text grounding either (see local_segment's
        # _label_only_rect_px), so falling back would silently swap this
        # correct rejection for a low-quality generic-rect guess. Propagate
        # as-is, never fall back, regardless of allow_grabcut_fallback.
        raise
    except SegmentationServiceError as exc:
        sidecar_error = exc
        if not allow_grabcut_fallback:
            raise

    logger.info(
        "SAM2 sidecar unavailable (%s); falling back to in-process GrabCut",
        sidecar_error,
    )
    grabcut_kwargs: dict[str, Any] = {}
    if min_nodes is not None:
        grabcut_kwargs["target_min"] = min_nodes
    if max_nodes is not None:
        grabcut_kwargs["target_max"] = max_nodes
    if target_nodes is not None:
        grabcut_kwargs["target_nodes"] = target_nodes
    try:
        return local_segment.segment_grabcut(
            image_path, points=points, box=box, label=label, **grabcut_kwargs
        )
    except Exception as grabcut_error:  # noqa: BLE001 - report both failures verbatim
        raise SegmentationServiceError(
            "segmentation unavailable: SAM2 sidecar failed "
            f"({sidecar_error}); GrabCut fallback also failed "
            f"({grabcut_error})"
        ) from grabcut_error


def _resolve_default_checkpoint(script_parent: Path) -> tuple:
    """Pick the checkpoint/model_cfg pair to default to, next to the
    ACTUAL configured segment.py (script_parent) -- same reasoning as the
    existing script.parent-relative FIX below.

    2026-07-27 regression this exists to prevent: bumping the *default*
    checkpoint filename (tiny -> small) silently broke every EXISTING
    install-sidecar checkout, which only ever downloaded tiny -- the new
    default pointed at a file that plain doesn't exist there, so every
    call fell through to "sidecar failed" and silently downgraded to the
    GrabCut fallback (no error surfaced -- the exact silent-quality-loss
    failure mode this codebase otherwise goes out of its way to avoid).
    Fix: prefer small if it's actually on disk, else fall back to tiny if
    THAT is on disk (covers every pre-2026-07-27 install-sidecar checkout
    without needing a re-run), else default to small anyway so a fresh
    install's error message still points at the modern name."""
    checkpoints_dir = script_parent / "checkpoints"
    small = checkpoints_dir / "sam2.1_hiera_small.pt"
    if small.is_file():
        return small, "configs/sam2.1/sam2.1_hiera_s.yaml"
    tiny = checkpoints_dir / "sam2.1_hiera_tiny.pt"
    if tiny.is_file():
        return tiny, "configs/sam2.1/sam2.1_hiera_t.yaml"
    return small, "configs/sam2.1/sam2.1_hiera_s.yaml"


def _run_sidecar(
    image_path: str,
    *,
    points: list[dict[str, Any]] | None = None,
    box: dict[str, float] | None = None,
    label: str | None = None,
    backend: str = "sam2",
    checkpoint: str | None = None,
    model_cfg: str | None = None,
    sidecar_python: str | None = None,
    sidecar_script: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    min_nodes: int | None = None,
    max_nodes: int | None = None,
    target_nodes: int | None = None,
) -> dict[str, Any]:
    """Invoke `sidecar/segment.py` as a subprocess in its own venv.

    Returns the sidecar's JSON contract verbatim (see run_segmentation's
    docstring). Raises SegmentationServiceError on ANY failure (venv/binary
    missing, non-zero exit, timeout, malformed stdout) -- run_segmentation
    catches this to try the GrabCut fallback.
    """
    if not points and not box and not label:
        raise SegmentationServiceError(
            "mask_object needs at least one of points/box/label to prompt "
            "the segmentation sidecar -- pick a point or box on the subject "
            "from a get_preview image first."
        )

    python = _sidecar_python(sidecar_python)
    script = _sidecar_script(sidecar_script)
    if not python.is_file():
        raise SegmentationServiceError(
            "segmentation sidecar not installed/configured: sidecar python "
            f"not found at {python}. Install the optional SAM2 sidecar (see "
            "dist/INSTALL-sidecar.md) and set DARKTABLE_MCP_SIDECAR_PYTHON "
            "(and DARKTABLE_MCP_SIDECAR_SEGMENT if it isn't at the default "
            "in-repo location), or pass sidecar_python explicitly. "
            "add_path_mask and every other tool work fine without it -- "
            "only mask_object's auto-segmentation needs the sidecar."
        )
    if not script.is_file():
        raise SegmentationServiceError(
            f"segmentation sidecar not installed/configured: {script} not "
            "found. Install the optional SAM2 sidecar (see "
            "dist/INSTALL-sidecar.md) and set DARKTABLE_MCP_SIDECAR_SEGMENT, "
            "or pass sidecar_script explicitly."
        )

    cmd: list[str] = [str(python), str(script), "--image", str(image_path)]
    for p in points or []:
        x, y = p["x"], p["y"]
        lbl = p.get("label", 1)
        cmd += ["--point", f"{x},{y},{lbl}"]
    if box:
        cmd += ["--box", f"{box['x']},{box['y']},{box['w']},{box['h']}"]
    if label:
        cmd += ["--label", label]
    if min_nodes is not None:
        cmd += ["--min-nodes", str(min_nodes)]
    if max_nodes is not None:
        cmd += ["--max-nodes", str(max_nodes)]
    if target_nodes is not None:
        cmd += ["--target-nodes", str(target_nodes)]

    cmd += ["--backend", backend]
    if backend == "sam2":
        # FIX: default checkpoint next to the ACTUAL configured segment.py
        # (script.parent), not the hardcoded dev-checkout SIDECAR_DIR. The
        # dev-checkout default (DEFAULT_CHECKPOINT) only happens to be
        # right when `script` IS the co-located dev sidecar; any real
        # install (env vars pointing elsewhere -- e.g. darktable-mcp
        # install-sidecar's own default ~/.local/share/darktable-mcp/
        # sidecar/) previously silently looked in the wrong place and
        # always fell through to "sidecar failed", which is straightforward
        # to miss because the GrabCut fallback (see run_segmentation) masks
        # it with a working-but-lower-quality result instead of a loud error.
        default_ckpt, default_cfg = _resolve_default_checkpoint(script.parent)
        cmd += ["--checkpoint", checkpoint or str(default_ckpt)]
        cmd += ["--model-cfg", model_cfg or default_cfg]

    logger.debug("segmentation sidecar cmd: %s", cmd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SegmentationServiceError(f"segmentation service unavailable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SegmentationServiceError(
            f"segmentation service unavailable: sidecar timed out after {timeout}s"
        ) from exc

    if proc.returncode != 0:
        raise SegmentationServiceError(
            "segmentation service unavailable: sidecar exited with code "
            f"{proc.returncode}: {proc.stderr.strip()[-2000:] or '(no stderr)'}"
        )

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SegmentationServiceError(
            f"segmentation service returned invalid JSON: {exc}; stdout={proc.stdout[:500]!r}"
        ) from exc

    # Grounding DINO ran fine but found nothing matching `label` -- a real,
    # confident answer (see sidecar/segment.py's LabelNotFoundError), NOT a
    # sidecar/infra failure. A distinct exception type so run_segmentation
    # skips its GrabCut fallback for this one case specifically (GrabCut has
    # no text grounding either -- falling back would silently swap a
    # correct rejection for a low-quality generic-rect guess).
    if isinstance(result, dict) and result.get("error_type") == "label_not_found":
        raise LabelNotFoundInImageError(result.get("error") or "label not found in image")

    if not isinstance(result, dict) or "polygon" not in result:
        raise SegmentationServiceError(
            f"segmentation service returned unexpected shape: {result!r}"
        )
    return result
