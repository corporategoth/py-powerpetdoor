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
from typing import Optional

# Import shared prompt_toolkit components
from .prompt_common import (
    PROMPT_TOOLKIT_AVAILABLE,
    CTL_HISTORY_FILE as HISTORY_FILE,
    InteractiveSession,
    InputLine,
)

# Import command infrastructure for local command handling
from .commands.base import (
    CommandResult,
    get_command_registry,
    parse_arg,
)
from .commands.history import History
from .commands.info import InfoCommandsMixin
from .commands.control import ControlCommandsMixin


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
        self.simulator = None  # Not needed for local commands
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
        - It's help/? (ctl generates its own help to include local commands)
        - It contains a help request (e.g., "sched help", "sched add ?")
        """
        parts = line.split()
        if not parts:
            return False

        cmd = parts[0].lower()

        # Help is always handled locally to show ctl-specific help
        if cmd in ("help", "?"):
            return True

        # Check if any part is a help request (subcommand help)
        for part in parts[1:]:
            if part.lower() in ("help", "?"):
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

        info = registry[cmd]
        cmd_path = [info.name]

        # Traverse subcommand hierarchy
        part_idx = 1
        while part_idx < len(parts) and info.subcommands:
            subcmd = parts[part_idx].lower()

            # Handle implicit help/? subcommand
            if subcmd in ("help", "?"):
                if info.args:
                    help_text = self._get_arg_help(info, cmd_path)
                elif info.subcommands:
                    help_text = self._get_subcommand_help(info, cmd_path)
                else:
                    help_text = f"{' '.join(cmd_path)}: {info.description or 'No help available.'}"
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

                # Parse arguments
                parsed_args, error = self._parse_args(remaining_parts, info.args, cmd_path)
                if error:
                    return LocalCommandResult(False, error.message)
                result = handler(*parsed_args)
            else:
                result = handler()
        except Exception as e:
            return LocalCommandResult(False, f"Error: {e}")

        # Check for exit marker
        if result.message == "__EXIT_CTL__":
            return LocalCommandResult(True, "", exit_ctl=True)

        return LocalCommandResult(result.success, result.message)

    def _parse_args(
        self,
        parts: list[str],
        arg_specs: list,
        cmd_path: list[str],
    ) -> tuple[list, CommandResult | None]:
        """Parse argument parts according to ArgSpec definitions."""
        parsed = []
        cmd_str = " ".join(cmd_path)
        usage = " ".join(spec.generate_usage() for spec in arg_specs)

        for i, spec in enumerate(arg_specs):
            if i < len(parts):
                value, error = parse_arg(parts[i], spec)
                if error:
                    return [], CommandResult(False, f"{error}\nUsage: {cmd_str} {usage}")
                parsed.append(value)
            elif spec.required:
                return [], CommandResult(
                    False, f"Missing required argument: {spec.name}\nUsage: {cmd_str} {usage}"
                )
            else:
                parsed.append(spec.default)

        return parsed, None


if PROMPT_TOOLKIT_AVAILABLE:
    from prompt_toolkit.patch_stdout import patch_stdout


def send_command(
    host: str,
    port: int,
    command: str,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Send a command to the simulator control port (one-shot mode).

    Args:
        host: Simulator host address
        port: Control port number
        command: Command to send
        timeout: Socket timeout in seconds

    Returns:
        Tuple of (success, response_message)
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall(f"{command}\n".encode())

            # Read response
            response = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                    # Check if we got a complete response (OK: or ERROR:)
                    decoded = response.decode()
                    # Look for complete response line
                    for line in decoded.split("\n"):
                        if line.startswith("OK:") or line.startswith("ERROR:"):
                            # Unescape newlines from protocol
                            response_str = line.strip()
                            success = response_str.startswith("OK:")
                            # Unescape the message portion
                            if success:
                                msg = response_str[4:].replace('\\n', '\n').replace('\\\\', '\\')
                                return success, f"OK: {msg}"
                            else:
                                msg = response_str[7:].replace('\\n', '\n').replace('\\\\', '\\')
                                return success, f"ERROR: {msg}"
                except socket.timeout:
                    break

            response_str = response.decode().strip()
            success = response_str.startswith("OK:")
            return success, response_str

    except ConnectionRefusedError:
        return False, f"Connection refused to {host}:{port}"
    except socket.timeout:
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
    except socket.timeout:
        return False, f"Connection timed out to {host}:{port}"
    except Exception as e:
        return False, f"Connection error: {e}"


async def interactive_mode_async(
    host: str, port: int, door_port: int, timeout: float, history_file: Optional[str]
):
    """Run in interactive mode using asyncio with log streaming."""
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

    async def socket_reader():
        """Single task that reads all messages from the socket.

        Routes messages to appropriate handlers:
        - LOG: messages are printed immediately
        - OK:/ERROR: messages go to the response queue
        """
        try:
            while not stop_event.is_set():
                try:
                    line = await reader.readline()
                    if not line:
                        # Connection closed
                        print("\n>>> Simulator disconnected.")
                        stop_event.set()
                        break
                    decoded = line.decode().strip()
                    if decoded.startswith("LOG:"):
                        # Print log message immediately
                        print(decoded[5:])
                        # Update client status from log messages
                        if "Client connected" in decoded:
                            has_clients[0] = True
                        elif "Client disconnected" in decoded or "connection closed" in decoded.lower():
                            # Check if there might still be other clients
                            # For simplicity, assume disconnected means no clients
                            # (a proper solution would track count)
                            pass
                    elif decoded.startswith("OK:"):
                        # Unescape newlines from protocol
                        msg = decoded[4:].replace('\\n', '\n').replace('\\\\', '\\')
                        # Route to response queue
                        await response_queue.put((True, msg))
                        # Update client count from status responses
                        if "Clients:" in decoded:
                            if "Clients: none" in decoded or "Clients: 0" in decoded:
                                has_clients[0] = False
                            else:
                                has_clients[0] = True
                    elif decoded.startswith("ERROR:"):
                        # Unescape newlines from protocol
                        msg = decoded[7:].replace('\\n', '\n').replace('\\\\', '\\')
                        await response_queue.put((False, msg))
                except asyncio.CancelledError:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not stop_event.is_set():
                print(f"\n>>> Connection error: {e}")
                stop_event.set()

    async def send_command_async(cmd: str) -> tuple[bool, str]:
        """Send a command and wait for response from the queue."""
        try:
            # Clear any stale responses from the queue
            while not response_queue.empty():
                try:
                    response_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            writer.write(f"{cmd}\n".encode())
            await writer.drain()

            # Wait for response from the reader task
            try:
                success, response = await asyncio.wait_for(
                    response_queue.get(), timeout=timeout
                )
                return success, response
            except asyncio.TimeoutError:
                return False, "Response timeout"
        except Exception as e:
            return False, f"Error: {e}"

    # Start the socket reader task
    reader_task = asyncio.create_task(socket_reader())

    # Use patch_stdout if available for proper prompt handling with async output
    stdout_ctx = None
    if PROMPT_TOOLKIT_AVAILABLE:
        stdout_ctx = patch_stdout()
        stdout_ctx.__enter__()

    # Get initial client status
    try:
        success, response = await send_command_async("status")
        if success and "Clients:" in response:
            # Check specifically for "Clients: none" or "Clients: 0"
            # (not just "none" anywhere, which would match "Notifications: none")
            if "Clients: none" in response or "Clients: 0" in response:
                has_clients[0] = False
            else:
                has_clients[0] = True
    except Exception:
        pass

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
                else:
                    # Basic fallback
                    line = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input(prompt_text)
                    )
                    line = line.strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                continue
            except asyncio.CancelledError:
                break

            if not line:
                continue

            # Handle history recall commands (!!, !n, !-n)
            resolved_line, was_history_recall, error = interactive.resolve_history_recall(line)
            if error:
                print(f">>> {error}")
                continue

            input_line = InputLine(
                original=line,
                resolved=resolved_line,
                was_history_recall=was_history_recall,
            )

            if was_history_recall:
                print(f">>> {input_line.original} -> {input_line.resolved}")

            # Check if this is a local command (local_only=True in registry)
            if local_handler.is_local_command(input_line.resolved):
                result = local_handler.execute(input_line.resolved)
                interactive.handle_result(input_line, result.success)
                if result.exit_ctl:
                    break
                if result.message:
                    print(f">>> {result.message}")
                continue

            # Send command to daemon
            success, response = await send_command_async(input_line.resolved)
            interactive.handle_result(input_line, success)

            if response:
                print(f">>> {response}")

    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        stop_event.set()
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
        if stdout_ctx:
            stdout_ctx.__exit__(None, None, None)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def interactive_mode_basic(host: str, port: int, door_port: int, timeout: float):
    """Run in interactive mode using basic input (fallback without asyncio)."""
    # Check connection first
    connected, error = check_connection(host, port, timeout)
    if not connected:
        print(f"Error: {error}")
        sys.exit(1)

    print(f"Connected to simulator control port at {host}:{port}")
    print("Type 'help' for commands, 'exit' to quit, 'shutdown' to stop daemon")
    print()

    # Create local command handler (no history in basic mode - no prompt_toolkit)
    local_handler = LocalCommandHandler(history=None)

    prompt_text = f"{host}:{door_port}> "
    try:
        while True:
            try:
                line = input(prompt_text).strip()
            except EOFError:
                break

            if not line:
                continue

            # Check if this is a local command (local_only=True in registry)
            if local_handler.is_local_command(line):
                result = local_handler.execute(line)
                if result.exit_ctl:
                    break
                if result.message:
                    print(f">>> {result.message}")
                continue

            success, response = send_command(host, port, line, timeout)

            # Check for disconnect
            if "Connection refused" in response or "Connection timed out" in response:
                print(f"\n>>> {response}")
                print("Simulator disconnected.")
                break

            # Strip OK:/ERROR: prefix for display
            if response.startswith("OK: "):
                msg = response[4:]
            elif response.startswith("ERROR: "):
                msg = response[7:]
            else:
                msg = response
            if msg:
                print(f">>> {msg}")

            # If we sent a shutdown command and it succeeded, exit
            if line.lower() == "shutdown" and success:
                break

    except KeyboardInterrupt:
        print("\nExiting.")


def interactive_mode(
    host: str,
    port: int,
    door_port: int,
    timeout: float,
    history_file: Optional[str] = None,
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
  %(prog)s -i                      # Interactive mode
  %(prog)s shutdown                # Stop the daemon

Use the 'help' command to see available simulator commands.
""",
    )
    parser.add_argument(
        "--host", "-H", default="127.0.0.1", help="Simulator host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", "-p", type=int, default=3001, help="Control port (default: 3001)"
    )
    parser.add_argument(
        "--door-port",
        "-d",
        type=int,
        default=None,
        help="Door simulator port for prompt display (default: control_port - 1)",
    )
    parser.add_argument(
        "--interactive", "-i", action="store_true", help="Run in interactive mode"
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=float,
        default=5.0,
        help="Command timeout in seconds (default: 5)",
    )
    if PROMPT_TOOLKIT_AVAILABLE:
        parser.add_argument(
            "--history",
            metavar="FILE",
            default=str(HISTORY_FILE),
            help=f"History file path, or 'none' to disable (default: {HISTORY_FILE})",
        )
    parser.add_argument(
        "command", nargs="*", help="Command to send (or use -i for interactive mode)"
    )

    args = parser.parse_args()

    # Determine door port for prompt display
    door_port = args.door_port if args.door_port is not None else args.port - 1

    # Get history file (None if prompt_toolkit not available)
    history_file = getattr(args, "history", None)

    if args.interactive:
        interactive_mode(args.host, args.port, door_port, args.timeout, history_file)
    elif args.command:
        command = " ".join(args.command)
        success, response = send_command(args.host, args.port, command, args.timeout)
        print(response)
        sys.exit(0 if success else 1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
