# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Shared history management for CLI and CTL.

Provides a unified History class that encapsulates all history functionality
and can be used by both the interactive CLI and the control client.
"""

import logging
import os
from pathlib import Path
from typing import Any

from ...i18n import t
from .base import CommandResult

logger = logging.getLogger(__name__)

#: The one message shown when history is unavailable. Do not duplicate it:
#: separate copies of the wording drift out of sync with each other.
HISTORY_UNAVAILABLE_MESSAGE = (
    "History not available. Install prompt_toolkit for history support:\n"
    "  pip install pypowerpetdoor[interactive]"
)

#: Entries ``history`` shows when no count is given.
DEFAULT_HISTORY_LIMIT = 20


def _create_private_file(path: str | Path) -> None:
    """Create a file with owner-only (0600) permissions.

    Tightens permissions on an existing file as well, so history files never
    remain world-readable regardless of the process umask.
    """
    fd = os.open(str(path), os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(str(path), 0o600)


class History:
    """Manages command history for interactive sessions.

    Encapsulates all history functionality including:
    - History storage (file or in-memory)
    - History recall commands (!!, !n, !-n)
    - History manipulation (remove, replace, clear)
    - History display formatting

    Usage:
        # Create history with file storage
        history = History(history_file="/path/to/history")

        # Create history with in-memory storage (or disabled)
        history = History()  # In-memory
        history = History(history_file="none")  # Disabled

        # Register with command handler if using CLI
        cmd_handler.set_history(history.prompt_toolkit_history)

        # Handle ! recall commands
        result = history.resolve_recall("!!")
        if result is not None:
            resolved_cmd, prefix = result
            if resolved_cmd is None:
                print(prefix)  # Error message
            else:
                # Execute resolved_cmd, show prefix
    """

    def __init__(self, history_file: str | Path | None = None, *, backend: Any = None):
        """Initialize history manager.

        Args:
            history_file: Path to history file, "none" to disable file storage,
                         or None for in-memory only.
            backend: An already-built prompt_toolkit history object to wrap
                instead of creating one. This is how the ``history`` command
                reaches these operations: the command handler is handed the
                raw backend, and wrapping it here keeps clear + formatting
                from being re-implemented inline.
        """
        self._history: Any = backend
        self._prompt_toolkit_available = backend is not None
        if backend is not None:
            return

        try:
            from prompt_toolkit.history import FileHistory, InMemoryHistory

            self._prompt_toolkit_available = True

            if history_file is None or str(history_file).lower() == "none":
                self._history = InMemoryHistory()
            else:
                # Create the file with 0600 before FileHistory touches it so
                # command history is never world-readable
                try:
                    _create_private_file(history_file)
                except OSError as e:
                    # ...and then *fall back*. Handing the same path to
                    # FileHistory after establishing it is unusable made
                    # prompt_toolkit raise inside the running application on
                    # every load and every store, so a one-character typo in
                    # `--history` bought a traceback and a modal "Press ENTER
                    # to continue" the operator had to dismiss, repeatedly,
                    # for the life of the session.
                    logger.warning(
                        t(
                            "simulator.commands.history.could_use_history_file_history",
                            "Could not use history file {history_file}: {e}; history is in-memory for this session",
                            history_file=history_file,
                            e=e,
                        )
                    )
                    self._history = InMemoryHistory()
                else:
                    self._history = FileHistory(str(history_file))
        except ImportError:
            # prompt_toolkit not available
            pass

    @property
    def available(self) -> bool:
        """Check if history is available (prompt_toolkit installed)."""
        return self._prompt_toolkit_available

    @property
    def prompt_toolkit_history(self) -> Any:
        """Get the underlying prompt_toolkit history object.

        Use this when creating a PromptSession or registering with CommandHandler.
        """
        return self._history

    def get_entries(self) -> list[str]:
        """Get all history entries, oldest first.

        Returns:
            List of history entries, or empty list if not available.
        """
        if not self._history:
            return []
        try:
            return list(self._history.get_strings())
        except Exception:
            return []

    def remove_last_entry(self) -> bool:
        """Remove the last (most recent) entry from history.

        Removes from both in-memory cache and the history file.

        Returns:
            True if successful, False otherwise.
        """
        if not self._history:
            return False

        try:
            # Remove from in-memory history
            # Note: _loaded_strings is stored newest-first, so pop(0) removes the most recent
            if hasattr(self._history, "_loaded_strings") and self._history._loaded_strings:
                self._history._loaded_strings.pop(0)

            # Rewrite the history file without the last entry
            if hasattr(self._history, "filename"):
                self._rewrite_history_file()
            return True
        except Exception as e:
            logger.debug(
                t(
                    "simulator.commands.history.error_removing_last_history_entry",
                    "Error removing last history entry: {e}",
                    e=e,
                )
            )
            return False

    def replace_last_entry(self, new_command: str) -> bool:
        """Replace the last (most recent) history entry with a different command.

        Updates both in-memory cache and the history file.

        Args:
            new_command: The command to replace the last entry with.

        Returns:
            True if successful, False otherwise.
        """
        if not self._history:
            return False

        try:
            # Replace in-memory history
            # Note: _loaded_strings is stored newest-first, so [0] is the most recent
            if hasattr(self._history, "_loaded_strings") and self._history._loaded_strings:
                self._history._loaded_strings[0] = new_command

            # Also update the file
            if hasattr(self._history, "filename"):
                self._rewrite_history_file()
            return True
        except Exception as e:
            logger.error(
                t(
                    "simulator.commands.history.error_replacing_last_history_entry",
                    "Error replacing last history entry: {e}",
                    e=e,
                )
            )
            return False

    def _rewrite_history_file(self) -> None:
        """Rewrite the history file from current in-memory entries."""
        import time

        entries = self.get_entries()
        with open(self._history.filename, "w") as f:
            for entry in entries:
                # FileHistory format: timestamp comment, then +line for each line
                f.write(f"# {time.time()}\n")
                for line in entry.split("\n"):
                    f.write(f"+{line}\n")

    def _clear_backend(self) -> None:
        """Clear the in-memory cache and truncate the file.

        Raises whatever the backend raises; :meth:`clear` reports that as
        False and :meth:`execute_command` reports it to the operator with
        the reason attached.
        """
        if hasattr(self._history, "_loaded_strings"):
            self._history._loaded_strings.clear()
        if hasattr(self._history, "filename"):
            with open(self._history.filename, "w"):
                pass

    def clear(self) -> bool:
        """Clear all history entries.

        Clears both in-memory cache and the history file.

        Returns:
            True if successful, False otherwise.
        """
        if not self._history:
            return False

        try:
            self._clear_backend()
            return True
        except Exception as e:
            logger.debug(
                t(
                    "simulator.commands.history.error_clearing_history",
                    "Error clearing history: {e}",
                    e=e,
                )
            )
            return False

    @staticmethod
    def _render(entries: list[str], limit: int) -> str:
        """Render history entries with absolute, 1-indexed IDs."""
        if not entries:
            return "No history"
        total = len(entries)
        start_idx = max(0, total - limit)
        shown_entries = entries[start_idx:]
        lines = [f"History ({len(shown_entries)} of {total} commands):"]
        for i, entry in enumerate(shown_entries):
            lines.append(f"  {start_idx + i + 1:5d}  {entry}")
        return "\n".join(lines)

    def format_entries(self, limit: int = DEFAULT_HISTORY_LIMIT) -> str:
        """Format history entries for display.

        Args:
            limit: Maximum number of entries to show.

        Returns:
            Formatted string with history entries.
        """
        if not self._history:
            return HISTORY_UNAVAILABLE_MESSAGE

        try:
            return self._render(self.get_entries(), limit)
        except Exception as e:
            return f"Error reading history: {e}"

    def resolve_recall(self, command_str: str) -> tuple[str, str] | tuple[None, str] | None:
        """Resolve history recall commands like !!, !n, !-n.

        Args:
            command_str: The command starting with !

        Returns:
            - (resolved_command, prefix_message) on success
            - (None, error_message) on error
            - None if this isn't a history recall pattern
        """
        if not self._history:
            return None

        if not command_str.startswith("!"):
            return None

        rest = command_str[1:]  # Remove leading !

        # Load history entries
        try:
            entries = self.get_entries()
        except Exception as e:
            return None, f"Error loading history: {e}"

        # The current command was already added to history, exclude it
        if entries and entries[-1] == command_str:
            entries = entries[:-1]

        if not entries:
            return None, "No history"

        # !! - repeat last command
        if rest == "!":
            return entries[-1], f"{command_str} -> {entries[-1]}"

        # !-n - run nth-to-last command
        if rest.startswith("-"):
            try:
                n = int(rest[1:])
                if n <= 0:
                    return None, "!-n requires a positive number"
                if n > len(entries):
                    return None, f"Only {len(entries)} commands in history"
                cmd = entries[-n]
                return cmd, f"{command_str} -> {cmd}"
            except ValueError:
                return None  # Not a history recall pattern

        # !n - run command at absolute history position n (1-indexed)
        try:
            n = int(rest)
            if n <= 0:
                return None, "!n requires a positive number"
            if n > len(entries):
                return None, f"Only {len(entries)} commands in history"
            cmd = entries[n - 1]
            return cmd, f"{command_str} -> {cmd}"
        except ValueError:
            return None  # Not a history recall pattern

    def execute_command(self, arg: str | None = None) -> CommandResult:
        """Execute the history command with optional argument.

        This is the single implementation of the ``history`` command;
        ``InfoCommandsMixin.history`` wraps its backend and delegates here.

        Args:
            arg: Optional argument - 'clear' to clear history,
                 or a number to show last N commands.

        Returns:
            The command result to display.
        """
        if not self._history:
            return CommandResult(False, HISTORY_UNAVAILABLE_MESSAGE)

        if arg and arg.lower() == "clear":
            try:
                self._clear_backend()
            except Exception as e:
                return CommandResult(
                    False,
                    t(
                        "simulator.commands.history.error_clearing_history",
                        "Error clearing history: {e}",
                        e=e,
                    ),
                )
            return CommandResult(
                True, t("simulator.commands.history.history_cleared", "History cleared")
            )

        limit = DEFAULT_HISTORY_LIMIT
        if arg:
            try:
                limit = int(arg)
            except ValueError:
                return CommandResult(
                    False,
                    t(
                        "simulator.commands.history.invalid_argument_use_clear_number",
                        "Invalid argument: {arg}. Use 'clear' or a number.",
                        arg=arg,
                    ),
                )
            if limit <= 0:
                return CommandResult(
                    False,
                    t("simulator.commands.history.number_must_positive", "Number must be positive"),
                )

        try:
            # get_strings() returns oldest first, which is what we want for indexing
            entries = list(self._history.get_strings())
        except Exception as e:
            return CommandResult(
                False,
                t(
                    "simulator.commands.history.error_reading_history",
                    "Error reading history: {e}",
                    e=e,
                ),
            )
        return CommandResult(True, self._render(entries, limit))
