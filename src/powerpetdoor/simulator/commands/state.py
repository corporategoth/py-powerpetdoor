# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""State document commands: reset the door, and read its configuration."""

from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import t
from ..scripting import (
    describe_out_of_directory_remedy,
    path_escapes_directory,
    script_paths_allowed,
)
from ..state_io import (
    STATE_SUFFIXES,
    StateDocumentError,
    load_document,
    render_state_listing,
    state_documents_in,
)
from .base import ArgSpec, CommandResult, command, subcommand

if TYPE_CHECKING:
    from ..server import DoorSimulator


def describe_state_argument() -> str:
    """Help text for a state-file argument, honoring the path policy.

    Reads the same module-level policy ``describe_script_argument`` does:
    over the control channel, advertising a file path points the operator
    at a form the very next line of code refuses.
    """
    if script_paths_allowed():
        return "State document name or file path (JSON, or YAML with PyYAML)"
    return "State document name (paths are not accepted over the control channel)"


class StateCommandsMixin:
    """Mixin providing the ``reset`` and ``state`` commands."""

    simulator: "DoorSimulator"
    _states_dir: str | None
    _initial_state_document: dict | None
    _allow_script_paths: bool

    def load_state_document(self, ref: str) -> dict:
        """Load a state document by bare name or, when allowed, by path.

        The path policy is the script policy, not a second one: the control
        channel is unauthenticated, and a `reset` that could name any file
        on the host would reopen in a new place exactly the hole
        ``_load_script_restricted`` closes for scripts.
        """
        if not self._allow_script_paths:
            if "/" in ref or "\\" in ref or ref.startswith("."):
                raise StateDocumentError(
                    t(
                        "simulator.commands.state.paths_not_allowed_over_control",
                        "State file paths are not allowed over the control channel; "
                        "use a bare name from the states directory",
                    )
                )
            return self._load_state_by_name(ref)
        path = Path(ref)
        if path.exists():
            return load_document(path)
        return self._load_state_by_name(ref)

    def _load_state_by_name(self, name: str) -> dict:
        """Resolve a bare name against the states directory.

        Unlike scripts there are no built-in state documents to fall back
        on - a shipped one would be invented device configuration rather
        than observed - so a bare name with no ``--states-dir`` configured
        is simply unknown.
        """
        if self._states_dir:
            base = Path(self._states_dir).resolve()
            for suffix in STATE_SUFFIXES:
                candidate = base / f"{name}{suffix}"
                if not candidate.is_file():
                    continue
                if path_escapes_directory(candidate, base):
                    raise StateDocumentError(
                        t(
                            "simulator.commands.state.resolves_outside_cannot_load",
                            "State document '{name}' resolves outside {states_dir} "
                            "and cannot be loaded by name; {arg0}",
                            name=name,
                            states_dir=self._states_dir,
                            arg0=describe_out_of_directory_remedy(),
                        )
                    )
                return load_document(candidate.resolve())
        raise StateDocumentError(
            t(
                "simulator.commands.state.unknown_state_document",
                "Unknown state document: {name}{arg0}",
                name=name,
                arg0=(
                    ""
                    if self._states_dir
                    else " (no states directory configured; see --states-dir)"
                ),
            )
        )

    @subcommand("list", "states", [], "State documents `reset` can load")
    def list_states(self) -> CommandResult:
        """List the state documents ``reset`` accepts by bare name.

        Shares its renderer with ``ppd-simulator --list-states``, so the
        pre-flight surface and the running one cannot disagree.
        """
        return CommandResult(
            True,
            "\n".join(render_state_listing(self._states_dir)),
            {"states": state_documents_in(self._states_dir)},
        )

    @command(
        "reset",
        [],
        "Reset the door to its initial state",
        category="control",
        args=[
            ArgSpec(
                "document",
                "string",
                required=False,
                description=describe_state_argument,
            )
        ],
    )
    async def reset(self, document: str | None = None) -> CommandResult:
        """Reset the simulator to a known state.

        With no argument this restores the ``--initial-state`` document the
        simulator was started with, or the built-in defaults if there was
        none. With an argument it resets to that document instead.

        The door is stopped and parked closed, the sensors and any
        obstruction are cleared, and every setting is broadcast - a reset
        that left connected clients believing the old world would be worse
        than no reset at all.
        """
        try:
            doc = (
                self._initial_state_document or {}
                if document is None
                else self.load_state_document(document)
            )
        except StateDocumentError as exc:
            return CommandResult(False, str(exc))

        await self.simulator.reset_state(doc)
        if document is None:
            message = (
                t("simulator.commands.state.reset_to_initial", "Reset to initial state")
                if self._initial_state_document
                else t("simulator.commands.state.reset_to_defaults", "Reset to defaults")
            )
        else:
            message = t(
                "simulator.commands.state.reset_to_document",
                "Reset to state document: {document}",
                document=document,
            )
        return CommandResult(True, message)
