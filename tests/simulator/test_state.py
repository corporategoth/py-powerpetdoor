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
    BatteryConfig,
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
        # Bitmask 0b0011111 = 31 converts to list [1, 1, 1, 1, 1, 0, 0]
        assert schedule.days_of_week == [1, 1, 1, 1, 1, 0, 0]

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
        state = DoorSimulatorState(
            outside_sensor_active=True,
            outside=True,
            safety_lock=False
        )
        assert state.is_sensor_blocking_close() is True

    def test_outside_sensor_active_but_disabled(self):
        """Outside sensor should NOT block close when active but disabled."""
        state = DoorSimulatorState(
            outside_sensor_active=True,
            outside=False,
            safety_lock=False
        )
        assert state.is_sensor_blocking_close() is False

    def test_outside_sensor_active_but_safety_locked(self):
        """Outside sensor should NOT block close when safety-locked."""
        state = DoorSimulatorState(
            outside_sensor_active=True,
            outside=True,
            safety_lock=True
        )
        assert state.is_sensor_blocking_close() is False

    def test_inside_blocks_even_with_safety_lock(self):
        """Inside sensor should block close even when safety_lock is on."""
        state = DoorSimulatorState(
            inside_sensor_active=True,
            inside=True,
            safety_lock=True  # Safety lock only affects outside sensor
        )
        assert state.is_sensor_blocking_close() is True

    def test_both_sensors_active_one_disabled(self):
        """Should block if at least one active sensor is enabled."""
        # Inside active and enabled, outside active but disabled
        state = DoorSimulatorState(
            inside_sensor_active=True,
            inside=True,
            outside_sensor_active=True,
            outside=False
        )
        assert state.is_sensor_blocking_close() is True

        # Inside active but disabled, outside active and enabled
        state = DoorSimulatorState(
            inside_sensor_active=True,
            inside=False,
            outside_sensor_active=True,
            outside=True,
            safety_lock=False
        )
        assert state.is_sensor_blocking_close() is True

    def test_both_sensors_active_both_disabled(self):
        """Should NOT block if both active sensors are disabled."""
        state = DoorSimulatorState(
            inside_sensor_active=True,
            inside=False,
            outside_sensor_active=True,
            outside=False
        )
        assert state.is_sensor_blocking_close() is False

    def test_cmd_lockout_prevents_inside_blocking(self):
        """When cmd_lockout is enabled, inside sensor should NOT block close."""
        state = DoorSimulatorState(
            inside_sensor_active=True,
            inside=True,
            cmd_lockout=True
        )
        assert state.is_sensor_blocking_close() is False

    def test_cmd_lockout_prevents_outside_blocking(self):
        """When cmd_lockout is enabled, outside sensor should NOT block close."""
        state = DoorSimulatorState(
            outside_sensor_active=True,
            outside=True,
            safety_lock=False,
            cmd_lockout=True
        )
        assert state.is_sensor_blocking_close() is False

    def test_cmd_lockout_disabled_allows_blocking(self):
        """When cmd_lockout is disabled, sensors should block as normal."""
        state = DoorSimulatorState(
            inside_sensor_active=True,
            inside=True,
            cmd_lockout=False
        )
        assert state.is_sensor_blocking_close() is True
