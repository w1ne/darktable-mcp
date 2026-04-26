"""Tests for PhotoTools module."""

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
