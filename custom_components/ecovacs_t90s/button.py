"""Ecovacs button module.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE. Changes: added the live view start/stop buttons (from ECOVACS Live
View, MIT), buttons that stop washing and drying the mops, and a button that
starts the scenario selected in the scenario select.
"""

from dataclasses import dataclass
from typing import Any, override

from deebot_client.capabilities import (
    CapabilityExecute,
    CapabilityExecuteTypes,
    CapabilityLifeSpan,
)
from deebot_client.commands import StationAction
from deebot_client.device import Device
from deebot_client.events import LifeSpan, StateEvent
from deebot_client.models import State

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .commands import settings
from .commands.clean import scenario_command, station_stop_command
from .const import DOMAIN, SUPPORTED_LIFESPANS, SUPPORTED_STATION_ACTIONS
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
class EcovacsButtonEntityDescription(
    ButtonEntityDescription,
    EcovacsCapabilityEntityDescription,
):
    """Ecovacs button entity description."""


@dataclass(kw_only=True, frozen=True)
class EcovacsLifespanButtonEntityDescription(ButtonEntityDescription):
    """Ecovacs lifespan button entity description."""

    component: LifeSpan


@dataclass(kw_only=True, frozen=True)
class EcovacsStationActionButtonEntityDescription(ButtonEntityDescription):
    """Ecovacs station action button entity description."""

    action: StationAction


@dataclass(kw_only=True, frozen=True)
class EcovacsStationStopButtonEntityDescription(ButtonEntityDescription):
    """Button that stops a running station action."""

    action: StationAction


ENTITY_DESCRIPTIONS: tuple[EcovacsButtonEntityDescription, ...] = (
    EcovacsButtonEntityDescription(
        capability_fn=lambda caps: caps.map.relocation if caps.map else None,
        key="relocate",
        translation_key="relocate",
        entity_category=EntityCategory.CONFIG,
    ),
)

STATION_ENTITY_DESCRIPTIONS = tuple(
    EcovacsStationActionButtonEntityDescription(
        action=action,
        key=f"station_action_{action.name.lower()}",
        translation_key=f"station_action_{action.name.lower()}",
    )
    for action in SUPPORTED_STATION_ACTIONS
)


# stationAction {"act": 4, "type": <type>} (app capture, deebot-client #2027)
STATION_STOP_ENTITY_DESCRIPTIONS: tuple[
    EcovacsStationStopButtonEntityDescription, ...
] = (
    EcovacsStationStopButtonEntityDescription(
        action=StationAction.WASH_MOP,
        key="station_stop_wash_mop",
        translation_key="station_stop_wash_mop",
    ),
    EcovacsStationStopButtonEntityDescription(
        action=StationAction.DRY_MOP,
        key="station_stop_dry_mop",
        translation_key="station_stop_dry_mop",
    ),
)


LIFESPAN_ENTITY_DESCRIPTIONS = tuple(
    EcovacsLifespanButtonEntityDescription(
        component=component,
        key=f"reset_lifespan_{component.name.lower()}",
        translation_key=f"reset_lifespan_{component.name.lower()}",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    )
    for component in SUPPORTED_LIFESPANS
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    entities: list[EcovacsEntity] = get_supported_entities(
        controller, EcovacsButtonEntity, ENTITY_DESCRIPTIONS
    )
    entities.extend(
        EcovacsResetLifespanButtonEntity(
            device, device.capabilities.life_span, description
        )
        for device in controller.devices
        for description in LIFESPAN_ENTITY_DESCRIPTIONS
        if description.component in device.capabilities.life_span.types
    )
    entities.extend(
        EcovacsStationActionButtonEntity(
            device, device.capabilities.station.action, description
        )
        for device in controller.devices
        if device.capabilities.station
        for description in STATION_ENTITY_DESCRIPTIONS
        if description.action in device.capabilities.station.action.types
    )
    # Only for robots whose station can wash/dry the mops
    entities.extend(
        EcovacsStationStopButtonEntity(
            device, controller.get_extras(device), description
        )
        for device in controller.devices
        if device.capabilities.station
        for description in STATION_STOP_ENTITY_DESCRIPTIONS
        if description.action in device.capabilities.station.action.types
    )
    async_add_entities(entities)

    async_add_feature_entities(
        hass,
        config_entry,
        async_add_entities,
        {
            settings.QUICK_COMMAND.key: lambda device, extras: [
                EcovacsStartScenarioButtonEntity(device, extras)
            ]
        },
    )

    if (live_view := controller.live_view) is not None:
        live_view_buttons: list[ButtonEntity] = []
        for device in controller.devices:
            live_view_buttons.append(
                EcovacsStartLiveViewButton(device, live_view, "start_live_view")
            )
            live_view_buttons.append(
                EcovacsStopLiveViewButton(device, live_view, "stop_live_view")
            )
        async_add_entities(live_view_buttons)


class EcovacsButtonEntity(
    EcovacsDescriptionEntity[CapabilityExecute],
    ButtonEntity,
):
    """Ecovacs button entity."""

    entity_description: EcovacsLifespanButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Press the button."""
        await self._device.execute_command(self._capability.execute())


class EcovacsResetLifespanButtonEntity(
    EcovacsDescriptionEntity[CapabilityLifeSpan],
    ButtonEntity,
):
    """Ecovacs reset lifespan button entity."""

    entity_description: EcovacsLifespanButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Press the button."""
        await self._device.execute_command(
            self._capability.reset(self.entity_description.component)
        )


class EcovacsStationActionButtonEntity(
    EcovacsDescriptionEntity[CapabilityExecuteTypes[StationAction]],
    ButtonEntity,
):
    """Ecovacs station action button entity."""

    entity_description: EcovacsStationActionButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Press the button."""
        await self._device.execute_command(
            self._capability.execute(self.entity_description.action)
        )


class EcovacsStartLiveViewButton(EcovacsLiveViewEntity, ButtonEntity):
    """Start the live view (from ECOVACS Live View)."""

    @override
    async def async_press(self) -> None:
        """Start the live view and wait for the first video frame."""
        await self._live_view.async_start(self.did)


class EcovacsStopLiveViewButton(EcovacsLiveViewEntity, ButtonEntity):
    """Stop the live view (from ECOVACS Live View)."""

    @override
    async def async_press(self) -> None:
        """Stop the live view."""
        await self._live_view.async_stop(self.did)


class EcovacsStationStopButtonEntity(
    EcovacsDescriptionEntity[DeviceExtras],
    ButtonEntity,
):
    """Stop washing or drying the mops."""

    entity_description: EcovacsStationStopButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Press the button."""
        command = await self._capability.async_execute(
            station_stop_command(self.entity_description.action.value)
        )
        info = self._device.device_info
        raise_for_outcome(command, info.get("nick") or info["name"])


class EcovacsStartScenarioButtonEntity(EcovacsExtrasEntity, ButtonEntity):
    """Start the scenario selected in the scenario select."""

    entity_description = ButtonEntityDescription(
        key="start_scenario", translation_key="start_scenario"
    )

    def __init__(self, device: Device, extras: DeviceExtras) -> None:
        """Initialize entity."""
        super().__init__(device, extras, self.entity_description)
        self._spec = settings.QUICK_COMMAND
        self._value_key = settings.QUICK_COMMAND.key

    def _update_from_value(self, value: Any) -> None:
        """Nothing to show."""

    @override
    async def async_press(self) -> None:
        """Start the selected scenario."""
        if not (scenario_id := self._capability.values.get("selected_scenario")):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="scenario_not_selected"
            )
        command = await self._capability.async_execute(scenario_command(scenario_id))
        raise_for_outcome(command, self._robot_name)
        self._device.events.notify(StateEvent(State.CLEANING))
