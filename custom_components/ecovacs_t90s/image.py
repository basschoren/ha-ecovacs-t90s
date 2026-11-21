"""Ecovacs image entities.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE. Changes: the map shows the furniture and carpets the robot knows
(``map_overlay.py``, option "show furniture on map") and lists them in the
``furniture`` attribute; new image entity with the map of the last clean job.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast, override

from deebot_client.capabilities import CapabilityMap
from deebot_client.device import Device
from deebot_client.events import CleanLogEntry, CleanLogEvent
from deebot_client.events.map import CachedMapInfoEvent, MapChangedEvent
from deebot_client.map import Map

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EcovacsConfigEntry
from .entity import EcovacsEntity, EcovacsExtrasEntity, async_add_feature_entities
from .extras import FEATURE_CLEAN_LOG_IMAGE, FEATURE_FURNITURE, DeviceExtras
from .map_overlay import overlay_furniture

_ATTR_FURNITURE = "furniture"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data

    def show_furniture() -> bool:
        return controller.show_furniture

    entities = [
        EcovacsMap(device, caps, hass, controller.get_extras(device), show_furniture)
        for device in controller.devices
        if (caps := device.capabilities.map)
    ]

    if entities:
        async_add_entities(entities)

    async_add_feature_entities(
        hass,
        config_entry,
        async_add_entities,
        {
            FEATURE_CLEAN_LOG_IMAGE: lambda device, extras: [
                EcovacsLastCleanMapImage(device, extras, hass)
            ]
        },
    )


class EcovacsMap(
    EcovacsEntity[CapabilityMap],
    ImageEntity,
):
    """Ecovacs map."""

    _attr_content_type = "image/svg+xml"
    _unrecorded_attributes = frozenset({_ATTR_FURNITURE})

    def __init__(
        self,
        device: Device,
        capability: CapabilityMap,
        hass: HomeAssistant,
        extras: DeviceExtras,
        show_furniture: Callable[[], bool],
    ) -> None:
        """Initialize entity."""
        super().__init__(device, capability, hass=hass)
        self._attr_extra_state_attributes = {}
        self._map = cast(Map, self._device.map)
        self._extras = extras
        self._show_furniture = show_furniture

    entity_description = EntityDescription(
        key="map",
        translation_key="map",
    )

    @override
    def image(self) -> bytes | None:
        """Return bytes of image or None."""
        if svg := self._map.get_svg_map():
            if self._show_furniture() and (items := self._extras.active_furniture):
                language = self.hass.config.language.split("-", maxsplit=1)[0].lower()
                svg = overlay_furniture(svg, items, self._extras.rotation, language)
            return svg.encode()

        return None

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_info(event: CachedMapInfoEvent) -> None:
            for map_obj in event.maps:
                if map_obj.using:
                    self._attr_extra_state_attributes["map_name"] = map_obj.name

        async def on_changed(event: MapChangedEvent) -> None:
            self._attr_image_last_updated = event.when
            self.async_write_ha_state()

        self._subscribe(self._capability.cached_info.event, on_info)
        self._subscribe(self._capability.changed.event, on_changed)
        self.async_on_remove(
            self._extras.async_subscribe(FEATURE_FURNITURE, self._on_furniture)
        )
        self._update_furniture_attribute()

    def _update_furniture_attribute(self) -> None:
        items = self._extras.active_furniture
        if items or self._extras.supported(FEATURE_FURNITURE):
            self._attr_extra_state_attributes[_ATTR_FURNITURE] = [
                item.as_dict() for item in items
            ]

    @callback
    def _on_furniture(self) -> None:
        self._update_furniture_attribute()
        # A new timestamp makes the frontend load the image again
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @override
    async def async_update(self) -> None:
        """Update the entity.

        Only used by the generic entity update service.
        """
        await super().async_update()
        self._map.refresh()
        await self._extras.async_refresh_furniture()


class EcovacsLastCleanMapImage(EcovacsExtrasEntity, ImageEntity):
    """Map image of the last clean job, as the Ecovacs cloud stores it."""

    entity_description = EntityDescription(
        key="last_clean_map", translation_key="last_clean_map"
    )
    _value_key = FEATURE_CLEAN_LOG_IMAGE
    _attr_image_url: str | None = None

    def __init__(
        self, device: Device, extras: DeviceExtras, hass: HomeAssistant
    ) -> None:
        """Initialize entity."""
        super().__init__(device, extras, self.entity_description, hass=hass)

    def _update_from_value(self, value: Any) -> None:
        if not isinstance(value, CleanLogEntry):
            return
        if value.image_url != self._attr_image_url:
            self._cached_image = None
        self._attr_image_url = value.image_url
        self._attr_image_last_updated = datetime.fromtimestamp(value.timestamp, UTC)
        self._attr_extra_state_attributes = {
            "area": value.area,
            "duration": value.duration,
            "type": value.type,
            "stop_reason": value.stop_reason.name.lower(),
        }

    async def async_update(self) -> None:
        """Ask for the clean logs again."""
        self._device.events.request_refresh(CleanLogEvent)
