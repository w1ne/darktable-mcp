"""Acceptance tests for the honesty-pass iteration.

Each test pins one piece of the desired final state. They are red at the
start of the iteration and turn green incrementally as deletion tasks
land. See docs/superpowers/specs/2026-04-27-honesty-pass-design.md.

These tests stay in the suite as a regression guard after the iteration
completes — do not delete this file when the tests go green.
"""

import importlib

import pytest

from darktable_mcp.server import DarktableMCPServer

EXPECTED_TOOLS = {
    "import_from_camera",
    "export_images",
    "extract_previews",
    "apply_ratings_batch",
    "open_in_darktable",
}


def test_server_registers_exactly_the_surviving_tools():
    """No broken or stubbed library tools advertised."""
    server = DarktableMCPServer()
    assert set(server.list_tools()) == EXPECTED_TOOLS


def test_lua_executor_module_is_removed():
    with pytest.raises(ImportError):
        importlib.import_module("darktable_mcp.darktable.lua_executor")


def test_library_detector_module_is_removed():
    with pytest.raises(ImportError):
        importlib.import_module("darktable_mcp.darktable.library_detector")


def test_photo_tools_module_is_removed():
    """Renamed to camera_tools.py."""
    with pytest.raises(ImportError):
        importlib.import_module("darktable_mcp.tools.photo_tools")


def test_camera_tools_module_exposes_camera_tools_class():
    mod = importlib.import_module("darktable_mcp.tools.camera_tools")
    assert hasattr(mod, "CameraTools")
    assert not hasattr(mod, "PhotoTools")
