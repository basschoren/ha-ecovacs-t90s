"""Settings the robot supports but deebot-client 18.7.0 has no entity for.

Each setting is read with a ``get...`` command (also used as the probe whether
the robot supports it) and changed with the matching ``set...`` command.
Answers of a DEEBOT T90S PRO OMNI (833gj5, fw 1.50.62) read with
``ecovacs_t90s.raw_command``::

    getWashInfo        {"amount": 1, "dryMop": 0, "duration": 60,
                        "interval": 15, "mode": 1, "wiseMode": 2}
    getDryingDuration  {"duration": 180}
    getMopExpandState  {"enable": 1, "level": 1}
    getCustomAreaMode  {"sweepMode": 0}
    getTrueDetect      {"enable": 1, "level": 1}
    getBlock           {"enable": 0, "end": "8:0", "start": "22:0"}
    getQuickCommand    [{"array": [], "mid": "...", "sort": "",
                         "supportBatchDel": 1}]   ({"type": "1,2"})
    getCarpetInfo      20003 rcp not support

Meaning of the set payloads (not yet verified on the T90S):

- ``setWashInfo {"mode": 0..3}``: mop wash water temperature (issue #2028,
  T90 PRO OMNI app capture). The app sends only the changed field, which
  makes deebot-client 18.7.0 log an error for the p2p request (#2028), so the
  current ``interval`` is sent along when known.
- ``setDryingDuration {"duration": 120|180|240}`` (issue #555, T30 OMNI).
- ``setCustomAreaMode {"sweepMode": 0|1|2}``: standard / deep / quick
  cleaning (issue #555).
- ``setMopExpandState {"enable", "level"}``: edge mopping, level 1 standard,
  level 0 extreme (issue #555; the T30 also sends ``frequency``, which the
  T90S does not report, so it is only sent back when the robot reported it).
- ``setTrueDetect {"enable": 1, "level"}``: level 1 standard, 0 high (#555).
- ``setBlock {"enable", "start", "end"}``: do not disturb (issue #555).
- ``setCachedMapInfo {"act": "userchangemap", "userUsing": 1, "mid"}``:
  switch the active map (app capture, issue #2027).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .base import T90Command


@dataclass(frozen=True, kw_only=True)
class SettingSpec:
    """A robot setting read with a get command and changed with a set command."""

    key: str
    get: str
    set: str | None = None
    get_args: Mapping[str, Any] | None = None
    # Keys body.data must contain for the setting to count as supported
    required: tuple[str, ...] = ()
    validator: Callable[[Any], bool] | None = None
    # Robot pushes with the same data, e.g. onWashInfo
    push: frozenset[str] = field(default_factory=frozenset)

    def get_command(self) -> T90Command:
        """Return the get (probe) command."""
        return T90Command(self.get, dict(self.get_args) if self.get_args else None)

    def set_command(self, args: Mapping[str, Any]) -> T90Command:
        """Return the set command."""
        if self.set is None:
            raise NotImplementedError(self.key)
        return T90Command(self.set, dict(args))

    def is_valid(self, data: Any) -> bool:
        """Return True if the get answer shows that the setting is supported."""
        if self.validator is not None:
            return self.validator(data)
        return isinstance(data, Mapping) and all(key in data for key in self.required)


def _push_names(get_name: str) -> frozenset[str]:
    base = get_name.removeprefix("get")
    return frozenset({f"on{base}", f"on{base}_V2"})


def _spec(key: str, get: str, set_: str | None, *required: str) -> SettingSpec:
    return SettingSpec(
        key=key, get=get, set=set_, required=required, push=_push_names(get)
    )


# ---------------------------------------------------------------- scenarios


@dataclass(frozen=True, kw_only=True)
class Scenario:
    """A saved cleaning scenario (quick command)."""

    id: str
    name: str
    map_id: str | None = None


_SCENARIO_ID_KEYS: Final = ("qcid", "qcId", "id", "cid")
_SCENARIO_NAME_KEYS: Final = ("name", "qcName", "title", "alias")


def _first_str(entry: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = entry.get(key)
        if (
            isinstance(value, (str, int))
            and not isinstance(value, bool)
            and (text := str(value).strip())
        ):
            return text
    return None


def parse_scenarios(data: Any) -> list[Scenario]:
    """Parse the answer of getQuickCommand.

    The T90S answers ``[{"array": [], "mid": "...", ...}]`` (one entry per
    map, the scenarios in ``array``). The format of a scenario entry is not
    known yet, so common id/name keys are tried. Never raises.
    """
    groups: list[Any]
    if isinstance(data, list):
        groups = data
    elif isinstance(data, Mapping):
        groups = [data]
    else:
        return []

    scenarios: dict[str, Scenario] = {}
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        map_id = _first_str(group, ("mid",))
        entries = group.get("array", group.get("list"))
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if (scenario_id := _first_str(entry, _SCENARIO_ID_KEYS)) is None:
                continue
            name = _first_str(entry, _SCENARIO_NAME_KEYS) or f"#{scenario_id}"
            scenarios.setdefault(
                scenario_id, Scenario(id=scenario_id, name=name, map_id=map_id)
            )
    return list(scenarios.values())


def _has_scenarios(data: Any) -> bool:
    return bool(parse_scenarios(data))


# -------------------------------------------------------------------- specs

WASH_INFO: Final = _spec("wash_info", "getWashInfo", "setWashInfo", "mode")
DRYING_DURATION: Final = _spec(
    "drying_duration", "getDryingDuration", "setDryingDuration", "duration"
)
CUSTOM_AREA_MODE: Final = _spec(
    "custom_area_mode", "getCustomAreaMode", "setCustomAreaMode", "sweepMode"
)
MOP_EXPAND: Final = _spec(
    "mop_expand", "getMopExpandState", "setMopExpandState", "enable"
)
TRUE_DETECT_LEVEL: Final = _spec(
    "true_detect_level", "getTrueDetect", "setTrueDetect", "enable", "level"
)
BLOCK: Final = _spec("block", "getBlock", "setBlock", "enable")
QUICK_COMMAND: Final = SettingSpec(
    key="quick_command",
    get="getQuickCommand",
    get_args={"type": "1,2"},
    validator=_has_scenarios,
    push=_push_names("getQuickCommand"),
)

SETTINGS: Final[tuple[SettingSpec, ...]] = (
    WASH_INFO,
    DRYING_DURATION,
    CUSTOM_AREA_MODE,
    MOP_EXPAND,
    TRUE_DETECT_LEVEL,
    BLOCK,
    QUICK_COMMAND,
)

# deebot-client capabilities without an entity in the core integration; the
# entity uses the deebot-client capability, the probe only guards creation.
VOICE_ASSISTANT: Final = SettingSpec(
    key="voice_assistant", get="getVoiceAssistantState", required=("enable",)
)
OTA: Final = SettingSpec(
    key="ota_auto_update",
    get="getOta",
    validator=lambda data: isinstance(data, Mapping) and bool(data.get("supportAuto")),
)
SWEEP_MODE: Final = SettingSpec(key="mop_only", get="getSweepMode", required=("type",))


# ----------------------------------------------------- option <-> payload maps

WASH_TEMPERATURE_MODES: Final[Mapping[str, int]] = {
    "cold": 0,
    "warm": 1,
    "hot": 2,
    "max": 3,
}
DRYING_DURATIONS: Final[Mapping[str, int]] = {
    "two_hours": 120,
    "three_hours": 180,
    "four_hours": 240,
}
CLEANING_SPEEDS: Final[Mapping[str, int]] = {"standard": 0, "deep": 1, "quick": 2}
TRUE_DETECT_LEVELS: Final[Mapping[str, int]] = {"standard": 1, "high": 0}
MOP_EXTENSION_OPTIONS: Final = ("off", "standard", "extreme")
_MOP_EXTENSION_LEVELS: Final[Mapping[str, int]] = {"standard": 1, "extreme": 0}

DEFAULT_BLOCK_START: Final = "22:0"
DEFAULT_BLOCK_END: Final = "8:0"


def _option_for(mapping: Mapping[str, int], value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    return next((option for option, item in mapping.items() if item == value), None)


def wash_temperature_option(data: Mapping[str, Any]) -> str | None:
    """Return the selected wash water temperature."""
    return _option_for(WASH_TEMPERATURE_MODES, data.get("mode"))


def wash_temperature_args(option: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the setWashInfo payload for a temperature."""
    args: dict[str, Any] = {"mode": WASH_TEMPERATURE_MODES[option]}
    if isinstance(interval := data.get("interval"), int) and not isinstance(
        interval, bool
    ):
        args["interval"] = interval
    return args


def drying_duration_option(data: Mapping[str, Any]) -> str | None:
    """Return the selected drying duration."""
    return _option_for(DRYING_DURATIONS, data.get("duration"))


def drying_duration_args(option: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the setDryingDuration payload."""
    return {"duration": DRYING_DURATIONS[option]}


def cleaning_speed_option(data: Mapping[str, Any]) -> str | None:
    """Return the selected cleaning speed."""
    return _option_for(CLEANING_SPEEDS, data.get("sweepMode"))


def cleaning_speed_args(option: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the setCustomAreaMode payload."""
    return {"sweepMode": CLEANING_SPEEDS[option]}


def mop_extension_option(data: Mapping[str, Any]) -> str | None:
    """Return the selected edge mopping mode."""
    enable = data.get("enable")
    if enable in (0, False):
        return "off"
    if enable in (1, True):
        return _option_for(_MOP_EXTENSION_LEVELS, data.get("level"))
    return None


def mop_extension_args(option: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the setMopExpandState payload."""
    if option == "off":
        level = data.get("level")
        args: dict[str, Any] = {
            "enable": 0,
            "level": level
            if isinstance(level, int) and not isinstance(level, bool)
            else 1,
        }
    else:
        args = {"enable": 1, "level": _MOP_EXTENSION_LEVELS[option]}
    if "frequency" in data:
        args["frequency"] = data["frequency"]
    return args


def true_detect_level_option(data: Mapping[str, Any]) -> str | None:
    """Return the selected TrueDetect sensitivity."""
    return _option_for(TRUE_DETECT_LEVELS, data.get("level"))


def true_detect_level_args(option: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the setTrueDetect payload (also turns TrueDetect on)."""
    return {"enable": 1, "level": TRUE_DETECT_LEVELS[option]}


def block_args(enable: bool, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the setBlock payload, keeping the configured times."""
    start = data.get("start")
    end = data.get("end")
    return {
        "enable": 1 if enable else 0,
        "start": start if isinstance(start, str) and start else DEFAULT_BLOCK_START,
        "end": end if isinstance(end, str) and end else DEFAULT_BLOCK_END,
    }


def set_active_map_command(map_id: str) -> T90Command:
    """Return the command that makes a map the active one."""
    return T90Command(
        "setCachedMapInfo",
        {"act": "userchangemap", "userUsing": 1, "mid": str(map_id)},
    )
