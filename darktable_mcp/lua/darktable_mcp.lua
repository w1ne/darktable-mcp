-- darktable_mcp: long-running plugin that exposes view_photos, rate_photos,
-- tag_photo, list_collections, list_photos_in_collection (tag-backed --
-- darktable's Lua API has no separate "collection" object), and the
-- darktable.develop.* darkroom-editing bridge (open_darkroom,
-- dev_version, dev_active_modules, dev_current_image, dev_get_conf_string, dev_get_params,
-- dev_set_params, dev_preview, dev_history_count, dev_enable_module,
-- dev_add_instance, dev_get_viewport, dev_add_path_mask,
-- dev_remove_last_instance, dev_set_raster_source) to the Python MCP server via file-based JSON
-- requests.
--
-- Loaded via `require "darktable_mcp"` from ~/.config/darktable/luarc.
-- Spawns a worker via dt.control.dispatch that polls
-- ~/.cache/darktable-mcp/ for request-*.json files, dispatches them to the
-- method registry, and writes response-<uuid>.json. See:
--   docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md
--
-- The poll cadence is ADAPTIVE (see POLL_*_MS below): 100ms while requests
-- are arriving, backing off to ~1s when the session is idle. The original
-- flat 100ms poll forked an `ls` subprocess ten times a second for the whole
-- lifetime of the user's darktable session, idle or not.

local dt = require("darktable")

-- LuaFileSystem, if the host darktable's Lua has it, lets us list the cache
-- directory in-process and skip the `ls` subprocess entirely. It is NOT part
-- of darktable's guaranteed Lua environment, so this must stay optional.
local lfs_available, lfs = pcall(require, "lfs")
if not lfs_available or type(lfs) ~= "table" or type(lfs.dir) ~= "function" then
  lfs_available, lfs = false, nil
end

-- print_log is the only diagnostic channel a loaded plugin has. Guard it so a
-- stripped-down dt table (or a very old darktable) cannot crash the worker.
local function log(msg)
  if dt and dt.print_log then dt.print_log(msg) end
end

-- ---- Shell + filesystem helpers --------------------------------------------

-- Quote an arbitrary string for safe interpolation into a /bin/sh command.
--
-- Paths here come from $XDG_CACHE_HOME / $HOME and from caller-supplied
-- import paths -- all outside our control. Interpolating one raw into a shell
-- string (the old `'ls -1 "' .. dir .. '"'`) breaks on a path containing a
-- double quote and EXECUTES attacker-chosen text on a path containing a
-- backtick or $(...): double quotes do not suppress command substitution.
--
-- Single quotes do suppress everything. The only byte that cannot appear
-- inside '...' is a single quote itself, encoded as the classic '\'' dance:
-- close the quote, emit an escaped quote, reopen.
local function shell_quote(s)
  return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

-- How deep a recursive import will walk. Bounds the work on a mistyped path
-- and cannot loop: neither `find` nor the lfs walk follows symlinked dirs.
local IMPORT_MAX_DEPTH = 8

-- Mode of `path` without following a symlink, so a symlinked directory is
-- skipped rather than descended into (that is what could loop). Returns nil
-- when the filesystem module cannot answer at all.
local function _link_mode(fs, path)
  local stat = fs.symlinkattributes or fs.attributes
  if type(stat) ~= "function" then return nil end
  local ok, mode = pcall(stat, path, "mode")
  if not ok then return nil end
  return mode
end

-- Walk `dir` with a LuaFileSystem-shaped module, appending it and every
-- subdirectory to `out`.
--
-- Returns TRUE only if every directory it touched was actually read. This
-- distinction is the whole point: the previous version appended `dir` to `out`
-- as its very first statement and wrapped the listing in a bare `pcall` whose
-- result it discarded, so a root that does not exist (or cannot be read)
-- produced a one-element `out` that was indistinguishable from a real, empty
-- leaf directory. list_directories_recursive's `#out == 0` failure check was
-- therefore unreachable whenever lfs was present, and import_batch answered
-- `recursive_honoured = true, directories_imported = 1` for a path it had
-- never managed to look at.
local _walk_lfs
_walk_lfs = function(fs, dir, out, depth)
  -- Stat the root BEFORE claiming it. lfs.dir() raises on a missing path, but
  -- some stand-ins simply yield nothing, so check both: an explicit stat when
  -- the module offers one, and the listing itself failing.
  local stat = fs.attributes or fs.symlinkattributes
  if type(stat) == "function" then
    local ok, mode = pcall(stat, dir, "mode")
    if not ok or mode ~= "directory" then return false end
  end

  out[#out + 1] = dir
  if depth >= IMPORT_MAX_DEPTH then return true end

  local entries = {}
  local listed = pcall(function()
    for entry in fs.dir(dir) do
      if entry ~= "." and entry ~= ".." then entries[#entries + 1] = entry end
    end
  end)
  if not listed then return false end

  -- A subdirectory we could not read means the tree we return is a subset of
  -- the real one, so propagate that upward instead of silently truncating.
  local complete = true
  for _, entry in ipairs(entries) do
    local child = dir .. "/" .. entry
    if _link_mode(fs, child) == "directory" then
      if not _walk_lfs(fs, child, out, depth + 1) then complete = false end
    end
  end
  return complete
end

-- Return `dir` plus every subdirectory beneath it, and whether the
-- enumeration actually succeeded. `false` means we could not read the whole
-- tree, so the result is a guess or a subset -- callers must not claim
-- recursion was honoured in that case.
--
-- `fs` is an injection point for the tests: pass a LuaFileSystem-shaped table
-- to exercise the lfs branch on a host (like CI's plain lua5.4) that has no
-- real lfs. Production callers pass nothing and get the real module or the
-- `find` fallback.
local function list_directories_recursive(dir, fs)
  fs = fs or (lfs_available and lfs or nil)
  local out, ok = {}, false
  if fs then
    ok = _walk_lfs(fs, dir, out, 0)
  else
    -- `find` does not follow symlinks without -L, so this cannot loop.
    local p = io.popen("find " .. shell_quote(dir)
      .. " -maxdepth " .. IMPORT_MAX_DEPTH .. " -type d 2>/dev/null")
    if p then
      for line in p:lines() do out[#out + 1] = line end
      p:close()
    end
    -- `find` prints nothing (and exits non-zero) for a path it cannot stat,
    -- so an empty listing IS the failure signal here.
    ok = #out > 0
  end
  if #out == 0 then out = {dir} end
  table.sort(out)   -- parents before children; deterministic import order
  return out, ok
end

-- ---- JSON encode/decode (minimal, MVP-only) --------------------------------
-- darktable Lua does not bundle a JSON library reliably. Inline a tiny
-- encoder/decoder sufficient for our request/response shapes.

local json = {}

-- A sparse integer-keyed table is emitted as a dense array padded with nulls
-- (see the table branch below). Refuse to do that for pathological sparsity
-- -- {[1]=x, [1e9]=y} would otherwise build a billion-element array -- and
-- fall back to the object form instead.
local SPARSE_ARRAY_MAX_HOLES = 1000

local function encode_value(v)
  local t = type(v)
  if t == "nil" then return "null"
  elseif t == "boolean" then return v and "true" or "false"
  elseif t == "number" then
    -- JSON has no inf/nan. tostring() would emit `inf` / `nan` / `-nan`,
    -- which is not valid JSON and reaches the Python client as a
    -- BridgeProtocolError on the WHOLE response -- so one bad float would
    -- discard an otherwise good result. Emit null for the bad value instead
    -- and keep the rest of the document parseable.
    if v ~= v then return "null" end                     -- nan (nan ~= nan)
    if v == math.huge or v == -math.huge then return "null" end
    return tostring(v)
  elseif t == "string" then
    local escaped = v:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
                     :gsub('\b', '\\b'):gsub('\f', '\\f')
    -- Escape any remaining control bytes (0x00-0x1F) as \u00XX.
    escaped = escaped:gsub('[%z\1-\31]', function(c)
      return string.format('\\u%04x', string.byte(c))
    end)
    return '"' .. escaped .. '"'
  elseif t == "table" then
    -- Array vs object detection. The old test was `n == max`, which silently
    -- mis-handled sparseness: {[1]=a, [3]=b} has n=2, max=3, so it fell
    -- through to the object branch and emitted {"1":a,"3":b} -- numeric
    -- string keys, a shape the Python client never expects for a list.
    -- Be explicit instead: classify the key set, then pick a branch.
    local n, max_idx = 0, 0
    local all_array_keys = true   -- every key is a positive whole number
    for k in pairs(v) do
      n = n + 1
      if type(k) == "number" and k >= 1 and k == math.floor(k) then
        if k > max_idx then max_idx = k end
      else
        all_array_keys = false
      end
    end

    if n == 0 then
      return "[]"
    elseif all_array_keys and max_idx == n then
      -- Dense 1..n: a plain array.
      local parts = {}
      for i = 1, n do parts[i] = encode_value(v[i]) end
      return "[" .. table.concat(parts, ",") .. "]"
    elseif all_array_keys and (max_idx - n) <= SPARSE_ARRAY_MAX_HOLES then
      -- Sparse but still array-shaped, e.g. {[1]=a, [3]=b} from a list that
      -- had an element removed. Keep it an ARRAY and fill the holes with
      -- null, which is what the Python client can actually consume.
      local parts = {}
      for i = 1, max_idx do parts[i] = encode_value(v[i]) end
      return "[" .. table.concat(parts, ",") .. "]"
    else
      -- Genuine object (string keys, mixed keys, or absurdly sparse indexes).
      -- Sort by key so the encoding is deterministic -- pairs() order is not.
      local entries = {}
      for k, val in pairs(v) do
        entries[#entries + 1] = {k = tostring(k), v = val}
      end
      table.sort(entries, function(a, b) return a.k < b.k end)
      local parts = {}
      for i = 1, #entries do
        parts[i] = encode_value(entries[i].k) .. ":" .. encode_value(entries[i].v)
      end
      return "{" .. table.concat(parts, ",") .. "}"
    end
  end
  error("cannot encode value of type " .. t)
end

function json.encode(v) return encode_value(v) end

-- Minimal recursive-descent decoder. Adequate for plain JSON requests.
local function skip_ws(s, i)
  while i <= #s and (s:sub(i,i) == " " or s:sub(i,i) == "\t" or s:sub(i,i) == "\n" or s:sub(i,i) == "\r") do
    i = i + 1
  end
  return i
end

local decode_value
local function decode_string(s, i)
  assert(s:sub(i,i) == '"', "expected string at " .. i)
  i = i + 1
  local out = {}
  while i <= #s do
    local c = s:sub(i,i)
    if c == '"' then return table.concat(out), i + 1
    elseif c == "\\" then
      local esc = s:sub(i+1, i+1)
      if esc == "n" then table.insert(out, "\n"); i = i + 2
      elseif esc == "r" then table.insert(out, "\r"); i = i + 2
      elseif esc == "t" then table.insert(out, "\t"); i = i + 2
      elseif esc == "b" then table.insert(out, "\b"); i = i + 2
      elseif esc == "f" then table.insert(out, "\f"); i = i + 2
      elseif esc == '"' or esc == "\\" or esc == "/" then table.insert(out, esc); i = i + 2
      elseif esc == "u" then
        -- \uXXXX: parse 4 hex digits, emit UTF-8 bytes.
        local hex = s:sub(i+2, i+5)
        if #hex ~= 4 or not hex:match("^%x%x%x%x$") then
          error("malformed \\u escape at " .. i)
        end
        local cp = tonumber(hex, 16)
        -- Encode codepoint as UTF-8.
        if cp < 0x80 then
          table.insert(out, string.char(cp))
        elseif cp < 0x800 then
          table.insert(out, string.char(
            0xC0 + math.floor(cp / 0x40),
            0x80 + (cp % 0x40)
          ))
        else
          table.insert(out, string.char(
            0xE0 + math.floor(cp / 0x1000),
            0x80 + (math.floor(cp / 0x40) % 0x40),
            0x80 + (cp % 0x40)
          ))
        end
        i = i + 6
      else error("unsupported escape \\" .. esc)
      end
    else
      table.insert(out, c)
      i = i + 1
    end
  end
  error("unterminated string")
end

local function decode_number(s, i)
  local start = i
  if s:sub(i,i) == "-" then i = i + 1 end
  while i <= #s and s:sub(i,i):match("[%d%.eE%-%+]") do i = i + 1 end
  local num = tonumber(s:sub(start, i-1))
  if num == nil then
    -- This is the catch-all branch of decode_value, so anything that is not
    -- an object/array/string/true/false/null lands here. Returning nil made
    -- json.decode succeed with a nil result for outright garbage, which the
    -- caller then had to guess at. Fail loudly instead.
    error(string.format("invalid JSON at position %d: %q",
      start, s:sub(start, start + 15)))
  end
  return num, i
end

local function decode_array(s, i)
  assert(s:sub(i,i) == "[")
  i = i + 1
  i = skip_ws(s, i)
  local out = {}
  if s:sub(i,i) == "]" then return out, i + 1 end
  while true do
    local v
    v, i = decode_value(s, i)
    table.insert(out, v)
    i = skip_ws(s, i)
    local c = s:sub(i,i)
    if c == "," then i = i + 1; i = skip_ws(s, i)
    elseif c == "]" then return out, i + 1
    else error("expected , or ] at " .. i)
    end
  end
end

local function decode_object(s, i)
  assert(s:sub(i,i) == "{")
  i = i + 1
  i = skip_ws(s, i)
  local out = {}
  if s:sub(i,i) == "}" then return out, i + 1 end
  while true do
    local k
    k, i = decode_string(s, i)
    i = skip_ws(s, i)
    assert(s:sub(i,i) == ":", "expected : at " .. i)
    i = skip_ws(s, i + 1)
    local v
    v, i = decode_value(s, i)
    out[k] = v
    i = skip_ws(s, i)
    local c = s:sub(i,i)
    if c == "," then i = i + 1; i = skip_ws(s, i)
    elseif c == "}" then return out, i + 1
    else error("expected , or } at " .. i)
    end
  end
end

decode_value = function(s, i)
  i = skip_ws(s, i)
  local c = s:sub(i,i)
  if c == "{" then return decode_object(s, i)
  elseif c == "[" then return decode_array(s, i)
  elseif c == '"' then return decode_string(s, i)
  elseif c == "t" and s:sub(i, i+3) == "true" then return true, i + 4
  elseif c == "f" and s:sub(i, i+4) == "false" then return false, i + 5
  elseif c == "n" and s:sub(i, i+3) == "null" then return nil, i + 4
  else return decode_number(s, i)
  end
end

function json.decode(s)
  local v = decode_value(s, 1)
  return v
end

-- ---- Method registry -------------------------------------------------------

local methods = {}

-- import_batch's post-import poll budget.
--
-- This poll runs INSIDE the worker loop and blocks every other bridge request
-- for its duration. It normally exits far earlier, as soon as the count stops
-- growing.
--
-- The old ceiling was 3s, which the per-subdirectory camera layout now
-- routinely outruns: import_from_camera writes one directory per camera
-- folder/card, so a single card import can register several hundred files
-- across many film rolls and darktable's background scan takes longer than
-- 3s to register them all. The Python client budgets 120s for import_batch
-- (DEFAULT_TIMEOUTS in darktable_mcp/bridge/client.py), so ~10s is affordable.
--
-- THREE independent bounds, because the attempt count alone is not a time
-- budget. Each attempt runs count_images_under, a full linear scan of
-- dt.database (there is no index to query -- see the note above view_photos),
-- so on a 30k-image library the scans, not the sleeps, dominate. The old
-- "100 x 100ms = 10s worst case" comment counted only the sleeping and was
-- wrong by however long 100 full library scans take -- potentially minutes of
-- head-of-line blocking, past the client's own 120s budget.
--   * IMPORT_POLL_ATTEMPTS      -- ceiling on scans, so the work is bounded.
--   * IMPORT_POLL_DEADLINE_SECONDS -- real wall clock, so slow scans cannot
--     push the total past it however few attempts they represent.
--   * IMPORT_ZERO_GRACE_POLLS   -- bail out early when NOTHING has appeared.
local IMPORT_POLL_ATTEMPTS = 100
local IMPORT_POLL_INTERVAL_MS = 100
-- Wall-clock ceiling on the whole poll, sleeps AND scans included.
local IMPORT_POLL_DEADLINE_SECONDS = 10
-- Consecutive polls showing no growth before the count is called final.
-- Without this the poll stopped at the FIRST non-zero count, which under-
-- reports a trickling multi-directory import.
local IMPORT_SETTLE_POLLS = 5
-- Attempts to allow before giving up on a count that is still exactly zero.
-- Zero after ~2s is the "bad path / darktable rejected the folder" case, and
-- 80 more full library scans will not rescue it -- they only make the caller
-- wait. Anything that HAS started arriving keeps the full settle behaviour.
local IMPORT_ZERO_GRACE_POLLS = 20

-- darktable's image.path is the parent directory; image.filename is the
-- bare basename. Callers want a single absolute file path they can hand
-- straight to export_images / Read / etc., so join them once here.
local function _image_full_path(image)
  local dir = image.path or ""
  if #dir > 0 and dir:sub(-1) ~= "/" then dir = dir .. "/" end
  return dir .. (image.filename or "")
end

-- KNOWN LIMITATION: this is an unavoidable O(n) linear scan of dt.database on
-- every call. darktable's Lua API exposes no index, query or predicate over
-- the library, so there is nothing to look something up by -- the only way to
-- find images matching a filter is to touch every one of them. On a 30k-image
-- library in interpreted Lua that is genuinely slow, which is why the Python
-- client budgets 30s for `view_photos` (DEFAULT_TIMEOUTS in
-- darktable_mcp/bridge/client.py) rather than the old flat 5s that made a
-- healthy-but-slow call look like "darktable is not running".
--
-- Since the scan itself cannot be avoided, the loop below avoids every bit of
-- per-image work that can be: string.lower(filter) is hoisted out (it used to
-- be recomputed for all n images), the cheapest predicate is tested first
-- (a numeric rating compare short-circuits before any string search), and no
-- result table is constructed for excluded images.
methods.view_photos = function(p)
  p = p or {}
  local out, count = {}, 0
  local limit = p.limit or 100
  local rating_min = p.rating_min
  local filter = p.filter
  -- Hoisted: one lower() for the whole scan instead of one per image.
  local filter_lc = nil
  if filter and filter ~= "" then filter_lc = string.lower(filter) end
  -- Hoisted: skip a global + table lookup per image per predicate.
  local slower, sfind = string.lower, string.find

  local source = (p.scope == "collection") and dt.collection or dt.database
  for _, image in ipairs(source) do
    if count >= limit then break end
    -- Cheapest predicate first: a number compare costs far less than a
    -- lower() + substring search, and rejecting here skips both.
    local include = true
    if rating_min and (image.rating or 0) < rating_min then
      include = false
    end
    if include and filter_lc then
      if not sfind(slower(image.filename or ""), filter_lc, 1, true) then
        include = false
      end
    end
    if include then
      count = count + 1
      out[count] = {
        id = tostring(image.id),
        filename = image.filename,
        path = _image_full_path(image),
        sidecar = image.sidecar,
        rating = image.rating or 0,
      }
    end
  end
  return out
end

-- Full, unfiltered dump of the current collection for get_contact_sheet
-- (T?.?, contact-sheet spec): filter/sort/offset/limit all happen in Python
-- on this raw list -- view_photos already does filter+limit lua-side, but
-- contact_sheet needs total_matching (post-filter, pre-page count) and
-- offset-based pagination, which only work if the whole collection crosses
-- the bridge once rather than being re-fetched per page. dt.gui.selection()
-- is otherwise only used internally by open_darkroom (see below); here it's
-- surfaced per-image so Python's filter="selected" has something to check.
methods.get_collection_images = function(p)
  p = p or {}
  local source = (p.scope == "library") and dt.database or dt.collection
  local selected_ids = {}
  for _, img in ipairs(dt.gui.selection()) do
    selected_ids[tostring(img.id)] = true
  end
  local out = {}
  for _, image in ipairs(source) do
    table.insert(out, {
      id = tostring(image.id),
      filename = image.filename,
      path = _image_full_path(image),
      rating = image.rating or 0,
      capture_time = image.exif_datetime_taken or "",
      selected = selected_ids[tostring(image.id)] or false,
      sidecar = image.sidecar,
    })
  end
  return out
end

methods.rate_photos = function(p)
  p = p or {}
  local updated = 0
  for _, photo_id in ipairs(p.photo_ids or {}) do
    local image = dt.database.get_image(tonumber(photo_id))
    if image then
      image.rating = p.rating
      updated = updated + 1
    end
  end
  return {updated = updated}
end

-- Count images that landed anywhere under `source_path`.
--
-- Matching is by PREFIX, not equality. Each imported directory becomes its
-- own darktable film roll, so with the per-subdirectory camera layout
-- (<destination>/store_00010001_DCIM_100NCD80/...) a film's path is never
-- EQUAL to source_path. The old equality test would have counted zero for
-- every single camera import.
local function count_images_under(source_path)
  local prefix = source_path
  if prefix:sub(-1) == "/" then prefix = prefix:sub(1, -2) end
  local prefix_slash = prefix .. "/"
  local plen = #prefix_slash
  local n = 0
  for _, image in ipairs(dt.database) do
    local film = image.film
    local fp = film and film.path
    if type(fp) == "string" then
      if fp:sub(-1) == "/" then fp = fp:sub(1, -2) end
      if fp == prefix or fp:sub(1, plen) == prefix_slash then
        n = n + 1
      end
    end
  end
  return n
end

-- Wait for darktable's background scan to register the import.
--
-- Returns (count, scan_incomplete). `scan_incomplete` means the hard ceiling
-- expired while the answer was still unsettled, so `count` is a FLOOR rather
-- than a confirmed total.
--
-- WARNING: this BLOCKS the worker loop. dt.control.sleep yields to darktable,
-- not to our own scan_dir, so no other bridge request is served while it
-- runs. The bound is IMPORT_POLL_DEADLINE_SECONDS (10s) of wall clock, which
-- covers the library scans as well as the sleeps -- an attempt-count-only
-- bound does not, because each attempt is a full O(n) pass over dt.database.
-- The bridge is one-request-at-a-time by design (see the IPC bridge MVP
-- spec), so this is tolerated, but do not add more in-worker polling loops.
--
-- `deps.now` overrides the clock; the tests use it to prove the wall-clock
-- deadline fires without actually waiting 10 seconds.
local function poll_for_imported(source_path, deps)
  local now = (deps and deps.now) or os.time
  local started = now()
  local count, stable = 0, 0
  for attempt = 1, IMPORT_POLL_ATTEMPTS do
    local seen = count_images_under(source_path)
    if seen > count then
      -- Still arriving: reset the settle window rather than returning the
      -- first non-zero count, which would under-report a multi-directory
      -- card import that trickles in over several seconds.
      count, stable = seen, 0
    elseif count > 0 then
      stable = stable + 1
      if stable >= IMPORT_SETTLE_POLLS then
        return count, false     -- settled; this count is final
      end
    end
    -- Nothing has appeared at all after the grace window: this is the bad
    -- path / rejected-folder case, not a slow trickle. Return promptly with
    -- scan_incomplete rather than burning the rest of the budget on scans
    -- that have nothing to find.
    if count == 0 and attempt >= IMPORT_ZERO_GRACE_POLLS then
      return 0, true
    end
    -- Deadline check BEFORE the sleep, so a run whose scans alone have eaten
    -- the budget stops here instead of paying for one more of them.
    if now() - started >= IMPORT_POLL_DEADLINE_SECONDS then
      return count, true
    end
    if dt.control and dt.control.sleep then
      dt.control.sleep(IMPORT_POLL_INTERVAL_MS)
    else
      break                     -- no way to yield; do not spin the GUI thread
    end
  end
  -- Ceiling reached with the answer still moving (or nothing found at all).
  return count, true
end

-- Tags are darktable's only persistent, user-named grouping of photos
-- (there is no separate "collection" object in the Lua API) so
-- list_collections / list_photos_in_collection surface dt.tags as the
-- practical equivalent of Lightroom-style collections.
methods.tag_photo = function(p)
  p = p or {}
  local photo_ids = p.photo_ids or {}
  if #photo_ids == 0 then error("tag_photo: photo_ids required and non-empty") end
  local add_names = p.tags or {}
  local remove_names = p.remove_tags or {}
  if #add_names == 0 and #remove_names == 0 then
    error("tag_photo: at least one of tags or remove_tags required")
  end

  local created = {}
  local add_tags = {}
  for _, name in ipairs(add_names) do
    local tag = dt.tags.find(name)
    if not tag then
      tag = dt.tags.create(name)
      table.insert(created, name)
    end
    table.insert(add_tags, tag)
  end

  local remove_tags = {}
  for _, name in ipairs(remove_names) do
    local tag = dt.tags.find(name)
    if tag then table.insert(remove_tags, tag) end
  end

  local updated, missing_photos = 0, {}
  for _, photo_id in ipairs(photo_ids) do
    local image = dt.database.get_image(tonumber(photo_id))
    if image then
      for _, tag in ipairs(add_tags) do image:attach_tag(tag) end
      for _, tag in ipairs(remove_tags) do image:detach_tag(tag) end
      updated = updated + 1
    else
      table.insert(missing_photos, tostring(photo_id))
    end
  end

  return {updated = updated, missing_photos = missing_photos, tags_created = created}
end

-- AI assessments/notes are stored in the standard dc:description xmp field
-- (image.description in darktable's Lua API), not a custom field, so they
-- round-trip through any xmp-aware tool and show up in darktable's own
-- metadata panel like a human-written caption would.
methods.set_photo_note = function(p)
  p = p or {}
  if not p.photo_id then error("set_photo_note: photo_id required") end
  local image = dt.database.get_image(tonumber(p.photo_id))
  if not image then return {updated = 0} end
  image.description = p.note or ""
  return {updated = 1}
end

methods.get_photo_note = function(p)
  p = p or {}
  if not p.photo_id then error("get_photo_note: photo_id required") end
  local image = dt.database.get_image(tonumber(p.photo_id))
  if not image then return {found = false} end
  return {found = true, note = image.description or ""}
end

methods.list_collections = function(p)
  p = p or {}
  local filter = p.filter or ""
  local out = {}
  for _, tag in ipairs(dt.tags) do
    local include = true
    if filter ~= "" then
      local ok = string.find(string.lower(tag.name), string.lower(filter), 1, true)
      if not ok then include = false end
    end
    if include then
      table.insert(out, {name = tag.name, count = #tag})
    end
  end
  return {collections = out, count = #out}
end

methods.list_photos_in_collection = function(p)
  p = p or {}
  local name = p.collection
  if not name or name == "" then error("list_photos_in_collection: collection required") end
  local tag = dt.tags.find(name)
  if not tag then return {found = false, collection = name, photos = {}, count = 0} end

  local limit = p.limit or 1000
  local out = {}
  for _, image in ipairs(tag:get_tagged_images()) do
    if #out >= limit then break end
    table.insert(out, {
      id = tostring(image.id),
      filename = image.filename,
      path = _image_full_path(image),
      rating = image.rating or 0,
    })
  end
  return {found = true, collection = name, photos = out, count = #out}
end

methods.import_batch = function(p)
  p = p or {}
  local source_path = p.source_path
  if not source_path or source_path == "" then
    error("import_batch: source_path required")
  end
  local recursive = p.recursive
  if recursive == nil then recursive = true end

  -- RECURSION. dt.database.import(path) takes ONLY a path in darktable 5.x --
  -- there is no per-call recursion argument. darktable's own recursion is
  -- governed by a persistent user preference, which this plugin deliberately
  -- does NOT read or write:
  --   * darktable's Lua `dt.preferences` API is documented for SCRIPT-scoped
  --     preferences (register/read/write under a script namespace). Reaching
  --     a darktable core conf key such as recurse_directories through it is
  --     NOT documented, and could not be verified against a running
  --     darktable here -- the 2026-04-27 spike confirmed only that the
  --     `dt.preferences` table exists, not what it can address.
  --   * Even if it worked, temporarily flipping a persistent user preference
  --     from a background worker is its own bug: a crash or a concurrent GUI
  --     import would leave the user's setting changed behind their back.
  --
  -- So recursive = true is honoured HERE instead: enumerate the tree and
  -- import every directory in it. This is now mandatory rather than
  -- incidental, because import_from_camera writes one subdirectory per camera
  -- folder/card (camera filenames repeat across folders and across the two
  -- cards of a dual-slot body), so the photos never sit flat in the
  -- destination.
  local dirs, enumerated
  if recursive then
    dirs, enumerated = list_directories_recursive(source_path)
  else
    -- We can import just the top directory, but we cannot PREVENT darktable
    -- from recursing if the user's preference says to. So recursive = false
    -- is never something we can claim to have honoured.
    dirs, enumerated = {source_path}, false
  end

  -- The return value is heterogeneous: a dt_lua_image_t for a single file, a
  -- dt_lua_film_t (the registered film roll) for a directory. darktable scans
  -- the folder asynchronously, so the image count is not available yet.
  local table_total, saw_table, saw_other = 0, false, false
  for i = 1, #dirs do
    local r = dt.database.import(dirs[i])
    if type(r) == "table" then
      saw_table = true
      table_total = table_total + #r
    elseif r ~= nil then
      saw_other = true
    end
  end

  local count, scan_incomplete
  if saw_table and not saw_other then
    -- Stub-table return (unit tests): `#r` is the definitive count.
    count, scan_incomplete = table_total, false
  else
    count, scan_incomplete = poll_for_imported(source_path)
    -- NOTE: scan_incomplete is true both when nothing showed up at all and
    -- when images were still arriving as the ceiling expired. This replaces
    -- the old "final fallback: treat as 1", which reported imported = 1 for
    -- an import darktable never confirmed -- including a directory import
    -- that in fact found nothing.
  end

  local recursive_honoured = recursive and enumerated
  local note = nil
  if recursive and not enumerated then
    -- #dirs, not "only that path": the walk may have read part of the tree
    -- before hitting an unreadable subdirectory, and those directories WERE
    -- imported. Saying "only that path" would be a second false claim.
    note = "recursive=true requested, but " .. source_path .. " could not be "
        .. "fully enumerated -- " .. #dirs .. " director"
        .. (#dirs == 1 and "y was" or "ies were") .. " imported. Any "
        .. "subdirectory not listed was registered only if darktable's own "
        .. "recursive-import preference is enabled."
  elseif not recursive then
    note = "recursive=false could not be enforced: darktable's Lua API has no "
        .. "per-call recursion flag, and this plugin does not modify your "
        .. "recurse_directories preference. Only " .. source_path .. " was "
        .. "passed to darktable, but darktable may still have recursed into "
        .. "it if that preference is enabled."
  end

  return {
    imported = count,
    source_path = source_path,
    recursive = recursive,
    -- Whether the requested recursion mode is the one that actually happened.
    -- false => `recursive` above is what the CALLER asked for, not a
    -- description of what darktable did. Read `note`.
    recursive_honoured = recursive_honoured,
    note = note,
    -- How many directories were handed to dt.database.import.
    directories_imported = #dirs,
    -- true => the background scan had not settled before the poll ceiling.
    -- `imported` is a floor, not a confirmed total; the import may still be
    -- in progress.
    scan_incomplete = scan_incomplete,
  }
end

methods.list_styles = function(p)
  local out = {}
  for _, style in ipairs(dt.styles) do
    table.insert(out, {
      name = style.name,
      description = style.description,
    })
  end
  return {styles = out, count = #out}
end

methods.apply_preset = function(p)
  p = p or {}
  local preset_name = p.preset_name
  local photo_ids = p.photo_ids or {}
  if not preset_name or preset_name == "" then
    error("apply_preset: preset_name required")
  end
  if #photo_ids == 0 then
    error("apply_preset: photo_ids required and non-empty")
  end

  -- Linear scan to resolve preset_name (string lookup unavailable on dt.styles).
  local style = nil
  for _, s in ipairs(dt.styles) do
    if s.name == preset_name then style = s; break end
  end
  if not style then
    error(string.format("apply_preset: style %q not found (use list_styles)", preset_name))
  end

  local applied = 0
  local missed = {}
  for _, photo_id in ipairs(photo_ids) do
    local image = dt.database.get_image(tonumber(photo_id))
    if image then
      image:apply_style(style)
      applied = applied + 1
    else
      table.insert(missed, tostring(photo_id))
    end
  end
  return {applied = applied, missed = missed, preset_name = preset_name}
end

-- ---- darktable.develop.* bridge (T1.6) -------------------------------------
-- Promoted from darktable-mcp/spike/spike_methods.lua (T0.3/T1.1-T1.5 probes)
-- into the clean bridge once the underlying C API stabilized. These wrap the
-- NEW Lua namespace `darktable.develop` (src/lua/develop.c, registered in
-- src/lua/init.c) added on branch agentic-mcp: version/active_modules/
-- get_params/set_params/preview/history_count, all scalar-only for now (see
-- PLAN.md §3.1 Risk #2 -- arrays/curves and masks are out of scope here).
--
-- open_darkroom is pure-Lua (no C change): it drives the selection +
-- current_view + async-poll technique sketched by the T0.3 spike, since
-- headless darkroom entry has no mouse to resolve dt_act_on_get_main_image()
-- via hover (see common/act_on.c:_get_main_image_hover). CORRECTION
-- (2026-07-24 bugreport): this comment used to claim the spike "proved" this
-- works -- it didn't. spike/run_spike.py's own sub-goal A branch explicitly
-- anticipates open_darkroom possibly NOT switching view and treats that as a
-- valid (documented-negative) spike outcome; no pass was ever recorded. This
-- worker_loop also runs on darktable's Lua thread pool (see call.c
-- stacked_job_queue / GThreadPool), not the GTK main thread, so
-- dt.gui.current_view(view) only *schedules* the switch on the GTK context
-- (g_main_context_invoke(), control/control.c:630) rather than performing it
-- inline -- hence the poll below. But scheduling the view switch is not
-- sufficient: views/darkroom.c's try_enter() independently re-resolves the
-- target image via dt_act_on_get_main_image() (common/act_on.c:650), which
-- can silently prefer mouseover/stale active_images over the Lua selection
-- we just set, or require the image to be part of the CURRENT lighttable
-- collection filter -- see the pre-check below, added after a real-world
-- failure where the switch never happened and try_enter() gave no error
-- back to Lua at all (darkroom.c:1150, "no image to open!" is GUI-log only).

methods.open_darkroom = function(p)
  p = p or {}
  local image_id = tonumber(p.image_id)
  if not image_id then error("open_darkroom: image_id required") end
  local image = dt.database.get_image(image_id)
  if not image then error("open_darkroom: image not found: " .. tostring(image_id)) end

  dt.gui.selection({ image })

  -- Diagnostic pre-check (bugreport 2026-07-24): try_enter() in
  -- views/darkroom.c does NOT read this Lua selection directly -- it
  -- resolves the target image via dt_act_on_get_main_image()
  -- (common/act_on.c:650), which can silently prefer mouseover or stale
  -- view_manager->active_images over the selection we just set (if the
  -- "activate images by" preference, plugins/lighttable/act_on, is hover
  -- mode -- act_on.c:34), and either way requires the image to be part of
  -- the CURRENT lighttable collection filter (its selection-table fallback
  -- JOINs against memory.collected_images -- act_on.c:582,632). If any of
  -- that fails, try_enter() just logs "no image to open!" and aborts with
  -- no error back to Lua (darkroom.c:1150), which used to show up here as a
  -- silent 3000ms timeout stuck in "lighttable" with zero diagnostics.
  -- dt.gui.action_images (lua/gui.c:85) calls the exact same
  -- dt_act_on_get_images() resolution try_enter() uses, so checking it here
  -- catches all three failure modes up front instead of burning the full
  -- poll timeout on a switch that could never succeed.
  local action_ids = {}
  for _, img in ipairs(dt.gui.action_images) do
    table.insert(action_ids, img.id)
  end
  local resolves_to_target = (#action_ids == 1 and action_ids[1] == image.id)
  if not resolves_to_target then
    return {
      view = dt.gui.current_view().name,
      requested_image_id = image_id,
      selection_count = #dt.gui.selection(),
      waited_ms_for_view_switch = 0,
      action_images = action_ids,
      diagnostic = "selection did not resolve to the requested image via darktable's " ..
        "act-on resolution (dt.gui.action_images = {" .. table.concat(action_ids, ",") ..
        "}); darkroom entry would silently fail without ever switching view. Likely " ..
        "cause: the 'activate images by' preference is set to mouseover and something " ..
        "else is hovered/active, OR image " .. tostring(image_id) .. " is not part of " ..
        "the current lighttable collection filter.",
    }
  end

  -- Bugreport 2026-07-25: switching current_view to darkroom while ALREADY
  -- IN darkroom (opening a second image right after the first) does NOT
  -- reload the new image. views/view.c's dt_view_manager_switch_by_view
  -- only calls the target view's enter() -- which is what calls
  -- dt_dev_load_image() for the new image_storage.id, views/darkroom.c:3860
  -- -- when new_view != old_view (view.c:443-444: "if(new_view != old_view
  -- && new_view->enter) new_view->enter(new_view);"). try_enter() alone
  -- (which DOES re-run and sets image_storage.id) is not enough: the actual
  -- pixelpipe/history/GUI reload lives in enter(), so without a real view
  -- transition darkroom keeps rendering the previous image while this
  -- method still reports "view=darkroom" as if it had switched. Force a
  -- genuine transition by bouncing through lighttable first so enter() is
  -- guaranteed to run again for the new image.
  local bounced = false
  if dt.gui.current_view().name == "darkroom" then
    bounced = true
    dt.gui.current_view(dt.gui.views.lighttable)
    local left = dt.gui.current_view()
    local left_wait = 0
    while left.name == "darkroom" and left_wait < 3000 do
      if dt.control and dt.control.sleep then
        dt.control.sleep(100)
        left_wait = left_wait + 100
      else
        break
      end
      left = dt.gui.current_view()
    end
  end

  dt.gui.current_view(dt.gui.views.darkroom)

  local current = dt.gui.current_view()
  local waited_ms = 0
  while current.name ~= "darkroom" and waited_ms < 3000 do
    if dt.control and dt.control.sleep then
      dt.control.sleep(100)
      waited_ms = waited_ms + 100
    else
      break
    end
    current = dt.gui.current_view()
  end

  return {
    view = current.name,
    requested_image_id = image_id,
    selection_count = #dt.gui.selection(),
    waited_ms_for_view_switch = waited_ms,
    bounced_through_lighttable = bounced,
    -- Absolute file path of the opened image (dir + filename joined via the
    -- module-local _image_full_path helper, same one view_photos uses).
    -- T3.3's mask_raster needs this to feed the actual image file to the
    -- matting sidecar without a dedicated new bridge method.
    path = _image_full_path(image),
  }
end

-- Version handshake (PLAN.md §3.5): {api, dt_lua_api, min_bridge}. Callers
-- can refuse to run if the C binding is older than the bridge expects.
methods.dev_version = function(p)
  return dt.develop.version()
end

-- {op, instance, id, multi_name, enabled, has_introspection} per active
-- module on the image currently open in darkroom.
methods.dev_active_modules = function(p)
  local mods = dt.develop.active_modules()
  local out = {}
  for i, m in ipairs(mods) do
    out[i] = {
      op = m.op,
      instance = m.instance,
      id = m.id,
      multi_name = m.multi_name,
      enabled = m.enabled,
      has_introspection = m.has_introspection,
    }
  end
  -- count disambiguates the empty case (Lua {} would JSON-encode ambiguously)
  return { count = #out, modules = out }
end

-- Report the image currently open in darkroom: {has_image, id, path, filename}.
-- Lets the server resolve the source file for an image the user opened BY HAND
-- in the GUI (no open_darkroom call, so no MCP-side path cache). Returns the C
-- result verbatim; has_image=false when no darkroom image is loaded. Also adds
-- `sidecar` (2026-07-31 bugreport: export_images silently exported the WRONG
-- duplicate/version because it never told darktable-cli which .xmp to use,
-- so darktable-cli fell back to its own auto-detect of the base/version-0
-- sidecar) -- `image.sidecar` is a stock darktable Lua field (already
-- version/duplicate-aware, src/lua/image.c) the C current_image() binding
-- itself doesn't return; fetched here in pure Lua via dt.database.get_image,
-- no C change needed.
methods.dev_current_image = function(p)
  local result = dt.develop.current_image()
  if result.has_image then
    local image = dt.database.get_image(result.id)
    if image then result.sidecar = image.sidecar end
  end
  return result
end

-- Read-only passthrough to an arbitrary core dt_conf key, e.g.
-- "plugins/darkroom/lut3d/def_path" (the lut3d module's configured LUT root
-- directory -- same key the UI file-chooser widget reads). Returns the raw
-- string verbatim ("" if the key resolves to an empty default -- never nil).
methods.dev_get_conf_string = function(p)
  p = p or {}
  local key = p.key
  if not key or key == "" then error("dev_get_conf_string: key required") end
  return dt.develop.get_conf_string(key)
end

-- Typed field map for one module instance (PLAN.md §3.1): scalar fields come
-- back as {value, min, max, default}. Returns the C result verbatim --
-- {op, instance, id, fields=...} on success, {error=...} if op/instance is
-- unknown.
methods.dev_get_params = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_get_params: op required") end
  local instance = tonumber(p.instance) or 0
  return dt.develop.get_params(op, instance)
end

-- Write fields, push history, reprocess. `fields` is a plain Lua table of
-- field=value. Returns the clamp report verbatim (PLAN.md §3.2):
-- {ok, applied, clamped, unknown_fields} -- out-of-range values are clamped
-- to introspection Min/Max (never rejected) and every clamp is reported.
methods.dev_set_params = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_set_params: op required") end
  local instance = tonumber(p.instance) or 0
  local fields = p.fields
  if type(fields) ~= "table" then error("dev_set_params: fields table required") end
  return dt.develop.set_params(op, instance, fields)
end

-- Read-only blend_params snapshot (opacity, mask_mode, blend_mode). Separate
-- from dev_get_params: blend_params is a plain struct outside module
-- introspection (LUT tooling, 2026-07-26 -- see get_blend_params_cb).
methods.dev_get_blend_params = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_get_blend_params: op required") end
  local instance = tonumber(p.instance) or 0
  return dt.develop.get_blend_params(op, instance)
end

-- Write blend_params fields (opacity 0..100, blend_mode raw int,
-- enable_uniform_blend convenience bool) and commit to history. See
-- set_blend_params_cb for the enable_uniform_blend all-or-nothing contract.
methods.dev_set_blend_params = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_set_blend_params: op required") end
  local instance = tonumber(p.instance) or 0
  local fields = p.fields
  if type(fields) ~= "table" then error("dev_set_blend_params: fields table required") end
  return dt.develop.set_blend_params(op, instance, fields)
end

-- Read-only history entry count for the open image (used to confirm history
-- coalescing per §3.3 and that preview() never writes to it).
methods.dev_history_count = function(p)
  return dt.develop.history_count()
end

-- PNG of the CURRENT LIVE darkroom edit, grabbed from preview_pipe->backbuf,
-- NO DB write (PLAN.md §3.4). Returns the C result verbatim:
-- {status="ok", path, width, height} | {status="processing", stale_preview,
-- ...} | {error=...}. `path` is a container path under $XDG_CACHE_HOME
-- (/run/cache-mcp in the docker bridge); the Python server remaps
-- /run -> the bridge run_dir so the host-side MCP client can read the file.
--
-- Optional `region` = {x,y,w,h} normalized 0..1 of the visible frame, passed
-- through to dt.develop.preview(max_w, max_h, x, y, w, h) for a full-detail
-- crop render (grain/sharpen/noise inspection). Omit for the full frame.
--
-- Optional `viewport` (2026-07-31 fix, set-viewport-design follow-up): "main"
-- | "preview2" reads dev->full.pipe / dev->preview2.pipe's OWN backbuf
-- instead of the default dev->preview_pipe -- see dt.develop.preview's own
-- long doc comment in src/lua/develop.c for why this is the fix that makes
-- set_viewport's zoom actually show up in captured pixels (preview_pipe has
-- a fixed native resolution entirely decoupled from any darkroom zoom).
-- Omitted: zero change from the original preview_pipe behavior.
methods.dev_preview = function(p)
  p = p or {}
  local max_w = tonumber(p.max_w) or 0
  local max_h = tonumber(p.max_h) or 0
  local region = p.region
  local rx, ry, rw, rh
  if type(region) == "table" and region.x ~= nil and region.y ~= nil
     and region.w ~= nil and region.h ~= nil then
    rx = tonumber(region.x)
    ry = tonumber(region.y)
    rw = tonumber(region.w)
    rh = tonumber(region.h)
  end
  return dt.develop.preview(max_w, max_h, rx, ry, rw, rh, p.viewport)
end

-- Toggle a module instance on/off and commit to history (PLAN.md T1.7).
-- Many modules ship OFF by default (grain, sharpen, vignette, tonecurve,
-- ...) and produce no visible effect until enabled -- this is the missing
-- prerequisite step before set_params on those. Returns the C result
-- verbatim: {ok, op, instance, enabled} | {error=...}.
methods.dev_enable_module = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_enable_module: op required") end
  local instance = tonumber(p.instance) or 0
  local enabled = p.enabled
  if enabled == nil then enabled = true end
  return dt.develop.enable_module(op, instance, enabled and true or false)
end

-- Add a new masked/parametric instance of a module (mirrors the GUI
-- "new instance" action) -- the base step for local edits (dodge/burn on a
-- second exposure instance, a second sharpen for a specific area, etc).
-- Returns the C result verbatim: {ok, op, instance=<new multi_priority>,
-- base_instance, multi_name} | {error=...}.
-- `fields` (optional): initial param overrides applied to the new instance
-- right after duplication, via the same introspection write path as
-- dev_set_params -- see add_instance_cb's doc comment (src/lua/develop.c) for
-- why this collapses into ONE history entry instead of the duplicate's own
-- commit plus a separate later dev_set_params call. Use this for modules
-- whose reload_defaults() gives a fresh instance different defaults than the
-- base instance it was cloned from (exposure's compensate_exposure_bias/
-- compensate_hilite_pres is the motivating case) -- dt_iop_gui_duplicate's
-- copy_params=TRUE clobbers those with the BASE instance's values otherwise.
methods.dev_add_instance = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_add_instance: op required") end
  local fields = p.fields
  if fields ~= nil and type(fields) ~= "table" then
    error("dev_add_instance: fields must be a table if given")
  end
  return dt.develop.add_instance(op, fields)
end

-- Build a drawn PATH mask from a normalized polygon and attach it to a
-- module's blend so the module's effect is restricted to that region
-- (PLAN.md T2.2, promoted from darktable-mcp/spike/spike_methods.lua once
-- the underlying C binding (add_path_mask_cb, src/lua/develop.c) stabilized).
-- `points` is a Lua array of {x=..,y=..} normalized 0..1 (>=3 nodes,
-- typically the polygon returned by the segmentation sidecar via T2.3's
-- mask_object). Returns the C result verbatim: {ok, op, instance, formid,
-- mask_id, points=<node count>, opacity, feather, smooth, mask_mode} |
-- {error=...}. `feather` (optional) is a fraction of the mask bbox; `smooth`
-- (optional bool, default true) uses Catmull-Rom bezier handles so curved
-- subjects are not faceted. nil args fall back to the C-side defaults.
methods.dev_add_path_mask = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_add_path_mask: op required") end
  local instance = tonumber(p.instance) or 0
  local points = p.points
  if type(points) ~= "table" then error("dev_add_path_mask: points array required") end
  local opacity = tonumber(p.opacity)
  if opacity == nil then opacity = 1.0 end
  local feather = tonumber(p.feather) -- nil -> C default (0.02)
  local smooth
  if p.smooth ~= nil then smooth = p.smooth and true or false end -- nil -> C default (true)
  return dt.develop.add_path_mask(op, instance, points, opacity, feather, smooth)
end

-- Retouch module: local heal/clone/blur/fill shapes tied to the module's own
-- wavelet scale + rt_forms array (dt.develop.retouch_add_shape/delete_shape/
-- list_shapes, src/lua/develop.c) -- distinct from dev_add_path_mask's
-- generic "restrict this module's blend to a region". retouch_add_shape/
-- update_shape/list_shapes support circle, ellipse, and path (see
-- docs/superpowers/specs/2026-07-31-retouch-ellipse-path-shapes-design.md).
-- brush is a later phase (per-vertex pressure data doesn't map cleanly to
-- an API caller).
-- blur/fill let you soften/erase texture on a nonzero wavelet scale WITHOUT
-- touching tone/shadow on the other scales -- unlike heal/clone at scale 0,
-- which always rewrites the full pixel (tone included). See
-- dt.develop.retouch_add_shape's doc comment (src/lua/develop.c) for the full
-- wavelet-scale explanation.
local RETOUCH_ALGOS_NEEDING_SOURCE = { heal = true, clone = true }

methods.dev_retouch_add_shape = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_retouch_add_shape: op required") end
  local instance = tonumber(p.instance) or 0
  local shape_type = p.shape_type
  if shape_type and shape_type ~= "circle" and shape_type ~= "ellipse" and shape_type ~= "path" then
    error("dev_retouch_add_shape: shape_type must be 'circle', 'ellipse', or 'path'")
  end
  local is_path = (shape_type == "path")
  local algorithm = p.algorithm
  if not algorithm or algorithm == "" then error("dev_retouch_add_shape: algorithm required") end
  -- target/radius are meaningless for a path (a polygon has no single
  -- center/radius) -- C ignores them for shape_type="path", so this wrapper
  -- accepts (but does not require) them for that case, substituting a
  -- placeholder 0.0 for the unconditional positional C args.
  local target = p.target
  local target_x, target_y = 0.0, 0.0
  if is_path then
    if type(target) == "table" then
      target_x, target_y = tonumber(target.x) or 0.0, tonumber(target.y) or 0.0
    end
  else
    if type(target) ~= "table" or tonumber(target.x) == nil or tonumber(target.y) == nil then
      error("dev_retouch_add_shape: target {x,y} required")
    end
    target_x, target_y = tonumber(target.x), tonumber(target.y)
  end
  local source = p.source
  local source_x, source_y
  if RETOUCH_ALGOS_NEEDING_SOURCE[algorithm] then
    if type(source) ~= "table" or tonumber(source.x) == nil or tonumber(source.y) == nil then
      error("dev_retouch_add_shape: source {x,y} required for heal/clone")
    end
    source_x, source_y = tonumber(source.x), tonumber(source.y)
  elseif type(source) == "table" then
    source_x, source_y = tonumber(source.x), tonumber(source.y)
  end
  local radius = tonumber(p.radius)
  if not is_path and radius == nil then error("dev_retouch_add_shape: radius required") end
  radius = radius or 0.01 -- placeholder for path, ignored by C
  local feather = tonumber(p.feather) or 0.0
  local scale = tonumber(p.wavelet_scale) -- nil -> C default (module's curr_scale)
  local opacity = tonumber(p.opacity)
  if opacity == nil then opacity = 1.0 end
  local blur_type = p.blur_type -- nil -> C default (module's current blur_type)
  local blur_radius = tonumber(p.blur_radius) -- nil -> C default (module's current blur_radius)
  local fill_mode = p.fill_mode -- nil -> C default (module's current fill_mode)
  local fill_color = p.fill_color
  local fill_r, fill_g, fill_b
  if type(fill_color) == "table" then
    fill_r, fill_g, fill_b =
      tonumber(fill_color.r), tonumber(fill_color.g), tonumber(fill_color.b)
  end
  local fill_brightness = tonumber(p.fill_brightness) -- nil -> C default
  -- radius_b defaults to radius (a circle-shaped ellipse) if omitted; only
  -- meaningful when shape_type="ellipse", ignored by C otherwise.
  local radius_b = tonumber(p.radius_b)
  local rotation = tonumber(p.rotation)
  local points = nil
  if is_path then
    if type(p.points) ~= "table" or #p.points < 3 then
      error("dev_retouch_add_shape: points (>=3 {x,y}) required for shape_type='path'")
    end
    points = p.points
  elseif p.points ~= nil then
    error("dev_retouch_add_shape: points only applies to shape_type='path'")
  end
  local smooth
  if p.smooth ~= nil then smooth = p.smooth and true or false end -- nil -> C default (true)
  return dt.develop.retouch_add_shape(op, instance, algorithm,
    target_x, target_y, radius, feather,
    source_x, source_y, scale, opacity,
    blur_type, blur_radius, fill_mode, fill_r, fill_g, fill_b, fill_brightness,
    shape_type, radius_b, rotation, points, smooth)
end

-- Move/resize an existing shape in place (same formid) -- see
-- dt.develop.retouch_update_shape's doc comment in src/lua/develop.c for why
-- this is a masks-history commit, distinct from retouch_add_shape's
-- iop-params-history one. algorithm/wavelet_scale/opacity/blur_*/fill_* are
-- all optional: omit to keep the shape's current value. source is required
-- only when the EFFECTIVE algorithm (the one given here, or the shape's
-- current one if omitted) is heal/clone -- C does the authoritative check
-- since only it knows the shape's current algorithm when none is passed.
methods.dev_retouch_update_shape = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_retouch_update_shape: op required") end
  local instance = tonumber(p.instance) or 0
  local formid = tonumber(p.formid)
  if formid == nil then error("dev_retouch_update_shape: formid required") end
  -- `points` implies the caller means to update a path -- in that mode
  -- target/radius are meaningless (a polygon has no single center/radius)
  -- and are NOT required, unlike the circle/ellipse case below. Whether the
  -- formid is ACTUALLY a path is something only C can verify (this wrapper
  -- has no way to look up the shape's current type), so a mismatch (points
  -- given for a non-path formid) surfaces as C's own graceful error.
  local points = nil
  if type(p.points) == "table" then
    if #p.points < 3 then
      error("dev_retouch_update_shape: points needs at least 3 {x,y} nodes")
    end
    points = p.points
  end
  local target = p.target
  local target_x, target_y
  if points then
    target_x, target_y = 0.0, 0.0
    if type(target) == "table" then
      target_x, target_y = tonumber(target.x) or 0.0, tonumber(target.y) or 0.0
    end
  else
    if type(target) ~= "table" or tonumber(target.x) == nil or tonumber(target.y) == nil then
      error("dev_retouch_update_shape: target {x,y} required")
    end
    target_x, target_y = tonumber(target.x), tonumber(target.y)
  end
  local algorithm = p.algorithm -- nil -> C keeps the shape's current algorithm
  local source = p.source
  local source_x, source_y
  if type(source) == "table" then
    source_x, source_y = tonumber(source.x), tonumber(source.y)
  elseif RETOUCH_ALGOS_NEEDING_SOURCE[algorithm] then
    error("dev_retouch_update_shape: source {x,y} required for heal/clone")
  end
  local radius = tonumber(p.radius)
  if not points and radius == nil then error("dev_retouch_update_shape: radius required") end
  radius = radius or 0.01 -- placeholder for path, ignored by C
  local feather = tonumber(p.feather) or 0.0
  local scale = tonumber(p.wavelet_scale) -- nil -> C keeps the shape's current scale
  local opacity = tonumber(p.opacity) -- nil -> C leaves opacity untouched
  local blur_type = p.blur_type
  local blur_radius = tonumber(p.blur_radius)
  local fill_mode = p.fill_mode
  local fill_color = p.fill_color
  local fill_r, fill_g, fill_b
  if type(fill_color) == "table" then
    fill_r, fill_g, fill_b =
      tonumber(fill_color.r), tonumber(fill_color.g), tonumber(fill_color.b)
  end
  local fill_brightness = tonumber(p.fill_brightness)
  -- only meaningful for an existing ellipse shape; C rejects them (rather
  -- than silently ignoring) if passed for a circle/path formid.
  local radius_b = tonumber(p.radius_b)
  local rotation = tonumber(p.rotation)
  local smooth
  if p.smooth ~= nil then smooth = p.smooth and true or false end -- nil -> C default (true)
  return dt.develop.retouch_update_shape(op, instance, formid,
    target_x, target_y, radius, feather,
    source_x, source_y, algorithm, scale, opacity,
    blur_type, blur_radius, fill_mode, fill_r, fill_g, fill_b, fill_brightness,
    radius_b, rotation, points, smooth)
end

methods.dev_retouch_delete_shape = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_retouch_delete_shape: op required") end
  local instance = tonumber(p.instance) or 0
  local formid = tonumber(p.formid)
  if formid == nil then error("dev_retouch_delete_shape: formid required") end
  return dt.develop.retouch_delete_shape(op, instance, formid)
end

methods.dev_retouch_list_shapes = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_retouch_list_shapes: op required") end
  local instance = tonumber(p.instance) or 0
  return dt.develop.retouch_list_shapes(op, instance)
end

-- Rollback helper for mask_object (T2.3, ACCEPTANCE T2.3-S2 / PLAN.md gap
-- "no orphan instance"): removes the most-recently-created multi-instance of
-- `op` via the SAME "new instance" GUI action darktable's own module header
-- button uses -- dt.gui.action("iop/"..op, -1, "instance", "delete"), which
-- dt_action_process resolves to dt_iop_module_t* and dispatches to
-- _gui_delete_callback -> dt_dev_module_remove (src/develop/imageop.c:480,
-- :4074 DT_ACTION_EFFECT_DELETE case). No C change needed -- this reuses the
-- existing action-system passthrough darktable.gui.action() already exposes
-- (same mechanism as the T0.3 spike's `nudge`).
--
-- `-1` asks dt_action_process to count instances of this op from the TAIL of
-- dev->iop (src/gui/accelerators.c _process_action: `instance<0` walks
-- g_list_last/g_list_previous). dt_dev_module_duplicate always inserts a new
-- instance immediately after the base (multi_priority 0) instance in
-- iop_order (src/develop/develop.c:3706 dt_ioppr_move_iop_after), so right
-- after mask_object's own add_instance(op) call the newest instance IS the
-- last (or only other) entry among this op's instances -- `-1` targets it
-- exactly. This is a narrow, single-purpose primitive for "undo the instance
-- I just created a moment ago", NOT a general "delete instance N" API: if
-- other instances of the same op were added/reordered by someone else
-- between add_instance and the rollback, `-1` may not resolve to the
-- intended instance. Requires >=2 instances of `op` to exist -- darktable's
-- own multi_show.close guard (imageop.c:_get_multi_show) -- which is always
-- true right after add_instance succeeded.
methods.dev_remove_last_instance = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_remove_last_instance: op required") end
  local action_path = "iop/" .. op
  -- IMPORTANT: the trailing `size` arg (any value != DT_READ_ACTION_ONLY,
  -- i.e. != -FLT_MAX) is NOT optional here. dt_action_process gates every
  -- state-changing effect behind DT_PERFORM_ACTION(move_size) (src/common/
  -- action.h: `(move_size) != DT_READ_ACTION_ONLY`); omitting it (as a bare
  -- 4-arg dt.gui.action call defaults move_size to DT_READ_ACTION_ONLY,
  -- src/lua/gui.c _action_cb) makes this call-and-return a harmless READ
  -- that resolves the target module and returns 0 WITHOUT invoking
  -- _gui_delete_callback -- confirmed empirically: it returns a valid
  -- (non-NaN) value and reports ok=true while leaving dev->iop completely
  -- unchanged. `1.0` below is the "perform it" trigger value, not a
  -- meaningful magnitude (the instance/delete effect ignores its size).
  local ok, ret = pcall(function()
    return dt.gui.action(action_path, -1, "instance", "delete", 1.0)
  end)
  if not ok then
    return { ok = false, op = op, error = "dt.gui.action raised: " .. tostring(ret) }
  end
  if ret ~= ret then -- NaN: dt_action_process's DT_ACTION_NOT_VALID sentinel
    return { ok = false, op = op,
             error = "dt.gui.action returned invalid (only one instance, or action path not found)" }
  end
  return { ok = true, op = op }
end

-- Read-only darkroom canvas zoom/pan for the main window and, when open on a
-- second monitor, the preview2 window. Returns the C result verbatim:
-- {main={...}, preview2={active=bool,...}} | {error=...}. Each viewport carries
-- a `region` {x,y,w,h} (top-left, normalized 0..1, clamped to [0,1]) = the crop
-- of the full image visible in that window; pass it straight into get_preview's
-- `region`. Raw zoom_x/zoom_y are center-relative (can be negative), NOT a
-- drop-in region.
methods.dev_get_viewport = function(p)
  return dt.develop.get_viewport()
end

-- Writable counterpart to dev_get_viewport (2026-07-31 set-viewport-design):
-- set an ABSOLUTE zoom_x/zoom_y/scale on "main" (dev->full) or "preview2".
-- Free zoom only (no fit/fill/1:1 snapping) -- the C side clamps `scale` to
-- darktable's own unconstrained-zoom bound and reports the clamp. This
-- wrapper does NOT do region math (aspect-expand, mode translation,
-- min_render_px_across) -- that lives in server.py, which reads
-- dev_get_viewport()'s processed_width/processed_height + current region/
-- scale to invert the region formula, then calls this with the resulting
-- zoom_x/zoom_y/scale. `wait_for_pipe` (default true) blocks (bounded by
-- `timeout_ms`, default 4000) until dev->full.pipe/dev->preview2.pipe has
-- actually reprocessed at the new zoom -- see dt.develop.set_viewport's own
-- long doc comment in src/lua/develop.c for why this is NOT the same
-- freshness check dev_preview uses. Returns the C result verbatim:
-- {ok, viewport, previous={zoom,zoom_label,closeup,zoom_x,zoom_y,scale},
-- applied={zoom_x,zoom_y,scale}, clamped=[...], pipe_ready, waited_ms} |
-- {error="viewport_not_active: ..."} | {error=...}.
methods.dev_set_viewport = function(p)
  p = p or {}
  local viewport = p.viewport or "main"
  local zoom_x = tonumber(p.zoom_x)
  local zoom_y = tonumber(p.zoom_y)
  local scale = tonumber(p.scale)
  if zoom_x == nil or zoom_y == nil or scale == nil then
    error("dev_set_viewport: zoom_x, zoom_y, scale required")
  end
  local wait_for_pipe = p.wait_for_pipe
  if wait_for_pipe == nil then wait_for_pipe = true end
  local timeout_ms = tonumber(p.timeout_ms)
  return dt.develop.set_viewport(viewport, zoom_x, zoom_y, scale,
    wait_for_pipe and true or false, timeout_ms)
end

-- Put a viewport's zoom/pan back exactly as dev_set_viewport (or
-- dev_get_viewport) reported it in `previous`/its own snapshot -- pass that
-- SAME table straight through as `previous` (no new state to invent, per
-- the set-viewport-design task's requirement 7). Same wait_for_pipe/
-- timeout_ms semantics as dev_set_viewport; no clamping (a state that was
-- once valid is restored as-is). Returns {ok, viewport, pipe_ready,
-- waited_ms} | {error="viewport_not_active: ..."} | {error=...}.
methods.dev_restore_viewport = function(p)
  p = p or {}
  local viewport = p.viewport or "main"
  local prev = p.previous
  if type(prev) ~= "table" then
    error("dev_restore_viewport: previous table required (zoom, closeup, zoom_x, zoom_y, scale)")
  end
  local zoom = tonumber(prev.zoom)
  local closeup = tonumber(prev.closeup)
  local zoom_x = tonumber(prev.zoom_x)
  local zoom_y = tonumber(prev.zoom_y)
  local scale = tonumber(prev.scale)
  if zoom == nil or closeup == nil or zoom_x == nil or zoom_y == nil or scale == nil then
    error("dev_restore_viewport: previous must have zoom, closeup, zoom_x, zoom_y, scale")
  end
  local wait_for_pipe = p.wait_for_pipe
  if wait_for_pipe == nil then wait_for_pipe = true end
  local timeout_ms = tonumber(p.timeout_ms)
  return dt.develop.restore_viewport(viewport, zoom, closeup, zoom_x, zoom_y, scale,
    wait_for_pipe and true or false, timeout_ms)
end

-- Bugreport (2026-07-25): a caller deriving a normalized point from
-- get_viewport()/get_preview() (the PROCESSED/display frame) and handing it
-- STRAIGHT to dev_retouch_add_shape/dev_add_path_mask (which store points in
-- the PIPE-INPUT/mask frame -- see dt.develop.backtransform_point's doc
-- comment in src/lua/develop.c) places the shape on the wrong part of the
-- image whenever orientation/crop/rotate/lens-correction is active -- these
-- two frames are NOT the same, confirmed via a real portrait photo where a
-- neck target landed on the chest even with a full, uncropped region. This
-- wrapper is the missing conversion step; len1/len2 are optional lengths
-- (e.g. radius/feather) converted alongside x/y in the same call.
methods.dev_backtransform_point = function(p)
  p = p or {}
  local x = tonumber(p.x)
  local y = tonumber(p.y)
  if x == nil or y == nil then error("dev_backtransform_point: x/y required") end
  local len1 = tonumber(p.len1)
  local len2 = tonumber(p.len2)
  return dt.develop.backtransform_point(x, y, len1, len2)
end

-- The INVERSE of dev_backtransform_point: mask-frame normalized point (what
-- dev_retouch_list_shapes/dev_get_mask report as stored geometry) -> processed/
-- display-frame normalized point (what get_preview/capture_viewport render).
-- Needed to plot an EXISTING shape on top of a captured render, or to check
-- that a written shape landed where it was aimed. len1/len2 are optional
-- lengths (radius/feather) in dt_masks' mindim-normalized convention, returned
-- normalized against the display frame WIDTH.
methods.dev_transform_point = function(p)
  p = p or {}
  local x = tonumber(p.x)
  local y = tonumber(p.y)
  if x == nil or y == nil then error("dev_transform_point: x/y required") end
  local len1 = tonumber(p.len1)
  local len2 = tonumber(p.len2)
  return dt.develop.transform_point(x, y, len1, len2)
end

-- T3.3 (promoted from darktable-mcp/spike/spike_methods.lua once the T3.1
-- go/no-go spike proved the underlying C binding, src/lua/develop.c
-- set_raster_source_cb): wire a downstream (consumer) module's blend to
-- consume the RASTER mask emitted by an upstream (source) module -- e.g. the
-- stock iop/rasterfile.c producer (T3.1, IOP_FLAGS_WRITE_RASTER) reading an
-- external PFM/PNG file. This is the ONE piece add_path_mask (T2.2, drawn
-- masks only) cannot do: a soft, per-pixel raster alpha instead of a hard
-- polygon boundary. opacity is 0..100 (percent, default 100.0) matching the
-- C binding's own blend_params->opacity scale -- NOT the 0..1 fraction
-- add_path_mask uses; mask_raster (T3.3, server.py) converts its 0..1
-- input before calling this. Returns the C result verbatim: {ok, consumer,
-- consumer_instance, source, source_instance, raster_mask_source,
-- raster_mask_instance, mask_mode, opacity} | {error=...}. The source
-- module must be EARLIER in the pixelpipe (lower iop_order) than the
-- consumer, or the C side returns an error.
methods.dev_set_raster_source = function(p)
  p = p or {}
  local cop = p.consumer_op or p.op
  if not cop or cop == "" then error("dev_set_raster_source: consumer_op required") end
  local sop = p.source_op
  if not sop or sop == "" then error("dev_set_raster_source: source_op required") end
  local cinst = tonumber(p.consumer_instance) or 0
  local sinst = tonumber(p.source_instance) or 0
  if p.opacity ~= nil then
    return dt.develop.set_raster_source(cop, cinst, sop, sinst, tonumber(p.opacity))
  end
  return dt.develop.set_raster_source(cop, cinst, sop, sinst)
end

-- Read-only enumeration of the drawn masks wired into ONE module instance's
-- blend group (mask_id/type/opacity/nb_points/name/operation/invert per
-- shape) -- the per-module half of get_module_mask; combine with
-- dev_get_blend_params (group-level opacity/mask_mode/blend_mode/invert) for
-- the full picture. Returns [] when the module has no drawn masks.
methods.dev_list_masks = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_list_masks: op required") end
  local instance = tonumber(p.instance) or 0
  return dt.develop.list_masks(op, instance)
end

-- Read-only enumeration of EVERY drawn mask shape in the current image,
-- independent of which module currently uses it -- so a caller can find a
-- shape drawn earlier (by hand in the GUI, or by another tool this session)
-- and attach it to a NEW module instance via dev_attach_mask, instead of
-- guessing a formid or redrawing it. Each entry also reports `used_by`
-- ({op, instance} pairs) so a caller can see who else already references it.
methods.dev_list_all_masks = function(p)
  return dt.develop.list_all_masks()
end

-- Read-only dump of ONE mask form's full point geometry (corner/ctrl1/ctrl2/
-- border/state per node for path/brush; single-entry descriptor for circle/
-- ellipse), plus its name -- so a caller can inspect/verify a mask's actual
-- shape (e.g. whether a node is a smooth or corner point: ctrl1/ctrl2 equal
-- to corner means corner, differing means smooth) without re-segmenting.
-- dt.develop.get_mask itself already existed in C (registered, unused until
-- 2026-07-31) -- this was the missing Lua-bridge wiring, not new C.
-- Points are in the same PIPE-INPUT/mask-frame convention as
-- add_path_mask's input -- see dt.develop.backtransform_point's doc comment
-- for why that differs from get_preview's display frame under crop/rotate/
-- orientation.
methods.dev_get_mask = function(p)
  p = p or {}
  local mask_id = tonumber(p.mask_id)
  if mask_id == nil then error("dev_get_mask: mask_id required") end
  return dt.develop.get_mask(mask_id)
end

-- Give a drawn mask a caller-chosen name instead of the auto-generated
-- "path #7"/"circle #3" (bugreport 2026-07-31: no way to tell masks apart
-- afterward except by formid, once several have been created in one
-- session). Returns {ok, mask_id, name} with name read back from the live
-- struct, not the requested string.
methods.dev_rename_mask = function(p)
  p = p or {}
  local mask_id = tonumber(p.mask_id)
  if mask_id == nil then error("dev_rename_mask: mask_id required") end
  local name = p.name
  if name == nil then error("dev_rename_mask: name required") end
  return dt.develop.rename_mask(mask_id, tostring(name))
end

-- Permanently delete a drawn mask shape, whether or not it's currently
-- attached to any module (2026-07-31 bugreport: no way to clean up an
-- orphan mask left over from experimentation -- detach_mask only unwires
-- it, still leaving it in dev->forms forever).
methods.dev_delete_mask = function(p)
  p = p or {}
  local mask_id = tonumber(p.mask_id)
  if mask_id == nil then error("dev_delete_mask: mask_id required") end
  return dt.develop.delete_mask(mask_id)
end

-- Wire an EXISTING drawn mask shape (formid, from dev_list_all_masks or the
-- return value of dev_add_path_mask/dev_mask_object/dev_retouch_add_shape)
-- into module (op, instance)'s blend group WITHOUT copying it -- the shape
-- stays one dev->forms entry, now referenced by this module's group in
-- ADDITION to whatever already referenced it (the same shape can back
-- several modules at once). `operation` (default "union") selects how this
-- shape combines with whatever else is already in the group: "union",
-- "intersection", "difference", "exclusion".
--
-- Also ORs DEVELOP_MASK_MASK|DEVELOP_MASK_ENABLED into blend_params->
-- mask_mode (bugreport 2026-07-27: wiring the group reference alone left
-- the mask inert -- dt_dev_pixelpipe's _piece_wants_blending gates the
-- actual blend on DEVELOP_MASK_ENABLED, so the shape existed but had no
-- visible effect until the pencil icon was clicked by hand). The returned
-- `mask_mode` is read back from the live struct after the write, so a
-- caller can verify it stuck instead of trusting the call silently.
methods.dev_attach_mask = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_attach_mask: op required") end
  local instance = tonumber(p.instance) or 0
  local formid = tonumber(p.formid)
  if formid == nil then error("dev_attach_mask: formid required") end
  local operation = p.operation
  return dt.develop.attach_mask(op, instance, formid, operation)
end

-- Unwire a drawn mask shape from module (op, instance)'s blend group WITHOUT
-- deleting the shape itself -- it stays in dev->forms and can be re-attached
-- (to this module or another) via dev_attach_mask. If this was the LAST shape
-- in the group, darktable's own dt_masks_form_remove cascades into deleting
-- the now-empty group, which resets EVERY module referencing it back to "no
-- mask" -- expected, mirrors the GUI's own "no masks" action exactly.
methods.dev_detach_mask = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_detach_mask: op required") end
  local instance = tonumber(p.instance) or 0
  local formid = tonumber(p.formid)
  if formid == nil then error("dev_detach_mask: formid required") end
  return dt.develop.detach_mask(op, instance, formid)
end

-- ---- Dispatch --------------------------------------------------------------

local function handle(req)
  -- A decodable request with a usable id but a junk `method` still deserves a
  -- real error response, not silence: silence costs the client its whole
  -- timeout and then reports "darktable not running", which is a lie.
  if type(req.method) ~= "string" then
    return {
      id = req.id,
      error = "malformed request: 'method' must be a string, got "
              .. type(req.method),
    }
  end
  local fn = methods[req.method]
  if not fn then
    return {id = req.id, error = "unknown method: " .. tostring(req.method)}
  end
  if req.method:sub(1, 4) == "dev_" and type(dt.develop) ~= "table" then
    return {id = req.id, error = "This editing tool requires patched darktable with the "
      .. "darktable.develop API (rfordinal/darktable-agentic). Library tools remain available."}
  end
  local ok, result_or_err = pcall(fn, req.params)
  if not ok then
    return {id = req.id, error = "handler raised: " .. tostring(result_or_err)}
  end
  return {id = req.id, result = result_or_err}
end

-- ---- File I/O --------------------------------------------------------------

-- shell_quote lives up top, next to the other filesystem helpers.

local function cache_dir()
  local base = os.getenv("XDG_CACHE_HOME")
  if not base or base == "" then
    local home = os.getenv("HOME")
    if not home or home == "" then home = "." end
    base = home .. "/.cache"
  end
  return base .. "/darktable-mcp"
end

-- In-process listing via LuaFileSystem. Preferred when available: no fork, no
-- shell, no quoting hazard, and no ~36k subprocesses per hour of idle session.
-- `lfs_mod` is injectable so the tests can exercise this path on a host where
-- lfs is not installed.
local function list_request_files_lfs(dir, lfs_mod)
  lfs_mod = lfs_mod or lfs
  local out = {}
  local ok = pcall(function()
    for entry in lfs_mod.dir(dir) do
      -- Readers must ignore *.tmp; the `.json$` anchor already does that,
      -- since an in-flight write is named request-<id>.json.tmp.
      if entry:match("^request%-.+%.json$") then
        out[#out + 1] = dir .. "/" .. entry
      end
    end
  end)
  if not ok then return {} end
  table.sort(out)   -- lfs.dir order is arbitrary; keep dispatch deterministic
  return out
end

-- Fallback listing for a darktable Lua without lfs. Note the glob stays
-- OUTSIDE the quoted directory so the shell still expands it.
local function list_request_files_ls(dir)
  local out = {}
  local p = io.popen("ls -1 " .. shell_quote(dir) .. "/request-*.json 2>/dev/null")
  if not p then return out end
  for line in p:lines() do
    if not line:match("%.tmp$") then table.insert(out, line) end
  end
  p:close()
  return out
end

local list_request_files
if lfs_available then
  list_request_files = function(dir) return list_request_files_lfs(dir) end
else
  list_request_files = list_request_files_ls
end

local function read_file(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local content = f:read("*a")
  f:close()
  return content
end

-- Returns true on success, or false plus a reason string. Every failure mode
-- (open, write, close, rename) is reported: a silent failure here is
-- indistinguishable, from the client's side, from darktable not running.
local function write_file_atomic(path, content)
  local tmp = path .. ".tmp"
  local f, oerr = io.open(tmp, "w")
  if not f then return false, tostring(oerr) end
  local wok, werr = f:write(content)
  -- close() is where a full disk usually surfaces: the write may sit in the
  -- stdio buffer until flush.
  local cok, cerr = f:close()
  if not wok then os.remove(tmp); return false, tostring(werr) end
  if not cok then os.remove(tmp); return false, tostring(cerr) end
  local rok, rerr = os.rename(tmp, path)
  if not rok then os.remove(tmp); return false, tostring(rerr) end
  return true
end

-- Write the response for `id`, reporting anything that goes wrong. A dropped
-- response costs the caller its entire timeout and then reports "darktable
-- not running" -- so a full disk or a bad permission MUST leave a trace.
local function write_response(dir, id, resp)
  local resp_path = dir .. "/response-" .. id .. ".json"
  local enc_ok, encoded = pcall(json.encode, resp)
  if not enc_ok then
    -- Encoding the result blew up; still answer, with the encoder's error.
    encoded = json.encode({
      id = id,
      error = "response encoding failed: " .. tostring(encoded),
    })
  end
  local ok, err = write_file_atomic(resp_path, encoded)
  if not ok then
    log(string.format(
      "darktable-mcp: FAILED to write %s (%s) -- disk full or permissions? "
      .. "The client will time out and report darktable as not running.",
      resp_path, tostring(err)))
    return false
  end
  return true
end

-- Extract a response-nameable id from a decoded request, or nil.
-- The pattern is a filename-safety check, not a UUID check: the id goes
-- straight into a path, so `/`, `..` and friends must never get through.
local function safe_request_id(req)
  if type(req) ~= "table" then return nil end
  local raw = req.id
  -- A JSON number id is perfectly recoverable -- coerce rather than drop it.
  if type(raw) ~= "string" and type(raw) ~= "number" then return nil end
  raw = tostring(raw)
  if not raw:match("^[%w%-_]+$") then return nil end
  return raw
end

-- Scans `dir` once. Returns the number of request files handled, which the
-- worker loop uses to drive its adaptive poll interval.
local function scan_dir(dir)
  local handled = 0
  for _, req_path in ipairs(list_request_files(dir)) do
    local content = read_file(req_path)
    if content then
      handled = handled + 1
      local ok, req = pcall(json.decode, content)
      local req_id = ok and safe_request_id(req) or nil

      if req_id then
        -- Recoverable: we can name a response file, so ALWAYS write one --
        -- handle() turns any junk in the rest of the request into an error
        -- response rather than silence.
        req.id = req_id
        write_response(dir, req_id, handle(req))
      else
        -- Unrecoverable: with no safely-nameable id there is no response
        -- filename the client would be polling for, so nothing can be sent
        -- back. Log it, so the failure is at least diagnosable, and still
        -- delete the file so it does not get retried forever.
        local reason
        if not ok then
          reason = "unparseable JSON: " .. tostring(req)
        elseif type(req) ~= "table" then
          reason = "top-level JSON value is not an object"
        else
          reason = "missing or unsafe 'id': " .. tostring(req.id)
        end
        log(string.format(
          "darktable-mcp: dropping malformed request %s (%s) -- no response "
          .. "can be addressed, the caller will time out",
          req_path, reason))
      end
      os.remove(req_path)
    end
  end
  return handled
end

-- Delete abandoned files older than max_age_seconds.
--
-- Covers responses as well as requests. When the Python client times out it
-- removes its own request file, but the worker may already be mid-flight and
-- writes response-<uuid>.json afterwards -- with nobody left to read or
-- delete it. Sweeping only request-*.json (the old behaviour) let those
-- orphans accumulate in the cache directory forever, across sessions.
-- *.json.tmp is swept too: a crash between open and rename strands one.
local function sweep_stale(dir, max_age_seconds)
  -- find's -mmin granularity is whole minutes, so 60s == 1min.
  local minutes = math.max(1, math.floor(max_age_seconds / 60))
  os.execute(string.format(
    'find %s -maxdepth 1 \\( -name "request-*.json" -o -name "response-*.json"'
    .. ' -o -name "*.json.tmp" \\) -mmin +%d -delete 2>/dev/null',
    shell_quote(dir), minutes))
end

-- ---- Worker loop -----------------------------------------------------------

-- Adaptive poll cadence.
--
-- The worker used to sleep a flat 100ms forever, so with the `ls` fallback it
-- forked ~10 subprocesses per second -- ~36,000 per hour -- for the entire
-- lifetime of the user's darktable session, even completely idle. Burning a
-- laptop battery to discover an empty directory 36,000 times is not a fair
-- trade for sub-100ms latency the user is not waiting on.
--
-- So: stay fast while requests are actually arriving, and step down after a
-- run of consecutive empty scans. Any request at all snaps the interval
-- straight back to POLL_FAST_MS, so a burst pays the fast cadence from its
-- second request onward and only the first request of an idle period pays the
-- backed-off latency.
local POLL_FAST_MS = 100     -- active: requests arriving, keep latency low
local POLL_MEDIUM_MS = 250   -- cooling down
local POLL_SLOW_MS = 1000    -- idle steady state: ~1 poll per second

-- Thresholds in consecutive IDLE SCANS (not wall-clock, since each tick's
-- length depends on the tier it was in).
--   ticks  1..9   @ 100ms -> ~1.0s of quiet
--   ticks 10..17  @ 250ms -> ~2.0s more (3.0s total)
--   ticks 18+     @ 1000ms -> steady state
local IDLE_TICKS_BEFORE_MEDIUM = 10
local IDLE_TICKS_BEFORE_SLOW = 18

-- Pure function of the idle-tick count, factored out so the progression is
-- unit-testable without running the real (infinite) loop.
local function poll_interval_ms(idle_ticks)
  if idle_ticks >= IDLE_TICKS_BEFORE_SLOW then return POLL_SLOW_MS end
  if idle_ticks >= IDLE_TICKS_BEFORE_MEDIUM then return POLL_MEDIUM_MS end
  return POLL_FAST_MS
end

-- Sweep cadence is measured in ELAPSED SECONDS, not ticks. The old
-- `tick % 100` fired every ~10s at the flat 100ms poll, but under adaptive
-- backoff a tick is 100ms..1000ms, so 100 ticks would drift anywhere from 10s
-- to 100s. Elapsed time keeps the cadence honest whatever the poll tier.
local SWEEP_INTERVAL_SECONDS = 10

-- INVARIANT: STALE_AGE_SECONDS must stay comfortably GREATER than the largest
-- per-method client budget in DEFAULT_TIMEOUTS (darktable_mcp/bridge/client.py
-- -- currently 120s for import_batch and apply_preset). Change either number
-- and you must re-check the other; tests/test_lua_dispatcher.py asserts the
-- relation across the two languages.
--
-- The sweep exists to collect files whose owner is GONE. A request whose
-- caller is still waiting is not abandoned, and the worker is single-threaded:
-- while it serves one 120s-budget call, later requests sit queued in the cache
-- directory doing nothing wrong. At the old 60s the sweep deleted those queued
-- request-*.json files out from under live callers, who then waited out their
-- full 120s and were told "darktable is not running" -- a bug the sweep itself
-- manufactured. 300s clears the largest budget with margin for the queueing
-- delay ahead of it.
local STALE_AGE_SECONDS = 300

local function worker_loop()
  local dir = cache_dir()
  os.execute("mkdir -p " .. shell_quote(dir))
  local idle_ticks = 0
  local last_sweep = os.time()
  while true do
    local handled = scan_dir(dir)
    if handled > 0 then
      idle_ticks = 0            -- snap back to the fast cadence immediately
    else
      idle_ticks = idle_ticks + 1
    end

    local now = os.time()
    if now - last_sweep >= SWEEP_INTERVAL_SECONDS then
      sweep_stale(dir, STALE_AGE_SECONDS)
      last_sweep = now
    end

    if dt.control and dt.control.sleep then
      dt.control.sleep(poll_interval_ms(idle_ticks))
    else
      log("darktable-mcp: dt.control.sleep missing, worker exiting")
      return
    end
  end
end

-- ---- Entry point -----------------------------------------------------------

log("darktable-mcp bridge: ready")
if dt.control and dt.control.dispatch then
  dt.control.dispatch(worker_loop)
end

-- ---- Test exports (used by tests/lua/test_dispatcher.lua) ------------------
return {
  handle = handle,
  scan_dir = scan_dir,
  methods = methods,
  json = json,
  -- Internals exercised directly by the unit tests.
  shell_quote = shell_quote,
  list_directories_recursive = list_directories_recursive,
  count_images_under = count_images_under,
  poll_for_imported = poll_for_imported,
  import_poll = {
    attempts = IMPORT_POLL_ATTEMPTS,
    interval_ms = IMPORT_POLL_INTERVAL_MS,
    settle_polls = IMPORT_SETTLE_POLLS,
    deadline_seconds = IMPORT_POLL_DEADLINE_SECONDS,
    zero_grace_polls = IMPORT_ZERO_GRACE_POLLS,
    max_depth = IMPORT_MAX_DEPTH,
  },
  cache_dir = cache_dir,
  safe_request_id = safe_request_id,
  write_file_atomic = write_file_atomic,
  write_response = write_response,
  sweep_stale = sweep_stale,
  list_request_files = list_request_files,
  list_request_files_ls = list_request_files_ls,
  list_request_files_lfs = list_request_files_lfs,
  lfs_available = lfs_available,
  poll_interval_ms = poll_interval_ms,
  poll_tiers = {
    fast_ms = POLL_FAST_MS,
    medium_ms = POLL_MEDIUM_MS,
    slow_ms = POLL_SLOW_MS,
    idle_ticks_before_medium = IDLE_TICKS_BEFORE_MEDIUM,
    idle_ticks_before_slow = IDLE_TICKS_BEFORE_SLOW,
  },
  sweep_interval_seconds = SWEEP_INTERVAL_SECONDS,
  stale_age_seconds = STALE_AGE_SECONDS,
}
