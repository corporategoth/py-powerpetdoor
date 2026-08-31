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

    def test_script_exercises_the_outside_sensor_both_ways(self):
        """The lock is the *outside* sensor's schedule override, so the
        script triggers that sensor with the lock off and then on."""
        script = get_builtin_script("safety_lock_test")
        triggers = [s for s in script.steps if s.action == "trigger"]
        sensors = [s.params.get("sensor", "") for s in triggers]

        assert sensors == ["outside", "outside"]

    def test_script_puts_a_closed_window_in_the_way(self):
        """Without a schedule there is nothing for the lock to override."""
        script = get_builtin_script("safety_lock_test")

        assert any(s.action == "add_schedule" for s in script.steps)

    async def test_script_passes_with_two_cycles(self, runner, simulator):
        """One cycle inside the window, one granted by the lock."""
        result = await runner.run(get_builtin_script("safety_lock_test"), verbose=False)

        assert result is True
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        assert simulator.state.total_open_cycles == 2
        assert simulator.state.safety_lock is False

    async def test_the_lock_grants_entry_past_a_closed_window(self, simulator):
        """Direct check, the polarity this script exists to pin.

        **Measured on the door** (see docs/protocol.md): the app
        calls this "always allow pet entry inside override timers", and
        `GET_SETTINGS` confirms the mapping is direct. It grants entry; it
        does not deny it. This asserted the opposite.
        """
        from powerpetdoor.simulator.state import Schedule

        simulator.state.schedules = {
            0: Schedule(
                index=0,
                enabled=True,
                days_of_week=[True] * 7,
                outside=True,
                start_hour=0,
                start_min=0,
                end_hour=0,
                end_min=1,
            )
        }

        simulator.trigger_sensor("outside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        simulator.state.safety_lock = True
        simulator.trigger_sensor("outside")
        assert simulator.state.door_status == DOOR_STATE_RISING


@requires_yaml
class TestSafetyLockTestMessages:
    """Broadcasts observed by a connected client during safety_lock_test."""

    async def test_broadcasts_exactly_two_cycles(self, runner, simulator, message_capture):
        """One cycle inside the window, one the lock grants past it.

        This expected a single cycle, on the reading that a locked door
        refuses the outside sensor entirely. Measured on hardware, the
        lock grants entry - so the locked trigger opens the door too.
        """
        result = await runner.run(get_builtin_script("safety_lock_test"), verbose=False)
        assert result is True

        expected = FULL_CYCLE + FULL_CYCLE
        sequence = await message_capture.wait_for_status_sequence(expected)
        assert sequence == expected
