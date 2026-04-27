"""Photo tools for managing photos in darktable library."""

import logging
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..utils.errors import DarktableMCPError

logger = logging.getLogger(__name__)


class PhotoTools:
    """High-level photo management operations using darktable Lua API."""

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
