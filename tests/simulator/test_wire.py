# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the shared wire-capture test helper.

``WireCapture`` is load-bearing for 40+ integration tests: if its framing
silently drops a message, those tests report a false *pass*. The framing
promises it makes in its own docstrings are pinned here.
"""

from __future__ import annotations

import pytest

from powerpetdoor.const import DOOR_STATUS, FIELD_DOOR_STATUS

from .wire import WireCapture


@pytest.fixture
def capture() -> WireCapture:
    """A capture with no streams attached (feed/inspection only)."""
    return WireCapture(None, None)


class TestWireCaptureFraming:
    """The reassembly the shared helper exists to provide."""

    def test_feed_returns_a_whole_frame(self, capture):
        """A complete frame in one read is parsed and recorded."""
        assert capture.feed(b'{"CMD": "PONG"}') == [{"CMD": "PONG"}]
        assert capture.messages == [{"CMD": "PONG"}]

    def test_feed_reassembles_a_frame_split_across_reads(self, capture):
        """Partial frames are carried over instead of being dropped."""
        assert capture.feed(b'{"CMD": "PO') == []
        assert capture.messages == []
        assert capture.feed(b'NG"}') == [{"CMD": "PONG"}]
        assert capture.messages == [{"CMD": "PONG"}]

    def test_feed_reassembles_a_byte_at_a_time_frame(self, capture):
        """The extreme split still yields exactly one message."""
        payload = b'{"CMD": "DOOR_STATUS", "door_status": "DOOR_RISING"}'
        parsed: list[dict] = []
        for index in range(len(payload)):
            parsed.extend(capture.feed(payload[index : index + 1]))
        assert parsed == [{"CMD": "DOOR_STATUS", "door_status": "DOOR_RISING"}]

    def test_feed_is_string_aware_about_braces(self, capture):
        """A brace inside a JSON string does not end the frame (the C5 defect)."""
        assert capture.feed(b'{"CMD": "a}b"}') == [{"CMD": "a}b"}]

    def test_feed_splits_multiple_frames_in_one_read(self, capture):
        """Back-to-back frames in a single read all come out."""
        assert capture.feed(b'{"CMD": "A"}{"CMD": "B"}') == [{"CMD": "A"}, {"CMD": "B"}]


class TestWireCaptureQueries:
    """The lookup helpers the integration tests assert through."""

    def test_find_message_returns_the_first_match(self, capture):
        """find_message matches on CMD and returns the earliest one."""
        capture.feed(b'{"CMD": "A", "n": 1}{"CMD": "A", "n": 2}')
        assert capture.find_message("A") == {"CMD": "A", "n": 1}

    def test_find_message_returns_none_when_absent(self, capture):
        """A CMD that never arrived is None, not an error."""
        capture.feed(b'{"CMD": "A"}')
        assert capture.find_message("B") is None

    def test_find_messages_returns_every_match(self, capture):
        """find_messages collects all frames with that CMD."""
        capture.feed(b'{"CMD": "A", "n": 1}{"CMD": "B"}{"CMD": "A", "n": 2}')
        assert capture.find_messages("A") == [{"CMD": "A", "n": 1}, {"CMD": "A", "n": 2}]

    def test_status_sequence_ignores_command_responses(self, capture):
        """Only CMD: DOOR_STATUS frames count as broadcasts."""
        payload = (
            f'{{"CMD": "OPEN", "{FIELD_DOOR_STATUS}": "DOOR_RISING"}}'
            f'{{"CMD": "{DOOR_STATUS}", "{FIELD_DOOR_STATUS}": "DOOR_SLOWING"}}'
        )
        capture.feed(payload.encode("ascii"))
        assert capture.get_status_sequence() == ["DOOR_SLOWING"]
