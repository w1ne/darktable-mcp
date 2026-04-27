"""Tests for darktable integration layer."""

from unittest.mock import patch

import pytest

from darktable_mcp.darktable.cli_wrapper import CLIWrapper
from darktable_mcp.utils.errors import DarktableNotFoundError


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
