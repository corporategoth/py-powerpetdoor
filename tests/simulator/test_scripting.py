# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator scripting module (scripting.py)."""

from __future__ import annotations

import asyncio

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.scripting import (
    YAML_AVAILABLE,
    AssertionFailed,
    Script,
    ScriptAssertionError,
    ScriptError,
    ScriptRunner,
    ScriptStep,
    get_builtin_script,
    list_builtin_scripts,
)

# Skip marker for tests that require PyYAML
requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")


def test_assertion_failed_alias():
    """AssertionFailed remains as a compatibility alias (renamed for N818)."""
    assert AssertionFailed is ScriptAssertionError
    assert issubclass(ScriptAssertionError, ScriptError)


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
        script = Script.from_simple_commands(
            [
                "log Starting test",
                "set hold_time 1",
                "assert door_status DOOR_CLOSED",
            ],
            name="Simple Test",
        )

        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_trigger_sensor_action(self, runner, simulator):
        """trigger_sensor action should work."""
        script = Script.from_simple_commands(
            [
                "trigger inside",
                "wait 0.2",
            ]
        )
        await runner.run(script, verbose=False)
        # Door should be opening or open
        assert simulator.state.door_status != DOOR_STATE_CLOSED

    @pytest.mark.asyncio
    async def test_set_action(self, runner, simulator):
        """set action should change state."""
        script = Script.from_simple_commands(
            [
                "set battery 42",
                "set hold_time 15",
            ]
        )
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
        script = Script.from_simple_commands(
            [
                "trigger inside",
                "wait_for door_open 5",
            ]
        )
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_timeout(self, runner, simulator):
        """wait_for should timeout if condition never becomes true."""
        # Don't trigger door, but wait for it to open
        script = Script.from_simple_commands(
            [
                "wait_for door_open 0.5",  # Short timeout
            ]
        )
        result = await runner.run(script, verbose=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_non_status_condition_already_true(self, runner, simulator):
        """wait_for on a non-status condition returns when already true."""
        simulator.state.power = False
        script = Script.from_simple_commands(["wait_for power_off 5"])
        result = await runner.run(script, verbose=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_non_status_condition_timeout(self, runner, simulator):
        """wait_for on a non-status condition times out when never true."""
        script = Script.from_simple_commands(["wait_for power_off 0.3"])
        result = await runner.run(script, verbose=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_unknown_condition_fails(self, runner, simulator):
        """wait_for on an unknown condition fails the script."""
        script = Script.from_simple_commands(["wait_for bogus_condition 1"])
        result = await runner.run(script, verbose=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_stop_script_interrupts_wait_for(self, runner, simulator):
        """stop() must interrupt a wait_for immediately (no wall-clock wait)."""
        # The door never reaches KEEPUP, so only stop() can end this wait
        script = Script.from_simple_commands(["wait_for door_keepup 30"])

        task = asyncio.create_task(runner.run(script, verbose=False))
        # Let the runner enter the wait_for step
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        runner.stop()

        result = await asyncio.wait_for(task, timeout=2.0)
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
