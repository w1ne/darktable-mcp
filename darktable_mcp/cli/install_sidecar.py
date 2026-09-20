"""`darktable-mcp install-sidecar` -- one-command SAM2 + MODNet sidecar setup.

Does everything dist/INSTALL-sidecar.md otherwise describes as a manual,
multi-step process, for BOTH optional upgrades sharing one sidecar venv:

    mask_object (T2.1/T2.3): SAM2 precise segmentation (else GrabCut fallback)
    mask_raster (T3.2/T3.3): MODNet soft matting (NO fallback -- see matte.py)

Steps:
    1. lay down a copy of the sidecar source (segment.py + matte.py +
       requirements*.txt, vendored as package data in
       darktable_mcp/sidecar_assets/ -- see that directory's README.md -- so
       no separate git clone/copy is needed),
    2. create a `uv venv --python 3.12` at <target>/.venv,
    3. install pinned CPU-only torch, THEN sam2 + transformers (Grounding
       DINO, 2026-07-27 -- gives mask_object's label-only prompts real text
       grounding instead of doing nothing; two separate steps -- see
       requirements.txt's own comment for why torch must not be merged with
       the rest),
    4. install onnxruntime (MODNet's only inference dependency -- no torch
       needed for it; checked/installed independently of step 3 so this
       still completes even on an older sidecar venv that already has
       sam2/torch but predates the onnxruntime pin),
    5. download the sam2.1_hiera_small checkpoint (~176MB),
    6. download the MODNet ONNX checkpoint (~24.7MB, from the HF mirror
       documented in sidecar/README.md "Enabling real MODNet"),
    7. print (and write to a config file) the exact env-var export lines
       (DARKTABLE_MCP_SIDECAR_PYTHON / _SEGMENT / _MATTE /
       DARKTABLE_MCP_MODNET_CHECKPOINT) needed to point the darktable-mcp
       server at the sidecar it just built -- covering BOTH mask_object and
       mask_raster from this one run.

Idempotent: every step checks whether its own output already exists/works
and skips re-doing it unless --force is passed. Safe to re-run after a
partial/interrupted run (e.g. network dropped mid-download), and safe to
re-run on top of an OLDER sidecar install that only ever had SAM2 set up
(step 4/6 will notice onnxruntime/the MODNet checkpoint are missing and add
them without redoing steps 1-3/5).

NOT run automatically by anything else -- mask_object works perfectly well
without ever running this (see segmentation_tools.py's GrabCut fallback);
this command exists purely to make the *optional* SAM2 and MODNet upgrades
a single command instead of a multi-page manual doc. mask_raster, unlike
mask_object, has no lower-quality fallback tier (see matte.py/errors.py
MattingServiceError) -- for mask_raster this command is not just a quality
upgrade, it is what makes the tool work at all.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

CHECKPOINT_URL = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt"
CHECKPOINT_NAME = "sam2.1_hiera_small.pt"
MODEL_CFG = "configs/sam2.1/sam2.1_hiera_s.yaml"
TORCH_INDEX_URL = "https://download.pytorch.org/whl/cpu"
# Below this, a partially-downloaded/truncated checkpoint is treated as
# incomplete and re-downloaded even without --force (the real file is
# ~176MB; anything under ~140MB is clearly a broken/partial fetch).
MIN_CHECKPOINT_BYTES = 140 * 1024 * 1024

# MODNet (T3.2, mask_raster) -- official checkpoint, ONNX export, re-hosted
# on the HF mirror documented in sidecar/README.md "Enabling real MODNet
# from scratch" (same model as ZHKKKe/MODNet's own GitHub release, just
# directly curl-able from here).
MODNET_CHECKPOINT_URL = (
    "https://huggingface.co/DavG25/modnet-pretrained-models/resolve/main/"
    "models/modnet_photographic_portrait_matting.onnx"
)
MODNET_CHECKPOINT_NAME = "modnet_photographic_portrait_matting.onnx"
ONNXRUNTIME_SPEC = "onnxruntime>=1.17.0"
# Real file is ~24.7MB; anything under ~15MB is clearly a broken/partial
# fetch (same truncation-guard idea as MIN_CHECKPOINT_BYTES for SAM2).
MIN_MODNET_CHECKPOINT_BYTES = 15 * 1024 * 1024


def _default_target_dir(home: Path) -> Path:
    return home / ".local" / "share" / "darktable-mcp" / "sidecar"


def _config_path(home: Path) -> Path:
    return home / ".config" / "darktable-mcp" / "sidecar.env"


def _venv_python(target_dir: Path) -> Path:
    return target_dir / ".venv" / "bin" / "python3.12"


def _checkpoint_path(target_dir: Path) -> Path:
    return target_dir / "checkpoints" / CHECKPOINT_NAME


def _modnet_checkpoint_path(target_dir: Path) -> Path:
    return target_dir / "checkpoints" / MODNET_CHECKPOINT_NAME


def _segment_script_path(target_dir: Path) -> Path:
    return target_dir / "segment.py"


def _matte_script_path(target_dir: Path) -> Path:
    return target_dir / "matte.py"


def _assets_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "sidecar_assets"


def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, **kwargs)


def _uv_path() -> str | None:
    return shutil.which("uv")


def step_copy_source(target_dir: Path, force: bool) -> bool:
    """Copy segment.py + matte.py + requirements*.txt from the package's
    bundled sidecar_assets/ into target_dir. Returns True on success."""
    print("[1/7] sidecar source (SAM2 segment.py + MODNet matte.py)")
    target_dir.mkdir(parents=True, exist_ok=True)
    assets = _assets_dir()
    if not assets.is_dir():
        print(
            f"  ERROR: bundled sidecar assets not found at {assets} -- this "
            "darktable-mcp install looks broken/incomplete (missing package "
            "data). Cannot continue."
        )
        return False
    any_copied = False
    for name in ("segment.py", "matte.py", "requirements.txt", "requirements-torch.txt"):
        src = assets / name
        dst = target_dir / name
        if dst.exists() and not force:
            print(f"  already present, skipping: {dst}")
            continue
        shutil.copyfile(src, dst)
        print(f"  wrote {dst}")
        any_copied = True
    if not any_copied and not force:
        print("  source already in place (use --force to re-copy)")
    return True


def step_create_venv(target_dir: Path, force: bool, python_version: str) -> bool:
    print("[2/7] uv venv (shared by SAM2 and MODNet)")
    uv = _uv_path()
    if not uv:
        print(
            "  ERROR: `uv` not found on PATH. Install it first: "
            "https://astral.sh/uv (or `pip install uv`), then re-run "
            "`darktable-mcp install-sidecar`. (Plain `python3 -m venv` + pip "
            "also works -- see dist/INSTALL-sidecar.md's manual fallback -- "
            "but this command specifically automates the uv path.)"
        )
        return False

    venv_python = _venv_python(target_dir)
    if venv_python.is_file() and not force:
        print(f"  venv already exists, skipping: {target_dir / '.venv'}")
        return True

    try:
        proc = _run(
            [uv, "venv", "--python", python_version, str(target_dir / ".venv")],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"  ERROR: failed to run uv: {exc}")
        return False
    if proc.returncode != 0:
        print(f"  ERROR: uv venv failed:\n{proc.stderr.strip()}")
        return False
    print(f"  created {target_dir / '.venv'}")
    return True


def _sam2_importable(venv_python: Path) -> bool:
    if not venv_python.is_file():
        return False
    proc = subprocess.run(
        [str(venv_python), "-c", "import sam2, torch"],  # noqa: S603
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _onnxruntime_importable(venv_python: Path) -> bool:
    if not venv_python.is_file():
        return False
    proc = subprocess.run(
        [str(venv_python), "-c", "import onnxruntime"],  # noqa: S603
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def step_install_deps(target_dir: Path, force: bool) -> bool:
    print("[3/7] SAM2 deps: torch (CPU) + sam2 (two-step, pinned)")
    uv = _uv_path()
    if not uv:
        print("  ERROR: `uv` not found on PATH (needed for `uv pip install`).")
        return False

    venv_python = _venv_python(target_dir)
    if not venv_python.is_file():
        print(f"  ERROR: venv python not found at {venv_python}; run step 2 first.")
        return False

    if not force and _sam2_importable(venv_python):
        print("  torch + sam2 already importable in the venv, skipping")
        return True

    torch_req = target_dir / "requirements-torch.txt"
    sam2_req = target_dir / "requirements.txt"
    if not torch_req.is_file() or not sam2_req.is_file():
        print(f"  ERROR: missing {torch_req} or {sam2_req}; run step 1 first.")
        return False

    try:
        proc = _run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(venv_python),
                "--index-url",
                TORCH_INDEX_URL,
                "-r",
                str(torch_req),
            ],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"  ERROR: failed to run uv pip install (torch step): {exc}")
        return False
    if proc.returncode != 0:
        print(
            "  ERROR: torch-CPU install failed (no network access to "
            f"{TORCH_INDEX_URL}? or pip/uv resolver error). stderr:\n"
            f"{proc.stderr.strip()[-2000:]}"
        )
        return False
    print("  torch (CPU) installed")

    try:
        proc = _run(
            [uv, "pip", "install", "--python", str(venv_python), "-r", str(sam2_req)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"  ERROR: failed to run uv pip install (sam2 step): {exc}")
        return False
    if proc.returncode != 0:
        print(
            "  ERROR: sam2/opencv/pillow install failed (no network access "
            "to PyPI? or pip/uv resolver error). stderr:\n"
            f"{proc.stderr.strip()[-2000:]}"
        )
        return False
    print("  sam2 + opencv + numpy + pillow installed")
    # requirements.txt also pins onnxruntime (shared MODNet dependency), so
    # this step usually leaves onnxruntime importable too -- step 4 still
    # verifies/installs it independently so an install that only ran step 4
    # (e.g. an older sidecar upgraded with --force limited to that step, or
    # a future --skip-sam2 mode) is not silently left without it.
    return True


def step_install_onnxruntime(target_dir: Path, force: bool) -> bool:
    """MODNet's only inference dependency (no torch needed). Independent of
    step 3's importability check so re-running install-sidecar on an OLDER
    sidecar venv (SAM2/torch already importable, predates the onnxruntime
    pin) still installs it instead of silently skipping."""
    print(f"[4/7] MODNet deps: {ONNXRUNTIME_SPEC} (CPU, no torch needed)")
    uv = _uv_path()
    if not uv:
        print("  ERROR: `uv` not found on PATH (needed for `uv pip install`).")
        return False

    venv_python = _venv_python(target_dir)
    if not venv_python.is_file():
        print(f"  ERROR: venv python not found at {venv_python}; run step 2 first.")
        return False

    if not force and _onnxruntime_importable(venv_python):
        print("  onnxruntime already importable in the venv, skipping")
        return True

    try:
        proc = _run(
            [uv, "pip", "install", "--python", str(venv_python), ONNXRUNTIME_SPEC],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"  ERROR: failed to run uv pip install (onnxruntime step): {exc}")
        return False
    if proc.returncode != 0:
        print(
            "  ERROR: onnxruntime install failed (no network access to "
            "PyPI? or pip/uv resolver error). stderr:\n"
            f"{proc.stderr.strip()[-2000:]}"
        )
        return False
    print("  onnxruntime installed")
    return True


def step_download_checkpoint(target_dir: Path, force: bool) -> bool:
    print("[5/7] SAM2.1 Hiera-small checkpoint (~176MB)")
    ckpt = _checkpoint_path(target_dir)
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    if ckpt.is_file() and ckpt.stat().st_size >= MIN_CHECKPOINT_BYTES and not force:
        print(f"  already downloaded, skipping: {ckpt} ({ckpt.stat().st_size} bytes)")
        return True

    print(f"  downloading {CHECKPOINT_URL}")
    tmp = ckpt.with_suffix(ckpt.suffix + ".part")
    try:
        with urllib.request.urlopen(CHECKPOINT_URL, timeout=30) as resp:  # noqa: S310
            with tmp.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        print(
            f"  ERROR: checkpoint download failed ({exc}). No network access? "
            "Re-run `darktable-mcp install-sidecar` once you have connectivity "
            "-- steps 1-4 will be skipped (already done) and only this "
            "download will be retried. Or download it manually to "
            f"{ckpt} (see dist/INSTALL-sidecar.md step 5)."
        )
        return False

    size = tmp.stat().st_size
    if size < MIN_CHECKPOINT_BYTES:
        tmp.unlink(missing_ok=True)
        print(
            f"  ERROR: downloaded file is only {size} bytes, expected ~149MB "
            "-- looks truncated/incomplete (partial network failure?). "
            "Removed the partial file; re-run to retry."
        )
        return False
    tmp.replace(ckpt)
    print(f"  wrote {ckpt} ({size} bytes)")
    return True


def step_download_modnet_checkpoint(target_dir: Path, force: bool) -> bool:
    print("[6/7] MODNet ONNX checkpoint (~24.7MB)")
    ckpt = _modnet_checkpoint_path(target_dir)
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    if ckpt.is_file() and ckpt.stat().st_size >= MIN_MODNET_CHECKPOINT_BYTES and not force:
        print(f"  already downloaded, skipping: {ckpt} ({ckpt.stat().st_size} bytes)")
        return True

    print(f"  downloading {MODNET_CHECKPOINT_URL}")
    tmp = ckpt.with_suffix(ckpt.suffix + ".part")
    try:
        with urllib.request.urlopen(MODNET_CHECKPOINT_URL, timeout=30) as resp:  # noqa: S310
            with tmp.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        print(
            f"  ERROR: MODNet checkpoint download failed ({exc}). No network "
            "access? Re-run `darktable-mcp install-sidecar` once you have "
            "connectivity -- steps 1-5 will be skipped (already done) and "
            "only this download will be retried. Or download it manually to "
            f"{ckpt} (see dist/INSTALL-sidecar.md step 6 / sidecar/README.md "
            "'Enabling real MODNet')."
        )
        return False

    size = tmp.stat().st_size
    if size < MIN_MODNET_CHECKPOINT_BYTES:
        tmp.unlink(missing_ok=True)
        print(
            f"  ERROR: downloaded file is only {size} bytes, expected ~24.7MB "
            "-- looks truncated/incomplete (partial network failure?). "
            "Removed the partial file; re-run to retry."
        )
        return False
    tmp.replace(ckpt)
    print(f"  wrote {ckpt} ({size} bytes)")
    return True


def step_print_env(
    target_dir: Path,
    home: Path,
    ok_so_far: bool,
    skipped_sam2_checkpoint: bool = False,
    skipped_modnet_checkpoint: bool = False,
) -> None:
    print("[7/7] env vars (mask_object SAM2 + mask_raster MODNet)")
    venv_python = _venv_python(target_dir)
    segment_script = _segment_script_path(target_dir)
    matte_script = _matte_script_path(target_dir)
    modnet_ckpt = _modnet_checkpoint_path(target_dir)
    lines = [
        "# darktable-mcp sidecar -- generated by `darktable-mcp install-sidecar`",
        "# mask_object (SAM2):",
        f"export DARKTABLE_MCP_SIDECAR_PYTHON={venv_python}",
        f"export DARKTABLE_MCP_SIDECAR_SEGMENT={segment_script}",
        "# mask_raster (MODNet):",
        f"export DARKTABLE_MCP_SIDECAR_MATTE={matte_script}",
        f"export DARKTABLE_MCP_MODNET_CHECKPOINT={modnet_ckpt}",
    ]
    config = _config_path(home)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    if ok_so_far:
        print(
            "sidecar ready (mask_object SAM2 + mask_raster MODNet). Set these "
            "env vars wherever darktable-mcp is launched from"
        )
    elif skipped_sam2_checkpoint or skipped_modnet_checkpoint:
        skipped_bits = []
        if skipped_sam2_checkpoint:
            skipped_bits.append(
                "SAM2 checkpoint (--skip-checkpoint) -- mask_object "
                "will keep using the GrabCut fallback"
            )
        if skipped_modnet_checkpoint:
            skipped_bits.append(
                "MODNet checkpoint (--skip-modnet-checkpoint) -- "
                "mask_raster will NOT work until it's downloaded "
                "(no fallback exists for it)"
            )
        print("sidecar venv ready, but download(s) SKIPPED: " + "; ".join(skipped_bits) + ".")
    else:
        print(
            "sidecar setup INCOMPLETE (see ERROR lines above) -- these env vars "
            "will only work once every step above has succeeded. Re-run "
            "`darktable-mcp install-sidecar` after fixing the issue (e.g. "
            "network access, installing `uv`) -- already-completed steps are "
            "skipped automatically."
        )
    print(f"(also written to {config} -- `source {config}` works too):")
    print()
    for line in lines:
        print(line)
    print()
    print(
        'Or add an "env" block to your Claude Desktop/Code MCP server '
        "registration:\n"
        '  "env": {\n'
        f'    "DARKTABLE_MCP_SIDECAR_PYTHON": "{venv_python}",\n'
        f'    "DARKTABLE_MCP_SIDECAR_SEGMENT": "{segment_script}",\n'
        f'    "DARKTABLE_MCP_SIDECAR_MATTE": "{matte_script}",\n'
        f'    "DARKTABLE_MCP_MODNET_CHECKPOINT": "{modnet_ckpt}"\n'
        "  }"
    )


def install_sidecar_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="darktable-mcp install-sidecar",
        description=(
            "One-command setup for BOTH optional darktable-mcp sidecars, "
            "sharing a single venv: SAM2 (precise mask_object masks -- "
            "without this, mask_object still works out of the box via a "
            "bundled rough GrabCut fallback) and MODNet (mask_raster soft "
            "matte masks for hair/fine detail -- mask_raster has NO "
            "fallback, so this step is required for it, not just a quality "
            "upgrade)."
        ),
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=None,
        help="where to build the sidecar (default: ~/.local/share/darktable-mcp/sidecar)",
    )
    parser.add_argument(
        "--python",
        dest="python_version",
        default="3.12",
        help="Python version for the sidecar venv (default: 3.12)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="redo every step even if its output already looks present/working",
    )
    parser.add_argument(
        "--skip-checkpoint",
        action="store_true",
        help=(
            "skip the ~149MB SAM2 checkpoint download (e.g. to verify the "
            "rest of the pipeline quickly, or if you'll copy the checkpoint in "
            "by hand); mask_object keeps working via the GrabCut fallback"
        ),
    )
    parser.add_argument(
        "--skip-modnet-checkpoint",
        action="store_true",
        help=(
            "skip the ~24.7MB MODNet checkpoint download; mask_raster will "
            "NOT work until it is downloaded (it has no fallback tier)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the steps and target paths without doing anything",
    )
    args = parser.parse_args(argv)

    home = Path.home()
    target_dir = args.target_dir or _default_target_dir(home)

    print(f"darktable-mcp install-sidecar: target={target_dir}")
    if args.dry_run:
        print("(dry run -- no changes will be made)")
        print(f"  [1/7] would copy segment.py + matte.py + requirements*.txt into {target_dir}")
        print(f"  [2/7] would run: uv venv --python {args.python_version} {target_dir / '.venv'}")
        print(
            "  [3/7] SAM2: would run: uv pip install --python "
            f"{_venv_python(target_dir)} --index-url {TORCH_INDEX_URL} "
            "-r requirements-torch.txt"
        )
        print(
            f"        would run: uv pip install --python {_venv_python(target_dir)} "
            "-r requirements.txt"
        )
        print(
            "  [4/7] MODNet: would run: uv pip install --python "
            f"{_venv_python(target_dir)} {ONNXRUNTIME_SPEC}"
        )
        if args.skip_checkpoint:
            print("  [5/7] SAM2 checkpoint: skipped (--skip-checkpoint)")
        else:
            print(f"  [5/7] would download {CHECKPOINT_URL} -> {_checkpoint_path(target_dir)}")
        if args.skip_modnet_checkpoint:
            print("  [6/7] MODNet checkpoint: skipped (--skip-modnet-checkpoint)")
        else:
            print(
                f"  [6/7] would download {MODNET_CHECKPOINT_URL} -> "
                f"{_modnet_checkpoint_path(target_dir)}"
            )
        print(f"  [7/7] would print/write env vars (SAM2 + MODNet) to {_config_path(home)}")
        return 0

    ok = True
    ok = step_copy_source(target_dir, args.force) and ok
    if ok:
        ok = step_create_venv(target_dir, args.force, args.python_version) and ok
    if ok:
        ok = step_install_deps(target_dir, args.force) and ok
    if ok:
        ok = step_install_onnxruntime(target_dir, args.force) and ok

    if args.skip_checkpoint:
        print("[5/7] SAM2.1 Hiera-small checkpoint: SKIPPED (--skip-checkpoint)")
        sam2_ckpt_ok = False
    else:
        sam2_ckpt_ok = step_download_checkpoint(target_dir, args.force)
        ok = sam2_ckpt_ok and ok

    if args.skip_modnet_checkpoint:
        print("[6/7] MODNet ONNX checkpoint: SKIPPED (--skip-modnet-checkpoint)")
        modnet_ckpt_ok = False
    else:
        modnet_ckpt_ok = step_download_modnet_checkpoint(target_dir, args.force)
        ok = modnet_ckpt_ok and ok

    all_downloads_ok = (sam2_ckpt_ok or args.skip_checkpoint) and (
        modnet_ckpt_ok or args.skip_modnet_checkpoint
    )
    step_print_env(
        target_dir,
        home,
        ok_so_far=ok and not args.skip_checkpoint and not args.skip_modnet_checkpoint,
        skipped_sam2_checkpoint=args.skip_checkpoint and all_downloads_ok,
        skipped_modnet_checkpoint=args.skip_modnet_checkpoint and all_downloads_ok,
    )

    if args.skip_checkpoint and all_downloads_ok:
        print(
            "\nNote: --skip-checkpoint was passed, so mask_object's SAM2 path "
            "is not ready -- mask_object will keep using the GrabCut fallback "
            "until you download it (re-run without --skip-checkpoint, or "
            f"place it manually at {_checkpoint_path(target_dir)})."
        )
    if args.skip_modnet_checkpoint and all_downloads_ok:
        print(
            "\nNote: --skip-modnet-checkpoint was passed, so mask_raster's "
            "MODNet checkpoint is missing -- mask_raster will NOT work (no "
            "fallback exists) until you download it (re-run without "
            "--skip-modnet-checkpoint, or place it manually at "
            f"{_modnet_checkpoint_path(target_dir)})."
        )

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(install_sidecar_main())
