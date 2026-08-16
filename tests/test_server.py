"""Tests for the main MCP server."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

from darktable_mcp.server import DarktableMCPServer


class TestDarktableMCPServer:
    """Test cases for DarktableMCPServer."""

    def test_server_initialization(self):
        server = DarktableMCPServer()
        assert server is not None
        assert hasattr(server, "app")

    def test_server_has_required_tools(self):
        server = DarktableMCPServer()
        expected_tools = {
            "import_from_camera",
            "export_images",
            "extract_previews",
            "apply_ratings_batch",
            "open_in_darktable",
            "view_photos",
            "rate_photos",
            "import_batch",
            "list_styles",
            "apply_preset",
        }
        assert set(server.list_tools()) == expected_tools

    @pytest.mark.asyncio
    async def test_server_can_start(self):
        server = DarktableMCPServer()

        @asynccontextmanager
        async def fake_stdio():
            yield (AsyncMock(), AsyncMock())

        with (
            patch("darktable_mcp.server.stdio_server", fake_stdio),
            patch.object(server.app, "run", new=AsyncMock(return_value=None)) as mock_run,
        ):
            await server.start()
            mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_import_from_camera_handler():
    server = DarktableMCPServer()
    mock_tools = Mock()
    mock_tools.import_from_camera.return_value = (
        "Copied 5 file(s) from Nikon DSC D800E (usb:002,002)\n"
        "Destination: /tmp/import-2026-04-26"
    )
    server.camera_tools = mock_tools

    result = await server._handle_import_from_camera({"destination": "/tmp/import-2026-04-26"})

    assert len(result) == 1
    assert "Copied 5 file(s)" in result[0].text
    assert "Nikon DSC D800E" in result[0].text
    mock_tools.import_from_camera.assert_called_once_with({"destination": "/tmp/import-2026-04-26"})


def test_server_registers_import_from_camera_tool():
    server = DarktableMCPServer()
    tool_names = [t.name for t in server._tool_definitions()]
    assert "import_from_camera" in tool_names


@pytest.mark.asyncio
async def test_handle_view_photos_returns_formatted_list():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = [
        {"id": "1", "filename": "a.NEF", "path": "/photos/a.NEF", "rating": 5},
        {"id": "2", "filename": "b.NEF", "path": "/photos/b.NEF", "rating": 4},
    ]
    result = await server._handle_view_photos({"filter": "", "limit": 10})
    assert len(result) == 1
    text = result[0].text
    assert "a.NEF" in text
    assert "b.NEF" in text
    # The absolute file path must be in the formatted output so the agent
    # can pass it straight to export_images. Otherwise view_photos and
    # export_images don't compose.
    assert "/photos/a.NEF" in text
    assert "/photos/b.NEF" in text
    server.bridge.call.assert_called_once_with("view_photos", {"filter": "", "limit": 10})


@pytest.mark.asyncio
async def test_handle_view_photos_no_results_message():
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


@pytest.mark.asyncio
async def test_handle_import_batch_returns_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"imported": 12, "source_path": "/path/foo"}
    result = await server._handle_import_batch({"source_path": "/path/foo"})
    assert "Imported 12" in result[0].text
    assert "/path/foo" in result[0].text
    server.bridge.call.assert_called_once_with("import_batch", {"source_path": "/path/foo"})


@pytest.mark.asyncio
async def test_handle_import_batch_friendly_error_when_plugin_missing():
    from darktable_mcp.bridge.client import BridgePluginNotInstalledError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgePluginNotInstalledError("missing")
    result = await server._handle_import_batch({"source_path": "/x"})
    assert "install-plugin" in result[0].text


@pytest.mark.asyncio
async def test_handle_import_batch_friendly_error_when_dt_not_running():
    from darktable_mcp.bridge.client import BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")
    result = await server._handle_import_batch({"source_path": "/x"})
    assert "darktable" in result[0].text.lower()


@pytest.mark.asyncio
async def test_handle_list_styles_returns_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "styles": [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}],
        "count": 2,
    }
    result = await server._handle_list_styles({})
    text = result[0].text
    assert "2 styles installed" in text
    assert "alpha" in text
    assert "beta" in text


@pytest.mark.asyncio
async def test_handle_list_styles_empty():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"styles": [], "count": 0}
    result = await server._handle_list_styles({})
    assert "No styles" in result[0].text


@pytest.mark.asyncio
async def test_handle_list_styles_truncates_at_50():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "styles": [{"name": f"s{i}", "description": ""} for i in range(75)],
        "count": 75,
    }
    result = await server._handle_list_styles({})
    text = result[0].text
    assert "75 styles installed" in text
    assert "and 25 more" in text


@pytest.mark.asyncio
async def test_handle_apply_preset_returns_applied_count():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {"applied": 3, "missed": [], "preset_name": "myStyle"}
    result = await server._handle_apply_preset(
        {
            "photo_ids": ["1", "2", "3"],
            "preset_name": "myStyle",
        }
    )
    text = result[0].text
    assert "myStyle" in text
    assert "3 photo" in text


@pytest.mark.asyncio
async def test_handle_apply_preset_reports_missed():
    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "applied": 1,
        "missed": ["999"],
        "preset_name": "myStyle",
    }
    result = await server._handle_apply_preset(
        {
            "photo_ids": ["1", "999"],
            "preset_name": "myStyle",
        }
    )
    text = result[0].text
    assert "999" in text
    assert "Missed" in text


@pytest.mark.asyncio
async def test_handle_apply_preset_friendly_error_when_dt_not_running():
    from darktable_mcp.bridge.client import BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")
    result = await server._handle_apply_preset(
        {
            "photo_ids": ["1"],
            "preset_name": "x",
        }
    )
    assert "darktable" in result[0].text.lower()


@pytest.mark.asyncio
async def test_handle_export_images_writes_side_file_and_short_summary(tmp_path):
    """`export_images` used to dump the full per-file map inline, blowing
    Claude's token budget at 400+ files. The handler now writes per-file
    results to a JSONL side file and returns only counts + the path."""
    import json

    from darktable_mcp.darktable.cli_wrapper import ExportResult

    server = DarktableMCPServer()
    fake_results = [
        ExportResult(input="/in/A.NEF", output=f"{tmp_path}/A.jpg", ok=True, error=None),
        ExportResult(input="/in/B.NEF", output=f"{tmp_path}/B.jpg", ok=True, error=None),
        ExportResult(input="/in/C.NEF", output=None, ok=False, error="Export failed: Boom"),
    ]
    server._cli = Mock()
    server._cli.batch_export.return_value = fake_results

    result = await server._handle_export_images(
        {
            "photo_ids": ["/in/A.NEF", "/in/B.NEF", "/in/C.NEF"],
            "output_path": str(tmp_path),
            "format": "jpeg",
            "quality": 95,
        }
    )
    text = result[0].text

    # Summary stays compact: counts + side file pointer + first error.
    assert "exported: 2" in text
    assert "failed: 1" in text
    assert ".export_images.jsonl" in text
    assert "Boom" in text  # first error gets a short snippet
    # Crucially, the per-file output map is NOT inlined.
    assert "/in/A.NEF" not in text
    assert "/in/B.NEF" not in text

    # Side file holds the full record, one JSON per line.
    side = tmp_path / ".export_images.jsonl"
    assert side.exists()
    entries = [json.loads(line) for line in side.read_text().splitlines()]
    assert len(entries) == 3
    assert {e["input"] for e in entries} == {r.input for r in fake_results}
    assert {e["ok"] for e in entries} == {True, False}
    by_input = {e["input"]: e for e in entries}
    assert by_input["/in/A.NEF"]["output"] == f"{tmp_path}/A.jpg"
    assert by_input["/in/A.NEF"]["error"] is None
    assert by_input["/in/C.NEF"]["output"] is None
    assert by_input["/in/C.NEF"]["error"] == "Export failed: Boom"


@pytest.mark.asyncio
async def test_handle_export_images_trusts_ok_not_the_output_string(tmp_path):
    """The old classifier sniffed the status string for "failed"/"Error", so a
    genuine success written into a directory named e.g. 'failed-rolls' was
    counted as a failure. `ExportResult.ok` is now the only authority."""
    from darktable_mcp.darktable.cli_wrapper import ExportResult

    server = DarktableMCPServer()
    server._cli = Mock()
    server._cli.batch_export.return_value = [
        ExportResult(
            input="/in/A.NEF",
            output=f"{tmp_path}/failed-rolls/Error-A.jpg",
            ok=True,
            error=None,
        ),
    ]

    result = await server._handle_export_images(
        {
            "photo_ids": ["/in/A.NEF"],
            "output_path": str(tmp_path),
            "format": "jpeg",
        }
    )
    assert "exported: 1, failed: 0" in result[0].text


@pytest.mark.asyncio
async def test_handle_export_images_offloads_the_batch(tmp_path):
    """darktable-cli runs one subprocess per file. Blocking the event loop for
    the whole batch stalls every other request on the stdio connection."""
    import threading

    from darktable_mcp.darktable.cli_wrapper import ExportResult

    loop_thread = threading.get_ident()
    seen = {}

    def slow_batch_export(**kwargs):
        seen["thread"] = threading.get_ident()
        return [ExportResult(input="/in/A.NEF", output="/out/A.jpg", ok=True, error=None)]

    server = DarktableMCPServer()
    server._cli = Mock()
    server._cli.batch_export.side_effect = slow_batch_export

    await server._handle_export_images(
        {
            "photo_ids": ["/in/A.NEF"],
            "output_path": str(tmp_path),
            "format": "jpeg",
        }
    )
    assert seen["thread"] != loop_thread, "batch_export ran on the event loop thread"


@pytest.mark.asyncio
async def test_handle_export_images_validates_required_args():
    server = DarktableMCPServer()
    r = await server._handle_export_images({"photo_ids": ["/x"]})
    assert "output_path" in r[0].text
    r = await server._handle_export_images({"output_path": "/tmp/x"})
    assert "photo_ids" in r[0].text


@pytest.mark.asyncio
async def test_handle_export_images_threads_max_dimensions_through(tmp_path):
    """`max_width`/`max_height` emit darktable-cli's real --width/--height and
    were unreachable from the tool schema."""
    from darktable_mcp.darktable.cli_wrapper import ExportResult

    server = DarktableMCPServer()
    server._cli = Mock()
    server._cli.batch_export.return_value = [
        ExportResult(input="/in/A.NEF", output=f"{tmp_path}/A.jpg", ok=True, error=None),
    ]

    await server._handle_export_images(
        {
            "photo_ids": ["/in/A.NEF"],
            "output_path": str(tmp_path),
            "format": "jpeg",
            "max_width": 2048,
            "max_height": 1536,
        }
    )
    kwargs = server._cli.batch_export.call_args.kwargs
    assert kwargs["max_width"] == 2048
    assert kwargs["max_height"] == 1536


@pytest.mark.asyncio
async def test_handle_export_images_defaults_max_dimensions_to_none(tmp_path):
    from darktable_mcp.darktable.cli_wrapper import ExportResult

    server = DarktableMCPServer()
    server._cli = Mock()
    server._cli.batch_export.return_value = [
        ExportResult(input="/in/A.NEF", output=f"{tmp_path}/A.jpg", ok=True, error=None),
    ]

    await server._handle_export_images(
        {
            "photo_ids": ["/in/A.NEF"],
            "output_path": str(tmp_path),
            "format": "jpeg",
        }
    )
    kwargs = server._cli.batch_export.call_args.kwargs
    assert kwargs["max_width"] is None
    assert kwargs["max_height"] is None
    # Tuning knobs stay out of the agent-facing surface.
    assert "max_workers" not in kwargs
    assert "timeout" not in kwargs


def test_cli_batch_export_accepts_the_max_dimensions_the_handler_sends():
    """The handler tests above mock `CLIWrapper`, so they cannot catch a real
    signature mismatch. `_handle_export_images` passes `max_width`/`max_height`
    to `batch_export`; if the real method does not accept them, every export
    raises TypeError in production while the mocked tests stay green.

    Blocked on a change in cli_wrapper.py (not owned here): `batch_export` must
    take `max_width: Optional[int] = None, max_height: Optional[int] = None`
    and forward them through `_export_one` into `export_image`.
    """
    import inspect

    from darktable_mcp.darktable.cli_wrapper import CLIWrapper

    params = inspect.signature(CLIWrapper.batch_export).parameters
    assert "max_width" in params
    assert "max_height" in params


def test_export_images_schema_exposes_size_limits_but_not_tuning_knobs():
    server = DarktableMCPServer()
    tool = next(t for t in server._tool_definitions() if t.name == "export_images")
    props = tool.inputSchema["properties"]

    assert props["max_width"]["type"] == "integer"
    assert props["max_height"]["type"] == "integer"
    assert "max_workers" not in props
    assert "timeout" not in props
    # Output names are de-collided, so the agent must not guess <stem>.<format>.
    assert "output" in tool.description


BRIDGE_HANDLER_CALLS = [
    ("_handle_view_photos", {}),
    ("_handle_rate_photos", {"photo_ids": ["1"], "rating": 3}),
    ("_handle_import_batch", {"source_path": "/x"}),
    ("_handle_list_styles", {}),
    ("_handle_apply_preset", {"photo_ids": ["1"], "preset_name": "x"}),
]


class TestBridgeErrorMappingIsSharedOnce:
    """All five bridge handlers used to carry their own copy of the same
    12-line try/except. There must now be exactly one copy."""

    def test_source_holds_a_single_copy_of_each_message(self):
        import inspect

        from darktable_mcp import server as server_module

        source = inspect.getsource(server_module)
        assert source.count('darktable-mcp install-plugin",') == 1
        assert source.count("bridge timeout") == 1
        assert source.count('text=f"Plugin error: {e}"') == 1

    @pytest.mark.parametrize("handler_name,args", BRIDGE_HANDLER_CALLS)
    @pytest.mark.asyncio
    async def test_plugin_missing_message_is_identical(self, handler_name, args):
        from darktable_mcp.bridge.client import BridgePluginNotInstalledError

        server = DarktableMCPServer()
        server.bridge = Mock()
        server.bridge.call.side_effect = BridgePluginNotInstalledError("missing")
        result = await getattr(server, handler_name)(args)
        assert result[0].text == (
            "darktable-mcp plugin not installed. Run: darktable-mcp install-plugin"
        )

    @pytest.mark.parametrize("handler_name,args", BRIDGE_HANDLER_CALLS)
    @pytest.mark.asyncio
    async def test_plugin_error_message_is_identical(self, handler_name, args):
        from darktable_mcp.bridge.client import BridgeError

        server = DarktableMCPServer()
        server.bridge = Mock()
        server.bridge.call.side_effect = BridgeError("boom")
        result = await getattr(server, handler_name)(args)
        assert result[0].text == "Plugin error: boom"


@pytest.mark.asyncio
async def test_timeout_message_names_the_budget_and_both_causes():
    """ "darktable not running" was a lie for a slow-but-alive call. The text
    must name the budget that elapsed and offer both explanations."""
    from darktable_mcp.bridge.client import DEFAULT_TIMEOUTS, BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")
    result = await server._handle_import_batch({"source_path": "/x"})
    text = result[0].text

    assert f"{DEFAULT_TIMEOUTS['import_batch']:g}s" in text
    assert "import_batch" in text
    assert "plugin" in text.lower()
    assert "large" in text.lower() or "longer" in text.lower()


@pytest.mark.asyncio
async def test_bridge_timeout_text_quotes_the_per_method_budget():
    """Different methods get different budgets, so the text must not hardcode
    one number."""
    from darktable_mcp.bridge.client import BridgeTimeoutError

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = BridgeTimeoutError("timeout")

    view = await server._handle_view_photos({})
    imports = await server._handle_import_batch({"source_path": "/x"})
    assert "30s" in view[0].text
    assert "120s" in imports[0].text


@pytest.mark.asyncio
async def test_handle_import_from_camera_offloads_the_transfer():
    """A card transfer can run for an hour. On the event loop it would stall
    every other request on the stdio connection for that whole time."""
    import threading

    loop_thread = threading.get_ident()
    seen = {}

    def slow_import(arguments):
        seen["thread"] = threading.get_ident()
        return "Copied 5 file(s)"

    server = DarktableMCPServer()
    server.camera_tools = Mock()
    server.camera_tools.import_from_camera.side_effect = slow_import

    result = await server._handle_import_from_camera({"destination": "/tmp/x"})
    assert "Copied 5 file(s)" in result[0].text
    assert seen["thread"] != loop_thread, "import_from_camera ran on the event loop thread"


@pytest.mark.asyncio
async def test_bridge_handlers_offload_the_call():
    """`Bridge.call` busy-waits with `time.sleep`, now for up to 120s. On the
    event loop that is a 120s outage for the whole connection."""
    import threading

    loop_thread = threading.get_ident()
    seen = {}

    def slow_call(method, params):
        seen["thread"] = threading.get_ident()
        return {"imported": 1, "source_path": "/x"}

    server = DarktableMCPServer()
    server.bridge = Mock()
    server.bridge.call.side_effect = slow_call

    await server._handle_import_batch({"source_path": "/x"})
    assert seen["thread"] != loop_thread, "Bridge.call ran on the event loop thread"


@pytest.mark.asyncio
async def test_handle_extract_previews_offloads():
    import threading

    loop_thread = threading.get_ident()
    seen = {}

    def slow_extract(**kwargs):
        seen["thread"] = threading.get_ident()
        return {"items": [], "count": 0}

    with (
        patch("darktable_mcp.server.extract_previews", side_effect=slow_extract),
        patch("darktable_mcp.server.format_extract_summary", return_value="done"),
    ):
        server = DarktableMCPServer()
        result = await server._handle_extract_previews({"source_dir": "/raws"})

    assert result[0].text == "done"
    assert seen["thread"] != loop_thread, "extract_previews ran on the event loop thread"


@pytest.mark.asyncio
async def test_handle_apply_ratings_batch_offloads():
    import threading

    loop_thread = threading.get_ident()
    seen = {}

    def slow_apply(**kwargs):
        seen["thread"] = threading.get_ident()
        return {"written": 1}

    with (
        patch("darktable_mcp.server.apply_ratings_batch", side_effect=slow_apply),
        patch("darktable_mcp.server.format_ratings_summary", return_value="done"),
    ):
        server = DarktableMCPServer()
        result = await server._handle_apply_ratings_batch(
            {"source_dir": "/raws", "ratings": {"DSC_1": 4}}
        )

    assert result[0].text == "done"
    assert seen["thread"] != loop_thread, "apply_ratings_batch ran on the event loop thread"


@pytest.mark.asyncio
async def test_handle_open_in_darktable_offloads():
    import threading

    loop_thread = threading.get_ident()
    seen = {}

    def slow_open(**kwargs):
        seen["thread"] = threading.get_ident()
        return {"launched": True}

    with (
        patch("darktable_mcp.server.open_in_darktable", side_effect=slow_open),
        patch("darktable_mcp.server.format_open_summary", return_value="done"),
    ):
        server = DarktableMCPServer()
        result = await server._handle_open_in_darktable({"source_dir": "/raws"})

    assert result[0].text == "done"
    assert seen["thread"] != loop_thread, "open_in_darktable ran on the event loop thread"


def test_import_from_camera_description_chains_into_import_batch():
    """import_batch shipped after this description was written; the tool used
    to tell the agent to have the user click 'import folder' by hand."""
    server = DarktableMCPServer()
    tool = next(t for t in server._tool_definitions() if t.name == "import_from_camera")
    desc = tool.description

    assert "import_batch" in desc
    assert "import folder" not in desc.lower()


def test_import_from_camera_description_is_honest_about_cost():
    server = DarktableMCPServer()
    tool = next(t for t in server._tool_definitions() if t.name == "import_from_camera")
    desc = tool.description.lower()

    assert "synchronously" in desc
    assert ".import.log" in desc
