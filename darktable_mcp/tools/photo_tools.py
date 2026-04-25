"""Photo tools for managing photos in darktable library."""

import json
from typing import Dict, Any, List, Optional

from ..darktable.lua_executor import LuaExecutor
from ..utils.errors import DarktableMCPError


class PhotoTools:
    """Provides high-level photo management operations using darktable Lua API."""

    def __init__(self):
        """Initialize PhotoTools with a LuaExecutor instance."""
        self.lua_executor = LuaExecutor()

    def view_photos(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """View photos from darktable library with optional filtering.

        Args:
            arguments: Dictionary containing:
                - filter (str): Filter photos by filename (case-insensitive)
                - rating_min (int, optional): Minimum rating (1-5)
                - limit (int): Maximum number of photos to return (default: 100)

        Returns:
            List of photo dictionaries with id, filename, path, and rating

        Raises:
            DarktableMCPError: If JSON parsing fails
        """
        filter_text = arguments.get("filter", "")
        rating_min = arguments.get("rating_min")
        limit = arguments.get("limit", 100)

        # Parameters passed safely to Lua script
        params = {
            "filter_text": filter_text,
            "limit": limit,
        }
        if rating_min is not None:
            params["rating_min"] = rating_min

        script = '''
        photos = {}
        count = 0
        for _, image in ipairs(dt.database) do
            if count >= limit then break end

            local include = true
            if rating_min and image.rating < rating_min then
                include = false
            end

            if include and filter_text ~= "" then
                local filename_match = string.find(string.lower(image.filename), string.lower(filter_text))
                if not filename_match then
                    include = false
                end
            end

            if include then
                table.insert(photos, {
                    id = tostring(image.id),
                    filename = image.filename,
                    path = image.path,
                    rating = image.rating or 0
                })
                count = count + 1
            end
        end

        print(dt.json.encode(photos))
        '''

        result = self.lua_executor.execute_script(script, params=params, headless=True)
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            raise DarktableMCPError(f"Failed to parse photo data: {result}")

    def rate_photos(self, arguments: Dict[str, Any]) -> str:
        """Rate photos in darktable library.

        Args:
            arguments: Dictionary containing:
                - photo_ids (list): List of photo IDs to rate
                - rating (int): Rating value (1-5)

        Returns:
            str: Status message with number of photos updated

        Raises:
            DarktableMCPError: If photo_ids missing, empty, or rating invalid
        """
        photo_ids = arguments.get("photo_ids", [])
        rating = arguments.get("rating", 0)

        if not photo_ids:
            raise DarktableMCPError("photo_ids is required")

        if not 1 <= rating <= 5:
            raise DarktableMCPError("rating must be between 1 and 5")

        # Parameters passed safely to Lua script
        params = {
            "photo_ids": photo_ids,
            "rating": rating,
        }

        script = '''
        local updated_count = 0

        for _, photo_id in ipairs(photo_ids) do
            local image = dt.database[tonumber(photo_id)]
            if image then
                image.rating = rating
                updated_count = updated_count + 1
            end
        end

        print("Updated " .. updated_count .. " photos with " .. rating .. " stars")
        '''

        return self.lua_executor.execute_script(script, params=params, headless=True)

    def import_batch(self, arguments: Dict[str, Any]) -> str:
        """Import photos in batch from a source directory.

        Args:
            arguments: Dictionary containing:
                - source_path (str): Path to directory with photos
                - recursive (bool): Whether to import recursively (default: False)

        Returns:
            str: Status message with number of photos imported

        Raises:
            DarktableMCPError: If source_path is missing
        """
        source_path = arguments.get("source_path")
        recursive = arguments.get("recursive", False)

        if not source_path:
            raise DarktableMCPError("source_path is required")

        # Parameters passed safely to Lua script
        params = {
            "source_path": source_path,
            "recursive": recursive,
        }

        script = '''
        local imported_files = dt.database.import(source_path, recursive)
        print("Imported " .. #imported_files .. " photos from " .. source_path)
        '''

        return self.lua_executor.execute_script(script, params=params, headless=True)

    def adjust_exposure(self, arguments: Dict[str, Any]) -> str:
        """Adjust exposure for photos (requires GUI for preview).

        Args:
            arguments: Dictionary containing:
                - photo_ids (list): List of photo IDs to adjust
                - exposure_ev (float): Exposure adjustment in EV (-5.0 to 5.0)

        Returns:
            str: Status message with number of photos adjusted

        Raises:
            DarktableMCPError: If photo_ids missing, empty, or exposure_ev invalid
        """
        photo_ids = arguments.get("photo_ids", [])
        exposure_ev = arguments.get("exposure_ev", 0.0)

        if not photo_ids:
            raise DarktableMCPError("photo_ids is required")

        if not -5.0 <= exposure_ev <= 5.0:
            raise DarktableMCPError("exposure_ev must be between -5.0 and 5.0")

        # Parameters passed safely to Lua script
        params = {
            "photo_ids": photo_ids,
            "exposure_ev": exposure_ev,
        }

        script = '''
        local adjusted_count = 0

        -- Process each photo
        for _, photo_id in ipairs(photo_ids) do
            local image = dt.database[tonumber(photo_id)]
            if image then
                -- Apply exposure adjustment
                if image.modules and image.modules.exposure then
                    image.modules.exposure.exposure = image.modules.exposure.exposure + exposure_ev
                    adjusted_count = adjusted_count + 1
                end
            end
        end

        print("Adjusted exposure for " .. adjusted_count .. " photos by " .. exposure_ev .. " EV")
        '''

        # Use GUI mode since user needs to see the adjustments
        return self.lua_executor.execute_script(
            script,
            params=params,
            headless=False,
            gui_purpose="Show exposure adjustment preview"
        )
