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

from ..client import make_bool
from ..const import (
    CMD_CHECK_RESET_REASON,
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
    CMD_GET_AUTO,
    CMD_GET_AUTORETRACT,
    CMD_GET_CMD_LOCKOUT,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HOLD_TIME,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_GET_POWER,
    CMD_GET_SCHEDULE,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_SENSORS,
    CMD_GET_SETTINGS,
    CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
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
    CMD_SET_TIMEZONE,
    COMMAND,
    CONFIG,
    DOOR_STATE_CLOSED,
    DOOR_STATUS,
    DOOR_TO_PHONE,
    FIELD_AC_PRESENT,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
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
    FIELD_RESET_REASON,
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
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    NOTIFY_SENSOR_INDOOR,
    NOTIFY_SENSOR_OUTDOOR,
    PING,
    PONG,
    SENSOR_STATE_OFF,
    SENSOR_STATE_ON,
    SUCCESS_FALSE,
    SUCCESS_TRUE,
)
from ..framing import EventThrottle, FrameDispatcher, FrameScanner
from ..sanitize import MAX_LOGGED_LENGTH, sanitize_text
from ..schedule import MAX_SCHEDULE_INDEX
from ..tz_utils import get_posix_tz_string, is_cache_initialized
from .engine import DoorMotionEngine
from .state import DoorSimulatorState, Schedule

logger = logging.getLogger(__name__)

#: Per-connection write-buffer ceiling, in bytes, above which a door client
#: that is not reading its own responses is dropped. Mirrors the control
#: channel's ``_ControlLogHandler.MAX_CLIENT_BACKLOG`` (round-6 security
#: finding 1, secondary instance).
MAX_WRITE_BACKLOG = 1024 * 1024

#: Network-derived strings are stripped of terminal control characters before
#: they reach any log, so a hostile peer cannot inject escape sequences into
#: operator consoles or forge extra log lines. The single implementation
#: lives in the library package (:mod:`powerpetdoor.sanitize`) and is shared
#: with the client library and the interactive front end.
sanitize_log_text = sanitize_text

#: Widest hold time (centiseconds) accepted from the wire; matches the
#: operator-side ``holdtime`` command's 900 s ceiling.
MAX_HOLD_TIME_CENTISECONDS = 90000
#: Longest ``SET_TIMEZONE`` string accepted from the wire. Real POSIX TZ
#: strings and IANA names are far shorter than this.
MAX_TIMEZONE_LENGTH = 128
#: Widest sensor trigger voltage accepted from the wire (millivolts).
MAX_TRIGGER_VOLTAGE = 65535


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


def _coerce_wire_flag(value: object, name: str) -> bool:
    """Coerce an untrusted wire field to a ``"1"``/``"0"`` protocol flag.

    Read with :func:`~powerpetdoor.client.make_bool`, the same way the
    client reads them, so ``1`` and ``"1"`` mean the same thing and
    ``bool("0") is True`` can never turn a setting on.

    Raises:
        WireValueError: If the value is not a recognizable flag.
    """
    flag = make_bool(value) if isinstance(value, (bool, int, str)) else None
    if not isinstance(flag, bool):
        raise WireValueError(f"{name} must be 0 or 1, got {value!r}")
    return flag


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
        # so an unauthenticated peer dribbling a never-terminated object
        # cannot make the daemon re-scan its retained buffer every time (S1).
        self._scanner = FrameScanner()
        # Twin of the client's counter: one notice per connection plus
        # doubling-schedule summaries, so a peer sending one non-ASCII byte
        # per TCP segment cannot buy one WARNING per byte - amplified again
        # by the control channel's log fan-out (Security round-5 Finding 1).
        self._non_ascii = EventThrottle(
            logger,
            logging.WARNING,
            "Simulator: escaped non-ASCII bytes in %d received chunks (%d bytes total)",
        )
        # Twins of the client's per-frame throttles. These fire once per
        # *frame*, so they are limited by the peer's byte rate rather than
        # its packet rate: 21,845 three-byte `{x}` frames fit in one 64 KiB
        # write and used to buy x46 write amplification, sustained for 17 s
        # after the attacker disconnected (round-6 security finding 2).
        self._bad_frames = EventThrottle(
            logger,
            logging.WARNING,
            "Simulator: %d JSON parse error(s) (%d bytes) on this connection",
        )
        self._unknown_commands = EventThrottle(
            logger,
            logging.WARNING,
            "Simulator: %d unknown command(s) (%d bytes) on this connection",
        )
        # Round 6 throttled the three sites above and left the *rejection*
        # sites - one record per rejected SET_*, per bad schedule and per
        # bad schedule list, with no length cap - at x1.9-2.6 write
        # amplification and one WARNING per frame (round-7 security L3).
        # One throttle for all three: they are the same event to an
        # operator ("the peer keeps sending fields we refuse"), and the
        # first occurrence is always reported in full.
        self._rejections = EventThrottle(
            logger,
            logging.WARNING,
            "Simulator: rejected %d malformed field(s)/payload(s) (%d bytes) on this connection",
        )
        # The write ceiling used to announce "dropping the connection"
        # once per message and then not drop it (round-7 security L2).
        self._connection_drops = EventThrottle(
            logger,
            logging.ERROR,
            "Simulator: %d connection-drop event(s) (%d bytes) on this connection",
        )
        #: Latched once a protocol violation has cost this peer its
        #: connection, so nothing re-checks and re-reports it.
        self._dropped = False
        self._tasks: set[asyncio.Task] = set()
        # One task per framed message, created synchronously per read, was
        # unbounded: 256 KiB of `{}` admitted 131,072 live tasks / ~145 MB,
        # linear in connections, with no connection cap (round-6 security
        # finding 1).
        self._dispatcher = FrameDispatcher(self._dispatch_frame)
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
        # End of this connection's framing state: report the counters'
        # suppressed tail rather than dropping it.
        self._non_ascii.flush()
        self._bad_frames.flush()
        self._unknown_commands.flush()
        self._rejections.flush()
        self._connection_drops.flush()
        self._scanner.reset()
        self._dispatcher.reset()
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
        Dispatch is bounded, so this also drains whatever the dispatcher is
        still holding back.
        """
        while True:
            pending = [task for task in self._tasks if not task.done()]
            if not pending:
                if not self._dispatcher.backlog:
                    return
                # Handlers finished but frames are still queued; the
                # dispatcher starts them from a done-callback, so yield.
                await asyncio.sleep(0)
                continue
            await asyncio.gather(*pending, return_exceptions=True)

    def data_received(self, data: bytes):
        # Escape non-ASCII bytes instead of dropping the chunk: dropping it
        # would strand a half-buffered frame and wedge framing until the
        # 64 KiB overflow disconnect. The affected frame fails json.loads
        # and is skipped on its own; later frames still arrive (L2).
        text = data.decode("ascii", errors="backslashreplace")
        if len(text) != len(data):
            self._non_ascii.record(len(data))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Simulator RX: %s", sanitize_log_text(text))

        frames, diag = self._scanner.feed(text)
        if diag.overflow:
            self._drop_connection("receive buffer overflowed without a complete message")
            return

        # Bounded dispatch, twin of the client's (round-6 security finding 1).
        self._dispatcher.submit(frames, self.transport)

    def _dispatch_frame(self, frame: str) -> asyncio.Task | None:
        """Decode one framed message and start its handler.

        Returns:
            The handler task, or None if the frame was not usable JSON.
        """
        try:
            msg = json.loads(frame)
        except json.JSONDecodeError as err:
            # Throttled twin of the client's site: one unparseable frame is
            # three bytes and used to buy a whole WARNING record.
            if self._bad_frames.record(len(frame)):
                logger.warning(
                    "Simulator: JSON parse error: %s (frame: %s)",
                    err,
                    sanitize_log_text(frame, MAX_LOGGED_LENGTH),
                )
            return None
        return self._create_task(self._handle_message(msg))

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
        """Log an unknown command, throttled (round-6 security finding 2)."""
        rendered = sanitize_log_text(cmd, MAX_LOGGED_LENGTH)
        if self._unknown_commands.record(len(rendered)):
            logger.warning("Simulator: Unknown command: %s", rendered)

    def _send(self, msg: dict):
        """Send a message to the client.

        A peer that issues valid commands and never reads the answers made
        the daemon buffer them without bound - 1.5 MB of requests bought
        36 MB of daemon heap, held for as long as the socket stayed open.
        The control channel got a write-buffer ceiling in round 5; this is
        the same ceiling on the door transport (round-6 security finding 1,
        secondary instance). A door client that is not reading its own
        responses is dropped, exactly like one that overflows the framing
        cap.
        """
        # Latched: this peer has already cost itself its connection, so
        # neither queued frames nor later broadcasts re-check the ceiling
        # and re-announce the drop.
        if self._dropped:
            return
        data = json.dumps(msg).encode("ascii")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Simulator TX: %s", sanitize_log_text(str(msg)))
        if not self.transport:
            return
        buffered = self.transport.get_write_buffer_size()
        if buffered > MAX_WRITE_BACKLOG:
            self._drop_connection(
                f"client is not reading its responses ({buffered} bytes buffered)"
            )
            return
        self.transport.write(data)

    def _drop_connection(self, reason: str) -> None:
        """Abort this connection after a declared protocol violation.

        ``transport.close()`` only sets ``_closing`` and removes the
        reader; ``connection_lost`` is deferred until the write buffer
        drains, and a peer holding a zero TCP window never lets it. So the
        protocol object, its ~1 MB buffer and its slot in
        ``DoorSimulator.protocols`` were held for the life of the daemon,
        ``ctl status`` kept reporting the client the daemon's own ERROR
        said it had dropped, and every later broadcast logged again
        (round-7 security L2).

        ``abort()`` discards the unsent tail and delivers
        ``connection_lost`` immediately. Discarding it is correct here:
        the peer has already violated the protocol, and this is the only
        thing that makes ``ctl status`` truthful.
        """
        if self._connection_drops.record(len(reason)):
            logger.error("Simulator: %s; dropping the connection", reason)
        self._dropped = True
        if self.transport:
            self.transport.abort()

    def _check_command_allowed(self, cmd: str) -> tuple[bool, str]:
        """Check if a command is allowed given current state.

        Returns (allowed, reason).
        """
        # Power must be on for door commands
        door_commands = {CMD_OPEN, CMD_OPEN_AND_HOLD, CMD_CLOSE}
        if cmd in door_commands and not self.state.power:
            return False, "Power is OFF"

        # Command lockout blocks remote commands when enabled
        if self.state.cmd_lockout and cmd in door_commands:
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

        # Client sends commands under "config" key for queries, "cmd" for actions
        cmd = msg.get(CONFIG) or msg.get(COMMAND)
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
            self._send(response)
            return

        if self.on_command:
            self.on_command(cmd, msg)

        # Check if command is allowed
        allowed, reason = self._check_command_allowed(cmd)
        if not allowed:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = reason
            self._send(response)
            return

        # Look up and execute handler
        handler = CommandRegistry.get(cmd)
        if handler is None:
            self._report_unknown_command(cmd)
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Unknown command"
            self._send(response)
            return

        try:
            await handler(self, msg, response)
        except WireValueError as err:
            # A deliberate rejection: the handler validated an untrusted
            # field and refused it before touching state. Report the actual
            # reason so a legitimate client can fix its payload.
            if self._rejections.record(len(str(err))):
                logger.warning(
                    "Simulator: Rejected %s: %s",
                    sanitize_log_text(cmd, MAX_LOGGED_LENGTH),
                    sanitize_log_text(err, MAX_LOGGED_LENGTH),
                )
            response = self._error_envelope(cmd, msg_id, str(err))
        except Exception:
            logger.exception("Simulator: Error handling command %s", sanitize_log_text(cmd))
            response = self._error_envelope(cmd, msg_id, "Command failed")

        self._send(response)

    @staticmethod
    def _error_envelope(cmd: str, msg_id: object, reason: str) -> dict:
        """Build the standard failure envelope for ``cmd``."""
        response: dict = {
            FIELD_CMD: cmd,
            FIELD_SUCCESS: SUCCESS_FALSE,
            FIELD_DIRECTION: DOOR_TO_PHONE,
            FIELD_REASON: reason,
        }
        if msg_id is not None:
            response[FIELD_MSG_ID_RESPONSE] = msg_id
        return response

    # ==========================================================================
    # Command Handlers - Get Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_GET_SETTINGS)
    async def _handle_get_settings(self, msg: dict, response: dict) -> None:
        response[FIELD_SETTINGS] = self.state.get_settings()

    @CommandRegistry.handler(CMD_GET_DOOR_STATUS)
    async def _handle_get_door_status(self, msg: dict, response: dict) -> None:
        response[FIELD_DOOR_STATUS] = self.state.door_status

    @CommandRegistry.handler(CMD_GET_SENSORS)
    async def _handle_get_sensors(self, msg: dict, response: dict) -> None:
        response[FIELD_INSIDE] = "1" if self.state.inside else "0"
        response[FIELD_OUTSIDE] = "1" if self.state.outside else "0"

    @CommandRegistry.handler(CMD_GET_POWER)
    async def _handle_get_power(self, msg: dict, response: dict) -> None:
        response[FIELD_POWER] = "1" if self.state.power else "0"

    @CommandRegistry.handler(CMD_GET_AUTO)
    async def _handle_get_auto(self, msg: dict, response: dict) -> None:
        response[FIELD_AUTO] = "1" if self.state.auto else "0"

    @CommandRegistry.handler(CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK)
    async def _handle_get_safety_lock(self, msg: dict, response: dict) -> None:
        response[FIELD_SETTINGS] = {
            FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: "1" if self.state.safety_lock else "0"
        }

    @CommandRegistry.handler(CMD_GET_CMD_LOCKOUT)
    async def _handle_get_cmd_lockout(self, msg: dict, response: dict) -> None:
        response[FIELD_SETTINGS] = {FIELD_CMD_LOCKOUT: "1" if self.state.cmd_lockout else "0"}

    @CommandRegistry.handler(CMD_GET_AUTORETRACT)
    async def _handle_get_autoretract(self, msg: dict, response: dict) -> None:
        response[FIELD_SETTINGS] = {FIELD_AUTORETRACT: "1" if self.state.autoretract else "0"}

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
        response[FIELD_BATTERY_PRESENT] = "1" if self.state.battery_present else "0"
        response[FIELD_AC_PRESENT] = "1" if self.state.ac_present else "0"

    @CommandRegistry.handler(CMD_GET_DOOR_OPEN_STATS)
    async def _handle_get_stats(self, msg: dict, response: dict) -> None:
        response[FIELD_TOTAL_OPEN_CYCLES] = self.state.total_open_cycles
        response[FIELD_TOTAL_AUTO_RETRACTS] = self.state.total_auto_retracts

    @CommandRegistry.handler(CMD_GET_NOTIFICATIONS)
    async def _handle_get_notifications(self, msg: dict, response: dict) -> None:
        response[FIELD_NOTIFICATIONS] = self.state.get_notifications()

    @CommandRegistry.handler(CMD_GET_TIMEZONE)
    async def _handle_get_timezone(self, msg: dict, response: dict) -> None:
        # Convert IANA timezone to POSIX format (as real hardware does)
        iana_tz = self.state.timezone
        if is_cache_initialized():
            posix_tz = get_posix_tz_string(iana_tz)
            if posix_tz:
                response[FIELD_TZ] = posix_tz
                return
        # Fallback to raw value if conversion fails (already POSIX, or unknown)
        response[FIELD_TZ] = iana_tz

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
        plus a useless "Command failed" reason. The sibling ``msgID`` field
        has been guarded since round 2 and every ``SET_*`` field since round
        3; these two were the last unguarded wire values (L3/S2).

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
        try:
            schedule = Schedule.from_dict(schedule_data)
        except ValueError as err:
            # Untrusted wire data: reject malformed schedules rather than
            # storing something that raises later during evaluation.
            if self._rejections.record(len(str(err))):
                logger.warning(
                    "Simulator: Rejected schedule: %s",
                    sanitize_log_text(err, MAX_LOGGED_LENGTH),
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
        nothing (L2). "Clear everything" now has to be spelled out as an
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
            if self._rejections.record(len(str(err))):
                logger.warning(
                    "Simulator: Rejected schedule list: %s",
                    sanitize_log_text(err, MAX_LOGGED_LENGTH),
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
        response[FIELD_HAS_REMOTE_ID] = "1" if self.state.has_remote_id else "0"

    @CommandRegistry.handler(CMD_HAS_REMOTE_KEY)
    async def _handle_has_remote_key(self, msg: dict, response: dict) -> None:
        response[FIELD_HAS_REMOTE_KEY] = "1" if self.state.has_remote_key else "0"

    @CommandRegistry.handler(CMD_CHECK_RESET_REASON)
    async def _handle_check_reset_reason(self, msg: dict, response: dict) -> None:
        response[FIELD_RESET_REASON] = self.state.reset_reason

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
        response[FIELD_INSIDE] = "1"

    @CommandRegistry.handler(CMD_DISABLE_INSIDE)
    async def _handle_disable_inside(self, msg: dict, response: dict) -> None:
        self.state.inside = False
        response[FIELD_INSIDE] = "0"

    @CommandRegistry.handler(CMD_ENABLE_OUTSIDE)
    async def _handle_enable_outside(self, msg: dict, response: dict) -> None:
        self.state.outside = True
        response[FIELD_OUTSIDE] = "1"

    @CommandRegistry.handler(CMD_DISABLE_OUTSIDE)
    async def _handle_disable_outside(self, msg: dict, response: dict) -> None:
        self.state.outside = False
        response[FIELD_OUTSIDE] = "0"

    @CommandRegistry.handler(CMD_ENABLE_AUTO)
    async def _handle_enable_auto(self, msg: dict, response: dict) -> None:
        self.state.auto = True
        response[FIELD_AUTO] = "1"

    @CommandRegistry.handler(CMD_DISABLE_AUTO)
    async def _handle_disable_auto(self, msg: dict, response: dict) -> None:
        self.state.auto = False
        response[FIELD_AUTO] = "0"

    @CommandRegistry.handler(CMD_POWER_ON)
    async def _handle_power_on(self, msg: dict, response: dict) -> None:
        self.state.power = True
        response[FIELD_POWER] = "1"
        logger.info("Simulator: Power ON")

    @CommandRegistry.handler(CMD_POWER_OFF)
    async def _handle_power_off(self, msg: dict, response: dict) -> None:
        self.state.power = False
        response[FIELD_POWER] = "0"
        logger.info("Simulator: Power OFF")
        # If door is open, close it when power goes off
        if self.state.door_status != DOOR_STATE_CLOSED:
            self.engine.close()

    @CommandRegistry.handler(CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK)
    async def _handle_enable_safety_lock(self, msg: dict, response: dict) -> None:
        self.state.safety_lock = True
        response[FIELD_SETTINGS] = {FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: "1"}
        logger.info("Simulator: Outside sensor safety lock ENABLED")

    @CommandRegistry.handler(CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK)
    async def _handle_disable_safety_lock(self, msg: dict, response: dict) -> None:
        self.state.safety_lock = False
        response[FIELD_SETTINGS] = {FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: "0"}
        logger.info("Simulator: Outside sensor safety lock DISABLED")

    @CommandRegistry.handler(CMD_ENABLE_CMD_LOCKOUT)
    async def _handle_enable_cmd_lockout(self, msg: dict, response: dict) -> None:
        self.state.cmd_lockout = True
        response[FIELD_SETTINGS] = {FIELD_CMD_LOCKOUT: "1"}
        logger.info("Simulator: Command lockout ENABLED")

    @CommandRegistry.handler(CMD_DISABLE_CMD_LOCKOUT)
    async def _handle_disable_cmd_lockout(self, msg: dict, response: dict) -> None:
        self.state.cmd_lockout = False
        response[FIELD_SETTINGS] = {FIELD_CMD_LOCKOUT: "0"}
        logger.info("Simulator: Command lockout DISABLED")

    @CommandRegistry.handler(CMD_ENABLE_AUTORETRACT)
    async def _handle_enable_autoretract(self, msg: dict, response: dict) -> None:
        self.state.autoretract = True
        response[FIELD_SETTINGS] = {FIELD_AUTORETRACT: "1"}
        logger.info("Simulator: Auto-retract ENABLED")

    @CommandRegistry.handler(CMD_DISABLE_AUTORETRACT)
    async def _handle_disable_autoretract(self, msg: dict, response: dict) -> None:
        self.state.autoretract = False
        response[FIELD_SETTINGS] = {FIELD_AUTORETRACT: "0"}
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
        # docs/protocol.md: the settings arrive as top-level "1"/"0" fields
        # (this is what the client sends). A nested "notifications" dict is
        # also tolerated for compatibility with older callers.
        settings: dict = {}
        nested = msg.get(FIELD_NOTIFICATIONS)
        if isinstance(nested, dict):
            settings.update(nested)
        notification_fields = (
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
            FIELD_LOW_BATTERY_NOTIFICATIONS,
        )
        for field_name in notification_fields:
            if field_name in msg:
                settings[field_name] = msg[field_name]

        # Coerce every supplied flag BEFORE assigning any of them, so a
        # malformed field cannot leave a half-applied notification set.
        attributes = {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "sensor_on_indoor",
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: "sensor_off_indoor",
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: "sensor_on_outdoor",
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: "sensor_off_outdoor",
            FIELD_LOW_BATTERY_NOTIFICATIONS: "low_battery",
        }
        coerced = {
            attribute: _coerce_wire_flag(settings[field_name], field_name)
            for field_name, attribute in attributes.items()
            if field_name in settings
        }
        for attribute, flag in coerced.items():
            setattr(self.state, attribute, flag)
        response[FIELD_NOTIFICATIONS] = self.state.get_notifications()

    @CommandRegistry.handler(CMD_SET_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_set_sensor_voltage(self, msg: dict, response: dict) -> None:
        if FIELD_SENSOR_TRIGGER_VOLTAGE in msg:
            # Nothing does arithmetic on this today, so an arbitrary JSON
            # value is currently inert - it is the same latent trap as
            # holdTime, so bound it at the door rather than later.
            self.state.sensor_trigger_voltage = _coerce_wire_int(
                msg[FIELD_SENSOR_TRIGGER_VOLTAGE],
                FIELD_SENSOR_TRIGGER_VOLTAGE,
                0,
                MAX_TRIGGER_VOLTAGE,
            )
        response[FIELD_SENSOR_TRIGGER_VOLTAGE] = self.state.sensor_trigger_voltage

    @CommandRegistry.handler(CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_set_sleep_voltage(self, msg: dict, response: dict) -> None:
        if FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE in msg:
            self.state.sleep_sensor_trigger_voltage = _coerce_wire_int(
                msg[FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE],
                FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
                0,
                MAX_TRIGGER_VOLTAGE,
            )
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
