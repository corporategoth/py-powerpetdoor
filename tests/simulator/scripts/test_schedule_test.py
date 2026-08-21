# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the schedule_test built-in script."""

from __future__ import annotations

import pytest

from powerpetdoor.const import (
    CMD_DELETE_SCHEDULE,
    CMD_SET_SCHEDULE,
    DOOR_STATE_CLOSED,
    FIELD_INDEX,
    FIELD_SCHEDULE,
)
from powerpetdoor.simulator.scripting import YAML_AVAILABLE, get_builtin_script

from .conftest import FULL_CYCLE

requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")


@requires_yaml
class TestScheduleTest:
    """Tests for the schedule_test script."""

    def test_script_exists(self):
        """The schedule_test script should exist and be loadable."""
        script = get_builtin_script("schedule_test")
        assert script.name == "Schedule Enforcement Test"

    def test_script_has_schedule_actions(self):
        """Script should include schedule add/remove actions."""
        script = get_builtin_script("schedule_test")
        actions = [s.action for s in script.steps]

        assert "add_schedule" in actions
        assert "remove_schedule" in actions

    def test_script_has_multiple_test_sections(self):
        """Script should have multiple test sections."""
        script = get_builtin_script("schedule_test")
        log_messages = [s.params.get("message", "") for s in script.steps if s.action == "log"]
        log_text = " ".join(log_messages)

        assert "Test 1" in log_text
        assert "Test 2" in log_text
        assert "Test 3" in log_text

    async def test_script_passes_with_schedules_cleaned_up(self, runner, simulator):
        """The script passes, cycles twice, and removes its schedule."""
        result = await runner.run(get_builtin_script("schedule_test"), verbose=False)

        assert result is True
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        assert simulator.state.total_open_cycles == 2
        assert simulator.state.schedules == {}


@requires_yaml
class TestScheduleTestMessages:
    """Broadcasts observed by a connected client during schedule_test."""

    async def test_broadcasts_two_cycles_and_schedule_changes(
        self, runner, simulator, message_capture
    ):
        """The client sees two cycles plus the schedule add/delete broadcasts."""
        result = await runner.run(get_builtin_script("schedule_test"), verbose=False)
        assert result is True

        # The schedule deletion broadcast is the last message the script causes
        await message_capture.wait_for(
            lambda msgs: any(m.get("CMD") == CMD_DELETE_SCHEDULE for m in msgs)
        )

        assert message_capture.get_status_sequence() == FULL_CYCLE + FULL_CYCLE

        set_messages = message_capture.find_messages(CMD_SET_SCHEDULE)
        assert len(set_messages) == 1
        assert set_messages[0][FIELD_SCHEDULE][FIELD_INDEX] == 1

        delete_messages = message_capture.find_messages(CMD_DELETE_SCHEDULE)
        assert len(delete_messages) == 1
        assert delete_messages[0][FIELD_INDEX] == 1
