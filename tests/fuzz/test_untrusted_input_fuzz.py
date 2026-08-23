# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Hypothesis property tests for the untrusted-input validation layer.

The layer under test is the validation layer for hostile wire input: the
``_coerce_wire_*`` family, ``WireValueError``, and the two schedule
parsers. These are the totality properties that layer is *for*: every
entry point must be total over arbitrary JSON-shaped input, raising only
its own declared exception type.

That is not theoretical. A totality bug of exactly this class has shipped
here (``int(float("inf"))`` raising ``OverflowError`` out of a validator),
and the library-side ``Schedule.from_dict`` had eight distinct crash
shapes that a totality property found in under a minute.

**A property is only worth its runtime if it draws the values it exists
for.** ``st.recursive`` with ``max_leaves=8`` spends its budget on
containers, so the measured draw rate for a top-level non-finite float was
0/600 and for a parseable 7-element ``daysOfWeek`` 18/600 - i.e. removing
``OverflowError`` from ``coerce_schedule_int``'s except clause would slip
through the whole fuzz suite, and so would ``coerce_schedule_day``
returning ``int(flag)``. Two strategies fix that:

- ``_pathological`` is mixed into every scalar coercer's input, so the
  non-finite/overflowing values these validators were written for are
  drawn on a fixed fraction of examples rather than by luck.
- ``_well_shaped_*`` feed the *success* path, so post-conditions that only
  run after a successful parse actually execute.

Example counts are bounded to keep the fuzz suite fast.
"""

from __future__ import annotations

import contextlib

from hypothesis import given, settings
from hypothesis import strategies as st

from powerpetdoor.door import Schedule as LibrarySchedule
from powerpetdoor.sanitize import sanitize_text
from powerpetdoor.schedule import (
    MAX_DAYS_BITMASK,
    MAX_SCHEDULE_INDEX,
    coerce_schedule_day,
    coerce_schedule_days,
    coerce_schedule_flag,
    coerce_schedule_int,
    coerce_schedule_time,
)
from powerpetdoor.simulator.protocol import (
    WireValueError,
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

# The values these validators exist to reject, drawn deliberately rather
# than hoped for. `1e400` is what `json.loads("1e400")` produces (inf);
# `int()` of any of the three floats raises OverflowError or ValueError,
# which is the totality hole these validators exist to close.
_pathological = st.sampled_from(
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        1e400,
        -(2**64),
        2**64,
        -1,
        MAX_SCHEDULE_INDEX + 1,
    ]
)

#: Hostile input with the pathological scalars mixed in explicitly.
_scalar_values = st.one_of(_json_values, _pathological)

#: The protocol's actual daysOfWeek shape, in every spelling the coercers
#: accept - so the success-path post-conditions genuinely run.
_well_shaped_days = st.lists(st.sampled_from([0, 1, "0", "1", True, False]), min_size=7, max_size=7)

#: A legal legacy bitmask.
_well_shaped_bitmask = st.integers(min_value=0, max_value=MAX_DAYS_BITMASK)

#: In-range values for the numeric wire coercers, plus the string
#: spellings a device might send them as. `_scalar_values` alone reached
#: the success-path post-conditions in 6-11 of 600 draws, so the "and the
#: result is in range" half of those properties barely ran.
_well_shaped_wire_number = st.one_of(
    st.integers(min_value=0, max_value=90000),
    st.floats(min_value=0, max_value=90000, allow_nan=False, allow_infinity=False),
)
_well_shaped_wire_index = st.integers(min_value=0, max_value=MAX_SCHEDULE_INDEX)
#: Strings a device plausibly sends, including over-long ones.
_well_shaped_wire_string = st.text(max_size=200)
#: Every spelling `make_bool` accepts, in both cases.
_well_shaped_wire_flag = st.sampled_from(
    [0, 1, True, False, "0", "1", "true", "false", "TRUE", "False", "yes", "no", "on", "off"]
)

#: The protocol's actual {hour, min} shape.
_well_shaped_time = st.fixed_dictionaries(
    {"hour": st.integers(min_value=0, max_value=23), "min": st.integers(min_value=0, max_value=59)}
)

_time_payloads = st.one_of(
    _json_values,
    _well_shaped_time,
    st.fixed_dictionaries({"hour": _scalar_values, "min": _scalar_values}),
)

# Protocol-shaped schedule payloads: realistic keys, arbitrary values.
_schedule_payloads = st.one_of(
    _json_values,
    st.fixed_dictionaries(
        {},
        optional={
            "index": st.one_of(_scalar_values, st.integers(0, MAX_SCHEDULE_INDEX)),
            "enabled": st.one_of(_scalar_values, st.sampled_from([0, 1, "0", "1"])),
            "daysOfWeek": st.one_of(
                _json_values,
                st.lists(_json_values, max_size=8),
                _well_shaped_days,
                _well_shaped_bitmask,
            ),
            "inside": st.one_of(_scalar_values, st.booleans()),
            "outside": st.one_of(_scalar_values, st.booleans()),
            "in_start_time": _time_payloads,
            "in_end_time": _time_payloads,
            "out_start_time": _time_payloads,
            "out_end_time": _time_payloads,
        },
    ),
)

#: Every codepoint :func:`sanitize_text` promises to neutralize, spelled
#: out independently of the module's own regex. Asserting the output
#: against ``_CONTROL_CHAR_RE`` would make the property unable to fail:
#: narrowing the production regex to ``[\x00]`` narrows the *check* in
#: lockstep.
_CONTROL_CODEPOINTS = frozenset([*range(0x00, 0x09), 0x0B, *range(0x0C, 0x20), *range(0x7F, 0xA0)])


class TestWireCoercerTotality:
    """Every ``_coerce_wire_*`` raises WireValueError or nothing at all."""

    @settings(max_examples=200, deadline=None)
    @given(value=st.one_of(_scalar_values, _well_shaped_wire_number))
    def test_number_coercer_only_raises_wire_value_error(self, value):
        with contextlib.suppress(WireValueError):
            result = _coerce_wire_number(value, "field", 0, 90000)
            assert isinstance(result, float)
            assert 0 <= result <= 90000

    @settings(max_examples=200, deadline=None)
    @given(value=st.one_of(_scalar_values, _well_shaped_wire_index))
    def test_int_coercer_only_raises_wire_value_error(self, value):
        with contextlib.suppress(WireValueError):
            result = _coerce_wire_int(value, "field", 0, MAX_SCHEDULE_INDEX)
            assert isinstance(result, int)
            assert 0 <= result <= MAX_SCHEDULE_INDEX

    @settings(max_examples=200, deadline=None)
    @given(value=st.one_of(_scalar_values, _well_shaped_wire_string))
    def test_string_coercer_only_raises_wire_value_error(self, value):
        with contextlib.suppress(WireValueError):
            result = _coerce_wire_string(value, "field", 128)
            assert isinstance(result, str)
            assert len(result) <= 128


class TestScheduleCoercerTotality:
    """The shared schedule helpers raise ValueError or nothing at all."""

    @settings(max_examples=200, deadline=None)
    @given(value=st.one_of(_scalar_values, _well_shaped_wire_index))
    def test_int_coercer_is_total(self, value):
        with contextlib.suppress(ValueError):
            result = coerce_schedule_int(value, "index", MAX_SCHEDULE_INDEX)
            assert isinstance(result, int)
            assert 0 <= result <= MAX_SCHEDULE_INDEX

    @settings(max_examples=100, deadline=None)
    @given(value=st.sampled_from([float("inf"), float("-inf"), float("nan"), 1e400]))
    def test_non_finite_floats_are_rejected_cleanly(self, value):
        """``int(float("inf"))`` raises OverflowError, not ValueError.

        The totality hole the module docstring describes: removing
        ``OverflowError`` from the except tuple slips through a strategy
        that draws a top-level non-finite float 0 times in 600 examples,
        so the value is sampled explicitly here.
        """
        try:
            coerce_schedule_int(value, "index", MAX_SCHEDULE_INDEX)
        except ValueError as err:
            assert "must be a number" in str(err)
        else:  # pragma: no cover - a finite result here would be the bug
            raise AssertionError(f"{value!r} was accepted as an index")

    @settings(max_examples=200, deadline=None)
    @given(value=_scalar_values)
    def test_day_coercer_is_total(self, value):
        with contextlib.suppress(ValueError):
            assert isinstance(coerce_schedule_day(value, 0), bool)

    @settings(max_examples=200, deadline=None)
    @given(value=st.one_of(_scalar_values, _well_shaped_days, _well_shaped_bitmask))
    def test_days_coercer_is_total(self, value):
        with contextlib.suppress(ValueError):
            days = coerce_schedule_days(value)
            assert len(days) == 7
            assert all(isinstance(day, bool) for day in days)

    @settings(max_examples=200, deadline=None)
    @given(value=_well_shaped_days)
    def test_a_well_shaped_day_list_always_parses_to_seven_bools(self, value):
        """The protocol's actual shape must reach the success path.

        ``x in (True, False)`` does not pin the bool contract - ``1 in
        (True, False)`` is True in Python - and the sibling ``isinstance``
        assertion is only reached on 18/600 draws, all through the
        integer-bitmask branch.
        """
        days = coerce_schedule_days(value)

        assert len(days) == 7
        assert all(isinstance(day, bool) for day in days)
        assert days == [flag in (1, "1", True) for flag in value]

    @settings(max_examples=200, deadline=None)
    @given(value=_well_shaped_bitmask)
    def test_a_legal_bitmask_always_parses_to_seven_bools(self, value):
        days = coerce_schedule_days(value)

        assert all(isinstance(day, bool) for day in days)
        assert days == [bool((value >> i) & 1) for i in range(7)]

    @settings(max_examples=200, deadline=None)
    @given(value=_scalar_values)
    def test_flag_coercer_never_raises_at_all(self, value):
        """Flags fail closed rather than raising - and never grant access."""
        assert isinstance(coerce_schedule_flag(value, "inside"), bool)

    @settings(max_examples=200, deadline=None)
    @given(value=_time_payloads)
    def test_time_coercer_is_total(self, value):
        with contextlib.suppress(ValueError):
            hour, minute = coerce_schedule_time(value, "start time")
            assert isinstance(hour, int)
            assert isinstance(minute, int)
            assert 0 <= hour <= 23
            assert 0 <= minute <= 59

    @settings(max_examples=200, deadline=None)
    @given(value=_well_shaped_time)
    def test_a_well_shaped_time_always_parses(self, value):
        """The success path, executed on every draw rather than by luck."""
        assert coerce_schedule_time(value, "start time") == (value["hour"], value["min"])


class TestScheduleParserTotality:
    """Both schedule parsers raise ValueError or nothing at all.

    The library's parser is the one that reads real device bytes, and it
    used to escape with TypeError/AttributeError in eight distinct shapes.
    Testing both sides here is the point: the twin that is not being
    changed is exactly where a fix goes missing.
    """

    @settings(max_examples=300, deadline=None)
    @given(payload=_schedule_payloads)
    def test_library_parser_only_raises_value_error(self, payload):
        with contextlib.suppress(ValueError):
            schedule = LibrarySchedule.from_dict(payload)
            assert len(schedule.days_of_week) == 7
            assert all(isinstance(day, bool) for day in schedule.days_of_week)
            assert isinstance(schedule.enabled, bool)
            assert 0 <= schedule.index <= MAX_SCHEDULE_INDEX

    @settings(max_examples=300, deadline=None)
    @given(payload=_schedule_payloads)
    def test_simulator_parser_only_raises_value_error(self, payload):
        with contextlib.suppress(ValueError):
            schedule = SimulatorSchedule.from_dict(payload)
            assert len(schedule.days_of_week) == 7
            assert all(isinstance(day, bool) for day in schedule.days_of_week)
            assert isinstance(schedule.enabled, bool)
            assert 0 <= schedule.index <= MAX_SCHEDULE_INDEX

    @settings(max_examples=200, deadline=None)
    @given(payload=_schedule_payloads)
    def test_both_emitters_agree_on_every_field_except_the_three_flags(self, payload):
        """Whatever both parsers accept, both must re-emit identically.

        ``enabled``, ``inside`` and ``outside`` are excluded deliberately
        and asserted separately below: the two emitters are opposite
        protocol directions (the library sends, the simulator replies), and
        firmware 1.7.18 spells those three as ints on the way back where we
        send JSON booleans. Every *other* field must match on every input
        both parsers accept.
        """
        try:
            library = LibrarySchedule.from_dict(payload)
            simulator = SimulatorSchedule.from_dict(payload)
        except ValueError:
            return

        flags = ("enabled", "inside", "outside")
        emitted = library.to_dict()
        replied = simulator.to_dict()

        assert {k: v for k, v in emitted.items() if k not in flags} == {
            k: v for k, v in replied.items() if k not in flags
        }
        for name in flags:
            # Conservative in what we send: the client->device payload
            # always carries a real JSON boolean, whatever spelling arrived.
            assert emitted[name] is True or emitted[name] is False
            # The device->client reply always carries the observed int.
            assert replied[name] in (0, 1)
            assert not isinstance(replied[name], bool)
            # ...and the two still agree on the *value*, only not the spelling.
            assert bool(replied[name]) is emitted[name]
        assert all(day in (0, 1) and not isinstance(day, bool) for day in emitted["daysOfWeek"])

    @settings(max_examples=200, deadline=None)
    @given(payload=_schedule_payloads)
    def test_a_parsed_schedule_is_always_safely_evaluable(self, payload):
        """Whatever parses must survive the sensor evaluation that follows."""
        with contextlib.suppress(ValueError):
            schedule = SimulatorSchedule.from_dict(payload)
            for weekday in range(7):
                assert isinstance(schedule.is_day_active(weekday), bool)
                assert isinstance(schedule.is_sensor_allowed("inside", 12, 30, weekday), bool)


class TestSanitizeProperties:
    """sanitize_text is total, idempotent, and leaks no control character."""

    @settings(max_examples=300, deadline=None)
    @given(text=st.text())
    def test_no_control_character_survives_and_it_is_idempotent(self, text):
        """Checked against an independent definition of "control character".

        Validating the output with ``sanitize_text``'s own
        ``_CONTROL_CHAR_RE`` would make this property unable to fail:
        narrowing the production regex to ``[\\x00]`` narrows the check
        with it, so ESC/CSI/DEL pass through and the property still passes.
        """
        out = sanitize_text(text)

        assert not _CONTROL_CODEPOINTS.intersection(ord(char) for char in out)
        assert sanitize_text(out) == out

    @settings(max_examples=300, deadline=None)
    @given(
        text=st.text(
            alphabet=st.sampled_from([chr(code) for code in sorted(_CONTROL_CODEPOINTS)]),
            min_size=1,
            max_size=8,
        )
    )
    def test_every_control_character_is_replaced_by_its_escape(self, text):
        """The fail-closed direction: each one appears as a visible ``\\xNN``."""
        out = sanitize_text(text)

        assert out == "".join(f"\\x{ord(char):02x}" for char in text)

    @settings(max_examples=200, deadline=None)
    @given(value=_scalar_values)
    def test_any_value_can_be_sanitized(self, value):
        """Log call sites hand it whatever came off the wire, not just str."""
        out = sanitize_text(value)

        assert isinstance(out, str)
        assert not _CONTROL_CODEPOINTS.intersection(ord(char) for char in out)
