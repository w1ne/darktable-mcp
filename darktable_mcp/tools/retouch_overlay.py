"""Draw retouch shape geometry on top of a captured viewport render.

WHY THIS IS DRAWN HERE AND NOT BY DARKTABLE
-------------------------------------------
The circles, the target-source connector line and the drag handles visible in
darkroom are painted with cairo onto the center-view WIDGET
(dt_masks_events_post_expose, src-dt/src/develop/masks/masks.c:1365, called
from src-dt/src/views/darkroom.c:1036). They are not part of any pixelpipe
buffer. capture_viewport/get_preview encode the PREVIEW pipe's backbuf, so no
amount of selecting a shape or toggling darktable's own "display mask" button
can make an overlay appear in what we capture -- and retouch's mask display is
additionally gated on the FULL pipe holding module focus
(src-dt/src/iop/retouch.c:3906), a different pipe than the one we read.

So the overlay is synthesized here from the geometry retouch_list_shapes
already reports. That is strictly better for a scripted caller: deterministic,
works with no GUI focus, mutates no darktable state (no selection, no toggles
left behind for the human user to undo), and it can show things darktable's own
overlay does not -- formid labels and which shapes overlap.

COORDINATE FRAME
----------------
Input geometry must be DISPLAY-frame normalized (retouch_list_shapes'
target_display/source_display/radius_display/feather_display fields, produced by
dt.develop.transform_point). The mask-frame storage values are a different frame
and plotting those directly would put every circle in the wrong place on any
portrait or cropped image -- the same frame confusion that put a heal point on
the chest instead of the neck in the 2026-07-25 bugreport, just in the read
direction.

MASK FALLOFF
------------
mask_only reproduces circle.c's own formula exactly
(src-dt/src/develop/masks/circle.c:1142-1157): with total = radius + border,
alpha = clip((total^2 - dist^2) / (total^2 - radius^2))^2, i.e. a quadratic
falloff between the radius and the outer edge of the feather, then scaled by the
shape's opacity. A zero feather degrades to a hard-edged disc (border2 == 0
would divide by zero).
"""

import math
import os
from typing import Any

from ..utils.errors import DarktableMCPError
from ..utils.viewport_coords import (
    image_point_to_snapshot_pixels,
    image_radius_to_snapshot_pixels,
)

MODE_ALL_SHAPES = "all_shapes"
MODE_SELECTED_SHAPE = "selected_shape"
MODE_SOURCE_AND_TARGET = "source_and_target"
MODE_MASK_ONLY = "mask_only"
MODES = (MODE_ALL_SHAPES, MODE_SELECTED_SHAPE, MODE_SOURCE_AND_TARGET, MODE_MASK_ONLY)

# Deliberately not darktable's own overlay palette: this is a machine-read
# render, so the target/source/feather roles are separated by hue as strongly as
# possible rather than by the subtle brightness differences the GUI uses.
_C_TARGET = (255, 64, 96)
_C_TARGET_FEATHER = (255, 150, 170)
_C_SOURCE = (64, 200, 255)
_C_LINK = (255, 214, 64)
_C_DIM = (150, 150, 150)
_C_LABEL = (255, 255, 255)
_C_LABEL_BG = (0, 0, 0)


class OverlayRenderError(DarktableMCPError):
    """Raised when the overlay cannot be drawn at all (missing render file,
    imaging libs unavailable, unknown mode)."""

    pass


def _import_libs():
    try:
        import numpy as np
        from PIL import Image, ImageDraw
    except ImportError as e:  # pragma: no cover - deps are non-optional base deps
        raise OverlayRenderError(
            f"retouch_render_overlay needs Pillow + numpy (base dependencies): {e}"
        )
    return np, Image, ImageDraw


def _shape_pixels(
    shape: dict[str, Any],
    region: dict[str, Any],
    render_width: int,
    render_height: int,
) -> dict[str, Any] | None:
    """One shape's display-frame geometry -> render pixel geometry, or None when
    the shape carries no display-frame fields (no processed pipe when it was
    listed, or a non-circle GUI-made shape)."""
    target = shape.get("target_display")
    if not isinstance(target, dict):
        return None
    radius = shape.get("radius_display")
    if radius is None:
        return None

    t = image_point_to_snapshot_pixels(region, target, render_width, render_height, "target")
    r_px = image_radius_to_snapshot_pixels(region, radius, render_width)
    feather = shape.get("feather_display") or 0.0
    f_px = image_radius_to_snapshot_pixels(region, feather, render_width) if feather else 0.0

    out: dict[str, Any] = {
        "formid": shape.get("formid"),
        "algorithm": shape.get("algorithm"),
        "opacity": float(shape.get("opacity") or 1.0),
        "target_px": (t["x"], t["y"]),
        "radius_px": r_px,
        "feather_px": f_px,
        "inside": bool(t["inside"]),
    }

    source = shape.get("source_display")
    if isinstance(source, dict):
        s = image_point_to_snapshot_pixels(region, source, render_width, render_height, "source")
        out["source_px"] = (s["x"], s["y"])
        out["source_inside"] = bool(s["inside"])
    return out


def _circle(draw, center: tuple[float, float], radius: float, colour, width: int = 2):
    cx, cy = center
    if radius <= 0:
        return
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=colour, width=width)


def _dashed_circle(
    draw, center: tuple[float, float], radius: float, colour, width: int = 1, segments: int = 32
):
    """Dashed ring for the feather boundary -- a solid second ring reads as a
    second shape at a glance."""
    if radius <= 0:
        return
    cx, cy = center
    step = 360.0 / segments
    for i in range(0, segments, 2):
        draw.arc(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            start=i * step,
            end=(i + 1) * step,
            fill=colour,
            width=width,
        )


def _crosshair(draw, center: tuple[float, float], colour, size: float = 4.0, width: int = 1):
    cx, cy = center
    draw.line([cx - size, cy, cx + size, cy], fill=colour, width=width)
    draw.line([cx, cy - size, cx, cy + size], fill=colour, width=width)


def _label(draw, at: tuple[float, float], text: str, colour=_C_LABEL):
    """Small text tag with a filled backing box so it stays readable over any
    photo content (no font file is bundled; PIL's default bitmap font is used)."""
    x, y = at
    try:
        box = draw.textbbox((x, y), text)
        draw.rectangle([box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1], fill=_C_LABEL_BG + (190,))
    except Exception:  # pragma: no cover - very old Pillow without textbbox
        pass
    draw.text((x, y), text, fill=colour)


def _alpha(colour, a: int):
    return (colour[0], colour[1], colour[2], a)


def _draw_shape(draw, geo: dict[str, Any], *, dim: bool, with_source: bool, label: bool):
    target = geo["target_px"]
    r = geo["radius_px"]
    f = geo["feather_px"]
    a = 110 if dim else 255
    t_col = _alpha(_C_DIM if dim else _C_TARGET, a)

    _circle(draw, target, r, t_col, width=1 if dim else 2)
    if f > 0:
        _dashed_circle(
            draw, target, r + f, _alpha(_C_DIM if dim else _C_TARGET_FEATHER, a), width=1
        )
    _crosshair(draw, target, t_col)

    source = geo.get("source_px")
    if with_source and source:
        s_col = _alpha(_C_DIM if dim else _C_SOURCE, a)
        _circle(draw, source, r, s_col, width=1 if dim else 2)
        _crosshair(draw, source, s_col)
        draw.line(
            [source[0], source[1], target[0], target[1]],
            fill=_alpha(_C_DIM if dim else _C_LINK, a),
            width=1,
        )

    if label and not dim:
        _label(draw, (target[0] + r + 4, target[1] - r - 4), f"#{geo['formid']}")


def _mask_alpha(np, geos: list[dict[str, Any]], width: int, height: int):
    """Composite every shape's circle.c falloff into one 0..1 alpha plane.
    Shapes combine by max(), matching how overlapping retouch masks read in
    the GUI (a pixel is masked as strongly as the strongest shape covering
    it)."""
    ys, xs = np.mgrid[0:height, 0:width]
    out = np.zeros((height, width), dtype=np.float32)
    for geo in geos:
        cx, cy = geo["target_px"]
        r = float(geo["radius_px"])
        f = float(geo["feather_px"])
        if r <= 0:
            continue
        d2 = (xs - cx) ** 2 + (ys - cy) ** 2
        total = r + f
        if f > 0:
            border2 = total * total - r * r
            ratio = (total * total - d2) / border2
            alpha = np.clip(ratio, 0.0, 1.0) ** 2
        else:
            alpha = (d2 <= r * r).astype(np.float32)
        out = np.maximum(out, alpha.astype(np.float32) * float(geo["opacity"]))
    return out


def render_overlay(
    render_path: str,
    out_path: str,
    shapes: list[dict[str, Any]],
    region: dict[str, Any],
    render_width: int,
    render_height: int,
    mode: str = MODE_ALL_SHAPES,
    highlight_formid: int | None = None,
    label_shapes: bool = True,
) -> dict[str, Any]:
    """Write an overlay PNG next to a captured render and return a summary.

    Returns {"path", "mode", "drawn": [formid...], "skipped_no_geometry":
    [formid...], "offscreen": [formid...], "geometry": [...]}. `offscreen`
    shapes are still drawn (partially visible circles are useful) -- they are
    listed so the caller knows why a listed shape may be invisible.
    """
    np, image_module, image_draw = _import_libs()

    if mode not in MODES:
        raise OverlayRenderError(f"mode must be one of {MODES}, got: {mode!r}")
    if not os.path.isfile(render_path):
        raise OverlayRenderError(f"captured render is gone: {render_path}")

    geos: list[dict[str, Any]] = []
    skipped: list[Any] = []
    for shape in shapes or []:
        geo = _shape_pixels(shape, region, render_width, render_height)
        if geo is None:
            skipped.append(shape.get("formid"))
        else:
            geos.append(geo)

    if mode == MODE_SELECTED_SHAPE or mode == MODE_SOURCE_AND_TARGET:
        if highlight_formid is None and len(geos) == 1:
            highlight_formid = geos[0]["formid"]
        if highlight_formid is None:
            raise OverlayRenderError(
                f"mode '{mode}' needs highlight_formid (the module has "
                f"{len(geos)} shapes; pass the formid to inspect)"
            )
        if not any(g["formid"] == highlight_formid for g in geos):
            raise OverlayRenderError(
                f"highlight_formid {highlight_formid} is not among the listed "
                "shapes with display-frame geometry "
                f"({[g['formid'] for g in geos]})"
            )

    base = image_module.open(render_path).convert("RGBA")
    # The render on disk is the authority on size; a mismatch with the
    # snapshot's recorded size would silently shift every circle.
    if base.size != (render_width, render_height):
        render_width, render_height = base.size

    if mode == MODE_MASK_ONLY:
        alpha = _mask_alpha(np, geos, render_width, render_height)
        img = image_module.fromarray((alpha * 255.0 + 0.5).astype("uint8"), mode="L")
        img.save(out_path, "PNG")
    else:
        layer = image_module.new("RGBA", (render_width, render_height), (0, 0, 0, 0))
        draw = image_draw.Draw(layer)
        for geo in geos:
            if mode == MODE_ALL_SHAPES:
                _draw_shape(
                    draw,
                    geo,
                    dim=(highlight_formid is not None and geo["formid"] != highlight_formid),
                    with_source=True,
                    label=label_shapes,
                )
            elif mode == MODE_SELECTED_SHAPE:
                _draw_shape(
                    draw,
                    geo,
                    dim=(geo["formid"] != highlight_formid),
                    with_source=(geo["formid"] == highlight_formid),
                    label=label_shapes,
                )
            elif mode == MODE_SOURCE_AND_TARGET:
                if geo["formid"] != highlight_formid:
                    continue
                _draw_shape(draw, geo, dim=False, with_source=True, label=label_shapes)
        image_module.alpha_composite(base, layer).convert("RGB").save(out_path, "PNG")

    return {
        "path": out_path,
        "mode": mode,
        "highlight_formid": highlight_formid,
        "render": {"width": render_width, "height": render_height},
        "drawn": [g["formid"] for g in geos],
        "skipped_no_geometry": skipped,
        "offscreen": [
            g["formid"]
            for g in geos
            if not g["inside"] or (("source_px" in g) and not g.get("source_inside", True))
        ],
        "geometry": [
            {
                "formid": g["formid"],
                "algorithm": g["algorithm"],
                "opacity": g["opacity"],
                "target_px": [round(g["target_px"][0], 1), round(g["target_px"][1], 1)],
                "source_px": (
                    [round(g["source_px"][0], 1), round(g["source_px"][1], 1)]
                    if "source_px" in g
                    else None
                ),
                "radius_px": round(g["radius_px"], 1),
                "feather_px": round(g["feather_px"], 1),
                "source_target_distance_px": (
                    round(
                        math.hypot(
                            g["source_px"][0] - g["target_px"][0],
                            g["source_px"][1] - g["target_px"][1],
                        ),
                        1,
                    )
                    if "source_px" in g
                    else None
                ),
            }
            for g in geos
        ],
    }


def find_overlaps(geometry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairs of shapes whose target discs intersect (feather included). Heal
    shapes stacked on each other are a real retouch mistake -- the second heal
    samples the first one's already-healed output -- and it is much easier to
    report the pair than to expect it to be spotted in the render."""
    out: list[dict[str, Any]] = []
    for i, a in enumerate(geometry):
        for b in geometry[i + 1 :]:
            d = math.hypot(
                a["target_px"][0] - b["target_px"][0], a["target_px"][1] - b["target_px"][1]
            )
            reach = (a["radius_px"] + a["feather_px"]) + (b["radius_px"] + b["feather_px"])
            if d < reach:
                out.append(
                    {
                        "formids": [a["formid"], b["formid"]],
                        "distance_px": round(d, 1),
                        "combined_reach_px": round(reach, 1),
                    }
                )
    return out
