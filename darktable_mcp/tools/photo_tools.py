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

        script = f'''
        photos = {{}}
        count = 0
        for _, image in ipairs(dt.database) do
            if count >= {limit} then break end

            local include = true
            if {rating_min or "nil"} and image.rating < {rating_min or 0} then
                include = false
            end

            if include and "{filter_text}" ~= "" then
                local filename_match = string.find(string.lower(image.filename), string.lower("{filter_text}"))
                if not filename_match then
                    include = false
                end
            end

            if include then
                table.insert(photos, {{
                    id = tostring(image.id),
                    filename = image.filename,
                    path = image.path,
                    rating = image.rating or 0
                }})
                count = count + 1
            end
        end

        print(dt.json.encode(photos))
        '''

        result = self.lua_executor.execute_script(script, headless=True)
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

        # Convert photo_ids list to Lua table syntax
        lua_ids = "{" + ", ".join(f'"{pid}"' for pid in photo_ids) + "}"

        script = f'''
        local photo_ids = {lua_ids}
        local rating = {rating}
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

        return self.lua_executor.execute_script(script, headless=True)

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

        script = f'''
        local source_path = "{source_path}"
        local recursive = {str(recursive).lower()}

        local imported_files = dt.database.import(source_path, recursive)
        print("Imported " .. #imported_files .. " photos from " .. source_path)
        '''

        return self.lua_executor.execute_script(script, headless=True)
