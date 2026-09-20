#!/usr/bin/env python3
"""T2.4 acceptance driver: get_viewport() now returns a drop-in `region`.

Problem being fixed: get_viewport() exposed raw zoom_x/zoom_y that are
CENTER-relative and can be NEGATIVE, so they did NOT compose with get_preview's
`region` arg (top-left, must be >=0). This driver proves that each viewport now
also carries a ready-to-use `region` {x,y,w,h} (top-left, normalized 0..1,
CLAMPED to [0,1]) that passes STRAIGHT INTO get_preview with no caller math.

Runs over the same spike bridge as run_f1.py / run_t2_2.py
(docker/run-dt-bridge-spike.sh + spike_methods.lua). Does NOT touch the clean
bridge.

Acceptance (all quoted in the final report):
  1. At "fit" zoom (default on open): get_viewport().main.region ~= {0,0,1,1}.
  2. Drop-in compose: take get_viewport().main.region and pass it VERBATIM as
     get_preview's region ({x,y,w,h}) -- must be accepted (no >=0 / range error)
     and return a valid crop. Both region and get_preview status quoted.
  3. region is always clamped: x>=0, y>=0, x+w<=1, y+h<=1 (assert).
  4. Best-effort: force a non-fit zoom via the darkroom "zoom close-up" action
     (DT_ZOOM_1 + closeup). If it lands, show region becomes a proper
     sub-rectangle (w<1 or h<1), still clamps, and STILL composes into
     get_preview. If headless can't change zoom, say so and rely on fit + math.
  5. preview2 inactive (no 2nd window headless): preview2.active==false and NO
     region key -- no crash.

Usage: python3 darktable-mcp/spike/run_t2_4.py
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

# rounding tolerance for the "fit == {0,0,1,1}" check
FIT_TOL = 0.02
# candidate action paths for "zoom close-up" (DT_ZOOM_1 + closeup flip). Headless
# action paths vary by build; try a few and keep whichever changes the zoom.
ZOOM_ACTION_PATHS = [
    "views/darkroom/zoom close-up",
    "darkroom/zoom close-up",
    "zoom close-up",
    "views/darkroom/zoom in",
    "darkroom/zoom in",
]


def _load_bridge_client_module():
    client_path = MCP_DIR / "darktable_mcp" / "bridge" / "client.py"
    spec = importlib.util.spec_from_file_location("darktable_mcp_bridge_client", client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_client_mod = _load_bridge_client_module()
Bridge = _client_mod.Bridge


def log(msg: str) -> None:
    print(f"[t2.4] {msg}", flush=True)


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


def region_is_clamped(reg: dict, tol: float = 1e-6):
    """True iff x>=0, y>=0, x+w<=1, y+h<=1 (within a tiny fp tolerance)."""
    if not isinstance(reg, dict):
        return False
    try:
        x, y, w, h = float(reg["x"]), float(reg["y"]), float(reg["w"]), float(reg["h"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        x >= -tol
        and y >= -tol
        and w > 0.0
        and h > 0.0
        and x + w <= 1.0 + tol
        and y + h <= 1.0 + tol
    )


def region_near_full(reg: dict, tol: float = FIT_TOL) -> bool:
    if not isinstance(reg, dict):
        return False
    return (
        abs(reg.get("x", 9) - 0.0) <= tol
        and abs(reg.get("y", 9) - 0.0) <= tol
        and abs(reg.get("w", 9) - 1.0) <= tol
        and abs(reg.get("h", 9) - 1.0) <= tol
    )


def preview_with_region(bridge, run_dir, reg: dict, tag: str):
    """Pass a viewport region VERBATIM as get_preview's {x,y,w,h}."""
    resp = bridge.call(
        "dev_preview",
        {"max_w": 320, "max_h": 320, "x": reg["x"], "y": reg["y"], "w": reg["w"], "h": reg["h"]},
        timeout=20.0,
    )
    p = resp.get("path") or resp.get("stale_preview")
    hp = host_path(run_dir, p) if p else None
    exists = bool(hp and Path(hp).is_file())
    log(
        f"  compose[{tag}]: region={json.dumps(reg)} -> status={resp.get('status')} "
        f"error={resp.get('error')} path_exists={exists}"
    )
    return resp, exists


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

        # let the pipe settle so processed_width/height (needed for region) exist
        bridge.call("dev_preview", {"max_w": 200, "max_h": 200}, timeout=20.0)

        # ============ CHECK 1: fit-zoom region ~= {0,0,1,1} ============
        log("=== CHECK 1: get_viewport() at fit zoom ===")
        vp = bridge.call("dev_get_viewport", {}, timeout=15.0)
        results["viewport_fit"] = vp
        log(f"get_viewport (fit) -> {json.dumps(vp)}")
        main_vp = vp.get("main") if isinstance(vp, dict) else None
        fit_region = main_vp.get("region") if isinstance(main_vp, dict) else None
        results["fit_region"] = fit_region
        log(
            f"main.region @ fit = {json.dumps(fit_region)}  (zoom_label="
            f"{main_vp.get('zoom_label') if main_vp else '?'})"
        )

        # ============ CHECK 3 (fit): clamp ============
        fit_clamped = region_is_clamped(fit_region)
        results["fit_region_clamped"] = fit_clamped

        # ============ CHECK 2: verbatim compose fit region -> get_preview ======
        log("=== CHECK 2: compose main.region -> get_preview (verbatim) ===")
        compose_resp, compose_ok = (None, False)
        if isinstance(fit_region, dict):
            compose_resp, compose_ok = preview_with_region(bridge, run_dir, fit_region, "fit")
        results["compose_fit_response"] = compose_resp
        results["compose_fit_ok"] = compose_ok
        compose_fit_no_error = bool(
            isinstance(compose_resp, dict)
            and not compose_resp.get("error")
            and compose_resp.get("status") in ("ok", "processing")
        )

        # ============ CHECK 5: preview2 inactive, no region, no crash ==========
        p2 = vp.get("preview2") if isinstance(vp, dict) else None
        results["preview2"] = p2
        p2_inactive_no_region = bool(
            isinstance(p2, dict) and p2.get("active") is False and "region" not in p2
        )
        results["preview2_inactive_no_region"] = p2_inactive_no_region
        log(f"preview2 = {json.dumps(p2)} (inactive_no_region={p2_inactive_no_region})")

        # ============ CHECK 4: best-effort non-fit zoom -> sub-rectangle =======
        log("=== CHECK 4: best-effort force non-fit zoom (zoom close-up / in) ===")
        zoom_changed = False
        zoom_action_used = None
        for ap in ZOOM_ACTION_PATHS:
            try:
                r = bridge.call("nudge", {"action_path": ap}, timeout=15.0)
            except Exception as e:  # noqa: BLE001
                log(f"  nudge {ap!r} raised: {e}")
                continue
            is_nan = bool(isinstance(r, dict) and r.get("is_nan"))
            log(f"  nudge {ap!r} -> {json.dumps(r)} (is_nan={is_nan})")
            if is_nan:
                continue  # action path not resolved on this build
            # re-read viewport and see if it left fit
            bridge.call("dev_preview", {"max_w": 200, "max_h": 200}, timeout=20.0)
            vp2 = bridge.call("dev_get_viewport", {}, timeout=15.0)
            mv2 = vp2.get("main") if isinstance(vp2, dict) else None
            lbl = mv2.get("zoom_label") if isinstance(mv2, dict) else None
            reg2 = mv2.get("region") if isinstance(mv2, dict) else None
            log(f"  after {ap!r}: zoom_label={lbl} region={json.dumps(reg2)}")
            if lbl not in (None, "fit"):
                zoom_changed = True
                zoom_action_used = ap
                results["viewport_nonfit"] = vp2
                results["nonfit_region"] = reg2
                break
        results["zoom_changed"] = zoom_changed
        results["zoom_action_used"] = zoom_action_used

        nonfit_is_subrect = False
        nonfit_clamped = False
        nonfit_compose_ok = False
        nonfit_compose_no_error = False
        if zoom_changed:
            reg2 = results.get("nonfit_region")
            nonfit_clamped = region_is_clamped(reg2)
            nonfit_is_subrect = bool(
                isinstance(reg2, dict)
                and (reg2.get("w", 1) < 1.0 - 1e-6 or reg2.get("h", 1) < 1.0 - 1e-6)
            )
            if isinstance(reg2, dict):
                cr, nonfit_compose_ok = preview_with_region(bridge, run_dir, reg2, "nonfit")
                results["compose_nonfit_response"] = cr
                nonfit_compose_no_error = bool(
                    isinstance(cr, dict)
                    and not cr.get("error")
                    and cr.get("status") in ("ok", "processing")
                )
        else:
            log(
                "  headless: could not change zoom off 'fit' via action system; "
                "relying on fit-zoom + math review for the sub-rectangle claim."
            )
        results["nonfit_is_subrect"] = nonfit_is_subrect
        results["nonfit_clamped"] = nonfit_clamped
        results["nonfit_compose_no_error"] = nonfit_compose_no_error

        results["container_alive"] = container_alive(run_dir)

        logtext = log_path.read_text(errors="replace")
        deadlock_hits = [
            ln.strip()
            for ln in logtext.splitlines()
            if any(m in ln.lower() for m in DEADLOCK_MARKERS)
        ]
        results["deadlock_log_hits"] = deadlock_hits

        print("\n===== T2.4 RESULTS =====")
        print(json.dumps(results, indent=2, default=str))

        # ---- evaluate ----
        checks = {
            "1 fit main.region ~= {0,0,1,1}": region_near_full(fit_region),
            "2 fit region composes into get_preview (no error, valid status)": compose_fit_no_error,
            "2 get_preview returned a crop path": compose_ok,
            "3 fit region clamped (x>=0,y>=0,x+w<=1,y+h<=1)": fit_clamped,
            "5 preview2 inactive: active=false AND no region key": p2_inactive_no_region,
            "container alive": results["container_alive"],
            "no deadlock/critical/assertion in log": len(deadlock_hits) == 0,
        }
        # CHECK 4 is best-effort: only enforce its sub-checks if zoom actually changed.
        if zoom_changed:
            checks["4 non-fit region is a sub-rectangle (w<1 or h<1)"] = nonfit_is_subrect
            checks["4 non-fit region still clamped"] = nonfit_clamped
            checks["4 non-fit region composes into get_preview"] = nonfit_compose_no_error
        else:
            log(
                "NOTE: CHECK 4 skipped (zoom not settable headless) -- not counted "
                "as a failure; correctness of the sub-rectangle path rests on the "
                "fit result + source-level derivation."
            )

        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print(f"\nZOOM CHANGED HEADLESS: {zoom_changed} (action={zoom_action_used})")
        print(f"ALL CHECKS PASSED: {allok}")
        return 0 if allok else 2
    except Exception as e:  # noqa: BLE001
        print(f"\n===== T2.4 INFRA FAILURE: {e} =====", file=sys.stderr)
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
