# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Driving a simulator from Python: one owned locally, one reached remotely."""

from __future__ import annotations

import asyncio

import pytest

from powerpetdoor.const import DOOR_STATE_CLOSED, DOOR_STATE_KEEPUP
from powerpetdoor.simulator.cli import run_simulator
from powerpetdoor.simulator.control import (
    CommandFailedError,
    LocalSimulator,
    RemoteSimulator,
    SimulatorControlError,
    SimulatorController,
    simulator_control,
)
from powerpetdoor.simulator.prompt_common import escape_message, unescape_message


@pytest.fixture
async def local():
    async with simulator_control(host="127.0.0.1", port=0) as controller:
        yield controller


@pytest.fixture
async def daemon():
    """A daemon in this process, plus its control port."""
    ready = asyncio.Event()
    ports: dict[str, int] = {}

    def on_ready(door_port, control_port):
        ports["door"], ports["control"] = door_port, control_port
        ready.set()

    task = asyncio.create_task(
        run_simulator(
            host="127.0.0.1",
            port=0,
            daemon=True,
            control_port=0,
            control_host="127.0.0.1",
            on_ready=on_ready,
        )
    )
    await asyncio.wait_for(ready.wait(), timeout=10)
    yield ports
    if not task.done():
        async with simulator_control("127.0.0.1", ports["control"], remote=True) as sim:
            await sim.shutdown()
    await asyncio.wait_for(task, timeout=10)


class TestLocalController:
    async def test_it_drives_the_door(self, local):
        assert await local.open() == "Opening and holding"
        await local.simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        await local.close_door()
        await local.simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    async def test_a_failing_command_raises(self, local):
        with pytest.raises(CommandFailedError, match="Unknown command"):
            await local.execute("frobnicate")

    async def test_it_exposes_the_live_state_object(self, local):
        """The local half may: it is the same process and the same object.

        The remote half deliberately cannot - see the class below.
        """
        local.simulator.state.hold_time = 3

        assert local.simulator.state.hold_time == 3

    async def test_an_unknown_sensor_is_refused_before_it_reaches_the_door(self, local):
        with pytest.raises(SimulatorControlError, match="Unknown sensor"):
            await local.trigger("sideways")

    async def test_close_stops_the_simulator_it_owns(self):
        async with simulator_control(host="127.0.0.1", port=0) as sim:
            simulator = sim.simulator
        assert simulator._running is False


class TestRemoteController:
    async def test_it_drives_a_daemon(self, daemon):
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            assert await sim.open() == "Opening and holding"
            assert await sim.close_door() == "Closing door"

    async def test_there_is_no_remote_state_accessor(self, daemon):
        """Deliberate: over a socket a state can only be a snapshot, and a
        proxy that made a stale one look live would be the vendor app's own
        bug. Assert against a remote door by running a script instead."""
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            assert not hasattr(sim, "state")

    async def test_a_named_script_runs_by_name(self, daemon):
        """The half of remote scripting that was missing: `run <name> wait`
        already existed on the wire, the Python API for it did not."""
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            assert await sim.run_script("basic_cycle", wait=True) == (
                "Script PASSED: Basic Door Cycle"
            )

    async def test_a_failing_command_raises(self, daemon):
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            with pytest.raises(CommandFailedError, match="Unknown command"):
                await sim.execute("frobnicate")

    async def test_reset_by_path_is_refused_over_the_channel(self, daemon):
        """The daemon's path policy reaches the programmatic client too."""
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            with pytest.raises(CommandFailedError, match="not allowed over the control channel"):
                await sim.reset("/etc/passwd")

    async def test_close_leaves_the_daemon_running(self, daemon):
        """A client that dialled a daemon must not stop it by leaving scope."""
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            await sim.open()

        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            assert await sim.close_door() == "Closing door"

    async def test_shutdown_is_the_explicit_one(self, daemon):
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            await sim.shutdown()

        with pytest.raises(SimulatorControlError, match="Cannot reach"):
            async with simulator_control("127.0.0.1", daemon["control"], remote=True):
                pass

    async def test_unsolicited_log_lines_do_not_answer_a_command(self, daemon):
        """The daemon broadcasts LOG:/STATUS: lines whenever it likes; a
        command issued mid-chatter must still get its own answer."""
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            await sim.execute("debug on")
            try:
                for _ in range(5):
                    assert await sim.execute("status")
            finally:
                await sim.execute("debug off")


class TestRemoteFailureModes:
    async def test_connecting_to_nothing_is_a_clear_error(self, refused_port):
        with pytest.raises(SimulatorControlError, match="Cannot reach the control channel"):
            async with simulator_control("127.0.0.1", refused_port, remote=True):
                pass

    async def test_executing_before_connecting_is_refused(self):
        with pytest.raises(SimulatorControlError, match="Not connected"):
            await RemoteSimulator("127.0.0.1", 1).execute("status")

    async def test_a_newline_in_a_command_is_refused(self, daemon):
        """One command per line is the whole framing; an embedded newline
        would be read as a second command."""
        async with simulator_control("127.0.0.1", daemon["control"], remote=True) as sim:
            with pytest.raises(SimulatorControlError, match="cannot contain a newline"):
                await sim.execute("status\nshutdown")

    async def test_a_silent_channel_times_out(self, daemon, monkeypatch):
        remote = RemoteSimulator("127.0.0.1", daemon["control"], timeout=0.05)
        await remote.connect()
        try:
            monkeypatch.setattr(remote._reader, "readline", _never)
            with pytest.raises(SimulatorControlError, match="No response within"):
                await remote.execute("status")
        finally:
            await remote.close()

    async def test_a_channel_that_closes_mid_command_is_reported(self, daemon):
        remote = RemoteSimulator("127.0.0.1", daemon["control"])
        await remote.connect()
        try:
            monkeypatched = remote._reader
            original = monkeypatched.readline

            async def closed():
                await original()
                return b""

            monkeypatched.readline = closed  # type: ignore[method-assign]
            with pytest.raises(SimulatorControlError, match="closed without answering"):
                await remote.execute("status")
        finally:
            await remote.close()

    async def test_close_tolerates_a_peer_that_already_vanished(self, daemon, monkeypatch):
        """Teardown races against a network peer are ordinary, not errors.

        A `with` block unwinding while the daemon is already gone must not
        raise out of `close()` - that would replace whatever the body was
        actually failing on.
        """
        remote = RemoteSimulator("127.0.0.1", daemon["control"])
        await remote.connect()

        async def vanished():
            raise ConnectionResetError("peer went away")

        monkeypatch.setattr(remote._writer, "wait_closed", vanished)
        await remote.close()

        assert remote._writer is None

    async def test_closing_twice_is_harmless(self, daemon):
        remote = RemoteSimulator("127.0.0.1", daemon["control"])
        await remote.connect()

        await remote.close()
        await remote.close()


async def _never():
    await asyncio.sleep(3600)
    return b""  # pragma: no cover - the sleep is the point


class TestTheSharedSurface:
    """Both implementations answer the same calls."""

    @pytest.mark.parametrize(
        "method",
        [
            "execute",
            "open",
            "close_door",
            "cycle",
            "toggle",
            "trigger",
            "obstruction",
            "run_script",
            "stop_script",
            "reset",
            "shutdown",
            "close",
        ],
    )
    def test_both_implementations_provide_it(self, method):
        assert callable(getattr(LocalSimulator, method))
        assert callable(getattr(RemoteSimulator, method))
        assert hasattr(SimulatorController, method)

    def test_the_base_refuses_to_be_used_directly(self):
        """The two abstract members are abstract on purpose."""
        controller = SimulatorController()

        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop_policy()
            asyncio.run(controller.execute("status"))
        with pytest.raises(NotImplementedError):
            asyncio.run(controller.close())

    async def test_the_convenience_methods_build_the_lines_they_claim(self, local, monkeypatch):
        """Each wrapper exists to spell one command; pin the spelling."""
        sent: list[str] = []

        async def record(line):
            sent.append(line)
            return "ok"

        monkeypatch.setattr(local, "execute", record)

        await local.open()
        await local.close_door()
        await local.cycle()
        await local.toggle()
        await local.trigger("inside")
        await local.trigger("outside", 2)
        await local.obstruction()
        await local.obstruction(0)
        await local.run_script("s")
        await local.run_script("s", wait=False)
        await local.stop_script()
        await local.stop_script(all_queued=True)
        await local.reset()
        await local.reset("fixture")
        await local.shutdown()

        assert sent == [
            "open",
            "close",
            "cycle",
            "toggle",
            "inside",
            "outside 2",
            "obstruction",
            "obstruction 0",
            "run s wait",
            "run s",
            "stop",
            "stop all",
            "reset",
            "reset fixture",
            "shutdown",
        ]


class TestAMessageCannotForgeExtraLines:
    """The control channel escapes newlines so a message stays one line.

    `control.py` briefly carried its own unescaper that replaced `\\n`
    before `\\\\`, so a literal backslash followed by an `n` - an ordinary
    Windows path, `scripts\\new.yaml` - had its `n` eaten and a real line
    feed put in its place. That is the escaping defeated at the point it
    is undone: a caller logging the message gets a forged second record.

    `ctl.py` never had the bug because it imports the shared helper. This
    pins that both readers keep using it.
    """

    def test_a_backslash_survives_the_round_trip(self):
        for probe in (
            r"Unknown setting: scripts\new.yaml",
            r"C:\temp\nothing",
            "trailing backslash \\",
            "a real\nnewline",
        ):
            assert unescape_message(escape_message(probe)) == probe, probe

    def test_no_line_feed_is_manufactured(self):
        """The property the escaping exists for."""
        forged = unescape_message(escape_message(r"path\new"))
        assert "\n" not in forged

    def test_control_does_not_carry_its_own_unescaper(self):
        """A second copy is how the two answers diverged in the first place."""
        import powerpetdoor.simulator.control as control

        assert not hasattr(control, "_unescape"), (
            "control.py has re-grown a private unescaper; use prompt_common's"
        )
