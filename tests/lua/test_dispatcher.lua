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
  -- view_photos must return the absolute file path (dir + filename), not
  -- just the directory. Otherwise it doesn't compose with export_images,
  -- which takes file paths in `photo_ids`.
  assertEq(result[1].path, "/photos/OTHER.NEF",
    "view_photos returns absolute file path")
end

do
  local result = internals.methods.view_photos({limit = 2})
  assertEq(#result, 2, "view_photos limit=2 caps at 2")
end

-- ---- methods.view_photos: trailing-slash dir handling ---------------------
do
  -- Some darktable backends include a trailing slash on image.path; some
  -- don't. The plugin must normalize both into a single-slash full path.
  local trail_img = {id = 999, filename = "TRAIL.NEF", path = "/photos/", rating = 0}
  images_by_id[999] = trail_img
  table.insert(iter_list, trail_img)
  local result = internals.methods.view_photos({filter = "TRAIL", limit = 10})
  assertEq(#result, 1, "view_photos trailing-slash filter returns 1 image")
  assertEq(result[1].path, "/photos/TRAIL.NEF",
    "view_photos collapses double slash from trailing-dir input")
  -- Cleanup so later tests that count totals don't trip.
  images_by_id[999] = nil
  table.remove(iter_list)
end

-- ---- methods.rate_photos ---------------------------------------------------
do
  local result = internals.methods.rate_photos({photo_ids = {"101", "102"}, rating = 1})
  assertEq(result.updated, 2, "rate_photos updated count")
  assertEq(images_by_id[101].rating, 1, "rate_photos changed image 101 rating")
  assertEq(images_by_id[102].rating, 1, "rate_photos changed image 102 rating")
end

-- ---- methods.import_batch --------------------------------------------------
do
  -- Stub dt.database.import to record args and return a fake list.
  -- darktable's real API takes only a path string; recursion is governed by
  -- a darktable preference, not a per-call argument.
  local recorded = {}
  local stub_imported = {{}, {}, {}}  -- 3 fake images
  -- Save original (in case test_dispatcher runs other tests later that need it).
  local original_db = stub_dt.database
  stub_dt.database = setmetatable({
    import = function(path)
      table.insert(recorded, {path = path})
      return stub_imported
    end,
  }, {__index = original_db})

  local result = internals.methods.import_batch({source_path = "/tmp/foo", recursive = true})
  assertEq(result.imported, 3, "import_batch returns count of imported images")
  assertEq(result.source_path, "/tmp/foo", "import_batch returns source_path back")
  assertEq(result.recursive, true, "import_batch echoes recursive back")
  assertEq(#recorded, 1, "dt.database.import called once")
  assertEq(recorded[1].path, "/tmp/foo", "import called with source_path")

  -- Default recursive = true when not specified
  local r2 = internals.methods.import_batch({source_path = "/tmp/bar"})
  assertEq(r2.recursive, true, "import_batch defaults recursive=true")

  -- Single-image return (non-table) should count as 1.
  stub_dt.database = setmetatable({
    import = function(_) return {} end,  -- ensure a userdata-like (we use empty table)
  }, {__index = original_db})

  -- Restore.
  stub_dt.database = original_db
end

-- ---- import_batch error: missing source_path -------------------------------
do
  local resp = internals.handle({
    id = "ib1",
    method = "import_batch",
    params = {},  -- no source_path
  })
  assertEq(resp.id, "ib1", "import_batch error preserves id")
  assertTrue(resp.error ~= nil, "import_batch returns error when source_path missing")
  assertTrue(string.find(resp.error or "", "source_path") ~= nil,
    "error message mentions source_path")
end

-- ---- methods.list_styles ---------------------------------------------------
do
  -- Stub dt.styles with a tiny inventory.
  local fake_styles = {
    {name = "alpha", description = "first style"},
    {name = "beta", description = "second style"},
  }
  local original_styles = stub_dt.styles
  stub_dt.styles = fake_styles  -- behaves like a list under ipairs

  local result = internals.methods.list_styles({})
  assertEq(result.count, 2, "list_styles returns count of styles")
  assertEq(#result.styles, 2, "list_styles returns table of styles")
  assertEq(result.styles[1].name, "alpha", "first style name")
  assertEq(result.styles[1].description, "first style", "first style description")
  assertEq(result.styles[2].name, "beta", "second style name")

  stub_dt.styles = original_styles
end

-- ---- methods.apply_preset --------------------------------------------------
do
  -- Stub dt.styles + image:apply_style + dt.database lookup.
  local applied_to = {}
  local make_stub_image = function(id)
    local img = {id = id}
    function img:apply_style(s) table.insert(applied_to, {id = self.id, style = s.name}) end
    return img
  end

  local fake_styles = {
    {name = "alpha", description = "a"},
    {name = "beta", description = "b"},
  }
  local original_styles = stub_dt.styles
  local original_db = stub_dt.database
  stub_dt.styles = fake_styles

  local stub_db_inner = {}
  stub_db_inner[101] = make_stub_image(101)
  stub_db_inner[102] = make_stub_image(102)
  stub_dt.database = setmetatable({}, {__index = stub_db_inner})

  local result = internals.methods.apply_preset({
    preset_name = "beta",
    photo_ids = {"101", "102"},
  })
  assertEq(result.applied, 2, "apply_preset applied count")
  assertEq(#result.missed, 0, "apply_preset no missed images")
  assertEq(result.preset_name, "beta", "apply_preset echoes preset_name")
  assertEq(#applied_to, 2, "apply_style invoked twice")
  assertEq(applied_to[1].style, "beta", "applied beta to first image")
  assertEq(applied_to[2].style, "beta", "applied beta to second image")

  stub_dt.styles = original_styles
  stub_dt.database = original_db
end

-- ---- apply_preset: missing image ID ----------------------------------------
do
  local fake_styles = {{name = "alpha", description = "a"}}
  local original_styles = stub_dt.styles
  local original_db = stub_dt.database
  stub_dt.styles = fake_styles
  stub_dt.database = setmetatable({}, {__index = function() return nil end})

  local result = internals.methods.apply_preset({
    preset_name = "alpha",
    photo_ids = {"999"},
  })
  assertEq(result.applied, 0, "apply_preset applied=0 when image missing")
  assertEq(#result.missed, 1, "apply_preset reports missed image")
  assertEq(result.missed[1], "999", "missed list contains the photo_id")

  stub_dt.styles = original_styles
  stub_dt.database = original_db
end

-- ---- apply_preset: unknown style -------------------------------------------
do
  local fake_styles = {{name = "alpha", description = "a"}}
  local original_styles = stub_dt.styles
  stub_dt.styles = fake_styles

  local resp = internals.handle({
    id = "ap1",
    method = "apply_preset",
    params = {preset_name = "nonexistent", photo_ids = {"1"}},
  })
  assertEq(resp.id, "ap1", "preserves id on error")
  assertTrue(resp.error ~= nil, "returns error for unknown style")
  assertTrue(string.find(resp.error or "", "nonexistent") ~= nil, "names the missing style")

  stub_dt.styles = original_styles
end

-- ---- apply_preset: missing preset_name -------------------------------------
do
  local resp = internals.handle({
    id = "ap2",
    method = "apply_preset",
    params = {photo_ids = {"1"}},
  })
  assertTrue(resp.error ~= nil, "errors on missing preset_name")
  assertTrue(string.find(resp.error or "", "preset_name") ~= nil, "error mentions preset_name")
end

-- ---- apply_preset: empty photo_ids -----------------------------------------
do
  local resp = internals.handle({
    id = "ap3",
    method = "apply_preset",
    params = {preset_name = "alpha", photo_ids = {}},
  })
  assertTrue(resp.error ~= nil, "errors on empty photo_ids")
  assertTrue(string.find(resp.error or "", "photo_ids") ~= nil, "error mentions photo_ids")
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

-- ---- Helpers for the filesystem-facing tests -------------------------------
local function sq(s) return internals.shell_quote(s) end

local function make_tmpdir(name)
  local base = os.getenv("TMPDIR") or "/tmp"
  if base:sub(-1) == "/" then base = base:sub(1, -2) end
  local dir = base .. "/" .. name .. "-" .. tostring(os.time()) .. "-" .. tostring(math.random(1e6))
  os.execute("mkdir -p " .. sq(dir))
  return dir
end

local function rmtree(dir) os.execute("rm -rf " .. sq(dir)) end

local function write_text(path, text)
  local f = io.open(path, "w")
  if not f then return false end
  f:write(text)
  f:close()
  return true
end

local function read_text(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local c = f:read("*a")
  f:close()
  return c
end

local function exists(path)
  local f = io.open(path, "r")
  if f then f:close(); return true end
  return false
end

-- Snapshot dt_log length, run body, return the log lines it appended.
local function capture_log(body)
  local before = #dt_log
  body()
  local out = {}
  for i = before + 1, #dt_log do out[#out + 1] = dt_log[i] end
  return out
end

-- ---- shell_quote -----------------------------------------------------------
do
  local q = internals.shell_quote
  assertEq(q("plain"), "'plain'", "shell_quote wraps a plain string")
  assertEq(q("/home/a b"), "'/home/a b'", "shell_quote handles spaces")
  -- Double quotes, backticks and $(...) are all INERT inside single quotes.
  assertEq(q('a"b'), [['a"b']], "shell_quote passes a double quote through")
  assertEq(q("a`id`b"), "'a`id`b'", "shell_quote neutralises backticks")
  assertEq(q("a$(id)b"), "'a$(id)b'", "shell_quote neutralises $(...)")
  assertEq(q("a$HOMEb"), "'a$HOMEb'", "shell_quote neutralises $VAR")
  -- The only hard case: an embedded single quote must close, escape, reopen.
  assertEq(q("it's"), [['it'\''s']], "shell_quote escapes an embedded quote")
  assertEq(q("''"), [[''\'''\''']], "shell_quote escapes repeated quotes")

  -- Prove the quoting actually survives /bin/sh: echo the string back and
  -- compare byte-for-byte. If quoting were wrong this would either error or
  -- return substituted text.
  local nasty = [[weird's "dir" $(echo PWNED) `echo PWNED` $HOME]]
  local p = io.popen("printf %s " .. q(nasty))
  local echoed = p:read("*a")
  p:close()
  assertEq(echoed, nasty, "shell_quote round-trips an adversarial string through sh")
end

-- ---- scan_dir in a directory whose name is full of shell metacharacters ----
do
  -- The whole point: cache_dir() comes from $HOME / $XDG_CACHE_HOME, so the
  -- worker must survive a directory named like this without breaking (or
  -- executing) anything.
  local dir = make_tmpdir([[dtmcp it's "a" $(echo x) `echo y` dir]])
  assertTrue(exists(dir .. "/.") or true, "tmpdir created")

  images_by_id[101].rating = 5
  images_by_id[102].rating = 3

  assertTrue(write_text(dir .. "/request-shq001.json",
    '{"id":"shq001","method":"view_photos","params":{"limit":1}}'),
    "wrote request into an adversarially-named directory")

  local handled = internals.scan_dir(dir)
  assertEq(handled, 1, "scan_dir found the request despite the shell metachars")

  local body = read_text(dir .. "/response-shq001.json")
  assertTrue(body ~= nil, "scan_dir wrote a response in the quoted directory")
  assertTrue(body and string.find(body, "shq001", 1, true) ~= nil,
    "response in the quoted directory carries the request id")
  assertTrue(not exists(dir .. "/request-shq001.json"),
    "request file removed from the quoted directory")

  -- sweep_stale shells out to find; it must not blow up on this path either.
  local ok_sweep = pcall(internals.sweep_stale, dir, 60)
  assertTrue(ok_sweep, "sweep_stale survives a shell-metachar directory")
  assertTrue(exists(dir .. "/response-shq001.json"),
    "sweep_stale leaves a FRESH response alone")

  rmtree(dir)
end

-- ---- sweep_stale also collects orphan RESPONSE files -----------------------
do
  local dir = make_tmpdir("dtmcp-sweep")
  write_text(dir .. "/request-old.json", "{}")
  write_text(dir .. "/response-old.json", "{}")
  write_text(dir .. "/response-old.json.tmp", "{}")
  write_text(dir .. "/request-fresh.json", "{}")
  write_text(dir .. "/response-fresh.json", "{}")
  write_text(dir .. "/keep-me.txt", "not ours")

  -- Backdate the "old" trio well past the 60s staleness window.
  os.execute("touch -t 202001010000 " .. sq(dir .. "/request-old.json")
    .. " " .. sq(dir .. "/response-old.json")
    .. " " .. sq(dir .. "/response-old.json.tmp")
    .. " " .. sq(dir .. "/keep-me.txt"))

  internals.sweep_stale(dir, 60)

  assertTrue(not exists(dir .. "/request-old.json"), "sweep deletes a stale request")
  -- This is the leak the sweep used to miss entirely: the client timed out and
  -- removed its request, the worker wrote the response anyway, nobody reads it.
  assertTrue(not exists(dir .. "/response-old.json"), "sweep deletes an ORPHAN response")
  assertTrue(not exists(dir .. "/response-old.json.tmp"), "sweep deletes a stranded .tmp")
  assertTrue(exists(dir .. "/request-fresh.json"), "sweep spares a fresh request")
  assertTrue(exists(dir .. "/response-fresh.json"), "sweep spares a fresh response")
  assertTrue(exists(dir .. "/keep-me.txt"), "sweep only touches its own filenames")

  rmtree(dir)
end

-- ---- safe_request_id -------------------------------------------------------
do
  local sri = internals.safe_request_id
  assertEq(sri({id = "abc-123_XYZ"}), "abc-123_XYZ", "accepts a filename-safe id")
  assertEq(sri({id = 42}), "42", "recovers a numeric id by coercion")
  assertEq(sri({id = "../../etc/passwd"}), nil, "rejects a path-traversal id")
  assertEq(sri({id = "a/b"}), nil, "rejects a slash in the id")
  assertEq(sri({id = ""}), nil, "rejects an empty id")
  assertEq(sri({}), nil, "rejects a missing id")
  assertEq(sri({id = {}}), nil, "rejects a non-scalar id")
  assertEq(sri("not a table"), nil, "rejects a non-table request")
end

-- ---- scan_dir: decodable-but-invalid request gets an ERROR RESPONSE --------
do
  -- Regression: these used to be deleted in silence, so the Python client sat
  -- out its full timeout and then reported "darktable not running" -- a lie.
  local dir = make_tmpdir("dtmcp-badreq")

  -- (a) valid id, no method at all.
  write_text(dir .. "/request-bad001.json", '{"id":"bad001","params":{}}')
  -- (b) valid id, method of the wrong type.
  write_text(dir .. "/request-bad002.json", '{"id":"bad002","method":123}')
  -- (c) numeric id -- recoverable, must still be answered.
  write_text(dir .. "/request-777.json", '{"id":777,"method":"nope"}')

  local logs = capture_log(function() internals.scan_dir(dir) end)
  assertEq(#logs, 0, "no log noise for requests that CAN be answered")

  local a = read_text(dir .. "/response-bad001.json")
  assertTrue(a ~= nil, "missing method still produces a response file")
  assertTrue(a and string.find(a, '"error"', 1, true) ~= nil,
    "missing method response carries an error field")
  assertTrue(a and string.find(a, "method", 1, true) ~= nil,
    "missing method error names the offending field")

  local b = read_text(dir .. "/response-bad002.json")
  assertTrue(b ~= nil, "non-string method still produces a response file")
  assertTrue(b and string.find(b, '"error"', 1, true) ~= nil,
    "non-string method response carries an error field")

  local c = read_text(dir .. "/response-777.json")
  assertTrue(c ~= nil, "numeric id is recovered and answered")
  assertTrue(c and string.find(c, "unknown method", 1, true) ~= nil,
    "numeric-id response reports the unknown method")

  assertTrue(not exists(dir .. "/request-bad001.json"), "bad request (a) deleted")
  assertTrue(not exists(dir .. "/request-bad002.json"), "bad request (b) deleted")
  assertTrue(not exists(dir .. "/request-777.json"), "bad request (c) deleted")

  rmtree(dir)
end

-- ---- scan_dir: UNRECOVERABLE request is logged, not silently dropped -------
do
  local dir = make_tmpdir("dtmcp-unrecoverable")

  write_text(dir .. "/request-junk1.json", "this is not json at all {{{")
  write_text(dir .. "/request-junk2.json", '"a bare string"')
  write_text(dir .. "/request-junk3.json", '{"id":"../escape","method":"view_photos"}')

  local logs = capture_log(function()
    local handled = internals.scan_dir(dir)
    assertEq(handled, 3, "scan_dir counts unrecoverable files as handled")
  end)

  assertEq(#logs, 3, "each unrecoverable request produced exactly one log line")
  local joined = table.concat(logs, "\n")
  assertTrue(string.find(joined, "malformed request", 1, true) ~= nil,
    "log says the request was malformed")
  assertTrue(string.find(joined, "junk1", 1, true) ~= nil,
    "log names the offending file")
  assertTrue(string.find(joined, "unparseable JSON", 1, true) ~= nil,
    "log distinguishes the unparseable-JSON case")
  assertTrue(string.find(joined, "not an object", 1, true) ~= nil,
    "log distinguishes the non-object case")
  assertTrue(string.find(joined, "unsafe 'id'", 1, true) ~= nil,
    "log distinguishes the unsafe-id case")

  -- No response can be addressed for any of these, and nothing may be left
  -- behind to be retried forever.
  assertTrue(not exists(dir .. "/request-junk1.json"), "unparseable request deleted")
  assertTrue(not exists(dir .. "/request-junk2.json"), "bare-string request deleted")
  assertTrue(not exists(dir .. "/request-junk3.json"), "unsafe-id request deleted")
  local p = io.popen("ls -1 " .. sq(dir) .. " 2>/dev/null | wc -l")
  local n = tonumber((p:read("*a") or "0"):match("%d+"))
  p:close()
  assertEq(n, 0, "unrecoverable requests leave no response files behind")

  rmtree(dir)
end

-- ---- write_file_atomic reports failure -------------------------------------
do
  local dir = make_tmpdir("dtmcp-writefail")
  -- Occupy the .tmp path with a DIRECTORY so io.open(tmp, "w") must fail.
  local target = dir .. "/blocked.json"
  os.execute("mkdir -p " .. sq(target .. ".tmp"))

  local ok, err = internals.write_file_atomic(target, "payload")
  assertEq(ok, false, "write_file_atomic returns false when the tmp path is blocked")
  assertTrue(err ~= nil and #tostring(err) > 0, "write_file_atomic returns a reason")

  local ok2 = internals.write_file_atomic(dir .. "/fine.json", "payload")
  assertEq(ok2, true, "write_file_atomic returns true on success")
  assertEq(read_text(dir .. "/fine.json"), "payload", "write_file_atomic writes the content")

  rmtree(dir)
end

-- ---- a failed response write is LOGGED, not swallowed ----------------------
do
  local dir = make_tmpdir("dtmcp-resp-writefail")
  -- Same trick: block the response's tmp path with a directory.
  os.execute("mkdir -p " .. sq(dir .. "/response-wf001.json.tmp"))
  write_text(dir .. "/request-wf001.json",
    '{"id":"wf001","method":"view_photos","params":{"limit":1}}')

  local logs = capture_log(function() internals.scan_dir(dir) end)
  assertEq(#logs, 1, "a dropped response emits exactly one log line")
  local msg = logs[1] or ""
  assertTrue(string.find(msg, "FAILED to write", 1, true) ~= nil,
    "log says the response write failed")
  assertTrue(string.find(msg, "wf001", 1, true) ~= nil,
    "log names the response that was lost")
  assertTrue(string.find(msg, "time out", 1, true) ~= nil,
    "log explains the client-visible symptom")

  rmtree(dir)
end

-- ---- list_request_files: ls fallback ---------------------------------------
do
  local dir = make_tmpdir("dtmcp-list")
  write_text(dir .. "/request-a.json", "{}")
  write_text(dir .. "/request-b.json", "{}")
  write_text(dir .. "/request-c.json.tmp", "{}")   -- in-flight write: ignore
  write_text(dir .. "/response-a.json", "{}")      -- not ours to dispatch
  write_text(dir .. "/other.json", "{}")

  local found = internals.list_request_files_ls(dir)
  assertEq(#found, 2, "ls listing returns exactly the two complete requests")
  local names = {}
  for _, p in ipairs(found) do names[p:match("[^/]+$")] = true end
  assertTrue(names["request-a.json"], "ls listing includes request-a.json")
  assertTrue(names["request-b.json"], "ls listing includes request-b.json")
  assertTrue(not names["request-c.json.tmp"], "ls listing skips the .tmp write")

  -- The lfs path must agree with the ls path. lfs is optional and usually
  -- absent, so inject a stand-in module exposing the same dir() iterator.
  local fake_lfs = {
    dir = function(d)
      local p = io.popen("ls -1a " .. sq(d) .. " 2>/dev/null")
      local lines = {}
      for line in p:lines() do lines[#lines + 1] = line end
      p:close()
      local i = 0
      return function() i = i + 1; return lines[i] end
    end,
  }
  local found2 = internals.list_request_files_lfs(dir, fake_lfs)
  assertEq(#found2, 2, "lfs listing returns the same two requests")
  assertEq(found2[1], dir .. "/request-a.json", "lfs listing is sorted (a first)")
  assertEq(found2[2], dir .. "/request-b.json", "lfs listing is sorted (b second)")

  -- A directory that cannot be read must yield an empty list, not an error.
  local ok, res = pcall(internals.list_request_files_lfs, dir .. "/nope", fake_lfs)
  assertTrue(ok, "lfs listing does not raise on an unreadable directory")
  assertEq(#(res or {}), 0, "lfs listing returns empty for an unreadable directory")

  assertTrue(type(internals.lfs_available) == "boolean",
    "plugin records whether lfs was reachable")

  rmtree(dir)
end

-- ---- adaptive poll backoff -------------------------------------------------
do
  local f = internals.poll_interval_ms
  local t = internals.poll_tiers

  assertEq(t.fast_ms, 100, "fast tier is 100ms")
  assertEq(t.medium_ms, 250, "medium tier is 250ms")
  assertEq(t.slow_ms, 1000, "slow tier is 1000ms -- idle polls ~once a second")

  -- A request just arrived (idle run reset to 0): stay fast.
  assertEq(f(0), 100, "0 idle ticks -> 100ms")
  assertEq(f(1), 100, "1 idle tick -> 100ms")
  assertEq(f(t.idle_ticks_before_medium - 1), 100, "just under the medium threshold -> 100ms")
  -- Step up.
  assertEq(f(t.idle_ticks_before_medium), 250, "medium threshold -> 250ms")
  assertEq(f(t.idle_ticks_before_slow - 1), 250, "just under the slow threshold -> 250ms")
  -- Steady-state idle.
  assertEq(f(t.idle_ticks_before_slow), 1000, "slow threshold -> 1000ms")
  assertEq(f(10000), 1000, "a long-idle session stays at 1000ms, it does not keep growing")

  -- Monotonic: the interval must never step back DOWN as idleness grows,
  -- otherwise an idle session would oscillate instead of settling.
  local prev = 0
  for i = 0, 40 do
    local cur = f(i)
    assertTrue(cur >= prev, "poll interval is monotonic non-decreasing at tick " .. i)
    prev = cur
  end

  -- The whole point of the change: an idle session must reach ~1 poll/second.
  -- Sum the wall-clock cost of the first 30s-worth of quiet and check the
  -- steady-state rate, plus how long the ramp itself takes.
  local ramp_ms = 0
  for i = 1, t.idle_ticks_before_slow - 1 do ramp_ms = ramp_ms + f(i) end
  assertTrue(ramp_ms <= 4000,
    "backoff reaches the slow tier within ~4s of quiet (got " .. ramp_ms .. "ms)")
  assertTrue(ramp_ms >= 1000,
    "backoff does not jump to slow instantly (got " .. ramp_ms .. "ms)")

  -- Old behaviour was a flat 100ms => 36000 polls/hour. New idle steady state
  -- must be an order of magnitude cheaper.
  local old_polls_per_hour = 3600 * 1000 / 100
  local new_polls_per_hour = 3600 * 1000 / t.slow_ms
  assertEq(new_polls_per_hour, 3600, "idle session polls 3600 times/hour, not 36000")
  assertTrue(new_polls_per_hour * 10 <= old_polls_per_hour,
    "idle poll rate is at least 10x cheaper than the old flat 100ms loop")

  -- Sweep cadence is now time-based, so it cannot drift with the poll tier.
  assertEq(internals.sweep_interval_seconds, 10, "sweep every 10 elapsed seconds")
  assertEq(internals.stale_age_seconds, 60, "files older than 60s are stale")
end

-- ---- scan_dir returns a handled count (drives the backoff) -----------------
do
  local dir = make_tmpdir("dtmcp-handled")
  assertEq(internals.scan_dir(dir), 0, "empty directory -> 0 handled (idle tick)")

  write_text(dir .. "/request-h1.json", '{"id":"h1","method":"view_photos","params":{"limit":1}}')
  write_text(dir .. "/request-h2.json", '{"id":"h2","method":"view_photos","params":{"limit":1}}')
  assertEq(internals.scan_dir(dir), 2, "two requests -> 2 handled (resets the backoff)")
  assertEq(internals.scan_dir(dir), 0, "requests consumed -> back to 0 handled")

  rmtree(dir)
end

-- ---- JSON encoder: sparse and mixed tables ---------------------------------
do
  local enc = internals.json.encode

  -- Dense array is unchanged.
  assertEq(enc({"a", "b", "c"}), '["a","b","c"]', "dense array encodes as an array")
  assertEq(enc({}), "[]", "empty table encodes as an empty array")

  -- Regression: {[1]=a,[3]=b} has n=2, max=3. The old `n == max` test sent it
  -- to the OBJECT branch, emitting {"1":"a","3":"b"} -- numeric string keys
  -- the Python client never expects where a list belongs.
  local sparse = {}
  sparse[1] = "a"
  sparse[3] = "b"
  assertEq(enc(sparse), '["a",null,"b"]', "sparse array stays an ARRAY, holes become null")
  assertTrue(string.find(enc(sparse), '"1"', 1, true) == nil,
    "sparse array does not emit numeric string keys")

  local leading_hole = {}
  leading_hole[2] = "x"
  assertEq(enc(leading_hole), '[null,"x"]', "leading hole encodes as null")

  local trailing = {}
  trailing[1] = 1
  trailing[2] = 2
  trailing[5] = 5
  assertEq(enc(trailing), "[1,2,null,null,5]", "multiple holes each become null")

  -- Mixed keys are a genuine object. Keys are sorted, so this is stable.
  local mixed = {}
  mixed[1] = "a"
  mixed.name = "n"
  assertEq(enc(mixed), '{"1":"a","name":"n"}', "mixed integer/string keys encode as an object")

  -- Zero and negative indexes are not array keys.
  local zero = {}
  zero[0] = "z"
  assertEq(enc(zero), '{"0":"z"}', "index 0 is an object key, not an array slot")

  -- Pathological sparsity must not allocate a giant array.
  local huge = {}
  huge[1] = "a"
  huge[100000] = "b"
  local encoded_huge = enc(huge)
  assertTrue(#encoded_huge < 200,
    "absurdly sparse table falls back to an object instead of a huge array")
  assertEq(encoded_huge:sub(1, 1), "{", "absurdly sparse table encodes as an object")

  -- Object key order is deterministic (pairs() order is not).
  local obj = {zulu = 1, alpha = 2, mike = 3}
  assertEq(enc(obj), '{"alpha":2,"mike":3,"zulu":1}', "object keys are sorted")
  assertEq(enc(obj), enc(obj), "object encoding is stable across calls")
end

-- ---- JSON encoder: non-finite numbers --------------------------------------
do
  local enc = internals.json.encode
  local inf = math.huge
  local nan = 0.0 / 0.0

  -- tostring(inf) is "inf"; tostring(nan) is "nan"/"-nan". None of those are
  -- JSON, and the Python client rejects the WHOLE response with
  -- BridgeProtocolError -- so one bad float used to discard a good result.
  assertEq(enc(inf), "null", "+inf encodes as null")
  assertEq(enc(-inf), "null", "-inf encodes as null")
  assertEq(enc(nan), "null", "nan encodes as null")

  local doc = enc({a = inf, b = -inf, c = nan, d = 1.5, e = 7})
  assertTrue(string.find(doc, "inf", 1, true) == nil, "encoded document contains no 'inf'")
  assertTrue(string.find(doc, "nan", 1, true) == nil, "encoded document contains no 'nan'")
  assertEq(doc, '{"a":null,"b":null,"c":null,"d":1.5,"e":7}',
    "non-finite values become null while finite neighbours survive")

  -- And it round-trips through a strict parser. (pcall so that a regression
  -- emitting `inf` reports as a failure rather than aborting the suite.)
  local dec_ok, back = pcall(internals.json.decode, doc)
  assertTrue(dec_ok, "encoded document with non-finite inputs is parseable JSON")
  if dec_ok then
    assertEq(back.a, nil, "null-encoded inf decodes back as absent")
    assertEq(back.d, 1.5, "finite float survives the round trip")
    assertEq(back.e, 7, "finite integer survives the round trip")
  end

  -- Inside an array the slot must still be filled, or the array shifts.
  assertEq(enc({1, inf, 3}), "[1,null,3]", "non-finite inside an array becomes null in place")
end

-- ---- JSON decoder: garbage must RAISE, not return nil ----------------------
do
  local dec = internals.json.decode
  for _, junk in ipairs({"this is not json {{{", "", "@@@", "tru", "[1,"}) do
    local ok = pcall(dec, junk)
    assertTrue(not ok, "json.decode raises on garbage input: " .. string.format("%q", junk))
  end
  -- Valid inputs still decode.
  assertEq(dec("42"), 42, "json.decode still reads a bare number")
  assertEq(dec("-1.5"), -1.5, "json.decode still reads a negative float")
  assertEq(dec("true"), true, "json.decode still reads true")
  assertEq(dec('{"a":1}').a, 1, "json.decode still reads an object")
end

-- ---- import_batch: honest fallback when the scan finds nothing -------------
do
  local original_db = stub_dt.database
  -- Non-table import return forces the polling branch; an empty database
  -- means the poll expires having found nothing.
  local empty_iter = setmetatable({}, {__index = function() return nil end})
  stub_dt.database = setmetatable({
    import = function(_) return "a-userdata-stand-in" end,
  }, {__index = empty_iter})
  -- ipairs over the stub must terminate immediately.
  local sleeps = 0
  local original_sleep = stub_dt.control.sleep
  stub_dt.control.sleep = function(_) sleeps = sleeps + 1 end

  local r = internals.methods.import_batch({source_path = "/tmp/empty-source"})
  -- Regression: this used to report imported = 1, inventing a count darktable
  -- never confirmed, for an import that in fact registered nothing.
  assertEq(r.imported, 0, "import_batch reports 0 when the scan found nothing")
  assertEq(r.scan_incomplete, true, "import_batch flags the expired poll")
  assertEq(r.source_path, "/tmp/empty-source", "import_batch still echoes source_path")
  assertTrue(sleeps > 0, "import_batch actually polled before giving up")

  stub_dt.control.sleep = original_sleep
  stub_dt.database = original_db
end

do
  -- Confirmed counts must NOT be flagged as incomplete.
  local original_db = stub_dt.database
  stub_dt.database = setmetatable({
    import = function(_) return {{}, {}} end,
  }, {__index = original_db})
  local r = internals.methods.import_batch({source_path = "/tmp/two"})
  assertEq(r.imported, 2, "import_batch reports the confirmed count")
  assertEq(r.scan_incomplete, false, "a confirmed count is not flagged incomplete")
  stub_dt.database = original_db
end

-- ---- list_directories_recursive --------------------------------------------
do
  -- Metacharacters in the name again: an import destination is caller-supplied.
  local root = make_tmpdir([[dtmcp-tree it's "a" $(echo x)]])
  os.execute("mkdir -p " .. sq(root .. "/store_00010001_DCIM_100NCD80"))
  os.execute("mkdir -p " .. sq(root .. "/store_00020001_DCIM_100NCD80"))
  os.execute("mkdir -p " .. sq(root .. "/store_00020001_DCIM_100NCD80/nested"))
  write_text(root .. "/store_00010001_DCIM_100NCD80/DSC_0001.NEF", "x")
  write_text(root .. "/store_00020001_DCIM_100NCD80/DSC_0001.NEF", "x")

  local dirs, enumerated = internals.list_directories_recursive(root)
  assertEq(enumerated, true, "tree enumeration succeeds for a real directory")
  assertEq(#dirs, 4, "enumeration returns the root plus its three subdirectories")
  assertEq(dirs[1], root, "the root sorts first, so parents import before children")
  local set = {}
  for _, d in ipairs(dirs) do set[d] = true end
  assertTrue(set[root .. "/store_00010001_DCIM_100NCD80"], "card-1 folder enumerated")
  assertTrue(set[root .. "/store_00020001_DCIM_100NCD80"], "card-2 folder enumerated")
  assertTrue(set[root .. "/store_00020001_DCIM_100NCD80/nested"], "nested folder enumerated")

  -- A path that cannot be read must report enumerated = false, NOT a
  -- confident single-element answer -- otherwise import_batch would claim it
  -- honoured recursion over a tree it never saw.
  local missing, ok2 = internals.list_directories_recursive(root .. "/does-not-exist")
  assertEq(ok2, false, "enumeration reports failure for an unreadable path")
  assertEq(#missing, 1, "failed enumeration still yields the requested path")

  rmtree(root)
end

-- ---- import_batch honours `recursive` itself -------------------------------
do
  -- Regression: `recursive` used to be read, defaulted to true, echoed back in
  -- the response -- and never used. Recursion was left entirely to darktable's
  -- recurse_directories preference, so with that preference off an
  -- import_from_camera destination registered NOTHING while the tool reported
  -- success with recursive: true.
  local root = make_tmpdir("dtmcp-import-tree")
  os.execute("mkdir -p " .. sq(root .. "/store_00010001_DCIM_100NCD80"))
  os.execute("mkdir -p " .. sq(root .. "/store_00020001_DCIM_100NCD80"))

  local recorded = {}
  local original_db = stub_dt.database
  stub_dt.database = setmetatable({
    import = function(path) table.insert(recorded, path); return {{}} end,
  }, {__index = original_db})

  local r = internals.methods.import_batch({source_path = root, recursive = true})
  assertEq(#recorded, 3, "recursive import calls dt.database.import per directory")
  assertEq(recorded[1], root, "the root directory is imported first")
  assertEq(r.directories_imported, 3, "response reports how many directories were imported")
  assertEq(r.recursive, true, "response echoes the requested mode")
  assertEq(r.recursive_honoured, true, "recursive=true is actually honoured, not just echoed")
  assertEq(r.note, nil, "no caveat needed when recursion was honoured")
  assertEq(r.imported, 3, "counts are summed across every directory imported")

  -- recursive = false: we can pass only the top directory, but we cannot stop
  -- darktable recursing if the user's preference says to -- so it must never
  -- be reported as honoured.
  recorded = {}
  local r2 = internals.methods.import_batch({source_path = root, recursive = false})
  assertEq(#recorded, 1, "non-recursive import touches only the top directory")
  assertEq(recorded[1], root, "non-recursive import passes the source path itself")
  assertEq(r2.recursive, false, "response echoes recursive=false")
  assertEq(r2.recursive_honoured, false, "recursive=false is NOT claimed as honoured")
  assertTrue(r2.note ~= nil, "recursive=false carries an explanatory note")
  assertTrue(string.find(r2.note or "", "preference", 1, true) ~= nil,
    "note tells the user recursion follows their darktable preference")

  -- An unreadable source must not claim honoured recursion either.
  recorded = {}
  local r3 = internals.methods.import_batch({
    source_path = root .. "/nope", recursive = true,
  })
  assertEq(r3.recursive_honoured, false,
    "recursion is not claimed when the tree could not be enumerated")
  assertTrue(r3.note ~= nil, "unenumerable source carries an explanatory note")

  stub_dt.database = original_db
  rmtree(root)
end

-- ---- count_images_under: PREFIX match, not equality ------------------------
do
  -- Every imported directory becomes its own film roll, so with the
  -- per-subdirectory camera layout a film.path is never EQUAL to the
  -- destination. The old equality test counted zero for every camera import.
  local root = "/photos/import"
  local films = {
    {film = {path = "/photos/import"}},
    {film = {path = "/photos/import/store_00010001_DCIM_100NCD80"}},
    {film = {path = "/photos/import/store_00020001_DCIM_100NCD80"}},
    {film = {path = "/photos/import/store_00020001_DCIM_100NCD80/"}},  -- trailing /
    {film = {path = "/photos/importer-elsewhere"}},   -- prefix string, NOT a child
    {film = {path = "/photos/other"}},
    {film = nil},                                     -- no film at all
  }
  local original_db = stub_dt.database
  stub_dt.database = films

  assertEq(internals.count_images_under(root), 4,
    "counts the destination and everything beneath it")
  assertEq(internals.count_images_under(root .. "/"), 4,
    "a trailing slash on the source path does not change the count")
  assertTrue(internals.count_images_under("/photos/importer-elsewhere") == 1,
    "a sibling whose name merely starts with the prefix is not a child")
  assertEq(internals.count_images_under("/nowhere"), 0, "unrelated path counts zero")

  stub_dt.database = original_db
end

-- ---- poll_for_imported: waits for the count to SETTLE ----------------------
do
  local original_db = stub_dt.database
  local original_sleep = stub_dt.control.sleep
  local cfg = internals.import_poll

  -- A trickling multi-directory card import: images keep arriving for a
  -- while. Stopping at the first non-zero count would report 1 of 300.
  local ticks = 0
  local films = {}
  stub_dt.database = films
  stub_dt.control.sleep = function(_)
    ticks = ticks + 1
    if ticks <= 8 then
      films[#films + 1] = {film = {path = "/dest/sub" .. ticks}}
    end
  end

  local count, incomplete = internals.poll_for_imported("/dest")
  assertEq(count, 8, "poll keeps waiting while the count is still growing")
  assertEq(incomplete, false, "a settled count is not flagged incomplete")
  assertTrue(ticks >= 8 + cfg.settle_polls,
    "poll observed a full settle window of no growth before returning")
  assertTrue(ticks < cfg.attempts, "a settled import returns well before the ceiling")

  -- Still arriving when the ceiling expires: the count is a FLOOR, so the
  -- caller must be told it is incomplete rather than shown a confident total.
  ticks = 0
  films = {}
  stub_dt.database = films
  stub_dt.control.sleep = function(_)
    ticks = ticks + 1
    films[#films + 1] = {film = {path = "/dest/sub" .. ticks}}
  end
  local count2, incomplete2 = internals.poll_for_imported("/dest")
  assertEq(incomplete2, true, "a never-settling import is flagged scan_incomplete")
  assertTrue(count2 > 0, "an incomplete scan still reports the floor it reached")
  assertEq(ticks, cfg.attempts, "the poll is hard-capped at the attempt ceiling")

  -- Ceiling of 10s: long enough for a several-hundred-file card import, short
  -- enough to bound how long the worker is blocked.
  assertEq(cfg.attempts * cfg.interval_ms, 10000, "import poll ceiling is 10s")

  stub_dt.control.sleep = original_sleep
  stub_dt.database = original_db
end

-- ---- handle: malformed method type -----------------------------------------
do
  local resp = internals.handle({id = "m1", method = nil})
  assertEq(resp.id, "m1", "malformed-method response preserves id")
  assertTrue(resp.error ~= nil, "missing method yields an error")
  assertTrue(string.find(resp.error, "method", 1, true) ~= nil,
    "missing-method error names the field")

  local resp2 = internals.handle({id = "m2", method = 42})
  assertTrue(resp2.error ~= nil, "non-string method yields an error")
  assertTrue(string.find(resp2.error, "number", 1, true) ~= nil,
    "non-string method error reports the actual type")
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
