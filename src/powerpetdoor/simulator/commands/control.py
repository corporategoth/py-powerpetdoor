# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Control commands."""

import logging
import sys
from collections.abc import Callable

from ...i18n import t
from .base import ArgSpec, CommandResult, command


class ControlCommandsMixin:
    """Mixin providing control commands."""

    stop_callback: Callable[[], None]
    _cli_mode: bool

    @command("shutdown", [], "Shutdown the simulator", category="control")
    def shutdown(self) -> CommandResult:
        """Shutdown the simulator.

        ``stop`` used to be an alias for this. It is not any more: with
        serialized script runs the natural reading of "stop" is "stop the
        running script", and killing the whole daemon instead is a
        foot-gun. ``stop`` now does exactly that (see the ``stop`` command);
        use ``shutdown`` (or, in the CLI, ``exit``/``q``/``quit``) to stop
        the simulator.
        """
        self.stop_callback()
        return CommandResult(
            True, t("simulator.commands.control.shutting_down", "Shutting down...")
        )

    @command(
        "debug",
        [],
        "Enable or disable debug logging",
        category="control",
        args=[
            ArgSpec(
                "state",
                "bool_toggle",
                required=False,
                description="on/off to set debug mode, omit to show current state",
            )
        ],
    )
    def debug(self, state: bool | None = None) -> CommandResult:
        """Enable or disable debug logging.

        When enabled, shows detailed debug messages including protocol traffic.
        When disabled, only shows info level and above.
        """
        root_logger = logging.getLogger()
        current_level = root_logger.level

        if state is None:
            # Show current state
            is_debug = current_level <= logging.DEBUG
            return CommandResult(
                True,
                t(
                    "simulator.commands.control.debug_logging",
                    "Debug logging: {arg0}",
                    arg0="on" if is_debug else "off",
                ),
            )

        if state:
            root_logger.setLevel(logging.DEBUG)
            return CommandResult(
                True, t("simulator.commands.control.debug_logging_enabled", "Debug logging enabled")
            )
        else:
            root_logger.setLevel(logging.INFO)
            return CommandResult(
                True,
                t("simulator.commands.control.debug_logging_disabled", "Debug logging disabled"),
            )

    @command(
        "exit",
        ["q", "quit"],
        "Exit the control client",
        category="control",
        interactive_only=True,
        local_only=True,
    )
    def exit_ctl(self) -> CommandResult:
        """Exit the control client (ctl).

        This command is handled locally by ctl and not sent to the daemon.
        It appears in help for ctl users but is hidden in CLI mode where
        exit/q/quit are aliases for shutdown.
        """
        # This is a placeholder - ctl intercepts exit locally
        return CommandResult(
            True,
            t(
                "simulator.commands.control.exit_handled_locally_by_control",
                "Exit is handled locally by the control client",
            ),
        )

    @command(
        "clear",
        ["cls"],
        "Clear the screen",
        category="control",
        interactive_only=True,
        local_only=True,
    )
    def clear(self) -> CommandResult:
        """Clear the terminal screen.

        A no-op off a terminal: writing the escape sequence to a pipe or a
        dumb terminal only injects literal garbage into the output.
        """
        # Use __stdout__ to bypass prompt_toolkit's patch_stdout
        out = sys.__stdout__ if sys.__stdout__ else sys.stdout
        if out.isatty():
            # ANSI escape: \033[2J clears the screen, \033[H homes the cursor
            out.write("\033[2J\033[H")
            out.flush()
        return CommandResult(True, "")
