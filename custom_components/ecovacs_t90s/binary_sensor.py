"""Binary sensor module.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE. Changes: removed the legacy (XMPP) entities, added the cloud
connection sensor.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from deebot_client.capabilities import CapabilityEvent
from deebot_client.events import Event
from deebot_client.events.water_info import MopAttachedEvent

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
)
from .extras import DeviceExtras
from .util import get_supported_entities


@dataclass(kw_only=True, frozen=True)
class EcovacsBinarySensorEntityDescription[EventT: Event](
    BinarySensorEntityDescription,
    EcovacsCapabilityEntityDescription,
):
    """Class describing Deebot binary sensor entity."""

    value_fn: Callable[[EventT], bool | None]


ENTITY_DESCRIPTIONS: tuple[EcovacsBinarySensorEntityDescription, ...] = (
    EcovacsBinarySensorEntityDescription[MopAttachedEvent](
        capability_fn=lambda caps: caps.water.mop_attached if caps.water else None,
        value_fn=lambda e: e.value,
        key="water_mop_attached",
        translation_key="water_mop_attached",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    async_add_entities(
        get_supported_entities(controller, EcovacsBinarySensor, ENTITY_DESCRIPTIONS)
    )
    async_add_entities(
        EcovacsCloudConnectedBinarySensor(device, controller.get_extras(device))
        for device in controller.devices
    )


class EcovacsBinarySensor[EventT: Event](
    EcovacsDescriptionEntity[CapabilityEvent[EventT]],
    BinarySensorEntity,
):
    """Ecovacs binary sensor."""

    entity_description: EcovacsBinarySensorEntityDescription

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: EventT) -> None:
            self._attr_is_on = self.entity_description.value_fn(event)
            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)


class EcovacsCloudConnectedBinarySensor(
    EcovacsEntity[DeviceExtras],
    BinarySensorEntity,
):
    """Whether the robot can be reached through the Ecovacs cloud.

    See connectivity.py for how this is determined.
    """

    _always_available = True
    _unrecorded_attributes = frozenset({"last_mqtt_message", "last_success"})
    entity_description = BinarySensorEntityDescription(
        key="cloud_connected",
        translation_key="cloud_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the connection state."""
        await super().async_added_to_hass()
        self.async_on_remove(self._capability.cloud.async_subscribe(self._handle))

    @callback
    def _handle(self) -> None:
        self.async_write_ha_state()

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the cloud can reach the robot."""
        return self._capability.cloud.connected

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details of the connection state."""
        cloud = self._capability.cloud
        last_mqtt = self.platform.config_entry.runtime_data.mqtt_last_message
        return {
            "robot_available": cloud.robot_available,
            "failure_reason": cloud.failure_reason,
            "last_success": cloud.last_success.isoformat()
            if cloud.last_success
            else None,
            "last_mqtt_message": last_mqtt.isoformat() if last_mqtt else None,
        }
