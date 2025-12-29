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
from typing import TYPE_CHECKING, Optional

from .commands import CommandHandler
from .server import DoorSimulator
from ..tz_utils import async_init_timezone_cache

# Import shared prompt_toolkit components
from .prompt_common import (
    PROMPT_TOOLKIT_AVAILABLE,
    CLI_HISTORY_FILE as HISTORY_FILE,
    InteractiveSession,
)

if PROMPT_TOOLKIT_AVAILABLE:
    from prompt_toolkit.patch_stdout import patch_stdout

if TYPE_CHECKING:
    from .scripting import ScriptRunner

logger = logging.getLogger(__name__)


class InteractivePrompt:
    """Manages interactive prompt with proper output handling.

    When enabled, this class handles displaying a prompt and ensuring
    that async output (like log messages) properly clears the line
    before printing and restores the prompt afterward.
    """

    def __init__(self, prompt: str = "$ "):
        self.prompt = prompt
        self._enabled = False
        self._handler: Optional[logging.Handler] = None
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
        self._handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        root_logger.addHandler(self._handler)
        self.show()

    def disable(self):
        """Disable the prompt and remove the logging handler."""
        if not self._enabled:
            return
        self._enabled = False

        root_logger = logging.getLogger()
        if self._handler:
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


async def run_simulator(
    host: str = "0.0.0.0",
    port: int = 3000,
    scripts: Optional[list[str]] = None,
    loop_scripts: bool = False,
    script_delay: float = 0,
    oneshot: bool = False,
    daemon: bool = False,
    run_for: Optional[float] = None,
    wait_for_client: bool = False,
    control_port: Optional[int] = None,
    history_file: Optional[str] = None,
    firmware: Optional[tuple[int, int, int]] = None,
    hardware: Optional[tuple[str, str]] = None,
):
    """Run the Power Pet Door simulator.

    Args:
        host: Address to bind the server
        port: Port to listen on for door protocol
        scripts: List of scripts to run (file paths or built-in names, auto-detected).
                 Implies non-interactive mode.
        loop_scripts: If True, run scripts continuously in a loop
        script_delay: Delay in seconds between script runs
        oneshot: If True, exit after scripts complete (even if run_for is set)
        daemon: If True, run without interactive input and no scripts.
        run_for: Maximum run time in seconds (oneshot can exit earlier)
        wait_for_client: If True, delay script start until a client connects
        control_port: Port for control commands (default: port + 1 in daemon/script mode)
        firmware: Firmware version as (major, minor, patch) tuple
        hardware: Hardware version as (ver, rev) tuple

    Returns:
        Script result (True if all passed, False if any failed, None if no scripts)
    """
    import sys

    from .scripting import ScriptRunner
    from .state import DoorSimulatorState

    # Initialize timezone cache for IANA to POSIX conversion
    await async_init_timezone_cache()

    # Create state with optional firmware/hardware version
    state = None
    if firmware or hardware:
        kwargs = {}
        if firmware:
            kwargs["fw_major"] = firmware[0]
            kwargs["fw_minor"] = firmware[1]
            kwargs["fw_patch"] = firmware[2]
        if hardware:
            kwargs["hw_ver"] = hardware[0]
            kwargs["hw_rev"] = hardware[1]
        state = DoorSimulatorState(**kwargs)

    # Holder for interactive session (set later if in interactive mode)
    # Used by callbacks to invalidate prompt on connect/disconnect
    session_holder: list[Optional[InteractiveSession]] = [None]

    def on_client_connect():
        """Called when a client connects - invalidate prompt to update color."""
        if session_holder[0]:
            session_holder[0].invalidate()

    def on_client_disconnect():
        """Called when a client disconnects - invalidate prompt to update color."""
        if session_holder[0]:
            session_holder[0].invalidate()

    # Start the simulator
    simulator = DoorSimulator(
        host=host,
        port=port,
        state=state,
        on_connect=on_client_connect,
        on_disconnect=on_client_disconnect,
    )
    await simulator.start()

    script_runner = ScriptRunner(simulator)

    # Set up control structures
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    script_result = [None]  # Use list to allow mutation in nested function
    script_queue: asyncio.Queue[str] = asyncio.Queue()

    # Create command handler
    cmd_handler = CommandHandler(
        simulator=simulator,
        script_runner=script_runner,
        stop_callback=stop_event.set,
        script_queue=script_queue,
    )

    # Determine mode
    interactive = not scripts and not daemon

    # Set interactive and CLI mode before printing help so interactive-only commands appear
    # and exit/q/quit are shown as aliases for shutdown
    if interactive:
        cmd_handler.set_interactive_mode(True)
        cmd_handler.set_cli_mode(True)

    # Print startup info
    print(f"Simulator started on port {port}")
    if control_port:
        print(f"Control port: {control_port}")
    if interactive:
        print("=" * 65)
        print(cmd_handler.get_help())
        print("=" * 65)
    print()

    # Start control server if configured
    control_server = None
    control_clients: set[asyncio.StreamWriter] = set()
    control_log_handler: logging.Handler | None = None

    if control_port:

        class ControlClientLogHandler(logging.Handler):
            """Logging handler that broadcasts to control clients."""

            def emit(self, record):
                try:
                    msg = self.format(record)
                    # Send log messages with LOG: prefix
                    data = f"LOG: {msg}\n".encode()
                    # Broadcast to all connected control clients
                    for writer in list(control_clients):
                        try:
                            writer.write(data)
                            # Don't await drain here - it would block
                            # The message will be sent eventually
                        except Exception:
                            # Client disconnected, will be cleaned up later
                            pass
                except Exception:
                    self.handleError(record)

        async def handle_control_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            """Handle a control connection."""
            addr = writer.get_extra_info("peername")
            logger.info(f"Control connection from {addr}")
            control_clients.add(writer)
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    cmd = line.decode().strip()
                    if not cmd:
                        continue

                    result = await cmd_handler.execute(cmd)
                    # Escape newlines for protocol (ctl will unescape)
                    escaped_msg = result.message.replace('\\', '\\\\').replace('\n', '\\n')
                    if result.success:
                        writer.write(f"OK: {escaped_msg}\n".encode())
                    else:
                        writer.write(f"ERROR: {escaped_msg}\n".encode())
                    await writer.drain()

                    # Check if we should exit
                    if stop_event.is_set():
                        break
            except Exception as e:
                logger.error(f"Control client error: {e}")
            finally:
                control_clients.discard(writer)
                writer.close()
                await writer.wait_closed()
                logger.info(f"Control connection closed from {addr}")

        control_server = await asyncio.start_server(
            handle_control_client, host, control_port
        )
        logger.info(f"Control server listening on {host}:{control_port}")

        # Install log handler to broadcast to control clients
        control_log_handler = ControlClientLogHandler()
        control_log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logging.getLogger().addHandler(control_log_handler)

    # Process queued scripts in background
    async def process_script_queue():
        while not stop_event.is_set():
            try:
                try:
                    script_ref = await asyncio.wait_for(
                        script_queue.get(), timeout=0.5
                    )
                except asyncio.TimeoutError:
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

    queue_task = asyncio.create_task(process_script_queue())

    # Run startup scripts if specified
    if scripts:
        async def run_startup_scripts():
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

        asyncio.create_task(run_startup_scripts())

    # Set up interactive input if applicable
    stdin_available = False
    prompt: Optional[InteractivePrompt] = None
    input_task: Optional[asyncio.Task] = None
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
            if PROMPT_TOOLKIT_AVAILABLE:
                # Use InteractiveSession.create for standard prompt setup
                history_path = history_file if history_file else str(HISTORY_FILE)
                interactive = InteractiveSession.create(
                    host=host,
                    port=port,
                    history_file=history_path,
                    is_connected=lambda: bool(simulator.protocols),
                )

                # Store in holder so connect/disconnect callbacks can invalidate
                session_holder[0] = interactive

                # Register history with command handler (for history command)
                if interactive.history:
                    cmd_handler.set_history(interactive.history)

                async def interactive_input_loop():
                    """Async input loop using prompt_toolkit."""
                    try:
                        async for input_line in interactive.input_loop(
                            stop_check=stop_event.is_set
                        ):
                            if input_line.was_history_recall:
                                print(f">>> {input_line.original} -> {input_line.resolved}")

                            result = await cmd_handler.execute(input_line.resolved)
                            interactive.handle_result(input_line, result.success)

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
                    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
                )
                root_logger.addHandler(new_handler)

                input_task = asyncio.create_task(interactive_input_loop())
            else:
                # Fallback to basic input with InteractivePrompt
                prompt_text = f"{host}:{port}> "
                prompt = InteractivePrompt(prompt_text)
                reader_removed = [False]  # Use list to allow mutation in nested function

                def handle_input():
                    # Don't read if we're shutting down (prevents blocking)
                    if stop_event.is_set() or reader_removed[0]:
                        return
                    try:
                        line = sys.stdin.readline().strip()
                        if line:
                            asyncio.create_task(process_interactive_command(line))
                        else:
                            # Empty line (just Enter), re-show prompt
                            prompt.show()
                    except Exception as e:
                        prompt.output(f"Error: {e}")

                async def process_interactive_command(line: str):
                    result = await cmd_handler.execute(line)
                    # Don't show prompt again after shutdown command
                    if stop_event.is_set():
                        prompt.clear_line()
                        if result.message:
                            print(f">>> {result.message}")
                        # Remove stdin reader immediately to avoid blocking shutdown
                        reader_removed[0] = True
                        try:
                            loop.remove_reader(sys.stdin.fileno())
                        except Exception:
                            pass
                    elif result.message:
                        prompt.output(f">>> {result.message}")
                    else:
                        prompt.show()

                prompt.enable()
                loop.add_reader(sys.stdin.fileno(), handle_input)
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
        if interactive and stdin_available and not PROMPT_TOOLKIT_AVAILABLE:
            try:
                loop.remove_reader(sys.stdin.fileno())
            except Exception:
                pass  # Already removed
        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass
        if control_server:
            control_server.close()
            await control_server.wait_closed()
        if control_log_handler:
            logging.getLogger().removeHandler(control_log_handler)
        await simulator.stop()

    return script_result[0]


def main():
    """CLI entry point for the simulator."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Power Pet Door Simulator - Fake door for testing"
    )
    parser.add_argument(
        "--host", "-H",
        default="0.0.0.0",
        help="Address to bind (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=3000,
        help="Port to listen on (default: 3000)"
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--script", "-s",
        action="append",
        dest="scripts",
        metavar="SCRIPT",
        help="Run a script (built-in name or file path, auto-detected). "
             "Can be specified multiple times to run scripts in sequence. "
             "Implies non-interactive mode."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run scripts continuously in a loop"
    )
    parser.add_argument(
        "--script-delay",
        type=float,
        default=0,
        metavar="SECONDS",
        help="Delay between scripts and loop iterations (default: 0)"
    )
    parser.add_argument(
        "--oneshot",
        action="store_true",
        help="Exit after scripts complete (useful for CI/CD). Takes precedence over --run-for."
    )
    parser.add_argument(
        "--wait-for-client", "-w",
        action="store_true",
        help="Wait for a client to connect before starting scripts. "
             "Scripts stop if client disconnects."
    )
    parser.add_argument(
        "--list-scripts", "-l",
        action="store_true",
        help="List available built-in scripts and exit"
    )
    parser.add_argument(
        "--daemon", "-D",
        nargs="?",
        type=int,
        const=-1,  # Sentinel: use default (port+1)
        default=None,
        metavar="CONTROL_PORT",
        help="Run in daemon mode (no interactive input, no scripts). "
             "Optionally specify control port (default: PORT+1). "
             "Mutually exclusive with --script."
    )
    parser.add_argument(
        "--run-for", "-r",
        type=float,
        metavar="SECONDS",
        help="Maximum run time in seconds (--oneshot can exit earlier)"
    )
    # Only add history argument if prompt_toolkit is available
    if PROMPT_TOOLKIT_AVAILABLE:
        parser.add_argument(
            "--history",
            metavar="FILE",
            default=str(HISTORY_FILE),
            help=f"History file path, or 'none' to disable (default: {HISTORY_FILE})"
        )
    parser.add_argument(
        "--firmware", "-f",
        metavar="VERSION",
        help="Firmware version to report (e.g., '1.2.3', default: 1.2.3)"
    )
    parser.add_argument(
        "--hardware",
        metavar="VERSION",
        help="Hardware version to report (e.g., '1.1' for 'ver 1 rev 1', default: 1.1)"
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
        result = asyncio.run(run_simulator(
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
            history_file=args.history,
            firmware=firmware,
            hardware=hardware,
        ))

        # Exit with appropriate code for CI/CD
        if args.oneshot and result is not None:
            sys.exit(0 if result else 1)

    except KeyboardInterrupt:
        print("\nSimulator stopped.")


if __name__ == "__main__":
    main()
