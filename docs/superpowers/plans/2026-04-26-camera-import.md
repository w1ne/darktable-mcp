# Camera Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single MCP tool `import_from_camera` that detects a connected camera (libgphoto2), copies its files to a local directory, and registers them with darktable's library.

**Architecture:** Two private helpers on `PhotoTools` wrap the `gphoto2` CLI (camera detection + file transfer). One public method orchestrates: detect → select → download → reuse the existing `dt.database.import()` Lua mechanism. One new tool entry and async handler in `server.py`. No new modules, no new classes.

**Tech Stack:** Python 3.8+, gphoto2 CLI (libgphoto2), darktable Lua API, pytest, unittest.mock.

---

## File Structure

**Modify:**
- `darktable_mcp/tools/photo_tools.py` — add `_detect_cameras`, `_download_from_camera`, `import_from_camera`
- `darktable_mcp/server.py` — register `import_from_camera` tool + add `_handle_import_from_camera`
- `tests/test_photo_tools.py` — add `TestPhotoToolsImportFromCamera` test class
- `tests/test_server.py` — add `test_import_from_camera_handler`

**Create:** none.

---

### Task 1: `_detect_cameras` Helper

**Files:**
- Modify: `darktable_mcp/tools/photo_tools.py` (add helper to `PhotoTools`)
- Test: `tests/test_photo_tools.py`

- [ ] **Step 1: Write failing test for parsing gphoto2 --auto-detect output**

Add to `tests/test_photo_tools.py`:

```python
class TestPhotoToolsDetectCameras:
    """Tests for PhotoTools._detect_cameras helper."""

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_one_camera(self, mock_run, _mock_executor):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
                "Nikon DSC D800E                usb:002,002\n"
            ),
            stderr="",
        )
        tools = PhotoTools()
        cameras = tools._detect_cameras()
        assert cameras == [{"model": "Nikon DSC D800E", "port": "usb:002,002"}]

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_none(self, mock_run, _mock_executor):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
            ),
            stderr="",
        )
        tools = PhotoTools()
        assert tools._detect_cameras() == []

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_multiple(self, mock_run, _mock_executor):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
                "Nikon DSC D800E                usb:002,002\n"
                "Canon EOS R5                   usb:003,004\n"
            ),
            stderr="",
        )
        tools = PhotoTools()
        cameras = tools._detect_cameras()
        assert len(cameras) == 2
        assert cameras[0]["model"] == "Nikon DSC D800E"
        assert cameras[1]["port"] == "usb:003,004"

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_detect_cameras_gphoto2_missing(self, mock_run, _mock_executor):
        mock_run.side_effect = FileNotFoundError("gphoto2")
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="gphoto2 not installed"):
            tools._detect_cameras()
```

Make sure these imports are present at the top of `tests/test_photo_tools.py` (add only what's missing):
```python
from unittest.mock import Mock, patch
import pytest
from darktable_mcp.tools.photo_tools import PhotoTools
from darktable_mcp.utils.errors import DarktableMCPError
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_photo_tools.py::TestPhotoToolsDetectCameras -v`
Expected: FAIL with `AttributeError: 'PhotoTools' object has no attribute '_detect_cameras'`

- [ ] **Step 3: Implement `_detect_cameras` and add subprocess import**

Add to top of `darktable_mcp/tools/photo_tools.py`:
```python
import subprocess
```

Add to `PhotoTools` class (after `__init__`, before `view_photos`):

```python
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
        return cameras
```

Also add the `re` import at the top of the file:
```python
import re
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_photo_tools.py::TestPhotoToolsDetectCameras -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/darktable-mcp
git add darktable_mcp/tools/photo_tools.py tests/test_photo_tools.py
git commit -m "feat: add _detect_cameras helper using gphoto2 --auto-detect"
```

---

### Task 2: `_download_from_camera` Helper

**Files:**
- Modify: `darktable_mcp/tools/photo_tools.py`
- Test: `tests/test_photo_tools.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_photo_tools.py`:

```python
class TestPhotoToolsDownloadFromCamera:
    """Tests for PhotoTools._download_from_camera helper."""

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_success(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=(
                "Saving file as /tmp/dest/IMG_0001.NEF\n"
                "Saving file as /tmp/dest/IMG_0002.NEF\n"
                "Saving file as /tmp/dest/IMG_0003.NEF\n"
            ),
            stderr="",
        )
        tools = PhotoTools()
        count, errors = tools._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        assert count == 3
        assert errors == []

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gphoto2"
        assert "--camera" in cmd
        assert "Nikon DSC D800E" in cmd
        assert "--port" in cmd
        assert "usb:002,002" in cmd
        assert "--get-all-files" in cmd

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_partial_failure(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(
            returncode=1,
            stdout=(
                "Saving file as /tmp/dest/IMG_0001.NEF\n"
                "Saving file as /tmp/dest/IMG_0002.NEF\n"
            ),
            stderr="ERROR: Could not download IMG_0003.NEF\n",
        )
        tools = PhotoTools()
        count, errors = tools._download_from_camera(
            "Nikon DSC D800E", "usb:002,002", tmp_path
        )
        assert count == 2
        assert any("IMG_0003" in e for e in errors)

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch("darktable_mcp.tools.photo_tools.subprocess.run")
    def test_download_creates_destination(self, mock_run, _mock_executor, tmp_path):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        target = tmp_path / "new_dir"
        tools = PhotoTools()
        tools._download_from_camera("Nikon DSC D800E", "usb:002,002", target)
        assert target.exists()
        assert target.is_dir()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_photo_tools.py::TestPhotoToolsDownloadFromCamera -v`
Expected: FAIL with `AttributeError: ... '_download_from_camera'`.

- [ ] **Step 3: Implement `_download_from_camera`**

Add `Path` and `Tuple` to imports in `darktable_mcp/tools/photo_tools.py`:
```python
from pathlib import Path
from typing import Any, Dict, List, Tuple
```

Add method to `PhotoTools` (after `_detect_cameras`):

```python
    def _download_from_camera(
        self, model: str, port: str, destination: Path
    ) -> Tuple[int, List[str]]:
        """Run gphoto2 to copy all files from a camera to destination.

        Args:
            model: gphoto2 model string (e.g. "Nikon DSC D800E").
            port: gphoto2 port string (e.g. "usb:002,002").
            destination: directory to write files into. Created if missing.

        Returns:
            Tuple of (files_downloaded, error_messages). On partial failure
            (gphoto2 non-zero exit but some files saved), returns the count
            of saved files and any stderr lines as errors.

        Raises:
            DarktableMCPError: if gphoto2 binary is not installed.
        """
        destination.mkdir(parents=True, exist_ok=True)

        cmd = [
            "gphoto2",
            "--camera",
            model,
            "--port",
            port,
            "--get-all-files",
            "--filename",
            f"{destination}/%f",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError as exc:
            raise DarktableMCPError(
                "gphoto2 not installed. Install with `apt install gphoto2`."
            ) from exc

        # Count "Saving file as ..." lines on stdout
        count = sum(
            1 for line in result.stdout.splitlines() if "Saving file as" in line
        )

        errors: List[str] = []
        if result.returncode != 0:
            errors.extend(
                line for line in result.stderr.splitlines() if line.strip()
            )

        return count, errors
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_photo_tools.py::TestPhotoToolsDownloadFromCamera -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/darktable-mcp
git add darktable_mcp/tools/photo_tools.py tests/test_photo_tools.py
git commit -m "feat: add _download_from_camera helper using gphoto2 --get-all-files"
```

---

### Task 3: `import_from_camera` Public Method

**Files:**
- Modify: `darktable_mcp/tools/photo_tools.py`
- Test: `tests/test_photo_tools.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_photo_tools.py`:

```python
class TestPhotoToolsImportFromCamera:
    """Tests for PhotoTools.import_from_camera."""

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_one_camera_default_destination(
        self, mock_detect, mock_download, mock_executor_cls, tmp_path, monkeypatch
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"}
        ]
        mock_download.return_value = (5, [])
        # Force HOME so the default destination lands inside tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_executor = mock_executor_cls.return_value
        mock_executor.execute_script.return_value = (
            "Imported 5 photos from /tmp/.../import-2026-04-26"
        )

        tools = PhotoTools()
        summary = tools.import_from_camera({})

        assert "Imported 5 photos" in summary
        assert "Nikon DSC D800E" in summary
        # Default destination must include today's date
        dest_arg = mock_download.call_args[0][2]
        assert str(tmp_path) in str(dest_arg)
        assert "import-" in dest_arg.name

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_no_cameras_raises(
        self, mock_detect, _mock_download, _mock_executor_cls
    ):
        mock_detect.return_value = []
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="No camera detected"):
            tools.import_from_camera({})

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_multiple_cameras_without_port_raises(
        self, mock_detect, _mock_download, _mock_executor_cls
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"},
            {"model": "Canon EOS R5", "port": "usb:003,004"},
        ]
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="Multiple cameras"):
            tools.import_from_camera({})

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_multiple_cameras_with_port_selects(
        self, mock_detect, mock_download, mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"},
            {"model": "Canon EOS R5", "port": "usb:003,004"},
        ]
        mock_download.return_value = (1, [])
        mock_executor_cls.return_value.execute_script.return_value = "Imported 1 photos"

        tools = PhotoTools()
        tools.import_from_camera(
            {"camera_port": "usb:003,004", "destination": str(tmp_path)}
        )

        # The selected camera's model should be passed to the download
        called_model = mock_download.call_args[0][0]
        called_port = mock_download.call_args[0][1]
        assert called_model == "Canon EOS R5"
        assert called_port == "usb:003,004"

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_invalid_port_raises(
        self, mock_detect, _mock_download, _mock_executor_cls
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"}
        ]
        tools = PhotoTools()
        with pytest.raises(DarktableMCPError, match="not found"):
            tools.import_from_camera({"camera_port": "usb:999,999"})

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_partial_copy_still_imports(
        self, mock_detect, mock_download, mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"}
        ]
        mock_download.return_value = (3, ["ERROR: file X failed"])
        mock_executor_cls.return_value.execute_script.return_value = (
            "Imported 3 photos"
        )
        tools = PhotoTools()
        summary = tools.import_from_camera({"destination": str(tmp_path)})
        assert "Imported 3 photos" in summary
        assert "Warning" in summary
        assert "1 file" in summary

    @patch("darktable_mcp.tools.photo_tools.LuaExecutor")
    @patch.object(PhotoTools, "_download_from_camera")
    @patch.object(PhotoTools, "_detect_cameras")
    def test_calls_lua_database_import(
        self, mock_detect, mock_download, mock_executor_cls, tmp_path
    ):
        mock_detect.return_value = [
            {"model": "Nikon DSC D800E", "port": "usb:002,002"}
        ]
        mock_download.return_value = (2, [])
        mock_executor = mock_executor_cls.return_value
        mock_executor.execute_script.return_value = "Imported 2 photos"

        tools = PhotoTools()
        tools.import_from_camera({"destination": str(tmp_path)})

        # Verify the Lua script that was sent calls dt.database.import
        lua_call = mock_executor.execute_script.call_args
        script = lua_call[0][0] if lua_call[0] else lua_call[1].get("script_content", "")
        # script may be passed positionally; check both
        all_args = list(lua_call.args) + list(lua_call.kwargs.values())
        assert any("dt.database.import" in str(a) for a in all_args)
        # And headless=True (Lua API path, not raw DB)
        assert lua_call.kwargs.get("headless") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_photo_tools.py::TestPhotoToolsImportFromCamera -v`
Expected: FAIL with `AttributeError: ... 'import_from_camera'`.

- [ ] **Step 3: Implement `import_from_camera`**

Add `datetime` import at top of `darktable_mcp/tools/photo_tools.py`:
```python
from datetime import date
```

Add method to `PhotoTools` (after `import_batch`, before `adjust_exposure`):

```python
    def import_from_camera(self, arguments: Dict[str, Any]) -> str:
        """Import all photos from a connected camera into darktable.

        Detects connected cameras via gphoto2, copies all files to a
        destination directory, and registers them with darktable's library
        using the official Lua API. Mirrors darktable's GUI camera import.

        Args:
            arguments: Dictionary containing:
                - destination (str, optional): target directory.
                  Default: ~/Pictures/import-YYYY-MM-DD/
                - camera_port (str, optional): gphoto2 port string,
                  required when 2+ cameras are connected.

        Returns:
            Human-readable summary string.

        Raises:
            DarktableMCPError: if no camera detected, multiple cameras
                without camera_port, invalid camera_port, or gphoto2
                missing.
        """
        camera_port = arguments.get("camera_port")
        destination_arg = arguments.get("destination")

        cameras = self._detect_cameras()

        if not cameras:
            raise DarktableMCPError(
                "No camera detected. Is the camera connected and powered on?"
            )

        if camera_port:
            matching = [c for c in cameras if c["port"] == camera_port]
            if not matching:
                ports = ", ".join(c["port"] for c in cameras)
                raise DarktableMCPError(
                    f"camera_port '{camera_port}' not found. "
                    f"Detected ports: {ports}"
                )
            camera = matching[0]
        elif len(cameras) > 1:
            listing = ", ".join(f"{c['model']} ({c['port']})" for c in cameras)
            raise DarktableMCPError(
                f"Multiple cameras detected: {listing}. "
                "Pass camera_port=... to select one."
            )
        else:
            camera = cameras[0]

        if destination_arg:
            destination = Path(destination_arg).expanduser().resolve()
        else:
            today = date.today().isoformat()
            destination = (
                Path.home() / "Pictures" / f"import-{today}"
            ).resolve()

        count, errors = self._download_from_camera(
            camera["model"], camera["port"], destination
        )

        # Register the destination directory with darktable via the Lua API
        params = {
            "source_path": str(destination),
            "recursive": True,
        }
        script = """
        local imported_files = dt.database.import(source_path, recursive)
        print("Imported " .. #imported_files .. " photos from " .. source_path)
        """
        import_output = self.lua_executor.execute_script(
            script, params=params, headless=True
        )

        summary_parts = [
            import_output.strip(),
            f"Source: {camera['model']} ({camera['port']})",
            f"Destination: {destination}",
            f"Files copied from camera: {count}",
        ]
        if errors:
            summary_parts.append(
                f"Warning: {len(errors)} file(s) failed to copy. "
                f"First error: {errors[0]}"
            )
        return "\n".join(summary_parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_photo_tools.py::TestPhotoToolsImportFromCamera -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Run full PhotoTools test suite to confirm no regression**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_photo_tools.py -v`
Expected: All previous tests + the 7 new tests PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/darktable-mcp
git add darktable_mcp/tools/photo_tools.py tests/test_photo_tools.py
git commit -m "feat: add import_from_camera tool for libgphoto2-based camera import"
```

---

### Task 4: Server Tool Registration and Handler

**Files:**
- Modify: `darktable_mcp/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing handler test**

Add to `tests/test_server.py`:

```python
@pytest.mark.asyncio
async def test_import_from_camera_handler():
    server = DarktableMCPServer()
    mock_tools = Mock()
    mock_tools.import_from_camera.return_value = (
        "Imported 5 photos from /tmp/import-2026-04-26\n"
        "Source: Nikon DSC D800E (usb:002,002)"
    )
    server._photo_tools = mock_tools

    result = await server._handle_import_from_camera(
        {"destination": "/tmp/import-2026-04-26"}
    )

    assert len(result) == 1
    assert "Imported 5 photos" in result[0].text
    assert "Nikon DSC D800E" in result[0].text
    mock_tools.import_from_camera.assert_called_once_with(
        {"destination": "/tmp/import-2026-04-26"}
    )


def test_server_registers_import_from_camera_tool():
    server = DarktableMCPServer()
    tool_names = [t.name for t in server._tool_definitions()]
    assert "import_from_camera" in tool_names
```

If `Mock` is not already imported in `tests/test_server.py`, add:
```python
from unittest.mock import Mock
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_server.py::test_import_from_camera_handler tests/test_server.py::test_server_registers_import_from_camera_tool -v`
Expected: FAIL — handler attribute missing and tool not registered.

- [ ] **Step 3: Add tool definition to `_tool_definitions` in `server.py`**

Find `_tool_definitions` in `darktable_mcp/server.py`. Add this `Tool(...)` entry to the returned list, immediately after the existing `import_batch` tool entry:

```python
            Tool(
                name="import_from_camera",
                description=(
                    "Import all photos from a connected camera into darktable. "
                    "Detects the camera via libgphoto2, copies files locally, "
                    "and registers them with the darktable library."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": (
                                "Target directory for copied files. "
                                "Default: ~/Pictures/import-YYYY-MM-DD/"
                            ),
                        },
                        "camera_port": {
                            "type": "string",
                            "description": (
                                "gphoto2 port string (e.g. 'usb:002,002'). "
                                "Required when multiple cameras are connected."
                            ),
                        },
                    },
                },
            ),
```

- [ ] **Step 4: Add handler entry to `_build_handlers`**

Find `_build_handlers` in `darktable_mcp/server.py`. Add this entry to the returned dict (after `"import_batch": self._handle_import_batch,`):

```python
            "import_from_camera": self._handle_import_from_camera,
```

- [ ] **Step 5: Add `_handle_import_from_camera` method**

Add to the `DarktableMCPServer` class (place it next to the other `_handle_*` methods, after `_handle_import_batch`):

```python
    async def _handle_import_from_camera(
        self, arguments: Dict[str, Any]
    ) -> List[TextContent]:
        try:
            result = self.photo_tools.import_from_camera(arguments)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            logger.error("import_from_camera failed: %s", e)
            return [TextContent(type="text", text=f"Error: {e}")]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/test_server.py::test_import_from_camera_handler tests/test_server.py::test_server_registers_import_from_camera_tool -v`
Expected: Both tests PASS.

- [ ] **Step 7: Run the full test suite**

Run: `cd ~/projects/darktable-mcp && ./venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS (previous 111 + 7 from Task 3 + 2 from this task = 120).

- [ ] **Step 8: Commit**

```bash
cd ~/projects/darktable-mcp
git add darktable_mcp/server.py tests/test_server.py
git commit -m "feat: register import_from_camera as MCP tool with async handler"
```

---

### Task 5: Manual Smoke Test Against the Real Camera

**Files:** none (verification only)

This is the live test. The unit tests don't prove anything against a real camera; this step does.

- [ ] **Step 1: Confirm gphoto2 is installed**

Run: `which gphoto2 && gphoto2 --version | head -1`
Expected: a path and a version line. If missing: `sudo apt install gphoto2` (Debian/Ubuntu).

- [ ] **Step 2: Confirm the camera is detected by libgphoto2**

With the Nikon D800E plugged in and powered on:

Run: `gphoto2 --auto-detect`
Expected: a row containing `Nikon DSC D800E` and a `usb:NNN,NNN` port.

- [ ] **Step 3: Confirm darktable's headless Lua API can import a directory**

Make a tiny scratch directory with a sample image first:

```bash
mkdir -p /tmp/dt-smoke
cp /usr/share/icons/hicolor/256x256/apps/darktable.png /tmp/dt-smoke/test.png 2>/dev/null \
  || cp ~/Pictures/*.{jpg,JPG,jpeg,JPEG} /tmp/dt-smoke/ 2>/dev/null \
  || echo "Bring your own sample file at /tmp/dt-smoke/test.jpg"
```

Run a one-off Lua import via the project's executor:

```bash
cd ~/projects/darktable-mcp && ./venv/bin/python -c "
from darktable_mcp.darktable.lua_executor import LuaExecutor
out = LuaExecutor().execute_script(
    'local f = dt.database.import(source_path, recursive); print(\"Imported \" .. #f)',
    params={'source_path': '/tmp/dt-smoke', 'recursive': True},
    headless=True,
)
print(out)
"
```

Expected: a line like `Imported 1`. **If this fails**, headless `dt.database.import` is not working on this system. Stop here and tell the user — the camera tool will fail the same way.

- [ ] **Step 4: Run the new tool end-to-end via the PhotoTools class**

```bash
cd ~/projects/darktable-mcp && ./venv/bin/python -c "
from darktable_mcp.tools.photo_tools import PhotoTools
print(PhotoTools().import_from_camera({}))
"
```

Expected: a multi-line summary including `Imported N photos`, `Source: Nikon DSC D800E (usb:...)`, and `Destination: /home/andrii/Pictures/import-2026-04-26/`. The destination directory should now contain the camera's files.

- [ ] **Step 5: Verify in darktable**

Open darktable. The newly imported photos should appear in the lighttable view. (They may need a `Recent collections` or filmroll selection to show.)

- [ ] **Step 6: Cleanup if needed**

If the smoke test was performed with files you don't actually want in your library, remove them in the darktable lighttable (right-click → Remove from library) and delete the `~/Pictures/import-YYYY-MM-DD/` directory.

- [ ] **Step 7: No commit**

Smoke test produces no code changes — nothing to commit. If something needed fixing, it's a regression in earlier tasks; go back and fix the relevant task.

---

## Self-Review

**1. Spec coverage:**
- ✅ `import_from_camera(destination?, camera_port?)` — Task 3
- ✅ 0/1/2+ camera handling — Task 3 tests
- ✅ gphoto2-not-installed error — Task 1 test, also surfaced via Task 2
- ✅ Default destination `~/Pictures/import-YYYY-MM-DD/` — Task 3
- ✅ Reuse `dt.database.import` headless Lua call — Task 3 (verified by `test_calls_lua_database_import`)
- ✅ Server registration + async handler — Task 4
- ✅ Live verification — Task 5

**2. Placeholder scan:** No TBD/TODO/"add appropriate" placeholders. All Steps 3 contain complete code.

**3. Type consistency:**
- `_detect_cameras` returns `List[Dict[str, str]]` — used the same way in Task 3 (`cameras[0]["port"]`).
- `_download_from_camera` returns `Tuple[int, List[str]]` — consumed in Task 3 as `count, errors`.
- `import_from_camera` returns `str`, server handler wraps in `TextContent` — consistent.

No gaps found.
