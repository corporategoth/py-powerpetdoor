# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the obstruction_test built-in script."""

from __future__ import annotations

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
)
from powerpetdoor.simulator.scripting import YAML_AVAILABLE, get_builtin_script

requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")

# Open, close attempt, obstruction retract, then the final undisturbed close.
#
# The retract happens from DOOR_CLOSING - the motor has started but the flap
# has not moved - so the door goes straight back to HOLDING without
# travelling. It does NOT pass through CLOSING_TOP_OPEN and back up through
# SLOWING, which is what this expected before DOOR_CLOSING was known about:
# the obstruction is present when the close begins, so it is caught at the
# first opportunity rather than one phase later.
RETRACT_SEQUENCE = [
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_CLOSING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_CLOSING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSED,
]


@requires_yaml
class TestObstructionTest:
    """Tests for the obstruction_test script."""

    def test_script_exists(self):
        """The obstruction_test script should exist and be loadable."""
        script = get_builtin_script("obstruction_test")
        assert script.name == "Obstruction Auto-Retract Test"

    def test_script_has_obstruction_action(self):
        """Script should include obstruction action."""
        script = get_builtin_script("obstruction_test")
        actions = [s.action for s in script.steps]
        assert "obstruction" in actions

    def test_script_enables_autoretract(self):
        """Script should enable autoretract before testing."""
        script = get_builtin_script("obstruction_test")
        assert script.steps[0].action == "set"
        assert script.steps[0].params == {"name": "autoretract", "value": "on"}

    def test_script_obstructs_during_close(self):
        """The obstruction fires only after the door has started closing."""
        script = get_builtin_script("obstruction_test")
        actions = [s.action for s in script.steps]
        obstruction_at = actions.index("obstruction")
        closing_wait = script.steps[obstruction_at - 1]
        assert closing_wait.action == "wait_for"
        assert closing_wait.params["condition"] == "door_closing"

    async def test_script_passes_with_one_retract(self, runner, simulator):
        """The script passes, having auto-retracted exactly once."""
        result = await runner.run(get_builtin_script("obstruction_test"), verbose=False)

        assert result is True
        assert simulator.state.total_auto_retracts == 1
        # The script ends as soon as the retract re-opens the door
        assert simulator.state.door_status == DOOR_STATE_HOLDING
        # The retract cleared the simulated obstruction
        assert simulator.state.inside_sensor_active is False


@requires_yaml
class TestObstructionTestMessages:
    """Broadcasts observed by a connected client during obstruction_test."""

    async def test_broadcasts_exact_retract_sequence(self, runner, simulator, message_capture):
        """The client sees the close attempt reverse, then the final close."""
        result = await runner.run(get_builtin_script("obstruction_test"), verbose=False)
        assert result is True

        # Let the re-opened door finish its final close deterministically
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=10.0)

        sequence = await message_capture.wait_for_status_sequence(RETRACT_SEQUENCE)
        assert sequence == RETRACT_SEQUENCE
