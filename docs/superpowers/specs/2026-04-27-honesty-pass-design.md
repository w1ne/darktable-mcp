# Honesty Pass: Remove Broken Library Tools

**Date:** 2026-04-27
**Iteration:** 1 of 2 (next: IPC bridge spec, separate doc)
**Goal:** The MCP tool list and the README should advertise only what actually works today.

## Context

The README's "Why some tools are parked" section states:

> `darktable-cli` deliberately does not load the user's library, so it
> cannot browse it. `darktable --lua` brings up the full GUI. That means
> there is no headless, official-API path today for library reads or
> writes.

But the same README's "Implemented tools" list still advertises four tools that depend exactly on that nonexistent path:

- `view_photos` → `lua_executor.execute_script(headless=True)` → `lua -e 'dt = require("darktable")(...)'` (does not work)
- `rate_photos` → same broken path
- `import_batch` → same broken path
- `adjust_exposure` → `darktable --lua` (one-shot script, no real preview) plus a script that calls `image.modules.exposure.exposure` which is not a real Lua API surface
- `apply_preset` → `_not_implemented` stub registered as a tool

The 2026-04-25 headless-lua-integration design assumed `lua -e 'require("darktable")(...)'` would deliver a headless library. That assumption was wrong. The proper unblocker is the IPC bridge / long-running plugin called out in the README, which is iteration 2.

This iteration removes the broken approach so the repo only contains code that works.

## Scope

**In scope:**

1. Unregister the five not-actually-working tools from the MCP server: `view_photos`, `rate_photos`, `import_batch`, `adjust_exposure`, `apply_preset`.
2. Delete the source code that backed them.
3. Delete the tests for the deleted code.
4. Update the README so it lists only tools that work.
5. Rename `darktable_mcp/tools/photo_tools.py` → `camera_tools.py` and class `PhotoTools` → `CameraTools` (after the prune the file only holds camera-import logic).

**Out of scope (iteration 2):**

- Designing or building the IPC bridge / long-running plugin.
- Re-implementing the deleted tools on top of that bridge.
- Inner-range filter (`rating_min=2, rating_max=4`) for `open_in_darktable` — defer to iteration 2 because the same bridge gives us the live filtering panel API.

## File-by-file changes

### Source files to delete

| Path | Reason |
|---|---|
| `darktable_mcp/darktable/lua_executor.py` | The broken `_execute_headless` is the only reason this exists; `_execute_with_gui` is unused after `adjust_exposure` is removed. |
| `darktable_mcp/darktable/library_detector.py` | Only `lua_executor._execute_headless` calls it. |

### Source files to modify

**`darktable_mcp/darktable/__init__.py`**

- Drop the `LuaExecutor` import and its entry in `__all__`. Keep `CLIWrapper`.

**`darktable_mcp/tools/photo_tools.py` → rename to `darktable_mcp/tools/camera_tools.py`**

- Rename file and class (`PhotoTools` → `CameraTools`).
- Drop the `LuaExecutor` import and `self.lua_executor` initialization.
- Drop `view_photos`, `rate_photos`, `import_batch`, `adjust_exposure` methods.
- Keep `__init__`, `import_from_camera`, `_detect_cameras`, `_download_from_camera`, `DOWNLOAD_TIMEOUT_DEFAULT`.
- Remove the `__init__` entirely. `CameraTools` becomes stateless (no `self.lua_executor` to hold). The `DarktableMCPError` wrap around `LuaExecutor` setup goes with it. `server.py` should drop the matching `try/except` if any.

**`darktable_mcp/server.py`**

- Update imports: `from .tools.photo_tools import PhotoTools` → `from .tools.camera_tools import CameraTools`.
- Update `self._photo_tools = PhotoTools()` → `self._camera_tools = CameraTools()` (rename the attribute as well so the codebase reads consistently).
- Remove the five `Tool(...)` registrations from the tool list: `view_photos`, `rate_photos`, `import_batch`, `adjust_exposure`, `apply_preset`.
- Remove the corresponding `_handler_map` entries.
- Remove the handler methods: `_handle_view_photos`, `_handle_rate_photos`, `_handle_import_batch`, `_handle_adjust_exposure`.
- Remove `_not_implemented` if `apply_preset` was its only caller.

**`darktable_mcp/darktable/scripts/`**

- Directory is currently empty; remove it if it exists on disk to keep the tree tidy.

### Test files

| File | Action |
|---|---|
| `tests/test_lua_executor_headless.py` | Delete entire file. |
| `tests/test_library_detector.py` | Delete entire file. |
| `tests/test_darktable.py` | Delete `TestLuaExecutor` class (lines around 12–46). Keep `TestCLIWrapper` — `CLIWrapper` is still used by `export_images`. |
| `tests/test_photo_tools.py` | Delete tests for the four removed methods. Keep tests for `import_from_camera`, `_detect_cameras`, `_download_from_camera`. Rename the file to `tests/test_camera_tools.py` to match the source rename. |
| `tests/test_integration.py` | Walk the file; delete every test whose subject is one of the five removed tools or that mocks `LuaExecutor`. Keep tests that cover `import_from_camera`, `export_images`, `extract_previews`, `apply_ratings_batch`, `open_in_darktable`. |
| `tests/test_server.py` | Drop the import-introspection assertions for `LuaExecutor` and `LibraryDetector` (around line 182–190). Drop tool-registration assertions for the five removed tools. |
| `tests/test_preview_tools.py` | Untouched. |

### README

Edit `README.md`:

- "Implemented tools" intro list (around line 9–24): drop `view_photos`, `rate_photos`, `import_batch`, `adjust_exposure`. Keep the camera, vision-rating, GUI/export, and `open_in_darktable` entries.
- "Not yet implemented" list: replace the single `apply_preset` line with a short pointer to the iteration-2 spec covering the full set of library-aware tools (view, rate, import, exposure, preset).
- "Quick test" block (around line 68–74): replace the three broken example queries with three concrete ones backed by real tools:
  - `"Pull a folder of raws off my camera"` (uses `import_from_camera`)
  - `"Extract previews from ~/Pictures/import-2026-04-26"` (uses `extract_previews`)
  - `"Open ~/Pictures/import-2026-04-26 in darktable, filtered to 5 stars"` (uses `open_in_darktable`)
- "Implemented tools" detailed section (around line 96–116): drop the four tools' detail entries.
- "Stubbed tools (registered, not implemented)" section: drop the `apply_preset` entry; the section becomes empty and is removed.
- Keep the "Why some tools are parked" section. Append one sentence: `> The iteration that builds the long-running plugin + IPC will be tracked in a separate spec under docs/superpowers/specs/.` (Plain text — no link, since the iteration-2 spec doesn't exist yet. The link goes in once iteration 2 is brainstormed.)

## Carry-forward to iteration 2

These Lua snippets were the documented API surface the deleted tools targeted. Preserved here so the IPC iteration doesn't have to rediscover them.

```lua
-- view_photos: iterate library, filter by filename + rating_min, return JSON
photos = {}
count = 0
for _, image in ipairs(dt.database) do
    if count >= limit then break end
    local include = true
    if rating_min and image.rating < rating_min then include = false end
    if include and filter_text ~= "" then
        local m = string.find(string.lower(image.filename), string.lower(filter_text))
        if not m then include = false end
    end
    if include then
        table.insert(photos, {
            id = tostring(image.id),
            filename = image.filename,
            path = image.path,
            rating = image.rating or 0,
        })
        count = count + 1
    end
end
print(dt.json.encode(photos))
```

```lua
-- rate_photos: set image.rating
for _, photo_id in ipairs(photo_ids) do
    local image = dt.database[tonumber(photo_id)]
    if image then image.rating = rating end
end
```

```lua
-- import_batch: register a directory as a film roll
local imported_files = dt.database.import(source_path, recursive)
```

The `adjust_exposure` script that was deleted referenced `image.modules.exposure.exposure`, which is not a real Lua API. The iteration-2 design must rediscover the correct path (likely `dt.styles` apply or per-history-stack manipulation) rather than carrying that snippet forward.

## Tests / acceptance

- Whole suite passes after the prune. Today the suite is 177 tests; expect a meaningful drop (deleted tests are not stubbed or skipped — they're gone).
- `grep -r LuaExecutor darktable_mcp tests` returns nothing.
- `grep -r LibraryDetector darktable_mcp tests` returns nothing.
- `darktable_mcp.server.DarktableMCPServer` lists exactly: `import_from_camera`, `export_images`, `extract_previews`, `apply_ratings_batch`, `open_in_darktable`.
- README "Implemented tools" list matches the registration list above, byte-for-byte equal in tool names.
- A reader cloning the repo, opening README, and trying every advertised tool succeeds (no advertised tool fails with "headless lua not supported").

## Iteration 2 pointer

Iteration 2 will design and build a long-running darktable + Lua plugin + Unix-socket IPC, restoring `view_photos`, `rate_photos`, `import_batch`, `adjust_exposure`, `apply_preset`, and adding the inner-range filter to `open_in_darktable`. That's a separate spec.

## Risk / rollback

- **Risk:** A claude-desktop config in the wild references one of the removed tools. The MCP client will report "tool not found" when called. Acceptable — the tool was advertised but did not work; the new failure mode is more honest than the old one.
- **Rollback:** `git revert` of the merge commit. The deleted code is preserved in git history.
