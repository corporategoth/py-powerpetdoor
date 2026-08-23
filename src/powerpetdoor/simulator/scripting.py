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
  - inside: Activate inside sensor with optional duration (default 0.5s)
  - outside: Activate outside sensor with optional duration (default 0.5s)
  - obstruction: Simulate obstruction (sets inside sensor active indefinitely)
  - pet_presence: Set pet in doorway (inside sensor active indefinitely)
  - open: Open door (optionally with hold)
  - close: Close door
  - wait: Wait for specified seconds
  - wait_for: Wait for a condition (with timeout)
  - set: Set a state value (power, battery, hold_time, etc.)
  - toggle: Toggle a boolean setting
  - assert: Assert a condition is true
  - log: Print a message
  - add_schedule: Add a schedule
  - remove_schedule: Remove a schedule
"""

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

if TYPE_CHECKING:
    from .server import DoorSimulator

from ..const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
)
from ..sanitize import sanitize_text
from ..schedule import MAX_SCHEDULE_INDEX, coerce_schedule_flag
from .engine import SENSOR_NAMES
from .state import END_OF_DAY_HOUR, END_OF_DAY_MINUTE, Schedule

logger = logging.getLogger(__name__)

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

#: Script-file spellings of the boolean values that the shared wire
#: coercer does not know. These are *front-end* vocabulary (``set
#: safety_lock enabled``) and are deliberately not added to ``make_bool``,
#: which reads device data.
_SCRIPT_BOOL_ALIASES = {"enabled": True, "disabled": False}


class ScriptError(Exception):
    """Error during script execution."""

    pass


class ScriptAssertionError(ScriptError):
    """Assertion in script failed."""

    pass


#: Backwards-compatible alias for :class:`ScriptAssertionError`.
AssertionFailed = ScriptAssertionError

#: wait_for conditions that map directly to door-status values. These are
#: awaited via the simulator's deterministic wait_for_status hook instead of
#: polling.
_STATUS_WAIT_CONDITIONS: dict[str, tuple[str, ...]] = {
    "door_closed": (DOOR_STATE_CLOSED,),
    "door_open": (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP),
    "door_rising": (DOOR_STATE_RISING,),
    "door_holding": (DOOR_STATE_HOLDING,),
    "door_keepup": (DOOR_STATE_KEEPUP,),
    "door_closing": (DOOR_STATE_CLOSING_TOP_OPEN, DOOR_STATE_CLOSING_MID_OPEN),
}

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

#: Conditions ``wait_for`` polls, beyond :data:`_STATUS_WAIT_CONDITIONS`.
_POLLED_WAIT_CONDITIONS: tuple[str, ...] = (
    "auto_off",
    "auto_on",
    "autoretract_off",
    "autoretract_on",
    "cmd_lockout_off",
    "cmd_lockout_on",
    "inside_disabled",
    "inside_enabled",
    "outside_disabled",
    "outside_enabled",
    "power_off",
    "power_on",
    "safety_lock_off",
    "safety_lock_on",
)

#: Every condition ``wait_for`` accepts.
WAIT_FOR_CONDITIONS: tuple[str, ...] = tuple(
    sorted({*_STATUS_WAIT_CONDITIONS, *_POLLED_WAIT_CONDITIONS})
)

#: Every condition ``assert`` accepts. Disjoint from
#: :data:`WAIT_FOR_CONDITIONS`, which is why the two errors cross-reference
#: each other.
ASSERT_CONDITIONS: tuple[str, ...] = (
    "auto",
    "autoretract",
    "battery",
    "cmd_lockout",
    "door_status",
    "hold_time",
    "inside",
    "outside",
    "power",
    "safety_lock",
    "total_auto_retracts",
    "total_open_cycles",
)

#: Settings ``set`` accepts.
SET_SETTINGS: tuple[str, ...] = (
    "auto",
    "autoretract",
    "battery",
    "cmd_lockout",
    "hold_time",
    "inside",
    "outside",
    "power",
    "safety_lock",
)

#: Settings ``toggle`` accepts - the boolean subset of
#: :data:`SET_SETTINGS`. ``hold_time`` and ``battery`` hold a value rather
#: than a state, so there is nothing to invert.
TOGGLE_SETTINGS: tuple[str, ...] = tuple(
    name for name in SET_SETTINGS if name not in ("battery", "hold_time")
)


def _other_table_hint(name: str, other: tuple[str, ...], other_action: str) -> str:
    """Name the *other* action when a rejected name is valid there.

    ``assert door_closed`` is the single most natural assertion in a door
    simulator, and it is a ``wait_for`` condition - so the runner
    recognises the name, for the other action, and said only "Unknown
    assertion condition". The error message is what the author who
    mistyped is actually looking at, so it names the other action.
    """
    return f" (that name belongs to the '{other_action}' action)" if name in other else ""


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
    "trigger_sensor": frozenset({"sensor"}),
    "trigger": frozenset({"sensor"}),
    "obstruction": frozenset(),
    "pet_presence": frozenset(),
    "pet_on": frozenset(),
    "pet_off": frozenset(),
    "inside": frozenset({"duration"}),
    "outside": frozenset({"duration"}),
    "open": frozenset({"hold"}),
    "close": frozenset(),
    "wait": frozenset({"seconds"}),
    "wait_for": frozenset({"condition", "timeout"}),
    "set": frozenset({"name", "value"}),
    "toggle": frozenset({"name"}),
    "assert": frozenset({"condition", "equals"}),
    "log": frozenset({"message"}),
    "add_schedule": frozenset({"index", "enabled"}),
    "remove_schedule": frozenset({"index"}),
    "battery": frozenset({"percent", "value"}),
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
        if self.params:
            params_str = ", ".join(f"{k}={v}" for k, v in self.params.items())
            return f"{self.action}({params_str})"
        return self.action


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
            raise ScriptError("PyYAML is required for script support: pip install pyyaml")

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as err:
            raise ScriptError(f"Invalid script YAML: {err}") from err
        if not isinstance(data, dict):
            raise ScriptError("Script must be a YAML dictionary")

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
                f"Unknown top-level key(s): {', '.join(str(key) for key in unknown)}. "
                f"Use: {', '.join(sorted(SCRIPT_TOP_LEVEL_KEYS))}"
            )

        name = data.get("name", "Unnamed Script")
        description = data.get("description", "")
        steps_data = data.get("steps", [])

        if not isinstance(steps_data, list):
            raise ScriptError("'steps' must be a list")

        steps = []
        for i, step_data in enumerate(steps_data, 1):
            if isinstance(step_data, str):
                # Simple action with no params: "- close"
                steps.append(ScriptStep(action=step_data, line_number=i))
            elif isinstance(step_data, dict):
                action = step_data.pop("action", None)
                if not action:
                    raise ScriptError(f"Step {i}: missing 'action' field")
                steps.append(ScriptStep(action=action, params=step_data, line_number=i))
            else:
                raise ScriptError(f"Step {i}: invalid step format")

        return cls(
            name=name,
            description=description,
            steps=steps,
            source_file=source_file,
        )

    @classmethod
    def from_file(cls, path: Path) -> "Script":
        """Load a script from a YAML file.

        Raises ScriptError for unreadable files and invalid YAML alike, so
        callers only need to handle one error type for loader failures.
        """
        try:
            content = path.read_text()
        except OSError as err:
            raise ScriptError(f"Cannot read script file '{path}': {err}") from err
        return cls.from_yaml(content, source_file=str(path))

    @classmethod
    def from_simple_commands(cls, commands: list[str], name: str = "Inline Script") -> "Script":
        """Create a script from simple command strings.

        Commands use a simple format:
            trigger inside
            wait 2
            trigger outside
            wait_for door_closed 10
            set battery 50
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
            elif action == "set":
                params["name"] = parts[1] if len(parts) > 1 else ""
                params["value"] = parts[2] if len(parts) > 2 else ""
            elif action == "toggle":
                params["name"] = parts[1] if len(parts) > 1 else ""
            elif action == "assert":
                params["condition"] = parts[1] if len(parts) > 1 else ""
                params["equals"] = parts[2] if len(parts) > 2 else ""
            elif action == "log":
                params["message"] = " ".join(parts[1:])
            elif action == "open":
                params["hold"] = "hold" in parts
            elif action in ("close", "obstruction", "pet_on", "pet_off"):
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

    def __init__(self, simulator: "DoorSimulator"):
        self.simulator = simulator
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
            raise ScriptError(f"Another script is already running: {self.current_script}")
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
            logger.info(f"Running script: {sanitize_text(script.name)}")
            if script.description:
                logger.info(f"  {sanitize_text(script.description)}")

        try:
            for step in script.steps:
                if self._stop_requested:
                    logger.info("Script stopped by request")
                    return False

                if verbose:
                    logger.info(f"  Step {step.line_number}: {sanitize_text(step)}")

                await self._execute_step(step)

            if self._stop_requested:
                # A stop that landed during the *last* step used to be
                # discarded: the loop simply ended and the run reported
                # PASSED with exit code 0, the opposite of what `stop`
                # documents and the one signal a CI abort relies on.
                logger.info("Script stopped by request")
                return False

            if verbose:
                logger.info(f"Script '{sanitize_text(script.name)}' completed successfully")
            return True

        except ScriptAssertionError as e:
            logger.error(f"Assertion failed at step {step.line_number}: {sanitize_text(e)}")
            return False
        except ScriptError as e:
            logger.error(f"Script error at step {step.line_number}: {sanitize_text(e)}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error at step {step.line_number}: {sanitize_text(e)}")
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
                    f"Unknown parameter(s) for {action}: {', '.join(unexpected)}. "
                    f"{accepted} (plus the annotations {annotations})"
                )

        state = self.simulator.state

        if action == "trigger_sensor" or action == "trigger":
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
                raise ScriptError(f"Unknown sensor: {sensor}. Use: {', '.join(SENSOR_NAMES)}")
            self.simulator.trigger_sensor(sensor)

        elif action == "obstruction":
            self.simulator.simulate_obstruction()

        elif action == "pet_presence" or action == "pet_on":
            # Pet presence = inside sensor active indefinitely (toggle on)
            if not state.inside_sensor_active:
                self.simulator.activate_sensor("inside", 0)

        elif action == "pet_off":
            # Clear inside sensor (via the simulator so the engine is woken)
            self.simulator.set_pet_in_doorway(False)

        elif action == "inside":
            # Activate inside sensor with optional duration
            duration = self._script_number(
                params.get("duration", 0.5), "duration", 0, MAX_SCRIPT_DELAY
            )
            self.simulator.activate_sensor("inside", duration)

        elif action == "outside":
            # Activate outside sensor with optional duration
            duration = self._script_number(
                params.get("duration", 0.5), "duration", 0, MAX_SCRIPT_DELAY
            )
            self.simulator.activate_sensor("outside", duration)

        elif action == "open":
            hold = self._script_bool(params.get("hold", False))
            await self.simulator.open_door(hold=hold)

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
            condition = params.get("condition", "door_closed")
            timeout = self._script_number(
                params.get("timeout", 30.0), "timeout", 0, MAX_SCRIPT_DELAY
            )
            await self._wait_for_condition(condition, timeout)

        elif action == "set":
            name = params.get("name", "")
            value = params.get("value", "")
            self._set_value(name, value)

        elif action == "toggle":
            name = params.get("name", "")
            self._toggle_value(name)

        elif action == "assert":
            condition = params.get("condition", "")
            expected = params.get("equals", "")
            self._assert_condition(condition, expected)

        elif action == "log":
            message = params.get("message", "")
            # Script-supplied text reaching an operator's terminal: same
            # rule as the wire channel.
            logger.info(f"  [SCRIPT] {sanitize_text(message)}")

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
                end_hour=END_OF_DAY_HOUR,
                end_min=END_OF_DAY_MINUTE,
            )
            self.simulator.add_schedule(schedule)

        elif action == "remove_schedule":
            index = int(self._script_number(params.get("index", 1), "index", 0, MAX_SCHEDULE_INDEX))
            self.simulator.remove_schedule(index)

        elif action == "battery":
            percent = self._script_number(
                params.get("percent", params.get("value", 50)), "battery", 0, 100
            )
            self.simulator.set_battery(int(percent))

        else:
            raise ScriptError(f"Unknown action: {action}. Use: {', '.join(sorted(_ACTION_PARAMS))}")

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

    async def _wait_for_condition(self, condition: str, timeout: float):
        """Wait for a condition to become true.

        Door-status conditions use the simulator's deterministic
        ``wait_for_status`` hook; other conditions fall back to a
        deadline-bounded poll. A concurrent :meth:`stop` interrupts the
        wait immediately.
        """
        if self._stop_requested:
            raise ScriptError("Script stopped while waiting")
        # Validates the condition name up front (raises on unknown)
        if self._check_condition(condition):
            return

        statuses = _STATUS_WAIT_CONDITIONS.get(condition.lower().replace("-", "_"))
        if statuses is not None:
            await self._wait_for_status(condition, statuses, timeout)
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._stop_requested:
                raise ScriptError("Script stopped while waiting")
            if self._check_condition(condition):
                return
            await asyncio.sleep(0.05)

        raise ScriptError(f"Timeout waiting for condition: {condition}")

    async def _wait_for_status(
        self, condition: str, statuses: tuple[str, ...], timeout: float
    ) -> None:
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
            raise ScriptError("Script stopped while waiting")
        if waiter in done and not waiter.cancelled() and waiter.exception() is None:
            return
        raise ScriptError(f"Timeout waiting for condition: {condition}")

    def _check_condition(self, condition: str) -> bool:
        """Check if a condition is true."""
        state = self.simulator.state
        condition = condition.lower().replace("-", "_")

        if condition == "door_closed":
            return state.door_status == DOOR_STATE_CLOSED
        elif condition == "door_open":
            return state.door_status in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP)
        elif condition == "door_rising":
            return state.door_status == DOOR_STATE_RISING
        elif condition == "door_holding":
            return state.door_status == DOOR_STATE_HOLDING
        elif condition == "door_keepup":
            return state.door_status == DOOR_STATE_KEEPUP
        elif condition == "door_closing":
            return state.door_status in (
                DOOR_STATE_CLOSING_TOP_OPEN,
                DOOR_STATE_CLOSING_MID_OPEN,
            )
        elif condition == "power_on":
            return state.power
        elif condition == "power_off":
            return not state.power
        elif condition == "inside_enabled":
            return state.inside
        elif condition == "outside_enabled":
            return state.outside
        elif condition == "autoretract_on":
            return state.autoretract
        elif condition == "autoretract_off":
            return not state.autoretract
        elif condition == "auto_on":
            return state.auto
        elif condition == "auto_off":
            return not state.auto
        elif condition == "inside_disabled":
            return not state.inside
        elif condition == "outside_disabled":
            return not state.outside
        elif condition == "safety_lock_on":
            return state.safety_lock
        elif condition == "safety_lock_off":
            return not state.safety_lock
        elif condition == "cmd_lockout_on":
            return state.cmd_lockout
        elif condition == "cmd_lockout_off":
            return not state.cmd_lockout
        else:
            raise ScriptError(
                f"Unknown condition: {condition}"
                f"{_other_table_hint(condition, ASSERT_CONDITIONS, 'assert')}. "
                f"Use: {', '.join(WAIT_FOR_CONDITIONS)}"
            )

    @staticmethod
    def _script_number(value: object, name: str, minimum: float, maximum: float) -> float:
        """Coerce a script-supplied numeric setting, bounded and finite.

        The YAML script channel is the third writer of these fields, and
        the only one that was unbounded: ``set hold_time inf`` stored a
        value that broke ``GET_SETTINGS`` for every client for the life of
        the process and parked the door in DOOR_HOLDING - the same damage
        the wire path is hardened against.

        Raises:
            ScriptError: If the value is not a finite number in range.
        """
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ScriptError(f"{name} must be a number, got {sanitize_text(value)!r}") from None
        if not math.isfinite(number):
            raise ScriptError(f"{name} must be a finite number, got {number!r}")
        if not minimum <= number <= maximum:
            raise ScriptError(f"{name} must be between {minimum} and {maximum}, got {number!r}")
        return number

    @staticmethod
    def _script_bool(value: object) -> bool:
        """Coerce a script-supplied boolean parameter, fail-closed.

        The third writer of these flags, and the one that was reading them
        with plain truthiness: unquoted YAML ``false`` parses to a real
        bool, but a quoted or templated ``"false"``/``"0"``/``"off"`` is a
        non-empty string, so ``enabled: "0"`` produced an **enabled**
        schedule. Routed through the library's ``coerce_schedule_flag``
        (``make_bool`` underneath) so this file stops being a third
        implementation of one concept, with the script-only
        ``enabled``/``disabled`` spellings layered on top - script
        vocabulary is a front-end concern and must not widen what the
        *protocol* parsers accept.
        """
        if isinstance(value, str):
            alias = _SCRIPT_BOOL_ALIASES.get(value.strip().lower())
            if alias is not None:
                return alias
        return coerce_schedule_flag(value, "script boolean")

    def _set_value(self, name: str, value: str):
        """Set a state value."""
        state = self.simulator.state
        name = name.lower().replace("-", "_")

        # One coercer for the whole file (and shared with the library),
        # rather than a third hand-rolled truthiness rule.
        bool_value = self._script_bool(value)

        if name == "power":
            state.power = bool_value
        elif name == "auto":
            state.auto = bool_value
        elif name == "battery":
            # Through the simulator so the 0-100 clamp and the low-battery
            # notification threshold logic apply, exactly as they do for
            # the operator's `battery` command.
            self.simulator.set_battery(int(self._script_number(value, "battery", 0, 100)))
        elif name == "hold_time":
            # hold_time is a float everywhere else (the protocol carries
            # centiseconds), so fractional values must be accepted here.
            state.hold_time = self._script_number(value, "hold_time", 0, MAX_SCRIPT_HOLD_TIME)
        elif name == "inside":
            state.inside = bool_value
        elif name == "outside":
            state.outside = bool_value
        elif name == "autoretract":
            state.autoretract = bool_value
        elif name == "safety_lock":
            state.safety_lock = bool_value
        elif name == "cmd_lockout":
            state.cmd_lockout = bool_value
        else:
            raise ScriptError(f"Unknown setting: {name}. Use: {', '.join(SET_SETTINGS)}")

    def _toggle_value(self, name: str):
        """Toggle a boolean state value."""
        state = self.simulator.state
        name = name.lower().replace("-", "_")

        if name == "power":
            state.power = not state.power
        elif name == "auto":
            state.auto = not state.auto
        elif name == "inside":
            state.inside = not state.inside
        elif name == "outside":
            state.outside = not state.outside
        elif name == "autoretract":
            state.autoretract = not state.autoretract
        elif name == "safety_lock":
            state.safety_lock = not state.safety_lock
        elif name == "cmd_lockout":
            state.cmd_lockout = not state.cmd_lockout
        else:
            raise ScriptError(
                f"Unknown setting to toggle: {name}"
                f"{_other_table_hint(name, SET_SETTINGS, 'set')}. "
                f"Use: {', '.join(TOGGLE_SETTINGS)}"
            )

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

    def _assert_condition(self, condition: str, expected: object):
        """Assert a condition equals an expected value."""
        state = self.simulator.state
        condition = condition.lower().replace("-", "_")

        actual: object = None

        if condition == "door_status":
            actual = state.door_status
        elif condition == "power":
            actual = "on" if state.power else "off"
        elif condition == "auto":
            actual = "on" if state.auto else "off"
        elif condition == "battery":
            actual = str(state.battery_percent)
        elif condition == "hold_time":
            actual = state.hold_time
        elif condition == "inside":
            actual = "enabled" if state.inside else "disabled"
        elif condition == "outside":
            actual = "enabled" if state.outside else "disabled"
        elif condition == "autoretract":
            actual = "on" if state.autoretract else "off"
        elif condition == "safety_lock":
            actual = "on" if state.safety_lock else "off"
        elif condition == "cmd_lockout":
            actual = "on" if state.cmd_lockout else "off"
        elif condition == "total_open_cycles":
            actual = str(state.total_open_cycles)
        elif condition == "total_auto_retracts":
            actual = str(state.total_auto_retracts)
        else:
            raise ScriptError(
                f"Unknown assertion condition: {condition}"
                f"{_other_table_hint(condition, WAIT_FOR_CONDITIONS, 'wait_for')}. "
                f"Use: {', '.join(ASSERT_CONDITIONS)}"
            )

        # Normalize both sides to text, then compare case-insensitively.
        expected_text = self._assert_text(expected)
        actual_text = self._assert_text(actual)

        if actual_text.casefold() != expected_text.casefold():
            raise ScriptAssertionError(
                f"{condition}: expected '{expected_text}', got '{actual_text}'"
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


def script_escapes_directory(path: Path, base: Path) -> bool:
    """Whether ``path`` resolves outside its own ``base`` directory.

    The single containment rule, shared by the loader and by every surface
    that advertises a script. It used to live only in the loader, so a
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
            if script_escapes_directory(path, base):
                # DEBUG, not WARNING: tab completion calls this on every
                # keystroke. `run <name>` explains the refusal in full.
                logger.debug(
                    "Not listing %s: it resolves outside %s",
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
            logger.warning(f"Failed to load script {sanitize_text(name)}: {error}")
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
        raise ScriptError(f"Unknown script: {name}. Available: {available}")
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
