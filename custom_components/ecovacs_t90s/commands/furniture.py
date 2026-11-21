"""Furniture and carpets known by the robot (``getFurnitureInfo_V2``).

Request: ``getFurnitureInfo_V2 {"mid": "<map id>"}``. Answer ``body.data``::

    {"mid": "<map id>", "info": [
        {"content": "x1,y1;x2,y2;...;xN,yN;<type>", "fid": 4, "type": 4,
         "source": 0, "edited": 0, "direction": "",
         "info": "4_1,undefined,270,0,1,0"},
        ...]}

``content`` is a polygon with any number of points (rectangles and L shapes
were seen on a DEEBOT T90S PRO OMNI) in map millimetres, the same frame as the
robot and charger positions of ``getPos``, followed by the type. The robot
also pushes ``onFurnitureInfo_V2``. Format first described in deebot-client
PR #2030 (T80 OMNI); checked against the answer of a T90S PRO OMNI.

Known types: 20 is a carpet (PR #2030, three carpets on the T90S). 8 is a bed
in the test data of PR #2030 (unverified). The T90S also reports 2, 4, 1005,
1006, 1014 and 1015, whose meaning no public source documents yet; they are
drawn as a generic outline.

The parser never raises: malformed entries are skipped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Any, Final

from .base import T90Command

COMMAND_NAME: Final = "getFurnitureInfo_V2"
PUSH_NAMES: Final = frozenset({"onFurnitureInfo_V2", "onFurnitureInfo"})

TYPE_CARPET: Final = 20
# type -> label key; see the module docstring for the sources
TYPE_LABELS: Final[Mapping[int, str]] = {
    TYPE_CARPET: "carpet",
    8: "bed",
}

MAX_ITEMS: Final = 300
MAX_POINTS: Final = 100
# Coordinates outside of this range (mm) are rejected as garbage
MAX_COORDINATE: Final = 1_000_000


def get_furniture_command(map_id: str) -> T90Command:
    """Return the command that requests the furniture of a map."""
    return T90Command(COMMAND_NAME, {"mid": str(map_id)})


@dataclass(frozen=True, kw_only=True)
class FurnitureItem:
    """One piece of furniture (or carpet) as outline in map millimetres."""

    fid: int | None
    type: int | None
    points: tuple[tuple[float, float], ...]
    source: int | None = None
    edited: int | None = None
    direction: str = ""
    info: str = ""

    @property
    def is_carpet(self) -> bool:
        """Return True for carpets."""
        return self.type == TYPE_CARPET

    @property
    def label(self) -> str | None:
        """Return the label key of the type, if known."""
        return TYPE_LABELS.get(self.type) if self.type is not None else None

    @property
    def angle(self) -> int | None:
        """Return the angle from the info field (third value), if present."""
        parts = self.info.split(",")
        return _to_int(parts[2]) if len(parts) >= 3 else None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly representation (attributes, diagnostics)."""
        return {
            "fid": self.fid,
            "type": self.type,
            "label": self.label,
            "points": [[_compact(x), _compact(y)] for x, y in self.points],
            "source": self.source,
            "edited": self.edited,
            "direction": self.direction,
            "info": self.info,
        }


def _compact(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _to_float(value: str) -> float | None:
    try:
        number = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(number) or abs(number) > MAX_COORDINATE:
        return None
    return number


def parse_furniture_item(raw: Any) -> FurnitureItem | None:
    """Parse one entry of ``info``; return None if it is unusable."""
    if not isinstance(raw, Mapping):
        return None
    content = raw.get("content")
    if not isinstance(content, str):
        return None

    points: list[tuple[float, float]] = []
    content_type: int | None = None
    for raw_segment in content.split(";"):
        segment = raw_segment.strip()
        if not segment:
            continue
        if "," not in segment:
            # the last field is the type
            content_type = _to_int(segment)
            continue
        values = segment.split(",")
        if len(values) != 2:
            return None
        x, y = _to_float(values[0]), _to_float(values[1])
        if x is None or y is None:
            return None
        points.append((x, y))

    if len(points) < 3 or len(points) > MAX_POINTS:
        return None

    item_type = _to_int(raw.get("type"))
    if item_type is None:
        item_type = content_type
    direction = raw.get("direction")
    info = raw.get("info")
    return FurnitureItem(
        fid=_to_int(raw.get("fid")),
        type=item_type,
        points=tuple(points),
        source=_to_int(raw.get("source")),
        edited=_to_int(raw.get("edited")),
        direction=direction if isinstance(direction, str) else "",
        info=info if isinstance(info, str) else "",
    )


def parse_furniture_info(data: Any) -> tuple[str | None, list[FurnitureItem]]:
    """Parse ``body.data``; return the map id (if any) and the usable items."""
    if not isinstance(data, Mapping):
        return None, []
    mid = data.get("mid")
    map_id = str(mid) if isinstance(mid, (str, int)) and str(mid) else None
    if isinstance(mid, bool):
        map_id = None

    info = data.get("info")
    if isinstance(info, Mapping):
        info = [info]
    if not isinstance(info, list):
        return map_id, []

    items = [item for raw in info[:MAX_ITEMS] if (item := parse_furniture_item(raw))]
    return map_id, items
