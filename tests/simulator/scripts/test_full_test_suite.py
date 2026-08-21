# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the full_test_suite built-in script."""

from __future__ import annotations

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
)
from powerpetdoor.simulator.scripting import YAML_AVAILABLE, get_builtin_script

from .conftest import FULL_CYCLE

requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")

# Test 5 opens with hold: the door parks in KEEPUP, then closes on command.
KEEPUP_CYCLE = [
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSED,
]


@requires_yaml
class TestFullTestSuite:
    """Tests for the full_test_suite script."""

    def test_script_exists(self):
        """The full_test_suite script should exist and be loadable."""
        script = get_builtin_script("full_test_suite")
        assert script.name == "Full Test Suite"

    def test_script_has_expected_sections(self):
        """Script should have all expected test sections."""
        script = get_builtin_script("full_test_suite")
        log_messages = [s.params.get("message", "") for s in script.steps if s.action == "log"]
        log_text = " ".join(log_messages)

        assert "Basic Door Cycle" in log_text
        assert "Outside Sensor" in log_text
        assert "Power Off" in log_text
        assert "Safety Lock" in log_text
        assert "Open and Hold" in log_text

    async def test_script_passes_and_restores_state(self, runner, simulator):
        """The suite passes, restores settings, and counts three cycles."""
        result = await runner.run(get_builtin_script("full_test_suite"), verbose=False)

        assert result is True
        # Script restores the settings it toggled
        assert simulator.state.power is True
        assert simulator.state.safety_lock is False
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        # Tests 1, 2 and 5 each complete a full close (3 counted cycles);
        # tests 3 and 4 must not move the door.
        assert simulator.state.total_open_cycles == 3


@requires_yaml
class TestFullTestSuiteMessages:
    """Broadcasts observed by a connected client during full_test_suite."""

    async def test_broadcasts_exact_three_cycles(self, runner, simulator, message_capture):
        """The client sees two sensor cycles, then the keepup cycle - nothing else."""
        result = await runner.run(get_builtin_script("full_test_suite"), verbose=False)
        assert result is True

        expected = FULL_CYCLE + FULL_CYCLE + KEEPUP_CYCLE
        sequence = await message_capture.wait_for_status_sequence(expected)
        assert sequence == expected
