# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Integration tests for PowerPetDoorClient with DoorSimulator.

These tests verify end-to-end communication between the client and simulator,
ensuring commands are handled correctly and callbacks are triggered appropriately.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from powerpetdoor import PowerPetDoorClient
from powerpetdoor.const import (
    CMD_CLOSE,
    CMD_DISABLE_INSIDE,
    CMD_ENABLE_INSIDE,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HOLD_TIME,
    CMD_GET_HW_INFO,
    CMD_GET_POWER,
    CMD_GET_SENSORS,
    CMD_GET_SETTINGS,
    CMD_OPEN,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    COMMAND,
    CONFIG,
    DOOR_STATE_CLOSED,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_POWER,
    NOTIFY_LOW_BATTERY,
    NOTIFY_SENSOR_INDOOR,
    SENSOR_STATE_ON,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
    Schedule,
)

# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def fast_timing():
    """Create fast timing config for integration tests."""
    return DoorTimingConfig(
        rise_time=0.1,
        default_hold_time=1,
        slowing_time=0.05,
        closing_top_time=0.05,
        closing_mid_time=0.05,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(fast_timing):
    """Create and start a simulator."""
    state = DoorSimulatorState(timing=fast_timing, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def client(simulator) -> PowerPetDoorClient:
    """Create a client connected to the simulator."""
    port = simulator.server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()

    client = PowerPetDoorClient(
        host="127.0.0.1",
        port=port,
        keepalive=0,  # Disable keepalive for tests
        timeout=5.0,
        reconnect=1.0,
        loop=loop,
    )

    # Connect without blocking
    await client.connect()

    # Wait for connection to be established on both sides (yield-driven,
    # no wall-clock sleeps)
    async with asyncio.timeout(5):
        while not (client.available and simulator.protocols):
            await asyncio.sleep(0)

    assert client.available, "Client failed to connect to simulator"
    assert len(simulator.protocols) > 0, "Simulator did not register the connection"

    yield client

    # Cleanup via the public shutdown API (stops reconnects and disconnects)
    client.shutdown()


class CallbackTracker:
    """Helper to track callback invocations."""

    def __init__(self):
        self.calls: list[tuple[str, Any]] = []
        self.events: dict[str, asyncio.Event] = {}

    def make_callback(self, name: str):
        """Create a callback that records invocations."""
        event = asyncio.Event()
        self.events[name] = event

        def callback(*args):
            self.calls.append((name, args))
            event.set()

        return callback

    async def wait_for(self, name: str, timeout: float = 2.0) -> bool:
        """Wait for a callback to be invoked."""
        if name not in self.events:
            return False
        try:
            await asyncio.wait_for(self.events[name].wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def get_calls(self, name: str) -> list[Any]:
        """Get all calls for a specific callback."""
        return [args for n, args in self.calls if n == name]

    def clear(self):
        """Clear all recorded calls."""
        self.calls.clear()
        for event in self.events.values():
            event.clear()


@pytest.fixture
def tracker():
    """Create a callback tracker."""
    return CallbackTracker()


# ============================================================================
# Connection Tests
# ============================================================================


class TestClientConnection:
    """Test client connection to simulator."""

    @pytest.mark.asyncio
    async def test_client_connects_to_simulator(self, client, simulator):
        """Client should successfully connect to the simulator."""
        assert client.available
        assert len(simulator.protocols) == 1

    @pytest.mark.asyncio
    async def test_client_host_port(self, client, simulator):
        """Client should report correct host and port."""
        port = simulator.server.sockets[0].getsockname()[1]
        assert client.host == "127.0.0.1"
        assert client.port == port


# ============================================================================
# Query Command Tests
# ============================================================================


class TestQueryCommands:
    """Test query commands from client to simulator."""

    @pytest.mark.asyncio
    async def test_get_door_status(self, client, simulator):
        """GET_DOOR_STATUS should return current door status."""
        future = client.send_message(CONFIG, CMD_GET_DOOR_STATUS, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert result == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_get_settings(self, client, simulator):
        """GET_SETTINGS should return all settings."""
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert isinstance(result, dict)
        assert FIELD_POWER in result
        assert FIELD_INSIDE in result
        assert FIELD_OUTSIDE in result

    @pytest.mark.asyncio
    async def test_get_power(self, client, simulator):
        """GET_POWER should return power state."""
        future = client.send_message(CONFIG, CMD_GET_POWER, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert result is True  # Default power is on

    @pytest.mark.asyncio
    async def test_get_sensors(self, client, simulator):
        """GET_SENSORS should return sensor states."""
        future = client.send_message(CONFIG, CMD_GET_SENSORS, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert FIELD_INSIDE in result
        assert FIELD_OUTSIDE in result

    @pytest.mark.asyncio
    async def test_get_hold_time(self, client, simulator):
        """GET_HOLD_TIME should return hold time in centiseconds."""
        future = client.send_message(CONFIG, CMD_GET_HOLD_TIME, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        # Protocol returns centiseconds, state stores seconds
        assert result == int(simulator.state.hold_time * 100)

    @pytest.mark.asyncio
    async def test_get_battery(self, client, simulator):
        """GET_DOOR_BATTERY should return battery info."""
        future = client.send_message(CONFIG, CMD_GET_DOOR_BATTERY, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert "batteryPercent" in result
        assert "batteryPresent" in result
        assert "acPresent" in result

    @pytest.mark.asyncio
    async def test_get_hw_info(self, client, simulator):
        """GET_HW_INFO should return hardware info."""
        future = client.send_message(CONFIG, CMD_GET_HW_INFO, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert isinstance(result, dict)


# ============================================================================
# Control Command Tests
# ============================================================================


class TestControlCommands:
    """Test control commands from client to simulator."""

    @pytest.mark.asyncio
    async def test_open_door(self, client, simulator, tracker):
        """OPEN command should open the door."""
        callback = tracker.make_callback("door_status")
        client.add_listener("test", door_status_update=callback)

        # Send open command
        client.send_message(COMMAND, CMD_OPEN)

        # Wait for door to start opening
        await tracker.wait_for("door_status", timeout=2.0)

        # Should have received status update
        calls = tracker.get_calls("door_status")
        assert len(calls) > 0

        # Door should be in an open state
        statuses = [c[0] for c in calls]
        assert any(
            s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP) for s in statuses
        )

    @pytest.mark.asyncio
    async def test_close_door(self, client, simulator, tracker):
        """CLOSE command should close the door."""
        # First open the door
        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        callback = tracker.make_callback("door_status")
        client.add_listener("test", door_status_update=callback)

        # Send close command
        client.send_message(COMMAND, CMD_CLOSE)

        # Wait for close to complete
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_power_off(self, client, simulator):
        """POWER_OFF should disable power."""
        future = client.send_message(COMMAND, CMD_POWER_OFF, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert result is False
        assert simulator.state.power is False

    @pytest.mark.asyncio
    async def test_power_on(self, client, simulator):
        """POWER_ON should enable power."""
        simulator.state.power = False

        future = client.send_message(COMMAND, CMD_POWER_ON, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert result is True
        assert simulator.state.power is True

    @pytest.mark.asyncio
    async def test_disable_inside_sensor(self, client, simulator):
        """DISABLE_INSIDE should disable inside sensor."""
        future = client.send_message(COMMAND, CMD_DISABLE_INSIDE, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert FIELD_INSIDE in result
        assert result[FIELD_INSIDE] is False
        assert simulator.state.inside is False

    @pytest.mark.asyncio
    async def test_enable_inside_sensor(self, client, simulator):
        """ENABLE_INSIDE should enable inside sensor."""
        simulator.state.inside = False

        future = client.send_message(COMMAND, CMD_ENABLE_INSIDE, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert FIELD_INSIDE in result
        assert result[FIELD_INSIDE] is True
        assert simulator.state.inside is True

    @pytest.mark.asyncio
    async def test_set_hold_time(self, client, simulator):
        """SET_HOLD_TIME should update hold time (centiseconds in protocol)."""
        future = client.send_message(
            CONFIG,
            CMD_SET_HOLD_TIME,
            notify=True,
            holdTime=1500,  # 15 seconds
        )
        result = await asyncio.wait_for(future, timeout=2.0)

        # Protocol uses centiseconds, state stores seconds
        assert result == 1500
        assert simulator.state.hold_time == 15.0


# ============================================================================
# Callback/Listener Tests
# ============================================================================


class TestClientCallbacks:
    """Test client callback system with simulator."""

    @pytest.mark.asyncio
    async def test_door_status_callback(self, client, simulator, tracker):
        """Door status changes should trigger callback."""
        callback = tracker.make_callback("door_status")
        client.add_listener("test", door_status_update=callback)

        # Trigger door opening via simulator
        simulator.trigger_sensor("inside")

        # Wait for callback
        await tracker.wait_for("door_status", timeout=2.0)

        calls = tracker.get_calls("door_status")
        assert len(calls) > 0

    @pytest.mark.asyncio
    async def test_sensor_callback(self, client, simulator, tracker):
        """Sensor state changes should trigger callback."""
        callback = tracker.make_callback("sensor")
        client.add_listener("test", sensor_update={FIELD_INSIDE: callback})

        # Change sensor state
        client.send_message(COMMAND, CMD_DISABLE_INSIDE)

        await tracker.wait_for("sensor", timeout=2.0)

        calls = tracker.get_calls("sensor")
        assert len(calls) > 0

    @pytest.mark.asyncio
    async def test_sensor_callback_receives_field_and_value(self, client, simulator, tracker):
        """Sensor callback should receive both field_name and value arguments."""
        callback = tracker.make_callback("sensor")
        client.add_listener("test", sensor_update={FIELD_INSIDE: callback})

        # Change sensor state
        client.send_message(COMMAND, CMD_DISABLE_INSIDE)

        await tracker.wait_for("sensor", timeout=2.0)

        calls = tracker.get_calls("sensor")
        assert len(calls) > 0
        # Verify callback received (field_name, value) tuple
        field_name, value = calls[0]
        assert field_name == FIELD_INSIDE
        assert value is False

    @pytest.mark.asyncio
    async def test_wildcard_sensor_listener(self, client, simulator, tracker):
        """Wildcard '*' sensor listener should receive callbacks for all sensor fields."""
        callback = tracker.make_callback("sensor")
        client.add_listener("test", sensor_update={"*": callback})

        # Change inside sensor state
        client.send_message(COMMAND, CMD_DISABLE_INSIDE)

        await tracker.wait_for("sensor", timeout=2.0)

        calls = tracker.get_calls("sensor")
        assert len(calls) > 0
        # Verify callback received (field_name, value) tuple
        field_name, value = calls[0]
        assert field_name == FIELD_INSIDE
        assert value is False

    @pytest.mark.asyncio
    async def test_wildcard_sensor_listener_power(self, client, simulator, tracker):
        """Wildcard sensor listener should receive power state changes."""
        callback = tracker.make_callback("sensor")
        client.add_listener("test", sensor_update={"*": callback})

        # Change power state
        client.send_message(COMMAND, CMD_POWER_OFF)

        await tracker.wait_for("sensor", timeout=2.0)

        calls = tracker.get_calls("sensor")
        assert len(calls) > 0
        # Verify callback received (field_name, value) tuple
        field_name, value = calls[0]
        assert field_name == FIELD_POWER
        assert value is False

    @pytest.mark.asyncio
    async def test_simulator_broadcast_triggers_wildcard_listener(self, client, simulator, tracker):
        """Simulator-initiated broadcasts should trigger wildcard sensor listeners.

        This tests the scenario where state changes are made from the simulator
        side (e.g., via CLI commands) rather than client-initiated commands.
        """
        callback = tracker.make_callback("sensor")
        client.add_listener("test", sensor_update={"*": callback})

        # Change power state from simulator side (broadcast)
        simulator.broadcast_power(False)

        await tracker.wait_for("sensor", timeout=2.0)

        calls = tracker.get_calls("sensor")
        assert len(calls) > 0
        # Verify callback received (field_name, value) tuple
        field_name, value = calls[0]
        assert field_name == FIELD_POWER
        assert value is False

    @pytest.mark.asyncio
    async def test_simulator_broadcast_inside_sensor(self, client, simulator, tracker):
        """Simulator broadcast for inside sensor should trigger listener."""
        callback = tracker.make_callback("sensor")
        client.add_listener("test", sensor_update={"*": callback})

        # Broadcast inside sensor change from simulator
        simulator.broadcast_inside_sensor(False)

        await tracker.wait_for("sensor", timeout=2.0)

        calls = tracker.get_calls("sensor")
        assert len(calls) > 0
        field_name, value = calls[0]
        assert field_name == FIELD_INSIDE
        assert value is False

    @pytest.mark.asyncio
    async def test_multiple_listeners(self, client, simulator, tracker):
        """Multiple listeners should all receive callbacks."""
        callback1 = tracker.make_callback("listener1")
        callback2 = tracker.make_callback("listener2")

        client.add_listener("test1", door_status_update=callback1)
        client.add_listener("test2", door_status_update=callback2)

        # Trigger status update
        simulator.trigger_sensor("inside")

        await tracker.wait_for("listener1", timeout=2.0)
        await tracker.wait_for("listener2", timeout=2.0)

        assert len(tracker.get_calls("listener1")) > 0
        assert len(tracker.get_calls("listener2")) > 0


# ============================================================================
# Settings Round-Trip Tests (client wire format -> simulator state)
# ============================================================================


class TestSettingsRoundTrips:
    """The simulator honors the exact wire formats the client/door send."""

    @pytest.mark.asyncio
    async def test_door_set_notifications_round_trip(self, simulator):
        """door.set_notifications (top-level "1"/"0" fields) updates the simulator."""
        from powerpetdoor import PowerPetDoor

        port = simulator.server.sockets[0].getsockname()[1]
        door = PowerPetDoor(
            host="127.0.0.1",
            port=port,
            keepalive=0,
            timeout=5.0,
            reconnect=1.0,
            loop=asyncio.get_running_loop(),
        )
        await door.connect()
        try:
            assert simulator.state.sensor_on_indoor is False
            assert simulator.state.low_battery is True

            await door.set_notifications(inside_on=True, low_battery=False)

            assert simulator.state.sensor_on_indoor is True
            assert simulator.state.low_battery is False
        finally:
            await door.disconnect()

    @pytest.mark.asyncio
    async def test_delete_schedule_response_echoes_index(self, client, simulator):
        """DELETE_SCHEDULE responses echo the deleted index to the client."""
        from powerpetdoor.const import CMD_DELETE_SCHEDULE, FIELD_INDEX
        from powerpetdoor.simulator import Schedule as SimSchedule

        simulator.state.schedules[2] = SimSchedule(index=2, inside=True)

        future = client.send_message(CONFIG, CMD_DELETE_SCHEDULE, notify=True, **{FIELD_INDEX: 2})
        result = await asyncio.wait_for(future, timeout=2.0)

        # The client resolves the future with the echoed index
        assert result == 2
        assert 2 not in simulator.state.schedules


# ============================================================================
# Notification Event Round-Trip Tests (D2: bare envelope)
# ============================================================================


class TestNotificationEvents:
    """Simulator emits bare notification envelopes; client dispatches them."""

    @pytest.mark.asyncio
    async def test_sensor_notification_round_trip(self, client, simulator, tracker):
        """Simulator sensor event -> client notification_event listener."""
        callback = tracker.make_callback("notify")
        client.add_listener("test", notification_event=callback)

        # Enable the notification and trigger the (closed) door's sensor
        simulator.state.sensor_on_indoor = True
        simulator.trigger_sensor("inside")

        assert await tracker.wait_for("notify", timeout=2.0)
        calls = tracker.get_calls("notify")
        assert calls[0] == (NOTIFY_SENSOR_INDOOR, SENSOR_STATE_ON)

    @pytest.mark.asyncio
    async def test_low_battery_notification_round_trip(self, client, simulator, tracker):
        """Simulator low-battery event -> client notification_event listener."""
        callback = tracker.make_callback("notify")
        client.add_listener("test", notification_event=callback)

        simulator.state.low_battery = True
        simulator.set_battery(15)  # crosses the 20% threshold

        assert await tracker.wait_for("notify", timeout=2.0)
        calls = tracker.get_calls("notify")
        # LOW_BATTERY carries no sensorState
        assert calls[0] == (NOTIFY_LOW_BATTERY, None)

    @pytest.mark.asyncio
    async def test_disabled_sensor_notification_not_emitted(self, client, simulator, tracker):
        """No notification event reaches the client when the setting is off."""
        callback = tracker.make_callback("notify")
        client.add_listener("test", notification_event=callback)

        # sensor_on_indoor defaults to disabled
        simulator.trigger_sensor("inside")

        # Sentinel round-trip: the simulator writes messages in order, so by
        # the time this reply arrives, any (unexpected) earlier notification
        # would already have been dispatched to the listener.
        future = client.send_message(CONFIG, CMD_GET_POWER, notify=True)
        await asyncio.wait_for(future, timeout=2.0)
        assert tracker.get_calls("notify") == []


# ============================================================================
# Full Door Cycle Tests
# ============================================================================


class TestDoorCycles:
    """Test full door operation cycles."""

    @pytest.mark.asyncio
    async def test_sensor_trigger_full_cycle(self, client, simulator, tracker):
        """Sensor trigger should cause full open/close cycle."""
        callback = tracker.make_callback("door_status")
        client.add_listener("test", door_status_update=callback)

        # Record initial cycle count
        initial_cycles = simulator.state.total_open_cycles

        # Trigger sensor
        simulator.trigger_sensor("inside")

        # Wait for cycle to complete: first the open, then the close
        await simulator.wait_for_status(DOOR_STATE_RISING, timeout=2.0)
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=10.0)

        # Cycle should have completed
        assert simulator.state.total_open_cycles == initial_cycles + 1
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        # Should have received multiple status updates
        calls = tracker.get_calls("door_status")
        assert len(calls) >= 2  # At least RISING and HOLDING

    @pytest.mark.asyncio
    async def test_client_initiated_open_close(self, client, simulator):
        """Client-initiated open/close should work correctly."""
        # Open door (a plain OPEN parks in HOLDING until the hold expires)
        client.send_message(COMMAND, CMD_OPEN)
        result = await simulator.wait_for_status(DOOR_STATE_HOLDING, timeout=5.0)
        assert result == DOOR_STATE_HOLDING

        # Close door
        client.send_message(COMMAND, CMD_CLOSE)
        result = await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
        assert result == DOOR_STATE_CLOSED


# ============================================================================
# Error Handling Tests
# ============================================================================


class TestErrorHandling:
    """Test error handling between client and simulator."""

    @pytest.mark.asyncio
    async def test_command_blocked_when_power_off(self, client, simulator):
        """Door commands should be blocked when power is off."""
        simulator.state.power = False

        # Deterministic: wait for the simulator to receive the OPEN command,
        # then for its handler task to finish - no wall-clock sleeps.
        received = asyncio.Event()
        protocol = simulator.protocols[0]
        protocol.on_command = lambda cmd, msg: cmd == CMD_OPEN and received.set()

        client.send_message(COMMAND, CMD_OPEN)
        await asyncio.wait_for(received.wait(), timeout=2.0)
        await protocol.drain()

        # The command was rejected: the door never moved
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_sensor_blocked_when_disabled(self, client, simulator):
        """Sensor trigger should be blocked when sensor is disabled."""
        simulator.state.inside = False

        # A blocked trigger is ignored synchronously - the door never moves
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED


# ============================================================================
# Schedule Callback Tests
# ============================================================================


class TestScheduleCallbacks:
    """Test schedule callback system with simulator."""

    @pytest.mark.asyncio
    async def test_schedule_update_callback(self, client, simulator, tracker):
        """Schedule add should trigger schedule_update callback."""
        callback = tracker.make_callback("schedule")
        client.add_listener("test", schedule_update=callback)

        # Add a schedule via simulator
        schedule = Schedule(
            index=0,
            enabled=True,
            inside=True,
            outside=False,
            start_hour=6,
            start_min=0,
            end_hour=22,
            end_min=0,
        )
        simulator.add_schedule(schedule)

        # Wait for callback
        await tracker.wait_for("schedule", timeout=2.0)

        calls = tracker.get_calls("schedule")
        assert len(calls) > 0
        # Callback receives schedule dict (values are strings from protocol)
        schedule_data = calls[0][0]
        assert schedule_data["index"] == 0
        assert schedule_data["enabled"] == "1"

    @pytest.mark.asyncio
    async def test_schedule_delete_callback(self, client, simulator, tracker):
        """Schedule delete should trigger schedule_delete callback."""
        # First add a schedule
        schedule = Schedule(index=0, enabled=True, inside=True, outside=False)
        simulator.state.schedules[0] = schedule

        callback = tracker.make_callback("schedule_delete")
        client.add_listener("test", schedule_delete=callback)

        # Delete the schedule via simulator
        simulator.remove_schedule(0)

        # Wait for callback
        await tracker.wait_for("schedule_delete", timeout=2.0)

        calls = tracker.get_calls("schedule_delete")
        assert len(calls) > 0
        # Callback receives schedule index
        deleted_index = calls[0][0]
        assert deleted_index == 0

    @pytest.mark.asyncio
    async def test_schedule_modify_triggers_update(self, client, simulator, tracker):
        """Modifying a schedule should trigger schedule_update callback."""
        # First add a schedule directly to state (no broadcast)
        schedule = Schedule(index=0, enabled=True, inside=True, outside=False)
        simulator.state.schedules[0] = schedule

        callback = tracker.make_callback("schedule")
        client.add_listener("test", schedule_update=callback)

        # Modify the schedule - this should broadcast
        schedule.enabled = False
        simulator.broadcast_schedule(schedule)

        # Wait for callback
        await tracker.wait_for("schedule", timeout=2.0)

        calls = tracker.get_calls("schedule")
        assert len(calls) > 0
        # Callback receives schedule dict (values are strings from protocol)
        schedule_data = calls[0][0]
        assert schedule_data["index"] == 0
        assert schedule_data["enabled"] == "0"
