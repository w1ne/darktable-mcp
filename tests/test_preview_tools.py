"""Tests for preview_tools (vision-rating workflow)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from darktable_mcp.tools.preview_tools import (
    XMP_TEMPLATE,
    apply_ratings_batch,
    extract_previews,
    format_extract_summary,
    format_ratings_summary,
)
from darktable_mcp.utils.errors import DarktableMCPError


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


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
