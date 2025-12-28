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
from pathlib import Path
from typing import Optional

from .commands import CommandHandler
from .server import DoorSimulator
from ..tz_utils import async_init_timezone_cache

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

    Returns:
        Script result (True if all passed, False if any failed, None if no scripts)
    """
    import sys

    from .scripting import ScriptRunner

    # Initialize timezone cache for IANA to POSIX conversion
    await async_init_timezone_cache()

    # Start the simulator
    simulator = DoorSimulator(host=host, port=port)
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
    if control_port:
        async def handle_control_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            """Handle a control connection."""
            addr = writer.get_extra_info("peername")
            logger.info(f"Control connection from {addr}")
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    cmd = line.decode().strip()
                    if not cmd:
                        continue

                    result = await cmd_handler.execute(cmd)
                    if result.success:
                        writer.write(f"OK: {result.message}\n".encode())
                    else:
                        writer.write(f"ERROR: {result.message}\n".encode())
                    await writer.drain()

                    # Check if we should exit
                    if stop_event.is_set():
                        break
            except Exception as e:
                logger.error(f"Control client error: {e}")
            finally:
                writer.close()
                await writer.wait_closed()
                logger.info(f"Control connection closed from {addr}")

        control_server = await asyncio.start_server(
            handle_control_client, host, control_port
        )
        logger.info(f"Control server listening on {host}:{control_port}")

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
    if interactive:
        try:
            if sys.stdin and sys.stdin.fileno() >= 0:
                import os
                os.fstat(sys.stdin.fileno())
                stdin_available = True
        except (OSError, ValueError, AttributeError):
            pass

        if stdin_available:
            # Create prompt with host:port format
            prompt = InteractivePrompt(f"{host}:{port}> ")

            def handle_input():
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
                    print(f">>> {result.message}")
                    # Remove stdin reader immediately to avoid blocking shutdown
                    loop.remove_reader(sys.stdin.fileno())
                else:
                    prompt.output(f">>> {result.message}")

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
        if prompt:
            prompt.disable()
        if interactive and stdin_available:
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

    args = parser.parse_args()

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
        ))

        # Exit with appropriate code for CI/CD
        if args.oneshot and result is not None:
            sys.exit(0 if result else 1)

    except KeyboardInterrupt:
        print("\nSimulator stopped.")


if __name__ == "__main__":
    main()
