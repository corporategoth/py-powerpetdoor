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

from ..scripting import (
    describe_script_argument,
    list_extra_scripts,
    script_completer,
    script_escapes_directory,
)
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


class QueuedScript:
    """One pending run: how to load it, what to call it, and its state.

    Deliberately a plain object rather than a tuple or a NamedTuple:
    ``run quick`` twelve times enqueues twelve *distinct* runs, so entries
    must compare by identity. The display name is carried alongside the
    reference because ``run`` has already loaded the script and knows it -
    ``list`` used to print a raw ``./scripts/long2.yaml`` on its ``Queued:``
    line while every other line printed ``Long Script B`` (T2).

    Attributes:
        ref: The reference passed to ``run``, used to load the script.
        name: The script's display name.
        cancelled: Set by :meth:`ScriptQueue.clear` when the entry is
            dropped, including after the consumer has claimed it.
    """

    __slots__ = ("cancelled", "name", "ref")

    def __init__(self, ref: str, name: str) -> None:
        self.ref = ref
        self.name = name
        self.cancelled = False


class ScriptQueue:
    """Script runs waiting to start, including the one already claimed.

    The queue consumer takes an entry off as soon as one exists and only
    *then* waits for the run lock, so a bare ``asyncio.Queue.qsize()``
    under-reported by one for the commonest case - a single script waiting
    behind a ``run ... wait`` - which displayed as "nothing pending" when
    something was (M2). A claimed entry stays counted, and named, until its
    run actually starts.

    A claim is *cancellable*, not merely visible. ``stop all`` used to
    empty ``_waiting`` only, so the one entry claim-tracking exists for
    survived the drop and started running seconds later - the drop count
    contradicted the queue depth ``list`` had just printed, and clearing
    one running plus N queued runs took two ``stop all`` commands
    (frontend M1). :meth:`clear` now marks claimed entries cancelled and
    :meth:`start` reports that to the consumer, which abandons the run.
    """

    def __init__(self) -> None:
        self._waiting: deque[QueuedScript] = deque()
        self._claimed: list[QueuedScript] = []
        self._arrived = asyncio.Event()

    async def put(self, script_ref: str, name: str) -> QueuedScript:
        """Queue a script to run when the runner is free.

        Args:
            script_ref: Reference the consumer loads the script from.
            name: Display name, already known to the caller.

        Returns:
            The queued entry.
        """
        entry = QueuedScript(script_ref, name)
        self._waiting.append(entry)
        self._arrived.set()
        return entry

    async def get(self) -> QueuedScript:
        """Wait for the next queued run and claim it.

        The claim keeps the entry visible to :meth:`pending` while the
        consumer waits for the run lock; :meth:`start` drops it once the
        run actually starts.
        """
        while not self._waiting:
            self._arrived.clear()
            await self._arrived.wait()
        entry = self._waiting.popleft()
        self._claimed.append(entry)
        return entry

    def start(self, entry: QueuedScript) -> bool:
        """Release a claim and report whether the run may proceed.

        Returns:
            False if :meth:`clear` dropped this entry while the consumer
            was parked on the run lock - the run must be abandoned, since
            it is exactly the run the operator just discarded.
        """
        self.release(entry)
        return not entry.cancelled

    def release(self, entry: QueuedScript) -> None:
        """Drop a claim taken by :meth:`get`; a repeat call is a no-op."""
        if entry in self._claimed:
            self._claimed.remove(entry)

    def clear(self) -> list[QueuedScript]:
        """Discard every pending run, claimed or not.

        Returns:
            The entries dropped, in the order :meth:`pending` reported
            them - so ``len()`` of this always equals the ``queued`` count
            ``status``/``list`` showed a moment earlier.
        """
        dropped = [*self._claimed, *self._waiting]
        for entry in dropped:
            entry.cancelled = True
        self._claimed.clear()
        self._waiting.clear()
        return dropped

    def qsize(self) -> int:
        """Pending runs, counting one claimed but not yet started (M2)."""
        return len(self._waiting) + len(self._claimed)

    def pending(self) -> list[str]:
        """The pending runs' names, in the order they will run."""
        return [entry.name for entry in (*self._claimed, *self._waiting)]


class ScriptStatus(NamedTuple):
    """A snapshot of the script runner's state.

    Attributes:
        running: Name of the running script, or None.
        queued: How many runs are still pending.
        stopping: Whether a stop has been requested for the running script.
        pending: Names of the pending runs, in order.
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
        """Resolve a bare script name against the scripts dir, then built-ins.

        Raises:
            ValueError: If the name matches a file in the scripts directory
                that resolves outside it. Falling through to
                ``Unknown script: <name>. Available: ..., <name>, ...``
                told the operator the script both did and did not exist,
                with no hint that a path policy was involved (round-6
                frontend L1). ``list``/``--list-scripts``/completion no
                longer advertise such files either.
        """
        if self._scripts_dir:
            base = Path(self._scripts_dir).resolve()
            for suffix in (".yaml", ".yml"):
                candidate = base / f"{name}{suffix}"
                if not candidate.is_file():
                    continue
                # Never follow a resolved path out of the base dir.
                if script_escapes_directory(candidate, base):
                    raise ValueError(
                        f"Script '{name}' resolves outside {self._scripts_dir} and cannot be "
                        f"run by name; move it into the directory or run it by path"
                    )
                return self._Script.from_file(candidate.resolve())
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
        """List available scripts (built-in plus any from --scripts-dir).

        A ``--scripts-dir`` script whose name matches a built-in shadows it
        (docs/simulator.md records that precedence). The listing says so
        rather than printing the same name twice with two descriptions and
        no marker of which one ``run`` picks - over ctl the built-in is
        genuinely unreachable, since paths are refused (round-6 frontend
        L3).
        """
        extra = list_extra_scripts()
        shadowed = {name for name, _ in extra}
        scripts = list(self._list_builtin_scripts())
        lines = ["Built-in scripts:"]
        for name, desc in scripts:
            marker = f" (shadowed by {self._scripts_dir}/{name})" if name in shadowed else ""
            lines.append(f"  {name}: {desc}{marker}")
        if self._scripts_dir is not None:
            # Header even when the directory is empty, exactly as
            # `--list-scripts` prints it: a ctl user who cannot see the
            # command line otherwise cannot tell "no --scripts-dir
            # configured" from "configured but empty" (T5).
            lines.append(f"Scripts from {self._scripts_dir}:")
            for name, desc in extra:
                lines.append(f"  {name}: {desc}")
            if not extra:
                lines.append("  (none)")
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

        ``stop all`` also empties the pending queue - including a run the
        consumer has already claimed but not started - so an operator does
        not have to issue one ``stop`` per queued entry and guess how many
        are left (L2, frontend M1). Its drop count therefore always matches
        the ``queued`` count ``status``/``list`` reported a moment earlier.
        """
        status = self.script_status()
        dropped: list[QueuedScript] = []
        if scope == STOP_ALL_KEYWORD and self.script_queue:
            dropped = self.script_queue.clear()
        if status.running is None:
            if dropped:
                return CommandResult(True, f"Dropped {len(dropped)} queued script(s)")
            if scope == STOP_ALL_KEYWORD:
                # "leave nothing running or queued" is already true, so a
                # CI wrapper doing `ctl stop all || fail` must not see a
                # failure for having nothing to do (T1).
                return CommandResult(True, "Nothing running or queued")
            if status.queued:
                # A claimed-but-not-started run: something *is* pending
                # even though nothing is running, so the flat "No script is
                # running" was wrong and left the operator polling `list`.
                return CommandResult(
                    False,
                    f"No script is running; {status.queued} queued "
                    f"(use 'stop all' to discard them)",
                )
            # `stop` used to be an alias for `shutdown`, so muscle memory is
            # the likeliest reason it lands on an idle simulator (T4).
            return CommandResult(
                False, "No script is running (use 'shutdown' to stop the simulator)"
            )
        if dropped:
            suffix = f" (dropped {len(dropped)} queued)"
        elif status.queued:
            # Plain `stop` on a non-empty queue immediately starts the next
            # run - so "the door is now idle" is exactly the wrong mental
            # model, and a repeated `stop` (the commonest way to check
            # whether the first one landed) kills the *next* script rather
            # than being idempotent (round-6 frontend L2).
            suffix = f" ({status.queued} still queued; use 'stop all' to discard them)"
        else:
            suffix = ""
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
                description=describe_script_argument,
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
                # The name goes onto the queue with the reference: `list`
                # reports names, and the loader has already resolved it (T2).
                await self.script_queue.put(script_ref, script.name)
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
