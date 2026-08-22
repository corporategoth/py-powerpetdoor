# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Script running commands."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

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


def format_script_status(running: str | None, queued: int) -> str:
    """Render the runner's busy state as one operator-facing line."""
    if running is None:
        state = "Script: none running"
    else:
        state = f'Script: running "{running}"'
    if queued:
        state += f" ({queued} queued)"
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
    script_queue: asyncio.Queue | None
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

    def script_status(self) -> tuple[str | None, int]:
        """The running script's name (or None) and the queue depth.

        Serialized runs made "busy" a real state; this is the single place
        that reports it, shared by ``status``, ``list`` and ``stop`` (M5).
        """
        running = self.script_runner.current_script if self.script_runner.busy else None
        queued = self.script_queue.qsize() if self.script_queue else 0
        return running, queued

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
        running, queued = self.script_status()
        lines.append(format_script_status(running, queued))
        return CommandResult(
            True,
            "\n".join(lines),
            {"scripts": scripts + extra, "running": running, "queued": queued},
        )

    @command("stop", [], "Stop the running script", category="scripts")
    def stop_script(self) -> CommandResult:
        """Stop the script that is currently running.

        This stops the *script*, not the simulator - ``shutdown`` does that.
        The runner checks the request between steps, so the script ends at
        its next step boundary and reports FAILED to whoever is waiting on
        it.
        """
        running, _queued = self.script_status()
        if running is None:
            return CommandResult(False, "No script is running")
        self.script_runner.stop()
        return CommandResult(True, f"Stopping script: {running}")

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
