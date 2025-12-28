# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""High-level Power Pet Door interface.

This module provides a Pythonic facade over the low-level PowerPetDoorClient,
offering cached state, type-safe enums, and simple async methods.

Example usage:
    from powerpetdoor import PowerPetDoor, DoorStatus

    async def main():
        door = PowerPetDoor("192.168.1.100")
        await door.connect()

        print(f"Door is {door.status.name}")
        print(f"Battery: {door.battery_percent}%")

        if door.is_closed:
            await door.open()

        await door.set_hold_time(15)
        await door.disconnect()
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from .client import PowerPetDoorClient
from .const import (
    COMMAND,
    CONFIG,
    # Commands
    CMD_CLOSE,
    CMD_DELETE_SCHEDULE,
    CMD_DISABLE_AUTO,
    CMD_DISABLE_AUTORETRACT,
    CMD_DISABLE_CMD_LOCKOUT,
    CMD_DISABLE_INSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_ENABLE_AUTO,
    CMD_ENABLE_AUTORETRACT,
    CMD_ENABLE_CMD_LOCKOUT,
    CMD_ENABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SETTINGS,
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_TIMEZONE,
    # Door states
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_IDLE,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    # Fields
    FIELD_AC_PRESENT,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD_LOCKOUT,
    FIELD_DAYSOFWEEK,
    FIELD_ENABLED,
    FIELD_END_TIME_SUFFIX,
    FIELD_HOUR,
    FIELD_INDEX,
    FIELD_INSIDE,
    FIELD_INSIDE_PREFIX,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_MINUTE,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_PREFIX,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_SCHEDULE,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_START_TIME_SUFFIX,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
)

logger = logging.getLogger(__name__)


class DoorStatus(Enum):
    """Door operational states."""

    IDLE = DOOR_STATE_IDLE
    CLOSED = DOOR_STATE_CLOSED
    RISING = DOOR_STATE_RISING
    SLOWING = DOOR_STATE_SLOWING
    HOLDING = DOOR_STATE_HOLDING
    KEEPUP = DOOR_STATE_KEEPUP
    CLOSING_TOP_OPEN = DOOR_STATE_CLOSING_TOP_OPEN
    CLOSING_MID_OPEN = DOOR_STATE_CLOSING_MID_OPEN

    @classmethod
    def from_string(cls, value: str) -> "DoorStatus":
        """Convert a string status to enum."""
        for status in cls:
            if status.value == value:
                return status
        return cls.CLOSED  # Default fallback


@dataclass
class NotificationSettings:
    """Door notification configuration."""

    inside_on: bool = False
    inside_off: bool = False
    outside_on: bool = False
    outside_off: bool = False
    low_battery: bool = False


@dataclass
class BatteryInfo:
    """Battery status information."""

    percent: int = 100
    present: bool = True
    ac_present: bool = True

    @property
    def charging(self) -> bool:
        """Whether the battery is charging (AC present and not full)."""
        return self.ac_present and self.percent < 100

    @property
    def discharging(self) -> bool:
        """Whether the battery is discharging (no AC and battery present)."""
        return not self.ac_present and self.present


@dataclass
class ScheduleTime:
    """A time of day for scheduling."""

    hour: int = 0
    minute: int = 0

    def to_dict(self) -> dict[str, int]:
        """Convert to protocol dict format."""
        return {FIELD_HOUR: self.hour, FIELD_MINUTE: self.minute}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleTime":
        """Create from protocol dict."""
        return cls(hour=data.get(FIELD_HOUR, 0), minute=data.get(FIELD_MINUTE, 0))


@dataclass
class Schedule:
    """A door schedule entry.

    Each schedule entry controls ONE sensor (inside or outside) for specific
    days and a time window. The `inside` and `outside` fields indicate which
    sensor this entry applies to.

    Protocol format:
        - daysOfWeek: list of 7 ints [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
        - inside/outside: bool flags for which sensor
        - Time fields use prefix (in/out) + StartTime/EndTime
    """

    index: int = 0
    enabled: bool = True
    # List of 7 values [Sun, Mon, Tue, Wed, Thu, Fri, Sat] where 1=active
    days_of_week: list = field(default_factory=lambda: [1, 1, 1, 1, 1, 1, 1])
    # Which sensor this entry is for
    inside: bool = False
    outside: bool = False
    # Time window (applies to whichever sensor is enabled)
    start: ScheduleTime = field(default_factory=lambda: ScheduleTime(6, 0))
    end: ScheduleTime = field(default_factory=lambda: ScheduleTime(22, 0))

    def to_dict(self) -> dict[str, Any]:
        """Convert to protocol dict format."""
        result = {
            FIELD_INDEX: self.index,
            FIELD_ENABLED: self.enabled,
            FIELD_DAYSOFWEEK: self.days_of_week.copy() if isinstance(self.days_of_week, list) else self.days_of_week,
            FIELD_INSIDE: self.inside,
            FIELD_OUTSIDE: self.outside,
        }

        # Set time fields for the appropriate sensor(s)
        if self.inside:
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = self.start.to_dict()
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = self.end.to_dict()
        else:
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = {FIELD_HOUR: 0, FIELD_MINUTE: 0}
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = {FIELD_HOUR: 0, FIELD_MINUTE: 0}

        if self.outside:
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = self.start.to_dict()
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = self.end.to_dict()
        else:
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = {FIELD_HOUR: 0, FIELD_MINUTE: 0}
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = {FIELD_HOUR: 0, FIELD_MINUTE: 0}

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Schedule":
        """Create from protocol dict."""
        inside = data.get(FIELD_INSIDE, False)
        outside = data.get(FIELD_OUTSIDE, False)

        # Get time from the appropriate prefix
        if inside:
            start = ScheduleTime.from_dict(
                data.get(f"{FIELD_INSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}", {})
            )
            end = ScheduleTime.from_dict(
                data.get(f"{FIELD_INSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}", {})
            )
        elif outside:
            start = ScheduleTime.from_dict(
                data.get(f"{FIELD_OUTSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}", {})
            )
            end = ScheduleTime.from_dict(
                data.get(f"{FIELD_OUTSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}", {})
            )
        else:
            start = ScheduleTime()
            end = ScheduleTime()

        # Handle daysOfWeek - could be list or legacy bitmask
        days = data.get(FIELD_DAYSOFWEEK, [1, 1, 1, 1, 1, 1, 1])
        if isinstance(days, int):
            # Convert bitmask to list [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
            days = [(days >> i) & 1 for i in range(7)]

        # Handle enabled field - could be bool or string
        enabled = data.get(FIELD_ENABLED, True)
        if isinstance(enabled, str):
            enabled = enabled == "1"

        return cls(
            index=data.get(FIELD_INDEX, 0),
            enabled=enabled,
            days_of_week=days,
            inside=inside,
            outside=outside,
            start=start,
            end=end,
        )


class PowerPetDoor:
    """High-level interface to a Power Pet Door.

    Provides a Pythonic API for controlling and monitoring a Power Pet Door,
    with cached state updated via callbacks and simple async methods.

    Example:
        door = PowerPetDoor("192.168.1.100")
        await door.connect()

        # Read state via properties
        print(door.status)
        print(door.battery_percent)

        # Control via async methods
        await door.open()
        await door.set_power(True)

        await door.disconnect()
    """

    def __init__(
        self,
        host: str,
        port: int = 3000,
        *,
        keepalive: float = 30.0,
        timeout: float = 10.0,
        reconnect: float = 5.0,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        """Initialize PowerPetDoor.

        Args:
            host: IP address or hostname of the door.
            port: TCP port (default 3000).
            keepalive: Seconds between keepalive pings (0 to disable).
            timeout: Seconds to wait for responses.
            reconnect: Seconds to wait before reconnecting on disconnect.
            loop: Optional event loop (uses current loop if not provided).
        """
        self._host = host
        self._port = port
        self._client = PowerPetDoorClient(
            host=host,
            port=port,
            keepalive=keepalive,
            timeout=timeout,
            reconnect=reconnect,
            loop=loop,
        )

        # Cached state
        self._status: DoorStatus = DoorStatus.CLOSED
        self._power: bool = True
        self._inside_sensor: bool = True
        self._outside_sensor: bool = True
        self._auto: bool = False
        self._safety_lock: bool = False
        self._autoretract: bool = True
        self._pet_proximity_keep_open: bool = False
        self._hold_time: float = 2.0
        self._timezone: str = ""
        self._battery = BatteryInfo()
        self._hw_info: dict[str, Any] = {}
        self._total_open_cycles: int = 0
        self._total_auto_retracts: int = 0
        self._notifications = NotificationSettings()
        self._schedules: list[Schedule] = []
        self._latency: Optional[float] = None

        # User callbacks
        self._status_callbacks: list[Callable[[DoorStatus], None]] = []
        self._settings_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._connect_callbacks: list[Callable[[], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []

    # =========================================================================
    # Connection
    # =========================================================================

    @property
    def connected(self) -> bool:
        """Whether the door is currently connected."""
        return self._client.available

    @property
    def host(self) -> str:
        """The door's IP address or hostname."""
        return self._host

    @property
    def port(self) -> int:
        """The door's TCP port."""
        return self._port

    @property
    def default_timeout(self) -> float:
        """Default timeout for commands, based on client retry configuration.

        This is the client's effective_timeout (cfg_timeout * MAX_FAILED_MSG),
        which represents the maximum time the client will attempt to get a
        response before dropping the message.
        """
        return self._client.effective_timeout

    @property
    def latency(self) -> Optional[float]:
        """Network latency to the door in seconds.

        This is determined from the round-trip time of ping/pong messages.
        Returns None if no ping has been received yet (e.g., before connection
        or if keepalive is disabled).
        """
        return self._latency

    async def connect(self) -> None:
        """Connect to the door and fetch initial state."""
        # Register callbacks to keep cache updated
        self._client.add_listener(
            "_door_facade",
            door_status_update=self._on_door_status,
            settings_update=self._on_settings,
            sensor_update={
                FIELD_POWER: self._on_power_update,
                FIELD_INSIDE: self._on_inside_update,
                FIELD_OUTSIDE: self._on_outside_update,
                FIELD_AUTO: self._on_auto_update,
                FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: self._on_safety_lock_update,
                FIELD_AUTORETRACT: self._on_autoretract_update,
                FIELD_CMD_LOCKOUT: self._on_cmd_lockout_update,
            },
            battery_update=self._on_battery_update,
            hold_time_update=self._on_hold_time_update,
            timezone_update=self._on_timezone_update,
            hw_info_update=self._on_hw_info_update,
            stats_update={
                FIELD_TOTAL_OPEN_CYCLES: self._on_total_cycles_update,
                FIELD_TOTAL_AUTO_RETRACTS: self._on_total_retracts_update,
            },
            notifications_update={
                FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: self._on_notify_inside_on,
                FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: self._on_notify_inside_off,
                FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: self._on_notify_outside_on,
                FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: self._on_notify_outside_off,
                FIELD_LOW_BATTERY_NOTIFICATIONS: self._on_notify_low_battery,
            },
        )

        self._client.add_handlers(
            "_door_facade",
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            on_ping=self._on_ping,
        )

        await self._client.connect()

        # Wait for connection to establish
        for _ in range(50):  # 5 seconds max
            if self._client.available:
                break
            await asyncio.sleep(0.1)

        if self._client.available:
            await self.refresh()

    async def disconnect(self) -> None:
        """Disconnect from the door."""
        self._client._shutdown = True
        self._client.del_listener("_door_facade")
        self._client.del_handlers("_door_facade")
        self._client.disconnect()

    # =========================================================================
    # Door Control
    # =========================================================================

    @property
    def status(self) -> DoorStatus:
        """Current door status."""
        return self._status

    @property
    def is_open(self) -> bool:
        """Whether the door is open or opening."""
        return self._status in (
            DoorStatus.RISING,
            DoorStatus.SLOWING,
            DoorStatus.HOLDING,
            DoorStatus.KEEPUP,
        )

    @property
    def is_closed(self) -> bool:
        """Whether the door is fully closed."""
        return self._status in (DoorStatus.CLOSED, DoorStatus.IDLE)

    @property
    def is_closing(self) -> bool:
        """Whether the door is currently closing."""
        return self._status in (
            DoorStatus.CLOSING_TOP_OPEN,
            DoorStatus.CLOSING_MID_OPEN,
        )

    @property
    def position(self) -> int:
        """Door position as percentage (0=closed, 100=fully open)."""
        position_map = {
            DoorStatus.IDLE: 0,
            DoorStatus.CLOSED: 0,
            DoorStatus.RISING: 33,
            DoorStatus.SLOWING: 66,
            DoorStatus.HOLDING: 100,
            DoorStatus.KEEPUP: 100,
            DoorStatus.CLOSING_TOP_OPEN: 66,
            DoorStatus.CLOSING_MID_OPEN: 33,
        }
        return position_map.get(self._status, 0)

    async def open(self) -> None:
        """Open the door (will auto-close after hold time)."""
        self._client.send_message(COMMAND, CMD_OPEN)

    async def open_and_hold(self) -> None:
        """Open the door and keep it open until manually closed."""
        self._client.send_message(COMMAND, CMD_OPEN_AND_HOLD)

    async def close(self) -> None:
        """Close the door."""
        self._client.send_message(COMMAND, CMD_CLOSE)

    async def toggle(self) -> None:
        """Toggle the door - open if closed, close if open."""
        if self.is_closed:
            await self.open()
        elif self.is_open:
            await self.close()
        # If closing, do nothing

    async def cycle(self) -> None:
        """Perform a full door cycle (open, hold for hold_time, close).

        This simulates a pet triggering the sensor - the door opens,
        holds for the configured hold time, then automatically closes.
        """
        await self.open()

    # =========================================================================
    # Sensors
    # =========================================================================

    @property
    def inside_sensor(self) -> bool:
        """Whether the inside sensor is enabled."""
        return self._inside_sensor

    async def set_inside_sensor(
        self, enabled: bool, *, timeout: Optional[float] = None
    ) -> None:
        """Enable or disable the inside sensor.

        Args:
            enabled: Whether to enable the sensor.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        cmd = CMD_ENABLE_INSIDE if enabled else CMD_DISABLE_INSIDE
        await asyncio.wait_for(
            self._client.send_message(COMMAND, cmd, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    @property
    def outside_sensor(self) -> bool:
        """Whether the outside sensor is enabled."""
        return self._outside_sensor

    async def set_outside_sensor(
        self, enabled: bool, *, timeout: Optional[float] = None
    ) -> None:
        """Enable or disable the outside sensor.

        Args:
            enabled: Whether to enable the sensor.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        cmd = CMD_ENABLE_OUTSIDE if enabled else CMD_DISABLE_OUTSIDE
        await asyncio.wait_for(
            self._client.send_message(COMMAND, cmd, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    # =========================================================================
    # Power
    # =========================================================================

    @property
    def power(self) -> bool:
        """Whether the door is powered on."""
        return self._power

    async def set_power(
        self, enabled: bool, *, timeout: Optional[float] = None
    ) -> None:
        """Turn door power on or off.

        Args:
            enabled: Whether to enable power.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        cmd = CMD_POWER_ON if enabled else CMD_POWER_OFF
        await asyncio.wait_for(
            self._client.send_message(COMMAND, cmd, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    # =========================================================================
    # Auto/Schedule Mode
    # =========================================================================

    @property
    def auto(self) -> bool:
        """Whether automatic scheduling is enabled."""
        return self._auto

    async def set_auto(
        self, enabled: bool, *, timeout: Optional[float] = None
    ) -> None:
        """Enable or disable automatic scheduling.

        Args:
            enabled: Whether to enable auto mode.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        cmd = CMD_ENABLE_AUTO if enabled else CMD_DISABLE_AUTO
        await asyncio.wait_for(
            self._client.send_message(COMMAND, cmd, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    # =========================================================================
    # Safety Features
    # =========================================================================

    @property
    def safety_lock(self) -> bool:
        """Whether outside sensor safety lock is enabled."""
        return self._safety_lock

    async def set_safety_lock(
        self, enabled: bool, *, timeout: Optional[float] = None
    ) -> None:
        """Enable or disable outside sensor safety lock.

        Args:
            enabled: Whether to enable safety lock.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        cmd = (
            CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK
            if enabled
            else CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK
        )
        await asyncio.wait_for(
            self._client.send_message(COMMAND, cmd, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    @property
    def autoretract(self) -> bool:
        """Whether auto-retract on obstruction is enabled."""
        return self._autoretract

    async def set_autoretract(
        self, enabled: bool, *, timeout: Optional[float] = None
    ) -> None:
        """Enable or disable auto-retract on obstruction.

        Args:
            enabled: Whether to enable auto-retract.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        cmd = CMD_ENABLE_AUTORETRACT if enabled else CMD_DISABLE_AUTORETRACT
        await asyncio.wait_for(
            self._client.send_message(COMMAND, cmd, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    @property
    def pet_proximity_keep_open(self) -> bool:
        """Whether door stays open when pet is in proximity.

        Note: This is the inverse of 'command lockout' in the protocol.
        """
        return self._pet_proximity_keep_open

    async def set_pet_proximity_keep_open(
        self, enabled: bool, *, timeout: Optional[float] = None
    ) -> None:
        """Enable or disable keeping door open when pet is in proximity.

        Note: This uses inverted logic - enabling this feature disables
        command lockout in the protocol.

        Args:
            enabled: Whether to enable pet proximity keep-open.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        # Inverted: enable keep-open = disable cmd_lockout
        cmd = CMD_DISABLE_CMD_LOCKOUT if enabled else CMD_ENABLE_CMD_LOCKOUT
        await asyncio.wait_for(
            self._client.send_message(COMMAND, cmd, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    # =========================================================================
    # Configuration
    # =========================================================================

    @property
    def hold_time(self) -> float:
        """Time in seconds the door stays open after sensor trigger."""
        return self._hold_time

    async def set_hold_time(
        self, seconds: float, *, timeout: Optional[float] = None
    ) -> None:
        """Set the hold-open time in seconds.

        Args:
            seconds: Hold time in seconds.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        # Protocol uses centiseconds
        centiseconds = int(seconds * 100)
        await asyncio.wait_for(
            self._client.send_message(
                CONFIG, CMD_SET_HOLD_TIME, notify=True, holdTime=centiseconds
            ),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    @property
    def timezone(self) -> str:
        """The door's timezone (POSIX format)."""
        return self._timezone

    async def set_timezone(
        self, tz: str, *, timeout: Optional[float] = None
    ) -> None:
        """Set the door's timezone.

        Args:
            tz: Timezone in POSIX format (e.g., 'EST5EDT,M3.2.0,M11.1.0').
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_SET_TIMEZONE, notify=True, tz=tz),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    # =========================================================================
    # Battery
    # =========================================================================

    @property
    def battery_percent(self) -> int:
        """Battery percentage (0-100)."""
        return self._battery.percent

    @property
    def battery_present(self) -> bool:
        """Whether a battery is present."""
        return self._battery.present

    @property
    def ac_present(self) -> bool:
        """Whether AC power is connected."""
        return self._battery.ac_present

    @property
    def battery(self) -> BatteryInfo:
        """Full battery information."""
        return self._battery

    # =========================================================================
    # Hardware Info
    # =========================================================================

    @property
    def firmware_version(self) -> str:
        """Firmware version string."""
        if not self._hw_info:
            return ""
        major = self._hw_info.get(FIELD_FW_MAJOR, 0)
        minor = self._hw_info.get(FIELD_FW_MINOR, 0)
        patch = self._hw_info.get(FIELD_FW_PATCH, 0)
        return f"{major}.{minor}.{patch}"

    @property
    def hardware_info(self) -> dict[str, Any]:
        """Full hardware information dict."""
        return self._hw_info.copy()

    # =========================================================================
    # Statistics
    # =========================================================================

    @property
    def total_open_cycles(self) -> int:
        """Total number of door open cycles."""
        return self._total_open_cycles

    @property
    def total_auto_retracts(self) -> int:
        """Total number of automatic retractions."""
        return self._total_auto_retracts

    # =========================================================================
    # Notifications
    # =========================================================================

    @property
    def notifications(self) -> NotificationSettings:
        """Current notification settings."""
        return self._notifications

    async def set_notifications(
        self,
        *,
        inside_on: Optional[bool] = None,
        inside_off: Optional[bool] = None,
        outside_on: Optional[bool] = None,
        outside_off: Optional[bool] = None,
        low_battery: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> None:
        """Update notification settings.

        Only specified settings are changed; others remain unchanged.

        Args:
            inside_on: Notify when inside sensor triggers.
            inside_off: Notify when inside sensor deactivates.
            outside_on: Notify when outside sensor triggers.
            outside_off: Notify when outside sensor deactivates.
            low_battery: Notify on low battery.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        settings = {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: (
                inside_on if inside_on is not None else self._notifications.inside_on
            ),
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: (
                inside_off
                if inside_off is not None
                else self._notifications.inside_off
            ),
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: (
                outside_on
                if outside_on is not None
                else self._notifications.outside_on
            ),
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: (
                outside_off
                if outside_off is not None
                else self._notifications.outside_off
            ),
            FIELD_LOW_BATTERY_NOTIFICATIONS: (
                low_battery
                if low_battery is not None
                else self._notifications.low_battery
            ),
        }
        await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_SET_NOTIFICATIONS, notify=True, **settings),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    # =========================================================================
    # Schedules
    # =========================================================================

    @property
    def schedules(self) -> list[Schedule]:
        """Current list of schedules."""
        return self._schedules.copy()

    async def get_schedule(
        self, index: int, *, timeout: Optional[float] = None
    ) -> Schedule:
        """Fetch a specific schedule by index.

        Args:
            index: Schedule index (0-based).
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        result = await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_SCHEDULE, notify=True, index=index),
            timeout=timeout if timeout is not None else self.default_timeout,
        )
        return Schedule.from_dict(result)

    async def set_schedule(
        self, schedule: Schedule, *, timeout: Optional[float] = None
    ) -> None:
        """Create or update a schedule.

        Args:
            schedule: The schedule to set.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await asyncio.wait_for(
            self._client.send_message(
                CONFIG, CMD_SET_SCHEDULE, notify=True, **{FIELD_SCHEDULE: schedule.to_dict()}
            ),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    async def delete_schedule(
        self, index: int, *, timeout: Optional[float] = None
    ) -> None:
        """Delete a schedule by index.

        Args:
            index: Schedule index to delete.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_DELETE_SCHEDULE, notify=True, index=index),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    async def refresh_schedules(
        self, *, timeout: Optional[float] = None
    ) -> list[Schedule]:
        """Refresh and return the schedule list.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        result = await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_SCHEDULE_LIST, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )
        self._schedules = [Schedule.from_dict(s) for s in (result or [])]
        return self._schedules.copy()

    # =========================================================================
    # Callbacks
    # =========================================================================

    def on_status_change(self, callback: Callable[[DoorStatus], None]) -> None:
        """Register a callback for door status changes."""
        self._status_callbacks.append(callback)

    def on_settings_change(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback for settings changes."""
        self._settings_callbacks.append(callback)

    def on_connect(self, callback: Callable[[], None]) -> None:
        """Register a callback for when the door connects."""
        self._connect_callbacks.append(callback)

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        """Register a callback for when the door disconnects."""
        self._disconnect_callbacks.append(callback)

    # =========================================================================
    # Refresh
    # =========================================================================

    async def refresh(self, *, timeout: Optional[float] = None) -> None:
        """Refresh all cached state from the door.

        Args:
            timeout: Seconds to wait for each response. Defaults to default_timeout.
        """
        await asyncio.gather(
            self.refresh_status(timeout=timeout),
            self.refresh_settings(timeout=timeout),
            self.refresh_battery(timeout=timeout),
            self.refresh_stats(timeout=timeout),
            self.refresh_hardware_info(timeout=timeout),
            return_exceptions=True,
        )

    async def refresh_status(
        self, *, timeout: Optional[float] = None
    ) -> DoorStatus:
        """Refresh and return the door status.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        result = await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_DOOR_STATUS, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )
        self._status = DoorStatus.from_string(result)
        return self._status

    async def refresh_settings(self, *, timeout: Optional[float] = None) -> None:
        """Refresh all settings from the door.

        Args:
            timeout: Seconds to wait for each response. Defaults to default_timeout.
        """
        effective_timeout = timeout if timeout is not None else self.default_timeout
        # GET_SETTINGS includes hold time, timezone, and sensor voltages
        # Notifications are separate
        await asyncio.gather(
            asyncio.wait_for(
                self._client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True),
                timeout=effective_timeout,
            ),
            asyncio.wait_for(
                self._client.send_message(CONFIG, CMD_GET_NOTIFICATIONS, notify=True),
                timeout=effective_timeout,
            ),
            return_exceptions=True,
        )

    async def refresh_battery(
        self, *, timeout: Optional[float] = None
    ) -> BatteryInfo:
        """Refresh and return battery info.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_DOOR_BATTERY, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )
        return self._battery

    async def refresh_stats(self, *, timeout: Optional[float] = None) -> None:
        """Refresh door statistics.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_DOOR_OPEN_STATS, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    async def refresh_hardware_info(
        self, *, timeout: Optional[float] = None
    ) -> dict[str, Any]:
        """Refresh and return hardware info.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        result = await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_HW_INFO, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )
        if result:
            self._hw_info = result
        return self._hw_info.copy()

    # =========================================================================
    # Internal Callbacks
    # =========================================================================

    def _on_door_status(self, status: str) -> None:
        """Handle door status update from client."""
        new_status = DoorStatus.from_string(status)
        if new_status != self._status:
            self._status = new_status
            for callback in self._status_callbacks:
                try:
                    callback(new_status)
                except Exception:
                    logger.exception("Error in status callback")

    def _on_settings(self, settings: dict[str, Any]) -> None:
        """Handle settings update from client."""
        # Update cached values from settings dict
        if FIELD_POWER in settings:
            self._power = bool(settings[FIELD_POWER])
        if FIELD_INSIDE in settings:
            self._inside_sensor = bool(settings[FIELD_INSIDE])
        if FIELD_OUTSIDE in settings:
            self._outside_sensor = bool(settings[FIELD_OUTSIDE])
        if FIELD_AUTO in settings:
            self._auto = bool(settings[FIELD_AUTO])
        if FIELD_OUTSIDE_SENSOR_SAFETY_LOCK in settings:
            self._safety_lock = bool(settings[FIELD_OUTSIDE_SENSOR_SAFETY_LOCK])
        if FIELD_AUTORETRACT in settings:
            self._autoretract = bool(settings[FIELD_AUTORETRACT])
        if FIELD_CMD_LOCKOUT in settings:
            # Inverted: cmd_lockout disabled means pet proximity keep open
            self._pet_proximity_keep_open = not bool(settings[FIELD_CMD_LOCKOUT])

        for callback in self._settings_callbacks:
            try:
                callback(settings)
            except Exception:
                logger.exception("Error in settings callback")

    def _on_power_update(self, *args) -> None:
        # Handle both (value) and (field, value) signatures
        value = args[-1]
        self._power = value

    def _on_inside_update(self, *args) -> None:
        # Handle both (value) and (field, value) signatures
        value = args[-1]
        self._inside_sensor = value

    def _on_outside_update(self, *args) -> None:
        # Handle both (value) and (field, value) signatures
        value = args[-1]
        self._outside_sensor = value

    def _on_auto_update(self, *args) -> None:
        # Handle both (value) and (field, value) signatures
        value = args[-1]
        self._auto = value

    def _on_safety_lock_update(self, *args) -> None:
        # Handle both (value) and (field, value) signatures
        value = args[-1]
        self._safety_lock = value

    def _on_autoretract_update(self, *args) -> None:
        # Handle both (value) and (field, value) signatures
        value = args[-1]
        self._autoretract = value

    def _on_cmd_lockout_update(self, *args) -> None:
        # Handle both (value) and (field, value) signatures
        value = args[-1]
        # Inverted logic
        self._pet_proximity_keep_open = not value

    def _on_battery_update(self, data: dict[str, Any]) -> None:
        """Handle battery update from client."""
        self._battery = BatteryInfo(
            percent=data.get(FIELD_BATTERY_PERCENT, self._battery.percent),
            present=data.get(FIELD_BATTERY_PRESENT, self._battery.present),
            ac_present=data.get(FIELD_AC_PRESENT, self._battery.ac_present),
        )

    def _on_hold_time_update(self, value: int) -> None:
        """Handle hold time update (value is in centiseconds)."""
        self._hold_time = value / 100.0

    def _on_timezone_update(self, value: str) -> None:
        self._timezone = value

    def _on_hw_info_update(self, data: dict[str, Any]) -> None:
        self._hw_info = data

    def _on_total_cycles_update(self, value: int) -> None:
        self._total_open_cycles = value

    def _on_total_retracts_update(self, value: int) -> None:
        self._total_auto_retracts = value

    def _on_notify_inside_on(self, value: bool) -> None:
        self._notifications.inside_on = value

    def _on_notify_inside_off(self, value: bool) -> None:
        self._notifications.inside_off = value

    def _on_notify_outside_on(self, value: bool) -> None:
        self._notifications.outside_on = value

    def _on_notify_outside_off(self, value: bool) -> None:
        self._notifications.outside_off = value

    def _on_notify_low_battery(self, value: bool) -> None:
        self._notifications.low_battery = value

    async def _on_connect(self) -> None:
        """Handle connection established."""
        for callback in self._connect_callbacks:
            try:
                callback()
            except Exception:
                logger.exception("Error in connect callback")

    async def _on_disconnect(self) -> None:
        """Handle connection lost."""
        self._latency = None  # Reset latency since we're no longer connected
        for callback in self._disconnect_callbacks:
            try:
                callback()
            except Exception:
                logger.exception("Error in disconnect callback")

    def _on_ping(self, latency_ms: int) -> None:
        """Handle ping response with latency measurement.

        Args:
            latency_ms: Round-trip latency in milliseconds.
        """
        self._latency = latency_ms / 1000.0
