# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Shared JSON frame scanner for the Power Pet Door wire protocol.

Both the client and the simulator receive streams of JSON objects sent
back-to-back over TCP, optionally separated by whitespace or newlines.
This module provides the single, hardened frame scanner used by both
sides of the protocol:

- **String-aware brace matching**: braces inside JSON string values
  (including backslash-escaped quotes) do not confuse framing.
- **Whitespace tolerant**: whitespace/newlines between messages are
  silently skipped.
- **Resyncs on garbage**: any non-JSON prefix is discarded up to the next
  ``{`` and reported via diagnostics (and a warning log).
- **Bounded memory**: a hard cap on the un-parsed buffer prevents a
  hostile or broken peer from growing memory without bound.
- **Never raises** on arbitrary input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

#: Hard cap (in characters) on the un-parsed receive buffer retained
#: between calls. Real door messages are far smaller than this.
MAX_BUFFER_SIZE = 64 * 1024


@dataclass
class FrameDiagnostics:
    """Diagnostics reported by a single :func:`extract_frames` pass.

    Attributes:
        discarded: Number of non-whitespace garbage characters discarded
            while resynchronizing to the next JSON object.
        overflow: True if the un-parsed remainder exceeded the buffer cap
            and was cleared. Callers should treat this as a protocol
            violation (e.g. drop the connection).
    """

    discarded: int = 0
    overflow: bool = False


def find_frame_end(s: str) -> int | None:
    """Find the end of the first complete JSON object in a string.

    Brace matching is string-aware: braces inside JSON string values are
    ignored, and backslash escapes (``\\"``, ``\\\\``) inside strings are
    handled correctly.

    Args:
        s: Text that should begin with ``{``.

    Returns:
        The index one past the closing brace of the first complete JSON
        object, or None if the string is empty, does not start with
        ``{``, or contains no complete object. Never raises.
    """
    if not s or s[0] != "{":
        return None

    depth = 0
    in_string = False
    escaped = False
    for i, c in enumerate(s):
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1

    return None


def extract_frames(
    buffer: str, max_buffer: int = MAX_BUFFER_SIZE
) -> tuple[list[str], str, FrameDiagnostics]:
    """Extract complete JSON object frames from a receive buffer.

    Designed to be called each time data arrives: append the new data to
    the retained buffer, call this function, dispatch the returned frames,
    and keep the returned remainder as the new buffer.

    Leading whitespace between messages is skipped. Garbage (anything
    before the next ``{``) is discarded and counted in the diagnostics,
    with a warning logged. If the un-parsed remainder would exceed
    ``max_buffer`` characters, it is cleared and ``diagnostics.overflow``
    is set — callers should treat this as a protocol violation and drop
    the connection.

    This function never raises on arbitrary input.

    Args:
        buffer: Accumulated received text (previous remainder + new data).
        max_buffer: Maximum size of the returned un-parsed remainder.

    Returns:
        Tuple of ``(frames, remainder, diagnostics)`` where ``frames`` is
        a list of balanced-brace JSON object candidate strings (each may
        still fail ``json.loads`` — callers must handle that), ``remainder``
        is the un-consumed tail to retain for the next call, and
        ``diagnostics`` reports discarded garbage and buffer overflow.
    """
    frames: list[str] = []
    diag = FrameDiagnostics()

    while buffer:
        buffer = buffer.lstrip()
        if not buffer:
            break

        if buffer[0] != "{":
            # Resync: discard garbage up to the next '{'.
            next_obj = buffer.find("{")
            if next_obj == -1:
                diag.discarded += len(buffer)
                buffer = ""
            else:
                diag.discarded += next_obj
                buffer = buffer[next_obj:]
            continue

        end = find_frame_end(buffer)
        if end is None:
            break

        frames.append(buffer[:end])
        buffer = buffer[end:]

    if diag.discarded:
        _LOGGER.warning(
            "Discarded %d bytes of non-JSON garbage while framing messages", diag.discarded
        )

    if len(buffer) > max_buffer:
        _LOGGER.error(
            "Un-parsed receive buffer exceeded %d bytes without a complete message; clearing it",
            max_buffer,
        )
        diag.overflow = True
        buffer = ""

    return frames, buffer, diag
