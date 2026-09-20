# Integration attribution

The agentic editing tools, Lua API adapters, optional sidecars, and associated
tests were recovered from Roman Fordinal's MIT-licensed `rfordinal/darktable-mcp`
`agentic-mcp` branch at `ff27e45e9b`. The merge preserves its commit history.
The original upstream notice follows for provenance. Integration changes retain
MCP 2.x and upstream export checks, add the missing LUT helper, preserve duplicate
sidecars, and serialize asynchronous GUI transactions.

# NOTICE

This directory is a fork of [w1ne/darktable-mcp](https://github.com/w1ne/darktable-mcp)
(cloned locally at `../ref-w1ne-mcp/`), MIT licensed -- see `LICENSE`
(unchanged, copyright w1ne).

Kept from upstream, unmodified: the file-based JSON-RPC bridge design
(`darktable_mcp/bridge/client.py`), the Lua bridge plugin
(`darktable_mcp/lua/darktable_mcp.lua`), the MCP server and tool wrappers
(`darktable_mcp/server.py`, `darktable_mcp/tools/`, `darktable_mcp/darktable/`,
`darktable_mcp/cli/`), and the existing test suite (`tests/`).

Added here for T0.2 (see `../PLAN.md` §5) -- getting the bridge running
end-to-end against a real, live darktable session built from `../src-dt/`:

- `e2e/test_view_photos_e2e.py` -- end-to-end script: starts a long-lived
  darktable + Xvfb session with the bridge wired in, waits for
  `darktable-mcp bridge: ready`, imports a fixture RAW, round-trips
  `view_photos` through the real `Bridge` client, tears down cleanly.
- `../docker/run-dt-bridge.sh` -- long-lived counterpart to
  `../docker/run-dt-xvfb.sh` (which is a bounded smoke test); starts/stops
  the container the e2e script drives.

No behavioral changes were made to the upstream Lua plugin or bridge client
for T0.2. Both `dt.control.dispatch` and `dt.control.sleep`, which the
plugin's worker loop depends on, are confirmed present in this build's Lua
API (9.7.0) -- see `src-dt/src/lua/call.c`.

Added here for T1.6 (`../PLAN.md` §5 T1.6, `../ACCEPTANCE.md` J1/T1.6) --
scalar darkroom editing exposed as typed MCP tools:

- `darktable_mcp/lua/darktable_mcp.lua` -- promoted `open_darkroom`,
  `dev_version`, `dev_active_modules`, `dev_get_params`, `dev_set_params`,
  `dev_preview`, `dev_history_count` from `spike/spike_methods.lua`
  (T0.3/T1.1-T1.5 probes) into the clean `methods` table, wrapping the new
  `darktable.develop.*` C API (branch `agentic-mcp`, `src-dt/src/lua/develop.c`).
- `darktable_mcp/server.py` -- five new MCP tools riding on those methods:
  `open_image_in_darkroom`, `list_modules`, `get_params`, `set_params`,
  `get_preview`. Tool descriptions are written to drive the
  discuss -> edit -> preview -> iterate loop (grounding edits in
  `get_params` bounds, surfacing the `set_params` clamp report, prompting a
  `get_preview` after every edit).
- `pyproject.toml` -- `requires-python` bumped to `>=3.10` to match what the
  `mcp` dependency actually needs (the T0.2 note that `__init__.py` importing
  `.server` needs `mcp` installed is resolved by this + a `uv`-managed venv,
  see `README.md`).
- `README.md` -- new "Darkroom editing" tool section and a "Connect from
  Claude Desktop / Claude Code" section (server command + how the bridge
  needs to be running, both containerized and real-workstation cases).

`spike/spike_methods.lua` and the `spike/run_t1_*.py` drivers are left in
place as the historical T1.1-T1.5 probes; they are not loaded by
`docker/run-dt-bridge.sh` (only `docker/run-dt-bridge-spike.sh` loads them).
