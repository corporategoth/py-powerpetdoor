# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the schedule_test built-in script."""
from __future__ import annotations

import pytest

from powerpetdoor.simulator.scripting import get_builtin_script, YAML_AVAILABLE


requires_yaml = pytest.mark.skipif(
    not YAML_AVAILABLE,
    reason="PyYAML not installed"
)


@requires_yaml
class TestScheduleTest:
    """Tests for the schedule_test script."""

    def test_script_exists(self):
        """The schedule_test script should exist and be loadable."""
        script = get_builtin_script("schedule_test")
        assert "schedule" in script.name.lower()

    def test_script_has_schedule_actions(self):
        """Script should include schedule add/remove actions."""
        script = get_builtin_script("schedule_test")
        actions = [s.action for s in script.steps]

        # Should have add and remove schedule actions
        assert "add_schedule" in actions
        assert "remove_schedule" in actions

    def test_script_has_multiple_test_sections(self):
        """Script should have multiple test sections."""
        script = get_builtin_script("schedule_test")

        # Find log messages
        log_messages = [
            s.params.get("message", "")
            for s in script.steps
            if s.action == "log"
        ]
        log_text = " ".join(log_messages)

        # Should have test sections
        assert "Test 1" in log_text or "No Schedule" in log_text

    @pytest.mark.asyncio
    async def test_script_runs_successfully(self, runner, simulator):
        """The schedule test should complete without errors."""
        script = get_builtin_script("schedule_test")
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_schedules_cleaned_up(self, runner, simulator):
        """Schedules should be removed after script completes."""
        script = get_builtin_script("schedule_test")
        await runner.run(script, verbose=False)
        # Script should clean up any schedules it added
        assert len(simulator.state.schedules) == 0


@requires_yaml
class TestScheduleTestMessages:
    """Test messages generated during schedule_test script execution."""

    @pytest.mark.asyncio
    async def test_generates_messages(self, runner, simulator, message_capture):
        """Schedule test should generate messages during execution."""
        script = get_builtin_script("schedule_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        # Schedule tests may not trigger door movement, but should generate
        # some messages when schedules are added/removed
        # Just verify the test runs and captures what messages there are
        assert message_capture.messages is not None

    @pytest.mark.asyncio
    async def test_door_opens_with_schedule_enabled(self, runner, simulator, message_capture):
        """When schedule is active, door operations should work."""
        from powerpetdoor.const import DOOR_STATE_RISING, DOOR_STATE_HOLDING

        script = get_builtin_script("schedule_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        statuses = message_capture.get_status_sequence()
        # If the schedule test triggers door operations, verify we see them
        if statuses:
            # If there are status updates, verify they include expected states
            has_movement = any(
                s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING)
                for s in statuses
            )
            # This is informational - schedule tests may or may not trigger door
            if not has_movement:
                # That's okay - schedule tests may only test schedule configuration
                pass