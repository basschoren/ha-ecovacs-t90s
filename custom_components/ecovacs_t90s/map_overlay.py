"""Draw furniture and carpets on the SVG map of deebot-client.

deebot-client renders the map in Rust; instead of changing that renderer, a
``<g>`` layer is added to the finished SVG (before ``</svg>``).

Coordinates: the renderer converts map millimetres with ``calc_point`` in
deebot-client ``src/map/mod.rs`` (one SVG unit is 50 mm, y points down):

==========  =============  =============
rotation    svg x          svg y
==========  =============  =============
0           x / 50         -y / 50
90          y / 50         x / 50
180         -x / 50        y / 50
270         -y / 50        -x / 50
==========  =============  =============

Room outlines, the robot and the charger use the same function and the
viewBox is computed from those points without an offset, so furniture in the
same millimetre frame lines up (checked with an SVG of deebot-client 18.7.0
in the tests).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from xml.sax.saxutils import escape

from .commands.furniture import FurnitureItem

PIXEL_WIDTH = 50.0
LAYER_ID = "ecovacs-t90s-furniture"

_CARPET_PATTERN_ID = "ecovacs-t90s-carpet"
_CARPET_STYLE = (
    f'fill="url(#{_CARPET_PATTERN_ID})" stroke="#8d6e63" stroke-opacity="0.8" '
    'stroke-width="1" stroke-dasharray="3 2" vector-effect="non-scaling-stroke"'
)
_FURNITURE_STYLE = (
    'fill="#9e9e9e" fill-opacity="0.22" stroke="#616161" stroke-opacity="0.75" '
    'stroke-width="1" vector-effect="non-scaling-stroke"'
)
_CARPET_PATTERN = (
    f'<defs><pattern id="{_CARPET_PATTERN_ID}" width="2" height="2" '
    'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    '<rect width="2" height="2" fill="#bcaaa4" fill-opacity="0.25"/>'
    '<line x1="0" y1="0" x2="0" y2="2" stroke="#8d6e63" stroke-opacity="0.35" '
    'stroke-width="0.6"/></pattern></defs>'
)
# Labels of furniture types without a known name (the type number) help to
# find out which id is which piece of furniture
LABEL_UNKNOWN_TYPES = True

LABELS: dict[str, dict[str, str]] = {
    "carpet": {"en": "Carpet", "nl": "Tapijt"},
    "bed": {"en": "Bed", "nl": "Bed"},
}


def calc_point(x: float, y: float, rotation: int) -> tuple[float, float]:
    """Convert map millimetres to SVG units (as deebot-client's calc_point)."""
    match rotation:
        case 90:
            px, py = y / PIXEL_WIDTH, x / PIXEL_WIDTH
        case 180:
            px, py = -x / PIXEL_WIDTH, y / PIXEL_WIDTH
        case 270:
            px, py = -y / PIXEL_WIDTH, -x / PIXEL_WIDTH
        case _:
            px, py = x / PIXEL_WIDTH, -y / PIXEL_WIDTH
    return round(px, 3) + 0.0, round(py, 3) + 0.0


def _fmt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _centroid(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Return the area centroid of a polygon (bounding box centre if flat)."""
    area = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(points, [*points[1:], points[0]], strict=True):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-9:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return cx / (3 * area), cy / (3 * area)


def label_text(item: FurnitureItem, language: str = "en") -> str | None:
    """Return the label drawn on a piece of furniture."""
    if (key := item.label) is not None:
        names = LABELS.get(key, {})
        return names.get(language) or names.get("en") or key
    if LABEL_UNKNOWN_TYPES and item.type is not None:
        return str(item.type)
    return None


def build_furniture_layer(
    items: Iterable[FurnitureItem], rotation: int = 0, language: str = "en"
) -> str:
    """Return the SVG group with the furniture, or "" if there is none."""
    # Carpets first, furniture can stand on them
    ordered = sorted(items, key=lambda item: (not item.is_carpet, item.fid or 0))
    if not ordered:
        return ""

    shapes: list[str] = []
    labels: list[str] = []
    for item in ordered:
        points = [calc_point(x, y, rotation) for x, y in item.points]
        style = _CARPET_STYLE if item.is_carpet else _FURNITURE_STYLE
        shapes.append(
            f'<polygon points="{" ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in points)}" '
            f"{style}/>"
        )
        if (text := label_text(item, language)) is None:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        size = max(1.5, min(4.0, (max(xs) - min(xs)) / 4, (max(ys) - min(ys)) / 2))
        if item.label is None:
            size = min(size, 2.5)
        cx, cy = _centroid(points)
        labels.append(
            f'<text x="{_fmt(cx)}" y="{_fmt(cy)}" font-size="{_fmt(size)}" '
            'text-anchor="middle" dominant-baseline="central" '
            'font-family="sans-serif" fill="#424242" fill-opacity="0.85">'
            f"{escape(text)}</text>"
        )

    return (
        f'<g id="{LAYER_ID}" pointer-events="none">'
        f"{_CARPET_PATTERN if any(i.is_carpet for i in ordered) else ''}"
        f"{''.join(shapes)}{''.join(labels)}</g>"
    )


def overlay_furniture(
    svg: str, items: Iterable[FurnitureItem], rotation: int = 0, language: str = "en"
) -> str:
    """Return the SVG with the furniture layer inserted before ``</svg>``."""
    layer = build_furniture_layer(items, rotation, language)
    if not layer or LAYER_ID in svg:
        return svg
    index = svg.rfind("</svg>")
    if index < 0:
        return svg
    return f"{svg[:index]}{layer}{svg[index:]}"
