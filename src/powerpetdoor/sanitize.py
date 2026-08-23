# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Terminal-safety helper shared by the library and the simulator.

Everything this project receives from a network peer is untrusted, and C0
control characters (ESC above all) are valid ASCII: they survive decoding
and land verbatim in whatever consumes them. A log record read with
``tail -f``/``journalctl``/``docker logs``, or a value echoed by the
interactive CLI, is therefore an ANSI-injection sink.

:func:`sanitize_text` is the implementation used by every such sink, and
:func:`sanitize_field` is its stricter sibling for a sink that renders one
device-supplied *field value*. They live in the library package - not in the
simulator's front-end stack - so ``client.py``, ``schedule.py`` and
``tz_utils.py`` can use them without importing simulator or
``prompt_toolkit`` code.
"""

import re

#: C0 control characters (except tab/newline), DEL, C1 control characters,
#: and the surrogate range.
#:
#: Surrogates are here because they are the one class that makes the
#: *result* unusable rather than merely ugly. ``"\ud800"`` is legal JSON,
#: arrives on the wire as six ASCII characters, survives
#: ``decode("ascii", errors="backslashreplace")`` unchanged, and becomes an
#: unpaired surrogate at ``json.loads``. An unpaired surrogate cannot be
#: encoded to UTF-8, so a "sanitized" string containing one is exactly what a
#: ``logging.FileHandler(encoding="utf-8")`` cannot write: 200 such frames
#: produced 0 log lines and 359 KB of logging-internal tracebacks on stderr.
#: Escaping them keeps the record.
#:
#: LF is deliberately **not** here: this is also applied to whole formatted
#: log records, and a multi-line traceback is legitimate there.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")

#: :data:`_CONTROL_CHAR_RE` plus LF, for :func:`sanitize_field`.
_FIELD_CHAR_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\ud800-\udfff]")

#: Appended by :func:`sanitize_text` when ``limit`` cut the value short.
_TRUNCATION_MARKER = "...(truncated)"

#: Default cap for echoing a whole peer-chosen frame into a log record.
#: The frame is attacker-chosen and may be anything up to the 64 KiB
#: framing cap, so the constant of any per-frame log line has to be
#: bounded independently of how often the line fires.
MAX_LOGGED_LENGTH = 200


def _escape(match: "re.Match[str]") -> str:
    """Render one matched code point as an unambiguous escape.

    The width matters: ``f"\\\\x{ord(c):02x}"`` renders ``U+D800`` as
    ``\\xd800``, which reads as ``\\xd8`` followed by ``00``. Anything above
    ``U+00FF`` gets the four-digit ``\\uNNNN`` spelling instead.
    """
    codepoint = ord(match.group())
    if codepoint <= 0xFF:
        return f"\\x{codepoint:02x}"
    return f"\\u{codepoint:04x}"


def sanitize_text(text: object, limit: int | None = None) -> str:
    """Neutralize terminal control characters in untrusted text.

    Accepts any value (network-derived fields are not guaranteed to be
    strings) and replaces C0 controls (except tab and newline), DEL, C1
    controls and unpaired surrogates with a visible ``\\xNN``/``\\uNNNN``
    escape, so the result is safe to write to a log record or a terminal
    **and** can always be encoded to UTF-8.

    Args:
        text: The untrusted value; stringified if it is not already a string.
        limit: Maximum number of characters to keep. Longer values are cut
            and marked as truncated. Truncation happens *before* escaping,
            so an attacker-chosen 64 KiB frame costs a bounded amount of
            both log volume and regex work.

    Returns:
        The value with every control character and surrogate replaced by its
        escape.
    """
    return _sanitize(_CONTROL_CHAR_RE, text, limit)


def sanitize_field(text: object, limit: int | None = None) -> str:
    """:func:`sanitize_text`, and LF as well, for one field value.

    Use this wherever a *single* device-supplied value is interpolated into
    a log record. A field value is single-valued by definition, so a newline
    in one is never legitimate: it ends the physical line, and a log reader
    takes everything after it as a fresh record - with a timestamp, a
    severity and a message the device chose. ``sanitize_text`` cannot do
    this itself, because it is also applied to whole formatted records,
    where a multi-line traceback is exactly what should be written.

    Args:
        text: The untrusted value; stringified if it is not already a string.
        limit: Maximum number of characters to keep, as for
            :func:`sanitize_text`.

    Returns:
        The value with every control character (including LF), DEL, C1
        control and surrogate replaced by its escape.
    """
    return _sanitize(_FIELD_CHAR_RE, text, limit)


def _sanitize(pattern: "re.Pattern[str]", text: object, limit: int | None) -> str:
    """Truncate then escape, shared by the two public entry points."""
    value = str(text)
    if limit is not None and len(value) > limit:
        value = value[:limit] + _TRUNCATION_MARKER
    return pattern.sub(_escape, value)
