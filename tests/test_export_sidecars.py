from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from darktable_mcp.darktable.cli_wrapper import CLIWrapper
from darktable_mcp.tools import contact_sheet_tools as sheets


def test_variant_export_preserves_normalized_path_and_bounds(tmp_path, monkeypatch):
    cli = CLIWrapper("darktable-cli", tmp_path / "cfg")

    def run(cmd, **kwargs):
        assert cmd[1:4] == ["photo.raw", "photo_02.raw.xmp", str(tmp_path / "out.jpg")]
        assert cmd[cmd.index("--width") + 1] == "300"
        assert cmd[cmd.index("--height") + 1] == "200"
        Path(cmd[3]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("darktable_mcp.darktable.cli_wrapper.subprocess.run", run)
    assert (
        cli.export_image(
            Path("photo.raw"),
            tmp_path / "out.jpeg",
            xmp_path=Path("photo_02.raw.xmp"),
            max_width=300,
            max_height=200,
        )
        == tmp_path / "out.jpg"
    )


def test_sidecar_list_must_match_inputs_before_export(tmp_path, monkeypatch):
    cli = CLIWrapper("darktable-cli", tmp_path / "cfg")
    run = Mock()
    monkeypatch.setattr("darktable_mcp.darktable.cli_wrapper.subprocess.run", run)
    with pytest.raises(ValueError, match="xmp_paths"):
        cli.batch_export([Path("a.raw"), Path("b.raw")], tmp_path / "out", xmp_paths=[None])
    run.assert_not_called()


def test_variant_cache_tracks_source_sidecar_and_subsecond_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    source = tmp_path / "source.raw"
    source.write_bytes(b"raw")
    sidecar = tmp_path / "version.xmp"
    sidecar.write_bytes(b"a")
    first = sheets.thumb_cache_path("1", str(source), 200, str(sidecar))
    assert first != sheets.thumb_cache_path("1", str(source), 200)
    import os

    stamp = sidecar.stat().st_mtime_ns
    os.utime(sidecar, ns=(stamp, stamp + 1))
    assert first != sheets.thumb_cache_path("1", str(source), 200, str(sidecar))
    assert first != sheets.thumb_cache_path("1", str(tmp_path / "other.raw"), 200, str(sidecar))


@pytest.mark.parametrize("sidecar_key", ["xmp_path", "sidecar"])
def test_thumbnail_uses_variant_and_returned_export_path(tmp_path, monkeypatch, sidecar_key):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    def export(**kwargs):
        assert kwargs["xmp_path"] == Path("variant.xmp")
        actual = kwargs["output_path"].with_suffix(".normalized.jpg")
        actual.write_bytes(b"image")
        return actual

    cli = SimpleNamespace(export_image=export)
    result = sheets.render_thumbnails(
        cli, [{"id": 1, "path": "raw", sidecar_key: "variant.xmp"}], 200
    )
    path, error = result["1"]
    assert error is None
    assert path.read_bytes() == b"image"


def test_overlapping_sheets_use_distinct_live_configdirs(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cli = CLIWrapper("darktable-cli", tmp_path / "cfg")
    active = set()
    observed = set()
    guard = threading.Lock()
    rendezvous = threading.Barrier(2)

    def run(cmd, **kwargs):
        config = cmd[cmd.index("--configdir") + 1]
        with guard:
            assert config not in active
            active.add(config)
            observed.add(config)
        try:
            rendezvous.wait(timeout=5)
            Path(cmd[2]).write_bytes(b"image")
        finally:
            with guard:
                active.remove(config)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("darktable_mcp.darktable.cli_wrapper.subprocess.run", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(sheets.render_thumbnails, cli, [{"id": i, "path": f"{i}.raw"}], 200)
            for i in range(2)
        ]
        for i, future in enumerate(futures):
            assert future.result()[str(i)][1] is None
    assert len(observed) == 2


def test_explicit_configdir_created_and_used(tmp_path, monkeypatch):
    cli = CLIWrapper("darktable-cli", tmp_path / "default")
    override = tmp_path / "override"

    def run(cmd, **kwargs):
        assert Path(cmd[cmd.index("--configdir") + 1]) == override
        assert override.is_dir()
        Path(cmd[2]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("darktable_mcp.darktable.cli_wrapper.subprocess.run", run)
    cli.export_image(Path("photo.raw"), tmp_path / "out.jpg", configdir=override)
