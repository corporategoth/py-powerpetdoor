# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Script running commands."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .base import ArgSpec, CommandResult, command
from ..scripting import script_completer

if TYPE_CHECKING:
    from ..scripting import Script, ScriptRunner


class ScriptsCommandsMixin:
    """Mixin providing script running commands."""

    script_runner: "ScriptRunner"
    script_queue: Optional[asyncio.Queue]
    _Script: type["Script"]
    _get_builtin_script: Callable[[str], "Script"]
    _list_builtin_scripts: Callable[[], list[tuple[str, str]]]

    def load_script(self, script_ref: str) -> "Script":
        """Load a script - auto-detect if it's a file path or built-in name."""
        path = Path(script_ref)
        if path.exists():
            return self._Script.from_file(path)
        else:
            return self._get_builtin_script(script_ref)

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
            )
        ],
    )
    async def run(self, script_ref: str) -> CommandResult:
        """Run a script (built-in name or file path)."""
        try:
            script = self.load_script(script_ref)
            if self.script_queue:
                await self.script_queue.put(script_ref)
                return CommandResult(True, f"Queued script: {script.name}")
            else:
                # Run directly
                success = await self.script_runner.run(script)
                status = "PASSED" if success else "FAILED"
                return CommandResult(success, f"Script {status}: {script.name}")
        except Exception as e:
            return CommandResult(False, f"Error: {e}")
