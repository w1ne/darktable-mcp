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
