"""Tests for darktable integration layer."""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from darktable_mcp.darktable.cli_wrapper import CLIWrapper
from darktable_mcp.utils.errors import DarktableNotFoundError, ExportError


class TestCLIWrapperDiscovery:
    """darktable-cli auto-detect and check_darktable_available."""

    @patch("shutil.which")
    def test_cli_wrapper_init(self, mock_which, tmp_path):
        mock_which.return_value = "/usr/bin/darktable-cli"
        wrapper = CLIWrapper(configdir=tmp_path)
        assert wrapper is not None

    @patch("shutil.which")
    def test_check_darktable_not_found(self, mock_which, tmp_path):
        mock_which.side_effect = ["/usr/bin/darktable-cli", None]
        wrapper = CLIWrapper(configdir=tmp_path)
        with pytest.raises(DarktableNotFoundError):
            wrapper.check_darktable_available()

    @patch("shutil.which")
    def test_check_darktable_found(self, mock_which, tmp_path):
        mock_which.side_effect = ["/usr/bin/darktable-cli", "/usr/bin/darktable"]
        wrapper = CLIWrapper(configdir=tmp_path)
        assert wrapper.check_darktable_available() == "/usr/bin/darktable"


class TestCLIWrapperConfigdir:
    """The dedicated configdir prevents the GUI's library lock from breaking exports."""

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_default_configdir_is_xdg_cache_namespaced(self, _mock_which, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        wrapper = CLIWrapper()
        assert wrapper.configdir == tmp_path / "darktable-mcp" / "cli-config"
        assert wrapper.configdir.is_dir()

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_default_configdir_falls_back_to_home_cache(
        self, _mock_which, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        wrapper = CLIWrapper()
        assert wrapper.configdir == tmp_path / ".cache" / "darktable-mcp" / "cli-config"
        assert wrapper.configdir.is_dir()

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_custom_configdir_is_respected_and_created(self, _mock_which, tmp_path):
        target = tmp_path / "nested" / "custom-cli-config"
        wrapper = CLIWrapper(configdir=target)
        assert wrapper.configdir == target
        assert target.is_dir()


class TestCLIWrapperExport:
    """Export passes --configdir so darktable-cli does not race the GUI's lock."""

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_passes_configdir_to_darktable_cli(
        self, _mock_which, mock_run, tmp_path
    ):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        cfg = tmp_path / "cfg"
        wrapper = CLIWrapper(configdir=cfg)
        wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg", 95)

        cmd = mock_run.call_args[0][0]
        assert "--core" in cmd
        idx = cmd.index("--configdir")
        assert cmd[idx + 1] == str(cfg)

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_includes_jpeg_quality_conf(self, _mock_which, mock_run, tmp_path):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        wrapper = CLIWrapper(configdir=tmp_path)
        wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg", quality=88)

        cmd = mock_run.call_args[0][0]
        assert "--conf" in cmd
        assert any("plugins/imageio/format/jpeg/quality=88" in part for part in cmd)

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_failure_raises_export_error_with_stderr(
        self, _mock_which, mock_run, tmp_path
    ):
        mock_run.return_value = Mock(
            returncode=1, stdout="", stderr="database is locked"
        )
        wrapper = CLIWrapper(configdir=tmp_path)
        with pytest.raises(ExportError, match="database is locked"):
            wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_timeout_raises_export_error(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["darktable-cli"], timeout=5)
        wrapper = CLIWrapper(configdir=tmp_path)
        with pytest.raises(ExportError, match="timed out"):
            wrapper.export_image(
                Path("/in.NEF"), tmp_path / "out.jpg", "jpeg", timeout=5
            )

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_batch_export_writes_each_file(self, _mock_which, mock_run, tmp_path):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        results = wrapper.batch_export(
            [Path("/a.NEF"), Path("/b.NEF")],
            tmp_path / "out",
            format_type="jpeg",
            quality=90,
        )
        assert len(results) == 2
        assert all("Exported to" in v for v in results.values())
        # Every invocation must include --configdir
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "--configdir" in cmd
