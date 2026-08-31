# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Every source applies a change the same way.

The simulator has four ways in - the prompt, a script, the control
socket, and the wire - and each of them used to write state its own way.
That is a door whose behaviour depends on who is watching: `inside_enable
on` from the prompt left a waiting pet outside, while `CMD_ENABLE_INSIDE`
off the wire let it in.

These tests pin the shared routes rather than the symptom, so a fifth
source, or a sixth value, cannot quietly reintroduce a second path.
"""

from __future__ import annotations

import ast
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from powerpetdoor.const import (
    CMD_DELETE_SCHEDULE,
    CMD_ENABLE_INSIDE,
    CMD_GET_SETTINGS,
    CMD_GET_TIMEZONE,
    CMD_POWER_OFF,
    CMD_SET_SCHEDULE,
    CMD_SET_TIMEZONE,
    CONFIG,
    DOOR_STATE_CLOSED,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    FIELD_AUTO,
    FIELD_CMD,
    FIELD_CMD_LOCKOUT,
    FIELD_HOLD_OPEN_TIME,
    FIELD_HOLD_TIME,
    FIELD_HOUR,
    FIELD_INDEX,
    FIELD_INSIDE_PREFIX,
    FIELD_MINUTE,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_REASON,
    FIELD_SCHEDULE,
    FIELD_SETTINGS,
    FIELD_START_TIME_SUFFIX,
    FIELD_SUCCESS,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    FIELD_VOLTAGE,
    SUCCESS_FALSE,
    SUCCESS_TRUE,
)
from powerpetdoor.schedule import (
    MAX_SCHEDULE_HOUR,
    MAX_SCHEDULE_MINUTE,
    coerce_schedule_time,
    valid_schedule_time,
    wire_bool_string,
)
from powerpetdoor.simulator import DoorSimulator, DoorSimulatorState, DoorTimingConfig
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.commands.base import _parse_time_str
from powerpetdoor.simulator.protocol import CommandRegistry, DoorSimulatorProtocol
from powerpetdoor.simulator.scripting import Script, ScriptRunner
from powerpetdoor.simulator.state import Schedule
from powerpetdoor.simulator.state_io import StateDocumentError
from powerpetdoor.simulator.state_io import _parse_hhmm as parse_time_of_day
from powerpetdoor.simulator.values import VALUE_NAMES, VALUES, read_value
from powerpetdoor.simulator.wire_values import (
    FIELD_DOCS,
    MAX_HOLD_TIME_CENTISECONDS,
    MAX_TRIGGER_VOLTAGE,
    OBJECT_FIELD_DOCS,
    WIRE_BOUNDS,
    WIRE_SWITCHES,
    WIRE_VALUES,
    settings_payload,
    wire_bounds,
)

SIMULATOR_SOURCE = pathlib.Path(DoorSimulator.__module__.replace(".", "/") + ".py")
PROTOCOL_SOURCE = pathlib.Path("src/powerpetdoor/simulator/protocol.py")


@pytest.fixture
def timing():
    return DoorTimingConfig(
        rise_time=0.02,
        slowing_time=0.02,
        closing_start_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
    )


@pytest.fixture
async def sim(timing):
    simulator = DoorSimulator(port=0, state=DoorSimulatorState(timing=timing, hold_time=0.2))
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest.fixture
def handler(sim):
    return CommandHandler(simulator=sim, script_runner=ScriptRunner(sim), stop_callback=MagicMock())


@pytest.fixture
async def protocol(sim):
    proto = DoorSimulatorProtocol(sim.state, engine=sim.engine, simulator=sim)
    proto.connection_made(_mock_transport())
    sim.protocols.append(proto)
    yield proto
    await proto.aclose()


def _mock_transport():
    transport = MagicMock()
    transport.get_write_buffer_size.return_value = 0
    return transport


async def dispatch(protocol, msg: dict) -> None:
    """Feed one message to the protocol and wait for it to be handled."""
    protocol.data_received(json.dumps(msg).encode("ascii"))
    await protocol.drain()


async def _pet_waiting(sim) -> None:
    """A pet at a switched-off inside sensor, refused and still standing there."""
    sim.state.inside = False
    sim.hold_sensor("inside", True)
    assert sim.state.door_status == DOOR_STATE_CLOSED


# ============================================================================
# Enabling a sensor admits a waiting pet, whoever enables it
# ============================================================================


class TestSensorEnableAdmitsWaitingPet:
    """The side effect that made the sources visibly disagree.

    A pet held at a disabled sensor is recorded but refused. Re-enabling
    the sensor has to re-ask the question, or the pet stays outside until
    it moves away and comes back - which a pet that never moved does not
    do.
    """

    async def test_from_the_prompt(self, sim, handler):
        await _pet_waiting(sim)
        await handler.execute("inside_enable on")
        assert sim.state.door_status == DOOR_STATE_RISING

    async def test_from_a_script(self, sim):
        await _pet_waiting(sim)
        await ScriptRunner(sim).run(Script.from_simple_commands(["set inside on"]), verbose=False)
        assert sim.state.door_status == DOOR_STATE_RISING

    async def test_from_the_registry(self, sim, handler):
        await _pet_waiting(sim)
        await handler.execute("set inside on")
        assert sim.state.door_status == DOOR_STATE_RISING

    async def test_from_the_wire(self, sim, protocol):
        await _pet_waiting(sim)
        await dispatch(protocol, {CONFIG: CMD_ENABLE_INSIDE, "msgId": 1})
        assert sim.state.door_status == DOOR_STATE_RISING


# ============================================================================
# Power off drops an open flap, whoever cuts power
# ============================================================================


class TestPowerOffClosesTheDoor:
    """A door with no power cannot hold its flap up.

    This lived in the wire handler alone, so `power off` from the prompt
    left the door standing open - the same command, two behaviours.
    """

    async def test_from_the_prompt(self, sim, handler):
        await sim.open_door(hold=True)
        await sim.engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        await handler.execute("power off")
        assert await sim.engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0) == DOOR_STATE_CLOSED

    async def test_from_the_wire(self, sim, protocol):
        await sim.open_door(hold=True)
        await sim.engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        await dispatch(protocol, {CONFIG: CMD_POWER_OFF, "msgId": 1})
        assert await sim.engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0) == DOOR_STATE_CLOSED

    async def test_power_on_leaves_a_closed_door_closed(self, sim, handler):
        """Only the off edge closes: switching power on must not move the flap."""
        sim.state.power = False
        await handler.execute("power on")
        assert sim.state.door_status == DOOR_STATE_CLOSED


# ============================================================================
# The wire reaches device values only
# ============================================================================


class TestWireCannotReachSimulationValues:
    """The simulation's own knobs have no wire spelling, and must not get one.

    Flap timings and battery rates exist so a test can make a cycle take
    60 ms. A real door has no such field, so a simulator that accepted one
    over TCP 3000 would be simulating a door that does not exist.
    """

    def test_apply_value_refuses_a_simulation_only_name(self, protocol):
        assert VALUES["rise_time"].simulation_only is True
        with pytest.raises(KeyError):
            protocol._apply_value("rise_time", 1.0)

    def test_apply_value_accepts_the_device_twin(self, protocol, sim):
        """The refusal is about the value, not about `_apply_value` itself."""
        protocol._apply_value("hold_time", 12.0)
        assert sim.state.hold_time == 12.0

    def test_no_wire_handler_names_a_simulation_value(self):
        """Every value the protocol names by literal is a device value."""
        tree = ast.parse(PROTOCOL_SOURCE.read_text())
        named = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_apply_value"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                named.add(node.args[0].value)
        assert named, "no _apply_value call sites found - has the route been renamed?"
        assert not [n for n in named if VALUES[n].simulation_only]

    def test_wire_settings_expose_no_simulation_value(self, sim):
        """Nor may a simulation knob leak out through a GET response."""
        sim.state.timing.rise_time = 7.25
        sim.state.battery_config.discharge_rate = 3.5
        settings = settings_payload(sim.state)
        assert 7.25 not in settings.values()
        assert 3.5 not in settings.values()
        assert not [k for k in settings if "rate" in k.lower() or "_time" in k.lower()]


# ============================================================================
# One writer per concern
# ============================================================================


class TestOnlyOneWriter:
    """Drift guards: a second writer is what these fixes removed."""

    def test_protocol_never_assigns_state_directly(self):
        """A wire handler that assigns to `state` has left the shared route."""
        tree = ast.parse(PROTOCOL_SOURCE.read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if _is_self_state_attribute(target):
                    offenders.append(f"{target.attr} (line {node.lineno})")
        assert offenders == []

    def test_protocol_never_mutates_a_state_container_directly(self):
        """Nor does it reach past the route with `.clear()` or `.pop()`.

        A wholesale replacement used to empty `state.schedules` in place,
        so it announced the arrivals and swallowed every departure.
        Assignment was already guarded; the mutating calls were the way
        around that guard.
        """
        mutators = {"clear", "pop", "popitem", "update", "setdefault", "append", "extend"}
        tree = ast.parse(PROTOCOL_SOURCE.read_text())
        offenders = [
            f"{node.func.value.attr}.{node.func.attr} (line {node.lineno})"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in mutators
            and _is_self_state_attribute(node.func.value)
        ]
        assert offenders == []

    def test_toggle_decision_lives_only_on_the_simulator(self):
        """Neither the prompt nor a script re-derives which way toggle goes.

        Both used to compare the door status themselves, and had already
        drifted: the prompt reported the mid-travel no-op and the script
        silently did nothing. Each surface's toggle path may now call
        `toggle_door` and nothing else that moves the door.
        """
        moves_the_door = {"open_door", "close_door"}
        for path, calls in (
            ("src/powerpetdoor/simulator/commands/door.py", _cli_toggle_calls()),
            ("src/powerpetdoor/simulator/scripting.py", _script_toggle_calls()),
        ):
            assert "toggle_door" in calls, f"{path} does not delegate to toggle_door"
            assert not (calls & moves_the_door), f"{path} still moves the door itself"


def _is_self_state_attribute(node) -> bool:
    """True for ``self.state.<something>``."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "state"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    )


def _calls_in(node) -> set[str]:
    return {
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


def _cli_toggle_calls() -> set[str]:
    """Calls made by the prompt's `toggle` command."""
    tree = ast.parse(pathlib.Path("src/powerpetdoor/simulator/commands/door.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "toggle":
            return _calls_in(node)
    raise AssertionError("no `toggle` command found in commands/door.py")


def _script_toggle_calls() -> set[str]:
    """Calls made by the script DSL's `toggle` action.

    The action is a branch rather than a function, so the branch is found
    by its test - `action == "toggle"` - and only its body is read.
    """
    tree = ast.parse(pathlib.Path("src/powerpetdoor/simulator/scripting.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        left, comparators = node.test.left, node.test.comparators
        if (
            isinstance(left, ast.Name)
            and left.id == "action"
            and isinstance(comparators[0], ast.Constant)
            and comparators[0].value == "toggle"
        ):
            return {c for stmt in node.body for c in _calls_in(stmt)}
    raise AssertionError('no `action == "toggle"` branch found in scripting.py')


# ============================================================================
# toggle_door, the single decision
# ============================================================================


class TestToggleDoor:
    async def test_closed_opens_and_holds(self, sim):
        assert await sim.toggle_door() == "open"
        assert await sim.engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0) == DOOR_STATE_KEEPUP

    async def test_open_closes(self, sim):
        await sim.open_door(hold=True)
        await sim.engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        assert await sim.toggle_door() == "close"

    async def test_in_travel_does_nothing(self, sim):
        """Nothing but an obstruction interrupts a door in motion."""
        await sim.open_door(hold=True)
        await sim.engine.wait_for_status(DOOR_STATE_RISING, timeout=2.0)
        assert await sim.toggle_door() is None
        assert sim.state.door_status == DOOR_STATE_RISING

    async def test_prompt_and_script_agree_in_travel(self, sim, handler):
        """The two surfaces that used to disagree, pinned against each other."""
        await sim.open_door(hold=True)
        await sim.engine.wait_for_status(DOOR_STATE_RISING, timeout=2.0)

        await handler.execute("toggle")
        assert sim.state.door_status == DOOR_STATE_RISING

        await ScriptRunner(sim).run(Script.from_simple_commands(["toggle"]), verbose=False)
        assert sim.state.door_status == DOOR_STATE_RISING


# ============================================================================
# Schedule maintenance
# ============================================================================


class TestScheduleMaintenance:
    """Add, remove and replace, from every source, through the same two verbs."""

    @staticmethod
    def _schedule(index: int) -> Schedule:
        return Schedule(
            index=index,
            enabled=True,
            days_of_week=[True] * 7,
            inside=True,
            start_hour=6,
            start_min=0,
            end_hour=22,
            end_min=0,
        )

    async def test_wire_add_and_delete(self, sim, protocol):
        await dispatch(
            protocol,
            {
                CONFIG: CMD_SET_SCHEDULE,
                "msgId": 1,
                FIELD_INDEX: 3,
                FIELD_SCHEDULE: self._schedule(3).to_dict(),
            },
        )
        assert 3 in sim.state.schedules
        await dispatch(protocol, {CONFIG: CMD_DELETE_SCHEDULE, "msgId": 2, FIELD_INDEX: 3})
        assert 3 not in sim.state.schedules

    async def test_prompt_add_and_delete(self, sim, handler):
        assert (await handler.execute("schedule add inside 6:00-22:00")).success is True
        assert 0 in sim.state.schedules
        assert (await handler.execute("schedule delete 0")).success is True
        assert 0 not in sim.state.schedules

    async def test_script_add_and_remove(self, sim):
        runner = ScriptRunner(sim)
        await runner.run(
            Script.from_simple_commands(["add_schedule 5", "remove_schedule 5"]), verbose=False
        )
        assert 5 not in sim.state.schedules

    async def test_replacing_the_table_announces_every_departure(self, sim):
        """The gap a wholesale replace had: departures went out silently."""
        sim.add_schedule(self._schedule(1), announce=False)
        sim.add_schedule(self._schedule(2), announce=False)
        deleted: list[int] = []
        added: list[int] = []
        sim.broadcast_schedule_delete = deleted.append
        sim.broadcast_schedule = lambda s: added.append(s.index)

        sim.set_schedules([self._schedule(2), self._schedule(7)])

        assert deleted == [1]
        assert added == [2, 7]
        assert sorted(sim.state.schedules) == [2, 7]

    async def test_prompt_clear_empties_the_table(self, sim, handler):
        sim.add_schedule(self._schedule(1), announce=False)
        sim.add_schedule(self._schedule(2), announce=False)
        result = await handler.execute("schedule clear")
        assert result.success is True
        assert sim.state.schedules == {}


# ============================================================================
# A pet arriving raises the notification, whichever verb put it there
# ============================================================================


class TestArrivalNotifications:
    """The notification reports the *event*, not the verb that caused it.

    Only `trigger_sensor` used to raise one, so `trigger inside` notified
    and `inside on` did not - the same pet at the same sensor, reported or
    silent depending on which word the operator typed.
    """

    @pytest.fixture(autouse=True)
    def _enable_both(self, sim):
        sim.state.sensor_on_indoor = True
        sim.state.sensor_off_indoor = True

    @staticmethod
    def _listen(sim) -> list[str]:
        seen: list[str] = []
        sim.add_notification_listener(seen.append)
        return seen

    async def test_hold(self, sim):
        seen = self._listen(sim)
        sim.hold_sensor("inside", True)
        assert seen == ["inside_on"]

    async def test_timed_activation(self, sim):
        seen = self._listen(sim)
        sim.engine.activate_sensor("inside", 5.0)
        assert seen == ["inside_on"]

    async def test_toggle_on(self, sim):
        seen = self._listen(sim)
        sim.engine.activate_sensor("inside", 0)
        assert seen == ["inside_on"]

    async def test_pass_through(self, sim):
        seen = self._listen(sim)
        sim.engine.trigger_sensor("inside")
        assert seen == ["inside_on"]

    async def test_from_the_prompt(self, sim, handler):
        seen = self._listen(sim)
        await handler.execute("inside on")
        assert seen == ["inside_on"]

    async def test_from_a_script(self, sim):
        seen = self._listen(sim)
        await ScriptRunner(sim).run(Script.from_simple_commands(["inside on"]), verbose=False)
        assert seen == ["inside_on"]

    async def test_a_disabled_sensor_raises_the_off_notification(self, sim):
        """The `_off` half names the sensor's switch, not the pet."""
        sim.state.inside = False
        seen = self._listen(sim)
        sim.hold_sensor("inside", True)
        assert seen == ["inside_off"]

    async def test_holding_an_already_present_pet_raises_nothing(self, sim):
        """Nothing arrived, so nothing is reported."""
        sim.hold_sensor("inside", True)
        seen = self._listen(sim)
        sim.hold_sensor("inside", True)
        assert seen == []

    async def test_a_settings_change_raises_nothing(self, sim):
        """`reevaluate_sensors` re-asks the question about a pet already there.

        It runs on every settings change, so notifying from the shared
        open gate would have reported an arrival every time anyone
        touched a switch.
        """
        sim.state.inside = False
        sim.hold_sensor("inside", True)
        seen = self._listen(sim)

        await handler_free_enable(sim)

        assert seen == []
        assert sim.state.door_status == DOOR_STATE_RISING

    async def test_toggling_off_raises_nothing(self, sim):
        """A pet leaving is not an arrival."""
        sim.hold_sensor("inside", True)
        seen = self._listen(sim)
        sim.engine.activate_sensor("inside", 0)
        assert seen == []
        assert sim.state.inside_sensor_active is False

    async def test_arriving_at_the_other_side_reports_that_side(self, sim):
        """Presence is exclusive; the arrival reported is the one that happened."""
        sim.state.sensor_on_outdoor = True
        sim.hold_sensor("inside", True)
        seen = self._listen(sim)

        sim.hold_sensor("outside", True)

        assert seen == ["outside_on"]
        assert sim.state.inside_sensor_active is False


async def handler_free_enable(sim) -> None:
    """Flip the inside sensor on through the value registry."""
    VALUES["inside"].apply(sim, True)


# ============================================================================
# The wire table
# ============================================================================


class TestWireTable:
    """One description of how a value appears on the wire.

    The response a command answers with and the broadcast an unsolicited
    change sends were written out separately, and had already diverged:
    `SET_TIMEZONE` echoed the raw IANA name while its broadcast, and every
    getter, sent the POSIX form.
    """

    def test_every_row_names_a_device_value(self):
        for name in WIRE_VALUES:
            assert name in VALUES, name
            assert VALUES[name].simulation_only is False, name

    def test_switches_are_exactly_the_rows_with_a_disable(self):
        assert WIRE_SWITCHES == tuple(
            sorted(n for n, w in WIRE_VALUES.items() if w.disable is not None)
        )

    def test_every_switch_is_a_boolean_value(self):
        for name in WIRE_SWITCHES:
            assert VALUES[name].kind == "bool", name

    def test_command_for_picks_by_truth(self):
        wire = WIRE_VALUES["power"]
        assert wire.command_for(True) == wire.enable
        assert wire.command_for(False) == wire.disable

    def test_a_value_with_no_disable_answers_one_command(self):
        wire = WIRE_VALUES["hold_time"]
        assert wire.disable is None
        assert wire.command_for(True) == wire.enable
        assert wire.command_for(False) == wire.enable

    def test_every_switch_command_has_a_registered_handler(self):
        """The generated pairs really are reachable off the wire."""
        for name in WIRE_SWITCHES:
            wire = WIRE_VALUES[name]
            assert CommandRegistry.get(wire.enable) is not None, wire.enable
            assert CommandRegistry.get(wire.disable) is not None, wire.disable

    async def test_response_and_broadcast_carry_the_same_payload(self, sim, protocol):
        """The property the table exists to guarantee."""
        for name in WIRE_SWITCHES:
            wire = WIRE_VALUES[name]
            for cmd, enabled in ((wire.enable, True), (wire.disable, False)):
                await dispatch(protocol, {CONFIG: cmd, "msgId": 1})
                answered = json.loads(protocol.transport.write.call_args[0][0].decode("ascii"))
                sent: list[dict] = []
                sim.send_to_clients = lambda c, p, _s=sent: _s.append({FIELD_CMD: c, **p})
                sim.broadcast_value(name)
                del sim.send_to_clients

                assert sent[0][FIELD_CMD] == cmd, name
                for key, value in sent[0].items():
                    assert answered[key] == value, f"{name}.{key} (enabled={enabled})"

    def test_get_settings_agrees_with_the_registry(self, sim):
        """`settings` spells these differently; it must not *mean* them differently.

        The bulk object uses its own field names and encodings - all
        firmware-verified, and deliberately not merged with the per-value
        payloads. What must hold is that both read the same value.
        """
        sim.state.power = False
        sim.state.auto = True
        sim.state.safety_lock = True
        sim.state.cmd_lockout = False
        sim.state.hold_time = 7.5
        settings = settings_payload(sim.state)

        for name, field in (
            ("power", FIELD_POWER),
            ("auto", FIELD_AUTO),
            ("safety_lock", FIELD_OUTSIDE_SENSOR_SAFETY_LOCK),
            ("cmd_lockout", FIELD_CMD_LOCKOUT),
        ):
            assert settings[field] == wire_bool_string(VALUES[name].get(sim.state)), name
        assert settings[FIELD_HOLD_OPEN_TIME] == int(VALUES["hold_time"].get(sim.state) * 100)


# ============================================================================
# No interface layer touches storage
# ============================================================================


#: The simulator's core: the state, the things that own it, and the value
#: registry itself. Everything else is an interface onto it.
CORE_MODULES = frozenset(
    {"state.py", "engine.py", "server.py", "values.py", "state_io.py", "notifications.py"}
)

#: Methods on the state that *are* the shared interface, so calling them
#: is not a bypass.
STATE_API = frozenset({"pet_present", "get_tzinfo", "is_sensor_allowed_by_schedule"})

#: The storage a registry row reads. Some rows spell their value
#: differently from the field behind it - `battery` reads
#: `battery_percent`, `firmware_version` reads three separate ints - so
#: the guard has to know the fields as well as the names.
_VALUE_ATTRIBUTES = frozenset(
    {
        "ac_present",
        "auto",
        "autoretract",
        "battery_config",
        "battery_percent",
        "battery_present",
        "cmd_lockout",
        "door_status",
        "fw_major",
        "fw_minor",
        "fw_patch",
        "has_remote_id",
        "has_remote_key",
        "hold_time",
        "hw_rev",
        "hw_ver",
        "inside",
        "obstruction_active",
        "outside",
        "power",
        "safety_lock",
        "sensor_trigger_voltage",
        "sleep_sensor_trigger_voltage",
        "timezone",
        "timing",
        "total_auto_retracts",
        "total_open_cycles",
    }
)


def _state_attribute_uses(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every ``<something>.state.<attr>`` in a module."""
    return [
        (node.lineno, node.attr)
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "state"
    ]


class TestInterfacesGoThroughTheCommonAccessor:
    """The wire, the prompt and the script DSL read values one way.

    The wire spells a value differently from the prompt - `"true"` where
    the prompt says `ON`, centiseconds where it says seconds - and that
    translation is each layer's own business. What is not its business is
    *where the value comes from*. Reaching past the accessor to the
    attribute works right up until a value grows tracing, moves storage,
    or starts proxying a real door, at which point every bypass is a
    separate thing to find.
    """

    def test_no_interface_module_reads_a_value_off_the_state(self):
        root = pathlib.Path("src/powerpetdoor/simulator")
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path.name in CORE_MODULES:
                continue
            for line, attr in _state_attribute_uses(path):
                if attr in VALUES or attr in _VALUE_ATTRIBUTES:
                    offenders.append(f"{path.relative_to(root)}:{line} state.{attr}")
        assert offenders == []

    def test_the_only_state_methods_reached_are_the_shared_api(self):
        """A helper the core exposes is fine; a storage field is not."""
        root = pathlib.Path("src/powerpetdoor/simulator")
        reached = set()
        for path in sorted(root.rglob("*.py")):
            if path.name in CORE_MODULES:
                continue
            reached.update(attr for _, attr in _state_attribute_uses(path))
        allowed = STATE_API | set(VALUES) | _VALUE_ATTRIBUTES
        assert reached <= allowed, sorted(reached - allowed)

    def test_read_value_and_the_registry_agree(self, sim):
        for name in VALUE_NAMES:
            assert read_value(sim.state, name) == VALUES[name].get(sim.state), name

    def test_schedules_are_reachable_without_touching_the_dict(self, sim):
        """Schedules have shared writers; they need a shared reader too."""
        schedule = TestScheduleMaintenance._schedule(2)
        sim.add_schedule(schedule, announce=False)

        assert sim.get_schedule(2) == schedule
        assert sim.get_schedule(99) is None
        assert dict(sim.get_schedules()) == {2: schedule}

    def test_the_schedule_view_is_read_only(self, sim):
        """Handing out the live dict would be the bypass with extra steps."""
        with pytest.raises(TypeError):
            sim.get_schedules()[5] = TestScheduleMaintenance._schedule(5)


# ============================================================================
# Surface parity
# ============================================================================


#: Prompt words that are the interactive session itself, not door
#: behaviour. A script has no session to inspect or leave.
SESSION_ONLY = frozenset(
    {
        "broadcast",
        "clear",
        "debug",
        "exit",
        "get",
        "help",
        "history",
        "list",
        "run",
        "shutdown",
        "status",
        "stop",
    }
)

#: Script words that are the script language itself. There is nothing at a
#: prompt for them to mean - you do not branch an interactive session.
SCRIPT_LANGUAGE_ONLY = frozenset({"assert", "if", "log", "repeat", "wait", "wait_for"})

#: ``(prompt word, script action)`` for one capability the two surfaces
#: spell differently. Each pair is a deliberate choice, not a gap.
SPELLING: tuple[tuple[str, str], ...] = (
    # Every named switch is `set <value>` in a script. `inside` already
    # means "put a pet there" at the prompt, so the enable needs its own
    # word there and cannot simply be called `inside` in both places.
    ("power", "set"),
    ("auto", "set"),
    ("inside_enable", "set"),
    ("outside_enable", "set"),
    ("safety", "set"),
    ("lockout", "set"),
    ("autoretract", "set"),
    ("ac", "set"),
    ("battery_present", "set"),
    ("charge_rate", "set"),
    ("discharge_rate", "set"),
    ("holdtime", "set"),
    ("timezone", "set"),
    # The prompt nests schedule work under one word; a script spells each
    # operation flat, because a script step has no subcommands.
    ("schedule", "add_schedule"),
    ("schedule", "remove_schedule"),
    ("schedule", "enable_schedule"),
    ("schedule", "clear_schedules"),
)

PROMPT_SPELLINGS = frozenset(word for word, _ in SPELLING)
SCRIPT_SPELLINGS = frozenset(action for _, action in SPELLING)


class TestSurfaceParity:
    """Anything the door can be told to do, every operator surface can say.

    The prompt, the script DSL and the control socket are three ways to
    reach one simulator. Where one of them cannot say something the others
    can, that is a gap - and it is how `trigger` ended up scriptable and
    controllable but impossible to type.
    """

    @staticmethod
    def _cli_words() -> set[str]:
        import powerpetdoor.simulator.commands.handler  # noqa: F401
        from powerpetdoor.simulator.commands.base import get_command_registry

        return {info.name for info in get_command_registry().values()}

    def test_every_prompt_word_is_reachable_from_a_script(self):
        from powerpetdoor.simulator.scripting import _ACTION_PARAMS

        unreachable = {
            word
            for word in self._cli_words()
            if word not in _ACTION_PARAMS
            and word not in SESSION_ONLY
            and word not in PROMPT_SPELLINGS
        }
        assert unreachable == set()

    def test_every_script_action_is_reachable_from_the_prompt(self):
        from powerpetdoor.simulator.scripting import _ACTION_PARAMS

        words = self._cli_words()
        unreachable = {
            action
            for action in _ACTION_PARAMS
            if action not in words
            and action not in SCRIPT_LANGUAGE_ONLY
            and action not in SCRIPT_SPELLINGS
        }
        assert unreachable == set()

    def test_every_spelling_pair_names_things_that_exist(self):
        """The map is an exemption list; a stale entry hides a real gap."""
        from powerpetdoor.simulator.scripting import _ACTION_PARAMS

        words = self._cli_words()
        for prompt_word, script_action in SPELLING:
            assert prompt_word in words, prompt_word
            assert script_action in _ACTION_PARAMS, script_action

    def test_every_writable_value_is_reachable_from_every_operator_surface(self):
        """`set` covers the registry, so this is about the registry's reach."""
        from powerpetdoor.simulator.values import WRITABLE

        assert set(WRITABLE) <= set(VALUES)
        assert all(VALUES[name].writable for name in WRITABLE)

    def test_the_control_socket_can_reach_anything_the_prompt_can(self):
        """`execute` is the whole surface; the named methods are shortcuts."""
        from powerpetdoor.simulator.control import SimulatorController

        assert callable(SimulatorController.execute)
        for shortcut in ("open", "close_door", "cycle", "toggle", "trigger", "obstruction"):
            assert hasattr(SimulatorController, shortcut), shortcut

    def test_the_wire_reaches_device_values_and_no_others(self):
        """The one surface that is *meant* to reach less."""
        for name in WIRE_VALUES:
            assert VALUES[name].simulation_only is False, name
        simulation = {n for n in VALUES if VALUES[n].simulation_only}
        assert simulation & set(WIRE_VALUES) == set()


class TestTriggerReachesEverySurface:
    """A pass-through, which used to be scriptable but not typeable."""

    async def test_from_the_prompt(self, sim, handler):
        sim.state.sensor_on_indoor = True
        seen: list[str] = []
        sim.add_notification_listener(seen.append)

        result = await handler.execute("trigger inside")

        assert result.success is True
        assert seen == ["inside_on"]

    async def test_from_a_script(self, sim):
        sim.state.sensor_on_indoor = True
        seen: list[str] = []
        sim.add_notification_listener(seen.append)

        await ScriptRunner(sim).run(Script.from_simple_commands(["trigger inside"]), verbose=False)

        assert seen == ["inside_on"]

    async def test_the_prompt_refuses_an_unknown_sensor(self, handler):
        result = await handler.execute("trigger nowhere")
        assert result.success is False
        assert "inside" in result.message

    async def test_a_pass_through_is_not_presence(self, sim, handler):
        """The distinction the prompt could not previously express.

        `inside on` leaves a pet standing at the sensor; a pass-through
        does not, which is why both words have to exist.
        """
        await handler.execute("trigger inside")
        assert sim.state.pet_present("inside") is False

        await handler.execute("inside on")
        assert sim.state.pet_present("inside") is True


class TestScheduleActionsReachEverySurface:
    """`schedule enable`/`disable`/`clear` had no script spelling."""

    async def test_enable_and_disable_from_a_script(self, sim):
        sim.add_schedule(TestScheduleMaintenance._schedule(1), announce=False)

        await ScriptRunner(sim).run(
            Script.from_simple_commands(["enable_schedule 1 off"]), verbose=False
        )
        assert sim.get_schedule(1).enabled is False

        await ScriptRunner(sim).run(
            Script.from_simple_commands(["enable_schedule 1 on"]), verbose=False
        )
        assert sim.get_schedule(1).enabled is True

    async def test_clear_from_a_script(self, sim):
        sim.add_schedule(TestScheduleMaintenance._schedule(1), announce=False)
        sim.add_schedule(TestScheduleMaintenance._schedule(2), announce=False)

        await ScriptRunner(sim).run(Script.from_simple_commands(["clear_schedules"]), verbose=False)

        assert dict(sim.get_schedules()) == {}

    async def test_enabling_a_missing_schedule_fails_the_script(self, sim):
        ok = await ScriptRunner(sim).run(
            Script.from_simple_commands(["enable_schedule 7 on"]), verbose=False
        )
        assert ok is False

    async def test_the_script_and_the_prompt_agree(self, sim, handler):
        """Both go through `add_schedule`, so both announce the change."""
        sim.add_schedule(TestScheduleMaintenance._schedule(3), announce=False)
        sent: list[int] = []
        sim.broadcast_schedule = lambda s: sent.append(s.index)

        await handler.execute("schedule disable 3")
        await ScriptRunner(sim).run(
            Script.from_simple_commands(["enable_schedule 3 on"]), verbose=False
        )

        assert sent == [3, 3]
        assert sim.get_schedule(3).enabled is True


# ============================================================================
# The timezone boundary
# ============================================================================


class TestTheWireCarriesPosixOnly:
    """POSIX in both directions; IANA belongs above the wire.

    `SET_TIMEZONE` used to echo the raw stored value while `GET_TIMEZONE`
    and `GET_SETTINGS` answered POSIX, so a client could read back
    something it never sent. Both halves are fixed: the reply is POSIX,
    and an IANA name is refused rather than stored.
    """

    @pytest.mark.parametrize(
        "posix",
        ["EST5EDT,M3.2.0,M11.1.0", "UTC0", "<+11>-11", "AEST-10AEDT,M10.1.0,M4.1.0/3"],
    )
    async def test_a_posix_string_is_accepted_and_echoed(self, sim, protocol, posix):
        await dispatch(protocol, {CONFIG: CMD_SET_TIMEZONE, "msgId": 1, FIELD_TZ: posix})
        answer = json.loads(protocol.transport.write.call_args[0][0].decode("ascii"))
        assert answer[FIELD_SUCCESS] == SUCCESS_TRUE
        assert answer[FIELD_TZ] == posix
        assert sim.state.timezone == posix

    @pytest.mark.parametrize("iana", ["America/New_York", "Etc/UTC", "Australia/Sydney"])
    async def test_an_iana_name_is_refused(self, sim, protocol, iana):
        before = sim.state.timezone
        await dispatch(protocol, {CONFIG: CMD_SET_TIMEZONE, "msgId": 1, FIELD_TZ: iana})
        answer = json.loads(protocol.transport.write.call_args[0][0].decode("ascii"))
        assert answer[FIELD_SUCCESS] == SUCCESS_FALSE
        assert "POSIX" in answer[FIELD_REASON]
        assert sim.state.timezone == before, "refused before anything was written"

    @pytest.mark.parametrize("bad", ["nonsense", "", "America/Nowhere"])
    async def test_a_string_that_is_neither_is_refused(self, sim, protocol, bad):
        await dispatch(protocol, {CONFIG: CMD_SET_TIMEZONE, "msgId": 1, FIELD_TZ: bad})
        answer = json.loads(protocol.transport.write.call_args[0][0].decode("ascii"))
        assert answer[FIELD_SUCCESS] == SUCCESS_FALSE

    async def test_all_three_readers_agree(self, sim, protocol):
        """SET's echo, GET_TIMEZONE and GET_SETTINGS are one value."""
        await dispatch(
            protocol,
            {CONFIG: CMD_SET_TIMEZONE, "msgId": 1, FIELD_TZ: "EST5EDT,M3.2.0,M11.1.0"},
        )
        set_echo = json.loads(protocol.transport.write.call_args[0][0].decode("ascii"))[FIELD_TZ]

        await dispatch(protocol, {CONFIG: CMD_GET_TIMEZONE, "msgId": 2})
        get_tz = json.loads(protocol.transport.write.call_args[0][0].decode("ascii"))[FIELD_TZ]

        await dispatch(protocol, {CONFIG: CMD_GET_SETTINGS, "msgId": 3})
        settings = json.loads(protocol.transport.write.call_args[0][0].decode("ascii"))

        assert set_echo == get_tz == settings[FIELD_SETTINGS][FIELD_TZ]

    async def test_an_operator_surface_takes_an_iana_name_and_stores_posix(self, sim, handler):
        """The prompt is above the wire, so it converts rather than refuses.

        And it converts *on the way in*: what is stored is the rule, so a
        client connected at the time is told the same thing whichever
        spelling was typed here.
        """
        result = await handler.execute("timezone America/New_York")

        assert result.success is True
        assert sim.state.timezone == "EST5EDT,M3.2.0,M11.1.0"

    async def test_a_script_stores_posix_too(self, sim):
        await ScriptRunner(sim).run(
            Script.from_simple_commands(["set timezone Australia/Sydney"]), verbose=False
        )
        assert sim.state.timezone == "AEST-10AEDT,M10.1.0,M4.1.0/3"

    async def test_what_an_operator_typed_is_what_the_wire_answers(self, sim, handler, protocol):
        """The whole point: one stored form, so nothing has to convert on read."""
        await handler.execute("timezone America/New_York")

        await dispatch(protocol, {CONFIG: CMD_GET_TIMEZONE, "msgId": 1})
        answer = json.loads(protocol.transport.write.call_args[0][0].decode("ascii"))

        assert answer[FIELD_TZ] == "EST5EDT,M3.2.0,M11.1.0" == sim.state.timezone


class TestBoundsAreDeclaredOnce:
    """What a value accepts is the registry's to say, on every path.

    Reading and writing were routed through
    :data:`~powerpetdoor.simulator.values.VALUES` long before the rules
    governing acceptable values were, and the rules drifted exactly where
    you would expect: `totalOpenCycles` and `totalAutoRetracts`
    documented no maximum while the registry capped both at 2**31-1, and
    the schedule hour was written out four separate times - as 23 in two
    of them, which is narrower than the device.

    A bound restated is a bound that can disagree with the one actually
    enforced, so these pin that every wire bound is *derived*.
    """

    #: Numeric wire fields the registry does not hold, and why. A schedule
    #: is not a value in the registry sense - it is a record with several
    #: numbers in it - so its bounds live with the schedule code, in
    #: `powerpetdoor.schedule`, which is equally single-source.
    NOT_REGISTRY_VALUES = {
        FIELD_INDEX: "a schedule slot, bounded by MAX_SCHEDULE_INDEX",
        FIELD_HOUR: "a schedule time, bounded by MAX_SCHEDULE_HOUR",
        FIELD_MINUTE: "a schedule time, bounded by MAX_SCHEDULE_MINUTE",
    }

    def test_every_wire_bound_matches_the_registry(self):
        for field, (name, scale) in WIRE_BOUNDS.items():
            spec = VALUES[name]
            assert wire_bounds(field) == {
                "minimum": int(spec.minimum * scale),
                "maximum": int(spec.maximum * scale),
            }

    def test_every_bounded_wire_field_is_accounted_for(self):
        """A numeric field must derive its bounds or say why it cannot."""
        documented = set(WIRE_BOUNDS) | set(self.NOT_REGISTRY_VALUES)
        stray = sorted(
            field
            for field, doc in FIELD_DOCS.items()
            if ("minimum" in doc or "maximum" in doc) and field not in documented
        )
        assert stray == [], (
            f"{stray} declare bounds that come from nowhere. Add them to "
            "WIRE_BOUNDS, or to NOT_REGISTRY_VALUES with a reason."
        )

    def test_the_documented_bound_is_the_enforced_one(self):
        """The doc and the validator must be the same number.

        `SET_HOLD_TIME` is refused above `MAX_HOLD_TIME_CENTISECONDS`, so
        a spec advertising anything else is advertising a value the door
        will reject.
        """
        assert FIELD_DOCS[FIELD_HOLD_TIME]["maximum"] == MAX_HOLD_TIME_CENTISECONDS
        assert FIELD_DOCS[FIELD_VOLTAGE]["maximum"] == MAX_TRIGGER_VOLTAGE

    def test_the_hold_time_ceiling_is_one_limit_in_two_units(self):
        """900 s and 90000 cs are the same ceiling; only one is written."""
        assert MAX_HOLD_TIME_CENTISECONDS == int(VALUES["hold_time"].maximum * 100)

    def test_the_totals_are_bounded_at_all(self):
        """The regression: both documented a minimum and no maximum."""
        for field in (FIELD_TOTAL_OPEN_CYCLES, FIELD_TOTAL_AUTO_RETRACTS):
            assert FIELD_DOCS[field]["maximum"] == MAX_TRIGGER_VOLTAGE

    def test_a_settings_spelling_shares_its_top_level_bound(self):
        """`holdOpenTime` and `holdTime` are the same quantity.

        Nested and top-level spellings differ on this wire, but what the
        value accepts cannot differ with how it is spelled.
        """
        assert (
            OBJECT_FIELD_DOCS[FIELD_SETTINGS][FIELD_HOLD_OPEN_TIME]["maximum"]
            == FIELD_DOCS[FIELD_HOLD_TIME]["maximum"]
        )


class TestTheScheduleTimeRuleIsDeclaredOnce:
    """Four layers validated schedule times, and they disagreed.

    The wire coercion and the state-document loader allowed hour 24; the
    CLI's `time_range` parser and the field's own documentation stopped
    at 23. Only the state loader knew hour 24 requires a zero minute.
    Every one of them now asks
    :func:`~powerpetdoor.schedule.valid_schedule_time`.
    """

    #: (hour, minute, whether every layer must accept it)
    CASES = [
        (0, 0, True),
        (23, 59, True),
        (24, 0, True),  # the device's own end-of-day spelling
        (24, 1, False),  # one minute past the end of the day
        (24, 30, False),
        (25, 0, False),
        (0, 60, False),
    ]

    @pytest.mark.parametrize("hour,minute,ok", CASES)
    def test_the_rule_itself(self, hour, minute, ok):
        assert valid_schedule_time(hour, minute) is ok

    @pytest.mark.parametrize("hour,minute,ok", CASES)
    def test_the_wire_agrees(self, hour, minute, ok):
        value = {FIELD_HOUR: hour, FIELD_MINUTE: minute}
        if ok:
            assert coerce_schedule_time(value, "t") == (hour, minute)
        else:
            with pytest.raises(ValueError):
                coerce_schedule_time(value, "t")

    @pytest.mark.parametrize("hour,minute,ok", CASES)
    def test_the_cli_agrees(self, hour, minute, ok):
        if ok:
            assert _parse_time_str(f"{hour}:{minute:02d}") == (hour, minute)
        else:
            with pytest.raises(ValueError):
                _parse_time_str(f"{hour}:{minute:02d}")

    @pytest.mark.parametrize("hour,minute,ok", CASES)
    def test_the_state_document_agrees(self, hour, minute, ok):
        if ok:
            assert parse_time_of_day(f"{hour:02d}:{minute:02d}", "t") == (hour, minute)
        else:
            with pytest.raises(StateDocumentError):
                parse_time_of_day(f"{hour:02d}:{minute:02d}", "t")

    def test_the_published_spec_agrees(self):
        """The documented bound is the enforced one, not a nearby number."""
        hour = OBJECT_FIELD_DOCS[FIELD_SCHEDULE][FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX]
        assert hour["properties"][FIELD_HOUR]["maximum"] == MAX_SCHEDULE_HOUR
        assert hour["properties"][FIELD_MINUTE]["maximum"] == MAX_SCHEDULE_MINUTE


class TestTheClientCanReadEverythingTheDoorAnswers:
    """A wire command the simulator answers but the client cannot parse.

    `GET_TIMERS_ENABLED` was restored to `const.py`, to the simulator, to
    the wire table and to the docs - and missed in the client's response
    registry, which is the one surface that turns the reply into a value.
    The future then resolves with the raw envelope instead of a bool and
    the field's listeners never fire.

    Nothing could see it: a missing registry key adds no uncovered line,
    so the 100% gate is met, and every other test of that handler drives
    it through the *setter* commands, which were still registered.

    Derived from the wire table rather than listed, so the next command
    to arrive is covered without anyone remembering this file.
    """

    @staticmethod
    def _every_wire_command() -> set[str]:
        """Every command in the wire table, by all three roles.

        The first version of this walked `getter` and the SWITCH
        enable/disable pairs, which left the four `CMD_SET_*` value
        commands - hold time, timezone and the two voltages - outside the
        perimeter entirely. Dropping `SET_SENSOR_TRIGGER_VOLTAGE` from
        its handler left the whole suite green at 100% coverage, which is
        the very hole this class exists to close, one category over.
        """
        return {
            command
            for wire in WIRE_VALUES.values()
            for command in (wire.enable, wire.disable, wire.getter)
            if command is not None
        }

    def test_every_wire_command_has_a_client_response_handler(self):
        from powerpetdoor.client import ResponseHandlerRegistry

        missing = sorted(
            command
            for command in self._every_wire_command()
            if command not in ResponseHandlerRegistry._handlers
        )
        assert missing == [], f"the door answers {missing} but the client cannot parse the reply"

    def test_the_perimeter_covers_the_setters_too(self):
        """Named, because their absence is what made the hole invisible."""
        covered = self._every_wire_command()
        for command in (
            "SET_HOLD_TIME",
            "SET_TIMEZONE",
            "SET_SENSOR_TRIGGER_VOLTAGE",
            "SET_SLEEP_SENSOR_TRIGGER_VOLTAGE",
        ):
            assert command in covered, f"{command} is outside the perimeter"

    def test_the_perimeter_is_not_empty(self):
        """A derivation that matched nothing would pass silently."""
        assert len(self._every_wire_command()) >= 20
