"""Local fallback through the Matter twin of the robot (all mocked)."""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import aiohttp
from deebot_client.events import AvailabilityEvent
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.ecovacs_t90s.local_fallback import (
    find_matter_twin,
    normalize_serial,
    serials_match,
)
from homeassistant.components.vacuum import DATA_COMPONENT, VacuumEntityFeature
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import DID, DOMAIN, FakeRobot, robot_ok, robot_unsupported

# Matter vacuum of a T90S: start, stop, pause, return home, locate, clean area
TWIN_FEATURES = (
    VacuumEntityFeature.START
    | VacuumEntityFeature.STATE
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.LOCATE
    | VacuumEntityFeature.CLEAN_AREA
)
SERVICES = ("start", "pause", "stop", "return_to_base", "locate", "clean_area")


def test_serial_matching() -> None:
    """Serials are compared tolerantly."""
    assert normalize_serial(" e0-12 3 ") == "E0123"
    assert normalize_serial(None) == ""
    assert serials_match("E0000000000000000001", "e0000000000000000001")
    assert serials_match("E0000000000000000001", "SN:E0000000000000000001")
    assert serials_match("E00000-00000-00000-0001", "E0000000000000000001")
    assert not serials_match("E0000000000000000001", "E0000000000000000002")
    assert not serials_match("1234567", "1234567")  # too short
    assert serials_match("E0000000000000000001", "0000000001")  # 10 characters
    assert not serials_match("E0000000000000000001", "000000001")  # too short
    assert not serials_match("E0000000000000000001", None)


def add_matter_twin(
    hass: HomeAssistant,
    serial: str = DID,
    features: int = TWIN_FEATURES,
    state: str = "docked",
    *,
    serial_number: bool = True,
) -> str:
    """Register a Matter robot device with a vacuum entity."""
    matter_entry = MockConfigEntry(domain="matter")
    matter_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=matter_entry.entry_id,
        identifiers={
            ("matter", "deviceid_0000000000000001-0000000000000014-MatterNodeDevice"),
            ("matter", f"serial_{serial}"),
        },
        serial_number=serial if serial_number else None,
        manufacturer="ECOVACS",
        model="833gj5",
    )
    entry = er.async_get(hass).async_get_or_create(
        "vacuum",
        "matter",
        "twin-vacuum",
        config_entry=matter_entry,
        device_id=device.id,
        suggested_object_id="matter_twin",
    )
    hass.states.async_set(entry.entity_id, state, {"supported_features": features})
    return entry.entity_id


def mock_vacuum_services(hass: HomeAssistant) -> dict[str, list[ServiceCall]]:
    """Replace the vacuum services, so calls to the twin are recorded."""
    return {
        service: async_mock_service(hass, "vacuum", service) for service in SERVICES
    }


def ids(hass: HomeAssistant, domain: str, key: str) -> str:
    """Return the entity id of an entity of the robot."""
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{DID}_{key}")
    assert entity_id is not None
    return entity_id


def vacuum_entity(hass: HomeAssistant) -> Any:
    """Return the vacuum entity object of the robot."""
    return hass.data[DATA_COMPONENT].get_entity(ids(hass, "vacuum", "vacuum"))


async def test_find_twin(hass: HomeAssistant, robot: FakeRobot) -> None:
    """The twin is found by serial number or by the Matter serial identifier."""
    assert find_matter_twin(hass, DID) is None
    twin = add_matter_twin(hass, serial_number=False)
    found = find_matter_twin(hass, DID.lower())
    assert found is not None
    assert found[1] == twin
    assert find_matter_twin(hass, "E9999999999999999999") is None
    assert find_matter_twin(hass, "") is None


async def test_twin_found_after_setup(
    hass: HomeAssistant, init_robot: MockConfigEntry
) -> None:
    """A twin paired later is found through the registry events."""
    status = ids(hass, "sensor", "local_fallback")
    assert hass.states.get(status).state == "not_found"
    twin = add_matter_twin(hass)
    await hass.async_block_till_done()
    state = hass.states.get(status)
    assert state.state == "available"
    assert state.attributes["entity_id"] == twin
    vacuum = hass.states.get(ids(hass, "vacuum", "vacuum"))
    assert vacuum.attributes["local_fallback_entity_id"] == twin
    assert vacuum.attributes["control_path"] == "cloud"

    hass.states.async_set(twin, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "unavailable"


async def test_routes_to_twin_while_robot_unavailable(
    hass: HomeAssistant, robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """While deebot-client reports the robot unavailable, the twin is used."""
    twin = add_matter_twin(hass)
    await hass.async_block_till_done()
    calls = mock_vacuum_services(hass)
    device = init_robot.runtime_data.devices[0]
    vacuum_id = ids(hass, "vacuum", "vacuum")
    cloud = ids(hass, "binary_sensor", "cloud_connected")
    battery = ids(hass, "sensor", "battery_level")

    device.events.notify(AvailabilityEvent(available=False))
    await hass.async_block_till_done()
    assert hass.states.get(cloud).state == "off"
    state = hass.states.get(vacuum_id)
    assert state.state == "docked"
    assert state.attributes["control_path"] == "local"
    # Cloud-only entities are unavailable
    assert hass.states.get(battery).state == "unavailable"

    entity = vacuum_entity(hass)
    robot.sent.clear()
    await entity.async_start()
    await entity.async_pause()
    await entity.async_stop()
    await entity.async_return_to_base()
    await entity.async_locate()
    for service in ("start", "pause", "stop", "return_to_base", "locate"):
        assert [call.data["entity_id"] for call in calls[service]] == [twin], service
    # The cloud was not tried
    assert [
        name for name, _ in robot.sent if name in ("clean_V2", "clean", "charge")
    ] == []

    # The state of the twin is shown
    hass.states.async_set(twin, "cleaning", {"supported_features": TWIN_FEATURES})
    await hass.async_block_till_done()
    assert hass.states.get(vacuum_id).state == "cleaning"
    # Twin unavailable too: the vacuum is unavailable
    hass.states.async_set(twin, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(vacuum_id).state == "unavailable"

    # Back online: the cloud is used again
    hass.states.async_set(twin, "docked", {"supported_features": TWIN_FEATURES})
    device.events.notify(AvailabilityEvent(available=True))
    await hass.async_block_till_done()
    assert hass.states.get(cloud).state == "on"
    assert hass.states.get(vacuum_id).attributes["control_path"] == "cloud"
    await entity.async_start()
    assert robot.calls("clean_V2")
    assert len(calls["start"]) == 1


async def test_routes_to_twin_on_connection_error(
    hass: HomeAssistant, robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """A command that cannot reach the cloud goes to the twin."""
    twin = add_matter_twin(hass)
    await hass.async_block_till_done()
    calls = mock_vacuum_services(hass)
    cloud = ids(hass, "binary_sensor", "cloud_connected")
    entity = vacuum_entity(hass)

    robot.answers["clean_V2"] = aiohttp.ClientConnectionError("no internet")
    await entity.async_start()
    assert [call.data["entity_id"] for call in calls["start"]] == [twin]
    assert robot.calls("clean") == []
    await hass.async_block_till_done()
    assert hass.states.get(cloud).state == "off"
    assert hass.states.get(cloud).attributes["failure_reason"] == "connection"

    # Further commands go to the twin until the cloud answers again
    await entity.async_return_to_base()
    assert len(calls["return_to_base"]) == 1
    assert robot.calls("charge") == []

    # The recovery check (getBattery every 30 s) finds the robot again
    robot.answers["clean_V2"] = robot_ok()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(cloud).state == "on"
    await entity.async_pause()
    assert robot.calls("clean_V2")[-1] == {"act": "pause"}
    assert calls["pause"] == []


async def test_routes_to_twin_on_slow_cloud(
    hass: HomeAssistant, robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """With a twin, the cloud gets a limited time to answer."""
    add_matter_twin(hass)
    await hass.async_block_till_done()
    calls = mock_vacuum_services(hass)

    async def slow(args: Any) -> dict[str, Any]:
        await asyncio.sleep(5)
        return robot_ok()

    robot.answers["playSound"] = slow
    with patch(
        "custom_components.ecovacs_t90s.vacuum.CLOUD_TIMEOUT_WITH_FALLBACK", 0.05
    ):
        await vacuum_entity(hass).async_locate()
    assert len(calls["locate"]) == 1


async def test_rejected_command_is_not_routed(
    hass: HomeAssistant, robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """A robot that answers with an error is not bypassed."""
    add_matter_twin(hass)
    await hass.async_block_till_done()
    calls = mock_vacuum_services(hass)
    robot.answers["clean_V2"] = robot_unsupported()
    robot.answers["clean"] = robot_unsupported()
    with pytest.raises(HomeAssistantError) as err:
        await vacuum_entity(hass).async_start()
    assert err.value.translation_key == "command_rejected"
    assert calls["start"] == []


async def test_clean_segments_through_twin(
    hass: HomeAssistant, robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """Rooms are cleaned through the Home Assistant areas both vacuums map."""
    twin = add_matter_twin(hass)
    await hass.async_block_till_done()
    calls = mock_vacuum_services(hass)
    registry = er.async_get(hass)
    vacuum_id = ids(hass, "vacuum", "vacuum")
    registry.async_update_entity_options(
        vacuum_id,
        "vacuum",
        {"area_mapping": {"kitchen": ["100000001_3"], "hall": ["100000001_4"]}},
    )
    registry.async_update_entity_options(
        twin, "vacuum", {"area_mapping": {"kitchen": ["7"]}}
    )
    device = init_robot.runtime_data.devices[0]
    device.events.notify(AvailabilityEvent(available=False))
    await hass.async_block_till_done()

    entity = vacuum_entity(hass)
    await entity.async_clean_segments(["100000001_3"])
    assert calls["clean_area"][0].data == {
        "entity_id": twin,
        "cleaning_area_id": ["kitchen"],
    }
    # The hall is not mapped for the twin
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_clean_segments(["100000001_3", "100000001_4"])
    assert err.value.translation_key == "local_fallback_area_not_mapped"
    assert len(calls["clean_area"]) == 1


async def test_twin_without_feature(
    hass: HomeAssistant, robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """A service the twin does not support is reported."""
    add_matter_twin(hass, features=TWIN_FEATURES & ~VacuumEntityFeature.LOCATE)
    await hass.async_block_till_done()
    calls = mock_vacuum_services(hass)
    init_robot.runtime_data.devices[0].events.notify(AvailabilityEvent(available=False))
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError) as err:
        await vacuum_entity(hass).async_locate()
    assert err.value.translation_key == "local_fallback_unsupported"
    assert calls["locate"] == []
    # Zones are cloud only: they are still sent to the cloud
    await vacuum_entity(hass).async_clean_zone([[0, 0, 100, -100]])
    assert robot.calls("clean_V2")[-1]["content"]["type"] == "freeClean"
