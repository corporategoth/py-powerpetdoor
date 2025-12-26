# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Schedule utility functions for Power Pet Door.

This module provides pure utility functions for working with Power Pet Door
schedules, including validation, compression, and diffing.
"""

from __future__ import annotations

from datetime import time
from copy import deepcopy
import logging

from .const import (
    FIELD_INDEX,
    FIELD_DAYSOFWEEK,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_ENABLED,
    FIELD_INSIDE_PREFIX,
    FIELD_OUTSIDE_PREFIX,
    FIELD_START_TIME_SUFFIX,
    FIELD_END_TIME_SUFFIX,
    FIELD_HOUR,
    FIELD_MINUTE,
)

_LOGGER = logging.getLogger(__name__)


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
            _LOGGER.debug("Schedule entry missing index field: %s", sched)
            return False

        if FIELD_DAYSOFWEEK not in sched:
            _LOGGER.debug("Schedule entry missing daysOfWeek field: %s", sched)
            return False

        # Validate daysOfWeek is a list of 7 elements
        if not isinstance(sched[FIELD_DAYSOFWEEK], list) or len(sched[FIELD_DAYSOFWEEK]) != 7:
            _LOGGER.debug("Schedule entry has invalid daysOfWeek format: %s", sched[FIELD_DAYSOFWEEK])
            return False

        # Validate time fields if inside or outside is enabled
        if sched.get(FIELD_INSIDE, False):
            in_start_key = FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX
            in_end_key = FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX
            if in_start_key not in sched or in_end_key not in sched:
                _LOGGER.debug("Schedule entry missing inside time fields: %s", sched)
                return False
            if FIELD_HOUR not in sched[in_start_key] or FIELD_MINUTE not in sched[in_start_key]:
                _LOGGER.debug("Schedule entry has invalid inside start time: %s", sched[in_start_key])
                return False
            if FIELD_HOUR not in sched[in_end_key] or FIELD_MINUTE not in sched[in_end_key]:
                _LOGGER.debug("Schedule entry has invalid inside end time: %s", sched[in_end_key])
                return False

        if sched.get(FIELD_OUTSIDE, False):
            out_start_key = FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX
            out_end_key = FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX
            if out_start_key not in sched or out_end_key not in sched:
                _LOGGER.debug("Schedule entry missing outside time fields: %s", sched)
                return False
            if FIELD_HOUR not in sched[out_start_key] or FIELD_MINUTE not in sched[out_start_key]:
                _LOGGER.debug("Schedule entry has invalid outside start time: %s", sched[out_start_key])
                return False
            if FIELD_HOUR not in sched[out_end_key] or FIELD_MINUTE not in sched[out_end_key]:
                _LOGGER.debug("Schedule entry has invalid outside end time: %s", sched[out_end_key])
                return False

        return True
    except Exception as e:
        _LOGGER.error("Error validating schedule entry: %s", e, exc_info=True)
        return False


# Schedule template with all fields initialized to defaults
schedule_template = {
    FIELD_INDEX: 0,
    FIELD_DAYSOFWEEK: [0, 0, 0, 0, 0, 0, 0],
    FIELD_INSIDE: False,
    FIELD_OUTSIDE: False,
    FIELD_ENABLED: True,
    FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX: {FIELD_HOUR: 0, FIELD_MINUTE: 0},
    FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX: {FIELD_HOUR: 0, FIELD_MINUTE: 0},
    FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX: {FIELD_HOUR: 0, FIELD_MINUTE: 0},
    FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX: {FIELD_HOUR: 0, FIELD_MINUTE: 0},
}


def compress_schedule(schedule: list[dict]) -> list[dict]:
    """Compress a schedule to minimize the number of entries.

    Takes a list of schedule entries and combines/merges them where possible:
    - Overlapping time periods on the same day are merged
    - Same time periods on different days are combined
    - Inside and outside entries with matching times/days are combined

    Args:
        schedule: List of schedule entry dictionaries

    Returns:
        Compressed list of schedule entries with sequential indices
    """
    expanded_sched = {
        FIELD_INSIDE: {},
        FIELD_OUTSIDE: {},
    }

    # Step 1 .. expand
    for sched in schedule:
        in_start = time(sched[FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_HOUR],
                        sched[FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_MINUTE])
        in_end = time(sched[FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_HOUR],
                      sched[FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_MINUTE])
        if in_end < in_start:
            in_start, in_end = in_end, in_start
        out_start = time(sched[FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_HOUR],
                         sched[FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_MINUTE])
        out_end = time(sched[FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_HOUR],
                       sched[FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_MINUTE])
        if out_end < out_start:
            out_start, out_end = out_end, out_start

        for day in range(len(sched[FIELD_DAYSOFWEEK])):
            if sched[FIELD_DAYSOFWEEK][day]:
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

            i=0
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
        out = []
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
                        FIELD_DAYSOFWEEK: [0, 0, 0, 0, 0, 0, 0]
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
            if (ent["start"] == sched["start"] and
                    ent["end"] == sched["end"] and
                    ent[FIELD_DAYSOFWEEK] == sched[FIELD_DAYSOFWEEK]):
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
            ent[FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX][FIELD_MINUTE] = sched["start"].minute
            ent[FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_HOUR] = sched["end"].hour
            ent[FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX][FIELD_MINUTE] = sched["end"].minute
        out.append(ent)
        index += 1

    return out


def schedule_entry_content_key(entry: dict) -> tuple:
    """Create a hashable key representing schedule entry content (ignoring index).

    This allows comparing entries by their actual content rather than their index,
    which is important for incremental sync since compression may reassign indices.

    Args:
        entry: Schedule entry dictionary

    Returns:
        Tuple that can be used as a dict key for comparison
    """
    # Extract time values
    in_start = (entry.get(FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX, {}).get(FIELD_HOUR, 0),
                entry.get(FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX, {}).get(FIELD_MINUTE, 0))
    in_end = (entry.get(FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX, {}).get(FIELD_HOUR, 0),
              entry.get(FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX, {}).get(FIELD_MINUTE, 0))
    out_start = (entry.get(FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX, {}).get(FIELD_HOUR, 0),
                 entry.get(FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX, {}).get(FIELD_MINUTE, 0))
    out_end = (entry.get(FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX, {}).get(FIELD_HOUR, 0),
               entry.get(FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX, {}).get(FIELD_MINUTE, 0))

    return (
        tuple(entry.get(FIELD_DAYSOFWEEK, [0] * 7)),
        entry.get(FIELD_INSIDE, False),
        entry.get(FIELD_OUTSIDE, False),
        entry.get(FIELD_ENABLED, True),
        in_start, in_end,
        out_start, out_end,
    )


def compute_schedule_diff(current_schedule: list[dict], new_schedule: list[dict]) -> tuple[list[int], list[dict]]:
    """Compare current and new schedules to determine what needs to change.

    Args:
        current_schedule: List of current schedule entries on device
        new_schedule: List of desired schedule entries

    Returns:
        Tuple of (entries_to_delete, entries_to_add) where:
        - entries_to_delete: list of indices to delete from device
        - entries_to_add: list of new schedule entries to add
    """
    # Build lookup of current entries by content key
    current_by_content = {}
    for entry in current_schedule:
        key = schedule_entry_content_key(entry)
        current_by_content[key] = entry

    # Build lookup of new entries by content key
    new_by_content = {}
    for entry in new_schedule:
        key = schedule_entry_content_key(entry)
        new_by_content[key] = entry

    # Find entries to delete (in current but not in new)
    entries_to_delete = []
    for key, entry in current_by_content.items():
        if key not in new_by_content:
            entries_to_delete.append(entry[FIELD_INDEX])

    # Find entries to add (in new but not in current)
    entries_to_add = []
    for key, entry in new_by_content.items():
        if key not in current_by_content:
            entries_to_add.append(entry)

    return (entries_to_delete, entries_to_add)
