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

:func:`sanitize_text` is the single implementation used by every such sink.
It lives in the library package - not in the simulator's front-end stack -
so ``client.py``, ``schedule.py`` and ``tz_utils.py`` can use it without
importing simulator or ``prompt_toolkit`` code.
"""

import re

#: C0 control characters (except tab/newline), DEL, and C1 control characters.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

#: Appended by :func:`sanitize_text` when ``limit`` cut the value short.
_TRUNCATION_MARKER = "...(truncated)"

#: Default cap for echoing a whole peer-chosen frame into a log record.
#: The frame is attacker-chosen and may be anything up to the 64 KiB
#: framing cap, so the constant of any per-frame log line has to be
#: bounded independently of how often the line fires (round-6 security
#: finding 2).
MAX_LOGGED_LENGTH = 200


def sanitize_text(text: object, limit: int | None = None) -> str:
    """Neutralize terminal control characters in untrusted text.

    Accepts any value (network-derived fields are not guaranteed to be
    strings) and replaces C0 controls (except tab and newline), DEL, and C1
    controls with their visible ``\\xNN`` escape, so the result is safe to
    write to a log record or a terminal.

    Args:
        text: The untrusted value; stringified if it is not already a string.
        limit: Maximum number of characters to keep. Longer values are cut
            and marked as truncated. Truncation happens *before* escaping,
            so an attacker-chosen 64 KiB frame costs a bounded amount of
            both log volume and regex work.

    Returns:
        The value with every control character replaced by ``\\xNN``.
    """
    value = str(text)
    if limit is not None and len(value) > limit:
        value = value[:limit] + _TRUNCATION_MARKER
    return _CONTROL_CHAR_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", value)
