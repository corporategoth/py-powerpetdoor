# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator protocol module (protocol.py)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from powerpetdoor.simulator import (
    DoorSimulatorProtocol,
    DoorSimulatorState,
    DoorTimingConfig,
    CommandRegistry,
)
from powerpetdoor.const import (
    FIELD_SUCCESS,
    PING,
    PONG,
    CONFIG,
    CMD_GET_SETTINGS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_SENSORS,
    CMD_GET_POWER,
    CMD_GET_HW_INFO,
    CMD_GET_DOOR_BATTERY,
    CMD_OPEN,
    CMD_CLOSE,
    CMD_POWER_ON,
    CMD_POWER_OFF,
    CMD_ENABLE_INSIDE,
    CMD_DISABLE_INSIDE,
    CMD_SET_HOLD_TIME,
    FIELD_DOOR_STATUS,
    FIELD_HOLD_TIME,
    FIELD_SETTINGS,
    FIELD_BATTERY_PERCENT,
    FIELD_INDEX,
    FIELD_SCHEDULE,
    CMD_GET_SCHEDULE,
    CMD_SET_SCHEDULE,
    CMD_DELETE_SCHEDULE,
)


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def timing_config():
    """Create a fast timing config for tests."""
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
def state(timing_config):
    """Create a test state with fast timing."""
    return DoorSimulatorState(timing=timing_config, hold_time=1)


@pytest.fixture
def mock_transport():
    """Create a mock transport."""
    transport = MagicMock()
    transport.get_extra_info.return_value = ("127.0.0.1", 12345)
    transport.write = MagicMock()
    return transport


@pytest.fixture
def protocol(state, mock_transport):
    """Create a protocol with mock transport."""
    proto = DoorSimulatorProtocol(state)
    proto.connection_made(mock_transport)
    return proto


# ============================================================================
# CommandRegistry Tests
# ============================================================================

class TestCommandRegistry:
    """Tests for CommandRegistry."""

    def test_handler_registration(self):
        """Handlers should be registered via decorator."""
        # CMD_GET_SETTINGS should be registered
        handler = CommandRegistry.get(CMD_GET_SETTINGS)
        assert handler is not None
        assert callable(handler)

    def test_unknown_command_returns_none(self):
        """Unknown commands should return None."""
        handler = CommandRegistry.get("UNKNOWN_COMMAND_XYZ")
        assert handler is None

    def test_all_expected_handlers_registered(self):
        """All expected command handlers should be registered."""
        expected_commands = [
            CMD_GET_SETTINGS,
            CMD_GET_DOOR_STATUS,
            CMD_GET_SENSORS,
            CMD_GET_POWER,
            CMD_GET_HW_INFO,
            CMD_GET_DOOR_BATTERY,
            CMD_OPEN,
            CMD_CLOSE,
            CMD_POWER_ON,
            CMD_POWER_OFF,
            CMD_ENABLE_INSIDE,
            CMD_DISABLE_INSIDE,
        ]
        for cmd in expected_commands:
            assert CommandRegistry.get(cmd) is not None, f"Handler for {cmd} not found"


# ============================================================================
# DoorSimulatorProtocol Tests
# ============================================================================

class TestDoorSimulatorProtocol:
    """Tests for DoorSimulatorProtocol."""

    def test_connection_made(self, state, mock_transport):
        """connection_made should store transport."""
        proto = DoorSimulatorProtocol(state)
        proto.connection_made(mock_transport)
        assert proto.transport is mock_transport

    def test_connection_lost_cancels_door_task(self, protocol):
        """connection_lost should cancel any door task."""
        # Create a mock door task
        protocol._door_task = MagicMock()
        protocol._door_task.cancel = MagicMock()
        protocol.connection_lost(None)
        protocol._door_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_ping_response(self, protocol, mock_transport):
        """Should respond to PING with PONG."""
        msg = json.dumps({PING: "test123"}).encode("ascii")
        protocol.data_received(msg)

        # Give async task time to run
        await asyncio.sleep(0.05)

        # Check response
        mock_transport.write.assert_called()
        response = json.loads(mock_transport.write.call_args[0][0].decode("ascii"))
        assert response["CMD"] == PONG
        assert response[PONG] == "test123"
        assert response[FIELD_SUCCESS] == "true"

    @pytest.mark.asyncio
    async def test_get_door_status(self, protocol, mock_transport):
        """Should respond to GET_DOOR_STATUS."""
        msg = json.dumps({CONFIG: CMD_GET_DOOR_STATUS, "msgId": 1}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)

        mock_transport.write.assert_called()
        response = json.loads(mock_transport.write.call_args[0][0].decode("ascii"))
        assert response["CMD"] == CMD_GET_DOOR_STATUS
        assert FIELD_DOOR_STATUS in response

    @pytest.mark.asyncio
    async def test_get_settings(self, protocol, mock_transport):
        """Should respond to GET_SETTINGS."""
        msg = json.dumps({CONFIG: CMD_GET_SETTINGS, "msgId": 1}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)

        mock_transport.write.assert_called()
        response = json.loads(mock_transport.write.call_args[0][0].decode("ascii"))
        assert response["CMD"] == CMD_GET_SETTINGS
        assert FIELD_SETTINGS in response

    @pytest.mark.asyncio
    async def test_get_battery(self, protocol, mock_transport, state):
        """Should respond to GET_DOOR_BATTERY."""
        state.battery_percent = 75
        msg = json.dumps({CONFIG: CMD_GET_DOOR_BATTERY, "msgId": 1}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)

        mock_transport.write.assert_called()
        response = json.loads(mock_transport.write.call_args[0][0].decode("ascii"))
        assert response[FIELD_BATTERY_PERCENT] == 75

    @pytest.mark.asyncio
    async def test_power_on_off(self, protocol, mock_transport, state):
        """Should handle POWER_ON and POWER_OFF commands."""
        # Power off
        state.power = True
        msg = json.dumps({CONFIG: CMD_POWER_OFF, "msgId": 1}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)
        assert state.power is False

        # Power on
        msg = json.dumps({CONFIG: CMD_POWER_ON, "msgId": 2}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)
        assert state.power is True

    @pytest.mark.asyncio
    async def test_enable_disable_inside(self, protocol, mock_transport, state):
        """Should handle ENABLE/DISABLE_INSIDE commands."""
        state.inside = True
        msg = json.dumps({CONFIG: CMD_DISABLE_INSIDE, "msgId": 1}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)
        assert state.inside is False

        msg = json.dumps({CONFIG: CMD_ENABLE_INSIDE, "msgId": 2}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)
        assert state.inside is True

    @pytest.mark.asyncio
    async def test_set_hold_time(self, protocol, mock_transport, state):
        """Should handle SET_HOLD_TIME command."""
        msg = json.dumps({
            CONFIG: CMD_SET_HOLD_TIME,
            FIELD_HOLD_TIME: 30,
            "msgId": 1
        }).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)
        assert state.hold_time == 30

    @pytest.mark.asyncio
    async def test_door_command_blocked_when_power_off(self, protocol, mock_transport, state):
        """Door commands should fail when power is off."""
        state.power = False
        msg = json.dumps({CONFIG: CMD_OPEN, "msgId": 1}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)

        response = json.loads(mock_transport.write.call_args[0][0].decode("ascii"))
        assert response[FIELD_SUCCESS] == "false"
        assert "Power" in response.get("reason", "")

    @pytest.mark.asyncio
    async def test_door_command_blocked_when_cmd_lockout(self, protocol, mock_transport, state):
        """Door commands should fail when command lockout is enabled."""
        state.power = True
        state.cmd_lockout = True
        msg = json.dumps({CONFIG: CMD_OPEN, "msgId": 1}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)

        response = json.loads(mock_transport.write.call_args[0][0].decode("ascii"))
        assert response[FIELD_SUCCESS] == "false"
        assert "lockout" in response.get("reason", "").lower()

    @pytest.mark.asyncio
    async def test_schedule_crud(self, protocol, mock_transport, state):
        """Should handle schedule CRUD operations."""
        # Create schedule
        schedule_data = {
            "index": 0,
            "enabled": "1",
            "daysOfWeek": 0b1111111,
            "inStartTime": {"hour": 6, "min": 0},
            "inEndTime": {"hour": 22, "min": 0},
            "outStartTime": {"hour": 6, "min": 0},
            "outEndTime": {"hour": 22, "min": 0},
        }
        msg = json.dumps({
            CONFIG: CMD_SET_SCHEDULE,
            FIELD_SCHEDULE: schedule_data,
            "msgId": 1
        }).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)
        assert 0 in state.schedules

        # Get schedule
        msg = json.dumps({CONFIG: CMD_GET_SCHEDULE, FIELD_INDEX: 0, "msgId": 2}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)
        response = json.loads(mock_transport.write.call_args[0][0].decode("ascii"))
        assert FIELD_SCHEDULE in response

        # Delete schedule
        msg = json.dumps({CONFIG: CMD_DELETE_SCHEDULE, FIELD_INDEX: 0, "msgId": 3}).encode("ascii")
        protocol.data_received(msg)
        await asyncio.sleep(0.05)
        assert 0 not in state.schedules

    def test_find_json_end_simple(self, protocol):
        """Should find end of simple JSON object."""
        assert protocol._find_json_end('{"a":1}') == 7
        assert protocol._find_json_end('{"a":1}extra') == 7

    def test_find_json_end_nested(self, protocol):
        """Should find end of nested JSON object."""
        assert protocol._find_json_end('{"a":{"b":1}}') == 13
        assert protocol._find_json_end('{"a":{"b":{"c":1}}}') == 19

    def test_find_json_end_incomplete(self, protocol):
        """Should return None for incomplete JSON."""
        assert protocol._find_json_end('{"a":1') is None
        assert protocol._find_json_end('{"a":{') is None

    def test_find_json_end_not_object(self, protocol):
        """Should return None if not starting with {."""
        assert protocol._find_json_end('') is None
        assert protocol._find_json_end('[1,2,3]') is None

    @pytest.mark.asyncio
    async def test_buffered_messages(self, protocol, mock_transport):
        """Should buffer and process partial messages."""
        # Send partial message
        protocol.data_received(b'{"')
        await asyncio.sleep(0.01)
        assert mock_transport.write.call_count == 0

        # Complete the message
        protocol.data_received(b'PING":"test"}')
        await asyncio.sleep(0.05)
        assert mock_transport.write.call_count > 0
