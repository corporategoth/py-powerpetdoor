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
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType

from ..const import (
    CMD_DELETE_SCHEDULE,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SETTINGS,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    DOOR_STATES_CLOSED,
    DOOR_STATES_FULLY_OPEN,
    DOOR_TO_PHONE,
    FIELD_AC_PRESENT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD,
    FIELD_DIRECTION,
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
    FIELD_FWINFO,
    FIELD_HW_REVISION,
    FIELD_HW_VERSION,
    FIELD_INDEX,
    FIELD_NOTIFICATIONS,
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SETTINGS,
    FIELD_SUCCESS,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    SUCCESS_TRUE,
)
from ..i18n import t
from ..tz_utils import async_init_timezone_cache
from .engine import DoorMotionEngine
from .notifications import (
    NOTIFICATION_SETTINGS,
    NOTIFY_LOW_BATTERY,
    sensor_notification,
)
from .protocol import DoorSimulatorProtocol
from .state import DoorSimulatorState, Schedule
from .values import VALUES
from .wire_values import WIRE_VALUES, notifications_payload, settings_payload

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
        self._notification_listeners: list[Callable[[str], None]] = []
        self.protocols: list[DoorSimulatorProtocol] = []
        self.engine = DoorMotionEngine(
            self.state,
            broadcast_status=self._broadcast_door_status,
            notify_sensor=self._notify_sensor_reached,
        )
        self._battery_task: asyncio.Task | None = None
        # Fractional battery percent accumulated between integer steps, so
        # rates below 1%/interval still charge/discharge correctly.
        self._battery_carry = 0.0
        self._running = False
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

    async def start(self):
        """Start the simulator server.

        Warms the timezone cache first: without it the door answers
        ``GET_SETTINGS``/``GET_TIMEZONE`` with a raw IANA name, where a
        real door always answers the POSIX form.
        """
        await async_init_timezone_cache()
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
                simulator=self,
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

        logger.info(
            t("simulator.server.door_simulator_listening", "Door simulator listening on %s:%s"),
            self.host,
            self.port,
        )

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
            logger.info(t("simulator.server.door_simulator_stopped", "Door simulator stopped"))

    async def reset_state(self, document: dict | None = None) -> None:
        """Return the door to a known state, and tell every client.

        Stops whatever the door was doing, parks it closed, clears the
        sensors and any obstruction, applies ``document`` over fresh
        defaults, then broadcasts everything. A reset that left connected
        clients believing the old world would be worse than no reset: the
        vendor app's stale-cache bug (see docs/protocol.md) is what that
        failure mode looks like from the outside.

        Statistics reset too. They are state, not configuration, and a
        reset that left ``totalOpenCycles`` behind would make test
        isolation quietly wrong.

        Args:
            document: A state document to apply over the defaults. ``None``
                and ``{}`` both mean "the defaults".
        """
        from .state_io import apply_document

        await self.engine.stop()

        fresh = DoorSimulatorState()
        apply_document(fresh, document or {})
        # Replace field by field: the engine, the protocols and this
        # object all hold the same state instance, and rebinding here
        # would leave them driving the old one.
        state = self.state
        for slot, value in vars(fresh).items():
            setattr(state, slot, value)

        self.engine.reset()
        self.broadcast_all()

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
                logger.exception(
                    t("simulator.server.error_battery_simulation", "Error in battery simulation")
                )

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
            t("simulator.server.battery", "Battery %s: %s%% -> %s%%"),
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
        """Raise the low-battery notification."""
        self.notify(NOTIFY_LOW_BATTERY)

    def add_notification_listener(self, callback: "Callable[[str], None]") -> "Callable[[], None]":
        """Subscribe to notifications. Returns an unsubscribe function.

        The callback receives the notification name - one of
        :data:`~powerpetdoor.simulator.notifications.NOTIFICATION_NAMES`.
        Exceptions are logged and isolated, so one bad listener cannot
        stop the door.
        """
        self._notification_listeners.append(callback)

        def unsubscribe() -> None:
            try:
                self._notification_listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def notify(self, name: str) -> bool:
        """Raise a notification, if its switch is on.

        **Nothing is sent to a connected client.** The door's notifications
        reach their owner through the vendor's service, not over TCP 3000;
        what the simulator offers instead is a record that one would have
        been raised - counted, logged, and delivered to listeners.

        Returns True if it was raised, False if its switch is off.
        """
        setting = NOTIFICATION_SETTINGS.get(name)
        if setting is None or not getattr(self.state, setting):
            return False

        self.state.notifications[name] = self.state.notifications.get(name, 0) + 1
        logger.info(
            t(
                "simulator.server.simulator_notification",
                "Simulator: Notification: %s",
            ),
            name,
        )
        for callback in list(self._notification_listeners):
            try:
                callback(name)
            except Exception:
                logger.exception(
                    t(
                        "simulator.server.simulator_notification_callback_failed",
                        "Simulator: notification callback failed",
                    )
                )
        return True

    def _notify_sensor_reached(self, sensor: str) -> None:
        """A pet reached ``sensor``; raise the matching notification.

        Which of the pair fires depends on whether that sensor was
        switched **on** at the time - `inside_off` is "a pet tried to get
        out and the sensor was off", which is the notification's point.
        """
        enabled = self.state.inside if sensor == "inside" else self.state.outside
        self.notify(sensor_notification(sensor, enabled))

    def send_to_clients(self, cmd: str, payload: dict) -> None:
        """Push one door-to-phone message to every connected client.

        The single output primitive. Every broadcast is this envelope
        around a different payload, and writing the envelope out per
        broadcast was eighteen chances to forget `dir` or `success`.
        """
        message = {
            FIELD_CMD: cmd,
            **payload,
            FIELD_SUCCESS: SUCCESS_TRUE,
            FIELD_DIRECTION: DOOR_TO_PHONE,
        }
        for protocol in self.protocols:
            protocol._send(message)

    def announce_value(self, name: str) -> None:
        """Tell connected clients the named value changed.

        The simulator decides *how* a change is announced, which is what
        keeps the value registry from having to know the wire exists: a
        value with a wire spelling goes out under its own command, and one
        without - the simulation's own knobs, a paired remote - is not a
        thing a real door announces at all.
        """
        if name in WIRE_VALUES:
            self.broadcast_value(name)

    def broadcast_value(self, name: str) -> None:
        """Announce a change to the named value.

        Reads :data:`~powerpetdoor.simulator.wire_values.WIRE_VALUES` for
        the command and payload, which is the same table the protocol
        answers its own commands from - so what a client is told
        spontaneously and what it is told on request cannot disagree.
        """
        wire = WIRE_VALUES[name]
        self.send_to_clients(
            wire.command_for(VALUES[name].get(self.state)), wire.payload(self.state)
        )

    def broadcast_settings(self):
        """Broadcast settings to all connected clients."""
        self.send_to_clients(CMD_GET_SETTINGS, {FIELD_SETTINGS: settings_payload(self.state)})

    def broadcast_notification_settings(self):
        """Broadcast notification settings change to all connected clients."""
        self.send_to_clients(
            CMD_SET_NOTIFICATIONS, {FIELD_NOTIFICATIONS: notifications_payload(self.state)}
        )

    def broadcast_hardware_info(self):
        """Broadcast hardware/firmware info to all connected clients."""
        self.send_to_clients(
            CMD_GET_HW_INFO,
            {
                FIELD_FWINFO: {
                    FIELD_FW_MAJOR: self.state.fw_major,
                    FIELD_FW_MINOR: self.state.fw_minor,
                    FIELD_FW_PATCH: self.state.fw_patch,
                    FIELD_HW_VERSION: self.state.hw_ver,
                    FIELD_HW_REVISION: self.state.hw_rev,
                }
            },
        )

    def broadcast_stats(self):
        """Broadcast door open statistics to all connected clients."""
        self.send_to_clients(
            CMD_GET_DOOR_OPEN_STATS,
            {
                FIELD_TOTAL_OPEN_CYCLES: self.state.total_open_cycles,
                FIELD_TOTAL_AUTO_RETRACTS: self.state.total_auto_retracts,
            },
        )

    def broadcast_schedules(self):
        """Broadcast schedule list to all connected clients."""
        self.send_to_clients(
            CMD_GET_SCHEDULE_LIST, {FIELD_SCHEDULES: self.state.get_schedule_list()}
        )

    def broadcast_schedule(self, schedule: Schedule):
        """Broadcast a single schedule add/update to all connected clients."""
        self.send_to_clients(CMD_SET_SCHEDULE, {FIELD_SCHEDULE: schedule.to_dict()})

    def broadcast_schedule_delete(self, index: int):
        """Broadcast a schedule deletion to all connected clients."""
        self.send_to_clients(CMD_DELETE_SCHEDULE, {FIELD_INDEX: index})

    def broadcast_notifications(self):
        """Broadcast notification settings to all connected clients."""
        self.send_to_clients(
            CMD_GET_NOTIFICATIONS, {FIELD_NOTIFICATIONS: notifications_payload(self.state)}
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

    def simulate_obstruction(self, duration: float | None = None):
        """Place (or clear) a physical obstruction in the doorway.

        Unlike a sensor, an obstruction does not stop a close from
        starting - the door travels into it. See
        :meth:`~powerpetdoor.simulator.engine.DoorMotionEngine.simulate_obstruction`.

        Args:
            duration: ``None`` toggles, ``0`` stays until cleared, a
                positive value clears itself after that many seconds.
        """
        self.engine.simulate_obstruction(duration)

    def clear_obstruction(self):
        """Remove the obstruction, freeing a door resting on it."""
        self.engine.clear_obstruction()

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

    def notify_settings_changed(self):
        """Re-run the sensor decision after a settings change.

        Power, the sensor enables, the safety lock, command lockout and the
        schedule all gate whether a held sensor may open the door. Changing
        one has to re-ask the question, or a pet shut out by a disabled
        sensor stays shut out after it is re-enabled.
        """
        self.engine.reevaluate_sensors()

    def hold_sensor(self, sensor: str, present: bool | None = None):
        """Hold a sensor active, release it, or flip it.

        A held sensor *is* pet presence - a collar sitting in range - so
        this is what ``inside on`` / ``outside toggle`` reach.
        """
        self.engine.hold_sensor(sensor, present)

    def set_pet_in_doorway(self, present: bool = True, sensor: str = "inside"):
        """Hold a sensor active, as a pet loitering in range would.

        Which sensor matters: an **outside** sensor is ignored while the
        safety lock is on, an inside one is not, so outside presence is the
        only way to exercise that interaction. Both are ignored entirely
        while command lockout is on, which is that setting's whole point.

        Args:
            present: Whether the pet is there.
            sensor: ``"inside"`` (the default) or ``"outside"``.
        """
        self.hold_sensor(sensor, present)
        logger.info(
            t("simulator.server.simulator_pet_doorway", "Simulator: Pet %s %s doorway"),
            "in" if present else "left",
            sensor,
        )

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

    async def toggle_door(self) -> str | None:
        """Open the door if it is closed, close it if it is open.

        The one place the toggle decision is made. The prompt, a script
        and a programmatic caller all reach it here, so "what does toggle
        do from here" has a single answer.

        Opening holds, so a toggled-open door stays open until it is
        toggled back. Mid-travel in either direction this does nothing:
        nothing but an obstruction is known to interrupt a real door in
        motion, so there is no honest behaviour to simulate. The wire
        protocol has no toggle at all - a client that wants one reads the
        status and picks.

        Returns:
            ``"open"`` or ``"close"`` for what it started, ``None`` if the
            door was in travel and it did nothing.
        """
        status = self.state.door_status
        if status in DOOR_STATES_CLOSED:
            await self.open_door(hold=True)
            return "open"
        if status in DOOR_STATES_FULLY_OPEN:
            await self.close_door()
            return "close"
        return None

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
        logger.info(
            t("simulator.server.simulator_ac", "Simulator: AC %s"),
            "connected" if present else "disconnected",
        )
        self._broadcast_battery_status()

    def set_battery_present(self, present: bool):
        """Set battery presence state and notify clients.

        Args:
            present: True if battery is installed, False if removed.
        """
        if self.state.battery_present == present:
            return

        self.state.battery_present = present
        logger.info(
            t("simulator.server.simulator_battery", "Simulator: Battery %s"),
            "installed" if present else "removed",
        )
        self._broadcast_battery_status()

    def set_charge_rate(self, rate: float):
        """Set battery charge rate (percent per minute).

        Args:
            rate: Charge rate in percent per minute. Set to 0 to disable charging.
        """
        self.state.battery_config.charge_rate = max(0.0, rate)
        logger.info(
            t(
                "simulator.server.simulator_charge_rate_set_min",
                "Simulator: Charge rate set to %s%%/min",
            ),
            rate,
        )

    def set_discharge_rate(self, rate: float):
        """Set battery discharge rate (percent per minute).

        Args:
            rate: Discharge rate in percent per minute. Set to 0 to disable discharging.
        """
        self.state.battery_config.discharge_rate = max(0.0, rate)
        logger.info(
            t(
                "simulator.server.simulator_discharge_rate_set_min",
                "Simulator: Discharge rate set to %s%%/min",
            ),
            rate,
        )

    def set_power(self, enabled: bool):
        """Set power state."""
        self.state.power = enabled
        logger.info(
            t("simulator.server.simulator_power", "Simulator: Power %s"), "ON" if enabled else "OFF"
        )

    # =========================================================================
    # Schedule Management
    # =========================================================================

    def add_schedule(self, schedule: Schedule, *, announce: bool = True):
        """Add or update a schedule.

        The one path every source takes - the prompt, a script, and
        `SET_SCHEDULE` off the wire - so a stored schedule is logged and
        announced the same way whoever wrote it.

        ``announce=False`` for the wire, which answers in its own response
        rather than also broadcasting to the client that asked.
        """
        self.state.schedules[schedule.index] = schedule
        logger.info(
            t("simulator.server.simulator_added_schedule", "Simulator: Added schedule %s"),
            schedule.index,
        )
        if announce:
            self.broadcast_schedule(schedule)

    def remove_schedule(self, index: int, *, announce: bool = True):
        """Remove a schedule by index. See :meth:`add_schedule`."""
        if index in self.state.schedules:
            del self.state.schedules[index]
            logger.info(
                t("simulator.server.simulator_removed_schedule", "Simulator: Removed schedule %s"),
                index,
            )
            if announce:
                self.broadcast_schedule_delete(index)

    def get_schedules(self) -> "Mapping[int, Schedule]":
        """Every stored schedule, by index.

        A read-only view, and the counterpart to :meth:`add_schedule` and
        :meth:`remove_schedule`. Reading the dict off the state works
        today and would keep working right up until schedules moved
        somewhere else, at which point every surface that reached past
        this would need finding.
        """
        return MappingProxyType(self.state.schedules)

    def get_schedule(self, index: int) -> "Schedule | None":
        """One schedule, or ``None`` if that slot is empty."""
        return self.state.schedules.get(index)

    def set_schedules(self, schedules: "Iterable[Schedule]", *, announce: bool = True):
        """Replace the whole table, one schedule at a time.

        The prompt's `schedule clear` is a wholesale operation, and used
        to reach past
        :meth:`add_schedule`/:meth:`remove_schedule` into the dict. Going
        through them means a wholesale replacement is logged and announced
        as the individual changes it actually is - a client watching the
        table sees every index that left and every index that arrived,
        rather than a silent swap.
        """
        incoming = {schedule.index: schedule for schedule in schedules}
        for index in [i for i in self.state.schedules if i not in incoming]:
            self.remove_schedule(index, announce=announce)
        for schedule in incoming.values():
            self.add_schedule(schedule, announce=announce)
