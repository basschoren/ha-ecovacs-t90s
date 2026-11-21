"""Controller module.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE. Changes: removed the legacy XMPP (py-sucks) robots, register extra
device classes before discovery, keep the REST configuration so the live view
can share the login.
"""

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
import logging
import ssl
from typing import TYPE_CHECKING

from deebot_client.api_client import ApiClient
from deebot_client.authentication import (
    Authenticator,
    RestConfiguration,
    create_rest_config,
)
from deebot_client.const import UNDEFINED, UndefinedType
from deebot_client.device import Device
from deebot_client.events import FirmwareEvent, NetworkInfoEvent
from deebot_client.exceptions import (
    DeebotError,
    DeviceVerificationRequiredError,
    InvalidAuthenticationError,
)
from deebot_client.models import AccountCredentials, ApiDeviceInfo, Credentials
from deebot_client.mqtt_client import MqttClient, create_mqtt_config
from deebot_client.util import md5

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client, device_registry as dr
from homeassistant.util.ssl import get_default_no_verify_context

from .const import (
    CONF_ACCOUNT_CREDENTIALS,
    CONF_OVERRIDE_MQTT_URL,
    CONF_OVERRIDE_REST_URL,
    CONF_VERIFY_MQTT_CERTIFICATE,
    DOMAIN,
)
from .device_classes import async_register_extra_device_classes

if TYPE_CHECKING:
    from .live_view_runtime import LiveViewRuntime

_LOGGER = logging.getLogger(__name__)


class EcovacsController:
    """Ecovacs controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize controller."""
        self._hass = hass
        self._entry = entry
        config = entry.data
        self._devices: list[Device] = []
        self._not_supported: list[ApiDeviceInfo] = []
        self._device_class_status: dict[str, str] = {}
        self._unsubscribe_device_events: list[Callable[[], None]] = []
        rest_url = config.get(CONF_OVERRIDE_REST_URL)
        self._device_id: str = config[CONF_DEVICE_ID]
        self._country: str = config[CONF_COUNTRY]

        self._rest_config = create_rest_config(
            aiohttp_client.async_get_clientsession(self._hass),
            device_id=self._device_id,
            alpha_2_country=self._country,
            override_rest_url=rest_url,
        )
        self._authenticator = Authenticator(
            self._rest_config,
            config[CONF_USERNAME],
            md5(config[CONF_PASSWORD]),
            # Renewing with the stored account credentials avoids the password
            # login, which keeps asking some accounts for device verification
            account_credentials=(
                AccountCredentials(**account)
                if (account := config.get(CONF_ACCOUNT_CREDENTIALS))
                else None
            ),
        )
        self._unsubscribe_account_credentials = (
            self._authenticator.subscribe_account_credentials(
                self._async_store_account_credentials
            )
        )
        self._api_client = ApiClient(self._authenticator)

        mqtt_url = config.get(CONF_OVERRIDE_MQTT_URL)
        ssl_context: UndefinedType | ssl.SSLContext = UNDEFINED
        if not config.get(CONF_VERIFY_MQTT_CERTIFICATE, True) and mqtt_url:
            ssl_context = get_default_no_verify_context()

        self._mqtt_config_fn = partial(
            create_mqtt_config,
            device_id=self._device_id,
            country=self._country,
            override_mqtt_url=mqtt_url,
            ssl_context=ssl_context,
        )
        self._mqtt_client: MqttClient | None = None
        # Set up by async_setup_entry after initialize()
        self.live_view: LiveViewRuntime | None = None

    async def initialize(self) -> None:
        """Init controller."""
        # Must run before the device list is requested
        self._device_class_status = await async_register_extra_device_classes()

        try:
            devices = await self._api_client.get_devices()

            if devices.mqtt:
                mqtt = await self._get_mqtt_client()
                mqtt_devices = [
                    Device(info, self._authenticator) for info in devices.mqtt
                ]
                async with asyncio.TaskGroup() as tg:

                    async def _init(device: Device) -> None:
                        """Initialize MQTT device."""
                        await device.initialize(mqtt)
                        self._subscribe_device_info_events(device)
                        self._devices.append(device)

                    for device in mqtt_devices:
                        tg.create_task(_init(device))

            if devices.xmpp:
                _LOGGER.warning(
                    "Ignoring %d legacy (XMPP) Ecovacs robot(s); this integration "
                    "only supports MQTT robots. Use the built-in Ecovacs "
                    "integration for them",
                    len(devices.xmpp),
                )
            for device_config in devices.not_supported:
                self._not_supported.append(device_config)
                _LOGGER.warning(
                    (
                        'Device "%s" (class %s) is not supported. More '
                        "information at "
                        "https://github.com/DeebotUniverse/client.py/issues/612"
                    ),
                    device_config.get("deviceName"),
                    device_config.get("class"),
                )

        except DeviceVerificationRequiredError as ex:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="device_verification_required",
            ) from ex
        except InvalidAuthenticationError as ex:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
            ) from ex
        except DeebotError as ex:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="setup_error",
            ) from ex

        _LOGGER.debug("Controller initialize complete")

    async def _async_store_account_credentials(
        self, account: AccountCredentials
    ) -> None:
        """Store new account credentials in the config entry."""
        new_value = asdict(account)
        if self._entry.data.get(CONF_ACCOUNT_CREDENTIALS) == new_value:
            return
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_ACCOUNT_CREDENTIALS: new_value},
        )

    def _subscribe_device_info_events(self, device: Device) -> None:
        """Update the device registry when the firmware or MAC is reported."""
        identifier = (DOMAIN, device.device_info["did"])
        device_registry = dr.async_get(self._hass)

        def device_entry() -> dr.DeviceEntry | None:
            return device_registry.async_get_device_by_identifier(
                identifier, self._entry.entry_id
            )

        async def on_firmware(event: FirmwareEvent) -> None:
            if entry := device_entry():
                device_registry.async_update_device(entry.id, sw_version=event.version)

        async def on_network(event: NetworkInfoEvent) -> None:
            if event.mac and (entry := device_entry()):
                device_registry.async_update_device(
                    entry.id,
                    new_connections={(dr.CONNECTION_NETWORK_MAC, event.mac)},
                )

        self._unsubscribe_device_events.extend(
            (
                device.events.subscribe(FirmwareEvent, on_firmware),
                device.events.subscribe(NetworkInfoEvent, on_network),
            )
        )

    async def teardown(self) -> None:
        """Disconnect controller."""
        if self.live_view is not None:
            try:
                await self.live_view.async_shutdown()
            except Exception:
                _LOGGER.exception("Error stopping the live view")
            self.live_view = None
        self._unsubscribe_account_credentials()
        for unsubscribe in self._unsubscribe_device_events:
            unsubscribe()
        for device in self._devices:
            await device.teardown()
        if self._mqtt_client is not None:
            await self._mqtt_client.disconnect()
        await self._authenticator.teardown()

    async def _get_mqtt_client(self) -> MqttClient:
        """Return validated MQTT client."""
        if self._mqtt_client is None:
            config = await self._hass.async_add_executor_job(self._mqtt_config_fn)
            mqtt = MqttClient(config, self._authenticator)
            await mqtt.verify_config()
            self._mqtt_client = mqtt

        return self._mqtt_client

    async def async_get_credentials(self) -> Credentials:
        """Return valid portal credentials of the shared login."""
        return await self._authenticator.authenticate()

    def get_device(self, did: str) -> Device | None:
        """Return the device with the given Ecovacs device ID."""
        return next(
            (device for device in self._devices if device.device_info["did"] == did),
            None,
        )

    @property
    def devices(self) -> list[Device]:
        """Return devices."""
        return self._devices

    @property
    def not_supported(self) -> list[ApiDeviceInfo]:
        """Return the raw records of robots deebot-client does not support."""
        return self._not_supported

    @property
    def device_class_status(self) -> dict[str, str]:
        """Return the status of the extra device class mappings."""
        return self._device_class_status

    @property
    def authenticator(self) -> Authenticator:
        """Return the shared authenticator."""
        return self._authenticator

    @property
    def rest_config(self) -> RestConfiguration:
        """Return the REST configuration of the shared login."""
        return self._rest_config

    @property
    def device_id(self) -> str:
        """Return the client device ID (the login resource)."""
        return self._device_id

    @property
    def country(self) -> str:
        """Return the configured two-letter country code."""
        return self._country
