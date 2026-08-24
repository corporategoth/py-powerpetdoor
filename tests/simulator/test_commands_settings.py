# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for settings management commands (commands/settings.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import powerpetdoor.simulator.commands.settings as settings_mod
import powerpetdoor.tz_utils as tz_utils
from powerpetdoor.const import (
    CMD_DISABLE_AUTORETRACT,
    CMD_DISABLE_CMD_LOCKOUT,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_ENABLE_AUTORETRACT,
    CMD_ENABLE_CMD_LOCKOUT,
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_SET_HOLD_TIME,
    CMD_SET_TIMEZONE,
    FIELD_CMD,
    FIELD_HOLD_TIME,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.commands.settings import _timezone_completer
from powerpetdoor.simulator.scripting import ScriptRunner

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


STUB_TIMEZONES = ["America/New_York", "Europe/London", "UTC"]
STUB_POSIX = {"America/New_York": "EST5EDT,M3.2.0,M11.1.0"}


@pytest.fixture
def tz_cache_ready(monkeypatch):
    """Pretend the timezone cache is initialized with a small fixed set."""
    monkeypatch.setattr(tz_utils, "is_cache_initialized", lambda: True)
    monkeypatch.setattr(tz_utils, "get_available_timezones", lambda: list(STUB_TIMEZONES))
    monkeypatch.setattr(tz_utils, "get_posix_tz_string", lambda tz: STUB_POSIX.get(tz))


@pytest.fixture
def tz_cache_empty(monkeypatch):
    """Pretend the timezone cache has not been initialized."""
    monkeypatch.setattr(tz_utils, "is_cache_initialized", lambda: False)
    monkeypatch.setattr(tz_utils, "get_available_timezones", lambda: [])
    monkeypatch.setattr(tz_utils, "get_posix_tz_string", lambda tz: None)


def _sent_cmds(protocol):
    return [call.args[0][FIELD_CMD] for call in protocol._send.call_args_list]


# ============================================================================
# safety / lockout / autoretract toggles
# ============================================================================


class TestSafetyCommand:
    async def test_bare_toggles(self, command_handler):
        state = command_handler.simulator.state
        state.safety_lock = False

        result = await command_handler.execute("safety")
        assert result.success is True
        assert result.message == "Safety lock: ON"
        assert state.safety_lock is True

        result = await command_handler.execute("safety")
        assert result.message == "Safety lock: OFF"
        assert state.safety_lock is False

    async def test_set_on_off(self, command_handler):
        state = command_handler.simulator.state

        result = await command_handler.execute("safety on")
        assert result.message == "Safety lock: ON"
        assert state.safety_lock is True

        result = await command_handler.execute("safety off")
        assert result.message == "Safety lock: OFF"
        assert state.safety_lock is False

    async def test_toggle_subcommand_and_aliases(self, command_handler):
        state = command_handler.simulator.state
        state.safety_lock = False

        result = await command_handler.execute("safety toggle")
        assert result.message == "Safety lock: ON"
        assert state.safety_lock is True

        result = await command_handler.execute("s t")
        assert result.message == "Safety lock: OFF"
        assert state.safety_lock is False

    async def test_broadcasts_setting_change(self, command_handler, mock_client):
        command_handler.simulator.state.safety_lock = False
        await command_handler.execute("safety on")
        assert CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK in _sent_cmds(mock_client)

        mock_client._send.reset_mock()
        await command_handler.execute("safety off")
        assert CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK in _sent_cmds(mock_client)

    async def test_invalid_value(self, command_handler):
        result = await command_handler.execute("safety maybe")
        assert result.success is False
        assert result.message == "'maybe' is not valid. Use on/off\nUsage: safety [on|off]"


class TestLockoutCommand:
    async def test_set_and_toggle(self, command_handler, mock_client):
        state = command_handler.simulator.state
        state.cmd_lockout = False

        result = await command_handler.execute("lockout on")
        assert result.message == "Command lockout: ON"
        assert state.cmd_lockout is True
        assert CMD_ENABLE_CMD_LOCKOUT in _sent_cmds(mock_client)

        mock_client._send.reset_mock()
        result = await command_handler.execute("lockout off")
        assert result.message == "Command lockout: OFF"
        assert state.cmd_lockout is False
        assert CMD_DISABLE_CMD_LOCKOUT in _sent_cmds(mock_client)

        result = await command_handler.execute("lockout toggle")
        assert result.message == "Command lockout: ON"
        assert state.cmd_lockout is True

    async def test_bare_toggle_and_alias(self, command_handler):
        state = command_handler.simulator.state
        state.cmd_lockout = False

        result = await command_handler.execute("l")
        assert result.message == "Command lockout: ON"
        assert state.cmd_lockout is True

        result = await command_handler.execute("lockout t")
        assert result.message == "Command lockout: OFF"
        assert state.cmd_lockout is False


class TestAutoretractCommand:
    async def test_set_and_toggle(self, command_handler, mock_client):
        state = command_handler.simulator.state
        state.autoretract = True

        result = await command_handler.execute("autoretract off")
        assert result.message == "Auto-retract: OFF"
        assert state.autoretract is False
        assert CMD_DISABLE_AUTORETRACT in _sent_cmds(mock_client)

        mock_client._send.reset_mock()
        result = await command_handler.execute("autoretract on")
        assert result.message == "Auto-retract: ON"
        assert state.autoretract is True
        assert CMD_ENABLE_AUTORETRACT in _sent_cmds(mock_client)

    async def test_bare_and_toggle_subcommand(self, command_handler):
        state = command_handler.simulator.state
        state.autoretract = True

        result = await command_handler.execute("a")
        assert result.message == "Auto-retract: OFF"
        assert state.autoretract is False

        result = await command_handler.execute("autoretract toggle")
        assert result.message == "Auto-retract: ON"
        assert state.autoretract is True


# ============================================================================
# holdtime
# ============================================================================


class TestHoldtimeCommand:
    async def test_set_broadcasts_centiseconds(self, command_handler, mock_client):
        result = await command_handler.execute("holdtime 5")
        assert result.success is True
        assert result.message == "Hold time set to 5.0s"
        assert command_handler.simulator.state.hold_time == 5.0

        payload = mock_client._send.call_args.args[0]
        assert payload[FIELD_CMD] == CMD_SET_HOLD_TIME
        assert payload[FIELD_HOLD_TIME] == 500

    async def test_below_minimum(self, command_handler):
        result = await command_handler.execute("holdtime 0.05")
        assert result.success is False
        assert result.message == "'0.05' is below minimum (0.1)\nUsage: holdtime [seconds]"

    async def test_zero_rejected(self, command_handler):
        result = await command_handler.execute("holdtime 0")
        assert result.success is False
        assert result.message == "'0' is below minimum (0.1)\nUsage: holdtime [seconds]"

    async def test_boundary_values_accepted(self, command_handler):
        """The documented 0.1-900 bounds, asserted *at* the edges."""
        assert (await command_handler.execute("holdtime 0.1")).success is True
        assert command_handler.simulator.state.hold_time == 0.1
        assert (await command_handler.execute("holdtime 900")).success is True
        assert command_handler.simulator.state.hold_time == 900.0

    @pytest.mark.parametrize("raw", ["nan", "inf", "Infinity", "1e400"])
    async def test_non_finite_is_refused_without_touching_state(self, command_handler, raw):
        """`holdtime nan` reported ERROR having already corrupted the state.

        Everything downstream then broke: `broadcast settings` and
        `broadcast all` failed forever after, and `GET_SETTINGS` /
        `GET_HOLD_TIME` answered every connected client
        `success:false / reason:"Command failed"` because
        `int(hold_time * 100)` raises.
        """
        before = command_handler.simulator.state.hold_time

        result = await command_handler.execute(f"holdtime {raw}")

        assert result.success is False
        assert result.message.startswith(f"'{raw}' must be a finite number")
        assert command_handler.simulator.state.hold_time == before
        # The value the wire path chokes on is still representable.
        assert int(command_handler.simulator.state.hold_time * 100) >= 0

    async def test_the_handler_itself_validates_before_writing(self, command_handler):
        """Second layer, for a direct programmatic caller of the mixin.

        `parse_arg` refuses non-finite input on the operator path, but the
        rule this pins is the ordering one: a command that reports ERROR
        must not have already mutated the state it reports failing on.
        """
        before = command_handler.simulator.state.hold_time

        result = command_handler.holdtime(float("nan"))

        assert result.success is False
        assert result.message == "Hold time must be a finite number, got nan"
        assert command_handler.simulator.state.hold_time == before


# ============================================================================
# battery / battery_present / charge rates
# ============================================================================


class TestBatteryCommand:
    async def test_out_of_range_rejected(self, command_handler):
        result = await command_handler.execute("battery 150")
        assert result.success is False
        assert result.message == "'150' is above maximum (100)\nUsage: battery [percent]"

        result = await command_handler.execute("battery -1")
        assert result.success is False
        assert result.message == "'-1' is below minimum (0)\nUsage: battery [percent]"

    async def test_direct_method_call_clamps(self, command_handler):
        """Commands are also callable directly as methods; the handler clamps
        out-of-range values that bypass ArgSpec validation."""
        result = command_handler.battery(150)
        assert result.success is True
        assert result.message == "Battery set to 100%"
        assert command_handler.simulator.state.battery_percent == 100

        result = command_handler.battery(-5)
        assert result.message == "Battery set to 0%"
        assert command_handler.simulator.state.battery_percent == 0

    async def test_boundary_values(self, command_handler):
        result = await command_handler.execute("battery 0")
        assert result.message == "Battery set to 0%"
        result = await command_handler.execute("battery 100")
        assert result.message == "Battery set to 100%"


class TestBatteryPresentCommand:
    async def test_off_and_toggle_subcommand(self, command_handler):
        state = command_handler.simulator.state
        state.battery_present = True

        result = await command_handler.execute("battery_present off")
        assert result.success is True
        assert result.message == "Battery: removed"
        assert state.battery_present is False

        result = await command_handler.execute("battery_present toggle")
        assert result.message == "Battery: installed"
        assert state.battery_present is True

        result = await command_handler.execute("bp t")
        assert result.message == "Battery: removed"
        assert state.battery_present is False


class TestChargeRates:
    async def test_discharge_rate_zero_disables(self, command_handler):
        result = await command_handler.execute("discharge_rate 0")
        assert result.success is True
        assert result.message == "Discharging disabled"
        assert command_handler.simulator.state.battery_config.discharge_rate == 0.0

    async def test_discharge_rate_show_current(self, command_handler):
        command_handler.simulator.set_discharge_rate(0.25)
        result = await command_handler.execute("discharge_rate")
        assert result.success is True
        assert result.message == "Discharge rate: 0.25%/min"

    async def test_negative_rates_rejected(self, command_handler):
        result = await command_handler.execute("charge_rate -1")
        assert result.success is False
        assert result.message == "'-1' is below minimum (0)\nUsage: charge_rate [rate]"

        result = await command_handler.execute("discharge_rate -1")
        assert result.success is False
        assert result.message == "'-1' is below minimum (0)\nUsage: discharge_rate [rate]"


# ============================================================================
# ac
# ============================================================================


class TestAcCommand:
    async def test_toggle_subcommand_and_aliases(self, command_handler):
        state = command_handler.simulator.state
        state.ac_present = True

        result = await command_handler.execute("ac toggle")
        assert result.success is True
        assert result.message == "AC set to disconnected"
        assert state.ac_present is False

        result = await command_handler.execute("ac t")
        assert result.message == "AC set to connected"
        assert state.ac_present is True

        result = await command_handler.execute("ac d")
        assert result.message == "AC set to disconnected"
        assert state.ac_present is False

        result = await command_handler.execute("ac c")
        assert result.message == "AC set to connected"
        assert state.ac_present is True

    async def test_bare_ac_reads_as_a_change_not_a_display(self, command_handler):
        """`ac` mutates, so it must not be phrased like `battery`/`holdtime`."""
        state = command_handler.simulator.state
        state.ac_present = True

        result = await command_handler.execute("ac")
        assert result.success is True
        assert result.message == "AC set to disconnected"
        assert state.ac_present is False


# ============================================================================
# timezone
# ============================================================================


class TestTimezoneCommand:
    async def test_show_without_cache(self, command_handler, tz_cache_empty):
        result = await command_handler.execute("timezone")
        assert result.success is True
        assert result.message == "Timezone: America/New_York"

    async def test_show_with_posix(self, command_handler, tz_cache_ready):
        result = await command_handler.execute("timezone")
        assert result.success is True
        assert result.message == "Timezone: America/New_York (EST5EDT,M3.2.0,M11.1.0)"

    async def test_show_with_cache_but_no_posix_mapping(self, command_handler, tz_cache_ready):
        command_handler.simulator.state.timezone = "Europe/London"
        result = await command_handler.execute("timezone")
        assert result.message == "Timezone: Europe/London"

    async def test_set_iana_with_posix(self, command_handler, tz_cache_ready, mock_client):
        command_handler.simulator.state.timezone = "UTC"
        result = await command_handler.execute("timezone America/New_York")
        assert result.success is True
        assert result.message == "Timezone set to America/New_York (EST5EDT,M3.2.0,M11.1.0)"
        assert command_handler.simulator.state.timezone == "America/New_York"
        assert CMD_SET_TIMEZONE in _sent_cmds(mock_client)

    async def test_set_iana_without_posix(self, command_handler, tz_cache_ready):
        result = await command_handler.execute("timezone Europe/London")
        assert result.success is True
        assert result.message == "Timezone set to Europe/London"
        assert command_handler.simulator.state.timezone == "Europe/London"

    async def test_set_utc_special_case(self, command_handler, tz_cache_ready):
        result = await command_handler.execute("timezone UTC")
        assert result.success is True
        assert result.message == "Timezone set to UTC"
        assert command_handler.simulator.state.timezone == "UTC"

    async def test_unknown_iana_rejected(self, command_handler, tz_cache_ready):
        result = await command_handler.execute("timezone Bogus/Zone")
        assert result.success is False
        assert result.message == "Unknown timezone: Bogus/Zone"
        assert command_handler.simulator.state.timezone == "America/New_York"

    async def test_iana_accepted_when_cache_uninitialized(self, command_handler, tz_cache_empty):
        """Without a cache there is no validation list; IANA names are trusted."""
        result = await command_handler.execute("timezone Europe/London")
        assert result.success is True
        assert result.message == "Timezone set to Europe/London"

    async def test_set_posix_string(self, command_handler, tz_cache_empty, mock_client):
        result = await command_handler.execute("timezone EST5EDT,M3.2.0,M11.1.0")
        assert result.success is True
        assert result.message == "Timezone set to EST5EDT,M3.2.0,M11.1.0"
        assert command_handler.simulator.state.timezone == "EST5EDT,M3.2.0,M11.1.0"
        assert CMD_SET_TIMEZONE in _sent_cmds(mock_client)

    async def test_set_posix_angle_bracket_form(self, command_handler, tz_cache_empty):
        result = await command_handler.execute("timezone <+05>-5")
        assert result.success is True
        assert result.message == "Timezone set to <+05>-5"

    async def test_invalid_timezone_rejected(self, command_handler, tz_cache_empty):
        result = await command_handler.execute("timezone NotATimezone")
        assert result.success is False
        assert result.message == (
            "Invalid timezone: NotATimezone. Use IANA name (e.g., 'America/New_York') "
            "or POSIX string (e.g., 'EST5EDT,M3.2.0,M11.1.0')"
        )
        assert command_handler.simulator.state.timezone == "America/New_York"

    async def test_tz_alias(self, command_handler, tz_cache_empty):
        result = await command_handler.execute("tz")
        assert result.success is True
        assert result.message == "Timezone: America/New_York"


class TestTimezoneCompleter:
    def test_returns_name_description_pairs(self, monkeypatch):
        # The completer uses the module-level import in settings.py
        monkeypatch.setattr(settings_mod, "get_available_timezones", lambda: list(STUB_TIMEZONES))
        assert _timezone_completer() == [
            ("America/New_York", ""),
            ("Europe/London", ""),
            ("UTC", ""),
        ]

    def test_empty_when_cache_unavailable(self, monkeypatch):
        monkeypatch.setattr(settings_mod, "get_available_timezones", lambda: [])
        assert _timezone_completer() == []
