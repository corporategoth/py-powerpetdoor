# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for PowerPetDoor facade class."""

from __future__ import annotations

import asyncio
import logging

import pytest

from powerpetdoor import (
    BatteryInfo,
    DoorStatus,
    NotificationSettings,
    PowerPetDoor,
    Schedule,
    ScheduleTime,
)
from powerpetdoor.client import CommandError
from powerpetdoor.const import (
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SETTINGS,
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


def _inside_payload(**overrides):
    """A minimal well-formed inside-sensor schedule payload.

    ``inside``/``outside`` entries must carry their own time window - the
    parser refuses to invent one (L5) - so hostile-input tests that are
    about some *other* field start from a complete payload.
    """
    payload = {
        "index": 0,
        "daysOfWeek": [1, 1, 1, 1, 1, 1, 1],
        "inside": True,
        "in_start_time": {"hour": 6, "min": 0},
        "in_end_time": {"hour": 22, "min": 0},
    }
    payload.update(overrides)
    return payload


class TestSchedule:
    """Test Schedule dataclass."""

    def test_defaults(self):
        """Default values should be reasonable."""
        schedule = Schedule()
        assert schedule.index == 0
        assert schedule.enabled is True
        # All days, as real booleans - True == 1 would mask an int regression (L2)
        assert schedule.days_of_week == [True, True, True, True, True, True, True]
        assert all(isinstance(day, bool) for day in schedule.days_of_week)
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

    def test_to_dict_days_are_wire_ints(self):
        """The wire protocol carries literal 1/0 ints, never bools (L2)."""
        schedule = Schedule(days_of_week=[True, False, True, False, True, False, True], inside=True)

        d = schedule.to_dict()

        assert d["daysOfWeek"] == [1, 0, 1, 0, 1, 0, 1]
        assert all(isinstance(day, int) and not isinstance(day, bool) for day in d["daysOfWeek"])

    def test_to_dict_outside_only_zeroes_inside_times(self):
        """An outside-only schedule zeroes the inside time fields."""
        schedule = Schedule(
            index=1,
            inside=False,
            outside=True,
            start=ScheduleTime(hour=9, minute=15),
            end=ScheduleTime(hour=17, minute=45),
        )

        d = schedule.to_dict()

        assert d["outside"] is True
        assert d["out_start_time"] == {"hour": 9, "min": 15}
        assert d["out_end_time"] == {"hour": 17, "min": 45}
        assert d["in_start_time"] == {"hour": 0, "min": 0}
        assert d["in_end_time"] == {"hour": 0, "min": 0}

    def test_from_dict_days_are_bools(self):
        """Wire 1/0 lists are converted to real booleans (L2)."""
        restored = Schedule.from_dict(
            _inside_payload(daysOfWeek=[1, 0, 1, 0, 1, 0, 1]),
        )

        assert restored.days_of_week == [True, False, True, False, True, False, True]
        assert all(isinstance(day, bool) for day in restored.days_of_week)

    def test_from_dict_legacy_bitmask(self):
        """A legacy int bitmask (bit 0 = Sunday) converts to booleans."""
        # 0b0111110 = 62: Monday through Friday
        restored = Schedule.from_dict(_inside_payload(daysOfWeek=62))

        assert restored.days_of_week == [False, True, True, True, True, True, False]
        assert all(isinstance(day, bool) for day in restored.days_of_week)

    def test_from_dict_no_sensor_defaults_midnight(self):
        """With neither sensor flagged, times default to midnight."""
        restored = Schedule.from_dict({})

        assert restored.inside is False
        assert restored.outside is False
        assert (restored.start.hour, restored.start.minute) == (0, 0)
        assert (restored.end.hour, restored.end.minute) == (0, 0)

    @pytest.mark.parametrize("flag", ["0", 0, False, "false", "off", "no"], ids=repr)
    def test_from_dict_disabled_day_flags_are_read_as_disabled(self, flag):
        """bool("0") is True, so day flags go through make_bool (L4/R4-M3).

        This is the library-side twin of the simulator's
        ``_coerce_schedule_day`` test: a firmware variant that sends
        ``"0"``/``"1"`` day flags (as it already does for ``enabled``) must
        not expand to every day of the week.
        """
        restored = Schedule.from_dict(_inside_payload(daysOfWeek=[flag] * 7))

        assert restored.days_of_week == [False] * 7

    @pytest.mark.parametrize("flag", ["0", 0, False, "false"], ids=repr)
    def test_from_dict_disabled_enabled_flag_is_read_as_disabled(self, flag):
        """``enabled`` is read the same way its daysOfWeek sibling is (T3)."""
        restored = Schedule.from_dict(_inside_payload(enabled=flag))

        assert restored.enabled is False

    @pytest.mark.parametrize("flag", ["1", 1, True, "true", "yes", "on"], ids=repr)
    def test_from_dict_enabled_accepts_every_flag_spelling(self, flag):
        """A bespoke ``== "1"`` read "true"/"yes"/"on" as disabled (T3)."""
        restored = Schedule.from_dict(_inside_payload(enabled=flag))

        assert restored.enabled is True

    def test_from_dict_enabled_is_always_a_real_bool(self):
        """A field declared ``enabled: bool`` must never hold 1/0 (T3)."""
        restored = Schedule.from_dict(_inside_payload(enabled=1))

        assert restored.enabled is True
        assert isinstance(restored.enabled, bool)
        assert restored.to_dict()["enabled"] is True

    def test_from_dict_unreadable_flag_fails_closed(self):
        """An unrecognizable sensor flag disables the entry, never enables it."""
        restored = Schedule.from_dict({"index": 0, "inside": ["yes"], "outside": {"a": 1}})

        assert restored.inside is False
        assert restored.outside is False


class TestDoorScheduleFromDictRejectsHostileInput:
    """The library's schedule parser reads bytes off the wire (R4-M1).

    Every payload here used to raise TypeError/AttributeError out of a
    documented public coroutine, or be swallowed by listener isolation and
    silently freeze the cached schedule list. The contract is a
    ``ValueError`` naming the offending field - the same contract the
    simulator's twin parser has had since round 3.
    """

    @pytest.mark.parametrize(
        "payload",
        [["not", "a", "schedule"], "schedule", 5, None],
        ids=["list", "string", "int", "null"],
    )
    def test_non_mapping_payload_rejected(self, payload):
        with pytest.raises(ValueError, match="Schedule must be an object"):
            Schedule.from_dict(payload)

    @pytest.mark.parametrize(
        "days",
        [[1], [1, 1, 1, 1, 1, 1, 1, 1], "1111111", None, 1.5],
        ids=["too-short", "too-long", "string", "null", "float"],
    )
    def test_wrong_shape_days_rejected(self, days):
        with pytest.raises(ValueError, match="daysOfWeek must be a list of 7 values"):
            Schedule.from_dict(_inside_payload(daysOfWeek=days))

    def test_unreadable_day_flag_rejected(self):
        with pytest.raises(ValueError, match=r"daysOfWeek\[3\] must be 0 or 1"):
            Schedule.from_dict(_inside_payload(daysOfWeek=[1, 1, 1, "maybe", 1, 1, 1]))

    @pytest.mark.parametrize(
        ("index", "message"),
        [
            ("not-a-number", "index must be a number"),
            (None, "index must be a number"),
            ([0], "index must be a number"),
            (float("inf"), "index must be a number"),
            (-1, "index must be between 0 and 255"),
            (256, "index must be between 0 and 255"),
        ],
        ids=["string", "null", "list", "inf", "negative", "too-large"],
    )
    def test_out_of_range_or_non_numeric_index_rejected(self, index, message):
        with pytest.raises(ValueError, match=message):
            Schedule.from_dict(_inside_payload(index=index))

    @pytest.mark.parametrize(
        "value",
        [5, None, [6, 0], True, 1.5, "06:00"],
        ids=["int", "null", "list", "bool", "float", "string"],
    )
    def test_non_mapping_time_rejected(self, value):
        with pytest.raises(ValueError, match="start time must be an object"):
            Schedule.from_dict(_inside_payload(in_start_time=value))

    @pytest.mark.parametrize(
        ("time_field", "message"),
        [
            ({"hour": "six", "min": 0}, "start time hour must be a number"),
            ({"hour": 24, "min": 0}, "start time hour must be between 0 and 23"),
            ({"hour": 6, "min": 60}, "start time minute must be between 0 and 59"),
            ({"min": 0}, "start time must specify hour"),
        ],
        ids=["non-numeric-hour", "hour-too-large", "minute-too-large", "no-hour"],
    )
    def test_out_of_range_times_rejected(self, time_field, message):
        with pytest.raises(ValueError, match=message):
            Schedule.from_dict(_inside_payload(in_start_time=time_field))

    def test_outside_entry_times_are_validated_too(self):
        with pytest.raises(ValueError, match="end time hour must be between 0 and 23"):
            Schedule.from_dict(
                {
                    "index": 0,
                    "outside": True,
                    "out_start_time": {"hour": 6, "min": 0},
                    "out_end_time": {"hour": 99, "min": 0},
                }
            )

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ({"index": 0, "inside": True}, "missing required field 'in_start_time'"),
            (
                {"index": 0, "inside": True, "in_start_time": {"hour": 6, "min": 0}},
                "missing required field 'in_end_time'",
            ),
            (
                {"index": 0, "outside": True, "out_end_time": {"hour": 22, "min": 0}},
                "missing required field 'out_start_time'",
            ),
        ],
        ids=["no-inside-start", "no-inside-end", "no-outside-start"],
    )
    def test_missing_time_window_is_rejected_not_invented(self, payload, message):
        """A selected sensor with no window must fail, not get a free one (L5)."""
        with pytest.raises(ValueError, match=message):
            Schedule.from_dict(payload)

    def test_valid_device_payload_still_parses(self):
        """The hardening must not reject what a real device actually sends."""
        restored = Schedule.from_dict(
            {
                "index": 3,
                "enabled": "1",
                "daysOfWeek": [1, 0, 0, 0, 0, 0, 1],
                "inside": False,
                "outside": True,
                "in_start_time": {"hour": 0, "min": 0},
                "in_end_time": {"hour": 0, "min": 0},
                "out_start_time": {"hour": 7, "min": 30},
                "out_end_time": {"hour": 21, "min": 15},
            }
        )

        assert restored.index == 3
        assert restored.enabled is True
        assert restored.days_of_week == [True, False, False, False, False, False, True]
        assert (restored.inside, restored.outside) == (False, True)
        assert (restored.start.hour, restored.start.minute) == (7, 30)
        assert (restored.end.hour, restored.end.minute) == (21, 15)

    @pytest.mark.parametrize(
        "schedule",
        [
            Schedule(index=0, inside=True, outside=False),
            Schedule(index=1, inside=False, outside=True),
            Schedule(index=2, inside=True, outside=True),
        ],
        ids=["inside", "outside", "both"],
    )
    def test_library_to_dict_output_round_trips(self, schedule):
        """Anything this library emits must survive its own parser."""
        assert Schedule.from_dict(schedule.to_dict()) == schedule

    def test_neither_sensor_round_trips_to_the_zero_window(self):
        """to_dict() writes zeroed windows when no sensor is selected.

        The entry gates nothing, so the parser reads the zeros back rather
        than the dataclass's 06:00-22:00 display default.
        """
        restored = Schedule.from_dict(Schedule(index=3).to_dict())

        assert (restored.inside, restored.outside) == (False, False)
        assert (restored.start.hour, restored.start.minute) == (0, 0)
        assert (restored.end.hour, restored.end.minute) == (0, 0)


# ============================================================================
# Connection Tests
# ============================================================================


class TestPowerPetDoorConnection:
    """Test PowerPetDoor connection handling."""

    async def test_connects_to_simulator(self, door, simulator):
        """Door should successfully connect to simulator."""
        assert door.connected
        assert len(simulator.protocols) == 1

    async def test_host_port_properties(self, door, simulator):
        """Door should report correct host and port."""
        port = simulator.server.sockets[0].getsockname()[1]
        assert door.host == "127.0.0.1"
        assert door.port == port

    async def test_second_connect_does_not_open_a_second_connection(self, door, simulator, caplog):
        """connect() while connected is a no-op, not a leaked socket (M2)."""
        transport = door._client._transport

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            await door.connect()

        assert door.connected is True
        assert door._client._transport is transport
        assert len(simulator.protocols) == 1
        assert "already connected" in caplog.text

    async def test_disconnect_after_double_connect_closes_everything(self, door, simulator):
        """One disconnect() leaves the device with no connections at all."""
        await door.connect()  # defensive re-connect

        gone = asyncio.Event()
        simulator._on_disconnect = gone.set

        await door.disconnect()

        async with asyncio.timeout(5.0):
            await gone.wait()
        assert door.connected is False
        assert simulator.protocols == []


# ============================================================================
# Door Status Tests
# ============================================================================


class TestPowerPetDoorStatus:
    """Test door status properties."""

    async def test_initial_status_closed(self, door):
        """Door should start in closed state."""
        assert door.status == DoorStatus.CLOSED
        assert door.is_closed is True
        assert door.is_open is False
        assert door.position == 0

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

    async def test_open_door(self, door, simulator):
        """open() should open the door to the stable HOLDING state."""
        await door.open()

        await wait_for_door_status(door, DoorStatus.HOLDING)

        assert door.is_open

    async def test_open_and_hold(self, door, simulator):
        """open_and_hold() should keep door open."""
        await door.open_and_hold()

        await wait_for_door_status(door, DoorStatus.KEEPUP)

        assert door.status == DoorStatus.KEEPUP

    async def test_close_door(self, door, simulator):
        """close() should close the door."""
        # First open (KEEPUP is the stable held-open state)
        await simulator.open_door(hold=True)
        await wait_for_door_status(door, DoorStatus.KEEPUP)

        # Then close
        await door.close()

        await wait_for_door_status(door, DoorStatus.CLOSED)
        assert door.is_closed

    async def test_toggle_opens_when_closed(self, door, simulator):
        """toggle() should open when door is closed."""
        assert door.is_closed

        await door.toggle()

        await wait_for_door_status(door, DoorStatus.HOLDING)
        assert door.is_open

    async def test_toggle_closes_when_open(self, door, simulator):
        """toggle() should close when door is open."""
        await simulator.open_door(hold=True)
        await wait_for_door_status(door, DoorStatus.KEEPUP)

        assert door.is_open

        await door.toggle()

        await wait_for_door_status(door, DoorStatus.CLOSED)
        assert door.is_closed

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

    async def test_inside_sensor_initial(self, door):
        """Inside sensor should start enabled."""
        assert door.inside_sensor is True

    async def test_disable_inside_sensor(self, door, simulator):
        """set_inside_sensor(False) should disable sensor."""
        await door.set_inside_sensor(False)

        assert door.inside_sensor is False
        assert simulator.state.inside is False

    async def test_enable_inside_sensor(self, door, simulator):
        """set_inside_sensor(True) should enable sensor."""
        simulator.state.inside = False

        await door.set_inside_sensor(True)

        assert door.inside_sensor is True
        assert simulator.state.inside is True

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

    async def test_power_initial(self, door):
        """Power should start on."""
        assert door.power is True

    async def test_power_off(self, door, simulator):
        """set_power(False) should turn off power."""
        await door.set_power(False)

        assert door.power is False
        assert simulator.state.power is False

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

    async def test_auto_initial(self, door):
        """Auto should reflect simulator default (enabled)."""
        assert door.auto is True

    async def test_enable_auto(self, door, simulator):
        """set_auto(True) should enable auto mode."""
        await door.set_auto(True)

        assert door.auto is True
        assert simulator.state.auto is True

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

    async def test_safety_lock(self, door, simulator):
        """Safety lock should be controllable."""
        await door.set_safety_lock(True)
        assert door.safety_lock is True

        await door.set_safety_lock(False)
        assert door.safety_lock is False

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

    async def test_hold_time_get(self, door, simulator):
        """hold_time reflects the device value exactly, in seconds."""
        # Simulator stores seconds; the wire carries centiseconds (1500),
        # and the door converts back to 15.0 seconds.
        simulator.state.hold_time = 15
        await door.refresh_settings()

        assert door.hold_time == 15.0

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

    async def test_battery_initial(self, door):
        """Battery info should have values from simulator."""
        # Simulator defaults to 100% battery
        assert door.battery_percent == 100
        assert door.battery_present is True
        assert door.ac_present is True

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

    async def test_status_change_callback(self, door, simulator):
        """on_status_change receives every transition of the open sequence."""
        statuses = []
        door.on_status_change(statuses.append)

        # Trigger the sensor and wait for the stable open state.
        simulator.trigger_sensor("inside")
        await wait_for_door_status(door, DoorStatus.HOLDING)

        assert statuses == [DoorStatus.RISING, DoorStatus.SLOWING, DoorStatus.HOLDING]

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

    async def test_refresh_status(self, door, simulator):
        """refresh_status should update status from door."""
        # Change simulator state directly
        simulator.state.door_status = DOOR_STATE_HOLDING

        status = await door.refresh_status()

        assert status == DoorStatus.HOLDING
        assert door.status == DoorStatus.HOLDING

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

    async def test_connect_failure_raises_connection_error(self, refused_port):
        """connect() to a dead port raises ConnectionError, not silence (M10)."""
        door = PowerPetDoor("127.0.0.1", port=refused_port, keepalive=0, timeout=0.2, reconnect=0.1)

        with pytest.raises(ConnectionError):
            await door.connect(timeout=0.5)

        assert door.connected is False

    async def test_connect_failure_leaves_no_reconnect_zombie(self, refused_port):
        """After a raised connect(), the client must not keep reconnecting."""
        door = PowerPetDoor("127.0.0.1", port=refused_port, keepalive=0, timeout=0.2, reconnect=0.1)

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

    async def test_disconnect_awaits_an_async_disconnect_handler(self, simulator):
        """door.disconnect() must go through aclose(), not bare shutdown().

        Its docstring promises "nothing outlives this call": the
        on_disconnect coroutine the call itself triggers is awaited. Every
        test passed with the aclose() call reverted to shutdown() (R4-M4).
        """
        port = simulator.server.sockets[0].getsockname()[1]
        door = PowerPetDoor("127.0.0.1", port=port, keepalive=0, timeout=5.0)
        await door.connect()

        finished: list[str] = []

        async def slow_disconnect():
            # One scheduling turn: enough that a shutdown()-only teardown
            # returns before this ever runs.
            await asyncio.sleep(0)
            finished.append("done")

        door._client.add_handlers("app", on_disconnect=slow_disconnect)

        await door.disconnect()

        assert finished == ["done"]
        assert door._client._handler_tasks == set()

    async def test_disconnect_cancels_a_handler_that_overruns_its_timeout(self, simulator):
        """The overrun half of the same promise, bounded by default_timeout."""
        port = simulator.server.sockets[0].getsockname()[1]
        door = PowerPetDoor("127.0.0.1", port=port, keepalive=0, timeout=0.05)
        await door.connect()
        started = asyncio.Event()
        cancelled: list[str] = []

        async def wedged_disconnect():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append("cancelled")
                raise

        door._client.add_handlers("app", on_disconnect=wedged_disconnect)

        # default_timeout is cfg_timeout * MAX_FAILED_MSG; the outer bound
        # here is what proves disconnect() does not wait forever.
        async with asyncio.timeout(door.default_timeout * 4):
            await door.disconnect()

        assert started.is_set()
        assert cancelled == ["cancelled"]
        assert door._client._handler_tasks == set()

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

    async def test_refresh_schedules_empty(self, door, simulator):
        """No schedules on the device returns [] and clears the cache."""
        door._schedules = [Schedule(index=5)]

        schedules = await door.refresh_schedules()

        assert schedules == []
        assert door.schedules == []

    async def test_get_schedule_by_index(self, door, simulator):
        """get_schedule fetches a single schedule."""
        simulator.state.schedules[1] = _sim_schedule(1, [1, 1, 1, 1, 1, 1, 1])

        schedule = await door.get_schedule(1)

        assert schedule.index == 1
        assert schedule.inside is True
        assert schedule.start.hour == 7

    async def test_get_schedule_unknown_index_raises(self, door, simulator):
        """get_schedule on a missing index raises CommandError."""
        from powerpetdoor import CommandError

        with pytest.raises(CommandError) as excinfo:
            await door.get_schedule(99)

        assert excinfo.value.reason == "Schedule not found"

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

    async def test_delete_schedule_removes(self, door, simulator):
        """delete_schedule removes it from the device and the cache."""
        simulator.state.schedules[0] = _sim_schedule(0, [1] * 7)
        await door.refresh_schedules()
        assert [s.index for s in door.schedules] == [0]

        await door.delete_schedule(0)

        assert 0 not in simulator.state.schedules
        assert door.schedules == []

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

    async def test_custom_cached_settings_drive_the_wire_payload(self, door):
        """Every field of a custom NotificationSettings reaches the wire.

        This replaces the old dataclass read-back test: it pins the same
        five fields, but through the merge that actually uses them.
        """
        from powerpetdoor.const import (
            FIELD_LOW_BATTERY_NOTIFICATIONS,
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
        )

        door._notifications = NotificationSettings(
            inside_on=True, outside_off=True, low_battery=True
        )
        sent = {}

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            sent.update(kwargs)
            future = asyncio.get_running_loop().create_future()
            future.set_result({})
            return future

        door._client.send_message = fake_send

        await door.set_notifications()  # no overrides: pure cache passthrough

        assert sent == {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "1",
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: "0",
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: "0",
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: "1",
            FIELD_LOW_BATTERY_NOTIFICATIONS: "1",
        }
        assert door.notifications is door._notifications


# ============================================================================
# Latency / Version / Position Tests (H10)
# ============================================================================


class TestDoorLatency:
    """Latency tracking from ping/pong (H10)."""

    async def test_latency_set_by_ping(self):
        """_on_ping converts milliseconds to seconds."""
        door = PowerPetDoor("127.0.0.1")
        assert door.latency is None

        door._on_ping(50)

        assert door.latency == 0.05

    async def test_latency_cleared_on_disconnect(self):
        """Disconnection resets latency to None."""
        door = PowerPetDoor("127.0.0.1")
        door._on_ping(50)

        await door._on_disconnect()

        assert door.latency is None


class TestVersionFormatting:
    """firmware_version / hardware_version string formatting (H10)."""

    async def test_firmware_version_populated(self):
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"fw_maj": 1, "fw_min": 2, "fw_pat": 3}
        assert door.firmware_version == "1.2.3"

    async def test_firmware_version_empty(self):
        door = PowerPetDoor("127.0.0.1")
        assert door.firmware_version == ""

    async def test_firmware_version_partial_defaults_zero(self):
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"fw_maj": 2}
        assert door.firmware_version == "2.0.0"

    async def test_hardware_version_populated(self):
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"ver": "1", "rev": "2"}
        assert door.hardware_version == "1 rev 2"

    async def test_hardware_version_empty_dict(self):
        door = PowerPetDoor("127.0.0.1")
        assert door.hardware_version == ""

    async def test_hardware_version_no_ver_fields(self):
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"fw_maj": 1}
        assert door.hardware_version == ""


class TestToggleWhileClosing:
    """toggle() is a no-op while the door is closing (H10)."""

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


# ============================================================================
# Device-Backed Property Tests (H10)
# ============================================================================


class TestDoorDeviceProperties:
    """Config/stats/notification surfaces against the simulator."""

    async def test_pet_proximity_keep_open_roundtrip(self, door, simulator):
        """Keep-open maps to the inverted cmd_lockout protocol flag."""
        # Simulator default: cmd_lockout False -> keep-open enabled.
        assert door.pet_proximity_keep_open is True

        await door.set_pet_proximity_keep_open(False)
        assert simulator.state.cmd_lockout is True
        assert door.pet_proximity_keep_open is False

        await door.set_pet_proximity_keep_open(True)
        assert simulator.state.cmd_lockout is False
        assert door.pet_proximity_keep_open is True

    async def test_timezone_set_and_cached(self, door, simulator):
        """set_timezone stores on the device and updates the cached property."""
        await door.set_timezone("PST8PDT,M3.2.0,M11.1.0")

        assert simulator.state.timezone == "PST8PDT,M3.2.0,M11.1.0"
        assert door.timezone == "PST8PDT,M3.2.0,M11.1.0"

    async def test_stats_properties_after_refresh(self, door, simulator):
        """refresh_stats updates both counters from the device."""
        simulator.state.total_open_cycles = 11
        simulator.state.total_auto_retracts = 3

        await door.refresh_stats()

        assert door.total_open_cycles == 11
        assert door.total_auto_retracts == 3

    async def test_notifications_reflect_device_state(self, door, simulator):
        """Notification settings are cached from the device on refresh."""
        simulator.state.sensor_on_indoor = True
        simulator.state.low_battery = True
        simulator.state.sensor_off_outdoor = False

        await door.refresh_settings()

        notifications = door.notifications
        assert isinstance(notifications, NotificationSettings)
        assert notifications.inside_on is True
        assert notifications.low_battery is True
        assert notifications.outside_off is False

    async def test_hardware_info_returns_copy(self, door):
        """Mutating the returned hardware info must not touch the cache."""
        info = door.hardware_info
        assert info  # Populated by the initial refresh

        info.clear()

        assert door.hardware_info  # Internal cache untouched
        assert door.firmware_version != ""


# ============================================================================
# Unit-Level Edge Tests (fake client sends)
# ============================================================================


class TestDoorUnitEdges:
    """Defensive branches driven with controlled client responses."""

    async def test_delete_schedule_without_echo_prunes_cache(self):
        """Firmware that omits the deleted index still prunes the cache."""
        door = PowerPetDoor("127.0.0.1")
        door._schedules = [Schedule(index=2, inside=True)]
        snapshots = []
        door.on_schedule_change(lambda schedules: snapshots.append([s.index for s in schedules]))

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)  # Ack without the echoed index
            return future

        door._client.send_message = fake_send

        await door.delete_schedule(2)

        assert door.schedules == []
        assert snapshots == [[]]

    async def test_refresh_schedules_survives_per_index_failures(self):
        """Timeouts, errors, and empty payloads are skipped, not fatal."""
        door = PowerPetDoor("127.0.0.1")

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            future = asyncio.get_running_loop().create_future()
            if cmd == CMD_GET_SCHEDULE_LIST:
                future.set_result([0, 1, 2])
            elif kwargs["index"] == 0:
                pass  # Never resolves -> per-index TimeoutError branch
            elif kwargs["index"] == 1:
                future.set_exception(RuntimeError("device glitch"))
            else:
                future.set_result({})  # Falsy payload -> skipped
            return future

        door._client.send_message = fake_send

        schedules = await door.refresh_schedules(timeout=0.05)

        assert schedules == []
        assert door.schedules == []

    async def test_refresh_names_each_failed_step_in_the_log(self, caplog):
        """A dead refresh step is reported at the door layer, not swallowed (L5)."""
        door = PowerPetDoor("127.0.0.1")

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            future = asyncio.get_running_loop().create_future()
            future.set_exception(CommandError(cmd, "NAK"))
            return future

        door._client.send_message = fake_send

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            await door.refresh()

        # refresh_settings() reports its own two sub-steps and returns
        # normally, so "settings" itself is not a failed step here.
        for step in ("status", "battery", "stats", "hardware_info"):
            assert f"Refresh step {step} failed" in caplog.text
        assert "Refresh step settings failed" not in caplog.text

    async def test_refresh_settings_names_each_failed_step_in_the_log(self, caplog):
        """Both settings sub-steps are named when they fail (L5)."""
        door = PowerPetDoor("127.0.0.1")

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            future = asyncio.get_running_loop().create_future()
            future.set_exception(CommandError(cmd, "NAK"))
            return future

        door._client.send_message = fake_send

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            await door.refresh_settings()

        assert f"Refresh step {CMD_GET_SETTINGS} failed" in caplog.text
        assert f"Refresh step {CMD_GET_NOTIFICATIONS} failed" in caplog.text

    async def test_refresh_hardware_info_keeps_cache_on_empty_result(self):
        """An empty hw-info payload leaves the cached info in place."""
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"fw_maj": 9}

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            future = asyncio.get_running_loop().create_future()
            future.set_result({})
            return future

        door._client.send_message = fake_send

        result = await door.refresh_hardware_info()

        assert result == {"fw_maj": 9}
        assert door.firmware_version == "9.0.0"


# ============================================================================
# Callback Registration and Isolation Tests (H10)
# ============================================================================


class TestDoorCallbackRegistration:
    """on_* registration methods fire and isolate exceptions."""

    async def test_settings_callbacks_fire_and_isolate(self):
        door = PowerPetDoor("127.0.0.1")
        calls = []

        def bad_callback(settings):
            calls.append("bad")
            raise RuntimeError("callback bug")

        door.on_settings_change(bad_callback)
        door.on_settings_change(lambda settings: calls.append(("good", settings)))

        payload = {FIELD_POWER: "1"}
        door._on_settings(payload)

        assert calls == ["bad", ("good", payload)]
        assert door.power is True

    async def test_connect_callbacks_fire_and_isolate(self):
        door = PowerPetDoor("127.0.0.1")
        calls = []

        def bad_callback():
            calls.append("bad")
            raise RuntimeError("callback bug")

        door.on_connect(bad_callback)
        door.on_connect(lambda: calls.append("good"))

        await door._on_connect()

        assert calls == ["bad", "good"]
        assert door._connected_event.is_set()

    async def test_disconnect_callbacks_fire_and_isolate(self):
        door = PowerPetDoor("127.0.0.1")
        calls = []

        def bad_callback():
            calls.append("bad")
            raise RuntimeError("callback bug")

        door.on_disconnect(bad_callback)
        door.on_disconnect(lambda: calls.append("good"))

        await door._on_disconnect()

        assert calls == ["bad", "good"]
        assert not door._connected_event.is_set()

    async def test_reconnect_refresh_failure_is_contained(self):
        """A failing post-reconnect refresh() still notifies connect callbacks."""
        door = PowerPetDoor("127.0.0.1")
        door._initialized = True
        refresh_calls = []

        async def failing_refresh(**kwargs):
            refresh_calls.append(1)
            raise RuntimeError("device gone")

        door.refresh = failing_refresh
        connected = []
        door.on_connect(lambda: connected.append("connect"))

        await door._on_connect()

        assert refresh_calls == [1]
        assert connected == ["connect"]


# ============================================================================
# Listener None-Value Guard Tests (D4)
# ============================================================================


class TestListenerNoneGuards:
    """An unrecognized wire value (None) must never clobber cached state."""

    @pytest.mark.parametrize(
        ("method", "attr"),
        [
            ("_on_power_update", "_power"),
            ("_on_inside_update", "_inside_sensor"),
            ("_on_outside_update", "_outside_sensor"),
            ("_on_auto_update", "_auto"),
            ("_on_safety_lock_update", "_safety_lock"),
            ("_on_autoretract_update", "_autoretract"),
            ("_on_cmd_lockout_update", "_pet_proximity_keep_open"),
        ],
    )
    async def test_sensor_none_value_leaves_cache(self, method, attr):
        door = PowerPetDoor("127.0.0.1")
        before = getattr(door, attr)

        getattr(door, method)("field", None)

        assert getattr(door, attr) is before

    @pytest.mark.parametrize(
        ("method", "attr"),
        [
            ("_on_notify_inside_on", "inside_on"),
            ("_on_notify_inside_off", "inside_off"),
            ("_on_notify_outside_on", "outside_on"),
            ("_on_notify_outside_off", "outside_off"),
            ("_on_notify_low_battery", "low_battery"),
        ],
    )
    async def test_notification_none_value_leaves_cache(self, method, attr):
        door = PowerPetDoor("127.0.0.1")
        assert getattr(door._notifications, attr) is False

        getattr(door, method)("field", None)
        assert getattr(door._notifications, attr) is False

        getattr(door, method)("field", True)
        assert getattr(door._notifications, attr) is True


# ============================================================================
# Schedule Cache Maintenance Tests (H10)
# ============================================================================


class TestScheduleCacheMaintenance:
    """_on_schedule_update/_delete maintain the cache and notify callbacks."""

    async def test_schedule_update_replaces_existing_entry(self):
        door = PowerPetDoor("127.0.0.1")
        door._on_schedule_update(
            Schedule(index=1, inside=True, start=ScheduleTime(hour=6, minute=0)).to_dict()
        )
        assert [s.index for s in door.schedules] == [1]

        door._on_schedule_update(
            Schedule(index=1, inside=True, start=ScheduleTime(hour=7, minute=30)).to_dict()
        )

        assert len(door.schedules) == 1
        assert door.schedules[0].start.hour == 7
        assert door.schedules[0].start.minute == 30

    @pytest.mark.parametrize(
        "payload",
        [
            {"daysOfWeek": None},
            {"inside": True, "in_start_time": 5},
            {"inside": True, "in_start_time": None},
            {"index": "x"},
            ["not", "a", "schedule"],
        ],
        ids=["null-days", "int-time", "null-time", "bad-index", "not-an-object"],
    )
    async def test_malformed_schedule_update_is_logged_and_dropped(self, payload, caplog):
        """A bad device payload must not silently freeze the cache (R4-M1).

        The client isolates listener exceptions, so the TypeError/
        AttributeError this used to raise was swallowed: the cached
        schedule list stopped tracking the device with nothing in the log
        to say the update had been lost.
        """
        door = PowerPetDoor("127.0.0.1")
        door._on_schedule_update(Schedule(index=1, inside=True).to_dict())
        assert [s.index for s in door.schedules] == [1]

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            door._on_schedule_update(payload)

        assert "Ignoring malformed schedule update from device" in caplog.text
        assert [s.index for s in door.schedules] == [1]

    async def test_schedule_callback_exception_isolated(self):
        door = PowerPetDoor("127.0.0.1")
        calls = []

        def bad_callback(schedules):
            calls.append("bad")
            raise RuntimeError("callback bug")

        door.on_schedule_change(bad_callback)
        door.on_schedule_change(
            lambda schedules: calls.append(("good", [s.index for s in schedules]))
        )

        door._on_schedule_update(Schedule(index=0, inside=True).to_dict())

        assert calls == ["bad", ("good", [0])]
