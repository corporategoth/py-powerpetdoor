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
- **Bounded CPU**: :class:`FrameScanner` carries the brace-matching state
  across calls, so every received character is examined exactly once no
  matter how the peer chunks it, and the retained remainder is never
  re-copied onto the incoming chunk.
- **Bounded logging**: repeated peer-driven notices are counted by an
  :class:`EventThrottle` and reported on a doubling schedule, so a hostile
  peer cannot turn one byte per TCP segment into one log line per byte.
- **Bounded dispatch**: :class:`FrameDispatcher` runs a bounded number of
  per-frame handlers at once and pauses the transport while the backlog
  drains, so one 256 KiB read of ``{}`` cannot admit 131,072 concurrent
  tasks (~145 MB) in a single callback.
- **Never raises** on arbitrary input.

Receivers should hold a :class:`FrameScanner` for the lifetime of a
connection and call :meth:`FrameScanner.feed` with each chunk.
:func:`extract_frames` is the stateless one-shot equivalent (it restarts
the scan from the beginning of the buffer it is handed, so a caller that
re-feeds its own remainder pays O(N^2)).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

#: Hard cap (in characters) on the un-parsed receive buffer retained
#: between calls. Real door messages are far smaller than this.
#:
#: The cap is enforced on the retained *character count*. The retained
#: pieces are coalesced once there are more than
#: :data:`MAX_RETAINED_PIECES` of them, which is what keeps the memory
#: actually held within a small factor of this number rather than the
#: ~26x a one-character-per-piece list costs (round-6 backend L1).
MAX_BUFFER_SIZE = 64 * 1024

#: Retained pieces are joined once the list grows past this. The join is
#: O(retained) but amortized over this many feeds, so the per-byte cost
#: stays far below re-concatenating on every feed while the memory held
#: stays within a small constant factor of :data:`MAX_BUFFER_SIZE`.
MAX_RETAINED_PIECES = 64

#: Largest gap (in occurrences) :class:`EventThrottle`'s doubling schedule
#: will grow to. Without it the cadence degrades without bound on a
#: long-lived connection (round-6 security informational 1).
MAX_THROTTLE_INTERVAL = 4096

#: Seconds of quiet after which :class:`EventThrottle` reports the next
#: occurrence immediately and restarts its doubling schedule, so a *new*
#: burst late in a long-lived connection is never invisible (round-6
#: backend L4).
THROTTLE_QUIET_PERIOD = 60.0


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


class EventThrottle:
    """Bounded reporting for one repeated, peer-driven log event.

    Both sides of this protocol have log sites that fire once per
    ``data_received`` on a condition the peer picks (non-JSON garbage,
    non-ASCII bytes). One byte per TCP segment therefore bought one log
    line per byte - x247 write amplification, and 91% of a core once the
    simulator's control channel fans the records out to parked ``ctl -i``
    sessions. Nothing accumulated, so unlike the 64 KiB overflow the
    connection was never dropped and the attack ran indefinitely.

    Counting here and reporting on a doubling schedule - the 1st, 2nd,
    4th, 8th, ... occurrence - keeps the operator's first signal immediate
    while making total log volume logarithmic in the peer's traffic
    instead of linear. :meth:`flush` reports the suppressed tail when the
    connection ends, so the totals are never lost, only batched.

    "A hostile peer cannot amplify the log" and "a real fault is reported
    promptly" are separable goals, and a purely monotonic count schedule
    only achieves the first: after 1024 events a *fresh* burst of 1023
    logged nothing at all, on a client designed to hold one connection for
    months (round-6 backend L4 / security informational 1). Two bounds fix
    that without giving the amplification back:

    - ``quiet_period`` seconds without a report make the next occurrence
      report immediately and restart the doubling schedule, so every new
      burst gets an immediate first signal and stays logarithmic from
      there. The time path alone can produce at most one line per quiet
      period.
    - ``max_interval`` caps the gap the schedule grows to, so the cadence
      on a connection that never ends degrades to "every N events" rather
      than to "every 2^k events".

    Hold one per connection, alongside the connection's other state.

    Args:
        logger: Logger the summaries are written to.
        level: Level the summaries are written at.
        message: Format string taking ``(occurrences, total)``.
        max_interval: Largest gap, in occurrences, between two reports of
            an uninterrupted burst.
        quiet_period: Seconds without a report after which the next
            occurrence reports immediately.
        clock: Monotonic clock source; injectable so tests can drive the
            quiet period deterministically.
    """

    __slots__ = (
        "_clock",
        "_count",
        "_last_report",
        "_level",
        "_logger",
        "_max_interval",
        "_message",
        "_next",
        "_quiet_period",
        "_reported",
        "_step",
        "_total",
    )

    def __init__(
        self,
        logger: logging.Logger,
        level: int,
        message: str,
        *,
        max_interval: int = MAX_THROTTLE_INTERVAL,
        quiet_period: float = THROTTLE_QUIET_PERIOD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._level = level
        self._message = message
        self._max_interval = max_interval
        self._quiet_period = quiet_period
        self._clock = clock
        self._count = 0
        self._total = 0
        self._reported = 0
        self._next = 1
        self._step = 1
        self._last_report = clock()

    @property
    def count(self) -> int:
        """Occurrences recorded since the last :meth:`reset`."""
        return self._count

    @property
    def total(self) -> int:
        """Summed magnitude recorded since the last :meth:`reset`."""
        return self._total

    def record(self, amount: int = 1) -> bool:
        """Count one occurrence, logging a summary when one is due.

        Args:
            amount: Magnitude of this occurrence (bytes, usually).

        Returns:
            True if this occurrence was reported. Callers with a per-event
            *detail* worth seeing (the offending frame, the parse error)
            can log it on exactly the occurrences the summary fires on,
            keeping the detail's cost logarithmic too.
        """
        self._count += 1
        self._total += amount
        now = self._clock()
        if now - self._last_report >= self._quiet_period:
            # A burst that starts after a quiet period reports at once and
            # gets its own doubling schedule from 1 again.
            self._step = 1
        elif self._count < self._next:
            return False
        self._last_report = now
        self._next = self._count + self._step
        self._step = min(self._step * 2, self._max_interval)
        self._reported = self._count
        self._logger.log(self._level, self._message, self._count, self._total)
        return True

    def flush(self) -> None:
        """Report whatever has been counted since the last summary."""
        if self._count > self._reported:
            self._last_report = self._clock()
            self._reported = self._count
            self._logger.log(self._level, self._message, self._count, self._total)

    def reset(self) -> None:
        """Forget everything counted so far (a new connection starts clean)."""
        self._count = 0
        self._total = 0
        self._reported = 0
        self._next = 1
        self._step = 1
        self._last_report = self._clock()


class _BraceScanner:
    """Resumable, string-aware brace-matching state machine.

    The whole point is resumability: :meth:`scan` stops at the end of the
    text it is given and remembers the depth/string state, so a caller
    feeding the same object one chunk at a time never re-reads a character
    it has already seen. Restarting the scan instead is what made a peer
    dribbling one never-terminated object cost O(N^2) (S1).
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
    how the peer chunks them.

    The remainder is kept as a *list of pieces* rather than one string.
    Concatenating it onto every incoming chunk was the last piece of
    per-byte amplification left in the framer: while an object is in
    progress the remainder grows towards the 64 KiB cap, so a peer
    dribbling one byte per segment paid a ~32 KiB memcpy per delivered
    byte (~590x, measured). Pieces are joined when a frame actually
    completes, when a caller reads :attr:`buffer`, and once more than
    :data:`MAX_RETAINED_PIECES` of them have accumulated - the last of
    those is what keeps the character cap a memory cap too, since a piece
    per delivered byte costs ~26x the cap in object headers and pointer
    slots (L3, round-6 backend L1).

    Args:
        max_buffer: Maximum size of the retained un-parsed remainder.
            Exceeding it clears the buffer and sets ``overflow``.
    """

    __slots__ = ("_discards", "_max_buffer", "_pieces", "_retained", "_scanner")

    def __init__(self, max_buffer: int = MAX_BUFFER_SIZE) -> None:
        self._max_buffer = max_buffer
        self._pieces: list[str] = []
        self._retained = 0
        self._scanner = _BraceScanner()
        self._discards = EventThrottle(
            _LOGGER,
            logging.WARNING,
            "Discarded %d runs (%d bytes) of non-JSON garbage while framing messages",
        )

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
        """Drop the retained remainder and any in-progress object.

        Also reports and clears the throttled garbage counter: this is the
        end of a connection's framing state, so the totals belong in the
        log now rather than being carried into the next one.
        """
        self._pieces = []
        self._retained = 0
        self._scanner.reset()
        self._discards.flush()
        self._discards.reset()

    def feed(self, data: str) -> tuple[list[str], FrameDiagnostics]:
        """Frame ``data``, appending it to whatever was retained before.

        Leading whitespace between messages is skipped. Garbage (anything
        before the next ``{``) is discarded and counted in the
        diagnostics, with a throttled warning logged. If the un-parsed
        remainder would exceed ``max_buffer`` characters it is cleared, the
        scanner is reset and ``diagnostics.overflow`` is set - callers
        should treat that as a protocol violation and drop the connection.

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
                    # counted, so counting it *inside* a garbage run made
                    # the total depend on where the peer cut the packet
                    # (T1). `split()` does this at C speed.
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
            # slot, so a peer dribbling two bytes per segment held 1.71 MiB
            # per connection against a 64 KiB cap - 26.7x (round-6 backend
            # L1). Joining here is O(retained) but amortized over
            # MAX_RETAINED_PIECES feeds, so essentially all of the CPU the
            # piece list bought is kept while the cap bounds memory again.
            head = ["".join(head)]
        self._pieces = head
        self._retained = retained

        if diag.discarded:
            self._discards.record(diag.discarded)

        if retained > self._max_buffer:
            _LOGGER.error(
                "Un-parsed receive buffer exceeded %d bytes without a complete message; clearing it",
                self._max_buffer,
            )
            diag.overflow = True
            self.reset()

        return frames, diag


#: Frames whose handlers may be in flight at once, per connection.
#: Far above any real device's burst, so normal traffic is dispatched
#: exactly as it was before the bound existed.
MAX_INFLIGHT_FRAMES = 64

#: Backlog depth (frames waiting for a dispatch slot) above which the
#: connection's transport stops being read until it drains.
MAX_FRAME_BACKLOG = 256


class FrameDispatcher:
    """Dispatch framed messages with bounded concurrency and backpressure.

    Both sides of this protocol used to create one ``asyncio.Task`` per
    framed message, synchronously, inside ``data_received`` - so none of
    them had run yet when the last one was created. asyncio reads up to
    256 KiB per callback and the cheapest legal frame is two bytes
    (``{}``), so a single read admitted 131,072 live tasks and ~145 MB of
    heap, in the shipped client as well as the simulator, with nothing
    bounding the count, the per-task cost, or the number of connections
    doing it at once (round-6 security finding 1).

    This holds the framed-but-not-yet-dispatched messages in a deque and
    keeps at most ``max_inflight`` handlers running at a time, starting
    the next one from the previous one's done-callback. When the backlog
    passes ``pause_at`` the transport is paused, and it resumes once the
    backlog drains - which is what bounds the *transient* peak rather than
    just the steady state.

    Hold one per connection, alongside the connection's
    :class:`FrameScanner`.

    Args:
        dispatch: Called with one frame; returns the task it started, or
            None if the frame produced no work (e.g. it failed to parse).
        max_inflight: Handlers allowed to run concurrently.
        pause_at: Backlog depth above which reading is paused.
    """

    __slots__ = (
        "_backlog",
        "_dispatch",
        "_inflight",
        "_max_inflight",
        "_pause_at",
        "_paused",
        "_transport",
    )

    def __init__(
        self,
        dispatch: Callable[[str], asyncio.Task | None],
        *,
        max_inflight: int = MAX_INFLIGHT_FRAMES,
        pause_at: int = MAX_FRAME_BACKLOG,
    ) -> None:
        self._dispatch = dispatch
        self._max_inflight = max_inflight
        self._pause_at = pause_at
        self._backlog: deque[str] = deque()
        self._inflight = 0
        self._paused = False
        self._transport: asyncio.ReadTransport | None = None

    @property
    def backlog(self) -> int:
        """Frames waiting for a dispatch slot."""
        return len(self._backlog)

    @property
    def inflight(self) -> int:
        """Frame handlers currently running."""
        return self._inflight

    @property
    def paused(self) -> bool:
        """Whether reading is currently paused for backpressure."""
        return self._paused

    def submit(self, frames: list[str], transport: asyncio.ReadTransport | None) -> None:
        """Queue ``frames`` for dispatch, starting as many as fit.

        Args:
            frames: Frames produced by one :meth:`FrameScanner.feed`.
            transport: The connection's transport, used for flow control.
                None disables flow control (the frames are still bounded
                by ``max_inflight``).
        """
        self._transport = transport
        self._backlog.extend(frames)
        self._pump()

    def reset(self) -> None:
        """Drop the undispatched backlog (the connection is over).

        ``_inflight`` is deliberately left alone: the handlers already
        running still deliver their done-callbacks, which is what returns
        the count to zero without it ever going negative.
        """
        self._backlog.clear()
        self._paused = False
        self._transport = None

    def _pump(self) -> None:
        while self._backlog and self._inflight < self._max_inflight:
            task = self._dispatch(self._backlog.popleft())
            if task is not None:
                self._inflight += 1
                task.add_done_callback(self._on_dispatched_done)
        self._update_flow()

    def _on_dispatched_done(self, task: asyncio.Task) -> None:
        self._inflight -= 1
        self._pump()

    def _update_flow(self) -> None:
        transport = self._transport
        if transport is None:
            return
        if len(self._backlog) > self._pause_at:
            if not self._paused:
                self._paused = True
                transport.pause_reading()
        elif self._paused:
            self._paused = False
            transport.resume_reading()


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
