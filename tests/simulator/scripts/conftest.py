# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Shared fixtures for built-in script tests.

The built-in scripts synchronize on door state via ``wait_for`` conditions,
so these tests run with fast door timing and assert exact outcomes - no
wall-clock coupling (test-fanatic M5).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    FIELD_DOOR_STATUS,
)
from powerpetdoor.framing import extract_frames
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.scripting import ScriptRunner

#: The exact broadcast sequence of one full sensor-triggered door cycle.
FULL_CYCLE = [
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSED,
]


class MessageCapture:
    """Collects simulator wire messages with deterministic waiting.

    A background reader appends parsed messages and fires an event, so
    tests await conditions on the captured stream instead of sleeping.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.messages: list[dict[str, Any]] = []
        self._buffer = ""
        self._new_message = asyncio.Event()
        self._listen_task: asyncio.Task | None = None

    def start_listening(self):
        """Start the background task that collects all messages."""
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self):
        """Background loop collecting messages."""
        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    break
                frames, self._buffer, _ = extract_frames(self._buffer + data.decode("ascii"))
                for frame in frames:
                    self.messages.append(json.loads(frame))
                if frames:
                    self._new_message.set()
        except asyncio.CancelledError:
            pass

    async def wait_for(
        self, predicate: Callable[[list[dict[str, Any]]], bool], timeout: float = 5.0
    ) -> None:
        """Wait until ``predicate(self.messages)`` holds (event-driven)."""
        async with asyncio.timeout(timeout):
            while not predicate(self.messages):
                self._new_message.clear()
                await self._new_message.wait()

    def find_messages(self, cmd: str) -> list[dict[str, Any]]:
        """Find all messages with the given CMD value."""
        return [msg for msg in self.messages if msg.get("CMD") == cmd]

    def get_status_sequence(self) -> list[str]:
        """Get the sequence of door statuses seen."""
        return [msg[FIELD_DOOR_STATUS] for msg in self.messages if FIELD_DOOR_STATUS in msg]

    async def wait_for_status_sequence(
        self, expected: list[str], timeout: float = 10.0
    ) -> list[str]:
        """Wait until the captured status sequence equals ``expected``.

        Returns the captured sequence; on timeout the caller's follow-up
        assertion reports the mismatching sequence.
        """
        try:
            await self.wait_for(
                lambda _msgs: self.get_status_sequence() == expected, timeout=timeout
            )
        except TimeoutError:
            pass
        return self.get_status_sequence()

    async def close(self):
        """Stop listening and close the connection."""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


@pytest.fixture
def script_timing():
    """Fast timing config for built-in script tests.

    The scripts synchronize on door state (wait_for), not wall-clock
    waits, so fast transitions are safe.
    """
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(script_timing):
    """Create and start a simulator for script tests."""
    state = DoorSimulatorState(timing=script_timing, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def runner(simulator):
    """Create a script runner."""
    return ScriptRunner(simulator)


@pytest.fixture
async def message_capture(simulator) -> MessageCapture:
    """A message capture connected to the simulator.

    The fixture returns only after the simulator has registered the
    connection, so every broadcast during script execution is captured.
    """
    port = simulator.server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    cap = MessageCapture(reader, writer)
    cap.start_listening()
    # Deterministic: yield to the loop until the server registers the client
    async with asyncio.timeout(2):
        while not simulator.protocols:
            await asyncio.sleep(0)
    yield cap
    await cap.close()
