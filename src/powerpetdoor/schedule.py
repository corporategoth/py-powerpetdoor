# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Schedule utility functions for Power Pet Door.

This module provides pure utility functions for working with Power Pet Door
schedules, including validation, compression, and diffing.

It also owns both untrusted-data boundaries for schedules, which are the
two outer layers of a deliberate three-layer split:

1. **Python API (strict).** :class:`powerpetdoor.door.Schedule` and
   :class:`powerpetdoor.simulator.state.Schedule` use real Python types
   and only those - ``enabled: bool``, ``days_of_week: list[bool]``,
   ``hour: int``. Nothing in memory ever holds ``"1"``.
2. **Serialization (conforms to the wire).** :class:`ScheduleWireFormat`
   plus :func:`build_schedule_payload` are the *single* place strict
   Python values are turned into what the firmware expects, and the only
   place a field's wire spelling is decided. It is per *direction*:
   client->device and device->client are separate formats and are not
   required to agree.
3. **Deserialization (liberal).** :func:`coerce_schedule_int` and friends
   accept every spelling a real device might plausibly send (``true`` /
   ``"1"`` / ``1``) and coerce to the layer-1 types. Both parsers read
   through them, so hardening lands on both sides at once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import time
from typing import Any, Final

from .client import make_bool
from .const import (
    FIELD_DAYSOFWEEK,
    FIELD_ENABLED,
    FIELD_END_TIME_SUFFIX,
    FIELD_HOUR,
    FIELD_INDEX,
    FIELD_INSIDE,
    FIELD_INSIDE_PREFIX,
    FIELD_MINUTE,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_PREFIX,
    FIELD_SCHEDULE,
    FIELD_START_TIME_SUFFIX,
)
from .i18n import t
from .sanitize import sanitize_field, sanitize_text

_LOGGER = logging.getLogger(__name__)

#: Highest schedule slot index accepted from the wire. Also bounds the
#: number of slots a hostile SET_SCHEDULE stream can allocate.
MAX_SCHEDULE_INDEX = 255


def coerce_schedule_int(value: object, name: str, maximum: int) -> int:
    """Coerce an untrusted wire value to an int in ``0..maximum``.

    Args:
        value: Raw wire value.
        name: Field name, used in the error message.
        maximum: Largest accepted value (inclusive).

    Raises:
        ValueError: If the value is not numeric or is out of range.
    """
    try:
        result: int = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        # int(float("inf")) raises OverflowError, not ValueError: without it
        # `1e400` escapes as an unhandled exception and the caller reports a
        # generic "Command failed" plus a stack trace for a value this
        # validator meant to reject cleanly.
        raise ValueError(
            t(
                "schedule.schedule_must_number_got",
                "Schedule {name} must be a number, got {value!r}",
                name=name,
                value=value,
            )
        ) from None
    if not 0 <= result <= maximum:
        raise ValueError(
            t(
                "schedule.schedule_must_between_got",
                "Schedule {name} must be between 0 and {maximum}, got {result}",
                name=name,
                maximum=maximum,
                result=result,
            )
        )
    return result


def coerce_schedule_day(value: object, position: int) -> bool:
    """Coerce one untrusted ``daysOfWeek`` element to a boolean.

    Plain truthiness is the wrong tool: the very same object carries
    ``enabled`` as a ``"0"``/``"1"`` *string* on the wire, and ``bool("0")``
    is True - an access-control entry would silently become active on a day
    the caller disabled. Values are read the way every other wire flag in
    this project is read (``make_bool``), and anything that is not a
    recognizable flag is rejected rather than guessed at.

    Raises:
        ValueError: If the element is not a recognizable 0/1 flag.
    """
    flag = make_bool(value) if isinstance(value, (bool, int, str)) else None
    if not isinstance(flag, bool):
        raise ValueError(
            t(
                "schedule.schedule_daysofweek_must_got",
                "Schedule daysOfWeek[{position}] must be 0 or 1, got {value!r}",
                position=position,
                value=value,
            )
        )
    return flag


#: Widest legacy ``daysOfWeek`` bitmask: seven days, bit 0 = Sunday.
MAX_DAYS_BITMASK = 0b1111111


def coerce_schedule_days(value: object) -> list[bool]:
    """Coerce an untrusted ``daysOfWeek`` value to exactly 7 booleans.

    Accepts the protocol's 7-element list or the legacy integer bitmask.
    The bitmask is range-checked like every other numeric wire field:
    ``(-1 >> i) & 1`` is 1 forever, so an unbounded mask turned *every*
    negative integer into "active all seven days" - failing open, which is
    the exact opposite of the doctrine :func:`coerce_schedule_flag` documents.

    Raises:
        ValueError: If the value is neither of those shapes, an element is
            not a 0/1 flag, or the bitmask is out of range.
    """
    if isinstance(value, int):
        # Legacy bitmask -> [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
        if not 0 <= value <= MAX_DAYS_BITMASK:
            raise ValueError(
                t(
                    "schedule.schedule_daysofweek_bitmask_must_between",
                    "Schedule daysOfWeek bitmask must be between 0 and {MAX_DAYS_BITMASK}, got {value!r}",
                    MAX_DAYS_BITMASK=MAX_DAYS_BITMASK,
                    value=value,
                )
            )
        return [bool((value >> i) & 1) for i in range(7)]
    if isinstance(value, list) and len(value) == 7:
        return [coerce_schedule_day(day, i) for i, day in enumerate(value)]
    raise ValueError(
        t(
            "schedule.schedule_daysofweek_must_list_values",
            "Schedule daysOfWeek must be a list of 7 values, got {value!r}",
            value=value,
        )
    )


def coerce_schedule_flag(value: object, name: str) -> bool:
    """Coerce an untrusted schedule flag (``enabled``/``inside``/``outside``).

    Read with ``make_bool`` for the same reason the day flags are: the
    device sends ``enabled`` as the string ``"1"``/``"0"``, and plain
    truthiness reads ``"0"`` as True. Anything unrecognizable fails closed
    (disabled) rather than raising - an unreadable flag must never *grant*
    access, and a schedule the device already stores should not become
    unreadable because one flag has a novel spelling.

    Args:
        value: Raw wire value.
        name: Field name, used only for the debug log.

    Returns:
        The flag as a real ``bool``.
    """
    flag = make_bool(value) if isinstance(value, (bool, int, str)) else None
    if flag is None:
        _LOGGER.debug(
            t(
                "schedule.schedule_recognizable_flag_treating_as",
                "Schedule %s is not a recognizable flag (%s); treating it as off",
            ),
            name,
            sanitize_field(value),
        )
        return False
    return flag


def require_schedule_field(data: dict, key: str) -> object:
    """Return ``data[key]``, rejecting the payload when the field is absent.

    Raises:
        ValueError: If ``key`` is missing.
    """
    if key not in data:
        raise ValueError(
            t(
                "schedule.schedule_missing_required_field",
                "Schedule is missing required field {key!r}",
                key=key,
            )
        )
    return data[key]


def coerce_schedule_time(value: object, name: str, *, require_hour: bool = True) -> tuple[int, int]:
    """Coerce an untrusted ``{hour, min}`` mapping to a valid (hour, minute).

    The minute defaults to 0, matching the protocol's own ``{hour: H, min: 0}``
    shape. Hour 24 is accepted: ``24:00`` is a natural end-of-day encoding and
    the firmware is not ours to constrain.

    Args:
        require_hour: True when validating a schedule someone is asking us to
            store, where a missing hour is a malformed request worth refusing.
            False when parsing a schedule the *device* reported: dropping the
            whole entry would hide a schedule that really exists on the door,
            so an absent hour becomes midnight instead.

    Raises:
        ValueError: If the value is not a mapping, the fields are not valid
            times, or the hour is absent and ``require_hour`` is set.
    """
    if not isinstance(value, dict):
        raise ValueError(
            t(
                "schedule.schedule_must_object_got",
                "Schedule {name} must be an object, got {value!r}",
                name=name,
                value=value,
            )
        )
    if require_hour and FIELD_HOUR not in value:
        raise ValueError(
            t(
                "schedule.schedule_must_specify_got",
                "Schedule {name} must specify {FIELD_HOUR}, got {value!r}",
                name=name,
                FIELD_HOUR=FIELD_HOUR,
                value=value,
            )
        )
    hour = coerce_schedule_int(value.get(FIELD_HOUR, 0), f"{name} hour", 24)
    minute = coerce_schedule_int(value.get(FIELD_MINUTE, 0), f"{name} minute", 59)
    return hour, minute


def week_0_mon_to_sun(val: int) -> int:
    """Convert weekday from Monday=0 format to Sunday=0 format.

    Args:
        val: Day of week where Monday=0, Sunday=6

    Returns:
        Day of week where Sunday=0, Saturday=6
    """
    return (val + 8) % 7


def week_0_sun_to_mon(val: int) -> int:
    """Convert weekday from Sunday=0 format to Monday=0 format.

    Args:
        val: Day of week where Sunday=0, Saturday=6

    Returns:
        Day of week where Monday=0, Sunday=6
    """
    return (val + 6) % 7


def validate_schedule_entry(sched: dict) -> bool:
    """Validate a schedule entry has required fields and valid data.

    Args:
        sched: Schedule entry dictionary

    Returns:
        True if valid, False otherwise
    """
    try:
        # Check required fields exist
        if FIELD_INDEX not in sched:
            _LOGGER.debug(
                t(
                    "schedule.schedule_entry_missing_index_field",
                    "Schedule entry missing index field: %s",
                ),
                sanitize_text(sched),
            )
            return False

        if FIELD_DAYSOFWEEK not in sched:
            _LOGGER.debug(
                t(
                    "schedule.schedule_entry_missing_daysofweek_field",
                    "Schedule entry missing daysOfWeek field: %s",
                ),
                sanitize_text(sched),
            )
            return False

        # Validate daysOfWeek is a list of 7 elements
        if not isinstance(sched[FIELD_DAYSOFWEEK], list) or len(sched[FIELD_DAYSOFWEEK]) != 7:
            _LOGGER.debug(
                t(
                    "schedule.schedule_entry_has_invalid_daysofweek",
                    "Schedule entry has invalid daysOfWeek format: %s",
                ),
                sanitize_text(sched[FIELD_DAYSOFWEEK]),
            )
            return False

        # Validate time fields if inside or outside is enabled
        if sched.get(FIELD_INSIDE, False):
            in_start_key = FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX
            in_end_key = FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX
            if in_start_key not in sched or in_end_key not in sched:
                _LOGGER.debug(
                    t(
                        "schedule.schedule_entry_missing_inside_time",
                        "Schedule entry missing inside time fields: %s",
                    ),
                    sanitize_text(sched),
                )
                return False
            if FIELD_HOUR not in sched[in_start_key] or FIELD_MINUTE not in sched[in_start_key]:
                _LOGGER.debug(
                    t(
                        "schedule.schedule_entry_has_invalid_inside",
                        "Schedule entry has invalid inside start time: %s",
                    ),
                    sanitize_text(sched[in_start_key]),
                )
                return False
            if FIELD_HOUR not in sched[in_end_key] or FIELD_MINUTE not in sched[in_end_key]:
                _LOGGER.debug(
                    t(
                        "schedule.schedule_entry_has_invalid_inside_1",
                        "Schedule entry has invalid inside end time: %s",
                    ),
                    sanitize_text(sched[in_end_key]),
                )
                return False

        if sched.get(FIELD_OUTSIDE, False):
            out_start_key = FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX
            out_end_key = FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX
            if out_start_key not in sched or out_end_key not in sched:
                _LOGGER.debug(
                    t(
                        "schedule.schedule_entry_missing_outside_time",
                        "Schedule entry missing outside time fields: %s",
                    ),
                    sanitize_text(sched),
                )
                return False
            if FIELD_HOUR not in sched[out_start_key] or FIELD_MINUTE not in sched[out_start_key]:
                _LOGGER.debug(
                    t(
                        "schedule.schedule_entry_has_invalid_outside",
                        "Schedule entry has invalid outside start time: %s",
                    ),
                    sanitize_text(sched[out_start_key]),
                )
                return False
            if FIELD_HOUR not in sched[out_end_key] or FIELD_MINUTE not in sched[out_end_key]:
                _LOGGER.debug(
                    t(
                        "schedule.schedule_entry_has_invalid_outside_1",
                        "Schedule entry has invalid outside end time: %s",
                    ),
                    sanitize_text(sched[out_end_key]),
                )
                return False

        return True
    except Exception as e:
        _LOGGER.error(
            t("schedule.error_validating_schedule_entry", "Error validating schedule entry: %s"),
            e,
            exc_info=True,
        )
        return False


# =============================================================================
# WIRE REPRESENTATION - the serialization boundary (layer 2)
#
# WIRE REPRESENTATION - DETERMINED BY DEVICE FIRMWARE, NOT BY OUR PREFERENCE.
#
# Everything below decides how a strict Python value is *spelled* on the
# wire. Do not "tidy" it to match the Python types, to match the other
# direction, or to match docs/protocol.md - that document is
# reverse-engineered and is not authority over what the firmware accepts.
# The client->device `enabled` field is a JSON boolean, not the "1"/"0" the
# doc describes: the boolean is what has actually run against real Power Pet
# Doors since v0.1.0.
#
# Each field is one line in one of the two format constants, so when a real
# device settles a question, flipping that field's spelling is a one-line
# change here with no ripple into the Python API (layer 1) or the parsers
# (layer 3, which stay liberal and accept every spelling regardless).
# =============================================================================


def wire_json_bool(value: bool) -> bool:
    """Spell a flag as a JSON boolean (``true``/``false``)."""
    return bool(value)


def wire_bool_string(value: bool) -> str:
    """Spell a flag as the string ``"true"``/``"false"``.

    **Verified against firmware 1.7.18**: this is how the device spells
    every flag inside ``GET_SETTINGS.settings`` (except ``doorOptions``,
    which is an int) and every flag inside
    ``GET_NOTIFICATIONS.notifications``.
    """
    return "true" if value else "false"


def wire_int_flag(value: bool) -> int:
    """Spell a flag as the integer ``1``/``0``.

    **Verified against firmware 1.7.18**: this is how the device spells
    ``inside``/``outside`` in ``GET_SENSORS``, the echoed field in an
    individual setting reply (``ENABLE_INSIDE`` -> ``{"inside": 1}``),
    ``doorOptions``, and ``enabled``/``inside``/``outside``/``daysOfWeek``
    inside a ``GET_SCHEDULE`` entry.
    """
    return 1 if value else 0


def wire_int(value: int) -> int:
    """Spell a number as a JSON integer."""
    return int(value)


@dataclass(frozen=True)
class ScheduleWireFormat:
    """How each schedule field is spelled on the wire, for one direction.

    One callable per field, so a firmware finding changes exactly one line.
    See the module docstring for the three-layer split this sits in the
    middle of.
    """

    index: Callable[[int], Any]
    enabled: Callable[[bool], Any]
    inside: Callable[[bool], Any]
    outside: Callable[[bool], Any]
    day: Callable[[bool], Any]
    hour: Callable[[int], Any]
    minute: Callable[[int], Any]

    def time(self, hour: int, minute: int) -> dict[str, Any]:
        """Spell one ``{hour, min}`` block."""
        return {FIELD_HOUR: self.hour(hour), FIELD_MINUTE: self.minute(minute)}


#: **client -> device**: the shape the library SENDS in ``SET_SCHEDULE``.
#: These spellings have run against real hardware since v0.1.0.
SCHEDULE_WIRE_TO_DEVICE = ScheduleWireFormat(
    index=wire_int,
    enabled=wire_json_bool,  # JSON boolean - proven against real firmware
    inside=wire_json_bool,
    outside=wire_json_bool,
    day=wire_int_flag,  # 1/0 integers
    hour=wire_int,
    minute=wire_int,
)

#: **device -> client**: the shape a real door REPLIES with, and therefore
#: what the simulator emits. **Verified against firmware 1.7.18**: a
#: ``GET_SCHEDULE`` reply spells ``enabled``, ``inside`` and ``outside`` as
#: the integers ``1``/``0``, and ``daysOfWeek`` as a list of integers.
#:
#: It differs from :data:`SCHEDULE_WIRE_TO_DEVICE` in three fields, and that
#: is not a bug: the two are opposite directions, not twins. What we SEND is
#: unchanged (JSON booleans, which real doors have accepted since v0.1.0);
#: only what a door SAYS is pinned here.
SCHEDULE_WIRE_FROM_DEVICE = replace(
    SCHEDULE_WIRE_TO_DEVICE,
    enabled=wire_int_flag,
    inside=wire_int_flag,
    outside=wire_int_flag,
)


#: How the device spells "the end of the day": hour 24, minute 0.
#:
#: **Measured against firmware 1.7.18.** A window of ``20:00-24:00`` reports
#: the sensor enabled at 21:07, and ``00:00-24:00`` enables it outright, so
#: hour 24 is not merely tolerated on input - the schedule engine honours it.
END_OF_DAY: Final = (24, 0)

#: Midnight, which is a legal START and never a meaningful END.
MIDNIGHT: Final = (0, 0)


def normalise_window_end(end: tuple[int, int]) -> tuple[int, int]:
    """Rewrite a window end of ``00:00`` to the device's ``24:00``.

    Midnight is the *first* minute of a day, so as an END it says the
    opposite of what anyone writing it means. The device does not reinterpret
    it: measured on firmware 1.7.18, a window of ``20:00-00:00`` leaves the
    sensor DISABLED, because the engine simply compares ``start <= now < end``
    and ``end`` of 0 is never greater than a start of 1200. The entry is
    stored perfectly and never fires.

    So "22:00 until midnight" has to be spelled ``22:00-24:00`` on the wire,
    and this is where that translation happens. Applied on the SEND path
    only - what a door reports is read back exactly as it behaves.
    """
    return END_OF_DAY if tuple(end) == MIDNIGHT else end


def window_minutes(start: tuple[int, int], end: tuple[int, int]) -> tuple[int, int]:
    """``(start, end)`` as minutes past midnight, with ``24:00`` as 1440."""
    return start[0] * 60 + start[1], end[0] * 60 + end[1]


def schedule_window_is_empty(start: tuple[int, int], end: tuple[int, int]) -> bool:
    """Whether the device would store this window and never act on it.

    The engine is ``start <= now < end``. Any window whose end does not
    exceed its start therefore matches no minute at all.

    **Measured against firmware 1.7.18**, all with the entry enabled and
    ``timersEnabled`` on: ``16:01-16:01`` and ``21:01-21:01`` (start == end)
    both report the sensor DISABLED, as do ``20:00-00:00`` and
    ``00:00-00:00``. A window that ends before it begins does NOT wrap past
    midnight - ``23:00-21:30`` reports disabled both on the day it names and
    on the day after, so it is neither a same-day wrap nor a spill into
    tomorrow. It is nothing.
    """
    start_min, end_min = window_minutes(start, end)
    return end_min <= start_min


def build_schedule_payload(
    fmt: ScheduleWireFormat,
    *,
    index: int,
    enabled: bool,
    days_of_week: Sequence[bool],
    inside: bool,
    outside: bool,
    start: tuple[int, int],
    end: tuple[int, int],
) -> dict[str, Any]:
    """Serialize one schedule entry in ``fmt``'s direction.

    The single serialization site for schedules: both
    :meth:`powerpetdoor.door.Schedule.to_dict` (client->device) and the
    simulator's :meth:`Schedule.to_dict` (device->client) go through it, so
    the only difference between the two directions is the format constant
    they pass.

    A schedule entry gates ONE sensor, so one time window is supplied and
    the unselected sensor's block is zeroed - which is why callers pass a
    single ``start``/``end`` rather than two.

    Args:
        fmt: Wire format for the direction being written.
        index: Schedule slot.
        enabled: Whether the entry is active.
        days_of_week: Seven flags, ``[Sun..Sat]``.
        inside: Whether the entry gates the inside sensor.
        outside: Whether the entry gates the outside sensor.
        start: ``(hour, minute)`` the window opens.
        end: ``(hour, minute)`` the window closes.
    """
    payload: dict[str, Any] = {
        FIELD_INDEX: fmt.index(index),
        FIELD_ENABLED: fmt.enabled(enabled),
        FIELD_DAYSOFWEEK: [fmt.day(bool(day)) for day in days_of_week],
        FIELD_INSIDE: fmt.inside(inside),
        FIELD_OUTSIDE: fmt.outside(outside),
    }
    for selected, prefix in ((inside, FIELD_INSIDE_PREFIX), (outside, FIELD_OUTSIDE_PREFIX)):
        window = (start, end) if selected else ((0, 0), (0, 0))
        payload[prefix + FIELD_START_TIME_SUFFIX] = fmt.time(*window[0])
        payload[prefix + FIELD_END_TIME_SUFFIX] = fmt.time(*window[1])
    return payload


def build_set_schedule_message(schedule: dict[str, Any]) -> dict[str, Any]:
    """Build the ``SET_SCHEDULE`` message fields for a schedule payload.

    **Verified against firmware 1.7.18**: the device requires the slot
    ``index`` as a *sibling* of the ``schedule`` object. A message carrying
    only ``schedule`` is answered ``success: "false"`` and writes nothing,
    however the entry itself is spelled.

    This is the single place that shape is built, so both the friendly
    facade (:meth:`powerpetdoor.door.PowerPetDoor.set_schedule`) and a
    message-level caller get it right::

        client.send_message(
            CONFIG, CMD_SET_SCHEDULE, notify=True,
            **build_set_schedule_message(schedule.to_dict()),
        )

    Args:
        schedule: A client->device schedule payload, i.e. the output of
            :func:`build_schedule_payload` with
            :data:`SCHEDULE_WIRE_TO_DEVICE` (which is what
            :meth:`powerpetdoor.door.Schedule.to_dict` returns).

    Returns:
        ``{"index": <slot>, "schedule": <payload>}``, ready to splat into
        :meth:`powerpetdoor.client.PowerPetDoorClient.send_message`.

    Raises:
        ValueError: If the payload carries no ``index`` to address.
    """
    if FIELD_INDEX not in schedule:
        raise ValueError(
            t(
                "schedule.schedule_payload_missing_required_field",
                "Schedule payload is missing required field {FIELD_INDEX!r}",
                FIELD_INDEX=FIELD_INDEX,
            )
        )
    return {FIELD_INDEX: schedule[FIELD_INDEX], FIELD_SCHEDULE: schedule}


#: Schedule template with all fields initialized to defaults.
#:
#: Built through the client->device boundary above, because every
#: ``compress_schedule()`` result is a deep copy of it and those payloads
#: are SENT to the device.
schedule_template = build_schedule_payload(
    SCHEDULE_WIRE_TO_DEVICE,
    index=0,
    enabled=True,
    days_of_week=[False] * 7,
    inside=False,
    outside=False,
    start=(0, 0),
    end=(0, 0),
)


def _require_complete_entry(sched: dict, position: int) -> None:
    """Validate that a schedule entry is fully populated for compression.

    compress_schedule() reads every field of every entry, so each entry
    must carry a 7-element daysOfWeek list, the inside/outside flags, and
    all four time sub-dicts with hour/min keys. Start from
    ``schedule_template`` (deep-copied) to guarantee completeness.

    Raises:
        ValueError: With a clear message identifying the offending entry.
    """
    if not isinstance(sched, dict):
        raise ValueError(
            t(
                "schedule.schedule_entry_dict",
                "Schedule entry {position} is not a dict: {sched!r}",
                position=position,
                sched=sched,
            )
        )

    days = sched.get(FIELD_DAYSOFWEEK)
    if not isinstance(days, list) or len(days) != 7:
        raise ValueError(
            t(
                "schedule.schedule_entry_needs_element_list",
                "Schedule entry {position} needs a 7-element {FIELD_DAYSOFWEEK!r} list: {sched!r}",
                position=position,
                FIELD_DAYSOFWEEK=FIELD_DAYSOFWEEK,
                sched=sched,
            )
        )

    for flag in (FIELD_INSIDE, FIELD_OUTSIDE):
        if flag not in sched:
            raise ValueError(
                t(
                    "schedule.schedule_entry_missing",
                    "Schedule entry {position} is missing {flag!r}: {sched!r}",
                    position=position,
                    flag=flag,
                    sched=sched,
                )
            )

    time_keys = (
        FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX,
        FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX,
        FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX,
        FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX,
    )
    for key in time_keys:
        time_field = sched.get(key)
        if (
            not isinstance(time_field, dict)
            or FIELD_HOUR not in time_field
            or FIELD_MINUTE not in time_field
        ):
            raise ValueError(
                t(
                    "schedule.schedule_entry_missing_time_field",
                    "Schedule entry {position} is missing time field {key!r} (expected a dict with {FIELD_HOUR!r}/{FIELD_MINUTE!r}): {sched!r}",
                    position=position,
                    key=key,
                    FIELD_HOUR=FIELD_HOUR,
                    FIELD_MINUTE=FIELD_MINUTE,
                    sched=sched,
                )
            )


def compress_schedule(schedule: list[dict]) -> list[dict]:
    """Compress a schedule to minimize the number of entries.

    Takes a list of schedule entries and combines/merges them where possible:
    - Overlapping time periods on the same day are merged
    - Same time periods on different days are combined
    - Inside and outside entries with matching times/days are combined

    Every entry must be fully populated (all four time sub-dicts, the
    inside/outside flags, and a 7-element daysOfWeek list) - start from
    ``schedule_template``. The input is not modified.

    Args:
        schedule: List of fully-populated schedule entry dictionaries

    Returns:
        Compressed list of schedule entries with sequential indices

    Raises:
        ValueError: If an entry is missing required fields.
    """
    for position, sched in enumerate(schedule):
        _require_complete_entry(sched, position)

    expanded_sched: dict[str, dict[int, list[dict[str, time]]]] = {
        FIELD_INSIDE: {},
        FIELD_OUTSIDE: {},
    }

    # Step 1 .. expand
    for sched in schedule:
        in_start = time(
            sched[FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_HOUR],
            sched[FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_MINUTE],
        )
        in_end = time(
            sched[FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_HOUR],
            sched[FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_MINUTE],
        )
        if in_end < in_start:
            in_start, in_end = in_end, in_start
        out_start = time(
            sched[FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_HOUR],
            sched[FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_MINUTE],
        )
        out_end = time(
            sched[FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_HOUR],
            sched[FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_MINUTE],
        )
        if out_end < out_start:
            out_start, out_end = out_end, out_start

        for day in range(len(sched[FIELD_DAYSOFWEEK])):
            # make_bool, not truthiness: bool("0") is True, and a firmware
            # variant that sends "0"/"1" day flags (as it already does for
            # `enabled`) would otherwise expand to every day of the week.
            if make_bool(sched[FIELD_DAYSOFWEEK][day]) is True:
                if sched[FIELD_INSIDE]:
                    daysched = expanded_sched[FIELD_INSIDE].setdefault(day, [])
                    daysched.append({"start": in_start, "end": in_end})
                if sched[FIELD_OUTSIDE]:
                    daysched = expanded_sched[FIELD_OUTSIDE].setdefault(day, [])
                    daysched.append({"start": out_start, "end": out_end})

    # Step 2 .. Combine adjacent or overlapping
    def combine_overlapping(xsched: dict) -> None:
        for daysched in xsched.values():
            daysched.sort(key=lambda d: d["start"])

            i = 0
            while i < len(daysched) - 1:
                if daysched[i]["end"] >= daysched[i + 1]["start"]:
                    if daysched[i]["end"] < daysched[i + 1]["end"]:
                        daysched[i]["end"] = daysched[i + 1]["end"]
                    del daysched[i + 1]
                else:
                    i = i + 1

    combine_overlapping(expanded_sched[FIELD_INSIDE])
    combine_overlapping(expanded_sched[FIELD_OUTSIDE])

    # Step 3 .. Combine days of week
    def collapse_split_field(xsched: dict) -> list:
        out: list[dict[str, Any]] = []
        for day, daysched in xsched.items():
            for sched in daysched:
                found = False
                for ent in out:
                    if ent["start"] == sched["start"] and ent["end"] == sched["end"]:
                        ent[FIELD_DAYSOFWEEK][day] = True
                        found = True
                        break
                if not found:
                    # Booleans in memory; the 1/0 wire spelling is applied
                    # once, at the serialization boundary (layer 1 vs 2).
                    ent = {
                        "start": sched["start"],
                        "end": sched["end"],
                        FIELD_DAYSOFWEEK: [False] * 7,
                    }
                    ent[FIELD_DAYSOFWEEK][day] = True
                    out.append(ent)
        return out

    split_sched = {
        FIELD_INSIDE: collapse_split_field(expanded_sched[FIELD_INSIDE]),
        FIELD_OUTSIDE: collapse_split_field(expanded_sched[FIELD_OUTSIDE]),
    }

    # Step 4 .. Combine Inside & Outside entries
    final_sched = []
    for sched in split_sched[FIELD_INSIDE]:
        ent = {
            FIELD_INSIDE: True,
            FIELD_OUTSIDE: False,
            FIELD_DAYSOFWEEK: sched[FIELD_DAYSOFWEEK],
            "start": sched["start"],
            "end": sched["end"],
        }
        final_sched.append(ent)
    for sched in split_sched[FIELD_OUTSIDE]:
        found = False
        for ent in final_sched:
            if (
                ent["start"] == sched["start"]
                and ent["end"] == sched["end"]
                and ent[FIELD_DAYSOFWEEK] == sched[FIELD_DAYSOFWEEK]
            ):
                ent[FIELD_OUTSIDE] = True
                found = True
                break
        if not found:
            ent = {
                FIELD_INSIDE: False,
                FIELD_OUTSIDE: True,
                FIELD_DAYSOFWEEK: sched[FIELD_DAYSOFWEEK],
                "start": sched["start"],
                "end": sched["end"],
            }
            final_sched.append(ent)

    # Step 5, serialize. These payloads are SENT, so they go through the
    # client->device boundary rather than spelling any field by hand.
    return [
        build_schedule_payload(
            SCHEDULE_WIRE_TO_DEVICE,
            index=index,
            enabled=True,
            days_of_week=sched[FIELD_DAYSOFWEEK],
            inside=sched[FIELD_INSIDE],
            outside=sched[FIELD_OUTSIDE],
            start=(sched["start"].hour, sched["start"].minute),
            end=(sched["end"].hour, sched["end"].minute),
        )
        for index, sched in enumerate(final_sched)
    ]


def schedule_entry_content_key(entry: dict) -> tuple:
    """Create a hashable key representing schedule entry content (ignoring index).

    This allows comparing entries by their actual content rather than their index,
    which is important for incremental sync since compression may reassign indices.

    Every flag is read through the shared coercers, so the wire spellings
    the rest of the codebase already accepts (``"1"``/``1``/``true``) all
    collapse to the same key. This is the third reader of ``daysOfWeek``
    and it was the only one still comparing the field raw: against the
    firmware variant that sends ``["1", ...]`` - the one
    ``compress_schedule`` and ``coerce_schedule_day`` were both hardened
    for - every entry looked changed, so the incremental-sync path this
    function exists to enable issued a full ``SET_SCHEDULE`` sweep at every
    sync against a single-connection, rate-limited device.

    Args:
        entry: Schedule entry dictionary

    Returns:
        Tuple that can be used as a dict key for comparison
    """
    # Extract time values
    in_start = (
        entry.get(FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX, {}).get(FIELD_HOUR, 0),
        entry.get(FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX, {}).get(FIELD_MINUTE, 0),
    )
    in_end = (
        entry.get(FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX, {}).get(FIELD_HOUR, 0),
        entry.get(FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX, {}).get(FIELD_MINUTE, 0),
    )
    out_start = (
        entry.get(FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX, {}).get(FIELD_HOUR, 0),
        entry.get(FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX, {}).get(FIELD_MINUTE, 0),
    )
    out_end = (
        entry.get(FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX, {}).get(FIELD_HOUR, 0),
        entry.get(FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX, {}).get(FIELD_MINUTE, 0),
    )

    # Every flag through the shared coercers (make_bool under the hood), so
    # "1"/1/True are one key and "0"/0/False/absent are another. The three
    # hand-rolled `== "1"` normalizations that used to live here were the
    # last places in the tree reading a wire flag without make_bool.
    enabled = coerce_schedule_flag(entry.get(FIELD_ENABLED, True), FIELD_ENABLED)
    inside = coerce_schedule_flag(entry.get(FIELD_INSIDE, False), FIELD_INSIDE)
    outside = coerce_schedule_flag(entry.get(FIELD_OUTSIDE, False), FIELD_OUTSIDE)

    days = entry.get(FIELD_DAYSOFWEEK, [0] * 7)
    try:
        day_key: tuple = tuple(coerce_schedule_days(days))
    except ValueError:
        # Unrecognizable day masks still have to produce *some* key rather
        # than raise out of a diffing helper; keep them distinct from every
        # readable mask by tagging the raw repr.
        day_key = ("?", repr(days))

    return (
        day_key,
        inside,
        outside,
        enabled,
        in_start,
        in_end,
        out_start,
        out_end,
    )


def compute_schedule_diff(
    current_schedule: list[dict], new_schedule: list[dict]
) -> tuple[list[int], list[dict]]:
    """Compare current and new schedules to determine what needs to change.

    This function optimizes schedule updates by:
    1. Keeping entries that already match (no change needed)
    2. Reusing indices from entries to be deleted for new entries (SET instead of DELETE+ADD)
    3. Only deleting entries when there are more current entries than new entries

    Neither input list is modified: the returned ``entries_to_set`` are
    deep copies of the new entries with their ``index`` field reassigned.

    ``current_schedule`` may be raw device dicts (the docstring below
    invites exactly that, and this helper is a public export), so every
    index read out of it goes through :func:`coerce_schedule_int` rather
    than being trusted. A current entry whose index is not a usable slot
    number cannot be addressed by ``SET_SCHEDULE``/``DELETE_SCHEDULE`` at
    all, so it is skipped with a warning - instead of raising ``TypeError``
    out of a public helper on a mixed list, or silently handing the caller a
    payload with ``"index": null``.

    Args:
        current_schedule: List of current schedule entries on device
        new_schedule: List of desired schedule entries

    Returns:
        Tuple of (entries_to_delete, entries_to_set) where:
        - entries_to_delete: list of indices to delete from device
        - entries_to_set: list of schedule entries (copies) to add/update
          via SET_SCHEDULE
    """
    # Content key -> the (validated) slot that content occupies on the device.
    current_by_content: dict[tuple, int] = {}
    current_indices: set[int] = set()
    for entry in current_schedule:
        try:
            index = coerce_schedule_int(entry.get(FIELD_INDEX), "index", MAX_SCHEDULE_INDEX)
        except ValueError as err:
            _LOGGER.warning(
                t(
                    "schedule.ignoring_current_schedule_entry_has",
                    "Ignoring a current schedule entry that has no usable index: %s",
                ),
                sanitize_text(err),
            )
            continue
        current_by_content[schedule_entry_content_key(entry)] = index
        current_indices.add(index)

    # Find entries that already exist (no change needed) and track which new entries need to be set
    entries_to_set: list[dict] = []
    matched_indices: set[int] = set()

    for entry in new_schedule:
        key = schedule_entry_content_key(entry)
        if key in current_by_content:
            # This content already exists - no change needed
            matched_indices.add(current_by_content[key])
        else:
            # This is a new/changed entry that needs to be SET. Copy it so
            # the index reassignment below never mutates the caller's input.
            entries_to_set.append(deepcopy(entry))

    # Indices that can be reused (current indices that weren't matched)
    reusable_indices = sorted(current_indices - matched_indices)

    # Assign indices to entries that need to be SET
    for i, entry in enumerate(entries_to_set):
        if i < len(reusable_indices):
            # Reuse an existing index (this is an UPDATE)
            entry[FIELD_INDEX] = reusable_indices[i]
        else:
            # Need a new index - the lowest slot nothing else is using.
            # `matched | reusable` is exactly `current_indices`, so the only
            # thing that has to be added is the slots already handed to
            # *earlier* brand-new entries in this same loop - without that,
            # two new entries share one index and one silently overwrites
            # the other.
            used_indices = (
                matched_indices
                | set(reusable_indices)
                | {e[FIELD_INDEX] for e in entries_to_set[:i]}
            )
            new_index = 0
            while new_index in used_indices:
                new_index += 1
            entry[FIELD_INDEX] = new_index

    # Delete any leftover reusable indices we didn't use (the slice is
    # empty whenever there are at least as many new entries as slots).
    entries_to_delete = reusable_indices[len(entries_to_set) :]

    return (entries_to_delete, entries_to_set)
