# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Control client for the Power Pet Door simulator.

This module provides a command-line tool to send commands to a running
simulator's control port. It uses the same prompt infrastructure as the
main CLI for consistent syntax highlighting and tab completion.
"""

import argparse
import asyncio
import socket
import sys
from typing import TYPE_CHECKING, cast

from ..sanitize import sanitize_text
from ..tz_utils import async_init_timezone_cache

# Import command infrastructure for local command handling
from .commands.base import (
    CommandResult,
    SubcommandInfo,
    get_command_registry,
    parse_args,
)
from .commands.control import ControlCommandsMixin
from .commands.history import History
from .commands.info import InfoCommandsMixin
from .commands.scripts import is_wait_run
from .prompt_common import (
    CTL_HISTORY_FILE as HISTORY_FILE,
)

# Import shared prompt_toolkit components
from .prompt_common import (
    PROMPT_TOOLKIT_AVAILABLE,
    InputLine,
    InteractiveSession,
    render_result,
    unescape_message,
)
from .scripting import set_script_paths_allowed

if TYPE_CHECKING:
    from .server import DoorSimulator


class LocalCommandResult:
    """Result of executing a local command."""

    def __init__(self, success: bool, message: str, exit_ctl: bool = False):
        self.success = success
        self.message = message
        self.exit_ctl = exit_ctl  # If True, ctl should exit


class LocalCommandHandler(InfoCommandsMixin, ControlCommandsMixin):
    """Handles local commands in ctl using the command registry.

    Uses the same dispatch mechanism as CommandHandler but only for local_only
    commands. Inherits from InfoCommandsMixin and ControlCommandsMixin to get
    the actual command implementations.
    """

    def __init__(self, history: History | None = None):
        self._interactive_mode = True  # ctl interactive mode
        self._cli_mode = False  # Not CLI mode, so exit is separate command
        self._history_obj = history  # History class instance
        self._history = history.prompt_toolkit_history if history else None  # For InfoCommandsMixin
        # Local commands never touch the simulator; the cast satisfies the
        # mixins' declared type without constructing a simulator here.
        self.simulator = cast("DoorSimulator", None)  # Not needed for local commands
        self.stop_callback = lambda: None  # Placeholder, not used for local commands

    def exit_ctl(self) -> CommandResult:
        """Override exit to signal ctl should exit.

        Returns a result with a special marker that the caller checks.
        """
        # Return a marker that execute() will detect
        return CommandResult(True, "__EXIT_CTL__")

    def is_local_command(self, line: str) -> bool:
        """Check if a command should be handled locally.

        A command is local if:
        - It's marked as local_only in the registry (exit, clear, history)
        - It's the top-level help/? command (ctl generates its own help)

        Note: Subcommand help (e.g., "schedule add help") is sent to the daemon,
        which has all the command handlers and can generate accurate help.
        """
        parts = line.split()
        if not parts:
            return False

        cmd = parts[0].lower()

        # Top-level help is handled locally to show ctl-specific help
        if cmd in ("help", "?"):
            return True

        registry = get_command_registry()

        if cmd not in registry:
            return False

        cmd_info = registry[cmd]

        # Check if it's marked as local_only
        if cmd_info.local_only:
            return True

        return False

    def execute(self, line: str) -> LocalCommandResult:
        """Execute a local command using registry-based dispatch.

        Args:
            line: The command line to execute

        Returns:
            LocalCommandResult with success status, message, and exit flag
        """
        registry = get_command_registry()

        parts = line.split()
        if not parts:
            return LocalCommandResult(False, "Empty command")

        cmd = parts[0].lower()

        # Look up command in registry
        if cmd not in registry:
            return LocalCommandResult(False, f"Unknown command: {cmd}")

        # History needs a real terminal session. Answer exactly as the CLI
        # does (and as ctl's own help already implies by hiding it) rather
        # than blaming a missing prompt_toolkit that may well be installed.
        if cmd in ("history", "hist") and not self._is_history_available():
            return LocalCommandResult(False, f"Unknown command: {cmd}")

        info: SubcommandInfo = registry[cmd]
        cmd_path = [info.name]

        # Traverse subcommand hierarchy
        part_idx = 1
        while part_idx < len(parts) and info.subcommands:
            subcmd = parts[part_idx].lower()

            # Handle implicit help/? subcommand (the loop condition guarantees
            # info.subcommands is non-empty here)
            if subcmd in ("help", "?"):
                if info.args:
                    help_text = self._get_arg_help(info, cmd_path)
                else:
                    help_text = self._get_subcommand_help(info, cmd_path)
                return LocalCommandResult(True, help_text)

            if subcmd in info.subcommands:
                subinfo = info.subcommands[subcmd]
                if subinfo.handler is not None:
                    info = subinfo
                    cmd_path.append(subinfo.name)
                    part_idx += 1
                else:
                    break
            else:
                if info.args:
                    break
                subnames = sorted(set(s.name for s in info.subcommands.values()))
                return LocalCommandResult(
                    False,
                    f"Unknown {' '.join(cmd_path)} subcommand: {subcmd}\n"
                    f"Available: {', '.join(subnames)}",
                )

        remaining_parts = parts[part_idx:]

        # Get the handler
        if info.handler is None:
            return LocalCommandResult(False, f"No handler for: {' '.join(parts[:part_idx])}")

        handler = getattr(self, info.handler.__name__)

        # Parse and call handler based on ArgSpec
        try:
            if info.args:
                # Check for help request as first arg
                if remaining_parts and remaining_parts[0].lower() in ("help", "?"):
                    help_text = self._get_arg_help(info, cmd_path)
                    return LocalCommandResult(True, help_text)

                # Parse arguments (shared parser also rejects extra arguments)
                parsed_args, error = parse_args(remaining_parts, info.args, cmd_path)
                if error:
                    return LocalCommandResult(False, error.message)
                result = handler(*parsed_args)
            else:
                if remaining_parts:
                    if remaining_parts[0].lower() in ("help", "?"):
                        cmd_str = " ".join(cmd_path)
                        return LocalCommandResult(
                            True, f"{cmd_str}: {info.description or 'No help available.'}"
                        )
                    cmd_str = " ".join(cmd_path)
                    return LocalCommandResult(
                        False,
                        f"Unexpected argument(s): {' '.join(remaining_parts)}\nUsage: {cmd_str}",
                    )
                result = handler()
        except Exception as e:
            # The transport already labels failures ("ERROR: ..."), so an
            # inner "Error: " prefix only doubles it up (T2).
            return LocalCommandResult(False, str(e))

        # Check for exit marker
        if result.message == "__EXIT_CTL__":
            return LocalCommandResult(True, "", exit_ctl=True)

        return LocalCommandResult(result.success, result.message)


if PROMPT_TOOLKIT_AVAILABLE:
    from prompt_toolkit.patch_stdout import patch_stdout


def send_command(
    host: str,
    port: int,
    command: str,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Send a command to the simulator control port (one-shot mode).

    ``timeout`` bounds a *gap* in daemon traffic, not the total wait: each
    received chunk restarts it, so a chatty command never times out. For
    ``run <script> wait`` there is no deadline at all - the script's
    duration is unbounded and arbitrarily quiet, and the live connection is
    the liveness signal. A wait-run also streams the daemon's ``LOG:`` lines
    to **stderr** as they arrive, so a CI job sees progress while the script
    runs and the assertion that failed when it does (M3); stdout stays
    clean for the single result line.

    Args:
        host: Simulator host address
        port: Control port number
        command: Command to send
        timeout: Seconds of silence tolerated while waiting for a response

    Returns:
        Tuple of (success, response_message)
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall(f"{command}\n".encode())
            stream_logs = is_wait_run(command)
            if stream_logs:
                sock.settimeout(None)

            # Read response - only complete (newline-terminated) lines are
            # parsed so a response spanning TCP segments is never truncated
            buffer = ""
            while True:
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    return False, (
                        f"Response timeout after {timeout}s waiting for {host}:{port} "
                        "(the command may still be running; raise --timeout)"
                    )
                if not chunk:
                    break
                buffer += chunk.decode(errors="replace")
                # Keep any trailing partial line in the buffer
                *complete_lines, buffer = buffer.split("\n")
                for line in complete_lines:
                    line = line.strip()
                    if line.startswith("OK:"):
                        msg = sanitize_text(unescape_message(line[4:]))
                        return True, f"OK: {msg}"
                    elif line.startswith("ERROR:"):
                        msg = sanitize_text(unescape_message(line[7:]))
                        return False, f"ERROR: {msg}"
                    elif stream_logs and line.startswith("LOG:"):
                        print(
                            sanitize_text(unescape_message(line[5:])),
                            file=sys.stderr,
                            flush=True,
                        )
                    # Other LOG:/STATUS: lines are not command responses - skip

            # Connection closed without a response line
            leftover = sanitize_text(buffer.strip())
            if leftover:
                return False, leftover
            return False, f"Connection closed without response from {host}:{port}"

    except ConnectionRefusedError:
        return False, f"Connection refused to {host}:{port}"
    except TimeoutError:
        return False, f"Connection timed out to {host}:{port}"
    except Exception as e:
        return False, f"Error: {e}"


def check_connection(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    """Check if the simulator is listening.

    Returns:
        Tuple of (connected, error_message)
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            return True, ""
    except ConnectionRefusedError:
        return False, f"Connection refused - simulator not running on {host}:{port}"
    except TimeoutError:
        return False, f"Connection timed out to {host}:{port}"
    except Exception as e:
        return False, f"Connection error: {e}"


def _enable_line_buffering(stream: object) -> None:
    """Make ``stream`` flush every line, even when it is not a terminal.

    A no-op on a TTY (already line-buffered) and on any stream that does
    not support reconfiguration - a test double, or stdout already
    redirected to something exotic.
    """
    if getattr(stream, "isatty", None) is not None and stream.isatty():  # type: ignore[attr-defined]
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)


def _basic_readline(prompt_text: str) -> "asyncio.Future[str | None]":
    """Read one line from stdin without blocking the event loop.

    Uses add_reader (not a thread) so a daemon shutdown can end the
    session immediately instead of waiting for the user to press Enter.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str | None] = loop.create_future()
    fd = sys.stdin.fileno()

    def on_readable():
        loop.remove_reader(fd)
        if fut.cancelled():
            return
        line = sys.stdin.readline()
        fut.set_result(line if line else None)

    def cleanup(_fut):
        try:
            loop.remove_reader(fd)
        except Exception:
            # Defensive. Linux selectors swallow errors for dead fds, so no
            # *real* selector can drive this clause - that part of the
            # exclusion rationale this line used to carry was true. What
            # was wrong was concluding it could not be tested:
            # `loop.remove_reader` is a stdlib API a test can replace, and
            # the contract this clause exists for (the error must not reach
            # the loop's exception handler) is now pinned by a seam test
            # rather than hidden from the gate (round-7 test-fanatic M4).
            #
            # Do not write the exclusion phrase itself in prose here: it is
            # matched by `re.search` against the whole source line, so a
            # comment mentioning it silently excludes that line - the same
            # shape as the bare `...` pattern round 6 removed.
            pass

    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    loop.add_reader(fd, on_readable)
    fut.add_done_callback(cleanup)
    return fut


async def interactive_mode_async(
    host: str, port: int, door_port: int, timeout: float, history_file: str | None
):
    """Run in interactive mode using asyncio with log streaming."""
    # stdout is block-buffered whenever it is not a terminal, so `ctl -i >
    # session.log`, `| tee`, `| grep`, a container capturing stdout or a
    # supervisor saw nothing at all while a command was in flight - the
    # streamed LOG: lines all landed at once at the next prompt, making
    # "still running" and "hung" indistinguishable (M3). prompt_toolkit is
    # not driving that case anyway.
    _enable_line_buffering(sys.stdout)

    # Initialize timezone cache for completion
    await async_init_timezone_cache()

    # Check connection first
    connected, error = check_connection(host, port, timeout)
    if not connected:
        print(f"Error: {error}")
        sys.exit(1)

    print(f"Connected to simulator control port at {host}:{port}")
    print("Type 'help' for commands, 'exit' to quit, 'shutdown' to stop daemon")
    print()

    # Connect with asyncio for persistent connection
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception as e:
        print(f"Error connecting: {e}")
        sys.exit(1)

    stop_event = asyncio.Event()

    # Track client connection status for prompt coloring
    has_clients = [False]

    # Queue for command responses (OK:/ERROR: messages)
    response_queue: asyncio.Queue[tuple[bool, str]] = asyncio.Queue()

    # Set up interactive session using shared InteractiveSession class
    interactive = InteractiveSession.create(
        host=host,
        port=door_port,
        history_file=history_file,
        is_connected=lambda: has_clients[0],
    )

    # Create local command handler with history from interactive session
    local_handler = LocalCommandHandler(history=interactive.history)

    # Set on every line received from the daemon. A command still waiting
    # for its OK:/ERROR: treats any traffic as proof of life and restarts
    # its silence deadline (streaming LOG: output must not time out).
    activity = asyncio.Event()

    async def socket_reader():
        """Single task that reads all messages from the socket.

        Routes messages to appropriate handlers:
        - STATUS: lines update the door-client count (prompt coloring)
        - LOG: messages are printed immediately (sanitized)
        - OK:/ERROR: messages go to the response queue
        """
        try:
            # Exits via break (disconnect or cancellation) or exception; the
            # main loop only sets stop_event immediately before cancelling
            # this task, so a stop_event-based loop condition is unreachable.
            while True:
                try:
                    line = await reader.readline()
                    if not line:
                        # Connection closed
                        print("\n>>> Simulator disconnected.")
                        stop_event.set()
                        break
                    decoded = line.decode(errors="replace").strip()
                    activity.set()
                    if decoded.startswith("STATUS:"):
                        # Structured door-client status from the daemon
                        payload = decoded[7:].strip()
                        if payload.startswith("clients="):
                            try:
                                count = int(payload[8:])
                            except ValueError:
                                continue
                            new_status = count > 0
                            if new_status != has_clients[0]:
                                has_clients[0] = new_status
                                interactive.invalidate()
                    elif decoded.startswith("LOG:"):
                        # Print log message immediately (never raw - network
                        # data must not inject terminal escapes)
                        print(sanitize_text(unescape_message(decoded[5:])))
                    elif decoded.startswith("OK:"):
                        msg = sanitize_text(unescape_message(decoded[4:]))
                        # Route to response queue
                        await response_queue.put((True, msg))
                    elif decoded.startswith("ERROR:"):
                        msg = sanitize_text(unescape_message(decoded[7:]))
                        await response_queue.put((False, msg))
                except asyncio.CancelledError:
                    break
        except Exception as e:
            if not stop_event.is_set():
                print(f"\n>>> Connection error: {e}")
                stop_event.set()

    async def await_response(silence_timeout: float | None) -> tuple[bool, str]:
        """Wait for the next OK:/ERROR:, bounded by silence, not total time.

        ``silence_timeout`` bounds a *gap* in daemon traffic: any line
        received (typically streaming LOG: output) restarts it. None waits
        indefinitely - used for ``run <script> wait``, whose duration is the
        script's, not the command's. Either way a dropped connection
        (stop_event) ends the wait.
        """
        response_task = asyncio.ensure_future(response_queue.get())
        stop_task = asyncio.ensure_future(stop_event.wait())
        try:
            while True:
                activity.clear()
                activity_task = asyncio.ensure_future(activity.wait())
                try:
                    done, _pending = await asyncio.wait(
                        [response_task, stop_task, activity_task],
                        timeout=silence_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    activity_task.cancel()
                if response_task in done:
                    return response_task.result()
                if stop_task in done:
                    return False, "Simulator disconnected before responding"
                if not done:
                    return False, (
                        f"Response timeout after {silence_timeout}s of silence "
                        "(the command may still be running; raise --timeout)"
                    )
                # Only activity fired: the daemon is alive, keep waiting.
        finally:
            for task in (response_task, stop_task):
                task.cancel()

    async def send_command_async(cmd: str) -> tuple[bool, str]:
        """Send a command and wait for response from the queue."""
        try:
            # Clear any stale responses from the queue
            while True:
                try:
                    response_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            writer.write(f"{cmd}\n".encode())
            await writer.drain()

            # A synchronous script run takes as long as the script does.
            return await await_response(None if is_wait_run(cmd) else timeout)
        except Exception as e:
            return False, f"Error: {e}"

    # Start the socket reader task
    reader_task = asyncio.create_task(socket_reader())

    # Use patch_stdout only when prompt_toolkit is actually driving the prompt
    stdout_ctx = None
    if interactive.available:
        stdout_ctx = patch_stdout()
        stdout_ctx.__enter__()

    # Initial client status arrives as a STATUS: line from the daemon
    # (sent on connect), handled by socket_reader.

    async def wait_for_stop():
        """Wait for stop event to be set."""
        await stop_event.wait()

    prompt_text = f"{host}:{door_port}> "  # Fallback for non-prompt_toolkit

    try:
        while not stop_event.is_set():
            try:
                if interactive.available:
                    # Race between prompt and disconnect detection
                    prompt_task = asyncio.create_task(interactive.prompt_async())
                else:
                    # Basic fallback - also raced against disconnect so the
                    # session ends as soon as the daemon goes away
                    prompt_task = asyncio.ensure_future(_basic_readline(prompt_text))
                stop_task = asyncio.create_task(wait_for_stop())

                done, pending = await asyncio.wait(
                    [prompt_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Cancel pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Check if we stopped due to disconnect
                if stop_task in done:
                    break

                # Get the prompt result
                line = prompt_task.result()
                if line is None:
                    # EOF
                    break
                line = line.strip()
            except EOFError:
                # Both prompt paths normally signal EOF by returning None, but
                # a raising prompt implementation must not escape the loop.
                break
            except KeyboardInterrupt:
                continue
            except asyncio.CancelledError:
                break

            if not line:
                continue

            # Handle history recall commands (!!, !n, !-n)
            resolved_line, was_history_recall, recall_error = interactive.resolve_history_recall(
                line
            )
            if recall_error:
                print(f">>> {recall_error}")
                continue

            input_line = InputLine(
                original=line,
                resolved=resolved_line,
                was_history_recall=was_history_recall,
            )

            if was_history_recall:
                print(interactive.format_recall(input_line))

            # Check if this is a local command (local_only=True in registry)
            if local_handler.is_local_command(input_line.resolved):
                result = local_handler.execute(input_line.resolved)
                interactive.handle_result(input_line, result.success)
                if result.exit_ctl:
                    break
                if result.message:
                    print(render_result(result.message))
                continue

            # Send command to daemon
            success, response = await send_command_async(input_line.resolved)
            interactive.handle_result(input_line, success)

            if response:
                print(render_result(response))

    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        stop_event.set()
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:  # pragma: no cover (defensive: socket_reader swallows its own cancellation; only an outer cancel landing exactly on this await would raise)
            pass
        if stdout_ctx:
            stdout_ctx.__exit__(None, None, None)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def interactive_mode(
    host: str,
    port: int,
    door_port: int,
    timeout: float,
    history_file: str | None = None,
):
    """Run in interactive mode, sending commands from stdin."""
    # Use async mode for log streaming support
    asyncio.run(interactive_mode_async(host, port, door_port, timeout, history_file))


def main():
    """CLI entry point for simulator control."""
    parser = argparse.ArgumentParser(
        description="Control a running Power Pet Door simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s status                  # Get simulator status
  %(prog)s inside                  # Trigger inside sensor
  %(prog)s run SCRIPT wait         # Run a script; exit code reflects PASSED/FAILED
  %(prog)s stop                    # Stop the running script (not the daemon)
  %(prog)s -i                      # Interactive mode
  %(prog)s shutdown                # Stop the daemon

Plain 'run SCRIPT' exits 0 as soon as it is queued; the queued script's own
result never reaches the exit code (only the 'wait' form reports that). A
script that fails to load is still an error, and exits 1. Use the 'help'
command to see available simulator commands.
""",
    )
    parser.add_argument(
        "--host", "-H", default="127.0.0.1", help="Simulator host (default: 127.0.0.1)"
    )
    parser.add_argument("--port", "-p", type=int, default=3001, help="Control port (default: 3001)")
    parser.add_argument(
        "--door-port",
        "-d",
        type=int,
        default=None,
        help="Door simulator port for prompt display (default: control_port - 1)",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="Run in interactive mode")
    parser.add_argument(
        "--timeout",
        "-t",
        type=float,
        default=5.0,
        help="Seconds of daemon SILENCE tolerated while waiting for a response "
        "(default: 5). Any received line restarts it, so a chatty command never "
        "trips it; 'run <script> wait' ignores it entirely and waits as long as "
        "the script takes.",
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
        "command", nargs="*", help="Command to send (or use -i for interactive mode)"
    )

    args = parser.parse_args()

    # `--timeout 0` reads to a user as "no timeout"; it actually put the
    # socket in non-blocking mode and yielded `Error: [Errno 115] Operation
    # now in progress`, and `-t -1` leaked `settimeout`'s own ValueError
    # text - an errno no operator should have to decode from a CLI flag
    # (round-9 frontend T1). `run <script> wait` is the documented spelling
    # for "wait as long as it takes", so a sentinel here would be a second
    # one.
    if args.timeout <= 0:
        parser.error(
            f"--timeout {args.timeout:g}: must be greater than 0 "
            "(use 'run <script> wait' to wait as long as a script takes)"
        )

    # The daemon refuses script paths over the control channel, so this
    # process must not complete them: offering `my_custom.yaml` steers the
    # user to a command that always fails, while the name that works
    # (`my_custom`) is one completion cannot offer (M1).
    set_script_paths_allowed(False)

    # Determine door port for prompt display
    door_port = args.door_port if args.door_port is not None else args.port - 1

    # Get history file (only used when prompt_toolkit drives the session)
    history_file = args.history if PROMPT_TOOLKIT_AVAILABLE else None

    try:
        if args.interactive:
            interactive_mode(args.host, args.port, door_port, args.timeout, history_file)
        elif args.command:
            command = " ".join(args.command)
            if not command.strip():
                # The daemon skips blank lines by design, so no answer can
                # ever come: `ppd-simulator-ctl ""` sat out the whole
                # --timeout and then advised raising it, which is wrong in
                # both halves - the command was never a command, and raising
                # the timeout only makes the hang longer. A shell wrapper
                # expanding an unset variable lands here immediately
                # (round-9 frontend L4).
                parser.error("empty command")
            success, response = send_command(args.host, args.port, command, args.timeout)
            print(response)
            sys.exit(0 if success else 1)
        else:
            parser.print_help()
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        # Interrupted runs must not report success (128 + SIGINT)
        sys.exit(130)


if __name__ == "__main__":
    main()
