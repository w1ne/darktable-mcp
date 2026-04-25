"""Darktable library path detection across different operating systems."""

from pathlib import Path
from typing import Optional

from ..utils.errors import DarktableNotFoundError


class LibraryDetector:
    """Detects the darktable library database path across different platforms."""

    def find_library(self) -> str:
        """Find the darktable library database path.

        Searches for the darktable library database in platform-specific locations:
        - Linux: ~/.config/darktable/library.db
        - macOS: ~/Library/Application Support/darktable/library.db
        - Windows: ~/AppData/Local/darktable/library.db

        Returns:
            str: Path to the darktable library database

        Raises:
            DarktableNotFoundError: If the library database is not found
        """
        default_locations = [
            Path.home() / ".config/darktable/library.db",  # Linux
            Path.home() / "Library/Application Support/darktable/library.db",  # macOS
            Path.home() / "AppData/Local/darktable/library.db",  # Windows
        ]

        for path in default_locations:
            if path.exists():
                return str(path)

        raise DarktableNotFoundError(
            "Please make sure darktable is installed and you've imported some photos first"
        )
