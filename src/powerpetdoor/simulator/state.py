# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""State dataclasses for Power Pet Door simulator.

This module contains all the state-related dataclasses used by the simulator.
"""

import logging
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime

from ..const import (
    DOOR_OPTION_AUTORETRACT,
    DOOR_STATE_CLOSED,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_CMD_LOCKOUT,
    FIELD_DAYSOFWEEK,
    FIELD_ENABLED,
    FIELD_END_TIME_SUFFIX,
    FIELD_HOLD_OPEN_TIME,
    FIELD_INDEX,
    FIELD_INSIDE,
    FIELD_INSIDE_PREFIX,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_PREFIX,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_START_TIME_SUFFIX,
    FIELD_TZ,
)
from ..i18n import t
from ..schedule import (
    MAX_SCHEDULE_INDEX,
    SCHEDULE_WIRE_FROM_DEVICE,
    build_schedule_payload,
    coerce_schedule_days,
    coerce_schedule_flag,
    coerce_schedule_int,
    coerce_schedule_time,
    require_schedule_field,
    wire_bool_string,
)
from ..tz_utils import find_iana_for_posix, get_posix_tz_string, is_cache_initialized

logger = logging.getLogger(__name__)

# Note: FIELD_INSIDE and FIELD_OUTSIDE are used both as:
# 1. Settings fields for sensor enable/disable (string "true"/"false")
# 2. Schedule entry fields for which sensor the entry applies to (int 1/0)
#
# That difference is the device's own inconsistency, not ours: the same
# concept is spelled differently per command, so each emitter names the
# speller it needs (`wire_bool_string` / `wire_int_flag`, both in
# powerpetdoor.schedule) rather than normalizing.
#
# The schedule-field coercion helpers (and MAX_SCHEDULE_INDEX) live in
# powerpetdoor.schedule so this parser and the library's
# powerpetdoor.door.Schedule parser share one implementation: hardening
# either one hardens both.

#: The last real minute of the day, and how a probed unit's FACTORY schedule
#: happens to spell the end of its full-day windows (``in 00:00-23:59``,
#: ``out 00:00-23:59`` on firmware 1.7.18).
#:
#: It is a spelling, not a special case. The schedule engine is measured to
#: be strictly ``start <= now < end``, so a window ending here really does
#: leave the sensor off for that final minute. Use :data:`WHOLE_DAY_END_HOUR`
#: to mean the end of the day.
END_OF_DAY_HOUR = 23
END_OF_DAY_MINUTE = 59

#: The end of the day, unambiguously. **Measured against firmware 1.7.18**:
#: the device honours hour 24 (``20:00-24:00`` reports the sensor enabled at
#: 21:07) and preserves it (write ``00:00-24:00``, read back
#: ``00:00-24:00``), so this is what a whole-day window should be written as.
WHOLE_DAY_END_HOUR = 24
WHOLE_DAY_END_MINUTE = 0


@dataclass
class DoorTimingConfig:
    """Configurable timing for door operations (all times in seconds)."""

    # Time for door to rise from closed to fully open
    rise_time: float = 1.5

    # Default hold time before auto-close (can be overridden by state.hold_time)
    default_hold_time: int = 2

    # Time for each phase of closing
    slowing_time: float = 0.3
    #: The brief first closing state, before the flap has moved. Measured on
    #: firmware 1.7.18 at roughly 180ms between DOOR_CLOSING and
    #: DOOR_CLOSING_TOP_OPEN.
    closing_start_time: float = 0.2
    closing_top_time: float = 0.4
    closing_mid_time: float = 0.4

    # Delay between sensor re-triggers resetting the hold timer
    sensor_retrigger_window: float = 0.5


@dataclass
class BatteryConfig:
    """Configuration for battery charge/discharge simulation.

    Rates are in percent per minute. Set to 0 to disable automatic changes.
    """

    # Charge rate when AC is present (percent per minute)
    # Default: 1% per minute = ~100 minutes to full charge
    charge_rate: float = 1.0

    # Discharge rate when AC is absent (percent per minute)
    # Default: 0.1% per minute = ~1000 minutes (~16 hours) to empty
    discharge_rate: float = 0.1

    # How often to update battery level (seconds)
    update_interval: float = 60.0


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
    #: Exactly 7 booleans, ``[Sun, Mon, Tue, Wed, Thu, Fri, Sat]``. Strict
    #: Python types in memory: the 1/0 wire spelling is applied once, at
    #: the serialization boundary, and ``from_dict`` normalizes incoming
    #: wire data back to booleans.
    days_of_week: list[bool] = field(default_factory=lambda: [True] * 7)
    # Which sensor this entry is for
    inside: bool = False
    outside: bool = False
    # Time window (same times used for whichever sensor is enabled)
    start_hour: int = 6
    start_min: int = 0
    end_hour: int = 22
    end_min: int = 0

    def to_dict(self) -> dict:
        """Serialize for the wire, device-to-client.

        The simulator plays the *device*, so this emits what a real door
        replies with — spelled by
        :data:`~powerpetdoor.schedule.SCHEDULE_WIRE_FROM_DEVICE`. **Verified
        against firmware 1.7.18**: ``enabled``, ``inside`` and ``outside``
        come back as the integers ``1``/``0``, where the library SENDS them
        as JSON booleans. That is deliberate: opposite directions are not
        twins. Do not unify them.
        """
        return build_schedule_payload(
            SCHEDULE_WIRE_FROM_DEVICE,
            index=self.index,
            enabled=self.enabled,
            days_of_week=self.days_of_week,
            inside=self.inside,
            outside=self.outside,
            start=(self.start_hour, self.start_min),
            end=(self.end_hour, self.end_min),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Schedule":
        """Create from protocol dict format.

        Everything here comes off the wire and is therefore untrusted: each
        field is validated and coerced so a stored schedule can never raise
        later, when a sensor trigger evaluates it (``is_day_active`` indexes
        ``days_of_week``; ``is_sensor_allowed`` does arithmetic on the
        times).

        Raises:
            ValueError: If the payload is not a schedule-shaped mapping, or
                a field is not coercible / out of its protocol range. The
                SET_SCHEDULE handler turns this into the standard error
                envelope.
        """
        if not isinstance(data, dict):
            raise ValueError(
                t(
                    "simulator.state.schedule_must_object_got",
                    "Schedule must be an object, got {data!r}",
                    data=data,
                )
            )

        # Identity and day mask first, so a bad index reports the bad index
        # rather than whatever the time block happens to complain about.
        index = coerce_schedule_int(data.get(FIELD_INDEX, 0), "index", MAX_SCHEDULE_INDEX)
        days_of_week = coerce_schedule_days(data.get(FIELD_DAYSOFWEEK, [1, 1, 1, 1, 1, 1, 1]))

        inside = coerce_schedule_flag(data.get(FIELD_INSIDE, False), FIELD_INSIDE)
        outside = coerce_schedule_flag(data.get(FIELD_OUTSIDE, False), FIELD_OUTSIDE)

        # Get time from the appropriate prefix
        if inside:
            prefix = FIELD_INSIDE_PREFIX
        elif outside:
            prefix = FIELD_OUTSIDE_PREFIX
        else:
            prefix = ""

        if prefix:
            # A sensor is selected, so the window that gates it is required:
            # defaulting an absent one to 06:00-22:00 would grant 16 hours of
            # access nobody asked for.
            start_hour, start_min = coerce_schedule_time(
                require_schedule_field(data, f"{prefix}{FIELD_START_TIME_SUFFIX}"), "start time"
            )
            end_hour, end_min = coerce_schedule_time(
                require_schedule_field(data, f"{prefix}{FIELD_END_TIME_SUFFIX}"), "end time"
            )
        else:
            # Neither sensor selected: the entry gates nothing, so the
            # placeholder window is harmless.
            start_hour, start_min = 6, 0
            end_hour, end_min = 22, 0

        return cls(
            index=index,
            # Read like every other wire flag rather than with a bespoke
            # `== "1"`: `true`/`yes`/`on` are as valid a spelling here as
            # they are for daysOfWeek right next to it.
            enabled=coerce_schedule_flag(data.get(FIELD_ENABLED, True), FIELD_ENABLED),
            days_of_week=days_of_week,
            inside=inside,
            outside=outside,
            start_hour=start_hour,
            start_min=start_min,
            end_hour=end_hour,
            end_min=end_min,
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

        # 23:59 is deliberately NOT special. An earlier version treated it as
        # end-of-day, reasoning that the factory schedule is `00:00-23:59`
        # and plainly means "always". That was an inference, and once the
        # device was measured to accept AND preserve `24:00` - write
        # `00:00-24:00` and a real door hands back `00:00-24:00` unchanged -
        # there was no longer any reason to guess. The rule below is taken
        # literally, so an entry that really does end at 23:59 really does
        # leave the sensor off for that final minute.

        # Everything below IS measured, against firmware 1.7.18 with
        # timersEnabled on, by reading the sensor flags the engine writes
        # through to (GET_SETTINGS reports the door's own verdict):
        #
        #   20:00-24:00 -> enabled at 21:07   hour 24 is a real end-of-day
        #   21:01-21:31 -> enabled at 21:01   start is INCLUSIVE
        #   20:31-21:01 -> disabled at 21:01  end is EXCLUSIVE
        #   16:01-16:01 -> disabled           start == end is EMPTY
        #   20:00-00:00 -> disabled           end 00:00 is EMPTY, not midnight
        #   23:00-21:30 -> disabled on the day it names AND on the next day,
        #                  so an inverted window is neither a same-day wrap
        #                  nor a spill into tomorrow. It is nothing at all.
        #
        # The engine is exactly `start <= now < end`, so any window whose end
        # does not exceed its start matches no minute. The device stores such
        # an entry perfectly and simply never acts on it.
        if end <= start:
            return False
        return start <= current_minutes < end


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
    battery_percent: int = 100
    battery_present: bool = True
    ac_present: bool = True

    # Battery simulation configuration
    battery_config: BatteryConfig = field(default_factory=BatteryConfig)

    # Settings
    # timezone holds either an IANA name (set locally) or a POSIX TZ string
    # (as received on the wire via SET_TIMEZONE); get_tzinfo() resolves both.
    timezone: str = "America/New_York"
    hold_time: float = 2.0
    sensor_trigger_voltage: int = 100
    sleep_sensor_trigger_voltage: int = 50

    # Stats (default to 0 for fresh simulator)
    total_open_cycles: int = 0
    total_auto_retracts: int = 0

    # Firmware/Hardware info
    fw_major: int = 1
    fw_minor: int = 2
    fw_patch: int = 3
    # Verified against firmware 1.7.18: GET_HW_INFO's `fwInfo` object is
    # all integers - `ver=1 rev=1 fw_maj=1 fw_min=7 fw_pat=18` - so these
    # are ints, not the version-like strings docs/protocol.md once showed.
    hw_ver: int = 1  # Hardware version
    hw_rev: int = 1  # Hardware revision

    # Remote/reset info
    has_remote_id: bool = True
    has_remote_key: bool = True
    reset_reason: str = "POWER_ON"  # Could be: POWER_ON, WATCHDOG, SOFT_RESET, etc.

    # Notifications
    sensor_on_indoor: bool = False
    sensor_off_indoor: bool = False
    sensor_on_outdoor: bool = False
    sensor_off_outdoor: bool = False
    low_battery: bool = True

    # Schedules (stored by index)
    schedules: dict[int, Schedule] = field(default_factory=dict)

    # Timing configuration
    timing: DoorTimingConfig = field(default_factory=DoorTimingConfig)

    # Sensor detection simulation state (for obstruction/pet simulation)
    # These represent the physical sensor detecting something (e.g., pet in doorway)
    inside_sensor_active: bool = False
    outside_sensor_active: bool = False

    # Internal: remembers which timezone value already produced a UTC-fallback
    # warning, so the warning is logged once per value.
    _tz_warned_for: str | None = field(default=None, repr=False, compare=False)

    @property
    def sensor_active(self) -> bool:
        """Check if any sensor is currently detecting something."""
        return self.inside_sensor_active or self.outside_sensor_active

    def is_sensor_blocking_close(self) -> bool:
        """Check if an active sensor should prevent closing.

        Returns True if a sensor is:
        - Active (detecting something)
        - Enabled (the sensor setting is on)
        - For outside sensor: not safety-locked
        - cmd_lockout is disabled (when enabled, sensors don't block close)
        """
        # When cmd_lockout is enabled, sensor detection doesn't prevent closing
        # (cmd_lockout=True means pet_proximity_keep_open=False)
        if self.cmd_lockout:
            return False
        # Inside sensor blocks if: active AND sensor enabled
        if self.inside_sensor_active and self.inside:
            return True
        # Outside sensor blocks if: active AND sensor enabled AND NOT safety-locked
        if self.outside_sensor_active and self.outside and not self.safety_lock:
            return True
        return False

    def wire_timezone(self) -> str:
        """The timezone as the device puts it on the wire: **POSIX**.

        **Verified against firmware 1.7.18**: the door answers
        ``EST5EDT,M3.2.0,M11.1.0``, never an IANA name. The simulator
        stores whichever form it was given (``get_tzinfo`` reads both) and
        converts here, so ``GET_SETTINGS``, ``GET_TIMEZONE`` and the
        timezone broadcast cannot disagree.

        Falls back to the stored value when the timezone cache has not been
        initialized, or when the zone has no POSIX rule to convert to.
        :meth:`~powerpetdoor.simulator.server.DoorSimulator.start` warms
        the cache, so a running simulator emits POSIX.
        """
        if is_cache_initialized():
            posix_tz = get_posix_tz_string(self.timezone)
            if posix_tz:
                return posix_tz
        return self.timezone

    def get_settings(self) -> dict:
        """Get full settings dict."""
        # Field-by-field spellings verified against firmware 1.7.18: the six
        # flags are "true"/"false" STRINGS, doorOptions/holdOpenTime/the two
        # voltages are INTS, and tz is a POSIX string. Same key set the real
        # unit returned, in the same spellings.
        return {
            FIELD_POWER: wire_bool_string(self.power),
            FIELD_INSIDE: wire_bool_string(self.inside),
            FIELD_OUTSIDE: wire_bool_string(self.outside),
            FIELD_AUTO: wire_bool_string(self.auto),
            FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: wire_bool_string(self.safety_lock),
            FIELD_CMD_LOCKOUT: wire_bool_string(self.cmd_lockout),
            # A BITFIELD, not a flag: verified against firmware 1.7.18,
            # DISABLE_AUTORETRACT leaves this 0 and ENABLE_AUTORETRACT
            # leaves it 2. Other bits exist but are unidentified, so the
            # simulator sets only the one it knows.
            FIELD_AUTORETRACT: DOOR_OPTION_AUTORETRACT if self.autoretract else 0,
            FIELD_TZ: self.wire_timezone(),
            FIELD_HOLD_OPEN_TIME: int(self.hold_time * 100),  # Convert to centiseconds
            FIELD_SENSOR_TRIGGER_VOLTAGE: self.sensor_trigger_voltage,
            FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE: self.sleep_sensor_trigger_voltage,
        }

    def get_notifications(self) -> dict:
        """Get notifications settings.

        All five flags are ``"true"``/``"false"`` **strings**, verified
        against firmware 1.7.18. Note the asymmetry with the write path:
        ``SET_NOTIFICATIONS`` demands JSON *booleans* and silently ignores
        strings.
        """
        return {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: wire_bool_string(self.sensor_on_indoor),
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: wire_bool_string(self.sensor_off_indoor),
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: wire_bool_string(self.sensor_on_outdoor),
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: wire_bool_string(self.sensor_off_outdoor),
            FIELD_LOW_BATTERY_NOTIFICATIONS: wire_bool_string(self.low_battery),
        }

    def get_schedule_list(self) -> list[int]:
        """Get list of schedule indices (matches real device behavior).

        Sorted by slot: the store is a dict, so a client that created slot
        5 before slot 1 got ``[5, 1]`` back - insertion order, not slot
        order, which no array-of-slots firmware would produce.
        """
        return sorted(self.schedules.keys())

    def get_tzinfo(self) -> zoneinfo.ZoneInfo:
        """Resolve the configured timezone to a usable tzinfo.

        ``timezone`` may be an IANA name (e.g. "America/New_York") or a wire
        POSIX TZ string (e.g. "EST5EDT,M3.2.0,M11.1.0" as received via
        SET_TIMEZONE). POSIX values are mapped back to an IANA zone via
        :func:`~powerpetdoor.tz_utils.find_iana_for_posix` (requires the
        timezone cache to be initialized). Falls back to UTC - with a
        warning logged once per value - when neither resolves.
        """
        # TypeError is caught too (defence in depth): SET_TIMEZONE validates
        # its input now, but schedule evaluation runs on every sensor trigger
        # and must never be the thing that raises. A non-string reaching here
        # (constructed directly, not off the wire) falls through to UTC
        # instead of propagating out of is_sensor_allowed_by_schedule.
        try:
            return zoneinfo.ZoneInfo(self.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, ValueError):
            pass

        try:
            iana = find_iana_for_posix(self.timezone)
        except TypeError:
            iana = None
        if iana:
            try:
                return zoneinfo.ZoneInfo(iana)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                pass

        if self._tz_warned_for != self.timezone:
            logger.warning(
                t(
                    "simulator.state.simulator_cannot_resolve_timezone_falling",
                    "Simulator: cannot resolve timezone %r; falling back to UTC for schedule evaluation",
                ),
                self.timezone,
            )
            self._tz_warned_for = self.timezone
        return zoneinfo.ZoneInfo("UTC")

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
        now = datetime.now(self.get_tzinfo())

        for schedule in self.schedules.values():
            if schedule.is_sensor_allowed(sensor, now.hour, now.minute, now.weekday()):
                return True

        return False
