# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator prompt_common module (sanitization, completion, gating)."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import pytest

from powerpetdoor.simulator.commands.history import History
from powerpetdoor.simulator.prompt_common import (
    PROMPT_TOOLKIT_AVAILABLE,
    InteractiveSession,
    escape_message,
    init_command_sets,
    sanitize_text,
    unescape_message,
    use_prompt_toolkit,
)

requires_prompt_toolkit = pytest.mark.skipif(
    not PROMPT_TOOLKIT_AVAILABLE, reason="prompt_toolkit not installed"
)


# ============================================================================
# Terminal output sanitization (security: ANSI/control-char injection)
# ============================================================================


class TestSanitizeText:
    """Tests for sanitize_text - untrusted data must not reach a terminal raw."""

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

    def test_preserves_newline_and_tab(self):
        """Plain whitespace formatting survives sanitization."""
        assert sanitize_text("line1\nline2\tend") == "line1\nline2\tend"

    def test_plain_text_unchanged(self):
        assert sanitize_text("Battery: 42%") == "Battery: 42%"

    def test_null_byte(self):
        result = sanitize_text("a\x00b")
        assert "\x00" not in result
        assert "\\x00" in result


class TestEscapeUnescape:
    """Tests for control-protocol escaping - must be exactly reversible."""

    def test_newline_round_trip(self):
        original = "line1\nline2"
        assert unescape_message(escape_message(original)) == original

    def test_literal_backslash_n_round_trip(self):
        """A literal backslash followed by 'n' must NOT become a newline.

        This is the unescape-order bug: unescaping \\n before \\\\ corrupted
        e.g. Windows-style paths like scripts\\new.yaml.
        """
        original = "scripts\\new.yaml"
        assert unescape_message(escape_message(original)) == original

    def test_backslash_and_newline_mix_round_trip(self):
        original = "a\\\nb\\nc"
        assert unescape_message(escape_message(original)) == original

    def test_trailing_backslash_round_trip(self):
        original = "ends with backslash\\"
        assert unescape_message(escape_message(original)) == original

    def test_escaped_form_is_single_line(self):
        """Escaped messages never contain a raw newline (no line forgery)."""
        assert "\n" not in escape_message("multi\nline\nmessage")

    def test_unescape_direct(self):
        """Unescape a wire-format message exactly as the daemon produces it."""
        # Wire form of literal backslash-n: doubled backslash then n
        assert unescape_message("scripts\\\\new.yaml") == "scripts\\new.yaml"
        # Wire form of a newline: single backslash then n
        assert unescape_message("line1\\nline2") == "line1\nline2"


# ============================================================================
# prompt_toolkit gating (non-TTY stdin falls back to basic input)
# ============================================================================


class TestUsePromptToolkit:
    """Tests for use_prompt_toolkit - the TTY/dumb-terminal gate."""

    def test_false_when_stdin_not_a_tty(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("piped input"))
        assert use_prompt_toolkit() is False

    def test_false_when_stdin_is_none(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", None)
        assert use_prompt_toolkit() is False

    @requires_prompt_toolkit
    def test_false_when_term_is_dumb(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setenv("TERM", "dumb")
        assert use_prompt_toolkit() is False

    @requires_prompt_toolkit
    def test_true_on_real_terminal(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setenv("TERM", "xterm-256color")
        assert use_prompt_toolkit() is True

    def test_false_when_isatty_raises(self, monkeypatch):
        def broken_isatty():
            raise ValueError("closed file")

        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=broken_isatty))
        assert use_prompt_toolkit() is False


class TestInteractiveSessionNonTty:
    """InteractiveSession must not create a PromptSession for non-TTY stdin."""

    def test_session_unavailable_for_piped_stdin(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("status\nexit\n"))
        session = InteractiveSession.create(host="127.0.0.1", port=3000, history_file="none")
        assert session.available is False
        assert session.history is None

    @requires_prompt_toolkit
    def test_session_available_on_tty(self, monkeypatch, tmp_path):
        import os
        import pty

        master, slave = pty.openpty()
        try:
            fake_stdin = os.fdopen(slave, "r", closefd=False)
            monkeypatch.setattr(sys, "stdin", fake_stdin)
            monkeypatch.setenv("TERM", "xterm")
            session = InteractiveSession.create(
                host="127.0.0.1", port=3000, history_file=str(tmp_path / "hist")
            )
            assert session.available is True
            assert session.history is not None
        finally:
            os.close(master)
            os.close(slave)


# ============================================================================
# History file permissions (security: world-readable history)
# ============================================================================


@requires_prompt_toolkit
class TestHistoryFilePermissions:
    """History files must be created owner-only (0600)."""

    def test_new_history_file_is_private(self, tmp_path):
        history_file = tmp_path / "history"
        History(str(history_file))
        assert history_file.exists()
        assert (history_file.stat().st_mode & 0o777) == 0o600

    def test_existing_history_file_is_tightened(self, tmp_path):
        history_file = tmp_path / "history"
        history_file.write_text("# old\n+status\n")
        history_file.chmod(0o644)
        History(str(history_file))
        assert (history_file.stat().st_mode & 0o777) == 0o600
        # Content is preserved
        assert "+status" in history_file.read_text()


# ============================================================================
# Tab completion (argument-position awareness)
# ============================================================================


@requires_prompt_toolkit
class TestCompleterPositionAwareness:
    """The completer must complete the argument at the cursor's position."""

    def _completions(self, text: str) -> list[str]:
        from prompt_toolkit.document import Document

        from powerpetdoor.simulator.prompt_common import SimulatorCompleter

        completer = SimulatorCompleter()
        return [c.text for c in completer.get_completions(Document(text, len(text)), None)]

    def test_first_argument_offers_choices(self):
        result = self._completions("schedule add ")
        assert "inside" in result
        assert "outside" in result
        assert "both" in result

    def test_second_argument_does_not_repeat_first_choices(self):
        """schedule add inside <TAB> is the time argument - no sensor choices."""
        result = self._completions("schedule add inside ")
        assert "inside" not in result
        assert "outside" not in result
        assert "both" not in result

    def test_third_argument_offers_days(self):
        """schedule add inside 6:00-22:00 <TAB> offers the days presets."""
        result = self._completions("schedule add inside 6:00-22:00 ")
        assert "all" in result
        assert "weekdays" in result
        assert "weekends" in result
        assert "mon" in result
        # Not the sensor choices again
        assert "inside" not in result

    def test_days_prefix_filtering(self):
        result = self._completions("schedule add inside 6:00-22:00 week")
        assert set(result) == {"weekdays", "weekends"}

    def test_consumed_bool_toggle_offers_nothing(self):
        """power on <TAB> - the single argument is consumed; nothing to offer."""
        assert self._completions("power on ") == []

    def test_no_subcommands_after_argument_consumed(self):
        """Once an argument is consumed, subcommands/help are no longer offered."""
        result = self._completions("power on ")
        assert "toggle" not in result
        assert "help" not in result

    def test_run_second_argument_offers_wait(self):
        result = self._completions("run basic_cycle ")
        assert result == ["wait"]

    def test_first_position_still_offers_subcommands_and_options(self):
        result = self._completions("power ")
        assert "toggle" in result
        assert "on" in result
        assert "off" in result
        assert "help" in result

    def test_days_presets_in_highlight_options(self):
        """The lexer's option set includes the days vocabulary."""
        from powerpetdoor.simulator import prompt_common

        init_command_sets()
        assert "weekdays" in prompt_common._OPTIONS
        assert "all" in prompt_common._OPTIONS
        assert "mon" in prompt_common._OPTIONS
