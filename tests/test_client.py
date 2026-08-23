# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for PowerPetDoorClient."""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

from powerpetdoor import (
    PowerPetDoorClient,
    PrioritizedMessage,
    autoretract_from_door_options,
    build_set_hold_time_message,
    build_set_notifications_message,
    build_set_voltage_message,
    envelope_for_command,
    find_end,
    framing,
    make_bool,
)
from powerpetdoor.client import MAX_FAILED_MSG, MAX_FAILED_PINGS, CommandError
from powerpetdoor.const import (
    CMD_CHECK_RESET_REASON,
    CMD_CLOSE,
    CMD_DELETE_SCHEDULE,
    CMD_DISABLE_INSIDE,
    CMD_ENABLE_AUTORETRACT,
    CMD_ENABLE_INSIDE,
    CMD_GET_AUTO,
    CMD_GET_AUTORETRACT,
    CMD_GET_CMD_LOCKOUT,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HOLD_TIME,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_GET_SCHEDULE,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_SETTINGS,
    CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_TIME,
    CMD_GET_TIMEZONE,
    CMD_HAS_REMOTE_ID,
    CMD_HAS_REMOTE_KEY,
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_SCHEDULE,
    COMMAND,
    COMMAND_ENVELOPE_COMMANDS,
    CONFIG,
    DOOR_OPTION_AUTORETRACT,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_CMD_LOCKOUT,
    FIELD_HOLD_OPEN_TIME,
    FIELD_HOLD_TIME,
    FIELD_INSIDE,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SETTINGS,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SUCCESS,
    FIELD_TIME,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    FIELD_VOLTAGE,
    PING,
    PONG,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
)
from tests.conftest import bigint_frame, nested_frame

# ============================================================================
# Helper Function Tests
# ============================================================================


class TestFindEnd:
    """Tests for the find_end JSON boundary detection function."""

    def test_empty_string(self):
        """Empty string returns None."""
        assert find_end("") is None

    def test_no_json_returns_none(self):
        """String not starting with { returns None (never raises).

        The old contract raised IndexError, which let one stray byte from
        the device escape data_received and poison the connection.
        """
        assert find_end("hello world") is None

    def test_simple_object(self):
        """Simple JSON object is detected - returns position after closing brace."""
        assert find_end('{"key": "value"}') == 16

    def test_nested_object(self):
        """Nested JSON objects are handled correctly."""
        json_str = '{"outer": {"inner": "value"}}'
        assert find_end(json_str) == len(json_str)

    def test_object_with_trailing(self):
        """Returns position after first complete object."""
        json_str = '{"first": 1}{"second": 2}'
        assert find_end(json_str) == 12

    def test_incomplete_object(self):
        """Incomplete JSON returns None."""
        assert find_end('{"key": "val') is None

    def test_array_in_object(self):
        """Arrays within objects are handled."""
        json_str = '{"items": [1, 2, 3]}'
        assert find_end(json_str) == len(json_str)

    def test_brace_inside_string_value(self):
        """Braces inside JSON string values do not corrupt framing.

        The old brace counter was not string-aware, so a legal payload
        like '{"a": "}"}' truncated mid-string.
        """
        json_str = '{"a": "}"}'
        assert find_end(json_str) == len(json_str)


class TestMakeBool:
    """Tests for the make_bool type coercion function."""

    def test_true_string(self):
        """String 'true' returns True."""
        assert make_bool("true") is True

    def test_false_string(self):
        """String 'false' returns False."""
        assert make_bool("false") is False

    def test_true_bool(self):
        """Boolean True returns True."""
        assert make_bool(True) is True

    def test_false_bool(self):
        """Boolean False returns False."""
        assert make_bool(False) is False

    def test_one_int(self):
        """Integer 1 returns True."""
        assert make_bool(1) is True

    def test_zero_int(self):
        """Integer 0 returns False."""
        assert make_bool(0) is False

    def test_truthy_string(self):
        """Non-empty string returns True."""
        assert make_bool("yes") is True

    def test_empty_string(self):
        """Empty string returns None (unrecognized)."""
        assert make_bool("") is None

    def test_none(self):
        """None returns None (passed through)."""
        assert make_bool(None) is None


# ============================================================================
# PrioritizedMessage Tests
# ============================================================================


class TestPrioritizedMessage:
    """Tests for the PrioritizedMessage dataclass."""

    def test_ordering_by_priority(self):
        """Messages are ordered by priority (lower = higher priority)."""
        msg1 = PrioritizedMessage(priority=PRIORITY_LOW, sequence=0, data={})
        msg2 = PrioritizedMessage(priority=PRIORITY_HIGH, sequence=1, data={})
        msg3 = PrioritizedMessage(priority=PRIORITY_CRITICAL, sequence=2, data={})

        sorted_msgs = sorted([msg1, msg2, msg3])
        assert sorted_msgs[0].priority == PRIORITY_CRITICAL
        assert sorted_msgs[1].priority == PRIORITY_HIGH
        assert sorted_msgs[2].priority == PRIORITY_LOW

    def test_ordering_by_sequence_within_priority(self):
        """Same priority messages are ordered by sequence (FIFO)."""
        msg1 = PrioritizedMessage(priority=PRIORITY_LOW, sequence=2, data={"id": 1})
        msg2 = PrioritizedMessage(priority=PRIORITY_LOW, sequence=0, data={"id": 2})
        msg3 = PrioritizedMessage(priority=PRIORITY_LOW, sequence=1, data={"id": 3})

        sorted_msgs = sorted([msg1, msg2, msg3])
        assert sorted_msgs[0].data["id"] == 2  # sequence 0
        assert sorted_msgs[1].data["id"] == 3  # sequence 1
        assert sorted_msgs[2].data["id"] == 1  # sequence 2

    def test_data_not_compared(self):
        """Data field is excluded from comparison."""
        msg1 = PrioritizedMessage(priority=0, sequence=0, data={"a": 1})
        msg2 = PrioritizedMessage(priority=0, sequence=0, data={"b": 2})
        # Should not raise - data is not compared
        assert (msg1 <= msg2) and (msg2 <= msg1)


# ============================================================================
# Client Connection Tests
# ============================================================================


class TestClientConnection:
    """Tests for client connection management."""

    def test_available_when_connected(self, mock_client):
        """Client is available when transport is connected."""
        client, transport, _ = mock_client
        assert client.available is True

    def test_unavailable_when_disconnected(self, disconnected_client):
        """available is a real False when no transport, never None."""
        assert disconnected_client.available is False

    async def test_loopless_client_resolves_running_loop(self):
        """loop=None latches onto the running loop at use time."""
        client = PowerPetDoorClient(
            host="127.0.0.1", port=3000, keepalive=0, timeout=1.0, reconnect=1.0
        )

        assert client._eventLoop is None  # No dead private loop created
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        assert client._eventLoop is asyncio.get_running_loop()
        assert future.get_loop() is asyncio.get_running_loop()
        future.cancel()

    def test_host_property(self, mock_client):
        """Host property returns configured host."""
        client, _, _ = mock_client
        assert client.host == "192.168.1.100"

    def test_port_property(self, mock_client):
        """Port property returns configured port."""
        client, _, _ = mock_client
        assert client.port == 3000

    def test_disconnect_clears_transport(self, mock_client):
        """Disconnect closes and clears transport."""
        client, transport, _ = mock_client
        client.disconnect()

        assert transport.is_closing()
        assert client._transport is None
        assert not client.available

    def test_disconnect_clears_queue(self, mock_client):
        """Disconnect clears the message queue."""
        client, _, _ = mock_client

        # Add some messages
        client.enqueue_data({"test": 1})
        client.enqueue_data({"test": 2})

        client.disconnect()

        assert client._queue == []

    def test_disconnect_resets_sequence(self, mock_client):
        """Disconnect resets the message sequence counter."""
        client, _, _ = mock_client

        # Increment sequence
        client._msg_sequence = 100

        client.disconnect()

        assert client._msg_sequence == 0

    def test_stop_sets_shutdown_flag(self, mock_client):
        """Stop sets the shutdown flag to prevent reconnection."""
        client, _, _ = mock_client
        client.stop()
        assert client._shutdown is True

    def test_start_clears_shutdown_flag(self, disconnected_client):
        """Start clears the shutdown flag."""
        disconnected_client._shutdown = True

        with patch.object(disconnected_client, "connect", new_callable=AsyncMock):
            disconnected_client.start()

        assert disconnected_client._shutdown is False


# ============================================================================
# Message Queue Tests
# ============================================================================


class TestMessageQueue:
    """Tests for the priority message queue."""

    def test_enqueue_adds_to_queue(self, mock_client):
        """Enqueue adds message to queue."""
        client, _, _ = mock_client
        client._can_dequeue = False  # Prevent auto-dequeue

        client.enqueue_data({"test": "data"}, priority=PRIORITY_LOW)

        assert client._queue

    def test_enqueue_increments_sequence(self, mock_client):
        """Each enqueue increments the sequence counter."""
        client, _, _ = mock_client
        client._can_dequeue = False

        initial_seq = client._msg_sequence
        client.enqueue_data({"test": 1})
        client.enqueue_data({"test": 2})

        assert client._msg_sequence == initial_seq + 2

    def test_priority_ordering_in_queue(self, mock_client):
        """Higher priority messages are dequeued first."""
        client, transport, _ = mock_client
        client._can_dequeue = False

        # Add messages in reverse priority order
        client.enqueue_data({"cmd": "low"}, priority=PRIORITY_LOW)
        client.enqueue_data({"cmd": "high"}, priority=PRIORITY_HIGH)
        client.enqueue_data({"cmd": "critical"}, priority=PRIORITY_CRITICAL)

        # Get messages in priority order
        msg1 = heapq.heappop(client._queue)
        msg2 = heapq.heappop(client._queue)
        msg3 = heapq.heappop(client._queue)

        assert msg1.data["cmd"] == "critical"
        assert msg2.data["cmd"] == "high"
        assert msg3.data["cmd"] == "low"

    def test_fifo_within_same_priority(self, mock_client):
        """Same priority messages maintain FIFO order."""
        client, _, _ = mock_client
        client._can_dequeue = False

        client.enqueue_data({"order": 1}, priority=PRIORITY_LOW)
        client.enqueue_data({"order": 2}, priority=PRIORITY_LOW)
        client.enqueue_data({"order": 3}, priority=PRIORITY_LOW)

        msg1 = heapq.heappop(client._queue)
        msg2 = heapq.heappop(client._queue)
        msg3 = heapq.heappop(client._queue)

        assert msg1.data["order"] == 1
        assert msg2.data["order"] == 2
        assert msg3.data["order"] == 3


# ============================================================================
# Send Message Tests
# ============================================================================


class TestSendMessage:
    """Tests for the send_message method."""

    def test_send_message_basic(self, mock_client):
        """Basic message sending queues message for transport."""
        client, transport, _ = mock_client
        client._can_dequeue = False  # Prevent async processing

        client.send_message(COMMAND, CMD_OPEN)

        # Message should be queued
        assert client._queue

    def test_send_message_increments_msgid(self, mock_client):
        """Each send_message increments the message ID."""
        client, transport, _ = mock_client

        initial_id = client.msgId
        client.send_message(COMMAND, CMD_OPEN)
        client.send_message(COMMAND, CMD_CLOSE)

        assert client.msgId == initial_id + 2

    def test_send_message_with_notify_returns_future(self, mock_client):
        """send_message with notify=True returns a future."""
        client, _, _ = mock_client

        result = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        assert result is not None
        assert asyncio.isfuture(result)

    def test_send_message_without_notify_returns_none(self, mock_client):
        """send_message without notify returns None."""
        client, _, _ = mock_client

        result = client.send_message(COMMAND, CMD_OPEN, notify=False)

        assert result is None

    def test_send_message_high_priority_for_door_commands(self, mock_client):
        """Door commands get high priority."""
        client, _, _ = mock_client
        client._can_dequeue = False

        client.send_message(COMMAND, CMD_OPEN)
        client.send_message(COMMAND, CMD_CLOSE)

        # Check priority of queued messages
        msg = heapq.heappop(client._queue)
        assert msg.priority == PRIORITY_HIGH

    def test_send_message_low_priority_for_status(self, mock_client):
        """Status commands get low priority."""
        client, _, _ = mock_client
        client._can_dequeue = False

        client.send_message(CONFIG, CMD_GET_SETTINGS)

        msg = heapq.heappop(client._queue)
        assert msg.priority == PRIORITY_LOW


# ============================================================================
# Data Received Tests
# ============================================================================


class TestDataReceived:
    """Tests for data_received and message processing."""

    def test_valid_json_processed(self, mock_client):
        """Valid JSON is decoded and dispatched to process_message."""
        client, _, _ = mock_client
        received = _capture_messages(client)

        client.data_received(b'{"success": "true", "CMD": "TEST_CMD", "msgId": 1}')

        assert received == [{"success": "true", "CMD": "TEST_CMD", "msgId": 1}]
        assert client._buffer == ""

    def test_partial_json_buffered(self, mock_client):
        """Incomplete JSON is buffered."""
        client, _, _ = mock_client

        # Send partial JSON
        partial = '{"incomplete": '
        client.data_received(partial.encode("ascii"))

        assert client._buffer == partial

    def test_multiple_messages_processed(self, mock_client):
        """Multiple complete messages in one chunk are all dispatched."""
        client, _, _ = mock_client
        received = _capture_messages(client)

        data = (
            '{"success": "true", "CMD": "A", "msgId": 1}{"success": "true", "CMD": "B", "msgId": 2}'
        )
        client.data_received(data.encode("ascii"))

        assert received == [
            {"success": "true", "CMD": "A", "msgId": 1},
            {"success": "true", "CMD": "B", "msgId": 2},
        ]
        assert client._buffer == ""

    def test_buffered_partial_completed(self, mock_client):
        """Buffered partial message is completed with next chunk."""
        client, _, _ = mock_client

        # Send partial
        client.data_received('{"key": '.encode("ascii"))
        assert client._buffer != ""

        # Complete it
        client.data_received('"value"}'.encode("ascii"))
        assert client._buffer == ""

    def test_rx_logging_does_not_sanitize_when_debug_is_off(self, mock_client, monkeypatch):
        """The regex substitution costs ~20x the suppressed log call it feeds.

        The simulator's identical line has always been guarded by
        isEnabledFor; the library side - the one that runs unattended for
        years inside a host application - was not.
        """
        from powerpetdoor import client as client_module

        calls: list[object] = []

        def _record(value):
            calls.append(value)
            return str(value)

        monkeypatch.setattr(client_module, "sanitize_text", _record)
        logger = logging.getLogger("powerpetdoor.client")
        original_level = logger.level
        client, _, _ = mock_client
        try:
            logger.setLevel(logging.INFO)
            client.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "1"}')
            assert calls == []

            logger.setLevel(logging.DEBUG)
            client.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "2"}')
            # The guard lets it through when DEBUG really is on.
            assert calls == ['{"success": "true", "CMD": "PONG", "PONG": "2"}']
        finally:
            logger.setLevel(original_level)

    def test_dribbled_frame_is_scanned_once_per_byte(self, mock_client, monkeypatch):
        """A byte-at-a-time hostile door costs O(N) CPU here, not O(N^2).

        The shipped client is the sharpest edge of the quadratic re-scan: a
        ~750 byte/s trickle used to pin a full core inside the host
        application's event loop.
        """
        from powerpetdoor import framing

        examined = [0]
        original = framing._BraceScanner.scan

        def counting_scan(self, s, start):
            end = original(self, s, start)
            examined[0] += end - start
            return end

        monkeypatch.setattr(framing._BraceScanner, "scan", counting_scan)

        client, _, _ = mock_client
        payload = '{"a": "' + "x" * 4000
        for char in payload:
            client.data_received(char.encode("ascii"))

        assert client._buffer == payload
        assert examined[0] == len(payload)

    def test_disconnect_resets_the_frame_scanner_state(self, mock_client):
        """A partial frame left by a dead connection cannot leak into the next.

        The retained text was already cleared; the brace/string state has to
        go with it, or the first object of the new connection is swallowed
        as a continuation of the old one.
        """
        client, _, _ = mock_client
        received = _capture_messages(client)

        client.data_received(b'{"a": "unterminated')
        assert client._buffer != ""
        client.disconnect()
        assert client._buffer == ""

        client.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "5"}')
        assert received == [{"success": "true", "CMD": "PONG", "PONG": "5"}]


# ============================================================================
# Listener System Tests
# ============================================================================


class TestListenerSystem:
    """Tests for the listener callback system."""

    def test_add_listener(self, mock_client, make_callback):
        """add_listener registers a callback."""
        client, _, _ = mock_client
        callback = make_callback("test")

        client.add_listener(name="test_listener", door_status_update=callback)

        assert "test_listener" in client.door_status_listeners

    def test_del_listener_removes_from_all_registries(self, mock_client, make_callback):
        """del_listener() removes the name from every listener registry."""
        client, _, _ = mock_client
        callback = make_callback("test")

        client.add_listener(
            "test_listener",
            door_status_update=callback,
            settings_update=callback,
            sensor_update={"*": callback},
            notifications_update={"*": callback},
            stats_update={"*": callback},
            hw_info_update=callback,
            battery_update=callback,
            timezone_update=callback,
            hold_time_update=callback,
            sensor_trigger_voltage_update=callback,
            sleep_sensor_trigger_voltage_update=callback,
            remote_id_update=callback,
            remote_key_update=callback,
            reset_reason_update=callback,
            schedule_update=callback,
            schedule_delete=callback,
            notification_event=callback,
        )

        client.del_listener("test_listener")

        registries = [
            client.door_status_listeners,
            client.settings_listeners,
            *client.sensor_listeners.values(),
            *client.notifications_listeners.values(),
            *client.stats_listeners.values(),
            client.hw_info_listeners,
            client.battery_listeners,
            client.timezone_listeners,
            client.hold_time_listeners,
            client.sensor_trigger_voltage_listeners,
            client.sleep_sensor_trigger_voltage_listeners,
            client.remote_id_listeners,
            client.remote_key_listeners,
            client.reset_reason_listeners,
            client.schedule_update_listeners,
            client.schedule_delete_listeners,
            client.notification_event_listeners,
        ]
        for registry in registries:
            assert "test_listener" not in registry

    def test_del_listener_unknown_name_is_noop(self, mock_client, make_callback):
        """del_listener() for a never-added name does not raise."""
        client, _, _ = mock_client
        client.add_listener("kept", door_status_update=make_callback("kept"))

        client.del_listener("never_added")

        assert "kept" in client.door_status_listeners

    def test_del_handlers_partial_registration_is_safe(self, mock_client, make_async_callback):
        """del_handlers() must not raise when only some handlers exist."""
        client, _, _ = mock_client
        client.add_handlers("partial", on_connect=make_async_callback("connect"))

        client.del_handlers("partial")
        client.del_handlers("never_added")

        assert "partial" not in client.on_connect

    async def test_listener_invoked_on_message(self, mock_client, callback_tracker, make_callback):
        """Listener callback is invoked when relevant message received."""
        client, _, device = mock_client
        callback = make_callback("door_status")

        client.add_listener(name="test", door_status_update=callback)

        # Simulate door status response
        device.send_response_sync(
            {FIELD_SUCCESS: "true", "CMD": "DOOR_STATUS", "door_status": "DOOR_CLOSED", "msgId": 1}
        )

        # Deterministic: spin the loop until the dispatched task has run.
        async with asyncio.timeout(1.0):
            while not callback_tracker["calls"]:
                await asyncio.sleep(0)

        assert callback_tracker["calls"] == ["door_status"]
        assert callback_tracker["args"] == [(("DOOR_CLOSED",), {})]

    def test_add_handlers_registers_callbacks(self, mock_client, make_async_callback):
        """add_handlers registers connection callbacks."""
        client, _, _ = mock_client
        on_connect = make_async_callback("connect")
        on_disconnect = make_async_callback("disconnect")

        client.add_handlers(name="test", on_connect=on_connect, on_disconnect=on_disconnect)

        assert "test" in client.on_connect
        assert "test" in client.on_disconnect


# ============================================================================
# Keepalive Tests
# ============================================================================


class TestKeepalive:
    """Tests for the PING/PONG keepalive mechanism (real keepalive())."""

    @staticmethod
    def _arm_keepalive(client):
        """Prepare a client so keepalive() runs its body immediately.

        keepalive() only acts when self._keepalive holds a non-cancelled
        handle; cfg_keepalive=0 makes its internal sleep instantaneous.
        """
        client.cfg_keepalive = 0
        client._keepalive = asyncio.get_running_loop().create_future()
        client._can_dequeue = False  # Keep the PING in the queue for inspection

    async def test_keepalive_sends_ping(self, mock_client):
        """keepalive() itself enqueues a PING carrying the last-ping token."""
        client, _, _ = mock_client
        self._arm_keepalive(client)
        client._last_ping = None

        await client.keepalive()

        assert client._last_ping is not None
        assert client._queue
        msg = heapq.heappop(client._queue)
        assert msg.data[PING] == client._last_ping

    async def test_keepalive_unanswered_ping_increments_failed_pings(self, mock_client):
        """An unanswered PING increments the failure counter and re-pings."""
        client, _, _ = mock_client
        self._arm_keepalive(client)
        client._last_ping = "123"
        client._failed_pings = 0

        await client.keepalive()

        assert client._failed_pings == 1
        assert client._queue  # A new PING was enqueued

    async def test_keepalive_disconnects_after_max_failed_pings(self, mock_client):
        """Reaching MAX_FAILED_PINGS unanswered pings drops the connection."""
        client, transport, _ = mock_client
        self._arm_keepalive(client)
        client._last_ping = "123"
        client._failed_pings = MAX_FAILED_PINGS - 1

        await client.keepalive()

        assert client._transport is None
        assert transport.is_closing()
        assert not client._queue  # No new PING after disconnect

    async def test_pong_clears_last_ping(self, mock_client):
        """Successful PONG response clears _last_ping."""
        client, _, device = mock_client

        ping_value = "123456789"
        client._last_ping = ping_value

        await client.process_message({FIELD_SUCCESS: "true", "CMD": "PONG", "PONG": ping_value})

        assert client._last_ping is None

    async def test_pong_resets_failed_pings(self, mock_client):
        """A matching PONG resets the failed-ping counter to zero."""
        client, _, _ = mock_client
        client._failed_pings = 2
        client._last_ping = "424242"

        await client.process_message({FIELD_SUCCESS: "true", "CMD": "PONG", "PONG": "424242"})

        assert client._failed_pings == 0


# ============================================================================
# Connection Lost Tests
# ============================================================================


class TestConnectionLost:
    """Tests for connection_lost and reconnect scheduling."""

    def test_connection_lost_triggers_disconnect(self, mock_client):
        """connection_lost triggers disconnect cleanup."""
        client, _, _ = mock_client
        # Set shutdown to prevent reconnect task from being created
        client._shutdown = True

        client.connection_lost(None)

        assert client._transport is None

    async def test_connection_lost_schedules_tracked_reconnect(self, mock_client):
        """connection_lost creates a tracked reconnect task."""
        client, _, _ = mock_client

        with patch.object(client, "connect", new_callable=AsyncMock) as mock_connect:
            client.cfg_reconnect = 0
            client.connection_lost(None)

            task = client._reconnect_task
            assert task is not None
            await task
            assert mock_connect.await_count == 1

    async def test_connection_lost_no_reconnect_when_shutdown(self, mock_client):
        """connection_lost does not schedule a reconnect after shutdown."""
        client, _, _ = mock_client
        client._shutdown = True

        client.connection_lost(None)

        assert client._reconnect_task is None

    async def test_stop_during_reconnect_delay_cancels_reconnect(self, mock_client):
        """Drop -> stop() during the reconnect delay -> no zombie reconnect."""
        client, _, _ = mock_client

        with patch.object(client, "connect", new_callable=AsyncMock) as mock_connect:
            client.connection_lost(None)  # cfg_reconnect=1.0 delay from fixture
            task = client._reconnect_task
            assert task is not None

            client.stop()

            with pytest.raises(asyncio.CancelledError):
                await task
            assert mock_connect.await_count == 0
            assert client._reconnect_task is None

    async def test_shutdown_cancels_pending_reconnect(self, mock_client):
        """The public shutdown() also cancels a pending reconnect."""
        client, _, _ = mock_client

        with patch.object(client, "connect", new_callable=AsyncMock) as mock_connect:
            client.connection_lost(None)
            task = client._reconnect_task
            assert task is not None

            client.shutdown()

            with pytest.raises(asyncio.CancelledError):
                await task
            assert mock_connect.await_count == 0

    async def test_reconnect_skips_connect_when_shutdown(self, mock_client):
        """reconnect() checks the shutdown flag after its delay."""
        client, _, _ = mock_client

        with patch.object(client, "connect", new_callable=AsyncMock) as mock_connect:
            client._shutdown = True
            await client.reconnect(0)

            assert mock_connect.await_count == 0

    async def test_connect_failure_after_shutdown_does_not_reconnect(
        self, disconnected_client, caplog
    ):
        """The funnel every connect failure goes through checks _shutdown.

        `handle_connect_failure()` is reached from `connect()`'s except
        block, i.e. after `stop()` may already have landed. Nothing
        asserted that a failure arriving *then* stays quiet: mutating the
        guard to `if True:` survived the whole 2324-test suite, because
        the bare three-dot coverage exclusion pattern had removed the log
        line beneath it from the gate entirely.
        """
        client = disconnected_client
        client._shutdown = True

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            client.handle_connect_failure()

        assert client._reconnect_task is None
        assert caplog.records == []

    async def test_connect_failure_before_shutdown_does_reconnect(
        self, disconnected_client, caplog
    ):
        """The other side of the guard: a live client still recovers."""
        client = disconnected_client
        client._shutdown = False

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            client.handle_connect_failure()

        try:
            assert client._reconnect_task is not None
            assert [rec.getMessage() for rec in caplog.records] == [
                "Unable to connect to power pet door. Reconnecting..."
            ]
        finally:
            client.stop()

    async def test_connect_is_noop_when_shutdown(self, disconnected_client):
        """connect() refuses to run once the client is shut down."""
        client = disconnected_client
        client._shutdown = True

        await client.connect()

        assert client._transport is None

    async def test_reset_shutdown_reenables_connect(self, disconnected_client):
        """reset_shutdown() clears the flag so connect() runs again."""
        client = disconnected_client
        client.shutdown()
        assert client._shutdown is True

        client.reset_shutdown()

        assert client._shutdown is False


class TestReconnectBehavior:
    """Reconnect against a real TCP server."""

    async def test_client_reconnects_after_server_restart(self, client_config):
        """The client automatically reconnects when the server comes back."""

        async def handle(reader, writer):
            # Hold the connection open until the peer disconnects, then
            # close the writer so no StreamWriter is left to be finalized
            # by the GC (ResourceWarning under filterwarnings=error).
            try:
                await reader.read()
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        client = PowerPetDoorClient(
            host="127.0.0.1",
            port=port,
            keepalive=0,
            timeout=2.0,
            reconnect=0.05,
            loop=asyncio.get_running_loop(),
        )
        connected = asyncio.Event()
        client.add_handlers("test", on_connect=connected.set)

        try:
            await client.connect()
            async with asyncio.timeout(2.0):
                await connected.wait()
            assert client.available is True

            # Kill the server and the client's connection. The client side
            # closes first: wait_closed() waits for connection handlers on
            # Python 3.12.1+, and handle() only finishes once it sees EOF.
            connected.clear()
            server.close()
            client._transport.close()
            await server.wait_closed()

            # Restart a server on the same port; the client must reconnect.
            server = await asyncio.start_server(handle, "127.0.0.1", port)
            async with asyncio.timeout(5.0):
                await connected.wait()
            assert client.available is True
        finally:
            client.shutdown()
            server.close()
            await server.wait_closed()

    async def test_connect_failure_schedules_retry(self, client_config, refused_port):
        """A refused connection schedules a backoff retry, not an exception."""
        client = PowerPetDoorClient(
            host="127.0.0.1",
            port=refused_port,
            keepalive=0,
            timeout=1.0,
            reconnect=0.05,
            loop=asyncio.get_running_loop(),
        )

        with patch.object(client, "reconnect", new_callable=AsyncMock) as mock_reconnect:
            await client.connect()
            task = client._reconnect_task
            assert task is not None
            await task

            assert mock_reconnect.await_count == 1
            (delay,) = mock_reconnect.await_args.args
            assert delay >= client.cfg_reconnect

    async def test_reconnect_backoff_grows_and_is_capped(self, disconnected_client):
        """Backoff doubles per failed attempt, jittered, capped."""
        from powerpetdoor.client import MAX_RECONNECT_DELAY, RECONNECT_JITTER

        client = disconnected_client
        client.cfg_reconnect = 5.0

        first = client._next_reconnect_delay()
        second = client._next_reconnect_delay()
        third = client._next_reconnect_delay()

        assert 5.0 <= first <= 5.0 * (1 + RECONNECT_JITTER)
        assert 10.0 <= second <= 10.0 * (1 + RECONNECT_JITTER)
        assert 20.0 <= third <= 20.0 * (1 + RECONNECT_JITTER)

        # Many failures later, the delay is capped.
        client._reconnect_attempts = 50
        capped = client._next_reconnect_delay()
        assert capped <= MAX_RECONNECT_DELAY * (1 + RECONNECT_JITTER)

    async def test_successful_connection_resets_backoff(self, mock_client):
        """connection_made resets the reconnect attempt counter."""
        client, transport, _ = mock_client
        client.disconnect()  # release the fixture's connection first
        client._reconnect_attempts = 7

        client.connection_made(transport)

        assert client._reconnect_attempts == 0


class TestDisconnectTransitions:
    """on_disconnect must fire only on real connected->disconnected."""

    async def test_on_disconnect_fires_after_real_connection(self, mock_client):
        """Disconnecting an established connection notifies handlers."""
        client, _, _ = mock_client
        disconnected = asyncio.Event()
        client.add_handlers("test", on_disconnect=disconnected.set)

        client.disconnect()

        async with asyncio.timeout(1.0):
            await disconnected.wait()

    async def test_on_disconnect_not_fired_when_never_connected(self, disconnected_client):
        """A failed connect attempt must not produce disconnect events."""
        client = disconnected_client
        events = []

        async def on_disconnect():
            events.append("disconnect")

        client.add_handlers("test", on_disconnect=on_disconnect)
        client.disconnect()
        await asyncio.sleep(0)  # Let any (wrongly) scheduled callbacks run

        assert events == []

    async def test_on_disconnect_not_fired_twice(self, mock_client):
        """A second disconnect() must not re-notify handlers."""
        client, _, _ = mock_client
        count = 0

        async def on_disconnect():
            nonlocal count
            count += 1

        client.add_handlers("test", on_disconnect=on_disconnect)
        client.disconnect()
        client.disconnect()
        await asyncio.sleep(0)

        assert count == 1


class TestQueueFlushOnConnect:
    """Messages enqueued while disconnected flush on reconnect."""

    async def test_connection_made_kicks_nonempty_queue(self, disconnected_client, mock_transport):
        """connection_made drains messages queued while disconnected."""
        client = disconnected_client
        client.send_message(COMMAND, CMD_OPEN)  # Queued: no transport yet
        assert client._queue

        client.connection_made(mock_transport)
        async with asyncio.timeout(1.0):
            while not mock_transport.written_data:
                await asyncio.sleep(0)

        sent = mock_transport.get_last_message()
        assert sent[COMMAND] == CMD_OPEN


# ============================================================================
# Outstanding Message Tracking Tests
# ============================================================================


class TestOutstandingMessages:
    """Tests for tracking outstanding (notify=True) messages."""

    def test_notify_message_tracked(self, mock_client):
        """Messages with notify=True are tracked in _outstanding."""
        client, _, _ = mock_client

        msg_id = client.msgId
        client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        assert msg_id in client._outstanding

    async def test_response_resolves_future(self, mock_client):
        """Response with matching msgId resolves the future."""
        client, _, device = mock_client

        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        # Send response (use msgID with capital D to match what the client expects)
        device.send_response_sync(
            {
                FIELD_SUCCESS: "true",
                "CMD": "GET_SETTINGS",
                "msgID": msg_id,
                "settings": {"power_state": True},
            }
        )

        # Future resolves with the settings payload and is untracked.
        result = await asyncio.wait_for(future, timeout=1.0)
        assert result == {"power_state": True}
        assert msg_id not in client._outstanding

    async def test_disconnect_fails_outstanding_with_connection_error(self, mock_client):
        """Disconnect fails in-flight futures with ConnectionError, not cancel."""
        client, _, _ = mock_client
        loop_errors = []
        loop = asyncio.get_running_loop()
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda lp, ctx: loop_errors.append(ctx))

        try:
            future1 = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
            future2 = client.send_message(CONFIG, CMD_GET_DOOR_STATUS, notify=True)

            client.disconnect()

            assert len(client._outstanding) == 0
            assert isinstance(future1.exception(), ConnectionError)
            assert isinstance(future2.exception(), ConnectionError)

            # Let the done-callbacks run: they must not raise KeyError
            # into the loop's exception handler.
            await asyncio.sleep(0)
            assert loop_errors == []
        finally:
            loop.set_exception_handler(old_handler)

    async def test_awaiting_caller_sees_connection_error(self, mock_client):
        """The documented `await future` pattern gets a typed error on disconnect."""
        client, _, _ = mock_client
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        client.disconnect()

        with pytest.raises(ConnectionError):
            await future


# ============================================================================
# check_receipt Retry Machinery Tests
# ============================================================================


class TestCheckReceipt:
    """Retry, drop, and success paths of the receipt checker."""

    async def test_dropped_message_future_raises_timeout(self, mock_client):
        """After MAX_FAILED_MSG timeouts the future fails with TimeoutError."""
        client, transport, _ = mock_client
        client.cfg_timeout = 0.05

        future = client.send_message(COMMAND, CMD_OPEN, notify=True)

        try:
            async with asyncio.timeout(5.0):
                await future
        except TimeoutError:
            pass

        # The drop (not our safety timeout) must have failed the future.
        assert future.done()
        assert not future.cancelled()
        assert isinstance(future.exception(), TimeoutError)

    async def test_check_receipt_retransmits_on_timeout(self, mock_client):
        """The unacknowledged message is retransmitted exactly once."""
        client, transport, _ = mock_client
        client.cfg_timeout = 0.05

        future = client.send_message(COMMAND, CMD_OPEN, notify=True)
        try:
            async with asyncio.timeout(5.0):
                await future
        except TimeoutError:
            pass

        assert len(transport.written_data) == MAX_FAILED_MSG
        assert transport.written_data[0] == transport.written_data[1]
        assert client._failed_msg == 0

    async def test_response_cancels_check_receipt(self, mock_client):
        """A prompt response stops the retry timer; no retransmit occurs."""
        client, transport, device = mock_client
        client.cfg_timeout = 0.05

        msg_id = client.msgId
        future = client.send_message(COMMAND, CMD_OPEN, notify=True)

        # Wait for the message to actually go out, then respond.
        async with asyncio.timeout(2.0):
            while not transport.written_data:
                await asyncio.sleep(0)
        await client.process_message(
            {FIELD_SUCCESS: "true", "CMD": CMD_OPEN, "msgID": msg_id, "door_status": "DOOR_RISING"}
        )

        result = await asyncio.wait_for(future, timeout=2.0)
        assert result["CMD"] == CMD_OPEN  # No handler: resolves with the raw ack
        assert client._check_receipt is None
        assert len(transport.written_data) == 1

    async def test_send_data_runtime_error_disconnects(self, mock_client):
        """A transport write failure triggers a clean disconnect."""
        client, transport, _ = mock_client

        def broken_write(data):
            raise RuntimeError("broken pipe")

        transport.write = broken_write
        client._last_command = None

        await client._send_data(b'{"config": "GET_DOOR_STATUS"}')

        assert client._transport is None

    async def test_send_data_transport_gone_after_sleep(self, mock_client, caplog):
        """disconnect() during the rate-limit sleep is not fatal.

        The warning is the only trace of the dropped message, so it is
        asserted.
        """
        client, transport, _ = mock_client
        client._last_command = None
        client._last_send = time.monotonic()  # Forces the rate-limit sleep

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.client"):
            send_task = asyncio.ensure_future(client._send_data(b'{"config": "X"}'))
            await asyncio.sleep(0)  # Let _send_data reach its sleep
            client.disconnect()

            await send_task  # Must not raise AttributeError

        assert transport.written_data == []
        assert "Connection closed while waiting to send; dropping message" in caplog.text


# ============================================================================
# Protocol Violation Tests (untrusted network input)
# ============================================================================


def _capture_messages(client) -> list[dict]:
    """Route data_received dispatch into a list instead of the event loop.

    Replaces process_message with a synchronous recorder so framing tests
    are deterministic (no task scheduling involved).
    """
    received: list[dict] = []

    async def _noop() -> None:
        pass

    def _record(msg, **_kwargs):
        # `**_kwargs` absorbs `frame_size=`, which the receive path passes
        # so the throttles record real wire bytes. A recorder that pins the
        # shipped signature would break on every keyword the production
        # code ever adds.
        received.append(msg)
        return _noop()

    client.process_message = _record
    client._track_task = lambda coro: coro.close()
    return received


class TestClientProtocolViolations:
    """The client must survive arbitrary bytes from a hostile/broken device."""

    def test_garbage_bytes_do_not_raise_and_are_discarded(self, mock_client):
        """Pure garbage input neither raises nor poisons the buffer."""
        client, _, _ = mock_client

        client.data_received(b"garbage not json")

        assert client._buffer == ""

    def test_garbage_prefix_before_valid_json_recovers(self, mock_client):
        """A garbage prefix is discarded and the following message parsed."""
        client, _, _ = mock_client
        received = _capture_messages(client)

        client.data_received(b'junk{"success": "true", "CMD": "PONG", "PONG": "1"}')

        assert received == [{"success": "true", "CMD": "PONG", "PONG": "1"}]
        assert client._buffer == ""

    def test_garbage_does_not_poison_subsequent_messages(self, mock_client):
        """After garbage, later chunks still parse (no permanent wedge)."""
        client, _, _ = mock_client
        received = _capture_messages(client)

        client.data_received(b"xxxx")
        client.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "2"}')

        assert received == [{"success": "true", "CMD": "PONG", "PONG": "2"}]

    def test_non_ascii_bytes_skipped(self, mock_client):
        """Non-ASCII data is escaped, then discarded as garbage, not fatal."""
        client, _, _ = mock_client

        client.data_received(b"\xff\xfe")

        assert client._buffer == ""

    def test_non_ascii_byte_mid_frame_does_not_desync_framing(self, mock_client, caplog):
        """One bad byte drops only its own frame; later frames arrive.

        Dropping the whole chunk on UnicodeDecodeError used to strand the
        half-buffered head of a frame, so framing never completed again
        until the 64 KiB overflow disconnect.
        """
        client, _, _ = mock_client
        received = _capture_messages(client)

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            # A frame arrives split, and its tail carries a stray byte.
            client.data_received(b'{"success": "true", "CMD": "A", ')
            client.data_received(b'"msgID": 1, "note": "\xff"}')
            # Three well-formed frames follow.
            for msg_id in (2, 3, 4):
                frame = json.dumps({"success": "true", "CMD": "B", "msgID": msg_id})
                client.data_received(frame.encode("ascii"))

        assert [msg["msgID"] for msg in received] == [2, 3, 4]
        assert client._buffer == ""
        assert "Received non-ASCII bytes from device" in caplog.text

    def test_brace_in_string_value_framed_correctly(self, mock_client):
        """A brace inside a JSON string value does not corrupt framing."""
        client, _, _ = mock_client
        received = _capture_messages(client)

        client.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "}"}')
        client.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "3"}')

        assert received == [
            {"success": "true", "CMD": "PONG", "PONG": "}"},
            {"success": "true", "CMD": "PONG", "PONG": "3"},
        ]
        assert client._buffer == ""

    def test_message_split_across_chunks(self, mock_client):
        """A message split across arbitrary chunk boundaries reassembles."""
        client, _, _ = mock_client
        received = _capture_messages(client)

        client.data_received(b'{"success": "tr')
        client.data_received(b'ue", "CMD": "PO')
        client.data_received(b'NG", "PONG": "4"}')

        assert received == [{"success": "true", "CMD": "PONG", "PONG": "4"}]
        assert client._buffer == ""

    def test_whitespace_separated_messages(self, mock_client):
        """Whitespace/newline separators between messages are tolerated."""
        client, _, _ = mock_client
        received = _capture_messages(client)

        client.data_received(
            b'{"success": "true", "CMD": "A"}\n {"success": "true", "CMD": "B"}\r\n'
        )

        assert received == [
            {"success": "true", "CMD": "A"},
            {"success": "true", "CMD": "B"},
        ]
        assert client._buffer == ""

    def test_malformed_json_frame_skipped(self, mock_client):
        """A balanced-brace but invalid JSON frame is skipped, not fatal."""
        client, _, _ = mock_client
        received = _capture_messages(client)

        client.data_received(b'{"a" broken}{"success": "true", "CMD": "PONG", "PONG": "5"}')

        assert received == [{"success": "true", "CMD": "PONG", "PONG": "5"}]
        assert client._buffer == ""

    def test_oversized_buffer_disconnects(self, mock_client):
        """Exceeding the un-parsed buffer cap drops the connection."""
        from powerpetdoor.framing import MAX_BUFFER_SIZE

        client, transport, _ = mock_client

        client.data_received(b"{" * (MAX_BUFFER_SIZE + 1))

        assert client._buffer == ""
        assert client._transport is None
        assert transport.is_closing()

    def test_overflow_reports_the_complete_frames_it_discards(self, mock_client, caplog):
        """A complete frame delivered with an overflow is dropped, loudly.

        The dispatch loop ran first, then ``_drop_connection()`` ->
        ``disconnect()`` cancelled every task it had just created: a
        legitimate, complete frame - an unsolicited notification, say -
        vanished with nothing in the log to say so. The overflow check now
        runs first and names the loss.
        """
        from powerpetdoor.framing import MAX_BUFFER_SIZE

        client, transport, _ = mock_client
        received = _capture_messages(client)

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            client.data_received(
                b'{"success": "true", "CMD": "PONG", "PONG": "1"}' + b"{" * (MAX_BUFFER_SIZE + 1)
            )

        assert received == []
        assert client._transport is None
        assert transport.is_closing()
        assert "discarding 1 complete frame(s) received in the same read" in caplog.text

    def test_empty_chunk_ignored(self, mock_client):
        """An empty chunk is a no-op."""
        client, _, _ = mock_client

        client.data_received(b"")

        assert client._buffer == ""


# ============================================================================
# Defensive process_message Tests
# ============================================================================


class TestProcessMessageDefensive:
    """process_message must treat every field as optional and untrusted."""

    async def test_message_missing_cmd_and_success_dropped(self, mock_client, caplog):
        """A JSON object with no CMD/success/notification is dropped quietly.

        The warning *is* the only observable, so it is asserted: an
        operator debugging a misbehaving door has exactly one signal that a
        frame was thrown away, and deleting the log call survived the whole
        suite. ``_tasks`` pins "dropped", not merely "logged".
        """
        client, _, _ = mock_client
        before = set(client._tasks)

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.client"):
            await client.process_message({"mystery": 1})

        assert "Ignoring malformed message from device" in caplog.text
        assert client._tasks == before

    async def test_non_dict_message_dropped(self, mock_client, caplog):
        """A non-dict message is dropped quietly - and says so."""
        client, _, _ = mock_client
        before = set(client._tasks)

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.client"):
            await client.process_message("not a dict")

        assert "Ignoring non-object message from device" in caplog.text
        assert client._tasks == before

    @pytest.mark.parametrize("bad_id", [[1, 2], {"nested": "id"}])
    async def test_unhashable_msg_id_resolves_no_future(self, mock_client, bad_id, caplog):
        """An unusable msgID is logged and ignored, never a dict lookup."""
        client, _, _ = mock_client
        client._can_dequeue = False
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.client"):
            await client.process_message(
                {"success": "true", "CMD": CMD_GET_SETTINGS, "msgID": bad_id, "settings": {}}
            )

        assert "Ignoring unusable msgID" in caplog.text
        assert future.done() is False
        assert client.replyMsgId == bad_id
        future.cancel()

    async def test_unhashable_msg_id_does_not_kill_the_receive_task(self, mock_client):
        """The whole receive path survives a list msgID off the wire."""
        client, _, _ = mock_client
        before = set(client._tasks)  # the keepalive task is tracked too

        client.data_received(b'{"success": "true", "CMD": "PONG", "msgID": [1, 2]}')

        tasks = [task for task in client._tasks if task not in before]
        assert len(tasks) == 1
        # Before the fix this raised TypeError: unhashable type: 'list'
        await asyncio.gather(*tasks)
        assert client.replyMsgId == [1, 2]

    @pytest.mark.parametrize("bad_cmd", [["x"], {"nested": "cmd"}, 5, 1.5, True], ids=repr)
    async def test_unhashable_or_non_string_cmd_matches_no_handler(self, mock_client, bad_cmd):
        """CMD is a wire value used as a dict key - it must never raise.

        A JSON container is a legal value on this wire and an unhashable
        key in Python, so ``ResponseHandlerRegistry.get`` used to raise
        ``TypeError: unhashable type`` for the sibling field ``msgID`` was
        already guarded against, turning a ~40-byte hostile frame into a
        ~447-byte ERROR traceback in the host application's log.
        """
        from powerpetdoor.client import ResponseHandlerRegistry

        assert ResponseHandlerRegistry.get(bad_cmd) is None

        client, _, _ = mock_client
        # process_message documents that handler dispatch is isolated; a
        # container CMD must produce no exception at all.
        await client.process_message({"success": "true", "CMD": bad_cmd})

    async def test_container_cmd_does_not_kill_the_receive_task(self, mock_client, caplog):
        """The receive path survives a list CMD with no traceback logged."""
        client, _, _ = mock_client

        # The keepalive task is tracked too, so select the new one.
        before = set(client._tasks)
        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            client.data_received(b'{"CMD": ["x"], "success": "true"}')
            tasks = [task for task in client._tasks if task not in before]
            assert len(tasks) == 1
            await asyncio.gather(*tasks)

        assert "Traceback" not in caplog.text
        assert caplog.records == []

    async def test_message_missing_success_fails_future(self, mock_client):
        """A response without success fails the matched future (typed)."""
        from powerpetdoor.client import CommandError

        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        await client.process_message({"CMD": "GET_SETTINGS", "msgID": msg_id})

        assert isinstance(future.exception(), CommandError)

    async def test_success_message_without_cmd_resolves_future_with_msg(self, mock_client):
        """A success envelope with no CMD acknowledges the matched future.

        No CMD means no specialized handler (the registry lookup returns
        None), so the acknowledgment itself is the result.
        """
        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        msg = {"success": "true", "msgID": msg_id}
        await client.process_message(msg)

        assert future.result() is msg

    def test_response_handler_registry_get_none_returns_none(self):
        """The registry lookup accepts a missing (None) CMD field."""
        from powerpetdoor.client import ResponseHandlerRegistry

        assert ResponseHandlerRegistry.get(None) is None

    async def test_failure_response_sets_command_error_with_reason(self, mock_client):
        """success:"false" fails the future with CommandError carrying cmd+reason."""
        from powerpetdoor.client import CommandError

        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        await client.process_message(
            {
                "success": "false",
                "CMD": "GET_SETTINGS",
                "msgID": msg_id,
                "reason": "Door is locked",
            }
        )

        err = future.exception()
        assert isinstance(err, CommandError)
        assert err.cmd == "GET_SETTINGS"
        assert err.reason == "Door is locked"
        assert "Door is locked" in str(err)

    async def test_handler_exception_fails_future(self, mock_client, monkeypatch):
        """A handler crash on a malformed payload fails the future."""
        from powerpetdoor.client import CommandError, ResponseHandlerRegistry

        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        def boom(self, msg, fut):
            raise RuntimeError("handler exploded")

        monkeypatch.setitem(ResponseHandlerRegistry._handlers, CMD_GET_SETTINGS, boom)

        await client.process_message(
            {"success": "true", "CMD": "GET_SETTINGS", "msgID": msg_id, FIELD_SETTINGS: {}}
        )

        assert future.done()
        err = future.exception()
        assert isinstance(err, CommandError)
        assert err.reason == "Malformed response"

    @pytest.mark.parametrize(
        ("cmd", "payload"),
        [
            (CMD_GET_DOOR_STATUS, {}),
            (CMD_GET_SETTINGS, {}),
            (CMD_GET_SETTINGS, {FIELD_SETTINGS: 5}),
            (CMD_GET_SETTINGS, {FIELD_SETTINGS: ["door_status"]}),
            (CMD_GET_NOTIFICATIONS, {}),
            (CMD_GET_NOTIFICATIONS, {FIELD_NOTIFICATIONS: "nope"}),
            (CMD_GET_SCHEDULE_LIST, {}),
        ],
        ids=repr,
    )
    async def test_missing_payload_field_takes_the_graceful_path(
        self, mock_client, caplog, cmd, payload
    ):
        """A legal envelope missing its payload must not log a traceback.

        Indexing ``msg[FIELD_SETTINGS]`` directly turned a 39-byte frame
        into a full ERROR traceback (x12.7 write amplification), jumping
        over the graceful "Response missing expected field" path that sits
        immediately below the handler call.
        """
        from powerpetdoor.client import CommandError

        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, cmd, notify=True)

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            await client.process_message(
                {"success": "true", "CMD": cmd, "msgID": msg_id, **payload}
            )

        err = future.exception()
        assert isinstance(err, CommandError)
        assert err.reason == "Response missing expected field"
        assert "Traceback" not in caplog.text
        assert [r for r in caplog.records if r.name == "powerpetdoor.client"] == []

    @pytest.mark.parametrize(
        "cmd",
        [
            CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK,
            CMD_GET_CMD_LOCKOUT,
            CMD_GET_AUTORETRACT,
        ],
        ids=repr,
    )
    async def test_scalar_settings_block_takes_the_graceful_path(self, mock_client, caplog, cmd):
        """``field in msg[settings]`` raises TypeError for a scalar - same class."""
        from powerpetdoor.client import CommandError

        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, cmd, notify=True)

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            await client.process_message(
                {"success": "true", "CMD": cmd, "msgID": msg_id, FIELD_SETTINGS: 5}
            )

        err = future.exception()
        assert isinstance(err, CommandError)
        assert err.reason == "Response missing expected field"
        assert [r for r in caplog.records if r.name == "powerpetdoor.client"] == []

    async def test_settings_missing_optional_fields_ok(
        self, mock_client, callback_tracker, make_callback
    ):
        """Settings without tz/holdOpenTime/voltages still process."""
        client, _, _ = mock_client
        client._can_dequeue = False
        client.add_listener(
            "test",
            settings_update=make_callback("settings"),
            timezone_update=make_callback("tz"),
            hold_time_update=make_callback("hold"),
            sensor_trigger_voltage_update=make_callback("voltage"),
            sleep_sensor_trigger_voltage_update=make_callback("sleep_voltage"),
        )
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        settings = {"power_state": "1", "inside": "1"}
        await client.process_message(
            {"success": "true", "CMD": "GET_SETTINGS", "msgID": msg_id, "settings": settings}
        )

        assert future.result() == settings
        assert callback_tracker["calls"] == ["settings"]

    async def test_listener_exception_isolated(self, mock_client):
        """One raising listener does not prevent the others running."""
        client, _, _ = mock_client
        calls = []

        def bad_listener(status):
            calls.append("bad")
            raise RuntimeError("listener bug")

        def good_listener(status):
            calls.append(("good", status))

        client.add_listener("bad", door_status_update=bad_listener)
        client.add_listener("good", door_status_update=good_listener)

        await client.process_message(
            {"success": "true", "CMD": "DOOR_STATUS", "door_status": "DOOR_CLOSED"}
        )

        assert calls == ["bad", ("good", "DOOR_CLOSED")]

    async def test_done_future_not_resolved_twice(self, mock_client):
        """Handlers never call set_result on a completed future."""
        client, _, _ = mock_client
        future = asyncio.get_running_loop().create_future()
        future.cancel()

        # Must not raise InvalidStateError.
        client._handle_door_status({"door_status": "DOOR_CLOSED"}, future)

    async def test_response_for_cancelled_future_ignored(self, mock_client):
        """A late response for a cancelled future is ignored, not fatal."""
        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_DOOR_STATUS, notify=True)
        future.cancel()

        await client.process_message(
            {
                "success": "true",
                "CMD": "GET_DOOR_STATUS",
                "msgID": msg_id,
                "door_status": "DOOR_CLOSED",
            }
        )

        assert future.cancelled()

    async def test_handler_dispatched_before_dequeue(self, mock_client):
        """The response handler runs before the next message dequeues."""
        client, _, _ = mock_client
        order = []

        client.add_listener("order", door_status_update=lambda s: order.append("handler"))

        async def fake_dequeue():
            order.append("dequeue")

        client.dequeue_data = fake_dequeue
        client._last_command = CMD_GET_DOOR_STATUS
        client._can_dequeue = True

        await client.process_message(
            {"success": "true", "CMD": "GET_DOOR_STATUS", "door_status": "DOOR_CLOSED"}
        )

        assert order == ["handler", "dequeue"]

    async def test_success_missing_expected_field_fails_future(self, mock_client):
        """A handler that cannot resolve its future fails it typed, not cancel."""
        from powerpetdoor.client import CommandError

        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        # GET_POWER response without the power_state payload field.
        await client.process_message({"success": "true", "CMD": "GET_POWER", "msgID": msg_id})

        assert future.done()
        assert not future.cancelled()
        err = future.exception()
        assert isinstance(err, CommandError)
        assert err.reason == "Response missing expected field"

    async def test_success_without_handler_resolves_with_raw_message(self, mock_client):
        """A successful response with no registered handler is the ack itself."""
        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(COMMAND, CMD_OPEN, notify=True)

        msg = {"success": "true", "CMD": CMD_OPEN, "msgID": msg_id}
        await client.process_message(msg)

        assert future.result() == msg

    async def test_unmatched_msgid_response_ignored(self, mock_client):
        """A response for an unknown msgID does not raise or touch futures."""
        client, _, _ = mock_client
        client._can_dequeue = False
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        await client.process_message(
            {
                "success": "true",
                "CMD": "GET_DOOR_STATUS",
                "msgID": 99999,
                "door_status": "DOOR_CLOSED",
            }
        )

        assert not future.done()


# ============================================================================
# Notification Event Tests
# ============================================================================


class TestNotificationEvents:
    """Device-initiated notification events (bare and CMD-style envelopes)."""

    async def test_bare_sensor_indoor_on(self, mock_client):
        """The documented bare envelope dispatches (event, state)."""
        from powerpetdoor.const import NOTIFY_SENSOR_INDOOR

        client, _, _ = mock_client
        events = []
        client.add_listener("test", notification_event=lambda ev, st: events.append((ev, st)))

        await client.process_message({"SENSOR_INDOOR": "", "sensorState": "on"})

        assert events == [(NOTIFY_SENSOR_INDOOR, "on")]

    async def test_bare_sensor_outdoor_off(self, mock_client):
        """Outdoor sensor-off bare envelope dispatches correctly."""
        from powerpetdoor.const import NOTIFY_SENSOR_OUTDOOR

        client, _, _ = mock_client
        events = []
        client.add_listener("test", notification_event=lambda ev, st: events.append((ev, st)))

        await client.process_message({"SENSOR_OUTDOOR": "", "sensorState": "off"})

        assert events == [(NOTIFY_SENSOR_OUTDOOR, "off")]

    async def test_bare_low_battery(self, mock_client):
        """LOW_BATTERY has no sensorState; state is None."""
        from powerpetdoor.const import NOTIFY_LOW_BATTERY

        client, _, _ = mock_client
        events = []
        client.add_listener("test", notification_event=lambda ev, st: events.append((ev, st)))

        await client.process_message({"LOW_BATTERY": ""})

        assert events == [(NOTIFY_LOW_BATTERY, None)]

    async def test_cmd_style_notification_tolerated(self, mock_client):
        """CMD-style notification envelopes are tolerated and also dispatch."""
        from powerpetdoor.const import NOTIFY_SENSOR_INDOOR

        client, _, _ = mock_client
        events = []
        client.add_listener("test", notification_event=lambda ev, st: events.append((ev, st)))

        await client.process_message(
            {"success": "true", "CMD": "SENSOR_INDOOR", "sensorState": "on"}
        )

        assert events == [(NOTIFY_SENSOR_INDOOR, "on")]

    async def test_bare_notification_without_listeners(self, mock_client):
        """A bare notification with no listeners registered is harmless."""
        client, _, _ = mock_client

        await client.process_message({"SENSOR_INDOOR": "", "sensorState": "on"})

    async def test_bare_notification_via_data_received(self, mock_client):
        """The full receive path handles a bare notification envelope."""
        from powerpetdoor.const import NOTIFY_SENSOR_INDOOR

        client, _, device = mock_client
        events = []
        client.add_listener("test", notification_event=lambda ev, st: events.append((ev, st)))

        device.send_response_sync({"SENSOR_INDOOR": "", "sensorState": "on"})
        await asyncio.sleep(0)

        assert events == [(NOTIFY_SENSOR_INDOOR, "on")]

    async def test_del_listener_removes_notification_event(self, mock_client):
        """del_listener also clears notification_event listeners."""
        client, _, _ = mock_client
        events = []
        client.add_listener("test", notification_event=lambda ev, st: events.append((ev, st)))
        client.del_listener("test")

        await client.process_message({"LOW_BATTERY": ""})

        assert events == []


# ============================================================================
# Timing Source Tests
# ============================================================================


class TestMonotonicTiming:
    """Intervals must use time.monotonic(); only the PING token is wall-clock."""

    async def test_send_pacing_does_not_use_wall_clock(self, mock_client, monkeypatch):
        """_send_data rate limiting uses monotonic, never time.time."""
        from types import SimpleNamespace

        client, _, _ = mock_client
        client._last_command = None
        client._last_send = time.monotonic()

        wall_calls = []

        def fake_wall_time():
            wall_calls.append(1)
            return 0.0

        fake_time = SimpleNamespace(time=fake_wall_time, monotonic=time.monotonic)
        monkeypatch.setattr("powerpetdoor.client.time", fake_time)

        await client._send_data(b'{"config": "GET_DOOR_STATUS"}')

        assert wall_calls == []

    async def test_pong_latency_never_negative_on_clock_step(self, mock_client):
        """PONG latency survives a wall-clock step (uses monotonic)."""
        client, _, _ = mock_client
        # Simulate an NTP step: the wire token claims a future wall time.
        client._last_ping = str(round(time.time() * 1000) + 10_000_000)
        client._last_ping_time = time.monotonic() - 0.05

        latencies = []
        client.add_handlers("test", on_ping=lambda ms: latencies.append(ms))

        await client.process_message({"success": "true", "CMD": "PONG", "PONG": client._last_ping})

        assert len(latencies) == 1
        assert latencies[0] >= 0
        assert client._last_ping is None
        assert client._failed_pings == 0

    async def test_pong_missing_token_ignored(self, mock_client):
        """A PONG without its token field is ignored, not fatal."""
        client, _, _ = mock_client
        client._last_ping = "12345"

        await client.process_message({"success": "true", "CMD": "PONG"})

        assert client._last_ping == "12345"


# ============================================================================
# Sensor Listener Signature Tests
# ============================================================================


class TestSensorListenerSignature:
    """Dict-based listeners receive (field, value)."""

    async def test_sensor_listener_receives_field_and_value(self, mock_client):
        """A per-field sensor listener is invoked as callback(field, value)."""
        from powerpetdoor.const import FIELD_POWER

        client, _, _ = mock_client
        calls = []
        client.add_listener(
            "test", sensor_update={FIELD_POWER: lambda field, value: calls.append((field, value))}
        )

        await client.process_message({"success": "true", "CMD": "GET_POWER", "power_state": "1"})

        assert calls == [(FIELD_POWER, True)]

    async def test_notifications_listener_receives_field_and_value(self, mock_client):
        """Notification listeners are invoked as callback(field, value)."""
        from powerpetdoor.const import (
            FIELD_LOW_BATTERY_NOTIFICATIONS,
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
        )

        client, _, _ = mock_client
        calls = []
        client.add_listener(
            "test",
            notifications_update={"*": lambda field, value: calls.append((field, value))},
        )

        await client.process_message(
            {
                "success": "true",
                "CMD": "GET_NOTIFICATIONS",
                "notifications": {
                    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "1",
                    FIELD_LOW_BATTERY_NOTIFICATIONS: "0",
                },
            }
        )

        assert (FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS, True) in calls
        assert (FIELD_LOW_BATTERY_NOTIFICATIONS, False) in calls
        assert len(calls) == 2

    async def test_stats_listener_receives_field_and_value(self, mock_client):
        """Stats listeners are invoked as callback(field, value)."""
        from powerpetdoor.const import (
            FIELD_TOTAL_AUTO_RETRACTS,
            FIELD_TOTAL_OPEN_CYCLES,
        )

        client, _, _ = mock_client
        calls = []
        client.add_listener(
            "test", stats_update={"*": lambda field, value: calls.append((field, value))}
        )

        await client.process_message(
            {
                "success": "true",
                "CMD": "GET_DOOR_OPEN_STATS",
                FIELD_TOTAL_OPEN_CYCLES: 42,
                FIELD_TOTAL_AUTO_RETRACTS: 7,
            }
        )

        assert calls == [(FIELD_TOTAL_OPEN_CYCLES, 42), (FIELD_TOTAL_AUTO_RETRACTS, 7)]


# ============================================================================
# Per-Field Listener Routing Tests
# ============================================================================


class TestPerFieldListenerRouting:
    """Single-field listener dicts route only their own field's updates."""

    async def test_sensor_subset_listeners_receive_only_their_field(self, mock_client):
        """Per-field sensor listeners each fire once, for their field only."""
        client, _, _ = mock_client
        client._can_dequeue = False
        auto_calls: list = []
        power_calls: list = []
        client.add_listener(
            "auto_only", sensor_update={FIELD_AUTO: lambda f, v: auto_calls.append((f, v))}
        )
        client.add_listener(
            "power_only", sensor_update={FIELD_POWER: lambda f, v: power_calls.append((f, v))}
        )

        settings = {
            FIELD_POWER: "1",
            FIELD_INSIDE: "1",
            FIELD_OUTSIDE: "0",
            FIELD_AUTO: "0",
            FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: "0",
            FIELD_CMD_LOCKOUT: "0",
            FIELD_AUTORETRACT: "1",
        }
        await client.process_message(
            {"success": "true", "CMD": CMD_GET_SETTINGS, "settings": settings}
        )

        assert auto_calls == [(FIELD_AUTO, False)]
        assert power_calls == [(FIELD_POWER, True)]

    async def test_notifications_subset_listeners_receive_only_their_field(self, mock_client):
        """Per-field notification listeners each fire once, for their field only."""
        client, _, _ = mock_client
        client._can_dequeue = False
        indoor_calls: list = []
        battery_calls: list = []
        client.add_listener(
            "indoor_on_only",
            notifications_update={
                FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: lambda f, v: indoor_calls.append((f, v))
            },
        )
        client.add_listener(
            "battery_only",
            notifications_update={
                FIELD_LOW_BATTERY_NOTIFICATIONS: lambda f, v: battery_calls.append((f, v))
            },
        )

        notifications = {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: "1",
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: "0",
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: "0",
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: "1",
            FIELD_LOW_BATTERY_NOTIFICATIONS: "0",
        }
        await client.process_message(
            {"success": "true", "CMD": "GET_NOTIFICATIONS", "notifications": notifications}
        )

        assert indoor_calls == [(FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS, True)]
        assert battery_calls == [(FIELD_LOW_BATTERY_NOTIFICATIONS, False)]

    async def test_stats_subset_listeners_receive_only_their_field(self, mock_client):
        """Per-field stats listeners each fire once, for their field only."""
        client, _, _ = mock_client
        client._can_dequeue = False
        cycle_calls: list = []
        retract_calls: list = []
        client.add_listener(
            "cycles_only",
            stats_update={FIELD_TOTAL_OPEN_CYCLES: lambda f, v: cycle_calls.append((f, v))},
        )
        client.add_listener(
            "retracts_only",
            stats_update={FIELD_TOTAL_AUTO_RETRACTS: lambda f, v: retract_calls.append((f, v))},
        )

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_DOOR_OPEN_STATS,
                FIELD_TOTAL_OPEN_CYCLES: 10,
                FIELD_TOTAL_AUTO_RETRACTS: 4,
            }
        )

        assert cycle_calls == [(FIELD_TOTAL_OPEN_CYCLES, 10)]
        assert retract_calls == [(FIELD_TOTAL_AUTO_RETRACTS, 4)]


# ============================================================================
# Response Handler Payload Tests
# ============================================================================


class TestResponseHandlerPayloads:
    """Each specialized handler dispatches its payload and resolves futures."""

    @staticmethod
    def _pending(client, cmd, msg_type=CONFIG):
        """Queue a notify command without sending it; return (msg_id, future)."""
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(msg_type, cmd, notify=True)
        return msg_id, future

    async def test_timezone_response_notifies_and_resolves(self, mock_client):
        client, _, _ = mock_client
        values: list = []
        client.add_listener("t", timezone_update=values.append)
        msg_id, future = self._pending(client, CMD_GET_TIMEZONE)

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_TIMEZONE,
                "msgID": msg_id,
                "tz": "PST8PDT,M3.2.0,M11.1.0",
            }
        )

        assert values == ["PST8PDT,M3.2.0,M11.1.0"]
        assert future.result() == "PST8PDT,M3.2.0,M11.1.0"

    async def test_sensor_trigger_voltage_response(self, mock_client):
        client, _, _ = mock_client
        values: list = []
        client.add_listener("t", sensor_trigger_voltage_update=values.append)
        msg_id, future = self._pending(client, CMD_GET_SENSOR_TRIGGER_VOLTAGE)

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_SENSOR_TRIGGER_VOLTAGE,
                "msgID": msg_id,
                "sensorTriggerVoltage": 120,
            }
        )

        assert values == [120]
        assert future.result() == 120

    async def test_sleep_sensor_trigger_voltage_response(self, mock_client):
        client, _, _ = mock_client
        values: list = []
        client.add_listener("t", sleep_sensor_trigger_voltage_update=values.append)
        msg_id, future = self._pending(client, CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE)

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
                "msgID": msg_id,
                "sleepSensorTriggerVoltage": 80,
            }
        )

        assert values == [80]
        assert future.result() == 80

    async def test_cmd_lockout_response_coerces_and_notifies(self, mock_client):
        client, _, _ = mock_client
        calls: list = []
        client.add_listener(
            "t", sensor_update={FIELD_CMD_LOCKOUT: lambda f, v: calls.append((f, v))}
        )
        msg_id, future = self._pending(client, CMD_GET_CMD_LOCKOUT)

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_CMD_LOCKOUT,
                "msgID": msg_id,
                "settings": {FIELD_CMD_LOCKOUT: "1"},
            }
        )

        assert calls == [(FIELD_CMD_LOCKOUT, True)]
        assert future.result() is True

    async def test_remote_id_response(self, mock_client):
        client, _, _ = mock_client
        values: list = []
        client.add_listener("t", remote_id_update=values.append)
        msg_id, future = self._pending(client, CMD_HAS_REMOTE_ID)

        await client.process_message(
            {"success": "true", "CMD": CMD_HAS_REMOTE_ID, "msgID": msg_id, "has_id": "true"}
        )

        assert values == [True]
        assert future.result() is True

    async def test_remote_key_response(self, mock_client):
        client, _, _ = mock_client
        values: list = []
        client.add_listener("t", remote_key_update=values.append)
        msg_id, future = self._pending(client, CMD_HAS_REMOTE_KEY)

        await client.process_message(
            {"success": "true", "CMD": CMD_HAS_REMOTE_KEY, "msgID": msg_id, "has_key": "false"}
        )

        assert values == [False]
        assert future.result() is False

    async def test_reset_reason_response(self, mock_client):
        client, _, _ = mock_client
        values: list = []
        client.add_listener("t", reset_reason_update=values.append)
        msg_id, future = self._pending(client, CMD_CHECK_RESET_REASON)

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_CHECK_RESET_REASON,
                "msgID": msg_id,
                "resetReason": "WATCHDOG",
            }
        )

        assert values == ["WATCHDOG"]
        assert future.result() == "WATCHDOG"

    async def test_delete_schedule_without_echoed_index(self, mock_client):
        """Firmware that omits the deleted index still resolves the future."""
        client, _, _ = mock_client
        deleted: list = []
        client.add_listener("t", schedule_delete=deleted.append)
        msg_id, future = self._pending(client, CMD_DELETE_SCHEDULE)

        await client.process_message(
            {"success": "true", "CMD": CMD_DELETE_SCHEDULE, "msgID": msg_id}
        )

        assert future.result() is None
        assert deleted == []

    async def test_stats_response_without_listeners_still_resolves(self, mock_client):
        """With no stats listeners the future still gets both values."""
        client, _, _ = mock_client
        msg_id, future = self._pending(client, CMD_GET_DOOR_OPEN_STATS)

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_DOOR_OPEN_STATS,
                "msgID": msg_id,
                FIELD_TOTAL_OPEN_CYCLES: 42,
                FIELD_TOTAL_AUTO_RETRACTS: 7,
            }
        )

        assert future.result() == {FIELD_TOTAL_OPEN_CYCLES: 42, FIELD_TOTAL_AUTO_RETRACTS: 7}

    async def test_settings_response_notifies_config_listeners(self, mock_client):
        """tz/holdOpenTime/voltage values inside settings reach their listeners."""
        client, _, _ = mock_client
        tz_values: list = []
        hold_values: list = []
        voltage_values: list = []
        sleep_values: list = []
        client.add_listener(
            "t",
            timezone_update=tz_values.append,
            hold_time_update=hold_values.append,
            sensor_trigger_voltage_update=voltage_values.append,
            sleep_sensor_trigger_voltage_update=sleep_values.append,
        )
        msg_id, future = self._pending(client, CMD_GET_SETTINGS)

        settings = {
            "tz": "EST5EDT,M3.2.0,M11.1.0",
            "holdOpenTime": 500,
            "sensorTriggerVoltage": 120,
            "sleepSensorTriggerVoltage": 80,
        }
        await client.process_message(
            {"success": "true", "CMD": CMD_GET_SETTINGS, "msgID": msg_id, "settings": settings}
        )

        assert tz_values == ["EST5EDT,M3.2.0,M11.1.0"]
        assert hold_values == [500]
        assert voltage_values == [120]
        assert sleep_values == [80]
        assert future.result() == settings

    @pytest.mark.parametrize(
        "cmd",
        [
            CMD_GET_AUTO,
            CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK,
            CMD_GET_CMD_LOCKOUT,
            CMD_GET_AUTORETRACT,
            CMD_GET_HW_INFO,
            CMD_GET_TIMEZONE,
            CMD_GET_HOLD_TIME,
            CMD_GET_SENSOR_TRIGGER_VOLTAGE,
            CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
            CMD_GET_SCHEDULE,
            CMD_HAS_REMOTE_ID,
            CMD_HAS_REMOTE_KEY,
            CMD_CHECK_RESET_REASON,
        ],
    )
    async def test_success_response_missing_payload_fails_future(self, mock_client, cmd):
        """Every guarded handler fails its future typed when the payload is absent."""
        client, _, _ = mock_client
        msg_id, future = self._pending(client, cmd)

        await client.process_message({"success": "true", "CMD": cmd, "msgID": msg_id})

        err = future.exception()
        assert isinstance(err, CommandError)
        assert err.reason == "Response missing expected field"


# ============================================================================
# Keepalive / Receipt Edge Tests
# ============================================================================


class TestKeepaliveReceiptEdges:
    """Timer bodies must be inert once their handle is cleared/cancelled."""

    async def test_keepalive_noop_when_not_armed(self, mock_client):
        """keepalive() does nothing when its task handle was cleared."""
        client, _, _ = mock_client
        client.cfg_keepalive = 0
        client._keepalive = None
        client._can_dequeue = False
        client._last_ping = None

        await client.keepalive()

        assert client._last_ping is None
        assert not client._queue

    async def test_check_receipt_after_response_resets_and_dequeues(self, mock_client):
        """A cleared receipt handle means answered: reset failures, no retransmit."""
        client, transport, _ = mock_client
        client.cfg_timeout = 0
        client._failed_msg = 1
        client._check_receipt = None
        client._can_dequeue = False

        await client.check_receipt(b'{"config": "GET_SETTINGS"}')

        assert client._failed_msg == 0
        assert transport.written_data == []  # No retransmit
        assert client._can_dequeue is True  # Queue empty: gate reopened

    async def test_send_data_without_transport_is_noop(self, disconnected_client, caplog):
        """_send_data without a connection warns and drops the payload."""
        client = disconnected_client

        with caplog.at_level(logging.WARNING):
            await client._send_data(b'{"config": "GET_SETTINGS"}')

        assert "without a connection active" in caplog.text

    async def test_fail_inflight_future_without_inflight_is_noop(self, mock_client):
        """No in-flight msgId: other outstanding futures are left untouched."""
        client, _, _ = mock_client
        client._can_dequeue = False
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
        client._inflight_msg_id = None

        client._fail_inflight_future(TimeoutError("dropped"))

        assert not future.done()
        future.cancel()

    async def test_fail_inflight_future_with_done_future_is_noop(self, mock_client):
        """A future that already completed is never failed again."""
        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
        future.set_result("done")
        client._inflight_msg_id = msg_id

        client._fail_inflight_future(TimeoutError("dropped"))

        assert future.result() == "done"
        assert client._inflight_msg_id is None


# ============================================================================
# Dequeue Edge Tests
# ============================================================================


class TestDequeueData:
    """dequeue_data guards and command-type classification."""

    async def test_dequeue_without_transport_warns_and_sends_nothing(
        self, disconnected_client, caplog
    ):
        """A dequeue that outlives the connection drops the message safely."""
        client = disconnected_client
        heapq.heappush(
            client._queue,
            PrioritizedMessage(priority=PRIORITY_LOW, sequence=0, data={CONFIG: CMD_GET_SETTINGS}),
        )

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.client"):
            await client.dequeue_data()

        assert "without a connection active" in caplog.text
        assert len(client._queue) == 1

    async def test_dequeue_blocked_while_receipt_outstanding(self, mock_client, caplog):
        """A pending receipt blocks dequeuing the next message."""
        client, transport, _ = mock_client
        client._can_dequeue = False
        client._check_receipt = asyncio.get_running_loop().create_future()
        heapq.heappush(
            client._queue,
            PrioritizedMessage(priority=PRIORITY_LOW, sequence=0, data={CONFIG: CMD_GET_SETTINGS}),
        )

        with caplog.at_level(logging.WARNING):
            await client.dequeue_data()

        assert "another message is still outstanding" in caplog.text
        assert len(client._queue) == 1
        assert transport.written_data == []

    async def test_dequeue_ping_expects_pong(self, mock_client):
        """A dequeued PING sets the expected response command to PONG."""
        client, transport, _ = mock_client
        client._can_dequeue = False
        client.send_message(PING, "12345")

        await client.dequeue_data()

        assert client._last_command == PONG
        assert transport.get_last_message()[PING] == "12345"

    async def test_dequeue_unknown_message_type_warns_and_sends(self, mock_client, caplog):
        """A message with no COMMAND/CONFIG/PING key is sent with a warning."""
        client, transport, _ = mock_client
        client._can_dequeue = False
        heapq.heappush(
            client._queue,
            PrioritizedMessage(priority=PRIORITY_LOW, sequence=0, data={"bogus": 1}),
        )

        with caplog.at_level(logging.WARNING):
            await client.dequeue_data()

        assert "Sending unknown command type" in caplog.text
        assert client._last_command is None
        assert transport.get_last_message() == {"bogus": 1}


# ============================================================================
# Process Message Dequeue-Gate Tests
# ============================================================================


class TestProcessMessageGates:
    """The ack/dequeue gate logic and no-future logging paths."""

    async def test_matching_cmd_without_receipt_or_gate_defers(self, mock_client):
        """cmd match with no receipt and a closed gate must not dequeue."""
        client, _, _ = mock_client
        calls = []

        async def fake_dequeue():
            calls.append("dequeue")

        client.dequeue_data = fake_dequeue
        client._last_command = CMD_GET_DOOR_STATUS
        client._check_receipt = None
        client._can_dequeue = False

        await client.process_message(
            {"success": "true", "CMD": CMD_GET_DOOR_STATUS, "door_status": "DOOR_CLOSED"}
        )

        assert calls == []

    async def test_handler_exception_without_future_only_logs(
        self, mock_client, caplog, monkeypatch
    ):
        """A handler crash with no paired future is logged, never raised."""
        from powerpetdoor.client import ResponseHandlerRegistry

        client, _, _ = mock_client
        client._can_dequeue = False

        def boom(self, msg, future):
            raise RuntimeError("handler exploded")

        monkeypatch.setitem(ResponseHandlerRegistry._handlers, CMD_GET_SETTINGS, boom)

        with caplog.at_level(logging.ERROR):
            await client.process_message(
                {"success": "true", "CMD": CMD_GET_SETTINGS, FIELD_SETTINGS: {}}
            )

        assert "Error handling GET_SETTINGS response" in caplog.text

    async def test_failure_response_without_future_only_logs(self, mock_client, caplog):
        """A device failure with no paired future is logged, never raised."""
        client, _, _ = mock_client
        client._can_dequeue = False

        with caplog.at_level(logging.WARNING):
            await client.process_message({"success": "false", "CMD": CMD_OPEN, "reason": "locked"})

        assert "Error reported by device" in caplog.text


# ============================================================================
# Lifecycle Handler Dispatch Tests
# ============================================================================


class TestHandlerDispatch:
    """_dispatch_handler isolates sync handler exceptions."""

    async def test_sync_handler_exception_logged_not_raised(self, mock_client, caplog):
        """A raising sync on_disconnect handler does not block the next one."""
        client, _, _ = mock_client
        good = asyncio.Event()

        def bad_handler():
            raise RuntimeError("handler bug")

        client.add_handlers("bad", on_disconnect=bad_handler)
        client.add_handlers("good", on_disconnect=good.set)

        with caplog.at_level(logging.ERROR):
            client.disconnect()

        assert "Connection handler 'bad' raised" in caplog.text
        assert good.is_set()


# ============================================================================
# Thread-Safety and Private-Loop Lifecycle Tests
# ============================================================================


class TestThreadsafeLifecycle:
    """run_coroutine_threadsafe guards and the blocking start() path."""

    def _make_client(self):
        return PowerPetDoorClient(
            host="127.0.0.1", port=3000, keepalive=0, timeout=1.0, reconnect=1.0
        )

    def test_run_coroutine_threadsafe_requires_started_client(self):
        """Submitting before the client has a loop raises a clear error."""
        client = self._make_client()

        async def coro():
            pass  # Never scheduled - submission must fail first

        pending = coro()
        with pytest.raises(RuntimeError, match="requires the client to be started first"):
            client.run_coroutine_threadsafe(pending)
        pending.close()

    def test_stop_before_start_is_noop(self):
        """stop() with no loop configured just records the shutdown."""
        client = self._make_client()

        client.stop()

        assert client._shutdown is True

    def test_disconnect_outside_loop_cancels_reconnect_task(self):
        """disconnect() from a non-loop thread still cancels the reconnect."""
        from types import SimpleNamespace

        client = self._make_client()
        cancelled = []
        client._reconnect_task = SimpleNamespace(cancel=lambda: cancelled.append(True))

        client.disconnect()

        assert cancelled == [True]
        assert client._reconnect_task is None

    def test_start_creates_private_loop_and_stop_ends_it(self):
        """The blocking start() path runs a private loop until stop()."""
        client = self._make_client()
        started = threading.Event()

        async def fake_connect():
            started.set()

        client.connect = fake_connect

        thread = threading.Thread(target=client.start, daemon=True)
        thread.start()
        try:
            assert started.wait(5.0)
            assert client._ownLoop is True

            # Thread-safe submission runs on the private loop.
            async def get_loop():
                return asyncio.get_running_loop()

            result = client.run_coroutine_threadsafe(get_loop()).result(timeout=5.0)
            assert result is client._eventLoop
        finally:
            client.stop()
            thread.join(5.0)

        assert not thread.is_alive()
        assert client._eventLoop.is_closed()


# ============================================================================
# Concurrency Tests
# ============================================================================


class TestConcurrency:
    """Parallel commands, interleaved responses, disconnect mid-flight."""

    @staticmethod
    async def _next_written(transport, already: int) -> dict:
        """Wait (bounded, event-loop driven) for the next written message."""
        async with asyncio.timeout(5.0):
            while len(transport.written_data) <= already:
                await asyncio.sleep(0)
        return json.loads(transport.written_data[already].decode("ascii"))

    async def test_parallel_commands_resolve_their_own_futures(self, mock_client):
        """Three concurrent notify commands each get their own response."""
        client, transport, device = mock_client
        payloads = {
            CMD_GET_SETTINGS: {"settings": {"power_state": "1"}},
            CMD_GET_DOOR_STATUS: {"door_status": "DOOR_CLOSED"},
            CMD_GET_HOLD_TIME: {"holdTime": 500},
        }
        futures = {cmd: client.send_message(CONFIG, cmd, notify=True) for cmd in payloads}

        async def serve():
            for i in range(len(payloads)):
                msg = await self._next_written(transport, i)
                cmd = msg[CONFIG]
                device.respond_success(msg["msgId"], cmd, **payloads[cmd])

        await asyncio.gather(serve(), *futures.values())

        assert futures[CMD_GET_SETTINGS].result() == {"power_state": "1"}
        assert futures[CMD_GET_DOOR_STATUS].result() == "DOOR_CLOSED"
        assert futures[CMD_GET_HOLD_TIME].result() == 500

    async def test_out_of_order_responses_resolve_correct_futures(self, mock_client):
        """Responses arriving out of order resolve exactly their own futures."""
        client, _, _ = mock_client
        client._can_dequeue = False  # Keep both commands queued (unsent)
        settings_id = client.msgId
        settings_future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
        status_id = client.msgId
        status_future = client.send_message(CONFIG, CMD_GET_DOOR_STATUS, notify=True)

        # The device answers the *second* command first.
        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_DOOR_STATUS,
                "msgID": status_id,
                "door_status": "DOOR_HOLDING",
            }
        )
        assert status_future.result() == "DOOR_HOLDING"
        assert not settings_future.done()

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_SETTINGS,
                "msgID": settings_id,
                "settings": {"power_state": "0"},
            }
        )
        assert settings_future.result() == {"power_state": "0"}

    async def test_disconnect_during_inflight_fails_future_typed(self, mock_client):
        """Disconnect while a command is on the wire fails it with ConnectionError."""
        client, transport, _ = mock_client
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
        await self._next_written(transport, 0)  # Message is on the wire
        assert client._check_receipt is not None

        client.disconnect()

        with pytest.raises(ConnectionError):
            await future
        assert client._check_receipt is None

    async def test_queued_while_disconnected_flush_in_priority_order(
        self, disconnected_client, mock_transport
    ):
        """Commands queued while offline flush by priority once connected."""
        client = disconnected_client
        client.send_message(CONFIG, CMD_GET_SETTINGS)  # PRIORITY_LOW, enqueued first
        client.send_message(COMMAND, CMD_OPEN)  # PRIORITY_HIGH
        client.send_message(PING, "12345")  # PRIORITY_CRITICAL
        assert len(client._queue) == 3

        client.connection_made(mock_transport)
        try:
            for i in range(3):
                msg = await self._next_written(mock_transport, i)
                if PING in msg:
                    reply = {"success": "true", "CMD": PONG, PONG: msg[PING]}
                elif COMMAND in msg:
                    reply = {"success": "true", "CMD": msg[COMMAND], "door_status": "DOOR_RISING"}
                else:
                    reply = {"success": "true", "CMD": msg[CONFIG], "settings": {}}
                client.data_received(json.dumps(reply).encode("ascii"))

            order = []
            for msg in mock_transport.get_written_messages():
                order.append(PING if PING in msg else msg.get(COMMAND) or msg.get(CONFIG))
            assert order == [PING, CMD_OPEN, CMD_GET_SETTINGS]
        finally:
            client.disconnect()


# ============================================================================
# Connect Idempotence Tests
# ============================================================================


class TestTransportIdentity:
    """A transport the client did not adopt must never drive its state.

    `PowerPetDoorClient` is itself an `asyncio.Protocol`, so attaching it
    to every socket means one object receives the lifecycle events of every
    socket it was ever attached to, with no way to tell them apart.
    `connect()` therefore wires a `_ConnectionAttempt` shim per attempt,
    which knows its own transport.
    """

    async def test_a_connection_completing_after_shutdown_is_aborted(self, disconnected_client):
        """shutdown() landed while create_connection() was still in flight.

        disconnect() has already run, so nothing holds a reference that
        would ever close this socket - a "shut down" client would sit
        keepalive-pinging the device forever.
        """
        from powerpetdoor.client import _ConnectionAttempt
        from tests.conftest import MockTransport

        client = disconnected_client
        client.shutdown()
        attempt = _ConnectionAttempt(client)
        refused = MockTransport()

        attempt.connection_made(refused)

        assert refused.aborted is True  # abort(), not close()
        assert client._transport is None

    async def test_a_declined_transport_forwards_nothing(self, mock_client):
        """Neither its bytes nor its loss may reach the live connection."""
        from powerpetdoor.client import _ConnectionAttempt
        from tests.conftest import MockTransport

        client, transport, _ = mock_client
        attempt = _ConnectionAttempt(client)
        attempt.connection_made(MockTransport())  # declined: already connected

        before = set(client._tasks)  # the keepalive task is tracked too
        attempt.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "1"}')
        attempt.connection_lost(None)

        assert client._tasks == before  # no message processing was scheduled
        assert client._transport is transport
        assert client.available is True

    async def test_an_adopted_transports_bytes_reach_the_client(self, disconnected_client):
        from powerpetdoor.client import _ConnectionAttempt
        from tests.conftest import MockTransport

        client = disconnected_client
        attempt = _ConnectionAttempt(client)
        transport = MockTransport()
        attempt.connection_made(transport)

        assert client._transport is transport
        before = set(client._tasks)
        attempt.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "1"}')

        assert len([task for task in client._tasks if task not in before]) == 1
        client.disconnect()

    async def test_a_superseded_transports_loss_is_ignored(self, disconnected_client, caplog):
        """disconnect() clears `_transport` at once, but asyncio delivers that
        socket's `connection_lost` a loop iteration later. If the caller
        reconnected meanwhile, forwarding it would close the healthy
        transport and burn a reconnect against a one-slot device.
        """
        from powerpetdoor.client import _ConnectionAttempt
        from tests.conftest import MockTransport

        client = disconnected_client
        first = _ConnectionAttempt(client)
        first.connection_made(MockTransport())
        client.disconnect()
        second = _ConnectionAttempt(client)
        new_transport = MockTransport()
        second.connection_made(new_transport)

        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.client"):
            first.connection_lost(None)  # the stale event finally lands

        assert client._transport is new_transport
        assert client.available is True
        assert client._reconnect_task is None
        assert "The server closed the connection" not in caplog.text
        assert "superseded transport" in caplog.text
        client.disconnect()

    async def test_a_local_drop_after_shutdown_does_not_reconnect(self, mock_client):
        """A failure path that fires after shutdown() must stay down."""
        client, _, _ = mock_client
        client._shutdown = True

        client._drop_connection()

        assert client._transport is None
        assert client._reconnect_task is None


class TestConnectIdempotence:
    """connect() must never open a second socket to the one-slot device."""

    @staticmethod
    async def _echo_server() -> tuple[asyncio.Server, int, list, asyncio.Event]:
        """Start a server that records every accepted connection."""
        accepted: list[asyncio.StreamWriter] = []
        first_accepted = asyncio.Event()
        first_closed = asyncio.Event()

        async def handle(reader, writer):
            accepted.append(writer)
            first_accepted.set()
            try:
                await reader.read()
            finally:
                first_closed.set()
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return server, server.sockets[0].getsockname()[1], accepted, first_accepted, first_closed

    async def test_second_connect_is_a_no_op_and_disconnect_kills_everything(self):
        """A second connect() reuses the live connection; disconnect ends it."""
        server, port, accepted, first_accepted, first_closed = await self._echo_server()
        client = PowerPetDoorClient(
            host="127.0.0.1",
            port=port,
            keepalive=30.0,
            timeout=2.0,
            reconnect=0.05,
            loop=asyncio.get_running_loop(),
        )
        try:
            await client.connect()
            async with asyncio.timeout(2.0):
                await first_accepted.wait()

            transport = client._transport
            keepalive = client._keepalive
            assert transport is not None
            assert keepalive is not None

            await client.connect()  # defensive re-connect

            assert client._transport is transport  # same socket
            assert client._keepalive is keepalive  # no orphaned keepalive
            assert len(accepted) == 1  # device sees one connection

            client.shutdown()

            async with asyncio.timeout(2.0):
                await first_closed.wait()
            assert client._transport is None
            assert transport.is_closing()
            await asyncio.gather(keepalive, return_exceptions=True)
            assert keepalive.cancelled()
        finally:
            client.shutdown()
            # Close every accepted peer explicitly: a leaked client socket
            # would otherwise keep a handler (and wait_closed) alive forever.
            for writer in accepted:
                writer.close()
            server.close()
            await server.wait_closed()

    async def test_connect_while_an_attempt_is_in_flight_is_a_no_op(self):
        """Two concurrent connect() calls still produce one connection."""
        server, port, accepted, first_accepted, _ = await self._echo_server()
        client = PowerPetDoorClient(
            host="127.0.0.1",
            port=port,
            keepalive=0,
            timeout=2.0,
            reconnect=0.05,
            loop=asyncio.get_running_loop(),
        )
        try:
            await asyncio.gather(client.connect(), client.connect())
            async with asyncio.timeout(2.0):
                await first_accepted.wait()

            assert client.available is True
            assert len(accepted) == 1
            assert client._connecting is False
        finally:
            client.shutdown()
            for writer in accepted:
                writer.close()
            server.close()
            await server.wait_closed()

    def test_connection_made_rejects_a_second_transport(self, mock_client, caplog):
        """Belt and braces: a second transport is aborted, not installed."""
        from tests.conftest import MockTransport

        client, transport, _ = mock_client
        intruder = MockTransport()

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.client"):
            client.connection_made(intruder)

        assert client._transport is transport
        assert intruder.aborted is True
        assert transport.is_closing() is False
        assert "Rejecting a second connection" in caplog.text


# ============================================================================
# connect() error funnelling
# ============================================================================


class TestConnectErrorFunnel:
    """Every connect failure funnels through handle_connect_failure()."""

    @pytest.mark.parametrize(
        ("host", "port", "expected"),
        [
            # loop.create_connection() does not raise OSError for these:
            ("a" * 64 + ".example", 3000, UnicodeEncodeError),
            ("127.0.0.1", 99999, OverflowError),
        ],
    )
    async def test_bad_host_or_port_logs_and_schedules_a_reconnect(
        self, host, port, expected, caplog
    ):
        """A ValueError-family failure must not kill the client silently."""
        client = PowerPetDoorClient(
            host=host,
            port=port,
            keepalive=0,
            timeout=2.0,
            reconnect=30.0,
            loop=asyncio.get_running_loop(),
        )
        try:
            # Pin the premise: this really is not an OSError.
            with pytest.raises(expected):
                await asyncio.get_running_loop().create_connection(lambda: client, host, port)

            with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
                await client.connect()  # must not raise

            assert f"Unable to connect to Power Pet Door at {host}:{port}" in caplog.text
            assert client._reconnect_task is not None
            assert client._connecting is False
        finally:
            client.shutdown()

    async def test_door_connect_reports_a_bad_port_as_connection_error(self):
        """door.connect() promises ConnectionError, not OverflowError."""
        from powerpetdoor.door import PowerPetDoor

        door = PowerPetDoor("127.0.0.1", port=99999, keepalive=0, timeout=0.2, reconnect=30.0)
        try:
            with pytest.raises(ConnectionError):
                await door.connect()
        finally:
            await door.disconnect()


# ============================================================================
# aclose(): async lifecycle handler teardown
# ============================================================================


class TestAclose:
    """shutdown() leaves async handlers running; aclose() is the clean exit."""

    async def test_aclose_without_handlers_is_a_no_op(self, mock_client):
        client, _, _ = mock_client
        await client.aclose()
        assert client._shutdown is True
        assert client._handler_tasks == set()

    async def test_aclose_awaits_an_async_disconnect_handler(self, mock_client):
        finished: list[str] = []

        async def slow_disconnect():
            await asyncio.sleep(0)
            finished.append("done")

        client, _, _ = mock_client
        client.add_handlers("app", on_disconnect=slow_disconnect)

        await client.aclose()

        assert finished == ["done"]
        assert client._handler_tasks == set()

    async def test_aclose_cancels_a_handler_that_overruns(self, mock_client, caplog):
        started = asyncio.Event()

        async def wedged_disconnect():
            started.set()
            await asyncio.sleep(3600)

        client, _, _ = mock_client
        client.add_handlers("app", on_disconnect=wedged_disconnect)

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.client"):
            await client.aclose(timeout=0.01)

        assert started.is_set()
        assert "did not finish in time" in caplog.text
        assert client._handler_tasks == set()

    async def test_aclose_honours_its_timeout_argument(self, mock_client):
        """cfg_timeout is 5.0 here; a 0.01 s aclose must return well inside it.

        Ignoring the argument and always using cfg_timeout used to survive
        every test, because the only caller that passes one also happened to
        be cancelled by the longer wait.
        """
        client, _, _ = mock_client
        assert client.cfg_timeout >= 1.0  # pin the premise the test relies on
        client.add_handlers("app", on_disconnect=lambda: asyncio.sleep(3600))

        async with asyncio.timeout(client.cfg_timeout / 2):
            await client.aclose(timeout=0.01)

        assert client._handler_tasks == set()

    async def test_aclose_cancelled_mid_wait_still_cancels_the_handlers(self, mock_client):
        """The one guarantee aclose() exists to make survives its own cancel.

        asyncio.wait re-raises CancelledError, so a cancel step after it
        never ran and every outstanding handler was left running,
        un-awaited and un-cancelled.
        """
        started = asyncio.Event()
        outcome: list[str] = []

        async def wedged_disconnect():
            started.set()
            try:
                await asyncio.sleep(3600)
                outcome.append("finished")  # pragma: no cover (never reached)
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

        client, _, _ = mock_client
        client.add_handlers("app", on_disconnect=wedged_disconnect)

        handler_tasks = None
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                closing = asyncio.ensure_future(client.aclose(timeout=30.0))
                await started.wait()
                handler_tasks = set(client._handler_tasks)
                await closing

        # The outer timeout cancelled aclose() mid-wait; the handler must
        # still have been cancelled rather than left running.
        await asyncio.gather(*handler_tasks, return_exceptions=True)
        assert outcome == ["cancelled"]
        assert all(task.done() for task in handler_tasks)

    async def test_aclose_cancelled_mid_wait_skips_handlers_that_finished(self, mock_client):
        """Only the still-running handlers are cancelled on the way out."""
        started = asyncio.Event()
        outcome: list[str] = []

        async def quick_disconnect():
            outcome.append("finished")

        async def wedged_disconnect():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

        client, _, _ = mock_client
        client.add_handlers("quick", on_disconnect=quick_disconnect)
        client.add_handlers("wedged", on_disconnect=wedged_disconnect)

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                closing = asyncio.ensure_future(client.aclose(timeout=30.0))
                await started.wait()
                await closing

        assert outcome == ["finished", "cancelled"]

    async def test_aclose_from_inside_a_handler_does_not_wait_for_itself(self, mock_client):
        """The 'don't await yourself' filter, otherwise a self-deadlock."""
        client, _, _ = mock_client
        completed = asyncio.Event()

        async def closing_disconnect():
            # Without the `task is not current` filter this waits out the
            # full timeout on its own task before returning.
            await client.aclose(timeout=30.0)
            completed.set()

        client.add_handlers("app", on_disconnect=closing_disconnect)

        async with asyncio.timeout(5.0):
            client.disconnect()
            await completed.wait()


# ============================================================================
# Background Task Tracking Tests
# ============================================================================


class TestBackgroundTaskTracking:
    """Fire-and-forget work is tracked, logged, and torn down."""

    async def test_message_processing_is_tracked(self, mock_client):
        """data_received schedules processing into the tracked set."""
        client, _, _ = mock_client
        # The keepalive task is tracked too, so select the one under test.
        before = set(client._tasks)

        client.data_received(b'{"success": "true", "CMD": "PONG", "PONG": "1"}')

        tasks = [task for task in client._tasks if task not in before]
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        assert client._tasks == before

    async def test_failing_task_is_logged_immediately(self, mock_client, caplog):
        """An escaping exception is reported by the done-callback, not at GC."""
        client, _, _ = mock_client
        before = set(client._tasks)

        async def boom():
            raise RuntimeError("kaboom")

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            task = client._track_task(boom())
            await asyncio.gather(task, return_exceptions=True)

        assert "Background client task failed" in caplog.text
        assert client._tasks == before

    async def test_keepalive_and_check_receipt_are_tracked(self, mock_client):
        """Both fire-and-forget timers go through _track_task.

        Held in their own attributes *and* in ``_tasks``: an exception
        escaping either was previously only reported by asyncio's
        "Task exception was never retrieved" hook at GC time, which is the
        exact failure mode ``_track_task`` exists to prevent.
        """
        client, _, _ = mock_client

        assert client._keepalive in client._tasks

        client.send_message(CONFIG, CMD_GET_SETTINGS)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert client._check_receipt is not None
        assert client._check_receipt in client._tasks

    async def test_start_tracks_the_connect_task(self, disconnected_client, monkeypatch):
        """start()'s connect() is tracked, not a bare ensure_future.

        Tracking is what makes an exception escaping connect() get logged
        immediately rather than at GC time, and what lets disconnect()
        cancel an attempt still in flight.
        """
        client = disconnected_client
        entered = asyncio.Event()

        async def slow_connect():
            entered.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(client, "connect", slow_connect)
        client.start()
        async with asyncio.timeout(2.0):
            await entered.wait()

        tracked = list(client._tasks)
        assert len(tracked) == 1

        client.disconnect()
        await asyncio.gather(*tracked, return_exceptions=True)
        assert tracked[0].cancelled()
        assert client._tasks == set()

    async def test_scheduled_reconnect_is_tracked(self, disconnected_client):
        """_schedule_reconnect()'s task is tracked as well."""
        client = disconnected_client
        client.cfg_reconnect = 60

        client._schedule_reconnect()

        task = client._reconnect_task
        assert task is not None
        assert task in client._tasks

        client.disconnect()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert client._tasks == set()

    async def test_disconnect_cancels_in_flight_processing(self, mock_client):
        """Connection-scoped work is cancelled when the connection drops."""
        client, _, _ = mock_client
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(60)

        task = client._track_task(slow())
        async with asyncio.timeout(2.0):
            await started.wait()

        client.disconnect()

        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert client._tasks == set()

    async def test_disconnect_from_inside_a_tracked_task_does_not_self_cancel(self, mock_client):
        """disconnect() is reachable from a tracked task (a failed write)."""
        client, _, _ = mock_client
        finished = []

        async def worker():
            client.disconnect()
            finished.append("ran to completion")

        task = client._track_task(worker())
        await task

        assert finished == ["ran to completion"]
        assert task.cancelled() is False

    async def test_async_lifecycle_handlers_survive_disconnect(self, mock_client):
        """on_disconnect coroutines are not cancelled by the disconnect."""
        client, _, _ = mock_client
        ran = asyncio.Event()

        async def on_disconnect():
            await asyncio.sleep(0)
            ran.set()

        client.add_handlers("late", on_disconnect=on_disconnect)
        client.disconnect()
        client.disconnect()  # a second teardown must not kill the first handler

        async with asyncio.timeout(2.0):
            await ran.wait()
        await asyncio.sleep(0)  # let the done-callback untrack the task
        assert client._handler_tasks == set()


class TestDecodeFailuresThatAreNotJSONDecodeError:
    """`data_received` "never raises on arbitrary input" - it used to.

    `json.JSONDecodeError` is a ValueError *subclass*, not a superset of
    what `json.loads` raises. A >4300-digit integer literal raises a bare
    ValueError and deep nesting raises RecursionError, and both escaped the
    decode. Landing inside `data_received` that fatal-errors the transport,
    and because `_adopt_transport` resets `_reconnect_attempts` on every
    successful connect, the client then sat in a hot reconnect loop at the
    base interval forever.

    Both frames are brace-balanced and under `MAX_BUFFER_SIZE`, so the
    framing cap is provably not what stops them.
    """

    @pytest.mark.parametrize("frame", [bigint_frame(), nested_frame()], ids=["bigint", "nested"])
    async def test_a_poisoned_frame_alone_does_not_escape_data_received(
        self, frame, mock_client, caplog
    ):
        client, transport, _ = mock_client
        assert len(frame) < framing.MAX_BUFFER_SIZE

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            client.data_received(frame)

        assert transport.is_closing() is False
        assert "Failed to decode JSON frame" in caplog.text

    async def test_deep_nesting_split_across_reads_is_not_caught_by_the_framing_cap(
        self, mock_client, caplog
    ):
        """Delivered in network-sized pieces, so only the decoder can stop it."""
        client, _, _ = mock_client
        frame = nested_frame()

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            for start in range(0, len(frame), 1400):
                client.data_received(frame[start : start + 1400])

        assert "Failed to decode JSON frame" in caplog.text

    @pytest.mark.parametrize(
        ("frame", "detail"),
        [
            (bigint_frame(), "Exceeds the limit (4300 digits)"),
            (nested_frame(), "maximum recursion depth exceeded"),
        ],
        ids=["bigint", "nested"],
    )
    async def test_a_poisoned_frame_lands_on_the_existing_decode_path(
        self, frame, detail, mock_client, caplog
    ):
        """Byte-identical treatment to `{x}`: the same log call site."""
        with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
            control, _, _ = mock_client
            control.data_received(b"{x}")
            # `record.msg` is the format string, i.e. the call site - stable
            # across the differing detail text.
            control_shape = [record.msg for record in caplog.records]
            caplog.clear()

            control.data_received(frame)

        assert [record.msg for record in caplog.records] == control_shape
        assert detail in caplog.records[0].getMessage()

    async def test_a_legitimate_frame_after_a_poisoned_one_is_still_handled(self, mock_client):
        client, _, _ = mock_client
        seen: list[dict] = []
        client.process_message = lambda msg, **_kwargs: _record(seen, msg)

        client.data_received(bigint_frame() + nested_frame() + b'{"CMD":"AFTER"}')
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0)

        assert seen == [{"CMD": "AFTER"}]


async def _record(seen: list[dict], msg: dict) -> None:
    seen.append(msg)


class TestHardwareInfoPayload:
    """`fwInfo` is the one payload sub-object whose value is *cached*."""

    async def test_a_mapping_payload_reaches_the_listeners(self, mock_client):
        client, _, device = mock_client
        seen: list[dict] = []
        client.add_listener("t", hw_info_update=seen.append)

        device.respond_success(1, "GET_HW_INFO", fwInfo={"ver": "1", "fw_maj": 2})
        await asyncio.sleep(0)

        assert seen == [{"ver": "1", "fw_maj": 2}]

    async def test_a_scalar_payload_is_not_handed_to_dict_typed_listeners(
        self, mock_client, caplog
    ):
        """`hw_info_update` is declared Callable[[dict], None].

        Handing it a string made `_notify_listeners` swallow the resulting
        AttributeError with nothing tying it to the frame that caused it,
        and `PowerPetDoor` then cached the scalar - poisoning three
        documented public properties until the next well-formed reply.
        """
        client, _, device = mock_client
        seen: list[object] = []
        client.add_listener("t", hw_info_update=seen.append)

        with caplog.at_level(logging.WARNING, logger="powerpetdoor.client"):
            device.respond_success(1, "GET_HW_INFO", fwInfo="1.2.3")
            await asyncio.sleep(0)

        assert seen == []
        assert [record.getMessage() for record in caplog.records] == [
            "Device sent a non-mapping fwInfo payload; not notifying hw_info listeners: 1.2.3",
        ]

    async def test_a_scalar_payload_still_resolves_the_caller_future(self, mock_client):
        """Liberal in what we accept: send_message() sees what was sent."""
        client, transport, device = mock_client
        future = client.send_message("config", "GET_HW_INFO", notify=True)
        await asyncio.sleep(0)
        msg_id = transport.get_last_message()["msgId"]

        device.respond_success(msg_id, "GET_HW_INFO", fwInfo="1.2.3")

        assert await asyncio.wait_for(future, 1.0) == "1.2.3"

    async def test_an_absent_payload_fails_the_future_typed(self, mock_client):
        client, transport, device = mock_client
        future = client.send_message("config", "GET_HW_INFO", notify=True)
        await asyncio.sleep(0)
        msg_id = transport.get_last_message()["msgId"]

        device.respond_success(msg_id, "GET_HW_INFO")

        with pytest.raises(CommandError, match="Response missing expected field"):
            await asyncio.wait_for(future, 1.0)


class TestAbsentSettingsFieldsAreGuarded:
    """The "may be absent on some firmware variants" guards, with it absent.

    `client.py:796-797` says outright: *"these fields may be absent on some
    firmware variants, so guard each one (never assume presence)"*. No test
    combined **a registered listener** with **a payload omitting that
    listener's field**, so flipping `listeners and field_name in settings`
    to `or` survived the whole suite - and with `or` the code indexes
    `settings[field_name]` and raises `KeyError` into `_LOGGER.exception`:
    a full traceback per frame.
    """

    SENSOR_FIELDS = [
        FIELD_POWER,
        FIELD_INSIDE,
        FIELD_OUTSIDE,
        FIELD_AUTO,
        FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
        FIELD_CMD_LOCKOUT,
        FIELD_AUTORETRACT,
    ]

    @pytest.mark.parametrize("field_name", SENSOR_FIELDS)
    async def test_a_registered_sensor_listener_is_not_called_for_an_absent_field(
        self, mock_client, caplog, field_name
    ):
        client, _, _ = mock_client
        client._can_dequeue = False
        calls: list[tuple] = []
        client.add_listener(
            "probe", sensor_update={field_name: lambda field, value: calls.append((field, value))}
        )

        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.client"):
            # Every *other* sensor field present; this one omitted.
            settings = {other: "1" for other in self.SENSOR_FIELDS if other != field_name}
            await client.process_message(
                {"success": "true", "CMD": CMD_GET_SETTINGS, "settings": settings}
            )

        assert calls == []
        assert "Traceback" not in caplog.text
        assert [record for record in caplog.records if record.exc_info] == []

    @pytest.mark.parametrize("field_name", SENSOR_FIELDS)
    async def test_the_same_listener_is_called_when_the_field_is_present(
        self, mock_client, field_name
    ):
        """The control: the guard must not be refusing everything.

        ``doorOptions`` carries the ON value that its own bitfield uses,
        because it is not a flag - see
        :func:`~powerpetdoor.client.autoretract_from_door_options`.
        """
        client, _, _ = mock_client
        client._can_dequeue = False
        calls: list[tuple] = []
        client.add_listener(
            "probe", sensor_update={field_name: lambda field, value: calls.append((field, value))}
        )
        on = DOOR_OPTION_AUTORETRACT if field_name == FIELD_AUTORETRACT else "1"

        await client.process_message(
            {"success": "true", "CMD": CMD_GET_SETTINGS, "settings": {field_name: on}}
        )

        assert calls == [(field_name, True)]

    @pytest.mark.parametrize(
        ("kwarg", "field_name"),
        [
            ("timezone_update", FIELD_TZ),
            ("hold_time_update", FIELD_HOLD_OPEN_TIME),
            ("sensor_trigger_voltage_update", FIELD_SENSOR_TRIGGER_VOLTAGE),
            ("sleep_sensor_trigger_voltage_update", FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE),
        ],
    )
    async def test_scalar_listeners_are_not_called_for_an_absent_field(
        self, mock_client, caplog, kwarg, field_name
    ):
        client, _, _ = mock_client
        client._can_dequeue = False
        calls: list = []
        client.add_listener("probe", **{kwarg: calls.append})

        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.client"):
            await client.process_message(
                {"success": "true", "CMD": CMD_GET_SETTINGS, "settings": {FIELD_POWER: "1"}}
            )
        assert calls == []

        # Control: present -> the listener does fire.
        await client.process_message(
            {"success": "true", "CMD": CMD_GET_SETTINGS, "settings": {field_name: 7}}
        )
        assert calls == [7]
        assert [record for record in caplog.records if record.exc_info] == []

    @pytest.mark.parametrize(
        ("field_name", "listener_field"),
        [
            (FIELD_TOTAL_OPEN_CYCLES, FIELD_TOTAL_AUTO_RETRACTS),
            (FIELD_TOTAL_AUTO_RETRACTS, FIELD_TOTAL_OPEN_CYCLES),
        ],
    )
    async def test_stats_listeners_are_not_called_for_an_absent_field(
        self, mock_client, caplog, field_name, listener_field
    ):
        """Same shape, stats path (`client.py:843`)."""
        client, _, _ = mock_client
        client._can_dequeue = False
        calls: list[tuple] = []
        client.add_listener(
            "probe",
            stats_update={listener_field: lambda field, value: calls.append((field, value))},
        )

        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.client"):
            await client.process_message(
                {"success": "true", "CMD": CMD_GET_DOOR_OPEN_STATS, field_name: 5}
            )
        assert calls == []

        # Control: present -> the listener does fire.
        await client.process_message(
            {"success": "true", "CMD": CMD_GET_DOOR_OPEN_STATS, listener_field: 9}
        )
        assert calls == [(listener_field, 9)]
        assert [record for record in caplog.records if record.exc_info] == []


# ============================================================================
# Wire-shape helpers - the message-level half of the parity rule
# ============================================================================


class TestEnvelopeForCommand:
    """Verified against firmware 1.7.18: the two envelope keys are not
    interchangeable, and only door motion is a `cmd`."""

    @pytest.mark.parametrize("cmd", [CMD_OPEN, CMD_OPEN_AND_HOLD, CMD_CLOSE])
    def test_door_motion_is_a_command(self, cmd):
        assert envelope_for_command(cmd) == COMMAND

    @pytest.mark.parametrize(
        "cmd",
        [
            CMD_ENABLE_INSIDE,
            CMD_DISABLE_INSIDE,
            CMD_POWER_ON,
            CMD_ENABLE_AUTORETRACT,
            CMD_GET_SETTINGS,
            CMD_SET_HOLD_TIME,
            CMD_SET_SCHEDULE,
            CMD_GET_TIME,
        ],
    )
    def test_everything_else_is_a_config(self, cmd):
        assert envelope_for_command(cmd) == CONFIG

    def test_an_unknown_name_is_a_config(self):
        """A hand-rolled command name gets the safe default, not `cmd`."""
        assert envelope_for_command("NO_SUCH_COMMAND") == CONFIG

    def test_the_set_and_the_helper_agree(self):
        """One source of truth, so the two cannot drift."""
        assert {
            cmd for cmd in COMMAND_ENVELOPE_COMMANDS if envelope_for_command(cmd) != COMMAND
        } == set()
        assert COMMAND_ENVELOPE_COMMANDS == frozenset({CMD_OPEN, CMD_OPEN_AND_HOLD, CMD_CLOSE})


class TestAutoretractFromDoorOptions:
    """`doorOptions` is an int BITFIELD; auto-retract is bit 1.

    Verified against firmware 1.7.18: DISABLE_AUTORETRACT leaves it 0 and
    ENABLE_AUTORETRACT leaves it 2. Truthiness gives the right answer on
    those two values by luck and the wrong one as soon as another bit is
    set, which is the whole reason this reader exists.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, False),
            (2, True),
            ("0", False),
            ("2", True),
            # Bit 0 alone is some other option: truthy, but NOT auto-retract.
            (1, False),
            (3, True),
            # Higher unidentified bits, without bit 1.
            (4, False),
            (6, True),
        ],
        ids=repr,
    )
    def test_the_bit_is_masked_not_tested_for_truth(self, value, expected):
        assert autoretract_from_door_options(value) is expected

    @pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
    def test_a_json_boolean_answers_the_question_directly(self, value, expected):
        """int(True) is 1, which masks to False - bools must short-circuit."""
        assert autoretract_from_door_options(value) is expected

    @pytest.mark.parametrize(
        ("value", "expected"), [("true", True), ("false", False), ("on", True), ("off", False)]
    )
    def test_a_flag_string_falls_back_to_make_bool(self, value, expected):
        """Liberal: a firmware answering `true` is answering directly."""
        assert autoretract_from_door_options(value) is expected

    @pytest.mark.parametrize(
        "value",
        [None, [], {}, "maybe", 2.5j],
        ids=["None", "list", "dict", "maybe", "complex"],
    )
    def test_an_unreadable_value_is_none_not_a_guess(self, value):
        assert autoretract_from_door_options(value) is None

    def test_an_overflowing_float_is_unreadable_rather_than_raising(self):
        """int(inf) raises OverflowError, not ValueError."""
        assert autoretract_from_door_options(float("inf")) is None


class TestBuildSetVoltageMessage:
    """Verified against firmware 1.7.18: the setters take `voltage`."""

    def test_it_uses_the_setters_field_not_the_getters(self):
        message = build_set_voltage_message(1500)

        assert message == {FIELD_VOLTAGE: 1500}
        assert FIELD_SENSOR_TRIGGER_VOLTAGE not in message

    def test_it_coerces_to_an_int(self):
        assert build_set_voltage_message(1500.0) == {FIELD_VOLTAGE: 1500}


class TestBuildSetHoldTimeMessage:
    """Seconds in, the device's centiseconds out."""

    @pytest.mark.parametrize(
        ("seconds", "centiseconds"), [(2.0, 200), (15.0, 1500), (0.01, 1), (0, 0)]
    )
    def test_seconds_become_centiseconds(self, seconds, centiseconds):
        assert build_set_hold_time_message(seconds) == {FIELD_HOLD_TIME: centiseconds}

    def test_a_fractional_centisecond_truncates_as_it_always_has(self):
        """`int()`, not `round()` - what every released version has sent."""
        assert build_set_hold_time_message(3.999) == {FIELD_HOLD_TIME: 399}


class TestBuildSetNotificationsMessage:
    """The nested object of JSON booleans, and nothing else."""

    def test_all_five_flags_are_nested_under_notifications(self):
        message = build_set_notifications_message(
            inside_on=True,
            inside_off=False,
            outside_on=True,
            outside_off=False,
            low_battery=True,
        )

        assert message == {
            FIELD_NOTIFICATIONS: {
                FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: True,
                FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: False,
                FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: True,
                FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: False,
                FIELD_LOW_BATTERY_NOTIFICATIONS: True,
            }
        }

    def test_the_values_are_json_booleans_not_ints_or_strings(self):
        """A nested object of strings is accepted by the door and IGNORED."""
        nested = build_set_notifications_message(
            inside_on=1,  # type: ignore[arg-type]
            inside_off=0,  # type: ignore[arg-type]
            outside_on="yes",  # type: ignore[arg-type]
            outside_off="",  # type: ignore[arg-type]
            low_battery=True,
        )[FIELD_NOTIFICATIONS]

        assert all(type(value) is bool for value in nested.values())

    def test_no_field_is_left_at_the_top_level(self):
        """Flat fields are rejected outright by the device."""
        message = build_set_notifications_message(
            inside_on=True,
            inside_off=True,
            outside_on=True,
            outside_off=True,
            low_battery=True,
        )

        assert set(message) == {FIELD_NOTIFICATIONS}


class TestTheDoorClockResponse:
    """`GET_TIME` is passed through verbatim - it is a string, not a clock."""

    async def test_the_asctime_string_reaches_the_listener_and_the_future(self, mock_client):
        client, _, _ = mock_client
        values: list = []
        client.add_listener("t", time_update=values.append)
        client._can_dequeue = False
        future = client._get_loop().create_future()
        client._outstanding[5] = future

        await client.process_message(
            {
                "success": "true",
                "CMD": CMD_GET_TIME,
                "msgID": 5,
                FIELD_TIME: "Sat Aug 22 23:13:48 2026",
            }
        )

        assert values == ["Sat Aug 22 23:13:48 2026"]
        assert future.result() == "Sat Aug 22 23:13:48 2026"

    async def test_a_reply_without_the_field_fails_the_future_typed(self, mock_client):
        client, _, _ = mock_client
        client._can_dequeue = False
        future = client._get_loop().create_future()
        client._outstanding[6] = future

        await client.process_message({"success": "true", "CMD": CMD_GET_TIME, "msgID": 6})

        with pytest.raises(CommandError):
            future.result()


class TestAnUnmatchedFailureDoesNotHangTheCaller:
    """Verified against firmware 1.7.18: failures carry no `msgID`.

    `process_message` therefore has to fall back to "this reply
    acknowledges the in-flight command", and the guard that does it is a
    compound condition - so the case where the *second* operand is the
    deciding one is pinned here too.
    """

    @staticmethod
    def _inflight(client, cmd):
        """Put `cmd` in flight with a future, as `send_message` would."""
        msg_id = client.msgId
        future = client._get_loop().create_future()
        client._outstanding[msg_id] = future
        client._inflight_msg_id = msg_id
        client._last_command = cmd
        client._check_receipt = client._get_loop().call_later(60, lambda: None)
        return future

    async def test_a_failure_with_no_id_fails_the_inflight_future(self, mock_client):
        client, _, _ = mock_client
        future = self._inflight(client, CMD_GET_SCHEDULE)

        await client.process_message(
            {"success": "false", "CMD": CMD_GET_SCHEDULE, "reason": "Schedule not found"}
        )

        assert future.done()
        with pytest.raises(CommandError) as err:
            future.result()
        assert err.value.reason == "Schedule not found"

    async def test_an_id_matched_failure_still_takes_the_ordinary_path(self, mock_client):
        """The control: the fallback is for *unmatched* failures only."""
        client, _, _ = mock_client
        msg_id = client.msgId
        future = client._get_loop().create_future()
        client._outstanding[msg_id] = future
        client._can_dequeue = False

        await client.process_message(
            {"success": "false", "CMD": CMD_GET_SCHEDULE, "msgID": msg_id, "reason": "nope"}
        )

        with pytest.raises(CommandError):
            future.result()

    async def test_a_failure_for_a_command_not_in_flight_is_left_alone(self, mock_client):
        """The second operand deciding: no id, and nothing was acknowledged.

        A stray failure for some *other* command must not fail whatever
        this client happens to be waiting on.
        """
        client, _, _ = mock_client
        future = self._inflight(client, CMD_GET_SCHEDULE)

        await client.process_message(
            {"success": "false", "CMD": CMD_GET_SETTINGS, "reason": "unrelated"}
        )

        assert not future.done()
        client._check_receipt.cancel()
