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
    CMD_GET_DOOR_BATTERY,
    NOTIFY_LOW_BATTERY,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_AC_PRESENT,
    FIELD_SUCCESS,
)

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

        def protocol_factory():
            protocol = DoorSimulatorProtocol(
                self.state,
                broadcast_status=self._broadcast_door_status,
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
        for protocol in self.protocols:
            protocol._send({
                "CMD": CMD_GET_DOOR_BATTERY,
                FIELD_BATTERY_PERCENT: self.state.battery_percent,
                FIELD_BATTERY_PRESENT: "1" if self.state.battery_present else "0",
                FIELD_AC_PRESENT: "1" if self.state.ac_present else "0",
                FIELD_SUCCESS: "true",
            })

    def _send_low_battery_notification(self):
        """Send low battery notification to connected clients."""
        if self.state.low_battery:
            for protocol in self.protocols:
                protocol._send({
                    "CMD": NOTIFY_LOW_BATTERY,
                    FIELD_BATTERY_PERCENT: self.state.battery_percent,
                    FIELD_SUCCESS: "true",
                })
            logger.info(f"Simulator: Low battery notification ({self.state.battery_percent}%)")

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
        """Open door directly without a client connection."""
        timing = self.state.timing

        self.state.door_status = DOOR_STATE_RISING
        self._broadcast_door_status()

        await asyncio.sleep(timing.rise_time)

        if hold:
            self.state.door_status = DOOR_STATE_KEEPUP
            self._broadcast_door_status()
        else:
            self.state.door_status = DOOR_STATE_HOLDING
            self._broadcast_door_status()

            # Hold for configured time, checking for pet presence
            hold_remaining = float(self.state.hold_time)
            while hold_remaining > 0:
                # If pet is in doorway, reset hold timer
                if self.state.pet_in_doorway:
                    logger.info("Simulator: Pet detected in doorway, resetting hold timer")
                    hold_remaining = float(self.state.hold_time)
                    self.state.pet_in_doorway = False

                await asyncio.sleep(0.1)
                hold_remaining -= 0.1

            # Close
            await self._direct_close_door()

    async def _direct_close_door(self):
        """Close door directly without a client connection."""
        timing = self.state.timing

        self.state.door_status = DOOR_STATE_SLOWING
        self._broadcast_door_status()
        await asyncio.sleep(timing.slowing_time)

        # Check for obstruction after slowing
        if await self._check_obstruction_retract():
            return

        self.state.door_status = DOOR_STATE_CLOSING_TOP_OPEN
        self._broadcast_door_status()
        await asyncio.sleep(timing.closing_top_time)

        # Check for obstruction after closing top
        if await self._check_obstruction_retract():
            return

        self.state.door_status = DOOR_STATE_CLOSING_MID_OPEN
        self._broadcast_door_status()
        await asyncio.sleep(timing.closing_mid_time)

        # Check for obstruction after closing mid
        if await self._check_obstruction_retract():
            return

        self.state.door_status = DOOR_STATE_CLOSED
        self._broadcast_door_status()
        self.state.total_open_cycles += 1

    async def _check_obstruction_retract(self) -> bool:
        """Check for obstruction and auto-retract if enabled.

        Returns True if door was retracted (caller should return early).
        """
        if self.state.obstruction_pending and self.state.autoretract:
            logger.info("Simulator: Obstruction detected! Auto-retracting...")
            self.state.obstruction_pending = False
            self.state.total_auto_retracts += 1
            await self._direct_open_door(hold=False)
            return True
        return False

    def _broadcast_door_status(self):
        """Broadcast door status to all connected clients."""
        for protocol in self.protocols:
            protocol._send_door_status()

    def simulate_obstruction(self):
        """Simulate obstruction detection during door close.

        If autoretract is enabled, the door will auto-retract (reopen).
        """
        if self.protocols:
            # Only need to set obstruction_pending once (shared state)
            self.protocols[0].simulate_obstruction()
        else:
            # Direct simulation without clients
            if self.state.door_status in (
                DOOR_STATE_SLOWING,
                DOOR_STATE_CLOSING_TOP_OPEN,
                DOOR_STATE_CLOSING_MID_OPEN,
            ):
                logger.info("Simulator: Obstruction detected during close")
                self.state.obstruction_pending = True
            else:
                logger.info(
                    f"Simulator: Obstruction ignored (door status: {self.state.door_status})"
                )

    def set_pet_in_doorway(self, present: bool = True):
        """Simulate pet presence in doorway (keeps door open longer)."""
        self.state.pet_in_doorway = present
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

    def remove_schedule(self, index: int):
        """Remove a schedule by index."""
        if index in self.state.schedules:
            del self.state.schedules[index]
            logger.info(f"Simulator: Removed schedule {index}")
