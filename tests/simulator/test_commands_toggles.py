# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for button, notification, simulation, door, control, and script-list
commands (commands/{buttons,notifications,simulation,door,control,scripts}.py).
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from powerpetdoor.const import (
    CMD_DISABLE_AUTO,
    CMD_DISABLE_INSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_ENABLE_AUTO,
    CMD_ENABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_NOTIFICATIONS,
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    FIELD_AUTO,
    FIELD_CMD,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_POWER,
    SUCCESS_FALSE,
    SUCCESS_TRUE,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.scripting import ScriptRunner, list_builtin_scripts

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def timing_config():
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_start_time=0.02,
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
def root_logger_level():
    """Save and restore the root logger level around debug-command tests."""
    root = logging.getLogger()
    saved = root.level
    yield root
    root.setLevel(saved)


def _last_payload(protocol):
    return protocol._send.call_args.args[0]


# ============================================================================
# Button commands (power / auto / inside_enable / outside_enable)
# ============================================================================


class TestPowerCommand:
    async def test_bare_toggles_both_directions(self, command_handler):
        state = command_handler.simulator.state
        state.power = True

        result = await command_handler.execute("power")
        assert result.success is True
        assert result.message == "Power: OFF"
        assert state.power is False

        result = await command_handler.execute("power")
        assert result.message == "Power: ON"
        assert state.power is True

    async def test_set_on_off_broadcasts(self, command_handler, mock_client):
        state = command_handler.simulator.state
        state.power = True

        result = await command_handler.execute("power off")
        assert result.message == "Power: OFF"
        assert state.power is False
        payload = _last_payload(mock_client)
        assert payload[FIELD_CMD] == CMD_POWER_OFF
        assert payload[FIELD_POWER] == SUCCESS_FALSE

        result = await command_handler.execute("power on")
        assert result.message == "Power: ON"
        payload = _last_payload(mock_client)
        assert payload[FIELD_CMD] == CMD_POWER_ON
        assert payload[FIELD_POWER] == SUCCESS_TRUE

    async def test_toggle_subcommand_and_aliases(self, command_handler):
        state = command_handler.simulator.state
        state.power = True

        result = await command_handler.execute("power toggle")
        assert result.message == "Power: OFF"

        result = await command_handler.execute("p t")
        assert result.message == "Power: ON"
        assert state.power is True

    async def test_invalid_value(self, command_handler):
        result = await command_handler.execute("power maybe")
        assert result.success is False
        assert result.message == "'maybe' is not valid. Use on/off\nUsage: power [on|off]"


class TestAutoCommand:
    async def test_set_on_off_broadcasts(self, command_handler, mock_client):
        state = command_handler.simulator.state
        state.auto = True

        result = await command_handler.execute("auto off")
        assert result.message == "Auto (schedule): OFF"
        assert state.auto is False
        payload = _last_payload(mock_client)
        assert payload[FIELD_CMD] == CMD_DISABLE_AUTO
        assert payload[FIELD_AUTO] == SUCCESS_FALSE

        result = await command_handler.execute("auto on")
        assert result.message == "Auto (schedule): ON"
        payload = _last_payload(mock_client)
        assert payload[FIELD_CMD] == CMD_ENABLE_AUTO
        assert payload[FIELD_AUTO] == SUCCESS_TRUE

    async def test_bare_toggle_alias_and_subcommand(self, command_handler):
        state = command_handler.simulator.state
        state.auto = True

        result = await command_handler.execute("m")
        assert result.message == "Auto (schedule): OFF"
        assert state.auto is False

        result = await command_handler.execute("auto toggle")
        assert result.message == "Auto (schedule): ON"
        assert state.auto is True


class TestInsideEnableCommand:
    async def test_set_off_on_broadcasts(self, command_handler, mock_client):
        state = command_handler.simulator.state
        state.inside = True

        result = await command_handler.execute("inside_enable off")
        assert result.message == "Inside sensor: disabled"
        assert state.inside is False
        payload = _last_payload(mock_client)
        assert payload[FIELD_CMD] == CMD_DISABLE_INSIDE
        assert payload[FIELD_INSIDE] == 0

        result = await command_handler.execute("inside_enable on")
        assert result.message == "Inside sensor: enabled"
        payload = _last_payload(mock_client)
        assert payload[FIELD_CMD] == CMD_ENABLE_INSIDE
        assert payload[FIELD_INSIDE] == 1

    async def test_bare_toggle_alias_and_subcommand(self, command_handler):
        state = command_handler.simulator.state
        state.inside = True

        result = await command_handler.execute("n")
        assert result.message == "Inside sensor: disabled"
        assert state.inside is False

        result = await command_handler.execute("inside_enable toggle")
        assert result.message == "Inside sensor: enabled"
        assert state.inside is True

        result = await command_handler.execute("inside_enable t")
        assert result.message == "Inside sensor: disabled"
        assert state.inside is False


class TestOutsideEnableCommand:
    async def test_set_off_on_broadcasts(self, command_handler, mock_client):
        state = command_handler.simulator.state
        state.outside = True

        result = await command_handler.execute("outside_enable off")
        assert result.message == "Outside sensor: disabled"
        assert state.outside is False
        payload = _last_payload(mock_client)
        assert payload[FIELD_CMD] == CMD_DISABLE_OUTSIDE
        assert payload[FIELD_OUTSIDE] == 0

        result = await command_handler.execute("outside_enable on")
        assert result.message == "Outside sensor: enabled"
        payload = _last_payload(mock_client)
        assert payload[FIELD_CMD] == CMD_ENABLE_OUTSIDE
        assert payload[FIELD_OUTSIDE] == 1

    async def test_bare_toggle_alias_and_subcommand(self, command_handler):
        state = command_handler.simulator.state
        state.outside = True

        result = await command_handler.execute("u")
        assert result.message == "Outside sensor: disabled"
        assert state.outside is False

        result = await command_handler.execute("outside_enable t")
        assert result.message == "Outside sensor: enabled"
        assert state.outside is True


# ============================================================================
# Notification commands (remaining subcommands)
# ============================================================================


class TestNotifyRemaining:
    async def test_inside_off_set_and_toggle(self, command_handler, mock_client):
        state = command_handler.simulator.state
        state.sensor_off_indoor = False

        result = await command_handler.execute("notify inside_off on")
        assert result.success is True
        assert result.message == "Notification inside_off: ON"
        assert state.sensor_off_indoor is True
        assert _last_payload(mock_client)[FIELD_CMD] == CMD_SET_NOTIFICATIONS

        result = await command_handler.execute("notify inside_off")
        assert result.message == "Notification inside_off: OFF"
        assert state.sensor_off_indoor is False

    async def test_outside_off_set_and_toggle(self, command_handler):
        state = command_handler.simulator.state
        state.sensor_off_outdoor = False

        result = await command_handler.execute("notify outside_off on")
        assert result.message == "Notification outside_off: ON"
        assert state.sensor_off_outdoor is True

        result = await command_handler.execute("notify outside_off off")
        assert result.message == "Notification outside_off: OFF"
        assert state.sensor_off_outdoor is False

    async def test_low_battery_aliases(self, command_handler):
        state = command_handler.simulator.state
        state.low_battery = True

        result = await command_handler.execute("notify lowbat off")
        assert result.message == "Notification low_battery: OFF"
        assert state.low_battery is False

        result = await command_handler.execute("notify low_bat on")
        assert result.message == "Notification low_battery: ON"
        assert state.low_battery is True


# ============================================================================
# Simulation commands (obstruction)
# ============================================================================


class TestObstructionCommand:
    async def test_obstruction_places_a_one_shot(self, command_handler):
        state = command_handler.simulator.state

        result = await command_handler.execute("obstruction")
        assert result.success is True
        assert result.message == "Obstruction placed (cleared by the retract it causes)"
        assert state.obstruction_active is True
        assert state.obstruction_oneshot is True

    async def test_obstruction_arms_no_sensor(self, command_handler):
        """The distinction the command exists to make: it is not a collar."""
        state = command_handler.simulator.state

        await command_handler.execute("obstruction")
        assert state.inside_sensor_active is False
        assert state.outside_sensor_active is False

    async def test_a_second_obstruction_clears_it(self, command_handler):
        state = command_handler.simulator.state
        await command_handler.execute("obstruction")

        result = await command_handler.execute("obstruction")
        assert result.success is True
        assert result.message == "Obstruction cleared"
        assert state.obstruction_active is False

    async def test_zero_stays_until_cleared(self, command_handler):
        """0 is the boundary: still there afterwards, unlike a one-shot."""
        state = command_handler.simulator.state

        result = await command_handler.execute("obstruction 0")
        assert result.message == "Obstruction placed (until cleared)"
        assert state.obstruction_oneshot is False

    async def test_a_positive_duration_reports_its_window(self, command_handler):
        """The other side of the 0 boundary."""
        state = command_handler.simulator.state

        result = await command_handler.execute("obstruction 5")
        assert result.message == "Obstruction placed for 5.0s"
        assert state.obstruction_active is True

    async def test_obstruction_alias_x(self, command_handler):
        result = await command_handler.execute("x")
        assert result.success is True
        assert result.message == "Obstruction placed (cleared by the retract it causes)"


# ============================================================================
# Door commands (inside / outside / close / hold / cycle / toggle)
# ============================================================================


class TestToggleCommand:
    """`toggle` mirrors PowerPetDoor.toggle, including its mid-travel no-op."""

    async def test_toggle_opens_a_closed_door(self, command_handler):
        state = command_handler.simulator.state
        assert state.door_status == DOOR_STATE_CLOSED

        result = await command_handler.execute("toggle")
        assert result.success is True
        assert result.message == "Toggle: opening and holding"
        await command_handler.simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

    async def test_toggle_closes_an_open_door(self, command_handler):
        simulator = command_handler.simulator
        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        result = await command_handler.execute("toggle")
        assert result.success is True
        assert result.message == "Toggle: closing"
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    @pytest.mark.parametrize(
        "status",
        [
            DOOR_STATE_RISING,
            DOOR_STATE_SLOWING,
            DOOR_STATE_CLOSING,
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
        ],
    )
    async def test_toggle_is_a_noop_mid_travel(self, command_handler, status):
        """Nothing but an obstruction interrupts a door in motion."""
        state = command_handler.simulator.state
        state.door_status = status

        result = await command_handler.execute("toggle")
        assert result.success is True
        assert result.message == f"Toggle: ignored, door is in motion ({status})"
        assert state.door_status == status

    async def test_toggle_alias_tg(self, command_handler):
        result = await command_handler.execute("tg")
        assert result.success is True
        assert result.message == "Toggle: opening and holding"


class TestInsideCommand:
    async def test_default_duration(self, command_handler):
        state = command_handler.simulator.state
        state.power = False  # No door motion; test sensor state only

        result = await command_handler.execute("inside")
        assert result.success is True
        assert result.message == "Inside sensor activated for 0.5s"
        assert state.inside_sensor_active is True

    async def test_explicit_duration(self, command_handler):
        command_handler.simulator.state.power = False
        result = await command_handler.execute("inside 2")
        assert result.message == "Inside sensor activated for 2.0s"

    async def test_zero_duration_toggles(self, command_handler):
        state = command_handler.simulator.state
        state.power = False
        state.inside_sensor_active = False

        result = await command_handler.execute("inside 0")
        assert result.message == "Inside sensor active"
        assert state.inside_sensor_active is True

        result = await command_handler.execute("inside 0")
        assert result.message == "Inside sensor clear"
        assert state.inside_sensor_active is False

    async def test_on_off_and_toggle_say_which_they_mean(self, command_handler):
        """`0` toggles, which a script read out of order cannot rely on."""
        state = command_handler.simulator.state
        state.power = False

        assert (await command_handler.execute("inside on")).message == "Inside sensor active"
        assert state.inside_sensor_active is True
        assert (await command_handler.execute("inside on")).message == "Inside sensor active"
        assert state.inside_sensor_active is True

        assert (await command_handler.execute("inside off")).message == "Inside sensor clear"
        assert state.inside_sensor_active is False
        assert (await command_handler.execute("inside toggle")).message == "Inside sensor active"

    async def test_a_misspelled_argument_is_refused(self, command_handler):
        """Failing closed to "off" would silently clear the sensor."""
        state = command_handler.simulator.state
        state.power = False
        await command_handler.execute("inside on")

        result = await command_handler.execute("inside nonsense")

        assert result.success is False
        assert "expected on, off, toggle" in result.message
        assert state.inside_sensor_active is True

    async def test_negative_duration_rejected(self, command_handler):
        result = await command_handler.execute("inside -1")
        assert result.success is False
        assert "between 0 and" in result.message

    async def test_alias_i(self, command_handler):
        command_handler.simulator.state.power = False
        result = await command_handler.execute("i")
        assert result.message == "Inside sensor activated for 0.5s"


class TestOutsideCommand:
    async def test_default_duration(self, command_handler):
        state = command_handler.simulator.state
        state.power = False

        result = await command_handler.execute("outside")
        assert result.success is True
        assert result.message == "Outside sensor activated for 0.5s"
        assert state.outside_sensor_active is True

    async def test_zero_duration_toggles(self, command_handler):
        state = command_handler.simulator.state
        state.power = False
        state.outside_sensor_active = False

        result = await command_handler.execute("outside 0")
        assert result.message == "Outside sensor active"
        assert state.outside_sensor_active is True

        result = await command_handler.execute("o 0")
        assert result.message == "Outside sensor clear"

    async def test_on_off_and_toggle_say_which_they_mean(self, command_handler):
        """`0` toggles, which a script read out of order cannot rely on."""
        state = command_handler.simulator.state
        state.power = False

        assert (await command_handler.execute("outside on")).message == "Outside sensor active"
        assert state.outside_sensor_active is True
        assert (await command_handler.execute("outside on")).message == "Outside sensor active"
        assert state.outside_sensor_active is True

        assert (await command_handler.execute("outside off")).message == "Outside sensor clear"
        assert state.outside_sensor_active is False
        assert (await command_handler.execute("outside toggle")).message == "Outside sensor active"

    async def test_a_misspelled_argument_is_refused(self, command_handler):
        """Failing closed to "off" would silently clear the sensor."""
        state = command_handler.simulator.state
        state.power = False
        await command_handler.execute("outside on")

        result = await command_handler.execute("outside nonsense")

        assert result.success is False
        assert "expected on, off, toggle" in result.message
        assert state.outside_sensor_active is True

    async def test_sensors_mutually_exclusive(self, command_handler):
        state = command_handler.simulator.state
        state.power = False
        state.inside_sensor_active = True

        await command_handler.execute("outside 0")
        assert state.outside_sensor_active is True
        assert state.inside_sensor_active is False


class TestOpenAndCloseCommands:
    async def test_open_opens_and_keeps_up(self, command_handler):
        sim = command_handler.simulator
        result = await command_handler.execute("open")
        assert result.success is True
        assert result.message == "Opening and holding"
        status = await sim.wait_for_status(DOOR_STATE_KEEPUP, timeout=5)
        assert status == DOOR_STATE_KEEPUP

    @pytest.mark.parametrize("spelling", ["hold", "h"])
    async def test_the_old_spellings_still_reach_open(self, command_handler, spelling):
        """`hold` was the command's name before `open` was; both are aliases now."""
        sim = command_handler.simulator
        result = await command_handler.execute(spelling)
        assert result.message == "Opening and holding"
        await sim.wait_for_status(DOOR_STATE_KEEPUP, timeout=5)

    async def test_close_after_hold(self, command_handler):
        sim = command_handler.simulator
        await command_handler.execute("open")
        await sim.wait_for_status(DOOR_STATE_KEEPUP, timeout=5)

        result = await command_handler.execute("close")
        assert result.success is True
        assert result.message == "Closing door"
        status = await sim.wait_for_status(DOOR_STATE_CLOSED, timeout=5)
        assert status == DOOR_STATE_CLOSED

    async def test_cycle_runs_full_sequence(self, command_handler):
        sim = command_handler.simulator
        seen = []
        unsubscribe = sim.add_status_listener(seen.append)

        result = await command_handler.execute("cycle")
        assert result.success is True
        assert result.message == "Starting door cycle"

        await sim.wait_for_status(DOOR_STATE_HOLDING, timeout=5)
        await sim.wait_for_status(DOOR_STATE_CLOSED, timeout=5)
        unsubscribe()
        assert DOOR_STATE_RISING in seen
        assert DOOR_STATE_HOLDING in seen
        assert seen[-1] == DOOR_STATE_CLOSED


# ============================================================================
# Control commands (shutdown / debug)
# ============================================================================


class TestShutdownCommand:
    async def test_shutdown_invokes_stop_callback(self, command_handler):
        result = await command_handler.execute("shutdown")
        assert result.success is True
        assert result.message == "Shutting down..."
        command_handler.stop_callback.assert_called_once_with()

    async def test_stop_is_not_an_alias_for_shutdown(self, command_handler):
        """`stop` stops the running script, never the whole simulator."""
        result = await command_handler.execute("stop")
        assert result.success is False
        assert result.message == "No script is running (use 'shutdown' to stop the simulator)"
        command_handler.stop_callback.assert_not_called()


class TestDebugCommand:
    async def test_show_when_off(self, command_handler, root_logger_level):
        root_logger_level.setLevel(logging.INFO)
        result = await command_handler.execute("debug")
        assert result.success is True
        assert result.message == "Debug logging: off"

    async def test_show_when_on(self, command_handler, root_logger_level):
        root_logger_level.setLevel(logging.DEBUG)
        result = await command_handler.execute("debug")
        assert result.message == "Debug logging: on"

    async def test_enable(self, command_handler, root_logger_level):
        root_logger_level.setLevel(logging.INFO)
        result = await command_handler.execute("debug on")
        assert result.message == "Debug logging enabled"
        assert root_logger_level.level == logging.DEBUG

    async def test_disable(self, command_handler, root_logger_level):
        root_logger_level.setLevel(logging.DEBUG)
        result = await command_handler.execute("debug off")
        assert result.message == "Debug logging disabled"
        assert root_logger_level.level == logging.INFO

    async def test_invalid_value(self, command_handler):
        result = await command_handler.execute("debug maybe")
        assert result.success is False
        assert result.message == "'maybe' is not valid. Use on/off\nUsage: debug [on|off]"


# ============================================================================
# Script list command
# ============================================================================


class TestListCommand:
    async def test_lists_all_builtin_scripts(self, command_handler):
        result = await command_handler.execute("list")
        assert result.success is True
        assert result.message.startswith("Built-in scripts:")

        scripts = list(list_builtin_scripts())
        assert scripts  # There are built-in scripts
        for name, desc in scripts:
            assert f"  {name}: {desc}" in result.message
        assert result.message.endswith("\nScript: none running")
        assert result.data == {
            "scripts": scripts,
            "running": None,
            "queued": 0,
            "pending": [],
            "stopping": False,
        }

    async def test_aliases(self, command_handler):
        for alias in ("/", "scripts"):
            result = await command_handler.execute(alias)
            assert result.success is True
            assert result.message.startswith("Built-in scripts:")


class TestTheSelectedLocaleReachesRealCommandOutput:
    """End-to-end proof that `t()` is wired up, not just importable.

    Every other i18n test drives `powerpetdoor.i18n` directly. This one goes
    through a real command handler, so it fails if the wrapping at the call
    site is wrong even when the module underneath is perfect - and it pins
    the property the whole design rests on: with no locale selected, output
    is byte-identical to what it was before any of this existed.
    """

    @pytest.fixture
    def german(self, tmp_path, monkeypatch):
        from powerpetdoor import i18n

        monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
        i18n.reset_for_testing()
        (tmp_path / "de_de.json").write_text(
            json.dumps(
                {
                    "_language": "Deutsch",
                    "simulator.commands.control.debug_logging_enabled": "Debug-Protokoll aktiviert",
                    "simulator.commands.control.debug_logging": "Debug-Protokoll: {arg0}",
                }
            ),
            encoding="utf-8",
        )
        yield i18n
        i18n.reset_for_testing()

    async def test_english_is_what_ships(self, command_handler, root_logger_level, german):
        """No locale selected: the exact English the tests above assert."""
        root_logger_level.setLevel(logging.INFO)
        result = await command_handler.execute("debug on")
        assert result.message == "Debug logging enabled"

    async def test_a_translated_key_renders_in_the_selected_locale(
        self, command_handler, root_logger_level, german
    ):
        german.set_locale("de_de")
        root_logger_level.setLevel(logging.INFO)

        result = await command_handler.execute("debug on")

        assert result.message == "Debug-Protokoll aktiviert"

    async def test_interpolation_survives_translation(
        self, command_handler, root_logger_level, german
    ):
        """The `{arg0}` the codemod generated must still receive its value."""
        german.set_locale("de_de")
        root_logger_level.setLevel(logging.DEBUG)

        result = await command_handler.execute("debug")

        assert result.message == "Debug-Protokoll: on"

    async def test_an_untranslated_key_falls_back_to_english(
        self, command_handler, root_logger_level, german
    ):
        """de_de.json has no entry for the 'disabled' message."""
        german.set_locale("de_de")
        root_logger_level.setLevel(logging.DEBUG)

        result = await command_handler.execute("debug off")

        assert result.message == "Debug logging disabled"
        assert (
            "de_de",
            "simulator.commands.control.debug_logging_disabled",
        ) in german.missing_keys()


class TestObstructionArgumentForms:
    """`obstruction` takes the argument `inside`/`outside` take."""

    async def test_bare_toggles(self, command_handler):
        state = command_handler.simulator.state

        assert (await command_handler.execute("obstruction")).success is True
        assert state.obstruction_active is True

        assert (await command_handler.execute("obstruction")).message == "Obstruction cleared"
        assert state.obstruction_active is False

    async def test_on_is_explicit_and_stays(self, command_handler):
        """`on` places one that survives the retract it causes; a bare
        `obstruction` places a one-shot instead."""
        state = command_handler.simulator.state

        result = await command_handler.execute("obstruction on")

        assert result.message == "Obstruction placed (until cleared)"
        assert state.obstruction_active is True
        assert state.obstruction_oneshot is False

    async def test_off_clears_it(self, command_handler):
        state = command_handler.simulator.state
        await command_handler.execute("obstruction on")

        result = await command_handler.execute("obstruction off")

        assert result.message == "Obstruction cleared"
        assert state.obstruction_active is False

    async def test_toggle_says_which_way_it_went(self, command_handler):
        state = command_handler.simulator.state

        assert (await command_handler.execute("obstruction toggle")).success is True
        assert state.obstruction_active is True
        assert (await command_handler.execute("obstruction toggle")).message == (
            "Obstruction cleared"
        )

    async def test_a_duration_reports_its_window(self, command_handler):
        result = await command_handler.execute("obstruction 5")

        assert result.message == "Obstruction placed for 5.0s"

    async def test_zero_is_until_cleared(self, command_handler):
        """The boundary against a positive duration."""
        result = await command_handler.execute("obstruction 0")

        assert result.message == "Obstruction placed (until cleared)"
        assert command_handler.simulator.state.obstruction_oneshot is False

    async def test_a_negative_duration_is_refused(self, command_handler):
        result = await command_handler.execute("obstruction -1")

        assert result.success is False
        assert "between 0 and" in result.message

    async def test_a_misspelled_argument_is_refused(self, command_handler):
        """Failing closed to "off" would silently clear the doorway."""
        state = command_handler.simulator.state
        await command_handler.execute("obstruction on")

        result = await command_handler.execute("obstruction nonsense")

        assert result.success is False
        assert "expected on, off, toggle" in result.message
        assert state.obstruction_active is True
