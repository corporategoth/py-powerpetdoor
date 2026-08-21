# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the safety_lock_test built-in script."""

from __future__ import annotations

import pytest

from powerpetdoor.const import DOOR_STATE_CLOSED, DOOR_STATE_RISING
from powerpetdoor.simulator.scripting import YAML_AVAILABLE, get_builtin_script

from .conftest import FULL_CYCLE

requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")


@requires_yaml
class TestSafetyLockTest:
    """Tests for the safety_lock_test script."""

    def test_script_exists(self):
        """The safety_lock_test script should exist and be loadable."""
        script = get_builtin_script("safety_lock_test")
        assert script.name == "Outside Sensor Safety Lock Test"

    def test_script_tests_both_sensors(self):
        """Script should test both inside and outside sensors."""
        script = get_builtin_script("safety_lock_test")
        triggers = [
            s for s in script.steps if s.action == "trigger_sensor" or s.action == "trigger"
        ]
        sensors = [s.params.get("sensor", "") for s in triggers]

        assert "outside" in sensors
        assert "inside" in sensors

    async def test_script_passes_with_one_inside_cycle(self, runner, simulator):
        """Only the inside trigger cycles the door; safety lock is restored."""
        result = await runner.run(get_builtin_script("safety_lock_test"), verbose=False)

        assert result is True
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        assert simulator.state.total_open_cycles == 1
        assert simulator.state.safety_lock is False

    async def test_outside_blocked_inside_works(self, simulator):
        """Direct simulator check: safety lock blocks outside, not inside."""
        simulator.state.safety_lock = True

        # Outside sensor is ignored synchronously - the door does not move
        simulator.trigger_sensor("outside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        # Inside sensor starts the door rising synchronously
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_RISING


@requires_yaml
class TestSafetyLockTestMessages:
    """Broadcasts observed by a connected client during safety_lock_test."""

    async def test_broadcasts_exact_single_cycle(self, runner, simulator, message_capture):
        """The blocked outside trigger adds nothing; one inside cycle only."""
        result = await runner.run(get_builtin_script("safety_lock_test"), verbose=False)
        assert result is True

        sequence = await message_capture.wait_for_status_sequence(FULL_CYCLE)
        assert sequence == FULL_CYCLE
