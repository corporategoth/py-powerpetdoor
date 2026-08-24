# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for schedule management commands (commands/schedules.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from powerpetdoor.const import (
    CMD_DELETE_SCHEDULE,
    CMD_SET_SCHEDULE,
    FIELD_CMD,
    FIELD_INDEX,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.scripting import ScriptRunner
from powerpetdoor.simulator.state import Schedule

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def timing_config():
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_start_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(timing_config):
    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
def command_handler(simulator):
    return CommandHandler(
        simulator=simulator,
        script_runner=ScriptRunner(simulator),
        stop_callback=MagicMock(),
    )


@pytest.fixture
def mock_client(simulator):
    """A fake connected client protocol for broadcast payload assertions."""
    protocol = MagicMock()
    protocol._door_task = None
    simulator.protocols.append(protocol)
    yield protocol
    simulator.protocols.clear()


def _sent_cmds(protocol):
    return [call.args[0][FIELD_CMD] for call in protocol._send.call_args_list]


# ============================================================================
# schedule (bare) and schedule list
# ============================================================================


class TestScheduleList:
    async def test_bare_schedule_shows_implicit_when_empty(self, command_handler):
        result = await command_handler.execute("schedule")
        assert result.success is True
        assert result.message == (
            "Schedules (auto mode ON):\n"
            "  (implicit): inside and outside sensors, all days, 00:00-23:59"
        )

    async def test_list_reflects_auto_off(self, command_handler):
        command_handler.simulator.state.auto = False
        result = await command_handler.execute("schedule list")
        assert result.success is True
        assert result.message.startswith("Schedules (auto mode OFF):")

    async def test_list_shows_configured_schedules_sorted(self, command_handler):
        sim = command_handler.simulator
        sim.state.schedules[2] = Schedule(index=2, inside=True, days_of_week=[0, 1, 1, 1, 1, 1, 0])
        sim.state.schedules[0] = Schedule(
            index=0, outside=True, start_hour=8, start_min=30, end_hour=20, end_min=15
        )

        result = await command_handler.execute("schedule list")
        assert result.success is True
        assert result.message == (
            "Schedules (auto mode ON):\n"
            "  #0: outside sensor, all days, 08:30-20:15 (enabled)\n"
            "  #2: inside sensor, weekdays, 06:00-22:00 (enabled)"
        )

    async def test_list_renders_both_sensors_and_disabled(self, command_handler):
        command_handler.simulator.state.schedules[0] = Schedule(
            index=0, inside=True, outside=True, enabled=False
        )
        result = await command_handler.execute("schedule list")
        assert (
            "  #0: inside and outside sensors, all days, 06:00-22:00 (disabled)" in result.message
        )

    async def test_list_renders_no_sensor_and_weekend_preset(self, command_handler):
        command_handler.simulator.state.schedules[0] = Schedule(
            index=0, days_of_week=[1, 0, 0, 0, 0, 0, 1]
        )
        result = await command_handler.execute("schedule list")
        assert "  #0: no sensors, weekends, 06:00-22:00 (enabled)" in result.message

    async def test_list_renders_custom_days_and_none_days(self, command_handler):
        sim = command_handler.simulator
        sim.state.schedules[0] = Schedule(index=0, inside=True, days_of_week=[1, 1, 0, 0, 0, 0, 0])
        sim.state.schedules[1] = Schedule(index=1, inside=True, days_of_week=[0] * 7)
        result = await command_handler.execute("schedule list")
        assert "  #0: inside sensor, sun, mon, 06:00-22:00 (enabled)" in result.message
        assert "  #1: inside sensor, none, 06:00-22:00 (enabled)" in result.message

    async def test_sched_alias(self, command_handler):
        result = await command_handler.execute("sched")
        assert result.success is True
        assert "Schedules (auto mode ON):" in result.message

    async def test_schedule_help_lists_subcommands(self, command_handler):
        result = await command_handler.execute("schedule help")
        assert result.success is True
        assert result.message.startswith("schedule subcommands:")
        for sub in ("list", "add", "clear", "delete", "enable", "disable", "days", "time"):
            assert f"\n  {sub}" in result.message


# ============================================================================
# schedule add
# ============================================================================


class TestScheduleAdd:
    async def test_add_inside_weekdays(self, command_handler):
        result = await command_handler.execute("schedule add inside 6:00-22:00 weekdays")
        assert result.success is True
        assert result.message == "Added schedule #0: inside sensor, weekdays, 06:00-22:00"

        sched = command_handler.simulator.state.schedules[0]
        assert sched.inside is True
        assert sched.outside is False
        assert sched.enabled is True
        assert sched.days_of_week == [0, 1, 1, 1, 1, 1, 0]
        assert (sched.start_hour, sched.start_min) == (6, 0)
        assert (sched.end_hour, sched.end_min) == (22, 0)

    async def test_add_outside_sensor(self, command_handler):
        result = await command_handler.execute("schedule add outside 7:15-19:45 weekends")
        assert result.success is True
        assert result.message == "Added schedule #0: outside sensor, weekends, 07:15-19:45"
        sched = command_handler.simulator.state.schedules[0]
        assert sched.inside is False
        assert sched.outside is True

    async def test_add_both_sensors_default_days(self, command_handler):
        result = await command_handler.execute("schedule add both 8:15-20:45")
        assert result.success is True
        assert (
            result.message == "Added schedule #0: inside and outside sensors, all days, 08:15-20:45"
        )
        sched = command_handler.simulator.state.schedules[0]
        assert sched.inside is True
        assert sched.outside is True
        assert sched.days_of_week == [1, 1, 1, 1, 1, 1, 1]

    async def test_add_broadcasts_schedule(self, command_handler, mock_client):
        await command_handler.execute("schedule add inside 6:00-22:00")
        assert CMD_SET_SCHEDULE in _sent_cmds(mock_client)

    async def test_add_allocates_next_free_index(self, command_handler):
        await command_handler.execute("schedule add inside 6:00-7:00")
        await command_handler.execute("schedule add inside 8:00-9:00")
        result = await command_handler.execute("schedule add inside 10:00-11:00")
        assert "Added schedule #2:" in result.message

    async def test_add_reuses_deleted_index(self, command_handler):
        await command_handler.execute("schedule add inside 6:00-7:00")
        await command_handler.execute("schedule add inside 8:00-9:00")
        await command_handler.execute("schedule delete 0")
        result = await command_handler.execute("schedule add outside 10:00-11:00")
        assert "Added schedule #0:" in result.message
        assert sorted(command_handler.simulator.state.schedules) == [0, 1]

    async def test_add_refuses_to_allocate_past_the_wire_bound(self, command_handler):
        """The index search is capped at MAX_SCHEDULE_INDEX.

        A wire peer can legitimately fill every legal slot (0-255); the
        operator's next `schedule add` then silently created index 256,
        which to_dict() put on the wire and GET_SCHEDULE_LIST returned - a
        value the simulator would itself reject if a client sent it.
        """
        from powerpetdoor.schedule import MAX_SCHEDULE_INDEX
        from powerpetdoor.simulator.state import Schedule

        state = command_handler.simulator.state
        for index in range(MAX_SCHEDULE_INDEX + 1):
            state.schedules[index] = Schedule(index=index)

        result = await command_handler.execute("schedule add inside 6:00-7:00")

        assert result.success is False
        assert result.message == "No free schedule slots"
        assert max(state.schedules) == MAX_SCHEDULE_INDEX

    async def test_add_still_allocates_the_last_legal_slot(self, command_handler):
        """The cap is inclusive: slot 255 is legal and must still be usable."""
        from powerpetdoor.schedule import MAX_SCHEDULE_INDEX
        from powerpetdoor.simulator.state import Schedule

        state = command_handler.simulator.state
        for index in range(MAX_SCHEDULE_INDEX):
            state.schedules[index] = Schedule(index=index)

        result = await command_handler.execute("schedule add inside 6:00-7:00")

        assert result.success is True
        assert f"Added schedule #{MAX_SCHEDULE_INDEX}:" in result.message

    async def test_add_invalid_time_range(self, command_handler):
        result = await command_handler.execute("schedule add inside 25:00-26:00 all")
        assert result.success is False
        assert result.message == (
            "Invalid time: 25:00\nUsage: schedule add <inside|outside|both> <start-end> [days]"
        )

    async def test_add_invalid_sensor(self, command_handler):
        result = await command_handler.execute("schedule add sideways 6:00-7:00")
        assert result.success is False
        assert result.message == (
            "'sideways' is not valid. Choose from: inside, outside, both\n"
            "Usage: schedule add <inside|outside|both> <start-end> [days]"
        )

    async def test_add_invalid_days(self, command_handler):
        result = await command_handler.execute("schedule add inside 6:00-7:00 funday")
        assert result.success is False
        assert result.message == (
            "Unknown day: fun. Use: sun, mon, tue, wed, thu, fri, sat or all/weekdays/weekends\n"
            "Usage: schedule add <inside|outside|both> <start-end> [days]"
        )

    async def test_add_missing_arguments(self, command_handler):
        result = await command_handler.execute("schedule add")
        assert result.success is False
        assert result.message == (
            "Missing required argument: sensor\n"
            "Usage: schedule add <inside|outside|both> <start-end> [days]"
        )

    async def test_add_help_shows_arguments(self, command_handler):
        result = await command_handler.execute("schedule add help")
        assert result.success is True
        assert "sensor:" in result.message
        assert "choices: inside, outside, both" in result.message


# ============================================================================
# schedule delete / clear
# ============================================================================


class TestScheduleDelete:
    async def test_delete_removes_schedule(self, command_handler):
        await command_handler.execute("schedule add inside 6:00-7:00")
        result = await command_handler.execute("schedule delete 0")
        assert result.success is True
        assert result.message == "Deleted schedule #0"
        assert command_handler.simulator.state.schedules == {}

    async def test_delete_broadcasts_deletion(self, command_handler, mock_client):
        await command_handler.execute("schedule add inside 6:00-7:00")
        mock_client._send.reset_mock()
        await command_handler.execute("schedule delete 0")
        payload = mock_client._send.call_args.args[0]
        assert payload[FIELD_CMD] == CMD_DELETE_SCHEDULE
        assert payload[FIELD_INDEX] == 0

    async def test_delete_missing_index(self, command_handler):
        result = await command_handler.execute("schedule delete 5")
        assert result.success is False
        assert result.message == "Schedule #5 not found"

    async def test_delete_negative_index_rejected(self, command_handler):
        result = await command_handler.execute("schedule delete -1")
        assert result.success is False
        assert result.message == "'-1' is below minimum (0)\nUsage: schedule delete <index>"

    async def test_delete_aliases(self, command_handler):
        for alias in ("del", "rm", "remove"):
            await command_handler.execute("schedule add inside 6:00-7:00")
            result = await command_handler.execute(f"schedule {alias} 0")
            assert result.success is True
            assert result.message == "Deleted schedule #0"


class TestScheduleClear:
    async def test_clear_empty(self, command_handler):
        result = await command_handler.execute("schedule clear")
        assert result.success is True
        assert result.message == "No schedules to clear"

    async def test_clear_removes_all(self, command_handler):
        await command_handler.execute("schedule add inside 6:00-7:00")
        await command_handler.execute("schedule add outside 8:00-9:00")
        result = await command_handler.execute("schedule clear")
        assert result.success is True
        assert result.message == "Cleared 2 schedule(s)"
        assert command_handler.simulator.state.schedules == {}


# ============================================================================
# schedule enable / disable
# ============================================================================


class TestScheduleEnableDisable:
    async def test_disable_then_enable(self, command_handler, mock_client):
        await command_handler.execute("schedule add inside 6:00-7:00")
        sched = command_handler.simulator.state.schedules[0]

        result = await command_handler.execute("schedule disable 0")
        assert result.success is True
        assert result.message == "Schedule #0 disabled"
        assert sched.enabled is False

        mock_client._send.reset_mock()
        result = await command_handler.execute("schedule enable 0")
        assert result.success is True
        assert result.message == "Schedule #0 enabled"
        assert sched.enabled is True
        assert _sent_cmds(mock_client) == [CMD_SET_SCHEDULE]

    async def test_on_off_aliases(self, command_handler):
        await command_handler.execute("schedule add inside 6:00-7:00")
        sched = command_handler.simulator.state.schedules[0]

        result = await command_handler.execute("schedule off 0")
        assert result.message == "Schedule #0 disabled"
        assert sched.enabled is False

        result = await command_handler.execute("schedule on 0")
        assert result.message == "Schedule #0 enabled"
        assert sched.enabled is True

    async def test_enable_missing_index(self, command_handler):
        result = await command_handler.execute("schedule enable 3")
        assert result.success is False
        assert result.message == "Schedule #3 not found"

    async def test_disable_missing_index(self, command_handler):
        result = await command_handler.execute("schedule disable 3")
        assert result.success is False
        assert result.message == "Schedule #3 not found"


# ============================================================================
# schedule days / time
# ============================================================================


class TestScheduleDays:
    async def test_set_days_preset(self, command_handler, mock_client):
        await command_handler.execute("schedule add inside 6:00-7:00")
        mock_client._send.reset_mock()

        result = await command_handler.execute("schedule days 0 weekends")
        assert result.success is True
        assert result.message == "Schedule #0 days: weekends"
        assert command_handler.simulator.state.schedules[0].days_of_week == [1, 0, 0, 0, 0, 0, 1]
        assert _sent_cmds(mock_client) == [CMD_SET_SCHEDULE]

    async def test_set_days_names(self, command_handler):
        await command_handler.execute("schedule add inside 6:00-7:00")
        result = await command_handler.execute("schedule days 0 mon,wed,fri")
        assert result.success is True
        assert result.message == "Schedule #0 days: mon, wed, fri"
        assert command_handler.simulator.state.schedules[0].days_of_week == [0, 1, 0, 1, 0, 1, 0]

    async def test_days_missing_index(self, command_handler):
        result = await command_handler.execute("schedule days 7 all")
        assert result.success is False
        assert result.message == "Schedule #7 not found"

    async def test_days_invalid_days(self, command_handler):
        await command_handler.execute("schedule add inside 6:00-7:00")
        result = await command_handler.execute("schedule days 0 blursday")
        assert result.success is False
        assert result.message == (
            "Unknown day: blu. Use: sun, mon, tue, wed, thu, fri, sat or all/weekdays/weekends\n"
            "Usage: schedule days <index> <days>"
        )


class TestScheduleTime:
    async def test_set_time(self, command_handler, mock_client):
        await command_handler.execute("schedule add inside 6:00-7:00")
        mock_client._send.reset_mock()

        result = await command_handler.execute("schedule time 0 7:30-21:15")
        assert result.success is True
        assert result.message == "Schedule #0 time: 07:30-21:15"
        sched = command_handler.simulator.state.schedules[0]
        assert (sched.start_hour, sched.start_min) == (7, 30)
        assert (sched.end_hour, sched.end_min) == (21, 15)
        assert _sent_cmds(mock_client) == [CMD_SET_SCHEDULE]

    async def test_time_missing_index(self, command_handler):
        result = await command_handler.execute("schedule time 4 6:00-7:00")
        assert result.success is False
        assert result.message == "Schedule #4 not found"

    async def test_time_invalid_range(self, command_handler):
        await command_handler.execute("schedule add inside 6:00-7:00")
        result = await command_handler.execute("schedule time 0 6:00")
        assert result.success is False
        assert result.message == (
            "Time range must be in format <start>-<end> (e.g., 6:00-22:00)\n"
            "Usage: schedule time <index> <start-end>"
        )
