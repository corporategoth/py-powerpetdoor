# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Protocol handler for Power Pet Door simulator.

This module contains the asyncio protocol for handling client connections
and the command registry for dispatching commands to handlers.

Door motion itself lives in :mod:`powerpetdoor.simulator.engine` - the
protocol only translates wire commands into engine calls so behavior is
identical whether or not a client is connected.
"""

import asyncio
import json
import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..const import (
    CMD_CLOSE,
    CMD_DELETE_SCHEDULE,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SENSORS,
    CMD_GET_SETTINGS,
    CMD_GET_TIME,
    CMD_HAS_REMOTE_ID,
    CMD_HAS_REMOTE_KEY,
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_TIMEZONE,
    COMMAND,
    COMMAND_ENVELOPE_COMMANDS,
    CONFIG,
    DOOR_STATUS,
    DOOR_TO_PHONE,
    FIELD_AC_PRESENT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD,
    FIELD_DIRECTION,
    FIELD_DOOR_STATUS,
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
    FIELD_FWINFO,
    FIELD_HAS_REMOTE_ID,
    FIELD_HAS_REMOTE_KEY,
    FIELD_HOLD_TIME,
    FIELD_HW_REVISION,
    FIELD_HW_VERSION,
    FIELD_INDEX,
    FIELD_INSIDE,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_MSG_ID,
    FIELD_MSG_ID_RESPONSE,
    FIELD_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_REASON,
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SETTINGS,
    FIELD_SUCCESS,
    FIELD_TIME,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    FIELD_VOLTAGE,
    PING,
    PONG,
    SUCCESS_FALSE,
    SUCCESS_TRUE,
)
from ..framing import FrameScanner
from ..i18n import t
from ..sanitize import MAX_LOGGED_LENGTH, sanitize_field, sanitize_text
from ..schedule import MAX_SCHEDULE_INDEX, wire_bool_string, wire_int_flag
from ..tz_utils import parse_posix_tz_string
from .engine import DoorMotionEngine
from .state import DoorSimulatorState, Schedule
from .values import VALUES
from .wire_values import (
    IGNORED_TRIGGER_VOLTAGE,
    MAX_HOLD_TIME_CENTISECONDS,
    MAX_TIMEZONE_LENGTH,
    MAX_TRIGGER_VOLTAGE,
    WIRE_SWITCHES,
    WIRE_VALUES,
    notifications_payload,
    read,
    settings_payload,
)

if TYPE_CHECKING:
    from .server import DoorSimulator

logger = logging.getLogger(__name__)

#: Network-derived strings are stripped of terminal control characters before
#: they reach any log, so escape sequences cannot reach operator consoles.
#: The single implementation lives in the library package
#: (:mod:`powerpetdoor.sanitize`) and is shared with the client library and
#: the interactive front end.
sanitize_log_text = sanitize_text


class WireValueError(ValueError):
    """An untrusted ``SET_*`` field failed validation.

    Always raised *before* any state is mutated, so a single hostile packet
    can never leave the simulator holding a value that a later command
    chokes on (``inf`` hold time breaks every subsequent ``GET_SETTINGS``;
    a list timezone breaks every schedule evaluation). ``_handle_message``
    turns it into the standard error envelope, carrying this message as the
    ``reason``.
    """


def _coerce_wire_number(value: object, name: str, minimum: float, maximum: float) -> float:
    """Coerce an untrusted numeric wire field to a finite value in range.

    JSON permits ``Infinity``/``NaN`` - Python's ``json.loads`` accepts both
    the literals and ``1e400`` - and neither survives contact with the rest
    of the simulator, so they must be rejected here rather than stored.

    Raises:
        WireValueError: If the value is not a finite number in range.
    """
    # bool is an int subclass; `true` is not a number on this wire.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WireValueError(
            t(
                "simulator.protocol.must_number_got",
                "{name} must be a number, got {value!r}",
                name=name,
                value=value,
            )
        )
    if not math.isfinite(value):
        raise WireValueError(
            t(
                "simulator.protocol.must_finite_number_got",
                "{name} must be a finite number, got {value!r}",
                name=name,
                value=value,
            )
        )
    if not minimum <= value <= maximum:
        raise WireValueError(
            t(
                "simulator.protocol.must_between_got",
                "{name} must be between {minimum} and {maximum}, got {value!r}",
                name=name,
                minimum=minimum,
                maximum=maximum,
                value=value,
            )
        )
    return float(value)


def _coerce_wire_int(value: object, name: str, minimum: int, maximum: int) -> int:
    """Coerce an untrusted wire field to an int in ``minimum..maximum``.

    Raises:
        WireValueError: If the value is not a finite number in range.
    """
    return int(_coerce_wire_number(value, name, minimum, maximum))


def _coerce_wire_string(value: object, name: str, max_length: int) -> str:
    """Coerce an untrusted wire field to a bounded string.

    Raises:
        WireValueError: If the value is not a string, or is too long.
    """
    if not isinstance(value, str):
        raise WireValueError(
            t(
                "simulator.protocol.must_string_got",
                "{name} must be a string, got {value!r}",
                name=name,
                value=value,
            )
        )
    if len(value) > max_length:
        raise WireValueError(
            t(
                "simulator.protocol.must_most_characters_got",
                "{name} must be at most {max_length} characters, got {arg0}",
                name=name,
                max_length=max_length,
                arg0=len(value),
            )
        )
    return value


def _version_parts(version: object, count: int) -> list[int]:
    """Split a dotted version into the ints the wire carries.

    The registry keeps a version as the one string every other surface
    shows - `1.7.18` - so the wire's five separate integer fields are a
    translation, not a second copy of the value.
    """
    parts = [int(part) for part in str(version).split(".")]
    return (parts + [0] * count)[:count]


class CommandRegistry:
    """Registry for command handlers.

    This class provides a decorator-based registration system for command
    handlers. Handlers are registered at class definition time and can be
    looked up by command name at runtime.
    """

    _handlers: dict[str, Callable] = {}

    @classmethod
    def handler(cls, cmd: str):
        """Decorator to register a command handler.

        Usage:
            @CommandRegistry.handler(CMD_GET_SETTINGS)
            async def handle_get_settings(self, msg, response):
                response[FIELD_SETTINGS] = settings_payload(self.state)
        """

        def decorator(func):
            cls._handlers[cmd] = func
            return func

        return decorator

    @classmethod
    def get(cls, cmd: str) -> Callable | None:
        """Get the handler for a command, or None if not found."""
        return cls._handlers.get(cmd)


class DoorSimulatorProtocol(asyncio.Protocol):
    """Protocol handler for simulated door connections."""

    def __init__(
        self,
        state: DoorSimulatorState,
        on_command: Callable[[str, dict], None] | None = None,
        broadcast_status: Callable[[], None] | None = None,
        on_disconnect: Callable[["DoorSimulatorProtocol"], None] | None = None,
        engine: DoorMotionEngine | None = None,
        simulator: "DoorSimulator | None" = None,
    ):
        self.state = state
        self.on_command = on_command
        self.broadcast_status = broadcast_status
        self.on_disconnect = on_disconnect
        self.transport: asyncio.Transport | None = None
        # One scanner per connection, carried across data_received() calls
        # so a client dribbling a never-terminated object cannot make the
        # daemon re-scan its retained buffer every time.
        self._scanner = FrameScanner()
        self._tasks: set[asyncio.Task] = set()
        #: The simulator this connection belongs to. Wire-driven changes
        #: go through its value registry and its door methods, so they
        #: have exactly the side effects a change from the prompt has.
        #:
        #: A connection made outside a server - the protocol unit tests -
        #: gets one built over the same state rather than a second,
        #: simpler way of applying a value. Two ways to apply one value is
        #: how the wire and the prompt drifted apart to begin with; there
        #: is now only the one.
        adopted = simulator is None
        if simulator is None:
            from .server import DoorSimulator as _DoorSimulator

            simulator = _DoorSimulator(state=state)
        self.simulator: DoorSimulator = simulator
        self._owns_engine = engine is None
        self.engine = engine or DoorMotionEngine(
            state,
            broadcast_status=self._broadcast_or_send_status,
            # A pet reaching a sensor raises a notification, which the
            # simulator counts, logs and delivers. It is deliberately not
            # a wire message - see simulator/notifications.py.
            notify_sensor=simulator._notify_sensor_reached,
        )
        if adopted:
            simulator.engine = self.engine

    @property
    def buffer(self) -> str:
        """The framing scanner's un-parsed remainder (introspection hook)."""
        return self._scanner.buffer

    def connection_made(self, transport):
        peername = transport.get_extra_info("peername")
        logger.info(
            t(
                "simulator.protocol.simulator_client_connected",
                "Simulator: Client connected from %s",
            ),
            peername,
        )
        self.transport = transport

    def connection_lost(self, exc):
        logger.info(
            t("simulator.protocol.simulator_client_disconnected", "Simulator: Client disconnected")
        )
        self._scanner.reset()
        for task in list(self._tasks):
            task.cancel()
        if self._owns_engine:
            self.engine.cancel_nowait()
        if self.on_disconnect:
            self.on_disconnect(self)

    async def aclose(self) -> None:
        """Cancel and await all tasks owned by this protocol."""
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_engine:
            await self.engine.stop()
        if self.transport:
            self.transport.close()

    async def drain(self) -> None:
        """Wait until all in-flight message-handler tasks complete.

        Deterministic synchronization hook for tests: after feeding data via
        :meth:`data_received`, ``await protocol.drain()`` guarantees every
        received message has been fully handled (responses written).
        """
        while True:
            pending = [task for task in self._tasks if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def data_received(self, data: bytes):
        # Escape non-ASCII bytes instead of dropping the chunk: dropping it
        # would strand a half-buffered frame and wedge framing until the
        # 64 KiB overflow disconnect. The affected frame fails json.loads
        # and is skipped on its own; later frames still arrive.
        text = data.decode("ascii", errors="backslashreplace")
        if len(text) != len(data):
            logger.warning(
                t(
                    "simulator.protocol.simulator_escaped_non_ascii_bytes",
                    "Simulator: escaped non-ASCII bytes in a received chunk",
                )
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                t("simulator.protocol.simulator_rx", "Simulator RX: %s"), sanitize_log_text(text)
            )

        frames, diag = self._scanner.feed(text)
        if diag.overflow:
            self._drop_connection("receive buffer overflowed without a complete message")
            return

        for frame in frames:
            try:
                msg = json.loads(frame)
            except (ValueError, RecursionError) as err:
                # Twin of the client's clause, widened for the same reason:
                # a >4300-digit integer literal raises a bare ValueError and
                # deep nesting raises RecursionError, neither of which is a
                # JSONDecodeError, and letting either escape fatal-errors
                # the transport and holds the fd plus the
                # `DoorSimulator.protocols` slot after the client walks away.
                logger.warning(
                    t(
                        "simulator.protocol.simulator_json_parse_error_frame",
                        "Simulator: JSON parse error: %s (frame: %s)",
                    ),
                    err,
                    sanitize_field(frame, MAX_LOGGED_LENGTH),
                )
                continue
            self._create_task(self._handle_message(msg))

    def _create_task(self, coro) -> asyncio.Task:
        """Create a tracked task so it can be drained/cancelled later."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                t(
                    "simulator.protocol.simulator_message_handler_task_failed",
                    "Simulator: message handler task failed",
                ),
                exc_info=task.exception(),
            )

    def _report_unknown_command(self, cmd: object) -> None:
        """Log an unknown command."""
        logger.warning(
            t("simulator.protocol.simulator_unknown_command", "Simulator: Unknown command: %s"),
            sanitize_field(cmd, MAX_LOGGED_LENGTH),
        )

    def _send(self, msg: dict):
        """Send a message to the client."""
        data = json.dumps(msg).encode("ascii")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                t("simulator.protocol.simulator_tx", "Simulator TX: %s"),
                sanitize_log_text(str(msg)),
            )
        if not self.transport:
            return
        self.transport.write(data)

    def _drop_connection(self, reason: str) -> None:
        """Abort this connection after a declared protocol violation.

        ``transport.close()`` only sets ``_closing`` and removes the
        reader; ``connection_lost`` is deferred until the write buffer
        drains, and a client holding a zero TCP window never lets it - so
        the protocol object and its slot in ``DoorSimulator.protocols``
        would be held for the life of the daemon, and ``ctl status`` would
        keep reporting a client the daemon's own ERROR said it had dropped.

        ``abort()`` discards the unsent tail and delivers
        ``connection_lost`` immediately.
        """
        logger.error(
            t(
                "simulator.protocol.simulator_dropping_connection",
                "Simulator: %s; dropping the connection",
            ),
            reason,
        )
        if self.transport:
            self.transport.abort()

    def _check_command_allowed(self, cmd: str) -> tuple[bool, str]:
        """Check if a command is allowed given current state.

        Returns (allowed, reason).
        """
        # Power must be on for door commands
        if cmd in COMMAND_ENVELOPE_COMMANDS and not read(self.state, "power"):
            return False, "Power is OFF"

        # Command lockout blocks remote commands when enabled
        if read(self.state, "cmd_lockout") and cmd in COMMAND_ENVELOPE_COMMANDS:
            return False, "Command lockout is enabled"

        return True, ""

    async def _handle_message(self, msg: dict):
        """Handle an incoming message."""
        msg_id = msg.get(FIELD_MSG_ID)

        # Handle PING
        if PING in msg:
            self._send(
                {
                    FIELD_CMD: PONG,
                    PONG: msg[PING],
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )
            return

        # Queries and configuration arrive under "config"; door motion
        # arrives under "cmd". Which key was used is not cosmetic - see
        # `COMMAND_ENVELOPE_COMMANDS` and the check below.
        envelope = CONFIG
        cmd = msg.get(CONFIG)
        if not cmd:
            envelope = COMMAND
            cmd = msg.get(COMMAND)
        if not cmd:
            return

        response = {FIELD_CMD: cmd, FIELD_SUCCESS: SUCCESS_TRUE, FIELD_DIRECTION: DOOR_TO_PHONE}
        if msg_id is not None:
            response[FIELD_MSG_ID_RESPONSE] = msg_id

        # Non-string commands cannot match any handler; answer the error
        # envelope without touching the (string-keyed) registry.
        if not isinstance(cmd, str):
            self._report_unknown_command(cmd)
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Unknown command"
            self._send_response(response)
            return

        if self.on_command:
            self.on_command(cmd, msg)

        # Only door motion is a "cmd".
        # `{"cmd": "ENABLE_INSIDE"}` is answered success:"false" by a real
        # door while `{"config": "ENABLE_INSIDE"}` succeeds. Reproduced so
        # a caller that picks the wrong envelope key fails here rather than
        # only against hardware.
        if envelope == COMMAND and cmd not in COMMAND_ENVELOPE_COMMANDS:
            logger.warning(
                t(
                    "simulator.protocol.simulator_sent_as_real_door",
                    "Simulator: %s was sent as %r; a real door only accepts %s that way",
                ),
                sanitize_field(cmd, MAX_LOGGED_LENGTH),
                COMMAND,
                "/".join(sorted(COMMAND_ENVELOPE_COMMANDS)),
            )
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = f"{cmd} must be sent as {CONFIG!r}, not {COMMAND!r}"
            self._send_response(response)
            return

        # Check if command is allowed
        allowed, reason = self._check_command_allowed(cmd)
        if not allowed:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = reason
            self._send_response(response)
            return

        # Look up and execute handler
        handler = CommandRegistry.get(cmd)
        if handler is None:
            self._report_unknown_command(cmd)
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Unknown command"
            self._send_response(response)
            return

        try:
            await handler(self, msg, response)
        except WireValueError as err:
            # A deliberate rejection: the handler validated an untrusted
            # field and refused it before touching state. Report the actual
            # reason so a legitimate client can fix its payload.
            logger.warning(
                t("simulator.protocol.simulator_rejected", "Simulator: Rejected %s: %s"),
                sanitize_field(cmd, MAX_LOGGED_LENGTH),
                sanitize_field(err, MAX_LOGGED_LENGTH),
            )
            response = self._error_envelope(cmd, str(err))
        except Exception:
            logger.exception(
                t(
                    "simulator.protocol.simulator_error_handling_command",
                    "Simulator: Error handling command %s",
                ),
                sanitize_field(cmd),
            )
            response = self._error_envelope(cmd, "Command failed")

        self._send_response(response)

    @staticmethod
    def _error_envelope(cmd: str, reason: str) -> dict:
        """Build the standard failure envelope for ``cmd``.

        No ``msgID``: see :meth:`_send_response`.
        """
        return {
            FIELD_CMD: cmd,
            FIELD_SUCCESS: SUCCESS_FALSE,
            FIELD_DIRECTION: DOOR_TO_PHONE,
            FIELD_REASON: reason,
        }

    def _send_response(self, response: dict) -> None:
        """Send a command response, dropping ``msgID`` when it failed.

        A real door echoes ``msgId``
        back as ``msgID`` on success, and omits it entirely on failure -
        the observed shape is ``{"success": "false", "dir": "d2p",
        "CMD": "..."}``. A client that pairs replies to requests by id
        therefore cannot pair a failure with anything and waits out its own
        timeout, which is why
        :meth:`~powerpetdoor.client.PowerPetDoorClient.process_message`
        falls back to failing the in-flight command.

        Emulating this is the point: a client that only handles
        id-matched failures passes against a simulator that echoes the id
        and then hangs against the real door.
        """
        if response.get(FIELD_SUCCESS) != SUCCESS_TRUE:
            response.pop(FIELD_MSG_ID_RESPONSE, None)
        self._send(response)

    # ==========================================================================
    # Command Handlers - Get Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_GET_SETTINGS)
    async def _handle_get_settings(self, msg: dict, response: dict) -> None:
        response[FIELD_SETTINGS] = settings_payload(self.state)

    @CommandRegistry.handler(CMD_GET_DOOR_STATUS)
    async def _handle_get_door_status(self, msg: dict, response: dict) -> None:
        response[FIELD_DOOR_STATUS] = read(self.state, "door_status")

    # A real door spells the same concept differently per command, and the
    # simulator reproduces that rather than normalizing it (see
    # docs/protocol.md, "Value spellings"). Two rules cover every handler
    # below:
    #
    # * a top-level flag is spelled per FIELD, not by a single rule:
    #   `GET_SENSORS` answers `inside`/`outside` as the ints 1/0, while
    #   `GET_POWER` and `GET_TIMERS_ENABLED` answer `power_state` and
    #   `timersEnabled` as the strings "true"/"false". The wire table
    #   carries which is which;
    # * a field carried inside a `settings` object is spelled the way
    #   GET_SETTINGS spells it - "true"/"false" strings, except doorOptions
    #   which is an int.

    @CommandRegistry.handler(CMD_GET_SENSORS)
    async def _handle_get_sensors(self, msg: dict, response: dict) -> None:
        response[FIELD_INSIDE] = wire_int_flag(read(self.state, "inside"))
        response[FIELD_OUTSIDE] = wire_int_flag(read(self.state, "outside"))

    @CommandRegistry.handler(CMD_GET_HW_INFO)
    async def _handle_get_hw_info(self, msg: dict, response: dict) -> None:
        # The registry keeps these as dotted strings, which is how every
        # other surface shows them; the wire splits them into five ints.
        fw_major, fw_minor, fw_patch = _version_parts(read(self.state, "firmware_version"), 3)
        hw_ver, hw_rev = _version_parts(read(self.state, "hardware_version"), 2)
        response[FIELD_FWINFO] = {
            FIELD_FW_MAJOR: fw_major,
            FIELD_FW_MINOR: fw_minor,
            FIELD_FW_PATCH: fw_patch,
            FIELD_HW_VERSION: hw_ver,
            FIELD_HW_REVISION: hw_rev,
        }

    @CommandRegistry.handler(CMD_GET_DOOR_BATTERY)
    async def _handle_get_battery(self, msg: dict, response: dict) -> None:
        present = read(self.state, "battery_present")
        # Report 0% if battery is not present
        response[FIELD_BATTERY_PERCENT] = read(self.state, "battery") if present else 0
        # "true"/"false" strings - `batteryPercent` alongside them is an int.
        response[FIELD_BATTERY_PRESENT] = wire_bool_string(present)
        response[FIELD_AC_PRESENT] = wire_bool_string(read(self.state, "ac_present"))

    @CommandRegistry.handler(CMD_GET_DOOR_OPEN_STATS)
    async def _handle_get_stats(self, msg: dict, response: dict) -> None:
        response[FIELD_TOTAL_OPEN_CYCLES] = read(self.state, "total_open_cycles")
        response[FIELD_TOTAL_AUTO_RETRACTS] = read(self.state, "total_auto_retracts")

    @CommandRegistry.handler(CMD_GET_NOTIFICATIONS)
    async def _handle_get_notifications(self, msg: dict, response: dict) -> None:
        response[FIELD_NOTIFICATIONS] = notifications_payload(self.state)

    @CommandRegistry.handler(CMD_GET_TIME)
    async def _handle_get_time(self, msg: dict, response: dict) -> None:
        """Answer the door's own wall clock, in its configured timezone.

        Undocumented by the vendor,
        but present, and worth having because schedules are evaluated
        against this clock - it is the only way to check that a door will
        fire a schedule when you expect it to.
        """
        # Through the registry, so the wire's answer and `get time` are
        # the same computation rather than two that happen to agree.
        response[FIELD_TIME] = VALUES["time"].get(self.state)

    # ==========================================================================
    # Command Handlers - Schedule Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_GET_SCHEDULE_LIST)
    async def _handle_get_schedule_list(self, msg: dict, response: dict) -> None:
        response[FIELD_SCHEDULES] = sorted(self.simulator.get_schedules())

    @staticmethod
    def _wire_schedule_index(msg: dict) -> int | None:
        """Validate the untrusted ``index`` of an index-addressed command.

        Used as a dict key, so a JSON container raises ``TypeError:
        unhashable type`` and one packet becomes a full traceback at ERROR
        plus a useless "Command failed" reason.

        Returns:
            The index, or None when the field is absent.

        Raises:
            WireValueError: If present but not an integer in range.
        """
        if FIELD_INDEX not in msg or msg[FIELD_INDEX] is None:
            return None
        return _coerce_wire_int(msg[FIELD_INDEX], FIELD_INDEX, 0, MAX_SCHEDULE_INDEX)

    @CommandRegistry.handler(CMD_GET_SCHEDULE)
    async def _handle_get_schedule(self, msg: dict, response: dict) -> None:
        index = self._wire_schedule_index(msg)
        schedule = None if index is None else self.simulator.get_schedule(index)
        if schedule is not None:
            response[FIELD_SCHEDULE] = schedule.to_dict()
        else:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Schedule not found"

    @CommandRegistry.handler(CMD_SET_SCHEDULE)
    async def _handle_set_schedule(self, msg: dict, response: dict) -> None:
        schedule_data = msg.get(FIELD_SCHEDULE)
        if not schedule_data:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Missing schedule"
            return
        # The slot index must be sent alongside
        # the schedule object. Sending only "schedule" is answered
        # success:"false" and writes nothing.
        if FIELD_INDEX not in msg:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Missing index"
            return
        try:
            schedule = Schedule.from_dict(schedule_data)
        except ValueError as err:
            # Untrusted wire data: reject malformed schedules rather than
            # storing something that raises later during evaluation.
            logger.warning(
                t(
                    "simulator.protocol.simulator_rejected_schedule",
                    "Simulator: Rejected schedule: %s",
                ),
                sanitize_field(err, MAX_LOGGED_LENGTH),
            )
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = str(err)
            return
        self._store_schedule(schedule)
        response[FIELD_SCHEDULE] = schedule.to_dict()
        logger.info(
            t("simulator.protocol.simulator_schedule_saved", "Simulator: Schedule %s saved"),
            sanitize_log_text(schedule.index),
        )

    @CommandRegistry.handler(CMD_DELETE_SCHEDULE)
    async def _handle_delete_schedule(self, msg: dict, response: dict) -> None:
        index = self._wire_schedule_index(msg)
        if index is not None and self.simulator.get_schedule(index) is not None:
            self._drop_schedule(index)
            # The real device echoes the deleted index in the response
            response[FIELD_INDEX] = index
            logger.info(
                t(
                    "simulator.protocol.simulator_schedule_deleted",
                    "Simulator: Schedule %s deleted",
                ),
                sanitize_log_text(index),
            )
        else:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Schedule not found"

    # ==========================================================================
    # Command Handlers - Remote/Reset Info
    # ==========================================================================

    @CommandRegistry.handler(CMD_HAS_REMOTE_ID)
    async def _handle_has_remote_id(self, msg: dict, response: dict) -> None:
        # The field is `has_id`, and its value is a "true"/"false" string.
        response[FIELD_HAS_REMOTE_ID] = wire_bool_string(read(self.state, "has_remote_id"))

    @CommandRegistry.handler(CMD_HAS_REMOTE_KEY)
    async def _handle_has_remote_key(self, msg: dict, response: dict) -> None:
        response[FIELD_HAS_REMOTE_KEY] = wire_bool_string(read(self.state, "has_remote_key"))

    # ==========================================================================
    # Command Handlers - Door Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_OPEN)
    async def _handle_open(self, msg: dict, response: dict) -> None:
        await self.simulator.open_door(hold=False)
        response[FIELD_DOOR_STATUS] = read(self.state, "door_status")

    @CommandRegistry.handler(CMD_OPEN_AND_HOLD)
    async def _handle_open_and_hold(self, msg: dict, response: dict) -> None:
        await self.simulator.open_door(hold=True)
        response[FIELD_DOOR_STATUS] = read(self.state, "door_status")

    @CommandRegistry.handler(CMD_CLOSE)
    async def _handle_close(self, msg: dict, response: dict) -> None:
        await self.simulator.close_door()
        response[FIELD_DOOR_STATUS] = read(self.state, "door_status")

    # ==========================================================================
    # Command Handlers - Enable/Disable Commands
    # ==========================================================================

    def _store_schedule(self, schedule: Schedule) -> None:
        """Store a schedule the way every other source stores one."""
        self.simulator.add_schedule(schedule, announce=False)

    def _drop_schedule(self, index: int) -> None:
        """Delete a schedule the way every other source deletes one."""
        self.simulator.remove_schedule(index, announce=False)

    def _apply_value(self, name: str, value: object) -> None:
        """Apply a wire-driven change through the shared value registry.

        The registry is what the CLI and the script DSL write through, so
        routing the wire through it too means a change has the same side
        effects whoever made it: enabling a sensor re-asks whether a pet
        already waiting at it may now come in, which a handler that only
        assigned to `state` did not.

        ``announce=False`` because a wire command answers in its own
        response; broadcasting as well would tell the requester twice.

        Raises:
            KeyError: If ``name`` is not a device value. The wire carries
                what a real door carries; the simulation's own knobs -
                flap timings, battery rates - have no wire spelling and
                must not acquire one through this door.
        """
        if VALUES[name].simulation_only:
            raise KeyError(name)
        VALUES[name].apply(self.simulator, value, announce=False)

    # ==========================================================================
    # Command Handlers - Set Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_SET_TIMEZONE)
    async def _handle_set_timezone(self, msg: dict, response: dict) -> None:
        if FIELD_TZ in msg:
            # A non-string here (a list, a dict, an int) is stored happily
            # and then breaks GET_SETTINGS and every schedule evaluation for
            # the life of the process - validate before assigning.
            timezone = _coerce_wire_string(msg[FIELD_TZ], FIELD_TZ, MAX_TIMEZONE_LENGTH)
            # The wire carries POSIX in both directions - `GET_TIMEZONE`,
            # `GET_SETTINGS` and this command's own reply all answer
            # `EST5EDT,M3.2.0,M11.1.0`, never an IANA name. So an IANA
            # name arriving here is a client that has not converted, and
            # accepting it would let that client pass against the
            # simulator and then read back something it never sent.
            #
            # IANA names are the business of `door.py` and anything above
            # it, and of the simulator's own operator surfaces - the
            # prompt's `timezone` command takes either and converts.
            parsed = parse_posix_tz_string(timezone)
            if not parsed or not parsed.get("std_abbrev"):
                raise WireValueError(
                    t(
                        "simulator.protocol.must_posix",
                        "{FIELD_TZ} must be a POSIX TZ string "
                        "(e.g. EST5EDT,M3.2.0,M11.1.0), got {timezone!r}",
                        FIELD_TZ=FIELD_TZ,
                        timezone=timezone,
                    )
                )
            self._apply_value("timezone", timezone)
        response.update(WIRE_VALUES["timezone"].payload(self.state))

    @CommandRegistry.handler(CMD_SET_HOLD_TIME)
    async def _handle_set_hold_time(self, msg: dict, response: dict) -> None:
        if FIELD_HOLD_TIME in msg:
            # `holdTime: 1e400` divides cleanly to inf; storing it makes
            # every later int(hold_time * 100) raise and parks the door in
            # DOOR_HOLDING forever. Validate before assigning.
            centiseconds = _coerce_wire_number(
                msg[FIELD_HOLD_TIME], FIELD_HOLD_TIME, 0, MAX_HOLD_TIME_CENTISECONDS
            )
            # Convert centiseconds to seconds for internal storage
            self._apply_value("hold_time", centiseconds / 100.0)
            logger.info(
                t(
                    "simulator.protocol.simulator_hold_time_set_s",
                    "Simulator: Hold time set to %ss",
                ),
                read(self.state, "hold_time"),
            )
        response.update(WIRE_VALUES["hold_time"].payload(self.state))

    @CommandRegistry.handler(CMD_SET_NOTIFICATIONS)
    async def _handle_set_notifications(self, msg: dict, response: dict) -> None:
        # The flags must arrive inside a nested
        # "notifications" object. Top-level fields are answered
        # success:"false" and write nothing, and a nested object carrying any
        # value as a *string* is answered with the current settings but does
        # not apply. Both quirks are reproduced here on purpose: emulating
        # them is what makes a client that gets the shape wrong fail in tests
        # rather than only against real hardware.
        nested = msg.get(FIELD_NOTIFICATIONS)
        if not isinstance(nested, dict):
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Missing notifications"
            return
        if not all(isinstance(value, bool) for value in nested.values()):
            # Accepted, and silently NOT applied - exactly what the door
            # does, and the most dangerous behaviour in this protocol: the
            # reply is a normal success envelope carrying the *current*
            # settings, so a client that checks `success`, or even reads the
            # echoed settings back, sees a healthy write.
            response[FIELD_NOTIFICATIONS] = notifications_payload(self.state)
            return

        # Every value is a real JSON boolean by now, so there is nothing
        # left to coerce and nothing that can half-apply.
        # Through the value registry, as the prompt and a script do, so a
        # switch is flipped the same way whoever flipped it.
        fields = {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "inside_on",
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: "inside_off",
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: "outside_on",
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: "outside_off",
            FIELD_LOW_BATTERY_NOTIFICATIONS: "low_battery",
        }
        for field_name, notification in fields.items():
            if field_name in nested:
                self._apply_value(f"notify_{notification}", nested[field_name])
        response[FIELD_NOTIFICATIONS] = notifications_payload(self.state)

    def _wire_voltage(self, msg: dict) -> int:
        """Read the new threshold out of a voltage setter's message.

        Both setters take ``voltage``
        and reject the ``sensorTriggerVoltage`` /
        ``sleepSensorTriggerVoltage`` name their getters answer with - the
        one docs/protocol.md used to document. Emulated, so a caller that
        sends the getter's name fails here rather than only on hardware.

        Raises:
            WireValueError: If ``voltage`` is absent, or is not an integer
                in range.
        """
        if FIELD_VOLTAGE not in msg:
            raise WireValueError(
                t(
                    "simulator.protocol.required_1",
                    "{FIELD_VOLTAGE} is required",
                    FIELD_VOLTAGE=FIELD_VOLTAGE,
                )
            )
        # Bounded at the door rather than later, as holdTime is. The bound
        # SATURATES rather than refusing, because that is what the device
        # does: 2**32-1 comes back as 2**31-1 with success:"true".
        value = _coerce_wire_int(msg[FIELD_VOLTAGE], FIELD_VOLTAGE, -(2**63), 2**63 - 1)
        return min(max(value, 0), MAX_TRIGGER_VOLTAGE)

    @CommandRegistry.handler(CMD_SET_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_set_sensor_voltage(self, msg: dict, response: dict) -> None:
        voltage = self._wire_voltage(msg)
        # Measured: 0 is accepted and ignored, leaving the old value.
        if voltage != IGNORED_TRIGGER_VOLTAGE:
            self._apply_value("sensor_trigger_voltage", voltage)
        response.update(WIRE_VALUES["sensor_trigger_voltage"].payload(self.state))

    @CommandRegistry.handler(CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_set_sleep_voltage(self, msg: dict, response: dict) -> None:
        voltage = self._wire_voltage(msg)
        if voltage != IGNORED_TRIGGER_VOLTAGE:
            self._apply_value("sleep_sensor_trigger_voltage", voltage)
        response.update(WIRE_VALUES["sleep_sensor_trigger_voltage"].payload(self.state))

    # ==========================================================================
    # Door Operation Delegation (single engine - see engine.py)
    # ==========================================================================

    def trigger_sensor(self, sensor: str):
        """Simulate a sensor trigger (pet walking through).

        Args:
            sensor: "inside" or "outside"
        """
        self.engine.trigger_sensor(sensor)

    def simulate_obstruction(self, duration: float | None = None):
        """Place (or clear) a physical obstruction in the doorway."""
        self.engine.simulate_obstruction(duration)

    def _send_door_status(self):
        """Send unsolicited door status update to this client only."""
        self._send(
            {
                FIELD_CMD: DOOR_STATUS,
                FIELD_DOOR_STATUS: read(self.state, "door_status"),
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            }
        )

    def _broadcast_or_send_status(self):
        """Broadcast door status to all clients, or send to this client only."""
        if self.broadcast_status:
            self.broadcast_status()
        else:
            self._send_door_status()


def _switch_handler(name: str, enabled: bool):
    """One half of a value's enable/disable pair, from the wire table.

    Fourteen of these were written out by hand, and each was the same
    three lines: apply the value, answer with its payload, log it. The
    table already says which command carries which value and what the
    payload looks like, so the handler is derived from it rather than
    restated - and a new switch is one row, not two handlers plus two
    log strings.
    """

    async def handle(self: DoorSimulatorProtocol, msg: dict, response: dict) -> None:
        self._apply_value(name, enabled)
        response.update(WIRE_VALUES[name].payload(self.state))
        logger.info(
            t("simulator.protocol.simulator_setting", "Simulator: %s %s"),
            name,
            "ENABLED" if enabled else "DISABLED",
        )

    handle.__name__ = f"_handle_{'enable' if enabled else 'disable'}_{name}"
    return handle


def _getter_handler(name: str):
    """A ``GET_*`` that reads back exactly one value, from the wire table.

    The reply to such a getter *is* the value's payload - the same body
    the setter answers with and the same one a broadcast carries - so
    six handlers were six restatements of what the table already said.
    One of them had drifted: `GET_HOLD_TIME` did its own `* 100` rather
    than asking :func:`hold_time_centiseconds`, making it a third copy of
    the seconds-to-centiseconds conversion.

    Getters that report more than one value (`GET_SENSORS`) or something
    assembled (`GET_SETTINGS`, `GET_HW_INFO`) stay hand-written, because
    there is no single value for the table to name.
    """

    async def handle(self: DoorSimulatorProtocol, msg: dict, response: dict) -> None:
        response.update(WIRE_VALUES[name].payload(self.state))

    handle.__name__ = f"_handle_get_{name}"
    return handle


for _name, _wire in WIRE_VALUES.items():
    if _wire.getter is not None:
        CommandRegistry.handler(_wire.getter)(_getter_handler(_name))
del _name, _wire


for _name in WIRE_SWITCHES:
    _wire = WIRE_VALUES[_name]
    assert _wire.disable is not None  # WIRE_SWITCHES is defined by having one
    CommandRegistry.handler(_wire.enable)(_switch_handler(_name, True))
    CommandRegistry.handler(_wire.disable)(_switch_handler(_name, False))
del _name, _wire
