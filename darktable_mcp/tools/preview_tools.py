"""Vision-rating workflow: extract auto-rotated previews and apply ratings via XMP sidecars.

This module is the "rate by vision" companion to PhotoTools. It works on raw
files BEFORE they're imported into darktable's library — so it never touches
the SQLite DB and doesn't need the Lua API. After ratings are applied, opening
the directory in darktable picks up the XMP sidecars on import.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..utils.errors import DarktableMCPError

logger = logging.getLogger(__name__)

RAW_EXTENSIONS = (
    ".NEF", ".nef",
    ".CR2", ".cr2", ".CR3", ".cr3",
    ".ARW", ".arw",
    ".RAF", ".raf",
    ".RW2", ".rw2",
    ".DNG", ".dng",
    ".ORF", ".orf",
    ".PEF", ".pef",
)

ISO_KEYS = (
    "Exif.Photo.ISOSpeedRatings",
    "Exif.Photo.PhotographicSensitivity",
    "Exif.Nikon3.ISOSettings",
    "Exif.Image.ISOSpeedRatings",
)

XMP_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="darktable-mcp preview_tools">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:darktable="http://darktable.sf.net/">
   <xmp:Rating>{rating}</xmp:Rating>
   <darktable:auto_presets_applied>0</darktable:auto_presets_applied>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def _import_vision_libs():
    """Lazy-import the optional [vision] deps with a helpful error."""
    try:
        import rawpy  # type: ignore[import-untyped]
        from PIL import Image, ImageOps  # type: ignore[import-untyped]
        import pyexiv2  # type: ignore[import-untyped]
    except ImportError as exc:
        raise DarktableMCPError(
            "Vision-rating tools require optional deps. Install with: "
            "pip install 'darktable-mcp[vision]'  "
            f"(missing: {exc.name})"
        ) from exc
    return rawpy, Image, ImageOps, pyexiv2


def _coerce_iso(value: Any) -> Optional[int]:
    """Coerce a raw EXIF ISO value into a sensible int; return None if unusable."""
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if isinstance(value, str):
        nums: List[int] = []
        for part in value.replace("/", " ").split():
            try:
                nums.append(int(part))
            except ValueError:
                continue
        nums = [n for n in nums if 50 <= n <= 102400]
        return max(nums) if nums else None
    try:
        n = int(value)
    except (ValueError, TypeError):
        return None
    return n if 50 <= n <= 102400 else None


def _parse_rational(value: Any) -> Optional[float]:
    """Parse pyexiv2 'a/b' rational-string into float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and "/" in value:
        try:
            num, den = value.split("/", 1)
            d = float(den)
            return float(num) / d if d else None
        except ValueError:
            return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _read_exif_summary(pyexiv2_mod: Any, raw_path: Path) -> Dict[str, Any]:
    """Read a small, useful EXIF summary from a raw file."""
    summary: Dict[str, Any] = {
        "iso": None,
        "shutter": None,
        "focal_mm": None,
        "aperture": None,
        "datetime": None,
    }
    img = None
    try:
        img = pyexiv2_mod.Image(str(raw_path))
        exif = img.read_exif() or {}
    except Exception as exc:  # pyexiv2 raises bare RuntimeError variants
        logger.debug("EXIF read failed for %s: %s", raw_path, exc)
        return summary
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:  # pragma: no cover — defensive
                pass

    for key in ISO_KEYS:
        if key in exif:
            iso = _coerce_iso(exif[key])
            if iso is not None:
                summary["iso"] = iso
                break

    summary["shutter"] = exif.get("Exif.Photo.ExposureTime") or exif.get(
        "Exif.Image.ExposureTime"
    )
    summary["focal_mm"] = _parse_rational(exif.get("Exif.Photo.FocalLength"))
    summary["aperture"] = _parse_rational(exif.get("Exif.Photo.FNumber"))
    summary["datetime"] = exif.get("Exif.Photo.DateTimeOriginal") or exif.get(
        "Exif.Image.DateTime"
    )
    return summary


def _resolve_raw_for_stem(source_dir: Path, stem: str) -> Optional[Path]:
    """Find the raw file for `stem` in `source_dir`, preferring uppercase NEF."""
    for ext in RAW_EXTENSIONS:
        candidate = source_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    return None


def extract_previews(
    source_dir: str | Path,
    output_dir: Optional[str | Path] = None,
    max_dim: int = 1024,
    thumb_dim: int = 384,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Extract auto-rotated JPEG previews from raw files for vision rating.

    For each raw file in ``source_dir``:
      * Pull the embedded JPEG via ``rawpy.extract_thumb()``.
      * Apply EXIF orientation so portrait shots render upright.
      * Resize to ``max_dim`` (longest edge); also write a ``thumb_dim`` thumb
        if ``thumb_dim > 0`` (set 0 to skip).
      * Capture a small EXIF summary (ISO, shutter, focal, aperture, datetime).

    Args:
        source_dir: Directory holding the raw files (NEF/CR2/ARW/etc).
        output_dir: Where to write JPEGs. Default: ``<source_dir>/.previews/``.
        max_dim: Longest-edge in pixels for the standard preview. Default 1024.
        thumb_dim: Longest-edge for the small first-pass thumb. 0 to skip.
        overwrite: Re-extract even if a preview file already exists.

    Returns a dict with ``output_dir`` and ``items`` (one entry per raw file)
    plus tallies. Preview paths are absolute strings, ready to feed straight
    into a Read tool call. Errors are reported per-item, never raised.
    """
    rawpy, Image, ImageOps, pyexiv2 = _import_vision_libs()

    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise DarktableMCPError(f"source_dir is not a directory: {src}")

    out_dir = Path(output_dir).expanduser().resolve() if output_dir else (src / ".previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir = out_dir / "thumb" if thumb_dim > 0 else None
    if thumb_dir is not None:
        thumb_dir.mkdir(exist_ok=True)

    raws = sorted(p for p in src.iterdir() if p.is_file() and p.suffix in RAW_EXTENSIONS)

    items: List[Dict[str, Any]] = []
    extracted = skipped = errors = 0

    for raw in raws:
        out_path = out_dir / f"{raw.stem}.jpg"
        thumb_path = thumb_dir / f"{raw.stem}.jpg" if thumb_dir is not None else None

        item: Dict[str, Any] = {
            "stem": raw.stem,
            "source": str(raw),
            "preview": str(out_path),
            "thumb": str(thumb_path) if thumb_path is not None else None,
            "exif": _read_exif_summary(pyexiv2, raw),
            "size": None,
            "error": None,
        }

        if not overwrite and out_path.exists() and out_path.stat().st_size > 0:
            try:
                with Image.open(out_path) as existing:
                    item["size"] = list(existing.size)
            except Exception:  # pragma: no cover
                item["size"] = None
            items.append(item)
            skipped += 1
            continue

        try:
            with rawpy.imread(str(raw)) as r:
                thumb = r.extract_thumb()
                data = thumb.data
            with Image.open(io.BytesIO(data)) as preview:
                # Auto-rotate based on EXIF orientation BEFORE resizing.
                rotated = ImageOps.exif_transpose(preview)
                rotated.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                rotated.save(out_path, "JPEG", quality=85)
                item["size"] = list(rotated.size)

                if thumb_path is not None:
                    small = rotated.copy()
                    small.thumbnail((thumb_dim, thumb_dim), Image.Resampling.LANCZOS)
                    small.save(thumb_path, "JPEG", quality=80)

            extracted += 1
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            errors += 1
        items.append(item)

    return {
        "output_dir": str(out_dir),
        "thumb_dir": str(thumb_dir) if thumb_dir is not None else None,
        "extracted": extracted,
        "skipped": skipped,
        "errors": errors,
        "items": items,
    }


def apply_ratings_batch(
    source_dir: str | Path,
    ratings: Mapping[str, int],
    log: bool = True,
) -> Dict[str, Any]:
    """Write XMP sidecars for a batch of ``{stem: rating}`` pairs.

    Sidecar path: ``<source_dir>/<stem>.<RAW_EXT>.xmp``. Rating range is
    ``[-1, 5]``: -1 = reject, 0 = unrated, 1-5 = stars. If ``log=True``,
    each rating is also appended to ``<source_dir>/ratings.jsonl`` so the
    history survives compaction and can be replayed/audited later.

    Args:
        source_dir: Directory holding the raw files (must already exist).
        ratings: Mapping of file stem to rating int.
        log: Append entries to ``<source_dir>/ratings.jsonl`` (default True).

    Returns a dict with ``applied``, ``errors``, ``log_path``, and ``items``
    (per-stem result with sidecar path or error). Per-item failures don't
    abort the batch.
    """
    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise DarktableMCPError(f"source_dir is not a directory: {src}")

    log_path = src / "ratings.jsonl" if log else None
    items: List[Dict[str, Any]] = []
    applied = errors = 0
    log_entries: List[str] = []
    now = time.time()

    for stem, rating in ratings.items():
        result: Dict[str, Any] = {
            "stem": stem,
            "rating": rating,
            "sidecar": None,
            "error": None,
        }
        try:
            r = int(rating)
        except (TypeError, ValueError):
            result["error"] = f"rating not int: {rating!r}"
            errors += 1
            items.append(result)
            continue
        if r < -1 or r > 5:
            result["error"] = f"rating out of range [-1, 5]: {r}"
            errors += 1
            items.append(result)
            continue

        raw = _resolve_raw_for_stem(src, stem)
        if raw is None:
            result["error"] = f"no raw file found for stem '{stem}'"
            errors += 1
            items.append(result)
            continue

        sidecar = src / f"{raw.name}.xmp"
        try:
            sidecar.write_text(XMP_TEMPLATE.format(rating=r))
        except OSError as exc:
            result["error"] = f"write failed: {exc}"
            errors += 1
            items.append(result)
            continue

        result["sidecar"] = str(sidecar)
        result["rating"] = r
        items.append(result)
        applied += 1
        if log_path is not None:
            log_entries.append(json.dumps({"stem": stem, "rating": r, "ts": now}))

    if log_path is not None and log_entries:
        with log_path.open("a") as fh:
            fh.write("\n".join(log_entries) + "\n")

    return {
        "applied": applied,
        "errors": errors,
        "log_path": str(log_path) if log_path is not None else None,
        "items": items,
    }


def format_extract_summary(result: Mapping[str, Any]) -> str:
    """Human-readable summary of ``extract_previews`` for tool TextContent."""
    return (
        f"output_dir: {result['output_dir']}\n"
        f"thumb_dir: {result.get('thumb_dir')}\n"
        f"extracted: {result['extracted']}, "
        f"skipped: {result['skipped']}, "
        f"errors: {result['errors']}\n"
        f"items: {len(result['items'])}"
    )


def format_ratings_summary(result: Mapping[str, Any]) -> str:
    """Human-readable summary of ``apply_ratings_batch`` for tool TextContent."""
    line = f"applied: {result['applied']}, errors: {result['errors']}"
    if result.get("log_path"):
        line += f"\nlog: {result['log_path']}"
    if result["errors"]:
        bad = [it for it in result["items"] if it.get("error")]
        line += "\nfailures:\n" + "\n".join(
            f"  {it['stem']}: {it['error']}" for it in bad[:10]
        )
        if len(bad) > 10:
            line += f"\n  ... and {len(bad) - 10} more"
    return line


# darktable filter property codes (from src/common/collection.h).
# 32 = DT_COLLECTION_PROP_RATING; 0 = DT_COLLECTION_PROP_FILMROLL.
_DT_PROP_RATING = 32
_DT_PROP_FILMROLL = 0


def _normalize_rating_range(
    rating: Optional[int],
    rating_min: Optional[int],
    rating_max: Optional[int],
) -> Optional[Tuple[int, int]]:
    """Return (lo, hi) for the rating filter, or None when no filter is requested.

    Accepts either a single ``rating`` (exact match) or a ``rating_min`` /
    ``rating_max`` range (either bound optional). Validates everything lands
    in the darktable-supported range ``[-1, 5]`` (-1 = reject).
    """
    if rating is not None and (rating_min is not None or rating_max is not None):
        raise DarktableMCPError(
            "Pass either `rating` or `rating_min`/`rating_max`, not both."
        )

    if rating is not None:
        lo = hi = int(rating)
    elif rating_min is None and rating_max is None:
        return None
    else:
        lo = int(rating_min) if rating_min is not None else -1
        hi = int(rating_max) if rating_max is not None else 5

    if lo < -1 or hi > 5 or lo > hi:
        raise DarktableMCPError(
            f"rating range out of bounds [-1, 5]: lo={lo}, hi={hi}"
        )
    return lo, hi


def build_darktable_command(
    source_dir: str | Path,
    rating: Optional[int] = None,
    rating_min: Optional[int] = None,
    rating_max: Optional[int] = None,
    darktable_path: str = "darktable",
) -> List[str]:
    """Build the ``darktable`` command line for a folder with an optional rating filter.

    Uses the darktable 5.x filtering system (``plugins/lighttable/filtering/*``).
    The 4.x-era ``plugins/collection/rating`` keys are NOT honored by the
    current GUI's filter bar, so we don't set them. The filter string format
    for rating is ``[lo;hi]`` (range, inclusive).

    Returns a list suitable for ``subprocess.Popen``.
    """
    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise DarktableMCPError(f"source_dir is not a directory: {src}")

    cmd: List[str] = [darktable_path]

    # Always pin the collection to "all film rolls" so an unrelated saved
    # collection doesn't hide the folder we're opening.
    cmd += [
        "--conf", f"plugins/lighttable/collect/num_rules=1",
        "--conf", f"plugins/lighttable/collect/item0={_DT_PROP_FILMROLL}",
        "--conf", "plugins/lighttable/collect/string0=%",
    ]

    rating_range = _normalize_rating_range(rating, rating_min, rating_max)
    if rating_range is not None:
        lo, hi = rating_range
        cmd += [
            "--conf", "plugins/lighttable/filtering/num_rules=1",
            "--conf", f"plugins/lighttable/filtering/item0={_DT_PROP_RATING}",
            "--conf", "plugins/lighttable/filtering/mode0=0",
            "--conf", f"plugins/lighttable/filtering/string0=[{lo};{hi}]",
            "--conf", "plugins/lighttable/filtering/off0=0",
        ]
    else:
        # Disable any leftover filtering rules so all images are visible.
        cmd += ["--conf", "plugins/lighttable/filtering/num_rules=0"]

    cmd.append(str(src))
    return cmd


def open_in_darktable(
    source_dir: str | Path,
    rating: Optional[int] = None,
    rating_min: Optional[int] = None,
    rating_max: Optional[int] = None,
    darktable_path: str = "darktable",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Spawn darktable on ``source_dir`` with the rating filter pre-applied.

    Opening a folder via the CLI registers it as a film roll on first launch,
    and the XMP sidecars produced by ``apply_ratings_batch`` are picked up
    automatically — so the lighttable view shows only the matching photos.
    The filter uses darktable 5.x's ``plugins/lighttable/filtering/*`` system;
    older ``plugins/collection/rating`` keys are intentionally not set because
    the modern filter bar overrides them.

    Args:
        source_dir: Folder containing raw files (with XMP sidecars next to them).
        rating: Filter to exactly this rating (-1 reject, 0 unrated, 1-5 stars).
        rating_min: Lower bound of a rating range (inclusive). Mutually exclusive with ``rating``.
        rating_max: Upper bound of a rating range (inclusive). Mutually exclusive with ``rating``.
        darktable_path: Executable to launch. Default: ``darktable`` on PATH.
        dry_run: If True, return the built command without spawning.

    Returns dict with ``command`` (list of args) and ``pid`` (None on dry_run).
    Raises ``DarktableMCPError`` if ``source_dir`` is invalid, the rating
    range is invalid, or the darktable binary can't be found.
    """
    cmd = build_darktable_command(
        source_dir,
        rating=rating,
        rating_min=rating_min,
        rating_max=rating_max,
        darktable_path=darktable_path,
    )

    if dry_run:
        return {"command": cmd, "pid": None}

    if shutil.which(darktable_path) is None and not Path(darktable_path).is_file():
        raise DarktableMCPError(
            f"darktable executable not found: {darktable_path!r}. "
            "Install darktable or pass `darktable_path` explicitly."
        )

    # Detach so darktable survives the MCP tool call returning.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"command": cmd, "pid": proc.pid}


def format_open_summary(result: Mapping[str, Any]) -> str:
    """Human-readable summary of ``open_in_darktable`` for tool TextContent."""
    pid = result.get("pid")
    head = f"darktable launched (pid={pid})" if pid else "command (dry run):"
    return head + "\n" + " ".join(result["command"])
