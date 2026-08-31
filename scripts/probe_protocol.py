#!/usr/bin/env python3
# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Discover the wire protocol by exercising the simulator.

Every command is sent to a real simulator and the real reply recorded, so
the resulting description is *observed* rather than declared. That
matters here more than it usually would: the simulator is the thing
so a shape taken from it is a shape a
real door produced. A hand-written spec is a third place for the protocol
to be described, and the two that already exist disagreed once.

What still cannot come from here is *why* - the accept-and-ignore
behaviours, the 32-bit saturation, the `config`-versus-`cmd` discovery,
the vendor app's stale-cache bug. Those are findings, and they live in
``docs/protocol.md``.

Two things are declared rather than discovered, because there is nowhere
else they could come from:

``REQUEST_ARGUMENTS``
    A representative argument for the commands that need one. A
    ``SET_HOLD_TIME`` with no ``holdTime`` echoes the current value and
    tells you nothing about the field.

``SILENT``
    Commands a real door answers with nothing at all. Emulated, so the
    prober must not wait for a reply that is never coming.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from powerpetdoor import const  # noqa: E402
from powerpetdoor.framing import FrameScanner  # noqa: E402
from powerpetdoor.simulator import DoorSimulator, DoorSimulatorState  # noqa: E402
from powerpetdoor.simulator.protocol import CommandRegistry  # noqa: E402
from powerpetdoor.simulator.state import Schedule  # noqa: E402

#: Commands that need an argument before they say anything interesting,
#: and a representative value for it. The values are legal - the point is
#: to record the shape of a *successful* exchange, not the refusals, which
#: docs/protocol.md tabulates.
#:
#: Every value here must DIFFER from the state's default. The effect probe
#: works by reading a value back, so writing the value it already held
#: reads as "accepted and ignored" - which is a real behaviour of this
#: door and must not be reported where it is not happening.
REQUEST_ARGUMENTS: dict[str, dict[str, Any]] = {
    const.CMD_SET_TIMEZONE: {const.FIELD_TZ: "GMT0BST,M3.5.0/1,M10.5.0"},
    const.CMD_SET_HOLD_TIME: {const.FIELD_HOLD_TIME: 1500},
    const.CMD_SET_SENSOR_TRIGGER_VOLTAGE: {const.FIELD_VOLTAGE: 1500},
    const.CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE: {const.FIELD_VOLTAGE: 1500},
    const.CMD_SET_NOTIFICATIONS: {
        const.FIELD_NOTIFICATIONS: {const.FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: True}
    },
    const.CMD_GET_SCHEDULE: {const.FIELD_INDEX: 0},
    const.CMD_DELETE_SCHEDULE: {const.FIELD_INDEX: 0},
    const.CMD_SET_SCHEDULE: {
        const.FIELD_INDEX: 0,
        const.FIELD_SCHEDULE: {
            "index": 0,
            "enabled": 1,
            "daysOfWeek": [1, 1, 1, 1, 1, 1, 1],
            "inside": 1,
            "outside": 0,
            "in_start_time": {"hour": 6, "min": 0},
            "in_end_time": {"hour": 22, "min": 0},
            "out_start_time": {"hour": 0, "min": 0},
            "out_end_time": {"hour": 0, "min": 0},
        },
    },
}


def probe_state() -> DoorSimulatorState:
    """A door with something in it.

    A default door has no schedules, so `GET_SCHEDULE` answered with
    `"schedule": null` and `GET_SCHEDULE_LIST` with an empty list - which
    documented the two commands that most need an example with nothing at
    all.

    Slot 0 is populated, and deliberately NOT with the schedule
    `REQUEST_ARGUMENTS` writes: the effect probe works by reading a value
    back, so seeding the value `SET_SCHEDULE` is about to write would
    report the command as accepted-and-ignored.
    """
    return DoorSimulatorState(
        schedules={
            0: Schedule(
                index=0,
                enabled=True,
                days_of_week=[False, True, True, True, True, True, False],
                inside=True,
                outside=False,
                start_hour=8,
                start_min=30,
                end_hour=18,
                end_min=45,
            )
        }
    )


#: Commands a door answers with nothing at all, so the prober must not
#: block waiting for a reply.
#:
#: Empty, and it should stay that way. Silence is a dropped request rather
#: than an answer, so a command listed here on the strength of one quiet
#: reply is a misattribution waiting to happen - ask again before adding
#: anything.
SILENT: frozenset[str] = frozenset()

#: Fields present in every reply. Recorded once on the envelope rather
#: than repeated into all 43 messages.
ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {const.FIELD_CMD, const.FIELD_SUCCESS, const.FIELD_DIRECTION, const.FIELD_MSG_ID_RESPONSE}
)


def json_type(value: Any) -> dict[str, Any]:
    """A JSON Schema fragment describing an observed value.

    Shape only. The example is carried alongside, because for this
    protocol the *spelling* is the whole story - `"true"` and `1` are both
    booleans on this wire and the door is particular about which goes
    where.
    """
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        item = json_type(value[0]) if value else {}
        return {"type": "array", "items": item}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: json_type(v) for k, v in value.items()},
        }
    return {}


class _Probe(asyncio.Protocol):
    """A client that sends one command and keeps whatever comes back."""

    def __init__(self) -> None:
        self.scanner = FrameScanner()
        self.messages: list[dict] = []
        self.transport: asyncio.Transport | None = None

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def data_received(self, data: bytes) -> None:  # type: ignore[override]
        frames, _ = self.scanner.feed(data.decode("ascii"))
        self.messages.extend(json.loads(frame) for frame in frames)

    def send(self, message: dict) -> None:
        assert self.transport is not None
        self.transport.write(json.dumps(message).encode("ascii"))


async def _exchange(port: int, command: str, msg_id: int) -> tuple[dict, dict | None]:
    """Send one command to a fresh connection and return (request, reply)."""
    loop = asyncio.get_running_loop()
    envelope = const.COMMAND if command in const.COMMAND_ENVELOPE_COMMANDS else const.CONFIG
    request: dict[str, Any] = {
        envelope: command,
        const.FIELD_MSG_ID: str(msg_id),
        # What the client actually puts on the wire. The door does not
        # require it - every command here was first probed without it and
        # answered normally - but a reader implementing from this should
        # see the shape real traffic has.
        const.FIELD_DIRECTION: const.PHONE_TO_DOOR,
    }
    request.update(REQUEST_ARGUMENTS.get(command, {}))

    transport, probe = await loop.create_connection(_Probe, "127.0.0.1", port)
    try:
        probe.send(request)
        # A silent command is *defined* by answering nothing, so waiting
        # for it is waiting forever. Give the others a bounded window and
        # take whatever arrived.
        deadline = 0.05 if command in SILENT else 2.0
        waited = 0.0
        while not probe.messages and waited < deadline:
            await asyncio.sleep(0.01)
            waited += 0.01
        reply = probe.messages[0] if probe.messages else None
    finally:
        transport.close()
    return request, reply


def _read_back_for(command: str) -> str | None:
    """The `GET_*` that reads what a `SET_*` writes, if there is one.

    Derived from the names rather than tabulated: `SET_HOLD_TIME` is read
    by `GET_HOLD_TIME`. A pairing that has to be maintained by hand is a
    pairing that goes stale.
    """
    if not command.startswith("SET_"):
        return None
    candidate = "GET_" + command[len("SET_") :]
    return candidate if candidate in CommandRegistry._handlers else None


async def _observe_effect(port: int, command: str, msg_id: int) -> bool | None:
    """Did the write actually change what the matching read returns?

    This is how "accepted and ignored" is *discovered* rather than
    asserted. The door has several - a `voltage` of 0 succeeds and is
    discarded, a nested `SET_NOTIFICATIONS` payload of strings likewise -
    and a description that reports them as working is worse than one that
    omits them, because a client will believe it.

    ``None`` when there is nothing to read the write back with.
    """
    read_back = _read_back_for(command)
    if read_back is None:
        return None
    before = (await _exchange(port, read_back, msg_id))[1]
    await _exchange(port, command, msg_id + 1)
    after = (await _exchange(port, read_back, msg_id + 2))[1]
    if before is None or after is None:
        return None
    strip = lambda m: {k: v for k, v in m.items() if k not in ENVELOPE_FIELDS}  # noqa: E731
    return strip(before) != strip(after)


async def probe_all() -> dict[str, dict[str, Any]]:
    """Exchange every command with a simulator, returning what was seen.

    The simulator is restarted for each command so a mutation (POWER_OFF,
    DELETE_SCHEDULE) cannot change the shape the next one records.
    """
    observed: dict[str, dict[str, Any]] = {}
    for msg_id, command in enumerate(sorted(CommandRegistry._handlers), start=1):
        simulator = DoorSimulator(host="127.0.0.1", port=0, state=probe_state())
        await simulator.start()
        assert simulator.server is not None
        port = simulator.server.sockets[0].getsockname()[1]
        try:
            request, reply = await _exchange(port, command, msg_id)
        finally:
            await simulator.stop()

        # A *fresh* simulator to measure the effect. The exchange above
        # already applied the command, and re-applying the same value
        # changes nothing whether or not the command works - which is
        # exactly the false "accepted and ignored" this must not report.
        effect_sim = DoorSimulator(host="127.0.0.1", port=0, state=probe_state())
        await effect_sim.start()
        assert effect_sim.server is not None
        try:
            took_effect = await _observe_effect(
                effect_sim.server.sockets[0].getsockname()[1], command, msg_id * 100
            )
        finally:
            await effect_sim.stop()

        envelope = const.COMMAND if command in const.COMMAND_ENVELOPE_COMMANDS else const.CONFIG
        observed[command] = {
            "envelope": envelope,
            "request": {k: v for k, v in request.items() if k not in (envelope,)},
            "reply": reply,
            "silent": reply is None,
            # True/False when a matching GET_* could confirm it; None when
            # there is nothing to read the write back with.
            "took_effect": took_effect,
            "read_back": _read_back_for(command),
        }
    return observed


def main() -> int:
    observed = asyncio.run(probe_all())
    silent = [c for c, o in observed.items() if o["silent"]]
    ignored = [c for c, o in observed.items() if o["took_effect"] is False]
    print(f"probed {len(observed)} command(s)")
    print(f"  answered with silence : {silent or 'none'}")
    print(f"  accepted and IGNORED  : {ignored or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
