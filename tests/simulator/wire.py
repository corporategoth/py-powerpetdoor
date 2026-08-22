# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Shared wire-message capture for simulator integration tests.

There used to be two ``MessageCapture`` classes, and one of them hand-rolled
a brace-depth scanner that was not string-aware - precisely the defect round
1 removed from ``client.find_end`` and replaced with
:func:`powerpetdoor.framing.extract_frames`. Any simulator payload carrying
a brace inside a JSON string value would have been mis-framed, and the
helper swallowed the resulting JSONDecodeError silently, so the message
would simply vanish.

The framing lives here once, in :class:`WireCapture`, and uses the
production scanner. The two capture styles (poll-based and
background-listener) subclass it and add only their waiting model.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from powerpetdoor.const import DOOR_STATUS, FIELD_DOOR_STATUS
from powerpetdoor.framing import extract_frames


class WireCapture:
    """Collects framed simulator messages from a stream reader.

    Args:
        reader: Stream to read simulator output from.
        writer: Stream to send commands on.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.messages: list[dict[str, Any]] = []
        self._buffer = ""

    async def send(self, msg: dict[str, Any]) -> None:
        """Send one message to the simulator."""
        self.writer.write(json.dumps(msg).encode("ascii"))
        await self.writer.drain()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Frame and record ``data``, returning the newly parsed messages.

        Partial frames are carried over to the next call, so a message split
        across reads is never lost.
        """
        frames, self._buffer, _diag = extract_frames(self._buffer + data.decode("ascii"))
        parsed = [json.loads(frame) for frame in frames]
        self.messages.extend(parsed)
        return parsed

    def find_message(self, cmd: str) -> dict[str, Any] | None:
        """The first captured message whose ``CMD`` is ``cmd``, if any."""
        for msg in self.messages:
            if msg.get("CMD") == cmd:
                return msg
        return None

    def find_messages(self, cmd: str) -> list[dict[str, Any]]:
        """Every captured message whose ``CMD`` is ``cmd``."""
        return [msg for msg in self.messages if msg.get("CMD") == cmd]

    def get_status_sequence(self) -> list[str]:
        """The exact sequence of unsolicited DOOR_STATUS broadcasts.

        Command responses (OPEN, CLOSE, GET_DOOR_STATUS) also carry a
        ``doorStatus`` field; only ``CMD: DOOR_STATUS`` frames are
        broadcasts, and matching on the field alone would let a command
        response satisfy an assertion about broadcasts.
        """
        return [msg[FIELD_DOOR_STATUS] for msg in self.messages if msg.get("CMD") == DOOR_STATUS]

    async def close(self) -> None:
        """Close the connection."""
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
