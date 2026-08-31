# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Scripting system for Power Pet Door simulator.

This module provides a YAML-based scripting system for automating
simulator behaviors. Scripts can be used for:
- Automated testing
- Reproducible test scenarios
- Demo/training purposes

Script format:
```yaml
name: "Pet goes outside"
description: "Simulates a pet triggering the inside sensor and going out"
steps:
  - action: trigger_sensor
    sensor: inside
  - action: wait
    seconds: 5
  - action: assert
    condition: door_status
    equals: DOOR_CLOSED
```

Available actions:
  - trigger_sensor: Trigger inside or outside sensor
Every action, its parameters and its description live in
:data:`_ACTION_PARAMS` and :data:`ACTION_DESCRIPTIONS`; ``schemas/script.schema.json``
is generated from them.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

if TYPE_CHECKING:
    from .server import DoorSimulator

from ..const import (
    DOOR_STATES_CLOSED,
    DOOR_STATES_CLOSING,
    DOOR_STATES_FULLY_OPEN,
    DOOR_STATES_OPENING,
)
from ..i18n import t
from ..sanitize import sanitize_text
from ..schedule import MAX_SCHEDULE_INDEX
from .coerce import CoercionError, coerce_bool, coerce_number, coerce_presence
from .engine import SENSOR_NAMES
from .notifications import NOTIFICATION_NAMES, NOTIFICATION_SETTINGS
from .state import WHOLE_DAY_END_HOUR, WHOLE_DAY_END_MINUTE, Schedule
from .values import (
    TOGGLEABLE,
    VALUE_NAMES,
    VALUES,
    WRITABLE,
    set_named_value,
    toggle_named_value,
)

logger = logging.getLogger(__name__)

#: Ceiling on a bare number a script may compare against. Large enough
#: for any real reading, small enough that `above: 1e400` cannot reach
#: `float("inf")` and make every later comparison meaningless.
MAX_SCRIPT_NUMBER = 1e12

#: Widest hold time (seconds) a script may park on the simulator. Matches
#: the wire ceiling (90000 centiseconds) that ``SET_HOLD_TIME`` enforces, so
#: no writer of this field can leave a value ``GET_SETTINGS`` chokes on.
MAX_SCRIPT_HOLD_TIME = 900.0

#: Longest delay (seconds) a script step may ask for: sensor durations,
#: ``wait`` and ``wait_for`` timeouts. These were the last unbounded script
#: numerics: ``duration: .nan`` reached ``asyncio.sleep`` and raised
#: ``ValueError("Invalid delay: NaN")`` inside a fire-and-forget task, so the
#: step silently did not happen, the sensor stayed active forever, the
#: operator got a stack trace - and the run still reported PASSED. One day is
#: far beyond any real simulation and still rejects ``inf``/``nan`` outright.
MAX_SCRIPT_DELAY = 86400.0

#: Largest ``repeat`` count. Bounded for the same reason every other script
#: number is: a runner that accepts ``times: 1e9`` has handed an unattended
#: CI job a hang, and the DSL's whole strictness posture is that a script
#: cannot quietly become something other than what it says.
MAX_SCRIPT_REPEAT = 10000


#: Script-file spellings of the boolean values that the shared wire
#: coercer does not know. These are *front-end* vocabulary (``set
#: safety_lock enabled``) and are deliberately not added to ``make_bool``,
#: which reads device data.
class ScriptError(Exception):
    """Error during script execution."""

    pass


class ScriptAssertionError(ScriptError):
    """Assertion in script failed."""

    pass


#: Backwards-compatible alias for :class:`ScriptAssertionError`.
AssertionFailed = ScriptAssertionError

# The accepted vocabulary of each `Unknown X` message below.
#
# Five of the DSL's seven "unknown name" errors named no alternatives at
# all, and the sharpest of them - `Unknown assertion condition:
# door_closed` - is a name the runner recognises *for the other action*.
# The script DSL is the **CI** front end: these messages are read in a
# build log, with no terminal to experiment in, which is exactly the
# context where naming the alternatives is worth the most.
#
# Each tuple is pinned against the `== "..."` chain it describes by
# `tests/simulator/test_scripting.py::TestUnknownNameErrorsNameTheAlternatives`,
# so the message and the implementation cannot drift.

#: How a condition's value may be compared with an expectation. Exactly
#: one per condition: two would be two answers to one question.
#:
#: ``equals``/``not_equals`` compare as text, case-insensitively, so a
#: door status, a number and a boolean all work. The other four are
#: numeric and refuse a condition that is not a number, rather than
#: silently comparing "DOOR_CLOSED" against 25.
COMPARISON_EQUALITY: tuple[str, ...] = ("equals", "not_equals")
COMPARISON_NUMERIC: tuple[str, ...] = ("above", "below", "at_least", "at_most")
COMPARISONS: tuple[str, ...] = (*COMPARISON_EQUALITY, *COMPARISON_NUMERIC)

#: The three ways ``if`` and ``repeat`` may state their condition. Exactly
#: one per step: two would be two answers to one question, which is a typo
#: rather than a refinement.
IF_CONDITION_FORMS: tuple[str, ...] = ("condition", "conditions", "any")

#: The door conditions that are really status sets, so a wait on one can
#: be signalled by the engine instead of polled.
_DOOR_CONDITION_STATUSES: dict[str, tuple[str, ...]] = {
    "door_open": tuple(sorted(DOOR_STATES_FULLY_OPEN)),
    "door_closed": tuple(sorted(DOOR_STATES_CLOSED)),
    "door_closing": tuple(sorted(DOOR_STATES_CLOSING)),
    "door_opening": tuple(sorted(DOOR_STATES_OPENING)),
}

#: Step keys holding a nested list of steps. Parsed recursively at load
#: time so a bad action inside an untaken branch still fails loudly.
BLOCK_PARAMS: tuple[str, ...] = ("steps", "then", "else")

#: What ``wait_for`` does when its timeout expires. ``fail`` is the default
#: and the historical behaviour; ``continue`` leaves the condition false
#: and lets the next step decide, which is how a script branches on
#: "it did not happen" without a separate mechanism for it.
ON_TIMEOUT_FAIL = "fail"
ON_TIMEOUT_CONTINUE = "continue"
ON_TIMEOUT_CHOICES: tuple[str, ...] = (ON_TIMEOUT_FAIL, ON_TIMEOUT_CONTINUE)

#: Every condition name, and what kind of value it reads.
#:
#: There used to be a second, parallel vocabulary of "fused" names -
#: ``power_on`` meaning ``power`` + ``equals: on`` - carried in two extra
#: tables with special-case code to expand them. Twenty of the twenty-two
#: were pure duplication. The two that were not (``door_open`` and
#: ``door_closing`` each cover several statuses) are ordinary boolean
#: conditions here instead, so there is one kind of name and one code path.
#:
#: ``door_open``/``door_closed``/``door_closing`` and ``position`` mirror
#: the library's own :class:`~powerpetdoor.door.PowerPetDoor` properties
#: rather than inventing a simulator-only notion of where the door is.
#: Conditions that are computed rather than read from a named value.
_COMPUTED_CONDITIONS: tuple[str, ...] = (
    "door_closed",
    "door_closing",
    "door_open",
    "door_opening",
    *(f"notified_{name}" for name in NOTIFICATION_NAMES),
)

#: Every condition: every named value, plus the computed ones. Derived, so
#: a value added to the registry is assertable without touching this file.
ASSERT_CONDITIONS: tuple[str, ...] = tuple(sorted((*VALUE_NAMES, *_COMPUTED_CONDITIONS)))

#: Alias kept so the three constructs can be documented as taking the same
#: vocabulary - they now literally do.
CONDITION_NAMES: tuple[str, ...] = ASSERT_CONDITIONS
WAIT_FOR_CONDITIONS: tuple[str, ...] = ASSERT_CONDITIONS

#: The value ``set`` reads as "invert this", rather than as a state.
TOGGLE_KEYWORD = "toggle"

#: Everything ``set`` accepts, from the value registry.
SET_SETTINGS: tuple[str, ...] = WRITABLE

#: The subset ``set <name> toggle`` accepts - the yes/no ones. A value
#: holds a number or a string and has nothing to invert.
TOGGLE_SETTINGS: tuple[str, ...] = TOGGLEABLE

#: The parameters each script action understands.
#:
#: No step parameter used to be validated at all, and the progress log
#: renders ``step.params`` verbatim - so ``wait: {duration: 8}`` (``duration``
#: is a real parameter name in this DSL, for ``inside``/``outside``) logged
#: ``Step 1: wait(duration=8)``, waited the 1.0 s default instead of 8, and
#: reported PASSED. The one piece of output an author inspects actively
#: confirmed the typo had been accepted, and a script written to wait out a
#: door cycle silently became a no-op that still exits 0. Every other
#: misspelling class in this DSL - action, setting, condition, and sensor -
#: fails loudly; parameters do too.
#:
#: Keys must stay in step with the ``action == "..."`` chain in
#: :meth:`ScriptRunner._execute_step`; that is pinned by
#: ``test_every_executed_action_declares_its_parameters``.
_ACTION_PARAMS: dict[str, frozenset[str]] = {
    "trigger": frozenset({"sensor"}),
    "obstruction": frozenset({"duration", "state"}),
    "inside": frozenset({"duration", "state"}),
    "outside": frozenset({"duration", "state"}),
    "open": frozenset(),
    "cycle": frozenset(),
    "close": frozenset(),
    "wait": frozenset({"seconds"}),
    "wait_for": frozenset(
        {"condition", "conditions", "any", *COMPARISONS, "timeout", "on_timeout"}
    ),
    "if": frozenset({"condition", "conditions", "any", *COMPARISONS, "then", "else"}),
    "repeat": frozenset({"times", "steps", "condition", "conditions", "any", *COMPARISONS}),
    "reset": frozenset({"initial_state"}),
    "set": frozenset({"name", "value"}),
    "toggle": frozenset(),
    "assert": frozenset({"condition", "conditions", "any", *COMPARISONS}),
    "log": frozenset({"message"}),
    "add_schedule": frozenset({"index", "enabled"}),
    "remove_schedule": frozenset({"index"}),
    "enable_schedule": frozenset({"index", "state"}),
    "clear_schedules": frozenset(),
    "battery": frozenset({"percent", "value"}),
    "notify": frozenset({"name", "state"}),
}

#: One line per action, for anything that has to explain the DSL to a
#: human: the generated JSON Schema, and an editor showing a tooltip.
#: ``test_every_action_is_described`` pins that this covers
#: :data:`_ACTION_PARAMS` exactly, so an action cannot arrive undescribed.
ACTION_DESCRIPTIONS: dict[str, str] = {
    "trigger": "A pet walks through a sensor, rather than standing at it.",
    "inside": "Put a pet at the inside sensor, for a moment or indefinitely.",
    "outside": "Put a pet at the outside sensor. Mutually exclusive with inside.",
    "obstruction": "Place or clear a physical blockage in the doorway.",
    "open": "Open the door and hold it open until something closes it.",
    "cycle": "Open, hold for hold_time, then close - the door button.",
    "close": "Close the door.",
    "toggle": "Open the door if closed, close it if open; nothing mid-travel.",
    "wait": "Pause for a number of seconds.",
    "wait_for": "Wait until a condition holds, or time out.",
    "if": "Run one block of steps or another, depending on a condition.",
    "repeat": "Run a block of steps a number of times, or while a condition holds.",
    "reset": "Return the door to its defaults, or to a named state document.",
    "set": "Set a named value. `toggle` as the value inverts a yes/no one.",
    "assert": "Fail the script unless a condition holds.",
    "log": "Print a message to the script log.",
    "battery": "Set the battery percentage.",
    "notify": "Switch a notification on or off.",
    "add_schedule": "Store a schedule allowing both sensors, every day, all day.",
    "remove_schedule": "Delete a schedule.",
    "enable_schedule": "Switch a stored schedule on or off.",
    "clear_schedules": "Delete every schedule.",
}

#: One line per parameter name. Parameters are shared across actions -
#: `index` means the same thing to every schedule action - so this is
#: keyed by name rather than by (action, name).
PARAM_DESCRIPTIONS: dict[str, str] = {
    "sensor": "Which sensor: inside or outside.",
    "state": "on/off/true/false. Omit to toggle.",
    "duration": "Seconds to hold. 0 means until cleared.",
    "seconds": "How long to wait.",
    "name": "The value, or notification, to act on.",
    "value": "The new value. `toggle` inverts a yes/no one.",
    "index": "Schedule slot.",
    "enabled": "Whether the schedule is active.",
    "percent": "Battery charge, 0-100.",
    "message": "Text to log.",
    "times": "How many iterations.",
    "steps": "The block to run.",
    "then": "Steps to run when the condition holds.",
    "else": "Steps to run when it does not.",
    "condition": "The name to test.",
    "conditions": "Several conditions, all of which must hold.",
    "any": "Several conditions, any one of which may hold.",
    "timeout": "Seconds to wait before giving up.",
    "on_timeout": "What a timeout means: fail the script, or continue.",
    "initial_state": "A state document to reset to, by name or path.",
    "equals": "Expected value.",
    "not_equals": "Value it must not be.",
    "above": "Must be greater than this.",
    "below": "Must be less than this.",
    "at_least": "Must be at least this.",
    "at_most": "Must be at most this.",
}

#: Documentation-only keys accepted on **any** step and read by nothing.
#:
#: Making unknown parameters an error is right - a typo'd real parameter
#: must fail loudly - but it also broke existing user scripts that annotate
#: their steps, which is an ordinary thing for a YAML author to do:
#:
#: .. code-block:: yaml
#:
#:     - action: wait
#:       seconds: 1
#:       note: let the door settle
#:
#: Allowing a *documented, closed* set keeps the strictness where it
#: matters (``duration:`` on a ``wait`` is still a hard failure) while
#: giving annotations a spelling that is guaranteed not to collide with a
#: parameter name. None of these appears in :data:`_ACTION_PARAMS`, which
#: ``test_annotation_keys_never_collide_with_a_real_parameter`` pins.
STEP_ANNOTATION_KEYS = frozenset({"comment", "description", "note"})

#: The complete set of keys :meth:`Script.from_yaml` reads. Anything else at
#: the top level of a script file is a misspelling and is refused, in the
#: same shape as every other unknown name in this DSL.
SCRIPT_TOP_LEVEL_KEYS = frozenset({"description", "name", "steps"})


@dataclass
class ScriptStep:
    """A single step in a script."""

    action: str
    params: dict = field(default_factory=dict)
    line_number: int = 0

    def __str__(self) -> str:
        """Render for the progress log, summarising nested blocks.

        The log is the one piece of output a script author inspects
        actively; a `repeat` that dumped the dataclass repr of every step
        it holds buried the run in a single unreadable line.
        """
        if not self.params:
            return self.action
        parts = []
        for key, value in self.params.items():
            if key in BLOCK_PARAMS and isinstance(value, list):
                count = len(value)
                parts.append(f"{key}=[{count} step{'' if count == 1 else 's'}]")
            else:
                parts.append(f"{key}={value}")
        return f"{self.action}({', '.join(parts)})"


@dataclass
class Script:
    """A simulator script."""

    name: str
    steps: list[ScriptStep]
    description: str = ""
    source_file: str | None = None

    @classmethod
    def from_yaml(cls, content: str, source_file: str | None = None) -> "Script":
        """Parse a script from YAML content."""
        if not YAML_AVAILABLE:
            raise ScriptError(
                t(
                    "simulator.scripting.pyyaml_required_script_support_pip",
                    "PyYAML is required for script support: pip install pyyaml",
                )
            )

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as err:
            raise ScriptError(
                t("simulator.scripting.invalid_script_yaml", "Invalid script YAML: {err}", err=err)
            ) from err
        if not isinstance(data, dict):
            raise ScriptError(
                t(
                    "simulator.scripting.script_must_yaml_dictionary",
                    "Script must be a YAML dictionary",
                )
            )

        # Top-level keys have the worst blast radius of any misspelling
        # class in this DSL: a step-parameter typo loses one step, `stpes:`
        # loses the entire file while still printing `>>> Script PASSED` and
        # exiting 0. Every other class - action, sensor, condition, setting,
        # step parameter - already fails loudly in this
        # `Unknown X: y. Use: ...` shape. `steps: []` legitimately means
        # "no steps", so the check is on unknown keys and not on emptiness.
        unknown = sorted(set(data) - SCRIPT_TOP_LEVEL_KEYS)
        if unknown:
            raise ScriptError(
                t(
                    "simulator.scripting.unknown_top_level_key_s",
                    "Unknown top-level key(s): {arg0}. Use: {arg1}",
                    arg0=", ".join(str(key) for key in unknown),
                    arg1=", ".join(sorted(SCRIPT_TOP_LEVEL_KEYS)),
                )
            )

        name = data.get("name", "Unnamed Script")
        description = data.get("description", "")
        steps_data = data.get("steps", [])

        if not isinstance(steps_data, list):
            raise ScriptError(t("simulator.scripting.steps_must_list", "'steps' must be a list"))

        steps = cls._parse_steps(steps_data)

        return cls(
            name=name,
            description=description,
            steps=steps,
            source_file=source_file,
        )

    @staticmethod
    def _at_step(i: int, message: str) -> str:
        """Prefix a load-time complaint with the step it came from.

        The executor has always said "Script error at step 7"; the load
        check took the index and dropped it, so a 35-step file with
        several `wait` steps answered "Unknown parameter(s) for wait"
        and left the reader to find which one.
        """
        return t(
            "simulator.scripting.step_prefix",
            "Step {i}: {message}",
            i=i,
            message=message,
        )

    @classmethod
    def _validate_step(cls, action: object, params: dict, i: int) -> None:
        """Refuse an unknown action or parameter while LOADING.

        Recursing into the blocks was only half of the promise this DSL
        makes. The steps inside an untaken `else` were built into
        :class:`ScriptStep` objects and never looked at again, so a
        misspelled action there was found the first time that branch was
        reached - which for a branch that is never taken is never. A
        script full of nonsense ran to completion and reported PASSED,
        and scripts gate CI by exit code.

        The same two checks the executor makes, in the same words, so a
        name is right or wrong at one place regardless of when it is
        noticed.
        """
        # `str()` rather than an isinstance guard: YAML will hand back an
        # int for `action: 42`, which is truthy and so survives the
        # missing-action check above. Naming it as an unknown action is
        # both the honest answer and better than the AttributeError the
        # executor used to raise on it.
        name = str(action).lower().replace("-", "_")
        known_params = _ACTION_PARAMS.get(name)
        if known_params is None:
            raise ScriptError(
                cls._at_step(
                    i,
                    t(
                        "simulator.scripting.unknown_action_use",
                        "Unknown action: {action}. Use: {arg0}",
                        action=name,
                        arg0=", ".join(sorted(_ACTION_PARAMS)),
                    ),
                )
            )
        # NOT minus BLOCK_PARAMS: `_ACTION_PARAMS` already lists `then`
        # and `else` under `if`, and `steps` under `repeat`, so subtracting
        # them again only excused the actions that have no block - `close`
        # with a `then:` passed the load check and was refused at run time,
        # making this weaker than the check it claims to mirror.
        unexpected = sorted(set(params) - known_params - STEP_ANNOTATION_KEYS)
        if unexpected:
            accepted = (
                f"Use: {', '.join(sorted(known_params))}"
                if known_params
                else f"{name} takes no parameters"
            )
            raise ScriptError(
                cls._at_step(
                    i,
                    t(
                        "simulator.scripting.unknown_parameter_s_plus_annotations",
                        "Unknown parameter(s) for {action}: {arg0}. {accepted} (plus the annotations {annotations})",
                        action=name,
                        arg0=", ".join(unexpected),
                        accepted=accepted,
                        annotations=", ".join(sorted(STEP_ANNOTATION_KEYS)),
                    ),
                )
            )

    @classmethod
    def _parse_steps(cls, steps_data: object) -> list["ScriptStep"]:
        """Parse a step list, recursing into the blocks `if` and `repeat` hold.

        Nested lists are parsed into :class:`ScriptStep` objects here rather
        than at execution time, so a misspelled action inside an `else:`
        branch fails when the script is *loaded* - the same moment every
        other misspelling in this DSL fails - instead of only when that
        branch happens to be taken.
        """
        if not isinstance(steps_data, list):
            raise ScriptError(t("simulator.scripting.steps_must_list", "'steps' must be a list"))

        steps: list[ScriptStep] = []
        for i, step_data in enumerate(steps_data, 1):
            if isinstance(step_data, str):
                # Simple action with no params: "- close". Validated like
                # any other: the first version of this checked only the
                # mapping form, so `- clsoe` in an untaken branch still
                # loaded, ran to completion and reported PASSED - the very
                # thing the check was added to stop.
                cls._validate_step(step_data, {}, i)
                steps.append(ScriptStep(action=step_data, line_number=i))
            elif isinstance(step_data, dict):
                params = dict(step_data)
                action = params.pop("action", None)
                if not action:
                    raise ScriptError(
                        t(
                            "simulator.scripting.step_missing_action_field",
                            "Step {i}: missing 'action' field",
                            i=i,
                        )
                    )
                for block in BLOCK_PARAMS:
                    if block in params:
                        params[block] = cls._parse_steps(params[block])
                cls._validate_step(action, params, i)
                steps.append(ScriptStep(action=action, params=params, line_number=i))
            else:
                raise ScriptError(
                    t(
                        "simulator.scripting.step_invalid_step_format",
                        "Step {i}: invalid step format",
                        i=i,
                    )
                )
        return steps

    @classmethod
    def from_file(cls, path: Path) -> "Script":
        """Load a script from a YAML file.

        Raises ScriptError for unreadable files and invalid YAML alike, so
        callers only need to handle one error type for loader failures.
        """
        try:
            content = path.read_text()
        except OSError as err:
            raise ScriptError(
                t(
                    "simulator.scripting.cannot_read_script_file",
                    "Cannot read script file '{path}': {err}",
                    path=path,
                    err=err,
                )
            ) from err
        return cls.from_yaml(content, source_file=str(path))

    @classmethod
    def from_simple_commands(cls, commands: list[str], name: str = "Inline Script") -> "Script":
        """Create a script from simple command strings.

        Commands use a simple format:
            trigger inside
            wait 2
            pet outside on
            wait_for door_closed 10
            set battery 50
            set power toggle
            assert door_status DOOR_CLOSED
        """
        steps = []
        for i, cmd in enumerate(commands, 1):
            parts = cmd.strip().split()
            if not parts:
                continue

            action = parts[0]
            params: dict = {}

            if action == "trigger":
                params["sensor"] = parts[1] if len(parts) > 1 else "inside"
            elif action == "wait":
                params["seconds"] = float(parts[1]) if len(parts) > 1 else 1.0
            elif action == "wait_for":
                params["condition"] = parts[1] if len(parts) > 1 else "door_closed"
                params["timeout"] = float(parts[2]) if len(parts) > 2 else 30.0
            elif action == "if":
                params["condition"] = parts[1] if len(parts) > 1 else ""
            elif action == "set":
                params["name"] = parts[1] if len(parts) > 1 else ""
                params["value"] = parts[2] if len(parts) > 2 else ""
            elif action == "assert":
                params["condition"] = parts[1] if len(parts) > 1 else ""
                if len(parts) > 2:
                    params["equals"] = parts[2]
            elif action == "log":
                params["message"] = " ".join(parts[1:])
            elif action in SENSOR_NAMES:
                if len(parts) > 1:
                    key = "duration" if parts[1].replace(".", "").isdigit() else "state"
                    params[key] = parts[1]
            elif action == "obstruction":
                if len(parts) > 1:
                    params["state"] = parts[1]
            elif action == "enable_schedule":
                params["index"] = parts[1] if len(parts) > 1 else "1"
                if len(parts) > 2:
                    params["state"] = parts[2]
            elif action == "notify":
                params["name"] = parts[1] if len(parts) > 1 else ""
                if len(parts) > 2:
                    params["state"] = parts[2]
            elif action in ("open", "cycle", "close", "toggle"):
                pass  # No params needed
            elif action == "add_schedule":
                params["index"] = int(parts[1]) if len(parts) > 1 else 1
            elif action == "remove_schedule":
                params["index"] = int(parts[1]) if len(parts) > 1 else 1

            steps.append(ScriptStep(action=action, params=params, line_number=i))

        return cls(name=name, steps=steps)


class ScriptRunner:
    """Executes scripts against a simulator.

    Runs are serialized: one simulator drives one script at a time. Two
    scripts running concurrently would fight over the same door (a queued
    script and a ``run ... wait`` used to interleave and fail each other's
    assertions), and ``stop()``'s state is per-runner, not per-run.
    """

    def __init__(
        self,
        simulator: "DoorSimulator",
        initial_state_document: dict | None = None,
        load_state_document: Callable[[str], dict] | None = None,
    ):
        """
        Args:
            simulator: The simulator to drive.
            initial_state_document: What a bare ``reset`` step restores.
            load_state_document: Resolves the name a ``reset`` step gives.
                Supplied by the command handler, which owns the path
                policy, so a script cannot reach a file the control
                channel would refuse.
        """
        self.simulator = simulator
        self.initial_state_document = initial_state_document
        self.load_state_document = load_state_document
        self.running = False
        self.current_script: str | None = None
        self._lock = asyncio.Lock()
        self._stop_requested = False
        self._stop_event = asyncio.Event()

    @property
    def busy(self) -> bool:
        """Whether a script is currently executing."""
        return self._lock.locked()

    @property
    def stop_requested(self) -> bool:
        """Whether a stop has been requested for the running script.

        ``stop`` takes effect at a step boundary, so the operator needs a
        way to tell a registered stop from one that never arrived.
        """
        return self._stop_requested

    async def run(
        self,
        script: Script,
        verbose: bool = True,
        *,
        queue_if_busy: bool = True,
        on_start: Callable[[], bool] | None = None,
    ) -> bool:
        """Execute a script, waiting for any in-flight script to finish.

        Args:
            script: The script to run.
            verbose: Log each step.
            queue_if_busy: When True (the default, used by the script
                queue), wait for the running script to finish. When False,
                refuse immediately rather than queue - callers that report
                a synchronous pass/fail must not silently block.
            on_start: Called once the run lock is held and this script is
                about to become the running one. The queue consumer uses
                it to stop counting the run as pending. Returning False
                abandons the run without executing a step: waiting for the
                lock is the window in which ``stop all`` can drop the
                entry, and starting it afterwards would run exactly what
                the operator just discarded.

        Returns:
            True if all steps (including assertions) passed. False for an
            abandoned run, which never touched the door.

        Raises:
            ScriptError: If another script is running and ``queue_if_busy``
                is False.
        """
        if not queue_if_busy and self.busy:
            raise ScriptError(
                t(
                    "simulator.scripting.another_script_already_running",
                    "Another script is already running: {current_script}",
                    current_script=self.current_script,
                )
            )
        async with self._lock:
            if on_start is not None and not on_start():
                return False
            self.current_script = script.name
            try:
                return await self._run_steps(script, verbose)
            finally:
                self.current_script = None

    async def _run_steps(self, script: Script, verbose: bool) -> bool:
        """Execute every step of ``script`` (caller holds the run lock)."""
        self.running = True
        self._stop_requested = False
        self._stop_event.clear()

        if verbose:
            # A YAML script is an untrusted input in this project's threat
            # model ("here is a repro script for the bug I filed"), and
            # PyYAML's "\e" escape puts a real ESC in a file that looks
            # clean. Sanitize at the source, exactly as the protocol
            # channel does.
            logger.info(
                t(
                    "simulator.scripting.running_script",
                    "Running script: {arg0}",
                    arg0=sanitize_text(script.name),
                )
            )
            if script.description:
                logger.info(
                    t(
                        "simulator.scripting.text",
                        "  {arg0}",
                        arg0=sanitize_text(script.description),
                    )
                )

        try:
            for step in script.steps:
                if self._stop_requested:
                    logger.info(
                        t(
                            "simulator.scripting.script_stopped_by_request",
                            "Script stopped by request",
                        )
                    )
                    return False

                if verbose:
                    logger.info(
                        t(
                            "simulator.scripting.step",
                            "  Step {line_number}: {arg0}",
                            line_number=step.line_number,
                            arg0=sanitize_text(step),
                        )
                    )

                await self._execute_step(step)

            if self._stop_requested:
                # A stop that landed during the *last* step used to be
                # discarded: the loop simply ended and the run reported
                # PASSED with exit code 0, the opposite of what `stop`
                # documents and the one signal a CI abort relies on.
                logger.info(
                    t("simulator.scripting.script_stopped_by_request", "Script stopped by request")
                )
                return False

            if verbose:
                logger.info(
                    t(
                        "simulator.scripting.script_completed_successfully",
                        "Script '{arg0}' completed successfully",
                        arg0=sanitize_text(script.name),
                    )
                )
            return True

        except ScriptAssertionError as e:
            logger.error(
                t(
                    "simulator.scripting.assertion_failed_step",
                    "Assertion failed at step {line_number}: {arg0}",
                    line_number=step.line_number,
                    arg0=sanitize_text(e),
                )
            )
            return False
        except ScriptError as e:
            logger.error(
                t(
                    "simulator.scripting.script_error_step",
                    "Script error at step {line_number}: {arg0}",
                    line_number=step.line_number,
                    arg0=sanitize_text(e),
                )
            )
            return False
        except Exception as e:
            logger.error(
                t(
                    "simulator.scripting.unexpected_error_step",
                    "Unexpected error at step {line_number}: {arg0}",
                    line_number=step.line_number,
                    arg0=sanitize_text(e),
                )
            )
            return False
        finally:
            self.running = False

    def stop(self):
        """Request the script to stop."""
        self._stop_requested = True
        self._stop_event.set()

    async def _execute_step(self, step: ScriptStep):
        """Execute a single script step."""
        action = step.action.lower().replace("-", "_")
        params = step.params

        # An unknown *action* is still caught by the chain's final `else`,
        # which stays authoritative for that; this only rejects unknown
        # parameters of an action we do recognize.
        known_params = _ACTION_PARAMS.get(action)
        if known_params is not None:
            unexpected = sorted(set(params) - known_params - STEP_ANNOTATION_KEYS)
            if unexpected:
                # "Use: none" for a no-parameter action read as an
                # instruction to pass the literal token `none`.
                accepted = (
                    f"Use: {', '.join(sorted(known_params))}"
                    if known_params
                    else f"{action} takes no parameters"
                )
                annotations = ", ".join(sorted(STEP_ANNOTATION_KEYS))
                raise ScriptError(
                    t(
                        "simulator.scripting.unknown_parameter_s_plus_annotations",
                        "Unknown parameter(s) for {action}: {arg0}. {accepted} (plus the annotations {annotations})",
                        action=action,
                        arg0=", ".join(unexpected),
                        accepted=accepted,
                        annotations=annotations,
                    )
                )

        if action == "trigger":
            # `sensor:` was the only user-supplied *name* in this DSL that
            # was not validated, and it failed in the worst direction: an
            # unrecognised name matched none of the engine's gates, so a
            # one-character typo opened the door with both sensors disabled
            # and the safety lock on - and still reported PASSED, which over
            # `ctl run <name> wait` is a green CI exit code. Every other
            # misspelling class here (action, setting, condition) already
            # fails loudly.
            sensor = params.get("sensor", "inside")
            if sensor not in SENSOR_NAMES:
                raise ScriptError(
                    t(
                        "simulator.scripting.unknown_sensor_use",
                        "Unknown sensor: {sensor}. Use: {arg0}",
                        sensor=sensor,
                        arg0=", ".join(SENSOR_NAMES),
                    )
                )
            self.simulator.trigger_sensor(sensor)

        elif action == "obstruction":
            # `state: off` is the explicit clear. Saying `obstruction`
            # twice also clears, matching the CLI, but in a script - read
            # later, out of order - the explicit form is the honest one.
            present, duration = self._presence(params, default=None)
            if duration is not None:
                self.simulator.simulate_obstruction(duration)
            elif present is None:
                self.simulator.simulate_obstruction()
            elif present:
                self.simulator.simulate_obstruction(0)
            else:
                self.simulator.clear_obstruction()

        elif action in SENSOR_NAMES:
            # A sensor held active *is* pet presence - a collar sitting in
            # range - so there is no separate `pet` action: `inside 0` and
            # `pet on` were the same thing, except that `pet on` never
            # opened a closed door, which a real collar at the sensor does.
            #
            # `duration: 0` toggles it indefinitely; `state:` says which
            # way explicitly, because a script is read out of order and
            # "toggle" is only unambiguous with the whole run in view.
            present, duration = self._presence(params, default=0.5)
            if duration is None:
                self.simulator.hold_sensor(action, present)
            else:
                self.simulator.activate_sensor(action, duration)

        elif action == "open":
            await self.simulator.open_door(hold=True)

        elif action == "cycle":
            await self.simulator.open_door(hold=False)

        elif action == "close":
            await self.simulator.close_door()

        elif action == "wait":
            seconds = self._script_number(
                params.get("seconds", 1.0), "seconds", 0, MAX_SCRIPT_DELAY
            )
            # Raced against the stop event, so `stop` during a long wait
            # takes effect straight away instead of at the end of the
            # sleep. This also shrinks the window in which a stop lands
            # during the final step and would otherwise be discarded.
            await self._sleep_or_stop(seconds)

        elif action == "wait_for":
            timeout = self._script_number(
                params.get("timeout", 30.0), "timeout", 0, MAX_SCRIPT_DELAY
            )
            on_timeout = self._script_choice(
                params.get("on_timeout", ON_TIMEOUT_FAIL), "on_timeout", ON_TIMEOUT_CHOICES
            )
            await self._wait_for_condition(params, timeout, on_timeout)

        elif action == "if":
            branch = params.get("then", []) if self._if_holds(params) else params.get("else", [])
            await self._execute_block(branch)

        elif action == "repeat":
            await self._execute_repeat(params)

        elif action == "reset":
            await self.simulator.reset_state(
                self._resolve_state_document(params.get("initial_state"))
            )

        elif action == "set":
            name = params.get("name", "")
            value = params.get("value", "")
            self._set_value(name, value)

        elif action == "toggle":
            # The door, exactly as the CLI's `toggle` means the door. A
            # setting is inverted with `set <name> toggle`, mirroring the
            # CLI's `power toggle` / `auto toggle` subcommands.
            await self.simulator.toggle_door()

        elif action == "assert":
            self._assert_condition(params)

        elif action == "log":
            message = params.get("message", "")
            # Script-supplied text reaching an operator's terminal: same
            # rule as the wire channel.
            logger.info(
                t("simulator.scripting.script", "  [SCRIPT] {arg0}", arg0=sanitize_text(message))
            )

        elif action == "add_schedule":
            # Bounded like every other writer of a schedule index: a script
            # could otherwise allocate a slot the wire path itself rejects.
            index = int(self._script_number(params.get("index", 1), "index", 0, MAX_SCHEDULE_INDEX))
            enabled = self._script_bool(params.get("enabled", True))
            # A schedule that allows BOTH sensors 24/7, so a script that
            # adds one behaves the same at every time of day. Spelled the way
            # the device itself spells a full day - 00:00-23:59 - rather than
            # midnight-to-midnight (see Schedule.is_sensor_allowed, which
            # treats 23:59 as end-of-day for exactly this reason).
            schedule = Schedule(
                index=index,
                enabled=enabled,
                days_of_week=[True] * 7,  # All days
                inside=True,  # Allow inside sensor
                outside=True,  # Allow outside sensor
                start_hour=0,
                start_min=0,
                # 24:00, not 23:59: the engine is strictly
                # `start <= now < end`, so 23:59 would leave the sensor off
                # for the last minute of every day and a script that means
                # "always" would behave differently at 23:59 than at 23:58.
                end_hour=WHOLE_DAY_END_HOUR,
                end_min=WHOLE_DAY_END_MINUTE,
            )
            self.simulator.add_schedule(schedule)

        elif action == "remove_schedule":
            index = int(self._script_number(params.get("index", 1), "index", 0, MAX_SCHEDULE_INDEX))
            self.simulator.remove_schedule(index)

        elif action == "enable_schedule":
            # `schedule enable`/`schedule disable` at the prompt. Stored
            # back through add_schedule, which is the one writer, so the
            # change is logged and announced like any other.
            index = int(self._script_number(params.get("index", 1), "index", 0, MAX_SCHEDULE_INDEX))
            stored = self.simulator.get_schedule(index)
            if stored is None:
                raise ScriptError(
                    t(
                        "simulator.scripting.schedule_not_found",
                        "Schedule #{index} not found",
                        index=index,
                    )
                )
            stored.enabled = self._script_bool(params.get("state", True))
            self.simulator.add_schedule(stored)

        elif action == "clear_schedules":
            # `schedule clear` at the prompt, through the same wholesale
            # writer, so every departure is announced.
            self.simulator.set_schedules([])

        elif action == "notify":
            # Mirrors the CLI's `notify <name> on|off`. A script that wants
            # to wait on a notification has to be able to switch it on.
            name = str(params.get("name", "")).lower().replace("-", "_")
            if name not in NOTIFICATION_SETTINGS:
                raise ScriptError(
                    t(
                        "simulator.scripting.unknown_notification_use",
                        "Unknown notification: {name}. Use: {arg0}",
                        name=name,
                        arg0=", ".join(NOTIFICATION_NAMES),
                    )
                )
            VALUES[f"notify_{name}"].apply(
                self.simulator, self._script_bool(params.get("state", True))
            )

        elif action == "battery":
            percent = self._script_number(
                params.get("percent", params.get("value", 50)), "battery", 0, 100
            )
            self.simulator.set_battery(int(percent))

        else:
            raise ScriptError(
                t(
                    "simulator.scripting.unknown_action_use",
                    "Unknown action: {action}. Use: {arg0}",
                    action=action,
                    arg0=", ".join(sorted(_ACTION_PARAMS)),
                )
            )

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, returning early if :meth:`stop` is requested meanwhile.

        A plain ``asyncio.sleep`` made ``wait`` uninterruptible, so the
        window in which a ``stop`` request sits unobserved was as long as
        the wait itself - and if that was the final step, the request was
        discarded entirely.
        """
        if self._stop_requested:
            return
        stopper = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({stopper}, timeout=seconds)
        finally:
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)

    def _condition_value(self, condition: str) -> object:
        """The current value of a named condition, in its natural type.

        One reader for ``assert``, ``wait_for``, ``if`` and ``repeat``,
        which is the point of there being one vocabulary. Values are
        returned as bools, numbers and strings rather than pre-rendered
        text, so the numeric comparisons can actually compare numbers.

        Raises:
            ScriptError: If the name is not a condition.
        """
        state = self.simulator.state
        # Every named value, plus the computed conditions a value table
        # cannot hold: the door-travel predicates and the notification
        # counters. One table means anything settable is also assertable.
        readers: dict[str, Callable[[], object]] = {
            name: cast("Callable[[], object]", lambda spec=spec: spec.get(state))
            for name, spec in VALUES.items()
        }
        readers.update(
            {
                "door_open": lambda: state.door_status in DOOR_STATES_FULLY_OPEN,
                "door_closed": lambda: state.door_status in DOOR_STATES_CLOSED,
                "door_closing": lambda: state.door_status in DOOR_STATES_CLOSING,
                "door_opening": lambda: state.door_status in DOOR_STATES_OPENING,
                **{
                    f"notified_{name}": cast(
                        "Callable[[], object]", lambda n=name: state.notifications.get(n, 0)
                    )
                    for name in NOTIFICATION_NAMES
                },
            }
        )
        reader = readers.get(condition)
        if reader is None:
            raise ScriptError(
                t(
                    "simulator.scripting.unknown_condition_use",
                    "Unknown condition: {condition}. Use: {arg1}",
                    condition=condition,
                    arg1=", ".join(CONDITION_NAMES),
                )
            )
        return reader()

    def _condition_holds(self, condition: object, params: dict) -> bool:
        """Whether a condition currently satisfies its comparison.

        With no comparison at all a boolean condition means "is true" -
        ``condition: door_closed`` reads the way it should. Anything else
        needs to say what it is being compared against, because there is no
        sensible default for a number or a status string.
        """
        name = str(condition).lower().replace("-", "_")
        actual = self._condition_value(name)

        operators = [key for key in COMPARISONS if key in params]
        if len(operators) > 1:
            raise ScriptError(
                t(
                    "simulator.scripting.one_comparison_only",
                    "'{condition}' has more than one comparison ({arg0}); use one",
                    condition=name,
                    arg0=", ".join(operators),
                )
            )
        if not operators:
            if isinstance(actual, bool):
                return actual
            raise ScriptError(
                t(
                    "simulator.scripting.condition_needs_a_comparison",
                    "'{condition}' is not a yes/no condition, so it needs one of: {arg0}",
                    condition=name,
                    arg0=", ".join(COMPARISONS),
                )
            )
        return self._compare(name, actual, operators[0], params[operators[0]])

    def _compare(self, name: str, actual: object, operator: str, expected: object) -> bool:
        """Apply one comparison to a condition's value."""
        if operator in COMPARISON_EQUALITY:
            same = self._equal(actual, expected)
            return same if operator == "equals" else not same

        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            # Comparing "DOOR_CLOSED" against 25 has no meaning, and
            # silently answering False would hide the mistake.
            raise ScriptError(
                t(
                    "simulator.scripting.comparison_needs_a_number",
                    "'{operator}' needs a numeric condition; '{condition}' is {arg0}",
                    operator=operator,
                    condition=name,
                    arg0=type(actual).__name__,
                )
            )
        limit = self._script_number(expected, operator, -MAX_SCRIPT_NUMBER, MAX_SCRIPT_NUMBER)
        if operator == "above":
            return actual > limit
        if operator == "below":
            return actual < limit
        if operator == "at_least":
            return actual >= limit
        return actual <= limit

    def _equal(self, actual: object, expected: object) -> bool:
        """Equality, in whichever spelling the author used.

        A boolean condition compares as a **boolean**, through the same
        coercer every other file-supplied flag uses - so ``equals: 1``,
        ``equals: "true"`` and ``equals: enabled`` all work, where a text
        comparison against the rendered ``"on"`` accepted only some of
        them. Everything else compares as case-insensitive text, which
        makes ``equals: 2`` match a stored ``2.0``.
        """
        if isinstance(actual, bool):
            try:
                return actual is coerce_bool(expected, "expected")
            except CoercionError as exc:
                raise ScriptError(str(exc)) from None
        return self._assert_text(actual).casefold() == self._assert_text(expected).casefold()

    async def _wait_for_condition(self, params: dict, timeout: float, on_timeout: str) -> bool:
        """Wait until a condition holds, or the timeout expires.

        Returns True if it was reached. On a timeout this raises unless
        ``on_timeout`` is ``continue``, in which case it returns False and
        the script carries on - which is what makes "do X if it happened, Y
        if it did not" expressible without a third mechanism: the condition
        is still false afterwards, so a following ``if`` sees it.

        A concurrent :meth:`stop` interrupts the wait immediately.
        """
        if self._stop_requested:
            raise ScriptError(
                t(
                    "simulator.scripting.script_stopped_while_waiting",
                    "Script stopped while waiting",
                )
            )
        described = self._describe_condition(params)
        # Validates the names up front, so a typo fails even if the door
        # happens to be in the state that would have satisfied it.
        if self._if_holds(params):
            return True

        statuses = self._status_set_for(params)
        if statuses is not None:
            reached = await self._wait_for_status(described, statuses, timeout)
        else:
            reached = await self._poll_for_condition(params, timeout)

        if not reached and on_timeout != ON_TIMEOUT_CONTINUE:
            raise ScriptError(
                t(
                    "simulator.scripting.timeout_waiting_condition",
                    "Timeout waiting for condition: {condition}",
                    condition=described,
                )
            )
        return reached

    @staticmethod
    def _describe_condition(params: dict) -> str:
        """Name a condition for an error message, in whichever form it took."""
        for form in IF_CONDITION_FORMS:
            if form in params:
                value = params[form]
                return str(value) if form == "condition" else f"{form}={value}"
        return "(none)"

    def _status_set_for(self, params: dict) -> tuple[str, ...] | None:
        """The door statuses a wait can be *signalled* by, if it is one.

        Door status is the only thing the engine announces a transition
        for, so anything else has to poll. Polling is a sleep-and-hope:
        ``DOOR_CLOSING`` lasts about 180 ms on a real door, so a 50 ms
        poll misses it on a slow runner and the script sees
        ``DOOR_CLOSING_TOP_OPEN`` instead. Every door condition therefore
        takes the deterministic path.

        Only a single condition qualifies; a list or a numeric comparison
        polls.
        """
        if list(params.keys() & set(IF_CONDITION_FORMS)) != ["condition"]:
            return None
        name = str(params["condition"]).lower().replace("-", "_")
        if name == "door_status" and "equals" in params:
            return (str(params["equals"]),)
        # The boolean door conditions, which are status sets by another
        # name. Only when asked positively: waiting for `door_closed` to
        # be *false* is not a transition the engine can signal.
        if any(key in params for key in COMPARISONS):
            return None
        return _DOOR_CONDITION_STATUSES.get(name)

    async def _poll_for_condition(self, params: dict, timeout: float) -> bool:
        """Poll a condition until it holds or the deadline passes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._stop_requested:
                raise ScriptError(
                    t(
                        "simulator.scripting.script_stopped_while_waiting",
                        "Script stopped while waiting",
                    )
                )
            if self._if_holds(params):
                return True
            await asyncio.sleep(0.05)
        return False

    async def _wait_for_status(
        self, condition: str, statuses: tuple[str, ...], timeout: float
    ) -> bool:
        """Await a door-status transition (or stop request) with a timeout."""
        waiter = asyncio.ensure_future(self.simulator.wait_for_status(statuses))
        stopper = asyncio.ensure_future(self._stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, stopper}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (waiter, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiter, stopper, return_exceptions=True)

        if stopper in done:
            raise ScriptError(
                t(
                    "simulator.scripting.script_stopped_while_waiting",
                    "Script stopped while waiting",
                )
            )
        return waiter in done and not waiter.cancelled() and waiter.exception() is None

    async def _execute_repeat(self, params: dict) -> None:
        """Run a block a fixed number of times, while a condition holds, or both.

        ``times`` alone is a counted loop. A condition alone - stated in any
        of the three forms :meth:`_if_holds` accepts - is a *while* loop,
        re-tested before each pass. Both together stop at whichever comes
        first, which is the useful shape for "do this until the door
        settles, but never more than ten times".

        There is no ``until``: the condition vocabulary is paired
        (``door_open``/``door_closed``, ``power_on``/``power_off``,
        ``obstruction_on``/``obstruction_off``, ``inside_enabled``/
        ``inside_disabled``), so "until X" is "while its opposite".

        A condition-only loop is still bounded by
        :data:`MAX_SCRIPT_REPEAT`, and reaching that bound is an **error**
        rather than a quiet exit. This DSL is the CI front end: a loop
        whose condition never goes false is a hung build, and a hang that
        reports PASSED would be worse than one that hangs.
        """
        has_condition = any(name in params for name in IF_CONDITION_FORMS)
        times = (
            int(self._script_number(params["times"], "times", 0, MAX_SCRIPT_REPEAT))
            if "times" in params
            else None
        )
        if times is None and not has_condition:
            raise ScriptError(
                t(
                    "simulator.scripting.repeat_needs_a_bound",
                    "'repeat' needs 'times', a condition, or both",
                )
            )

        limit = MAX_SCRIPT_REPEAT if times is None else times
        steps = params.get("steps", [])
        passes = 0
        while passes < limit:
            if self._stop_requested:
                return
            if has_condition and not self._if_holds(params):
                return
            await self._execute_block(steps)
            passes += 1

        if times is None:
            raise ScriptError(
                t(
                    "simulator.scripting.repeat_never_finished",
                    "'repeat' ran {arg0} times without its condition going "
                    "false; add 'times' if it is meant to run this long",
                    arg0=MAX_SCRIPT_REPEAT,
                )
            )

    def _if_holds(self, params: dict) -> bool:
        """Evaluate an ``if`` step's condition, in whichever form it took.

        Three spellings, one meaning each:

        - ``condition:`` (+ optional ``equals:``) - a single condition.
        - ``conditions:`` - a list; **all** must hold.
        - ``any:`` - a list; **at least one** must hold.

        Deliberately flat. Nested boolean algebra is where a config DSL
        becomes a language you debug instead of use, and it is already
        unnecessary: blocks nest, so ``(a and b) or c`` is an ``if`` inside
        an ``else``.
        """
        forms = [name for name in IF_CONDITION_FORMS if name in params]
        if len(forms) != 1:
            raise ScriptError(
                t(
                    "simulator.scripting.if_needs_exactly_one_form",
                    "'if' needs exactly one of: {arg0} (got {arg1})",
                    arg0=", ".join(IF_CONDITION_FORMS),
                    arg1=", ".join(forms) if forms else "none",
                )
            )
        form = forms[0]
        if form == "condition":
            return self._condition_holds(params["condition"], params)
        stray = [key for key in COMPARISONS if key in params]
        if stray:
            # A comparison belongs to the single-condition form; silently
            # ignoring it here would make a real typo look accepted.
            raise ScriptError(
                t(
                    "simulator.scripting.comparison_belongs_to_condition",
                    "'{arg0}' applies to 'condition', not to '{form}' - "
                    "put it on the entry it belongs to",
                    arg0=stray[0],
                    form=form,
                )
            )
        entries = params["conditions"] if form == "conditions" else params["any"]
        if not isinstance(entries, list) or not entries:
            raise ScriptError(
                t(
                    "simulator.scripting.condition_list_must_be_nonempty",
                    "'{form}' must be a non-empty list of conditions",
                    form=form,
                )
            )
        results = [self._entry_holds(entry, form) for entry in entries]
        return all(results) if form == "conditions" else any(results)

    def _entry_holds(self, entry: object, form: str) -> bool:
        """Evaluate one entry of a ``conditions:``/``any:`` list.

        A bare string is a fused shorthand, the same way a bare string is a
        no-parameter step in the step list.
        """
        if isinstance(entry, str):
            return self._condition_holds(entry, {})
        if not isinstance(entry, dict):
            raise ScriptError(
                t(
                    "simulator.scripting.condition_entry_invalid",
                    "Each entry of '{form}' must be a condition name or a mapping with 'condition'",
                    form=form,
                )
            )
        unexpected = sorted(set(entry) - {"condition", *COMPARISONS})
        if unexpected:
            raise ScriptError(
                t(
                    "simulator.scripting.unknown_condition_entry_key",
                    "Unknown key(s) in a '{form}' entry: {arg0}. Use: condition, {arg1}",
                    form=form,
                    arg0=", ".join(str(key) for key in unexpected),
                    arg1=", ".join(COMPARISONS),
                )
            )
        if "condition" not in entry:
            raise ScriptError(
                t(
                    "simulator.scripting.condition_entry_needs_condition",
                    "An entry of '{form}' has no 'condition'",
                    form=form,
                )
            )
        return self._condition_holds(entry["condition"], entry)

    async def _execute_block(self, steps: object) -> None:
        """Run a nested step list, honouring a stop request between steps.

        ``if``/``repeat`` bodies are parsed into :class:`ScriptStep` objects
        at load time, so this only has to run them.
        """
        if not isinstance(steps, list):
            raise ScriptError(
                t(
                    "simulator.scripting.block_must_be_list",
                    "A nested block must be a list of steps",
                )
            )
        for step in steps:
            if self._stop_requested:
                raise ScriptError(t("simulator.scripting.script_stopped", "Script stopped"))
            await self._execute_step(step)

    def _resolve_state_document(self, document: object) -> dict:
        """Load the document a ``reset`` step names, or the runner's initial one.

        Subject to the same path policy as running a script by name: a
        script arriving over the unauthenticated control channel must not
        be able to read configuration from an arbitrary file.
        """
        if document is None:
            return self.initial_state_document or {}
        loader = self.load_state_document
        if loader is None:
            raise ScriptError(
                t(
                    "simulator.scripting.no_state_loader",
                    "This runner cannot load state documents by name",
                )
            )
        try:
            return loader(str(document))
        except Exception as exc:
            raise ScriptError(str(exc)) from None

    def _presence(self, params: dict, default: float | None) -> tuple[bool | None, float | None]:
        """Read the ``state``/``duration`` argument the three share.

        ``inside``, ``outside`` and ``obstruction`` all answer "is it there,
        and for how long", so they take one argument shape:
        ``on``/``off``/``toggle`` or a number of seconds. ``default`` is
        what a bare step means - a brief pulse for a sensor, a toggle for
        an obstruction, which is the one place they differ and the one
        place it is physical: a pet walks past a sensor, a boot is placed.
        """
        given = params.get("state", params.get("duration"))
        if given is None:
            return (None, default) if default is not None else (None, None)
        try:
            present, duration = coerce_presence(given)
        except CoercionError as exc:
            raise ScriptError(str(exc)) from None
        if duration is not None:
            duration = self._script_number(duration, "duration", 0, MAX_SCRIPT_DELAY)
        return present, duration

    @staticmethod
    def _script_choice(value: object, name: str, choices: tuple[str, ...]) -> str:
        """Coerce a script-supplied keyword, refusing anything unlisted.

        Same loud failure every other misspelling class in this DSL gets:
        a silently-ignored ``on_timeout: contineu`` would turn a guarded
        wait back into a failing one with no sign in the log.
        """
        text = str(value).strip().lower()
        if text not in choices:
            raise ScriptError(
                t(
                    "simulator.scripting.unknown_choice_use",
                    "Unknown {name}: {arg0}. Use: {arg1}",
                    name=name,
                    arg0=sanitize_text(value),
                    arg1=", ".join(choices),
                )
            )
        return text

    @staticmethod
    def _script_number(value: object, name: str, minimum: float, maximum: float) -> float:
        """Coerce a script-supplied numeric setting, bounded and finite.

        The bounds themselves live in :mod:`~powerpetdoor.simulator.coerce`,
        shared with the state-document loader: two writers of the same
        fields must not disagree about what ``hold_time: inf`` means. This
        wrapper only re-labels the failure as a script error, so every
        call site keeps raising what the runner catches.

        Raises:
            ScriptError: If the value is not a finite number in range.
        """
        try:
            return coerce_number(value, name, minimum, maximum)
        except CoercionError as exc:
            raise ScriptError(str(exc)) from None

    @staticmethod
    def _script_bool(value: object) -> bool:
        """Coerce a script-supplied boolean parameter, fail-closed.

        Shared with the state-document loader - see
        :func:`~powerpetdoor.simulator.coerce.coerce_bool` for why plain
        truthiness is wrong here, and why a bad value raises rather than
        failing closed.

        Raises:
            ScriptError: If the value is not a boolean spelling.
        """
        try:
            return coerce_bool(value, "script boolean")
        except CoercionError as exc:
            raise ScriptError(str(exc)) from None

    def _set_value(self, name: str, value: str):
        """Set a named value, or invert it when told to ``toggle``.

        Writes through the same registry the CLI's ``set`` uses, so a
        value reachable at the prompt is reachable from a script.

        Raises:
            ScriptError: If the name is not a value, is read-only, or the
                supplied value is not usable for it.
        """
        try:
            if str(value).strip().lower() == TOGGLE_KEYWORD:
                toggle_named_value(self.simulator, name)
            else:
                set_named_value(self.simulator, name, value)
        except CoercionError as exc:
            raise ScriptError(str(exc)) from None

    @staticmethod
    def _assert_text(value: object) -> str:
        """Render one side of an ``assert`` comparison as text.

        ``equals:`` arrives straight from PyYAML, which resolves ``on`` /
        ``off`` (and ``yes`` / ``no`` / ``true`` / ``false``) to booleans and
        bare digits to ints. Calling ``.lower()`` on those raises
        ``AttributeError``, so most of the documented conditions failed a
        *true* assertion with a Python internals message. Both sides go
        through here, so the two spellings always agree.
        """
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, float) and value.is_integer():
            # hold_time is stored as a float, so `equals: 2` (and the quoted
            # `equals: "2"`) both have to match a stored 2.0.
            return str(int(value))
        return str(value)

    def _assert_condition(self, params: dict) -> None:
        """Assert a condition, in any of the forms ``if`` accepts.

        Goes through the same :meth:`_if_holds` that ``wait_for``, ``if``
        and ``repeat`` use, so the four can never disagree about what a
        name means or how a value is spelled.
        """
        if self._if_holds(params):
            return

        described = self._describe_condition(params)
        operator = next((key for key in COMPARISONS if key in params), None)
        if operator is None or "condition" not in params:
            raise ScriptAssertionError(
                t(
                    "simulator.scripting.assertion_failed",
                    "assertion failed: {condition}",
                    condition=described,
                )
            )
        name = str(params["condition"]).lower().replace("-", "_")
        raise ScriptAssertionError(
            t(
                "simulator.scripting.expected_got",
                "{condition}: expected {operator} '{expected_text}', got '{actual_text}'",
                condition=name,
                operator=operator,
                expected_text=self._assert_text(params[operator]),
                actual_text=self._assert_text(self._condition_value(name)),
            )
        )


# Directory containing built-in script files
SCRIPTS_DIR = Path(__file__).parent / "scripts"

# Extra scripts directory registered by --scripts-dir. Module-level so the
# name resolver, `list`, the unknown-script hint, and tab completion all
# see the same set of runnable scripts (the completer is referenced from an
# ArgSpec and has no handler context of its own).
_extra_scripts_dir: Path | None = None


#: Whether this process may run a script by file path. ctl talks to a
#: daemon that refuses paths outright (`_load_script_restricted`), so
#: completing local YAML files there steers the user to a form guaranteed
#: to fail - and cannot offer the bare name that works. Module-level for the
#: same reason as _extra_scripts_dir: the completer is referenced from an
#: ArgSpec and has no handler context of its own.
_script_paths_allowed = True


def set_extra_scripts_dir(directory: str | Path | None) -> None:
    """Register (or clear) the extra scripts directory from ``--scripts-dir``."""
    global _extra_scripts_dir
    _extra_scripts_dir = Path(directory) if directory else None


def set_script_paths_allowed(allowed: bool) -> None:
    """Declare whether this front end may run scripts by file path."""
    global _script_paths_allowed
    _script_paths_allowed = allowed


def script_paths_allowed() -> bool:
    """Whether this front end may run scripts by file path."""
    return _script_paths_allowed


def describe_script_argument() -> str:
    """Help text for a ``script`` argument, honoring the path policy.

    ``run help`` over ctl answered "Script name or file path" while the
    very next command answered "Script paths are not allowed over the
    control channel" - the in-client help pointing at the broken form.
    """
    if _script_paths_allowed:
        return "Script name or file path"
    return "Script name (paths are not accepted over the control channel)"


def describe_out_of_directory_remedy() -> str:
    """What to do about a script that resolves outside ``--scripts-dir``.

    Policy-aware for the same reason :func:`describe_script_argument` is:
    over the control channel "or run it by path" pointed the operator at a
    form the very next line of code refuses, so the two refusals pointed at
    each other. Locally, running it by path really is the remedy, so the
    advice stays.
    """
    if _script_paths_allowed:
        return "move it into the directory or run it by path"
    return "move it into the directory (paths are not accepted over the control channel)"


def path_escapes_directory(path: Path, base: Path) -> bool:
    """Whether ``path`` resolves outside its own ``base`` directory.

    The single containment rule, shared by the script loader, the state
    document loader, and every surface that advertises either. It used to live only in the loader, so a
    symlink pointing out of ``--scripts-dir`` was listed by ``list``,
    ``--list-scripts`` and tab completion - and then refused by ``run``
    with ``Unknown script: linked. Available: ..., linked, ...``, a message
    that contradicted itself inside one line.

    Args:
        path: Candidate script file.
        base: Already-resolved directory it must live directly in.
    """
    return path.resolve().parent != base


def _script_files_in(directory: Path) -> dict[str, Path]:
    """Map script name -> path for the YAML files directly in ``directory``.

    Files that resolve outside ``directory`` are omitted, so what is
    advertised is exactly what :meth:`load_script` will accept.
    """
    scripts: dict[str, Path] = {}
    if not directory.exists():
        return scripts
    base = directory.resolve()
    for pattern in ("*.yaml", "*.yml"):
        for path in directory.glob(pattern):
            if path_escapes_directory(path, base):
                # DEBUG, not WARNING: tab completion calls this on every
                # keystroke. `run <name>` explains the refusal in full.
                logger.debug(
                    t(
                        "simulator.scripting.listing_resolves_outside",
                        "Not listing %s: it resolves outside %s",
                    ),
                    sanitize_text(path.name),
                    base,
                )
                continue
            scripts[path.stem] = path
    return scripts


def _get_script_files() -> dict[str, Path]:
    """Get all available script files from the built-in scripts directory."""
    return _script_files_in(SCRIPTS_DIR)


def get_extra_script_files() -> dict[str, Path]:
    """Script name -> path for the registered ``--scripts-dir``, if any."""
    return _script_files_in(_extra_scripts_dir) if _extra_scripts_dir else {}


#: Largest number of remembered script descriptions. Each entry is keyed
#: on the file's identity *and* its stat, so editing a script adds a key
#: rather than replacing one - without a cap, a long-lived daemon whose
#: scripts are regenerated would grow the cache without bound. Clearing
#: wholesale (rather than evicting) keeps the bookkeeping free; the next
#: listing simply re-parses.
MAX_DESCRIPTION_CACHE = 512

#: ``(path, st_mtime_ns, st_size)`` -> ``(description, error_text | None)``.
_description_cache: dict[tuple[str, int, int], tuple[str, str | None]] = {}


def _describe_script(path: Path) -> tuple[str, str | None]:
    """Read one script's description, remembering it per file version.

    Every listing and every Tab keystroke used to fully YAML-parse every
    candidate file - 200 scripts cost ~600 ms, on the same event loop that
    serves the door protocol, so one keystroke stalled the emulated device.
    Descriptions are what the parse is *for* and they change only when the
    file does, so the stat tuple is the key: new and edited files are still
    picked up immediately, which is the behaviour ``list`` relies on.
    """
    try:
        stat = path.stat()
    except OSError:
        # Vanished between listing and describing; parse and let the
        # failure be reported, but do not poison the cache with it.
        return _read_description(path)
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _description_cache.get(key)
    if cached is not None:
        return cached
    described = _read_description(path)
    if len(_description_cache) >= MAX_DESCRIPTION_CACHE:
        _description_cache.clear()
    _description_cache[key] = described
    return described


def _read_description(path: Path) -> tuple[str, str | None]:
    """Parse ``path`` and return ``(description, error_text | None)``."""
    try:
        return Script.from_file(path).description, None
    except Exception as e:
        return f"(Error loading: {sanitize_text(e)})", sanitize_text(e)


def _describe_scripts(script_files: dict[str, Path]) -> list[tuple[str, str]]:
    """Build sorted (name, description) pairs for the given script files."""
    result = []
    for name, path in sorted(script_files.items()):
        description, error = _describe_script(path)
        if error is not None:
            # Both the name and the error text are file-derived.
            # Reported on every listing, not only the first: the cache is
            # a parse cache, not a report cache.
            logger.warning(
                t(
                    "simulator.scripting.failed_load_script",
                    "Failed to load script {arg0}: {error}",
                    arg0=sanitize_text(name),
                    error=error,
                )
            )
        result.append((name, description))
    return result


def get_builtin_script(name: str) -> Script:
    """Get a built-in script by name.

    Scripts are loaded from YAML files in the 'scripts' directory. The
    error for an unknown name also lists any ``--scripts-dir`` scripts, so
    the hint matches what ``list`` shows.
    """
    script_files = _get_script_files()
    if name not in script_files:
        available = ", ".join(sorted({*script_files, *get_extra_script_files()}))
        # "built-in" would read as if the --scripts-dir names in that same
        # Available: list were built-ins too.
        raise ScriptError(
            t(
                "simulator.scripting.unknown_script_available",
                "Unknown script: {name}. Available: {available}",
                name=name,
                available=available,
            )
        )
    return Script.from_file(script_files[name])


def list_builtin_scripts() -> list[tuple[str, str]]:
    """List all built-in scripts with descriptions.

    Returns a list of (name, description) tuples.
    """
    return _describe_scripts(_get_script_files())


def list_extra_scripts() -> list[tuple[str, str]]:
    """List the ``--scripts-dir`` scripts with descriptions."""
    return _describe_scripts(get_extra_script_files())


@dataclass
class ScriptListing:
    """The rendered script listing plus the raw pairs behind it.

    Attributes:
        lines: The listing as printed, shadow markers included.
        builtin: ``(name, description)`` for the built-in scripts.
        extra: ``(name, description)`` for the ``--scripts-dir`` scripts.
    """

    lines: list[str]
    builtin: list[tuple[str, str]]
    extra: list[tuple[str, str]]


def render_script_listing(
    scripts_dir: str | None,
    builtin: list[tuple[str, str]] | None = None,
) -> ScriptListing:
    """Render "what can I run?" for every surface that answers it.

    The ``list`` command and ``ppd-simulator --list-scripts`` print the
    same list, and ``cli.py`` even carries a comment saying they are meant
    to agree - but the shadow marker used to land in ``list`` and in tab
    completion only. ``--list-scripts`` is the pre-flight surface (it needs
    no daemon), so it is the one most likely to be consulted first, and it
    showed the shadowed name twice with two contradicting descriptions and
    nothing saying which one ``run`` picks.

    The marker also prints the *real* file, from
    :func:`get_extra_script_files`, rather than reconstructing
    ``<dir>/<name>``: the reconstructed form dropped the suffix, so it read
    like a path but ``ls`` on it failed, and it could not express a
    shadowing ``.yml``.

    Args:
        scripts_dir: The configured ``--scripts-dir``, or None. When None
            the "Scripts from ..." section is omitted entirely.
        builtin: Pre-computed built-in pairs, for callers holding an
            injected lister. Defaults to :func:`list_builtin_scripts`.
    """
    extra_files = get_extra_script_files()
    extra = _describe_scripts(extra_files)
    builtin = list(list_builtin_scripts() if builtin is None else builtin)

    lines = ["Built-in scripts:"]
    for name, desc in builtin:
        shadowing = extra_files.get(name)
        marker = f" (shadowed by {shadowing})" if shadowing is not None else ""
        lines.append(f"  {name}: {desc}{marker}")
    if scripts_dir is not None:
        # Header even when the directory is empty, so the flag's effect is
        # visible rather than silently absent.
        lines.append(f"Scripts from {scripts_dir}:")
        for name, desc in extra:
            lines.append(f"  {name}: {desc}")
        if not extra:
            lines.append("  (none)")
    return ScriptListing(lines=lines, builtin=builtin, extra=extra)


def matches_completion_prefix(candidate: str, prefix: str) -> bool:
    """Whether ``candidate`` would survive the prompt's own completion filter.

    The single definition of "this completion matches what was typed",
    used both by the pre-filter inside :func:`script_completer` and by the
    filter ``prompt_common.SimulatorCompleter`` applies to every candidate
    it is handed. They have to agree: a case-*sensitive* pre-filter would
    silently drop completions for uppercase input that the prompt would
    otherwise have offered.
    """
    return candidate.lower().startswith(prefix.lower())


def script_completer(prefix: str = "") -> list[tuple[str, str]]:
    """Return list of (script_name, description) for tab completion.

    This function is designed to be used as an ArgSpec completer.
    Returns both builtin scripts and YAML files based on the prefix path.

    Args:
        prefix: The partial path/name being completed (e.g., "", "basic", "./scr")

    Gracefully handles missing PyYAML by returning just script names.
    When this front end may not run scripts by path (ctl, whose daemon
    refuses them), only script *names* are offered - suggesting a local
    file there is suggesting a command that always fails.

    In the name branch the prefix filters *before* the parse. It used to
    filter nowhere: every candidate was fully YAML-parsed and the whole
    set handed to prompt_toolkit, so completing four characters that
    already identify one file cost exactly as much as completing nothing -
    ~600 ms for a 200-script directory, on the door server's own event
    loop. The test is case-insensitive to match the downstream filter in
    ``prompt_common``, which is
    ``name.lower().startswith(word_before.lower())``; a case-sensitive one
    here would silently change completion for uppercase input.
    """
    result: list[tuple[str, str]] = []

    if not _script_paths_allowed and ("/" in prefix or "\\" in prefix):
        # A path-shaped prefix can only ever complete to a command this
        # front end's daemon refuses outright, so offer nothing.
        return result

    # Determine the directory to search based on prefix
    # We preserve the original prefix format (e.g., "./" stays as "./")
    if prefix:
        prefix_path = Path(prefix)
        if prefix.endswith("/") or prefix.endswith("\\"):
            # User typed a directory path ending with /
            search_dir = prefix_path
            dir_prefix = prefix  # e.g., "./" or "src/"
        elif "/" in prefix or "\\" in prefix:
            # User is typing a path like "./scripts/bas" or "./da"
            search_dir = prefix_path.parent
            # Get the directory prefix as a string (preserves "./")
            last_sep = max(prefix.rfind("/"), prefix.rfind("\\"))
            dir_prefix = prefix[: last_sep + 1]  # e.g., "./" from "./da"
        else:
            # Just a name prefix like "bas"
            search_dir = None
            dir_prefix = ""
    else:
        search_dir = None
        dir_prefix = ""

    # If searching in a specific directory, list files there
    if search_dir is not None:
        # Resolve relative to cwd
        if not search_dir.is_absolute():
            search_dir = Path.cwd() / search_dir

        if search_dir.is_dir():
            # List YAML files in that directory
            for pattern in ("*.yaml", "*.yml"):
                for path in search_dir.glob(pattern):
                    if path.is_file():
                        # Use dir_prefix to preserve original format (e.g., "./")
                        completion = dir_prefix + path.name
                        result.append((completion, "(file)"))

            # Also list subdirectories for further navigation
            for subdir in search_dir.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("."):
                    completion = dir_prefix + subdir.name + "/"
                    result.append((completion, "(directory)"))
    else:
        # No specific directory - show builtin scripts and cwd files

        # Add builtin scripts, then any registered --scripts-dir scripts.
        # A shadowed built-in is skipped rather than offered a second time:
        # the completer cannot disambiguate two identical strings, and the
        # scripts-dir copy is the one `run` would pick.
        extra_files = get_extra_script_files()
        for script_files, label in (
            ({k: v for k, v in _get_script_files().items() if k not in extra_files}, "(builtin)"),
            (extra_files, "(scripts-dir)"),
        ):
            for name in sorted(script_files):
                if not matches_completion_prefix(name, prefix):
                    continue
                if not YAML_AVAILABLE:
                    result.append((name, label))
                    continue
                description, error = _describe_script(script_files[name])
                result.append((name, label if error is not None else description or label))

        if _script_paths_allowed:
            # Add YAML files from current directory
            cwd = Path.cwd()
            for pattern in ("*.yaml", "*.yml"):
                for path in cwd.glob(pattern):
                    if matches_completion_prefix(path.name, prefix) and path.is_file():
                        result.append((path.name, "(local file)"))

            # Add subdirectories that might contain scripts
            for subdir in cwd.iterdir():
                if not matches_completion_prefix(subdir.name + "/", prefix):
                    # Checked before the glob below, which is the expensive
                    # half of this walk.
                    continue
                if subdir.is_dir() and not subdir.name.startswith("."):
                    # Check if it has any yaml files
                    has_yaml = any(subdir.glob("*.yaml")) or any(subdir.glob("*.yml"))
                    if has_yaml:
                        result.append((subdir.name + "/", "(directory)"))

    return result
