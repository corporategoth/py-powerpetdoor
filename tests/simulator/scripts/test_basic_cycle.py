# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the basic_cycle built-in script."""
from __future__ import annotations

import pytest

from powerpetdoor.simulator.scripting import get_builtin_script, YAML_AVAILABLE
from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_RISING,
    DOOR_STATE_HOLDING,
)


requires_yaml = pytest.mark.skipif(
    not YAML_AVAILABLE,
    reason="PyYAML not installed"
)


@requires_yaml
class TestBasicCycle:
    """Tests for the basic_cycle script."""

    def test_script_exists(self):
        """The basic_cycle script should exist and be loadable."""
        script = get_builtin_script("basic_cycle")
        assert script.name == "Basic Door Cycle"

    def test_script_has_expected_steps(self):
        """Script should have expected structure."""
        script = get_builtin_script("basic_cycle")
        assert len(script.steps) > 0
        # Should start with assert door is closed
        assert script.steps[0].action == "assert"
        # Should include trigger_sensor
        actions = [s.action for s in script.steps]
        assert "trigger_sensor" in actions

    @pytest.mark.asyncio
    async def test_script_runs_successfully(self, runner, simulator):
        """The script should complete without errors."""
        script = get_builtin_script("basic_cycle")
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_door_returns_to_closed(self, runner, simulator):
        """After running, door should be closed."""
        script = get_builtin_script("basic_cycle")
        await runner.run(script, verbose=False)
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_cycle_count_increases(self, runner, simulator):
        """Running the script should increase open cycle count."""
        initial_count = simulator.state.total_open_cycles
        script = get_builtin_script("basic_cycle")
        await runner.run(script, verbose=False)
        assert simulator.state.total_open_cycles > initial_count


@requires_yaml
class TestBasicCycleMessages:
    """Test messages generated during basic_cycle script execution."""

    @pytest.mark.asyncio
    async def test_generates_status_updates(self, runner, simulator, message_capture):
        """Script should generate door status update messages."""
        script = get_builtin_script("basic_cycle")
        await runner.run(script, verbose=False)

        # Give time for messages to be collected
        import asyncio
        await asyncio.sleep(0.1)

        status_updates = message_capture.find_status_updates()
        assert len(status_updates) > 0, "Should receive status update messages"

    @pytest.mark.asyncio
    async def test_status_sequence_includes_open_close(
        self, runner, simulator, message_capture
    ):
        """Status updates should show door opening and closing."""
        script = get_builtin_script("basic_cycle")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        statuses = message_capture.get_status_sequence()

        # Should see door rising or holding (open states)
        has_open = any(
            s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING)
            for s in statuses
        )
        assert has_open, f"Should see open state in sequence: {statuses}"

        # Should end with door closed
        if statuses:
            assert statuses[-1] == DOOR_STATE_CLOSED, "Should end closed"

    @pytest.mark.asyncio
    async def test_multiple_status_messages_during_cycle(
        self, runner, simulator, message_capture
    ):
        """Should receive multiple status updates as door goes through cycle."""
        script = get_builtin_script("basic_cycle")
        await runner.run(script, verbose=False)

        import asyncio
        await asyncio.sleep(0.1)

        status_updates = message_capture.find_status_updates()
        # A full cycle should generate at least: rising, holding, closing stages
        assert len(status_updates) >= 3, (
            f"Expected at least 3 status updates for full cycle, got {len(status_updates)}"
        )
