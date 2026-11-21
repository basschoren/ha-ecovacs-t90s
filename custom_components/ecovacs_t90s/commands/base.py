"""Robot command that records how the robot answered.

deebot-client 18.7.0 reports a failed command only as an empty answer, so a
robot that rejects a command (``20003 rcp not support``) cannot be told apart
from a robot that is offline. ``T90Command`` is a small deebot-client
``JsonCommand`` (it goes through the normal device queue and the shared login)
that keeps the outcome, the error code and the answer data on the instance.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
import logging
from typing import TYPE_CHECKING, Any

from deebot_client.commands.json.common import JsonCommand
from deebot_client.message import HandlingResult, HandlingState

if TYPE_CHECKING:
    from deebot_client.authentication import Authenticator
    from deebot_client.event_bus import EventBus
    from deebot_client.models import ApiDeviceInfo

_LOGGER = logging.getLogger(__name__)

# Body code of a robot that does not know a command
CODE_NOT_SUPPORTED = 20003
# errno of the cloud API: robot offline / no answer from the robot in time
ERRNO_OFFLINE = 4200
ERRNO_NO_ANSWER = 500


class CommandOutcome(StrEnum):
    """How a robot command ended."""

    PENDING = "pending"
    OK = "ok"
    # The robot answered with error code 20003 ("rcp not support")
    UNSUPPORTED = "unsupported"
    # The robot answered with another error code
    REJECTED = "rejected"
    # The cloud reports the robot as offline (errno 4200)
    OFFLINE = "offline"
    # The cloud got no answer from the robot in time (errno 500) or timed out
    NO_ANSWER = "no_answer"
    # The request to the Ecovacs cloud failed (network, DNS, HTTP error)
    CONNECTION = "connection"

    @property
    def is_connection_problem(self) -> bool:
        """Return True if the robot could not be reached through the cloud."""
        return self in (
            CommandOutcome.PENDING,
            CommandOutcome.OFFLINE,
            CommandOutcome.NO_ANSWER,
            CommandOutcome.CONNECTION,
        )


def _to_int(value: Any) -> int | None:
    """Return value as int, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def classify_response(
    response: Any, ok_codes: frozenset[int] = frozenset({0})
) -> tuple[CommandOutcome, int | None, Any]:
    """Return outcome, error code and answer data of a device command answer.

    A robot answer looks like
    ``{"ret": "ok", "resp": {"header": {...}, "body": {"code": 0, "data": {...}}}}``;
    a cloud error like ``{"ret": "fail", "errno": 500, "error": "..."}``.
    The data is a dict for most commands, but a list for some (for example
    ``getQuickCommand``); anything else is returned as an empty dict.
    """
    if not isinstance(response, Mapping):
        return CommandOutcome.REJECTED, None, {}

    if response.get("ret") != "ok":
        errno = _to_int(response.get("errno"))
        if errno == ERRNO_OFFLINE:
            return CommandOutcome.OFFLINE, errno, {}
        if errno == ERRNO_NO_ANSWER:
            return CommandOutcome.NO_ANSWER, errno, {}
        return CommandOutcome.REJECTED, errno, {}

    resp = response.get("resp", response)
    body = resp.get("body") if isinstance(resp, Mapping) else None
    if not isinstance(body, Mapping):
        return CommandOutcome.OK, None, {}

    data = body.get("data")
    if isinstance(data, Mapping):
        data = dict(data)
    elif not isinstance(data, list):
        data = {}
    code = _to_int(body.get("code"))
    if code is None or code in ok_codes:
        return CommandOutcome.OK, code, data
    if code == CODE_NOT_SUPPORTED:
        return CommandOutcome.UNSUPPORTED, code, data
    return CommandOutcome.REJECTED, code, data


class T90Command(JsonCommand):
    """JSON command with explicit name and arguments that records its outcome.

    The answer is not fed into the event handling of deebot-client.
    """

    NAME = "t90Command"

    def __init__(
        self,
        name: str,
        args: dict[str, Any] | list[Any] | None = None,
        *,
        ok_codes: frozenset[int] = frozenset({0}),
    ) -> None:
        """Initialize the command."""
        self.NAME = name
        super().__init__(args)
        self._ok_codes = ok_codes
        self.outcome = CommandOutcome.PENDING
        self.code: int | None = None
        # body.data of the answer: a dict, or a list for some commands
        self.data: Any = {}
        self.response: dict[str, Any] = {}

    @property
    def args(self) -> dict[str, Any] | list[Any]:
        """Return the arguments sent to the robot (body.data)."""
        return self._args

    async def _execute(
        self,
        authenticator: Authenticator,
        device_info: ApiDeviceInfo,
        event_bus: EventBus,
    ) -> tuple[HandlingResult, dict[str, Any]]:
        """Execute the command and remember connection problems."""
        self.outcome = CommandOutcome.PENDING
        try:
            result = await super()._execute(authenticator, device_info, event_bus)
        except Exception:
            self.outcome = CommandOutcome.CONNECTION
            raise
        if self.outcome is CommandOutcome.PENDING:
            # deebot-client caught an ApiTimeoutError
            self.outcome = CommandOutcome.NO_ANSWER
        return result

    def _handle_response(
        self, event_bus: EventBus, response: dict[str, Any]
    ) -> HandlingResult:
        """Record the answer."""
        self.response = dict(response) if isinstance(response, Mapping) else {}
        self.outcome, self.code, self.data = classify_response(response, self._ok_codes)
        if self.outcome is CommandOutcome.OK:
            return HandlingResult.success()
        _LOGGER.debug(
            "Command %s %s: %s (code %s)",
            self.NAME,
            self._args,
            self.outcome,
            self.code,
        )
        return HandlingResult(HandlingState.FAILED)

    def __eq__(self, obj: object) -> bool:
        """Compare name and arguments."""
        return (
            isinstance(obj, T90Command)
            and self.NAME == obj.NAME
            and self._args == obj._args
        )

    __hash__ = object.__hash__
