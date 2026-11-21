"""Smoke tests for ecovacs_t90s with mocked Ecovacs APIs."""

import asyncio
import importlib
import logging
import pkgutil
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from deebot_client import hardware
from deebot_client.exceptions import ApiError, InvalidAuthenticationError
from deebot_client.models import Credentials
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import DID, DOMAIN, ENTRY_DATA

PKG = "custom_components.ecovacs_t90s"


@pytest.fixture
def reset_device_classes() -> None:
    """Forget earlier device class registrations."""
    from custom_components.ecovacs_t90s import device_classes

    for cls in ("833gj5", "zzzzzz"):
        hardware._DEVICES.pop(cls, None)
        hardware._NOT_FOUND.discard(cls)
        device_classes._MAPPED.pop(cls, None)


def test_import_all_modules() -> None:
    """Every module of the integration imports."""
    pkg = importlib.import_module(PKG)
    names = [m.name for m in pkgutil.iter_modules(pkg.__path__)]
    assert len(names) >= 20
    for name in names:
        importlib.import_module(f"{PKG}.{name}")


@pytest.mark.usefixtures("reset_device_classes")
async def test_device_class_mapping(caplog: pytest.LogCaptureFixture) -> None:
    """833gj5 gets the capabilities of twunby; the mapping is idempotent."""
    from custom_components.ecovacs_t90s.device_classes import (
        async_register_extra_device_classes,
    )

    assert await hardware.get_static_device_info("833gj5") is None
    assert await async_register_extra_device_classes() == {"833gj5": "mapped"}
    assert await hardware.get_static_device_info(
        "833gj5"
    ) is await hardware.get_static_device_info("twunby")
    assert await async_register_extra_device_classes() == {"833gj5": "mapped"}

    # A natively supported class is left alone
    assert await async_register_extra_device_classes({"twunby": "yna5xi"}) == {
        "twunby": "native"
    }
    # Unknown base class
    assert await async_register_extra_device_classes({"zzzzzz": "yyyyyy"}) == {
        "zzzzzz": "failed"
    }
    assert "does not know its base class" in caplog.text


@pytest.mark.usefixtures("reset_device_classes")
async def test_device_class_mapping_internals_changed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A changed deebot-client cache is reported instead of crashing."""
    from custom_components.ecovacs_t90s.device_classes import (
        async_register_extra_device_classes,
    )

    twunby = await hardware.get_static_device_info("twunby")
    renamed_cache = {"twunby": twunby}

    async def lookup(device_class: str) -> Any:
        return renamed_cache.get(device_class)

    # The private cache was renamed: report, do not crash
    with (
        patch.object(hardware, "get_static_device_info", lookup),
        patch.object(hardware, "_DEVICES", new=None),
    ):
        result = await async_register_extra_device_classes()
    assert result == {"833gj5": "failed"}
    assert "internals of deebot_client.hardware changed" in caplog.text

    # The cache exists but the lookup ignores it: report and undo
    with patch.object(hardware, "get_static_device_info", lookup):
        result = await async_register_extra_device_classes()
    assert result == {"833gj5": "failed"}
    assert "ignored the registered capabilities" in caplog.text
    assert "833gj5" not in hardware._DEVICES


@pytest.mark.usefixtures("reset_device_classes")
async def test_setup_t90s(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The T90S is discovered, all entities attach to one device."""
    entry = init_integration
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text
    assert entry.state is ConfigEntryState.LOADED
    controller = entry.runtime_data
    assert controller.device_class_status == {"833gj5": "mapped"}
    assert [d.device_info["did"] for d in controller.devices] == [DID]
    assert controller.not_supported == []
    assert DOMAIN not in hass.data

    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, DID), init_integration.entry_id
    )
    assert device is not None
    assert device.model_id == "833gj5"
    assert device.name == "Test robot"
    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert {e.device_id for e in entities} == {device.id}
    by_uid = {(e.domain, e.unique_id): e for e in entities}
    for key in [
        ("vacuum", f"{DID}_vacuum"),
        ("image", f"{DID}_map"),
        ("camera", f"{DID}_live_view"),
        ("switch", f"{DID}_live_view"),
        ("button", f"{DID}_start_live_view"),
        ("button", f"{DID}_stop_live_view"),
        ("sensor", f"{DID}_live_view_status"),
        ("sensor", f"{DID}_battery_level"),
    ]:
        assert key in by_uid, key
    assert len(entities) > 30

    camera = by_uid[("camera", f"{DID}_live_view")].entity_id
    assert hass.states.get(camera) is not None
    status = by_uid[("sensor", f"{DID}_live_view_status")].entity_id
    assert hass.states.get(status).state == "idle"
    names = sorted(
        f"{e.entity_id} | {hass.states.get(e.entity_id).name if hass.states.get(e.entity_id) else '-'}"
        for e in entities
    )
    print(*names, sep=chr(10))


async def test_live_view_uses_shared_login(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    mock_authenticator: Mock,
) -> None:
    """Live view needs a PIN, then uses the shared authenticator's credentials."""
    entry = init_integration
    controller = entry.runtime_data
    ids = {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if e.domain in ("button", "sensor")
    }
    start = ids[f"{DID}_start_live_view"]
    status = ids[f"{DID}_live_view_status"]

    await hass.services.async_call(
        "button", "press", {"entity_id": start}, blocking=True
    )
    state = hass.states.get(status)
    assert state.state == "error"
    assert "PIN" in state.attributes["last_error"]

    # Set a PIN through the options flow: applied without a reload
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"live_view_pin": " 1234 ", "auto_stop_minutes": 5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {"live_view_pin": "1234", "auto_stop_minutes": 5}
    assert entry.runtime_data is controller
    assert controller.live_view.manager.pin == "1234"
    assert controller.live_view.manager.auto_stop_minutes == 5

    start_watch = AsyncMock(side_effect=RuntimeError("blocked in test"))
    raw_list = AsyncMock(
        return_value=[
            {
                "did": DID,
                "class": "833gj5",
                "resource": "wxyz",
                "service": {"mqs": "mqs2.example.invalid"},
            }
        ]
    )
    with (
        patch(f"{PKG}.live_view.async_start_watch", start_watch),
        patch(f"{PKG}.live_view_runtime.async_get_global_device_list", raw_list),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": start}, blocking=True
        )
    kwargs = start_watch.call_args.kwargs
    assert kwargs["credentials"] == Credentials("token", "user_id", 0)
    assert kwargs["login_client_id"] == ENTRY_DATA[CONF_DEVICE_ID]
    assert kwargs["country"] == "NL"
    assert kwargs["pin"] == "1234"
    assert kwargs["rest_config"] is controller.rest_config
    assert kwargs["robot"]["resource"] == "wxyz"
    assert kwargs["robot"]["service"]["mqs"] == "mqs2.example.invalid"
    assert raw_list.call_args.kwargs["login_client_id"] == ENTRY_DATA[CONF_DEVICE_ID]
    state = hass.states.get(status)
    assert state.state == "error"
    assert state.attributes["last_error"] == "blocked in test"

    # The switch starts the live view in the background
    switch = entity_registry.async_get_entity_id("switch", DOMAIN, f"{DID}_live_view")
    start_watch.reset_mock()
    with patch(f"{PKG}.live_view.async_start_watch", start_watch):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert start_watch.await_count == 1
    assert hass.states.get(switch).state == "off"


async def test_raw_command(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    mock_authenticator: Mock,
) -> None:
    """raw_command returns raw answers and errors as data."""
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, DID), init_integration.entry_id
    )
    vacuum = entity_registry.async_get_entity_id("vacuum", DOMAIN, f"{DID}_vacuum")

    response = await hass.services.async_call(
        DOMAIN,
        "raw_command",
        {"device_id": device.id, "command": "getFoo", "params": {"a": 1}},
        blocking=True,
        return_response=True,
    )
    assert response["success"] is True
    sent = response["response"]["resp"]["body"]["data"]["echo"]
    assert sent["cmdName"] == "getFoo"
    assert sent["toId"] == DID
    assert sent["toType"] == "833gj5"
    assert sent["payload"]["body"] == {"data": {"a": 1}}

    # entity target, failing api call
    mock_authenticator.post_authenticated.side_effect = ApiError("boom")
    response = await hass.services.async_call(
        DOMAIN,
        "raw_command",
        {"entity_id": vacuum, "command": "getFoo"},
        blocking=True,
        return_response=True,
    )
    assert response["success"] is False
    assert response["error"] == "ApiError"
    assert response["message"] == "boom"

    # robot answers with an error
    mock_authenticator.post_authenticated.side_effect = None
    mock_authenticator.post_authenticated.return_value = {
        "ret": "fail",
        "errno": 500,
        "error": "timeout",
    }
    response = await hass.services.async_call(
        DOMAIN,
        "raw_command",
        {"entity_id": vacuum, "command": "getFoo"},
        blocking=True,
        return_response=True,
    )
    assert response["success"] is False
    assert response["response"]["errno"] == 500

    # timeout
    async def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    mock_authenticator.post_authenticated.side_effect = slow
    response = await hass.services.async_call(
        DOMAIN,
        "raw_command",
        {"entity_id": vacuum, "command": "getFoo", "timeout": 1},
        blocking=True,
        return_response=True,
    )
    assert response["success"] is False
    assert response["error"] == "timeout"

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "raw_command",
            {"command": "getFoo"},
            blocking=True,
            return_response=True,
        )


async def test_diagnostics_redacted(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Diagnostics do not contain credentials."""
    from custom_components.ecovacs_t90s.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    hass.config_entries.async_update_entry(
        init_integration, options={"live_view_pin": "9999", "auto_stop_minutes": 10}
    )
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    text = repr(diag)
    for secret in (
        "test-password",
        "user@example.invalid",
        "9999",
        DID,
        "TESTDEV1",
        "account_token",
    ):
        assert secret not in text, secret
    assert diag["device_class_mappings"]["833gj5"] in ("mapped", "native")
    assert diag["live_view"]["pin_configured"] is True


@pytest.mark.usefixtures("mock_mqtt_client")
async def test_config_flow_cloud(hass: HomeAssistant, mock_authenticator: Mock) -> None:
    """One login creates the entry, the PIN goes to the options."""
    with patch(f"{PKG}.async_setup_entry", return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MODE: "cloud"}
        )
        assert result["step_id"] == "auth"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "user@example.invalid",
                CONF_PASSWORD: "test-password",
                CONF_COUNTRY: "NL",
                "live_view_pin": "4321",
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_USERNAME] == "user@example.invalid"
    assert len(data[CONF_DEVICE_ID]) == 8
    assert "live_view_pin" not in data
    assert data["account_credentials"]["access_token"] == "account_token"
    assert result["options"] == {"live_view_pin": "4321", "auto_stop_minutes": 10}


@pytest.mark.usefixtures("mock_mqtt_client")
async def test_config_flow_invalid_auth(
    hass: HomeAssistant, mock_authenticator: Mock
) -> None:
    """Invalid credentials show an error."""
    mock_authenticator.authenticate.side_effect = InvalidAuthenticationError
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "u@example.invalid", CONF_PASSWORD: "x", CONF_COUNTRY: "NL"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
