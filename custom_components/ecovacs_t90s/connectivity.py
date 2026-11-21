"""Whether a robot can be reached through the Ecovacs cloud.

deebot-client has no public "connected" flag. It exposes:

- ``AvailabilityEvent``: a REST check (``getBattery``) every 60 seconds while
  no message arrived; it is also set to available by every MQTT message and
  every answered command, and to unavailable when the cloud reports the robot
  offline (errno 4200) or the check gets no answer;
- ``MqttClient.last_message_received_at``: when the last MQTT message came in
  (any robot of the account).

The connection counts as down when the last ``AvailabilityEvent`` said
unavailable, or when a robot command of this integration failed because the
cloud could not be reached, reported the robot offline or got no answer. The
second case reacts at once instead of up to a minute later. It is cleared by
the next MQTT message of the robot, any answered command, an availability
change, or a recovery check (``getBattery``) every 30 seconds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
import logging
from typing import Any

from deebot_client.device import Device
from deebot_client.events import AvailabilityEvent

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

RECOVERY_INTERVAL = 30.0


class CloudConnection:
    """Cloud connection state of one robot."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: Device,
        recovery_probe: Callable[[], Awaitable[bool]],
        create_task: Callable[[Awaitable[Any], str], Any],
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._device = device
        self._recovery_probe = recovery_probe
        self._create_task = create_task
        self.robot_available: bool | None = None
        self.failed_since: datetime | None = None
        self.failure_reason: str | None = None
        self.last_success: datetime | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._recovery_unsub: CALLBACK_TYPE | None = None

    @property
    def connected(self) -> bool:
        """Return True unless the robot is known to be unreachable."""
        return self.robot_available is not False and self.failed_since is None

    @callback
    def async_start(self) -> None:
        """Follow the availability of the robot."""

        async def on_availability(event: AvailabilityEvent) -> None:
            changed = self.robot_available != event.available
            self.robot_available = event.available
            if event.available:
                self.last_success = dt_util.utcnow()
                if self.failed_since is not None:
                    self._clear_failure()
                    changed = True
            if changed:
                self._notify()

        self._unsubscribe = self._device.events.subscribe(
            AvailabilityEvent, on_availability
        )

    @callback
    def async_stop(self) -> None:
        """Stop following the robot."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._cancel_recovery()
        self._listeners.clear()

    @callback
    def async_subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call listener when the connection state changes."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @callback
    def report_success(self) -> None:
        """Note that the robot answered through the cloud."""
        self.last_success = dt_util.utcnow()
        if self.failed_since is not None:
            _LOGGER.debug("Robot reachable through the Ecovacs cloud again")
            self._clear_failure()
            self._notify()

    @callback
    def report_failure(self, reason: str) -> None:
        """Note that a command could not reach the robot."""
        self.failure_reason = reason
        if self.failed_since is not None:
            return
        _LOGGER.debug("Robot not reachable through the Ecovacs cloud: %s", reason)
        self.failed_since = dt_util.utcnow()
        self._schedule_recovery()
        self._notify()

    def _clear_failure(self) -> None:
        self.failed_since = None
        self.failure_reason = None
        self._cancel_recovery()

    def _schedule_recovery(self) -> None:
        self._cancel_recovery()

        @callback
        def run(_now: datetime) -> None:
            self._recovery_unsub = None
            self._create_task(self._async_recover(), "ecovacs_t90s_cloud_recovery")

        self._recovery_unsub = async_call_later(self._hass, RECOVERY_INTERVAL, run)

    def _cancel_recovery(self) -> None:
        if self._recovery_unsub is not None:
            self._recovery_unsub()
            self._recovery_unsub = None

    async def _async_recover(self) -> None:
        if self.failed_since is None:
            return
        if await self._recovery_probe():
            self.report_success()
        elif self.failed_since is not None:
            self._schedule_recovery()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def diagnostics(self) -> dict[str, Any]:
        """Return the state for diagnostics."""
        return {
            "connected": self.connected,
            "robot_available": self.robot_available,
            "failed_since": self.failed_since.isoformat()
            if self.failed_since
            else None,
            "failure_reason": self.failure_reason,
            "last_success": self.last_success.isoformat()
            if self.last_success
            else None,
        }
