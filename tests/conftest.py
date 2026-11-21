"""Fixtures for the ecovacs_t90s tests.

Run from the repository root with ``pytest tests`` in an environment with
``pytest-homeassistant`` (same version as Home Assistant). On Windows, put
``tests/windows_stubs`` on ``PYTHONPATH`` first (Home Assistant needs the POSIX
``fcntl`` and ``resource`` modules). All Ecovacs APIs are mocked.
"""

from collections.abc import AsyncGenerator, Generator
import os
import pathlib
import sys
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from deebot_client import const
from deebot_client.command import DeviceCommandResult
from deebot_client.device import Device
from deebot_client.exceptions import ApiError
from deebot_client.models import AccountCredentials, Credentials
import pytest

PROJECT = os.environ.get("ECOVACS_T90S_PROJECT") or str(
    pathlib.Path(__file__).resolve().parents[1]
)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from pytest_homeassistant.common import MockConfigEntry  # noqa: E402

from homeassistant.const import (  # noqa: E402
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant  # noqa: E402

DOMAIN = "ecovacs_t90s"
DID = "E0000000000000000001"
ENTRY_DATA = {
    CONF_USERNAME: "user@example.invalid",
    CONF_PASSWORD: "test-password",
    CONF_COUNTRY: "NL",
    CONF_DEVICE_ID: "TESTDEV1",
}
T90S_RECORD = {
    "did": DID,
    "name": DID,
    "class": "833gj5",
    "resource": "abcd",
    "company": "eco-ng",
    "service": {"jmq": "jmq.example.invalid", "mqs": "mqs.example.invalid"},
    "deviceName": "DEEBOT T90S PRO OMNI",
    "nick": "Test robot",
}


if sys.platform == "win32":
    # asyncio's self-pipe uses an AF_INET socketpair on Windows, which the
    # Home Assistant test plugin blocks. All network calls are mocked anyway.
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None

try:
    import aiortc  # noqa: F401
except ImportError:
    # The native av/aiortc libraries can be blocked on Windows (Application
    # Control). Use stand-ins so the modules import; WebRTC is not exercised.
    import types

    _av = types.ModuleType("av")
    _av.AudioFrame = type("AudioFrame", (), {})
    sys.modules["av"] = _av
    _aiortc = types.ModuleType("aiortc")
    for _name in (
        "RTCConfiguration",
        "RTCIceServer",
        "RTCPeerConnection",
        "RTCRtpSender",
        "RTCSessionDescription",
    ):
        setattr(_aiortc, _name, type(_name, (), {}))
    _aiortc.MediaStreamTrack = type(
        "MediaStreamTrack", (), {"__init__": lambda self: None}
    )
    sys.modules["aiortc"] = _aiortc
    AIORTC_STUBBED = True
else:
    AIORTC_STUBBED = False


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in all tests."""


@pytest.fixture
def device_records() -> list[dict[str, Any]]:
    """Raw device records returned by the device list api."""
    return [dict(T90S_RECORD)]


@pytest.fixture
def mock_authenticator(device_records: list[dict[str, Any]]) -> Generator[Mock]:
    """Mock the authenticator class used by controller and config flow."""
    with (
        patch(
            "custom_components.ecovacs_t90s.controller.Authenticator", autospec=True
        ) as mock,
        patch("custom_components.ecovacs_t90s.config_flow.Authenticator", new=mock),
    ):
        authenticator = mock.return_value
        authenticator.authenticate.return_value = Credentials("token", "user_id", 0)
        authenticator.account_credentials = AccountCredentials(
            "account_token", "account_user_id"
        )

        async def post_authenticated(
            path: str,
            json: dict[str, Any],
            *,
            query_params: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            match path:
                case const.PATH_API_APPSVR_APP:
                    return {"code": 0, "devices": device_records, "errno": "0"}
                case const.PATH_API_USERS_USER:
                    return {"todo": "result", "result": "ok", "devices": device_records}
                case const.PATH_API_IOT_DEVMANAGER:
                    return {
                        "ret": "ok",
                        "resp": {"body": {"code": 0, "data": {"echo": json}}},
                    }
                case _:
                    raise ApiError(f"Path not mocked: {path}")

        authenticator.post_authenticated.side_effect = post_authenticated
        yield authenticator


@pytest.fixture
def mock_mqtt_client(mock_authenticator: Mock) -> Generator[Mock]:
    """Mock the MQTT client."""
    with (
        patch(
            "custom_components.ecovacs_t90s.controller.MqttClient", autospec=True
        ) as mock,
        patch("custom_components.ecovacs_t90s.config_flow.MqttClient", new=mock),
    ):
        client = mock.return_value
        client.subscribe.return_value = lambda: None
        yield client


@pytest.fixture
def mock_device_execute() -> Generator[AsyncMock]:
    """Do not send robot commands during tests."""
    with patch.object(
        Device,
        "_execute_command",
        return_value=DeviceCommandResult(device_reached=True),
    ) as mock_execute:
        yield mock_execute


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry."""
    return MockConfigEntry(
        title=ENTRY_DATA[CONF_USERNAME],
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={"live_view_pin": "", "auto_stop_minutes": 10},
        minor_version=2,
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_authenticator: Mock,
    mock_mqtt_client: Mock,
    mock_device_execute: AsyncMock,
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    yield mock_config_entry
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
