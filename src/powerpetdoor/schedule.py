# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Schedule utility functions for Power Pet Door.

This module provides pure utility functions for working with Power Pet Door
schedules, including validation, compression, and diffing.

It also owns the single set of coercion helpers used to turn an untrusted
``daysOfWeek``/time/index payload into safe values
(:func:`coerce_schedule_int` and friends). Both schedule parsers - the
library's :class:`powerpetdoor.door.Schedule` and the simulator's
:class:`powerpetdoor.simulator.state.Schedule` - read wire data through
them, so hardening lands on both sides at once.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import time
from typing import Any, cast

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
    FIELD_START_TIME_SUFFIX,
)
from .sanitize import sanitize_text

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
        raise ValueError(f"Schedule {name} must be a number, got {value!r}") from None
    if not 0 <= result <= maximum:
        raise ValueError(f"Schedule {name} must be between 0 and {maximum}, got {result}")
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
        raise ValueError(f"Schedule daysOfWeek[{position}] must be 0 or 1, got {value!r}")
    return flag


#: Widest legacy ``daysOfWeek`` bitmask: seven days, bit 0 = Sunday.
MAX_DAYS_BITMASK = 0b1111111


def coerce_schedule_days(value: object) -> list[bool]:
    """Coerce an untrusted ``daysOfWeek`` value to exactly 7 booleans.

    Accepts the protocol's 7-element list or the legacy integer bitmask.
    The bitmask is range-checked like every other numeric wire field:
    ``(-1 >> i) & 1`` is 1 forever, so an unbounded mask turned *every*
    negative integer into "active all seven days" - failing open, which is
    the exact opposite of the doctrine :func:`coerce_schedule_flag`
    documents (R5-L1).

    Raises:
        ValueError: If the value is neither of those shapes, an element is
            not a 0/1 flag, or the bitmask is out of range.
    """
    if isinstance(value, int):
        # Legacy bitmask -> [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
        if not 0 <= value <= MAX_DAYS_BITMASK:
            raise ValueError(
                f"Schedule daysOfWeek bitmask must be between 0 and {MAX_DAYS_BITMASK}, "
                f"got {value!r}"
            )
        return [bool((value >> i) & 1) for i in range(7)]
    if isinstance(value, list) and len(value) == 7:
        return [coerce_schedule_day(day, i) for i, day in enumerate(value)]
    raise ValueError(f"Schedule daysOfWeek must be a list of 7 values, got {value!r}")


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
            "Schedule %s is not a recognizable flag (%s); treating it as off",
            name,
            sanitize_text(value),
        )
        return False
    return flag


def require_schedule_field(data: dict, key: str) -> object:
    """Return ``data[key]``, rejecting the payload when the field is absent.

    Raises:
        ValueError: If ``key`` is missing.
    """
    if key not in data:
        raise ValueError(f"Schedule is missing required field {key!r}")
    return data[key]


def coerce_schedule_time(value: object, name: str) -> tuple[int, int]:
    """Coerce an untrusted ``{hour, min}`` mapping to a valid (hour, minute).

    The hour is required: this entry's whole purpose is to gate sensor
    access, so materializing a permissive window out of an absent field is
    the wrong way to fail (L5). The minute defaults to 0, matching the
    protocol's own ``{hour: H, min: 0}`` shape.

    Raises:
        ValueError: If the value is not a mapping, carries no hour, or the
            fields are not valid times.
    """
    if not isinstance(value, dict):
        raise ValueError(f"Schedule {name} must be an object, got {value!r}")
    if FIELD_HOUR not in value:
        raise ValueError(f"Schedule {name} must specify {FIELD_HOUR}, got {value!r}")
    hour = coerce_schedule_int(value[FIELD_HOUR], f"{name} hour", 23)
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
            _LOGGER.debug("Schedule entry missing index field: %s", sanitize_text(sched))
            return False

        if FIELD_DAYSOFWEEK not in sched:
            _LOGGER.debug("Schedule entry missing daysOfWeek field: %s", sanitize_text(sched))
            return False

        # Validate daysOfWeek is a list of 7 elements
        if not isinstance(sched[FIELD_DAYSOFWEEK], list) or len(sched[FIELD_DAYSOFWEEK]) != 7:
            _LOGGER.debug(
                "Schedule entry has invalid daysOfWeek format: %s",
                sanitize_text(sched[FIELD_DAYSOFWEEK]),
            )
            return False

        # Validate time fields if inside or outside is enabled
        if sched.get(FIELD_INSIDE, False):
            in_start_key = FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX
            in_end_key = FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX
            if in_start_key not in sched or in_end_key not in sched:
                _LOGGER.debug("Schedule entry missing inside time fields: %s", sanitize_text(sched))
                return False
            if FIELD_HOUR not in sched[in_start_key] or FIELD_MINUTE not in sched[in_start_key]:
                _LOGGER.debug(
                    "Schedule entry has invalid inside start time: %s",
                    sanitize_text(sched[in_start_key]),
                )
                return False
            if FIELD_HOUR not in sched[in_end_key] or FIELD_MINUTE not in sched[in_end_key]:
                _LOGGER.debug(
                    "Schedule entry has invalid inside end time: %s",
                    sanitize_text(sched[in_end_key]),
                )
                return False

        if sched.get(FIELD_OUTSIDE, False):
            out_start_key = FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX
            out_end_key = FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX
            if out_start_key not in sched or out_end_key not in sched:
                _LOGGER.debug(
                    "Schedule entry missing outside time fields: %s", sanitize_text(sched)
                )
                return False
            if FIELD_HOUR not in sched[out_start_key] or FIELD_MINUTE not in sched[out_start_key]:
                _LOGGER.debug(
                    "Schedule entry has invalid outside start time: %s",
                    sanitize_text(sched[out_start_key]),
                )
                return False
            if FIELD_HOUR not in sched[out_end_key] or FIELD_MINUTE not in sched[out_end_key]:
                _LOGGER.debug(
                    "Schedule entry has invalid outside end time: %s",
                    sanitize_text(sched[out_end_key]),
                )
                return False

        return True
    except Exception as e:
        _LOGGER.error("Error validating schedule entry: %s", e, exc_info=True)
        return False


# Schedule template with all fields initialized to defaults.
#
# Wire types match docs/protocol.md "Schedule Format" (and therefore
# ``simulator.state.Schedule.to_dict``) field for field: ``index`` int,
# ``daysOfWeek`` 7 ints, ``inside``/``outside`` JSON bools, ``enabled`` the
# string "1"/"0", and ``{hour, min}`` ints. ``enabled`` was a JSON boolean
# here, which every ``compress_schedule()`` result inherited (M1).
schedule_template = {
    FIELD_INDEX: 0,
    FIELD_DAYSOFWEEK: [0, 0, 0, 0, 0, 0, 0],
    FIELD_INSIDE: False,
    FIELD_OUTSIDE: False,
    FIELD_ENABLED: "1",
    FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX: {FIELD_HOUR: 0, FIELD_MINUTE: 0},
    FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX: {FIELD_HOUR: 0, FIELD_MINUTE: 0},
    FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX: {FIELD_HOUR: 0, FIELD_MINUTE: 0},
    FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX: {FIELD_HOUR: 0, FIELD_MINUTE: 0},
}


def _require_complete_entry(sched: dict, position: int) -> None:
    """Validate that a schedule entry is fully populated for compression (T7).

    compress_schedule() reads every field of every entry, so each entry
    must carry a 7-element daysOfWeek list, the inside/outside flags, and
    all four time sub-dicts with hour/min keys. Start from
    ``schedule_template`` (deep-copied) to guarantee completeness.

    Raises:
        ValueError: With a clear message identifying the offending entry.
    """
    if not isinstance(sched, dict):
        raise ValueError(f"Schedule entry {position} is not a dict: {sched!r}")

    days = sched.get(FIELD_DAYSOFWEEK)
    if not isinstance(days, list) or len(days) != 7:
        raise ValueError(
            f"Schedule entry {position} needs a 7-element {FIELD_DAYSOFWEEK!r} list: {sched!r}"
        )

    for flag in (FIELD_INSIDE, FIELD_OUTSIDE):
        if flag not in sched:
            raise ValueError(f"Schedule entry {position} is missing {flag!r}: {sched!r}")

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
                f"Schedule entry {position} is missing time field {key!r} "
                f"(expected a dict with {FIELD_HOUR!r}/{FIELD_MINUTE!r}): {sched!r}"
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
        ValueError: If an entry is missing required fields (T7).
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
            # `enabled`) would otherwise expand to every day of the week (L4).
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
                        ent[FIELD_DAYSOFWEEK][day] = 1
                        found = True
                        break
                if not found:
                    ent = {
                        "start": sched["start"],
                        "end": sched["end"],
                        FIELD_DAYSOFWEEK: [0, 0, 0, 0, 0, 0, 0],
                    }
                    ent[FIELD_DAYSOFWEEK][day] = 1
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

    # Step 5, make template rows
    out = []
    index = 0
    for sched in final_sched:
        ent = deepcopy(schedule_template)
        ent[FIELD_INDEX] = index
        ent[FIELD_DAYSOFWEEK] = sched[FIELD_DAYSOFWEEK]
        if sched[FIELD_INSIDE]:
            ent[FIELD_INSIDE] = True
            ent[FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_HOUR] = sched["start"].hour
            ent[FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_MINUTE] = sched["start"].minute
            ent[FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_HOUR] = sched["end"].hour
            ent[FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_MINUTE] = sched["end"].minute
        if sched[FIELD_OUTSIDE]:
            ent[FIELD_OUTSIDE] = True
            ent[FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_HOUR] = sched["start"].hour
            ent[FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_MINUTE] = sched[
                "start"
            ].minute
            ent[FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_HOUR] = sched["end"].hour
            ent[FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_MINUTE] = sched["end"].minute
        out.append(ent)
        index += 1

    return out


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
    sync against a single-connection, rate-limited device (L1).

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
    deep copies of the new entries with their ``index`` field reassigned
    (L13).

    Args:
        current_schedule: List of current schedule entries on device
        new_schedule: List of desired schedule entries

    Returns:
        Tuple of (entries_to_delete, entries_to_set) where:
        - entries_to_delete: list of indices to delete from device
        - entries_to_set: list of schedule entries (copies) to add/update
          via SET_SCHEDULE
    """
    # Build lookup of current entries by content key
    current_by_content = {}
    for entry in current_schedule:
        key = schedule_entry_content_key(entry)
        current_by_content[key] = entry

    # Build set of indices currently in use. Entries are raw protocol dicts
    # (untyped); device entries always carry an integer index, so cast tells
    # the type checker what .get() returns without changing runtime behavior.
    current_indices = {cast(int, entry.get(FIELD_INDEX)) for entry in current_schedule}

    # Find entries that already exist (no change needed) and track which new entries need to be set
    entries_to_set = []
    matched_indices: set[int] = set()

    for entry in new_schedule:
        key = schedule_entry_content_key(entry)
        if key in current_by_content:
            # This content already exists - no change needed
            matched_indices.add(cast(int, current_by_content[key].get(FIELD_INDEX)))
        else:
            # This is a new/changed entry that needs to be SET. Copy it so
            # the index reassignment below never mutates the caller's
            # input (L13).
            entries_to_set.append(deepcopy(entry))

    # Indices that can be reused (current indices that weren't matched)
    reusable_indices = sorted(current_indices - matched_indices)

    # Indices to delete (reusable indices we won't use because we have fewer new entries)
    entries_to_delete = []

    # Assign indices to entries that need to be SET
    for i, entry in enumerate(entries_to_set):
        if i < len(reusable_indices):
            # Reuse an existing index (this is an UPDATE)
            entry[FIELD_INDEX] = reusable_indices[i]
        else:
            # Need a new index - find the lowest unused index
            new_index = 0
            used_indices = (
                matched_indices
                | set(reusable_indices[:i])
                | {e.get(FIELD_INDEX) for e in entries_to_set[:i]}
            )
            while new_index in used_indices or new_index in current_indices:
                new_index += 1
            entry[FIELD_INDEX] = new_index

    # Delete any leftover reusable indices we didn't use
    if len(entries_to_set) < len(reusable_indices):
        entries_to_delete = reusable_indices[len(entries_to_set) :]

    return (entries_to_delete, entries_to_set)
