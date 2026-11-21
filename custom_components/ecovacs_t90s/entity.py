"""Ecovacs entity module.

``EcovacsEntity`` and friends are derived from the Home Assistant core
``ecovacs`` integration (Apache-2.0), see NOTICE. Changes: removed the legacy
(XMPP) entity, added ``EcovacsLiveViewEntity`` for the live view entities and
``EcovacsExtrasEntity`` plus helpers for the entities of extra features.
"""

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from deebot_client.capabilities import Capabilities
from deebot_client.device import Device
from deebot_client.events import AvailabilityEvent
from deebot_client.events.base import Event

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .commands.base import CommandOutcome, T90Command
from .const import DOMAIN, SIGNAL_FEATURE_SUPPORTED

if TYPE_CHECKING:
    from .commands.settings import SettingSpec
    from .extras import DeviceExtras
    from .live_view_runtime import LiveViewRuntime


def robot_device_info(device: Device) -> DeviceInfo:
    """Return the device registry info of a robot."""
    device_info = device.device_info
    info = DeviceInfo(
        identifiers={(DOMAIN, device_info["did"])},
        manufacturer="Ecovacs",
        serial_number=device_info["name"],
        model_id=device_info["class"],
    )

    if nick := device_info.get("nick"):
        info.name = nick

    if model := device_info.get("deviceName"):
        info.model = model

    # Unset until reported, so a stored version isn't cleared on startup
    if fw_version := device.fw_version:
        info.sw_version = fw_version

    if mac := device.mac:
        info.connections = {(dr.CONNECTION_NETWORK_MAC, mac)}

    return info


class EcovacsEntity[CapabilityEntityT](Entity):
    """Ecovacs entity."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _always_available: bool = False

    def __init__(
        self,
        device: Device,
        capability: CapabilityEntityT,
        **kwargs: Any,
    ) -> None:
        """Initialize entity."""
        super().__init__(**kwargs)
        self._attr_unique_id = (
            f"{device.device_info['did']}_{self.entity_description.key}"
        )

        self._device = device
        self._capability = capability
        self._subscribed_events: set[type[Event]] = set()

    @property
    @override
    def device_info(self) -> DeviceInfo | None:
        """Return device specific attributes."""
        return robot_device_info(self._device)

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        if not self._always_available:

            async def on_available(event: AvailabilityEvent) -> None:
                self._attr_available = event.available
                self.async_write_ha_state()

            self._subscribe(AvailabilityEvent, on_available)

    def _subscribe[EventT: Event](
        self,
        event_type: type[EventT],
        callback: Callable[[EventT], Coroutine[Any, Any, None]],
    ) -> None:
        """Subscribe to events."""
        self._subscribed_events.add(event_type)
        self.async_on_remove(self._device.events.subscribe(event_type, callback))

    async def async_update(self) -> None:
        """Update the entity.

        Only used by the generic entity update service.
        """
        for event_type in self._subscribed_events:
            self._device.events.request_refresh(event_type)


class EcovacsDescriptionEntity[CapabilityEntityT](EcovacsEntity[CapabilityEntityT]):
    """Ecovacs entity."""

    def __init__(
        self,
        device: Device,
        capability: CapabilityEntityT,
        entity_description: EntityDescription,
        **kwargs: Any,
    ) -> None:
        """Initialize entity."""
        self.entity_description = entity_description
        super().__init__(device, capability, **kwargs)


@dataclass(kw_only=True, frozen=True)
class EcovacsCapabilityEntityDescription[CapabilityEntityT](
    EntityDescription,
):
    """Ecovacs entity description."""

    capability_fn: Callable[[Capabilities], CapabilityEntityT | None]


class EcovacsLiveViewEntity(Entity):
    """Base for the live view entities of one robot.

    Attaches to the same device as the robot entities. Live view entities stay
    available while the robot is offline, as in ECOVACS Live View.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, device: Device, live_view: LiveViewRuntime, key: str) -> None:
        """Initialize the entity."""
        self._device = device
        self._live_view = live_view
        self.did: str = device.device_info["did"]
        self._attr_translation_key = key
        self._attr_unique_id = f"{self.did}_{key}"

    @property
    @override
    def device_info(self) -> DeviceInfo | None:
        """Return device specific attributes."""
        return robot_device_info(self._device)

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the live view state."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._live_view.manager.subscribe(self.async_write_ha_state)
        )


type FeatureFactory = Callable[[Device, DeviceExtras], list[Entity]]


@callback
def async_add_feature_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factories: Mapping[str, FeatureFactory],
) -> None:
    """Add entities of extra features as soon as the robot supports them."""
    controller = entry.runtime_data
    added: set[tuple[str, str]] = set()

    @callback
    def add(did: str, key: str) -> None:
        if (factory := factories.get(key)) is None or (did, key) in added:
            return
        if (device := controller.get_device(did)) is None:
            return
        added.add((did, key))
        if entities := factory(device, controller.extras[did]):
            async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_FEATURE_SUPPORTED.format(entry.entry_id), add
        )
    )
    for did, extras in controller.extras.items():
        for key in extras.supported_features():
            add(did, key)


def raise_for_outcome(command: T90Command, name: str) -> None:
    """Raise if a robot command did not succeed."""
    if command.outcome is CommandOutcome.OK:
        return
    if command.outcome.is_connection_problem:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="robot_unreachable",
            translation_placeholders={"name": name, "reason": str(command.outcome)},
        )
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="command_rejected",
        translation_placeholders={"command": command.NAME, "code": str(command.code)},
    )


class EcovacsExtrasEntity(EcovacsDescriptionEntity["DeviceExtras"]):
    """Entity of an extra feature, following one value of the extras runtime.

    Cloud-only: unavailable while deebot-client reports the robot unavailable.
    """

    _value_key: str
    _spec: SettingSpec | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the value."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._capability.async_subscribe(self._value_key, self._handle_value)
        )
        self._update_from_value(self._capability.values.get(self._value_key))

    @callback
    def _handle_value(self) -> None:
        self._update_from_value(self._capability.values.get(self._value_key))
        self.async_write_ha_state()

    def _update_from_value(self, value: Any) -> None:
        """Update the entity attributes from the value."""
        raise NotImplementedError

    @property
    def _robot_name(self) -> str:
        info = self._device.device_info
        return info.get("nick") or info["name"]

    async def async_update(self) -> None:
        """Read the setting again (generic entity update service)."""
        if self._spec is not None:
            await self._capability.async_refresh(self._spec)
