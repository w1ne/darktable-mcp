#!/usr/bin/env python3
"""T0.2 end-to-end acceptance: file-based JSON-RPC bridge, live darktable.

Starts a long-lived darktable session under Xvfb inside the dt-build
container (docker/run-dt-bridge.sh) with the darktable_mcp Lua plugin wired
into an isolated --configdir, waits for the plugin's readiness line, imports
a fixture RAW into the isolated library via the bridge's own `import_batch`
method, then round-trips `view_photos` through the same Python bridge client
the real MCP server uses (darktable_mcp.bridge.client.Bridge) and asserts
the imported image comes back non-empty.

This is not a pytest suite -- it drives a real docker container and a real
darktable GUI process end to end, so it is run as a script:

    python3 darktable-mcp/e2e/test_view_photos_e2e.py

Exit code 0 = PASS (prints the ready line + the view_photos response JSON).
Non-zero = FAIL (prints whatever diagnostic it has, including a dt.log tail).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
MCP_DIR = E2E_DIR.parent
ROOT_DIR = MCP_DIR.parent

RUN_SCRIPT = ROOT_DIR / "docker" / "run-dt-bridge.sh"
FIXTURE_IMAGE = ROOT_DIR / "src-dt" / "src" / "tests" / "integration" / "images" / "mire1.cr2"

READY_LINE = "darktable-mcp bridge: ready"


def _load_bridge_client_module():
    """Load bridge/client.py directly, bypassing darktable_mcp/__init__.py.

    The package __init__ unconditionally imports .server, which requires the
    `mcp` pip package (server.py:8) -- not installed here and out of scope
    for T0.2 (that's the MCP-tool-wiring layer, T1.6). client.py itself has
    zero package-internal imports (stdlib only: json/os/time/uuid/pathlib),
    so it loads standalone fine via an explicit file spec.
    """
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
    print(f"[e2e] {msg}", flush=True)


def wait_for_ready(log_path: Path, timeout: float = 90.0) -> str:
    """Poll dt.log until READY_LINE appears; return that line (stripped)."""
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
    result = subprocess.run(
        [str(RUN_SCRIPT), "start"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"run-dt-bridge.sh start failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    run_dir = result.stdout.strip().splitlines()[-1].strip()
    if not run_dir:
        raise RuntimeError(f"run-dt-bridge.sh start printed no RUN_DIR:\n{result.stdout!r}")
    return run_dir


def stop_bridge(run_dir: str) -> None:
    subprocess.run(
        [str(RUN_SCRIPT), "stop", run_dir],
        capture_output=True,
        text=True,
        timeout=30,
    )


def check_no_orphans(run_dir: str) -> None:
    """Best-effort: confirm the container this run started is really gone."""
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


def main() -> int:
    if not FIXTURE_IMAGE.is_file():
        log(f"FAIL: fixture image not found at {FIXTURE_IMAGE}")
        return 1

    run_dir = start_bridge()
    log(f"RUN_DIR = {run_dir}")
    log_path = Path(run_dir) / "dt.log"

    try:
        ready_line = wait_for_ready(log_path, timeout=90.0)
        log(f"bridge ready: {ready_line!r}")

        # Fixture: drop the darktable test-suite's own mire1.cr2 (a real
        # Canon CR2, used upstream for integration tests -- see
        # src-dt/src/tests/integration/images/) into the isolated
        # import-src/ that's bind-mounted into the container at /run.
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
        log(f"import_batch response: {json.dumps(import_result)}")
        if not import_result.get("imported"):
            raise RuntimeError(f"import_batch imported 0 files: {import_result}")

        photos = bridge.call("view_photos", {"limit": 50}, timeout=15.0)
        log(f"view_photos response: {json.dumps(photos, indent=2)}")

        if not photos:
            raise RuntimeError("view_photos returned an empty result")
        if not any("mire1" in (p.get("filename") or "").lower() for p in photos):
            raise RuntimeError(f"imported fixture not found in view_photos result: {photos}")

        print("\n===== PASS =====")
        print(f"ready line: {ready_line}")
        print("view_photos result:")
        print(json.dumps(photos, indent=2))
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"\n===== FAIL: {e} =====", file=sys.stderr)
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            print(f"----- dt.log tail -----\n{tail}\n------------------------", file=sys.stderr)
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
