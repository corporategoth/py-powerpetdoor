# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Script running commands."""

import asyncio
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ..scripting import list_extra_scripts, script_completer
from .base import ArgSpec, CommandResult, command

if TYPE_CHECKING:
    from ..scripting import Script, ScriptRunner

#: The ``run`` command name and its aliases (single source of truth, also
#: used by ctl to recognize a synchronous run).
RUN_COMMAND = "run"
RUN_ALIASES = ["r", "file"]
#: Trailing keyword that makes ``run`` synchronous.
RUN_WAIT_KEYWORD = "wait"
#: Trailing keyword that makes ``stop`` drain the pending queue too.
STOP_ALL_KEYWORD = "all"


class ScriptQueue:
    """Script runs waiting to start, including the one already claimed.

    The queue consumer takes an entry off as soon as one exists and only
    *then* waits for the run lock, so a bare ``asyncio.Queue.qsize()``
    under-reported by one for the commonest case - a single script waiting
    behind a ``run ... wait`` - which displayed as "nothing pending" when
    something was (M2). A claimed entry stays counted, and named, until its
    run actually starts.
    """

    def __init__(self) -> None:
        self._waiting: deque[str] = deque()
        self._claimed: list[str] = []
        self._arrived = asyncio.Event()

    async def put(self, script_ref: str) -> None:
        """Queue a script reference to run when the runner is free."""
        self._waiting.append(script_ref)
        self._arrived.set()

    async def get(self) -> str:
        """Wait for the next queued run and claim it.

        The claim keeps the entry visible to :meth:`pending` while the
        consumer waits for the run lock; :meth:`release` drops it once the
        run has started.
        """
        while not self._waiting:
            self._arrived.clear()
            await self._arrived.wait()
        script_ref = self._waiting.popleft()
        self._claimed.append(script_ref)
        return script_ref

    def release(self, script_ref: str) -> None:
        """Drop a claim taken by :meth:`get`; a repeat call is a no-op."""
        if script_ref in self._claimed:
            self._claimed.remove(script_ref)

    def clear(self) -> list[str]:
        """Discard every run that has not been claimed yet.

        Returns:
            The references dropped, in queue order.
        """
        dropped = list(self._waiting)
        self._waiting.clear()
        return dropped

    def qsize(self) -> int:
        """Pending runs, counting one claimed but not yet started (M2)."""
        return len(self._waiting) + len(self._claimed)

    def pending(self) -> list[str]:
        """The pending run references, in the order they will run."""
        return [*self._claimed, *self._waiting]


class ScriptStatus(NamedTuple):
    """A snapshot of the script runner's state.

    Attributes:
        running: Name of the running script, or None.
        queued: How many runs are still pending.
        stopping: Whether a stop has been requested for the running script.
        pending: References of the pending runs, in order.
    """

    running: str | None
    queued: int
    stopping: bool
    pending: tuple[str, ...]


def format_script_status(status: ScriptStatus) -> str:
    """Render the runner's busy state as one operator-facing line.

    A requested-but-not-yet-effective stop gets its own word: ``stop`` takes
    effect at a step boundary, and without this the operator cannot tell a
    registered stop from one that never arrived (L3).
    """
    if status.running is None:
        state = "Script: none running"
    elif status.stopping:
        state = f'Script: stopping "{status.running}"'
    else:
        state = f'Script: running "{status.running}"'
    if status.queued:
        state += f" ({status.queued} queued)"
    return state


def is_wait_run(command_line: str) -> bool:
    """Whether ``command_line`` is a synchronous ``run <script> wait``.

    A wait-run blocks for as long as the script takes, so ctl must not hold
    it to the generic response timeout.
    """
    parts = command_line.split()
    return (
        len(parts) >= 3
        and parts[0].lower() in (RUN_COMMAND, *RUN_ALIASES)
        and parts[-1].lower() == RUN_WAIT_KEYWORD
    )


class ScriptsCommandsMixin:
    """Mixin providing script running commands."""

    script_runner: "ScriptRunner"
    script_queue: ScriptQueue | None
    _scripts_dir: str | None
    _allow_script_paths: bool
    _Script: type["Script"]
    _get_builtin_script: Callable[[str], "Script"]
    _list_builtin_scripts: Callable[[], list[tuple[str, str]]]

    def load_script(self, script_ref: str) -> "Script":
        """Load a script - auto-detect if it's a file path or built-in name.

        When ``_allow_script_paths`` is False (unauthenticated control channel),
        only bare script names are accepted; they resolve against the configured
        scripts directory and the built-in scripts, never arbitrary paths.
        """
        if not self._allow_script_paths:
            return self._load_script_restricted(script_ref)
        path = Path(script_ref)
        if path.exists():
            return self._Script.from_file(path)
        else:
            return self._load_script_by_name(script_ref)

    def _load_script_restricted(self, script_ref: str) -> "Script":
        """Load a script by bare name only (no paths, no traversal)."""
        if "/" in script_ref or "\\" in script_ref or script_ref.startswith("."):
            raise ValueError(
                "Script paths are not allowed over the control channel; "
                "use a bare script name (see 'list')"
            )
        return self._load_script_by_name(script_ref)

    def _load_script_by_name(self, name: str) -> "Script":
        """Resolve a bare script name against the scripts dir, then built-ins."""
        if self._scripts_dir:
            base = Path(self._scripts_dir).resolve()
            for suffix in (".yaml", ".yml"):
                candidate = (base / f"{name}{suffix}").resolve()
                # Belt and braces: never follow a resolved path out of the base dir
                if candidate.is_file() and candidate.parent == base:
                    return self._Script.from_file(candidate)
        return self._get_builtin_script(name)

    def script_status(self) -> ScriptStatus:
        """The runner's state: what is running, what is pending, and why.

        Serialized runs made "busy" a real state; this is the single place
        that reports it, shared by ``status``, ``list`` and ``stop`` (M5).
        """
        running = self.script_runner.current_script if self.script_runner.busy else None
        queue = self.script_queue
        return ScriptStatus(
            running=running,
            queued=queue.qsize() if queue else 0,
            stopping=running is not None and self.script_runner.stop_requested,
            pending=tuple(queue.pending()) if queue else (),
        )

    @command("list", ["/", "scripts"], "List runnable scripts", category="scripts")
    def list_scripts(self) -> CommandResult:
        """List available scripts (built-in plus any from --scripts-dir)."""
        scripts = list(self._list_builtin_scripts())
        lines = ["Built-in scripts:"]
        for name, desc in scripts:
            lines.append(f"  {name}: {desc}")
        extra = list_extra_scripts()
        if extra:
            lines.append(f"Scripts from {self._scripts_dir}:")
            for name, desc in extra:
                lines.append(f"  {name}: {desc}")
        status = self.script_status()
        lines.append(format_script_status(status))
        if status.pending:
            # A bare count cannot answer "is the thing I queued five
            # minutes ago still waiting?" (M2).
            lines.append(f"Queued: {', '.join(status.pending)}")
        return CommandResult(
            True,
            "\n".join(lines),
            {
                "scripts": scripts + extra,
                "running": status.running,
                "queued": status.queued,
                "pending": list(status.pending),
                "stopping": status.stopping,
            },
        )

    @command(
        "stop",
        [],
        "Stop the running script",
        category="scripts",
        args=[
            ArgSpec(
                "scope",
                "choice",
                required=False,
                choices=[STOP_ALL_KEYWORD],
                description="'all' to discard every queued run as well",
            )
        ],
    )
    def stop_script(self, scope: str | None = None) -> CommandResult:
        """Stop the script that is currently running.

        This stops the *script*, not the simulator - ``shutdown`` does that.
        The runner checks the request between steps, so the script ends at
        its next step boundary and reports FAILED to whoever is waiting on
        it.

        ``stop all`` also empties the pending queue, so an operator does not
        have to issue one ``stop`` per queued entry and guess how many are
        left (L2).
        """
        status = self.script_status()
        dropped: list[str] = []
        if scope == STOP_ALL_KEYWORD and self.script_queue:
            dropped = self.script_queue.clear()
        if status.running is None:
            if dropped:
                return CommandResult(True, f"Dropped {len(dropped)} queued script(s)")
            # `stop` used to be an alias for `shutdown`, so muscle memory is
            # the likeliest reason it lands on an idle simulator (T4).
            return CommandResult(
                False, "No script is running (use 'shutdown' to stop the simulator)"
            )
        suffix = f" (dropped {len(dropped)} queued)" if dropped else ""
        if status.stopping:
            # Repeating `stop` used to answer with a fresh success, which
            # reads as if the first one had not registered (L3).
            return CommandResult(True, f"Stop already requested for: {status.running}{suffix}")
        self.script_runner.stop()
        return CommandResult(True, f"Stopping script: {status.running}{suffix}")

    @command(
        RUN_COMMAND,
        RUN_ALIASES,
        "Run a script",
        category="scripts",
        args=[
            ArgSpec(
                "script",
                "string",
                description="Script name or file path",
                completer=script_completer,
            ),
            ArgSpec(
                "mode",
                "choice",
                required=False,
                choices=[RUN_WAIT_KEYWORD],
                description="'wait' to run synchronously and report PASSED/FAILED "
                "(success reflects the script result; fails if another script is running)",
            ),
        ],
    )
    async def run(self, script_ref: str, mode: str | None = None) -> CommandResult:
        """Run a script (built-in name or file path).

        Without 'wait' the script is queued and the result only reports the
        queueing; a queued script waits for any in-flight script.

        With 'wait', the script runs synchronously and the result reflects
        pass/fail - useful for scripting against the control channel. A
        wait-run never queues: if another script is already running it
        fails immediately, so the reported pass/fail always belongs to the
        script that was asked for.
        """
        try:
            script = self.load_script(script_ref)
            if self.script_queue and mode != RUN_WAIT_KEYWORD:
                await self.script_queue.put(script_ref)
                return CommandResult(True, f"Queued script: {script.name}")
            else:
                # Run directly (no queue configured, or 'wait' requested)
                success = await self.script_runner.run(script, queue_if_busy=False)
                status = "PASSED" if success else "FAILED"
                return CommandResult(success, f"Script {status}: {script.name}")
        except Exception as e:
            # The transport already labels failures ("ERROR: ..."), so an
            # inner "Error: " prefix only doubles it up (T2).
            return CommandResult(False, str(e))
