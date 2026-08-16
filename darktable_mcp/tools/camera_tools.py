"""Camera-import tooling using libgphoto2."""

import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from ..utils.errors import DarktableMCPError

logger = logging.getLogger(__name__)

_MSC_PORT_PREFIX = "disk:"
_MODEL_WORD_RE = re.compile(r"[A-Za-z0-9]{4,}")
_TAG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: `gphoto2 --get-config serialnumber` prints a `Current: <value>` line.
_SERIAL_CURRENT_RE = re.compile(r"^Current:\s*(.+?)\s*$", re.MULTILINE)
#: A serial number is the only thing that makes an identity unique to one
#: body, and `_camera_identity` spells it `sn_<serial>`. Lower-case on
#: purpose: a model word like "SN" survives `_model_tag` upper-cased and
#: must not be mistaken for a serial.
_IDENTITY_SERIAL_RE = re.compile(r"(?:^|_)sn_[A-Za-z0-9]")
#: `gphoto2 --list-files` prints one `#<n> <name> <perms> <size> <mime>` line
#: per file. The name is separated from the rest by 2+ spaces.
_LIST_FILE_LINE_RE = re.compile(r"^#(\d+)\s+(\S.*?)\s{2,}(.*)$")
#: First number in the remainder of such a line, with gphoto2's unit suffix.
#: Real gphoto2 prints KB; older/other builds print raw bytes, so the unit is
#: optional and "no unit" means bytes.
_LIST_SIZE_RE = re.compile(r"(\d+)\s*(KB|MB|GB|B)?(?:\s|$)")
#: gphoto2 announces each file it writes. The path is needed to identify the
#: one that was in flight when a transfer timed out — that file is truncated.
_SAVING_AS_RE = re.compile(r"Saving file as\s+(.+?)\s*$")


class _CameraFile(NamedTuple):
    """One entry of `gphoto2 --list-files` for a single camera folder.

    `number` is gphoto2's own 1-based file number within the folder, which
    is what `--get-file` takes. Not `index`: that name is already a tuple
    method, and shadowing it on a NamedTuple is a trap. `size_kb` is None when the line carried no
    parseable size — treated everywhere as "unknown", never as "zero".
    """

    number: int
    name: str
    size_kb: int | None


class _FetchResult(NamedTuple):
    """Outcome of one `gphoto2 --get-*` invocation into the staging area.

    `timeout` is carried rather than raised so the caller can still place
    the files that did arrive — a timeout that threw away a half-finished
    transfer would make a card too big for the budget impossible to import
    at all, however many times it is retried.
    """

    saved: int
    skipped: int
    errors: list[str]
    last_saved: str | None
    timeout: subprocess.TimeoutExpired | None


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

    Three things prevent that now:

    1. The destination tag carries the camera's identity: a sanitised model
       plus, when the body reports one, its serial number. The tag is
       derived only from things that are stable across unplug/replug (never
       from the gphoto2 port, which is reassigned), so re-running into the
       same destination is still a cheap idempotent resume.
    2. On *both* paths, an existing destination file is only ever treated as
       "already copied" when it still looks like the same file — equal size
       and equal first/last 8 KB. Anything else is written under a distinct
       `-2` name and reported; nothing is overwritten, nothing is dropped.
    3. Nothing is written straight into the destination any more. The PTP
       path downloads into a private per-run staging directory that cannot
       collide with anything, and every file is then moved into place
       through the same never-overwrite decision the MSC path uses. gphoto2's
       `--skip-existing` is still passed, but it can only ever fire against
       files this same run just downloaded into that private directory, so
       it is no longer load-bearing for correctness.

    Two bodies of the same model that report no serial number still share a
    tag, and therefore a destination directory. That is now handled rather
    than merely documented: before skipping files that a destination already
    appears to hold, a weak-identity camera is asked for a small sample of
    them and the bytes are compared (`_probe_body_identity`). A second body
    fails that check, so all of its files are fetched and land beside the
    first body's as `DSC_0001-2.NEF`.

    What is still *not* guaranteed:

    - The sample is bounded (`PROBE_SAMPLE_SIZE`). When a folder holds more
      candidates than that, a second body whose sampled files happen to be
      byte-identical to the first body's — same names, same sizes, same
      content — is not detected, and its differing files in that folder are
      skipped. When a folder holds no more candidates than the sample size,
      the check is exhaustive and there is no hole.
    - The cheap skip rests on the size `gphoto2 --list-files` reports
      (kilobyte granularity, ±1 KB) plus the sample above. Content is not
      compared for every file because on PTP reading one byte of a file
      means downloading all of it.
    - When `--list-files` cannot be read or parsed, the folder falls back to
      a full `--get-all-files` pull into staging on every run. That is
      correct — placement still compares content — but it is not a cheap
      resume, and the reason is written to the progress log.
    - Two cameras that report the *same* serial number (a firmware bug)
      would both be trusted as unique and skip the sample check.
    - Files are fetched by gphoto2's per-folder file number, which comes
      from the listing. Shooting onto the card *during* an import can shift
      those numbers, so the wrong subset gets fetched. Nothing is lost or
      misfiled by it — each file still arrives under its own name and is
      placed by content — but files that were missed are reported by name
      (`_report_missing`) and need another run.
    """

    DOWNLOAD_TIMEOUT_DEFAULT = 3600
    LIST_FOLDERS_TIMEOUT = 30
    LIST_FILES_TIMEOUT = 60
    NUM_FILES_TIMEOUT = 30
    SERIAL_TIMEOUT = 15
    PROGRESS_LOG_NAME = ".import.log"
    #: Private per-run download area under the destination. Dot-prefixed so
    #: darktable's own folder import ignores it, and excluded from every
    #: file count this module makes.
    STAGING_DIR_NAME = ".import-staging"
    #: How many already-present-looking files a weak-identity camera is
    #: asked to re-send so their bytes can be compared with what is already
    #: in the destination. Small on purpose: this is the price of every
    #: resume for a body with no serial number. Three is enough to catch a
    #: different body while costing about one RAW file per folder, because
    #: the sample is the smallest, the median and the largest candidate.
    PROBE_SAMPLE_SIZE = 3
    #: `--list-files` reports sizes in whole kilobytes, and different builds
    #: round differently, so sizes within this many KB count as equal.
    SIZE_TOLERANCE_KB = 1
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
    last_detect_warning: str | None = None

    def _detect_cameras(self) -> list[dict[str, str]]:
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

        cameras: list[dict[str, str]] = []
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

    def _list_image_folders(self, model: str, port: str) -> list[str]:
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

        leaves: list[str] = []
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

    def _count_files_in_folder(self, model: str, port: str, src_folder: str) -> int | None:
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
    def _parse_size_kb(rest: str) -> int | None:
        """Pull the file size out of the tail of a `--list-files` line.

        Args:
            rest: everything after the filename, e.g. "rd  9863 KB image/x-nikon-nef".

        Returns:
            Size in whole kilobytes, or None when the line carries no
            parseable number. None means "unknown", and every caller treats
            unknown as "cannot be used to authorise a skip on its own".
        """
        match = _LIST_SIZE_RE.search(rest)
        if not match:
            return None
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "KB":
            return value
        if unit == "MB":
            return value * 1024
        if unit == "GB":
            return value * 1024 * 1024
        return value // 1024

    def _list_files_in_folder(
        self, model: str, port: str, src_folder: str
    ) -> list[_CameraFile] | None:
        """Enumerate one camera folder's files with their sizes.

        This is what replaced `--skip-existing` as the resume mechanism: it
        lets us decide *ourselves*, before transferring anything, which
        files the destination already holds — and, unlike gphoto2's
        path-only compare, the decision can look at the size and (via
        `_probe_body_identity`) at the bytes.

        Deliberately total, like `_probe_serial`: every failure mode
        degrades to None and the caller falls back to pulling the whole
        folder into staging, which is slower but still cannot lose a file.

        Returns:
            One `_CameraFile` per line parsed, in gphoto2's own order, or
            None when the listing could not be obtained or yielded nothing.
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
                    "--list-files",
                ],
                capture_output=True,
                text=True,
                timeout=self.LIST_FILES_TIMEOUT,
                env=env,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None

        if result.returncode != 0:
            return None

        entries: list[_CameraFile] = []
        for line in result.stdout.splitlines():
            match = _LIST_FILE_LINE_RE.match(line.rstrip())
            if not match:
                continue
            entries.append(
                _CameraFile(
                    number=int(match.group(1)),
                    name=match.group(2).strip(),
                    size_kb=self._parse_size_kb(match.group(3)),
                )
            )
        return entries or None

    @staticmethod
    def _range_expression(indices: list[int]) -> str:
        """Collapse gphoto2 file numbers into its `--get-file` RANGE syntax.

        Args:
            indices: 1-based file numbers, any order.

        Returns:
            e.g. "1-3,7,9-10". Empty string for an empty selection.
        """
        ordered = sorted(set(indices))
        if not ordered:
            return ""
        spans: list[str] = []
        start = previous = ordered[0]
        for value in ordered[1:]:
            if value == previous + 1:
                previous = value
                continue
            spans.append(str(start) if start == previous else f"{start}-{previous}")
            start = previous = value
        spans.append(str(start) if start == previous else f"{start}-{previous}")
        return ",".join(spans)

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

    def _probe_serial(self, model: str, port: str) -> str | None:
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

    @staticmethod
    def _identity_is_body_unique(identity: str) -> bool:
        """Does this identity pick out one physical body, or just a model?

        A serial number does; a model name does not — every other
        `Nikon_D850` in the world resolves to the same tag, and two of them
        imported into one destination share a directory. This is the switch
        that decides whether an "already there" file may be skipped on the
        strength of its name and size alone, or whether the camera has to
        prove it with bytes first (`_probe_body_identity`).

        Args:
            identity: `_camera_identity` output.

        Returns:
            True only when the identity carries an `sn_<serial>` component.
        """
        return bool(_IDENTITY_SERIAL_RE.search(identity or ""))

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

    @staticmethod
    def _write_log(progress_log: TextIO | None, text: str) -> None:
        """Append one line to the progress log, if there is one."""
        if progress_log is None:
            return
        progress_log.write(text if text.endswith("\n") else text + "\n")
        progress_log.flush()

    def _run_gphoto2_get(
        self,
        model: str,
        port: str,
        src_folder: str,
        selection: list[str],
        filename_pattern: str,
        timeout_seconds: int,
        progress_log: TextIO | None = None,
        expected_total: int | None = None,
        saved_offset: int = 0,
    ) -> "_FetchResult":
        """Run one gphoto2 transfer and stream its progress.

        `selection` is either `["--get-all-files"]` or
        `["--get-file", "1-3,7"]`; everything else about the invocation is
        identical, so both routes get the same locale pinning, the same
        streamed progress and the same gvfs-lock diagnosis.

        `--skip-existing` is still passed, but `filename_pattern` always
        points into a private per-run staging directory, so it can only
        fire against a file this same run just wrote there. It is a guard
        against gphoto2 stopping to ask "overwrite?" on stdin, not a
        correctness mechanism — correctness is `_place_staged_files`.

        Streams stdout via Popen + reader threads so per-file progress is
        written to `progress_log` as the transfer happens (the user can
        `tail -f` the log file in another terminal). `--filename %f.%C`
        preserves the file extension.

        Args:
            model: gphoto2 model string.
            port: gphoto2 port string.
            src_folder: camera folder to pull from.
            selection: the "which files" flags, see above.
            filename_pattern: gphoto2 `--filename` pattern, inside staging.
            timeout_seconds: subprocess timeout for this transfer.
            progress_log: open text file handle for progress lines.
            expected_total: if known, formats progress as "(N/total)".
            saved_offset: number already reported for this folder, so a
                probe transfer followed by the main one keeps counting up.

        Returns:
            `_FetchResult`. A timeout is *returned*, not raised, so the
            caller can still place the files that did arrive before it
            propagates the failure.

        Raises:
            DarktableMCPError: gphoto2 missing or camera locked by another
                process (gvfs etc.).
        """
        cmd = [
            "gphoto2",
            "--camera",
            model,
            "--port",
            port,
            "--folder",
            src_folder,
            *selection,
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
        stderr_buf: list[str] = []
        last_saved: list[str] = []

        def _consume_stdout() -> None:
            stream = proc.stdout
            if stream is None:
                return
            try:
                for line in stream:
                    if "Saving file as" in line:
                        counters["saved"] += 1
                        match = _SAVING_AS_RE.search(line)
                        if match:
                            last_saved.append(match.group(1))
                        if progress_log is not None:
                            ts = datetime.now().strftime("%H:%M:%S")
                            done = saved_offset + counters["saved"]
                            if expected_total:
                                prefix = f"[{ts}] ({done}/{expected_total}) "
                            else:
                                prefix = f"[{ts}] ({done}) "
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

        timeout_exc: subprocess.TimeoutExpired | None = None
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            timeout_exc = exc
            proc.kill()
            t_out.join(timeout=2)
            t_err.join(timeout=2)

        if timeout_exc is None:
            t_out.join(timeout=5)
            t_err.join(timeout=5)

        saved = counters["saved"]
        skipped = counters["skipped"]
        returncode = proc.returncode if proc.returncode is not None else 0

        errors: list[str] = []
        if returncode != 0 and timeout_exc is None:
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

        return _FetchResult(
            saved=saved,
            skipped=skipped,
            errors=errors,
            last_saved=last_saved[-1] if last_saved else None,
            timeout=timeout_exc,
        )

    @classmethod
    def _dest_twins(cls, folder_dest: Path, name: str) -> list[Path]:
        """Every destination file that could already be a copy of `name`.

        A previous run may have parked a same-named-but-different photo as
        `DSC_0001-2.NEF`, so "is this file already here?" has to consider
        the whole `-N` family, not just the camera's own name. Without that,
        the second body would re-fetch its whole card on every resume and
        grow a new `-3`, `-4`, ... each time.

        The scan stops at the first gap because `_never_overwrite_target`
        allocates the suffixes densely. A user who deletes `-2` but keeps
        `-3` hides the latter from this check, which costs a redundant copy,
        never a lost one.
        """
        twins: list[Path] = []
        primary = folder_dest / name
        if primary.exists():
            twins.append(primary)
        for index in range(2, cls.MAX_DISTINCT_SUFFIX + 1):
            candidate = folder_dest / cls._distinct_name(name, index)
            if not candidate.exists():
                break
            twins.append(candidate)
        return twins

    @classmethod
    def _destination_holds(cls, folder_dest: Path, entry: _CameraFile) -> bool:
        """Does the destination look like it already holds this card file?

        "Looks like" is the honest word: this is a name plus a kilobyte-
        granular size compare, which is all that can be known about a PTP
        file without downloading it. It is only ever used to *propose* a
        skip; for a camera whose identity is not unique to one body that
        proposal still has to survive `_probe_body_identity`.
        """
        twins = cls._dest_twins(folder_dest, entry.name)
        if not twins:
            return False
        if entry.size_kb is None:
            # No size to compare: the name is all we have, which is exactly
            # what gphoto2's --skip-existing used to decide on.
            return True
        for twin in twins:
            try:
                if abs(twin.stat().st_size // 1024 - entry.size_kb) <= cls.SIZE_TOLERANCE_KB:
                    return True
            except OSError:
                continue
        return False

    @classmethod
    def _probe_sample(cls, present: list[_CameraFile]) -> list[_CameraFile]:
        """Pick the files a weak-identity camera must re-send to prove itself.

        Smallest, median and largest by reported size: the smallest keeps
        the check cheap, the largest and the median keep it from being
        fooled by a folder full of identically-sized sidecars. Deterministic
        so two runs of the same card probe the same files.
        """
        ordered = sorted(present, key=lambda e: (e.size_kb if e.size_kb is not None else 0, e.name))
        if len(ordered) <= cls.PROBE_SAMPLE_SIZE:
            return ordered
        picks = {0, len(ordered) // 2, len(ordered) - 1}
        return [ordered[i] for i in sorted(picks)]

    def _probe_body_identity(
        self,
        model: str,
        port: str,
        src_folder: str,
        present: list[_CameraFile],
        folder_dest: Path,
        probe_dir: Path,
        timeout_seconds: int,
        progress_log: TextIO | None = None,
    ) -> tuple[bool, dict[str, Path], list[str]]:
        """Ask the camera to prove that the destination holds *its* photos.

        This is the fix for the one hole the identity tag cannot close. Two
        bodies of the same model that report no serial number resolve to the
        same tag and therefore the same destination directory, and no name,
        size, folder path or port can tell them apart — the port is
        reassigned on every re-plug, and the collision usually happens
        across two runs (two imports into the same
        `~/Pictures/import-<today>/`), not within one. The only thing that
        can distinguish them is the bytes, and on PTP getting the bytes
        means downloading the file. So download a bounded sample of them.

        Args:
            model: gphoto2 model string.
            port: gphoto2 port string.
            src_folder: camera folder being imported.
            present: the listed files the destination appears to hold.
            folder_dest: destination subdirectory for this camera folder.
            probe_dir: private directory the sample is fetched into.
            timeout_seconds: budget for the sample transfer.
            progress_log: progress log, for the verdict line.

        Returns:
            (verified, fetched, errors). `verified` True means every sampled
            file matched something already in the destination, so the whole
            `present` set may be skipped without transferring it. False
            means at least one did not — the destination belongs to another
            body (or holds truncated files), and the caller must fetch
            everything. `fetched` maps filename to the already-downloaded
            copy in `probe_dir` so the caller does not pay for it twice.
        """
        sample = self._probe_sample(present)
        ranges = self._range_expression([entry.number for entry in sample])
        probe_dir.mkdir(parents=True, exist_ok=True)
        result = self._run_gphoto2_get(
            model,
            port,
            src_folder,
            ["--get-file", ranges],
            f"{probe_dir}/%f.%C",
            timeout_seconds,
        )
        fetched = {path.name: path for path in sorted(probe_dir.iterdir()) if path.is_file()}
        errors = list(result.errors)

        if not fetched:
            self._write_log(
                progress_log,
                f"   !! could not re-read a sample of {src_folder} to confirm that "
                f"{folder_dest.name}/ holds this camera's photos; fetching the "
                f"whole folder instead so nothing can be skipped by mistake",
            )
            return False, {}, errors

        unmatched = [
            name
            for name, staged in fetched.items()
            if not any(
                self._looks_like_same_file(staged, twin)
                for twin in self._dest_twins(folder_dest, name)
            )
        ]
        if unmatched:
            self._write_log(
                progress_log,
                f"   ** {folder_dest.name}/ already holds different photos under "
                f"these names ({', '.join(sorted(unmatched)[:3])}) — a second body of "
                f"the same model with no serial number. Fetching the whole folder; "
                f"nothing already there will be overwritten.",
            )
            return False, fetched, errors

        self._write_log(
            progress_log,
            f"   confirmed by sample ({', '.join(sorted(fetched))}): "
            f"{folder_dest.name}/ already holds this camera's copies of "
            f"{len(present)} file(s)",
        )
        return True, fetched, errors

    def _place_staged_files(
        self,
        stage_folder: Path,
        folder_dest: Path,
        progress_log: TextIO | None = None,
    ) -> tuple[int, int, list[str], list[Path]]:
        """Move a folder's downloaded files into the destination for keeps.

        Every move goes through `_never_overwrite_target`, the same decision
        the MSC path uses, so a name that is already taken by a *different*
        photo yields `DSC_0001-2.NEF` instead of an overwrite or a silent
        skip. Relative subdirectories are preserved, which matters for the
        `/` fallback where gphoto2's `%F` recreates the camera's own tree.

        Returns:
            (placed, skipped, messages, unplaced). `skipped` counts files
            the destination provably already had. `unplaced` are files that
            could not be moved at all; they are deliberately left in staging
            rather than discarded, and the caller reports where they are.
        """
        placed = 0
        skipped = 0
        messages: list[str] = []
        unplaced: list[Path] = []
        if not stage_folder.exists():
            return placed, skipped, messages, unplaced

        for staged in sorted(p for p in stage_folder.rglob("*") if p.is_file()):
            relative = staged.relative_to(stage_folder)
            target_dir = folder_dest / relative.parent
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                target = self._never_overwrite_target(staged, target_dir)
                if target is None:
                    skipped += 1
                    staged.unlink()
                    continue
                shutil.move(str(staged), str(target))
                placed += 1
                if target.name != staged.name:
                    note = (
                        f"{self.RENAMED_PREFIX} {folder_dest.name}/{staged.name} is a "
                        f"different file from the one already in the destination; "
                        f"both kept, the new one as {target.name}"
                    )
                    messages.append(note)
                    self._write_log(progress_log, f"** {note}")
            except OSError as exc:
                unplaced.append(staged)
                messages.append(f"{staged.name}: {exc}")
                self._write_log(progress_log, f"!! {staged.name}: {exc}")
        return placed, skipped, messages, unplaced

    def _download_one_folder(
        self,
        model: str,
        port: str,
        src_folder: str,
        destination: Path,
        timeout_seconds: int,
        progress_log: TextIO | None = None,
        expected_total: int | None = None,
        identity: str | None = None,
    ) -> tuple[int, int, list[str]]:
        """Copy one camera folder into the destination without losing a file.

        Files end up in `<destination>/<identity>_<folder tag>/`, never
        directly in `destination`: `%f` is the bare basename, so a shared
        destination would make `100NCD80/DSC_0001.NEF` and
        `101NCD80/DSC_0001.NEF` collide — and, because the folder path is
        generic across bodies of the same generation, would do the same to
        two different cameras imported into one destination.

        They get there in three steps, none of which trusts a path:

        1. `--list-files` says what the folder holds and how big each file
           is. Anything the destination already holds under that name at
           that size (including as a `-2` alternative from an earlier
           conflict) is a *candidate* to skip, and is not transferred.
        2. If the camera's identity is not unique to one body — no serial
           number — the candidates are not taken on trust: a bounded sample
           is re-downloaded and compared byte-wise with what is on disk
           (`_probe_body_identity`). A second body fails that check and its
           whole folder is fetched.
        3. Everything fetched lands in a private per-run staging directory
           and is then moved into place by `_place_staged_files`, which
           never overwrites and never skips a file it cannot recognise.

        This is what replaced trusting `--skip-existing`: gphoto2 compares
        *nothing but the target path*, decides inside its own process, and
        reports a skip that looks exactly like a success, so a second body's
        `DSC_0001.NEF` used to vanish with no error and a full-looking
        destination. `--skip-existing` is still passed (it stops gphoto2
        blocking on an interactive overwrite prompt) but it now only ever
        sees a directory this run created.

        Costs, honestly: a resume for a body that reports a serial number
        transfers nothing at all — better than before, where gphoto2 still
        walked every file. A resume for a body with no serial transfers
        `PROBE_SAMPLE_SIZE` files per folder. When `--list-files` cannot be
        parsed, or `src_folder` is "/" (the recursive fallback, where the
        listing does not describe what the pull will produce), the whole
        folder is re-fetched into staging every run and placement dedupes it
        by content — correct, but not cheap; the log says so.

        Args:
            model: gphoto2 model string.
            port: gphoto2 port string.
            src_folder: camera folder to pull.
            destination: import root; the per-folder subdirectory and the
                staging area are created underneath it.
            timeout_seconds: overall budget for this folder, covering the
                listing, the sample and the transfer.
            progress_log: open text file handle to receive timestamped
                "Saving file as ..." lines. None to disable logging.
            expected_total: if known, formats progress as "(N/total)".
            identity: camera identity from `_camera_identity`, computed once
                per camera by the caller so the serial probe is not repeated
                per folder. None means "derive it from `model` alone", which
                is the safe fallback for direct callers — never a bare
                folder tag with no camera in it, and never a claim of
                uniqueness, so the sample check stays on.

        Returns:
            Tuple of (files_saved, files_skipped, error_lines). `files_saved`
            counts files that actually landed in the destination, not lines
            gphoto2 printed.

        Raises:
            DarktableMCPError: gphoto2 missing or camera locked by another
                process (gvfs etc.).
            subprocess.TimeoutExpired: raised *after* the files that did
                arrive have been placed, so a re-run resumes from there.
        """
        ident = identity if identity is not None else self._model_tag(model)
        # One source for the directory name: the post-flight check in
        # `_download_from_camera` resolves it the same way, and a second
        # spelling here would make it count an empty directory.
        folder_dest = self._folder_dest(destination, src_folder, ident)
        folder_dest.mkdir(parents=True, exist_ok=True)

        run_dir = destination / self.STAGING_DIR_NAME / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        stage_folder = run_dir / folder_dest.name
        stage_folder.mkdir(parents=True, exist_ok=True)

        is_root_pull = self._folder_tag(src_folder) == self.ROOT_FOLDER_TAG
        pattern = f"{stage_folder}/%F/%f.%C" if is_root_pull else f"{stage_folder}/%f.%C"

        deadline = time.monotonic() + timeout_seconds
        errors: list[str] = []
        placed = 0
        skipped = 0
        carried = 0
        timed_out: subprocess.TimeoutExpired | None = None

        try:
            listing = None if is_root_pull else self._list_files_in_folder(model, port, src_folder)
            if listing is None:
                if not is_root_pull:
                    self._write_log(
                        progress_log,
                        f"   (no usable file list for {src_folder}: re-reading the whole "
                        f"folder this run; files already in the destination are "
                        f"recognised by content when they are placed)",
                    )
                selection: list[str] | None = ["--get-all-files"]
            else:
                fetch, skipped, carried, plan_errors = self._plan_folder_fetch(
                    model,
                    port,
                    src_folder,
                    listing,
                    folder_dest,
                    stage_folder,
                    run_dir / ".probe",
                    max(1, math.ceil(deadline - time.monotonic())),
                    ident,
                    progress_log,
                )
                errors.extend(plan_errors)
                if not fetch:
                    selection = None
                elif len(fetch) == len(listing):
                    selection = ["--get-all-files"]
                else:
                    selection = ["--get-file", self._range_expression(fetch)]

            if selection is not None:
                result = self._run_gphoto2_get(
                    model,
                    port,
                    src_folder,
                    selection,
                    pattern,
                    max(1, math.ceil(deadline - time.monotonic())),
                    progress_log=progress_log,
                    expected_total=expected_total,
                    saved_offset=carried,
                )
                errors.extend(result.errors)
                timed_out = result.timeout
                if timed_out is not None and result.last_saved:
                    # The file gphoto2 was writing when the clock ran out is
                    # truncated. Placing it would put a half photo in the
                    # destination under the real name, where the next run
                    # would find it and keep it forever.
                    partial = Path(result.last_saved)
                    if partial.is_file() and run_dir in partial.parents:
                        partial.unlink()
                        self._write_log(
                            progress_log,
                            f"   dropped the partially transferred {partial.name}",
                        )

            placed, placement_skipped, messages, unplaced = self._place_staged_files(
                stage_folder, folder_dest, progress_log
            )
            skipped += placement_skipped
            errors.extend(messages)

            if listing is not None:
                errors.extend(self._report_missing(listing, folder_dest, src_folder, progress_log))

            if unplaced:
                errors.append(
                    f"{self.SHORTFALL_PREFIX} {len(unplaced)} file(s) were copied off "
                    f"the camera but could not be moved into {folder_dest}; they are "
                    f"still in {stage_folder}"
                )
        finally:
            self._clear_staging(run_dir)

        if timed_out is not None:
            raise timed_out
        return placed, skipped, errors

    def _plan_folder_fetch(
        self,
        model: str,
        port: str,
        src_folder: str,
        listing: list[_CameraFile],
        folder_dest: Path,
        stage_folder: Path,
        probe_dir: Path,
        timeout_seconds: int,
        identity: str,
        progress_log: TextIO | None = None,
    ) -> tuple[list[int], int, int, list[str]]:
        """Decide which of a folder's files actually have to be transferred.

        Returns:
            (fetch_indices, skipped, carried, errors). `skipped` counts
            files the destination is trusted to hold already; `carried`
            counts files the sample check downloaded and left in staging so
            the main transfer does not pay for them twice.
        """
        present = [entry for entry in listing if self._destination_holds(folder_dest, entry)]
        if not present:
            return [entry.number for entry in listing], 0, 0, []

        present_numbers = {entry.number for entry in present}
        if self._identity_is_body_unique(identity):
            # The serial number makes this directory provably this body's.
            return (
                [entry.number for entry in listing if entry.number not in present_numbers],
                len(present),
                0,
                [],
            )

        verified, fetched, errors = self._probe_body_identity(
            model,
            port,
            src_folder,
            present,
            folder_dest,
            probe_dir,
            timeout_seconds,
            progress_log,
        )
        if verified:
            for staged in fetched.values():
                staged.unlink(missing_ok=True)
            return (
                [entry.number for entry in listing if entry.number not in present_numbers],
                len(present),
                0,
                errors,
            )

        # Another body's photos are in this directory (or the sample could
        # not be read). Nothing may be skipped on name and size alone; every
        # file is fetched and `_place_staged_files` decides by content.
        for name, staged in fetched.items():
            shutil.move(str(staged), str(stage_folder / name))
        return (
            [entry.number for entry in listing if entry.name not in fetched],
            0,
            len(fetched),
            errors,
        )

    def _report_missing(
        self,
        listing: list[_CameraFile],
        folder_dest: Path,
        src_folder: str,
        progress_log: TextIO | None = None,
    ) -> list[str]:
        """Name every listed file the destination still does not hold.

        The whole point of the module is that a photo is never lost without
        the user hearing about it. Everything else here is best effort; this
        is the check that turns "best effort" into a statement, because it
        compares what the card said it had against what is on disk after the
        transfer, per file and by name.
        """
        missing = [
            entry.name for entry in listing if not self._destination_holds(folder_dest, entry)
        ]
        if not missing:
            return []
        shown = ", ".join(missing[:5])
        if len(missing) > 5:
            shown += f", ... ({len(missing) - 5} more)"
        message = (
            f"{self.SHORTFALL_PREFIX} {len(missing)} file(s) listed in {src_folder} "
            f"are still not in {folder_dest.name}/: {shown}"
        )
        self._write_log(progress_log, f"!! {message}")
        return [message]

    def _clear_staging(self, run_dir: Path) -> None:
        """Remove this run's staging directory, keeping anything unplaced.

        `rmtree` only when the tree holds no files: a file still sitting in
        staging is a photo that came off the camera and could not be put
        anywhere, and deleting it is exactly the data loss this module
        exists to prevent. The caller has already reported where it is.
        """
        try:
            if not run_dir.exists():
                return
            if any(p.is_file() for p in run_dir.rglob("*")):
                return
            shutil.rmtree(run_dir, ignore_errors=True)
            parent = run_dir.parent
            if parent.name == self.STAGING_DIR_NAME and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:  # pragma: no cover - cleanup is best-effort
            pass

    def _count_files_on_disk(self, destination: Path) -> int:
        """Count files under the destination, recursively, ignoring the log.

        Recursive because every source folder now gets its own
        subdirectory — a flat `iterdir()` would count zero and make the
        post-flight shortfall check fire on a perfectly good import.

        The staging area is excluded: files in it have not been placed yet,
        and counting them would let a half-finished transfer paper over a
        shortfall.
        """
        if not destination.exists():
            return 0
        return sum(
            1
            for entry in destination.rglob("*")
            if entry.is_file()
            and entry.name != self.PROGRESS_LOG_NAME
            and self.STAGING_DIR_NAME not in entry.parts
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
        return Path(port[len(_MSC_PORT_PREFIX) :])

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
        basename = Path(msc_port[len(_MSC_PORT_PREFIX) :]).name
        model_words = {w.upper() for w in _MODEL_WORD_RE.findall(ptp_model)}
        mount_words = {w.upper() for w in _MODEL_WORD_RE.findall(basename)}
        return bool(model_words & mount_words)

    def _group_cameras(self, cameras: list[dict[str, str]]) -> list[list[dict[str, str]]]:
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
        groups: list[list[dict[str, str]]] = []
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

    def _never_overwrite_target(self, src: Path, sub_dest: Path) -> Path | None:
        """Decide where one incoming file may be written, or that it is present.

        Shared by both import paths — the MSC walker copies into it, the PTP
        path moves staged downloads through it — because both need the same
        one rule: never overwrite, and never skip, a file that is not
        provably the file we already have. `shutil.copy2` onto an occupied
        path destroys a photo that was already safely on disk — the exact
        failure this whole module is written to prevent — and skipping on a
        bare size match destroys it just as effectively by never copying it.

        Args:
            src: incoming file — on the card, or already downloaded into the
                per-run staging directory.
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
    ) -> tuple[int, int, list[str]]:
        """Copy DCIM-shaped files from a USB Mass-Storage card mount.

        Walks `<mount>/DCIM/<subdir>/` for image and video files and copies
        each into `<destination>/<card>_<subdir>/`. One directory per source
        folder keeps files from different folders and different cards apart,
        but a card label is not a device identity — two cards formatted in
        the same body are both `EOS_DIGITAL`, and a single-slot reader
        mounts them at the same path — so the directory is never trusted on
        its own. Every write goes through `_never_overwrite_target`, which:

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
        images: list[tuple[Path, Path]] = []
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
        errors: list[str] = []

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
                    dst = self._never_overwrite_target(src, sub_dest)
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
    ) -> tuple[int, int, list[str]]:
        """Copy all files from a camera, walking each storage folder.

        - Pre-flight: resolve this camera's identity once (one gphoto2 call
          at most, never one per folder), enumerate leaf folders + count
          expected files per folder.
        - Each folder is written to its own
          `<dest>/<camera identity>_<folder tag>/` subdirectory, so
          same-named files from different folders, different cards or
          different bodies cannot overwrite or "skip-existing" each other.
          Two bodies that share an identity share the subdirectory, and
          `_download_one_folder` keeps both sets of photos there by
          comparing bytes rather than paths.
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
          destination. `_download_one_folder` adds a sharper, per-file
          version of the same check whenever it could read a file list:
          every listed name that is still not in the destination is
          reported by name.

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
        expected_per_folder: list[int | None] = []
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
        all_errors: list[str] = []

        log = open(log_path, "a", encoding="utf-8")
        try:
            log.write(
                f"\n=== Import started " f"{datetime.now().isoformat(timespec='seconds')} ===\n"
            )
            log.write(f"Camera: {model} ({port})\n")
            log.write(f"Camera identity tag: {identity or '(none available)'}\n")
            log.write(f"Destination: {destination}\n")
            log.write(
                f"Folders: {len(folders)}; expected files: "
                f"{expected_total if expected_total else '?'}\n"
            )
            log.flush()

            for position, (folder, expected) in enumerate(zip(folders, expected_per_folder)):
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
                    f"-- Folder {folder} done: {saved} new file(s), " f"{skipped} skipped --\n"
                )
                log.flush()

            # Count only the folders belonging to this camera, so files put
            # here by another card in the same import don't mask a shortfall.
            disk_count = sum(
                self._count_files_on_disk(self._folder_dest(destination, folder, identity))
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

    def import_from_camera(self, arguments: dict[str, Any]) -> str:
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
        both paths keep both sets of photos in it — neither ever overwrites
        or skips a file it cannot recognise by content, writing the newcomer
        as `IMG_0001-2.CR3` and saying so. Per-file progress is
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
                    f"camera_port '{camera_port}' not found. " f"Detected ports: {ports}"
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
        all_errors: list[str] = []
        for entry in target_group:
            try:
                count, skipped, errors = self._download_from_camera(
                    entry["model"], entry["port"], destination, timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                if total_count == 0 and not all_errors:
                    raise DarktableMCPError(
                        f"Camera transfer timed out after {timeout_seconds} s. "
                        f"Destination {destination} holds everything that was "
                        "copied before the clock ran out. Re-run the tool to "
                        "resume — files already in the destination are "
                        "recognised and not fetched again."
                    ) from exc
                all_errors.append(f"{entry['model']} ({entry['port']}) timed out")
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
            e for e in all_errors if not e.startswith((self.SHORTFALL_PREFIX, self.RENAMED_PREFIX))
        ]

        summary_parts = [
            f"Copied {total_count} new file(s) from {sources}",
            f"Skipped {total_skipped} file(s) already present in the destination",
            f"Destination: {destination} ({disk_count} files on disk, "
            "one subdirectory per camera folder)",
            f"Progress log: {log_path}",
            f'  Tail in another terminal during long imports: tail -f "{log_path}"',
            # Copying is not importing. Point at the tool that finishes the
            # job rather than at manual GUI steps: import_batch registers the
            # destination as a film roll over the Lua bridge.
            f"Next: call import_batch on {destination} to register these "
            "photos in the darktable library (it recurses into the "
            "per-camera subdirectories).",
        ]
        if shortfalls:
            # Loud on purpose: the user is about to format the card.
            summary_parts.append("")
            summary_parts.append("!! INCOMPLETE IMPORT — some photos did not make it off the card:")
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
            summary_parts.append(f"Warning: {len(other_errors)} issue(s). First: {other_errors[0]}")
        if self.last_detect_warning:
            summary_parts.append(f"Note: {self.last_detect_warning}")
        return "\n".join(summary_parts)
