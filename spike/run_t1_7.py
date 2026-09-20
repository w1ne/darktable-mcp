#!/usr/bin/env python3
"""T1.7 acceptance driver: generic ARRAY + STRUCT decode/encode (curves).

Proves darktable.develop.get_params/set_params now handle composite params
(arrays, structs, array-of-struct curves) generically -- the tonecurve L-channel
curve round-trips as an array of {x,y} objects, a gentle S-curve raises image
contrast (luma std-dev), and the over-capacity guard is enforced. Reuses the
same spike bridge as run_t1_5.py (docker/run-dt-bridge-spike.sh +
spike_methods.lua, now registering dev_enable_module + dev_dump_introspection).
Does NOT touch the clean bridge.

Acceptance points (all quoted in the final report), PLAN.md §3.1 / J2:
  0. dump_introspection(tonecurve): how get_introspection_linear represents the
     2D array-of-struct + the root-struct top-level field list.
  1. get_params(tonecurve,0): L-channel curve = array of {x,y} objects (NOT the
     old {type=array} placeholder) + the tonecurve_nodes count field. Plus one
     nested shape from another module (colorbalancergb / denoiseprofile).
  2. enable + set a gentle S-curve on L via set_params -> ok; get_params readback
     shows the new nodes + count.
  3. get_preview luma std-dev BEFORE (identity) vs AFTER (S-curve): must rise.
  4. Over-capacity: set >capacity nodes -> clamped/reported, no crash.
  5. Scalar regression: get/set exposure still works unchanged.

Usage: python3 darktable-mcp/spike/run_t1_7.py
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

RUN_SCRIPT = ROOT_DIR / "docker" / "run-dt-bridge-spike.sh"
FIXTURE_IMAGE = ROOT_DIR / "src-dt" / "src" / "tests" / "integration" / "images" / "mire1.cr2"
READY_LINE = "darktable-mcp bridge: ready"
DEADLOCK_MARKERS = ("deadlock", "assertion", "assert failed", "**: assertion", "critical")


def _load_bridge_client_module():
    client_path = MCP_DIR / "darktable_mcp" / "bridge" / "client.py"
    spec = importlib.util.spec_from_file_location("darktable_mcp_bridge_client", client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge


def log(msg: str) -> None:
    print(f"[t1.7] {msg}", flush=True)


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


def luma_stats(path: str):
    """(mean, std) of the L (luma) channel of a PNG, or (None, None)."""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            arr = np.asarray(im.convert("L"), dtype="float64")
            return float(arr.mean()), float(arr.std())
    except Exception as e:  # noqa: BLE001
        log(f"luma_stats skipped: {e}")
        return None, None


def first_few_nodes(curve_channel, n=4):
    """curve_channel is a Lua array (list) of {x,y} dicts."""
    if not isinstance(curve_channel, list):
        return None
    return curve_channel[:n]


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
        photos = bridge.call("view_photos", {"limit": 50}, timeout=15.0)
        mire = next((p for p in photos if "mire1" in (p.get("filename") or "").lower()), None)
        if not mire:
            raise RuntimeError(f"fixture not in view_photos: {photos}")
        image_id = int(mire["id"])
        log(f"target image_id = {image_id}")

        opened = bridge.call("open_darkroom", {"image_id": image_id}, timeout=20.0)
        log(f"open_darkroom -> {json.dumps(opened)}")
        if opened.get("view") != "darkroom":
            raise RuntimeError(f"failed to enter darkroom: {opened}")

        # ---------- CHECK 0: how the linear list represents composites ----------
        log("=== CHECK 0: dump_introspection(tonecurve) ===")
        dump = bridge.call(
            "dev_dump_introspection", {"op": "tonecurve", "instance": 0}, timeout=15.0
        )
        results["introspection_linear"] = dump.get("linear")
        results["introspection_top_level"] = dump.get("top_level")
        log(f"tonecurve top-level fields = {json.dumps(dump.get('top_level'))}")
        for e in dump.get("linear", []):
            extra = ""
            if e.get("array_count") is not None:
                extra = f" count={e['array_count']} elem={e.get('array_elem_type')}"
            log(f"  linear: name={e['name']!r:32} type={e['type']:7} offset={e['offset']}{extra}")

        # ---------- CHECK 1: get_params tonecurve -> curve as array of {x,y} ----------
        log("=== CHECK 1: get_params(tonecurve,0) ===")
        gp = bridge.call("dev_get_params", {"op": "tonecurve", "instance": 0}, timeout=15.0)
        fields = gp.get("fields", {})
        tc = fields.get("tonecurve")
        tcn = fields.get("tonecurve_nodes")
        results["tonecurve_field_is_placeholder"] = (
            isinstance(tc, dict) and tc.get("type") == "array"
        )
        # L channel = outer[0] (Lua 1-based -> index 0 in the JSON list)
        l_channel = tc[0] if isinstance(tc, list) and tc else None
        results["get1_L_channel_first_nodes"] = first_few_nodes(l_channel, 4)
        results["get1_tonecurve_nodes"] = tcn
        log(f"tonecurve is placeholder? {results['tonecurve_field_is_placeholder']}")
        log(f"L-channel first nodes = {json.dumps(results['get1_L_channel_first_nodes'])}")
        log(f"tonecurve_nodes field = {json.dumps(tcn)}")

        # bonus: one nested shape from another module
        nested_demo = None
        for op in ("colorbalancergb", "denoiseprofile", "colorbalance"):
            try:
                g = bridge.call("dev_get_params", {"op": op, "instance": 0}, timeout=15.0)
                gf = g.get("fields", {})
                for fname, val in gf.items():
                    if isinstance(val, list) and val and not (isinstance(val, dict)):
                        nested_demo = {
                            "op": op,
                            "field": fname,
                            "value_head": val[:4],
                            "len": len(val),
                        }
                        break
                if nested_demo:
                    break
            except Exception:  # noqa: BLE001
                continue
        results["nested_shape_demo"] = nested_demo
        log(f"nested shape demo = {json.dumps(nested_demo)}")

        # ---------- CHECK 2a: enable tonecurve to its identity default FIRST ----------
        # Enabling INSERTS the module node into the pixelpipe. Doing that while an
        # in-flight preview render is running can race the worker's lock-free read
        # of module->enabled and leave the session rendering tonecurve as a no-op
        # (observed intermittently -- a T1.4/T1.5 concern, not the curve write,
        # which the readback below proves correct every run). So we enable to the
        # IDENTITY default first, let the pipe settle with the node present, take
        # the baseline there, and then the measured step only changes curve
        # COORDINATES on an already-present node -- a plain reprocess.
        log("=== CHECK 2a: enable tonecurve (identity default) ===")
        en = bridge.call(
            "dev_enable_module", {"op": "tonecurve", "instance": 0, "enabled": True}, timeout=15.0
        )
        results["enable_module"] = en
        log(f"enable_module -> {json.dumps(en)}")
        time.sleep(1.0)

        # ---------- baseline preview: tonecurve ENABLED with identity curve ----------
        log("=== baseline preview (tonecurve ON, identity curve) ===")
        prev_base = bridge.call("dev_preview", {"max_w": 900, "max_h": 900}, timeout=25.0)
        pbase = prev_base.get("path") or prev_base.get("stale_preview")
        mean_before, std_before = luma_stats(host_path(run_dir, pbase)) if pbase else (None, None)
        results["preview_before_json"] = prev_base
        results["luma_std_before"] = std_before
        log(f"BEFORE: mean={mean_before} std={std_before} json={json.dumps(prev_base)}")

        # ---------- CHECK 2b: set gentle S-curve on L ----------
        log("=== CHECK 2b: set S-curve on L ===")
        s_curve = [
            {"x": 0.0, "y": 0.0},
            {"x": 0.25, "y": 0.10},
            {"x": 0.5, "y": 0.5},
            {"x": 0.75, "y": 0.90},
            {"x": 1.0, "y": 1.0},
        ]
        set_curve = bridge.call(
            "dev_set_params",
            {
                "op": "tonecurve",
                "instance": 0,
                "fields": {"tonecurve": [s_curve], "tonecurve_nodes": [5]},
            },
            timeout=20.0,
        )
        results["set_curve"] = set_curve
        log(f"set_params(S-curve) -> {json.dumps(set_curve)}")

        gp2 = bridge.call("dev_get_params", {"op": "tonecurve", "instance": 0}, timeout=15.0)
        f2 = gp2.get("fields", {})
        tc2 = f2.get("tonecurve")
        l2 = tc2[0][:5] if isinstance(tc2, list) and tc2 else None
        results["readback_L_nodes"] = l2
        results["readback_tonecurve_nodes"] = f2.get("tonecurve_nodes")
        log(f"readback L nodes[0:5] = {json.dumps(l2)}")
        log(f"readback tonecurve_nodes = {json.dumps(f2.get('tonecurve_nodes'))}")

        # ---------- CHECK 3b: preview after S-curve std-dev ----------
        # The live backbuf preview (T1.5) can occasionally hand back the previous
        # settled frame if the fresh render hasn't landed yet -- a preview timing
        # race, independent of the curve write (which the readback above already
        # confirms is committed). Poll a few times until the pipe has clearly
        # settled on the new curve (std risen) rather than trusting one grab.
        log("=== CHECK 3: preview after S-curve (settle-poll) ===")
        prev_after = None
        mean_after = std_after = None
        for attempt in range(5):
            prev_after = bridge.call("dev_preview", {"max_w": 900, "max_h": 900}, timeout=25.0)
            pafter = prev_after.get("path") or prev_after.get("stale_preview")
            mean_after, std_after = (
                luma_stats(host_path(run_dir, pafter)) if pafter else (None, None)
            )
            log(
                f"AFTER attempt {attempt}: mean={mean_after} std={std_after}"
                f" status={prev_after.get('status')}"
            )
            if std_before is not None and std_after is not None and (std_after - std_before) > 1.0:
                break
            # preview() only re-renders when the pipe is not already VALID, so a
            # stuck (pre-curve) frame never self-corrects. Re-commit the curve to
            # force a genuine fresh reprocess -- same as a user re-touching the
            # pipe -- then grab again. (Guards against the T1.5 enable/render race.)
            bridge.call(
                "dev_enable_module",
                {"op": "tonecurve", "instance": 0, "enabled": True},
                timeout=15.0,
            )
            bridge.call(
                "dev_set_params",
                {
                    "op": "tonecurve",
                    "instance": 0,
                    "fields": {"tonecurve": [s_curve], "tonecurve_nodes": [5]},
                },
                timeout=20.0,
            )
            time.sleep(0.5)
        results["preview_after_json"] = prev_after
        results["luma_std_after"] = std_after
        log(f"AFTER (final): mean={mean_after} std={std_after} json={json.dumps(prev_after)}")

        # ---------- CHECK 4: over-capacity guard ----------
        log("=== CHECK 4: over-capacity (25 nodes into a 20-slot channel) ===")
        too_many = [{"x": i / 24.0, "y": i / 24.0} for i in range(25)]
        set_over = bridge.call(
            "dev_set_params",
            {
                "op": "tonecurve",
                "instance": 0,
                "fields": {"tonecurve": [too_many], "tonecurve_nodes": [20]},
            },
            timeout=20.0,
        )
        results["set_over_capacity"] = set_over
        log(f"set_params(25 nodes) -> {json.dumps(set_over)}")
        alive_over = container_alive(run_dir)
        results["container_alive_after_overcap"] = alive_over
        # restore the good S-curve so the session stays consistent
        bridge.call(
            "dev_set_params",
            {
                "op": "tonecurve",
                "instance": 0,
                "fields": {"tonecurve": [s_curve], "tonecurve_nodes": [5]},
            },
            timeout=20.0,
        )

        # ---------- CHECK 5: scalar regression (exposure) ----------
        log("=== CHECK 5: scalar regression get/set exposure ===")
        exp_get = bridge.call("dev_get_params", {"op": "exposure", "instance": 0}, timeout=15.0)
        exp_field = exp_get.get("fields", {}).get("exposure")
        results["exposure_get"] = exp_field
        exp_set = bridge.call(
            "dev_set_params",
            {"op": "exposure", "instance": 0, "fields": {"exposure": 0.75}},
            timeout=15.0,
        )
        results["exposure_set"] = exp_set
        exp_get2 = bridge.call("dev_get_params", {"op": "exposure", "instance": 0}, timeout=15.0)
        exp_field2 = exp_get2.get("fields", {}).get("exposure")
        results["exposure_readback"] = exp_field2
        log(
            f"exposure get={json.dumps(exp_field)} set={json.dumps(exp_set)}"
            f" readback={json.dumps(exp_field2)}"
        )

        logtext = log_path.read_text(errors="replace")
        deadlock_hits = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        results["deadlock_log_hits"] = deadlock_hits

        print("\n===== T1.7 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        # ---- evaluate ----
        def curve_is_objects(nodes):
            return (
                isinstance(nodes, list)
                and len(nodes) >= 2
                and all(isinstance(n, dict) and "x" in n and "y" in n for n in nodes)
            )

        over_reported = False
        if isinstance(set_over, dict):
            for c in set_over.get("clamped", []) or []:
                if (
                    isinstance(c, dict)
                    and "tonecurve" in str(c.get("field", ""))
                    and c.get("requested") == 25
                ):
                    over_reported = True

        readback_ok = (
            curve_is_objects(l2)
            and abs((l2[1]["y"] if l2 and len(l2) > 1 else 0) - 0.10) < 1e-4
            and isinstance(results["readback_tonecurve_nodes"], list)
            and results["readback_tonecurve_nodes"][0] == 5
        )

        std_ok = std_before is not None and std_after is not None and (std_after - std_before) > 1.0

        exp_ok = (
            isinstance(exp_set, dict)
            and exp_set.get("ok")
            and isinstance(exp_field2, dict)
            and abs(exp_field2.get("value", 0) - 0.75) < 1e-4
        )

        checks = {
            "0 tonecurve top-level fields resolved": bool(dump.get("top_level")),
            "1 tonecurve NOT a placeholder": results["tonecurve_field_is_placeholder"] is False,
            "1 L-channel is array of {x,y} objects": curve_is_objects(
                results["get1_L_channel_first_nodes"]
            ),
            "1 tonecurve_nodes is a numeric array": isinstance(tcn, list) and len(tcn) == 3,
            "1 nested shape from another module": nested_demo is not None,
            "2 set_curve ok": isinstance(set_curve, dict) and bool(set_curve.get("ok")),
            "2 readback shows new S-curve + count=5": readback_ok,
            "3 luma std-dev rises after S-curve": std_ok,
            "4 over-capacity reported (25->20)": over_reported,
            "4 container alive after over-capacity": alive_over,
            "5 scalar exposure get/set unchanged": exp_ok,
            "no deadlock/critical/assertion in log": len(deadlock_hits) == 0,
        }
        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        delta = (
            std_after - std_before if std_before is not None and std_after is not None else "n/a"
        )
        print(f"\nSTD-DEV BEFORE = {std_before}  AFTER = {std_after}  DELTA = {delta}")
        print(f"ALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== T1.7 INFRA FAILURE: {e} =====", file=sys.stderr)
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
