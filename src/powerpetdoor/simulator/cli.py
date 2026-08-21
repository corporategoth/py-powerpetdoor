# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""CLI for Power Pet Door simulator.

This module provides the interactive command-line interface for running
and controlling the door simulator.
"""

import asyncio
import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..tz_utils import async_init_timezone_cache
from .commands import CommandHandler
from .prompt_common import (
    CLI_HISTORY_FILE as HISTORY_FILE,
)

# Import shared prompt_toolkit components
from .prompt_common import (
    PROMPT_TOOLKIT_AVAILABLE,
    InteractiveSession,
    escape_message,
    sanitize_text,
    use_prompt_toolkit,
)
from .server import DoorSimulator

if PROMPT_TOOLKIT_AVAILABLE:
    from prompt_toolkit.patch_stdout import patch_stdout

if TYPE_CHECKING:
    from typing import TextIO

    from .scripting import ScriptRunner
    from .state import DoorSimulatorState

logger = logging.getLogger(__name__)


class _SanitizingFormatter(logging.Formatter):
    """Formatter that neutralizes control characters in log output.

    Log records can carry network-derived data (e.g. unknown protocol
    commands); sanitizing at format time keeps ANSI escapes out of the
    operator's terminal.
    """

    def format(self, record) -> str:
        return sanitize_text(super().format(record))


class InteractivePrompt:
    """Manages interactive prompt with proper output handling.

    When enabled, this class handles displaying a prompt and ensuring
    that async output (like log messages) properly clears the line
    before printing and restores the prompt afterward.
    """

    def __init__(self, prompt: str = "$ "):
        self.prompt = prompt
        self._enabled = False
        self._handler: logging.Handler | None = None
        self._saved_handlers: list[logging.Handler] = []

    def enable(self):
        """Enable the prompt and install the logging handler."""
        if self._enabled:
            return
        self._enabled = True

        # Remove existing handlers and save them for later restoration
        root_logger = logging.getLogger()
        self._saved_handlers = list(root_logger.handlers)
        for handler in self._saved_handlers:
            root_logger.removeHandler(handler)

        # Install a custom handler that clears line before output
        self._handler = _PromptLoggingHandler(self)
        self._handler.setFormatter(_SanitizingFormatter("%(asctime)s [%(levelname)s] %(message)s"))
        root_logger.addHandler(self._handler)
        self.show()

    def disable(self):
        """Disable the prompt and remove the logging handler."""
        if not self._enabled:
            return
        self._enabled = False

        root_logger = logging.getLogger()
        if self._handler:  # pragma: no branch (defensive: enable() always installs a handler)
            root_logger.removeHandler(self._handler)
            self._handler = None

        # Restore saved handlers
        for handler in self._saved_handlers:
            root_logger.addHandler(handler)
        self._saved_handlers = []

    def show(self):
        """Display the prompt."""
        if self._enabled:
            sys.stdout.write(self.prompt)
            sys.stdout.flush()

    def clear_line(self):
        """Clear the current line (prompt and any partial input)."""
        if self._enabled:
            # Move to start of line and clear to end
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def output(self, text: str):
        """Print text, handling prompt correctly."""
        self.clear_line()
        print(text)
        self.show()


class _PromptLoggingHandler(logging.Handler):
    """Logging handler that respects the interactive prompt."""

    def __init__(self, prompt: InteractivePrompt):
        super().__init__()
        self._prompt = prompt

    def emit(self, record):
        try:
            msg = self.format(record)
            self._prompt.clear_line()
            print(msg)
            self._prompt.show()
        except Exception:
            self.handleError(record)


# Default control port offset from simulator port
CONTROL_PORT_OFFSET = 1

# Default bind address for the (unauthenticated) daemon control channel.
# Loopback by default; widening requires an explicit --control-host.
DEFAULT_CONTROL_HOST = "127.0.0.1"


class _ControlLogHandler(logging.Handler):
    """Logging handler that broadcasts sanitized log lines to control clients."""

    def __init__(self, clients: set[asyncio.StreamWriter]):
        super().__init__()
        self._clients = clients

    def emit(self, record):
        try:
            msg = self.format(record)
            # Sanitize control characters and escape newlines so untrusted
            # (network-derived) data can neither inject terminal escapes nor
            # forge extra protocol lines.
            data = f"LOG: {escape_message(sanitize_text(msg))}\n".encode()
            # Broadcast to all connected control clients
            for writer in list(self._clients):
                try:
                    writer.write(data)
                    # Don't await drain here - it would block.
                    # The message will be sent eventually.
                except Exception:
                    # Client disconnected, will be cleaned up later
                    pass
        except Exception:
            self.handleError(record)


class ControlChannel:
    """Line-based TCP control channel for daemon mode.

    Protocol (one message per line, ``\\n`` terminated):

    - client -> server: ``<command>``
    - server -> client: ``OK: <msg>`` / ``ERROR: <msg>`` (command responses,
      newlines escaped as ``\\n``, control characters sanitized)
    - server -> client: ``LOG: <msg>`` (broadcast log records, same escaping)
    - server -> client: ``STATUS: clients=<n>`` (door-client count; sent on
      connect and whenever a door client connects or disconnects)

    The channel is UNAUTHENTICATED - bind it to loopback unless remote
    control is explicitly desired.
    """

    def __init__(
        self,
        cmd_handler: CommandHandler,
        host: str,
        port: int,
        stop_event: asyncio.Event,
        client_count: Callable[[], int] | None = None,
    ):
        self.cmd_handler = cmd_handler
        self.host = host
        self.port = port
        self.stop_event = stop_event
        self._client_count = client_count or (lambda: 0)
        self.server: asyncio.Server | None = None
        self.clients: set[asyncio.StreamWriter] = set()
        self.log_handler: logging.Handler | None = None

    @property
    def bound_port(self) -> int:
        """The actual port the control server is listening on."""
        if self.server and self.server.sockets:
            return self.server.sockets[0].getsockname()[1]
        return self.port

    async def start(self):
        """Start the control server and install the log broadcast handler."""
        self.server = await asyncio.start_server(self._handle_client, self.host, self.port)
        logger.info(f"Control server listening on {self.host}:{self.bound_port}")

        self.log_handler = _ControlLogHandler(self.clients)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(self.log_handler)

    async def stop(self):
        """Remove the log handler and shut the control server down."""
        if self.log_handler:
            logging.getLogger().removeHandler(self.log_handler)
            self.log_handler = None
        # Close lingering client connections so their handlers finish
        # (wait_closed waits for handler completion on Python 3.12.1+)
        for writer in list(self.clients):
            try:
                writer.close()
            except Exception:
                pass
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    def broadcast_status(self):
        """Broadcast the door-client count to all control clients.

        This is the structured signal ctl uses for prompt coloring instead of
        scraping human-readable log lines.
        """
        data = f"STATUS: clients={self._client_count()}\n".encode()
        for writer in list(self.clients):
            try:
                writer.write(data)
            except Exception:
                pass

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a control connection."""
        addr = writer.get_extra_info("peername")
        logger.info(f"Control connection from {addr}")
        self.clients.add(writer)
        try:
            # Send initial door-client status so ctl can color its prompt
            writer.write(f"STATUS: clients={self._client_count()}\n".encode())
            await writer.drain()

            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode(errors="replace").strip()
                if not cmd:
                    continue

                result = await self.cmd_handler.execute(cmd)
                # Sanitize control chars, then escape newlines for the protocol
                # (ctl will unescape)
                escaped_msg = escape_message(sanitize_text(result.message))
                if result.success:
                    writer.write(f"OK: {escaped_msg}\n".encode())
                else:
                    writer.write(f"ERROR: {escaped_msg}\n".encode())
                await writer.drain()

                # Check if we should exit
                if self.stop_event.is_set():
                    break
        except Exception as e:
            logger.error(f"Control client error: {e}")
        finally:
            self.clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(f"Control connection closed from {addr}")


def _build_state(
    firmware: tuple[int, int, int] | None,
    hardware: tuple[str, str] | None,
) -> "DoorSimulatorState | None":
    """Build a simulator state for custom firmware/hardware versions.

    Returns None when no override is requested (the simulator then uses its
    default state).
    """
    from .state import DoorSimulatorState

    if not (firmware or hardware):
        return None
    kwargs: dict[str, int | str] = {}
    if firmware:
        kwargs["fw_major"] = firmware[0]
        kwargs["fw_minor"] = firmware[1]
        kwargs["fw_patch"] = firmware[2]
    if hardware:
        kwargs["hw_ver"] = hardware[0]
        kwargs["hw_rev"] = hardware[1]
    return DoorSimulatorState(**kwargs)  # type: ignore[arg-type]


async def _process_script_queue(
    script_queue: "asyncio.Queue[str]",
    stop_event: asyncio.Event,
    cmd_handler: CommandHandler,
    script_runner: "ScriptRunner",
    poll_interval: float = 0.5,
) -> None:
    """Run scripts queued at runtime (via the ``run`` command) until stopped."""
    while not stop_event.is_set():
        try:
            try:
                script_ref = await asyncio.wait_for(script_queue.get(), timeout=poll_interval)
            except TimeoutError:
                continue

            try:
                script = cmd_handler.load_script(script_ref)
                logger.info(f"Running queued script: {script.name}")
                success = await script_runner.run(script)
                logger.info(f"Script {'PASSED' if success else 'FAILED'}: {script.name}")
            except Exception as e:
                logger.error(f"Error running queued script: {e}")
        except asyncio.CancelledError:
            break


async def _run_startup_scripts(
    scripts: list[str],
    *,
    simulator: DoorSimulator,
    cmd_handler: CommandHandler,
    script_runner: "ScriptRunner",
    stop_event: asyncio.Event,
    script_result: list,
    loop_scripts: bool,
    script_delay: float,
    oneshot: bool,
    wait_for_client: bool,
) -> None:
    """Run the startup scripts requested on the command line.

    Sets ``script_result[0]`` to the overall pass/fail result and, in oneshot
    mode, sets ``stop_event`` once the scripts complete.
    """
    all_success = True
    run_count = 0
    try:
        # Wait for client connection if requested
        if wait_for_client:
            print(">>> Waiting for client connection...")
            while not simulator.protocols:
                if stop_event.is_set():
                    return
                await asyncio.sleep(0.1)
            print(">>> Client connected, starting scripts")

        while True:
            # Check for client disconnect if wait_for_client
            if wait_for_client and not simulator.protocols:
                print(">>> Client disconnected, stopping scripts")
                break

            run_count += 1
            if loop_scripts:
                print(f"\n>>> Script run #{run_count}")

            for i, script_ref in enumerate(scripts):
                # Check for disconnect before each script
                if wait_for_client and not simulator.protocols:
                    print(">>> Client disconnected, stopping scripts")
                    break

                # Add delay between scripts (not before first one)
                if i > 0 and script_delay > 0:
                    print(f">>> Waiting {script_delay}s before next script...")
                    await asyncio.sleep(script_delay)

                try:
                    script = cmd_handler.load_script(script_ref)
                    print(f"\n>>> Running script: {script.name}")
                    success = await script_runner.run(script)
                    if not success:
                        all_success = False
                        print(f">>> Script FAILED: {script.name}")
                    else:
                        print(f">>> Script PASSED: {script.name}")
                except Exception as e:
                    print(f"Error running script '{script_ref}': {e}")
                    all_success = False
            else:
                # Loop completed without break (no disconnect)
                if not loop_scripts:
                    break

                # Delay before next loop iteration
                if script_delay > 0:
                    print(f">>> Waiting {script_delay}s before next loop...")
                    await asyncio.sleep(script_delay)
                continue

            # Inner loop was broken (disconnect), exit outer loop too
            break

    except asyncio.CancelledError:
        pass
    finally:
        script_result[0] = all_success
        if oneshot:
            print(f"\n>>> All scripts {'PASSED' if all_success else 'FAILED'}")
            stop_event.set()


class _BasicStdinInput:
    """Plain-input interactive fallback (no prompt_toolkit or non-TTY stdin).

    Reads commands from stdin via the event loop's reader callback and
    executes them, coordinating output with an :class:`InteractivePrompt`.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        prompt: InteractivePrompt,
        cmd_handler: CommandHandler,
        stop_event: asyncio.Event,
        stdin: "TextIO | None" = None,
    ):
        self._loop = loop
        self._prompt = prompt
        self._cmd_handler = cmd_handler
        self._stop_event = stop_event
        self._stdin = stdin if stdin is not None else sys.stdin
        self._reader_removed = False

    def start(self) -> None:
        """Enable the prompt and start reading stdin."""
        self._prompt.enable()
        self._loop.add_reader(self._stdin.fileno(), self.handle_input)

    def stop(self) -> None:
        """Remove the stdin reader (idempotent; safe if never added)."""
        self._reader_removed = True
        try:
            self._loop.remove_reader(self._stdin.fileno())
        except Exception:
            pass  # Already removed

    def handle_input(self) -> None:
        """Reader callback: consume one line and schedule its execution."""
        # Don't read if we're shutting down (prevents blocking)
        if self._stop_event.is_set() or self._reader_removed:
            return
        try:
            line = self._stdin.readline().strip()
            if line:
                asyncio.create_task(self.process_command(line))
            else:
                # Empty line (just Enter), re-show prompt
                self._prompt.show()
        except Exception as e:
            self._prompt.output(f"Error: {e}")

    async def process_command(self, line: str) -> None:
        """Execute one command line and print its result."""
        result = await self._cmd_handler.execute(line)
        # Don't show prompt again after shutdown command
        if self._stop_event.is_set():
            self._prompt.clear_line()
            if result.message:
                print(f">>> {result.message}")
            # Remove stdin reader immediately to avoid blocking shutdown
            self.stop()
        elif result.message:
            self._prompt.output(f">>> {result.message}")
        else:
            self._prompt.show()


async def run_simulator(
    host: str = "0.0.0.0",
    port: int = 3000,
    scripts: list[str] | None = None,
    loop_scripts: bool = False,
    script_delay: float = 0,
    oneshot: bool = False,
    daemon: bool = False,
    run_for: float | None = None,
    wait_for_client: bool = False,
    control_port: int | None = None,
    control_host: str = DEFAULT_CONTROL_HOST,
    scripts_dir: str | None = None,
    history_file: str | None = None,
    firmware: tuple[int, int, int] | None = None,
    hardware: tuple[str, str] | None = None,
    on_ready: Callable[[int, int | None], None] | None = None,
):
    """Run the Power Pet Door simulator.

    Args:
        host: Address to bind the door-protocol server
        port: Port to listen on for door protocol
        scripts: List of scripts to run (file paths or built-in names, auto-detected).
                 Implies non-interactive mode.
        loop_scripts: If True, run scripts continuously in a loop
        script_delay: Delay in seconds between script runs
        oneshot: If True, exit after scripts complete (even if run_for is set)
        daemon: If True, run without interactive input and no scripts.
        run_for: Maximum run time in seconds (oneshot can exit earlier)
        wait_for_client: If True, delay script start until a client connects
        control_port: Port for control commands (only used in daemon mode;
                      main() defaults it to port + 1 there)
        control_host: Address to bind the control channel (default: 127.0.0.1).
                      The control channel is unauthenticated - widening this
                      exposes full simulator control to the network.
        scripts_dir: Optional directory of extra scripts runnable by bare name
        history_file: History file path, or 'none' to disable
        firmware: Firmware version as (major, minor, patch) tuple
        hardware: Hardware version as (ver, rev) tuple
        on_ready: Optional callback invoked once servers are listening, with
                  (door_port, control_port) actual bound ports. Useful for
                  tests and embedding with ephemeral ports.

    Returns:
        Script result (True if all passed, False if any failed, None if no scripts)
    """
    from .scripting import ScriptRunner

    # Initialize timezone cache for IANA to POSIX conversion
    await async_init_timezone_cache()

    # Create state with optional firmware/hardware version
    state = _build_state(firmware, hardware)

    # Holder for interactive session (set later if in interactive mode)
    # Used by callbacks to invalidate prompt on connect/disconnect
    session_holder: list[InteractiveSession | None] = [None]
    # Holder for the control channel (set later in daemon mode)
    # Used by callbacks to broadcast door-client status to control clients
    channel_holder: list[ControlChannel | None] = [None]

    def on_client_connect():
        """Called when a client connects - update prompt color / notify ctl."""
        if session_holder[0]:
            session_holder[0].invalidate()
        if channel_holder[0]:
            channel_holder[0].broadcast_status()

    def on_client_disconnect():
        """Called when a client disconnects - update prompt color / notify ctl."""
        if session_holder[0]:
            session_holder[0].invalidate()
        if channel_holder[0]:
            channel_holder[0].broadcast_status()

    # Start the simulator
    simulator = DoorSimulator(
        host=host,
        port=port,
        state=state,
        on_connect=on_client_connect,
        on_disconnect=on_client_disconnect,
    )
    await simulator.start()

    # The actual bound port (differs from `port` when an ephemeral port 0 was
    # requested, e.g. in tests)
    actual_port = port
    if simulator.server and simulator.server.sockets:  # pragma: no branch (bound after start())
        actual_port = simulator.server.sockets[0].getsockname()[1]

    script_runner = ScriptRunner(simulator)

    # Set up control structures
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    script_result = [None]  # Use list to allow mutation in nested function
    script_queue: asyncio.Queue[str] = asyncio.Queue()

    # Create command handler. In daemon mode the handler serves the
    # unauthenticated control channel, so restrict script running to bare
    # names (no arbitrary filesystem paths).
    cmd_handler = CommandHandler(
        simulator=simulator,
        script_runner=script_runner,
        stop_callback=stop_event.set,
        script_queue=script_queue,
        scripts_dir=scripts_dir,
        allow_script_paths=not daemon,
    )

    # Determine mode
    interactive = not scripts and not daemon

    # Set interactive and CLI mode before printing help so interactive-only commands appear
    # and exit/q/quit are shown as aliases for shutdown
    if interactive:
        cmd_handler.set_interactive_mode(True)
        cmd_handler.set_cli_mode(True)

    # Start control channel if configured
    control_channel: ControlChannel | None = None
    if control_port is not None:
        control_channel = ControlChannel(
            cmd_handler=cmd_handler,
            host=control_host,
            port=control_port,
            stop_event=stop_event,
            client_count=lambda: len(simulator.protocols),
        )
        await control_channel.start()
        channel_holder[0] = control_channel

    # Print startup info
    print(f"Simulator started on {host}:{actual_port}")
    if control_channel:
        print(f"Control channel: {control_host}:{control_channel.bound_port}")
    if interactive:
        print("=" * 65)
        print(cmd_handler.get_help())
        print("=" * 65)
    print()

    if on_ready:
        on_ready(actual_port, control_channel.bound_port if control_channel else None)

    # Process queued scripts in background
    queue_task = asyncio.create_task(
        _process_script_queue(script_queue, stop_event, cmd_handler, script_runner)
    )

    # Run startup scripts if specified
    if scripts:
        asyncio.create_task(
            _run_startup_scripts(
                scripts,
                simulator=simulator,
                cmd_handler=cmd_handler,
                script_runner=script_runner,
                stop_event=stop_event,
                script_result=script_result,
                loop_scripts=loop_scripts,
                script_delay=script_delay,
                oneshot=oneshot,
                wait_for_client=wait_for_client,
            )
        )

    # Set up interactive input if applicable
    stdin_available = False
    prompt: InteractivePrompt | None = None
    basic_input: _BasicStdinInput | None = None
    input_task: asyncio.Task | None = None
    stdout_ctx = None  # prompt_toolkit patch_stdout context

    if interactive:
        try:
            if sys.stdin and sys.stdin.fileno() >= 0:
                import os

                os.fstat(sys.stdin.fileno())
                stdin_available = True
        except (OSError, ValueError, AttributeError):
            pass

        if stdin_available:
            # Only use prompt_toolkit on a real terminal; piped/dumb-terminal
            # stdin falls back to plain input to avoid garbled output
            if use_prompt_toolkit():
                # Use InteractiveSession.create for standard prompt setup
                history_path = history_file if history_file else str(HISTORY_FILE)
                session = InteractiveSession.create(
                    host=host,
                    port=actual_port,
                    history_file=history_path,
                    is_connected=lambda: bool(simulator.protocols),
                )

                # Store in holder so connect/disconnect callbacks can invalidate
                session_holder[0] = session

                # Register history with command handler (for history command)
                if session.history:
                    cmd_handler.set_history(session.history)

                async def interactive_input_loop():
                    """Async input loop using prompt_toolkit."""
                    try:
                        async for input_line in session.input_loop(stop_check=stop_event.is_set):
                            if input_line.was_history_recall:
                                print(f">>> {input_line.original} -> {input_line.resolved}")

                            result = await cmd_handler.execute(input_line.resolved)
                            session.handle_result(input_line, result.success)

                            if result.message:
                                print(f">>> {result.message}")
                            if stop_event.is_set():
                                break
                    except asyncio.CancelledError:
                        pass
                    finally:
                        # Signal stop on EOF
                        stop_event.set()

                # Enter patch_stdout context for the rest of the run
                # This ensures all log output is handled properly with the prompt
                stdout_ctx = patch_stdout()
                stdout_ctx.__enter__()

                # Reinstall logging to use patched stderr
                root_logger = logging.getLogger()
                for handler in root_logger.handlers[:]:
                    if isinstance(handler, logging.StreamHandler):
                        root_logger.removeHandler(handler)
                new_handler = logging.StreamHandler(sys.stderr)
                new_handler.setFormatter(
                    _SanitizingFormatter("%(asctime)s [%(levelname)s] %(message)s")
                )
                root_logger.addHandler(new_handler)

                input_task = asyncio.create_task(interactive_input_loop())
            else:
                # Fallback to basic input with InteractivePrompt
                prompt = InteractivePrompt(f"{host}:{actual_port}> ")
                basic_input = _BasicStdinInput(loop, prompt, cmd_handler, stop_event)
                basic_input.start()
        else:
            logger.warning("stdin not available, running in daemon mode")

    # Handle run_for timeout
    if run_for:

        async def timeout_shutdown():
            await asyncio.sleep(run_for)
            logger.info(f"Run time ({run_for}s) elapsed, shutting down")
            stop_event.set()

        asyncio.create_task(timeout_shutdown())

    # Wait for stop signal
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        # Cleanup
        if input_task:
            input_task.cancel()
            try:
                await input_task
            except asyncio.CancelledError:
                pass
        if stdout_ctx:
            stdout_ctx.__exit__(None, None, None)
        if prompt:
            prompt.disable()
        if basic_input:
            basic_input.stop()
        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass
        if control_channel:
            await control_channel.stop()
        await simulator.stop()

    return script_result[0]


def main():
    """CLI entry point for the simulator."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Power Pet Door Simulator - Fake door for testing")
    parser.add_argument(
        "--host", "-H", default="0.0.0.0", help="Address to bind (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", "-p", type=int, default=3000, help="Port to listen on (default: 3000)"
    )
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--script",
        "-s",
        action="append",
        dest="scripts",
        metavar="SCRIPT",
        help="Run a script (built-in name or file path, auto-detected). "
        "Can be specified multiple times to run scripts in sequence. "
        "Implies non-interactive mode.",
    )
    parser.add_argument("--loop", action="store_true", help="Run scripts continuously in a loop")
    parser.add_argument(
        "--script-delay",
        type=float,
        default=0,
        metavar="SECONDS",
        help="Delay between scripts and loop iterations (default: 0)",
    )
    parser.add_argument(
        "--oneshot",
        action="store_true",
        help="Exit after scripts complete (useful for CI/CD). Takes precedence over --run-for.",
    )
    parser.add_argument(
        "--wait-for-client",
        "-w",
        action="store_true",
        help="Wait for a client to connect before starting scripts. "
        "Scripts stop if client disconnects.",
    )
    parser.add_argument(
        "--list-scripts", "-l", action="store_true", help="List available built-in scripts and exit"
    )
    parser.add_argument(
        "--daemon",
        "-D",
        nargs="?",
        type=int,
        const=-1,  # Sentinel: use default (port+1)
        default=None,
        metavar="CONTROL_PORT",
        help="Run in daemon mode (no interactive input, no scripts). "
        "Optionally specify control port (default: PORT+1). "
        "Mutually exclusive with --script.",
    )
    parser.add_argument(
        "--control-host",
        metavar="ADDRESS",
        default=DEFAULT_CONTROL_HOST,
        help=f"Address to bind the daemon control channel (default: {DEFAULT_CONTROL_HOST}). "
        "WARNING: the control channel is UNAUTHENTICATED - anyone who can reach it "
        "can shut down or fully reconfigure the simulator. Only widen this "
        "(e.g. 0.0.0.0) on a trusted network.",
    )
    parser.add_argument(
        "--scripts-dir",
        metavar="DIR",
        default=None,
        help="Directory of extra YAML scripts runnable by bare name "
        "(in addition to the built-in scripts)",
    )
    parser.add_argument(
        "--run-for",
        "-r",
        type=float,
        metavar="SECONDS",
        help="Maximum run time in seconds (--oneshot can exit earlier)",
    )
    # Always registered so command lines are portable; ignored (with a note in
    # the help text) when prompt_toolkit is not installed
    parser.add_argument(
        "--history",
        metavar="FILE",
        default=str(HISTORY_FILE),
        help=f"History file path, or 'none' to disable (default: {HISTORY_FILE}). "
        "Requires prompt_toolkit; ignored otherwise.",
    )
    parser.add_argument(
        "--firmware",
        "-f",
        metavar="VERSION",
        help="Firmware version to report (e.g., '1.2.3', default: 1.2.3)",
    )
    parser.add_argument(
        "--hardware",
        metavar="VERSION",
        help="Hardware version to report (e.g., '1.1' for 'ver 1 rev 1', default: 1.1)",
    )

    args = parser.parse_args()

    # Set history_file to None if prompt_toolkit not available
    if not PROMPT_TOOLKIT_AVAILABLE:
        args.history = None

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # List scripts and exit
    if args.list_scripts:
        from .scripting import list_builtin_scripts

        print("Available built-in scripts:")
        for name, desc in list_builtin_scripts():
            print(f"  {name}: {desc}")
        return

    # Determine daemon mode and control port
    daemon = args.daemon is not None

    # Validate mutually exclusive options
    if args.scripts and daemon:
        parser.error("--script and --daemon are mutually exclusive")

    if daemon:
        # -1 means use default (port+1), otherwise use specified port
        control_port = args.port + 1 if args.daemon == -1 else args.daemon
    else:
        control_port = None

    # Parse firmware version if provided
    firmware = None
    if args.firmware:
        try:
            parts = args.firmware.split(".")
            if len(parts) != 3:
                parser.error("Firmware version must be in format major.minor.patch (e.g., '1.2.3')")
            firmware = (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            parser.error("Firmware version must contain only numbers (e.g., '1.2.3')")

    # Parse hardware version if provided
    hardware = None
    if args.hardware:
        parts = args.hardware.split(".")
        if len(parts) != 2:
            parser.error("Hardware version must be in format ver.rev (e.g., '1.1')")
        hardware = (parts[0], parts[1])

    try:
        result = asyncio.run(
            run_simulator(
                host=args.host,
                port=args.port,
                scripts=args.scripts,
                loop_scripts=args.loop,
                script_delay=args.script_delay,
                oneshot=args.oneshot,
                daemon=daemon,
                run_for=args.run_for,
                wait_for_client=args.wait_for_client,
                control_port=control_port,
                control_host=args.control_host,
                scripts_dir=args.scripts_dir,
                history_file=args.history,
                firmware=firmware,
                hardware=hardware,
            )
        )

        # Exit with appropriate code for CI/CD
        if args.oneshot and result is not None:
            sys.exit(0 if result else 1)

    except KeyboardInterrupt:
        print("\nSimulator stopped.")
        # Interrupted runs must not report success to CI (128 + SIGINT)
        sys.exit(130)


if __name__ == "__main__":
    main()
