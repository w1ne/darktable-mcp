"""Integration tests for darktable MCP server."""

import json
import logging
from unittest.mock import patch

import pytest

from darktable_mcp.server import DarktableMCPServer
from darktable_mcp.tools.photo_tools import PhotoTools
from darktable_mcp.utils.errors import DarktableMCPError, DarktableNotFoundError

logger = logging.getLogger(__name__)


class TestIntegrationFullWorkflow:
    """Test end-to-end workflows combining multiple tools."""

    @pytest.mark.asyncio
    async def test_full_workflow_integration(self):
        """Test complete workflow: view -> rate -> export"""
        server = DarktableMCPServer()

        # Mock darktable responses
        view_response = json.dumps(
            [
                {"id": "123", "filename": "photo1.jpg", "path": "/photos/photo1.jpg", "rating": 0},
                {"id": "124", "filename": "photo2.jpg", "path": "/photos/photo2.jpg", "rating": 2},
            ]
        )
        rate_response = "Updated 2 photos with 4 stars"

        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            # Mock headless lua execution for view and rate
            mock_executor.execute_script.side_effect = [
                view_response,  # view_photos
                rate_response,  # rate_photos
            ]

            server._photo_tools = PhotoTools()
            server._photo_tools.lua_executor = mock_executor

            # Test view photos
            view_result = await server._handle_view_photos({"limit": 10})
            assert len(view_result) == 1
            assert "photo1.jpg" in view_result[0].text
            assert "photo2.jpg" in view_result[0].text

            # Test rate photos
            rate_result = await server._handle_rate_photos(
                {"photo_ids": ["123", "124"], "rating": 4}
            )
            assert len(rate_result) == 1
            assert "Updated 2 photos" in rate_result[0].text

    @pytest.mark.asyncio
    async def test_workflow_with_error_handling(self):
        """Test workflow handles errors gracefully."""
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "invalid json that won't parse"

            server._photo_tools = PhotoTools()
            server._photo_tools.lua_executor = mock_executor

            # view_photos should fail with JSON parsing error
            result = await server._handle_view_photos({"limit": 10})
            assert len(result) == 1
            assert "Error:" in result[0].text or "Failed to parse" in result[0].text


class TestPhotoToolsErrorHandling:
    """Test error handling in PhotoTools."""

    def test_missing_darktable_error_handling(self):
        """Test PhotoTools handles missing darktable gracefully."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            MockExecutor.side_effect = DarktableNotFoundError("darktable not found")

            with pytest.raises(DarktableMCPError) as exc_info:
                PhotoTools()

            assert "darktable setup error" in str(exc_info.value)

    def test_malformed_response_handling(self):
        """Test PhotoTools handles malformed JSON responses."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "invalid json"

            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.view_photos({"limit": 10})

            assert "Failed to parse photo data" in str(exc_info.value)

    def test_malformed_response_not_list(self):
        """Test PhotoTools handles non-list JSON responses."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            # Return a dict instead of a list
            mock_executor.execute_script.return_value = '{"id": "123", "filename": "test.jpg"}'

            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.view_photos({"limit": 10})

            assert "Expected list of photos" in str(exc_info.value)

    def test_library_not_found_error(self):
        """Test PhotoTools handles library not found error."""
        with patch("darktable_mcp.darktable.library_detector.LibraryDetector") as MockDetector:
            mock_detector = MockDetector.return_value
            mock_detector.find_library.side_effect = DarktableNotFoundError(
                "Please make sure darktable is installed and you've imported some photos first"
            )

            with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
                mock_executor = MockExecutor.return_value
                # Simulate the library detector being called in _execute_headless
                mock_executor.execute_script.side_effect = DarktableNotFoundError(
                    "Please make sure darktable is installed and you've imported some photos first"
                )

                tools = PhotoTools()
                with pytest.raises(Exception):  # Will be DarktableNotFoundError or similar
                    tools.view_photos({"limit": 10})

    def test_view_photos_json_validation(self):
        """Test view_photos properly validates and parses JSON."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            # Valid JSON response
            valid_response = json.dumps(
                [{"id": "1", "filename": "test.jpg", "path": "/test", "rating": 3}]
            )
            mock_executor.execute_script.return_value = valid_response

            tools = PhotoTools()
            result = tools.view_photos({"limit": 10})

            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["filename"] == "test.jpg"

    def test_malformed_json_error_message(self):
        """Test error message for malformed JSON is helpful."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "not valid json {"

            tools = PhotoTools()
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.view_photos({"limit": 10})

            error_msg = str(exc_info.value)
            assert "Failed to parse photo data" in error_msg


class TestPhotoToolsValidation:
    """Test input validation in PhotoTools."""

    def test_rate_photos_validation_required_fields(self):
        """Test rate_photos validates required fields."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor"):
            tools = PhotoTools()

            with pytest.raises(DarktableMCPError) as exc_info:
                tools.rate_photos({})  # Missing both photo_ids and rating

            assert "photo_ids is required" in str(exc_info.value)

    def test_rate_photos_validation_rating_bounds(self):
        """Test rate_photos validates rating bounds."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor"):
            tools = PhotoTools()

            # Test too low
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.rate_photos({"photo_ids": ["123"], "rating": 0})
            assert "rating must be between 1 and 5" in str(exc_info.value)

            # Test too high
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.rate_photos({"photo_ids": ["123"], "rating": 6})
            assert "rating must be between 1 and 5" in str(exc_info.value)

    def test_import_batch_validation_required_fields(self):
        """Test import_batch validates required fields."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor"):
            tools = PhotoTools()

            with pytest.raises(DarktableMCPError) as exc_info:
                tools.import_batch({})  # Missing source_path

            assert "source_path is required" in str(exc_info.value)

    def test_adjust_exposure_validation_bounds(self):
        """Test adjust_exposure validates exposure bounds."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor"):
            tools = PhotoTools()

            # Test too high
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.adjust_exposure({"photo_ids": ["123"], "exposure_ev": 5.1})
            assert "exposure_ev must be between" in str(exc_info.value)

            # Test too low
            with pytest.raises(DarktableMCPError) as exc_info:
                tools.adjust_exposure({"photo_ids": ["123"], "exposure_ev": -5.1})
            assert "exposure_ev must be between" in str(exc_info.value)


class TestServerErrorHandling:
    """Test error handling in the MCP server."""

    @pytest.mark.asyncio
    async def test_server_handles_tool_exceptions(self):
        """Test server gracefully handles tool exceptions."""
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.PhotoTools") as MockPhotoTools:
            mock_tools = MockPhotoTools.return_value
            mock_tools.view_photos.side_effect = DarktableMCPError("Failed to connect to darktable")

            server._photo_tools = mock_tools

            result = await server._handle_view_photos({"limit": 10})

            assert len(result) == 1
            assert "Error:" in result[0].text
            assert "Failed to connect" in result[0].text

    @pytest.mark.asyncio
    async def test_server_handles_unexpected_exceptions(self):
        """Test server handles unexpected exceptions."""
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.PhotoTools") as MockPhotoTools:
            mock_tools = MockPhotoTools.return_value
            mock_tools.view_photos.side_effect = RuntimeError("Unexpected internal error")

            server._photo_tools = mock_tools

            result = await server._handle_view_photos({"limit": 10})

            assert len(result) == 1
            assert "Error:" in result[0].text

    @pytest.mark.asyncio
    async def test_server_handles_rate_photos_errors(self):
        """Test server handles rate_photos errors."""
        server = DarktableMCPServer()

        with patch("darktable_mcp.tools.photo_tools.PhotoTools") as MockPhotoTools:
            mock_tools = MockPhotoTools.return_value
            mock_tools.rate_photos.side_effect = DarktableMCPError("rating must be between 1 and 5")

            server._photo_tools = mock_tools

            result = await server._handle_rate_photos({"photo_ids": ["123"], "rating": 10})

            assert len(result) == 1
            assert "Error:" in result[0].text


class TestIntegrationEdgeCases:
    """Test edge cases in integration scenarios."""

    def test_view_photos_empty_result(self):
        """Test view_photos handles empty results."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "[]"

            tools = PhotoTools()
            result = tools.view_photos({"limit": 10})

            assert isinstance(result, list)
            assert len(result) == 0

    def test_rate_photos_empty_photo_ids(self):
        """Test rate_photos rejects empty photo_ids list."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor"):
            tools = PhotoTools()

            with pytest.raises(DarktableMCPError) as exc_info:
                tools.rate_photos({"photo_ids": [], "rating": 4})

            assert "photo_ids is required" in str(exc_info.value)

    def test_view_photos_with_all_filters(self):
        """Test view_photos with all filter options."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = json.dumps(
                [{"id": "1", "filename": "vacation_2024.jpg", "path": "/photos", "rating": 5}]
            )

            tools = PhotoTools()
            result = tools.view_photos({"filter": "vacation", "rating_min": 4, "limit": 50})

            assert len(result) == 1
            assert result[0]["rating"] == 5

    def test_adjust_exposure_boundary_values(self):
        """Test adjust_exposure with boundary values."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Adjusted exposure for 1 photos by 5.0 EV"

            tools = PhotoTools()

            # Max value
            result = tools.adjust_exposure({"photo_ids": ["123"], "exposure_ev": 5.0})
            assert "Adjusted exposure" in result

            # Min value
            mock_executor.execute_script.return_value = "Adjusted exposure for 1 photos by -5.0 EV"
            result = tools.adjust_exposure({"photo_ids": ["123"], "exposure_ev": -5.0})
            assert "Adjusted exposure" in result

    def test_import_batch_with_recursive_flag(self):
        """Test import_batch respects recursive flag."""
        with patch("darktable_mcp.tools.photo_tools.LuaExecutor") as MockExecutor:
            mock_executor = MockExecutor.return_value
            mock_executor.execute_script.return_value = "Imported 10 photos from /photos"

            tools = PhotoTools()

            # Test with recursive=True
            result = tools.import_batch({"source_path": "/photos", "recursive": True})
            assert "Imported 10 photos" in result

            # Test with recursive=False
            mock_executor.execute_script.return_value = "Imported 5 photos from /photos"
            result = tools.import_batch({"source_path": "/photos", "recursive": False})
            assert "Imported 5 photos" in result
