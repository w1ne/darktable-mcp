"""Tests for preview_tools (vision-rating workflow)."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from darktable_mcp.tools.preview_tools import (
    XMP_TEMPLATE,
    apply_ratings_batch,
    build_darktable_command,
    extract_previews,
    format_extract_summary,
    format_open_summary,
    format_ratings_summary,
    open_in_darktable,
)
from darktable_mcp.utils.errors import DarktableMCPError


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# A realistic darktable-written sidecar: namespace declarations, an edit
# history, and the rating as an element. Clobbering this destroys the edit.
DARKTABLE_SIDECAR = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:darktable="http://darktable.sf.net/">
   <xmp:Rating>2</xmp:Rating>
   <darktable:history_end>7</darktable:history_end>
   <darktable:iop_order_version>2</darktable:iop_order_version>
   <darktable:history>
    <rdf:Seq>
     <rdf:li darktable:operation="exposure" darktable:params="gz09eJxjY4A..."/>
     <rdf:li darktable:operation="colorbalancergb" darktable:params="gz11eJxj..."/>
    </rdf:Seq>
   </darktable:history>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""

# Some tools write the rating as an attribute instead of an element.
ATTRIBUTE_SIDECAR = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmp:Rating="1"
    xmp:Label="green"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


class TestApplyRatingsBatch:
    def test_writes_sidecar_with_rating(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0001.NEF")

        result = apply_ratings_batch(tmp_path, {"DSC_0001": 5}, log=False)

        sidecar = tmp_path / "DSC_0001.NEF.xmp"
        assert sidecar.exists()
        content = sidecar.read_text()
        assert "<xmp:Rating>5</xmp:Rating>" in content
        assert result["applied"] == 1
        assert result["errors"] == 0

    def test_rejects_out_of_range_rating(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0002.NEF")

        result = apply_ratings_batch(tmp_path, {"DSC_0002": 6}, log=False)

        assert result["applied"] == 0
        assert result["errors"] == 1
        assert "out of range" in result["items"][0]["error"]
        assert not (tmp_path / "DSC_0002.NEF.xmp").exists()

    def test_accepts_reject_rating(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0003.NEF")

        result = apply_ratings_batch(tmp_path, {"DSC_0003": -1}, log=False)

        assert result["applied"] == 1
        assert "<xmp:Rating>-1</xmp:Rating>" in (tmp_path / "DSC_0003.NEF.xmp").read_text()

    def test_missing_raw_reported_per_item(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0004.NEF")
        result = apply_ratings_batch(
            tmp_path, {"DSC_0004": 3, "DSC_NOPE": 4}, log=False
        )
        assert result["applied"] == 1
        assert result["errors"] == 1
        bad = next(it for it in result["items"] if it["stem"] == "DSC_NOPE")
        assert "no raw file" in bad["error"]

    def test_resolves_lowercase_and_other_raw_extensions(self, tmp_path: Path) -> None:
        _touch(tmp_path / "IMG_0001.cr2")
        _touch(tmp_path / "DSCF1234.RAF")

        result = apply_ratings_batch(
            tmp_path, {"IMG_0001": 4, "DSCF1234": 5}, log=False
        )
        assert result["applied"] == 2
        assert (tmp_path / "IMG_0001.cr2.xmp").exists()
        assert (tmp_path / "DSCF1234.RAF.xmp").exists()

    def test_jsonl_log_appended(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0010.NEF")
        _touch(tmp_path / "DSC_0011.NEF")

        apply_ratings_batch(tmp_path, {"DSC_0010": 3}, log=True)
        apply_ratings_batch(tmp_path, {"DSC_0011": 5}, log=True)

        log_path = tmp_path / "ratings.jsonl"
        assert log_path.exists()
        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        assert [(e["stem"], e["rating"]) for e in lines] == [
            ("DSC_0010", 3),
            ("DSC_0011", 5),
        ]
        assert all(isinstance(e["ts"], (int, float)) for e in lines)

    def test_log_disabled_writes_no_file(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0020.NEF")
        result = apply_ratings_batch(tmp_path, {"DSC_0020": 4}, log=False)
        assert result["log_path"] is None
        assert not (tmp_path / "ratings.jsonl").exists()

    def test_invalid_source_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DarktableMCPError):
            apply_ratings_batch(tmp_path / "does-not-exist", {"x": 3})

    def test_per_item_failure_does_not_abort_batch(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_A.NEF")
        _touch(tmp_path / "DSC_B.NEF")

        result = apply_ratings_batch(
            tmp_path,
            {"DSC_A": 4, "DSC_BAD": "not-int", "DSC_B": 2},  # type: ignore[dict-item]
            log=False,
        )
        assert result["applied"] == 2
        assert result["errors"] == 1

    def test_ambiguous_stem_records_which_raw_was_used(self, tmp_path: Path) -> None:
        _touch(tmp_path / "X.NEF")
        _touch(tmp_path / "X.dng")

        result = apply_ratings_batch(tmp_path, {"X": 3}, log=False)

        item = result["items"][0]
        assert item["raw"] == str(tmp_path / "X.NEF")  # NEF preferred over DNG
        assert set(item["ambiguous_raws"]) == {"X.NEF", "X.dng"}


class TestExistingSidecarIsNotClobbered:
    """An existing sidecar holds the whole edit history — never overwrite it."""

    def test_darktable_history_survives_and_rating_is_updated(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0100.NEF")
        sidecar = tmp_path / "DSC_0100.NEF.xmp"
        sidecar.write_text(DARKTABLE_SIDECAR)

        result = apply_ratings_batch(tmp_path, {"DSC_0100": 5}, log=False)

        content = sidecar.read_text()
        assert result["applied"] == 1
        assert result["errors"] == 0
        assert result["items"][0]["action"] == "updated"
        # Rating changed...
        assert "<xmp:Rating>5</xmp:Rating>" in content
        assert "<xmp:Rating>2</xmp:Rating>" not in content
        # ...and nothing else did.
        assert 'darktable:operation="exposure"' in content
        assert 'darktable:operation="colorbalancergb"' in content
        assert "<darktable:history_end>7</darktable:history_end>" in content
        assert content == DARKTABLE_SIDECAR.replace(
            "<xmp:Rating>2</xmp:Rating>", "<xmp:Rating>5</xmp:Rating>"
        )

    def test_attribute_form_rating_is_patched(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0101.NEF")
        sidecar = tmp_path / "DSC_0101.NEF.xmp"
        sidecar.write_text(ATTRIBUTE_SIDECAR)

        result = apply_ratings_batch(tmp_path, {"DSC_0101": 4}, log=False)

        content = sidecar.read_text()
        assert result["applied"] == 1
        assert 'xmp:Rating="4"' in content
        assert 'xmp:Label="green"' in content  # untouched

    def test_unparseable_sidecar_is_skipped_not_overwritten(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0102.NEF")
        sidecar = tmp_path / "DSC_0102.NEF.xmp"
        original = "<x>this is not an xmp sidecar we understand</x>\n"
        sidecar.write_text(original)

        result = apply_ratings_batch(tmp_path, {"DSC_0102": 5}, log=False)

        assert result["applied"] == 0
        assert result["errors"] == 1
        assert "refusing to overwrite" in result["items"][0]["error"]
        assert "force=True" in result["items"][0]["error"]
        assert sidecar.read_text() == original

    def test_force_replaces_existing_sidecar(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0103.NEF")
        sidecar = tmp_path / "DSC_0103.NEF.xmp"
        sidecar.write_text("<x>garbage</x>\n")

        result = apply_ratings_batch(tmp_path, {"DSC_0103": 1}, log=False, force=True)

        content = sidecar.read_text()
        assert result["applied"] == 1
        assert result["items"][0]["action"] == "replaced"
        assert "<xmp:Rating>1</xmp:Rating>" in content
        assert "garbage" not in content

    def test_force_also_replaces_a_darktable_sidecar(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0104.NEF")
        sidecar = tmp_path / "DSC_0104.NEF.xmp"
        sidecar.write_text(DARKTABLE_SIDECAR)

        apply_ratings_batch(tmp_path, {"DSC_0104": 0}, log=False, force=True)

        content = sidecar.read_text()
        assert "<xmp:Rating>0</xmp:Rating>" in content
        assert "colorbalancergb" not in content  # force is explicitly destructive

    def test_new_sidecar_still_uses_the_full_template(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0105.NEF")

        result = apply_ratings_batch(tmp_path, {"DSC_0105": 3}, log=False)

        assert result["items"][0]["action"] == "created"
        assert (tmp_path / "DSC_0105.NEF.xmp").read_text() == XMP_TEMPLATE.format(rating=3)

    def test_existing_sidecar_keeps_its_permissions(self, tmp_path: Path) -> None:
        # The atomic replace must not tighten a world-readable sidecar to 0600.
        _touch(tmp_path / "DSC_0107.NEF")
        sidecar = tmp_path / "DSC_0107.NEF.xmp"
        sidecar.write_text(DARKTABLE_SIDECAR)
        sidecar.chmod(0o644)

        apply_ratings_batch(tmp_path, {"DSC_0107": 5}, log=False)

        assert sidecar.stat().st_mode & 0o777 == 0o644

    def test_no_temp_files_left_behind(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0106.NEF")
        apply_ratings_batch(tmp_path, {"DSC_0106": 2}, log=False)
        assert [p.name for p in tmp_path.glob("*.tmp")] == []


class TestAlreadyInDarktableFlag:
    def test_darktable_sidecar_sets_the_flag(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0200.NEF")
        (tmp_path / "DSC_0200.NEF.xmp").write_text(DARKTABLE_SIDECAR)

        result = apply_ratings_batch(tmp_path, {"DSC_0200": 4}, log=False)

        assert result["items"][0]["already_in_darktable"] is True

    def test_fresh_photo_does_not_set_the_flag(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0201.NEF")

        result = apply_ratings_batch(tmp_path, {"DSC_0201": 4}, log=False)

        assert result["items"][0]["already_in_darktable"] is False

    def test_non_darktable_sidecar_does_not_set_the_flag(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0202.NEF")
        (tmp_path / "DSC_0202.NEF.xmp").write_text(ATTRIBUTE_SIDECAR)

        result = apply_ratings_batch(tmp_path, {"DSC_0202": 4}, log=False)

        assert result["items"][0]["already_in_darktable"] is False

    def test_summary_warns_about_the_database_copy(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0203.NEF")
        (tmp_path / "DSC_0203.NEF.xmp").write_text(DARKTABLE_SIDECAR)

        text = format_ratings_summary(apply_ratings_batch(tmp_path, {"DSC_0203": 4}, log=False))

        assert "WARNING" in text
        assert "database" in text
        assert "read sidecar files" in text
        assert "DSC_0203" in text

    def test_summary_has_no_warning_when_nothing_is_flagged(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0204.NEF")

        text = format_ratings_summary(apply_ratings_batch(tmp_path, {"DSC_0204": 4}, log=False))

        assert "WARNING" not in text


class TestExtractPreviews:
    def test_invalid_source_dir_raises(self, tmp_path: Path) -> None:
        # Will raise via _import_vision_libs OR via the dir check; both are DarktableMCPError.
        with pytest.raises(DarktableMCPError):
            extract_previews(tmp_path / "nope")

    def test_empty_source_returns_no_items(self, tmp_path: Path) -> None:
        # Skip if vision deps not installed in this env.
        pytest.importorskip("rawpy")
        pytest.importorskip("PIL")
        pytest.importorskip("pyexiv2")

        result = extract_previews(tmp_path)
        assert result["extracted"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0
        assert result["items"] == []
        assert (tmp_path / ".previews").is_dir()
        # Even on an empty source dir, a side file is created (empty).
        side = Path(result["side_file"])
        assert side.exists()
        assert side.read_text() == ""

    def test_side_file_has_one_jsonl_line_per_item(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The full per-file detail moved out of the function's return into a
        JSONL side file. Verify the file is exactly one parseable line per item.

        We bypass the vision deps by stubbing _import_vision_libs and seeding
        the items list manually — the JSONL writer doesn't care which path
        produced the items.
        """
        from darktable_mcp.tools import preview_tools as pt

        # Stub the vision libs so the function can run without rawpy/PIL.
        monkeypatch.setattr(
            pt, "_import_vision_libs", lambda: (None, None, None, None)
        )
        # Empty source dir → items list will be []; we need at least one fake
        # raw to exercise the per-file write path. Easiest: run twice — once
        # to produce an empty side file (covered above), and a second pass
        # where we synthesize items by patching iterdir.
        fake_raws = [tmp_path / "DSC_0001.NEF", tmp_path / "DSC_0002.NEF"]
        for r in fake_raws:
            r.write_bytes(b"\xff" * 10)

        # Force the read path to fail (no vision deps) so each item is
        # recorded with an error — that's still a valid per-item record.
        result = pt.extract_previews(tmp_path)
        side = Path(result["side_file"])
        lines = [json.loads(l) for l in side.read_text().splitlines()]
        assert len(lines) == 2
        assert {l["stem"] for l in lines} == {"DSC_0001", "DSC_0002"}
        # Each line should be a complete record with the expected keys.
        for entry in lines:
            assert {"stem", "source", "preview", "exif", "size", "error"} <= entry.keys()


# --- Fake vision libs -------------------------------------------------------
# The [vision] extras (rawpy, Pillow, pyexiv2) are optional and usually absent,
# so the extraction paths are exercised against these stand-ins.


class _FakePILImage:
    """Minimal stand-in for a PIL.Image.Image."""

    def __init__(self, size: tuple = (4000, 3000), origin: str = "jpeg") -> None:
        self.size = size
        self.origin = origin
        self.closed = False

    def copy(self) -> _FakePILImage:
        return _FakePILImage(self.size, self.origin)

    def thumbnail(self, box: tuple, resample=None) -> None:
        longest = max(box)
        width, height = self.size
        scale = min(1.0, longest / max(width, height))
        self.size = (int(width * scale), int(height * scale))

    def save(self, path, fmt=None, quality=None) -> None:
        Path(path).write_bytes(b"\xff\xd8fake-jpeg")

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _FakePILImage:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeImageModule:
    class Resampling:
        LANCZOS = "lanczos"

    def __init__(self) -> None:
        self.open_calls: list = []
        self.fromarray_calls: list = []

    def open(self, fp):
        self.open_calls.append(fp)
        if isinstance(fp, io.BytesIO):
            data = fp.getvalue()
            if not isinstance(data, bytes):  # pragma: no cover — guard
                raise OSError("cannot identify image file")
            return _FakePILImage(origin="jpeg")
        return _FakePILImage(origin="file")

    def fromarray(self, array):
        self.fromarray_calls.append(array)
        return _FakePILImage(size=(320, 240), origin="bitmap")


class _FakeImageOps:
    @staticmethod
    def exif_transpose(image):
        return image


class _FakeNdarray:
    """Stands in for the numpy array rawpy hands back for BITMAP thumbs."""

    shape = (240, 320, 3)


class _FakeThumb:
    def __init__(self, data, fmt) -> None:
        self.data = data
        self.format = fmt


class _FakeRawContext:
    def __init__(self, thumb, error: str | None, delay: float) -> None:
        self._thumb = thumb
        self._error = error
        self._delay = delay

    def __enter__(self) -> _FakeRawContext:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def extract_thumb(self):
        if self._delay:
            time.sleep(self._delay)
        if self._error:
            raise RuntimeError(self._error)
        return self._thumb


class _FakeRawpyModule:
    class ThumbFormat:
        JPEG = "thumb-jpeg"
        BITMAP = "thumb-bitmap"

    def __init__(
        self,
        fmt: str = "thumb-jpeg",
        errors: tuple = (),
        delays: dict | None = None,
    ) -> None:
        self.fmt = fmt
        self.errors = errors
        self.delays = delays or {}
        self.read: list = []

    def imread(self, path: str):
        self.read.append(path)
        stem = Path(path).stem
        data = _FakeNdarray() if self.fmt == self.ThumbFormat.BITMAP else b"\xff\xd8jpegbytes"
        error = f"unsupported file: {stem}" if stem in self.errors else None
        return _FakeRawContext(_FakeThumb(data, self.fmt), error, self.delays.get(stem, 0.0))


class _FakePyexiv2Module:
    """EXIF reads always fail here; _read_exif_summary swallows that by design."""

    @staticmethod
    def Image(path):  # noqa: N802 — mirrors pyexiv2's API
        raise RuntimeError("no exif in a fake raw")


def _install_fake_vision(monkeypatch, rawpy_mod) -> _FakeImageModule:
    """Point _import_vision_libs at the fakes and return the fake Image module."""
    from darktable_mcp.tools import preview_tools as pt

    image_mod = _FakeImageModule()
    monkeypatch.setattr(
        pt,
        "_import_vision_libs",
        lambda: (rawpy_mod, image_mod, _FakeImageOps, _FakePyexiv2Module),
    )
    return image_mod


class TestThumbnailFormats:
    def test_jpeg_thumb_goes_through_image_open(self, tmp_path: Path, monkeypatch) -> None:
        rawpy_mod = _FakeRawpyModule(fmt=_FakeRawpyModule.ThumbFormat.JPEG)
        image_mod = _install_fake_vision(monkeypatch, rawpy_mod)
        (tmp_path / "DSC_0001.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=0)

        assert result["extracted"] == 1
        assert result["errors"] == 0
        assert image_mod.fromarray_calls == []
        assert len(image_mod.open_calls) == 1
        assert Path(result["items"][0]["preview"]).exists()

    def test_bitmap_thumb_goes_through_image_fromarray(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # rawpy returns a numpy ndarray for ThumbFormat.BITMAP — Image.open
        # would choke on it, so it must take the fromarray path instead.
        rawpy_mod = _FakeRawpyModule(fmt=_FakeRawpyModule.ThumbFormat.BITMAP)
        image_mod = _install_fake_vision(monkeypatch, rawpy_mod)
        (tmp_path / "DSC_0002.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=0)

        item = result["items"][0]
        assert item["error"] is None
        assert result["extracted"] == 1
        assert len(image_mod.fromarray_calls) == 1
        assert isinstance(image_mod.fromarray_calls[0], _FakeNdarray)
        assert image_mod.open_calls == []  # never fed an ndarray
        assert item["size"] == [320, 240]

    def test_bitmap_thumb_still_writes_the_small_thumb(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        rawpy_mod = _FakeRawpyModule(fmt=_FakeRawpyModule.ThumbFormat.BITMAP)
        _install_fake_vision(monkeypatch, rawpy_mod)
        (tmp_path / "DSC_0003.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=128)

        assert Path(result["items"][0]["thumb"]).exists()


class TestRawExtensionMatching:
    def test_extension_set_is_lowercase_only(self) -> None:
        # Case-insensitive matching only holds if the set itself is lowercase
        # and every comparison goes through `.suffix.lower()`. macOS's
        # filesystem is case-insensitive, so this invariant — not a file on
        # disk — is what protects case-sensitive Linux boxes.
        from darktable_mcp.tools.preview_tools import RAW_EXTENSIONS

        assert all(ext == ext.lower() for ext in RAW_EXTENSIONS)
        assert {".srw", ".nrw", ".rwl", ".erf", ".mrw", ".x3f", ".iiq", ".3fr",
                ".kdc", ".mos"} <= set(RAW_EXTENSIONS)

    def test_find_raws_matches_regardless_of_case(self, tmp_path: Path) -> None:
        from darktable_mcp.tools.preview_tools import _find_raws_for_stem

        (tmp_path / "DSC_9999.NeF").write_bytes(b"raw")

        found = _find_raws_for_stem(tmp_path, "DSC_9999")

        assert [p.name for p in found] == ["DSC_9999.NeF"]

    def test_mixed_case_and_extra_formats_are_discovered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        names = [
            "a.NEF", "b.nef", "c.Nef",       # case must not matter
            "d.Cr2", "e.ARW", "f.srw",       # Samsung
            "g.nrw", "h.rwl", "i.erf",
            "j.mrw", "k.x3f", "l.iiq",
            "m.3fr", "n.kdc", "o.mos",
        ]
        for name in names:
            (tmp_path / name).write_bytes(b"raw")
        (tmp_path / "notes.txt").write_text("not a raw")
        (tmp_path / "preview.jpg").write_bytes(b"not a raw")

        result = extract_previews(tmp_path, thumb_dim=0)

        assert result["extracted"] == len(names)
        assert {it["stem"] for it in result["items"]} == {Path(n).stem for n in names}

    def test_uppercase_extension_resolves_for_ratings(self, tmp_path: Path) -> None:
        # `.Nef` used to be missed entirely by the explicit-variant tuple.
        _touch(tmp_path / "DSC_1234.Nef")
        result = apply_ratings_batch(tmp_path, {"DSC_1234": 3}, log=False)
        assert result["applied"] == 1
        assert (tmp_path / "DSC_1234.Nef.xmp").exists()

    def test_newly_supported_extension_resolves_for_ratings(self, tmp_path: Path) -> None:
        _touch(tmp_path / "SAM_0001.SRW")
        result = apply_ratings_batch(tmp_path, {"SAM_0001": 2}, log=False)
        assert result["applied"] == 1
        assert (tmp_path / "SAM_0001.SRW.xmp").exists()


class TestParallelExtraction:
    def test_items_keep_sorted_filename_order(self, tmp_path: Path, monkeypatch) -> None:
        # The first file is the slowest, so a completion-ordered result would
        # put it last. Order must follow the sorted filenames regardless.
        delays = {"DSC_0001": 0.20, "DSC_0002": 0.10, "DSC_0003": 0.0, "DSC_0004": 0.0}
        _install_fake_vision(monkeypatch, _FakeRawpyModule(delays=delays))
        for stem in delays:
            (tmp_path / f"{stem}.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=0, max_workers=4)

        assert [it["stem"] for it in result["items"]] == [
            "DSC_0001", "DSC_0002", "DSC_0003", "DSC_0004",
        ]
        # The JSONL side file follows the same order.
        raw_lines = Path(result["side_file"]).read_text().splitlines()
        entries = [json.loads(line) for line in raw_lines]
        assert [e["stem"] for e in entries] == [it["stem"] for it in result["items"]]

    def test_one_bad_raw_does_not_sink_the_batch(self, tmp_path: Path, monkeypatch) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule(errors=("DSC_0002",)))
        for stem in ("DSC_0001", "DSC_0002", "DSC_0003"):
            (tmp_path / f"{stem}.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=0, max_workers=4)

        assert result["extracted"] == 2
        assert result["errors"] == 1
        bad = next(it for it in result["items"] if it["stem"] == "DSC_0002")
        assert "unsupported file" in bad["error"]
        assert all(it["error"] is None for it in result["items"] if it["stem"] != "DSC_0002")

    def test_empty_source_dir_needs_no_pool(self, tmp_path: Path, monkeypatch) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule())

        result = extract_previews(tmp_path, thumb_dim=0)

        assert result["items"] == []
        assert (result["extracted"], result["skipped"], result["errors"]) == (0, 0, 0)
        assert Path(result["side_file"]).read_text() == ""

    def test_max_workers_one_still_works(self, tmp_path: Path, monkeypatch) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        (tmp_path / "DSC_0001.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=0, max_workers=1)

        assert result["extracted"] == 1

    def test_existing_previews_are_still_skipped(self, tmp_path: Path, monkeypatch) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        (tmp_path / "DSC_0001.NEF").write_bytes(b"raw")
        previews = tmp_path / ".previews"
        previews.mkdir()
        (previews / "DSC_0001.jpg").write_bytes(b"already here")

        result = extract_previews(tmp_path, thumb_dim=0)

        assert result["skipped"] == 1
        assert result["extracted"] == 0


class TestRecursiveExtraction:
    """`import_from_camera` writes one subdirectory per camera folder / card.

    A flat `iterdir()` scan of an import destination finds zero raws and
    reports `extracted: 0` as a success — the exact silent-success failure
    mode this pass exists to remove.
    """

    def test_raws_two_levels_down_are_found(self, tmp_path: Path, monkeypatch) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        deep = tmp_path / "store_00010001" / "DCIM_100NCD80"
        deep.mkdir(parents=True)
        (deep / "DSC_0001.NEF").write_bytes(b"raw")
        (deep / "DSC_0002.NEF").write_bytes(b"raw")
        (tmp_path / ".import.log").write_text("copied 2 files\n")

        result = extract_previews(tmp_path, thumb_dim=0)

        assert result["extracted"] == 2
        assert {it["rel_path"] for it in result["items"]} == {
            "store_00010001/DCIM_100NCD80/DSC_0001.NEF",
            "store_00010001/DCIM_100NCD80/DSC_0002.NEF",
        }

    def test_same_name_in_two_subdirs_gets_two_distinct_previews(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Dual-slot body: same filename, different photo. A flat output layout
        # would let one preview silently overwrite the other.
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        for card in ("store_00010001_DCIM_100NCD80", "store_00020001_DCIM_100NCD80"):
            (tmp_path / card).mkdir()
            (tmp_path / card / "DSC_0001.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=96)

        previews = [Path(it["preview"]) for it in result["items"]]
        thumbs = [Path(it["thumb"]) for it in result["items"]]
        assert result["extracted"] == 2
        assert len(set(previews)) == 2
        assert all(p.exists() for p in previews)
        assert all(t.exists() for t in thumbs)
        # The output tree mirrors the source tree.
        out = Path(result["output_dir"])
        assert (out / "store_00010001_DCIM_100NCD80" / "DSC_0001.jpg").exists()
        assert (out / "store_00020001_DCIM_100NCD80" / "DSC_0001.jpg").exists()
        assert (out / "thumb" / "store_00020001_DCIM_100NCD80" / "DSC_0001.jpg").exists()
        # `stem` stays for back-compat but is ambiguous; the paths are not.
        assert [it["stem"] for it in result["items"]] == ["DSC_0001", "DSC_0001"]
        assert len({it["source"] for it in result["items"]}) == 2

    def test_recursive_items_stay_in_sorted_path_order(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        delays = {"DSC_0001": 0.15, "DSC_0002": 0.0}
        _install_fake_vision(monkeypatch, _FakeRawpyModule(delays=delays))
        for card in ("card_b", "card_a"):
            (tmp_path / card).mkdir()
            for stem in delays:
                (tmp_path / card / f"{stem}.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=0, max_workers=4)

        assert [it["rel_path"] for it in result["items"]] == [
            "card_a/DSC_0001.NEF",
            "card_a/DSC_0002.NEF",
            "card_b/DSC_0001.NEF",
            "card_b/DSC_0002.NEF",
        ]

    def test_rerun_does_not_ingest_its_own_output(self, tmp_path: Path, monkeypatch) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        (tmp_path / "card_a").mkdir()
        (tmp_path / "card_a" / "DSC_0001.NEF").write_bytes(b"raw")

        first = extract_previews(tmp_path, thumb_dim=0)
        # Anything raw-suffixed landing in the output tree must stay invisible.
        (Path(first["output_dir"]) / "STRAY_0001.NEF").write_bytes(b"raw")
        second = extract_previews(tmp_path, thumb_dim=0)

        assert len(first["items"]) == 1
        assert len(second["items"]) == 1
        assert second["skipped"] == 1
        assert second["extracted"] == 0
        assert "STRAY_0001" not in {it["stem"] for it in second["items"]}

    def test_custom_output_dir_inside_source_is_excluded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A non-dot output_dir under source_dir would otherwise be re-scanned.
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        (tmp_path / "card_a").mkdir()
        (tmp_path / "card_a" / "DSC_0001.NEF").write_bytes(b"raw")
        out = tmp_path / "previews"
        out.mkdir()
        (out / "STRAY_0001.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, output_dir=out, thumb_dim=0)

        assert [it["stem"] for it in result["items"]] == ["DSC_0001"]

    def test_flat_layout_still_works(self, tmp_path: Path, monkeypatch) -> None:
        # Recursion must not break the pre-import flat case.
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        (tmp_path / "DSC_0001.NEF").write_bytes(b"raw")

        result = extract_previews(tmp_path, thumb_dim=0)

        assert result["extracted"] == 1
        assert result["items"][0]["rel_path"] == "DSC_0001.NEF"
        assert Path(result["items"][0]["preview"]).parent == Path(result["output_dir"])


class TestRecursiveRatingKeys:
    @staticmethod
    def _two_cards(tmp_path: Path) -> None:
        for card in ("store_00010001_DCIM_100NCD80", "store_00020001_DCIM_100NCD80"):
            (tmp_path / card).mkdir()
            _touch(tmp_path / card / "DSC_0001.NEF")

    def test_bare_stem_in_one_subdir_resolves(self, tmp_path: Path) -> None:
        card = tmp_path / "store_00010001_DCIM_100NCD80"
        card.mkdir()
        _touch(card / "DSC_0007.NEF")

        result = apply_ratings_batch(tmp_path, {"DSC_0007": 4}, log=False)

        assert result["applied"] == 1
        # The sidecar sits next to its raw, not at the source root.
        assert (card / "DSC_0007.NEF.xmp").exists()
        assert not (tmp_path / "DSC_0007.NEF.xmp").exists()
        assert result["items"][0]["rel_path"] == "store_00010001_DCIM_100NCD80/DSC_0007.NEF"

    def test_ambiguous_bare_stem_errors_instead_of_guessing(self, tmp_path: Path) -> None:
        self._two_cards(tmp_path)

        result = apply_ratings_batch(tmp_path, {"DSC_0001": 5}, log=False)

        item = result["items"][0]
        assert result["applied"] == 0
        assert result["errors"] == 1
        assert "matches raws in 2 directories" in item["error"]
        assert "store_00010001_DCIM_100NCD80/DSC_0001.NEF" in item["error"]
        assert "store_00020001_DCIM_100NCD80/DSC_0001.NEF" in item["error"]
        assert "relative to source_dir" in item["error"]
        assert item["candidates"] == [
            "store_00010001_DCIM_100NCD80/DSC_0001.NEF",
            "store_00020001_DCIM_100NCD80/DSC_0001.NEF",
        ]
        # Nothing was written for either candidate.
        assert list(tmp_path.rglob("*.xmp")) == []

    def test_relative_path_key_resolves_the_right_card(self, tmp_path: Path) -> None:
        self._two_cards(tmp_path)

        result = apply_ratings_batch(
            tmp_path, {"store_00020001_DCIM_100NCD80/DSC_0001": 5}, log=False
        )

        assert result["applied"] == 1
        assert result["errors"] == 0
        assert (tmp_path / "store_00020001_DCIM_100NCD80" / "DSC_0001.NEF.xmp").exists()
        assert not (tmp_path / "store_00010001_DCIM_100NCD80" / "DSC_0001.NEF.xmp").exists()

    def test_relative_path_key_may_carry_the_extension(self, tmp_path: Path) -> None:
        self._two_cards(tmp_path)

        result = apply_ratings_batch(
            tmp_path, {"store_00010001_DCIM_100NCD80/DSC_0001.NEF": 2}, log=False
        )

        assert result["applied"] == 1
        assert (tmp_path / "store_00010001_DCIM_100NCD80" / "DSC_0001.NEF.xmp").exists()

    def test_same_stem_two_extensions_in_one_dir_is_not_a_cross_dir_error(
        self, tmp_path: Path
    ) -> None:
        # One directory, two extensions: still resolvable via the preference
        # order, and reported via `ambiguous_raws` — not the cross-dir error.
        card = tmp_path / "card_a"
        card.mkdir()
        _touch(card / "DSC_0001.NEF")
        _touch(card / "DSC_0001.dng")

        result = apply_ratings_batch(tmp_path, {"DSC_0001": 3}, log=False)

        item = result["items"][0]
        assert result["applied"] == 1
        assert item["error"] is None
        assert set(item["ambiguous_raws"]) == {"DSC_0001.NEF", "DSC_0001.dng"}
        assert "candidates" not in item

    def test_log_records_the_relative_path(self, tmp_path: Path) -> None:
        card = tmp_path / "card_a"
        card.mkdir()
        _touch(card / "DSC_0009.NEF")

        apply_ratings_batch(tmp_path, {"DSC_0009": 1}, log=True)

        entries = [
            json.loads(line)
            for line in (tmp_path / "ratings.jsonl").read_text().splitlines()
            if line
        ]
        assert entries[0]["rel_path"] == "card_a/DSC_0009.NEF"
        assert entries[0]["stem"] == "DSC_0009"

    def test_missing_stem_still_reports_cleanly(self, tmp_path: Path) -> None:
        (tmp_path / "card_a").mkdir()
        _touch(tmp_path / "card_a" / "DSC_0001.NEF")

        result = apply_ratings_batch(tmp_path, {"card_b/DSC_0001": 3}, log=False)

        assert result["errors"] == 1
        assert "no raw file" in result["items"][0]["error"]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions"
)
class TestReadOnlyOutputDir:
    def test_unwritable_output_dir_raises_darktable_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        (tmp_path / "DSC_0001.NEF").write_bytes(b"raw")
        out = tmp_path / "out"
        out.mkdir()
        out.chmod(0o500)
        try:
            with pytest.raises(DarktableMCPError) as excinfo:
                extract_previews(tmp_path, output_dir=out, thumb_dim=0)
        finally:
            out.chmod(0o700)
        assert "read-only" in str(excinfo.value) or "cannot" in str(excinfo.value)

    def test_missing_uncreatable_output_dir_raises_darktable_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _install_fake_vision(monkeypatch, _FakeRawpyModule())
        parent = tmp_path / "locked"
        parent.mkdir()
        parent.chmod(0o500)
        try:
            with pytest.raises(DarktableMCPError) as excinfo:
                extract_previews(tmp_path, output_dir=parent / "out")
        finally:
            parent.chmod(0o700)
        assert "cannot create output_dir" in str(excinfo.value)

    def test_unwritable_sidecar_is_a_per_item_error(self, tmp_path: Path) -> None:
        src = tmp_path / "raws"
        src.mkdir()
        _touch(src / "DSC_0001.NEF")
        src.chmod(0o500)
        try:
            result = apply_ratings_batch(src, {"DSC_0001": 4}, log=False)
        finally:
            src.chmod(0o700)
        assert result["applied"] == 0
        assert result["errors"] == 1
        assert "write failed" in result["items"][0]["error"]


class TestRatingLogFailure:
    def test_unwritable_log_raises_darktable_error(self, tmp_path: Path) -> None:
        _touch(tmp_path / "DSC_0001.NEF")
        # A directory where the log file should be: the append can't succeed.
        (tmp_path / "ratings.jsonl").mkdir()

        with pytest.raises(DarktableMCPError) as excinfo:
            apply_ratings_batch(tmp_path, {"DSC_0001": 4}, log=True)

        assert "rating log" in str(excinfo.value)
        # The sidecar itself was still written before the log blew up.
        assert (tmp_path / "DSC_0001.NEF.xmp").exists()


class TestFormatHelpers:
    def test_extract_summary_includes_counts(self) -> None:
        text = format_extract_summary(
            {
                "output_dir": "/tmp/p",
                "thumb_dir": "/tmp/p/thumb",
                "extracted": 3,
                "skipped": 1,
                "errors": 0,
                "items": [{}, {}, {}, {}],
            }
        )
        assert "/tmp/p" in text
        assert "extracted: 3" in text
        assert "skipped: 1" in text

    def test_extract_summary_mentions_side_file_when_present(self) -> None:
        text = format_extract_summary(
            {
                "output_dir": "/tmp/p",
                "thumb_dir": None,
                "extracted": 5,
                "skipped": 0,
                "errors": 0,
                "side_file": "/tmp/p/.extract_previews.jsonl",
                "items": [],
            }
        )
        assert ".extract_previews.jsonl" in text
        assert "details:" in text or "side_file" in text or "JSONL" in text

    def test_extract_summary_back_compat_without_side_file(self) -> None:
        # Old callers that don't set side_file still get a clean summary.
        text = format_extract_summary(
            {
                "output_dir": "/tmp/p",
                "thumb_dir": None,
                "extracted": 1,
                "skipped": 0,
                "errors": 0,
                "items": [{}],
            }
        )
        assert "extracted: 1" in text
        assert ".jsonl" not in text  # no side file mentioned when absent

    def test_ratings_summary_lists_failures(self) -> None:
        text = format_ratings_summary(
            {
                "applied": 2,
                "errors": 1,
                "log_path": "/tmp/r/ratings.jsonl",
                "items": [
                    {"stem": "A", "error": None},
                    {"stem": "B", "error": None},
                    {"stem": "C", "error": "no raw file"},
                ],
            }
        )
        assert "applied: 2" in text
        assert "errors: 1" in text
        assert "C: no raw file" in text
        assert "ratings.jsonl" in text


class TestXmpTemplate:
    def test_template_has_rating_placeholder(self) -> None:
        out = XMP_TEMPLATE.format(rating=4)
        assert "<xmp:Rating>4</xmp:Rating>" in out
        assert "auto_presets_applied>0" in out


class TestBuildDarktableCommand:
    def test_command_pins_collection_to_all_film_rolls(self, tmp_path: Path) -> None:
        # Even without a rating filter, we always set the collect rules so a
        # stale saved collection can't hide the folder being opened.
        cmd = build_darktable_command(tmp_path)
        joined = " ".join(cmd)
        assert "plugins/lighttable/collect/num_rules=1" in joined
        assert "plugins/lighttable/collect/item0=0" in joined
        assert "plugins/lighttable/collect/string0=%" in joined
        assert cmd[-1] == str(tmp_path)
        assert "--luacmd" not in cmd  # no filter requested

    def _luacmd(self, cmd: list[str]) -> str:
        assert "--luacmd" in cmd, "expected --luacmd in command"
        return cmd[cmd.index("--luacmd") + 1]

    def test_invalid_source_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DarktableMCPError):
            build_darktable_command(tmp_path / "nope", rating=5)

    def test_rejects_combined_rating_and_range(self, tmp_path: Path) -> None:
        with pytest.raises(DarktableMCPError):
            build_darktable_command(tmp_path, rating=5, rating_min=4)

    def test_rejects_range_out_of_bounds(self, tmp_path: Path) -> None:
        with pytest.raises(DarktableMCPError):
            build_darktable_command(tmp_path, rating=6)
        with pytest.raises(DarktableMCPError):
            build_darktable_command(tmp_path, rating_min=4, rating_max=3)


class TestRatingFilterEmission:
    """Tests for _build_filter_luacmd with the new collect.filter shape.

    The emitted snippet must:
    - start with `local dt = require("darktable")` (the global isn't exposed in --luacmd scope)
    - call `dt.gui.libs.collect.new_rule()` and set item to DT_COLLECTION_PROP_RATING
    - set the rule's `data` field to the encoded rating string
    - call `dt.gui.libs.collect.filter({r})` to apply
    """

    def test_exact_rating_emits_data_n(self, tmp_path: Path) -> None:
        cmd = build_darktable_command(tmp_path, rating=5)
        lua = self._luacmd(cmd)
        assert 'local dt = require("darktable")' in lua
        assert 'r.item = "DT_COLLECTION_PROP_RATING"' in lua
        assert 'r.data = "5"' in lua
        assert "dt.gui.libs.collect.filter({r})" in lua
        # No reference to the broken old API.
        assert "filter.rating" not in lua
        assert "rating_comparator" not in lua

    def test_reject_emits_data_minus_1(self, tmp_path: Path) -> None:
        cmd = build_darktable_command(tmp_path, rating=-1)
        lua = self._luacmd(cmd)
        assert 'r.data = "-1"' in lua

    def test_unstarred_emits_data_0(self, tmp_path: Path) -> None:
        cmd = build_darktable_command(tmp_path, rating=0)
        lua = self._luacmd(cmd)
        assert 'r.data = "0"' in lua

    def test_rating_min_emits_geq(self, tmp_path: Path) -> None:
        cmd = build_darktable_command(tmp_path, rating_min=4)
        lua = self._luacmd(cmd)
        assert 'r.data = ">=4"' in lua

    def test_rating_max_emits_leq(self, tmp_path: Path) -> None:
        cmd = build_darktable_command(tmp_path, rating_max=2)
        lua = self._luacmd(cmd)
        assert 'r.data = "<=2"' in lua

    def test_rating_full_range_emits_no_luacmd(self, tmp_path: Path) -> None:
        cmd = build_darktable_command(tmp_path, rating_min=-1, rating_max=5)
        assert "--luacmd" not in cmd

    def test_rating_not_rejected_emits_geq_0(self, tmp_path: Path) -> None:
        cmd = build_darktable_command(tmp_path, rating_min=0, rating_max=5)
        lua = self._luacmd(cmd)
        assert 'r.data = ">=0"' in lua

    def test_inner_range_emits_bracket_semicolon(self, tmp_path: Path) -> None:
        cmd = build_darktable_command(tmp_path, rating_min=2, rating_max=4)
        lua = self._luacmd(cmd)
        assert 'r.data = "[2;4]"' in lua

    def test_inner_range_2_to_2_emits_exact(self, tmp_path: Path) -> None:
        # rating_min == rating_max collapses to exact rating.
        cmd = build_darktable_command(tmp_path, rating_min=3, rating_max=3)
        lua = self._luacmd(cmd)
        assert 'r.data = "3"' in lua

    @staticmethod
    def _luacmd(cmd: list) -> str:
        idx = cmd.index("--luacmd")
        return cmd[idx + 1]


class TestOpenInDarktable:
    def test_dry_run_returns_command_and_filter_hint(self, tmp_path: Path) -> None:
        result = open_in_darktable(tmp_path, rating=5, dry_run=True)
        assert result["pid"] is None
        assert result["filter_hint"] == "★★★★★"
        assert str(tmp_path) in result["command"]
        # Exact rating → --luacmd populated.
        assert "--luacmd" in result["command"]

    def test_dry_run_no_rating_returns_no_hint(self, tmp_path: Path) -> None:
        result = open_in_darktable(tmp_path, dry_run=True)
        assert result["filter_hint"] is None

    def test_dry_run_rating_range_hint(self, tmp_path: Path) -> None:
        result = open_in_darktable(tmp_path, rating_min=4, rating_max=5, dry_run=True)
        assert result["filter_hint"] == "★★★★ to ★★★★★"

    def test_dry_run_reject_hint(self, tmp_path: Path) -> None:
        result = open_in_darktable(tmp_path, rating=-1, dry_run=True)
        assert result["filter_hint"] == "rejected"

    def test_missing_executable_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DarktableMCPError):
            open_in_darktable(
                tmp_path,
                rating=5,
                darktable_path="/nonexistent/darktable-binary",
            )


class _FakePopen:
    """Stand-in for subprocess.Popen with a scripted exit code.

    stdout/stderr are real readable streams, because that is where the
    library-lock notice actually arrives: darktable stays *alive* after a
    failed handoff on macOS, so liveness tells us nothing and the only
    signal is what it wrote to the pipe.
    """

    def __init__(self, exit_code: int | None, out: bytes = b"", err: bytes = b"") -> None:
        self.pid = 4242
        self._exit_code = exit_code
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self.communicate_calls = 0
        self.killed = False

    def poll(self):
        return self._exit_code

    def kill(self):
        self.killed = True
        self._exit_code = -9

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        return b"", b""


class TestLaunchVerification:
    """darktable is single-instance: a blocked launch must not read as success."""

    @staticmethod
    def _patch(monkeypatch, proc: _FakePopen) -> None:
        from darktable_mcp.tools import preview_tools as pt

        monkeypatch.setattr(pt.shutil, "which", lambda name: "/usr/bin/darktable")
        monkeypatch.setattr(pt.subprocess, "Popen", lambda *a, **k: proc)
        # Keep the probe window short so tests don't idle for 1.5s.
        monkeypatch.setattr(pt, "_LAUNCH_PROBE_SECONDS", 0.05)
        monkeypatch.setattr(pt, "_LAUNCH_POLL_INTERVAL", 0.01)

    def test_lock_notice_while_child_stays_alive_is_not_a_launch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The macOS case, verified against darktable 5.6.0.

        darktable sees the lock, fails to hand off over D-Bus (there is no
        session bus), and then HANGS. `poll()` returns None forever, so a
        liveness-only probe reports a successful launch and a pid for a
        process that will never show a window.
        """
        proc = _FakePopen(
            exit_code=None,
            out=(
                b"[init] the database lock file contains a pid that seems to be alive"
                b" in your system: 48712\n"
                b"[init] database is locked, probably another process is already"
                b" using it\n"
                b"[dt_init] trying to open the images in the running instance\n"
            ),
        )
        self._patch(monkeypatch, proc)

        result = open_in_darktable(tmp_path, rating=5)

        assert result["launched"] is False
        assert result["pid"] is None
        assert result["handed_off_to_running_instance"] is False
        assert "already running" in result["reason"]
        # A hung child we spawned is ours to clean up.
        assert proc.killed is True

    def test_lock_notice_with_clean_exit_reports_handoff(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The Linux case: the handoff succeeds and the images open in the running window."""
        proc = _FakePopen(
            exit_code=0,
            out=b"[dt_init] trying to open the images in the running instance\n",
        )
        self._patch(monkeypatch, proc)

        result = open_in_darktable(tmp_path, rating=5)

        assert result["launched"] is False
        assert result["handed_off_to_running_instance"] is True
        assert "handed the folder to the running session" in result["reason"]
        assert proc.killed is False

    def test_immediate_exit_without_lock_notice_still_raises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        proc = _FakePopen(exit_code=1, err=b"some other startup failure\n")
        self._patch(monkeypatch, proc)

        with pytest.raises(DarktableMCPError) as excinfo:
            open_in_darktable(tmp_path, rating=5)

        message = str(excinfo.value)
        assert "exited immediately" in message
        assert "exit code 1" in message
        # The captured output is surfaced, not swallowed.
        assert "some other startup failure" in message

    def test_immediate_exit_zero_also_reports_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # darktable can bail with status 0 too; a dead child is a dead child.
        self._patch(monkeypatch, _FakePopen(exit_code=0))

        with pytest.raises(DarktableMCPError) as excinfo:
            open_in_darktable(tmp_path)

        assert "exited immediately" in str(excinfo.value)

    def test_live_process_reports_success(self, tmp_path: Path, monkeypatch) -> None:
        proc = _FakePopen(exit_code=None)
        self._patch(monkeypatch, proc)

        result = open_in_darktable(tmp_path, rating_min=4)

        assert result["launched"] is True
        assert result["pid"] == 4242
        assert result["dry_run"] is False
        assert result["filter_hint"] == "★★★★ to ★★★★★"
        # Never call communicate() on a live child — that would block forever.
        assert proc.communicate_calls == 0
        assert "pid=4242" in format_open_summary(result)

    def test_dry_run_never_spawns(self, tmp_path: Path, monkeypatch) -> None:
        from darktable_mcp.tools import preview_tools as pt

        def _boom(*args, **kwargs):  # pragma: no cover — must not be reached
            raise AssertionError("dry_run must not spawn a process")

        monkeypatch.setattr(pt.subprocess, "Popen", _boom)

        result = open_in_darktable(tmp_path, rating=5, dry_run=True)

        assert result["pid"] is None
        assert result["dry_run"] is True
        assert result["launched"] is False
        assert "dry run" in format_open_summary(result)


class TestFormatOpenSummary:
    def test_dry_run_formatted(self, tmp_path: Path) -> None:
        result = open_in_darktable(tmp_path, dry_run=True)
        text = format_open_summary(result)
        assert "dry run" in text
        assert "darktable" in text
        # No rating hint requested → no instruction line.
        assert "filter bar" not in text

    def test_dry_run_includes_filter_hint_when_rating_set(self, tmp_path: Path) -> None:
        result = open_in_darktable(tmp_path, rating=5, dry_run=True)
        text = format_open_summary(result)
        assert "★★★★★" in text
        # Exact rating goes through Lua → "pre-applied" wording.
        assert "pre-applied" in text

    def test_dry_run_range_open_bound_pre_applied(self, tmp_path: Path) -> None:
        # rating_min=4 → data=">=4", fully pre-applied.
        result = open_in_darktable(tmp_path, rating_min=4, dry_run=True)
        text = format_open_summary(result)
        assert "pre-applied" in text

    def test_dry_run_inner_range_pre_applied(self, tmp_path: Path) -> None:
        # 2..4 is now first-class via data="[2;4]" — pre-applied.
        result = open_in_darktable(tmp_path, rating_min=2, rating_max=4, dry_run=True)
        text = format_open_summary(result)
        assert "pre-applied" in text

    def test_live_run_includes_pid(self) -> None:
        text = format_open_summary(
            {"pid": 12345, "command": ["darktable", "/x"], "filter_hint": None}
        )
        assert "pid=12345" in text

    def test_failed_launch_is_rendered_honestly(self) -> None:
        text = format_open_summary(
            {
                "pid": 12345,
                "command": ["darktable", "/x"],
                "filter_hint": None,
                "launched": False,
                "reason": "exit code 1 — library.db is locked",
            }
        )
        assert "did NOT launch" in text
        assert "library.db is locked" in text
        assert "launched (pid" not in text
