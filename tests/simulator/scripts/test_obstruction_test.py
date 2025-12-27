# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the obstruction_test built-in script."""
from __future__ import annotations

import pytest

from powerpetdoor.simulator.scripting import get_builtin_script, YAML_AVAILABLE


requires_yaml = pytest.mark.skipif(
    not YAML_AVAILABLE,
    reason="PyYAML not installed"
)


@requires_yaml
class TestObstructionTest:
    """Tests for the obstruction_test script."""

    def test_script_exists(self):
        """The obstruction_test script should exist and be loadable."""
        script = get_builtin_script("obstruction_test")
        assert "obstruction" in script.name.lower() or "retract" in script.name.lower()

    def test_script_has_obstruction_action(self):
        """Script should include obstruction action."""
        script = get_builtin_script("obstruction_test")
        actions = [s.action for s in script.steps]
        assert "obstruction" in actions

    def test_script_enables_autoretract(self):
        """Script should enable autoretract before testing."""
        script = get_builtin_script("obstruction_test")
        # First few steps should set up autoretract
        for step in script.steps[:3]:
            if step.action == "set" and step.params.get("name") == "autoretract":
                assert step.params.get("value") == "on"
                break

    @pytest.mark.asyncio
    async def test_script_runs_successfully(self, runner, simulator):
        """The obstruction test should complete without errors."""
        script = get_builtin_script("obstruction_test")
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_autoretract_counter_increases(self, runner, simulator):
        """Auto-retract counter should increase after obstruction."""
        # Ensure autoretract is enabled
        simulator.state.autoretract = True
        initial_count = simulator.state.total_auto_retracts

        script = get_builtin_script("obstruction_test")
        await runner.run(script, verbose=False)

        # The obstruction should have triggered an auto-retract
        assert simulator.state.total_auto_retracts > initial_count


@requires_yaml
class TestObstructionTestMessages:
    """Test messages generated during obstruction_test script execution."""

    @pytest.mark.asyncio
    async def test_generates_status_updates(self, runner, simulator, message_capture):
        """Obstruction test should generate status update messages."""
        script = get_builtin_script("obstruction_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        status_updates = message_capture.find_status_updates()
        assert len(status_updates) > 0, "Should receive status update messages"

    @pytest.mark.asyncio
    async def test_status_shows_door_opening(self, runner, simulator, message_capture):
        """Should see door open before obstruction is triggered."""
        from powerpetdoor.const import DOOR_STATE_RISING, DOOR_STATE_HOLDING

        script = get_builtin_script("obstruction_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        statuses = message_capture.get_status_sequence()
        has_open = any(
            s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING)
            for s in statuses
        )
        assert has_open, f"Should see door open states: {statuses}"

    @pytest.mark.asyncio
    async def test_door_retracts_on_obstruction(self, runner, simulator, message_capture):
        """After obstruction, door should retract (show rising state again)."""
        from powerpetdoor.const import DOOR_STATE_RISING

        script = get_builtin_script("obstruction_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        statuses = message_capture.get_status_sequence()
        rising_count = sum(1 for s in statuses if s == DOOR_STATE_RISING)
        # Should see rising at least twice: initial open + retract after obstruction
        assert rising_count >= 2, (
            f"Expected at least 2 rising states (open + retract), got {rising_count}: {statuses}"
        )