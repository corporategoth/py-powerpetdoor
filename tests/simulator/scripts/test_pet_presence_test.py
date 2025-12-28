# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the pet_presence_test built-in script."""
from __future__ import annotations

import pytest

from powerpetdoor.simulator.scripting import get_builtin_script, YAML_AVAILABLE


requires_yaml = pytest.mark.skipif(
    not YAML_AVAILABLE,
    reason="PyYAML not installed"
)


@requires_yaml
class TestPetPresenceTest:
    """Tests for the pet_presence_test script."""

    def test_script_exists(self):
        """The pet_presence_test script should exist and be loadable."""
        script = get_builtin_script("pet_presence_test")
        assert "pet" in script.name.lower() or "presence" in script.name.lower()

    def test_script_has_pet_actions(self):
        """Script should include pet_presence and pet_off actions."""
        script = get_builtin_script("pet_presence_test")
        actions = [s.action for s in script.steps]
        assert "pet_presence" in actions or "pet_on" in actions
        assert "pet_off" in actions

    @pytest.mark.asyncio
    async def test_script_runs_successfully(self, runner, simulator):
        """The pet presence test should complete without errors."""
        script = get_builtin_script("pet_presence_test")
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_pet_flag_cleared_after_test(self, runner, simulator):
        """Pet sensor should be cleared after script completes."""
        script = get_builtin_script("pet_presence_test")
        await runner.run(script, verbose=False)
        assert simulator.state.inside_sensor_active is False


@requires_yaml
class TestPetPresenceTestMessages:
    """Test messages generated during pet_presence_test script execution."""

    @pytest.mark.asyncio
    async def test_generates_status_updates(self, runner, simulator, message_capture):
        """Pet presence test should generate status update messages."""
        script = get_builtin_script("pet_presence_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        status_updates = message_capture.find_status_updates()
        assert len(status_updates) > 0, "Should receive status update messages"

    @pytest.mark.asyncio
    async def test_door_opens_during_pet_presence(self, runner, simulator, message_capture):
        """Door should open when pet presence is simulated."""
        from powerpetdoor.const import DOOR_STATE_RISING, DOOR_STATE_HOLDING

        script = get_builtin_script("pet_presence_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        statuses = message_capture.get_status_sequence()
        has_open = any(
            s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING)
            for s in statuses
        )
        assert has_open, f"Should see door open during pet presence: {statuses}"

    @pytest.mark.asyncio
    async def test_door_stays_open_with_pet(self, runner, simulator, message_capture):
        """Door should hold open while pet is in doorway."""
        from powerpetdoor.const import DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP

        script = get_builtin_script("pet_presence_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        statuses = message_capture.get_status_sequence()
        # Should see holding or keepup states while pet is present
        has_hold = any(
            s in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP)
            for s in statuses
        )
        assert has_hold, f"Should see door holding open for pet: {statuses}"