"""Tests for preview_tools (vision-rating workflow)."""

from __future__ import annotations

import json
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
