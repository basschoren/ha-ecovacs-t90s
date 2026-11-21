"""Live view start path with mocked ECOVACS/Kinesis network calls."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from .conftest import DID

PKG = "custom_components.ecovacs_t90s"


class _StopHere(Exception):
    """Raised by the mocked Kinesis call to end the start early."""


@pytest.mark.parametrize("foreign_live_view_data", [False, True])
async def test_live_view_start_uses_own_http_session(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    foreign_live_view_data: bool,
) -> None:
    """The WebRTC session uses the HTTP session of this integration.

    Upstream looked it up in hass.data["ecovacs_live"]. With the old
    integration loaded next to this one that lookup found no robot and failed
    with "coroutine raised StopIteration".
    """
    from custom_components.ecovacs_t90s.live_view import KinesisSession

    if foreign_live_view_data:
        hass.data["ecovacs_live"] = {
            "other_entry": {"devices": [{"did": "other"}], "manager": object()}
        }
    runtime = init_integration.runtime_data.live_view
    runtime.update_options({"live_view_pin": "1234", "auto_stop_minutes": 10})

    session_info = KinesisSession(
        region="eu-west-1",
        channel_name="channel",
        client_id="client",
        access_key_id="AKIDEXAMPLE",
        secret_access_key="secret",
        session_token="token",
    )
    seen_http: list[Any] = []

    async def describe(http: Any, info: KinesisSession) -> str:
        seen_http.append(http)
        assert info is session_info
        raise _StopHere("stopped after the channel lookup")

    with (
        patch.object(runtime, "_async_enrich_robot_records", AsyncMock()),
        patch(
            f"{PKG}.live_view.async_start_watch", AsyncMock(return_value=session_info)
        ),
        patch(f"{PKG}.live_view.async_send_app_ping", AsyncMock()),
        patch(f"{PKG}.live_view._describe_channel", side_effect=describe),
    ):
        assert await runtime.async_start(DID) is False

    manager = runtime.manager
    assert seen_http == [manager.http]
    assert manager.status[DID] == "error"
    assert manager.errors[DID] == "stopped after the channel lookup"
    assert "StopIteration" not in (manager.errors[DID] or "")
    assert DID not in manager.sessions
    if foreign_live_view_data:
        # The foreign data is left alone
        assert set(hass.data["ecovacs_live"]) == {"other_entry"}
