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

# Updated 2026-04-27 (iteration 2): added view_photos + rate_photos restored via IPC bridge.
EXPECTED_TOOLS = {
    "import_from_camera",
    "export_images",
    "extract_previews",
    "apply_ratings_batch",
    "open_in_darktable",
    "view_photos",
    # Contact-sheet spec: grid-of-thumbnails visual culling over the current
    # collection (see docs/contact-sheet spec).
    "get_contact_sheet",
    "rate_photos",
    # DAM extension: tagging + tag-backed collections (darktable's Lua API
    # has no separate "collection" object, so collections == tags).
    "tag_photo",
    # AI-written per-photo assessment/notes, stored in the standard
    # dc:description xmp field (image.description in darktable's Lua API).
    "set_photo_note",
    "get_photo_note",
    "list_collections",
    "list_photos_in_collection",
    "import_batch",
    "list_styles",
    "apply_preset",
    # T1.6: Phase 1 scalar darkroom-editing tools (see PLAN.md §5 T1.6).
    "open_image_in_darkroom",
    "navigate_photo",
    "get_current_image",
    "list_modules",
    "get_params",
    "set_params",
    # LUT tooling (2026-07-26): blend_params is a separate flat struct from
    # module params, needed since lut3d has no "amount" of its own.
    "get_blend_params",
    "set_blend_params",
    "get_preview",
    # T1.7: enable/instance/viewport wiring (see PLAN.md §5 T1.7).
    "enable_module",
    # LUT tooling (2026-07-26): list the lut3d module's configured LUT root
    # (same conf key the UI's own file-chooser dropdown reads), try a LUT
    # without a lasting edit, and compare several side by side.
    "list_luts",
    "preview_lut",
    "compare_luts",
    "add_instance",
    "get_viewport",
    # Writable viewport zoom/pan (2026-07-31 set-viewport-design): the write
    # counterpart to get_viewport()/capture_viewport, whose render detail is
    # otherwise capped by whatever zoom the human last left it at.
    "set_viewport",
    "restore_viewport",
    # Viewport-relative retouch (2026-07-25 design proposal): capture a
    # snapshot of what the user sees in main/preview2, then place retouch
    # points relative to that snapshot instead of the full image.
    "capture_viewport",
    # T2.3: object masks (see PLAN.md §5 T2.2/T2.3).
    "add_path_mask",
    "mask_object",
    # Viewport-relative drawn masks (2026-07-31 delegated block C): the
    # add_path_mask/generic-overlay counterpart to retouch's own viewport kit
    # above -- points relative to a capture_viewport snapshot instead of the
    # full image, plus a generic drawn-mask overlay renderer (path/brush
    # polygons, circles) since darktable's own mask overlay is painted on the
    # GUI widget for every mask type, not just retouch's.
    "add_path_mask_in_viewport",
    "render_module_mask",
    # T3.3: raster (matte) masks (see PLAN.md §5 T3.3).
    "mask_raster",
    # Retouch Phase 1: local heal/clone circle shapes (rt_forms + wavelet
    # scale), see PLAN.md's retouch section.
    "retouch_add_shape",
    "retouch_add_shape_in_viewport",
    "retouch_update_shape_in_viewport",
    "retouch_delete_shape",
    "retouch_delete_shapes",
    "retouch_list_shapes",
    # Overlay render: shape geometry drawn on a capture_viewport snapshot,
    # because darktable's own mask overlay is painted on the GUI widget and is
    # absent from every buffer we can capture (see retouch_overlay.py).
    "retouch_render_overlay",
    # Mask group management (2026-07-27): attach/detach an EXISTING drawn
    # shape to/from a module's blend group without copying it, so hand-drawn
    # shapes or shapes from other tools can be shared across new module
    # instances instead of redrawn per instance.
    "list_masks",
    "get_module_mask",
    "get_mask_geometry",
    "rename_mask",
    "delete_mask",
    "attach_mask",
    "detach_mask",
    "set_module_mask",
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
