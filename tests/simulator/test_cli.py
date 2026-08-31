# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the simulator CLI (cli.py): control channel and entry point."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
    cli,
    prompt_common,
)
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.commands.base import get_command_registry
from powerpetdoor.simulator.commands.scripts import ScriptQueue
from powerpetdoor.simulator.prompt_common import (
    PROMPT_TOOLKIT_AVAILABLE,
    InteractiveSession,
)
from powerpetdoor.simulator.scripting import ScriptRunner

requires_prompt_toolkit = pytest.mark.skipif(
    not PROMPT_TOOLKIT_AVAILABLE, reason="prompt_toolkit not installed"
)

# ============================================================================
# Helpers / fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def _command_registry_guard():
    """Restore the global command registry after each test.

    Interactive runs call set_cli_mode(True), which mutates the global
    registry (removes 'exit', adds exit/q/quit aliases to shutdown) and is
    never undone by run_simulator. Without this guard, interactive tests
    would poison other tests in the same worker.
    """
    registry = get_command_registry()
    saved_entries = dict(registry)
    infos = {id(info): info for info in registry.values()}
    saved_aliases = {key: list(info.aliases) for key, info in infos.items()}
    yield
    registry.clear()
    registry.update(saved_entries)
    for key, info in infos.items():
        info.aliases = saved_aliases[key]


@pytest.fixture
def root_logger_guard():
    """Snapshot and restore root logger handlers/level.

    The prompt_toolkit interactive path swaps root logging handlers and does
    not restore them (the process normally exits afterward).
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


class PipeStdin:
    """A minimal stdin stand-in backed by a pipe read fd.

    readline() reads unbuffered, byte-at-a-time, so a line written to the
    pipe is consumed exactly once per reader callback (a buffered reader
    would slurp multiple lines and strand them outside the event loop's
    readiness notifications).

    The trailing newline is kept, because a real ``readline()`` returns
    ``""`` *only* at EOF - and EOF is what ends the session. Dropping it
    here made a bare Enter indistinguishable from EOF.
    """

    def __init__(self, read_fd: int):
        self._fd = read_fd

    def fileno(self) -> int:
        return self._fd

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        data = b""
        while True:
            ch = os.read(self._fd, 1)
            if not ch:
                break
            data += ch
            if ch == b"\n":
                break
        return data.decode()


class FakeTransport:
    """Recording fake for the transport behind a StreamWriter."""

    def __init__(self, write_buffer_size: int = 0):
        self._write_buffer_size = write_buffer_size

    def get_write_buffer_size(self) -> int:
        return self._write_buffer_size


class FakeStreamWriter:
    """Recording fake for asyncio.StreamWriter used in ControlChannel tests."""

    def __init__(
        self,
        *,
        fail_write=False,
        fail_close=False,
        fail_wait_closed=False,
        closing=False,
        write_buffer_size=0,
    ):
        self.data = b""
        self.closed = False
        self._fail_write = fail_write
        self._fail_close = fail_close
        self._fail_wait_closed = fail_wait_closed
        self._closing = closing
        self.transport = FakeTransport(write_buffer_size)

    def get_extra_info(self, name):
        return ("127.0.0.1", 55555)

    def is_closing(self) -> bool:
        return self._closing or self.closed

    def write(self, data: bytes):
        if self._fail_write:
            raise RuntimeError("write failed")
        self.data += data

    async def drain(self):
        pass

    def close(self):
        if self._fail_close:
            raise RuntimeError("close failed")
        self.closed = True

    async def wait_closed(self):
        if self._fail_wait_closed:
            raise ConnectionResetError("connection reset")

    def lines(self) -> list[str]:
        return self.data.decode().splitlines()


class FakeStreamReader:
    """Feeds a scripted list of lines, then EOF (b'').

    A ``ValueError`` in the list is *raised* rather than returned, which is
    what asyncio's ``readline()`` does for a line longer than ``limit``.
    """

    def __init__(self, lines: list[bytes | ValueError]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            item = self._lines.pop(0)
            if isinstance(item, ValueError):
                raise item
            return item
        return b""


PASSING_SCRIPT = """\
name: Passing Script
description: A trivially passing script
steps:
  - action: log
    message: hello
"""

#: A script that LOADS cleanly and fails while running, which is what
#: the oneshot reporting path is about. It used to fail on an unknown
#: action, but those are now refused at load time, and a load failure
#: takes a different path with a different message.
FAILING_SCRIPT = """\
name: Failing Script
description: A script that always fails
steps:
  - action: assert
    condition: door_open
"""


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
async def control_setup(timing_config, tmp_path):
    """A running simulator with a ControlChannel on ephemeral loopback ports.

    The command handler is configured exactly like daemon mode: script paths
    are NOT allowed, and tmp_path serves as the extra scripts directory.
    """
    channel_holder: list[cli.ControlChannel | None] = [None]

    def notify_status():
        if channel_holder[0]:
            channel_holder[0].broadcast_status()

    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    simulator = DoorSimulator(
        host="127.0.0.1",
        port=0,
        state=state,
        on_connect=notify_status,
        on_disconnect=notify_status,
    )
    await simulator.start()

    stop_event = asyncio.Event()
    handler = CommandHandler(
        simulator=simulator,
        script_runner=ScriptRunner(simulator),
        stop_callback=stop_event.set,
        scripts_dir=str(tmp_path),
        allow_script_paths=False,
    )
    channel = cli.ControlChannel(
        cmd_handler=handler,
        host="127.0.0.1",
        port=0,
        stop_event=stop_event,
        client_count=lambda: len(simulator.protocols),
    )
    await channel.start()
    channel_holder[0] = channel

    yield simulator, channel, tmp_path

    await channel.stop()
    await simulator.stop()


async def read_line_matching(reader, prefix: str, timeout: float = 5.0) -> str:
    """Read lines until one starts with the given prefix."""
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout)
        assert line, f"connection closed while waiting for {prefix!r} line"
        decoded = line.decode().rstrip("\n")
        if decoded.startswith(prefix):
            return decoded


async def read_line_containing(reader, needle: str, timeout: float = 5.0) -> str:
    """Read lines until one contains the given substring."""
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout)
        assert line, f"connection closed while waiting for line containing {needle!r}"
        decoded = line.decode().rstrip("\n")
        if needle in decoded:
            return decoded


# ============================================================================
# ControlChannel protocol behavior
# ============================================================================


class TestControlChannelProtocol:
    """Tests for the control-channel line protocol."""

    async def test_initial_status_line_on_connect(self, control_setup):
        """Connecting clients immediately receive a structured STATUS line."""
        _, channel, _ = control_setup
        reader, writer = await asyncio.open_connection("127.0.0.1", channel.bound_port)
        try:
            line = await read_line_matching(reader, "STATUS:")
            assert line == "STATUS: clients=0"
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_ok_response_is_single_escaped_line(self, control_setup):
        """Multi-line command output arrives as ONE line with \\n escapes."""
        _, channel, _ = control_setup
        reader, writer = await asyncio.open_connection("127.0.0.1", channel.bound_port)
        try:
            await read_line_matching(reader, "STATUS:")
            writer.write(b"status\n")
            await writer.drain()
            line = await read_line_matching(reader, "OK:")
            # The status output is multi-line; on the wire it must be escaped
            assert "\\n" in line
            assert "Door:" in line
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_error_response_for_unknown_command(self, control_setup):
        _, channel, _ = control_setup
        reader, writer = await asyncio.open_connection("127.0.0.1", channel.bound_port)
        try:
            await read_line_matching(reader, "STATUS:")
            writer.write(b"definitely_bogus\n")
            await writer.drain()
            line = await read_line_matching(reader, "ERROR:")
            assert "Unknown command" in line
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_log_broadcast_is_sanitized_and_single_line(self, control_setup):
        """Untrusted data in log records must not carry raw control characters
        or forge extra protocol lines."""
        _, channel, _ = control_setup
        reader, writer = await asyncio.open_connection("127.0.0.1", channel.bound_port)
        try:
            await read_line_matching(reader, "STATUS:")
            logging.getLogger("powerpetdoor.test").warning("evil \x1b[2J payload\nFORGED: line")
            line = await read_line_containing(reader, "evil")
            assert line.startswith("LOG: ")
            assert "\x1b" not in line
            assert "\\x1b" in line
            # The embedded newline is escaped into the same LOG line
            assert "FORGED: line" in line
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_status_broadcast_on_door_client_connect_and_disconnect(self, control_setup):
        """Door-client connects/disconnects produce STATUS lines - the
        structured signal ctl uses for prompt coloring."""
        simulator, channel, _ = control_setup
        door_port = simulator.server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", channel.bound_port)
        try:
            greeting = await read_line_matching(reader, "STATUS:")
            assert greeting == "STATUS: clients=0"

            door_reader, door_writer = await asyncio.open_connection("127.0.0.1", door_port)
            line = await read_line_matching(reader, "STATUS:")
            assert line == "STATUS: clients=1"

            door_writer.close()
            await door_writer.wait_closed()
            line = await read_line_matching(reader, "STATUS:")
            assert line == "STATUS: clients=0"
        finally:
            writer.close()
            await writer.wait_closed()


# ============================================================================
# Control-channel script restrictions (path traversal)
# ============================================================================


class TestControlChannelScriptRestrictions:
    """run over the control channel only accepts bare script names."""

    async def _run_over_channel(self, channel, command: str) -> str:
        reader, writer = await asyncio.open_connection("127.0.0.1", channel.bound_port)
        try:
            await read_line_matching(reader, "STATUS:")
            writer.write(f"{command}\n".encode())
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), 5)
                assert line
                decoded = line.decode().rstrip("\n")
                if decoded.startswith("OK:") or decoded.startswith("ERROR:"):
                    return decoded
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_absolute_path_rejected(self, control_setup, tmp_path):
        _, channel, _ = control_setup
        secret = tmp_path / "secret.yaml"
        secret.write_text(PASSING_SCRIPT)
        response = await self._run_over_channel(channel, f"run {secret}")
        assert response.startswith("ERROR:")
        assert "not allowed" in response

    async def test_relative_traversal_rejected(self, control_setup):
        _, channel, _ = control_setup
        response = await self._run_over_channel(channel, "run ../../etc/passwd")
        assert response.startswith("ERROR:")
        assert "not allowed" in response

    async def test_backslash_path_rejected(self, control_setup):
        _, channel, _ = control_setup
        response = await self._run_over_channel(channel, "run ..\\..\\secret")
        assert response.startswith("ERROR:")
        assert "not allowed" in response

    async def test_dotfile_rejected(self, control_setup):
        _, channel, _ = control_setup
        response = await self._run_over_channel(channel, "run .hidden")
        assert response.startswith("ERROR:")
        assert "not allowed" in response

    async def test_bare_name_resolves_in_scripts_dir(self, control_setup):
        """A bare name resolves against the configured scripts directory."""
        _, channel, scripts_dir = control_setup
        (scripts_dir / "myscript.yaml").write_text(PASSING_SCRIPT)
        response = await self._run_over_channel(channel, "run myscript")
        assert response.startswith("OK:")
        assert "PASSED" in response

    async def test_unknown_bare_name_lists_builtins(self, control_setup):
        _, channel, _ = control_setup
        response = await self._run_over_channel(channel, "run no_such_script")
        assert response.startswith("ERROR:")
        assert "Unknown script" in response


# ============================================================================
# run_simulator end-to-end (daemon mode)
# ============================================================================


class TestRunSimulatorDaemon:
    """End-to-end daemon-mode test of run_simulator."""

    async def test_daemon_end_to_end_shutdown_via_control(self, capsys):
        ready = asyncio.Event()
        ports: dict[str, int | None] = {}

        def on_ready(door_port, control_port):
            ports["door"] = door_port
            ports["control"] = control_port
            ready.set()

        task = asyncio.create_task(
            cli.run_simulator(
                host="127.0.0.1",
                port=0,
                daemon=True,
                control_port=0,
                control_host="127.0.0.1",
                on_ready=on_ready,
            )
        )
        await asyncio.wait_for(ready.wait(), 10)
        assert ports["control"] is not None

        reader, writer = await asyncio.open_connection("127.0.0.1", ports["control"])
        greeting = await read_line_matching(reader, "STATUS:")
        assert greeting == "STATUS: clients=0"

        writer.write(b"shutdown\n")
        await writer.drain()
        response = await read_line_matching(reader, "OK:")
        assert "Shutting down" in response

        result = await asyncio.wait_for(task, 10)
        assert result is None  # No scripts ran

        writer.close()

        out = capsys.readouterr().out
        # Startup banner includes host AND actual port (also for the channel)
        assert f"Simulator started on 127.0.0.1:{ports['door']}" in out
        assert f"Control channel: 127.0.0.1:{ports['control']}" in out


# ============================================================================
# Terminal log sanitization (interactive CLI)
# ============================================================================


class TestLogSanitization:
    """Log records printed to the interactive terminal are sanitized."""

    def test_sanitizing_formatter_neutralizes_escapes(self):
        formatter = cli._SanitizingFormatter("%(message)s")
        record = logging.LogRecord(
            "test", logging.WARNING, __file__, 1, "evil \x1b[2J payload", None, None
        )
        formatted = formatter.format(record)
        assert "\x1b" not in formatted
        assert "\\x1b" in formatted

    def test_interactive_prompt_handler_sanitizes(self, capsys):
        """The fallback-prompt logging handler must not pass ESC through."""
        prompt = cli.InteractivePrompt("$ ")
        prompt.enable()
        try:
            logging.getLogger("powerpetdoor.test_prompt").warning("evil \x1b[2J bye")
        finally:
            prompt.disable()
        out = capsys.readouterr().out
        # The prompt's own line-clearing uses ESC; the untrusted message's
        # ESC must be escaped, not printed raw
        assert "evil \x1b" not in out
        assert "evil \\x1b[2J bye" in out


# ============================================================================
# main() argument plumbing
# ============================================================================


class TestMainArguments:
    """Tests for the ppd-simulator entry point."""

    @pytest.fixture(autouse=True)
    def never_runs(self, monkeypatch):
        """Reaching run_simulator here is a failure, not a 60 s hang.

        Most of these tests call cli.main() directly and rely on an argparse
        error (or --list-scripts) to exit first - which is exactly what they
        assert, so it is exactly what a regression breaks. Without this they
        fall through into a real ``asyncio.run(run_simulator(...))``, binding
        the default simulator port inside a unit test and failing only via
        the pytest-timeout cap, with a confusing cause. Tests that mean to
        reach run_simulator patch over this afterwards.
        """

        async def _boom(**kwargs):
            raise AssertionError("cli.main() reached run_simulator; it should have exited first")

        monkeypatch.setattr(cli, "run_simulator", _boom)

    def _run_main(self, monkeypatch, argv, fake_run=None):
        captured: dict = {}
        # Also exposed on the instance so a caller that has to catch main()'s
        # SystemExit (every --oneshot invocation does) can still read it.
        self._captured = captured

        if fake_run is None:

            async def fake_run(**kwargs):
                captured.update(kwargs)
                return None

        monkeypatch.setattr(cli, "run_simulator", fake_run)
        monkeypatch.setattr(sys, "argv", argv)
        cli.main()
        return captured

    def test_control_host_defaults_to_loopback(self, monkeypatch):
        """Daemon mode must bind the control channel to 127.0.0.1 by default,
        even though the door server binds 0.0.0.0."""
        captured = self._run_main(monkeypatch, ["ppd-simulator", "--daemon"])
        assert captured["control_host"] == "127.0.0.1"
        assert captured["host"] == "0.0.0.0"
        assert captured["daemon"] is True
        assert captured["control_port"] == 3001

    def test_control_host_explicit_opt_in(self, monkeypatch):
        captured = self._run_main(
            monkeypatch, ["ppd-simulator", "--daemon", "--control-host", "0.0.0.0"]
        )
        assert captured["control_host"] == "0.0.0.0"

    def test_control_host_warning_in_help_text(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--help"])
        with pytest.raises(SystemExit):
            cli.main()
        help_text = capsys.readouterr().out
        assert "--control-host" in help_text
        assert "UNAUTHENTICATED" in help_text

    def test_scripts_dir_flag(self, monkeypatch, tmp_path):
        captured = self._run_main(
            monkeypatch,
            ["ppd-simulator", "--daemon", "--scripts-dir", str(tmp_path)],
        )
        assert captured["scripts_dir"] == str(tmp_path)

    def test_history_flag_accepted_without_prompt_toolkit(self, monkeypatch):
        """--history must be a valid flag even when prompt_toolkit is absent."""
        monkeypatch.setattr(cli, "PROMPT_TOOLKIT_AVAILABLE", False)
        captured = self._run_main(monkeypatch, ["ppd-simulator", "--daemon", "--history", "none"])
        assert captured["history_file"] is None

    def test_a_keyboard_interrupt_reaching_main_exits_130(self, monkeypatch, capsys):
        """main()'s handler, in isolation.

        On its own this asserts nothing about the shipped binary - the
        binary swallowed the cancellation so this handler was never
        entered. `TestTheRealBinaryUnderSIGINT` is what pins that half;
        this pins the exit code the handler chooses.
        """

        async def interrupted_run(**kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_simulator", interrupted_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--oneshot", "-s", "basic_cycle"])

        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 130
        assert "Simulator stopped." in capsys.readouterr().out

    def test_oneshot_without_a_verdict_exits_one(self, monkeypatch):
        """`--oneshot` that produced no result is a failure, not a success.

        `result is None` means the run never reached the end of its scripts.
        The old `and result is not None` guard fell through to exit **0**,
        which is what made an interrupted CI run report success. `--oneshot`
        without `--script` is refused at rc 2, so this state has no
        legitimate meaning other than "interrupted".
        """

        async def fake_run(**kwargs):
            return None

        monkeypatch.setattr(cli, "run_simulator", fake_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "-s", "x", "--oneshot"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 1

    def test_list_scripts_prints_builtins_without_running(self, capsys, monkeypatch):
        called = []

        async def fake_run(**kwargs):
            called.append(kwargs)
            return None

        monkeypatch.setattr(cli, "run_simulator", fake_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--list-scripts"])
        cli.main()
        out = capsys.readouterr().out
        # Same header the `list` command prints
        assert "Built-in scripts:" in out
        assert "  basic_cycle: " in out
        assert called == []

    def test_script_and_daemon_mutually_exclusive(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "-s", "basic_cycle", "--daemon"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "error: --script and --daemon are mutually exclusive" in err

    @pytest.mark.parametrize(
        ("flag", "message"),
        [
            (["--loop"], "error: --loop cannot be used without --script"),
            (["--script-delay", "1"], "error: --script-delay cannot be used without --script"),
            (["--oneshot"], "error: --oneshot cannot be used without --script"),
            (["--wait-for-client"], "error: --wait-for-client cannot be used without --script"),
        ],
    )
    def test_script_only_flags_rejected_without_script(self, capsys, monkeypatch, flag, message):
        """Silently ignoring a mode-scoped flag is a CI repeatability trap."""
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", *flag])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        assert message in capsys.readouterr().err

    def test_all_script_only_flags_listed_together(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--oneshot", "--loop"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        assert "error: --loop, --oneshot cannot be used without --script" in capsys.readouterr().err

    def test_script_only_flags_in_daemon_mode_say_so(self, capsys, monkeypatch):
        """--daemon --oneshot used to advise --script, which --daemon refuses."""
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--daemon", "--oneshot"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        assert "error: --oneshot is not available in daemon mode" in capsys.readouterr().err

    def test_control_host_rejected_without_daemon(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--control-host", "0.0.0.0"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        assert "error: --control-host requires --daemon" in capsys.readouterr().err

    def test_script_only_flags_accepted_with_script(self, monkeypatch):
        # `--oneshot` always exits explicitly now (the stub run returns no
        # verdict, so that is rc 1); the point here is the plumbing.
        with pytest.raises(SystemExit):
            captured = self._run_main(
                monkeypatch,
                ["ppd-simulator", "-s", "basic_cycle", "--oneshot", "--loop", "--wait-for-client"],
            )
        captured = self._captured
        assert captured["oneshot"] is True
        assert captured["loop_scripts"] is True
        assert captured["wait_for_client"] is True

    def test_list_scripts_shows_scripts_dir_entries(self, capsys, monkeypatch, tmp_path):
        """--list-scripts must show everything `run` can resolve."""
        (tmp_path / "my_custom.yaml").write_text(
            "name: My Custom Script\ndescription: Local extras\nsteps:\n  - action: log\n"
            "    message: ok\n"
        )
        monkeypatch.setattr(
            sys, "argv", ["ppd-simulator", "--list-scripts", "--scripts-dir", str(tmp_path)]
        )
        cli.main()
        out = capsys.readouterr().out
        # Same header the `list` command prints
        assert "Built-in scripts:" in out
        assert f"Scripts from {tmp_path}:" in out
        assert "  my_custom: Local extras" in out

    def test_list_scripts_shows_an_empty_scripts_dir_explicitly(
        self, capsys, monkeypatch, tmp_path
    ):
        """The flag's effect must be visible even when it finds nothing."""
        monkeypatch.setattr(
            sys, "argv", ["ppd-simulator", "--list-scripts", "--scripts-dir", str(tmp_path)]
        )
        cli.main()
        out = capsys.readouterr().out
        assert f"Scripts from {tmp_path}:\n  (none)\n" in out

    def test_missing_scripts_dir_is_rejected(self, capsys, monkeypatch, tmp_path):
        """A typo'd --scripts-dir used to be silently ignored."""
        missing = tmp_path / "nope"
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--scripts-dir", str(missing)])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        assert f"error: --scripts-dir {missing}: not a directory" in capsys.readouterr().err

    def test_daemon_explicit_control_port(self, monkeypatch):
        captured = self._run_main(monkeypatch, ["ppd-simulator", "--daemon", "4321"])
        assert captured["control_port"] == 4321

    def test_default_log_handler_sanitizes(self, monkeypatch, root_logger_guard):
        """--script (headless/CI) and --daemon kept the plain formatter.

        The two interactive paths install _SanitizingFormatter themselves,
        so headless and daemon modes were the only ones with no terminal
        escape protection at all - defence in depth for anything that is
        not sanitized at its source.
        """
        self._run_main(monkeypatch, ["ppd-simulator", "--daemon"])

        handlers = logging.getLogger().handlers
        assert handlers
        assert all(isinstance(h.formatter, cli._SanitizingFormatter) for h in handlers)

        record = logging.LogRecord(
            "test", logging.WARNING, __file__, 1, "evil \x1b[2J log", None, None
        )
        formatted = handlers[0].format(record)
        assert "\x1b" not in formatted
        assert "evil \\x1b[2J log" in formatted

    def test_firmware_parsed_to_tuple(self, monkeypatch):
        captured = self._run_main(monkeypatch, ["ppd-simulator", "--firmware", "2.5.7"])
        assert captured["firmware"] == (2, 5, 7)

    def test_firmware_wrong_part_count_rejected(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--firmware", "1.2"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "error: Firmware version must be in format major.minor.patch (e.g., '1.2.3')" in err

    def test_firmware_non_numeric_rejected(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--firmware", "a.b.c"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "error: Firmware version must contain only numbers (e.g., '1.2.3')" in err

    def test_hardware_parsed_to_tuple(self, monkeypatch):
        """Integers: a real door's fwInfo object is all ints."""
        captured = self._run_main(monkeypatch, ["ppd-simulator", "--hardware", "3.2"])
        assert captured["hardware"] == (3, 2)

    def test_hardware_non_numeric_rejected(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--hardware", "a.b"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "error: Hardware version must contain only numbers (e.g., '1.1')" in err

    def test_hardware_wrong_format_rejected(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--hardware", "1"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "error: Hardware version must be in format ver.rev (e.g., '1.1')" in err

    def test_oneshot_pass_exits_zero(self, monkeypatch):
        async def fake_run(**kwargs):
            return True

        monkeypatch.setattr(cli, "run_simulator", fake_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "-s", "x", "--oneshot"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 0

    def test_oneshot_fail_exits_one(self, monkeypatch):
        async def fake_run(**kwargs):
            return False

        monkeypatch.setattr(cli, "run_simulator", fake_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "-s", "x", "--oneshot"])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 1

    def test_script_flags_plumbed_through(self, monkeypatch):
        captured = self._run_main(
            monkeypatch,
            [
                "ppd-simulator",
                "--debug",
                "-s",
                "a",
                "-s",
                "b",
                "--loop",
                "--script-delay",
                "1.5",
                "--run-for",
                "2.5",
                "-w",
            ],
        )
        assert captured["scripts"] == ["a", "b"]
        assert captured["loop_scripts"] is True
        assert captured["script_delay"] == 1.5
        assert captured["run_for"] == 2.5
        assert captured["wait_for_client"] is True
        assert captured["oneshot"] is False
        assert captured["daemon"] is False
        assert captured["control_port"] is None


# ============================================================================
# InteractivePrompt (basic-input fallback prompt)
# ============================================================================


class TestInteractivePrompt:
    """Unit tests for the fallback InteractivePrompt."""

    def test_show_and_clear_do_nothing_when_disabled(self, capsys):
        prompt = cli.InteractivePrompt("$ ")
        prompt.show()
        prompt.clear_line()
        assert capsys.readouterr().out == ""

    def test_output_clears_line_prints_and_reshows_prompt(self, capsys, monkeypatch):
        """On a terminal, output() erases the prompt line before printing."""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        prompt = cli.InteractivePrompt("$ ")
        prompt.enable()
        try:
            prompt.output("hello")
        finally:
            prompt.disable()
        # enable() shows the prompt; output() clears the line, prints, reshows
        assert capsys.readouterr().out == "$ \r\033[Khello\n$ "

    def test_output_emits_no_ansi_off_a_terminal(self, capsys):
        """Piped output must stay free of escape sequences.

        pytest's capture replaces stdout with a non-tty, which is exactly
        the piped/`TERM=dumb` case the fallback prompt exists for.
        """
        prompt = cli.InteractivePrompt("$ ")
        prompt.enable()
        try:
            prompt.output("hello")
        finally:
            prompt.disable()
        out = capsys.readouterr().out
        assert out == "$ hello\n$ "
        assert "\033" not in out

    def test_enable_twice_is_idempotent(self):
        root = logging.getLogger()
        prompt = cli.InteractivePrompt("$ ")
        prompt.enable()
        try:
            handlers_after_first = list(root.handlers)
            saved_after_first = list(prompt._saved_handlers)
            prompt.enable()
            assert list(root.handlers) == handlers_after_first
            assert list(prompt._saved_handlers) == saved_after_first
        finally:
            prompt.disable()

    def test_disable_without_enable_is_noop(self):
        root = logging.getLogger()
        before = list(root.handlers)
        cli.InteractivePrompt("$ ").disable()
        assert list(root.handlers) == before

    def test_disable_restores_exact_handlers(self):
        root = logging.getLogger()
        before = list(root.handlers)
        prompt = cli.InteractivePrompt("$ ")
        prompt.enable()
        assert isinstance(root.handlers[-1], cli._PromptLoggingHandler)
        prompt.disable()
        assert list(root.handlers) == before

    def test_logging_handler_format_error_calls_handle_error(self, capsys):
        """A bad log record must not crash the handler (handleError path)."""
        prompt = cli.InteractivePrompt("$ ")
        prompt.enable()
        try:
            # %d with a non-number makes Formatter.format raise inside emit
            logging.getLogger("powerpetdoor.test_prompt_err").warning("%d", "not-a-number")
        finally:
            prompt.disable()
        assert "--- Logging error ---" in capsys.readouterr().err


# ============================================================================
# Non-TTY status output
# ============================================================================


class TestStatusPrint:
    """Operator-facing status lines must survive a redirected stdout."""

    def test_status_print_flushes(self, monkeypatch):
        """stdout is block-buffered off a terminal; the banner and script
        progress would otherwise appear after the fact, or die with the
        process on SIGTERM."""
        flushes: list[bool] = []

        class RecordingStdout(io.StringIO):
            def flush(self):
                flushes.append(True)
                super().flush()

        stream = RecordingStdout()
        monkeypatch.setattr(sys, "stdout", stream)
        cli.status_print("Simulator started on 0.0.0.0:3000")

        assert stream.getvalue() == "Simulator started on 0.0.0.0:3000\n"
        assert flushes  # print(..., flush=True)

    def test_status_print_blank_line(self, capsys):
        cli.status_print()
        assert capsys.readouterr().out == "\n"


# ============================================================================
# _ControlLogHandler (daemon log broadcast)
# ============================================================================


class TestControlLogHandler:
    def _record(self, msg, args=None):
        return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)

    def test_broken_writer_does_not_stop_broadcast(self):
        good = FakeStreamWriter()
        bad = FakeStreamWriter(fail_write=True)
        clients = {good, bad}
        handler = cli._ControlLogHandler(clients)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.emit(self._record("hi"))
        assert good.data == b"LOG: hi\n"
        # A writer that raised is dropped, not retried on every record
        assert clients == {good}

    def test_closing_writer_is_dropped_without_writing(self):
        """A peer that died mid-stream is only noticed here."""
        good = FakeStreamWriter()
        dead = FakeStreamWriter(closing=True)
        clients = {good, dead}
        handler = cli._ControlLogHandler(clients)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.emit(self._record("hi"))
        assert dead.data == b""
        assert clients == {good}

    def test_records_are_dropped_for_a_client_with_a_runaway_backlog(self):
        """A parked ctl session must not grow the daemon's heap.

        `emit` cannot `drain()`, so an attached-but-not-reading client
        queued every record in daemon memory - measured at +0.16 MB/s under
        a hostile dribble, tracking the log rate with no bound in sight.
        """
        reading = FakeStreamWriter()
        stalled = FakeStreamWriter(write_buffer_size=cli._ControlLogHandler.MAX_CLIENT_BACKLOG + 1)
        clients = {reading, stalled}
        handler = cli._ControlLogHandler(clients)
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(self._record("hi"))

        assert reading.data == b"LOG: hi\n"
        assert stalled.data == b""
        # Dropped, not disconnected: the session recovers when it reads.
        assert clients == {reading, stalled}

    def test_a_client_at_the_backlog_limit_still_receives(self):
        """The threshold is a ceiling, not a hair trigger."""
        writer = FakeStreamWriter(write_buffer_size=cli._ControlLogHandler.MAX_CLIENT_BACKLOG)
        handler = cli._ControlLogHandler({writer})
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(self._record("hi"))

        assert writer.data == b"LOG: hi\n"

    def test_emit_refuses_to_re_enter(self):
        """asyncio logs from inside write(); rebroadcasting that is the loop.

        The scenario (a ctl client killed / piped to head): the writer stays
        open but every write emits a root-logger WARNING. Without the guard
        this recurses until the stack blows; with it, exactly one record is
        broadcast per real record.
        """
        records: list[str] = []

        class LoggingWriter(FakeStreamWriter):
            def write(self, data: bytes):
                super().write(data)
                records.append(data.decode())
                # What asyncio's selector transport does once _conn_lost
                # passes its threshold.
                logging.getLogger("asyncio").warning("socket.send() raised exception.")

        writer = LoggingWriter()
        handler = cli._ControlLogHandler({writer})
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            logging.getLogger("powerpetdoor.test").warning("one real record")
        finally:
            root.removeHandler(handler)

        assert records == ["LOG: one real record\n"]

    def test_format_error_calls_handle_error(self, capsys):
        good = FakeStreamWriter()
        handler = cli._ControlLogHandler({good})
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.emit(self._record("%d", ("not-a-number",)))
        assert good.data == b""
        assert "--- Logging error ---" in capsys.readouterr().err


# ============================================================================
# ControlChannel edge cases (direct _handle_client with fakes)
# ============================================================================


def make_channel(execute=None, stop_event=None, client_count=None):
    """Build a ControlChannel around a stub command handler (server not started)."""

    async def default_execute(cmd):
        return SimpleNamespace(success=True, message=f"ran {cmd}")

    handler = SimpleNamespace(execute=execute or default_execute)
    return cli.ControlChannel(
        cmd_handler=handler,
        host="127.0.0.1",
        port=4567,
        stop_event=stop_event or asyncio.Event(),
        client_count=client_count,
    )


class TestControlChannelEdges:
    def test_bound_port_before_start_returns_configured_port(self):
        assert make_channel().bound_port == 4567

    async def test_handle_client_answers_empty_lines(self, caplog):
        """A blank line is refused, not dropped.

        Skipping it meant no answer could ever come, so
        `ppd-simulator-ctl ""` sat out the whole `--timeout` and then
        advised raising it - advice that is wrong in both halves. A shell
        wrapper expanding an unset variable lands here. One line closes it.
        """
        executed = []

        async def execute(cmd):
            executed.append(cmd)
            return SimpleNamespace(success=True, message=f"ran {cmd}")

        channel = make_channel(execute)
        reader = FakeStreamReader([b"\n", b"   \n", b"ping\n"])
        writer = FakeStreamWriter()
        with caplog.at_level(logging.INFO, logger="powerpetdoor.simulator.cli"):
            await channel._handle_client(reader, writer)
        assert executed == ["ping"]
        assert writer.lines() == [
            "STATUS: clients=0",
            "ERROR: Empty command",
            "ERROR: Empty command",
            "OK: ran ping",
        ]
        assert writer.closed is True
        assert "Control connection closed" in caplog.text

    async def test_handle_client_refuses_an_over_long_line_without_an_error_record(self, caplog):
        """asyncio's `readline()` raises `ValueError` past `limit`.

        It used to escape to the generic `except Exception` below and log
        "Control client error: Separator is found, but chunk is longer than
        limit" at **ERROR** - which `_ControlLogHandler` then broadcast into
        every other operator's `ctl` session, while the sender saw only
        "Connection closed without response". asyncio consumes through the
        newline before raising, so the connection is still usable and the
        next command is answered.
        """
        executed = []

        async def execute(cmd):
            executed.append(cmd)
            return SimpleNamespace(success=True, message=f"ran {cmd}")

        channel = make_channel(execute)
        reader = FakeStreamReader(
            [ValueError("Separator is found, but chunk is longer than limit"), b"ping\n"]
        )
        writer = FakeStreamWriter()
        with caplog.at_level(logging.INFO, logger="powerpetdoor.simulator.cli"):
            await channel._handle_client(reader, writer)

        assert executed == ["ping"]
        assert writer.lines() == [
            "STATUS: clients=0",
            f"ERROR: Command line too long (max {cli.MAX_CONTROL_LINE} bytes)",
            "OK: ran ping",
        ]
        assert [r.levelno for r in caplog.records if "over-long" in r.getMessage()] == [
            logging.INFO
        ]
        assert "Control client error" not in caplog.text

    async def test_handle_client_error_result(self):
        async def execute(cmd):
            return SimpleNamespace(success=False, message="no such thing")

        channel = make_channel(execute)
        writer = FakeStreamWriter()
        await channel._handle_client(FakeStreamReader([b"bogus\n"]), writer)
        assert "ERROR: no such thing" in writer.lines()

    async def test_handle_client_escapes_multiline_response(self):
        async def execute(cmd):
            return SimpleNamespace(success=True, message="line1\nline2")

        channel = make_channel(execute)
        writer = FakeStreamWriter()
        await channel._handle_client(FakeStreamReader([b"status\n"]), writer)
        assert "OK: line1\\nline2" in writer.lines()

    async def test_handle_client_stops_after_stop_event(self):
        stop_event = asyncio.Event()
        executed = []

        async def execute(cmd):
            executed.append(cmd)
            stop_event.set()
            return SimpleNamespace(success=True, message="bye")

        channel = make_channel(execute, stop_event=stop_event)
        writer = FakeStreamWriter()
        await channel._handle_client(FakeStreamReader([b"shutdown\n", b"after\n"]), writer)
        # The loop breaks after the stop event; the second command never runs
        assert executed == ["shutdown"]
        assert writer.closed is True

    async def test_handle_client_exception_logged_and_connection_closed(self, caplog):
        async def execute(cmd):
            raise RuntimeError("kaboom")

        channel = make_channel(execute)
        writer = FakeStreamWriter()
        with caplog.at_level(logging.ERROR, logger="powerpetdoor.simulator.cli"):
            await channel._handle_client(FakeStreamReader([b"boom\n"]), writer)
        assert "Control client error: kaboom" in caplog.text
        assert writer.closed is True
        assert writer not in channel.clients

    async def test_normal_hang_up_is_not_an_error(self, caplog):
        """A one-shot ctl exiting mid-write is not an ERROR.

        Essentially every one-shot `run`/`stop` produced one - the client
        reads its `OK:` line and exits while the daemon is still emitting
        log lines for that command - and `_ControlLogHandler` broadcast the
        bogus ERROR to every other ctl session.
        """

        class HangingUpWriter(FakeStreamWriter):
            def write(self, data: bytes):
                raise BrokenPipeError(32, "Broken pipe")

        channel = make_channel()
        writer = HangingUpWriter()
        with caplog.at_level(logging.DEBUG, logger="powerpetdoor.simulator.cli"):
            await channel._handle_client(FakeStreamReader([b"ping\n"]), writer)

        assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert "hung up" in caplog.text
        assert writer.closed is True
        assert writer not in channel.clients

    async def test_handle_client_wait_closed_error_swallowed(self, caplog):
        channel = make_channel()
        writer = FakeStreamWriter(fail_wait_closed=True)
        with caplog.at_level(logging.INFO, logger="powerpetdoor.simulator.cli"):
            await channel._handle_client(FakeStreamReader([]), writer)
        assert writer.closed is True
        assert "Control connection closed" in caplog.text

    def test_broadcast_status_swallows_write_errors(self):
        channel = make_channel()  # default client_count -> 0
        good = FakeStreamWriter()
        bad = FakeStreamWriter(fail_write=True)
        channel.clients.update({good, bad})
        channel.broadcast_status()
        assert good.data == b"STATUS: clients=0\n"
        assert channel.clients == {good}

    def test_broadcast_status_drops_closing_writers(self):
        channel = make_channel()
        good = FakeStreamWriter()
        dead = FakeStreamWriter(closing=True)
        channel.clients.update({good, dead})
        channel.broadcast_status()
        assert dead.data == b""
        assert channel.clients == {good}

    async def test_stop_twice_is_safe(self, control_setup):
        _, channel, _ = control_setup
        await channel.stop()
        await channel.stop()  # log_handler and server are both None now
        assert channel.server is None

    async def test_stop_closes_lingering_clients(self, control_setup):
        _, channel, _ = control_setup
        reader, writer = await asyncio.open_connection("127.0.0.1", channel.bound_port)
        try:
            await read_line_matching(reader, "STATUS:")
            # A client whose close() raises must not break shutdown
            channel.clients.add(FakeStreamWriter(fail_close=True))
            await channel.stop()
            eof = await asyncio.wait_for(reader.readline(), 5)
            assert eof == b""
        finally:
            writer.close()


# ============================================================================
# _build_state (firmware/hardware overrides)
# ============================================================================


class TestBuildState:
    def test_no_overrides_returns_none(self):
        assert cli._build_state(None, None) is None

    def test_firmware_only(self):
        state = cli._build_state((9, 8, 7), None)
        assert isinstance(state, DoorSimulatorState)
        assert (state.fw_major, state.fw_minor, state.fw_patch) == (9, 8, 7)

    def test_hardware_only(self):
        state = cli._build_state(None, (3, 2))
        assert (state.hw_ver, state.hw_rev) == (3, 2)

    def test_firmware_and_hardware(self):
        state = cli._build_state((1, 2, 3), (4, 5))
        assert (state.fw_major, state.fw_minor, state.fw_patch) == (1, 2, 3)
        assert (state.hw_ver, state.hw_rev) == (4, 5)


# ============================================================================
# _process_script_queue (queued 'run' commands)
# ============================================================================


class TestProcessScriptQueue:
    def _stubs(self, run_result=True, load_error=None):
        ran = asyncio.Event()
        runs = []

        def load_script(ref):
            if load_error is not None:
                raise load_error
            return SimpleNamespace(name=f"Script-{ref}")

        async def run(script, on_start=None):
            if on_start is not None and not on_start():
                return False
            runs.append(script.name)
            ran.set()
            return run_result

        handler = SimpleNamespace(load_script=load_script)
        runner = SimpleNamespace(run=run)
        return handler, runner, ran, runs

    async def test_runs_queued_script_and_logs_pass(self, caplog):
        caplog.set_level(logging.INFO, logger="powerpetdoor.simulator.cli")
        handler, runner, ran, runs = self._stubs(run_result=True)
        queue = ScriptQueue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            cli._process_script_queue(queue, stop, handler, runner, poll_interval=0.01)
        )
        await queue.put("good", "Script-good")
        await asyncio.wait_for(ran.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert runs == ["Script-good"]
        assert "Running queued script: Script-good" in caplog.text
        assert "Script PASSED: Script-good" in caplog.text

    async def test_failed_script_logged(self, caplog):
        caplog.set_level(logging.INFO, logger="powerpetdoor.simulator.cli")
        handler, runner, ran, _ = self._stubs(run_result=False)
        queue = ScriptQueue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            cli._process_script_queue(queue, stop, handler, runner, poll_interval=0.01)
        )
        await queue.put("bad", "Script-bad")
        await asyncio.wait_for(ran.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert "Script FAILED: Script-bad" in caplog.text

    async def test_load_error_logged_and_loop_continues(self, caplog):
        caplog.set_level(logging.ERROR, logger="powerpetdoor.simulator.cli")
        handler, runner, _, runs = self._stubs(load_error=ValueError("nope"))
        queue = ScriptQueue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            cli._process_script_queue(queue, stop, handler, runner, poll_interval=0.01)
        )
        await queue.put("broken", "Script-broken")
        # Deterministic: wait until the error record has been emitted
        async with asyncio.timeout(5):
            while "Error running queued script: nope" not in caplog.text:
                await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert runs == []

    @staticmethod
    def _blocking_runner(release, started, depth_while_blocked, depth_after_start, queue):
        """A runner stub that parks on `release` the way the run lock does."""

        async def run(script, on_start=None):
            # Stand in for waiting on the run lock: the entry is dequeued
            # but has not started, so it must still be reported as pending.
            depth_while_blocked.append(queue.qsize())
            await release.wait()
            proceed = on_start is None or on_start()
            # 0 only if on_start released the claim; the consumer's own
            # `finally` cannot be what satisfies this.
            depth_after_start.append(queue.qsize())
            started.set()
            return proceed

        return run

    async def test_claim_is_released_only_once_the_run_starts(self):
        """A dequeued run stays counted until it actually starts."""
        queue = ScriptQueue()
        stop = asyncio.Event()
        started = asyncio.Event()
        release = asyncio.Event()
        depth_while_blocked: list[int] = []
        depth_after_start: list[int] = []

        def load_script(ref):
            return SimpleNamespace(name=f"Script-{ref}")

        task = asyncio.create_task(
            cli._process_script_queue(
                queue,
                stop,
                SimpleNamespace(load_script=load_script),
                SimpleNamespace(
                    run=self._blocking_runner(
                        release, started, depth_while_blocked, depth_after_start, queue
                    )
                ),
                poll_interval=0.01,
            )
        )
        await queue.put("waiting", "Waiting Script")
        async with asyncio.timeout(5):
            while not depth_while_blocked:
                await asyncio.sleep(0)

        assert depth_while_blocked == [1]
        assert queue.pending() == ["Waiting Script"]

        release.set()
        await asyncio.wait_for(started.wait(), 5)
        # Released by on_start, inside the run - not by the consumer's finally.
        assert depth_after_start == [0]
        assert queue.qsize() == 0

        stop.set()
        await asyncio.wait_for(task, 5)

    async def test_a_claim_dropped_by_stop_all_never_runs(self, caplog):
        """`stop all` in the claim window abandons the run.

        The consumer is parked on the run lock, so the entry is claimed but
        not started. `clear()` cancels it, `on_start` reports that, and the
        runner returns without executing a step - instead of starting the
        run the operator was just told had been dropped.
        """
        caplog.set_level(logging.INFO, logger="powerpetdoor.simulator.cli")
        queue = ScriptQueue()
        stop = asyncio.Event()
        started = asyncio.Event()
        release = asyncio.Event()
        depth_while_blocked: list[int] = []
        depth_after_start: list[int] = []
        steps_run: list[str] = []

        def load_script(ref):
            return SimpleNamespace(name=f"Script-{ref}")

        async def run(script, on_start=None):
            depth_while_blocked.append(queue.qsize())
            await release.wait()
            if on_start is not None and not on_start():
                started.set()
                return False
            steps_run.append(script.name)
            depth_after_start.append(queue.qsize())
            started.set()
            return True

        task = asyncio.create_task(
            cli._process_script_queue(
                queue,
                stop,
                SimpleNamespace(load_script=load_script),
                SimpleNamespace(run=run),
                poll_interval=0.01,
            )
        )
        await queue.put("claimed", "Claimed Script")
        async with asyncio.timeout(5):
            while not depth_while_blocked:
                await asyncio.sleep(0)

        assert queue.clear() != []  # the claimed entry is what is dropped
        release.set()
        await asyncio.wait_for(started.wait(), 5)

        assert steps_run == []
        assert queue.qsize() == 0
        assert "Dropped queued script: Script-claimed" in caplog.text
        assert "Script PASSED" not in caplog.text
        assert "Script FAILED" not in caplog.text

        stop.set()
        await asyncio.wait_for(task, 5)

    async def test_claim_is_released_when_loading_fails(self):
        """A load failure never reaches on_start, so the finally must clear it."""
        queue = ScriptQueue()
        stop = asyncio.Event()

        def load_script(ref):
            raise ValueError("nope")

        task = asyncio.create_task(
            cli._process_script_queue(
                queue,
                stop,
                SimpleNamespace(load_script=load_script),
                SimpleNamespace(run=None),
                poll_interval=0.01,
            )
        )
        await queue.put("broken", "Script-broken")
        async with asyncio.timeout(5):
            while queue.qsize():
                await asyncio.sleep(0)

        assert queue.pending() == []
        stop.set()
        await asyncio.wait_for(task, 5)

    async def test_returns_immediately_when_already_stopped(self):
        handler, runner, _, runs = self._stubs()
        stop = asyncio.Event()
        stop.set()
        await cli._process_script_queue(ScriptQueue(), stop, handler, runner)
        assert runs == []

    async def test_poll_timeout_then_stop_exits_loop(self):
        handler, runner, _, runs = self._stubs()
        stop = asyncio.Event()
        task = asyncio.create_task(
            cli._process_script_queue(ScriptQueue(), stop, handler, runner, poll_interval=0.01)
        )
        await asyncio.sleep(0)  # let the task enter its poll wait
        stop.set()
        # The task exits after its poll interval times out and rechecks stop
        assert await asyncio.wait_for(task, 5) is None
        assert runs == []

    async def test_cancelled_while_waiting_breaks_cleanly(self):
        handler, runner, _, _ = self._stubs()
        stop = asyncio.Event()
        task = asyncio.create_task(
            cli._process_script_queue(ScriptQueue(), stop, handler, runner, poll_interval=60)
        )
        await asyncio.sleep(0)  # let the task enter its poll wait
        task.cancel()
        # The CancelledError is caught by the loop's break -> normal return
        assert await asyncio.wait_for(task, 5) is None


# ============================================================================
# _run_startup_scripts (startup script sequencing)
# ============================================================================


class TestRunStartupScripts:
    def _make(self, run_results=None, side_effect=None, load_error=None):
        """Build stub simulator/handler/runner and shared state."""
        sim = SimpleNamespace(protocols=[])
        stop = asyncio.Event()
        result: list = [None]
        runs: list[str] = []
        started = asyncio.Event()

        def load_script(ref):
            if load_error is not None:
                raise load_error
            return SimpleNamespace(name=ref)

        async def run(script):
            started.set()
            runs.append(script.name)
            if side_effect:
                return await side_effect(script, sim)
            if run_results is None:
                return True
            return run_results.pop(0)

        handler = SimpleNamespace(load_script=load_script)
        runner = SimpleNamespace(run=run)
        return sim, handler, runner, stop, result, runs, started

    async def _run(self, scripts, sim, handler, runner, stop, result, **kwargs):
        defaults = {
            "loop_scripts": False,
            "script_delay": 0,
            "oneshot": False,
            "wait_for_client": False,
        }
        defaults.update(kwargs)
        await cli._run_startup_scripts(
            scripts,
            simulator=sim,
            cmd_handler=handler,
            script_runner=runner,
            stop_event=stop,
            script_result=result,
            **defaults,
        )

    async def test_single_passing_script_oneshot(self, capsys):
        sim, handler, runner, stop, result, runs, _ = self._make()
        await self._run(["s1"], sim, handler, runner, stop, result, oneshot=True)
        assert runs == ["s1"]
        assert result[0] is True
        assert stop.is_set()
        out = capsys.readouterr().out
        assert ">>> Running script: s1" in out
        assert ">>> Script PASSED: s1" in out
        assert ">>> All scripts PASSED" in out

    async def test_failing_script_oneshot(self, capsys):
        sim, handler, runner, stop, result, runs, _ = self._make(run_results=[False])
        await self._run(["s1"], sim, handler, runner, stop, result, oneshot=True)
        assert result[0] is False
        assert stop.is_set()
        out = capsys.readouterr().out
        assert ">>> Script FAILED: s1" in out
        assert ">>> All scripts FAILED" in out

    async def test_load_error_marks_failure(self, capsys):
        sim, handler, runner, stop, result, runs, _ = self._make(load_error=ValueError("nope"))
        await self._run(["s1"], sim, handler, runner, stop, result)
        assert runs == []
        assert result[0] is False
        assert "Error running script 's1': nope" in capsys.readouterr().out

    async def test_delay_between_scripts_not_oneshot(self, capsys):
        sim, handler, runner, stop, result, runs, _ = self._make()
        await self._run(["s1", "s2"], sim, handler, runner, stop, result, script_delay=0.01)
        assert runs == ["s1", "s2"]
        assert result[0] is True
        assert not stop.is_set()  # oneshot=False leaves the simulator running
        out = capsys.readouterr().out
        assert ">>> Waiting 0.01s before next script..." in out
        assert ">>> All scripts" not in out

    async def test_wait_for_client_returns_early_when_stopped(self, capsys):
        sim, handler, runner, stop, result, runs, _ = self._make()
        stop.set()
        await self._run(["s1"], sim, handler, runner, stop, result, wait_for_client=True)
        assert runs == []
        # The finally block still records the (vacuous) overall result
        assert result[0] is True
        assert ">>> Waiting for client connection..." in capsys.readouterr().out

    async def test_wait_for_client_starts_after_connect(self, capsys):
        sim, handler, runner, stop, result, runs, _ = self._make()
        task = asyncio.create_task(
            self._run(["s1"], sim, handler, runner, stop, result, wait_for_client=True)
        )
        await asyncio.sleep(0)  # task prints the waiting banner and polls
        sim.protocols.append("client")
        await asyncio.wait_for(task, 5)
        assert runs == ["s1"]
        assert result[0] is True
        out = capsys.readouterr().out
        assert ">>> Waiting for client connection..." in out
        assert ">>> Client connected, starting scripts" in out

    async def test_disconnect_between_scripts_stops_sequence(self, capsys):
        async def disconnecting_run(script, sim):
            sim.protocols.clear()
            return True

        sim, handler, runner, stop, result, runs, _ = self._make(side_effect=disconnecting_run)
        sim.protocols.append("client")
        await self._run(
            ["s1", "s2"], sim, handler, runner, stop, result, wait_for_client=True, oneshot=True
        )
        assert runs == ["s1"]  # s2 never ran
        assert result[0] is True
        assert stop.is_set()
        assert ">>> Client disconnected, stopping scripts" in capsys.readouterr().out

    async def test_script_progress_lines_sanitize_the_script_name(self, capsys):
        """The name comes out of an untrusted YAML file and hits a terminal.

        PyYAML rejects raw C0 bytes in a scalar but its ``\\e`` escape
        produces a real ESC, so "the file looks clean" is not a defence.
        """
        poison = "\x1b[2J\x1b[1;1H*** PWNED ***\x07"
        sim, handler, runner, stop, result, _runs, _ = self._make(run_results=[False])
        handler.load_script = lambda ref: SimpleNamespace(name=poison)

        await self._run(["evil"], sim, handler, runner, stop, result, oneshot=True)

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x07" not in out
        assert ">>> Running script: \\x1b[2J\\x1b[1;1H*** PWNED ***\\x07" in out
        assert ">>> Script FAILED: \\x1b[2J\\x1b[1;1H*** PWNED ***\\x07" in out

    async def test_script_load_error_is_sanitized(self, capsys):
        """Loader errors quote file-derived text straight back at the operator."""
        sim, handler, runner, stop, result, _runs, _ = self._make()

        def exploding_load(ref):
            raise ValueError("bad \x1b[2J yaml")

        handler.load_script = exploding_load

        await self._run(["evil\x1b[2J"], sim, handler, runner, stop, result, oneshot=True)

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "Error running script 'evil\\x1b[2J': bad \\x1b[2J yaml" in out

    async def test_inner_loop_disconnect_line_is_flushed(self, monkeypatch):
        """The one progress line the flush fix missed.

        It is also the only line that explains why the remaining scripts
        never ran, and off a terminal it died in the buffer - not even
        SIGTERM got it out.
        """
        flushed: list[str] = []

        class RecordingStdout(io.StringIO):
            def flush(self):
                flushed.append(self.getvalue())
                super().flush()

        async def disconnecting_run(script, sim):
            sim.protocols.clear()
            return True

        sim, handler, runner, stop, result, runs, _ = self._make(side_effect=disconnecting_run)
        sim.protocols.append("client")
        stream = RecordingStdout()
        monkeypatch.setattr(sys, "stdout", stream)

        await self._run(
            ["s1", "s2"], sim, handler, runner, stop, result, wait_for_client=True, oneshot=True
        )

        # A flush must land with this line as the newest output. Merely
        # appearing in a later flush's snapshot is what a bare print()
        # already achieved - and what died in the buffer for real.
        assert any(text.endswith(">>> Client disconnected, stopping scripts\n") for text in flushed)

    async def test_loop_scripts_until_disconnect(self, capsys):
        calls = [0]

        async def second_run_disconnects(script, sim):
            calls[0] += 1
            if calls[0] == 2:
                sim.protocols.clear()
            return True

        sim, handler, runner, stop, result, runs, _ = self._make(side_effect=second_run_disconnects)
        sim.protocols.append("client")
        await self._run(
            ["s1"],
            sim,
            handler,
            runner,
            stop,
            result,
            loop_scripts=True,
            script_delay=0.01,
            wait_for_client=True,
        )
        assert runs == ["s1", "s1"]
        out = capsys.readouterr().out
        assert ">>> Script run #1" in out
        assert ">>> Script run #2" in out
        assert ">>> Script run #3" not in out
        assert ">>> Waiting 0.01s before next loop..." in out
        assert ">>> Client disconnected, stopping scripts" in out

    async def test_zero_script_delay_loops_without_waiting(self, capsys):
        """`--loop --script-delay 0` must continue, not sleep.

        The `if script_delay > 0:` guard in the outer loop was never
        exercised in its False direction - mutating it to `>= 0` survived
        the whole suite, hidden from the coverage gate by the bare
        three-dot exclusion pattern.
        """

        async def stop_after_two(script, sim):
            if len(runs) >= 2:
                sim.protocols.clear()
            return True

        sim, handler, runner, stop, result, runs, _ = self._make(side_effect=stop_after_two)
        sim.protocols.append("client")
        await self._run(
            ["s1"],
            sim,
            handler,
            runner,
            stop,
            result,
            loop_scripts=True,
            script_delay=0,
            wait_for_client=True,
        )

        assert runs == ["s1", "s1"]
        out = capsys.readouterr().out
        assert ">>> Script run #2" in out
        assert "before next loop" not in out

    async def test_zero_script_delay_between_scripts_does_not_wait(self, capsys):
        """The *other* `script_delay > 0` guard, at `cli.py:488`.

        The inner guard has a positive-delay test but had no zero-delay
        counterpart, so a mutant printing ">>> Waiting 0s before next
        script..." between every script - or before the *first* one - went
        unnoticed. Coverage cannot see this: `if A and B:` is one branch
        point with two destinations, so 100% branch coverage never requires
        `i > 0 and delay == 0`.
        """
        sim, handler, runner, stop, result, runs, _ = self._make()
        await self._run(["s1", "s2"], sim, handler, runner, stop, result, script_delay=0)

        assert runs == ["s1", "s2"]
        assert "before next script" not in capsys.readouterr().out

    async def test_the_first_script_never_waits_even_with_a_delay(self, capsys):
        """The `i > 0` operand of the same compound condition.

        `i > 0` -> `i >= 0` makes the run pause before the *first* script.
        """
        sim, handler, runner, stop, result, runs, _ = self._make()
        await self._run(["s1"], sim, handler, runner, stop, result, script_delay=0.01)

        assert runs == ["s1"]
        assert "before next script" not in capsys.readouterr().out

    async def _cancel_mid_script(self, stop, result, *, scripts=("s1",), **kwargs):
        """Start a run that blocks inside its first script, then cancel it."""
        blocker = asyncio.Event()

        async def blocking_run(script, sim):
            await blocker.wait()
            return True

        sim, handler, runner, _stop, _result, runs, started = self._make(side_effect=blocking_run)
        task = asyncio.create_task(
            self._run(list(scripts), sim, handler, runner, stop, result, **kwargs)
        )
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        return task, runs

    async def test_a_cancelled_run_records_no_verdict_and_re_raises(self, capsys):
        """An interrupted run must not claim a result it never established.

        `all_success` at the moment of a cancellation means "nothing has
        failed *yet*", not "every assertion ran and passed". Recording it
        made `ppd-simulator --script … --oneshot` print
        `>>> All scripts PASSED` and exit **0** for a run stopped inside a
        30 s `wait`, two steps before its `assert`. Re-raising is what
        lets `asyncio.Runner` turn the cancellation back into
        `KeyboardInterrupt` for main().
        """
        stop = asyncio.Event()
        result: list = [None]
        task, runs = await self._cancel_mid_script(stop, result, oneshot=True)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert runs == ["s1"]
        assert result[0] is None
        assert not stop.is_set()
        out = capsys.readouterr().out
        assert ">>> All scripts" not in out
        assert ">>> Interrupted after 0 of 1 script(s)" in out

    async def test_a_cancelled_run_counts_the_scripts_that_did_finish(self, capsys):
        """The banner reports progress, so a partial run is not mistaken for a
        whole one: the first script completed, the second was cut off."""
        stop = asyncio.Event()
        result: list = [None]
        sim, handler, runner, _stop, _result, runs, started = self._make()

        blocker = asyncio.Event()
        real_run = runner.run

        async def run(script):
            await real_run(script)
            if script.name == "s2":
                await blocker.wait()
            return True

        runner.run = run
        task = asyncio.create_task(
            self._run(["s1", "s2"], sim, handler, runner, stop, result, oneshot=True)
        )
        while runs != ["s1", "s2"]:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)

        assert result[0] is None
        assert ">>> Interrupted after 1 of 2 script(s)" in capsys.readouterr().out

    async def test_a_cancelled_non_oneshot_run_prints_no_banner_at_all(self, capsys):
        """Without `--oneshot` there is no verdict line either way, so an
        interrupted run stays silent - but still records no result."""
        stop = asyncio.Event()
        result: list = [None]
        task, _runs = await self._cancel_mid_script(stop, result, oneshot=False)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert result[0] is None
        out = capsys.readouterr().out
        assert ">>> All scripts" not in out
        assert ">>> Interrupted" not in out


# ============================================================================
# _BasicStdinInput (plain-input fallback)
# ============================================================================


class RecordingPrompt:
    def __init__(self):
        self.calls = []

    def enable(self):
        self.calls.append("enable")

    def show(self):
        self.calls.append("show")

    def clear_line(self):
        self.calls.append("clear")

    def output(self, text):
        self.calls.append(("output", text))


class TestBasicStdinInput:
    def _make(self, readline=None, fileno=None, message="done", success=True, sets_stop=False):
        loop = asyncio.get_running_loop()
        prompt = RecordingPrompt()
        stop = asyncio.Event()
        executed = []
        done = asyncio.Event()

        async def execute(line):
            executed.append(line)
            if sets_stop:
                stop.set()
            done.set()
            return SimpleNamespace(success=success, message=message)

        handler = SimpleNamespace(execute=execute)
        stdin = SimpleNamespace(
            readline=readline or (lambda: ""),
            fileno=fileno or (lambda: -1),
        )
        basic = cli._BasicStdinInput(loop, prompt, handler, stop, stdin=stdin)
        return basic, prompt, stop, executed, done

    async def test_start_enables_prompt_and_registers_reader(self):
        r_fd, w_fd = os.pipe()
        try:
            loop = asyncio.get_running_loop()
            prompt = RecordingPrompt()
            basic = cli._BasicStdinInput(
                loop, prompt, SimpleNamespace(), asyncio.Event(), stdin=PipeStdin(r_fd)
            )
            basic.start()
            assert prompt.calls == ["enable"]
            # remove_reader returns True only if a reader was registered
            assert loop.remove_reader(r_fd) is True
        finally:
            os.close(r_fd)
            os.close(w_fd)

    async def test_handle_input_ignored_when_stopped(self):
        def readline():
            pytest.fail("readline must not be called after stop_event is set")

        basic, prompt, stop, _, _ = self._make(readline=readline)
        stop.set()
        basic.handle_input()
        assert prompt.calls == []

    async def test_handle_input_ignored_after_reader_removed(self):
        def readline():
            pytest.fail("readline must not be called after stop()")

        basic, prompt, _, _, _ = self._make(readline=readline, fileno=lambda: -1)
        basic.stop()
        basic.handle_input()
        assert prompt.calls == []

    async def test_stop_swallows_fileno_errors(self):
        def broken_fileno():
            raise ValueError("closed")

        basic, _, _, _, _ = self._make(fileno=broken_fileno)
        basic.stop()  # must not raise
        assert basic._reader_removed is True

    async def test_handle_input_executes_command_and_prints_result(self):
        basic, prompt, _, executed, done = self._make(readline=lambda: "status\n", message="OK-MSG")
        basic.handle_input()
        await asyncio.wait_for(done.wait(), 5)
        assert executed == ["status"]
        assert ("output", ">>> OK-MSG") in prompt.calls

    async def test_handle_input_empty_line_reshows_prompt(self):
        basic, prompt, _, executed, _ = self._make(readline=lambda: "\n")
        basic.handle_input()
        assert prompt.calls == ["show"]
        assert executed == []

    async def test_eof_ends_the_session_instead_of_re_showing_the_prompt(self):
        """`readline() == ""` is EOF, not a bare Enter.

        An fd at EOF is *permanently* readable, so treating it as Enter
        re-arms the reader forever: 98% of a core, tens of MB of prompt
        text, and a process that never exits - on pipe-backed stdin only,
        which is why a terminal never showed it.
        """
        basic, prompt, stop, executed, _ = self._make(readline=lambda: "")

        basic.handle_input()

        assert stop.is_set() is True
        assert basic._reader_removed is True
        assert executed == []
        assert "show" not in prompt.calls

    async def test_eof_is_only_acted_on_once(self):
        """The second callback (if any) must be inert, not a second shutdown."""
        reads = ["", ""]
        basic, prompt, stop, _, _ = self._make(readline=lambda: reads.pop(0))

        basic.handle_input()
        basic.handle_input()

        assert prompt.calls.count("clear") == 1

    async def test_a_non_pollable_stdin_falls_back_to_blocking_reads(self, tmp_path):
        """/dev/null, a regular file and some heredocs are not pollable.

        `epoll` refuses them with PermissionError out of `add_reader`, which
        used to be a 37-line traceback and rc 1 before the prompt appeared.
        """
        script = tmp_path / "commands"
        script.write_text("status\n")
        loop = asyncio.get_running_loop()
        prompt = RecordingPrompt()
        stop = asyncio.Event()
        executed = []

        async def execute(line):
            executed.append(line)
            return SimpleNamespace(success=True, message="")

        with script.open() as handle:
            basic = cli._BasicStdinInput(
                loop, prompt, SimpleNamespace(execute=execute), stop, stdin=handle
            )
            with pytest.raises(PermissionError):
                loop.add_reader(handle.fileno(), lambda: None)

            basic.start()
            await asyncio.wait_for(stop.wait(), 5)

        # The command ran, and EOF at the end of the file ended the session.
        assert executed == ["status"]
        assert basic._blocking_task is None or basic._blocking_task.done()

    async def test_the_blocking_reader_stops_when_the_session_ends(self):
        """A shutdown from elsewhere ends the read loop, not just EOF."""
        loop = asyncio.get_running_loop()
        prompt = RecordingPrompt()
        stop = asyncio.Event()

        def readline():
            stop.set()  # e.g. a `shutdown` command completing meanwhile
            return "\n"

        basic = cli._BasicStdinInput(
            loop,
            prompt,
            SimpleNamespace(),
            stop,
            stdin=SimpleNamespace(readline=readline, fileno=lambda: -1),
        )

        await asyncio.wait_for(basic._read_blocking(), 5)

        assert prompt.calls == ["show"]

    async def test_dev_null_stdin_ends_the_session_immediately(self):
        loop = asyncio.get_running_loop()
        prompt = RecordingPrompt()
        stop = asyncio.Event()

        with open(os.devnull) as handle:
            basic = cli._BasicStdinInput(loop, prompt, SimpleNamespace(), stop, stdin=handle)
            basic.start()
            await asyncio.wait_for(stop.wait(), 5)

        assert basic._reader_removed is True

    async def test_handle_input_readline_error_reported(self):
        def broken_readline():
            raise RuntimeError("boom")

        basic, prompt, _, _, _ = self._make(readline=broken_readline)
        basic.handle_input()
        assert prompt.calls == [("output", "Error: boom")]

    async def test_process_command_shutdown_with_message(self, capsys):
        basic, prompt, stop, _, _ = self._make(message="Bye", sets_stop=True)
        await basic.process_command("shutdown")
        assert "clear" in prompt.calls
        assert basic._reader_removed is True
        assert capsys.readouterr().out == ">>> Bye\n"

    async def test_process_command_shutdown_without_message(self, capsys):
        basic, prompt, stop, _, _ = self._make(message="", sets_stop=True)
        await basic.process_command("shutdown")
        assert "clear" in prompt.calls
        assert basic._reader_removed is True
        assert capsys.readouterr().out == ""

    async def test_process_command_message_shown_via_prompt(self):
        basic, prompt, _, _, _ = self._make(message="hello")
        await basic.process_command("status")
        assert prompt.calls == [("output", ">>> hello")]

    async def test_process_command_empty_message_reshows_prompt(self):
        basic, prompt, _, _, _ = self._make(message="")
        await basic.process_command("clear")
        assert prompt.calls == ["show"]

    async def test_process_command_sanitizes_network_poisoned_output(self):
        """render_result is the ONLY sanitizer on this path.

        A hostile SET_TIMEZONE stores a string the ``timezone`` command
        echoes straight back, so the CLI's own print site must escape it.
        """
        basic, prompt, _, _, _ = self._make(message="Timezone: \x1b[2J\x1b[1;1H*** PWNED ***\x07")
        await basic.process_command("timezone")
        assert prompt.calls == [("output", ">>> Timezone: \\x1b[2J\\x1b[1;1H*** PWNED ***\\x07")]

    async def test_process_command_shutdown_sanitizes_output(self, capsys):
        """The shutdown branch prints through the same sanitizer."""
        basic, _, _, _, _ = self._make(message="Bye \x1b[2J", sets_stop=True)
        await basic.process_command("shutdown")
        out = capsys.readouterr().out
        assert out == ">>> Bye \\x1b[2J\n"
        assert "\x1b" not in out


# ============================================================================
# run_simulator end-to-end: script mode
# ============================================================================


class TestRunSimulatorScripts:
    async def test_oneshot_passing_script_returns_true(self, tmp_path, capsys):
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)
        result = await asyncio.wait_for(
            cli.run_simulator(host="127.0.0.1", port=0, scripts=[str(script)], oneshot=True),
            15,
        )
        assert result is True
        out = capsys.readouterr().out
        assert ">>> Script PASSED: Passing Script" in out
        assert ">>> All scripts PASSED" in out

    async def test_oneshot_failing_script_returns_false(self, tmp_path, capsys):
        script = tmp_path / "fail.yaml"
        script.write_text(FAILING_SCRIPT)
        result = await asyncio.wait_for(
            cli.run_simulator(host="127.0.0.1", port=0, scripts=[str(script)], oneshot=True),
            15,
        )
        assert result is False
        out = capsys.readouterr().out
        assert ">>> Script FAILED: Failing Script" in out
        assert ">>> All scripts FAILED" in out

    async def test_cancelling_mid_script_cancels_the_startup_task_first(self, tmp_path, capsys):
        """The startup-script task is cancelled *inside* run_simulator's
        cleanup, not left for `asyncio.run`'s shutdown.

        As a bare `create_task` it was reaped after `main()` had already
        read `script_result[0]`, so `run_simulator` returned `None` and the
        `>>> All scripts PASSED` banner was printed by the dying task on the
        way out - a verdict for a run that never reached its assertion, on
        the exit path that then reported 0.
        """
        script = tmp_path / "slow.yaml"
        script.write_text(
            "name: Slow Script\nsteps:\n  - action: log\n    message: started\n"
            "  - action: wait\n    seconds: 30\n"
            "  - action: assert\n    condition: door_status\n    equals: DOOR_HOLDING\n"
        )
        task = asyncio.create_task(
            cli.run_simulator(host="127.0.0.1", port=0, scripts=[str(script)], oneshot=True)
        )
        while ">>> Running script: Slow Script" not in capsys.readouterr().out:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)

        out = capsys.readouterr().out
        assert ">>> All scripts" not in out
        assert ">>> Interrupted after 0 of 1 script(s)" in out

    async def test_oneshot_unknown_script_returns_false(self, capsys):
        result = await asyncio.wait_for(
            cli.run_simulator(
                host="127.0.0.1", port=0, scripts=["definitely_missing_script"], oneshot=True
            ),
            15,
        )
        assert result is False
        assert "Error running script 'definitely_missing_script':" in capsys.readouterr().out

    async def test_wait_for_client_starts_scripts_after_connect(self, tmp_path, capsys):
        script = tmp_path / "pass.yaml"
        script.write_text(PASSING_SCRIPT)
        ready = asyncio.Event()
        ports: dict[str, int] = {}

        def on_ready(door_port, control_port):
            ports["door"] = door_port
            ready.set()

        task = asyncio.create_task(
            cli.run_simulator(
                host="127.0.0.1",
                port=0,
                scripts=[str(script)],
                oneshot=True,
                wait_for_client=True,
                on_ready=on_ready,
            )
        )
        await asyncio.wait_for(ready.wait(), 10)
        reader, writer = await asyncio.open_connection("127.0.0.1", ports["door"])
        try:
            result = await asyncio.wait_for(task, 15)
        finally:
            writer.close()
        assert result is True
        out = capsys.readouterr().out
        assert ">>> Waiting for client connection..." in out
        assert ">>> Client connected, starting scripts" in out
        assert ">>> Script PASSED: Passing Script" in out


# ============================================================================
# run_simulator end-to-end: daemon mode extras
# ============================================================================


class TestRunSimulatorDaemonExtras:
    async def _start_daemon(self, **kwargs):
        ready = asyncio.Event()
        ports: dict[str, int | None] = {}

        def on_ready(door_port, control_port):
            ports["door"] = door_port
            ports["control"] = control_port
            ready.set()

        task = asyncio.create_task(
            cli.run_simulator(
                host="127.0.0.1",
                port=0,
                daemon=True,
                control_port=0,
                control_host="127.0.0.1",
                on_ready=on_ready,
                **kwargs,
            )
        )
        await asyncio.wait_for(ready.wait(), 10)
        return task, ports

    async def test_door_client_events_broadcast_status(self):
        """run_simulator's own connect/disconnect callbacks notify ctl clients."""
        task, ports = await self._start_daemon()
        reader, writer = await asyncio.open_connection("127.0.0.1", ports["control"])
        try:
            greeting = await read_line_matching(reader, "STATUS:")
            assert greeting == "STATUS: clients=0"

            door_reader, door_writer = await asyncio.open_connection("127.0.0.1", ports["door"])
            line = await read_line_matching(reader, "STATUS:")
            assert line == "STATUS: clients=1"

            door_writer.close()
            await door_writer.wait_closed()
            line = await read_line_matching(reader, "STATUS:")
            assert line == "STATUS: clients=0"

            writer.write(b"shutdown\n")
            await writer.drain()
            assert await asyncio.wait_for(task, 10) is None
        finally:
            writer.close()

    async def test_queued_script_runs_and_logs_over_control(self, tmp_path, caplog):
        """'run <name>' queues the script; the queue processor runs it and the
        result is broadcast as LOG lines."""
        # LOG broadcasting requires INFO records to pass the root logger level
        # (main() configures this via basicConfig; tests must do it explicitly)
        caplog.set_level(logging.INFO)
        (tmp_path / "myscript.yaml").write_text(PASSING_SCRIPT)
        task, ports = await self._start_daemon(scripts_dir=str(tmp_path))
        reader, writer = await asyncio.open_connection("127.0.0.1", ports["control"])
        try:
            await read_line_matching(reader, "STATUS:")
            writer.write(b"run myscript\n")
            await writer.drain()
            response = await read_line_matching(reader, "OK:")
            assert response == "OK: Queued script: Passing Script"

            line = await read_line_containing(reader, "Script PASSED: Passing Script")
            assert line.startswith("LOG: ")

            writer.write(b"shutdown\n")
            await writer.drain()
            assert await asyncio.wait_for(task, 10) is None
        finally:
            writer.close()

    async def test_empty_scripts_dir_warns_at_startup(self, tmp_path, caplog):
        """An existing but empty --scripts-dir must not be silent either."""
        caplog.set_level(logging.WARNING)
        task, ports = await self._start_daemon(scripts_dir=str(tmp_path))
        reader, writer = await asyncio.open_connection("127.0.0.1", ports["control"])
        try:
            await read_line_matching(reader, "STATUS:")
            assert f"No *.yaml/*.yml scripts found in {tmp_path}" in caplog.text
            writer.write(b"shutdown\n")
            await writer.drain()
            assert await asyncio.wait_for(task, 10) is None
        finally:
            writer.close()

    async def test_stop_during_startup_cancels_queue_task_cleanly(self, monkeypatch):
        """A stop that fires before the event loop ever runs the script-queue
        task must not hang or leak (queue task is cancelled before starting)."""
        real_handler = cli.CommandHandler
        captured: dict = {}

        def capturing_handler(**kwargs):
            captured["stop"] = kwargs["stop_callback"]
            return real_handler(**kwargs)

        monkeypatch.setattr(cli, "CommandHandler", capturing_handler)

        def on_ready(door_port, control_port):
            captured["stop"]()

        result = await asyncio.wait_for(
            cli.run_simulator(host="127.0.0.1", port=0, daemon=True, on_ready=on_ready),
            10,
        )
        assert result is None

    async def test_external_cancel_shuts_down_cleanly(self):
        """Cancelling the run_simulator task performs full cleanup and then
        lets the cancellation through.

        Swallowing it is how Ctrl-C became exit 0: `asyncio.Runner`
        delivers SIGINT by cancelling the main task and only re-raises
        `KeyboardInterrupt` if that cancellation actually propagates.
        """
        task, ports = await self._start_daemon()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        # ...and the cleanup really ran: the door port is free again.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", ports["door"]))


# ============================================================================
# run_simulator end-to-end: interactive modes
# ============================================================================


def _track_execute(monkeypatch):
    """Wrap CommandHandler.execute to report executed commands via a queue."""
    executed: asyncio.Queue = asyncio.Queue()
    real_execute = CommandHandler.execute

    async def tracking_execute(handler_self, command_str):
        result = await real_execute(handler_self, command_str)
        executed.put_nowait((command_str, result.success))
        return result

    monkeypatch.setattr(CommandHandler, "execute", tracking_execute)
    return executed


class TestRunSimulatorInteractive:
    async def test_stdin_unavailable_falls_back_to_daemon_like_mode(self, caplog):
        """Under pytest, stdin has no usable fileno: interactive mode must warn
        and keep running (here bounded by run_for)."""
        caplog.set_level(logging.INFO)
        result = await asyncio.wait_for(
            cli.run_simulator(host="127.0.0.1", port=0, run_for=0.05),
            10,
        )
        assert result is None
        assert "stdin not available, running without interactive input" in caplog.text
        assert "Run time (0.05s) elapsed, shutting down" in caplog.text

    async def test_stdin_none_falls_back_to_daemon_like_mode(self, monkeypatch, caplog):
        """sys.stdin can be None entirely (e.g. pythonw): same fallback."""
        caplog.set_level(logging.WARNING)
        monkeypatch.setattr(sys, "stdin", None)
        result = await asyncio.wait_for(
            cli.run_simulator(host="127.0.0.1", port=0, run_for=0.05),
            10,
        )
        assert result is None
        assert "stdin not available, running without interactive input" in caplog.text

    async def test_basic_input_fallback_end_to_end(self, monkeypatch, capsys, root_logger_guard):
        """Non-TTY stdin drives the plain-input fallback: commands execute,
        output goes through the prompt, shutdown ends the run."""
        executed = _track_execute(monkeypatch)
        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdin", PipeStdin(r_fd))

            ready = asyncio.Event()
            ports: dict[str, int] = {}

            def on_ready(door_port, control_port):
                ports["door"] = door_port
                ready.set()

            task = asyncio.create_task(
                cli.run_simulator(host="127.0.0.1", port=0, on_ready=on_ready)
            )
            await asyncio.wait_for(ready.wait(), 10)

            os.write(w_fd, b"\n")  # empty line: prompt is re-shown
            os.write(w_fd, b"status\n")
            assert await asyncio.wait_for(executed.get(), 10) == ("status", True)

            os.write(w_fd, b"shutdown\n")
            assert await asyncio.wait_for(executed.get(), 10) == ("shutdown", True)
            assert await asyncio.wait_for(task, 10) is None
        finally:
            os.close(w_fd)
            os.close(r_fd)

        out = capsys.readouterr().out
        prompt_text = f"127.0.0.1:{ports['door']}> "
        # Exactly three: initial, after the empty line, after the output.
        # `>= 3` let a duplicated-prompt regression through.
        assert out.count(prompt_text) == 3
        assert ">>> " in out
        assert "Door:" in out  # status output
        assert "Shutting down" in out

    @requires_prompt_toolkit
    async def test_prompt_toolkit_end_to_end(self, monkeypatch, tmp_path, root_logger_guard):
        """Full prompt_toolkit session: command, history recall, prompt
        invalidation on door connect/disconnect, exit-as-shutdown, and
        history canonicalization."""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        monkeypatch.setattr(cli, "use_prompt_toolkit", lambda: True)
        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: True)

        executed = _track_execute(monkeypatch)

        invalidated: asyncio.Queue = asyncio.Queue()
        real_invalidate = InteractiveSession.invalidate

        def tracking_invalidate(session_self):
            real_invalidate(session_self)
            invalidated.put_nowait(True)

        monkeypatch.setattr(InteractiveSession, "invalidate", tracking_invalidate)

        r_fd, w_fd = os.pipe()
        history_file = tmp_path / "hist"
        try:
            monkeypatch.setattr(sys, "stdin", PipeStdin(r_fd))

            ready = asyncio.Event()
            ports: dict[str, int] = {}

            def on_ready(door_port, control_port):
                ports["door"] = door_port
                ready.set()

            with create_pipe_input() as pipe_input:
                with create_app_session(input=pipe_input, output=DummyOutput()):
                    task = asyncio.create_task(
                        cli.run_simulator(
                            host="127.0.0.1",
                            port=0,
                            history_file=str(history_file),
                            on_ready=on_ready,
                        )
                    )
                    await asyncio.wait_for(ready.wait(), 10)

                    pipe_input.send_text("status\r")
                    assert await asyncio.wait_for(executed.get(), 10) == ("status", True)

                    # Door client connect/disconnect invalidates the prompt
                    dr, dw = await asyncio.open_connection("127.0.0.1", ports["door"])
                    await asyncio.wait_for(invalidated.get(), 10)
                    dw.close()
                    await dw.wait_closed()
                    await asyncio.wait_for(invalidated.get(), 10)

                    # clear succeeds with an empty message (nothing printed)
                    pipe_input.send_text("clear\r")
                    assert await asyncio.wait_for(executed.get(), 10) == ("clear", True)

                    # !1 recalls the first history entry (status)
                    pipe_input.send_text("!1\r")
                    assert await asyncio.wait_for(executed.get(), 10) == ("status", True)

                    # In CLI mode, exit is an alias for shutdown
                    pipe_input.send_text("exit\r")
                    assert await asyncio.wait_for(executed.get(), 10) == ("exit", True)
                    assert await asyncio.wait_for(task, 10) is None
        finally:
            os.close(w_fd)
            os.close(r_fd)

        # History: !1 was stored resolved, exit was canonicalized to shutdown
        entries = [
            line[1:] for line in history_file.read_text().splitlines() if line.startswith("+")
        ]
        assert entries == ["status", "clear", "status", "shutdown"]

    @requires_prompt_toolkit
    async def test_prompt_toolkit_eof_ends_session(self, monkeypatch, root_logger_guard):
        """Closing the input (EOF, e.g. Ctrl-D) ends the interactive run."""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        monkeypatch.setattr(cli, "use_prompt_toolkit", lambda: True)
        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: True)
        executed = _track_execute(monkeypatch)

        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdin", PipeStdin(r_fd))
            ready = asyncio.Event()

            def on_ready(door_port, control_port):
                ready.set()

            with create_pipe_input() as pipe_input:
                with create_app_session(input=pipe_input, output=DummyOutput()):
                    task = asyncio.create_task(
                        cli.run_simulator(
                            host="127.0.0.1", port=0, history_file="none", on_ready=on_ready
                        )
                    )
                    await asyncio.wait_for(ready.wait(), 10)
                    pipe_input.send_text("status\r")
                    assert await asyncio.wait_for(executed.get(), 10) == ("status", True)
                    pipe_input.close()  # EOF at the prompt
                    assert await asyncio.wait_for(task, 10) is None
        finally:
            os.close(w_fd)
            os.close(r_fd)

    @requires_prompt_toolkit
    async def test_prompt_session_unavailable_degrades_to_immediate_eof(
        self, monkeypatch, root_logger_guard
    ):
        """If the PromptSession cannot actually be built (the session-level
        gate disagrees with the CLI-level gate, e.g. the terminal went away),
        the input loop sees immediate EOF and the run ends cleanly."""
        monkeypatch.setattr(cli, "use_prompt_toolkit", lambda: True)
        # prompt_common's own gate still sees the non-TTY test stdin, so
        # InteractiveSession is created without a PromptSession or history

        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdin", PipeStdin(r_fd))
            result = await asyncio.wait_for(
                cli.run_simulator(host="127.0.0.1", port=0, history_file="none"),
                10,
            )
        finally:
            os.close(w_fd)
            os.close(r_fd)
        assert result is None

    @requires_prompt_toolkit
    async def test_prompt_toolkit_run_for_cancels_input_loop(self, monkeypatch, root_logger_guard):
        """run_for expiry cancels the input task while it awaits the prompt."""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        monkeypatch.setattr(cli, "use_prompt_toolkit", lambda: True)
        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: True)

        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdin", PipeStdin(r_fd))
            with create_pipe_input() as pipe_input:
                with create_app_session(input=pipe_input, output=DummyOutput()):
                    result = await asyncio.wait_for(
                        cli.run_simulator(
                            host="127.0.0.1",
                            port=0,
                            history_file="none",
                            run_for=0.05,
                        ),
                        10,
                    )
        finally:
            os.close(w_fd)
            os.close(r_fd)
        assert result is None

    @requires_prompt_toolkit
    async def test_prompt_toolkit_stop_before_input_loop_starts(
        self, monkeypatch, root_logger_guard
    ):
        """A stop that fires during startup cancels the never-started input
        task cleanly (no hang, no unhandled CancelledError)."""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        monkeypatch.setattr(cli, "use_prompt_toolkit", lambda: True)
        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: True)

        real_handler = cli.CommandHandler
        captured: dict = {}

        def capturing_handler(**kwargs):
            captured["stop"] = kwargs["stop_callback"]
            return real_handler(**kwargs)

        monkeypatch.setattr(cli, "CommandHandler", capturing_handler)

        def on_ready(door_port, control_port):
            captured["stop"]()

        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdin", PipeStdin(r_fd))
            with create_pipe_input() as pipe_input:
                with create_app_session(input=pipe_input, output=DummyOutput()):
                    result = await asyncio.wait_for(
                        cli.run_simulator(
                            host="127.0.0.1",
                            port=0,
                            history_file="none",
                            on_ready=on_ready,
                        ),
                        10,
                    )
        finally:
            os.close(w_fd)
            os.close(r_fd)
        assert result is None


# ============================================================================
# Ctrl-C against the real binary
# ============================================================================


class TestTheRealBinaryUnderSIGINT:
    """The exit code and the verdict banner, measured on a real process.

    Monkeypatching `cli.run_simulator` to raise `KeyboardInterrupt` directly
    only asserts `main()`'s handler in isolation, and cannot fail for the
    reason this exists: the shipped binary swallowed the cancellation
    `asyncio.Runner` uses to deliver SIGINT, so that handler was never
    entered and an interrupted `--oneshot` run printed
    `>>> All scripts PASSED` and exited **0**. Only a real process carries
    that machinery, so only a real process can pin it.
    """

    LONG_SCRIPT = """\
name: Long Script
steps:
  - action: log
    message: starting
  - action: wait
    seconds: 30
  - action: assert
    condition: door_status
    equals: DOOR_CLOSED
"""

    def _run(self, script_path, *, interrupt_at=None):
        """Run `ppd-simulator --script <path> --oneshot`, optionally SIGINTing.

        Returns (returncode, combined output). `interrupt_at` is a marker
        line fragment; SIGINT is sent as soon as it appears, so the signal
        lands deterministically inside the 30 s `wait` step rather than after
        a wall-clock guess.
        """
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "powerpetdoor.simulator",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--script",
                str(script_path),
                "--oneshot",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        chunks: list[str] = []
        try:
            if interrupt_at is not None:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    chunks.append(line)
                    if interrupt_at in line:
                        break
                else:  # no break: the marker never arrived
                    raise AssertionError(f"marker {interrupt_at!r} never appeared")
                proc.send_signal(signal.SIGINT)
            chunks.append(proc.stdout.read())
            return proc.wait(timeout=30), "".join(chunks)
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_sigint_mid_run_exits_130_and_claims_no_verdict(self, tmp_path):
        script = tmp_path / "long.yaml"
        script.write_text(self.LONG_SCRIPT)

        rc, out = self._run(script, interrupt_at="Step 2")

        assert rc == 130, out
        assert ">>> All scripts PASSED" not in out
        assert ">>> All scripts FAILED" not in out
        assert ">>> Interrupted after 0 of 1 script(s)" in out
        # Step 3 is the assertion the run existed for; it never ran.
        assert "Step 3" not in out

    def test_an_uninterrupted_run_still_passes_and_exits_zero(self, tmp_path):
        """Control: the same binary, the same flags, no signal."""
        script = tmp_path / "short.yaml"
        script.write_text(
            "name: Short Script\nsteps:\n"
            "  - action: assert\n    condition: door_status\n    equals: DOOR_CLOSED\n"
        )

        rc, out = self._run(script)

        assert rc == 0, out
        assert ">>> All scripts PASSED" in out


# ============================================================================
# Startup failures
# ============================================================================


class TestBindTimeArgumentsFailAsArguments:
    """Ports and hosts were the one class this parser did not check.

    `--scripts-dir` is caught by `parser.error(...)`; `--port 99999` reached
    `socket.bind()` and exited with `OverflowError: bind(): port must be
    0-65535` under 30 lines of asyncio traceback carrying absolute paths
    from the machine that built the venv.
    """

    @pytest.fixture(autouse=True)
    def _never_reach_run_simulator(self, monkeypatch):
        async def _boom(**kwargs):
            raise AssertionError("main() reached run_simulator; it should have exited first")

        monkeypatch.setattr(cli, "run_simulator", _boom)

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["--port", "99999"], "error: --port 99999: port must be 0-65535"),
            (["--port", "-5"], "error: --port -5: port must be 0-65535"),
            (["--port", "65536"], "error: --port 65536: port must be 0-65535"),
            (["--daemon", "99999"], "error: --daemon 99999: port must be 0-65535"),
            (["--daemon", "-2"], "error: --daemon -2: port must be 0-65535"),
        ],
        ids=["port-high", "port-negative", "port-limit+1", "daemon-high", "daemon-negative"],
    )
    def test_an_out_of_range_port_is_an_argument_error(self, capsys, monkeypatch, argv, message):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", *argv])

        with pytest.raises(SystemExit) as exc_info:
            cli.main()

        assert exc_info.value.code == 2
        assert message in capsys.readouterr().err

    @pytest.mark.parametrize("port", [cli.MIN_PORT, cli.MAX_PORT], ids=["min-port", "max-port"])
    def test_the_control_at_both_ends_of_the_range(self, monkeypatch, port):
        """Rule 8: `limit` itself must be accepted, on both bounds."""
        recorded: dict = {}

        async def fake_run(**kwargs):
            recorded.update(kwargs)
            return None

        monkeypatch.setattr(cli, "run_simulator", fake_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--port", str(port), "--daemon"])

        cli.main()

        assert recorded["port"] == port

    def test_the_daemon_sentinel_is_not_range_checked(self, monkeypatch):
        """`--daemon` with no value is -1, which is not a port an operator
        typed - checking it would refuse the documented default."""
        recorded: dict = {}

        async def fake_run(**kwargs):
            recorded.update(kwargs)
            return None

        monkeypatch.setattr(cli, "run_simulator", fake_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--port", "3000", "--daemon"])

        cli.main()

        assert cli.DAEMON_DEFAULT_CONTROL_PORT == -1
        assert recorded["control_port"] == 3000 + cli.CONTROL_PORT_OFFSET

    @pytest.mark.parametrize("value", ["-5", "0", "-0.5"], ids=["negative", "zero", "fractional"])
    def test_a_non_positive_run_for_is_an_argument_error(self, capsys, monkeypatch, value):
        """`--run-for -5` was accepted and silently meant "shut down
        immediately", logging `Run time (-5.0s) elapsed, shutting down` as
        if five negative seconds had passed."""
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--daemon", "--run-for", value])

        with pytest.raises(SystemExit) as exc_info:
            cli.main()

        assert exc_info.value.code == 2
        assert (
            f"error: --run-for {float(value):g}: must be greater than 0" in capsys.readouterr().err
        )

    def test_the_control_a_positive_run_for_is_plumbed_through(self, monkeypatch):
        recorded: dict = {}

        async def fake_run(**kwargs):
            recorded.update(kwargs)
            return None

        monkeypatch.setattr(cli, "run_simulator", fake_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--daemon", "--run-for", "0.001"])

        cli.main()

        assert recorded["run_for"] == 0.001

    @pytest.mark.parametrize(
        "argv",
        [
            ["--host", "300.1.1.1"],
            ["--daemon", "--control-host", "300.1.1.1"],
        ],
        ids=["host", "control-host"],
    )
    def test_an_unresolvable_bind_address_is_an_argument_error(self, capsys, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", *argv])

        with pytest.raises(SystemExit) as exc_info:
            cli.main()

        assert exc_info.value.code == 2
        assert "300.1.1.1: " in capsys.readouterr().err


class TestStartupBindFailuresPrintOneSentence:
    """Which of the two ports failed, and which flag changes it."""

    @staticmethod
    def _occupied_port():
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock, sock.getsockname()[1]

    async def test_the_door_port_failure_names_the_door_port(self):
        sock, port = self._occupied_port()
        try:
            with pytest.raises(cli.SimulatorStartupError) as exc_info:
                await cli.run_simulator(host="127.0.0.1", port=port, daemon=True)
        finally:
            sock.close()

        err = exc_info.value
        assert err.role == "door"
        assert err.port == port
        assert str(err).startswith(f"Cannot start: door server cannot use 127.0.0.1:{port} (")
        assert str(err).endswith("change it with --port")

    async def test_the_control_port_failure_names_the_control_port(self):
        """The case with an empty stdout before the fix: the door bound fine
        and only the derived control port collided."""
        sock, port = self._occupied_port()
        try:
            with pytest.raises(cli.SimulatorStartupError) as exc_info:
                await cli.run_simulator(host="127.0.0.1", port=0, daemon=True, control_port=port)
        finally:
            sock.close()

        err = exc_info.value
        assert err.role == "control"
        assert err.port == port
        assert str(err).endswith("change it with --daemon PORT")

    async def test_a_failed_control_bind_does_not_leave_the_door_server_listening(self):
        """The door server is started first; it has to come back down."""
        sock, port = self._occupied_port()
        door_ports: list[int] = []
        real_start = DoorSimulator.start

        async def recording_start(self):
            await real_start(self)
            door_ports.append(self.server.sockets[0].getsockname()[1])

        try:
            DoorSimulator.start = recording_start
            with pytest.raises(cli.SimulatorStartupError):
                await cli.run_simulator(host="127.0.0.1", port=0, daemon=True, control_port=port)
        finally:
            DoorSimulator.start = real_start
            sock.close()

        assert len(door_ports) == 1
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", door_ports[0]))

    def test_main_prints_the_sentence_and_exits_1(self, capsys, monkeypatch):
        async def failing(**kwargs):
            raise cli.SimulatorStartupError(
                "door", "0.0.0.0", 3000, OSError(98, "address already in use")
            )

        monkeypatch.setattr(cli, "run_simulator", failing)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--daemon"])

        with pytest.raises(SystemExit) as exc_info:
            cli.main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err.splitlines() == [
            "Cannot start: door server cannot use 0.0.0.0:3000 "
            "(address already in use); change it with --port"
        ]
        assert "Traceback" not in captured.err

    def test_debug_still_gives_the_traceback(self, capsys, monkeypatch):
        async def failing(**kwargs):
            raise cli.SimulatorStartupError(
                "door", "0.0.0.0", 3000, OSError(98, "address already in use")
            )

        monkeypatch.setattr(cli, "run_simulator", failing)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--daemon", "--debug"])

        with pytest.raises(SystemExit) as exc_info:
            cli.main()

        assert exc_info.value.code == 1
        assert "Traceback" in capsys.readouterr().err


class TestStateDocumentArguments:
    """`--initial-state` and `--states-dir` fail the command line, not a
    daemon several seconds later - the same rule `--scripts-dir` follows."""

    def test_initial_state_is_applied_to_the_starting_state(self, tmp_path):
        from powerpetdoor.simulator.cli import _build_state

        state = _build_state(None, None, {"settings": {"hold_time": 44}})

        assert state is not None
        assert state.hold_time == 44

    def test_an_explicit_firmware_flag_beats_the_document(self):
        """A file silently overriding the flag just typed is the
        surprising precedence; the command line wins."""
        from powerpetdoor.simulator.cli import _build_state

        state = _build_state((9, 8, 7), None, {"hardware": {"fw_major": 1}})

        assert state is not None
        assert state.fw_major == 9

    def test_no_overrides_at_all_leaves_the_default_state(self):
        from powerpetdoor.simulator.cli import _build_state

        assert _build_state(None, None, None) is None

    def test_a_missing_initial_state_file_exits_two(self, monkeypatch, capsys):
        monkeypatch.setattr(
            sys, "argv", ["ppd-simulator", "--initial-state", "/nonexistent/s.json"]
        )

        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        assert exit_info.value.code == 2
        assert "--initial-state" in capsys.readouterr().err

    def test_a_malformed_initial_state_file_exits_two(self, tmp_path, monkeypatch, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{nope")
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--initial-state", str(bad)])

        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        assert exit_info.value.code == 2
        assert "not valid" in capsys.readouterr().err

    def test_an_unknown_key_in_the_initial_state_exits_two(self, tmp_path, monkeypatch, capsys):
        """A CONTENT error, not a syntax one.

        Loading only parses; the keys are checked when the document is
        applied, which happened later inside start() and escaped as a
        32-line traceback. A missing file and a syntax error both gave a
        clean usage line, so this was the one shape of bad
        `--initial-state` that did not.
        """
        bad = tmp_path / "bad.json"
        bad.write_text('{"nonexistent_key": 1}')
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--initial-state", str(bad)])

        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        err = capsys.readouterr().err
        assert exit_info.value.code == 2
        assert "Unknown key(s)" in err
        assert "Traceback" not in err

    def test_a_bad_value_in_the_initial_state_exits_two(self, tmp_path, monkeypatch, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text('{"settings": {"power": "maybe"}}')
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--initial-state", str(bad)])

        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        err = capsys.readouterr().err
        assert exit_info.value.code == 2
        assert "must be true or false" in err
        assert "Traceback" not in err

    def test_a_valid_initial_state_still_starts(self, tmp_path, monkeypatch):
        """The check must not reject documents that were always fine."""
        good = tmp_path / "good.json"
        good.write_text('{"settings": {"power": false}}')
        monkeypatch.setattr(
            sys,
            "argv",
            # Ephemeral port and a brief run: the point is that argument
            # parsing accepts the document, not that the server does work.
            ["ppd-simulator", "--initial-state", str(good), "--run-for", "0.1", "--port", "0"],
        )

        cli.main()

    def test_a_states_dir_that_is_not_a_directory_exits_two(self, tmp_path, monkeypatch, capsys):
        """A typo'd directory used to be found only by a ctl user much
        later, which is why --scripts-dir checks this too."""
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--states-dir", str(tmp_path / "nope")])

        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        assert exit_info.value.code == 2
        assert "--states-dir" in capsys.readouterr().err


class TestListStatesFlag:
    """`--list-states` is the pre-flight surface for `reset`.

    It needs no daemon, exactly as `--list-scripts` is for `run`, and
    shares its renderer with the `list states` command so the two cannot
    disagree about what will load.
    """

    def test_it_lists_the_directory_and_exits(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "quiet_night.json").write_text("{}")
        monkeypatch.setattr(
            sys, "argv", ["ppd-simulator", "--list-states", "--states-dir", str(tmp_path)]
        )

        cli.main()

        out = capsys.readouterr().out
        assert str(tmp_path) in out
        assert "quiet_night" in out

    def test_without_a_directory_it_names_the_missing_flag(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--list-states"])

        cli.main()

        assert "--states-dir" in capsys.readouterr().out
