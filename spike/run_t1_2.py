#!/usr/bin/env python3
"""T1.2 acceptance driver: exercise darktable.develop.get_params(op, instance).

Reuses the same spike bridge as run_t1_1.py (docker/run-dt-bridge-spike.sh +
spike_methods.lua, now also registering the dev_get_params probe). Checks:

  1. get_params("exposure", 0) -> fields incl. exposure (float w/ min/max/def)
     and black (float). Quote them.
  2. An ENUM field renders as {value,label,options} -- exposure.mode.
  3. A BOOL field renders as a bare bool -- exposure.compensate_exposure_bias.
  4. get_params("nonexistent", 0) -> {error=...} table, no crash.
  5. Round-trip: active_modules() reports instance=0 for base exposure
     (multi_priority), and get_params("exposure",0) matches it.

Usage: python3 darktable-mcp/spike/run_t1_2.py
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
    print(f"[t1.2] {msg}", flush=True)


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

        log("=== open darkroom on mire1 ===")
        opened = bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
        log(f"open_darkroom -> {json.dumps(opened)}")
        if opened.get("view") != "darkroom":
            raise RuntimeError(f"failed to enter darkroom: {opened}")

        # --- CHECK 5a: active_modules reports base exposure instance == 0 ---
        am = bridge.call("dev_active_modules", {}, timeout=15.0)
        exposure_am = [m for m in am.get("modules", []) if m["op"] == "exposure"]
        results["active_modules_exposure"] = exposure_am
        log(f"active_modules exposure entries: {json.dumps(exposure_am)}")

        # --- CHECK 1/2/3: get_params(exposure, 0) ---
        log("=== CHECK: get_params('exposure', 0) ===")
        gp = bridge.call("dev_get_params", {"op": "exposure", "instance": 0}, timeout=15.0)
        results["get_params_exposure"] = gp
        log(f"get_params(exposure,0) ->\n{json.dumps(gp, indent=2)}")

        # --- CHECK 4: unknown module -> error table ---
        log("=== CHECK: get_params('nonexistent', 0) ===")
        gerr = bridge.call("dev_get_params", {"op": "nonexistent", "instance": 0}, timeout=15.0)
        results["get_params_nonexistent"] = gerr
        log(f"get_params(nonexistent,0) -> {json.dumps(gerr)}")

        fields = gp.get("fields", {}) if isinstance(gp, dict) else {}
        exposure_field = fields.get("exposure")
        black_field = fields.get("black")
        mode_field = fields.get("mode")
        bias_field = fields.get("compensate_exposure_bias")

        print("\n===== T1.2 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        checks = {
            "exposure float shape": (
                isinstance(exposure_field, dict)
                and "value" in exposure_field
                and "min" in exposure_field
                and "max" in exposure_field
                and "default" in exposure_field
            ),
            "black float shape": (
                isinstance(black_field, dict) and "min" in black_field and "max" in black_field
            ),
            "enum mode shape {value,label,options}": (
                isinstance(mode_field, dict)
                and "value" in mode_field
                and "label" in mode_field
                and isinstance(mode_field.get("options"), list)
                and len(mode_field["options"]) >= 2
            ),
            "bool compensate_exposure_bias is bare bool": isinstance(bias_field, bool),
            "unknown module -> error table": isinstance(gerr, dict) and "error" in gerr,
            "roundtrip: base exposure instance==0 in active_modules": any(
                m.get("instance") == 0 for m in exposure_am
            ),
            "roundtrip: get_params instance==0": gp.get("instance") == 0,
        }
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print(f"\nALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== T1.2 INFRA FAILURE: {e} =====", file=sys.stderr)
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
