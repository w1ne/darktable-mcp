"""Integration guards for variant paths and nested blocking bridge helpers."""

import threading
from unittest.mock import AsyncMock, Mock

import pytest

from darktable_mcp import server as module


@pytest.mark.asyncio
async def test_view_photos_exposes_host_sidecar_for_exact_variant_export(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKTABLE_MCP_RUN_DIR", str(tmp_path))
    server = object.__new__(module.DarktableMCPServer)
    server._bridge_call = AsyncMock(
        return_value=(
            [
                {
                    "id": "4",
                    "filename": "image.raw",
                    "path": "/run/image.raw",
                    "sidecar": "/run/image_02.raw.xmp",
                }
            ],
            None,
        )
    )
    reply = await server._handle_view_photos({})
    assert str(tmp_path / "image.raw") in reply[0].text
    assert f"xmp_path: {tmp_path / 'image_02.raw.xmp'}" in reply[0].text


@pytest.mark.asyncio
async def test_get_preview_nested_image_lookup_does_not_block_event_loop(monkeypatch):
    server = object.__new__(module.DarktableMCPServer)
    event_loop_thread = threading.get_ident()

    def call(method, *args, **kwargs):
        assert threading.get_ident() != event_loop_thread, f"{method} blocked the event loop"
        if method == "dev_preview":
            return {"status": "ok", "path": "/tmp/preview.jpg"}
        assert method == "dev_current_image"
        return {"has_image": True, "id": 4, "filename": "image.raw"}

    server.bridge = Mock(call=call)
    server._download_url = Mock(return_value=None)
    monkeypatch.setattr(module, "_inline_image_content", Mock(return_value=None))
    reply = await server._handle_get_preview({})
    assert "id=4 filename=image.raw" in reply[0].text


@pytest.mark.asyncio
async def test_current_image_exposes_host_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKTABLE_MCP_RUN_DIR", str(tmp_path))
    server = object.__new__(module.DarktableMCPServer)
    server.bridge = Mock()
    server.bridge.call.return_value = {
        "has_image": True,
        "id": 4,
        "filename": "image.raw",
        "path": "/run/image.raw",
        "sidecar": "/run/image_02.raw.xmp",
    }
    reply = await server._handle_get_current_image({})
    assert f"sidecar={tmp_path / 'image_02.raw.xmp'}" in reply[0].text
