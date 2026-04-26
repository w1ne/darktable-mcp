"""Tests for PhotoTools module."""

import subprocess
from unittest.mock import Mock, patch

import pytest

from darktable_mcp.tools.photo_tools import PhotoTools
from darktable_mcp.utils.errors import DarktableMCPError


class TestPhotoToolsViewPhotos:
    """Test cases for PhotoTools.view_photos()"""

    def test_view_photos_basic(self):
        """Test basic view_photos functionality."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = '[{"id": "123", "filename": "test.jpg"}]'

            tools = PhotoTools()
            result = tools.view_photos({"filter": "", "limit": 10})

            assert len(result) == 1
            assert result[0]["id"] == "123"
            mock_executor.execute_script.assert_called_once()

    def test_view_photos_with_filter(self):
        """Test view_photos with filename filter."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = (
                '[{"id": "456", "filename": "vacation.jpg"}]'
            )

            tools = PhotoTools()
            result = tools.view_photos({"filter": "vacation", "limit": 10})

            assert len(result) == 1
            assert result[0]["filename"] == "vacation.jpg"
            mock_executor.execute_script.assert_called_once()

    def test_view_photos_with_rating_min(self):
        """Test view_photos with minimum rating filter."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = (
                '[{"id": "789", "filename": "best.jpg", "rating": 4}]'
            )

            tools = PhotoTools()
            result = tools.view_photos({"filter": "", "rating_min": 4, "limit": 10})

            assert len(result) == 1
            assert result[0]["rating"] == 4
            mock_executor.execute_script.assert_called_once()

    def test_view_photos_empty_result(self):
        """Test view_photos when no photos match."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "[]"

            tools = PhotoTools()
            result = tools.view_photos({"filter": "nonexistent", "limit": 10})

            assert len(result) == 0
            mock_executor.execute_script.assert_called_once()

    def test_view_photos_json_parse_error(self):
        """Test view_photos handles JSON parsing errors."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "invalid json"

            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.view_photos({"filter": "", "limit": 10})

            assert "Failed to parse photo data" in str(exc_info.value)


class TestPhotoToolsRatePhotos:
    """Test cases for PhotoTools.rate_photos()"""

    def test_rate_photos_basic(self):
        """Test basic rate_photos functionality."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Updated 2 photos with 4 stars"

            tools = PhotoTools()
            result = tools.rate_photos({"photo_ids": ["123", "456"], "rating": 4})

            assert "Updated 2 photos" in result
            mock_executor.execute_script.assert_called_once()

    def test_rate_photos_missing_photo_ids(self):
        """Test rate_photos raises error when photo_ids missing."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.rate_photos({"rating": 4})

            assert "photo_ids is required" in str(exc_info.value)

    def test_rate_photos_empty_photo_ids(self):
        """Test rate_photos raises error when photo_ids is empty."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.rate_photos({"photo_ids": [], "rating": 4})

            assert "photo_ids is required" in str(exc_info.value)

    def test_rate_photos_invalid_rating_too_low(self):
        """Test rate_photos raises error for rating < 1."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.rate_photos({"photo_ids": ["123"], "rating": 0})

            assert "rating must be between 1 and 5" in str(exc_info.value)

    def test_rate_photos_invalid_rating_too_high(self):
        """Test rate_photos raises error for rating > 5."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.rate_photos({"photo_ids": ["123"], "rating": 6})

            assert "rating must be between 1 and 5" in str(exc_info.value)

    def test_rate_photos_single_photo(self):
        """Test rate_photos with a single photo."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Updated 1 photos with 5 stars"

            tools = PhotoTools()
            result = tools.rate_photos({"photo_ids": ["999"], "rating": 5})

            assert "Updated 1 photos" in result
            mock_executor.execute_script.assert_called_once()


class TestPhotoToolsImportBatch:
    """Test cases for PhotoTools.import_batch()"""

    def test_import_batch_basic(self):
        """Test basic import_batch functionality."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Imported 5 photos from /path/to/photos"

            tools = PhotoTools()
            result = tools.import_batch({"source_path": "/path/to/photos", "recursive": True})

            assert "Imported 5 photos" in result
            mock_executor.execute_script.assert_called_once()

    def test_import_batch_missing_source_path(self):
        """Test import_batch raises error when source_path missing."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.import_batch({"recursive": True})

            assert "source_path is required" in str(exc_info.value)

    def test_import_batch_non_recursive(self):
        """Test import_batch with recursive=False."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Imported 3 photos from /path/to/photos"

            tools = PhotoTools()
            result = tools.import_batch({"source_path": "/path/to/photos", "recursive": False})

            assert "Imported 3 photos" in result
            mock_executor.execute_script.assert_called_once()

    def test_import_batch_default_recursive(self):
        """Test import_batch defaults recursive to False."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Imported 2 photos from /path/to/photos"

            tools = PhotoTools()
            result = tools.import_batch({"source_path": "/path/to/photos"})

            assert "Imported 2 photos" in result
            mock_executor.execute_script.assert_called_once()


class TestPhotoToolsAdjustExposure:
    """Test cases for PhotoTools.adjust_exposure()"""

    def test_adjust_exposure_basic(self):
        """Test basic adjust_exposure functionality."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Adjusted exposure for 2 photos"

            tools = PhotoTools()
            result = tools.adjust_exposure({"photo_ids": ["123", "456"], "exposure_ev": 1.5})

            assert "Adjusted exposure" in result
            # Verify GUI mode was used (headless=False)
            mock_executor.execute_script.assert_called_once()
            call_kwargs = mock_executor.execute_script.call_args[1]
            assert call_kwargs["headless"] is False
            assert call_kwargs["gui_purpose"] == "Show exposure adjustment preview"

    def test_adjust_exposure_validation_too_high(self):
        """Test adjust_exposure raises error for exposure > 5.0."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.adjust_exposure({"photo_ids": ["123"], "exposure_ev": 6.0})

            assert "exposure_ev must be between" in str(exc_info.value)

    def test_adjust_exposure_validation_too_low(self):
        """Test adjust_exposure raises error for exposure < -5.0."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.adjust_exposure({"photo_ids": ["123"], "exposure_ev": -6.0})

            assert "exposure_ev must be between" in str(exc_info.value)

    def test_adjust_exposure_missing_photo_ids(self):
        """Test adjust_exposure raises error when photo_ids missing."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.adjust_exposure({"exposure_ev": 1.0})

            assert "photo_ids is required" in str(exc_info.value)

    def test_adjust_exposure_single_photo(self):
        """Test adjust_exposure with a single photo."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Adjusted exposure for 1 photos by 2.0 EV"

            tools = PhotoTools()
            result = tools.adjust_exposure({"photo_ids": ["999"], "exposure_ev": 2.0})

            assert "Adjusted exposure" in result
            mock_executor.execute_script.assert_called_once()

    def test_adjust_exposure_boundary_values(self):
        """Test adjust_exposure with boundary exposure values."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Adjusted exposure for 1 photos by 5.0 EV"

            tools = PhotoTools()
            # Test max value
            result = tools.adjust_exposure({"photo_ids": ["123"], "exposure_ev": 5.0})
            assert "Adjusted exposure" in result

            # Test min value
            mock_executor.execute_script.return_value = "Adjusted exposure for 1 photos by -5.0 EV"
            result = tools.adjust_exposure({"photo_ids": ["123"], "exposure_ev": -5.0})
            assert "Adjusted exposure" in result


class TestPhotoToolsDetectCameras:
    """Tests for PhotoTools._detect_cameras helper."""

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_one_camera(self, mock_run, _mock_executor):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
                "Nikon DSC D800E                usb:002,002\n"
            ),
            stderr="",
        )
        tools = PhotoTools()
        cameras = tools._detect_cameras()
        assert cameras == [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_run.assert_called_once_with(
            ["gphoto2", "--auto-detect"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_none(self, mock_run, _mock_executor):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
            ),
            stderr="",
        )
        tools = PhotoTools()
        assert tools._detect_cameras() == []

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_multiple(self, mock_run, _mock_executor):
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
        tools = PhotoTools()
        cameras = tools._detect_cameras()
        assert len(cameras) == 2
        assert cameras[0]["model"] == "Nikon DSC D800E"
        assert cameras[1]["port"] == "usb:003,004"

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_gphoto2_missing(self, mock_run, _mock_executor):
        mock_run.side_effect = FileNotFoundError("gphoto2")
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="gphoto2 not installed"):
            tools._detect_cameras()

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_timeout(self, mock_run, _mock_executor):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["gphoto2", "--auto-detect"], timeout=10
        )
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="timed out"):
            tools._detect_cameras()

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_locked_by_gvfs(self, mock_run, _mock_executor):
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
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="Could not lock"):
            tools._detect_cameras()


class TestPhotoToolsDownloadFromCamera:
    """Tests for PhotoTools._download_from_camera helper."""

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_success(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Saving file as /tmp/dest/IMG_0001.NEF\n"
                "Saving file as /tmp/dest/IMG_0002.NEF\n"
                "Saving file as /tmp/dest/IMG_0003.NEF\n"
            ),
            stderr="",
        )
        tools = PhotoTools()
        count, errors = tools._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)
        assert count == 3
        assert errors == []

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gphoto2"
        assert "--camera" in cmd
        assert "Nikon DSC D800E" in cmd
        assert "--port" in cmd
        assert "usb:002,002" in cmd
        assert "--get-all-files" in cmd

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_partial_failure(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(
            returncode=1,
            stdout=(
                "Saving file as /tmp/dest/IMG_0001.NEF\n" "Saving file as /tmp/dest/IMG_0002.NEF\n"
            ),
            stderr="ERROR: Could not download IMG_0003.NEF\n",
        )
        tools = PhotoTools()
        count, errors = tools._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)
        assert count == 2
        assert any("IMG_0003" in e for e in errors)

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_creates_destination(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        target = tmp_path / "new_dir"
        tools = PhotoTools()
        tools._download_from_camera("Nikon DSC D800E", "usb:002,002", target)
        assert target.exists()
        assert target.is_dir()

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_gphoto2_missing(self, mock_run, _mock_executor, tmp_path):
        mock_run.side_effect = FileNotFoundError("gphoto2")
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="gphoto2 not installed"):
            tools._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_passes_c_locale_env(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        PhotoTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)
        env = mock_run.call_args.kwargs.get("env")
        assert env is not None
        assert env.get("LC_ALL") == "C"
        assert env.get("LANG") == "C"

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_uses_skip_existing(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        PhotoTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--skip-existing" in cmd

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_default_timeout_is_one_hour(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        PhotoTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)
        assert mock_run.call_args.kwargs["timeout"] == 3600

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_respects_custom_timeout(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        PhotoTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path, timeout_seconds=120
        )
        assert mock_run.call_args.kwargs["timeout"] == 120

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_surfaces_gvfs_lock_error(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr=(
                "*** Error ***\n"
                "An error occurred in the io-layer ('Could not lock the device')\n"
            ),
        )
        with pytest.raises(DarktableMCPError, match="Another process is "):
            PhotoTools()._download_from_camera("Nikon DSC D800E", "usb:002,002", tmp_path)

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_skip_existing_only_is_not_lock_error(
        self, mock_run, _mock_executor, tmp_path
    ):
        # When all files already exist on disk, gphoto2 prints "Skip
        # existing" lines and may still return rc=0 (or rc=1 in some
        # versions); either way, this is not a lock error. We must NOT
        # raise — just return (count=0, errors=[]).
        mock_run.return_value = Mock(
            returncode=0,
            stdout="Skip existing file /tmp/x/IMG_0001.NEF\n",
            stderr="",
        )
        count, errors = PhotoTools()._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        assert count == 0
        assert errors == []


class TestPhotoToolsImportFromCamera:
    """Tests for PhotoTools.import_from_camera."""

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_one_camera_default_destination(
        self, mock_detect, mock_download, _mock_executor_cls, tmp_path, monkeypatch
    ):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (5, [])
        # Force HOME so the default destination lands inside tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))

        tools = PhotoTools()
        summary = tools.import_from_camera({})

        assert "Copied 5 file(s)" in summary
        assert "Nikon DSC D800E" in summary
        # Default destination must include today's date
        dest_arg = mock_download.call_args[0][2]
        assert str(tmp_path) in str(dest_arg)
        assert "import-" in dest_arg.name

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_no_cameras_raises(self, mock_detect, _mock_download, _mock_executor_cls):
        mock_detect.return_value = []
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="No camera detected"):
            tools.import_from_camera({})

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_multiple_cameras_without_port_raises(
        self, mock_detect, _mock_download, _mock_executor_cls
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"},
            {"model": "Canon EOS R5", "port": "usb:003,004"},
        ]
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="Multiple cameras"):
            tools.import_from_camera({})

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_multiple_cameras_with_port_selects(
        self, mock_detect, mock_download, _mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"},
            {"model": "Canon EOS R5", "port": "usb:003,004"},
        ]
        mock_download.return_value = (1, [])

        tools = PhotoTools()
        tools.import_from_camera({"camera_port": "usb:003,004", "destination": str(tmp_path)})

        # The selected camera's model should be passed to the download
        called_model = mock_download.call_args[0][0]
        called_port = mock_download.call_args[0][1]
        assert called_model == "Canon EOS R5"
        assert called_port == "usb:003,004"

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_invalid_port_raises(self, mock_detect, _mock_download, _mock_executor_cls):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="not found"):
            tools.import_from_camera({"camera_port": "usb:999,999"})

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_partial_copy_reports_warning(
        self, mock_detect, mock_download, _mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (3, ["ERROR: file X failed"])
        tools = PhotoTools()
        summary = tools.import_from_camera({"destination": str(tmp_path)})
        assert "Copied 3 file(s)" in summary
        assert "Warning" in summary
        assert "1 file" in summary

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_does_not_call_lua_executor(
        self, mock_detect, mock_download, mock_executor_cls, tmp_path
    ):
        # The pragmatic version copies files but doesn't try to register them
        # with darktable's library — that's left to the user via the GUI.
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (2, [])

        tools = PhotoTools()
        summary = tools.import_from_camera({"destination": str(tmp_path)})

        mock_executor_cls.return_value.execute_script.assert_not_called()
        # User must be told how to register the files in darktable
        assert "darktable" in summary.lower()
        assert "import folder" in summary.lower()

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_one_camera_with_matching_port(
        self, mock_detect, mock_download, _mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (4, [])

        tools = PhotoTools()
        summary = tools.import_from_camera(
            {"camera_port": "usb:002,002", "destination": str(tmp_path)}
        )

        assert "Copied 4 file(s)" in summary
        # Selection should use the matching camera even when only one exists
        assert mock_download.call_args[0][0] == "Nikon DSC D800E"
        assert mock_download.call_args[0][1] == "usb:002,002"

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_download_timeout_raises_clean_error(
        self, mock_detect, mock_download, _mock_executor_cls, tmp_path
    ):
        import subprocess

        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.side_effect = subprocess.TimeoutExpired(cmd=["gphoto2"], timeout=600)

        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="timed out"):
            tools.import_from_camera({"destination": str(tmp_path)})

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_total_download_failure_raises(
        self, mock_detect, mock_download, _mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (0, ["ERROR: device unreachable"])

        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="No files were transferred"):
            tools.import_from_camera({"destination": str(tmp_path)})

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_timeout_seconds_argument_flows_to_download(
        self, mock_detect, mock_download, _mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (1, [])

        tools = PhotoTools()
        tools.import_from_camera({"destination": str(tmp_path), "timeout_seconds": 120})

        # _download_from_camera receives timeout_seconds as the 4th positional arg
        assert mock_download.call_args[0][3] == 120

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_timeout_seconds_default_is_one_hour(
        self, mock_detect, mock_download, _mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]
        mock_download.return_value = (1, [])

        tools = PhotoTools()
        tools.import_from_camera({"destination": str(tmp_path)})

        assert mock_download.call_args[0][3] == 3600
