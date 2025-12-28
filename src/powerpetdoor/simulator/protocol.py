# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Protocol handler for Power Pet Door simulator.

This module contains the asyncio protocol for handling client connections
and the command registry for dispatching commands to handlers.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional, TYPE_CHECKING

from ..tz_utils import get_posix_tz_string, is_cache_initialized

from ..const import (
    COMMAND,
    PING,
    PONG,
    DOOR_STATUS,
    FIELD_MSG_ID,
    FIELD_MSG_ID_RESPONSE,
    FIELD_DIRECTION,
    FIELD_CMD,
    DOOR_TO_PHONE,
    DOOR_STATE_CLOSED,
    DOOR_STATE_RISING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_SLOWING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    CMD_GET_SETTINGS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_SENSORS,
    CMD_GET_POWER,
    CMD_GET_AUTO,
    CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_GET_CMD_LOCKOUT,
    CMD_GET_AUTORETRACT,
    CMD_GET_HW_INFO,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_TIMEZONE,
    CMD_GET_HOLD_TIME,
    CMD_GET_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_SCHEDULE_LIST,
    CMD_SET_SCHEDULE_LIST,
    CMD_GET_SCHEDULE,
    CMD_SET_SCHEDULE,
    CMD_DELETE_SCHEDULE,
    CMD_HAS_REMOTE_ID,
    CMD_HAS_REMOTE_KEY,
    CMD_CHECK_RESET_REASON,
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_CLOSE,
    CMD_ENABLE_INSIDE,
    CMD_DISABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_ENABLE_AUTO,
    CMD_DISABLE_AUTO,
    CMD_POWER_ON,
    CMD_POWER_OFF,
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_ENABLE_CMD_LOCKOUT,
    CMD_DISABLE_CMD_LOCKOUT,
    CMD_ENABLE_AUTORETRACT,
    CMD_DISABLE_AUTORETRACT,
    CMD_SET_TIMEZONE,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_POWER,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_AUTO,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_CMD_LOCKOUT,
    FIELD_AUTORETRACT,
    FIELD_SETTINGS,
    FIELD_NOTIFICATIONS,
    FIELD_TZ,
    FIELD_HOLD_TIME,
    FIELD_DOOR_STATUS,
    FIELD_SUCCESS,
    SUCCESS_TRUE,
    SUCCESS_FALSE,
    FIELD_FWINFO,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_AC_PRESENT,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_SCHEDULES,
    FIELD_SCHEDULE,
    FIELD_INDEX,
    NOTIFY_SENSOR_INDOOR,
    NOTIFY_SENSOR_OUTDOOR,
    FIELD_SENSOR_STATE,
)

from .state import DoorSimulatorState, Schedule

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


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
    def get(cls, cmd: str) -> Optional[Callable]:
        """Get the handler for a command, or None if not found."""
        return cls._handlers.get(cmd)


class DoorSimulatorProtocol(asyncio.Protocol):
    """Protocol handler for simulated door connections."""

    def __init__(
        self,
        state: DoorSimulatorState,
        on_command: Optional[Callable[[str, dict], None]] = None,
        broadcast_status: Optional[Callable[[], None]] = None,
    ):
        self.state = state
        self.on_command = on_command
        self.broadcast_status = broadcast_status
        self.transport: Optional[asyncio.Transport] = None
        self.buffer = ""
        self._door_task: Optional[asyncio.Task] = None
        self._hold_remaining: float = 0
        self._last_sensor_trigger: float = 0

    def connection_made(self, transport: asyncio.Transport):
        peername = transport.get_extra_info("peername")
        logger.info(f"Simulator: Client connected from {peername}")
        self.transport = transport

    def connection_lost(self, exc):
        logger.info("Simulator: Client disconnected")
        if self._door_task:
            self._door_task.cancel()

    def data_received(self, data: bytes):
        try:
            text = data.decode("ascii")
            logger.debug(f"Simulator RX: {text}")
            self.buffer += text

            # Parse complete JSON objects
            while self.buffer:
                end = self._find_json_end(self.buffer)
                if end is None:
                    break

                block = self.buffer[:end]
                self.buffer = self.buffer[end:]

                try:
                    msg = json.loads(block)
                    asyncio.create_task(self._handle_message(msg))
                except json.JSONDecodeError as e:
                    logger.error(f"Simulator: JSON parse error: {e}")

        except Exception as e:
            logger.error(f"Simulator: Error receiving data: {e}")

    def _find_json_end(self, s: str) -> Optional[int]:
        """Find the end of a JSON object in a string."""
        if not s or s[0] != "{":
            return None

        depth = 0
        for i, c in enumerate(s):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        return None

    def _send(self, msg: dict):
        """Send a message to the client."""
        data = json.dumps(msg).encode("ascii")
        logger.debug(f"Simulator TX: {msg}")
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
            self._send({FIELD_CMD: PONG, PONG: msg[PING], FIELD_SUCCESS: SUCCESS_TRUE, FIELD_DIRECTION: DOOR_TO_PHONE})
            return

        # Client sends commands under "config" key for queries, "cmd" for actions
        from ..const import CONFIG
        cmd = msg.get(CONFIG) or msg.get(COMMAND)
        if not cmd:
            return

        if self.on_command:
            self.on_command(cmd, msg)

        response = {FIELD_CMD: cmd, FIELD_SUCCESS: SUCCESS_TRUE, FIELD_DIRECTION: DOOR_TO_PHONE}
        if msg_id:
            response[FIELD_MSG_ID_RESPONSE] = msg_id

        # Check if command is allowed
        allowed, reason = self._check_command_allowed(cmd)
        if not allowed:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response["reason"] = reason
            self._send(response)
            return

        # Look up and execute handler
        handler = CommandRegistry.get(cmd)
        if handler:
            await handler(self, msg, response)
        else:
            logger.warning(f"Simulator: Unknown command: {cmd}")

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
        response[FIELD_SETTINGS] = {
            FIELD_CMD_LOCKOUT: "1" if self.state.cmd_lockout else "0"
        }

    @CommandRegistry.handler(CMD_GET_AUTORETRACT)
    async def _handle_get_autoretract(self, msg: dict, response: dict) -> None:
        response[FIELD_SETTINGS] = {
            FIELD_AUTORETRACT: "1" if self.state.autoretract else "0"
        }

    @CommandRegistry.handler(CMD_GET_HW_INFO)
    async def _handle_get_hw_info(self, msg: dict, response: dict) -> None:
        response[FIELD_FWINFO] = {
            "fw_maj": self.state.fw_major,
            "fw_min": self.state.fw_minor,
            "fw_pat": self.state.fw_patch,
        }

    @CommandRegistry.handler(CMD_GET_DOOR_BATTERY)
    async def _handle_get_battery(self, msg: dict, response: dict) -> None:
        response[FIELD_BATTERY_PERCENT] = self.state.battery_percent
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
        # Fallback to raw value if conversion fails
        response[FIELD_TZ] = iana_tz

    @CommandRegistry.handler(CMD_GET_HOLD_TIME)
    async def _handle_get_hold_time(self, msg: dict, response: dict) -> None:
        response[FIELD_HOLD_TIME] = self.state.hold_time

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
            response["reason"] = "Schedule not found"

    @CommandRegistry.handler(CMD_SET_SCHEDULE)
    async def _handle_set_schedule(self, msg: dict, response: dict) -> None:
        schedule_data = msg.get(FIELD_SCHEDULE)
        if schedule_data:
            schedule = Schedule.from_dict(schedule_data)
            self.state.schedules[schedule.index] = schedule
            response[FIELD_SCHEDULE] = schedule.to_dict()
            logger.info(f"Simulator: Schedule {schedule.index} saved")
        else:
            response[FIELD_SUCCESS] = SUCCESS_FALSE

    @CommandRegistry.handler(CMD_DELETE_SCHEDULE)
    async def _handle_delete_schedule(self, msg: dict, response: dict) -> None:
        index = msg.get(FIELD_INDEX)
        if index is not None and index in self.state.schedules:
            del self.state.schedules[index]
            logger.info(f"Simulator: Schedule {index} deleted")
        else:
            response[FIELD_SUCCESS] = SUCCESS_FALSE
            response["reason"] = "Schedule not found"

    @CommandRegistry.handler(CMD_SET_SCHEDULE_LIST)
    async def _handle_set_schedule_list(self, msg: dict, response: dict) -> None:
        schedules_data = msg.get(FIELD_SCHEDULES, [])
        if isinstance(schedules_data, list):
            # Clear existing and load new schedules
            self.state.schedules.clear()
            for sched_data in schedules_data:
                schedule = Schedule.from_dict(sched_data)
                self.state.schedules[schedule.index] = schedule
            logger.info(f"Simulator: Loaded {len(schedules_data)} schedules")
        response[FIELD_SCHEDULES] = self.state.get_schedule_list()

    # ==========================================================================
    # Command Handlers - Remote/Reset Info
    # ==========================================================================

    @CommandRegistry.handler(CMD_HAS_REMOTE_ID)
    async def _handle_has_remote_id(self, msg: dict, response: dict) -> None:
        response["hasRemoteId"] = "1" if self.state.has_remote_id else "0"

    @CommandRegistry.handler(CMD_HAS_REMOTE_KEY)
    async def _handle_has_remote_key(self, msg: dict, response: dict) -> None:
        response["hasRemoteKey"] = "1" if self.state.has_remote_key else "0"

    @CommandRegistry.handler(CMD_CHECK_RESET_REASON)
    async def _handle_check_reset_reason(self, msg: dict, response: dict) -> None:
        response["resetReason"] = self.state.reset_reason

    # ==========================================================================
    # Command Handlers - Door Commands
    # ==========================================================================

    @CommandRegistry.handler(CMD_OPEN)
    async def _handle_open(self, msg: dict, response: dict) -> None:
        await self._simulate_door_open(hold=False)
        response[FIELD_DOOR_STATUS] = self.state.door_status

    @CommandRegistry.handler(CMD_OPEN_AND_HOLD)
    async def _handle_open_and_hold(self, msg: dict, response: dict) -> None:
        await self._simulate_door_open(hold=True)
        response[FIELD_DOOR_STATUS] = self.state.door_status

    @CommandRegistry.handler(CMD_CLOSE)
    async def _handle_close(self, msg: dict, response: dict) -> None:
        await self._simulate_door_close()
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
            asyncio.create_task(self._simulate_door_close())

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
            self.state.timezone = msg[FIELD_TZ]
        response[FIELD_TZ] = self.state.timezone

    @CommandRegistry.handler(CMD_SET_HOLD_TIME)
    async def _handle_set_hold_time(self, msg: dict, response: dict) -> None:
        if FIELD_HOLD_TIME in msg:
            self.state.hold_time = msg[FIELD_HOLD_TIME]
            logger.info(f"Simulator: Hold time set to {self.state.hold_time}s")
        response[FIELD_HOLD_TIME] = self.state.hold_time

    @CommandRegistry.handler(CMD_SET_NOTIFICATIONS)
    async def _handle_set_notifications(self, msg: dict, response: dict) -> None:
        if FIELD_NOTIFICATIONS in msg:
            n = msg[FIELD_NOTIFICATIONS]
            if FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS in n:
                self.state.sensor_on_indoor = n[FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS] == "1"
            if FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS in n:
                self.state.sensor_off_indoor = n[FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS] == "1"
            if FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS in n:
                self.state.sensor_on_outdoor = n[FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS] == "1"
            if FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS in n:
                self.state.sensor_off_outdoor = n[FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS] == "1"
            if FIELD_LOW_BATTERY_NOTIFICATIONS in n:
                self.state.low_battery = n[FIELD_LOW_BATTERY_NOTIFICATIONS] == "1"
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
    # Door Operation Simulation
    # ==========================================================================

    async def _simulate_door_open(self, hold: bool = False):
        """Simulate door opening sequence with realistic timing."""
        # Cancel any existing door movement
        if self._door_task:
            self._door_task.cancel()
            try:
                await self._door_task
            except asyncio.CancelledError:
                pass

        self.state.door_status = DOOR_STATE_RISING
        self._broadcast_or_send_status()

        async def door_sequence():
            timing = self.state.timing

            # Door rises
            await asyncio.sleep(timing.rise_time)

            # Door slows as it approaches the top (still opening)
            self.state.door_status = DOOR_STATE_SLOWING
            self._broadcast_or_send_status()
            await asyncio.sleep(timing.slowing_time)

            if hold:
                self.state.door_status = DOOR_STATE_KEEPUP
                self._broadcast_or_send_status()
                # Hold indefinitely until explicit close
            else:
                self.state.door_status = DOOR_STATE_HOLDING
                self._broadcast_or_send_status()

                # Hold for the configured time, checking for sensor blocking close
                self._hold_remaining = float(self.state.hold_time)
                while self._hold_remaining > 0 or self.state.is_sensor_blocking_close():
                    # If sensor is blocking close, reset hold timer
                    if self.state.is_sensor_blocking_close():
                        logger.debug("Simulator: Sensor blocking close, resetting hold timer")
                        self._hold_remaining = float(self.state.hold_time)

                    await asyncio.sleep(0.1)
                    self._hold_remaining -= 0.1

                # Start closing
                await self._do_close_sequence()

        self._door_task = asyncio.create_task(door_sequence())

    async def _simulate_door_close(self):
        """Initiate door closing sequence."""
        if self._door_task:
            self._door_task.cancel()
            try:
                await self._door_task
            except asyncio.CancelledError:
                pass

        await self._do_close_sequence()

    async def _do_close_sequence(self):
        """Execute the door closing sequence with sensor detection."""
        timing = self.state.timing

        self.state.door_status = DOOR_STATE_CLOSING_TOP_OPEN
        self._broadcast_or_send_status()

        async def close_sequence():
            await asyncio.sleep(timing.closing_top_time)

            # Check for sensor blocking close
            if self.state.is_sensor_blocking_close() and self.state.autoretract:
                logger.info("Simulator: Sensor blocking close! Auto-retracting...")
                # Clear the active sensors
                self.state.inside_sensor_active = False
                self.state.outside_sensor_active = False
                self.state.total_auto_retracts += 1
                # Door auto-retracts (opens again)
                await self._simulate_door_open(hold=False)
                return

            self.state.door_status = DOOR_STATE_CLOSING_MID_OPEN
            self._broadcast_or_send_status()
            await asyncio.sleep(timing.closing_mid_time)

            # Final sensor check
            if self.state.is_sensor_blocking_close() and self.state.autoretract:
                logger.info("Simulator: Sensor blocking close! Auto-retracting...")
                # Clear the active sensors
                self.state.inside_sensor_active = False
                self.state.outside_sensor_active = False
                self.state.total_auto_retracts += 1
                await self._simulate_door_open(hold=False)
                return

            self.state.door_status = DOOR_STATE_CLOSED
            self._broadcast_or_send_status()
            self.state.total_open_cycles += 1

        self._door_task = asyncio.create_task(close_sequence())

    def _send_door_status(self):
        """Send unsolicited door status update to this client only."""
        self._send({
            FIELD_CMD: DOOR_STATUS,
            FIELD_DOOR_STATUS: self.state.door_status,
            FIELD_SUCCESS: SUCCESS_TRUE,
            FIELD_DIRECTION: DOOR_TO_PHONE,
        })

    def _broadcast_or_send_status(self):
        """Broadcast door status to all clients, or send to this client only."""
        if self.broadcast_status:
            self.broadcast_status()
        else:
            self._send_door_status()

    def _send_sensor_notification(self, sensor: str, state: str = "on"):
        """Send sensor trigger notification if enabled.

        Args:
            sensor: "inside" or "outside"
            state: "on" (sensor triggered) or "off" (sensor released)
        """
        # Check if notification is enabled
        if sensor == "inside":
            if state == "on" and not self.state.sensor_on_indoor:
                return
            if state == "off" and not self.state.sensor_off_indoor:
                return
            notify_type = NOTIFY_SENSOR_INDOOR
        else:  # outside
            if state == "on" and not self.state.sensor_on_outdoor:
                return
            if state == "off" and not self.state.sensor_off_outdoor:
                return
            notify_type = NOTIFY_SENSOR_OUTDOOR

        self._send({
            FIELD_CMD: notify_type,
            FIELD_SENSOR_STATE: state,
            FIELD_SUCCESS: SUCCESS_TRUE,
            FIELD_DIRECTION: DOOR_TO_PHONE,
        })
        logger.debug(f"Simulator: Sent {sensor} sensor {state} notification")

    def trigger_sensor(self, sensor: str):
        """Simulate a sensor trigger (pet walking through).

        Args:
            sensor: "inside" or "outside"
        """
        now = time.time()

        # Check if sensor is enabled and power is on
        if not self.state.power:
            logger.info(f"Simulator: Sensor {sensor} ignored (power OFF)")
            return

        # Check command lockout
        if self.state.cmd_lockout:
            logger.info(f"Simulator: Sensor {sensor} ignored (command lockout)")
            return

        if sensor == "inside" and not self.state.inside:
            logger.info("Simulator: Inside sensor ignored (disabled)")
            return

        if sensor == "outside":
            if not self.state.outside:
                logger.info("Simulator: Outside sensor ignored (disabled)")
                return
            if self.state.safety_lock:
                logger.info("Simulator: Outside sensor ignored (safety lock)")
                return

        # Check schedule enforcement
        if not self.state.is_sensor_allowed_by_schedule(sensor):
            logger.info(f"Simulator: {sensor.capitalize()} sensor ignored (outside schedule)")
            return

        # If door is already open/holding, re-trigger extends hold time
        if self.state.door_status in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP):
            if now - self._last_sensor_trigger > self.state.timing.sensor_retrigger_window:
                logger.info(f"Simulator: {sensor.capitalize()} sensor re-triggered, extending hold")
                self._hold_remaining = float(self.state.hold_time)
                self._last_sensor_trigger = now
                self._send_sensor_notification(sensor, "on")
            return

        # If door is closing, activate the sensor to trigger auto-retract
        if self.state.door_status in (
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
        ):
            logger.info(f"Simulator: {sensor.capitalize()} sensor during close, activating sensor")
            # Activate the appropriate sensor (mutually exclusive)
            if sensor == "inside":
                self.state.inside_sensor_active = True
                self.state.outside_sensor_active = False
            else:
                self.state.outside_sensor_active = True
                self.state.inside_sensor_active = False
            self._last_sensor_trigger = now
            self._send_sensor_notification(sensor, "on")
            return

        # Door is closed, trigger open
        logger.info(f"Simulator: {sensor.capitalize()} sensor triggered, opening door")
        self._last_sensor_trigger = now
        self._send_sensor_notification(sensor, "on")
        asyncio.create_task(self._simulate_door_open(hold=False))

    def simulate_obstruction(self):
        """Simulate obstruction detection (inside sensor active indefinitely).

        Works in any door state:
        - Closed/opening: Will prevent closing once door reaches HOLDING
        - Holding: Prevents closing
        - Closing: Triggers auto-retract if enabled
        """
        # Set inside sensor active (obstruction = something in the doorway)
        self.state.inside_sensor_active = True
        self.state.outside_sensor_active = False

        if self.state.door_status == DOOR_STATE_CLOSED:
            logger.info("Simulator: Obstruction set (will block close when door opens)")
        elif self.state.door_status in (DOOR_STATE_RISING, DOOR_STATE_SLOWING):
            logger.info("Simulator: Obstruction set (will block close when door reaches top)")
        elif self.state.door_status in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP):
            logger.info("Simulator: Obstruction set (blocking close)")
        elif self.state.door_status in (
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
        ):
            logger.info("Simulator: Obstruction during close (will trigger retract)")
        else:
            logger.info(f"Simulator: Obstruction set (door status: {self.state.door_status})")
