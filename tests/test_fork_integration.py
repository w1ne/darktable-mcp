"""Contract tests for integrating agentic tools with the upstream transport."""

from darktable_mcp.server import DarktableMCPServer


def test_agentic_tools_registered_alongside_library_tools():
    server = DarktableMCPServer()
    names = set(server.list_tools())
    assert {
        "view_photos",
        "export_images",
        "get_contact_sheet",
        "open_image_in_darkroom",
        "mask_object",
        "retouch_add_shape",
        "capture_viewport",
        "set_viewport",
        "restore_viewport",
    } <= names
    assert {tool.name for tool in server._tool_definitions()} == names


async def _check_current_image_thread():
    import threading
    from unittest.mock import Mock

    server = DarktableMCPServer()
    loop_thread = threading.get_ident()
    seen = []

    def reply(*args, **kwargs):
        seen.append(threading.get_ident())
        return {"has_image": False}

    server.bridge = Mock()
    server.bridge.call.side_effect = reply
    await server._handle_get_current_image({})
    assert seen and seen[0] != loop_thread


def test_agentic_bridge_does_not_block_event_loop():
    import asyncio

    asyncio.run(_check_current_image_thread())


def test_gui_transactions_do_not_interleave():
    import asyncio

    from mcp.types import TextContent

    async def check():
        server = DarktableMCPServer()
        active = 0
        peak = 0

        async def handler(arguments):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return [TextContent(type="text", text="ok")]

        server._handler_map["set_params"] = handler
        await asyncio.gather(
            server._call_tool("set_params", {}), server._call_tool("set_params", {})
        )
        assert peak == 1

    asyncio.run(check())


def test_cancelled_gui_call_finishes_before_next_edit():
    import asyncio

    from mcp.types import TextContent

    async def check():
        server = DarktableMCPServer()
        started, release = asyncio.Event(), asyncio.Event()
        order = []

        async def first(arguments):
            started.set()
            await release.wait()
            order.append("restored")
            return [TextContent(type="text", text="restored")]

        async def second(arguments):
            order.append("edited")
            return [TextContent(type="text", text="edited")]

        server._handler_map["preview_lut"] = first
        server._handler_map["set_params"] = second
        task = asyncio.create_task(server._call_tool("preview_lut", {}))
        await started.wait()
        task.cancel()
        queued = asyncio.create_task(server._call_tool("set_params", {}))
        await asyncio.sleep(0)
        assert not order
        # Validation of a headless tool stays responsive while GUI is locked.
        result = await server._call_tool("export_images", {})
        assert "output_path" in result[0].text
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await queued
        assert order == ["restored", "edited"]

    asyncio.run(check())


def test_custom_http_mount_is_used_for_downloads(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("DTMCP_PUBLIC_URL", "https://photos.example")
    photo = tmp_path / "preview.png"
    photo.write_bytes(b"preview")
    server = DarktableMCPServer()
    with patch("uvicorn.Server") as runner:
        runner.return_value.serve = AsyncMock()
        asyncio.run(server.run_http(path="/photos"))
    assert server._download_url(str(photo)).startswith("https://photos.example/photos/files/")
