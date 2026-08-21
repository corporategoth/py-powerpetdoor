# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the shared JSON frame scanner."""

from __future__ import annotations

import json

from powerpetdoor.framing import (
    MAX_BUFFER_SIZE,
    FrameDiagnostics,
    extract_frames,
    find_frame_end,
)

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
        """A garbage-only buffer is fully discarded, not retained."""
        frames, remainder, diag = extract_frames("garbage not json")
        assert frames == []
        assert remainder == ""
        assert diag.discarded == len("garbage not json")

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
