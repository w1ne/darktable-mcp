"""Input validation utilities for darktable MCP server."""

import os
import re
from pathlib import Path
from typing import List, Optional, Union

from .errors import ValidationError, InvalidRatingError


def validate_rating(rating: Union[int, str]) -> int:
    """Validate star rating is between 1-5.

    Args:
        rating: Rating value to validate

    Returns:
        int: Validated rating

    Raises:
        InvalidRatingError: If rating is not 1-5
    """
    try:
        rating_int = int(rating)
    except (ValueError, TypeError):
        raise InvalidRatingError(f"Rating must be an integer, got: {rating}")

    if not (1 <= rating_int <= 5):
        raise InvalidRatingError(f"Rating must be between 1-5, got: {rating_int}")

    return rating_int


def validate_file_path(path: Union[str, Path]) -> Path:
    """Validate file path exists and is readable.

    Args:
        path: File path to validate

    Returns:
        Path: Validated path object

    Raises:
        ValidationError: If path is invalid or not accessible
    """
    path_obj = Path(path)

    if not path_obj.exists():
        raise ValidationError(f"Path does not exist: {path}")

    if not os.access(path_obj, os.R_OK):
        raise ValidationError(f"Path is not readable: {path}")

    return path_obj


def validate_directory_path(path: Union[str, Path]) -> Path:
    """Validate directory path exists and is readable.

    Args:
        path: Directory path to validate

    Returns:
        Path: Validated path object

    Raises:
        ValidationError: If path is invalid or not accessible
    """
    path_obj = validate_file_path(path)

    if not path_obj.is_dir():
        raise ValidationError(f"Path is not a directory: {path}")

    return path_obj


def validate_image_extensions(files: List[str]) -> List[str]:
    """Validate files have supported image extensions.

    Args:
        files: List of file paths

    Returns:
        List[str]: Filtered list of valid image files
    """
    supported_extensions = {
        '.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp',
        '.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng',
        '.raf', '.orf', '.rw2', '.pef', '.srw'
    }

    valid_files = []
    for file_path in files:
        ext = Path(file_path).suffix.lower()
        if ext in supported_extensions:
            valid_files.append(file_path)

    return valid_files


def validate_preset_name(preset: str) -> str:
    """Validate preset name format.

    Args:
        preset: Preset name to validate

    Returns:
        str: Validated preset name

    Raises:
        ValidationError: If preset name is invalid
    """
    if not preset or not preset.strip():
        raise ValidationError("Preset name cannot be empty")

    # Basic validation - alphanumeric, spaces, hyphens, underscores
    if not re.match(r'^[a-zA-Z0-9\s\-_]+$', preset):
        raise ValidationError(f"Invalid preset name format: {preset}")

    return preset.strip()