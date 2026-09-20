#!/usr/bin/env python3
"""T2.2 acceptance driver: add_path_mask -- a drawn PATH mask that RESTRICTS a
module's effect to a synthetic polygon region.

Exercises darktable.develop.add_path_mask() over the same spike bridge as
run_t1_*.py / run_f1.py (docker/run-dt-bridge-spike.sh + spike_methods.lua).
Does NOT touch the clean bridge.

Acceptance (all quoted in the final report):
  1. Open mire1.cr2 in darkroom. add_instance("exposure") -> instance 1.
     add_path_mask("exposure",1, <octagon ~filling the CENTER box
     (0.35,0.35)-(0.65,0.65)>, opacity=1.0). Then set_params("exposure",1,
     {exposure:2.0}) + enable the instance.
  2. Measure mean luma INSIDE the polygon (tight center region {0.45,0.45,0.1,0.1})
     vs OUTSIDE (corner region {0,0,0.15,0.15}), BEFORE and AFTER the masked
     edit. PROOF of localization: inside luma rose substantially while outside
     luma stayed put. Both quoted.
  3. Blend linkage: add_path_mask return has mask_id != 0 and mask_mode with the
     DEVELOP_MASK_MASK (1<<1) bit set. Quoted.
  4. Degenerate: 2 points -> {error}, no crash. Malformed point (missing y) ->
     {error}, no crash.
  5. No-hang: create several masks / rapid add_path_mask+preview cycles;
     container alive; log clean.

Usage: python3 darktable-mcp/spike/run_t2_2.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
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

DEVELOP_MASK_MASK = 1 << 1  # blend.h:97 -- drawn-mask bit

# An octagon ~filling the center box (0.35,0.35)-(0.65,0.65).
CENTER_OCTAGON = [
    {"x": 0.40, "y": 0.35},
    {"x": 0.60, "y": 0.35},
    {"x": 0.65, "y": 0.40},
    {"x": 0.65, "y": 0.60},
    {"x": 0.60, "y": 0.65},
    {"x": 0.40, "y": 0.65},
    {"x": 0.35, "y": 0.60},
    {"x": 0.35, "y": 0.40},
]
# tight region deep INSIDE the polygon, and a corner region well OUTSIDE it.
INSIDE_REGION = (0.45, 0.45, 0.10, 0.10)
OUTSIDE_REGION = (0.0, 0.0, 0.15, 0.15)


def _load_bridge_client_module():
    client_path = MCP_DIR / "darktable_mcp" / "bridge" / "client.py"
    spec = importlib.util.spec_from_file_location("darktable_mcp_bridge_client", client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge


def log(msg: str) -> None:
    print(f"[t2.2] {msg}", flush=True)


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
    """Mean Rec.601 luma of a PNG (0..255)."""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(png_path) as im:
            a = np.asarray(im.convert("L"), dtype="float64")
        return float(a.mean())
    except Exception as e:  # noqa: BLE001
        log(f"mean_luma failed for {png_path}: {e}")
        return None


def region_luma(bridge, run_dir, box, tag):
    """dev_preview on a normalized region -> mean luma of the returned PNG."""
    x, y, w, h = box
    resp = bridge.call(
        "dev_preview", {"max_w": 300, "max_h": 300, "x": x, "y": y, "w": w, "h": h}, timeout=20.0
    )
    p = resp.get("path") or resp.get("stale_preview")
    lum = mean_luma(host_path(run_dir, p)) if p else None
    log(f"  region[{tag}] {box} -> status={resp.get('status')} luma={lum}")
    return lum, resp


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

        # PRISTINE BASELINE luma, captured BEFORE any instance/mask/exposure edit
        # (only the image's stock modules). This is the reference both regions are
        # compared against, so the "outside unchanged" claim is not contaminated by
        # a duplicated instance transiently acting globally before it gets masked.
        log("--- pristine baseline luma (before add_instance) ---")
        base_in, _ = region_luma(bridge, run_dir, INSIDE_REGION, "inside")
        base_out, _ = region_luma(bridge, run_dir, OUTSIDE_REGION, "outside")
        results["baseline_inside_luma"] = base_in
        results["baseline_outside_luma"] = base_out

        # ============ CHECK 1: add a masked exposure instance ============
        log("=== CHECK 1: add_instance(exposure) ===")
        inst = bridge.call("dev_add_instance", {"op": "exposure"}, timeout=15.0)
        results["add_instance"] = inst
        log(f"add_instance(exposure) -> {json.dumps(inst)}")
        instance = int(inst.get("instance", 1)) if isinstance(inst, dict) and inst.get("ok") else 1

        # add_path_mask: octagon over the center box, opacity 1.0.
        log(f"=== add_path_mask(exposure,{instance}, center octagon, opacity=1.0) ===")
        apm = bridge.call(
            "dev_add_path_mask",
            {"op": "exposure", "instance": instance, "points": CENTER_OCTAGON, "opacity": 1.0},
            timeout=20.0,
        )
        results["add_path_mask"] = apm
        log(f"add_path_mask -> {json.dumps(apm)}")

        # localize the effect: exposure +2 EV on the masked instance, then enable.
        setp = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": instance, "fields": {"exposure": 2.0}},
            timeout=15.0,
        )
        results["set_params_exposure"] = setp
        log(f"set_params(exposure=2.0) -> {json.dumps(setp)}")
        en = bridge.call(
            "dev_enable_module",
            {"op": "exposure", "instance": instance, "enabled": True},
            timeout=15.0,
        )
        results["enable_exposure"] = en
        log(f"enable_module(exposure,{instance}) -> {json.dumps(en)}")

        # AFTER luma: inside should rise a lot, outside should stay ~put.
        log("--- after masked exposure luma ---")
        aft_in, _ = region_luma(bridge, run_dir, INSIDE_REGION, "inside")
        aft_out, _ = region_luma(bridge, run_dir, OUTSIDE_REGION, "outside")
        results["after_inside_luma"] = aft_in
        results["after_outside_luma"] = aft_out

        d_in = (aft_in - base_in) if (aft_in is not None and base_in is not None) else None
        d_out = (aft_out - base_out) if (aft_out is not None and base_out is not None) else None
        results["delta_inside_luma"] = d_in
        results["delta_outside_luma"] = d_out
        log(f"LUMA inside  base={base_in} after={aft_in} delta={d_in}")
        log(f"LUMA outside base={base_out} after={aft_out} delta={d_out}")

        alive_c1 = container_alive(run_dir)
        results["container_alive_after_c1"] = alive_c1

        # ============ CHECK 3: blend linkage ============
        mask_id = apm.get("mask_id") if isinstance(apm, dict) else None
        mask_mode = apm.get("mask_mode") if isinstance(apm, dict) else None
        results["mask_id"] = mask_id
        results["mask_mode"] = mask_mode
        results["mask_mode_has_MASK_bit"] = bool(
            mask_mode is not None and (mask_mode & DEVELOP_MASK_MASK)
        )
        log(
            f"blend linkage: mask_id={mask_id} mask_mode={mask_mode} "
            f"MASK_bit={results['mask_mode_has_MASK_bit']}"
        )

        # ============ CHECK 4: degenerate / malformed ============
        log("=== CHECK 4: degenerate (2 points) + malformed point ===")
        deg = bridge.call(
            "dev_add_path_mask",
            {
                "op": "exposure",
                "instance": instance,
                "points": [{"x": 0.4, "y": 0.4}, {"x": 0.6, "y": 0.6}],
                "opacity": 1.0,
            },
            timeout=15.0,
        )
        results["degenerate_2pts"] = deg
        log(f"add_path_mask(2 points) -> {json.dumps(deg)}")

        mal = bridge.call(
            "dev_add_path_mask",
            {
                "op": "exposure",
                "instance": instance,
                "points": [{"x": 0.4, "y": 0.4}, {"x": 0.6}, {"x": 0.5, "y": 0.6}],
                "opacity": 1.0,
            },
            timeout=15.0,
        )
        results["malformed_point"] = mal
        log(f"add_path_mask(malformed point) -> {json.dumps(mal)}")
        alive_c4 = container_alive(run_dir)
        results["container_alive_after_c4"] = alive_c4

        # ============ CHECK 5: no-hang, several masks + previews ============
        log("=== CHECK 5: 8 rapid add_path_mask + preview cycles ===")
        cyc = {"total": 8, "completed": 0, "results": [], "first_hang": None}
        for i in range(8):
            off = 0.02 * i
            poly = [
                {"x": 0.40 + off, "y": 0.35},
                {"x": 0.60, "y": 0.35 + off},
                {"x": 0.65, "y": 0.60},
                {"x": 0.40, "y": 0.65},
            ]
            try:
                r = bridge.call(
                    "dev_add_path_mask",
                    {"op": "exposure", "instance": instance, "points": poly, "opacity": 0.8},
                    timeout=15.0,
                )
                bridge.call("dev_preview", {"max_w": 200, "max_h": 200}, timeout=15.0)
                cyc["completed"] += 1
                cyc["results"].append(bool(isinstance(r, dict) and r.get("ok")))
            except Exception as e:  # noqa: BLE001
                cyc["first_hang"] = {"i": i, "error": str(e)}
                log(f"CYCLE HANG at {i}: {e}")
                break
        results["cycles"] = cyc
        log(f"cycles: {cyc['completed']}/8; oks={cyc['results']}; first_hang={cyc['first_hang']}")
        results["container_alive_after_cycles"] = container_alive(run_dir)

        logtext = log_path.read_text(errors="replace")
        deadlock_hits = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        results["deadlock_log_hits"] = deadlock_hits

        print("\n===== T2.2 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        # ---- evaluate ----
        # localization proof: inside rose substantially AND outside stayed put.
        inside_rose = d_in is not None and d_in > 15.0
        outside_put = d_out is not None and abs(d_out) < 5.0

        checks = {
            "1 add_instance ok": isinstance(inst, dict) and inst.get("ok") is True,
            "1 add_path_mask ok": isinstance(apm, dict) and apm.get("ok") is True,
            "1 set_params(exposure) ok": isinstance(setp, dict) and setp.get("ok") is True,
            "2 inside luma rose substantially (delta>15)": inside_rose,
            "2 outside luma stayed put (|delta|<5)": outside_put,
            "3 mask_id != 0": bool(mask_id),
            "3 mask_mode has DEVELOP_MASK_MASK bit": results["mask_mode_has_MASK_bit"],
            "4 degenerate 2-pt -> error (no crash)": (
                isinstance(deg, dict) and bool(deg.get("error"))
            ),
            "4 malformed point -> error (no crash)": (
                isinstance(mal, dict) and bool(mal.get("error"))
            ),
            "4 container alive after degenerate calls": alive_c4,
            "5 all 8 cycles completed no hang": cyc["completed"] == 8 and cyc["first_hang"] is None,
            "5 container alive after cycles": results["container_alive_after_cycles"],
            "no deadlock/critical/assertion in log": len(deadlock_hits) == 0,
        }
        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print(f"\nALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== T2.2 INFRA FAILURE: {e} =====", file=sys.stderr)
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
