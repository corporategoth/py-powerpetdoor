# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for powerpetdoor.sanitize - the shared terminal-safety helper.

This is the single implementation used by the client library, the simulator
protocol and the interactive front end; untrusted data must not reach a
terminal (or a log record read on one) raw.
"""

import asyncio
import logging

import pytest

from powerpetdoor.sanitize import sanitize_text


class TestSanitizeText:
    """Control characters are replaced by their visible \\xNN escape."""

    def test_escapes_esc_character(self):
        """ESC (0x1b) must be neutralized so ANSI sequences cannot execute."""
        result = sanitize_text("evil \x1b[2J text")
        assert "\x1b" not in result
        assert "\\x1b" in result

    def test_escapes_carriage_return(self):
        """CR can overwrite the current line - must be neutralized."""
        result = sanitize_text("before\rafter")
        assert "\r" not in result
        assert "\\x0d" in result

    def test_escapes_c1_controls(self):
        """C1 range (0x80-0x9f) includes CSI - must be neutralized."""
        result = sanitize_text("a\x9bmb")  # 0x9b is CSI
        assert "\x9b" not in result
        assert "\\x9b" in result

    def test_escapes_del(self):
        result = sanitize_text("a\x7fb")
        assert "\x7f" not in result
        assert "\\x7f" in result

    def test_escapes_bel(self):
        assert sanitize_text("ding\x07") == "ding\\x07"

    def test_preserves_newline_and_tab(self):
        """Plain whitespace formatting survives sanitization."""
        assert sanitize_text("line1\nline2\tend") == "line1\nline2\tend"

    def test_plain_text_unchanged(self):
        assert sanitize_text("Battery: 42%") == "Battery: 42%"

    def test_null_byte(self):
        result = sanitize_text("a\x00b")
        assert "\x00" not in result
        assert "\\x00" in result

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (123, "123"),
            (None, "None"),
            # repr() already renders control characters as \xNN text, so a
            # container stringifies to something with no raw controls left.
            (["a\x1bb"], "['a\\x1bb']"),
            ({"k": "\x9b"}, "{'k': '\\x9b'}"),
        ],
    )
    def test_accepts_non_string_values(self, value, expected):
        """Network-derived fields are not guaranteed to be strings."""
        result = sanitize_text(value)
        assert result == expected
        assert "\x1b" not in result and "\x9b" not in result


class TestLibraryLogSinks:
    """The shipped library must not put raw device bytes into a log record."""

    def test_decode_failure_log_is_sanitized(self, mock_client, caplog):
        """A brace-balanced but invalid frame is logged escaped, at ERROR.

        Two ERROR records: the throttle's running tally and, on the
        occurrences the throttle reports, the sanitized detail.
        """
        client, _, _ = mock_client
        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            client.data_received(b"{\x1b[2J\x1b[1;1H*** PWNED ***\x07}")

        records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(records) == 2
        assert (
            records[0].getMessage()
            == "Failed to decode 1 JSON frame(s) from device (26 bytes) on this connection"
        )
        message = records[1].getMessage()
        assert "\x1b" not in message
        assert "\x07" not in message
        assert "\\x1b[2J\\x1b[1;1H*** PWNED ***\\x07" in message

    def test_rx_debug_log_is_sanitized(self, mock_client, caplog):
        """The RX debug trace carries the same escapes, not raw bytes."""
        client, _, _ = mock_client
        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.client"):
            client.data_received(b'{"CMD": "\x1bXbogus"}')

        messages = [r.getMessage() for r in caplog.records if r.message.startswith("RX < ")]
        assert len(messages) == 1
        assert "\x1b" not in messages[0]
        assert "\\x1b" in messages[0]

    async def test_notification_state_log_is_sanitized(self, mock_client, caplog):
        """A hostile sensorState (sent as a JSON \\u escape) is logged escaped."""
        client, _, _ = mock_client
        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.client"):
            client.data_received(rb'{"SENSOR_INDOOR": "", "sensorState": "on\u001b[2J"}')
            await asyncio.gather(*list(client._tasks))

        messages = [
            r.getMessage() for r in caplog.records if "Notification event" in r.getMessage()
        ]
        assert len(messages) == 1
        assert "\x1b" not in messages[0]
        assert "on\\x1b[2J" in messages[0]

    def test_posix_tz_parse_failure_log_is_sanitized(self, caplog):
        """tz_utils logs a device-supplied POSIX TZ string escaped."""
        from powerpetdoor.tz_utils import parse_posix_tz_string

        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.tz_utils"):
            assert parse_posix_tz_string("\x1b[2J!!!") is None

        assert "\x1b" not in caplog.text
        assert "\\x1b[2J!!!" in caplog.text

    def test_schedule_validation_log_is_sanitized(self, caplog):
        """schedule.py logs a device-supplied time value escaped."""
        from powerpetdoor.schedule import validate_schedule_entry

        entry = {
            "index": 0,
            "daysOfWeek": [1, 1, 1, 1, 1, 1, 1],
            "inside": True,
            "in_start_time": {"hour": "\x1b[2Jevil"},
            "in_end_time": {"hour": 22, "min": 0},
        }
        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.schedule"):
            assert validate_schedule_entry(entry) is False

        assert "\x1b" not in caplog.text
        assert "\\x1b[2Jevil" in caplog.text


class TestTruncation:
    """Peer-chosen text is bounded before it reaches a log record."""

    def test_a_short_value_is_untouched(self):
        assert sanitize_text("{}", limit=200) == "{}"

    def test_a_long_value_is_cut_and_marked(self):
        """A frame is attacker-chosen and can run to the 64 KiB cap."""
        result = sanitize_text("x" * 5000, limit=200)

        assert result == "x" * 200 + "...(truncated)"
        assert len(result) == 214

    def test_truncation_happens_before_escaping(self):
        """The regex must not be run over the whole 64 KiB either."""
        result = sanitize_text("a" * 10 + "\x1b" * 10_000, limit=10)

        assert result == "a" * 10 + "...(truncated)"
        assert "\x1b" not in result

    def test_control_characters_inside_the_kept_prefix_are_still_escaped(self):
        result = sanitize_text("\x1b[2J" + "x" * 500, limit=8)

        assert result.startswith("\\x1b[2Jxxxx")
        assert result.endswith("...(truncated)")

    def test_no_limit_keeps_the_previous_behaviour(self):
        assert sanitize_text("x" * 5000) == "x" * 5000

    def test_the_default_frame_limit_is_exported(self):
        from powerpetdoor.sanitize import MAX_LOGGED_LENGTH

        assert MAX_LOGGED_LENGTH == 200
