# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Shared JSON frame scanner for the Power Pet Door wire protocol.

Both the client and the simulator receive streams of JSON objects sent
back-to-back over TCP, optionally separated by whitespace or newlines.
This module provides the single frame scanner used by both sides of the
protocol:

- **String-aware brace matching**: braces inside JSON string values
  (including backslash-escaped quotes) do not confuse framing.
- **Whitespace tolerant**: whitespace/newlines between messages are
  silently skipped.
- **Resyncs on garbage**: any non-JSON prefix is discarded up to the next
  ``{`` and reported via diagnostics (and a warning log).
- **Bounded memory**: a hard cap on the un-parsed buffer keeps a door that
  never finishes a message from growing memory without bound.
- **Bounded CPU**: :class:`FrameScanner` carries the brace-matching state
  across calls, so every received character is examined exactly once no
  matter how the door chunks it, and the retained remainder is never
  re-copied onto the incoming chunk.
- **Never raises** on arbitrary input.

Receivers should hold a :class:`FrameScanner` for the lifetime of a
connection and call :meth:`FrameScanner.feed` with each chunk.
:func:`extract_frames` is the stateless one-shot equivalent (it restarts
the scan from the beginning of the buffer it is handed, so a caller that
re-feeds its own remainder pays O(N^2)).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

#: Hard cap (in characters) on the un-parsed receive buffer retained
#: between calls. Real door messages are far smaller than this; a door that
#: never terminates a message must not exhaust memory.
#:
#: The cap is enforced on the retained *character count*. The retained
#: pieces are coalesced once there are more than
#: :data:`MAX_RETAINED_PIECES` of them, which is what keeps the memory
#: actually held within a small factor of this number rather than the ~26x
#: a one-character-per-piece list costs.
MAX_BUFFER_SIZE = 64 * 1024

#: Retained pieces are joined once the list grows past this. The join is
#: O(retained) but amortized over this many feeds, so the per-byte cost
#: stays far below re-concatenating on every feed while the memory held
#: stays within a small constant factor of :data:`MAX_BUFFER_SIZE`.
MAX_RETAINED_PIECES = 64


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


class _BraceScanner:
    """Resumable, string-aware brace-matching state machine.

    The whole point is resumability: :meth:`scan` stops at the end of the
    text it is given and remembers the depth/string state, so a caller
    feeding the same object one chunk at a time never re-reads a character
    it has already seen. Restarting the scan instead makes a door that
    dribbles one never-terminated object cost O(N^2).
    """

    __slots__ = ("depth", "escaped", "in_string")

    def __init__(self) -> None:
        self.depth = 0
        self.in_string = False
        self.escaped = False

    @property
    def open(self) -> bool:
        """Whether an object is currently in progress (unbalanced braces)."""
        return self.depth > 0

    def reset(self) -> None:
        """Forget any in-progress object."""
        self.depth = 0
        self.in_string = False
        self.escaped = False

    def scan(self, s: str, start: int) -> int:
        """Advance the scanner over ``s`` starting at index ``start``.

        Args:
            s: Text to scan. ``s[start]`` must be ``{`` when no object is
                already in progress.
            start: Index of the first character to examine.

        Returns:
            The index one past the closing brace of the object that
            completed, or ``len(s)`` if the object is still open (in which
            case :attr:`open` is True and the next call resumes here).
        """
        depth = self.depth
        in_string = self.in_string
        escaped = self.escaped
        i = start
        n = len(s)
        while i < n:
            c = s[i]
            i += 1
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
                if depth <= 0:
                    break
        self.depth = depth
        self.in_string = in_string
        self.escaped = escaped
        return i


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

    scanner = _BraceScanner()
    end = scanner.scan(s, 0)
    return None if scanner.open else end


class FrameScanner:
    """Stateful JSON frame scanner for one connection.

    Hold one of these per connection and feed it every chunk as it
    arrives. The retained remainder and the brace-matching state travel
    together, so a partially delivered object is resumed rather than
    re-scanned: total work is linear in the bytes received regardless of
    how the door chunks them.

    The remainder is kept as a *list of pieces* rather than one string,
    because concatenating it onto every incoming chunk costs a memcpy of
    the whole retained buffer per delivered byte while an object is in
    progress. Pieces are joined when a frame actually completes, when a
    caller reads :attr:`buffer`, and once more than
    :data:`MAX_RETAINED_PIECES` of them have accumulated - the last of
    those is what keeps the character cap a memory cap too, since a piece
    per delivered byte costs ~26x the cap in object headers and pointer
    slots.

    Args:
        max_buffer: Maximum size of the retained un-parsed remainder.
            Exceeding it clears the buffer and sets ``overflow``.
    """

    __slots__ = ("_max_buffer", "_pieces", "_retained", "_scanner")

    def __init__(self, max_buffer: int = MAX_BUFFER_SIZE) -> None:
        self._max_buffer = max_buffer
        self._pieces: list[str] = []
        self._retained = 0
        self._scanner = _BraceScanner()

    @property
    def buffer(self) -> str:
        """The un-parsed remainder retained for the next :meth:`feed`."""
        if not self._pieces:
            return ""
        if len(self._pieces) > 1:
            # Coalesce in place so repeated reads stay cheap.
            self._pieces = ["".join(self._pieces)]
        return self._pieces[0]

    def reset(self) -> None:
        """Drop the retained remainder and any in-progress object."""
        self._pieces = []
        self._retained = 0
        self._scanner.reset()

    def feed(self, data: str) -> tuple[list[str], FrameDiagnostics]:
        """Frame ``data``, appending it to whatever was retained before.

        Leading whitespace between messages is skipped. Garbage (anything
        before the next ``{``) is discarded and counted in the
        diagnostics, with a warning logged. If the un-parsed remainder
        would exceed ``max_buffer`` characters it is cleared, the scanner
        is reset and ``diagnostics.overflow`` is set - callers should treat
        that as a protocol violation and drop the connection.

        This method never raises on arbitrary input.

        Args:
            data: Newly received text.

        Returns:
            Tuple of ``(frames, diagnostics)`` where ``frames`` is a list
            of balanced-brace JSON object candidate strings (each may
            still fail ``json.loads`` - callers must handle that).
        """
        frames: list[str] = []
        diag = FrameDiagnostics()
        scanner = self._scanner
        n = len(data)
        # Everything retained from an earlier call was already examined,
        # and when an object is in progress the retained pieces start at
        # that object's opening brace - so only the new chunk is scanned,
        # and `consumed` (the index in `data` everything before which is
        # finished) starts at 0 either way.
        head = self._pieces
        retained = self._retained
        i = 0
        consumed = 0

        while i < n:
            if not scanner.open:
                char = data[i]
                if char.isspace():
                    i += 1
                    consumed = i
                    continue
                if char != "{":
                    # Resync: discard garbage up to the next '{'. Only the
                    # non-whitespace is counted - whitespace at a resync
                    # boundary is consumed by the branch above and never
                    # counted, so counting it *inside* a garbage run would
                    # make the total depend on where the chunk was cut.
                    # `split()` does this at C speed.
                    next_obj = data.find("{", i)
                    end = n if next_obj == -1 else next_obj
                    diag.discarded += sum(map(len, data[i:end].split()))
                    i = consumed = end
                    continue
                consumed = i
            i = scanner.scan(data, i)
            if not scanner.open:
                chunk = data[consumed:i]
                # The only place the retained pieces are joined: an object
                # that spans feeds completes exactly once.
                frames.append("".join((*head, chunk)) if head else chunk)
                head = []
                retained = 0
                consumed = i

        tail = data[consumed:]
        if tail:
            head.append(tail)
            retained += len(tail)
        if len(head) > MAX_RETAINED_PIECES:
            # `_retained` counts characters, but each retained piece is a
            # separate `str` object (~49 bytes of header) plus a pointer
            # slot, so two bytes per segment would hold 1.71 MiB per
            # connection against a 64 KiB cap. Joining here is O(retained)
            # but amortized over MAX_RETAINED_PIECES feeds.
            head = ["".join(head)]
        self._pieces = head
        self._retained = retained

        if diag.discarded:
            _LOGGER.warning(
                "Discarded %d bytes of non-JSON garbage while framing messages", diag.discarded
            )

        if retained > self._max_buffer:
            _LOGGER.error(
                "Un-parsed receive buffer exceeded %d bytes without a complete message; clearing it",
                self._max_buffer,
            )
            diag.overflow = True
            self.reset()

        return frames, diag


def extract_frames(
    buffer: str, max_buffer: int = MAX_BUFFER_SIZE
) -> tuple[list[str], str, FrameDiagnostics]:
    """Extract complete JSON object frames from a receive buffer (one-shot).

    Stateless convenience wrapper around :class:`FrameScanner` for callers
    that hold nothing but a string. Note that re-feeding the returned
    remainder restarts the brace scan from its first character; a
    connection that receives a partial object over many reads should hold
    a :class:`FrameScanner` instead, which resumes where it left off.

    Leading whitespace between messages is skipped. Garbage (anything
    before the next ``{``) is discarded and counted in the diagnostics,
    with a warning logged. If the un-parsed remainder would exceed
    ``max_buffer`` characters, it is cleared and ``diagnostics.overflow``
    is set - callers should treat this as a protocol violation and drop
    the connection.

    This function never raises on arbitrary input.

    Args:
        buffer: Accumulated received text (previous remainder + new data).
        max_buffer: Maximum size of the returned un-parsed remainder.

    Returns:
        Tuple of ``(frames, remainder, diagnostics)`` where ``frames`` is
        a list of balanced-brace JSON object candidate strings (each may
        still fail ``json.loads`` - callers must handle that), ``remainder``
        is the un-consumed tail to retain for the next call, and
        ``diagnostics`` reports discarded garbage and buffer overflow.
    """
    scanner = FrameScanner(max_buffer)
    frames, diag = scanner.feed(buffer)
    return frames, scanner.buffer, diag
