"""Tests for darktable_mcp.bridge.client.Bridge."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from darktable_mcp.bridge.client import (
    Bridge,
    BridgeError,
    BridgePluginNotInstalledError,
    BridgeProtocolError,
    BridgeTimeoutError,
)


class FakePlugin:
    """Background thread that mimics the Lua plugin: watches the cache dir,
    writes a canned response when a request file appears."""

    def __init__(self, cache_dir: Path, response_factory):
        self.cache_dir = cache_dir
        self.response_factory = response_factory  # request_dict -> response_dict
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.is_set():
            for req_path in sorted(self.cache_dir.glob("request-*.json")):
                if req_path.name.endswith(".tmp"):
                    continue
                try:
                    text = req_path.read_text(encoding="utf-8")
                    req = json.loads(text)
                except (OSError, json.JSONDecodeError):
                    continue
                response = self.response_factory(req)
                resp_path = self.cache_dir / f"response-{req['id']}.json"
                tmp_path = resp_path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(response), encoding="utf-8")
                os.rename(tmp_path, resp_path)
                req_path.unlink(missing_ok=True)
            time.sleep(0.02)


@pytest.fixture
def fake_plugin_lua_file(tmp_path, monkeypatch):
    """Make plugin-installed detection succeed by faking the install location."""
    fake_home = tmp_path / "home"
    plugin_dir = fake_home / ".config" / "darktable" / "lua"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "darktable_mcp.lua").write_text("-- fake")
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    cache = tmp_path / "cache" / "darktable-mcp"
    cache.mkdir(parents=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return cache


def test_call_returns_result_on_happy_path(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"id": req["id"], "result": [{"id": "1", "filename": "a.NEF"}]},
    )
    plugin.start()
    try:
        bridge = Bridge()
        result = bridge.call("view_photos", {"limit": 10})
        assert result == [{"id": "1", "filename": "a.NEF"}]
    finally:
        plugin.stop()


def test_call_raises_timeout_when_no_response(cache_dir, fake_plugin_lua_file):
    bridge = Bridge()
    with pytest.raises(BridgeTimeoutError):
        bridge.call("view_photos", {}, timeout=0.5)


def test_call_raises_plugin_not_installed_when_lua_missing(cache_dir, tmp_path, monkeypatch):
    # No plugin file written.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bridge = Bridge()
    with pytest.raises(BridgePluginNotInstalledError):
        bridge.call("view_photos", {})


def test_call_raises_bridge_error_on_error_response(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"id": req["id"], "error": "image 999 not in library"},
    )
    plugin.start()
    try:
        bridge = Bridge()
        with pytest.raises(BridgeError, match="image 999 not in library"):
            bridge.call("rate_photos", {"photo_ids": ["999"], "rating": 5})
    finally:
        plugin.stop()


def test_call_raises_protocol_error_on_malformed_response(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"not_id": "x", "garbage": True},
    )
    plugin.start()
    try:
        bridge = Bridge()
        with pytest.raises(BridgeProtocolError):
            bridge.call("view_photos", {})
    finally:
        plugin.stop()


def test_error_field_wins_over_result_field(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"id": req["id"], "result": "ignored", "error": "boom"},
    )
    plugin.start()
    try:
        bridge = Bridge()
        with pytest.raises(BridgeError, match="boom"):
            bridge.call("view_photos", {})
    finally:
        plugin.stop()


def test_call_cleans_up_request_file_on_timeout(cache_dir, fake_plugin_lua_file):
    bridge = Bridge()
    with pytest.raises(BridgeTimeoutError):
        bridge.call("view_photos", {}, timeout=0.5)
    leftover = list(cache_dir.glob("request-*.json"))
    assert leftover == [], f"request file not cleaned up: {leftover}"


def test_call_cleans_up_response_file_on_success(cache_dir, fake_plugin_lua_file):
    plugin = FakePlugin(
        cache_dir, lambda req: {"id": req["id"], "result": "ok"}
    )
    plugin.start()
    try:
        bridge = Bridge()
        bridge.call("view_photos", {})
        leftover = list(cache_dir.glob("response-*.json"))
        assert leftover == [], f"response file not cleaned up: {leftover}"
    finally:
        plugin.stop()


def test_concurrent_calls_get_correct_results(cache_dir, fake_plugin_lua_file):
    # The fake plugin echoes the params back as the result so each caller can
    # verify it got its OWN response, not someone else's.
    plugin = FakePlugin(
        cache_dir,
        lambda req: {"id": req["id"], "result": req["params"]},
    )
    plugin.start()
    try:
        bridge = Bridge()
        results = {}
        errors = []

        def make_call(label):
            try:
                results[label] = bridge.call("echo", {"label": label})
            except Exception as e:
                errors.append((label, e))

        threads = [threading.Thread(target=make_call, args=(f"thread-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], errors
        for i in range(5):
            label = f"thread-{i}"
            assert results[label] == {"label": label}
    finally:
        plugin.stop()
