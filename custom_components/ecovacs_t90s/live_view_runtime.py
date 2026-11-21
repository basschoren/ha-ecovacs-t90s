"""Glue between the shared Ecovacs login and the live view runtime.

The live view used to log in on its own (ECOVACS Live View). Here it reuses
the deebot-client ``Authenticator`` of the controller: the login resource
(client device ID), the portal URL and fresh portal credentials all come from
the one login the user configured.
"""

from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.core import HomeAssistant

from .const import (
    CONF_AUTO_STOP_MINUTES,
    CONF_LIVE_VIEW_PIN,
    DEFAULT_AUTO_STOP_MINUTES,
)
from .live_view import LiveViewManager
from .live_view_client import async_get_global_device_list

if TYPE_CHECKING:
    from .controller import EcovacsController

_LOGGER = logging.getLogger(__name__)

# Fields of the raw device record the live view uses
_ROBOT_FIELDS = ("did", "class", "resource", "name", "deviceName", "nick")


def _robot_record(device_info: Mapping[str, Any]) -> dict[str, Any]:
    """Return the robot record the live view runtime expects."""
    record = {key: str(device_info.get(key, "")) for key in _ROBOT_FIELDS}
    service = device_info.get("service")
    record["service"] = dict(service) if isinstance(service, Mapping) else {}
    return record


def _options_pin(options: Mapping[str, Any]) -> str:
    return str(options.get(CONF_LIVE_VIEW_PIN) or "").strip()


def _options_auto_stop(options: Mapping[str, Any]) -> int:
    try:
        return max(
            0, int(options.get(CONF_AUTO_STOP_MINUTES, DEFAULT_AUTO_STOP_MINUTES))
        )
    except TypeError, ValueError:
        return DEFAULT_AUTO_STOP_MINUTES


class LiveViewRuntime:
    """Live view state of one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        controller: EcovacsController,
        options: Mapping[str, Any],
    ) -> None:
        """Initialize the live view runtime."""
        self._controller = controller
        # A dedicated HTTP session, as in ECOVACS Live View, where this was the
        # proven working setup for the Kinesis/WebRTC signalling requests.
        self._session = aiohttp.ClientSession()
        self._records_enriched = False
        self.robots: dict[str, dict[str, Any]] = {
            device.device_info["did"]: _robot_record(device.device_info)
            for device in controller.devices
        }
        self.manager = LiveViewManager(
            hass,
            aiohttp_session=self._session,
            rest_config=controller.rest_config,
            get_credentials=controller.async_get_credentials,
            login_client_id=controller.device_id,
            country=controller.country.upper(),
            pin=_options_pin(options),
            auto_stop_minutes=_options_auto_stop(options),
        )

    def update_options(self, options: Mapping[str, Any]) -> None:
        """Apply changed options without reloading the robot connection."""
        self.manager.pin = _options_pin(options)
        self.manager.auto_stop_minutes = _options_auto_stop(options)
        self.manager._notify()  # noqa: SLF001

    @property
    def pin_configured(self) -> bool:
        """Return if a Live View PIN is configured."""
        return bool(self.manager.pin)

    async def _async_enrich_robot_records(self) -> None:
        """Merge the app's raw device records into the robot records.

        deebot-client already provides did, class and resource. The ECOVACS app
        asks for the device list with extra parameters; ECOVACS Live View uses
        that answer (for example the ``service.mqs`` host of the stream start
        ping), so ask for it once before the first live view start.
        """
        if self._records_enriched:
            return
        try:
            credentials = await self._controller.async_get_credentials()
            records = await async_get_global_device_list(
                self._session,
                portal_url=self._controller.rest_config.portal_url,
                credentials=credentials,
                login_client_id=self._controller.device_id,
            )
        except Exception:
            _LOGGER.warning(
                "Could not get the raw ECOVACS device list for the live view; "
                "using the device records of deebot-client",
                exc_info=True,
            )
            return

        for record in records:
            did = str(record.get("did", ""))
            if (robot := self.robots.get(did)) is not None:
                robot.update(_robot_record(record))
        self._records_enriched = True

    async def async_start(self, did: str) -> bool:
        """Start the live view of a robot."""
        robot = self.robots[did]
        if self.pin_configured:
            await self._async_enrich_robot_records()
        return await self.manager.start(robot)

    async def async_stop(self, did: str) -> None:
        """Stop the live view of a robot."""
        await self.manager.stop(self.robots[did])

    async def async_shutdown(self) -> None:
        """Stop all sessions and close the HTTP session."""
        try:
            await self.manager.stop_all()
        finally:
            if not self._session.closed:
                await self._session.close()

    def diagnostics(self) -> dict[str, Any]:
        """Return non-sensitive live view state per robot (index based)."""
        manager = self.manager
        return {
            "pin_configured": self.pin_configured,
            "auto_stop_minutes": manager.auto_stop_minutes,
            "records_enriched": self._records_enriched,
            "robots": [
                {
                    "class": robot["class"],
                    "has_mqs_service": bool(robot["service"].get("mqs")),
                    "status": manager.status.get(did, "idle"),
                    "streaming": manager.is_streaming(did),
                    "video_frames": manager.frames.get(did, 0),
                    "last_error": manager.errors.get(did),
                }
                for did, robot in self.robots.items()
            ],
        }
