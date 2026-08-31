# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The prompt's view of the value registry.

``get`` and ``set`` reach every named value generically. The named
commands - ``power``, ``safety``, ``inside_enable`` and the rest - are the
same values under the words an operator actually types, and they are
*generated* from :data:`SWITCH_COMMANDS` rather than written out.

Each was eight lines of decorator plus a two-line body plus a near-copy
for its ``toggle`` subcommand, and every one of them ended in the same
call. What genuinely differs between them - the word, its shortcut, the
label in the reply, and whether it reads "ON" or "enabled" - is the row.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ...i18n import t
from ..coerce import CoercionError
from ..values import (
    VALUE_NAMES,
    VALUES,
    WRITABLE,
    canonical_name,
    coerce_value,
    set_named_value,
    toggle_named_value,
)
from .base import ArgSpec, CommandResult, SubcommandInfo, command, subcommand

if TYPE_CHECKING:
    from ..server import DoorSimulator
    from ..state import DoorSimulatorState


def render_value(name: str, state: "DoorSimulatorState") -> str:
    """One ``name: value`` line."""
    return f"{name}: {VALUES[name].get(state)}"


class ValueCommandsMixin:
    """Mixin providing the generic ``get``/``set`` commands."""

    simulator: "DoorSimulator"

    @command(
        "get",
        [],
        "Show a value, or every value",
        category="info",
        args=[
            ArgSpec(
                "name",
                "string",
                required=False,
                description="Value to show; omit for all",
            )
        ],
    )
    def get_value(self, name: str | None = None) -> CommandResult:
        """Show one value, or all of them.

        Anything :class:`~powerpetdoor.door.PowerPetDoor` can read is
        readable here, which is what makes the simulator testable against
        the same surface a consumer sees.
        """
        state = self.simulator.state
        if name is None:
            device = [n for n in VALUE_NAMES if not VALUES[n].simulation_only]
            simulated = [n for n in VALUE_NAMES if VALUES[n].simulation_only]
            lines = ["Door:"]
            lines += [f"  {render_value(n, state)}" for n in device]
            lines.append("Simulation:")
            lines += [f"  {render_value(n, state)}" for n in simulated]
            return CommandResult(
                True, "\n".join(lines), {n: VALUES[n].get(state) for n in VALUE_NAMES}
            )

        key = canonical_name(name)
        if key not in VALUES:
            return CommandResult(False, _unknown(key))
        return CommandResult(True, render_value(key, state), {key: VALUES[key].get(state)})

    @command(
        "set",
        [],
        "Change a value",
        category="settings",
        args=[
            ArgSpec("name", "string", description="Value to change"),
            ArgSpec("value", "string", description="New value"),
        ],
    )
    def set_value(self, name: str, value: str) -> CommandResult:
        """Change one value, applying whatever broadcast a real change sends.

        The resolve-refuse-coerce-apply chain belongs to
        :func:`~powerpetdoor.simulator.values.set_named_value`, which the
        script DSL writes through too; this only turns its refusal into a
        :class:`CommandResult`.
        """
        try:
            # `toggle` is documented as a value `set` accepts, and the
            # script DSL has always taken it. The prompt refused it with
            # "must be true or false", which left the values that have no
            # named command of their own - the notification switches,
            # obstruction, the remote flags - invertible from a script but
            # not by hand. Same route either way: `toggle_named_value`
            # writes through `set_named_value`.
            if value.strip().lower() == "toggle":
                key = canonical_name(name)
                toggle_named_value(self.simulator, key)
            else:
                key = set_named_value(self.simulator, name, value)
        except CoercionError as exc:
            return CommandResult(False, str(exc))
        return CommandResult(True, render_value(key, self.simulator.state))


def _unknown(name: str) -> str:
    return t(
        "simulator.commands.values.unknown_value",
        "Unknown value: {name}. Use: {arg0}",
        name=name,
        arg0=", ".join(VALUE_NAMES),
    )


@dataclass(frozen=True)
class SwitchCommand:
    """One boolean value under the word an operator types for it.

    Attributes:
        value: The name in :data:`~powerpetdoor.simulator.values.VALUES`.
        name: The command word. Often differs from the value - the door's
            `inside` is the prompt's `inside_enable`, because `inside`
            already means "put a pet there".
        aliases: Shortcuts.
        label: How the reply names it.
        description: Help text.
        category: Help grouping.
        words: What the reply calls the two states.
        template: The reply, given ``label`` and ``state``.
        extra_subcommands: Words beyond ``toggle`` - `ac` has
            ``connect``/``disconnect``, which say the same thing plainly.
    """

    value: str
    name: str
    aliases: list[str]
    label: str
    description: str
    category: str
    words: tuple[str, str] = ("ON", "OFF")
    template: str = "{label}: {state}"
    extra_subcommands: list[SubcommandInfo] = field(default_factory=list)

    def message(self, on: bool) -> str:
        """The reply for a switch that is now ``on``."""
        return self.template.format(label=self.label, state=self.words[0 if on else 1])


_ENABLED = ("enabled", "disabled")

#: Every boolean value that has a word of its own at the prompt.
SWITCH_COMMANDS: tuple[SwitchCommand, ...] = (
    SwitchCommand("power", "power", ["p"], "Power", "Toggle or set power", "buttons"),
    SwitchCommand(
        "auto", "auto", ["m"], "Auto (schedule)", "Toggle or set auto/schedule mode", "buttons"
    ),
    SwitchCommand(
        "inside",
        "inside_enable",
        ["n"],
        "Inside sensor",
        "Toggle or set inside sensor enable",
        "buttons",
        _ENABLED,
    ),
    SwitchCommand(
        "outside",
        "outside_enable",
        ["u"],
        "Outside sensor",
        "Toggle or set outside sensor enable",
        "buttons",
        _ENABLED,
    ),
    SwitchCommand(
        "safety_lock",
        "safety",
        ["s"],
        "Safety lock",
        "Toggle or set outside sensor safety lock",
        "settings",
    ),
    SwitchCommand(
        "cmd_lockout",
        "lockout",
        ["l"],
        "Command lockout",
        "Toggle or set command lockout",
        "settings",
    ),
    SwitchCommand(
        "autoretract",
        "autoretract",
        ["a"],
        "Auto-retract",
        "Toggle or set auto-retract",
        "settings",
    ),
    SwitchCommand(
        "battery_present",
        "battery_present",
        ["bp"],
        "Battery",
        "Toggle or set battery presence",
        "settings",
        ("installed", "removed"),
    ),
    SwitchCommand(
        "ac_present",
        "ac",
        [],
        "AC power",
        "Toggle or set AC power connection",
        "settings",
        ("connected", "disconnected"),
        # Phrased as "AC set to ..." rather than "AC: ...": the latter is
        # how the read-only displays (`battery`, `holdtime`) phrase
        # themselves, so a bare `ac` would look like it was showing rather
        # than changing.
        "AC set to {state}",
        [
            SubcommandInfo("connect", ["c"], "Connect AC power"),
            SubcommandInfo("disconnect", ["d"], "Disconnect AC power"),
        ],
    ),
)


def _switch_value_arg() -> list[ArgSpec]:
    """The ``on|off`` argument every switch command takes."""
    return [ArgSpec("value", "bool_toggle", required=False, description="on/off or omit to toggle")]


def _switch_handler(switch: SwitchCommand, fixed: bool | None):
    """A switch command's body: set or invert, then say what it is now.

    ``fixed`` is the value a subcommand pins - ``ac connect`` is always
    True - or ``None`` for the main command, which takes it as an
    argument and toggles when that is omitted too.
    """

    def handle(self, value: bool | None = None) -> CommandResult:
        wanted = fixed if fixed is not None else value
        if wanted is None:
            new_state = toggle_named_value(self.simulator, switch.value)
        else:
            set_named_value(self.simulator, switch.value, wanted)
            new_state = wanted
        return CommandResult(True, switch.message(new_state))

    return handle


def _named(fn, name: str, doc: str):
    """Give a generated handler the name its registration is looked up by."""
    fn.__name__ = name
    fn.__qualname__ = f"ValueCommandsMixin.{name}"
    fn.__doc__ = doc
    return fn


def _generate_switch_commands() -> None:
    """Attach one command, plus its subcommands, per switch row.

    A command's handler is looked up by name off the mixin, so each
    generated function is set on the class under the name its
    ``CommandInfo`` was registered with.
    """
    for switch in SWITCH_COMMANDS:
        subs = [
            SubcommandInfo("toggle", ["t"], f"Toggle {switch.label.lower()}"),
            *switch.extra_subcommands,
        ]
        setattr(
            ValueCommandsMixin,
            switch.name,
            command(
                switch.name,
                switch.aliases,
                switch.description,
                category=switch.category,
                args=_switch_value_arg(),
                subcommands=subs,
            )(_named(_switch_handler(switch, None), switch.name, switch.description)),
        )

        pinned: dict[str, bool | None] = {"toggle": None, "connect": True, "disconnect": False}
        for info in subs:
            fname = f"{switch.name}_{info.name}"
            setattr(
                ValueCommandsMixin,
                fname,
                subcommand(switch.name, info.name, info.aliases, info.description)(
                    _named(_switch_handler(switch, pinned[info.name]), fname, info.description)
                ),
            )


_generate_switch_commands()

__all__ = [
    "SWITCH_COMMANDS",
    "WRITABLE",
    "SwitchCommand",
    "ValueCommandsMixin",
    "coerce_value",
    "render_value",
]
