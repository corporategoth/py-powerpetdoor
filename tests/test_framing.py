# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the shared JSON frame scanner."""

from __future__ import annotations

import json
import logging

import pytest

from powerpetdoor import framing
from powerpetdoor.framing import (
    MAX_BUFFER_SIZE,
    FrameDiagnostics,
    FrameScanner,
    extract_frames,
    find_frame_end,
)


@pytest.fixture
def scan_counter(monkeypatch):
    """Count the characters the brace scanner actually examines.

    ``_BraceScanner.scan`` returns the index it stopped at, so the work it
    did is exactly ``end - start``. Summing that across a whole delivery
    is the direct measurement of the quadratic-rescan defect (S1).
    """
    examined = [0]
    original = framing._BraceScanner.scan

    def counting_scan(self, s, start):
        end = original(self, s, start)
        examined[0] += end - start
        return end

    monkeypatch.setattr(framing._BraceScanner, "scan", counting_scan)
    return examined


# ============================================================================
# find_frame_end
# ============================================================================


class TestFindFrameEnd:
    """Tests for the string-aware brace matcher."""

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert find_frame_end("") is None

    def test_non_brace_start_returns_none(self):
        """A string not starting with '{' returns None (never raises)."""
        assert find_frame_end("hello world") is None

    def test_simple_object(self):
        """Simple JSON object end is found."""
        assert find_frame_end('{"key": "value"}') == 16

    def test_nested_object(self):
        """Nested JSON objects are handled correctly."""
        s = '{"outer": {"inner": "value"}}'
        assert find_frame_end(s) == len(s)

    def test_object_with_trailing(self):
        """Returns position after the first complete object only."""
        assert find_frame_end('{"first": 1}{"second": 2}') == 12

    def test_incomplete_object_returns_none(self):
        """Incomplete JSON returns None."""
        assert find_frame_end('{"key": "val') is None

    def test_closing_brace_in_string_value(self):
        """A '}' inside a string value does not terminate the frame."""
        s = '{"a": "}"}'
        assert find_frame_end(s) == len(s)

    def test_opening_brace_in_string_value(self):
        """A '{' inside a string value does not increase nesting depth."""
        s = '{"a": "{"}'
        assert find_frame_end(s) == len(s)

    def test_escaped_quote_in_string(self):
        """An escaped quote does not end the string state."""
        s = '{"a": "x\\"}\\""}'
        assert find_frame_end(s) == len(s)
        assert json.loads(s) == {"a": 'x"}"'}

    def test_escaped_backslash_before_closing_quote(self):
        """An escaped backslash before the closing quote ends the string."""
        s = '{"a": "x\\\\"}'
        assert find_frame_end(s) == len(s)
        assert json.loads(s) == {"a": "x\\"}

    def test_unterminated_string_returns_none(self):
        """An object with an unterminated string value is incomplete."""
        assert find_frame_end('{"a": "never ends}') is None

    def test_array_in_object(self):
        """Arrays within objects do not affect brace matching."""
        s = '{"items": [1, 2, 3]}'
        assert find_frame_end(s) == len(s)


# ============================================================================
# extract_frames
# ============================================================================


class TestExtractFrames:
    """Tests for the stream frame extractor."""

    def test_empty_buffer(self):
        """Empty buffer yields no frames and no remainder."""
        frames, remainder, diag = extract_frames("")
        assert frames == []
        assert remainder == ""
        assert diag == FrameDiagnostics()

    def test_single_frame(self):
        """A single complete object is extracted fully."""
        frames, remainder, diag = extract_frames('{"a": 1}')
        assert frames == ['{"a": 1}']
        assert remainder == ""
        assert diag.discarded == 0
        assert diag.overflow is False

    def test_multiple_frames(self):
        """Back-to-back objects are all extracted in order."""
        frames, remainder, _ = extract_frames('{"a": 1}{"b": 2}{"c": 3}')
        assert frames == ['{"a": 1}', '{"b": 2}', '{"c": 3}']
        assert remainder == ""

    def test_partial_frame_retained(self):
        """An incomplete trailing object is kept as the remainder."""
        frames, remainder, _ = extract_frames('{"a": 1}{"b": ')
        assert frames == ['{"a": 1}']
        assert remainder == '{"b": '

    def test_whitespace_between_frames(self):
        """Whitespace separators between messages are tolerated."""
        frames, remainder, diag = extract_frames('{"a": 1} \n\t{"b": 2}\r\n')
        assert frames == ['{"a": 1}', '{"b": 2}']
        assert remainder == ""
        assert diag.discarded == 0

    def test_newline_terminated_message(self):
        """A newline-terminated message (per old protocol docs) parses cleanly."""
        frames, remainder, diag = extract_frames('{"a": 1}\n')
        assert frames == ['{"a": 1}']
        assert remainder == ""
        assert diag.discarded == 0

    def test_leading_whitespace_only(self):
        """A whitespace-only buffer is consumed with no diagnostics."""
        frames, remainder, diag = extract_frames("  \n\t ")
        assert frames == []
        assert remainder == ""
        assert diag.discarded == 0

    def test_garbage_prefix_resync(self):
        """Garbage before an object is discarded and the object recovered."""
        frames, remainder, diag = extract_frames('junk{"a": 1}')
        assert frames == ['{"a": 1}']
        assert remainder == ""
        assert diag.discarded == 4

    def test_garbage_between_frames(self):
        """Garbage between objects is discarded; both objects recovered."""
        frames, remainder, diag = extract_frames('{"a": 1}xx{"b": 2}')
        assert frames == ['{"a": 1}', '{"b": 2}']
        assert diag.discarded == 2

    def test_garbage_only_buffer_cleared(self):
        """A garbage-only buffer is fully discarded, not retained.

        Only the non-whitespace is *counted*: the two spaces in this
        stream are discarded like everything else, but counting them would
        make the total depend on where the peer cut the packet (T1).
        """
        frames, remainder, diag = extract_frames("garbage not json")
        assert frames == []
        assert remainder == ""
        assert diag.discarded == len("garbagenotjson")

    def test_stray_closing_brace_discarded(self):
        """A stray '}' (e.g. from earlier mis-framing) is discarded."""
        frames, remainder, diag = extract_frames('}{"a": 1}')
        assert frames == ['{"a": 1}']
        assert diag.discarded == 1

    def test_brace_in_string_frames_correctly(self):
        """A message containing a brace inside a string frames correctly."""
        msg = '{"reason": "unmatched } brace", "a": 1}'
        frames, remainder, _ = extract_frames(msg + '{"b": 2}')
        assert frames == [msg, '{"b": 2}']
        assert remainder == ""

    def test_balanced_but_invalid_json_still_framed(self):
        """A balanced-brace but invalid JSON candidate is still returned.

        JSON validation is the caller's responsibility.
        """
        frames, remainder, _ = extract_frames('{"a" broken}{"b": 2}')
        assert frames == ['{"a" broken}', '{"b": 2}']
        assert remainder == ""

    def test_buffer_at_cap_retained(self):
        """An incomplete buffer exactly at the cap is retained."""
        buf = "{" + "x" * (MAX_BUFFER_SIZE - 1)
        # 'x' after '{' is garbage *inside* an incomplete object; the frame
        # scanner treats it as an unfinished frame and retains it.
        frames, remainder, diag = extract_frames(buf)
        assert frames == []
        assert remainder == buf
        assert diag.overflow is False

    def test_buffer_over_cap_cleared(self):
        """An incomplete buffer over the cap is cleared with overflow set."""
        buf = "{" * (MAX_BUFFER_SIZE + 1)
        frames, remainder, diag = extract_frames(buf)
        assert frames == []
        assert remainder == ""
        assert diag.overflow is True

    def test_custom_cap(self):
        """The cap is configurable."""
        frames, remainder, diag = extract_frames('{"a": ', max_buffer=3)
        assert frames == []
        assert remainder == ""
        assert diag.overflow is True

    def test_complete_frames_extracted_before_cap_check(self):
        """Complete frames are extracted even if garbage overflows the cap."""
        msg = '{"a": 1}'
        frames, remainder, diag = extract_frames(msg + "{" * 10, max_buffer=5)
        assert frames == [msg]
        assert remainder == ""
        assert diag.overflow is True

    def test_incremental_delivery_across_chunks(self):
        """Chunked delivery reassembles the same frames."""
        chunks = ['{"a"', ': 1}{"b": ', "2}"]
        buffer = ""
        collected: list[str] = []
        for chunk in chunks:
            frames, buffer, diag = extract_frames(buffer + chunk)
            collected.extend(frames)
            assert diag.overflow is False
        assert collected == ['{"a": 1}', '{"b": 2}']
        assert buffer == ""


# ============================================================================
# FrameScanner (stateful, per-connection)
# ============================================================================


class TestFrameScanner:
    """Tests for the resumable per-connection scanner."""

    def test_single_frame_in_one_feed(self):
        """A complete object in one chunk comes straight back out."""
        scanner = FrameScanner()
        frames, diag = scanner.feed('{"a": 1}')
        assert frames == ['{"a": 1}']
        assert scanner.buffer == ""
        assert diag == FrameDiagnostics()

    def test_frame_split_across_feeds_is_reassembled(self):
        """A frame split across reads is joined, not lost."""
        scanner = FrameScanner()
        assert scanner.feed('{"CMD": "PO')[0] == []
        assert scanner.buffer == '{"CMD": "PO'
        assert scanner.feed('NG"}')[0] == ['{"CMD": "PONG"}']
        assert scanner.buffer == ""

    def test_brace_inside_a_string_split_across_feeds(self):
        """The string state machine survives a chunk boundary."""
        scanner = FrameScanner()
        assert scanner.feed('{"a": "x}')[0] == []
        assert scanner.feed('y"}')[0] == ['{"a": "x}y"}']
        assert scanner.buffer == ""

    def test_escaped_quote_split_across_feeds(self):
        """A backslash escape split across chunks is still an escape."""
        scanner = FrameScanner()
        assert scanner.feed('{"a": "x\\')[0] == []
        assert scanner.feed('"}"}')[0] == ['{"a": "x\\"}"}']
        assert scanner.buffer == ""

    def test_multiple_frames_and_whitespace(self):
        """Several frames plus separators come out in order."""
        scanner = FrameScanner()
        frames, diag = scanner.feed('{"a": 1} \n\t{"b": 2}\r\n')
        assert frames == ['{"a": 1}', '{"b": 2}']
        assert scanner.buffer == ""
        assert diag.discarded == 0

    def test_garbage_between_frames_is_discarded(self):
        """Non-JSON garbage resyncs to the next object."""
        scanner = FrameScanner()
        frames, diag = scanner.feed('{"a": 1}xx{"b": 2}')
        assert frames == ['{"a": 1}', '{"b": 2}']
        assert diag.discarded == 2
        assert scanner.buffer == ""

    def test_garbage_split_across_feeds(self):
        """Garbage delivered in pieces is discarded in pieces."""
        scanner = FrameScanner()
        assert scanner.feed("gar")[1].discarded == 3
        assert scanner.buffer == ""
        frames, diag = scanner.feed('bage{"a": 1}')
        assert frames == ['{"a": 1}']
        assert diag.discarded == 4

    def test_overflow_clears_buffer_and_scanner_state(self):
        """Overflow resets the in-progress object, not just the buffer."""
        scanner = FrameScanner(max_buffer=8)
        frames, diag = scanner.feed('{"a": "xxxxxxxxxx')
        assert frames == []
        assert diag.overflow is True
        assert scanner.buffer == ""
        # The abandoned object must not swallow the next real frame: if the
        # depth/in_string state survived, this would be treated as a
        # continuation instead of a fresh object.
        frames, diag = scanner.feed('{"b": 2}')
        assert frames == ['{"b": 2}']
        assert diag.overflow is False

    def test_reset_drops_a_partial_frame(self):
        """reset() forgets the retained remainder and the open object."""
        scanner = FrameScanner()
        scanner.feed('{"a": ')
        assert scanner.buffer == '{"a": '
        scanner.reset()
        assert scanner.buffer == ""
        assert scanner.feed('{"b": 2}')[0] == ['{"b": 2}']

    def test_dribbled_object_examines_each_character_once(self, scan_counter):
        """A byte-at-a-time unterminated object costs O(N), not O(N^2).

        This is the CPU-exhaustion vector (S1): re-scanning the retained
        buffer on every ``data_received`` cost ~N^2/2 character steps for
        N delivered bytes. Each byte must be examined exactly once.
        """
        payload = '{"a": "' + "x" * 4000
        scanner = FrameScanner()
        for char in payload:
            scanner.feed(char)
        assert scanner.buffer == payload
        assert scan_counter[0] == len(payload)

    def test_scan_work_is_independent_of_chunking(self, scan_counter):
        """Total scan work is the same whatever chunk size the peer picks."""
        payload = '{"a": "' + "y" * 2000
        for chunk_size in (1, 7, 256, len(payload)):
            scan_counter[0] = 0
            scanner = FrameScanner()
            for start in range(0, len(payload), chunk_size):
                scanner.feed(payload[start : start + chunk_size])
            assert scan_counter[0] == len(payload)

    def test_nested_braces_dribbled_are_still_linear(self, scan_counter):
        """Nested braces defeat a '}'-lookahead shortcut but not state carry."""
        payload = "{" * 3000
        scanner = FrameScanner()
        for char in payload:
            scanner.feed(char)
        assert scan_counter[0] == len(payload)


# ============================================================================
# Copy cost and chunk-invariance (L3, T1)
# ============================================================================


class TestRetainedRemainderCost:
    """The retained remainder is never re-copied onto the incoming chunk (L3)."""

    def test_the_scanner_never_sees_the_retained_remainder_again(self, monkeypatch):
        """The string handed to the scanner is the new chunk, not buffer+chunk.

        ``buf = self._buffer + data`` re-allocated the whole retained
        remainder on every ``feed()``. While an object is in progress that
        remainder grows toward the 64 KiB cap, so each delivered byte cost
        a ~32 KiB memcpy - ~590x amplification at 1-byte chunks, measured.
        The length of the string passed to ``scan`` *is* that copy.
        """
        sizes: list[int] = []
        original = framing._BraceScanner.scan

        def recording_scan(self, s, start):
            sizes.append(len(s))
            return original(self, s, start)

        monkeypatch.setattr(framing._BraceScanner, "scan", recording_scan)

        payload = '{"a": "' + "x" * 500 + '"}'
        scanner = FrameScanner()
        for char in payload:
            frames, _diag = scanner.feed(char)

        assert frames == [payload]
        # One character per call throughout. The old shape grew to
        # len(payload) on the last call and summed to O(N^2) bytes copied.
        assert sizes == [1] * len(payload)

    def test_the_remainder_is_retained_as_pieces_not_one_string(self):
        """Nothing is concatenated until a frame completes."""
        scanner = FrameScanner()
        for chunk in ('{"a"', ': "b', "cd"):
            scanner.feed(chunk)

        assert len(scanner._pieces) == 3

    def test_buffer_property_coalesces_and_is_stable(self):
        """Reading .buffer repeatedly must not keep re-joining pieces."""
        scanner = FrameScanner()
        for char in '{"a": "xyz':
            scanner.feed(char)

        assert scanner.buffer == '{"a": "xyz'
        # Coalesced in place: a second read sees a single piece.
        assert len(scanner._pieces) == 1
        assert scanner.buffer == '{"a": "xyz'

    def test_retained_length_tracks_the_pieces(self):
        """The overflow check reads a running total, not len(join(...))."""
        scanner = FrameScanner()
        for chunk in ('{"a"', ": ", '"xy'):
            scanner.feed(chunk)

        assert scanner._retained == len('{"a": "xy')
        assert scanner.buffer == '{"a": "xy'

    def test_a_frame_that_spans_three_feeds_is_assembled_intact(self):
        """The join happens across every retained piece, in order."""
        scanner = FrameScanner()
        assert scanner.feed('{"a":')[0] == []
        assert scanner.feed(' "b')[0] == []
        frames, _diag = scanner.feed('c"}')

        assert frames == ['{"a": "bc"}']
        assert scanner.buffer == ""

    def test_a_trailing_frame_after_a_spanning_one_is_not_prefixed(self):
        """Pieces are dropped when the frame they belong to completes."""
        scanner = FrameScanner()
        scanner.feed('{"a":')
        frames, _diag = scanner.feed(' 1}{"b": 2}')

        assert frames == ['{"a": 1}', '{"b": 2}']
        assert scanner.buffer == ""


class TestDiscardedIsChunkInvariant:
    """``discarded`` counts non-whitespace only, so chunking cannot move it (T1)."""

    @pytest.mark.parametrize(
        "stream",
        [
            "000000000000000 ",
            " :}}\n\\{a{",
            "abc \t def{}",
            "   ",
            "}}} {{",
        ],
        ids=repr,
    )
    def test_total_is_the_same_at_every_cut_point(self, stream):
        one_shot = FrameScanner()
        expected = one_shot.feed(stream)[1].discarded

        for cut in range(len(stream) + 1):
            scanner = FrameScanner()
            total = scanner.feed(stream[:cut])[1].discarded
            total += scanner.feed(stream[cut:])[1].discarded
            assert total == expected, f"cut at {cut}"

    def test_whitespace_inside_a_garbage_run_is_not_counted(self):
        """The documented contract: "non-whitespace garbage characters"."""
        frames, diag = FrameScanner().feed("ab cd{}")

        assert frames == ["{}"]
        assert diag.discarded == 4

    def test_byte_at_a_time_garbage_matches_a_single_delivery(self):
        one_shot = FrameScanner().feed("xx yy zz")[1].discarded

        scanner = FrameScanner()
        total = sum(scanner.feed(char)[1].discarded for char in "xx yy zz")

        assert total == one_shot == 6


# ============================================================================
# EventThrottle / bounded log volume (Security round-5 Finding 1)
# ============================================================================


class TestEventThrottle:
    """Repeated peer-driven notices are counted, not logged one per event."""

    def _throttle(self):
        return framing.EventThrottle(framing._LOGGER, logging.WARNING, "seen %d (%d bytes)")

    def test_reports_on_a_doubling_schedule(self, caplog):
        throttle = self._throttle()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            for _ in range(16):
                throttle.record()

        # 1, 2, 4, 8, 16 - logarithmic in the peer's traffic, not linear.
        assert [record.getMessage() for record in caplog.records] == [
            "seen 1 (1 bytes)",
            "seen 2 (2 bytes)",
            "seen 4 (4 bytes)",
            "seen 8 (8 bytes)",
            "seen 16 (16 bytes)",
        ]

    def test_first_occurrence_is_never_delayed(self, caplog):
        """The operator's first signal must stay immediate."""
        throttle = self._throttle()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            throttle.record(41)

        assert len(caplog.records) == 1
        assert throttle.count == 1
        assert throttle.total == 41

    def test_flush_reports_the_suppressed_tail(self, caplog):
        throttle = self._throttle()
        for _ in range(3):
            throttle.record(10)
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            throttle.flush()

        assert [record.getMessage() for record in caplog.records] == ["seen 3 (30 bytes)"]

    def test_flush_is_silent_when_nothing_is_outstanding(self, caplog):
        throttle = self._throttle()
        throttle.record()  # reported immediately
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            throttle.flush()
            throttle.flush()

        assert caplog.records == []

    def test_reset_starts_the_next_connection_clean(self, caplog):
        throttle = self._throttle()
        for _ in range(5):
            throttle.record()

        throttle.reset()
        assert (throttle.count, throttle.total) == (0, 0)
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            throttle.record(7)

        assert [record.getMessage() for record in caplog.records] == ["seen 1 (7 bytes)"]


class TestGarbageLogVolumeIsBounded:
    """A garbage dribble cannot buy one log line per byte (Security F1)."""

    def test_ten_thousand_garbage_chunks_produce_a_handful_of_records(self, caplog):
        scanner = FrameScanner()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            for _ in range(10000):
                scanner.feed("x")

        # Reports at 1, 2, 4, ... 8192: 14 summaries for 10,000 events,
        # versus 10,000 log lines before.
        assert len(caplog.records) == 14
        assert "8192 runs (8192 bytes)" in caplog.records[-1].getMessage()

    def test_the_first_garbage_run_is_still_reported_immediately(self, caplog):
        scanner = FrameScanner()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            scanner.feed("garbage{}")

        assert len(caplog.records) == 1
        assert "non-JSON garbage" in caplog.records[0].getMessage()

    def test_reset_reports_the_connection_total_once(self, caplog):
        scanner = FrameScanner()
        for _ in range(3):
            scanner.feed("zz")
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            scanner.reset()

        assert len(caplog.records) == 1
        assert "3 runs (6 bytes)" in caplog.records[0].getMessage()

    def test_reset_is_silent_when_nothing_was_discarded(self, caplog):
        scanner = FrameScanner()
        scanner.feed('{"a": 1}')
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.framing"):
            scanner.reset()

        assert caplog.records == []
