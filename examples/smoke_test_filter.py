"""Live-darktable smoke test for the rating-filter --luacmd snippet.

For each of the 5 filter cases, launch a fresh darktable with the
emitted snippet, wait for startup, and assert no LUA ERROR lines
appeared. Without this we can't tell that the snippet actually parses
and runs in the real Lua context — unit tests only check the string.

Usage: venv/bin/python examples/smoke_test_filter.py
"""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from darktable_mcp.tools.preview_tools import build_darktable_command

REAL_DARKTABLE = "/home/andrii/.local/bin/darktable"
TEST_FOLDER = Path("/home/andrii/Pictures/import-2026-04-26")
LOG_PREFIX = "/tmp/darktable-filter-smoke-"


def kill_running_darktable() -> None:
    subprocess.run(["killall", "-9", "darktable"], capture_output=True)
    time.sleep(2)


def run_one_case(label: str, rating_kwargs: dict) -> tuple[bool, str]:
    """Launch darktable with the emitted filter, wait, scan for LUA ERROR."""
    kill_running_darktable()
    log_path = f"{LOG_PREFIX}{label}.log"
    if os.path.exists(log_path):
        os.remove(log_path)

    cmd = build_darktable_command(TEST_FOLDER, darktable_path=REAL_DARKTABLE, **rating_kwargs)
    cmd = ["darktable" if c == "darktable" else c for c in cmd]  # use the real path arg
    # Need -d lua so any Lua errors land in stderr.
    cmd = cmd[:1] + ["-d", "lua"] + cmd[1:]

    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(8)

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    time.sleep(2)

    log_text = Path(log_path).read_text(errors="replace")
    # The bridge plugin is loaded; its "ready" message confirms Lua actually ran.
    bridge_ready = "darktable-mcp bridge: ready" in log_text
    # Look for LUA ERROR lines that aren't from unrelated noise.
    lua_errors = [
        line for line in log_text.splitlines()
        if "LUA ERROR" in line
    ]
    return (bridge_ready and not lua_errors, log_text if (lua_errors or not bridge_ready) else "")


def main() -> int:
    cases = [
        ("exact-5", {"rating": 5}),
        ("geq-4", {"rating_min": 4}),
        ("leq-2", {"rating_max": 2}),
        ("range-2-4", {"rating_min": 2, "rating_max": 4}),
        ("full-range", {"rating_min": -1, "rating_max": 5}),  # emits no luacmd; control case
    ]
    failures = []
    for label, kwargs in cases:
        print(f"--- case: {label} ({kwargs}) ---")
        ok, log_excerpt = run_one_case(label, kwargs)
        if ok:
            print(f"  OK")
        else:
            failures.append((label, log_excerpt))
            print(f"  FAIL - see {LOG_PREFIX}{label}.log")
    kill_running_darktable()
    if failures:
        print(f"\n{len(failures)} case(s) failed:")
        for label, excerpt in failures:
            print(f"\n--- {label} log excerpt ---")
            for line in excerpt.splitlines():
                if "LUA ERROR" in line or "darktable-mcp" in line.lower():
                    print(f"  {line}")
        return 1
    print(f"\nAll {len(cases)} cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
