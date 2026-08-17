"""File-based JSON request/response bridge to the darktable Lua plugin."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

#: How long to sleep between polls of the response file. Small enough that a
#: fast plugin round-trip still feels instant, large enough not to spin a core.
POLL_INTERVAL_SECONDS = 0.05

#: Fallback wait for methods with no entry in :data:`DEFAULT_TIMEOUTS`.
FALLBACK_TIMEOUT = 15.0

#: Per-method wait budgets, in seconds. These are cost estimates for the work
#: the Lua side actually does: `view_photos` linearly scans `dt.database` in
#: interpreted Lua, and `import_batch` / `apply_preset` drive darktable's own
#: importer, which sleeps internally. A single 5s budget for everything made
#: slow-but-healthy calls look like "darktable is not running".
DEFAULT_TIMEOUTS = {
    "view_photos": 30.0,
    "rate_photos": 30.0,
    "import_batch": 120.0,
    "list_styles": 15.0,
    "apply_preset": 120.0,
}


def resolve_timeout(method: str, timeout: float | None = None) -> float:
    """Resolve the wait budget for one bridge call.

    Args:
        method: Bridge method name.
        timeout: Explicit caller override. Wins over every default when given.

    Returns:
        float: Seconds to wait before raising :class:`BridgeTimeoutError`.
    """
    if timeout is not None:
        return timeout
    return DEFAULT_TIMEOUTS.get(method, FALLBACK_TIMEOUT)


class BridgeError(Exception):
    """Plugin returned an explicit error in its response."""


class BridgeTimeoutError(BridgeError):
    """No response within the configured timeout."""


class BridgePluginNotInstalledError(BridgeError):
    """The Lua plugin file is not present in the user's darktable config."""


class BridgeProtocolError(BridgeError):
    """Response file existed but did not match the expected schema."""


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "darktable-mcp"


def _plugin_path() -> Path:
    return Path.home() / ".config" / "darktable" / "lua" / "darktable_mcp.lua"


class Bridge:
    """Synchronous file-based JSON-RPC client to the darktable Lua plugin.

    Each `call` writes one request file and waits for the matching response
    file. Atomic writes via tmp+rename. Cleans up its own request file on
    timeout and its response file after read.
    """

    def __init__(self, cache_dir: Path | None = None, plugin_path: Path | None = None):
        self._cache_dir = cache_dir or _cache_dir()
        self._plugin_path = plugin_path or _plugin_path()

    def call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        """Send one request to the plugin and wait for its response.

        Args:
            method: Bridge method name.
            params: Method params, serialised into the request file as-is.
            timeout: Seconds to wait. ``None`` resolves via
                :data:`DEFAULT_TIMEOUTS`, then :data:`FALLBACK_TIMEOUT`.

        Returns:
            Any: The plugin's ``result`` field.

        Raises:
            BridgePluginNotInstalledError: The Lua plugin file is absent.
            BridgeTimeoutError: No response arrived within the budget.
            BridgeError: The plugin answered with an ``error`` field.
            BridgeProtocolError: The response did not match the schema.
        """
        timeout = resolve_timeout(method, timeout)

        if not self._plugin_path.is_file():
            raise BridgePluginNotInstalledError(
                f"plugin not installed at {self._plugin_path}. " "Run: darktable-mcp install-plugin"
            )

        self._cache_dir.mkdir(parents=True, exist_ok=True)

        req_id = str(uuid.uuid4())
        req_path = self._cache_dir / f"request-{req_id}.json"
        resp_path = self._cache_dir / f"response-{req_id}.json"
        payload = json.dumps(
            {"id": req_id, "method": method, "params": params},
            ensure_ascii=False,
        )

        # Atomic write: tmp + rename.
        tmp = req_path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.rename(tmp, req_path)

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if resp_path.exists():
                    try:
                        text = resp_path.read_text(encoding="utf-8")
                    except OSError:
                        time.sleep(POLL_INTERVAL_SECONDS)
                        continue
                    try:
                        response = json.loads(text)
                    except json.JSONDecodeError as e:
                        raise BridgeProtocolError(
                            f"response not valid JSON: {e}; payload: {text!r}"
                        )
                    finally:
                        resp_path.unlink(missing_ok=True)

                    if not isinstance(response, dict) or "id" not in response:
                        raise BridgeProtocolError(f"response missing id field: {response!r}")
                    if "error" in response:
                        raise BridgeError(str(response["error"]))
                    if "result" not in response:
                        raise BridgeProtocolError(
                            f"response has neither error nor result: {response!r}"
                        )
                    return response["result"]
                time.sleep(POLL_INTERVAL_SECONDS)

            raise BridgeTimeoutError(
                f"method {method!r} got no plugin response within its " f"{timeout:g}s timeout"
            )
        finally:
            # Best-effort cleanup of our own request file.
            req_path.unlink(missing_ok=True)
