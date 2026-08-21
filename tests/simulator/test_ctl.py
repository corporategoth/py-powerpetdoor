# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the simulator control client (ctl.py)."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from powerpetdoor.simulator import ctl
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

    @pytest.mark.asyncio
    async def test_parses_ok_response(self):
        server, port = await start_fake_daemon(make_line_responder(b"OK: hello\n"))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is True
        assert msg == "OK: hello"

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_escaped_newlines_are_unescaped(self):
        server, port = await start_fake_daemon(make_line_responder(b"OK: line1\\nline2\n"))
        try:
            success, msg = await send_command_async(port, "status")
        finally:
            server.close()
            await server.wait_closed()
        assert success is True
        assert msg == "OK: line1\nline2"

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_connection_refused_message(self):
        # Grab a port with no listener by binding then closing
        server, port = await start_fake_daemon(make_line_responder(b""))
        server.close()
        await server.wait_closed()
        success, msg = await send_command_async(port, "status")
        assert success is False
        assert "Connection refused" in msg


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
        assert "Unknown command" in result.message


# ============================================================================
# Interactive mode with piped (non-TTY) stdin
# ============================================================================


class TestInteractiveModePipedStdin:
    """Non-TTY stdin uses the plain-input fallback and still detects
    daemon disconnects immediately (no pending Enter required)."""

    @pytest.mark.asyncio
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
        assert "Simulator disconnected" in out
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

    def test_one_shot_failure_exit_code(self, monkeypatch):
        def fake_send(host, port, command, timeout):
            return False, "ERROR: nope"

        monkeypatch.setattr(ctl, "send_command", fake_send)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator-ctl", "bogus"])

        with pytest.raises(SystemExit) as exc_info:
            ctl.main()
        assert exc_info.value.code == 1
