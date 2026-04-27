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
