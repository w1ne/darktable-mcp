#!/usr/bin/env python3
"""T1.4 acceptance driver: module lifecycle (enable_module race fix + add_instance).

Proves the four T1.4 acceptance points against the live C bindings over the
spike bridge (docker/run-dt-bridge-spike.sh + spike_methods.lua). Does NOT touch
the clean bridge.

Acceptance points (all quoted in the final report):
  1. RACE FIX (key test): tonecurve default-disabled. In a SINGLE fresh session:
        open darkroom -> pre-enable baseline preview -> set an S-curve
        (set_params) -> enable_module("tonecurve",0,true) -> preview.
     Measure luma std-dev delta vs the pre-enable baseline. Repeat the WHOLE
     session 10 times FRESH (fresh darktable process each time). The std-dev
     must rise 10/10 with NO enable-to-identity-first workaround. Quote the 10
     deltas.
  2. enable/disable round-trip: enable a disabled module -> active_modules shows
     enabled=true + history grew; disable -> enabled=false + history grew.
  3. add_instance("exposure") -> returns new multi_priority (e.g. 1);
     active_modules shows two exposure entries; set_params on instance 1 works
     independently of instance 0.
  4. No-hang: 20 rapid enable/disable + add_instance cycles all return, the
     container stays alive, and the log is clean (no deadlock/assert/critical).

Usage: python3 darktable-mcp/spike/run_t1_4.py [N_RACE_SESSIONS]
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

# S-curve on the L channel: lifts contrast clearly (shadows down, highlights up).
S_CURVE = [
    {"x": 0.0, "y": 0.0},
    {"x": 0.25, "y": 0.10},
    {"x": 0.5, "y": 0.5},
    {"x": 0.75, "y": 0.90},
    {"x": 1.0, "y": 1.0},
]
# minimum std-dev rise that counts as "the contrast increase appeared".
STD_DELTA_MIN = 1.0


def _load_bridge_client_module():
    client_path = MCP_DIR / "darktable_mcp" / "bridge" / "client.py"
    spec = importlib.util.spec_from_file_location("darktable_mcp_bridge_client", client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge


def log(msg: str) -> None:
    print(f"[t1.4] {msg}", flush=True)


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
    result = subprocess.run([str(RUN_SCRIPT), "start"], capture_output=True, text=True, timeout=90)
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


def luma_std(path: str):
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            arr = np.asarray(im.convert("L"), dtype="float64")
            return float(arr.std())
    except Exception as e:  # noqa: BLE001
        log(f"luma_std skipped: {e}")
        return None


def open_fixture(run_dir: str) -> Bridge:
    """Import the fixture, enter darkroom on it, return a ready Bridge."""
    log_path = Path(run_dir) / "dt.log"
    log(f"bridge ready: {wait_for_ready(log_path)!r}")
    shutil.copy2(FIXTURE_IMAGE, Path(run_dir) / "import-src" / FIXTURE_IMAGE.name)
    cache_dir = Path(run_dir) / "cache-mcp" / "darktable-mcp"
    plugin_path = Path(run_dir) / "config" / "lua" / "darktable_mcp.lua"
    bridge = Bridge(cache_dir=cache_dir, plugin_path=plugin_path)

    bridge.call("import_batch", {"source_path": "/run/import-src", "recursive": True}, timeout=30.0)
    photos = bridge.call("view_photos", {"limit": 50}, timeout=15.0)
    mire = next((p for p in photos if "mire1" in (p.get("filename") or "").lower()), None)
    if not mire:
        raise RuntimeError(f"fixture not in view_photos: {photos}")
    image_id = int(mire["id"])
    opened = bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
    if opened.get("view") != "darkroom":
        raise RuntimeError(f"failed to enter darkroom: {opened}")
    return bridge


def preview_std(bridge: Bridge, run_dir: str):
    prev = bridge.call("dev_preview", {"max_w": 900, "max_h": 900}, timeout=25.0)
    p = prev.get("path") or prev.get("stale_preview")
    std = luma_std(host_path(run_dir, p)) if p else None
    return std, prev


def race_session(idx: int) -> dict:
    """ONE fresh-process race test: baseline -> set S-curve -> enable -> preview.

    Deliberately does NO sleep between set_params and enable_module so the enable
    lands while the set_params-triggered preview render is most likely still in
    flight -- the exact condition that used to poison the pixelpipe cache.
    """
    run_dir = start_bridge()
    log(f"[race {idx}] RUN_DIR = {run_dir}")
    try:
        bridge = open_fixture(run_dir)

        # pre-enable baseline (tonecurve is default-disabled -> plain image)
        std_before, prev_before = preview_std(bridge, run_dir)

        # set the S-curve while STILL disabled (kicks an OFF render), then enable
        # IMMEDIATELY (races that in-flight render), then read back the frame.
        set_res = bridge.call(
            "dev_set_params",
            {
                "op": "tonecurve",
                "instance": 0,
                "fields": {"tonecurve": [S_CURVE], "tonecurve_nodes": [5]},
            },
            timeout=20.0,
        )
        en_res = bridge.call(
            "dev_enable_module", {"op": "tonecurve", "instance": 0, "enabled": True}, timeout=15.0
        )
        std_after, prev_after = preview_std(bridge, run_dir)

        delta = (
            (std_after - std_before) if (std_before is not None and std_after is not None) else None
        )
        log(
            f"[race {idx}] std_before={std_before} std_after={std_after} delta={delta} "
            f"before_status={prev_before.get('status')} after_status={prev_after.get('status')} "
            f"enabled={en_res.get('enabled')}"
        )
        return {
            "idx": idx,
            "std_before": std_before,
            "std_after": std_after,
            "delta": delta,
            "set_ok": bool(set_res.get("ok")),
            "enabled": en_res.get("enabled"),
            "after_status": prev_after.get("status"),
            "alive": container_alive(run_dir),
        }
    finally:
        stop_bridge(run_dir)


def lifecycle_session() -> dict:
    """One session covering acceptance #2, #3, #4 (no fresh-process needed)."""
    out: dict = {}
    run_dir = start_bridge()
    log(f"[lifecycle] RUN_DIR = {run_dir}")
    log_path = Path(run_dir) / "dt.log"
    try:
        bridge = open_fixture(run_dir)

        def active(op):
            mods = bridge.call("dev_active_modules", {}, timeout=15.0)["modules"]
            return [m for m in mods if m["op"] == op]

        def hcount():
            return bridge.call("dev_history_count", {}, timeout=15.0)["count"]

        # ---- #2 enable/disable round-trip (use a cleanly disabled module) ----
        log("=== #2 enable/disable round-trip (colisa/velvia-style: use 'velvia') ===")
        target = "velvia"  # default-disabled, single-op, cheap
        # fall back to tonecurve if velvia not present in this build
        if not active(target):
            target = "tonecurve"
        h0 = hcount()
        en1 = bridge.call(
            "dev_enable_module", {"op": target, "instance": 0, "enabled": True}, timeout=15.0
        )
        h1 = hcount()
        st_on = active(target)[0]
        dis1 = bridge.call(
            "dev_enable_module", {"op": target, "instance": 0, "enabled": False}, timeout=15.0
        )
        h2 = hcount()
        st_off = active(target)[0]
        out["roundtrip"] = {
            "target": target,
            "h_before": h0,
            "h_after_enable": h1,
            "h_after_disable": h2,
            "enable_ret": en1,
            "disable_ret": dis1,
            "enabled_after_enable": st_on["enabled"],
            "enabled_after_disable": st_off["enabled"],
        }
        log(f"roundtrip: {json.dumps(out['roundtrip'])}")

        # ---- #3 add_instance(exposure) ----
        log("=== #3 add_instance(exposure) ===")
        exp_before = active("exposure")
        add = bridge.call("dev_add_instance", {"op": "exposure"}, timeout=20.0)
        time.sleep(0.5)
        exp_after = active("exposure")
        new_prio = add.get("instance")
        # set exposure independently on instance 0 and the new instance
        set0 = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"exposure": 0.20}},
            timeout=15.0,
        )
        set1 = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": new_prio, "fields": {"exposure": 1.30}},
            timeout=15.0,
        )
        g0 = bridge.call("dev_get_params", {"op": "exposure", "instance": 0}, timeout=15.0)
        g1 = bridge.call("dev_get_params", {"op": "exposure", "instance": new_prio}, timeout=15.0)
        v0 = g0.get("fields", {}).get("exposure", {}).get("value")
        v1 = g1.get("fields", {}).get("exposure", {}).get("value")
        out["add_instance"] = {
            "add_ret": add,
            "exposure_count_before": len(exp_before),
            "exposure_count_after": len(exp_after),
            "new_multi_priority": new_prio,
            "instances_after": [
                {"instance": m["instance"], "multi_name": m["multi_name"]} for m in exp_after
            ],
            "set0_ok": bool(set0.get("ok")),
            "set1_ok": bool(set1.get("ok")),
            "readback_instance0_exposure": v0,
            "readback_instance1_exposure": v1,
        }
        log(f"add_instance: {json.dumps(out['add_instance'])}")

        # ---- #4 20 rapid enable/disable + add_instance cycles, no hang ----
        log("=== #4 20 rapid enable/disable + add_instance cycles ===")
        cycle_returns = 0
        for i in range(20):
            want = i % 2 == 0
            r = bridge.call(
                "dev_enable_module",
                {"op": "tonecurve", "instance": 0, "enabled": want},
                timeout=15.0,
            )
            if isinstance(r, dict) and (r.get("ok") or "enabled" in r):
                cycle_returns += 1
            if i % 5 == 4:
                ai = bridge.call("dev_add_instance", {"op": "exposure"}, timeout=20.0)
                if isinstance(ai, dict) and ai.get("ok"):
                    cycle_returns += 1
        alive = container_alive(run_dir)
        out["rapid"] = {
            "expected_returns": 24,
            "actual_returns": cycle_returns,
            "container_alive": alive,
        }
        log(f"rapid cycles: {json.dumps(out['rapid'])}")

        logtext = log_path.read_text(errors="replace")
        deadlock_hits = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        out["deadlock_log_hits"] = deadlock_hits
        return out
    finally:
        stop_bridge(run_dir)


def main() -> int:  # noqa: C901
    if not FIXTURE_IMAGE.is_file():
        log(f"FAIL: fixture not found at {FIXTURE_IMAGE}")
        return 1

    n_sessions = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    results: dict = {}

    # ---------- ACCEPTANCE #1: the race fix, 10 fresh sessions ----------
    log(f"=== ACCEPTANCE #1: race fix over {n_sessions} FRESH sessions ===")
    race = []
    for i in range(1, n_sessions + 1):
        try:
            race.append(race_session(i))
        except Exception as e:  # noqa: BLE001
            log(f"[race {i}] INFRA FAILURE: {e}")
            race.append({"idx": i, "delta": None, "error": str(e)})
    results["race_sessions"] = race
    deltas = [r.get("delta") for r in race]
    passes = sum(1 for d in deltas if d is not None and d > STD_DELTA_MIN)

    # ---------- ACCEPTANCE #2/#3/#4: one lifecycle session ----------
    log("=== ACCEPTANCE #2/#3/#4: lifecycle session ===")
    try:
        results["lifecycle"] = lifecycle_session()
    except Exception as e:  # noqa: BLE001
        log(f"lifecycle INFRA FAILURE: {e}")
        results["lifecycle"] = {"error": str(e)}

    print("\n===== T1.4 RESULTS =====")
    print(json.dumps(results, indent=2, default=str))

    lc = results.get("lifecycle", {})
    rt = lc.get("roundtrip", {})
    ai = lc.get("add_instance", {})
    rp = lc.get("rapid", {})

    checks = {
        f"1 race: std-dev rose in ALL {n_sessions} fresh sessions": passes == n_sessions,
        "2 enable sets enabled=true": rt.get("enabled_after_enable") is True,
        "2 history grew on enable": (rt.get("h_after_enable") or 0) > (rt.get("h_before") or 0),
        "2 disable sets enabled=false": rt.get("enabled_after_disable") is False,
        "2 history grew on disable": (
            (rt.get("h_after_disable") or 0) > (rt.get("h_after_enable") or 0)
        ),
        "3 add_instance returned a new multi_priority": (
            isinstance(ai.get("new_multi_priority"), int) and ai.get("new_multi_priority") >= 1
        ),
        "3 two exposure instances now present": (
            ai.get("exposure_count_after", 0) == ai.get("exposure_count_before", 0) + 1
        ),
        "3 instance 1 set independent of instance 0": (
            ai.get("readback_instance0_exposure") is not None
            and ai.get("readback_instance1_exposure") is not None
            and abs(ai["readback_instance0_exposure"] - 0.20) < 1e-3
            and abs(ai["readback_instance1_exposure"] - 1.30) < 1e-3
        ),
        "4 all rapid cycles returned": rp.get("actual_returns") == rp.get("expected_returns"),
        "4 container alive after rapid cycles": rp.get("container_alive") is True,
        "no deadlock/critical/assertion in log": len(lc.get("deadlock_log_hits", [])) == 0,
    }
    print()
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    print(f"\nRACE STD-DEV DELTAS ({passes}/{n_sessions} rose > {STD_DELTA_MIN}):")
    for r in race:
        print(
            f"  session {r['idx']:2}: before={r.get('std_before')} after={r.get('std_after')} "
            f"delta={r.get('delta')}  status={r.get('after_status')}"
        )

    allok = all(checks.values())
    print(f"\nALL CHECKS PASSED: {allok}")
    return 0 if allok else 2


if __name__ == "__main__":
    sys.exit(main())
