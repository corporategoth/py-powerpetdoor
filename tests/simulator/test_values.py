# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The value registry, and the `get`/`set` commands that read it.

The registry is what makes the simulator's four surfaces agree, so it is
also what has to be pinned: a value that drops out of the table silently
loses its `get`, its `set`, and the script condition that asserts on it,
all at once.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from powerpetdoor.door import PowerPetDoor
from powerpetdoor.simulator import DoorSimulator, DoorSimulatorState
from powerpetdoor.simulator.coerce import CoercionError
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.commands.values import (
    SWITCH_COMMANDS,
    coerce_value,
    render_value,
)
from powerpetdoor.simulator.notifications import NOTIFICATION_NAMES
from powerpetdoor.simulator.scripting import ScriptRunner
from powerpetdoor.simulator.values import (
    VALUE_NAMES,
    VALUES,
    WRITABLE,
    set_named_value,
    toggle_named_value,
)
from powerpetdoor.simulator.wire_values import WIRE_VALUES


@pytest.fixture
def sim():
    return DoorSimulator(port=0, state=DoorSimulatorState())


@pytest.fixture
def handler(sim):
    return CommandHandler(simulator=sim, script_runner=ScriptRunner(sim), stop_callback=MagicMock())


# ============================================================================
# The table itself
# ============================================================================


class TestRegistryShape:
    def test_every_value_reads(self, sim):
        """A `get` of the whole table must not raise on any row."""
        for name in VALUE_NAMES:
            VALUES[name].get(sim.state)

    def test_writable_is_exactly_the_rows_with_a_setter(self):
        assert WRITABLE == tuple(sorted(n for n in VALUES if VALUES[n].set is not None))

    def test_names_are_sorted_and_complete(self):
        assert VALUE_NAMES == tuple(sorted(VALUES))

    def test_read_only_rows_are_read_only(self):
        """The four the door computes rather than stores."""
        assert not VALUES["door_status"].writable
        assert not VALUES["position"].writable
        assert not VALUES["time"].writable

    def test_apply_refuses_a_read_only_row(self, sim):
        with pytest.raises(ValueError, match="read-only"):
            VALUES["door_status"].apply(sim, "DOOR_IDLE")

    def test_every_notification_has_a_row(self):
        for name in NOTIFICATION_NAMES:
            assert f"notify_{name}" in VALUES

    def test_facade_readable_properties_are_all_in_the_table(self):
        """Parity with :class:`~powerpetdoor.door.PowerPetDoor`.

        Anything a consumer can read off the facade should be assertable
        in a script, which is the whole reason the table exists.
        """
        expected = {
            "power",
            "auto",
            "inside",
            "outside",
            "autoretract",
            "hold_time",
            "timezone",
            "battery",
            "battery_present",
            "ac_present",
            "door_status",
            "position",
            "sensor_trigger_voltage",
            "sleep_sensor_trigger_voltage",
        }
        missing = {name for name in expected if name not in VALUES}
        assert missing == set()
        assert all(hasattr(PowerPetDoor, name) for name in ("is_open", "position", "battery"))


# ============================================================================
# Setters with behaviour of their own
# ============================================================================


class TestSetters:
    def test_announce_is_skipped_when_asked(self, sim):
        sent = []
        sim.broadcast_value = sent.append
        VALUES["autoretract"].apply(sim, True, announce=False)
        assert sim.state.autoretract is True
        assert sent == []

    def test_timing_setter_writes_the_timing_block(self, sim):
        VALUES["rise_time"].apply(sim, 3.5)
        assert sim.state.timing.rise_time == 3.5

    def test_firmware_version_splits_into_three(self, sim):
        VALUES["firmware_version"].apply(sim, "1.7.18")
        assert (sim.state.fw_major, sim.state.fw_minor, sim.state.fw_patch) == (1, 7, 18)
        assert VALUES["firmware_version"].get(sim.state) == "1.7.18"

    def test_firmware_version_pads_a_short_one(self, sim):
        VALUES["firmware_version"].apply(sim, "2")
        assert (sim.state.fw_major, sim.state.fw_minor, sim.state.fw_patch) == (2, 0, 0)

    def test_hardware_version_splits_into_two(self, sim):
        VALUES["hardware_version"].apply(sim, "3.4")
        assert (sim.state.hw_ver, sim.state.hw_rev) == (3, 4)
        assert VALUES["hardware_version"].get(sim.state) == "3.4"

    def test_hardware_version_pads_a_short_one(self, sim):
        VALUES["hardware_version"].apply(sim, "5")
        assert (sim.state.hw_ver, sim.state.hw_rev) == (5, 0)

    def test_a_value_reported_inside_settings_announces_that(self, sim):
        """A trigger voltage is not a message of its own on a real door.

        It arrives inside `settings`, so the row says so explicitly rather
        than getting the derived "push it under its own command".
        """
        sent = []
        sim.broadcast_settings = lambda: sent.append("settings")
        sim.broadcast_value = lambda name: sent.append(f"value:{name}")

        VALUES["sensor_trigger_voltage"].apply(sim, 1500)

        assert sent == ["settings"]
        assert sim.state.sensor_trigger_voltage == 1500

    def test_a_counter_announces_inside_the_stats(self, sim):
        sent = []
        sim.broadcast_stats = lambda: sent.append("stats")
        VALUES["total_open_cycles"].apply(sim, 9)
        assert sent == ["stats"]

    def test_a_value_with_no_wire_spelling_announces_nothing(self, sim):
        """`has_remote_id` is readable over the wire but never pushed."""
        assert "has_remote_id" not in WIRE_VALUES
        sent = []
        sim.send_to_clients = lambda cmd, payload: sent.append(cmd)
        VALUES["has_remote_id"].apply(sim, True)
        assert sent == []

    def test_obstruction_sets_and_clears(self, sim):
        VALUES["obstruction"].apply(sim, True)
        assert sim.state.obstruction_active is True
        VALUES["obstruction"].apply(sim, False)
        assert sim.state.obstruction_active is False


# ============================================================================
# Coercion
# ============================================================================


class TestCoerceValue:
    def test_bool_row(self):
        assert coerce_value("power", VALUES["power"], "on") is True
        assert coerce_value("power", VALUES["power"], "off") is False

    def test_int_row_truncates_to_int(self):
        assert coerce_value("battery", VALUES["battery"], "42") == 42
        assert isinstance(coerce_value("battery", VALUES["battery"], "42"), int)

    def test_number_row_keeps_the_fraction(self):
        assert coerce_value("hold_time", VALUES["hold_time"], "2.5") == 2.5

    def test_text_row_passes_through(self):
        assert coerce_value("timezone", VALUES["timezone"], "UTC") == "UTC"

    def test_refusal_names_the_value(self):
        """ "must be a finite number" is unreadable in a CI log without it."""
        with pytest.raises(CoercionError, match="hold_time"):
            coerce_value("hold_time", VALUES["hold_time"], "soon")

    @pytest.mark.parametrize("raw", ["-1", "901"])
    def test_number_row_enforces_its_bounds(self, raw):
        with pytest.raises(CoercionError):
            coerce_value("hold_time", VALUES["hold_time"], raw)

    def test_number_row_accepts_both_ends(self):
        """The bounds are inclusive; a hold of exactly 900 s is legal."""
        assert coerce_value("hold_time", VALUES["hold_time"], "0") == 0.0
        assert coerce_value("hold_time", VALUES["hold_time"], "900") == 900.0


# ============================================================================
# The `get` and `set` commands
# ============================================================================


class TestGetCommand:
    async def test_one_value(self, handler, sim):
        sim.state.hold_time = 7.0
        result = await handler.execute("get hold_time")
        assert result.success is True
        assert result.message == "hold_time: 7.0"
        assert result.data == {"hold_time": 7.0}

    async def test_dashes_are_accepted_for_underscores(self, handler):
        assert (await handler.execute("get hold-time")).success is True

    async def test_all_values_split_door_from_simulation(self, handler):
        result = await handler.execute("get")
        assert result.success is True
        assert result.message.startswith("Door:")
        assert "\nSimulation:" in result.message
        assert set(result.data) == set(VALUE_NAMES)

    async def test_simulation_values_land_under_simulation(self, handler):
        result = await handler.execute("get")
        door, simulated = result.message.split("\nSimulation:")
        assert "rise_time" in simulated
        assert "rise_time" not in door
        assert "hold_time" in door

    async def test_unknown_name_is_refused_and_lists_the_options(self, handler):
        result = await handler.execute("get nonsense")
        assert result.success is False
        assert "nonsense" in result.message
        assert "hold_time" in result.message

    def test_render_value(self, sim):
        sim.state.battery_percent = 55
        assert render_value("battery", sim.state) == "battery: 55"


class TestSetCommand:
    async def test_writes_and_echoes(self, handler, sim):
        result = await handler.execute("set hold_time 12")
        assert result.success is True
        assert result.message == "hold_time: 12.0"
        assert sim.state.hold_time == 12.0

    async def test_simulation_value_is_writable_here(self, handler, sim):
        """Unlike the wire, the prompt reaches the simulation's own knobs."""
        assert (await handler.execute("set rise_time 0.05")).success is True
        assert sim.state.timing.rise_time == 0.05

    async def test_the_change_is_announced(self, handler, sim):
        """`set hold_time 12` broadcasts what `holdtime 12` broadcasts.

        Applying without announcing left connected clients holding a
        value the simulator had already moved on from.
        """
        sent = []
        sim.broadcast_value = sent.append
        await handler.execute("set hold_time 12")
        assert sent == ["hold_time"]

    async def test_toggle_from_a_script_announces_too(self, sim):
        sent = []
        sim.broadcast_value = sent.append
        sim.state.autoretract = False
        toggle_named_value(sim, "autoretract")
        assert sent == ["autoretract"]
        assert sim.state.autoretract is True

    async def test_set_can_be_asked_not_to_announce(self, sim):
        """The wire answers in its own response, so it passes announce=False."""
        sent = []
        sim.broadcast_value = sent.append
        set_named_value(sim, "hold_time", "12", announce=False)
        assert sim.state.hold_time == 12.0
        assert sent == []

    async def test_read_only_value_is_refused(self, handler):
        result = await handler.execute("set door_status DOOR_IDLE")
        assert result.success is False
        assert "read-only" in result.message

    async def test_unknown_name_is_refused(self, handler):
        result = await handler.execute("set nonsense 1")
        assert result.success is False
        assert "nonsense" in result.message

    async def test_bad_value_is_refused_with_the_name(self, handler, sim):
        before = sim.state.hold_time
        result = await handler.execute("set hold_time soon")
        assert result.success is False
        assert "hold_time" in result.message
        assert sim.state.hold_time == before


# ============================================================================
# The generated switch commands
# ============================================================================


class TestGeneratedSwitchCommands:
    """Each named switch is one row of :data:`SWITCH_COMMANDS`.

    They were nine commands plus nine `toggle` twins, every one of them a
    decorator and a two-line body ending in the same call. Generating them
    is why `power off` and `set power off` cannot drift apart: there is no
    second body to drift.
    """

    @pytest.fixture(params=SWITCH_COMMANDS, ids=lambda s: s.name)
    def switch(self, request):
        return request.param

    def test_every_row_names_a_writable_boolean(self, switch):
        spec = VALUES[switch.value]
        assert spec.kind == "bool"
        assert spec.writable is True

    async def test_setting_on_and_off(self, handler, sim, switch):
        assert (await handler.execute(f"{switch.name} on")).success is True
        assert VALUES[switch.value].get(sim.state) is True
        assert (await handler.execute(f"{switch.name} off")).success is True
        assert VALUES[switch.value].get(sim.state) is False

    async def test_bare_word_toggles(self, handler, sim, switch):
        before = VALUES[switch.value].get(sim.state)
        await handler.execute(switch.name)
        assert VALUES[switch.value].get(sim.state) is (not before)

    async def test_toggle_subcommand_toggles(self, handler, sim, switch):
        before = VALUES[switch.value].get(sim.state)
        await handler.execute(f"{switch.name} toggle")
        assert VALUES[switch.value].get(sim.state) is (not before)

    async def test_reply_uses_the_rows_own_words(self, handler, sim, switch):
        on = await handler.execute(f"{switch.name} on")
        off = await handler.execute(f"{switch.name} off")
        assert on.message == switch.message(True)
        assert off.message == switch.message(False)
        assert on.message != off.message

    async def test_aliases_reach_the_same_value(self, handler, sim, switch):
        for alias in switch.aliases:
            await handler.execute(f"{alias} on")
            assert VALUES[switch.value].get(sim.state) is True
            await handler.execute(f"{alias} off")
            assert VALUES[switch.value].get(sim.state) is False

    async def test_the_change_is_announced(self, handler, sim, switch):
        sent = []
        sim.broadcast_value = sent.append
        await handler.execute(f"{switch.name} on")
        assert sent == ([switch.value] if switch.value in WIRE_VALUES else [])

    async def test_exact_wording_is_preserved(self, handler):
        """The three rows that do not read "<Label>: ON"."""
        assert (await handler.execute("ac connect")).message == "AC set to connected"
        assert (await handler.execute("ac disconnect")).message == "AC set to disconnected"
        assert (await handler.execute("battery_present on")).message == "Battery: installed"
        assert (await handler.execute("battery_present off")).message == "Battery: removed"
        assert (await handler.execute("inside_enable on")).message == "Inside sensor: enabled"
        assert (await handler.execute("power on")).message == "Power: ON"

    async def test_connect_and_disconnect_are_not_toggles(self, handler, sim):
        """`ac connect` twice leaves it connected; only `ac` flips."""
        await handler.execute("ac connect")
        await handler.execute("ac connect")
        assert sim.state.ac_present is True
        await handler.execute("ac disconnect")
        await handler.execute("ac disconnect")
        assert sim.state.ac_present is False

    def test_each_word_maps_to_the_value_it_names(self):
        """Pinned by literal, because the rest of this class reads the row.

        A test that asserts through `switch.value` follows a mistyped row
        wherever it points - `power` wired to `auto` passes everything
        else here. This is the one place the mapping is stated
        independently.
        """
        assert {s.name: s.value for s in SWITCH_COMMANDS} == {
            "power": "power",
            "auto": "auto",
            "inside_enable": "inside",
            "outside_enable": "outside",
            "safety": "safety_lock",
            "lockout": "cmd_lockout",
            "autoretract": "autoretract",
            "battery_present": "battery_present",
            "ac": "ac_present",
        }

    def test_no_two_rows_claim_the_same_word(self):
        words = [w for s in SWITCH_COMMANDS for w in (s.name, *s.aliases)]
        assert len(words) == len(set(words))
