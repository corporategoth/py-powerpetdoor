# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for powerpetdoor.sanitize - the shared terminal-safety helper.

This is the single implementation used by the client library, the simulator
protocol and the interactive front end; untrusted data must not reach a
terminal (or a log record read on one) raw.
"""

import contextlib
import io
import json
import logging

import pytest

from powerpetdoor.sanitize import sanitize_field, sanitize_text


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


class TestSurrogatesAreEscaped:
    """An unpaired surrogate is the one class whose *result* is unusable.

    ``"\\ud800"`` is legal JSON, arrives on the wire as six ASCII characters,
    and becomes an unpaired surrogate at ``json.loads``. It cannot be encoded
    to UTF-8, so leaving it unescaped produces exactly what a
    ``logging.FileHandler(encoding="utf-8")`` cannot write: the record is
    dropped from the operator's log file entirely and the payload goes to
    stderr instead.
    """

    @pytest.mark.parametrize(
        ("codepoint", "expected"),
        [
            (0xD7FF, "퟿"),  # last code point below the surrogate block
            (0xD800, "\\ud800"),  # first surrogate
            (0xDFFF, "\\udfff"),  # last surrogate
            (0xE000, ""),  # first code point above it
        ],
        ids=["U+D7FF", "U+D800", "U+DFFF", "U+E000"],
    )
    def test_the_surrogate_block_boundary_is_exact(self, codepoint, expected):
        """Both edges of the surrogate block: escape the block, and only the block.

        Compared through `ascii()` on purpose: a *failing* assertion would
        otherwise put a raw unpaired surrogate into pytest's report, and
        pytest-xdist cannot serialize that back to the controller - the run
        would die with an INTERNALERROR instead of naming this test.
        """
        assert ascii(sanitize_text(chr(codepoint))) == ascii(expected)

    def test_the_escape_is_width_aware(self):
        """``\\x{ord:02x}`` would render U+D800 as ``\\xd800``, which reads as
        ``\\xd8`` followed by ``00``. Above U+00FF the escape is ``\\uNNNN``."""
        assert ascii(sanitize_text("\x1b")) == ascii("\\x1b")
        assert ascii(sanitize_text("\x9f")) == ascii("\\x9f")
        assert ascii(sanitize_text("\ud800")) == ascii("\\ud800")

    def test_a_sanitized_value_always_encodes_to_utf8(self):
        assert sanitize_text("\ud800BAD", 200).encode("utf-8") == b"\\ud800BAD"

    def test_a_surrogate_from_the_wire_produces_a_writable_log_record(self, tmp_path):
        """End to end: pure-ASCII wire bytes, a real UTF-8 file handler.

        This is the shipped `door.py:1494` sink, measured at 200 hostile
        frames -> **0** log lines and 359 KB of stderr. The assertion is that
        the record the handler produces can actually be written; `caplog`
        cannot see this failure mode, because it never encodes anything.
        """
        from powerpetdoor.door import PowerPetDoor

        # Exactly what a peer sends: the six ASCII characters \ud800.
        wire = rb'{"fwInfo": "\ud800SECRETPROBE"}'
        assert wire.decode("ascii") == wire.decode()  # pure ASCII on the wire
        payload = json.loads(wire)["fwInfo"]

        door = PowerPetDoor(host="127.0.0.1", port=3000)
        log_path = tmp_path / "ppd.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        door_logger = logging.getLogger("powerpetdoor.door")
        door_logger.addHandler(handler)
        previous = door_logger.propagate
        door_logger.propagate = False
        # `logging.handleError` writes the failing record to stderr verbatim,
        # raw surrogate and all. Swallowing it keeps a *failing* run readable:
        # pytest-xdist cannot serialize captured output containing one.
        swallowed = io.StringIO()
        try:
            with contextlib.redirect_stderr(swallowed):
                door._on_hw_info_update(payload)
        finally:
            door_logger.propagate = previous
            door_logger.removeHandler(handler)
            handler.close()

        assert ascii(swallowed.getvalue()) == ascii("")
        written = log_path.read_text(encoding="utf-8")
        assert "Ignoring non-mapping hardware info: \\ud800SECRETPROBE" in written


class TestSanitizeFieldEscapesLineFeeds:
    """A newline in a *field value* forges log records.

    `_CONTROL_CHAR_RE` escapes CR but not LF, so a device-supplied field
    interpolated into a log line ends the physical line and everything
    after it is read as a fresh record - with a timestamp, a severity and
    a message the device chose. LF cannot go into `sanitize_text` itself:
    that is also applied to whole formatted records, where a multi-line
    traceback is exactly what should be written.
    """

    FORGERY = "ok\n2025-01-01 00:00:00 [CRITICAL] the door is on fire"

    def test_a_field_value_cannot_start_a_new_line(self):
        result = sanitize_field(self.FORGERY)

        assert "\n" not in result
        assert "\\x0a" in result
        assert result.count("CRITICAL") == 1  # the text survives, inert

    def test_sanitize_text_still_passes_line_feeds_through(self):
        """The formatter legitimately emits multi-line tracebacks."""
        assert "\n" in sanitize_text(self.FORGERY)

    def test_it_is_additive_over_sanitize_text(self):
        """Everything sanitize_text escapes, sanitize_field escapes too."""
        raw = "".join(chr(code) for code in range(0x00, 0xA0)) + "\ud800"

        assert sanitize_field(raw) == sanitize_text(raw).replace("\n", "\\x0a")

    def test_it_keeps_tab_like_sanitize_text_does(self):
        assert sanitize_field("a\tb") == "a\tb"

    def test_it_truncates_before_escaping_like_sanitize_text(self):
        result = sanitize_field("\n" * 100, limit=4)

        assert result == "\\x0a" * 4 + "...(truncated)"


class TestLibraryLogSinks:
    """The shipped library must not put raw device bytes into a log record."""

    def test_decode_failure_log_is_sanitized(self, mock_client, caplog):
        """A brace-balanced but invalid frame is logged escaped, at ERROR."""
        client, _, _ = mock_client
        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            client.data_received(b"{\x1b[2J\x1b[1;1H*** PWNED ***\x07}")

        records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(records) == 1
        message = records[0].getMessage()
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

    def test_the_truncation_boundary_is_exact(self):
        """Assert *at* the limit: `len(value) > limit` must not become `>=`,
        which would mark a value of exactly `limit` as truncated though
        nothing was cut."""
        from powerpetdoor.sanitize import sanitize_text

        assert sanitize_text("x" * 10, 10) == "x" * 10
        assert sanitize_text("x" * 11, 10) == "x" * 10 + "...(truncated)"
        assert sanitize_text("x" * 9, 10) == "x" * 9
