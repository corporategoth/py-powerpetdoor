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


def sanitize_text(text: object) -> str:
    """Neutralize terminal control characters in untrusted text.

    Accepts any value (network-derived fields are not guaranteed to be
    strings) and replaces C0 controls (except tab and newline), DEL, and C1
    controls with their visible ``\\xNN`` escape, so the result is safe to
    write to a log record or a terminal.

    Args:
        text: The untrusted value; stringified if it is not already a string.

    Returns:
        The value with every control character replaced by ``\\xNN``.
    """
    return _CONTROL_CHAR_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))
