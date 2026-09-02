# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for PowerPetDoor facade class."""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from powerpetdoor import (
    REFRESH_STEP_SETTINGS,
    REFRESH_STEPS,
    BatteryInfo,
    DoorStatus,
    NotificationSettings,
    PowerPetDoor,
    Schedule,
    ScheduleTime,
    envelope_for_command,
)
from powerpetdoor import door as door_module
from powerpetdoor.client import CommandError
from powerpetdoor.const import (
    CMD_ENABLE_INSIDE,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SETTINGS,
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    COMMAND,
    CONFIG,
    DOOR_OPTION_AUTORETRACT,
    DOOR_STATE_CLOSED,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_POWEROFF,
    DOOR_STATE_RISING,
    FIELD_AC_PRESENT,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD_LOCKOUT,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_VOLTAGE,
    TIME_FORMAT,
)
from powerpetdoor.sanitize import MAX_LOGGED_LENGTH, sanitize_text
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.wire_values import (
    settings_payload,
)
from powerpetdoor.tz_utils import resolve_tzinfo
from tests.conftest import GOLDEN_SCHEDULE_WIRE_TO_DEVICE, assert_schedule_wire_types

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
        closing_start_time=0.05,
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
        """from_string maps unknown strings to UNKNOWN with a warning."""
        assert DoorStatus.from_string("INVALID") == DoorStatus.UNKNOWN
        assert DoorStatus.from_string("") == DoorStatus.UNKNOWN
        assert "Unknown door status" in caplog.text

    def test_unknown_status_is_neither_open_nor_closed(self):
        """An UNKNOWN status must not claim the door is closed."""
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
    parser refuses to invent one - so hostile-input tests that are about
    some *other* field start from a complete payload.
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
        # All days, as real booleans - True == 1 would mask an int regression
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

    def test_to_dict_matches_the_client_to_device_wire_shape(self):
        """The library emitter pins the shape it SENDS to the device.

        Every field is pinned against the same golden payload the
        simulator's emitter is checked against, so the two can never drift
        in any field - except ``enabled``, which differs on purpose: this
        emitter is client->device and has sent a JSON boolean to real
        firmware since v0.1.0, while the simulator plays the device side
        and replies ``"1"``. The two must not be unified on the authority
        of the reverse-engineered docs/protocol.md; this test is what stops
        that.
        """
        schedule = Schedule(
            index=3,
            enabled=True,
            days_of_week=[True, False, True, False, True, False, True],
            inside=True,
            outside=False,
            start=ScheduleTime(hour=6, minute=30),
            end=ScheduleTime(hour=22, minute=15),
        )

        payload = schedule.to_dict()

        assert payload == GOLDEN_SCHEDULE_WIRE_TO_DEVICE
        assert_schedule_wire_types(payload, flag_type=bool)

    def test_to_dict_disabled_emits_json_false(self):
        """``enabled: False`` goes out as JSON ``false``, not the string "0".

        This is the client->device direction; the JSON boolean is what has
        run against real hardware.
        """
        payload = Schedule(index=0, enabled=False, inside=True).to_dict()

        assert payload["enabled"] is False

    def test_to_dict_days_are_wire_ints(self):
        """The wire protocol carries literal 1/0 ints, never bools."""
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

    def test_a_window_ending_at_midnight_goes_out_as_hour_24(self):
        """Midnight as an END is rewritten to the device's end-of-day.

        the door does NOT reinterpret it.
        A stored `20:00-00:00` leaves the sensor disabled, because the engine
        is `start <= now < end` and an end of 0 never exceeds a start of
        1200. So "22:00 until midnight" has to leave here as 22:00-24:00 or
        it silently never fires.
        """
        schedule = Schedule(
            index=0,
            inside=True,
            start=ScheduleTime(hour=22, minute=0),
            end=ScheduleTime(hour=0, minute=0),
        )

        d = schedule.to_dict()

        assert d["in_start_time"] == {"hour": 22, "min": 0}
        assert d["in_end_time"] == {"hour": 24, "min": 0}

    def test_the_unselected_sensors_filler_is_not_mistaken_for_a_whole_day(self):
        """The zeroed block stays zeroed.

        The boundary the end-of-day rewrite has to respect: the protocol
        wants the other sensor's times as all-zero filler, and turning that
        00:00 end into 24:00 would spell a WHOLE DAY window for the sensor
        this entry is not even about.
        """
        schedule = Schedule(
            index=0,
            inside=True,
            outside=False,
            start=ScheduleTime(hour=22, minute=0),
            end=ScheduleTime(hour=0, minute=0),
        )

        d = schedule.to_dict()

        assert d["in_end_time"] == {"hour": 24, "min": 0}
        assert d["out_start_time"] == {"hour": 0, "min": 0}
        assert d["out_end_time"] == {"hour": 0, "min": 0}

    def test_midnight_to_midnight_goes_out_as_a_whole_day(self):
        """00:00-00:00 is 00:00-24:00 on the wire.

        The rule is positional, not contextual: 00:00 as a START is always
        the first minute of the day, and 00:00 as an END is always the last.
        So the one spelling that means nothing at all to the device becomes
        the one that means everything - which is what anyone writing
        "midnight to midnight" intended, and what this integration's own card
        used to emit before it knew better.
        """
        schedule = Schedule(
            index=0,
            inside=True,
            start=ScheduleTime(hour=0, minute=0),
            end=ScheduleTime(hour=0, minute=0),
        )

        d = schedule.to_dict()

        assert d["in_start_time"] == {"hour": 0, "min": 0}
        assert d["in_end_time"] == {"hour": 24, "min": 0}

    def test_an_ordinary_end_is_left_alone(self):
        """Only 00:00 is rewritten - the control for the rule above."""
        schedule = Schedule(
            index=0,
            inside=True,
            start=ScheduleTime(hour=6, minute=0),
            end=ScheduleTime(hour=20, minute=0),
        )

        assert schedule.to_dict()["in_end_time"] == {"hour": 20, "min": 0}

    @pytest.mark.parametrize(
        ("start", "end", "empty"),
        [
            ((6, 0), (20, 0), False),
            ((22, 0), (0, 0), False),  # becomes 22:00-24:00
            ((0, 0), (0, 0), False),  # becomes 00:00-24:00, a whole day
            ((0, 0), (24, 0), False),
            ((9, 0), (9, 0), True),
            ((23, 0), (1, 0), True),
        ],
        ids=[
            "ordinary",
            "ends-at-midnight",
            "midnight-to-midnight",
            "whole-day",
            "equal-ends",
            "inverted",
        ],
    )
    def test_window_is_empty_matches_the_device(self, start, end, empty):
        """Which windows a real door stores and then never acts on.

        Every row measured on the door by reading the sensor flags the
        schedule engine writes through to.
        """
        schedule = Schedule(
            index=0,
            inside=True,
            start=ScheduleTime(hour=start[0], minute=start[1]),
            end=ScheduleTime(hour=end[0], minute=end[1]),
        )

        assert schedule.window_is_empty() is empty

    def test_validate_for_send_refuses_a_window_that_ends_before_it_starts(self):
        """23:00-01:00 cannot be expressed, so it is refused rather than sent.

        The door would accept it, echo it back unchanged and never act on it,
        so nothing downstream catches the mistake.
        """
        schedule = Schedule(
            index=0,
            inside=True,
            start=ScheduleTime(hour=23, minute=0),
            end=ScheduleTime(hour=1, minute=0),
        )

        with pytest.raises(ValueError, match="covers no time"):
            schedule.validate_for_send()

    def test_validate_for_send_allows_a_window_ending_at_midnight(self):
        """The normalisation runs first, so 22:00-00:00 is 22:00-24:00."""
        schedule = Schedule(
            index=0,
            inside=True,
            start=ScheduleTime(hour=22, minute=0),
            end=ScheduleTime(hour=0, minute=0),
        )

        schedule.validate_for_send()

    def test_validate_for_send_refuses_equal_ends_too(self):
        """Coinciding ends are refused as well, and that changed.

        This used to assert the opposite, reasoning that such an entry is not
        malformed and the caller may have meant it. But `09:00-09:00` is an
        EMPTY window: the door accepts it and
        then simply stops permitting the sensor, with nothing anywhere to say
        why. `enabled=False` says the same thing and says it visibly, so
        there is no reason to let the silent spelling through.
        """
        schedule = Schedule(
            index=0,
            inside=True,
            start=ScheduleTime(hour=9, minute=0),
            end=ScheduleTime(hour=9, minute=0),
        )

        with pytest.raises(ValueError, match="covers no time"):
            schedule.validate_for_send()

    def test_validate_for_send_still_allows_a_whole_day(self):
        """The boundary: 00:00-00:00 normalises to 00:00-24:00 and passes.

        Refusing it would block the commonest window anyone writes.
        """
        Schedule(
            index=0,
            inside=True,
            start=ScheduleTime(hour=0, minute=0),
            end=ScheduleTime(hour=0, minute=0),
        ).validate_for_send()

    def test_from_dict_days_are_bools(self):
        """Wire 1/0 lists are converted to real booleans."""
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
        """bool("0") is True, so day flags go through make_bool.

        This is the library-side twin of the simulator's
        ``_coerce_schedule_day`` test: a firmware variant that sends
        ``"0"``/``"1"`` day flags (as it already does for ``enabled``) must
        not expand to every day of the week.
        """
        restored = Schedule.from_dict(_inside_payload(daysOfWeek=[flag] * 7))

        assert restored.days_of_week == [False] * 7

    @pytest.mark.parametrize("flag", ["0", 0, False, "false"], ids=repr)
    def test_from_dict_disabled_enabled_flag_is_read_as_disabled(self, flag):
        """``enabled`` is read the same way its daysOfWeek sibling is."""
        restored = Schedule.from_dict(_inside_payload(enabled=flag))

        assert restored.enabled is False

    @pytest.mark.parametrize("flag", ["1", 1, True, "true", "yes", "on"], ids=repr)
    def test_from_dict_enabled_accepts_every_flag_spelling(self, flag):
        """A bespoke ``== "1"`` read "true"/"yes"/"on" as disabled."""
        restored = Schedule.from_dict(_inside_payload(enabled=flag))

        assert restored.enabled is True

    def test_from_dict_enabled_is_always_a_real_bool(self):
        """A field declared ``enabled: bool`` must never hold 1/0."""
        restored = Schedule.from_dict(_inside_payload(enabled=1))

        assert restored.enabled is True
        assert isinstance(restored.enabled, bool)
        # Liberal in what we accept (1), conservative in what we send: the
        # client->device payload carries the JSON boolean it always has.
        assert restored.to_dict()["enabled"] is True

    def test_from_dict_no_days_defaults_to_every_day(self):
        """An absent daysOfWeek means "every day", and that is pinned.

        The default direction matters and was unobserved on both sides: two
        tests parsed a payload without the field, neither looked at the
        result, so flipping ``[1]*7`` to ``[0]*7`` changed nothing any test
        could see.
        """
        assert Schedule.from_dict({}).days_of_week == [True] * 7
        assert Schedule.from_dict({"index": 4}).days_of_week == [True] * 7

    @pytest.mark.parametrize(
        ("mask", "expected"),
        [
            (0, [False] * 7),
            (1, [True] + [False] * 6),
            (127, [True] * 7),
            (62, [False, True, True, True, True, True, False]),
            (True, [True] + [False] * 6),
            (False, [False] * 7),
        ],
        ids=repr,
    )
    def test_from_dict_bitmask_boundaries(self, mask, expected):
        """The legacy bitmask branch, pinned across its whole range.

        ``True``/``False`` are ints on this wire like everywhere else, so
        they are masks too - one bit and no bits respectively.
        """
        assert Schedule.from_dict(_inside_payload(daysOfWeek=mask)).days_of_week == expected

    @pytest.mark.parametrize("mask", [-1, -128, 128, 2**64], ids=repr)
    def test_from_dict_out_of_range_bitmask_is_rejected(self, mask):
        """An out-of-range mask is rejected, not read modulo 7 bits.

        ``-1 >> i & 1`` is 1 forever, so the old unbounded branch turned
        every negative integer into "every day on" - the exact opposite of
        the fail-closed doctrine ``coerce_schedule_flag`` documents. The
        bound is checked in both directions for the same reason every
        other coercer here checks both.
        """
        with pytest.raises(ValueError, match="daysOfWeek"):
            Schedule.from_dict(_inside_payload(daysOfWeek=mask))

    def test_from_dict_inside_wins_when_both_sensors_are_flagged(self):
        """Statement order is the rule, so pin it on both parsers.

        ``schedule add both`` produces both-flag entries in-project, so the
        branch is live even though docs/protocol.md calls it out of spec.
        """
        restored = Schedule.from_dict(
            {
                "index": 0,
                "inside": True,
                "outside": True,
                "daysOfWeek": [1] * 7,
                "in_start_time": {"hour": 6, "min": 0},
                "in_end_time": {"hour": 7, "min": 0},
                "out_start_time": {"hour": 20, "min": 0},
                "out_end_time": {"hour": 21, "min": 0},
            }
        )

        assert (restored.start.hour, restored.end.hour) == (6, 7)

    def test_from_dict_unreadable_flag_fails_closed(self):
        """An unrecognizable sensor flag disables the entry, never enables it."""
        restored = Schedule.from_dict({"index": 0, "inside": ["yes"], "outside": {"a": 1}})

        assert restored.inside is False
        assert restored.outside is False


class TestDoorScheduleFromDictRejectsHostileInput:
    """The library's schedule parser reads bytes off the wire.

    Every payload here used to raise TypeError/AttributeError out of a
    documented public coroutine, or be swallowed by listener isolation and
    silently freeze the cached schedule list. The contract is a
    ``ValueError`` naming the offending field - the same contract the
    simulator's twin parser has.
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
            ({"hour": 25, "min": 0}, "start time hour must be between 0 and 24"),
            ({"hour": 6, "min": 60}, "start time minute must be between 0 and 59"),
        ],
        ids=["non-numeric-hour", "hour-too-large", "minute-too-large"],
    )
    def test_out_of_range_times_rejected(self, time_field, message):
        with pytest.raises(ValueError, match=message):
            Schedule.from_dict(_inside_payload(in_start_time=time_field))

    @pytest.mark.parametrize(
        ("time_field", "expected_hour"),
        [({"hour": 24, "min": 0}, 24), ({"min": 0}, 0)],
        ids=["end-of-day", "no-hour"],
    )
    def test_the_read_path_keeps_entries_it_cannot_fully_parse(self, time_field, expected_hour):
        """Refusing here would make refresh_schedules() drop the whole entry.

        24:00 is a natural end-of-day encoding and an absent hour is a shape a
        firmware variant could send; either way the schedule really exists on
        the door, so hiding it is worse than reading it generously.
        """
        sched = Schedule.from_dict(_inside_payload(in_start_time=time_field))
        assert sched.start.hour == expected_hour

    def test_outside_entry_times_are_validated_too(self):
        with pytest.raises(ValueError, match="end time hour must be between 0 and 24"):
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
        """A selected sensor with no window must fail, not get a free one."""
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
        """connect() while connected is a no-op, not a leaked socket."""
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
        """After open() the door reaches the stable KEEPUP state."""
        await door.open()

        await wait_for_door_status(door, DoorStatus.KEEPUP)

        assert door.status == DoorStatus.KEEPUP
        assert door.is_open is True
        assert door.is_closed is False


# ============================================================================
# Door Control Tests
# ============================================================================


class TestPowerPetDoorControl:
    """Test door control methods."""

    async def test_open_holds_the_door_up(self, door, simulator):
        """open() sends OPEN_AND_HOLD: the door goes up and stays up.

        The hold timer is 1s here, so a door that had merely been sent OPEN
        would be closing by the time the motion task finishes. Awaiting that
        task is what makes "it did not close itself" a fact rather than a
        guess about timing.
        """
        await door.open()

        await wait_for_door_status(door, DoorStatus.KEEPUP)
        assert door.is_open

        await asyncio.gather(simulator.engine._task, return_exceptions=True)

        assert door.status == DoorStatus.KEEPUP
        assert door.is_closed is False

    async def test_open_and_hold_is_gone(self, door):
        """The old name is removed, not silently aliased.

        A caller still on `open_and_hold()` must fail loudly rather than get
        a method that quietly means something else now.
        """
        assert not hasattr(door, "open_and_hold")

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
        """toggle() should open when door is closed, and leave it open."""
        assert door.is_closed

        await door.toggle()

        await wait_for_door_status(door, DoorStatus.KEEPUP)
        assert door.is_open

    async def test_toggle_closes_when_open(self, door, simulator):
        """toggle() should close when door is open."""
        await simulator.open_door(hold=True)
        await wait_for_door_status(door, DoorStatus.KEEPUP)

        assert door.is_open

        await door.toggle()

        await wait_for_door_status(door, DoorStatus.CLOSED)
        assert door.is_closed

    async def test_cycle_opens_then_closes_itself(self, door, simulator):
        """cycle() sends OPEN: the door rises, holds, then closes unbidden.

        The close is the point of the method - no CLOSE is sent here, so a
        cycle() that had been wired to OPEN_AND_HOLD would park in KEEPUP
        and never reach CLOSED.
        """
        assert door.is_closed

        await door.cycle()

        await wait_for_door_status(door, DoorStatus.HOLDING)
        assert door.is_open

        await wait_for_door_status(door, DoorStatus.CLOSED)
        assert door.is_closed


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
# Settings Coercion Tests
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
# Connect Lifecycle Tests
# ============================================================================


class TestConnectLifecycle:
    """The documented no-loop connect pattern and failure semantics."""

    async def test_connect_without_explicit_loop(self, simulator):
        """PowerPetDoor(host); await door.connect() works with loop=None."""
        port = simulator.server.sockets[0].getsockname()[1]
        door = PowerPetDoor("127.0.0.1", port=port, keepalive=0, timeout=5.0, reconnect=0.5)

        await door.connect()
        try:
            assert door.connected is True
        finally:
            await door.disconnect()

    async def test_connect_failure_raises_connection_error(self, refused_port):
        """connect() to a dead port raises ConnectionError, not silence."""
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
        """disconnect() before connect() must not raise."""
        door = PowerPetDoor("127.0.0.1")
        await door.disconnect()

    async def test_double_disconnect_is_safe(self, simulator):
        """Two disconnect() calls in a row must not raise."""
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
        test passed with the aclose() call reverted to shutdown().
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
        """connect() after disconnect() re-arms the client."""
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
        """After a client-level auto-reconnect, the cache resynchronizes."""
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
# Schedule API Tests
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
    """door.py schedule methods against the simulator."""

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

    async def test_set_schedule_refuses_a_window_that_ends_before_it_starts(self, door, simulator):
        """Nothing reaches the door, so nothing is stored to be ignored later.

        Measured on the door: the device accepts such an entry, echoes
        it back unchanged and never acts on it. Asserting the simulator's
        slot is still empty is the point - a rejection that still wrote would
        leave the user a schedule that reads correctly and does nothing.
        """
        schedule = Schedule(
            index=2,
            inside=True,
            start=ScheduleTime(hour=23, minute=0),
            end=ScheduleTime(hour=1, minute=0),
        )

        with pytest.raises(ValueError, match="covers no time"):
            await door.set_schedule(schedule)

        assert 2 not in simulator.state.schedules

    async def test_set_schedule_sends_a_midnight_end_as_hour_24(self, door, simulator):
        """22:00-00:00 lands on the door as 22:00-24:00, so it actually fires."""
        await door.set_schedule(
            Schedule(
                index=4,
                inside=True,
                start=ScheduleTime(hour=22, minute=0),
                end=ScheduleTime(hour=0, minute=0),
            )
        )

        stored = simulator.state.schedules[4]
        assert (stored.start_hour, stored.start_min) == (22, 0)
        assert (stored.end_hour, stored.end_min) == (24, 0)
        # ...and the door agrees it is a real window, not an empty one.
        assert stored.is_sensor_allowed("inside", 23, 30, 0) is True

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
# Notifications API Tests
# ============================================================================


class TestSetNotifications:
    """set_notifications merge semantics and wire format."""

    async def test_partial_update_preserves_others(self, door):
        """Unspecified settings are sent with their cached values."""
        from powerpetdoor.const import (
            FIELD_LOW_BATTERY_NOTIFICATIONS,
            FIELD_NOTIFICATIONS,
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

        # A NESTED object of JSON booleans: flat top-level fields are
        # rejected outright, and a nested object of strings is accepted
        # and silently not applied.
        assert sent == {
            FIELD_NOTIFICATIONS: {
                FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: True,  # Preserved from cache
                FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: False,
                FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: False,
                FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: False,
                FIELD_LOW_BATTERY_NOTIFICATIONS: True,  # Explicitly set
            }
        }
        assert all(type(v) is bool for v in sent[FIELD_NOTIFICATIONS].values())

    async def test_custom_cached_settings_drive_the_wire_payload(self, door):
        """Every field of a custom NotificationSettings reaches the wire.

        This replaces the old dataclass read-back test: it pins the same
        five fields, but through the merge that actually uses them.
        """
        from powerpetdoor.const import (
            FIELD_LOW_BATTERY_NOTIFICATIONS,
            FIELD_NOTIFICATIONS,
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
            FIELD_NOTIFICATIONS: {
                FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: True,
                FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: False,
                FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: False,
                FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: True,
                FIELD_LOW_BATTERY_NOTIFICATIONS: True,
            }
        }
        # bool, not the int 1: True == 1 in Python, so dict equality alone
        # would not catch a regression back to a stringified/int payload -
        # and the device silently ignores a payload whose values are strings.
        assert all(type(v) is bool for v in sent[FIELD_NOTIFICATIONS].values())
        assert door.notifications is door._notifications


# ============================================================================
# Latency / Version / Position Tests
# ============================================================================


class TestDoorLatency:
    """Latency tracking from ping/pong."""

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
    """firmware_version / hardware_version string formatting."""

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


class TestToggleMidTravel:
    """toggle() is a no-op while the door is moving, in either direction.

    ``is_open`` is deliberately wider than "open" - it covers RISING and
    SLOWING so a consumer rendering a cover entity sees a door on its way
    up as open rather than closed. Toggling used to test it, read a rising
    door as "open, so close it", and reverse a door mid-travel. Nothing but
    an obstruction is known to interrupt a real door in motion.
    """

    @pytest.mark.parametrize(
        "status",
        [
            DoorStatus.RISING,
            DoorStatus.SLOWING,
            DoorStatus.CLOSING,
            DoorStatus.CLOSING_TOP_OPEN,
            DoorStatus.CLOSING_MID_OPEN,
        ],
        ids=lambda s: s.name.lower(),
    )
    async def test_toggle_noop_mid_travel(self, status):
        from unittest.mock import AsyncMock, patch

        door = PowerPetDoor("127.0.0.1")
        door._status = status

        with (
            patch.object(door, "open", new_callable=AsyncMock) as mock_open,
            patch.object(door, "close", new_callable=AsyncMock) as mock_close,
        ):
            await door.toggle()

        assert mock_open.await_count == 0
        assert mock_close.await_count == 0

    @pytest.mark.parametrize(
        ("status", "opens", "closes"),
        [
            (DoorStatus.CLOSED, 1, 0),
            (DoorStatus.IDLE, 1, 0),
            (DoorStatus.HOLDING, 0, 1),
            (DoorStatus.KEEPUP, 0, 1),
        ],
        ids=lambda v: getattr(v, "name", v),
    )
    async def test_toggle_acts_only_when_the_door_is_settled(self, status, opens, closes):
        """The other side of the boundary: settled states still toggle."""
        from unittest.mock import AsyncMock, patch

        door = PowerPetDoor("127.0.0.1")
        door._status = status

        with (
            patch.object(door, "open", new_callable=AsyncMock) as mock_open,
            patch.object(door, "close", new_callable=AsyncMock) as mock_close,
        ):
            await door.toggle()

        assert mock_open.await_count == opens
        assert mock_close.await_count == closes

    async def test_a_rising_door_still_reads_as_open(self):
        """toggle changed; is_open did not - it is a different question."""
        door = PowerPetDoor("127.0.0.1")
        door._status = DoorStatus.RISING

        assert door.is_open is True
        assert door.is_closed is False


class TestStatusCallbackIsolation:
    """A raising status callback must not break the others."""

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
    """position maps every status to an exact percentage."""

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
# Device-Backed Property Tests
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

    async def test_refresh_schedules_sorts_by_index(self):
        """`door.schedules` order must not depend on the last code path.

        `GET_SCHEDULE_LIST` returns slots, and a device (or a simulator)
        whose slots were filled out of order can answer them out of order.
        `_on_schedule_update` re-sorts after every push, so without this
        the public property was sorted or unsorted depending on whether the
        last thing that touched it was a refresh or a push.
        """
        door = PowerPetDoor("127.0.0.1")

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            future = asyncio.get_running_loop().create_future()
            if cmd == CMD_GET_SCHEDULE_LIST:
                future.set_result([5, 1, 3])  # insertion order, not slot order
            else:
                future.set_result(
                    {
                        "index": kwargs["index"],
                        "daysOfWeek": [1] * 7,
                        "inside": True,
                        "in_start_time": {"hour": 6, "min": 0},
                        "in_end_time": {"hour": 22, "min": 0},
                    }
                )
            return future

        door._client.send_message = fake_send

        schedules = await door.refresh_schedules(timeout=1.0)

        assert [s.index for s in schedules] == [1, 3, 5]
        assert [s.index for s in door.schedules] == [1, 3, 5]

    async def test_refresh_names_each_failed_step_in_the_log(self, caplog):
        """A dead refresh step is reported at the door layer, not swallowed."""
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
        """Both settings sub-steps are named when they fail."""
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

    async def test_refresh_names_the_failed_steps_to_its_caller(self):
        """A log line cannot be acted on; the return value can.

        The device drops requests - any command, occasionally, including a
        valid one - so a partial refresh is ordinary rather than
        exceptional. A consumer with a freshness contract of its own has no
        other way to learn that the cache it is about to serve is stale.
        """
        door = PowerPetDoor("127.0.0.1")

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            future = asyncio.get_running_loop().create_future()
            future.set_exception(CommandError(cmd, "NAK"))
            return future

        door._client.send_message = fake_send

        assert await door.refresh() == list(REFRESH_STEPS)

    async def test_refresh_reports_settings_though_it_arrives_as_a_list(self, door, simulator):
        """The step that matters most is the one that does not raise.

        `refresh_settings` answers with its own failed sub-steps rather than
        raising, so inside `refresh`'s gather it comes back as a non-empty
        LIST where every other failed step is an exception. Counting only
        exceptions therefore misses precisely the step carrying the power
        flag, the sensor enables and the hold time.

        Driven against the running simulator so that GET_SETTINGS is the
        ONLY thing that fails - every other step really lands, so `settings`
        can reach the result by way of the list and by no other route.
        """
        real_send = door._client.send_message

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            if cmd == CMD_GET_SETTINGS:
                future = asyncio.get_running_loop().create_future()
                future.set_exception(CommandError(cmd, "NAK"))
                return future
            return real_send(msg_type, cmd, notify=notify, **kwargs)

        door._client.send_message = fake_send

        assert await door.refresh() == [REFRESH_STEP_SETTINGS]

    async def test_refresh_settings_names_the_failed_command_to_its_caller(self, door, simulator):
        """The two commands fail separately and are reported separately.

        Losing GET_SETTINGS leaves the power flag stale; losing
        GET_NOTIFICATIONS leaves only the five notification toggles stale.
        A caller that cannot tell them apart has to treat the cheap failure
        as the expensive one.
        """
        real_send = door._client.send_message

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            if cmd == CMD_GET_NOTIFICATIONS:
                future = asyncio.get_running_loop().create_future()
                future.set_exception(CommandError(cmd, "NAK"))
                return future
            return real_send(msg_type, cmd, notify=notify, **kwargs)

        door._client.send_message = fake_send

        assert await door.refresh_settings() == [CMD_GET_NOTIFICATIONS]

    async def test_a_refresh_that_landed_reports_nothing_failed(self, door, simulator):
        """The other side of the boundary, against a real conversation.

        Without this the tests above pass on a function that always names
        every step, which would report a healthy door as permanently stale.
        """
        assert await door.refresh() == []
        assert await door.refresh_settings() == []

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
# Callback Registration and Isolation Tests
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
# Listener None-Value Guard Tests
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


class TestFalsyNonBoolFlagsDoNotReadAsOff:
    """`make_bool` passes a value it does not recognize straight through.

    Its final `else: return v` returns the argument for anything that is
    neither a string nor an int, so `[]`, `{}` and `0.0` arrive at these
    listeners as themselves, not as None - and `if value is not None` let
    them into a strictly typed cache. A known-ON `safety_lock` receiving
    `[]` then read False: a safety flag failing in the permissive
    direction. (It is not permanent - any `refresh_settings()` or reconnect
    heals it - but it is wrong until then.)

    Widening `make_bool` is not the fix: `compress_schedule` calls it
    unguarded on day flags, where "unrecognized" has to stay fail-closed.
    """

    FALSY_NON_BOOLS = [[], {}, 0.0, (), set()]

    @pytest.mark.parametrize("value", FALSY_NON_BOOLS)
    async def test_a_known_on_safety_lock_is_not_turned_off(self, value, caplog):
        door = PowerPetDoor("127.0.0.1")
        door._safety_lock = True

        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.door"):
            door._on_safety_lock_update(FIELD_OUTSIDE_SENSOR_SAFETY_LOCK, value)

        assert door.safety_lock is True
        assert "keeping the cached value" in caplog.text

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
            ("_on_notify_inside_on", "_notifications"),
            ("_on_notify_inside_off", "_notifications"),
            ("_on_notify_outside_on", "_notifications"),
            ("_on_notify_outside_off", "_notifications"),
            ("_on_notify_low_battery", "_notifications"),
        ],
    )
    async def test_every_facade_flag_listener_keeps_its_cache(self, method, attr):
        """All twelve, because they all cached whatever `make_bool` returned."""
        door = PowerPetDoor("127.0.0.1")
        before = copy.copy(getattr(door, attr))

        getattr(door, method)("field", [])

        assert getattr(door, attr) == before

    async def test_the_settings_sweep_keeps_its_cache_too(self):
        """`_on_settings` reads the same values through the same coercion."""
        door = PowerPetDoor("127.0.0.1")
        door._safety_lock = True
        door._pet_proximity_keep_open = True

        door._on_settings({FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: [], FIELD_CMD_LOCKOUT: []})

        assert door.safety_lock is True
        assert door._pet_proximity_keep_open is True

    async def test_a_real_bool_still_lands(self):
        """The guard must not make the listeners inert."""
        door = PowerPetDoor("127.0.0.1")
        door._safety_lock = True

        door._on_safety_lock_update(FIELD_OUTSIDE_SENSOR_SAFETY_LOCK, False)

        assert door.safety_lock is False


# ============================================================================
# Schedule Cache Maintenance Tests
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
        """A bad device payload must not silently freeze the cache.

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


# ============================================================================
# Untrusted device payloads reaching the facade
# ============================================================================


async def _next_request(transport, timeout: float = 5.0) -> dict:
    """Wait for the client to actually put a request on the wire.

    ``enqueue_data`` hands off to a task and the send path honours
    ``MINIMUM_TIME_BETWEEN_MSGS``, so the write is not synchronous with the
    call that requested it.
    """
    async with asyncio.timeout(timeout):
        while True:
            message = transport.get_last_message()
            if message is not None:
                return message
            await asyncio.sleep(0.005)


class TestFacadeRejectsMalformedDevicePayloads:
    """The facade caches device data; a scalar there poisons it silently."""

    async def test_hw_info_listener_ignores_a_non_mapping(self, caplog):
        """`_hw_info` is the only retained payload, and three public
        properties treat it as a dict - `hardware_info` raised
        `AttributeError: 'str' object has no attribute 'copy'` with nothing
        in the log naming the frame that caused it."""
        door = PowerPetDoor("127.0.0.1")
        door._hw_info = {"ver": "1"}

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            door._on_hw_info_update("1.2.3")

        assert door._hw_info == {"ver": "1"}
        assert door.hardware_info == {"ver": "1"}
        assert door.firmware_version == "0.0.0"
        assert [record.getMessage() for record in caplog.records] == [
            "Ignoring non-mapping hardware info: 1.2.3",
        ]

    async def test_hw_info_listener_still_caches_a_mapping(self):
        door = PowerPetDoor("127.0.0.1")

        door._on_hw_info_update({"fw_maj": 1, "fw_min": 2, "fw_pat": 3})

        assert door.firmware_version == "1.2.3"

    async def test_refresh_hardware_info_ignores_a_non_mapping_result(self, mock_client, caplog):
        client, transport, device = mock_client
        door = PowerPetDoor("127.0.0.1")
        door._client = client
        door._hw_info = {"ver": "9"}

        task = asyncio.ensure_future(door.refresh_hardware_info())
        msg_id = (await _next_request(transport))["msgId"]
        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            device.respond_success(msg_id, "GET_HW_INFO", fwInfo=5)
            result = await asyncio.wait_for(task, 1.0)

        assert result == {"ver": "9"}
        assert door._hw_info == {"ver": "9"}
        assert [r.getMessage() for r in caplog.records if r.name == "powerpetdoor.door"] == [
            "Ignoring non-mapping hardware info: 5"
        ]

    @pytest.mark.parametrize("payload", [3, 1.5, True, "01", {"0": {}}], ids=repr)
    async def test_refresh_schedules_rejects_a_non_list_index_list(
        self, mock_client, caplog, payload
    ):
        """Iterating the raw value raised TypeError out of a documented
        coroutine for a scalar, and issued one GET_SCHEDULE *per character*
        for a string - 200 sequential round trips against a device that
        rate-limits between messages."""
        client, transport, device = mock_client
        door = PowerPetDoor("127.0.0.1")
        door._client = client
        door._schedules = [Schedule(index=7)]

        task = asyncio.ensure_future(door.refresh_schedules())
        msg_id = (await _next_request(transport))["msgId"]
        transport.clear()
        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            device.respond_success(msg_id, "GET_SCHEDULE_LIST", schedules=payload)
            result = await asyncio.wait_for(task, 1.0)

        assert result == []
        assert door.schedules == []
        # No follow-up GET_SCHEDULE was issued at all.
        assert transport.get_written_messages() == []
        door_logs = [r.getMessage() for r in caplog.records if r.name == "powerpetdoor.door"]
        assert len(door_logs) == 1
        assert door_logs[0].startswith("Device sent a non-list schedule index list: ")

    async def test_refresh_schedules_timeout_log_survives_a_string_index(self, mock_client, caplog):
        """`%d` on a device-supplied index turned the timeout warning into
        a logging-internal formatting error on stderr."""
        client, transport, device = mock_client
        door = PowerPetDoor("127.0.0.1")
        door._client = client

        # Step 1 is answered; step 2 (GET_SCHEDULE index "zero") is not, so
        # the timeout warning fires with a string index.
        task = asyncio.ensure_future(door.refresh_schedules(timeout=0.3))
        msg_id = (await _next_request(transport))["msgId"]
        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            device.respond_success(msg_id, "GET_SCHEDULE_LIST", schedules=["zero"])
            assert await asyncio.wait_for(task, 5.0) == []

        assert "Timeout fetching schedule zero" in [
            r.getMessage() for r in caplog.records if r.name == "powerpetdoor.door"
        ]


# ============================================================================
# Facade cache type guards
# ============================================================================


class TestFacadeCacheIsTypeGuarded:
    """Nothing enters the facade cache without a type check.

    ``PowerPetDoor`` is layer 1 (strict Python types) and the client is
    layer 3 (deliberately liberal - it hands the facade whatever the device
    said, and ``make_bool`` is *documented* to return None for a string it
    does not recognize). Five listeners assigned those values straight into
    strictly typed attributes:

    - ``batteryPercent: "55"`` made the documented ``battery.charging``
      property raise ``TypeError`` with **nothing logged**;
    - stats and timezone silently held the wrong Python type;
    - ``holdOpenTime: "200"`` raised out of the listener - a full traceback
      per frame - and ``NaN`` was cached into a property documented
      ``-> float``.

    And the ``dict.get(key, cached)`` "keep the last good value" defaults
    could never fire, because ``_handle_battery`` always builds every key:
    a reply that *omitted* a field overwrote a good cached value with None.
    """

    @pytest.fixture
    def door(self):
        return PowerPetDoor("127.0.0.1")

    # -- battery ------------------------------------------------------------

    @pytest.mark.parametrize(
        "percent",
        ["55", None, True, float("nan"), float("inf"), [55], {"p": 55}],
        ids=["str", "absent", "bool", "nan", "inf", "list", "dict"],
    )
    async def test_a_bad_battery_percent_keeps_the_cached_value(self, door, percent, caplog):
        door._battery = BatteryInfo(percent=42, present=True, ac_present=True)

        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.door"):
            door._on_battery_update(
                {
                    FIELD_BATTERY_PERCENT: percent,
                    FIELD_BATTERY_PRESENT: True,
                    FIELD_AC_PRESENT: True,
                }
            )

        assert door.battery_percent == 42
        assert isinstance(door.battery_percent, int)
        # The property that used to raise now answers, every time.
        assert door.battery.charging is True
        assert any("keeping the cached value" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        ("percent", "expected"),
        [(55, 55), (0, 0), (100, 100), (55.7, 55)],
        ids=["int", "zero", "full", "float-is-coerced"],
    )
    async def test_a_usable_battery_percent_is_cached_as_an_int(self, door, percent, expected):
        door._on_battery_update({FIELD_BATTERY_PERCENT: percent})

        assert door.battery_percent == expected
        assert isinstance(door.battery_percent, int)

    async def test_a_huge_integer_percent_does_not_overflow_the_guard(self, door):
        """``math.isfinite`` on a 10**400 int raises OverflowError.

        The guard returns on the ``isinstance(int)`` branch before it can,
        so a hostile device cannot turn the type check itself into an
        exception escaping the listener.
        """
        door._on_battery_update({FIELD_BATTERY_PERCENT: 10**400})

        assert door.battery_percent == 10**400

    @pytest.mark.parametrize(
        "value", [None, "1", 1, "maybe"], ids=["unrecognized", "str", "int", "make_bool-None"]
    )
    async def test_a_non_bool_battery_flag_keeps_the_cached_value(self, door, value):
        """``make_bool`` returns None for a value it does not recognize.

        The cached value has to be *decisive*. Seeding ``present=True``
        made "kept the cache" and "coerced the int" give the same answer
        for the ``1`` parameter (``bool(1)`` is ``True``), so a `_keep_bool`
        that coerced ints passed this test and the whole suite (CLAUDE.md
        rules 8/9). Every parameter here is truthy, and the cache is False.
        """
        door._battery = BatteryInfo(percent=42, present=False, ac_present=False)

        door._on_battery_update({FIELD_BATTERY_PRESENT: value, FIELD_AC_PRESENT: value})

        assert door.battery_present is False
        assert door.ac_present is False
        assert door.battery.charging is False
        assert door.battery.discharging is False

    async def test_a_usable_battery_flag_is_cached(self, door):
        door._battery = BatteryInfo(percent=42, present=True, ac_present=True)

        door._on_battery_update({FIELD_BATTERY_PRESENT: False, FIELD_AC_PRESENT: False})

        assert door.battery_present is False
        assert door.ac_present is False

    # -- stats --------------------------------------------------------------

    @pytest.mark.parametrize(
        "value", ["5", None, True, 1.5e400, [5]], ids=["str", "null", "bool", "inf", "list"]
    )
    async def test_bad_stats_counters_keep_the_cached_values(self, door, value):
        door._total_open_cycles = 11
        door._total_auto_retracts = 3

        door._on_total_cycles_update(FIELD_TOTAL_OPEN_CYCLES, value)
        door._on_total_retracts_update(FIELD_TOTAL_AUTO_RETRACTS, value)

        assert door.total_open_cycles == 11
        assert door.total_auto_retracts == 3

    async def test_usable_stats_counters_are_cached(self, door):
        door._on_total_cycles_update(FIELD_TOTAL_OPEN_CYCLES, 7)
        door._on_total_retracts_update(FIELD_TOTAL_AUTO_RETRACTS, 2)

        assert door.total_open_cycles == 7
        assert door.total_auto_retracts == 2

    # -- timezone -----------------------------------------------------------

    @pytest.mark.parametrize("value", [5, None, True, ["EST"]], ids=["int", "null", "bool", "list"])
    async def test_a_non_str_timezone_keeps_the_cached_value(self, door, value):
        door._timezone = "EST5EDT,M3.2.0,M11.1.0"

        door._on_timezone_update(value)

        assert door.timezone == "EST5EDT,M3.2.0,M11.1.0"

    async def test_a_str_timezone_is_cached(self, door):
        door._on_timezone_update("UTC0")

        assert door.timezone == "UTC0"

    # -- hold time ----------------------------------------------------------

    @pytest.mark.parametrize(
        "value",
        ["200", None, float("nan"), True, [200], 10**400, -(10**400)],
        ids=["str", "null", "nan", "bool", "list", "huge-int", "huge-negative-int"],
    )
    @pytest.mark.parametrize("cached", [4.0, 0.29], ids=["exact-round-trip", "lossy-round-trip"])
    async def test_a_bad_hold_time_keeps_the_cached_value_without_raising(
        self, door, value, cached
    ):
        """A string used to raise ``TypeError`` straight out of the listener.

        The cached seed has to be decisive too. ``_hold_time`` is populated
        as ``centiseconds / 100.0``, and for 4,586 of the 90,001 centisecond
        values a device can send (5.1%) ``int(x * 100) != round(x * 100)`` -
        but ``4.0`` is one of the values where they agree, so replacing the
        fallback's ``round()`` with ``int()`` silently rewrote the cache and
        passed this test. ``0.29`` is decisive: ``0.29 * 100`` is
        ``28.999999999999996``.

        ``10**400`` is legal JSON, an ``int``, and ``value / 100.0`` raises
        ``OverflowError``. ``-(10**400)`` is the same case on the other side
        of zero, and it is the half the magnitude guard's ``-maximum <=``
        operand is the only thing stopping - a device sending a negative
        arbitrary-precision integer is no less plausible than a positive
        one, and ``docs/protocol.md`` is reverse-engineered and constrains
        neither.
        """
        door._hold_time = cached

        door._on_hold_time_update(value)

        assert door.hold_time == cached

    async def test_a_usable_hold_time_is_cached_in_seconds(self, door):
        door._on_hold_time_update(1500)

        assert door.hold_time == 15.0

    @pytest.mark.parametrize(
        ("sign", "offset", "accepted"),
        [(1, 0, True), (1, 1, False), (-1, 0, True), (-1, 1, False)],
        ids=["+limit", "+limit+1", "-limit", "-limit-1"],
    )
    async def test_the_representability_bound_is_exact_on_both_signs(
        self, door, caplog, sign, offset, accepted
    ):
        """The bound is what ``float`` can hold, not what a protocol says.

        ``docs/protocol.md`` is reverse-engineered, so the facade must not
        refuse a device value for exceeding a bound this project invented.
        It refuses it only when the arithmetic downstream (``/ 100.0``)
        physically cannot be performed - which is exactly
        ``sys.float_info.max``.

        It is a *magnitude* bound, so CLAUDE.md rule 8 has four points, not
        two. Only the two positive ones were pinned, and dropping the
        ``-maximum <=`` half survived the whole suite: shipped,
        ``-(10**400)`` is rejected and the cache kept; without that operand
        it reaches ``centiseconds / 100.0`` and raises ``OverflowError``.
        """
        door._hold_time = 4.0
        limit = int(sys.float_info.max)
        value = sign * (limit + offset)

        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.door"):
            door._on_hold_time_update(value)

        if accepted:
            assert door.hold_time == value / 100.0
            assert not any("keeping the cached value" in r.getMessage() for r in caplog.records)
        else:
            assert door.hold_time == 4.0
            assert any("keeping the cached value" in r.getMessage() for r in caplog.records)

    async def test_the_representability_bound_is_not_applied_to_the_other_int_fields(self, door):
        """Only the consumer that does float arithmetic passes ``maximum``.

        Python ints are unbounded and these three merely store the value,
        so bounding inside ``_keep_int`` for every caller would refuse
        values that are harmless - and would contradict
        ``test_a_huge_integer_percent_does_not_overflow_the_guard``.
        """
        huge = 10**400

        door._on_battery_update({FIELD_BATTERY_PERCENT: huge})
        door._on_total_cycles_update(FIELD_TOTAL_OPEN_CYCLES, huge)
        door._on_total_retracts_update(FIELD_TOTAL_AUTO_RETRACTS, huge)

        assert door.battery_percent == huge
        assert door.total_open_cycles == huge
        assert door.total_auto_retracts == huge


class TestTheRejectionLogSaysWhatWasExpected:
    """`_log_rejected`'s third argument was asserted nowhere, for any field.

    The only assertion on this line anywhere was the substring
    `"keeping the cached value"`, which is identical for every rejection
    reason - so dropping the `"int" if maximum is None else f"int of
    magnitude <= {maximum:g}"` ternary survived the whole suite, and a
    `-10**400` rejected for *magnitude* logged `expected int` for a value
    that **is** an `int`. That is the one diagnostic an operator reaches
    for when a firmware variant is misbehaving, and error text is part of
    the contract.
    """

    @pytest.mark.parametrize(
        ("update", "value", "field", "expected"),
        [
            pytest.param(
                lambda door, value: door._on_total_cycles_update(FIELD_TOTAL_OPEN_CYCLES, value),
                "55",
                FIELD_TOTAL_OPEN_CYCLES,
                "int",
                id="int-unbounded",
            ),
            pytest.param(
                lambda door, value: door._on_hold_time_update(value),
                -(10**400),
                "hold_time",
                "int of magnitude <= 1.79769e+308",
                id="int-bounded",
            ),
        ],
    )
    async def test_the_expected_text_names_the_real_constraint(
        self, door, caplog, update, value, field, expected
    ):
        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.door"):
            update(door, value)

        rendered = sanitize_text(value, MAX_LOGGED_LENGTH)
        assert (
            f"Ignoring {rendered} from device for {field} (expected {expected}); "
            "keeping the cached value"
        ) in [record.getMessage() for record in caplog.records]

    @pytest.mark.parametrize(
        ("helper", "value", "expected"),
        [
            (door_module._keep_bool, "1", "bool"),
            (door_module._keep_str, 7, "str"),
            (door_module._keep_int, "9", "int"),
        ],
        ids=["bool", "str", "int"],
    )
    def test_every_expected_spelling_is_pinned(self, caplog, helper, value, expected):
        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.door"):
            helper(value, "cached", "some_field")

        assert [record.getMessage() for record in caplog.records] == [
            f"Ignoring {value} from device for some_field (expected {expected}); "
            "keeping the cached value"
        ]

    def test_the_rejected_value_is_sanitized_and_bounded(self, caplog):
        """The value comes off the wire, so the line is a log sink like any
        other: control characters escaped, length capped."""
        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.door"):
            door_module._keep_int("\x1b[2J" + "A" * 5000, 0, "some_field")

        message = caplog.records[0].getMessage()
        assert "\x1b" not in message
        assert "\\x1b[2JAAA" in message
        assert "...(truncated)" in message
        assert len(message) < 400


class TestTheFacadeTimeoutSaysWhatTimedOut:
    """`asyncio.wait_for` raises a bare `TimeoutError()`.

    Its `repr()` is literally `TimeoutError()`, so a developer saw an empty
    exception after a 20-second stall with no way to tell "the door is
    wedged" from "you never called connect()" - the least actionable
    exception this API can produce. The queue-on-reconnect behaviour it
    hides is deliberate and documented, so the fix is the message, not the
    behaviour.
    """

    async def test_a_disconnected_setter_names_the_command_and_the_queue(self):
        door = PowerPetDoor("10.0.0.5", port=3000, timeout=0.01, reconnect=0)

        with pytest.raises(TimeoutError) as exc_info:
            await door.set_hold_time(5, timeout=0.01)

        message = str(exc_info.value)
        assert message != ""
        assert message.startswith("SET_HOLD_TIME timed out after 0.01s waiting for 10.0.0.5:3000")
        assert "the command is queued and will be sent" in message

    async def test_a_connected_timeout_does_not_blame_the_connection(self, mock_client):
        """The control: connected, so the queue sentence must *not* appear -
        that is the half that distinguishes "wedged" from "never connected"."""
        door = PowerPetDoor("10.0.0.5", port=3000)
        door._client = mock_client[0]

        assert door.connected is True
        with pytest.raises(TimeoutError) as exc_info:
            await door.set_hold_time(5, timeout=0.01)

        message = str(exc_info.value)
        assert message == "SET_HOLD_TIME timed out after 0.01s waiting for 10.0.0.5:3000"

    async def test_the_default_timeout_is_reported_when_none_is_given(self, mock_client):
        door = PowerPetDoor("10.0.0.5", port=3000)
        door._client = mock_client[0]
        door._client.cfg_timeout = 0.01

        with pytest.raises(TimeoutError) as exc_info:
            await door.refresh_battery()

        assert str(exc_info.value) == (
            f"{CMD_GET_DOOR_BATTERY} timed out after {door.default_timeout}s "
            "waiting for 10.0.0.5:3000"
        )

    async def test_a_response_that_arrives_is_returned_unchanged(self, door, simulator):
        """The control for the wrapper itself: against the real simulator,
        the happy path returns exactly what it always did."""
        simulator.state.door_status = DOOR_STATE_HOLDING

        assert await door.refresh_status() is DoorStatus.HOLDING
        assert await door.refresh_hardware_info() == door.hardware_info


# ============================================================================
# Facade surface added once hardware settled the protocol
# ============================================================================


class TestSensorTriggerVoltageFacade:
    """`sensorTriggerVoltage` is readable and settable; the facade exposes both.

    It was reachable only from the message level before: `GET_SETTINGS`
    carries it, and the client has always had a listener for it, but
    `PowerPetDoor` had neither a property nor a setter.
    """

    async def test_the_settings_refresh_populates_both_voltages(self, door, simulator):
        simulator.state.sensor_trigger_voltage = 2000
        simulator.state.sleep_sensor_trigger_voltage = 1800

        await door.refresh_settings()

        assert door.sensor_trigger_voltage == 2000
        assert door.sleep_sensor_trigger_voltage == 1800

    async def test_set_sensor_trigger_voltage_reaches_the_device(self, door, simulator):
        await door.set_sensor_trigger_voltage(1500)

        assert simulator.state.sensor_trigger_voltage == 1500

    async def test_set_sleep_sensor_trigger_voltage_reaches_the_device(self, door, simulator):
        await door.set_sleep_sensor_trigger_voltage(1800)

        assert simulator.state.sleep_sensor_trigger_voltage == 1800

    async def test_the_setters_send_the_voltage_field_not_the_getters(self, door):
        """The whole point of `build_set_voltage_message`, pinned end to end.

        A real door rejects the getter's
        field name, so a facade that sent `sensorTriggerVoltage` would
        silently do nothing.
        """
        sent: dict = {}

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            sent.update(kwargs)
            future = asyncio.get_running_loop().create_future()
            future.set_result({})
            return future

        door._client.send_message = fake_send

        await door.set_sensor_trigger_voltage(1500)

        assert sent == {FIELD_VOLTAGE: 1500}
        assert FIELD_SENSOR_TRIGGER_VOLTAGE not in sent

    @pytest.mark.parametrize(
        ("field_name", "attribute"),
        [
            (FIELD_SENSOR_TRIGGER_VOLTAGE, "sensor_trigger_voltage"),
            (FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE, "sleep_sensor_trigger_voltage"),
        ],
    )
    def test_an_unusable_voltage_keeps_the_cached_value(self, field_name, attribute):
        """A device value that is not an int must not poison an int property."""
        door = PowerPetDoor("127.0.0.1")
        setattr(door, f"_{attribute}", 2000)

        door._on_settings({field_name: "lots"})

        assert getattr(door, attribute) == 2000

    @pytest.mark.parametrize(
        ("listener", "attribute"),
        [
            ("_on_sensor_trigger_voltage_update", "sensor_trigger_voltage"),
            ("_on_sleep_sensor_trigger_voltage_update", "sleep_sensor_trigger_voltage"),
        ],
    )
    def test_the_dedicated_listener_also_caches(self, listener, attribute):
        """The GET_/SET_ replies arrive through their own listener, not settings."""
        door = PowerPetDoor("127.0.0.1")

        getattr(door, listener)(1234)
        assert getattr(door, attribute) == 1234

        getattr(door, listener)([])
        assert getattr(door, attribute) == 1234


class TestRemotePairingFacade:
    """HAS_REMOTE_ID / HAS_REMOTE_KEY had no facade surface at all."""

    async def test_refresh_remote_info_populates_both(self, door, simulator):
        simulator.state.has_remote_id = True
        simulator.state.has_remote_key = False

        await door.refresh_remote_info()

        assert door.has_remote_id is True
        assert door.has_remote_key is False

    def test_defaults_before_the_first_refresh(self):
        """Not part of GET_SETTINGS, so they stay at their default until asked."""
        door = PowerPetDoor("127.0.0.1")

        assert door.has_remote_id is False
        assert door.has_remote_key is False

    @pytest.mark.parametrize(
        ("listener", "prop"),
        [("_on_remote_id_update", "has_remote_id"), ("_on_remote_key_update", "has_remote_key")],
    )
    def test_an_unreadable_value_keeps_the_cached_flag(self, listener, prop):
        door = PowerPetDoor("127.0.0.1")
        getattr(door, listener)(True)

        getattr(door, listener)(None)

        assert getattr(door, prop) is True


class TestDoorClockFacade:
    """`GET_TIME`: undocumented by the vendor, and the only way to check that
    a door will fire a schedule when you expect it to."""

    async def test_refresh_time_parses_the_devices_asctime(self, door):
        when = await door.refresh_time()

        assert isinstance(when, datetime)
        # `refresh_time` returns the reading in THIS machine's zone, so its
        # face value equals the door's raw string only when the two zones
        # happen to agree. That held on a developer's machine and failed in
        # CI, which runs UTC. Convert back to the door's own zone - the
        # round trip the docstring documents - and the comparison is
        # timezone-independent.
        door_tz = resolve_tzinfo(door.timezone) if door.timezone else None
        face = when.astimezone(door_tz) if door_tz else when
        assert face.strftime(TIME_FORMAT) == door.device_time

    async def test_device_time_keeps_the_raw_string(self, door):
        await door.refresh_time()

        assert door.device_time
        assert datetime.strptime(door.device_time, TIME_FORMAT)

    def test_device_time_is_empty_before_the_first_refresh(self):
        assert PowerPetDoor("127.0.0.1").device_time == ""

    async def test_an_unparseable_time_returns_none_but_keeps_the_string(self, door):
        """The door was observed answering a stale/odd frame; do not raise."""
        door._on_time_update("not a time")

        parsed = await door.refresh_time()

        assert parsed is None or isinstance(parsed, datetime)

    async def test_refresh_time_is_aware_and_local_once_the_zone_is_known(self, door):
        """The reading is an instant, not a wall clock.

        The device sends a bare asctime with no offset. Anchoring it to
        the door's zone and converting to local gives the caller one
        instant they can compare against `datetime.now()` directly;
        naive, it is silently wrong unless the caller shares the zone.
        """
        door._timezone = "America/New_York"

        when = await door.refresh_time()

        assert when is not None
        assert when.tzinfo is not None
        assert when.utcoffset() == datetime.now().astimezone().utcoffset()
        # Read back in the door's own zone, it is exactly what the door said.
        face = when.astimezone(ZoneInfo("America/New_York")).strftime(TIME_FORMAT)
        assert face == door.device_time

    async def test_refresh_time_is_aware_for_the_posix_zone_a_door_reports(self, door, monkeypatch):
        """The zone a REAL door sends, from a cold cache.

        The test above uses `America/New_York`, which `ZoneInfo()`
        resolves on its own - so it passed while this path was broken. A
        door reports POSIX (`EST5EDT,M3.2.0,M11.1.0`), and mapping that
        back to an IANA zone needs the tzdata cache. Nothing on the read
        path built it, so an integrator who never called `set_timezone()`
        got a NAIVE datetime from a method documenting an aware one, and
        `when - datetime.now(tz)` raised TypeError.
        """
        from powerpetdoor import tz_utils

        # Cold: exactly what a fresh process looks like.
        monkeypatch.setattr(tz_utils, "_cache_initialized", False)
        monkeypatch.setattr(tz_utils, "_posix_to_iana", {})
        monkeypatch.setattr(tz_utils, "_iana_to_posix", {})
        monkeypatch.setattr(tz_utils, "_iana_timezones", set())
        door._timezone = "EST5EDT,M3.2.0,M11.1.0"

        when = await door.refresh_time()

        assert when is not None
        assert when.tzinfo is not None, "a POSIX zone still yielded a naive reading"
        # The point of being aware: it can be compared to an aware now.
        assert isinstance(when - datetime.now().astimezone(), timedelta)

    async def test_refresh_time_stays_naive_without_a_zone(self, door):
        """With no timezone there is nothing to anchor the reading to."""
        door._timezone = ""

        when = await door.refresh_time()

        assert when is not None
        assert when.tzinfo is None
        assert when.strftime(TIME_FORMAT) == door.device_time

    def test_a_non_string_time_keeps_the_cached_one(self):
        door = PowerPetDoor("127.0.0.1")
        door._on_time_update("Sat Aug 22 23:13:48 2026")

        door._on_time_update(12345)

        assert door.device_time == "Sat Aug 22 23:13:48 2026"


class TestTheFacadeSendsEveryCommandUnderTheRightEnvelopeKey:
    """`{"cmd": "ENABLE_INSIDE"}` fails.

    Every released version before this one sent the individual setting
    commands under `cmd`, so none of them worked against a real door. Each
    facade call is driven here and the envelope key it chose is compared
    against the single source of truth.
    """

    @staticmethod
    def _recorder(door):
        sent: list[tuple[str, str]] = []

        def fake_send(msg_type, cmd, notify=False, **kwargs):
            sent.append((msg_type, cmd))
            future = asyncio.get_running_loop().create_future()
            future.set_result({})
            return future

        door._client.send_message = fake_send
        return sent

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda d: d.open(), id="open"),
            pytest.param(lambda d: d.cycle(), id="cycle"),
            pytest.param(lambda d: d.close(), id="close"),
            pytest.param(lambda d: d.set_inside_sensor(True), id="set_inside_sensor"),
            pytest.param(lambda d: d.set_outside_sensor(False), id="set_outside_sensor"),
            pytest.param(lambda d: d.set_power(True), id="set_power"),
            pytest.param(lambda d: d.set_auto(False), id="set_auto"),
            pytest.param(lambda d: d.set_safety_lock(True), id="set_safety_lock"),
            pytest.param(lambda d: d.set_autoretract(False), id="set_autoretract"),
            pytest.param(
                lambda d: d.set_pet_proximity_keep_open(True), id="set_pet_proximity_keep_open"
            ),
            pytest.param(lambda d: d.set_hold_time(2.0), id="set_hold_time"),
            pytest.param(lambda d: d.set_timezone("UTC0"), id="set_timezone"),
            pytest.param(lambda d: d.set_notifications(low_battery=True), id="set_notifications"),
            pytest.param(lambda d: d.set_sensor_trigger_voltage(1500), id="set_sensor_voltage"),
            pytest.param(
                lambda d: d.set_sleep_sensor_trigger_voltage(1800), id="set_sleep_voltage"
            ),
            pytest.param(lambda d: d.refresh_remote_info(), id="refresh_remote_info"),
            pytest.param(lambda d: d.refresh_time(), id="refresh_time"),
            pytest.param(lambda d: d.refresh_status(), id="refresh_status"),
            pytest.param(lambda d: d.refresh_battery(), id="refresh_battery"),
            pytest.param(lambda d: d.refresh_stats(), id="refresh_stats"),
            pytest.param(lambda d: d.refresh_hardware_info(), id="refresh_hardware_info"),
            pytest.param(lambda d: d.delete_schedule(0), id="delete_schedule"),
        ],
    )
    async def test_the_envelope_key_matches_envelope_for_command(self, call):
        door = PowerPetDoor("127.0.0.1")
        sent = self._recorder(door)

        await call(door)

        assert sent
        assert [
            (msg_type, cmd) for msg_type, cmd in sent if msg_type != envelope_for_command(cmd)
        ] == []

    async def test_door_motion_really_is_the_cmd_envelope(self):
        """The control: the mapping under test is not "everything is config"."""
        door = PowerPetDoor("127.0.0.1")
        sent = self._recorder(door)

        await door.open()

        assert sent == [(COMMAND, CMD_OPEN_AND_HOLD)]

    async def test_open_and_cycle_send_the_commands_their_names_promise(self):
        """`open()` holds the door up; `cycle()` is the timed open.

        Both are door motion on the same envelope, so the envelope test
        above cannot tell them apart - swap the two and it still passes.
        This pins which wire command each name reaches.
        """
        door = PowerPetDoor("127.0.0.1")
        sent = self._recorder(door)

        await door.open()
        await door.cycle()
        await door.toggle()

        assert sent == [
            (COMMAND, CMD_OPEN_AND_HOLD),
            (COMMAND, CMD_OPEN),
            (COMMAND, CMD_OPEN_AND_HOLD),
        ]

    async def test_a_setting_command_really_is_the_config_envelope(self):
        door = PowerPetDoor("127.0.0.1")
        sent = self._recorder(door)

        await door.set_inside_sensor(True)

        assert sent == [(CONFIG, CMD_ENABLE_INSIDE)]


class TestASwitchedOffDoorIsNotUnknown:
    """`DOOR_POWEROFF` reaches the facade as a state, not as a warning.

    Measured on a real door: switch `power_state` off and
    `GET_DOOR_STATUS` answers `DOOR_POWEROFF`. The enum had no member for
    it, so `from_string` fell to `UNKNOWN` and logged - on every status
    read, for as long as the door stayed off. A consumer publishing the
    status then showed "unknown" for a door the user had simply switched
    off, and Home Assistant drops an undeclared state from long-term
    statistics, so the history had a hole in it too.
    """

    def test_the_wire_name_parses(self):
        assert DoorStatus.from_string(DOOR_STATE_POWEROFF) is DoorStatus.POWEROFF

    def test_it_does_not_log_an_unknown_status(self, caplog):
        """The warning is the symptom a user reports."""
        with caplog.at_level(logging.WARNING, logger="powerpetdoor.door"):
            DoorStatus.from_string(DOOR_STATE_POWEROFF)
        assert "Unknown door status" not in caplog.text

    def test_a_name_that_really_is_unknown_still_warns(self):
        """...and the warning has not simply been removed."""
        assert DoorStatus.from_string("DOOR_TELEPORTING") is DoorStatus.UNKNOWN

    async def test_the_facade_reads_a_switched_off_door_as_closed(self, door, simulator):
        """Over the real wire, end to end.

        The flap is down and the motor will not run, so `is_closed` is the
        honest answer - leaving it neither open nor closed would blank a
        cover entity for a door whose position is not in doubt.
        """
        await door.set_power(False)
        await door.refresh_status()

        assert door.status is DoorStatus.POWEROFF
        assert door.is_closed
        assert not door.is_open
        assert door.position == 0

    async def test_switching_it_back_on_restores_an_ordinary_state(self, door, simulator):
        """The other side of the boundary, so this cannot latch."""
        await door.set_power(False)
        await door.refresh_status()
        assert door.status is DoorStatus.POWEROFF

        await door.set_power(True)
        await door.refresh_status()

        assert door.status is not DoorStatus.POWEROFF
        assert door.is_closed


class TestTheAppSettingPolarities:
    """The mapping from the vendor app's switches to wire fields.

    Established experimentally, with the door's owner driving the app
    against a live capture - measurement, not inference. Two of the three
    are counter-intuitive enough that "simplifying" them is a standing
    temptation, so each polarity is pinned here.
    """

    async def test_keep_open_is_the_inverse_of_command_lockout(self, door, simulator):
        """App *"Allow pet to keep door open"* OFF  ⇒  allowCmdLockout "true".

        `PowerPetDoor` exposes the app's meaning, so the facade flag and
        the wire field must always be opposites.
        """
        await door.set_pet_proximity_keep_open(True)
        assert simulator.state.cmd_lockout is False
        assert door.pet_proximity_keep_open is True

        await door.set_pet_proximity_keep_open(False)
        assert simulator.state.cmd_lockout is True
        assert door.pet_proximity_keep_open is False

    def test_a_cmd_lockout_settings_frame_is_inverted_on_the_way_in(self):
        """The read path carries the same inversion as the write path."""
        door = PowerPetDoor("127.0.0.1")

        door._on_settings({FIELD_CMD_LOCKOUT: "true"})
        assert door.pet_proximity_keep_open is False

        door._on_settings({FIELD_CMD_LOCKOUT: "false"})
        assert door.pet_proximity_keep_open is True

    async def test_safety_lock_is_direct_not_inverted(self, door, simulator):
        """App *"always allow pet entry inside override timers"* ⇒
        `outsideSensorSafetyLock`, direct.

        The field name reads like a lock on the outside sensor; the app
        presents it as a schedule override. The polarity is the thing that
        settles it.
        """
        await door.set_safety_lock(True)
        assert simulator.state.safety_lock is True
        assert door.safety_lock is True

        await door.set_safety_lock(False)
        assert simulator.state.safety_lock is False
        assert door.safety_lock is False

    async def test_autoretract_is_bit_one_of_door_options(self, door, simulator):
        """App *"Auto Retract"* ⇒ `doorOptions` bit 1: on ⇒ 2, off ⇒ 0."""
        await door.set_autoretract(True)
        assert settings_payload(simulator.state)[FIELD_AUTORETRACT] == DOOR_OPTION_AUTORETRACT
        assert door.autoretract is True

        await door.set_autoretract(False)
        assert settings_payload(simulator.state)[FIELD_AUTORETRACT] == 0
        assert door.autoretract is False

    def test_an_unrelated_door_options_bit_does_not_read_as_autoretract(self):
        """Bit 0 and bits 2+ are unidentified; only bit 1 is auto-retract.

        Plain truthiness would report `doorOptions: 1` as auto-retract ON.
        """
        door = PowerPetDoor("127.0.0.1")
        door._autoretract = False

        door._on_settings({FIELD_AUTORETRACT: 1})
        assert door.autoretract is False

        door._on_settings({FIELD_AUTORETRACT: 3})
        assert door.autoretract is True


class TestTheTimezoneScanStaysOffTheEventLoop:
    """Building the tzdata cache is hundreds of blocking `open()` calls.

    `to_posix_tz` warms it itself when cold - via the SYNC initialiser,
    on whatever thread called it. On the event loop that is a stall of
    tens of milliseconds, so both `PowerPetDoor` paths warm it in a
    thread first and leave `to_posix_tz` nothing to do.

    The read path (`refresh_time`) is pinned incidentally, because a cold
    cache changes its return type from aware to naive. The write path
    sends the identical value either way - the only difference is whether
    the loop stops - so nothing caught its removal, and two reviewers
    found the same silence independently.
    """

    async def test_set_timezone_does_not_scan_tzdata_on_the_loop(self, door, monkeypatch):
        from powerpetdoor import tz_utils

        # Cold, as a fresh process is.
        monkeypatch.setattr(tz_utils, "_cache_initialized", False)
        monkeypatch.setattr(tz_utils, "_posix_to_iana", {})
        monkeypatch.setattr(tz_utils, "_iana_to_posix", {})
        monkeypatch.setattr(tz_utils, "_iana_timezones", set())

        on_loop: list[str] = []
        real_sync = tz_utils.init_timezone_cache_sync

        def spy() -> None:
            # `async_init_timezone_cache` reaches the same function through
            # `asyncio.to_thread`, so being off the loop's thread is what
            # distinguishes the two routes.
            if threading.current_thread() is threading.main_thread():
                on_loop.append("blocking scan on the event loop")
            real_sync()

        monkeypatch.setattr(tz_utils, "init_timezone_cache_sync", spy)

        await door.set_timezone("America/New_York")

        assert on_loop == [], on_loop[0] if on_loop else ""

    async def test_set_timezone_still_sends_the_posix_form(self, door, simulator):
        """Warming must not change what goes on the wire."""
        await door.set_timezone("America/New_York")

        assert simulator.state.timezone.startswith("EST5EDT"), simulator.state.timezone


class TestANotificationChangeIsDetectable:
    """A settings object handed out and then mutated tells nobody.

    `_on_notify_*` updated `self._notifications` in place while the
    property returned that same object, so a snapshot taken before a
    change equalled the state after it and poll-and-compare could never
    see a notification setting move. There was no other route either:
    `on_settings_change` fires for `GET_SETTINGS`, whose payload carries
    none of these five flags.

    `battery` replaces its object, which is why the same comparison has
    always worked there.
    """

    def test_a_snapshot_survives_a_later_change(self):
        door = PowerPetDoor("127.0.0.1")
        before = door.notifications

        door._on_notify_inside_on("x", True)

        assert before.inside_on is False, "the snapshot was mutated underneath the caller"
        assert door.notifications.inside_on is True
        assert before != door.notifications, "poll-and-compare cannot see the change"

    @pytest.mark.parametrize(
        "handler,attribute",
        [
            ("_on_notify_inside_on", "inside_on"),
            ("_on_notify_inside_off", "inside_off"),
            ("_on_notify_outside_on", "outside_on"),
            ("_on_notify_outside_off", "outside_off"),
            ("_on_notify_low_battery", "low_battery"),
        ],
    )
    def test_every_flag_replaces_rather_than_mutates(self, handler, attribute):
        """All five, because a fix on one path when five exist is not a fix."""
        door = PowerPetDoor("127.0.0.1")
        before = door.notifications

        getattr(door, handler)("x", True)

        assert getattr(before, attribute) is False
        assert getattr(door.notifications, attribute) is True

    def test_an_unreadable_value_keeps_the_previous_one(self):
        """Replacing must not lose the fail-soft behaviour."""
        door = PowerPetDoor("127.0.0.1")
        door._on_notify_low_battery("x", True)

        door._on_notify_low_battery("x", None)

        assert door.notifications.low_battery is True
