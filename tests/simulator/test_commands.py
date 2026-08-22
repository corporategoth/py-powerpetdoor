# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator commands module (commands.py)."""

from __future__ import annotations

import asyncio
import io
import sys
from unittest.mock import MagicMock

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_HOLDING,
    DOOR_STATE_RISING,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.commands.scripts import ScriptQueue, is_wait_run
from powerpetdoor.simulator.scripting import Script, ScriptRunner, ScriptStep


class _TtyStdout(io.StringIO):
    """A stdout replacement that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def timing_config():
    """Create a fast timing config for tests."""
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(timing_config):
    """Create and start a simulator."""
    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
def command_handler(simulator):
    """Create a command handler for the simulator."""
    script_runner = ScriptRunner(simulator)
    stop_callback = MagicMock()
    handler = CommandHandler(
        simulator=simulator,
        script_runner=script_runner,
        stop_callback=stop_callback,
    )
    return handler


# ============================================================================
# Notification Command Tests
# ============================================================================


class TestNotifyCommand:
    """Tests for the notify command."""

    async def test_notify_shows_all_settings(self, command_handler):
        """notify with no args should show all notification settings."""
        result = await command_handler.execute("notify")
        assert result.success is True
        assert "Notifications:" in result.message
        assert "inside_on:" in result.message
        assert "inside_off:" in result.message
        assert "outside_on:" in result.message
        assert "outside_off:" in result.message
        assert "low_battery:" in result.message

    async def test_notify_toggle_inside_on(self, command_handler):
        """notify inside_on should toggle the setting."""
        state = command_handler.simulator.state
        initial = state.sensor_on_indoor

        result = await command_handler.execute("notify inside_on")
        assert result.success is True
        assert state.sensor_on_indoor != initial

        # Toggle back
        result = await command_handler.execute("notify inside_on")
        assert result.success is True
        assert state.sensor_on_indoor == initial

    async def test_notify_set_inside_on_on(self, command_handler):
        """notify inside_on on should enable the notification."""
        state = command_handler.simulator.state
        state.sensor_on_indoor = False

        result = await command_handler.execute("notify inside_on on")
        assert result.success is True
        assert "ON" in result.message
        assert state.sensor_on_indoor is True

    async def test_notify_set_inside_on_off(self, command_handler):
        """notify inside_on off should disable the notification."""
        state = command_handler.simulator.state
        state.sensor_on_indoor = True

        result = await command_handler.execute("notify inside_on off")
        assert result.success is True
        assert "OFF" in result.message
        assert state.sensor_on_indoor is False

    async def test_notify_low_battery(self, command_handler):
        """notify low_battery should toggle low battery notifications."""
        state = command_handler.simulator.state
        initial = state.low_battery

        result = await command_handler.execute("notify low_battery")
        assert result.success is True
        assert state.low_battery != initial

    async def test_notify_outside_on(self, command_handler):
        """notify outside_on should toggle outside sensor on notification."""
        state = command_handler.simulator.state
        initial = state.sensor_on_outdoor

        result = await command_handler.execute("notify outside_on")
        assert result.success is True
        assert state.sensor_on_outdoor != initial

    async def test_notify_unknown_notification(self, command_handler):
        """notify with unknown name should fail."""
        result = await command_handler.execute("notify unknown_notify")
        assert result.success is False
        assert "Unknown notify subcommand" in result.message

    async def test_notify_invalid_value(self, command_handler):
        """notify with invalid value should fail."""
        result = await command_handler.execute("notify inside_on maybe")
        assert result.success is False
        assert "not valid" in result.message


# ============================================================================
# Cycle Command Tests
# ============================================================================


class TestCycleCommand:
    """Tests for the cycle command."""

    async def test_cycle_starts_door_operation(self, command_handler):
        """cycle command should start a door cycle."""
        sim = command_handler.simulator
        assert sim.state.door_status == DOOR_STATE_CLOSED

        result = await command_handler.execute("cycle")
        assert result.success is True
        assert "Starting door cycle" in result.message

        # Wait deterministically for the door to start rising
        status = await sim.wait_for_status(DOOR_STATE_RISING, timeout=5)
        assert status == DOOR_STATE_RISING

    async def test_cycle_alias_y(self, command_handler):
        """'y' alias should work for cycle command."""
        result = await command_handler.execute("y")
        assert result.success is True
        assert "Starting door cycle" in result.message

    async def test_cycle_full_sequence(self, command_handler):
        """cycle should complete a full open-hold-close sequence."""
        sim = command_handler.simulator
        assert sim.state.door_status == DOOR_STATE_CLOSED

        states_seen = []
        unsubscribe = sim.add_status_listener(states_seen.append)

        result = await command_handler.execute("cycle")
        assert result.success is True

        await sim.wait_for_status(DOOR_STATE_HOLDING, timeout=5)
        await sim.wait_for_status(DOOR_STATE_CLOSED, timeout=5)
        unsubscribe()

        assert DOOR_STATE_RISING in states_seen
        assert DOOR_STATE_HOLDING in states_seen
        assert states_seen[-1] == DOOR_STATE_CLOSED


# ============================================================================
# Battery Command Tests
# ============================================================================


class TestBatteryCommands:
    """Tests for battery-related commands."""

    async def test_ac_command_toggle(self, command_handler):
        """ac command should toggle AC connection."""
        state = command_handler.simulator.state
        initial = state.ac_present

        result = await command_handler.execute("ac")
        assert result.success is True
        assert state.ac_present != initial

    async def test_ac_command_connect(self, command_handler):
        """ac connect should enable AC."""
        state = command_handler.simulator.state
        state.ac_present = False

        result = await command_handler.execute("ac connect")
        assert result.success is True
        assert "connected" in result.message
        assert state.ac_present is True

    async def test_ac_command_disconnect(self, command_handler):
        """ac disconnect should disable AC."""
        state = command_handler.simulator.state
        state.ac_present = True

        result = await command_handler.execute("ac disconnect")
        assert result.success is True
        assert "disconnected" in result.message
        assert state.ac_present is False

    async def test_battery_present_toggle(self, command_handler):
        """battery_present command should toggle battery presence."""
        state = command_handler.simulator.state
        initial = state.battery_present

        result = await command_handler.execute("battery_present")
        assert result.success is True
        assert state.battery_present != initial

    async def test_battery_present_on(self, command_handler):
        """battery_present on should install battery."""
        state = command_handler.simulator.state
        state.battery_present = False

        result = await command_handler.execute("battery_present on")
        assert result.success is True
        assert "installed" in result.message
        assert state.battery_present is True

    async def test_charge_rate_set(self, command_handler):
        """charge_rate should set the charge rate."""
        result = await command_handler.execute("charge_rate 5.0")
        assert result.success is True
        assert "5.0" in result.message
        assert command_handler.simulator.state.battery_config.charge_rate == 5.0

    async def test_charge_rate_zero_disables(self, command_handler):
        """charge_rate 0 should disable charging."""
        result = await command_handler.execute("charge_rate 0")
        assert result.success is True
        assert "disabled" in result.message
        assert command_handler.simulator.state.battery_config.charge_rate == 0.0

    async def test_charge_rate_show_current(self, command_handler):
        """charge_rate with no arg should show current rate."""
        result = await command_handler.execute("charge_rate")
        assert result.success is True
        assert "Charge rate:" in result.message

    async def test_discharge_rate_set(self, command_handler):
        """discharge_rate should set the discharge rate."""
        result = await command_handler.execute("discharge_rate 0.5")
        assert result.success is True
        assert "0.5" in result.message
        assert command_handler.simulator.state.battery_config.discharge_rate == 0.5

    async def test_status_shows_battery_info(self, command_handler):
        """status command should show battery and notification info."""
        result = await command_handler.execute("status")
        assert result.success is True
        assert "Battery:" in result.message
        assert "AC:" in result.message
        assert "Notifications:" in result.message


# ============================================================================
# Close Command Alias Tests
# ============================================================================


class TestAliases:
    """Tests for command aliases."""

    async def test_close_alias_c(self, command_handler):
        """'c' alias should work for close command."""
        from powerpetdoor.const import DOOR_STATE_KEEPUP

        # First open the door and wait for it to be held open
        sim = command_handler.simulator
        await sim.open_door(hold=True)
        await sim.wait_for_status(DOOR_STATE_KEEPUP, timeout=5)

        result = await command_handler.execute("c")
        assert result.success is True
        assert "Closing" in result.message
        status = await sim.wait_for_status(DOOR_STATE_CLOSED, timeout=5)
        assert status == DOOR_STATE_CLOSED

    async def test_run_alias_r(self, command_handler):
        """'r' resolves to run, which rejects an unknown script by name."""
        result = await command_handler.execute("r nonexistent")

        assert result.success is False
        assert result.message.startswith("Unknown script: nonexistent")


# ============================================================================
# Broadcast Command Tests
# ============================================================================


class TestBroadcastCommand:
    """Tests for the broadcast command."""

    async def test_broadcast_no_arg_shows_types(self, command_handler):
        """broadcast with no arg should show available subcommands."""
        result = await command_handler.execute("broadcast")
        assert result.success
        assert "broadcast subcommands:" in result.message
        assert "status" in result.message
        assert "settings" in result.message
        assert "battery" in result.message
        assert "all" in result.message

    async def test_broadcast_alias_bc(self, command_handler):
        """'bc' alias should work for broadcast command."""
        result = await command_handler.execute("bc")
        assert result.success
        assert "broadcast subcommands:" in result.message

    async def test_broadcast_no_clients_error(self, command_handler):
        """broadcast should fail when no clients connected."""
        result = await command_handler.execute("broadcast status")
        assert not result.success
        assert "No clients connected" in result.message

    async def test_broadcast_invalid_type(self, command_handler):
        """broadcast with invalid type should fail."""
        result = await command_handler.execute("broadcast invalid")
        assert not result.success
        assert "Unknown broadcast subcommand" in result.message

    async def test_broadcast_status_with_client(self, command_handler):
        """broadcast status should work when client connected."""
        # Add a mock protocol to simulate a connected client
        mock_protocol = MagicMock()
        mock_protocol._door_task = None  # Prevent cleanup issues
        command_handler.simulator.protocols.append(mock_protocol)

        try:
            result = await command_handler.execute("broadcast status")
            assert result.success
            assert "Broadcast status:" in result.message
        finally:
            command_handler.simulator.protocols.clear()

    async def test_broadcast_all_with_client(self, command_handler):
        """broadcast all should work when client connected."""
        mock_protocol = MagicMock()
        mock_protocol._door_task = None  # Prevent cleanup issues
        command_handler.simulator.protocols.append(mock_protocol)

        try:
            result = await command_handler.execute("broadcast all")
            assert result.success
            assert "Broadcast all data" in result.message
        finally:
            command_handler.simulator.protocols.clear()


# ============================================================================
# Status Command Client Count Tests
# ============================================================================


class TestStatusClientCount:
    """Tests for client count in status command."""

    async def test_status_shows_no_clients(self, command_handler):
        """status should show 'Clients: none' when no clients connected."""
        result = await command_handler.execute("status")
        assert result.success
        assert "Clients: none" in result.message

    async def test_status_shows_one_client(self, command_handler):
        """status should show '1 client' when one client connected."""
        mock_protocol = MagicMock()
        mock_protocol._door_task = None
        command_handler.simulator.protocols.append(mock_protocol)

        try:
            result = await command_handler.execute("status")
            assert result.success
            assert "Clients: 1 client" in result.message
        finally:
            command_handler.simulator.protocols.clear()

    async def test_status_shows_multiple_clients(self, command_handler):
        """status should show 'N clients' when multiple clients connected."""
        for _ in range(3):
            mock = MagicMock()
            mock._door_task = None
            command_handler.simulator.protocols.append(mock)

        try:
            result = await command_handler.execute("status")
            assert result.success
            assert "Clients: 3 clients" in result.message
        finally:
            command_handler.simulator.protocols.clear()

    async def test_status_data_includes_client_count(self, command_handler):
        """status result.data should include connected_clients."""
        for _ in range(2):
            mock = MagicMock()
            mock._door_task = None
            command_handler.simulator.protocols.append(mock)

        try:
            result = await command_handler.execute("status")
            assert result.data is not None
            assert result.data["connected_clients"] == 2
        finally:
            command_handler.simulator.protocols.clear()


# ============================================================================
# Interactive-Only Command Tests
# ============================================================================


class TestInteractiveOnlyCommands:
    """Tests for interactive-only commands."""

    async def test_clear_rejected_when_not_interactive(self, command_handler):
        """clear command should be rejected when not in interactive mode."""
        # By default, _interactive_mode is False
        assert command_handler._interactive_mode is False

        result = await command_handler.execute("clear")
        assert result.success is False
        assert "Unknown command" in result.message

    async def test_clear_writes_the_ansi_sequence_on_a_terminal(self, command_handler, monkeypatch):
        """On a real terminal `clear` must actually clear it.

        The assertion is against the buffer this test installs. `capsys`
        patches sys.stdout, not sys.__stdout__, so a capsys-based assertion
        here is vacuous - it holds whether or not the sequence was written
        (R3-M1).
        """
        command_handler.set_interactive_mode(True)
        tty = _TtyStdout()
        monkeypatch.setattr(sys, "__stdout__", tty)

        result = await command_handler.execute("clear")

        assert result.success is True
        assert result.message == ""  # cli.py skips printing an empty message
        assert tty.getvalue() == "\033[2J\033[H"

    async def test_clear_writes_nothing_off_a_terminal(self, command_handler, monkeypatch):
        """Piped/dumb sessions must not receive ANSI garbage (T3)."""
        command_handler.set_interactive_mode(True)
        pipe = io.StringIO()  # isatty() -> False
        monkeypatch.setattr(sys, "__stdout__", pipe)

        result = await command_handler.execute("clear")

        assert result.success is True
        assert result.message == ""
        assert pipe.getvalue() == ""

    async def test_clear_falls_back_to_sys_stdout(self, command_handler, monkeypatch):
        """Under pythonw/embedding sys.__stdout__ can be None."""
        command_handler.set_interactive_mode(True)
        tty = _TtyStdout()
        monkeypatch.setattr(sys, "__stdout__", None)
        monkeypatch.setattr(sys, "stdout", tty)

        result = await command_handler.execute("clear")

        assert result.success is True
        assert tty.getvalue() == "\033[2J\033[H"

    async def test_clear_alias_cls_rejected_when_not_interactive(self, command_handler):
        """cls alias should also be rejected when not in interactive mode."""
        result = await command_handler.execute("cls")
        assert result.success is False
        assert "Unknown command" in result.message

    async def test_history_rejected_when_not_interactive(self, command_handler):
        """history command should be rejected when not in interactive mode."""
        # Even if history is available, it should be rejected in non-interactive mode
        result = await command_handler.execute("history")
        assert result.success is False
        assert "Unknown command" in result.message

    async def test_history_alias_rejected_when_not_interactive(self, command_handler):
        """hist alias should be rejected when not in interactive mode."""
        result = await command_handler.execute("hist")
        assert result.success is False
        assert "Unknown command" in result.message

    async def test_help_works_when_not_interactive(self, command_handler):
        """help command should work even when not in interactive mode."""
        result = await command_handler.execute("help")
        assert result.success is True
        assert "Commands:" in result.message

    async def test_help_hides_interactive_commands_when_not_interactive(self, command_handler):
        """help should not show interactive-only commands when not in interactive mode.

        Checks the rendered command entries ("clear (cls)" / "history (hist)")
        because "clear" also appears in the schedule command's subcommand usage.
        """
        result = await command_handler.execute("help")
        assert result.success is True
        assert "clear (cls)" not in result.message
        assert "history (hist)" not in result.message

    async def test_help_shows_interactive_commands_in_interactive_mode(self, command_handler):
        """help should show interactive-only commands in interactive mode."""
        command_handler.set_interactive_mode(True)

        result = await command_handler.execute("help")
        assert result.success is True
        assert "clear (cls)" in result.message

    async def test_interactive_mode_round_trip_changes_behavior(self, command_handler):
        """Enabling then disabling interactive mode restores command hiding."""
        command_handler.set_interactive_mode(True)
        result = await command_handler.execute("clear")
        assert result.success is True

        command_handler.set_interactive_mode(False)
        result = await command_handler.execute("clear")
        assert result.success is False
        assert "Unknown command" in result.message


# ============================================================================
# Pet Presence Command Tests
# ============================================================================


class TestPetCommand:
    """Tests for the pet presence command (drives set_pet_in_doorway)."""

    async def test_pet_bare_toggles_on(self, command_handler):
        """pet with no args toggles pet presence on when off."""
        state = command_handler.simulator.state
        state.inside_sensor_active = False

        result = await command_handler.execute("pet")
        assert result.success is True
        assert "Pet in doorway" in result.message
        assert state.inside_sensor_active is True

    async def test_pet_bare_toggles_off(self, command_handler):
        """pet with no args toggles pet presence off when on."""
        state = command_handler.simulator.state
        state.inside_sensor_active = True

        result = await command_handler.execute("pet")
        assert result.success is True
        assert "Pet left doorway" in result.message
        assert state.inside_sensor_active is False

    async def test_pet_on(self, command_handler):
        state = command_handler.simulator.state
        state.inside_sensor_active = False

        result = await command_handler.execute("pet on")
        assert result.success is True
        assert state.inside_sensor_active is True

    async def test_pet_on_clears_outside_sensor(self, command_handler):
        """Pet presence uses the inside sensor; sensors are mutually exclusive."""
        state = command_handler.simulator.state
        state.outside_sensor_active = True

        result = await command_handler.execute("pet on")
        assert result.success is True
        assert state.inside_sensor_active is True
        assert state.outside_sensor_active is False

    async def test_pet_off(self, command_handler):
        state = command_handler.simulator.state
        state.inside_sensor_active = True

        result = await command_handler.execute("pet off")
        assert result.success is True
        assert state.inside_sensor_active is False

    async def test_pet_alias_d(self, command_handler):
        """'d' alias (the key documented in docs/simulator.md) works."""
        state = command_handler.simulator.state
        state.inside_sensor_active = False

        result = await command_handler.execute("d")
        assert result.success is True
        assert state.inside_sensor_active is True

    async def test_pet_toggle_subcommand(self, command_handler):
        state = command_handler.simulator.state
        state.inside_sensor_active = False

        result = await command_handler.execute("pet toggle")
        assert result.success is True
        assert state.inside_sensor_active is True

    async def test_pet_invalid_value(self, command_handler):
        result = await command_handler.execute("pet maybe")
        assert result.success is False
        assert "not valid" in result.message

    async def test_pet_in_help(self, command_handler):
        result = await command_handler.execute("help")
        assert result.success is True
        assert "pet (d)" in result.message


# ============================================================================
# No-Argument Show Semantics Tests (battery / holdtime)
# ============================================================================


class TestNoArgShowSemantics:
    """Bare value commands must SHOW the value, never mutate it."""

    async def test_battery_bare_shows_without_mutating(self, command_handler):
        state = command_handler.simulator.state
        state.battery_percent = 73

        result = await command_handler.execute("battery")
        assert result.success is True
        assert result.message == "Battery: 73%"
        assert state.battery_percent == 73

    async def test_battery_set_value(self, command_handler):
        result = await command_handler.execute("battery 55")
        assert result.success is True
        assert "Battery set to 55%" in result.message
        assert command_handler.simulator.state.battery_percent == 55

    async def test_battery_random_subcommand(self, command_handler):
        """Randomization moved to an explicit subcommand."""
        result = await command_handler.execute("battery random")
        assert result.success is True
        assert "Battery set to" in result.message
        assert 10 <= command_handler.simulator.state.battery_percent <= 100

    async def test_holdtime_bare_shows_current_value(self, command_handler):
        command_handler.simulator.state.hold_time = 7.5

        result = await command_handler.execute("holdtime")
        assert result.success is True
        assert result.message == "Hold time: 7.5s"
        assert command_handler.simulator.state.hold_time == 7.5

    async def test_holdtime_set_value(self, command_handler):
        result = await command_handler.execute("holdtime 5")
        assert result.success is True
        assert "Hold time set to 5.0s" in result.message
        assert command_handler.simulator.state.hold_time == 5.0

    async def test_holdtime_still_validates_range(self, command_handler):
        result = await command_handler.execute("holdtime 10000")
        assert result.success is False
        assert "above maximum" in result.message


# ============================================================================
# Extra Argument Rejection Tests
# ============================================================================


class TestExtraArgumentRejection:
    """Unconsumed arguments are an error, not silently ignored."""

    async def test_no_arg_command_rejects_extras(self, command_handler):
        result = await command_handler.execute("close now please")
        assert result.success is False
        assert "Unexpected argument(s): now please" in result.message

    async def test_arg_command_rejects_extras(self, command_handler):
        result = await command_handler.execute("power on off extra")
        assert result.success is False
        assert "Unexpected argument(s): off extra" in result.message

    async def test_holdtime_typo_rejected(self, command_handler):
        """'holdtime 5 3' (meant 5.3) must not silently set 5."""
        command_handler.simulator.state.hold_time = 8.0
        result = await command_handler.execute("holdtime 5 3")
        assert result.success is False
        assert "Unexpected argument(s): 3" in result.message
        assert command_handler.simulator.state.hold_time == 8.0

    async def test_subcommand_rejects_extras(self, command_handler):
        result = await command_handler.execute("ac toggle extra")
        assert result.success is False
        assert "Unexpected argument(s): extra" in result.message

    async def test_no_arg_command_help_still_works(self, command_handler):
        """'close help' shows the description instead of executing close."""
        result = await command_handler.execute("close help")
        assert result.success is True
        assert "Close the door" in result.message


# ============================================================================
# CLI Mode Registry Restore Tests
# ============================================================================


class TestCliModeRestore:
    """set_cli_mode(False) must fully restore the exit command."""

    @pytest.fixture(autouse=True)
    def cli_mode_guard(self, command_handler):
        """Always leave the *global* registry out of CLI mode.

        Without this, a failing assertion between set_cli_mode(True) and
        set_cli_mode(False) leaves every later test in this xdist worker
        looking at a corrupted registry - a cascade of misleading failures
        around the one real one (R3-T2).
        """
        try:
            yield
        finally:
            command_handler.set_cli_mode(False)

    async def test_exit_restored_after_cli_mode(self, command_handler):
        from powerpetdoor.simulator.commands.base import get_command_registry

        registry = get_command_registry()
        original_exit = registry["exit"]
        original_shutdown_aliases = list(registry["shutdown"].aliases)

        command_handler.set_cli_mode(True)
        # In CLI mode exit/q/quit alias shutdown
        assert registry["exit"] is registry["shutdown"]
        assert registry["q"] is registry["shutdown"]

        command_handler.set_cli_mode(False)
        # exit and ALL its aliases must be back
        assert registry["exit"] is original_exit
        assert registry["q"] is original_exit
        assert registry["quit"] is original_exit
        # shutdown aliases restored, still a list (dataclass declares list)
        assert registry["shutdown"].aliases == original_shutdown_aliases
        assert isinstance(registry["shutdown"].aliases, list)
        assert isinstance(registry["exit"].aliases, list)

    async def test_exit_executes_after_cli_mode_round_trip(self, command_handler):
        command_handler.set_cli_mode(True)
        command_handler.set_cli_mode(False)
        command_handler.set_interactive_mode(True)

        result = await command_handler.execute("exit")
        assert result.success is True
        assert "handled locally" in result.message


# ============================================================================
# Help / Usage Consistency Tests
# ============================================================================


class TestHelpUsageConsistency:
    """Late-registered subcommands must appear in usage and help."""

    async def test_schedule_usage_lists_subcommands(self, command_handler):
        from powerpetdoor.simulator.commands.base import get_command_registry

        usage = get_command_registry()["schedule"].usage
        assert usage is not None
        for sub in ("add", "clear", "delete", "list"):
            assert sub in usage

    async def test_notify_and_broadcast_usage_present(self, command_handler):
        from powerpetdoor.simulator.commands.base import get_command_registry

        registry = get_command_registry()
        assert registry["notify"].usage is not None
        assert "inside_on" in registry["notify"].usage
        assert registry["broadcast"].usage is not None
        assert "settings" in registry["broadcast"].usage

    async def test_top_level_help_shows_schedule_usage(self, command_handler):
        result = await command_handler.execute("help")
        assert result.success is True
        assert "schedule (sched) [add|" in result.message

    async def test_arg_command_help_lists_subcommands(self, command_handler):
        """'power help' must mention the registered toggle subcommand."""
        result = await command_handler.execute("power help")
        assert result.success is True
        assert "Subcommands:" in result.message
        assert "toggle" in result.message

    async def test_schedule_add_help_shows_friendly_default(self, command_handler):
        """The days default renders as 'all', not a raw Python list."""
        result = await command_handler.execute("schedule add help")
        assert result.success is True
        assert "default: all" in result.message
        assert "[1, 1" not in result.message

    async def test_history_usage_is_meaningful(self, command_handler):
        from powerpetdoor.simulator.commands.base import get_command_registry

        assert get_command_registry()["history"].usage == "[clear|N]"

    async def test_timezone_arg_type_is_string(self, command_handler):
        from powerpetdoor.simulator.commands.base import get_command_registry

        assert get_command_registry()["timezone"].args[0].arg_type == "string"


# ============================================================================
# Script Run Tests (wait mode and path restrictions)
# ============================================================================

PASSING_SCRIPT = """\
name: Passing Script
steps:
  - action: log
    message: ok
"""

FAILING_SCRIPT = """\
name: Failing Script
steps:
  - action: bogus_action
"""


class TestRunWaitMode:
    """run <script> wait executes synchronously and reflects pass/fail."""

    @pytest.fixture
    def queued_handler(self, simulator):
        """A handler with a script queue, like daemon/interactive mode."""
        handler = CommandHandler(
            simulator=simulator,
            script_runner=ScriptRunner(simulator),
            stop_callback=MagicMock(),
            script_queue=ScriptQueue(),
        )
        return handler

    async def test_run_queues_by_default(self, queued_handler, tmp_path):
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)

        result = await queued_handler.execute(f"run {script}")
        assert result.success is True
        assert "Queued script" in result.message
        assert queued_handler.script_queue.qsize() == 1

    async def test_run_wait_reports_pass(self, queued_handler, tmp_path):
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)

        result = await queued_handler.execute(f"run {script} wait")
        assert result.success is True
        assert "Script PASSED" in result.message
        # Not queued - ran synchronously
        assert queued_handler.script_queue.qsize() == 0

    async def test_run_wait_reports_fail(self, queued_handler, tmp_path):
        script = tmp_path / "fail.yaml"
        script.write_text(FAILING_SCRIPT)

        result = await queued_handler.execute(f"run {script} wait")
        assert result.success is False
        assert "Script FAILED" in result.message

    async def test_run_invalid_mode_rejected(self, queued_handler, tmp_path):
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)

        result = await queued_handler.execute(f"run {script} sideways")
        assert result.success is False
        assert "not valid" in result.message

    async def test_run_wait_is_refused_while_another_script_runs(self, queued_handler, tmp_path):
        """Concurrent script execution is impossible; the caller is told why.

        A wait-run's exit code must belong to the script that was asked
        for, so it never queues behind an in-flight script.
        """
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)
        runner = queued_handler.script_runner
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_step(step):
            entered.set()
            await release.wait()

        runner._execute_step = blocking_step
        busy = asyncio.ensure_future(
            runner.run(Script(name="Long Script", steps=[ScriptStep(action="open", params={})]))
        )
        async with asyncio.timeout(2.0):
            await entered.wait()

        # Bounded: without the fast-fail guard this call waits on the run
        # lock that only `release` frees, and the test would hang forever
        # instead of failing (R3-M4).
        async with asyncio.timeout(2.0):
            result = await queued_handler.execute(f"run {script} wait")

        assert result.success is False
        assert result.message == "Another script is already running: Long Script"

        release.set()
        await asyncio.wait_for(busy, 2.0)

    async def test_plain_run_still_queues_behind_a_wait_run(self, queued_handler, tmp_path):
        """Queueing during a wait-run is accepted and runs afterwards."""
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)
        runner = queued_handler.script_runner
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_step(step):
            entered.set()
            await release.wait()

        runner._execute_step = blocking_step
        busy = asyncio.ensure_future(
            runner.run(Script(name="Long Script", steps=[ScriptStep(action="open", params={})]))
        )
        async with asyncio.timeout(2.0):
            await entered.wait()

        result = await queued_handler.execute(f"run {script}")

        assert result.success is True
        assert result.message == "Queued script: Passing Script"
        assert queued_handler.script_queue.qsize() == 1

        release.set()
        await asyncio.wait_for(busy, 2.0)


class TestScriptQueue:
    """The queue counts the run already taken off it (M2)."""

    async def test_put_then_get_preserves_order(self):
        queue = ScriptQueue()
        await queue.put("a")
        await queue.put("b")

        assert await queue.get() == "a"
        assert await queue.get() == "b"

    async def test_a_claimed_run_is_still_pending(self):
        queue = ScriptQueue()
        await queue.put("a")

        assert queue.qsize() == 1
        claimed = await queue.get()
        # Dequeued but not started: still waiting, from the operator's view.
        assert queue.qsize() == 1
        assert queue.pending() == ["a"]

        queue.release(claimed)
        assert queue.qsize() == 0
        assert queue.pending() == []

    async def test_release_is_idempotent(self):
        """The consumer releases on start and again in its finally."""
        queue = ScriptQueue()
        await queue.put("a")
        claimed = await queue.get()

        queue.release(claimed)
        queue.release(claimed)  # must not raise

        assert queue.qsize() == 0

    async def test_pending_lists_claimed_first(self):
        queue = ScriptQueue()
        await queue.put("a")
        await queue.put("b")
        await queue.get()

        assert queue.pending() == ["a", "b"]

    async def test_clear_drops_unclaimed_runs_only(self):
        queue = ScriptQueue()
        for ref in ("a", "b", "c"):
            await queue.put(ref)
        await queue.get()  # "a" is claimed and already starting

        assert queue.clear() == ["b", "c"]
        assert queue.pending() == ["a"]

    async def test_get_waits_for_an_arrival(self):
        queue = ScriptQueue()
        getter = asyncio.ensure_future(queue.get())
        await asyncio.sleep(0)
        assert not getter.done()

        await queue.put("late")

        assert await asyncio.wait_for(getter, 2.0) == "late"

    async def test_get_can_be_polled_with_a_timeout(self):
        """_process_script_queue wraps get() in wait_for; it must be cancellable."""
        queue = ScriptQueue()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.01)

        # A cancelled get must not have consumed anything.
        await queue.put("still-here")
        assert await asyncio.wait_for(queue.get(), 2.0) == "still-here"


class TestScriptBusyVisibility:
    """Serialized runs made "busy" a real state; it must be observable (M5)."""

    @pytest.fixture
    def queued_handler(self, simulator):
        handler = CommandHandler(
            simulator=simulator,
            script_runner=ScriptRunner(simulator),
            stop_callback=MagicMock(),
            script_queue=ScriptQueue(),
        )
        return handler

    @staticmethod
    async def _start_blocking_script(runner, name="Slow Script"):
        """Start a two-step script parked on its first step.

        Two steps, so a stop request has a step boundary to be noticed at.
        Returns (task, release).
        """
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_step(step):
            entered.set()
            await release.wait()

        runner._execute_step = blocking_step
        steps = [ScriptStep(action="open", params={}), ScriptStep(action="close", params={})]
        task = asyncio.ensure_future(runner.run(Script(name=name, steps=steps)))
        async with asyncio.timeout(2.0):
            await entered.wait()
        return task, release

    async def test_status_reports_the_running_script(self, queued_handler):
        task, release = await self._start_blocking_script(queued_handler.script_runner)
        try:
            result = await queued_handler.execute("status")
            assert result.success is True
            assert '  Script: running "Slow Script"' in result.message
            assert result.data["running_script"] == "Slow Script"
            assert result.data["queued_scripts"] == 0
        finally:
            release.set()
            await asyncio.wait_for(task, 2.0)

    async def test_status_reports_the_queue_depth(self, queued_handler, tmp_path):
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)
        task, release = await self._start_blocking_script(queued_handler.script_runner)
        try:
            await queued_handler.execute(f"run {script}")
            result = await queued_handler.execute("status")
            assert '  Script: running "Slow Script" (1 queued)' in result.message
            assert result.data["queued_scripts"] == 1
        finally:
            release.set()
            await asyncio.wait_for(task, 2.0)

    async def test_status_reports_no_script_when_idle(self, queued_handler):
        result = await queued_handler.execute("status")
        assert "  Script: none running" in result.message
        assert result.data["running_script"] is None

    async def test_list_reports_the_running_script(self, queued_handler):
        task, release = await self._start_blocking_script(queued_handler.script_runner)
        try:
            result = await queued_handler.execute("list")
            assert result.message.endswith('\nScript: running "Slow Script"')
            assert result.data["running"] == "Slow Script"
        finally:
            release.set()
            await asyncio.wait_for(task, 2.0)

    async def test_stop_ends_the_running_script(self, queued_handler):
        """`stop` stops the script, and the run reports FAILED (M5)."""
        runner = queued_handler.script_runner
        task, release = await self._start_blocking_script(runner)
        try:
            result = await queued_handler.execute("stop")
            assert result.success is True
            assert result.message == "Stopping script: Slow Script"
            assert runner._stop_requested is True
        finally:
            release.set()
            assert await asyncio.wait_for(task, 2.0) is False

    async def test_stop_without_a_running_script_reports_so(self, queued_handler):
        result = await queued_handler.execute("stop")
        assert result.success is False
        # `stop` was an alias for `shutdown` until this release, so muscle
        # memory is the likeliest reason it lands on an idle simulator (T4).
        assert result.message == "No script is running (use 'shutdown' to stop the simulator)"

    async def test_stop_does_not_shut_the_simulator_down(self, queued_handler):
        """The whole point of dropping the shutdown alias (M5)."""
        await queued_handler.execute("stop")
        queued_handler.stop_callback.assert_not_called()

    async def test_shutdown_no_longer_answers_to_stop(self, queued_handler):
        result = await queued_handler.execute("shutdown")
        assert result.success is True
        assert result.message == "Shutting down..."
        queued_handler.stop_callback.assert_called_once_with()

    async def test_status_shows_a_pending_stop(self, queued_handler):
        """A requested-but-not-yet-effective stop is visible (L3).

        `stop` takes effect at a step boundary, so an operator watching
        `status` could not tell a registered stop from one that never
        arrived - the run above reported `running` before and after.
        """
        task, release = await self._start_blocking_script(queued_handler.script_runner)
        try:
            await queued_handler.execute("stop")
            result = await queued_handler.execute("status")
            assert '  Script: stopping "Slow Script"' in result.message
        finally:
            release.set()
            await asyncio.wait_for(task, 2.0)

    async def test_repeat_stop_says_the_first_one_registered(self, queued_handler):
        """A second `stop` used to answer with a fresh success (L3)."""
        task, release = await self._start_blocking_script(queued_handler.script_runner)
        try:
            assert (await queued_handler.execute("stop")).message == "Stopping script: Slow Script"
            again = await queued_handler.execute("stop")
            assert again.success is True
            assert again.message == "Stop already requested for: Slow Script"
        finally:
            release.set()
            await asyncio.wait_for(task, 2.0)

    async def test_status_counts_a_dequeued_but_unstarted_run(self, queued_handler):
        """The `(N queued)` indicator used to under-report by one (M2).

        The queue consumer takes an entry off as soon as one exists and
        only then waits for the run lock, so the commonest case - one
        script waiting behind a `run ... wait` - displayed as "nothing
        pending".
        """
        task, release = await self._start_blocking_script(queued_handler.script_runner)
        try:
            await queued_handler.script_queue.put("my_custom")
            # Exactly what _process_script_queue does before blocking on
            # the run lock.
            claimed = await queued_handler.script_queue.get()
            assert claimed == "my_custom"

            result = await queued_handler.execute("status")
            assert '  Script: running "Slow Script" (1 queued)' in result.message
            assert result.data["queued_scripts"] == 1

            listed = await queued_handler.execute("list")
            assert "Queued: my_custom" in listed.message
            assert listed.data["pending"] == ["my_custom"]
        finally:
            release.set()
            await asyncio.wait_for(task, 2.0)

    async def test_stop_all_drains_the_queue_in_one_command(self, queued_handler):
        """Queued runs had to be cancelled one `stop` at a time (L2)."""
        task, release = await self._start_blocking_script(queued_handler.script_runner)
        try:
            for _ in range(3):
                await queued_handler.script_queue.put("my_custom")

            result = await queued_handler.execute("stop all")

            assert result.success is True
            assert result.message == "Stopping script: Slow Script (dropped 3 queued)"
            assert queued_handler.script_queue.qsize() == 0
        finally:
            release.set()
            await asyncio.wait_for(task, 2.0)

    async def test_stop_all_with_nothing_running_still_drains(self, queued_handler):
        """Draining the queue is worth reporting even with nothing in flight."""
        await queued_handler.script_queue.put("my_custom")

        result = await queued_handler.execute("stop all")

        assert result.success is True
        assert result.message == "Dropped 1 queued script(s)"
        assert queued_handler.script_queue.qsize() == 0

    async def test_stop_all_with_nothing_at_all_reports_idle(self, queued_handler):
        result = await queued_handler.execute("stop all")

        assert result.success is False
        assert result.message == "No script is running (use 'shutdown' to stop the simulator)"

    async def test_stop_rejects_an_unknown_scope(self, queued_handler):
        result = await queued_handler.execute("stop everything")

        assert result.success is False
        assert "everything" in result.message


class TestScriptPathRestrictions:
    """load_script path handling for local vs control-channel handlers."""

    @pytest.fixture
    def restricted_handler(self, simulator, tmp_path):
        """A handler configured like the daemon control channel."""
        return CommandHandler(
            simulator=simulator,
            script_runner=ScriptRunner(simulator),
            stop_callback=MagicMock(),
            scripts_dir=str(tmp_path),
            allow_script_paths=False,
        )

    async def test_local_handler_accepts_paths(self, command_handler, tmp_path):
        """The local interactive CLI may still run arbitrary paths."""
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)

        result = await command_handler.execute(f"run {script}")
        assert result.success is True
        assert "Script PASSED" in result.message

    async def test_restricted_rejects_absolute_path(self, restricted_handler, tmp_path):
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)

        result = await restricted_handler.execute(f"run {script}")
        assert result.success is False
        assert "not allowed" in result.message

    async def test_restricted_rejects_traversal(self, restricted_handler):
        result = await restricted_handler.execute("run ../../etc/passwd")
        assert result.success is False
        assert "not allowed" in result.message

    async def test_restricted_rejects_hidden_names(self, restricted_handler):
        result = await restricted_handler.execute("run .sneaky")
        assert result.success is False
        assert "not allowed" in result.message

    async def test_restricted_resolves_scripts_dir_name(self, restricted_handler, tmp_path):
        (tmp_path / "goodscript.yaml").write_text(PASSING_SCRIPT)

        result = await restricted_handler.execute("run goodscript")
        assert result.success is True
        assert "Script PASSED" in result.message

    async def test_restricted_builtin_name_still_loads(self, restricted_handler):
        """Built-in scripts remain available by bare name (load only)."""
        script = restricted_handler.load_script("pet_presence_test")
        assert script.name


class TestScriptsDirVisibility:
    """--scripts-dir scripts must be discoverable, not just runnable (M4)."""

    @pytest.fixture
    def scripts_dir_handler(self, simulator, tmp_path):
        (tmp_path / "my_custom.yaml").write_text(
            "name: My Custom Script\ndescription: Local extras\nsteps:\n  - action: log\n"
            "    message: ok\n"
        )
        return CommandHandler(
            simulator=simulator,
            script_runner=ScriptRunner(simulator),
            stop_callback=MagicMock(),
            scripts_dir=str(tmp_path),
            allow_script_paths=False,
        )

    async def test_list_shows_scripts_dir_entries(self, scripts_dir_handler, tmp_path):
        result = await scripts_dir_handler.execute("list")

        assert result.success is True
        assert f"Scripts from {tmp_path}:" in result.message
        assert "  my_custom: Local extras" in result.message
        assert ("my_custom", "Local extras") in result.data["scripts"]

    async def test_unknown_script_error_names_scripts_dir_entries(self, scripts_dir_handler):
        result = await scripts_dir_handler.execute("run bogus")

        assert result.success is False
        assert result.message.startswith("Unknown script: bogus. Available: ")
        assert "my_custom" in result.message

    async def test_completion_offers_scripts_dir_entries(self, scripts_dir_handler):
        from powerpetdoor.simulator.scripting import script_completer

        assert ("my_custom", "Local extras") in script_completer("")

    async def test_list_shows_only_builtins_without_a_scripts_dir(self, command_handler):
        result = await command_handler.execute("list")

        assert result.success is True
        assert "Scripts from" not in result.message


# ============================================================================
# is_wait_run (R3-M3)
# ============================================================================


class TestIsWaitRun:
    """The helper that removes ctl's response deadline entirely.

    Getting it wrong either hangs the operator indefinitely (a plain
    command treated as a wait-run) or times out mid-script (a wait-run
    treated as a plain command). 100% branch coverage does not help: the
    whole function is one boolean expression, so short-circuit paths are
    not separate coverage arcs - only a value table pins it.
    """

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Arity: three parts, the last of which is the keyword.
            ("run foo wait", True),
            ("run foo/bar.yaml wait", True),
            ("run foo wait extra", False),
            # A script *named* `wait` is not a wait-run.
            ("run wait", False),
            ("wait", False),
            ("", False),
            ("   ", False),
            # Every alias of `run`, since the daemon accepts them all.
            ("r foo wait", True),
            ("file foo wait", True),
            # Command case: the daemon's dispatch is case-insensitive.
            ("RUN foo wait", True),
            ("Run foo wait", True),
            ("R foo wait", True),
            ("FILE foo wait", True),
            # Keyword case: parse_arg's choice matching is case-insensitive.
            ("run foo WAIT", True),
            ("run foo Wait", True),
            # Not the run command at all.
            ("status wait", False),
            ("shutdown foo wait", False),
            ("runner foo wait", False),
            # Right command, wrong keyword.
            ("run foo waitt", False),
            ("run foo now", False),
        ],
    )
    def test_is_wait_run(self, line, expected):
        assert is_wait_run(line) is expected
