"""Camera-import tooling using libgphoto2."""

import os
import re
import shutil
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

from ..utils.errors import DarktableMCPError


_MSC_PORT_PREFIX = "disk:"
_MODEL_WORD_RE = re.compile(r"[A-Za-z0-9]{4,}")


class CameraTools:
    """Camera import via gphoto2 (libgphoto2 — same library darktable's GUI uses)."""

    DOWNLOAD_TIMEOUT_DEFAULT = 3600
    LIST_FOLDERS_TIMEOUT = 30
    NUM_FILES_TIMEOUT = 30
    PROGRESS_LOG_NAME = ".import.log"

    _FOLDER_LINE_RE = re.compile(r"There (?:is|are) (\d+) folders? in folder '([^']+)'\.")
    _NUM_FILES_RE = re.compile(r":\s*(\d+)\s*$", re.MULTILINE)

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

    def _list_image_folders(self, model: str, port: str) -> List[str]:
        """Enumerate leaf folders (no subfolders) on the camera.

        Multi-storage cameras (dual CF/SD bodies, etc.) expose each card as
        a separate `/store_*` root, so a single `--get-all-files` from `/`
        is unreliable. Walking the tree and copying per-leaf is robust.

        Returns:
            Sorted list of leaf folder paths. If parsing yields nothing
            (older gphoto2, locale issues, unusual layouts), returns
            `["/"]` so callers fall back to a single recursive download.
        """
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        try:
            result = subprocess.run(
                [
                    "gphoto2",
                    "--camera",
                    model,
                    "--port",
                    port,
                    "--list-folders",
                ],
                capture_output=True,
                text=True,
                timeout=self.LIST_FOLDERS_TIMEOUT,
                env=env,
            )
        except FileNotFoundError as exc:
            raise DarktableMCPError(
                "gphoto2 not installed. Install with `apt install gphoto2`."
            ) from exc
        except subprocess.TimeoutExpired:
            return ["/"]

        if result.returncode != 0:
            return ["/"]

        leaves: List[str] = []
        for line in result.stdout.splitlines():
            match = self._FOLDER_LINE_RE.search(line)
            if not match:
                continue
            count = int(match.group(1))
            path = match.group(2)
            if count == 0:
                leaves.append(path)

        if not leaves:
            return ["/"]
        return sorted(leaves)

    def _count_files_in_folder(
        self, model: str, port: str, src_folder: str
    ) -> Optional[int]:
        """Count files in a single camera folder via `--num-files`.

        Returns None on any failure (gphoto2 missing, timeout, parse error).
        Used purely to surface "X of Y" progress; absence is non-fatal.
        """
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        try:
            result = subprocess.run(
                [
                    "gphoto2",
                    "--camera",
                    model,
                    "--port",
                    port,
                    "--folder",
                    src_folder,
                    "--num-files",
                ],
                capture_output=True,
                text=True,
                timeout=self.NUM_FILES_TIMEOUT,
                env=env,
            )
        except FileNotFoundError as exc:
            raise DarktableMCPError(
                "gphoto2 not installed. Install with `apt install gphoto2`."
            ) from exc
        except subprocess.TimeoutExpired:
            return None

        if result.returncode != 0:
            return None

        match = self._NUM_FILES_RE.search(result.stdout)
        return int(match.group(1)) if match else None

    def _download_one_folder(
        self,
        model: str,
        port: str,
        src_folder: str,
        destination: Path,
        timeout_seconds: int,
        progress_log: Optional[TextIO] = None,
        expected_total: Optional[int] = None,
    ) -> Tuple[int, List[str]]:
        """Run gphoto2 to copy all files in a single camera folder.

        Streams stdout via Popen + reader threads so per-file progress is
        written to `progress_log` as the transfer happens (the user can
        `tail -f` the log file in another terminal). `--filename %f.%C`
        preserves the file extension. `--skip-existing` makes resumes
        cheap and idempotent.

        Args:
            progress_log: open text file handle to receive timestamped
                "Saving file as ..." lines. None to disable logging.
            expected_total: if known, formats progress as "(N/total)".

        Returns:
            Tuple of (files_saved, error_lines).

        Raises:
            DarktableMCPError: gphoto2 missing or camera locked by another
                process (gvfs etc.).
            subprocess.TimeoutExpired: caller handles partial transfers.
        """
        cmd = [
            "gphoto2",
            "--camera",
            model,
            "--port",
            port,
            "--folder",
            src_folder,
            "--get-all-files",
            "--skip-existing",
            "--filename",
            f"{destination}/%f.%C",
        ]

        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise DarktableMCPError(
                "gphoto2 not installed. Install with `apt install gphoto2`."
            ) from exc

        counters = {"saved": 0, "skipped": 0}
        stderr_buf: List[str] = []

        def _consume_stdout() -> None:
            stream = proc.stdout
            if stream is None:
                return
            try:
                for line in stream:
                    if "Saving file as" in line:
                        counters["saved"] += 1
                        if progress_log is not None:
                            ts = datetime.now().strftime("%H:%M:%S")
                            if expected_total:
                                prefix = f"[{ts}] ({counters['saved']}/{expected_total}) "
                            else:
                                prefix = f"[{ts}] ({counters['saved']}) "
                            progress_log.write(prefix + line.rstrip("\n") + "\n")
                            progress_log.flush()
                    elif "Skip existing" in line:
                        counters["skipped"] += 1
                        if progress_log is not None:
                            progress_log.write(line if line.endswith("\n") else line + "\n")
                            progress_log.flush()
            except Exception:  # pragma: no cover - reader is best-effort
                pass

        def _consume_stderr() -> None:
            stream = proc.stderr
            if stream is None:
                return
            try:
                for line in stream:
                    stderr_buf.append(line)
            except Exception:  # pragma: no cover - reader is best-effort
                pass

        t_out = threading.Thread(target=_consume_stdout, daemon=True)
        t_err = threading.Thread(target=_consume_stderr, daemon=True)
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            raise

        t_out.join(timeout=5)
        t_err.join(timeout=5)

        saved = counters["saved"]
        skipped = counters["skipped"]
        returncode = proc.returncode if proc.returncode is not None else 0

        errors: List[str] = []
        if returncode != 0:
            errors.extend(line.rstrip() for line in stderr_buf if line.strip())

        stderr_lower = "".join(stderr_buf).lower()
        if (
            saved == 0
            and skipped == 0
            and returncode != 0
            and ("could not lock" in stderr_lower or "could not claim" in stderr_lower)
        ):
            raise DarktableMCPError(
                f"Could not access camera at {port}. Another process is "
                "holding it (typically gvfs / GNOME's volume monitor on "
                "Linux desktops). Disconnect and reconnect the camera, "
                "or stop gvfs-gphoto2-volume-monitor, then retry."
            )

        return saved, errors

    def _count_files_on_disk(self, destination: Path) -> int:
        """Count files in the destination directory (non-recursive, ignore log)."""
        if not destination.exists():
            return 0
        return sum(
            1
            for entry in destination.iterdir()
            if entry.is_file() and entry.name != self.PROGRESS_LOG_NAME
        )

    # ---- USB Mass Storage handling -----------------------------------------
    #
    # Some bodies (Nikon DSLRs in particular) can simultaneously expose one
    # card via PTP and the other as a USB Mass-Storage block device. gphoto2
    # `--auto-detect` reports both: a normal PTP entry plus a generic
    # "Mass Storage Camera (disk:/media/...)" entry. The pre-fix `import_from_camera`
    # treated them as two separate cameras and made the user pick — which
    # silently halved the import (we hit this on a real Nikon D800E shoot).

    @staticmethod
    def _is_msc_port(port: str) -> bool:
        """True for the `disk:/...` port style gphoto2 uses for USB-MSC mounts."""
        return port.startswith(_MSC_PORT_PREFIX)

    @staticmethod
    def _msc_mount(port: str) -> Path:
        """Strip the `disk:` prefix and return the mount point as a Path."""
        return Path(port[len(_MSC_PORT_PREFIX):])

    @staticmethod
    def _msc_matches_ptp(msc_port: str, ptp_model: str) -> bool:
        """Heuristic: does this MSC mount look like the same device as a PTP camera?

        Compares 4+ character word tokens (case-insensitive) shared between
        the mount-path basename and the PTP model name. Example: mount
        `/media/user/NIKON D800E` matches PTP model "Nikon DSC D800E"
        because they share {"NIKON", "D800E"}. A card reader holding a
        different brand's card won't match and stays in its own group.
        """
        if not msc_port.startswith(_MSC_PORT_PREFIX):
            return False
        basename = Path(msc_port[len(_MSC_PORT_PREFIX):]).name
        model_words = {w.upper() for w in _MODEL_WORD_RE.findall(ptp_model)}
        mount_words = {w.upper() for w in _MODEL_WORD_RE.findall(basename)}
        return bool(model_words & mount_words)

    def _group_cameras(
        self, cameras: List[Dict[str, str]]
    ) -> List[List[Dict[str, str]]]:
        """Group MSC mounts with the PTP camera they likely belong to.

        Returns a list of groups; each group is a non-empty list of camera
        dicts that should be imported together. PTP-only cameras and
        unmatched MSC mounts each get their own singleton group.

        We pair each MSC entry with at most one PTP camera (greedy first
        match) so two PTP cameras can't both claim the same card mount.
        """
        msc = [c for c in cameras if self._is_msc_port(c["port"])]
        ptp = [c for c in cameras if not self._is_msc_port(c["port"])]

        used: set = set()
        groups: List[List[Dict[str, str]]] = []
        for ptp_cam in ptp:
            group = [ptp_cam]
            for i, msc_cam in enumerate(msc):
                if i in used:
                    continue
                if self._msc_matches_ptp(msc_cam["port"], ptp_cam["model"]):
                    group.append(msc_cam)
                    used.add(i)
            groups.append(group)
        for i, msc_cam in enumerate(msc):
            if i not in used:
                groups.append([msc_cam])
        return groups

    def _download_from_msc(
        self,
        mount: Path,
        destination: Path,
        timeout_seconds: int = DOWNLOAD_TIMEOUT_DEFAULT,
    ) -> Tuple[int, List[str]]:
        """Copy DCIM-shaped files from a USB Mass-Storage card mount.

        Walks `<mount>/DCIM/<subdir>/` for image and video files and copies
        each into `destination`. Files that already exist with matching size
        are skipped (cheap idempotent resume — same-name + same-size is good
        enough for cards where filenames are unique per device session).

        timeout_seconds is accepted for API symmetry with the PTP path but
        not enforced — filesystem copies are bounded by their own I/O.
        """
        del timeout_seconds  # filesystem copy doesn't need a subprocess timeout
        destination.mkdir(parents=True, exist_ok=True)

        dcim = mount / "DCIM"
        if not dcim.is_dir():
            return 0, [f"no DCIM/ folder under {mount}"]

        images: List[Path] = []
        for sub in sorted(dcim.iterdir()):
            if sub.is_dir():
                for entry in sorted(sub.iterdir()):
                    if entry.is_file() and not entry.name.startswith("."):
                        images.append(entry)

        expected = len(images)
        log_path = destination / self.PROGRESS_LOG_NAME
        saved = 0
        errors: List[str] = []

        with open(log_path, "a", encoding="utf-8") as log:
            log.write(
                f"\n=== Import (MSC) started "
                f"{datetime.now().isoformat(timespec='seconds')} ===\n"
            )
            log.write(f"Mount: {mount}\n")
            log.write(f"Destination: {destination}\n")
            log.write(f"Files found under DCIM: {expected}\n")
            log.flush()

            for src in images:
                dst = destination / src.name
                try:
                    if dst.exists() and dst.stat().st_size == src.stat().st_size:
                        continue
                    shutil.copy2(src, dst)
                    saved += 1
                    ts = datetime.now().strftime("%H:%M:%S")
                    log.write(f"[{ts}] ({saved}/{expected}) {src.name}\n")
                    log.flush()
                except OSError as exc:
                    errors.append(f"{src.name}: {exc}")
                    log.write(f"!! {src.name}: {exc}\n")
                    log.flush()

            log.write(
                f"=== Import (MSC) finished "
                f"{datetime.now().isoformat(timespec='seconds')}: "
                f"{saved} new, {expected} found ===\n"
            )
            log.flush()

        return saved, errors

    def _download_from_camera(
        self,
        model: str,
        port: str,
        destination: Path,
        timeout_seconds: int = DOWNLOAD_TIMEOUT_DEFAULT,
    ) -> Tuple[int, List[str]]:
        """Copy all files from a camera, walking each storage folder.

        - Pre-flight: enumerate leaf folders + count expected files per folder.
        - During transfer: stream per-file progress to <dest>/.import.log
          so the user can `tail -f` it.
        - Per-folder failures are recorded but don't abort the whole
          transfer (a flaky CF slot won't sink a healthy SD slot).
        - Lock errors raised before any progress propagate so the user
          sees the gvfs-style hint instead of a vague partial result.
        - Post-flight: validate disk file count vs expected; surface a
          shortfall warning so silent under-copies are visible.

        Args:
            model: gphoto2 model string (e.g. "Nikon DSC D800E").
            port: gphoto2 port string (e.g. "usb:002,002").
            destination: directory to write files into. Created if missing.
            timeout_seconds: subprocess timeout per folder. Default 3600 s.

        Returns:
            Tuple of (total_files_saved, error_messages).

        Raises:
            DarktableMCPError: gphoto2 missing, or camera lock fails before
                anything is copied.
            subprocess.TimeoutExpired: a per-folder transfer exceeded
                `timeout_seconds`. Caller is responsible; partial files may
                remain in `destination`.
        """
        # USB Mass-Storage entries from gphoto2 take a totally different
        # route — gphoto2's PTP folder enumeration doesn't apply, and the
        # files are just regular files on a mounted filesystem. Dispatch
        # to the filesystem walker so the orchestrator above can iterate
        # PTP+MSC pairs uniformly via this single entry point.
        if self._is_msc_port(port):
            return self._download_from_msc(
                self._msc_mount(port), destination, timeout_seconds
            )

        destination.mkdir(parents=True, exist_ok=True)

        folders = self._list_image_folders(model, port)

        # Pre-flight expected counts (best-effort; skips if --num-files fails).
        expected_per_folder: List[Optional[int]] = []
        expected_total = 0
        for folder in folders:
            try:
                count = self._count_files_in_folder(model, port, folder)
            except DarktableMCPError:
                count = None
            expected_per_folder.append(count)
            if count:
                expected_total += count

        log_path = destination / self.PROGRESS_LOG_NAME
        total_count = 0
        all_errors: List[str] = []

        log = open(log_path, "a", encoding="utf-8")
        try:
            log.write(
                f"\n=== Import started "
                f"{datetime.now().isoformat(timespec='seconds')} ===\n"
            )
            log.write(f"Camera: {model} ({port})\n")
            log.write(f"Destination: {destination}\n")
            log.write(
                f"Folders: {len(folders)}; expected files: "
                f"{expected_total if expected_total else '?'}\n"
            )
            log.flush()

            for folder, expected in zip(folders, expected_per_folder):
                expected_str = expected if expected is not None else "?"
                log.write(f"\n-- Folder {folder} (expected {expected_str}) --\n")
                log.flush()
                try:
                    saved, errors = self._download_one_folder(
                        model,
                        port,
                        folder,
                        destination,
                        timeout_seconds,
                        progress_log=log,
                        expected_total=expected_total or None,
                    )
                except DarktableMCPError as exc:
                    if total_count == 0 and not all_errors:
                        raise
                    all_errors.append(f"folder {folder}: {exc}")
                    log.write(f"!! ERROR in {folder}: {exc}\n")
                    log.flush()
                    continue
                total_count += saved
                all_errors.extend(errors)
                log.write(f"-- Folder {folder} done: {saved} new file(s) --\n")
                log.flush()

            disk_count = self._count_files_on_disk(destination)
            if expected_total and disk_count < expected_total:
                shortfall = expected_total - disk_count
                msg = (
                    f"Post-flight check: {disk_count}/{expected_total} files "
                    f"in destination — {shortfall} short of expected"
                )
                all_errors.append(msg)
                log.write(f"!! {msg}\n")
            log.write(
                f"\n=== Import finished "
                f"{datetime.now().isoformat(timespec='seconds')}: "
                f"{total_count} new, {disk_count} on disk, "
                f"{expected_total or '?'} expected ===\n"
            )
            log.flush()
        finally:
            log.close()

        return total_count, all_errors

    def import_from_camera(self, arguments: Dict[str, Any]) -> str:
        """Copy all photos from a connected camera to a local directory.

        Detects connected cameras via gphoto2 (libgphoto2 — same library
        darktable's GUI camera-import uses) and copies all files to a
        destination directory. Per-file progress is streamed to a log
        file inside the destination so long imports can be monitored
        with `tail -f`. Registering the directory with darktable's
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

        # Group PTP camera entries with any MSC mounts that look like the
        # same physical device. A Nikon body in hybrid mode shows up as both
        # a PTP camera and a "Mass Storage Camera (disk:/media/...NIKON
        # D800E)" entry — they are one device with two cards exposed via
        # different protocols, and both should be imported together.
        groups = self._group_cameras(cameras)

        if camera_port:
            target_group = next(
                (g for g in groups if any(c["port"] == camera_port for c in g)),
                None,
            )
            if target_group is None:
                ports = ", ".join(c["port"] for c in cameras)
                raise DarktableMCPError(
                    f"camera_port '{camera_port}' not found. "
                    f"Detected ports: {ports}"
                )
        elif len(groups) > 1:
            listing = "; ".join(
                " + ".join(f"{c['model']} ({c['port']})" for c in g) for g in groups
            )
            raise DarktableMCPError(
                f"Multiple distinct cameras detected: {listing}. "
                "Pass camera_port=... to select one."
            )
        else:
            target_group = groups[0]

        if destination_arg:
            destination = Path(destination_arg).expanduser().resolve()
        else:
            today = date.today().isoformat()
            destination = (Path.home() / "Pictures" / f"import-{today}").resolve()

        # Iterate every entry in the chosen group (one PTP source + zero or
        # more MSC mounts in the common Nikon-hybrid case). A timeout on the
        # first source is fatal — nothing has been copied yet so the user
        # gets the resume hint. After at least one source has produced
        # files, downstream errors are recorded but don't abort: half a
        # successful import beats no import at all.
        total_count = 0
        all_errors: List[str] = []
        for entry in target_group:
            try:
                count, errors = self._download_from_camera(
                    entry["model"], entry["port"], destination, timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                if total_count == 0 and not all_errors:
                    raise DarktableMCPError(
                        f"Camera transfer timed out after {timeout_seconds} s. "
                        f"Destination {destination} may contain partial files. "
                        "Re-run the tool to resume — `--skip-existing` is on, "
                        "so already-copied files are not re-downloaded."
                    ) from exc
                all_errors.append(
                    f"{entry['model']} ({entry['port']}) timed out"
                )
                continue
            except DarktableMCPError as exc:
                if total_count == 0 and not all_errors:
                    raise
                all_errors.append(f"{entry['model']} ({entry['port']}): {exc}")
                continue
            total_count += count
            all_errors.extend(errors)

        if total_count == 0 and all_errors:
            first = target_group[0]
            raise DarktableMCPError(
                f"No files were transferred from {first['model']} "
                f"({first['port']}). First error: {all_errors[0]}"
            )

        log_path = destination / self.PROGRESS_LOG_NAME
        disk_count = self._count_files_on_disk(destination)
        sources = ", ".join(f"{c['model']} ({c['port']})" for c in target_group)

        summary_parts = [
            f"Copied {total_count} new file(s) from {sources}",
            f"Destination: {destination} ({disk_count} files on disk)",
            f"Progress log: {log_path}",
            f"  Tail in another terminal during long imports: tail -f \"{log_path}\"",
            "Open darktable and choose 'import folder' on this directory to add them to your library.",
        ]
        if all_errors:
            summary_parts.append(
                f"Warning: {len(all_errors)} issue(s). First: {all_errors[0]}"
            )
        return "\n".join(summary_parts)
