"""File-based JSON request/response bridge to the darktable Lua plugin."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


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

    def call(self, method: str, params: dict[str, Any], timeout: float = 5.0) -> Any:
        if not self._plugin_path.is_file():
            raise BridgePluginNotInstalledError(
                f"plugin not installed at {self._plugin_path}. "
                "Run: darktable-mcp install-plugin"
            )

        self._cache_dir.mkdir(parents=True, exist_ok=True)

        req_id = str(uuid.uuid4())
        req_path = self._cache_dir / f"request-{req_id}.json"
        resp_path = self._cache_dir / f"response-{req_id}.json"
        payload = json.dumps({"id": req_id, "method": method, "params": params})

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
                        time.sleep(0.05)
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
                        raise BridgeProtocolError(
                            f"response missing id field: {response!r}"
                        )
                    if "error" in response:
                        raise BridgeError(str(response["error"]))
                    if "result" not in response:
                        raise BridgeProtocolError(
                            f"response has neither error nor result: {response!r}"
                        )
                    return response["result"]
                time.sleep(0.05)

            raise BridgeTimeoutError(
                f"no response from plugin within {timeout}s for method {method!r}"
            )
        finally:
            # Best-effort cleanup of our own request file.
            req_path.unlink(missing_ok=True)
