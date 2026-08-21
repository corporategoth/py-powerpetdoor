# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Script running commands."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..scripting import script_completer
from .base import ArgSpec, CommandResult, command

if TYPE_CHECKING:
    from ..scripting import Script, ScriptRunner


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

    @command("list", ["/", "scripts"], "List built-in scripts", category="scripts")
    def list_scripts(self) -> CommandResult:
        """List available built-in scripts."""
        scripts = list(self._list_builtin_scripts())
        lines = ["Built-in scripts:"]
        for name, desc in scripts:
            lines.append(f"  {name}: {desc}")
        return CommandResult(True, "\n".join(lines), {"scripts": scripts})

    @command(
        "run",
        ["r", "file"],
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
                choices=["wait"],
                description="'wait' to run synchronously and report PASSED/FAILED "
                "(success reflects the script result)",
            ),
        ],
    )
    async def run(self, script_ref: str, mode: str | None = None) -> CommandResult:
        """Run a script (built-in name or file path).

        With 'wait', the script runs synchronously and the result reflects
        pass/fail - useful for scripting against the control channel where
        the queued result would otherwise only appear in the daemon log.
        """
        try:
            script = self.load_script(script_ref)
            if self.script_queue and mode != "wait":
                await self.script_queue.put(script_ref)
                return CommandResult(True, f"Queued script: {script.name}")
            else:
                # Run directly (no queue configured, or 'wait' requested)
                success = await self.script_runner.run(script)
                status = "PASSED" if success else "FAILED"
                return CommandResult(success, f"Script {status}: {script.name}")
        except Exception as e:
            return CommandResult(False, f"Error: {e}")
