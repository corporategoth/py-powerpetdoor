# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator state module (state.py)."""

from __future__ import annotations

import json

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    FIELD_AUTO,
    FIELD_INSIDE,
    FIELD_POWER,
    FIELD_TZ,
)
from powerpetdoor.simulator import (
    BatteryConfig,
    DoorSimulatorState,
    DoorTimingConfig,
    Schedule,
)
from tests.conftest import GOLDEN_SCHEDULE_WIRE_FROM_DEVICE, assert_schedule_wire_types

# ============================================================================
# DoorTimingConfig Tests
# ============================================================================


class TestDoorTimingConfig:
    """Tests for DoorTimingConfig dataclass."""

    def test_default_values(self):
        """Default timing values should be reasonable."""
        config = DoorTimingConfig()
        assert config.rise_time == 1.5
        assert config.default_hold_time == 2
        assert config.slowing_time == 0.3
        assert config.closing_top_time == 0.4
        assert config.closing_mid_time == 0.4
        assert config.sensor_retrigger_window == 0.5

    def test_custom_values(self):
        """Should accept custom timing values."""
        config = DoorTimingConfig(
            rise_time=2.0,
            default_hold_time=15,
            slowing_time=0.5,
        )
        assert config.rise_time == 2.0
        assert config.default_hold_time == 15
        assert config.slowing_time == 0.5


# ============================================================================
# BatteryConfig Tests
# ============================================================================


class TestBatteryConfig:
    """Tests for BatteryConfig dataclass."""

    def test_default_values(self):
        """Default battery config should have reasonable defaults."""
        config = BatteryConfig()
        assert config.charge_rate == 1.0  # 1% per minute
        assert config.discharge_rate == 0.1  # 0.1% per minute
        assert config.update_interval == 60.0  # 60 seconds

    def test_custom_values(self):
        """Should accept custom battery config values."""
        config = BatteryConfig(
            charge_rate=5.0,
            discharge_rate=0.5,
            update_interval=30.0,
        )
        assert config.charge_rate == 5.0
        assert config.discharge_rate == 0.5
        assert config.update_interval == 30.0

    def test_zero_rates(self):
        """Should accept zero rates to disable automatic changes."""
        config = BatteryConfig(charge_rate=0.0, discharge_rate=0.0)
        assert config.charge_rate == 0.0
        assert config.discharge_rate == 0.0


# ============================================================================
# Schedule Tests
# ============================================================================


class TestSchedule:
    """Tests for Schedule dataclass."""

    def test_default_values(self):
        """Default schedule should cover all days 6AM-10PM."""
        schedule = Schedule(index=0)
        assert schedule.index == 0
        assert schedule.enabled is True
        assert schedule.days_of_week == [1, 1, 1, 1, 1, 1, 1]  # All days
        assert schedule.inside is False
        assert schedule.outside is False
        assert schedule.start_hour == 6
        assert schedule.end_hour == 22

    def test_to_dict_matches_the_device_to_client_wire_shape(self):
        """The simulator emitter pins the shape the DEVICE replies with.

        Counterpart of the library-side golden test. Every field but
        ``enabled`` is compared against the same payload, so a divergence
        anywhere else fails on whichever side moved; ``enabled`` differs
        because this is the device->client direction (``"1"``, as observed)
        while the library's emitter is client->device (a JSON boolean, as
        shipped since v0.1.0). Do not unify them.
        """
        schedule = Schedule(
            index=3,
            enabled=True,
            days_of_week=[True, False, True, False, True, False, True],
            inside=True,
            outside=False,
            start_hour=6,
            start_min=30,
            end_hour=22,
            end_min=15,
        )

        payload = schedule.to_dict()

        assert payload == GOLDEN_SCHEDULE_WIRE_FROM_DEVICE
        assert_schedule_wire_types(payload, enabled_type=str)

    def test_to_dict(self):
        """Should convert to protocol dict format."""
        schedule = Schedule(index=1, enabled=True, inside=True)
        result = schedule.to_dict()
        assert result["index"] == 1
        assert result["enabled"] == "1"
        assert "daysOfWeek" in result
        assert result["inside"] is True
        assert result["outside"] is False
        assert "in_start_time" in result
        assert "in_end_time" in result
        assert "out_start_time" in result
        assert "out_end_time" in result

    def test_to_dict_outside_sensor(self):
        """An outside-only entry zeroes the inside times and fills out*."""
        schedule = Schedule(
            index=2, outside=True, start_hour=7, start_min=30, end_hour=19, end_min=15
        )
        result = schedule.to_dict()
        assert result["outside"] is True
        assert result["inside"] is False
        assert result["out_start_time"] == {"hour": 7, "min": 30}
        assert result["out_end_time"] == {"hour": 19, "min": 15}
        # Inside times default to zero for a non-inside entry
        assert result["in_start_time"] == {"hour": 0, "min": 0}
        assert result["in_end_time"] == {"hour": 0, "min": 0}

    def test_from_dict(self):
        """Should create from protocol dict format."""
        data = {
            "index": 2,
            "enabled": "1",
            "daysOfWeek": [1, 1, 1, 1, 1, 0, 0],  # Sun-Thu (protocol: Sun=0)
            "inside": True,
            "outside": False,
            "in_start_time": {"hour": 8, "min": 0},
            "in_end_time": {"hour": 18, "min": 30},
            "out_start_time": {"hour": 0, "min": 0},
            "out_end_time": {"hour": 0, "min": 0},
        }
        schedule = Schedule.from_dict(data)
        assert schedule.index == 2
        assert schedule.enabled is True
        assert schedule.days_of_week == [1, 1, 1, 1, 1, 0, 0]
        assert schedule.inside is True
        assert schedule.outside is False
        assert schedule.start_hour == 8
        assert schedule.start_min == 0
        assert schedule.end_hour == 18
        assert schedule.end_min == 30

    def test_from_dict_outside_sensor_times(self):
        """An outside-sensor entry reads its times from the out* fields."""
        data = {
            "index": 4,
            "enabled": "1",
            "daysOfWeek": [1, 1, 1, 1, 1, 1, 1],
            "inside": False,
            "outside": True,
            "in_start_time": {"hour": 0, "min": 0},
            "in_end_time": {"hour": 0, "min": 0},
            "out_start_time": {"hour": 7, "min": 15},
            "out_end_time": {"hour": 19, "min": 45},
        }
        schedule = Schedule.from_dict(data)
        assert schedule.outside is True
        assert schedule.inside is False
        assert (schedule.start_hour, schedule.start_min) == (7, 15)
        assert (schedule.end_hour, schedule.end_min) == (19, 45)

    def test_from_dict_no_sensor_uses_defaults(self):
        """An entry for neither sensor falls back to the default window."""
        schedule = Schedule.from_dict({"index": 5, "inside": False, "outside": False})
        assert (schedule.start_hour, schedule.start_min) == (6, 0)
        assert (schedule.end_hour, schedule.end_min) == (22, 0)

    def test_from_dict_legacy_bitmask(self):
        """Should handle legacy bitmask format for days_of_week."""
        data = {
            "index": 2,
            "enabled": "1",
            "daysOfWeek": 0b0011111,  # Legacy bitmask
            "inside": True,
            "outside": False,
            "in_start_time": {"hour": 8, "min": 0},
            "in_end_time": {"hour": 18, "min": 30},
        }
        schedule = Schedule.from_dict(data)
        # Bitmask 0b0011111 = 31 -> Sun..Thu on, Fri/Sat off, as real bools.
        # `True == 1`, so the isinstance check is what actually pins the
        # normalization.
        assert schedule.days_of_week == [True, True, True, True, True, False, False]
        assert all(isinstance(day, bool) for day in schedule.days_of_week)

    def test_from_dict_normalizes_days_to_seven_bools(self):
        """Wire days become exactly 7 booleans, whatever their flag spelling."""
        schedule = Schedule.from_dict(
            {
                "index": 0,
                "inside": True,
                "in_start_time": {"hour": 6, "min": 0},
                "in_end_time": {"hour": 22, "min": 0},
                "daysOfWeek": [1, 0, "yes", "no", "off", 2, True],
            }
        )
        assert schedule.days_of_week == [True, False, True, False, False, True, True]
        assert all(isinstance(day, bool) for day in schedule.days_of_week)

    def test_from_dict_reads_string_day_flags_as_flags_not_truthiness(self):
        """`"0"` disables the day - bool("0") is True, which would enable it."""
        schedule = Schedule.from_dict(
            {
                "index": 0,
                "inside": True,
                "in_start_time": {"hour": 6, "min": 0},
                "in_end_time": {"hour": 22, "min": 0},
                "daysOfWeek": ["1", "0", "1", "0", "1", "0", "1"],
            }
        )
        assert schedule.days_of_week == [True, False, True, False, True, False, True]

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [
            ("1", True),
            (1, True),
            (True, True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            (0, False),
            (False, False),
            ("false", False),
            ("off", False),
            (["what"], False),
        ],
        ids=repr,
    )
    def test_from_dict_reads_enabled_like_every_other_wire_flag(self, flag, expected):
        """`enabled` is read the way its daysOfWeek sibling is.

        A bespoke `== "1"` read "true"/"yes"/"on" as *disabled*, and left an
        integer 1/0 in a field declared `enabled: bool`.
        """
        schedule = Schedule.from_dict(
            {
                "index": 0,
                "enabled": flag,
                "inside": True,
                "in_start_time": {"hour": 6, "min": 0},
                "in_end_time": {"hour": 22, "min": 0},
            }
        )

        assert schedule.enabled is expected
        assert schedule.to_dict()["enabled"] == ("1" if expected else "0")

    @pytest.mark.parametrize("bad_day", ["", None, "maybe", [1], 1.5, {}])
    def test_from_dict_rejects_unreadable_day_flags(self, bad_day):
        """A day element that is not a 0/1 flag is rejected, not guessed at."""
        with pytest.raises(ValueError, match=r"daysOfWeek\[3\] must be 0 or 1"):
            Schedule.from_dict(
                {
                    "index": 0,
                    "inside": True,
                    "in_start_time": {"hour": 6, "min": 0},
                    "in_end_time": {"hour": 22, "min": 0},
                    "daysOfWeek": [1, 1, 1, bad_day, 1, 1, 1],
                }
            )

    def test_to_dict_writes_wire_ints_for_bool_days(self):
        """Booleans in memory are written back to the wire as 1/0.

        `True == 1`, so equality alone cannot tell the two apart - the type
        check is what pins the wire format (docs/protocol.md uses 1/0).
        """
        schedule = Schedule(index=0, inside=True, days_of_week=[True, False] * 3 + [True])
        days = schedule.to_dict()["daysOfWeek"]
        assert days == [1, 0, 1, 0, 1, 0, 1]
        assert all(type(day) is int for day in days)

    def test_to_dict_serializes_days_as_json_ints(self):
        """What actually goes on the wire is `1`/`0`, never `true`/`false`."""
        schedule = Schedule(index=0, inside=True, days_of_week=[True, False] * 3 + [True])
        assert '"daysOfWeek": [1, 0, 1, 0, 1, 0, 1]' in json.dumps(schedule.to_dict())

    def test_from_dict_coerces_numeric_strings(self):
        """Numeric strings from a sloppy client are coerced, not stored raw."""
        schedule = Schedule.from_dict(
            {
                "index": "3",
                "inside": True,
                "in_start_time": {"hour": "7", "min": "5"},
                "in_end_time": {"hour": 19, "min": 0},
            }
        )
        assert schedule.index == 3
        assert (schedule.start_hour, schedule.start_min) == (7, 5)

    def test_roundtrip_conversion(self):
        """to_dict and from_dict should be inverses."""
        original = Schedule(
            index=3,
            enabled=False,
            days_of_week=[1, 0, 1, 0, 1, 0, 1],
            inside=True,
            outside=False,
            start_hour=7,
            start_min=30,
        )
        converted = Schedule.from_dict(original.to_dict())
        assert converted.index == original.index
        assert converted.enabled == original.enabled
        assert converted.days_of_week == original.days_of_week
        assert converted.inside == original.inside
        assert converted.outside == original.outside
        assert converted.start_hour == original.start_hour
        assert converted.start_min == original.start_min

    def test_is_day_active_monday(self):
        """Should correctly check if Monday is active."""
        # Protocol format: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
        # Monday only = [0, 1, 0, 0, 0, 0, 0]
        schedule = Schedule(index=0, enabled=True, days_of_week=[0, 1, 0, 0, 0, 0, 0])
        assert schedule.is_day_active(0) is True  # Monday (Python weekday 0)
        assert schedule.is_day_active(1) is False  # Tuesday
        assert schedule.is_day_active(6) is False  # Sunday

    def test_is_day_active_weekend(self):
        """Should correctly check weekend days."""
        # Protocol format: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
        # Sat + Sun = [1, 0, 0, 0, 0, 0, 1]
        schedule = Schedule(index=0, enabled=True, days_of_week=[1, 0, 0, 0, 0, 0, 1])
        assert schedule.is_day_active(5) is True  # Saturday (Python weekday 5)
        assert schedule.is_day_active(6) is True  # Sunday (Python weekday 6)
        assert schedule.is_day_active(0) is False  # Monday

    def test_is_day_active_disabled_schedule(self):
        """Disabled schedule should never be active."""
        schedule = Schedule(index=0, enabled=False, days_of_week=[1, 1, 1, 1, 1, 1, 1])
        assert schedule.is_day_active(0) is False
        assert schedule.is_day_active(6) is False

    def test_coinciding_ends_are_a_full_twenty_four_hour_window(self):
        """All 1440 minutes, not 1439 and not 0.

        The window end is exclusive, so `[start, end)` covers at most 1439
        of the day's minutes: `00:00-23:59` looks like a 24/7 entry and
        blocks the sensor for exactly the minute 23:59. Two schedule-script
        tests failed for that one minute a day. Coinciding ends are
        therefore the only spelling a true 24h window has.
        """
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=[True] * 7,
            inside=True,
            start_hour=0,
            start_min=0,
            end_hour=0,
            end_min=0,
        )

        allowed = [
            minute
            for minute in range(1440)
            if schedule.is_sensor_allowed("inside", minute // 60, minute % 60, 0)
        ]

        assert len(allowed) == 1440

    def test_an_exclusive_end_window_covers_every_minute_but_the_last(self):
        """The shape that made 00:00-23:59 wrong, stated directly."""
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=[True] * 7,
            inside=True,
            start_hour=0,
            start_min=0,
            end_hour=23,
            end_min=59,
        )

        assert schedule.is_sensor_allowed("inside", 23, 58, 0) is True
        assert schedule.is_sensor_allowed("inside", 23, 59, 0) is False

    def test_is_sensor_allowed_inside_normal_hours(self):
        """Inside sensor should be allowed during scheduled hours."""
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=[1, 1, 1, 1, 1, 1, 1],
            inside=True,
            outside=False,
            start_hour=8,
            end_hour=20,
        )
        # 10:00 on Monday should be allowed
        assert schedule.is_sensor_allowed("inside", 10, 0, 0) is True
        # 6:00 on Monday should NOT be allowed
        assert schedule.is_sensor_allowed("inside", 6, 0, 0) is False
        # 21:00 on Monday should NOT be allowed
        assert schedule.is_sensor_allowed("inside", 21, 0, 0) is False
        # Outside sensor should NOT be allowed (this entry is for inside only)
        assert schedule.is_sensor_allowed("outside", 10, 0, 0) is False

    def test_is_sensor_allowed_outside_normal_hours(self):
        """Outside sensor should be allowed during scheduled hours."""
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=[1, 1, 1, 1, 1, 1, 1],
            inside=False,
            outside=True,
            start_hour=9,
            end_hour=17,
        )
        assert schedule.is_sensor_allowed("outside", 12, 0, 0) is True
        assert schedule.is_sensor_allowed("outside", 8, 0, 0) is False
        # Inside sensor should NOT be allowed (this entry is for outside only)
        assert schedule.is_sensor_allowed("inside", 12, 0, 0) is False

    def test_is_sensor_allowed_inactive_day(self):
        """The right sensor at the right time is still blocked on an off day."""
        # Protocol day order: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]; Monday off
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=[1, 0, 1, 1, 1, 1, 1],
            inside=True,
            start_hour=8,
            end_hour=20,
        )
        # 10:00 within the window, but Monday (Python weekday 0) is inactive
        assert schedule.is_sensor_allowed("inside", 10, 0, 0) is False
        # The same time on Tuesday is allowed
        assert schedule.is_sensor_allowed("inside", 10, 0, 1) is True

    def test_is_sensor_allowed_crosses_midnight(self):
        """Should handle schedules that cross midnight."""
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=[1, 1, 1, 1, 1, 1, 1],
            inside=True,
            outside=False,
            start_hour=22,
            end_hour=6,
        )
        # 23:00 should be allowed
        assert schedule.is_sensor_allowed("inside", 23, 0, 0) is True
        # 2:00 should be allowed
        assert schedule.is_sensor_allowed("inside", 2, 0, 0) is True
        # 12:00 should NOT be allowed
        assert schedule.is_sensor_allowed("inside", 12, 0, 0) is False

    @pytest.mark.parametrize(
        ("hour", "minute", "allowed"),
        [(21, 59, False), (22, 0, True), (5, 59, True), (6, 0, False)],
        ids=["before-start", "at-start", "last-minute", "at-end"],
    )
    def test_a_midnight_crossing_window_at_its_four_edge_minutes(self, hour, minute, allowed):
        """Assert *at* the boundary, not only inside it.

        This test's older half asserts 23:00, 02:00 and 12:00 - the
        interior of the window and one point far outside it - so both
        comparisons that define the window were unpinned: `>= start` -> `>`
        and `< end` -> `<=` each survived the whole suite. The window is
        inclusive at the start and exclusive at the end, and those are the
        two minutes that say so.
        """
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=[1, 1, 1, 1, 1, 1, 1],
            inside=True,
            outside=False,
            start_hour=22,
            end_hour=6,
        )

        assert schedule.is_sensor_allowed("inside", hour, minute, 0) is allowed

    @pytest.mark.parametrize(
        ("hour", "minute", "allowed"),
        [(7, 59, False), (8, 0, True), (16, 59, True), (17, 0, False)],
        ids=["before-start", "at-start", "last-minute", "at-end"],
    )
    def test_a_normal_window_at_its_four_edge_minutes(self, hour, minute, allowed):
        """The same four edges on the non-crossing branch."""
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=[1, 1, 1, 1, 1, 1, 1],
            inside=True,
            outside=False,
            start_hour=8,
            end_hour=17,
        )

        assert schedule.is_sensor_allowed("inside", hour, minute, 0) is allowed


class TestScheduleFromDictRejectsHostileInput:
    """Wire schedules are untrusted: malformed ones must never be stored.

    A stored malformed schedule used to raise IndexError/TypeError later,
    during sensor evaluation - a quiet, persistent denial of the
    sensor-trigger feature.
    """

    def test_non_mapping_payload_rejected(self):
        with pytest.raises(ValueError, match="Schedule must be an object"):
            Schedule.from_dict(["not", "a", "schedule"])

    @pytest.mark.parametrize(
        "days",
        [[1], [1, 1, 1, 1, 1, 1, 1, 1], "1111111", None],
        ids=["too-short", "too-long", "string", "null"],
    )
    def test_wrong_length_days_rejected(self, days):
        with pytest.raises(ValueError, match="daysOfWeek must be a list of 7 values"):
            Schedule.from_dict({"index": 0, "inside": True, "daysOfWeek": days})

    def test_truncated_days_cannot_reach_is_day_active(self):
        """The exact reproduction: a 1-element daysOfWeek is refused."""
        with pytest.raises(ValueError):
            Schedule.from_dict({"index": 0, "inside": True, "daysOfWeek": [1]})

    @pytest.mark.parametrize(
        ("index", "message"),
        [
            ("not-a-number", "index must be a number"),
            (None, "index must be a number"),
            (-1, "index must be between 0 and 255"),
            (256, "index must be between 0 and 255"),
        ],
    )
    def test_out_of_range_or_non_numeric_index_rejected(self, index, message):
        with pytest.raises(ValueError, match=message):
            Schedule.from_dict({"index": index, "inside": True})

    def test_non_mapping_time_rejected(self):
        with pytest.raises(ValueError, match="start time must be an object"):
            Schedule.from_dict({"index": 0, "inside": True, "in_start_time": [6, 0]})

    @pytest.mark.parametrize(
        ("time_field", "message"),
        [
            ({"hour": "six", "min": 0}, "start time hour must be a number"),
            ({"hour": 24, "min": 0}, "start time hour must be between 0 and 23"),
            ({"hour": 6, "min": 60}, "start time minute must be between 0 and 59"),
        ],
    )
    def test_out_of_range_times_rejected(self, time_field, message):
        with pytest.raises(ValueError, match=message):
            Schedule.from_dict({"index": 0, "inside": True, "in_start_time": time_field})

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
            (
                {"index": 0, "inside": True},
                "missing required field 'in_start_time'",
            ),
            (
                {"index": 0, "inside": True, "in_start_time": {"hour": 6, "min": 0}},
                "missing required field 'in_end_time'",
            ),
            (
                {"index": 0, "outside": True, "out_end_time": {"hour": 22, "min": 0}},
                "missing required field 'out_start_time'",
            ),
            (
                {
                    "index": 0,
                    "inside": True,
                    "in_start_time": {},
                    "in_end_time": {"hour": 22, "min": 0},
                },
                "start time must specify hour",
            ),
        ],
    )
    def test_missing_time_window_is_rejected_not_invented(self, payload, message):
        """A selected sensor with no window must fail, not get 06:00-22:00."""
        with pytest.raises(ValueError, match=message):
            Schedule.from_dict(payload)

    def test_entry_for_neither_sensor_keeps_the_placeholder_window(self):
        """With no sensor selected the entry gates nothing, so defaults are fine."""
        schedule = Schedule.from_dict({"index": 4})
        assert (schedule.inside, schedule.outside) == (False, False)
        assert (schedule.start_hour, schedule.start_min) == (6, 0)
        assert (schedule.end_hour, schedule.end_min) == (22, 0)
        # The absent-daysOfWeek default was unobserved on both sides.
        assert schedule.days_of_week == [True] * 7

    def test_from_dict_no_days_defaults_to_every_day(self):
        """An absent daysOfWeek means "every day" here too."""
        assert Schedule.from_dict({}).days_of_week == [True] * 7

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
        """The legacy bitmask branch, pinned across its range."""
        assert Schedule.from_dict({"index": 0, "daysOfWeek": mask}).days_of_week == expected

    @pytest.mark.parametrize("mask", [-1, -128, 128, 2**64], ids=repr)
    def test_from_dict_out_of_range_bitmask_is_rejected(self, mask):
        """A negative mask must not fail open to all seven days."""
        with pytest.raises(ValueError, match="daysOfWeek"):
            Schedule.from_dict({"index": 0, "daysOfWeek": mask})

    def test_from_dict_inside_wins_when_both_sensors_are_flagged(self):
        """Statement order is the rule, so pin it on both parsers."""
        schedule = Schedule.from_dict(
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

        assert (schedule.start_hour, schedule.end_hour) == (6, 7)

    def test_infinite_index_is_rejected_with_the_number_reason(self):
        """int(inf) raises OverflowError, which must not escape the validator."""
        with pytest.raises(ValueError, match="index must be a number"):
            Schedule.from_dict({"index": float("inf"), "inside": True})


# ============================================================================
# DoorSimulatorState Tests
# ============================================================================


class TestDoorSimulatorState:
    """Tests for DoorSimulatorState dataclass."""

    def test_default_values(self):
        """Default state should have sensible defaults."""
        state = DoorSimulatorState()
        assert state.door_status == DOOR_STATE_CLOSED
        assert state.power is True
        assert state.inside is True
        assert state.outside is True
        assert state.auto is True
        assert state.battery_percent == 100
        assert state.hold_time == 2
        # Counters should default to 0
        assert state.total_open_cycles == 0
        assert state.total_auto_retracts == 0

    def test_battery_config_default(self):
        """State should have default battery config."""
        state = DoorSimulatorState()
        assert state.battery_config is not None
        assert state.battery_config.charge_rate == 1.0
        assert state.battery_config.discharge_rate == 0.1

    def test_battery_config_custom(self):
        """State should accept custom battery config."""
        config = BatteryConfig(charge_rate=2.0, discharge_rate=0.5)
        state = DoorSimulatorState(battery_config=config)
        assert state.battery_config.charge_rate == 2.0
        assert state.battery_config.discharge_rate == 0.5

    def test_battery_presence(self):
        """State should track battery and AC presence."""
        state = DoorSimulatorState()
        assert state.battery_present is True  # Default
        assert state.ac_present is True  # Default

        state = DoorSimulatorState(battery_present=False, ac_present=False)
        assert state.battery_present is False
        assert state.ac_present is False

    def test_get_settings(self):
        """get_settings should return protocol format."""
        state = DoorSimulatorState(power=True, inside=False, auto=True)
        settings = state.get_settings()
        assert settings[FIELD_POWER] == "1"
        assert settings[FIELD_INSIDE] == "0"
        assert settings[FIELD_AUTO] == "1"
        assert FIELD_TZ in settings

    def test_get_settings_converts_timezone_to_posix(self):
        """With the tz cache ready, settings carry the POSIX rule."""
        from powerpetdoor import tz_utils

        tz_utils.init_timezone_cache_sync()
        state = DoorSimulatorState(timezone="America/New_York")
        assert state.get_settings()[FIELD_TZ] == "EST5EDT,M3.2.0,M11.1.0"

    def test_get_settings_keeps_raw_timezone_when_cache_uninitialized(self, monkeypatch):
        """Without the tz cache, the raw stored value is reported."""
        from powerpetdoor.simulator import state as state_module

        monkeypatch.setattr(state_module, "is_cache_initialized", lambda: False)
        state = DoorSimulatorState(timezone="America/New_York")
        assert state.get_settings()[FIELD_TZ] == "America/New_York"

    def test_get_settings_keeps_raw_timezone_when_unconvertible(self, monkeypatch):
        """An unconvertible zone keeps the raw stored value."""
        from powerpetdoor.simulator import state as state_module

        monkeypatch.setattr(state_module, "is_cache_initialized", lambda: True)
        monkeypatch.setattr(state_module, "get_posix_tz_string", lambda tz: None)
        state = DoorSimulatorState(timezone="America/New_York")
        assert state.get_settings()[FIELD_TZ] == "America/New_York"

    def test_get_notifications(self):
        """get_notifications should return notification settings."""
        state = DoorSimulatorState(
            sensor_on_indoor=True,
            sensor_off_indoor=False,
            low_battery=True,
        )
        notifications = state.get_notifications()
        assert notifications["sensorOnIndoorNotificationsEnabled"] == "1"
        assert notifications["sensorOffIndoorNotificationsEnabled"] == "0"
        assert notifications["lowBatteryNotificationsEnabled"] == "1"

    def test_get_schedule_list(self):
        """get_schedule_list should return list of schedule indices."""
        state = DoorSimulatorState()
        state.schedules[0] = Schedule(index=0)
        state.schedules[1] = Schedule(index=1)
        result = state.get_schedule_list()
        assert len(result) == 2
        assert result[0] == 0
        assert result[1] == 1

    def test_get_schedule_list_is_sorted_by_slot(self):
        """Slots created out of order still come back in slot order.

        The store is a dict, so insertion order leaked into the reply and
        `door.refresh_schedules` inherited it - leaving the public
        `door.schedules` sorted or unsorted depending on which code path
        last touched it.
        """
        state = DoorSimulatorState()
        for index in (5, 1, 3):
            state.schedules[index] = Schedule(index=index)

        assert state.get_schedule_list() == [1, 3, 5]

    def test_is_sensor_allowed_by_schedule_no_auto(self):
        """Should allow all sensors when auto/timers disabled."""
        state = DoorSimulatorState(auto=False)
        state.schedules[0] = Schedule(
            index=0,
            enabled=True,
            inside=True,
            outside=False,
            start_hour=22,
            end_hour=6,
        )
        # Even at 12:00 when schedule says no, auto=False allows it
        assert state.is_sensor_allowed_by_schedule("inside") is True

    def test_is_sensor_allowed_by_schedule_no_schedules(self):
        """Should allow all sensors when no schedules defined."""
        state = DoorSimulatorState(auto=True)
        assert state.is_sensor_allowed_by_schedule("inside") is True
        assert state.is_sensor_allowed_by_schedule("outside") is True


# ============================================================================
# Sensor Detection Model Tests
# ============================================================================


class TestSensorDetectionModel:
    """Tests for the sensor detection model (inside_sensor_active, outside_sensor_active)."""

    def test_sensor_active_defaults(self):
        """Sensor active flags should default to False."""
        state = DoorSimulatorState()
        assert state.inside_sensor_active is False
        assert state.outside_sensor_active is False

    def test_sensor_active_property_none_active(self):
        """sensor_active property should be False when no sensors active."""
        state = DoorSimulatorState()
        assert state.sensor_active is False

    def test_sensor_active_property_inside_active(self):
        """sensor_active property should be True when inside sensor active."""
        state = DoorSimulatorState(inside_sensor_active=True)
        assert state.sensor_active is True

    def test_sensor_active_property_outside_active(self):
        """sensor_active property should be True when outside sensor active."""
        state = DoorSimulatorState(outside_sensor_active=True)
        assert state.sensor_active is True

    def test_sensor_active_property_both_active(self):
        """sensor_active property should be True when both sensors active."""
        state = DoorSimulatorState(inside_sensor_active=True, outside_sensor_active=True)
        assert state.sensor_active is True


class TestIsSensorBlockingClose:
    """Tests for is_sensor_blocking_close() method."""

    def test_no_sensors_active(self):
        """Should not block close when no sensors are active."""
        state = DoorSimulatorState()
        assert state.is_sensor_blocking_close() is False

    def test_inside_sensor_active_and_enabled(self):
        """Inside sensor should block close when active AND enabled."""
        state = DoorSimulatorState(inside_sensor_active=True, inside=True)
        assert state.is_sensor_blocking_close() is True

    def test_inside_sensor_active_but_disabled(self):
        """Inside sensor should NOT block close when active but disabled."""
        state = DoorSimulatorState(inside_sensor_active=True, inside=False)
        assert state.is_sensor_blocking_close() is False

    def test_outside_sensor_active_and_enabled(self):
        """Outside sensor should block close when active, enabled, and NOT safety-locked."""
        state = DoorSimulatorState(outside_sensor_active=True, outside=True, safety_lock=False)
        assert state.is_sensor_blocking_close() is True

    def test_outside_sensor_active_but_disabled(self):
        """Outside sensor should NOT block close when active but disabled."""
        state = DoorSimulatorState(outside_sensor_active=True, outside=False, safety_lock=False)
        assert state.is_sensor_blocking_close() is False

    def test_outside_sensor_active_but_safety_locked(self):
        """Outside sensor should NOT block close when safety-locked."""
        state = DoorSimulatorState(outside_sensor_active=True, outside=True, safety_lock=True)
        assert state.is_sensor_blocking_close() is False

    def test_inside_blocks_even_with_safety_lock(self):
        """Inside sensor should block close even when safety_lock is on."""
        state = DoorSimulatorState(
            inside_sensor_active=True,
            inside=True,
            safety_lock=True,  # Safety lock only affects outside sensor
        )
        assert state.is_sensor_blocking_close() is True

    def test_both_sensors_active_one_disabled(self):
        """Should block if at least one active sensor is enabled."""
        # Inside active and enabled, outside active but disabled
        state = DoorSimulatorState(
            inside_sensor_active=True, inside=True, outside_sensor_active=True, outside=False
        )
        assert state.is_sensor_blocking_close() is True

        # Inside active but disabled, outside active and enabled
        state = DoorSimulatorState(
            inside_sensor_active=True,
            inside=False,
            outside_sensor_active=True,
            outside=True,
            safety_lock=False,
        )
        assert state.is_sensor_blocking_close() is True

    def test_both_sensors_active_both_disabled(self):
        """Should NOT block if both active sensors are disabled."""
        state = DoorSimulatorState(
            inside_sensor_active=True, inside=False, outside_sensor_active=True, outside=False
        )
        assert state.is_sensor_blocking_close() is False

    def test_cmd_lockout_prevents_inside_blocking(self):
        """When cmd_lockout is enabled, inside sensor should NOT block close."""
        state = DoorSimulatorState(inside_sensor_active=True, inside=True, cmd_lockout=True)
        assert state.is_sensor_blocking_close() is False

    def test_cmd_lockout_prevents_outside_blocking(self):
        """When cmd_lockout is enabled, outside sensor should NOT block close."""
        state = DoorSimulatorState(
            outside_sensor_active=True, outside=True, safety_lock=False, cmd_lockout=True
        )
        assert state.is_sensor_blocking_close() is False

    def test_cmd_lockout_disabled_allows_blocking(self):
        """When cmd_lockout is disabled, sensors should block as normal."""
        state = DoorSimulatorState(inside_sensor_active=True, inside=True, cmd_lockout=False)
        assert state.is_sensor_blocking_close() is True


# ============================================================================
# Timezone Resolution Tests (POSIX wire values)
# ============================================================================


class TestGetTzinfo:
    """Tests for DoorSimulatorState.get_tzinfo timezone resolution."""

    def test_iana_name_resolves_directly(self):
        """An IANA timezone name resolves without the cache."""
        state = DoorSimulatorState(timezone="America/New_York")
        assert state.get_tzinfo().key == "America/New_York"

    def test_posix_wire_value_maps_to_iana(self):
        """A POSIX TZ string (as stored by SET_TIMEZONE) maps back to IANA."""
        from powerpetdoor import tz_utils

        tz_utils.init_timezone_cache_sync()
        posix = tz_utils.get_posix_tz_string("America/New_York")
        assert posix == "EST5EDT,M3.2.0,M11.1.0"

        state = DoorSimulatorState(timezone=posix)
        tzinfo = state.get_tzinfo()
        # Any IANA zone sharing this POSIX rule is acceptable; it must
        # observe US-eastern UTC offsets, not UTC.
        from datetime import datetime, timedelta

        january = datetime(2026, 1, 15, 12, 0, tzinfo=tzinfo)
        assert january.utcoffset() == timedelta(hours=-5)
        july = datetime(2026, 7, 15, 12, 0, tzinfo=tzinfo)
        assert july.utcoffset() == timedelta(hours=-4)

    def test_stale_posix_mapping_falls_back_to_utc(self, monkeypatch, caplog):
        """A POSIX value mapping to a nonexistent IANA zone falls back to UTC."""
        import logging

        from powerpetdoor.simulator import state as state_module

        # The cache maps the POSIX rule to a zone tzdata cannot resolve
        monkeypatch.setattr(state_module, "find_iana_for_posix", lambda posix: "Not/A_Zone")
        state = DoorSimulatorState(timezone="XXX-1YYY,M3.2.0,M11.1.0")
        with caplog.at_level(logging.WARNING, logger="powerpetdoor.simulator.state"):
            assert state.get_tzinfo().key == "UTC"
        assert "falling back to UTC" in caplog.text

    def test_unresolvable_timezone_falls_back_to_utc_with_single_warning(self, caplog):
        """Unknown values fall back to UTC and warn exactly once per value."""
        import logging

        state = DoorSimulatorState(timezone="Not/A/Real_Zone")
        with caplog.at_level(logging.WARNING, logger="powerpetdoor.simulator.state"):
            assert state.get_tzinfo().key == "UTC"
            assert state.get_tzinfo().key == "UTC"

        warnings = [rec for rec in caplog.records if "falling back to UTC" in rec.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.parametrize("timezone", [["x"], {"a": 1}, 5, None])
    def test_unhashable_or_wrong_typed_timezone_falls_back_to_utc(self, timezone, caplog):
        """Schedule evaluation runs on every sensor trigger and must never raise.

        SET_TIMEZONE validates its input now, but a value set directly (or
        by a future caller) must still degrade to UTC rather than propagate
        a TypeError out of is_sensor_allowed_by_schedule.
        """
        import logging

        from powerpetdoor import tz_utils

        tz_utils.init_timezone_cache_sync()
        state = DoorSimulatorState(timezone=timezone)  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="powerpetdoor.simulator.state"):
            assert state.get_tzinfo().key == "UTC"
        assert state.is_sensor_allowed_by_schedule("inside") is True

    def test_warning_re_emitted_for_new_value(self, caplog):
        """Changing to a different bad value warns again."""
        import logging

        state = DoorSimulatorState(timezone="Bad/Zone_One")
        with caplog.at_level(logging.WARNING, logger="powerpetdoor.simulator.state"):
            state.get_tzinfo()
            state.timezone = "Bad/Zone_Two"
            state.get_tzinfo()

        warnings = [rec for rec in caplog.records if "falling back to UTC" in rec.getMessage()]
        assert len(warnings) == 2

    def test_schedule_evaluation_with_posix_timezone(self):
        """Schedules evaluate (not UTC-fallback) after a wire SET_TIMEZONE value."""
        from powerpetdoor import tz_utils

        tz_utils.init_timezone_cache_sync()
        state = DoorSimulatorState(timezone="EST5EDT,M3.2.0,M11.1.0", auto=True)

        # A 24/7 schedule window allows the sensor regardless of local time
        state.schedules[0] = Schedule(
            index=0,
            enabled=True,
            days_of_week=[1, 1, 1, 1, 1, 1, 1],
            inside=True,
            start_hour=0,
            start_min=0,
            end_hour=0,
            end_min=0,
        )
        assert state.is_sensor_allowed_by_schedule("inside") is True

        # No scheduled day never allows it
        state.schedules[0] = Schedule(
            index=0,
            enabled=True,
            days_of_week=[0, 0, 0, 0, 0, 0, 0],
            inside=True,
            start_hour=0,
            start_min=0,
            end_hour=0,
            end_min=0,
        )
        assert state.is_sensor_allowed_by_schedule("inside") is False

    def test_hold_time_accepts_float(self):
        """hold_time is a float field."""
        state = DoorSimulatorState(hold_time=2.5)
        assert state.hold_time == 2.5
        assert DoorSimulatorState.__dataclass_fields__["hold_time"].type is float
