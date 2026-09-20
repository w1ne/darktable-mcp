"""Draw generic drawn-mask geometry (path/brush polygons, circles) on top of a
captured viewport render -- the generic-shape counterpart to
retouch_overlay.py, which only knows retouch's own circle target/source pairs.

WHY THIS IS DRAWN HERE AND NOT BY DARKTABLE
-------------------------------------------
Same reasoning as retouch_overlay.py's own docstring: darktable's mask
outline/handles are cairo-painted onto the darkroom center-view WIDGET
(dt_masks_events_post_expose, src-dt/src/develop/masks/masks.c:1365) for
EVERY mask type, not just retouch's circles -- so it is absent from the
pixelpipe backbuf capture_viewport/get_preview encode, regardless of which
module the mask belongs to.

COORDINATE FRAME
-----------------
Callers must forward-transform mask-frame geometry (from get_mask_geometry,
i.e. dt.develop.get_mask) into PROCESSED/DISPLAY-frame-normalized coordinates
via dt.develop.transform_point BEFORE building the `shapes` list this module
draws -- see server.py's render_module_mask for the two-stage read pipeline
(the mirror image of add_path_mask_in_viewport's write pipeline). This module
itself only does the second, already-established step: display-frame ->
snapshot render pixels (utils/viewport_coords.py).

SCOPE / KNOWN APPROXIMATIONS (read before trusting mask_only pixel-for-pixel)
------------------------------------------------------------------------------
- Path/brush boundaries are drawn as a STRAIGHT-EDGE polygon through each
  node's `corner`, not darktable's own Catmull-Rom-derived cubic Bezier
  through corner/ctrl1/ctrl2 (src-dt/src/develop/masks/path.c's
  path_points_border, around line 1500). For a smoothed mask this is a
  faceted approximation of the true curve, tightest right at each node and
  loosest at the midpoint of a long curved segment. It is still positioned
  correctly (same nodes, same transform) -- good enough to answer "did my
  mask land on the right subject", not "is this edge pixel-perfect".
- mask_only for path/brush fills the node polygon at a FLAT alpha (the
  shape's own blend-group opacity, no per-node spatial falloff) -- darktable's
  real border/feather is a parallel offset curve with its own quadratic
  falloff (same family as circle.c's, but position-dependent along the
  boundary instead of radial), which this module does not attempt to model.
- Only 'path', 'brush' and 'circle' mask types are drawn. 'ellipse' is
  reported as unsupported (skipped_unsupported) rather than approximated with
  its rotation silently dropped, which would draw a wrong-looking shape with
  no indication it was wrong.
- Each node is classified corner-vs-smooth by comparing `corner` to `ctrl1`/
  `ctrl2` (get_mask_geometry's own convention: equal means corner, differing
  means smooth) -- this comparison is done by the CALLER on the raw mask-frame
  values before any transform (equality is frame-invariant: the same (x,y)
  pair maps to the same transformed point), so this module only receives an
  `is_corner` boolean per node, not the raw ctrl1/ctrl2.
"""

import os
from typing import Any

from ..utils.errors import DarktableMCPError
from ..utils.viewport_coords import (
    image_point_to_snapshot_pixels,
    image_radius_to_snapshot_pixels,
)

MODE_ALL_SHAPES = "all_shapes"
MODE_SELECTED_SHAPE = "selected_shape"
MODE_MASK_ONLY = "mask_only"
MODES = (MODE_ALL_SHAPES, MODE_SELECTED_SHAPE, MODE_MASK_ONLY)

SUPPORTED_TYPES = ("path", "brush", "circle")

# Deliberately not darktable's own mask-manager palette, same reasoning as
# retouch_overlay.py: this is a machine-read render, roles separated by hue.
_C_BOUNDARY = (90, 230, 130)
_C_CORNER_NODE = (255, 255, 255)
_C_SMOOTH_NODE = (120, 200, 255)
_C_DIM = (150, 150, 150)
_C_LABEL = (255, 255, 255)
_C_LABEL_BG = (0, 0, 0)


class MaskOverlayRenderError(DarktableMCPError):
    """Raised when the overlay cannot be drawn at all (missing render file,
    imaging libs unavailable, unknown mode, or nothing drawable)."""

    pass


def _import_libs():
    try:
        import numpy as np
        from PIL import Image, ImageDraw
    except ImportError as e:  # pragma: no cover - deps are non-optional base deps
        raise MaskOverlayRenderError(
            f"render_module_mask needs Pillow + numpy (base dependencies): {e}"
        )
    return np, Image, ImageDraw


def _shape_pixels(
    shape: dict[str, Any],
    region: dict[str, Any],
    render_width: int,
    render_height: int,
) -> dict[str, Any] | None:
    """One shape's DISPLAY-frame geometry (already forward-transformed by the
    caller) -> render pixel geometry, or None when the shape's type/fields
    are not enough to draw (e.g. transform_point failed for one of its
    nodes upstream)."""
    mtype = shape.get("type")
    formid = shape.get("formid")
    opacity = float(shape.get("opacity") if shape.get("opacity") is not None else 1.0)

    if mtype in ("path", "brush"):
        nodes = shape.get("nodes_display") or []
        if len(nodes) < 2:
            return None
        boundary_px: list[tuple[float, float]] = []
        node_px: list[dict[str, Any]] = []
        any_inside = False
        for i, node in enumerate(nodes):
            pt = image_point_to_snapshot_pixels(
                region,
                {"x": node["x"], "y": node["y"]},
                render_width,
                render_height,
                label=f"node[{i}]",
            )
            any_inside = any_inside or bool(pt["inside"])
            boundary_px.append((pt["x"], pt["y"]))
            node_px.append(
                {
                    "x": pt["x"],
                    "y": pt["y"],
                    "is_corner": bool(node.get("is_corner")),
                }
            )
        xs = [p[0] for p in boundary_px]
        ys = [p[1] for p in boundary_px]
        return {
            "formid": formid,
            "type": mtype,
            "name": shape.get("name"),
            "opacity": opacity,
            "boundary_px": boundary_px,
            "nodes_px": node_px,
            "bbox_px": (min(xs), min(ys), max(xs), max(ys)),
            "inside": any_inside,
        }

    if mtype == "circle":
        center = shape.get("center_display")
        radius = shape.get("radius_display")
        if not isinstance(center, dict) or radius is None:
            return None
        c = image_point_to_snapshot_pixels(
            region,
            center,
            render_width,
            render_height,
            label="center",
        )
        r_px = image_radius_to_snapshot_pixels(region, float(radius), render_width)
        return {
            "formid": formid,
            "type": "circle",
            "name": shape.get("name"),
            "opacity": opacity,
            "center_px": (c["x"], c["y"]),
            "radius_px": r_px,
            "bbox_px": (
                c["x"] - r_px,
                c["y"] - r_px,
                c["x"] + r_px,
                c["y"] + r_px,
            ),
            "inside": bool(c["inside"]),
        }

    return None


def _alpha(colour, a: int):
    return (colour[0], colour[1], colour[2], a)


def _label(draw, at: tuple[float, float], text: str, colour=_C_LABEL):
    x, y = at
    try:
        box = draw.textbbox((x, y), text)
        draw.rectangle([box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1], fill=_C_LABEL_BG + (190,))
    except Exception:  # pragma: no cover - very old Pillow without textbbox
        pass
    draw.text((x, y), text, fill=colour)


def _node_marker(draw, at: tuple[float, float], is_corner: bool, colour_a: int, size: float = 3.5):
    x, y = at
    if is_corner:
        colour = _alpha(_C_CORNER_NODE, colour_a)
        draw.rectangle([x - size, y - size, x + size, y + size], outline=colour, width=1)
    else:
        colour = _alpha(_C_SMOOTH_NODE, colour_a)
        draw.ellipse([x - size, y - size, x + size, y + size], outline=colour, width=1)


def _draw_shape(draw, geo: dict[str, Any], *, dim: bool, label: bool):
    a = 110 if dim else 255
    boundary_colour = _alpha(_C_DIM if dim else _C_BOUNDARY, a)

    if geo["type"] in ("path", "brush"):
        pts = geo["boundary_px"]
        closed = pts + [pts[0]]
        draw.line(closed, fill=boundary_colour, width=1 if dim else 2)
        if not dim:
            for node in geo["nodes_px"]:
                _node_marker(draw, (node["x"], node["y"]), node["is_corner"], a)
        if label and not dim:
            first = pts[0]
            _label(draw, (first[0] + 6, first[1] - 12), f"#{geo['formid']}")
    elif geo["type"] == "circle":
        cx, cy = geo["center_px"]
        r = geo["radius_px"]
        if r > 0:
            draw.ellipse(
                [cx - r, cy - r, cx + r, cy + r], outline=boundary_colour, width=1 if dim else 2
            )
        if label and not dim:
            _label(draw, (cx + r + 4, cy - r - 4), f"#{geo['formid']}")


def _mask_alpha(np, geos: list[dict[str, Any]], width: int, height: int):
    """Composite every shape into one 0..1 alpha plane, combined via max()
    (matches how overlapping darktable masks read: a pixel is masked as
    strongly as the strongest shape covering it). See this module's
    docstring for the flat-fill (no border falloff) approximation used for
    path/brush."""
    from PIL import Image, ImageDraw

    out = np.zeros((height, width), dtype=np.float32)
    for geo in geos:
        layer = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(layer)
        if geo["type"] in ("path", "brush"):
            draw.polygon(geo["boundary_px"], fill=255)
        elif geo["type"] == "circle":
            cx, cy = geo["center_px"]
            r = geo["radius_px"]
            if r > 0:
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
        arr = (np.asarray(layer, dtype=np.float32) / 255.0) * float(geo["opacity"])
        out = np.maximum(out, arr)
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

    `shapes` entries must already carry DISPLAY-frame geometry (see this
    module's docstring / server.py's render_module_mask for the forward
    transform). Returns {"path", "mode", "highlight_formid", "render",
    "drawn": [formid...], "offscreen": [formid...], "geometry": [...]}.
    """
    np, image_module, image_draw = _import_libs()

    if mode not in MODES:
        raise MaskOverlayRenderError(f"mode must be one of {MODES}, got: {mode!r}")
    if not os.path.isfile(render_path):
        raise MaskOverlayRenderError(f"captured render is gone: {render_path}")

    geos: list[dict[str, Any]] = []
    for shape in shapes or []:
        geo = _shape_pixels(shape, region, render_width, render_height)
        if geo is not None:
            geos.append(geo)

    if not geos:
        raise MaskOverlayRenderError("no shapes with usable display-frame geometry to draw")

    if mode == MODE_SELECTED_SHAPE:
        if highlight_formid is None and len(geos) == 1:
            highlight_formid = geos[0]["formid"]
        if highlight_formid is None:
            raise MaskOverlayRenderError(
                f"mode '{mode}' needs highlight_formid (the module has "
                f"{len(geos)} shapes; pass the formid to inspect)"
            )
        if not any(g["formid"] == highlight_formid for g in geos):
            raise MaskOverlayRenderError(
                f"highlight_formid {highlight_formid} is not among the drawable "
                f"shapes ({[g['formid'] for g in geos]})"
            )

    base = image_module.open(render_path).convert("RGBA")
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
                    label=label_shapes,
                )
            elif mode == MODE_SELECTED_SHAPE:
                _draw_shape(draw, geo, dim=(geo["formid"] != highlight_formid), label=label_shapes)
        image_module.alpha_composite(base, layer).convert("RGB").save(out_path, "PNG")

    return {
        "path": out_path,
        "mode": mode,
        "highlight_formid": highlight_formid,
        "render": {"width": render_width, "height": render_height},
        "drawn": [g["formid"] for g in geos],
        "offscreen": [g["formid"] for g in geos if not g["inside"]],
        "geometry": [
            {
                "formid": g["formid"],
                "type": g["type"],
                "name": g.get("name"),
                "opacity": g["opacity"],
                "bbox_px": [round(v, 1) for v in g["bbox_px"]],
                "node_count": len(g["nodes_px"]) if "nodes_px" in g else None,
                "corner_node_count": (
                    sum(1 for n in g["nodes_px"] if n["is_corner"]) if "nodes_px" in g else None
                ),
            }
            for g in geos
        ],
    }
