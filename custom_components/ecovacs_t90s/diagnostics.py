"""Ecovacs diagnostics.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE. Changes: also redacts the client device ID and the Live View PIN,
reports unsupported robots, the device class mappings, the live view state
and the state of the extra features (probes, settings, furniture, cloud
connection, local fallback).
"""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant

from . import EcovacsConfigEntry
from .const import (
    CONF_ACCOUNT_CREDENTIALS,
    CONF_LIVE_VIEW_PIN,
    CONF_OVERRIDE_MQTT_URL,
    CONF_OVERRIDE_REST_URL,
)

REDACT_CONFIG = {
    CONF_ACCOUNT_CREDENTIALS,
    CONF_DEVICE_ID,
    CONF_LIVE_VIEW_PIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    "title",
    "unique_id",
    CONF_OVERRIDE_MQTT_URL,
    CONF_OVERRIDE_REST_URL,
}
REDACT_DEVICE = {"did", CONF_NAME, "homeId", "nick", "resource", "service"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: EcovacsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    controller = config_entry.runtime_data
    diag: dict[str, Any] = {
        "config": async_redact_data(config_entry.as_dict(), REDACT_CONFIG)
    }

    diag["devices"] = [
        async_redact_data(device.device_info, REDACT_DEVICE)
        for device in controller.devices
    ]
    diag["not_supported_devices"] = [
        async_redact_data(device, REDACT_DEVICE) for device in controller.not_supported
    ]
    diag["device_class_mappings"] = controller.device_class_status
    diag["live_view"] = (
        controller.live_view.diagnostics() if controller.live_view else None
    )
    # Per robot, in the order of "devices"; no serial numbers or robot ids
    diag["extras"] = [
        extras.diagnostics() if (extras := controller.extras.get(did)) else None
        for did in (device.device_info["did"] for device in controller.devices)
    ]

    return diag
