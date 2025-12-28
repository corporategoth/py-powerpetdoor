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
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.const import (
    CONFIG,
    CMD_GET_DOOR_STATUS,
    CMD_GET_SETTINGS,
    CMD_GET_POWER,
    CMD_GET_SENSORS,
    CMD_GET_AUTO,
    CMD_GET_AUTORETRACT,
    CMD_GET_HOLD_TIME,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_HW_INFO,
    CMD_OPEN,
    CMD_CLOSE,
    CMD_POWER_ON,
    CMD_POWER_OFF,
    CMD_ENABLE_INSIDE,
    CMD_DISABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_SET_HOLD_TIME,
    COMMAND,
    DOOR_STATE_CLOSED,
    DOOR_STATE_RISING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_POWER,
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

    # Wait for connection to be established on both sides
    for _ in range(50):  # 5 seconds max
        if client.available and len(simulator.protocols) > 0:
            break
        await asyncio.sleep(0.1)

    assert client.available, "Client failed to connect to simulator"
    assert len(simulator.protocols) > 0, "Simulator did not register the connection"

    yield client

    # Cleanup
    client._shutdown = True
    client.disconnect()


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
        except asyncio.TimeoutError:
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
        """GET_HOLD_TIME should return hold time."""
        future = client.send_message(CONFIG, CMD_GET_HOLD_TIME, notify=True)
        result = await asyncio.wait_for(future, timeout=2.0)

        assert result == simulator.state.hold_time

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
        assert any(s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP)
                   for s in statuses)

    @pytest.mark.asyncio
    async def test_close_door(self, client, simulator, tracker):
        """CLOSE command should close the door."""
        # First open the door
        await simulator.open_door(hold=True)
        await asyncio.sleep(0.2)

        callback = tracker.make_callback("door_status")
        client.add_listener("test", door_status_update=callback)

        # Send close command
        client.send_message(COMMAND, CMD_CLOSE)

        # Wait for close to complete
        for _ in range(50):
            if simulator.state.door_status == DOOR_STATE_CLOSED:
                break
            await asyncio.sleep(0.1)

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
        """SET_HOLD_TIME should update hold time."""
        future = client.send_message(
            CONFIG, CMD_SET_HOLD_TIME, notify=True, holdTime=15
        )
        result = await asyncio.wait_for(future, timeout=2.0)

        assert result == 15
        assert simulator.state.hold_time == 15


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

        # Wait for cycle to complete
        for _ in range(100):  # 10 seconds max
            if simulator.state.door_status == DOOR_STATE_CLOSED:
                if simulator.state.total_open_cycles > initial_cycles:
                    break
            await asyncio.sleep(0.1)

        # Cycle should have completed
        assert simulator.state.total_open_cycles == initial_cycles + 1
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        # Should have received multiple status updates
        calls = tracker.get_calls("door_status")
        assert len(calls) >= 2  # At least RISING and HOLDING

    @pytest.mark.asyncio
    async def test_client_initiated_open_close(self, client, simulator):
        """Client-initiated open/close should work correctly."""
        # Open door
        client.send_message(COMMAND, CMD_OPEN)

        # Wait for door to open
        for _ in range(50):
            if simulator.state.door_status in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP):
                break
            await asyncio.sleep(0.1)

        assert simulator.state.door_status in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP)

        # Close door
        client.send_message(COMMAND, CMD_CLOSE)

        # Wait for door to close
        for _ in range(50):
            if simulator.state.door_status == DOOR_STATE_CLOSED:
                break
            await asyncio.sleep(0.1)

        assert simulator.state.door_status == DOOR_STATE_CLOSED


# ============================================================================
# Error Handling Tests
# ============================================================================

class TestErrorHandling:
    """Test error handling between client and simulator."""

    @pytest.mark.asyncio
    async def test_command_blocked_when_power_off(self, client, simulator):
        """Door commands should be blocked when power is off."""
        simulator.state.power = False

        # Try to open door
        client.send_message(COMMAND, CMD_OPEN)
        await asyncio.sleep(0.5)

        # Door should still be closed
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_sensor_blocked_when_disabled(self, client, simulator):
        """Sensor trigger should be blocked when sensor is disabled."""
        simulator.state.inside = False

        # Try to trigger inside sensor
        simulator.trigger_sensor("inside")
        await asyncio.sleep(0.5)

        # Door should still be closed
        assert simulator.state.door_status == DOOR_STATE_CLOSED
