# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Interactive-session tests for the simulator control client (ctl.py).

Covers both prompt paths (prompt_toolkit and the plain-input add_reader
fallback), STATUS/LOG routing, response handling, and failure modes. All
synchronization is event-based: tests wait on daemon-received commands or on
output written by the session - never on sleeps.

Note: sys.stdin/sys.stdout are replaced from inside the test bodies (via the
installer fixtures) because pytest's capture plugin re-installs its own
streams at call-phase start, which would silently undo fixture-time patches.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys

import pytest

from powerpetdoor.simulator import ctl

# ============================================================================
# Harness
# ============================================================================


class ScriptedDaemon:
    """Fake control daemon serving scripted greeting lines and replies.

    Each connection receives ``greeting`` immediately, then commands are
    answered from ``responses`` (commands absent from the mapping get no
    reply). Received command lines are recorded in an asyncio.Queue.

    ctl's check_connection probe also connects (and closes without sending);
    it records nothing because it never sends a command.
    """

    def __init__(self):
        self.greeting: bytes = b"STATUS: clients=0\n"
        self.responses: dict[str, bytes] = {}
        self.received: asyncio.Queue[str] = asyncio.Queue()
        self.server: asyncio.Server | None = None
        self.port: int = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        # On 3.13+ wait_closed also waits for connection handlers; keep
        # teardown bounded even if a failing test leaked a connection.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(5):
                await self.server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        try:
            writer.write(self.greeting)
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode().strip()
                await self.received.put(cmd)
                response = self.responses.get(cmd)
                if response:
                    writer.write(response)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def drain_received(self) -> list[str]:
        """Return all recorded commands without blocking."""
        commands = []
        while not self.received.empty():
            commands.append(self.received.get_nowait())
        return commands


class RecordingStdout(io.TextIOBase):
    """A sys.stdout replacement that supports awaiting on written content."""

    def __init__(self):
        super().__init__()
        self.text = ""
        self._event = asyncio.Event()

    def write(self, s: str) -> int:
        self.text += s
        self._event.set()
        return len(s)

    def flush(self) -> None:
        pass

    async def wait_for(self, needle: str, count: int = 1, timeout: float = 10.0) -> None:
        """Wait until ``needle`` has appeared ``count`` times in the output."""
        async with asyncio.timeout(timeout):
            while self.text.count(needle) < count:
                self._event.clear()
                await self._event.wait()


@pytest.fixture
async def daemon():
    d = ScriptedDaemon()
    yield d
    if d.server is not None:
        await d.stop()


@pytest.fixture
def piped_stdin(monkeypatch):
    """Installer: replace sys.stdin with a pipe; returns the write fd."""
    created: dict[str, object] = {}

    def install() -> int:
        read_fd, write_fd = os.pipe()
        stdin_file = os.fdopen(read_fd, "r")
        monkeypatch.setattr(sys, "stdin", stdin_file)
        created["write_fd"] = write_fd
        created["file"] = stdin_file
        return write_fd

    yield install
    if created:
        with contextlib.suppress(OSError):
            os.close(created["write_fd"])
        created["file"].close()


@pytest.fixture
def recorded_stdout(monkeypatch):
    """Installer: replace sys.stdout with a RecordingStdout and return it."""
    recorder = RecordingStdout()

    def install() -> RecordingStdout:
        monkeypatch.setattr(sys, "stdout", recorder)
        return recorder

    return install


@pytest.fixture
async def start_session():
    """Factory starting session tasks; leaked tasks are cancelled on teardown
    so a failing assertion can never hang fixture cleanup."""
    tasks: list[asyncio.Task] = []

    def factory(daemon: ScriptedDaemon, timeout: float = 2.0) -> asyncio.Task:
        task = asyncio.create_task(
            ctl.interactive_mode_async("127.0.0.1", daemon.port, daemon.port - 1, timeout, "none")
        )
        tasks.append(task)
        return task

    yield factory
    for task in tasks:
        if not task.done():
            task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 5)


def prompt_for(daemon: ScriptedDaemon) -> str:
    return f"127.0.0.1:{daemon.port - 1}> "


# ============================================================================
# Connection failures before the session starts
# ============================================================================


class TestInteractiveConnectFailures:
    """Failure paths before the interactive loop begins."""

    @pytest.mark.asyncio
    async def test_connection_refused_exits_1(self, capsys):
        # Grab a port with no listener by binding then closing
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        with pytest.raises(SystemExit) as exc_info:
            await ctl.interactive_mode_async("127.0.0.1", port, port - 1, 1.0, "none")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert f"Error: Connection refused - simulator not running on 127.0.0.1:{port}" in out

    @pytest.mark.asyncio
    async def test_open_connection_failure_exits_1(self, daemon, monkeypatch, capsys):
        """A failure between the probe and the persistent connect is reported."""
        await daemon.start()

        async def broken_open_connection(host, port):
            raise RuntimeError("boom")

        monkeypatch.setattr(asyncio, "open_connection", broken_open_connection)
        with pytest.raises(SystemExit) as exc_info:
            await ctl.interactive_mode_async("127.0.0.1", daemon.port, daemon.port - 1, 1.0, "none")
        assert exc_info.value.code == 1
        assert "Error connecting: boom" in capsys.readouterr().out


# ============================================================================
# Plain-input fallback sessions
# ============================================================================


class TestFallbackSession:
    """Full sessions over the add_reader stdin fallback."""

    @pytest.mark.asyncio
    async def test_full_session_flow(self, daemon, piped_stdin, recorded_stdout, start_session):
        """STATUS routing, LOG sanitization, OK/ERROR responses, empty lines,
        local commands, and exit - in one scripted session."""
        daemon.greeting = b"".join(
            [
                b"STATUS: clients=0\n",  # no change (already disconnected)
                b"STATUS: clients=1\n",  # change -> invalidate
                b"STATUS: clients=abc\n",  # unparseable count -> skipped
                b"STATUS: bootup complete\n",  # non-clients payload -> skipped
                b"NOISE: not a protocol line\n",  # unknown prefix -> skipped
                b"LOG: evil \x1b[2J log\n",  # must be sanitized on print
            ]
        )
        daemon.responses = {"status": b"OK: door fine\n", "bad": b"ERROR: nope\n"}
        await daemon.start()
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()
        prompt = prompt_for(daemon)

        task = start_session(daemon)
        await recorder.wait_for(prompt, count=1)
        os.write(stdin_fd, b"status\n")
        await recorder.wait_for(">>> door fine")

        await recorder.wait_for(prompt, count=2)
        os.write(stdin_fd, b"bad\n")
        await recorder.wait_for(">>> nope")

        await recorder.wait_for(prompt, count=3)
        os.write(stdin_fd, b"\n")  # empty line: re-prompt, nothing sent
        await recorder.wait_for(prompt, count=4)

        os.write(stdin_fd, b"help\n")  # local command: printed, not sent
        await recorder.wait_for(">>> Commands:")

        await recorder.wait_for(prompt, count=5)
        # Local command with an empty result message (clear writes its ANSI
        # sequence to the real stdout, never through the session output)
        os.write(stdin_fd, b"clear\n")
        await recorder.wait_for(prompt, count=6)

        os.write(stdin_fd, b"exit\n")
        await asyncio.wait_for(task, 10)

        out = recorder.text
        assert "Connected to simulator control port at 127.0.0.1" in out
        # The LOG line is printed with the ESC byte escaped, never raw
        assert "evil \\x1b[2J log" in out
        assert "\x1b" not in out
        # The unknown NOISE line was dropped, not printed
        assert "not a protocol line" not in out
        # Only the two daemon commands ever reached the daemon
        assert daemon.drain_received() == ["status", "bad"]

    @pytest.mark.asyncio
    async def test_response_timeout_message(
        self, daemon, piped_stdin, recorded_stdout, start_session
    ):
        """A daemon that never answers a command produces 'Response timeout'."""
        await daemon.start()
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()
        prompt = prompt_for(daemon)

        task = start_session(daemon, timeout=0.3)
        await recorder.wait_for(prompt, count=1)
        os.write(stdin_fd, b"slow\n")  # no scripted response
        await recorder.wait_for(">>> Response timeout")

        await recorder.wait_for(prompt, count=2)
        os.write(stdin_fd, b"exit\n")
        await asyncio.wait_for(task, 10)
        assert daemon.drain_received() == ["slow"]

    @pytest.mark.asyncio
    async def test_stale_responses_dropped_before_next_command(
        self, daemon, piped_stdin, recorded_stdout, start_session
    ):
        """An extra unsolicited OK: line must not be mistaken for the next
        command's response."""
        daemon.responses = {
            "first": b"OK: r1\nOK: stale\nLOG: sync-marker\n",
            "second": b"OK: r2\n",
        }
        await daemon.start()
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()
        prompt = prompt_for(daemon)

        task = start_session(daemon)
        await recorder.wait_for(prompt, count=1)
        os.write(stdin_fd, b"first\n")
        await recorder.wait_for(">>> r1")
        # The LOG marker proves the stale OK: is already queued client-side
        await recorder.wait_for("sync-marker")

        await recorder.wait_for(prompt, count=2)
        os.write(stdin_fd, b"second\n")
        await recorder.wait_for(">>> r2")
        assert ">>> stale" not in recorder.text

        await recorder.wait_for(prompt, count=3)
        os.write(stdin_fd, b"exit\n")
        await asyncio.wait_for(task, 10)

    @pytest.mark.asyncio
    async def test_stdin_eof_ends_session(
        self, daemon, piped_stdin, recorded_stdout, start_session
    ):
        """Closing stdin (EOF) ends the session while the daemon stays up."""
        await daemon.start()
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()
        task = start_session(daemon)
        await recorder.wait_for(prompt_for(daemon), count=1)
        os.close(stdin_fd)
        await asyncio.wait_for(task, 10)
        assert daemon.drain_received() == []


# ============================================================================
# Failure injection mid-session
# ============================================================================


class _WriterProxy:
    """Delegating writer that fails on a marked command and on wait_closed."""

    def __init__(self, writer):
        self._writer = writer

    def write(self, data: bytes) -> None:
        if data == b"doomed\n":
            raise RuntimeError("send failed")
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        raise RuntimeError("already closed")


class _FakeReader:
    """readline script: one OK (gated on the command being sent), then raise."""

    def __init__(self, command_sent: asyncio.Event):
        self._command_sent = command_sent
        self._calls = 0

    async def readline(self) -> bytes:
        self._calls += 1
        if self._calls == 1:
            await self._command_sent.wait()
            return b"OK: \n"  # empty response message
        raise RuntimeError("wire torn")


class _FakeWriter:
    """Writer whose only job is to signal when a command goes out."""

    def __init__(self, command_sent: asyncio.Event):
        self._command_sent = command_sent

    def write(self, data: bytes) -> None:
        self._command_sent.set()

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _TeardownErrorReader:
    """A reader whose in-flight read fails with a plain error when the
    session shuts down and cancels it (models transports that surface an
    OSError instead of propagating the cancellation)."""

    async def readline(self) -> bytes:
        try:
            await asyncio.Event().wait()  # blocks until cancelled
        except asyncio.CancelledError:
            raise RuntimeError("torn during shutdown") from None
        return b""  # pragma: no cover (readline only ends via cancellation)


class TestSessionFaults:
    """Mid-session failures must be reported and end or continue the session
    exactly as designed."""

    @pytest.mark.asyncio
    async def test_send_failure_is_reported_and_close_errors_swallowed(
        self, daemon, piped_stdin, recorded_stdout, monkeypatch, start_session
    ):
        daemon.responses = {"status": b"OK: still here\n"}
        await daemon.start()

        real_open_connection = asyncio.open_connection

        async def proxied_open_connection(host, port):
            reader, writer = await real_open_connection(host, port)
            return reader, _WriterProxy(writer)

        monkeypatch.setattr(asyncio, "open_connection", proxied_open_connection)
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()
        prompt = prompt_for(daemon)

        task = start_session(daemon)
        await recorder.wait_for(prompt, count=1)
        os.write(stdin_fd, b"doomed\n")
        await recorder.wait_for(">>> Error: send failed")

        # The session survives a failed send
        await recorder.wait_for(prompt, count=2)
        os.write(stdin_fd, b"status\n")
        await recorder.wait_for(">>> still here")

        await recorder.wait_for(prompt, count=3)
        os.write(stdin_fd, b"exit\n")
        # wait_closed raising in the finally block must not escape
        await asyncio.wait_for(task, 10)
        assert daemon.drain_received() == ["status"]

    @pytest.mark.asyncio
    async def test_reader_failure_ends_session(
        self, daemon, piped_stdin, recorded_stdout, monkeypatch, start_session
    ):
        """A socket-reader exception prints a connection error and ends the
        session; an empty OK response is not printed."""
        await daemon.start()  # only serves the check_connection probe

        command_sent = asyncio.Event()

        async def fake_open_connection(host, port):
            return _FakeReader(command_sent), _FakeWriter(command_sent)

        monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()

        task = start_session(daemon)
        await recorder.wait_for(prompt_for(daemon), count=1)
        os.write(stdin_fd, b"ping\n")
        result = await asyncio.wait_for(task, 10)

        assert result is None
        out = recorder.text
        assert ">>> Connection error: wire torn" in out
        # The empty OK response must not produce an empty '>>> ' line
        assert ">>> \n" not in out

    @pytest.mark.asyncio
    async def test_reader_error_during_shutdown_is_silent(
        self, daemon, piped_stdin, recorded_stdout, monkeypatch, start_session
    ):
        """A read error surfacing while the session is already shutting down
        must not print a spurious connection error."""
        await daemon.start()  # only serves the check_connection probe

        command_sent = asyncio.Event()

        async def fake_open_connection(host, port):
            return _TeardownErrorReader(), _FakeWriter(command_sent)

        monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()

        task = start_session(daemon)
        await recorder.wait_for(prompt_for(daemon), count=1)
        os.write(stdin_fd, b"exit\n")  # clean local exit cancels the reader
        result = await asyncio.wait_for(task, 10)

        assert result is None
        assert ">>> Connection error" not in recorder.text


# ============================================================================
# KeyboardInterrupt and cancellation
# ============================================================================


class TestInterruptsAndCancellation:
    @pytest.mark.asyncio
    async def test_ctrl_c_at_prompt_continues_session(
        self, daemon, piped_stdin, recorded_stdout, monkeypatch, start_session
    ):
        """A KeyboardInterrupt surfacing at the prompt re-prompts instead of
        exiting (simulated at the exact await the real SIGINT would hit)."""
        await daemon.start()

        real_wait = asyncio.wait
        calls = {"n": 0}

        async def interrupted_wait(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return await real_wait(*args, **kwargs)

        monkeypatch.setattr(asyncio, "wait", interrupted_wait)
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()

        task = start_session(daemon)
        # First prompt is interrupted; the loop must print a second prompt
        await recorder.wait_for(prompt_for(daemon), count=2)
        os.write(stdin_fd, b"exit\n")
        await asyncio.wait_for(task, 10)
        assert daemon.drain_received() == []

    @pytest.mark.asyncio
    async def test_ctrl_c_during_command_prints_exiting(
        self, daemon, piped_stdin, recorded_stdout, monkeypatch, start_session
    ):
        """A KeyboardInterrupt during command handling exits with 'Exiting.'"""
        await daemon.start()

        def interrupted_execute(self, line):
            raise KeyboardInterrupt

        monkeypatch.setattr(ctl.LocalCommandHandler, "execute", interrupted_execute)
        stdin_fd = piped_stdin()
        recorder = recorded_stdout()

        task = start_session(daemon)
        await recorder.wait_for(prompt_for(daemon), count=1)
        os.write(stdin_fd, b"exit\n")  # local command -> interrupted execute
        await asyncio.wait_for(task, 10)
        assert "Exiting." in recorder.text

    @pytest.mark.asyncio
    async def test_task_cancellation_breaks_cleanly(
        self, daemon, piped_stdin, recorded_stdout, start_session
    ):
        """Cancelling the session task while at the prompt runs the cleanup
        path instead of leaking the connection."""
        await daemon.start()
        piped_stdin()
        recorder = recorded_stdout()
        task = start_session(daemon)
        await recorder.wait_for(prompt_for(daemon), count=1)
        task.cancel()
        result = await asyncio.wait_for(task, 10)
        assert result is None
        assert task.cancelled() is False


# ============================================================================
# prompt_toolkit path
# ============================================================================


class TestPromptToolkitSession:
    """Drive the prompt_toolkit branch with a pipe input and dummy output."""

    @pytest.mark.asyncio
    async def test_toolkit_session_with_history_recall(self, daemon, monkeypatch, start_session):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        from powerpetdoor.simulator import prompt_common

        daemon.greeting = b"STATUS: clients=1\n"
        daemon.responses = {"status": b"OK: door fine\n"}
        await daemon.start()

        # Force the prompt_toolkit path despite the non-tty test stdin; the
        # actual input is the pipe input below.
        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: True)

        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                task = start_session(daemon)
                # Recall with no history: error is printed locally, nothing
                # is sent to the daemon
                pipe_input.send_text("!99\r")
                pipe_input.send_text("status\r")
                cmd = await asyncio.wait_for(daemon.received.get(), 10)
                assert cmd == "status"
                # !! recalls the last command and resends it
                pipe_input.send_text("!!\r")
                cmd = await asyncio.wait_for(daemon.received.get(), 10)
                assert cmd == "status"
                # exit is handled locally
                pipe_input.send_text("exit\r")
                await asyncio.wait_for(task, 10)

        assert daemon.drain_received() == []
