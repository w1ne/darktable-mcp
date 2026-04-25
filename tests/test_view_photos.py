"""Tests for the view_photos handler.

The handler shells out to darktable via LuaExecutor; we mock the executor
to exercise the parsing layer without spawning darktable.
"""

from unittest.mock import MagicMock

import pytest

from darktable_mcp.server import DarktableMCPServer


@pytest.mark.asyncio
async def test_view_photos_parses_lua_output():
    server = DarktableMCPServer()
    server._lua = MagicMock()
    server._lua.execute_script.return_value = (
        "42\t5\t/photos/2024/a.raw\n"
        "43\t-1\t/photos/2024/b.raw\n"
        "44\t3\t/photos/2025/c.raw\n"
    )

    result = await server._handle_view_photos({"rating_min": 3, "limit": 10})

    assert len(result) == 1
    text = result[0].text
    assert "#42" in text and "/photos/2024/a.raw" in text
    assert "#44" in text and "/photos/2025/c.raw" in text

    # Assert params were forwarded to the Lua executor.
    args, kwargs = server._lua.execute_script.call_args
    params = args[1] if len(args) > 1 else kwargs.get("params")
    assert params == {"rating_min": 3, "filter_text": "", "limit": 10}


@pytest.mark.asyncio
async def test_view_photos_empty_output():
    server = DarktableMCPServer()
    server._lua = MagicMock()
    server._lua.execute_script.return_value = ""

    result = await server._handle_view_photos({})

    assert result[0].text == "No photos found."


@pytest.mark.asyncio
async def test_view_photos_skips_malformed_lines():
    server = DarktableMCPServer()
    server._lua = MagicMock()
    server._lua.execute_script.return_value = (
        "garbage line\n"
        "42\t5\t/ok.raw\n"
        "\n"
        "not\tan\tint\trow\n"
        "43\tnope\t/path\n"
    )

    result = await server._handle_view_photos({})

    assert "#42" in result[0].text
    assert "/ok.raw" in result[0].text
    # Only the one valid row should make it through.
    assert result[0].text.count("\n") == 0
