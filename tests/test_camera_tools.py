"""Tests for CameraTools module."""

import subprocess
from unittest.mock import MagicMock, Mock, patch

import pytest

from darktable_mcp.tools.camera_tools import CameraTools
from darktable_mcp.utils.errors import DarktableMCPError


def _popen_mock(stdout="", stderr="", returncode=0, raises_timeout=False):
    """Build a Popen-like MagicMock with iterable stdout/stderr.

    Threads in _download_one_folder iterate `proc.stdout` and `proc.stderr`
    once each, so plain iterators over splitlines (with line endings kept)
    are sufficient.
    """
    proc = MagicMock()
    proc.stdout = iter(stdout.splitlines(keepends=True))
    proc.stderr = iter(stderr.splitlines(keepends=True))
    if raises_timeout:
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["gphoto2"], timeout=60)
    else:
        proc.wait.return_value = returncode
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


class TestCameraToolsDetectCameras:
    """Tests for CameraTools._detect_cameras helper."""

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_detect_cameras_one_camera(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
                "Nikon DSC D800E                usb:002,002\n"
            ),
            stderr="",
        )
        tools = CameraTools()
        cameras = tools._detect_cameras()
        assert cameras == [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_run.assert_called_once_with(
            ["gphoto2", "--auto-detect"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_detect_cameras_none(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
            ),
            stderr="",
        )
        tools = CameraTools()
        assert tools._detect_cameras() == []

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_detect_cameras_multiple(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
                "Nikon DSC D800E                usb:002,002\n"
                "Canon EOS R5                   usb:003,004\n"
            ),
            stderr="",
        )
        tools = CameraTools()
        cameras = tools._detect_cameras()
        assert len(cameras) == 2
        assert cameras[0]["model"] == "Nikon DSC D800E"
        assert cameras[1]["port"] == "usb:003,004"

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_detect_cameras_gphoto2_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError("gphoto2")
        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="gphoto2 not installed"):
            tools._detect_cameras()

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_detect_cameras_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["gphoto2", "--auto-detect"], timeout=10
        )
        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="timed out"):
            tools._detect_cameras()

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_detect_cameras_locked_by_gvfs(self, mock_run):
        mock_run.return_value = Mock(
            returncode=1,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
            ),
            stderr=(
                "*** Error ***\n"
                "An error occurred in the io-layer ('Could not lock the device')\n"
            ),
        )
        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="Could not lock"):
            tools._detect_cameras()


class TestCameraToolsListImageFolders:
    """Tests for CameraTools._list_image_folders helper."""

    DUAL_STORAGE_OUTPUT = (
        "There are 2 folders in folder '/'.\n"
        " - store_00010001\n"
        " - store_00020001\n"
        "There is 1 folder in folder '/store_00010001'.\n"
        " - DCIM\n"
        "There is 1 folder in folder '/store_00010001/DCIM'.\n"
        " - 101D800E\n"
        "There are 0 folders in folder '/store_00010001/DCIM/101D800E'.\n"
        "There is 1 folder in folder '/store_00020001'.\n"
        " - DCIM\n"
        "There are 2 folders in folder '/store_00020001/DCIM'.\n"
        " - 100D800E\n"
        " - 101D800E\n"
        "There are 0 folders in folder '/store_00020001/DCIM/100D800E'.\n"
        "There are 0 folders in folder '/store_00020001/DCIM/101D800E'.\n"
    )

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_list_dual_storage_returns_three_leaves(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0, stdout=self.DUAL_STORAGE_OUTPUT, stderr=""
        )
        leaves = CameraTools()._list_image_folders("Nikon DSC D800E", "usb:002,002")
        assert leaves == [
            "/store_00010001/DCIM/101D800E",
            "/store_00020001/DCIM/100D800E",
            "/store_00020001/DCIM/101D800E",
        ]

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_list_passes_camera_and_port(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        CameraTools()._list_image_folders("Nikon DSC D800E", "usb:002,002")
        cmd = mock_run.call_args[0][0]
        assert "--camera" in cmd
        assert "Nikon DSC D800E" in cmd
        assert "--port" in cmd
        assert "usb:002,002" in cmd
        assert "--list-folders" in cmd

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_list_falls_back_to_root_on_empty_parse(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="garbage\n", stderr="")
        leaves = CameraTools()._list_image_folders("Nikon DSC D800E", "usb:002,002")
        assert leaves == ["/"]

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_list_falls_back_to_root_on_nonzero_exit(self, mock_run):
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="error")
        leaves = CameraTools()._list_image_folders("Nikon DSC D800E", "usb:002,002")
        assert leaves == ["/"]

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_list_falls_back_to_root_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gphoto2"], timeout=30)
        leaves = CameraTools()._list_image_folders("Nikon DSC D800E", "usb:002,002")
        assert leaves == ["/"]

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_list_raises_when_gphoto2_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError("gphoto2")
        with pytest.raises(DarktableMCPError, match="gphoto2 not installed"):
            CameraTools()._list_image_folders("Nikon DSC D800E", "usb:002,002")


class TestCameraToolsDownloadOneFolder:
    """Tests for CameraTools._download_one_folder helper.

    The helper streams via subprocess.Popen now (so per-file progress can
    be written to a log as gphoto2 writes it), so tests mock Popen and
    feed iterables for stdout/stderr.
    """

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_success(self, mock_popen, tmp_path):
        mock_popen.return_value = _popen_mock(
            stdout=(
                "Saving file as /tmp/dest/IMG_0001.NEF\n"
                "Saving file as /tmp/dest/IMG_0002.NEF\n"
                "Saving file as /tmp/dest/IMG_0003.NEF\n"
            ),
        )
        tools = CameraTools()
        count, errors = tools._download_one_folder(
            "Nikon DSC D800E",
            "usb:002,002",
            "/store_00010001/DCIM/101D800E",
            tmp_path,
            timeout_seconds=600,
        )
        assert count == 3
        assert errors == []

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "gphoto2"
        assert "--camera" in cmd
        assert "Nikon DSC D800E" in cmd
        assert "--port" in cmd
        assert "usb:002,002" in cmd
        assert "--folder" in cmd
        assert "/store_00010001/DCIM/101D800E" in cmd
        assert "--get-all-files" in cmd
        assert "--skip-existing" in cmd

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_preserves_extension_in_filename_pattern(
        self, mock_popen, tmp_path
    ):
        mock_popen.return_value = _popen_mock()
        CameraTools()._download_one_folder(
            "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
        )
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--filename")
        filename_pattern = cmd[idx + 1]
        # Must include both name and extension placeholders. Using %f alone
        # drops the extension (e.g. DSC_3270 instead of DSC_3270.NEF). %C is
        # the file suffix as reported by the camera, so the pattern works
        # for any format the camera produces (NEF/CR2/CR3/ARW/DNG/RAF/ORF/
        # JPG/MP4/MOV/...).
        assert "%f" in filename_pattern
        assert "%C" in filename_pattern
        assert filename_pattern.endswith("%f.%C")

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_handles_mixed_formats(self, mock_popen, tmp_path):
        """gphoto2's %C placeholder gives the correct extension per file,
        so the same call copies RAW + JPEG + video without per-format logic."""
        mock_popen.return_value = _popen_mock(
            stdout=(
                "Saving file as /dest/IMG_0001.NEF\n"   # Nikon RAW
                "Saving file as /dest/IMG_0001.JPG\n"   # JPEG sidecar
                "Saving file as /dest/IMG_0002.CR2\n"   # Canon RAW
                "Saving file as /dest/IMG_0003.CR3\n"   # Canon RAW (newer)
                "Saving file as /dest/IMG_0004.ARW\n"   # Sony RAW
                "Saving file as /dest/IMG_0005.RAF\n"   # Fuji RAW
                "Saving file as /dest/IMG_0006.DNG\n"   # Adobe / Pentax / iPhone ProRAW
                "Saving file as /dest/MVI_0007.MP4\n"   # Video
            ),
        )
        log_path = tmp_path / "progress.log"
        with open(log_path, "w", encoding="utf-8") as log:
            count, errors = CameraTools()._download_one_folder(
                "Some Camera", "usb:001,001", "/", tmp_path,
                timeout_seconds=60, progress_log=log,
            )
        assert count == 8
        assert errors == []
        body = log_path.read_text()
        for ext in ("NEF", "JPG", "CR2", "CR3", "ARW", "RAF", "DNG", "MP4"):
            assert ext in body

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_partial_failure(self, mock_popen, tmp_path):
        mock_popen.return_value = _popen_mock(
            stdout=(
                "Saving file as /tmp/dest/IMG_0001.NEF\n"
                "Saving file as /tmp/dest/IMG_0002.NEF\n"
            ),
            stderr="ERROR: Could not download IMG_0003.NEF\n",
            returncode=1,
        )
        tools = CameraTools()
        count, errors = tools._download_one_folder(
            "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
        )
        assert count == 2
        assert any("IMG_0003" in e for e in errors)

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_gphoto2_missing(self, mock_popen, tmp_path):
        mock_popen.side_effect = FileNotFoundError("gphoto2")
        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="gphoto2 not installed"):
            tools._download_one_folder(
                "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
            )

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_passes_c_locale_env(self, mock_popen, tmp_path):
        mock_popen.return_value = _popen_mock()
        CameraTools()._download_one_folder(
            "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
        )
        env = mock_popen.call_args.kwargs.get("env")
        assert env is not None
        assert env.get("LC_ALL") == "C"
        assert env.get("LANG") == "C"

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_respects_custom_timeout(self, mock_popen, tmp_path):
        proc = _popen_mock()
        mock_popen.return_value = proc
        CameraTools()._download_one_folder(
            "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=120
        )
        # Timeout is plumbed into proc.wait(), not Popen() itself.
        proc.wait.assert_called_with(timeout=120)

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_timeout_kills_process(self, mock_popen, tmp_path):
        mock_popen.return_value = _popen_mock(raises_timeout=True)
        with pytest.raises(subprocess.TimeoutExpired):
            CameraTools()._download_one_folder(
                "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
            )
        mock_popen.return_value.kill.assert_called_once()

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_surfaces_gvfs_lock_error(self, mock_popen, tmp_path):
        mock_popen.return_value = _popen_mock(
            stderr=(
                "*** Error ***\n"
                "An error occurred in the io-layer ('Could not lock the device')\n"
            ),
            returncode=1,
        )
        with pytest.raises(DarktableMCPError, match="Another process is "):
            CameraTools()._download_one_folder(
                "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
            )

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_skip_existing_only_is_not_lock_error(
        self, mock_popen, tmp_path
    ):
        # When all files already exist on disk, gphoto2 prints "Skip
        # existing" lines and may still return rc=0 (or rc=1 in some
        # versions); either way, this is not a lock error. We must NOT
        # raise — just return (count=0, errors=[]).
        mock_popen.return_value = _popen_mock(
            stdout="Skip existing file /tmp/x/IMG_0001.NEF\n",
        )
        count, errors = CameraTools()._download_one_folder(
            "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
        )
        assert count == 0
        assert errors == []

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_writes_progress_lines_to_log(self, mock_popen, tmp_path):
        mock_popen.return_value = _popen_mock(
            stdout=(
                "Saving file as /dest/A.NEF\n"
                "Saving file as /dest/B.NEF\n"
            ),
        )
        log_path = tmp_path / "progress.log"
        with open(log_path, "w", encoding="utf-8") as log:
            count, _ = CameraTools()._download_one_folder(
                "Nikon DSC D800E",
                "usb:002,002",
                "/",
                tmp_path,
                timeout_seconds=60,
                progress_log=log,
                expected_total=10,
            )
        assert count == 2
        body = log_path.read_text()
        # Two progress lines, each with the (n/total) prefix and the saved path.
        assert "(1/10)" in body
        assert "(2/10)" in body
        assert "A.NEF" in body
        assert "B.NEF" in body


class TestCameraToolsCountFilesInFolder:
    """Tests for CameraTools._count_files_in_folder helper."""

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_count_parses_num_files_output(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0,
            stdout="Number of files in folder '/store/DCIM/101D800E': 427\n",
            stderr="",
        )
        n = CameraTools()._count_files_in_folder(
            "Nikon DSC D800E", "usb:002,002", "/store/DCIM/101D800E"
        )
        assert n == 427

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_count_returns_none_on_nonzero_exit(self, mock_run):
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="error")
        assert (
            CameraTools()._count_files_in_folder("Nikon DSC D800E", "usb:002,002", "/")
            is None
        )

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_count_returns_none_on_unparseable_output(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="weird output", stderr="")
        assert (
            CameraTools()._count_files_in_folder("Nikon DSC D800E", "usb:002,002", "/")
            is None
        )

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_count_returns_none_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gphoto2"], timeout=30)
        assert (
            CameraTools()._count_files_in_folder("Nikon DSC D800E", "usb:002,002", "/")
            is None
        )

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_count_raises_when_gphoto2_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError("gphoto2")
        with pytest.raises(DarktableMCPError, match="gphoto2 not installed"):
            CameraTools()._count_files_in_folder("Nikon DSC D800E", "usb:002,002", "/")


class TestCameraToolsDownloadFromCamera:
    """Tests for the multi-folder orchestrator _download_from_camera."""

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_iterates_each_storage_leaf(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = [
            "/store_00010001/DCIM/101D800E",
            "/store_00020001/DCIM/101D800E",
        ]
        mock_download.return_value = (10, [])
        tools = CameraTools()
        count, errors = tools._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        assert count == 20
        assert errors == []
        assert mock_download.call_count == 2
        called_folders = [call.args[2] for call in mock_download.call_args_list]
        assert called_folders == [
            "/store_00010001/DCIM/101D800E",
            "/store_00020001/DCIM/101D800E",
        ]

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_aggregates_errors_across_folders(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/a", "/b"]
        mock_download.side_effect = [(2, ["err1"]), (3, ["err2", "err3"])]
        count, errors = CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        assert count == 5
        assert errors == ["err1", "err2", "err3"]

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_continues_when_later_folder_raises(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/a", "/b"]
        # First folder copies 5 files, second folder explodes — total
        # should still report the 5 from folder A and a recorded error.
        mock_download.side_effect = [
            (5, []),
            DarktableMCPError("Could not access camera at usb:002,002. ..."),
        ]
        count, errors = CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        assert count == 5
        assert any("/b" in e for e in errors)

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_first_folder_lock_error_propagates(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/a", "/b"]
        mock_download.side_effect = DarktableMCPError(
            "Could not access camera at usb:002,002. Another process is holding it."
        )
        with pytest.raises(DarktableMCPError, match="Another process"):
            CameraTools()._download_from_camera(
                "Nikon DSC D800E", "usb:002,002", tmp_path
            )

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_creates_destination(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/"]
        mock_download.return_value = (0, [])
        target = tmp_path / "new_dir"
        CameraTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", target)
        assert target.exists()
        assert target.is_dir()

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_default_timeout_passed_per_folder(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/a", "/b"]
        mock_download.return_value = (1, [])
        CameraTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)
        # timeout_seconds is the 5th positional arg (model, port, folder, dest, timeout)
        for call in mock_download.call_args_list:
            assert call.args[4] == 3600

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_custom_timeout_passed_per_folder(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/a", "/b"]
        mock_download.return_value = (1, [])
        CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path, timeout_seconds=120
        )
        for call in mock_download.call_args_list:
            assert call.args[4] == 120

    @patch.object(CameraTools, "_count_files_in_folder")
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_writes_progress_log_to_destination(
        self, mock_list, mock_download, mock_count, tmp_path
    ):
        mock_list.return_value = ["/store/DCIM/101"]
        mock_count.return_value = 5
        mock_download.return_value = (5, [])
        CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        log_path = tmp_path / ".import.log"
        assert log_path.exists()
        body = log_path.read_text()
        assert "Import started" in body
        assert "Camera: Nikon DSC D800E" in body
        assert "expected files: 5" in body
        assert "Import finished" in body

    @patch.object(CameraTools, "_count_files_in_folder")
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_post_flight_shortfall_recorded_in_errors(
        self, mock_list, mock_download, mock_count, tmp_path
    ):
        mock_list.return_value = ["/a"]
        mock_count.return_value = 10
        mock_download.return_value = (3, [])  # claimed only 3 saved
        # Simulate 3 files actually on disk to mirror the claim.
        for n in range(3):
            (tmp_path / f"file_{n}.NEF").write_bytes(b"")
        count, errors = CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        assert count == 3
        assert any("Post-flight" in e for e in errors)
        assert any("3/10" in e for e in errors)

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_passes_progress_log_kwarg_to_download(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/a"]
        mock_download.return_value = (0, [])
        CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        # progress_log must be passed in so per-file progress can be streamed
        kwargs = mock_download.call_args.kwargs
        assert "progress_log" in kwargs
        assert kwargs["progress_log"] is not None


class TestCameraToolsImportFromCamera:
    """Tests for CameraTools.import_from_camera."""

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_one_camera_default_destination(
        self, mock_detect, mock_download, tmp_path, monkeypatch
    ):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (5, [])
        # Force HOME so the default destination lands inside tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))

        tools = CameraTools()
        summary = tools.import_from_camera({})

        assert "Copied 5 new file(s)" in summary
        assert "Nikon DSC D800E" in summary
        # Default destination must include today's date
        dest_arg = mock_download.call_args[0][2]
        assert str(tmp_path) in str(dest_arg)
        assert "import-" in dest_arg.name

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_no_cameras_raises(self, mock_detect, _mock_download):
        mock_detect.return_value = []
        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="No camera detected"):
            tools.import_from_camera({})

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_multiple_cameras_without_port_raises(self, mock_detect, _mock_download):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"},
            {"model": "Canon EOS R5", "port": "usb:003,004"},
        ]
        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="Multiple cameras"):
            tools.import_from_camera({})

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_multiple_cameras_with_port_selects(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"},
            {"model": "Canon EOS R5", "port": "usb:003,004"},
        ]
        mock_download.return_value = (1, [])

        tools = CameraTools()
        tools.import_from_camera({"camera_port": "usb:003,004", "destination": str(tmp_path)})

        # The selected camera's model should be passed to the download
        called_model = mock_download.call_args[0][0]
        called_port = mock_download.call_args[0][1]
        assert called_model == "Canon EOS R5"
        assert called_port == "usb:003,004"

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_invalid_port_raises(self, mock_detect, _mock_download):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="not found"):
            tools.import_from_camera({"camera_port": "usb:999,999"})

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_partial_copy_reports_warning(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (3, ["ERROR: file X failed"])
        tools = CameraTools()
        summary = tools.import_from_camera({"destination": str(tmp_path)})
        assert "Copied 3 new file(s)" in summary
        assert "Warning" in summary
        assert "1 issue" in summary

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_one_camera_with_matching_port(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (4, [])

        tools = CameraTools()
        summary = tools.import_from_camera(
            {"camera_port": "usb:002,002", "destination": str(tmp_path)}
        )

        assert "Copied 4 new file(s)" in summary
        # Selection should use the matching camera even when only one exists
        assert mock_download.call_args[0][0] == "Nikon DSC D800E"
        assert mock_download.call_args[0][1] == "usb:002,002"

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_download_timeout_raises_clean_error(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.side_effect = subprocess.TimeoutExpired(cmd=["gphoto2"], timeout=600)

        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="timed out"):
            tools.import_from_camera({"destination": str(tmp_path)})

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_total_download_failure_raises(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (0, ["ERROR: device unreachable"])

        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="No files were transferred"):
            tools.import_from_camera({"destination": str(tmp_path)})

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_timeout_seconds_argument_flows_to_download(
        self, mock_detect, mock_download, tmp_path
    ):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (1, [])

        tools = CameraTools()
        tools.import_from_camera({"destination": str(tmp_path), "timeout_seconds": 120})

        # _download_from_camera receives timeout_seconds as the 4th positional arg
        assert mock_download.call_args[0][3] == 120

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_timeout_seconds_default_is_one_hour(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (1, [])

        tools = CameraTools()
        tools.import_from_camera({"destination": str(tmp_path)})

        assert mock_download.call_args[0][3] == 3600
