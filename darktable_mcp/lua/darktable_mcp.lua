-- darktable_mcp: long-running plugin that exposes view_photos and
-- rate_photos to the Python MCP server via file-based JSON requests.
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

local _walk_lfs
_walk_lfs = function(dir, out, depth)
  out[#out + 1] = dir
  if depth >= IMPORT_MAX_DEPTH then return end
  pcall(function()
    for entry in lfs.dir(dir) do
      if entry ~= "." and entry ~= ".." then
        local child = dir .. "/" .. entry
        -- lfs.attributes (not symlinkAttributes) would follow symlinks and
        -- could loop; use the link's own mode so a symlinked directory is
        -- skipped rather than descended into.
        local mode = lfs.symlinkattributes and lfs.symlinkattributes(child, "mode")
                     or lfs.attributes(child, "mode")
        if mode == "directory" then _walk_lfs(child, out, depth + 1) end
      end
    end
  end)
end

-- Return `dir` plus every subdirectory beneath it, and whether the
-- enumeration actually succeeded. `false` means we could not read the tree
-- and the single-element result is a guess, NOT a proven leaf directory --
-- callers must not claim recursion was honoured in that case.
local function list_directories_recursive(dir)
  local out = {}
  if lfs_available then
    _walk_lfs(dir, out, 0)
  else
    -- `find` does not follow symlinks without -L, so this cannot loop.
    local p = io.popen("find " .. shell_quote(dir)
      .. " -maxdepth " .. IMPORT_MAX_DEPTH .. " -type d 2>/dev/null")
    if p then
      for line in p:lines() do out[#out + 1] = line end
      p:close()
    end
  end
  if #out == 0 then return {dir}, false end
  table.sort(out)   -- parents before children; deterministic import order
  return out, true
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
-- for its duration, so it is hard-capped: 100 x 100ms = 10s worst case. It
-- normally exits far earlier, as soon as the count stops growing.
--
-- The old ceiling was 3s, which the per-subdirectory camera layout now
-- routinely outruns: import_from_camera writes one directory per camera
-- folder/card, so a single card import can register several hundred files
-- across many film rolls and darktable's background scan takes longer than
-- 3s to register them all. The Python client budgets 120s for import_batch
-- (DEFAULT_TIMEOUTS in darktable_mcp/bridge/client.py), so 10s is affordable.
local IMPORT_POLL_ATTEMPTS = 100
local IMPORT_POLL_INTERVAL_MS = 100
-- Consecutive polls showing no growth before the count is called final.
-- Without this the poll stopped at the FIRST non-zero count, which under-
-- reports a trickling multi-directory import.
local IMPORT_SETTLE_POLLS = 5

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

  for _, image in ipairs(dt.database) do
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
        rating = image.rating or 0,
      }
    end
  end
  return out
end

methods.rate_photos = function(p)
  p = p or {}
  local updated = 0
  for _, photo_id in ipairs(p.photo_ids or {}) do
    local image = dt.database[tonumber(photo_id)]
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
-- runs -- worst case IMPORT_POLL_ATTEMPTS * IMPORT_POLL_INTERVAL_MS (10s) of
-- head-of-line blocking. The bridge is one-request-at-a-time by design (see
-- the IPC bridge MVP spec), so this is tolerated, but do not add more
-- in-worker polling loops.
local function poll_for_imported(source_path)
  local count, stable = 0, 0
  for _ = 1, IMPORT_POLL_ATTEMPTS do
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
    if dt.control and dt.control.sleep then
      dt.control.sleep(IMPORT_POLL_INTERVAL_MS)
    else
      break                     -- no way to yield; do not spin the GUI thread
    end
  end
  -- Ceiling reached with the answer still moving (or nothing found at all).
  return count, true
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
    note = "recursive=true requested, but " .. source_path .. " could not be "
        .. "enumerated, so only that path was imported. Any subdirectories "
        .. "were registered only if darktable's own recursive-import "
        .. "preference is enabled."
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
    local image = dt.database[tonumber(photo_id)]
    if image then
      image:apply_style(style)
      applied = applied + 1
    else
      table.insert(missed, tostring(photo_id))
    end
  end
  return {applied = applied, missed = missed, preset_name = preset_name}
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
local STALE_AGE_SECONDS = 60

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
