-- T0.3 de-risk spike: pure-Lua live darkroom control.
--
-- Does NOT modify darktable_mcp.lua (the clean T0.2 bridge). Instead it
-- `require`s the core plugin module (which returns {handle, scan_dir,
-- methods, json} -- see darktable_mcp/lua/darktable_mcp.lua bottom) and
-- extends the SAME `methods` table in place. The worker loop already
-- started by darktable_mcp.lua's own `require` picks up these new methods
-- automatically because it's the identical table by reference.
--
-- Install (done by docker/run-dt-bridge-spike.sh, not the clean
-- run-dt-bridge.sh): copy this file next to darktable_mcp.lua in the
-- isolated configdir's lua/ subdir, and make luarc:
--   require "darktable_mcp"
--   require "spike_methods"
--
-- Methods added, see PLAN.md T0.3 sub-goals A/B/C:
--   open_darkroom(image_id)         -- sub-goal A: enter darkroom on a
--                                       specific image, headless.
--   nudge(action_path,element,      -- sub-goal A: darktable.gui.action()
--         effect,size)                 passthrough; omit effect+size to
--                                       read (DT_READ_ACTION_ONLY).
--   preview(max_w,max_h)            -- sub-goal B: export current
--                                       action-image via new_format("jpeg").
--   leave_darkroom()                -- sub-goal C: switch to lighttable,
--                                       which darkroom's leave() commits
--                                       history to DB (dt_dev_write_history)
--                                       -- see views/darkroom.c:4082.
--   debug_state()                   -- diagnostics: current view + the
--                                       images darktable.gui.action_images
--                                       resolves to right now.

local dt = require("darktable")
local core = require("darktable_mcp")
local methods = core.methods

local function cache_dir()
  local base = os.getenv("XDG_CACHE_HOME")
  if not base or base == "" then
    base = os.getenv("HOME") .. "/.cache"
  end
  return base .. "/darktable-mcp"
end

-- ---- sub-goal A: enter darkroom on a specific image, headless -------------

methods.open_darkroom = function(p)
  p = p or {}
  local image_id = tonumber(p.image_id)
  if not image_id then error("open_darkroom: image_id required") end
  local image = dt.database[image_id]
  if not image then error("open_darkroom: image not found: " .. tostring(image_id)) end

  -- Headless has no mouse, so dt_act_on_get_main_image() (which try_enter()
  -- in views/darkroom.c uses to pick the image) can't resolve via hover.
  -- It falls back to view_manager->active_images, then to the DB selection
  -- (see common/act_on.c:_get_main_image_hover). Setting the Lua selection
  -- is the pure-Lua way to steer that fallback deterministically.
  dt.gui.selection({ image })

  dt.gui.current_view(dt.gui.views.darkroom)

  -- dt.gui.current_view(view) calls dt_ctl_switch_mode_to_by_view(), which
  -- schedules the actual switch via g_main_context_invoke() (see
  -- control/control.c:626-630) instead of performing it inline. Reading
  -- dt.gui.current_view() again in the SAME call still shows the OLD view
  -- (confirmed empirically -- see PLAN.md T0.3 findings). Poll a few worker
  -- ticks (dt.control.sleep yields to the main loop, it does not block it)
  -- until the switch has actually landed, or give up and report what we see.
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
  }
end

methods.leave_darkroom = function(p)
  dt.gui.current_view(dt.gui.views.lighttable)
  local current = dt.gui.current_view()
  local waited_ms = 0
  while current.name ~= "lighttable" and waited_ms < 3000 do
    if dt.control and dt.control.sleep then
      dt.control.sleep(100)
      waited_ms = waited_ms + 100
    else
      break
    end
    current = dt.gui.current_view()
  end
  return { view = current.name, waited_ms_for_view_switch = waited_ms }
end

methods.debug_state = function(p)
  local current = dt.gui.current_view()
  local imgs = {}
  for i, img in ipairs(dt.gui.action_images) do
    imgs[i] = { id = tostring(img.id), filename = img.filename }
  end
  local sel = {}
  for i, img in ipairs(dt.gui.selection()) do
    sel[i] = { id = tostring(img.id), filename = img.filename }
  end
  return { view = current.name, action_images = imgs, selection = sel }
end

-- ---- sub-goal A: darktable.gui.action() passthrough ------------------------

methods.nudge = function(p)
  p = p or {}
  local action_path = p.action_path
  if not action_path or action_path == "" then
    error("nudge: action_path required")
  end
  local element = p.element
  local effect = p.effect
  local size = p.size

  local ok, ret = pcall(function()
    if element ~= nil and effect ~= nil and size ~= nil then
      return dt.gui.action(action_path, element, effect, size)
    elseif element ~= nil and effect ~= nil then
      return dt.gui.action(action_path, element, effect)
    elseif element ~= nil then
      return dt.gui.action(action_path, element)
    else
      return dt.gui.action(action_path)
    end
  end)

  if not ok then
    error("nudge: dt.gui.action raised: " .. tostring(ret))
  end

  local is_nan = ret ~= ret -- NaN != NaN, dt_action_process returns NAN for invalid action
  return { value = ret, is_nan = is_nan, action_path = action_path, element = element, effect = effect, size = size }
end

-- ---- sub-goal B: export current action-image as a JPEG preview ------------

methods.preview = function(p)
  p = p or {}
  local max_w = tonumber(p.max_w) or 0
  local max_h = tonumber(p.max_h) or 0

  local images = dt.gui.action_images
  if not images or #images == 0 then
    error("preview: no action image resolved (dt.gui.action_images empty) -- select or open_darkroom first")
  end
  local image = images[1]

  local fmt = dt.new_format("jpeg")
  fmt.max_width = max_w
  fmt.max_height = max_h

  local dir = cache_dir() .. "/spike-previews"
  os.execute('mkdir -p "' .. dir .. '"')
  local tag = p.tag or "x"
  local path = string.format("%s/preview-%s-%s-%d.jpg", dir, tostring(image.id), tostring(tag), os.time())

  local ok = fmt:write_image(image, path)
  if not ok then
    error("preview: write_image returned falsy (export failed) for " .. path)
  end

  return { path = path, image_id = tostring(image.id), max_w = max_w, max_h = max_h }
end

-- ---- T1.1: exercise the new C API darktable.develop.* --------------------
-- These are temporary probes for the T1.1 acceptance test only; they call the
-- brand-new C bindings registered by src/lua/develop.c.

methods.dev_version = function(p)
  return dt.develop.version()
end

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

-- T1.2: darktable.develop.get_params(op, instance) -- typed field map.
-- Returns the C result verbatim ({op,instance,id,fields=...} or {error=...}).
methods.dev_get_params = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_get_params: op required") end
  local instance = tonumber(p.instance) or 0
  return dt.develop.get_params(op, instance)
end

-- T1.3: darktable.develop.set_params(op, instance, fields) -- write + commit.
-- Returns the C result verbatim ({ok,applied,clamped,unknown_fields} or
-- {error=...}). `fields` is passed straight through as a Lua table.
methods.dev_set_params = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_set_params: op required") end
  local instance = tonumber(p.instance) or 0
  local fields = p.fields
  if type(fields) ~= "table" then error("dev_set_params: fields table required") end
  return dt.develop.set_params(op, instance, fields)
end

-- T1.3: read-only history size probe for coalescing observation.
methods.dev_history_count = function(p)
  return dt.develop.history_count()
end

-- T1.5: darktable.develop.preview(max_w, max_h) -- PNG of the CURRENT LIVE
-- darkroom edit grabbed from preview_pipe->backbuf, NO DB write. Returns the C
-- result verbatim: {status="ok",path,width,height} |
-- {status="processing",stale_preview,...} | {error=...}. The `path` is a
-- container path under $XDG_CACHE_HOME (/run/cache-mcp); the driver remaps
-- /run -> run_dir on the host.
methods.dev_preview = function(p)
  p = p or {}
  local max_w = tonumber(p.max_w) or 0
  local max_h = tonumber(p.max_h) or 0
  -- FIX 2: optional normalized region {x,y,w,h} in 0..1 of the visible frame.
  if p.x ~= nil and p.y ~= nil and p.w ~= nil and p.h ~= nil then
    return dt.develop.preview(max_w, max_h,
      tonumber(p.x), tonumber(p.y), tonumber(p.w), tonumber(p.h))
  end
  return dt.develop.preview(max_w, max_h)
end

-- FIX 3 / T2.4: darktable.develop.get_viewport() -- read-only main + preview2
-- canvas zoom/pan state. Returns the C result verbatim ({main=..,preview2=..} |
-- {error=...}). Each viewport now includes a `region` {x,y,w,h} (top-left,
-- normalized 0..1, clamped to [0,1]) = the visible crop of the full image;
-- pass it directly as dev_preview's {x,y,w,h}. The raw zoom_x/zoom_y are
-- center-relative (can be negative) and are NOT a drop-in region.
methods.dev_get_viewport = function(p)
  return dt.develop.get_viewport()
end

-- T1.7: darktable.develop.enable_module(op, instance, bool) -- toggle + commit.
methods.dev_enable_module = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_enable_module: op required") end
  local instance = tonumber(p.instance) or 0
  local enabled = p.enabled
  if enabled == nil then enabled = true end
  return dt.develop.enable_module(op, instance, enabled and true or false)
end

-- T1.7: darktable.develop.dump_introspection(op, instance) -- raw linear list +
-- root-struct top-level child names. Read-only investigation probe.
methods.dev_dump_introspection = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_dump_introspection: op required") end
  local instance = tonumber(p.instance) or 0
  return dt.develop.dump_introspection(op, instance)
end

-- T1.4: darktable.develop.add_instance(op) -- duplicate a module as a new
-- multi-instance (mirrors the GUI new-instance path). Returns the C result
-- verbatim: {ok,op,instance=<new multi_priority>,base_instance,multi_name} |
-- {error=...}.
methods.dev_add_instance = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_add_instance: op required") end
  return dt.develop.add_instance(op)
end

-- T2.2: darktable.develop.add_path_mask(op, instance, points, opacity, feather)
-- Build a drawn PATH mask from a normalized polygon and attach it to a module's
-- blend so the module's effect is restricted to that region. `points` is a Lua
-- array of {x=..,y=..} normalized 0..1. Returns the C result verbatim
-- ({ok,formid,mask_id,points,opacity,feather,mask_mode} | {error=...}).
methods.dev_add_path_mask = function(p)
  p = p or {}
  local op = p.op
  if not op or op == "" then error("dev_add_path_mask: op required") end
  local instance = tonumber(p.instance) or 0
  local points = p.points
  if type(points) ~= "table" then error("dev_add_path_mask: points array required") end
  local opacity = tonumber(p.opacity)
  if opacity == nil then opacity = 1.0 end
  if p.feather ~= nil then
    return dt.develop.add_path_mask(op, instance, points, opacity, tonumber(p.feather))
  end
  return dt.develop.add_path_mask(op, instance, points, opacity)
end

-- T3.1 (raster-producer spike): darktable.develop.set_raster_source(consumer_op,
-- consumer_instance, source_op, source_instance[, opacity]) -- wire a downstream
-- module's blend to consume an upstream module's RASTER mask. Returns the C
-- result verbatim ({ok,consumer,source,raster_mask_source,mask_mode,...} |
-- {error=...}).
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

dt.print_log("darktable-mcp spike_methods: loaded, methods=" ..
  "open_darkroom,leave_darkroom,debug_state,nudge,preview,dev_version," ..
  "dev_active_modules,dev_get_params,dev_set_params,dev_history_count,dev_preview," ..
  "dev_enable_module,dev_dump_introspection,dev_add_instance,dev_add_path_mask")
