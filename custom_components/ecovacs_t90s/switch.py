"""Ecovacs switch module.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE. Changes: added the live view switch (a local addition to ECOVACS
Live View, MIT); switches for deebot-client capabilities the core integration
has no entity for (voice assistant, mop-only mode, automatic firmware
updates) and for do not disturb, created only when the robot supports them.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, override

from deebot_client.capabilities import CapabilitySet, CapabilitySetEnable
from deebot_client.command import CommandWithMessageHandling
from deebot_client.device import Device
from deebot_client.events import EnableEvent, OtaEvent
from deebot_client.events.auto_empty import AutoEmptyEvent

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .commands import settings
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
    EcovacsExtrasEntity,
    EcovacsLiveViewEntity,
    async_add_feature_entities,
    raise_for_outcome,
)
from .extras import DeviceExtras
from .util import get_supported_entities


@dataclass(kw_only=True, frozen=True)
class EcovacsSwitchEntityDescription(
    SwitchEntityDescription,
    EcovacsCapabilityEntityDescription,
):
    """Ecovacs switch entity description."""

    set_fn: Callable[[CapabilitySet, bool], CommandWithMessageHandling] = (
        lambda cap, enabled: cap.set(enabled)
    )


ENTITY_DESCRIPTIONS: tuple[EcovacsSwitchEntityDescription, ...] = (
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.advanced_mode,
        key="advanced_mode",
        translation_key="advanced_mode",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.clean.continuous,
        key="continuous_cleaning",
        translation_key="continuous_cleaning",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.carpet_auto_fan_boost,
        key="carpet_auto_fan_boost",
        translation_key="carpet_auto_fan_boost",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.clean.preference,
        key="clean_preference",
        translation_key="clean_preference",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.true_detect,
        key="true_detect",
        translation_key="true_detect",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.border_switch,
        key="border_switch",
        translation_key="border_switch",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.child_lock,
        key="child_lock",
        translation_key="child_lock",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.moveup_warning,
        key="move_up_warning",
        translation_key="move_up_warning",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.cross_map_border_warning,
        key="cross_map_border_warning",
        translation_key="cross_map_border_warning",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.safe_protect,
        key="safe_protect",
        translation_key="safe_protect",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.border_spin,
        key="border_spin",
        translation_key="border_spin",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.station.auto_empty if c.station else None,
        # Passing no frequency keeps the one set with the auto_empty select
        set_fn=lambda cap, enabled: cap.set(enabled, None),
        key="auto_empty",
        translation_key="auto_empty",
        entity_category=EntityCategory.CONFIG,
    ),
)


# deebot-client capabilities without an entity in the core integration; the
# key is the probe key (commands/settings.py), so they are only created when
# the robot answers the get command
PROBED_ENTITY_DESCRIPTIONS: tuple[EcovacsSwitchEntityDescription, ...] = (
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.voice_assistant,
        key=settings.VOICE_ASSISTANT.key,
        translation_key="voice_assistant",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.sweep_mode,
        key=settings.SWEEP_MODE.key,
        translation_key="mop_only",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    entities: list[EcovacsEntity] = get_supported_entities(
        controller, EcovacsSwitchEntity, ENTITY_DESCRIPTIONS
    )
    if entities:
        async_add_entities(entities)

    if (live_view := controller.live_view) is not None:
        async_add_entities(
            EcovacsLiveViewSwitch(device, live_view, "live_view")
            for device in controller.devices
        )

    def capability_switch(
        description: EcovacsSwitchEntityDescription,
    ) -> Callable[[Device, DeviceExtras], list[Entity]]:
        def create(device: Device, extras: DeviceExtras) -> list[Entity]:
            capability = description.capability_fn(device.capabilities)
            return (
                [EcovacsSwitchEntity(device, capability, description)]
                if capability
                else []
            )

        return create

    def ota_switch(device: Device, extras: DeviceExtras) -> list[Entity]:
        capability = device.capabilities.settings.ota
        if not isinstance(capability, CapabilitySetEnable):
            return []
        return [EcovacsOtaSwitchEntity(device, capability)]

    factories: dict[str, Callable[[Device, DeviceExtras], list[Entity]]] = {
        description.key: capability_switch(description)
        for description in PROBED_ENTITY_DESCRIPTIONS
    }
    factories[settings.OTA.key] = ota_switch
    factories[settings.BLOCK.key] = lambda device, extras: [
        EcovacsDoNotDisturbSwitchEntity(device, extras)
    ]
    async_add_feature_entities(hass, config_entry, async_add_entities, factories)


class EcovacsSwitchEntity(
    EcovacsDescriptionEntity[CapabilitySet[EnableEvent | AutoEmptyEvent, ...]],
    SwitchEntity,
):
    """Ecovacs switch entity."""

    entity_description: EcovacsSwitchEntityDescription

    _attr_is_on = False

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: EnableEvent | AutoEmptyEvent) -> None:
            self._attr_is_on = event.enabled
            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self._device.execute_command(
            self.entity_description.set_fn(self._capability, True)
        )

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self._device.execute_command(
            self.entity_description.set_fn(self._capability, False)
        )


class EcovacsLiveViewSwitch(EcovacsLiveViewEntity, SwitchEntity):
    """Turn the live view of one robot on or off.

    A local addition to ECOVACS Live View. The switch follows the session
    state, so it also turns off when the automatic shutdown ends the session.
    """

    @property
    @override
    def is_on(self) -> bool:
        """Return whether the live view is starting or running."""
        manager = self._live_view.manager
        return (
            manager.is_streaming(self.did)
            or manager.status.get(self.did) == "requesting_session"
        )

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the live view without blocking the action call."""
        if self.is_on:
            return
        # Bound to the config entry, so an unload cancels a pending start
        self.platform.config_entry.async_create_background_task(
            self.hass,
            self._live_view.async_start(self.did),
            name=f"ecovacs_t90s_live_view_start_{self.entity_id}",
        )

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the live view."""
        await self._live_view.async_stop(self.did)


class EcovacsOtaSwitchEntity(
    EcovacsEntity[CapabilitySetEnable[OtaEvent]],
    SwitchEntity,
):
    """Automatic firmware updates."""

    entity_description = SwitchEntityDescription(
        key="ota_auto_update",
        translation_key="ota_auto_update",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    )
    _attr_is_on: bool | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: OtaEvent) -> None:
            if event.auto_enabled is not None:
                self._attr_is_on = bool(event.auto_enabled)
            self._attr_extra_state_attributes = {
                "firmware_version": event.version,
                "update_status": event.status,
                "update_progress": event.progress,
            }
            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn automatic updates on."""
        await self._device.execute_command(self._capability.set(True))

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn automatic updates off."""
        await self._device.execute_command(self._capability.set(False))


class EcovacsDoNotDisturbSwitchEntity(EcovacsExtrasEntity, SwitchEntity):
    """Do not disturb (setBlock); the times are kept as set in the app."""

    entity_description = SwitchEntityDescription(
        key="do_not_disturb",
        translation_key="do_not_disturb",
        entity_category=EntityCategory.CONFIG,
    )
    _attr_is_on: bool | None = None

    def __init__(self, device: Device, extras: DeviceExtras) -> None:
        """Initialize entity."""
        super().__init__(device, extras, self.entity_description)
        self._spec = settings.BLOCK
        self._value_key = settings.BLOCK.key

    def _update_from_value(self, value: Any) -> None:
        if not isinstance(value, Mapping):
            self._attr_is_on = None
            return
        enable = value.get("enable")
        self._attr_is_on = bool(enable) if enable is not None else None
        self._attr_extra_state_attributes = {
            "start": value.get("start"),
            "end": value.get("end"),
        }

    async def _async_set(self, enable: bool) -> None:
        value = self._capability.values.get(self._value_key)
        current = value if isinstance(value, Mapping) else {}
        command = await self._capability.async_set(
            settings.BLOCK, settings.block_args(enable, current)
        )
        raise_for_outcome(command, self._robot_name)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn do not disturb on."""
        await self._async_set(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn do not disturb off."""
        await self._async_set(False)
