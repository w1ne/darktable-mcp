"""Photo tools for managing photos in darktable library."""

import json
import logging
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..darktable.lua_executor import LuaExecutor
from ..utils.errors import DarktableMCPError, DarktableNotFoundError

logger = logging.getLogger(__name__)


class PhotoTools:
    """High-level photo management operations using darktable Lua API."""

    def __init__(self) -> None:
        """Initialize PhotoTools with a LuaExecutor instance.

        Raises:
            DarktableMCPError: If darktable not properly configured
        """
        try:
            self.lua_executor = LuaExecutor()
        except DarktableNotFoundError as e:
            raise DarktableMCPError(
                f"darktable setup error: {e}. "
                "Please ensure darktable is installed and opened once."
            ) from e

    def _detect_cameras(self) -> List[Dict[str, str]]:
        """Run `gphoto2 --auto-detect` and return parsed list of cameras.

        Returns:
            List of dicts with keys "model" and "port", e.g.
            [{"model": "Nikon DSC D800E", "port": "usb:002,002"}].
            Empty list if no cameras detected.

        Raises:
            DarktableMCPError: if gphoto2 binary is not installed.
        """
        try:
            result = subprocess.run(
                ["gphoto2", "--auto-detect"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise DarktableMCPError(
                "gphoto2 not installed. Install with `apt install gphoto2` "
                "(Debian/Ubuntu) or your distro's package manager."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DarktableMCPError(
                "Camera detection timed out after 10 seconds. "
                "Is the camera busy or the USB connection unstable?"
            ) from exc

        cameras: List[Dict[str, str]] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            # Skip header ("Model ... Port") and separator ("----...")
            if not stripped or stripped.startswith("Model") or set(stripped) <= {"-"}:
                continue
            # gphoto2 separates model from port with 2+ spaces
            parts = re.split(r"\s{2,}", stripped, maxsplit=1)
            if len(parts) != 2:
                continue
            cameras.append({"model": parts[0].strip(), "port": parts[1].strip()})
        if not cameras and result.returncode != 0 and result.stderr.strip():
            raise DarktableMCPError(
                f"gphoto2 exited with code {result.returncode}: " f"{result.stderr.strip()[:200]}"
            )
        return cameras

    DOWNLOAD_TIMEOUT_DEFAULT = 3600

    def _download_from_camera(
        self,
        model: str,
        port: str,
        destination: Path,
        timeout_seconds: int = DOWNLOAD_TIMEOUT_DEFAULT,
    ) -> Tuple[int, List[str]]:
        """Run gphoto2 to copy all files from a camera to destination.

        Args:
            model: gphoto2 model string (e.g. "Nikon DSC D800E").
            port: gphoto2 port string (e.g. "usb:002,002").
            destination: directory to write files into. Created if missing.
            timeout_seconds: subprocess timeout. Default 3600 s (1 hour);
                a full 64 GB SD card over USB 3.0 fits comfortably.

        Returns:
            Tuple of (files_downloaded, error_messages). On partial failure
            (gphoto2 non-zero exit but some files saved), returns the count
            of saved files and any stderr lines as errors.

        Raises:
            DarktableMCPError: if gphoto2 binary is not installed, or if
                gphoto2 fails to lock the camera (typically because gvfs
                or another process holds it).
            subprocess.TimeoutExpired: if the transfer exceeds
                `timeout_seconds`. Caller is responsible for handling this;
                partial files may be present in `destination`.
        """
        destination.mkdir(parents=True, exist_ok=True)

        cmd = [
            "gphoto2",
            "--camera",
            model,
            "--port",
            port,
            "--get-all-files",
            "--skip-existing",
            "--filename",
            f"{destination}/%f",
        ]

        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
        except FileNotFoundError as exc:
            raise DarktableMCPError(
                "gphoto2 not installed. Install with `apt install gphoto2`."
            ) from exc

        # Count "Saving file as ..." lines on stdout
        count = sum(1 for line in result.stdout.splitlines() if "Saving file as" in line)
        skipped = sum(1 for line in result.stdout.splitlines() if "Skip existing" in line)

        errors: List[str] = []
        if result.returncode != 0:
            errors.extend(line for line in result.stderr.splitlines() if line.strip())

        # If nothing was saved or skipped and gphoto2 reports a lock error,
        # surface a clear "another process has the camera" message rather
        # than the raw "*** Error ***" stderr header.
        stderr_lower = result.stderr.lower() if result.stderr else ""
        if (
            count == 0
            and skipped == 0
            and result.returncode != 0
            and ("could not lock" in stderr_lower or "could not claim" in stderr_lower)
        ):
            raise DarktableMCPError(
                f"Could not access camera at {port}. Another process is "
                "holding it (typically gvfs / GNOME's volume monitor on "
                "Linux desktops). Disconnect and reconnect the camera, "
                "or stop gvfs-gphoto2-volume-monitor, then retry."
            )

        return count, errors

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

        script = """
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
        """

        result = self.lua_executor.execute_script(script, params=params, headless=True)
        try:
            photos = json.loads(result)
            if not isinstance(photos, list):
                raise DarktableMCPError("Expected list of photos from darktable")
            return photos
        except json.JSONDecodeError as exc:
            # Log the raw result for debugging
            logger.error("Failed to parse darktable response: %s", result)
            raise DarktableMCPError(
                "Failed to parse photo data from darktable. "
                "Please check that darktable is properly configured."
            ) from exc

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

        script = """
        local updated_count = 0

        for _, photo_id in ipairs(photo_ids) do
            local image = dt.database[tonumber(photo_id)]
            if image then
                image.rating = rating
                updated_count = updated_count + 1
            end
        end

        print("Updated " .. updated_count .. " photos with " .. rating .. " stars")
        """

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

        script = """
        local imported_files = dt.database.import(source_path, recursive)
        print("Imported " .. #imported_files .. " photos from " .. source_path)
        """

        return self.lua_executor.execute_script(script, params=params, headless=True)

    def import_from_camera(self, arguments: Dict[str, Any]) -> str:
        """Copy all photos from a connected camera to a local directory.

        Detects connected cameras via gphoto2 (libgphoto2 — same library
        darktable's GUI camera-import uses) and copies all files to a
        destination directory. Registering the directory with darktable's
        library is left to the user (open darktable, click "import folder")
        because the Lua API path for that step is not yet reliable on all
        installs.

        Args:
            arguments: Dictionary containing:
                - destination (str, optional): target directory.
                  Default: ~/Pictures/import-YYYY-MM-DD/
                - camera_port (str, optional): gphoto2 port string,
                  required when 2+ cameras are connected.
                - timeout_seconds (int, optional): subprocess timeout for
                  the transfer. Default 3600 (1 hour). Bump higher for
                  cards larger than ~50 GB.

        Returns:
            Human-readable summary string.

        Raises:
            DarktableMCPError: if no camera detected, multiple cameras
                without camera_port, invalid camera_port, gphoto2 missing,
                transfer timed out, or all files failed to copy.
        """
        camera_port = arguments.get("camera_port")
        destination_arg = arguments.get("destination")
        timeout_seconds = int(arguments.get("timeout_seconds", self.DOWNLOAD_TIMEOUT_DEFAULT))

        cameras = self._detect_cameras()

        if not cameras:
            raise DarktableMCPError("No camera detected. Is the camera connected and powered on?")

        if camera_port:
            matching = [c for c in cameras if c["port"] == camera_port]
            if not matching:
                ports = ", ".join(c["port"] for c in cameras)
                raise DarktableMCPError(
                    f"camera_port '{camera_port}' not found. " f"Detected ports: {ports}"
                )
            camera = matching[0]
        elif len(cameras) > 1:
            listing = ", ".join(f"{c['model']} ({c['port']})" for c in cameras)
            raise DarktableMCPError(
                f"Multiple cameras detected: {listing}. " "Pass camera_port=... to select one."
            )
        else:
            camera = cameras[0]

        if destination_arg:
            destination = Path(destination_arg).expanduser().resolve()
        else:
            today = date.today().isoformat()
            destination = (Path.home() / "Pictures" / f"import-{today}").resolve()

        try:
            count, errors = self._download_from_camera(
                camera["model"], camera["port"], destination, timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            raise DarktableMCPError(
                f"Camera transfer timed out after {timeout_seconds} s. "
                f"Destination {destination} may contain partial files. "
                "Re-run the tool to resume — `--skip-existing` is on, so "
                "already-copied files are not re-downloaded."
            ) from exc

        if count == 0 and errors:
            raise DarktableMCPError(
                f"No files were transferred from {camera['model']} "
                f"({camera['port']}). First error: {errors[0]}"
            )

        summary_parts = [
            f"Copied {count} file(s) from {camera['model']} ({camera['port']})",
            f"Destination: {destination}",
            "Open darktable and choose 'import folder' on this directory to add them to your library.",
        ]
        if errors:
            summary_parts.append(
                f"Warning: {len(errors)} file(s) failed to copy. First error: {errors[0]}"
            )
        return "\n".join(summary_parts)

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

        script = """
        local adjusted_count = 0

        -- Process each photo
        for _, photo_id in ipairs(photo_ids) do
            local image = dt.database[tonumber(photo_id)]
            if image then
                -- Apply exposure adjustment
                if image.modules then
                    if not image.modules.exposure then
                        image.modules.exposure = {exposure = 0}
                    end
                    image.modules.exposure.exposure = image.modules.exposure.exposure + exposure_ev
                    adjusted_count = adjusted_count + 1
                end
            end
        end

        print("Adjusted exposure for " .. adjusted_count .. " photos by " .. exposure_ev .. " EV")
        """

        # Use GUI mode since user needs to see the adjustments
        return self.lua_executor.execute_script(
            script,
            params=params,
            headless=False,
            gui_purpose="Show exposure adjustment preview",
        )
