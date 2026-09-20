"""get_contact_sheet: grid-of-thumbnails view over the current darktable
collection, for fast batch visual culling instead of opening each photo in
darkroom.

Collection listing comes from the Lua bridge (methods.get_collection_images,
see darktable_mcp.lua) -- this module only filters/sorts/paginates the
already-fetched list and handles thumbnail rendering + grid compositing, so
it stays testable without a running darktable/Bridge.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..darktable.cli_wrapper import CLIWrapper
from ..utils.errors import ExportError

FILTER_VALUES = ("all", "unrated", "rated", "rejected", "selected")
SORT_VALUES = ("filename", "image_id", "capture_time", "rating")
DIRECTION_VALUES = ("asc", "desc")
BACKGROUND_COLORS = {
    # BGR, matching the spec's hex swatches (#181818 / #F2F2F2).
    "dark": (24, 24, 24),
    "light": (242, 242, 242),
}

MAX_SHEET_WIDTH = 2400
CELL_PADDING = 14
TEXT_STRIP_HEIGHT = 40
RENDER_TIMEOUT = 30
THUMB_RENDER_WORKERS = 4


# ---- filter / sort / paginate (pure, no I/O) -------------------------------


def validate_offset(offset: Any) -> str | None:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return "INVALID_OFFSET"
    return None


def validate_limit(limit: Any) -> str | None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 64):
        return "INVALID_LIMIT"
    return None


def filter_items(items: list[dict[str, Any]], filter_name: str) -> list[dict[str, Any]]:
    if filter_name == "all":
        return list(items)
    if filter_name == "unrated":
        return [i for i in items if (i.get("rating") or 0) == 0]
    if filter_name == "rated":
        return [i for i in items if (i.get("rating") or 0) >= 1]
    if filter_name == "rejected":
        return [i for i in items if (i.get("rating") or 0) == -1]
    if filter_name == "selected":
        return [i for i in items if i.get("selected")]
    raise ValueError(f"unknown filter: {filter_name!r}")


def _sort_key(item: dict[str, Any], sort_name: str):
    if sort_name == "filename":
        return (item.get("filename") or "").lower()
    if sort_name == "image_id":
        return int(item.get("id") or 0)
    if sort_name == "capture_time":
        return item.get("capture_time") or ""
    if sort_name == "rating":
        return item.get("rating") or 0
    raise ValueError(f"unknown sort: {sort_name!r}")


def sort_items(items: list[dict[str, Any]], sort_name: str, direction: str) -> list[dict[str, Any]]:
    return sorted(items, key=lambda i: _sort_key(i, sort_name), reverse=(direction == "desc"))


def paginate(
    items: list[dict[str, Any]], offset: int, limit: int
) -> tuple[list[dict[str, Any]], int | None, bool]:
    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < total
    return page, (next_offset if has_more else None), has_more


# ---- thumbnail cache + render ----------------------------------------------


def cache_root() -> Path:
    run_dir = os.environ.get("DARKTABLE_MCP_RUN_DIR")
    if run_dir:
        base = Path(run_dir) / "cache-mcp" / "darktable-mcp"
    else:
        cache_home = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        base = Path(cache_home) / "darktable-mcp"
    return base


def _source_version_tag(source_path: str, xmp_path: str | None = None) -> str:
    """Track both source and selected sidecar with subsecond precision."""
    versions = []
    for name in (source_path, xmp_path or source_path + ".xmp"):
        path = Path(name)
        try:
            stat = path.stat()
            version = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            version = "missing"
        versions.append(f"{path.resolve()}:{version}")
    return ":".join(versions)


def thumb_cache_path(
    image_id: str, source_path: str, width: int, xmp_path: str | None = None
) -> Path:
    key = f"{image_id}:{_source_version_tag(source_path, xmp_path)}:{width}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_root() / "contact-sheet-thumbs" / f"{digest}.jpg"


def render_thumbnail_for(
    cli: CLIWrapper,
    image_id: str,
    source_path: str,
    width: int,
    worker_configdir: Path | None = None,
    xmp_path: str | None = None,
) -> Path:
    """Render (or reuse a cached render of) a width-capped JPEG for one source
    image via darktable-cli, respecting any XMP sidecar edit history next to
    it. Raises ExportError on failure -- caller marks that cell as errored
    rather than failing the whole sheet.

    `worker_configdir`, when given, is passed through as this export's
    `--configdir` instead of `cli`'s own -- see render_thumbnails() for why."""
    out_path = thumb_cache_path(image_id, source_path, width, xmp_path)
    if out_path.is_file():
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".tmp-{uuid.uuid4().hex}.jpg")
    written_path = tmp_path
    try:
        written_path = cli.export_image(
            input_path=Path(source_path),
            output_path=tmp_path,
            format_type="jpeg",
            quality=90,
            max_width=width,
            max_height=width * 8,  # width-bound only; aspect ratio preserved by darktable-cli
            timeout=RENDER_TIMEOUT,
            configdir=worker_configdir,
            xmp_path=Path(xmp_path) if xmp_path else None,
        )
        os.replace(written_path, out_path)
        return out_path
    finally:
        tmp_path.unlink(missing_ok=True)
        if written_path != tmp_path:
            written_path.unlink(missing_ok=True)


def render_thumbnails(
    cli: CLIWrapper, page_items: list[dict[str, Any]], width: int
) -> dict[str, tuple[Path | None, str | None]]:
    """Render every item's thumbnail in parallel (subprocess-per-image, so
    threads only wait on I/O). Returns image_id -> (path or None, error or
    None); a failed render never raises past here -- one bad photo must not
    take down the whole sheet (acceptance criterion #8).

    The wrapper assigns a private config directory to every live worker thread,
    including workers from overlapping sheet and batch calls. Its slot reuse
    bounds disk usage without sharing a database between active renders.
    """

    def _one(item: dict[str, Any]) -> tuple[str, Path | None, str | None]:
        image_id = str(item["id"])
        try:
            path = render_thumbnail_for(
                cli,
                image_id,
                item["path"],
                width,
                xmp_path=item.get("xmp_path") or item.get("sidecar"),
            )
            return image_id, path, None
        except ExportError as e:
            return image_id, None, str(e)
        except Exception as e:  # noqa: BLE001 - one bad file must not abort the sheet
            return image_id, None, str(e)

    results: dict[str, tuple[Path | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=THUMB_RENDER_WORKERS) as pool:
        for image_id, path, error in pool.map(_one, page_items):
            results[image_id] = (path, error)
    return results


# ---- grid compositing -------------------------------------------------------


def _text_color(background: str) -> tuple[int, int, int]:
    return (235, 235, 235) if background == "dark" else (20, 20, 20)


def _truncate_filename(filename: str, max_chars: int) -> str:
    if len(filename) <= max_chars:
        return filename
    return filename[: max_chars - 1] + "…"


def _rating_text(rating: int) -> str:
    if rating == -1:
        return "REJECT"
    if rating == 0:
        return "—"
    return "★ " + str(rating)


def _cell_image(
    thumb: np.ndarray | None,
    width: int,
    row_height: int,
    background_bgr: tuple[int, int, int],
    position: int,
    image_id: str,
    filename: str,
    rating: int,
    error: str | None,
    include_filename: bool,
    include_image_id: bool,
    include_rating: bool,
    include_sequence_number: bool,
) -> np.ndarray:
    cell_h = row_height + TEXT_STRIP_HEIGHT
    cell = np.full((cell_h, width, 3), background_bgr, dtype=np.uint8)
    text_color = _text_color("dark" if sum(background_bgr) < 384 else "light")

    if thumb is not None:
        th, tw = thumb.shape[:2]
        x_off = max(0, (width - tw) // 2)
        y_off = max(0, (row_height - th) // 2)
        cell[y_off : y_off + th, x_off : x_off + tw] = thumb
    else:
        msg = "PREVIEW ERROR"
        cv2.putText(
            cell,
            msg,
            (12, row_height // 2 - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (60, 60, 220),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            cell,
            f"ID: {image_id}",
            (12, row_height // 2 + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            text_color,
            1,
            cv2.LINE_AA,
        )

    if include_sequence_number:
        label = f"{position:02d}"
        cv2.rectangle(cell, (4, 4), (34, 26), (0, 0, 0), -1)
        cv2.putText(
            cell,
            label,
            (8, 21),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    line_y = row_height + 18
    if include_filename:
        max_chars = max(8, width // 8)
        cv2.putText(
            cell,
            _truncate_filename(filename, max_chars),
            (8, line_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            text_color,
            1,
            cv2.LINE_AA,
        )
        line_y += 18

    info_parts = []
    if include_image_id:
        info_parts.append(f"ID: {image_id}")
    if include_rating:
        info_parts.append(_rating_text(rating) if not error else "ERROR")
    if info_parts:
        cv2.putText(
            cell,
            "   ".join(info_parts),
            (8, line_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            text_color,
            1,
            cv2.LINE_AA,
        )

    return cell


def compose_sheet(
    page_items: list[dict[str, Any]],
    thumb_results: dict[str, tuple[Path | None, str | None]],
    columns: int,
    thumb_width: int,
    background: str,
    include_filename: bool,
    include_image_id: bool,
    include_rating: bool,
    include_sequence_number: bool,
) -> np.ndarray:
    background_bgr = BACKGROUND_COLORS[background]
    rows = -(-len(page_items) // columns)  # ceil

    loaded: dict[str, np.ndarray | None] = {}
    for item in page_items:
        image_id = str(item["id"])
        path, _err = thumb_results.get(image_id, (None, "not rendered"))
        img = cv2.imread(str(path), cv2.IMREAD_COLOR) if path else None
        if img is not None and img.shape[1] != thumb_width:
            scale = thumb_width / img.shape[1]
            img = cv2.resize(
                img,
                (thumb_width, max(1, int(img.shape[0] * scale))),
                interpolation=cv2.INTER_AREA,
            )
        loaded[image_id] = img

    row_heights = []
    for r in range(rows):
        chunk = page_items[r * columns : (r + 1) * columns]
        heights = [
            loaded[str(it["id"])].shape[0] for it in chunk if loaded[str(it["id"])] is not None
        ]
        row_heights.append(max(heights) if heights else int(thumb_width * 0.75))

    sheet_w = columns * thumb_width + (columns + 1) * CELL_PADDING
    sheet_h = sum(h + TEXT_STRIP_HEIGHT for h in row_heights) + (rows + 1) * CELL_PADDING
    canvas = np.full((sheet_h, sheet_w, 3), background_bgr, dtype=np.uint8)

    y = CELL_PADDING
    for r in range(rows):
        row_h = row_heights[r]
        chunk = page_items[r * columns : (r + 1) * columns]
        x = CELL_PADDING
        for i, item in enumerate(chunk):
            image_id = str(item["id"])
            _path, error = thumb_results.get(image_id, (None, "not rendered"))
            cell = _cell_image(
                loaded[image_id],
                thumb_width,
                row_h,
                background_bgr,
                position=r * columns + i + 1,
                image_id=image_id,
                filename=item.get("filename") or "",
                rating=item.get("rating") or 0,
                error=error,
                include_filename=include_filename,
                include_image_id=include_image_id,
                include_rating=include_rating,
                include_sequence_number=include_sequence_number,
            )
            canvas[y : y + cell.shape[0], x : x + cell.shape[1]] = cell
            x += thumb_width + CELL_PADDING
        y += row_h + TEXT_STRIP_HEIGHT + CELL_PADDING

    return canvas


def effective_thumb_width(columns: int, requested_width: int) -> int:
    """Shrink the per-thumbnail width up front (not the finished canvas) so
    MAX_SHEET_WIDTH is respected without blurring the identifier text -- a
    post-hoc resize of the whole sheet would shrink the text too."""
    max_by_width = (MAX_SHEET_WIDTH - (columns + 1) * CELL_PADDING) // columns
    return max(80, min(requested_width, max_by_width))


def write_sheet(canvas: np.ndarray, out_path: Path, quality: int = 90) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ExportError("failed to encode contact sheet JPEG")
    out_path.write_bytes(buf.tobytes())


def new_sheet_path() -> Path:
    return cache_root() / "contact-sheet-sheets" / f"sheet-{uuid.uuid4().hex}.jpg"
