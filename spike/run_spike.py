#!/usr/bin/env python3
"""T0.3 de-risk spike driver: pure-Lua live darkroom control, no C changes.

Drives docker/run-dt-bridge-spike.sh (spike variant of run-dt-bridge.sh that
additionally loads darktable-mcp/spike/spike_methods.lua) through the same
Python Bridge client T0.2 uses, and measures the three sub-goals from
PLAN.md T0.3:

  A. can Lua drive a darkroom slider live?      (open_darkroom + nudge)
  B. can Lua return a preview image?            (preview)
  C. do they compose into a visible edit loop?  (nudge -> preview, luma diff)

Exit code 0 = spike ran to completion (does NOT mean "all sub-goals passed"
-- a well-documented negative on A or C is itself a valid spike result, see
PLAN.md). Non-zero = the spike itself could not run (container/bridge
failure), not a sub-goal failure.

Usage: python3 darktable-mcp/spike/run_spike.py
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
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge
BridgeError = _client_mod.BridgeError
BridgeTimeoutError = _client_mod.BridgeTimeoutError


def log(msg: str) -> None:
    print(f"[spike] {msg}", flush=True)


def wait_for_ready(log_path: Path, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if READY_LINE in line:
                    return line.strip()
            if "darktable exit code:" in text:
                raise RuntimeError(
                    f"darktable exited before the bridge became ready; dt.log tail:\n{text[-4000:]}"
                )
        time.sleep(0.5)
    tail = (
        log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        if log_path.exists()
        else "(no log file)"
    )
    raise TimeoutError(f"'{READY_LINE}' not seen within {timeout}s; dt.log tail:\n{tail}")


def start_bridge() -> str:
    result = subprocess.run([str(RUN_SCRIPT), "start"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"run-dt-bridge-spike.sh start failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    run_dir = result.stdout.strip().splitlines()[-1].strip()
    if not run_dir:
        raise RuntimeError(f"run-dt-bridge-spike.sh start printed no RUN_DIR:\n{result.stdout!r}")
    return run_dir


def stop_bridge(run_dir: str) -> None:
    subprocess.run([str(RUN_SCRIPT), "stop", run_dir], capture_output=True, text=True, timeout=30)


def check_no_orphans(run_dir: str) -> None:
    cid_file = Path(run_dir) / "container_id"
    if not cid_file.exists():
        return
    cid = cid_file.read_text().strip()
    result = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"id={cid}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.stdout.strip():
        raise RuntimeError(f"container {cid} still present after stop: {result.stdout!r}")
    log(f"confirmed container {cid} no longer present (docker ps -a)")


def to_host_path(container_path: str, run_dir: str) -> Path:
    """The Lua worker runs inside the container where XDG_CACHE_HOME=/run/cache-mcp
    (see docker/run-dt-bridge-spike.sh), so paths it returns are container-absolute
    (/run/...). The host bind-mounts run_dir at /run, so swap the prefix."""
    if container_path.startswith("/run/"):
        return Path(run_dir) / container_path[len("/run/") :]
    return Path(container_path)


def mean_luma(jpeg_path: str) -> float:
    from PIL import Image

    with Image.open(jpeg_path) as im:
        gray = im.convert("L")
        pixels = list(gray.getdata())
    return sum(pixels) / len(pixels)


def image_dims(jpeg_path: str) -> tuple[int, int]:
    from PIL import Image

    with Image.open(jpeg_path) as im:
        return im.size


def main() -> int:
    if not FIXTURE_IMAGE.is_file():
        log(f"FAIL: fixture image not found at {FIXTURE_IMAGE}")
        return 1

    findings: dict = {"A": {}, "B": {}, "C": {}}

    run_dir = start_bridge()
    log(f"RUN_DIR = {run_dir}")
    log_path = Path(run_dir) / "dt.log"

    try:
        ready_line = wait_for_ready(log_path, timeout=90.0)
        log(f"bridge ready: {ready_line!r}")

        import_src_host = Path(run_dir) / "import-src"
        shutil.copy2(FIXTURE_IMAGE, import_src_host / FIXTURE_IMAGE.name)
        log(f"copied fixture {FIXTURE_IMAGE.name} into {import_src_host}")

        cache_dir = Path(run_dir) / "cache-mcp" / "darktable-mcp"
        plugin_path = Path(run_dir) / "config" / "lua" / "darktable_mcp.lua"
        bridge = Bridge(cache_dir=cache_dir, plugin_path=plugin_path)

        import_result = bridge.call(
            "import_batch",
            {"source_path": "/run/import-src", "recursive": True},
            timeout=30.0,
        )
        log(f"import_batch: {json.dumps(import_result)}")
        if not import_result.get("imported"):
            raise RuntimeError(f"import_batch imported 0 files: {import_result}")

        photos = bridge.call("view_photos", {"limit": 50}, timeout=15.0)
        mire = next((p for p in photos if "mire1" in (p.get("filename") or "").lower()), None)
        if not mire:
            raise RuntimeError(f"imported fixture not found in view_photos result: {photos}")
        image_id = int(mire["id"])
        log(f"target image_id = {image_id} ({mire['filename']})")

        # ---------------- SUB-GOAL A: darkroom + live slider ----------------
        log("=== SUB-GOAL A: open darkroom headlessly, nudge exposure slider ===")
        try:
            open_result = bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
            log(f"open_darkroom: {json.dumps(open_result)}")
            findings["A"]["open_darkroom_result"] = open_result

            debug1 = bridge.call("debug_state", {}, timeout=10.0)
            log(f"debug_state after open_darkroom: {json.dumps(debug1)}")
            findings["A"]["debug_state_after_open"] = debug1

            if open_result.get("view") != "darkroom":
                findings["A"]["verdict"] = (
                    "open_darkroom did NOT switch to darkroom view "
                    f"(got view={open_result.get('view')!r})"
                )
                log(f"A: {findings['A']['verdict']}")
            else:
                read_before = bridge.call(
                    "nudge",
                    {"action_path": "iop/exposure/exposure", "element": "value"},
                    timeout=10.0,
                )
                log(f"exposure read (before nudge): {json.dumps(read_before)}")
                findings["A"]["read_before"] = read_before

                write_result = bridge.call(
                    "nudge",
                    {
                        "action_path": "iop/exposure/exposure",
                        "element": "value",
                        "effect": "set",
                        "size": 2.5,
                    },
                    timeout=10.0,
                )
                log(f"exposure write (set 2.5): {json.dumps(write_result)}")
                findings["A"]["write_result"] = write_result

                read_after = bridge.call(
                    "nudge",
                    {"action_path": "iop/exposure/exposure", "element": "value"},
                    timeout=10.0,
                )
                log(f"exposure read (after nudge): {json.dumps(read_after)}")
                findings["A"]["read_after"] = read_after

                changed = (
                    read_before.get("value") is not None
                    and read_after.get("value") is not None
                    and read_after["value"] != read_before["value"]
                )
                findings["A"]["passed"] = bool(changed)
                findings["A"]["verdict"] = (
                    f"read-back changed {read_before.get('value')} -> {read_after.get('value')}"
                    if changed
                    else (
                        f"read-back DID NOT change ({read_before.get('value')} ->"
                        f" {read_after.get('value')})"
                    )
                )
                log(f"A verdict: {findings['A']['verdict']}")
        except BridgeError as e:
            findings["A"]["error"] = str(e)
            findings["A"]["passed"] = False
            findings["A"]["verdict"] = f"bridge error: {e}"
            log(f"A FAILED with bridge error: {e}")

        # ---------------- SUB-GOAL B: preview export -------------------------
        log("=== SUB-GOAL B: export preview JPEG ===")
        try:
            preview_before = bridge.call(
                "preview",
                {"max_w": 640, "max_h": 640, "tag": "before"},
                timeout=60.0,
            )
            log(f"preview (before, baseline): {json.dumps(preview_before)}")
            findings["B"]["preview_result"] = preview_before

            p = to_host_path(preview_before["path"], run_dir)
            size_bytes = p.stat().st_size if p.exists() else 0
            dims = image_dims(str(p)) if p.exists() else None
            findings["B"]["file_size_bytes"] = size_bytes
            findings["B"]["dims"] = dims
            findings["B"]["passed"] = bool(
                p.exists() and size_bytes > 0 and dims and dims[0] <= 640 and dims[1] <= 640
            )
            findings["B"]["verdict"] = f"path={p}, size={size_bytes} bytes, dims={dims}"
            log(f"B verdict: {findings['B']['verdict']}")
        except BridgeError as e:
            findings["B"]["error"] = str(e)
            findings["B"]["passed"] = False
            findings["B"]["verdict"] = f"bridge error: {e}"
            log(f"B FAILED with bridge error: {e}")

        # ---------------- SUB-GOAL C: does the loop close? -------------------
        log("=== SUB-GOAL C: nudge exposure way up, compare mean luma ===")
        try:
            baseline_path = str(to_host_path(findings["B"]["preview_result"]["path"], run_dir))
            luma_before = mean_luma(baseline_path)
            log(f"baseline preview mean luma = {luma_before:.3f} ({baseline_path})")
            findings["C"]["luma_before"] = luma_before

            # Large, unmissable exposure lift.
            big_write = bridge.call(
                "nudge",
                {
                    "action_path": "iop/exposure/exposure",
                    "element": "value",
                    "effect": "set",
                    "size": 4.0,
                },
                timeout=10.0,
            )
            log(f"big exposure write (set 4.0EV): {json.dumps(big_write)}")
            findings["C"]["big_write_result"] = big_write

            read_confirm = bridge.call(
                "nudge",
                {"action_path": "iop/exposure/exposure", "element": "value"},
                timeout=10.0,
            )
            log(f"exposure read-back immediately after big write: {json.dumps(read_confirm)}")
            findings["C"]["read_confirm"] = read_confirm

            # Immediate preview -- per PLAN.md this is expected to read
            # committed DB history, NOT the live darkroom edit.
            preview_after_immediate = bridge.call(
                "preview",
                {"max_w": 640, "max_h": 640, "tag": "after-immediate"},
                timeout=60.0,
            )
            after_immediate_host = str(to_host_path(preview_after_immediate["path"], run_dir))
            luma_after_immediate = mean_luma(after_immediate_host)
            log(
                f"preview taken WITHOUT leaving darkroom: mean luma = {luma_after_immediate:.3f} "
                f"({after_immediate_host})"
            )
            findings["C"]["preview_after_immediate"] = preview_after_immediate
            findings["C"]["luma_after_immediate"] = luma_after_immediate

            # Now force a commit the only pure-Lua way available: leave
            # darkroom (view switch -> views/darkroom.c leave() ->
            # dt_dev_write_history()), then re-enter and take another preview.
            leave_result = bridge.call("leave_darkroom", {}, timeout=20.0)
            log(f"leave_darkroom: {json.dumps(leave_result)}")
            findings["C"]["leave_result"] = leave_result

            preview_after_commit = bridge.call(
                "preview",
                {"max_w": 640, "max_h": 640, "tag": "after-commit"},
                timeout=60.0,
            )
            after_commit_host = str(to_host_path(preview_after_commit["path"], run_dir))
            luma_after_commit = mean_luma(after_commit_host)
            log(
                "preview taken AFTER leaving darkroom (forces dt_dev_write_history): "
                f"mean luma = {luma_after_commit:.3f} ({after_commit_host})"
            )
            findings["C"]["preview_after_commit"] = preview_after_commit
            findings["C"]["luma_after_commit"] = luma_after_commit

            immediate_changed = luma_after_immediate > luma_before + 1.0
            commit_changed = luma_after_commit > luma_before + 1.0

            findings["C"]["passed_immediate"] = bool(immediate_changed)
            findings["C"]["passed_after_commit"] = bool(commit_changed)
            findings["C"]["verdict"] = (
                f"luma_before={luma_before:.3f}, "
                f"luma_after_immediate(no view switch)={luma_after_immediate:.3f} "
                f"(changed={immediate_changed}), "
                f"luma_after_commit(view-switch round-trip)={luma_after_commit:.3f} "
                f"(changed={commit_changed})"
            )
            log(f"C verdict: {findings['C']['verdict']}")
        except BridgeError as e:
            findings["C"]["error"] = str(e)
            findings["C"]["verdict"] = f"bridge error: {e}"
            log(f"C FAILED with bridge error: {e}")

        print("\n===== SPIKE FINDINGS (T0.3) =====")
        print(json.dumps(findings, indent=2, default=str))
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"\n===== SPIKE INFRA FAILURE: {e} =====", file=sys.stderr)
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
            print(f"----- dt.log tail -----\n{tail}\n------------------------", file=sys.stderr)
        print(json.dumps(findings, indent=2, default=str), file=sys.stderr)
        return 1

    finally:
        log("stopping bridge container")
        stop_bridge(run_dir)
        try:
            check_no_orphans(run_dir)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: orphan check failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
