# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the simulator CLI (cli.py): control channel and entry point."""

from __future__ import annotations

import asyncio
import logging
import sys

import pytest

from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
    cli,
)
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.scripting import ScriptRunner

# ============================================================================
# Helpers / fixtures
# ============================================================================

PASSING_SCRIPT = """\
name: Passing Script
description: A trivially passing script
steps:
  - action: log
    message: hello
"""

FAILING_SCRIPT = """\
name: Failing Script
description: A script that always fails
steps:
  - action: bogus_action
"""


@pytest.fixture
def timing_config():
    """Create a fast timing config for tests."""
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_absolute_path_rejected(self, control_setup, tmp_path):
        _, channel, _ = control_setup
        secret = tmp_path / "secret.yaml"
        secret.write_text(PASSING_SCRIPT)
        response = await self._run_over_channel(channel, f"run {secret}")
        assert response.startswith("ERROR:")
        assert "not allowed" in response

    @pytest.mark.asyncio
    async def test_relative_traversal_rejected(self, control_setup):
        _, channel, _ = control_setup
        response = await self._run_over_channel(channel, "run ../../etc/passwd")
        assert response.startswith("ERROR:")
        assert "not allowed" in response

    @pytest.mark.asyncio
    async def test_backslash_path_rejected(self, control_setup):
        _, channel, _ = control_setup
        response = await self._run_over_channel(channel, "run ..\\..\\secret")
        assert response.startswith("ERROR:")
        assert "not allowed" in response

    @pytest.mark.asyncio
    async def test_dotfile_rejected(self, control_setup):
        _, channel, _ = control_setup
        response = await self._run_over_channel(channel, "run .hidden")
        assert response.startswith("ERROR:")
        assert "not allowed" in response

    @pytest.mark.asyncio
    async def test_bare_name_resolves_in_scripts_dir(self, control_setup):
        """A bare name resolves against the configured scripts directory."""
        _, channel, scripts_dir = control_setup
        (scripts_dir / "myscript.yaml").write_text(PASSING_SCRIPT)
        response = await self._run_over_channel(channel, "run myscript")
        assert response.startswith("OK:")
        assert "PASSED" in response

    @pytest.mark.asyncio
    async def test_unknown_bare_name_lists_builtins(self, control_setup):
        _, channel, _ = control_setup
        response = await self._run_over_channel(channel, "run no_such_script")
        assert response.startswith("ERROR:")
        assert "Unknown built-in script" in response


# ============================================================================
# run_simulator end-to-end (daemon mode)
# ============================================================================


class TestRunSimulatorDaemon:
    """End-to-end daemon-mode test of run_simulator."""

    @pytest.mark.asyncio
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

    def _run_main(self, monkeypatch, argv, fake_run=None):
        captured = {}

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

    def test_keyboard_interrupt_exits_130(self, monkeypatch):
        """An interrupted run must not report success to CI."""

        async def interrupted_run(**kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_simulator", interrupted_run)
        monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--oneshot", "-s", "basic_cycle"])

        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 130
