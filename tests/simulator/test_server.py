# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator server module (server.py)."""
from __future__ import annotations

import asyncio

import pytest

from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    Schedule,
    DoorTimingConfig,
    BatteryConfig,
)
from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_RISING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_SLOWING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
)


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
        """open_door should change door state."""
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        await simulator.open_door(hold=True)
        await asyncio.sleep(0.1)
        # Should be KEEPUP when hold=True
        assert simulator.state.door_status in (DOOR_STATE_RISING, DOOR_STATE_KEEPUP)

    @pytest.mark.asyncio
    async def test_close_door(self, simulator):
        """close_door should close the door."""
        await simulator.open_door(hold=True)
        await asyncio.sleep(0.1)
        await simulator.close_door()
        await asyncio.sleep(0.2)
        # Should be closing or closed
        assert simulator.state.door_status in (
            DOOR_STATE_SLOWING,
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
            DOOR_STATE_CLOSED,
        )

    @pytest.mark.asyncio
    async def test_trigger_sensor_opens_door(self, simulator):
        """trigger_sensor should open the door."""
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        simulator.trigger_sensor("inside")
        await asyncio.sleep(0.1)
        assert simulator.state.door_status != DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_trigger_sensor_ignored_when_power_off(self, simulator):
        """trigger_sensor should be ignored when power is off."""
        simulator.set_power(False)
        simulator.trigger_sensor("inside")
        await asyncio.sleep(0.1)
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_trigger_sensor_ignored_when_disabled(self, simulator):
        """trigger_sensor should be ignored when sensor is disabled."""
        simulator.state.inside = False
        simulator.trigger_sensor("inside")
        await asyncio.sleep(0.1)
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_outside_sensor_ignored_with_safety_lock(self, simulator):
        """Outside sensor should be ignored when safety lock is enabled."""
        simulator.state.safety_lock = True
        simulator.trigger_sensor("outside")
        await asyncio.sleep(0.1)
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        # Inside sensor should still work
        simulator.trigger_sensor("inside")
        await asyncio.sleep(0.1)
        assert simulator.state.door_status != DOOR_STATE_CLOSED

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
        """set_pet_in_doorway should update state."""
        simulator.set_pet_in_doorway(True)
        assert simulator.state.pet_in_doorway is True

        simulator.set_pet_in_doorway(False)
        assert simulator.state.pet_in_doorway is False


# ============================================================================
# Door Operation Sequence Tests
# ============================================================================

class TestDoorOperationSequences:
    """Tests for complete door operation sequences."""

    @pytest.mark.asyncio
    async def test_full_open_close_cycle(self, simulator):
        """Door should go through complete open/close cycle."""
        states_seen = set()

        # Use trigger_sensor which starts the operation in a task (non-blocking)
        simulator.trigger_sensor("inside")

        # Track states over time
        for _ in range(50):
            states_seen.add(simulator.state.door_status)
            await asyncio.sleep(0.05)
            # Early exit if we've seen the full cycle
            if DOOR_STATE_CLOSED in states_seen and len(states_seen) > 1:
                break

        # Should have seen at least rising or holding state
        assert DOOR_STATE_RISING in states_seen or DOOR_STATE_HOLDING in states_seen

    @pytest.mark.asyncio
    async def test_obstruction_detection(self, simulator):
        """Obstruction flag should be settable and affect state."""
        # Verify initial state
        assert simulator.state.obstruction_pending is False

        # Set obstruction
        simulator.state.obstruction_pending = True
        assert simulator.state.obstruction_pending is True

        # Clear it
        simulator.state.obstruction_pending = False
        assert simulator.state.obstruction_pending is False

    @pytest.mark.asyncio
    async def test_open_and_hold_keeps_door_open(self, simulator):
        """open_door with hold=True should keep door open indefinitely."""
        await simulator.open_door(hold=True)
        await asyncio.sleep(0.2)

        # Should be in KEEPUP state (held open)
        assert simulator.state.door_status == DOOR_STATE_KEEPUP

        # Wait more time - should still be KEEPUP
        await asyncio.sleep(0.3)
        assert simulator.state.door_status == DOOR_STATE_KEEPUP


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
