"""install-plugin / uninstall-plugin subcommands.

Library functions take an explicit `home_dir` for testability;
CLI entry points pass `Path.home()`.
"""

from __future__ import annotations

import argparse
import importlib.resources
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
