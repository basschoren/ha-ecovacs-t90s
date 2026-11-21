"""Clean, charge and station commands with their exact payloads.

Sources of the payloads:

- ``clean_V2`` start: verified on a DEEBOT T90S PRO OMNI (833gj5, fw 1.50.62;
  ``clean`` is answered with 20003 there). The same payload is what
  ecovacs-deebot.js sends.
- ``clean_V2`` stop, ``freeClean`` rooms and rectangles: replayed on a T90 PRO
  OMNI (class k2iwaq, fw 1.103.0) in deebot-client issue #2027, and on class
  twunby in PR #1860. Not yet verified on the T90S.
- ``clean_V2`` pause/resume, zone + room in one job, ``qcClean`` scenarios and
  ``stationAction`` stop wash/dry: app captures in issue #2027.
- ``clean`` (V1), ``charge`` and ``playSound``: deebot-client 18.7.0
  (``Clean``, ``CleanArea``, ``Charge``, ``PlaySound``).

The ``freeClean`` value is a list of elements separated by ``;``. Each element
starts with its type: ``1,<room id>`` is a room, ``3,null,x1,y1,x2,y2`` a
rectangle in map millimetres (issue #2027; ecovacs-deebot.js builds rooms the
same way). deebot-client's ``CleanAreaV2`` writes ``<count>,<room>,<room>``,
which the robots read as one room only (PR #1860).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Final

from deebot_client.models import CleanAction

from .base import T90Command

VARIANT_V2: Final = "v2"
VARIANT_V1: Final = "v1"

# freeClean element types (issue #2027)
FREE_CLEAN_ROOM: Final = 1
FREE_CLEAN_RECTANGLE: Final = 3

CHARGE_ALREADY_CHARGING: Final = 30007

_V2_ACTION_ARGS: Final[dict[CleanAction, dict[str, Any]]] = {
    CleanAction.START: {
        "act": "start",
        "content": {"total": 0, "donotClean": 0, "count": 0, "type": "auto"},
    },
    CleanAction.PAUSE: {"act": "pause"},
    CleanAction.RESUME: {"act": "resume"},
    CleanAction.STOP: {"act": "stop"},
}
_V1_ACTION_ARGS: Final[dict[CleanAction, dict[str, Any]]] = {
    CleanAction.START: {"act": "start", "type": "auto"},
    CleanAction.PAUSE: {"act": "pause"},
    CleanAction.RESUME: {"act": "resume"},
    CleanAction.STOP: {"act": "stop"},
}

type Payloads = dict[str, tuple[str, dict[str, Any]]]
type Rectangle = Sequence[int | float]


def _number(value: int | float) -> str:
    """Format a coordinate or id without a trailing .0."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def clean_action_payloads(action: CleanAction) -> Payloads:
    """Return the V2 and V1 payloads of a start/pause/resume/stop action."""
    return {
        VARIANT_V2: ("clean_V2", _deepcopy(_V2_ACTION_ARGS[action])),
        VARIANT_V1: ("clean", dict(_V1_ACTION_ARGS[action])),
    }


def _deepcopy(args: dict[str, Any]) -> dict[str, Any]:
    return {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in args.items()
    }


def free_clean_value(
    rectangles: Iterable[Rectangle] = (), rooms: Iterable[int] = ()
) -> str:
    """Return the freeClean value: rectangles first, then rooms."""
    elements = [
        ",".join([str(FREE_CLEAN_RECTANGLE), "null", *(_number(v) for v in rectangle)])
        for rectangle in rectangles
    ]
    elements.extend(f"{FREE_CLEAN_ROOM},{_number(room)}" for room in rooms)
    return ";".join(elements)


def _free_clean_args(value: str) -> dict[str, Any]:
    return {"act": "start", "content": {"type": "freeClean", "value": value}}


def rooms_payloads(rooms: Sequence[int], count: int = 1) -> Payloads:
    """Return the payloads that clean the given rooms in this order."""
    return {
        VARIANT_V2: ("clean_V2", _free_clean_args(free_clean_value(rooms=rooms))),
        VARIANT_V1: (
            "clean",
            {
                "act": "start",
                "type": "spotArea",
                "content": ",".join(_number(room) for room in rooms),
                "count": count,
            },
        ),
    }


def zone_payloads(
    rectangles: Sequence[Rectangle], rooms: Sequence[int] = (), count: int = 1
) -> Payloads:
    """Return the payloads that clean rectangles (and rooms) in one job.

    The V1 command only knows one rectangle without rooms.
    """
    payloads: Payloads = {
        VARIANT_V2: (
            "clean_V2",
            _free_clean_args(free_clean_value(rectangles, rooms)),
        )
    }
    if len(rectangles) == 1 and not rooms:
        payloads[VARIANT_V1] = (
            "clean",
            {
                "act": "start",
                "type": "customArea",
                "content": ",".join(_number(v) for v in rectangles[0]),
                "count": count,
            },
        )
    return payloads


def scenario_command(scenario_id: str) -> T90Command:
    """Return the command that starts a saved scenario (app capture #2027)."""
    return T90Command(
        "clean_V2",
        {"act": "start", "content": {"type": "qcClean", "value": str(scenario_id)}},
    )


def charge_command() -> T90Command:
    """Return the return-to-dock command (code 30007: already charging)."""
    return T90Command(
        "charge",
        {"act": "go"},
        ok_codes=frozenset({0, CHARGE_ALREADY_CHARGING}),
    )


def play_sound_command() -> T90Command:
    """Return the locate command."""
    return T90Command("playSound", {"count": 1, "sid": 30})


# stationAction types (deebot-client StationAction)
STATION_DRY_MOP: Final = 2
STATION_WASH_MOP: Final = 4
_STATION_STOP: Final = 4


def station_stop_command(station_type: int) -> T90Command:
    """Return the command that stops a station action (app capture #2027)."""
    return T90Command("stationAction", {"act": _STATION_STOP, "type": station_type})
