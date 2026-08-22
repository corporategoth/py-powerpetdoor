# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Hypothesis property tests for the untrusted-input validation layer.

Round 3 added an entire validation layer for hostile wire input - the
``_coerce_wire_*`` family, ``WireValueError``, and two rewritten schedule
parsers - and the property suite did not grow with it (R4-L5). These are
the totality properties that layer is *for*: every entry point must be
total over arbitrary JSON-shaped input, raising only its own declared
exception type.

That is not theoretical. The round-3 wave itself fixed a totality bug of
exactly this class (``int(float("inf"))`` raising ``OverflowError`` out of
a validator), and the library-side ``Schedule.from_dict`` had eight
distinct crash shapes that a totality property found in under a minute.

Example counts are bounded to keep the fuzz suite fast.
"""

from __future__ import annotations

import contextlib

from hypothesis import given, settings
from hypothesis import strategies as st

from powerpetdoor.door import Schedule as LibrarySchedule
from powerpetdoor.sanitize import _CONTROL_CHAR_RE, sanitize_text
from powerpetdoor.schedule import (
    MAX_SCHEDULE_INDEX,
    coerce_schedule_day,
    coerce_schedule_days,
    coerce_schedule_flag,
    coerce_schedule_int,
    coerce_schedule_time,
)
from powerpetdoor.simulator.protocol import (
    WireValueError,
    _coerce_wire_flag,
    _coerce_wire_int,
    _coerce_wire_number,
    _coerce_wire_string,
)
from powerpetdoor.simulator.state import Schedule as SimulatorSchedule

# Anything json.loads can produce, including the containers that are not
# usable dict keys and the non-finite floats JSON permits.
_json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**9), max_value=10**9),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(max_size=8),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=8), children, max_size=4),
    ),
    max_leaves=8,
)

_time_payloads = st.one_of(
    _json_values,
    st.fixed_dictionaries({"hour": _json_values, "min": _json_values}),
)

# Protocol-shaped schedule payloads: realistic keys, arbitrary values.
_schedule_payloads = st.one_of(
    _json_values,
    st.fixed_dictionaries(
        {},
        optional={
            "index": _json_values,
            "enabled": _json_values,
            "daysOfWeek": st.one_of(_json_values, st.lists(_json_values, max_size=8)),
            "inside": _json_values,
            "outside": _json_values,
            "in_start_time": _time_payloads,
            "in_end_time": _time_payloads,
            "out_start_time": _time_payloads,
            "out_end_time": _time_payloads,
        },
    ),
)


class TestWireCoercerTotality:
    """Every ``_coerce_wire_*`` raises WireValueError or nothing at all."""

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_number_coercer_only_raises_wire_value_error(self, value):
        with contextlib.suppress(WireValueError):
            result = _coerce_wire_number(value, "field", 0, 90000)
            assert isinstance(result, float)

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_int_coercer_only_raises_wire_value_error(self, value):
        with contextlib.suppress(WireValueError):
            result = _coerce_wire_int(value, "field", 0, MAX_SCHEDULE_INDEX)
            assert isinstance(result, int)
            assert 0 <= result <= MAX_SCHEDULE_INDEX

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_string_coercer_only_raises_wire_value_error(self, value):
        with contextlib.suppress(WireValueError):
            result = _coerce_wire_string(value, "field", 128)
            assert isinstance(result, str)
            assert len(result) <= 128

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_flag_coercer_only_raises_wire_value_error(self, value):
        with contextlib.suppress(WireValueError):
            assert _coerce_wire_flag(value, "field") in (True, False)


class TestScheduleCoercerTotality:
    """The shared schedule helpers raise ValueError or nothing at all."""

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_int_coercer_is_total(self, value):
        with contextlib.suppress(ValueError):
            result = coerce_schedule_int(value, "index", MAX_SCHEDULE_INDEX)
            assert 0 <= result <= MAX_SCHEDULE_INDEX

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_day_coercer_is_total(self, value):
        with contextlib.suppress(ValueError):
            assert coerce_schedule_day(value, 0) in (True, False)

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_days_coercer_is_total(self, value):
        with contextlib.suppress(ValueError):
            days = coerce_schedule_days(value)
            assert len(days) == 7
            assert all(isinstance(day, bool) for day in days)

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_flag_coercer_never_raises_at_all(self, value):
        """Flags fail closed rather than raising - and never grant access."""
        assert coerce_schedule_flag(value, "inside") in (True, False)

    @settings(max_examples=200, deadline=None)
    @given(value=_time_payloads)
    def test_time_coercer_is_total(self, value):
        with contextlib.suppress(ValueError):
            hour, minute = coerce_schedule_time(value, "start time")
            assert 0 <= hour <= 23
            assert 0 <= minute <= 59


class TestScheduleParserTotality:
    """Both schedule parsers raise ValueError or nothing at all.

    The library's parser is the one that reads real device bytes, and it
    used to escape with TypeError/AttributeError in eight distinct shapes
    (R4-M1). Testing both sides here is the point: the twin that was not
    changed is exactly where the last three rounds' fixes went missing.
    """

    @settings(max_examples=300, deadline=None)
    @given(payload=_schedule_payloads)
    def test_library_parser_only_raises_value_error(self, payload):
        with contextlib.suppress(ValueError):
            schedule = LibrarySchedule.from_dict(payload)
            assert len(schedule.days_of_week) == 7
            assert isinstance(schedule.enabled, bool)
            assert 0 <= schedule.index <= MAX_SCHEDULE_INDEX

    @settings(max_examples=300, deadline=None)
    @given(payload=_schedule_payloads)
    def test_simulator_parser_only_raises_value_error(self, payload):
        with contextlib.suppress(ValueError):
            schedule = SimulatorSchedule.from_dict(payload)
            assert len(schedule.days_of_week) == 7
            assert isinstance(schedule.enabled, bool)
            assert 0 <= schedule.index <= MAX_SCHEDULE_INDEX

    @settings(max_examples=200, deadline=None)
    @given(payload=_schedule_payloads)
    def test_a_parsed_schedule_is_always_safely_evaluable(self, payload):
        """Whatever parses must survive the sensor evaluation that follows."""
        with contextlib.suppress(ValueError):
            schedule = SimulatorSchedule.from_dict(payload)
            for weekday in range(7):
                assert schedule.is_day_active(weekday) in (True, False)
                assert schedule.is_sensor_allowed("inside", 12, 30, weekday) in (True, False)


class TestSanitizeProperties:
    """sanitize_text is total, idempotent, and leaks no control character."""

    @settings(max_examples=300, deadline=None)
    @given(text=st.text())
    def test_no_control_character_survives_and_it_is_idempotent(self, text):
        out = sanitize_text(text)
        assert _CONTROL_CHAR_RE.search(out) is None
        assert sanitize_text(out) == out

    @settings(max_examples=200, deadline=None)
    @given(value=_json_values)
    def test_any_value_can_be_sanitized(self, value):
        """Log call sites hand it whatever came off the wire, not just str."""
        out = sanitize_text(value)
        assert isinstance(out, str)
        assert _CONTROL_CHAR_RE.search(out) is None
