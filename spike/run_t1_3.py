#!/usr/bin/env python3
"""T1.3 acceptance driver: darktable.develop.set_params(op, instance, fields).

HIGHEST-RISK task (threading + history_mutex). This driver exercises every
acceptance point in PLAN.md T1.3, most importantly the 50-call deadlock stress
test (acceptance 6). Reuses the same spike bridge as run_t1_1/2.py
(docker/run-dt-bridge-spike.sh + spike_methods.lua, now also registering the
dev_set_params + dev_history_count probes). Does NOT touch the clean bridge.

Acceptance points (all quoted in the final report):
  1. set_params(exposure,0,{exposure=1.5}) -> ok, applied.exposure==1.5;
     get_params(exposure,0) then shows ~1.5.
  2. Clamp: set_params(exposure,0,{exposure=999}) -> applied.exposure==18.0,
     clamped[] reports requested 999 -> 18 (min -18 / max 18).
  3. Unknown field: set_params(exposure,0,{nonsense=1}) -> unknown_fields
     ["nonsense"], ok still true, no crash.
  4. Enum by label: set exposure.mode by symbolic label string, confirm via
     get_params.
  5. HISTORY + LIVE: pixelpipe reprocess line present in dt.log after a write;
     native coalescing entry-count for iterations consecutive same-module writes; best
     -effort luma-rose export sanity (DB-export path).
  6. DEADLOCK STRESS: 50 rapid set_params calls, ALL must return, process must
     stay alive, no deadlock/assertion lines in dt.log.

Usage: python3 darktable-mcp/spike/run_t1_3.py
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

# pixelpipe/history lines emitted at -d pipe / -d dev that prove a reprocess.
PIPE_MARKERS = ("dev_pixelpipe_change", "pixelpipe_process", "process_image_job", "pixelpipe")
# fatal patterns that would signal the #1 risk materialising.
DEADLOCK_MARKERS = ("deadlock", "assertion", "assert failed", "**: assertion")


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
    print(f"[t1.3] {msg}", flush=True)


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


def mean_luma(path: str):
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(path).convert("L")
        return float(np.asarray(img, dtype="float64").mean())
    except Exception as e:  # noqa: BLE001
        log(f"mean_luma skipped: {e}")
        return None


def exposure_field_value(gp: dict):
    f = (gp or {}).get("fields", {}).get("exposure")
    if isinstance(f, dict):
        return f.get("value")
    return f


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

        # report exposure module enabled state (affects luma sub-test only).
        am = bridge.call("dev_active_modules", {}, timeout=15.0)
        exp_am = next((m for m in am.get("modules", []) if m["op"] == "exposure"), None)
        results["exposure_module"] = exp_am
        log(f"exposure module state: {json.dumps(exp_am)}")

        # ---------- CHECK 1: basic write + readback ----------
        log("=== CHECK 1: set_params(exposure,0,{exposure=1.5}) ===")
        c1 = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"exposure": 1.5}},
            timeout=15.0,
        )
        results["check1_set"] = c1
        log(f"set_params -> {json.dumps(c1)}")
        c1_rb = bridge.call("dev_get_params", {"op": "exposure", "instance": 0}, timeout=15.0)
        results["check1_readback_exposure"] = exposure_field_value(c1_rb)
        log(f"get_params exposure value after set -> {results['check1_readback_exposure']}")

        # ---------- CHECK 2: clamp ----------
        log("=== CHECK 2: set_params(exposure,0,{exposure=999}) ===")
        c2 = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"exposure": 999}},
            timeout=15.0,
        )
        results["check2_clamp"] = c2
        log(f"set_params(999) -> {json.dumps(c2)}")

        # ---------- CHECK 3: unknown field ----------
        log("=== CHECK 3: set_params(exposure,0,{nonsense=1}) ===")
        c3 = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"nonsense": 1}},
            timeout=15.0,
        )
        results["check3_unknown"] = c3
        log(f"set_params(nonsense) -> {json.dumps(c3)}")

        # ---------- CHECK 4: enum by label ----------
        log("=== CHECK 4: set exposure.mode by label 'EXPOSURE_MODE_DEFLICKER' ===")
        c4 = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"mode": "EXPOSURE_MODE_DEFLICKER"}},
            timeout=15.0,
        )
        results["check4_enum_set"] = c4
        log(f"set_params(mode=label) -> {json.dumps(c4)}")
        c4_rb = bridge.call("dev_get_params", {"op": "exposure", "instance": 0}, timeout=15.0)
        results["check4_readback_mode"] = (c4_rb or {}).get("fields", {}).get("mode")
        log(f"get_params mode after set -> {json.dumps(results['check4_readback_mode'])}")
        # reset mode back to manual so it doesn't disturb the luma sub-test.
        bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"mode": "EXPOSURE_MODE_MANUAL"}},
            timeout=15.0,
        )

        # ---------- CHECK 5a: native coalescing entry count ----------
        log("=== CHECK 5a: native history coalescing over iterations consecutive writes ===")
        # settle: one write so exposure is the current top history item.
        bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"exposure": 0.1}},
            timeout=15.0,
        )
        hc_before = bridge.call("dev_history_count", {}, timeout=15.0)
        iterations = 8
        for i in range(iterations):
            bridge.call(
                "dev_set_params",
                {"op": "exposure", "instance": 0, "fields": {"exposure": 0.2 + 0.1 * i}},
                timeout=15.0,
            )
        hc_after = bridge.call("dev_history_count", {}, timeout=15.0)
        results["coalescing"] = {
            "iterations": iterations,
            "before": hc_before,
            "after": hc_after,
            "delta": hc_after.get("count", 0) - hc_before.get("count", 0),
        }
        log(
            f"coalescing: before={json.dumps(hc_before)} after={json.dumps(hc_after)} "
            f"delta={results['coalescing']['delta']} for iterations={iterations} writes"
        )

        # ---------- CHECK 5b: pixelpipe reprocess line present ----------
        time.sleep(1.0)  # let the async pipe log flush
        logtext = log_path.read_text(errors="replace")
        pipe_lines = [
            ln.strip() for ln in logtext.splitlines() if any(m in ln for m in PIPE_MARKERS)
        ]
        results["pipe_line_sample"] = pipe_lines[-1] if pipe_lines else None
        results["pipe_line_count"] = len(pipe_lines)
        log(
            f"pixelpipe/reprocess log lines seen: {len(pipe_lines)}; "
            f"last = {results['pipe_line_sample']!r}"
        )

        # ---------- CHECK 5c: best-effort luma-rose export sanity ----------
        # DB-export path: leave darkroom commits history to DB, preview exports
        # committed history. Only meaningful if exposure module is enabled.
        luma = {"attempted": False}
        try:
            if exp_am and exp_am.get("enabled"):
                luma["attempted"] = True
                bridge.call(
                    "dev_set_params",
                    {"op": "exposure", "instance": 0, "fields": {"exposure": 0.0}},
                    timeout=15.0,
                )
                bridge.call("leave_darkroom", {}, timeout=20.0)
                base = bridge.call(
                    "preview", {"max_w": 400, "max_h": 400, "tag": "base"}, timeout=30.0
                )
                # preview returns the container path (/run/...); map to host.
                base_host = (
                    run_dir + base["path"][len("/run") :]
                    if base["path"].startswith("/run")
                    else base["path"]
                )
                luma["base_luma"] = mean_luma(base_host)
                bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
                bridge.call(
                    "dev_set_params",
                    {"op": "exposure", "instance": 0, "fields": {"exposure": 4.0}},
                    timeout=15.0,
                )
                bridge.call("leave_darkroom", {}, timeout=20.0)
                hi = bridge.call("preview", {"max_w": 400, "max_h": 400, "tag": "hi"}, timeout=30.0)
                hi_host = (
                    run_dir + hi["path"][len("/run") :]
                    if hi["path"].startswith("/run")
                    else hi["path"]
                )
                luma["hi_luma"] = mean_luma(hi_host)
                bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
            else:
                luma["skipped_reason"] = "exposure module not enabled (force-enable is T1.4)"
        except Exception as e:  # noqa: BLE001
            luma["error"] = str(e)
        results["luma"] = luma
        log(f"luma sanity: {json.dumps(luma)}")

        # ---------- CHECK 6: 50-call deadlock stress ----------
        log("=== CHECK 6: 50 rapid set_params calls (deadlock stress) ===")
        stress = {"total": 50, "returned": 0, "first_hang": None, "errors": []}
        for i in range(50):
            val = -3.0 + (i % 40) * 0.15  # varying, in-range
            try:
                r = bridge.call(
                    "dev_set_params",
                    {"op": "exposure", "instance": 0, "fields": {"exposure": val}},
                    timeout=10.0,
                )
                if isinstance(r, dict) and r.get("ok"):
                    stress["returned"] += 1
                else:
                    stress["errors"].append({"i": i, "resp": r})
            except Exception as e:  # noqa: BLE001  (timeout == hang)
                stress["first_hang"] = {"i": i, "value": val, "error": str(e)}
                log(f"STRESS HANG at call {i} (value={val}): {e}")
                break
        results["stress"] = stress
        log(f"stress: {stress['returned']}/50 returned; first_hang={stress['first_hang']}")

        alive = container_alive(run_dir)
        results["container_alive_after_stress"] = alive
        # liveness ping through the bridge.
        try:
            ping = bridge.call("dev_version", {}, timeout=10.0)
            results["post_stress_ping"] = ping
        except Exception as e:  # noqa: BLE001
            results["post_stress_ping"] = {"error": str(e)}

        logtext = log_path.read_text(errors="replace")
        deadlock_hits = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        results["deadlock_log_hits"] = deadlock_hits

        print("\n===== T1.3 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        def approx(a, b, tol=1e-3):
            try:
                return abs(float(a) - float(b)) <= tol
            except Exception:  # noqa: BLE001
                return False

        c2_clamped = (c2 or {}).get("clamped", [])
        c2_rec = next((r for r in c2_clamped if r.get("field") == "exposure"), None)
        mode_rb = results.get("check4_readback_mode") or {}

        checks = {
            "1 set ok + applied.exposure==1.5": (
                isinstance(c1, dict)
                and c1.get("ok") is True
                and approx((c1.get("applied") or {}).get("exposure"), 1.5)
            ),
            "1 get_params readback ~1.5": approx(
                results["check1_readback_exposure"], 1.5, tol=1e-2
            ),
            "2 clamp applied.exposure==18.0": approx(
                (c2.get("applied") or {}).get("exposure"), 18.0
            ),
            "2 clamp record 999->18": (
                c2_rec is not None
                and approx(c2_rec.get("requested"), 999)
                and approx(c2_rec.get("applied"), 18.0)
            ),
            "3 unknown_fields==['nonsense'] & ok": (
                isinstance(c3, dict)
                and c3.get("ok") is True
                and c3.get("unknown_fields") == ["nonsense"]
            ),
            "4 enum by label -> mode.value==1 label DEFLICKER": (
                isinstance(mode_rb, dict)
                and mode_rb.get("value") == 1
                and mode_rb.get("label") == "EXPOSURE_MODE_DEFLICKER"
            ),
            "5 pixelpipe reprocess line present": bool(results.get("pipe_line_sample")),
            "5 coalescing delta is small (<=2 for iterations=8)": (
                results["coalescing"]["delta"] <= 2
            ),
            "6 all 50 stress calls returned": (
                stress["returned"] == 50 and stress["first_hang"] is None
            ),
            "6 container alive after stress": alive,
            "6 no deadlock/assertion in log": len(deadlock_hits) == 0,
        }
        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print(f"\nALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== T1.3 INFRA FAILURE: {e} =====", file=sys.stderr)
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
