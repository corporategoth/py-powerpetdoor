# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Hypothesis property tests for schedule compression and diffing (H6).

The invariants pinned here are derived from the implementation contract:

- ``compress_schedule`` is idempotent, never invents or loses coverage
  (per sensor/day/minute, closed intervals, inverted windows normalized),
  and always emits fully-populated template entries with sequential
  indices.
- ``compute_schedule_diff`` returns a (deletes, sets) pair that, applied
  to the current schedule, yields exactly the new schedule's content;
  deletes reference only real indices and set indices never collide.
- The weekday converters are mutual inverses over the 0-6 domain.
- ``validate_schedule_entry`` is total: any dict input yields a bool.

Example counts are bounded to keep the fuzz suite fast.
"""

from __future__ import annotations

from copy import deepcopy

from hypothesis import given, settings
from hypothesis import strategies as st

from powerpetdoor import (
    compress_schedule,
    compute_schedule_diff,
    schedule_entry_content_key,
    schedule_template,
    validate_schedule_entry,
    week_0_mon_to_sun,
    week_0_sun_to_mon,
)
from powerpetdoor.const import (
    FIELD_DAYSOFWEEK,
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

_TIME_KEYS = (
    FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX,
    FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX,
    FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX,
    FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX,
)

_day_masks = st.lists(st.integers(min_value=0, max_value=1), min_size=7, max_size=7)
_clock_times = st.tuples(
    st.integers(min_value=0, max_value=23), st.integers(min_value=0, max_value=59)
)


@st.composite
def _entries(draw):
    """A fully-populated schedule entry, as compress_schedule requires."""
    entry = deepcopy(schedule_template)
    entry[FIELD_DAYSOFWEEK] = draw(_day_masks)
    entry[FIELD_INSIDE] = draw(st.booleans())
    entry[FIELD_OUTSIDE] = draw(st.booleans())
    for prefix in (FIELD_INSIDE_PREFIX, FIELD_OUTSIDE_PREFIX):
        for suffix in (FIELD_START_TIME_SUFFIX, FIELD_END_TIME_SUFFIX):
            hour, minute = draw(_clock_times)
            entry[prefix + suffix] = {FIELD_HOUR: hour, FIELD_MINUTE: minute}
    return entry


_schedules = st.lists(_entries(), max_size=4)


@st.composite
def _indexed_unique_schedules(draw):
    """A schedule list with unique content keys and unique indices.

    Mirrors real device state: compressed schedules never carry duplicate
    content, and every entry occupies its own index slot.
    """
    entries = draw(st.lists(_entries(), max_size=4))
    seen: set = set()
    unique = []
    for entry in entries:
        key = schedule_entry_content_key(entry)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    for i, entry in enumerate(unique):
        entry[FIELD_INDEX] = i
    return unique


def _window_minutes(entry: dict, prefix: str) -> tuple[int, int]:
    """The entry's normalized (start, end) window in minutes-of-day."""
    start = entry[prefix + FIELD_START_TIME_SUFFIX]
    end = entry[prefix + FIELD_END_TIME_SUFFIX]
    s = start[FIELD_HOUR] * 60 + start[FIELD_MINUTE]
    e = end[FIELD_HOUR] * 60 + end[FIELD_MINUTE]
    return (min(s, e), max(s, e))


def _covered(schedule: list[dict], sensor: str, prefix: str, day: int, minute: int) -> bool:
    """Whether any entry covers (sensor, day, minute); closed intervals."""
    for entry in schedule:
        if not entry[sensor] or not entry[FIELD_DAYSOFWEEK][day]:
            continue
        start, end = _window_minutes(entry, prefix)
        if start <= minute <= end:
            return True
    return False


def _critical_minutes(*schedules: list[dict]) -> list[int]:
    """All boundary minutes (+/- 1) across the given schedules.

    Interval unions are equal iff they agree at every boundary point and
    its immediate neighbors, so sampling these suffices for equality.
    """
    minutes = {0, 1439}
    for schedule in schedules:
        for entry in schedule:
            for prefix in (FIELD_INSIDE_PREFIX, FIELD_OUTSIDE_PREFIX):
                start, end = _window_minutes(entry, prefix)
                minutes.update({start - 1, start, start + 1, end - 1, end, end + 1})
    return sorted(m for m in minutes if 0 <= m <= 1439)


class TestCompressProperties:
    """compress_schedule invariants."""

    @settings(max_examples=50, deadline=None)
    @given(schedule=_schedules)
    def test_compress_is_idempotent_up_to_ordering(self, schedule):
        """Re-compressing preserves content exactly (order may differ).

        Entry order - and therefore index assignment - depends on the
        day-dictionary iteration order, so strict list equality does not
        hold across a second pass. Content equality (ignoring index) is
        the invariant the sync layer relies on: compute_schedule_diff
        keys entries by schedule_entry_content_key, never by index.
        """
        compressed = compress_schedule(schedule)
        recompressed = compress_schedule(compressed)

        assert sorted(schedule_entry_content_key(e) for e in recompressed) == sorted(
            schedule_entry_content_key(e) for e in compressed
        )

    @settings(max_examples=50, deadline=None)
    @given(schedule=_schedules)
    def test_compress_preserves_coverage(self, schedule):
        """Every (sensor, day, minute) is covered before iff after."""
        compressed = compress_schedule(schedule)
        minutes = _critical_minutes(schedule, compressed)
        for sensor, prefix in (
            (FIELD_INSIDE, FIELD_INSIDE_PREFIX),
            (FIELD_OUTSIDE, FIELD_OUTSIDE_PREFIX),
        ):
            for day in range(7):
                for minute in minutes:
                    assert _covered(schedule, sensor, prefix, day, minute) == _covered(
                        compressed, sensor, prefix, day, minute
                    ), (sensor, day, minute)

    @settings(max_examples=50, deadline=None)
    @given(schedule=_schedules)
    def test_compress_output_shape(self, schedule):
        """Output entries validate, are sequentially indexed and non-empty."""
        compressed = compress_schedule(schedule)

        assert [entry[FIELD_INDEX] for entry in compressed] == list(range(len(compressed)))
        for entry in compressed:
            assert validate_schedule_entry(entry) is True
            assert entry[FIELD_INSIDE] or entry[FIELD_OUTSIDE]
            assert sum(entry[FIELD_DAYSOFWEEK]) >= 1
            for key in _TIME_KEYS:
                assert set(entry[key]) == {FIELD_HOUR, FIELD_MINUTE}

    @settings(max_examples=50, deadline=None)
    @given(schedule=_schedules)
    def test_compress_does_not_mutate_input(self, schedule):
        """The caller's schedule list is never modified."""
        snapshot = deepcopy(schedule)
        compress_schedule(schedule)
        assert schedule == snapshot


class TestDiffProperties:
    """compute_schedule_diff invariants."""

    @settings(max_examples=75, deadline=None)
    @given(current=_indexed_unique_schedules(), new=_indexed_unique_schedules())
    def test_applying_diff_yields_target_content(self, current, new):
        """delete+set applied to current produces exactly new's content."""
        deletes, sets = compute_schedule_diff(current, new)

        result = {entry[FIELD_INDEX]: entry for entry in deepcopy(current)}
        for index in deletes:
            assert index in result  # Deletes only reference real indices
            del result[index]

        set_indices = [entry[FIELD_INDEX] for entry in sets]
        assert len(set_indices) == len(set(set_indices))  # No index set twice
        for entry in sets:
            result[entry[FIELD_INDEX]] = entry

        result_keys = sorted(schedule_entry_content_key(entry) for entry in result.values())
        target_keys = sorted(schedule_entry_content_key(entry) for entry in new)
        assert result_keys == target_keys

    @settings(max_examples=75, deadline=None)
    @given(current=_indexed_unique_schedules(), new=_indexed_unique_schedules())
    def test_diff_does_not_mutate_inputs(self, current, new):
        """Neither input list is modified (L13)."""
        current_snapshot = deepcopy(current)
        new_snapshot = deepcopy(new)

        compute_schedule_diff(current, new)

        assert current == current_snapshot
        assert new == new_snapshot


class TestWeekConversionProperties:
    """Weekday converter invariants."""

    @given(day=st.integers(min_value=0, max_value=6))
    def test_conversions_are_mutual_inverses_in_range(self, day):
        assert week_0_mon_to_sun(week_0_sun_to_mon(day)) == day
        assert week_0_sun_to_mon(week_0_mon_to_sun(day)) == day
        assert 0 <= week_0_mon_to_sun(day) <= 6
        assert 0 <= week_0_sun_to_mon(day) <= 6


class TestValidateProperties:
    """validate_schedule_entry is a total function over dicts."""

    _messy_values = st.recursive(
        st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=6)),
        lambda children: st.one_of(
            st.lists(children, max_size=7),
            st.dictionaries(st.text(max_size=8), children, max_size=4),
        ),
        max_leaves=10,
    )

    @settings(max_examples=100, deadline=None)
    @given(entry=st.dictionaries(st.text(max_size=12), _messy_values, max_size=6))
    def test_validate_never_raises_and_returns_bool(self, entry):
        assert validate_schedule_entry(entry) in (True, False)
