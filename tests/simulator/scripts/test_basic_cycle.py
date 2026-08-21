# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the basic_cycle built-in script."""

from __future__ import annotations

import pytest

from powerpetdoor.const import DOOR_STATE_CLOSED
from powerpetdoor.simulator.scripting import YAML_AVAILABLE, get_builtin_script

from .conftest import FULL_CYCLE

requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")


@requires_yaml
class TestBasicCycle:
    """Tests for the basic_cycle script."""

    def test_script_exists(self):
        """The basic_cycle script should exist and be loadable."""
        script = get_builtin_script("basic_cycle")
        assert script.name == "Basic Door Cycle"

    def test_script_has_expected_steps(self):
        """Script asserts closed, triggers the sensor, and waits on state."""
        script = get_builtin_script("basic_cycle")
        actions = [s.action for s in script.steps]
        # Starts by asserting the door is closed
        assert script.steps[0].action == "assert"
        assert script.steps[0].params == {"condition": "door_status", "equals": "DOOR_CLOSED"}
        assert "trigger_sensor" in actions
        # Deterministic: synchronizes on door state, not wall-clock waits
        assert "wait_for" in actions
        assert "wait" not in actions

    async def test_script_passes_with_closed_end_state(self, runner, simulator):
        """The script passes, completes one cycle, and leaves the door closed."""
        result = await runner.run(get_builtin_script("basic_cycle"), verbose=False)

        assert result is True
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        assert simulator.state.total_open_cycles == 1
        assert simulator.state.hold_time == 1.0  # set by the script


@requires_yaml
class TestBasicCycleMessages:
    """Broadcasts observed by a connected client during basic_cycle."""

    async def test_broadcasts_exact_full_cycle(self, runner, simulator, message_capture):
        """A connected client sees exactly one full open/close sequence."""
        result = await runner.run(get_builtin_script("basic_cycle"), verbose=False)
        assert result is True

        sequence = await message_capture.wait_for_status_sequence(FULL_CYCLE)
        assert sequence == FULL_CYCLE
