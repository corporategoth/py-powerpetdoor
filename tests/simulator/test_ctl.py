# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the simulator control client (ctl.py)."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import os
import socket
import sys

import pytest

from powerpetdoor.simulator import cli, ctl
from powerpetdoor.simulator.commands.base import (
    ArgSpec,
    CommandInfo,
    SubcommandInfo,
    get_command_registry,
)
from powerpetdoor.simulator.commands.history import History
from powerpetdoor.simulator.commands.info import InfoCommandsMixin
from powerpetdoor.simulator.ctl import LocalCommandHandler

# ============================================================================
# Helpers
# ============================================================================


async def start_fake_daemon(handler):
    """Start a fake control daemon on an ephemeral loopback port."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def make_line_responder(payload: bytes):
    """Build a handler that waits for one command line then sends payload."""

    async def handler(reader, writer):
        line = await reader.readline()
        if line:  # Skip the empty check_connection probe
            writer.write(payload)
            await writer.drain()
        writer.close()

    return handler


async def send_command_async(port: int, command: str, timeout: float = 2.0):
    """Run the blocking one-shot send_command in an executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, ctl.send_command, "127.0.0.1", port, command, timeout)


# ============================================================================
# One-shot send_command
# ============================================================================


class TestSendCommand:
    """Tests for one-shot command sending."""

    async def test_parses_ok_response(self):
        server, port = await start_fake_daemon(make_line_responder(b"OK: hello\n"))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is True
        assert msg == "OK: hello"

    async def test_skips_status_and_log_lines(self):
        """STATUS:/LOG: lines are not command responses."""
        payload = b"STATUS: clients=0\nLOG: some noise\nOK: done\n"
        server, port = await start_fake_daemon(make_line_responder(payload))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is True
        assert msg == "OK: done"

    async def test_unterminated_line_is_not_a_success(self):
        """A response line without its trailing newline must not be treated
        as a complete successful response (truncation bug)."""
        server, port = await start_fake_daemon(make_line_responder(b"OK: partial"))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is False
        assert "OK: partial" in msg

    async def test_unescape_order_preserves_literal_backslash_n(self):
        """Wire bytes 'scripts\\\\new.yaml' are a literal backslash + 'n',
        not a newline (unescape-order bug)."""
        server, port = await start_fake_daemon(make_line_responder(b"OK: scripts\\\\new.yaml\n"))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is True
        assert msg == "OK: scripts\\new.yaml"
        assert "\n" not in msg[4:]

    async def test_escaped_newlines_are_unescaped(self):
        server, port = await start_fake_daemon(make_line_responder(b"OK: line1\\nline2\n"))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is True
        assert msg == "OK: line1\nline2"

    async def test_control_characters_are_sanitized(self):
        """ANSI escapes from the network must never reach the terminal raw."""
        server, port = await start_fake_daemon(make_line_responder(b"OK: \x1b[2Jboom\n"))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is True
        assert "\x1b" not in msg
        assert "\\x1b" in msg

    async def test_response_timeout_has_explicit_message(self):
        """A daemon that accepts but never responds must produce an
        explanatory message, not an empty line."""

        async def silent_handler(reader, writer):
            # Read until the client gives up and closes; never respond
            await reader.read()
            writer.close()

        server, port = await start_fake_daemon(silent_handler)
        try:
            success, msg = await send_command_async(port, "status", timeout=0.3)
        finally:
            server.close()
            await server.wait_closed()
        assert success is False
        assert "Response timeout after 0.3s" in msg

    async def test_wait_run_ignores_the_response_timeout(self):
        """`run <script> wait` has no deadline while the daemon is alive.

        The daemon here stays silent for far longer than --timeout, exactly
        like a long script with no logging; the answer must still arrive.
        """
        released = asyncio.Event()
        # Recorded, not asserted: an AssertionError inside a connected
        # callback goes to the loop exception handler, not to the test.
        received: list[bytes] = []

        async def slow_wait_run(reader, writer):
            line = await reader.readline()
            if line:
                received.append(line)
                released.set()
                # Silence an order of magnitude longer than the timeout.
                await asyncio.sleep(0.5)
                writer.write(b"OK: Script PASSED: Full Test Suite\n")
                await writer.drain()
            writer.close()

        server, port = await start_fake_daemon(slow_wait_run)
        try:
            success, msg = await send_command_async(port, "run full_test_suite wait", timeout=0.05)
        finally:
            server.close()
            await server.wait_closed()

        assert released.is_set()
        assert received == [b"run full_test_suite wait\n"]
        assert success is True
        assert msg == "OK: Script PASSED: Full Test Suite"

    async def test_plain_run_still_honors_the_response_timeout(self):
        """Only wait-runs drop the deadline; a queued run keeps it."""

        async def silent_handler(reader, writer):
            await reader.read()
            writer.close()

        server, port = await start_fake_daemon(silent_handler)
        try:
            success, msg = await send_command_async(port, "run full_test_suite", timeout=0.3)
        finally:
            server.close()
            await server.wait_closed()

        assert success is False
        assert "Response timeout after 0.3s" in msg
        assert "the command may still be running; raise --timeout" in msg

    async def test_streaming_output_extends_the_response_deadline(self):
        """Each received chunk restarts the silence timer for any command.

        The margin is deliberately 10x (0.1 s gaps against a 1.0 s budget):
        under `-n auto` on a loaded runner the loop can be delayed >100 ms
        between a sleep expiring and the write landing, and this is one of
        the few tests where a slow machine could produce a *false failure*
        rather than a false pass. Total runtime is unchanged.
        """

        async def chatty_handler(reader, writer):
            line = await reader.readline()
            if line:
                # Four gaps, each far under the timeout: total elapsed is
                # well past it, but no single gap is.
                for _ in range(4):
                    await asyncio.sleep(0.1)
                    writer.write(b"LOG: still working\n")
                    await writer.drain()
                writer.write(b"OK: done\n")
                await writer.drain()
            writer.close()

        server, port = await start_fake_daemon(chatty_handler)
        try:
            success, msg = await send_command_async(port, "status", timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()

        assert success is True
        assert msg == "OK: done"

    async def test_wait_run_streams_log_lines_to_stderr(self, capfd):
        """A one-shot wait-run must show why a script failed.

        The assertion text lives only in the LOG: stream; ctl used to parse
        and discard it, so the documented CI recipe printed one bare
        "ERROR: Script FAILED" and nothing else.
        """

        async def failing_wait_run(reader, writer):
            line = await reader.readline()
            if line:
                writer.write(b"LOG: Running script: Failing Script\n")
                writer.write(b"LOG: Assertion failed at step 3: door_status\n")
                writer.write(b"ERROR: Script FAILED: Failing Script\n")
                await writer.drain()
            writer.close()

        server, port = await start_fake_daemon(failing_wait_run)
        try:
            success, msg = await send_command_async(port, "run failing wait", timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()

        assert success is False
        assert msg == "ERROR: Script FAILED: Failing Script"
        captured = capfd.readouterr()
        # stdout stays clean and scriptable; the narrative goes to stderr.
        assert captured.out == ""
        assert captured.err.splitlines() == [
            "Running script: Failing Script",
            "Assertion failed at step 3: door_status",
        ]

    async def test_plain_run_does_not_stream_log_lines(self):
        """Only a wait-run streams; a queued run stays quiet as before."""

        async def chatty_handler(reader, writer):
            line = await reader.readline()
            if line:
                writer.write(b"LOG: unrelated daemon chatter\n")
                writer.write(b"OK: Queued script: Custom\n")
                await writer.drain()
            writer.close()

        server, port = await start_fake_daemon(chatty_handler)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                success, msg = await send_command_async(port, "run custom", timeout=2.0)
            finally:
                server.close()
                await server.wait_closed()

        assert success is True
        assert msg == "OK: Queued script: Custom"
        assert err.getvalue() == ""

    async def test_streamed_log_lines_are_sanitized(self, capfd):
        """Streamed daemon output cannot inject ANSI into the operator's shell."""

        async def hostile_handler(reader, writer):
            line = await reader.readline()
            if line:
                writer.write(b"LOG: evil \x1b[2J log\n")
                writer.write(b"OK: Script PASSED: x\n")
                await writer.drain()
            writer.close()

        server, port = await start_fake_daemon(hostile_handler)
        try:
            success, _msg = await send_command_async(port, "run x wait", timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()

        assert success is True
        err = capfd.readouterr().err
        assert "\x1b" not in err
        assert "evil \\x1b[2J log" in err

    async def test_connection_refused_message(self, refused_port):
        success, msg = await send_command_async(refused_port, "status")
        assert success is False
        assert msg == f"Connection refused to 127.0.0.1:{refused_port}"

    async def test_error_response_is_parsed(self):
        server, port = await start_fake_daemon(make_line_responder(b"ERROR: bad thing\n"))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is False
        assert msg == "ERROR: bad thing"

    async def test_connection_closed_without_any_response(self):
        """A daemon that reads the command and closes without replying must
        produce an explanatory message."""

        async def read_then_close(reader, writer):
            await reader.readline()
            writer.close()

        server, port = await start_fake_daemon(read_then_close)
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is False
        assert msg == f"Connection closed without response from 127.0.0.1:{port}"

    def test_connect_timeout_message(self, monkeypatch):
        """A TCP connect timeout gets its own explicit message."""

        def timeout_connect(self, addr):
            raise TimeoutError

        monkeypatch.setattr(socket.socket, "connect", timeout_connect)
        success, msg = ctl.send_command("127.0.0.1", 12345, "status", 0.5)
        assert success is False
        assert msg == "Connection timed out to 127.0.0.1:12345"

    def test_unexpected_connect_error_message(self):
        """Non-timeout, non-refused failures fall into the generic branch."""
        # Port 70000 is out of range: connect() raises OverflowError
        success, msg = ctl.send_command("127.0.0.1", 70000, "status", 0.5)
        assert success is False
        assert msg.startswith("Error: ")
        assert "0-65535" in msg


# ============================================================================
# check_connection
# ============================================================================


class TestCheckConnection:
    """Tests for the pre-session connectivity probe."""

    async def test_listening_daemon_reports_connected(self):
        async def accept_only(reader, writer):
            await reader.read()
            writer.close()

        server, port = await start_fake_daemon(accept_only)
        try:
            connected, error = ctl.check_connection("127.0.0.1", port, timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()
        assert connected is True
        assert error == ""

    async def test_refused_reports_not_running(self, refused_port):
        connected, error = ctl.check_connection("127.0.0.1", refused_port, timeout=2.0)
        assert connected is False
        assert error == f"Connection refused - simulator not running on 127.0.0.1:{refused_port}"

    def test_timeout_reports_timed_out(self, monkeypatch):
        def timeout_connect(self, addr):
            raise TimeoutError

        monkeypatch.setattr(socket.socket, "connect", timeout_connect)
        connected, error = ctl.check_connection("127.0.0.1", 12345, timeout=0.5)
        assert connected is False
        assert error == "Connection timed out to 127.0.0.1:12345"

    def test_unexpected_error_reported(self):
        connected, error = ctl.check_connection("127.0.0.1", 70000, timeout=0.5)
        assert connected is False
        assert error.startswith("Connection error: ")
        assert "0-65535" in error


# ============================================================================
# LocalCommandHandler (ctl-local commands)
# ============================================================================


class TestLocalCommandHandler:
    """Tests for ctl's local command dispatch."""

    def test_help_is_local(self):
        handler = LocalCommandHandler(history=None)
        assert handler.is_local_command("help") is True
        assert handler.is_local_command("?") is True

    def test_exit_is_local(self):
        handler = LocalCommandHandler(history=None)
        assert handler.is_local_command("exit") is True
        assert handler.is_local_command("q") is True

    def test_daemon_commands_are_not_local(self):
        handler = LocalCommandHandler(history=None)
        assert handler.is_local_command("status") is False
        assert handler.is_local_command("power on") is False

    def test_exit_sets_exit_flag(self):
        handler = LocalCommandHandler(history=None)
        result = handler.execute("exit")
        assert result.success is True
        assert result.exit_ctl is True

    def test_extra_arguments_rejected(self):
        """Local no-arg commands reject leftovers instead of ignoring them."""
        handler = LocalCommandHandler(history=None)
        result = handler.execute("exit please")
        assert result.success is False
        assert "Unexpected argument(s): please" in result.message

    def test_no_arg_command_help(self):
        handler = LocalCommandHandler(history=None)
        result = handler.execute("exit help")
        assert result.success is True
        assert "exit" in result.message

    def test_unknown_command(self):
        handler = LocalCommandHandler(history=None)
        result = handler.execute("definitely_not_a_command")
        assert result.success is False
        assert result.message == "Unknown command: definitely_not_a_command"

    def test_empty_line_is_not_local(self):
        handler = LocalCommandHandler(history=None)
        assert handler.is_local_command("") is False

    def test_unknown_command_is_not_local(self):
        handler = LocalCommandHandler(history=None)
        assert handler.is_local_command("frobnicate") is False

    def test_execute_blank_line(self):
        handler = LocalCommandHandler(history=None)
        result = handler.execute("   ")
        assert result.success is False
        assert result.message == "Empty command"


# ============================================================================
# LocalCommandHandler registry-based dispatch paths
# ============================================================================


@pytest.fixture
def registry_command():
    """Inject synthetic commands into the registry, removed on teardown.

    init_command_sets() is forced first so the cached highlighting sets never
    include the synthetic names.
    """
    from powerpetdoor.simulator.prompt_common import init_command_sets

    init_command_sets()
    registry = get_command_registry()
    added: list[str] = []

    def add(name: str, **kwargs) -> CommandInfo:
        info = CommandInfo(name=name, **kwargs)
        registry[name] = info
        added.append(name)
        return info

    yield add
    for name in added:
        registry.pop(name, None)


class TestLocalCommandDispatch:
    """Tests for execute()'s registry traversal and help generation."""

    def test_subcommand_help_generated(self):
        """'<cmd> help' on a subcommand-bearing command lists subcommands."""
        handler = LocalCommandHandler(history=None)
        result = handler.execute("broadcast help")
        assert result.success is True
        lines = result.message.split("\n")
        assert lines[0] == "broadcast subcommands:"
        assert "  status - Broadcast door status" in lines

    def test_arg_help_generated_during_traversal(self):
        """'<cmd> help' on a command with args AND subcommands shows arg help."""
        handler = LocalCommandHandler(history=None)
        result = handler.execute("auto help")
        assert result.success is True
        assert result.message.startswith("auto [on|off]")
        assert "Arguments:" in result.message

    def test_traversal_into_subcommand_handler_error(self):
        """A handler exception is reported, not raised (broadcast needs a
        simulator, which ctl does not have)."""
        handler = LocalCommandHandler(history=None)
        result = handler.execute("broadcast status")
        assert result.success is False
        assert result.message == "'NoneType' object has no attribute 'protocols'"

    def test_unknown_subcommand_lists_alternatives(self):
        handler = LocalCommandHandler(history=None)
        result = handler.execute("broadcast bogus")
        assert result.success is False
        assert result.message == (
            "Unknown broadcast subcommand: bogus\n"
            "Available: all, battery, hwinfo, notifications, schedules, settings, stats, status"
        )

    def test_subcommand_without_handler_falls_through_to_parent(self, registry_command):
        """A registered subcommand with no handler is treated as an argument
        of the parent command."""
        registry_command(
            "loctest_nohandler_sub",
            handler=InfoCommandsMixin.help,
            subcommands={"sub": SubcommandInfo(name="sub", handler=None)},
        )
        handler = LocalCommandHandler(history=None)
        result = handler.execute("loctest_nohandler_sub sub")
        assert result.success is False
        assert result.message == "Unexpected argument(s): sub\nUsage: loctest_nohandler_sub"

    def test_args_stop_subcommand_traversal(self, registry_command):
        """A word that is not a subcommand parses as an argument when the
        command accepts arguments."""
        registry_command(
            "loctest_args",
            handler=InfoCommandsMixin.history,
            args=[ArgSpec("action", "string", required=False, choices=["clear"])],
            subcommands={"sub": SubcommandInfo(name="sub", handler=InfoCommandsMixin.help)},
        )
        handler = LocalCommandHandler(history=History("none"))
        result = handler.execute("loctest_args zap")
        assert result.success is False
        assert result.message == "Invalid argument: zap. Use 'clear' or a number."

    def test_traversal_into_subcommand_with_handler_succeeds(self, registry_command):
        registry_command(
            "loctest_deep",
            handler=InfoCommandsMixin.history,
            subcommands={"sub": SubcommandInfo(name="sub", handler=InfoCommandsMixin.help)},
        )
        handler = LocalCommandHandler(history=None)
        result = handler.execute("loctest_deep sub")
        assert result.success is True
        assert result.message.startswith("Commands:")

    def test_command_without_handler_reports_no_handler(self, registry_command):
        registry_command("loctest_nohandler", handler=None)
        handler = LocalCommandHandler(history=None)
        result = handler.execute("loctest_nohandler")
        assert result.success is False
        assert result.message == "No handler for: loctest_nohandler"

    def test_arg_command_help_request(self):
        handler = LocalCommandHandler(history=History("none"))
        result = handler.execute("history help")
        assert result.success is True
        assert result.message.startswith("history [action]")
        assert "Arguments:" in result.message

    def test_history_without_a_terminal_session_is_unknown(self):
        """ctl answers exactly as the CLI does, not "install prompt_toolkit".

        Under pytest stdin is not a tty, so no history object is registered
        and history is genuinely unavailable for this session.
        """
        handler = LocalCommandHandler(history=None)

        for name in ("history", "hist"):
            result = handler.execute(name)
            assert result.success is False
            assert result.message == f"Unknown command: {name}"

    def test_arg_parse_error_includes_usage(self):
        handler = LocalCommandHandler(history=None)
        result = handler.execute("debug frob")
        assert result.success is False
        assert result.message == "'frob' is not valid. Use on/off\nUsage: debug [on|off]"

    def test_extra_args_rejected_for_arg_command(self):
        handler = LocalCommandHandler(history=History("none"))
        result = handler.execute("history 1 2")
        assert result.success is False
        assert result.message == "Unexpected argument(s): 2\nUsage: history [action]"

    def test_arg_command_executes_with_history(self):
        history = History("none")
        history.prompt_toolkit_history.append_string("status")
        history.prompt_toolkit_history.append_string("help")
        handler = LocalCommandHandler(history=history)
        result = handler.execute("history")
        assert result.success is True
        assert result.message == "History (2 of 2 commands):\n      1  status\n      2  help"

    def test_help_on_no_arg_command_shows_description(self):
        handler = LocalCommandHandler(history=None)
        result = handler.execute("help help")
        assert result.success is True
        assert result.message == "help: Show available commands"


# ============================================================================
# Interactive mode with piped (non-TTY) stdin
# ============================================================================


class TestInteractiveModePipedStdin:
    """Non-TTY stdin uses the plain-input fallback and still detects
    daemon disconnects immediately (no pending Enter required)."""

    async def test_piped_session_runs_and_exits_on_daemon_close(self, monkeypatch, capsys):
        command_seen = asyncio.Event()

        async def daemon_handler(reader, writer):
            # Structured status greeting (prompt coloring signal)
            writer.write(b"STATUS: clients=1\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode().strip()
                if cmd == "status":
                    command_seen.set()
                    writer.write(b"OK: door fine\n")
                    await writer.drain()
                    # Simulate the daemon dying right after the response
                    break
            writer.close()

        server, port = await start_fake_daemon(daemon_handler)

        # Pipe stdin: one command, then the pipe stays OPEN (no EOF) - the
        # session must still end when the daemon disconnects.
        read_fd, write_fd = os.pipe()
        stdin_file = os.fdopen(read_fd, "r")
        monkeypatch.setattr(sys, "stdin", stdin_file)
        os.write(write_fd, b"status\n")

        try:
            await asyncio.wait_for(
                ctl.interactive_mode_async("127.0.0.1", port, port - 1, 2.0, "none"),
                timeout=10,
            )
        finally:
            os.close(write_fd)
            stdin_file.close()
            server.close()
            await server.wait_closed()

        assert command_seen.is_set()
        out = capsys.readouterr().out
        assert ">>> door fine" in out
        assert ">>> Simulator disconnected." in out
        # No prompt_toolkit non-TTY warning / garbled output
        assert "Input is not a terminal" not in out


# ============================================================================
# Argument parsing (main)
# ============================================================================


class TestCtlMain:
    """Tests for the ctl entry point."""

    def test_history_flag_accepted_without_prompt_toolkit(self, monkeypatch):
        """--history must be a valid flag even when prompt_toolkit is absent."""
        monkeypatch.setattr(ctl, "PROMPT_TOOLKIT_AVAILABLE", False)

        calls = {}

        def fake_send(host, port, command, timeout):
            calls["command"] = command
            return True, "OK: fine"

        monkeypatch.setattr(ctl, "send_command", fake_send)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "--history", "none", "status"])

        with pytest.raises(SystemExit) as exc_info:
            ctl.main()
        assert exc_info.value.code == 0
        assert calls["command"] == "status"

    def test_one_shot_failure_exit_code(self, monkeypatch, capsys):
        def fake_send(host, port, command, timeout):
            return False, "ERROR: nope"

        monkeypatch.setattr(ctl, "send_command", fake_send)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "bogus"])

        with pytest.raises(SystemExit) as exc_info:
            ctl.main()
        assert exc_info.value.code == 1
        assert "ERROR: nope" in capsys.readouterr().out

    def test_no_arguments_prints_help_and_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl"])
        with pytest.raises(SystemExit) as exc_info:
            ctl.main()
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "usage:" in out
        assert "Control a running Power Pet Door simulator" in out
        # The epilog is what a bare invocation shows, so it must name the
        # only exit-code-bearing form and the command whose meaning just
        # changed.
        assert "run SCRIPT wait" in out
        assert "exit code reflects PASSED/FAILED" in out
        assert "Stop the running script (not the daemon)" in out
        # "always exits 0" was not literally true - a script that fails to
        # load exits 1, because the load happens before the enqueue.
        assert "Plain 'run SCRIPT' exits 0 as soon as it is queued" in out
        assert "script that fails to load is still an error, and exits 1." in out

    def test_main_forbids_script_path_completion(self, monkeypatch):
        """ctl's daemon refuses script paths, so ctl must not complete them."""
        from powerpetdoor.simulator import scripting

        monkeypatch.setattr(scripting, "_script_paths_allowed", True)

        def fake_send(host, port, command, timeout):
            return True, "OK: fine"

        monkeypatch.setattr(ctl, "send_command", fake_send)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "status"])

        with pytest.raises(SystemExit):
            ctl.main()

        assert scripting._script_paths_allowed is False
        assert scripting.script_completer("./") == []

    def test_interactive_dispatch_defaults(self, monkeypatch):
        """-i runs the interactive loop; door port defaults to port - 1."""
        calls = {}

        async def fake_interactive(host, port, door_port, timeout, history_file):
            calls["args"] = (host, port, door_port, timeout, history_file)

        monkeypatch.setattr(ctl, "interactive_mode_async", fake_interactive)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "-i", "--port", "4001"])
        ctl.main()
        assert calls["args"] == ("127.0.0.1", 4001, 4000, 5.0, str(ctl.HISTORY_FILE))

    def test_interactive_dispatch_explicit_door_port(self, monkeypatch):
        calls = {}

        async def fake_interactive(host, port, door_port, timeout, history_file):
            calls["args"] = (host, port, door_port, timeout, history_file)

        monkeypatch.setattr(ctl, "interactive_mode_async", fake_interactive)
        monkeypatch.setattr(
            sys,
            "argv",
            ["ppd-simulator-ctl", "-i", "-d", "1234", "-t", "2.5", "--history", "none"],
        )
        ctl.main()
        assert calls["args"] == ("127.0.0.1", 3001, 1234, 2.5, "none")

    def test_one_shot_keyboard_interrupt_exits_130(self, monkeypatch, capsys):
        """Ctrl-C during a one-shot command must exit 130, not traceback."""

        def interrupted_send(host, port, command, timeout):
            raise KeyboardInterrupt

        monkeypatch.setattr(ctl, "send_command", interrupted_send)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "status"])
        with pytest.raises(SystemExit) as exc_info:
            ctl.main()
        assert exc_info.value.code == 130
        assert "Interrupted." in capsys.readouterr().out

    def test_interactive_keyboard_interrupt_exits_130(self, monkeypatch, capsys):
        """Ctrl-C during interactive startup must exit 130, not traceback."""

        async def interrupted_interactive(host, port, door_port, timeout, history_file):
            raise KeyboardInterrupt

        monkeypatch.setattr(ctl, "interactive_mode_async", interrupted_interactive)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "-i"])
        with pytest.raises(SystemExit) as exc_info:
            ctl.main()
        assert exc_info.value.code == 130
        assert "Interrupted." in capsys.readouterr().out


# ============================================================================
# Module import without prompt_toolkit
# ============================================================================


class TestModuleWithoutPromptToolkit:
    """The module must import cleanly when prompt_toolkit is unavailable."""

    def test_reload_without_prompt_toolkit(self):
        from powerpetdoor.simulator import prompt_common

        original = prompt_common.PROMPT_TOOLKIT_AVAILABLE
        try:
            prompt_common.PROMPT_TOOLKIT_AVAILABLE = False
            reloaded = importlib.reload(ctl)
            assert reloaded.PROMPT_TOOLKIT_AVAILABLE is False
            # Local commands still work without the toolkit
            result = reloaded.LocalCommandHandler(history=None).execute("exit")
            assert result.exit_ctl is True
        finally:
            prompt_common.PROMPT_TOOLKIT_AVAILABLE = original
            importlib.reload(ctl)


# ============================================================================
# _basic_readline (plain-input fallback primitive)
# ============================================================================


@pytest.fixture
def pipe_stdin(monkeypatch):
    """Replace sys.stdin with the read end of a pipe; yields the write fd."""
    read_fd, write_fd = os.pipe()
    stdin_file = os.fdopen(read_fd, "r")
    monkeypatch.setattr(sys, "stdin", stdin_file)
    yield write_fd
    try:
        os.close(write_fd)
    except OSError:
        pass
    stdin_file.close()


class TestBasicReadline:
    """Tests for the non-blocking stdin reader."""

    async def test_returns_line_and_writes_prompt(self, pipe_stdin, capsys):
        fut = ctl._basic_readline("PROMPT> ")
        os.write(pipe_stdin, b"hello\n")
        line = await asyncio.wait_for(fut, 5)
        assert line == "hello\n"
        assert "PROMPT> " in capsys.readouterr().out

    async def test_eof_returns_none(self, pipe_stdin, capsys):
        fut = ctl._basic_readline("> ")
        os.close(pipe_stdin)
        line = await asyncio.wait_for(fut, 5)
        assert line is None

    async def test_a_non_pollable_stdin_is_read_directly(self, monkeypatch, tmp_path, capsys):
        """/dev/null, a regular file and some heredocs are not pollable.

        `epoll` refuses them with PermissionError out of `add_reader`,
        which used to be a 37-line traceback and rc 1 instead of a prompt.
        """
        script = tmp_path / "commands"
        script.write_text("status\n")

        with script.open() as handle:
            monkeypatch.setattr(sys, "stdin", handle)
            loop = asyncio.get_running_loop()
            with pytest.raises(PermissionError):
                loop.add_reader(handle.fileno(), lambda: None)

            line = await asyncio.wait_for(ctl._basic_readline("> "), 5)
            at_eof = await asyncio.wait_for(ctl._basic_readline("> "), 5)

        assert line == "status\n"
        assert at_eof is None  # EOF still ends the session
        assert capsys.readouterr().out.count("> ") == 2

    async def test_cancel_does_not_consume_pending_input(self, pipe_stdin):
        """A cancelled prompt must leave buffered stdin data untouched and
        must not try to resolve the cancelled future."""
        loop = asyncio.get_running_loop()
        callback_errors: list[dict] = []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: callback_errors.append(ctx))
        try:
            fut = ctl._basic_readline("> ")
            os.write(pipe_stdin, b"later\n")
            # One loop pass: the readability callback is scheduled, then we
            # cancel before it runs (same batch)
            await asyncio.sleep(0)
            fut.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert fut.cancelled() is True
            # No InvalidStateError from setting a result on a cancelled future
            assert callback_errors == []
            # The pending line was NOT consumed by the cancelled reader
            assert sys.stdin.readline() == "later\n"
        finally:
            loop.set_exception_handler(old_handler)

    async def test_cleanup_swallows_a_failing_remove_reader(self, pipe_stdin):
        """The defensive `except` around `loop.remove_reader` in cleanup.

        It carried a `# pragma: no cover` saying it "cannot be triggered
        deterministically" because Linux selectors swallow errors for dead
        fds. That first clause is true - no *real* selector drives this -
        but `loop.remove_reader` is a stdlib API a test can replace, and
        the contract the clause exists for is precisely testable: the error
        must not reach the loop's exception handler. Pinned by this seam
        test instead of hidden by a pragma.
        """
        loop = asyncio.get_running_loop()
        callback_errors: list[dict] = []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: callback_errors.append(ctx))
        real_remove_reader = loop.remove_reader

        def boom(fd):
            raise OSError(9, "Bad file descriptor")

        try:
            fut = ctl._basic_readline("> ")
            loop.remove_reader = boom  # type: ignore[method-assign]
            fut.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            loop.remove_reader = real_remove_reader  # type: ignore[method-assign]
            loop.set_exception_handler(old_handler)

        assert callback_errors == [], f"cleanup let the error escape: {callback_errors}"
        # The reader really was registered, so the cleanup really ran.
        real_remove_reader(sys.stdin.fileno())


class TestEnableLineBuffering:
    """ctl -i off a terminal was block-buffered: 12 s of live log, 0 bytes."""

    def test_reconfigures_a_non_tty_stream(self):
        calls = []

        class FakeStream:
            def isatty(self):
                return False

            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        ctl._enable_line_buffering(FakeStream())

        assert calls == [{"line_buffering": True}]

    def test_leaves_a_terminal_alone(self):
        """A TTY is already line-buffered; reconfiguring it would be noise."""

        class FakeTty:
            def isatty(self):
                return True

            def reconfigure(self, **kwargs):  # pragma: no cover (must not run)
                raise AssertionError("a TTY must not be reconfigured")

        ctl._enable_line_buffering(FakeTty())

    def test_tolerates_a_stream_without_reconfigure(self):
        """A StringIO test double, or an exotic redirect, must not crash ctl."""
        ctl._enable_line_buffering(io.StringIO())

    def test_tolerates_a_stream_without_isatty(self):
        ctl._enable_line_buffering(object())

    async def test_interactive_session_enables_it(self, monkeypatch, refused_port):
        """The session must turn it on before anything is printed."""
        seen = []
        monkeypatch.setattr(ctl, "_enable_line_buffering", lambda stream: seen.append(stream))

        with pytest.raises(SystemExit):
            await ctl.interactive_mode_async(
                "127.0.0.1", refused_port, refused_port - 1, 1.0, "none"
            )

        assert seen == [sys.stdout]


# ============================================================================
# One-shot end-to-end against a real daemon
# ============================================================================


class TestOneShotEndToEnd:
    """send_command against the real daemon control channel."""

    async def test_one_shot_roundtrip_and_shutdown(self):
        ready = asyncio.Event()
        ports: dict[str, int] = {}

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
        try:
            ok, msg = await send_command_async(ports["control"], "status", timeout=5)
            assert ok is True
            assert msg.startswith("OK: ")
            # Multi-line status arrives escaped on the wire and is unescaped
            assert "\n" in msg
            assert "Door:" in msg

            ok, msg = await send_command_async(ports["control"], "definitely_bogus", timeout=5)
            assert ok is False
            assert msg == "ERROR: Unknown command: definitely_bogus. Type 'help' for commands."
        finally:
            ok, msg = await send_command_async(ports["control"], "shutdown", timeout=5)
            await asyncio.wait_for(task, 10)
        assert ok is True
        assert msg == "OK: Shutting down..."


class TestUnanswerableAndOutOfRangeArguments:
    """Two shapes that could never work, refused before a socket is opened.

    `ppd-simulator-ctl ""` sat out the full `--timeout` and then advised
    raising it - the daemon skips blank lines by design, so no answer could
    ever come and the advice was wrong in both halves. `-t 0` put the
    socket in *non-blocking* mode and surfaced `Error: [Errno 115]
    Operation now in progress`; `-t -1` leaked `settimeout`'s own
    ValueError text.
    """

    @pytest.fixture(autouse=True)
    def _no_socket(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("ctl.main() opened a socket; it should have exited first")

        monkeypatch.setattr(ctl, "send_command", _boom)
        monkeypatch.setattr(ctl, "interactive_mode", _boom)

    @pytest.mark.parametrize("command", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
    def test_a_blank_command_is_refused_locally(self, monkeypatch, capsys, command):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", command])

        with pytest.raises(SystemExit) as exc_info:
            ctl.main()

        assert exc_info.value.code == 2
        assert "error: empty command" in capsys.readouterr().err

    @pytest.mark.parametrize("timeout", ["0", "-1", "-0.5"], ids=["zero", "negative", "fractional"])
    def test_a_non_positive_timeout_is_an_argument_error(self, monkeypatch, capsys, timeout):
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "-t", timeout, "status"])

        with pytest.raises(SystemExit) as exc_info:
            ctl.main()

        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert f"error: --timeout {float(timeout):g}: must be greater than 0" in err
        assert "run <script> wait" in err

    def test_the_control_a_positive_timeout_still_works(self, monkeypatch):
        """Rule 8: the smallest usable value is accepted."""
        calls = {}

        def fake_send(host, port, command, timeout):
            calls["timeout"] = timeout
            return True, "OK: fine"

        monkeypatch.setattr(ctl, "send_command", fake_send)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "-t", "0.001", "status"])

        with pytest.raises(SystemExit) as exc_info:
            ctl.main()

        assert exc_info.value.code == 0
        assert calls["timeout"] == 0.001
