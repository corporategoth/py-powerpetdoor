# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Unit tests for the History class (commands/history.py).

Covers storage backends (file / in-memory / disabled), file permissions,
entry manipulation, display formatting, ! recall resolution, and the
history command implementation.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from powerpetdoor.simulator.commands.history import History, _create_private_file


@pytest.fixture
def hist_file(tmp_path):
    return tmp_path / "history"


@pytest.fixture
def file_history(hist_file):
    """A file-backed History preloaded with three commands."""
    history = History(hist_file)
    for cmd in ("status", "power on", "close"):
        history.prompt_toolkit_history.append_string(cmd)
    return history


@pytest.fixture
def memory_history():
    """An in-memory History preloaded with three commands."""
    history = History()
    for cmd in ("status", "power on", "close"):
        history.prompt_toolkit_history.append_string(cmd)
    return history


@pytest.fixture
def disabled_history(monkeypatch):
    """A History constructed as if prompt_toolkit were not installed."""
    monkeypatch.setitem(sys.modules, "prompt_toolkit.history", None)
    return History()


def _raise(*args, **kwargs):
    raise RuntimeError("boom")


# ============================================================================
# Construction and storage backends
# ============================================================================


class TestConstruction:
    def test_no_file_uses_in_memory(self):
        from prompt_toolkit.history import InMemoryHistory

        history = History()
        assert history.available is True
        assert isinstance(history.prompt_toolkit_history, InMemoryHistory)

    def test_none_string_uses_in_memory(self):
        from prompt_toolkit.history import InMemoryHistory

        history = History("none")
        assert isinstance(history.prompt_toolkit_history, InMemoryHistory)

    def test_none_string_case_insensitive(self):
        from prompt_toolkit.history import InMemoryHistory

        history = History("NONE")
        assert isinstance(history.prompt_toolkit_history, InMemoryHistory)

    def test_file_path_uses_file_history(self, hist_file):
        from prompt_toolkit.history import FileHistory

        history = History(hist_file)
        assert history.available is True
        assert isinstance(history.prompt_toolkit_history, FileHistory)

    def test_history_file_created_owner_only(self, hist_file):
        History(hist_file)
        assert hist_file.exists()
        assert stat.S_IMODE(os.stat(hist_file).st_mode) == 0o600

    def test_existing_file_permissions_tightened(self, hist_file):
        hist_file.write_text("# old\n+status\n")
        os.chmod(hist_file, 0o644)
        History(hist_file)
        assert stat.S_IMODE(os.stat(hist_file).st_mode) == 0o600

    def test_unwritable_path_logs_warning_and_continues(self, tmp_path, caplog):
        bad_path = tmp_path / "missing_dir" / "history"
        with caplog.at_level("WARNING"):
            history = History(bad_path)
        assert f"Could not create history file {bad_path}" in caplog.text
        # History object still usable (FileHistory is lazy)
        assert history.available is True
        assert history.get_entries() == []

    def test_prompt_toolkit_missing_disables_history(self, disabled_history):
        assert disabled_history.available is False
        assert disabled_history.prompt_toolkit_history is None

    def test_create_private_file_direct(self, tmp_path):
        path = tmp_path / "private"
        _create_private_file(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ============================================================================
# Disabled behavior (prompt_toolkit unavailable)
# ============================================================================


class TestDisabledBehavior:
    def test_get_entries_empty(self, disabled_history):
        assert disabled_history.get_entries() == []

    def test_remove_last_entry_false(self, disabled_history):
        assert disabled_history.remove_last_entry() is False

    def test_replace_last_entry_false(self, disabled_history):
        assert disabled_history.replace_last_entry("x") is False

    def test_clear_false(self, disabled_history):
        assert disabled_history.clear() is False

    def test_format_entries_unavailable_message(self, disabled_history):
        assert disabled_history.format_entries() == "History not available (install prompt_toolkit)"

    def test_resolve_recall_none(self, disabled_history):
        assert disabled_history.resolve_recall("!!") is None

    def test_execute_command_unavailable_message(self, disabled_history):
        assert (
            disabled_history.execute_command() == "History not available (install prompt_toolkit)"
        )


# ============================================================================
# get_entries
# ============================================================================


class TestGetEntries:
    def test_entries_oldest_first(self, memory_history):
        assert memory_history.get_entries() == ["status", "power on", "close"]

    def test_empty_history(self):
        assert History().get_entries() == []

    def test_error_reading_returns_empty(self, memory_history, monkeypatch):
        monkeypatch.setattr(memory_history.prompt_toolkit_history, "get_strings", _raise)
        assert memory_history.get_entries() == []


# ============================================================================
# remove_last_entry / replace_last_entry / file rewriting
# ============================================================================


class TestRemoveLastEntry:
    def test_removes_from_memory(self, memory_history):
        assert memory_history.remove_last_entry() is True
        assert memory_history.get_entries() == ["status", "power on"]

    def test_rewrites_file_in_filehistory_format(self, file_history, hist_file):
        assert file_history.remove_last_entry() is True
        assert file_history.get_entries() == ["status", "power on"]

        content = hist_file.read_text()
        assert "+status\n" in content
        assert "+power on\n" in content
        assert "+close" not in content
        # Every non-blank line is either a timestamp comment or a +line
        for line in content.splitlines():
            if line:
                assert line.startswith(("#", "+"))

    def test_removed_entry_gone_after_reload(self, file_history, hist_file):
        file_history.remove_last_entry()
        from prompt_toolkit.history import FileHistory

        reloaded = list(FileHistory(str(hist_file)).load_history_strings())
        # load_history_strings returns newest first
        assert reloaded == ["power on", "status"]

    def test_empty_history_still_returns_true(self):
        # Nothing to remove, but the operation itself does not fail
        assert History().remove_last_entry() is True

    def test_error_returns_false(self, file_history, monkeypatch):
        monkeypatch.setattr(file_history, "_rewrite_history_file", _raise)
        assert file_history.remove_last_entry() is False


class TestReplaceLastEntry:
    def test_replaces_in_memory(self, memory_history):
        assert memory_history.replace_last_entry("open") is True
        assert memory_history.get_entries() == ["status", "power on", "open"]

    def test_replaces_in_file(self, file_history, hist_file):
        assert file_history.replace_last_entry("open") is True
        content = hist_file.read_text()
        assert "+open\n" in content
        assert "+close" not in content

    def test_multiline_entry_round_trip(self, file_history, hist_file):
        file_history.prompt_toolkit_history.append_string("line1\nline2")
        assert file_history.get_entries()[-1] == "line1\nline2"

        # Rewrite the file (via replace of the multi-line entry) and reload
        assert file_history.replace_last_entry("a\nb") is True
        assert file_history.get_entries()[-1] == "a\nb"
        assert "+a\n+b\n" in hist_file.read_text()

        from prompt_toolkit.history import FileHistory

        reloaded = list(FileHistory(str(hist_file)).load_history_strings())
        assert reloaded[0] == "a\nb"

    def test_empty_history_still_returns_true(self):
        assert History().replace_last_entry("x") is True

    def test_error_returns_false(self, file_history, monkeypatch):
        monkeypatch.setattr(file_history, "_rewrite_history_file", _raise)
        assert file_history.replace_last_entry("x") is False


# ============================================================================
# clear
# ============================================================================


class TestClear:
    def test_clears_memory(self, memory_history):
        assert memory_history.clear() is True
        assert memory_history.get_entries() == []

    def test_truncates_file(self, file_history, hist_file):
        assert file_history.clear() is True
        assert file_history.get_entries() == []
        assert hist_file.read_text() == ""

    def test_error_returns_false(self, file_history, tmp_path):
        # Point the file history at a directory - truncating it fails
        file_history.prompt_toolkit_history.filename = str(tmp_path)
        assert file_history.clear() is False

    def test_backend_without_loaded_strings_is_a_noop(self, memory_history):
        """The hasattr guards tolerate custom history backends without the
        private prompt_toolkit attributes (no in-memory cache, no file)."""

        class MinimalBackend:
            def get_strings(self):
                return ["status"]

        memory_history._history = MinimalBackend()
        assert memory_history.clear() is True
        assert memory_history.remove_last_entry() is True
        assert memory_history.replace_last_entry("x") is True
        # Nothing was actually removed - the backend has no mutation support
        assert memory_history.get_entries() == ["status"]


# ============================================================================
# format_entries
# ============================================================================


class TestFormatEntries:
    def test_formats_with_header_and_ids(self, memory_history):
        assert memory_history.format_entries() == (
            "History (3 of 3 commands):\n      1  status\n      2  power on\n      3  close"
        )

    def test_limit_shows_most_recent_with_absolute_ids(self, memory_history):
        assert memory_history.format_entries(limit=2) == (
            "History (2 of 3 commands):\n      2  power on\n      3  close"
        )

    def test_empty_history(self):
        assert History().format_entries() == "No history"

    def test_error_reading(self, memory_history, monkeypatch):
        monkeypatch.setattr(memory_history, "get_entries", _raise)
        assert memory_history.format_entries() == "Error reading history: boom"


# ============================================================================
# resolve_recall
# ============================================================================


class TestResolveRecall:
    def test_not_a_recall_pattern(self, memory_history):
        assert memory_history.resolve_recall("status") is None

    def test_bang_bang_repeats_last(self, memory_history):
        assert memory_history.resolve_recall("!!") == ("close", "!! -> close")

    def test_current_command_excluded(self, memory_history):
        # The recall command itself was already added to history
        memory_history.prompt_toolkit_history.append_string("!!")
        assert memory_history.resolve_recall("!!") == ("close", "!! -> close")

    def test_empty_history(self):
        assert History().resolve_recall("!!") == (None, "No history")

    def test_only_entry_is_current_command(self):
        history = History()
        history.prompt_toolkit_history.append_string("!!")
        assert history.resolve_recall("!!") == (None, "No history")

    def test_relative_recall(self, memory_history):
        assert memory_history.resolve_recall("!-1") == ("close", "!-1 -> close")
        assert memory_history.resolve_recall("!-3") == ("status", "!-3 -> status")

    def test_relative_zero_rejected(self, memory_history):
        assert memory_history.resolve_recall("!-0") == (None, "!-n requires a positive number")

    def test_relative_out_of_range(self, memory_history):
        assert memory_history.resolve_recall("!-4") == (None, "Only 3 commands in history")

    def test_relative_non_numeric_not_a_pattern(self, memory_history):
        assert memory_history.resolve_recall("!-abc") is None

    def test_absolute_recall(self, memory_history):
        assert memory_history.resolve_recall("!1") == ("status", "!1 -> status")
        assert memory_history.resolve_recall("!2") == ("power on", "!2 -> power on")

    def test_absolute_zero_rejected(self, memory_history):
        assert memory_history.resolve_recall("!0") == (None, "!n requires a positive number")

    def test_absolute_out_of_range(self, memory_history):
        assert memory_history.resolve_recall("!4") == (None, "Only 3 commands in history")

    def test_absolute_non_numeric_not_a_pattern(self, memory_history):
        assert memory_history.resolve_recall("!abc") is None

    def test_error_loading_history(self, memory_history, monkeypatch):
        monkeypatch.setattr(memory_history, "get_entries", _raise)
        assert memory_history.resolve_recall("!!") == (None, "Error loading history: boom")


# ============================================================================
# execute_command
# ============================================================================


class TestExecuteCommand:
    def test_no_arg_shows_last_20(self, memory_history):
        result = memory_history.execute_command()
        assert result.startswith("History (3 of 3 commands):")
        assert "      3  close" in result

    def test_numeric_arg_limits_output(self, memory_history):
        result = memory_history.execute_command("1")
        assert result == "History (1 of 3 commands):\n      3  close"

    def test_limit_larger_than_history(self, memory_history):
        result = memory_history.execute_command("99")
        assert result.startswith("History (3 of 3 commands):")

    def test_clear(self, memory_history):
        assert memory_history.execute_command("clear") == "History cleared"
        assert memory_history.get_entries() == []

    def test_clear_case_insensitive(self, memory_history):
        assert memory_history.execute_command("CLEAR") == "History cleared"

    def test_clear_error(self, file_history, tmp_path):
        file_history.prompt_toolkit_history.filename = str(tmp_path)
        assert file_history.execute_command("clear") == "Error clearing history"

    def test_zero_rejected(self, memory_history):
        assert memory_history.execute_command("0") == "Number must be positive"

    def test_negative_rejected(self, memory_history):
        assert memory_history.execute_command("-5") == "Number must be positive"

    def test_invalid_argument(self, memory_history):
        assert (
            memory_history.execute_command("abc")
            == "Invalid argument: abc. Use 'clear' or a number."
        )
