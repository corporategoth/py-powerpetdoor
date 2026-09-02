# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Every value the simulator can show or change, in one table.

Three surfaces read this: the CLI's ``get``/``set``, the script DSL's
``set``, and the condition vocabulary a script asserts on. One entry
therefore serves all three, which is the point - parity between what
:class:`~powerpetdoor.door.PowerPetDoor` exposes and what a script can
reach is a property of this table rather than something maintained by
hand in three places. ``tests/simulator/test_values.py`` pins it against
the facade's own properties.

Two kinds live here, and the difference matters to anyone reading a
script:

- **Device values** exist on a real Power Pet Door and are what the wire
  protocol carries.
- **Simulation values** (:attr:`ValueSpec.simulation_only`) are the
  simulator's own knobs - how fast the flap moves, how fast the battery
  drains. No real door has them; they exist so a test can make a cycle
  take 60 ms instead of four seconds.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..const import DOOR_POSITIONS, DOOR_STATE_CLOSED, DOOR_STATE_POWEROFF, TIME_FORMAT
from ..i18n import t
from ..tz_utils import to_posix_tz
from .coerce import CoercionError, coerce_bool, coerce_number
from .notifications import NOTIFICATION_NAMES, NOTIFICATION_SETTINGS

if TYPE_CHECKING:
    from .server import DoorSimulator
    from .state import DoorSimulatorState

#: Bound shared by the two trigger voltages. Measured: the device stores a
#: signed 32-bit value and saturates there.
MAX_VOLTAGE = 2**31 - 1
#: Longest hold the operator surfaces accept, in seconds.
MAX_HOLD_TIME = 900.0
#: Bound on the simulation's own timings, in seconds.
MAX_TIMING = 60.0
#: Bound on a battery rate, in percent per minute.
MAX_RATE = 100.0


@dataclass(frozen=True)
class ValueSpec:
    """One named value: how to read it, how to write it, what it accepts.

    Attributes:
        kind: ``bool``, ``int``, ``number`` or ``text``. Decides how a
            supplied value is coerced and how a condition compares.
        get: Reads the value from a state.
        set: Applies a coerced value, including any broadcast a real
            change would send. ``None`` means read-only.
        minimum/maximum: Bounds for ``int``/``number``.
        simulation_only: True for the simulator's own knobs.
        description: One line, shown by ``get``/``set`` help.
    """

    kind: str
    get: Callable[[DoorSimulatorState], Any]
    description: str
    set: Callable[[DoorSimulator, Any], None] | None = None
    announce: Callable[[DoorSimulator, Any], None] | None = None
    minimum: float = 0.0
    maximum: float = 0.0
    simulation_only: bool = False
    #: Filled in from the table's own keys, so a row cannot be told its
    #: name twice and disagree with itself.
    name: str = ""

    @property
    def writable(self) -> bool:
        return self.set is not None

    def apply(self, sim: DoorSimulator, value: Any, *, announce: bool = True) -> None:
        """Write the value, run its side effects, and optionally announce it.

        **Side effects always run.** Enabling a sensor re-asks whether a
        pet already waiting at it may now come in, whoever asked - the
        prompt, a script, or an `ENABLE_INSIDE` off the wire. A door that
        admitted the pet for one source and not another would be a door
        whose behaviour depended on who was watching.

        ``announce`` is the part that differs by source: a wire command
        answers in its own response, so it does not also broadcast.

        *How* to announce is the simulator's business, not the value's - a
        value that has a wire spelling is pushed under its own command,
        and one that a real door only reports as part of something larger
        says so with an explicit :attr:`announce`.
        """
        if self.set is None:
            raise ValueError(t("simulator.values.read_only", "{name} is read-only", name=self.name))
        self.set(sim, value)
        if not announce:
            return
        if self.announce is not None:
            self.announce(sim, value)
        else:
            sim.announce_value(self.name)


def _flag(attr: str):
    """Write a boolean, then re-ask the sensor question it may have changed."""

    def setter(sim: DoorSimulator, value: Any) -> None:
        setattr(sim.state, attr, value)
        sim.notify_settings_changed()

    return setter


def _power(sim: DoorSimulator, value: Any) -> None:
    """Switch main power, dropping an open flap when it goes off.

    A door with no power cannot hold its flap up, so cutting power closes
    it. That is the door's behaviour, not one surface's, so it lives with
    the value rather than in whichever handler happened to notice.
    """
    sim.state.power = value
    if not value and sim.state.door_status != DOOR_STATE_CLOSED:
        sim.engine.close()
    sim.notify_settings_changed()


def _door_status(state: DoorSimulatorState) -> str:
    """The door state, as the unit reports it.

    A switched-off door answers ``DOOR_POWEROFF`` rather than any of the
    nine motion states - the flap is down and the motor will not run, so
    none of them describes it. Read through here rather than off
    ``state.door_status`` so the wire, the prompt and a script condition
    cannot disagree about what a powered-off door is doing.

    Whether the unit also *pushes* the change when power is switched off
    is unprobed, so nothing here broadcasts it; every reader simply tells
    the truth when asked.
    """
    if not state.power:
        return DOOR_STATE_POWEROFF
    return state.door_status


def _timezone(sim: DoorSimulator, value: Any) -> None:
    """Store a timezone, as POSIX, whichever spelling arrived.

    The wire carries POSIX and nothing else, so that is the canonical
    form. An operator surface may be handed an IANA name - `timezone
    America/New_York` at the prompt is an ordinary thing to type - and
    the conversion belongs here, at the one place the value is written,
    rather than on the way out to each reader.

    Raises:
        CoercionError: If it is neither a POSIX TZ string nor an IANA
            name that converts to one.
    """
    try:
        sim.state.timezone = to_posix_tz(str(value))
    except ValueError as exc:
        raise CoercionError(str(exc)) from None


def _number(attr: str, cast: Callable[[Any], Any] = float):
    """Write a numeric or text field."""

    def setter(sim: DoorSimulator, value: Any) -> None:
        setattr(sim.state, attr, cast(value))

    return setter


def _announce(broadcast: str):
    """Announce a change as part of a *larger* message.

    Only for values a real door does not report on their own - a trigger
    voltage arrives inside `settings`, a counter inside the open stats.
    Anything with a wire spelling of its own needs nothing here; the
    simulator derives it.
    """

    def announcer(sim: DoorSimulator, value: Any) -> None:
        getattr(sim, broadcast)()

    return announcer


def _timing(attr: str):
    """One of the simulation's own timings."""

    def setter(sim: DoorSimulator, value: Any) -> None:
        setattr(sim.state.timing, attr, float(value))

    return setter


def _version(kind: str):
    """Firmware or hardware version, spelled the way `status` shows it."""

    def setter(sim: DoorSimulator, value: Any) -> None:
        parts = [int(p) for p in str(value).split(".")]
        if kind == "firmware":
            sim.state.fw_major, sim.state.fw_minor, sim.state.fw_patch = (parts + [0, 0, 0])[:3]
        else:
            sim.state.hw_ver, sim.state.hw_rev = (parts + [0, 0])[:2]
        sim.broadcast_hardware_info()

    return setter


def _notification(name: str):
    def setter(sim: DoorSimulator, value: Any) -> None:
        setattr(sim.state, NOTIFICATION_SETTINGS[name], value)
        sim.broadcast_notification_settings()

    return setter


#: Every value, by name. Names match the script condition vocabulary, so
#: anything settable here is also assertable.
VALUES: dict[str, ValueSpec] = {
    # -- device: the switches -------------------------------------------
    "power": ValueSpec(
        "bool",
        lambda s: s.power,
        "Main power",
        _power,
    ),
    "auto": ValueSpec(
        "bool",
        lambda s: s.auto,
        "Schedules enabled",
        _flag("auto"),
    ),
    "inside": ValueSpec(
        "bool",
        lambda s: s.inside,
        "Inside sensor enabled",
        _flag("inside"),
    ),
    "outside": ValueSpec(
        "bool",
        lambda s: s.outside,
        "Outside sensor enabled",
        _flag("outside"),
    ),
    "autoretract": ValueSpec(
        "bool",
        lambda s: s.autoretract,
        "Auto-retract on obstruction",
        _flag("autoretract"),
    ),
    "safety_lock": ValueSpec(
        "bool",
        lambda s: s.safety_lock,
        "Outside sensor overrides the schedule",
        _flag("safety_lock"),
    ),
    "cmd_lockout": ValueSpec(
        "bool",
        lambda s: s.cmd_lockout,
        "Door ignores pet proximity and closes on its timer",
        _flag("cmd_lockout"),
    ),
    # -- device: the values ---------------------------------------------
    "hold_time": ValueSpec(
        "number",
        lambda s: s.hold_time,
        "Seconds the door stays open",
        _number("hold_time"),
        maximum=MAX_HOLD_TIME,
    ),
    "timezone": ValueSpec(
        "text",
        lambda s: s.timezone,
        "IANA name or POSIX TZ string; stored as POSIX",
        _timezone,
    ),
    "sensor_trigger_voltage": ValueSpec(
        "int",
        lambda s: s.sensor_trigger_voltage,
        "Collar detection threshold (mV)",
        _number("sensor_trigger_voltage", cast=int),
        _announce("broadcast_settings"),
        maximum=MAX_VOLTAGE,
    ),
    "sleep_sensor_trigger_voltage": ValueSpec(
        "int",
        lambda s: s.sleep_sensor_trigger_voltage,
        "Sleep-mode threshold (mV)",
        _number("sleep_sensor_trigger_voltage", cast=int),
        _announce("broadcast_settings"),
        maximum=MAX_VOLTAGE,
    ),
    # -- device: power source -------------------------------------------
    "battery": ValueSpec(
        "int",
        lambda s: s.battery_percent,
        "Battery charge (%)",
        lambda sim, v: sim.set_battery(int(v)),
        maximum=100,
    ),
    "battery_present": ValueSpec(
        "bool",
        lambda s: s.battery_present,
        "Whether a battery is fitted",
        lambda sim, v: sim.set_battery_present(v),
    ),
    "ac_present": ValueSpec(
        "bool",
        lambda s: s.ac_present,
        "Whether mains power is connected",
        lambda sim, v: sim.set_ac_present(v),
    ),
    # -- device: identity and diagnostics -------------------------------
    "firmware_version": ValueSpec(
        "text",
        lambda s: f"{s.fw_major}.{s.fw_minor}.{s.fw_patch}",
        "Reported firmware version",
        _version("firmware"),
    ),
    "hardware_version": ValueSpec(
        "text",
        lambda s: f"{s.hw_ver}.{s.hw_rev}",
        "Reported hardware version",
        _version("hardware"),
    ),
    "has_remote_id": ValueSpec(
        "bool",
        lambda s: s.has_remote_id,
        "Whether a remote ID is paired",
        _flag("has_remote_id"),
    ),
    "has_remote_key": ValueSpec(
        "bool",
        lambda s: s.has_remote_key,
        "Whether a remote key is paired",
        _flag("has_remote_key"),
    ),
    # -- device: counters and live state --------------------------------
    "total_open_cycles": ValueSpec(
        "int",
        lambda s: s.total_open_cycles,
        "Completed open/close cycles",
        _number("total_open_cycles", cast=int),
        _announce("broadcast_stats"),
        maximum=2**31 - 1,
    ),
    "total_auto_retracts": ValueSpec(
        "int",
        lambda s: s.total_auto_retracts,
        "Auto-retracts so far",
        _number("total_auto_retracts", cast=int),
        _announce("broadcast_stats"),
        maximum=2**31 - 1,
    ),
    "door_status": ValueSpec("text", _door_status, "Exact door state (read-only)"),
    "position": ValueSpec(
        "int",
        lambda s: DOOR_POSITIONS.get(_door_status(s), 0),
        "How far open, 0-100 (read-only)",
    ),
    "time": ValueSpec(
        "text",
        lambda s: datetime.now(s.get_tzinfo()).strftime(TIME_FORMAT),
        "The door's own clock, in its timezone (read-only)",
    ),
    "obstruction": ValueSpec(
        "bool",
        lambda s: s.obstruction_active,
        "Whether a physical obstruction is in the doorway",
        lambda sim, v: sim.simulate_obstruction(0) if v else sim.clear_obstruction(),
    ),
    # -- simulation only ------------------------------------------------
    "charge_rate": ValueSpec(
        "number",
        lambda s: s.battery_config.charge_rate,
        "Battery charge rate (%/min)",
        lambda sim, v: sim.set_charge_rate(float(v)),
        maximum=MAX_RATE,
        simulation_only=True,
    ),
    "discharge_rate": ValueSpec(
        "number",
        lambda s: s.battery_config.discharge_rate,
        "Battery drain rate (%/min)",
        lambda sim, v: sim.set_discharge_rate(float(v)),
        maximum=MAX_RATE,
        simulation_only=True,
    ),
    **{
        name: ValueSpec(
            "number",
            (lambda a: lambda s: getattr(s.timing, a))(name),
            f"Simulated {name.replace('_', ' ')} (seconds)",
            _timing(name),
            maximum=MAX_TIMING,
            simulation_only=True,
        )
        for name in (
            "rise_time",
            "slowing_time",
            "closing_start_time",
            "closing_top_time",
            "closing_mid_time",
            "sensor_retrigger_window",
        )
    },
    # -- device: the notification switches ------------------------------
    **{
        f"notify_{name}": ValueSpec(
            "bool",
            (lambda a: lambda s: getattr(s, a))(NOTIFICATION_SETTINGS[name]),
            f"Whether the {name} notification is switched on",
            _notification(name),
        )
        for name in NOTIFICATION_NAMES
    },
}

# A row is told its name once - here, from the key it is stored under.
for _name, _spec in VALUES.items():
    object.__setattr__(_spec, "name", _name)
del _name, _spec

#: Names that can be written, in a stable order.
WRITABLE: tuple[str, ...] = tuple(sorted(n for n, v in VALUES.items() if v.writable))
#: Every name, in a stable order.
VALUE_NAMES: tuple[str, ...] = tuple(sorted(VALUES))

#: The boolean subset - the only rows `toggle` means anything for.
TOGGLEABLE: tuple[str, ...] = tuple(
    sorted(n for n, v in VALUES.items() if v.writable and v.kind == "bool")
)


def coerce_value(name: str, spec: ValueSpec, raw: object) -> Any:
    """Coerce a supplied value to what its entry accepts.

    The name is carried through so a refusal says *which* value was wrong,
    which is the whole difference between "must be a finite number" and
    "hold_time must be a finite number" in a CI log.

    Raises:
        CoercionError: If the value is not usable for that kind.
    """
    if spec.kind == "bool":
        return coerce_bool(raw, name)
    if spec.kind in ("int", "number"):
        # float() so a bound written as `100` and one written as `100.0`
        # produce the same message.
        number = coerce_number(raw, name, float(spec.minimum), float(spec.maximum))
        return int(number) if spec.kind == "int" else number
    return str(raw)


def read_value(state: DoorSimulatorState, name: str) -> Any:
    """Read one value, through the accessor every surface shares.

    The counterpart to :func:`set_named_value`. Interface layers read
    through this rather than off the state object, so a value that grows
    tracing, moves to different storage, or starts proxying real hardware
    changes in one place. What each layer does with the value afterwards -
    the wire's ``"true"``/``"false"`` strings and centiseconds, the
    prompt's ``ON``/``OFF`` - is that layer's own translation.
    """
    return VALUES[name].get(state)


def canonical_name(name: str) -> str:
    """The registry spelling of a name typed at a prompt or in YAML."""
    return name.lower().replace("-", "_")


def _writable_spec(name: str, *, toggling: bool = False) -> ValueSpec:
    """Look up a writable row, or say precisely why it is not one.

    Raises:
        CoercionError: If there is no such value, or it cannot be written
            (or, when ``toggling``, does not hold a yes/no state).
    """
    spec = VALUES.get(name)
    if spec is None or (toggling and not spec.writable):
        raise CoercionError(
            t(
                "simulator.values.unknown_toggle" if toggling else "simulator.values.unknown",
                "Unknown setting to toggle: {name}. Use: {arg0}"
                if toggling
                else "Unknown setting: {name}. Use: {arg0}",
                name=name,
                arg0=", ".join(TOGGLEABLE if toggling else WRITABLE),
            )
        )
    if toggling and spec.kind != "bool":
        raise CoercionError(
            t(
                "simulator.values.not_a_state",
                "Cannot toggle {name}: it holds a value, not a state. Toggleable: {arg0}",
                name=name,
                arg0=", ".join(TOGGLEABLE),
            )
        )
    if not spec.writable:
        raise CoercionError(t("simulator.values.read_only", "{name} is read-only", name=name))
    return spec


def set_named_value(sim: DoorSimulator, name: str, value: Any, *, announce: bool = True) -> str:
    """Resolve, refuse, coerce and apply - the one way a value is written.

    The prompt's ``set``, the script DSL's ``set`` and the control socket
    all land here, so a value is refused for the same reasons and applied
    with the same side effects and the same broadcast whoever wrote it.

    Args:
        sim: The simulator to write to.
        name: Value name, in any spelling :func:`canonical_name` accepts.
        value: The value as supplied, of any type.
        announce: Whether to broadcast the change to connected clients.
            A wire command answers in its own response and passes False.

    Returns:
        The canonical name written.

    Raises:
        CoercionError: If the name is not a writable value, or the value
            is not usable for it.
    """
    name = canonical_name(name)
    spec = _writable_spec(name)
    spec.apply(sim, coerce_value(name, spec, value), announce=announce)
    return name


def toggle_named_value(sim: DoorSimulator, name: str, *, announce: bool = True) -> bool:
    """Invert a boolean value through :func:`set_named_value`'s route.

    Returns:
        The new state.

    Raises:
        CoercionError: If the name is not a writable yes/no value.
    """
    name = canonical_name(name)
    spec = _writable_spec(name, toggling=True)
    new_value = not spec.get(sim.state)
    spec.apply(sim, new_value, announce=announce)
    return new_value
