"""Support for Ecovacs Ecovacs Vacuums.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE. Changes: removed the legacy (XMPP) vacuum; start, pause, resume,
stop and room cleaning use ``clean_V2`` first for mapped device classes with a
fallback to ``clean`` on ``20003`` (``commands/clean.py``); commands report
failures; commands are routed to the robot's Matter twin while the Ecovacs
cloud cannot reach the robot (``local_fallback.py``); rooms without a name get
a generated one; new services ``clean_zone`` and ``start_scenario``.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
import logging
from typing import TYPE_CHECKING, Any, override

from deebot_client.capabilities import Capabilities, DeviceType
from deebot_client.device import Device
from deebot_client.events import (
    CachedMapInfoEvent,
    CleanCountEvent,
    FanSpeedEvent,
    RoomsEvent,
    StateEvent,
)
from deebot_client.events.map import Map
from deebot_client.models import CleanAction, Room, State

from homeassistant.components.vacuum import (
    Segment,
    StateVacuumEntity,
    StateVacuumEntityDescription,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import slugify

from . import EcovacsConfigEntry
from .commands.base import CommandOutcome, T90Command
from .commands.clean import (
    CHARGE_ALREADY_CHARGING,
    Payloads,
    charge_command,
    clean_action_payloads,
    play_sound_command,
    rooms_payloads,
    scenario_command,
    zone_payloads,
)
from .const import DOMAIN
from .entity import EcovacsEntity
from .extras import DeviceExtras
from .local_fallback import matter_area_ids
from .util import get_name_key

_LOGGER = logging.getLogger(__name__)
_SEGMENTS_SEPARATOR = "_"

# Answer time allowed to the cloud before a command goes to the Matter twin
CLOUD_TIMEOUT_WITH_FALLBACK = 15.0
# Ask for the state again after a command, in case no push arrives
STATE_REFRESH_DELAY = 10.0

_ROOM_WORD = {"nl": "Kamer"}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Ecovacs vacuums."""

    controller = config_entry.runtime_data
    vacuums = [
        EcovacsVacuum(device, controller.get_extras(device))
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.VACUUM
    ]
    _LOGGER.debug("Adding Ecovacs Vacuums to Home Assistant: %s", vacuums)
    async_add_entities(vacuums)


_STATE_TO_VACUUM_STATE = {
    State.IDLE: VacuumActivity.IDLE,
    State.CLEANING: VacuumActivity.CLEANING,
    State.RETURNING: VacuumActivity.RETURNING,
    State.DOCKED: VacuumActivity.DOCKED,
    State.ERROR: VacuumActivity.ERROR,
    State.PAUSED: VacuumActivity.PAUSED,
}

_ATTR_ROOMS = "rooms"
_ATTR_CONTROL_PATH = "control_path"
_ATTR_LOCAL_ENTITY = "local_fallback_entity_id"

type LocalCall = tuple[str, dict[str, Any]]


def room_name(room: Room, language: str) -> str:
    """Return the room name, or "Room <id>" for rooms without a name."""
    if name := room.name.strip():
        return name
    word = _ROOM_WORD.get(language.split("-", maxsplit=1)[0].lower(), "Room")
    return f"{word} {room.id}"


def _int_list(value: Any, what: str) -> list[int]:
    """Return room ids from a list or a comma separated string."""
    items = value.split(",") if isinstance(value, str) else value
    try:
        return [int(item) for item in items]
    except TypeError, ValueError:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_room_ids",
            translation_placeholders={"value": str(value), "what": what},
        ) from None


def _rectangles(value: Any) -> list[list[float]]:
    """Return rectangles from [[x1, y1, x2, y2], ...], [x1, ...] or "x1,..."."""
    if isinstance(value, str):
        value = value.split(",")
    try:
        if value and all(isinstance(item, (list, tuple)) for item in value):
            rectangles = [[float(v) for v in item] for item in value]
        else:
            flat = [float(v) for v in value]
            rectangles = [flat[i : i + 4] for i in range(0, len(flat), 4)]
    except TypeError, ValueError:
        rectangles = []
    if not rectangles or any(len(rectangle) != 4 for rectangle in rectangles):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_zone",
            translation_placeholders={"value": str(value)},
        )
    return rectangles


class EcovacsVacuum(
    EcovacsEntity[Capabilities],
    StateVacuumEntity,
):
    """Ecovacs vacuum."""

    _unrecorded_attributes = frozenset({_ATTR_ROOMS})

    _attr_supported_features = (
        VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
    )

    entity_description = StateVacuumEntityDescription(
        key="vacuum", translation_key="vacuum", name=None
    )

    def __init__(self, device: Device, extras: DeviceExtras) -> None:
        """Initialize the vacuum."""
        super().__init__(device, device.capabilities)
        self._extras = extras

        self._room_event: RoomsEvent | None = None
        self._maps: dict[str, Map] = {}
        self._clean_count = 1
        self._state_refresh: CALLBACK_TYPE | None = None

        if fan_speed := self._capability.fan_speed:
            self._attr_supported_features |= VacuumEntityFeature.FAN_SPEED
            self._attr_fan_speed_list = [
                get_name_key(level) for level in fan_speed.types
            ]

        if self._capability.map and self._capability.clean.action.area:
            self._attr_supported_features |= VacuumEntityFeature.CLEAN_AREA

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_status(event: StateEvent) -> None:
            self._attr_activity = _STATE_TO_VACUUM_STATE[event.state]
            self.async_write_ha_state()

        self._subscribe(self._capability.state.event, on_status)

        if self._capability.fan_speed:

            async def on_fan_speed(event: FanSpeedEvent) -> None:
                self._attr_fan_speed = get_name_key(event.speed)
                self.async_write_ha_state()

            self._subscribe(self._capability.fan_speed.event, on_fan_speed)

        if clean_count := self._capability.clean.count:

            async def on_clean_count(event: CleanCountEvent) -> None:
                self._clean_count = event.count

            self._subscribe(clean_count.event, on_clean_count)

        if map_caps := self._capability.map:

            async def on_rooms(event: RoomsEvent) -> None:
                self._room_event = event
                self._check_segments_changed()
                self.async_write_ha_state()

            self._subscribe(map_caps.rooms.event, on_rooms)

            async def on_map_info(event: CachedMapInfoEvent) -> None:
                self._maps = {map_obj.id: map_obj for map_obj in event.maps}
                self._check_segments_changed()

            self._subscribe(map_caps.cached_info.event, on_map_info)

        self.async_on_remove(self._extras.cloud.async_subscribe(self._on_path_change))
        self.async_on_remove(
            self._extras.fallback.async_subscribe(self._on_path_change)
        )

    @callback
    def _on_path_change(self) -> None:
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    # ------------------------------------------------------------ state

    def _use_local(self) -> bool:
        """Return True while the twin stands in for the cloud."""
        return not self._extras.cloud.connected and self._extras.fallback.ready

    @property
    @override
    def available(self) -> bool:
        """Available through the cloud or the Matter twin."""
        return self._attr_available or self._extras.fallback.ready

    @property
    @override
    def activity(self) -> VacuumActivity | None:
        """Return the activity; the twin's while the cloud is not reachable."""
        if self._use_local():
            return self._extras.fallback.activity
        return self._attr_activity

    def _language(self) -> str:
        return self.hass.config.language if self.hass else "en"

    @property
    @override
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return entity specific state attributes.

        Implemented by platform classes. Convention for attribute names
        is lowercase snake_case.
        """
        attributes: dict[str, Any] = {
            _ATTR_CONTROL_PATH: "local" if self._use_local() else "cloud",
        }
        if (local_entity := self._extras.fallback.entity_id) is not None:
            attributes[_ATTR_LOCAL_ENTITY] = local_entity
        if self._room_event is None:
            return attributes

        rooms: dict[str, Any] = {}
        language = self._language()
        for room in self._room_event.rooms:
            # convert room name to snake_case to meet the convention
            name = slugify(room_name(room, language))
            room_values = rooms.get(name)
            if room_values is None:
                rooms[name] = room.id
            elif isinstance(room_values, list):
                room_values.append(room.id)
            else:
                # Convert from int to list
                rooms[name] = [room_values, room.id]

        attributes[_ATTR_ROOMS] = rooms
        return attributes

    # ---------------------------------------------------------- routing

    async def _async_run(
        self,
        action: str,
        cloud: Callable[[float | None], Awaitable[T90Command | None]],
        local: Callable[[], LocalCall] | None,
        on_success: Callable[[T90Command], None] | None = None,
    ) -> None:
        """Run a command through the cloud, or through the twin if needed."""
        extras = self._extras
        fallback = extras.fallback
        if local is not None and self._use_local():
            await self._async_local(action, local)
            return

        timeout = (
            CLOUD_TIMEOUT_WITH_FALLBACK
            if local is not None and fallback.ready
            else None
        )
        command = await cloud(timeout)
        if command is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_send_command_not_supported",
                translation_placeholders={"command": action, "name": self._name()},
            )
        if command.outcome is CommandOutcome.OK:
            if on_success is not None:
                on_success(command)
            return
        if command.outcome.is_connection_problem:
            if local is not None and fallback.ready:
                _LOGGER.info(
                    "Ecovacs cloud cannot reach %s (%s); sending %s to %s",
                    self.entity_id,
                    command.outcome,
                    action,
                    fallback.entity_id,
                )
                await self._async_local(action, local)
                return
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="robot_unreachable",
                translation_placeholders={
                    "name": self._name(),
                    "reason": str(command.outcome),
                },
            )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_rejected",
            translation_placeholders={
                "command": command.NAME,
                "code": str(command.code),
            },
        )

    async def _async_local(self, action: str, local: Callable[[], LocalCall]) -> None:
        fallback = self._extras.fallback
        service, data = local()
        if not fallback.supports(service):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="local_fallback_unsupported",
                translation_placeholders={
                    "action": action,
                    "entity_id": str(fallback.entity_id),
                },
            )
        await fallback.async_call(service, data)

    def _name(self) -> str:
        info = self._device.device_info
        return info.get("nick") or info["name"]

    def _optimistic(self, state: State) -> Callable[[T90Command], None]:
        """Show the expected state at once and ask for the real one later."""

        def apply(_command: T90Command) -> None:
            self._device.events.notify(StateEvent(state))
            self._schedule_state_refresh()

        return apply

    def _schedule_state_refresh(self) -> None:
        self._cancel_state_refresh()

        @callback
        def refresh(_now: Any) -> None:
            self._state_refresh = None
            self._device.events.request_refresh(StateEvent)

        self._state_refresh = async_call_later(self.hass, STATE_REFRESH_DELAY, refresh)

    def _cancel_state_refresh(self) -> None:
        if self._state_refresh is not None:
            self._state_refresh()
            self._state_refresh = None

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending state refresh."""
        self._cancel_state_refresh()
        await super().async_will_remove_from_hass()

    def _clean(
        self, payloads: Payloads
    ) -> Callable[[float | None], Awaitable[T90Command | None]]:
        async def run(limit: float | None) -> T90Command | None:
            return await self._extras.async_run_clean(payloads, limit=limit)

        return run

    def _single(
        self, factory: Callable[[], T90Command]
    ) -> Callable[[float | None], Awaitable[T90Command | None]]:
        async def run(limit: float | None) -> T90Command | None:
            return await self._extras.async_execute(factory(), limit=limit)

        return run

    @staticmethod
    def _local(
        service: str, data: dict[str, Any] | None = None
    ) -> Callable[[], LocalCall]:
        return lambda: (service, data or {})

    async def _clean_command(self, action: CleanAction) -> None:
        local_service = {
            CleanAction.START: "start",
            CleanAction.RESUME: "start",
            CleanAction.PAUSE: "pause",
            CleanAction.STOP: "stop",
        }[action]
        state = {
            CleanAction.START: State.CLEANING,
            CleanAction.RESUME: State.CLEANING,
            CleanAction.PAUSE: State.PAUSED,
            CleanAction.STOP: State.IDLE,
        }[action]
        await self._async_run(
            local_service,
            self._clean(clean_action_payloads(action)),
            self._local(local_service),
            self._optimistic(state),
        )

    # --------------------------------------------------------- commands

    @override
    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set fan speed."""
        if TYPE_CHECKING:
            assert self._capability.fan_speed
        await self._device.execute_command(self._capability.fan_speed.set(fan_speed))

    @override
    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Set the vacuum cleaner to return to the dock."""

        def on_success(command: T90Command) -> None:
            state = (
                State.DOCKED
                if command.code == CHARGE_ALREADY_CHARGING
                else State.RETURNING
            )
            self._device.events.notify(StateEvent(state))
            self._schedule_state_refresh()

        await self._async_run(
            "return_to_base",
            self._single(charge_command),
            self._local("return_to_base"),
            on_success,
        )

    @override
    async def async_stop(self, **kwargs: Any) -> None:
        """Stop the vacuum cleaner."""
        await self._clean_command(CleanAction.STOP)

    @override
    async def async_pause(self) -> None:
        """Pause the vacuum cleaner."""
        await self._clean_command(CleanAction.PAUSE)

    @override
    async def async_start(self) -> None:
        """Start the vacuum cleaner, or resume a paused job."""
        state = self._device.events.get_last_event(StateEvent)
        paused = state is not None and state.state == State.PAUSED
        await self._clean_command(CleanAction.RESUME if paused else CleanAction.START)

    @override
    async def async_locate(self, **kwargs: Any) -> None:
        """Locate the vacuum cleaner."""
        await self._async_run(
            "locate", self._single(play_sound_command), self._local("locate")
        )

    async def async_clean_rooms(self, room_ids: Sequence[int]) -> None:
        """Clean rooms (deebot-client room ids) in this order."""
        await self._async_run(
            "clean_rooms",
            self._clean(rooms_payloads(room_ids, self._clean_count)),
            None,
            self._optimistic(State.CLEANING),
        )

    async def async_clean_zone(
        self, zones: Any, rooms: Any = None, **kwargs: Any
    ) -> None:
        """Clean rectangles (map millimetres) and optionally rooms in one job."""
        rectangles = _rectangles(zones)
        room_ids = _int_list(rooms, "rooms") if rooms else []
        payloads = zone_payloads(rectangles, room_ids, self._clean_count)
        await self._async_run(
            "clean_zone",
            self._clean(payloads),
            None,
            self._optimistic(State.CLEANING),
        )

    async def async_start_scenario(self, scenario_id: str, **kwargs: Any) -> None:
        """Start a saved scenario (quick command) of the app."""
        await self._async_run(
            "start_scenario",
            self._single(lambda: scenario_command(str(scenario_id))),
            None,
            self._optimistic(State.CLEANING),
        )

    @override
    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Send a command to a vacuum cleaner."""
        _LOGGER.debug("async_send_command %s with %s", command, params)
        if params is None:
            params = {}
        elif isinstance(params, list):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_send_command_params_dict",
            )

        if command in ["spot_area", "custom_area"]:
            key = "rooms" if command == "spot_area" else "coordinates"
            if key not in params:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="vacuum_send_command_params_required",
                    translation_placeholders={"command": command},
                )
            if self._capability.clean.action.area is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="vacuum_send_command_not_supported",
                    translation_placeholders={"command": command, "name": self._name()},
                )
            if "cleanings" in params:
                self._clean_count = int(params["cleanings"])
            if command == "spot_area":
                await self.async_clean_rooms(_int_list(params["rooms"], "rooms"))
            else:
                await self.async_clean_zone(params["coordinates"])
        else:
            await self._device.execute_command(
                self._capability.custom.set(command, params)
            )

    async def async_raw_get_positions(
        self,
    ) -> dict[str, Any]:
        """Get bot and chargers positions."""
        _LOGGER.debug("async_raw_get_positions")

        if not (map_cap := self._capability.map) or not (
            position_commands := map_cap.position.get
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_raw_get_positions_not_supported",
            )

        return await self._device.execute_command(position_commands[0])

    # --------------------------------------------------------- segments

    @callback
    def _check_segments_changed(self) -> None:
        """Check if segments have changed and create repair issue."""
        last_seen = self.last_seen_segments
        if last_seen is None:
            return

        last_seen_ids = {seg.id for seg in last_seen}
        current_ids = {seg.id for seg in self._get_segments()}

        if current_ids != last_seen_ids:
            self.async_create_segments_issue()

    def _get_segments(self) -> list[Segment]:
        """Get the segments that can be cleaned."""
        last_seen = self.last_seen_segments or []
        if self._room_event is None or not self._maps:
            # If we don't have the necessary information to
            # determine segments, return the last seen segments to
            # avoid temporarily losing all segments until we get
            # the necessary information, which could cause
            # unnecessary issues to be created
            return last_seen

        map_id = self._room_event.map_id
        if (map_obj := self._maps.get(map_id)) is None:
            _LOGGER.warning("Map ID %s not found in available maps", map_id)
            return []

        id_prefix = f"{map_id}{_SEGMENTS_SEPARATOR}"
        other_map_ids = {
            map_obj.id
            for map_obj in self._maps.values()
            if map_obj.id != self._room_event.map_id
        }
        # Include segments from the current map and any segments
        # from other maps that were previously seen, as we want
        # to continue showing segments from other maps for
        # mapping purposes
        segments = [
            seg for seg in last_seen if _split_composite_id(seg.id)[0] in other_map_ids
        ]
        language = self._language()
        segments.extend(
            Segment(
                id=f"{id_prefix}{room.id}",
                name=room_name(room, language),
                group=map_obj.name,
            )
            for room in self._room_event.rooms
        )
        return segments

    @override
    async def async_get_segments(self) -> list[Segment]:
        """Get the segments that can be cleaned."""
        return self._get_segments()

    @override
    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        """Perform an area clean.

        Only cleans segments from the currently selected map. While the cloud
        cannot reach the robot, the Home Assistant areas of the segments are
        cleaned by the Matter twin (both vacuums need an area mapping).
        """

        def local() -> LocalCall:
            fallback = self._extras.fallback
            try:
                areas = matter_area_ids(
                    self.hass, self.entity_id, str(fallback.entity_id), segment_ids
                )
            except LookupError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="local_fallback_area_not_mapped",
                    translation_placeholders={
                        "segment": str(err),
                        "entity_id": str(fallback.entity_id),
                    },
                ) from None
            return "clean_area", {"cleaning_area_id": areas}

        if self._use_local():
            await self._async_local("clean_area", local)
            return

        if not self._maps:
            _LOGGER.warning("No map information available, cannot clean segments")
            return

        valid_room_ids: list[int] = []
        for composite_id in segment_ids:
            map_id, segment_id = _split_composite_id(composite_id)
            if (map_obj := self._maps.get(map_id)) is None:
                _LOGGER.warning("Map ID %s not found in available maps", map_id)
                continue

            if not map_obj.using:
                room = next(
                    (
                        segment.name
                        for segment in self.last_seen_segments or []
                        if segment.id == composite_id
                    ),
                    "",
                )
                _LOGGER.warning(
                    'Map "%s" is not currently selected, skipping segment "%s" (%s)',
                    map_obj.name,
                    room,
                    segment_id,
                )
                continue

            valid_room_ids.append(int(segment_id))

        if not valid_room_ids:
            _LOGGER.warning(
                "No valid segments to clean after validation,"
                " skipping clean segments command"
            )
            return

        await self._async_run(
            "clean_area",
            self._clean(rooms_payloads(valid_room_ids, self._clean_count)),
            local,
            self._optimistic(State.CLEANING),
        )


@callback
def _split_composite_id(composite_id: str) -> tuple[str, str]:
    """Split a composite ID into its components."""
    map_id, _, segment_id = composite_id.partition(_SEGMENTS_SEPARATOR)
    return map_id, segment_id
