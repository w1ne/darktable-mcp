"""Bridge from the MCP server process to the MODNet matting sidecar (T3.2),
for T3.3's mask_raster tool.

Unlike segmentation_tools.py (mask_object's SAM2/GrabCut path), there is
deliberately NO in-process fallback here. GrabCut (or any threshold/contour
trick) produces a hard-edged region, not a matte -- the entire point of the
raster-mask path (T3.1/T3.3) is a SOFT, continuous per-pixel alpha for hair/
fur/fine detail that a drawn polygon mask cannot represent. Degrading to a
hard-edged stand-in on sidecar failure would silently defeat the reason this
tool exists, so any failure here is a clear, actionable error instead:
"install the sidecar for matte masks" (mirrors mask_object's sidecar-absent
message, but WITHOUT a fallback that quietly changes the result's nature).

Same cross-venv subprocess pattern as segmentation_tools._run_sidecar: the
sidecar (`darktable-mcp/sidecar/matte.py`) lives in its own uv venv (onnx
runtime + opencv + numpy + pillow, no torch needed for inference), separate
from both darktable-mcp's own `.venv` and the darktable process itself
(which runs inside the docker bridge container with no Python at all). This
MCP server process runs on the HOST, so it subprocesses the sidecar venv's
python3.12 directly, passing it an image path this process can already read
(the original image file, or a host-remapped preview export -- see
server.py's mask_raster handler for which one it picks and why) and parsing
its stdout JSON.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from ..utils.errors import MattingServiceError

logger = logging.getLogger(__name__)

# darktable-mcp/darktable_mcp/tools/matting_tools.py -> darktable-mcp/
_MCP_ROOT = Path(__file__).resolve().parents[2]
SIDECAR_DIR = _MCP_ROOT / "sidecar"
SIDECAR_SCRIPT = SIDECAR_DIR / "matte.py"

# Same sidecar venv as segmentation_tools.py (matte.py and segment.py are
# co-located, see sidecar/README.md) -- reuses the SAME
# DARKTABLE_MCP_SIDECAR_PYTHON env var so a single install-sidecar setup
# covers both tools. matte.py itself gets its own script-path env var since
# it's a different entrypoint file:
#   DARKTABLE_MCP_SIDECAR_PYTHON  -- path to the sidecar venv's python3.12
#                                    (shared with segmentation_tools.py)
#   DARKTABLE_MCP_SIDECAR_MATTE   -- path to the sidecar's matte.py
#   DARKTABLE_MCP_MODNET_CHECKPOINT -- path to the MODNet ONNX checkpoint
#                                      (falls back to matte.py's own
#                                      MODNET_CHECKPOINT env var handling,
#                                      then the co-located dev-checkout
#                                      default)
# Falls back to the co-located dev-checkout layout (sidecar/.venv/...,
# sidecar/checkpoints/...) when unset, which is what the in-repo spike/tests
# use. Also overridable per-call via run_matting(sidecar_python=...,
# sidecar_script=..., checkpoint=...).
ENV_SIDECAR_PYTHON = "DARKTABLE_MCP_SIDECAR_PYTHON"
ENV_SIDECAR_MATTE = "DARKTABLE_MCP_SIDECAR_MATTE"
ENV_MODNET_CHECKPOINT = "DARKTABLE_MCP_MODNET_CHECKPOINT"
DEFAULT_SIDECAR_PYTHON = SIDECAR_DIR / ".venv" / "bin" / "python3.12"
DEFAULT_CHECKPOINT = SIDECAR_DIR / "checkpoints" / "modnet_photographic_portrait_matting.onnx"

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
    env = os.environ.get(ENV_SIDECAR_MATTE)
    if env:
        return Path(env)
    return SIDECAR_SCRIPT


def _checkpoint(override: str | None = None, script: Path | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get(ENV_MODNET_CHECKPOINT) or os.environ.get("MODNET_CHECKPOINT")
    if env:
        return Path(env)
    # Same fix as segmentation_tools' checkpoint default: resolve relative to
    # the ACTUAL configured script's directory, not the hardcoded dev-checkout
    # SIDECAR_DIR, so a real install (env vars pointing elsewhere) doesn't
    # silently look in the wrong place.
    base = script.parent if script else SIDECAR_DIR
    return base / "checkpoints" / "modnet_photographic_portrait_matting.onnx"


def run_matting(
    image_path: str,
    *,
    out_path: str,
    backend: str = "modnet",
    checkpoint: str | None = None,
    ref_size: int = 512,
    sidecar_python: str | None = None,
    sidecar_script: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Invoke `sidecar/matte.py` as a subprocess in its own venv to produce a
    16-bit grayscale PNG alpha matte at `out_path` (caller's responsibility
    to make this a UNIQUE path per call -- see server.py mask_raster's
    comment on why: rasterfile.c's mask cache is keyed on a hash of its
    params blob + image id, not file content, so reusing a path can serve a
    stale cached mask after the file on disk changes).

    Returns matte.py's JSON contract verbatim:
        {"alpha_path", "format", "size": {"w","h"}, "backend",
         "alpha_min", "alpha_max", "alpha_mean"}

    Raises MattingServiceError on ANY failure (venv/script/checkpoint
    missing, non-zero exit, timeout, malformed stdout) -- there is NO
    fallback tier here (see module docstring): callers (mask_raster) must
    treat this as "no matte, no darkroom side effects yet" and surface the
    error as-is, pointing the user at installing the sidecar.
    """
    python = _sidecar_python(sidecar_python)
    script = _sidecar_script(sidecar_script)
    if not python.is_file():
        raise MattingServiceError(
            "matting sidecar not installed/configured: sidecar python not "
            f"found at {python}. mask_raster needs the MODNet matting "
            "sidecar (there is no lower-quality fallback -- a hard-edged "
            "segmenter cannot produce a soft matte). Install it (see "
            "dist/INSTALL-sidecar.md / sidecar/README.md 'Enabling real "
            f"MODNet') and set {ENV_SIDECAR_PYTHON} (and {ENV_SIDECAR_MATTE} "
            "if it isn't at the default in-repo location), or pass "
            "sidecar_python explicitly. Every other darktable-mcp tool "
            "(including mask_object) works fine without it."
        )
    if not script.is_file():
        raise MattingServiceError(
            f"matting sidecar not installed/configured: {script} not found. "
            "Install the sidecar (see dist/INSTALL-sidecar.md) and set "
            f"{ENV_SIDECAR_MATTE}, or pass sidecar_script explicitly."
        )

    ckpt = _checkpoint(checkpoint, script) if backend == "modnet" else None
    if backend == "modnet" and not ckpt.is_file():
        raise MattingServiceError(
            f"matting sidecar checkpoint not found at {ckpt}. Download the "
            "MODNet ONNX checkpoint (see sidecar/README.md 'Enabling real "
            f"MODNet from scratch') or set {ENV_MODNET_CHECKPOINT}, or pass "
            "checkpoint explicitly."
        )

    out_dir = Path(out_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(python),
        str(script),
        "--image",
        str(image_path),
        "--out",
        str(out_path),
        "--format",
        "png16",
        "--backend",
        backend,
    ]
    if backend == "modnet":
        cmd += ["--checkpoint", str(ckpt), "--ref-size", str(ref_size)]

    logger.debug("matting sidecar cmd: %s", cmd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MattingServiceError(f"matting service unavailable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MattingServiceError(
            f"matting service unavailable: sidecar timed out after {timeout}s"
        ) from exc

    if proc.returncode != 0:
        raise MattingServiceError(
            "matting service unavailable: sidecar exited with code "
            f"{proc.returncode}: {proc.stderr.strip()[-2000:] or '(no stderr)'}"
        )

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MattingServiceError(
            f"matting service returned invalid JSON: {exc}; stdout={proc.stdout[:500]!r}"
        ) from exc

    if not isinstance(result, dict) or "alpha_path" not in result:
        raise MattingServiceError(f"matting service returned unexpected shape: {result!r}")
    return result
