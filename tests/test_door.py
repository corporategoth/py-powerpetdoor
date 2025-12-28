# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for PowerPetDoor facade class."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from powerpetdoor import (
    PowerPetDoor,
    DoorStatus,
    NotificationSettings,
    BatteryInfo,
    Schedule,
    ScheduleTime,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_RISING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
)


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def fast_timing():
    """Create fast timing config for tests."""
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
async def door(simulator) -> PowerPetDoor:
    """Create a PowerPetDoor connected to the simulator."""
    port = simulator.server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()

    door = PowerPetDoor(
        host="127.0.0.1",
        port=port,
        keepalive=0,  # Disable keepalive for tests
        timeout=5.0,
        reconnect=1.0,
        loop=loop,
    )

    await door.connect()

    yield door

    # Cleanup - mark as shutdown and disconnect
    door._client._shutdown = True
    door._client.disconnect()


# ============================================================================
# DoorStatus Enum Tests
# ============================================================================


class TestDoorStatus:
    """Test DoorStatus enum."""

    def test_from_string_valid(self):
        """from_string should convert valid status strings."""
        assert DoorStatus.from_string(DOOR_STATE_CLOSED) == DoorStatus.CLOSED
        assert DoorStatus.from_string(DOOR_STATE_RISING) == DoorStatus.RISING
        assert DoorStatus.from_string(DOOR_STATE_HOLDING) == DoorStatus.HOLDING
        assert DoorStatus.from_string(DOOR_STATE_KEEPUP) == DoorStatus.KEEPUP

    def test_from_string_invalid(self):
        """from_string should return CLOSED for invalid strings."""
        assert DoorStatus.from_string("INVALID") == DoorStatus.CLOSED
        assert DoorStatus.from_string("") == DoorStatus.CLOSED

    def test_all_states_have_values(self):
        """All enum members should have non-empty values."""
        for status in DoorStatus:
            assert status.value
            assert isinstance(status.value, str)


# ============================================================================
# Dataclass Tests
# ============================================================================


class TestNotificationSettings:
    """Test NotificationSettings dataclass."""

    def test_defaults(self):
        """Default values should all be False."""
        settings = NotificationSettings()
        assert settings.inside_on is False
        assert settings.inside_off is False
        assert settings.outside_on is False
        assert settings.outside_off is False
        assert settings.low_battery is False

    def test_custom_values(self):
        """Custom values should be stored correctly."""
        settings = NotificationSettings(
            inside_on=True, outside_off=True, low_battery=True
        )
        assert settings.inside_on is True
        assert settings.inside_off is False
        assert settings.outside_on is False
        assert settings.outside_off is True
        assert settings.low_battery is True


class TestBatteryInfo:
    """Test BatteryInfo dataclass."""

    def test_defaults(self):
        """Default values should indicate full battery with AC."""
        battery = BatteryInfo()
        assert battery.percent == 100
        assert battery.present is True
        assert battery.ac_present is True

    def test_charging_property(self):
        """charging should be True when AC present and not full."""
        battery = BatteryInfo(percent=50, ac_present=True)
        assert battery.charging is True

        battery = BatteryInfo(percent=100, ac_present=True)
        assert battery.charging is False

        battery = BatteryInfo(percent=50, ac_present=False)
        assert battery.charging is False

    def test_discharging_property(self):
        """discharging should be True when no AC and battery present."""
        battery = BatteryInfo(percent=50, present=True, ac_present=False)
        assert battery.discharging is True

        battery = BatteryInfo(percent=50, present=True, ac_present=True)
        assert battery.discharging is False

        battery = BatteryInfo(percent=50, present=False, ac_present=False)
        assert battery.discharging is False


class TestScheduleTime:
    """Test ScheduleTime dataclass."""

    def test_defaults(self):
        """Default values should be midnight."""
        time = ScheduleTime()
        assert time.hour == 0
        assert time.minute == 0

    def test_to_dict(self):
        """to_dict should create protocol-compatible dict."""
        time = ScheduleTime(hour=14, minute=30)
        d = time.to_dict()
        assert d["hour"] == 14
        assert d["min"] == 30

    def test_from_dict(self):
        """from_dict should parse protocol dict."""
        time = ScheduleTime.from_dict({"hour": 8, "min": 45})
        assert time.hour == 8
        assert time.minute == 45


class TestSchedule:
    """Test Schedule dataclass."""

    def test_defaults(self):
        """Default values should be reasonable."""
        schedule = Schedule()
        assert schedule.index == 0
        assert schedule.enabled is True
        assert schedule.days_of_week == [1, 1, 1, 1, 1, 1, 1]  # All days
        assert schedule.inside is False
        assert schedule.outside is False
        assert schedule.start.hour == 6
        assert schedule.end.hour == 22

    def test_to_dict_roundtrip(self):
        """Schedule should survive to_dict/from_dict roundtrip."""
        original = Schedule(
            index=2,
            enabled=True,
            days_of_week=[0, 1, 0, 1, 0, 1, 0],
            inside=True,
            outside=False,
            start=ScheduleTime(hour=6, minute=0),
            end=ScheduleTime(hour=22, minute=0),
        )
        d = original.to_dict()
        restored = Schedule.from_dict(d)

        assert restored.index == original.index
        assert restored.enabled == original.enabled
        assert restored.days_of_week == original.days_of_week
        assert restored.inside == original.inside
        assert restored.outside == original.outside
        assert restored.start.hour == original.start.hour
        assert restored.end.minute == original.end.minute


# ============================================================================
# Connection Tests
# ============================================================================


class TestPowerPetDoorConnection:
    """Test PowerPetDoor connection handling."""

    @pytest.mark.asyncio
    async def test_connects_to_simulator(self, door, simulator):
        """Door should successfully connect to simulator."""
        assert door.connected
        assert len(simulator.protocols) == 1

    @pytest.mark.asyncio
    async def test_host_port_properties(self, door, simulator):
        """Door should report correct host and port."""
        port = simulator.server.sockets[0].getsockname()[1]
        assert door.host == "127.0.0.1"
        assert door.port == port


# ============================================================================
# Door Status Tests
# ============================================================================


class TestPowerPetDoorStatus:
    """Test door status properties."""

    @pytest.mark.asyncio
    async def test_initial_status_closed(self, door):
        """Door should start in closed state."""
        assert door.status == DoorStatus.CLOSED
        assert door.is_closed is True
        assert door.is_open is False
        assert door.position == 0

    @pytest.mark.asyncio
    async def test_status_after_open(self, door, simulator):
        """Status should update after opening."""
        await door.open()

        # Wait for door to open (poll instead of fixed sleep for CI reliability)
        for _ in range(50):
            if door.is_open:
                break
            await asyncio.sleep(0.1)

        assert door.status in (DoorStatus.RISING, DoorStatus.HOLDING, DoorStatus.KEEPUP)
        assert door.is_open is True
        assert door.is_closed is False


# ============================================================================
# Door Control Tests
# ============================================================================


class TestPowerPetDoorControl:
    """Test door control methods."""

    @pytest.mark.asyncio
    async def test_open_door(self, door, simulator):
        """open() should open the door."""
        await door.open()

        # Wait for door to open
        for _ in range(50):
            if door.is_open:
                break
            await asyncio.sleep(0.1)

        assert door.is_open

    @pytest.mark.asyncio
    async def test_open_and_hold(self, door, simulator):
        """open_and_hold() should keep door open."""
        await door.open_and_hold()

        # Wait for door to reach keepup
        for _ in range(50):
            if door.status == DoorStatus.KEEPUP:
                break
            await asyncio.sleep(0.1)

        assert door.status == DoorStatus.KEEPUP

    @pytest.mark.asyncio
    async def test_close_door(self, door, simulator):
        """close() should close the door."""
        # First open
        await simulator.open_door(hold=True)
        await asyncio.sleep(0.2)

        # Then close
        await door.close()

        # Wait for door to close
        for _ in range(50):
            if door.is_closed:
                break
            await asyncio.sleep(0.1)

        assert door.is_closed

    @pytest.mark.asyncio
    async def test_toggle_opens_when_closed(self, door, simulator):
        """toggle() should open when door is closed."""
        assert door.is_closed

        await door.toggle()

        # Wait for door to open (poll instead of fixed sleep for CI reliability)
        for _ in range(50):
            if door.is_open:
                break
            await asyncio.sleep(0.1)

        assert door.is_open

    @pytest.mark.asyncio
    async def test_toggle_closes_when_open(self, door, simulator):
        """toggle() should close when door is open."""
        await simulator.open_door(hold=True)
        await asyncio.sleep(0.2)
        await door.refresh_status()

        assert door.is_open

        await door.toggle()

        # Wait for door to close
        for _ in range(50):
            if door.is_closed:
                break
            await asyncio.sleep(0.1)

        assert door.is_closed

    @pytest.mark.asyncio
    async def test_cycle_opens_door(self, door, simulator):
        """cycle() should open the door (and it auto-closes after hold_time)."""
        assert door.is_closed

        await door.cycle()

        # Wait for door to open
        for _ in range(50):
            if door.is_open:
                break
            await asyncio.sleep(0.1)

        assert door.is_open


# ============================================================================
# Sensor Tests
# ============================================================================


class TestPowerPetDoorSensors:
    """Test sensor control."""

    @pytest.mark.asyncio
    async def test_inside_sensor_initial(self, door):
        """Inside sensor should start enabled."""
        assert door.inside_sensor is True

    @pytest.mark.asyncio
    async def test_disable_inside_sensor(self, door, simulator):
        """set_inside_sensor(False) should disable sensor."""
        await door.set_inside_sensor(False)

        assert door.inside_sensor is False
        assert simulator.state.inside is False

    @pytest.mark.asyncio
    async def test_enable_inside_sensor(self, door, simulator):
        """set_inside_sensor(True) should enable sensor."""
        simulator.state.inside = False

        await door.set_inside_sensor(True)

        assert door.inside_sensor is True
        assert simulator.state.inside is True

    @pytest.mark.asyncio
    async def test_outside_sensor(self, door, simulator):
        """Outside sensor should be controllable."""
        await door.set_outside_sensor(False)
        assert door.outside_sensor is False

        await door.set_outside_sensor(True)
        assert door.outside_sensor is True


# ============================================================================
# Power Tests
# ============================================================================


class TestPowerPetDoorPower:
    """Test power control."""

    @pytest.mark.asyncio
    async def test_power_initial(self, door):
        """Power should start on."""
        assert door.power is True

    @pytest.mark.asyncio
    async def test_power_off(self, door, simulator):
        """set_power(False) should turn off power."""
        await door.set_power(False)

        assert door.power is False
        assert simulator.state.power is False

    @pytest.mark.asyncio
    async def test_power_on(self, door, simulator):
        """set_power(True) should turn on power."""
        simulator.state.power = False

        await door.set_power(True)

        assert door.power is True
        assert simulator.state.power is True


# ============================================================================
# Auto Mode Tests
# ============================================================================


class TestPowerPetDoorAuto:
    """Test auto/schedule mode."""

    @pytest.mark.asyncio
    async def test_auto_initial(self, door):
        """Auto should reflect simulator default (enabled)."""
        assert door.auto is True

    @pytest.mark.asyncio
    async def test_enable_auto(self, door, simulator):
        """set_auto(True) should enable auto mode."""
        await door.set_auto(True)

        assert door.auto is True
        assert simulator.state.auto is True

    @pytest.mark.asyncio
    async def test_disable_auto(self, door, simulator):
        """set_auto(False) should disable auto mode."""
        simulator.state.auto = True

        await door.set_auto(False)

        assert door.auto is False
        assert simulator.state.auto is False


# ============================================================================
# Safety Feature Tests
# ============================================================================


class TestPowerPetDoorSafety:
    """Test safety features."""

    @pytest.mark.asyncio
    async def test_safety_lock(self, door, simulator):
        """Safety lock should be controllable."""
        await door.set_safety_lock(True)
        assert door.safety_lock is True

        await door.set_safety_lock(False)
        assert door.safety_lock is False

    @pytest.mark.asyncio
    async def test_autoretract(self, door, simulator):
        """Autoretract should be controllable."""
        await door.set_autoretract(False)
        assert door.autoretract is False

        await door.set_autoretract(True)
        assert door.autoretract is True


# ============================================================================
# Configuration Tests
# ============================================================================


class TestPowerPetDoorConfig:
    """Test configuration properties."""

    @pytest.mark.asyncio
    async def test_hold_time_get(self, door, simulator):
        """hold_time should return current hold time."""
        # Simulator uses centiseconds internally but we set hold_time in seconds
        simulator.state.hold_time = 15
        await door.refresh_settings()

        # The refresh fetches from protocol which may convert
        assert door.hold_time > 0

    @pytest.mark.asyncio
    async def test_hold_time_set(self, door, simulator):
        """set_hold_time should update hold time."""
        await door.set_hold_time(20.0)

        # Verify via simulator (protocol converts to centiseconds)
        assert simulator.state.hold_time == 2000


# ============================================================================
# Battery Tests
# ============================================================================


class TestPowerPetDoorBattery:
    """Test battery properties."""

    @pytest.mark.asyncio
    async def test_battery_initial(self, door):
        """Battery info should have values from simulator."""
        # Simulator defaults to 100% battery
        assert door.battery_percent == 100
        assert door.battery_present is True
        assert door.ac_present is True

    @pytest.mark.asyncio
    async def test_battery_info_object(self, door):
        """battery property should return BatteryInfo."""
        info = door.battery
        assert isinstance(info, BatteryInfo)
        assert info.percent == door.battery_percent


# ============================================================================
# Callback Tests
# ============================================================================


class TestPowerPetDoorCallbacks:
    """Test callback registration."""

    @pytest.mark.asyncio
    async def test_status_change_callback(self, door, simulator):
        """on_status_change should be called when status changes."""
        statuses = []

        def callback(status: DoorStatus):
            statuses.append(status)

        door.on_status_change(callback)

        # Trigger status change
        simulator.trigger_sensor("inside")
        await asyncio.sleep(0.3)

        assert len(statuses) > 0
        assert any(s != DoorStatus.CLOSED for s in statuses)

    @pytest.mark.asyncio
    async def test_multiple_callbacks(self, door, simulator):
        """Multiple callbacks should all be called."""
        calls1 = []
        calls2 = []

        door.on_status_change(lambda s: calls1.append(s))
        door.on_status_change(lambda s: calls2.append(s))

        simulator.trigger_sensor("inside")
        await asyncio.sleep(0.3)

        assert len(calls1) > 0
        assert len(calls2) > 0


# ============================================================================
# Refresh Tests
# ============================================================================


class TestPowerPetDoorRefresh:
    """Test refresh methods."""

    @pytest.mark.asyncio
    async def test_refresh_status(self, door, simulator):
        """refresh_status should update status from door."""
        # Change simulator state directly
        simulator.state.door_status = DOOR_STATE_HOLDING

        status = await door.refresh_status()

        assert status == DoorStatus.HOLDING
        assert door.status == DoorStatus.HOLDING

    @pytest.mark.asyncio
    async def test_refresh_all(self, door, simulator):
        """refresh should update all state."""
        # Just verify it doesn't error
        await door.refresh()

        # Status should be accurate
        assert door.status == DoorStatus.from_string(simulator.state.door_status)
