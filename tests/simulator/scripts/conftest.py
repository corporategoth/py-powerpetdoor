# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Shared fixtures for built-in script tests."""
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
from powerpetdoor.simulator.scripting import ScriptRunner
from powerpetdoor.const import FIELD_DOOR_STATUS


class MessageCapture:
    """Helper to capture messages from the simulator."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.messages: list[dict[str, Any]] = []
        self._listen_task: asyncio.Task | None = None

    async def start_listening(self):
        """Start background task to collect all messages."""
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self):
        """Background loop to collect messages."""
        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    break
                self.messages.extend(self._parse_messages(data.decode("ascii")))
        except asyncio.CancelledError:
            pass

    async def stop_listening(self):
        """Stop the background listener and return all messages."""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        return self.messages

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

    def find_status_updates(self) -> list[dict[str, Any]]:
        """Find all door status update messages."""
        return [msg for msg in self.messages if FIELD_DOOR_STATUS in msg]

    def get_status_sequence(self) -> list[str]:
        """Get the sequence of door statuses seen."""
        return [msg[FIELD_DOOR_STATUS] for msg in self.find_status_updates()]

    async def close(self):
        """Close the connection."""
        await self.stop_listening()
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


@pytest.fixture
def script_timing():
    """Create slower timing config for built-in script tests.

    Built-in scripts expect realistic timing to properly test assertions
    at specific points in the door cycle. The rise_time must be longer
    than the waits before assertions (e.g., basic_cycle waits 0.5s then
    asserts DOOR_RISING).
    """
    return DoorTimingConfig(
        rise_time=1.0,  # Must be > 0.5s to pass basic_cycle assertions
        default_hold_time=2,
        slowing_time=0.2,
        closing_top_time=0.2,
        closing_mid_time=0.2,
        sensor_retrigger_window=0.3,
    )


@pytest.fixture
async def simulator(script_timing):
    """Create and start a simulator with script-appropriate timing."""
    state = DoorSimulatorState(timing=script_timing, hold_time=2)
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
    """Create a message capture connected to the simulator.

    This connects a client that listens for all messages sent by
    the simulator during script execution.
    """
    port = simulator.server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    cap = MessageCapture(reader, writer)
    await cap.start_listening()
    # Give the listener task time to start its first read operation
    await asyncio.sleep(0.05)
    yield cap
    await cap.close()
