"""State dataclasses for Power Pet Door simulator.

This module contains all the state-related dataclasses used by the simulator.
"""

from dataclasses import dataclass, field
from datetime import datetime

from ..tz_utils import get_posix_tz_string, is_cache_initialized
from ..const import (
    DOOR_STATE_CLOSED,
    FIELD_INDEX,
    FIELD_ENABLED,
    FIELD_DAYSOFWEEK,
    FIELD_INSIDE_PREFIX,
    FIELD_OUTSIDE_PREFIX,
    FIELD_START_TIME_SUFFIX,
    FIELD_END_TIME_SUFFIX,
    FIELD_HOUR,
    FIELD_MINUTE,
    FIELD_POWER,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_AUTO,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_CMD_LOCKOUT,
    FIELD_AUTORETRACT,
    FIELD_TZ,
    FIELD_HOLD_OPEN_TIME,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
)

# Note: FIELD_INSIDE and FIELD_OUTSIDE are used both as:
# 1. Settings fields for sensor enable/disable (string "0"/"1")
# 2. Schedule entry fields for which sensor the entry applies to (bool)


@dataclass
class DoorTimingConfig:
    """Configurable timing for door operations (all times in seconds)."""

    # Time for door to rise from closed to fully open
    rise_time: float = 1.5

    # Default hold time before auto-close (can be overridden by state.hold_time)
    default_hold_time: int = 10

    # Time for each phase of closing
    slowing_time: float = 0.3
    closing_top_time: float = 0.4
    closing_mid_time: float = 0.4

    # Delay between sensor re-triggers resetting the hold timer
    sensor_retrigger_window: float = 0.5


@dataclass
class Schedule:
    """A door schedule entry.

    Each schedule entry controls ONE sensor (inside or outside) for specific
    days and times. The `inside` and `outside` fields indicate which sensor
    this entry applies to.

    Protocol format:
        - daysOfWeek: list of 7 ints [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
        - inside/outside: bool flags for which sensor
        - Time fields use prefix (in/out) + StartTime/EndTime
    """

    index: int
    enabled: bool = True
    # List of 7 values [Sun, Mon, Tue, Wed, Thu, Fri, Sat] where 1=active
    days_of_week: list = field(default_factory=lambda: [1, 1, 1, 1, 1, 1, 1])
    # Which sensor this entry is for
    inside: bool = False
    outside: bool = False
    # Time window (same times used for whichever sensor is enabled)
    start_hour: int = 6
    start_min: int = 0
    end_hour: int = 22
    end_min: int = 0

    def to_dict(self) -> dict:
        """Convert to protocol dict format."""
        result = {
            FIELD_INDEX: self.index,
            FIELD_ENABLED: "1" if self.enabled else "0",
            FIELD_DAYSOFWEEK: self.days_of_week.copy(),
            FIELD_INSIDE: self.inside,
            FIELD_OUTSIDE: self.outside,
        }
        # Set time fields for the appropriate sensor(s)
        if self.inside:
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = {
                FIELD_HOUR: self.start_hour,
                FIELD_MINUTE: self.start_min,
            }
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = {
                FIELD_HOUR: self.end_hour,
                FIELD_MINUTE: self.end_min,
            }
        else:
            # Default empty times for inside
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = {
                FIELD_HOUR: 0, FIELD_MINUTE: 0,
            }
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = {
                FIELD_HOUR: 0, FIELD_MINUTE: 0,
            }

        if self.outside:
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = {
                FIELD_HOUR: self.start_hour,
                FIELD_MINUTE: self.start_min,
            }
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = {
                FIELD_HOUR: self.end_hour,
                FIELD_MINUTE: self.end_min,
            }
        else:
            # Default empty times for outside
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = {
                FIELD_HOUR: 0, FIELD_MINUTE: 0,
            }
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = {
                FIELD_HOUR: 0, FIELD_MINUTE: 0,
            }

        return result

    @classmethod
    def from_dict(cls, data: dict) -> "Schedule":
        """Create from protocol dict format."""
        inside = data.get(FIELD_INSIDE, False)
        outside = data.get(FIELD_OUTSIDE, False)

        # Get time from the appropriate prefix
        if inside:
            start = data.get(f"{FIELD_INSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}", {})
            end = data.get(f"{FIELD_INSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}", {})
        elif outside:
            start = data.get(f"{FIELD_OUTSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}", {})
            end = data.get(f"{FIELD_OUTSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}", {})
        else:
            start = {}
            end = {}

        # Handle daysOfWeek - could be list or legacy bitmask
        days = data.get(FIELD_DAYSOFWEEK, [1, 1, 1, 1, 1, 1, 1])
        if isinstance(days, int):
            # Convert bitmask to list [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
            days = [(days >> i) & 1 for i in range(7)]

        return cls(
            index=data.get(FIELD_INDEX, 0),
            enabled=data.get(FIELD_ENABLED, "1") == "1" if isinstance(data.get(FIELD_ENABLED), str) else bool(data.get(FIELD_ENABLED, True)),
            days_of_week=days,
            inside=inside,
            outside=outside,
            start_hour=start.get(FIELD_HOUR, 6),
            start_min=start.get(FIELD_MINUTE, 0),
            end_hour=end.get(FIELD_HOUR, 22),
            end_min=end.get(FIELD_MINUTE, 0),
        )

    def is_day_active(self, weekday: int) -> bool:
        """Check if schedule is active on a given weekday.

        Args:
            weekday: 0=Monday, 1=Tuesday, ..., 6=Sunday (Python's weekday format)

        Returns:
            True if the schedule is active on this day.
        """
        if not self.enabled:
            return False
        # Convert Python weekday (Mon=0...Sun=6) to protocol format (Sun=0, Mon=1, ..., Sat=6)
        day_index = (weekday + 1) % 7
        return bool(self.days_of_week[day_index])

    def is_sensor_allowed(self, sensor: str, hour: int, minute: int, weekday: int) -> bool:
        """Check if a sensor trigger is allowed at the given time.

        Args:
            sensor: "inside" or "outside"
            hour: Hour (0-23)
            minute: Minute (0-59)
            weekday: Python weekday (0=Monday, 6=Sunday)

        Returns:
            True if the sensor is allowed to trigger at this time.
        """
        # Check if this entry is for the requested sensor
        if sensor == "inside" and not self.inside:
            return False
        if sensor == "outside" and not self.outside:
            return False

        if not self.is_day_active(weekday):
            return False

        current_minutes = hour * 60 + minute
        start = self.start_hour * 60 + self.start_min
        end = self.end_hour * 60 + self.end_min

        # Handle schedules that cross midnight
        if start <= end:
            return start <= current_minutes < end
        else:
            return current_minutes >= start or current_minutes < end


@dataclass
class DoorSimulatorState:
    """State of the simulated door."""

    # Door position
    door_status: str = DOOR_STATE_CLOSED

    # Sensors
    power: bool = True
    inside: bool = True
    outside: bool = True
    auto: bool = True
    autoretract: bool = True  # Enable by default for testing
    safety_lock: bool = False
    cmd_lockout: bool = False

    # Battery
    battery_percent: int = 85
    battery_present: bool = True
    ac_present: bool = True

    # Settings
    timezone: str = "America/New_York"
    hold_time: int = 10
    sensor_trigger_voltage: int = 100
    sleep_sensor_trigger_voltage: int = 50

    # Stats
    total_open_cycles: int = 1234
    total_auto_retracts: int = 56

    # Firmware
    fw_major: int = 1
    fw_minor: int = 2
    fw_patch: int = 3

    # Remote/reset info
    has_remote_id: bool = True
    has_remote_key: bool = True
    reset_reason: str = "POWER_ON"  # Could be: POWER_ON, WATCHDOG, SOFT_RESET, etc.

    # Notifications
    sensor_on_indoor: bool = True
    sensor_off_indoor: bool = False
    sensor_on_outdoor: bool = True
    sensor_off_outdoor: bool = False
    low_battery: bool = True

    # Schedules (stored by index)
    schedules: dict = field(default_factory=dict)

    # Timing configuration
    timing: DoorTimingConfig = field(default_factory=DoorTimingConfig)

    # Obstruction simulation state
    obstruction_pending: bool = False

    # Pet presence simulation (keeps door open)
    pet_in_doorway: bool = False

    def get_settings(self) -> dict:
        """Get full settings dict."""
        # Convert IANA timezone to POSIX format if cache is initialized
        tz_value = self.timezone
        if is_cache_initialized():
            posix_tz = get_posix_tz_string(self.timezone)
            if posix_tz:
                tz_value = posix_tz

        return {
            FIELD_POWER: "1" if self.power else "0",
            FIELD_INSIDE: "1" if self.inside else "0",
            FIELD_OUTSIDE: "1" if self.outside else "0",
            FIELD_AUTO: "1" if self.auto else "0",
            FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: "1" if self.safety_lock else "0",
            FIELD_CMD_LOCKOUT: "1" if self.cmd_lockout else "0",
            FIELD_AUTORETRACT: "1" if self.autoretract else "0",
            FIELD_TZ: tz_value,
            FIELD_HOLD_OPEN_TIME: self.hold_time,
            FIELD_SENSOR_TRIGGER_VOLTAGE: self.sensor_trigger_voltage,
            FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE: self.sleep_sensor_trigger_voltage,
        }

    def get_notifications(self) -> dict:
        """Get notifications settings."""
        return {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "1" if self.sensor_on_indoor else "0",
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: "1" if self.sensor_off_indoor else "0",
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: "1" if self.sensor_on_outdoor else "0",
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: "1" if self.sensor_off_outdoor else "0",
            FIELD_LOW_BATTERY_NOTIFICATIONS: "1" if self.low_battery else "0",
        }

    def get_schedule_list(self) -> list:
        """Get list of all schedules."""
        return [sched.to_dict() for sched in self.schedules.values()]

    def is_sensor_allowed_by_schedule(self, sensor: str) -> bool:
        """Check if a sensor trigger is allowed based on schedules.

        When auto (timersEnabled) is on and schedules exist, sensor triggers
        are only allowed during the scheduled time windows.

        Args:
            sensor: "inside" or "outside"

        Returns:
            True if the sensor trigger is allowed.
        """
        # If timers are disabled, allow all triggers
        if not self.auto:
            return True

        # If no schedules, allow all triggers
        if not self.schedules:
            return True

        # Check if any schedule allows this sensor at the current time
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(self.timezone)
        except Exception:
            # Fallback to UTC if timezone is invalid
            import zoneinfo
            tz = zoneinfo.ZoneInfo("UTC")

        now = datetime.now(tz)

        for schedule in self.schedules.values():
            if schedule.is_sensor_allowed(sensor, now.hour, now.minute, now.weekday()):
                return True

        return False
