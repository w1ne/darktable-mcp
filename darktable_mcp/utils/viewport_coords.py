"""Viewport-local -> PROCESSED/DISPLAY-frame-normalized coordinate transform.

Shared by capture_viewport/retouch_add_shape_in_viewport/
retouch_update_shape_in_viewport (server.py) so the affine remap lives in one
place instead of drifting across three call sites. The math mirrors
_push_viewport's own region derivation (src-dt/src/lua/develop.c) applied to
a point instead of a rectangle:

    display_x = region.x + local_x * region.w
    display_y = region.y + local_y * region.h

IMPORTANT -- this module produces PROCESSED/DISPLAY-frame coordinates (the
same frame get_viewport()/get_preview() use), which is a DIFFERENT frame
than darktable's masks (circle/path/brush/etc, and therefore
retouch_add_shape/add_path_mask) actually store points in -- masks are
normalized against the PIPE-INPUT frame, and the two frames diverge whenever
orientation/crop/rotate/lens-correction is active (confirmed via a real
bugreport: a neck target ended up on the chest, even with the full,
uncropped frame). Callers MUST additionally run this module's output through
`dt.develop.backtransform_point` (src-dt/src/lua/develop.c, bridged as
dev_backtransform_point) before handing coordinates to retouch_add_shape/
retouch_update_shape/add_path_mask -- see server.py's
_handle_retouch_add_shape_in_viewport for the full two-stage pipeline.

Radius is a LENGTH, not a position: it is scaled by region.w (the viewport's
width-fraction of the DISPLAY frame) ONLY -- never by height, never by an
average of the two. Using height or an average would make a circle's
rendered size depend on the external window's aspect ratio, which is wrong
(flagged explicitly during design review). The SECOND stage
(backtransform_point) re-normalizes this display-frame length against
mindim(iwidth, iheight) to match circle.c's actual mask-space convention --
this module's radius output is only an intermediate value, not the final one.

Every function here REJECTS out-of-bounds input with ViewportCoordinateError
instead of clamping -- clamping a wrong point to the edge of the viewport
would silently retouch the wrong spot, which is exactly the failure mode
this whole feature exists to prevent.
"""

from typing import Any

from .errors import DarktableMCPError

VIEWPORT_NORMALIZED = "viewport_normalized"
VIEWPORT_PIXELS = "viewport_pixels"
# Preferred, unambiguous names. "viewport_pixels" was read by a caller as
# "pixels of the darktable window" (1135x784 in the 2026-07-26 bugreport)
# when it has ALWAYS meant "pixels of the PNG capture_viewport returned"
# (471x326 there) -- two different pixel grids, and picking the wrong one
# silently retouches the wrong spot. The snapshot_* names say which grid it
# is; the viewport_* names stay as permanent aliases.
SNAPSHOT_NORMALIZED = "snapshot_normalized"
SNAPSHOT_PIXELS = "snapshot_pixels"
_ALIASES = {
    SNAPSHOT_NORMALIZED: VIEWPORT_NORMALIZED,
    SNAPSHOT_PIXELS: VIEWPORT_PIXELS,
}
POINT_SPACES = (
    SNAPSHOT_NORMALIZED,
    SNAPSHOT_PIXELS,
    VIEWPORT_NORMALIZED,
    VIEWPORT_PIXELS,
)


def canonical_space(space: Any) -> str:
    """Map a caller-supplied coordinate_space onto its canonical name, so the
    snapshot_* and viewport_* spellings behave identically everywhere."""
    return _ALIASES.get(space, space)


class ViewportCoordinateError(DarktableMCPError):
    """Raised when a viewport-relative coordinate is malformed, falls outside
    the captured region/render, or the region itself is degenerate. Always
    surfaced to the caller as an explicit error -- never silently clamped."""

    pass


def _require_region(region: dict[str, Any]) -> dict[str, float]:
    if not isinstance(region, dict) or not all(k in region for k in ("x", "y", "w", "h")):
        raise ViewportCoordinateError("region must be a {x,y,w,h} dict")
    try:
        rx, ry, rw, rh = (float(region[k]) for k in ("x", "y", "w", "h"))
    except (TypeError, ValueError) as e:
        raise ViewportCoordinateError(f"region values must be numeric: {e}")
    if rw <= 0 or rh <= 0:
        raise ViewportCoordinateError(f"region has non-positive width/height: {region}")
    return {"x": rx, "y": ry, "w": rw, "h": rh}


def _local_normalized(
    point: dict[str, Any],
    space: str,
    render_width: int | None,
    render_height: int | None,
    label: str,
) -> dict[str, float]:
    """Point in viewport-local space -> normalized 0..1 within that viewport,
    rejecting anything outside the render/frame instead of clamping it."""
    if not isinstance(point, dict) or "x" not in point or "y" not in point:
        raise ViewportCoordinateError(f"{label} {{x,y}} is required")
    try:
        x = float(point["x"])
        y = float(point["y"])
    except (TypeError, ValueError) as e:
        raise ViewportCoordinateError(f"{label} values must be numeric: {e}")

    space = canonical_space(space)
    if space == VIEWPORT_NORMALIZED:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ViewportCoordinateError(
                f"{label} ({x}, {y}) is outside the captured viewport "
                "(viewport_normalized must be within [0,1])"
            )
        return {"x": x, "y": y}

    if space == VIEWPORT_PIXELS:
        if not render_width or not render_height:
            raise ViewportCoordinateError(
                "viewport_pixels requires the snapshot's render width/height"
            )
        if not (0 <= x <= render_width and 0 <= y <= render_height):
            raise ViewportCoordinateError(
                f"{label} ({x}, {y})px is outside the captured render "
                f"({render_width}x{render_height}px)"
            )
        return {"x": x / render_width, "y": y / render_height}

    raise ViewportCoordinateError(f"coordinate_space must be one of {POINT_SPACES}, got: {space!r}")


def viewport_point_to_image(
    region: dict[str, Any],
    point: dict[str, Any],
    space: str,
    render_width: int | None = None,
    render_height: int | None = None,
    label: str = "point",
) -> dict[str, float]:
    """Viewport-local point (normalized or pixel) -> full-image-normalized
    point. Raises ViewportCoordinateError if the point falls outside the
    captured viewport/render -- never clamps."""
    r = _require_region(region)
    local = _local_normalized(point, space, render_width, render_height, label)
    return {
        "x": r["x"] + local["x"] * r["w"],
        "y": r["y"] + local["y"] * r["h"],
    }


def viewport_radius_to_image(
    region: dict[str, Any],
    radius: float,
    space: str,
    render_width: int | None = None,
) -> float:
    """Viewport-local radius (normalized or pixel) -> full-image-normalized
    radius. Scaled by region.w ONLY (viewport width-fraction of the full
    image) -- a length must not depend on the viewport's aspect ratio."""
    r = _require_region(region)
    try:
        radius = float(radius)
    except (TypeError, ValueError) as e:
        raise ViewportCoordinateError(f"radius must be numeric: {e}")
    if radius <= 0:
        raise ViewportCoordinateError(f"radius must be positive, got: {radius}")

    space = canonical_space(space)
    if space == VIEWPORT_NORMALIZED:
        local_r = radius
    elif space == VIEWPORT_PIXELS:
        if not render_width:
            raise ViewportCoordinateError("viewport_pixels requires the snapshot's render width")
        local_r = radius / render_width
    else:
        raise ViewportCoordinateError(
            f"coordinate_space must be one of {POINT_SPACES}, got: {space!r}"
        )
    return local_r * r["w"]


# ---- The other direction: display frame -> snapshot render pixels -----------
#
# Used by retouch_render_overlay to PLOT existing shapes on top of a captured
# render. Unlike the write path above, these functions do NOT reject
# out-of-region input: a shape legitimately sits outside the captured viewport
# (it was placed while the view was elsewhere), and the honest answer is
# "off-render", not an error that hides the other shapes. Callers get an
# `inside` flag and decide.


def image_point_to_snapshot_pixels(
    region: dict[str, Any],
    point: dict[str, Any],
    render_width: int,
    render_height: int,
    label: str = "point",
) -> dict[str, Any]:
    """Display-frame-normalized point -> pixel coordinates within the captured
    render. Returns {"x": px, "y": px, "inside": bool}; `inside` is False when
    the point falls outside the captured region (the pixel values are then the
    honest extrapolation, not a clamp)."""
    r = _require_region(region)
    if not isinstance(point, dict) or "x" not in point or "y" not in point:
        raise ViewportCoordinateError(f"{label} {{x,y}} is required")
    try:
        x = float(point["x"])
        y = float(point["y"])
    except (TypeError, ValueError) as e:
        raise ViewportCoordinateError(f"{label} values must be numeric: {e}")
    if not render_width or not render_height:
        raise ViewportCoordinateError(
            "image_point_to_snapshot_pixels requires the render width/height"
        )

    local_x = (x - r["x"]) / r["w"]
    local_y = (y - r["y"]) / r["h"]
    return {
        "x": local_x * render_width,
        "y": local_y * render_height,
        "inside": 0.0 <= local_x <= 1.0 and 0.0 <= local_y <= 1.0,
    }


def image_radius_to_snapshot_pixels(
    region: dict[str, Any],
    radius: float,
    render_width: int,
) -> float:
    """Display-frame-normalized LENGTH (normalized against the display frame's
    width) -> pixels within the captured render. Inverse of
    viewport_radius_to_image: divide by region.w, scale by render width --
    width only, never height, never an average."""
    r = _require_region(region)
    try:
        radius = float(radius)
    except (TypeError, ValueError) as e:
        raise ViewportCoordinateError(f"radius must be numeric: {e}")
    if not render_width:
        raise ViewportCoordinateError("image_radius_to_snapshot_pixels requires the render width")
    return (radius / r["w"]) * render_width
