# Camera Import Design

**Date:** 2026-04-26
**Status:** Approved for planning

## Goal

Add a single MCP tool, `import_from_camera`, that mirrors darktable's GUI camera-import flow: detect a connected camera (via libgphoto2), copy its files to a local directory, and register them with the darktable library. One call, no manual mounting, no manual gphoto2 commands.

## User-facing behavior

```
import_from_camera(destination?: str, camera_port?: str) -> str
```

- **`camera_port` provided:** must match a detected port. If not, raises `DarktableMCPError` listing detected ports.
- **`camera_port` omitted, 0 cameras:** raises `DarktableMCPError` with a clear "no camera detected" message.
- **`camera_port` omitted, 1 camera:** uses it.
- **`camera_port` omitted, 2+ cameras:** raises `DarktableMCPError` listing the detected `(model, port)` pairs and prompting the user to pass `camera_port`.
- **gphoto2 not installed:** raises `DarktableMCPError` with install hint (`apt install gphoto2` on the user's distro).

`destination` defaults to `~/Pictures/import-YYYY-MM-DD/`. The directory is created if missing. Files are copied with their original camera-side filenames (`%f` pattern). On a partial copy (some files succeed, some fail), the tool still attempts to import whatever landed in `destination` and reports both copy errors and import results.

Return value: human-readable summary string, e.g. `"Imported 47 photos from Nikon DSC D800E to /home/andrii/Pictures/import-2026-04-26/"`.

## Reuse from darktable

- **Camera enumeration:** `gphoto2 --auto-detect` (parses stdout — `model` and `port` columns). Same library as darktable's GUI.
- **File transfer:** `gphoto2 --camera "<model>" --port "<port>" --get-all-files --filename "<destination>/%f"`. Same library as darktable's GUI.
- **Library registration:** `dt.database.import(destination, true)` via existing `LuaExecutor.execute_script(headless=True)`. Identical mechanism to existing `import_batch`. No raw SQLite.
- **No new logic for filename templating, dedup, or session/base directory pattern parsing.** Out of scope; default destination is good enough, override via parameter.

## Architecture

Two thin helpers added to existing `darktable_mcp/tools/photo_tools.py`:

```python
def _detect_cameras(self) -> List[Dict[str, str]]:
    # Runs `gphoto2 --auto-detect`, parses output into [{"model": ..., "port": ...}, ...]

def _download_from_camera(self, model: str, port: str, destination: Path) -> Tuple[int, List[str]]:
    # Runs `gphoto2 --get-all-files`, returns (files_downloaded, errors)
```

One new public method on `PhotoTools`:

```python
def import_from_camera(self, arguments: Dict[str, Any]) -> str:
    # Orchestrates: detect → select → download → import_batch logic
```

Tool registration and a thin async handler are added to `darktable_mcp/server.py` following the existing pattern (`_handle_import_from_camera`).

**No new module.** No `CameraManager` class. No `list_cameras` tool. ~80 LOC of production code total.

## Data flow

```
import_from_camera(destination?, camera_port?)
        │
        ▼
  _detect_cameras()        ── gphoto2 --auto-detect
        │
        ▼
  pick camera (0 → error, 1 → use it, 2+ → require camera_port)
        │
        ▼
  ensure destination dir exists
        │
        ▼
  _download_from_camera()  ── gphoto2 --get-all-files
        │
        ▼
  LuaExecutor.execute_script(           ── dt.database.import(<dest>, true)
      "dt.database.import('<dest>', true)",
      headless=True
  )
        │
        ▼
  return summary string
```

## Error handling

| Condition | Behavior |
|---|---|
| `gphoto2` binary missing | `DarktableMCPError("gphoto2 not installed; install with `apt install gphoto2`")` |
| No cameras detected | `DarktableMCPError("No camera detected. Is the camera connected and powered on?")` |
| Multiple cameras, no `camera_port` | `DarktableMCPError("Multiple cameras detected: <list>. Pass camera_port=...")` |
| `camera_port` doesn't match any detected port | `DarktableMCPError("camera_port '<x>' not found. Detected ports: <list>")` |
| `destination` is unwritable | `DarktableMCPError("Cannot write to <destination>: <oserror>")` |
| gphoto2 returns non-zero (partial or total transfer failure) | Continue: still call `dt.database.import` on whatever was copied. Summary includes `"Warning: <N> files failed to copy"` |
| `dt.database.import` fails | `DarktableLuaError` propagates as `DarktableMCPError` (existing behavior) |

## Security

- `camera_port` and the auto-detected `model`/`port` strings are passed to `subprocess.run` as **separate argv elements** (no shell, no string concat). gphoto2 itself rejects malformed port URIs.
- `destination` is canonicalised via `Path(...).expanduser().resolve()` before use; the resolved path is interpolated into the Lua import call.
- The Lua import call uses parameter injection through `LuaExecutor._generate_param_lua` (same path existing tools use), not raw f-string interpolation, so the destination string is escaped properly.

## Testing

Unit tests in `tests/test_photo_tools.py` (extending the existing `TestPhotoTools*` classes), all using `unittest.mock.patch` on `subprocess.run`:

- `test_import_from_camera_no_cameras` — gphoto2 returns "No camera found" → `DarktableMCPError`
- `test_import_from_camera_one_camera` — single camera, mocked successful download + Lua import → returns summary
- `test_import_from_camera_multiple_cameras_requires_port` — 2 cameras, no `camera_port` → `DarktableMCPError` listing both
- `test_import_from_camera_multiple_cameras_with_port` — 2 cameras, `camera_port` provided → uses correct camera
- `test_import_from_camera_invalid_port` — `camera_port` doesn't match → `DarktableMCPError`
- `test_import_from_camera_gphoto2_missing` — `subprocess.run` raises `FileNotFoundError` → `DarktableMCPError` with install hint
- `test_import_from_camera_partial_copy_failure` — gphoto2 returncode != 0 but some files copied → still imports, summary includes warning
- `test_import_from_camera_default_destination` — verifies `~/Pictures/import-YYYY-MM-DD/` is used when destination omitted
- `test_import_from_camera_custom_destination` — verifies custom path is honoured

Integration test in `tests/test_server.py`:

- `test_import_from_camera_handler` — exercises the full async handler with mocked PhotoTools

No live gphoto2 / live camera tests in CI. Manual smoke test against the user's Nikon D800E is the validation step at end of implementation.

## Out of scope

- A separate `list_cameras` tool. (If we ever want it, it's trivially `_detect_cameras` exposed.)
- Reading darktable's `session/base_directory_pattern` config to honour user's GUI-configured destination layout. (Possible follow-up; default `~/Pictures/import-YYYY-MM-DD/` is good enough now.)
- Filename pattern expansion (`$(YEAR)$(MONTH)$(DAY)/<filename>` etc.). Out of scope.
- Selective import (date range, file extension filter). Out of scope.
- Deleting files from the camera card after import. Out of scope.
- Tethered shooting. Out of scope.
- Switching `import_batch` or `rate_photos` to GUI mode. The user clarified this isn't required (Lua API in either mode is fine).
