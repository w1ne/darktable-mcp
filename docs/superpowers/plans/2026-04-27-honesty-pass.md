# Honesty Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the broken library tools and their dead supporting code so the MCP tool list and README only advertise things that work.

**Architecture:** Pure deletion plus one rename. No new behavior. A single new acceptance test file pins the desired final state up front; the rest of the work is structured to flip those acceptance tests green one at a time while keeping the suite green at every commit. Spec: `docs/superpowers/specs/2026-04-27-honesty-pass-design.md`.

**Tech Stack:** Python 3.12, pytest, MCP SDK. No new dependencies.

---

## File map

**Delete:**
- `darktable_mcp/darktable/lua_executor.py`
- `darktable_mcp/darktable/library_detector.py`
- `darktable_mcp/darktable/scripts/` (empty directory if it exists)
- `tests/test_lua_executor_headless.py`
- `tests/test_library_detector.py`

**Rename:**
- `darktable_mcp/tools/photo_tools.py` → `darktable_mcp/tools/camera_tools.py` (class `PhotoTools` → `CameraTools`)
- `tests/test_photo_tools.py` → `tests/test_camera_tools.py`

**Modify:**
- `darktable_mcp/server.py` — strip 5 tool registrations, 4 handler methods, 5 `_handler_map` entries; update `PhotoTools` → `CameraTools` import + attribute name; drop the `try/except` around `CameraTools()` if it becomes pointless.
- `darktable_mcp/darktable/__init__.py` — drop `LuaExecutor` import and `__all__` entry.
- `darktable_mcp/tools/camera_tools.py` (after rename) — drop `LuaExecutor` import + `self.lua_executor`; remove `__init__`; remove the four broken methods.
- `tests/test_server.py` — drop registration-list assertions for removed tools, drop `TestToolHandlers` cases for removed handlers, drop `LuaExecutor`/`LibraryDetector`/`PhotoTools` references in `test_all_imports_work`.
- `tests/test_integration.py` — delete every test whose subject is one of the 5 removed tools or that mocks `LuaExecutor`. Audit and prune.
- `tests/test_darktable.py` — drop `TestLuaExecutor` class. Keep `TestCLIWrapper`.
- `tests/test_camera_tools.py` (after rename) — keep only `import_from_camera` / `_detect_cameras` / `_download_from_camera` cases; rename `PhotoTools` → `CameraTools`.
- `README.md` — strip 4 broken tools + `apply_preset` everywhere they appear; replace 3 quick-test examples; append iteration-2 pointer.

**Create:**
- `tests/test_honesty_pass_acceptance.py` — pins the final state (registered tools set, missing modules, renamed class). Drives the deletion order via TDD.

---

## Task 1: Add acceptance pinning tests

**Files:**
- Create: `tests/test_honesty_pass_acceptance.py`

These tests describe the end state. They fail at the start of this iteration. Each subsequent task flips one of them green.

- [ ] **Step 1: Write the acceptance test file**

Write `tests/test_honesty_pass_acceptance.py`:

```python
"""Acceptance tests for the honesty-pass iteration.

Each test pins one piece of the desired final state. They are red at the
start of the iteration and turn green incrementally as deletion tasks
land. See docs/superpowers/specs/2026-04-27-honesty-pass-design.md.
"""

import importlib

import pytest

from darktable_mcp.server import DarktableMCPServer

EXPECTED_TOOLS = {
    "import_from_camera",
    "export_images",
    "extract_previews",
    "apply_ratings_batch",
    "open_in_darktable",
}


def test_server_registers_exactly_the_surviving_tools():
    """No broken or stubbed library tools advertised."""
    server = DarktableMCPServer()
    assert set(server.list_tools()) == EXPECTED_TOOLS


def test_lua_executor_module_is_removed():
    with pytest.raises(ImportError):
        importlib.import_module("darktable_mcp.darktable.lua_executor")


def test_library_detector_module_is_removed():
    with pytest.raises(ImportError):
        importlib.import_module("darktable_mcp.darktable.library_detector")


def test_photo_tools_module_is_removed():
    """Renamed to camera_tools.py."""
    with pytest.raises(ImportError):
        importlib.import_module("darktable_mcp.tools.photo_tools")


def test_camera_tools_module_exposes_camera_tools_class():
    mod = importlib.import_module("darktable_mcp.tools.camera_tools")
    assert hasattr(mod, "CameraTools")
    assert not hasattr(mod, "PhotoTools")
```

- [ ] **Step 2: Run tests to verify they all fail in the expected way**

Run: `venv/bin/pytest tests/test_honesty_pass_acceptance.py -v`

Expected: all 5 tests FAIL.
- `test_server_registers_exactly_the_surviving_tools` fails because today's set includes `view_photos`, `rate_photos`, etc.
- `test_lua_executor_module_is_removed` and `test_library_detector_module_is_removed` fail because the modules still exist.
- `test_photo_tools_module_is_removed` fails because `darktable_mcp.tools.photo_tools` still exists.
- `test_camera_tools_module_exposes_camera_tools_class` fails because `darktable_mcp.tools.camera_tools` doesn't exist yet.

- [ ] **Step 3: Verify the rest of the suite is still green**

Run: `venv/bin/pytest --ignore=tests/test_honesty_pass_acceptance.py -q`

Expected: 177 passed (current count). The new acceptance file is the only thing red right now.

- [ ] **Step 4: Commit**

```bash
git add tests/test_honesty_pass_acceptance.py
git commit -m "test: pin honesty-pass acceptance state"
```

---

## Task 2: Unregister the 5 broken tools from the MCP server

**Files:**
- Modify: `darktable_mcp/server.py` — remove 5 `Tool(...)` registrations, 4 handler methods, 5 entries in `_handler_map`
- Modify: `tests/test_server.py` — drop assertions and integration tests that target removed tools
- Modify: `tests/test_integration.py` — drop end-to-end tests that target removed tools
- Test acceptance: `tests/test_honesty_pass_acceptance.py::test_server_registers_exactly_the_surviving_tools` flips green

We unregister and delete server-side handlers in one task because the tests in `test_server.py::TestToolHandlers` and the corresponding `test_integration.py` cases call the handler methods directly. They must die together with the methods or the suite goes red.

- [ ] **Step 1: Read the current handler map and tool definitions to confirm names**

Run: `grep -n '"view_photos"\|"rate_photos"\|"import_batch"\|"adjust_exposure"\|"apply_preset"\|_handle_view_photos\|_handle_rate_photos\|_handle_import_batch\|_handle_adjust_exposure\|_not_implemented' darktable_mcp/server.py`

Expected: ~20 lines listing the registrations, the handler methods, the `_handler_map` entries, and the `_not_implemented` helper.

- [ ] **Step 2: Edit `darktable_mcp/server.py`**

Remove these blocks. Use `Edit` (read first if needed):

1. The `Tool(name="view_photos", ...)` block in `_tool_definitions()` (lines around 75–101).
2. The `Tool(name="rate_photos", ...)` block (around 102–119).
3. The `Tool(name="import_batch", ...)` block — find by `name="import_batch"`.
4. The `Tool(name="adjust_exposure", ...)` block.
5. The `Tool(name="apply_preset", ...)` block (the stub).
6. The `_handler_map` entries (around lines 388–393):
   ```python
   "view_photos": self._handle_view_photos,
   "rate_photos": self._handle_rate_photos,
   "import_batch": self._handle_import_batch,
   "adjust_exposure": self._handle_adjust_exposure,
   "apply_preset": self._not_implemented("apply_preset"),
   ```
   Keep the rest.
7. The four async handler methods (around lines 416–470): `_handle_view_photos`, `_handle_rate_photos`, `_handle_import_batch`, `_handle_adjust_exposure`.
8. The `_not_implemented` static helper (around lines 404–414) since `apply_preset` was its only caller.

Do not touch `_handle_import_from_camera`, `_handle_export_images`, `_handle_extract_previews`, `_handle_apply_ratings_batch`, `_handle_open_in_darktable`, or anything `import_from_camera` / `export_images` / `extract_previews` / `apply_ratings_batch` / `open_in_darktable`-shaped.

- [ ] **Step 3: Edit `tests/test_server.py`**

Read the file first. Then:

1. In `test_server_has_required_tools` (around lines 19–31): replace the `expected_tools` list to only contain surviving tools, and tighten the assertion to set-equality so the test fails if a stray tool reappears:
   ```python
   def test_server_has_required_tools(self):
       server = DarktableMCPServer()
       expected_tools = {
           "import_from_camera",
           "export_images",
           "extract_previews",
           "apply_ratings_batch",
           "open_in_darktable",
       }
       assert set(server.list_tools()) == expected_tools
   ```
2. Delete the `TestToolHandlers` class entirely (around lines 48–131). Every method in it targets a removed handler.
3. In `test_all_tools_implemented` (around lines 134–153): replace the list-and-stubbed two-list structure with a single set check identical to the one above, OR delete this test outright since it now duplicates `test_server_has_required_tools`. Delete it — duplication is the bigger sin.
4. In `test_all_imports_work` (around lines 180–191): drop the `LibraryDetector` import line, the `LuaExecutor` import line, the `PhotoTools` import line, and the three `assert ... is not None` lines that reference them. The test should end up only verifying `DarktableMCPServer` imports cleanly. Or just delete the test — the rest of the suite would scream loudly if `DarktableMCPServer` failed to import. Delete it.

- [ ] **Step 4: Edit `tests/test_integration.py`**

Read the file first. Identify and delete every test whose subject is one of the removed tools. Use this checklist:

```bash
grep -n "view_photos\|rate_photos\|import_batch\|adjust_exposure\|apply_preset" tests/test_integration.py
```

For each match, delete the surrounding `def test_*` function (its full body, including any decorators above the `def`). Also delete any `with patch("darktable_mcp.tools.photo_tools.LuaExecutor")` block whose enclosing test is being removed. Keep tests for `import_from_camera`, `export_images`, `extract_previews`, `apply_ratings_batch`, `open_in_darktable`. If a test patches `LuaExecutor` but is *about* a surviving tool (e.g. the patch is incidental), drop just the `LuaExecutor` patch line and replace it with no-op. After your edit, `grep "LuaExecutor" tests/test_integration.py` must return zero hits.

- [ ] **Step 5: Run the suite and confirm acceptance pin flips**

Run: `venv/bin/pytest -q`

Expected: All non-acceptance tests pass. Of the 5 acceptance tests, `test_server_registers_exactly_the_surviving_tools` is now GREEN; the other 4 are still red. Total acceptance: 1/5 green.

If a non-acceptance test fails: most likely cause is a leftover handler reference in `test_server.py` or `test_integration.py`. Fix and re-run. Do not move on with anything red.

- [ ] **Step 6: Commit**

```bash
git add darktable_mcp/server.py tests/test_server.py tests/test_integration.py
git commit -m "refactor(server): unregister broken library tools (view/rate/import/adjust/preset)"
```

---

## Task 3: Delete the 4 broken methods on `PhotoTools`

**Files:**
- Modify: `darktable_mcp/tools/photo_tools.py` — remove 4 methods, the `LuaExecutor` import, and the `__init__` (becomes stateless after the methods are gone)
- Modify: `tests/test_photo_tools.py` — remove cases for the 4 deleted methods

The class is still called `PhotoTools` after this task. The rename happens in Task 4.

- [ ] **Step 1: Edit `darktable_mcp/tools/photo_tools.py`**

Read the file. Then:

1. Delete the import line `from ..darktable.lua_executor import LuaExecutor`.
2. Delete the `__init__` method entirely (around lines 21–33). The class becomes stateless.
3. Delete the four methods: `view_photos` (around 168–239), `rate_photos` (around 241–284), `import_batch` (around 286–317), `adjust_exposure` (around 407–462). Keep `import_from_camera`, `_detect_cameras`, `_download_from_camera`, and the `DOWNLOAD_TIMEOUT_DEFAULT` class attribute.

After the edit, the file should contain only: docstring, imports (no `LuaExecutor`), class declaration, `DOWNLOAD_TIMEOUT_DEFAULT`, and the three camera-related methods. No `__init__`.

- [ ] **Step 2: Edit `tests/test_photo_tools.py`**

Read the file. Identify every test whose subject is one of the deleted methods:

```bash
grep -n "def test\|view_photos\|rate_photos\|import_batch\|adjust_exposure" tests/test_photo_tools.py
```

Delete those `def test_*` blocks (full body, including decorators). Keep tests targeting `import_from_camera`, `_detect_cameras`, `_download_from_camera`. Drop any helper `@patch("darktable_mcp.tools.photo_tools.LuaExecutor")` decorators on surviving tests — they no longer match a real attribute. After the edit, `grep "LuaExecutor" tests/test_photo_tools.py` must return zero hits.

- [ ] **Step 3: Run the suite**

Run: `venv/bin/pytest -q`

Expected: All non-acceptance tests pass. Acceptance still 1/5 green (no module/rename change yet).

- [ ] **Step 4: Commit**

```bash
git add darktable_mcp/tools/photo_tools.py tests/test_photo_tools.py
git commit -m "refactor(photo_tools): drop view/rate/import/adjust methods and LuaExecutor wiring"
```

---

## Task 4: Rename `PhotoTools` → `CameraTools` and the file/test file

**Files:**
- Rename: `darktable_mcp/tools/photo_tools.py` → `darktable_mcp/tools/camera_tools.py`
- Rename: `tests/test_photo_tools.py` → `tests/test_camera_tools.py`
- Modify: `darktable_mcp/server.py` — update import and attribute name
- Test acceptance: `test_photo_tools_module_is_removed` and `test_camera_tools_module_exposes_camera_tools_class` flip green

- [ ] **Step 1: Rename the source file with `git mv`**

Run:
```bash
git mv darktable_mcp/tools/photo_tools.py darktable_mcp/tools/camera_tools.py
```

- [ ] **Step 2: Rename the class inside `camera_tools.py`**

Edit `darktable_mcp/tools/camera_tools.py`:

1. Module docstring: change `"""Photo tools for managing photos in darktable library."""` (or whatever survived) to `"""Camera-import tooling using libgphoto2."""`.
2. `class PhotoTools:` → `class CameraTools:`. Update the class docstring to describe camera import only.

Confirm: `grep "PhotoTools" darktable_mcp/tools/camera_tools.py` returns zero.

- [ ] **Step 3: Rename the test file with `git mv`**

Run:
```bash
git mv tests/test_photo_tools.py tests/test_camera_tools.py
```

- [ ] **Step 4: Rename references inside the test file**

Edit `tests/test_camera_tools.py`. Replace every `PhotoTools` with `CameraTools` and every `darktable_mcp.tools.photo_tools` with `darktable_mcp.tools.camera_tools`. Use a single `Edit` call with `replace_all=true` for each token if convenient.

Confirm: `grep "PhotoTools\|tools.photo_tools" tests/test_camera_tools.py` returns zero.

- [ ] **Step 5: Update `darktable_mcp/server.py`**

Find and replace:
- Import: `from .tools.photo_tools import PhotoTools` → `from .tools.camera_tools import CameraTools`
- Attribute: `self._photo_tools = PhotoTools()` → `self._camera_tools = CameraTools()`
- Every other `self._photo_tools.` → `self._camera_tools.`
- Every other `PhotoTools` reference → `CameraTools`

Also: since `CameraTools.__init__` no longer exists (Task 3 deleted it) and `CameraTools()` cannot raise `DarktableNotFoundError` anymore, find and remove any `try/except DarktableMCPError` wrapper around the `self._camera_tools = CameraTools()` line. If the construction is inside a `try`, hoist it out.

Confirm: `grep "PhotoTools\|_photo_tools" darktable_mcp/server.py` returns zero.

- [ ] **Step 6: Run the suite**

Run: `venv/bin/pytest -q`

Expected: All non-acceptance tests pass. Acceptance now 3/5 green (added `test_photo_tools_module_is_removed` and `test_camera_tools_module_exposes_camera_tools_class`).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(tools): rename PhotoTools to CameraTools and update wiring"
```

---

## Task 5: Delete `LuaExecutor`, `LibraryDetector`, and their tests

**Files:**
- Delete: `darktable_mcp/darktable/lua_executor.py`
- Delete: `darktable_mcp/darktable/library_detector.py`
- Delete: `darktable_mcp/darktable/scripts/` (only if it exists and is empty)
- Delete: `tests/test_lua_executor_headless.py`
- Delete: `tests/test_library_detector.py`
- Modify: `darktable_mcp/darktable/__init__.py` — drop `LuaExecutor` from import and `__all__`
- Modify: `tests/test_darktable.py` — drop `TestLuaExecutor` class, keep `TestCLIWrapper`
- Test acceptance: the remaining two acceptance tests flip green

- [ ] **Step 1: Delete the two source files**

Run:
```bash
git rm darktable_mcp/darktable/lua_executor.py darktable_mcp/darktable/library_detector.py
```

- [ ] **Step 2: Delete the empty `scripts/` directory if it exists**

Run:
```bash
[ -d darktable_mcp/darktable/scripts ] && rmdir darktable_mcp/darktable/scripts || echo "scripts dir already absent"
```

Expected: either `rmdir` succeeds silently or the echo prints `scripts dir already absent`. If `rmdir` fails because the directory is non-empty, stop and inspect — the spec says it is empty; if it isn't, report the contents instead of force-deleting.

- [ ] **Step 3: Delete the two test files**

Run:
```bash
git rm tests/test_lua_executor_headless.py tests/test_library_detector.py
```

- [ ] **Step 4: Edit `darktable_mcp/darktable/__init__.py`**

Read first. The file currently has:
```python
from .cli_wrapper import CLIWrapper
from .lua_executor import LuaExecutor

__all__ = ["CLIWrapper", "LuaExecutor"]
```

Replace with:
```python
from .cli_wrapper import CLIWrapper

__all__ = ["CLIWrapper"]
```

(Preserve the module docstring if present.)

- [ ] **Step 5: Edit `tests/test_darktable.py`**

Read first. The file has two classes: `TestLuaExecutor` (around lines 12–46) and `TestCLIWrapper` (from around line 48 onward).

1. Delete the `from darktable_mcp.darktable.lua_executor import LuaExecutor` import line at the top of the file.
2. Delete the entire `TestLuaExecutor` class.
3. Keep `TestCLIWrapper` and the `from darktable_mcp.darktable.cli_wrapper import CLIWrapper` import.

Confirm: `grep "LuaExecutor" tests/test_darktable.py` returns zero.

- [ ] **Step 6: Final grep audit**

Run:
```bash
grep -rn "LuaExecutor\|LibraryDetector" darktable_mcp tests || echo "clean"
```

Expected: prints `clean`. If anything matches, find the leftover and delete it before committing.

- [ ] **Step 7: Run the full suite**

Run: `venv/bin/pytest -q`

Expected: All tests pass. Acceptance now 5/5 green. Total test count is significantly lower than the starting 177.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(darktable): delete LuaExecutor, LibraryDetector, and their tests"
```

---

## Task 6: Update README to match reality

**Files:**
- Modify: `README.md`

The spec lists exactly which sections to touch. Apply each edit precisely.

- [ ] **Step 1: Read the current README**

Read `README.md` so the edits land at the right line numbers. Sections to touch (line numbers in the current file are approximate — the actual edits use `Edit` with unique surrounding context):

1. **"Implemented tools"** intro list (around lines 9–24): the section that begins `**Implemented tools:**` and lists library / vision-rating / GUI bullets. Drop the `view_photos`, `rate_photos`, `import_batch`, and `adjust_exposure` bullets. Keep camera, vision-rating, `open_in_darktable`, `export_images`.
2. **"Not yet implemented"** (around line 26): currently lists `apply_preset`. Replace the body of this section with: `The full library-aware tool set (browsing, rating, importing into the library, exposure adjustment, preset application) is deferred to iteration 2 — see the spec under \`docs/superpowers/specs/\` once written.`
3. **"Quick test"** block (around lines 68–74): the three example questions reference `view_photos`, `rate_photos`, `import_batch`. Replace them with these three:
   - `# "Pull a folder of raws off my camera" (uses import_from_camera)`
   - `# "Extract previews from ~/Pictures/import-2026-04-26" (uses extract_previews)`
   - `# "Open ~/Pictures/import-2026-04-26 in darktable, filtered to 5 stars" (uses open_in_darktable)`
4. **"Implemented tools"** detailed section (around lines 96–116): the long-form descriptions. Drop the four detail entries for `view_photos`, `rate_photos`, `import_batch`, `adjust_exposure`. Keep the rest.
5. **"Stubbed tools (registered, not implemented)"** section (around line 113): the section header and the `apply_preset` entry. Delete the entire section (header + entry) — there are no stubbed tools left.
6. **"Why some tools are parked"** section: append, on its own line at the end of the section: `> The iteration that builds the long-running plugin + IPC will be tracked in a separate spec under \`docs/superpowers/specs/\`.`

- [ ] **Step 2: Apply the six edits**

Use `Edit` calls with surrounding context to make each change unique. Do not use `replace_all`.

- [ ] **Step 3: Sanity check**

Run:
```bash
grep -E "view_photos|rate_photos|import_batch|adjust_exposure|apply_preset" README.md || echo "clean"
```

Expected: prints `clean`.

- [ ] **Step 4: Run the suite (no source changes, but cheap)**

Run: `venv/bin/pytest -q`

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs(readme): list only tools that work; defer library tools to iteration 2"
```

---

## Task 7: Final acceptance, then push

**Files:**
- No code changes; verification only.

- [ ] **Step 1: Re-verify the grep audits**

Run, in this exact order:
```bash
grep -rn "LuaExecutor" darktable_mcp tests || echo "clean: LuaExecutor"
grep -rn "LibraryDetector" darktable_mcp tests || echo "clean: LibraryDetector"
grep -rn "PhotoTools" darktable_mcp tests || echo "clean: PhotoTools"
grep -rn "_photo_tools" darktable_mcp tests || echo "clean: _photo_tools"
grep -E "view_photos|rate_photos|import_batch|adjust_exposure|apply_preset" README.md || echo "clean: README"
```

Expected: every line ends with `clean: <name>`. Any other output is a leftover; fix it in a small follow-up commit before pushing.

- [ ] **Step 2: Run the full suite one last time**

Run: `venv/bin/pytest -q`

Expected: all tests pass, including the 5 acceptance tests in `tests/test_honesty_pass_acceptance.py`.

- [ ] **Step 3: Eyeball the diff against `origin/main`**

Run: `git log --oneline origin/main..HEAD` and `git diff --stat origin/main..HEAD`.

Expected log entries (in order):
1. `test: pin honesty-pass acceptance state`
2. `refactor(server): unregister broken library tools (view/rate/import/adjust/preset)`
3. `refactor(photo_tools): drop view/rate/import/adjust methods and LuaExecutor wiring`
4. `refactor(tools): rename PhotoTools to CameraTools and update wiring`
5. `refactor(darktable): delete LuaExecutor, LibraryDetector, and their tests`
6. `docs(readme): list only tools that work; defer library tools to iteration 2`

`git diff --stat` should show net deletions on the order of −1500 lines.

- [ ] **Step 4: Push to origin/main**

This step requires explicit user approval per the project's git safety conventions. Pause and ask: `Ready to push 6 commits to origin/main? (yes/no)`. Only push on `yes`.

```bash
git push origin main
```

- [ ] **Step 5: Mark done**

Iteration 1 is shipped. Iteration 2 (IPC bridge) needs its own brainstorming → spec → plan cycle, kicked off as a separate session.
