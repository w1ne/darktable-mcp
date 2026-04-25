"""Tests for darktable library path detection."""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from darktable_mcp.darktable.library_detector import LibraryDetector
from darktable_mcp.utils.errors import DarktableNotFoundError


class TestLibraryDetector:
    """Test cases for LibraryDetector."""

    def test_find_darktable_library_linux(self):
        """Test library detection on Linux."""
        with patch('pathlib.Path.home') as mock_home:
            mock_home.return_value = Path('/home/user')
            with patch('pathlib.Path.exists') as mock_exists:
                mock_exists.return_value = True
                detector = LibraryDetector()
                result = detector.find_library()
                assert result == '/home/user/.config/darktable/library.db'

    def test_find_darktable_library_macos(self):
        """Test library detection on macOS."""
        with patch('pathlib.Path.home') as mock_home:
            mock_home.return_value = Path('/Users/user')

            def mock_exists_impl(path_self):
                # Only macOS path exists
                if 'Library/Application Support' in str(path_self):
                    return True
                return False

            with patch.object(Path, 'exists', mock_exists_impl):
                detector = LibraryDetector()
                result = detector.find_library()
                assert result == '/Users/user/Library/Application Support/darktable/library.db'

    def test_find_darktable_library_windows(self):
        """Test library detection on Windows."""
        with patch('pathlib.Path.home') as mock_home:
            mock_home.return_value = Path('C:\\Users\\user')

            def mock_exists_impl(path_self):
                # Only Windows path exists
                if 'AppData' in str(path_self):
                    return True
                return False

            with patch.object(Path, 'exists', mock_exists_impl):
                detector = LibraryDetector()
                result = detector.find_library()
                # Windows path comparison - normalize both
                result_normalized = result.replace('\\', '/')
                expected = 'C:/Users/user/AppData/Local/darktable/library.db'
                assert result_normalized == expected

    def test_find_darktable_library_not_found(self):
        """Test error when no library found."""
        with patch('pathlib.Path.home') as mock_home:
            mock_home.return_value = Path('/home/user')
            with patch('pathlib.Path.exists') as mock_exists:
                mock_exists.return_value = False
                detector = LibraryDetector()
                with pytest.raises(DarktableNotFoundError) as exc_info:
                    detector.find_library()
                assert "Please make sure darktable is installed" in str(exc_info.value)

    def test_find_darktable_library_returns_first_existing(self):
        """Test that find_library returns the first existing library path."""
        with patch('pathlib.Path.home') as mock_home:
            mock_home.return_value = Path('/home/user')

            def exists_side_effect():
                # Simulate that only macOS path exists (for testing)
                # This won't work with our simple implementation, but tests the logic
                return True

            with patch.object(Path, 'exists', return_value=True):
                detector = LibraryDetector()
                result = detector.find_library()
                # Should return Linux path (first in the list)
                assert result == '/home/user/.config/darktable/library.db'
