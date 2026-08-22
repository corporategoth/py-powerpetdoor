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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .client import PowerPetDoorClient, make_bool
from .const import (
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
    COMMAND,
    CONFIG,
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
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
    FIELD_HOUR,
    FIELD_HW_REVISION,
    FIELD_HW_VERSION,
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
)
from .schedule import (
    MAX_SCHEDULE_INDEX,
    coerce_schedule_days,
    coerce_schedule_flag,
    coerce_schedule_int,
    coerce_schedule_time,
    require_schedule_field,
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
    #: Status string not recognized (e.g. newer firmware); the door is in
    #: an indeterminate state - neither is_open nor is_closed is True.
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_string(cls, value: str) -> DoorStatus:
        """Convert a string status to enum.

        Unrecognized status strings map to :attr:`UNKNOWN` with a warning
        logged - never silently claim a possibly-open door is closed (L16).
        """
        for status in cls:
            if status.value == value:
                return status
        logger.warning("Unknown door status from device: %r", value)
        return cls.UNKNOWN


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
    def from_dict(cls, data: object, name: str = "time") -> ScheduleTime:
        """Create from protocol dict.

        Everything here comes off the wire and is untrusted: a device that
        answers ``in_start_time: 5`` (or ``null``, or a list) must produce a
        ``ValueError`` the caller can handle, not an ``AttributeError`` out
        of a documented coroutine (R4-M1).

        Args:
            data: The ``{hour, min}`` mapping from the device.
            name: Field name used in error messages.

        Raises:
            ValueError: If the value is not a valid ``{hour, min}`` mapping.
        """
        hour, minute = coerce_schedule_time(data, name)
        return cls(hour=hour, minute=minute)


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
    # List of 7 booleans [Sun, Mon, Tue, Wed, Thu, Fri, Sat] where True=active
    days_of_week: list[bool] = field(
        default_factory=lambda: [True, True, True, True, True, True, True]
    )
    # Which sensor this entry is for
    inside: bool = False
    outside: bool = False
    # Time window (applies to whichever sensor is enabled)
    start: ScheduleTime = field(default_factory=lambda: ScheduleTime(6, 0))
    end: ScheduleTime = field(default_factory=lambda: ScheduleTime(22, 0))

    def to_dict(self) -> dict[str, Any]:
        """Convert to protocol dict format.

        Wire types follow docs/protocol.md "Schedule Format" exactly, which
        is also what the simulator's :meth:`Schedule.to_dict` emits:
        ``index`` int, ``enabled`` the string ``"1"``/``"0"``,
        ``daysOfWeek`` a list of 7 ints, ``inside``/``outside`` JSON bools,
        and the four time objects ``{hour, min}`` ints.

        ``enabled`` used to go out as a JSON boolean - the one field where
        the library's emitter disagreed with both the documented protocol
        and the simulator, and the field that decides whether an
        access-control entry is live (M1). Every reader in this tree takes
        both spellings (``coerce_schedule_flag`` and
        ``schedule_entry_content_key`` both go through ``make_bool``), so
        nothing downstream changes.
        """
        # Protocol uses 1/0 for days, convert from booleans
        days_as_int = [1 if d else 0 for d in self.days_of_week]
        result: dict[str, Any] = {
            FIELD_INDEX: self.index,
            FIELD_ENABLED: "1" if self.enabled else "0",
            FIELD_DAYSOFWEEK: days_as_int,
            FIELD_INSIDE: self.inside,
            FIELD_OUTSIDE: self.outside,
        }

        # Set time fields for the appropriate sensor(s)
        if self.inside:
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = self.start.to_dict()
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = self.end.to_dict()
        else:
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = {
                FIELD_HOUR: 0,
                FIELD_MINUTE: 0,
            }
            result[f"{FIELD_INSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = {
                FIELD_HOUR: 0,
                FIELD_MINUTE: 0,
            }

        if self.outside:
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = self.start.to_dict()
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = self.end.to_dict()
        else:
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_START_TIME_SUFFIX}"] = {
                FIELD_HOUR: 0,
                FIELD_MINUTE: 0,
            }
            result[f"{FIELD_OUTSIDE_PREFIX}{FIELD_END_TIME_SUFFIX}"] = {
                FIELD_HOUR: 0,
                FIELD_MINUTE: 0,
            }

        return result

    @classmethod
    def from_dict(cls, data: object) -> Schedule:
        """Create from protocol dict.

        Every field here comes off the wire and is untrusted. Each is
        validated through the shared coercion helpers in
        :mod:`powerpetdoor.schedule` - the same ones the simulator's
        parser uses - so a malformed device reply raises a ``ValueError``
        naming the offending field instead of a ``TypeError`` /
        ``AttributeError`` escaping a documented public coroutine (or being
        swallowed by listener isolation, silently freezing the cached
        schedule list).

        Raises:
            ValueError: If the payload is not a schedule-shaped mapping, or
                a field is not coercible / out of its protocol range.
        """
        if not isinstance(data, dict):
            raise ValueError(f"Schedule must be an object, got {data!r}")

        # Identity and day mask first, so a bad index reports the bad index
        # rather than whatever the time block happens to complain about.
        index = coerce_schedule_int(data.get(FIELD_INDEX, 0), "index", MAX_SCHEDULE_INDEX)
        # A list of 7 flags or the legacy integer bitmask. Read with
        # make_bool, never truthiness: bool("0") is True, so a firmware
        # variant sending "0"/"1" day flags would otherwise turn on every
        # day of the week (L4).
        days = coerce_schedule_days(data.get(FIELD_DAYSOFWEEK, [1, 1, 1, 1, 1, 1, 1]))
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
            # A sensor is selected, so the window that gates it is required
            # (L5) - the same rule the simulator's parser enforces.
            start = ScheduleTime.from_dict(
                require_schedule_field(data, f"{prefix}{FIELD_START_TIME_SUFFIX}"), "start time"
            )
            end = ScheduleTime.from_dict(
                require_schedule_field(data, f"{prefix}{FIELD_END_TIME_SUFFIX}"), "end time"
            )
        else:
            # Neither sensor selected: the entry gates nothing.
            start = ScheduleTime()
            end = ScheduleTime()

        return cls(
            index=index,
            # Read like every other wire flag rather than with a bespoke
            # `== "1"`, and normalized to a real bool so a field declared
            # `enabled: bool` never holds 1/0 (T3).
            enabled=coerce_schedule_flag(data.get(FIELD_ENABLED, True), FIELD_ENABLED),
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
        loop: asyncio.AbstractEventLoop | None = None,
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
        self._latency: float | None = None

        # Connection synchronization (M10): set by the client's on_connect
        # hook, cleared on disconnect. _initialized marks that the initial
        # connect()+refresh() completed, so later on_connect events are
        # auto-reconnects that must resynchronize the cache.
        self._connected_event = asyncio.Event()
        self._initialized = False

        # User callbacks
        self._status_callbacks: list[Callable[[DoorStatus], None]] = []
        self._settings_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._connect_callbacks: list[Callable[[], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []
        self._schedule_callbacks: list[Callable[[list[Schedule]], None]] = []

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
    def latency(self) -> float | None:
        """Network latency to the door in seconds.

        This is determined from the round-trip time of ping/pong messages.
        Returns None if no ping has been received yet (e.g., before connection
        or if keepalive is disabled).
        """
        return self._latency

    async def connect(self, *, timeout: float | None = None) -> None:
        """Connect to the door and fetch initial state.

        Waits (event-driven, no polling) for the connection to establish
        and performs an initial refresh() so cached properties are valid
        when this returns. May be called again after disconnect().

        Idempotent: calling connect() while already connected is a no-op,
        so a defensive re-connect cannot open a second socket to the
        single-connection device and orphan the live one (M2).

        Args:
            timeout: Seconds to wait for the connection to establish.
                Defaults to default_timeout.

        Raises:
            ConnectionError: If the connection is not established within
                the timeout. The client is fully shut down first (no
                background reconnect keeps running), so connect() may
                safely be retried.
        """
        if self.connected:
            logger.warning("Ignoring connect(): already connected to %s", self._host)
            return

        # Re-arm the client in case disconnect() was called earlier (M6).
        self._client.reset_shutdown()
        self._connected_event.clear()
        self._initialized = False

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
            schedule_update=self._on_schedule_update,
            schedule_delete=self._on_schedule_delete,
        )

        self._client.add_handlers(
            "_door_facade",
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            on_ping=self._on_ping,
        )

        await self._client.connect()

        # Wait for the connection, signalled by the client's on_connect
        # hook - no polling (M10).
        effective_timeout = timeout if timeout is not None else self.default_timeout
        try:
            async with asyncio.timeout(effective_timeout):
                await self._connected_event.wait()
        except TimeoutError:
            # Tear down cleanly so no background reconnect loop survives a
            # raised connect(), then report the failure to the caller.
            self._client.shutdown()
            self._client.del_listener("_door_facade")
            self._client.del_handlers("_door_facade")
            raise ConnectionError(
                f"Failed to connect to Power Pet Door at {self._host}:{self._port}"
            ) from None

        await self.refresh()
        self._initialized = True

    async def disconnect(self) -> None:
        """Disconnect from the door and stop automatic reconnection.

        Async lifecycle handlers still in flight (e.g. the ``on_disconnect``
        this call itself triggers) are awaited, then cancelled if they
        overrun ``default_timeout``, so nothing outlives this call (T2).

        Idempotent: safe to call multiple times, and before connect().
        """
        self._initialized = False
        await self._client.aclose(self.default_timeout)
        self._client.del_listener("_door_facade")
        self._client.del_handlers("_door_facade")

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

    async def set_inside_sensor(self, enabled: bool, *, timeout: float | None = None) -> None:
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

    async def set_outside_sensor(self, enabled: bool, *, timeout: float | None = None) -> None:
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

    async def set_power(self, enabled: bool, *, timeout: float | None = None) -> None:
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

    async def set_auto(self, enabled: bool, *, timeout: float | None = None) -> None:
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

    async def set_safety_lock(self, enabled: bool, *, timeout: float | None = None) -> None:
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

    async def set_autoretract(self, enabled: bool, *, timeout: float | None = None) -> None:
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
        self, enabled: bool, *, timeout: float | None = None
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

    async def set_hold_time(self, seconds: float, *, timeout: float | None = None) -> None:
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

    async def set_timezone(self, tz: str, *, timeout: float | None = None) -> None:
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
    def hardware_version(self) -> str:
        """Hardware version string."""
        if not self._hw_info:
            return ""
        ver = self._hw_info.get(FIELD_HW_VERSION, "")
        rev = self._hw_info.get(FIELD_HW_REVISION, "")
        if not ver and not rev:
            return ""
        return f"{ver} rev {rev}"

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
        inside_on: bool | None = None,
        inside_off: bool | None = None,
        outside_on: bool | None = None,
        outside_off: bool | None = None,
        low_battery: bool | None = None,
        timeout: float | None = None,
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
        merged = {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: (
                inside_on if inside_on is not None else self._notifications.inside_on
            ),
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: (
                inside_off if inside_off is not None else self._notifications.inside_off
            ),
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: (
                outside_on if outside_on is not None else self._notifications.outside_on
            ),
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: (
                outside_off if outside_off is not None else self._notifications.outside_off
            ),
            FIELD_LOW_BATTERY_NOTIFICATIONS: (
                low_battery if low_battery is not None else self._notifications.low_battery
            ),
        }
        # The wire protocol uses "1"/"0" strings (docs/protocol.md).
        settings = {key: "1" if value else "0" for key, value in merged.items()}
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

    async def get_schedule(self, index: int, *, timeout: float | None = None) -> Schedule:
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

    async def set_schedule(self, schedule: Schedule, *, timeout: float | None = None) -> None:
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

    async def delete_schedule(self, index: int, *, timeout: float | None = None) -> None:
        """Delete a schedule by index.

        Args:
            index: Schedule index to delete.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_DELETE_SCHEDULE, notify=True, index=index),
            timeout=timeout if timeout is not None else self.default_timeout,
        )
        # Keep the cache correct even when the device does not echo the
        # deleted index (the listener is a no-op if it already ran).
        if any(s.index == index for s in self._schedules):
            self._on_schedule_delete(index)

    async def refresh_schedules(self, *, timeout: float | None = None) -> list[Schedule]:
        """Refresh and return the schedule list.

        This performs a two-step fetch matching the real device behavior:
        1. Get list of schedule indices
        2. Fetch each schedule individually

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        effective_timeout = timeout if timeout is not None else self.default_timeout

        # Step 1: Get schedule indices
        indices = await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_SCHEDULE_LIST, notify=True),
            timeout=effective_timeout,
        )

        if not indices:
            self._schedules = []
            return []

        # Step 2: Fetch each schedule individually
        schedules = []
        for idx in indices:
            try:
                result = await asyncio.wait_for(
                    self._client.send_message(
                        CONFIG, CMD_GET_SCHEDULE, notify=True, **{FIELD_INDEX: idx}
                    ),
                    timeout=effective_timeout,
                )
                if result:
                    schedules.append(Schedule.from_dict(result))
            except TimeoutError:
                logger.warning("Timeout fetching schedule %d", idx)
            except Exception:
                logger.exception("Error fetching schedule %d", idx)

        # Sorted for the same reason _on_schedule_update sorts: the public
        # `schedules` property must not be ordered by whichever code path
        # last touched it. GET_SCHEDULE_LIST returns slots, and a device
        # with slots filled out of order returns them out of order (T3).
        schedules.sort(key=lambda s: s.index)
        self._schedules = schedules
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

    def on_schedule_change(self, callback: Callable[[list[Schedule]], None]) -> None:
        """Register a callback for schedule changes.

        The callback receives the updated list of schedules whenever
        a schedule is added, updated, or deleted.
        """
        self._schedule_callbacks.append(callback)

    # =========================================================================
    # Refresh
    # =========================================================================

    @staticmethod
    def _log_refresh_failures(names: list[str], results: Sequence[Any]) -> None:
        """Log each failed step of a gathered refresh (L5).

        ``refresh()``/``refresh_settings()`` gather with
        ``return_exceptions=True`` so one dead command cannot abort the
        rest. Without this, a device NAK or a drop during connect() would
        silently leave cached properties at their constructor defaults.
        """
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Refresh step %s failed: %r", name, result)

    async def refresh(self, *, timeout: float | None = None) -> None:
        """Refresh all cached state from the door.

        Individual step failures are logged and do not abort the rest;
        properties whose refresh failed keep their previous cached value.

        Args:
            timeout: Seconds to wait for each response. Defaults to default_timeout.
        """
        results = await asyncio.gather(
            self.refresh_status(timeout=timeout),
            self.refresh_settings(timeout=timeout),
            self.refresh_battery(timeout=timeout),
            self.refresh_stats(timeout=timeout),
            self.refresh_hardware_info(timeout=timeout),
            return_exceptions=True,
        )
        self._log_refresh_failures(
            ["status", "settings", "battery", "stats", "hardware_info"], results
        )

    async def refresh_status(self, *, timeout: float | None = None) -> DoorStatus:
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

    async def refresh_settings(self, *, timeout: float | None = None) -> None:
        """Refresh all settings from the door.

        Individual step failures are logged and do not abort the other
        step; settings whose refresh failed keep their cached value.

        Args:
            timeout: Seconds to wait for each response. Defaults to default_timeout.
        """
        effective_timeout = timeout if timeout is not None else self.default_timeout
        # GET_SETTINGS includes hold time, timezone, and sensor voltages
        # Notifications are separate
        results = await asyncio.gather(
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
        self._log_refresh_failures([CMD_GET_SETTINGS, CMD_GET_NOTIFICATIONS], results)

    async def refresh_battery(self, *, timeout: float | None = None) -> BatteryInfo:
        """Refresh and return battery info.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_DOOR_BATTERY, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )
        return self._battery

    async def refresh_stats(self, *, timeout: float | None = None) -> None:
        """Refresh door statistics.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await asyncio.wait_for(
            self._client.send_message(CONFIG, CMD_GET_DOOR_OPEN_STATS, notify=True),
            timeout=timeout if timeout is not None else self.default_timeout,
        )

    async def refresh_hardware_info(self, *, timeout: float | None = None) -> dict[str, Any]:
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
        """Handle settings update from client.

        Wire values are protocol strings ("0"/"1"), so they must be
        coerced with make_bool - bool("0") is True, which would cache the
        inverse of the device state (test-fanatic H1). Unrecognized values
        leave the cached state untouched.
        """
        # (settings field, cache attribute, inverted?)
        boolean_fields = (
            (FIELD_POWER, "_power", False),
            (FIELD_INSIDE, "_inside_sensor", False),
            (FIELD_OUTSIDE, "_outside_sensor", False),
            (FIELD_AUTO, "_auto", False),
            (FIELD_OUTSIDE_SENSOR_SAFETY_LOCK, "_safety_lock", False),
            (FIELD_AUTORETRACT, "_autoretract", False),
            # Inverted: cmd_lockout disabled means pet proximity keep open
            (FIELD_CMD_LOCKOUT, "_pet_proximity_keep_open", True),
        )
        for field_name, attr, inverted in boolean_fields:
            if field_name in settings:
                value = make_bool(settings[field_name])
                if value is not None:
                    setattr(self, attr, (not value) if inverted else value)

        for callback in self._settings_callbacks:
            try:
                callback(settings)
            except Exception:
                logger.exception("Error in settings callback")

    # Sensor listeners are invoked by the client as callback(field, value)
    # (decision D4); value is None when the wire value was unrecognized,
    # in which case the cached state is left untouched.

    def _on_power_update(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._power = value

    def _on_inside_update(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._inside_sensor = value

    def _on_outside_update(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._outside_sensor = value

    def _on_auto_update(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._auto = value

    def _on_safety_lock_update(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._safety_lock = value

    def _on_autoretract_update(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._autoretract = value

    def _on_cmd_lockout_update(self, field_name: str, value: bool | None) -> None:
        # Inverted logic
        if value is not None:
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

    # Stats and notification listeners are invoked by the client as
    # callback(field, value) (decision D4 / backend M2).

    def _on_total_cycles_update(self, field_name: str, value: int) -> None:
        self._total_open_cycles = value

    def _on_total_retracts_update(self, field_name: str, value: int) -> None:
        self._total_auto_retracts = value

    def _on_notify_inside_on(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._notifications.inside_on = value

    def _on_notify_inside_off(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._notifications.inside_off = value

    def _on_notify_outside_on(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._notifications.outside_on = value

    def _on_notify_outside_off(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._notifications.outside_off = value

    def _on_notify_low_battery(self, field_name: str, value: bool | None) -> None:
        if value is not None:
            self._notifications.low_battery = value

    async def _on_connect(self) -> None:
        """Handle connection established."""
        self._connected_event.set()
        # After a client-level auto-reconnect the cached state may be
        # stale; resynchronize before notifying callbacks (M10). The
        # initial connect() performs its own refresh.
        if self._initialized:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Error refreshing state after reconnect")
        for callback in self._connect_callbacks:
            try:
                callback()
            except Exception:
                logger.exception("Error in connect callback")

    async def _on_disconnect(self) -> None:
        """Handle connection lost."""
        self._connected_event.clear()
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

    def _on_schedule_update(self, schedule_data: dict[str, Any]) -> None:
        """Handle schedule add/update from client.

        A malformed entry is reported and dropped rather than allowed to
        raise: the client isolates listener exceptions, so an escaping
        error would leave the cached schedule list silently stale with
        nothing in the log to say the update was lost (R4-M1).
        """
        try:
            schedule = Schedule.from_dict(schedule_data)
        except ValueError as err:
            logger.warning("Ignoring malformed schedule update from device: %s", err)
            return
        # Update or add the schedule in our cache
        for i, s in enumerate(self._schedules):
            if s.index == schedule.index:
                self._schedules[i] = schedule
                break
        else:
            self._schedules.append(schedule)
        # Sort by index for consistent ordering
        self._schedules.sort(key=lambda s: s.index)
        # Notify callbacks
        self._notify_schedule_change()

    def _on_schedule_delete(self, index: int) -> None:
        """Handle schedule delete from client."""
        self._schedules = [s for s in self._schedules if s.index != index]
        self._notify_schedule_change()

    def _notify_schedule_change(self) -> None:
        """Notify all schedule callbacks with the current schedule list."""
        schedules_copy = self._schedules.copy()
        for callback in self._schedule_callbacks:
            try:
                callback(schedules_copy)
            except Exception:
                logger.exception("Error in schedule callback")
