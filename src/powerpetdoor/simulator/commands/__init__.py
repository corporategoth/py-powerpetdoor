# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Unified command handler for the Power Pet Door simulator.

This module provides a command dispatcher that can be used by:
- Interactive keyboard input
- Control port connections
- Scripts
- Direct Python API calls

The command handler is split into category-specific mixins for maintainability:
- DoorCommandsMixin: Door operations (inside, outside, close, hold, cycle)
- SimulationCommandsMixin: Simulation events (obstruction)
- ButtonCommandsMixin: Physical button toggles (power, auto, inside_enable, etc.)
- SettingsCommandsMixin: Settings management (safety, lockout, battery, etc.)
- NotifyCommandsMixin: Notification settings
- ScheduleCommandsMixin: Schedule management
- ScriptsCommandsMixin: Script running
- InfoCommandsMixin: Status and help
- ControlCommandsMixin: Simulator control (exit)
"""

from .base import (
    ArgSpec,
    CommandInfo,
    CommandResult,
    SubcommandInfo,
    command,
    get_canonical_command,
    parse_arg,
    subcommand,
)
from .handler import CommandHandler

__all__ = [
    "ArgSpec",
    "CommandHandler",
    "CommandInfo",
    "CommandResult",
    "SubcommandInfo",
    "command",
    "get_canonical_command",
    "parse_arg",
    "subcommand",
]
