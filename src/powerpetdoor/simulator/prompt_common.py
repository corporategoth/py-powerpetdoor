# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Common prompt_toolkit components shared between cli.py and ctl.py.

This module provides syntax highlighting, tab completion, styling, and
the InteractiveSession class for the simulator command-line interfaces.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .commands.base import get_command_registry, get_canonical_command
from .commands.history import History

# Import CommandHandler to ensure all command modules are loaded and their
# @command/@subcommand decorators populate the registry. This is needed for
# ctl.py which otherwise only imports a subset of command mixins.
from .commands.handler import CommandHandler  # noqa: F401

# Try to import prompt_toolkit for enhanced interactive features
try:
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.styles import Style

    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False

if TYPE_CHECKING:
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.styles import Style

# History file paths
CLI_HISTORY_FILE = Path.home() / ".powerpetdoor_simulator_history"
CTL_HISTORY_FILE = Path.home() / ".powerpetdoor_ctl_history"

# Style for syntax highlighting
SIMULATOR_STYLE = (
    Style.from_dict(
        {
            # Commands
            "command": "#00aa00 bold",  # Green for commands
            "alias": "#00aa00",  # Green (not bold) for aliases
            # Arguments
            "subcommand": "#0088ff",  # Blue for subcommands
            "option": "#ff8800",  # Orange for on/off options
            "number": "#aa00aa",  # Purple for numbers
            # Prompt - connected (white) vs disconnected (gray)
            "prompt.connected": "#ffffff bold",
            "prompt.disconnected": "#888888",
        }
    )
    if PROMPT_TOOLKIT_AVAILABLE
    else None
)

# Command categories for syntax highlighting (populated dynamically)
_COMMANDS: set[str] = set()
_ALIASES: set[str] = set()
_SUBCOMMANDS: set[str] = set()
_OPTIONS: set[str] = set()


def _collect_subcommands_and_options(subcommands: dict) -> None:
    """Recursively collect subcommand names and options from a subcommand registry."""
    for info in subcommands.values():
        _SUBCOMMANDS.add(info.name)
        for alias in info.aliases:
            _SUBCOMMANDS.add(alias)
        # Collect choices from args (any arg type with choices, not just "choice" type)
        for arg in info.args:
            if arg.arg_type == "bool_toggle":
                _OPTIONS.update(["on", "off"])
            elif arg.choices:
                _OPTIONS.update(c.lower() for c in arg.choices)
        # Recurse into nested subcommands
        if info.subcommands:
            _collect_subcommands_and_options(info.subcommands)


def init_command_sets():
    """Initialize command sets for syntax highlighting from the command registry."""
    global _COMMANDS, _ALIASES, _SUBCOMMANDS, _OPTIONS
    if _COMMANDS:
        return  # Already initialized
    for info in get_command_registry().values():
        _COMMANDS.add(info.name)
        for alias in info.aliases:
            _ALIASES.add(alias)
        # Collect options from command args (any arg type with choices)
        for arg in info.args:
            if arg.arg_type == "bool_toggle":
                _OPTIONS.update(["on", "off"])
            elif arg.choices:
                _OPTIONS.update(c.lower() for c in arg.choices)
        # Collect subcommands recursively
        if info.subcommands:
            _collect_subcommands_and_options(info.subcommands)


def get_commands() -> set[str]:
    """Get the set of command names."""
    init_command_sets()
    return _COMMANDS


def get_aliases() -> set[str]:
    """Get the set of command aliases."""
    init_command_sets()
    return _ALIASES


# Only define prompt_toolkit classes when available
if PROMPT_TOOLKIT_AVAILABLE:

    class SimulatorLexer(Lexer):
        """Syntax highlighter for simulator commands."""

        def _get_current_command_info(self, words: list[str]):
            """Traverse command hierarchy and return current command info.

            Returns:
                Tuple of (info, depth) where:
                - info: The CommandInfo at the current position, or None
                - depth: How many words were consumed as commands/subcommands
            """
            if not words:
                return None, 0

            registry = get_command_registry()
            cmd = words[0].lower()
            if cmd not in registry:
                return None, 0

            info = registry[cmd]
            depth = 1

            # Traverse subcommand hierarchy
            for i in range(1, len(words)):
                word = words[i].lower()
                # Check for "help" pseudo-subcommand (valid if command has subcommands or args)
                if word in ("help", "?") and (info.subcommands or info.args):
                    depth = i + 1
                    break  # help is terminal, don't traverse further
                if info.subcommands and word in info.subcommands:
                    # Count this as part of the command path for highlighting
                    # (even if subcommand has no handler, it's still a valid subcommand)
                    info = info.subcommands[word]
                    depth = i + 1
                else:
                    break

            return info, depth

        def _get_valid_subcommands_at_depth(self, words: list[str], depth: int) -> set[str]:
            """Get valid subcommand names at a specific depth in the command hierarchy."""
            if depth == 0:
                return set()

            registry = get_command_registry()
            cmd = words[0].lower()
            if cmd not in registry:
                return set()

            info = registry[cmd]

            # Traverse to the right depth
            for i in range(1, depth):
                if i >= len(words):
                    break
                word = words[i].lower()
                if info.subcommands and word in info.subcommands:
                    info = info.subcommands[word]
                else:
                    break

            # Return subcommand names at this level
            result = set()
            if info.subcommands:
                for sub_info in info.subcommands.values():
                    result.add(sub_info.name)
                    result.update(sub_info.aliases)
            # Add help/? as pseudo-subcommands if command has subcommands or args
            if info.subcommands or info.args:
                result.add("help")
                result.add("?")
            return result

        def lex_document(self, document):
            """Return a lexer function for the document."""
            # Initialize command sets if needed
            init_command_sets()

            def get_line_tokens(line_number):
                line = document.lines[line_number]
                tokens = []
                words = line.split()
                pos = 0

                # Get command context
                info, cmd_depth = self._get_current_command_info(words)

                for i, word in enumerate(words):
                    # Find start position of this word
                    start = line.find(word, pos)
                    # Add any whitespace before
                    if start > pos:
                        tokens.append(("", line[pos:start]))

                    # Determine token style
                    if i == 0:
                        # First word is command
                        if word.lower() in _COMMANDS:
                            tokens.append(("class:command", word))
                        elif word.lower() in _ALIASES:
                            tokens.append(("class:alias", word))
                        else:
                            tokens.append(("", word))
                    elif i < cmd_depth:
                        # This word is part of the command/subcommand path
                        valid_subs = self._get_valid_subcommands_at_depth(words, i)
                        if word.lower() in valid_subs:
                            tokens.append(("class:subcommand", word))
                        else:
                            tokens.append(("", word))
                    else:
                        # Arguments after command path
                        if (
                            word.replace(".", "")
                            .replace("-", "")
                            .replace(":", "")
                            .isdigit()
                        ):
                            tokens.append(("class:number", word))
                        elif word.lower() in _OPTIONS:
                            tokens.append(("class:option", word))
                        else:
                            tokens.append(("", word))

                    pos = start + len(word)

                # Add trailing whitespace
                if pos < len(line):
                    tokens.append(("", line[pos:]))

                return tokens

            return get_line_tokens

    class SimulatorCompleter(Completer):
        """Tab completion for simulator commands."""

        def _get_commands(self) -> list[tuple[str, str]]:
            """Get all unique command names with descriptions."""
            seen = set()
            commands = []
            for info in get_command_registry().values():
                if info.name not in seen:
                    seen.add(info.name)
                    commands.append((info.name, info.description))
                    for alias in info.aliases:
                        if alias not in seen:
                            seen.add(alias)
                            commands.append((alias, f"Alias for {info.name}"))
            return sorted(commands, key=lambda x: x[0])

        def _traverse_to_current_info(self, words: list[str]):
            """Traverse command hierarchy based on words already typed.

            Returns:
                Tuple of (info, depth) where:
                - info: The CommandInfo at the current position, or None
                - depth: How many words were consumed as commands/subcommands
            """
            if not words:
                return None, 0

            registry = get_command_registry()
            cmd = words[0].lower()
            if cmd not in registry:
                return None, 0

            info = registry[cmd]
            depth = 1

            # Traverse subcommand hierarchy
            for i in range(1, len(words)):
                word = words[i].lower()
                if info.subcommands and word in info.subcommands:
                    # Traverse into the subcommand
                    info = info.subcommands[word]
                    depth = i + 1
                else:
                    break

            return info, depth

        def _get_subcommands_for_info(self, info) -> list[tuple[str, str]]:
            """Get subcommands for a CommandInfo with descriptions."""
            if not info or not info.subcommands:
                return []

            # Collect unique subcommands (avoid duplicates from aliases)
            seen = set()
            result = []
            for sub_info in info.subcommands.values():
                if sub_info.name not in seen:
                    seen.add(sub_info.name)
                    result.append((sub_info.name, sub_info.description or ""))
                    # Also add aliases
                    for alias in sub_info.aliases:
                        if alias not in seen:
                            seen.add(alias)
                            result.append((alias, f"Alias for {sub_info.name}"))
            return sorted(result, key=lambda x: x[0])

        def _get_arg_options_for_info(self, info) -> list[tuple[str, str]]:
            """Get argument options for a CommandInfo."""
            if not info or not info.args:
                return []

            # Check the first arg's type
            arg = info.args[0]
            if arg.arg_type == "bool_toggle":
                return [("on", "Enable"), ("off", "Disable")]
            elif arg.arg_type == "choice" and arg.choices:
                return [(c.lower(), c) for c in arg.choices]
            # Also check for choices on string args (like history's "clear")
            elif arg.choices:
                return [(c.lower(), c) for c in arg.choices]
            return []

        def _get_help_completions(self, info) -> list[tuple[str, str]]:
            """Get help pseudo-subcommands if command has subcommands or args."""
            if not info:
                return []
            # Offer help if command has subcommands or args that could use explanation
            if info.subcommands or info.args:
                return [("help", "Show help for this command")]
            return []

        def _get_script_completions(self) -> list[tuple[str, str]]:
            """Get builtin script names for run/file command."""
            try:
                from .scripting import list_builtin_scripts
                return [(name, desc) for name, desc in list_builtin_scripts()]
            except Exception:
                return []

        def get_completions(self, document, complete_event):
            """Generate completions for the current input."""
            text = document.text_before_cursor
            words = text.split()

            # Determine what we're completing
            if not text or text.endswith(" "):
                # Starting a new word
                word_before = ""
                completed_words = words
            else:
                # Completing current word
                word_before = words[-1] if words else ""
                completed_words = words[:-1] if words else []

            if not completed_words:
                # Complete command names
                for cmd, desc in self._get_commands():
                    if cmd.startswith(word_before.lower()):
                        yield Completion(
                            cmd,
                            start_position=-len(word_before),
                            display_meta=desc,
                        )
            else:
                # Traverse to current position in command hierarchy
                info, depth = self._traverse_to_current_info(completed_words)

                # Special case for run/file command
                if completed_words[0].lower() in ("run", "r", "file"):
                    for name, desc in self._get_script_completions():
                        if name.startswith(word_before.lower()):
                            yield Completion(
                                name,
                                start_position=-len(word_before),
                                display_meta=desc,
                            )
                    return

                if info:
                    # Collect all possible completions
                    all_completions = []

                    # Add subcommands
                    all_completions.extend(self._get_subcommands_for_info(info))

                    # Add argument options (on/off, choices, etc.)
                    all_completions.extend(self._get_arg_options_for_info(info))

                    # Add help pseudo-subcommand
                    all_completions.extend(self._get_help_completions(info))

                    # Yield matching completions
                    for name, desc in all_completions:
                        if name.startswith(word_before.lower()):
                            yield Completion(
                                name,
                                start_position=-len(word_before),
                                display_meta=desc,
                            )


@dataclass
class InputLine:
    """Processed input line from an interactive session."""

    original: str  # Original input (may be !!, !n, etc.)
    resolved: str  # Resolved command (after history recall)
    was_history_recall: bool  # True if command was resolved from !n/!!/!-n


class InteractiveSession:
    """Manages an interactive prompt session with history.

    This class encapsulates all the common functionality for interactive
    sessions in both cli.py and ctl.py:
    - History setup and management
    - PromptSession creation with syntax highlighting and completion
    - History recall command resolution (!!, !n, !-n)
    - Post-execution history cleanup (remove failed, replace aliases)
    - Input loop with proper error handling

    Usage:
        session = InteractiveSession.create(
            host="127.0.0.1",
            port=3000,
            history_file="/path/to/history",
            is_connected=lambda: bool(clients),
        )

        async for input_line in session.input_loop():
            result = await execute_command(input_line.resolved)
            session.handle_result(input_line, result.success)
            if result.message:
                print(session.format_output(input_line, result.message))
    """

    def __init__(
        self,
        history_file: Optional[str] = None,
        get_prompt: Optional[Callable[[], Any]] = None,
        prompt_text: str = "> ",
    ):
        """Initialize the interactive session.

        Args:
            history_file: Path to history file, "none" to disable, or None for in-memory.
            get_prompt: Optional callable returning prompt (can return FormattedText).
                       If None, uses prompt_text.
            prompt_text: Simple string prompt (used if get_prompt is None).
        """
        self._prompt_text = prompt_text
        self._get_prompt = get_prompt
        self._session = None
        self._history: Optional[History] = None

        if PROMPT_TOOLKIT_AVAILABLE:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

            self._history = History(history_file)

            self._session = PromptSession(
                history=self._history.prompt_toolkit_history,
                completer=SimulatorCompleter(),
                complete_while_typing=False,
                lexer=SimulatorLexer(),
                style=SIMULATOR_STYLE,
                auto_suggest=AutoSuggestFromHistory(),
                enable_history_search=True,
            )

    @classmethod
    def create(
        cls,
        host: str,
        port: int,
        history_file: Optional[str] = None,
        is_connected: Optional[Callable[[], bool]] = None,
    ) -> "InteractiveSession":
        """Create an InteractiveSession with standard prompt formatting.

        Args:
            host: Host address for prompt display
            port: Port number for prompt display
            history_file: Path to history file, "none" to disable, or None for in-memory.
            is_connected: Optional callback returning True if connected to client(s).
                         When provided, prompt color changes based on connection status.

        Returns:
            Configured InteractiveSession instance
        """
        prompt_text = f"{host}:{port}> "

        get_prompt = None
        if PROMPT_TOOLKIT_AVAILABLE and is_connected is not None:
            from prompt_toolkit.formatted_text import FormattedText

            def get_prompt():
                if is_connected():
                    return FormattedText([("class:prompt.connected", prompt_text)])
                else:
                    return FormattedText([("class:prompt.disconnected", prompt_text)])

        return cls(
            history_file=history_file,
            get_prompt=get_prompt,
            prompt_text=prompt_text,
        )

    @property
    def available(self) -> bool:
        """Check if interactive session is available (prompt_toolkit installed)."""
        return PROMPT_TOOLKIT_AVAILABLE and self._session is not None

    @property
    def history(self) -> Optional[History]:
        """Get the History object, or None if not available."""
        return self._history

    def resolve_history_recall(self, line: str) -> tuple[str, bool, Optional[str]]:
        """Resolve history recall commands (!!, !n, !-n).

        Args:
            line: The input line

        Returns:
            Tuple of (resolved_line, was_history_recall, error_message).
            - If not a history recall: (line, False, None)
            - If history recall succeeded: (resolved_line, True, None)
            - If history recall failed: (line, False, error_message)
        """
        if not self._history or not line.startswith("!"):
            return line, False, None

        result = self._history.resolve_recall(line)
        if result is None:
            return line, False, None

        resolved_cmd, prefix = result
        if resolved_cmd is None:
            # Error case - prefix contains error message
            return line, False, prefix

        return resolved_cmd, True, None

    def handle_result(
        self,
        input_line: InputLine,
        success: bool,
    ) -> None:
        """Handle post-execution history cleanup.

        This should be called after executing a command to:
        - Replace !n commands with their resolved form
        - Remove failed commands from history
        - Replace aliases with canonical command names

        Args:
            input_line: The InputLine from the input loop
            success: Whether the command succeeded
        """
        if not self._history:
            return

        if input_line.was_history_recall:
            if success:
                # Replace !1 with the resolved command
                self._history.replace_last_entry(input_line.resolved)
            else:
                # Failed - remove entirely
                self._history.remove_last_entry()
        elif not success:
            # Remove failed commands from history
            self._history.remove_last_entry()
        else:
            # Replace alias with canonical command name
            canonical = get_canonical_command(input_line.resolved)
            if canonical:
                self._history.replace_last_entry(canonical)

    def format_output(self, input_line: InputLine, message: str) -> str:
        """Format command output with history recall prefix if needed.

        Args:
            input_line: The InputLine from the input loop
            message: The command result message

        Returns:
            Formatted output string with >>> prefix and history recall info
        """
        if input_line.was_history_recall:
            return f">>> {input_line.original} -> {input_line.resolved}\n>>> {message}"
        return f">>> {message}"

    async def prompt_async(self) -> Optional[str]:
        """Get input from the user asynchronously.

        Returns:
            The input line stripped, or None on EOF/error.
        """
        if not self._session:
            return None

        try:
            prompt = self._get_prompt() if self._get_prompt else self._prompt_text
            line = await self._session.prompt_async(prompt)
            return line.strip() if line else ""
        except EOFError:
            return None
        except KeyboardInterrupt:
            return ""  # Return empty to continue loop

    async def input_loop(
        self,
        stop_check: Optional[Callable[[], bool]] = None,
    ):
        """Async generator that yields processed input lines.

        Handles:
        - Prompting for input
        - History recall resolution (!!, !n, !-n)
        - EOF and keyboard interrupt handling
        - Empty line filtering

        Args:
            stop_check: Optional callback that returns True to stop the loop.
                       Checked before each prompt.

        Yields:
            InputLine objects with original and resolved commands.
            Stops on EOF or when stop_check returns True.

        Example:
            async for input_line in session.input_loop(stop_check=stop_event.is_set):
                result = await execute(input_line.resolved)
                session.handle_result(input_line, result.success)
                if result.message:
                    print(session.format_output(input_line, result.message))
        """
        while True:
            # Check stop condition
            if stop_check and stop_check():
                break

            try:
                line = await self.prompt_async()
                if line is None:
                    # EOF
                    break
                if not line:
                    # Empty line or keyboard interrupt
                    continue

                # Handle history recall (!!, !n, !-n)
                resolved_line, was_history_recall, error = self.resolve_history_recall(line)
                if error:
                    print(f">>> {error}")
                    continue

                yield InputLine(
                    original=line,
                    resolved=resolved_line,
                    was_history_recall=was_history_recall,
                )

            except EOFError:
                break
            except KeyboardInterrupt:
                continue
