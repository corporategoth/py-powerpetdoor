# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Power Pet Door simulator server.

This module contains the main DoorSimulator class that provides a TCP server
for simulating a Power Pet Door device.

All door motion is delegated to the single :class:`DoorMotionEngine`
(see engine.py), shared by the protocol path (connected clients) and the
no-client path (CLI commands and scripts), so door behavior is identical
either way.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable

from ..const import (
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
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SETTINGS,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_TIMEZONE,
    DOOR_TO_PHONE,
    FIELD_AC_PRESENT,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD,
    FIELD_CMD_LOCKOUT,
    FIELD_DIRECTION,
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
    FIELD_FWINFO,
    FIELD_HOLD_TIME,
    FIELD_HW_REVISION,
    FIELD_HW_VERSION,
    FIELD_INDEX,
    FIELD_INSIDE,
    FIELD_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SETTINGS,
    FIELD_SUCCESS,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    NOTIFY_LOW_BATTERY,
    SENSOR_STATE_ON,
    SUCCESS_TRUE,
)
from ..tz_utils import get_posix_tz_string, is_cache_initialized
from .engine import DoorMotionEngine
from .protocol import DoorSimulatorProtocol, make_sensor_notification
from .state import DoorSimulatorState, Schedule

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

        # Deterministically wait for a door state (tests/scripts)
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=5)

        await simulator.stop()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 3000,
        state: DoorSimulatorState | None = None,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ):
        self.host = host
        self.port = port
        self.state = state or DoorSimulatorState()
        self.server: asyncio.Server | None = None
        self.protocols: list[DoorSimulatorProtocol] = []
        self.engine = DoorMotionEngine(
            self.state,
            broadcast_status=self._broadcast_door_status,
            notify_sensor=self._broadcast_sensor_notification,
        )
        self._battery_task: asyncio.Task | None = None
        # Fractional battery percent accumulated between integer steps, so
        # rates below 1%/interval still charge/discharge correctly.
        self._battery_carry = 0.0
        self._running = False
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

    async def start(self):
        """Start the simulator server."""
        loop = asyncio.get_running_loop()

        def handle_disconnect(protocol):
            if protocol in self.protocols:
                self.protocols.remove(protocol)
                if self._on_disconnect:
                    self._on_disconnect()

        def protocol_factory():
            protocol = DoorSimulatorProtocol(
                self.state,
                broadcast_status=self._broadcast_door_status,
                on_disconnect=handle_disconnect,
                engine=self.engine,
            )
            self.protocols.append(protocol)
            if self._on_connect:
                self._on_connect()
            return protocol

        self.server = await loop.create_server(
            protocol_factory,
            self.host,
            self.port,
        )

        self._running = True
        self._battery_task = asyncio.create_task(self._battery_simulation_loop())

        logger.info("Door simulator listening on %s:%s", self.host, self.port)

    async def stop(self):
        """Stop the simulator server and all of its tasks."""
        self._running = False

        if self._battery_task:
            self._battery_task.cancel()
            try:
                await self._battery_task
            except asyncio.CancelledError:
                pass
            self._battery_task = None

        # Stop the door-motion engine (cancels and awaits its tasks)
        await self.engine.stop()

        # Close all client connections and await their tasks
        for protocol in list(self.protocols):
            await protocol.aclose()
        self.protocols.clear()

        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("Door simulator stopped")

    # =========================================================================
    # Deterministic status hooks (delegated to the engine)
    # =========================================================================

    async def wait_for_status(
        self, status: str | Iterable[str], timeout: float | None = None
    ) -> str:
        """Wait until the door reaches (one of) the given status value(s).

        See :meth:`DoorMotionEngine.wait_for_status`.
        """
        return await self.engine.wait_for_status(status, timeout)

    def add_status_listener(self, callback: Callable[[str], None]) -> Callable[[], None]:
        """Register a door-status change callback; returns an unsubscriber.

        See :meth:`DoorMotionEngine.add_status_listener`.
        """
        return self.engine.add_status_listener(callback)

    # =========================================================================
    # Battery Simulation
    # =========================================================================

    async def _battery_simulation_loop(self):
        """Background task that simulates battery charge/discharge over time."""
        while self._running:
            try:
                await asyncio.sleep(self.state.battery_config.update_interval)

                if not self._running:
                    break

                self._battery_tick()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in battery simulation")

    def _battery_tick(self):
        """Apply one battery charge/discharge step.

        Called once per ``update_interval`` by the simulation loop; exposed
        separately so tests can drive it deterministically. Fractional
        percent deltas accumulate in ``_battery_carry`` until a whole
        percent is reached, so rates below 1%/interval work correctly.
        """
        state = self.state
        config = state.battery_config

        # Only simulate if battery is present
        if not state.battery_present:
            return

        if state.ac_present and config.charge_rate > 0:
            # Charging: increase battery level (rate is per minute)
            delta = config.charge_rate * (config.update_interval / 60.0)
        elif not state.ac_present and config.discharge_rate > 0:
            # Discharging: decrease battery level
            delta = -config.discharge_rate * (config.update_interval / 60.0)
        else:
            return

        self._battery_carry += delta
        step = int(self._battery_carry)  # whole percent, truncated toward zero
        if step == 0:
            return
        self._battery_carry -= step

        old_percent = state.battery_percent
        new_percent = max(0, min(100, old_percent + step))
        if new_percent == old_percent:
            # Pinned at the cap/floor: drop the remainder so it cannot
            # offset a later direction change.
            self._battery_carry = 0.0
            return

        state.battery_percent = new_percent
        logger.debug(
            "Battery %s: %s%% -> %s%%",
            "charging" if step > 0 else "discharging",
            old_percent,
            new_percent,
        )
        self._broadcast_battery_status()

        # Check for low battery notification (discharge crossing the threshold)
        if (
            step < 0
            and old_percent > LOW_BATTERY_THRESHOLD
            and new_percent <= LOW_BATTERY_THRESHOLD
        ):
            self._send_low_battery_notification()

    def _broadcast_battery_status(self):
        """Broadcast battery status to all connected clients."""
        # Report 0% if battery is not present
        percent = self.state.battery_percent if self.state.battery_present else 0
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_GET_DOOR_BATTERY,
                    FIELD_BATTERY_PERCENT: percent,
                    FIELD_BATTERY_PRESENT: "1" if self.state.battery_present else "0",
                    FIELD_AC_PRESENT: "1" if self.state.ac_present else "0",
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def _send_low_battery_notification(self):
        """Send low battery notification to connected clients.

        Uses the bare notification envelope from docs/protocol.md
        ("Notification Events"): ``{"LOW_BATTERY": ""}``.
        """
        if self.state.low_battery:
            for protocol in self.protocols:
                protocol._send({NOTIFY_LOW_BATTERY: ""})
            logger.info("Simulator: Low battery notification (%s%%)", self.state.battery_percent)

    def _broadcast_sensor_notification(self, sensor: str, sensor_state: str = SENSOR_STATE_ON):
        """Send a sensor notification event to all connected clients.

        Uses the bare notification envelope from docs/protocol.md; respects
        the per-event notification enable settings.
        """
        notification = make_sensor_notification(self.state, sensor, sensor_state)
        if notification is None:
            return
        for protocol in self.protocols:
            protocol._send(notification)
        logger.debug("Simulator: Sent %s sensor %s notification", sensor, sensor_state)

    def broadcast_settings(self):
        """Broadcast settings to all connected clients."""
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_GET_SETTINGS,
                    FIELD_SETTINGS: self.state.get_settings(),
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_safety_lock(self, enabled: bool):
        """Broadcast safety lock setting change to all connected clients."""
        cmd = (
            CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK
            if enabled
            else CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK
        )
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: cmd,
                    FIELD_SETTINGS: {FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: "1" if enabled else "0"},
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )
        self.engine.notify_sensors_changed()

    def broadcast_cmd_lockout(self, enabled: bool):
        """Broadcast command lockout setting change to all connected clients."""
        cmd = CMD_ENABLE_CMD_LOCKOUT if enabled else CMD_DISABLE_CMD_LOCKOUT
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: cmd,
                    FIELD_SETTINGS: {FIELD_CMD_LOCKOUT: "1" if enabled else "0"},
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )
        self.engine.notify_sensors_changed()

    def broadcast_autoretract(self, enabled: bool):
        """Broadcast autoretract setting change to all connected clients."""
        cmd = CMD_ENABLE_AUTORETRACT if enabled else CMD_DISABLE_AUTORETRACT
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: cmd,
                    FIELD_SETTINGS: {FIELD_AUTORETRACT: "1" if enabled else "0"},
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_hold_time(self):
        """Broadcast hold time setting change to all connected clients."""
        # Convert seconds to centiseconds for protocol
        hold_time_cs = int(self.state.hold_time * 100)
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_SET_HOLD_TIME,
                    FIELD_HOLD_TIME: hold_time_cs,
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_timezone(self):
        """Broadcast timezone setting change to all connected clients."""
        # Convert IANA to POSIX if possible
        tz_value = self.state.timezone
        if is_cache_initialized():
            posix_tz = get_posix_tz_string(self.state.timezone)
            if posix_tz:
                tz_value = posix_tz
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_SET_TIMEZONE,
                    FIELD_TZ: tz_value,
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_notification_settings(self):
        """Broadcast notification settings change to all connected clients."""
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_SET_NOTIFICATIONS,
                    FIELD_NOTIFICATIONS: self.state.get_notifications(),
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_power(self, enabled: bool):
        """Broadcast power setting change to all connected clients."""
        cmd = CMD_POWER_ON if enabled else CMD_POWER_OFF
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: cmd,
                    FIELD_POWER: "1" if enabled else "0",
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_auto(self, enabled: bool):
        """Broadcast auto/timers setting change to all connected clients."""
        cmd = CMD_ENABLE_AUTO if enabled else CMD_DISABLE_AUTO
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: cmd,
                    FIELD_AUTO: "1" if enabled else "0",
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_inside_sensor(self, enabled: bool):
        """Broadcast inside sensor enable/disable to all connected clients."""
        cmd = CMD_ENABLE_INSIDE if enabled else CMD_DISABLE_INSIDE
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: cmd,
                    FIELD_INSIDE: "1" if enabled else "0",
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )
        self.engine.notify_sensors_changed()

    def broadcast_outside_sensor(self, enabled: bool):
        """Broadcast outside sensor enable/disable to all connected clients."""
        cmd = CMD_ENABLE_OUTSIDE if enabled else CMD_DISABLE_OUTSIDE
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: cmd,
                    FIELD_OUTSIDE: "1" if enabled else "0",
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )
        self.engine.notify_sensors_changed()

    def broadcast_hardware_info(self):
        """Broadcast hardware/firmware info to all connected clients."""
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_GET_HW_INFO,
                    FIELD_FWINFO: {
                        FIELD_FW_MAJOR: self.state.fw_major,
                        FIELD_FW_MINOR: self.state.fw_minor,
                        FIELD_FW_PATCH: self.state.fw_patch,
                        FIELD_HW_VERSION: self.state.hw_ver,
                        FIELD_HW_REVISION: self.state.hw_rev,
                    },
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_stats(self):
        """Broadcast door open statistics to all connected clients."""
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_GET_DOOR_OPEN_STATS,
                    FIELD_TOTAL_OPEN_CYCLES: self.state.total_open_cycles,
                    FIELD_TOTAL_AUTO_RETRACTS: self.state.total_auto_retracts,
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_schedules(self):
        """Broadcast schedule list to all connected clients."""
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_GET_SCHEDULE_LIST,
                    FIELD_SCHEDULES: self.state.get_schedule_list(),
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_schedule(self, schedule: Schedule):
        """Broadcast a single schedule add/update to all connected clients."""
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_SET_SCHEDULE,
                    FIELD_SCHEDULE: schedule.to_dict(),
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_schedule_delete(self, index: int):
        """Broadcast a schedule deletion to all connected clients."""
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_DELETE_SCHEDULE,
                    FIELD_INDEX: index,
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

    def broadcast_notifications(self):
        """Broadcast notification settings to all connected clients."""
        for protocol in self.protocols:
            protocol._send(
                {
                    FIELD_CMD: CMD_GET_NOTIFICATIONS,
                    FIELD_NOTIFICATIONS: self.state.get_notifications(),
                    FIELD_SUCCESS: SUCCESS_TRUE,
                    FIELD_DIRECTION: DOOR_TO_PHONE,
                }
            )

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

        Works identically with and without connected clients - both paths
        drive the shared door-motion engine.

        Args:
            sensor: "inside" or "outside"
        """
        self.engine.trigger_sensor(sensor)

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
        self.engine.simulate_obstruction()

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
        self.engine.activate_sensor(sensor, duration)

    def set_pet_in_doorway(self, present: bool = True):
        """Simulate pet presence in doorway (keeps door open longer).

        This is an alias for activate_sensor("inside", 0) for backwards compatibility.
        """
        if present:
            self.state.inside_sensor_active = True
            self.state.outside_sensor_active = False
        else:
            self.state.inside_sensor_active = False
        self.engine.notify_sensors_changed()
        logger.info("Simulator: Pet %s doorway", "in" if present else "left")

    # =========================================================================
    # Door Control
    # =========================================================================

    async def open_door(self, hold: bool = False):
        """Open the door (as if triggered by sensor or schedule).

        Works with or without connected clients.
        """
        self.engine.open(hold=hold)

    async def close_door(self):
        """Close the door.

        Works with or without connected clients.
        """
        self.engine.close()

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
        logger.info("Simulator: AC %s", "connected" if present else "disconnected")
        self._broadcast_battery_status()

    def set_battery_present(self, present: bool):
        """Set battery presence state and notify clients.

        Args:
            present: True if battery is installed, False if removed.
        """
        if self.state.battery_present == present:
            return

        self.state.battery_present = present
        logger.info("Simulator: Battery %s", "installed" if present else "removed")
        self._broadcast_battery_status()

    def set_charge_rate(self, rate: float):
        """Set battery charge rate (percent per minute).

        Args:
            rate: Charge rate in percent per minute. Set to 0 to disable charging.
        """
        self.state.battery_config.charge_rate = max(0.0, rate)
        logger.info("Simulator: Charge rate set to %s%%/min", rate)

    def set_discharge_rate(self, rate: float):
        """Set battery discharge rate (percent per minute).

        Args:
            rate: Discharge rate in percent per minute. Set to 0 to disable discharging.
        """
        self.state.battery_config.discharge_rate = max(0.0, rate)
        logger.info("Simulator: Discharge rate set to %s%%/min", rate)

    def set_power(self, enabled: bool):
        """Set power state."""
        self.state.power = enabled
        logger.info("Simulator: Power %s", "ON" if enabled else "OFF")

    # =========================================================================
    # Schedule Management
    # =========================================================================

    def add_schedule(self, schedule: Schedule):
        """Add or update a schedule."""
        self.state.schedules[schedule.index] = schedule
        logger.info("Simulator: Added schedule %s", schedule.index)
        self.broadcast_schedule(schedule)

    def remove_schedule(self, index: int):
        """Remove a schedule by index."""
        if index in self.state.schedules:
            del self.state.schedules[index]
            logger.info("Simulator: Removed schedule %s", index)
            self.broadcast_schedule_delete(index)
