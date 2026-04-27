-- Lua dispatcher unit tests. Stubs the darktable `dt` global with a fake
-- database and verifies the method registry + scan_dir behavior in isolation.
--
-- Run via: lua tests/lua/test_dispatcher.lua
-- Exits 0 on success, non-zero on first failure.

-- ---- Test harness ----------------------------------------------------------
local failures = {}
local function assertEq(actual, expected, label)
  if actual ~= expected then
    table.insert(failures, string.format("%s: expected %s, got %s",
      label, tostring(expected), tostring(actual)))
  end
end
local function assertTrue(cond, label)
  if not cond then
    table.insert(failures, string.format("%s: expected truthy, got falsy", label))
  end
end

-- ---- Stub dt table ---------------------------------------------------------
-- Use ids that DO NOT overlap the iteration array's integer indexes (1..N),
-- so dt.database[id] always resolves via __index without colliding with
-- the iteration storage.
local images_by_id = {
  [101] = {id = 101, filename = "DSC_0001.NEF", path = "/photos", rating = 5},
  [102] = {id = 102, filename = "DSC_0002.NEF", path = "/photos", rating = 3},
  [103] = {id = 103, filename = "OTHER.NEF",   path = "/photos", rating = 4},
}
local iter_list = {}
for _, img in pairs(images_by_id) do table.insert(iter_list, img) end
local stub_db = setmetatable(iter_list, {
  __index = function(_, k) return images_by_id[k] end,
})

local dt_log = {}
local stub_dt = {
  database = stub_db,
  print_log = function(msg) table.insert(dt_log, msg) end,
  control = {
    dispatch = function(_) end,
    sleep = function(_) end,
  },
}
-- Make `require("darktable")` return our stub by pre-populating package.loaded.
package.loaded.darktable = stub_dt

-- ---- Load the plugin -------------------------------------------------------
package.path = package.path .. ";./darktable_mcp/lua/?.lua"
local internals = require("darktable_mcp")

-- ---- Verify the plugin announces itself on load ----------------------------
do
  assertEq(#dt_log, 1, "ready message logged on load")
  assertTrue(string.find(dt_log[1] or "", "ready"),
    "ready message contains the word 'ready'")
end

-- ---- methods.view_photos ---------------------------------------------------
do
  local result = internals.methods.view_photos({rating_min = 4, limit = 10})
  assertEq(#result, 2, "view_photos rating_min=4 returns 2 images")
  -- Order-independent: collect ratings, both must be >= 4, and the SET
  -- of ids must be {"101","103"} (the only images with rating >= 4).
  local ids_seen = {}
  for _, img in ipairs(result) do
    assertTrue(img.rating >= 4, "view_photos rating_min=4 image rating >= 4")
    ids_seen[img.id] = true
  end
  assertTrue(ids_seen["101"], "view_photos rating_min=4 includes id 101")
  assertTrue(ids_seen["103"], "view_photos rating_min=4 includes id 103")
end

do
  local result = internals.methods.view_photos({filter = "OTHER", limit = 10})
  assertEq(#result, 1, "view_photos filter=OTHER returns 1 image")
  assertEq(result[1].filename, "OTHER.NEF", "view_photos filter result filename")
end

do
  local result = internals.methods.view_photos({limit = 2})
  assertEq(#result, 2, "view_photos limit=2 caps at 2")
end

-- ---- methods.rate_photos ---------------------------------------------------
do
  local result = internals.methods.rate_photos({photo_ids = {"101", "102"}, rating = 1})
  assertEq(result.updated, 2, "rate_photos updated count")
  assertEq(images_by_id[101].rating, 1, "rate_photos changed image 101 rating")
  assertEq(images_by_id[102].rating, 1, "rate_photos changed image 102 rating")
end

-- ---- handle: known method --------------------------------------------------
do
  local resp = internals.handle({id = "abc", method = "view_photos", params = {limit = 1}})
  assertEq(resp.id, "abc", "handle preserves id")
  assertTrue(resp.result ~= nil, "handle known method returns result")
  assertTrue(resp.error == nil, "handle known method has no error")
end

-- ---- handle: unknown method ------------------------------------------------
do
  local resp = internals.handle({id = "xyz", method = "bogus", params = {}})
  assertEq(resp.id, "xyz", "handle preserves id on error")
  assertTrue(resp.error ~= nil, "handle unknown method returns error")
  assertTrue(string.find(resp.error, "bogus"), "error message names the method")
end

-- ---- scan_dir: full request/response round-trip ----------------------------
do
  local tmpdir = os.getenv("TMPDIR") or "/tmp"
  local test_dir = tmpdir .. "/darktable-mcp-lua-test-" .. tostring(os.time())
  os.execute("mkdir -p " .. test_dir)

  -- Reset stub state so view_photos in scan_dir sees the original data.
  images_by_id[101].rating = 5
  images_by_id[102].rating = 3

  -- Write a request file.
  local req_path = test_dir .. "/request-test001.json"
  local f = io.open(req_path, "w")
  f:write('{"id":"test001","method":"view_photos","params":{"limit":1}}')
  f:close()

  internals.scan_dir(test_dir)

  -- Verify request file was deleted.
  local req_check = io.open(req_path, "r")
  assertTrue(req_check == nil, "scan_dir deletes request file after processing")
  if req_check then req_check:close() end

  -- Verify response file appeared with correct content.
  local resp_path = test_dir .. "/response-test001.json"
  local resp_f = io.open(resp_path, "r")
  assertTrue(resp_f ~= nil, "scan_dir wrote response file")
  if resp_f then
    local content = resp_f:read("*a")
    resp_f:close()
    assertTrue(string.find(content, "test001"), "response contains request id")
    assertTrue(string.find(content, "result"), "response contains result field")
  end

  os.execute("rm -rf " .. test_dir)
end

-- ---- JSON round-trip with non-ASCII ----------------------------------------
do
  local original = {filter = "Тест", filename = "café.NEF"}
  local encoded = internals.json.encode(original)
  local decoded = internals.json.decode(encoded)
  assertEq(decoded.filter, "Тест", "non-ASCII Cyrillic round-trips")
  assertEq(decoded.filename, "café.NEF", "non-ASCII Latin-1 supplement round-trips")
end

-- ---- JSON \uXXXX escape decoding (matches what Python's json.dumps emits) -
do
  -- Python emits "Test" as ASCII, but emits "é" as é by default.
  local payload = '{"name":"caf\\u00e9.NEF"}'
  local decoded = internals.json.decode(payload)
  assertEq(decoded.name, "café.NEF", "\\u00e9 escape decodes to UTF-8")
end

-- ---- JSON control-byte escaping in encoder --------------------------------
do
  local with_ctrl = "ab\1cd"
  local encoded = internals.json.encode({s = with_ctrl})
  -- Encoder must escape \1 as  (otherwise Python's strict parser rejects).
  assertTrue(string.find(encoded, "\\u0001", 1, true) ~= nil,
    "encoder escapes 0x01 as \\u0001")
  local decoded = internals.json.decode(encoded)
  assertEq(decoded.s, with_ctrl, "control byte round-trips through escape")
end

-- ---- Report ----------------------------------------------------------------
if #failures > 0 then
  io.stderr:write("FAILED:\n")
  for _, msg in ipairs(failures) do
    io.stderr:write("  " .. msg .. "\n")
  end
  os.exit(1)
end
print("OK: all dispatcher tests passed")
os.exit(0)
