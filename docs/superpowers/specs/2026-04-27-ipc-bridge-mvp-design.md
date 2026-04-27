# IPC Bridge MVP: Live Library Tools via Long-Running Lua Plugin

**Date:** 2026-04-27
**Iteration:** 2 (MVP). Restoration of remaining tools and inner-range filter deferred to iteration 2.5+.
**Goal:** Enable `view_photos` and `rate_photos` against the user's interactive darktable session via a long-running Lua plugin and a file-based JSON request/response bridge.

## Context

Iteration 1 (the "honesty pass", spec `2026-04-27-honesty-pass-design.md`) removed five broken or stubbed tools (`view_photos`, `rate_photos`, `import_batch`, `adjust_exposure`, `apply_preset`) plus the dead `LuaExecutor`/`LibraryDetector` infrastructure they depended on. The README's "Why some tools are parked" section names the unblocker:

> The planned unblocker is a long-running darktable instance running a Lua plugin that exposes a small RPC (e.g. unix socket); the MCP server talks to that plugin. That keeps every rule above intact — official API, headless after the user already has darktable open, no DB poking.

This is iteration 2. It builds the smallest version of that unblocker that ships two real tools and proves the architecture. Restoring the remaining three parked tools (`import_batch`, `adjust_exposure`, `apply_preset`) and adding the inner-range filter case for `open_in_darktable` are deferred — once the bridge exists, each is roughly a 10-line Lua method plus a 10-line MCP handler.

## Scope

**In scope:**

1. A Lua plugin `darktable_mcp.lua` that loads into the user's interactive darktable session, runs a worker loop polling a cache directory for request files, dispatches to a method registry, and writes responses.
2. Two methods in the registry: `view_photos` and `rate_photos`. Same signatures and return shapes as the broken tools they replace (preserved in iteration 1's spec carry-forward Lua snippets).
3. A Python `Bridge` client that writes requests, polls for responses, and surfaces timeouts / missing-plugin / malformed-response errors as `BridgeError` subclasses.
4. MCP tool registrations for `view_photos` and `rate_photos` in `server.py`, wired through the bridge.
5. CLI commands: `darktable-mcp install-plugin` and `darktable-mcp uninstall-plugin`. Idempotent.
6. Tests: Lua dispatcher unit tests against a stub `dt` table; Python bridge tests against a fake plugin worker; MCP handler tests with a mocked bridge; install command filesystem tests; manual end-to-end smoke test documented in the plan.
7. A Task 0 spike before any implementation work to verify darktable's Lua worker primitive (`darktable.control.dispatch` / `dt.control.sleep` or equivalent) actually runs a non-blocking loop without freezing the GUI.

**Out of scope (future iterations):**

- `import_batch`, `adjust_exposure`, `apply_preset` — incremental adds once the bridge is proven.
- Inner-range filter (`rating_min=2, rating_max=4`) for `open_in_darktable`.
- macOS / Windows cache-dir path handling — Linux only for MVP. (`platformdirs` covers this on the Python side; the Lua side hardcodes `~/.cache/darktable-mcp/`.)
- MCP-managed background darktable instance (lifecycle option β from brainstorming). Plugin requires user's interactive session.
- Concurrent / batched requests. MVP is one-at-a-time.
- Plugin auto-update across versions.

## Architecture

```
[Claude]                                               [User's darktable]
   │                                                          │
   │  view_photos                                             │
   ▼                                                          │
[MCP server]                                                  │
   │                                                          │
   │ 1. write request-<uuid>.json                            │
   │    to ~/.cache/darktable-mcp/                           │
   │                                                          │
   │                       request-<uuid>.json ──────────────▶│ 2. plugin worker
   │                                                          │    polls dir every
   │                                                          │    ~100ms, finds
   │                                                          │    file, runs handler
   │                                                          │
   │                       response-<uuid>.json ◀─────────────│ 3. writes result;
   │                                                          │    deletes request
   │ 4. polls for response,                                  │
   │    reads, deletes both                                   │
   ▼                                                          │
[Claude gets result]                                          │
```

### Components

**1. Lua plugin — `darktable_mcp/lua/darktable_mcp.lua`**

Lives inside the Python package; copied into `~/.config/darktable/lua/` by `install-plugin`. Loaded by darktable when `~/.config/darktable/luarc` does `require "darktable_mcp"`.

Responsibilities:
- On load, create the cache directory if missing, log readiness via `darktable.print_log`, spawn the worker via `darktable.control.dispatch(worker_loop)` (exact primitive confirmed by Task 0 spike).
- `worker_loop` is `while true do scan_dir(); dt.control.sleep(100); end`.
- `scan_dir` walks `request-*.json`, calls `handle(req)`, writes `response-<uuid>.json` atomically (via `.tmp` + `os.rename`), deletes the request file.
- `handle(req)` looks up `req.method` in a method table; calls it with `req.params`; returns either `{result = ...}` or `{error = "<message>"}`.
- Every ~100 worker iterations (~10 seconds), sweep stale `request-*.json` files older than 60s — covers crashed clients.

**2. Python bridge client — `darktable_mcp/bridge/client.py`**

Single class `Bridge` with one public method `call(method, params, timeout=5.0) -> dict`. Pure file I/O; no subprocess management.

Responsibilities:
- Write `request-<uuid>.json.tmp` then `os.rename` to `request-<uuid>.json` (atomic on POSIX).
- Poll `response-<uuid>.json` every 50ms up to `timeout`.
- On response: read, delete the response file. If the response carries an `error` field, raise `BridgeError(error_message)` regardless of whether `result` is also present (error wins). Otherwise return the parsed `result` value.
- On timeout: delete the request file, raise `BridgeTimeoutError`.
- On missing plugin file (`~/.config/darktable/lua/darktable_mcp.lua` absent): raise `BridgePluginNotInstalledError` with a friendly install hint.
- On malformed response (non-JSON, missing `id`/`result`/`error`): raise `BridgeProtocolError`.

**3. MCP tool handlers — `darktable_mcp/server.py`**

Two new handlers: `_handle_view_photos`, `_handle_rate_photos`. Both:
- Pull args from `arguments`.
- Call `self.bridge.call(method, params)`.
- Translate result into MCP `TextContent`.
- Catch `BridgePluginNotInstalledError` → friendly error: `"darktable-mcp plugin not installed. Run: darktable-mcp install-plugin"`.
- Catch `BridgeTimeoutError` → friendly error: `"darktable not running, or plugin not loaded. Open darktable and try again."`.
- Catch `BridgeError` → surface the plugin's error string verbatim.

The `Bridge` instance is constructed eagerly in `DarktableMCPServer.__init__` (stateless and cheap, like `CameraTools` post-honesty-pass).

### Wire protocol

**Cache directory:** `~/.cache/darktable-mcp/` on Linux. (`platformdirs.user_cache_dir("darktable-mcp")` on the Python side; hardcoded `os.getenv("XDG_CACHE_HOME") or (os.getenv("HOME") .. "/.cache")` joined with `darktable-mcp` on the Lua side. Same path on Linux.)

**Filenames:**
- Request: `request-<uuid4>.json`
- Response: `response-<uuid4>.json` (matches the request uuid)
- Atomic write convention: `<final-name>.tmp` first, then `os.rename` to final name. Readers ignore `*.tmp`.

**Request shape:**
```json
{
  "id": "<uuid4>",
  "method": "view_photos",
  "params": {"filter": "DSC", "rating_min": 4, "limit": 50}
}
```

**Response shape (success):**
```json
{
  "id": "<uuid4>",
  "result": [
    {"id": "123", "filename": "DSC_0001.NEF", "path": "/...", "rating": 4}
  ]
}
```

**Response shape (error):**
```json
{
  "id": "<uuid4>",
  "error": "image 99999 not in library"
}
```

### Tool surface (Lua)

```lua
methods.view_photos = function(p)
  local out, count = {}, 0
  local limit = p.limit or 100
  local filter = p.filter or ""
  for _, image in ipairs(dt.database) do
    if count >= limit then break end
    if (not p.rating_min or image.rating >= p.rating_min)
       and (filter == "" or string.find(string.lower(image.filename), string.lower(filter))) then
      table.insert(out, {
        id = tostring(image.id),
        filename = image.filename,
        path = image.path,
        rating = image.rating or 0,
      })
      count = count + 1
    end
  end
  return out
end

methods.rate_photos = function(p)
  local updated = 0
  for _, photo_id in ipairs(p.photo_ids) do
    local image = dt.database[tonumber(photo_id)]
    if image then
      image.rating = p.rating
      updated = updated + 1
    end
  end
  return {updated = updated}
end
```

These mirror the Lua snippets preserved in iteration 1's spec carry-forward.

### MCP tool signatures (Python)

- `view_photos(filter?: str = "", rating_min?: int, limit?: int = 100)` → list of `{id, filename, path, rating}`.
- `rate_photos(photo_ids: list[str], rating: int)` → `"Updated N photos with R stars"`.

Same signatures and return wording as the broken versions iteration 1 removed.

### Install command

```
$ darktable-mcp install-plugin
✓ wrote ~/.config/darktable/lua/darktable_mcp.lua
✓ added 'require "darktable_mcp"' to ~/.config/darktable/luarc (was: missing)
Restart darktable to load the plugin.
```

Behavior:
- Source the plugin via `importlib.resources.files("darktable_mcp").joinpath("lua/darktable_mcp.lua").read_bytes()`.
- Write to `~/.config/darktable/lua/darktable_mcp.lua`. Creates parent dirs if missing.
- If `~/.config/darktable/luarc` exists, append `require "darktable_mcp"` if not already present (string match). If absent, create it with that single line.
- Idempotent: re-running prints "already installed" for each step.

`darktable-mcp uninstall-plugin` — symmetric: removes the plugin file, removes the require line from `luarc` (leaves other lines alone).

Both wired as Python entry points alongside the existing `darktable-mcp` MCP server entry point.

### Plugin lifecycle (steady state)

1. **Load.** `require "darktable_mcp"` runs the plugin script once at darktable startup. Script creates cache dir, logs readiness, spawns worker.
2. **Idle.** Worker loops at ~100ms cadence. When no request files present, each iteration is one `dt.control.sleep` plus a directory scan (~µs cost). GUI unaffected.
3. **Request.** MCP writes `request-<uuid>.json`. Within 100ms (avg 50ms) the worker's next scan picks it up. Plugin runs `handle(req)`, writes `response-<uuid>.json`, deletes the request file. Total round-trip latency: 50–250ms.
4. **Cleanup sweep.** Every ~10s the worker also purges `request-*.json` files older than 60s (crashed clients).
5. **Shutdown.** Plugin has no teardown hook. When darktable exits, the Lua thread dies. Stale files in cache get cleaned up on the next plugin load (or by the periodic sweep if dt restarts within 60s — first sweep cycle after restart catches them).

## Error handling

| Failure | MCP user-facing message |
|---|---|
| Plugin file missing | `darktable-mcp plugin not installed. Run: darktable-mcp install-plugin` |
| Plugin installed but darktable not running (timeout, no response within 5s) | `darktable not running, or plugin not loaded. Open darktable and try again.` |
| Plugin returned `{"error": "..."}` | `Plugin error: <message>` |
| Response not valid JSON or missing fields | `Bridge protocol error: <details>` (this is a bug, log full payload) |
| Cache dir not writable | `Cache directory <path> not writable: <errno>` |

## Testing strategy

**Lua plugin (Lua interpreter, no darktable):**
- `tests/lua/test_dispatcher.lua` exercises the method registry with a stub `dt` table that mocks `dt.database`. Run via `lua tests/lua/test_dispatcher.lua` from `pytest` using `subprocess.run`. Asserts request → response shape.

**Python `Bridge` client:**
- `tests/test_bridge.py`. A `threading.Thread` fixture acts as a fake plugin worker — watches the cache dir, writes a canned response when a request appears. Cases: happy path, timeout, malformed response, error response, missing plugin file, cache dir not writable.

**MCP handlers:**
- `tests/test_server.py` additions. Patch `Bridge.call` to return canned values / raise. Cases: success path, plugin-not-installed error, timeout error, plugin error.

**Install command:**
- `tests/test_install_plugin.py`. Use `tmp_path` as a fake `$HOME/.config/darktable/`. Cases: clean install, idempotent re-run, existing `luarc` with other lines (line preserved), uninstall removes only the require line.

**End-to-end (manual):**
- Documented in the plan. Step list: install plugin → open darktable on a populated library → run `view_photos` via Claude Desktop → verify results match what darktable shows → run `rate_photos` on one image → verify rating change visible in lighttable.

## Risks

**R1: Lua worker primitive may not exist or may block the GUI.**
*Mitigation:* Task 0 spike before any implementation. Write a minimal plugin (~10 lines) that runs `while true do dt.control.sleep(100); end`. Install locally. Open darktable. Verify GUI stays responsive (zoom, pan, switch views) for 5 minutes. If it fails, escalate — likely fallback is keyboard-shortcut wake-up (Section 1 architecture changes; protocol/install/tests survive).

**R2: `xmp:Rating` set via `image.rating = N` may buffer in darktable's in-memory state without writing to XMP until export/save.**
*Mitigation:* Spike-verify by setting a rating via the bridge, quitting darktable, re-opening, confirming the rating persists. If darktable buffers writes, document as known behavior ("changes visible immediately in the open session; persisted on next normal darktable save"). Acceptable for MVP.

**R3: Plugin-side directory scan on every poll has filesystem cost.**
*Mitigation:* Cache dir lives in `~/.cache` (typically tmpfs or fast SSD). 100ms cadence × 1 dir read of usually-empty dir = negligible. If it ever shows up in profiling, lower to 200ms or use `inotify` via `os.execute` on a helper. Not a launch concern.

**R4: User's darktable Lua API version might lack a needed primitive on older releases.**
*Mitigation:* Document minimum darktable version (4.0+) in the README's Requirements section. The existing Requirements line already says 4.0+; spike will confirm the worker primitive is available there.

**R5: `os.rename` on Windows is not always atomic across drives.**
*Mitigation:* MVP is Linux only. Document.

**R6: Multiple MCP server instances writing to the same cache dir.**
*Mitigation:* Each request has a uuid; collisions are statistically impossible. The bridge does not assume exclusive cache-dir ownership. No mitigation needed.

## Acceptance

- `darktable-mcp install-plugin` writes the Lua file and `luarc` line idempotently.
- With darktable open, `view_photos` returns a JSON list of images matching the filter/rating criteria, identical in shape to what the iteration-1-removed tool advertised.
- With darktable open, `rate_photos` updates the in-memory rating of named images and the change is visible in the lighttable.
- With darktable closed, both tools return the friendly "open darktable and try again" message — not a crash, not a 30-second hang.
- Without the plugin installed, both tools return the friendly "run install-plugin" message.
- Suite is green. New tests cover: bridge happy path, bridge timeout, bridge error, plugin-missing detection, install idempotence, dispatcher method routing, dispatcher unknown-method handling.
- Manual end-to-end smoke test documented in the plan passes on the maintainer's Linux machine.

## Iteration boundary

Iteration 2 ends with: bridge architecture proven, two real tools shipped, Task 0 spike's findings recorded in the plan or the README so iteration 2.5 can build on them.

Iteration 2.5 (separate spec/plan): add `import_batch`, `adjust_exposure`, `apply_preset` plus the inner-range filter for `open_in_darktable`. Each is incremental — one Lua method, one MCP handler, one set of tests.
