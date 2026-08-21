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
import re
from collections.abc import Callable

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
from ..framing import extract_frames
from ..tz_utils import get_posix_tz_string, is_cache_initialized
from .engine import DoorMotionEngine
from .state import DoorSimulatorState, Schedule

logger = logging.getLogger(__name__)

# C0 control characters (except tab/newline), DEL, and C1 control characters.
# Network-derived strings are stripped of these before they reach any log so
# a hostile peer cannot inject terminal escape sequences into operator
# consoles or forge extra log lines.
#
# NOTE: this duplicates prompt_common.sanitize_text on purpose - the core
# simulator must not import the interactive front-end stack. Dedup is left
# for a later cleanup wave.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_log_text(text: object) -> str:
    """Neutralize terminal control characters in untrusted text.

    Accepts any value (network-derived fields are not guaranteed to be
    strings) and replaces C0 controls (except tab and newline), DEL, and C1
    controls with their visible ``\\xNN`` escape so the result is safe to log.
    """
    return _CONTROL_CHAR_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))


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
        self.buffer = ""
        self._tasks: set[asyncio.Task] = set()
        self._owns_engine = engine is None
        self.engine = engine or DoorMotionEngine(
            state,
            broadcast_status=self._broadcast_or_send_status,
            notify_sensor=self._send_sensor_notification,
        )

    def connection_made(self, transport):
        peername = transport.get_extra_info("peername")
        logger.info("Simulator: Client connected from %s", peername)
        self.transport = transport

    def connection_lost(self, exc):
        logger.info("Simulator: Client disconnected")
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
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            logger.warning("Simulator: Dropping %d bytes of undecodable data", len(data))
            return

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Simulator RX: %s", sanitize_log_text(text))

        frames, self.buffer, diag = extract_frames(self.buffer + text)
        if diag.overflow:
            logger.error(
                "Simulator: receive buffer overflowed without a complete message; "
                "dropping client connection"
            )
            self.buffer = ""
            if self.transport:
                self.transport.close()
            return

        for frame in frames:
            try:
                msg = json.loads(frame)
            except json.JSONDecodeError as err:
                logger.warning("Simulator: JSON parse error: %s", err)
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

    def _send(self, msg: dict):
        """Send a message to the client."""
        data = json.dumps(msg).encode("ascii")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Simulator TX: %s", sanitize_log_text(str(msg)))
        if self.transport:
            self.transport.write(data)

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
            logger.warning("Simulator: Unknown command: %s", sanitize_log_text(cmd))
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
            logger.warning("Simulator: Unknown command: %s", sanitize_log_text(cmd))
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Unknown command"
            self._send(response)
            return

        try:
            await handler(self, msg, response)
        except Exception:
            logger.exception("Simulator: Error handling command %s", sanitize_log_text(cmd))
            response = {
                FIELD_CMD: cmd,
                FIELD_SUCCESS: SUCCESS_FALSE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
                FIELD_REASON: "Command failed",
            }
            if msg_id is not None:
                response[FIELD_MSG_ID_RESPONSE] = msg_id

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

    @CommandRegistry.handler(CMD_GET_SCHEDULE)
    async def _handle_get_schedule(self, msg: dict, response: dict) -> None:
        index = msg.get(FIELD_INDEX)
        if index is not None and index in self.state.schedules:
            response[FIELD_SCHEDULE] = self.state.schedules[index].to_dict()
        else:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response[FIELD_REASON] = "Schedule not found"

    @CommandRegistry.handler(CMD_SET_SCHEDULE)
    async def _handle_set_schedule(self, msg: dict, response: dict) -> None:
        schedule_data = msg.get(FIELD_SCHEDULE)
        if schedule_data:
            schedule = Schedule.from_dict(schedule_data)
            self.state.schedules[schedule.index] = schedule
            response[FIELD_SCHEDULE] = schedule.to_dict()
            logger.info("Simulator: Schedule %s saved", sanitize_log_text(schedule.index))
        else:
            response[FIELD_SUCCESS] = SUCCESS_FALSE

    @CommandRegistry.handler(CMD_DELETE_SCHEDULE)
    async def _handle_delete_schedule(self, msg: dict, response: dict) -> None:
        index = msg.get(FIELD_INDEX)
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
        schedules_data = msg.get(FIELD_SCHEDULES, [])
        if isinstance(schedules_data, list):
            # Clear existing and load new schedules
            self.state.schedules.clear()
            for sched_data in schedules_data:
                schedule = Schedule.from_dict(sched_data)
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
            # Store the wire (POSIX) value as-is; schedule evaluation maps
            # it back to an IANA zone (see DoorSimulatorState.get_tzinfo).
            self.state.timezone = msg[FIELD_TZ]
        response[FIELD_TZ] = self.state.timezone

    @CommandRegistry.handler(CMD_SET_HOLD_TIME)
    async def _handle_set_hold_time(self, msg: dict, response: dict) -> None:
        if FIELD_HOLD_TIME in msg:
            # Convert centiseconds to seconds for internal storage
            self.state.hold_time = msg[FIELD_HOLD_TIME] / 100.0
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

        if FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS in settings:
            self.state.sensor_on_indoor = settings[FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS] == "1"
        if FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS in settings:
            self.state.sensor_off_indoor = settings[FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS] == "1"
        if FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS in settings:
            self.state.sensor_on_outdoor = settings[FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS] == "1"
        if FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS in settings:
            self.state.sensor_off_outdoor = settings[FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS] == "1"
        if FIELD_LOW_BATTERY_NOTIFICATIONS in settings:
            self.state.low_battery = settings[FIELD_LOW_BATTERY_NOTIFICATIONS] == "1"
        response[FIELD_NOTIFICATIONS] = self.state.get_notifications()

    @CommandRegistry.handler(CMD_SET_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_set_sensor_voltage(self, msg: dict, response: dict) -> None:
        if FIELD_SENSOR_TRIGGER_VOLTAGE in msg:
            self.state.sensor_trigger_voltage = msg[FIELD_SENSOR_TRIGGER_VOLTAGE]
        response[FIELD_SENSOR_TRIGGER_VOLTAGE] = self.state.sensor_trigger_voltage

    @CommandRegistry.handler(CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE)
    async def _handle_set_sleep_voltage(self, msg: dict, response: dict) -> None:
        if FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE in msg:
            self.state.sleep_sensor_trigger_voltage = msg[FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE]
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
