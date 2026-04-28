# Iteration 3: list_styles + apply_preset; adjust_exposure retired

**Date:** 2026-04-28
**Goal:** Ship `apply_preset` over the existing IPC bridge plus a companion `list_styles` discovery tool. Retire `adjust_exposure` from the parked list with documented findings.

## Spike findings

Probe artifacts: `spike/styles_exposure_probe{,2,3,4}.lua`. Logs in `/tmp/iter3-spike{,2,3,4}.log`.

### apply_preset → SHIPPABLE

- `dt.styles` is a userdata supporting `ipairs` and `#`. Iteration yields `dt_style_t` userdata with two readable string fields: `name` and `description`. No `items` / `id` / `iop_list` field.
- `dt.styles[i]` (integer) works. `dt.styles[name]` does NOT — string lookup throws. Caller must iterate to resolve a name.
- `image:apply_style(style)` is canonical and runs cleanly in lighttable view (fully headless-compatible). `dt.styles.apply(style, image)` is also valid.
- Live-tested via probe E1 against a real image; `image.is_altered` flipped after apply.
- User's darktable has 534 bundled camera-input-matrix styles installed (per-camera color profiles); zero custom user styles in `~/.config/darktable/styles/`.

### adjust_exposure → DEFERRED indefinitely

No clean Lua path exists in API 9.6.0:

1. **`image.modules` does not exist.** `dt_lua_image_t` userdata only exposes metadata fields (id, filename, path, exif_*, rating, title, description, creator, is_altered) and a small method set (`apply_style`, `create_style`, `duplicate`, `delete`, `move`, `copy`, `drop_cache`, `reset`, `group_with`, `make_group_leader`). The iteration-1 code that referenced `image.modules.exposure.exposure` was hallucinated.
2. **`image.history` does not exist.** No path to manipulate history items from Lua.
3. **`dt.gui.action(path, instance, element, value)` exists but returns `nan` from lighttable view** — exposure-module sliders only exist when a darkroom view is active for the target image. Driving exposure via this path requires switching to darkroom + loading the image + issuing the action + switching back, which (a) violates the headless rule and (b) is single-image-at-a-time.
4. **`dt.styles.create(image, name, description)` works, but snapshots an image's existing history.** It cannot synthesize a "+N EV exposure" preset from scratch — chicken-and-egg.

**Realistic future paths (all ugly):**
- Constraint-based: ship `apply_preset` and document that users must pre-create named exposure styles via the GUI once per EV value.
- `darktable-cli --style` for batch export with a style applied — non-Lua, non-GUI, but export-only (doesn't persist edits in the library).
- Synthesized `.dtstyle` files: reverse-engineer the XML format and write them directly. Likely fragile across darktable versions; separate research effort.

**Decision:** retire `adjust_exposure` from the README's parked list. If a future user has a real need, the constraint-based path (pre-created styles + `apply_preset`) covers it.

## Scope

**In:** add `list_styles` and `apply_preset` MCP tools through the bridge; retire `adjust_exposure` from the parked list and from documentation.

**Out:** synthesizing `.dtstyle` files, exposure-via-styles workflow scaffolding, GUI-mode tools.

## File changes

### `darktable_mcp/lua/darktable_mcp.lua` — two new methods

```lua
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

  -- Linear scan to resolve preset_name (string lookup unavailable).
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
      table.insert(missed, photo_id)
    end
  end
  return {applied = applied, missed = missed, preset_name = preset_name}
end
```

### `darktable_mcp/server.py` — two new MCP tools

`list_styles()` with no required params; description explains it's a discovery tool. Returns up to ~534 entries on the user's machine (so the formatted text response will need to be paginated or summarized — strawman: return `count` plus first 50 names plus a hint).

`apply_preset(photo_ids: list[str], preset_name: str)` with the same Bridge-error funnel as the other library tools.

### Test additions

- `tests/lua/test_dispatcher.lua` — stub `dt.styles` with 2 fake styles; verify `list_styles` returns both, `apply_preset` finds the right one and calls `image:apply_style`.
- `tests/test_server.py` — handler tests for both new tools (mocking the Bridge).
- `tests/test_ipc_bridge_acceptance.py` — pins for `list_styles` and `apply_preset` registered.
- `tests/test_honesty_pass_acceptance.py` — `EXPECTED_TOOLS` grows from 8 to 10.

### `README.md`

- Add `list_styles` and `apply_preset` to the Library operations bullet block.
- Update the "Why some tools are parked" paragraph: drop the sentence claiming `adjust_exposure` is still parked. Replace with a line pointing to the deferral rationale doc.

## Real-darktable acceptance test

Once the plugin is reinstalled and darktable restarted:

1. Call `list_styles()`. Expect `count >= 500` (the 534 bundled camera input matrices plus any user-installed). Print first 5 names.
2. Pick one style name (e.g. the first one returned).
3. Pick one image id from the user's library (a NEF from `~/Pictures/import-2026-04-26/`).
4. Call `apply_preset(photo_ids=[<id>], preset_name=<name>)`. Expect `{applied: 1, missed: [], preset_name: <name>}`.
5. Verify the change persisted: query `library.db` for the image's `flags` (the `0x100` "altered" bit) and confirm it was set OR query `history` table for new entries for that image_id.
6. Cleanup: revert the test image via `image:reset()` in a follow-up call OR document the change and let the user revert via GUI ("undo" / Ctrl-Z in lighttable).

## Acceptance criteria

- Suite stays green (152 → ~158 with the new handler/dispatcher tests).
- Acceptance pins for `list_styles` and `apply_preset` flip green.
- README reflects 10 tools and the `adjust_exposure` retirement.
- Real-darktable test demonstrates the bridge call returns the expected count and applied count.

## Iteration boundary

After this, iteration 3 is the natural end state for the design. Future work — IF needed — would be in iteration 4: (a) constraint-based exposure tooling using pre-created styles, (b) batch-export with `--style`, (c) some new tool the user actually needs. None of those are scoped today.
