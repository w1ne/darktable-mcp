"""Command-line wrapper for darktable operations."""

import itertools
import logging
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..utils.errors import DarktableNotFoundError, ExportError

logger = logging.getLogger(__name__)

_SAFE_SUFFIX_RE = re.compile(r"[^A-Za-z0-9_-]+")

# darktable-cli picks the output extension from the format itself and
# ignores the one we ask for: request `out.jpeg` and it writes `out.jpg`,
# request `out.tiff` and it writes `out.tif`. Naming the output with the
# wrong extension makes the post-export existence check fail on a file
# that actually exported fine, so plan the name darktable-cli will use.
# Verified against darktable 5.6.0.
FORMAT_EXTENSIONS = {
    "jpeg": "jpg",
    "jpg": "jpg",
    "png": "png",
    "tiff": "tif",
    "tif": "tif",
}


def _output_extension(format_type: str) -> str:
    """Return the file extension darktable-cli actually writes for a format."""
    fmt = format_type.lower()
    return FORMAT_EXTENSIONS.get(fmt, fmt)


@dataclass
class ExportResult:
    """Outcome of exporting one file."""

    input: str
    output: Optional[str]
    ok: bool
    error: Optional[str]


class CLIWrapper:
    """Wrapper for darktable command-line operations.

    Sidecar caveat: exports run against a dedicated `configdir` that is
    deliberately isolated from the GUI's `~/.config/darktable/` (see
    `_default_configdir`), so darktable-cli never contends for the
    library.db lock the running GUI holds. The trade-off is that the CLI
    cannot see the GUI's library database, so it reads edits **only from
    XMP sidecar files** next to each raw. A user who has darktable's
    "write sidecar file for each image" preference turned off gets
    exports of the *unedited* image with no warning from darktable-cli.
    Callers should surface this when exports look wrong.
    """

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
        # Concurrent darktable-cli processes must not share a configdir: they
        # contend for the same library.db and one of them silently produces no
        # output file at all (observed on darktable 5.6.0 — two parallel
        # exports, only one file written, both exiting non-zero with nothing
        # but a "notice:" on stderr). Hand every worker thread its own
        # sub-configdir instead. Threads are reused across the batch, so this
        # costs one library.db per worker, not per file.
        self._thread_state = threading.local()
        self._slot_counter = itertools.count()

    def _worker_configdir(self) -> Path:
        """Return a configdir private to the calling thread.

        The main thread keeps `self.configdir` itself, so single-threaded
        callers and existing behaviour are unchanged; pool workers get
        `<configdir>/worker-N/`.
        """
        slot = getattr(self._thread_state, "slot", None)
        if slot is None:
            slot = next(self._slot_counter)
            self._thread_state.slot = slot
        if slot == 0:
            return self.configdir
        worker_dir = self.configdir / f"worker-{slot}"
        worker_dir.mkdir(parents=True, exist_ok=True)
        return worker_dir

    @staticmethod
    def _default_configdir() -> Path:
        """Pick a per-user cache dir isolated from the GUI's `~/.config/darktable/`.

        Sharing the user's main config dir means darktable-cli waits for
        a lock that the running GUI holds and aborts with "database is
        locked". Use the XDG cache namespace instead so each MCP install
        gets its own throwaway library.db.

        Consequence: the throwaway library holds no edit history, so
        exports pick up develop settings from XMP sidecars only. See the
        class docstring.
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

        A zero exit code from darktable-cli is not proof of an export:
        unsupported inputs make it exit 0 while writing nothing. The
        output path is stat'ed afterwards and a missing or empty file is
        reported as a failure.

        Args:
            input_path: Path to input image
            output_path: Path for output image
            format_type: Export format (jpeg, png, tiff)
            quality: Export quality (1-100)
            max_width: Maximum width in pixels, or None for unconstrained.
                Passed to darktable-cli's `--width` flag; may be given
                independently of `max_height`.
            max_height: Maximum height in pixels, or None for unconstrained.
                Passed to darktable-cli's `--height` flag.
            timeout: subprocess timeout in seconds (default 120 s).

        Returns:
            bool: True if export successful

        Raises:
            ExportError: If export fails, times out, or produces no file
        """
        try:
            cmd = [
                self.darktable_cli_path,
                str(input_path),
                str(output_path),
            ]

            # Size constraints: darktable-cli takes both bounds as first-class
            # flags and reads 0 as "unconstrained on this axis", so either bound
            # can be given on its own. The format-specific max_width/max_height
            # conf keys are jpeg-only and were a no-op for png/tiff. These are
            # darktable-cli's own options, so they must precede `--core`.
            if max_width is not None or max_height is not None:
                cmd.extend(["--width", str(max_width or 0)])
                cmd.extend(["--height", str(max_height or 0)])

            # Everything after `--core` is handed to the darktable core.
            cmd.extend(["--core", "--configdir", str(self._worker_configdir())])

            fmt = format_type.lower()
            if fmt == "jpeg":
                cmd.extend(["--conf", f"plugins/imageio/format/jpeg/quality={quality}"])
            elif fmt == "png":
                cmd.extend(["--conf", "plugins/imageio/format/png/bpp=8"])
            elif fmt == "tiff":
                cmd.extend(["--conf", "plugins/imageio/format/tiff/bpp=8"])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

            if result.returncode != 0:
                error_msg = result.stderr or "Unknown error"
                raise ExportError(f"Export failed: {error_msg}")

            self._assert_output_written(output_path)

            return True

        except subprocess.TimeoutExpired:
            raise ExportError("Export operation timed out")
        except ExportError:
            raise
        except Exception as e:
            raise ExportError(f"Failed to export image: {str(e)}")

    @staticmethod
    def _assert_output_written(output_path: Path) -> None:
        """Confirm darktable-cli actually produced the promised file.

        Args:
            output_path: Path darktable-cli was told to write

        Raises:
            ExportError: If the path is missing or zero-byte
        """
        try:
            size = output_path.stat().st_size
        except OSError:
            raise ExportError(
                f"Export reported success but wrote no file at {output_path} "
                f"(darktable-cli can exit 0 on an unsupported input)"
            )

        if size == 0:
            raise ExportError(
                f"Export reported success but wrote a zero-byte file at {output_path}"
            )

    @staticmethod
    def _plan_output_paths(
        input_files: List[Path],
        output_dir: Path,
        format_type: str,
    ) -> List[Path]:
        """Assign one distinct output path per input, in input order.

        Two inputs from different source folders can share a stem
        (`DSC_0001.NEF`), which used to make the second export silently
        overwrite the first. Colliding names get a suffix derived from the
        source directory, falling back to a `-2`, `-3` counter.

        Args:
            input_files: Input paths, in the caller's order
            output_dir: Directory every output lands in
            format_type: Export format; the extension is the one
                darktable-cli actually writes for it, not the format name

        Returns:
            List[Path]: One output path per input, all distinct
        """
        ext = _output_extension(format_type)
        taken = set()
        planned: List[Path] = []

        for input_file in input_files:
            stem = input_file.stem
            candidate = output_dir / f"{stem}.{ext}"

            if candidate in taken:
                parent = _SAFE_SUFFIX_RE.sub("-", input_file.parent.name).strip("-")[:32]
                if parent:
                    candidate = output_dir / f"{stem}-{parent}.{ext}"

                counter = 2
                while candidate in taken:
                    candidate = output_dir / f"{stem}-{counter}.{ext}"
                    counter += 1

            taken.add(candidate)
            planned.append(candidate)

        return planned

    def batch_export(
        self,
        input_files: List[Path],
        output_dir: Path,
        format_type: str = "jpeg",
        quality: int = 95,
        max_width: Optional[int] = None,
        max_height: Optional[int] = None,
        max_workers: Optional[int] = None,
        timeout: int = EXPORT_TIMEOUT_DEFAULT,
    ) -> List[ExportResult]:
        """Export multiple images in batch, in parallel.

        Each export boots a full darktable core, so the work is
        subprocess-bound and runs on a small thread pool. Output paths are
        de-duplicated *before* dispatch so concurrent workers cannot race
        for the same name.

        Sidecar caveat: exports read develop settings from XMP sidecars
        only, never from the GUI's library database (see the class
        docstring). If the user has darktable's "write sidecar file for
        each image" preference turned off, every file here exports
        without its edits and darktable-cli reports success. Surface this
        to the user when a batch looks unedited.

        Args:
            input_files: List of input file paths
            output_dir: Output directory
            format_type: Export format
            quality: Export quality
            max_width: Longest-edge bound in pixels, or None for unconstrained.
            max_height: Height bound in pixels, or None for unconstrained.
            max_workers: Thread pool size. Defaults to
                `min(4, os.cpu_count() or 1)`.
            timeout: Per-file subprocess timeout in seconds.

        Returns:
            List[ExportResult]: One result per input, in input order.
        """
        if not input_files:
            return []

        output_dir.mkdir(parents=True, exist_ok=True)
        planned = self._plan_output_paths(input_files, output_dir, format_type)

        workers = max_workers if max_workers and max_workers > 0 else min(4, os.cpu_count() or 1)
        results: List[Optional[ExportResult]] = [None] * len(input_files)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._export_one,
                    input_file,
                    output_file,
                    format_type,
                    quality,
                    max_width=max_width,
                    max_height=max_height,
                    timeout=timeout,
                ): index
                for index, (input_file, output_file) in enumerate(zip(input_files, planned))
            }

            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()

        return [result for result in results if result is not None]

    def _export_one(
        self,
        input_file: Path,
        output_file: Path,
        format_type: str,
        quality: int,
        max_width: Optional[int] = None,
        max_height: Optional[int] = None,
        timeout: int = EXPORT_TIMEOUT_DEFAULT,
    ) -> ExportResult:
        """Export a single file, converting any failure into an ExportResult.

        Args:
            input_file: Path to the source image
            output_file: Pre-assigned, collision-free destination path
            format_type: Export format
            quality: Export quality
            max_width: Longest-edge bound in pixels, or None for unconstrained.
            max_height: Height bound in pixels, or None for unconstrained.
            timeout: subprocess timeout in seconds

        Returns:
            ExportResult: Never raises; failures land in `error`.
        """
        try:
            self.export_image(
                input_file,
                output_file,
                format_type,
                quality,
                max_width=max_width,
                max_height=max_height,
                timeout=timeout,
            )
        except Exception as e:
            logger.error("Failed to export %s: %s", input_file, e)
            return ExportResult(
                input=str(input_file),
                output=str(output_file) if output_file.exists() else None,
                ok=False,
                error=str(e),
            )

        return ExportResult(
            input=str(input_file),
            output=str(output_file) if output_file.exists() else None,
            ok=True,
            error=None,
        )

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
