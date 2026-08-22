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
from .state import Schedule

logger = logging.getLogger(__name__)


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

    async def run(
        self, script: Script, verbose: bool = True, *, queue_if_busy: bool = True
    ) -> bool:
        """Execute a script, waiting for any in-flight script to finish.

        Args:
            script: The script to run.
            verbose: Log each step.
            queue_if_busy: When True (the default, used by the script
                queue), wait for the running script to finish. When False,
                refuse immediately rather than queue - callers that report
                a synchronous pass/fail must not silently block.

        Returns:
            True if all steps (including assertions) passed.

        Raises:
            ScriptError: If another script is running and ``queue_if_busy``
                is False.
        """
        if not queue_if_busy and self.busy:
            raise ScriptError(f"Another script is already running: {self.current_script}")
        async with self._lock:
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
            logger.info(f"Running script: {script.name}")
            if script.description:
                logger.info(f"  {script.description}")

        try:
            for step in script.steps:
                if self._stop_requested:
                    logger.info("Script stopped by request")
                    return False

                if verbose:
                    logger.info(f"  Step {step.line_number}: {step}")

                await self._execute_step(step)

            if verbose:
                logger.info(f"Script '{script.name}' completed successfully")
            return True

        except ScriptAssertionError as e:
            logger.error(f"Assertion failed at step {step.line_number}: {e}")
            return False
        except ScriptError as e:
            logger.error(f"Script error at step {step.line_number}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error at step {step.line_number}: {e}")
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

        state = self.simulator.state

        if action == "trigger_sensor" or action == "trigger":
            sensor = params.get("sensor", "inside")
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
            duration = float(params.get("duration", 0.5))
            self.simulator.activate_sensor("inside", duration)

        elif action == "outside":
            # Activate outside sensor with optional duration
            duration = float(params.get("duration", 0.5))
            self.simulator.activate_sensor("outside", duration)

        elif action == "open":
            hold = params.get("hold", False)
            await self.simulator.open_door(hold=hold)

        elif action == "close":
            await self.simulator.close_door()

        elif action == "wait":
            seconds = float(params.get("seconds", 1.0))
            await asyncio.sleep(seconds)

        elif action == "wait_for":
            condition = params.get("condition", "door_closed")
            timeout = float(params.get("timeout", 30.0))
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
            logger.info(f"  [SCRIPT] {message}")

        elif action == "add_schedule":
            index = int(params.get("index", 1))
            enabled = params.get("enabled", True)
            # Create a schedule that allows BOTH sensors 24/7 (midnight to midnight)
            # This ensures tests pass regardless of the time of day.
            # Note: Each schedule entry controls specific sensors via inside/outside flags.
            schedule = Schedule(
                index=index,
                enabled=enabled,
                days_of_week=[1, 1, 1, 1, 1, 1, 1],  # All days
                inside=True,  # Allow inside sensor
                outside=True,  # Allow outside sensor
                start_hour=0,
                start_min=0,
                end_hour=23,
                end_min=59,
            )
            self.simulator.add_schedule(schedule)

        elif action == "remove_schedule":
            index = int(params.get("index", 1))
            self.simulator.remove_schedule(index)

        elif action == "battery":
            percent = int(params.get("percent", params.get("value", 50)))
            self.simulator.set_battery(percent)

        else:
            raise ScriptError(f"Unknown action: {action}")

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
            raise ScriptError(f"Unknown condition: {condition}")

    def _set_value(self, name: str, value: str):
        """Set a state value."""
        state = self.simulator.state
        name = name.lower().replace("-", "_")

        bool_value = value.lower() in ("true", "1", "on", "yes", "enabled")

        if name == "power":
            state.power = bool_value
        elif name == "auto":
            state.auto = bool_value
        elif name == "battery":
            state.battery_percent = int(value)
        elif name == "hold_time":
            # hold_time is a float everywhere else (the protocol carries
            # centiseconds), so fractional values must be accepted here.
            state.hold_time = float(value)
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
            raise ScriptError(f"Unknown setting: {name}")

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
            raise ScriptError(f"Unknown setting to toggle: {name}")

    def _assert_condition(self, condition: str, expected: str):
        """Assert a condition equals an expected value."""
        state = self.simulator.state
        condition = condition.lower().replace("-", "_")

        actual = None

        if condition == "door_status":
            actual = state.door_status
        elif condition == "power":
            actual = "on" if state.power else "off"
        elif condition == "auto":
            actual = "on" if state.auto else "off"
        elif condition == "battery":
            actual = str(state.battery_percent)
        elif condition == "hold_time":
            actual = str(state.hold_time)
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
            raise ScriptError(f"Unknown assertion condition: {condition}")

        # Normalize expected value
        expected_normalized = expected.upper() if condition == "door_status" else expected.lower()
        actual_normalized = actual.upper() if condition == "door_status" else actual.lower()

        if actual_normalized != expected_normalized:
            raise ScriptAssertionError(f"{condition}: expected '{expected}', got '{actual}'")


# Directory containing built-in script files
SCRIPTS_DIR = Path(__file__).parent / "scripts"

# Extra scripts directory registered by --scripts-dir. Module-level so the
# name resolver, `list`, the unknown-script hint, and tab completion all
# see the same set of runnable scripts (the completer is referenced from an
# ArgSpec and has no handler context of its own).
_extra_scripts_dir: Path | None = None


def set_extra_scripts_dir(directory: str | Path | None) -> None:
    """Register (or clear) the extra scripts directory from ``--scripts-dir``."""
    global _extra_scripts_dir
    _extra_scripts_dir = Path(directory) if directory else None


def _script_files_in(directory: Path) -> dict[str, Path]:
    """Map script name -> path for the YAML files directly in ``directory``."""
    scripts: dict[str, Path] = {}
    if directory.exists():
        for pattern in ("*.yaml", "*.yml"):
            for path in directory.glob(pattern):
                scripts[path.stem] = path
    return scripts


def _get_script_files() -> dict[str, Path]:
    """Get all available script files from the built-in scripts directory."""
    return _script_files_in(SCRIPTS_DIR)


def get_extra_script_files() -> dict[str, Path]:
    """Script name -> path for the registered ``--scripts-dir``, if any."""
    return _script_files_in(_extra_scripts_dir) if _extra_scripts_dir else {}


def _describe_scripts(script_files: dict[str, Path]) -> list[tuple[str, str]]:
    """Build sorted (name, description) pairs for the given script files."""
    result = []
    for name, path in sorted(script_files.items()):
        try:
            script = Script.from_file(path)
            result.append((name, script.description))
        except Exception as e:
            logger.warning(f"Failed to load script {name}: {e}")
            result.append((name, f"(Error loading: {e})"))
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
        # Available: list were built-ins too (T3).
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


def script_completer(prefix: str = "") -> list[tuple[str, str]]:
    """Return list of (script_name, description) for tab completion.

    This function is designed to be used as an ArgSpec completer.
    Returns both builtin scripts and YAML files based on the prefix path.

    Args:
        prefix: The partial path/name being completed (e.g., "", "basic", "./scr")

    Gracefully handles missing PyYAML by returning just script names.
    """
    result = []

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

        # Add builtin scripts, then any registered --scripts-dir scripts
        for script_files, label in (
            (_get_script_files(), "(builtin)"),
            (get_extra_script_files(), "(scripts-dir)"),
        ):
            if YAML_AVAILABLE:
                for name, path in sorted(script_files.items()):
                    try:
                        script = Script.from_file(path)
                        result.append((name, script.description or label))
                    except Exception:
                        result.append((name, label))
            else:
                for name in sorted(script_files.keys()):
                    result.append((name, label))

        # Add YAML files from current directory
        cwd = Path.cwd()
        for pattern in ("*.yaml", "*.yml"):
            for path in cwd.glob(pattern):
                if path.is_file():
                    result.append((path.name, "(local file)"))

        # Add subdirectories that might contain scripts
        for subdir in cwd.iterdir():
            if subdir.is_dir() and not subdir.name.startswith("."):
                # Check if it has any yaml files
                has_yaml = any(subdir.glob("*.yaml")) or any(subdir.glob("*.yml"))
                if has_yaml:
                    result.append((subdir.name + "/", "(directory)"))

    return result
