# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the full_test_suite built-in script."""
from __future__ import annotations

import pytest

from powerpetdoor.simulator.scripting import get_builtin_script, YAML_AVAILABLE
from powerpetdoor.const import DOOR_STATE_CLOSED


requires_yaml = pytest.mark.skipif(
    not YAML_AVAILABLE,
    reason="PyYAML not installed"
)


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
        # Check for log messages that indicate test sections
        log_messages = [
            s.params.get("message", "")
            for s in script.steps
            if s.action == "log"
        ]
        log_text = " ".join(log_messages)

        # Should have all major test sections
        assert "Basic Door Cycle" in log_text
        assert "Outside Sensor" in log_text
        assert "Power Off" in log_text
        assert "Safety Lock" in log_text
        assert "Open and Hold" in log_text

    @pytest.mark.asyncio
    async def test_script_runs_successfully(self, runner, simulator):
        """The full test suite should complete without errors."""
        script = get_builtin_script("full_test_suite")
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_state_restored_after_tests(self, runner, simulator):
        """After running, power and safety_lock should be restored."""
        script = get_builtin_script("full_test_suite")
        await runner.run(script, verbose=False)
        # Script should restore these settings
        assert simulator.state.power is True
        assert simulator.state.safety_lock is False

    @pytest.mark.asyncio
    async def test_door_ends_closed(self, runner, simulator):
        """After running, door should be closed."""
        script = get_builtin_script("full_test_suite")
        await runner.run(script, verbose=False)
        assert simulator.state.door_status == DOOR_STATE_CLOSED


@requires_yaml
class TestFullTestSuiteMessages:
    """Test messages generated during full_test_suite script execution."""

    @pytest.mark.asyncio
    async def test_generates_many_status_updates(self, runner, simulator, message_capture):
        """Full suite should generate many status update messages."""
        script = get_builtin_script("full_test_suite")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        status_updates = message_capture.find_status_updates()
        # Full test suite runs multiple cycles, should have many updates
        assert len(status_updates) >= 5, (
            f"Full test suite should generate many status updates, got {len(status_updates)}"
        )

    @pytest.mark.asyncio
    async def test_status_sequence_has_variety(self, runner, simulator, message_capture):
        """Should see multiple different door states during test."""
        from powerpetdoor.const import DOOR_STATE_RISING, DOOR_STATE_HOLDING

        script = get_builtin_script("full_test_suite")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        statuses = message_capture.get_status_sequence()
        unique_statuses = set(statuses)

        # Should see at least closed, rising, and some open state
        assert len(unique_statuses) >= 2, (
            f"Should see multiple status types, got: {unique_statuses}"
        )
        # Verify we saw open states
        has_open = any(
            s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING)
            for s in statuses
        )
        assert has_open, f"Should see open states in sequence: {statuses}"