# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for PowerPetDoor facade class."""

from __future__ import annotations

import asyncio

import pytest

from powerpetdoor import (
    BatteryInfo,
    DoorStatus,
    NotificationSettings,
    PowerPetDoor,
    Schedule,
    ScheduleTime,
)
from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_CMD_LOCKOUT,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
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

    await door.disconnect()


async def wait_for_door_status(door, status: DoorStatus, timeout: float = 5.0) -> None:
    """Wait deterministically until the door reports the given status.

    Event-driven (door status callbacks) - no sleep-and-hope polling.
    """
    event = asyncio.Event()

    def _check(new_status: DoorStatus) -> None:
        if new_status == status:
            event.set()

    door.on_status_change(_check)
    if door.status == status:
        event.set()
    async with asyncio.timeout(timeout):
        await event.wait()


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

    def test_from_string_invalid(self, caplog):
        """from_string maps unknown strings to UNKNOWN with a warning (L16)."""
        assert DoorStatus.from_string("INVALID") == DoorStatus.UNKNOWN
        assert DoorStatus.from_string("") == DoorStatus.UNKNOWN
        assert "Unknown door status" in caplog.text

    def test_unknown_status_is_neither_open_nor_closed(self):
        """An UNKNOWN status must not claim the door is closed (L16)."""
        door = PowerPetDoor("127.0.0.1")
        door._status = DoorStatus.UNKNOWN

        assert door.is_open is False
        assert door.is_closed is False
        assert door.is_closing is False
        assert door.position == 0

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
        settings = NotificationSettings(inside_on=True, outside_off=True, low_battery=True)
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
        """After open() the door reaches the stable HOLDING state."""
        await door.open()

        await wait_for_door_status(door, DoorStatus.HOLDING)

        assert door.status == DoorStatus.HOLDING
        assert door.is_open is True
        assert door.is_closed is False


# ============================================================================
# Door Control Tests
# ============================================================================


class TestPowerPetDoorControl:
    """Test door control methods."""

    @pytest.mark.asyncio
    async def test_open_door(self, door, simulator):
        """open() should open the door to the stable HOLDING state."""
        await door.open()

        await wait_for_door_status(door, DoorStatus.HOLDING)

        assert door.is_open

    @pytest.mark.asyncio
    async def test_open_and_hold(self, door, simulator):
        """open_and_hold() should keep door open."""
        await door.open_and_hold()

        await wait_for_door_status(door, DoorStatus.KEEPUP)

        assert door.status == DoorStatus.KEEPUP

    @pytest.mark.asyncio
    async def test_close_door(self, door, simulator):
        """close() should close the door."""
        # First open (KEEPUP is the stable held-open state)
        await simulator.open_door(hold=True)
        await wait_for_door_status(door, DoorStatus.KEEPUP)

        # Then close
        await door.close()

        await wait_for_door_status(door, DoorStatus.CLOSED)
        assert door.is_closed

    @pytest.mark.asyncio
    async def test_toggle_opens_when_closed(self, door, simulator):
        """toggle() should open when door is closed."""
        assert door.is_closed

        await door.toggle()

        await wait_for_door_status(door, DoorStatus.HOLDING)
        assert door.is_open

    @pytest.mark.asyncio
    async def test_toggle_closes_when_open(self, door, simulator):
        """toggle() should close when door is open."""
        await simulator.open_door(hold=True)
        await wait_for_door_status(door, DoorStatus.KEEPUP)

        assert door.is_open

        await door.toggle()

        await wait_for_door_status(door, DoorStatus.CLOSED)
        assert door.is_closed

    @pytest.mark.asyncio
    async def test_cycle_opens_door(self, door, simulator):
        """cycle() should open the door (and it auto-closes after hold_time)."""
        assert door.is_closed

        await door.cycle()

        await wait_for_door_status(door, DoorStatus.HOLDING)
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
        """hold_time reflects the device value exactly, in seconds."""
        # Simulator stores seconds; the wire carries centiseconds (1500),
        # and the door converts back to 15.0 seconds.
        simulator.state.hold_time = 15
        await door.refresh_settings()

        assert door.hold_time == 15.0

    @pytest.mark.asyncio
    async def test_hold_time_set(self, door, simulator):
        """set_hold_time should update hold time."""
        await door.set_hold_time(20.0)

        # door.set_hold_time sends seconds, simulator stores seconds
        assert simulator.state.hold_time == 20.0


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
        """on_status_change receives every transition of the open sequence."""
        statuses = []
        door.on_status_change(statuses.append)

        # Trigger the sensor and wait for the stable open state.
        simulator.trigger_sensor("inside")
        await wait_for_door_status(door, DoorStatus.HOLDING)

        assert statuses == [DoorStatus.RISING, DoorStatus.SLOWING, DoorStatus.HOLDING]

    @pytest.mark.asyncio
    async def test_multiple_callbacks(self, door, simulator):
        """Multiple callbacks all receive the same transitions."""
        calls1 = []
        calls2 = []

        door.on_status_change(calls1.append)
        door.on_status_change(calls2.append)

        simulator.trigger_sensor("inside")
        await wait_for_door_status(door, DoorStatus.HOLDING)

        assert calls1 == calls2
        assert calls1[-1] == DoorStatus.HOLDING


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
        """refresh should update every cached aspect from the simulator."""
        simulator.state.door_status = DOOR_STATE_HOLDING
        simulator.state.battery_percent = 73

        await door.refresh()

        assert door.status == DoorStatus.HOLDING
        assert door.battery_percent == 73
        assert door.firmware_version != ""


# ============================================================================
# Settings Coercion Tests (test-fanatic H1)
# ============================================================================


class TestSettingsCoercion:
    """_on_settings must coerce protocol '0'/'1' strings, not bool() them."""

    def _make_door(self):
        return PowerPetDoor("127.0.0.1")

    async def test_on_settings_parses_protocol_string_zeros(self):
        """power:'0' etc must cache as False (bool('0') is True — the bug)."""
        door = self._make_door()

        door._on_settings(
            {
                FIELD_POWER: "0",
                FIELD_INSIDE: "0",
                FIELD_OUTSIDE: "0",
                FIELD_AUTO: "0",
                FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: "0",
                FIELD_AUTORETRACT: "0",
                FIELD_CMD_LOCKOUT: "0",
            }
        )

        assert door.power is False
        assert door.inside_sensor is False
        assert door.outside_sensor is False
        assert door.auto is False
        assert door.safety_lock is False
        assert door.autoretract is False
        # Inverted: lockout "0" means pet-proximity keep-open is enabled
        assert door.pet_proximity_keep_open is True

    async def test_on_settings_parses_protocol_string_ones(self):
        """power:'1' etc must cache as True; lockout '1' means keep-open off."""
        door = self._make_door()
        door._power = False
        door._inside_sensor = False
        door._pet_proximity_keep_open = True

        door._on_settings(
            {
                FIELD_POWER: "1",
                FIELD_INSIDE: "1",
                FIELD_CMD_LOCKOUT: "1",
            }
        )

        assert door.power is True
        assert door.inside_sensor is True
        assert door.pet_proximity_keep_open is False

    async def test_on_settings_unrecognized_value_leaves_cache(self):
        """An unparseable settings value must not clobber the cached state."""
        door = self._make_door()
        door._power = True

        door._on_settings({FIELD_POWER: "banana"})

        assert door.power is True

    async def test_refresh_settings_power_off_reflected(self, door, simulator):
        """End-to-end: simulator power off -> refresh -> door.power False."""
        simulator.state.power = False

        await door.refresh_settings()

        assert door.power is False


# ============================================================================
# Connect Lifecycle Tests (D5/C1, M10, M6)
# ============================================================================


class TestConnectLifecycle:
    """The documented no-loop connect pattern and failure semantics."""

    async def test_connect_without_explicit_loop(self, simulator):
        """PowerPetDoor(host); await door.connect() works with loop=None (C1)."""
        port = simulator.server.sockets[0].getsockname()[1]
        door = PowerPetDoor("127.0.0.1", port=port, keepalive=0, timeout=5.0, reconnect=0.5)

        await door.connect()
        try:
            assert door.connected is True
        finally:
            await door.disconnect()

    async def test_connect_failure_raises_connection_error(self, unused_tcp_port):
        """connect() to a dead port raises ConnectionError, not silence (M10)."""
        door = PowerPetDoor(
            "127.0.0.1", port=unused_tcp_port, keepalive=0, timeout=0.2, reconnect=0.1
        )

        with pytest.raises(ConnectionError):
            await door.connect(timeout=0.5)

        assert door.connected is False

    async def test_connect_failure_leaves_no_reconnect_zombie(self, unused_tcp_port):
        """After a raised connect(), the client must not keep reconnecting."""
        door = PowerPetDoor(
            "127.0.0.1", port=unused_tcp_port, keepalive=0, timeout=0.2, reconnect=0.1
        )

        with pytest.raises(ConnectionError):
            await door.connect(timeout=0.5)

        assert door._client._shutdown is True
        assert door._client._reconnect_task is None

    async def test_disconnect_before_connect_is_safe(self):
        """disconnect() before connect() must not raise (M6)."""
        door = PowerPetDoor("127.0.0.1")
        await door.disconnect()

    async def test_double_disconnect_is_safe(self, simulator):
        """Two disconnect() calls in a row must not raise (M6)."""
        port = simulator.server.sockets[0].getsockname()[1]
        door = PowerPetDoor("127.0.0.1", port=port, keepalive=0, timeout=5.0)

        await door.connect()
        await door.disconnect()
        await door.disconnect()

        assert door.connected is False

    async def test_reconnect_after_disconnect(self, simulator):
        """connect() after disconnect() re-arms the client (M6)."""
        port = simulator.server.sockets[0].getsockname()[1]
        door = PowerPetDoor("127.0.0.1", port=port, keepalive=0, timeout=5.0)

        await door.connect()
        await door.disconnect()
        assert door.connected is False

        await door.connect()
        try:
            assert door.connected is True
        finally:
            await door.disconnect()

    async def test_refresh_scheduled_after_auto_reconnect(self, simulator):
        """After a client-level auto-reconnect, the cache resynchronizes (M10)."""
        port = simulator.server.sockets[0].getsockname()[1]
        door = PowerPetDoor("127.0.0.1", port=port, keepalive=0, timeout=5.0, reconnect=0.05)
        await door.connect()

        refreshed = asyncio.Event()
        door.on_status_change(lambda s: refreshed.set() if s == DoorStatus.HOLDING else None)

        # Change simulator state, then kill the connection server-side.
        simulator.state.door_status = DOOR_STATE_HOLDING
        for protocol in list(simulator.protocols):
            protocol.transport.close()

        try:
            # The post-reconnect refresh() must pick up the new status.
            async with asyncio.timeout(5.0):
                await refreshed.wait()
            assert door.status == DoorStatus.HOLDING
        finally:
            await door.disconnect()


# ============================================================================
# Schedule API Tests (H10)
# ============================================================================


def _sim_schedule(index, days, start=(7, 30), end=(21, 15), inside=True, outside=False):
    """Build a simulator-side schedule entry."""
    from powerpetdoor.simulator.state import Schedule as SimSchedule

    return SimSchedule(
        index=index,
        inside=inside,
        outside=outside,
        days_of_week=days,
        start_hour=start[0],
        start_min=start[1],
        end_hour=end[0],
        end_min=end[1],
    )


class TestDoorSchedules:
    """door.py schedule methods against the simulator (H10)."""

    @pytest.mark.asyncio
    async def test_refresh_schedules_two_step_fetch(self, door, simulator):
        """refresh_schedules fetches the list then each schedule."""
        simulator.state.schedules[0] = _sim_schedule(0, [0, 1, 1, 1, 1, 1, 0])
        simulator.state.schedules[2] = _sim_schedule(
            2, [1, 0, 0, 0, 0, 0, 1], start=(9, 0), end=(17, 0), inside=False, outside=True
        )

        schedules = await door.refresh_schedules()

        assert [s.index for s in schedules] == [0, 2]
        assert schedules[0].inside is True
        assert schedules[0].days_of_week == [False, True, True, True, True, True, False]
        assert schedules[0].start.hour == 7
        assert schedules[0].start.minute == 30
        assert schedules[0].end.hour == 21
        assert schedules[0].end.minute == 15
        assert schedules[1].outside is True
        assert schedules[1].start.hour == 9
        assert schedules[1].end.hour == 17
        assert [s.index for s in door.schedules] == [0, 2]

    @pytest.mark.asyncio
    async def test_refresh_schedules_empty(self, door, simulator):
        """No schedules on the device returns [] and clears the cache."""
        door._schedules = [Schedule(index=5)]

        schedules = await door.refresh_schedules()

        assert schedules == []
        assert door.schedules == []

    @pytest.mark.asyncio
    async def test_get_schedule_by_index(self, door, simulator):
        """get_schedule fetches a single schedule."""
        simulator.state.schedules[1] = _sim_schedule(1, [1, 1, 1, 1, 1, 1, 1])

        schedule = await door.get_schedule(1)

        assert schedule.index == 1
        assert schedule.inside is True
        assert schedule.start.hour == 7

    @pytest.mark.asyncio
    async def test_get_schedule_unknown_index_raises(self, door, simulator):
        """get_schedule on a missing index raises CommandError."""
        from powerpetdoor import CommandError

        with pytest.raises(CommandError) as excinfo:
            await door.get_schedule(99)

        assert excinfo.value.reason == "Schedule not found"

    @pytest.mark.asyncio
    async def test_set_schedule_roundtrip(self, door, simulator):
        """set_schedule stores the schedule on the device and in the cache."""
        schedule = Schedule(
            index=3,
            enabled=True,
            days_of_week=[False, True, True, True, True, True, False],
            inside=True,
            outside=False,
            start=ScheduleTime(hour=6, minute=15),
            end=ScheduleTime(hour=22, minute=45),
        )

        await door.set_schedule(schedule)

        stored = simulator.state.schedules[3]
        assert stored.inside is True
        assert stored.start_hour == 6
        assert stored.start_min == 15
        assert stored.end_hour == 22
        assert stored.end_min == 45
        assert [s.index for s in door.schedules] == [3]

    @pytest.mark.asyncio
    async def test_delete_schedule_removes(self, door, simulator):
        """delete_schedule removes it from the device and the cache."""
        simulator.state.schedules[0] = _sim_schedule(0, [1] * 7)
        await door.refresh_schedules()
        assert [s.index for s in door.schedules] == [0]

        await door.delete_schedule(0)

        assert 0 not in simulator.state.schedules
        assert door.schedules == []

    @pytest.mark.asyncio
    async def test_on_schedule_change_fired_on_set_and_delete(self, door, simulator):
        """Schedule callbacks fire with the updated list on set and delete."""
        snapshots = []
        door.on_schedule_change(lambda schedules: snapshots.append(list(schedules)))

        await door.set_schedule(Schedule(index=0, inside=True))
        assert snapshots
        assert [s.index for s in snapshots[-1]] == [0]

        await door.delete_schedule(0)
        assert snapshots[-1] == []


# ============================================================================
# Notifications API Tests (H10)
# ============================================================================


class TestSetNotifications:
    """set_notifications merge semantics and wire format (H10)."""

    @pytest.mark.asyncio
    async def test_partial_update_preserves_others(self, door):
        """Unspecified settings are sent with their cached values."""
        from powerpetdoor.const import (
            FIELD_LOW_BATTERY_NOTIFICATIONS,
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
        )

        door._notifications.inside_on = True
        sent = {}

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            sent.update(kwargs)
            future = asyncio.get_running_loop().create_future()
            future.set_result({})
            return future

        door._client.send_message = fake_send

        await door.set_notifications(low_battery=True)

        # Wire values are "1"/"0" strings per docs/protocol.md.
        assert sent == {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "1",  # Preserved from cache
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: "0",
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: "0",
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: "0",
            FIELD_LOW_BATTERY_NOTIFICATIONS: "1",  # Explicitly set
        }


# ============================================================================
# Latency / Version / Position Tests (H10)
# ============================================================================


class TestDoorLatency:
    """Latency tracking from ping/pong (H10)."""

    @pytest.mark.asyncio
    async def test_latency_set_by_ping(self):
        """_on_ping converts milliseconds to seconds."""
        door = PowerPetDoor("127.0.0.1")
        assert door.latency is None

        door._on_ping(50)

        assert door.latency == 0.05

    @pytest.mark.asyncio
    async def test_latency_cleared_on_disconnect(self):
        """Disconnection resets latency to None."""
        door = PowerPetDoor("127.0.0.1")
        door._on_ping(50)

        await door._on_disconnect()

        assert door.latency is None


class TestVersionFormatting:
    """firmware_version / hardware_version string formatting (H10)."""

    @pytest.mark.asyncio
    async def test_firmware_version_populated(self):
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"fw_maj": 1, "fw_min": 2, "fw_pat": 3}
        assert door.firmware_version == "1.2.3"

    @pytest.mark.asyncio
    async def test_firmware_version_empty(self):
        door = PowerPetDoor("127.0.0.1")
        assert door.firmware_version == ""

    @pytest.mark.asyncio
    async def test_firmware_version_partial_defaults_zero(self):
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"fw_maj": 2}
        assert door.firmware_version == "2.0.0"

    @pytest.mark.asyncio
    async def test_hardware_version_populated(self):
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"ver": "1", "rev": "2"}
        assert door.hardware_version == "1 rev 2"

    @pytest.mark.asyncio
    async def test_hardware_version_empty_dict(self):
        door = PowerPetDoor("127.0.0.1")
        assert door.hardware_version == ""

    @pytest.mark.asyncio
    async def test_hardware_version_no_ver_fields(self):
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"fw_maj": 1}
        assert door.hardware_version == ""


class TestToggleWhileClosing:
    """toggle() is a no-op while the door is closing (H10)."""

    @pytest.mark.asyncio
    async def test_toggle_noop_while_closing(self):
        from unittest.mock import AsyncMock, patch

        door = PowerPetDoor("127.0.0.1")
        door._status = DoorStatus.CLOSING_TOP_OPEN

        with (
            patch.object(door, "open", new_callable=AsyncMock) as mock_open,
            patch.object(door, "close", new_callable=AsyncMock) as mock_close,
        ):
            await door.toggle()

        assert mock_open.await_count == 0
        assert mock_close.await_count == 0


class TestStatusCallbackIsolation:
    """A raising status callback must not break the others (H10)."""

    @pytest.mark.asyncio
    async def test_status_callback_exception_does_not_break_others(self):
        door = PowerPetDoor("127.0.0.1")
        calls = []

        def bad_callback(status):
            calls.append("bad")
            raise RuntimeError("callback bug")

        door.on_status_change(bad_callback)
        door.on_status_change(lambda status: calls.append(("good", status)))

        door._on_door_status(DOOR_STATE_RISING)

        assert calls == ["bad", ("good", DoorStatus.RISING)]


class TestPositionMap:
    """position maps every status to an exact percentage (H10)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (DoorStatus.IDLE, 0),
            (DoorStatus.CLOSED, 0),
            (DoorStatus.RISING, 33),
            (DoorStatus.SLOWING, 66),
            (DoorStatus.HOLDING, 100),
            (DoorStatus.KEEPUP, 100),
            (DoorStatus.CLOSING_TOP_OPEN, 66),
            (DoorStatus.CLOSING_MID_OPEN, 33),
            (DoorStatus.UNKNOWN, 0),
        ],
    )
    async def test_position_for_status(self, status, expected):
        door = PowerPetDoor("127.0.0.1")
        door._status = status
        assert door.position == expected
