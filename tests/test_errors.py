"""Tests for darktable_mcp.utils.errors module."""

import pytest

from darktable_mcp.utils.errors import (
    DarktableMCPError,
    DarktableNotFoundError,
    DarktableLuaError,
    InvalidRatingError,
    PhotoNotFoundError,
    ValidationError,
    ExportError,
)


class TestExceptionHierarchy:
    """Test custom exception classes and their hierarchy."""

    def test_base_exception(self):
        """Test base DarktableMCPError exception."""
        error = DarktableMCPError("Base error message")
        assert str(error) == "Base error message"
        assert isinstance(error, Exception)

    def test_darktable_not_found_error(self):
        """Test DarktableNotFoundError inherits from base."""
        error = DarktableNotFoundError("darktable not found")
        assert str(error) == "darktable not found"
        assert isinstance(error, DarktableMCPError)
        assert isinstance(error, Exception)

    def test_darktable_lua_error(self):
        """Test DarktableLuaError inherits from base."""
        error = DarktableLuaError("Lua script failed")
        assert str(error) == "Lua script failed"
        assert isinstance(error, DarktableMCPError)
        assert isinstance(error, Exception)

    def test_invalid_rating_error(self):
        """Test InvalidRatingError inherits from base."""
        error = InvalidRatingError("Invalid rating: 7")
        assert str(error) == "Invalid rating: 7"
        assert isinstance(error, DarktableMCPError)
        assert isinstance(error, Exception)

    def test_photo_not_found_error(self):
        """Test PhotoNotFoundError inherits from base."""
        error = PhotoNotFoundError("Photo not found: /path/to/photo.jpg")
        assert str(error) == "Photo not found: /path/to/photo.jpg"
        assert isinstance(error, DarktableMCPError)
        assert isinstance(error, Exception)

    def test_validation_error(self):
        """Test ValidationError inherits from base."""
        error = ValidationError("Validation failed")
        assert str(error) == "Validation failed"
        assert isinstance(error, DarktableMCPError)
        assert isinstance(error, Exception)

    def test_export_error(self):
        """Test ExportError inherits from base."""
        error = ExportError("Export failed")
        assert str(error) == "Export failed"
        assert isinstance(error, DarktableMCPError)
        assert isinstance(error, Exception)

    def test_exception_can_be_raised_and_caught(self):
        """Test exceptions can be raised and caught properly."""
        # Test raising and catching specific exception
        with pytest.raises(InvalidRatingError):
            raise InvalidRatingError("Rating out of range")

        # Test catching as base exception
        with pytest.raises(DarktableMCPError):
            raise PhotoNotFoundError("Photo missing")

        # Test catching as general Exception
        with pytest.raises(Exception):
            raise DarktableLuaError("Script error")

    def test_exception_without_message(self):
        """Test exceptions work without custom messages."""
        error = ValidationError()
        assert isinstance(error, ValidationError)
        assert isinstance(error, DarktableMCPError)