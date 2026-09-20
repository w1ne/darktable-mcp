#!/usr/bin/env python3
"""F1-F3 acceptance driver: GUI widget refresh + region preview + get_viewport.

Exercises the three workstation-testing fixes in src/lua/develop.c over the same
spike bridge as run_t1_*.py (docker/run-dt-bridge-spike.sh + spike_methods.lua).
Does NOT touch the clean bridge.

Acceptance (all quoted in the final report):
  1. GUI refresh: enable grain, set_params(grain,{strength:90}); confirm
     get_params echoes 90 AND the module panel slider re-reads params -- probed
     via the action system (dt.gui.action read of iop/grain/strength) which
     reads the LIVE widget, so a stale widget would return the old position.
     No crash / no deadlock (container stays alive, log clean).
  2. Region preview: full-frame vs region {0,0,0.25,0.25} at the same max_w --
     the region PNG is a zoomed-in top-left crop. Both response JSONs quoted;
     dims + region echo compared.
  3. get_viewport() returns a sane {main, preview2} table in darkroom; error
     (not crash) in lighttable. Both quoted; preview2.active=false confirmed
     present (no 2nd window opened headless).
  4. No-hang: 20 rapid set_params + preview(region) + get_viewport cycles all
     return; log clean.

Usage: python3 darktable-mcp/spike/run_f1.py
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


def _load_bridge_client_module():
    client_path = MCP_DIR / "darktable_mcp" / "bridge" / "client.py"
    spec = importlib.util.spec_from_file_location("darktable_mcp_bridge_client", client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge


def log(msg: str) -> None:
    print(f"[f1] {msg}", flush=True)


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


def image_dims(path: str):
    try:
        from PIL import Image

        with Image.open(path) as img:
            return list(img.size)
    except Exception as e:  # noqa: BLE001
        log(f"image_dims failed for {path}: {e}")
        return None


def crop_corr(full_path: str, region_path: str, box):
    """Downscale the top-left quarter of the full image to the region image size
    and return normalized cross-correlation of luma -- a spot check that the
    region really is the top-left crop. box=(x,y,w,h) normalized."""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(full_path) as f, Image.open(region_path) as r:
            fw, fh = f.size
            x, y, w, h = box
            crop = f.crop((int(x * fw), int(y * fh), int((x + w) * fw), int((y + h) * fh)))
            crop = crop.convert("L").resize(r.size)
            a = np.asarray(crop, dtype="float64").ravel()
            b = np.asarray(r.convert("L"), dtype="float64").ravel()
            a -= a.mean()
            b -= b.mean()
            denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
            return float((a @ b) / denom)
    except Exception as e:  # noqa: BLE001
        log(f"crop_corr skipped: {e}")
        return None


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

        # ================= CHECK 1: GUI widget refresh =================
        log("=== CHECK 1: enable grain + set_params(strength=90), verify widget refresh ===")
        en = bridge.call(
            "dev_enable_module", {"op": "grain", "instance": 0, "enabled": True}, timeout=15.0
        )
        results["enable_grain"] = en
        log(f"enable_module(grain) -> {json.dumps(en)}")

        # widget value BEFORE (via action read of the live slider)
        try:
            wid_before = bridge.call("nudge", {"action_path": "iop/grain/strength"}, timeout=10.0)
        except Exception as e:  # noqa: BLE001
            wid_before = {"error": str(e)}
        log(f"slider read BEFORE set -> {json.dumps(wid_before)}")

        setp = bridge.call(
            "dev_set_params",
            {"op": "grain", "instance": 0, "fields": {"strength": 90}},
            timeout=15.0,
        )
        results["set_params_grain"] = setp
        log(f"set_params(grain,strength=90) -> {json.dumps(setp)}")

        gp = bridge.call("dev_get_params", {"op": "grain", "instance": 0}, timeout=15.0)
        strength_echo = None
        try:
            strength_echo = gp["fields"]["strength"]["value"]
        except Exception:  # noqa: BLE001
            pass
        results["get_params_strength"] = gp.get("fields", {}).get("strength")
        log(f"get_params strength -> {json.dumps(results['get_params_strength'])}")

        try:
            wid_after = bridge.call("nudge", {"action_path": "iop/grain/strength"}, timeout=10.0)
        except Exception as e:  # noqa: BLE001
            wid_after = {"error": str(e)}
        results["slider_before"] = wid_before
        results["slider_after"] = wid_after
        log(f"slider read AFTER set -> {json.dumps(wid_after)}")

        alive_c1 = container_alive(run_dir)
        results["container_alive_after_c1"] = alive_c1

        # ================= CHECK 2: region preview =================
        log("=== CHECK 2: full-frame vs region {0,0,0.25,0.25} at max_w=400 ===")
        full = bridge.call("dev_preview", {"max_w": 400, "max_h": 400}, timeout=20.0)
        results["preview_full"] = full
        log(f"preview FULL -> {json.dumps(full)}")
        region = bridge.call(
            "dev_preview",
            {"max_w": 400, "max_h": 400, "x": 0.0, "y": 0.0, "w": 0.25, "h": 0.25},
            timeout=20.0,
        )
        results["preview_region"] = region
        log(f"preview REGION -> {json.dumps(region)}")

        full_p = full.get("path") or full.get("stale_preview")
        reg_p = region.get("path") or region.get("stale_preview")
        results["full_png_dims"] = image_dims(host_path(run_dir, full_p)) if full_p else None
        results["region_png_dims"] = image_dims(host_path(run_dir, reg_p)) if reg_p else None
        corr = None
        if full_p and reg_p:
            corr = crop_corr(
                host_path(run_dir, full_p), host_path(run_dir, reg_p), (0.0, 0.0, 0.25, 0.25)
            )
        results["region_topleft_correlation"] = corr
        log(
            f"full png dims={results['full_png_dims']} region png dims={results['region_png_dims']}"
            f" topleft-crop corr={corr}"
        )

        # degenerate region -> graceful error
        bad = bridge.call(
            "dev_preview",
            {"max_w": 200, "max_h": 200, "x": 0.5, "y": 0.5, "w": 0.0, "h": 0.2},
            timeout=20.0,
        )
        results["preview_degenerate_region"] = bad
        log(f"preview degenerate region (w=0) -> {json.dumps(bad)}")

        # ================= CHECK 3: get_viewport =================
        log("=== CHECK 3: get_viewport() in darkroom ===")
        vp = bridge.call("dev_get_viewport", {}, timeout=15.0)
        results["viewport_darkroom"] = vp
        log(f"get_viewport (darkroom) -> {json.dumps(vp)}")

        # ================= CHECK 4: no-hang 20 cycles =================
        log("=== CHECK 4: 20 rapid set_params + region-preview + get_viewport cycles ===")
        cyc = {"total": 20, "completed": 0, "statuses": [], "first_hang": None}
        for i in range(20):
            val = float(i * 5 % 100)
            try:
                bridge.call(
                    "dev_set_params",
                    {"op": "grain", "instance": 0, "fields": {"strength": val}},
                    timeout=10.0,
                )
                pr = bridge.call(
                    "dev_preview",
                    {"max_w": 300, "max_h": 300, "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3},
                    timeout=10.0,
                )
                bridge.call("dev_get_viewport", {}, timeout=10.0)
                cyc["completed"] += 1
                cyc["statuses"].append(pr.get("status") or ("error" if pr.get("error") else "?"))
            except Exception as e:  # noqa: BLE001
                cyc["first_hang"] = {"i": i, "value": val, "error": str(e)}
                log(f"CYCLE HANG at {i} (value={val}): {e}")
                break
        results["cycles"] = cyc
        log(
            f"cycles: {cyc['completed']}/20; statuses={cyc['statuses']};"
            f" first_hang={cyc['first_hang']}"
        )
        results["container_alive_after_cycles"] = container_alive(run_dir)

        # ================= CHECK 3b: get_viewport in lighttable -> error =========
        log("=== CHECK 3b: leave darkroom, get_viewport() must return {error} ===")
        bridge.call("leave_darkroom", {}, timeout=20.0)
        time.sleep(0.5)
        vp_lt = bridge.call("dev_get_viewport", {}, timeout=15.0)
        results["viewport_lighttable"] = vp_lt
        log(f"get_viewport (lighttable) -> {json.dumps(vp_lt)}")
        results["container_alive_final"] = container_alive(run_dir)

        logtext = log_path.read_text(errors="replace")
        deadlock_hits = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        results["deadlock_log_hits"] = deadlock_hits

        print("\n===== F1-F3 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        # ---- evaluate ----
        def slider_val(d):
            if isinstance(d, dict) and "value" in d and not d.get("is_nan", False):
                return d.get("value")
            return None

        sb, sa = slider_val(wid_before), slider_val(wid_after)
        widget_changed = sb is not None and sa is not None and abs((sa or 0) - (sb or 0)) > 1e-6

        full_dims = results["full_png_dims"]
        reg_dims = results["region_png_dims"]
        region_zoomed = False
        if full_dims and reg_dims and region.get("region"):
            # both capped at 400; region is a 0.25x0.25 crop so its backbuf px area
            # is far smaller -> region png should be <= full and content = top-left.
            region_zoomed = corr is not None and corr > 0.5

        checks = {
            "1 enable_module ok": isinstance(en, dict) and en.get("ok") is True,
            "1 set_params ok": isinstance(setp, dict) and setp.get("ok") is True,
            "1 get_params echoes strength=90": strength_echo == 90,
            "1 slider widget value changed after set (widget refreshed)": widget_changed,
            "1 container alive after c1 (no crash/deadlock)": alive_c1,
            "2 full preview returned a frame": bool(full_p),
            "2 region preview returned a frame": bool(reg_p),
            "2 region echoes region block": bool(region.get("region")),
            "2 region content = top-left crop (corr>0.5)": region_zoomed,
            "2 degenerate region -> error (no crash)": (
                isinstance(bad, dict) and bool(bad.get("error"))
            ),
            "3 viewport main present in darkroom": (
                isinstance(vp, dict) and isinstance(vp.get("main"), dict)
            ),
            "3 viewport preview2.active field present": (
                isinstance(vp.get("preview2"), dict) and "active" in vp.get("preview2", {})
            ),
            "3 viewport main has zoom+scale": (
                isinstance(vp.get("main"), dict) and "zoom" in vp["main"] and "scale" in vp["main"]
            ),
            "3b get_viewport error in lighttable": (
                isinstance(vp_lt, dict) and bool(vp_lt.get("error"))
            ),
            "4 all 20 cycles completed no hang": (
                cyc["completed"] == 20 and cyc["first_hang"] is None
            ),
            "4 container alive after cycles": results["container_alive_after_cycles"],
            "no deadlock/critical/assertion in log": len(deadlock_hits) == 0,
        }
        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print(f"\nALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== F1-F3 INFRA FAILURE: {e} =====", file=sys.stderr)
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
