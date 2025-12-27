# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator scripting module (scripting.py)."""
from __future__ import annotations

import asyncio
import json

import pytest

from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.scripting import (
    Script,
    ScriptStep,
    ScriptRunner,
    ScriptError,
    AssertionFailed,
    get_builtin_script,
    list_builtin_scripts,
    YAML_AVAILABLE,
)
from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
)


# Skip marker for tests that require PyYAML
requires_yaml = pytest.mark.skipif(
    not YAML_AVAILABLE,
    reason="PyYAML not installed"
)


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def fast_timing():
    """Create a fast timing config for unit tests."""
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(fast_timing):
    """Create and start a simulator with fast timing for unit tests."""
    state = DoorSimulatorState(timing=fast_timing, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def runner(simulator):
    """Create a script runner with fast timing."""
    return ScriptRunner(simulator)


# ============================================================================
# ScriptStep Tests
# ============================================================================

class TestScriptStep:
    """Tests for ScriptStep dataclass."""

    def test_basic_step(self):
        """Create a basic step."""
        step = ScriptStep(action="trigger_sensor", params={"sensor": "inside"})
        assert step.action == "trigger_sensor"
        assert step.params["sensor"] == "inside"

    def test_step_str_with_params(self):
        """String representation with params."""
        step = ScriptStep(action="wait", params={"seconds": 5})
        assert "wait" in str(step)
        assert "5" in str(step)

    def test_step_str_without_params(self):
        """String representation without params."""
        step = ScriptStep(action="close")
        assert str(step) == "close"


# ============================================================================
# Script Tests
# ============================================================================

class TestScript:
    """Tests for Script class."""

    @requires_yaml
    def test_from_yaml_basic(self):
        """Parse a basic YAML script."""
        yaml_content = """
name: "Test Script"
description: "A test"
steps:
  - action: trigger_sensor
    sensor: inside
  - action: wait
    seconds: 1
"""
        script = Script.from_yaml(yaml_content)
        assert script.name == "Test Script"
        assert script.description == "A test"
        assert len(script.steps) == 2
        assert script.steps[0].action == "trigger_sensor"
        assert script.steps[1].action == "wait"

    @requires_yaml
    def test_from_yaml_simple_actions(self):
        """Parse simple string actions."""
        yaml_content = """
name: "Simple"
steps:
  - close
  - open
"""
        script = Script.from_yaml(yaml_content)
        assert len(script.steps) == 2
        assert script.steps[0].action == "close"
        assert script.steps[1].action == "open"

    @requires_yaml
    def test_from_yaml_missing_action(self):
        """Should raise error for step without action."""
        yaml_content = """
name: "Bad"
steps:
  - sensor: inside
"""
        with pytest.raises(ScriptError, match="missing 'action'"):
            Script.from_yaml(yaml_content)

    @requires_yaml
    def test_from_yaml_invalid_step(self):
        """Should raise error for invalid step format."""
        yaml_content = """
name: "Bad"
steps:
  - 123
"""
        with pytest.raises(ScriptError, match="invalid step format"):
            Script.from_yaml(yaml_content)

    @requires_yaml
    def test_from_yaml_not_dict(self):
        """Should raise error if root is not dict."""
        with pytest.raises(ScriptError, match="must be a YAML dictionary"):
            Script.from_yaml("just a string")

    def test_from_simple_commands(self):
        """Parse simple command strings."""
        commands = [
            "trigger inside",
            "wait 2",
            "assert door_status DOOR_CLOSED",
            "set battery 50",
        ]
        script = Script.from_simple_commands(commands, name="Test")
        assert len(script.steps) == 4
        assert script.steps[0].action == "trigger"
        assert script.steps[0].params["sensor"] == "inside"
        assert script.steps[1].params["seconds"] == 2.0
        assert script.steps[2].params["condition"] == "door_status"
        assert script.steps[3].params["value"] == "50"


# ============================================================================
# ScriptRunner Tests
# ============================================================================

class TestScriptRunner:
    """Tests for ScriptRunner class."""

    @pytest.mark.asyncio
    async def test_run_simple_script(self, runner, simulator):
        """Run a simple script successfully."""
        script = Script.from_simple_commands([
            "log Starting test",
            "set hold_time 1",
            "assert door_status DOOR_CLOSED",
        ], name="Simple Test")

        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_trigger_sensor_action(self, runner, simulator):
        """trigger_sensor action should work."""
        script = Script.from_simple_commands([
            "trigger inside",
            "wait 0.2",
        ])
        await runner.run(script, verbose=False)
        # Door should be opening or open
        assert simulator.state.door_status != DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_set_action(self, runner, simulator):
        """set action should change state."""
        script = Script.from_simple_commands([
            "set battery 42",
            "set hold_time 15",
        ])
        await runner.run(script, verbose=False)
        assert simulator.state.battery_percent == 42
        assert simulator.state.hold_time == 15

    @pytest.mark.asyncio
    async def test_toggle_action(self, runner, simulator):
        """toggle action should flip boolean state."""
        original_power = simulator.state.power
        script = Script.from_simple_commands(["toggle power"])
        await runner.run(script, verbose=False)
        assert simulator.state.power != original_power

    @pytest.mark.asyncio
    async def test_assert_success(self, runner, simulator):
        """assert action should pass when condition matches."""
        simulator.state.battery_percent = 75
        script = Script.from_simple_commands(["assert battery 75"])
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_assert_failure(self, runner, simulator):
        """assert action should fail when condition doesn't match."""
        simulator.state.battery_percent = 75
        script = Script.from_simple_commands(["assert battery 50"])
        result = await runner.run(script, verbose=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_condition(self, runner, simulator):
        """wait_for should wait until condition is true."""
        # Trigger door open, then wait for it to close
        simulator.state.hold_time = 1
        script = Script.from_simple_commands([
            "trigger inside",
            "wait_for door_open 5",
        ])
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_timeout(self, runner, simulator):
        """wait_for should timeout if condition never becomes true."""
        # Don't trigger door, but wait for it to open
        script = Script.from_simple_commands([
            "wait_for door_open 0.5",  # Short timeout
        ])
        result = await runner.run(script, verbose=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_stop_script(self, runner, simulator):
        """stop() should stop a running script."""
        # Use multiple steps since stop is checked at the start of each step
        script = Script.from_simple_commands([
            "wait 0.1",
            "wait 0.1",
            "wait 0.1",
            "wait 10",  # Long wait that should be skipped
        ])

        async def run_and_stop():
            task = asyncio.create_task(runner.run(script, verbose=False))
            await asyncio.sleep(0.25)  # Let first two waits complete
            runner.stop()
            return await task

        result = await run_and_stop()
        assert result is False

    @pytest.mark.asyncio
    async def test_unknown_action_fails(self, runner, simulator):
        """Unknown action should fail the script."""
        script = Script(
            name="Bad",
            steps=[ScriptStep(action="nonexistent_action")],
        )
        result = await runner.run(script, verbose=False)
        assert result is False


# ============================================================================
# Built-in Script Infrastructure Tests
# ============================================================================

@requires_yaml
class TestBuiltinScriptInfrastructure:
    """Tests for built-in script loading infrastructure.

    Individual script tests are in tests/simulator/scripts/test_*.py
    """

    def test_list_builtin_scripts(self):
        """Should list available built-in scripts."""
        scripts = list_builtin_scripts()
        assert len(scripts) > 0
        # Should have name and description tuples
        for name, description in scripts:
            assert isinstance(name, str)
            assert isinstance(description, str)

    def test_get_unknown_script_raises(self):
        """Should raise for unknown script name."""
        with pytest.raises(ScriptError, match="Unknown built-in script"):
            get_builtin_script("nonexistent_script_xyz")

    @requires_yaml
    def test_all_builtin_scripts_parse(self):
        """All built-in scripts should parse without errors."""
        for name, _ in list_builtin_scripts():
            script = get_builtin_script(name)
            assert script.name is not None
            assert len(script.steps) > 0
