# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Simulation event commands."""

from typing import TYPE_CHECKING

from ...i18n import t
from .base import ArgSpec, CommandResult, SubcommandInfo, command, subcommand

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
        return CommandResult(
            True,
            t("simulator.commands.simulation.simulating_obstruction", "Simulating obstruction"),
        )

    def _set_pet_presence(self, present: bool) -> CommandResult:
        """Apply pet presence and build the result message."""
        self.simulator.set_pet_in_doorway(present)
        state = "in doorway" if present else "left doorway"
        return CommandResult(
            True, t("simulator.commands.simulation.pet", "Pet {state}", state=state)
        )

    @command(
        "pet",
        ["d"],
        "Toggle or set pet presence in doorway (holds door open)",
        category="simulation",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle pet presence")],
    )
    def pet(self, value: bool | None = None) -> CommandResult:
        """Toggle or set pet presence in the doorway.

        A pet standing in the doorway keeps the inside sensor active,
        which holds the door open until the pet leaves.
        """
        if value is None:
            value = not self.simulator.state.inside_sensor_active
        return self._set_pet_presence(value)

    @subcommand("pet", "toggle", ["t"], "Toggle pet presence")
    def pet_toggle(self) -> CommandResult:
        """Toggle pet presence in the doorway."""
        return self._set_pet_presence(not self.simulator.state.inside_sensor_active)
