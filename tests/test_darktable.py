"""Tests for darktable integration layer."""

from unittest.mock import Mock, patch

import pytest

from darktable_mcp.darktable.cli_wrapper import CLIWrapper
from darktable_mcp.darktable.lua_executor import LuaExecutor
from darktable_mcp.utils.errors import DarktableLuaError, DarktableNotFoundError


class TestLuaExecutor:
    """Test cases for LuaExecutor."""

    @patch("shutil.which")
    def test_lua_executor_init(self, mock_which):
        """Test LuaExecutor can be initialized."""
        mock_which.return_value = "/usr/bin/darktable"
        executor = LuaExecutor()
        assert executor is not None

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_execute_lua_script_success(self, mock_which, mock_run):
        """Test successful Lua script execution."""
        mock_which.return_value = "/usr/bin/darktable"
        mock_run.return_value = Mock(returncode=0, stdout="success")

        executor = LuaExecutor()
        result = executor.execute_script("test_script.lua", {"param": "value"})

        assert result == "success"
        mock_run.assert_called_once()

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_execute_lua_script_failure(self, mock_which, mock_run):
        """Test Lua script execution failure."""
        mock_which.return_value = "/usr/bin/darktable"
        mock_run.return_value = Mock(returncode=1, stderr="error")

        executor = LuaExecutor()

        with pytest.raises(DarktableLuaError):
            executor.execute_script("test_script.lua", {})


class TestCLIWrapper:
    """Test cases for CLIWrapper."""

    @patch("shutil.which")
    def test_cli_wrapper_init(self, mock_which):
        """Test CLIWrapper can be initialized."""
        mock_which.return_value = "/usr/bin/darktable-cli"
        wrapper = CLIWrapper()
        assert wrapper is not None

    @patch("shutil.which")
    def test_check_darktable_not_found(self, mock_which):
        """Test darktable executable not found."""
        mock_which.side_effect = ["/usr/bin/darktable-cli", None]

        wrapper = CLIWrapper()

        with pytest.raises(DarktableNotFoundError):
            wrapper.check_darktable_available()

    @patch("shutil.which")
    def test_check_darktable_found(self, mock_which):
        """Test darktable executable found."""
        mock_which.side_effect = ["/usr/bin/darktable-cli", "/usr/bin/darktable"]

        wrapper = CLIWrapper()
        result = wrapper.check_darktable_available()

        assert result == "/usr/bin/darktable"
