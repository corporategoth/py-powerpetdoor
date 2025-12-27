# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the safety_lock_test built-in script."""
from __future__ import annotations

import pytest

from powerpetdoor.simulator.scripting import get_builtin_script, YAML_AVAILABLE


requires_yaml = pytest.mark.skipif(
    not YAML_AVAILABLE,
    reason="PyYAML not installed"
)


@requires_yaml
class TestSafetyLockTest:
    """Tests for the safety_lock_test script."""

    def test_script_exists(self):
        """The safety_lock_test script should exist and be loadable."""
        script = get_builtin_script("safety_lock_test")
        assert "safety" in script.name.lower() or "lock" in script.name.lower()

    def test_script_tests_both_sensors(self):
        """Script should test both inside and outside sensors."""
        script = get_builtin_script("safety_lock_test")

        # Find trigger_sensor actions
        triggers = [
            s for s in script.steps
            if s.action == "trigger_sensor" or s.action == "trigger"
        ]
        sensors = [s.params.get("sensor", "") for s in triggers]

        # Should test both sensors
        assert "outside" in sensors
        assert "inside" in sensors

    @pytest.mark.asyncio
    async def test_script_runs_successfully(self, runner, simulator):
        """The safety lock test should complete without errors."""
        script = get_builtin_script("safety_lock_test")
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_safety_lock_disabled_after_test(self, runner, simulator):
        """Safety lock should be disabled after script completes."""
        script = get_builtin_script("safety_lock_test")
        await runner.run(script, verbose=False)
        assert simulator.state.safety_lock is False

    @pytest.mark.asyncio
    async def test_outside_blocked_inside_works(self, runner, simulator):
        """Outside sensor should be blocked but inside should work."""
        # Enable safety lock
        simulator.state.safety_lock = True

        # Outside sensor should NOT open door
        simulator.trigger_sensor("outside")
        import asyncio
        await asyncio.sleep(0.3)
        from powerpetdoor.const import DOOR_STATE_CLOSED
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        # Inside sensor SHOULD open door
        simulator.trigger_sensor("inside")
        await asyncio.sleep(0.3)
        assert simulator.state.door_status != DOOR_STATE_CLOSED

        # Cleanup
        simulator.state.safety_lock = False


@requires_yaml
class TestSafetyLockTestMessages:
    """Test messages generated during safety_lock_test script execution."""

    @pytest.mark.asyncio
    async def test_generates_status_updates(self, runner, simulator, message_capture):
        """Safety lock test should generate status update messages."""
        script = get_builtin_script("safety_lock_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        status_updates = message_capture.find_status_updates()
        assert len(status_updates) > 0, "Should receive status update messages"

    @pytest.mark.asyncio
    async def test_inside_sensor_opens_door(self, runner, simulator, message_capture):
        """Inside sensor should open door even with safety lock enabled."""
        from powerpetdoor.const import DOOR_STATE_RISING, DOOR_STATE_HOLDING

        script = get_builtin_script("safety_lock_test")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        statuses = message_capture.get_status_sequence()
        # Inside sensor should trigger door opening
        has_open = any(
            s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING)
            for s in statuses
        )
        assert has_open, f"Inside sensor should open door: {statuses}"