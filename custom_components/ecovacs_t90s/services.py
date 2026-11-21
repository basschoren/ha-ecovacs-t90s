"""Ecovacs services.

``raw_get_positions`` is from the Home Assistant core ``ecovacs`` integration
(Apache-2.0), see NOTICE. New: ``raw_command``, a debug service that sends an
arbitrary JSON command to a robot and returns the raw answer; ``clean_zone``
(rectangles and rooms in one job) and ``start_scenario`` (saved scenario of
the app) for the vacuum entity.
"""

import asyncio
import logging
import time
from typing import Any

from deebot_client.commands.json.custom import CustomCommand
from deebot_client.device import Device
from deebot_client.event_bus import EventBus
from deebot_client.message import HandlingResult
from deebot_client.models import ApiDeviceInfo
import probatio

from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    service,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_RAW_GET_POSITIONS = "raw_get_positions"
SERVICE_RAW_COMMAND = "raw_command"
SERVICE_CLEAN_ZONE = "clean_zone"
SERVICE_START_SCENARIO = "start_scenario"

ATTR_ZONES = "zones"
ATTR_ROOMS = "rooms"
ATTR_SCENARIO_ID = "scenario_id"
MAX_ZONES = 10

ATTR_COMMAND = "command"
ATTR_PARAMS = "params"
ATTR_TIMEOUT = "timeout"
DEFAULT_RAW_COMMAND_TIMEOUT = 30.0

RAW_COMMAND_SCHEMA = probatio.Schema(
    {
        probatio.Optional(ATTR_DEVICE_ID): probatio.All(
            probatio.EnsureList(), [cv.string]
        ),
        probatio.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids,
        probatio.Required(ATTR_COMMAND): probatio.All(cv.string, probatio.Strip),
        probatio.Optional(ATTR_PARAMS): probatio.Any(dict, list, None),
        probatio.Optional(
            ATTR_TIMEOUT, default=DEFAULT_RAW_COMMAND_TIMEOUT
        ): probatio.All(probatio.Coerce(float), probatio.Range(min=1, max=300)),
    },
    extra=probatio.ALLOW_EXTRA,
)


class RawJsonCommand(CustomCommand):
    """JSON command whose answer is returned as is.

    Unlike ``CustomCommand``, a failed answer is not dropped and the answer is
    not fed into the event handling of the device.
    """

    def _handle_response(
        self, event_bus: EventBus, response: dict[str, Any]
    ) -> HandlingResult:
        """Accept every answer."""
        return HandlingResult.success()

    async def async_request(self, device: Device, authenticator: Any) -> dict[str, Any]:
        """Send the command and return the raw API answer.

        Errors of the API call are raised, so they can be reported to the
        caller. Falls back to the public ``Device.execute_command`` (which
        logs errors and returns an empty answer) if deebot-client changed.
        """
        device_info: ApiDeviceInfo = device.device_info
        request = getattr(self, "_execute_api_request", None)
        if authenticator is None or request is None:
            return await device.execute_command(self)
        return await request(authenticator, device_info)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services."""

    # Vacuum Services
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RAW_GET_POSITIONS,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="async_raw_get_positions",
        supports_response=SupportsResponse.ONLY,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_CLEAN_ZONE,
        entity_domain=VACUUM_DOMAIN,
        schema={
            probatio.Required(ATTR_ZONES): probatio.Any(
                probatio.All(list, probatio.Length(min=1, max=MAX_ZONES * 4)),
                cv.string,
            ),
            probatio.Optional(ATTR_ROOMS): probatio.Any(list, cv.string, int),
        },
        func="async_clean_zone",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_START_SCENARIO,
        entity_domain=VACUUM_DOMAIN,
        schema={
            probatio.Required(ATTR_SCENARIO_ID): probatio.All(
                cv.string, probatio.Strip, probatio.Length(min=1)
            ),
        },
        func="async_start_scenario",
    )

    async def async_raw_command(call: ServiceCall) -> ServiceResponse:
        """Send a raw JSON command to one robot."""
        controller, device = _resolve_target(hass, call)
        command: str = call.data[ATTR_COMMAND]
        params: dict[str, Any] | list[Any] | None = call.data.get(ATTR_PARAMS)
        timeout: float = call.data[ATTR_TIMEOUT]
        if not command:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="raw_command_empty",
            )

        info = device.device_info
        result: dict[str, Any] = {
            "command": command,
            "params": params,
            "device_class": info["class"],
        }
        cmd = RawJsonCommand(command, params)
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                response = await cmd.async_request(device, controller.authenticator)
        except TimeoutError:
            result |= {
                "success": False,
                "error": "timeout",
                "message": f"No answer within {timeout:g} seconds",
            }
        except Exception as ex:
            _LOGGER.debug("Raw command %s failed", command, exc_info=True)
            result |= {
                "success": False,
                "error": type(ex).__name__,
                "message": str(ex) or repr(ex),
            }
        else:
            if not response:
                result |= {
                    "success": False,
                    "error": "no_response",
                    "message": "deebot-client returned no answer, see the log",
                }
            else:
                result |= {
                    "success": response.get("ret") == "ok",
                    "response": response,
                }
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_RAW_COMMAND,
        async_raw_command,
        schema=RAW_COMMAND_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _resolve_target(hass: HomeAssistant, call: ServiceCall) -> tuple[Any, Device]:
    """Return the controller and robot targeted by a service call."""
    device_ids: set[str] = set(call.data.get(ATTR_DEVICE_ID) or [])
    entity_registry = er.async_get(hass)
    for entity_id in call.data.get(ATTR_ENTITY_ID) or []:
        entity_entry = entity_registry.async_get(entity_id)
        if entity_entry is None or entity_entry.device_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="raw_command_unknown_target",
                translation_placeholders={"target": entity_id},
            )
        device_ids.add(entity_entry.device_id)

    if len(device_ids) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="raw_command_one_target",
        )
    device_id = device_ids.pop()

    device_entry = dr.async_get(hass).async_get(device_id)
    dids = (
        {
            identifier
            for domain, identifier in device_entry.identifiers
            if domain == DOMAIN
        }
        if device_entry is not None
        else set()
    )
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        controller = entry.runtime_data
        for did in dids:
            if (device := controller.get_device(did)) is not None:
                return controller, device

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="raw_command_unknown_target",
        translation_placeholders={"target": device_id},
    )
