"""Local fallback through the robot's Matter twin.

Recent Ecovacs robots can also be paired with Home Assistant over Matter. The
``matter`` integration then creates a second device with its own ``vacuum``
entity, which works on the local network without the Ecovacs cloud.

The twin is found by serial number: deebot-client's device record carries the
serial in ``name`` (the core integration stores it as the device's
``serial_number``), the ``matter`` integration stores the Matter
BasicInformation SerialNumber as ``serial_number`` and as the identifier
``("matter", "serial_<serial>")``. Both were checked to be the same on a
DEEBOT T90S PRO OMNI. Matching ignores case and anything but letters and
digits, and accepts one serial ending with the other (at least 10
characters), for firmware that adds a prefix.

This integration does not depend on ``matter``: when no twin exists, nothing
changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import logging
import re
from typing import Any

from homeassistant.components.vacuum import (
    DOMAIN as VACUUM_DOMAIN,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)

MATTER_DOMAIN = "matter"
_MATTER_SERIAL_PREFIX = "serial_"
_MIN_SERIAL_LENGTH = 8
_MIN_SUFFIX_LENGTH = 10

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_FOUND = "not_found"

SERVICE_FEATURES: dict[str, VacuumEntityFeature] = {
    "start": VacuumEntityFeature.START,
    "pause": VacuumEntityFeature.PAUSE,
    "stop": VacuumEntityFeature.STOP,
    "return_to_base": VacuumEntityFeature.RETURN_HOME,
    "locate": VacuumEntityFeature.LOCATE,
    "clean_area": VacuumEntityFeature.CLEAN_AREA,
}


def normalize_serial(value: Any) -> str:
    """Return the serial in upper case without separators."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^0-9A-Z]", "", value.upper())


def serials_match(first: Any, second: Any) -> bool:
    """Return True if two serial numbers belong to the same robot."""
    a, b = normalize_serial(first), normalize_serial(second)
    if len(a) < _MIN_SERIAL_LENGTH or len(b) < _MIN_SERIAL_LENGTH:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= _MIN_SUFFIX_LENGTH and longer.endswith(shorter)


def _matter_serials(device: dr.DeviceEntry) -> list[str]:
    """Return the serial numbers a Matter device entry carries."""
    serials = [device.serial_number] if device.serial_number else []
    serials.extend(
        identifier.removeprefix(_MATTER_SERIAL_PREFIX)
        for domain, identifier in device.identifiers
        if domain == MATTER_DOMAIN and identifier.startswith(_MATTER_SERIAL_PREFIX)
    )
    return serials


def find_matter_twin(
    hass: HomeAssistant, serial: str | None
) -> tuple[dr.DeviceEntry, str | None] | None:
    """Return the Matter device with this serial and its vacuum entity id."""
    if not normalize_serial(serial):
        return None
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    for device in device_registry.devices:
        if not any(domain == MATTER_DOMAIN for domain, _ in device.identifiers):
            continue
        if not any(serials_match(serial, other) for other in _matter_serials(device)):
            continue
        vacuum = next(
            (
                entry.entity_id
                for entry in er.async_entries_for_device(entity_registry, device.id)
                if entry.domain == VACUUM_DOMAIN and entry.platform == MATTER_DOMAIN
            ),
            None,
        )
        return device, vacuum
    return None


def matter_area_ids(
    hass: HomeAssistant,
    own_entity_id: str,
    matter_entity_id: str,
    segment_ids: Sequence[str],
) -> list[str]:
    """Return the Home Assistant areas that clean the segments on the twin.

    Both vacuum entities map Home Assistant areas to their own segments
    (``vacuum.clean_area``), so the area is the common key. Raises LookupError
    with the segment id that cannot be mapped.
    """
    entity_registry = er.async_get(hass)

    def mapping(entity_id: str) -> Mapping[str, Sequence[str]]:
        entry = entity_registry.async_get(entity_id)
        options = entry.options.get(VACUUM_DOMAIN, {}) if entry else {}
        return options.get("area_mapping") or {}

    own_mapping = mapping(own_entity_id)
    twin_mapping = mapping(matter_entity_id)
    areas: list[str] = []
    for segment_id in segment_ids:
        area = next(
            (area for area, segments in own_mapping.items() if segment_id in segments),
            None,
        )
        if area is None or not twin_mapping.get(area):
            raise LookupError(segment_id)
        if area not in areas:
            areas.append(area)
    return areas


class LocalFallback:
    """The Matter twin of one robot."""

    def __init__(self, hass: HomeAssistant, serial: str | None) -> None:
        """Initialize."""
        self._hass = hass
        self._serial = serial
        self.device_id: str | None = None
        self.entity_id: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._state_unsub: CALLBACK_TYPE | None = None
        self._last_status: str | None = None

    @property
    def status(self) -> str:
        """Return available, unavailable (twin without usable vacuum) or not_found."""
        if self.device_id is None:
            return STATUS_NOT_FOUND
        if self.entity_id is None:
            return STATUS_UNAVAILABLE
        state = self._hass.states.get(self.entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            return STATUS_UNAVAILABLE
        return STATUS_AVAILABLE

    @property
    def ready(self) -> bool:
        """Return True if commands can be sent to the twin."""
        return self.status == STATUS_AVAILABLE

    @property
    def activity(self) -> VacuumActivity | None:
        """Return the activity the twin reports."""
        if self.entity_id is None:
            return None
        state = self._hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return VacuumActivity(state.state)
        except ValueError:
            return None

    def supports(self, service: str) -> bool:
        """Return True if the twin supports the vacuum service."""
        if self.entity_id is None or (feature := SERVICE_FEATURES.get(service)) is None:
            return False
        state = self._hass.states.get(self.entity_id)
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0) if state else 0
        return bool(int(features or 0) & feature)

    @callback
    def async_start(self) -> None:
        """Find the twin and follow registry changes."""

        @callback
        def on_registry_update(_event: Event[Any]) -> None:
            self.async_rescan()

        self._unsubs.extend(
            (
                self._hass.bus.async_listen(
                    dr.EVENT_DEVICE_REGISTRY_UPDATED, on_registry_update
                ),
                self._hass.bus.async_listen(
                    er.EVENT_ENTITY_REGISTRY_UPDATED, on_registry_update
                ),
            )
        )
        self.async_rescan()

    @callback
    def async_stop(self) -> None:
        """Stop following the registries and the twin."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None
        self._listeners.clear()

    @callback
    def async_subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call listener when the twin or its state changes."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @callback
    def async_rescan(self) -> None:
        """Look for the twin again."""
        found = find_matter_twin(self._hass, self._serial)
        device_id, entity_id = (found[0].id, found[1]) if found else (None, None)
        if (device_id, entity_id) != (self.device_id, self.entity_id):
            if device_id is not None and self.device_id is None:
                _LOGGER.info(
                    "Found the Matter twin of the robot: %s", entity_id or device_id
                )
            self.device_id, self.entity_id = device_id, entity_id
            self._track_state()
        self._notify_if_changed(force=False)

    def _track_state(self) -> None:
        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None
        if self.entity_id is None:
            return

        @callback
        def on_state(_event: Event[Any]) -> None:
            self._notify_if_changed(force=True)

        self._state_unsub = async_track_state_change_event(
            self._hass, [self.entity_id], on_state
        )

    def _notify_if_changed(self, *, force: bool) -> None:
        status = self.status
        if not force and status == self._last_status:
            return
        self._last_status = status
        for listener in list(self._listeners):
            listener()

    async def async_call(
        self, service: str, data: Mapping[str, Any] | None = None
    ) -> None:
        """Call a vacuum service of the twin."""
        if self.entity_id is None:
            raise LookupError("no Matter twin")
        await self._hass.services.async_call(
            VACUUM_DOMAIN,
            service,
            {ATTR_ENTITY_ID: self.entity_id, **(data or {})},
            blocking=True,
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return the state for diagnostics (no serial numbers)."""
        return {
            "status": self.status,
            "matter_entity_id": self.entity_id,
            "supported_services": [
                service for service in SERVICE_FEATURES if self.supports(service)
            ],
        }
