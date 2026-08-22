# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator scripting module (scripting.py)."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    FIELD_HOLD_OPEN_TIME,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
    scripting,
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
    script_completer,
)

# Skip marker for tests that require PyYAML
requires_yaml = pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")

SCRIPT_LOGGER = "powerpetdoor.simulator.scripting"


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
    """Create a simulator with fast timing for unit tests.

    The TCP server is not started - the script runner drives the
    simulator API directly. stop() still cleans up engine tasks.
    """
    state = DoorSimulatorState(timing=fast_timing, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    yield sim
    await sim.stop()


@pytest.fixture
async def runner(simulator):
    """Create a script runner with fast timing."""
    return ScriptRunner(simulator)


def make_runner(**state_kwargs) -> ScriptRunner:
    """Create a runner over a fresh (unstarted) simulator for sync tests."""
    return ScriptRunner(DoorSimulator(port=0, state=DoorSimulatorState(**state_kwargs)))


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
        assert str(step) == "wait(seconds=5)"

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
        with pytest.raises(ScriptError, match="Step 1: missing 'action' field"):
            Script.from_yaml(yaml_content)

    @requires_yaml
    def test_from_yaml_invalid_step(self):
        """Should raise error for invalid step format."""
        yaml_content = """
name: "Bad"
steps:
  - 123
"""
        with pytest.raises(ScriptError, match="Step 1: invalid step format"):
            Script.from_yaml(yaml_content)

    @requires_yaml
    def test_from_yaml_not_dict(self):
        """Should raise error if root is not dict."""
        with pytest.raises(ScriptError, match="must be a YAML dictionary"):
            Script.from_yaml("just a string")

    @requires_yaml
    def test_from_yaml_malformed_yaml(self):
        """Malformed YAML is wrapped in a ScriptError, not a yaml error."""
        with pytest.raises(ScriptError, match="Invalid script YAML"):
            Script.from_yaml("steps: [unclosed")

    @requires_yaml
    def test_from_yaml_steps_not_a_list(self):
        """A non-list 'steps' value raises the exact error."""
        with pytest.raises(ScriptError, match="'steps' must be a list"):
            Script.from_yaml("name: Bad\nsteps: 5")

    def test_from_yaml_requires_pyyaml(self, monkeypatch):
        """Without PyYAML, from_yaml raises the install hint."""
        monkeypatch.setattr(scripting, "YAML_AVAILABLE", False)
        with pytest.raises(
            ScriptError, match="PyYAML is required for script support: pip install pyyaml"
        ):
            Script.from_yaml("name: x")

    def test_yaml_import_guard(self):
        """The import guard sets YAML_AVAILABLE=False when PyYAML is missing."""
        import coverage

        code = (
            "import sys; sys.modules['yaml'] = None; "
            "import powerpetdoor.simulator.scripting as s; "
            "assert s.YAML_AVAILABLE is False"
        )
        project_root = Path(__file__).parents[2]
        env = os.environ.copy()
        if coverage.Coverage.current() is not None:
            # Record the subprocess's execution of the import guard in the
            # parent's coverage data (coverage.py process_startup hook).
            env["COVERAGE_PROCESS_START"] = str(project_root / "pyproject.toml")
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_root,
            env=env,
        )
        assert result.returncode == 0, result.stderr

    @requires_yaml
    def test_from_file_loads_and_records_source(self, tmp_path):
        """from_file parses the file and records its path."""
        path = tmp_path / "demo.yaml"
        path.write_text("name: Demo\nsteps:\n  - close\n")
        script = Script.from_file(path)
        assert script.name == "Demo"
        assert script.source_file == str(path)

    @requires_yaml
    def test_from_file_missing_raises_scripterror(self, tmp_path):
        """A missing script file raises ScriptError (not raw OSError)."""
        with pytest.raises(ScriptError, match="Cannot read script file"):
            Script.from_file(tmp_path / "missing.yaml")

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

    def test_from_simple_commands_defaults_and_blank_lines(self):
        """Blank commands are skipped; omitted arguments use defaults."""
        script = Script.from_simple_commands(
            ["", "  ", "trigger", "wait", "wait_for", "set", "toggle", "assert", "log a b"]
        )
        actions = {step.action: step.params for step in script.steps}
        assert len(script.steps) == 7  # the two blank commands are dropped
        assert actions["trigger"] == {"sensor": "inside"}
        assert actions["wait"] == {"seconds": 1.0}
        assert actions["wait_for"] == {"condition": "door_closed", "timeout": 30.0}
        assert actions["set"] == {"name": "", "value": ""}
        assert actions["toggle"] == {"name": ""}
        assert actions["assert"] == {"condition": "", "equals": ""}
        assert actions["log"] == {"message": "a b"}

    def test_from_simple_commands_unlisted_action_gets_no_params(self):
        """Commands outside the parser's list pass through with empty params."""
        script = Script.from_simple_commands(["battery 42"])
        assert len(script.steps) == 1
        assert script.steps[0].action == "battery"
        assert script.steps[0].params == {}

    def test_from_simple_commands_remaining_forms(self):
        """open/close/schedule/pet commands parse to the expected params."""
        script = Script.from_simple_commands(
            [
                "open hold",
                "open",
                "close",
                "obstruction",
                "pet_on",
                "pet_off",
                "add_schedule 4",
                "add_schedule",
                "remove_schedule 2",
                "remove_schedule",
            ]
        )
        params = [step.params for step in script.steps]
        assert params == [
            {"hold": True},
            {"hold": False},
            {},
            {},
            {},
            {},
            {"index": 4},
            {"index": 1},
            {"index": 2},
            {"index": 1},
        ]


# ============================================================================
# ScriptRunner Tests
# ============================================================================


class TestScriptRunner:
    """Tests for ScriptRunner class."""

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

    async def test_run_verbose_logs_script_and_steps(self, runner, simulator, caplog):
        """verbose=True logs the name, description, steps, and completion."""
        script = Script(
            name="Verbose",
            description="A described script",
            steps=[ScriptStep(action="log", params={"message": "hi"}, line_number=1)],
        )
        with caplog.at_level(logging.INFO, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=True)

        assert result is True
        messages = [rec.getMessage() for rec in caplog.records]
        assert "Running script: Verbose" in messages
        assert "  A described script" in messages
        assert "  Step 1: log(message=hi)" in messages
        assert "  [SCRIPT] hi" in messages
        assert "Script 'Verbose' completed successfully" in messages

    async def test_run_verbose_without_description(self, runner, simulator, caplog):
        """verbose=True with no description skips the description line."""
        script = Script(
            name="Bare",
            steps=[ScriptStep(action="log", params={"message": "x"}, line_number=1)],
        )
        with caplog.at_level(logging.INFO, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=True)

        assert result is True
        messages = [rec.getMessage() for rec in caplog.records]
        assert "Running script: Bare" in messages
        # No indented description line was logged before the first step
        assert messages[messages.index("Running script: Bare") + 1] == "  Step 1: log(message=x)"

    async def test_trigger_sensor_action(self, runner, simulator):
        """trigger_sensor opens the door; wait_for observes it deterministically."""
        script = Script.from_simple_commands(
            [
                "trigger inside",
                "wait_for door_open 5",
            ]
        )
        result = await runner.run(script, verbose=False)
        assert result is True
        # hold_time has not expired: the door is holding open
        assert simulator.state.door_status == DOOR_STATE_HOLDING

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

    async def test_toggle_action(self, runner, simulator):
        """toggle action should flip boolean state."""
        assert simulator.state.power is True
        script = Script.from_simple_commands(["toggle power"])
        await runner.run(script, verbose=False)
        assert simulator.state.power is False

    async def test_assert_success(self, runner, simulator):
        """assert action should pass when condition matches."""
        simulator.state.battery_percent = 75
        script = Script.from_simple_commands(["assert battery 75"])
        result = await runner.run(script, verbose=False)
        assert result is True

    async def test_assert_failure(self, runner, simulator, caplog):
        """assert action should fail the script with the exact message."""
        simulator.state.battery_percent = 75
        script = Script.from_simple_commands(["assert battery 50"])
        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)
        assert result is False
        assert "Assertion failed at step 1: battery: expected '50', got '75'" in caplog.text

    async def test_wait_for_condition(self, runner, simulator):
        """wait_for should wait until condition is true."""
        simulator.state.hold_time = 1
        script = Script.from_simple_commands(
            [
                "trigger inside",
                "wait_for door_open 5",
            ]
        )
        result = await runner.run(script, verbose=False)
        assert result is True

    async def test_wait_for_door_closing_condition(self, runner, simulator):
        """wait_for door_closing resolves on the first closing state."""
        simulator.state.hold_time = 0.05
        script = Script.from_simple_commands(
            [
                "trigger inside",
                "wait_for door_holding 5",
                "wait_for door_closing 5",
                f"assert door_status {DOOR_STATE_CLOSING_TOP_OPEN}",
            ]
        )
        result = await runner.run(script, verbose=False)
        assert result is True

    async def test_wait_for_timeout(self, runner, simulator, caplog):
        """wait_for should timeout if condition never becomes true."""
        script = Script.from_simple_commands(
            [
                "wait_for door_open 0.2",  # Short timeout
            ]
        )
        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)
        assert result is False
        assert "Script error at step 1: Timeout waiting for condition: door_open" in caplog.text

    async def test_wait_for_non_status_condition_already_true(self, runner, simulator):
        """wait_for on a non-status condition returns when already true."""
        simulator.state.power = False
        script = Script.from_simple_commands(["wait_for power_off 5"])
        result = await runner.run(script, verbose=False)
        assert result is True

    async def test_wait_for_non_status_condition_timeout(self, runner, simulator):
        """wait_for on a non-status condition times out when never true."""
        script = Script.from_simple_commands(["wait_for power_off 0.3"])
        result = await runner.run(script, verbose=False)
        assert result is False

    async def test_wait_for_non_status_condition_becomes_true(self, runner, simulator):
        """The poll loop returns once the condition flips to true."""
        task = asyncio.create_task(runner._wait_for_condition("power_off", 5))
        await asyncio.sleep(0)  # enter the poll loop
        simulator.state.power = False
        await asyncio.wait_for(task, timeout=2.0)

    async def test_stop_interrupts_non_status_poll(self, runner, simulator):
        """stop() aborts a non-status poll with the exact error."""
        task = asyncio.create_task(runner._wait_for_condition("power_off", 5))
        await asyncio.sleep(0)  # enter the poll loop
        runner.stop()
        with pytest.raises(ScriptError, match="Script stopped while waiting"):
            await asyncio.wait_for(task, timeout=2.0)

    async def test_stop_before_wait_raises_immediately(self, runner, simulator):
        """A wait_for started after stop() fails without waiting."""
        runner._stop_requested = True
        with pytest.raises(ScriptError, match="Script stopped while waiting"):
            await runner._wait_for_condition("door_closed", 1)

    async def test_wait_for_unknown_condition_fails(self, runner, simulator, caplog):
        """wait_for on an unknown condition fails the script."""
        script = Script.from_simple_commands(["wait_for bogus_condition 1"])
        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)
        assert result is False
        assert "Unknown condition: bogus_condition" in caplog.text

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

    async def test_engine_stop_fails_status_wait(self, runner, simulator):
        """Stopping the engine cancels the status waiter; the wait errors out."""
        task = asyncio.create_task(runner._wait_for_condition("door_keepup", 5))
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # enter the status wait

        await simulator.engine.stop()
        with pytest.raises(ScriptError, match="Timeout waiting for condition: door_keepup"):
            await asyncio.wait_for(task, timeout=2.0)

    async def test_stop_between_steps_aborts_script(self, runner, simulator, caplog):
        """A stop() during one step prevents the next step from running."""

        async def stopping_open(hold=False):
            runner.stop()

        simulator.open_door = stopping_open
        script = Script.from_simple_commands(["open", "log after-stop"])

        with caplog.at_level(logging.INFO, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)

        assert result is False
        messages = [rec.getMessage() for rec in caplog.records]
        assert "Script stopped by request" in messages
        assert "  [SCRIPT] after-stop" not in messages

    async def test_stop_during_the_last_step_fails_the_run(self, runner, simulator, caplog):
        """A stop landing during the FINAL step must still report FAILED (H1).

        The stop check only ran at the top of each iteration, so a stop
        during the last step was silently discarded: the loop ended, the
        run reported `Script PASSED` and a ctl wait-run exited 0 - the
        opposite of what `stop` documents, from the one command whose whole
        purpose is a trustworthy exit code.
        """

        async def stopping_open(hold=False):
            runner.stop()

        simulator.open_door = stopping_open
        script = Script.from_simple_commands(["open"])  # the stop is the last step

        with caplog.at_level(logging.INFO, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)

        assert result is False
        messages = [rec.getMessage() for rec in caplog.records]
        assert "Script stopped by request" in messages

    async def test_stop_during_the_last_step_of_a_longer_script_fails_the_run(
        self, runner, simulator
    ):
        """Same defect with steps before the last one (the two-step case, H1)."""

        async def stopping_close():
            runner.stop()

        simulator.close_door = stopping_close
        script = Script.from_simple_commands(["log first", "close"])

        assert await runner.run(script, verbose=False) is False

    async def test_stop_interrupts_a_plain_wait(self, runner, simulator):
        """`wait N` is raced against the stop event, not slept through (H1).

        An uninterruptible wait made the "stop lands during the final step"
        window as long as the final wait.
        """
        script = Script.from_simple_commands(["wait 30"])

        task = asyncio.create_task(runner.run(script, verbose=False))
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # let the runner enter the wait step
        runner.stop()

        async with asyncio.timeout(2.0):
            assert await task is False

    async def test_wait_still_waits_when_no_stop_is_requested(self, runner, simulator):
        """The interruptible wait must still actually wait."""
        loop = asyncio.get_running_loop()
        started = loop.time()

        assert await runner.run(Script.from_simple_commands(["wait 0.05"]), verbose=False) is True

        assert loop.time() - started >= 0.05

    async def test_wait_returns_immediately_when_already_stopped(self, runner, simulator):
        """A stop requested before the wait step skips the sleep entirely."""
        runner.stop()
        loop = asyncio.get_running_loop()
        started = loop.time()

        assert await runner._sleep_or_stop(30) is None

        assert loop.time() - started < 1.0

    async def test_stop_requested_is_observable_while_running(self, runner, simulator):
        """`stop` takes effect at a step boundary, so the pending state shows (L3)."""
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_step(step):
            entered.set()
            await release.wait()

        runner._execute_step = blocking_step
        script = Script.from_simple_commands(["log a", "log b"])
        task = asyncio.ensure_future(runner.run(script, verbose=False))
        async with asyncio.timeout(2.0):
            await entered.wait()

        assert runner.stop_requested is False
        runner.stop()
        assert runner.stop_requested is True

        release.set()
        assert await asyncio.wait_for(task, 2.0) is False

        # The request is cleared when the next run starts, so it cannot leak
        # across runs even though the flag outlives the run that set it.
        del runner._execute_step
        assert await runner.run(Script.from_simple_commands(["log b"]), verbose=False) is True
        assert runner.stop_requested is False

    async def test_on_start_fires_once_the_run_lock_is_held(self, runner, simulator):
        """The queue consumer stops counting a run as pending when it starts (M2)."""
        seen: list[str | None] = []
        script = Script.from_simple_commands(["log hello"])

        await runner.run(script, verbose=False, on_start=lambda: seen.append(runner.current_script))

        assert seen == [script.name]

    async def test_unknown_action_fails(self, runner, simulator, caplog):
        """Unknown action should fail the script."""
        script = Script(
            name="Bad",
            steps=[ScriptStep(action="nonexistent_action", line_number=1)],
        )
        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)
        assert result is False
        assert "Script error at step 1: Unknown action: nonexistent_action" in caplog.text

    async def test_unexpected_error_fails_script(self, runner, simulator, caplog):
        """A non-ScriptError exception fails the script via the catch-all."""
        script = Script(
            name="Bad",
            steps=[ScriptStep(action="wait", params={"seconds": "abc"}, line_number=1)],
        )
        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)
        assert result is False
        assert "Unexpected error at step 1" in caplog.text


class TestScriptActions:
    """Per-action behavior of the step dispatcher."""

    async def test_obstruction_action(self, runner, simulator):
        """obstruction activates the inside sensor indefinitely."""
        result = await runner.run(Script.from_simple_commands(["obstruction"]), verbose=False)
        assert result is True
        assert simulator.state.inside_sensor_active is True
        assert simulator.state.outside_sensor_active is False

    async def test_pet_on_activates_inside_sensor(self, runner, simulator):
        """pet_on turns the inside sensor on; the closed door starts opening."""
        result = await runner.run(Script.from_simple_commands(["pet_on"]), verbose=False)
        assert result is True
        assert simulator.state.inside_sensor_active is True
        assert simulator.state.door_status == DOOR_STATE_RISING

    async def test_pet_on_when_already_active_is_noop(self, runner, simulator):
        """A second pet_on must not toggle the sensor back off."""
        simulator.state.power = False  # keep the door still; only flags matter
        result = await runner.run(Script.from_simple_commands(["pet_on", "pet_on"]), verbose=False)
        assert result is True
        assert simulator.state.inside_sensor_active is True

    async def test_pet_off_clears_inside_sensor(self, runner, simulator):
        """pet_off clears the inside sensor."""
        simulator.state.power = False
        result = await runner.run(Script.from_simple_commands(["pet_on", "pet_off"]), verbose=False)
        assert result is True
        assert simulator.state.inside_sensor_active is False

    async def test_inside_action_with_duration(self, runner, simulator):
        """The inside action activates the sensor for the given duration."""
        simulator.state.power = False
        script = Script(
            name="Inside",
            steps=[ScriptStep(action="inside", params={"duration": 0.02}, line_number=1)],
        )
        result = await runner.run(script, verbose=False)
        assert result is True
        assert simulator.state.inside_sensor_active is True
        # The tracked deactivation timer clears it
        await asyncio.gather(*simulator.engine._aux_tasks)
        assert simulator.state.inside_sensor_active is False

    async def test_outside_action_with_duration(self, runner, simulator):
        """The outside action activates the sensor for the given duration."""
        simulator.state.power = False
        script = Script(
            name="Outside",
            steps=[ScriptStep(action="outside", params={"duration": 0.02}, line_number=1)],
        )
        result = await runner.run(script, verbose=False)
        assert result is True
        assert simulator.state.outside_sensor_active is True
        await asyncio.gather(*simulator.engine._aux_tasks)
        assert simulator.state.outside_sensor_active is False

    async def test_open_hold_and_close_actions(self, runner, simulator):
        """open hold parks the door in KEEPUP; close brings it back down."""
        script = Script.from_simple_commands(
            [
                "open hold",
                "wait_for door_keepup 5",
                f"assert door_status {DOOR_STATE_KEEPUP}",
                "close",
                "wait_for door_closed 5",
            ]
        )
        result = await runner.run(script, verbose=False)
        assert result is True
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    async def test_battery_action_percent(self, runner, simulator):
        """The battery action sets the battery percentage (clamped)."""
        script = Script(
            name="Battery",
            steps=[ScriptStep(action="battery", params={"percent": 42}, line_number=1)],
        )
        result = await runner.run(script, verbose=False)
        assert result is True
        assert simulator.state.battery_percent == 42

    async def test_battery_action_value_fallback(self, runner, simulator):
        """The battery action accepts 'value' as an alternative to 'percent'."""
        script = Script(
            name="Battery",
            steps=[ScriptStep(action="battery", params={"value": 37}, line_number=1)],
        )
        result = await runner.run(script, verbose=False)
        assert result is True
        assert simulator.state.battery_percent == 37

    async def test_add_and_remove_schedule_actions(self, runner, simulator):
        """add_schedule installs a 24/7 both-sensor schedule; remove deletes it."""
        result = await runner.run(Script.from_simple_commands(["add_schedule 3"]), verbose=False)
        assert result is True
        schedule = simulator.state.schedules[3]
        assert schedule.enabled is True
        assert schedule.inside is True
        assert schedule.outside is True
        assert (schedule.start_hour, schedule.start_min) == (0, 0)
        assert (schedule.end_hour, schedule.end_min) == (23, 59)

        result = await runner.run(Script.from_simple_commands(["remove_schedule 3"]), verbose=False)
        assert result is True
        assert simulator.state.schedules == {}

    async def test_add_schedule_disabled(self, runner, simulator):
        """add_schedule honors an explicit enabled=False."""
        script = Script(
            name="Sched",
            steps=[
                ScriptStep(
                    action="add_schedule", params={"index": 2, "enabled": False}, line_number=1
                )
            ],
        )
        result = await runner.run(script, verbose=False)
        assert result is True
        assert simulator.state.schedules[2].enabled is False

    async def test_action_name_normalization(self, runner, simulator):
        """Hyphenated/uppercase action names are normalized."""
        script = Script(
            name="Norm",
            steps=[ScriptStep(action="Pet-Presence", line_number=1)],
        )
        result = await runner.run(script, verbose=False)
        assert result is True
        assert simulator.state.inside_sensor_active is True


# ============================================================================
# Condition / Set / Toggle / Assert Matrices
# ============================================================================


class TestCheckConditionMatrix:
    """_check_condition supports every documented condition token."""

    @pytest.mark.parametrize(
        ("condition", "state_kwargs", "expected"),
        [
            ("door_closed", {}, True),
            ("door_closed", {"door_status": DOOR_STATE_RISING}, False),
            ("door_open", {"door_status": DOOR_STATE_HOLDING}, True),
            ("door_open", {"door_status": DOOR_STATE_KEEPUP}, True),
            ("door_open", {}, False),
            ("door_rising", {"door_status": DOOR_STATE_RISING}, True),
            ("door_rising", {}, False),
            ("door_holding", {"door_status": DOOR_STATE_HOLDING}, True),
            ("door_holding", {"door_status": DOOR_STATE_KEEPUP}, False),
            ("door_keepup", {"door_status": DOOR_STATE_KEEPUP}, True),
            ("door_keepup", {"door_status": DOOR_STATE_HOLDING}, False),
            ("door_closing", {"door_status": DOOR_STATE_CLOSING_TOP_OPEN}, True),
            ("door_closing", {}, False),
            ("power_on", {}, True),
            ("power_on", {"power": False}, False),
            ("power_off", {"power": False}, True),
            ("power_off", {}, False),
            ("inside_enabled", {}, True),
            ("inside_enabled", {"inside": False}, False),
            ("inside_disabled", {"inside": False}, True),
            ("outside_enabled", {}, True),
            ("outside_disabled", {"outside": False}, True),
            ("outside_disabled", {}, False),
            ("autoretract_on", {}, True),
            ("autoretract_off", {"autoretract": False}, True),
            ("autoretract_off", {}, False),
            ("auto_on", {}, True),
            ("auto_off", {"auto": False}, True),
            ("auto_off", {}, False),
            ("safety_lock_on", {"safety_lock": True}, True),
            ("safety_lock_off", {}, True),
            ("safety_lock_off", {"safety_lock": True}, False),
            ("cmd_lockout_on", {"cmd_lockout": True}, True),
            ("cmd_lockout_off", {}, True),
            ("cmd_lockout_off", {"cmd_lockout": True}, False),
            # Hyphens normalize to underscores
            ("door-closed", {}, True),
        ],
    )
    def test_condition_matrix(self, condition, state_kwargs, expected):
        """Each condition evaluates to its single correct value."""
        runner = make_runner(**state_kwargs)
        assert runner._check_condition(condition) is expected

    def test_unknown_condition_raises(self):
        """Unknown conditions raise the exact error."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown condition: bogus"):
            runner._check_condition("bogus")


class TestSetValueMatrix:
    """_set_value covers every supported setting."""

    @pytest.mark.parametrize(
        ("name", "value", "attr", "expected"),
        [
            ("power", "off", "power", False),
            ("power", "on", "power", True),
            ("auto", "0", "auto", False),
            ("battery", "55", "battery_percent", 55),
            ("hold_time", "3", "hold_time", 3),
            ("inside", "false", "inside", False),
            ("outside", "no", "outside", False),
            ("autoretract", "true", "autoretract", True),
            ("safety_lock", "enabled", "safety_lock", True),
            ("cmd_lockout", "yes", "cmd_lockout", True),
            # Hyphens normalize to underscores
            ("safety-lock", "1", "safety_lock", True),
        ],
    )
    def test_set_matrix(self, name, value, attr, expected):
        """Each setting stores the parsed value."""
        runner = make_runner()
        runner._set_value(name, value)
        assert getattr(runner.simulator.state, attr) == expected

    def test_set_hold_time_accepts_fractional_seconds(self):
        """hold_time is a float everywhere else, so 1.5 must work (L6)."""
        runner = make_runner()
        runner._set_value("hold_time", "1.5")
        assert runner.simulator.state.hold_time == 1.5

    def test_set_unknown_raises(self):
        """Unknown settings raise the exact error."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown setting: bogus"):
            runner._set_value("bogus", "1")


class TestScriptWriterIsBoundedLikeTheWire:
    """The YAML script channel is the third writer of these fields (S3).

    Round 3 bounded the wire path and the CLI path was already bounded; the
    script path was not, so `set hold_time inf` re-opened exactly the
    Medium round 3 closed - GET_SETTINGS (issued by the shipped client on
    every connect and refresh) then fails for every client for the life of
    the process, and the door parks in DOOR_HOLDING.
    """

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("inf", "hold_time must be a finite number"),
            ("-inf", "hold_time must be a finite number"),
            ("nan", "hold_time must be a finite number"),
            ("1e400", "hold_time must be a finite number"),
            ("-1", "hold_time must be between 0 and 900.0"),
            ("901", "hold_time must be between 0 and 900.0"),
            ("later", "hold_time must be a number"),
        ],
        ids=["inf", "-inf", "nan", "1e400", "negative", "too-large", "non-numeric"],
    )
    def test_hold_time_rejects_what_the_wire_rejects(self, value, message):
        runner = make_runner()
        before = runner.simulator.state.hold_time

        with pytest.raises(ScriptError, match=message):
            runner._set_value("hold_time", value)

        assert runner.simulator.state.hold_time == before

    def test_hold_time_at_the_ceiling_is_accepted(self):
        """The bound matches the wire's 90000 centiseconds, not something tighter."""
        runner = make_runner()
        runner._set_value("hold_time", "900")
        assert runner.simulator.state.hold_time == 900.0

    def test_rejected_hold_time_leaves_get_settings_working(self):
        """The damage the bound exists to prevent, reproduced end to end."""
        runner = make_runner()
        with pytest.raises(ScriptError):
            runner._set_value("hold_time", "inf")

        # int(inf * 100) raises OverflowError; this must still answer.
        assert runner.simulator.state.get_settings()[FIELD_HOLD_OPEN_TIME] == 200

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("99999999999", "battery must be between 0 and 100"),
            ("-5", "battery must be between 0 and 100"),
            ("inf", "battery must be a finite number"),
            ("full", "battery must be a number"),
        ],
        ids=["huge", "negative", "inf", "non-numeric"],
    )
    def test_battery_is_bounded_and_clamped(self, value, message):
        runner = make_runner()
        before = runner.simulator.state.battery_percent

        with pytest.raises(ScriptError, match=message):
            runner._set_value("battery", value)

        assert runner.simulator.state.battery_percent == before

    def test_battery_goes_through_the_simulator_clamp(self):
        """set battery uses set_battery(), like the operator's own command."""
        runner = make_runner()
        runner._set_value("battery", "42")
        assert runner.simulator.state.battery_percent == 42


class TestToggleValueMatrix:
    """_toggle_value flips every supported boolean."""

    @pytest.mark.parametrize(
        "name",
        ["power", "auto", "inside", "outside", "autoretract", "safety_lock", "cmd_lockout"],
    )
    def test_toggle_matrix(self, name):
        """Each toggle flips the attribute and flips it back."""
        runner = make_runner()
        original = getattr(runner.simulator.state, name)
        runner._toggle_value(name)
        assert getattr(runner.simulator.state, name) is (not original)
        runner._toggle_value(name)
        assert getattr(runner.simulator.state, name) is original

    def test_toggle_unknown_raises(self):
        """Unknown toggles raise the exact error."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown setting to toggle: bogus"):
            runner._toggle_value("bogus")


class TestAssertConditionMatrix:
    """_assert_condition covers every supported condition."""

    @pytest.mark.parametrize(
        ("condition", "state_kwargs", "expected"),
        [
            ("door_status", {"door_status": DOOR_STATE_RISING}, DOOR_STATE_RISING),
            # door_status comparison is case-insensitive (upper-normalized)
            ("door_status", {}, "door_closed"),
            ("power", {"power": True}, "on"),
            ("power", {"power": False}, "off"),
            ("auto", {"auto": False}, "off"),
            ("battery", {"battery_percent": 66}, "66"),
            ("hold_time", {"hold_time": 5}, "5"),
            ("inside", {"inside": True}, "enabled"),
            ("inside", {"inside": False}, "disabled"),
            ("outside", {"outside": False}, "disabled"),
            ("autoretract", {"autoretract": True}, "on"),
            ("safety_lock", {"safety_lock": True}, "on"),
            ("cmd_lockout", {"cmd_lockout": False}, "off"),
            ("total_open_cycles", {"total_open_cycles": 7}, "7"),
            ("total_auto_retracts", {"total_auto_retracts": 2}, "2"),
        ],
    )
    def test_assert_matrix_passes(self, condition, state_kwargs, expected):
        """A matching expectation does not raise."""
        runner = make_runner(**state_kwargs)
        runner._assert_condition(condition, expected)

    def test_assert_failure_message_contains_expected_and_actual(self):
        """The failure message names the condition and both values."""
        runner = make_runner(battery_percent=75)
        with pytest.raises(ScriptAssertionError) as exc_info:
            runner._assert_condition("battery", "50")
        assert str(exc_info.value) == "battery: expected '50', got '75'"

    def test_assert_unknown_condition_raises(self):
        """Unknown assertion conditions raise the exact error."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown assertion condition: bogus"):
            runner._assert_condition("bogus", "1")


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
        with pytest.raises(ScriptError, match="Unknown script"):
            get_builtin_script("nonexistent_script_xyz")

    def test_all_builtin_scripts_parse(self):
        """All built-in scripts should parse without errors."""
        for name, _ in list_builtin_scripts():
            script = get_builtin_script(name)
            assert script.name is not None
            assert len(script.steps) > 0

    def test_scripts_dir_supports_yml_extension(self, tmp_path, monkeypatch):
        """Both .yaml and .yml files are discovered as built-ins."""
        (tmp_path / "one.yaml").write_text("name: One\nsteps:\n  - close\n")
        (tmp_path / "two.yml").write_text("name: Two\nsteps:\n  - close\n")
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", tmp_path)

        assert dict(list_builtin_scripts()) == {"one": "", "two": ""}
        assert get_builtin_script("two").name == "Two"

    def test_missing_scripts_dir_yields_no_builtins(self, tmp_path, monkeypatch):
        """A missing scripts directory produces an empty available list."""
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", tmp_path / "gone")

        assert list_builtin_scripts() == []
        with pytest.raises(ScriptError, match="Unknown script: foo. Available: $"):
            get_builtin_script("foo")

    def test_list_builtin_scripts_reports_broken_script(self, tmp_path, monkeypatch, caplog):
        """A script that fails to load is listed with the error."""
        (tmp_path / "broken.yaml").write_text("steps: [unclosed")
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", tmp_path)

        with caplog.at_level(logging.WARNING, logger=SCRIPT_LOGGER):
            result = list_builtin_scripts()

        assert len(result) == 1
        name, description = result[0]
        assert name == "broken"
        assert description.startswith("(Error loading: ")
        assert "Failed to load script broken" in caplog.text


# ============================================================================
# script_completer Tests
# ============================================================================


@pytest.fixture
def completer_tree(tmp_path, monkeypatch):
    """A cwd tree with local scripts, subdirectories, and a hidden dir."""
    (tmp_path / "local.yaml").write_text("name: Local\nsteps: []\n")
    (tmp_path / "other.yml").write_text("name: Other\nsteps: []\n")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested.yaml").write_text("name: Nested\nsteps: []\n")
    (tmp_path / "empty").mkdir()
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.yaml").write_text("name: Secret\nsteps: []\n")
    # A directory whose name matches the glob - must not complete as a file
    (tmp_path / "dir.yaml").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@requires_yaml
class TestScriptCompleter:
    """Tab-completion candidates for the run command."""

    def test_empty_prefix_lists_builtins_and_cwd(self, completer_tree):
        """No prefix: built-ins with descriptions, local files, script dirs."""
        result = script_completer("")
        as_dict = dict(result)

        # Built-in scripts appear with their real description
        assert as_dict["basic_cycle"] == get_builtin_script("basic_cycle").description
        # Local YAML files (both extensions)
        assert as_dict["local.yaml"] == "(local file)"
        assert as_dict["other.yml"] == "(local file)"
        # Only subdirectories that contain YAML files are offered
        assert as_dict["subdir/"] == "(directory)"
        assert "empty/" not in as_dict
        assert ".hidden/" not in as_dict
        # The yaml-named directory is neither a local file nor a script dir
        assert "dir.yaml" not in as_dict
        assert "dir.yaml/" not in as_dict

    def test_name_prefix_lists_same_candidates(self, completer_tree):
        """A bare name prefix does not pre-filter (the caller filters)."""
        assert script_completer("bas") == script_completer("")

    def test_directory_prefix_lists_directory(self, completer_tree):
        """A './' prefix lists that directory's files and all subdirs."""
        result = script_completer("./")
        as_dict = dict(result)

        assert as_dict["./local.yaml"] == "(file)"
        assert as_dict["./other.yml"] == "(file)"
        assert as_dict["./subdir/"] == "(directory)"
        # In directory listings, subdirs are offered even without YAML files
        assert as_dict["./empty/"] == "(directory)"
        assert "./.hidden/" not in as_dict
        # The yaml-named directory completes as a directory, not a file
        assert as_dict["./dir.yaml/"] == "(directory)"
        assert ("./dir.yaml", "(file)") not in result
        # Built-ins are not mixed into directory listings
        assert "basic_cycle" not in as_dict

    def test_path_prefix_preserves_directory_part(self, completer_tree):
        """Completing './subdir/ne' keeps the './subdir/' prefix."""
        assert script_completer("./subdir/ne") == [("./subdir/nested.yaml", "(file)")]

    def test_absolute_directory_prefix(self, completer_tree):
        """An absolute directory prefix lists with absolute completions."""
        prefix = str(completer_tree / "subdir") + "/"
        assert script_completer(prefix) == [(prefix + "nested.yaml", "(file)")]

    def test_nonexistent_directory_yields_nothing(self, completer_tree):
        """A prefix pointing at a missing directory produces no candidates."""
        assert script_completer("./nope/") == []

    def test_no_builtins_offered_when_scripts_dir_missing(self, completer_tree, monkeypatch):
        """With no scripts directory, only local candidates are offered."""
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", completer_tree / "gone")
        as_dict = dict(script_completer(""))
        assert "basic_cycle" not in as_dict
        assert as_dict["local.yaml"] == "(local file)"

    def test_completer_without_yaml_lists_builtin_names(self, completer_tree, monkeypatch):
        """Without PyYAML, built-ins complete as plain '(builtin)' entries."""
        monkeypatch.setattr(scripting, "YAML_AVAILABLE", False)
        result = dict(script_completer(""))
        assert result["basic_cycle"] == "(builtin)"

    def test_completer_swallows_builtin_load_errors(self, completer_tree, monkeypatch):
        """A broken built-in still completes, described as '(builtin)'."""

        def raise_load(path):
            raise RuntimeError("boom")

        monkeypatch.setattr(scripting.Script, "from_file", raise_load)
        result = dict(script_completer(""))
        assert result["basic_cycle"] == "(builtin)"


class TestScriptChannelIsSanitized:
    """A YAML script is a named untrusted input in this threat model (S3).

    PyYAML rejects raw C0 bytes in scalars but its ``\\e`` escape produces a
    real ESC, so "the file looks clean" is not a defence. Every script
    string that reaches a log is sanitized at its source, exactly as the
    protocol channel's are.
    """

    POISON = "\x1b[2J\x1b[1;1H*** PWNED ***\x07"

    async def test_script_name_and_description_are_sanitized(self, runner, simulator, caplog):
        script = Script(
            name=self.POISON,
            description=self.POISON,
            steps=[ScriptStep(action="log", params={"message": "hi"}, line_number=1)],
        )

        with caplog.at_level(logging.INFO, logger=SCRIPT_LOGGER):
            assert await runner.run(script) is True

        assert "\x1b" not in caplog.text
        assert "\x07" not in caplog.text
        assert "\\x1b[2J" in caplog.text

    async def test_log_action_message_is_sanitized(self, runner, simulator, caplog):
        script = Script(
            name="clean",
            steps=[ScriptStep(action="log", params={"message": self.POISON}, line_number=1)],
        )

        with caplog.at_level(logging.INFO, logger=SCRIPT_LOGGER):
            await runner.run(script, verbose=False)

        assert "[SCRIPT] \\x1b[2J" in caplog.text
        assert "\x1b" not in caplog.text

    async def test_step_parameters_are_sanitized(self, runner, simulator, caplog):
        """The verbose per-step line echoes params straight from the file."""
        script = Script(
            name="clean",
            steps=[ScriptStep(action="log", params={"message": self.POISON}, line_number=1)],
        )

        with caplog.at_level(logging.INFO, logger=SCRIPT_LOGGER):
            await runner.run(script, verbose=True)

        assert "Step 1: log(message=\\x1b[2J" in caplog.text
        assert "\x1b" not in caplog.text

    async def test_script_error_text_is_sanitized(self, runner, simulator, caplog):
        """Failure reasons quote script-supplied text back at the operator."""
        script = Script(
            name="clean",
            steps=[ScriptStep(action=f"nope{self.POISON}", line_number=1)],
        )

        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            assert await runner.run(script, verbose=False) is False

        assert "\x1b" not in caplog.text
        assert "\x07" not in caplog.text
        # The action name is case-normalized before it reaches the message.
        assert "Unknown action: nope\\x1b[2j" in caplog.text


class TestScriptCompleterWithoutPaths:
    """ctl's daemon refuses script paths, so ctl must not complete them (M1).

    Completion used to offer ``my_custom.yaml`` - guaranteed to fail with
    "Unknown script" - while the name that works (``my_custom``) was the
    one thing it could not offer.
    """

    @pytest.fixture(autouse=True)
    def no_paths(self):
        scripting.set_script_paths_allowed(False)
        yield
        scripting.set_script_paths_allowed(True)

    def test_only_script_names_are_offered(self, completer_tree):
        as_dict = dict(script_completer(""))

        assert as_dict["basic_cycle"] == get_builtin_script("basic_cycle").description
        assert "local.yaml" not in as_dict
        assert "other.yml" not in as_dict
        assert "subdir/" not in as_dict

    def test_scripts_dir_names_are_still_offered(self, completer_tree, tmp_path):
        """A daemon-side --scripts-dir name is a bare name, so it stays."""
        extra = tmp_path / "extras"
        extra.mkdir()
        (extra / "my_custom.yaml").write_text("name: Custom\nsteps: []\n")
        scripting.set_extra_scripts_dir(extra)

        as_dict = dict(script_completer(""))

        assert "my_custom" in as_dict
        assert "my_custom.yaml" not in as_dict

    @pytest.mark.parametrize("prefix", ["./", "./subdir/", "scripts/", "./nope/"])
    def test_path_prefixes_offer_nothing(self, completer_tree, prefix):
        """Every path form the daemon refuses completes to nothing."""
        assert script_completer(prefix) == []

    def test_name_prefix_still_completes(self, completer_tree):
        """A bare name prefix keeps working - that is the form that runs."""
        assert dict(script_completer("bas"))["basic_cycle"]


# ============================================================================
# Concurrent Script Execution (frontend round-2 M3)
# ============================================================================


class TestScriptSerialization:
    """One simulator runs one script at a time.

    Two concurrent runs used to drive the same door and fail each other's
    assertions, and they shared ``_stop_requested``/``_stop_event``.
    """

    @staticmethod
    def _blocking_runner(simulator) -> tuple[ScriptRunner, asyncio.Event, asyncio.Event]:
        """A runner whose steps block until ``release`` is set."""
        runner = ScriptRunner(simulator)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_step(step):
            entered.set()
            await release.wait()

        runner._execute_step = blocking_step
        return runner, entered, release

    @staticmethod
    def _script(name: str) -> Script:
        return Script(name=name, steps=[ScriptStep(action="open", params={}, line_number=1)])

    async def test_wait_run_is_refused_while_another_script_runs(self, simulator):
        """queue_if_busy=False fails fast, naming the script that holds the door."""
        runner, entered, release = self._blocking_runner(simulator)
        first = asyncio.ensure_future(runner.run(self._script("First")))
        async with asyncio.timeout(2.0):
            await entered.wait()

        assert runner.busy is True
        assert runner.current_script == "First"

        # Bounded: without the fast-fail guard this await parks on the run
        # lock that only `release` (the line below) frees, and a regression
        # would hang the suite forever instead of failing (R3-M4).
        with pytest.raises(ScriptError, match="Another script is already running: First"):
            async with asyncio.timeout(2.0):
                await runner.run(self._script("Second"), queue_if_busy=False)

        release.set()
        assert await asyncio.wait_for(first, 2.0) is True
        assert runner.busy is False
        assert runner.current_script is None

    async def test_queued_run_waits_for_the_running_script(self, simulator):
        """The default (queue) path blocks instead of interleaving."""
        runner, entered, release = self._blocking_runner(simulator)
        first = asyncio.ensure_future(runner.run(self._script("First")))
        async with asyncio.timeout(2.0):
            await entered.wait()

        second = asyncio.ensure_future(runner.run(self._script("Second")))
        # Give the queued run every chance to barge in.
        for _ in range(5):
            await asyncio.sleep(0)
        assert second.done() is False
        assert runner.current_script == "First"

        release.set()
        assert await asyncio.wait_for(first, 2.0) is True
        assert await asyncio.wait_for(second, 2.0) is True

    async def test_wait_run_after_the_first_finishes_is_allowed(self, simulator):
        """The busy guard is not sticky - it clears when the run ends."""
        runner, entered, release = self._blocking_runner(simulator)
        first = asyncio.ensure_future(runner.run(self._script("First")))
        async with asyncio.timeout(2.0):
            await entered.wait()
        release.set()
        await asyncio.wait_for(first, 2.0)

        assert await runner.run(self._script("Second"), queue_if_busy=False) is True
