# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Bounded coercion of values that arrive from a file or a script.

The YAML script channel and the JSON state-document channel are two ways
into the same fields, and both are outside the wire path's own hardening.
``set hold_time inf`` from a script stored a value that broke
``GET_SETTINGS`` for every client for the life of the process and parked
the door in ``DOOR_HOLDING``; a state document saying the same thing would
do the same damage. One implementation, so the two channels cannot drift
on what a field will accept.

Callers wrap :class:`CoercionError` in whatever their own surface raises -
``ScriptError`` for a script step, ``StateDocumentError`` for a document -
so the message reaches the operator labelled with the thing they were
actually doing.
"""

from __future__ import annotations

import math

from ..i18n import t
from ..sanitize import sanitize_text


class CoercionError(ValueError):
    """A supplied value is not usable for the field it was given for."""


def coerce_number(value: object, name: str, minimum: float, maximum: float) -> float:
    """Coerce ``value`` to a finite float within ``[minimum, maximum]``.

    Args:
        value: The value as it arrived, of any type.
        name: Field name, for the error message.
        minimum: Smallest accepted value, inclusive.
        maximum: Largest accepted value, inclusive.

    Raises:
        CoercionError: If the value is not a finite number in range.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise CoercionError(
            t(
                "simulator.scripting.must_number_got",
                "{name} must be a number, got {arg0!r}",
                name=name,
                arg0=sanitize_text(value),
            )
        ) from None
    if not math.isfinite(number):
        raise CoercionError(
            t(
                "simulator.scripting.must_finite_number_got",
                "{name} must be a finite number, got {number!r}",
                name=name,
                number=number,
            )
        )
    if not minimum <= number <= maximum:
        raise CoercionError(
            t(
                "simulator.scripting.must_between_got",
                "{name} must be between {minimum} and {maximum}, got {number!r}",
                name=name,
                minimum=minimum,
                maximum=maximum,
                number=number,
            )
        )
    return number


#: Every spelling an authored boolean may take. Kept explicit so a typo
#: cannot fall through to a fail-closed "false" - the wire parser's
#: leniency is right for the wire and wrong here.
_TRUE_WORDS = frozenset({"on", "true", "yes", "1", "enabled", "enable", "active"})
_FALSE_WORDS = frozenset({"off", "false", "no", "0", "disabled", "disable", "clear"})


def coerce_bool(value: object, name: str = "boolean") -> bool:
    """Coerce a file-supplied boolean, **raising** on anything unrecognized.

    Deliberately stricter than
    :func:`~powerpetdoor.schedule.coerce_schedule_flag`, which fails closed.
    That one reads values off the **wire**, where an unreadable flag must
    never *grant* access and a schedule the device already stores must not
    become unloadable because one flag has a novel spelling.

    This one reads values a human **wrote** - a script step, a state
    document, a command argument - and there the same leniency is a bug:
    ``set power maybe`` silently turned the power *off* and reported
    success, which is the exact silent-misspelling failure every other
    name in this DSL is checked against.

    Numbers are accepted only as ``0``/``1``. Truthiness is not the
    question being asked; ``enabled: 2`` is a mistake, not a yes.

    Raises:
        CoercionError: If the value is not a recognized boolean spelling.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value)
        raise CoercionError(
            t(
                "simulator.coerce.number_not_boolean",
                "{name} must be true or false, got the number {arg0}",
                name=name,
                arg0=value,
            )
        )
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise CoercionError(
        t(
            "simulator.coerce.not_boolean",
            "{name} must be true or false, got {arg0!r}",
            name=name,
            arg0=sanitize_text(value),
        )
    )


#: The word that means "flip whatever it is now", shared by `inside`,
#: `outside` and `obstruction` - and by the `toggle`/`t` subcommand the
#: boolean settings already have.
TOGGLE_WORD = "toggle"


def coerce_presence(value: object) -> tuple[bool | None, float | None]:
    """Read the argument `inside`, `outside` and `obstruction` all share.

    Returns ``(present, duration)``: exactly one is not None.

    - ``on``/``off`` (and every other spelling :func:`coerce_bool` takes)
      set it explicitly.
    - ``toggle`` flips it, and is ``None`` for both, meaning "decide from
      the current state".
    - a number is a duration in seconds.

    Three commands took three shapes for one idea. A script is read out of
    order, where a bare toggle is only unambiguous with the whole run in
    view, so the explicit words matter more there than at a prompt.

    Raises:
        CoercionError: If the value is neither a number nor a recognized
            on/off/toggle word.
    """
    text = str(value).strip().lower()
    if text == TOGGLE_WORD:
        return None, None
    try:
        return None, float(text)
    except ValueError:
        pass
    # **Strict**, unlike `coerce_bool`. That one fails closed because an
    # unreadable flag arriving from the wire must never *grant* access. An
    # argument someone typed is the opposite case: `inside nonsense` read
    # as "off" silently turns the sensor off, and every other misspelling
    # in this project fails loudly.
    if text in _TRUE_WORDS:
        return True, None
    if text in _FALSE_WORDS:
        return False, None
    raise CoercionError(
        t(
            "simulator.coerce.expected_on_off_toggle_or_seconds",
            "expected on, off, {toggle} or a number of seconds, got {arg0!r}",
            toggle=TOGGLE_WORD,
            arg0=sanitize_text(value),
        )
    )
