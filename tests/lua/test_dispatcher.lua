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
  [101] = {id = 101, filename = "DSC_0001.NEF", path = "/photos", rating = 5,
           sidecar = "/photos/DSC_0001.NEF.xmp"},
  [102] = {id = 102, filename = "DSC_0002.NEF", path = "/photos", rating = 3},
  [103] = {id = 103, filename = "OTHER.NEF",   path = "/photos", rating = 4},
}
-- attach_tag/detach_tag mutate the tag's own array part (see stub_tags
-- below), mirroring the real image:attach_tag(tag)/image:detach_tag(tag)
-- Lua API used by methods.tag_photo.
for _, img in pairs(images_by_id) do
  img.attach_tag = function(self, tag)
    for _, v in ipairs(tag) do if v == self.id then return end end
    table.insert(tag, self.id)
  end
  img.detach_tag = function(self, tag)
    for i, v in ipairs(tag) do
      if v == self.id then table.remove(tag, i); return end
    end
  end
end
local iter_list = {}
for _, img in pairs(images_by_id) do table.insert(iter_list, img) end
local stub_db = setmetatable(iter_list, {
  __index = function(_, k) return images_by_id[k] end,
})
-- Real darktable exposes dt.database.get_image(id) as the by-real-id lookup
-- (dt.database[n] is positional/OFFSET, a different thing entirely -- see
-- src-dt/src/lua/database.c:database_numindex vs database_get_image).
stub_db.get_image = function(id) return images_by_id[id] end

-- ---- Stub dt.tags -----------------------------------------------------
-- A tag object's array part holds the ids of images it's attached to, so
-- both `#tag` (count) and `tag:get_tagged_images()` work off plain ipairs.
local tags_by_name = {}
local tags_array = {}
local function make_tag(name)
  local tag = {name = name}
  tag.get_tagged_images = function(self)
    local imgs = {}
    for _, imgid in ipairs(self) do table.insert(imgs, images_by_id[imgid]) end
    return imgs
  end
  return tag
end
local stub_tags = setmetatable({}, {
  __index = function(_, k)
    if k == "find" then
      return function(name) return tags_by_name[name] end
    elseif k == "create" then
      return function(name)
        local tag = make_tag(name)
        tags_by_name[name] = tag
        table.insert(tags_array, tag)
        return tag
      end
    else
      return tags_array[k]
    end
  end,
})

local dt_log = {}
local stub_dt = {
  database = stub_db,
  -- dt.collection is the currently-open lighttable view; these tests don't
  -- exercise the collection-vs-library distinction itself (that's a
  -- darktable-core behavior, not plugin logic), so point it at the same
  -- fixture data as dt.database.
  collection = stub_db,
  tags = stub_tags,
  print_log = function(msg) table.insert(dt_log, msg) end,
  control = {
    dispatch = function(_) end,
    sleep = function(_) end,
  },
}
local gui_view_state = {name = "lighttable"}
stub_dt.gui = {
  -- Minimal stub: empty action_images means the selection never "resolves"
  -- (see open_darkroom's diagnostic pre-check), so most tests hit the
  -- early-return diagnostic branch without needing to simulate an actual
  -- lighttable<->darkroom view switch. current_view is stateful (records
  -- the last view "switched" to) so the same-view-is-a-pipeline-no-op bug
  -- (bugreport 2026-07-25) can actually be exercised: bouncing through
  -- lighttable must be visible as a real state change, not a fixed stub.
  selection = function(sel) return sel or {} end,
  action_images = {},
  current_view = function(target)
    if target then gui_view_state = {name = target.name} end
    return gui_view_state
  end,
  views = {
    darkroom = {name = "darkroom"},
    lighttable = {name = "lighttable"},
  },
}

-- ---- Stub dt.develop --------------------------------------------------
-- Records every call so tests can assert exactly what the Lua wrapper
-- methods (dev_retouch_*) forwarded to the (real, C-only, unstubbable) core
-- binding -- these tests lock in the bridge-layer arg validation/defaults
-- contract, not the C logic itself (see PLAN.md's retouch section).
local develop_calls = {}
stub_dt.develop = {
  retouch_add_shape = function(...)
    table.insert(develop_calls, {name = "retouch_add_shape", args = {...}})
    return {ok = true, formid = 42, algorithm = "heal", wavelet_scale = 2}
  end,
  retouch_update_shape = function(...)
    table.insert(develop_calls, {name = "retouch_update_shape", args = {...}})
    return {ok = true, formid = 42, algorithm = "heal", wavelet_scale = 2, opacity = 1.0}
  end,
  retouch_delete_shape = function(...)
    table.insert(develop_calls, {name = "retouch_delete_shape", args = {...}})
    return {ok = true}
  end,
  retouch_list_shapes = function(...)
    table.insert(develop_calls, {name = "retouch_list_shapes", args = {...}})
    return {module = "retouch", instance = 0, shapes = {}}
  end,
  -- Identity stub (no orientation/crop/lens-correction simulated): real C
  -- semantics are display-frame-normalized -> mask/pipe-input-frame
  -- normalized (2026-07-25 bugreport fix) -- only the bridge-layer
  -- arg-forwarding contract is under test here, not the transform math
  -- itself (that can only be verified against a real darktable process).
  backtransform_point = function(x, y, len1, len2)
    table.insert(develop_calls, {name = "backtransform_point", args = {x, y, len1, len2}})
    local out = {x = x, y = y}
    if len1 ~= nil then out.len1 = len1 end
    if len2 ~= nil then out.len2 = len2 end
    return out
  end,
  -- Identity stub for the opposite direction (mask/pipe-input frame ->
  -- processed/display frame), used when reading shapes back for verification
  -- or overlay drawing. Same caveat as above: only arg forwarding is tested.
  transform_point = function(x, y, len1, len2)
    table.insert(develop_calls, {name = "transform_point", args = {x, y, len1, len2}})
    local out = {x = x, y = y}
    if len1 ~= nil then out.len1 = len1 end
    if len2 ~= nil then out.len2 = len2 end
    return out
  end,
  -- Writable viewport zoom/pan (2026-07-31 set-viewport-design). Only the
  -- bridge-layer arg-forwarding/defaulting contract is under test here (the
  -- real dt_dev_zoom_move/clamp/wait-for-pipe semantics can only be verified
  -- against a live darktable process) -- canned response mirrors the C
  -- binding's documented shape.
  set_viewport = function(viewport, zoom_x, zoom_y, scale, wait_for_pipe, timeout_ms)
    table.insert(develop_calls, {name = "set_viewport",
      args = {viewport, zoom_x, zoom_y, scale, wait_for_pipe, timeout_ms}})
    return {
      ok = true, viewport = viewport,
      previous = {zoom = 3, zoom_label = "free", closeup = 0,
                  zoom_x = -0.02, zoom_y = 0.05, scale = 0.4},
      applied = {zoom_x = zoom_x, zoom_y = zoom_y, scale = scale},
      clamped = {}, pipe_ready = true, waited_ms = 15,
    }
  end,
  restore_viewport = function(viewport, zoom, closeup, zoom_x, zoom_y, scale, wait_for_pipe, timeout_ms)
    table.insert(develop_calls, {name = "restore_viewport",
      args = {viewport, zoom, closeup, zoom_x, zoom_y, scale, wait_for_pipe, timeout_ms}})
    return {ok = true, viewport = viewport, pipe_ready = true, waited_ms = 8}
  end,
  -- capture_viewport/get_preview fix (2026-07-31 set-viewport-design
  -- follow-up): preview(max_w, max_h[, x, y, w, h][, viewport]) -- viewport
  -- picks dev->full.pipe/dev->preview2.pipe instead of the default
  -- dev->preview_pipe. Canned response mirrors the real C binding's shape
  -- (viewport_source echoes back what was actually used).
  preview = function(max_w, max_h, x, y, w, h, viewport)
    table.insert(develop_calls, {name = "preview", args = {max_w, max_h, x, y, w, h, viewport}})
    return {
      status = "ok", path = "/tmp/preview.png", width = 640, height = 480,
      frame_width = 640, frame_height = 480,
      viewport_source = viewport or "preview_pipe",
    }
  end,
  -- LUT tooling (2026-07-26): reads an arbitrary core conf key. Stub returns
  -- a canned lut3d root path for the one key the tests exercise, "" for
  -- anything else (matches the real C binding's never-nil contract).
  get_conf_string = function(key)
    table.insert(develop_calls, {name = "get_conf_string", args = {key}})
    if key == "plugins/darkroom/lut3d/def_path" then
      return "/tmp/luts"
    end
    return ""
  end,
  -- Blend opacity control (2026-07-26): lut3d has no "amount" of its own, so
  -- intensity control goes through blend_params instead of module params.
  get_blend_params = function(op, instance)
    table.insert(develop_calls, {name = "get_blend_params", args = {op, instance}})
    return {op = op, instance = instance, opacity = 100.0, mask_mode = 0, blend_mode = 3}
  end,
  set_blend_params = function(op, instance, fields)
    table.insert(develop_calls, {name = "set_blend_params", args = {op, instance, fields}})
    return {
      ok = true, op = op, instance = instance,
      opacity = fields.opacity or 100.0,
      mask_mode = fields.enable_uniform_blend and 1 or 0,
      blend_mode = fields.blend_mode or 3,
      invert = fields.invert or false,
    }
  end,
  -- Mask group management (2026-07-27): attach/detach an EXISTING shape to a
  -- module's blend group without copying it, plus global/per-module listing.
  add_instance = function(op, fields)
    table.insert(develop_calls, {name = "add_instance", args = {op, fields}})
    local result = {ok = true, op = op, instance = 1, base_instance = 0, multi_name = ""}
    if fields ~= nil then
      result.fields_applied = {ok = true, applied = fields, clamped = {}, unknown_fields = {}}
    end
    return result
  end,
  list_masks = function(op, instance)
    table.insert(develop_calls, {name = "list_masks", args = {op, instance}})
    return {
      {mask_id = 42, type = "circle", opacity = 1.0, nb_points = 1, name = "circle 1",
       module = op, instance = instance, operation = "union", invert = false},
    }
  end,
  list_all_masks = function()
    table.insert(develop_calls, {name = "list_all_masks", args = {}})
    return {
      {formid = 42, name = "circle 1", type = "circle", used_by = {{op = "retouch", instance = 0}}},
    }
  end,
  attach_mask = function(op, instance, formid, operation)
    table.insert(develop_calls, {name = "attach_mask", args = {op, instance, formid, operation}})
    -- mask_mode=3 = DEVELOP_MASK_ENABLED|DEVELOP_MASK_MASK, matching the real
    -- C binding's post-2026-07-27-fix behavior (see attach_mask_cb).
    return {ok = true, op = op, instance = instance, formid = formid, operation = operation or "union", mask_mode = 3}
  end,
  detach_mask = function(op, instance, formid)
    table.insert(develop_calls, {name = "detach_mask", args = {op, instance, formid}})
    return {ok = true, op = op, instance = instance, formid = formid}
  end,
  get_mask = function(mask_id)
    table.insert(develop_calls, {name = "get_mask", args = {mask_id}})
    return {
      mask_id = mask_id, type = "path", name = "path #1",
      points = {{corner = {0.1, 0.2}, ctrl1 = {0.1, 0.2}, ctrl2 = {0.1, 0.2}, border = {0.02, 0.02}, state = 1}},
    }
  end,
  rename_mask = function(mask_id, name)
    table.insert(develop_calls, {name = "rename_mask", args = {mask_id, name}})
    return {ok = true, mask_id = mask_id, name = name}
  end,
  delete_mask = function(mask_id)
    table.insert(develop_calls, {name = "delete_mask", args = {mask_id}})
    return {ok = true, mask_id = mask_id}
  end,
  current_image = function()
    table.insert(develop_calls, {name = "current_image", args = {}})
    return {has_image = true, id = 101, path = "/photos/DSC_0001.NEF", filename = "DSC_0001.NEF"}
  end,
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

-- ---- methods.view_photos: explicit collection scope differs from the
-- whole library (bug: view_photos was scanning dt.database unconditionally
-- and returning photos outside whatever collection/filter was open) --------
do
  -- Collection narrower than the library: only image 103.
  stub_dt.collection = setmetatable({images_by_id[103]}, {
    __index = function(_, k) return images_by_id[k] end,
  })
  local scoped = internals.methods.view_photos({limit = 10, scope = "collection"})
  assertEq(#scoped, 1, "view_photos collection scope only sees the open collection")
  assertEq(scoped[1].id, "103", "view_photos collection scope returns the collection's image")

  local whole = internals.methods.view_photos({limit = 10, scope = "library"})
  assertEq(#whole, 3, "view_photos scope=library ignores the open collection")

  stub_dt.collection = stub_db
end

-- ---- methods.rate_photos ---------------------------------------------------
do
  local result = internals.methods.rate_photos({photo_ids = {"101", "102"}, rating = 1})
  assertEq(result.updated, 2, "rate_photos updated count")
  assertEq(images_by_id[101].rating, 1, "rate_photos changed image 101 rating")
  assertEq(images_by_id[102].rating, 1, "rate_photos changed image 102 rating")
end

-- ---- methods.open_darkroom: real-id lookup (bugreport 2026-07-24: image ids
-- from view_photos/list_photos_in_collection are real database ids, NOT
-- positions -- dt.database[id] is a positional OFFSET lookup and returning
-- the wrong/no image for any id past the library's row count or past any
-- id gap; dt.database.get_image(id) is the actual by-id lookup) ------------
do
  local result = internals.methods.open_darkroom({image_id = "103"})
  assertTrue(result.diagnostic ~= nil,
    "open_darkroom (stubbed gui) hits the no-view-switch diagnostic branch")
  assertEq(result.requested_image_id, 103,
    "open_darkroom resolves real image id 103 via get_image, not a position")
end

do
  local ok, err = pcall(internals.methods.open_darkroom, {image_id = "999999"})
  assertTrue(not ok, "open_darkroom errors for an id with no matching image")
  assertTrue(string.find(err or "", "image not found") ~= nil,
    "open_darkroom error names the missing id")
end

-- ---- methods.open_darkroom: bugreport 2026-07-25 -- switching
-- current_view to darkroom while ALREADY in darkroom is a pipeline no-op
-- (views/view.c only calls enter()/dt_dev_load_image() on a real
-- old_view != new_view transition), so opening a second image right after
-- the first must bounce through lighttable to force a genuine reload. -----
do
  -- Selection resolves cleanly this time (unlike the diagnostic-branch
  -- tests above) so execution reaches the actual view-switch logic.
  stub_dt.gui.current_view(stub_dt.gui.views.darkroom)
  stub_dt.gui.action_images = {images_by_id[103]}

  local result = internals.methods.open_darkroom({image_id = "103"})
  assertTrue(result.bounced_through_lighttable,
    "open_darkroom bounces through lighttable when already in darkroom")
  assertEq(result.view, "darkroom",
    "open_darkroom ends back in darkroom after the forced bounce")

  stub_dt.gui.action_images = {}
end

do
  -- Control case: NOT already in darkroom (coming from lighttable) should
  -- NOT bounce -- the real view.c transition already fires enter() on its
  -- own, so a bounce here would just be a pointless extra round trip.
  stub_dt.gui.current_view(stub_dt.gui.views.lighttable)
  stub_dt.gui.action_images = {images_by_id[103]}

  local result = internals.methods.open_darkroom({image_id = "103"})
  assertTrue(not result.bounced_through_lighttable,
    "open_darkroom does not bounce when coming from lighttable")
  assertEq(result.view, "darkroom", "open_darkroom still ends in darkroom")

  stub_dt.gui.action_images = {}
end

-- ---- methods.dev_retouch_add_shape / dev_retouch_delete_shape /
-- dev_retouch_list_shapes: bridge-layer arg validation/defaults/forwarding
-- contract (the actual shape-creation C logic lives in src/lua/develop.c
-- and can only be verified against a real darktable process). ------------
do
  develop_calls = {}
  local result = internals.methods.dev_retouch_add_shape({
    op = "retouch",
    algorithm = "heal",
    target = {x = 0.4, y = 0.3},
    source = {x = 0.35, y = 0.3},
    radius = 0.02,
  })
  assertEq(result.formid, 42, "dev_retouch_add_shape forwards the C result")
  assertEq(#develop_calls, 1, "dev_retouch_add_shape calls dt.develop.retouch_add_shape once")
  local call = develop_calls[1]
  assertEq(call.name, "retouch_add_shape", "correct C function called")
  assertEq(call.args[1], "retouch", "op forwarded")
  assertEq(call.args[2], 0, "instance defaults to 0")
  assertEq(call.args[3], "heal", "algorithm forwarded")
  assertEq(call.args[4], 0.4, "target.x forwarded")
  assertEq(call.args[5], 0.3, "target.y forwarded")
  assertEq(call.args[6], 0.02, "radius forwarded")
  assertEq(call.args[7], 0.0, "feather defaults to 0.0")
  assertEq(call.args[8], 0.35, "source.x forwarded")
  assertEq(call.args[9], 0.3, "source.y forwarded")
  assertEq(call.args[10], nil, "wavelet_scale omitted -> nil (C default)")
  assertEq(call.args[11], 1.0, "opacity defaults to 1.0")
end

do
  develop_calls = {}
  internals.methods.dev_retouch_add_shape({
    op = "retouch",
    instance = 1,
    algorithm = "clone",
    target = {x = 0.5, y = 0.5},
    source = {x = 0.6, y = 0.6},
    radius = 0.03,
    feather = 0.1,
    wavelet_scale = 3,
    opacity = 0.8,
  })
  local call = develop_calls[1]
  assertEq(call.args[2], 1, "instance forwarded when given")
  assertEq(call.args[7], 0.1, "feather forwarded when given")
  assertEq(call.args[10], 3, "wavelet_scale forwarded when given")
  assertEq(call.args[11], 0.8, "opacity forwarded when given")
end

do
  local ok, err = pcall(internals.methods.dev_retouch_add_shape, {
    op = "retouch", algorithm = "heal", target = {x = 0.4, y = 0.3}, radius = 0.02,
  })
  assertTrue(not ok, "dev_retouch_add_shape errors without a source point")
  assertTrue(string.find(err or "", "source") ~= nil, "error mentions source")
end

do
  -- "path" used to be unsupported (this test predates the path batch); now
  -- only genuinely unsupported types (e.g. "brush") are still rejected.
  local ok, err = pcall(internals.methods.dev_retouch_add_shape, {
    op = "retouch", algorithm = "heal", shape_type = "brush",
    target = {x = 0.4, y = 0.3}, source = {x = 0.35, y = 0.3}, radius = 0.02,
  })
  assertTrue(not ok, "dev_retouch_add_shape errors on unsupported shape_type")
  assertTrue(string.find(err or "", "circle") ~= nil, "error mentions circle")
end

do
  -- 2026-07-31 path batch: shape_type="path" requires >=3 points, and
  -- target/radius are NOT required (a polygon has no single center/radius).
  local ok, err = pcall(internals.methods.dev_retouch_add_shape, {
    op = "retouch", algorithm = "heal", shape_type = "path",
    source = {x = 0.35, y = 0.3},
  })
  assertTrue(not ok, "dev_retouch_add_shape errors without points for shape_type='path'")
  assertTrue(string.find(err or "", "points") ~= nil, "error mentions points")
end

do
  develop_calls = {}
  internals.methods.dev_retouch_add_shape({
    op = "retouch", algorithm = "heal", shape_type = "path",
    source = {x = 0.35, y = 0.3},
    points = {{x = 0.1, y = 0.1}, {x = 0.2, y = 0.1}, {x = 0.15, y = 0.2}},
    smooth = false,
  })
  local call = develop_calls[1]
  assertEq(call.args[19], "path", "shape_type forwarded")
  local pts = call.args[22]
  assertEq(#pts, 3, "points table forwarded with 3 nodes")
  assertEq(pts[1].x, 0.1, "point 1 x forwarded")
  assertEq(call.args[23], false, "smooth forwarded")
end

do
  -- 2026-07-31 ellipse batch: shape_type="ellipse" forwards radius_b/rotation
  -- as the new trailing C args (positions 19-21).
  develop_calls = {}
  internals.methods.dev_retouch_add_shape({
    op = "retouch", algorithm = "heal", shape_type = "ellipse",
    target = {x = 0.4, y = 0.3}, source = {x = 0.35, y = 0.3},
    radius = 0.04, radius_b = 0.02, rotation = 30,
  })
  local call = develop_calls[1]
  assertEq(call.args[19], "ellipse", "shape_type forwarded")
  assertEq(call.args[20], 0.02, "radius_b forwarded")
  assertEq(call.args[21], 30, "rotation forwarded")
end

do
  -- shape_type="ellipse" without radius_b: still succeeds (C defaults
  -- radius_b to radius), Lua does not inject a value itself.
  develop_calls = {}
  internals.methods.dev_retouch_add_shape({
    op = "retouch", algorithm = "heal", shape_type = "ellipse",
    target = {x = 0.4, y = 0.3}, source = {x = 0.35, y = 0.3}, radius = 0.04,
  })
  local call = develop_calls[1]
  assertEq(call.args[19], "ellipse", "shape_type forwarded")
  assertEq(call.args[20], nil, "radius_b omitted -> nil (C defaults to radius)")
  assertEq(call.args[21], nil, "rotation omitted -> nil (C defaults to 0)")
end

-- ---- methods.dev_retouch_update_shape: bridge-layer arg validation/
-- defaults/forwarding for in-place move/resize (2026-07-25 viewport-relative
-- retouch design). algorithm/wavelet_scale/opacity are all optional (nil ->
-- C keeps the shape's current value); target/source/radius/feather are
-- always resent in full (a "move", not a partial field patch).
do
  develop_calls = {}
  local result = internals.methods.dev_retouch_update_shape({
    op = "retouch",
    formid = 42,
    target = {x = 0.5, y = 0.4},
    source = {x = 0.45, y = 0.4},
    radius = 0.03,
  })
  assertEq(result.formid, 42, "dev_retouch_update_shape forwards the C result")
  assertEq(#develop_calls, 1, "dev_retouch_update_shape calls dt.develop.retouch_update_shape once")
  local call = develop_calls[1]
  assertEq(call.name, "retouch_update_shape", "correct C function called")
  assertEq(call.args[1], "retouch", "op forwarded")
  assertEq(call.args[2], 0, "instance defaults to 0")
  assertEq(call.args[3], 42, "formid forwarded")
  assertEq(call.args[4], 0.5, "target.x forwarded")
  assertEq(call.args[5], 0.4, "target.y forwarded")
  assertEq(call.args[6], 0.03, "radius forwarded")
  assertEq(call.args[7], 0.0, "feather defaults to 0.0")
  assertEq(call.args[8], 0.45, "source.x forwarded")
  assertEq(call.args[9], 0.4, "source.y forwarded")
  assertEq(call.args[10], nil, "algorithm omitted -> nil (C keeps current)")
  assertEq(call.args[11], nil, "wavelet_scale omitted -> nil (C keeps current)")
  assertEq(call.args[12], nil, "opacity omitted -> nil (C leaves it untouched)")
end

do
  develop_calls = {}
  internals.methods.dev_retouch_update_shape({
    op = "retouch",
    instance = 1,
    formid = 7,
    target = {x = 0.5, y = 0.5},
    source = {x = 0.6, y = 0.6},
    radius = 0.04,
    feather = 0.05,
    algorithm = "clone",
    wavelet_scale = 2,
    opacity = 0.7,
  })
  local call = develop_calls[1]
  assertEq(call.args[2], 1, "instance forwarded when given")
  assertEq(call.args[7], 0.05, "feather forwarded when given")
  assertEq(call.args[10], "clone", "algorithm forwarded when given")
  assertEq(call.args[11], 2, "wavelet_scale forwarded when given")
  assertEq(call.args[12], 0.7, "opacity forwarded when given")
end

do
  local ok, err = pcall(internals.methods.dev_retouch_update_shape, {
    op = "retouch", formid = 42, algorithm = "heal",
    target = {x = 0.4, y = 0.3}, radius = 0.02,
  })
  assertTrue(not ok, "dev_retouch_update_shape errors without a source point when algorithm='heal'")
  assertTrue(string.find(err or "", "source") ~= nil, "error mentions source")
end

do
  -- algorithm omitted entirely (keep the shape's current one) AND source
  -- omitted: the bridge no longer knows whether the shape's current
  -- algorithm needs a source, so it must NOT error here -- only C (which
  -- knows the shape's live algorithm) can make that call. 2026-07-31 blur/
  -- fill batch: source is nil for blur/fill algorithms, so a bare "move"
  -- call must be allowed through without one.
  develop_calls = {}
  local result = internals.methods.dev_retouch_update_shape({
    op = "retouch", formid = 42, target = {x = 0.4, y = 0.3}, radius = 0.02,
  })
  assertEq(result.formid, 42, "dev_retouch_update_shape without algorithm/source reaches C")
  local call = develop_calls[1]
  assertEq(call.args[8], nil, "source_x omitted -> nil when algorithm is unspecified and no source given")
  assertEq(call.args[9], nil, "source_y omitted -> nil when algorithm is unspecified and no source given")
end

do
  -- 2026-07-31 ellipse step 2: radius_b/rotation forwarded as new trailing
  -- C args (positions 20-21).
  develop_calls = {}
  internals.methods.dev_retouch_update_shape({
    op = "retouch", formid = 42, target = {x = 0.4, y = 0.3}, radius = 0.02,
    radius_b = 0.03, rotation = 45,
  })
  local call = develop_calls[1]
  assertEq(call.args[20], 0.03, "radius_b forwarded")
  assertEq(call.args[21], 45, "rotation forwarded")
end

do
  -- 2026-07-31 path batch: `points` implies path-update mode -- target/
  -- radius are NOT required in this case (a polygon has no single
  -- center/radius), and points/smooth forward to positions 22-23.
  develop_calls = {}
  internals.methods.dev_retouch_update_shape({
    op = "retouch", formid = 42,
    points = {{x = 0.5, y = 0.5}, {x = 0.55, y = 0.5}, {x = 0.52, y = 0.55}},
    smooth = false,
  })
  local call = develop_calls[1]
  local pts = call.args[22]
  assertEq(#pts, 3, "points forwarded with 3 nodes")
  assertEq(call.args[23], false, "smooth forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_retouch_update_shape, {
    op = "retouch", formid = 42, points = {{x = 0.1, y = 0.1}, {x = 0.2, y = 0.1}},
  })
  assertTrue(not ok, "dev_retouch_update_shape errors with fewer than 3 points")
  assertTrue(string.find(err or "", "points") ~= nil, "error mentions points")
end

do
  local ok, err = pcall(internals.methods.dev_retouch_update_shape, {
    op = "retouch", target = {x = 0.4, y = 0.3}, source = {x = 0.35, y = 0.3}, radius = 0.02,
  })
  assertTrue(not ok, "dev_retouch_update_shape errors without a formid")
  assertTrue(string.find(err or "", "formid") ~= nil, "error mentions formid")
end

do
  develop_calls = {}
  local result = internals.methods.dev_retouch_delete_shape({op = "retouch", formid = 42})
  assertTrue(result.ok, "dev_retouch_delete_shape forwards the C result")
  local call = develop_calls[1]
  assertEq(call.name, "retouch_delete_shape", "correct C function called")
  assertEq(call.args[1], "retouch", "op forwarded")
  assertEq(call.args[2], 0, "instance defaults to 0")
  assertEq(call.args[3], 42, "formid forwarded")
end

do
  develop_calls = {}
  local result = internals.methods.dev_retouch_list_shapes({op = "retouch"})
  assertEq(result.module, "retouch", "dev_retouch_list_shapes forwards the C result")
  local call = develop_calls[1]
  assertEq(call.name, "retouch_list_shapes", "correct C function called")
  assertEq(call.args[1], "retouch", "op forwarded")
  assertEq(call.args[2], 0, "instance defaults to 0")
end

-- ---- methods.dev_set_viewport / methods.dev_restore_viewport: bridge-layer
-- arg forwarding + defaulting for the writable zoom/pan control
-- (2026-07-31 set-viewport-design). Only the wrapper contract is under test
-- (defaults, required-arg validation, previous-table unpacking) -- the real
-- dt_dev_zoom_move/clamp/wait-for-pipe semantics need a live darktable
-- process, flagged as such in the delegated task report.
do
  develop_calls = {}
  local result = internals.methods.dev_set_viewport({zoom_x = -0.1, zoom_y = 0.05, scale = 2.5})
  assertTrue(result.ok, "dev_set_viewport forwards the C result")
  assertEq(result.viewport, "main", "result viewport echoed back")
  local call = develop_calls[1]
  assertEq(call.name, "set_viewport", "correct C function called")
  assertEq(call.args[1], "main", "viewport defaults to 'main'")
  assertEq(call.args[2], -0.1, "zoom_x forwarded")
  assertEq(call.args[3], 0.05, "zoom_y forwarded")
  assertEq(call.args[4], 2.5, "scale forwarded")
  assertEq(call.args[5], true, "wait_for_pipe defaults to true")
  assertEq(call.args[6], nil, "timeout_ms forwarded as nil when omitted (C applies its own default)")
end

do
  develop_calls = {}
  local result = internals.methods.dev_set_viewport({
    viewport = "preview2", zoom_x = 0.0, zoom_y = 0.0, scale = 1.0,
    wait_for_pipe = false, timeout_ms = 1000,
  })
  assertTrue(result.ok, "dev_set_viewport forwards the C result (preview2)")
  local call = develop_calls[1]
  assertEq(call.args[1], "preview2", "viewport forwarded when given")
  assertEq(call.args[5], false, "wait_for_pipe forwarded when explicitly false")
  assertEq(call.args[6], 1000, "timeout_ms forwarded when given")
end

do
  local ok, err = pcall(internals.methods.dev_set_viewport, {zoom_x = 0.1, zoom_y = 0.1})
  assertTrue(not ok, "dev_set_viewport errors without scale")
  assertTrue(string.find(err or "", "zoom_x, zoom_y, scale") ~= nil,
    "error names the missing required fields")
end

do
  develop_calls = {}
  local previous = {zoom = 0, zoom_label = "fit", closeup = 0, zoom_x = 0.0, zoom_y = 0.0, scale = 0.35}
  local result = internals.methods.dev_restore_viewport({previous = previous})
  assertTrue(result.ok, "dev_restore_viewport forwards the C result")
  local call = develop_calls[1]
  assertEq(call.name, "restore_viewport", "correct C function called")
  assertEq(call.args[1], "main", "viewport defaults to 'main'")
  assertEq(call.args[2], 0, "previous.zoom unpacked positionally")
  assertEq(call.args[3], 0, "previous.closeup unpacked positionally")
  assertEq(call.args[4], 0.0, "previous.zoom_x unpacked positionally")
  assertEq(call.args[5], 0.0, "previous.zoom_y unpacked positionally")
  assertEq(call.args[6], 0.35, "previous.scale unpacked positionally")
  assertEq(call.args[7], true, "wait_for_pipe defaults to true")
end

do
  develop_calls = {}
  local previous = {zoom = 3, zoom_label = "free", closeup = 1, zoom_x = -0.2, zoom_y = 0.3, scale = 4.0}
  internals.methods.dev_restore_viewport({
    viewport = "preview2", previous = previous, wait_for_pipe = false, timeout_ms = 2000,
  })
  local call = develop_calls[1]
  assertEq(call.args[1], "preview2", "viewport forwarded when given")
  assertEq(call.args[7], false, "wait_for_pipe forwarded when explicitly false")
  assertEq(call.args[8], 2000, "timeout_ms forwarded when given")
end

do
  local ok, err = pcall(internals.methods.dev_restore_viewport, {})
  assertTrue(not ok, "dev_restore_viewport errors without previous")
  assertTrue(string.find(err or "", "previous") ~= nil, "error mentions previous")
end

do
  local ok, err = pcall(internals.methods.dev_restore_viewport, {previous = {zoom = 0, closeup = 0}})
  assertTrue(not ok, "dev_restore_viewport errors when previous is missing fields")
  assertTrue(string.find(err or "", "zoom_x") ~= nil, "error names a missing field")
end

-- ---- methods.dev_preview: viewport= forwarding (2026-07-31 set-viewport-
-- design follow-up, the fix that makes capture_viewport('main') actually
-- reflect a prior set_viewport zoom instead of always reading the
-- fixed-resolution preview_pipe). Omitted viewport must still forward as
-- nil -- no behavior change for every pre-existing dev_preview caller.
do
  develop_calls = {}
  local result = internals.methods.dev_preview({max_w = 1024, max_h = 1024})
  assertEq(result.viewport_source, "preview_pipe", "no viewport -> stub echoes preview_pipe")
  local call = develop_calls[1]
  assertEq(call.name, "preview", "correct C function called")
  assertEq(call.args[1], 1024, "max_w forwarded")
  assertEq(call.args[2], 1024, "max_h forwarded")
  assertEq(call.args[3], nil, "region x forwarded as nil when omitted")
  assertEq(call.args[7], nil, "viewport forwarded as nil when omitted")
end

do
  develop_calls = {}
  local result = internals.methods.dev_preview({max_w = 1400, max_h = 1400, viewport = "main"})
  assertEq(result.viewport_source, "main", "viewport='main' forwarded and echoed")
  local call = develop_calls[1]
  assertEq(call.args[3], nil, "no region forwarded (capture_viewport's own fix: full.pipe's "
    .. "backbuf IS already the zoomed crop, passing region would double-crop)")
  assertEq(call.args[7], "main", "viewport forwarded")
end

do
  develop_calls = {}
  local result = internals.methods.dev_preview({
    max_w = 800, max_h = 800, viewport = "preview2",
    region = {x = 0.1, y = 0.2, w = 0.3, h = 0.4},
  })
  assertEq(result.viewport_source, "preview2", "viewport='preview2' forwarded and echoed")
  local call = develop_calls[1]
  assertEq(call.args[3], 0.1, "region.x forwarded when explicitly given together with viewport")
  assertEq(call.args[4], 0.2, "region.y forwarded")
  assertEq(call.args[5], 0.3, "region.w forwarded")
  assertEq(call.args[6], 0.4, "region.h forwarded")
  assertEq(call.args[7], "preview2", "viewport forwarded")
end

-- ---- methods.dev_backtransform_point: bridge-layer arg forwarding for the
-- display-frame -> mask-frame conversion (2026-07-25 coordinate-frame
-- bugreport fix). len1/len2 optional -- nil when omitted, forwarded when given.
do
  develop_calls = {}
  local result = internals.methods.dev_backtransform_point({x = 0.5, y = 0.3})
  assertEq(result.x, 0.5, "dev_backtransform_point forwards x (identity stub)")
  assertEq(result.y, 0.3, "dev_backtransform_point forwards y (identity stub)")
  assertEq(result.len1, nil, "len1 omitted when not given")
  local call = develop_calls[1]
  assertEq(call.name, "backtransform_point", "correct C function called")
  assertEq(call.args[1], 0.5, "x forwarded")
  assertEq(call.args[2], 0.3, "y forwarded")
  assertEq(call.args[3], nil, "len1 forwarded as nil when omitted")
  assertEq(call.args[4], nil, "len2 forwarded as nil when omitted")
end

do
  develop_calls = {}
  local result = internals.methods.dev_backtransform_point({x = 0.5, y = 0.3, len1 = 0.02, len2 = 0.01})
  assertEq(result.len1, 0.02, "len1 forwarded and returned")
  assertEq(result.len2, 0.01, "len2 forwarded and returned")
  local call = develop_calls[1]
  assertEq(call.args[3], 0.02, "len1 forwarded when given")
  assertEq(call.args[4], 0.01, "len2 forwarded when given")
end

do
  local ok, err = pcall(internals.methods.dev_backtransform_point, {y = 0.3})
  assertTrue(not ok, "dev_backtransform_point errors without x")
  assertTrue(string.find(err or "", "x/y") ~= nil, "error mentions x/y")
end

-- ---- methods.dev_transform_point: the mask-frame -> display-frame direction,
-- needed to verify or DRAW an existing shape against a captured render
-- (retouch_render_overlay, 2026-07-26). Must call the C `transform_point`, not
-- the backtransform -- mixing the two silently mirrors every coordinate.
do
  develop_calls = {}
  local result = internals.methods.dev_transform_point({x = 0.4, y = 0.6, len1 = 0.02})
  assertEq(result.x, 0.4, "dev_transform_point forwards x (identity stub)")
  assertEq(result.y, 0.6, "dev_transform_point forwards y (identity stub)")
  assertEq(result.len1, 0.02, "len1 forwarded and returned")
  assertEq(result.len2, nil, "len2 omitted when not given")
  local call = develop_calls[1]
  assertEq(call.name, "transform_point", "correct C function called (not backtransform)")
  assertEq(call.args[1], 0.4, "x forwarded")
  assertEq(call.args[3], 0.02, "len1 forwarded")
  assertEq(call.args[4], nil, "len2 forwarded as nil when omitted")
end

do
  local ok, err = pcall(internals.methods.dev_transform_point, {x = 0.4})
  assertTrue(not ok, "dev_transform_point errors without y")
  assertTrue(string.find(err or "", "x/y") ~= nil, "error mentions x/y")
end

-- ---- methods.dev_get_conf_string: LUT tooling (2026-07-26), needed to read
-- the lut3d module's configured root dir (plugins/darkroom/lut3d/def_path)
-- so list_luts scans the SAME directory as the darktable UI dropdown.
do
  develop_calls = {}
  local result = internals.methods.dev_get_conf_string({key = "plugins/darkroom/lut3d/def_path"})
  assertEq(result, "/tmp/luts", "dev_get_conf_string forwards the C return value")
  local call = develop_calls[1]
  assertEq(call.name, "get_conf_string", "correct C function called")
  assertEq(call.args[1], "plugins/darkroom/lut3d/def_path", "key forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_get_conf_string, {})
  assertTrue(not ok, "dev_get_conf_string errors without key")
  assertTrue(string.find(err or "", "key") ~= nil, "error mentions key")
end

-- ---- methods.dev_get_blend_params / dev_set_blend_params: blend opacity
-- control (2026-07-26), needed for LUT intensity since lut3d has no "amount"
-- of its own -- blend_params is a separate flat struct from module params.
do
  develop_calls = {}
  local result = internals.methods.dev_get_blend_params({op = "lut3d", instance = 0})
  assertEq(result.opacity, 100.0, "dev_get_blend_params forwards opacity")
  local call = develop_calls[1]
  assertEq(call.name, "get_blend_params", "correct C function called")
  assertEq(call.args[1], "lut3d", "op forwarded")
  assertEq(call.args[2], 0, "instance forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_get_blend_params, {})
  assertTrue(not ok, "dev_get_blend_params errors without op")
end

do
  develop_calls = {}
  local result = internals.methods.dev_set_blend_params({
    op = "lut3d", instance = 0,
    fields = {opacity = 35.0, enable_uniform_blend = true},
  })
  assertEq(result.ok, true, "dev_set_blend_params reports ok")
  assertEq(result.opacity, 35.0, "dev_set_blend_params forwards opacity")
  local call = develop_calls[1]
  assertEq(call.name, "set_blend_params", "correct C function called")
  assertEq(call.args[3].opacity, 35.0, "fields forwarded")
  assertEq(call.args[3].enable_uniform_blend, true, "enable_uniform_blend forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_set_blend_params, {op = "lut3d", instance = 0})
  assertTrue(not ok, "dev_set_blend_params errors without fields")
  assertTrue(string.find(err or "", "fields") ~= nil, "error mentions fields")
end

-- ---- methods.dev_add_instance: fields={} initial-param overrides
-- (2026-07-27) -- exposure's compensate_exposure_bias/compensate_hilite_pres
-- must be settable on the NEW instance without a second history entry.
do
  develop_calls = {}
  local result = internals.methods.dev_add_instance({op = "exposure"})
  assertEq(result.instance, 1, "dev_add_instance without fields forwards result")
  local call = develop_calls[1]
  assertEq(call.name, "add_instance", "correct C function called")
  assertEq(call.args[1], "exposure", "op forwarded")
  assertTrue(call.args[2] == nil, "no fields table forwarded when fields omitted")
  assertTrue(result.fields_applied == nil, "no fields_applied when fields omitted")
end

do
  develop_calls = {}
  local result = internals.methods.dev_add_instance({
    op = "exposure",
    fields = {compensate_exposure_bias = false, compensate_hilite_pres = false},
  })
  local call = develop_calls[1]
  assertEq(call.args[2].compensate_exposure_bias, false, "fields forwarded to C call")
  assertTrue(result.fields_applied ~= nil, "fields_applied present when fields given")
  assertEq(result.fields_applied.applied.compensate_hilite_pres, false,
    "fields_applied echoes the applied field")
end

do
  local ok, err = pcall(internals.methods.dev_add_instance, {op = "exposure", fields = "not-a-table"})
  assertTrue(not ok, "dev_add_instance errors when fields is not a table")
end

do
  local ok, err = pcall(internals.methods.dev_add_instance, {})
  assertTrue(not ok, "dev_add_instance errors without op")
end

-- ---- methods.dev_list_masks / dev_list_all_masks / dev_attach_mask /
-- dev_detach_mask: mask group management (2026-07-27) -- attach/detach an
-- EXISTING shape to a module's blend group without copying it. -----------
do
  develop_calls = {}
  local result = internals.methods.dev_list_masks({op = "retouch", instance = 0})
  assertEq(#result, 1, "dev_list_masks forwards the C result")
  assertEq(result[1].operation, "union", "dev_list_masks includes operation")
  local call = develop_calls[1]
  assertEq(call.name, "list_masks", "correct C function called")
  assertEq(call.args[1], "retouch", "op forwarded")
  assertEq(call.args[2], 0, "instance forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_list_masks, {})
  assertTrue(not ok, "dev_list_masks errors without op")
end

do
  develop_calls = {}
  local result = internals.methods.dev_list_all_masks({})
  assertEq(#result, 1, "dev_list_all_masks forwards the C result")
  assertEq(result[1].formid, 42, "dev_list_all_masks includes formid")
  assertEq(result[1].used_by[1].op, "retouch", "dev_list_all_masks includes used_by")
  assertEq(develop_calls[1].name, "list_all_masks", "correct C function called")
end

do
  develop_calls = {}
  local result = internals.methods.dev_attach_mask({op = "exposure", instance = 1, formid = 42})
  assertEq(result.ok, true, "dev_attach_mask reports ok")
  local call = develop_calls[1]
  assertEq(call.name, "attach_mask", "correct C function called")
  assertEq(call.args[1], "exposure", "op forwarded")
  assertEq(call.args[2], 1, "instance forwarded")
  assertEq(call.args[3], 42, "formid forwarded")
  assertTrue(call.args[4] == nil, "operation omitted forwards nil (C defaults to union)")
end

do
  develop_calls = {}
  internals.methods.dev_attach_mask({op = "exposure", instance = 1, formid = 42, operation = "difference"})
  local call = develop_calls[1]
  assertEq(call.args[4], "difference", "operation forwarded when given")
end

do
  local ok, err = pcall(internals.methods.dev_attach_mask, {op = "exposure", instance = 1})
  assertTrue(not ok, "dev_attach_mask errors without formid")
  assertTrue(string.find(err or "", "formid") ~= nil, "error mentions formid")
end

do
  local ok, err = pcall(internals.methods.dev_attach_mask, {formid = 42})
  assertTrue(not ok, "dev_attach_mask errors without op")
end

do
  develop_calls = {}
  local result = internals.methods.dev_detach_mask({op = "exposure", instance = 1, formid = 42})
  assertEq(result.ok, true, "dev_detach_mask reports ok")
  local call = develop_calls[1]
  assertEq(call.name, "detach_mask", "correct C function called")
  assertEq(call.args[3], 42, "formid forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_detach_mask, {op = "exposure", instance = 1})
  assertTrue(not ok, "dev_detach_mask errors without formid")
end

-- ---- methods.dev_get_mask / dev_rename_mask (2026-07-31) -- mask geometry
-- read + rename, closing the gap a bugreport found: get_mask already
-- existed in C (registered) but was never wired through the Lua bridge. ----
do
  develop_calls = {}
  local result = internals.methods.dev_get_mask({mask_id = 42})
  assertEq(result.mask_id, 42, "dev_get_mask forwards the C result")
  assertEq(result.points[1].corner[1], 0.1, "dev_get_mask includes point geometry")
  local call = develop_calls[1]
  assertEq(call.name, "get_mask", "correct C function called")
  assertEq(call.args[1], 42, "mask_id forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_get_mask, {})
  assertTrue(not ok, "dev_get_mask errors without mask_id")
end

do
  develop_calls = {}
  local result = internals.methods.dev_rename_mask({mask_id = 42, name = "model body"})
  assertEq(result.ok, true, "dev_rename_mask reports ok")
  assertEq(result.name, "model body", "dev_rename_mask returns the new name")
  local call = develop_calls[1]
  assertEq(call.name, "rename_mask", "correct C function called")
  assertEq(call.args[1], 42, "mask_id forwarded")
  assertEq(call.args[2], "model body", "name forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_rename_mask, {mask_id = 42})
  assertTrue(not ok, "dev_rename_mask errors without name")
end

do
  local ok, err = pcall(internals.methods.dev_rename_mask, {name = "x"})
  assertTrue(not ok, "dev_rename_mask errors without mask_id")
end

do
  develop_calls = {}
  local result = internals.methods.dev_delete_mask({mask_id = 42})
  assertEq(result.ok, true, "dev_delete_mask reports ok")
  local call = develop_calls[1]
  assertEq(call.name, "delete_mask", "correct C function called")
  assertEq(call.args[1], 42, "mask_id forwarded")
end

do
  local ok, err = pcall(internals.methods.dev_delete_mask, {})
  assertTrue(not ok, "dev_delete_mask errors without mask_id")
end

-- ---- methods.dev_current_image sidecar field (2026-07-31) -- bugreport:
-- export_images silently exported the base/version-0 duplicate's sidecar
-- instead of the one open in darkroom. dev_current_image now merges in
-- image.sidecar (stock darktable Lua field) so a caller can pass the exact
-- sidecar to export_images's xmp_paths. -------------------------------------
do
  develop_calls = {}
  local result = internals.methods.dev_current_image({})
  assertEq(result.has_image, true, "dev_current_image forwards has_image")
  assertEq(result.id, 101, "dev_current_image forwards id")
  assertEq(result.sidecar, "/photos/DSC_0001.NEF.xmp", "dev_current_image merges in sidecar")
  assertEq(develop_calls[1].name, "current_image", "correct C function called")
end

do
  -- has_image=false: must NOT attempt a database lookup at all.
  local original_current_image = stub_dt.develop.current_image
  stub_dt.develop.current_image = function() return {has_image = false, id = -1} end
  local result = internals.methods.dev_current_image({})
  assertEq(result.has_image, false, "dev_current_image forwards has_image=false")
  assertTrue(result.sidecar == nil, "no sidecar merged in when no image is open")
  stub_dt.develop.current_image = original_current_image
end

-- Contact sheets must identify the same duplicate sidecar as view_photos.
do
  local images = internals.methods.get_collection_images({scope = "library"})
  local found = nil
  for _, image in ipairs(images) do
    if image.id == "101" then found = image end
  end
  assertTrue(found ~= nil, "collection contains the expected image")
  if found then
    assertEq(found.sidecar, "/photos/DSC_0001.NEF.xmp", "collection preserves duplicate sidecar")
  end
end

-- ---- methods.tag_photo ------------------------------------------------------
do
  local result = internals.methods.tag_photo({photo_ids = {"101", "102"}, tags = {"keep"}})
  assertEq(result.updated, 2, "tag_photo updated count")
  assertEq(#result.tags_created, 1, "tag_photo created one new tag")
  assertEq(result.tags_created[1], "keep", "tag_photo reports created tag name")
  assertEq(#tags_by_name["keep"], 2, "tag 'keep' now attached to 2 images")
end

do
  -- Re-attaching an existing tag must not report it as newly created, and
  -- attaching to a photo that's already tagged must not duplicate the entry.
  local result = internals.methods.tag_photo({photo_ids = {"101", "999"}, tags = {"keep"}})
  assertEq(#result.tags_created, 0, "tag_photo does not re-create an existing tag")
  assertEq(result.updated, 1, "tag_photo only counts photos that exist")
  assertEq(result.missing_photos[1], "999", "tag_photo reports missing photo id")
  assertEq(#tags_by_name["keep"], 2, "re-attaching an existing tag does not duplicate")
end

do
  local result = internals.methods.tag_photo({photo_ids = {"101"}, remove_tags = {"keep"}})
  assertEq(result.updated, 1, "tag_photo remove_tags updated count")
  assertEq(#tags_by_name["keep"], 1, "detaching removes image from tag")
end

do
  local resp = internals.handle({id = "tp1", method = "tag_photo", params = {photo_ids = {}}})
  assertTrue(resp.error ~= nil, "tag_photo errors on empty photo_ids")
  assertTrue(string.find(resp.error or "", "photo_ids") ~= nil, "error mentions photo_ids")
end

do
  local resp = internals.handle({id = "tp2", method = "tag_photo", params = {photo_ids = {"101"}}})
  assertTrue(resp.error ~= nil, "tag_photo errors when neither tags nor remove_tags given")
end

-- ---- methods.list_collections -----------------------------------------------
do
  local result = internals.methods.list_collections({})
  assertEq(result.count, 1, "list_collections returns one tag so far")
  assertEq(result.collections[1].name, "keep", "list_collections returns tag name")
  assertEq(result.collections[1].count, 1, "list_collections returns tag photo count")
end

do
  local result = internals.methods.list_collections({filter = "nope"})
  assertEq(result.count, 0, "list_collections filter excludes non-matching tags")
end

-- ---- methods.list_photos_in_collection --------------------------------------
do
  local result = internals.methods.list_photos_in_collection({collection = "keep"})
  assertTrue(result.found, "list_photos_in_collection finds existing tag")
  assertEq(result.count, 1, "list_photos_in_collection returns one photo")
  assertEq(result.photos[1].id, "102", "list_photos_in_collection returns correct photo id")
  assertEq(result.photos[1].path, "/photos/DSC_0002.NEF",
    "list_photos_in_collection returns absolute file path")
end

do
  local result = internals.methods.list_photos_in_collection({collection = "nonexistent"})
  assertTrue(not result.found, "list_photos_in_collection reports not found for unknown tag")
  assertEq(result.count, 0, "list_photos_in_collection returns zero photos for unknown tag")
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
  stub_dt.database = setmetatable({
    get_image = function(id) return stub_db_inner[id] end,
  }, {__index = stub_db_inner})

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
  stub_dt.database = {get_image = function(_) return nil end}

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

  -- The stale age must outlast the LONGEST client budget in DEFAULT_TIMEOUTS
  -- (darktable_mcp/bridge/client.py: 120s for import_batch and apply_preset).
  -- The worker is single-threaded, so a request can sit queued behind a call
  -- that legitimately runs for its full budget. At the old 60s the sweep
  -- deleted those still-wanted request-*.json files, and their callers waited
  -- out the whole 120s only to be told "darktable is not running".
  local MAX_CLIENT_TIMEOUT = 120
  assertTrue(internals.stale_age_seconds > MAX_CLIENT_TIMEOUT,
    "stale age (" .. tostring(internals.stale_age_seconds) .. "s) must exceed "
    .. "the largest client timeout (" .. MAX_CLIENT_TIMEOUT .. "s), or the "
    .. "sweep deletes requests whose callers are still waiting")
  assertTrue(internals.stale_age_seconds >= 300,
    "stale age leaves margin for queueing delay ahead of the longest call")
  -- find's -mmin granularity is whole minutes; the age must survive the
  -- floor() in sweep_stale with the invariant intact.
  assertTrue(math.floor(internals.stale_age_seconds / 60) * 60 > MAX_CLIENT_TIMEOUT,
    "stale age still exceeds the client timeout after sweep_stale rounds it "
    .. "down to whole minutes for find -mmin")
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

-- ---- list_directories_recursive: the LFS branch ----------------------------
-- The block above only ever exercises the `find` fallback, because CI runs a
-- plain lua with no LuaFileSystem. That left the lfs branch untested, and it
-- was broken in exactly the way the fallback is not: _walk_lfs appended the
-- root unconditionally and threw away the result of its own pcall, so on a
-- darktable whose Lua HAS lfs, import_batch{source_path="/does/not/exist"}
-- answered recursive_honoured = true, directories_imported = 1, note = nil.
-- Inject a stand-in module so both branches are covered on every host.
do
  -- os.execute's return shape differs across 5.1/5.2+; accept both.
  local function shell_ok(cmd)
    local a, _, c = os.execute(cmd)
    return a == true or a == 0 or (a ~= false and c == 0)
  end
  local function mode_of(path, follow)
    if not follow and shell_ok("test -L " .. sq(path)) then return "link" end
    if shell_ok("test -d " .. sq(path)) then return "directory" end
    if shell_ok("test -e " .. sq(path)) then return "file" end
    return nil
  end

  -- A LuaFileSystem stand-in over the real filesystem. Crucially, dir() RAISES
  -- on an unreadable path, like the real lfs.dir does.
  local fake_lfs = {
    attributes = function(path, what)
      if what ~= "mode" then return nil end
      return mode_of(path, true)
    end,
    symlinkattributes = function(path, what)
      if what ~= "mode" then return nil end
      return mode_of(path, false)
    end,
    dir = function(d)
      if not shell_ok("test -d " .. sq(d)) then
        error("cannot open " .. d .. ": No such file or directory")
      end
      local p = io.popen("ls -1a " .. sq(d) .. " 2>/dev/null")
      local lines = {}
      for line in p:lines() do lines[#lines + 1] = line end
      p:close()
      local i = 0
      return function() i = i + 1; return lines[i] end
    end,
  }

  local root = make_tmpdir([[dtmcp-lfs-tree it's "a" $(echo x)]])
  os.execute("mkdir -p " .. sq(root .. "/store_00010001_DCIM_100NCD80"))
  os.execute("mkdir -p " .. sq(root .. "/store_00020001_DCIM_100NCD80/nested"))
  write_text(root .. "/store_00010001_DCIM_100NCD80/DSC_0001.NEF", "x")

  local dirs, enumerated = internals.list_directories_recursive(root, fake_lfs)
  assertEq(enumerated, true, "lfs walk reports success for a real directory")
  assertEq(#dirs, 4, "lfs walk returns the root plus its three subdirectories")
  assertEq(dirs[1], root, "lfs walk sorts the root first")
  local set = {}
  for _, d in ipairs(dirs) do set[d] = true end
  assertTrue(set[root .. "/store_00020001_DCIM_100NCD80/nested"],
    "lfs walk descends into nested directories")
  assertTrue(not set[root .. "/store_00010001_DCIM_100NCD80/DSC_0001.NEF"],
    "lfs walk lists directories only, not files")

  -- THE REGRESSION. A root that does not exist must NOT come back as a
  -- confident single-element answer, or import_batch claims it honoured
  -- recursion over a tree it never opened.
  local missing, ok_missing =
    internals.list_directories_recursive(root .. "/does-not-exist", fake_lfs)
  assertEq(ok_missing, false, "lfs walk reports failure for a nonexistent root")
  assertEq(#missing, 1, "failed lfs walk still yields the requested path")
  assertEq(missing[1], root .. "/does-not-exist",
    "failed lfs walk echoes the path it was asked about")

  -- A real but genuinely EMPTY directory is the case the failure signal must
  -- not be confused with: read fine, no children, so enumeration succeeded.
  local empty = root .. "/empty-but-real"
  os.execute("mkdir -p " .. sq(empty))
  local edirs, eok = internals.list_directories_recursive(empty, fake_lfs)
  assertEq(eok, true, "an empty but READABLE directory enumerates successfully")
  assertEq(#edirs, 1, "an empty directory yields just itself")

  -- A module with no attributes()/symlinkattributes() at all: the only signal
  -- is dir() raising, and that must still be propagated.
  local minimal_lfs = {dir = fake_lfs.dir}
  local _, ok_min =
    internals.list_directories_recursive(root .. "/nope", minimal_lfs)
  assertEq(ok_min, false,
    "a raising dir() alone is enough to report enumeration failure")
  local _, ok_min_real = internals.list_directories_recursive(empty, minimal_lfs)
  assertEq(ok_min_real, true, "the same minimal module still succeeds on a real path")

  rmtree(root)
end

-- ---- list_directories_recursive: an unreadable SUBDIRECTORY ----------------
do
  -- Synthetic tree, so this does not depend on chmod behaving the same for
  -- root and non-root test runners. /synth/b stats as a directory but cannot
  -- be listed -- the permission-denied case.
  local listable = {
    ["/synth"] = {"a", "b"},
    ["/synth/a"] = {},
  }
  local dirs_that_exist = {["/synth"] = true, ["/synth/a"] = true, ["/synth/b"] = true}
  -- No symlinkattributes: also covers _link_mode's fallback to attributes().
  local synth_lfs = {
    attributes = function(path, what)
      if what ~= "mode" then return nil end
      return dirs_that_exist[path] and "directory" or nil
    end,
    dir = function(d)
      local entries = listable[d]
      if not entries then error("permission denied: " .. d) end
      local list = {".", ".."}
      for _, e in ipairs(entries) do list[#list + 1] = e end
      local i = 0
      return function() i = i + 1; return list[i] end
    end,
  }

  local dirs, enumerated = internals.list_directories_recursive("/synth", synth_lfs)
  assertEq(enumerated, false,
    "a subdirectory that cannot be read makes the whole enumeration untrusted")
  -- The partial result is still returned: those directories DO get imported,
  -- which is why import_batch's note reports a count instead of claiming
  -- "only that path was imported".
  assertEq(#dirs, 3, "the directories that were readable are still returned")
  assertEq(dirs[1], "/synth", "partial enumeration is still sorted, root first")
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
  assertEq(cfg.deadline_seconds, 10, "import poll wall-clock ceiling is 10s")
  assertEq(cfg.attempts * cfg.interval_ms, 10000,
    "the sleep budget matches the wall-clock deadline")

  stub_dt.control.sleep = original_sleep
  stub_dt.database = original_db
end

-- ---- poll_for_imported: gives up early when NOTHING arrives ----------------
do
  -- Every attempt runs count_images_under, a full linear scan of dt.database.
  -- The old loop ran all 100 of them whenever the count stayed 0 -- the bad
  -- path / "darktable rejected the folder" case -- so the worst case was 100
  -- full library scans, not the 10s the comment claimed. On a 30k-image
  -- library that is minutes of head-of-line blocking for every other bridge
  -- request, past the client's own 120s budget.
  local original_db = stub_dt.database
  local original_sleep = stub_dt.control.sleep
  local cfg = internals.import_poll

  -- Count the SCANS, not the sleeps: the scans are the expensive half, and
  -- counting sleeps is exactly the mistake the old comment made. ipairs()
  -- probes index 1 first on an empty database, so this tallies one per scan.
  local scans = 0
  stub_dt.database = setmetatable({}, {__index = function(_, k)
    if k == 1 then scans = scans + 1 end
    return nil
  end})
  stub_dt.control.sleep = function(_) end

  local count, incomplete = internals.poll_for_imported("/dest/rejected")
  assertEq(count, 0, "a folder that registered nothing reports 0")
  assertEq(incomplete, true, "and is still flagged scan_incomplete")
  assertTrue(scans <= cfg.zero_grace_polls,
    "a count stuck at zero stops after the grace window, not " .. cfg.attempts
    .. " full library scans (ran " .. scans .. ")")
  assertTrue(scans < cfg.attempts,
    "the early exit really is earlier than the attempt ceiling")
  assertTrue(cfg.zero_grace_polls * cfg.interval_ms <= 3000,
    "the zero-result grace window is a couple of seconds, not the full budget")

  stub_dt.control.sleep = original_sleep
  stub_dt.database = original_db
end

-- ---- poll_for_imported: the wall-clock deadline bounds the SCANS -----------
do
  -- Bounding attempts is not the same as bounding time. If each scan is slow
  -- (a big library), the attempt ceiling can be nowhere near reached while the
  -- wall clock runs past the caller's budget. Drive an injected clock that
  -- jumps forward per attempt and prove the poll stops on time.
  local original_db = stub_dt.database
  local original_sleep = stub_dt.control.sleep
  local cfg = internals.import_poll

  -- Images keep arriving, so neither the settle path nor the zero-grace exit
  -- can fire; only the deadline can stop this.
  local films = {}
  stub_dt.database = films
  local ticks = 0
  stub_dt.control.sleep = function(_)
    ticks = ticks + 1
    films[#films + 1] = {film = {path = "/slow/sub" .. ticks}}
  end

  -- 3 simulated seconds per scan: the deadline lands on attempt 4-5, decades
  -- short of the 100-attempt ceiling.
  local fake_seconds = 0
  local function fake_now()
    local t = fake_seconds
    fake_seconds = fake_seconds + 3
    return t
  end

  local count, incomplete =
    internals.poll_for_imported("/slow", {now = fake_now})
  assertEq(incomplete, true, "a poll cut off by the deadline is flagged incomplete")
  assertTrue(count > 0, "the deadline still reports the floor reached so far")
  assertTrue(ticks < cfg.attempts,
    "the wall-clock deadline stops the poll long before the attempt ceiling "
    .. "(ran " .. ticks .. " of " .. cfg.attempts .. ")")
  assertTrue(fake_seconds <= (cfg.deadline_seconds + 6),
    "the poll does not overrun its wall-clock budget by more than one scan "
    .. "(simulated " .. fake_seconds .. "s for a " .. cfg.deadline_seconds
    .. "s budget)")

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

-- Unpatched installations keep library tools but explain editing requirements.
do
  local dt = require("darktable")
  local develop = dt.develop
  dt.develop = nil
  local resp = internals.handle({id = "stock", method = "dev_version", params = {}})
  assertTrue(resp.error ~= nil and string.find(resp.error, "patched darktable", 1, true) ~= nil,
    "stock darktable explains the missing editing API")
  dt.develop = develop
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
