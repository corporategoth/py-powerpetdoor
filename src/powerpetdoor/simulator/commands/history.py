# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Shared history management for CLI and CTL.

Provides a unified History class that encapsulates all history functionality
and can be used by both the interactive CLI and the control client.
"""

import logging
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)


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

    def __init__(self, history_file: Optional[str | Path] = None):
        """Initialize history manager.

        Args:
            history_file: Path to history file, "none" to disable file storage,
                         or None for in-memory only.
        """
        self._history: Any = None
        self._prompt_toolkit_available = False

        try:
            from prompt_toolkit.history import FileHistory, InMemoryHistory

            self._prompt_toolkit_available = True

            if history_file is None or str(history_file).lower() == "none":
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
            logger.debug(f"Error removing last history entry: {e}")
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
            logger.error(f"Error replacing last history entry: {e}")
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

    def clear(self) -> bool:
        """Clear all history entries.

        Clears both in-memory cache and the history file.

        Returns:
            True if successful, False otherwise.
        """
        if not self._history:
            return False

        try:
            # Clear in-memory history
            if hasattr(self._history, "_loaded_strings"):
                self._history._loaded_strings.clear()

            # Truncate the file
            if hasattr(self._history, "filename"):
                with open(self._history.filename, "w"):
                    pass
            return True
        except Exception as e:
            logger.debug(f"Error clearing history: {e}")
            return False

    def format_entries(self, limit: int = 20) -> str:
        """Format history entries for display.

        Args:
            limit: Maximum number of entries to show.

        Returns:
            Formatted string with history entries.
        """
        if not self._history:
            return "History not available (install prompt_toolkit)"

        try:
            entries = self.get_entries()
            if not entries:
                return "No history"

            total = len(entries)
            start_idx = max(0, total - limit)
            shown_entries = entries[start_idx:]
            lines = [f"History ({len(shown_entries)} of {total} commands):"]
            for i, entry in enumerate(shown_entries):
                history_id = start_idx + i + 1  # 1-indexed
                lines.append(f"  {history_id:5d}  {entry}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error reading history: {e}"

    def resolve_recall(self, command_str: str) -> Union[tuple[str, str], tuple[None, str], None]:
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

    def execute_command(self, arg: Optional[str] = None) -> str:
        """Execute the history command with optional argument.

        This handles the 'history' command itself (show/clear history).

        Args:
            arg: Optional argument - 'clear' to clear history,
                 or a number to show last N commands.

        Returns:
            Result message to display.
        """
        if not self._history:
            return "History not available (install prompt_toolkit)"

        if arg and arg.lower() == "clear":
            if self.clear():
                return "History cleared"
            else:
                return "Error clearing history"

        limit = 20
        if arg:
            try:
                limit = int(arg)
                if limit <= 0:
                    return "Number must be positive"
            except ValueError:
                return f"Invalid argument: {arg}. Use 'clear' or a number."

        return self.format_entries(limit)
