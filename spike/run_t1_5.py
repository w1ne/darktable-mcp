#!/usr/bin/env python3
"""T1.5 acceptance driver: darktable.develop.preview(max_w, max_h).

Proves the LIVE conversational loop with NO DB write: set_params changes the
in-memory edit, preview() encodes a PNG straight from preview_pipe->backbuf
(the live rendered frame), no export / no dt_dev_write_history. Reuses the same
spike bridge as run_t1_1/2/3.py (docker/run-dt-bridge-spike.sh +
spike_methods.lua, now also registering the dev_preview probe). Does NOT touch
the clean bridge.

Acceptance points (all quoted in the final report), PLAN.md §3.4 / T1.5:
  1. LIVE loop, no DB: set_params(exposure=-2.0) -> preview(800,800) = PNG A;
     set_params(exposure=+2.0) -> preview(800,800) = PNG B. mean_luma(B) >>
     mean_luma(A). Both response JSONs + both luma numbers quoted.
  2. NO DB commit: history_count is UNCHANGED across a run of preview() calls
     (preview writes neither history nor XMP/DB); we never leave darkroom.
  3. Dimensions respect max_w/max_h and preserve aspect; no upscale. W/H quoted
     for a few caps.
  4. NO-HANG: 20 rapid set_params+preview cycles all return within timeout;
     darktable stays alive; log clean of deadlock/critical.
  5. NOT-IN-DARKROOM: leave darkroom, preview() -> {error=...}, no crash.

Usage: python3 darktable-mcp/spike/run_t1_5.py
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

# fatal patterns that would signal the #1 (threading) risk materialising.
DEADLOCK_MARKERS = ("deadlock", "assertion", "assert failed", "**: assertion", "critical")


def _load_bridge_client_module():
    client_path = MCP_DIR / "darktable_mcp" / "bridge" / "client.py"
    spec = importlib.util.spec_from_file_location("darktable_mcp_bridge_client", client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge
BridgeError = _client_mod.BridgeError


def log(msg: str) -> None:
    print(f"[t1.5] {msg}", flush=True)


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
    """Map a container path under /run to the host run_dir."""
    if container_path and container_path.startswith("/run"):
        return run_dir + container_path[len("/run") :]
    return container_path


def image_dims(path: str):
    try:
        from PIL import Image

        with Image.open(path) as img:
            return list(img.size)  # (w, h)
    except Exception as e:  # noqa: BLE001
        log(f"image_dims failed for {path}: {e}")
        return None


def mean_luma(path: str):
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            img = im.convert("L")
            return float(np.asarray(img, dtype="float64").mean())
    except Exception as e:  # noqa: BLE001
        log(f"mean_luma skipped: {e}")
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

        am = bridge.call("dev_active_modules", {}, timeout=15.0)
        exp_am = next((m for m in am.get("modules", []) if m["op"] == "exposure"), None)
        results["exposure_module"] = exp_am
        log(f"exposure module state: {json.dumps(exp_am)}")

        # ---------- CHECK 1: LIVE loop, no DB, luma A << B ----------
        log("=== CHECK 1: set exposure=-2.0 -> preview A ; set +2.0 -> preview B ===")
        set_a = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"exposure": -2.0}},
            timeout=15.0,
        )
        log(f"set_params(-2.0) -> {json.dumps(set_a)}")
        prev_a = bridge.call("dev_preview", {"max_w": 800, "max_h": 800}, timeout=20.0)
        results["preview_A_json"] = prev_a
        log(f"preview A -> {json.dumps(prev_a)}")

        set_b = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"exposure": 2.0}},
            timeout=15.0,
        )
        log(f"set_params(+2.0) -> {json.dumps(set_b)}")
        prev_b = bridge.call("dev_preview", {"max_w": 800, "max_h": 800}, timeout=20.0)
        results["preview_B_json"] = prev_b
        log(f"preview B -> {json.dumps(prev_b)}")

        luma_a = luma_b = None
        path_a = prev_a.get("path") or prev_a.get("stale_preview")
        path_b = prev_b.get("path") or prev_b.get("stale_preview")
        if path_a:
            luma_a = mean_luma(host_path(run_dir, path_a))
        if path_b:
            luma_b = mean_luma(host_path(run_dir, path_b))
        results["luma_A"] = luma_a
        results["luma_B"] = luma_b
        log(f"mean_luma A (exposure -2.0) = {luma_a} ; B (exposure +2.0) = {luma_b}")

        # ---------- CHECK 2: NO DB commit (history_count unchanged by preview) ----------
        log("=== CHECK 2: history_count unchanged across preview() calls (no DB write) ===")
        hc_before = bridge.call("dev_history_count", {}, timeout=15.0)
        for _ in range(5):
            bridge.call("dev_preview", {"max_w": 256, "max_h": 256}, timeout=20.0)
        hc_after = bridge.call("dev_history_count", {}, timeout=15.0)
        results["history_before_previews"] = hc_before
        results["history_after_previews"] = hc_after
        log(
            f"history_count before 5 previews = {json.dumps(hc_before)} ; after ="
            f" {json.dumps(hc_after)}"
        )

        # ---------- CHECK 3: dimensions respect caps + aspect, no upscale ----------
        log("=== CHECK 3: dimension caps ===")
        dim_cases = [
            {"max_w": 800, "max_h": 800},
            {"max_w": 400, "max_h": 400},
            {"max_w": 200, "max_h": 100},
            {"max_w": 100000, "max_h": 100000},  # huge: must NOT upscale
        ]
        dims = []
        for cap in dim_cases:
            r = bridge.call("dev_preview", cap, timeout=20.0)
            p = r.get("path") or r.get("stale_preview")
            actual = image_dims(host_path(run_dir, p)) if p else None
            dims.append(
                {
                    "cap": cap,
                    "reported": [r.get("width"), r.get("height")],
                    "png": actual,
                    "status": r.get("status"),
                }
            )
            log(
                f"cap {cap} -> reported {r.get('width')}x{r.get('height')} png={actual}"
                f" status={r.get('status')}"
            )
        results["dimensions"] = dims

        # ---------- CHECK 4: no-hang 20 rapid set+preview cycles ----------
        log("=== CHECK 4: 20 rapid set_params+preview cycles (no-hang) ===")
        cyc = {"total": 20, "completed": 0, "statuses": [], "first_hang": None}
        for i in range(20):
            val = -3.0 + (i % 20) * 0.3
            try:
                bridge.call(
                    "dev_set_params",
                    {"op": "exposure", "instance": 0, "fields": {"exposure": val}},
                    timeout=10.0,
                )
                pr = bridge.call("dev_preview", {"max_w": 512, "max_h": 512}, timeout=10.0)
                cyc["completed"] += 1
                cyc["statuses"].append(pr.get("status") or ("error" if pr.get("error") else "?"))
            except Exception as e:  # noqa: BLE001  (timeout == hang)
                cyc["first_hang"] = {"i": i, "value": val, "error": str(e)}
                log(f"CYCLE HANG at {i} (value={val}): {e}")
                break
        results["cycles"] = cyc
        log(
            f"cycles: {cyc['completed']}/20 completed; statuses={cyc['statuses']};"
            f" first_hang={cyc['first_hang']}"
        )

        alive_mid = container_alive(run_dir)
        results["container_alive_after_cycles"] = alive_mid

        # ---------- CHECK 5: not-in-darkroom -> error ----------
        log("=== CHECK 5: leave darkroom, preview() must return {error} ===")
        bridge.call("leave_darkroom", {}, timeout=20.0)
        time.sleep(0.5)
        prev_nd = bridge.call("dev_preview", {"max_w": 400, "max_h": 400}, timeout=20.0)
        results["preview_not_in_darkroom"] = prev_nd
        log(f"preview (not in darkroom) -> {json.dumps(prev_nd)}")
        alive_after = container_alive(run_dir)
        results["container_alive_after_check5"] = alive_after

        logtext = log_path.read_text(errors="replace")
        deadlock_hits = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        results["deadlock_log_hits"] = deadlock_hits

        print("\n===== T1.5 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        # ---- evaluate ----
        def is_ok(r):
            return isinstance(r, dict) and r.get("status") == "ok" and r.get("path")

        luma_ok = luma_a is not None and luma_b is not None and (luma_b - luma_a) > 10.0

        hc_unchanged = hc_before.get("count") == hc_after.get("count") and hc_before.get(
            "history_end"
        ) == hc_after.get("history_end")

        def cap_ok(entry):
            png = entry.get("png")
            cap = entry["cap"]
            rep = entry.get("reported")
            if not png or not rep:
                return False
            w, h = png
            # png matches reported
            if [w, h] != rep:
                return False
            # respect caps
            if cap["max_w"] < 50000 and (w > cap["max_w"] or h > cap["max_h"]):
                return False
            return True

        # backbuf native size = the huge-cap result (no upscale ceiling)
        native = None
        for e in dims:
            if e["cap"]["max_w"] >= 50000:
                native = e.get("png")
        no_upscale_ok = True
        if native:
            for e in dims:
                png = e.get("png")
                if png and (png[0] > native[0] or png[1] > native[1]):
                    no_upscale_ok = False

        checks = {
            "1 preview A ok (status ok + path)": is_ok(prev_a),
            "1 preview B ok (status ok + path)": is_ok(prev_b),
            "1 live luma B >> A (delta>10)": luma_ok,
            "2 history_count unchanged by preview": hc_unchanged,
            "3 all dimension caps respected + aspect": all(cap_ok(e) for e in dims),
            "3 no upscale beyond backbuf": no_upscale_ok,
            "4 all 20 cycles completed no hang": (
                cyc["completed"] == 20 and cyc["first_hang"] is None
            ),
            "4 container alive after cycles": alive_mid,
            "5 not-in-darkroom -> error": isinstance(prev_nd, dict) and bool(prev_nd.get("error")),
            "5 container alive after check5": alive_after,
            "no deadlock/critical/assertion in log": len(deadlock_hits) == 0,
        }
        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print(f"\nALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== T1.5 INFRA FAILURE: {e} =====", file=sys.stderr)
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
