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
from pathlib import Path
from typing import TYPE_CHECKING

from ..i18n import t
from ..sanitize import sanitize_text
from ..tz_utils import async_init_timezone_cache
from .commands import CommandHandler
from .commands.scripts import QueuedScript, ScriptQueue
from .prompt_common import (
    CLI_HISTORY_FILE as HISTORY_FILE,
)

# Import shared prompt_toolkit components
from .prompt_common import (
    PROMPT_TOOLKIT_AVAILABLE,
    InteractiveSession,
    escape_message,
    render_result,
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


class SimulatorStartupError(OSError):
    """A bind or resolve failure while starting one of the two servers.

    The mirror-image failure on the client half of this product answers
    ``Connection refused to 127.0.0.1:39999``; the server half answered a
    30-line traceback through asyncio internals in which the one useful line
    was last, printed absolute paths from the machine that built the venv,
    and - when only the derived ``--daemon`` control port collided - left
    stdout completely empty, so nothing said which of the two ports was the
    problem.

    Carrying the *role* is the whole point: `('0.0.0.0', 3861) in use` does
    not tell an operator which flag to change.
    """

    def __init__(self, role: str, host: str, port: int, cause: OSError):
        self.role = role
        self.host = host
        self.port = port
        self.cause = cause
        flag = "--port" if role == "door" else "--daemon PORT"
        super().__init__(
            f"Cannot start: {role} server cannot use {host}:{port} "
            f"({cause.strerror or cause}); change it with {flag}"
        )


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
        """Clear the current line (prompt and any partial input).

        Only emits the ANSI erase sequence on a real terminal: on a pipe or a
        dumb terminal it would render as literal garbage in the output, and
        there is no cursor to rewind anyway.
        """
        if self._enabled and sys.stdout.isatty():
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


def status_print(message: str = "") -> None:
    """Print an operator-facing status line, flushed.

    stdout is block-buffered whenever it is not a terminal, while logging
    goes to stderr unbuffered. Without an explicit flush the startup banner
    and script progress appear after the fact in a redirected log - or not
    at all, since the buffer dies with the process on SIGTERM.
    """
    print(message, flush=True)


# Default control port offset from simulator port
CONTROL_PORT_OFFSET = 1

#: `--daemon` with no argument. Not a valid port, so it cannot collide with
#: one an operator actually typed.
DAEMON_DEFAULT_CONTROL_PORT = -1

#: Longest control-channel command line accepted, in bytes. Explicit rather
#: than asyncio's implicit default so the refusal can name it.
MAX_CONTROL_LINE = 64 * 1024

#: Inclusive TCP port range. Checked in the parser rather than at bind time,
#: where it surfaced as `OverflowError: bind(): port must be 0-65535` under
#: 30 lines of asyncio traceback.
MIN_PORT = 0
MAX_PORT = 65535


# Default bind address for the (unauthenticated) daemon control channel.
# Loopback by default; widening requires an explicit --control-host.
DEFAULT_CONTROL_HOST = "127.0.0.1"


def _validate_port(parser, flag: str, value: int) -> None:
    """Refuse an out-of-range port with a usage line, like every other flag."""
    if not MIN_PORT <= value <= MAX_PORT:
        parser.error(
            t(
                "simulator.cli.port_must",
                "{flag} {value}: port must be {MIN_PORT}-{MAX_PORT}",
                flag=flag,
                value=value,
                MIN_PORT=MIN_PORT,
                MAX_PORT=MAX_PORT,
            )
        )


def _validate_host(parser, flag: str, value: str) -> None:
    """Refuse an unresolvable bind address with a usage line.

    The bind is going to resolve it anyway; doing it here means the failure
    is an argument error at rc 2 rather than a `socket.gaierror` traceback.
    """
    import socket

    try:
        socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)
    except OSError as err:
        parser.error(
            t(
                "simulator.cli.text",
                "{flag} {value}: {arg0}",
                flag=flag,
                value=value,
                arg0=err.strerror or err,
            )
        )


class _ControlLogHandler(logging.Handler):
    """Logging handler that broadcasts sanitized log lines to control clients.

    Installed on the **root** logger, which makes the broadcast itself a
    log source: writing to a transport whose peer has gone away does not
    raise, it makes asyncio log ``socket.send() raised exception.`` at
    WARNING - synchronously, inside this handler. Without a guard that
    record is broadcast to the same dead writer, producing another record,
    and one closed ``ctl`` session floods every other session with hundreds
    of lines. Three things break the loop: dead writers are dropped,
    failing writers are dropped, and ``emit`` refuses to re-enter.

    Records are also dropped for a client whose write buffer has run away.
    ``emit`` cannot ``drain()`` (it is called synchronously from arbitrary
    logging call sites), so a ``ctl -i`` session parked in a terminal and
    not reading grows an unbounded queue in the daemon's memory - measured
    at +0.16 MB/s under a hostile dribble, tracking the log rate with no
    bound in sight.
    """

    #: Per-client write-buffer ceiling, in bytes, above which log records
    #: are dropped for that client rather than queued in daemon memory.
    MAX_CLIENT_BACKLOG = 1024 * 1024

    def __init__(self, clients: set[asyncio.StreamWriter]):
        super().__init__()
        self._clients = clients
        self._broadcasting = False

    def emit(self, record):
        if self._broadcasting:
            # A record produced *by* this broadcast (asyncio's write
            # warnings). Rebroadcasting it is the feedback loop.
            return
        self._broadcasting = True
        try:
            msg = self.format(record)
            # Sanitize control characters and escape newlines so untrusted
            # (network-derived) data can neither inject terminal escapes nor
            # forge extra protocol lines.
            data = f"LOG: {escape_message(sanitize_text(msg))}\n".encode()
            # Broadcast to all connected control clients
            for writer in list(self._clients):
                if writer.is_closing():
                    # The reader task reaps writers on EOF, but a peer that
                    # died mid-stream is only noticed here.
                    self._clients.discard(writer)
                    continue
                try:
                    if writer.transport.get_write_buffer_size() > self.MAX_CLIENT_BACKLOG:
                        # This client is not reading. Dropping its log lines
                        # is strictly better than growing the daemon's heap
                        # on behalf of a stalled terminal.
                        continue
                    writer.write(data)
                    # Don't await drain here - it would block.
                    # The message will be sent eventually.
                except Exception:
                    self._clients.discard(writer)
        except Exception:
            self.handleError(record)
        finally:
            self._broadcasting = False


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
            # getsockname() is untyped in typeshed; for AF_INET it is (host, port)
            port: int = self.server.sockets[0].getsockname()[1]
            return port
        return self.port

    async def start(self):
        """Start the control server and install the log broadcast handler."""
        self.server = await asyncio.start_server(
            self._handle_client, self.host, self.port, limit=MAX_CONTROL_LINE
        )
        logger.info(
            t(
                "simulator.cli.control_server_listening",
                "Control server listening on {host}:{bound_port}",
                host=self.host,
                bound_port=self.bound_port,
            )
        )

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
            if writer.is_closing():
                self.clients.discard(writer)
                continue
            try:
                writer.write(data)
            except Exception:
                self.clients.discard(writer)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a control connection."""
        addr = writer.get_extra_info("peername")
        logger.info(
            t("simulator.cli.control_connection", "Control connection from {addr}", addr=addr)
        )
        self.clients.add(writer)
        try:
            # Send initial door-client status so ctl can color its prompt
            writer.write(f"STATUS: clients={self._client_count()}\n".encode())
            await writer.drain()

            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # asyncio's readline() raises this when a line exceeds
                    # `limit`; it has already consumed through the newline,
                    # so the connection stays usable. Before, it escaped to
                    # the generic handler below and logged an asyncio
                    # internal ("Separator is found, but chunk is longer
                    # than limit") at ERROR - which _ControlLogHandler then
                    # broadcast into every other operator's ctl session.
                    logger.info(
                        t(
                            "simulator.cli.control_client_sent_over_long",
                            "Control client %s sent an over-long command line",
                        ),
                        addr,
                    )
                    writer.write(
                        f"ERROR: Command line too long (max {MAX_CONTROL_LINE} bytes)\n".encode()
                    )
                    await writer.drain()
                    continue
                if not line:
                    break
                cmd = line.decode(errors="replace").strip()
                if not cmd:
                    # Answering costs one line and removes an unanswerable
                    # request from the protocol: silently skipping it meant
                    # `ppd-simulator-ctl ""` waited out the full --timeout
                    # and then blamed the daemon.
                    writer.write(b"ERROR: Empty command\n")
                    await writer.drain()
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
        except ConnectionError as e:
            # A one-shot ctl reads its `OK:` line and exits while the daemon
            # is still emitting log lines for that command, so essentially
            # every one-shot `run`/`stop` hung up mid-write. Reporting a
            # normal hang-up at ERROR - and broadcasting it to every other
            # ctl session via _ControlLogHandler - trains operators to
            # ignore the one severity that should never be ignorable.
            logger.debug(
                t(
                    "simulator.cli.control_client_hung_up",
                    "Control client {addr} hung up: {e}",
                    addr=addr,
                    e=e,
                )
            )
        except Exception as e:
            logger.error(t("simulator.cli.control_client_error", "Control client error: {e}", e=e))
        finally:
            self.clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(
                t(
                    "simulator.cli.control_connection_closed",
                    "Control connection closed from {addr}",
                    addr=addr,
                )
            )


def _build_state(
    firmware: tuple[int, int, int] | None,
    hardware: tuple[int, int] | None,
    initial_state: dict | None = None,
) -> "DoorSimulatorState | None":
    """Build the starting state from a document and any version overrides.

    Returns None when nothing is requested (the simulator then uses its
    default state).

    ``--firmware``/``--hardware`` are applied **after** the document, so an
    explicit flag beats a file. That is the ordinary precedence for a
    command line over its config, and the alternative - a document silently
    overriding the flag the operator just typed - is the surprising one.
    """
    from .state import DoorSimulatorState
    from .state_io import apply_document

    if not (firmware or hardware or initial_state):
        return None
    state = DoorSimulatorState()
    if initial_state:
        apply_document(state, initial_state)
    if firmware:
        state.fw_major, state.fw_minor, state.fw_patch = firmware
    if hardware:
        state.hw_ver, state.hw_rev = hardware
    return state


async def _process_script_queue(
    script_queue: ScriptQueue,
    stop_event: asyncio.Event,
    cmd_handler: CommandHandler,
    script_runner: "ScriptRunner",
    poll_interval: float = 0.5,
) -> None:
    """Run scripts queued at runtime (via the ``run`` command) until stopped."""
    while not stop_event.is_set():
        try:
            try:
                entry = await asyncio.wait_for(script_queue.get(), timeout=poll_interval)
            except TimeoutError:
                continue

            try:
                script = cmd_handler.load_script(entry.ref)

                def _on_start(entry: QueuedScript = entry, name: str = script.name) -> bool:
                    """Release the claim, and veto a run that was dropped."""
                    # The claim is dropped when the run actually starts, not
                    # when it was dequeued: until then it is still pending
                    # and `status`/`list` must say so. A `stop all` in that
                    # window cancels it, and the run is abandoned rather
                    # than started seconds later.
                    if not script_queue.start(entry):
                        logger.info(
                            t(
                                "simulator.cli.dropped_queued_script",
                                "Dropped queued script: {arg0}",
                                arg0=sanitize_text(name),
                            )
                        )
                        return False
                    logger.info(
                        t(
                            "simulator.cli.running_queued_script",
                            "Running queued script: {arg0}",
                            arg0=sanitize_text(name),
                        )
                    )
                    return True

                success = await script_runner.run(script, on_start=_on_start)
                if not entry.cancelled:
                    status = "PASSED" if success else "FAILED"
                    logger.info(
                        t(
                            "simulator.cli.script",
                            "Script {status}: {arg0}",
                            status=status,
                            arg0=sanitize_text(script.name),
                        )
                    )
            except Exception as e:
                logger.error(
                    t(
                        "simulator.cli.error_running_queued_script",
                        "Error running queued script: {arg0}",
                        arg0=sanitize_text(e),
                    )
                )
            finally:
                # Also covers a load failure, which never reaches on_start.
                script_queue.release(entry)
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

    An **interrupted** run establishes no result: ``script_result[0]`` is left
    at ``None`` and no PASSED/FAILED verdict is printed. ``all_success`` at the
    moment of a cancellation means "nothing has failed *yet*", which is not the
    same thing as "every assertion ran and passed" - printing
    ``>>> All scripts PASSED`` for a run cut off before its ``assert`` step is
    a false green on the documented CI command.
    """
    all_success = True
    run_count = 0
    completed = 0
    interrupted = False
    try:
        # Wait for client connection if requested
        if wait_for_client:
            status_print(
                t("simulator.cli.waiting_client_connection", ">>> Waiting for client connection...")
            )
            while not simulator.protocols:
                if stop_event.is_set():
                    return
                await asyncio.sleep(0.1)
            status_print(
                t(
                    "simulator.cli.client_connected_starting_scripts",
                    ">>> Client connected, starting scripts",
                )
            )

        while True:
            # Check for client disconnect if wait_for_client
            if wait_for_client and not simulator.protocols:
                status_print(
                    t(
                        "simulator.cli.client_disconnected_stopping_scripts",
                        ">>> Client disconnected, stopping scripts",
                    )
                )
                break

            run_count += 1
            completed = 0
            if loop_scripts:
                status_print(
                    t(
                        "simulator.cli.script_run",
                        "\n>>> Script run #{run_count}",
                        run_count=run_count,
                    )
                )

            for i, script_ref in enumerate(scripts):
                # Check for disconnect before each script
                if wait_for_client and not simulator.protocols:
                    # Flushed like every other progress line: this is the
                    # one that explains why the remaining scripts never
                    # ran, and it died in the buffer.
                    status_print(
                        t(
                            "simulator.cli.client_disconnected_stopping_scripts",
                            ">>> Client disconnected, stopping scripts",
                        )
                    )
                    break

                # Add delay between scripts (not before first one)
                if i > 0 and script_delay > 0:
                    status_print(
                        t(
                            "simulator.cli.waiting_s_before_next_script",
                            ">>> Waiting {script_delay}s before next script...",
                            script_delay=script_delay,
                        )
                    )
                    await asyncio.sleep(script_delay)

                try:
                    script = cmd_handler.load_script(script_ref)
                    # The name comes out of a YAML file, which this project's
                    # threat model treats as untrusted; PyYAML's "\e" escape
                    # puts a real ESC in a file that looks clean, and this
                    # goes straight to the operator's terminal.
                    name = sanitize_text(script.name)
                    status_print(
                        t("simulator.cli.running_script", "\n>>> Running script: {name}", name=name)
                    )
                    success = await script_runner.run(script)
                    if not success:
                        all_success = False
                        status_print(
                            t("simulator.cli.script_failed", ">>> Script FAILED: {name}", name=name)
                        )
                    else:
                        status_print(
                            t("simulator.cli.script_passed", ">>> Script PASSED: {name}", name=name)
                        )
                except Exception as e:
                    status_print(
                        t(
                            "simulator.cli.error_running_script",
                            "Error running script '{arg0}': {arg1}",
                            arg0=sanitize_text(script_ref),
                            arg1=sanitize_text(e),
                        )
                    )
                    all_success = False
                completed += 1
            else:
                # Loop completed without break (no disconnect)
                if not loop_scripts:
                    break

                # Delay before next loop iteration
                if script_delay > 0:
                    status_print(
                        t(
                            "simulator.cli.waiting_s_before_next_loop",
                            ">>> Waiting {script_delay}s before next loop...",
                            script_delay=script_delay,
                        )
                    )
                    await asyncio.sleep(script_delay)
                continue

            # Inner loop was broken (disconnect), exit outer loop too
            break

    except asyncio.CancelledError:
        interrupted = True
        raise
    finally:
        if interrupted:
            # No verdict: leave script_result[0] at None so main() exits
            # non-zero, and report what actually happened instead.
            if oneshot:
                status_print(
                    t(
                        "simulator.cli.interrupted_after_script_s",
                        "\n>>> Interrupted after {completed} of {arg0} script(s)",
                        completed=completed,
                        arg0=len(scripts),
                    )
                )
        else:
            script_result[0] = all_success
            if oneshot:
                status_print(
                    t(
                        "simulator.cli.all_scripts",
                        "\n>>> All scripts {arg0}",
                        arg0="PASSED" if all_success else "FAILED",
                    )
                )
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
        self._blocking_task: asyncio.Task | None = None

    def start(self) -> None:
        """Enable the prompt and start reading stdin."""
        self._prompt.enable()
        try:
            self._loop.add_reader(self._stdin.fileno(), self.handle_input)
        except OSError:
            # Not every stdin is pollable: epoll rejects /dev/null, a
            # regular file and the temp file bash uses for a heredoc with
            # PermissionError. Read those with plain blocking reads (they
            # never wait long) instead of dying with a traceback.
            self._blocking_task = asyncio.ensure_future(self._read_blocking())

    def stop(self) -> None:
        """Stop reading stdin (idempotent; safe if never started)."""
        self._reader_removed = True
        task, self._blocking_task = self._blocking_task, None
        if task is not None:
            task.cancel()
        try:
            self._loop.remove_reader(self._stdin.fileno())
        except Exception:
            pass  # Already removed, or never added

    def handle_input(self) -> None:
        """Reader callback: consume one line and schedule its execution."""
        # Don't read if we're shutting down (prevents blocking)
        if self._stop_event.is_set() or self._reader_removed:
            return
        try:
            line = self._stdin.readline()
        except Exception as e:
            self._prompt.output(f"Error: {e}")
            return
        self._handle_line(line)

    def _handle_line(self, line: str) -> bool:
        """Act on one raw line; False once the session is over.

        An empty string is EOF, not a bare Enter. That distinction is the
        whole point: an fd at EOF is permanently readable, so treating it
        as Enter re-arms this callback forever - a busy loop that prints a
        prompt per turn and never exits. EOF ends the session, exactly like
        Ctrl-D on a terminal.
        """
        if line == "":
            self.stop()
            self._prompt.clear_line()
            self._stop_event.set()
            return False
        line = line.strip()
        if line:
            asyncio.create_task(self.process_command(line))
        else:
            # Empty line (just Enter), re-show prompt
            self._prompt.show()
        return True

    async def _read_blocking(self) -> None:
        """Drive :meth:`_handle_line` from blocking reads (non-pollable stdin)."""
        while not self._stop_event.is_set() and not self._reader_removed:
            line = await self._loop.run_in_executor(None, self._stdin.readline)
            if not self._handle_line(line):
                return

    async def process_command(self, line: str) -> None:
        """Execute one command line and print its result."""
        result = await self._cmd_handler.execute(line)
        # Don't show prompt again after shutdown command
        if self._stop_event.is_set():
            self._prompt.clear_line()
            if result.message:
                status_print(render_result(result.message))
            # Remove stdin reader immediately to avoid blocking shutdown
            self.stop()
        elif result.message:
            self._prompt.output(render_result(result.message))
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
    states_dir: str | None = None,
    initial_state: dict | None = None,
    history_file: str | None = None,
    firmware: tuple[int, int, int] | None = None,
    hardware: tuple[int, int] | None = None,
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
        states_dir: Directory of state documents `reset` can load by bare name
        initial_state: State document applied over the defaults at startup,
                       and restored by a bare `reset`
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
    state = _build_state(firmware, hardware, initial_state)

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
    try:
        await simulator.start()
    except OSError as err:
        raise SimulatorStartupError(t("simulator.cli.door", "door"), host, port, err) from err

    # The actual bound port (differs from `port` when an ephemeral port 0 was
    # requested, e.g. in tests)
    actual_port = port
    if simulator.server and simulator.server.sockets:  # pragma: no branch (bound after start())
        actual_port = simulator.server.sockets[0].getsockname()[1]

    script_runner = ScriptRunner(simulator, initial_state_document=initial_state)

    # Set up control structures
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    script_result = [None]  # Use list to allow mutation in nested function
    script_queue = ScriptQueue()

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
        states_dir=states_dir,
        initial_state_document=initial_state,
    )
    # The handler owns the state-file path policy, so a `reset` step in a
    # script resolves names through it rather than reaching the filesystem
    # on its own - a script arriving over the control channel must not be
    # able to read a file the channel itself would refuse.
    script_runner.load_state_document = cmd_handler.load_state_document

    # The handler publishes scripts_dir on construction; say so out loud when
    # the directory turns out to hold nothing runnable.
    if scripts_dir:
        from .scripting import list_extra_scripts

        if not list_extra_scripts():
            logger.warning(
                t("simulator.cli.yaml_yml_scripts_found", "No *.yaml/*.yml scripts found in %s"),
                scripts_dir,
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
        try:
            await control_channel.start()
        except OSError as err:
            await simulator.stop()
            raise SimulatorStartupError(
                t("simulator.cli.control", "control"), control_host, control_port, err
            ) from err
        channel_holder[0] = control_channel

    # Print startup info
    status_print(
        t(
            "simulator.cli.simulator_started",
            "Simulator started on {host}:{actual_port}",
            host=host,
            actual_port=actual_port,
        )
    )
    if control_channel:
        status_print(
            t(
                "simulator.cli.control_channel",
                "Control channel: {control_host}:{bound_port}",
                control_host=control_host,
                bound_port=control_channel.bound_port,
            )
        )
    if interactive:
        status_print("=" * 65)
        status_print(cmd_handler.get_help())
        status_print("=" * 65)
    status_print()

    if on_ready:
        on_ready(actual_port, control_channel.bound_port if control_channel else None)

    # Process queued scripts in background
    queue_task = asyncio.create_task(
        _process_script_queue(script_queue, stop_event, cmd_handler, script_runner)
    )

    # Run startup scripts if specified. The task reference is held (and the
    # task cancelled in the cleanup below) so that `_run_startup_scripts`'
    # `finally` always runs *before* `script_result[0]` is read. As a bare
    # `create_task` it was instead reaped by `asyncio.run`'s shutdown, i.e.
    # after main() had already read `None` and fallen through to exit 0 - and
    # an un-referenced task is eligible for garbage collection.
    startup_task: asyncio.Task | None = None
    if scripts:
        startup_task = asyncio.create_task(
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
                                print(session.format_recall(input_line))

                            result = await cmd_handler.execute(input_line.resolved)
                            session.handle_result(input_line, result.success)

                            if result.message:
                                # Command output can carry network-poisoned
                                # state (a wire-set timezone, say): never
                                # print it to a terminal raw.
                                print(render_result(result.message))
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
            # Not daemon mode as the docs define it (no control channel is
            # started on this path) - just headless.
            logger.warning(
                t(
                    "simulator.cli.stdin_available_running_without_interactive",
                    "stdin not available, running without interactive input",
                )
            )

    # Handle run_for timeout
    if run_for:

        async def timeout_shutdown():
            await asyncio.sleep(run_for)
            logger.info(
                t(
                    "simulator.cli.run_time_s_elapsed_shutting",
                    "Run time ({run_for}s) elapsed, shutting down",
                    run_for=run_for,
                )
            )
            stop_event.set()

        asyncio.create_task(timeout_shutdown())

    # Wait for stop signal.
    #
    # A cancellation here is how `asyncio.Runner` delivers Ctrl-C: it cancels
    # the main task and only turns that back into `KeyboardInterrupt` if the
    # cancellation actually propagates. Swallowing it made `asyncio.run()`
    # return normally, so main()'s `except KeyboardInterrupt` never fired and
    # an interrupted `--oneshot` run exited 0. Clean up in the `finally` and
    # let the cancellation through.
    try:
        await stop_event.wait()
    finally:
        # Cleanup
        if startup_task:
            startup_task.cancel()
            try:
                await startup_task
            except asyncio.CancelledError:
                pass
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
        "--list-scripts",
        "-l",
        action="store_true",
        help="List runnable scripts (built-in, plus --scripts-dir if given) and exit",
    )
    parser.add_argument(
        "--daemon",
        "-D",
        nargs="?",
        type=int,
        const=DAEMON_DEFAULT_CONTROL_PORT,  # Sentinel: use default (port+1)
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
        "--list-states",
        action="store_true",
        help="List state documents in --states-dir (loadable by bare name) and exit",
    )
    parser.add_argument(
        "--initial-state",
        metavar="FILE",
        default=None,
        help="State document (JSON, or YAML with PyYAML) applied over the defaults "
        "at startup. A bare 'reset' returns to it.",
    )
    parser.add_argument(
        "--states-dir",
        metavar="DIR",
        default=None,
        help="Directory of state documents that 'reset' can load by bare name",
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

    # A typo'd --scripts-dir used to be silently ignored: the daemon started
    # happily and the first sign of trouble was an "Unknown script" error
    # from a ctl user much later.
    if args.scripts_dir is not None and not Path(args.scripts_dir).is_dir():
        parser.error(
            t(
                "simulator.cli.scripts_dir_directory",
                "--scripts-dir {scripts_dir}: not a directory",
                scripts_dir=args.scripts_dir,
            )
        )

    if args.states_dir is not None and not Path(args.states_dir).is_dir():
        parser.error(
            t(
                "simulator.cli.states_dir_directory",
                "--states-dir {states_dir}: not a directory",
                states_dir=args.states_dir,
            )
        )

    # Parsed here rather than at start(): a malformed initial state must
    # fail the command line, not a running daemon several seconds later,
    # for the same reason a typo'd --scripts-dir does.
    initial_state_document = None
    if args.initial_state is not None:
        from .state_io import StateDocumentError, load_document

        try:
            initial_state_document = load_document(args.initial_state)
        except StateDocumentError as exc:
            parser.error(
                t(
                    "simulator.cli.initial_state_invalid",
                    "--initial-state {initial_state}: {arg0}",
                    initial_state=args.initial_state,
                    arg0=str(exc),
                )
            )

    # Bind-time values were the one class of bad argument this parser did
    # not check, so `--port 99999` reached socket.bind() and exited with a
    # 30-line `OverflowError: bind(): port must be 0-65535` through asyncio
    # internals, and `--host 300.1.1.1` with a `socket.gaierror`. Everything
    # else here already answers with a usage line at rc 2; these now do too.
    _validate_port(parser, "--port", args.port)
    if args.daemon is not None and args.daemon != DAEMON_DEFAULT_CONTROL_PORT:
        _validate_port(parser, "--daemon", args.daemon)
    _validate_host(parser, "--host", args.host)
    _validate_host(parser, "--control-host", args.control_host)

    # `--run-for -5` was accepted and silently meant "shut down
    # immediately", logging `Run time (-5.0s) elapsed, shutting down` as if
    # five negative seconds had passed.
    if args.run_for is not None and args.run_for <= 0:
        parser.error(
            t(
                "simulator.cli.run_must_greater_than",
                "--run-for {run_for:g}: must be greater than 0",
                run_for=args.run_for,
            )
        )

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    # Defence in depth for anything not sanitized at its source: the
    # interactive paths install this formatter themselves, but --script
    # (headless/CI) and --daemon kept the plain one, so they had no
    # terminal-escape protection at all.
    for handler in logging.getLogger().handlers:
        handler.setFormatter(_SanitizingFormatter("%(asctime)s [%(levelname)s] %(message)s"))

    # List scripts and exit
    if args.list_scripts:
        from .scripting import render_script_listing, set_extra_scripts_dir

        # The very same renderer the `list` command uses. These two
        # surfaces were always *intended* to agree, but the shadow marker
        # once landed only in `list`, so the pre-flight surface printed a
        # shadowed name twice with no marker. Sharing the renderer is the
        # only way that stays true.
        set_extra_scripts_dir(args.scripts_dir)
        for line in render_script_listing(args.scripts_dir).lines:
            # Names and descriptions come out of YAML files.
            print(sanitize_text(line))
        return

    # List state documents and exit. The pre-flight surface for `reset`,
    # needing no daemon, exactly as --list-scripts is for `run`.
    if args.list_states:
        from .state_io import render_state_listing

        for line in render_state_listing(args.states_dir):
            print(sanitize_text(line))
        return

    # Determine daemon mode and control port
    daemon = args.daemon is not None

    # Validate mutually exclusive options
    if args.scripts and daemon:
        parser.error(
            t(
                "simulator.cli.script_daemon_mutually_exclusive",
                "--script and --daemon are mutually exclusive",
            )
        )

    # Mode-scoped flags: silently ignoring them is a repeatability trap in
    # CI wrappers (e.g. `ppd-simulator --oneshot` with no script exits 0
    # having run nothing but an interactive session).
    script_only_flags = [
        ("--loop", args.loop),
        ("--script-delay", bool(args.script_delay)),
        ("--oneshot", args.oneshot),
        ("--wait-for-client", args.wait_for_client),
    ]
    if not args.scripts:
        unusable = [name for name, given in script_only_flags if given]
        if unusable:
            # In daemon mode --script is itself refused, so "use --script"
            # would just send the operator round a second time.
            if daemon:
                parser.error(
                    t(
                        "simulator.cli.available_daemon_mode",
                        "{arg0} is not available in daemon mode",
                        arg0=", ".join(unusable),
                    )
                )
            parser.error(
                t(
                    "simulator.cli.cannot_used_without_script",
                    "{arg0} cannot be used without --script",
                    arg0=", ".join(unusable),
                )
            )
    if not daemon and args.control_host != DEFAULT_CONTROL_HOST:
        parser.error(
            t("simulator.cli.control_host_requires_daemon", "--control-host requires --daemon")
        )

    if daemon:
        # -1 means use default (port + CONTROL_PORT_OFFSET), otherwise the
        # port given. The offset was a named constant *and* inlined here,
        # so nothing read the constant and the documented default had no
        # executable pin.
        control_port = (
            args.port + CONTROL_PORT_OFFSET
            if args.daemon == DAEMON_DEFAULT_CONTROL_PORT
            else args.daemon
        )
    else:
        control_port = None

    # Parse firmware version if provided
    firmware = None
    if args.firmware:
        try:
            parts = args.firmware.split(".")
            if len(parts) != 3:
                parser.error(
                    t(
                        "simulator.cli.firmware_version_must_format_major",
                        "Firmware version must be in format major.minor.patch (e.g., '1.2.3')",
                    )
                )
            firmware = (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            parser.error(
                t(
                    "simulator.cli.firmware_version_must_contain_only",
                    "Firmware version must contain only numbers (e.g., '1.2.3')",
                )
            )

    # Parse hardware version if provided
    hardware = None
    if args.hardware:
        try:
            parts = args.hardware.split(".")
            if len(parts) != 2:
                parser.error(
                    t(
                        "simulator.cli.hardware_version_must_format_ver",
                        "Hardware version must be in format ver.rev (e.g., '1.1')",
                    )
                )
            # Integers, because a real door's fwInfo object is all integers.
            hardware = (int(parts[0]), int(parts[1]))
        except ValueError:
            parser.error(
                t(
                    "simulator.cli.hardware_version_must_contain_only",
                    "Hardware version must contain only numbers (e.g., '1.1')",
                )
            )

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
                states_dir=args.states_dir,
                initial_state=initial_state_document,
                history_file=args.history,
                firmware=firmware,
                hardware=hardware,
            )
        )

        # Exit with appropriate code for CI/CD. `result is None` means the run
        # never established a verdict - it was interrupted before the scripts
        # finished - which can never legitimately mean success. `--oneshot`
        # without `--script` is already refused above at rc 2, so there is no
        # other way to reach this with no result.
        if args.oneshot:
            sys.exit(0 if result else 1)

    except SimulatorStartupError as err:
        # One operator sentence naming the role of the port that failed,
        # not 30 lines of asyncio internals with build-machine paths in
        # them. The traceback stays available behind --debug.
        print(err, file=sys.stderr)
        if args.debug:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    except KeyboardInterrupt:
        print(t("simulator.cli.simulator_stopped", "\nSimulator stopped."))
        # Interrupted runs must not report success to CI (128 + SIGINT)
        sys.exit(130)


if __name__ == "__main__":
    main()
