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
from datetime import datetime

from ..const import (
    CMD_CLOSE,
    CMD_DELETE_SCHEDULE,
    CMD_DISABLE_AUTO,
    CMD_DISABLE_AUTORETRACT,
    CMD_DISABLE_CMD_LOCKOUT,
    CMD_DISABLE_INSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_ENABLE_AUTO,
    CMD_ENABLE_AUTORETRACT,
    CMD_ENABLE_CMD_LOCKOUT,
    CMD_ENABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HOLD_TIME,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_POWER,
    CMD_GET_SCHEDULE,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_SENSORS,
    CMD_GET_SETTINGS,
    CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_TIME,
    CMD_GET_TIMEZONE,
    CMD_HAS_REMOTE_ID,
    CMD_HAS_REMOTE_KEY,
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_SCHEDULE_LIST,
    CMD_SET_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_TIME,
    CMD_SET_TIMEZONE,
    COMMAND,
    COMMAND_ENVELOPE_COMMANDS,
    CONFIG,
    DOOR_STATE_CLOSED,
    DOOR_STATUS,
    DOOR_TO_PHONE,
    FIELD_AC_PRESENT,
    FIELD_AUTO,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD,
    FIELD_CMD_LOCKOUT,
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
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_REASON,
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_STATE,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SETTINGS,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SUCCESS,
    FIELD_TIME,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    FIELD_VOLTAGE,
    NOTIFY_SENSOR_INDOOR,
    NOTIFY_SENSOR_OUTDOOR,
    PING,
    PONG,
    SENSOR_STATE_OFF,
    SENSOR_STATE_ON,
    SUCCESS_FALSE,
    SUCCESS_TRUE,
    TIME_FORMAT,
)
from ..framing import FrameScanner
from ..sanitize import MAX_LOGGED_LENGTH, sanitize_field, sanitize_text
from ..schedule import MAX_SCHEDULE_INDEX, wire_bool_string, wire_int_flag
from .engine import DoorMotionEngine
from .state import DoorSimulatorState, Schedule

logger = logging.getLogger(__name__)

#: Network-derived strings are stripped of terminal control characters before
#: they reach any log, so escape sequences cannot reach operator consoles.
#: The single implementation lives in the library package
#: (:mod:`powerpetdoor.sanitize`) and is shared with the client library and
#: the interactive front end.
sanitize_log_text = sanitize_text

#: Widest hold time (centiseconds) accepted from the wire; matches the
#: operator-side ``holdtime`` command's 900 s ceiling.
MAX_HOLD_TIME_CENTISECONDS = 90000
#: Longest ``SET_TIMEZONE`` string accepted from the wire. Real POSIX TZ
#: strings and IANA names are far shorter than this.
MAX_TIMEZONE_LENGTH = 128
#: Widest sensor trigger voltage accepted from the wire (millivolts).
MAX_TRIGGER_VOLTAGE = 65535


class SilentDropError(Exception):
    """Raised by a handler whose command the device answers with silence.

    Exactly one command behaves this way (``SET_TIME``), and it matters:
    every other rejected shape gets a ``success: "false"`` envelope, so a
    client that treats "no failure envelope" as success hangs. Raising
    rather than returning keeps the "every handler path ends in a
    response" rule visible at the one place it is broken.
    """


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
        raise WireValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise WireValueError(f"{name} must be a finite number, got {value!r}")
    if not minimum <= value <= maximum:
        raise WireValueError(f"{name} must be between {minimum} and {maximum}, got {value!r}")
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
        raise WireValueError(f"{name} must be a string, got {value!r}")
    if len(value) > max_length:
        raise WireValueError(f"{name} must be at most {max_length} characters, got {len(value)}")
    return value


def make_sensor_notification(
    state: DoorSimulatorState, sensor: str, sensor_state: str
) -> dict | None:
    """Build the bare notification envelope for a sensor event.

    Per docs/protocol.md "Notification Events", notification events use a
    bare envelope with no ``CMD``/``success``/``msgID``: the event name is a
    key with an empty-string value plus a ``sensorState`` of "on"/"off".

    Args:
        state: Simulator state (consulted for notification enable settings).
        sensor: "inside" or "outside".
        sensor_state: SENSOR_STATE_ON or SENSOR_STATE_OFF.

    Returns:
        The notification message dict, or None if the corresponding
        notification setting is disabled.
    """
    if sensor == "inside":
        if sensor_state == SENSOR_STATE_ON and not state.sensor_on_indoor:
            return None
        if sensor_state == SENSOR_STATE_OFF and not state.sensor_off_indoor:
            return None
        notify_type = NOTIFY_SENSOR_INDOOR
    else:  # outside
        if sensor_state == SENSOR_STATE_ON and not state.sensor_on_outdoor:
            return None
        if sensor_state == SENSOR_STATE_OFF and not state.sensor_off_outdoor:
            return None
        notify_type = NOTIFY_SENSOR_OUTDOOR

    return {notify_type: "", FIELD_SENSOR_STATE: sensor_state}


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
                response[FIELD_SETTINGS] = self.state.get_settings()
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
        self._owns_engine = engine is None
        self.engine = engine or DoorMotionEngine(
            state,
            broadcast_status=self._broadcast_or_send_status,
            notify_sensor=self._send_sensor_notification,
        )

    @property
    def buffer(self) -> str:
        """The framing scanner's un-parsed remainder (introspection hook)."""
        return self._scanner.buffer

    def connection_made(self, transport):
        peername = transport.get_extra_info("peername")
        logger.info("Simulator: Client connected from %s", peername)
        self.transport = transport

    def connection_lost(self, exc):
        logger.info("Simulator: Client disconnected")
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
            logger.warning("Simulator: escaped non-ASCII bytes in a received chunk")

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Simulator RX: %s", sanitize_log_text(text))

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
                    "Simulator: JSON parse error: %s (frame: %s)",
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
            logger.error("Simulator: message handler task failed", exc_info=task.exception())

    def _report_unknown_command(self, cmd: object) -> None:
        """Log an unknown command."""
        logger.warning("Simulator: Unknown command: %s", sanitize_field(cmd, MAX_LOGGED_LENGTH))

    def _send(self, msg: dict):
        """Send a message to the client."""
        data = json.dumps(msg).encode("ascii")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Simulator TX: %s", sanitize_log_text(str(msg)))
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
        logger.error("Simulator: %s; dropping the connection", reason)
        if self.transport:
            self.transport.abort()

    def _check_command_allowed(self, cmd: str) -> tuple[bool, str]:
        """Check if a command is allowed given current state.

        Returns (allowed, reason).
        """
        # Power must be on for door commands
        if cmd in COMMAND_ENVELOPE_COMMANDS and not self.state.power:
            return False, "Power is OFF"

        # Command lockout blocks remote commands when enabled
        if self.state.cmd_lockout and cmd in COMMAND_ENVELOPE_COMMANDS:
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

        # Verified against firmware 1.7.18: only door motion is a "cmd".
        # `{"cmd": "ENABLE_INSIDE"}` is answered success:"false" by a real
        # door while `{"config": "ENABLE_INSIDE"}` succeeds. Reproduced so
        # a caller that picks the wrong envelope key fails here rather than
        # only against hardware.
        if envelope == COMMAND and cmd not in COMMAND_ENVELOPE_COMMANDS:
            logger.warning(
                "Simulator: %s was sent as %r; a real door only accepts %s that way",
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
        except SilentDropError:
            # The device answers nothing at all - see `_handle_set_time`.
            logger.info(
                "Simulator: %s answered with silence, as the device does",
                sanitize_field(cmd, MAX_LOGGED_LENGTH),
            )
            return
        except WireValueError as err:
            # A deliberate rejection: the handler validated an untrusted
            # field and refused it before touching state. Report the actual
            # reason so a legitimate client can fix its payload.
            logger.warning(
                "Simulator: Rejected %s: %s",
                sanitize_field(cmd, MAX_LOGGED_LENGTH),
                sanitize_field(err, MAX_LOGGED_LENGTH),
            )
            response = self._error_envelope(cmd, str(err))
        except Exception:
            logger.exception("Simulator: Error handling command %s", sanitize_field(cmd))
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

        **Verified against firmware 1.7.18**: a real door echoes ``msgId``
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
        response[FIELD_SETTINGS] = self.state.get_settings()

    @CommandRegistry.handler(CMD_GET_DOOR_STATUS)
    async def _handle_get_door_status(self, msg: dict, response: dict) -> None:
        response[FIELD_DOOR_STATUS] = self.state.door_status

    # A real door spells the same concept differently per command, and the
    # simulator reproduces that rather than normalizing it (see
    # docs/protocol.md, "Value spellings"). Two rules cover every handler
    # below:
    #
    # * a field carried at the TOP LEVEL of a reply is an int 1/0 - verified
    #   for GET_SENSORS and for the individual setting replies
    #   (`{"config": "ENABLE_INSIDE"}` -> `{"inside": 1}`);
    # * a field carried inside a `settings` object is spelled the way
    #   GET_SETTINGS spells it - "true"/"false" strings, except doorOptions
    #   which is an int.
    #
    # Five commands this project defines have NO handler on purpose. Firmware
    # 1.7.18 answers success:"false" to every one of them, so the simulator
    # lets them fall through to "Unknown command" too:
    #
    #   GET_TIMERS_ENABLED, GET_AUTORETRACT, GET_CMD_LOCKOUT,
    #   GET_OUTSIDE_SENSOR_SAFETY_LOCK, CHECK_RESET_REASON
    #
    # The first four read out of GET_SETTINGS instead (`timersEnabled`,
    # `doorOptions`, `allowCmdLockout`, `outsideSensorSafetyLock`); the
    # last has no substitute. The constants stay in const.py: the client
    # keeps response handlers for them in case another firmware revision
    # does implement them.

    @CommandRegistry.handler(CMD_GET_SENSORS)
    async def _handle_get_sensors(self, msg: dict, response: dict) -> None:
        response[FIELD_INSIDE] = wire_int_flag(self.state.inside)
        response[FIELD_OUTSIDE] = wire_int_flag(self.state.outside)

    @CommandRegistry.handler(CMD_GET_POWER)
    async def _handle_get_power(self, msg: dict, response: dict) -> None:
        response[FIELD_POWER] = wire_int_flag(self.state.power)

    @CommandRegistry.handler(CMD_GET_HW_INFO)
    async def _handle_get_hw_info(self, msg: dict, response: dict) -> None:
        response[FIELD_FWINFO] = {
            FIELD_FW_MAJOR: self.state.fw_major,
            FIELD_FW_MINOR: self.state.fw_minor,
            FIELD_FW_PATCH: self.state.fw_patch,
            FIELD_HW_VERSION: self.state.hw_ver,
            FIELD_HW_REVISION: self.state.hw_rev,
        }

    @CommandRegistry.handler(CMD_GET_DOOR_BATTERY)
    async def _handle_get_battery(self, msg: dict, response: dict) -> None:
        # Report 0% if battery is not present
        percent = self.state.battery_percent if self.state.battery_present else 0
        response[FIELD_BATTERY_PERCENT] = percent
        # "true"/"false" strings, verified against firmware 1.7.18 -
        # `batteryPercent` alongside them is an int.
        response[FIELD_BATTERY_PRESENT] = wire_bool_string(self.state.battery_present)
        response[FIELD_AC_PRESENT] = wire_bool_string(self.state.ac_present)

    @CommandRegistry.handler(CMD_GET_DOOR_OPEN_STATS)
    async def _handle_get_stats(self, msg: dict, response: dict) -> None:
        response[FIELD_TOTAL_OPEN_CYCLES] = self.state.total_open_cycles
        response[FIELD_TOTAL_AUTO_RETRACTS] = self.state.total_auto_retracts

    @CommandRegistry.handler(CMD_GET_NOTIFICATIONS)
    async def _handle_get_notifications(self, msg: dict, response: dict) -> None:
        response[FIELD_NOTIFICATIONS] = self.state.get_notifications()

    @CommandRegistry.handler(CMD_GET_TIMEZONE)
    async def _handle_get_timezone(self, msg: dict, response: dict) -> None:
        # POSIX, as real hardware answers - the single conversion lives on
        # the state so this and GET_SETTINGS cannot disagree.
        response[FIELD_TZ] = self.state.wire_timezone()

    @CommandRegistry.handler(CMD_GET_TIME)
    async def _handle_get_time(self, msg: dict, response: dict) -> None:
        """Answer the door's own wall clock, in its configured timezone.

        **Verified against firmware 1.7.18**: undocumented by the vendor,
        but present, and worth having because schedules are evaluated
        against this clock - it is the only way to check that a door will
        fire a schedule when you expect it to.
        """
        response[FIELD_TIME] = datetime.now(self.state.get_tzinfo()).strftime(TIME_FORMAT)

    @CommandRegistry.handler(CMD_SET_TIME)
    async def _handle_set_time(self, msg: dict, response: dict) -> None:
        """Answer a ``SET_TIME`` with **silence**, as the real door does.

        **Verified against firmware 1.7.18**: the clock is read-only, and
        this one command is answered with no frame at all - where every
        other rejected shape (including ``SET_CLOCK``, ``SET_DATE`` and
        ``SYNC_TIME``) answers ``success: "false"``. Reproduced so a
        client that reads silence as success hangs here rather than only
        against hardware.
        """
        raise SilentDropError(CMD_SET_TIME)

    @CommandRegistry.handler(CMD_GET_HOLD_TIME)
    async def _handle_get_hold_time(self, msg: dict, response: dict) -> None:
        # Convert seconds to centiseconds for protocol
        response[FIELD_HOLD_TIME] = int(self.state.hold_time * 100)

    @CommandRegistry.handler(CMD_GET_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_get_sensor_voltage(self, msg: dict, response: dict) -> None:
        response[FIELD_SENSOR_TRIGGER_VOLTAGE] = self.state.sensor_trigger_voltage

    @CommandRegistry.handler(CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_get_sleep_voltage(self, msg: dict, response: dict) -> None:
        response[FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE] = self.state.sleep_sensor_trigger_voltage

    # ==========================================================================
    # Command Handlers - Schedule Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_GET_SCHEDULE_LIST)
    async def _handle_get_schedule_list(self, msg: dict, response: dict) -> None:
        response[FIELD_SCHEDULES] = self.state.get_schedule_list()

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
        if index is not None and index in self.state.schedules:
            response[FIELD_SCHEDULE] = self.state.schedules[index].to_dict()
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
        # Observed on firmware 1.7.18: the slot index must be sent alongside
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
                "Simulator: Rejected schedule: %s",
                sanitize_field(err, MAX_LOGGED_LENGTH),
            )
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = str(err)
            return
        self.state.schedules[schedule.index] = schedule
        response[FIELD_SCHEDULE] = schedule.to_dict()
        logger.info("Simulator: Schedule %s saved", sanitize_log_text(schedule.index))

    @CommandRegistry.handler(CMD_DELETE_SCHEDULE)
    async def _handle_delete_schedule(self, msg: dict, response: dict) -> None:
        index = self._wire_schedule_index(msg)
        if index is not None and index in self.state.schedules:
            del self.state.schedules[index]
            # The real device echoes the deleted index in the response
            response[FIELD_INDEX] = index
            logger.info("Simulator: Schedule %s deleted", sanitize_log_text(index))
        else:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Schedule not found"

    @CommandRegistry.handler(CMD_SET_SCHEDULE_LIST)
    async def _handle_set_schedule_list(self, msg: dict, response: dict) -> None:
        """Replace the whole schedule store from the wire.

        The field is *required*, and must be a list. Defaulting an absent
        ``schedules`` to ``[]`` made a one-word packet wipe every stored
        schedule and answer ``success: "true"``, and a wrong-typed field
        fell straight through to the same success response having done
        nothing. "Clear everything" now has to be spelled out as an
        explicit ``"schedules": []``, and every other shape is rejected
        with a reason the way docs/protocol.md says every ``SET_*`` is.
        """
        if FIELD_SCHEDULES not in msg:
            raise WireValueError(f"{FIELD_SCHEDULES} is required")
        schedules_data = msg[FIELD_SCHEDULES]
        if not isinstance(schedules_data, list):
            raise WireValueError(f"{FIELD_SCHEDULES} must be a list, got {schedules_data!r}")
        try:
            parsed = [Schedule.from_dict(sched_data) for sched_data in schedules_data]
        except ValueError as err:
            # Reject the whole list atomically: a partial load would
            # leave the simulator in a state no client asked for.
            logger.warning(
                "Simulator: Rejected schedule list: %s",
                sanitize_field(err, MAX_LOGGED_LENGTH),
            )
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = str(err)
            return
        # Clear existing and load new schedules
        self.state.schedules.clear()
        for schedule in parsed:
            self.state.schedules[schedule.index] = schedule
        logger.info("Simulator: Loaded %d schedules", len(schedules_data))
        response[FIELD_SCHEDULES] = self.state.get_schedule_list()

    # ==========================================================================
    # Command Handlers - Remote/Reset Info
    # ==========================================================================

    @CommandRegistry.handler(CMD_HAS_REMOTE_ID)
    async def _handle_has_remote_id(self, msg: dict, response: dict) -> None:
        # Verified against firmware 1.7.18: the field is `has_id`, and its
        # value is a "true"/"false" string.
        response[FIELD_HAS_REMOTE_ID] = wire_bool_string(self.state.has_remote_id)

    @CommandRegistry.handler(CMD_HAS_REMOTE_KEY)
    async def _handle_has_remote_key(self, msg: dict, response: dict) -> None:
        response[FIELD_HAS_REMOTE_KEY] = wire_bool_string(self.state.has_remote_key)

    # ==========================================================================
    # Command Handlers - Door Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_OPEN)
    async def _handle_open(self, msg: dict, response: dict) -> None:
        self.engine.open(hold=False)
        response[FIELD_DOOR_STATUS] = self.state.door_status

    @CommandRegistry.handler(CMD_OPEN_AND_HOLD)
    async def _handle_open_and_hold(self, msg: dict, response: dict) -> None:
        self.engine.open(hold=True)
        response[FIELD_DOOR_STATUS] = self.state.door_status

    @CommandRegistry.handler(CMD_CLOSE)
    async def _handle_close(self, msg: dict, response: dict) -> None:
        self.engine.close()
        response[FIELD_DOOR_STATUS] = self.state.door_status

    # ==========================================================================
    # Command Handlers - Enable/Disable Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_ENABLE_INSIDE)
    async def _handle_enable_inside(self, msg: dict, response: dict) -> None:
        self.state.inside = True
        response[FIELD_INSIDE] = wire_int_flag(True)

    @CommandRegistry.handler(CMD_DISABLE_INSIDE)
    async def _handle_disable_inside(self, msg: dict, response: dict) -> None:
        self.state.inside = False
        response[FIELD_INSIDE] = wire_int_flag(False)

    @CommandRegistry.handler(CMD_ENABLE_OUTSIDE)
    async def _handle_enable_outside(self, msg: dict, response: dict) -> None:
        self.state.outside = True
        response[FIELD_OUTSIDE] = wire_int_flag(True)

    @CommandRegistry.handler(CMD_DISABLE_OUTSIDE)
    async def _handle_disable_outside(self, msg: dict, response: dict) -> None:
        self.state.outside = False
        response[FIELD_OUTSIDE] = wire_int_flag(False)

    @CommandRegistry.handler(CMD_ENABLE_AUTO)
    async def _handle_enable_auto(self, msg: dict, response: dict) -> None:
        self.state.auto = True
        response[FIELD_AUTO] = wire_int_flag(True)

    @CommandRegistry.handler(CMD_DISABLE_AUTO)
    async def _handle_disable_auto(self, msg: dict, response: dict) -> None:
        self.state.auto = False
        response[FIELD_AUTO] = wire_int_flag(False)

    @CommandRegistry.handler(CMD_POWER_ON)
    async def _handle_power_on(self, msg: dict, response: dict) -> None:
        self.state.power = True
        response[FIELD_POWER] = wire_int_flag(True)
        logger.info("Simulator: Power ON")

    @CommandRegistry.handler(CMD_POWER_OFF)
    async def _handle_power_off(self, msg: dict, response: dict) -> None:
        self.state.power = False
        response[FIELD_POWER] = wire_int_flag(False)
        logger.info("Simulator: Power OFF")
        # If door is open, close it when power goes off
        if self.state.door_status != DOOR_STATE_CLOSED:
            self.engine.close()

    @CommandRegistry.handler(CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK)
    async def _handle_enable_safety_lock(self, msg: dict, response: dict) -> None:
        self.state.safety_lock = True
        response[FIELD_SETTINGS] = {FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: wire_bool_string(True)}
        logger.info("Simulator: Outside sensor safety lock ENABLED")

    @CommandRegistry.handler(CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK)
    async def _handle_disable_safety_lock(self, msg: dict, response: dict) -> None:
        self.state.safety_lock = False
        response[FIELD_SETTINGS] = {FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: wire_bool_string(False)}
        logger.info("Simulator: Outside sensor safety lock DISABLED")

    @CommandRegistry.handler(CMD_ENABLE_CMD_LOCKOUT)
    async def _handle_enable_cmd_lockout(self, msg: dict, response: dict) -> None:
        self.state.cmd_lockout = True
        response[FIELD_SETTINGS] = {FIELD_CMD_LOCKOUT: wire_bool_string(True)}
        logger.info("Simulator: Command lockout ENABLED")

    @CommandRegistry.handler(CMD_DISABLE_CMD_LOCKOUT)
    async def _handle_disable_cmd_lockout(self, msg: dict, response: dict) -> None:
        self.state.cmd_lockout = False
        response[FIELD_SETTINGS] = {FIELD_CMD_LOCKOUT: wire_bool_string(False)}
        logger.info("Simulator: Command lockout DISABLED")

    @CommandRegistry.handler(CMD_ENABLE_AUTORETRACT)
    async def _handle_enable_autoretract(self, msg: dict, response: dict) -> None:
        self.state.autoretract = True
        # Verified against firmware 1.7.18: these two answer with the WHOLE
        # settings object, not just the field they changed.
        response[FIELD_SETTINGS] = self.state.get_settings()
        logger.info("Simulator: Auto-retract ENABLED")

    @CommandRegistry.handler(CMD_DISABLE_AUTORETRACT)
    async def _handle_disable_autoretract(self, msg: dict, response: dict) -> None:
        self.state.autoretract = False
        response[FIELD_SETTINGS] = self.state.get_settings()
        logger.info("Simulator: Auto-retract DISABLED")

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
            # Store the wire (POSIX) value as-is; schedule evaluation maps
            # it back to an IANA zone (see DoorSimulatorState.get_tzinfo).
            self.state.timezone = timezone
        response[FIELD_TZ] = self.state.timezone

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
            self.state.hold_time = centiseconds / 100.0
            logger.info("Simulator: Hold time set to %ss", self.state.hold_time)
        # Convert seconds to centiseconds for protocol response
        response[FIELD_HOLD_TIME] = int(self.state.hold_time * 100)

    @CommandRegistry.handler(CMD_SET_NOTIFICATIONS)
    async def _handle_set_notifications(self, msg: dict, response: dict) -> None:
        # Observed on firmware 1.7.18: the flags must arrive inside a nested
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
            response[FIELD_NOTIFICATIONS] = self.state.get_notifications()
            return

        # Every value is a real JSON boolean by now, so there is nothing
        # left to coerce and nothing that can half-apply.
        attributes = {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "sensor_on_indoor",
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: "sensor_off_indoor",
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: "sensor_on_outdoor",
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: "sensor_off_outdoor",
            FIELD_LOW_BATTERY_NOTIFICATIONS: "low_battery",
        }
        for field_name, attribute in attributes.items():
            if field_name in nested:
                setattr(self.state, attribute, nested[field_name])
        response[FIELD_NOTIFICATIONS] = self.state.get_notifications()

    def _wire_voltage(self, msg: dict) -> int:
        """Read the new threshold out of a voltage setter's message.

        **Verified against firmware 1.7.18**: both setters take ``voltage``
        and reject the ``sensorTriggerVoltage`` /
        ``sleepSensorTriggerVoltage`` name their getters answer with - the
        one docs/protocol.md used to document. Emulated, so a caller that
        sends the getter's name fails here rather than only on hardware.

        Raises:
            WireValueError: If ``voltage`` is absent, or is not an integer
                in range.
        """
        if FIELD_VOLTAGE not in msg:
            raise WireValueError(f"{FIELD_VOLTAGE} is required")
        # Nothing does arithmetic on this today, so an arbitrary JSON value
        # is currently inert - it is the same latent trap as holdTime, so
        # bound it at the door rather than later.
        return _coerce_wire_int(msg[FIELD_VOLTAGE], FIELD_VOLTAGE, 0, MAX_TRIGGER_VOLTAGE)

    @CommandRegistry.handler(CMD_SET_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_set_sensor_voltage(self, msg: dict, response: dict) -> None:
        self.state.sensor_trigger_voltage = self._wire_voltage(msg)
        # The reply echoes the GETTER's field name, not the setter's.
        response[FIELD_SENSOR_TRIGGER_VOLTAGE] = self.state.sensor_trigger_voltage

    @CommandRegistry.handler(CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_set_sleep_voltage(self, msg: dict, response: dict) -> None:
        self.state.sleep_sensor_trigger_voltage = self._wire_voltage(msg)
        response[FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE] = self.state.sleep_sensor_trigger_voltage

    # ==========================================================================
    # Door Operation Delegation (single engine - see engine.py)
    # ==========================================================================

    def trigger_sensor(self, sensor: str):
        """Simulate a sensor trigger (pet walking through).

        Args:
            sensor: "inside" or "outside"
        """
        self.engine.trigger_sensor(sensor)

    def simulate_obstruction(self):
        """Simulate obstruction detection (inside sensor active indefinitely)."""
        self.engine.simulate_obstruction()

    def _send_door_status(self):
        """Send unsolicited door status update to this client only."""
        self._send(
            {
                FIELD_CMD: DOOR_STATUS,
                FIELD_DOOR_STATUS: self.state.door_status,
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

    def _send_sensor_notification(self, sensor: str, state: str = SENSOR_STATE_ON):
        """Send sensor trigger notification (bare envelope) if enabled.

        Args:
            sensor: "inside" or "outside"
            state: "on" (sensor triggered) or "off" (sensor released)
        """
        notification = make_sensor_notification(self.state, sensor, state)
        if notification is None:
            return
        self._send(notification)
        logger.debug("Simulator: Sent %s sensor %s notification", sensor, state)
