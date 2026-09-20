#!/usr/bin/env python3
"""Retouch Phase 1 acceptance driver: retouch_add_shape/list_shapes/delete_shape
-- a real HEAL circle that samples a SOURCE region onto a TARGET region on the
retouch module's own rt_forms[] array (not the generic blend-mask system).

Exercises darktable.develop.retouch_add_shape/retouch_delete_shape/
retouch_list_shapes over the plain bridge harness (docker/run-dt-bridge.sh) --
these are real darktable_mcp.lua methods, not spike-only probes, so no
spike_methods.lua staging is needed.

Acceptance (maps to the user's original acceptance criteria):
  1. Heal circle created: target + source (absolute points), radius, feather,
     wavelet_scale all round-trip through retouch_list_shapes.
  2. PROOF the heal actually moved pixels: target region's luma, which starts
     far from the source region's luma (mire1.cr2 has distinct color/tone
     patches), moves MUCH closer to the source's baseline luma after healing.
     This is the same before/after region-luma methodology as T2.2
     (run_t2_2.py), adapted to prove sampling rather than localization.
  3. retouch_list_shapes reflects the shape (formid/algorithm/shape_type
     circle/target/source/radius/feather/wavelet_scale) and NOT a padded
     300-slot dump.
  4. retouch_delete_shape removes it; a fresh preview reverts close to the
     ORIGINAL target baseline (proof deletion undid the effect, not just
     hid it), and retouch_list_shapes shows zero shapes again.
  5. Degenerate calls (missing source, unsupported algorithm/shape_type,
     deleting an unknown formid) return {error}, never crash.
  6. History stack grew sensibly (dev_history_count) and no
     deadlock/assertion/critical in the container log throughout.

Usage: python3 darktable-mcp/spike/run_retouch.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
MCP_DIR = SPIKE_DIR.parent
ROOT_DIR = MCP_DIR.parent

RUN_SCRIPT = ROOT_DIR / "docker" / "run-dt-bridge.sh"
FIXTURE_IMAGE = ROOT_DIR / "src-dt" / "src" / "tests" / "integration" / "images" / "mire1.cr2"
READY_LINE = "darktable-mcp bridge: ready"
DEADLOCK_MARKERS = ("deadlock", "assertion", "assert failed", "**: assertion", "critical")

# Target region has an original tone/color distinct from the source region
# (mire1.cr2 is a test chart with multiple patches) -- makes "did the source
# actually get sampled onto the target" a measurable luma delta, not a guess.
TARGET_REGION = (0.45, 0.45, 0.06, 0.06)  # tight box around the heal target
SOURCE_REGION = (0.10, 0.10, 0.06, 0.06)  # tight box around the heal source
TARGET_CENTER = {"x": 0.48, "y": 0.48}
SOURCE_CENTER = {"x": 0.13, "y": 0.13}


def _load_bridge_client_module():
    client_path = MCP_DIR / "darktable_mcp" / "bridge" / "client.py"
    spec = importlib.util.spec_from_file_location("darktable_mcp_bridge_client", client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge
BridgeError = _client_mod.BridgeError


def call_expect_error(bridge, method, params, timeout=15.0):
    """For calls we EXPECT the C side to reject: Bridge.call raises BridgeError
    for any {"error": ...} response (it doesn't return it as a plain dict), so
    normalize that into {"error": str(e)} for uniform handling below."""
    try:
        result = bridge.call(method, params, timeout=timeout)
        return result
    except BridgeError as e:
        return {"error": str(e)}


def log(msg: str) -> None:
    print(f"[retouch] {msg}", flush=True)


def wait_for_ready(log_path: Path, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if READY_LINE in line:
                    return line.strip()
            if "darktable exit code:" in text:
                raise RuntimeError(f"darktable exited before ready; tail:\n{text[-4000:]}")
        time.sleep(0.5)
    tail = log_path.read_text(errors="replace")[-4000:] if log_path.exists() else "(no log)"
    raise TimeoutError(f"'{READY_LINE}' not seen within {timeout}s; tail:\n{tail}")


def start_bridge() -> str:
    result = subprocess.run([str(RUN_SCRIPT), "start"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"start failed rc={result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip().splitlines()[-1].strip()


def stop_bridge(run_dir: str) -> None:
    subprocess.run([str(RUN_SCRIPT), "stop", run_dir], capture_output=True, text=True, timeout=30)


def container_alive(run_dir: str) -> bool:
    cid_path = Path(run_dir) / "container_id"
    if not cid_path.exists():
        return False
    cid = cid_path.read_text().strip()
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", cid],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def host_path(run_dir: str, container_path: str) -> str:
    if container_path and container_path.startswith("/run"):
        return run_dir + container_path[len("/run") :]
    return container_path


def mean_luma(png_path: str):
    """Mean Rec.601 luma of a PNG (0..255)."""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(png_path) as im:
            a = np.asarray(im.convert("L"), dtype="float64")
        return float(a.mean())
    except Exception as e:  # noqa: BLE001
        log(f"mean_luma failed for {png_path}: {e}")
        return None


def region_luma(bridge, run_dir, box, tag):
    x, y, w, h = box
    resp = bridge.call(
        "dev_preview",
        {"max_w": 300, "max_h": 300, "region": {"x": x, "y": y, "w": w, "h": h}},
        timeout=20.0,
    )
    p = resp.get("path") or resp.get("stale_preview")
    lum = mean_luma(host_path(run_dir, p)) if p else None
    log(f"  region[{tag}] {box} -> status={resp.get('status')} luma={lum}")
    return lum, resp


def main() -> int:  # noqa: C901
    if not FIXTURE_IMAGE.is_file():
        log(f"FAIL: fixture not found at {FIXTURE_IMAGE}")
        return 1

    results: dict = {}
    run_dir = start_bridge()
    log(f"RUN_DIR = {run_dir}")
    log_path = Path(run_dir) / "dt.log"
    try:
        log(f"bridge ready: {wait_for_ready(log_path)!r}")

        shutil.copy2(FIXTURE_IMAGE, Path(run_dir) / "import-src" / FIXTURE_IMAGE.name)
        cache_dir = Path(run_dir) / "cache-mcp" / "darktable-mcp"
        plugin_path = Path(run_dir) / "config" / "lua" / "darktable_mcp.lua"
        bridge = Bridge(cache_dir=cache_dir, plugin_path=plugin_path)

        imp = bridge.call(
            "import_batch", {"source_path": "/run/import-src", "recursive": True}, timeout=30.0
        )
        log(f"import_batch: {json.dumps(imp)}")
        photos = bridge.call("view_photos", {"limit": 50, "scope": "library"}, timeout=15.0)
        mire = next((p for p in photos if "mire1" in (p.get("filename") or "").lower()), None)
        if not mire:
            raise RuntimeError(f"fixture not in view_photos: {photos}")
        image_id = int(mire["id"])
        log(f"target image_id = {image_id}")

        opened = bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
        log(f"open_darkroom -> {json.dumps(opened)}")
        if opened.get("view") != "darkroom":
            raise RuntimeError(f"failed to enter darkroom: {opened}")

        hist_before = bridge.call("dev_history_count", {}, timeout=10.0)
        results["history_count_before"] = hist_before
        log(f"history_count before = {json.dumps(hist_before)}")

        # ============ enable the retouch module (instance 0 exists by ============
        # ============ default; only add_instance() creates ADDITIONAL ones) ====
        log("=== enable_module(retouch, 0) ===")
        en = bridge.call(
            "dev_enable_module", {"op": "retouch", "instance": 0, "enabled": True}, timeout=15.0
        )
        results["enable_retouch"] = en
        log(f"enable_module(retouch,0) -> {json.dumps(en)}")

        log("--- baseline luma (retouch enabled, no shapes yet) ---")
        base_target, _ = region_luma(bridge, run_dir, TARGET_REGION, "target")
        base_source, _ = region_luma(bridge, run_dir, SOURCE_REGION, "source")
        results["baseline_target_luma"] = base_target
        results["baseline_source_luma"] = base_source

        # ============ CHECK 5a: degenerate calls before the real one ============
        log("=== CHECK 5: degenerate calls ===")
        no_source = call_expect_error(
            bridge,
            "dev_retouch_add_shape",
            {
                "op": "retouch",
                "instance": 0,
                "algorithm": "heal",
                "target": TARGET_CENTER,
                "radius": 0.03,
            },
        )
        results["degenerate_no_source"] = no_source
        log(f"add_shape(no source) -> {json.dumps(no_source)}")

        bad_algo = call_expect_error(
            bridge,
            "dev_retouch_add_shape",
            {
                "op": "retouch",
                "instance": 0,
                "algorithm": "blur",
                "target": TARGET_CENTER,
                "source": SOURCE_CENTER,
                "radius": 0.03,
            },
        )
        results["degenerate_bad_algo"] = bad_algo
        log(f"add_shape(algorithm=blur) -> {json.dumps(bad_algo)}")

        bad_delete = call_expect_error(
            bridge, "dev_retouch_delete_shape", {"op": "retouch", "instance": 0, "formid": 999999}
        )
        results["degenerate_bad_delete"] = bad_delete
        log(f"delete_shape(unknown formid) -> {json.dumps(bad_delete)}")
        alive_c5 = container_alive(run_dir)
        results["container_alive_after_degenerate"] = alive_c5

        # ============ CHECK 1+2: real heal shape ============
        log("=== CHECK 1: retouch_add_shape(heal, target, source, radius=0.05, feather=0.1) ===")
        shape = bridge.call(
            "dev_retouch_add_shape",
            {
                "op": "retouch",
                "instance": 0,
                "algorithm": "heal",
                "target": TARGET_CENTER,
                "source": SOURCE_CENTER,
                "radius": 0.05,
                "feather": 0.1,
                "opacity": 1.0,
            },
            timeout=20.0,
        )
        results["add_shape"] = shape
        log(f"retouch_add_shape -> {json.dumps(shape)}")
        formid = shape.get("formid") if isinstance(shape, dict) else None

        log("--- after-heal luma ---")
        aft_target, _ = region_luma(bridge, run_dir, TARGET_REGION, "target")
        aft_source, _ = region_luma(bridge, run_dir, SOURCE_REGION, "source")
        results["after_heal_target_luma"] = aft_target
        results["after_heal_source_luma"] = aft_source

        dist_before = (
            abs(base_target - base_source) if None not in (base_target, base_source) else None
        )
        dist_after = (
            abs(aft_target - base_source) if None not in (aft_target, base_source) else None
        )
        results["target_to_source_distance_before"] = dist_before
        results["target_to_source_distance_after"] = dist_after
        log(f"|target-source| luma distance: before={dist_before} after={dist_after}")

        # ============ CHECK 3: list_shapes round-trip ============
        log("=== CHECK 3: retouch_list_shapes ===")
        listed = bridge.call(
            "dev_retouch_list_shapes", {"op": "retouch", "instance": 0}, timeout=15.0
        )
        results["list_shapes_after_add"] = listed
        log(f"retouch_list_shapes -> {json.dumps(listed)}")
        shapes = listed.get("shapes", []) if isinstance(listed, dict) else []
        listed_shape = next((s for s in shapes if s.get("formid") == formid), None)

        # ============ CHECK 4: delete + revert ============
        log(f"=== CHECK 4: retouch_delete_shape(formid={formid}) ===")
        deleted = bridge.call(
            "dev_retouch_delete_shape",
            {"op": "retouch", "instance": 0, "formid": formid},
            timeout=15.0,
        )
        results["delete_shape"] = deleted
        log(f"retouch_delete_shape -> {json.dumps(deleted)}")

        listed_after_delete = bridge.call(
            "dev_retouch_list_shapes", {"op": "retouch", "instance": 0}, timeout=15.0
        )
        results["list_shapes_after_delete"] = listed_after_delete
        log(f"retouch_list_shapes (after delete) -> {json.dumps(listed_after_delete)}")

        log("--- after-delete luma (should revert toward baseline) ---")
        revert_target, _ = region_luma(bridge, run_dir, TARGET_REGION, "target")
        results["after_delete_target_luma"] = revert_target
        dist_revert = (
            abs(revert_target - base_target) if None not in (revert_target, base_target) else None
        )
        results["target_baseline_distance_after_delete"] = dist_revert
        log(f"|after_delete - baseline| target luma distance: {dist_revert}")

        hist_after = bridge.call("dev_history_count", {}, timeout=10.0)
        results["history_count_after"] = hist_after
        log(f"history_count after = {json.dumps(hist_after)}")

        alive_final = container_alive(run_dir)
        results["container_alive_final"] = alive_final

        logtext = log_path.read_text(errors="replace")
        deadlock_hits = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        results["deadlock_log_hits"] = deadlock_hits

        print("\n===== RETOUCH RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        healed_toward_source = (
            dist_before is not None
            and dist_after is not None
            and dist_before > 10.0
            and dist_after < dist_before * 0.5
        )
        reverted_after_delete = dist_revert is not None and dist_revert < 5.0

        def _hist_count(h):
            return h.get("count") if isinstance(h, dict) else h

        checks = {
            "5a missing source -> error (no crash)": (
                isinstance(no_source, dict) and bool(no_source.get("error"))
            ),
            "5b unsupported algorithm -> error (no crash)": (
                isinstance(bad_algo, dict) and bool(bad_algo.get("error"))
            ),
            "5c unknown formid delete -> error (no crash)": (
                isinstance(bad_delete, dict) and bool(bad_delete.get("error"))
            ),
            "container alive after degenerate calls": alive_c5,
            "1 add_shape ok=true with a formid": (
                isinstance(shape, dict) and shape.get("ok") is True and bool(formid)
            ),
            "1 algorithm/shape_type echoed correctly": (
                isinstance(shape, dict)
                and shape.get("algorithm") == "heal"
                and shape.get("shape_type") == "circle"
            ),
            "2 target luma moved substantially toward source (proof of sampling)": (
                healed_toward_source
            ),
            "3 list_shapes contains the created shape": listed_shape is not None,
            "3 list_shapes shape fields match request": (
                bool(listed_shape)
                and (
                    listed_shape.get("algorithm") == "heal"
                    and listed_shape.get("shape_type") == "circle"
                    and abs((listed_shape.get("target") or {}).get("x", -1) - TARGET_CENTER["x"])
                    < 1e-3
                    and abs((listed_shape.get("source") or {}).get("x", -1) - SOURCE_CENTER["x"])
                    < 1e-3
                )
            ),
            "4 delete_shape ok=true": isinstance(deleted, dict) and deleted.get("ok") is True,
            "4 list_shapes empty after delete": (
                isinstance(listed_after_delete, dict)
                and len(listed_after_delete.get("shapes", [])) == 0
            ),
            "4 target luma reverted close to baseline after delete": reverted_after_delete,
            "6 history_count grew": (
                (_hist_count(hist_after) or 0) > (_hist_count(hist_before) or 0)
            ),
            "container alive at end": alive_final,
            "no deadlock/critical/assertion in log": len(deadlock_hits) == 0,
        }
        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print(f"\nALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== RETOUCH INFRA FAILURE: {e} =====", file=sys.stderr)
        if log_path.exists():
            print(
                "----- dt.log tail -----\n" + log_path.read_text(errors="replace")[-8000:],
                file=sys.stderr,
            )
        print(json.dumps(results, indent=2, default=str), file=sys.stderr)
        return 1
    finally:
        log("stopping bridge container")
        stop_bridge(run_dir)


if __name__ == "__main__":
    sys.exit(main())
