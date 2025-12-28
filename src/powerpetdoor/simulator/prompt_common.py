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
        # Collect choices from args
        for arg in info.args:
            if arg.arg_type == "bool_toggle":
                _OPTIONS.update(["on", "off"])
            elif arg.arg_type == "choice" and arg.choices:
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
        # Collect options from command args
        for arg in info.args:
            if arg.arg_type == "bool_toggle":
                _OPTIONS.update(["on", "off"])
            elif arg.arg_type == "choice" and arg.choices:
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

        def lex_document(self, document):
            """Return a lexer function for the document."""
            # Initialize command sets if needed
            init_command_sets()

            def get_line_tokens(line_number):
                line = document.lines[line_number]
                tokens = []
                words = line.split()
                pos = 0

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
                    elif i == 1:
                        # Second word might be subcommand or option
                        if word.lower() in _SUBCOMMANDS:
                            tokens.append(("class:subcommand", word))
                        elif word.lower() in _OPTIONS:
                            tokens.append(("class:option", word))
                        elif word.replace(".", "").replace("-", "").isdigit():
                            tokens.append(("class:number", word))
                        else:
                            tokens.append(("", word))
                    else:
                        # Other words
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

        def _get_subcommands(self, cmd: str) -> list[tuple[str, str]]:
            """Get subcommands for a command with descriptions from the registry."""
            # Special case for run/file command - add builtin scripts
            if cmd in ("run", "r", "file"):
                try:
                    from .scripting import list_builtin_scripts

                    return [(name, desc) for name, desc in list_builtin_scripts()]
                except Exception:
                    return []

            # Look up command in registry
            registry = get_command_registry()
            if cmd not in registry:
                return []

            info = registry[cmd]
            if not info.subcommands:
                return []

            # Collect unique subcommands (avoid duplicates from aliases)
            seen = set()
            result = []
            for sub_info in info.subcommands.values():
                if sub_info.name not in seen:
                    seen.add(sub_info.name)
                    result.append((sub_info.name, sub_info.description))
                    # Also add aliases
                    for alias in sub_info.aliases:
                        if alias not in seen:
                            seen.add(alias)
                            result.append((alias, f"Alias for {sub_info.name}"))
            return sorted(result, key=lambda x: x[0])

        def _get_toggle_options(self, cmd: str) -> list[tuple[str, str]]:
            """Get toggle options for commands from the registry."""
            registry = get_command_registry()
            if cmd not in registry:
                return []

            info = registry[cmd]
            if not info.args:
                return []

            # Check the first arg's type
            arg = info.args[0]
            if arg.arg_type == "bool_toggle":
                return [("on", "Enable"), ("off", "Disable")]
            elif arg.arg_type == "choice" and arg.choices:
                return [(c.lower(), c) for c in arg.choices]
            return []

        def get_completions(self, document, complete_event):
            """Generate completions for the current input."""
            text = document.text_before_cursor
            words = text.split()

            # Determine what we're completing
            if not text or text.endswith(" "):
                # Starting a new word
                word_before = ""
                completing_first = len(words) == 0
            else:
                # Completing current word
                word_before = words[-1] if words else ""
                completing_first = len(words) == 1

            if completing_first or not words:
                # Complete command names
                for cmd, desc in self._get_commands():
                    if cmd.startswith(word_before):
                        yield Completion(
                            cmd,
                            start_position=-len(word_before),
                            display_meta=desc,
                        )
            else:
                # Complete subcommands or options
                cmd = words[0].lower()
                subcommands = self._get_subcommands(cmd)
                if subcommands:
                    for sub, desc in subcommands:
                        if sub.startswith(word_before):
                            yield Completion(
                                sub,
                                start_position=-len(word_before),
                                display_meta=desc,
                            )
                else:
                    options = self._get_toggle_options(cmd)
                    for opt, desc in options:
                        if opt.startswith(word_before):
                            yield Completion(
                                opt,
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
