# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator prompt_common module (sanitization, completion, gating)."""

from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

import pytest

from powerpetdoor.simulator import prompt_common
from powerpetdoor.simulator.commands.base import get_command_registry
from powerpetdoor.simulator.commands.history import History
from powerpetdoor.simulator.prompt_common import (
    PROMPT_TOOLKIT_AVAILABLE,
    InputLine,
    InteractiveSession,
    escape_message,
    get_aliases,
    get_commands,
    init_command_sets,
    sanitize_text,
    unescape_message,
    use_prompt_toolkit,
)

requires_prompt_toolkit = pytest.mark.skipif(
    not PROMPT_TOOLKIT_AVAILABLE, reason="prompt_toolkit not installed"
)


@pytest.fixture
def pt_pipe(monkeypatch):
    """A prompt_toolkit pipe-input app session for driving real PromptSessions.

    Also forces use_prompt_toolkit() to True so InteractiveSession builds a
    real PromptSession despite the non-TTY test stdin.
    """
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: True)
    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            yield pipe_input


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
        init_command_sets()
        assert "weekdays" in prompt_common._OPTIONS
        assert "all" in prompt_common._OPTIONS
        assert "mon" in prompt_common._OPTIONS


# ============================================================================
# Command sets for highlighting (init_command_sets and accessors)
# ============================================================================


class TestCommandSets:
    def test_get_commands_contains_known_commands(self):
        commands = get_commands()
        assert {"schedule", "broadcast", "status"} <= commands

    def test_get_aliases_contains_known_aliases(self):
        assert "bc" in get_aliases()

    def test_init_command_sets_is_idempotent(self):
        init_command_sets()
        before = set(prompt_common._COMMANDS)
        init_command_sets()  # early-return path: sets must be unchanged
        assert prompt_common._COMMANDS == before

    def test_nested_subcommand_options_collected_recursively(self, monkeypatch):
        """Options and names of sub-subcommands are collected (recursion)."""
        inner = SimpleNamespace(
            name="inner",
            aliases=["in2"],
            args=[SimpleNamespace(arg_type="bool_toggle", choices=None)],
            subcommands=None,
        )
        outer = SimpleNamespace(name="outer", aliases=[], args=[], subcommands={"inner": inner})
        cmd = SimpleNamespace(name="fakecmd", aliases=["fk"], args=[], subcommands={"outer": outer})
        monkeypatch.setitem(get_command_registry(), "fakecmd", cmd)
        # Force re-collection into fresh (monkeypatch-restored) sets
        monkeypatch.setattr(prompt_common, "_COMMANDS", set())
        monkeypatch.setattr(prompt_common, "_ALIASES", set())
        monkeypatch.setattr(prompt_common, "_SUBCOMMANDS", set())
        monkeypatch.setattr(prompt_common, "_OPTIONS", set())
        init_command_sets()
        assert "fakecmd" in prompt_common._COMMANDS
        assert "fk" in prompt_common._ALIASES
        assert {"outer", "inner", "in2"} <= prompt_common._SUBCOMMANDS
        # on/off come from the nested subcommand's bool_toggle arg - proof
        # that the recursive descent visited it
        assert {"on", "off"} <= prompt_common._OPTIONS


# ============================================================================
# Syntax highlighting (SimulatorLexer)
# ============================================================================


@requires_prompt_toolkit
class TestSimulatorLexer:
    def _lex(self, text: str):
        from prompt_toolkit.document import Document

        from powerpetdoor.simulator.prompt_common import SimulatorLexer

        return SimulatorLexer().lex_document(Document(text))(0)

    def test_full_command_chain_token_classes(self):
        tokens = self._lex("schedule add inside 6:00-22:00 weekdays")
        assert tokens == [
            ("class:command", "schedule"),
            ("", " "),
            ("class:subcommand", "add"),
            ("", " "),
            ("class:option", "inside"),
            ("", " "),
            ("class:number", "6:00-22:00"),
            ("", " "),
            ("class:option", "weekdays"),
        ]

    def test_alias_first_word(self):
        tokens = self._lex("bc hello")
        assert tokens == [("class:alias", "bc"), ("", " "), ("", "hello")]

    def test_unknown_command_plain_with_number_arg(self):
        tokens = self._lex("frobnicate 42")
        assert tokens == [("", "frobnicate"), ("", " "), ("class:number", "42")]

    def test_help_highlighted_as_subcommand(self):
        tokens = self._lex("schedule help")
        assert tokens == [("class:command", "schedule"), ("", " "), ("class:subcommand", "help")]

    def test_question_mark_help_highlighted(self):
        tokens = self._lex("power ?")
        assert tokens == [("class:command", "power"), ("", " "), ("class:subcommand", "?")]

    def test_invalid_subcommand_plain(self):
        tokens = self._lex("schedule bogus")
        assert tokens == [("class:command", "schedule"), ("", " "), ("", "bogus")]

    def test_help_after_subcommand(self):
        tokens = self._lex("schedule add help")
        assert tokens == [
            ("class:command", "schedule"),
            ("", " "),
            ("class:subcommand", "add"),
            ("", " "),
            ("class:subcommand", "help"),
        ]

    def test_help_for_args_only_command(self):
        """help is valid after a command that has args but no subcommands."""
        tokens = self._lex("holdtime help")
        assert tokens == [("class:command", "holdtime"), ("", " "), ("class:subcommand", "help")]

    def test_bool_option_highlighted(self):
        tokens = self._lex("power on")
        assert tokens == [("class:command", "power"), ("", " "), ("class:option", "on")]

    def test_multiple_spaces_preserved_as_gap_token(self):
        tokens = self._lex("power  on")
        assert tokens == [("class:command", "power"), ("", "  "), ("class:option", "on")]

    def test_trailing_whitespace_token(self):
        tokens = self._lex("power ")
        assert tokens == [("class:command", "power"), ("", " ")]

    def test_empty_line_yields_no_tokens(self):
        assert self._lex("") == []

    def test_get_current_command_info_empty_words(self):
        from powerpetdoor.simulator.prompt_common import SimulatorLexer

        assert SimulatorLexer()._get_current_command_info([]) == (None, 0)

    def test_valid_subcommands_at_depth_zero_is_empty(self):
        from powerpetdoor.simulator.prompt_common import SimulatorLexer

        assert SimulatorLexer()._get_valid_subcommands_at_depth(["schedule"], 0) == set()

    def test_valid_subcommands_unknown_command_is_empty(self):
        from powerpetdoor.simulator.prompt_common import SimulatorLexer

        assert SimulatorLexer()._get_valid_subcommands_at_depth(["nosuch", "x"], 1) == set()

    def test_valid_subcommands_traversal_stops_at_unknown_word(self):
        """An unknown middle word stops the descent at the last valid level."""
        from powerpetdoor.simulator.prompt_common import SimulatorLexer

        result = SimulatorLexer()._get_valid_subcommands_at_depth(["schedule", "bogus", "x"], 2)
        assert {"add", "delete", "help", "?"} <= result

    def test_valid_subcommands_depth_beyond_words_stops(self):
        """A depth larger than the word count stops at the last real word."""
        from powerpetdoor.simulator.prompt_common import SimulatorLexer

        result = SimulatorLexer()._get_valid_subcommands_at_depth(["schedule", "add"], 5)
        # 'add' has args but no subcommands: only the help pseudo-subcommands
        assert result == {"help", "?"}

    def test_valid_subcommands_none_for_plain_command(self):
        """A command with neither subcommands nor args offers nothing, not
        even help."""
        from powerpetdoor.simulator.prompt_common import SimulatorLexer

        assert SimulatorLexer()._get_valid_subcommands_at_depth(["clear"], 1) == set()

    def test_registry_key_mismatch_renders_plain(self, monkeypatch):
        """A subcommand registered under a key that is not its own name or
        alias is counted in the command path but not highlighted."""
        # Populate the (cached) highlight sets from the real registry first,
        # so the fake entry below is never collected into them
        init_command_sets()
        weird_sub = SimpleNamespace(
            name="different", aliases=[], args=[], subcommands=None, description=""
        )
        cmd = SimpleNamespace(name="fakecmd", aliases=[], args=[], subcommands={"weird": weird_sub})
        monkeypatch.setitem(get_command_registry(), "fakecmd", cmd)
        tokens = self._lex("fakecmd weird")
        # fakecmd is not in the (cached) highlight sets; weird is in the path
        # but only 'different' would be a valid subcommand name
        assert tokens == [("", "fakecmd"), ("", " "), ("", "weird")]


# ============================================================================
# Tab completion internals (SimulatorCompleter)
# ============================================================================


@requires_prompt_toolkit
class TestCompleterInternals:
    def _completer(self):
        from powerpetdoor.simulator.prompt_common import SimulatorCompleter

        return SimulatorCompleter()

    def _completions(self, text: str) -> list[str]:
        from prompt_toolkit.document import Document

        return [c.text for c in self._completer().get_completions(Document(text, len(text)), None)]

    def test_empty_input_offers_all_commands_and_aliases(self):
        result = self._completions("")
        assert {"schedule", "broadcast", "status", "bc"} <= set(result)

    def test_prefix_filters_command_names(self):
        result = self._completions("sch")
        assert "schedule" in result
        assert all(name.startswith("sch") for name in result)

    def test_alias_completion_meta_names_target(self):
        from prompt_toolkit.document import Document

        completions = {
            c.text: c for c in self._completer().get_completions(Document("bc", 2), None)
        }
        assert "bc" in completions
        assert "Alias for broadcast" in completions["bc"].display_meta_text

    def test_unknown_command_with_args_offers_nothing(self):
        assert self._completions("bogus xyz ") == []

    def test_command_without_args_or_subcommands_offers_nothing(self):
        assert self._completions("clear ") == []

    def test_history_string_arg_choices_offered(self):
        # 'clear' from the string arg's choices, plus the help pseudo-subcommand
        assert self._completions("history ") == ["clear", "help"]

    def test_run_uses_prefix_aware_script_completer(self):
        assert "basic_cycle" in self._completions("run ")

    async def test_timezone_uses_zero_arg_completer(self):
        from powerpetdoor.tz_utils import async_init_timezone_cache

        await async_init_timezone_cache()
        assert "UTC" in self._completions("timezone ")

    def test_traverse_empty_words(self):
        assert self._completer()._traverse_to_current_info([]) == (None, 0)

    def test_help_completions_without_info(self):
        assert self._completer()._get_help_completions(None) == []

    def test_help_completions_for_plain_command(self):
        registry = get_command_registry()
        assert self._completer()._get_help_completions(registry["clear"]) == []

    def test_arg_completer_exception_yields_no_options(self):
        def exploding_completer(prefix):
            raise RuntimeError("completer broke")

        info = SimpleNamespace(
            args=[SimpleNamespace(arg_type="string", choices=None, completer=exploding_completer)],
            subcommands=None,
        )
        assert self._completer()._get_arg_options_for_info(info, "", 0) == []

    def test_duplicate_subcommand_alias_deduplicated(self):
        info = SimpleNamespace(
            subcommands={"a": SimpleNamespace(name="a", aliases=["x", "x"], description="d")}
        )
        result = self._completer()._get_subcommands_for_info(info)
        assert result == [("a", "d"), ("x", "Alias for a")]

    def test_duplicate_command_alias_deduplicated(self, monkeypatch):
        cmd = SimpleNamespace(
            name="fakecmd", aliases=["dup", "dup"], description="d", args=[], subcommands=None
        )
        monkeypatch.setitem(get_command_registry(), "fakecmd", cmd)
        commands = dict(self._completer()._get_commands())
        assert commands["fakecmd"] == "d"
        assert commands["dup"] == "Alias for fakecmd"


# ============================================================================
# InteractiveSession: prompt_async and input_loop (real PromptSession)
# ============================================================================


@requires_prompt_toolkit
class TestPromptAsync:
    async def test_returns_stripped_line(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        pt_pipe.send_text("  status  \r")
        assert await asyncio.wait_for(session.prompt_async(), 5) == "status"

    async def test_empty_line_returns_empty_string(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        pt_pipe.send_text("\r")
        assert await asyncio.wait_for(session.prompt_async(), 5) == ""

    async def test_uses_get_prompt_callable(self, pt_pipe):
        session = InteractiveSession.create(
            host="h", port=1, history_file="none", is_connected=lambda: True
        )
        pt_pipe.send_text("power\r")
        assert await asyncio.wait_for(session.prompt_async(), 5) == "power"

    async def test_keyboard_interrupt_returns_empty_string(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        pt_pipe.send_text("\x03")  # Ctrl-C
        assert await asyncio.wait_for(session.prompt_async(), 5) == ""

    async def test_eof_returns_none(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        pt_pipe.close()
        assert await asyncio.wait_for(session.prompt_async(), 5) is None

    async def test_without_session_returns_none(self):
        # Non-TTY stdin: no PromptSession was created
        session = InteractiveSession(history_file="none")
        assert session.available is False
        assert await session.prompt_async() is None


@requires_prompt_toolkit
class TestInputLoop:
    async def test_yields_lines_then_stops_on_eof(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        pt_pipe.send_text("status\rpower\r")
        pt_pipe.close()
        lines = [line async for line in session.input_loop()]
        assert [(line.resolved, line.was_history_recall) for line in lines] == [
            ("status", False),
            ("power", False),
        ]

    async def test_stop_check_stops_before_prompting(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        lines = [line async for line in session.input_loop(stop_check=lambda: True)]
        assert lines == []

    async def test_empty_lines_are_skipped(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        pt_pipe.send_text("\rstatus\r")
        pt_pipe.close()
        lines = [line.resolved async for line in session.input_loop()]
        assert lines == ["status"]

    async def test_recall_resolves_previous_command(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        pt_pipe.send_text("status\r!1\r")
        pt_pipe.close()
        lines = [line async for line in session.input_loop()]
        assert [(line.original, line.resolved, line.was_history_recall) for line in lines] == [
            ("status", "status", False),
            ("!1", "status", True),
        ]

    async def test_recall_error_printed_and_loop_continues(self, pt_pipe, capsys):
        session = InteractiveSession(history_file="none")
        # "!99" is itself added to history and excluded from recall - with
        # nothing else in history the recall fails with "No history"
        pt_pipe.send_text("!99\rstatus\r")
        pt_pipe.close()
        lines = [line.resolved async for line in session.input_loop()]
        assert lines == ["status"]
        assert ">>> No history" in capsys.readouterr().out

    async def test_keyboard_interrupt_at_yield_continues_loop(self, pt_pipe):
        """A KeyboardInterrupt delivered while suspended at the yield point
        (Ctrl-C during command execution) continues the loop."""
        session = InteractiveSession(history_file="none")
        pt_pipe.send_text("status\r")
        gen = session.input_loop()
        first = await asyncio.wait_for(gen.__anext__(), 5)
        assert first.resolved == "status"
        pt_pipe.send_text("power\r")
        nxt = await asyncio.wait_for(gen.athrow(KeyboardInterrupt), 5)
        assert nxt.resolved == "power"
        await gen.aclose()

    async def test_eof_error_at_yield_stops_loop(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        pt_pipe.send_text("status\r")
        gen = session.input_loop()
        await asyncio.wait_for(gen.__anext__(), 5)
        with pytest.raises(StopAsyncIteration):
            await gen.athrow(EOFError)


# ============================================================================
# InteractiveSession: history recall / result handling / formatting
# ============================================================================


@requires_prompt_toolkit
class TestResolveHistoryRecall:
    def _session_with_history(self, pt_pipe, entries=("status",)):
        session = InteractiveSession(history_file="none")
        for entry in entries:
            session.history.prompt_toolkit_history.append_string(entry)
        return session

    def test_plain_line_passes_through(self, pt_pipe):
        session = self._session_with_history(pt_pipe)
        assert session.resolve_history_recall("status") == ("status", False, None)

    def test_non_recall_bang_passes_through(self, pt_pipe):
        session = self._session_with_history(pt_pipe)
        assert session.resolve_history_recall("!abc") == ("!abc", False, None)

    def test_bang_bang_recalls_last(self, pt_pipe):
        session = self._session_with_history(pt_pipe)
        assert session.resolve_history_recall("!!") == ("status", True, None)

    def test_bang_n_recalls_absolute(self, pt_pipe):
        session = self._session_with_history(pt_pipe)
        assert session.resolve_history_recall("!1") == ("status", True, None)

    def test_bang_minus_n_recalls_relative(self, pt_pipe):
        session = self._session_with_history(pt_pipe)
        assert session.resolve_history_recall("!-1") == ("status", True, None)

    def test_out_of_range_reports_error(self, pt_pipe):
        session = self._session_with_history(pt_pipe)
        assert session.resolve_history_recall("!99") == (
            "!99",
            False,
            "Only 1 commands in history",
        )

    def test_zero_reports_error(self, pt_pipe):
        session = self._session_with_history(pt_pipe)
        assert session.resolve_history_recall("!0") == (
            "!0",
            False,
            "!n requires a positive number",
        )

    def test_empty_history_reports_error(self, pt_pipe):
        session = InteractiveSession(history_file="none")
        assert session.resolve_history_recall("!!") == ("!!", False, "No history")

    def test_without_history_passes_through(self):
        # Non-TTY session: no history at all
        session = InteractiveSession(history_file="none")
        assert session.history is None
        assert session.resolve_history_recall("!!") == ("!!", False, None)


@requires_prompt_toolkit
class TestHandleResult:
    def _session(self, pt_pipe, entries):
        session = InteractiveSession(history_file="none")
        for entry in entries:
            session.history.prompt_toolkit_history.append_string(entry)
        return session

    def test_successful_recall_stores_resolved_command(self, pt_pipe):
        session = self._session(pt_pipe, ["status", "!1"])
        session.handle_result(InputLine("!1", "status", True), True)
        assert session.history.get_entries() == ["status", "status"]

    def test_failed_recall_removed_from_history(self, pt_pipe):
        session = self._session(pt_pipe, ["status", "!1"])
        session.handle_result(InputLine("!1", "status", True), False)
        assert session.history.get_entries() == ["status"]

    def test_failed_command_removed_from_history(self, pt_pipe):
        session = self._session(pt_pipe, ["status", "bogus"])
        session.handle_result(InputLine("bogus", "bogus", False), False)
        assert session.history.get_entries() == ["status"]

    def test_alias_replaced_with_canonical_name(self, pt_pipe):
        session = self._session(pt_pipe, ["bc"])
        session.handle_result(InputLine("bc", "bc", False), True)
        assert session.history.get_entries() == ["broadcast"]

    def test_canonical_command_left_untouched(self, pt_pipe):
        session = self._session(pt_pipe, ["status"])
        session.handle_result(InputLine("status", "status", False), True)
        assert session.history.get_entries() == ["status"]

    def test_without_history_is_noop(self):
        session = InteractiveSession(history_file="none")
        assert session.history is None
        session.handle_result(InputLine("status", "status", False), True)  # no raise


class TestFormatOutput:
    def test_normal_output_prefixed(self):
        session = InteractiveSession(history_file="none")
        line = InputLine("status", "status", False)
        assert session.format_output(line, "Door: CLOSED") == ">>> Door: CLOSED"

    def test_recall_output_shows_resolution(self):
        session = InteractiveSession(history_file="none")
        line = InputLine("!1", "status", True)
        assert session.format_output(line, "Door: CLOSED") == (">>> !1 -> status\n>>> Door: CLOSED")


# ============================================================================
# InteractiveSession: prompt formatting and invalidation
# ============================================================================


@requires_prompt_toolkit
class TestPromptFormatting:
    def test_create_prompt_reflects_connection_state(self):
        connected = {"value": True}
        session = InteractiveSession.create(
            host="h", port=9, history_file="none", is_connected=lambda: connected["value"]
        )
        assert list(session._get_prompt()) == [("class:prompt.connected", "h:9> ")]
        connected["value"] = False
        assert list(session._get_prompt()) == [("class:prompt.disconnected", "h:9> ")]

    def test_invalidate_noop_without_prompt_session(self):
        session = InteractiveSession(history_file="none")
        assert session.available is False
        session.invalidate()  # must not raise

    def test_invalidate_updates_message_from_get_prompt(self, pt_pipe):
        session = InteractiveSession.create(
            host="h", port=9, history_file="none", is_connected=lambda: False
        )
        session.invalidate()
        assert list(session._session.message) == [("class:prompt.disconnected", "h:9> ")]

    def test_invalidate_without_get_prompt_keeps_message(self, pt_pipe):
        session = InteractiveSession(history_file="none", prompt_text="x> ")
        before = session._session.message
        session.invalidate()
        assert session._session.message is before


# ============================================================================
# Degraded mode: prompt_toolkit not installed
# ============================================================================


class TestWithoutPromptToolkit:
    async def test_module_degrades_gracefully(self):
        """With prompt_toolkit unimportable the module must still load and
        every entry point must degrade instead of raising."""
        import importlib

        real = sys.modules.get("prompt_toolkit.completion")
        # A None entry makes 'from prompt_toolkit.completion import ...' raise
        sys.modules["prompt_toolkit.completion"] = None
        try:
            importlib.reload(prompt_common)
            assert prompt_common.PROMPT_TOOLKIT_AVAILABLE is False
            assert prompt_common.use_prompt_toolkit() is False
            assert prompt_common.SIMULATOR_STYLE is None

            session = prompt_common.InteractiveSession.create(
                host="h", port=1, history_file="none", is_connected=lambda: True
            )
            assert session.available is False
            assert session.history is None
            # No prompt-coloring closure is built without prompt_toolkit
            assert session._get_prompt is None
            # prompt_async degrades to EOF; invalidate is a no-op
            assert await session.prompt_async() is None
            session.invalidate()
        finally:
            if real is not None:
                sys.modules["prompt_toolkit.completion"] = real
            else:
                del sys.modules["prompt_toolkit.completion"]
            importlib.reload(prompt_common)
        assert prompt_common.PROMPT_TOOLKIT_AVAILABLE is True
