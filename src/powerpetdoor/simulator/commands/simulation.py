# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Simulation event commands."""

from typing import TYPE_CHECKING

from .base import CommandResult, command

if TYPE_CHECKING:
    from ..server import DoorSimulator


class SimulationCommandsMixin:
    """Mixin providing simulation event commands."""

    simulator: "DoorSimulator"

    @command(
        "obstruction",
        ["x"],
        "Simulate obstruction (triggers auto-retract)",
        category="simulation",
    )
    def obstruction(self) -> CommandResult:
        """Simulate an obstruction."""
        self.simulator.simulate_obstruction()
        return CommandResult(True, "Simulating obstruction")
