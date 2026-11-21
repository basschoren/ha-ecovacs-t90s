"""Ecovacs DEEBOT T90S integration.

Combines the Home Assistant core ``ecovacs`` integration (Apache-2.0) with the
camera live view of ECOVACS Live View (MIT), see NOTICE.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .controller import EcovacsController
from .live_view_runtime import LiveViewRuntime
from .services import async_setup_services

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.LAWN_MOWER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.VACUUM,
]
type EcovacsConfigEntry = ConfigEntry[EcovacsController]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the component."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EcovacsConfigEntry) -> None:
    """Set up this integration using UI."""
    controller = EcovacsController(hass, entry)

    entry.async_on_unload(controller.teardown)

    await controller.initialize()

    controller.live_view = LiveViewRuntime(hass, controller, entry.options)
    entry.runtime_data = controller

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


async def _async_entry_updated(hass: HomeAssistant, entry: EcovacsConfigEntry) -> None:
    """Apply changed live view options.

    The entry is also updated when deebot-client renews the stored account
    credentials, so this must not reload the entry.
    """
    if (live_view := entry.runtime_data.live_view) is not None:
        live_view.update_options(entry.options)


async def async_unload_entry(hass: HomeAssistant, entry: EcovacsConfigEntry) -> None:
    """Unload config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
