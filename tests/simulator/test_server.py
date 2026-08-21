# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator server module (server.py)."""

from __future__ import annotations

import asyncio
import json

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    FIELD_DOOR_STATUS,
    NOTIFY_LOW_BATTERY,
)
from powerpetdoor.simulator import (
    BatteryConfig,
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
    Schedule,
)

FULL_CYCLE_SEQUENCE = [
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSED,
]

# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def timing_config():
    """Create a fast timing config for tests."""
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(timing_config):
    """Create and start a simulator."""
    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


# ============================================================================
# DoorSimulator Server Tests
# ============================================================================


class TestDoorSimulator:
    """Tests for DoorSimulator server."""

    @pytest.mark.asyncio
    async def test_start_stop(self, timing_config):
        """Should start and stop cleanly."""
        state = DoorSimulatorState(timing=timing_config)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        assert sim.server is not None
        await sim.stop()

    @pytest.mark.asyncio
    async def test_listens_on_port(self, simulator):
        """Should listen on configured port."""
        port = simulator.server.sockets[0].getsockname()[1]
        assert port > 0

    @pytest.mark.asyncio
    async def test_open_door(self, simulator):
        """open_door with hold=True should reach and stay in KEEPUP."""
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        await simulator.open_door(hold=True)
        result = await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        assert result == DOOR_STATE_KEEPUP

    @pytest.mark.asyncio
    async def test_close_door(self, simulator):
        """close_door should fully close the door."""
        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        await simulator.close_door()
        result = await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)
        assert result == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_trigger_sensor_opens_door(self, simulator):
        """trigger_sensor should start opening the door immediately."""
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_RISING

    @pytest.mark.asyncio
    async def test_trigger_sensor_ignored_when_power_off(self, simulator):
        """trigger_sensor should be ignored when power is off."""
        simulator.set_power(False)
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_trigger_sensor_ignored_when_disabled(self, simulator):
        """trigger_sensor should be ignored when sensor is disabled."""
        simulator.state.inside = False
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_outside_sensor_ignored_with_safety_lock(self, simulator):
        """Outside sensor should be ignored when safety lock is enabled."""
        simulator.state.safety_lock = True
        simulator.trigger_sensor("outside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        # Inside sensor should still work
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_RISING

    def test_set_battery(self, simulator):
        """set_battery should update battery percent."""
        simulator.set_battery(50)
        assert simulator.state.battery_percent == 50

    def test_set_battery_clamps_values(self, simulator):
        """set_battery should clamp to 0-100."""
        simulator.set_battery(150)
        assert simulator.state.battery_percent == 100

        simulator.set_battery(-10)
        assert simulator.state.battery_percent == 0

    def test_set_power(self, simulator):
        """set_power should update power state."""
        simulator.set_power(False)
        assert simulator.state.power is False

        simulator.set_power(True)
        assert simulator.state.power is True

    def test_add_schedule(self, simulator):
        """add_schedule should add to state."""
        schedule = Schedule(index=5, enabled=True)
        simulator.add_schedule(schedule)
        assert 5 in simulator.state.schedules
        assert simulator.state.schedules[5] is schedule

    def test_remove_schedule(self, simulator):
        """remove_schedule should remove from state."""
        schedule = Schedule(index=3)
        simulator.add_schedule(schedule)
        assert 3 in simulator.state.schedules

        simulator.remove_schedule(3)
        assert 3 not in simulator.state.schedules

    def test_set_pet_in_doorway(self, simulator):
        """set_pet_in_doorway should update inside_sensor_active state."""
        simulator.set_pet_in_doorway(True)
        assert simulator.state.inside_sensor_active is True

        simulator.set_pet_in_doorway(False)
        assert simulator.state.inside_sensor_active is False


# ============================================================================
# Door Operation Sequence Tests
# ============================================================================


class TestDoorOperationSequences:
    """Tests for complete door operation sequences."""

    @pytest.mark.asyncio
    async def test_full_open_close_cycle(self, simulator):
        """A sensor trigger runs the exact full open/close sequence."""
        seen: list[str] = []
        unsubscribe = simulator.add_status_listener(seen.append)

        simulator.trigger_sensor("inside")
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
        unsubscribe()

        assert seen == FULL_CYCLE_SEQUENCE
        assert simulator.state.total_open_cycles == 1

    @pytest.mark.asyncio
    async def test_sensor_active_state(self, simulator):
        """Sensor active flags should be settable and affect is_sensor_blocking_close."""
        # Verify initial state
        assert simulator.state.inside_sensor_active is False
        assert simulator.state.outside_sensor_active is False
        assert simulator.state.is_sensor_blocking_close() is False

        # Set inside sensor active (with sensor enabled)
        simulator.state.inside_sensor_active = True
        assert simulator.state.inside_sensor_active is True
        assert simulator.state.is_sensor_blocking_close() is True

        # Clear it
        simulator.state.inside_sensor_active = False
        assert simulator.state.inside_sensor_active is False
        assert simulator.state.is_sensor_blocking_close() is False

    @pytest.mark.asyncio
    async def test_open_and_hold_keeps_door_open(self, simulator):
        """open_door with hold=True should keep door open indefinitely."""
        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        # KEEPUP is terminal: the sequence task has finished and the door stays
        await asyncio.gather(simulator.engine._task, return_exceptions=True)
        assert simulator.state.door_status == DOOR_STATE_KEEPUP

    @pytest.mark.asyncio
    async def test_wait_for_status_timeout(self, simulator):
        """wait_for_status raises TimeoutError when never reached."""
        with pytest.raises(TimeoutError):
            await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=0.05)


# ============================================================================
# Engine Path Parity Tests (M12: one engine, identical behavior)
# ============================================================================


class TestEnginePathParity:
    """Door behavior must be identical with and without a connected client."""

    async def _run_retract_cycle(self, simulator) -> list[str]:
        """Open-and-hold, close, obstruct during close, observe full sequence."""
        seen: list[str] = []
        unsubscribe = simulator.add_status_listener(seen.append)

        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        await simulator.close_door()
        # Pet enters the doorway while the door is closing
        simulator.trigger_sensor("inside")
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

        unsubscribe()
        return seen

    @pytest.mark.asyncio
    async def test_retract_cycle_identical_with_and_without_client(self, timing_config):
        """The full transition sequence matches exactly on both paths."""
        state = DoorSimulatorState(timing=timing_config, hold_time=0.1)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        try:
            # Path 1: no client connected
            no_client_seen = await self._run_retract_cycle(sim)
            assert sim.state.total_auto_retracts == 1

            # Path 2: a client is connected and watches the broadcasts
            port = sim.server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                with_client_seen = await self._run_retract_cycle(sim)
                assert sim.state.total_auto_retracts == 2

                assert with_client_seen == no_client_seen

                # The connected client observed the same status sequence
                # (read until the final CLOSED broadcast has arrived)
                text = ""
                broadcast_statuses: list[str] = []
                deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < deadline:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=1.0)
                    text += chunk.decode("ascii")
                    broadcast_statuses = [
                        msg[FIELD_DOOR_STATUS]
                        for msg in _parse_json_stream(text)
                        if FIELD_DOOR_STATUS in msg
                    ]
                    if len(broadcast_statuses) >= len(with_client_seen):
                        break
                assert broadcast_statuses == with_client_seen
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_hold_extension_identical_with_and_without_client(self, timing_config):
        """Sensor re-trigger extends the hold deadline on both paths."""
        state = DoorSimulatorState(timing=timing_config, hold_time=10.0)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        loop = asyncio.get_running_loop()
        try:
            deadlines = []
            for connect_client in (False, True):
                if connect_client:
                    port = sim.server.sockets[0].getsockname()[1]
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)

                simulator_engine = sim.engine
                sim.trigger_sensor("inside")
                await sim.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

                simulator_engine._last_sensor_trigger = 0.0  # outside retrigger window
                sim.trigger_sensor("inside")
                deadlines.append(simulator_engine._hold_deadline - loop.time())

                # Reset for the next round
                await sim.close_door()
                await sim.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)
                if connect_client:
                    writer.close()
                    await writer.wait_closed()

            # Both paths granted a full fresh hold_time
            assert deadlines[0] == pytest.approx(10.0, abs=0.5)
            assert deadlines[1] == pytest.approx(10.0, abs=0.5)
        finally:
            await sim.stop()


def _parse_json_stream(data: str) -> list[dict]:
    """Parse back-to-back JSON objects from a captured stream."""
    decoder = json.JSONDecoder()
    messages = []
    pos = 0
    while pos < len(data):
        if data[pos].isspace():
            pos += 1
            continue
        msg, end = decoder.raw_decode(data, pos)
        messages.append(msg)
        pos = end
    return messages


# ============================================================================
# Battery Simulation Tests
# ============================================================================


class TestBatterySimulation:
    """Tests for battery simulation methods."""

    @pytest.fixture
    async def simulator_with_battery(self, timing_config):
        """Create a simulator with fast battery updates for testing."""
        battery_config = BatteryConfig(
            charge_rate=600.0,  # 600%/min = 10%/sec (fast for testing)
            discharge_rate=600.0,  # 600%/min = 10%/sec
            update_interval=0.1,  # Update every 100ms
        )
        state = DoorSimulatorState(
            timing=timing_config,
            hold_time=1,
            battery_config=battery_config,
            battery_percent=50,
        )
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        yield sim
        await sim.stop()

    def test_set_ac_present(self, simulator):
        """set_ac_present should update AC state."""
        assert simulator.state.ac_present is True
        simulator.set_ac_present(False)
        assert simulator.state.ac_present is False
        simulator.set_ac_present(True)
        assert simulator.state.ac_present is True

    def test_set_battery_present(self, simulator):
        """set_battery_present should update battery presence."""
        assert simulator.state.battery_present is True
        simulator.set_battery_present(False)
        assert simulator.state.battery_present is False
        simulator.set_battery_present(True)
        assert simulator.state.battery_present is True

    def test_set_charge_rate(self, simulator):
        """set_charge_rate should update charge rate."""
        simulator.set_charge_rate(5.0)
        assert simulator.state.battery_config.charge_rate == 5.0
        simulator.set_charge_rate(0.0)
        assert simulator.state.battery_config.charge_rate == 0.0

    def test_set_discharge_rate(self, simulator):
        """set_discharge_rate should update discharge rate."""
        simulator.set_discharge_rate(0.5)
        assert simulator.state.battery_config.discharge_rate == 0.5
        simulator.set_discharge_rate(0.0)
        assert simulator.state.battery_config.discharge_rate == 0.0

    def test_set_charge_rate_negative_clamps_to_zero(self, simulator):
        """set_charge_rate should clamp negative values to zero."""
        simulator.set_charge_rate(-5.0)
        assert simulator.state.battery_config.charge_rate == 0.0

    def test_set_discharge_rate_negative_clamps_to_zero(self, simulator):
        """set_discharge_rate should clamp negative values to zero."""
        simulator.set_discharge_rate(-5.0)
        assert simulator.state.battery_config.discharge_rate == 0.0

    @pytest.mark.asyncio
    async def test_battery_charges_when_ac_present(self, simulator_with_battery):
        """Battery should charge when AC is present."""
        sim = simulator_with_battery
        sim.set_ac_present(True)
        initial = sim.state.battery_percent

        # Wait for a few update cycles
        await asyncio.sleep(0.3)

        # Battery should have increased
        assert sim.state.battery_percent > initial

    @pytest.mark.asyncio
    async def test_battery_discharges_when_ac_absent(self, simulator_with_battery):
        """Battery should discharge when AC is absent."""
        sim = simulator_with_battery
        sim.set_ac_present(False)
        initial = sim.state.battery_percent

        # Wait for a few update cycles
        await asyncio.sleep(0.3)

        # Battery should have decreased
        assert sim.state.battery_percent < initial

    @pytest.mark.asyncio
    async def test_battery_no_change_when_absent(self, simulator_with_battery):
        """Battery should not change when battery is absent."""
        sim = simulator_with_battery
        sim.set_battery_present(False)
        initial = sim.state.battery_percent
        sim.set_ac_present(False)

        await asyncio.sleep(0.3)

        # Battery should not have changed
        assert sim.state.battery_percent == initial

    @pytest.mark.asyncio
    async def test_battery_caps_at_100(self, simulator_with_battery):
        """Battery should not exceed 100%."""
        sim = simulator_with_battery
        sim.set_battery(99)
        sim.set_ac_present(True)

        await asyncio.sleep(0.5)

        # Battery should be capped at 100
        assert sim.state.battery_percent <= 100

    @pytest.mark.asyncio
    async def test_battery_floors_at_0(self, simulator_with_battery):
        """Battery should not go below 0%."""
        sim = simulator_with_battery
        sim.set_battery(1)
        sim.set_ac_present(False)

        await asyncio.sleep(0.5)

        # Battery should be floored at 0
        assert sim.state.battery_percent >= 0

    @pytest.mark.asyncio
    async def test_zero_charge_rate_no_change(self, simulator_with_battery):
        """Battery should not change with zero charge rate."""
        sim = simulator_with_battery
        sim.set_charge_rate(0.0)
        sim.set_ac_present(True)
        initial = sim.state.battery_percent

        await asyncio.sleep(0.3)

        # Battery should not have changed
        assert sim.state.battery_percent == initial

    @pytest.mark.asyncio
    async def test_zero_discharge_rate_no_change(self, simulator_with_battery):
        """Battery should not change with zero discharge rate."""
        sim = simulator_with_battery
        sim.set_discharge_rate(0.0)
        sim.set_ac_present(False)
        initial = sim.state.battery_percent

        await asyncio.sleep(0.3)

        # Battery should not have changed
        assert sim.state.battery_percent == initial


# ============================================================================
# Client Connection Management Tests
# ============================================================================


class TestClientConnectionManagement:
    """Tests for client connect/disconnect and protocols list management."""

    @pytest.mark.asyncio
    async def test_protocols_list_starts_empty(self, simulator):
        """protocols list should be empty when no clients connected."""
        assert len(simulator.protocols) == 0

    @pytest.mark.asyncio
    async def test_client_connect_adds_to_protocols(self, simulator):
        """Connecting a client should add to protocols list."""
        port = simulator.server.sockets[0].getsockname()[1]

        # Connect a client
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)  # Give time for connection to be processed

        assert len(simulator.protocols) == 1

        writer.close()
        await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_client_disconnect_removes_from_protocols(self, simulator):
        """Disconnecting a client should remove from protocols list."""
        port = simulator.server.sockets[0].getsockname()[1]

        # Connect a client
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)

        assert len(simulator.protocols) == 1

        # Disconnect
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)  # Give time for disconnect to be processed

        assert len(simulator.protocols) == 0

    @pytest.mark.asyncio
    async def test_multiple_clients_tracked(self, simulator):
        """Multiple clients should all be tracked in protocols list."""
        port = simulator.server.sockets[0].getsockname()[1]

        # Connect multiple clients
        clients = []
        for _ in range(3):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            clients.append((reader, writer))
            await asyncio.sleep(0.02)

        assert len(simulator.protocols) == 3

        # Disconnect one
        _, writer = clients.pop()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        assert len(simulator.protocols) == 2

        # Cleanup remaining
        for _, writer in clients:
            writer.close()
            await writer.wait_closed()


# ============================================================================
# Broadcast Tests
# ============================================================================


class TestBroadcastFunctions:
    """Tests for broadcast functions that send updates to connected clients."""

    @pytest.mark.asyncio
    async def test_broadcast_hold_time_sends_centiseconds(self, simulator):
        """broadcast_hold_time should send hold time in centiseconds."""
        port = simulator.server.sockets[0].getsockname()[1]

        # Connect a client
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)

        # Set hold time to 5 seconds
        simulator.state.hold_time = 5.0

        # Trigger broadcast
        simulator.broadcast_hold_time()

        # Read the broadcast message
        try:
            data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            import json

            msg = json.loads(data.decode("ascii"))

            # Should contain hold time in centiseconds (500)
            assert msg.get("holdTime") == 500
        finally:
            writer.close()
            await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_broadcast_settings_sends_to_all_clients(self, simulator):
        """broadcast_settings should send settings to all connected clients."""
        port = simulator.server.sockets[0].getsockname()[1]

        # Connect two clients
        clients = []
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            clients.append((reader, writer))
            await asyncio.sleep(0.02)

        # Trigger broadcast
        simulator.broadcast_settings()

        # Both clients should receive the broadcast
        import json

        for reader, writer in clients:
            try:
                data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
                msg = json.loads(data.decode("ascii"))
                assert "settings" in msg
            finally:
                writer.close()
                await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_broadcast_settings_hold_time_in_centiseconds(self, simulator):
        """broadcast_settings should include hold time in centiseconds."""
        port = simulator.server.sockets[0].getsockname()[1]

        # Connect a client
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)

        # Set hold time to 3.5 seconds
        simulator.state.hold_time = 3.5

        # Trigger broadcast
        simulator.broadcast_settings()

        # Read the broadcast message
        import json

        try:
            data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            msg = json.loads(data.decode("ascii"))

            # Settings should contain hold time in centiseconds (350)
            settings = msg.get("settings", {})
            assert settings.get("holdOpenTime") == 350
        finally:
            writer.close()
            await writer.wait_closed()


# ============================================================================
# Notification Event Tests (D2: bare envelope)
# ============================================================================


class TestLowBatteryNotification:
    """Low battery notifications use the bare envelope from protocol.md."""

    @pytest.mark.asyncio
    async def test_set_battery_crossing_threshold_sends_bare_envelope(self, simulator):
        """Crossing the threshold emits exactly {"LOW_BATTERY": ""}."""
        port = simulator.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.sleep(0.05)  # let the connection register
            simulator.state.low_battery = True
            simulator.set_battery(15)  # 100 -> 15 crosses the 20% threshold

            data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
            messages = _parse_json_stream(data.decode("ascii"))
            low_battery = [m for m in messages if NOTIFY_LOW_BATTERY in m]
            assert low_battery == [{NOTIFY_LOW_BATTERY: ""}]
        finally:
            writer.close()
            await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_no_notification_when_disabled(self, simulator):
        """No LOW_BATTERY event when the notification setting is off."""
        port = simulator.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.sleep(0.05)
            simulator.state.low_battery = False
            simulator.set_battery(15)

            # The battery status broadcast still arrives, but no notification
            data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
            messages = _parse_json_stream(data.decode("ascii"))
            assert all(NOTIFY_LOW_BATTERY not in m for m in messages)
        finally:
            writer.close()
            await writer.wait_closed()


# ============================================================================
# Shutdown Cleanliness Tests (L14: no leaked tasks)
# ============================================================================


class TestShutdownCleanliness:
    """stop() must cancel and await every simulator-owned task."""

    @pytest.mark.asyncio
    async def test_stop_with_door_mid_cycle(self, timing_config):
        """Stopping mid-cycle leaves no pending engine task."""
        state = DoorSimulatorState(timing=timing_config, hold_time=1)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()

        sim.trigger_sensor("inside")
        assert sim.engine._task is not None

        await sim.stop()
        assert sim.engine._task is None
        assert sim.engine._retired == set()
        assert sim.engine._aux_tasks == set()
        assert sim._battery_task is None
        assert all(task.done() for task in sim._tasks)

    @pytest.mark.asyncio
    async def test_stop_with_pending_sensor_timer(self, timing_config):
        """A pending timed sensor deactivation is cancelled by stop()."""
        state = DoorSimulatorState(timing=timing_config, hold_time=1)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()

        sim.activate_sensor("inside", duration=30.0)
        assert len(sim.engine._aux_tasks) == 1
        timer_task = next(iter(sim.engine._aux_tasks))

        await sim.stop()
        assert timer_task.cancelled()
        assert sim.engine._aux_tasks == set()

    @pytest.mark.asyncio
    async def test_stop_with_connected_client_mid_cycle(self, timing_config):
        """Stopping with a connected client cancels protocol tasks cleanly."""
        state = DoorSimulatorState(timing=timing_config, hold_time=1)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()

        port = sim.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.sleep(0.05)
            assert len(sim.protocols) == 1
            protocol = sim.protocols[0]

            sim.trigger_sensor("inside")
            await sim.stop()

            assert all(task.done() for task in protocol._tasks)
            assert sim.protocols == []
        finally:
            writer.close()
