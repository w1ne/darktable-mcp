"""Filesystem and image-grid helpers for the recovered LUT server tools.

These helpers only inspect LUT metadata and render existing previews; applying
LUTs and restoring darktable state remain the bridge handler's responsibility.
"""

from __future__ import annotations

import math
import shlex
from pathlib import Path, PureWindowsPath
from typing import Any

import cv2
import numpy as np

SUPPORTED_FORMATS = {".cube", ".3dl", ".png"}
MAX_SHEET_WIDTH = 2400
MAX_COLUMNS = 64
MAX_ENTRIES = 64


class LutRootNotConfiguredError(ValueError):
    """The darktable LUT root configuration is missing."""


def _root(root_dir: str) -> Path:
    if not isinstance(root_dir, str) or not root_dir.strip():
        raise LutRootNotConfiguredError("plugins/darkroom/lut3d/def_path is not set")
    root = Path(root_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"LUT root does not exist: {root}")
    return root


def _within_root(root: Path, relative: str) -> Path:
    path = Path(relative)
    windows = PureWindowsPath(relative)
    if path.is_absolute() or windows.is_absolute() or windows.drive or "\\" in relative:
        raise ValueError("LUT paths must be relative to the configured root")
    if ".." in path.parts:
        raise ValueError("LUT paths cannot contain parent traversal")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("LUT path escapes the configured root")
    return resolved


def resolve_lut_path(root_dir: str, relative: str) -> Path:
    """Return a regular LUT file confined to the configured root."""
    if not isinstance(relative, str) or not relative:
        raise ValueError("A relative LUT path is required")
    result = _within_root(_root(root_dir), relative)
    if not result.is_file():
        raise FileNotFoundError(f"LUT file does not exist: {relative}")
    if result.suffix.lower() not in SUPPORTED_FORMATS:
        raise ValueError("Supported LUT formats are .cube, .3dl and .png")
    return result


def scan_lut_directory(root_dir: str, directory: str | None = None) -> list[dict[str, Any]]:
    """List supported files deterministically without following escaped symlinks."""
    root = _root(root_dir)
    scope = _within_root(root, directory or "")
    if not scope.is_dir():
        raise FileNotFoundError(f"LUT directory does not exist: {directory}")
    items = []
    for path in scope.rglob("*"):
        if path.suffix.lower() not in SUPPORTED_FORMATS or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            continue
        items.append(
            {
                "path": path.relative_to(root).as_posix(),
                "name": path.stem,
                "format": path.suffix[1:].lower(),
            }
        )
    return sorted(items, key=lambda item: (item["path"].casefold(), item["path"]))


def parse_cube_header(path: Path) -> dict[str, Any]:
    """Read bounded .cube metadata, stopping when numeric LUT data starts.

    Malformed optional metadata is ignored so one bad title cannot break listing.
    This does not validate the LUT's full sample data.
    """
    result: dict[str, Any] = {}
    try:
        with Path(path).open(encoding="utf-8-sig", errors="replace") as source:
            # Header metadata is small; never scan megabytes of LUT samples.
            for _ in range(256):
                line = source.readline(4096)
                if not line:
                    break
                try:
                    fields = shlex.split(line, comments=True)
                except ValueError:
                    continue
                if not fields:
                    continue
                try:
                    float(fields[0])
                except ValueError:
                    pass
                else:
                    break
                if fields[0] == "TITLE" and len(fields) == 2:
                    result["title"] = fields[1]
                elif fields[0] in ("LUT_3D_SIZE", "LUT_1D_SIZE") and len(fields) == 2:
                    try:
                        size = int(fields[1])
                    except ValueError:
                        continue
                    if size > 0:
                        result["size"] = size
    except OSError:
        # A file may disappear between directory enumeration and metadata read.
        return {}
    return result


def effective_cell_width(columns: int, requested_width: int) -> int:
    if not 1 <= columns <= MAX_COLUMNS or requested_width < 1:
        raise ValueError("Columns must be 1..64 and cell width must be positive")
    return min(requested_width, MAX_SHEET_WIDTH // columns)


def compose_lut_compare_grid(
    entries: list[dict[str, Any]], columns: int, cell_width: int, background: str = "dark"
) -> np.ndarray:
    """Compose aspect-preserving BGR previews with labels and error cells."""
    width = effective_cell_width(columns, cell_width)
    if not 1 <= len(entries) <= MAX_ENTRIES:
        raise ValueError("LUT comparison requires 1..64 entries")
    preview_height = max(1, int(width * 0.75))
    strip = 48
    height = preview_height + strip
    color = (242, 242, 242) if background == "light" else (24, 24, 24)
    foreground = (24, 24, 24) if background == "light" else (230, 230, 230)
    canvas = np.full(
        (math.ceil(len(entries) / columns) * height, columns * width, 3), color, dtype=np.uint8
    )
    for index, entry in enumerate(entries):
        x, y = (index % columns) * width, (index // columns) * height
        cell = canvas[y : y + height, x : x + width]
        error = entry.get("error")
        path = entry.get("image_path")
        image = None
        if not error and path and Path(path).is_file():
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is not None:
            image_h, image_w = image.shape[:2]
            ratio = min(1.0, width / image_w, preview_height / image_h)
            resized = cv2.resize(
                image,
                (max(1, int(image_w * ratio)), max(1, int(image_h * ratio))),
                interpolation=cv2.INTER_AREA,
            )
            rh, rw = resized.shape[:2]
            top, left = (preview_height - rh) // 2, (width - rw) // 2
            cell[top : top + rh, left : left + rw] = resized
        else:
            cv2.putText(
                cell,
                "ERROR",
                (5, max(15, preview_height // 2)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                foreground,
                1,
                cv2.LINE_AA,
            )
        label = str(entry.get("label") or "")
        # Draw into the cell view so long text cannot spill into adjacent cells.
        cv2.putText(
            cell,
            label,
            (5, preview_height + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            foreground,
            1,
            cv2.LINE_AA,
        )
        if error or image is None:
            cv2.putText(
                cell,
                str(error or "Preview unavailable"),
                (5, preview_height + 36),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                foreground,
                1,
                cv2.LINE_AA,
            )
    return canvas
