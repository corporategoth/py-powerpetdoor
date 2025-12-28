# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Integration tests for simulator - verifies correct message sending."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.const import (
    PING,
    PONG,
    CONFIG,
    CMD_GET_DOOR_STATUS,
    CMD_OPEN,
    CMD_CLOSE,
    FIELD_SUCCESS,
    FIELD_DOOR_STATUS,
    DOOR_STATE_CLOSED,
    DOOR_STATE_RISING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_SLOWING,
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
async def simulator(timing_config):
    """Create and start a simulator."""
    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


class MessageCapture:
    """Helper to capture messages from the simulator."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.messages: list[dict[str, Any]] = []

    async def send(self, msg: dict[str, Any]) -> None:
        """Send a message to the simulator."""
        data = json.dumps(msg).encode("ascii")
        self.writer.write(data)
        await self.writer.drain()

    async def receive_all(self, timeout: float = 1.0) -> list[dict[str, Any]]:
        """Receive all available messages within timeout."""
        messages = []
        try:
            data = await asyncio.wait_for(
                self.reader.read(8192),
                timeout=timeout
            )
            if data:
                messages.extend(self._parse_messages(data.decode("ascii")))
        except asyncio.TimeoutError:
            pass
        self.messages.extend(messages)
        return messages

    async def receive_until(
        self,
        predicate: callable,
        timeout: float = 5.0,
        poll_interval: float = 0.1
    ) -> list[dict[str, Any]]:
        """Receive messages until predicate returns True for any message."""
        start = asyncio.get_event_loop().time()
        messages = []

        while asyncio.get_event_loop().time() - start < timeout:
            try:
                data = await asyncio.wait_for(
                    self.reader.read(4096),
                    timeout=poll_interval
                )
                if data:
                    new_msgs = self._parse_messages(data.decode("ascii"))
                    messages.extend(new_msgs)
                    self.messages.extend(new_msgs)
                    for msg in new_msgs:
                        if predicate(msg):
                            return messages
            except asyncio.TimeoutError:
                continue

        return messages

    def _parse_messages(self, data: str) -> list[dict[str, Any]]:
        """Parse multiple JSON objects from a string."""
        messages = []
        pos = 0
        while pos < len(data):
            if data[pos] != '{':
                pos += 1
                continue
            depth = 0
            end = pos
            for i, c in enumerate(data[pos:], pos):
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > pos:
                try:
                    messages.append(json.loads(data[pos:end]))
                except json.JSONDecodeError:
                    pass
            pos = end if end > pos else pos + 1
        return messages

    def find_message(self, cmd: str) -> dict[str, Any] | None:
        """Find a message by CMD field."""
        for msg in self.messages:
            if msg.get("CMD") == cmd:
                return msg
        return None

    def find_status_updates(self) -> list[dict[str, Any]]:
        """Find all door status update messages."""
        return [
            msg for msg in self.messages
            if FIELD_DOOR_STATUS in msg
        ]

    async def close(self):
        """Close the connection."""
        self.writer.close()
        await self.writer.wait_closed()


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

    @pytest.mark.asyncio
    async def test_ping_pong(self, capture):
        """PING should receive PONG response."""
        await capture.send({PING: "test123"})
        messages = await capture.receive_all(timeout=0.5)

        assert len(messages) >= 1
        pong = capture.find_message(PONG)
        assert pong is not None
        assert pong[PONG] == "test123"
        assert pong[FIELD_SUCCESS] == "true"

    @pytest.mark.asyncio
    async def test_get_door_status(self, capture, simulator):
        """GET_DOOR_STATUS should return current status."""
        await capture.send({CONFIG: CMD_GET_DOOR_STATUS, "msgId": 1})
        messages = await capture.receive_all(timeout=0.5)

        status_msg = capture.find_message(CMD_GET_DOOR_STATUS)
        assert status_msg is not None
        assert status_msg[FIELD_SUCCESS] == "true"
        assert status_msg[FIELD_DOOR_STATUS] == simulator.state.door_status


# ============================================================================
# Door Operation Message Tests
# ============================================================================

class TestDoorOperationMessages:
    """Test messages sent during door operations."""

    @pytest.mark.asyncio
    async def test_open_door_sends_status_updates(self, capture, simulator):
        """Opening door should send status update messages."""
        # Send OPEN command
        await capture.send({CONFIG: CMD_OPEN, "msgId": 1})

        # Wait for door to start rising
        await capture.receive_until(
            lambda m: m.get(FIELD_DOOR_STATUS) in (DOOR_STATE_RISING, DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP),
            timeout=2.0
        )

        # Receive any remaining messages (command response might arrive after status)
        await capture.receive_all(timeout=0.2)

        # Should have received open response
        open_response = capture.find_message(CMD_OPEN)
        assert open_response is not None
        assert open_response[FIELD_SUCCESS] == "true"

        # Should have received status updates
        status_updates = capture.find_status_updates()
        assert len(status_updates) > 0

    @pytest.mark.asyncio
    async def test_close_door_sends_status_updates(self, capture, simulator):
        """Closing door should send status update messages."""
        # First open the door
        await simulator.open_door(hold=True)
        await asyncio.sleep(0.1)

        # Clear any existing messages
        await capture.receive_all(timeout=0.2)
        capture.messages.clear()

        # Send CLOSE command
        await capture.send({CONFIG: CMD_CLOSE, "msgId": 1})

        # Wait for close response and status updates
        messages = await capture.receive_until(
            lambda m: m.get(FIELD_DOOR_STATUS) == DOOR_STATE_CLOSED,
            timeout=3.0
        )

        # Should have received close response
        close_response = capture.find_message(CMD_CLOSE)
        assert close_response is not None
        assert close_response[FIELD_SUCCESS] == "true"

    @pytest.mark.asyncio
    async def test_sensor_trigger_sends_status_updates(self, capture, simulator):
        """Sensor trigger should send door status updates."""
        # Trigger inside sensor
        simulator.trigger_sensor("inside")

        # Wait for door to start opening
        messages = await capture.receive_until(
            lambda m: m.get(FIELD_DOOR_STATUS) in (DOOR_STATE_RISING, DOOR_STATE_HOLDING),
            timeout=2.0
        )

        # Should have received status updates with door opening
        status_updates = capture.find_status_updates()
        assert len(status_updates) > 0

        # At least one should show door not closed
        door_opening = any(
            msg[FIELD_DOOR_STATUS] != DOOR_STATE_CLOSED
            for msg in status_updates
        )
        assert door_opening, "Should see door status change from closed"

    @pytest.mark.asyncio
    async def test_full_door_cycle_messages(self, capture, simulator):
        """Full door cycle should send complete status sequence."""
        # Trigger sensor to start a full cycle
        simulator.trigger_sensor("inside")

        # Wait for door to close again (full cycle)
        await capture.receive_until(
            lambda m: (
                m.get(FIELD_DOOR_STATUS) == DOOR_STATE_CLOSED
                and any(
                    msg.get(FIELD_DOOR_STATUS) != DOOR_STATE_CLOSED
                    for msg in capture.messages
                )
            ),
            timeout=5.0
        )

        status_updates = capture.find_status_updates()
        statuses = [msg[FIELD_DOOR_STATUS] for msg in status_updates]

        # Should see progression: some non-closed states, then closed
        non_closed = [s for s in statuses if s != DOOR_STATE_CLOSED]
        assert len(non_closed) > 0, "Should see door open before closing"


# ============================================================================
# Multi-Client Tests
# ============================================================================

class TestMultiClient:
    """Test simulator behavior with multiple clients."""

    @pytest.mark.asyncio
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

            # Both clients should receive status updates
            msgs1 = await cap1.receive_until(
                lambda m: m.get(FIELD_DOOR_STATUS) == DOOR_STATE_RISING
                or m.get(FIELD_DOOR_STATUS) == DOOR_STATE_HOLDING,
                timeout=2.0
            )
            msgs2 = await cap2.receive_until(
                lambda m: m.get(FIELD_DOOR_STATUS) == DOOR_STATE_RISING
                or m.get(FIELD_DOOR_STATUS) == DOOR_STATE_HOLDING,
                timeout=2.0
            )

            # Both should have received status updates
            assert len(cap1.find_status_updates()) > 0
            assert len(cap2.find_status_updates()) > 0

        finally:
            await cap1.close()
            await cap2.close()

    @pytest.mark.asyncio
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

            # Wait for BOTH the command response AND a status update on client 1
            # The command response and status update may arrive in any order
            await cap1.receive_until(
                lambda m: (
                    cap1.find_message(CMD_OPEN) is not None
                    and len(cap1.find_status_updates()) > 0
                ),
                timeout=2.0
            )

            # Wait for status update on client 2
            await cap2.receive_until(
                lambda m: m.get(FIELD_DOOR_STATUS) == DOOR_STATE_RISING
                or m.get(FIELD_DOOR_STATUS) == DOOR_STATE_HOLDING
                or m.get(FIELD_DOOR_STATUS) == DOOR_STATE_KEEPUP,
                timeout=2.0
            )

            # Client 1 should have OPEN response
            assert cap1.find_message(CMD_OPEN) is not None

            # Both should have status updates
            assert len(cap2.find_status_updates()) > 0

        finally:
            await cap1.close()
            await cap2.close()
