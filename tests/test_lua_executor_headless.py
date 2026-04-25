"""Tests for headless mode Lua script executor."""

from unittest.mock import Mock, patch

import pytest

from darktable_mcp.darktable.lua_executor import LuaExecutor
from darktable_mcp.utils.errors import DarktableLuaError


class TestLuaExecutorHeadlessMode:
    """Test cases for LuaExecutor headless mode."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup for each test."""
        # Mock darktable path to avoid DarktableNotFoundError
        pass

    def test_execute_script_headless_mode(self):
        """Test executing Lua script in headless mode."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout='{"success": true}')

            executor = LuaExecutor(darktable_path="/usr/bin/darktable")
            result = executor.execute_script(
                'dt = require("darktable"); print("test")', headless=True
            )

            assert result == '{"success": true}'
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "lua" in args
            assert "-e" in args

    def test_execute_script_gui_mode(self):
        """Test executing Lua script with GUI mode."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="GUI result")

            executor = LuaExecutor(darktable_path="/usr/bin/darktable")
            result = executor.execute_script(
                'print("test")', headless=False, gui_purpose="Show user preview"
            )

            assert result == "GUI result"
            args = mock_run.call_args[0][0]
            assert "darktable" in args[0]
            assert "--lua" in args

    def test_execute_script_headless_with_library_detector(self):
        """Test that headless mode uses LibraryDetector."""
        with patch("subprocess.run") as mock_run:
            with patch("darktable_mcp.darktable.library_detector.LibraryDetector") as mock_detector:
                mock_detector_instance = Mock()
                mock_detector_instance.find_library.return_value = (
                    "/home/user/.config/darktable/library.db"
                )
                mock_detector.return_value = mock_detector_instance
                mock_run.return_value = Mock(returncode=0, stdout="test output")

                executor = LuaExecutor(darktable_path="/usr/bin/darktable")
                result = executor.execute_script('print("hello")', headless=True)

                assert result == "test output"
                mock_detector.assert_called_once()
                mock_detector_instance.find_library.assert_called_once()

    def test_execute_script_headless_default_mode(self):
        """Test that headless=True is default."""
        with patch("subprocess.run") as mock_run:
            with patch("darktable_mcp.darktable.library_detector.LibraryDetector") as mock_detector:
                mock_detector_instance = Mock()
                mock_detector_instance.find_library.return_value = (
                    "/home/user/.config/darktable/library.db"
                )
                mock_detector.return_value = mock_detector_instance
                mock_run.return_value = Mock(returncode=0, stdout="test output")

                executor = LuaExecutor(darktable_path="/usr/bin/darktable")
                # Call without headless parameter (should default to True)
                result = executor.execute_script('print("hello")')

                assert result == "test output"
                # Verify lua was called (not darktable)
                args = mock_run.call_args[0][0]
                assert "lua" in args

    def test_execute_script_headless_with_params(self):
        """Test headless execution with parameters."""
        with patch("subprocess.run") as mock_run:
            with patch("darktable_mcp.darktable.library_detector.LibraryDetector") as mock_detector:
                mock_detector_instance = Mock()
                mock_detector_instance.find_library.return_value = (
                    "/home/user/.config/darktable/library.db"
                )
                mock_detector.return_value = mock_detector_instance
                mock_run.return_value = Mock(returncode=0, stdout="result")

                executor = LuaExecutor(darktable_path="/usr/bin/darktable")
                params = {"test_param": "test_value", "test_num": 42}
                result = executor.execute_script("print(test_param)", params=params, headless=True)

                assert result == "result"
                # Verify the script call includes parameter lua
                call_args = mock_run.call_args[0][0]
                script_content = call_args[2]  # -e argument
                assert "test_param" in script_content
                assert "test_num" in script_content

    def test_execute_script_headless_error_handling(self):
        """Test error handling in headless mode."""
        with patch("subprocess.run") as mock_run:
            with patch("darktable_mcp.darktable.library_detector.LibraryDetector") as mock_detector:
                mock_detector_instance = Mock()
                mock_detector_instance.find_library.return_value = (
                    "/home/user/.config/darktable/library.db"
                )
                mock_detector.return_value = mock_detector_instance
                mock_run.return_value = Mock(returncode=1, stderr="Lua error message")

                executor = LuaExecutor(darktable_path="/usr/bin/darktable")
                with pytest.raises(DarktableLuaError) as exc_info:
                    executor.execute_script("invalid lua", headless=True)

                assert "Headless Lua script execution failed" in str(exc_info.value)
                assert "Lua error message" in str(exc_info.value)

    def test_execute_script_gui_error_handling(self):
        """Test error handling in GUI mode."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stderr="Script error")

            executor = LuaExecutor(darktable_path="/usr/bin/darktable")
            with pytest.raises(DarktableLuaError) as exc_info:
                executor.execute_script("invalid", headless=False)

            assert "Lua script execution failed" in str(exc_info.value)

    def test_execute_script_timeout_headless(self):
        """Test timeout handling in headless mode."""
        import subprocess

        with patch("subprocess.run") as mock_run:
            with patch("darktable_mcp.darktable.library_detector.LibraryDetector") as mock_detector:
                mock_detector_instance = Mock()
                mock_detector_instance.find_library.return_value = (
                    "/home/user/.config/darktable/library.db"
                )
                mock_detector.return_value = mock_detector_instance
                mock_run.side_effect = subprocess.TimeoutExpired("lua", 30)

                executor = LuaExecutor(darktable_path="/usr/bin/darktable")
                with pytest.raises(DarktableLuaError) as exc_info:
                    executor.execute_script("long running", headless=True)

                assert "timed out" in str(exc_info.value).lower()

    def test_execute_script_gui_mode_cleanup(self):
        """Test that GUI mode cleans up temp files on error."""
        with patch("subprocess.run") as mock_run:
            with patch("pathlib.Path.unlink") as mock_unlink:
                mock_run.return_value = Mock(returncode=0, stdout="success")

                executor = LuaExecutor(darktable_path="/usr/bin/darktable")
                result = executor.execute_script('print("test")', headless=False)

                assert result == "success"
                # Verify temp file was cleaned up
                mock_unlink.assert_called_once()
