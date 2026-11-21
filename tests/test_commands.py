"""Payloads and answer handling of the integration's own robot commands."""

from deebot_client.models import CleanAction
import pytest

from custom_components.ecovacs_t90s.commands import settings
from custom_components.ecovacs_t90s.commands.base import (
    CommandOutcome,
    T90Command,
    classify_response,
)
from custom_components.ecovacs_t90s.commands.clean import (
    charge_command,
    clean_action_payloads,
    free_clean_value,
    play_sound_command,
    rooms_payloads,
    scenario_command,
    station_stop_command,
    zone_payloads,
)

from .conftest import load_fixture, robot_no_answer, robot_ok, robot_unsupported


def test_clean_action_payloads() -> None:
    """clean_V2 first, clean (V1) as fallback."""
    # Verified on a T90S: this starts the robot, "clean" is answered with 20003
    assert clean_action_payloads(CleanAction.START) == {
        "v2": (
            "clean_V2",
            {
                "act": "start",
                "content": {"total": 0, "donotClean": 0, "count": 0, "type": "auto"},
            },
        ),
        "v1": ("clean", {"act": "start", "type": "auto"}),
    }
    assert clean_action_payloads(CleanAction.PAUSE)["v2"] == (
        "clean_V2",
        {"act": "pause"},
    )
    assert clean_action_payloads(CleanAction.RESUME)["v2"] == (
        "clean_V2",
        {"act": "resume"},
    )
    assert clean_action_payloads(CleanAction.STOP) == {
        "v2": ("clean_V2", {"act": "stop"}),
        "v1": ("clean", {"act": "stop"}),
    }
    # Copies, so a caller cannot change the templates
    payload = clean_action_payloads(CleanAction.START)["v2"][1]
    payload["content"]["type"] = "x"
    assert (
        clean_action_payloads(CleanAction.START)["v2"][1]["content"]["type"] == "auto"
    )


def test_free_clean_grammar() -> None:
    """Rooms are "1,<id>", rectangles "3,null,x1,y1,x2,y2", joined with ";"."""
    assert free_clean_value(rooms=[2, 1]) == "1,2;1,1"
    assert free_clean_value([[1410, 2990, -360, 1210]]) == "3,null,1410,2990,-360,1210"
    assert (
        free_clean_value([[1410.0, 2990, -360, 1210.5]], [1])
        == "3,null,1410,2990,-360,1210.5;1,1"
    )


def test_room_and_zone_payloads() -> None:
    """Room and zone jobs in both variants."""
    assert rooms_payloads([2, 1], count=2) == {
        "v2": (
            "clean_V2",
            {"act": "start", "content": {"type": "freeClean", "value": "1,2;1,1"}},
        ),
        "v1": (
            "clean",
            {"act": "start", "type": "spotArea", "content": "2,1", "count": 2},
        ),
    }
    assert zone_payloads([[1410, 2990, -360, 1210]], [1]) == {
        "v2": (
            "clean_V2",
            {
                "act": "start",
                "content": {
                    "type": "freeClean",
                    "value": "3,null,1410,2990,-360,1210;1,1",
                },
            },
        )
    }
    single = zone_payloads([[0, 0, 100, -100]])
    assert single["v1"] == (
        "clean",
        {"act": "start", "type": "customArea", "content": "0,0,100,-100", "count": 1},
    )


def test_other_commands() -> None:
    """Scenario, charge, locate and station stop."""
    command = scenario_command("5510")
    assert (command.NAME, command.args) == (
        "clean_V2",
        {"act": "start", "content": {"type": "qcClean", "value": "5510"}},
    )
    command = charge_command()
    assert (command.NAME, command.args) == ("charge", {"act": "go"})
    already = robot_ok()
    already["resp"]["body"]["code"] = 30007
    assert classify_response(already, frozenset({0, 30007}))[0] is CommandOutcome.OK
    command = play_sound_command()
    assert (command.NAME, command.args) == ("playSound", {"count": 1, "sid": 30})
    assert station_stop_command(4).args == {"act": 4, "type": 4}
    assert station_stop_command(2).args == {"act": 4, "type": 2}
    assert T90Command("getFoo", {"a": 1}) == T90Command("getFoo", {"a": 1})
    assert T90Command("getFoo", {"a": 1}) != T90Command("getFoo", {"a": 2})


@pytest.mark.parametrize(
    ("response", "outcome", "code", "data"),
    [
        (robot_ok({"enable": 1}), CommandOutcome.OK, 0, {"enable": 1}),
        (robot_ok([{"array": []}]), CommandOutcome.OK, 0, [{"array": []}]),
        (robot_ok("text"), CommandOutcome.OK, 0, {}),
        (robot_unsupported(), CommandOutcome.UNSUPPORTED, 20003, {}),
        (
            {"ret": "ok", "resp": {"body": {"code": "500", "msg": "fail"}}},
            CommandOutcome.REJECTED,
            500,
            {},
        ),
        (robot_no_answer(), CommandOutcome.NO_ANSWER, 500, {}),
        ({"ret": "fail", "errno": "4200"}, CommandOutcome.OFFLINE, 4200, {}),
        ({"ret": "fail", "errno": 3}, CommandOutcome.REJECTED, 3, {}),
        ({"ret": "ok"}, CommandOutcome.OK, None, {}),
        (None, CommandOutcome.REJECTED, None, {}),
    ],
)
def test_classify_response(
    response: object, outcome: CommandOutcome, code: int | None, data: object
) -> None:
    """Robot answers are classified."""
    assert classify_response(response) == (outcome, code, data)


def test_connection_problems() -> None:
    """Only cloud/robot reachability counts as connection problem."""
    assert CommandOutcome.CONNECTION.is_connection_problem
    assert CommandOutcome.NO_ANSWER.is_connection_problem
    assert CommandOutcome.OFFLINE.is_connection_problem
    assert not CommandOutcome.UNSUPPORTED.is_connection_problem
    assert not CommandOutcome.REJECTED.is_connection_problem
    assert not CommandOutcome.OK.is_connection_problem


def test_settings_of_real_robot() -> None:
    """The answers of a T90S are read correctly."""
    answers = load_fixture("answers_833gj5.json")
    wash = answers["getWashInfo"]
    assert settings.WASH_INFO.is_valid(wash)
    assert settings.wash_temperature_option(wash) == "warm"
    # The known interval is sent along (deebot-client #2028)
    assert settings.wash_temperature_args("max", wash) == {"mode": 3, "interval": 15}
    assert settings.wash_temperature_args("cold", {}) == {"mode": 0}

    assert settings.drying_duration_option(answers["getDryingDuration"]) == (
        "three_hours"
    )
    assert settings.drying_duration_args("four_hours", {}) == {"duration": 240}

    assert settings.cleaning_speed_option(answers["getCustomAreaMode"]) == "standard"
    assert settings.cleaning_speed_args("quick", {}) == {"sweepMode": 2}

    expand = answers["getMopExpandState"]
    assert settings.mop_extension_option(expand) == "standard"
    # No frequency, as the T90S does not report one
    assert settings.mop_extension_args("extreme", expand) == {"enable": 1, "level": 0}
    assert settings.mop_extension_args("off", expand) == {"enable": 0, "level": 1}
    assert settings.mop_extension_args("standard", {"frequency": 1}) == {
        "enable": 1,
        "level": 1,
        "frequency": 1,
    }
    assert settings.mop_extension_option({"enable": 0, "level": 0}) == "off"

    true_detect = answers["getTrueDetect"]
    assert settings.TRUE_DETECT_LEVEL.is_valid(true_detect)
    assert not settings.TRUE_DETECT_LEVEL.is_valid({"enable": 1})
    assert settings.true_detect_level_option(true_detect) == "standard"
    assert settings.true_detect_level_args("high", true_detect) == {
        "enable": 1,
        "level": 0,
    }

    block = answers["getBlock"]
    assert settings.block_args(True, block) == {
        "enable": 1,
        "start": "22:0",
        "end": "8:0",
    }
    assert settings.block_args(False, {}) == {
        "enable": 0,
        "start": "22:0",
        "end": "8:0",
    }

    # No scenarios defined: the scenario entities are not created
    quick = answers["getQuickCommand"]
    assert settings.parse_scenarios(quick) == []
    assert not settings.QUICK_COMMAND.is_valid(quick)
    command = settings.QUICK_COMMAND.get_command()
    assert (command.NAME, command.args) == ("getQuickCommand", {"type": "1,2"})


def test_parse_scenarios() -> None:
    """Scenario entries are read defensively (format not known yet)."""
    data = [
        {
            "mid": "1",
            "array": [
                {"qcid": "5510", "name": "Kitchen"},
                {"id": 7},
                {"name": "no id"},
                "garbage",
                {"qcid": "5510", "name": "duplicate"},
            ],
        },
        {"mid": "2", "array": "not a list"},
        "garbage",
    ]
    scenarios = settings.parse_scenarios(data)
    assert [(s.id, s.name, s.map_id) for s in scenarios] == [
        ("5510", "Kitchen", "1"),
        ("7", "#7", "1"),
    ]
    assert (
        settings.parse_scenarios({"list": [{"qcId": "1", "qcName": "A"}]})[0].name
        == "A"
    )
    assert settings.parse_scenarios(None) == []
    assert settings.QUICK_COMMAND.is_valid(data)


def test_setting_specs() -> None:
    """Get/set names and push names."""
    assert settings.WASH_INFO.push == frozenset({"onWashInfo", "onWashInfo_V2"})
    command = settings.BLOCK.set_command({"enable": 1})
    assert (command.NAME, command.args) == ("setBlock", {"enable": 1})
    with pytest.raises(NotImplementedError):
        settings.QUICK_COMMAND.set_command({})
    command = settings.set_active_map_command("123")
    assert command.args == {"act": "userchangemap", "userUsing": 1, "mid": "123"}
    assert "getCarpetInfo" not in {spec.get for spec in settings.SETTINGS}
