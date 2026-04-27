-- darktable-mcp Task 0 spike: confirm a Lua worker can run a non-blocking
-- loop inside an interactive darktable session without freezing the GUI.
--
-- Install: copy this file to ~/.config/darktable/lua/ and add
--   require "lua_worker_probe"
-- to ~/.config/darktable/luarc, then restart darktable.
--
-- Observe for 5 minutes: the GUI must stay responsive (zoom, pan, switch
-- views) and the log file at /tmp/darktable-mcp-spike.log should grow by
-- one line every ~1 second.

local dt = require("darktable")

local LOG = "/tmp/darktable-mcp-spike.log"

local function append(line)
  local f = io.open(LOG, "a")
  if f then
    f:write(line .. "\n")
    f:close()
  end
end

-- Truncate fresh each launch.
local f = io.open(LOG, "w")
if f then
  f:write("worker probe started at " .. os.date() .. "\n")
  f:close()
end

-- Step A: Introspect dt.control to discover the actual primitive names.
local function dump_keys(tbl, label)
  if type(tbl) ~= "table" and type(tbl) ~= "userdata" then
    append(label .. ": not a table/userdata (type=" .. type(tbl) .. ")")
    return
  end
  local keys = {}
  -- pairs works for plain tables; for userdata with __pairs metamethod it should too.
  local ok, err = pcall(function()
    for k, _ in pairs(tbl) do
      keys[#keys + 1] = tostring(k)
    end
  end)
  if not ok then
    append(label .. ": pairs() failed: " .. tostring(err))
    -- Try a metatable index walk.
    local mt = getmetatable(tbl)
    if mt and mt.__index and type(mt.__index) == "table" then
      for k, _ in pairs(mt.__index) do
        keys[#keys + 1] = tostring(k) .. "(mt)"
      end
    end
  end
  table.sort(keys)
  append(label .. ": " .. table.concat(keys, ", "))
end

dump_keys(dt, "dt keys")
dump_keys(dt.control, "dt.control keys")
if dt.gui then dump_keys(dt.gui, "dt.gui keys") end

-- Step B: Probe specific primitives by existence (no loadstring — Lua 5.3+).
local function probe(path, val)
  append("probe " .. path .. " => " .. type(val))
end

probe("dt.control.sleep", dt.control and dt.control.sleep)
probe("dt.control.execute", dt.control and dt.control.execute)
probe("dt.control.read", dt.control and dt.control.read)
probe("dt.control.dispatch", dt.control and dt.control.dispatch)
probe("dt.print_log", dt.print_log)
probe("dt.register_event", dt.register_event)
probe("dt.gui.create_job", dt.gui and dt.gui.create_job)

dt.print_log("darktable-mcp spike: introspection complete; starting worker")
append("introspection complete at " .. os.date())

-- Step C: Attempt the worker loop. If dt.control.sleep exists, use it
-- inside a coroutine via dt.control.dispatch / dt.control.execute pattern.
-- Try the primitives in order of likelihood.
local function worker_loop()
  local tick = 0
  while true do
    tick = tick + 1
    append("tick " .. tick .. " at " .. os.date())
    if dt.control and dt.control.sleep then
      dt.control.sleep(1000)
    else
      append("FATAL: dt.control.sleep missing; cannot continue ticking")
      return
    end
  end
end

-- Attempt 1: dt.control.dispatch (documented primitive for async work).
if dt.control and dt.control.dispatch then
  append("attempt 1: dt.control.dispatch(worker_loop)")
  local ok, err = pcall(dt.control.dispatch, worker_loop)
  if not ok then
    append("dispatch raised: " .. tostring(err))
  end
elseif dt.control and dt.control.execute then
  append("attempt 2: no dispatch; only execute exists. Skipping (execute is for shell cmds).")
elseif dt.control and dt.control.sleep then
  -- No async wrapper found. Calling sleep on the main thread would freeze
  -- the GUI; do NOT loop here. Just confirm sleep exists by calling it
  -- ONCE and then exit, so the user knows the GUI didn't hang because we
  -- never attempted an unbounded loop on the main thread.
  append("attempt 3: no async wrapper; calling dt.control.sleep(500) once on main thread")
  dt.control.sleep(500)
  append("returned from sleep at " .. os.date())
  append("VERDICT: sleep exists but no async wrapper found — main-thread loop would freeze GUI")
else
  append("VERDICT: neither dispatch nor sleep exists; need different primitive")
end
