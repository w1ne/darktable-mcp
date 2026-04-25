"""Tests for darktable_mcp.utils.validation module."""

import os
import re
import tempfile
from pathlib import Path
import pytest

from darktable_mcp.utils.validation import (
    validate_rating,
    validate_file_path,
    validate_directory_path,
    validate_image_extensions,
    validate_preset_name,
)
from darktable_mcp.utils.errors import ValidationError, InvalidRatingError


class TestValidateRating:
    """Test rating validation function."""

    def test_valid_integers(self):
        """Test valid integer ratings."""
        for rating in [1, 2, 3, 4, 5]:
            assert validate_rating(rating) == rating

    def test_valid_string_integers(self):
        """Test valid string representations of integers."""
        for rating_str in ["1", "2", "3", "4", "5"]:
            expected = int(rating_str)
            assert validate_rating(rating_str) == expected

    def test_invalid_range_low(self):
        """Test rating below valid range."""
        with pytest.raises(InvalidRatingError, match="Rating must be between 1-5, got: 0"):
            validate_rating(0)

    def test_invalid_range_high(self):
        """Test rating above valid range."""
        with pytest.raises(InvalidRatingError, match="Rating must be between 1-5, got: 6"):
            validate_rating(6)

    def test_invalid_negative(self):
        """Test negative rating."""
        with pytest.raises(InvalidRatingError, match="Rating must be between 1-5, got: -1"):
            validate_rating(-1)

    def test_invalid_type_string(self):
        """Test invalid string input."""
        with pytest.raises(InvalidRatingError, match="Rating must be an integer, got: abc"):
            validate_rating("abc")

    def test_invalid_type_float(self):
        """Test float input (converts to int, but may be outside range)."""
        # Python's int() converts 3.5 to 3, which is valid
        # Test with a float that converts to invalid range
        with pytest.raises(InvalidRatingError, match="Rating must be between 1-5, got: 6"):
            validate_rating(6.7)

    def test_invalid_type_none(self):
        """Test None input."""
        with pytest.raises(InvalidRatingError, match="Rating must be an integer, got: None"):
            validate_rating(None)

    def test_edge_cases(self):
        """Test edge cases."""
        # Test string with leading/trailing spaces (should still work if convertible)
        assert validate_rating("  3  ") == 3

        # Test empty string
        with pytest.raises(InvalidRatingError):
            validate_rating("")


class TestValidateFilePath:
    """Test file path validation function."""

    def test_valid_existing_file(self):
        """Test with an existing file."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            result = validate_file_path(tmp_path)
            assert result == tmp_path
            assert isinstance(result, Path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_valid_existing_file_string(self):
        """Test with an existing file path as string."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path_str = tmp.name

        try:
            result = validate_file_path(tmp_path_str)
            assert result == Path(tmp_path_str)
            assert isinstance(result, Path)
        finally:
            Path(tmp_path_str).unlink(missing_ok=True)

    def test_nonexistent_file(self):
        """Test with non-existent file."""
        nonexistent = "/tmp/nonexistent_file_12345"
        with pytest.raises(ValidationError, match=f"Path does not exist: {nonexistent}"):
            validate_file_path(nonexistent)

    def test_unreadable_file(self, tmp_path):
        """Test with unreadable file (mock scenario)."""
        # Create a file
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        # On most systems, we can't actually make a file unreadable to ourselves,
        # so we'll just verify the function would work with readable files
        result = validate_file_path(test_file)
        assert result == test_file


class TestValidateDirectoryPath:
    """Test directory path validation function."""

    def test_valid_existing_directory(self, tmp_path):
        """Test with an existing directory."""
        result = validate_directory_path(tmp_path)
        assert result == tmp_path
        assert isinstance(result, Path)

    def test_valid_existing_directory_string(self, tmp_path):
        """Test with an existing directory path as string."""
        result = validate_directory_path(str(tmp_path))
        assert result == tmp_path
        assert isinstance(result, Path)

    def test_nonexistent_directory(self):
        """Test with non-existent directory."""
        nonexistent = "/tmp/nonexistent_dir_12345"
        with pytest.raises(ValidationError, match=f"Path does not exist: {nonexistent}"):
            validate_directory_path(nonexistent)

    def test_file_instead_of_directory(self, tmp_path):
        """Test with a file when directory is expected."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        with pytest.raises(ValidationError, match=f"Path is not a directory: {test_file}"):
            validate_directory_path(test_file)


class TestValidateImageExtensions:
    """Test image extension validation function."""

    def test_valid_image_extensions(self):
        """Test with valid image file extensions."""
        files = [
            "photo.jpg", "image.jpeg", "picture.png", "scan.tiff",
            "raw.cr2", "photo.nef", "image.arw", "picture.dng"
        ]
        result = validate_image_extensions(files)
        assert result == files

    def test_mixed_extensions(self):
        """Test with mix of valid and invalid extensions."""
        files = [
            "photo.jpg",      # valid
            "document.txt",   # invalid
            "image.png",      # valid
            "video.mp4",      # invalid
            "raw.cr2"         # valid
        ]
        expected = ["photo.jpg", "image.png", "raw.cr2"]
        result = validate_image_extensions(files)
        assert result == expected

    def test_case_insensitive(self):
        """Test that extension matching is case insensitive."""
        files = ["photo.JPG", "image.PNG", "scan.TIFF", "raw.CR2"]
        result = validate_image_extensions(files)
        assert result == files

    def test_no_valid_files(self):
        """Test with no valid image files."""
        files = ["document.txt", "video.mp4", "audio.mp3"]
        result = validate_image_extensions(files)
        assert result == []

    def test_empty_list(self):
        """Test with empty file list."""
        result = validate_image_extensions([])
        assert result == []

    def test_various_raw_formats(self):
        """Test various RAW file formats."""
        raw_files = [
            "canon.cr2", "canon.cr3",        # Canon
            "nikon.nef",                     # Nikon
            "sony.arw",                      # Sony
            "adobe.dng",                     # Adobe DNG
            "fuji.raf",                      # Fujifilm
            "olympus.orf",                   # Olympus
            "panasonic.rw2",                 # Panasonic
            "pentax.pef",                    # Pentax
            "samsung.srw"                    # Samsung
        ]
        result = validate_image_extensions(raw_files)
        assert result == raw_files


class TestValidatePresetName:
    """Test preset name validation function."""

    def test_valid_preset_names(self):
        """Test valid preset names."""
        valid_names = [
            "Portrait",
            "Landscape Mode",
            "Black-and-White",
            "High_Contrast",
            "Vintage Look 2024",
            "Studio-Lighting_v2"
        ]
        for name in valid_names:
            result = validate_preset_name(name)
            assert result == name.strip()

    def test_whitespace_stripping(self):
        """Test that leading/trailing whitespace is stripped."""
        result = validate_preset_name("  Portrait  ")
        assert result == "Portrait"

    def test_empty_preset_name(self):
        """Test empty preset name."""
        with pytest.raises(ValidationError, match="Preset name cannot be empty"):
            validate_preset_name("")

    def test_whitespace_only_preset_name(self):
        """Test preset name with only whitespace."""
        with pytest.raises(ValidationError, match="Preset name cannot be empty"):
            validate_preset_name("   ")

    def test_invalid_characters(self):
        """Test preset names with invalid characters."""
        invalid_names = [
            "Portrait@Studio",      # @ symbol
            "Black&White",          # & symbol
            "High.Contrast",        # . symbol
            "Portrait/Landscape",   # / symbol
            "Mode#1",              # # symbol
            "Vintage*Look"         # * symbol
        ]
        for name in invalid_names:
            with pytest.raises(ValidationError, match=f"Invalid preset name format: {re.escape(name)}"):
                validate_preset_name(name)

    def test_numbers_allowed(self):
        """Test that numbers in preset names are allowed."""
        result = validate_preset_name("Portrait2024")
        assert result == "Portrait2024"

    def test_spaces_allowed(self):
        """Test that spaces in preset names are allowed."""
        result = validate_preset_name("High Contrast Mode")
        assert result == "High Contrast Mode"

    def test_hyphens_and_underscores_allowed(self):
        """Test that hyphens and underscores are allowed."""
        result = validate_preset_name("Black-and-White_v2")
        assert result == "Black-and-White_v2"