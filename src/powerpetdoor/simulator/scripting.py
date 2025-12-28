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
  - obstruction: Simulate obstruction during close
  - pet_presence: Set pet in doorway (extends hold time)
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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

if TYPE_CHECKING:
    from .server import DoorSimulator

from ..const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
)
from .state import Schedule

logger = logging.getLogger(__name__)


class ScriptError(Exception):
    """Error during script execution."""
    pass


class AssertionFailed(ScriptError):
    """Assertion in script failed."""
    pass


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
    source_file: Optional[str] = None

    @classmethod
    def from_yaml(cls, content: str, source_file: Optional[str] = None) -> "Script":
        """Parse a script from YAML content."""
        if not YAML_AVAILABLE:
            raise ScriptError("PyYAML is required for script support: pip install pyyaml")

        data = yaml.safe_load(content)
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
        """Load a script from a YAML file."""
        content = path.read_text()
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
            params = {}

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
    """Executes scripts against a simulator."""

    def __init__(self, simulator: "DoorSimulator"):
        self.simulator = simulator
        self.running = False
        self._stop_requested = False

    async def run(self, script: Script, verbose: bool = True) -> bool:
        """Execute a script.

        Returns True if all steps (including assertions) passed.
        """
        self.running = True
        self._stop_requested = False

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

        except AssertionFailed as e:
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
            self.simulator.set_pet_in_doorway(True)

        elif action == "pet_off":
            self.simulator.set_pet_in_doorway(False)

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
                inside=True,   # Allow inside sensor
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
        """Wait for a condition to become true."""
        state = self.simulator.state
        start = time.time()

        while time.time() - start < timeout:
            if self._stop_requested:
                raise ScriptError("Script stopped while waiting")

            if self._check_condition(condition):
                return

            await asyncio.sleep(0.1)

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
            state.hold_time = int(value)
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
            raise AssertionFailed(
                f"{condition}: expected '{expected}', got '{actual}'"
            )


# Directory containing built-in script files
SCRIPTS_DIR = Path(__file__).parent / "scripts"


def _get_script_files() -> dict[str, Path]:
    """Get all available script files from the scripts directory."""
    scripts = {}
    if SCRIPTS_DIR.exists():
        for path in SCRIPTS_DIR.glob("*.yaml"):
            scripts[path.stem] = path
        for path in SCRIPTS_DIR.glob("*.yml"):
            scripts[path.stem] = path
    return scripts


def get_builtin_script(name: str) -> Script:
    """Get a built-in script by name.

    Scripts are loaded from YAML files in the 'scripts' directory.
    """
    script_files = _get_script_files()
    if name not in script_files:
        available = ", ".join(sorted(script_files.keys()))
        raise ScriptError(f"Unknown built-in script: {name}. Available: {available}")
    return Script.from_file(script_files[name])


def list_builtin_scripts() -> list[tuple[str, str]]:
    """List all built-in scripts with descriptions.

    Returns a list of (name, description) tuples.
    """
    result = []
    for name, path in sorted(_get_script_files().items()):
        try:
            script = Script.from_file(path)
            result.append((name, script.description))
        except Exception as e:
            logger.warning(f"Failed to load script {name}: {e}")
            result.append((name, f"(Error loading: {e})"))
    return result
