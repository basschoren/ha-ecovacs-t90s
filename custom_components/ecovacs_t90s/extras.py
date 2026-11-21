"""Per-robot runtime for the features this integration adds to deebot-client.

- Probes at setup which extra settings the robot supports. A get command that
  is answered with an error (``20003 rcp not support``) or without the
  expected fields marks the feature unsupported; no entity is created. A
  probe without answer is retried later (and when the robot comes back).
  Entities of supported features are added through a dispatcher signal, so
  setup never waits for the robot.
- Keeps the values of these settings, updated from get answers, set commands
  and robot pushes (MQTT messages the robot sends, e.g. ``onWashInfo``).
- Keeps the furniture of the active map (``getFurnitureInfo_V2``).
- Runs clean commands in the variant the robot understands (``clean_V2`` or
  ``clean``), falling back to the other one on ``20003``.
- Holds the cloud connection state and the Matter twin (local fallback).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
import logging
import time
from typing import TYPE_CHECKING, Any, Final

from deebot_client.capabilities import CapabilitySet, CapabilitySetEnable
from deebot_client.device import Device
from deebot_client.events import (
    CachedMapInfoEvent,
    CleanLogEvent,
    MapInfoEvent,
    PositionsEvent,
)
from deebot_client.events.map import Map
from deebot_client.rs.map import PositionType, RotationAngle
import orjson

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .commands.base import CommandOutcome, T90Command
from .commands.clean import VARIANT_V1, VARIANT_V2, Payloads
from .commands.furniture import (
    PUSH_NAMES as FURNITURE_PUSH_NAMES,
    FurnitureItem,
    get_furniture_command,
    parse_furniture_info,
)
from .commands.settings import (
    OTA,
    SETTINGS,
    SWEEP_MODE,
    VOICE_ASSISTANT,
    SettingSpec,
)
from .connectivity import CloudConnection
from .const import SIGNAL_FEATURE_SUPPORTED
from .local_fallback import LocalFallback

if TYPE_CHECKING:
    from datetime import datetime

_LOGGER = logging.getLogger(__name__)

FEATURE_FURNITURE: Final = "furniture"
FEATURE_POSITION: Final = "position"
FEATURE_CLEAN_LOG_IMAGE: Final = "clean_log_image"
FEATURE_ACTIVE_MAP: Final = "active_map"

# Listener keys besides the setting keys
VALUE_MAPS: Final = "maps"

PROBE_TIMEOUT: Final = 30.0
COMMAND_TIMEOUT: Final = 60.0
PROBE_RETRY_DELAYS: Final = (120.0, 600.0, 1800.0)
# Probes without answer are not repeated forever (also on availability changes)
MAX_PROBE_ATTEMPTS: Final = 8
FURNITURE_MIN_INTERVAL: Final = 30.0

_ROTATIONS: Final[dict[RotationAngle, int]] = {
    RotationAngle.DEG_0: 0,
    RotationAngle.DEG_90: 90,
    RotationAngle.DEG_180: 180,
    RotationAngle.DEG_270: 270,
}


class FeatureStatus(StrEnum):
    """Whether the robot supports a feature."""

    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    NO_ANSWER = "no_answer"


def _body_data(payload: Any) -> Any:
    """Return body.data of a pushed message (bytes, str or dict)."""
    if isinstance(payload, (bytes, bytearray, str)):
        try:
            payload = orjson.loads(payload)
        except orjson.JSONDecodeError:
            return None
    if not isinstance(payload, Mapping):
        return None
    body = payload.get("body")
    if not isinstance(body, Mapping):
        return None
    return body.get("data")


def install_message_hook(device: Device, handler: Callable[[str, Any], None]) -> bool:
    """Also pass the MQTT messages of a robot to handler.

    deebot-client has no public hook for messages it does not know (for
    example ``onFurnitureInfo_V2``). ``Device.initialize`` subscribes the
    bound method ``Device._handle_message`` at the MQTT client, so it is
    wrapped on the instance before ``initialize`` runs. If deebot-client
    changes, the hook is skipped and only the pushes are missing.
    """
    original = getattr(device, "_handle_message", None)
    if not callable(original):
        _LOGGER.warning(
            "deebot-client changed: cannot follow robot messages; furniture "
            "and settings update only when Home Assistant asks for them"
        )
        return False

    def hooked(message_name: str, message_data: Any) -> None:
        original(message_name, message_data)
        try:
            handler(message_name, message_data)
        except Exception:
            _LOGGER.exception("Error handling robot message %s", message_name)

    device._handle_message = hooked  # type: ignore[method-assign]  # noqa: SLF001
    return True


class DeviceExtras:
    """Runtime of the extra features of one robot."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: Device,
        *,
        prefer_v2: bool,
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.device = device
        self.did: str = device.device_info["did"]
        self.prefer_v2 = prefer_v2
        self.clean_variant: str | None = None
        self.cloud = CloudConnection(
            hass, device, self._async_recovery_probe, self._create_task
        )
        self.fallback = LocalFallback(hass, device.device_info.get("name"))
        self.features: dict[str, FeatureStatus] = {}
        self.feature_details: dict[str, str] = {}
        self.values: dict[str, Any] = {}
        self.maps: list[Map] = []
        self.active_map_id: str | None = None
        self.rotation: int = 0
        self.furniture: dict[str, list[FurnitureItem]] = {}
        self.message_hook_installed = False
        self._listeners: defaultdict[str, list[Callable[[], None]]] = defaultdict(list)
        self._push_specs: dict[str, SettingSpec] = {}
        self._furniture_requested: dict[str, float] = {}
        self._furniture_task: asyncio.Task[Any] | None = None
        self._probe_attempts = 0
        self._probe_retry: CALLBACK_TYPE | None = None
        self._probing = False
        self._unsubs: list[Callable[[], None]] = []
        self._started = False

    # ------------------------------------------------------------- lifecycle

    def _create_task(self, coro: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        return self.entry.async_create_background_task(
            self.hass, coro, f"{name}_{self.did}"
        )

    @callback
    def async_start(self) -> None:
        """Start following the robot and probe the extra features."""
        if self._started:
            return
        self._started = True
        self.cloud.async_start()
        self.fallback.async_start()
        self._unsubs.append(self.cloud.async_subscribe(self._on_cloud_change))
        for spec in self._specs():
            for name in spec.push:
                self._push_specs[name] = spec

        events = self.device.events
        capabilities = self.device.capabilities
        if capabilities.map:
            self._unsubs.append(
                events.subscribe(CachedMapInfoEvent, self._on_cached_map_info)
            )
            self._unsubs.append(events.subscribe(MapInfoEvent, self._on_map_info))
            self._unsubs.append(events.subscribe(PositionsEvent, self._on_positions))
        if capabilities.clean.log:
            self._unsubs.append(events.subscribe(CleanLogEvent, self._on_clean_log))

        self._create_task(self.async_probe(), "ecovacs_t90s_probe")

    @callback
    def async_stop(self) -> None:
        """Stop all listeners and timers."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._probe_retry is not None:
            self._probe_retry()
            self._probe_retry = None
        self.cloud.async_stop()
        self.fallback.async_stop()
        self._listeners.clear()

    # ------------------------------------------------------------- listeners

    @callback
    def async_subscribe(
        self, key: str, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Call listener when the value of key changes."""
        self._listeners[key].append(listener)

        def remove() -> None:
            if listener in self._listeners.get(key, []):
                self._listeners[key].remove(listener)

        return remove

    def _notify(self, key: str) -> None:
        for listener in list(self._listeners.get(key, [])):
            listener()

    def supported(self, key: str) -> bool:
        """Return True if the robot supports the feature."""
        return self.features.get(key) is FeatureStatus.SUPPORTED

    def supported_features(self) -> list[str]:
        """Return the keys of all supported features."""
        return [
            key
            for key, status in self.features.items()
            if status is FeatureStatus.SUPPORTED
        ]

    def _mark_supported(self, key: str) -> None:
        if self.features.get(key) is FeatureStatus.SUPPORTED:
            return
        self.features[key] = FeatureStatus.SUPPORTED
        self.feature_details.pop(key, None)
        _LOGGER.debug("Robot supports %s", key)
        async_dispatcher_send(
            self.hass,
            SIGNAL_FEATURE_SUPPORTED.format(self.entry.entry_id),
            self.did,
            key,
        )

    def _mark(self, key: str, status: FeatureStatus, detail: str) -> None:
        self.features[key] = status
        self.feature_details[key] = detail
        _LOGGER.debug("Robot feature %s: %s (%s)", key, status, detail)

    # -------------------------------------------------------------- commands

    async def async_execute(
        self,
        command: T90Command,
        *,
        limit: float | None = COMMAND_TIMEOUT,
        report_failure: bool = True,
    ) -> T90Command:
        """Execute a command through the device queue and note the outcome."""
        try:
            async with asyncio.timeout(limit):
                await self.device.execute_command(command)
        except TimeoutError:
            command.outcome = CommandOutcome.NO_ANSWER
        if command.outcome is CommandOutcome.PENDING:
            command.outcome = CommandOutcome.NO_ANSWER
        if not command.outcome.is_connection_problem:
            # The robot answered, even if with an error
            self.cloud.report_success()
        elif report_failure:
            self.cloud.report_failure(str(command.outcome))
        return command

    async def async_run_clean(
        self, payloads: Payloads, *, limit: float | None = COMMAND_TIMEOUT
    ) -> T90Command | None:
        """Run a clean command, trying the other variant on 20003."""
        order = [VARIANT_V2, VARIANT_V1] if self.prefer_v2 else [VARIANT_V1, VARIANT_V2]
        if self.clean_variant in order:
            order.remove(self.clean_variant)
            order.insert(0, self.clean_variant)
        command: T90Command | None = None
        for variant in order:
            if variant not in payloads:
                continue
            name, args = payloads[variant]
            command = await self.async_execute(T90Command(name, args), limit=limit)
            if command.outcome is CommandOutcome.OK:
                if self.clean_variant != variant:
                    _LOGGER.debug("Robot accepts the %s clean commands", variant)
                self.clean_variant = variant
                return command
            if command.outcome is not CommandOutcome.UNSUPPORTED:
                return command
            _LOGGER.debug("Robot does not support %s %s", name, args)
        return command

    async def _async_recovery_probe(self) -> bool:
        command = await self.async_execute(
            T90Command("getBattery"), limit=PROBE_TIMEOUT, report_failure=False
        )
        return not command.outcome.is_connection_problem

    # ---------------------------------------------------------------- probes

    def _specs(self) -> list[SettingSpec]:
        specs = list(SETTINGS)
        settings = self.device.capabilities.settings
        if settings.voice_assistant is not None:
            specs.append(VOICE_ASSISTANT)
        if isinstance(settings.ota, CapabilitySetEnable):
            specs.append(OTA)
        if settings.sweep_mode is not None:
            specs.append(SWEEP_MODE)
        return specs

    async def async_probe(self) -> None:
        """Probe the settings whose support is not known yet."""
        if self._probing:
            return
        if self.cloud.robot_available is False:
            # Probed when the robot is available again
            return
        self._probing = True
        try:
            pending = [
                spec
                for spec in self._specs()
                if self.features.get(spec.key, FeatureStatus.UNKNOWN)
                in (FeatureStatus.UNKNOWN, FeatureStatus.NO_ANSWER)
            ]
            if not pending or self._probe_attempts >= MAX_PROBE_ATTEMPTS:
                return
            self._probe_attempts += 1
            await asyncio.gather(*(self._async_probe_one(spec) for spec in pending))
        finally:
            self._probing = False
        self._schedule_probe_retry()

    async def _async_probe_one(self, spec: SettingSpec) -> None:
        command = await self.async_execute(
            spec.get_command(), limit=PROBE_TIMEOUT, report_failure=False
        )
        outcome = command.outcome
        if outcome is CommandOutcome.OK:
            self.values[spec.key] = command.data
            if spec.is_valid(command.data):
                self._mark_supported(spec.key)
            else:
                self._mark(
                    spec.key,
                    FeatureStatus.UNSUPPORTED,
                    "answer without the expected data",
                )
            self._notify(spec.key)
        elif outcome in (CommandOutcome.UNSUPPORTED, CommandOutcome.REJECTED):
            self._mark(spec.key, FeatureStatus.UNSUPPORTED, f"code {command.code}")
        else:
            self._mark(spec.key, FeatureStatus.NO_ANSWER, str(outcome))

    def _schedule_probe_retry(self) -> None:
        if self._probe_retry is not None:
            return
        if FeatureStatus.NO_ANSWER not in self.features.values():
            return
        if self._probe_attempts > len(PROBE_RETRY_DELAYS):
            return

        @callback
        def retry(_now: datetime) -> None:
            self._probe_retry = None
            self._create_task(self.async_probe(), "ecovacs_t90s_probe")

        self._probe_retry = async_call_later(
            self.hass, PROBE_RETRY_DELAYS[self._probe_attempts - 1], retry
        )

    @callback
    def _on_cloud_change(self) -> None:
        if self.cloud.robot_available and (
            FeatureStatus.NO_ANSWER in self.features.values()
            or not any(spec.key in self.features for spec in self._specs())
        ):
            self._create_task(self.async_probe(), "ecovacs_t90s_probe")

    # -------------------------------------------------------------- settings

    async def async_refresh(self, spec: SettingSpec) -> None:
        """Read a setting again."""
        command = await self.async_execute(
            spec.get_command(), limit=PROBE_TIMEOUT, report_failure=False
        )
        if command.outcome is CommandOutcome.OK and spec.is_valid(command.data):
            self.values[spec.key] = command.data
            self._notify(spec.key)

    async def async_set(self, spec: SettingSpec, args: Mapping[str, Any]) -> T90Command:
        """Change a setting; returns the command (check its outcome)."""
        command = await self.async_execute(spec.set_command(args))
        if command.outcome is CommandOutcome.OK:
            current = self.values.get(spec.key)
            merged = dict(current) if isinstance(current, Mapping) else {}
            merged.update(args)
            self.values[spec.key] = merged
            self._notify(spec.key)
        return command

    # ---------------------------------------------------------------- pushes

    @callback
    def handle_message(self, name: str, payload: Any) -> None:
        """Handle an MQTT message of the robot (after deebot-client did)."""
        self.cloud.report_success()
        if name in FURNITURE_PUSH_NAMES:
            data = _body_data(payload)
            if isinstance(data, Mapping) and "info" in data:
                map_id, items = parse_furniture_info(data)
                if map_id := map_id or self.active_map_id:
                    self._mark_supported(FEATURE_FURNITURE)
                    self._set_furniture(map_id, items)
            else:
                self._schedule_furniture_refresh(force=True)
            return
        if (spec := self._push_specs.get(name)) is None:
            return
        data = _body_data(payload)
        if not isinstance(data, Mapping) or not data:
            return
        current = self.values.get(spec.key)
        merged = dict(current) if isinstance(current, Mapping) else {}
        merged.update(data)
        self.values[spec.key] = merged
        self._notify(spec.key)

    # ----------------------------------------------------------------- maps

    async def _on_cached_map_info(self, event: CachedMapInfoEvent) -> None:
        self.maps = sorted(event.maps, key=lambda item: (item.name or "", item.id))
        active = next((item for item in event.maps if item.using), None)
        map_id = active.id if active else None
        rotation = _ROTATIONS.get(active.angle, 0) if active else 0
        changed = (map_id, rotation) != (self.active_map_id, self.rotation)
        self.active_map_id, self.rotation = map_id, rotation
        self._notify(VALUE_MAPS)
        major = (
            self.device.capabilities.map.major if self.device.capabilities.map else None
        )
        if len(event.maps) >= 2 and not isinstance(major, CapabilitySet):
            # The core active map select needs the "major" capability
            self._mark_supported(FEATURE_ACTIVE_MAP)
        if changed:
            self._notify(FEATURE_FURNITURE)
        if map_id:
            self._schedule_furniture_refresh(force=changed)

    async def _on_map_info(self, event: MapInfoEvent) -> None:
        # The outline was (re)sent, e.g. after the map was edited
        self._schedule_furniture_refresh(force=False)

    def _schedule_furniture_refresh(self, *, force: bool) -> None:
        if self.features.get(FEATURE_FURNITURE) is FeatureStatus.UNSUPPORTED:
            return
        if (map_id := self.active_map_id) is None:
            return
        if self._furniture_task is not None and not self._furniture_task.done():
            return
        last = self._furniture_requested.get(map_id)
        if (
            not force
            and last is not None
            and (time.monotonic() - last < FURNITURE_MIN_INTERVAL)
        ):
            return
        self._furniture_requested[map_id] = time.monotonic()
        self._furniture_task = self._create_task(
            self.async_refresh_furniture(map_id), "ecovacs_t90s_furniture"
        )

    async def async_refresh_furniture(self, map_id: str | None = None) -> None:
        """Request the furniture of a map (the active one by default)."""
        if (map_id := map_id or self.active_map_id) is None:
            return
        command = await self.async_execute(
            get_furniture_command(map_id), limit=PROBE_TIMEOUT, report_failure=False
        )
        if command.outcome is CommandOutcome.OK:
            answer_map_id, items = parse_furniture_info(command.data)
            self._mark_supported(FEATURE_FURNITURE)
            self._set_furniture(answer_map_id or map_id, items)
        elif command.outcome in (CommandOutcome.UNSUPPORTED, CommandOutcome.REJECTED):
            self._mark(
                FEATURE_FURNITURE, FeatureStatus.UNSUPPORTED, f"code {command.code}"
            )
        else:
            self._mark(FEATURE_FURNITURE, FeatureStatus.NO_ANSWER, str(command.outcome))
            # Try again on the next trigger
            self._furniture_requested.pop(map_id, None)

    def _set_furniture(self, map_id: str, items: list[FurnitureItem]) -> None:
        if self.furniture.get(map_id) == items:
            return
        self.furniture[map_id] = items
        if map_id == self.active_map_id or self.active_map_id is None:
            self._notify(FEATURE_FURNITURE)

    @callback
    def redraw_map(self) -> None:
        """Make the map image redraw the furniture (option changed)."""
        self._notify(FEATURE_FURNITURE)

    @property
    def active_furniture(self) -> list[FurnitureItem]:
        """Return the furniture of the active map."""
        if self.active_map_id is None:
            return []
        return self.furniture.get(self.active_map_id, [])

    # ----------------------------------------------------- positions & logs

    async def _on_positions(self, event: PositionsEvent) -> None:
        robot = next(
            (item for item in event.positions if item.type == PositionType.DEEBOT),
            None,
        )
        if robot is None:
            return
        charger = next(
            (item for item in event.positions if item.type == PositionType.CHARGER),
            None,
        )
        self.values[FEATURE_POSITION] = {
            "x": robot.x,
            "y": robot.y,
            "a": robot.a,
            "charger": {"x": charger.x, "y": charger.y} if charger else None,
        }
        self._mark_supported(FEATURE_POSITION)
        self._notify(FEATURE_POSITION)

    async def _on_clean_log(self, event: CleanLogEvent) -> None:
        logs = [log for log in event.logs if log.image_url]
        if not logs:
            return
        self.values[FEATURE_CLEAN_LOG_IMAGE] = max(logs, key=lambda log: log.timestamp)
        self._mark_supported(FEATURE_CLEAN_LOG_IMAGE)
        self._notify(FEATURE_CLEAN_LOG_IMAGE)

    # ----------------------------------------------------------- diagnostics

    def diagnostics(self) -> dict[str, Any]:
        """Return the state for diagnostics."""
        return {
            "prefer_clean_v2": self.prefer_v2,
            "clean_variant": self.clean_variant,
            "message_hook_installed": self.message_hook_installed,
            "features": {
                key: {"status": str(status), "detail": self.feature_details.get(key)}
                for key, status in sorted(self.features.items())
            },
            "settings": {
                key: value
                for key, value in self.values.items()
                if key not in (FEATURE_POSITION, FEATURE_CLEAN_LOG_IMAGE)
            },
            "maps": len(self.maps),
            "rotation": self.rotation,
            "furniture": [item.as_dict() for item in self.active_furniture],
            "cloud": self.cloud.diagnostics(),
            "local_fallback": self.fallback.diagnostics(),
        }
