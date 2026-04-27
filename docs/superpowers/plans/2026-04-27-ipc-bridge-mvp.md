# IPC Bridge MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `view_photos` and `rate_photos` over a long-running darktable Lua plugin + file-based JSON request/response bridge.

**Architecture:** Python MCP server writes JSON request files into `~/.cache/darktable-mcp/`; a Lua plugin loaded into the user's interactive darktable session polls the cache directory via a worker coroutine, dispatches to a method registry, writes a response file. No subprocess management, no socket libraries, no per-call darktable spawn. Spec: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md`.

**Tech Stack:** Python 3.8+ (stdlib only for the bridge — `json`, `uuid`, `pathlib`, `time`, `os`), Lua (whatever ships with darktable), darktable 4.0+ Lua API. No new Python dependencies.

---

## File map

**Create:**

- `darktable_mcp/bridge/__init__.py` — package marker.
- `darktable_mcp/bridge/client.py` — `Bridge` class + `BridgeError`/`BridgeTimeoutError`/`BridgePluginNotInstalledError`/`BridgeProtocolError`.
- `darktable_mcp/lua/darktable_mcp.lua` — the plugin: cache-dir setup, worker loop, method registry (`view_photos`, `rate_photos`), file dispatch, periodic stale-request sweep.
- `darktable_mcp/cli/__init__.py` — package marker.
- `darktable_mcp/cli/install_plugin.py` — `install(home_dir: Path)` and `uninstall(home_dir: Path)` library functions plus `install_main()` / `uninstall_main()` CLI entry points.
- `tests/test_bridge.py` — Bridge client tests (happy path, timeout, errors, cleanup, concurrency).
- `tests/test_install_plugin.py` — CLI tests against a tmp `$HOME/.config/darktable/`.
- `tests/test_lua_dispatcher.py` — pytest wrapper that runs Lua dispatcher tests via `lua` subprocess.
- `tests/lua/test_dispatcher.lua` — Lua dispatcher tests with a stub `dt` table.
- `tests/test_ipc_bridge_acceptance.py` — pinning tests for the iteration's deliverables.
- `spike/lua_worker_probe.lua` — Task 0 spike artifact (kept in repo for reproducibility).
- `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-spike-findings.md` — Task 0 written observations.

**Modify:**

- `darktable_mcp/__init__.py` — `main()` becomes an argparse dispatcher: no-args → run MCP server (current behavior); `install-plugin` → call `cli.install_plugin.install_main()`; `uninstall-plugin` → call `cli.install_plugin.uninstall_main()`.
- `darktable_mcp/server.py` — construct `Bridge` eagerly in `__init__`; add `view_photos` and `rate_photos` to `_tool_definitions()`; add `_handle_view_photos` and `_handle_rate_photos` async handlers; add to `_handler_map`.
- `tests/test_server.py` — add tests for the two new handlers (mocking `Bridge.call`).
- `tests/test_honesty_pass_acceptance.py` — update `EXPECTED_TOOLS` to include the 2 new tools (was 5, becomes 7). The pin's purpose ("registered set is exactly this list") is preserved; it just reflects the iteration-2 reality.
- `pyproject.toml` — ensure `darktable_mcp/lua/*.lua` ships in the wheel via `[tool.setuptools.package-data]`.
- `README.md` — restore `view_photos` and `rate_photos` to "Implemented tools"; update the "Why some tools are parked" section to point at this iteration's spec; add a one-line install note ("After `pip install darktable-mcp`, run `darktable-mcp install-plugin` and restart darktable").

---

## Task 0: Spike — verify Lua worker primitive works without freezing the GUI

**Files:**
- Create: `spike/lua_worker_probe.lua`
- Create: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-spike-findings.md`

**Why this is Task 0 and not Task 1:** the entire architecture in the spec assumes darktable's Lua runtime exposes a primitive that runs a `while true do ... sleep ... end` loop without blocking the GUI thread. If that assumption is wrong, the whole design pivots. Verify empirically before writing any code that depends on it.

- [ ] **Step 1: Write the spike script**

Write `/home/andrii/projects/darktable-mcp/spike/lua_worker_probe.lua`:

```lua
-- darktable-mcp Task 0 spike: confirm a Lua worker can run a non-blocking
-- loop inside an interactive darktable session without freezing the GUI.
--
-- Install: copy this file to ~/.config/darktable/lua/ and add
--   require "lua_worker_probe"
-- to ~/.config/darktable/luarc, then restart darktable.
--
-- Observe for 5 minutes: the GUI must stay responsive (zoom, pan, switch
-- views) and the log file at /tmp/darktable-mcp-spike.log should grow by
-- one line every ~1 second.

local dt = require("darktable")

local function worker_loop()
  local f = io.open("/tmp/darktable-mcp-spike.log", "w")
  if f then
    f:write("worker started at " .. os.date() .. "\n")
    f:close()
  end
  local tick = 0
  while true do
    tick = tick + 1
    local f2 = io.open("/tmp/darktable-mcp-spike.log", "a")
    if f2 then
      f2:write("tick " .. tick .. " at " .. os.date() .. "\n")
      f2:close()
    end
    -- Try the most likely primitive name; fall back to others if needed.
    if dt.control and dt.control.sleep then
      dt.control.sleep(1000)
    else
      -- If sleep doesn't exist under dt.control, this script will print
      -- an error to darktable's log and we'll know to try a different name.
      error("dt.control.sleep not found — try dt.gui.libs or coroutine.yield")
    end
  end
end

dt.print_log("darktable-mcp spike: starting worker")
if dt.control and dt.control.async then
  dt.control.async(worker_loop)
else
  -- Fallback: try invoking directly. If this blocks darktable's main
  -- Lua thread, we'll see GUI hang and know the spike failed.
  worker_loop()
end
```

- [ ] **Step 2: Manual install + observation**

This is a manual step — the executor performs it on the maintainer's machine and records observations.

```bash
mkdir -p ~/.config/darktable/lua
cp /home/andrii/projects/darktable-mcp/spike/lua_worker_probe.lua ~/.config/darktable/lua/

# Idempotently add the require line
grep -q '^require "lua_worker_probe"' ~/.config/darktable/luarc 2>/dev/null \
  || echo 'require "lua_worker_probe"' >> ~/.config/darktable/luarc

# Open darktable
darktable &
```

Observe for 5 minutes:
1. Does `/tmp/darktable-mcp-spike.log` grow by one line per second? (Confirms the loop runs.)
2. Does the darktable GUI stay responsive — can you zoom into an image, switch from lighttable to darkroom, hover thumbnails? (Confirms the worker is non-blocking.)
3. Does darktable's own log (visible via `journalctl --user -f` or in stderr) show the `darktable-mcp spike: starting worker` line? (Confirms the plugin loaded.)

Cleanup after observation:
```bash
sed -i '/^require "lua_worker_probe"/d' ~/.config/darktable/luarc
rm ~/.config/darktable/lua/lua_worker_probe.lua
rm -f /tmp/darktable-mcp-spike.log
killall darktable
```

- [ ] **Step 3: Document findings**

Write `/home/andrii/projects/darktable-mcp/docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-spike-findings.md`:

```markdown
# Task 0 Spike Findings

**Date:** <YYYY-MM-DD when run>
**darktable version:** <output of `darktable --version | head -1`>
**OS / display server:** <e.g. Linux 6.x / Wayland (COSMIC)>

## Verdict

PASS / FAIL / PARTIAL

## What worked

- `dt.control.async(worker_loop)` <did/did not> spawn a non-blocking worker.
- `dt.control.sleep(1000)` <did/did not> yield correctly (1-second cadence).
- `/tmp/darktable-mcp-spike.log` grew by <N> lines over <M> minutes.
- GUI responsiveness during observation: <responsive / occasional hitches / frozen>.

## What did not work

<empty if PASS; otherwise note exact error from darktable log and which primitive failed>

## Implication for the bridge design

<one of:>
- PASS → design proceeds as spec'd. Use `dt.control.async` + `dt.control.sleep` in the plugin.
- FAIL → STOP. Escalate to the human with the failure mode. Likely fallback is keyboard-shortcut wake-up; the spec architecture changes.
- PARTIAL → describe what works and what doesn't; propose adjustment.
```

Fill in the actual observed values.

- [ ] **Step 4: Commit**

```bash
git add spike/lua_worker_probe.lua docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-spike-findings.md
git commit -m "spike(ipc-bridge): verify dt.control.async worker primitive"
```

- [ ] **Step 5: Decision gate**

If findings are PASS, proceed to Task 1.
If FAIL or PARTIAL, STOP. Report the findings to the human; do not start Task 1. The spec needs revision.

---

## Task 1: Acceptance pinning tests

**Files:**
- Create: `tests/test_ipc_bridge_acceptance.py`

Pin the iteration's end state. Tests fail at the start; each later task flips one or two green.

- [ ] **Step 1: Write the acceptance test file**

Write `/home/andrii/projects/darktable-mcp/tests/test_ipc_bridge_acceptance.py`:

```python
"""Acceptance tests for the IPC bridge MVP iteration.

Each test pins one piece of the desired final state. They are red at the
start of the iteration and turn green incrementally as tasks land.
See docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md.

These tests stay in the suite as a regression guard after the iteration
completes — do not delete this file when the tests go green.
"""

import importlib
import importlib.resources
import subprocess
import sys

import pytest

from darktable_mcp.server import DarktableMCPServer


def test_view_photos_registered():
    server = DarktableMCPServer()
    assert "view_photos" in server.list_tools()


def test_rate_photos_registered():
    server = DarktableMCPServer()
    assert "rate_photos" in server.list_tools()


def test_bridge_module_exports_required_symbols():
    mod = importlib.import_module("darktable_mcp.bridge.client")
    for name in (
        "Bridge",
        "BridgeError",
        "BridgeTimeoutError",
        "BridgePluginNotInstalledError",
        "BridgeProtocolError",
    ):
        assert hasattr(mod, name), f"darktable_mcp.bridge.client.{name} missing"


def test_lua_plugin_file_ships_with_package():
    plugin = importlib.resources.files("darktable_mcp").joinpath("lua/darktable_mcp.lua")
    assert plugin.is_file(), f"plugin file not found at {plugin}"
    text = plugin.read_text(encoding="utf-8")
    assert "view_photos" in text
    assert "rate_photos" in text


def test_install_plugin_subcommand_runs():
    # Subcommand dispatch via `darktable-mcp install-plugin --help`.
    # We don't run the actual install; just confirm the subcommand is wired.
    result = subprocess.run(
        [sys.executable, "-m", "darktable_mcp", "install-plugin", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "install" in result.stdout.lower()


def test_uninstall_plugin_subcommand_runs():
    result = subprocess.run(
        [sys.executable, "-m", "darktable_mcp", "uninstall-plugin", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "uninstall" in result.stdout.lower()
```

- [ ] **Step 2: Run tests to verify they fail in the expected way**

Run: `venv/bin/pytest tests/test_ipc_bridge_acceptance.py -v`

Expected: all 6 tests FAIL.
- `test_view_photos_registered`, `test_rate_photos_registered` — fail because the tools aren't registered yet.
- `test_bridge_module_exports_required_symbols` — fail with `ModuleNotFoundError: No module named 'darktable_mcp.bridge'`.
- `test_lua_plugin_file_ships_with_package` — fail because the plugin file doesn't exist.
- `test_install_plugin_subcommand_runs`, `test_uninstall_plugin_subcommand_runs` — fail because the subcommands aren't wired (the `python -m darktable_mcp` invocation may also need a `__main__.py` — Task 5 handles this).

If any test fails for a *different* reason (e.g. import error in `darktable_mcp.server`), report as a concern.

- [ ] **Step 3: Verify the rest of the suite is still green**

Run: `venv/bin/pytest --ignore=tests/test_ipc_bridge_acceptance.py -q`

Expected: 116 passed (current count).

- [ ] **Step 4: Commit**

```bash
git add tests/test_ipc_bridge_acceptance.py
git commit -m "test: pin IPC bridge MVP acceptance state"
```

---

## Task 2: Bridge client (TDD)

**Files:**
- Create: `darktable_mcp/bridge/__init__.py`
- Create: `darktable_mcp/bridge/client.py`
- Create: `tests/test_bridge.py`

The Bridge client is testable in complete isolation from darktable: the tests stand up a `threading.Thread` that watches the cache dir and writes a canned response when a request appears (the "fake plugin worker"). This task delivers a fully-tested Bridge before any Lua exists.

- [ ] **Step 1: Write the package marker**

Write `/home/andrii/projects/darktable-mcp/darktable_mcp/bridge/__init__.py`:

```python
"""File-based JSON request/response bridge to the darktable Lua plugin."""
```

- [ ] **Step 2: Write the failing tests**

Write `/home/andrii/projects/darktable-mcp/tests/test_bridge.py`:

```python
"""Tests for darktable_mcp.bridge.client.Bridge."""

import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from darktable_mcp.bridge.client import (
    Bridge,
    BridgeError,
    BridgePluginNotInstalledError,
    BridgeProtocolError,
    BridgeTimeoutError,
)


class FakePlugin:
    """Background thread that mimics the Lua plugin: watches the cache dir,
    writes a canned response when a request file appears."""

    def __init__(self, cache_dir: Path, response_factory):
        self.cache_dir = cache_dir
        self.response_factory = response_factory  # request_dict -> response_dict
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.is_set():
            for req_path in sorted(self.cache_dir.glob("request-*.json")):
                if req_path.name.endswith(".tmp"):
                    continue
                try:
                    text = req_path.read_text(encoding="utf-8")
                    req = json.loads(text)
                except (OSError, json.JSONDecodeError):
                    continue
                response = self.response_factory(req)
                resp_path = self.cache_dir / f"response-{req['id']}.json"
                tmp_path = resp_path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(response), encoding="utf-8")
                os.rename(tmp_path, resp_path)
                req_path.unlink(missing_ok=True)
            time.sleep(0.02)


@pytest.fixture
def fake_plugin_lua_file(tmp_path, monkeypatch):
    """Make plugin-installed detection succeed by faking the install location."""
    fake_home = tmp_path / "home"
    plugin_dir = fake_home / ".config" / "darktable" / "lua"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "darktable_mcp.lua").write_text("-- fake")
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    cache = tmp_path / "cache" / "darktable-mcp"
    cache.mkdir(parents=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return cache


def test_call_returns_result_on_happy_path(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"id": req["id"], "result": [{"id": "1", "filename": "a.NEF"}]},
    )
    plugin.start()
    try:
        bridge = Bridge()
        result = bridge.call("view_photos", {"limit": 10})
        assert result == [{"id": "1", "filename": "a.NEF"}]
    finally:
        plugin.stop()


def test_call_raises_timeout_when_no_response(cache_dir, fake_plugin_lua_file):
    bridge = Bridge()
    with pytest.raises(BridgeTimeoutError):
        bridge.call("view_photos", {}, timeout=0.5)


def test_call_raises_plugin_not_installed_when_lua_missing(cache_dir, tmp_path, monkeypatch):
    # No plugin file written.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bridge = Bridge()
    with pytest.raises(BridgePluginNotInstalledError):
        bridge.call("view_photos", {})


def test_call_raises_bridge_error_on_error_response(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"id": req["id"], "error": "image 999 not in library"},
    )
    plugin.start()
    try:
        bridge = Bridge()
        with pytest.raises(BridgeError, match="image 999 not in library"):
            bridge.call("rate_photos", {"photo_ids": ["999"], "rating": 5})
    finally:
        plugin.stop()


def test_call_raises_protocol_error_on_malformed_response(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"not_id": "x", "garbage": True},
    )
    plugin.start()
    try:
        bridge = Bridge()
        with pytest.raises(BridgeProtocolError):
            bridge.call("view_photos", {})
    finally:
        plugin.stop()


def test_error_field_wins_over_result_field(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"id": req["id"], "result": "ignored", "error": "boom"},
    )
    plugin.start()
    try:
        bridge = Bridge()
        with pytest.raises(BridgeError, match="boom"):
            bridge.call("view_photos", {})
    finally:
        plugin.stop()


def test_call_cleans_up_request_file_on_timeout(cache_dir, fake_plugin_lua_file):
    bridge = Bridge()
    with pytest.raises(BridgeTimeoutError):
        bridge.call("view_photos", {}, timeout=0.5)
    leftover = list(cache_dir.glob("request-*.json"))
    assert leftover == [], f"request file not cleaned up: {leftover}"


def test_call_cleans_up_response_file_on_success(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir, lambda req: {"id": req["id"], "result": "ok"}
    )
    plugin.start()
    try:
        bridge = Bridge()
        bridge.call("view_photos", {})
        leftover = list(cache_dir.glob("response-*.json"))
        assert leftover == [], f"response file not cleaned up: {leftover}"
    finally:
        plugin.stop()


def test_concurrent_calls_get_correct_results(cache_dir, fake_plugin_lua_file):
    # The fake plugin echoes the params back as the result so each caller can
    # verify it got its OWN response, not someone else's.
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"id": req["id"], "result": req["params"]},
    )
    plugin.start()
    try:
        bridge = Bridge()
        results = {}
        errors = []

        def make_call(label):
            try:
                results[label] = bridge.call("echo", {"label": label})
            except Exception as e:
                errors.append((label, e))

        threads = [threading.Thread(target=make_call, args=(f"thread-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], errors
        for i in range(5):
            label = f"thread-{i}"
            assert results[label] == {"label": label}
    finally:
        plugin.stop()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_bridge.py -q`

Expected: all tests fail with `ModuleNotFoundError: No module named 'darktable_mcp.bridge.client'`.

- [ ] **Step 4: Write the Bridge client**

Write `/home/andrii/projects/darktable-mcp/darktable_mcp/bridge/client.py`:

```python
"""File-based JSON request/response bridge to the darktable Lua plugin."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


class BridgeError(Exception):
    """Plugin returned an explicit error in its response."""


class BridgeTimeoutError(BridgeError):
    """No response within the configured timeout."""


class BridgePluginNotInstalledError(BridgeError):
    """The Lua plugin file is not present in the user's darktable config."""


class BridgeProtocolError(BridgeError):
    """Response file existed but did not match the expected schema."""


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "darktable-mcp"


def _plugin_path() -> Path:
    return Path.home() / ".config" / "darktable" / "lua" / "darktable_mcp.lua"


class Bridge:
    """Synchronous file-based JSON-RPC client to the darktable Lua plugin.

    Each `call` writes one request file and waits for the matching response
    file. Atomic writes via tmp+rename. Cleans up its own request file on
    timeout and its response file after read.
    """

    def __init__(self, cache_dir: Optional[Path] = None, plugin_path: Optional[Path] = None):
        self._cache_dir = cache_dir or _cache_dir()
        self._plugin_path = plugin_path or _plugin_path()

    def call(self, method: str, params: Dict[str, Any], timeout: float = 5.0) -> Any:
        if not self._plugin_path.is_file():
            raise BridgePluginNotInstalledError(
                f"plugin not installed at {self._plugin_path}. "
                "Run: darktable-mcp install-plugin"
            )

        self._cache_dir.mkdir(parents=True, exist_ok=True)

        req_id = str(uuid.uuid4())
        req_path = self._cache_dir / f"request-{req_id}.json"
        resp_path = self._cache_dir / f"response-{req_id}.json"
        payload = json.dumps({"id": req_id, "method": method, "params": params})

        # Atomic write: tmp + rename.
        tmp = req_path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.rename(tmp, req_path)

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if resp_path.exists():
                    try:
                        text = resp_path.read_text(encoding="utf-8")
                    except OSError:
                        time.sleep(0.05)
                        continue
                    try:
                        response = json.loads(text)
                    except json.JSONDecodeError as e:
                        raise BridgeProtocolError(
                            f"response not valid JSON: {e}; payload: {text!r}"
                        )
                    finally:
                        resp_path.unlink(missing_ok=True)

                    if not isinstance(response, dict) or "id" not in response:
                        raise BridgeProtocolError(
                            f"response missing id field: {response!r}"
                        )
                    if "error" in response:
                        raise BridgeError(str(response["error"]))
                    if "result" not in response:
                        raise BridgeProtocolError(
                            f"response has neither error nor result: {response!r}"
                        )
                    return response["result"]
                time.sleep(0.05)

            raise BridgeTimeoutError(
                f"no response from plugin within {timeout}s for method {method!r}"
            )
        finally:
            # Best-effort cleanup of our own request file.
            req_path.unlink(missing_ok=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/pytest tests/test_bridge.py -q`

Expected: all 9 tests pass.

- [ ] **Step 6: Run full suite — confirm acceptance pin progress**

Run: `venv/bin/pytest -q`

Expected: 116 (existing) + 9 (new bridge) + 1 acceptance pin flipped (`test_bridge_module_exports_required_symbols`) = 126 passed; 5 acceptance pins still failing.

- [ ] **Step 7: Commit**

```bash
git add darktable_mcp/bridge/__init__.py darktable_mcp/bridge/client.py tests/test_bridge.py
git commit -m "feat(bridge): file-based JSON request/response client to darktable Lua plugin"
```

---

## Task 3: Lua plugin (TDD via lua subprocess)

**Files:**
- Create: `darktable_mcp/lua/darktable_mcp.lua`
- Create: `tests/lua/test_dispatcher.lua`
- Create: `tests/test_lua_dispatcher.py`

The dispatcher logic (method registry, request parsing, response shape, file I/O) is testable in plain Lua with a stub `dt` table. The worker loop / `dt.control.dispatch` / `dt.control.sleep` are darktable-runtime concerns covered by Task 0 spike + Task 6 manual smoke test, not by these unit tests.

**API NOTE FROM TASK 0 SPIKE:** The spec originally named the primitive `dt.control.async`, but the empirical spike confirmed the actual symbol in darktable 5.4.1 is `dt.control.dispatch`. Use `dt.control.dispatch` in the plugin code below. If you encounter `dt.control.async` anywhere else in the spec or older drafts, mentally substitute `dispatch` — they refer to the same fire-and-forget non-blocking-worker primitive.

- [ ] **Step 1: Write the failing Lua tests**

Write `/home/andrii/projects/darktable-mcp/tests/lua/test_dispatcher.lua`:

```lua
-- Lua dispatcher unit tests. Stubs the darktable `dt` global with a fake
-- database and verifies the method registry + scan_dir behavior in isolation.
--
-- Run via: lua tests/lua/test_dispatcher.lua
-- Exits 0 on success, non-zero on first failure.

-- ---- Test harness ----------------------------------------------------------
local failures = {}
local function assertEq(actual, expected, label)
  if actual ~= expected then
    table.insert(failures, string.format("%s: expected %s, got %s",
      label, tostring(expected), tostring(actual)))
  end
end
local function assertTrue(cond, label)
  if not cond then
    table.insert(failures, string.format("%s: expected truthy, got falsy", label))
  end
end

-- ---- Stub dt table ---------------------------------------------------------
local stub_images = {
  [1] = {id = 1, filename = "DSC_0001.NEF", path = "/photos", rating = 5},
  [2] = {id = 2, filename = "DSC_0002.NEF", path = "/photos", rating = 3},
  [3] = {id = 3, filename = "OTHER.NEF",   path = "/photos", rating = 4},
}
-- dt.database supports: ipairs() iteration AND [id] subscript access.
local stub_db = {}
for _, img in pairs(stub_images) do table.insert(stub_db, img) end
setmetatable(stub_db, {__index = stub_images})

local dt_log = {}
_G.darktable = {
  database = stub_db,
  print_log = function(msg) table.insert(dt_log, msg) end,
  control = {
    async = function(_) end,    -- no-op for unit tests
    sleep = function(_) end,    -- no-op for unit tests
  },
}

-- ---- Load the plugin (requires it expose internals via a return) -----------
-- The plugin file at the end returns an internals table for testing:
--   return {handle = handle, scan_dir = scan_dir, methods = methods}
-- So we can require it without triggering the worker.
package.path = package.path .. ";./darktable_mcp/lua/?.lua"
local internals = require("darktable_mcp")

-- ---- methods.view_photos ---------------------------------------------------
do
  local result = internals.methods.view_photos({rating_min = 4, limit = 10})
  assertEq(#result, 2, "view_photos rating_min=4 returns 2 images")
  assertEq(result[1].rating, 5, "view_photos result[1].rating")
  assertTrue(result[1].id == "1" or result[1].id == "3",
    "view_photos returns string-id 1 or 3")
end

do
  local result = internals.methods.view_photos({filter = "OTHER", limit = 10})
  assertEq(#result, 1, "view_photos filter=OTHER returns 1 image")
  assertEq(result[1].filename, "OTHER.NEF", "view_photos filter result filename")
end

do
  local result = internals.methods.view_photos({limit = 2})
  assertEq(#result, 2, "view_photos limit=2 caps at 2")
end

-- ---- methods.rate_photos ---------------------------------------------------
do
  local result = internals.methods.rate_photos({photo_ids = {"1", "2"}, rating = 1})
  assertEq(result.updated, 2, "rate_photos updated count")
  assertEq(stub_images[1].rating, 1, "rate_photos changed image 1 rating")
  assertEq(stub_images[2].rating, 1, "rate_photos changed image 2 rating")
end

-- ---- handle: known method --------------------------------------------------
do
  local resp = internals.handle({id = "abc", method = "view_photos", params = {limit = 1}})
  assertEq(resp.id, "abc", "handle preserves id")
  assertTrue(resp.result ~= nil, "handle known method returns result")
  assertTrue(resp.error == nil, "handle known method has no error")
end

-- ---- handle: unknown method ------------------------------------------------
do
  local resp = internals.handle({id = "xyz", method = "bogus", params = {}})
  assertEq(resp.id, "xyz", "handle preserves id on error")
  assertTrue(resp.error ~= nil, "handle unknown method returns error")
  assertTrue(string.find(resp.error, "bogus"), "error message names the method")
end

-- ---- scan_dir: full request/response round-trip ----------------------------
do
  local tmpdir = os.getenv("TMPDIR") or "/tmp"
  local test_dir = tmpdir .. "/darktable-mcp-lua-test-" .. tostring(os.time())
  os.execute("mkdir -p " .. test_dir)

  -- Reset stub state.
  stub_images[1].rating = 5
  stub_images[2].rating = 3

  -- Write a request file.
  local req_path = test_dir .. "/request-test001.json"
  local f = io.open(req_path, "w")
  f:write('{"id":"test001","method":"view_photos","params":{"limit":1}}')
  f:close()

  internals.scan_dir(test_dir)

  -- Verify request file was deleted.
  local req_check = io.open(req_path, "r")
  assertTrue(req_check == nil, "scan_dir deletes request file after processing")
  if req_check then req_check:close() end

  -- Verify response file appeared with correct content.
  local resp_path = test_dir .. "/response-test001.json"
  local resp_f = io.open(resp_path, "r")
  assertTrue(resp_f ~= nil, "scan_dir wrote response file")
  if resp_f then
    local content = resp_f:read("*a")
    resp_f:close()
    assertTrue(string.find(content, "test001"), "response contains request id")
    assertTrue(string.find(content, "result"), "response contains result field")
  end

  os.execute("rm -rf " .. test_dir)
end

-- ---- Report ----------------------------------------------------------------
if #failures > 0 then
  io.stderr:write("FAILED:\n")
  for _, msg in ipairs(failures) do
    io.stderr:write("  " .. msg .. "\n")
  end
  os.exit(1)
end
print("OK: all dispatcher tests passed")
os.exit(0)
```

- [ ] **Step 2: Write the pytest wrapper**

Write `/home/andrii/projects/darktable-mcp/tests/test_lua_dispatcher.py`:

```python
"""Pytest wrapper that runs the Lua dispatcher tests via the system lua interpreter."""

import shutil
import subprocess
from pathlib import Path

import pytest

LUA_AVAILABLE = shutil.which("lua") is not None
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not LUA_AVAILABLE, reason="lua interpreter not installed")
def test_lua_dispatcher():
    result = subprocess.run(
        ["lua", "tests/lua/test_dispatcher.lua"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"Lua tests failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_lua_dispatcher.py -v`

Expected: FAIL because `darktable_mcp/lua/darktable_mcp.lua` doesn't exist yet (`require("darktable_mcp")` errors). If `lua` is not installed, the test SKIPS — note the skip and ensure the implementer installs lua before proceeding (e.g. `apt install lua5.4`).

- [ ] **Step 4: Write the Lua plugin**

Write `/home/andrii/projects/darktable-mcp/darktable_mcp/lua/darktable_mcp.lua`:

```lua
-- darktable_mcp: long-running plugin that exposes view_photos and
-- rate_photos to the Python MCP server via file-based JSON requests.
--
-- Loaded via `require "darktable_mcp"` from ~/.config/darktable/luarc.
-- Spawns a worker coroutine that polls ~/.cache/darktable-mcp/ every
-- ~100ms for request-*.json files, dispatches them to the method registry,
-- and writes response-<uuid>.json. See:
--   docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md

local dt = require("darktable")

-- ---- JSON encode/decode (minimal, MVP-only) --------------------------------
-- darktable Lua does not bundle a JSON library reliably. Inline a tiny
-- encoder/decoder sufficient for our request/response shapes (objects,
-- arrays, strings, numbers, booleans, null). For anything more exotic,
-- consider vendoring a real library; for the MVP this is enough.

local json = {}

local function encode_value(v)
  local t = type(v)
  if t == "nil" then return "null"
  elseif t == "boolean" then return v and "true" or "false"
  elseif t == "number" then return tostring(v)
  elseif t == "string" then
    return '"' .. v:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t') .. '"'
  elseif t == "table" then
    -- Detect array vs object by checking for sequential integer keys.
    local n, max = 0, 0
    for k in pairs(v) do
      n = n + 1
      if type(k) == "number" and k > max then max = k end
    end
    if n == max and n > 0 then
      local parts = {}
      for i = 1, n do parts[i] = encode_value(v[i]) end
      return "[" .. table.concat(parts, ",") .. "]"
    elseif n == 0 then
      return "[]"
    else
      local parts = {}
      for k, val in pairs(v) do
        table.insert(parts, encode_value(tostring(k)) .. ":" .. encode_value(val))
      end
      return "{" .. table.concat(parts, ",") .. "}"
    end
  end
  error("cannot encode value of type " .. t)
end

function json.encode(v) return encode_value(v) end

-- Minimal recursive-descent decoder. Adequate for plain JSON requests.
local function skip_ws(s, i)
  while i <= #s and (s:sub(i,i) == " " or s:sub(i,i) == "\t" or s:sub(i,i) == "\n" or s:sub(i,i) == "\r") do
    i = i + 1
  end
  return i
end

local decode_value
local function decode_string(s, i)
  assert(s:sub(i,i) == '"', "expected string at " .. i)
  i = i + 1
  local out = {}
  while i <= #s do
    local c = s:sub(i,i)
    if c == '"' then return table.concat(out), i + 1
    elseif c == "\\" then
      local esc = s:sub(i+1, i+1)
      if esc == "n" then table.insert(out, "\n")
      elseif esc == "r" then table.insert(out, "\r")
      elseif esc == "t" then table.insert(out, "\t")
      elseif esc == '"' or esc == "\\" or esc == "/" then table.insert(out, esc)
      else error("unsupported escape \\" .. esc)
      end
      i = i + 2
    else
      table.insert(out, c)
      i = i + 1
    end
  end
  error("unterminated string")
end

local function decode_number(s, i)
  local start = i
  if s:sub(i,i) == "-" then i = i + 1 end
  while i <= #s and s:sub(i,i):match("[%d%.eE%-%+]") do i = i + 1 end
  return tonumber(s:sub(start, i-1)), i
end

local function decode_array(s, i)
  assert(s:sub(i,i) == "[")
  i = i + 1
  i = skip_ws(s, i)
  local out = {}
  if s:sub(i,i) == "]" then return out, i + 1 end
  while true do
    local v
    v, i = decode_value(s, i)
    table.insert(out, v)
    i = skip_ws(s, i)
    local c = s:sub(i,i)
    if c == "," then i = i + 1; i = skip_ws(s, i)
    elseif c == "]" then return out, i + 1
    else error("expected , or ] at " .. i)
    end
  end
end

local function decode_object(s, i)
  assert(s:sub(i,i) == "{")
  i = i + 1
  i = skip_ws(s, i)
  local out = {}
  if s:sub(i,i) == "}" then return out, i + 1 end
  while true do
    local k
    k, i = decode_string(s, i)
    i = skip_ws(s, i)
    assert(s:sub(i,i) == ":", "expected : at " .. i)
    i = skip_ws(s, i + 1)
    local v
    v, i = decode_value(s, i)
    out[k] = v
    i = skip_ws(s, i)
    local c = s:sub(i,i)
    if c == "," then i = i + 1; i = skip_ws(s, i)
    elseif c == "}" then return out, i + 1
    else error("expected , or } at " .. i)
    end
  end
end

decode_value = function(s, i)
  i = skip_ws(s, i)
  local c = s:sub(i,i)
  if c == "{" then return decode_object(s, i)
  elseif c == "[" then return decode_array(s, i)
  elseif c == '"' then return decode_string(s, i)
  elseif c == "t" and s:sub(i, i+3) == "true" then return true, i + 4
  elseif c == "f" and s:sub(i, i+4) == "false" then return false, i + 5
  elseif c == "n" and s:sub(i, i+3) == "null" then return nil, i + 4
  else return decode_number(s, i)
  end
end

function json.decode(s)
  local v = decode_value(s, 1)
  return v
end

-- ---- Method registry -------------------------------------------------------

local methods = {}

methods.view_photos = function(p)
  p = p or {}
  local out, count = {}, 0
  local limit = p.limit or 100
  local filter = p.filter or ""
  local rating_min = p.rating_min
  for _, image in ipairs(dt.database) do
    if count >= limit then break end
    local include = true
    if rating_min and (image.rating or 0) < rating_min then include = false end
    if include and filter ~= "" then
      local ok = string.find(string.lower(image.filename), string.lower(filter), 1, true)
      if not ok then include = false end
    end
    if include then
      table.insert(out, {
        id = tostring(image.id),
        filename = image.filename,
        path = image.path,
        rating = image.rating or 0,
      })
      count = count + 1
    end
  end
  return out
end

methods.rate_photos = function(p)
  p = p or {}
  local updated = 0
  for _, photo_id in ipairs(p.photo_ids or {}) do
    local image = dt.database[tonumber(photo_id)]
    if image then
      image.rating = p.rating
      updated = updated + 1
    end
  end
  return {updated = updated}
end

-- ---- Dispatch --------------------------------------------------------------

local function handle(req)
  local fn = methods[req.method]
  if not fn then
    return {id = req.id, error = "unknown method: " .. tostring(req.method)}
  end
  local ok, result_or_err = pcall(fn, req.params)
  if not ok then
    return {id = req.id, error = "handler raised: " .. tostring(result_or_err)}
  end
  return {id = req.id, result = result_or_err}
end

-- ---- File I/O --------------------------------------------------------------

local function cache_dir()
  local base = os.getenv("XDG_CACHE_HOME")
  if not base or base == "" then
    base = os.getenv("HOME") .. "/.cache"
  end
  return base .. "/darktable-mcp"
end

local function list_request_files(dir)
  local out = {}
  local p = io.popen('ls -1 "' .. dir .. '"/request-*.json 2>/dev/null')
  if not p then return out end
  for line in p:lines() do
    if not line:match("%.tmp$") then table.insert(out, line) end
  end
  p:close()
  return out
end

local function read_file(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local content = f:read("*a")
  f:close()
  return content
end

local function write_file_atomic(path, content)
  local tmp = path .. ".tmp"
  local f = io.open(tmp, "w")
  if not f then return false end
  f:write(content)
  f:close()
  return os.rename(tmp, path)
end

local function scan_dir(dir)
  for _, req_path in ipairs(list_request_files(dir)) do
    local content = read_file(req_path)
    if content then
      local ok, req = pcall(json.decode, content)
      if ok and type(req) == "table" and req.id then
        local resp = handle(req)
        local resp_path = dir .. "/response-" .. req.id .. ".json"
        write_file_atomic(resp_path, json.encode(resp))
      end
      os.remove(req_path)
    end
  end
end

local function sweep_stale(dir, max_age_seconds)
  -- Simple approach: shell out. find prints paths older than N minutes;
  -- we use seconds via -mmin with arithmetic. For the MVP, 60s = 1min.
  local minutes = math.max(1, math.floor(max_age_seconds / 60))
  os.execute(string.format(
    'find "%s" -maxdepth 1 -name "request-*.json" -mmin +%d -delete 2>/dev/null',
    dir, minutes))
end

-- ---- Worker loop -----------------------------------------------------------

local function worker_loop()
  local dir = cache_dir()
  os.execute('mkdir -p "' .. dir .. '"')
  local tick = 0
  while true do
    scan_dir(dir)
    tick = tick + 1
    if tick % 100 == 0 then
      sweep_stale(dir, 60)
    end
    if dt.control and dt.control.sleep then
      dt.control.sleep(100)
    else
      -- Should never reach here in real darktable; tests pass a no-op stub.
      break
    end
  end
end

-- ---- Entry point -----------------------------------------------------------

dt.print_log("darktable-mcp bridge: ready")
if dt.control and dt.control.dispatch then
  dt.control.dispatch(worker_loop)
end

-- ---- Test exports (used by tests/lua/test_dispatcher.lua) ------------------
return {
  handle = handle,
  scan_dir = scan_dir,
  methods = methods,
  json = json,
}
```

- [ ] **Step 5: Run Lua tests to verify they pass**

Run: `venv/bin/pytest tests/test_lua_dispatcher.py -v`

Expected: PASS. If `lua` is not installed, install it (`apt install lua5.4` or similar) and re-run.

- [ ] **Step 6: Run full suite — confirm acceptance pin progress**

Run: `venv/bin/pytest -q`

Expected: 126 (post-Task-2) + 1 (lua dispatcher test) + 1 acceptance pin flipped (`test_lua_plugin_file_ships_with_package`) = 128 passed; 4 acceptance pins still failing.

- [ ] **Step 7: Commit**

```bash
git add darktable_mcp/lua/darktable_mcp.lua tests/lua/test_dispatcher.lua tests/test_lua_dispatcher.py
git commit -m "feat(lua): plugin with view_photos + rate_photos dispatcher and worker loop"
```

---

## Task 4: Wire MCP handlers

**Files:**
- Modify: `darktable_mcp/server.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_honesty_pass_acceptance.py`

- [ ] **Step 1: Read `server.py` to confirm shape**

Run: `grep -n "_handler_map\|_tool_definitions\|_handle_\|self.bridge\|self._cli" darktable_mcp/server.py`

Confirm the current shape: `_tool_definitions()` returns a list of `Tool(...)` objects; `_handler_map` is a dict built once in `__init__`.

- [ ] **Step 2: Write the failing handler tests**

Add to `/home/andrii/projects/darktable-mcp/tests/test_server.py` (append to the file, after the existing tests):

```python
@pytest.mark.asyncio
async def test_handle_view_photos_returns_formatted_list():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = [
        {"id": "1", "filename": "a.NEF", "path": "/p", "rating": 5},
        {"id": "2", "filename": "b.NEF", "path": "/p", "rating": 4},
    ]
    result = await server._handle_view_photos({"filter": "", "limit": 10})
    assert len(result) == 1
    text = result[0].text
    assert "a.NEF" in text
    assert "b.NEF" in text
    server.bridge.call.assert_called_once_with(
        "view_photos", {"filter": "", "limit": 10}
    )


@pytest.mark.asyncio
async def test_handle_view_photos_no_results_message():
    from darktable_mcp.bridge.client import BridgePluginNotInstalledError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = []
    result = await server._handle_view_photos({})
    assert "No photos" in result[0].text


@pytest.mark.asyncio
async def test_handle_view_photos_friendly_message_when_plugin_missing():
    from darktable_mcp.bridge.client import BridgePluginNotInstalledError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgePluginNotInstalledError("missing")
    result = await server._handle_view_photos({})
    assert "install-plugin" in result[0].text


@pytest.mark.asyncio
async def test_handle_view_photos_friendly_message_when_dt_not_running():
    from darktable_mcp.bridge.client import BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")
    result = await server._handle_view_photos({})
    assert "darktable" in result[0].text.lower()
    assert "open" in result[0].text.lower() or "running" in result[0].text.lower()


@pytest.mark.asyncio
async def test_handle_rate_photos_returns_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"updated": 3}
    result = await server._handle_rate_photos({"photo_ids": ["1", "2", "3"], "rating": 4})
    assert "3" in result[0].text
    assert "4" in result[0].text
    server.bridge.call.assert_called_once_with(
        "rate_photos", {"photo_ids": ["1", "2", "3"], "rating": 4}
    )
```

If `Mock` isn't already imported at the top of `test_server.py`, ensure `from unittest.mock import AsyncMock, Mock, patch` is present (it should be from prior tests).

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_server.py -q -k "view_photos or rate_photos"`

Expected: FAIL because `_handle_view_photos` / `_handle_rate_photos` don't exist yet.

- [ ] **Step 4: Wire the bridge and handlers in `server.py`**

Edit `/home/andrii/projects/darktable-mcp/darktable_mcp/server.py`:

1. Add import near the existing imports:
   ```python
   from .bridge.client import (
       Bridge,
       BridgeError,
       BridgePluginNotInstalledError,
       BridgeTimeoutError,
   )
   ```

2. In `DarktableMCPServer.__init__`, after `self.camera_tools = CameraTools()`, add:
   ```python
   self.bridge = Bridge()
   ```
   The bridge is stateless (no init-time I/O, no subprocess), like CameraTools.

3. In `_tool_definitions()`, add two new `Tool(...)` blocks. Place them at the start of the list (before `import_from_camera`) so they read first in the MCP advertisement:
   ```python
   Tool(
       name="view_photos",
       description=(
           "Browse photos in the user's darktable library. Filter by "
           "filename substring, minimum star rating, or both. Returns "
           "id, filename, path, and rating per match. Requires darktable "
           "to be running with the darktable-mcp Lua plugin installed "
           "(see darktable-mcp install-plugin)."
       ),
       inputSchema={
           "type": "object",
           "properties": {
               "filter": {
                   "type": "string",
                   "description": "Substring filter on filename (case-insensitive)",
               },
               "rating_min": {
                   "type": "integer",
                   "minimum": -1,
                   "maximum": 5,
                   "description": "Minimum star rating to include",
               },
               "limit": {
                   "type": "integer",
                   "minimum": 1,
                   "maximum": 1000,
                   "default": 100,
                   "description": "Maximum number of photos to return",
               },
           },
       },
   ),
   Tool(
       name="rate_photos",
       description=(
           "Apply a star rating to one or more photos in the user's "
           "darktable library. Requires darktable to be running with "
           "the darktable-mcp Lua plugin installed."
       ),
       inputSchema={
           "type": "object",
           "properties": {
               "photo_ids": {
                   "type": "array",
                   "items": {"type": "string"},
                   "description": "List of photo IDs (from view_photos)",
               },
               "rating": {
                   "type": "integer",
                   "minimum": -1,
                   "maximum": 5,
                   "description": "Star rating: -1=reject, 0=unrated, 1-5=stars",
               },
           },
           "required": ["photo_ids", "rating"],
       },
   ),
   ```

4. Add the two handler methods (place near other `_handle_*` methods):
   ```python
   async def _handle_view_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
       try:
           photos = self.bridge.call("view_photos", arguments)
       except BridgePluginNotInstalledError:
           return [TextContent(
               type="text",
               text="darktable-mcp plugin not installed. Run: darktable-mcp install-plugin",
           )]
       except BridgeTimeoutError:
           return [TextContent(
               type="text",
               text="darktable not running, or plugin not loaded. Open darktable and try again.",
           )]
       except BridgeError as e:
           return [TextContent(type="text", text=f"Plugin error: {e}")]

       if not photos:
           return [TextContent(type="text", text="No photos found matching criteria")]
       lines = [f"Found {len(photos)} photos:"]
       for p in photos:
           stars = "⭐" * (p.get("rating") or 0)
           lines.append(f"ID: {p['id']} | {p['filename']} | Rating: {stars}")
       return [TextContent(type="text", text="\n".join(lines))]

   async def _handle_rate_photos(self, arguments: Dict[str, Any]) -> List[TextContent]:
       try:
           result = self.bridge.call("rate_photos", arguments)
       except BridgePluginNotInstalledError:
           return [TextContent(
               type="text",
               text="darktable-mcp plugin not installed. Run: darktable-mcp install-plugin",
           )]
       except BridgeTimeoutError:
           return [TextContent(
               type="text",
               text="darktable not running, or plugin not loaded. Open darktable and try again.",
           )]
       except BridgeError as e:
           return [TextContent(type="text", text=f"Plugin error: {e}")]

       updated = result.get("updated", 0)
       return [TextContent(
           type="text",
           text=f"Updated {updated} photos with {arguments.get('rating')} stars",
       )]
   ```

5. Add to `_handler_map` (in `_build_handlers`):
   ```python
   "view_photos": self._handle_view_photos,
   "rate_photos": self._handle_rate_photos,
   ```

- [ ] **Step 5: Update the iteration-1 acceptance pin**

Edit `/home/andrii/projects/darktable-mcp/tests/test_honesty_pass_acceptance.py`:

```python
EXPECTED_TOOLS = {
    "import_from_camera",
    "export_images",
    "extract_previews",
    "apply_ratings_batch",
    "open_in_darktable",
    "view_photos",
    "rate_photos",
}
```

The pin's purpose ("the registered set is exactly this list") is preserved — the list is just longer now. Add a one-line note at the top of the docstring or above the constant:

```python
# Updated 2026-04-27 (iteration 2): added view_photos + rate_photos restored via IPC bridge.
```

- [ ] **Step 6: Run the suite**

Run: `venv/bin/pytest -q`

Expected: 128 (post-Task-3) + 5 (new server handler tests) + 2 acceptance pins flipped (`test_view_photos_registered`, `test_rate_photos_registered`) + 0 broken (the iteration-1 pin update keeps that test green) = 133 passed; 2 acceptance pins still failing (the install/uninstall subcommand tests).

- [ ] **Step 7: Commit**

```bash
git add darktable_mcp/server.py tests/test_server.py tests/test_honesty_pass_acceptance.py
git commit -m "feat(server): wire view_photos and rate_photos through the bridge"
```

---

## Task 5: install-plugin / uninstall-plugin CLI subcommands

**Files:**
- Create: `darktable_mcp/cli/__init__.py`
- Create: `darktable_mcp/cli/install_plugin.py`
- Create: `darktable_mcp/__main__.py` (so `python -m darktable_mcp` works)
- Modify: `darktable_mcp/__init__.py` (argparse-dispatch in `main()`)
- Modify: `pyproject.toml` (add Lua file to package data)
- Create: `tests/test_install_plugin.py`

- [ ] **Step 1: Write the package marker**

Write `/home/andrii/projects/darktable-mcp/darktable_mcp/cli/__init__.py`:

```python
"""CLI subcommands for darktable-mcp."""
```

- [ ] **Step 2: Write the failing tests**

Write `/home/andrii/projects/darktable-mcp/tests/test_install_plugin.py`:

```python
"""Tests for darktable_mcp.cli.install_plugin install/uninstall functions."""

from pathlib import Path

import pytest

from darktable_mcp.cli.install_plugin import install, uninstall


def test_install_writes_plugin_file_and_luarc(tmp_path):
    home = tmp_path
    install(home)
    plugin = home / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    luarc = home / ".config" / "darktable" / "luarc"
    assert plugin.is_file()
    assert "view_photos" in plugin.read_text()
    assert luarc.is_file()
    assert 'require "darktable_mcp"' in luarc.read_text()


def test_install_is_idempotent(tmp_path):
    install(tmp_path)
    install(tmp_path)  # second run should not duplicate the require line
    luarc_text = (tmp_path / ".config" / "darktable" / "luarc").read_text()
    assert luarc_text.count('require "darktable_mcp"') == 1


def test_install_preserves_existing_luarc_lines(tmp_path):
    luarc = tmp_path / ".config" / "darktable"
    luarc.mkdir(parents=True)
    (luarc / "luarc").write_text('-- user comment\nrequire "other_plugin"\n')
    install(tmp_path)
    text = (luarc / "luarc").read_text()
    assert "-- user comment" in text
    assert 'require "other_plugin"' in text
    assert 'require "darktable_mcp"' in text


def test_install_creates_luarc_if_missing(tmp_path):
    install(tmp_path)
    luarc = tmp_path / ".config" / "darktable" / "luarc"
    assert luarc.is_file()
    assert luarc.read_text().strip() == 'require "darktable_mcp"'


def test_uninstall_removes_plugin_file_and_require_line(tmp_path):
    install(tmp_path)
    uninstall(tmp_path)
    plugin = tmp_path / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    luarc = tmp_path / ".config" / "darktable" / "luarc"
    assert not plugin.exists()
    assert luarc.is_file()
    assert 'require "darktable_mcp"' not in luarc.read_text()


def test_uninstall_preserves_other_luarc_lines(tmp_path):
    luarc_path = tmp_path / ".config" / "darktable" / "luarc"
    luarc_path.parent.mkdir(parents=True)
    luarc_path.write_text('-- comment\nrequire "other"\nrequire "darktable_mcp"\n')
    (tmp_path / ".config" / "darktable" / "lua").mkdir()
    (tmp_path / ".config" / "darktable" / "lua" / "darktable_mcp.lua").write_text("-- fake")
    uninstall(tmp_path)
    text = luarc_path.read_text()
    assert "-- comment" in text
    assert 'require "other"' in text
    assert 'require "darktable_mcp"' not in text


def test_uninstall_when_not_installed_is_noop(tmp_path):
    # No prior install. Should not raise.
    uninstall(tmp_path)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_install_plugin.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'darktable_mcp.cli'`.

- [ ] **Step 4: Write the install/uninstall implementation**

Write `/home/andrii/projects/darktable-mcp/darktable_mcp/cli/install_plugin.py`:

```python
"""install-plugin / uninstall-plugin subcommands.

Library functions take an explicit `home_dir` for testability;
CLI entry points pass `Path.home()`.
"""

from __future__ import annotations

import argparse
import importlib.resources
import sys
from pathlib import Path

REQUIRE_LINE = 'require "darktable_mcp"'


def _plugin_dir(home: Path) -> Path:
    return home / ".config" / "darktable" / "lua"


def _luarc_path(home: Path) -> Path:
    return home / ".config" / "darktable" / "luarc"


def _packaged_lua_bytes() -> bytes:
    return (
        importlib.resources.files("darktable_mcp")
        .joinpath("lua/darktable_mcp.lua")
        .read_bytes()
    )


def install(home: Path) -> None:
    plugin_dir = _plugin_dir(home)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    plugin_file = plugin_dir / "darktable_mcp.lua"
    plugin_file.write_bytes(_packaged_lua_bytes())

    luarc = _luarc_path(home)
    if luarc.exists():
        text = luarc.read_text(encoding="utf-8")
    else:
        text = ""
    if REQUIRE_LINE not in text:
        if text and not text.endswith("\n"):
            text += "\n"
        text += REQUIRE_LINE + "\n"
        luarc.parent.mkdir(parents=True, exist_ok=True)
        luarc.write_text(text, encoding="utf-8")


def uninstall(home: Path) -> None:
    plugin_file = _plugin_dir(home) / "darktable_mcp.lua"
    if plugin_file.exists():
        plugin_file.unlink()

    luarc = _luarc_path(home)
    if luarc.exists():
        lines = luarc.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [ln for ln in lines if ln.strip() != REQUIRE_LINE]
        luarc.write_text("".join(kept), encoding="utf-8")


def install_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="darktable-mcp install-plugin",
        description="Install the darktable_mcp Lua plugin into the user's darktable config.",
    )
    parser.parse_args(argv)
    home = Path.home()
    install(home)
    print(f"✓ wrote {_plugin_dir(home) / 'darktable_mcp.lua'}")
    print(f"✓ ensured 'require \"darktable_mcp\"' in {_luarc_path(home)}")
    print("Restart darktable to load the plugin.")
    return 0


def uninstall_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="darktable-mcp uninstall-plugin",
        description="Remove the darktable_mcp Lua plugin from the user's darktable config.",
    )
    parser.parse_args(argv)
    home = Path.home()
    uninstall(home)
    print(f"✓ removed {_plugin_dir(home) / 'darktable_mcp.lua'}")
    print(f"✓ removed 'require \"darktable_mcp\"' from {_luarc_path(home)}")
    return 0
```

- [ ] **Step 5: Run install tests to verify they pass**

Run: `venv/bin/pytest tests/test_install_plugin.py -q`

Expected: all 7 tests pass.

- [ ] **Step 6: Read current `darktable_mcp/__init__.py` to confirm `main()` shape**

Run: `cat darktable_mcp/__init__.py`

You should see a `main()` function that runs the MCP server. Confirm its current behavior so the dispatcher wraps it correctly.

- [ ] **Step 7: Wire the subcommand dispatcher in `darktable_mcp/__init__.py`**

The current `main()` body uses `argparse` and runs the server. Refactor: split the server-running body into a private helper `_run_server()`, then make `main()` dispatch on the first positional arg before falling through to the server. Concrete replacement for the file:

```python
"""Darktable MCP Server - Model Context Protocol server for darktable."""

import asyncio
import logging
import sys

from .server import DarktableMCPServer

__version__ = "0.1.0"
__author__ = "w1ne"
__email__ = "14119286+w1ne@users.noreply.github.com"

logger = logging.getLogger(__name__)


def _run_server() -> None:
    """Run the MCP server over stdio (default subcommand / no-args behavior)."""
    import argparse

    parser = argparse.ArgumentParser(description="Darktable MCP Server (stdio)")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        asyncio.run(DarktableMCPServer().run())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error("Server error: %s", e)
        sys.exit(1)


def main() -> None:
    """Entry point: run MCP server (default) or dispatch to a subcommand."""
    args = sys.argv[1:]
    if args and args[0] == "install-plugin":
        from .cli.install_plugin import install_main
        sys.exit(install_main(args[1:]))
    if args and args[0] == "uninstall-plugin":
        from .cli.install_plugin import uninstall_main
        sys.exit(uninstall_main(args[1:]))
    _run_server()


if __name__ == "__main__":
    main()
```

Note: the existing `main()` uses `DarktableMCPServer().run()` not `.start()` — keep that as-is. The dispatcher only intercepts the two new subcommand strings; everything else flows to `_run_server()` unchanged.

- [ ] **Step 8: Add `__main__.py` so `python -m darktable_mcp` works**

Write `/home/andrii/projects/darktable-mcp/darktable_mcp/__main__.py`:

```python
from . import main

main()
```

This is what the iteration-1 acceptance pin uses (`python -m darktable_mcp install-plugin --help`).

- [ ] **Step 9: Update `pyproject.toml` so the Lua file ships in the wheel**

Edit `/home/andrii/projects/darktable-mcp/pyproject.toml`. Find the `[tool.setuptools.packages.find]` section. Add a sibling section:

```toml
[tool.setuptools.package-data]
darktable_mcp = ["lua/*.lua"]
```

If a `[tool.setuptools.package-data]` already exists, merge the entry.

- [ ] **Step 10: Verify the package data ships**

Run:
```bash
venv/bin/pip install -e . --quiet
venv/bin/python -c "import importlib.resources; print(importlib.resources.files('darktable_mcp').joinpath('lua/darktable_mcp.lua').is_file())"
```

Expected: `True`.

- [ ] **Step 11: Run the full suite**

Run: `venv/bin/pytest -q`

Expected: 133 (post-Task-4) + 7 (new install tests) + 2 acceptance pins flipped (`test_install_plugin_subcommand_runs`, `test_uninstall_plugin_subcommand_runs`) = 142 passed; 0 acceptance pins failing.

- [ ] **Step 12: Commit**

```bash
git add darktable_mcp/cli/__init__.py darktable_mcp/cli/install_plugin.py darktable_mcp/__main__.py darktable_mcp/__init__.py pyproject.toml tests/test_install_plugin.py
git commit -m "feat(cli): install-plugin / uninstall-plugin subcommands"
```

---

## Task 6: Manual end-to-end smoke test + findings note

**Files:**
- Create: `examples/smoke_test_bridge.py`
- Modify: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-spike-findings.md` (append the smoke-test results)

This is a manual task. The executor performs it on the maintainer's machine and records observations.

- [ ] **Step 1: Write a standalone smoke-test script**

Write `/home/andrii/projects/darktable-mcp/examples/smoke_test_bridge.py`:

```python
"""Standalone smoke test — exercise the bridge without Claude Desktop.

Usage: venv/bin/python examples/smoke_test_bridge.py [<image_id>]

Pre-requisite: `darktable-mcp install-plugin` has been run, darktable is
open, and the user's library has at least one image. If you pass an
image_id, that image gets re-rated to 5 stars; without it, only the read
path is exercised.
"""

import sys

from darktable_mcp.bridge.client import (
    Bridge,
    BridgeError,
    BridgePluginNotInstalledError,
    BridgeTimeoutError,
)


def main():
    bridge = Bridge()
    try:
        photos = bridge.call("view_photos", {"limit": 5}, timeout=10.0)
    except BridgePluginNotInstalledError:
        print("FAIL: plugin not installed. Run: darktable-mcp install-plugin")
        sys.exit(1)
    except BridgeTimeoutError:
        print("FAIL: timeout. Is darktable open?")
        sys.exit(1)
    except BridgeError as e:
        print(f"FAIL: bridge error: {e}")
        sys.exit(1)

    print(f"OK: view_photos returned {len(photos)} images")
    for p in photos:
        print(f"  id={p['id']} filename={p['filename']} rating={p['rating']}")

    if len(sys.argv) > 1:
        target_id = sys.argv[1]
        print(f"\nRating image {target_id} as 5 stars...")
        try:
            result = bridge.call(
                "rate_photos",
                {"photo_ids": [target_id], "rating": 5},
                timeout=10.0,
            )
        except BridgeError as e:
            print(f"FAIL: {e}")
            sys.exit(1)
        print(f"OK: rate_photos returned {result}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Manual execution**

Procedure (executor follows step by step on the maintainer's machine):

1. Build + install: `venv/bin/pip install -e .` (re-run if Task 5 hasn't done this).
2. Install the plugin: `venv/bin/darktable-mcp install-plugin`.
3. Open darktable. Confirm the log shows `darktable-mcp bridge: ready` (check via `journalctl --user -f -n 100 | grep darktable` while opening, OR run `darktable -d lua 2>&1 | head -20` in another shell).
4. Verify the user's library has at least one image (or import a test image first).
5. Run the smoke test (read path only): `venv/bin/python examples/smoke_test_bridge.py`. Expected: prints "OK: view_photos returned N images" with one line per image.
6. Pick one of the returned image IDs (e.g. the lowest one). Run the smoke test (read + write): `venv/bin/python examples/smoke_test_bridge.py <id>`. Expected: read line + "OK: rate_photos returned {'updated': 1}".
7. Switch to the darktable lighttable; verify the chosen image's rating is now 5 stars in the GUI.
8. Quit darktable. Re-run the smoke test (no args). Expected: "FAIL: timeout. Is darktable open?" — confirms the friendly error path.
9. Cleanup: re-open darktable; reset the rating to its original value via the lighttable (or via another `rate_photos` call); quit.

- [ ] **Step 3: Append findings to the spike findings doc**

Append a new section to `/home/andrii/projects/darktable-mcp/docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-spike-findings.md`:

```markdown

## Task 6 Smoke Test

**Date:** <YYYY-MM-DD>
**darktable version:** <output of `darktable --version | head -1`>
**Library size:** <approximate image count>

### Verdict

PASS / FAIL / PARTIAL

### Observations

- view_photos round-trip latency: <approximate ms>
- rate_photos round-trip latency: <approximate ms>
- Rating change visible in lighttable after rate_photos: <yes / no / required-refresh>
- Friendly error on darktable-not-running: <observed text>
- darktable log line `darktable-mcp bridge: ready` observed: <yes / no>

### Issues encountered

<empty if PASS; otherwise describe>
```

Fill in the actual observed values.

- [ ] **Step 4: Commit**

```bash
git add examples/smoke_test_bridge.py docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-spike-findings.md
git commit -m "test(smoke): manual end-to-end smoke test for bridge"
```

- [ ] **Step 5: Decision gate**

If the smoke test passes, proceed to Task 7.
If it fails, STOP. Report findings to the human. Likely cause: gap between Task 0 spike conditions and real two-process operation; the spec/plan needs revision.

---

## Task 7: README update + final acceptance + push

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Restore `view_photos` and `rate_photos` to the README**

Read `/home/andrii/projects/darktable-mcp/README.md` first.

In the "Implemented tools" intro list (around lines 9–20), add a new section above "Camera ingest":

```markdown
Library operations (via Lua plugin — requires `darktable-mcp install-plugin` and an open darktable session):
- `view_photos` — Browse photos in the darktable library with filename/rating filters.
- `rate_photos` — Apply star ratings to photos in the library.
```

In the detailed "Implemented tools" section (around lines 95–106), add corresponding long-form entries:

```markdown
Library operations (via Lua plugin — see `darktable-mcp install-plugin`):
- `view_photos(filter?, rating_min?, limit?)` — Returns photos matching the filename substring and minimum star rating, up to `limit` (default 100). Each entry: `id`, `filename`, `path`, `rating`. Requires darktable to be open with the plugin loaded.
- `rate_photos(photo_ids, rating)` — Applies a star rating (-1=reject, 0=unrated, 1-5=stars) to the named photos. Returns updated count. Requires darktable to be open with the plugin loaded.
```

- [ ] **Step 2: Update the "Why some tools are parked" section to reflect iteration 2**

Edit the section. Replace the existing iteration-2 pointer with a real link:

```markdown
> Iteration 2 (this iteration's spec: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md`) restored `view_photos` and `rate_photos` via a long-running Lua plugin. The remaining parked tools (`import_batch`, `adjust_exposure`, `apply_preset`) and the inner-range filter for `open_in_darktable` are deferred to a follow-up iteration — each is incremental once the bridge exists.
```

- [ ] **Step 3: Add an install note in the Installation section**

After the existing `pip install` block (around lines 53–60), add:

```markdown
After installing, also install the darktable Lua plugin:

\`\`\`bash
darktable-mcp install-plugin
\`\`\`

This copies one Lua file into `~/.config/darktable/lua/` and adds a single `require` line to `~/.config/darktable/luarc`. Restart darktable for the plugin to load. The library tools (`view_photos`, `rate_photos`) require darktable to be open with the plugin loaded.
```

(Use literal triple-backticks; the escapes above are for this plan markdown.)

- [ ] **Step 4: Sanity check**

Run:
```bash
grep -E "view_photos|rate_photos" README.md && echo "found" || echo "missing"
grep -E "install-plugin" README.md && echo "found" || echo "missing"
```

Expected: both print `found`.

- [ ] **Step 5: Run the full suite one last time**

Run: `venv/bin/pytest -q`

Expected: 142 passed (no regressions from README changes — there are no source changes).

- [ ] **Step 6: Eyeball the diff against `origin/main`**

Run:
```bash
git log --oneline origin/main..HEAD
git diff --stat origin/main..HEAD
```

Expected commits (in order):
1. `spike(ipc-bridge): verify dt.control.async worker primitive`
2. `test: pin IPC bridge MVP acceptance state`
3. `feat(bridge): file-based JSON request/response client to darktable Lua plugin`
4. `feat(lua): plugin with view_photos + rate_photos dispatcher and worker loop`
5. `feat(server): wire view_photos and rate_photos through the bridge`
6. `feat(cli): install-plugin / uninstall-plugin subcommands`
7. `test(smoke): manual end-to-end smoke test for bridge`
8. (this commit, next step) `docs(readme): restore view_photos and rate_photos via Lua plugin`

- [ ] **Step 7: Commit the README update**

```bash
git add README.md
git commit -m "docs(readme): restore view_photos and rate_photos via Lua plugin"
```

- [ ] **Step 8: Final grep audits**

Run:
```bash
grep -rn "BridgeError\|Bridge()" darktable_mcp tests | head -10
```

Expected: matches in `darktable_mcp/server.py`, `darktable_mcp/bridge/client.py`, `tests/test_bridge.py`, `tests/test_server.py` — all expected sites.

```bash
grep -rn "view_photos\|rate_photos" darktable_mcp/server.py darktable_mcp/lua/darktable_mcp.lua tests/test_honesty_pass_acceptance.py
```

Expected: matches in all three (server registration, plugin methods, acceptance pin).

- [ ] **Step 9: Push to origin/main (with explicit user approval)**

This step requires explicit user approval per the project's git safety conventions. Pause and ask the human: `Ready to push <N> commits to origin/main? (yes/no)`. Only push on `yes`.

```bash
git push origin main
```

- [ ] **Step 10: Mark done**

Iteration 2 (IPC bridge MVP) shipped. Iteration 2.5 (restoring `import_batch`, `adjust_exposure`, `apply_preset`, plus inner-range filter for `open_in_darktable`) is its own brainstorming → spec → plan cycle when the maintainer is ready.
