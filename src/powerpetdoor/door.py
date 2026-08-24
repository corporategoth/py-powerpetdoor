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
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .client import (
    PowerPetDoorClient,
    autoretract_from_door_options,
    build_set_hold_time_message,
    build_set_notifications_message,
    build_set_voltage_message,
    envelope_for_command,
    make_bool,
)
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
    CMD_GET_TIME,
    CMD_HAS_REMOTE_ID,
    CMD_HAS_REMOTE_KEY,
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_TIMEZONE,
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
    FIELD_HAS_REMOTE_ID,
    FIELD_HAS_REMOTE_KEY,
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
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_START_TIME_SUFFIX,
    FIELD_TIME,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    TIME_FORMAT,
)
from .i18n import t
from .sanitize import MAX_LOGGED_LENGTH, sanitize_field, sanitize_text
from .schedule import (
    MAX_SCHEDULE_INDEX,
    SCHEDULE_WIRE_TO_DEVICE,
    build_schedule_payload,
    build_set_schedule_message,
    coerce_schedule_days,
    coerce_schedule_flag,
    coerce_schedule_int,
    coerce_schedule_time,
    normalise_window_end,
    require_schedule_field,
    schedule_window_is_empty,
    window_minutes,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Facade cache guards (layer 1)
# =============================================================================
#
# `PowerPetDoor` is the strict-typed layer: every property here is annotated
# with a concrete Python type and consumers (the Home Assistant integration
# publishes these as sensor states) are entitled to rely on it. The client is
# layer 3 and is deliberately liberal - it hands the facade whatever the
# device said, and `make_bool` is *documented* to return None for a string it
# does not recognize - so the coercion has to happen here, on the way into the
# cache.
#
# The rule these helpers enforce, and the one to apply to any listener added
# later: **nothing enters the facade cache without a type check, and a value
# that fails it leaves the cache untouched.** (`batteryPercent: "55"` used to
# make the documented `door.battery.charging` property raise TypeError with
# nothing logged.)
#
# Rejections log at DEBUG, not WARNING: these listeners fire once per device
# frame. The client already logs every received frame at DEBUG, so an
# operator diagnosing a firmware variant has both halves in the same place.


#: The largest magnitude an ``int`` can have and still survive conversion
#: to ``float``. Python ints are unbounded; ``float`` is not, so
#: ``huge_int / 100.0`` raises ``OverflowError`` rather than returning
#: ``inf``.
#:
#: This is a **representability** limit, deliberately not a protocol one.
#: ``docs/protocol.md`` is reverse-engineered and is not authority over
#: what real firmware sends, so the facade must not refuse a device value
#: for being larger than a bound this project invented - it refuses it only
#: for being a value the arithmetic downstream physically cannot perform.
_FLOAT_REPRESENTABLE_MAX = sys.float_info.max


def _keep_int(value: Any, cached: int, field_name: str, *, maximum: float | None = None) -> int:
    """Coerce a device value to ``int``, or keep the cached one.

    Args:
        value: The value the device sent.
        cached: The value to keep if ``value`` is not usable.
        field_name: Field name, for the rejection log line.
        maximum: Optional inclusive magnitude bound. Left ``None`` by
            every caller that merely *stores* the result - Python ints are
            unbounded and ``battery_percent`` publishes one verbatim, so
            bounding here globally would refuse values that are perfectly
            harmless (and would contradict
            ``test_a_huge_integer_percent_does_not_overflow_the_guard``).
            Pass it only where the consumer does float arithmetic on the
            result; see :meth:`PowerPetDoor._on_hold_time_update`.
    """
    # bool is an int subclass; True must not become 1 in a counter field.
    if not isinstance(value, bool):
        coerced: int | None = None
        if isinstance(value, int):
            coerced = value
        # json.loads accepts NaN/Infinity by default, and int() raises on
        # both, so finiteness is checked before the conversion rather than
        # after. isinstance(int) above already matched, so an arbitrarily
        # large integer never reaches math.isfinite (which would overflow)
        # - the `maximum` check below is what handles that case, and only
        # for the callers that need it.
        elif isinstance(value, float) and math.isfinite(value):
            coerced = int(value)
        if coerced is not None and (maximum is None or -maximum <= coerced <= maximum):
            return coerced
    _log_rejected(
        field_name, value, "int" if maximum is None else f"int of magnitude <= {maximum:g}"
    )
    return cached


def _keep_bool(value: Any, cached: bool, field_name: str) -> bool:
    """Keep a device value only if it is already a ``bool``."""
    if isinstance(value, bool):
        return value
    _log_rejected(field_name, value, "bool")
    return cached


def _keep_flag(value: Any, cached: bool, field_name: str) -> bool:
    """Keep a ``make_bool``-coerced sensor flag only if it really is a bool.

    ``make_bool`` returns its argument unchanged for a value that is
    neither a string nor an int, so ``[]``, ``{}`` and ``0.0`` reach these
    listeners as themselves rather than as None. ``if value is not None``
    therefore let them into a strictly typed cache, where a known-ON
    ``safety_lock`` receiving ``[]`` reads False - a safety flag failing in
    the permissive direction. (The cache heals on the next
    ``refresh_settings()`` or reconnect; it is wrong until then.)

    Widening ``make_bool`` instead is not an option: ``compress_schedule``
    calls it unguarded on day flags, where "unrecognized" has to stay
    fail-closed.

    None - the documented "unrecognized string" result - keeps the cached
    value silently, because the client already logged the frame.
    """
    if isinstance(value, bool):
        return value
    if value is not None:
        _log_rejected(field_name, value, "bool")
    return cached


def _keep_str(value: Any, cached: str, field_name: str) -> str:
    """Keep a device value only if it is already a ``str``."""
    if isinstance(value, str):
        return value
    _log_rejected(field_name, value, "str")
    return cached


def _log_rejected(field_name: str, value: Any, expected: str) -> None:
    """Record a device value the facade refused to cache."""
    logger.debug(
        t(
            "door.ignoring_device_expected_keeping_cached",
            "Ignoring %s from device for %s (expected %s); keeping the cached value",
        ),
        sanitize_text(value, MAX_LOGGED_LENGTH),
        field_name,
        expected,
    )


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
        logged - never silently claim a possibly-open door is closed.

        Args:
            value: The status string the device sent.
        """
        for status in cls:
            if status.value == value:
                return status
        logger.warning(
            t("door.unknown_door_status_device", "Unknown door status from device: %s"),
            sanitize_field(value, MAX_LOGGED_LENGTH),
        )
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
        of a documented coroutine.

        Args:
            data: The ``{hour, min}`` mapping from the device.
            name: Field name used in error messages.

        Raises:
            ValueError: If the value is not a valid ``{hour, min}`` mapping.
        """
        # Read path: this is the device telling us what it has. Refusing an
        # entry here makes refresh_schedules() drop it, hiding a schedule that
        # exists on the door, so an absent hour becomes midnight instead.
        hour, minute = coerce_schedule_time(data, name, require_hour=False)
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
        """Serialize for the wire, client-to-device.

        This builds the ``SET_SCHEDULE`` payload the library **sends**, so
        every field's spelling comes from
        :data:`~powerpetdoor.schedule.SCHEDULE_WIRE_TO_DEVICE` — the single
        marked place those choices live. Notably ``enabled`` goes out as a
        JSON boolean, *not* the ``"1"``/``"0"`` string the simulator
        replies with: the simulator emits the *device->client* direction,
        so the two are not twins and are not required to agree.

        Parsers on both sides stay liberal regardless
        (``coerce_schedule_flag`` and ``schedule_entry_content_key`` both
        go through ``make_bool``), so ``true``/``"1"``/``1`` are read
        identically whichever direction they arrive from.
        """
        return build_schedule_payload(
            SCHEDULE_WIRE_TO_DEVICE,
            index=self.index,
            enabled=self.enabled,
            days_of_week=self.days_of_week,
            inside=self.inside,
            outside=self.outside,
            start=(self.start.hour, self.start.minute),
            # A window end of 00:00 goes out as 24:00. Midnight is the first
            # minute of a day, so as an end it says the opposite of what it
            # means, and the device does NOT reinterpret it: measured on
            # firmware 1.7.18, 20:00-00:00 is stored faithfully and never
            # fires. Only the SELECTED sensor's window is translated - the
            # other sensor's block stays the all-zero filler the protocol
            # asks for, which must not become a whole-day window.
            end=normalise_window_end((self.end.hour, self.end.minute)),
        )

    def window_is_empty(self) -> bool:
        """Whether this entry, AS THIS LIBRARY WOULD SEND IT, never fires.

        The end is normalised first, so this answers the useful question -
        "if I write this, will it do anything?" - rather than the literal
        one. The two differ for exactly one spelling: a door that literally
        holds ``00:00-00:00`` is gating that sensor OFF right now (measured),
        while this reports False because sending it would produce
        ``00:00-24:00``, a whole day. That spelling only exists on a door by
        mistake, and re-saving it makes the door agree.

        See :func:`powerpetdoor.schedule.schedule_window_is_empty`. Worth
        asking, because a window that can never fire and one that is switched
        off look identical in a listing but only one of them is deliberate.
        """
        return schedule_window_is_empty(
            (self.start.hour, self.start.minute),
            normalise_window_end((self.end.hour, self.end.minute)),
        )

    def validate_for_send(self) -> None:
        """Raise if this entry could never fire on a real door.

        Applied when SENDING only. A door asked for a window that ends before
        it begins accepts it, echoes it back unchanged, and then silently
        never acts on it - so nothing downstream catches the mistake, and the
        user is left with a schedule that reads correctly and does nothing.

        The end is normalised first, so ``22:00-00:00`` passes (it goes out
        as ``22:00-24:00``) while ``23:00-01:00`` is refused: the device
        cannot express a window running into the next day at all, and the
        caller has to spell it as two entries.

        Raises:
            ValueError: If the window ends before it starts.
        """
        start = (self.start.hour, self.start.minute)
        end = normalise_window_end((self.end.hour, self.end.minute))
        start_min, end_min = window_minutes(start, end)
        if end_min < start_min:
            raise ValueError(
                t(
                    "door.schedule_end_before_start",
                    "Schedule window {start} to {end} ends before it starts. "
                    "The door cannot schedule past midnight in one entry - use "
                    "{start} to 24:00 on this day and 00:00 to {end} on the next.",
                    start=f"{start[0]:02d}:{start[1]:02d}",
                    end=f"{self.end.hour:02d}:{self.end.minute:02d}",
                )
            )

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
            raise ValueError(
                t(
                    "door.schedule_must_object_got",
                    "Schedule must be an object, got {data!r}",
                    data=data,
                )
            )

        # Identity and day mask first, so a bad index reports the bad index
        # rather than whatever the time block happens to complain about.
        index = coerce_schedule_int(data.get(FIELD_INDEX, 0), "index", MAX_SCHEDULE_INDEX)
        # A list of 7 flags or the legacy integer bitmask. Read with
        # make_bool, never truthiness: bool("0") is True, so a firmware
        # variant sending "0"/"1" day flags would otherwise turn on every
        # day of the week.
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
            # A sensor is selected, so the window that gates it is required -
            # the same rule the simulator's parser enforces.
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
            # `enabled: bool` never holds 1/0.
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

    .. warning::

       **Commands issued while disconnected are queued, not refused, and
       they execute on the next connection.** This is a *physical* door.
       ``open()``/``close()``/``toggle()``/``cycle()`` are
       fire-and-forget: with no transport there is nothing to write to,
       so the message sits in the client's priority queue until a
       connection appears - and then the door opens, unattended, for a
       request the caller was told nothing about. Measured against a real
       device emulation: ``open()`` returned in 0.000 s during a
       reconnect window and the door latched open 4.0 s later.

       This is deliberate (:class:`~powerpetdoor.PowerPetDoorClient` flushes
       its queue on connect), and it is what makes a command survive a
       transient drop. Check :attr:`connected` first if you need "refuse
       when offline" instead::

           if not door.connected:
               raise ConnectionError("door is offline")
           await door.open()

       The awaited setters have the same semantics: they wait
       :attr:`default_timeout` and then raise ``TimeoutError`` with a
       message saying the command is still queued. See "Behaviour while
       disconnected" in ``docs/door.md``.
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
        self._sensor_trigger_voltage: int = 0
        self._sleep_sensor_trigger_voltage: int = 0
        self._timezone: str = ""
        self._device_time: str = ""
        self._has_remote_id: bool = False
        self._has_remote_key: bool = False
        self._battery = BatteryInfo()
        self._hw_info: dict[str, Any] = {}
        self._total_open_cycles: int = 0
        self._total_auto_retracts: int = 0
        self._notifications = NotificationSettings()
        self._schedules: list[Schedule] = []
        self._latency: float | None = None

        # Connection synchronization: set by the client's on_connect
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

    async def _await_response(self, cmd: str, future, timeout: float | None):
        """Await a command's response, with a timeout that says something.

        Every command and setter on this facade is
        ``await asyncio.wait_for(<future>, timeout=...)``, and
        ``asyncio.wait_for`` raises a **bare** ``TimeoutError()`` - its
        ``repr`` is literally ``TimeoutError()``. A developer saw an empty
        exception after a 20-second stall with no way to tell "the door is
        wedged" from "you never called ``connect()``". This is the least
        actionable exception the API can produce, so the message names the
        command, the wait, the endpoint, and - when there is no connection -
        the fact that the command is **queued** rather than lost.

        Args:
            cmd: The protocol command being awaited, for the message.
            future: The future returned by ``send_message(..., notify=True)``.
            timeout: Seconds to wait, or None for :attr:`default_timeout`.

        Returns:
            Whatever the command's response resolved to.

        Raises:
            TimeoutError: With a message, on expiry.
        """
        effective = timeout if timeout is not None else self.default_timeout
        try:
            return await asyncio.wait_for(future, timeout=effective)
        except TimeoutError as err:
            detail = f"{cmd} timed out after {effective}s waiting for {self._host}:{self._port}"
            if not self.connected:
                detail += (
                    "; not connected - the command is queued and will be sent when the "
                    "connection is next established (call connect() first to avoid this)"
                )
            raise TimeoutError(detail) from err

    async def connect(self, *, timeout: float | None = None) -> None:
        """Connect to the door and fetch initial state.

        Waits (event-driven, no polling) for the connection to establish
        and performs an initial refresh() so cached properties are valid
        when this returns. May be called again after disconnect().

        Idempotent: calling connect() while already connected is a no-op,
        so a defensive re-connect cannot open a second socket to the
        single-connection device and orphan the live one.

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
            logger.warning(
                t(
                    "door.ignoring_connect_already_connected",
                    "Ignoring connect(): already connected to %s",
                ),
                self._host,
            )
            return

        # Re-arm the client in case disconnect() was called earlier.
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
            sensor_trigger_voltage_update=self._on_sensor_trigger_voltage_update,
            sleep_sensor_trigger_voltage_update=self._on_sleep_sensor_trigger_voltage_update,
            remote_id_update=self._on_remote_id_update,
            remote_key_update=self._on_remote_key_update,
            timezone_update=self._on_timezone_update,
            time_update=self._on_time_update,
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
        # hook - no polling.
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
                t(
                    "door.failed_connect_power_pet_door",
                    "Failed to connect to Power Pet Door at {host}:{port}",
                    host=self._host,
                    port=self._port,
                )
            ) from None

        await self.refresh()
        self._initialized = True

    async def disconnect(self) -> None:
        """Disconnect from the door and stop automatic reconnection.

        Async lifecycle handlers still in flight (e.g. the ``on_disconnect``
        this call itself triggers) are awaited, then cancelled if they
        overrun ``default_timeout``, so nothing outlives this call.

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
        """Open the door and keep it open until :meth:`close` is called.

        Sends ``OPEN_AND_HOLD``, which parks the door in
        :attr:`DoorStatus.KEEPUP`. "Open" means the door is open and stays
        open: the hold timer does not apply, and only an explicit
        :meth:`close` (or the door's own button) brings it down. For the
        timed open that closes itself, see :meth:`cycle`.
        """
        self._client.send_message(envelope_for_command(CMD_OPEN_AND_HOLD), CMD_OPEN_AND_HOLD)

    async def close(self) -> None:
        """Close the door."""
        self._client.send_message(envelope_for_command(CMD_CLOSE), CMD_CLOSE)

    async def toggle(self) -> None:
        """Toggle the door - open if closed, close if open.

        Opening goes through :meth:`open`, so a toggled-open door stays
        open until it is toggled back. Does nothing mid-travel.
        """
        if self.is_closed:
            await self.open()
        elif self.is_open:
            await self.close()
        # If closing, do nothing

    async def cycle(self) -> None:
        """Run a full door cycle: open, hold for ``hold_time``, close.

        Sends ``OPEN``, the timed open - the same thing a pet triggering a
        sensor gets. The door rises, sits in :attr:`DoorStatus.HOLDING` for
        the configured hold time, then closes itself. Use :meth:`open` for
        an open that stays open.
        """
        self._client.send_message(envelope_for_command(CMD_OPEN), CMD_OPEN)

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
        await self._await_response(
            cmd, self._client.send_message(envelope_for_command(cmd), cmd, notify=True), timeout
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
        await self._await_response(
            cmd, self._client.send_message(envelope_for_command(cmd), cmd, notify=True), timeout
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
        await self._await_response(
            cmd, self._client.send_message(envelope_for_command(cmd), cmd, notify=True), timeout
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
        await self._await_response(
            cmd, self._client.send_message(envelope_for_command(cmd), cmd, notify=True), timeout
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
        await self._await_response(
            cmd, self._client.send_message(envelope_for_command(cmd), cmd, notify=True), timeout
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
        await self._await_response(
            cmd, self._client.send_message(envelope_for_command(cmd), cmd, notify=True), timeout
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
        await self._await_response(
            cmd, self._client.send_message(envelope_for_command(cmd), cmd, notify=True), timeout
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
        # `build_set_hold_time_message` owns the seconds -> centiseconds
        # conversion, and is equally reachable from a message-level caller.
        await self._await_response(
            CMD_SET_HOLD_TIME,
            self._client.send_message(
                CONFIG, CMD_SET_HOLD_TIME, notify=True, **build_set_hold_time_message(seconds)
            ),
            timeout,
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
        await self._await_response(
            CMD_SET_TIMEZONE,
            self._client.send_message(CONFIG, CMD_SET_TIMEZONE, notify=True, tz=tz),
            timeout,
        )

    @property
    def sensor_trigger_voltage(self) -> int:
        """Capacitive sensor trigger threshold, in millivolts.

        Read out of ``GET_SETTINGS``; the probed unit reported 2000.
        """
        return self._sensor_trigger_voltage

    async def set_sensor_trigger_voltage(
        self, millivolts: int, *, timeout: float | None = None
    ) -> None:
        """Set the sensor trigger threshold.

        Args:
            millivolts: The new threshold. Verified settable on firmware
                1.7.18 (2000 -> 1500 -> 2000).
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        # `build_set_voltage_message` owns the wire shape - the setter's
        # field is `voltage`, NOT the getter's `sensorTriggerVoltage` - and
        # is equally reachable from a message-level caller.
        await self._await_response(
            CMD_SET_SENSOR_TRIGGER_VOLTAGE,
            self._client.send_message(
                CONFIG,
                CMD_SET_SENSOR_TRIGGER_VOLTAGE,
                notify=True,
                **build_set_voltage_message(millivolts),
            ),
            timeout,
        )

    @property
    def sleep_sensor_trigger_voltage(self) -> int:
        """Sensor trigger threshold used in the door's sleep state, in mV."""
        return self._sleep_sensor_trigger_voltage

    async def set_sleep_sensor_trigger_voltage(
        self, millivolts: int, *, timeout: float | None = None
    ) -> None:
        """Set the sleep-state sensor trigger threshold.

        Args:
            millivolts: The new threshold. Verified settable on firmware
                1.7.18 (2000 -> 1800 -> 2000).
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await self._await_response(
            CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
            self._client.send_message(
                CONFIG,
                CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
                notify=True,
                **build_set_voltage_message(millivolts),
            ),
            timeout,
        )

    # =========================================================================
    # Remote pairing
    # =========================================================================

    @property
    def has_remote_id(self) -> bool:
        """Whether the door has a remote ID paired.

        Populated by :meth:`refresh_remote_info`; it is not part of
        ``GET_SETTINGS``, so it stays at its default until that runs.
        """
        return self._has_remote_id

    @property
    def has_remote_key(self) -> bool:
        """Whether the door has a remote key paired."""
        return self._has_remote_key

    async def refresh_remote_info(self, *, timeout: float | None = None) -> None:
        """Query whether a remote ID and remote key are paired.

        Deliberately not part of :meth:`refresh`: it is static pairing
        information, not live state, and two extra round trips on every
        reconnect buy nothing.

        Args:
            timeout: Seconds to wait for each response. Defaults to
                default_timeout.
        """
        for cmd in (CMD_HAS_REMOTE_ID, CMD_HAS_REMOTE_KEY):
            await self._await_response(
                cmd,
                self._client.send_message(envelope_for_command(cmd), cmd, notify=True),
                timeout,
            )

    @property
    def device_time(self) -> str:
        """The door's own wall clock, as it last reported it.

        The raw ``asctime`` string the device sends, in *its* configured
        timezone, or ``""`` before :meth:`refresh_time` has run. Kept as
        the device spelled it: it is a snapshot, not a live clock, and the
        door was observed to answer a stale frame occasionally.

        Use :meth:`refresh_time` to fetch and parse a fresh one.
        """
        return self._device_time

    async def refresh_time(self, *, timeout: float | None = None) -> datetime | None:
        """Read the door's own wall clock.

        Schedules are evaluated against this clock, so it is the only way
        to check that a door will fire a schedule when you expect it to -
        a door whose timezone or clock is wrong opens on the wrong
        schedule with nothing else to show for it.

        Deliberately not part of :meth:`refresh`: it is a diagnostic, and
        it goes stale the moment it arrives.

        Args:
            timeout: Seconds to wait for the response. Defaults to
                default_timeout.

        Returns:
            The reported time as a **naive** :class:`~datetime.datetime`
            (the device sends no offset; it is local to
            :attr:`timezone`), or None if the string was unparseable -
            in which case :attr:`device_time` still holds it verbatim.
        """
        await self._await_response(
            CMD_GET_TIME,
            self._client.send_message(
                envelope_for_command(CMD_GET_TIME), CMD_GET_TIME, notify=True
            ),
            timeout,
        )
        try:
            return datetime.strptime(self._device_time, TIME_FORMAT)
        except ValueError:
            logger.debug(
                t(
                    "door.device_reported_unparseable_time",
                    "Device reported an unparseable time: %s",
                ),
                sanitize_text(self._device_time),
            )
            return None

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
        # The device is given the complete set every time (there is no
        # partial form), so the unspecified flags come from the cache.
        # `build_set_notifications_message` owns the wire shape - nested
        # object, all five flags, JSON booleans - and is equally reachable
        # from a message-level caller.
        merged = build_set_notifications_message(
            inside_on=self._notifications.inside_on if inside_on is None else inside_on,
            inside_off=self._notifications.inside_off if inside_off is None else inside_off,
            outside_on=self._notifications.outside_on if outside_on is None else outside_on,
            outside_off=self._notifications.outside_off if outside_off is None else outside_off,
            low_battery=self._notifications.low_battery if low_battery is None else low_battery,
        )
        await self._await_response(
            CMD_SET_NOTIFICATIONS,
            self._client.send_message(CONFIG, CMD_SET_NOTIFICATIONS, notify=True, **merged),
            timeout,
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
        result = await self._await_response(
            CMD_GET_SCHEDULE,
            self._client.send_message(CONFIG, CMD_GET_SCHEDULE, notify=True, index=index),
            timeout,
        )
        return Schedule.from_dict(result)

    async def set_schedule(self, schedule: Schedule, *, timeout: float | None = None) -> None:
        """Create or update a schedule.

        Args:
            schedule: The schedule to set.
            timeout: Seconds to wait for response. Defaults to default_timeout.

        Raises:
            ValueError: If the window ends before it begins. The device would
                accept such an entry and never act on it, so it is refused
                here rather than written and quietly ignored.
        """
        schedule.validate_for_send()
        # `build_set_schedule_message` owns the wire shape - the slot index
        # as a sibling of the schedule object, which firmware 1.7.18
        # requires - and is equally reachable from a message-level caller.
        await self._await_response(
            CMD_SET_SCHEDULE,
            self._client.send_message(
                CONFIG,
                CMD_SET_SCHEDULE,
                notify=True,
                **build_set_schedule_message(schedule.to_dict()),
            ),
            timeout,
        )

    async def delete_schedule(self, index: int, *, timeout: float | None = None) -> None:
        """Delete a schedule by index.

        Args:
            index: Schedule index to delete.
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await self._await_response(
            CMD_DELETE_SCHEDULE,
            self._client.send_message(CONFIG, CMD_DELETE_SCHEDULE, notify=True, index=index),
            timeout,
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
        indices = await self._await_response(
            CMD_GET_SCHEDULE_LIST,
            self._client.send_message(CONFIG, CMD_GET_SCHEDULE_LIST, notify=True),
            effective_timeout,
        )

        if not indices:
            self._schedules = []
            return []

        # `indices` comes straight off the wire. Iterating it unchecked
        # raised a bare TypeError out of this documented coroutine for any
        # scalar, and quietly fanned out one GET_SCHEDULE *per character*
        # for a string - 200 sequential round trips against a device that
        # rate-limits between messages.
        if not isinstance(indices, list):
            logger.warning(
                t(
                    "door.device_sent_non_list_schedule",
                    "Device sent a non-list schedule index list: %s",
                ),
                sanitize_text(indices, MAX_LOGGED_LENGTH),
            )
            self._schedules = []
            return []

        # Step 2: Fetch each schedule individually
        schedules = []
        for idx in indices:
            try:
                result = await self._await_response(
                    CMD_GET_SCHEDULE,
                    self._client.send_message(
                        CONFIG, CMD_GET_SCHEDULE, notify=True, **{FIELD_INDEX: idx}
                    ),
                    effective_timeout,
                )
                if result:
                    schedules.append(Schedule.from_dict(result))
            except TimeoutError:
                # %s, not %d: the index is only an int if the device says
                # so, and a string index turned this warning into a
                # logging-internal formatting error on stderr.
                logger.warning(
                    t("door.timeout_fetching_schedule", "Timeout fetching schedule %s"),
                    sanitize_text(idx),
                )
            except Exception:
                logger.exception(
                    t("door.error_fetching_schedule", "Error fetching schedule %s"),
                    sanitize_text(idx),
                )

        # Sorted for the same reason _on_schedule_update sorts: the public
        # `schedules` property must not be ordered by whichever code path
        # last touched it. GET_SCHEDULE_LIST returns slots, and a device
        # with slots filled out of order returns them out of order.
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
        """Log each failed step of a gathered refresh.

        ``refresh()``/``refresh_settings()`` gather with
        ``return_exceptions=True`` so one dead command cannot abort the
        rest. Without this, a device NAK or a drop during connect() would
        silently leave cached properties at their constructor defaults.
        """
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    t("door.refresh_step_failed", "Refresh step %s failed: %r"), name, result
                )

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
        result = await self._await_response(
            CMD_GET_DOOR_STATUS,
            self._client.send_message(CONFIG, CMD_GET_DOOR_STATUS, notify=True),
            timeout,
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
            self._await_response(
                CMD_GET_SETTINGS,
                self._client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True),
                effective_timeout,
            ),
            self._await_response(
                CMD_GET_NOTIFICATIONS,
                self._client.send_message(CONFIG, CMD_GET_NOTIFICATIONS, notify=True),
                effective_timeout,
            ),
            return_exceptions=True,
        )
        self._log_refresh_failures([CMD_GET_SETTINGS, CMD_GET_NOTIFICATIONS], results)

    async def refresh_battery(self, *, timeout: float | None = None) -> BatteryInfo:
        """Refresh and return battery info.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await self._await_response(
            CMD_GET_DOOR_BATTERY,
            self._client.send_message(CONFIG, CMD_GET_DOOR_BATTERY, notify=True),
            timeout,
        )
        return self._battery

    async def refresh_stats(self, *, timeout: float | None = None) -> None:
        """Refresh door statistics.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        await self._await_response(
            CMD_GET_DOOR_OPEN_STATS,
            self._client.send_message(CONFIG, CMD_GET_DOOR_OPEN_STATS, notify=True),
            timeout,
        )

    async def refresh_hardware_info(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Refresh and return hardware info.

        Args:
            timeout: Seconds to wait for response. Defaults to default_timeout.
        """
        result = await self._await_response(
            CMD_GET_HW_INFO,
            self._client.send_message(CONFIG, CMD_GET_HW_INFO, notify=True),
            timeout,
        )
        if isinstance(result, dict) and result:
            self._hw_info = result
        elif result:
            # The client stays liberal and resolves the future with
            # whatever the device sent; the facade must not cache a value
            # its three public properties would then choke on.
            logger.warning(
                t(
                    "door.ignoring_non_mapping_hardware_info",
                    "Ignoring non-mapping hardware info: %s",
                ),
                sanitize_text(result, MAX_LOGGED_LENGTH),
            )
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
                    logger.exception(t("door.error_status_callback", "Error in status callback"))

    def _on_settings(self, settings: dict[str, Any]) -> None:
        """Handle settings update from client.

        Wire values are protocol strings ("0"/"1"), so they must be coerced
        with make_bool - bool("0") is True, which would cache the inverse of
        the device state. Unrecognized values leave the cached state
        untouched.
        """
        # (settings field, cache attribute, inverted?)
        boolean_fields = (
            (FIELD_POWER, "_power", False),
            (FIELD_INSIDE, "_inside_sensor", False),
            (FIELD_OUTSIDE, "_outside_sensor", False),
            (FIELD_AUTO, "_auto", False),
            (FIELD_OUTSIDE_SENSOR_SAFETY_LOCK, "_safety_lock", False),
            # doorOptions is an int BITFIELD, so it is read through
            # `autoretract_from_door_options` rather than `make_bool`.
            (FIELD_AUTORETRACT, "_autoretract", False),
            # Inverted: cmd_lockout disabled means pet proximity keep open
            (FIELD_CMD_LOCKOUT, "_pet_proximity_keep_open", True),
        )
        for field_name, attr, inverted in boolean_fields:
            if field_name in settings:
                cached = getattr(self, attr)
                reader = (
                    autoretract_from_door_options if field_name == FIELD_AUTORETRACT else make_bool
                )
                value = _keep_flag(
                    reader(settings[field_name]),
                    (not cached) if inverted else cached,
                    field_name,
                )
                setattr(self, attr, (not value) if inverted else value)

        for field_name, attr in (
            (FIELD_SENSOR_TRIGGER_VOLTAGE, "_sensor_trigger_voltage"),
            (FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE, "_sleep_sensor_trigger_voltage"),
        ):
            if field_name in settings:
                setattr(
                    self, attr, _keep_int(settings[field_name], getattr(self, attr), field_name)
                )

        for callback in self._settings_callbacks:
            try:
                callback(settings)
            except Exception:
                logger.exception(t("door.error_settings_callback", "Error in settings callback"))

    # Sensor listeners are invoked by the client as callback(field, value);
    # the cached state is left untouched unless the value is a real bool
    # (see :func:`_keep_flag`).

    def _on_power_update(self, field_name: str, value: bool | None) -> None:
        self._power = _keep_flag(value, self._power, field_name)

    def _on_inside_update(self, field_name: str, value: bool | None) -> None:
        self._inside_sensor = _keep_flag(value, self._inside_sensor, field_name)

    def _on_outside_update(self, field_name: str, value: bool | None) -> None:
        self._outside_sensor = _keep_flag(value, self._outside_sensor, field_name)

    def _on_auto_update(self, field_name: str, value: bool | None) -> None:
        self._auto = _keep_flag(value, self._auto, field_name)

    def _on_safety_lock_update(self, field_name: str, value: bool | None) -> None:
        self._safety_lock = _keep_flag(value, self._safety_lock, field_name)

    def _on_autoretract_update(self, field_name: str, value: bool | None) -> None:
        self._autoretract = _keep_flag(value, self._autoretract, field_name)

    def _on_cmd_lockout_update(self, field_name: str, value: bool | None) -> None:
        # Inverted logic
        self._pet_proximity_keep_open = not _keep_flag(
            value, not self._pet_proximity_keep_open, field_name
        )

    def _on_battery_update(self, data: dict[str, Any]) -> None:
        """Handle battery update from client.

        Every field is type-checked on the way in (see the module-level
        facade cache guards). The ``dict.get(key, cached)`` defaults this
        used to rely on were dead code - ``_handle_battery`` always builds
        all three keys, holding None for a field the device omitted - so a
        reply that omitted ``batteryPercent`` replaced a good cached value
        with None and made ``battery.charging`` raise.
        """
        self._battery = BatteryInfo(
            percent=_keep_int(
                data.get(FIELD_BATTERY_PERCENT), self._battery.percent, "battery_percent"
            ),
            present=_keep_bool(
                data.get(FIELD_BATTERY_PRESENT), self._battery.present, "battery_present"
            ),
            ac_present=_keep_bool(
                data.get(FIELD_AC_PRESENT), self._battery.ac_present, "ac_present"
            ),
        )

    def _on_hold_time_update(self, value: int) -> None:
        """Handle hold time update (value is in centiseconds).

        A device that spells ``holdOpenTime`` as ``"200"`` made
        ``value / 100.0`` raise TypeError, which the client's listener
        isolation turned into a full traceback *per frame* while the cache
        stayed silently stale; and ``NaN`` (which ``json.loads`` accepts)
        was cached straight into a property documented ``-> float``.

        An arbitrary-precision *integer* fails the same way: it is legal
        JSON, passes every type check, and then makes ``value / 100.0``
        raise ``OverflowError`` - one unthrottled traceback per frame
        through the client's listener isolation, with the cache left
        silently stale. This is the one retained facade value with float
        arithmetic on it, so it is the one that passes ``maximum``. The
        bound is float representability, **not** a protocol ceiling:
        bounding it at the simulator's ``MAX_HOLD_TIME_CENTISECONDS`` would
        make the shipped facade refuse a device value on the authority of a
        reverse-engineered constant.
        """
        centiseconds = _keep_int(
            value,
            round(self._hold_time * 100),
            "hold_time",
            maximum=_FLOAT_REPRESENTABLE_MAX,
        )
        self._hold_time = centiseconds / 100.0

    def _on_sensor_trigger_voltage_update(self, value: int) -> None:
        self._sensor_trigger_voltage = _keep_int(
            value, self._sensor_trigger_voltage, FIELD_SENSOR_TRIGGER_VOLTAGE
        )

    def _on_sleep_sensor_trigger_voltage_update(self, value: int) -> None:
        self._sleep_sensor_trigger_voltage = _keep_int(
            value, self._sleep_sensor_trigger_voltage, FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE
        )

    def _on_remote_id_update(self, value: bool) -> None:
        self._has_remote_id = _keep_flag(value, self._has_remote_id, FIELD_HAS_REMOTE_ID)

    def _on_remote_key_update(self, value: bool) -> None:
        self._has_remote_key = _keep_flag(value, self._has_remote_key, FIELD_HAS_REMOTE_KEY)

    def _on_timezone_update(self, value: str) -> None:
        self._timezone = _keep_str(value, self._timezone, "timezone")

    def _on_time_update(self, value: str) -> None:
        self._device_time = _keep_str(value, self._device_time, FIELD_TIME)

    def _on_hw_info_update(self, data: dict[str, Any]) -> None:
        """Cache the device's hardware info.

        Guarded like every other value the facade *retains*: a scalar
        cached here poisons three documented public properties
        (``firmware_version``, ``hardware_version``, ``hardware_info`` all
        raise ``AttributeError``) with nothing in the log tying the failure
        to the frame that caused it, and it heals only on the next
        well-formed reply. The client already shields this listener, so the
        guard is defence in depth against a third-party client subclass
        calling it directly.

        ``_hw_info`` is not the only device payload the facade retains:
        ``_battery``, ``_total_open_cycles``, ``_total_auto_retracts`` and
        ``_timezone`` are retained too, and all of them go through the
        module-level ``_keep_*`` helpers.
        """
        if not isinstance(data, dict):
            logger.warning(
                t(
                    "door.ignoring_non_mapping_hardware_info",
                    "Ignoring non-mapping hardware info: %s",
                ),
                sanitize_field(data, MAX_LOGGED_LENGTH),
            )
            return
        self._hw_info = data

    # Stats and notification listeners are invoked by the client as
    # callback(field, value).

    def _on_total_cycles_update(self, field_name: str, value: int) -> None:
        self._total_open_cycles = _keep_int(value, self._total_open_cycles, field_name)

    def _on_total_retracts_update(self, field_name: str, value: int) -> None:
        self._total_auto_retracts = _keep_int(value, self._total_auto_retracts, field_name)

    def _on_notify_inside_on(self, field_name: str, value: bool | None) -> None:
        self._notifications.inside_on = _keep_flag(value, self._notifications.inside_on, field_name)

    def _on_notify_inside_off(self, field_name: str, value: bool | None) -> None:
        self._notifications.inside_off = _keep_flag(
            value, self._notifications.inside_off, field_name
        )

    def _on_notify_outside_on(self, field_name: str, value: bool | None) -> None:
        self._notifications.outside_on = _keep_flag(
            value, self._notifications.outside_on, field_name
        )

    def _on_notify_outside_off(self, field_name: str, value: bool | None) -> None:
        self._notifications.outside_off = _keep_flag(
            value, self._notifications.outside_off, field_name
        )

    def _on_notify_low_battery(self, field_name: str, value: bool | None) -> None:
        self._notifications.low_battery = _keep_flag(
            value, self._notifications.low_battery, field_name
        )

    async def _on_connect(self) -> None:
        """Handle connection established."""
        self._connected_event.set()
        # After a client-level auto-reconnect the cached state may be
        # stale; resynchronize before notifying callbacks. The initial
        # connect() performs its own refresh.
        if self._initialized:
            try:
                await self.refresh()
            except Exception:
                logger.exception(
                    t(
                        "door.error_refreshing_state_after_reconnect",
                        "Error refreshing state after reconnect",
                    )
                )
        for callback in self._connect_callbacks:
            try:
                callback()
            except Exception:
                logger.exception(t("door.error_connect_callback", "Error in connect callback"))

    async def _on_disconnect(self) -> None:
        """Handle connection lost."""
        self._connected_event.clear()
        self._latency = None  # Reset latency since we're no longer connected
        for callback in self._disconnect_callbacks:
            try:
                callback()
            except Exception:
                logger.exception(
                    t("door.error_disconnect_callback", "Error in disconnect callback")
                )

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
        nothing in the log to say the update was lost.
        """
        try:
            schedule = Schedule.from_dict(schedule_data)
        except ValueError as err:
            # `err` embeds `{value!r}` of the untrusted payload, so it is
            # length-capped before it reaches the log.
            logger.warning(
                t(
                    "door.ignoring_malformed_schedule_update_device",
                    "Ignoring malformed schedule update from device: %s",
                ),
                sanitize_field(err, MAX_LOGGED_LENGTH),
            )
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
                logger.exception(t("door.error_schedule_callback", "Error in schedule callback"))
