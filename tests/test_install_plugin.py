"""Tests for darktable_mcp.cli.install_plugin install/uninstall functions."""

from darktable_mcp.cli.install_plugin import install, uninstall


def test_install_writes_plugin_file_and_luarc(tmp_path):
    home = tmp_path
    install(home)
    plugin = home / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    luarc = home / ".config" / "darktable" / "luarc"
    assert plugin.is_file()
    assert "view_photos" in plugin.read_text()
    assert luarc.is_file()
    assert 'require "darktable_mcp"' in luarc.read_text()


def test_install_is_idempotent(tmp_path):
    install(tmp_path)
    install(tmp_path)  # second run should not duplicate the require line
    luarc_text = (tmp_path / ".config" / "darktable" / "luarc").read_text()
    assert luarc_text.count('require "darktable_mcp"') == 1


def test_install_preserves_existing_luarc_lines(tmp_path):
    luarc = tmp_path / ".config" / "darktable"
    luarc.mkdir(parents=True)
    (luarc / "luarc").write_text('-- user comment\nrequire "other_plugin"\n')
    install(tmp_path)
    text = (luarc / "luarc").read_text()
    assert "-- user comment" in text
    assert 'require "other_plugin"' in text
    assert 'require "darktable_mcp"' in text


def test_install_creates_luarc_if_missing(tmp_path):
    install(tmp_path)
    luarc = tmp_path / ".config" / "darktable" / "luarc"
    assert luarc.is_file()
    assert luarc.read_text().strip() == 'require "darktable_mcp"'


def test_uninstall_removes_plugin_file_and_require_line(tmp_path):
    install(tmp_path)
    uninstall(tmp_path)
    plugin = tmp_path / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    luarc = tmp_path / ".config" / "darktable" / "luarc"
    assert not plugin.exists()
    assert luarc.is_file()
    assert 'require "darktable_mcp"' not in luarc.read_text()


def test_uninstall_preserves_other_luarc_lines(tmp_path):
    luarc_path = tmp_path / ".config" / "darktable" / "luarc"
    luarc_path.parent.mkdir(parents=True)
    luarc_path.write_text('-- comment\nrequire "other"\nrequire "darktable_mcp"\n')
    (tmp_path / ".config" / "darktable" / "lua").mkdir()
    (tmp_path / ".config" / "darktable" / "lua" / "darktable_mcp.lua").write_text("-- fake")
    uninstall(tmp_path)
    text = luarc_path.read_text()
    assert "-- comment" in text
    assert 'require "other"' in text
    assert 'require "darktable_mcp"' not in text


def test_uninstall_when_not_installed_is_noop(tmp_path):
    # No prior install. Should not raise.
    uninstall(tmp_path)


def test_install_overwrites_modified_plugin_file(tmp_path):
    install(tmp_path)
    plugin = tmp_path / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    plugin.write_text("-- user-modified garbage")
    install(tmp_path)
    assert "view_photos" in plugin.read_text()


def test_install_re_enables_commented_out_require(tmp_path):
    luarc_dir = tmp_path / ".config" / "darktable"
    luarc_dir.mkdir(parents=True)
    (luarc_dir / "luarc").write_text(
        '-- some other comment\n-- require "darktable_mcp"\n'
    )
    install(tmp_path)
    text = (luarc_dir / "luarc").read_text()
    # The commented-out form is still there (we don't touch it),
    # but a fresh active require line was appended.
    assert '-- require "darktable_mcp"' in text
    active_lines = [
        ln for ln in text.splitlines()
        if ln.split("--", 1)[0].rstrip() == 'require "darktable_mcp"'
    ]
    assert len(active_lines) == 1


def test_uninstall_removes_require_with_inline_comment(tmp_path):
    luarc_dir = tmp_path / ".config" / "darktable"
    luarc_dir.mkdir(parents=True)
    (luarc_dir / "luarc").write_text(
        'require "other"\nrequire "darktable_mcp"  -- with a comment\n'
    )
    (luarc_dir / "lua").mkdir()
    (luarc_dir / "lua" / "darktable_mcp.lua").write_text("-- fake")
    uninstall(tmp_path)
    text = (luarc_dir / "luarc").read_text()
    assert 'require "other"' in text
    assert 'darktable_mcp' not in text
