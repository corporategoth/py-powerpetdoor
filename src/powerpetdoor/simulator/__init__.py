# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Power Pet Door simulator submodule.

This module provides a simulated Power Pet Door that speaks the same
protocol as the real device. Useful for testing clients without
real hardware.

The simulator can:
- Respond to all client commands
- Spontaneously trigger events (sensor triggers, door movements)
- Simulate realistic door open/close sequences with configurable timing
- Simulate obstruction detection and auto-retract
- Simulate pet presence keeping door open
- Store and retrieve schedules
- Be controlled interactively via keyboard or programmatically

Example usage:
    # Run interactively
    python -m powerpetdoor.simulator

    # Or use programmatically
    from powerpetdoor.simulator import DoorSimulator
    simulator = DoorSimulator(port=3000)
    await simulator.start()
"""

from .state import DoorSimulatorState, Schedule, DoorTimingConfig, BatteryConfig
from .protocol import DoorSimulatorProtocol, CommandRegistry
from .server import DoorSimulator
from .cli import run_simulator, main
from .commands import CommandHandler, CommandResult
from .scripting import (
    Script,
    ScriptRunner,
    ScriptStep,
    ScriptError,
    AssertionFailed,
    get_builtin_script,
    list_builtin_scripts,
)

__all__ = [
    # Main classes
    "DoorSimulator",
    "DoorSimulatorProtocol",
    "DoorSimulatorState",
    # State helpers
    "Schedule",
    "DoorTimingConfig",
    "BatteryConfig",
    # Command registry
    "CommandRegistry",
    # CLI
    "run_simulator",
    "main",
    # Commands
    "CommandHandler",
    "CommandResult",
    # Scripting
    "Script",
    "ScriptRunner",
    "ScriptStep",
    "ScriptError",
    "AssertionFailed",
    "get_builtin_script",
    "list_builtin_scripts",
]
