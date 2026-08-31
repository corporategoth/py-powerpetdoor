# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Drive a simulator from Python - one you own, or a daemon you connect to.

``ppd-simulator-ctl`` can already drive a running daemon from a shell.
This is the same thing for a program: one interface, two implementations,
so a test can be written once and run either against a simulator it spins
up itself or against a long-lived daemon.

**The shared surface is commands, not state.** :class:`LocalSimulator`
exposes the live
:class:`~powerpetdoor.simulator.state.DoorSimulatorState` because it is the
same object in the same process; :class:`RemoteSimulator` has no state
accessor at all. Over a socket a state could only ever be a *snapshot*, and
a proxy that made a stale snapshot look live would be a worse bug than its
absence - it is exactly the failure the vendor app has (see
``docs/protocol.md``: it renders its own cached copy and silently writes it
back).

To assert against a remote door, run a script: ``run <name> wait`` reports
PASSED/FAILED over the channel, and ``assert`` steps are where this project
already puts assertions.

Three lifecycle verbs, not two, for the same reason ``stop`` and
``shutdown`` are already distinct on the control channel:

- :meth:`stop_script` stops the running script.
- :meth:`close` releases what this object owns. Locally that stops the
  simulator; remotely it only disconnects - a client falling out of a
  ``with`` block must never kill somebody else's daemon.
- :meth:`shutdown` stops the simulator either way, and is the one that
  needs saying out loud.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from ..i18n import t
from ..sanitize import sanitize_text
from .cli import DEFAULT_CONTROL_HOST

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .server import DoorSimulator


class SimulatorControlError(Exception):
    """A command was refused, or the control channel could not be reached."""


class CommandFailedError(SimulatorControlError):
    """The simulator ran the command and reported failure."""


#: How long to wait for a gap in daemon traffic before giving up. Bounds a
#: *gap*, not the total: each chunk restarts it, so a chatty command never
#: times out. Matches ``ctl``'s own default.
DEFAULT_TIMEOUT = 5.0


class SimulatorController:
    """The surface a local and a remote simulator both provide."""

    async def execute(self, line: str) -> str:
        """Run one command line and return its message.

        Raises:
            CommandFailedError: If the simulator reported the command failed.
        """
        raise NotImplementedError  # pragma: no cover - abstract

    # -- door -------------------------------------------------------------

    async def open(self) -> str:
        """Open the door and hold it open."""
        return await self.execute("open")

    async def close_door(self) -> str:
        """Close the door."""
        return await self.execute("close")

    async def cycle(self) -> str:
        """Open, hold for ``hold_time``, then close."""
        return await self.execute("cycle")

    async def toggle(self) -> str:
        """Open if closed, close if open; nothing mid-travel."""
        return await self.execute("toggle")

    async def trigger(self, sensor: str, duration: float | None = None) -> str:
        """Activate a sensor, as a collar-wearing pet would."""
        if sensor not in ("inside", "outside"):
            raise SimulatorControlError(
                t(
                    "simulator.control.unknown_sensor_use",
                    "Unknown sensor: {arg0}. Use: inside, outside",
                    arg0=sanitize_text(sensor),
                )
            )
        suffix = "" if duration is None else f" {duration}"
        return await self.execute(f"{sensor}{suffix}")

    async def obstruction(self, duration: float | None = None) -> str:
        """Place or clear a physical obstruction in the doorway."""
        suffix = "" if duration is None else f" {duration}"
        return await self.execute(f"obstruction{suffix}")

    # -- scripts ----------------------------------------------------------

    async def run_script(self, name: str, wait: bool = True) -> str:
        """Run a script **by name**.

        With ``wait`` the run is synchronous and its pass/fail is the
        result; without it the script is queued and the result only
        reports the queueing.
        """
        return await self.execute(f"run {name}{' wait' if wait else ''}")

    async def stop_script(self, all_queued: bool = False) -> str:
        """Stop the running script, optionally discarding the queue."""
        return await self.execute(f"stop{' all' if all_queued else ''}")

    # -- state ------------------------------------------------------------

    async def reset(self, document: str | None = None) -> str:
        """Reset to the initial state, or to a named state document."""
        return await self.execute(f"reset{'' if document is None else ' ' + document}")

    # -- lifecycle --------------------------------------------------------

    async def shutdown(self) -> str:
        """Stop the simulator itself. Remotely, this ends someone's daemon."""
        return await self.execute("shutdown")

    async def close(self) -> None:
        """Release what this object owns. Never stops a daemon it merely dialled."""
        raise NotImplementedError  # pragma: no cover - abstract


class LocalSimulator(SimulatorController):
    """A simulator running in this process, driven through its handler."""

    def __init__(self, simulator: DoorSimulator, handler: Any) -> None:
        self._simulator = simulator
        self._handler = handler

    @property
    def simulator(self) -> DoorSimulator:
        """The simulator object itself, for callers that want its live state."""
        return self._simulator

    async def execute(self, line: str) -> str:
        result = await self._handler.execute(line)
        if not result.success:
            raise CommandFailedError(result.message)
        return str(result.message)

    async def close(self) -> None:
        """Stop the simulator, which this object owns."""
        await self._simulator.stop()


class RemoteSimulator(SimulatorController):
    """A daemon reached over its control port.

    Speaks the same line protocol ``ppd-simulator-ctl`` does: one command
    per line, answered by ``OK:`` or ``ERROR:``. ``LOG:`` and ``STATUS:``
    lines arrive unsolicited and are skipped, so a command issued while the
    daemon is chatty still returns its own answer.
    """

    def __init__(
        self,
        host: str = DEFAULT_CONTROL_HOST,
        port: int = 3001,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the control connection."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), self._timeout
            )
        except (OSError, TimeoutError) as exc:
            raise SimulatorControlError(
                t(
                    "simulator.control.cannot_reach_channel",
                    "Cannot reach the control channel at {host}:{port}: {arg0}",
                    host=self._host,
                    port=self._port,
                    arg0=exc,
                )
            ) from None

    async def execute(self, line: str) -> str:
        if self._writer is None or self._reader is None:
            raise SimulatorControlError(
                t(
                    "simulator.control.not_connected",
                    "Not connected; call connect() first",
                )
            )
        if "\n" in line:
            # One command per line is the whole framing; a newline inside
            # one would be read as a second command.
            raise SimulatorControlError(
                t(
                    "simulator.control.command_cannot_contain_newline",
                    "A command line cannot contain a newline",
                )
            )

        async with self._lock:
            self._writer.write(f"{line}\n".encode())
            await self._writer.drain()
            return await self._read_response(line)

    async def _read_response(self, line: str) -> str:
        """Read until the OK/ERROR that answers this command.

        A run that waits on a script has no deadline: its duration is
        unbounded and arbitrarily quiet, and the live connection is the
        liveness signal - the same rule ``ctl`` applies.
        """
        assert self._reader is not None
        unbounded = line.split()[:1] == ["run"] and line.split()[-1:] == ["wait"]
        while True:
            try:
                raw = await (
                    self._reader.readline()
                    if unbounded
                    else asyncio.wait_for(self._reader.readline(), self._timeout)
                )
            except TimeoutError:
                raise SimulatorControlError(
                    t(
                        "simulator.control.no_response_within",
                        "No response within {timeout}s to: {arg0}",
                        timeout=self._timeout,
                        arg0=sanitize_text(line),
                    )
                ) from None
            if not raw:
                raise SimulatorControlError(
                    t(
                        "simulator.control.channel_closed_without_answering",
                        "The control channel closed without answering",
                    )
                )
            text = raw.decode(errors="replace").strip()
            if text.startswith("OK:"):
                return _unescape(text[3:].strip())
            if text.startswith("ERROR:"):
                raise CommandFailedError(_unescape(text[6:].strip()))
            # LOG:/STATUS: lines are not answers to anything.

    async def close(self) -> None:
        """Disconnect, leaving the daemon running.

        Emphatically not ``shutdown``: this object dialled a daemon it does
        not own, and leaving a ``with`` block must not stop it.
        """
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                # The peer vanishing during teardown is ordinary. Raising
                # here would replace whatever the `with` body was failing
                # on with a connection error from the unwind.
                pass


def _unescape(message: str) -> str:
    r"""Undo the control channel's ``\n`` escaping."""
    return message.replace("\\n", "\n").replace("\\\\", "\\")


@asynccontextmanager
async def simulator_control(
    host: str = DEFAULT_CONTROL_HOST,
    port: int = 3000,
    *,
    remote: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    **simulator_kwargs: Any,
) -> AsyncIterator[SimulatorController]:
    """Drive a simulator, local or remote, through one interface.

    Args:
        host: Address of the control channel (remote) or to bind (local).
        port: Control port (remote) or door port (local).
        remote: True to dial an existing daemon's control port; False to
            start a simulator in this process.
        timeout: Seconds of silence tolerated on a remote channel.
        **simulator_kwargs: Passed to :class:`DoorSimulator` when local.

    Leaving the block calls :meth:`SimulatorController.close`, which stops
    a local simulator and merely disconnects from a remote one.
    """
    if remote:
        controller: SimulatorController = RemoteSimulator(host, port, timeout)
        await controller.connect()  # type: ignore[attr-defined]
        try:
            yield controller
        finally:
            await controller.close()
        return

    from .commands import CommandHandler
    from .scripting import ScriptRunner
    from .server import DoorSimulator

    simulator = DoorSimulator(host=host, port=port, **simulator_kwargs)
    await simulator.start()
    runner = ScriptRunner(simulator)
    handler = CommandHandler(simulator, runner, lambda: None)
    runner.load_state_document = handler.load_state_document
    controller = LocalSimulator(simulator, handler)
    try:
        yield controller
    finally:
        await controller.close()
