"""Camera-import tooling using libgphoto2."""

import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

from ..utils.errors import DarktableMCPError


logger = logging.getLogger(__name__)

_MSC_PORT_PREFIX = "disk:"
_MODEL_WORD_RE = re.compile(r"[A-Za-z0-9]{4,}")
_TAG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: `gphoto2 --get-config serialnumber` prints a `Current: <value>` line.
_SERIAL_CURRENT_RE = re.compile(r"^Current:\s*(.+?)\s*$", re.MULTILINE)
#: Words that carry no device identity. gphoto2 calls every USB-Mass-Storage
#: mount "Mass Storage Camera", so a model made only of these contributes
#: nothing to a destination tag and is dropped rather than baked in.
_GENERIC_MODEL_WORDS = frozenset({"MASS", "STORAGE", "CAMERA", "USB", "DISK", "GENERIC"})


class CameraTools:
    """Camera import via gphoto2 (libgphoto2 — same library darktable's GUI uses).

    Destination layout: every source folder gets its own subdirectory under
    the destination, `<destination>/<camera-identity>_<source-folder-tag>/`.
    Camera filenames are only unique *within* one folder on one body —
    `100NCD80/DSC_0001.NEF` and `101NCD80/DSC_0001.NEF` are different
    photos, so are the two `DSC_0001.NEF` on the two cards of a dual-slot
    body, and so are the `DSC_0001.NEF` of two different Nikon bodies whose
    gphoto2 folder paths are byte-identical (`/store_00010001/DCIM/100NCD80`
    is generic across bodies). Flattening any of those onto one path made
    the newcomer vanish — gphoto2's `--skip-existing` and the MSC size
    compare both read it as "already copied" — which is silent data loss.

    Two things prevent that now:

    1. The destination tag carries the camera's identity: a sanitised model
       plus, when the body reports one, its serial number. The tag is
       derived only from things that are stable across unplug/replug (never
       from the gphoto2 port, which is reassigned), so re-running into the
       same destination is still a cheap idempotent resume.
    2. On the MSC path, an existing destination file is only ever treated as
       "already copied" when it still looks like the same file. Anything
       else is written under a distinct name; nothing is overwritten.

    What is still *not* guaranteed: two bodies of the same model that report
    no serial number share a tag. On the MSC path point 2 keeps both files
    regardless. On the PTP path gphoto2's own `--skip-existing` decides, and
    it compares nothing but the path — see `_download_one_folder`.
    """

    DOWNLOAD_TIMEOUT_DEFAULT = 3600
    LIST_FOLDERS_TIMEOUT = 30
    NUM_FILES_TIMEOUT = 30
    SERIAL_TIMEOUT = 15
    PROGRESS_LOG_NAME = ".import.log"
    #: Subdirectory used when the camera folder tree could not be enumerated
    #: and a single recursive download from "/" is used instead.
    ROOT_FOLDER_TAG = "camera"
    #: Marker that lets `import_from_camera` pick post-flight shortfalls out
    #: of the generic error list and show them prominently.
    SHORTFALL_PREFIX = "Post-flight check:"
    #: Marker for "this file could not keep its own name, both were kept".
    #: Not a failure, but the user must be told which file is which.
    RENAMED_PREFIX = "Name conflict:"
    #: Bytes read from each end of a file when deciding "is this the same
    #: photo we already copied?". See `_looks_like_same_file`.
    EDGE_SAMPLE_BYTES = 8192
    #: Upper bound on `-2`, `-3`, ... suffixes tried before giving up on a
    #: name. Reaching it means something is badly wrong; we error instead of
    #: looping or overwriting.
    MAX_DISTINCT_SUFFIX = 99

    _FOLDER_LINE_RE = re.compile(r"There (?:is|are) (\d+) folders? in folder '([^']+)'\.")
    _NUM_FILES_RE = re.compile(r":\s*(\d+)\s*$", re.MULTILINE)

    #: stderr of the last `--auto-detect` that exited non-zero yet still
    #: parsed cameras. Surfaced in the import summary so a partially failed
    #: detect is distinguishable from a clean one.
    last_detect_warning: Optional[str] = None

    def _detect_cameras(self) -> List[Dict[str, str]]:
        """Run `gphoto2 --auto-detect` and return parsed list of cameras.

        A non-zero exit code with cameras still parsed means the detect
        only partly worked (one bus enumerated, another refused). That is
        not fatal — the parsed entries are usable — but it must not look
        like a clean detect, so the stderr is logged and stashed on
        `last_detect_warning` for the caller to surface.

        Returns:
            List of dicts with keys "model" and "port", e.g.
            [{"model": "Nikon DSC D800E", "port": "usb:002,002"}].
            Empty list if no cameras detected.

        Raises:
            DarktableMCPError: if gphoto2 binary is not installed.
        """
        self.last_detect_warning = None
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
        if cameras and result.returncode != 0:
            detail = result.stderr.strip()[:200] or "(no stderr)"
            self.last_detect_warning = (
                f"gphoto2 --auto-detect exited with code {result.returncode} "
                f"but still listed {len(cameras)} camera(s); some may be "
                f"missing. stderr: {detail}"
            )
            logger.warning("%s", self.last_detect_warning)
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

    @staticmethod
    def _model_tag(model: str) -> str:
        """Sanitise a camera model string into a tag fragment.

        Words that identify no particular device are dropped: gphoto2 labels
        every USB-Mass-Storage mount "Mass Storage Camera", and baking that
        into the destination would look like identity while providing none.

        Args:
            model: gphoto2 model string, e.g. "Nikon DSC D800E".

        Returns:
            e.g. "Nikon_DSC_D800E". Empty string when nothing distinctive
            survives (e.g. "Mass Storage Camera") — callers must treat that
            as "no identity available", not as a usable tag.
        """
        words = [w for w in re.split(r"[^A-Za-z0-9]+", model or "") if w]
        kept = [w for w in words if w.upper() not in _GENERIC_MODEL_WORDS]
        return "_".join(kept)

    def _probe_serial(self, model: str, port: str) -> Optional[str]:
        """Ask the camera for its serial number. None whenever that fails.

        Deliberately total: most compacts, many DSLRs and every MSC mount
        have no `serialnumber` config, and a camera that is busy answering a
        transfer must not have the whole import fail over an optional tag
        component. Every failure mode — binary missing, timeout, non-zero
        exit, unparseable output, all-zero placeholder — degrades to None
        and the caller falls back to a model-only identity.

        Args:
            model: gphoto2 model string.
            port: gphoto2 port string.

        Returns:
            Sanitised serial with leading zero padding removed, or None.
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
                    "--get-config",
                    "serialnumber",
                ],
                capture_output=True,
                text=True,
                timeout=self.SERIAL_TIMEOUT,
                env=env,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None

        if result.returncode != 0:
            return None
        match = _SERIAL_CURRENT_RE.search(result.stdout)
        if not match:
            return None
        # Nikon pads to 32 hex chars with leading zeros; some bodies report
        # nothing but zeros, which is a placeholder rather than an identity.
        cleaned = re.sub(r"[^A-Za-z0-9]", "", match.group(1)).lstrip("0")
        return cleaned[:32] or None

    def _camera_identity(self, model: str, port: str) -> str:
        """Stable per-device tag fragment for one camera.

        Stability across runs is the whole point: the resume workflow copies
        into the same destination again and relies on landing on the same
        paths. So the identity is built only from things that survive an
        unplug — the model and, when the body offers one, its serial. The
        port is deliberately *not* used: gphoto2 reassigns `usb:002,004` on
        every re-plug, which would scatter one camera across a new
        subdirectory per session and re-download everything.

        Call this once per camera, not once per folder — it may spawn a
        gphoto2 subprocess.

        Args:
            model: gphoto2 model string.
            port: gphoto2 port string (used to reach the camera, never as
                part of the returned identity).

        Returns:
            e.g. "Nikon_DSC_D800E_sn_3001234", or "Nikon_DSC_D800E" when the
            body reports no serial, or "" when neither is available.
        """
        parts = [p for p in (self._model_tag(model),) if p]
        serial = self._probe_serial(model, port)
        if serial:
            parts.append(f"sn_{serial}")
        return "_".join(parts)

    @classmethod
    def _folder_tag(cls, src_folder: str) -> str:
        """Turn a camera folder path into one filesystem-safe directory name.

        The whole path is kept (joined with "_") because the leaf name alone
        is not unique: a dual-slot body happily exposes
        `/store_00010001/DCIM/101D800E` and `/store_00020001/DCIM/101D800E`.

        Args:
            src_folder: gphoto2 folder path, e.g. "/store_1/DCIM/101D800E".

        Returns:
            Directory name, e.g. "store_1_DCIM_101D800E". `ROOT_FOLDER_TAG`
            for "/" or anything that sanitises down to nothing.
        """
        parts = [p for p in src_folder.split("/") if p not in ("", ".", "..")]
        if not parts:
            return cls.ROOT_FOLDER_TAG
        tag = _TAG_UNSAFE_RE.sub("_", "_".join(parts))
        tag = re.sub(r"_+", "_", tag).strip("_")
        return tag or cls.ROOT_FOLDER_TAG

    @classmethod
    def _camera_folder_tag(cls, identity: str, src_folder: str) -> str:
        """Combine camera identity and source folder into one directory name.

        The folder path alone is not unique across bodies:
        `/store_00010001/DCIM/100NCD80` is what *every* Nikon of that
        generation reports, so without the identity prefix two different
        bodies imported into one destination — the documented resume
        workflow, and the default `~/Pictures/import-<today>/` shared by
        every import on the same day — would land on the same path.

        Args:
            identity: `_camera_identity` output; "" when unavailable.
            src_folder: gphoto2 folder path.

        Returns:
            e.g. "Nikon_DSC_D800E_store_00010001_DCIM_100NCD80". Falls back
            to the bare folder tag when `identity` is empty.
        """
        path_tag = cls._folder_tag(src_folder)
        ident = _TAG_UNSAFE_RE.sub("_", identity).strip("_") if identity else ""
        return f"{ident}_{path_tag}" if ident else path_tag

    @classmethod
    def _folder_dest(cls, destination: Path, src_folder: str, identity: str = "") -> Path:
        """Destination subdirectory that receives one camera folder's files."""
        return destination / cls._camera_folder_tag(identity, src_folder)

    def _download_one_folder(
        self,
        model: str,
        port: str,
        src_folder: str,
        destination: Path,
        timeout_seconds: int,
        progress_log: Optional[TextIO] = None,
        expected_total: Optional[int] = None,
        identity: Optional[str] = None,
    ) -> Tuple[int, int, List[str]]:
        """Run gphoto2 to copy all files in a single camera folder.

        Files land in `<destination>/<identity>_<folder tag>/`, never
        directly in `destination`: `%f` is the bare basename, so a shared
        destination would make `100NCD80/DSC_0001.NEF` and
        `101NCD80/DSC_0001.NEF` collide — and, because the folder path is
        generic across bodies of the same generation, would do the same to
        two different cameras imported into one destination.
        `--skip-existing` would silently drop the newcomer in both cases.

        What `--skip-existing` does and does not guarantee: gphoto2 compares
        *nothing but the target path*. It never looks at size or content. So
        a skip is safe exactly to the extent that the path is unique to one
        photo, which is what the `<identity>_<folder tag>` directory buys.
        The residual hole is two bodies of the same model that both report
        no serial number: they share an identity, and if both hold a
        `DSC_0001.NEF` in the same folder path the second one is skipped
        with no error. Nothing on the PTP path can detect that — gphoto2
        makes the decision inside its own process, and the post-flight
        count sees a full destination. Import such bodies into separate
        destinations. (The MSC path has no such hole; see
        `_download_from_msc`, which never trusts a path alone.)

        Streams stdout via Popen + reader threads so per-file progress is
        written to `progress_log` as the transfer happens (the user can
        `tail -f` the log file in another terminal). `--filename %f.%C`
        preserves the file extension.

        When `src_folder` is "/" (folder enumeration failed, so this is one
        recursive pull of the whole camera) `%F` — gphoto2's own camera
        folder path — is inserted as well, so the recursion still cannot
        flatten two folders onto each other.

        Args:
            model: gphoto2 model string.
            port: gphoto2 port string.
            src_folder: camera folder to pull.
            destination: import root; the per-folder subdirectory is
                created underneath it.
            timeout_seconds: subprocess timeout for this folder.
            progress_log: open text file handle to receive timestamped
                "Saving file as ..." lines. None to disable logging.
            expected_total: if known, formats progress as "(N/total)".
            identity: camera identity from `_camera_identity`, computed once
                per camera by the caller so the serial probe is not repeated
                per folder. None means "derive it from `model` alone", which
                is the safe fallback for direct callers — never a bare
                folder tag with no camera in it.

        Returns:
            Tuple of (files_saved, files_skipped, error_lines).

        Raises:
            DarktableMCPError: gphoto2 missing or camera locked by another
                process (gvfs etc.).
            subprocess.TimeoutExpired: caller handles partial transfers.
        """
        ident = identity if identity is not None else self._model_tag(model)
        folder_dest = self._folder_dest(destination, src_folder, ident)
        folder_dest.mkdir(parents=True, exist_ok=True)
        if self._folder_tag(src_folder) == self.ROOT_FOLDER_TAG:
            filename_pattern = f"{folder_dest}/%F/%f.%C"
        else:
            filename_pattern = f"{folder_dest}/%f.%C"

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
            filename_pattern,
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

        return saved, skipped, errors

    def _count_files_on_disk(self, destination: Path) -> int:
        """Count files under the destination, recursively, ignoring the log.

        Recursive because every source folder now gets its own
        subdirectory — a flat `iterdir()` would count zero and make the
        post-flight shortfall check fire on a perfectly good import.
        """
        if not destination.exists():
            return 0
        return sum(
            1
            for entry in destination.rglob("*")
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

    @classmethod
    def _msc_folder_tag(cls, mount: Path, sub: Path, model: str = "") -> str:
        """Destination subdirectory name for one DCIM folder on one card.

        The card label is part of the tag because two cards of the same body
        routinely carry the same folder name (`100NCD80`) holding different
        photos with the same filenames. The model is prefixed too when it
        adds anything: gphoto2 reports MSC mounts as "Mass Storage Camera",
        which identifies nothing, and on a hybrid body the mount label
        ("NIKON D800E") already repeats the model, so both cases are dropped
        rather than doubled up.

        A label is not a device identity — two cards formatted in the same
        body carry the same one (`EOS_DIGITAL`), and a single-slot reader
        gives them the same mount path as well. That collision is handled
        where it actually matters, in `_download_from_msc`, which never
        overwrites and never skips a file it cannot recognise.

        Args:
            mount: card mount point, e.g. /media/user/NIKON D800E.
            sub: the DCIM subfolder, e.g. <mount>/DCIM/100NCD80.
            model: gphoto2 model string for this source, if known.

        Returns:
            Filesystem-safe directory name, e.g. "NIKON_D800E_100NCD80".
        """
        model_tag = cls._model_tag(model) if model else ""
        if model_tag and cls._msc_matches_ptp(f"{_MSC_PORT_PREFIX}{mount}", model):
            model_tag = ""  # the label already says the same thing
        parts = [p for p in (model_tag, mount.name, sub.name) if p]
        return cls._folder_tag("/".join(parts)) if parts else cls.ROOT_FOLDER_TAG

    @classmethod
    def _edge_sample(cls, path: Path, size: int) -> bytes:
        """Read the first and last `EDGE_SAMPLE_BYTES` of a file."""
        n = cls.EDGE_SAMPLE_BYTES
        with open(path, "rb") as handle:
            head = handle.read(n)
            if size <= 2 * n:
                return head + handle.read()
            handle.seek(-n, os.SEEK_END)
            return head + handle.read(n)

    @classmethod
    def _looks_like_same_file(cls, src: Path, dst: Path) -> bool:
        """Is `dst` plausibly the copy of `src` a previous run already made?

        Equal size *and* equal first/last 8 KB. Deliberately not a hash:
        hashing every 40 MB raw on a 2000-shot card means reading ~80 GB to
        answer a question that is almost always "yes, same file", whereas
        two reads of 8 KB cost the same regardless of file size. The edges
        are where two different photos differ even when their sizes happen
        to match — the EXIF timestamp and frame counter sit in the header,
        and the last block of compressed image data is effectively random.

        The tradeoff is one-sided by construction: this test accepts a
        strict subset of what a size compare accepts, so it can only ever
        make us copy something we did not have to. It cannot *prove* two
        files are identical, so it is never used to authorise an overwrite —
        only to authorise a skip, whose worst case is a redundant copy under
        a `-2` name rather than a lost photo.

        Args:
            src: file on the card.
            dst: candidate file already in the destination.

        Returns:
            True when the two match on size and both sampled edges. False on
            any mismatch, and on any OSError — unreadable means unproven,
            and unproven means "copy it".
        """
        try:
            size = src.stat().st_size
            if size != dst.stat().st_size:
                return False
            if size == 0:
                return True
            return cls._edge_sample(src, size) == cls._edge_sample(dst, size)
        except OSError:
            return False

    @staticmethod
    def _distinct_name(name: str, index: int) -> str:
        """Insert a `-N` discriminator before the extension.

        Args:
            name: original filename, e.g. "IMG_0001.CR3".
            index: discriminator, 2 for the first alternative.

        Returns:
            e.g. "IMG_0001-2.CR3". Deterministic, so a resumed run reuses
            the same alternative name instead of inventing a new one.
        """
        stem, dot, ext = name.rpartition(".")
        if not dot:
            return f"{name}-{index}"
        return f"{stem}-{index}{dot}{ext}"

    def _msc_target_path(self, src: Path, sub_dest: Path) -> Optional[Path]:
        """Decide where one card file may be written, or that it is present.

        The one rule: never overwrite, and never skip, a file that is not
        provably the file we already have. `shutil.copy2` onto an occupied
        path destroys a photo that was already safely on disk — the exact
        failure this whole module is written to prevent — and skipping on a
        bare size match destroys it just as effectively by never copying it.

        Args:
            src: file on the card.
            sub_dest: destination subdirectory for its source folder.

        Returns:
            A path to copy to — either `sub_dest/<name>` or a deterministic
            `<stem>-N<ext>` alternative — or None when this exact file is
            already in the destination and should be counted as a skip.

        Raises:
            OSError: `MAX_DISTINCT_SUFFIX` alternatives are all taken by
                other files. Reported per-file by the caller; still no
                overwrite.
        """
        dst = sub_dest / src.name
        if not dst.exists():
            return dst
        if self._looks_like_same_file(src, dst):
            return None
        for index in range(2, self.MAX_DISTINCT_SUFFIX + 1):
            candidate = sub_dest / self._distinct_name(src.name, index)
            if not candidate.exists():
                return candidate
            # A previous run already parked this same file here: skipping
            # keeps the resume idempotent instead of growing a -3, -4, ...
            if self._looks_like_same_file(src, candidate):
                return None
        raise OSError(
            f"{self.MAX_DISTINCT_SUFFIX} differing files already occupy the "
            f"{src.name} name in {sub_dest}; refusing to overwrite any of them"
        )

    def _download_from_msc(
        self,
        mount: Path,
        destination: Path,
        timeout_seconds: int = DOWNLOAD_TIMEOUT_DEFAULT,
        model: str = "",
    ) -> Tuple[int, int, List[str]]:
        """Copy DCIM-shaped files from a USB Mass-Storage card mount.

        Walks `<mount>/DCIM/<subdir>/` for image and video files and copies
        each into `<destination>/<card>_<subdir>/`. One directory per source
        folder keeps files from different folders and different cards apart,
        but a card label is not a device identity — two cards formatted in
        the same body are both `EOS_DIGITAL`, and a single-slot reader
        mounts them at the same path — so the directory is never trusted on
        its own. Every write goes through `_msc_target_path`, which:

        - copies when nothing is in the way;
        - skips, counting a skip, when the destination file still looks like
          the same file (size plus both 8 KB edges), which is the resume
          case;
        - and otherwise writes the newcomer under a deterministic
          `IMG_0001-2.CR3` name and reports it with `RENAMED_PREFIX`, so the
          user ends up holding both photos.

        `shutil.copy2` is therefore never aimed at an occupied path. The
        earlier "exists and same size ⇒ skip, else overwrite" pair lost a
        photo either way: the skip branch never copied the newcomer, and the
        overwrite branch destroyed a file already safely on disk.

        `timeout_seconds` is an overall budget for the whole walk, checked
        between files. A flaky reader cannot stall the import for an hour
        with no explanation any more; the copy stops and reports how far it
        got so a re-run can resume.

        Args:
            mount: card mount point.
            destination: import root.
            timeout_seconds: overall budget for this card, in seconds.
            model: gphoto2 model string for this source, if known. Folded
                into the destination tag when it adds identity.

        Returns:
            Tuple of (files_saved, files_skipped, messages). Messages hold
            real errors and `RENAMED_PREFIX` notices; a notice means both
            files were kept, not that anything failed.
        """
        deadline = time.monotonic() + timeout_seconds
        destination.mkdir(parents=True, exist_ok=True)

        dcim = mount / "DCIM"
        if not dcim.is_dir():
            return 0, 0, [f"no DCIM/ folder under {mount}"]

        # (source file, destination subdirectory) pairs, folder by folder.
        images: List[Tuple[Path, Path]] = []
        for sub in sorted(dcim.iterdir()):
            if sub.is_dir():
                sub_dest = destination / self._msc_folder_tag(mount, sub, model)
                for entry in sorted(sub.iterdir()):
                    if entry.is_file() and not entry.name.startswith("."):
                        images.append((entry, sub_dest))

        expected = len(images)
        log_path = destination / self.PROGRESS_LOG_NAME
        saved = 0
        skipped = 0
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

            for index, (src, sub_dest) in enumerate(images):
                if time.monotonic() >= deadline:
                    msg = (
                        f"Timed out after {timeout_seconds} s reading {mount}: "
                        f"copied {saved} new, skipped {skipped}, "
                        f"{expected - index} file(s) not read. The card reader "
                        f"may be flaky — re-run the tool to resume."
                    )
                    errors.append(msg)
                    log.write(f"!! {msg}\n")
                    log.flush()
                    break
                try:
                    sub_dest.mkdir(parents=True, exist_ok=True)
                    dst = self._msc_target_path(src, sub_dest)
                    if dst is None:
                        skipped += 1
                        continue
                    shutil.copy2(src, dst)
                    saved += 1
                    ts = datetime.now().strftime("%H:%M:%S")
                    log.write(f"[{ts}] ({saved}/{expected}) {sub_dest.name}/{dst.name}\n")
                    if dst.name != src.name:
                        note = (
                            f"{self.RENAMED_PREFIX} {sub_dest.name}/{src.name} is a "
                            f"different file from the one already in the destination; "
                            f"both kept, the new one as {dst.name}"
                        )
                        errors.append(note)
                        log.write(f"** {note}\n")
                    log.flush()
                except OSError as exc:
                    errors.append(f"{src.name}: {exc}")
                    log.write(f"!! {src.name}: {exc}\n")
                    log.flush()

            log.write(
                f"=== Import (MSC) finished "
                f"{datetime.now().isoformat(timespec='seconds')}: "
                f"{saved} new, {skipped} skipped, {expected} found ===\n"
            )
            log.flush()

        return saved, skipped, errors

    def _download_from_camera(
        self,
        model: str,
        port: str,
        destination: Path,
        timeout_seconds: int = DOWNLOAD_TIMEOUT_DEFAULT,
    ) -> Tuple[int, int, List[str]]:
        """Copy all files from a camera, walking each storage folder.

        - Pre-flight: resolve this camera's identity once (one gphoto2 call
          at most, never one per folder), enumerate leaf folders + count
          expected files per folder.
        - Each folder is written to its own
          `<dest>/<camera identity>_<folder tag>/` subdirectory, so
          same-named files from different folders, different cards or
          different bodies cannot overwrite or "skip-existing" each other.
        - During transfer: stream per-file progress to <dest>/.import.log
          so the user can `tail -f` it.
        - `timeout_seconds` is an overall budget for this camera, not a
          per-folder one: each folder gets what is left of it, so N folders
          can no longer take N × the number the user asked for.
        - Per-folder failures are recorded but don't abort the whole
          transfer (a flaky CF slot won't sink a healthy SD slot).
        - Lock errors raised before any progress propagate so the user
          sees the gvfs-style hint instead of a vague partial result.
        - Post-flight: validate disk file count vs expected; surface a
          shortfall warning so silent under-copies are visible. It counts
          the identity-prefixed directories this camera actually wrote to,
          so it stays honest when another body imported into the same
          destination. It cannot see a `--skip-existing` drop between two
          same-model bodies with no serial number, because such a drop
          leaves a full destination — see `_download_one_folder`.

        Args:
            model: gphoto2 model string (e.g. "Nikon DSC D800E").
            port: gphoto2 port string (e.g. "usb:002,002").
            destination: directory to write files into. Created if missing.
            timeout_seconds: overall budget for this camera, shared across
                all its folders. Default 3600 s.

        Returns:
            Tuple of (total_files_saved, total_files_skipped, error_messages).

        Raises:
            DarktableMCPError: gphoto2 missing, or camera lock fails before
                anything is copied.
            subprocess.TimeoutExpired: the first folder exceeded the budget
                before anything was copied. Caller is responsible; partial
                files may remain in `destination`. Once files have been
                copied, a later timeout is recorded as an error instead so
                the copied count is not lost.
        """
        # USB Mass-Storage entries from gphoto2 take a totally different
        # route — gphoto2's PTP folder enumeration doesn't apply, and the
        # files are just regular files on a mounted filesystem. Dispatch
        # to the filesystem walker so the orchestrator above can iterate
        # PTP+MSC pairs uniformly via this single entry point.
        if self._is_msc_port(port):
            return self._download_from_msc(
                self._msc_mount(port), destination, timeout_seconds, model=model
            )

        deadline = time.monotonic() + timeout_seconds
        destination.mkdir(parents=True, exist_ok=True)

        # Resolved once for the whole camera: the serial probe is a gphoto2
        # round-trip, and re-asking per folder would both cost time and risk
        # a folder landing under a different tag if one probe fails.
        identity = self._camera_identity(model, port)

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
        total_skipped = 0
        all_errors: List[str] = []

        log = open(log_path, "a", encoding="utf-8")
        try:
            log.write(
                f"\n=== Import started "
                f"{datetime.now().isoformat(timespec='seconds')} ===\n"
            )
            log.write(f"Camera: {model} ({port})\n")
            log.write(f"Camera identity tag: {identity or '(none available)'}\n")
            log.write(f"Destination: {destination}\n")
            log.write(
                f"Folders: {len(folders)}; expected files: "
                f"{expected_total if expected_total else '?'}\n"
            )
            log.flush()

            for position, (folder, expected) in enumerate(
                zip(folders, expected_per_folder)
            ):
                # Overall budget: each folder only gets what is left of it.
                remaining = math.ceil(deadline - time.monotonic())
                if remaining <= 0:
                    msg = (
                        f"Transfer budget of {timeout_seconds} s ran out before "
                        f"folder {folder}: {len(folders) - position} folder(s) "
                        f"not copied. Re-run the tool with a larger "
                        f"timeout_seconds to finish — already-copied files are "
                        f"skipped."
                    )
                    all_errors.append(msg)
                    log.write(f"!! {msg}\n")
                    log.flush()
                    break

                expected_str = expected if expected is not None else "?"
                log.write(f"\n-- Folder {folder} (expected {expected_str}) --\n")
                log.flush()
                try:
                    saved, skipped, errors = self._download_one_folder(
                        model,
                        port,
                        folder,
                        destination,
                        remaining,
                        progress_log=log,
                        expected_total=expected_total or None,
                        identity=identity,
                    )
                except DarktableMCPError as exc:
                    if total_count == 0 and not all_errors:
                        raise
                    all_errors.append(f"folder {folder}: {exc}")
                    log.write(f"!! ERROR in {folder}: {exc}\n")
                    log.flush()
                    continue
                except subprocess.TimeoutExpired:
                    if total_count == 0 and not all_errors:
                        raise
                    msg = (
                        f"folder {folder} timed out; {len(folders) - position - 1} "
                        f"further folder(s) not attempted"
                    )
                    all_errors.append(msg)
                    log.write(f"!! {msg}\n")
                    log.flush()
                    break
                total_count += saved
                total_skipped += skipped
                all_errors.extend(errors)
                log.write(
                    f"-- Folder {folder} done: {saved} new file(s), "
                    f"{skipped} skipped --\n"
                )
                log.flush()

            # Count only the folders belonging to this camera, so files put
            # here by another card in the same import don't mask a shortfall.
            disk_count = sum(
                self._count_files_on_disk(
                    self._folder_dest(destination, folder, identity)
                )
                for folder in folders
            )
            if expected_total and disk_count < expected_total:
                shortfall = expected_total - disk_count
                msg = (
                    f"{self.SHORTFALL_PREFIX} {disk_count}/{expected_total} files "
                    f"in destination — {shortfall} short of expected"
                )
                all_errors.append(msg)
                log.write(f"!! {msg}\n")
            log.write(
                f"\n=== Import finished "
                f"{datetime.now().isoformat(timespec='seconds')}: "
                f"{total_count} new, {total_skipped} skipped, "
                f"{disk_count} on disk, "
                f"{expected_total or '?'} expected ===\n"
            )
            log.flush()
        finally:
            log.close()

        return total_count, total_skipped, all_errors

    def import_from_camera(self, arguments: Dict[str, Any]) -> str:
        """Copy all photos from a connected camera to a local directory.

        Detects connected cameras via gphoto2 (libgphoto2 — same library
        darktable's GUI camera-import uses) and copies all files to a
        destination directory. Each camera folder / card folder lands in
        its own subdirectory,
        `<destination>/<camera identity>_<folder tag>/<filename>`, because
        camera filenames are only unique within one folder on one body and
        flattening them silently dropped duplicates. The identity is the
        camera model plus its serial number when it reports one; two bodies
        of the same model that report no serial share a subdirectory, and
        on the PTP path that is the one case where a same-named photo can
        still be dropped without an error — import such bodies into separate
        destinations. The USB-Mass-Storage path keeps both regardless: it
        never overwrites and never skips a file it cannot recognise, writing
        the newcomer as `IMG_0001-2.CR3` and saying so. Per-file progress is
        streamed to a log file inside the destination so long imports can
        be monitored with `tail -f`. Registering the directory with
        darktable's library is left to the user (open darktable, click
        "import folder") because the Lua API path for that step is not yet
        reliable on all installs.

        Args:
            arguments: Dictionary containing:
                - destination (str, optional): target directory.
                  Default: ~/Pictures/import-YYYY-MM-DD/
                - camera_port (str, optional): gphoto2 port string,
                  required when 2+ cameras are connected.
                - timeout_seconds (int, optional): overall budget for the
                  transfer from one source, shared across all of its
                  folders. Default 3600 (1 hour). Bump higher for cards
                  larger than ~50 GB.

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
        total_skipped = 0
        all_errors: List[str] = []
        for entry in target_group:
            try:
                count, skipped, errors = self._download_from_camera(
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
            total_skipped += skipped
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

        shortfalls = [e for e in all_errors if e.startswith(self.SHORTFALL_PREFIX)]
        renames = [e for e in all_errors if e.startswith(self.RENAMED_PREFIX)]
        other_errors = [
            e
            for e in all_errors
            if not e.startswith((self.SHORTFALL_PREFIX, self.RENAMED_PREFIX))
        ]

        summary_parts = [
            f"Copied {total_count} new file(s) from {sources}",
            f"Skipped {total_skipped} file(s) already present in the destination",
            f"Destination: {destination} ({disk_count} files on disk, "
            "one subdirectory per camera folder)",
            f"Progress log: {log_path}",
            f"  Tail in another terminal during long imports: tail -f \"{log_path}\"",
            "Open darktable and choose 'import folder' on this directory to add them to your library.",
        ]
        if shortfalls:
            # Loud on purpose: the user is about to format the card.
            summary_parts.append("")
            summary_parts.append(
                "!! INCOMPLETE IMPORT — some photos did not make it off the card:"
            )
            summary_parts.extend(f"     {msg}" for msg in shortfalls)
            summary_parts.append(
                "   Do NOT format the card. Re-run this tool to fetch the "
                "missing files (already-copied files are skipped)."
            )
        if renames:
            # Not a failure — both photos are on disk — but the user has to
            # know that some files are not under the name the camera gave
            # them, or they will go looking for a photo they think is lost.
            summary_parts.append("")
            summary_parts.append(
                f"Kept both copies for {len(renames)} name conflict(s) — a file "
                "already in the destination had the same name but different "
                "content, so nothing was overwritten:"
            )
            summary_parts.extend(f"     {msg}" for msg in renames[:5])
            if len(renames) > 5:
                summary_parts.append(f"     ... and {len(renames) - 5} more (see the log)")
        if other_errors:
            summary_parts.append(
                f"Warning: {len(other_errors)} issue(s). First: {other_errors[0]}"
            )
        if self.last_detect_warning:
            summary_parts.append(f"Note: {self.last_detect_warning}")
        return "\n".join(summary_parts)
