#!/usr/bin/env python3
"""T1.1 acceptance driver: exercise the new darktable.develop.* C bindings.

Reuses the T0.3 spike bridge (docker/run-dt-bridge-spike.sh + spike_methods.lua,
which now also registers dev_version / dev_active_modules probes). Checks:

  1. dev_version -> {api:1, dt_lua_api:"9.7.0", min_bridge:1}
  2. dev_active_modules WHILE IN LIGHTTABLE (no darkroom image) -> empty (NULL-guard)
  3. open mire1.cr2 in darkroom, dev_active_modules -> non-empty, includes exposure

Usage: python3 darktable-mcp/spike/run_t1_1.py
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
    print(f"[t1.1] {msg}", flush=True)


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


def main() -> int:
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

        # --- CHECK 1: version() ---
        log("=== CHECK 1: dev_version ===")
        ver = bridge.call("dev_version", {}, timeout=10.0)
        log(f"dev_version -> {json.dumps(ver)}")
        results["version"] = ver

        # --- CHECK 2: NULL-guard in lighttable (before entering darkroom) ---
        log("=== CHECK 2: dev_active_modules in LIGHTTABLE (NULL-guard) ===")
        lt = bridge.call("dev_active_modules", {}, timeout=10.0)
        log(f"dev_active_modules (lighttable) -> {json.dumps(lt)}")
        results["lighttable_active_modules"] = lt

        # --- CHECK 3: open darkroom, active_modules non-empty w/ exposure ---
        log("=== CHECK 3: open darkroom on mire1, dev_active_modules ===")
        opened = bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
        log(f"open_darkroom -> {json.dumps(opened)}")
        results["open_darkroom"] = opened

        dr = bridge.call("dev_active_modules", {}, timeout=15.0)
        results["darkroom_active_modules"] = dr
        log(f"dev_active_modules (darkroom) count = {dr.get('count')}")
        ops = [m["op"] for m in dr.get("modules", [])]
        log(f"ops = {ops}")
        exposure = [m for m in dr.get("modules", []) if m["op"] == "exposure"]
        log(f"exposure entries: {json.dumps(exposure)}")
        # a few sample entries
        log(f"first 6 entries: {json.dumps(dr.get('modules', [])[:6], indent=2)}")

        print("\n===== T1.1 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        ok = (
            ver.get("api") == 1
            and ver.get("dt_lua_api") == "9.7.0"
            and ver.get("min_bridge") == 1
            and lt.get("count") == 0
            and opened.get("view") == "darkroom"
            and dr.get("count", 0) > 0
            and any(m["op"] == "exposure" for m in dr.get("modules", []))
        )
        print(f"\nALL CHECKS PASSED: {ok}")
        return 0 if ok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== T1.1 INFRA FAILURE: {e} =====", file=sys.stderr)
        if log_path.exists():
            print(
                "----- dt.log tail -----\n" + log_path.read_text(errors="replace")[-6000:],
                file=sys.stderr,
            )
        print(json.dumps(results, indent=2, default=str), file=sys.stderr)
        return 1
    finally:
        log("stopping bridge container")
        stop_bridge(run_dir)


if __name__ == "__main__":
    sys.exit(main())
