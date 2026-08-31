# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the pet_presence_test built-in script."""

from __future__ import annotations

import pytest

from powerpetdoor.const import DOOR_STATE_CLOSED
from powerpetdoor.simulator.scripting import YAML_AVAILABLE, get_builtin_script

from .conftest import FULL_CYCLE

requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")


@requires_yaml
class TestPetPresenceTest:
    """Tests for the pet_presence_test script."""

    def test_script_exists(self):
        """The pet_presence_test script should exist and be loadable."""
        script = get_builtin_script("pet_presence_test")
        assert script.name == "Pet Presence Hold Extension"

    def test_script_has_pet_actions(self):
        """Script should include pet_presence and pet_off actions."""
        script = get_builtin_script("pet_presence_test")
        actions = [s.action for s in script.steps]
        # `pet_presence` is gone: a sensor held active *is* pet
        # presence, so the script says `inside` with a duration of 0.
        assert "inside" in actions
        assert "trigger" in actions

    def test_script_waits_past_hold_time_with_pet_present(self):
        """The wall-clock wait genuinely exceeds the configured hold time.

        This is the one intentional `wait` in the built-in scripts: proving
        the pet keeps the door open requires letting more than hold_time
        pass. The margin must stay comfortably above the hold time.
        """
        script = get_builtin_script("pet_presence_test")
        set_steps = {
            s.params.get("name"): s.params.get("value") for s in script.steps if s.action == "set"
        }
        hold_time = float(set_steps["hold_time"])
        waits = [float(s.params["seconds"]) for s in script.steps if s.action == "wait"]
        assert len(waits) == 1
        assert waits[0] > hold_time

    async def test_script_passes_with_closed_end_state(self, runner, simulator):
        """The script passes: pet held the door open, then it closed."""
        result = await runner.run(get_builtin_script("pet_presence_test"), verbose=False)

        assert result is True
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        assert simulator.state.inside_sensor_active is False
        assert simulator.state.total_open_cycles == 1


@requires_yaml
class TestPetPresenceTestMessages:
    """Broadcasts observed by a connected client during pet_presence_test."""

    async def test_broadcasts_exact_single_cycle(self, runner, simulator, message_capture):
        """One cycle only - the pet extends HOLDING without extra transitions."""
        result = await runner.run(get_builtin_script("pet_presence_test"), verbose=False)
        assert result is True

        sequence = await message_capture.wait_for_status_sequence(FULL_CYCLE)
        assert sequence == FULL_CYCLE
