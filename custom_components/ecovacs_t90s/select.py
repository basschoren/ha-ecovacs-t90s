"""Ecovacs select entity module.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE. Changes: selects for the extra settings (wash water temperature,
drying duration, cleaning speed, edge mopping, TrueDetect sensitivity), the
saved scenarios and the active map of robots without the "major" map
capability, each created only when the robot supports it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, override

from deebot_client.capabilities import CapabilityMap, CapabilitySet, CapabilitySetTypes
from deebot_client.command import CommandWithMessageHandling
from deebot_client.device import Device
from deebot_client.events import EfficiencyModeEvent, WorkModeEvent, auto_empty
from deebot_client.events.base import Event
from deebot_client.events.map import CachedMapInfoEvent, MajorMapEvent
from deebot_client.events.water_info import WaterAmountEvent

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .commands import settings
from .commands.settings import Scenario, SettingSpec, parse_scenarios
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
    EcovacsExtrasEntity,
    async_add_feature_entities,
    raise_for_outcome,
)
from .extras import FEATURE_ACTIVE_MAP, VALUE_MAPS, DeviceExtras
from .util import get_name_key, get_supported_entities


@dataclass(kw_only=True, frozen=True)
class EcovacsSelectEntityDescription[EventT: Event](
    SelectEntityDescription,
    EcovacsCapabilityEntityDescription,
):
    """Ecovacs select entity description."""

    current_option_fn: Callable[[EventT], str | None]
    options_fn: Callable[[CapabilitySetTypes], list[str]]
    set_option_fn: Callable[[CapabilitySetTypes, str], CommandWithMessageHandling] = (
        lambda cap, option: cap.set(option)
    )


ENTITY_DESCRIPTIONS: tuple[EcovacsSelectEntityDescription, ...] = (
    EcovacsSelectEntityDescription[WaterAmountEvent](
        capability_fn=lambda caps: (
            caps.water.amount
            if caps.water and isinstance(caps.water.amount, CapabilitySetTypes)
            else None
        ),
        current_option_fn=lambda e: get_name_key(e.value),
        options_fn=lambda water: [get_name_key(amount) for amount in water.types],
        key="water_amount",
        translation_key="water_amount",
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSelectEntityDescription[WorkModeEvent](
        capability_fn=lambda caps: caps.clean.work_mode,
        current_option_fn=lambda e: get_name_key(e.mode),
        options_fn=lambda cap: [get_name_key(mode) for mode in cap.types],
        key="work_mode",
        translation_key="work_mode",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSelectEntityDescription[auto_empty.AutoEmptyEvent](
        capability_fn=lambda caps: caps.station.auto_empty if caps.station else None,
        current_option_fn=lambda e: get_name_key(e.frequency) if e.frequency else None,
        options_fn=lambda cap: [get_name_key(freq) for freq in cap.types],
        set_option_fn=lambda cap, option: cap.set(None, option),
        key="auto_empty",
        translation_key="auto_empty",
    ),
    EcovacsSelectEntityDescription[EfficiencyModeEvent](
        capability_fn=lambda caps: caps.settings.efficiency_mode,
        current_option_fn=lambda e: get_name_key(e.efficiency),
        options_fn=lambda cap: [get_name_key(mode) for mode in cap.types],
        key="efficiency_mode",
        translation_key="efficiency_mode",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
)


@dataclass(kw_only=True, frozen=True)
class EcovacsSettingSelectEntityDescription(SelectEntityDescription):
    """Select of an extra robot setting."""

    spec: SettingSpec
    option_fn: Callable[[Mapping[str, Any]], str | None]
    args_fn: Callable[[str, Mapping[str, Any]], dict[str, Any]]


SETTING_ENTITY_DESCRIPTIONS: tuple[EcovacsSettingSelectEntityDescription, ...] = (
    EcovacsSettingSelectEntityDescription(
        key="wash_temperature",
        translation_key="wash_temperature",
        spec=settings.WASH_INFO,
        options=list(settings.WASH_TEMPERATURE_MODES),
        option_fn=settings.wash_temperature_option,
        args_fn=settings.wash_temperature_args,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSettingSelectEntityDescription(
        key="drying_duration",
        translation_key="drying_duration",
        spec=settings.DRYING_DURATION,
        options=list(settings.DRYING_DURATIONS),
        option_fn=settings.drying_duration_option,
        args_fn=settings.drying_duration_args,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSettingSelectEntityDescription(
        key="cleaning_speed",
        translation_key="cleaning_speed",
        spec=settings.CUSTOM_AREA_MODE,
        options=list(settings.CLEANING_SPEEDS),
        option_fn=settings.cleaning_speed_option,
        args_fn=settings.cleaning_speed_args,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSettingSelectEntityDescription(
        key="edge_mopping",
        translation_key="edge_mopping",
        spec=settings.MOP_EXPAND,
        options=list(settings.MOP_EXTENSION_OPTIONS),
        option_fn=settings.mop_extension_option,
        args_fn=settings.mop_extension_args,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSettingSelectEntityDescription(
        key="true_detect_sensitivity",
        translation_key="true_detect_sensitivity",
        spec=settings.TRUE_DETECT_LEVEL,
        options=list(settings.TRUE_DETECT_LEVELS),
        option_fn=settings.true_detect_level_option,
        args_fn=settings.true_detect_level_args,
        entity_category=EntityCategory.CONFIG,
    ),
)


def _setting_selects(key: str, device: Device, extras: DeviceExtras) -> list[Entity]:
    return [
        EcovacsSettingSelectEntity(device, extras, description)
        for description in SETTING_ENTITY_DESCRIPTIONS
        if description.spec.key == key
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    entities = get_supported_entities(
        controller, EcovacsSelectEntity, ENTITY_DESCRIPTIONS
    )
    entities.extend(
        EcovacsActiveMapSelectEntity(device, map_cap, major)
        for device in controller.devices
        if (map_cap := device.capabilities.map)
        and isinstance(major := map_cap.major, CapabilitySet)
    )
    if entities:
        async_add_entities(entities)

    factories: dict[str, Callable[[Device, DeviceExtras], list[Entity]]] = {
        description.spec.key: partial(_setting_selects, description.spec.key)
        for description in SETTING_ENTITY_DESCRIPTIONS
    }
    factories[settings.QUICK_COMMAND.key] = lambda device, extras: [
        EcovacsScenarioSelectEntity(device, extras)
    ]
    factories[FEATURE_ACTIVE_MAP] = lambda device, extras: [
        EcovacsMapSlotSelectEntity(device, extras)
    ]
    async_add_feature_entities(hass, config_entry, async_add_entities, factories)


class EcovacsSelectEntity[EventT: Event](
    EcovacsDescriptionEntity[CapabilitySetTypes[EventT, [str], str]],
    SelectEntity,
):
    """Ecovacs select entity."""

    _attr_current_option: str | None = None
    entity_description: EcovacsSelectEntityDescription

    def __init__(
        self,
        device: Device,
        capability: CapabilitySetTypes[EventT, [str], str],
        entity_description: EcovacsSelectEntityDescription,
        **kwargs: Any,
    ) -> None:
        """Initialize entity."""
        super().__init__(device, capability, entity_description, **kwargs)
        self._attr_options = entity_description.options_fn(capability)

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: EventT) -> None:
            if (option := self.entity_description.current_option_fn(event)) is not None:
                self._attr_current_option = option
                self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)

    @override
    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self._device.execute_command(
            self.entity_description.set_option_fn(self._capability, option)
        )


class EcovacsActiveMapSelectEntity(
    EcovacsEntity[CapabilityMap],
    SelectEntity,
):
    """Ecovacs active map select entity."""

    entity_description = SelectEntityDescription(
        key="active_map",
        translation_key="active_map",
        entity_category=EntityCategory.CONFIG,
    )

    def __init__(
        self,
        device: Device,
        capability: CapabilityMap,
        major: CapabilitySet[MajorMapEvent, [str]],
        **kwargs: Any,
    ) -> None:
        """Initialize entity."""
        super().__init__(device, capability, **kwargs)
        self._major = major
        self._option_to_id: dict[str, str] = {}
        self._id_to_option: dict[str, str] = {}

        self._handle_on_cached_map(
            device.events.get_last_event(CachedMapInfoEvent)
            or CachedMapInfoEvent(set())
        )

    def _handle_on_cached_map(self, event: CachedMapInfoEvent) -> None:
        self._id_to_option.clear()
        self._option_to_id.clear()

        for map_info in event.maps:
            name = map_info.name or map_info.id
            self._id_to_option[map_info.id] = name
            self._option_to_id[name] = map_info.id

            if map_info.using:
                self._attr_current_option = name

        if self._attr_current_option not in self._option_to_id:
            self._attr_current_option = None

        # Sort named maps first, then numeric IDs
        # (unnamed maps during building) in ascending order.
        self._attr_options = sorted(
            self._option_to_id.keys(), key=lambda x: (x.isdigit(), x.lower())
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_cached_map(event: CachedMapInfoEvent) -> None:
            self._handle_on_cached_map(event)
            self.async_write_ha_state()

        self._subscribe(self._capability.cached_info.event, on_cached_map)

        async def on_major_map(event: MajorMapEvent) -> None:
            self._attr_current_option = self._id_to_option.get(event.map_id)
            self.async_write_ha_state()

        self._subscribe(self._major.event, on_major_map)

    @override
    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self._device.execute_command(self._major.set(self._option_to_id[option]))


class EcovacsSettingSelectEntity(EcovacsExtrasEntity, SelectEntity):
    """Select of an extra robot setting."""

    entity_description: EcovacsSettingSelectEntityDescription
    _attr_current_option: str | None = None

    def __init__(
        self,
        device: Device,
        extras: DeviceExtras,
        entity_description: EcovacsSettingSelectEntityDescription,
    ) -> None:
        """Initialize entity."""
        super().__init__(device, extras, entity_description)
        self._spec = entity_description.spec
        self._value_key = entity_description.spec.key

    def _update_from_value(self, value: Any) -> None:
        self._attr_current_option = (
            self.entity_description.option_fn(value)
            if isinstance(value, Mapping)
            else None
        )

    @override
    async def async_select_option(self, option: str) -> None:
        """Change the setting."""
        value = self._capability.values.get(self._value_key)
        current = value if isinstance(value, Mapping) else {}
        command = await self._capability.async_set(
            self.entity_description.spec,
            self.entity_description.args_fn(option, current),
        )
        raise_for_outcome(command, self._robot_name)


class EcovacsScenarioSelectEntity(EcovacsExtrasEntity, SelectEntity):
    """The saved scenarios of the app; the start button starts the selection."""

    entity_description = SelectEntityDescription(
        key="scenario", translation_key="scenario"
    )
    _attr_current_option: str | None = None

    def __init__(self, device: Device, extras: DeviceExtras) -> None:
        """Initialize entity."""
        super().__init__(device, extras, self.entity_description)
        self._spec = settings.QUICK_COMMAND
        self._value_key = settings.QUICK_COMMAND.key
        self._by_option: dict[str, Scenario] = {}
        # The options are needed when the entity is registered
        self._update_from_value(extras.values.get(self._value_key))

    def _update_from_value(self, value: Any) -> None:
        self._by_option = {}
        for scenario in parse_scenarios(value):
            option = scenario.name
            if option in self._by_option:
                option = f"{scenario.name} ({scenario.id})"
            self._by_option[option] = scenario
        self._attr_options = list(self._by_option)
        if self._attr_current_option not in self._by_option:
            self._attr_current_option = next(iter(self._by_option), None)
        self._store_selection()

    def _store_selection(self) -> None:
        scenario = self._by_option.get(self._attr_current_option or "")
        self._capability.values["selected_scenario"] = scenario.id if scenario else None

    @override
    async def async_select_option(self, option: str) -> None:
        """Select a scenario (nothing is sent to the robot)."""
        self._attr_current_option = option
        self._store_selection()
        self.async_write_ha_state()


class EcovacsMapSlotSelectEntity(EcovacsExtrasEntity, SelectEntity):
    """Active map for robots without the deebot-client "major" capability."""

    entity_description = SelectEntityDescription(
        key="active_map",
        translation_key="active_map",
        entity_category=EntityCategory.CONFIG,
    )
    _attr_current_option: str | None = None

    def __init__(self, device: Device, extras: DeviceExtras) -> None:
        """Initialize entity."""
        super().__init__(device, extras, self.entity_description)
        self._value_key = VALUE_MAPS
        self._option_to_id: dict[str, str] = {}
        # The options are needed when the entity is registered
        self._update_from_value(None)

    def _update_from_value(self, value: Any) -> None:
        extras = self._capability
        self._option_to_id = {}
        for map_info in extras.maps:
            option = map_info.name or map_info.id
            if option in self._option_to_id:
                option = f"{option} ({map_info.id})"
            self._option_to_id[option] = map_info.id
        self._attr_options = list(self._option_to_id)
        self._attr_current_option = next(
            (
                option
                for option, map_id in self._option_to_id.items()
                if map_id == extras.active_map_id
            ),
            None,
        )

    @override
    async def async_select_option(self, option: str) -> None:
        """Switch the active map."""
        command = await self._capability.async_execute(
            settings.set_active_map_command(self._option_to_id[option])
        )
        raise_for_outcome(command, self._robot_name)
        self._device.events.request_refresh(CachedMapInfoEvent)

    async def async_update(self) -> None:
        """Ask for the maps again."""
        self._device.events.request_refresh(CachedMapInfoEvent)
