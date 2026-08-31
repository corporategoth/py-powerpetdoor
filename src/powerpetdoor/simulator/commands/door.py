# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Door operation commands."""

import asyncio
from typing import TYPE_CHECKING

from ...const import DOOR_STATES_CLOSED, DOOR_STATES_FULLY_OPEN
from ...i18n import t
from ..coerce import CoercionError, coerce_number, coerce_presence
from ..engine import SENSOR_NAMES
from ..values import read_value
from .base import ArgSpec, CommandResult, command

#: What a bare `inside`/`outside` means: a pet walking past, not
#: standing there. An obstruction has no equivalent - it is placed,
#: not walked past - which is the one place the three differ.
DEFAULT_SENSOR_PULSE = 0.5

#: Longest a sensor may be held by a duration argument. A day, the
#: same ceiling the script channel uses; 0 means indefinitely.
MAX_SENSOR_DURATION = 86400.0

if TYPE_CHECKING:
    from ..server import DoorSimulator


class DoorCommandsMixin:
    """Mixin providing door operation commands."""

    simulator: "DoorSimulator"

    def _sensor_command(self, sensor: str, value: str | None) -> CommandResult:
        """`inside`/`outside`, which take the argument `obstruction` takes.

        All three answer "is it there, and for how long", so all three read
        ``on``/``off``/``toggle`` or a number of seconds. A held sensor
        *is* pet presence - a collar sitting in range - which is why there
        is no separate `pet` command any more: it was `inside on` by
        another name, except that it never opened a closed door, which a
        real collar at the sensor does.
        """
        if value is None:
            self.simulator.activate_sensor(sensor, DEFAULT_SENSOR_PULSE)
            return CommandResult(
                True,
                t(
                    "simulator.commands.door.sensor_activated_s",
                    "{sensor} sensor activated for {duration}s",
                    sensor=sensor.capitalize(),
                    duration=DEFAULT_SENSOR_PULSE,
                ),
            )
        try:
            present, duration = coerce_presence(value)
            if duration is not None:
                # Routing through coerce_presence dropped the bound the
                # old float ArgSpec enforced, so `inside -1` reached
                # asyncio.sleep() instead of being refused.
                duration = coerce_number(duration, "duration", 0, MAX_SENSOR_DURATION)
        except CoercionError as exc:
            return CommandResult(False, str(exc))

        if duration is not None:
            self.simulator.activate_sensor(sensor, duration)
            if duration == 0:
                return self._sensor_state_result(sensor)
            return CommandResult(
                True,
                t(
                    "simulator.commands.door.sensor_activated_s",
                    "{sensor} sensor activated for {duration}s",
                    sensor=sensor.capitalize(),
                    duration=duration,
                ),
            )
        self.simulator.hold_sensor(sensor, present)
        return self._sensor_state_result(sensor)

    def _sensor_state_result(self, sensor: str) -> CommandResult:
        """Report where a sensor ended up, since toggling does not say."""
        return CommandResult(
            True,
            t(
                "simulator.commands.door.sensor_now",
                "{sensor} sensor {state}",
                sensor=sensor.capitalize(),
                state="active" if self.simulator.state.pet_present(sensor) else "clear",
            ),
        )

    @command(
        "trigger",
        ["tr"],
        "A pet walks through a sensor, rather than standing at it",
        category="door",
        args=[
            ArgSpec(
                "sensor",
                "choice",
                choices=list(SENSOR_NAMES),
                description="Which sensor the pet passes",
            )
        ],
    )
    def trigger(self, sensor: str) -> CommandResult:
        """A pass-through, which is not the same as presence.

        `inside on` puts a pet at the sensor and leaves it there; this is
        one walking past. The difference is visible: a pass-through
        extends the hold on an already-open door and retracts one that is
        closing, where standing still does neither.

        The script DSL and the control socket already had this; the prompt
        did not, so the one thing you cannot do by hand was the thing
        scripts test with.
        """
        self.simulator.trigger_sensor(sensor)
        return CommandResult(
            True,
            t(
                "simulator.commands.door.sensor_triggered",
                "{sensor} sensor triggered",
                sensor=sensor.capitalize(),
            ),
        )

    @command(
        "inside",
        ["i"],
        "Inside sensor: a pet in range, for a moment or indefinitely",
        category="door",
        args=[
            ArgSpec(
                "value",
                "string",
                required=False,
                description="on/off/toggle, or seconds (0 = until cleared)",
            )
        ],
    )
    def inside(self, value: str | None = None) -> CommandResult:
        """Activate the inside sensor. See :meth:`_sensor_command`."""
        return self._sensor_command("inside", value)

    @command(
        "outside",
        ["o"],
        "Outside sensor: a pet in range, for a moment or indefinitely",
        category="door",
        args=[
            ArgSpec(
                "value",
                "string",
                required=False,
                description="on/off/toggle, or seconds (0 = until cleared)",
            )
        ],
    )
    def outside(self, value: str | None = None) -> CommandResult:
        """Activate the outside sensor. See :meth:`_sensor_command`."""
        return self._sensor_command("outside", value)

    @command("close", ["c"], "Close the door", category="door")
    def close(self) -> CommandResult:
        """Close the door."""
        asyncio.create_task(self.simulator.close_door())
        return CommandResult(True, t("simulator.commands.door.closing_door", "Closing door"))

    @command("open", ["hold", "h"], "Open the door and hold it open", category="door")
    def open(self) -> CommandResult:
        """Open the door and hold it open until it is closed."""
        asyncio.create_task(self.simulator.open_door(hold=True))
        return CommandResult(
            True, t("simulator.commands.door.opening_holding", "Opening and holding")
        )

    @command("toggle", ["tg"], "Open the door if closed, close it if open", category="door")
    def toggle(self) -> CommandResult:
        """Toggle the door, mirroring :meth:`powerpetdoor.door.PowerPetDoor.toggle`.

        The decision itself belongs to
        :meth:`~powerpetdoor.simulator.server.DoorSimulator.toggle_door`,
        which a script and a programmatic caller reach too; this reports
        what it chose.
        """
        status = read_value(self.simulator.state, "door_status")
        asyncio.create_task(self.simulator.toggle_door())
        if status in DOOR_STATES_CLOSED:
            return CommandResult(
                True, t("simulator.commands.door.toggle_opening", "Toggle: opening and holding")
            )
        if status in DOOR_STATES_FULLY_OPEN:
            return CommandResult(
                True, t("simulator.commands.door.toggle_closing", "Toggle: closing")
            )
        return CommandResult(
            True,
            t(
                "simulator.commands.door.toggle_in_travel",
                "Toggle: ignored, door is in motion ({status})",
                status=status,
            ),
        )

    @command("cycle", ["y"], "Full door cycle (like pressing door button)", category="door")
    def cycle(self) -> CommandResult:
        """Run a full door cycle - open, hold for hold_time, close.

        This simulates pressing the physical button on the door,
        which opens the door, holds for hold_time, then closes.
        Unlike sensor triggers, this bypasses sensor enable checks.
        """
        asyncio.create_task(self.simulator.open_door(hold=False))
        return CommandResult(
            True, t("simulator.commands.door.starting_door_cycle", "Starting door cycle")
        )
