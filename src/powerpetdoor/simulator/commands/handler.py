# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Command handler that combines all command mixins."""

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Optional

from .base import (
    ArgSpec,
    CommandResult,
    SubcommandInfo,
    get_command_registry,
    parse_arg,
)
from .history import History
from .buttons import ButtonCommandsMixin
from .control import ControlCommandsMixin
from .door import DoorCommandsMixin
from .info import InfoCommandsMixin
from .notifications import NotifyCommandsMixin
from .schedules import ScheduleCommandsMixin
from .scripts import ScriptsCommandsMixin
from .settings import SettingsCommandsMixin
from .simulation import SimulationCommandsMixin

if TYPE_CHECKING:
    from ..server import DoorSimulator
    from ..scripting import ScriptRunner

logger = logging.getLogger(__name__)


class CommandHandler(
    DoorCommandsMixin,
    SimulationCommandsMixin,
    ButtonCommandsMixin,
    SettingsCommandsMixin,
    NotifyCommandsMixin,
    ScheduleCommandsMixin,
    ScriptsCommandsMixin,
    InfoCommandsMixin,
    ControlCommandsMixin,
):
    """Handles commands for the simulator.

    Provides a unified interface for controlling the simulator from
    interactive input, control port, or scripts.

    Commands can be invoked:
    - Via execute() with a command string
    - Directly as methods (e.g., handler.inside(), handler.power(True))
    """

    def __init__(
        self,
        simulator: "DoorSimulator",
        script_runner: "ScriptRunner",
        stop_callback: Callable[[], None],
        script_queue: Optional[asyncio.Queue] = None,
    ):
        """Initialize the command handler.

        Args:
            simulator: The door simulator instance
            script_runner: The script runner instance
            stop_callback: Function to call to stop the simulator
            script_queue: Optional queue for queueing scripts
        """
        self.simulator = simulator
        self.script_runner = script_runner
        self.stop_callback = stop_callback
        self.script_queue = script_queue
        self._history_obj: History | None = None  # Set by cli.py when using prompt_toolkit
        self._history = None  # prompt_toolkit history for InfoCommandsMixin compatibility
        self._interactive_mode = False  # Set by cli.py for interactive sessions
        self._cli_mode = False  # Set by cli.py for CLI interactive mode (vs ctl/daemon)

        # Import here to avoid circular imports
        from ..scripting import Script, get_builtin_script, list_builtin_scripts
        from ..state import Schedule

        self._Script = Script
        self._get_builtin_script = get_builtin_script
        self._list_builtin_scripts = list_builtin_scripts
        self._Schedule = Schedule

        # Register subcommand handlers from decorated methods
        self._register_subcommand_handlers()

    def _register_subcommand_handlers(self):
        """Register subcommand handlers from @subcommand decorated methods."""
        _command_registry = get_command_registry()

        for name in dir(self):
            if name.startswith("_"):
                continue
            method = getattr(self, name)
            if not callable(method):
                continue

            # Check for subcommand decorator metadata
            func = getattr(method, "__func__", method)
            if hasattr(func, "_subcommand_info") and hasattr(func, "_parent_path"):
                sub_info: SubcommandInfo = func._subcommand_info
                parent_path: list[str] = func._parent_path

                # Find the root command
                if not parent_path or parent_path[0] not in _command_registry:
                    logger.warning(
                        f"Parent command '{parent_path[0] if parent_path else '?'}' "
                        f"not found for subcommand '{sub_info.name}'"
                    )
                    continue

                # Navigate to the parent through the path
                parent_info: SubcommandInfo = _command_registry[parent_path[0]]
                for i, part in enumerate(parent_path[1:], 1):
                    if part not in parent_info.subcommands:
                        logger.warning(
                            f"Subcommand '{part}' not found in path "
                            f"{parent_path[:i]} for '{sub_info.name}'"
                        )
                        parent_info = None
                        break
                    parent_info = parent_info.subcommands[part]

                if parent_info is None:
                    continue

                # Create a new SubcommandInfo with the handler bound
                new_sub_info = SubcommandInfo(
                    name=sub_info.name,
                    aliases=sub_info.aliases,
                    description=sub_info.description,
                    usage=sub_info.usage,
                    handler=func,
                    args=sub_info.args,
                    subcommands=sub_info.subcommands,
                )

                # Register under name and aliases
                parent_info.subcommands[sub_info.name] = new_sub_info
                for alias in sub_info.aliases:
                    parent_info.subcommands[alias] = new_sub_info

    def set_history(self, history: History):
        """Set the History object for history-related functionality.

        Args:
            history: A History instance that wraps prompt_toolkit history
        """
        self._history_obj = history
        self._history = history.prompt_toolkit_history  # For InfoCommandsMixin compatibility

    def set_interactive_mode(self, enabled: bool):
        """Set whether the handler is operating in interactive mode.

        When interactive mode is disabled, commands marked with interactive_only=True
        will return an error instead of executing.

        Args:
            enabled: True for interactive mode, False for non-interactive (e.g., control port)
        """
        self._interactive_mode = enabled

    def set_cli_mode(self, enabled: bool):
        """Set whether the handler is running in CLI interactive mode.

        When CLI mode is enabled, exit/q/quit are registered as aliases
        for shutdown (since in CLI mode they do the same thing), and the
        separate exit command is hidden.

        Args:
            enabled: True for CLI interactive mode, False for ctl/daemon mode
        """
        self._cli_mode = enabled

        # Dynamically add/remove exit aliases for shutdown
        _command_registry = get_command_registry()
        if "shutdown" not in _command_registry:
            return

        shutdown_info = _command_registry["shutdown"]
        cli_aliases = ["exit", "q", "quit"]

        if enabled:
            # In CLI mode, exit/q/quit become aliases for shutdown
            # First, remove the standalone exit command's registry entries
            # (the exit command itself is hidden via get_help check)
            if "exit" in _command_registry:
                exit_info = _command_registry["exit"]
                # Remove exit command's aliases from registry
                for alias in list(exit_info.aliases):
                    if alias in _command_registry and _command_registry[alias] is exit_info:
                        del _command_registry[alias]

            # Add exit/q/quit as aliases for shutdown
            for alias in cli_aliases:
                _command_registry[alias] = shutdown_info
                if alias not in shutdown_info.aliases:
                    shutdown_info.aliases = tuple(list(shutdown_info.aliases) + [alias])
        else:
            # Remove exit/q/quit from shutdown aliases
            for alias in cli_aliases:
                if alias in _command_registry and _command_registry[alias] is shutdown_info:
                    del _command_registry[alias]
            shutdown_info.aliases = tuple(
                a for a in shutdown_info.aliases if a not in cli_aliases
            )

            # Restore exit command's aliases to registry
            if "exit" in _command_registry:
                exit_info = _command_registry["exit"]
                for alias in exit_info.aliases:
                    if alias not in _command_registry:
                        _command_registry[alias] = exit_info

    async def execute(self, command_str: str) -> CommandResult:
        """Execute a command string and return the result.

        Recursively dispatches to subcommand handlers when available.
        Implicit 'help' and '?' subcommands show help for commands with subcommands.

        Args:
            command_str: The command string to execute (e.g., "inside", "schedule add")

        Returns:
            CommandResult with success status and message
        """
        _command_registry = get_command_registry()

        if not command_str:
            return CommandResult(False, "Empty command")

        parts = command_str.split()
        cmd = parts[0].lower()

        # Look up command in registry
        if cmd not in _command_registry:
            return CommandResult(
                False, f"Unknown command: {cmd}. Type 'help' for commands."
            )

        # Check for interactive-only commands (hide them when not in interactive mode)
        cmd_info = _command_registry[cmd]
        if cmd_info.interactive_only and not self._interactive_mode:
            return CommandResult(
                False, f"Unknown command: {cmd}. Type 'help' for commands."
            )

        # Reject local_only commands when running as daemon control port
        # In CLI mode (_cli_mode=True) or interactive mode, these are handled normally
        # Daemon control port has _interactive_mode=False and _cli_mode=False
        if cmd_info.local_only and not self._cli_mode and not self._interactive_mode:
            return CommandResult(
                False, f"Unknown command: {cmd}. Type 'help' for commands."
            )

        # Hide history command when prompt_toolkit is not available
        if cmd in ("history", "hist") and not self._is_history_available():
            return CommandResult(
                False, f"Unknown command: {cmd}. Type 'help' for commands."
            )

        info: SubcommandInfo = _command_registry[cmd]
        cmd_path = [_command_registry[cmd].name]  # Track command path for help

        # Traverse subcommand hierarchy to find the deepest matching handler
        part_idx = 1
        while part_idx < len(parts) and info.subcommands:
            subcmd = parts[part_idx].lower()

            # Handle implicit help/? subcommand
            if subcmd in ("help", "?"):
                # If we have args, show arg help instead of subcommand help
                if info.args:
                    help_text = self._get_arg_help(info, cmd_path)
                elif info.subcommands:
                    help_text = self._get_subcommand_help(info, cmd_path)
                else:
                    help_text = f"{' '.join(cmd_path)}: {info.description or 'No help available.'}"
                return CommandResult(True, help_text)

            if subcmd in info.subcommands:
                subinfo = info.subcommands[subcmd]
                # Only descend if subcommand has its own handler
                if subinfo.handler is not None:
                    info = subinfo
                    cmd_path.append(subinfo.name)
                    part_idx += 1
                else:
                    # Subcommand exists but has no handler - stop here
                    break
            else:
                # Not a subcommand - if command has args, treat as argument
                if info.args:
                    break
                # Unknown subcommand - report error with available subcommands
                subnames = sorted(set(s.name for s in info.subcommands.values()))
                cmd_str = " ".join(cmd_path)
                return CommandResult(
                    False,
                    f"Unknown {cmd_str} subcommand: {subcmd}\n"
                    f"Available: {', '.join(subnames)}",
                )

        # Build remaining argument parts
        remaining_parts = parts[part_idx:]

        # Get the handler
        if info.handler is None:
            return CommandResult(
                False, f"No handler for: {' '.join(parts[:part_idx])}"
            )

        handler = getattr(self, info.handler.__name__)

        # Parse and call handler based on ArgSpec
        if info.args:
            # Check for help request as first arg
            if remaining_parts and remaining_parts[0].lower() in ("help", "?"):
                help_text = self._get_arg_help(info, cmd_path)
                return CommandResult(True, help_text)

            # Parse arguments according to ArgSpec
            parsed_args, error = self._parse_args(remaining_parts, info.args, cmd_path)
            if error:
                return error
            # Call handler with parsed arguments
            try:
                result = handler(*parsed_args)
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception as e:
                return CommandResult(False, f"Error: {e}")
        else:
            # No args defined - call handler with no arguments
            try:
                result = handler()
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception as e:
                return CommandResult(False, f"Error: {e}")

        return result

    def _parse_args(
        self,
        parts: list[str],
        arg_specs: list[ArgSpec],
        cmd_path: list[str],
    ) -> tuple[list, Optional[CommandResult]]:
        """Parse argument parts according to ArgSpec definitions.

        Returns:
            (parsed_args, error) - error is None on success
        """
        parsed = []
        cmd_str = " ".join(cmd_path)
        usage = " ".join(spec.generate_usage() for spec in arg_specs)

        for i, spec in enumerate(arg_specs):
            if i < len(parts):
                value, error = parse_arg(parts[i], spec)
                if error:
                    return [], CommandResult(
                        False, f"{error}\nUsage: {cmd_str} {usage}"
                    )
                parsed.append(value)
            elif spec.required:
                return [], CommandResult(
                    False,
                    f"Missing required argument: {spec.name}\nUsage: {cmd_str} {usage}",
                )
            else:
                parsed.append(spec.default)

        return parsed, None
