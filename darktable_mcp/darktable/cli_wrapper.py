"""Command-line wrapper for darktable operations."""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from ..utils.errors import DarktableNotFoundError, ExportError

logger = logging.getLogger(__name__)


class CLIWrapper:
    """Wrapper for darktable command-line operations."""

    EXPORT_TIMEOUT_DEFAULT = 120

    def __init__(
        self,
        darktable_cli_path: Optional[str] = None,
        configdir: Optional[Path] = None,
    ):
        """Initialize the CLI wrapper.

        Args:
            darktable_cli_path: Path to darktable-cli executable
                (auto-detect if None)
            configdir: Dedicated darktable config directory for CLI runs.
                Defaults to `$XDG_CACHE_HOME/darktable-mcp/cli-config/`
                so darktable-cli does not share the GUI's library.db
                lock — exports work even when the user has darktable
                open. Created on first use.
        """
        self.darktable_cli_path = darktable_cli_path or self._find_darktable_cli()
        self.configdir = Path(configdir) if configdir else self._default_configdir()
        self.configdir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_configdir() -> Path:
        """Pick a per-user cache dir isolated from the GUI's `~/.config/darktable/`.

        Sharing the user's main config dir means darktable-cli waits for
        a lock that the running GUI holds and aborts with "database is
        locked". Use the XDG cache namespace instead so each MCP install
        gets its own throwaway library.db.
        """
        cache_home = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return Path(cache_home) / "darktable-mcp" / "cli-config"

    def _find_darktable_cli(self) -> str:
        """Find darktable-cli executable in system PATH.

        Returns:
            str: Path to darktable-cli executable

        Raises:
            DarktableNotFoundError: If darktable-cli is not found
        """
        darktable_cli_path = shutil.which("darktable-cli")
        if not darktable_cli_path:
            raise DarktableNotFoundError(
                "darktable-cli executable not found in PATH. " "Please install darktable."
            )

        return darktable_cli_path

    def check_darktable_available(self) -> str:
        """Check if darktable is available and return path.

        Returns:
            str: Path to darktable executable

        Raises:
            DarktableNotFoundError: If darktable is not found
        """
        darktable_path = shutil.which("darktable")
        if not darktable_path:
            raise DarktableNotFoundError(
                "darktable executable not found in PATH. " "Please install darktable."
            )

        return darktable_path

    def export_image(
        self,
        input_path: Path,
        output_path: Path,
        format_type: str = "jpeg",
        quality: int = 95,
        max_width: Optional[int] = None,
        max_height: Optional[int] = None,
        timeout: int = EXPORT_TIMEOUT_DEFAULT,
    ) -> bool:
        """Export an image using darktable-cli.

        Args:
            input_path: Path to input image
            output_path: Path for output image
            format_type: Export format (jpeg, png, tiff)
            quality: Export quality (1-100)
            max_width: Maximum width in pixels
            max_height: Maximum height in pixels
            timeout: subprocess timeout in seconds (default 120 s).

        Returns:
            bool: True if export successful

        Raises:
            ExportError: If export fails
        """
        try:
            cmd = [
                self.darktable_cli_path,
                str(input_path),
                str(output_path),
                "--core",
                "--configdir",
                str(self.configdir),
            ]

            fmt = format_type.lower()
            if fmt == "jpeg":
                cmd.extend(["--conf", f"plugins/imageio/format/jpeg/quality={quality}"])
            elif fmt == "png":
                cmd.extend(["--conf", "plugins/imageio/format/png/bpp=8"])
            elif fmt == "tiff":
                cmd.extend(["--conf", "plugins/imageio/format/tiff/bpp=8"])

            # Add size constraints if specified
            if max_width and max_height:
                cmd.extend(
                    [
                        "--conf",
                        f"plugins/imageio/format/jpeg/max_width={max_width}",
                        "--conf",
                        f"plugins/imageio/format/jpeg/max_height={max_height}",
                    ]
                )

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

            if result.returncode != 0:
                error_msg = result.stderr or "Unknown error"
                raise ExportError(f"Export failed: {error_msg}")

            return True

        except subprocess.TimeoutExpired:
            raise ExportError("Export operation timed out")
        except ExportError:
            raise
        except Exception as e:
            raise ExportError(f"Failed to export image: {str(e)}")

    def batch_export(
        self,
        input_files: List[Path],
        output_dir: Path,
        format_type: str = "jpeg",
        quality: int = 95,
    ) -> Dict[str, str]:
        """Export multiple images in batch.

        Args:
            input_files: List of input file paths
            output_dir: Output directory
            format_type: Export format
            quality: Export quality

        Returns:
            Dict[str, str]: Mapping of input files to status messages
        """
        results = {}
        output_dir.mkdir(parents=True, exist_ok=True)

        for input_file in input_files:
            try:
                output_file = output_dir / f"{input_file.stem}.{format_type.lower()}"

                success = self.export_image(input_file, output_file, format_type, quality)

                if success:
                    results[str(input_file)] = f"Exported to {output_file}"
                else:
                    results[str(input_file)] = "Export failed"

            except Exception as e:
                results[str(input_file)] = f"Error: {str(e)}"
                logger.error(f"Failed to export {input_file}: {e}")

        return results

    def get_version(self) -> str:
        """Get darktable version information.

        Returns:
            str: Version information
        """
        try:
            result = subprocess.run(
                [self.darktable_cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                return result.stdout.strip()
            else:
                return "Version information unavailable"

        except Exception as e:
            logger.error(f"Failed to get version: {e}")
            return "Version check failed"
