"""Tests for CameraTools module."""

import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from darktable_mcp.tools.camera_tools import CameraTools, _CameraFile
from darktable_mcp.utils.errors import DarktableMCPError

#: Captured before any patching so the tests that exercise the serial probe
#: itself can reach the real implementation past the autouse stub below.
_REAL_PROBE_SERIAL = CameraTools._probe_serial
#: Same trick for the file listing, whose own tests need the real thing.
_REAL_LIST_FILES = CameraTools._list_files_in_folder


@pytest.fixture(autouse=True)
def _no_file_listing():
    """Keep `gphoto2 --list-files` away from real hardware in every test.

    `_download_one_folder` asks the camera what a folder holds so it can
    decide for itself what still needs transferring. Two reasons to stub it
    by default: an unstubbed call would talk to a camera plugged into a
    developer machine, and — because `patch("...subprocess.Popen")` replaces
    the attribute on the shared subprocess module — it would otherwise be
    answered by whatever Popen mock the test installed for the *download*.

    The default is "no listing available", which is the degraded path: the
    whole folder is pulled into staging and de-duplicated by content when it
    is placed. Tests that want the cheap path patch this themselves.
    """
    with patch.object(CameraTools, "_list_files_in_folder", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_serial_probe():
    """Keep the serial probe away from real hardware in every test.

    `_camera_identity` shells out to `gphoto2 --get-config serialnumber`. On
    a developer machine that actually has gphoto2 and a camera plugged in,
    an unstubbed probe would talk to the camera during a unit-test run. The
    default here is "this body reports no serial", which is also the
    least-capable configuration, so tests that do not opt in are exercising
    the degraded path.
    """
    with patch.object(CameraTools, "_probe_serial", return_value=None):
        yield


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
    def test_detect_partial_failure_is_recorded_not_swallowed(self, mock_run, caplog):
        # One bus enumerated, another refused: the parsed camera is usable,
        # but this must not look like a clean detect.
        mock_run.return_value = Mock(
            returncode=1,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
                "Nikon DSC D800E                usb:002,002\n"
            ),
            stderr="*** Error ***\nCould not claim the USB device\n",
        )
        tools = CameraTools()
        with caplog.at_level("WARNING", logger="darktable_mcp.tools.camera_tools"):
            cameras = tools._detect_cameras()
        assert len(cameras) == 1
        assert tools.last_detect_warning is not None
        assert "Could not claim the USB device" in tools.last_detect_warning
        assert "Could not claim the USB device" in caplog.text

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_detect_clean_run_leaves_no_warning(self, mock_run):
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
        tools._detect_cameras()
        assert tools.last_detect_warning is None

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
        mock_run.return_value = Mock(returncode=0, stdout=self.DUAL_STORAGE_OUTPUT, stderr="")
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
        # CHANGED (staging fix): the returned count is now files that landed
        # in the destination, not "Saving file as" lines gphoto2 printed, so
        # the fake has to actually write them. That is the point of the
        # change — the old count could not tell a saved file from a claim.
        folder = "/store_00010001/DCIM/101D800E"
        mock_popen.side_effect = _FakeGphoto2(
            {folder: ["IMG_0001.NEF", "IMG_0002.NEF", "IMG_0003.NEF"]}
        )
        tools = CameraTools()
        count, skipped, errors = tools._download_one_folder(
            "Nikon DSC D800E",
            "usb:002,002",
            folder,
            tmp_path,
            timeout_seconds=600,
        )
        assert count == 3
        assert errors == []
        assert sorted(
            p.name
            for p in (tmp_path / tools._camera_folder_tag("Nikon_DSC_D800E", folder)).iterdir()
        ) == [
            "IMG_0001.NEF",
            "IMG_0002.NEF",
            "IMG_0003.NEF",
        ]

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
        # CHANGED (staging fix): a fake that writes the files, for the same
        # reason as test_download_one_folder_success — the count measures
        # placement now.
        mock_popen.side_effect = _FakeGphoto2(
            {
                "/": [
                    "IMG_0001.NEF",  # Nikon RAW
                    "IMG_0001.JPG",  # JPEG sidecar
                    "IMG_0002.CR2",  # Canon RAW
                    "IMG_0003.CR3",  # Canon RAW (newer)
                    "IMG_0004.ARW",  # Sony RAW
                    "IMG_0005.RAF",  # Fuji RAW
                    "IMG_0006.DNG",  # Adobe / Pentax / iPhone ProRAW
                    "MVI_0007.MP4",  # Video
                ]
            }
        )
        log_path = tmp_path / "progress.log"
        with open(log_path, "w", encoding="utf-8") as log:
            count, skipped, errors = CameraTools()._download_one_folder(
                "Some Camera",
                "usb:001,001",
                "/",
                tmp_path,
                timeout_seconds=60,
                progress_log=log,
            )
        assert count == 8
        assert errors == []
        body = log_path.read_text()
        for ext in ("NEF", "JPG", "CR2", "CR3", "ARW", "RAF", "DNG", "MP4"):
            assert ext in body

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_partial_failure(self, mock_popen, tmp_path):
        # CHANGED (staging fix): same reason — the two files that did arrive
        # have to exist for the count to see them. The third one erroring is
        # still reported from stderr.
        mock_popen.side_effect = _FakeGphoto2(
            {"/": ["IMG_0001.NEF", "IMG_0002.NEF"]},
            stderr="ERROR: Could not download IMG_0003.NEF\n",
            returncode=1,
        )
        tools = CameraTools()
        count, skipped, errors = tools._download_one_folder(
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
    def test_download_one_folder_skip_existing_only_is_not_lock_error(self, mock_popen, tmp_path):
        # When all files already exist on disk, gphoto2 prints "Skip
        # existing" lines and may still return rc=0 (or rc=1 in some
        # versions); either way, this is not a lock error. We must NOT
        # raise — just return (count=0, errors=[]).
        mock_popen.return_value = _popen_mock(
            stdout="Skip existing file /tmp/x/IMG_0001.NEF\n",
        )
        count, skipped, errors = CameraTools()._download_one_folder(
            "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
        )
        assert count == 0
        assert errors == []

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_download_one_folder_writes_progress_lines_to_log(self, mock_popen, tmp_path):
        # CHANGED (staging fix): a writing fake, so the returned count still
        # reflects two files. The log lines themselves are unchanged — they
        # are still written from gphoto2's stream as the transfer happens,
        # which is what `tail -f` depends on.
        mock_popen.side_effect = _FakeGphoto2({"/": ["A.NEF", "B.NEF"]})
        log_path = tmp_path / "progress.log"
        with open(log_path, "w", encoding="utf-8") as log:
            count, _, _ = CameraTools()._download_one_folder(
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


class TestCameraToolsFolderLayout:
    """Destination layout: one subdirectory per source folder.

    `%f` is the bare basename, so writing every camera folder into a single
    directory made `100NCD80/DSC_0001.NEF` and `101NCD80/DSC_0001.NEF`
    collide — and with `--skip-existing` the second photo was silently
    dropped. These pin the per-folder subdirectory that prevents it.
    """

    def test_folder_tag_keeps_whole_path_so_leaves_cannot_collide(self):
        a = CameraTools._folder_tag("/store_00010001/DCIM/101D800E")
        b = CameraTools._folder_tag("/store_00020001/DCIM/101D800E")
        assert a == "store_00010001_DCIM_101D800E"
        assert a != b

    def test_folder_tag_sanitises_and_handles_root(self):
        assert CameraTools._folder_tag("/") == CameraTools.ROOT_FOLDER_TAG
        assert CameraTools._folder_tag("") == CameraTools.ROOT_FOLDER_TAG
        assert CameraTools._folder_tag("/DCIM/My Card!/x") == "DCIM_My_Card_x"

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_filename_pattern_points_into_per_folder_subdir(self, mock_popen, tmp_path):
        mock_popen.return_value = _popen_mock()
        CameraTools()._download_one_folder(
            "Nikon DSC D800E",
            "usb:002,002",
            "/store_00010001/DCIM/101D800E",
            tmp_path,
            timeout_seconds=60,
        )
        cmd = mock_popen.call_args[0][0]
        pattern = cmd[cmd.index("--filename") + 1]
        # CHANGED (camera-identity fix): the tag is now prefixed with the
        # camera, because the folder path alone is generic across bodies.
        # A direct caller that passes no identity gets the model-derived
        # one — never a bare folder tag.
        # CHANGED (staging fix): gphoto2 no longer writes into the
        # destination at all. It writes into a private per-run directory
        # under it, and the files are moved into `<dest>/<tag>/` afterwards
        # by logic that can see what is already there. The tag still has to
        # be in the path, so it is still pinned here.
        tag = "Nikon_DSC_D800E_store_00010001_DCIM_101D800E"
        relative = Path(pattern).relative_to(tmp_path)
        assert relative.parts[0] == CameraTools.STAGING_DIR_NAME
        assert relative.parts[2] == tag
        assert pattern.endswith("%f.%C")
        assert (tmp_path / tag).is_dir()
        # And the staging directory does not survive the call.
        assert not (tmp_path / CameraTools.STAGING_DIR_NAME).exists()

    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_root_fallback_pattern_uses_camera_folder_placeholder(self, mock_popen, tmp_path):
        # When folder enumeration failed we do one recursive pull from "/".
        # gphoto2's own %F (camera folder path) keeps that recursion from
        # flattening two folders onto each other.
        mock_popen.return_value = _popen_mock()
        CameraTools()._download_one_folder(
            "Nikon DSC D800E", "usb:002,002", "/", tmp_path, timeout_seconds=60
        )
        cmd = mock_popen.call_args[0][0]
        pattern = cmd[cmd.index("--filename") + 1]
        # CHANGED (camera-identity fix): the recursive-fallback directory is
        # per-camera too, so two bodies falling back on the same day do not
        # both pour into <dest>/camera/.
        # CHANGED (staging fix): that directory is now reached in two steps,
        # via staging. %F is still in the pattern — the recursion must not
        # flatten two camera folders onto each other inside staging either,
        # and the relative tree is preserved when the files are placed.
        tag = f"Nikon_DSC_D800E_{CameraTools.ROOT_FOLDER_TAG}"
        relative = Path(pattern).relative_to(tmp_path)
        assert relative.parts[0] == CameraTools.STAGING_DIR_NAME
        assert relative.parts[2] == tag
        assert pattern.endswith("%F/%f.%C")


class _FakeGphoto2:
    """Stand-in for the gphoto2 binary that actually writes files.

    Reads `--folder` and `--filename` out of the command line, resolves the
    `%f` / `%C` / `%F` placeholders the same way gphoto2 does and writes one
    file per photo in that camera folder. `--skip-existing` is honoured per
    resolved target path — which is exactly the mechanism that used to eat
    photos when every folder resolved into the same directory, and which the
    fix demotes to a same-run guard inside a private staging directory.

    Also answers `--list-files` (via the `run` method, wired to
    `subprocess.run`) and honours `--get-file <range>`, so a test can watch
    the tool fetch *some* of a folder instead of all of it. `fetched` records
    every filename the fake was actually asked to transfer, which is how the
    cheap-resume tests prove that nothing came down the wire.
    """

    #: Every photo is this many bytes unless `sizes` says otherwise. Two
    #: bodies' `DSC_0001.NEF` are the same length on purpose.
    DEFAULT_SIZE = 64

    def __init__(self, tree, marker="", sizes=None, stderr="", returncode=0, refuse=()):
        """Build a fake camera.

        Args:
            tree: mapping of camera folder path -> list of filenames.
            marker: prefix written into each file's body. Two fakes with
                different markers stand for two different bodies holding
                different photos at byte-identical camera paths — the file
                bodies differ while the byte size stays the same, which is
                the shape that defeats every size-based comparison.
            sizes: optional filename -> byte size overrides.
            stderr: stderr text for the download invocation.
            returncode: exit status for the download invocation.
            refuse: filenames the camera lists but will not hand over, i.e.
                a read error on one photo of an otherwise healthy card.
        """
        self.tree = tree
        self.marker = marker
        self.sizes = sizes or {}
        self.stderr = stderr
        self.returncode = returncode
        self.refuse = set(refuse)
        self.calls = []
        self.fetched = []

    def _size(self, name):
        return self.sizes.get(name, self.DEFAULT_SIZE)

    def _body(self, folder, name):
        return f"{self.marker}{folder}/{name}".ljust(self._size(name))[: self._size(name)]

    @staticmethod
    def _selected(cmd, count):
        """Which 1-based file numbers this invocation asks for."""
        if "--get-all-files" in cmd:
            return list(range(1, count + 1))
        spec = cmd[cmd.index("--get-file") + 1]
        wanted = []
        for span in spec.split(","):
            if "-" in span:
                first, last = span.split("-")
                wanted.extend(range(int(first), int(last) + 1))
            else:
                wanted.append(int(span))
        return wanted

    def run(self, cmd, **kwargs):
        """`subprocess.run` side effect: answers `--list-files`."""
        self.calls.append(cmd)
        folder = cmd[cmd.index("--folder") + 1]
        names = self.tree.get(folder, [])
        lines = [f"There are {len(names)} files in folder '{folder}'.\n"]
        for position, name in enumerate(names, start=1):
            # Mirrors gphoto2's own columns, including its whole-KB sizes.
            lines.append(
                f"#{position:<5} {name:<27} rd  {self._size(name) // 1024} KB image/x-raw\n"
            )
        return Mock(returncode=0, stdout="".join(lines), stderr="")

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        folder = cmd[cmd.index("--folder") + 1]
        pattern = cmd[cmd.index("--filename") + 1]
        skip_existing = "--skip-existing" in cmd
        names = self.tree.get(folder, [])
        lines = []
        for position in self._selected(cmd, len(names)):
            name = names[position - 1]
            if name in self.refuse:
                continue
            stem, _, ext = name.rpartition(".")
            target = Path(
                pattern.replace("%F", folder.strip("/")).replace("%f", stem).replace("%C", ext)
            )
            if skip_existing and target.exists():
                lines.append(f"Skip existing file {target}\n")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self._body(folder, name))
            self.fetched.append(name)
            lines.append(f"Saving file as {target}\n")
        return _popen_mock(stdout="".join(lines), stderr=self.stderr, returncode=self.returncode)


class TestCameraToolsNoFilenameCollisions:
    """The dual-card case this tool exists for: identical names, two folders."""

    FOLDERS = ["/store_00010001/DCIM/100NCD80", "/store_00020001/DCIM/100NCD80"]
    TREE = {
        "/store_00010001/DCIM/100NCD80": ["DSC_0001.NEF"],
        "/store_00020001/DCIM/100NCD80": ["DSC_0001.NEF"],
    }

    @patch.object(CameraTools, "_count_files_in_folder", return_value=1)
    @patch.object(CameraTools, "_list_image_folders")
    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_same_name_in_two_folders_lands_as_two_files(
        self, mock_popen, mock_list, _mock_count, tmp_path
    ):
        mock_list.return_value = self.FOLDERS
        mock_popen.side_effect = _FakeGphoto2(self.TREE)

        saved, skipped, errors = CameraTools()._download_from_camera(
            "Nikon D850", "usb:002,002", tmp_path
        )

        assert saved == 2, "second card's DSC_0001.NEF must not be skipped"
        assert skipped == 0
        assert errors == []
        landed = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.NEF"))
        # CHANGED (camera-identity fix): paths gained the camera prefix.
        assert landed == [
            "Nikon_D850_store_00010001_DCIM_100NCD80/DSC_0001.NEF",
            "Nikon_D850_store_00020001_DCIM_100NCD80/DSC_0001.NEF",
        ]
        # Distinct content: these are genuinely two different photos.
        bodies = {p.read_text() for p in tmp_path.rglob("*.NEF")}
        assert len(bodies) == 2

    @patch.object(CameraTools, "_count_files_in_folder", return_value=1)
    @patch.object(CameraTools, "_list_image_folders")
    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_rerun_skips_and_reports_the_skip_count(
        self, mock_popen, mock_list, _mock_count, tmp_path
    ):
        mock_list.return_value = self.FOLDERS
        mock_popen.side_effect = _FakeGphoto2(self.TREE)
        tools = CameraTools()
        tools._download_from_camera("Nikon D850", "usb:002,002", tmp_path)

        # Second run over an unchanged card: nothing new, everything skipped.
        mock_popen.side_effect = _FakeGphoto2(self.TREE)
        saved, skipped, errors = tools._download_from_camera("Nikon D850", "usb:002,002", tmp_path)
        assert saved == 0
        assert skipped == 2
        assert errors == []
        assert len(list(tmp_path.rglob("*.NEF"))) == 2


class TestCameraToolsCountFilesOnDisk:
    """`_count_files_on_disk` feeds the shortfall check, so it must be recursive."""

    def test_counts_files_in_subdirectories(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b" / "deeper").mkdir(parents=True)
        (tmp_path / "a" / "DSC_0001.NEF").write_bytes(b"")
        (tmp_path / "b" / "DSC_0001.NEF").write_bytes(b"")
        (tmp_path / "b" / "deeper" / "DSC_0002.NEF").write_bytes(b"")
        (tmp_path / "loose.JPG").write_bytes(b"")
        assert CameraTools()._count_files_on_disk(tmp_path) == 4

    def test_excludes_progress_log_at_any_depth(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / CameraTools.PROGRESS_LOG_NAME).write_text("root log")
        (tmp_path / "a" / CameraTools.PROGRESS_LOG_NAME).write_text("nested log")
        (tmp_path / "a" / "DSC_0001.NEF").write_bytes(b"")
        assert CameraTools()._count_files_on_disk(tmp_path) == 1

    def test_missing_destination_counts_zero(self, tmp_path):
        assert CameraTools()._count_files_on_disk(tmp_path / "nope") == 0


class TestCameraToolsOverallTimeoutBudget:
    """`timeout_seconds` is a budget for the whole camera, not per folder."""

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_later_folders_get_the_remaining_budget(
        self, mock_list, mock_download, _mock_count, tmp_path, monkeypatch
    ):
        mock_list.return_value = ["/a", "/b"]
        mock_download.return_value = (1, 0, [])
        # Fake clock: 40 s elapse during the first folder.
        ticks = iter([0.0, 0.0, 40.0, 40.0])
        monkeypatch.setattr("darktable_mcp.tools.camera_tools.time.monotonic", lambda: next(ticks))
        CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path, timeout_seconds=100
        )
        timeouts = [call.args[4] for call in mock_download.call_args_list]
        assert timeouts == [100, 60]

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_exhausted_budget_stops_and_reports_untouched_folders(
        self, mock_list, mock_download, _mock_count, tmp_path, monkeypatch
    ):
        mock_list.return_value = ["/a", "/b", "/c"]
        mock_download.return_value = (2, 0, [])
        ticks = iter([0.0, 0.0, 999.0, 999.0])
        monkeypatch.setattr("darktable_mcp.tools.camera_tools.time.monotonic", lambda: next(ticks))
        saved, _skipped, errors = CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path, timeout_seconds=100
        )
        assert saved == 2
        assert mock_download.call_count == 1
        assert any("budget of 100 s ran out" in e for e in errors)
        assert any("2 folder(s) not copied" in e for e in errors)


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
        assert CameraTools()._count_files_in_folder("Nikon DSC D800E", "usb:002,002", "/") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_count_returns_none_on_unparseable_output(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="weird output", stderr="")
        assert CameraTools()._count_files_in_folder("Nikon DSC D800E", "usb:002,002", "/") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_count_returns_none_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gphoto2"], timeout=30)
        assert CameraTools()._count_files_in_folder("Nikon DSC D800E", "usb:002,002", "/") is None

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
    def test_iterates_each_storage_leaf(self, mock_list, mock_download, _mock_count, tmp_path):
        mock_list.return_value = [
            "/store_00010001/DCIM/101D800E",
            "/store_00020001/DCIM/101D800E",
        ]
        mock_download.return_value = (10, 0, [])
        tools = CameraTools()
        count, skipped, errors = tools._download_from_camera(
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
        mock_download.side_effect = [(2, 1, ["err1"]), (3, 0, ["err2", "err3"])]
        count, skipped, errors = CameraTools()._download_from_camera(
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
            (5, 0, []),
            DarktableMCPError("Could not access camera at usb:002,002. ..."),
        ]
        count, skipped, errors = CameraTools()._download_from_camera(
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
            CameraTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_creates_destination(self, mock_list, mock_download, _mock_count, tmp_path):
        mock_list.return_value = ["/"]
        mock_download.return_value = (0, 0, [])
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
        mock_download.return_value = (1, 0, [])
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
        mock_download.return_value = (1, 0, [])
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
        mock_download.return_value = (5, 0, [])
        CameraTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)
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
        mock_download.return_value = (3, 0, [])  # claimed only 3 saved
        # Simulate 3 files actually on disk to mirror the claim. They live
        # in the per-source-folder subdirectory now, not the destination
        # root, so the post-flight count has to look there.
        # CHANGED (camera-identity fix): that subdirectory carries the
        # camera prefix. This assertion is the wiring gate for the
        # shortfall check — if the check kept using the old bare folder tag
        # it would count an empty directory and report 0/10 here.
        folder_dest = tmp_path / "Nikon_DSC_D800E_a"
        folder_dest.mkdir()
        for n in range(3):
            (folder_dest / f"file_{n}.NEF").write_bytes(b"")
        count, skipped, errors = CameraTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        assert count == 3
        assert any(e.startswith(CameraTools.SHORTFALL_PREFIX) for e in errors)
        assert any("3/10" in e for e in errors)

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_passes_progress_log_kwarg_to_download(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/a"]
        mock_download.return_value = (0, 0, [])
        CameraTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)
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
        mock_download.return_value = (5, 0, [])
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
        with pytest.raises(DarktableMCPError, match="Multiple distinct cameras"):
            tools.import_from_camera({})

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_multiple_cameras_with_port_selects(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"},
            {"model": "Canon EOS R5", "port": "usb:003,004"},
        ]
        mock_download.return_value = (1, 0, [])

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
        mock_download.return_value = (3, 0, ["ERROR: file X failed"])
        tools = CameraTools()
        summary = tools.import_from_camera({"destination": str(tmp_path)})
        assert "Copied 3 new file(s)" in summary
        assert "Warning" in summary
        assert "1 issue" in summary

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_one_camera_with_matching_port(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (4, 0, [])

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
        mock_download.return_value = (0, 0, ["ERROR: device unreachable"])

        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="No files were transferred"):
            tools.import_from_camera({"destination": str(tmp_path)})

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_timeout_seconds_argument_flows_to_download(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (1, 0, [])

        tools = CameraTools()
        tools.import_from_camera({"destination": str(tmp_path), "timeout_seconds": 120})

        # _download_from_camera receives timeout_seconds as the 4th positional arg
        assert mock_download.call_args[0][3] == 120

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_summary_reports_skipped_files(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (2, 7, [])
        summary = CameraTools().import_from_camera({"destination": str(tmp_path)})
        # "Copied 2" alone would read as though the other 7 never existed.
        assert "Copied 2 new file(s)" in summary
        assert "Skipped 7 file(s) already present" in summary

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_shortfall_is_prominent_in_summary(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        shortfall = (
            f"{CameraTools.SHORTFALL_PREFIX} 90/100 files in destination " "— 10 short of expected"
        )
        mock_download.return_value = (90, 0, [shortfall])
        summary = CameraTools().import_from_camera({"destination": str(tmp_path)})
        # Must not hide behind a generic "1 issue(s)" line — the user is
        # about to format the card.
        assert "INCOMPLETE IMPORT" in summary
        assert "10 short of expected" in summary
        assert "Do NOT format the card" in summary
        assert "1 issue(s)" not in summary

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_partial_detect_warning_is_surfaced(self, mock_detect, mock_download, tmp_path):
        def detect(_self=None):
            tools.last_detect_warning = "gphoto2 --auto-detect exited with code 1 ..."
            return [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]

        tools = CameraTools()
        mock_detect.side_effect = detect
        mock_download.return_value = (1, 0, [])
        summary = tools.import_from_camera({"destination": str(tmp_path)})
        assert "Note: gphoto2 --auto-detect exited with code 1" in summary

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_timeout_seconds_default_is_one_hour(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (1, 0, [])

        tools = CameraTools()
        tools.import_from_camera({"destination": str(tmp_path)})

        assert mock_download.call_args[0][3] == 3600


# ----------------------------------------------------------------------------
# USB Mass-Storage handling: hybrid Nikon-style cameras expose one card via
# PTP and another as a USB-MSC mount. The pre-fix import_from_camera made
# the user pick one and silently dropped the other half — these tests pin
# the auto-merge behavior so that regression doesn't sneak back.
# ----------------------------------------------------------------------------


class TestCameraToolsMSCHelpers:
    """Pure helpers around the `disk:` / Mass-Storage port style."""

    def test_is_msc_port(self):
        assert CameraTools._is_msc_port("disk:/media/user/NIKON D800E") is True
        assert CameraTools._is_msc_port("usb:002,003") is False
        assert CameraTools._is_msc_port("") is False

    def test_msc_mount_strips_prefix(self):
        assert CameraTools._msc_mount("disk:/media/user/NIKON D800E") == __import__("pathlib").Path(
            "/media/user/NIKON D800E"
        )

    def test_msc_matches_ptp_for_nikon_hybrid(self):
        # The exact case from the real session: mount basename "NIKON
        # D800E" must match PTP model "Nikon DSC D800E" via shared 4+ char
        # tokens {"NIKON", "D800E"}.
        assert (
            CameraTools._msc_matches_ptp("disk:/media/andrii/NIKON D800E", "Nikon DSC D800E")
            is True
        )

    def test_msc_matches_ptp_rejects_unrelated_camera(self):
        # A Canon card in a card reader must NOT be paired with a Nikon
        # camera connected on PTP.
        assert (
            CameraTools._msc_matches_ptp("disk:/media/andrii/EOS_R5_DCIM", "Nikon DSC D800E")
            is False
        )

    def test_msc_matches_ptp_returns_false_for_non_disk_port(self):
        assert CameraTools._msc_matches_ptp("usb:002,003", "anything") is False


class TestCameraToolsGrouping:
    """`_group_cameras` decides which detected entries get imported together."""

    def test_groups_ptp_and_matching_msc_together(self):
        cams = [
            {"model": "Mass Storage Camera", "port": "disk:/media/user/NIKON D800E"},
            {"model": "Nikon DSC D800E", "port": "usb:002,003"},
        ]
        groups = CameraTools()._group_cameras(cams)
        assert len(groups) == 1
        ports = sorted(c["port"] for c in groups[0])
        assert ports == ["disk:/media/user/NIKON D800E", "usb:002,003"]

    def test_keeps_unrelated_cards_separate(self):
        cams = [
            {"model": "Nikon DSC D800E", "port": "usb:002,003"},
            {"model": "Mass Storage Camera", "port": "disk:/media/user/EOS_5D_MKIV"},
        ]
        groups = CameraTools()._group_cameras(cams)
        assert len(groups) == 2
        models_per_group = [{c["model"] for c in g} for g in groups]
        assert {"Nikon DSC D800E"} in models_per_group
        assert {"Mass Storage Camera"} in models_per_group

    def test_pure_ptp_setup_yields_one_group_per_camera(self):
        cams = [
            {"model": "Nikon DSC D800E", "port": "usb:002,003"},
            {"model": "Canon EOS R5", "port": "usb:003,004"},
        ]
        groups = CameraTools()._group_cameras(cams)
        assert len(groups) == 2

    def test_unmatched_msc_alone_is_its_own_group(self):
        cams = [
            {"model": "Mass Storage Camera", "port": "disk:/media/user/SOMECARD"},
        ]
        groups = CameraTools()._group_cameras(cams)
        assert len(groups) == 1
        assert groups[0][0]["port"] == "disk:/media/user/SOMECARD"


class TestCameraToolsDownloadFromMSC:
    """Direct filesystem walk for USB-MSC cards."""

    def _make_dcim(self, mount, layout):
        """Build a synthetic DCIM tree under `mount`. layout is dict of
        subfolder -> list of filenames. Files are tiny so size-compare works."""
        for sub, files in layout.items():
            d = mount / "DCIM" / sub
            d.mkdir(parents=True)
            for name in files:
                (d / name).write_bytes(name.encode())

    def test_walks_dcim_subfolders_and_copies_all_files(self, tmp_path):
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._make_dcim(
            mount,
            {
                "100D800E": ["DSC_0001.NEF", "DSC_0002.NEF"],
                "101D800E": ["DSC_0003.NEF"],
            },
        )
        saved, skipped, errors = CameraTools()._download_from_msc(mount, dest)
        assert saved == 3
        assert skipped == 0
        assert errors == []
        # One subdirectory per card folder, files inside them.
        assert sorted(str(p.relative_to(dest)) for p in dest.rglob("*.NEF")) == [
            "card_100D800E/DSC_0001.NEF",
            "card_100D800E/DSC_0002.NEF",
            "card_101D800E/DSC_0003.NEF",
        ]

    def test_skips_existing_same_size_files(self, tmp_path):
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._make_dcim(mount, {"100D800E": ["DSC_0001.NEF"]})
        # Pre-populate the destination with an identically-sized file, in
        # the subdirectory this card folder maps to.
        sub = dest / "card_100D800E"
        sub.mkdir(parents=True)
        (sub / "DSC_0001.NEF").write_bytes(b"DSC_0001.NEF")  # same length as source
        saved, skipped, errors = CameraTools()._download_from_msc(mount, dest)
        assert saved == 0
        assert skipped == 1
        assert errors == []

    def test_writes_progress_log_with_per_file_lines(self, tmp_path):
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._make_dcim(mount, {"100D800E": ["A.NEF", "B.NEF"]})
        CameraTools()._download_from_msc(mount, dest)
        log = (dest / ".import.log").read_text()
        assert "Import (MSC) started" in log
        assert "(1/2)" in log
        assert "(2/2)" in log
        assert "A.NEF" in log
        assert "B.NEF" in log
        assert "Import (MSC) finished" in log

    def test_no_dcim_returns_error(self, tmp_path):
        mount = tmp_path / "card"
        mount.mkdir()
        dest = tmp_path / "out"
        saved, skipped, errors = CameraTools()._download_from_msc(mount, dest)
        assert saved == 0
        assert any("DCIM" in e for e in errors)

    def test_same_name_same_size_in_two_dcim_folders_both_land(self, tmp_path):
        # Two folders, one filename, identical byte size — the exact shape
        # the old `dst.exists() and same size` test threw away.
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._make_dcim(
            mount,
            {
                "100NCD80": ["DSC_0001.NEF"],
                "101NCD80": ["DSC_0001.NEF"],
            },
        )
        (mount / "DCIM" / "100NCD80" / "DSC_0001.NEF").write_bytes(b"first-photo!")
        (mount / "DCIM" / "101NCD80" / "DSC_0001.NEF").write_bytes(b"second-photo")

        saved, skipped, errors = CameraTools()._download_from_msc(mount, dest)

        assert saved == 2, "second folder's DSC_0001.NEF must not be skipped"
        assert skipped == 0
        assert errors == []
        assert sorted(str(p.relative_to(dest)) for p in dest.rglob("*.NEF")) == [
            "card_100NCD80/DSC_0001.NEF",
            "card_101NCD80/DSC_0001.NEF",
        ]
        assert (dest / "card_100NCD80" / "DSC_0001.NEF").read_bytes() == b"first-photo!"
        assert (dest / "card_101NCD80" / "DSC_0001.NEF").read_bytes() == b"second-photo"

    def test_rerun_skips_everything_and_reports_the_count(self, tmp_path):
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._make_dcim(mount, {"100D800E": ["A.NEF", "B.NEF"]})
        tools = CameraTools()
        assert tools._download_from_msc(mount, dest)[:2] == (2, 0)
        saved, skipped, errors = tools._download_from_msc(mount, dest)
        assert (saved, skipped) == (0, 2)
        assert errors == []
        assert len(list(dest.rglob("*.NEF"))) == 2

    def test_timeout_budget_aborts_with_partial_progress_error(self, tmp_path, monkeypatch):
        # A flaky reader must not stall the import for an hour with no
        # explanation: the budget is checked between files.
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._make_dcim(mount, {"100D800E": ["A.NEF", "B.NEF", "C.NEF"]})
        # Clock: deadline calc, then one check per file. Budget blows after
        # the first file is copied.
        ticks = iter([0.0, 0.0, 500.0])
        monkeypatch.setattr("darktable_mcp.tools.camera_tools.time.monotonic", lambda: next(ticks))
        saved, skipped, errors = CameraTools()._download_from_msc(mount, dest, timeout_seconds=120)
        assert saved == 1
        assert len(errors) == 1
        assert "Timed out after 120 s" in errors[0]
        assert "copied 1 new" in errors[0]
        assert "2 file(s) not read" in errors[0]
        assert "!! Timed out" in (dest / ".import.log").read_text()


class TestCameraToolsHybridDispatch:
    """`_download_from_camera` routes disk:/ ports to the MSC walker."""

    @patch.object(CameraTools, "_download_from_msc")
    def test_disk_port_dispatches_to_msc(self, mock_msc, tmp_path):
        mock_msc.return_value = (5, 0, [])
        result = CameraTools()._download_from_camera(
            "Mass Storage Camera",
            "disk:/media/user/NIKON D800E",
            tmp_path,
        )
        assert result == (5, 0, [])
        mock_msc.assert_called_once()
        called_mount = mock_msc.call_args[0][0]
        assert str(called_mount) == "/media/user/NIKON D800E"

    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_list_image_folders", return_value=["/store/DCIM/101"])
    @patch.object(CameraTools, "_download_from_msc")
    def test_usb_port_does_not_use_msc(
        self, mock_msc, _mock_list, _mock_count, mock_download, tmp_path
    ):
        mock_download.return_value = (3, 0, [])
        CameraTools()._download_from_camera("Nikon DSC D800E", "usb:002,003", tmp_path)
        mock_msc.assert_not_called()


class TestCameraToolsImportFromCameraHybrid:
    """`import_from_camera` auto-merges PTP + matching MSC into one import."""

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_hybrid_nikon_imports_from_both_sources_without_camera_port(
        self, mock_detect, mock_download, tmp_path
    ):
        # The exact pair we hit on the real D800E shoot: PTP camera + a
        # generic Mass-Storage entry whose mount path identifies it as the
        # same Nikon body.
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,006"},
            {"model": "Mass Storage Camera", "port": "disk:/media/user/NIKON D800E"},
        ]
        mock_download.return_value = (10, 0, [])
        tools = CameraTools()
        summary = tools.import_from_camera({"destination": str(tmp_path)})
        # Both sources were imported, totals are summed.
        assert mock_download.call_count == 2
        called_ports = sorted(call.args[1] for call in mock_download.call_args_list)
        assert called_ports == ["disk:/media/user/NIKON D800E", "usb:002,006"]
        assert "Copied 20 new file(s)" in summary

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_hybrid_with_camera_port_picks_the_group_containing_that_port(
        self, mock_detect, mock_download, tmp_path
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,006"},
            {"model": "Mass Storage Camera", "port": "disk:/media/user/NIKON D800E"},
        ]
        mock_download.return_value = (4, 0, [])
        tools = CameraTools()
        # Picking the MSC port still pulls the whole group (because the
        # pair is one logical device).
        tools.import_from_camera(
            {
                "destination": str(tmp_path),
                "camera_port": "disk:/media/user/NIKON D800E",
            }
        )
        assert mock_download.call_count == 2

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_two_unrelated_cameras_still_require_camera_port(
        self, mock_detect, mock_download, tmp_path
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,006"},
            {"model": "Mass Storage Camera", "port": "disk:/media/user/EOS_5D_MKIV"},
        ]
        tools = CameraTools()
        with pytest.raises(DarktableMCPError, match="Multiple distinct"):
            tools.import_from_camera({"destination": str(tmp_path)})
        mock_download.assert_not_called()


# ----------------------------------------------------------------------------
# Camera identity in the destination tag.
#
# A per-folder subdirectory only separates folders. The gphoto2 folder path
# `/store_00010001/DCIM/100NCD80` is what *every* Nikon of that generation
# reports, so two bodies imported into one destination — the documented
# resume workflow, and the default ~/Pictures/import-<today>/ that every
# import on the same day shares — collided again, and `--skip-existing`
# dropped the newcomer. These pin the identity that keeps them apart.
# ----------------------------------------------------------------------------


class TestCameraToolsModelTag:
    """`_model_tag` turns a gphoto2 model string into a tag fragment."""

    def test_sanitises_a_real_model(self):
        assert CameraTools._model_tag("Nikon DSC D800E") == "Nikon_DSC_D800E"
        assert CameraTools._model_tag("Canon EOS R5") == "Canon_EOS_R5"

    def test_strips_path_hostile_characters(self):
        assert CameraTools._model_tag("Sony ILCE-7M4 (Control)") == "Sony_ILCE_7M4_Control"

    def test_generic_mass_storage_model_yields_no_identity(self):
        # gphoto2 calls every USB-MSC mount this. Baking it into the tag
        # would look like identity while providing none, and would make two
        # unrelated cards look like the same device.
        assert CameraTools._model_tag("Mass Storage Camera") == ""
        assert CameraTools._model_tag("USB PTP Class Camera") == "PTP_Class"

    def test_empty_model_is_tolerated(self):
        assert CameraTools._model_tag("") == ""


class TestCameraToolsSerialProbe:
    """`_probe_serial` adds real per-device identity — when it can."""

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_parses_current_line_and_strips_zero_padding(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Label: Serial Number\n"
                "Readonly: 0\n"
                "Type: TEXT\n"
                "Current: 00000000000000000000000030014567\n"
                "END\n"
            ),
            stderr="",
        )
        assert _REAL_PROBE_SERIAL(CameraTools(), "Nikon DSC D800E", "usb:002,002") == "30014567"
        cmd = mock_run.call_args[0][0]
        assert "--get-config" in cmd
        assert "serialnumber" in cmd

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_all_zero_serial_is_not_an_identity(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="Current: 0000000000000000\n", stderr="")
        assert _REAL_PROBE_SERIAL(CameraTools(), "Some Camera", "usb:001,001") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_unsupported_config_degrades_to_none(self, mock_run):
        # Most compacts and every MSC mount have no serialnumber config.
        mock_run.return_value = Mock(
            returncode=1, stdout="", stderr="*** Error: unknown config name\n"
        )
        assert _REAL_PROBE_SERIAL(CameraTools(), "Some Camera", "usb:001,001") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_unparseable_output_degrades_to_none(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="weird output\n", stderr="")
        assert _REAL_PROBE_SERIAL(CameraTools(), "Some Camera", "usb:001,001") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_missing_binary_degrades_instead_of_raising(self, mock_run):
        # Unlike the other helpers this one must not raise: the serial is an
        # optional tag component and the import has to survive without it.
        mock_run.side_effect = FileNotFoundError("gphoto2")
        assert _REAL_PROBE_SERIAL(CameraTools(), "Some Camera", "usb:001,001") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_timeout_degrades_to_none(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gphoto2"], timeout=15)
        assert _REAL_PROBE_SERIAL(CameraTools(), "Some Camera", "usb:001,001") is None


class TestCameraToolsIdentity:
    """`_camera_identity` composes the stable per-device tag fragment."""

    def test_model_only_when_no_serial_is_reported(self):
        assert CameraTools()._camera_identity("Nikon DSC D800E", "usb:002,002") == (
            "Nikon_DSC_D800E"
        )

    @patch.object(CameraTools, "_probe_serial", return_value="30014567")
    def test_serial_is_appended_when_available(self, _mock_probe):
        assert CameraTools()._camera_identity("Nikon DSC D800E", "usb:002,002") == (
            "Nikon_DSC_D800E_sn_30014567"
        )

    @patch.object(CameraTools, "_probe_serial", return_value="30014567")
    def test_identity_never_contains_the_port(self, _mock_probe):
        # gphoto2 reassigns the port on every re-plug. A port in the tag
        # would scatter one camera across a new subdirectory per session and
        # re-download the whole card each time.
        first = CameraTools()._camera_identity("Nikon DSC D800E", "usb:002,004")
        second = CameraTools()._camera_identity("Nikon DSC D800E", "usb:003,011")
        assert first == second
        assert "002" not in first
        assert "usb" not in first

    def test_no_identity_at_all_is_tolerated(self):
        assert CameraTools()._camera_identity("Mass Storage Camera", "disk:/x") == ""

    def test_folder_tag_without_identity_falls_back_to_the_bare_path_tag(self):
        assert CameraTools._camera_folder_tag("", "/store_1/DCIM/100NCD80") == (
            "store_1_DCIM_100NCD80"
        )


class TestCameraToolsCrossCameraCollisions:
    """Two bodies, one destination — the collision the folder tag reopened."""

    FOLDERS = ["/store_00010001/DCIM/100NCD80"]
    TREE = {"/store_00010001/DCIM/100NCD80": ["DSC_0001.NEF"]}

    @patch.object(CameraTools, "_count_files_in_folder", return_value=1)
    @patch.object(CameraTools, "_list_image_folders")
    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_two_models_sharing_a_folder_path_keep_both_files(
        self, mock_popen, mock_list, _mock_count, tmp_path
    ):
        # Same gphoto2 folder path, same filename, same byte size, different
        # photo. Before the identity prefix the second body's DSC_0001.NEF
        # was silently dropped by --skip-existing.
        mock_list.return_value = self.FOLDERS
        tools = CameraTools()

        mock_popen.side_effect = _FakeGphoto2(self.TREE, marker="NIKON-")
        saved_a, _, _ = tools._download_from_camera("Nikon D850", "usb:002,002", tmp_path)
        mock_popen.side_effect = _FakeGphoto2(self.TREE, marker="CANON-")
        saved_b, skipped_b, errors_b = tools._download_from_camera(
            "Canon EOS R5", "usb:003,004", tmp_path
        )

        assert saved_a == 1
        assert saved_b == 1, "the second body's DSC_0001.NEF must not be skipped"
        assert skipped_b == 0
        assert errors_b == []
        landed = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.NEF"))
        assert landed == [
            "Canon_EOS_R5_store_00010001_DCIM_100NCD80/DSC_0001.NEF",
            "Nikon_D850_store_00010001_DCIM_100NCD80/DSC_0001.NEF",
        ]
        bodies = {p.read_text() for p in tmp_path.rglob("*.NEF")}
        assert len(bodies) == 2, "both photos must survive, not one twice"

    @patch.object(CameraTools, "_count_files_in_folder", return_value=1)
    @patch.object(CameraTools, "_list_image_folders")
    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_same_model_two_serials_do_not_share_a_destination(
        self, mock_popen, mock_list, _mock_count, tmp_path
    ):
        # Two identical Nikon bodies. The model alone cannot tell them
        # apart; the serial can, and it is what stops the second body's
        # photos from being skipped as "already copied".
        mock_list.return_value = self.FOLDERS
        tools = CameraTools()
        serials = {"usb:002,002": "30014567", "usb:002,005": "30019999"}

        with patch.object(CameraTools, "_probe_serial", side_effect=lambda m, p: serials[p]):
            mock_popen.side_effect = _FakeGphoto2(self.TREE, marker="BODY-A-")
            tools._download_from_camera("Nikon D850", "usb:002,002", tmp_path)
            mock_popen.side_effect = _FakeGphoto2(self.TREE, marker="BODY-B-")
            saved, skipped, _ = tools._download_from_camera("Nikon D850", "usb:002,005", tmp_path)

        assert (saved, skipped) == (1, 0)
        landed = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.NEF"))
        assert landed == [
            "Nikon_D850_sn_30014567_store_00010001_DCIM_100NCD80/DSC_0001.NEF",
            "Nikon_D850_sn_30019999_store_00010001_DCIM_100NCD80/DSC_0001.NEF",
        ]

    @patch.object(CameraTools, "_count_files_in_folder", return_value=1)
    @patch.object(CameraTools, "_list_image_folders")
    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_resume_produces_stable_paths_across_two_runs(
        self, mock_popen, mock_list, _mock_count, tmp_path
    ):
        # Cheap idempotent resume is the whole reason the tag must not
        # contain anything session-scoped. Same camera, same destination,
        # second run: identical paths, nothing new, everything skipped.
        mock_list.return_value = self.FOLDERS
        tools = CameraTools()
        with patch.object(CameraTools, "_probe_serial", return_value="30014567"):
            mock_popen.side_effect = _FakeGphoto2(self.TREE, marker="BODY-A-")
            tools._download_from_camera("Nikon D850", "usb:002,002", tmp_path)
            first = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.NEF"))

            # Re-plugged: gphoto2 handed out a different port this time.
            mock_popen.side_effect = _FakeGphoto2(self.TREE, marker="BODY-A-")
            saved, skipped, errors = tools._download_from_camera(
                "Nikon D850", "usb:003,017", tmp_path
            )
            second = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.NEF"))

        assert (saved, skipped) == (0, 1)
        assert errors == []
        assert first == second
        assert len(second) == 1

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_identity_is_passed_to_every_folder_download(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        # The wiring gate: a correct identity helper that the download path
        # never calls would leave the collision wide open.
        mock_list.return_value = ["/a", "/b"]
        mock_download.return_value = (1, 0, [])
        with patch.object(CameraTools, "_probe_serial", return_value="30014567"):
            CameraTools()._download_from_camera("Nikon D850", "usb:002,002", tmp_path)
        for call in mock_download.call_args_list:
            assert call.kwargs["identity"] == "Nikon_D850_sn_30014567"

    @patch.object(CameraTools, "_count_files_in_folder", return_value=None)
    @patch.object(CameraTools, "_download_one_folder")
    @patch.object(CameraTools, "_list_image_folders")
    def test_serial_is_probed_once_per_camera_not_once_per_folder(
        self, mock_list, mock_download, _mock_count, tmp_path
    ):
        mock_list.return_value = ["/a", "/b", "/c"]
        mock_download.return_value = (1, 0, [])
        with patch.object(CameraTools, "_probe_serial", return_value="30014567") as mock_probe:
            CameraTools()._download_from_camera("Nikon D850", "usb:002,002", tmp_path)
        assert mock_download.call_count == 3
        assert mock_probe.call_count == 1

    @patch.object(CameraTools, "_count_files_in_folder", return_value=1)
    @patch.object(CameraTools, "_list_image_folders")
    @patch("darktable_mcp.tools.camera_tools.subprocess.Popen")
    def test_shortfall_check_still_fires_with_the_identity_prefixed_tag(
        self, mock_popen, mock_list, _mock_count, tmp_path
    ):
        # The post-flight count has to look inside the *new* directory name.
        # If it kept using the bare folder tag it would find nothing and cry
        # shortfall on a perfectly good import — and once desensitised, the
        # real shortfall would be ignored too.
        mock_list.return_value = self.FOLDERS
        mock_popen.side_effect = _FakeGphoto2(self.TREE, marker="NIKON-")
        saved, _, errors = CameraTools()._download_from_camera(
            "Nikon D850", "usb:002,002", tmp_path
        )
        assert saved == 1
        assert not [e for e in errors if e.startswith(CameraTools.SHORTFALL_PREFIX)]


class TestCameraToolsSameFileHeuristic:
    """`_looks_like_same_file` decides skip vs keep-both. Never overwrite."""

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_identical_files_match(self, tmp_path):
        a = self._write(tmp_path / "a.NEF", b"x" * 40000)
        b = self._write(tmp_path / "b.NEF", b"x" * 40000)
        assert CameraTools._looks_like_same_file(a, b) is True

    def test_different_size_never_matches(self, tmp_path):
        a = self._write(tmp_path / "a.NEF", b"x" * 40000)
        b = self._write(tmp_path / "b.NEF", b"x" * 39999)
        assert CameraTools._looks_like_same_file(a, b) is False

    def test_same_size_different_header_is_caught(self, tmp_path):
        # Two exposures of the same scene compress to the same size often
        # enough; their EXIF timestamps never match.
        a = self._write(tmp_path / "a.NEF", b"EXIF-2024-01-01" + b"x" * 40000)
        b = self._write(tmp_path / "b.NEF", b"EXIF-2024-06-30" + b"x" * 40000)
        assert CameraTools._looks_like_same_file(a, b) is False

    def test_same_size_different_tail_is_caught(self, tmp_path):
        a = self._write(tmp_path / "a.NEF", b"x" * 40000 + b"AAAA")
        b = self._write(tmp_path / "b.NEF", b"x" * 40000 + b"BBBB")
        assert CameraTools._looks_like_same_file(a, b) is False

    def test_small_files_are_compared_whole(self, tmp_path):
        # Under 2x the sample size there is no middle to miss.
        a = self._write(tmp_path / "a.NEF", b"first-photo!")
        b = self._write(tmp_path / "b.NEF", b"second-photo")
        assert CameraTools._looks_like_same_file(a, b) is False

    def test_middle_only_difference_is_the_documented_limit(self, tmp_path):
        # Honest about the bound: an 8 KB head + 8 KB tail sample cannot see
        # a change confined to the middle of a large file. That is the
        # bounded cost the comment in _looks_like_same_file argues for — a
        # full hash would read every byte of every raw on the card. The
        # heuristic accepts a strict subset of what the old size-only test
        # accepted, so it is never worse than what it replaced.
        n = CameraTools.EDGE_SAMPLE_BYTES
        a = self._write(tmp_path / "a.NEF", b"h" * n + b"A" * 100 + b"t" * n)
        b = self._write(tmp_path / "b.NEF", b"h" * n + b"B" * 100 + b"t" * n)
        assert CameraTools._looks_like_same_file(a, b) is True

    def test_unreadable_destination_counts_as_not_matching(self, tmp_path):
        a = self._write(tmp_path / "a.NEF", b"data")
        assert CameraTools._looks_like_same_file(a, tmp_path / "missing.NEF") is False

    def test_empty_files_match(self, tmp_path):
        a = self._write(tmp_path / "a.NEF", b"")
        b = self._write(tmp_path / "b.NEF", b"")
        assert CameraTools._looks_like_same_file(a, b) is True


class TestCameraToolsDistinctName:
    """Alternative names are deterministic, so a resume reuses them."""

    def test_inserts_index_before_the_extension(self):
        assert CameraTools._distinct_name("IMG_0001.CR3", 2) == "IMG_0001-2.CR3"
        assert CameraTools._distinct_name("DSC_0001.NEF", 7) == "DSC_0001-7.NEF"

    def test_extensionless_name_gets_a_suffix(self):
        assert CameraTools._distinct_name("RAWFILE", 2) == "RAWFILE-2"

    def test_only_the_last_dot_is_treated_as_the_extension(self):
        assert CameraTools._distinct_name("IMG_0001.sidecar.xmp", 2) == ("IMG_0001.sidecar-2.xmp")


class TestCameraToolsMSCNeverDestroys:
    """`_download_from_msc` must not overwrite, and must not skip blindly.

    A card label is not a device identity: two cards formatted in the same
    body are both `EOS_DIGITAL`, and a single-slot reader gives them the
    same mount path. So the destination directory can legitimately already
    hold a *different* photo under the same name. The old code either
    skipped it (equal size) or ran `shutil.copy2` over it (different size),
    destroying a file that was already safely on disk.
    """

    def _card(self, mount, folder, files):
        """(Re)create a card at `mount`. files: name -> bytes."""
        if mount.exists():
            shutil.rmtree(mount)
        d = mount / "DCIM" / folder
        d.mkdir(parents=True)
        for name, data in files.items():
            (d / name).write_bytes(data)

    def test_two_cards_with_the_same_label_keep_both_photos(self, tmp_path):
        mount = tmp_path / "EOS_DIGITAL"
        dest = tmp_path / "out"
        tools = CameraTools()

        self._card(mount, "100EOS5D", {"IMG_0001.CR3": b"card-A-photo"})
        assert tools._download_from_msc(mount, dest)[:2] == (1, 0)

        # Same single-slot reader, second card out of the same body: the
        # label, the mount path, the DCIM folder, the filename and the byte
        # size are all identical. Only the photo differs.
        self._card(mount, "100EOS5D", {"IMG_0001.CR3": b"card-B-photo"})
        saved, skipped, notes = tools._download_from_msc(mount, dest)

        assert (saved, skipped) == (1, 0), "card B's photo must be copied, not skipped"
        sub = dest / "EOS_DIGITAL_100EOS5D"
        assert sub.joinpath("IMG_0001.CR3").read_bytes() == b"card-A-photo"
        assert sub.joinpath("IMG_0001-2.CR3").read_bytes() == b"card-B-photo"
        assert [n for n in notes if n.startswith(CameraTools.RENAMED_PREFIX)]
        assert "IMG_0001-2.CR3" in notes[0]

    def test_existing_file_of_a_different_size_is_never_overwritten(self, tmp_path):
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._card(mount, "100D800E", {"DSC_0001.NEF": b"new-photo-from-the-card"})
        sub = dest / "card_100D800E"
        sub.mkdir(parents=True)
        precious = sub / "DSC_0001.NEF"
        precious.write_bytes(b"a-different-photo-already-safely-on-disk")

        saved, skipped, notes = CameraTools()._download_from_msc(mount, dest)

        assert (saved, skipped) == (1, 0)
        assert precious.read_bytes() == b"a-different-photo-already-safely-on-disk"
        assert sub.joinpath("DSC_0001-2.NEF").read_bytes() == b"new-photo-from-the-card"
        assert [n for n in notes if n.startswith(CameraTools.RENAMED_PREFIX)]

    def test_same_size_rerun_still_skips_and_reports_the_skip_count(self, tmp_path):
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._card(mount, "100D800E", {"A.NEF": b"photo-one!!", "B.NEF": b"photo-two!!"})
        tools = CameraTools()
        assert tools._download_from_msc(mount, dest)[:2] == (2, 0)

        saved, skipped, notes = tools._download_from_msc(mount, dest)
        assert (saved, skipped) == (0, 2), "an unchanged card must be a cheap no-op"
        assert notes == []
        assert len(list(dest.rglob("*.NEF"))) == 2

    def test_rerun_after_a_name_conflict_does_not_keep_growing_copies(self, tmp_path):
        # Resume must be idempotent even once an alternative name exists,
        # otherwise every re-run adds -3, -4, -5 ... and the destination
        # becomes unusable.
        mount = tmp_path / "EOS_DIGITAL"
        dest = tmp_path / "out"
        tools = CameraTools()
        self._card(mount, "100EOS5D", {"IMG_0001.CR3": b"card-A-photo"})
        tools._download_from_msc(mount, dest)
        self._card(mount, "100EOS5D", {"IMG_0001.CR3": b"card-B-photo"})
        tools._download_from_msc(mount, dest)

        saved, skipped, notes = tools._download_from_msc(mount, dest)
        assert (saved, skipped) == (0, 1)
        assert notes == []
        assert sorted(p.name for p in dest.rglob("*.CR3")) == [
            "IMG_0001-2.CR3",
            "IMG_0001.CR3",
        ]

    def test_exhausting_the_alternatives_errors_instead_of_overwriting(self, tmp_path):
        mount = tmp_path / "card"
        dest = tmp_path / "out"
        self._card(mount, "100D800E", {"DSC_0001.NEF": b"newcomer"})
        sub = dest / "card_100D800E"
        sub.mkdir(parents=True)
        sub.joinpath("DSC_0001.NEF").write_bytes(b"occupied")
        for index in range(2, CameraTools.MAX_DISTINCT_SUFFIX + 1):
            sub.joinpath(f"DSC_0001-{index}.NEF").write_bytes(b"occupied")

        saved, skipped, errors = CameraTools()._download_from_msc(mount, dest)

        assert (saved, skipped) == (0, 0)
        assert any("refusing to overwrite" in e for e in errors)
        assert sub.joinpath("DSC_0001.NEF").read_bytes() == b"occupied"

    def test_conflicting_copy_is_logged(self, tmp_path):
        mount = tmp_path / "EOS_DIGITAL"
        dest = tmp_path / "out"
        tools = CameraTools()
        self._card(mount, "100EOS5D", {"IMG_0001.CR3": b"card-A-photo"})
        tools._download_from_msc(mount, dest)
        self._card(mount, "100EOS5D", {"IMG_0001.CR3": b"card-B-photo"})
        tools._download_from_msc(mount, dest)
        log = (dest / ".import.log").read_text()
        assert CameraTools.RENAMED_PREFIX in log
        assert "IMG_0001-2.CR3" in log


class TestCameraToolsMSCFolderTag:
    """The MSC destination tag folds in the model only when it adds identity."""

    def test_generic_mass_storage_model_does_not_pollute_the_tag(self):
        tag = CameraTools._msc_folder_tag(
            Path("/media/user/NIKON D800E"),
            Path("/media/user/NIKON D800E/DCIM/100NCD80"),
            "Mass Storage Camera",
        )
        assert tag == "NIKON_D800E_100NCD80"

    def test_model_already_echoed_by_the_label_is_not_repeated(self):
        tag = CameraTools._msc_folder_tag(
            Path("/media/user/NIKON D800E"),
            Path("/media/user/NIKON D800E/DCIM/100NCD80"),
            "Nikon DSC D800E",
        )
        assert tag == "NIKON_D800E_100NCD80"

    def test_informative_model_is_prefixed(self):
        # A card reader mounts by label only: "Untitled" says nothing about
        # which body wrote the card, so the model earns its place.
        tag = CameraTools._msc_folder_tag(
            Path("/media/user/Untitled"),
            Path("/media/user/Untitled/DCIM/100EOS5D"),
            "Canon EOS R5",
        )
        assert tag == "Canon_EOS_R5_Untitled_100EOS5D"

    def test_no_model_keeps_the_previous_label_only_tag(self):
        tag = CameraTools._msc_folder_tag(
            Path("/media/user/EOS_DIGITAL"),
            Path("/media/user/EOS_DIGITAL/DCIM/100EOS5D"),
        )
        assert tag == "EOS_DIGITAL_100EOS5D"

    @patch.object(CameraTools, "_download_from_msc", return_value=(1, 0, []))
    def test_dispatch_threads_the_model_into_the_msc_walker(self, mock_msc, tmp_path):
        CameraTools()._download_from_camera(
            "Mass Storage Camera", "disk:/media/user/EOS_DIGITAL", tmp_path
        )
        assert mock_msc.call_args.kwargs["model"] == "Mass Storage Camera"


class TestCameraToolsImportReportsNameConflicts:
    """A kept-both rename is not an error, but the user must be told."""

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_rename_notice_is_its_own_section_not_a_generic_issue(
        self, mock_detect, mock_download, tmp_path
    ):
        mock_detect.return_value = [
            {"model": "Mass Storage Camera", "port": "disk:/media/user/EOS_DIGITAL"}
        ]
        note = (
            f"{CameraTools.RENAMED_PREFIX} EOS_DIGITAL_100EOS5D/IMG_0001.CR3 is a "
            "different file from the one already in the destination; both kept, "
            "the new one as IMG_0001-2.CR3"
        )
        mock_download.return_value = (1, 0, [note])
        summary = CameraTools().import_from_camera({"destination": str(tmp_path)})
        assert "Kept both copies for 1 name conflict(s)" in summary
        assert "IMG_0001-2.CR3" in summary
        assert "nothing was overwritten" in summary
        # Must not be buried in the generic warning line.
        assert "issue(s)" not in summary

    @patch.object(CameraTools, "_download_from_camera")
    @patch.object(CameraTools, "_detect_cameras")
    def test_clean_import_has_no_conflict_section(self, mock_detect, mock_download, tmp_path):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (5, 0, [])
        summary = CameraTools().import_from_camera({"destination": str(tmp_path)})
        assert "name conflict" not in summary


def _run_ptp_import(
    fake,
    destination,
    folders,
    model="Nikon D850",
    port="usb:002,002",
    serial=None,
    tools=None,
    expected=None,
):
    """Drive one whole PTP import against a fake camera.

    Wires the fake to both halves of the gphoto2 CLI — `subprocess.run` for
    `--list-files`, `subprocess.Popen` for the transfers — and restores the
    real `_list_files_in_folder`, which the autouse fixture stubs out. The
    folder walk and the `--num-files` pre-flight are stubbed because they
    would otherwise be answered by the same `subprocess.run` mock.

    Args:
        fake: a `_FakeGphoto2`.
        destination: import root.
        folders: what `_list_image_folders` should report.
        model: gphoto2 model string.
        port: gphoto2 port string.
        serial: what the body reports for `serialnumber`. None is the
            interesting case — that is the body that cannot be told apart
            from another of the same model.
        tools: reuse an existing CameraTools instance if given.
        expected: what `--num-files` reports per folder.

    Returns:
        Whatever `_download_from_camera` returns.
    """
    tools = tools or CameraTools()
    patches = [
        patch.object(CameraTools, "_list_image_folders", return_value=folders),
        patch.object(CameraTools, "_count_files_in_folder", return_value=expected),
        patch.object(CameraTools, "_list_files_in_folder", _REAL_LIST_FILES),
        patch.object(CameraTools, "_probe_serial", return_value=serial),
        patch("darktable_mcp.tools.camera_tools.subprocess.run", side_effect=fake.run),
        patch("darktable_mcp.tools.camera_tools.subprocess.Popen", side_effect=fake),
    ]
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        return tools._download_from_camera(model, port, destination)


def _transfer_calls(fake):
    """The gphoto2 invocations that actually moved data off the camera."""
    return [c for c in fake.calls if "--get-all-files" in c or "--get-file" in c]


class TestCameraToolsListFilesParsing:
    """`--list-files` is the resume mechanism now, so its parser is load-bearing."""

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_parses_gphoto2_columns_including_kilobyte_sizes(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "There are 2 files in folder '/store_00010001/DCIM/100NCD80'.\n"
                "#1     DSC_0001.NEF               rd  9863 KB image/x-nikon-nef\n"
                "#2     DSC_0002.NEF               rd  9871 KB image/x-nikon-nef\n"
            ),
            stderr="",
        )
        entries = _REAL_LIST_FILES(
            CameraTools(), "Nikon D850", "usb:002,002", "/store_00010001/DCIM/100NCD80"
        )
        assert [(e.number, e.name, e.size_kb) for e in entries] == [
            (1, "DSC_0001.NEF", 9863),
            (2, "DSC_0002.NEF", 9871),
        ]
        cmd = mock_run.call_args[0][0]
        assert "--list-files" in cmd
        assert "/store_00010001/DCIM/100NCD80" in cmd

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_raw_byte_sizes_are_normalised_to_kilobytes(self, mock_run):
        # Not every build prints KB. A bare byte count must not be read as
        # kilobytes, or every size compare would be off by a factor of 1024
        # and nothing would ever be recognised as already copied.
        mock_run.return_value = Mock(
            returncode=0,
            stdout="#1     IMG_0001.JPG               rd  2097152 image/jpeg\n",
            stderr="",
        )
        entries = _REAL_LIST_FILES(CameraTools(), "Some Camera", "usb:001,001", "/")
        assert entries[0].size_kb == 2048

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_line_without_a_size_yields_unknown_not_zero(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0,
            stdout="#1     DSC_0001.NEF               rd  image/x-nikon-nef\n",
            stderr="",
        )
        entries = _REAL_LIST_FILES(CameraTools(), "Nikon D850", "usb:002,002", "/x")
        assert entries[0].size_kb is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_nonzero_exit_degrades_to_none(self, mock_run):
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="*** Error ***\n")
        assert _REAL_LIST_FILES(CameraTools(), "Nikon D850", "usb:002,002", "/x") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_unparseable_output_degrades_to_none(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="weird output\n", stderr="")
        assert _REAL_LIST_FILES(CameraTools(), "Nikon D850", "usb:002,002", "/x") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_missing_binary_degrades_instead_of_raising(self, mock_run):
        # Like the serial probe: the listing is an optimisation, and losing
        # it must cost speed, not the import.
        mock_run.side_effect = FileNotFoundError("gphoto2")
        assert _REAL_LIST_FILES(CameraTools(), "Nikon D850", "usb:002,002", "/x") is None

    @patch("darktable_mcp.tools.camera_tools.subprocess.run")
    def test_timeout_degrades_to_none(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gphoto2"], timeout=60)
        assert _REAL_LIST_FILES(CameraTools(), "Nikon D850", "usb:002,002", "/x") is None


class TestCameraToolsRangeExpression:
    """`--get-file` takes gphoto2's RANGE syntax; a wrong range fetches wrong files."""

    def test_consecutive_numbers_collapse_into_a_span(self):
        assert CameraTools._range_expression([1, 2, 3]) == "1-3"

    def test_gaps_are_preserved(self):
        assert CameraTools._range_expression([1, 3, 4, 5, 9]) == "1,3-5,9"

    def test_unordered_input_is_sorted_and_deduplicated(self):
        assert CameraTools._range_expression([5, 1, 5, 2]) == "1-2,5"

    def test_empty_selection_is_empty(self):
        assert CameraTools._range_expression([]) == ""


class TestCameraToolsIdentityStrength:
    """Only a serial number makes an identity unique to one physical body."""

    def test_serial_identity_is_unique(self):
        assert CameraTools._identity_is_body_unique("Nikon_D850_sn_30014567") is True

    def test_model_only_identity_is_not_unique(self):
        assert CameraTools._identity_is_body_unique("Nikon_D850") is False

    def test_empty_identity_is_not_unique(self):
        assert CameraTools._identity_is_body_unique("") is False

    def test_a_model_word_spelled_sn_is_not_a_serial(self):
        # `_model_tag` upper-cases nothing, but model words arrive as the
        # camera spells them. The marker this checks for is the literal
        # lower-case `sn_` prefix `_camera_identity` writes.
        assert CameraTools._identity_is_body_unique("Canon_SN_5") is False


class TestCameraToolsDestinationHolds:
    """The cheap "already there?" test: name plus a kilobyte-granular size."""

    def _entry(self, name="DSC_0001.NEF", number=1, size_kb=0):
        return _CameraFile(number=number, name=name, size_kb=size_kb)

    def test_missing_name_is_not_held(self, tmp_path):
        assert CameraTools._destination_holds(tmp_path, self._entry()) is False

    def test_matching_name_and_size_is_held(self, tmp_path):
        (tmp_path / "DSC_0001.NEF").write_bytes(b"x" * 2048)
        assert CameraTools._destination_holds(tmp_path, self._entry(size_kb=2)) is True

    def test_same_name_very_different_size_is_not_held(self, tmp_path):
        (tmp_path / "DSC_0001.NEF").write_bytes(b"x" * 2048)
        assert CameraTools._destination_holds(tmp_path, self._entry(size_kb=9000)) is False

    def test_a_previous_conflict_copy_counts_as_holding_it(self, tmp_path):
        # The second body's photo lives under -2. Without this, that body
        # would re-fetch its whole card on every resume and grow a -3.
        (tmp_path / "DSC_0001.NEF").write_bytes(b"a" * 2048)
        (tmp_path / "DSC_0001-2.NEF").write_bytes(b"b" * 4096)
        assert CameraTools._destination_holds(tmp_path, self._entry(size_kb=4)) is True

    def test_unknown_size_falls_back_to_the_name(self, tmp_path):
        (tmp_path / "DSC_0001.NEF").write_bytes(b"x" * 2048)
        assert CameraTools._destination_holds(tmp_path, self._entry(size_kb=None)) is True


class TestCameraToolsPtpNeverLosesAPhoto:
    """The gap this change closes: two bodies, one destination, PTP.

    Two Nikons of the same model that report no serial number resolve to the
    same identity, so they share a destination subdirectory. Their photos
    share names *and* sizes. gphoto2's `--skip-existing` compared nothing but
    the path, so the second body's DSC_0001.NEF was dropped inside gphoto2's
    own process with no error and a destination that looked complete.
    """

    FOLDER = "/store_00010001/DCIM/100NCD80"
    FOLDERS = [FOLDER]
    TREE = {FOLDER: ["DSC_0001.NEF", "DSC_0002.NEF"]}
    TAG = "Nikon_D850_store_00010001_DCIM_100NCD80"

    def _body(self, marker):
        return _FakeGphoto2(self.TREE, marker=marker)

    def test_second_body_with_the_same_names_and_sizes_keeps_both_photos(self, tmp_path):
        first = self._body("A")
        saved, skipped, errors = _run_ptp_import(first, tmp_path, self.FOLDERS)
        assert (saved, skipped) == (2, 0)

        second = self._body("B")
        saved, skipped, errors = _run_ptp_import(second, tmp_path, self.FOLDERS)

        assert saved == 2, "the second body's photos must not be skipped"
        assert skipped == 0
        landed = sorted(p.name for p in (tmp_path / self.TAG).iterdir())
        assert landed == [
            "DSC_0001-2.NEF",
            "DSC_0001.NEF",
            "DSC_0002-2.NEF",
            "DSC_0002.NEF",
        ]
        bodies = {p.read_text() for p in (tmp_path / self.TAG).iterdir()}
        assert len(bodies) == 4, "four genuinely different photos are on disk"
        renames = [e for e in errors if e.startswith(CameraTools.RENAMED_PREFIX)]
        assert len(renames) == 2, "the user is told which files were renamed"
        assert not [e for e in errors if e.startswith(CameraTools.SHORTFALL_PREFIX)]

    def test_the_second_body_resumes_cheaply_and_does_not_grow_a_third_copy(self, tmp_path):
        _run_ptp_import(self._body("A"), tmp_path, self.FOLDERS)
        _run_ptp_import(self._body("B"), tmp_path, self.FOLDERS)

        again = self._body("B")
        saved, skipped, errors = _run_ptp_import(again, tmp_path, self.FOLDERS)

        assert saved == 0
        assert skipped == 2
        assert len(list((tmp_path / self.TAG).iterdir())) == 4, "no -3 copies"
        # Cheap: only the identity sample came down the wire, not the folder.
        assert sorted(again.fetched) == ["DSC_0001.NEF", "DSC_0002.NEF"]

    def test_the_first_body_still_resumes_without_being_confused_by_the_second(self, tmp_path):
        _run_ptp_import(self._body("A"), tmp_path, self.FOLDERS)
        _run_ptp_import(self._body("B"), tmp_path, self.FOLDERS)

        again = self._body("A")
        saved, skipped, _errors = _run_ptp_import(again, tmp_path, self.FOLDERS)
        assert (saved, skipped) == (0, 2)
        assert len(list((tmp_path / self.TAG).iterdir())) == 4

    def test_the_conflict_is_reported_in_the_import_summary(self, tmp_path):
        _run_ptp_import(self._body("A"), tmp_path, self.FOLDERS)
        second = self._body("B")
        with patch.object(
            CameraTools,
            "_detect_cameras",
            return_value=[{"model": "Nikon D850", "port": "usb:002,002"}],
        ):
            patches = [
                patch.object(CameraTools, "_list_image_folders", return_value=self.FOLDERS),
                patch.object(CameraTools, "_count_files_in_folder", return_value=None),
                patch.object(CameraTools, "_list_files_in_folder", _REAL_LIST_FILES),
                patch("darktable_mcp.tools.camera_tools.subprocess.run", side_effect=second.run),
                patch("darktable_mcp.tools.camera_tools.subprocess.Popen", side_effect=second),
            ]
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                summary = CameraTools().import_from_camera({"destination": str(tmp_path)})
        assert "Kept both copies for 2 name conflict(s)" in summary
        assert "DSC_0001-2.NEF" in summary


class TestCameraToolsPtpResumeStaysCheap:
    """Requirement two: an ordinary re-run must not re-read the card."""

    FOLDER = "/store_00010001/DCIM/100NCD80"
    FOLDERS = [FOLDER]
    TREE = {FOLDER: [f"DSC_{n:04d}.NEF" for n in range(1, 6)]}
    TAG_NO_SERIAL = "Nikon_D850_store_00010001_DCIM_100NCD80"
    TAG_SERIAL = "Nikon_D850_sn_30014567_store_00010001_DCIM_100NCD80"

    def test_a_body_with_a_serial_number_transfers_nothing_on_a_re_run(self, tmp_path):
        first = _FakeGphoto2(self.TREE, marker="A")
        _run_ptp_import(first, tmp_path, self.FOLDERS, serial="30014567")
        before = sorted(p.name for p in (tmp_path / self.TAG_SERIAL).iterdir())

        again = _FakeGphoto2(self.TREE, marker="A")
        saved, skipped, errors = _run_ptp_import(again, tmp_path, self.FOLDERS, serial="30014567")

        assert (saved, skipped) == (0, 5)
        assert errors == []
        assert again.fetched == [], "a serial number makes the directory provably this body's"
        assert _transfer_calls(again) == [], "gphoto2 was not even asked to transfer"
        assert sorted(p.name for p in (tmp_path / self.TAG_SERIAL).iterdir()) == before

    def test_a_body_without_a_serial_pays_only_the_bounded_sample(self, tmp_path):
        _run_ptp_import(_FakeGphoto2(self.TREE, marker="A"), tmp_path, self.FOLDERS)

        again = _FakeGphoto2(self.TREE, marker="A")
        saved, skipped, errors = _run_ptp_import(again, tmp_path, self.FOLDERS)

        assert (saved, skipped) == (0, 5)
        assert errors == []
        assert len(again.fetched) == CameraTools.PROBE_SAMPLE_SIZE
        assert len(list((tmp_path / self.TAG_NO_SERIAL).iterdir())) == 5

    def test_resume_produces_the_same_paths_twice(self, tmp_path):
        _run_ptp_import(_FakeGphoto2(self.TREE, marker="A"), tmp_path, self.FOLDERS)
        first = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.NEF"))
        _run_ptp_import(_FakeGphoto2(self.TREE, marker="A"), tmp_path, self.FOLDERS)
        second = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.NEF"))
        assert first == second
        assert not (tmp_path / CameraTools.STAGING_DIR_NAME).exists()

    def test_only_the_new_photos_are_fetched_after_more_shooting(self, tmp_path):
        _run_ptp_import(
            _FakeGphoto2(self.TREE, marker="A"), tmp_path, self.FOLDERS, serial="30014567"
        )

        grown = dict(self.TREE)
        grown[self.FOLDER] = self.TREE[self.FOLDER] + ["DSC_0006.NEF", "DSC_0007.NEF"]
        again = _FakeGphoto2(grown, marker="A")
        saved, skipped, _errors = _run_ptp_import(again, tmp_path, self.FOLDERS, serial="30014567")

        assert (saved, skipped) == (2, 5)
        assert again.fetched == ["DSC_0006.NEF", "DSC_0007.NEF"]
        # Fetched by number, as a range, rather than pulling the folder.
        transfer = _transfer_calls(again)[0]
        assert "--get-file" in transfer
        assert transfer[transfer.index("--get-file") + 1] == "6-7"


class TestCameraToolsPtpTellsTheUserWhatDidNotArrive:
    """Requirement one: no photo goes missing without the user hearing it."""

    FOLDER = "/store_00010001/DCIM/100NCD80"
    FOLDERS = [FOLDER]
    TREE = {FOLDER: ["DSC_0001.NEF", "DSC_0002.NEF", "DSC_0003.NEF"]}

    def test_a_file_the_camera_refuses_to_send_is_named_in_the_errors(self, tmp_path):
        fake = _FakeGphoto2(self.TREE, marker="A", refuse=["DSC_0002.NEF"])
        saved, _skipped, errors = _run_ptp_import(fake, tmp_path, self.FOLDERS)

        assert saved == 2
        shortfalls = [e for e in errors if e.startswith(CameraTools.SHORTFALL_PREFIX)]
        assert shortfalls, "a file that never arrived must be reported"
        assert "DSC_0002.NEF" in shortfalls[0]

    def test_a_clean_import_reports_no_shortfall(self, tmp_path):
        fake = _FakeGphoto2(self.TREE, marker="A")
        _saved, _skipped, errors = _run_ptp_import(fake, tmp_path, self.FOLDERS)
        assert errors == []

    def test_files_that_cannot_be_placed_are_kept_and_reported(self, tmp_path):
        # The failure mode the staging design introduces: a download that
        # succeeds but cannot be moved into place. The file must not be
        # thrown away with the staging directory.
        fake = _FakeGphoto2(self.TREE, marker="A")
        with patch.object(
            CameraTools, "_never_overwrite_target", side_effect=OSError("read-only file system")
        ):
            saved, _skipped, errors = _run_ptp_import(fake, tmp_path, self.FOLDERS)

        assert saved == 0
        shortfalls = [e for e in errors if e.startswith(CameraTools.SHORTFALL_PREFIX)]
        assert any("could not be moved" in e for e in shortfalls)
        staged = sorted(p.name for p in (tmp_path / CameraTools.STAGING_DIR_NAME).rglob("*.NEF"))
        assert staged == ["DSC_0001.NEF", "DSC_0002.NEF", "DSC_0003.NEF"]

    def test_an_unreadable_sample_forces_a_full_fetch_instead_of_a_skip(self, tmp_path):
        # If the camera cannot re-send the sample, nothing has been proven,
        # so nothing may be skipped: fetch everything and let placement
        # decide by content.
        _run_ptp_import(_FakeGphoto2(self.TREE, marker="A"), tmp_path, self.FOLDERS)

        again = _FakeGphoto2(self.TREE, marker="A", refuse=self.TREE[self.FOLDER])
        saved, skipped, _errors = _run_ptp_import(again, tmp_path, self.FOLDERS)
        assert saved == 0
        assert skipped == 0, "nothing was proven, so nothing was counted as skipped"
        assert _transfer_calls(again), "the folder was re-requested rather than trusted"


class TestCameraToolsPtpSampleIsBounded:
    """The hole that is left, pinned so it cannot be quietly forgotten."""

    FOLDER = "/store_00010001/DCIM/100NCD80"
    FOLDERS = [FOLDER]
    NAMES = [f"DSC_{n:04d}.NEF" for n in range(1, 9)]
    TREE = {FOLDER: NAMES}
    TAG = "Nikon_D850_store_00010001_DCIM_100NCD80"

    class _PartlyIdenticalBody(_FakeGphoto2):
        """A second body whose sampled photos happen to be byte-identical."""

        DIFFERENT = "DSC_0004.NEF"

        def _body(self, folder, name):
            marker = self.marker if name == self.DIFFERENT else ""
            return f"{marker}{folder}/{name}".ljust(self._size(name))[: self._size(name)]

    def test_a_difference_outside_the_sample_is_not_detected(self, tmp_path):
        _run_ptp_import(self._PartlyIdenticalBody(self.TREE, marker=""), tmp_path, self.FOLDERS)
        second = self._PartlyIdenticalBody(self.TREE, marker="B")
        saved, skipped, _errors = _run_ptp_import(second, tmp_path, self.FOLDERS)

        # Documented in the CameraTools docstring: the sample is bounded, so
        # a second body that matches on every sampled file is taken to be
        # the same body and its differing photo is skipped. Closing this
        # would mean downloading every file on every run.
        assert (saved, skipped) == (0, 8)
        assert len(list((tmp_path / self.TAG).iterdir())) == 8
        assert len(second.fetched) == CameraTools.PROBE_SAMPLE_SIZE

    def test_a_folder_no_bigger_than_the_sample_is_checked_exhaustively(self, tmp_path):
        small = {self.FOLDER: self.NAMES[: CameraTools.PROBE_SAMPLE_SIZE]}
        _run_ptp_import(_FakeGphoto2(small, marker="A"), tmp_path, self.FOLDERS)
        second = _FakeGphoto2(small, marker="B")
        saved, _skipped, _errors = _run_ptp_import(second, tmp_path, self.FOLDERS)
        assert saved == CameraTools.PROBE_SAMPLE_SIZE
        assert len(list((tmp_path / self.TAG).iterdir())) == 2 * CameraTools.PROBE_SAMPLE_SIZE


class TestCameraToolsStagingHousekeeping:
    """Staging is inside the destination, so it must never be counted or left."""

    def test_staged_files_are_not_counted_as_imported(self, tmp_path):
        (tmp_path / "Nikon_D850_a").mkdir()
        (tmp_path / "Nikon_D850_a" / "DSC_0001.NEF").write_bytes(b"x")
        staged = tmp_path / CameraTools.STAGING_DIR_NAME / "123-abc" / "Nikon_D850_a"
        staged.mkdir(parents=True)
        (staged / "DSC_0002.NEF").write_bytes(b"y")
        assert CameraTools()._count_files_on_disk(tmp_path) == 1

    def test_a_finished_folder_leaves_no_staging_directory(self, tmp_path):
        folder = "/store_00010001/DCIM/100NCD80"
        fake = _FakeGphoto2({folder: ["DSC_0001.NEF"]}, marker="A")
        _run_ptp_import(fake, tmp_path, [folder])
        assert not (tmp_path / CameraTools.STAGING_DIR_NAME).exists()

    def test_a_timeout_places_what_arrived_and_drops_the_truncated_file(self, tmp_path):
        # gphoto2 announces a file before writing it, so the last announced
        # file is the one that was in flight when the clock ran out. Placing
        # it would put a truncated photo in the destination under the real
        # name, where the next run would find it and keep it forever.
        folder = "/store_00010001/DCIM/100NCD80"
        tag = "Nikon_D850_store_00010001_DCIM_100NCD80"

        def _side_effect(cmd, **kwargs):
            pattern = cmd[cmd.index("--filename") + 1]
            base = Path(pattern.replace("%f.%C", ""))
            base.mkdir(parents=True, exist_ok=True)
            lines = []
            for name, content in (
                ("DSC_0001.NEF", "complete-1".ljust(64)),
                ("DSC_0002.NEF", "complete-2".ljust(64)),
                ("DSC_0003.NEF", "trunc"),
            ):
                (base / name).write_text(content)
                lines.append(f"Saving file as {base / name}\n")
            return _popen_mock(stdout="".join(lines), raises_timeout=True)

        with pytest.raises(subprocess.TimeoutExpired):
            with patch(
                "darktable_mcp.tools.camera_tools.subprocess.Popen", side_effect=_side_effect
            ):
                CameraTools()._download_one_folder(
                    "Nikon D850", "usb:002,002", folder, tmp_path, timeout_seconds=1
                )

        assert sorted(p.name for p in (tmp_path / tag).iterdir()) == [
            "DSC_0001.NEF",
            "DSC_0002.NEF",
        ]
        assert not (tmp_path / CameraTools.STAGING_DIR_NAME).exists()
