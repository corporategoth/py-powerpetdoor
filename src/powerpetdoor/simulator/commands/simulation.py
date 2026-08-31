# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Simulation event commands."""

from typing import TYPE_CHECKING

from ...i18n import t
from ..coerce import CoercionError, coerce_number, coerce_presence
from .base import ArgSpec, CommandResult, command
from .door import MAX_SENSOR_DURATION

if TYPE_CHECKING:
    from ..server import DoorSimulator


class SimulationCommandsMixin:
    """Mixin providing simulation event commands."""

    simulator: "DoorSimulator"

    @command(
        "obstruction",
        ["x"],
        "Place a physical obstruction in the doorway (triggers auto-retract)",
        category="simulation",
        args=[
            ArgSpec(
                "value",
                "string",
                required=False,
                description="on/off/toggle, or seconds (0 = until cleared)",
            )
        ],
    )
    def obstruction(self, value: str | None = None) -> CommandResult:
        """Place or clear a physical obstruction in the doorway.

        Takes the argument ``inside``/``outside`` take, because all three
        answer "is it there, and for how long". An obstruction is not a
        sensor, though: it is something the flap travels *into*, so the
        door still starts its close and meets it at the bottom - and you
        cannot obstruct a closed door, so one placed on a shut door simply
        waits for the next close.

        A bare ``obstruction`` toggles rather than pulsing, which is the
        one place the three differ and the one place it is physical: a pet
        walks past a sensor, a boot is placed.
        """
        if value is None:
            self.simulator.simulate_obstruction()
            return self._obstruction_result()
        try:
            present, duration = coerce_presence(value)
            if duration is not None:
                duration = coerce_number(duration, "duration", 0, MAX_SENSOR_DURATION)
        except CoercionError as exc:
            return CommandResult(False, str(exc))

        if duration is not None:
            self.simulator.simulate_obstruction(duration)
            if duration == 0:
                return CommandResult(
                    True,
                    t(
                        "simulator.commands.simulation.obstruction_placed_until_cleared",
                        "Obstruction placed (until cleared)",
                    ),
                )
            return CommandResult(
                True,
                t(
                    "simulator.commands.simulation.obstruction_placed_for_s",
                    "Obstruction placed for {duration}s",
                    duration=duration,
                ),
            )
        if present is None:
            self.simulator.simulate_obstruction()
        elif present:
            self.simulator.simulate_obstruction(0)
        else:
            self.simulator.clear_obstruction()
        return self._obstruction_result()

    def _obstruction_result(self) -> CommandResult:
        """Report where the doorway ended up, since toggling does not say.

        Read off the state rather than off what was there before: `on` and
        a bare toggle both place an obstruction, but only the bare one is
        a *one-shot*, and saying so is the difference between "it will
        survive the retract" and "it will not".
        """
        state = self.simulator.state
        if not state.obstruction_active:
            return CommandResult(
                True,
                t("simulator.commands.simulation.obstruction_cleared", "Obstruction cleared"),
            )
        if state.obstruction_oneshot:
            return CommandResult(
                True,
                t(
                    "simulator.commands.simulation.obstruction_placed",
                    "Obstruction placed (cleared by the retract it causes)",
                ),
            )
        return CommandResult(
            True,
            t(
                "simulator.commands.simulation.obstruction_placed_until_cleared",
                "Obstruction placed (until cleared)",
            ),
        )
