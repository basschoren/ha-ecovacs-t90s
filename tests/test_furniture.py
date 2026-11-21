"""Furniture parser and SVG overlay."""

import base64
from compression import zstd
import json
import re

from deebot_client.events.map import Position
from deebot_client.rs.map import MapData, PositionType, RotationAngle
import pytest

from custom_components.ecovacs_t90s.commands.furniture import (
    FurnitureItem,
    get_furniture_command,
    parse_furniture_info,
    parse_furniture_item,
)
from custom_components.ecovacs_t90s.map_overlay import (
    LAYER_ID,
    build_furniture_layer,
    calc_point,
    label_text,
    overlay_furniture,
)

from .conftest import load_fixture

# Sample of the task description (format of deebot-client PR #2030)
SAMPLE = {
    "content": "1000,1000;1000,2600;3400,2600;3400,1000;8",
    "fid": 8,
    "type": 8,
    "source": 0,
    "edited": 0,
    "direction": "",
    "info": "8_1,undefined,270,0,1,0",
}


def test_command() -> None:
    """The request carries the map id."""
    command = get_furniture_command("123")
    assert (command.NAME, command.args) == ("getFurnitureInfo_V2", {"mid": "123"})


def test_parse_sample() -> None:
    """The sample is parsed."""
    item = parse_furniture_item(SAMPLE)
    assert item == FurnitureItem(
        fid=8,
        type=8,
        points=((1000, 1000), (1000, 2600), (3400, 2600), (3400, 1000)),
        source=0,
        edited=0,
        direction="",
        info="8_1,undefined,270,0,1,0",
    )
    assert item.angle == 270
    assert not item.is_carpet
    assert item.label == "bed"
    assert item.as_dict()["points"][0] == [1000, 1000]


def test_parse_real_robot() -> None:
    """Answer of a T90S: rectangles and an L shape, carpets and unknown types."""
    map_id, items = parse_furniture_info(load_fixture("furniture_833gj5.json"))
    assert map_id == "100000001"
    assert len(items) == 8
    assert [item.type for item in items] == [20, 20, 1014, 4, 2, 1015, 1006, 1005]
    assert sum(item.is_carpet for item in items) == 2
    l_shape = next(item for item in items if item.fid == 5)
    assert len(l_shape.points) == 6
    assert l_shape.points[-1] == (840, -9432)
    assert l_shape.angle == 90
    assert next(item for item in items if item.fid == 2).edited == 1


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "text",
        [],
        {},
        {"content": 5},
        {"content": ""},
        {"content": "1,2;3,4;20"},  # two points only
        {"content": "1,2;3,4;5,x;20"},  # not a number
        {"content": "1,2,3;3,4;5,6;20"},  # three values in a point
        {"content": "1,2;3,4;nan,6;20"},
        {"content": "1,2;3,4;1e12,6;20"},
        {"content": ";".join(["1,2"] * 101) + ";20"},  # too many points
    ],
)
def test_parse_malformed_item(raw: object) -> None:
    """Malformed entries are skipped, never raised."""
    assert parse_furniture_item(raw) is None


def test_parse_lenient_fields() -> None:
    """Missing or odd fields fall back to sensible values."""
    item = parse_furniture_item(
        {"content": " 0,0 ; 10,0 ; 10,10 ;; 1005 ", "type": True, "fid": "7"}
    )
    assert item is not None
    assert item.type == 1005  # from the content, as type is not a number
    assert item.fid == 7
    assert item.info == ""
    assert item.angle is None
    item = parse_furniture_item({"content": "0,0;10,0;10,10", "info": 5})
    assert item is not None
    assert item.type is None
    assert item.label is None


@pytest.mark.parametrize(
    ("data", "expected_mid", "expected_count"),
    [
        (None, None, 0),
        ([], None, 0),
        ({"mid": "1"}, "1", 0),
        ({"mid": "1", "info": "x"}, "1", 0),
        ({"mid": True, "info": [SAMPLE]}, None, 1),
        ({"mid": 5, "info": SAMPLE}, "5", 1),
        ({"info": [SAMPLE, {"content": "bad"}, None]}, None, 1),
    ],
)
def test_parse_malformed_info(
    data: object, expected_mid: str | None, expected_count: int
) -> None:
    """Malformed answers give what can be used."""
    map_id, items = parse_furniture_info(data)
    assert map_id == expected_mid
    assert len(items) == expected_count


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (20.0, 20.0)),
        (90, (-20.0, 20.0)),
        (180, (-20.0, -20.0)),
        (270, (20.0, -20.0)),
    ],
)
def test_calc_point(rotation: int, expected: tuple[float, float]) -> None:
    """Same transformation as deebot-client's calc_point (src/map/mod.rs)."""
    assert calc_point(1000, -1000, rotation) == expected


def _deebot_svg(rotation: RotationAngle) -> str:
    """Render an SVG with the Rust renderer of deebot-client."""
    info = [
        [
            "1",
            "1;0,0,1;5000,0,1;5000,-4000,1;0,-4000,1",
            "2;5000,0,1;9000,0,1;9000,-4000,1;5000,-4000,1",
        ],
        [
            "2",
            "1;0,0;5000,0;5000,-4000;0,-4000",
            "2;5000,0;9000,0;9000,-4000;5000,-4000",
        ],
    ]
    data = MapData()
    data.map_info.set(
        base64.b64encode(zstd.compress(json.dumps(info).encode())).decode()
    )
    svg = data.generate_svg(
        [],
        [
            Position(PositionType.DEEBOT, 1000, -1000, 0),
            Position(PositionType.CHARGER, -123, -15, 0),
        ],
        rotation,
    )
    assert svg is not None
    return svg


@pytest.mark.parametrize(
    ("rotation", "angle"),
    [
        (RotationAngle.DEG_0, 0),
        (RotationAngle.DEG_90, 90),
        (RotationAngle.DEG_270, 270),
    ],
)
def test_overlay_lines_up_with_deebot_svg(rotation: RotationAngle, angle: int) -> None:
    """A piece of furniture at the robot position is drawn where the robot is."""
    svg = _deebot_svg(rotation)
    robot = re.search(r'<use href="#d" x="([-\d.]+)" y="([-\d.]+)"', svg)
    assert robot is not None
    robot_xy = (float(robot.group(1)), float(robot.group(2)))

    item = FurnitureItem(
        fid=1,
        type=20,
        points=((1000, -1000), (2000, -1000), (2000, -2000), (1000, -2000)),
    )
    result = overlay_furniture(svg, [item], angle)
    assert result.startswith(svg[: svg.rfind("</svg>")])
    assert result.endswith("</g></svg>")
    polygon = re.search(rf'<g id="{LAYER_ID}".*?<polygon points="([^"]+)"', result)
    assert polygon is not None
    first = tuple(float(v) for v in polygon.group(1).split(" ")[0].split(","))
    assert first == robot_xy

    # The viewBox (room outline 0..9000 x 0..-4000 mm) contains the furniture
    view_box = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', svg)
    assert view_box is not None
    min_x, min_y, width, height = (float(v) for v in view_box.groups())
    for point in polygon.group(1).split(" "):
        x, y = (float(v) for v in point.split(","))
        assert min_x <= x <= min_x + width
        assert min_y <= y <= min_y + height

    # Applying twice does not draw twice
    assert overlay_furniture(result, [item], angle) == result


def test_overlay_real_robot_layer() -> None:
    """All furniture of the T90S is drawn; carpets first and with a pattern."""
    _, items = parse_furniture_info(load_fixture("furniture_833gj5.json"))
    layer = build_furniture_layer(items, 0, "nl")
    assert layer.count("<polygon") == 8
    assert layer.index("url(#ecovacs-t90s-carpet)") < layer.index('fill="#9e9e9e"')
    assert layer.count("url(#ecovacs-t90s-carpet)") == 2
    assert ">Tapijt</text>" in layer
    # Unknown types are labelled with their type number
    assert ">1014</text>" in layer
    # charger at (-123, -15) mm -> (-2.46, 0.3); a carpet corner (-30, -6229)
    assert "-0.6,124.58" in layer


def test_overlay_edge_cases() -> None:
    """Nothing is changed without furniture or without </svg>."""
    item = parse_furniture_item(SAMPLE)
    assert item is not None
    assert overlay_furniture("<svg></svg>", []) == "<svg></svg>"
    assert overlay_furniture("<svg>", [item]) == "<svg>"
    assert build_furniture_layer([]) == ""
    assert label_text(item, "xx") == "Bed"
    odd = FurnitureItem(fid=1, type=None, points=((0, 0), (1, 0), (0, 1)))
    assert label_text(odd) is None
    named = FurnitureItem(fid=1, type=20, points=((0, 0), (0, 0), (0, 0)))
    layer = build_furniture_layer([named])
    assert '<text x="0" y="0"' in layer
