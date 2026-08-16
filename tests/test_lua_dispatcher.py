"""Pytest wrapper that runs the Lua dispatcher tests via a system lua interpreter.

The Lua half of the bridge is a real program with real bugs, so these tests
have to actually execute. A skipped test file reads as green and proves
nothing, so set ``DARKTABLE_MCP_REQUIRE_LUA=1`` (do this in CI) to turn the
"no interpreter" skip into a hard failure.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

#: Interpreters we accept, best first. Distros disagree on the binary name:
#: Homebrew and Arch ship `lua`, Debian/Ubuntu ship `lua5.4` / `lua5.3`.
LUA_CANDIDATES = ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit")

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "darktable_mcp" / "lua" / "darktable_mcp.lua"


def _find_lua() -> str | None:
    for name in LUA_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


LUA_BIN = _find_lua()

#: Opt-in strictness: without an interpreter the suite proves nothing, so a
#: CI job that means to gate the Lua side should refuse to let it skip.
REQUIRE_LUA = os.environ.get("DARKTABLE_MCP_REQUIRE_LUA") == "1"

_skip_no_lua = pytest.mark.skipif(
    LUA_BIN is None and not REQUIRE_LUA,
    reason=(
        "no lua interpreter found (tried: "
        + ", ".join(LUA_CANDIDATES)
        + "). Install one (brew install lua / apt install lua5.4), or set "
        "DARKTABLE_MCP_REQUIRE_LUA=1 to make this a failure."
    ),
)


@_skip_no_lua
def test_lua_dispatcher():
    """The Lua dispatcher unit suite must run and pass, not skip."""
    assert LUA_BIN is not None, (
        "DARKTABLE_MCP_REQUIRE_LUA=1 but no lua interpreter is installed "
        f"(tried: {', '.join(LUA_CANDIDATES)})"
    )
    result = subprocess.run(
        [LUA_BIN, "tests/lua/test_dispatcher.lua"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Lua tests failed under {LUA_BIN}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Guard against a suite that exits 0 without having asserted anything
    # (e.g. an early `return` or a load failure swallowed somewhere).
    assert "OK: all dispatcher tests passed" in result.stdout, (
        "Lua suite exited 0 but never printed its success line -- it did not "
        f"run to completion.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # The harness writes to stderr only on failure.
    assert result.stderr.strip() == "", f"unexpected Lua stderr: {result.stderr}"


# ---- Static guards on the plugin source ------------------------------------
# These do not need an interpreter, so they run everywhere.

_SHELL_CALL = re.compile(r"\b(?:io\.popen|os\.execute)\s*\(")


def test_every_shell_invocation_is_quoted():
    """No shell command may interpolate a path without shell_quote().

    cache_dir() derives from $HOME / $XDG_CACHE_HOME. Interpolating either
    raw (the old `'ls -1 "' .. dir .. '"'`) breaks on a double quote and
    executes attacker-chosen text on a backtick or $(...).
    """
    source = PLUGIN.read_text(encoding="utf-8")
    offenders = []
    for match in _SHELL_CALL.finditer(source):
        # A single os.execute(string.format(...)) can span several lines, so
        # look at a window rather than one line.
        window = source[match.start() : match.start() + 500]
        if "shell_quote(" not in window:
            line_no = source[: match.start()].count("\n") + 1
            offenders.append(f"line {line_no}: {match.group(0)}")
    assert not offenders, (
        "shell invocation(s) in darktable_mcp.lua do not route their "
        "interpolated path through shell_quote(): " + "; ".join(offenders)
    )


def test_plugin_has_no_flat_poll_loop():
    """The worker must sleep an adaptive interval, not a hardcoded 100ms.

    A flat `dt.control.sleep(100)` in the worker forks ~36,000 `ls`
    subprocesses per hour for the whole darktable session, idle or not.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    # Look at the CALL SITE, not the definition -- `local function
    # poll_interval_ms(idle_ticks)` would satisfy a naive substring check even
    # with the worker back on a hardcoded sleep.
    assert "sleep(poll_interval_ms(" in source, (
        "worker_loop no longer sleeps poll_interval_ms(...); the adaptive "
        "backoff has been removed, renamed, or unwired from the loop"
    )
    worker = source.split("local function worker_loop", 1)
    assert len(worker) == 2, "worker_loop not found in the plugin"
    body = worker[1]
    assert "dt.control.sleep(100)" not in body, (
        "worker_loop still contains a hardcoded 100ms sleep"
    )


def test_import_batch_does_not_echo_an_unhonoured_recursive_flag():
    """`recursive` must drive behaviour, not just be echoed back.

    It used to be read, defaulted to true, and returned in the response
    without ever being used -- so with darktable's recurse_directories
    preference off, importing an import_from_camera destination registered
    nothing while the tool reported success with ``recursive: true``.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split("methods.import_batch = function", 1)
    assert len(body) == 2, "methods.import_batch not found in the plugin"
    body = body[1].split("\nmethods.", 1)[0]

    assert "list_directories_recursive(source_path)" in body, (
        "import_batch does not enumerate the tree, so `recursive` is not "
        "honoured -- recursion would silently fall back to the user's "
        "darktable preference"
    )
    assert "recursive_honoured" in body, (
        "import_batch does not report whether the requested recursion mode "
        "actually happened"
    )
    # The plugin must not silently rewrite a persistent user preference.
    assert "preferences.write" not in source, (
        "the plugin writes a darktable preference; flipping a persistent user "
        "setting from the background worker is its own bug"
    )


def test_stale_age_outlasts_every_client_timeout():
    """The sweep must never delete a request whose caller is still waiting.

    The Lua worker is single-threaded, so a request can sit queued behind a
    call that legitimately runs for its full budget. With STALE_AGE_SECONDS at
    60 -- shorter than the 120s ``import_batch`` / ``apply_preset`` budgets --
    the sweep deleted the queued ``request-*.json`` before the worker ever got
    to it, and its caller waited out the whole 120s to be told "darktable is
    not running".

    This is the only place the two halves of the invariant are compared, since
    the numbers live in different languages.
    """
    from darktable_mcp.bridge.client import DEFAULT_TIMEOUTS, FALLBACK_TIMEOUT

    source = PLUGIN.read_text(encoding="utf-8")
    match = re.search(r"^local STALE_AGE_SECONDS\s*=\s*(\d+)", source, re.MULTILINE)
    assert match is not None, "STALE_AGE_SECONDS not found in the plugin"
    stale_age = int(match.group(1))

    max_client_timeout = max([*DEFAULT_TIMEOUTS.values(), FALLBACK_TIMEOUT])
    assert stale_age > 120, (
        f"STALE_AGE_SECONDS is {stale_age}s; it must exceed the 120s client "
        "budgets or the sweep deletes live requests"
    )
    assert stale_age > max_client_timeout, (
        f"STALE_AGE_SECONDS ({stale_age}s) must exceed the largest client "
        f"budget in DEFAULT_TIMEOUTS ({max_client_timeout}s). Raise the stale "
        "age, or the sweep will delete requests whose callers are still "
        "waiting for them."
    )
    # sweep_stale floors the age to whole minutes for `find -mmin`, so check
    # the value that actually reaches find, not the constant.
    assert (stale_age // 60) * 60 > max_client_timeout, (
        f"STALE_AGE_SECONDS ({stale_age}s) rounds down to "
        f"{(stale_age // 60) * 60}s for find -mmin, which no longer clears the "
        f"{max_client_timeout}s client budget"
    )
    # The comment is the only thing that will warn whoever edits either number.
    assert "DEFAULT_TIMEOUTS" in source, (
        "the STALE_AGE_SECONDS invariant no longer names DEFAULT_TIMEOUTS as "
        "the thing it must stay ahead of"
    )


def test_sweep_covers_orphan_responses():
    """sweep_stale must collect response-*.json, not just request-*.json.

    A client that times out deletes its own request file, but the worker may
    already be mid-flight and writes response-<uuid>.json afterwards. Unswept,
    those orphans grow the cache directory without bound across sessions.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    sweep = source.split("local function sweep_stale", 1)
    assert len(sweep) == 2, "sweep_stale not found in the plugin"
    body = sweep[1].split("\nend", 1)[0]
    assert '-name "request-*.json"' in body, "sweep no longer covers requests"
    assert '-name "response-*.json"' in body, (
        "sweep_stale does not delete orphan response files"
    )
