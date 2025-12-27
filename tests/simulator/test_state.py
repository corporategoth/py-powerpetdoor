# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator state module (state.py)."""
from __future__ import annotations

import pytest

from powerpetdoor.simulator import (
    DoorSimulatorState,
    Schedule,
    DoorTimingConfig,
)
from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    FIELD_POWER,
    FIELD_INSIDE,
    FIELD_AUTO,
    FIELD_TZ,
)


# ============================================================================
# DoorTimingConfig Tests
# ============================================================================

class TestDoorTimingConfig:
    """Tests for DoorTimingConfig dataclass."""

    def test_default_values(self):
        """Default timing values should be reasonable."""
        config = DoorTimingConfig()
        assert config.rise_time == 1.5
        assert config.default_hold_time == 10
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
# Schedule Tests
# ============================================================================

class TestSchedule:
    """Tests for Schedule dataclass."""

    def test_default_values(self):
        """Default schedule should cover all days 6AM-10PM."""
        schedule = Schedule(index=0)
        assert schedule.index == 0
        assert schedule.enabled is True
        assert schedule.days_of_week == 0b1111111  # All days
        assert schedule.inside_start_hour == 6
        assert schedule.inside_end_hour == 22
        assert schedule.outside_start_hour == 6
        assert schedule.outside_end_hour == 22

    def test_to_dict(self):
        """Should convert to protocol dict format."""
        schedule = Schedule(index=1, enabled=True)
        result = schedule.to_dict()
        assert result["index"] == 1
        assert result["enabled"] == "1"
        assert "daysOfWeek" in result
        assert "in_start_time" in result
        assert "in_end_time" in result
        assert "out_start_time" in result
        assert "out_end_time" in result

    def test_from_dict(self):
        """Should create from protocol dict format."""
        data = {
            "index": 2,
            "enabled": "1",
            "daysOfWeek": 0b0011111,  # Mon-Fri
            "in_start_time": {"hour": 8, "min": 0},
            "in_end_time": {"hour": 18, "min": 30},
            "out_start_time": {"hour": 9, "min": 0},
            "out_end_time": {"hour": 17, "min": 0},
        }
        schedule = Schedule.from_dict(data)
        assert schedule.index == 2
        assert schedule.enabled is True
        assert schedule.days_of_week == 0b0011111
        assert schedule.inside_start_hour == 8
        assert schedule.inside_start_min == 0
        assert schedule.inside_end_hour == 18
        assert schedule.inside_end_min == 30

    def test_roundtrip_conversion(self):
        """to_dict and from_dict should be inverses."""
        original = Schedule(
            index=3,
            enabled=False,
            days_of_week=0b1010101,
            inside_start_hour=7,
            inside_start_min=30,
        )
        converted = Schedule.from_dict(original.to_dict())
        assert converted.index == original.index
        assert converted.enabled == original.enabled
        assert converted.days_of_week == original.days_of_week
        assert converted.inside_start_hour == original.inside_start_hour
        assert converted.inside_start_min == original.inside_start_min

    def test_is_day_active_monday(self):
        """Should correctly check if Monday is active."""
        # Monday = bit 2 (0b0000010)
        schedule = Schedule(index=0, enabled=True, days_of_week=0b0000010)
        assert schedule.is_day_active(0) is True  # Monday
        assert schedule.is_day_active(1) is False  # Tuesday
        assert schedule.is_day_active(6) is False  # Sunday

    def test_is_day_active_weekend(self):
        """Should correctly check weekend days."""
        # Sat=64 (0b1000000), Sun=1 (0b0000001)
        schedule = Schedule(index=0, enabled=True, days_of_week=0b1000001)
        assert schedule.is_day_active(5) is True  # Saturday
        assert schedule.is_day_active(6) is True  # Sunday
        assert schedule.is_day_active(0) is False  # Monday

    def test_is_day_active_disabled_schedule(self):
        """Disabled schedule should never be active."""
        schedule = Schedule(index=0, enabled=False, days_of_week=0b1111111)
        assert schedule.is_day_active(0) is False
        assert schedule.is_day_active(6) is False

    def test_is_sensor_allowed_inside_normal_hours(self):
        """Inside sensor should be allowed during scheduled hours."""
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=0b1111111,
            inside_start_hour=8,
            inside_end_hour=20,
        )
        # 10:00 on Monday should be allowed
        assert schedule.is_sensor_allowed("inside", 10, 0, 0) is True
        # 6:00 on Monday should NOT be allowed
        assert schedule.is_sensor_allowed("inside", 6, 0, 0) is False
        # 21:00 on Monday should NOT be allowed
        assert schedule.is_sensor_allowed("inside", 21, 0, 0) is False

    def test_is_sensor_allowed_outside_normal_hours(self):
        """Outside sensor should be allowed during scheduled hours."""
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=0b1111111,
            outside_start_hour=9,
            outside_end_hour=17,
        )
        assert schedule.is_sensor_allowed("outside", 12, 0, 0) is True
        assert schedule.is_sensor_allowed("outside", 8, 0, 0) is False

    def test_is_sensor_allowed_crosses_midnight(self):
        """Should handle schedules that cross midnight."""
        schedule = Schedule(
            index=0,
            enabled=True,
            days_of_week=0b1111111,
            inside_start_hour=22,
            inside_end_hour=6,
        )
        # 23:00 should be allowed
        assert schedule.is_sensor_allowed("inside", 23, 0, 0) is True
        # 2:00 should be allowed
        assert schedule.is_sensor_allowed("inside", 2, 0, 0) is True
        # 12:00 should NOT be allowed
        assert schedule.is_sensor_allowed("inside", 12, 0, 0) is False


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
        assert state.battery_percent == 85
        assert state.hold_time == 10

    def test_get_settings(self):
        """get_settings should return protocol format."""
        state = DoorSimulatorState(power=True, inside=False, auto=True)
        settings = state.get_settings()
        assert settings[FIELD_POWER] == "1"
        assert settings[FIELD_INSIDE] == "0"
        assert settings[FIELD_AUTO] == "1"
        assert FIELD_TZ in settings

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
        """get_schedule_list should return list of schedule dicts."""
        state = DoorSimulatorState()
        state.schedules[0] = Schedule(index=0)
        state.schedules[1] = Schedule(index=1)
        result = state.get_schedule_list()
        assert len(result) == 2
        assert result[0]["index"] == 0
        assert result[1]["index"] == 1

    def test_is_sensor_allowed_by_schedule_no_auto(self):
        """Should allow all sensors when auto/timers disabled."""
        state = DoorSimulatorState(auto=False)
        state.schedules[0] = Schedule(
            index=0,
            enabled=True,
            inside_start_hour=22,
            inside_end_hour=6,
        )
        # Even at 12:00 when schedule says no, auto=False allows it
        assert state.is_sensor_allowed_by_schedule("inside") is True

    def test_is_sensor_allowed_by_schedule_no_schedules(self):
        """Should allow all sensors when no schedules defined."""
        state = DoorSimulatorState(auto=True)
        assert state.is_sensor_allowed_by_schedule("inside") is True
        assert state.is_sensor_allowed_by_schedule("outside") is True
