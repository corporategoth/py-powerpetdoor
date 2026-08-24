# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Integration tests for simulator - verifies correct message sending."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from powerpetdoor.const import (
    CMD_CLOSE,
    CMD_GET_DOOR_STATUS,
    CMD_OPEN,
    CONFIG,
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    FIELD_DOOR_STATUS,
    FIELD_SUCCESS,
    PING,
    PONG,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from tests.simulator.wire import WireCapture

#: The exact DOOR_STATUS broadcast sequence for an open that runs to
#: HOLDING, and for a complete open/hold/close cycle.
OPENING_SEQUENCE = [DOOR_STATE_RISING, DOOR_STATE_SLOWING, DOOR_STATE_HOLDING]
CLOSING_SEQUENCE = [
    # DOOR_CLOSING first: measured on a real door (firmware 1.7.18), the
    # motor starts before the flap moves and the device reports it.
    DOOR_STATE_CLOSING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSED,
]
FULL_CYCLE = [*OPENING_SEQUENCE, *CLOSING_SEQUENCE]

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
        closing_start_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(timing_config):
    """Create and start a simulator."""
    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


class MessageCapture(WireCapture):
    """Poll-based capture: read with a deadline, then assert on the stream.

    Framing (and everything else generic) lives in WireCapture, which uses
    the production `extract_frames` scanner - see tests/simulator/wire.py
    for why that matters.
    """

    async def receive_all(self, timeout: float = 1.0) -> list[dict[str, Any]]:
        """Receive all available messages within timeout."""
        try:
            data = await asyncio.wait_for(self.reader.read(8192), timeout=timeout)
        except TimeoutError:
            return []
        return self.feed(data) if data else []

    async def receive_until(
        self, predicate: callable, timeout: float = 5.0, poll_interval: float = 0.1
    ) -> list[dict[str, Any]]:
        """Receive messages until predicate returns True for any message."""
        start = asyncio.get_event_loop().time()
        messages: list[dict[str, Any]] = []

        while asyncio.get_event_loop().time() - start < timeout:
            try:
                data = await asyncio.wait_for(self.reader.read(4096), timeout=poll_interval)
            except TimeoutError:
                continue
            if data:
                new_msgs = self.feed(data)
                messages.extend(new_msgs)
                for msg in new_msgs:
                    if predicate(msg):
                        return messages

        return messages

    async def receive_status_sequence(self, expected: list[str], timeout: float = 5.0) -> list[str]:
        """Receive until the broadcast status sequence equals ``expected``.

        Returns the captured sequence; on timeout the caller's assertion
        reports the mismatch.
        """
        await self.receive_until(
            lambda _msg: self.get_status_sequence() == expected, timeout=timeout
        )
        return self.get_status_sequence()


@pytest.fixture
async def capture(simulator) -> MessageCapture:
    """Create a message capture connected to the simulator."""
    port = simulator.server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    cap = MessageCapture(reader, writer)
    yield cap
    await cap.close()


# ============================================================================
# Basic Protocol Tests
# ============================================================================


class TestBasicProtocol:
    """Test basic protocol message handling."""

    async def test_ping_pong(self, capture):
        """PING should receive PONG response."""
        await capture.send({PING: "test123"})
        messages = await capture.receive_all(timeout=0.5)

        assert len(messages) >= 1
        pong = capture.find_message(PONG)
        assert pong is not None
        assert pong[PONG] == "test123"
        assert pong[FIELD_SUCCESS] == "true"

    async def test_get_door_status(self, capture, simulator):
        """GET_DOOR_STATUS should return current status."""
        await capture.send({CONFIG: CMD_GET_DOOR_STATUS, "msgId": 1})
        await capture.receive_all(timeout=0.5)

        status_msg = capture.find_message(CMD_GET_DOOR_STATUS)
        assert status_msg is not None
        assert status_msg[FIELD_SUCCESS] == "true"
        assert status_msg[FIELD_DOOR_STATUS] == simulator.state.door_status


# ============================================================================
# Door Operation Message Tests
# ============================================================================


class TestDoorOperationMessages:
    """Test messages sent during door operations."""

    async def test_open_door_sends_status_updates(self, capture, simulator):
        """Opening the door broadcasts the exact opening sequence."""
        await capture.send({CONFIG: CMD_OPEN, "msgId": 1})

        statuses = await capture.receive_status_sequence(OPENING_SEQUENCE, timeout=3.0)

        assert statuses == OPENING_SEQUENCE
        open_response = capture.find_message(CMD_OPEN)
        assert open_response is not None
        assert open_response[FIELD_SUCCESS] == "true"

    async def test_close_door_sends_status_updates(self, capture, simulator):
        """Closing from KEEPUP broadcasts the exact closing sequence.

        The old version asserted only that a CLOSE response arrived, so it
        passed with *every* door-status broadcast suppressed - it could not
        fail for the thing it is named after.
        """
        # Open the door and wait until its final broadcast has been
        # *captured* before clearing, so no in-flight KEEPUP frame lands in
        # the sequence under test.
        await simulator.open_door(hold=True)
        await capture.receive_until(
            lambda m: m.get(FIELD_DOOR_STATUS) == DOOR_STATE_KEEPUP, timeout=3.0
        )
        assert capture.get_status_sequence()[-1] == DOOR_STATE_KEEPUP
        capture.messages.clear()

        # Send CLOSE command
        await capture.send({CONFIG: CMD_CLOSE, "msgId": 1})

        statuses = await capture.receive_status_sequence(CLOSING_SEQUENCE, timeout=3.0)

        assert statuses == CLOSING_SEQUENCE
        close_response = capture.find_message(CMD_CLOSE)
        assert close_response is not None
        assert close_response[FIELD_SUCCESS] == "true"

    async def test_sensor_trigger_sends_status_updates(self, capture, simulator):
        """A sensor trigger broadcasts the exact opening sequence."""
        simulator.trigger_sensor("inside")

        statuses = await capture.receive_status_sequence(OPENING_SEQUENCE, timeout=3.0)

        assert statuses == OPENING_SEQUENCE

    async def test_full_door_cycle_messages(self, capture, simulator):
        """A sensor-driven cycle broadcasts every state, in order."""
        simulator.trigger_sensor("inside")

        statuses = await capture.receive_status_sequence(FULL_CYCLE, timeout=6.0)

        assert statuses == FULL_CYCLE


# ============================================================================
# Multi-Client Tests
# ============================================================================


class TestMultiClient:
    """Test simulator behavior with multiple clients."""

    async def test_multiple_clients_receive_broadcasts(self, simulator):
        """Multiple clients should receive status broadcasts."""
        port = simulator.server.sockets[0].getsockname()[1]

        # Connect two clients
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)

        cap1 = MessageCapture(r1, w1)
        cap2 = MessageCapture(r2, w2)

        try:
            # Trigger door from one client via simulator API
            simulator.trigger_sensor("inside")

            # Both clients see the same broadcasts, in the same order: a
            # ">0 status updates" assertion could not tell a dropped state
            # (or a command response) from a real broadcast.
            assert await cap1.receive_status_sequence(OPENING_SEQUENCE, 3.0) == OPENING_SEQUENCE
            assert await cap2.receive_status_sequence(OPENING_SEQUENCE, 3.0) == OPENING_SEQUENCE

        finally:
            await cap1.close()
            await cap2.close()

    async def test_command_from_one_client_broadcasts(self, simulator):
        """Command from one client should broadcast status to all."""
        port = simulator.server.sockets[0].getsockname()[1]

        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)

        cap1 = MessageCapture(r1, w1)
        cap2 = MessageCapture(r2, w2)

        try:
            # Send OPEN from client 1
            await cap1.send({CONFIG: CMD_OPEN, "msgId": 1})

            # The issuing client gets the response *and* the same broadcast
            # sequence every other client gets.
            assert await cap1.receive_status_sequence(OPENING_SEQUENCE, 3.0) == OPENING_SEQUENCE
            assert await cap2.receive_status_sequence(OPENING_SEQUENCE, 3.0) == OPENING_SEQUENCE

            open_response = cap1.find_message(CMD_OPEN)
            assert open_response is not None
            assert open_response[FIELD_SUCCESS] == "true"
            # The command response only reaches the client that issued it.
            assert cap2.find_message(CMD_OPEN) is None

        finally:
            await cap1.close()
            await cap2.close()
