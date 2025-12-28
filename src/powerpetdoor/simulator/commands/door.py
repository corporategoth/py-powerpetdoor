# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Door operation commands."""

import asyncio
from typing import TYPE_CHECKING

from .base import ArgSpec, CommandResult, command

if TYPE_CHECKING:
    from ..server import DoorSimulator


class DoorCommandsMixin:
    """Mixin providing door operation commands."""

    simulator: "DoorSimulator"

    @command(
        "inside",
        ["i"],
        "Activate inside sensor detection",
        category="door",
        args=[
            ArgSpec(
                "duration",
                "float",
                required=False,
                default=0.5,
                min_value=0,
                description="Duration in seconds (0 = toggle)",
            )
        ],
    )
    def inside(self, duration: float = 0.5) -> CommandResult:
        """Activate inside sensor detection.

        Args:
            duration: How long sensor stays active (default 0.5s).
                     0 = toggle mode (on indefinitely if off, off if on)
        """
        self.simulator.activate_sensor("inside", duration)
        if duration == 0:
            state = (
                "activated" if self.simulator.state.inside_sensor_active else "deactivated"
            )
            return CommandResult(True, f"Inside sensor {state} (toggle)")
        return CommandResult(True, f"Inside sensor activated for {duration}s")

    @command(
        "outside",
        ["o"],
        "Activate outside sensor detection",
        category="door",
        args=[
            ArgSpec(
                "duration",
                "float",
                required=False,
                default=0.5,
                min_value=0,
                description="Duration in seconds (0 = toggle)",
            )
        ],
    )
    def outside(self, duration: float = 0.5) -> CommandResult:
        """Activate outside sensor detection.

        Args:
            duration: How long sensor stays active (default 0.5s).
                     0 = toggle mode (on indefinitely if off, off if on)
        """
        self.simulator.activate_sensor("outside", duration)
        if duration == 0:
            state = (
                "activated"
                if self.simulator.state.outside_sensor_active
                else "deactivated"
            )
            return CommandResult(True, f"Outside sensor {state} (toggle)")
        return CommandResult(True, f"Outside sensor activated for {duration}s")

    @command("close", ["c"], "Close the door", category="door")
    def close(self) -> CommandResult:
        """Close the door."""
        asyncio.create_task(self.simulator.close_door())
        return CommandResult(True, "Closing door")

    @command("hold", ["h", "open"], "Open and hold the door", category="door")
    def hold(self) -> CommandResult:
        """Open the door and hold it open."""
        asyncio.create_task(self.simulator.open_door(hold=True))
        return CommandResult(True, "Opening and holding")

    @command(
        "cycle", ["y"], "Full door cycle (like pressing door button)", category="door"
    )
    def cycle(self) -> CommandResult:
        """Run a full door cycle - open, hold, close.

        This simulates pressing the physical button on the door,
        which opens the door, holds for hold_time, then closes.
        Unlike sensor triggers, this bypasses sensor enable checks.
        """
        asyncio.create_task(self.simulator.open_door(hold=False))
        return CommandResult(True, "Starting door cycle")
