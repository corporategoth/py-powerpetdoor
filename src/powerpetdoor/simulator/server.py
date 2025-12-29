# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Power Pet Door simulator server.

This module contains the main DoorSimulator class that provides a TCP server
for simulating a Power Pet Door device.
"""

import asyncio
import logging
from typing import Optional

from ..const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_RISING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_SLOWING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    CMD_POWER_ON,
    CMD_POWER_OFF,
    CMD_ENABLE_AUTO,
    CMD_DISABLE_AUTO,
    CMD_ENABLE_INSIDE,
    CMD_DISABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_ENABLE_CMD_LOCKOUT,
    CMD_DISABLE_CMD_LOCKOUT,
    CMD_ENABLE_AUTORETRACT,
    CMD_DISABLE_AUTORETRACT,
    CMD_SET_HOLD_TIME,
    CMD_SET_TIMEZONE,
    CMD_SET_NOTIFICATIONS,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE_LIST,
    CMD_SET_SCHEDULE,
    CMD_DELETE_SCHEDULE,
    CMD_GET_SETTINGS,
    NOTIFY_LOW_BATTERY,
    DOOR_TO_PHONE,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_AC_PRESENT,
    FIELD_CMD,
    FIELD_CMD_LOCKOUT,
    FIELD_DIRECTION,
    FIELD_FWINFO,
    FIELD_HOLD_TIME,
    FIELD_INSIDE,
    FIELD_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_INDEX,
    FIELD_POWER,
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SETTINGS,
    FIELD_SUCCESS,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    FIELD_HW_VERSION,
    FIELD_HW_REVISION,
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
    SUCCESS_TRUE,
)

from ..tz_utils import get_posix_tz_string, is_cache_initialized
from .state import DoorSimulatorState, Schedule, BatteryConfig
from .protocol import DoorSimulatorProtocol

logger = logging.getLogger(__name__)

# Low battery threshold for notifications
LOW_BATTERY_THRESHOLD = 20


class DoorSimulator:
    """Power Pet Door simulator server.

    This class simulates a Power Pet Door device. It listens on a TCP
    port and responds to commands from PowerPetDoorClient.

    Example:
        simulator = DoorSimulator(port=3000)
        await simulator.start()

        # Simulate a pet triggering the inside sensor
        simulator.trigger_sensor("inside")

        # Or control programmatically
        await simulator.open_door()
        await simulator.close_door()

        await simulator.stop()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 3000,
        state: Optional[DoorSimulatorState] = None,
    ):
        self.host = host
        self.port = port
        self.state = state or DoorSimulatorState()
        self.server: Optional[asyncio.Server] = None
        self.protocols: list[DoorSimulatorProtocol] = []
        self._battery_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the simulator server."""
        loop = asyncio.get_running_loop()

        def handle_disconnect(protocol):
            if protocol in self.protocols:
                self.protocols.remove(protocol)

        def protocol_factory():
            protocol = DoorSimulatorProtocol(
                self.state,
                broadcast_status=self._broadcast_door_status,
                on_disconnect=handle_disconnect,
            )
            self.protocols.append(protocol)
            return protocol

        self.server = await loop.create_server(
            protocol_factory,
            self.host,
            self.port,
        )

        self._running = True
        self._battery_task = asyncio.create_task(self._battery_simulation_loop())

        logger.info(f"Door simulator listening on {self.host}:{self.port}")

    async def stop(self):
        """Stop the simulator server."""
        self._running = False

        if self._battery_task:
            self._battery_task.cancel()
            try:
                await self._battery_task
            except asyncio.CancelledError:
                pass
            self._battery_task = None

        # Close all client connections and cancel their tasks
        for protocol in self.protocols:
            if protocol._door_task:
                protocol._door_task.cancel()
                try:
                    await protocol._door_task
                except asyncio.CancelledError:
                    pass
            if protocol.transport:
                protocol.transport.close()
        self.protocols.clear()

        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("Door simulator stopped")

    # =========================================================================
    # Battery Simulation
    # =========================================================================

    async def _battery_simulation_loop(self):
        """Background task that simulates battery charge/discharge over time."""
        while self._running:
            try:
                config = self.state.battery_config
                await asyncio.sleep(config.update_interval)

                if not self._running:
                    break

                # Only simulate if battery is present
                if not self.state.battery_present:
                    continue

                old_percent = self.state.battery_percent

                if self.state.ac_present and config.charge_rate > 0:
                    # Charging: increase battery level
                    # Rate is per minute, interval is in seconds
                    delta = config.charge_rate * (config.update_interval / 60.0)
                    new_percent = min(100, self.state.battery_percent + delta)
                    if new_percent != self.state.battery_percent:
                        self.state.battery_percent = int(new_percent)
                        logger.debug(
                            f"Battery charging: {old_percent}% -> {self.state.battery_percent}%"
                        )
                        self._broadcast_battery_status()

                elif not self.state.ac_present and config.discharge_rate > 0:
                    # Discharging: decrease battery level
                    delta = config.discharge_rate * (config.update_interval / 60.0)
                    new_percent = max(0, self.state.battery_percent - delta)
                    if int(new_percent) != self.state.battery_percent:
                        self.state.battery_percent = int(new_percent)
                        logger.debug(
                            f"Battery discharging: {old_percent}% -> {self.state.battery_percent}%"
                        )
                        self._broadcast_battery_status()

                        # Check for low battery notification
                        if (
                            old_percent > LOW_BATTERY_THRESHOLD
                            and self.state.battery_percent <= LOW_BATTERY_THRESHOLD
                        ):
                            self._send_low_battery_notification()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in battery simulation: {e}")

    def _broadcast_battery_status(self):
        """Broadcast battery status to all connected clients."""
        # Report 0% if battery is not present
        percent = self.state.battery_percent if self.state.battery_present else 0
        for protocol in self.protocols:
            protocol._send({
                "CMD": CMD_GET_DOOR_BATTERY,
                FIELD_BATTERY_PERCENT: percent,
                FIELD_BATTERY_PRESENT: "1" if self.state.battery_present else "0",
                FIELD_AC_PRESENT: "1" if self.state.ac_present else "0",
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def _send_low_battery_notification(self):
        """Send low battery notification to connected clients."""
        if self.state.low_battery:
            for protocol in self.protocols:
                protocol._send({
                    "CMD": NOTIFY_LOW_BATTERY,
                    FIELD_BATTERY_PERCENT: self.state.battery_percent,
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                })
            logger.info(f"Simulator: Low battery notification ({self.state.battery_percent}%)")

    def broadcast_settings(self):
        """Broadcast settings to all connected clients."""
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: CMD_GET_SETTINGS,
                FIELD_SETTINGS: self.state.get_settings(),
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_safety_lock(self, enabled: bool):
        """Broadcast safety lock setting change to all connected clients."""
        cmd = CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK if enabled else CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: cmd,
                FIELD_SETTINGS: {FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: "1" if enabled else "0"},
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_cmd_lockout(self, enabled: bool):
        """Broadcast command lockout setting change to all connected clients."""
        cmd = CMD_ENABLE_CMD_LOCKOUT if enabled else CMD_DISABLE_CMD_LOCKOUT
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: cmd,
                FIELD_SETTINGS: {FIELD_CMD_LOCKOUT: "1" if enabled else "0"},
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_autoretract(self, enabled: bool):
        """Broadcast autoretract setting change to all connected clients."""
        cmd = CMD_ENABLE_AUTORETRACT if enabled else CMD_DISABLE_AUTORETRACT
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: cmd,
                FIELD_SETTINGS: {FIELD_AUTORETRACT: "1" if enabled else "0"},
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_hold_time(self):
        """Broadcast hold time setting change to all connected clients."""
        # Convert seconds to centiseconds for protocol
        hold_time_cs = int(self.state.hold_time * 100)
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: CMD_SET_HOLD_TIME,
                FIELD_HOLD_TIME: hold_time_cs,
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_timezone(self):
        """Broadcast timezone setting change to all connected clients."""
        # Convert IANA to POSIX if possible
        tz_value = self.state.timezone
        if is_cache_initialized():
            posix_tz = get_posix_tz_string(self.state.timezone)
            if posix_tz:
                tz_value = posix_tz
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: CMD_SET_TIMEZONE,
                FIELD_TZ: tz_value,
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_notification_settings(self):
        """Broadcast notification settings change to all connected clients."""
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: CMD_SET_NOTIFICATIONS,
                FIELD_NOTIFICATIONS: self.state.get_notifications(),
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_power(self, enabled: bool):
        """Broadcast power setting change to all connected clients."""
        cmd = CMD_POWER_ON if enabled else CMD_POWER_OFF
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: cmd,
                FIELD_POWER: "1" if enabled else "0",
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_auto(self, enabled: bool):
        """Broadcast auto/timers setting change to all connected clients."""
        cmd = CMD_ENABLE_AUTO if enabled else CMD_DISABLE_AUTO
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: cmd,
                FIELD_AUTO: "1" if enabled else "0",
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_inside_sensor(self, enabled: bool):
        """Broadcast inside sensor enable/disable to all connected clients."""
        cmd = CMD_ENABLE_INSIDE if enabled else CMD_DISABLE_INSIDE
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: cmd,
                FIELD_INSIDE: "1" if enabled else "0",
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_outside_sensor(self, enabled: bool):
        """Broadcast outside sensor enable/disable to all connected clients."""
        cmd = CMD_ENABLE_OUTSIDE if enabled else CMD_DISABLE_OUTSIDE
        for protocol in self.protocols:
            protocol._send({
                FIELD_CMD: cmd,
                FIELD_OUTSIDE: "1" if enabled else "0",
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_hardware_info(self):
        """Broadcast hardware/firmware info to all connected clients."""
        for protocol in self.protocols:
            protocol._send({
                "CMD": CMD_GET_HW_INFO,
                FIELD_FWINFO: {
                    FIELD_FW_MAJOR: self.state.fw_major,
                    FIELD_FW_MINOR: self.state.fw_minor,
                    FIELD_FW_PATCH: self.state.fw_patch,
                    FIELD_HW_VERSION: self.state.hw_ver,
                    FIELD_HW_REVISION: self.state.hw_rev,
                },
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_stats(self):
        """Broadcast door open statistics to all connected clients."""
        for protocol in self.protocols:
            protocol._send({
                "CMD": CMD_GET_DOOR_OPEN_STATS,
                FIELD_TOTAL_OPEN_CYCLES: self.state.total_open_cycles,
                FIELD_TOTAL_AUTO_RETRACTS: self.state.total_auto_retracts,
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_schedules(self):
        """Broadcast schedule list to all connected clients."""
        for protocol in self.protocols:
            protocol._send({
                "CMD": CMD_GET_SCHEDULE_LIST,
                FIELD_SCHEDULES: self.state.get_schedule_list(),
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_schedule(self, schedule: Schedule):
        """Broadcast a single schedule add/update to all connected clients."""
        for protocol in self.protocols:
            protocol._send({
                "CMD": CMD_SET_SCHEDULE,
                FIELD_SCHEDULE: schedule.to_dict(),
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_schedule_delete(self, index: int):
        """Broadcast a schedule deletion to all connected clients."""
        for protocol in self.protocols:
            protocol._send({
                "CMD": CMD_DELETE_SCHEDULE,
                FIELD_INDEX: index,
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_notifications(self):
        """Broadcast notification settings to all connected clients."""
        for protocol in self.protocols:
            protocol._send({
                "CMD": CMD_GET_NOTIFICATIONS,
                FIELD_NOTIFICATIONS: self.state.get_notifications(),
                FIELD_SUCCESS: SUCCESS_TRUE,
                FIELD_DIRECTION: DOOR_TO_PHONE,
            })

    def broadcast_all(self):
        """Broadcast all state information to all connected clients."""
        self._broadcast_door_status()
        self.broadcast_settings()
        self._broadcast_battery_status()
        self.broadcast_hardware_info()
        self.broadcast_stats()
        self.broadcast_schedules()
        self.broadcast_notifications()

    # =========================================================================
    # Spontaneous Events (simulate from door side)
    # =========================================================================

    def trigger_sensor(self, sensor: str):
        """Simulate a sensor trigger (pet walking through).

        Works both with and without connected clients.

        Args:
            sensor: "inside" or "outside"
        """
        if self.protocols:
            # If clients connected, use the first protocol's trigger_sensor.
            # Status updates will be broadcast to all clients via broadcast_status callback.
            self.protocols[0].trigger_sensor(sensor)
        else:
            # No clients connected - directly simulate the sensor trigger
            self._direct_trigger_sensor(sensor)

    def _direct_trigger_sensor(self, sensor: str):
        """Directly trigger a sensor without requiring a connected client.

        This is used when running scripts without a client connection.
        """
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

        # Door is closed, trigger open
        logger.info(f"Simulator: {sensor.capitalize()} sensor triggered, opening door")
        asyncio.create_task(self._direct_open_door(hold=False))

    async def _direct_open_door(self, hold: bool = False):
        """Open door directly without a client connection.

        State-aware behavior:
        - If already open (HOLDING/KEEPUP): do nothing
        - If already opening (RISING/SLOWING): do nothing
        - If closing: reverse to equivalent opening state and continue
        - If closed: start full opening sequence
        """
        current_status = self.state.door_status
        timing = self.state.timing

        # Already open - do nothing
        if current_status in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP):
            logger.debug("Simulator: Open command ignored (already open)")
            return

        # Already opening - do nothing
        if current_status in (DOOR_STATE_RISING, DOOR_STATE_SLOWING):
            logger.debug("Simulator: Open command ignored (already opening)")
            return

        # Determine starting state based on current position
        if current_status == DOOR_STATE_CLOSING_TOP_OPEN:
            start_state = DOOR_STATE_SLOWING
            skip_rising = True
            logger.info("Simulator: Reversing close at top, continuing to open")
        elif current_status == DOOR_STATE_CLOSING_MID_OPEN:
            start_state = DOOR_STATE_RISING
            skip_rising = False
            logger.info("Simulator: Reversing close at mid, continuing to open")
        else:
            start_state = DOOR_STATE_RISING
            skip_rising = False

        self.state.door_status = start_state
        self._broadcast_door_status()

        if not skip_rising:
            await asyncio.sleep(timing.rise_time)

            # Door slows as it approaches the top (still opening)
            self.state.door_status = DOOR_STATE_SLOWING
            self._broadcast_door_status()

        await asyncio.sleep(timing.slowing_time)

        if hold:
            self.state.door_status = DOOR_STATE_KEEPUP
            self._broadcast_door_status()
        else:
            self.state.door_status = DOOR_STATE_HOLDING
            self._broadcast_door_status()

            # Hold for configured time, checking for sensor blocking close
            hold_remaining = float(self.state.hold_time)
            while hold_remaining > 0 or self.state.is_sensor_blocking_close():
                # If sensor is blocking close, reset hold timer
                if self.state.is_sensor_blocking_close():
                    logger.debug("Simulator: Sensor blocking close, resetting hold timer")
                    hold_remaining = float(self.state.hold_time)

                await asyncio.sleep(0.1)
                hold_remaining -= 0.1

            # Close
            await self._direct_close_door()

    async def _direct_close_door(
        self,
        start_state: str = DOOR_STATE_CLOSING_TOP_OPEN,
        skip_top: bool = False,
    ):
        """Close door directly without a client connection.

        State-aware behavior:
        - If already closed: do nothing
        - If already closing: do nothing
        - If opening: reverse to equivalent closing state and continue
        - If open (HOLDING/KEEPUP): start full closing sequence

        Args:
            start_state: The initial closing state (for internal use during reversal).
            skip_top: If True, skip the CLOSING_TOP_OPEN phase (for internal use).
        """
        current_status = self.state.door_status
        timing = self.state.timing

        # Already closed - do nothing
        if current_status == DOOR_STATE_CLOSED:
            logger.debug("Simulator: Close command ignored (already closed)")
            return

        # Already closing - do nothing
        if current_status in (DOOR_STATE_CLOSING_TOP_OPEN, DOOR_STATE_CLOSING_MID_OPEN):
            logger.debug("Simulator: Close command ignored (already closing)")
            return

        # Determine starting state based on current position (only if not already set)
        if start_state == DOOR_STATE_CLOSING_TOP_OPEN and not skip_top:
            if current_status == DOOR_STATE_RISING:
                start_state = DOOR_STATE_CLOSING_MID_OPEN
                skip_top = True
                logger.info("Simulator: Reversing open at rising, closing from mid")
            elif current_status == DOOR_STATE_SLOWING:
                start_state = DOOR_STATE_CLOSING_TOP_OPEN
                skip_top = False
                logger.info("Simulator: Reversing open at slowing, closing from top")

        self.state.door_status = start_state
        self._broadcast_door_status()

        if not skip_top:
            await asyncio.sleep(timing.closing_top_time)

            # Check for sensor blocking close after closing top
            if await self._check_sensor_retract():
                return

            self.state.door_status = DOOR_STATE_CLOSING_MID_OPEN
            self._broadcast_door_status()

        await asyncio.sleep(timing.closing_mid_time)

        # Check for sensor blocking close after closing mid
        if await self._check_sensor_retract():
            return

        self.state.door_status = DOOR_STATE_CLOSED
        self._broadcast_door_status()
        self.state.total_open_cycles += 1

    async def _check_sensor_retract(self) -> bool:
        """Check for sensor blocking close and auto-retract if enabled.

        Returns True if door was retracted (caller should return early).
        """
        if self.state.is_sensor_blocking_close() and self.state.autoretract:
            logger.info("Simulator: Sensor blocking close! Auto-retracting...")
            # Clear the active sensors
            self.state.inside_sensor_active = False
            self.state.outside_sensor_active = False
            self.state.total_auto_retracts += 1
            await self._direct_open_door(hold=False)
            return True
        return False

    def _broadcast_door_status(self):
        """Broadcast door status to all connected clients."""
        for protocol in self.protocols:
            protocol._send_door_status()

    def simulate_obstruction(self):
        """Simulate obstruction detection (inside sensor active indefinitely).

        Works in any door state:
        - Closed/opening: Will prevent closing once door reaches HOLDING
        - Holding: Prevents closing
        - Closing: Triggers auto-retract if enabled
        """
        if self.protocols:
            # Use protocol method for proper handling
            self.protocols[0].simulate_obstruction()
        else:
            # Direct simulation without clients - set inside sensor active
            self.state.inside_sensor_active = True
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

    def activate_sensor(self, sensor: str, duration: float = 0.5):
        """Activate sensor detection with optional duration.

        Args:
            sensor: "inside" or "outside"
            duration: How long sensor stays active in seconds.
                     0 = toggle mode (on indefinitely if off, off if on)
                     >0 = active for that duration then auto-deactivates

        This is mutually exclusive - activating one sensor clears the other.
        If door is closed, triggers a door cycle (respecting sensor enable and safety).
        """
        # Mutually exclusive - clear the other sensor
        if sensor == "inside":
            self.state.outside_sensor_active = False
            if duration == 0:
                # Toggle mode
                self.state.inside_sensor_active = not self.state.inside_sensor_active
                logger.info(
                    f"Simulator: Inside sensor {'activated' if self.state.inside_sensor_active else 'deactivated'} (toggle)"
                )
            else:
                self.state.inside_sensor_active = True
                logger.info(f"Simulator: Inside sensor activated for {duration}s")
                # Schedule deactivation
                asyncio.create_task(self._deactivate_sensor_after("inside", duration))
        elif sensor == "outside":
            self.state.inside_sensor_active = False
            if duration == 0:
                # Toggle mode
                self.state.outside_sensor_active = not self.state.outside_sensor_active
                logger.info(
                    f"Simulator: Outside sensor {'activated' if self.state.outside_sensor_active else 'deactivated'} (toggle)"
                )
            else:
                self.state.outside_sensor_active = True
                logger.info(f"Simulator: Outside sensor activated for {duration}s")
                # Schedule deactivation
                asyncio.create_task(self._deactivate_sensor_after("outside", duration))

        # If door is closed and sensor should trigger, open the door
        if self.state.door_status == DOOR_STATE_CLOSED:
            should_trigger = False
            if sensor == "inside" and self.state.inside_sensor_active:
                # Inside sensor: check if enabled and power on
                if self.state.power and self.state.inside:
                    should_trigger = True
            elif sensor == "outside" and self.state.outside_sensor_active:
                # Outside sensor: check if enabled, power on, and not safety locked
                if self.state.power and self.state.outside and not self.state.safety_lock:
                    should_trigger = True

            if should_trigger:
                logger.info(f"Simulator: {sensor.capitalize()} sensor triggering door cycle")
                asyncio.create_task(self._direct_open_door(hold=False))

    async def _deactivate_sensor_after(self, sensor: str, duration: float):
        """Deactivate sensor after specified duration."""
        await asyncio.sleep(duration)
        if sensor == "inside" and self.state.inside_sensor_active:
            self.state.inside_sensor_active = False
            logger.info("Simulator: Inside sensor deactivated (duration expired)")
        elif sensor == "outside" and self.state.outside_sensor_active:
            self.state.outside_sensor_active = False
            logger.info("Simulator: Outside sensor deactivated (duration expired)")

    def set_pet_in_doorway(self, present: bool = True):
        """Simulate pet presence in doorway (keeps door open longer).

        This is an alias for activate_sensor("inside", 0) for backwards compatibility.
        """
        if present:
            self.state.inside_sensor_active = True
            self.state.outside_sensor_active = False
        else:
            self.state.inside_sensor_active = False
        logger.info(f"Simulator: Pet {'in' if present else 'left'} doorway")

    # =========================================================================
    # Door Control
    # =========================================================================

    async def open_door(self, hold: bool = False):
        """Open the door (as if triggered by sensor or schedule).

        Works with or without connected clients.
        """
        if self.protocols:
            # Only need to trigger once - status broadcasts go to all clients
            await self.protocols[0]._simulate_door_open(hold=hold)
        else:
            await self._direct_open_door(hold=hold)

    async def close_door(self):
        """Close the door.

        Works with or without connected clients.
        """
        if self.protocols:
            # Only need to trigger once - status broadcasts go to all clients
            await self.protocols[0]._simulate_door_close()
        else:
            await self._direct_close_door()

    # =========================================================================
    # State Management
    # =========================================================================

    def set_battery(self, percent: int):
        """Set battery percentage and notify connected clients.

        Sends a low battery notification if battery drops below 20%
        and low battery notifications are enabled.
        """
        old_percent = self.state.battery_percent
        self.state.battery_percent = max(0, min(100, percent))

        self._broadcast_battery_status()

        # Send low battery notification if crossing threshold
        if old_percent > LOW_BATTERY_THRESHOLD and percent <= LOW_BATTERY_THRESHOLD:
            self._send_low_battery_notification()

    def set_ac_present(self, present: bool):
        """Set AC power connection state and notify clients.

        Args:
            present: True if AC is connected, False if disconnected.
        """
        if self.state.ac_present == present:
            return

        self.state.ac_present = present
        logger.info(f"Simulator: AC {'connected' if present else 'disconnected'}")
        self._broadcast_battery_status()

    def set_battery_present(self, present: bool):
        """Set battery presence state and notify clients.

        Args:
            present: True if battery is installed, False if removed.
        """
        if self.state.battery_present == present:
            return

        self.state.battery_present = present
        logger.info(f"Simulator: Battery {'installed' if present else 'removed'}")
        self._broadcast_battery_status()

    def set_charge_rate(self, rate: float):
        """Set battery charge rate (percent per minute).

        Args:
            rate: Charge rate in percent per minute. Set to 0 to disable charging.
        """
        self.state.battery_config.charge_rate = max(0.0, rate)
        logger.info(f"Simulator: Charge rate set to {rate}%/min")

    def set_discharge_rate(self, rate: float):
        """Set battery discharge rate (percent per minute).

        Args:
            rate: Discharge rate in percent per minute. Set to 0 to disable discharging.
        """
        self.state.battery_config.discharge_rate = max(0.0, rate)
        logger.info(f"Simulator: Discharge rate set to {rate}%/min")

    def set_power(self, enabled: bool):
        """Set power state."""
        self.state.power = enabled
        logger.info(f"Simulator: Power {'ON' if enabled else 'OFF'}")

    # =========================================================================
    # Schedule Management
    # =========================================================================

    def add_schedule(self, schedule: Schedule):
        """Add or update a schedule."""
        self.state.schedules[schedule.index] = schedule
        logger.info(f"Simulator: Added schedule {schedule.index}")
        self.broadcast_schedule(schedule)

    def remove_schedule(self, index: int):
        """Remove a schedule by index."""
        if index in self.state.schedules:
            del self.state.schedules[index]
            logger.info(f"Simulator: Removed schedule {index}")
            self.broadcast_schedule_delete(index)
