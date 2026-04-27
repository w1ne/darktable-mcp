# Task 0 Spike Findings

**Date:** 2026-04-27
**darktable version:** darktable 5.4.1 (Lua API 9.6.0)
**OS / display server:** Linux 6.18.7 / Wayland (COSMIC)

## Verdict

**PASS** — with one correction to the spec's primitive name.

## What worked

- A Lua plugin loaded from `~/.config/darktable/lua/lua_worker_probe.lua`
  (registered via `require "lua_worker_probe"` in `~/.config/darktable/luarc`)
  successfully spawned a non-blocking worker that ticked once per second
  while darktable's GUI process kept running.
- `dt.control.dispatch(worker_loop)` is the correct primitive — **not**
  `dt.control.async`. The original spec name `dt.control.async` does not
  exist in darktable 5.4.1's Lua API and produced
  `LUA ERROR : ... field "async" not found for type dt_lua_singleton_control`
  on the first probe attempt.
- `dt.control.sleep(1000)` yields correctly with the expected 1-second
  cadence inside a `dispatch`-spawned coroutine.
- `/tmp/darktable-mcp-spike.log` grew from 0 lines → 34 lines over ~33s
  observation window (8 introspection lines + 1 "introspection complete"
  + 1 "attempt 1: dispatch" + 31 sequential `tick N at <timestamp>` lines).
  Tick timestamps are monotonically spaced 1 second apart from
  `15:38:01` to `15:38:34`. No drift, no skips.
- Darktable stdout (lua channel) showed only the expected
  `LUA darktable-mcp spike: introspection complete; starting worker`
  message — no errors, no warnings related to the plugin.

## Confirmed `dt.control` surface (darktable 5.4.1)

```
dt.control keys: dispatch, ending, execute, read, sleep
```

Other relevant globals discovered:

```
dt keys: collection, configuration, control, database, destroy_event,
         destroy_storage, films, gettext, gui, guides, new_format,
         new_storage, new_widget, password, preferences, print,
         print_error, print_hinter, print_log, print_toast, query_event,
         register_event, register_lib, register_storage, styles, tags,
         util
dt.gui keys: action, action_images, create_job, current_view, hovered,
             libs, mimic, panel_get_size, panel_hide, panel_hide_all,
             panel_set_size, panel_show, panel_show_all, panel_visible,
             selection, views
```

(`dt.gui.create_job` is a second viable async primitive worth knowing
about — it shows a progress indicator in the GUI's job panel. Not needed
for the bridge; `dt.control.dispatch` is silent and lighter.)

## What did not work

- `dt.control.async` — does not exist. The spec must be updated to use
  `dt.control.dispatch`.
- `loadstring` — the embedded Lua is 5.3+ where `loadstring` was removed
  in favor of `load`. Minor; only affected the introspection probe code,
  not the worker primitive itself.

## Implication for the bridge design

**PASS → design proceeds as spec'd, with one rename.**

- Use `dt.control.dispatch(worker_fn)` (not `dt.control.async`) to spawn
  the IPC bridge worker.
- Use `dt.control.sleep(ms)` inside the worker loop for cooperative
  yielding — confirmed it does **not** block the GUI thread.
- The plugin install path
  (`~/.config/darktable/lua/<name>.lua` + `require "<name>"` in
  `~/.config/darktable/luarc`) works as expected.
- darktable's single-instance lock means the user must close any running
  GUI session before the bridge plugin reloads — flag this in install
  docs.

No other architectural assumptions changed. Iteration 2 implementation
can proceed.

## Raw observations

Representative lines from `/tmp/darktable-mcp-spike.log`:

```
worker probe started at Mon 27 Apr 2026 03:38:01 PM CEST
dt.control keys: dispatch, ending, execute, read, sleep
probe dt.control.sleep => function
probe dt.control.dispatch => function
probe dt.gui.create_job => function
introspection complete at Mon 27 Apr 2026 03:38:01 PM CEST
attempt 1: dt.control.dispatch(worker_loop)
tick 1 at Mon 27 Apr 2026 03:38:01 PM CEST
tick 2 at Mon 27 Apr 2026 03:38:02 PM CEST
tick 3 at Mon 27 Apr 2026 03:38:03 PM CEST
...
tick 30 at Mon 27 Apr 2026 03:38:30 PM CEST
tick 31 at Mon 27 Apr 2026 03:38:31 PM CEST
tick 32 at Mon 27 Apr 2026 03:38:32 PM CEST
tick 33 at Mon 27 Apr 2026 03:38:33 PM CEST
tick 34 at Mon 27 Apr 2026 03:38:34 PM CEST
```

Relevant lines from `/tmp/darktable-spike-stdout.log` (lua channel only,
filtered with `grep -iE "lua|mcp|spike|error|warning"`):

```
  Lua                    -> ENABLED  - API version 9.6.0
 /tmp/.mount_DarktaOeFEAg/usr/bin/darktable -d lua
     1.7693 LUA darktable-mcp spike: introspection complete; starting worker
```

(The `atk-bridge: get_device_events_reply: unknown signature` warning
also appeared but is a generic GTK/AT-SPI noise on Wayland, unrelated to
Lua.)

## Note on the first-attempt failure

The very first probe (before this final run) used the spec's literal
`dt.control.async` name and produced:

```
LUA ERROR : /home/andrii/.config/darktable/lua/lua_worker_probe.lua:40:
  field "async" not found for type dt_lua_singleton_control
```

This is exactly why the spike was needed. The corrected primitive
(`dt.control.dispatch`) was found by introspecting `pairs(dt.control)`
on the second run.
