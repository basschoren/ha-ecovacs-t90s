"""Extra features with a fake robot that answers like a T90S."""

import json
import logging
from typing import Any

from deebot_client.events import RoomsEvent
from deebot_client.models import Room
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import (
    DID,
    DOMAIN,
    FakeRobot,
    load_fixture,
    robot_no_answer,
    robot_ok,
    robot_unsupported,
)

START_V2 = {
    "act": "start",
    "content": {"total": 0, "donotClean": 0, "count": 0, "type": "auto"},
}


def answer_like_t90s(robot: FakeRobot) -> None:
    """Answer the get commands like the real T90S did."""
    for name, data in load_fixture("answers_833gj5.json").items():
        if not name.startswith("_"):
            robot.answers[name] = (
                robot_unsupported() if data is None else robot_ok(data)
            )
    robot.answers["getFurnitureInfo_V2"] = robot_ok(
        {
            key: value
            for key, value in load_fixture("furniture_833gj5.json").items()
            if not key.startswith("_")
        }
    )
    robot.answers["getVoiceAssistantState"] = robot_unsupported()
    robot.answers["getOta"] = robot_no_answer()
    robot.answers["getSweepMode"] = robot_ok({"type": 0})


@pytest.fixture
def t90s_robot(robot: FakeRobot) -> FakeRobot:
    """Fake robot with the answers of a T90S."""
    answer_like_t90s(robot)
    return robot


def entity_id(hass: HomeAssistant, domain: str, key: str) -> str | None:
    """Return the entity id of an entity of the robot."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{DID}_{key}")


@pytest.mark.usefixtures("t90s_robot")
async def test_probes_create_supported_entities(
    hass: HomeAssistant,
    init_robot: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entities exist only for the settings the robot answered."""
    extras = init_robot.runtime_data.extras[DID]
    assert extras.prefer_v2 is True
    assert extras.features["wash_info"] == "supported"
    assert extras.features["drying_duration"] == "supported"
    assert extras.features["custom_area_mode"] == "supported"
    assert extras.features["mop_expand"] == "supported"
    assert extras.features["true_detect_level"] == "supported"
    assert extras.features["block"] == "supported"
    assert extras.features["mop_only"] == "supported"
    assert extras.features["voice_assistant"] == "unsupported"
    assert extras.feature_details["voice_assistant"] == "code 20003"
    assert extras.features["ota_auto_update"] == "no_answer"
    # No scenarios saved
    assert extras.features["quick_command"] == "unsupported"
    assert extras.features["furniture"] == "supported"
    assert extras.features["position"] == "supported"

    states = {
        key: hass.states.get(eid).state
        for key, domain in (
            ("wash_temperature", "select"),
            ("drying_duration", "select"),
            ("cleaning_speed", "select"),
            ("edge_mopping", "select"),
            ("do_not_disturb", "switch"),
        )
        if (eid := entity_id(hass, domain, key))
    }
    assert states == {
        "wash_temperature": "warm",
        "drying_duration": "three_hours",
        "cleaning_speed": "standard",
        "edge_mopping": "standard",
        "do_not_disturb": "off",
    }
    registry = er.async_get(hass)
    # Disabled by default, but created
    assert hass.states.get(
        entity_id(hass, "select", "true_detect_sensitivity")
    ).state == ("standard")
    for domain, key in (
        ("switch", "mop_only"),
        ("sensor", "position_x"),
        ("sensor", "position_angle"),
    ):
        eid = entity_id(hass, domain, key)
        assert eid is not None, key
        assert registry.async_get(eid).disabled_by is not None
    # Not created
    for domain, key in (
        ("switch", "voice_assistant"),
        ("switch", "ota_auto_update"),
        ("select", "scenario"),
        ("button", "start_scenario"),
        ("select", "active_map"),
    ):
        assert entity_id(hass, domain, key) is None, key
    # Station stop buttons (station can wash and dry)
    assert entity_id(hass, "button", "station_stop_wash_mop")
    assert entity_id(hass, "button", "station_stop_dry_mop")

    # Furniture of the active map
    image = hass.states.get(entity_id(hass, "image", "map"))
    furniture = image.attributes["furniture"]
    assert len(furniture) == 8
    assert furniture[0]["type"] == 20

    # Cloud connection and local fallback
    assert (
        hass.states.get(entity_id(hass, "binary_sensor", "cloud_connected")).state
        == "on"
    )
    assert (
        hass.states.get(entity_id(hass, "sensor", "local_fallback")).state
        == "not_found"
    )
    assert not [
        record
        for record in caplog.records
        if record.name.startswith("custom_components")
        and record.levelno >= logging.WARNING
    ], caplog.text


async def test_set_settings(
    hass: HomeAssistant, t90s_robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """Changing a setting sends the set command and updates the entity."""
    wash = entity_id(hass, "select", "wash_temperature")
    await hass.services.async_call(
        "select", "select_option", {"entity_id": wash, "option": "max"}, blocking=True
    )
    assert t90s_robot.calls("setWashInfo") == [{"mode": 3, "interval": 15}]
    await hass.async_block_till_done()
    assert hass.states.get(wash).state == "max"

    edge = entity_id(hass, "select", "edge_mopping")
    await hass.services.async_call(
        "select", "select_option", {"entity_id": edge, "option": "off"}, blocking=True
    )
    assert t90s_robot.calls("setMopExpandState") == [{"enable": 0, "level": 1}]

    dnd = entity_id(hass, "switch", "do_not_disturb")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": dnd}, blocking=True
    )
    assert t90s_robot.calls("setBlock") == [
        {"enable": 1, "start": "22:0", "end": "8:0"}
    ]
    assert hass.states.get(dnd).state == "on"

    t90s_robot.answers["setDryingDuration"] = robot_unsupported()
    drying = entity_id(hass, "select", "drying_duration")
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": drying, "option": "two_hours"},
            blocking=True,
        )
    assert err.value.translation_key == "command_rejected"
    assert hass.states.get(drying).state == "three_hours"


@pytest.mark.usefixtures("t90s_robot")
async def test_pushes(hass: HomeAssistant, init_robot: MockConfigEntry) -> None:
    """Robot pushes update the settings and the furniture."""
    extras = init_robot.runtime_data.extras[DID]
    assert extras.message_hook_installed is True
    device = init_robot.runtime_data.devices[0]

    def push(name: str, data: dict[str, Any]) -> None:
        extras.handle_message(name, json.dumps({"body": {"data": data}}).encode())

    push("onWashInfo", {"mode": 0})
    await hass.async_block_till_done()
    assert (
        hass.states.get(entity_id(hass, "select", "wash_temperature")).state == "cold"
    )

    push(
        "onFurnitureInfo_V2",
        {
            "mid": "100000001",
            "info": [{"content": "0,0;1000,0;1000,-1000;20", "fid": 1, "type": 20}],
        },
    )
    await hass.async_block_till_done()
    image = hass.states.get(entity_id(hass, "image", "map"))
    assert len(image.attributes["furniture"]) == 1

    # Unknown pushes and broken payloads are ignored
    extras.handle_message("onSomething", b"{}")
    extras.handle_message("onWashInfo", b"not json")

    # The real hook passes messages on after deebot-client handled them
    from custom_components.ecovacs_t90s.extras import install_message_hook

    seen: list[str] = []
    assert install_message_hook(device, lambda name, data: seen.append(name))
    device._handle_message("onBattery", {"body": {"data": {"value": 50}}})
    assert seen == ["onBattery"]


async def test_vacuum_clean_v2_first(
    hass: HomeAssistant, t90s_robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """clean_V2 is used; on 20003 the robot gets clean and that is remembered."""
    vacuum = entity_id(hass, "vacuum", "vacuum")
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum}, blocking=True
    )
    await hass.async_block_till_done()
    assert t90s_robot.calls("clean_V2") == [START_V2]
    assert t90s_robot.calls("clean") == []
    assert hass.states.get(vacuum).state == "cleaning"

    await hass.services.async_call(
        "vacuum", "pause", {"entity_id": vacuum}, blocking=True
    )
    await hass.async_block_till_done()
    assert t90s_robot.calls("clean_V2")[-1] == {"act": "pause"}
    assert hass.states.get(vacuum).state == "paused"

    # Start while paused resumes
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum}, blocking=True
    )
    assert t90s_robot.calls("clean_V2")[-1] == {"act": "resume"}

    # A robot without clean_V2
    t90s_robot.answers["clean_V2"] = robot_unsupported()
    t90s_robot.sent.clear()
    await hass.services.async_call(
        "vacuum", "stop", {"entity_id": vacuum}, blocking=True
    )
    assert [name for name, _ in t90s_robot.sent if name.startswith("clean")] == [
        "clean_V2",
        "clean",
    ]
    assert t90s_robot.calls("clean") == [{"act": "stop"}]
    t90s_robot.sent.clear()
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum}, blocking=True
    )
    assert [name for name, _ in t90s_robot.sent if name.startswith("clean")] == [
        "clean"
    ]

    # Both variants rejected
    t90s_robot.answers["clean"] = robot_unsupported()
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "vacuum", "start", {"entity_id": vacuum}, blocking=True
        )
    assert err.value.translation_key == "command_rejected"

    # Return to base and locate
    await hass.services.async_call(
        "vacuum", "return_to_base", {"entity_id": vacuum}, blocking=True
    )
    await hass.async_block_till_done()
    assert t90s_robot.calls("charge") == [{"act": "go"}]
    assert hass.states.get(vacuum).state == "returning"
    await hass.services.async_call(
        "vacuum", "locate", {"entity_id": vacuum}, blocking=True
    )
    assert t90s_robot.calls("playSound") == [{"count": 1, "sid": 30}]


async def test_vacuum_unreachable_without_fallback(
    hass: HomeAssistant, t90s_robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """Without a Matter twin a connection problem is reported."""
    vacuum = entity_id(hass, "vacuum", "vacuum")
    cloud = entity_id(hass, "binary_sensor", "cloud_connected")
    t90s_robot.answers["clean_V2"] = robot_no_answer()
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "vacuum", "start", {"entity_id": vacuum}, blocking=True
        )
    assert err.value.translation_key == "robot_unreachable"
    # No V1 attempt on a connection problem
    assert t90s_robot.calls("clean") == []
    assert hass.states.get(cloud).state == "off"
    assert hass.states.get(cloud).attributes["failure_reason"] == "no_answer"

    # Any answer of the robot clears the failure
    await hass.services.async_call(
        "vacuum", "locate", {"entity_id": vacuum}, blocking=True
    )
    assert hass.states.get(cloud).state == "on"


async def test_rooms_zones_scenarios(
    hass: HomeAssistant, t90s_robot: FakeRobot, init_robot: MockConfigEntry
) -> None:
    """Room, zone and scenario jobs use freeClean / qcClean."""
    vacuum = entity_id(hass, "vacuum", "vacuum")
    await hass.services.async_call(
        "vacuum",
        "send_command",
        {"entity_id": vacuum, "command": "spot_area", "params": {"rooms": [2, 1]}},
        blocking=True,
    )
    assert t90s_robot.calls("clean_V2")[-1] == {
        "act": "start",
        "content": {"type": "freeClean", "value": "1,2;1,1"},
    }
    await hass.services.async_call(
        DOMAIN,
        "clean_zone",
        {"entity_id": vacuum, "zones": [[1410, 2990, -360, 1210]], "rooms": [1]},
        blocking=True,
    )
    assert t90s_robot.calls("clean_V2")[-1] == {
        "act": "start",
        "content": {"type": "freeClean", "value": "3,null,1410,2990,-360,1210;1,1"},
    }
    await hass.services.async_call(
        DOMAIN,
        "clean_zone",
        {"entity_id": vacuum, "zones": "0,0,1000,-1000,2000,0,3000,-1000"},
        blocking=True,
    )
    assert t90s_robot.calls("clean_V2")[-1]["content"]["value"] == (
        "3,null,0,0,1000,-1000;3,null,2000,0,3000,-1000"
    )
    await hass.services.async_call(
        DOMAIN,
        "start_scenario",
        {"entity_id": vacuum, "scenario_id": "5510"},
        blocking=True,
    )
    assert t90s_robot.calls("clean_V2")[-1] == {
        "act": "start",
        "content": {"type": "qcClean", "value": "5510"},
    }
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "clean_zone",
            {"entity_id": vacuum, "zones": [[1, 2, 3]]},
            blocking=True,
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "clean_zone",
            {"entity_id": vacuum, "zones": [[1, 2, 3, 4]], "rooms": "kitchen"},
            blocking=True,
        )

    # Station stop buttons
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id(hass, "button", "station_stop_dry_mop")},
        blocking=True,
    )
    assert t90s_robot.calls("stationAction") == [{"act": 4, "type": 2}]


@pytest.mark.usefixtures("t90s_robot")
async def test_room_names(hass: HomeAssistant, init_robot: MockConfigEntry) -> None:
    """Rooms without a name get "Room <id>" (translated)."""
    device = init_robot.runtime_data.devices[0]
    vacuum = entity_id(hass, "vacuum", "vacuum")
    device.events.notify(
        RoomsEvent(map_id="100000001", rooms=[Room("", 3, ""), Room("Hal", 4, "")])
    )
    await hass.async_block_till_done()
    assert hass.states.get(vacuum).attributes["rooms"] == {"room_3": 3, "hal": 4}

    from homeassistant.components.vacuum import DATA_COMPONENT

    entity = hass.data[DATA_COMPONENT].get_entity(vacuum)
    segments = await entity.async_get_segments()
    assert [segment.name for segment in segments] == ["Room 3", "Hal"]
    assert segments[0].id == "100000001_3"

    hass.config.language = "nl"
    entity.async_write_ha_state()
    assert hass.states.get(vacuum).attributes["rooms"] == {"kamer_3": 3, "hal": 4}


async def test_probe_retry_and_unknown_answers(
    hass: HomeAssistant, init_robot: MockConfigEntry, robot: FakeRobot
) -> None:
    """Probes without answer are retried; empty answers count as unsupported."""
    extras = init_robot.runtime_data.extras[DID]
    # Default answer of the fake robot: ok without data
    assert extras.features["wash_info"] == "unsupported"
    assert extras.feature_details["wash_info"] == "answer without the expected data"
    assert entity_id(hass, "select", "wash_temperature") is None

    robot.answers["getDryingDuration"] = robot_no_answer()
    extras.features["drying_duration"] = extras.features["drying_duration"].NO_ANSWER
    await extras.async_probe()
    assert extras.features["drying_duration"] == "no_answer"
    # The robot answers on the next attempt: the entity appears
    robot.answers["getDryingDuration"] = robot_ok({"duration": 120})
    await extras.async_probe()
    await hass.async_block_till_done()
    assert extras.features["drying_duration"] == "supported"
    drying = entity_id(hass, "select", "drying_duration")
    assert hass.states.get(drying).state == "two_hours"


@pytest.mark.usefixtures("t90s_robot")
async def test_diagnostics(hass: HomeAssistant, init_robot: MockConfigEntry) -> None:
    """Diagnostics show the extra features without robot ids."""
    from custom_components.ecovacs_t90s.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, init_robot)
    extras = diag["extras"][0]
    assert extras["features"]["furniture"]["status"] == "supported"
    assert len(extras["furniture"]) == 8
    assert extras["local_fallback"]["status"] == "not_found"
    assert extras["cloud"]["connected"] is True
    assert DID not in repr(diag)


@pytest.mark.usefixtures("mock_mqtt_client")
async def test_optional_entities(
    hass: HomeAssistant, t90s_robot: FakeRobot, mock_config_entry: MockConfigEntry
) -> None:
    """Scenarios, a second map, OTA and clean logs create their entities."""
    t90s_robot.answers["getQuickCommand"] = robot_ok(
        [{"array": [{"qcid": "5510", "name": "Kitchen"}], "mid": "100000001"}]
    )
    t90s_robot.answers["getOta"] = robot_ok(
        {
            "supportAuto": 1,
            "autoSwitch": 0,
            "ver": "1.50.62",
            "status": "idle",
            "progress": 0,
        }
    )
    maps = load_fixture("answers_833gj5.json")["getCachedMapInfo"]
    maps["info"][1] = {
        "mid": "100000002",
        "index": 1,
        "status": 1,
        "using": 0,
        "built": 1,
        "name": "Upstairs",
    }
    t90s_robot.answers["getCachedMapInfo"] = robot_ok(maps)
    t90s_robot.clean_logs = [
        {
            "ts": 1763700000,
            "imageUrl": "https://example.invalid/map.png",
            "type": "auto",
            "area": 12,
            "stopReason": 1,
            "last": 900,
        }
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)

    scenario = entity_id(hass, "select", "scenario")
    assert hass.states.get(scenario).state == "Kitchen"
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id(hass, "button", "start_scenario")},
        blocking=True,
    )
    assert t90s_robot.calls("clean_V2")[-1] == {
        "act": "start",
        "content": {"type": "qcClean", "value": "5510"},
    }

    active_map = entity_id(hass, "select", "active_map")
    state = hass.states.get(active_map)
    assert state.attributes["options"] == ["100000001", "Upstairs"]
    assert state.state == "100000001"
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": active_map, "option": "Upstairs"},
        blocking=True,
    )
    assert t90s_robot.calls("setCachedMapInfo") == [
        {"act": "userchangemap", "userUsing": 1, "mid": "100000002"}
    ]

    ota = entity_id(hass, "switch", "ota_auto_update")
    assert registry.async_get(ota).disabled_by is not None

    image = entity_id(hass, "image", "last_clean_map")
    state = hass.states.get(image)
    assert state.state == "2025-11-21T04:40:00+00:00"
    assert state.attributes["area"] == 12
    assert state.attributes["stop_reason"] == "finished"

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
