# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the power_lockout_test built-in script."""

from __future__ import annotations

import pytest

from powerpetdoor.const import CMD_GET_DOOR_OPEN_STATS, DOOR_STATE_CLOSED
from powerpetdoor.simulator.scripting import YAML_AVAILABLE, get_builtin_script

requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")


@requires_yaml
class TestPowerLockoutTest:
    """Tests for the power_lockout_test script."""

    def test_script_exists(self):
        """The power_lockout_test script should exist and be loadable."""
        script = get_builtin_script("power_lockout_test")
        assert script.name == "Power and Lockout Test"

    def test_script_tests_both_conditions(self):
        """Script should test both power off and command lockout."""
        script = get_builtin_script("power_lockout_test")
        set_actions = [s for s in script.steps if s.action == "set"]
        names_set = [s.params.get("name", "") for s in set_actions]

        assert "power" in names_set
        assert "cmd_lockout" in names_set

    async def test_script_passes_without_door_motion(self, runner, simulator):
        """The script passes; the door never moved and settings are restored."""
        result = await runner.run(get_builtin_script("power_lockout_test"), verbose=False)

        assert result is True
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        assert simulator.state.total_open_cycles == 0
        # Script restores the settings it toggled
        assert simulator.state.power is True
        assert simulator.state.cmd_lockout is False


@requires_yaml
class TestPowerLockoutTestMessages:
    """Broadcasts observed by a connected client during power_lockout_test."""

    async def test_no_status_broadcasts(self, runner, simulator, message_capture):
        """Blocked triggers must not broadcast any door status change.

        A stats broadcast after the script acts as a sentinel: once it
        arrives, any (unexpected) earlier status broadcast would already
        have been captured.
        """
        result = await runner.run(get_builtin_script("power_lockout_test"), verbose=False)
        assert result is True

        simulator.broadcast_stats()
        await message_capture.wait_for(
            lambda msgs: any(m.get("CMD") == CMD_GET_DOOR_OPEN_STATS for m in msgs)
        )

        assert message_capture.get_status_sequence() == []
