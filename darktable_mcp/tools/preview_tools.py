"""Vision-rating workflow: extract auto-rotated previews and apply ratings via XMP sidecars.

Operates on raw files BEFORE they are imported into darktable's library, so it
never touches the SQLite DB and doesn't need the Lua API. Extracts previews,
writes XMP sidecars (xmp:Rating) next to the raws, and can launch the darktable
GUI on the directory — sidecars are picked up automatically on import.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..utils.errors import DarktableMCPError

logger = logging.getLogger(__name__)

# Extensions are matched case-insensitively: always compare `p.suffix.lower()`
# against this set, never `p.suffix`. A `.Nef` is a NEF.
RAW_EXTENSIONS = frozenset(
    {
        ".nef", ".nrw",           # Nikon
        ".cr2", ".cr3", ".crw",   # Canon
        ".arw", ".srf", ".sr2",   # Sony
        ".raf",                   # Fujifilm
        ".rw2",                   # Panasonic
        ".dng",                   # Adobe / generic
        ".orf",                   # Olympus
        ".pef",                   # Pentax
        ".srw",                   # Samsung
        ".rwl",                   # Leica
        ".erf",                   # Epson
        ".mrw",                   # Minolta
        ".x3f",                   # Sigma
        ".iiq",                   # Phase One
        ".3fr", ".fff",           # Hasselblad
        ".kdc", ".dcr",           # Kodak
        ".mos",                   # Leaf
    }
)

# Preference order when several raws share one stem (e.g. an in-camera DNG
# next to the native NEF). Anything not listed sorts after these, by name.
RAW_EXTENSION_PREFERENCE = (
    ".nef", ".cr3", ".cr2", ".arw", ".raf", ".rw2", ".orf", ".pef", ".dng",
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

# darktable emits the rating either as an element (`<xmp:Rating>3</xmp:Rating>`)
# or as an attribute on rdf:Description (`xmp:Rating="3"`). Match both, and work
# on bytes so everything outside the rating stays byte-identical.
_RATING_ELEMENT_RE = re.compile(rb"(<xmp:Rating>)\s*(-?\d+)\s*(</xmp:Rating>)")
_RATING_ATTR_RE = re.compile(rb"(xmp:Rating\s*=\s*\")(-?\d+)(\")")

# Presence of any of these in a sidecar means the image has already been
# through darktable, so darktable's library.db holds the authoritative copy.
_DARKTABLE_MARKERS = (
    b"xmlns:darktable",
    b"darktable:history",
    b"darktable:history_end",
    b"darktable:iop_order_list",
    b"darktable:xmp_version",
)

_UNRECOGNISED_SIDECAR = (
    "existing sidecar not recognised; refusing to overwrite (pass force=True to replace)"
)


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


def _raw_sort_key(path: Path) -> Tuple[int, str]:
    """Sort key ranking raws by format preference, then by filename."""
    ext = path.suffix.lower()
    try:
        rank = RAW_EXTENSION_PREFERENCE.index(ext)
    except ValueError:
        rank = len(RAW_EXTENSION_PREFERENCE)
    return rank, path.name


def _is_within(path: Path, base: Path) -> bool:
    """True when ``path`` is ``base`` or lives underneath it."""
    try:
        return path == base or path.is_relative_to(base)
    except (OSError, ValueError):  # pragma: no cover — defensive
        return False


def _iter_raw_files(source_dir: Path, exclude: Optional[Path] = None) -> List[Path]:
    """Recursively list the raw files under ``source_dir``, in sorted path order.

    ``import_from_camera`` writes one subdirectory per camera folder / card
    (``<dest>/store_00010001_DCIM_100NCD80/DSC_0001.NEF``) precisely because
    camera filenames repeat, so the scan must recurse or it finds nothing at
    all. Dot-entries are skipped — that covers the default ``.previews``
    output tree and the importer's ``.import.log`` — and ``exclude`` skips a
    user-supplied ``output_dir`` that happens to sit inside the source tree,
    so a second run never ingests its own output.
    """
    try:
        entries = list(source_dir.rglob("*"))
    except OSError as exc:
        raise DarktableMCPError(f"cannot scan source_dir {source_dir}: {exc}") from exc

    matches: List[Path] = []
    for path in entries:
        if path.suffix.lower() not in RAW_EXTENSIONS:
            continue
        try:
            rel = path.relative_to(source_dir)
        except ValueError:  # pragma: no cover — rglob can't leave the root
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        if exclude is not None and _is_within(path, exclude):
            continue
        if not path.is_file():
            continue
        matches.append(path)
    return sorted(matches, key=lambda p: p.parts)


def _raw_lookup_keys(source_dir: Path, path: Path) -> List[str]:
    """Keys a caller may use for ``path``: the bare stem and the relative path."""
    rel_key = path.relative_to(source_dir).with_suffix("").as_posix()
    return [path.stem] if rel_key == path.stem else [path.stem, rel_key]


def _index_raws(source_dir: Path, exclude: Optional[Path] = None) -> Dict[str, List[Path]]:
    """Index the raws under ``source_dir`` by bare stem AND by relative path.

    Built once per batch: a per-stem ``rglob`` would rescan the whole tree for
    every one of several hundred ratings.
    """
    index: Dict[str, List[Path]] = {}
    for path in _iter_raw_files(source_dir, exclude=exclude):
        for key in _raw_lookup_keys(source_dir, path):
            index.setdefault(key, []).append(path)
    return index


def _lookup_raws(index: Mapping[str, List[Path]], key: str) -> List[Path]:
    """Resolve a ratings key (bare stem or source-relative path) to raw paths."""
    normalized = Path(key).as_posix().lstrip("/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    matches = index.get(normalized)
    if matches is None and Path(normalized).suffix.lower() in RAW_EXTENSIONS:
        # Forgive a key that still carries its raw extension.
        matches = index.get(Path(normalized).with_suffix("").as_posix())
    return sorted(matches, key=_raw_sort_key) if matches else []


def _find_raws_for_stem(source_dir: Path, stem: str) -> List[Path]:
    """List every raw under ``source_dir`` matching ``stem``, best candidate first.

    ``stem`` is either a bare stem (``DSC_1234``) or a path relative to
    ``source_dir`` without the extension (``store_1_DCIM_100NCD80/DSC_1234``),
    which is how a caller disambiguates a stem that repeats across camera
    folders. Extension matching is case-insensitive, so ``DSC_1234.NEF`` is
    found on case-sensitive filesystems too. Several hits mean the key is
    ambiguous — same stem with two extensions, or the same filename in two
    subdirectories; callers must surface that rather than guess.
    """
    return _lookup_raws(_index_raws(source_dir), stem)


def _resolve_raw_for_stem(source_dir: Path, stem: str) -> Optional[Path]:
    """Find the best raw for ``stem`` under ``source_dir`` (recursive, case-insensitive)."""
    matches = _find_raws_for_stem(source_dir, stem)
    return matches[0] if matches else None


def _sample_umask() -> int:
    """Read the process umask by setting it to 0 and immediately putting it back.

    There is no read-only umask call on POSIX, so this is the only portable way
    to learn the value — and it is why the result must be cached rather than
    re-derived per write. See ``_NEW_FILE_MODE``.

    Returns:
        The umask in force when this ran.
    """
    umask = os.umask(0)
    os.umask(umask)
    return umask


# Mode for a sidecar we create from scratch, matching what a plain `open()`
# would have produced (0644 under the usual 022 umask) instead of `mkstemp`'s
# 0600. Sampled once, at import, on purpose: probing the umask blanks it
# process-wide for an instant, and this server is multithreaded — every MCP
# handler runs under `asyncio.to_thread` and `extract_previews` drives its own
# ThreadPoolExecutor — so a file or directory another thread creates inside
# that window (e.g. `out_path.parent.mkdir`) would land world-writable. Probing
# per write would reopen that race once per new sidecar, hundreds of times in a
# large batch. The trade: a host process that calls `os.umask()` after import
# is not tracked here. That is the right way round — a server changing its
# umask mid-flight is far rarer than concurrent writes.
_NEW_FILE_MODE = 0o666 & ~_sample_umask()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically via a temp file in the same directory.

    An interrupted run can then never leave a truncated sidecar behind: the
    original file survives untouched until ``os.replace`` swaps it in one step.
    The replacement keeps the original file's permissions (``mkstemp`` would
    otherwise silently tighten a 0644 sidecar to 0600); a brand-new sidecar
    gets ``_NEW_FILE_MODE``. Never touches the process umask.
    """
    try:
        mode: Optional[int] = path.stat().st_mode & 0o777
    except OSError:
        mode = None

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, _NEW_FILE_MODE if mode is None else mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover — defensive
            pass
        raise


def _patch_rating(existing: bytes, rating: int) -> Optional[bytes]:
    """Rewrite only the rating value inside an existing sidecar.

    Returns the patched bytes (everything else byte-identical), or None when no
    recognisable ``xmp:Rating`` element or attribute is present — in which case
    the caller must refuse to overwrite rather than guess.
    """
    value = str(rating).encode("ascii")
    for pattern in (_RATING_ELEMENT_RE, _RATING_ATTR_RE):
        patched, count = pattern.subn(
            lambda m: m.group(1) + value + m.group(3), existing, count=1
        )
        if count:
            return patched
    return None


def _has_darktable_history(existing: bytes) -> bool:
    """True when the sidecar carries darktable-namespace content.

    That is the strongest local signal that darktable already knows the image,
    and therefore that its library.db copy — not this sidecar — is what the
    lighttable will display.
    """
    return any(marker in existing for marker in _DARKTABLE_MARKERS)


def _thumb_to_image(rawpy_mod: Any, image_mod: Any, thumb: Any) -> Any:
    """Build a PIL image from a rawpy thumbnail, handling JPEG and BITMAP.

    ``rawpy.extract_thumb()`` returns either JPEG bytes (most cameras) or a
    numpy ndarray when ``thumb.format`` is ``ThumbFormat.BITMAP``. Feeding the
    ndarray to ``Image.open`` fails, so bitmaps go through ``Image.fromarray``.
    The result never aliases libraw-owned memory, so it stays valid after the
    ``rawpy.imread`` context closes.
    """
    fmt = getattr(thumb, "format", None)
    thumb_formats = getattr(rawpy_mod, "ThumbFormat", None)
    bitmap = getattr(thumb_formats, "BITMAP", None)
    if bitmap is not None and fmt == bitmap:
        return image_mod.fromarray(thumb.data).copy()
    return image_mod.open(io.BytesIO(thumb.data))


def _default_workers() -> int:
    """Thread count for the extraction pool: capped at 8, at least 1."""
    return max(1, min(8, os.cpu_count() or 1))


def extract_previews(
    source_dir: str | Path,
    output_dir: Optional[str | Path] = None,
    max_dim: int = 1024,
    thumb_dim: int = 384,
    overwrite: bool = False,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Extract auto-rotated JPEG previews from raw files for vision rating.

    For each raw file in ``source_dir``:
      * Pull the embedded JPEG via ``rawpy.extract_thumb()``.
      * Apply EXIF orientation so portrait shots render upright.
      * Resize to ``max_dim`` (longest edge); also write a ``thumb_dim`` thumb
        if ``thumb_dim > 0`` (set 0 to skip).
      * Capture a small EXIF summary (ISO, shutter, focal, aperture, datetime).

    The scan recurses. ``import_from_camera`` writes one subdirectory per
    camera folder / card, so a flat scan of an import destination would find
    zero raws and report a clean ``extracted: 0``. The output tree mirrors the
    source tree (``a/DSC_0001.NEF`` -> ``<out_dir>/a/DSC_0001.jpg``) because
    camera filenames repeat across folders; a flat output layout would let one
    preview silently overwrite another. Dot-entries — the default
    ``.previews`` tree, the importer's ``.import.log`` — and any ``output_dir``
    inside the source tree are skipped, so a re-run never ingests its own
    output.

    Args:
        source_dir: Directory holding the raw files (NEF/CR2/ARW/etc), scanned
            recursively.
        output_dir: Where to write JPEGs. Default: ``<source_dir>/.previews/``.
        max_dim: Longest-edge in pixels for the standard preview. Default 1024.
        thumb_dim: Longest-edge for the small first-pass thumb. 0 to skip.
        overwrite: Re-extract even if a preview file already exists.
        max_workers: Thread-pool size. Default ``min(8, os.cpu_count())``.
            The heavy lifting (libraw decode, Pillow resize) is native code
            that releases the GIL, so threads genuinely parallelise.

    Returns a dict with ``output_dir`` and ``items`` (one entry per raw file,
    in sorted path order) plus tallies. ``stem`` is kept for backward
    compatibility but is NOT unique across subdirectories — use ``rel_path``,
    or the absolute ``source`` / ``preview`` / ``thumb`` paths, to
    disambiguate. Per-file errors are reported per-item, never raised — one
    bad raw can't sink the batch.
    """
    rawpy, Image, ImageOps, pyexiv2 = _import_vision_libs()

    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise DarktableMCPError(f"source_dir is not a directory: {src}")

    out_dir = Path(output_dir).expanduser().resolve() if output_dir else (src / ".previews")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DarktableMCPError(f"cannot create output_dir {out_dir}: {exc}") from exc
    thumb_dir = out_dir / "thumb" if thumb_dim > 0 else None
    if thumb_dir is not None:
        try:
            thumb_dir.mkdir(exist_ok=True)
        except OSError as exc:
            raise DarktableMCPError(f"cannot create thumb dir {thumb_dir}: {exc}") from exc

    raws = _iter_raw_files(src, exclude=out_dir)

    def _extract_one(raw: Path) -> Tuple[Dict[str, Any], str]:
        """Extract one raw. Returns (item, status) with status in extracted/skipped/error."""
        # Mirror the source tree under the output dir. Camera filenames repeat
        # across folders and cards, so a flat `<out_dir>/<stem>.jpg` would let
        # `a/DSC_0001.NEF` and `b/DSC_0001.NEF` silently overwrite each other.
        rel_parent = raw.parent.relative_to(src)
        out_path = out_dir / rel_parent / f"{raw.stem}.jpg"
        thumb_path = (
            thumb_dir / rel_parent / f"{raw.stem}.jpg" if thumb_dir is not None else None
        )

        item: Dict[str, Any] = {
            "stem": raw.stem,
            "rel_path": raw.relative_to(src).as_posix(),
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
            return item, "skipped"

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if thumb_path is not None:
                thumb_path.parent.mkdir(parents=True, exist_ok=True)
            with rawpy.imread(str(raw)) as r:
                preview = _thumb_to_image(rawpy, Image, r.extract_thumb())
            try:
                # Auto-rotate based on EXIF orientation BEFORE resizing.
                rotated = ImageOps.exif_transpose(preview)
                rotated.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                rotated.save(out_path, "JPEG", quality=85)
                item["size"] = list(rotated.size)

                if thumb_path is not None:
                    small = rotated.copy()
                    small.thumbnail((thumb_dim, thumb_dim), Image.Resampling.LANCZOS)
                    small.save(thumb_path, "JPEG", quality=80)
            finally:
                try:
                    preview.close()
                except Exception:  # pragma: no cover — defensive
                    pass
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            return item, "error"
        return item, "extracted"

    workers = _default_workers() if max_workers is None else max(1, int(max_workers))
    if raws:
        with ThreadPoolExecutor(max_workers=min(workers, len(raws))) as pool:
            # executor.map preserves input order, so `items` stays sorted by
            # filename no matter which worker finishes first.
            results = list(pool.map(_extract_one, raws))
    else:
        results = []

    items = [item for item, _ in results]
    extracted = sum(1 for _, status in results if status == "extracted")
    skipped = sum(1 for _, status in results if status == "skipped")
    errors = sum(1 for _, status in results if status == "error")

    # Write per-item details to a JSONL side file so the MCP tool response
    # can stay short (one entry per file blew Claude's token budget at ~700+
    # NEFs). Agents pull the full data with the regular Read tool when they
    # actually need it.
    side_file = out_dir / ".extract_previews.jsonl"
    try:
        with side_file.open("w") as fh:
            for it in items:
                fh.write(json.dumps(it) + "\n")
    except OSError as exc:
        raise DarktableMCPError(
            f"cannot write details file {side_file}: {exc}. "
            "Is the output directory read-only?"
        ) from exc

    return {
        "output_dir": str(out_dir),
        "thumb_dir": str(thumb_dir) if thumb_dir is not None else None,
        "extracted": extracted,
        "skipped": skipped,
        "errors": errors,
        "side_file": str(side_file),
        "items": items,
    }


def apply_ratings_batch(
    source_dir: str | Path,
    ratings: Mapping[str, int],
    log: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Write XMP sidecars for a batch of ``{key: rating}`` pairs.

    The sidecar sits next to its raw, at ``<raw path>.xmp``. Rating range is
    ``[-1, 5]``: -1 = reject, 0 = unrated, 1-5 = stars. If ``log=True``,
    each rating is also appended to ``<source_dir>/ratings.jsonl`` so the
    history survives compaction and can be replayed/audited later.

    Raws are resolved recursively, because ``import_from_camera`` puts each
    camera folder / card in its own subdirectory. A key is either a bare stem
    (``DSC_0001``) or a path relative to ``source_dir`` without the extension
    (``store_00010001_DCIM_100NCD80/DSC_0001``). A bare stem that matches raws
    in more than one subdirectory is an error, never a guess: camera filenames
    repeat, and picking one would rate the wrong photo.

    An existing sidecar is never clobbered. A sidecar written by darktable
    holds the whole edit history, so when one is present only the rating value
    is rewritten in place (element ``<xmp:Rating>N</xmp:Rating>`` or attribute
    ``xmp:Rating="N"``) and every other byte is preserved. A sidecar with no
    recognisable rating is skipped with an error unless ``force=True``. Every
    write goes through a temp file + ``os.replace``, so an interrupted run
    cannot truncate a real sidecar.

    Args:
        source_dir: Directory holding the raw files (must already exist);
            searched recursively.
        ratings: Mapping of bare stem OR source-relative path to rating int.
        log: Append entries to ``<source_dir>/ratings.jsonl`` (default True).
        force: Replace an existing sidecar wholesale with the stub template,
            discarding any edit history it holds. Off by default.

    Returns a dict with ``applied``, ``errors``, ``log_path``, and ``items``.
    Each item carries ``sidecar``, ``raw``, ``action``
    (``created``/``updated``/``replaced``), ``already_in_darktable``, and
    ``error``. Per-item failures don't abort the batch.
    """
    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise DarktableMCPError(f"source_dir is not a directory: {src}")

    log_path = src / "ratings.jsonl" if log else None
    items: List[Dict[str, Any]] = []
    applied = errors = 0
    log_entries: List[str] = []
    now = time.time()
    # One recursive scan for the whole batch, not one per rating.
    index = _index_raws(src)

    for stem, rating in ratings.items():
        result: Dict[str, Any] = {
            "stem": stem,
            "rating": rating,
            "sidecar": None,
            "raw": None,
            "action": None,
            "already_in_darktable": False,
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

        candidates = _lookup_raws(index, stem)
        if not candidates:
            result["error"] = f"no raw file found for stem '{stem}'"
            errors += 1
            items.append(result)
            continue

        rel_names = [p.relative_to(src).as_posix() for p in candidates]
        if len({p.parent for p in candidates}) > 1:
            # Same filename in two camera folders / on two cards. Rating one of
            # them at random would silently rate the wrong photo.
            result["candidates"] = rel_names
            result["error"] = (
                f"stem '{stem}' matches raws in "
                f"{len({p.parent for p in candidates})} directories "
                f"({', '.join(rel_names)}); pass a path relative to source_dir "
                f"(e.g. '{rel_names[0].rsplit('.', 1)[0]}') instead of a bare stem"
            )
            errors += 1
            items.append(result)
            continue

        raw = candidates[0]
        result["raw"] = str(raw)
        result["rel_path"] = raw.relative_to(src).as_posix()
        if len(candidates) > 1:
            # Same directory, several extensions (raw + in-camera DNG): the
            # preference order picks one, but say which.
            result["ambiguous_raws"] = [p.name for p in candidates]

        sidecar = raw.parent / f"{raw.name}.xmp"
        try:
            existing = sidecar.read_bytes() if sidecar.exists() else None
        except OSError as exc:
            result["error"] = f"read failed: {exc}"
            errors += 1
            items.append(result)
            continue

        if existing is None:
            payload: Optional[bytes] = XMP_TEMPLATE.format(rating=r).encode("utf-8")
            action = "created"
        elif force:
            payload = XMP_TEMPLATE.format(rating=r).encode("utf-8")
            action = "replaced"
        else:
            payload = _patch_rating(existing, r)
            action = "updated"

        if existing is not None:
            result["already_in_darktable"] = _has_darktable_history(existing)

        if payload is None:
            result["error"] = _UNRECOGNISED_SIDECAR
            errors += 1
            items.append(result)
            continue

        try:
            _atomic_write_bytes(sidecar, payload)
        except OSError as exc:
            result["error"] = f"write failed: {exc}"
            errors += 1
            items.append(result)
            continue

        result["sidecar"] = str(sidecar)
        result["rating"] = r
        result["action"] = action
        items.append(result)
        applied += 1
        if log_path is not None:
            # `rel_path` disambiguates a stem that repeats across subdirectories.
            log_entries.append(
                json.dumps(
                    {
                        "stem": stem,
                        "rel_path": result["rel_path"],
                        "rating": r,
                        "ts": now,
                    }
                )
            )

    if log_path is not None and log_entries:
        try:
            with log_path.open("a") as fh:
                fh.write("\n".join(log_entries) + "\n")
        except OSError as exc:
            raise DarktableMCPError(
                f"cannot append to rating log {log_path}: {exc}. "
                "Is the directory read-only? (sidecars were already written)"
            ) from exc

    return {
        "applied": applied,
        "errors": errors,
        "log_path": str(log_path) if log_path is not None else None,
        "items": items,
    }


def format_extract_summary(result: Mapping[str, Any]) -> str:
    """Human-readable summary of ``extract_previews`` for tool TextContent."""
    item_count = len(result.get("items", []))
    lines = [
        f"output_dir: {result['output_dir']}",
        f"thumb_dir: {result.get('thumb_dir')}",
        f"extracted: {result['extracted']}, "
        f"skipped: {result['skipped']}, "
        f"errors: {result['errors']}",
        f"items: {item_count}",
    ]
    side_file = result.get("side_file")
    if side_file:
        lines.append(
            f"details: {side_file} (JSONL, one line per file: stem, paths, EXIF, size, error)"
        )
    return "\n".join(lines)


def format_ratings_summary(result: Mapping[str, Any]) -> str:
    """Human-readable summary of ``apply_ratings_batch`` for tool TextContent."""
    line = f"applied: {result['applied']}, errors: {result['errors']}"
    if result.get("log_path"):
        line += f"\nlog: {result['log_path']}"

    known = [
        it
        for it in result.get("items", [])
        if it.get("already_in_darktable") and not it.get("error")
    ]
    if known:
        names = ", ".join(str(it.get("stem")) for it in known[:10])
        if len(known) > 10:
            names += f", ... (+{len(known) - 10} more)"
        line += (
            f"\nWARNING: {len(known)} image(s) are already known to darktable "
            "(their sidecars carry darktable history). The rating was written to the "
            "sidecar, but darktable keeps using its own database copy and the "
            "lighttable will NOT show the new rating until you select those images "
            'and run "selected image(s) -> read sidecar files".'
            f"\n  affected: {names}"
        )

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


def _format_rating_label(lo: int, hi: int) -> str:
    """Format a rating range as a human-readable hint (e.g. '★★★★★', 'rejected', '★★ to ★★★★')."""
    def stars(n: int) -> str:
        if n == -1:
            return "rejected"
        if n == 0:
            return "unstarred"
        return "★" * n
    if lo == hi:
        return stars(lo)
    return f"{stars(lo)} to {stars(hi)}"


# darktable's collection-rule `data` field is the only Lua surface in
# API 9.6.0 that can drive the rating filter externally. The new
# filtering panel doesn't expose Lua bindings; the simple-mode filter
# library doesn't have a `rating` field.

def _rating_data_for(lo: int, hi: int) -> str:
    """Encode a rating range as the string darktable's RATING-rule data field expects."""
    if lo == hi:
        return str(lo)
    if lo == -1:
        return f"<={hi}"
    if hi == 5:
        return f">={lo}"
    return f"[{lo};{hi}]"


def _luacmd_collect_rating(data: str) -> str:
    """Emit a --luacmd snippet that adds a single RATING rule via collect.filter.

    The snippet uses `local dt = require("darktable")` because the global
    `darktable` is NOT exposed in --luacmd scope.
    """
    return (
        'local dt = require("darktable"); '
        'local r = dt.gui.libs.collect.new_rule(); '
        'r.item = "DT_COLLECTION_PROP_RATING"; '
        'r.mode = "DT_LIB_COLLECT_MODE_AND"; '
        f'r.data = "{data}"; '
        'dt.gui.libs.collect.filter({r})'
    )


def _build_filter_luacmd(lo: int, hi: int) -> Optional[str]:
    """Generate a --luacmd snippet that pre-applies the rating filter.

    Returns None when no rule is needed (full range -1..5).
    Otherwise returns a Lua snippet that calls
    ``dt.gui.libs.collect.filter`` with a single RATING rule whose `data`
    field encodes the requested range.
    """
    if lo == -1 and hi == 5:
        return None
    return _luacmd_collect_rating(_rating_data_for(lo, hi))


def build_darktable_command(
    source_dir: str | Path,
    rating: Optional[int] = None,
    rating_min: Optional[int] = None,
    rating_max: Optional[int] = None,
    darktable_path: str = "darktable",
) -> List[str]:
    """Build the ``darktable`` command line for opening a folder.

    The folder is registered as a film roll on first launch and any XMP
    sidecars are picked up automatically. We pin the active collection to
    "all film rolls" via ``--conf`` so a stale saved collection can't hide
    the folder being opened.

    When a rating filter is requested, the lighttable opens already
    filtered via ``darktable.gui.libs.collect.filter`` with a single
    ``DT_COLLECTION_PROP_RATING`` rule whose ``data`` field encodes the
    range: ``"N"`` for an exact rating, ``">=N"``/``"<=N"`` for open
    bounds, ``"[LO;HI]"`` for arbitrary inner ranges. The full range
    ``[-1, 5]`` skips the luacmd entirely.

    Returns a list suitable for ``subprocess.Popen``.
    """
    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise DarktableMCPError(f"source_dir is not a directory: {src}")

    rating_range = _normalize_rating_range(rating, rating_min, rating_max)

    cmd: List[str] = [
        darktable_path,
        "--conf", "plugins/lighttable/collect/num_rules=1",
        "--conf", f"plugins/lighttable/collect/item0={_DT_PROP_FILMROLL}",
        "--conf", "plugins/lighttable/collect/string0=%",
    ]

    if rating_range is not None:
        lua = _build_filter_luacmd(*rating_range)
        if lua is not None:
            cmd += ["--luacmd", lua]

    cmd.append(str(src))
    return cmd


# How long to watch a freshly spawned darktable before believing it started.
_LAUNCH_PROBE_SECONDS = 3.0
_LAUNCH_POLL_INTERVAL = 0.25
_LAUNCH_OUTPUT_CHARS = 800

# What darktable prints when the library.db lock is already held. Liveness
# alone is NOT enough to detect this, and the behaviour is platform-specific:
# darktable notices the lock, then tries to hand the files to the running
# instance over D-Bus. On Linux that handoff usually succeeds and the child
# exits 0 having opened the images in the existing window. On macOS there is
# no session D-Bus, the handoff fails with a GLib assertion, and the child
# then HANGS indefinitely — alive, but never showing anything. Observed on
# darktable 5.6.0. So we watch what the child says, not just whether it runs.
_LOCK_SIGNATURES = (
    "database is locked",
    "trying to open the images in the running instance",
    "the database lock file contains a pid",
)


def _drain_in_background(proc: Any, sink: Optional[List[str]] = None) -> None:
    """Consume a live child's stdout/stderr on daemon threads.

    Keeps the pipes readable without ever blocking the child on a full pipe
    buffer, and without closing the read end (which would hand darktable a
    SIGPIPE the first time it logged something). When `sink` is given, the
    first `_LAUNCH_OUTPUT_CHARS` of output are retained so the caller can
    tell a real launch from one that is quietly blocked on the lock.
    """
    lock = threading.Lock()

    def _pump(stream: Any) -> None:
        try:
            # readline, NOT read(n): a blocking read(8192) waits for the
            # full 8192 bytes, so a live child that logs only a couple of
            # hundred bytes (exactly the library-lock notice) would never
            # surface them. readline returns on each newline.
            for chunk in iter(stream.readline, b""):
                if not chunk:
                    break
                if sink is None:
                    continue
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", "replace")
                with lock:
                    if sum(len(c) for c in sink) < _LAUNCH_OUTPUT_CHARS:
                        sink.append(str(chunk))
        except (OSError, ValueError):  # pragma: no cover — stream torn down
            pass
        finally:
            try:
                stream.close()
            except Exception:  # pragma: no cover — defensive
                pass

    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        if stream is not None:
            threading.Thread(target=_pump, args=(stream,), daemon=True).start()


def _looks_blocked_by_lock(text: str) -> bool:
    """True when darktable's output says another instance holds the library."""
    lowered = text.lower()
    return any(sig in lowered for sig in _LOCK_SIGNATURES)


def _wait_for_launch(proc: Any, sink: Optional[List[str]] = None) -> Optional[int]:
    """Poll a freshly spawned process briefly.

    Returns its exit code, or None if still alive. Stops early once the
    child has announced that the library is locked, so a hung macOS child
    does not cost the full probe window.
    """
    deadline = time.monotonic() + _LAUNCH_PROBE_SECONDS
    code: Optional[int] = proc.poll()
    while code is None and time.monotonic() < deadline:
        if sink is not None and _looks_blocked_by_lock("".join(sink)):
            break
        time.sleep(_LAUNCH_POLL_INTERVAL)
        code = proc.poll()
    return code


def open_in_darktable(
    source_dir: str | Path,
    rating: Optional[int] = None,
    rating_min: Optional[int] = None,
    rating_max: Optional[int] = None,
    darktable_path: str = "darktable",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Spawn darktable on ``source_dir``, pre-applying a rating filter.

    Opening a folder via the CLI registers it as a film roll on first launch,
    and any XMP sidecars produced by ``apply_ratings_batch`` are picked up
    automatically. The lighttable opens already filtered via
    ``darktable.gui.libs.collect.filter`` with a single
    ``DT_COLLECTION_PROP_RATING`` rule whose ``data`` field encodes the
    range:

      * ``rating=N``                 exact (``data="N"``)
      * ``rating_min=N``             N or higher (``data=">=N"``)
      * ``rating_max=N``             N or lower (``data="<=N"``)
      * arbitrary inner range LO..HI (``data="[LO;HI]"``)
      * full ``[-1, 5]``             no filter (no luacmd emitted)

    Args:
        source_dir: Folder containing raw files (with XMP sidecars next to them).
        rating: Filter hint — exactly this rating (-1 reject, 0 unrated, 1-5 stars).
        rating_min: Filter hint — lower bound of a rating range (inclusive).
        rating_max: Filter hint — upper bound of a rating range (inclusive).
        darktable_path: Executable to launch. Default: ``darktable`` on PATH.
        dry_run: If True, return the built command without spawning.

    The spawned process is watched for ~1.5s before success is reported.
    darktable is single-instance — it holds a lock on
    ``~/.config/darktable/library.db`` — so a second launch while a session is
    open dies immediately. That case raises instead of claiming a launch.

    Returns dict with ``command``, ``pid`` (None on dry_run), ``launched``,
    ``dry_run``, and a ``filter_hint`` describing which filter to set (None
    when no rating args were passed). Raises ``DarktableMCPError`` if
    ``source_dir`` is invalid, the rating range is invalid, the darktable
    binary can't be found, or darktable exits instead of staying open.
    """
    cmd = build_darktable_command(
        source_dir,
        rating=rating,
        rating_min=rating_min,
        rating_max=rating_max,
        darktable_path=darktable_path,
    )

    rating_range = _normalize_rating_range(rating, rating_min, rating_max)
    filter_hint = _format_rating_label(*rating_range) if rating_range else None

    if dry_run:
        return {
            "command": cmd,
            "pid": None,
            "filter_hint": filter_hint,
            "launched": False,
            "dry_run": True,
        }

    if shutil.which(darktable_path) is None and not Path(darktable_path).is_file():
        raise DarktableMCPError(
            f"darktable executable not found: {darktable_path!r}. "
            "Install darktable or pass `darktable_path` explicitly."
        )

    # Detach so darktable survives the MCP tool call returning. Output goes to
    # pipes rather than DEVNULL so an immediate failure can be explained; a
    # healthy child's pipes are drained on background threads (see below).
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Start draining immediately: darktable is chatty at startup (it logs every
    # bundled style it imports), and an undrained 64KB pipe buffer would block
    # the child inside our own probe window.
    captured: List[str] = []
    _drain_in_background(proc, captured)

    exit_code = _wait_for_launch(proc, captured)
    detail = "".join(captured).strip()[-_LAUNCH_OUTPUT_CHARS:]
    blocked = _looks_blocked_by_lock(detail)

    if blocked:
        # A running session holds ~/.config/darktable/library.db. darktable
        # tries to hand the files to that instance over D-Bus; on Linux that
        # usually works and this child exits 0, on macOS it fails and the
        # child hangs. Either way this call did not open a new window, and a
        # hung child is ours to clean up.
        if exit_code is None:
            try:
                proc.kill()
            except Exception:  # pragma: no cover — defensive
                pass
        handed_off = exit_code == 0
        message = (
            "darktable is already running and holds the lock on "
            "~/.config/darktable/library.db, so this call did not open a new "
            "window. "
            + (
                "It handed the folder to the running session instead — look at "
                "the darktable window that is already open."
                if handed_off
                else "The handoff to the running session did not complete "
                "(no session D-Bus, which is normal on macOS), so nothing was "
                "opened. Switch to the darktable window that is already open "
                "and select the film roll there."
            )
            + " The bridge-based tools in this project need that session anyway."
        )
        return {
            "command": cmd,
            "pid": None,
            "filter_hint": filter_hint,
            "launched": False,
            "handed_off_to_running_instance": handed_off,
            "reason": message,
            "detail": detail,
            "dry_run": False,
        }

    if exit_code is not None:
        message = (
            f"darktable exited immediately (exit code {exit_code}) instead of "
            "staying open."
        )
        if detail:
            message += f"\ndarktable said:\n{detail}"
        raise DarktableMCPError(message)

    return {
        "command": cmd,
        "pid": proc.pid,
        "filter_hint": filter_hint,
        "launched": True,
        "dry_run": False,
    }


def format_open_summary(result: Mapping[str, Any]) -> str:
    """Human-readable summary of ``open_in_darktable`` for tool TextContent."""
    pid = result.get("pid")
    launched = result.get("launched", pid is not None)
    if result.get("dry_run") or (pid is None and launched is False and "reason" not in result):
        head = "command (dry run):"
    elif launched:
        head = f"darktable launched (pid={pid})"
    else:
        reason = result.get("reason") or (
            "the process exited immediately — darktable is probably already running "
            "and holding the library lock; switch to the existing window."
        )
        head = f"darktable did NOT launch: {reason}"
    lines = [head, " ".join(result["command"])]
    hint = result.get("filter_hint")
    if hint:
        applied = any("--luacmd" == a for a in result["command"])
        if applied:
            lines.append(f"Rating filter pre-applied: {hint}")
        else:
            lines.append(
                f"Rating range hint: {hint}. (Range pre-apply not yet "
                f"supported — open the lighttable's filter bar to refine.)"
            )
    return "\n".join(lines)


