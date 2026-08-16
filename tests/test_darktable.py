"""Tests for darktable integration layer."""

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from darktable_mcp.darktable.cli_wrapper import FORMAT_EXTENSIONS, CLIWrapper, ExportResult
from darktable_mcp.utils.errors import DarktableNotFoundError, ExportError


def fake_run(returncode: int = 0, stderr: str = "", write_output: bool = True, size: int = 16):
    """Build a subprocess.run stand-in that also writes the promised output file.

    `export_image` now stats the output path, so a mock that only fakes the
    exit code would make every export look like a silent no-op.

    Args:
        returncode: Exit code darktable-cli should report
        stderr: stderr text darktable-cli should report
        write_output: Whether to actually create the output file
        size: Byte length of the file to create

    Returns:
        Callable: side_effect for a patched `subprocess.run`
    """

    def _run(cmd, *_args, **_kwargs):
        if write_output and returncode == 0:
            output = Path(cmd[2])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"x" * size)
        return Mock(returncode=returncode, stdout="", stderr=stderr)

    return _run


def fake_run_renaming_to_format(format_type: str, write_output: bool = True):
    """Build a subprocess.run stand-in that renames the output the way darktable-cli does.

    Real darktable-cli ignores the extension it is handed and writes the
    format's own: ask for `out.jpeg` and `out.jpg` appears instead.

    Args:
        format_type: Export format whose extension darktable-cli imposes
        write_output: Whether to actually create the renamed file

    Returns:
        Callable: side_effect for a patched `subprocess.run`
    """
    ext = FORMAT_EXTENSIONS[format_type.lower()]

    def _run(cmd, *_args, **_kwargs):
        if write_output:
            written = Path(cmd[2]).with_suffix(f".{ext}")
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_bytes(b"x" * 16)
        return Mock(returncode=0, stdout="", stderr="")

    return _run


def claim_configdirs_concurrently(wrapper: CLIWrapper, count: int) -> list:
    """Have `count` threads hold a worker configdir at the same instant.

    Every thread stays alive until all of them have claimed, so the result
    shows what overlapping exports would see rather than what sequential
    ones would.

    Args:
        wrapper: Wrapper whose slots are being claimed
        count: Number of simultaneous claimants

    Returns:
        list: One configdir Path per thread
    """
    seen: list = []
    lock = threading.Lock()
    start = threading.Barrier(count)
    claimed = threading.Barrier(count)

    def collect():
        start.wait(timeout=5)
        with lock:
            seen.append(wrapper._worker_configdir())
        claimed.wait(timeout=5)

    threads = [threading.Thread(target=collect) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(seen) == count, f"only {len(seen)} of {count} threads claimed a configdir"
    return seen


class TestCLIWrapperDiscovery:
    """darktable-cli auto-detect and check_darktable_available."""

    @patch("shutil.which")
    def test_cli_wrapper_init(self, mock_which, tmp_path):
        mock_which.return_value = "/usr/bin/darktable-cli"
        wrapper = CLIWrapper(configdir=tmp_path)
        assert wrapper is not None

    @patch("shutil.which")
    def test_check_darktable_not_found(self, mock_which, tmp_path):
        mock_which.side_effect = ["/usr/bin/darktable-cli", None]
        wrapper = CLIWrapper(configdir=tmp_path)
        with pytest.raises(DarktableNotFoundError):
            wrapper.check_darktable_available()

    @patch("shutil.which")
    def test_check_darktable_found(self, mock_which, tmp_path):
        mock_which.side_effect = ["/usr/bin/darktable-cli", "/usr/bin/darktable"]
        wrapper = CLIWrapper(configdir=tmp_path)
        assert wrapper.check_darktable_available() == "/usr/bin/darktable"


class TestCLIWrapperConfigdir:
    """The dedicated configdir prevents the GUI's library lock from breaking exports."""

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_default_configdir_is_xdg_cache_namespaced(self, _mock_which, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        wrapper = CLIWrapper()
        assert wrapper.configdir == tmp_path / "darktable-mcp" / "cli-config"
        assert wrapper.configdir.is_dir()

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_default_configdir_falls_back_to_home_cache(
        self, _mock_which, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        wrapper = CLIWrapper()
        assert wrapper.configdir == tmp_path / ".cache" / "darktable-mcp" / "cli-config"
        assert wrapper.configdir.is_dir()

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_custom_configdir_is_respected_and_created(self, _mock_which, tmp_path):
        target = tmp_path / "nested" / "custom-cli-config"
        wrapper = CLIWrapper(configdir=target)
        assert wrapper.configdir == target
        assert target.is_dir()

    def test_sidecar_caveat_is_documented(self):
        """A silently unedited export is the cost of the isolated configdir."""
        assert "sidecar" in CLIWrapper.__doc__.lower()
        assert "sidecar" in CLIWrapper.batch_export.__doc__.lower()


class TestCLIWrapperExport:
    """Export passes --configdir so darktable-cli does not race the GUI's lock."""

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_passes_configdir_to_darktable_cli(
        self, _mock_which, mock_run, tmp_path
    ):
        mock_run.side_effect = fake_run()
        cfg = tmp_path / "cfg"
        wrapper = CLIWrapper(configdir=cfg)
        wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg", 95)

        cmd = mock_run.call_args[0][0]
        assert "--core" in cmd
        idx = cmd.index("--configdir")
        assert cmd[idx + 1] == str(cfg)

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_includes_jpeg_quality_conf(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path)
        wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg", quality=88)

        cmd = mock_run.call_args[0][0]
        assert "--conf" in cmd
        assert any("plugins/imageio/format/jpeg/quality=88" in part for part in cmd)

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_failure_raises_export_error_with_stderr(
        self, _mock_which, mock_run, tmp_path
    ):
        mock_run.side_effect = fake_run(returncode=1, stderr="database is locked")
        wrapper = CLIWrapper(configdir=tmp_path)
        with pytest.raises(ExportError, match="database is locked"):
            wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_timeout_raises_export_error(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["darktable-cli"], timeout=5)
        wrapper = CLIWrapper(configdir=tmp_path)
        with pytest.raises(ExportError, match="timed out"):
            wrapper.export_image(
                Path("/in.NEF"), tmp_path / "out.jpg", "jpeg", timeout=5
            )


class TestCLIWrapperOutputVerification:
    """darktable-cli exits 0 without writing a file for unsupported inputs."""

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_missing_output_despite_exit_zero_raises(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run(write_output=False)
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        out = tmp_path / "out.jpg"
        with pytest.raises(ExportError, match=str(out)):
            wrapper.export_image(Path("/in.NEF"), out, "jpeg")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_zero_byte_output_raises(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run(size=0)
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        with pytest.raises(ExportError, match="zero-byte"):
            wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_batch_reports_not_ok_when_no_file_written(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run(write_output=False)
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        results = wrapper.batch_export([Path("/a.NEF")], tmp_path / "out")

        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].output is None
        assert "wrote no file" in results[0].error


class TestCLIWrapperSizeFlags:
    """Size bounds use darktable-cli's --width/--height, not jpeg-only conf keys."""

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_both_bounds_become_width_and_height_flags(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        wrapper.export_image(
            Path("/in.NEF"), tmp_path / "out.jpg", "jpeg", max_width=1600, max_height=1200
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--width") + 1] == "1600"
        assert cmd[cmd.index("--height") + 1] == "1200"
        assert not any("max_width" in part for part in cmd)

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_single_bound_sends_zero_for_the_other_axis(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        wrapper.export_image(Path("/in.NEF"), tmp_path / "out.png", "png", max_width=2048)

        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--width") + 1] == "2048"
        assert cmd[cmd.index("--height") + 1] == "0"

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_size_flags_precede_core(self, _mock_which, mock_run, tmp_path):
        """Everything after --core belongs to the darktable core, not the CLI."""
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg", max_height=900)

        cmd = mock_run.call_args[0][0]
        assert cmd.index("--height") < cmd.index("--core")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_no_bounds_emits_no_size_flags(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpg", "jpeg")

        cmd = mock_run.call_args[0][0]
        assert "--width" not in cmd
        assert "--height" not in cmd


class TestBatchExportContract:
    """batch_export returns structured results, not prose the caller must sniff."""

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_batch_export_writes_each_file(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        results = wrapper.batch_export(
            [Path("/a.NEF"), Path("/b.NEF")],
            tmp_path / "out",
            format_type="jpeg",
            quality=90,
        )
        assert len(results) == 2
        assert all(r.ok for r in results)
        # Every invocation must include --configdir
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "--configdir" in cmd

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_result_shape(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        out = tmp_path / "out"
        (result,) = wrapper.batch_export([Path("/a.NEF")], out, format_type="png")

        assert isinstance(result, ExportResult)
        assert result.input == str(Path("/a.NEF"))
        assert result.output == str(out / "a.png")
        assert result.ok is True
        assert result.error is None

    def test_empty_input_returns_empty_list(self, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/darktable-cli"):
            wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        assert wrapper.batch_export([], tmp_path / "out") == []

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_output_path_containing_failed_is_still_ok(self, _mock_which, mock_run, tmp_path):
        """The old string-sniffing contract misread this as a failure."""
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        (result,) = wrapper.batch_export([Path("/shoot-failed/a.NEF")], tmp_path / "out")

        assert result.ok is True
        assert result.error is None

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_timeout_is_threaded_through_to_subprocess(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        wrapper.batch_export([Path("/a.NEF")], tmp_path / "out", timeout=7)

        assert mock_run.call_args.kwargs["timeout"] == 7

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_failures_do_not_abort_the_batch(self, _mock_which, mock_run, tmp_path):
        def _run(cmd, *_args, **_kwargs):
            if "b.NEF" in cmd[1]:
                return Mock(returncode=1, stdout="", stderr="boom")
            Path(cmd[2]).write_bytes(b"x")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _run
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        results = wrapper.batch_export(
            [Path("/a.NEF"), Path("/b.NEF"), Path("/c.NEF")], tmp_path / "out"
        )

        assert [r.ok for r in results] == [True, False, True]
        assert "boom" in results[1].error


class TestBatchExportCollisions:
    """Same stem from two source folders must not overwrite one output."""

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_same_stem_gets_distinct_outputs(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        results = wrapper.batch_export(
            [Path("/shootA/DSC_0001.NEF"), Path("/shootB/DSC_0001.NEF")],
            tmp_path / "out",
            format_type="jpeg",
        )

        outputs = [r.output for r in results]
        assert all(r.ok for r in results)
        assert len(set(outputs)) == 2
        assert outputs[0] == str(tmp_path / "out" / "DSC_0001.jpg")
        assert outputs[1] == str(tmp_path / "out" / "DSC_0001-shootB.jpg")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_three_way_collision_falls_back_to_counter(self, _mock_which, mock_run, tmp_path):
        """Identical source dir names cannot disambiguate; a counter must."""
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        results = wrapper.batch_export(
            [
                Path("/one/raw/DSC_0001.NEF"),
                Path("/two/raw/DSC_0001.NEF"),
                Path("/three/raw/DSC_0001.NEF"),
            ],
            tmp_path / "out",
        )

        outputs = [r.output for r in results]
        assert len(set(outputs)) == 3
        assert outputs[2] == str(tmp_path / "out" / "DSC_0001-2.jpg")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_each_input_gets_its_own_cli_invocation(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        wrapper.batch_export(
            [Path("/shootA/DSC_0001.NEF"), Path("/shootB/DSC_0001.NEF")], tmp_path / "out"
        )

        written = {call[0][0][2] for call in mock_run.call_args_list}
        assert len(written) == 2


class TestBatchExportParallelism:
    """Threads speed the batch up but must not reorder results."""

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_results_stay_in_input_order_under_parallelism(
        self, _mock_which, mock_run, tmp_path
    ):
        def _run(cmd, *_args, **_kwargs):
            # Finish in reverse order so a naive as_completed collector scrambles.
            index = int(Path(cmd[1]).stem)
            time.sleep((8 - index) * 0.01)
            Path(cmd[2]).write_bytes(b"x")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _run
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        inputs = [Path(f"/src/{i}.NEF") for i in range(8)]
        results = wrapper.batch_export(inputs, tmp_path / "out", max_workers=8)

        assert [r.input for r in results] == [str(p) for p in inputs]
        assert [r.output for r in results] == [
            str(tmp_path / "out" / f"{i}.jpg") for i in range(8)
        ]

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_exports_actually_overlap(self, _mock_which, mock_run, tmp_path):
        live = 0
        peak = 0
        lock = threading.Lock()

        def _run(cmd, *_args, **_kwargs):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1
            Path(cmd[2]).write_bytes(b"x")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _run
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        wrapper.batch_export(
            [Path(f"/src/{i}.NEF") for i in range(4)], tmp_path / "out", max_workers=4
        )

        assert peak > 1

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_max_workers_defaults_without_argument(self, _mock_which, mock_run, tmp_path):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        results = wrapper.batch_export(
            [Path(f"/src/{i}.NEF") for i in range(6)], tmp_path / "out"
        )

        assert len(results) == 6
        assert all(r.ok for r in results)


class TestRealDarktableRegressions:
    """Regressions found by running against a real darktable 5.6.0.

    Both of these passed the mocked suite and failed on real hardware, so
    they are pinned here explicitly.
    """

    @pytest.mark.parametrize(
        "format_type,expected_ext",
        [("jpeg", "jpg"), ("jpg", "jpg"), ("png", "png"), ("tiff", "tif"), ("tif", "tif")],
    )
    def test_planned_extension_matches_what_darktable_cli_writes(
        self, format_type, expected_ext, tmp_path
    ):
        """darktable-cli renames the output to the format's own extension.

        Ask it for `out.jpeg` and it writes `out.jpg`; ask for `out.tiff`
        and it writes `out.tif`. Planning the requested name instead of the
        written one made the post-export existence check fail on files that
        had exported perfectly well.
        """
        planned = CLIWrapper._plan_output_paths(
            [Path("/src/DSC_0001.NEF")], tmp_path / "out", format_type
        )

        assert planned[0].suffix == f".{expected_ext}"

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_each_worker_thread_gets_its_own_configdir(self, _mock_which, tmp_path):
        """Concurrent darktable-cli processes must not share a configdir.

        They contend for the same library.db; observed on darktable 5.6.0,
        two parallel exports sharing one configdir produced a single output
        file and no error worth the name -- silent data loss.

        The claimants are held alive until all three have a slot: slots are
        recycled once their owner exits, so threads that ran back-to-back
        are *allowed* to share a directory and only overlapping ones must
        differ.
        """
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")

        seen = claim_configdirs_concurrently(wrapper, 3)

        assert len(set(seen)) == 3, f"configdirs collided across threads: {seen}"

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_worker_configdir_is_stable_within_one_thread(self, _mock_which, tmp_path):
        """Threads are reused across a batch: one library.db per worker, not per file."""
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")

        assert wrapper._worker_configdir() == wrapper._worker_configdir()

    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_dead_threads_hand_their_configdir_back(self, _mock_which, tmp_path):
        """A finished thread's slot is reused instead of a fresh one being minted.

        Every `batch_export` builds a new pool with new threads. Keying the
        slot to the thread for the life of the process meant each batch left
        another complete configdir behind.
        """
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")

        first = claim_configdirs_concurrently(wrapper, 3)
        second = claim_configdirs_concurrently(wrapper, 3)

        assert set(second) == set(first), "later threads minted new configdirs"
        assert len(set(second)) == 3, "concurrent threads shared a configdir"

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_worker_configdirs_stay_bounded_across_many_batches(
        self, _mock_which, mock_run, tmp_path
    ):
        """Configdirs are bounded by peak concurrency, not by total batches.

        Each one is a full darktable configdir (library.db, data.db,
        darktablerc) that nothing ever cleans up, so an MCP server doing
        many batches used to leave hundreds under the cache dir.
        """
        mock_run.side_effect = fake_run()
        cfg = tmp_path / "cfg"
        wrapper = CLIWrapper(configdir=cfg)

        for batch in range(6):
            results = wrapper.batch_export(
                [Path(f"/src/{batch}-{i}.NEF") for i in range(4)],
                tmp_path / "out",
                max_workers=2,
            )
            assert all(r.ok for r in results), "batch failed; the count below proves nothing"

        worker_dirs = sorted(p.name for p in cfg.glob("worker-*"))
        assert len(worker_dirs) <= 2, f"one configdir per thread, not per slot: {worker_dirs}"

    @pytest.mark.parametrize(
        "format_type,requested,written",
        [("jpeg", "out.jpeg", "out.jpg"), ("tiff", "out.tiff", "out.tif")],
    )
    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_image_normalizes_the_requested_extension(
        self, _mock_which, mock_run, format_type, requested, written, tmp_path
    ):
        """A direct `export_image(src, dst / "out.jpeg", "jpeg")` must not fail.

        The signature invites it, darktable-cli writes `out.jpg` anyway, and
        stat'ing the caller's spelling turned a perfectly good export into
        `Export reported success but wrote no file`.
        """
        mock_run.side_effect = fake_run_renaming_to_format(format_type)
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")

        result = wrapper.export_image(Path("/in.NEF"), tmp_path / requested, format_type)

        assert result == tmp_path / written
        assert result.is_file()
        assert mock_run.call_args[0][0][2] == str(tmp_path / written)

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_image_keeps_a_matching_extension_untouched(
        self, _mock_which, mock_run, tmp_path
    ):
        """Normalising must not rewrite `photo.2024.jpg` into something else."""
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")

        result = wrapper.export_image(Path("/in.NEF"), tmp_path / "photo.2024.jpg", "jpeg")

        assert result == tmp_path / "photo.2024.jpg"

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_mismatched_extension_still_catches_a_genuine_no_output_export(
        self, _mock_which, mock_run, tmp_path
    ):
        """Normalising the name must not degrade into "some file showed up".

        A stray file in the output directory is not this export's output.
        """
        mock_run.side_effect = fake_run_renaming_to_format("jpeg", write_output=False)
        (tmp_path / "decoy.jpg").write_bytes(b"not mine")
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")

        with pytest.raises(ExportError, match="wrote no file"):
            wrapper.export_image(Path("/in.NEF"), tmp_path / "out.jpeg", "jpeg")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_batch_results_report_the_path_darktable_cli_wrote(
        self, _mock_which, mock_run, tmp_path
    ):
        """`_export_one` and `export_image` must agree on the real name."""
        mock_run.side_effect = fake_run_renaming_to_format("tiff")
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")

        (result,) = wrapper.batch_export(
            [Path("/src/DSC_0001.NEF")], tmp_path / "out", format_type="tiff"
        )

        assert result.ok is True
        assert result.output == str(tmp_path / "out" / "DSC_0001.tif")
        assert Path(result.output).is_file()

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_one_reports_the_renamed_path_not_the_requested_one(
        self, _mock_which, mock_run, tmp_path
    ):
        """`_plan_output_paths` already normalises, so probe the seam directly.

        If `_export_one` echoed its own argument back, a plan that ever
        disagreed with darktable-cli would be reported as a path holding no
        file.
        """
        mock_run.side_effect = fake_run_renaming_to_format("jpeg")
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")

        result = wrapper._export_one(Path("/in.NEF"), tmp_path / "out.jpeg", "jpeg", 95)

        assert result.ok is True
        assert result.output == str(tmp_path / "out.jpg")

    @patch("darktable_mcp.darktable.cli_wrapper.subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/darktable-cli")
    def test_export_command_carries_the_calling_threads_configdir(
        self, _mock_which, mock_run, tmp_path
    ):
        mock_run.side_effect = fake_run()
        wrapper = CLIWrapper(configdir=tmp_path / "cfg")
        wrapper.batch_export([Path("/src/a.NEF")], tmp_path / "out")

        cmd = mock_run.call_args[0][0]
        assert "--configdir" in cmd
        assert str(tmp_path / "cfg") in cmd[cmd.index("--configdir") + 1]
