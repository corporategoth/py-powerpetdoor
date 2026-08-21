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

from powerpetdoor.const import (
    CMD_CLOSE,
    CMD_DELETE_SCHEDULE,
    CMD_DISABLE_INSIDE,
    CMD_ENABLE_INSIDE,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HOLD_TIME,
    CMD_GET_HW_INFO,
    CMD_GET_POWER,
    CMD_GET_SCHEDULE,
    CMD_GET_SENSORS,
    CMD_GET_SETTINGS,
    CMD_OPEN,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CONFIG,
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_RISING,
    FIELD_BATTERY_PERCENT,
    FIELD_CMD,
    FIELD_DOOR_STATUS,
    FIELD_HOLD_OPEN_TIME,
    FIELD_HOLD_TIME,
    FIELD_INDEX,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_MSG_ID_RESPONSE,
    FIELD_NOTIFICATIONS,
    FIELD_REASON,
    FIELD_SCHEDULE,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_STATE,
    FIELD_SETTINGS,
    FIELD_SUCCESS,
    NOTIFY_SENSOR_INDOOR,
    NOTIFY_SENSOR_OUTDOOR,
    PING,
    PONG,
    SENSOR_STATE_OFF,
    SENSOR_STATE_ON,
    SUCCESS_FALSE,
)
from powerpetdoor.framing import MAX_BUFFER_SIZE
from powerpetdoor.simulator import (
    CommandRegistry,
    DoorSimulatorProtocol,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.engine import DoorMotionEngine
from powerpetdoor.simulator.protocol import make_sensor_notification, sanitize_log_text

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
async def protocol(state, mock_transport):
    """Create a protocol with mock transport (cleaned up after the test)."""
    proto = DoorSimulatorProtocol(state)
    proto.connection_made(mock_transport)
    yield proto
    await proto.aclose()


def last_response(mock_transport) -> dict:
    """Decode the most recent message written to the transport."""
    return json.loads(mock_transport.write.call_args[0][0].decode("ascii"))


def all_responses(mock_transport) -> list[dict]:
    """Decode every message written to the transport."""
    return [
        json.loads(call.args[0].decode("ascii")) for call in mock_transport.write.call_args_list
    ]


async def dispatch(protocol, msg: dict) -> None:
    """Feed one message to the protocol and wait for it to be handled."""
    protocol.data_received(json.dumps(msg).encode("ascii"))
    await protocol.drain()


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

    @pytest.mark.asyncio
    async def test_connection_lost_cancels_own_engine(self, state, mock_transport):
        """connection_lost should cancel the door engine it owns."""
        proto = DoorSimulatorProtocol(state)
        proto.connection_made(mock_transport)
        proto.engine.open()
        task = proto.engine._task
        assert task is not None

        proto.connection_lost(None)

        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_connection_lost_leaves_shared_engine_running(self, state, mock_transport):
        """connection_lost must NOT stop a server-shared engine."""
        engine = DoorMotionEngine(state)
        proto = DoorSimulatorProtocol(state, engine=engine)
        proto.connection_made(mock_transport)
        engine.open()
        task = engine._task
        assert task is not None

        proto.connection_lost(None)
        await asyncio.sleep(0)

        assert not task.cancelled()
        # The door still finishes its motion after the client is gone
        assert await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0) == DOOR_STATE_HOLDING
        await engine.stop()

    @pytest.mark.asyncio
    async def test_ping_response(self, protocol, mock_transport):
        """Should respond to PING with PONG."""
        await dispatch(protocol, {PING: "test123"})

        mock_transport.write.assert_called()
        response = last_response(mock_transport)
        assert response[FIELD_CMD] == PONG
        assert response[PONG] == "test123"
        assert response[FIELD_SUCCESS] == "true"

    @pytest.mark.asyncio
    async def test_get_door_status(self, protocol, mock_transport):
        """Should respond to GET_DOOR_STATUS."""
        await dispatch(protocol, {CONFIG: CMD_GET_DOOR_STATUS, "msgId": 1})

        mock_transport.write.assert_called()
        response = last_response(mock_transport)
        assert response[FIELD_CMD] == CMD_GET_DOOR_STATUS
        assert response[FIELD_DOOR_STATUS] == DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_get_settings(self, protocol, mock_transport):
        """Should respond to GET_SETTINGS."""
        await dispatch(protocol, {CONFIG: CMD_GET_SETTINGS, "msgId": 1})

        mock_transport.write.assert_called()
        response = last_response(mock_transport)
        assert response[FIELD_CMD] == CMD_GET_SETTINGS
        assert FIELD_SETTINGS in response

    @pytest.mark.asyncio
    async def test_get_battery(self, protocol, mock_transport, state):
        """Should respond to GET_DOOR_BATTERY."""
        state.battery_percent = 75
        await dispatch(protocol, {CONFIG: CMD_GET_DOOR_BATTERY, "msgId": 1})

        mock_transport.write.assert_called()
        response = last_response(mock_transport)
        assert response[FIELD_BATTERY_PERCENT] == 75

    @pytest.mark.asyncio
    async def test_power_on_off(self, protocol, mock_transport, state):
        """Should handle POWER_ON and POWER_OFF commands."""
        # Power off
        state.power = True
        await dispatch(protocol, {CONFIG: CMD_POWER_OFF, "msgId": 1})
        assert state.power is False

        # Power on
        await dispatch(protocol, {CONFIG: CMD_POWER_ON, "msgId": 2})
        assert state.power is True

    @pytest.mark.asyncio
    async def test_power_off_closes_open_door(self, protocol, mock_transport, state):
        """POWER_OFF while the door is open should start closing it."""
        protocol.engine.open()
        await protocol.engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

        await dispatch(protocol, {CONFIG: CMD_POWER_OFF, "msgId": 1})

        assert state.door_status == DOOR_STATE_CLOSING_TOP_OPEN
        assert (
            await protocol.engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)
            == DOOR_STATE_CLOSED
        )

    @pytest.mark.asyncio
    async def test_enable_disable_inside(self, protocol, mock_transport, state):
        """Should handle ENABLE/DISABLE_INSIDE commands."""
        state.inside = True
        await dispatch(protocol, {CONFIG: CMD_DISABLE_INSIDE, "msgId": 1})
        assert state.inside is False

        await dispatch(protocol, {CONFIG: CMD_ENABLE_INSIDE, "msgId": 2})
        assert state.inside is True

    @pytest.mark.asyncio
    async def test_set_hold_time(self, protocol, mock_transport, state):
        """Should handle SET_HOLD_TIME command (centiseconds)."""
        await dispatch(
            protocol,
            {
                CONFIG: CMD_SET_HOLD_TIME,
                FIELD_HOLD_TIME: 3000,  # 30 seconds in centiseconds
                "msgId": 1,
            },
        )
        # State stores seconds, protocol uses centiseconds
        assert state.hold_time == 30.0

    @pytest.mark.asyncio
    async def test_door_command_blocked_when_power_off(self, protocol, mock_transport, state):
        """Door commands should fail when power is off."""
        state.power = False
        await dispatch(protocol, {CONFIG: CMD_OPEN, "msgId": 1})

        response = last_response(mock_transport)
        assert response[FIELD_SUCCESS] == "false"
        assert "Power" in response.get(FIELD_REASON, "")

    @pytest.mark.asyncio
    async def test_door_command_blocked_when_cmd_lockout(self, protocol, mock_transport, state):
        """Door commands should fail when command lockout is enabled."""
        state.power = True
        state.cmd_lockout = True
        await dispatch(protocol, {CONFIG: CMD_OPEN, "msgId": 1})

        response = last_response(mock_transport)
        assert response[FIELD_SUCCESS] == "false"
        assert "lockout" in response.get(FIELD_REASON, "").lower()

    @pytest.mark.asyncio
    async def test_open_command_starts_door_sequence(self, protocol, mock_transport, state):
        """OPEN should start the door rising and echo the new status."""
        await dispatch(protocol, {CONFIG: CMD_OPEN, "msgId": 1})

        responses = all_responses(mock_transport)
        open_response = next(r for r in responses if r.get(FIELD_CMD) == CMD_OPEN)
        assert open_response[FIELD_SUCCESS] == "true"
        assert open_response[FIELD_DOOR_STATUS] == DOOR_STATE_RISING

        # The engine completes the open deterministically
        assert (
            await protocol.engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)
            == DOOR_STATE_HOLDING
        )

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
        await dispatch(
            protocol, {CONFIG: CMD_SET_SCHEDULE, FIELD_SCHEDULE: schedule_data, "msgId": 1}
        )
        assert 0 in state.schedules

        # Get schedule
        await dispatch(protocol, {CONFIG: CMD_GET_SCHEDULE, FIELD_INDEX: 0, "msgId": 2})
        response = last_response(mock_transport)
        assert FIELD_SCHEDULE in response

        # Delete schedule
        await dispatch(protocol, {CONFIG: CMD_DELETE_SCHEDULE, FIELD_INDEX: 0, "msgId": 3})
        assert 0 not in state.schedules

    @pytest.mark.asyncio
    async def test_set_notifications_top_level_fields(self, protocol, mock_transport, state):
        """SET_NOTIFICATIONS uses top-level "1"/"0" fields (docs/protocol.md)."""
        assert state.sensor_on_indoor is False
        assert state.low_battery is True

        await dispatch(
            protocol,
            {
                CONFIG: CMD_SET_NOTIFICATIONS,
                FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "1",
                FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: "0",
                FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: "1",
                FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: "0",
                FIELD_LOW_BATTERY_NOTIFICATIONS: "0",
                "msgId": 1,
            },
        )

        assert state.sensor_on_indoor is True
        assert state.sensor_off_indoor is False
        assert state.sensor_on_outdoor is True
        assert state.sensor_off_outdoor is False
        assert state.low_battery is False

        response = last_response(mock_transport)
        assert response[FIELD_NOTIFICATIONS][FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS] == "1"
        assert response[FIELD_NOTIFICATIONS][FIELD_LOW_BATTERY_NOTIFICATIONS] == "0"

    @pytest.mark.asyncio
    async def test_set_notifications_nested_dict_still_accepted(
        self, protocol, mock_transport, state
    ):
        """The legacy nested "notifications" payload is still honored."""
        await dispatch(
            protocol,
            {
                CONFIG: CMD_SET_NOTIFICATIONS,
                FIELD_NOTIFICATIONS: {FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "1"},
                "msgId": 1,
            },
        )
        assert state.sensor_on_indoor is True

    @pytest.mark.asyncio
    async def test_delete_schedule_echoes_index(self, protocol, mock_transport, state):
        """DELETE_SCHEDULE echoes the deleted index (real device behavior)."""
        from powerpetdoor.simulator import Schedule

        state.schedules[3] = Schedule(index=3, inside=True)
        await dispatch(protocol, {CONFIG: CMD_DELETE_SCHEDULE, FIELD_INDEX: 3, "msgId": 1})

        response = last_response(mock_transport)
        assert response[FIELD_SUCCESS] == "true"
        assert response[FIELD_INDEX] == 3
        assert 3 not in state.schedules

    @pytest.mark.asyncio
    async def test_buffered_messages(self, protocol, mock_transport):
        """Should buffer and process partial messages."""
        # Send partial message
        protocol.data_received(b'{"')
        await protocol.drain()
        assert mock_transport.write.call_count == 0

        # Complete the message
        protocol.data_received(b'PING":"test"}')
        await protocol.drain()
        responses = all_responses(mock_transport)
        assert len(responses) == 1
        assert responses[0][FIELD_CMD] == PONG
        assert responses[0][PONG] == "test"


# ============================================================================
# Wire Framing Tests (shared framing module integration)
# ============================================================================


class TestFraming:
    """Negative and resync tests for the receive framing."""

    @pytest.mark.asyncio
    async def test_garbage_bytes_discarded(self, protocol, mock_transport):
        """Pure garbage must not wedge the buffer or produce a response."""
        protocol.data_received(b"garbage not json")
        await protocol.drain()

        assert protocol.buffer == ""
        assert mock_transport.write.call_count == 0

        # A subsequent valid message still works (no prefix poisoning)
        await dispatch(protocol, {PING: "after-garbage"})
        response = last_response(mock_transport)
        assert response[PONG] == "after-garbage"

    @pytest.mark.asyncio
    async def test_garbage_prefix_resyncs_to_next_object(self, protocol, mock_transport):
        """Garbage before a valid object is discarded, the object parsed."""
        protocol.data_received(b'junk-bytes{"PING": "t1"}')
        await protocol.drain()

        response = last_response(mock_transport)
        assert response[FIELD_CMD] == PONG
        assert response[PONG] == "t1"
        assert protocol.buffer == ""

    @pytest.mark.asyncio
    async def test_brace_inside_string_value(self, protocol, mock_transport):
        """Braces inside JSON string values must not break framing."""
        await dispatch(protocol, {PING: "a}{b"})

        response = last_response(mock_transport)
        assert response[PONG] == "a}{b"
        assert protocol.buffer == ""

    @pytest.mark.asyncio
    async def test_split_frames_across_many_chunks(self, protocol, mock_transport):
        """A message split byte-by-byte must produce exactly one response."""
        payload = json.dumps({PING: "split"}).encode("ascii")
        for i in range(len(payload)):
            protocol.data_received(payload[i : i + 1])
        await protocol.drain()

        responses = all_responses(mock_transport)
        assert len(responses) == 1
        assert responses[0][PONG] == "split"

    @pytest.mark.asyncio
    async def test_multiple_messages_in_one_chunk(self, protocol, mock_transport):
        """Multiple whitespace-separated messages in one chunk all handled."""
        chunk = json.dumps({PING: "one"}) + " \n " + json.dumps({PING: "two"})
        protocol.data_received(chunk.encode("ascii"))
        await protocol.drain()

        pongs = [r[PONG] for r in all_responses(mock_transport)]
        assert pongs == ["one", "two"]

    @pytest.mark.asyncio
    async def test_invalid_json_with_balanced_braces_skipped(self, protocol, mock_transport):
        """Balanced-brace but invalid JSON is skipped, later messages work."""
        protocol.data_received(b'{"a" broken}')
        await protocol.drain()
        assert mock_transport.write.call_count == 0

        await dispatch(protocol, {PING: "ok"})
        assert last_response(mock_transport)[PONG] == "ok"

    @pytest.mark.asyncio
    async def test_oversized_buffer_drops_connection(self, protocol, mock_transport):
        """An unterminated frame beyond the cap clears the buffer and drops the client."""
        oversized = b'{"a": "' + b"x" * (MAX_BUFFER_SIZE + 1024)
        protocol.data_received(oversized)
        await protocol.drain()

        assert protocol.buffer == ""
        mock_transport.close.assert_called_once()
        assert mock_transport.write.call_count == 0

    @pytest.mark.asyncio
    async def test_non_ascii_data_dropped_without_poisoning(self, protocol, mock_transport):
        """Undecodable bytes are dropped; later messages still work."""
        protocol.data_received(b"\xff\xfe\xfd")
        await protocol.drain()
        assert protocol.buffer == ""

        await dispatch(protocol, {PING: "still-alive"})
        assert last_response(mock_transport)[PONG] == "still-alive"


# ============================================================================
# Protocol Violation / Error Envelope Tests
# ============================================================================


class TestProtocolViolations:
    """Unknown commands and malformed messages get the error envelope."""

    @pytest.mark.asyncio
    async def test_unknown_command_answers_failure(self, protocol, mock_transport):
        """Unknown commands must answer success:"false" with a reason."""
        await dispatch(protocol, {CONFIG: "NO_SUCH_COMMAND", "msgId": 7})

        response = last_response(mock_transport)
        assert response[FIELD_CMD] == "NO_SUCH_COMMAND"
        assert response[FIELD_SUCCESS] == SUCCESS_FALSE
        assert response[FIELD_REASON] == "Unknown command"
        assert response[FIELD_MSG_ID_RESPONSE] == 7

    @pytest.mark.asyncio
    async def test_non_string_command_answers_failure(self, protocol, mock_transport):
        """A non-string command value must not crash and answers failure."""
        await dispatch(protocol, {CONFIG: 123, "msgId": 8})

        response = last_response(mock_transport)
        assert response[FIELD_SUCCESS] == SUCCESS_FALSE
        assert response[FIELD_REASON] == "Unknown command"
        assert response[FIELD_MSG_ID_RESPONSE] == 8

    @pytest.mark.asyncio
    async def test_msgid_zero_echoed(self, protocol, mock_transport):
        """msgId 0 must be echoed back (0 is a valid message id)."""
        await dispatch(protocol, {CONFIG: CMD_GET_POWER, "msgId": 0})

        response = last_response(mock_transport)
        assert response[FIELD_MSG_ID_RESPONSE] == 0

    @pytest.mark.asyncio
    async def test_handler_exception_answers_command_failed(self, protocol, mock_transport, state):
        """A handler crash must answer the error envelope, not go silent."""
        # holdTime as a string makes the handler's arithmetic raise TypeError
        await dispatch(
            protocol, {CONFIG: CMD_SET_HOLD_TIME, FIELD_HOLD_TIME: "not-a-number", "msgId": 9}
        )

        response = last_response(mock_transport)
        assert response[FIELD_CMD] == CMD_SET_HOLD_TIME
        assert response[FIELD_SUCCESS] == SUCCESS_FALSE
        assert response[FIELD_REASON] == "Command failed"
        assert response[FIELD_MSG_ID_RESPONSE] == 9
        # State was not corrupted
        assert state.hold_time == 1

    @pytest.mark.asyncio
    async def test_set_schedule_missing_payload_fails(self, protocol, mock_transport):
        """CMD_SET_SCHEDULE without a schedule payload answers failure."""
        await dispatch(protocol, {CONFIG: CMD_SET_SCHEDULE, "msgId": 4})

        response = last_response(mock_transport)
        assert response[FIELD_SUCCESS] == SUCCESS_FALSE

    @pytest.mark.asyncio
    async def test_get_schedule_unknown_index_fails(self, protocol, mock_transport):
        """GET_SCHEDULE for a missing index answers failure with a reason."""
        await dispatch(protocol, {CONFIG: CMD_GET_SCHEDULE, FIELD_INDEX: 99, "msgId": 5})

        response = last_response(mock_transport)
        assert response[FIELD_SUCCESS] == SUCCESS_FALSE
        assert response[FIELD_REASON] == "Schedule not found"

    @pytest.mark.asyncio
    async def test_delete_schedule_unknown_index_fails(self, protocol, mock_transport):
        """DELETE_SCHEDULE for a missing index answers failure with a reason."""
        await dispatch(protocol, {CONFIG: CMD_DELETE_SCHEDULE, FIELD_INDEX: 42, "msgId": 6})

        response = last_response(mock_transport)
        assert response[FIELD_SUCCESS] == SUCCESS_FALSE
        assert response[FIELD_REASON] == "Schedule not found"


# ============================================================================
# Log Sanitization Tests
# ============================================================================


class TestLogSanitization:
    """Network-derived strings are sanitized before logging."""

    def test_sanitize_escapes_esc_sequences(self):
        """ESC and other C0 controls are replaced with visible escapes."""
        assert sanitize_log_text("\x1b[2Jcleared") == "\\x1b[2Jcleared"

    def test_sanitize_escapes_c1_and_del(self):
        """DEL and C1 controls are escaped too."""
        assert sanitize_log_text("\x7f\x9b") == "\\x7f\\x9b"

    def test_sanitize_preserves_tab_and_newline(self):
        """Tab and newline survive (log formatting handles them)."""
        assert sanitize_log_text("a\tb\nc") == "a\tb\nc"

    def test_sanitize_accepts_non_string(self):
        """Non-string values are coerced, never raise."""
        assert sanitize_log_text(123) == "123"

    @pytest.mark.asyncio
    async def test_unknown_command_log_is_sanitized(self, protocol, mock_transport, caplog):
        """The unknown-command warning must not contain raw ESC bytes."""
        import logging

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.simulator.protocol"):
            await dispatch(protocol, {CONFIG: "\x1b[2J\x1b]0;evil\x07CMD", "msgId": 1})

        messages = [rec.getMessage() for rec in caplog.records]
        assert any("Unknown command" in m for m in messages)
        for m in messages:
            assert "\x1b" not in m


# ============================================================================
# Sensor Notification Envelope Tests (D2: bare envelope)
# ============================================================================


class TestSensorNotifications:
    """Simulator emits the protocol.md bare notification envelope."""

    def test_make_sensor_notification_inside_on(self, state):
        """Inside sensor 'on' event uses the bare envelope."""
        state.sensor_on_indoor = True
        msg = make_sensor_notification(state, "inside", SENSOR_STATE_ON)
        assert msg == {NOTIFY_SENSOR_INDOOR: "", FIELD_SENSOR_STATE: SENSOR_STATE_ON}

    def test_make_sensor_notification_outside_off(self, state):
        """Outside sensor 'off' event uses the bare envelope."""
        state.sensor_off_outdoor = True
        msg = make_sensor_notification(state, "outside", SENSOR_STATE_OFF)
        assert msg == {NOTIFY_SENSOR_OUTDOOR: "", FIELD_SENSOR_STATE: SENSOR_STATE_OFF}

    def test_make_sensor_notification_has_no_cmd_or_success(self, state):
        """The bare envelope must not carry CMD/success fields."""
        state.sensor_on_outdoor = True
        msg = make_sensor_notification(state, "outside", SENSOR_STATE_ON)
        assert FIELD_CMD not in msg
        assert FIELD_SUCCESS not in msg

    @pytest.mark.parametrize(
        ("sensor", "sensor_state"),
        [
            ("inside", SENSOR_STATE_ON),
            ("inside", SENSOR_STATE_OFF),
            ("outside", SENSOR_STATE_ON),
            ("outside", SENSOR_STATE_OFF),
        ],
    )
    def test_make_sensor_notification_disabled_returns_none(self, state, sensor, sensor_state):
        """Disabled notification settings suppress the event."""
        # All notification settings default to disabled in the state fixture
        assert make_sensor_notification(state, sensor, sensor_state) is None

    @pytest.mark.asyncio
    async def test_send_sensor_notification_writes_bare_envelope(
        self, protocol, mock_transport, state
    ):
        """The wire bytes contain exactly the bare envelope."""
        state.sensor_on_indoor = True
        protocol._send_sensor_notification("inside", SENSOR_STATE_ON)

        response = last_response(mock_transport)
        assert response == {NOTIFY_SENSOR_INDOOR: "", FIELD_SENSOR_STATE: SENSOR_STATE_ON}

    @pytest.mark.asyncio
    async def test_send_sensor_notification_disabled_writes_nothing(
        self, protocol, mock_transport, state
    ):
        """No message is written when the notification setting is off."""
        protocol._send_sensor_notification("inside", SENSOR_STATE_ON)
        assert mock_transport.write.call_count == 0


# ============================================================================
# Connection Lifecycle Tests
# ============================================================================


class TestConnectionLifecycle:
    """Tests for connection and disconnection handling."""

    def test_on_disconnect_callback_called(self, state, mock_transport):
        """on_disconnect callback should be called when connection lost."""
        disconnect_callback = MagicMock()
        proto = DoorSimulatorProtocol(
            state,
            on_disconnect=disconnect_callback,
        )
        proto.connection_made(mock_transport)

        # Simulate disconnect
        proto.connection_lost(None)

        disconnect_callback.assert_called_once_with(proto)

    def test_on_disconnect_with_exception(self, state, mock_transport):
        """on_disconnect should be called even when disconnect has exception."""
        disconnect_callback = MagicMock()
        proto = DoorSimulatorProtocol(
            state,
            on_disconnect=disconnect_callback,
        )
        proto.connection_made(mock_transport)

        # Simulate disconnect with exception
        proto.connection_lost(Exception("Connection reset"))

        disconnect_callback.assert_called_once_with(proto)

    def test_no_callback_when_none(self, state, mock_transport):
        """Should not error when on_disconnect is None."""
        proto = DoorSimulatorProtocol(state)
        proto.connection_made(mock_transport)

        # Should not raise
        proto.connection_lost(None)


# ============================================================================
# Protocol Value Conversion Tests
# ============================================================================


class TestHoldTimeCentiseconds:
    """Tests for hold time centiseconds <-> seconds conversion.

    The protocol uses centiseconds (1/100th of a second) for hold time,
    but the internal state stores seconds for easier manipulation.
    These tests verify the conversion is correct in all code paths.
    """

    @pytest.mark.asyncio
    async def test_get_hold_time_returns_centiseconds(self, protocol, mock_transport, state):
        """GET_HOLD_TIME should return hold time in centiseconds."""
        # Set state to 5 seconds
        state.hold_time = 5.0

        await dispatch(protocol, {CONFIG: CMD_GET_HOLD_TIME, "msgId": 1})

        response = last_response(mock_transport)
        # Should return 500 centiseconds
        assert response[FIELD_HOLD_TIME] == 500

    @pytest.mark.asyncio
    async def test_set_hold_time_converts_to_seconds(self, protocol, mock_transport, state):
        """SET_HOLD_TIME should convert centiseconds to seconds in state."""
        # Send 1500 centiseconds (15 seconds)
        await dispatch(protocol, {CONFIG: CMD_SET_HOLD_TIME, FIELD_HOLD_TIME: 1500, "msgId": 1})

        # State should store 15.0 seconds
        assert state.hold_time == 15.0

    @pytest.mark.asyncio
    async def test_set_hold_time_response_is_centiseconds(self, protocol, mock_transport, state):
        """SET_HOLD_TIME response should echo back centiseconds."""
        await dispatch(protocol, {CONFIG: CMD_SET_HOLD_TIME, FIELD_HOLD_TIME: 2500, "msgId": 1})

        response = last_response(mock_transport)
        # Response should contain centiseconds
        assert response[FIELD_HOLD_TIME] == 2500

    @pytest.mark.asyncio
    async def test_get_settings_hold_time_is_centiseconds(self, protocol, mock_transport, state):
        """GET_SETTINGS should return hold time in centiseconds."""
        # Set state to 7.5 seconds
        state.hold_time = 7.5

        await dispatch(protocol, {CONFIG: CMD_GET_SETTINGS, "msgId": 1})

        response = last_response(mock_transport)
        settings = response[FIELD_SETTINGS]
        # Should return 750 centiseconds
        assert settings[FIELD_HOLD_OPEN_TIME] == 750

    @pytest.mark.asyncio
    async def test_hold_time_round_trip(self, protocol, mock_transport, state):
        """Setting then getting hold time should preserve the value."""
        # Set to 4200 centiseconds (42 seconds)
        await dispatch(protocol, {CONFIG: CMD_SET_HOLD_TIME, FIELD_HOLD_TIME: 4200, "msgId": 1})

        # Now get it back
        await dispatch(protocol, {CONFIG: CMD_GET_HOLD_TIME, "msgId": 2})

        response = last_response(mock_transport)
        assert response[FIELD_HOLD_TIME] == 4200

    @pytest.mark.asyncio
    async def test_hold_time_fractional_seconds(self, protocol, mock_transport, state):
        """Should handle fractional second values correctly."""
        # 50 centiseconds = 0.5 seconds
        await dispatch(protocol, {CONFIG: CMD_SET_HOLD_TIME, FIELD_HOLD_TIME: 50, "msgId": 1})

        assert state.hold_time == 0.5

        # Verify it comes back correctly
        await dispatch(protocol, {CONFIG: CMD_GET_HOLD_TIME, "msgId": 2})

        response = last_response(mock_transport)
        assert response[FIELD_HOLD_TIME] == 50

    @pytest.mark.asyncio
    async def test_default_hold_time(self, state):
        """Default hold time should be 1 second."""
        # The fixture creates state with hold_time=1
        assert state.hold_time == 1.0

    @pytest.mark.asyncio
    async def test_hold_time_in_settings_matches_dedicated_command(
        self, protocol, mock_transport, state
    ):
        """Hold time from GET_SETTINGS should match GET_HOLD_TIME."""
        state.hold_time = 12.34  # 1234 centiseconds

        # Get via dedicated command
        await dispatch(protocol, {CONFIG: CMD_GET_HOLD_TIME, "msgId": 1})
        hold_time_response = last_response(mock_transport)

        # Get via settings
        await dispatch(protocol, {CONFIG: CMD_GET_SETTINGS, "msgId": 2})
        settings_response = last_response(mock_transport)

        # Both should return 1234 centiseconds
        assert hold_time_response[FIELD_HOLD_TIME] == 1234
        assert settings_response[FIELD_SETTINGS][FIELD_HOLD_OPEN_TIME] == 1234
