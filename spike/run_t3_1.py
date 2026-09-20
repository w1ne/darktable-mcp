#!/usr/bin/env python3
"""T3.1 raster-producer spike (go/no-go): inject an EXTERNAL float alpha bitmap
as a raster mask that a DOWNSTREAM module blends through, giving SOFT per-pixel
edges a drawn path mask cannot.

Mechanism proven end-to-end over the same spike bridge as run_t2_2.py:
  * upstream producer = the stock iop/rasterfile.c module (IOP_FLAGS_WRITE_RASTER):
    reads a float PFM/PNG off disk, resamples to roi, emits it under
    BLEND_RASTER_ID via dt_iop_piece_set_raster.
  * we hand it a SYNTHETIC horizontal 0->1 gradient PFM as the alpha.
  * downstream consumer = a new exposure instance at +3 EV; its blend is wired to
    the rasterfile raster mask via the NEW dev_set_raster_source binding.
  * PASS = the exposure lift is gated by the alpha with SOFT edges: mean luma
    DELTA grows monotonically left->right following the alpha ramp (NOT a hard
    on/off, NOT flat).

Usage: python3 darktable-mcp/spike/run_t3_1.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
MCP_DIR = SPIKE_DIR.parent
ROOT_DIR = MCP_DIR.parent

RUN_SCRIPT = ROOT_DIR / "docker" / "run-dt-bridge-spike.sh"
FIXTURE_IMAGE = ROOT_DIR / "src-dt" / "src" / "tests" / "integration" / "images" / "mire1.cr2"
READY_LINE = "darktable-mcp bridge: ready"
DEADLOCK_MARKERS = ("deadlock", "assertion", "assert failed", "**: assertion", "critical")

# vertical strips (x, y, w, h) normalized, spanning the alpha ramp left->right.
STRIPS = [
    (0.05, 0.2, 0.08, 0.6),
    (0.28, 0.2, 0.08, 0.6),
    (0.50, 0.2, 0.08, 0.6),
    (0.72, 0.2, 0.08, 0.6),
    (0.90, 0.2, 0.08, 0.6),
]

PFM_W, PFM_H = 256, 256


def _load_bridge_client_module():
    client_path = MCP_DIR / "darktable_mcp" / "bridge" / "client.py"
    spec = importlib.util.spec_from_file_location("darktable_mcp_bridge_client", client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge


def log(msg: str) -> None:
    print(f"[t3.1] {msg}", flush=True)


def write_gradient_pfm(path: Path, w: int, h: int) -> None:
    """3-channel color PFM, horizontal 0->1 ramp (value = column/(w-1)),
    little-endian (scale -1.0), rows bottom-to-top (irrelevant for a
    horizontal ramp)."""
    with open(path, "wb") as f:
        f.write(b"PF\n")
        f.write(f"{w} {h}\n".encode())
        f.write(b"-1.0\n")
        for _row in range(h):
            row = bytearray()
            for col in range(w):
                v = col / (w - 1)
                row += struct.pack("<fff", v, v, v)
            f.write(row)


def wait_for_ready(log_path: Path, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if READY_LINE in line:
                    return line.strip()
            if "darktable exit code:" in text:
                raise RuntimeError(f"darktable exited before ready; tail:\n{text[-4000:]}")
        time.sleep(0.5)
    tail = log_path.read_text(errors="replace")[-4000:] if log_path.exists() else "(no log)"
    raise TimeoutError(f"'{READY_LINE}' not seen within {timeout}s; tail:\n{tail}")


def start_bridge() -> str:
    result = subprocess.run([str(RUN_SCRIPT), "start"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"start failed rc={result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip().splitlines()[-1].strip()


def stop_bridge(run_dir: str) -> None:
    subprocess.run([str(RUN_SCRIPT), "stop", run_dir], capture_output=True, text=True, timeout=30)


def container_alive(run_dir: str) -> bool:
    cid_path = Path(run_dir) / "container_id"
    if not cid_path.exists():
        return False
    cid = cid_path.read_text().strip()
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", cid],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def host_path(run_dir: str, container_path: str) -> str:
    if container_path and container_path.startswith("/run"):
        return run_dir + container_path[len("/run") :]
    return container_path


def mean_luma(png_path: str):
    try:
        import numpy as np
        from PIL import Image

        with Image.open(png_path) as im:
            a = np.asarray(im.convert("L"), dtype="float64")
        return float(a.mean())
    except Exception as e:  # noqa: BLE001
        log(f"mean_luma failed for {png_path}: {e}")
        return None


def strip_lumas(bridge, run_dir, tag):
    out = []
    for x, y, w, h in STRIPS:
        resp = bridge.call(
            "dev_preview",
            {"max_w": 200, "max_h": 400, "x": x, "y": y, "w": w, "h": h},
            timeout=20.0,
        )
        p = resp.get("path") or resp.get("stale_preview")
        lum = mean_luma(host_path(run_dir, p)) if p else None
        out.append(lum)
    log(f"  strips[{tag}] = {[f'{v:.2f}' if v is not None else None for v in out]}")
    return out


def main() -> int:  # noqa: C901
    if not FIXTURE_IMAGE.is_file():
        log(f"FAIL: fixture not found at {FIXTURE_IMAGE}")
        return 1

    results: dict = {}
    run_dir = start_bridge()
    log(f"RUN_DIR = {run_dir}")
    log_path = Path(run_dir) / "dt.log"
    try:
        log(f"bridge ready: {wait_for_ready(log_path)!r}")

        # synthetic gradient alpha -> a dir mounted into the container at /run.
        pfm_host = Path(run_dir) / "import-src" / "gradient.pfm"
        write_gradient_pfm(pfm_host, PFM_W, PFM_H)
        log(f"wrote gradient PFM {PFM_W}x{PFM_H} -> {pfm_host}")
        pfm_container_dir = "/run/import-src"
        pfm_file = "gradient.pfm"

        shutil.copy2(FIXTURE_IMAGE, Path(run_dir) / "import-src" / FIXTURE_IMAGE.name)
        cache_dir = Path(run_dir) / "cache-mcp" / "darktable-mcp"
        plugin_path = Path(run_dir) / "config" / "lua" / "darktable_mcp.lua"
        bridge = Bridge(cache_dir=cache_dir, plugin_path=plugin_path)

        imp = bridge.call(
            "import_batch", {"source_path": "/run/import-src", "recursive": True}, timeout=30.0
        )
        log(f"import_batch: {json.dumps(imp)}")
        photos = bridge.call("view_photos", {"limit": 50}, timeout=15.0)
        mire = next((p for p in photos if "mire1" in (p.get("filename") or "").lower()), None)
        if not mire:
            raise RuntimeError(f"fixture not in view_photos: {photos}")
        image_id = int(mire["id"])
        log(f"target image_id = {image_id}")

        opened = bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
        log(f"open_darkroom -> {json.dumps(opened)}")
        if opened.get("view") != "darkroom":
            raise RuntimeError(f"failed to enter darkroom: {opened}")

        # ---- pristine baseline strips (before any raster wiring) ----
        log("--- pristine baseline strip lumas ---")
        base = strip_lumas(bridge, run_dir, "baseline")
        results["baseline_strips"] = base

        # ---- producer: point rasterfile at the gradient + enable it ----
        log("=== rasterfile: set path/file params to the gradient PFM ===")
        rf = bridge.call(
            "dev_set_params",
            {
                "op": "rasterfile",
                "instance": 0,
                "fields": {"path": pfm_container_dir, "file": pfm_file},
            },
            timeout=15.0,
        )
        results["rasterfile_set_params"] = rf
        log(f"rasterfile set_params -> {json.dumps(rf)}")
        rfen = bridge.call(
            "dev_enable_module", {"op": "rasterfile", "instance": 0, "enabled": True}, timeout=15.0
        )
        results["rasterfile_enable"] = rfen
        log(f"rasterfile enable -> {json.dumps(rfen)}")

        # ---- consumer: new exposure instance at +3 EV ----
        log("=== add_instance(exposure) + exposure=1.0 ===")
        inst = bridge.call("dev_add_instance", {"op": "exposure"}, timeout=15.0)
        results["add_instance"] = inst
        instance = int(inst.get("instance", 1)) if isinstance(inst, dict) and inst.get("ok") else 1
        log(f"add_instance -> {json.dumps(inst)} (instance={instance})")
        setp = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": instance, "fields": {"exposure": 1.0}},
            timeout=15.0,
        )
        results["set_exposure"] = setp
        log(f"set_params(exposure=3.0) -> {json.dumps(setp)}")

        # ---- wire the raster: exposure consumes rasterfile's mask ----
        log("=== set_raster_source(exposure <- rasterfile) ===")
        wire = bridge.call(
            "dev_set_raster_source",
            {
                "consumer_op": "exposure",
                "consumer_instance": instance,
                "source_op": "rasterfile",
                "source_instance": 0,
                "opacity": 100.0,
            },
            timeout=15.0,
        )
        results["set_raster_source"] = wire
        log(f"set_raster_source -> {json.dumps(wire)}")

        en = bridge.call(
            "dev_enable_module",
            {"op": "exposure", "instance": instance, "enabled": True},
            timeout=15.0,
        )
        results["enable_exposure"] = en
        log(f"enable exposure instance -> {json.dumps(en)}")

        # ---- after strips ----
        log("--- after (masked exposure) strip lumas ---")
        aft = strip_lumas(bridge, run_dir, "after")
        results["after_strips"] = aft

        deltas = [None if (a is None or b is None) else (a - b) for a, b in zip(aft, base)]
        results["deltas"] = deltas
        log(f"DELTAS left->right = {[f'{d:.2f}' if d is not None else None for d in deltas]}")

        results["container_alive"] = container_alive(run_dir)
        logtext = log_path.read_text(errors="replace")
        results["deadlock_log_hits"] = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        # surface any raster-mask pipe diagnostics
        results["raster_log_lines"] = [
            ln.strip() for ln in logtext.splitlines() if "raster" in ln.lower()
        ][-25:]

        print("\n===== T3.1 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        # ---- evaluate soft-edge ramp ----
        # Compare the interior strips (0..3): the far-right strip (alpha~0.9) can
        # clip to white at high exposure and is excluded from the monotonic test.
        valid = all(d is not None for d in deltas)
        interior = deltas[:4] if valid else []
        monotonic = valid and all(
            interior[i] <= interior[i + 1] + 1.0 for i in range(len(interior) - 1)
        )
        strict_rise = valid and (interior[-1] - interior[0]) > 8.0
        left_dim = valid and deltas[0] < deltas[2] - 8.0  # left far weaker than middle
        right_strong = valid and deltas[3] > 12.0  # near-alpha=1 strongly affected
        # NOT a hard on/off: middle strips lie strictly between left edge and peak.
        peak = max(deltas) if valid else 0
        soft_middle = valid and (
            deltas[0] + 4.0 < deltas[1] < peak - 2.0 and deltas[1] < deltas[2] < peak + 1.0
        )

        checks = {
            "rasterfile set_params ok": isinstance(rf, dict) and rf.get("ok") is True,
            "rasterfile enabled ok": isinstance(rfen, dict) and rfen.get("ok") is True,
            "add_instance(exposure) ok": isinstance(inst, dict) and inst.get("ok") is True,
            "set_raster_source ok": isinstance(wire, dict) and wire.get("ok") is True,
            "set_raster_source has RASTER bit (1<<3)": (
                isinstance(wire, dict) and bool((wire.get("mask_mode") or 0) & (1 << 3))
            ),
            "deltas valid (all strips rendered)": valid,
            "interior ramp rises left->right (>8)": strict_rise,
            "left edge dimmer than middle": left_dim,
            "near-full-alpha strip strongly affected (>12)": right_strong,
            "interior monotonic non-decreasing": monotonic,
            "SOFT (middle strictly between edges, not on/off)": soft_middle,
            "container alive": results["container_alive"],
            "no deadlock/critical/assertion in log": len(results["deadlock_log_hits"]) == 0,
        }
        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print(f"\nALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== T3.1 INFRA FAILURE: {e} =====", file=sys.stderr)
        if log_path.exists():
            print(
                "----- dt.log tail -----\n" + log_path.read_text(errors="replace")[-8000:],
                file=sys.stderr,
            )
        print(json.dumps(results, indent=2, default=str), file=sys.stderr)
        return 1
    finally:
        log("stopping bridge container")
        stop_bridge(run_dir)


if __name__ == "__main__":
    sys.exit(main())
