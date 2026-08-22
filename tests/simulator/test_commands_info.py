# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for info/status/help/broadcast/history commands (commands/info.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import powerpetdoor.simulator.prompt_common as prompt_common
from powerpetdoor.const import DOOR_STATE_HOLDING
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.commands.history import History
from powerpetdoor.simulator.scripting import ScriptRunner
from powerpetdoor.simulator.state import Schedule

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def timing_config():
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
    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
def command_handler(simulator):
    return CommandHandler(
        simulator=simulator,
        script_runner=ScriptRunner(simulator),
        stop_callback=MagicMock(),
    )


@pytest.fixture
def mock_client(simulator):
    protocol = MagicMock()
    protocol._door_task = None
    simulator.protocols.append(protocol)
    yield protocol
    simulator.protocols.clear()


@pytest.fixture
def interactive_handler(command_handler):
    """A handler in interactive mode with an in-memory History attached."""
    command_handler.set_interactive_mode(True)
    history = History()  # In-memory
    command_handler.set_history(history)
    return command_handler


# ============================================================================
# status command
# ============================================================================


class TestStatusFullText:
    async def test_configured_state_renders_every_section(self, command_handler):
        s = command_handler.simulator.state
        s.door_status = DOOR_STATE_HOLDING
        s.power = False
        s.auto = False
        s.inside = True
        s.outside = False
        s.safety_lock = True
        s.cmd_lockout = True
        s.autoretract = False
        s.hold_time = 3.5
        s.battery_percent = 50
        s.battery_present = True
        s.ac_present = False
        s.battery_config.discharge_rate = 0.1
        s.sensor_on_indoor = False
        s.sensor_off_indoor = False
        s.sensor_on_outdoor = False
        s.sensor_off_outdoor = False
        s.low_battery = True
        s.inside_sensor_active = True
        s.outside_sensor_active = False
        s.schedules[0] = Schedule(index=0, inside=True)
        s.total_open_cycles = 7
        s.total_auto_retracts = 2

        result = await command_handler.execute("status")
        assert result.success is True
        assert result.message == (
            "Current State:\n"
            "  Clients: none\n"
            f"  Door: {DOOR_STATE_HOLDING}\n"
            "  Power: OFF\n"
            "  Auto (schedule): OFF\n"
            "  Inside sensor: enabled\n"
            "  Outside sensor: disabled\n"
            "  Safety lock: ON\n"
            "  Command lockout: ON\n"
            "  Auto-retract: OFF\n"
            "  Hold time: 3.5s\n"
            "  Battery: 50% (discharging 0.1%/min)\n"
            "  AC: disconnected\n"
            "  Notifications: low_bat\n"
            "  Sensor active: inside\n"
            "  Schedules: [0]\n"
            "  Open cycles: 7\n"
            "  Auto-retracts: 2\n"
            "  Script: none running"
        )
        assert result.data["door"] == DOOR_STATE_HOLDING
        assert result.data["schedules"] == [0]
        assert result.data["battery_percent"] == 50
        assert result.data["running_script"] is None
        assert result.data["queued_scripts"] == 0

    async def test_status_aliases(self, command_handler):
        for alias in ("state", "info", "v"):
            result = await command_handler.execute(alias)
            assert result.success is True
            assert result.message.startswith("Current State:")


class TestStatusBatteryVariants:
    async def test_no_battery(self, command_handler):
        s = command_handler.simulator.state
        s.battery_present = False
        result = await command_handler.execute("status")
        assert "  Battery: 100% (no battery)" in result.message

    async def test_charging(self, command_handler):
        s = command_handler.simulator.state
        s.battery_percent = 50
        s.ac_present = True
        s.battery_config.charge_rate = 1.0
        result = await command_handler.execute("status")
        assert "  Battery: 50% (charging 1.0%/min)" in result.message

    async def test_full_battery_no_suffix(self, command_handler):
        # Defaults: AC connected, 100% - neither charging nor discharging shown
        result = await command_handler.execute("status")
        assert "  Battery: 100%\n" in result.message

    async def test_empty_battery_no_discharge_suffix(self, command_handler):
        s = command_handler.simulator.state
        s.battery_percent = 0
        s.ac_present = False
        result = await command_handler.execute("status")
        assert "  Battery: 0%\n" in result.message


class TestStatusNotifyAndSensorStrings:
    async def test_all_notifications_on(self, command_handler):
        s = command_handler.simulator.state
        s.sensor_on_indoor = True
        s.sensor_off_indoor = True
        s.sensor_on_outdoor = True
        s.sensor_off_outdoor = True
        s.low_battery = True
        result = await command_handler.execute("status")
        assert "  Notifications: in_on, in_off, out_on, out_off, low_bat" in result.message

    async def test_low_battery_off_others_on(self, command_handler):
        s = command_handler.simulator.state
        s.sensor_on_indoor = True
        s.sensor_off_indoor = True
        s.sensor_on_outdoor = True
        s.sensor_off_outdoor = True
        s.low_battery = False
        result = await command_handler.execute("status")
        assert "  Notifications: in_on, in_off, out_on, out_off\n" in result.message

    async def test_no_notifications(self, command_handler):
        s = command_handler.simulator.state
        s.low_battery = False
        result = await command_handler.execute("status")
        assert "  Notifications: none" in result.message

    async def test_both_sensors_active(self, command_handler):
        s = command_handler.simulator.state
        s.inside_sensor_active = True
        s.outside_sensor_active = True
        result = await command_handler.execute("status")
        assert "  Sensor active: inside, outside" in result.message

    async def test_outside_sensor_active(self, command_handler):
        s = command_handler.simulator.state
        s.outside_sensor_active = True
        result = await command_handler.execute("status")
        assert "  Sensor active: outside" in result.message


# ============================================================================
# broadcast subcommands
# ============================================================================


class TestBroadcastSubcommands:
    async def test_settings(self, command_handler, mock_client):
        result = await command_handler.execute("broadcast settings")
        assert result.success is True
        assert result.message == "Broadcast settings"
        assert mock_client._send.called

    async def test_battery_on_ac(self, command_handler, mock_client):
        result = await command_handler.execute("broadcast battery")
        assert result.success is True
        assert result.message == "Broadcast battery: 100% (AC)"

    async def test_battery_no_ac(self, command_handler, mock_client):
        s = command_handler.simulator.state
        s.ac_present = False
        s.battery_percent = 42
        result = await command_handler.execute("broadcast battery")
        assert result.message == "Broadcast battery: 42% (no AC)"

    async def test_hwinfo(self, command_handler, mock_client):
        result = await command_handler.execute("broadcast hwinfo")
        assert result.success is True
        assert result.message == "Broadcast hwinfo: fw 1.2.3, hw 1 rev 1"
        assert mock_client._send.called

    async def test_stats(self, command_handler, mock_client):
        s = command_handler.simulator.state
        s.total_open_cycles = 4
        s.total_auto_retracts = 1
        result = await command_handler.execute("broadcast stats")
        assert result.success is True
        assert result.message == "Broadcast stats: 4 cycles, 1 retracts"

    async def test_schedules(self, command_handler, mock_client):
        command_handler.simulator.state.schedules[0] = Schedule(index=0, inside=True)
        result = await command_handler.execute("broadcast schedules")
        assert result.success is True
        assert result.message == "Broadcast schedules: 1 schedule(s)"

    async def test_notifications(self, command_handler, mock_client):
        result = await command_handler.execute("broadcast notifications")
        assert result.success is True
        assert result.message == "Broadcast notifications"

    @pytest.mark.parametrize(
        "subcommand",
        ["settings", "battery", "hwinfo", "stats", "schedules", "notifications", "all", "status"],
    )
    async def test_all_subcommands_require_clients(self, command_handler, subcommand):
        result = await command_handler.execute(f"broadcast {subcommand}")
        assert result.success is False
        assert result.message == "No clients connected"


# ============================================================================
# argument help rendering
# ============================================================================


class TestArgHelp:
    async def test_holdtime_help_shows_bounds(self, command_handler):
        result = await command_handler.execute("holdtime help")
        assert result.success is True
        assert result.message.startswith("holdtime [seconds]")
        assert "Set or show hold time in seconds" in result.message
        assert "min: 0.1" in result.message
        assert "max: 900" in result.message
        assert "[optional]" in result.message

    async def test_schedule_delete_help_shows_min_and_required(self, command_handler):
        result = await command_handler.execute("schedule delete help")
        assert result.success is True
        assert "index: Schedule index [required] (min: 0)" in result.message

    async def test_question_mark_alias_for_help(self, command_handler):
        result = await command_handler.execute("battery ?")
        assert result.success is True
        assert result.message.startswith("battery [percent]")
        assert "min: 0" in result.message
        assert "max: 100" in result.message


# ============================================================================
# history command (through execute, with a real History)
# ============================================================================


class TestHistoryCommand:
    async def test_empty_history(self, interactive_handler):
        result = await interactive_handler.execute("history")
        assert result.success is True
        assert result.message == "No history"

    async def test_shows_entries_with_ids(self, interactive_handler):
        for cmd in ("status", "power on", "close"):
            interactive_handler._history.append_string(cmd)
        result = await interactive_handler.execute("history")
        assert result.success is True
        assert result.message == (
            "History (3 of 3 commands):\n      1  status\n      2  power on\n      3  close"
        )

    async def test_limit_argument(self, interactive_handler):
        for cmd in ("status", "power on", "close"):
            interactive_handler._history.append_string(cmd)
        result = await interactive_handler.execute("history 2")
        assert result.message == "History (2 of 3 commands):\n      2  power on\n      3  close"

    async def test_hist_alias(self, interactive_handler):
        interactive_handler._history.append_string("status")
        result = await interactive_handler.execute("hist")
        assert result.success is True
        assert result.message == "History (1 of 1 commands):\n      1  status"

    async def test_clear(self, interactive_handler):
        interactive_handler._history.append_string("status")
        result = await interactive_handler.execute("history clear")
        assert result.success is True
        assert result.message == "History cleared"

        result = await interactive_handler.execute("history")
        assert result.message == "No history"

    async def test_zero_rejected(self, interactive_handler):
        result = await interactive_handler.execute("history 0")
        assert result.success is False
        assert result.message == "Number must be positive"

    async def test_invalid_argument(self, interactive_handler):
        result = await interactive_handler.execute("history bogus")
        assert result.success is False
        assert result.message == "Invalid argument: bogus. Use 'clear' or a number."

    async def test_clear_truncates_history_file(self, command_handler, tmp_path):
        hist_file = tmp_path / "history"
        command_handler.set_interactive_mode(True)
        history = History(hist_file)
        command_handler.set_history(history)
        history.prompt_toolkit_history.append_string("status")
        assert hist_file.read_text() != ""

        result = await command_handler.execute("history clear")
        assert result.success is True
        assert result.message == "History cleared"
        assert hist_file.read_text() == ""

    async def test_clear_with_minimal_backend_without_cache(self, command_handler, tmp_path):
        """Backends without prompt_toolkit's private cache attr still clear
        their file; the hasattr guards skip the in-memory step."""
        hist_file = tmp_path / "history"
        hist_file.write_text("# 1\n+status\n")

        class MinimalBackend:
            filename = str(hist_file)

        command_handler.set_interactive_mode(True)
        command_handler._history = MinimalBackend()
        result = await command_handler.execute("history clear")
        assert result.success is True
        assert result.message == "History cleared"
        assert hist_file.read_text() == ""

    async def test_clear_error_reported(self, interactive_handler):
        stub = MagicMock()
        stub._loaded_strings.clear.side_effect = RuntimeError("boom")
        interactive_handler._history = stub
        result = await interactive_handler.execute("history clear")
        assert result.success is False
        assert result.message == "Error clearing history: boom"

    async def test_read_error_reported(self, interactive_handler):
        stub = MagicMock()
        stub.get_strings.side_effect = RuntimeError("boom")
        interactive_handler._history = stub
        result = await interactive_handler.execute("history")
        assert result.success is False
        assert result.message == "Error reading history: boom"

    async def test_unavailable_message_when_no_history_object(self, command_handler, monkeypatch):
        # prompt_toolkit is nominally usable, but no history was registered
        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: True)
        command_handler.set_interactive_mode(True)
        result = await command_handler.execute("history")
        assert result.success is False
        assert result.message == (
            "History not available. Install prompt_toolkit for history support:\n"
            "  pip install pypowerpetdoor[interactive]"
        )

    async def test_hidden_when_prompt_toolkit_unusable(self, command_handler, monkeypatch):
        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: False)
        command_handler.set_interactive_mode(True)
        result = await command_handler.execute("history")
        assert result.success is False
        assert result.message == "Unknown command: history. Type 'help' for commands."


# ============================================================================
# get_help visibility
# ============================================================================


class TestHelpVisibility:
    async def test_history_listed_when_available(self, interactive_handler):
        result = await interactive_handler.execute("help")
        assert result.success is True
        assert "history (hist) [clear|N]" in result.message

    async def test_history_hidden_when_unusable(self, command_handler, monkeypatch):
        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: False)
        command_handler.set_interactive_mode(True)
        result = await command_handler.execute("help")
        assert result.success is True
        assert "history (hist)" not in result.message

    async def test_exit_listed_outside_cli_mode(self, command_handler):
        command_handler.set_interactive_mode(True)
        result = await command_handler.execute("help")
        assert "exit (q, quit)" in result.message
