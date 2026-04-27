-- darktable_mcp: long-running plugin that exposes view_photos and
-- rate_photos to the Python MCP server via file-based JSON requests.
--
-- Loaded via `require "darktable_mcp"` from ~/.config/darktable/luarc.
-- Spawns a worker via dt.control.dispatch that polls
-- ~/.cache/darktable-mcp/ every ~100ms for request-*.json files,
-- dispatches them to the method registry, and writes
-- response-<uuid>.json. See:
--   docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md

local dt = require("darktable")

-- ---- JSON encode/decode (minimal, MVP-only) --------------------------------
-- darktable Lua does not bundle a JSON library reliably. Inline a tiny
-- encoder/decoder sufficient for our request/response shapes.

local json = {}

local function encode_value(v)
  local t = type(v)
  if t == "nil" then return "null"
  elseif t == "boolean" then return v and "true" or "false"
  elseif t == "number" then return tostring(v)
  elseif t == "string" then
    return '"' .. v:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t') .. '"'
  elseif t == "table" then
    -- Detect array vs object by checking for sequential integer keys.
    local n, max = 0, 0
    for k in pairs(v) do
      n = n + 1
      if type(k) == "number" and k > max then max = k end
    end
    if n == max and n > 0 then
      local parts = {}
      for i = 1, n do parts[i] = encode_value(v[i]) end
      return "[" .. table.concat(parts, ",") .. "]"
    elseif n == 0 then
      return "[]"
    else
      local parts = {}
      for k, val in pairs(v) do
        table.insert(parts, encode_value(tostring(k)) .. ":" .. encode_value(val))
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
      if esc == "n" then table.insert(out, "\n")
      elseif esc == "r" then table.insert(out, "\r")
      elseif esc == "t" then table.insert(out, "\t")
      elseif esc == '"' or esc == "\\" or esc == "/" then table.insert(out, esc)
      else error("unsupported escape \\" .. esc)
      end
      i = i + 2
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
  return tonumber(s:sub(start, i-1)), i
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

methods.view_photos = function(p)
  p = p or {}
  local out, count = {}, 0
  local limit = p.limit or 100
  local filter = p.filter or ""
  local rating_min = p.rating_min
  for _, image in ipairs(dt.database) do
    if count >= limit then break end
    local include = true
    if rating_min and (image.rating or 0) < rating_min then include = false end
    if include and filter ~= "" then
      local ok = string.find(string.lower(image.filename), string.lower(filter), 1, true)
      if not ok then include = false end
    end
    if include then
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

-- ---- Dispatch --------------------------------------------------------------

local function handle(req)
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

local function cache_dir()
  local base = os.getenv("XDG_CACHE_HOME")
  if not base or base == "" then
    base = os.getenv("HOME") .. "/.cache"
  end
  return base .. "/darktable-mcp"
end

local function list_request_files(dir)
  local out = {}
  local p = io.popen('ls -1 "' .. dir .. '"/request-*.json 2>/dev/null')
  if not p then return out end
  for line in p:lines() do
    if not line:match("%.tmp$") then table.insert(out, line) end
  end
  p:close()
  return out
end

local function read_file(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local content = f:read("*a")
  f:close()
  return content
end

local function write_file_atomic(path, content)
  local tmp = path .. ".tmp"
  local f = io.open(tmp, "w")
  if not f then return false end
  f:write(content)
  f:close()
  return os.rename(tmp, path)
end

local function scan_dir(dir)
  for _, req_path in ipairs(list_request_files(dir)) do
    local content = read_file(req_path)
    if content then
      local ok, req = pcall(json.decode, content)
      if ok and type(req) == "table" and req.id then
        local resp = handle(req)
        local resp_path = dir .. "/response-" .. req.id .. ".json"
        write_file_atomic(resp_path, json.encode(resp))
      end
      os.remove(req_path)
    end
  end
end

local function sweep_stale(dir, max_age_seconds)
  -- Simple approach: shell out. find prints paths older than N minutes;
  -- we use seconds via -mmin with arithmetic. For the MVP, 60s = 1min.
  local minutes = math.max(1, math.floor(max_age_seconds / 60))
  os.execute(string.format(
    'find "%s" -maxdepth 1 -name "request-*.json" -mmin +%d -delete 2>/dev/null',
    dir, minutes))
end

-- ---- Worker loop -----------------------------------------------------------

local function worker_loop()
  local dir = cache_dir()
  os.execute('mkdir -p "' .. dir .. '"')
  local tick = 0
  while true do
    scan_dir(dir)
    tick = tick + 1
    if tick % 100 == 0 then
      sweep_stale(dir, 60)
    end
    if dt.control and dt.control.sleep then
      dt.control.sleep(100)
    else
      -- Should never reach here in real darktable; tests pass a no-op stub.
      break
    end
  end
end

-- ---- Entry point -----------------------------------------------------------

dt.print_log("darktable-mcp bridge: ready")
if dt.control and dt.control.dispatch then
  dt.control.dispatch(worker_loop)
end

-- ---- Test exports (used by tests/lua/test_dispatcher.lua) ------------------
return {
  handle = handle,
  scan_dir = scan_dir,
  methods = methods,
  json = json,
}
