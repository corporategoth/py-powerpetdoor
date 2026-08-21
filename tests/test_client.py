# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for PowerPetDoorClient."""

from __future__ import annotations

import asyncio
import heapq
import time
from unittest.mock import AsyncMock, patch

import pytest

from powerpetdoor import (
    PowerPetDoorClient,
    PrioritizedMessage,
    find_end,
    make_bool,
)
from powerpetdoor.client import MAX_FAILED_MSG, MAX_FAILED_PINGS
from powerpetdoor.const import (
    CMD_CLOSE,
    CMD_GET_DOOR_STATUS,
    CMD_GET_SETTINGS,
    CMD_OPEN,
    COMMAND,
    CONFIG,
    FIELD_SUCCESS,
    PING,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
)

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
        the device escape data_received and poison the connection
        (security finding 4 / decision D1).
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
        like '{"a": "}"}' truncated mid-string (test-fanatic C5).
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
        """available is a real False when no transport, never None (L4)."""
        assert disconnected_client.available is False

    async def test_loopless_client_resolves_running_loop(self):
        """loop=None latches onto the running loop at use time (D5/C1)."""
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
        """del_listener() removes the name from every listener registry (C3)."""
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
        """del_listener() for a never-added name does not raise (C3)."""
        client, _, _ = mock_client
        client.add_listener("kept", door_status_update=make_callback("kept"))

        client.del_listener("never_added")

        assert "kept" in client.door_status_listeners

    def test_del_handlers_partial_registration_is_safe(self, mock_client, make_async_callback):
        """del_handlers() must not raise when only some handlers exist (M6)."""
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

        # Allow the event loop to process pending tasks
        await asyncio.sleep(0.01)

        assert "door_status" in callback_tracker["calls"]

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
    """Tests for the PING/PONG keepalive mechanism (real keepalive(), C2)."""

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
    """Tests for connection_lost and reconnect scheduling (H2/H5)."""

    def test_connection_lost_triggers_disconnect(self, mock_client):
        """connection_lost triggers disconnect cleanup."""
        client, _, _ = mock_client
        # Set shutdown to prevent reconnect task from being created
        client._shutdown = True

        client.connection_lost(None)

        assert client._transport is None

    async def test_connection_lost_schedules_tracked_reconnect(self, mock_client):
        """connection_lost creates a tracked reconnect task (H2)."""
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
        """Drop -> stop() during the reconnect delay -> no zombie reconnect (H2)."""
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
        """The public shutdown() also cancels a pending reconnect (M6/H2)."""
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
        """reconnect() checks the shutdown flag after its delay (H2)."""
        client, _, _ = mock_client

        with patch.object(client, "connect", new_callable=AsyncMock) as mock_connect:
            client._shutdown = True
            await client.reconnect(0)

            assert mock_connect.await_count == 0

    async def test_connect_is_noop_when_shutdown(self, disconnected_client):
        """connect() refuses to run once the client is shut down (H2)."""
        client = disconnected_client
        client._shutdown = True

        await client.connect()

        assert client._transport is None

    async def test_reset_shutdown_reenables_connect(self, disconnected_client):
        """reset_shutdown() clears the flag so connect() runs again (M6)."""
        client = disconnected_client
        client.shutdown()
        assert client._shutdown is True

        client.reset_shutdown()

        assert client._shutdown is False


class TestReconnectBehavior:
    """Reconnect against a real TCP server (H5)."""

    async def test_client_reconnects_after_server_restart(self, client_config):
        """The client automatically reconnects when the server comes back."""

        async def handle(reader, writer):
            pass  # Accept and hold the connection open

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

            # Kill the server and the client's connection.
            connected.clear()
            server.close()
            await server.wait_closed()
            client._transport.close()

            # Restart a server on the same port; the client must reconnect.
            server = await asyncio.start_server(handle, "127.0.0.1", port)
            async with asyncio.timeout(5.0):
                await connected.wait()
            assert client.available is True
        finally:
            client.shutdown()
            server.close()
            await server.wait_closed()

    async def test_connect_failure_schedules_retry(self, client_config, unused_tcp_port):
        """A refused connection schedules a backoff retry, not an exception."""
        client = PowerPetDoorClient(
            host="127.0.0.1",
            port=unused_tcp_port,
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
        """Backoff doubles per failed attempt, jittered, capped (L1)."""
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
        """connection_made resets the reconnect attempt counter (L1)."""
        client, transport, _ = mock_client
        client._reconnect_attempts = 7

        client.connection_made(transport)

        assert client._reconnect_attempts == 0


class TestDisconnectTransitions:
    """on_disconnect must fire only on real connected->disconnected (L2)."""

    async def test_on_disconnect_fires_after_real_connection(self, mock_client):
        """Disconnecting an established connection notifies handlers."""
        client, _, _ = mock_client
        disconnected = asyncio.Event()
        client.add_handlers("test", on_disconnect=disconnected.set)

        client.disconnect()

        async with asyncio.timeout(1.0):
            await disconnected.wait()

    async def test_on_disconnect_not_fired_when_never_connected(self, disconnected_client):
        """A failed connect attempt must not produce disconnect events (L2)."""
        client = disconnected_client
        events = []

        async def on_disconnect():
            events.append("disconnect")

        client.add_handlers("test", on_disconnect=on_disconnect)
        client.disconnect()
        await asyncio.sleep(0)  # Let any (wrongly) scheduled callbacks run

        assert events == []

    async def test_on_disconnect_not_fired_twice(self, mock_client):
        """A second disconnect() must not re-notify handlers (L2)."""
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
    """Messages enqueued while disconnected flush on reconnect (L3)."""

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

        # Allow the event loop to process pending tasks
        await asyncio.sleep(0.01)

        # Future should be resolved with the settings payload and untracked
        assert future.result() == {"power_state": True}
        assert msg_id not in client._outstanding

    async def test_disconnect_fails_outstanding_with_connection_error(self, mock_client):
        """Disconnect fails in-flight futures with ConnectionError, not cancel (M1/L9)."""
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
            # into the loop's exception handler (M1).
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
# check_receipt Retry Machinery Tests (H4)
# ============================================================================


class TestCheckReceipt:
    """Retry, drop, and success paths of the receipt checker (H4)."""

    async def test_dropped_message_future_raises_timeout(self, mock_client):
        """After MAX_FAILED_MSG timeouts the future fails with TimeoutError (H4)."""
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

    async def test_send_data_transport_gone_after_sleep(self, mock_client):
        """disconnect() during the rate-limit sleep is not fatal (M7)."""
        client, transport, _ = mock_client
        client._last_command = None
        client._last_send = time.monotonic()  # Forces the rate-limit sleep

        send_task = asyncio.ensure_future(client._send_data(b'{"config": "X"}'))
        await asyncio.sleep(0)  # Let _send_data reach its sleep
        client.disconnect()

        await send_task  # Must not raise AttributeError

        assert transport.written_data == []


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

    def _record(msg):
        received.append(msg)
        return _noop()

    client.process_message = _record
    client.ensure_future = lambda coro: coro.close()
    return received


class TestClientProtocolViolations:
    """The client must survive arbitrary bytes from a hostile/broken device."""

    def test_garbage_bytes_do_not_raise_and_are_discarded(self, mock_client):
        """Pure garbage input neither raises nor poisons the buffer (C4)."""
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
        """Non-ASCII data is discarded without raising."""
        client, _, _ = mock_client

        client.data_received(b"\xff\xfe")

        assert client._buffer == ""

    def test_brace_in_string_value_framed_correctly(self, mock_client):
        """A brace inside a JSON string value does not corrupt framing (C5)."""
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
        """Whitespace/newline separators between messages are tolerated (H3)."""
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
        """Exceeding the un-parsed buffer cap drops the connection (D1)."""
        from powerpetdoor.framing import MAX_BUFFER_SIZE

        client, transport, _ = mock_client

        client.data_received(b"{" * (MAX_BUFFER_SIZE + 1))

        assert client._buffer == ""
        assert client._transport is None
        assert transport.is_closing()

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

    async def test_message_missing_cmd_and_success_dropped(self, mock_client):
        """A JSON object with no CMD/success/notification is dropped quietly."""
        client, _, _ = mock_client

        await client.process_message({"mystery": 1})

    async def test_non_dict_message_dropped(self, mock_client):
        """A non-dict message is dropped quietly."""
        client, _, _ = mock_client

        await client.process_message("not a dict")

    async def test_message_missing_success_fails_future(self, mock_client):
        """A response without success fails the matched future (typed)."""
        from powerpetdoor.client import CommandError

        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        await client.process_message({"CMD": "GET_SETTINGS", "msgID": msg_id})

        assert isinstance(future.exception(), CommandError)

    async def test_failure_response_sets_command_error_with_reason(self, mock_client):
        """success:"false" fails the future with CommandError carrying cmd+reason (L10)."""
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

    async def test_handler_exception_fails_future(self, mock_client):
        """A handler crash on a malformed payload fails the future (D3/M3)."""
        from powerpetdoor.client import CommandError

        client, _, _ = mock_client
        client._can_dequeue = False
        msg_id = client.msgId
        future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

        # GET_SETTINGS response missing the settings payload entirely.
        await client.process_message({"success": "true", "CMD": "GET_SETTINGS", "msgID": msg_id})

        assert future.done()
        assert isinstance(future.exception(), CommandError)

    async def test_settings_missing_optional_fields_ok(
        self, mock_client, callback_tracker, make_callback
    ):
        """Settings without tz/holdOpenTime/voltages still process (M3)."""
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
        """One raising listener does not prevent the others running (D3)."""
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
        """Handlers never call set_result on a completed future (M11)."""
        client, _, _ = mock_client
        future = asyncio.get_running_loop().create_future()
        future.cancel()

        # Must not raise InvalidStateError.
        client._handle_door_status({"door_status": "DOOR_CLOSED"}, future)

    async def test_response_for_cancelled_future_ignored(self, mock_client):
        """A late response for a cancelled future is ignored, not fatal (M11)."""
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
        """The response handler runs before the next message dequeues (M11)."""
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
        """A handler that cannot resolve its future fails it typed, not cancel (L9)."""
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
# Notification Event Tests (D2)
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
        """CMD-style notification envelopes also dispatch (D2 tolerance)."""
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
# Timing Source Tests (L11)
# ============================================================================


class TestMonotonicTiming:
    """Intervals must use time.monotonic(); only the PING token is wall-clock."""

    async def test_send_pacing_does_not_use_wall_clock(self, mock_client, monkeypatch):
        """_send_data rate limiting uses monotonic, never time.time (L11)."""
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
        """PONG latency survives a wall-clock step (uses monotonic) (L11)."""
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
# Sensor Listener Signature Tests (D4)
# ============================================================================


class TestSensorListenerSignature:
    """Dict-based listeners receive (field, value) — pins the D4 contract."""

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
        """Notification listeners are invoked as callback(field, value) (M2)."""
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
        """Stats listeners are invoked as callback(field, value) (M2)."""
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
