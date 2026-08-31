# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator scripting module (scripting.py)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    FIELD_HOLD_OPEN_TIME,
)
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
    scripting,
)
from powerpetdoor.simulator.engine import SENSOR_NAMES
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
from powerpetdoor.simulator.state import WHOLE_DAY_END_HOUR, WHOLE_DAY_END_MINUTE
from powerpetdoor.simulator.wire_values import (
    settings_payload,
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
        closing_start_time=0.02,
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
        step = ScriptStep(action="trigger", params={"sensor": "inside"})
        assert step.action == "trigger"
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
  - action: trigger
    sensor: inside
  - action: wait
    seconds: 1
"""
        script = Script.from_yaml(yaml_content)
        assert script.name == "Test Script"
        assert script.description == "A test"
        assert len(script.steps) == 2
        assert script.steps[0].action == "trigger"
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

    def test_from_simple_commands_enable_schedule(self):
        script = Script.from_simple_commands(["enable_schedule 3 off"])
        assert script.steps[0].params == {"index": "3", "state": "off"}

    def test_from_simple_commands_enable_schedule_defaults_to_on(self):
        """Omitting the state means enable, as `schedule enable` does."""
        script = Script.from_simple_commands(["enable_schedule 3"])
        assert script.steps[0].params == {"index": "3"}

    def test_from_simple_commands_enable_schedule_defaults_the_index(self):
        script = Script.from_simple_commands(["enable_schedule"])
        assert script.steps[0].params == {"index": "1"}

    def test_from_simple_commands_clear_schedules(self):
        script = Script.from_simple_commands(["clear_schedules"])
        assert script.steps[0].action == "clear_schedules"
        assert script.steps[0].params == {}

    def test_from_simple_commands_notify(self):
        """`notify <name> [on|off]` - the name is a parameter, not a suffix."""
        script = Script.from_simple_commands(["notify inside_on off"])
        assert script.steps[0].params == {"name": "inside_on", "state": "off"}

    def test_from_simple_commands_notify_without_a_state(self):
        """Omitting the state toggles, so the parameter is left off."""
        script = Script.from_simple_commands(["notify low_battery"])
        assert script.steps[0].params == {"name": "low_battery"}

    def test_from_simple_commands_notify_without_a_name(self):
        """A bare `notify` parses; the runner is what refuses the empty name."""
        script = Script.from_simple_commands(["notify"])
        assert script.steps[0].params == {"name": ""}

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
        # `toggle` is the door and takes nothing; a setting inverts
        # through `set <name> toggle`.
        assert actions["toggle"] == {}
        # `assert x` with no expected value no longer invents an empty
        # one: a bare boolean condition means "is true".
        assert actions["assert"] == {"condition": ""}
        assert actions["log"] == {"message": "a b"}

    def test_from_simple_commands_unlisted_action_gets_no_params(self):
        """Commands outside the parser's list pass through with empty params."""
        script = Script.from_simple_commands(["battery 42"])
        assert len(script.steps) == 1
        assert script.steps[0].action == "battery"
        assert script.steps[0].params == {}

    def test_from_simple_commands_remaining_forms(self):
        """open/cycle/close/obstruction/sensor/schedule shorthands."""
        script = Script.from_simple_commands(
            [
                "open",
                "cycle",
                "close",
                "obstruction",
                "inside on",
                "add_schedule 4",
                "add_schedule",
                "remove_schedule 2",
                "remove_schedule",
            ]
        )
        params = [step.params for step in script.steps]
        assert params == [
            {},
            {},
            {},
            {},
            {"state": "on"},
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

    async def test_trigger_action(self, runner, simulator):
        """trigger opens the door; wait_for observes it deterministically."""
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

    async def test_set_toggle_inverts_a_setting(self, runner, simulator):
        """`toggle` is the *door* now, matching the CLI; a setting inverts
        through `set <name> toggle`, matching `power toggle` there."""
        assert simulator.state.power is True
        script = Script.from_simple_commands(["set power toggle"])
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
        assert (
            "Assertion failed at step 1: battery: expected equals '50', got '75'"
        ) in caplog.text

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
                "wait_for door_open 5",
                "wait_for door_closing 5",
                # The FIRST closing state, which a real door reports as the
                # motor starts and before the flap moves.
                f"assert door_status {DOOR_STATE_CLOSING}",
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
        simulator.state.power = True
        script = Script.from_simple_commands(["wait_for power 5"])
        result = await runner.run(script, verbose=False)
        assert result is True

    async def test_wait_for_non_status_condition_timeout(self, runner, simulator):
        """wait_for on a non-status condition times out when never true."""
        script = Script.from_yaml(
            "name: t\n"
            "steps:\n"
            "  - action: wait_for\n"
            "    condition: power\n"
            "    equals: off\n"
            "    timeout: 0.3\n"
        )
        result = await runner.run(script, verbose=False)
        assert result is False

    async def test_wait_for_non_status_condition_becomes_true(self, runner, simulator):
        """The poll loop returns once the condition flips to true."""
        task = asyncio.create_task(
            runner._wait_for_condition({"condition": "power", "equals": "off"}, 5, "fail")
        )
        await asyncio.sleep(0)  # enter the poll loop
        simulator.state.power = False
        await asyncio.wait_for(task, timeout=2.0)

    async def test_stop_interrupts_non_status_poll(self, runner, simulator):
        """stop() aborts a non-status poll with the exact error."""
        task = asyncio.create_task(
            runner._wait_for_condition({"condition": "power", "equals": "off"}, 5, "fail")
        )
        await asyncio.sleep(0)  # enter the poll loop
        runner.stop()
        with pytest.raises(ScriptError, match="Script stopped while waiting"):
            await asyncio.wait_for(task, timeout=2.0)

    async def test_stop_before_wait_raises_immediately(self, runner, simulator):
        """A wait_for started after stop() fails without waiting."""
        runner._stop_requested = True
        with pytest.raises(ScriptError, match="Script stopped while waiting"):
            await runner._wait_for_condition({"condition": "door_closed"}, 1, "fail")

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
        script = Script.from_simple_commands(["wait_for door_open 30"])

        task = asyncio.create_task(runner.run(script, verbose=False))
        # Let the runner enter the wait_for step
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        runner.stop()

        result = await asyncio.wait_for(task, timeout=2.0)
        assert result is False

    async def test_engine_stop_fails_status_wait(self, runner, simulator):
        """Stopping the engine cancels the status waiter; the wait errors out."""
        task = asyncio.create_task(
            runner._wait_for_condition(
                {"condition": "door_status", "equals": "DOOR_KEEPUP"}, 5, "fail"
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # enter the status wait

        await simulator.engine.stop()
        with pytest.raises(ScriptError, match="Timeout waiting for condition"):
            await asyncio.wait_for(task, timeout=2.0)

    async def test_stop_during_a_status_wait_says_stopped_not_timeout(self, runner, simulator):
        """`stop` during `wait_for door_open` must not be reported as a timeout.

        Two independent mutants of the `if stopper in done:` branch survived
        the whole suite: the branch never firing, and its message replaced by
        the timeout message. An operator who typed `stop` was then told
        "Timeout waiting for condition: door_open" - a misleading diagnosis
        with a generous timeout still running.
        """
        task = asyncio.create_task(
            runner._wait_for_condition(
                {"condition": "door_status", "equals": "DOOR_KEEPUP"}, 30, "fail"
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # enter the status wait

        runner.stop()

        with pytest.raises(ScriptError) as excinfo:
            await asyncio.wait_for(task, timeout=2.0)
        assert str(excinfo.value) == "Script stopped while waiting"

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
        """A stop landing during the FINAL step must still report FAILED.

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
        """Same defect with steps before the last one (the two-step case)."""

        async def stopping_close():
            runner.stop()

        simulator.close_door = stopping_close
        script = Script.from_simple_commands(["log first", "close"])

        assert await runner.run(script, verbose=False) is False

    async def test_stop_interrupts_a_plain_wait(self, runner, simulator):
        """`wait N` is raced against the stop event, not slept through.

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
        """`stop` takes effect at a step boundary, so the pending state shows."""
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
        """The queue consumer stops counting a run as pending when it starts."""
        seen: list[bool] = []
        script = Script.from_simple_commands(["log hello"])

        def on_start() -> bool:
            seen.append(runner.busy)
            return True

        assert await runner.run(script, verbose=False, on_start=on_start) is True

        # Called with the run lock held, before the first step.
        assert seen == [True]

    async def test_on_start_returning_false_abandons_the_run(self, runner, simulator):
        """A claim dropped while parked on the lock must not run.

        `stop all` cancels the claimed entry while the consumer waits for
        the run lock; starting the script afterwards would run exactly what
        the operator was told had been dropped.
        """
        script = Script.from_simple_commands(["set power off"])
        simulator.state.power = True

        assert await runner.run(script, verbose=False, on_start=lambda: False) is False

        # Not a single step executed, and the runner is free again.
        assert simulator.state.power is True
        assert runner.current_script is None
        assert runner.busy is False

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

    async def test_unexpected_error_fails_script(self, runner, simulator, caplog, monkeypatch):
        """A non-ScriptError exception fails the script via the catch-all."""

        def boom():
            raise RuntimeError("hardware exploded")

        monkeypatch.setattr(simulator, "simulate_obstruction", boom)
        script = Script(
            name="Bad",
            steps=[ScriptStep(action="obstruction", line_number=1)],
        )
        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)
        assert result is False
        assert "Unexpected error at step 1" in caplog.text


class TestAssertConditionsSurviveTheYamlLoader:
    """`equals:` arrives from PyYAML already resolved.

    PyYAML turns `on`/`off` (and `yes`/`no`/`true`/`false`) into booleans
    and bare digits into ints, and the comparison then called `.lower()`
    on them - so 9 of the 12 documented `assert` conditions failed a
    **true** assertion with `AttributeError: 'bool' object has no attribute
    'lower'`. Quoting does not rescue `hold_time`, which is stored as a
    float: `equals: "2"` still lost to `str(2.0)`.
    """

    @pytest.mark.parametrize(
        ("condition", "literal", "setup"),
        [
            ("power", "off", lambda state: setattr(state, "power", False)),
            ("power", "on", lambda state: setattr(state, "power", True)),
            ("auto", "off", lambda state: setattr(state, "auto", False)),
            ("autoretract", "on", lambda state: setattr(state, "autoretract", True)),
            ("safety_lock", "off", lambda state: setattr(state, "safety_lock", False)),
            ("cmd_lockout", "on", lambda state: setattr(state, "cmd_lockout", True)),
            ("battery", "75", lambda state: setattr(state, "battery_percent", 75)),
            ("hold_time", "2", lambda state: setattr(state, "hold_time", 2.0)),
            ("hold_time", '"2"', lambda state: setattr(state, "hold_time", 2.0)),
            ("hold_time", "2.5", lambda state: setattr(state, "hold_time", 2.5)),
            ("total_open_cycles", "4", lambda state: setattr(state, "total_open_cycles", 4)),
            ("total_auto_retracts", "1", lambda state: setattr(state, "total_auto_retracts", 1)),
            ("inside", "enabled", lambda state: setattr(state, "inside", True)),
            ("outside", "disabled", lambda state: setattr(state, "outside", False)),
            ("door_status", "DOOR_CLOSED", lambda state: None),
        ],
    )
    async def test_a_true_assertion_written_in_yaml_passes(
        self, runner, simulator, condition, literal, setup
    ):
        setup(simulator.state)
        script = Script.from_yaml(
            f"name: A\nsteps:\n  - action: assert\n    condition: {condition}\n"
            f"    equals: {literal}\n"
        )

        assert await runner.run(script, verbose=False) is True

    async def test_a_false_assertion_written_in_yaml_still_fails(self, runner, simulator, caplog):
        """The guard must not turn every assertion into a pass."""
        simulator.state.power = True
        script = Script.from_yaml(
            "name: A\nsteps:\n  - action: assert\n    condition: power\n    equals: off\n"
        )

        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            assert await runner.run(script, verbose=False) is False

        assert "power: expected equals 'off', got 'on'" in caplog.text

    async def test_the_failure_message_reports_the_normalized_values(
        self, runner, simulator, caplog
    ):
        """`got '2.0'` for a hold time of 2 s reads like a different number."""
        simulator.state.hold_time = 2.0
        script = Script.from_yaml(
            "name: A\nsteps:\n  - action: assert\n    condition: hold_time\n    equals: 3\n"
        )

        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            assert await runner.run(script, verbose=False) is False

        assert "hold_time: expected equals '3', got '2'" in caplog.text


class TestScriptActions:
    """Per-action behavior of the step dispatcher."""

    async def test_obstruction_action(self, runner, simulator):
        """obstruction places a physical obstruction, arming no sensor."""
        result = await runner.run(Script.from_simple_commands(["obstruction"]), verbose=False)
        assert result is True
        assert simulator.state.obstruction_active is True
        assert simulator.state.obstruction_oneshot is True
        assert simulator.state.inside_sensor_active is False
        assert simulator.state.outside_sensor_active is False

    async def test_obstruction_duration_zero_stays_until_cleared(self, runner, simulator):
        """0 is the boundary against the omitted-argument one-shot."""
        script = Script.from_yaml("name: t\nsteps:\n  - action: obstruction\n    duration: 0\n")
        assert await runner.run(script, verbose=False) is True
        assert simulator.state.obstruction_active is True
        assert simulator.state.obstruction_oneshot is False

    async def test_obstruction_duration_expires(self, runner, simulator):
        """The other side of the boundary: a positive window clears itself."""
        script = Script.from_yaml("name: t\nsteps:\n  - action: obstruction\n    duration: 0.05\n")
        assert await runner.run(script, verbose=False) is True
        assert simulator.state.obstruction_active is True

        await asyncio.sleep(0.15)
        assert simulator.state.obstruction_active is False

    async def test_obstruction_rejects_a_non_numeric_duration(self, runner, simulator):
        """Same loud failure every other mistyped script value gets."""
        script = Script.from_yaml("name: t\nsteps:\n  - action: obstruction\n    duration: soon\n")
        assert await runner.run(script, verbose=False) is False
        assert simulator.state.obstruction_active is False

    async def test_holding_the_inside_sensor_is_pet_presence(self, runner, simulator):
        """There is no `pet` action: `inside on` is the same thing, and
        unlike `pet on` it opens a closed door, which a collar does."""
        result = await runner.run(Script.from_simple_commands(["inside on"]), verbose=False)
        assert result is True
        assert simulator.state.inside_sensor_active is True
        assert simulator.state.door_status == DOOR_STATE_RISING

    async def test_a_second_on_is_not_a_toggle(self, runner, simulator):
        """`on` says which way it means; only `toggle`/`0` flips."""
        simulator.state.power = False  # keep the door still; only flags matter
        result = await runner.run(
            Script.from_simple_commands(["inside on", "inside on"]), verbose=False
        )
        assert result is True
        assert simulator.state.inside_sensor_active is True

    async def test_off_clears_the_sensor(self, runner, simulator):
        """`inside off` clears the sensor a pet was holding."""
        simulator.state.power = False
        result = await runner.run(
            Script.from_simple_commands(["inside on", "inside off"]), verbose=False
        )
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
                "wait_for door_open 5",
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
        # 00:00-23:59, spelled the way the device itself spells a full day
        # . Its final minute is inside
        # the window, so the schedule really does allow both sensors at
        # every minute of every day - which is what the sum below pins.
        # 24:00: a script's "always" window has to be active at 23:59 too.
        assert (schedule.end_hour, schedule.end_min) == (WHOLE_DAY_END_HOUR, WHOLE_DAY_END_MINUTE)
        assert schedule.is_sensor_allowed("inside", 23, 59, 0) is True
        assert (
            sum(
                schedule.is_sensor_allowed("inside", minute // 60, minute % 60, weekday)
                for weekday in range(7)
                for minute in range(1440)
            )
            == 7 * 1440
        )

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
            steps=[
                ScriptStep(action="Wait-For", params={"condition": "door_closed"}, line_number=1)
            ],
        )
        result = await runner.run(script, verbose=False)
        assert result is True


# ============================================================================
# Condition / Set / Toggle / Assert Matrices
# ============================================================================


class TestCheckConditionMatrix:
    """Every condition name, in both the bare and compared forms."""

    @pytest.mark.parametrize(
        ("condition", "state_kwargs", "expected"),
        [
            ("door_closed", {}, True),
            ("door_closed", {"door_status": DOOR_STATE_RISING}, False),
            ("door_open", {"door_status": DOOR_STATE_HOLDING}, True),
            ("door_open", {"door_status": DOOR_STATE_KEEPUP}, True),
            ("door_open", {}, False),
            (
                {"condition": "door_status", "equals": "DOOR_RISING"},
                {"door_status": DOOR_STATE_RISING},
                True,
            ),
            ({"condition": "door_status", "equals": "DOOR_RISING"}, {}, False),
            (
                {"condition": "door_status", "equals": "DOOR_HOLDING"},
                {"door_status": DOOR_STATE_HOLDING},
                True,
            ),
            (
                {"condition": "door_status", "equals": "DOOR_HOLDING"},
                {"door_status": DOOR_STATE_KEEPUP},
                False,
            ),
            (
                {"condition": "door_status", "equals": "DOOR_KEEPUP"},
                {"door_status": DOOR_STATE_KEEPUP},
                True,
            ),
            (
                {"condition": "door_status", "equals": "DOOR_KEEPUP"},
                {"door_status": DOOR_STATE_HOLDING},
                False,
            ),
            ("door_closing", {"door_status": DOOR_STATE_CLOSING_TOP_OPEN}, True),
            ("door_closing", {}, False),
            ({"condition": "power", "equals": "on"}, {}, True),
            ({"condition": "power", "equals": "on"}, {"power": False}, False),
            ({"condition": "power", "equals": "off"}, {"power": False}, True),
            ({"condition": "power", "equals": "off"}, {}, False),
            ({"condition": "inside", "equals": "on"}, {}, True),
            ({"condition": "inside", "equals": "on"}, {"inside": False}, False),
            ({"condition": "inside", "equals": "off"}, {"inside": False}, True),
            ({"condition": "outside", "equals": "on"}, {}, True),
            ({"condition": "outside", "equals": "off"}, {"outside": False}, True),
            ({"condition": "outside", "equals": "off"}, {}, False),
            ({"condition": "autoretract", "equals": "on"}, {}, True),
            ({"condition": "autoretract", "equals": "off"}, {"autoretract": False}, True),
            ({"condition": "autoretract", "equals": "off"}, {}, False),
            ({"condition": "auto", "equals": "on"}, {}, True),
            ({"condition": "auto", "equals": "off"}, {"auto": False}, True),
            ({"condition": "auto", "equals": "off"}, {}, False),
            ({"condition": "safety_lock", "equals": "on"}, {"safety_lock": True}, True),
            ({"condition": "safety_lock", "equals": "off"}, {}, True),
            ({"condition": "safety_lock", "equals": "off"}, {"safety_lock": True}, False),
            ({"condition": "cmd_lockout", "equals": "on"}, {"cmd_lockout": True}, True),
            ({"condition": "cmd_lockout", "equals": "off"}, {}, True),
            ({"condition": "cmd_lockout", "equals": "off"}, {"cmd_lockout": True}, False),
            # Hyphens normalize to underscores
            ("door-closed", {}, True),
        ],
    )
    def test_condition_matrix(self, condition, state_kwargs, expected):
        """Each condition evaluates to its single correct value.

        A bare name is a yes/no condition asked directly; a mapping is the
        `condition` + comparison form.
        """
        runner = make_runner(**state_kwargs)
        params = {"condition": condition} if isinstance(condition, str) else condition
        assert runner._if_holds(params) is expected

    def test_unknown_condition_raises(self):
        """Unknown conditions raise the exact error."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown condition: bogus"):
            runner._if_holds({"condition": "bogus"})


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
        """hold_time is a float everywhere else, so 1.5 must work."""
        runner = make_runner()
        runner._set_value("hold_time", "1.5")
        assert runner.simulator.state.hold_time == 1.5

    def test_set_unknown_raises(self):
        """Unknown settings raise the exact error."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown setting: bogus"):
            runner._set_value("bogus", "1")

    def test_set_read_only_raises(self):
        """A value the door computes is refused, not silently ignored.

        `door_status` is in the registry so a script can assert on it;
        being there must not make it writable.
        """
        runner = make_runner()
        with pytest.raises(ScriptError, match="door_status is read-only"):
            runner._set_value("door_status", "DOOR_IDLE")

    def test_set_read_only_is_refused_before_coercion(self):
        """The refusal names read-only, not the value's shape."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="read-only"):
            runner._set_value("position", "50")


class TestScriptWriterIsBoundedLikeTheWire:
    """The YAML script channel is the third writer of these fields.

    The wire path and the CLI path are both bounded; the script path was
    not, so `set hold_time inf` re-opened the same hole - GET_SETTINGS
    (issued by the shipped client on every connect and refresh) then fails
    for every client for the life of the process, and the door parks in
    DOOR_HOLDING.
    """

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("inf", "hold_time must be a finite number"),
            ("-inf", "hold_time must be a finite number"),
            ("nan", "hold_time must be a finite number"),
            ("1e400", "hold_time must be a finite number"),
            ("-1", "hold_time must be between 0.0 and 900.0"),
            ("901", "hold_time must be between 0.0 and 900.0"),
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
        assert settings_payload(runner.simulator.state)[FIELD_HOLD_OPEN_TIME] == 200

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("99999999999", "battery must be between 0.0 and 100.0"),
            ("-5", "battery must be between 0.0 and 100.0"),
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


class TestScriptDelaysAreBounded:
    """The last unbounded script numerics.

    ``duration: .nan`` reached ``asyncio.sleep`` and raised
    ``ValueError("Invalid delay: NaN")`` inside a fire-and-forget task: the
    step silently did not happen, the sensor stayed active forever, the
    operator got an unhandled-task traceback - and the run still reported
    PASSED, a false-green CI signal.
    """

    @pytest.mark.parametrize(
        ("action", "param", "name"),
        [
            ("inside", "duration", "duration"),
            ("outside", "duration", "duration"),
            ("wait", "seconds", "seconds"),
            ("wait_for", "timeout", "timeout"),
        ],
    )
    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            (float("nan"), "must be a finite number"),
            (float("inf"), "must be a finite number"),
            (float("-inf"), "must be a finite number"),
            (1e400, "must be a finite number"),
            (-1, "must be between 0 and 86400.0"),
            (86401, "must be between 0 and 86400.0"),
            ("soon", "must be a number"),
        ],
        ids=["nan", "inf", "-inf", "1e400", "negative", "too-large", "non-numeric"],
    )
    async def test_delay_values_are_rejected(self, action, param, name, value, reason):
        runner = make_runner()
        script = Script(
            name="Bad",
            steps=[ScriptStep(action=action, params={param: value}, line_number=1)],
        )

        assert await runner.run(script, verbose=False) is False
        assert runner.simulator.state.inside_sensor_active is False
        assert runner.simulator.state.outside_sensor_active is False

    @pytest.mark.parametrize(
        ("action", "param"),
        [("inside", "duration"), ("outside", "duration"), ("wait", "seconds")],
    )
    async def test_zero_is_still_accepted(self, action, param):
        """The floor is 0, which several built-in scripts rely on."""
        runner = make_runner()
        script = Script(
            name="Fine",
            steps=[ScriptStep(action=action, params={param: 0}, line_number=1)],
        )

        assert await runner.run(script, verbose=False) is True

    async def test_a_rejected_delay_reports_the_field_name(self, caplog):
        runner = make_runner()
        script = Script(
            name="Bad",
            steps=[ScriptStep(action="wait", params={"seconds": float("nan")}, line_number=1)],
        )

        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            assert await runner.run(script, verbose=False) is False

        assert "seconds must be a finite number" in caplog.text


class TestScriptPathsAllowedFlag:
    """One policy flag, read by the completer and the in-client help."""

    def test_default_allows_paths(self):
        assert scripting.script_paths_allowed() is True
        assert scripting.describe_script_argument() == "Script name or file path"

    def test_restricting_changes_the_help_text(self, monkeypatch):
        monkeypatch.setattr(scripting, "_script_paths_allowed", False)

        assert scripting.script_paths_allowed() is False
        assert scripting.describe_script_argument() == (
            "Script name (paths are not accepted over the control channel)"
        )


class TestToggleValueMatrix:
    """`set <name> toggle` flips every supported boolean."""

    @pytest.mark.parametrize(
        "name",
        ["power", "auto", "inside", "outside", "autoretract", "safety_lock", "cmd_lockout"],
    )
    def test_toggle_matrix(self, name):
        """Each toggle flips the attribute and flips it back."""
        runner = make_runner()
        original = getattr(runner.simulator.state, name)
        runner._set_value(name, "toggle")
        assert getattr(runner.simulator.state, name) is (not original)
        runner._set_value(name, "toggle")
        assert getattr(runner.simulator.state, name) is original

    def test_toggle_unknown_raises(self):
        """Unknown toggles raise the exact error."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown setting to toggle: bogus"):
            runner._set_value("bogus", "toggle")

    def test_toggle_a_value_that_is_not_a_state_raises(self):
        """A number has nothing to invert, and says so rather than coercing."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Cannot toggle hold_time"):
            runner._set_value("hold_time", "toggle")

    def test_toggle_a_read_only_value_raises(self):
        """Read-only rows are refused by the toggle path too."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown setting to toggle: door_status"):
            runner._set_value("door_status", "toggle")


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
        runner._assert_condition({"condition": condition, "equals": expected})

    def test_assert_failure_message_contains_expected_and_actual(self):
        """The failure message names the condition and both values."""
        runner = make_runner(battery_percent=75)
        with pytest.raises(ScriptAssertionError) as exc_info:
            runner._assert_condition({"condition": "battery", "equals": "50"})
        assert str(exc_info.value) == "battery: expected equals '50', got '75'"

    def test_assert_unknown_condition_raises(self):
        """Unknown assertion conditions raise the exact error."""
        runner = make_runner()
        with pytest.raises(ScriptError, match="Unknown condition: bogus"):
            runner._assert_condition({"condition": "bogus", "equals": "1"})


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
        """All built-in scripts parse, and the parse is actually pinned.

        `script.name is not None` and `len(steps) > 0` claim "parses
        without errors" while pinning almost nothing, and the loop had no
        non-emptiness guard so it would pass vacuously if discovery broke.
        """
        listed = list_builtin_scripts()

        assert len(listed) >= 5
        for name, description in listed:
            script = get_builtin_script(name)
            assert script.name and isinstance(script.name, str)
            assert script.description == description
            assert script.steps
            for step in script.steps:
                assert isinstance(step, ScriptStep)
                assert step.action and isinstance(step.action, str)
                assert isinstance(step.params, dict)

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


@requires_yaml
class TestDescriptionsAreCachedPerFileVersion:
    """Every listing used to re-parse every file, on the door's own loop.

    `list`, `--list-scripts` and every Tab keystroke share one renderer,
    and it fully YAML-parsed every candidate every time: ~600 ms for a
    200-script `--scripts-dir`, held on the event loop that serves the
    door protocol. Descriptions are what the parse is *for*, and they
    change only when the file does.
    """

    @pytest.fixture(autouse=True)
    def _empty_cache(self):
        scripting._description_cache.clear()
        yield
        scripting._description_cache.clear()

    def test_a_second_listing_does_not_re_parse(self, tmp_path, monkeypatch):
        (tmp_path / "one.yaml").write_text("name: One\ndescription: first\nsteps:\n  - close\n")
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", tmp_path)
        assert list_builtin_scripts() == [("one", "first")]

        parses = []
        original = scripting.Script.from_file
        monkeypatch.setattr(
            scripting.Script,
            "from_file",
            staticmethod(lambda path: (parses.append(path), original(path))[1]),
        )

        assert list_builtin_scripts() == [("one", "first")]
        assert parses == []

    def test_an_edited_script_is_picked_up_immediately(self, tmp_path, monkeypatch):
        """The behaviour `list` relies on: no stale description, ever."""
        script = tmp_path / "one.yaml"
        script.write_text("name: One\ndescription: first\nsteps:\n  - close\n")
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", tmp_path)
        assert list_builtin_scripts() == [("one", "first")]

        script.write_text("name: One\ndescription: second!\nsteps:\n  - close\n")

        assert list_builtin_scripts() == [("one", "second!")]

    def test_a_same_size_edit_is_still_picked_up(self, tmp_path, monkeypatch):
        """`st_size` alone is not enough; the key carries `st_mtime_ns` too."""
        script = tmp_path / "one.yaml"
        script.write_text("name: One\ndescription: aaaaa\nsteps:\n  - close\n")
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", tmp_path)
        assert list_builtin_scripts() == [("one", "aaaaa")]

        script.write_text("name: One\ndescription: bbbbb\nsteps:\n  - close\n")
        assert script.stat().st_size == len("name: One\ndescription: aaaaa\nsteps:\n  - close\n")

        assert list_builtin_scripts() == [("one", "bbbbb")]

    def test_a_same_mtime_edit_still_reparses(self, tmp_path, monkeypatch):
        """...and the mirror image: `st_mtime_ns` alone is not enough either.

        The key's `st_size` component is what covers a filesystem with
        coarse `mtime` granularity, and `os.utime`-preserving tooling -
        exactly the cases it exists for. Dropping it survived the whole
        suite, and `os.utime(path, ns=...)` makes the collision
        deterministic rather than a race.
        """
        script = tmp_path / "one.yaml"
        script.write_text("name: One\ndescription: FIRST\nsteps:\n  - close\n")
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", tmp_path)
        before = script.stat()
        assert list_builtin_scripts() == [("one", "FIRST")]

        script.write_text("name: One\ndescription: SECOND-and-longer\nsteps:\n  - close\n")
        os.utime(script, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = script.stat()
        # The premise: identical mtime, different size.
        assert after.st_mtime_ns == before.st_mtime_ns
        assert after.st_size != before.st_size

        assert list_builtin_scripts() == [("one", "SECOND-and-longer")]

    def test_a_broken_script_is_reported_on_every_listing(self, tmp_path, monkeypatch, caplog):
        """The cache is a *parse* cache, not a report cache."""
        (tmp_path / "broken.yaml").write_text("steps: [unclosed")
        monkeypatch.setattr(scripting, "SCRIPTS_DIR", tmp_path)

        with caplog.at_level(logging.WARNING, logger=SCRIPT_LOGGER):
            first = list_builtin_scripts()
            second = list_builtin_scripts()

        assert first == second
        assert first[0][1].startswith("(Error loading: ")
        assert caplog.text.count("Failed to load script broken") == 2

    def test_a_file_that_vanishes_is_not_cached(self, tmp_path):
        """Between listing and describing, a file can go away."""
        script = tmp_path / "gone.yaml"
        script.write_text("name: Gone\nsteps:\n  - close\n")
        script.unlink()

        description, error = scripting._describe_script(script)

        assert description.startswith("(Error loading: ")
        assert error is not None
        assert scripting._description_cache == {}

    def test_the_cache_is_bounded(self, tmp_path, monkeypatch):
        """An edit adds a key rather than replacing one, so it needs a cap."""
        monkeypatch.setattr(scripting, "MAX_DESCRIPTION_CACHE", 4)
        script = tmp_path / "one.yaml"

        for index in range(10):
            # Each revision is a *different length*, so the key differs by
            # `st_size` alone. The equal-length `v0`..`v9` this used to write
            # leaned entirely on `st_mtime_ns` to tell revisions apart, and
            # writes this close together get one timestamp on a filesystem
            # with coarse mtime granularity - the whole case
            # `test_a_same_mtime_edit_still_reparses` says `st_size` is in
            # the key to cover. CI duly served a stale `v5` for `v6`.
            description = "v" * (index + 1)
            script.write_text(f"name: One\ndescription: {description}\nsteps:\n  - close\n")
            assert scripting._describe_script(script) == (description, None)

        assert len(scripting._description_cache) <= 4

    def test_the_cache_bound_is_512(self):
        """Pinned by value: relaxing a resource cap must be argued for."""
        assert scripting.MAX_DESCRIPTION_CACHE == 512


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

    def test_a_name_prefix_narrows_the_candidates_before_parsing(self, completer_tree):
        """The prefix filters here now, not only downstream.

        This used to `assert script_completer("bas") == script_completer("")`
        - completing four characters that identify one file cost exactly as
        much as completing nothing, because every candidate was fully
        YAML-parsed and the whole set handed to prompt_toolkit to filter.
        For a 200-script `--scripts-dir` that is ~600 ms on the door
        server's own event loop, from one keystroke.
        """
        narrowed = script_completer("bas")

        assert [name for name, _ in narrowed] == ["basic_cycle"]
        assert len(script_completer("")) > len(narrowed)

    def test_the_prefix_filter_is_case_insensitive(self, completer_tree):
        """It has to match `prompt_common`'s downstream filter exactly.

        That filter is `name.lower().startswith(word_before.lower())`, so a
        case-sensitive pre-filter here would silently stop offering
        completions the prompt would otherwise have shown.
        """
        assert [name for name, _ in script_completer("BAS")] == ["basic_cycle"]
        assert [name for name, _ in script_completer("LOCAL.")] == ["local.yaml"]
        assert [name for name, _ in script_completer("SUBDIR/")] == []
        assert [name for name, _ in script_completer("SUBDI")] == ["subdir/"]

    def test_a_prefix_matching_nothing_returns_nothing(self, completer_tree):
        assert script_completer("zzz-no-such-script") == []

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
    """A YAML script is a named untrusted input in this threat model.

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
    """ctl's daemon refuses script paths, so ctl must not complete them.

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
# Concurrent Script Execution
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
        # would hang the suite forever instead of failing.
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


class TestScriptBooleanCoercion:
    """Script booleans go through one coercer, and **raise** on nonsense.

    They used to fail closed, inheriting that from the wire parser. On the
    wire it is right - an unreadable flag must never *grant* access, and a
    schedule the device already stores must stay loadable. For a value
    someone typed it is a bug: `set power maybe` silently turned the power
    *off* and reported PASSED.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("true", True),
            ("1", True),
            (1, True),
            ("on", True),
            ("yes", True),
            ("enabled", True),
            ("false", False),
            ("0", False),
            (0, False),
            ("off", False),
            ("no", False),
            ("disabled", False),
        ],
        ids=repr,
    )
    def test_script_bool_matrix(self, value, expected):
        assert ScriptRunner._script_bool(value) is expected

    @pytest.mark.parametrize(
        "value",
        ["banana", None, [1], "", 2, -1],
        ids=repr,
    )
    def test_an_unrecognized_spelling_raises(self, value):
        """Not "false" - a mistake. Numbers other than 0/1 included:
        truthiness is not the question a boolean field is asking."""
        with pytest.raises(ScriptError, match="must be true or false"):
            ScriptRunner._script_bool(value)

    async def test_a_misspelled_setting_value_fails_the_script(self, runner, simulator):
        """The whole point: it must not quietly mean "off"."""
        simulator.state.power = True

        result = await runner.run(Script.from_simple_commands(["set power maybe"]), verbose=False)

        assert result is False
        assert simulator.state.power is True

    async def test_a_quoted_false_no_longer_enables_a_schedule(self, runner, simulator):
        """`enabled: "0"` produced an *enabled* schedule.

        Unquoted YAML `false` parses to a real bool, so this only bit a
        quoted or templated value - but the same argument applies to the
        numeric parameters, which are all bounded.
        """
        await runner._execute_step(
            ScriptStep(action="add_schedule", params={"index": 3, "enabled": "0"})
        )

        assert simulator.state.schedules[3].enabled is False
        assert simulator.state.schedules[3].to_dict()["enabled"] == 0

    async def test_a_quoted_true_still_enables_a_schedule(self, runner, simulator):
        await runner._execute_step(
            ScriptStep(action="add_schedule", params={"index": 4, "enabled": "yes"})
        )

        assert simulator.state.schedules[4].enabled is True

    async def test_open_holds_and_cycle_does_not(self, runner, simulator):
        """The two actions differ only in the flag they pass through.

        They dispatch to the same `open_door`, so nothing downstream can
        tell them apart - this is the only place the split is visible.
        """
        held: list[bool] = []

        async def record(hold=False):
            held.append(hold)

        simulator.open_door = record

        await runner._execute_step(ScriptStep(action="open", params={}))
        await runner._execute_step(ScriptStep(action="cycle", params={}))

        assert held == [True, False]


#: The one condition vocabulary, spelled out. `assert`, `wait_for` and
#: `if` all name it, so all three errors quote the same list - which is the
#: visible half of them sharing one reader.
#: The tail every "Unknown condition" message carries. Derived, because
#: three actions quote the same list and pinning it as a literal only
#: proved they had been edited together. The *set* is pinned separately,
#: below, so a vocabulary change is still a visible line in a diff.
CONDITION_VOCABULARY = "Use: " + ", ".join(scripting.CONDITION_NAMES)


def _chain_names(function_name: str, variable: str) -> set[str]:
    """The names one of `scripting`'s `x == "..."` dispatch chains accepts."""
    source = Path(scripting.__file__).read_text()
    body = source.split(f"def {function_name}", 1)[1].split("\n    def ", 1)[0]
    return set(re.findall(rf'{variable} == "([a-z_]+)"', body))


def _reader_names(function_name: str) -> set[str]:
    """The keys of a `{"name": lambda ...}` dispatch table.

    Some keys are built by comprehension - one counter per notification -
    so an f-string prefix over a module constant is resolved rather than
    skipped. Skipping would hide a whole family of condition from the
    guard, which is the opposite of what it is for.
    """
    source = Path(scripting.__file__).read_text()
    body = source.split(f"def {function_name}", 1)[1].split("\n    def ", 1)[0]
    names = set(re.findall(r'"([a-z_]+)": lambda', body))
    for prefix, var in re.findall(r'f"([a-z_]+)\{(\w+)\}"\s*:', body):
        for const in re.findall(rf"for {var} in ([A-Z][A-Z_]+)", body):
            names |= {f"{prefix}{v}" for v in getattr(scripting, const, ())}
    return names


class TestUnknownNameErrorsNameTheAlternatives:
    """Five of the DSL's seven "unknown name" errors named nothing.

    The script DSL is the *CI* front end - these messages are read in a
    build log with no terminal to experiment in, which is exactly where
    naming the alternatives is worth the most - and every one of them had
    the accepted set as a literal in the same function. The sharpest was
    `Unknown assertion condition: door_closed`: the single most natural
    assertion in a door simulator, and a name the runner recognises *for
    the other action*.

    Each published tuple is pinned against the chain it describes, so the
    message cannot drift from the implementation.
    """

    @pytest.mark.parametrize(
        ("published", "function_name", "variable"),
        [
            (scripting.SET_SETTINGS, "writable", None),
            (scripting.TOGGLE_SETTINGS, "boolean", None),
        ],
        ids=["set", "toggle"],
    )
    def test_the_published_set_matches_the_registry(self, published, function_name, variable):
        """Both derive from the value table rather than a dispatch chain,
        so a value added there is settable without touching this file."""
        from powerpetdoor.simulator.values import VALUES

        expected = {n for n, v in VALUES.items() if v.writable}
        if function_name == "boolean":
            expected = {n for n in expected if VALUES[n].kind == "bool"}
        assert set(published) == expected

    def test_the_published_assert_set_matches_the_reader_table(self):
        """`assert`, `wait_for` and `if` all read through one table.

        Every named value is a condition, plus the computed ones a value
        table cannot hold.
        """
        from powerpetdoor.simulator.values import VALUE_NAMES

        assert set(scripting.ASSERT_CONDITIONS) == (
            set(VALUE_NAMES) | set(scripting._COMPUTED_CONDITIONS)
        )

    def test_wait_for_and_assert_take_the_same_vocabulary(self):
        """There used to be two disjoint sets plus 22 "fused" names that
        baked a value into the name. One vocabulary now."""
        assert scripting.WAIT_FOR_CONDITIONS == scripting.ASSERT_CONDITIONS
        assert scripting.CONDITION_NAMES == scripting.ASSERT_CONDITIONS

    def test_the_condition_vocabulary_is_the_union_of_both(self):
        assert set(scripting.CONDITION_NAMES) == (
            set(scripting.ASSERT_CONDITIONS) | set(scripting.WAIT_FOR_CONDITIONS)
        )

    def test_every_published_set_is_sorted_and_unique(self):
        """The message renders them in order; a duplicate would show twice."""
        for published in (
            scripting.ASSERT_CONDITIONS,
            scripting.SET_SETTINGS,
            scripting.TOGGLE_SETTINGS,
            scripting.WAIT_FOR_CONDITIONS,
        ):
            assert list(published) == sorted(set(published))

    def test_toggle_accepts_exactly_the_boolean_settings(self):
        """`toggle` takes the yes/no values; the rest hold a number or a
        string and have nothing to invert."""
        from powerpetdoor.simulator.values import VALUES

        assert set(scripting.TOGGLE_SETTINGS) == {
            name for name in scripting.SET_SETTINGS if VALUES[name].kind == "bool"
        }
        assert set(scripting.SET_SETTINGS) - set(scripting.TOGGLE_SETTINGS) == {
            name for name in scripting.SET_SETTINGS if VALUES[name].kind != "bool"
        }

    def test_the_two_condition_vocabularies_are_no_longer_disjoint(self):
        """They used to be, and that was the wart.

        `assert door_closed` is the most natural assertion in a door
        simulator and was rejected, purely because `wait_for` owned the
        name. One vocabulary now: every fused shorthand works in `assert`
        and every plain condition works in `wait_for`.
        """
        assert set(scripting.WAIT_FOR_CONDITIONS) - set(scripting.CONDITION_NAMES) == set()
        assert set(scripting.ASSERT_CONDITIONS) - set(scripting.CONDITION_NAMES) == set()

    @pytest.mark.parametrize(
        ("step", "expected"),
        [
            (
                ScriptStep(action="frobnicate"),
                "Unknown action: frobnicate. Use: add_schedule, assert, battery, "
                "clear_schedules, close, cycle, enable_schedule, if, inside, log, notify, "
                "obstruction, open, outside, remove_schedule, repeat, reset, set, toggle, "
                "trigger, wait, wait_for",
            ),
            (
                ScriptStep(action="set", params={"name": "powr", "value": "1"}),
                "Unknown setting: powr. Use: ac_present, auto, autoretract, battery, battery_present, charge_rate, closing_mid_time, closing_start_time, closing_top_time, cmd_lockout, discharge_rate, firmware_version, hardware_version, has_remote_id, has_remote_key, hold_time, inside, notify_inside_off, notify_inside_on, notify_low_battery, notify_outside_off, notify_outside_on, obstruction, outside, power, rise_time, safety_lock, sensor_retrigger_window, sensor_trigger_voltage, sleep_sensor_trigger_voltage, slowing_time, timezone, total_auto_retracts, total_open_cycles",
            ),
            (
                ScriptStep(action="set", params={"name": "powr", "value": "toggle"}),
                "Unknown setting to toggle: powr. Use: ac_present, auto, autoretract, battery_present, cmd_lockout, has_remote_id, has_remote_key, inside, notify_inside_off, notify_inside_on, notify_low_battery, notify_outside_off, notify_outside_on, obstruction, outside, power, safety_lock",
            ),
            (
                ScriptStep(action="assert", params={"condition": "bogus", "equals": "x"}),
                "Unknown condition: bogus. " + CONDITION_VOCABULARY,
            ),
            (
                ScriptStep(action="wait_for", params={"condition": "door_stat", "timeout": 0.01}),
                "Unknown condition: door_stat. " + CONDITION_VOCABULARY,
            ),
        ],
        ids=["action", "setting", "toggle", "assert", "condition"],
    )
    async def test_the_message_names_the_accepted_set(self, runner, simulator, step, expected):
        with pytest.raises(ScriptError) as error:
            await runner._execute_step(step)

        assert str(error.value) == expected

    async def test_a_value_setting_cannot_be_toggled(self, runner, simulator):
        """`hold_time` has no state to invert, and the message says why.

        It used to point at the `set` action, which made sense when
        `toggle` was its own action. It is reached through
        `set hold_time toggle` now, so pointing at `set` would be telling
        the author to use what they are already using.
        """
        with pytest.raises(ScriptError) as error:
            await runner._execute_step(
                ScriptStep(action="set", params={"name": "hold_time", "value": "toggle"})
            )

        assert "Cannot toggle hold_time: it holds a value, not a state" in str(error.value)

    async def test_a_name_valid_nowhere_says_it_is_unknown(self, runner, simulator):
        """The control: a name that is no setting at all reads differently
        from one that is a setting but holds a value."""
        with pytest.raises(ScriptError) as error:
            await runner._execute_step(
                ScriptStep(action="set", params={"name": "bogus", "value": "toggle"})
            )

        assert "Unknown setting to toggle: bogus" in str(error.value)


def _action_chain_names() -> set[str]:
    """Every action name `_execute_step` dispatches on."""
    return set(_parameters_read_per_action())


def _execute_step_body() -> str:
    """The source of `ScriptRunner._execute_step`, for the drift guards."""
    source = Path(scripting.__file__).read_text()
    return source.split("async def _execute_step", 1)[1].split("\n    async def ", 1)[0]


#: Both ways a step parameter is read: `params.get("x")` and `params["x"]`.
_PARAM_READ = r'params(?:\.get\(\s*|\[)"([a-z_]+)"'

#: A call handed the whole `params` dict, whatever else it is given -
#: `self._presence(params, default=0.5)` counts as much as
#: `self._if_holds(params)`. Matching only the bare form let the
#: comparison operators and `duration` read as declared-but-unused.
_DELEGATES_PARAMS = r"self\.(_[a-z_]+)\([^)]*\bparams\b"


def _method_body(name: str) -> str:
    """The source of one `ScriptRunner` method.

    Signature-agnostic: some of these are `@staticmethod` and take no
    `self`, and some are `async`.
    """
    source = Path(scripting.__file__).read_text()
    body = source.split(f"def {name}(", 1)[1]
    return re.split(r"\n    (?:async )?def ", body, maxsplit=1)[0]


def _constant_driven_reads(body: str) -> set[str]:
    """Parameter names a body reads by *iterating a constant*.

    `_condition_holds` asks `for key in COMPARISONS if key in params`, so
    `equals`, `above` and the rest never appear as a literal subscript and
    a regex over the source cannot see them. Exempting them would blind
    the guard to a whole class of parameter; resolving the constant keeps
    it honest.
    """
    names: set[str] = set()
    if "in params" not in body:
        return names
    for const in re.findall(r"for \w+ in ([A-Z][A-Z_]+)", body):
        value = getattr(scripting, const, None)
        if isinstance(value, tuple) and all(isinstance(v, str) for v in value):
            names |= set(value)
    return names


def _parameters_read_per_action() -> dict[str, set[str]]:
    """Map action -> the parameter names read inside *its* branch.

    Splits `_execute_step` on its own `if/elif action == "..."` chain, the
    same extraction `test_every_executed_action_declares_its_parameters`
    performs, so the two guards read the source the same way.

    A branch that hands the whole `params` dict to a helper - as `if` does
    to `_if_holds` - has its reads counted too. Without that the guard
    would report every delegated parameter as declared-but-unread, and the
    obvious way to silence it would be to stop declaring them, which is
    exactly the validation this DSL exists to keep.
    """
    blocks = re.split(r"\n        (?:el)?if action (?:==|in) ", _execute_step_body())
    per_action: dict[str, set[str]] = {}
    for block in blocks[1:]:
        header, _, body = block.partition(":\n")
        # `inside`/`outside` share one branch, spelled `action in
        # SENSOR_NAMES`, because a sensor is a sensor. The guard has to
        # follow that or it reports both as undeclared.
        names = (
            list(SENSOR_NAMES) if "SENSOR_NAMES" in header else re.findall(r'"([a-z_]+)"', header)
        )
        assert names, f"could not read an action name from {header!r}"
        read = set(re.findall(_PARAM_READ, body))
        pending = list(re.findall(_DELEGATES_PARAMS, body))
        seen: set[str] = set()
        while pending:
            helper = pending.pop()
            if helper in seen:
                continue
            seen.add(helper)
            helper_body = _method_body(helper)
            read |= set(re.findall(_PARAM_READ, helper_body))
            read |= _constant_driven_reads(helper_body)
            # Transitively: `repeat` delegates to `_execute_repeat`, which
            # delegates again to `_if_holds`.
            pending += re.findall(_DELEGATES_PARAMS, helper_body)
        for name in names:
            per_action[name] = read
    return per_action


class TestUnknownNamesInStepsFailLoudly:
    """Every user-supplied name in this DSL must fail loudly when misspelled.

    A misspelled *action* has always failed. A misspelled *sensor* did not:
    the engine's gates are `if sensor == "inside"` / `elif sensor ==
    "outside"`, so an unrecognised name matched none of them and fell
    through to the door open. One character opened the door with both
    sensors disabled and the safety lock on, and still reported PASSED -
    which over `ctl run <name> wait` is a green CI exit code. A misspelled
    *parameter* was ignored just as silently, and the progress log echoed
    it back as if accepted.
    """

    async def test_an_unknown_sensor_fails_the_script(self, runner, simulator, caplog):
        script = Script(
            name="Typo",
            steps=[ScriptStep(action="trigger", params={"sensor": "insde"}, line_number=1)],
        )

        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)

        assert result is False
        assert "Script error at step 1: Unknown sensor: insde. Use: inside, outside" in caplog.text

    async def test_an_unknown_sensor_cannot_bypass_the_gates(self, runner, simulator):
        """The control: identical state, one character apart.

        With both sensors disabled and the safety lock on, the real sensor
        name is gated and the typo used to open the door anyway.
        """
        simulator.state.inside = False
        simulator.state.outside = False
        simulator.state.safety_lock = True

        typo = Script(
            name="Typo",
            steps=[ScriptStep(action="trigger", params={"sensor": "insde"}, line_number=1)],
        )
        assert await runner.run(typo, verbose=False) is False
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        real = Script.from_simple_commands(["trigger inside"], name="Real")
        assert await runner.run(real, verbose=False) is True
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    @pytest.mark.parametrize("sensor", ["inside", "outside"])
    async def test_the_real_sensor_names_still_work(self, runner, simulator, sensor):
        simulator.state.safety_lock = False
        script = Script.from_simple_commands([f"trigger {sensor}"], name="Real")

        assert await runner.run(script, verbose=False) is True
        assert simulator.state.door_status == DOOR_STATE_RISING

    @pytest.mark.parametrize(
        ("action", "params", "expected"),
        [
            (
                "wait",
                {"duration": 8},
                "Unknown parameter(s) for wait: duration. Use: seconds "
                "(plus the annotations comment, description, note)",
            ),
            (
                "trigger",
                {"sensr": "inside"},
                "Unknown parameter(s) for trigger: sensr. Use: sensor "
                "(plus the annotations comment, description, note)",
            ),
            # "Use: none" read as an instruction to pass the literal token
            # `none`.
            (
                "close",
                {"hold": True},
                "Unknown parameter(s) for close: hold. close takes no parameters "
                "(plus the annotations comment, description, note)",
            ),
            # `open` used to take `hold`, and `hold: false` meant what is
            # now `cycle`. A script carrying the old spelling must fail
            # rather than hold a door the author asked to let close.
            (
                "open",
                {"hold": False},
                "Unknown parameter(s) for open: hold. open takes no parameters "
                "(plus the annotations comment, description, note)",
            ),
            (
                "wait_for",
                {"condition": "door_closed", "timout": 1, "zzz": 2},
                "Unknown parameter(s) for wait_for: timout, zzz. Use: above, any, at_least, at_most, below, condition, conditions, equals, not_equals, on_timeout, timeout "
                "(plus the annotations comment, description, note)",
            ),
        ],
        ids=[
            "wait-duration",
            "sensor-typo",
            "no-params-action",
            "retired-open-hold",
            "two-unknowns",
        ],
    )
    async def test_an_unknown_parameter_fails_the_script(
        self, runner, simulator, caplog, action, params, expected
    ):
        script = Script(
            name="Typo", steps=[ScriptStep(action=action, params=params, line_number=1)]
        )

        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            result = await runner.run(script, verbose=False)

        assert result is False
        assert f"Script error at step 1: {expected}" in caplog.text

    @pytest.mark.parametrize("annotation", sorted(scripting.STEP_ANNOTATION_KEYS))
    async def test_a_documented_annotation_is_accepted_on_any_step(
        self, runner, simulator, annotation
    ):
        """Making unknown parameters an error broke annotated user scripts.

        `- action: wait / seconds: 1 / note: let the door settle` is an
        ordinary thing for a YAML author to write, and the strict check
        turned its exit code from 0 into 1. A closed, documented set of
        annotation keys keeps the strictness where it matters.
        """
        script = Script(
            name="Annotated",
            steps=[
                ScriptStep(
                    action="wait",
                    params={"seconds": 0.01, annotation: "let the door settle"},
                    line_number=1,
                ),
                ScriptStep(action="close", params={annotation: "and shut it"}, line_number=2),
            ],
        )

        assert await runner.run(script, verbose=False) is True

    async def test_an_annotation_does_not_excuse_a_typod_real_parameter(
        self, runner, simulator, caplog
    ):
        """The strictness that motivated the change must survive it."""
        script = Script(
            name="Annotated typo",
            steps=[
                ScriptStep(
                    action="wait",
                    params={"duration": 8, "note": "why is this not waiting"},
                    line_number=1,
                )
            ],
        )

        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            assert await runner.run(script, verbose=False) is False

        assert "Unknown parameter(s) for wait: duration" in caplog.text
        assert "note" not in caplog.text.split("Unknown parameter(s) for wait: ")[1].split(".")[0]

    async def test_an_annotation_is_read_by_nothing(self, runner, simulator):
        """`note:` must not shadow a real parameter or change behaviour."""
        loop = asyncio.get_running_loop()
        started = loop.time()

        await runner._execute_step(
            ScriptStep(action="wait", params={"seconds": 0.05, "note": "10"}, line_number=1)
        )

        assert 0.04 < loop.time() - started < 1.0

    async def test_the_typod_wait_no_longer_silently_shortens_the_wait(self, runner, simulator):
        """The observable substance: 8 s asked for, 1 s taken, PASSED reported."""
        loop = asyncio.get_running_loop()
        script = Script(
            name="Typo", steps=[ScriptStep(action="wait", params={"duration": 8}, line_number=1)]
        )

        started = loop.time()
        result = await runner.run(script, verbose=False)

        assert result is False
        assert loop.time() - started < 1.0

    async def test_an_unknown_action_is_still_reported_as_such(self, runner, simulator, caplog):
        """The action chain stays authoritative for unknown *actions*."""
        script = Script(name="Bad", steps=[ScriptStep(action="frobnicate", line_number=1)])

        with caplog.at_level(logging.ERROR, logger=SCRIPT_LOGGER):
            assert await runner.run(script, verbose=False) is False

        assert "Unknown action: frobnicate" in caplog.text

    def test_every_executed_action_declares_its_parameters(self):
        """`_ACTION_PARAMS` must not drift from the dispatch chain.

        If an action is added to `_execute_step` and not to the table its
        parameters silently stop being validated, which is exactly the
        state this fix found the DSL in.
        """
        # `inside`/`outside` share one branch (`action in SENSOR_NAMES`),
        # so read the chain through the helper that understands both
        # spellings rather than a bare `==` regex.
        assert _action_chain_names() == set(scripting._ACTION_PARAMS)

    def test_every_declared_parameter_is_actually_read_by_that_action(self):
        """...and the table must not grow parameters nothing consumes.

        This used to flatten both sides:
        `set().union(*_ACTION_PARAMS.values()) - read == set()`, where
        `read` was scraped from the whole `_execute_step` body. Six
        parameter names are shared by more than one action (`condition`,
        `duration`, `index`, `name`, `sensor`, `value`), so 11 of the 19
        actions could gain a fictional parameter and the union check could
        not notice - not merely "was not tested against one mutation", but
        structurally incapable.

        The failure mode that protects against: the progress log echoes the
        typo back as accepted, the parameter does nothing, and `ctl run
        <name> wait` exits 0 - a green CI result for a script that tested
        nothing.
        """
        per_action = _parameters_read_per_action()

        assert per_action == {
            action: set(params) for action, params in scripting._ACTION_PARAMS.items()
        }

    def test_the_union_check_this_replaced_really_was_blind(self):
        """The reason the assertion above is per-action and not flattened.

        The old check was `set().union(*_ACTION_PARAMS.values()) - read`,
        with `read` scraped from the whole method body. Adding any already
        declared name to any action leaves both sides unchanged, so the
        check is *structurally* incapable of noticing - not merely
        untested. Demonstrated exhaustively rather than asserted.
        """
        table = scripting._ACTION_PARAMS
        # The same reads the real guard sees, only flattened - which is
        # precisely the difference that made the old check blind.
        read = set().union(*_parameters_read_per_action().values())
        declared = set().union(*table.values())
        assert declared - read == set(), "the old check passes on the real table"

        undetected = [
            (action, name)
            for action in table
            for name in declared
            if name not in table[action]
            and (declared | {name}) - read == set()  # what the old check would compute
        ]

        assert len(undetected) == len(table) * len(declared) - sum(len(p) for p in table.values())
        assert len(table) == 22
        assert len(declared) == 26
        # ...and the per-action check sees every one of them.
        per_action = _parameters_read_per_action()
        for action, name in undetected:
            grown = {**{a: set(p) for a, p in table.items()}, action: set(table[action]) | {name}}
            assert grown != per_action

    def test_most_actions_share_a_parameter_name(self):
        """Why the blindness is a *present-day* hole, not a hypothetical."""
        table = scripting._ACTION_PARAMS
        shared = {
            name
            for name in set().union(*table.values())
            if sum(name in params for params in table.values()) > 1
        }

        assert shared == {
            "above",
            "any",
            "at_least",
            "at_most",
            "below",
            "condition",
            "conditions",
            "duration",
            "equals",
            "index",
            "name",
            "not_equals",
            "state",
            "value",
        }
        assert sum(1 for params in table.values() if params & shared) == 13

    def test_annotation_keys_never_collide_with_a_real_parameter(self):
        """`note:` must stay a no-op, not shadow something an action reads."""
        declared = set().union(*scripting._ACTION_PARAMS.values())

        assert scripting.STEP_ANNOTATION_KEYS & declared == set()
        assert scripting.STEP_ANNOTATION_KEYS == {"comment", "description", "note"}


class TestSimpleCommandShorthandDefaults:
    """The documented 2-word forms were never supplied by any test.

    `from_simple_commands` reads `parts[2]` only when `len(parts) > 2`, so
    `> 2` -> `>= 2` would raise IndexError on the documented 2-word form -
    and survived the whole suite, because every test supplied three words.
    These pin the documented defaults.
    """

    def test_wait_for_defaults_to_a_thirty_second_timeout(self):
        script = Script.from_simple_commands(["wait_for door_closed"])

        assert script.steps[0].params == {"condition": "door_closed", "timeout": 30.0}

    def test_set_defaults_to_an_empty_value(self):
        script = Script.from_simple_commands(["set power"])

        assert script.steps[0].params == {"name": "power", "value": ""}

    def test_assert_defaults_to_an_empty_expectation(self):
        script = Script.from_simple_commands(["assert door_status"])

        assert script.steps[0].params == {"condition": "door_status"}

    def test_the_three_word_forms_still_carry_their_third_word(self):
        """The control: the branch that was exercised must keep working."""
        assert Script.from_simple_commands(["wait_for door_closed 10"]).steps[0].params == {
            "condition": "door_closed",
            "timeout": 10.0,
        }
        assert Script.from_simple_commands(["set power 1"]).steps[0].params == {
            "name": "power",
            "value": "1",
        }
        assert Script.from_simple_commands(["assert door_status DOOR_CLOSED"]).steps[0].params == {
            "condition": "door_status",
            "equals": "DOOR_CLOSED",
        }


class TestTheAnnotationKeysAreDocumented:
    """A closed set is only usable if it is written down."""

    def test_the_docs_name_every_annotation_key(self):
        doc = (Path(scripting.__file__).parents[3] / "docs" / "scripting.md").read_text()
        section = " ".join(doc.split())

        for key in scripting.STEP_ANNOTATION_KEYS:
            assert f"`{key}`" in section
        assert "accepted on any step and are read by nothing" in section


class TestUnknownTopLevelKeysAreRefused:
    """The last silent misspelling class in this DSL.

    `Script.from_yaml` read exactly three keys with `data.get(...)` defaults
    and never looked at what else was in the mapping, so `stpes:` produced a
    zero-step script that printed `>>> Script PASSED` and exited **0** - the
    whole file silently became a no-op that still reported success. Every
    other misspelling class (action, sensor, condition, setting, step
    parameter) fails loudly by deliberate decision.
    """

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (
                "name: Door cycle regression test\nstpes:\n  - action: log\n    message: x\n",
                "Unknown top-level key(s): stpes. Use: description, name, steps",
            ),
            (
                "nmae: Door cycle\nsteps:\n  - action: log\n    message: x\n",
                "Unknown top-level key(s): nmae. Use: description, name, steps",
            ),
            (
                "name: X\nsteps: []\nzzz: 1\naaa: 2\n",
                "Unknown top-level key(s): aaa, zzz. Use: description, name, steps",
            ),
            # A non-string key stringifies rather than raising in the join.
            (
                "name: X\nsteps: []\n1: 2\n",
                "Unknown top-level key(s): 1. Use: description, name, steps",
            ),
        ],
        ids=["steps-typo", "name-typo", "two-unknowns", "non-string-key"],
    )
    def test_an_unknown_top_level_key_is_a_load_error(self, content, expected):
        with pytest.raises(ScriptError) as exc_info:
            Script.from_yaml(content)

        assert str(exc_info.value) == expected

    def test_an_empty_steps_list_is_still_legal(self):
        """`steps: []` legitimately means "no steps", so the check has to be
        on unknown keys and not on emptiness."""
        script = Script.from_yaml("name: Empty\ndescription: nothing\nsteps: []\n")

        assert script.name == "Empty"
        assert script.steps == []

    def test_every_shipped_script_still_loads(self):
        """The control: a new refusal must not refuse the built-ins."""
        for name, _description in list_builtin_scripts():
            assert get_builtin_script(name).steps

    def test_the_refusal_surfaces_through_the_listing(self, tmp_path, monkeypatch):
        """A load-time failure, so `list` / `--list-scripts` report it via the
        existing `(Error loading: ...)` path rather than showing a script that
        would silently pass."""
        (tmp_path / "broken.yaml").write_text("name: Broken\nstpes:\n  - action: log\n")
        scripting.set_extra_scripts_dir(str(tmp_path))
        monkeypatch.setattr(scripting, "_description_cache", {})

        lines = scripting.render_script_listing(str(tmp_path)).lines

        assert any(
            "broken: (Error loading: Unknown top-level key(s): stpes." in line for line in lines
        )


class TestOneConditionVocabulary:
    """`assert`, `wait_for` and `if` read through one table.

    They used to take disjoint vocabularies: `assert door_closed` - the
    most natural assertion in a door simulator - was rejected purely
    because `wait_for` owned the name, and `_other_table_hint` existed to
    apologise for it.
    """

    async def test_assert_accepts_a_fused_wait_for_name(self, runner, simulator):
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        await runner._execute_step(ScriptStep(action="assert", params={"condition": "door_closed"}))

    async def test_assert_fails_on_a_fused_name_that_does_not_hold(self, runner, simulator):
        with pytest.raises(ScriptAssertionError):
            await runner._execute_step(
                ScriptStep(action="assert", params={"condition": "door_open"})
            )

    async def test_wait_for_accepts_a_plain_assert_name_and_a_value(self, runner, simulator):
        """The user-facing point of the merge: wait for 3 auto retracts."""
        simulator.state.total_auto_retracts = 3

        await runner._execute_step(
            ScriptStep(
                action="wait_for",
                params={"condition": "total_auto_retracts", "equals": 3, "timeout": 1},
            )
        )

    async def test_wait_for_a_door_status_by_value(self, runner, simulator):
        await runner._execute_step(
            ScriptStep(
                action="wait_for",
                params={"condition": "door_status", "equals": DOOR_STATE_CLOSED, "timeout": 1},
            )
        )

    async def test_an_unknown_condition_names_the_whole_vocabulary(self, runner, simulator):
        with pytest.raises(ScriptError) as error:
            await runner._execute_step(ScriptStep(action="assert", params={"condition": "bogus"}))

        assert "door_closed" in str(error.value)
        assert "total_auto_retracts" in str(error.value)


class TestWaitForOnTimeout:
    async def test_the_default_still_fails(self, runner, simulator):
        with pytest.raises(ScriptError, match="Timeout waiting for condition"):
            await runner._execute_step(
                ScriptStep(action="wait_for", params={"condition": "door_open", "timeout": 0.05})
            )

    async def test_continue_carries_on(self, runner, simulator):
        await runner._execute_step(
            ScriptStep(
                action="wait_for",
                params={"condition": "door_open", "timeout": 0.05, "on_timeout": "continue"},
            )
        )

        assert simulator.state.door_status == DOOR_STATE_CLOSED

    async def test_a_misspelled_on_timeout_fails_loudly(self, runner, simulator):
        """Silently ignoring it would turn a guarded wait back into a
        failing one with no sign in the log."""
        with pytest.raises(ScriptError, match="Unknown on_timeout: contineu"):
            await runner._execute_step(
                ScriptStep(
                    action="wait_for",
                    params={"condition": "door_open", "timeout": 0.05, "on_timeout": "contineu"},
                )
            )


class TestIfElse:
    async def test_the_then_branch_runs_when_it_holds(self, runner, simulator):
        simulator.simulate_obstruction(0)

        await runner._execute_step(
            ScriptStep(
                action="if",
                params={
                    "condition": "obstruction",
                    "equals": "on",
                    "then": [ScriptStep(action="obstruction")],
                    "else": [ScriptStep(action="open")],
                },
            )
        )

        assert simulator.state.obstruction_active is False

    async def test_the_else_branch_runs_when_it_does_not(self, runner, simulator):
        """Which is why `if` needs an `else`: writing the inverted guard a
        second time is what this replaces."""
        await runner._execute_step(
            ScriptStep(
                action="if",
                params={
                    "condition": "obstruction",
                    "equals": "on",
                    "then": [ScriptStep(action="open")],
                    "else": [ScriptStep(action="obstruction")],
                },
            )
        )

        assert simulator.state.obstruction_active is True

    async def test_a_missing_branch_is_simply_skipped(self, runner, simulator):
        await runner._execute_step(
            ScriptStep(action="if", params={"condition": "door_open", "then": []})
        )

    async def test_an_unknown_condition_in_an_if_fails(self, runner, simulator):
        with pytest.raises(ScriptError, match="Unknown condition"):
            await runner._execute_step(
                ScriptStep(action="if", params={"condition": "bogus", "then": []})
            )

    async def test_a_branch_that_is_not_a_list_is_refused(self, runner, simulator):
        with pytest.raises(ScriptError, match="must be a list of steps"):
            await runner._execute_step(
                ScriptStep(action="if", params={"condition": "door_closed", "then": "open"})
            )


class TestRepeat:
    async def test_it_runs_the_block_that_many_times(self, runner, simulator):
        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={
                    "times": 3,
                    "steps": [ScriptStep(action="obstruction", params={"duration": 0})],
                },
            )
        )

        assert simulator.state.obstruction_active is True

    async def test_zero_times_runs_nothing(self, runner, simulator):
        """The boundary: 0 is a legal count, not a synonym for 1."""
        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={"times": 0, "steps": [ScriptStep(action="obstruction")]},
            )
        )

        assert simulator.state.obstruction_active is False

    async def test_one_time_runs_it_once(self, runner, simulator):
        """The other side of the boundary."""
        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={"times": 1, "steps": [ScriptStep(action="obstruction")]},
            )
        )

        assert simulator.state.obstruction_active is True

    async def test_a_condition_makes_it_a_while_loop(self, runner, simulator):
        """Re-tested before each pass, so the body can end it."""
        simulator.simulate_obstruction(0)

        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={
                    "condition": "obstruction",
                    "equals": "on",
                    "steps": [ScriptStep(action="obstruction")],
                },
            )
        )

        assert simulator.state.obstruction_active is False

    async def test_a_condition_already_false_runs_nothing(self, runner, simulator):
        """The test is before the body, not after: this is `while`, not
        `do-while`."""
        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={
                    "condition": "obstruction",
                    "equals": "on",
                    "steps": [ScriptStep(action="open")],
                },
            )
        )

        assert simulator.state.door_status == DOOR_STATE_CLOSED

    async def test_the_condition_list_forms_work_here_too(self, runner, simulator):
        """Same three spellings as `if`, so there is nothing new to learn."""
        simulator.simulate_obstruction(0)

        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={
                    "conditions": [{"condition": "obstruction", "equals": "on"}, "door_closed"],
                    "steps": [ScriptStep(action="obstruction")],
                },
            )
        )

        assert simulator.state.obstruction_active is False

    async def test_times_and_a_condition_stop_at_whichever_comes_first(self, runner, simulator):
        """`times` wins here: the condition would never end the loop."""
        logged: list[str] = []
        simulator.simulate_obstruction(0)

        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={
                    "times": 3,
                    "condition": "obstruction",
                    "equals": "on",
                    "steps": [ScriptStep(action="battery", params={"percent": 50})],
                },
            )
        )
        del logged

        assert simulator.state.obstruction_active is True

    async def test_the_condition_wins_when_it_ends_first(self, runner, simulator):
        simulator.simulate_obstruction(0)

        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={
                    "times": 99,
                    "condition": "obstruction",
                    "equals": "on",
                    "steps": [ScriptStep(action="obstruction")],
                },
            )
        )

        assert simulator.state.obstruction_active is False

    async def test_neither_times_nor_a_condition_is_refused(self, runner, simulator):
        """A `repeat` with no bound at all says nothing about what it does."""
        with pytest.raises(ScriptError, match="needs 'times', a condition, or both"):
            await runner._execute_step(
                ScriptStep(action="repeat", params={"steps": [ScriptStep(action="close")]})
            )

    async def test_a_condition_that_never_ends_is_an_error(self, runner, simulator, monkeypatch):
        """A hung build that reported PASSED would be worse than one that
        merely hangs, so hitting the backstop fails the run."""
        monkeypatch.setattr(scripting, "MAX_SCRIPT_REPEAT", 5)

        with pytest.raises(ScriptError, match="without its condition going false"):
            await runner._execute_step(
                ScriptStep(
                    action="repeat",
                    params={
                        "condition": "door_closed",
                        "steps": [ScriptStep(action="log", params={"message": "x"})],
                    },
                )
            )

    async def test_an_unbounded_count_is_refused(self, runner, simulator):
        """A runner that took `times: 1e9` would hand an unattended CI job
        a hang - the same damage `set hold_time inf` used to do."""
        with pytest.raises(ScriptError, match="between"):
            await runner._execute_step(
                ScriptStep(
                    action="repeat",
                    params={"times": 1e9, "steps": [ScriptStep(action="close")]},
                )
            )

    async def test_a_stop_request_breaks_out(self, runner, simulator):
        runner.stop()

        await runner._execute_step(
            ScriptStep(
                action="repeat",
                params={"times": 5, "steps": [ScriptStep(action="obstruction")]},
            )
        )

        assert simulator.state.obstruction_active is False


class TestNestedBlocksAreParsedAtLoadTime:
    """A bad action inside an untaken branch must still fail the load.

    Parsing lazily would mean a script passed CI for months and then broke
    the first time its `else` was reached.
    """

    def test_a_bad_action_in_an_untaken_branch_fails_the_load(self):
        """The guarantee this class is named for, finally asserted.

        The class checked only that nested blocks became `ScriptStep`
        objects - which they did - while the actions inside them were
        never looked at. A script whose untaken `else` was full of
        nonsense ran to completion and reported PASSED, and scripts gate
        CI by exit code, so the failure mode was a green run that tested
        nothing.
        """
        with pytest.raises(ScriptError, match="Unknown action: totally_bogus"):
            Script.from_yaml(
                "name: t\n"
                "steps:\n"
                "  - action: if\n"
                "    condition: door_closed\n"
                "    then:\n"
                "      - action: close\n"
                "    else:\n"
                "      - action: totally_bogus\n"
            )

    def test_a_bad_parameter_in_an_untaken_branch_fails_the_load(self):
        with pytest.raises(ScriptError, match="Unknown parameter"):
            Script.from_yaml(
                "name: t\n"
                "steps:\n"
                "  - action: if\n"
                "    condition: door_closed\n"
                "    then:\n"
                "      - action: close\n"
                "    else:\n"
                "      - action: close\n"
                "        nonexistent_param: 1\n"
            )

    def test_a_bad_action_inside_repeat_fails_the_load(self):
        """`repeat` holds a block too, and a repeat of zero never runs it."""
        with pytest.raises(ScriptError, match="Unknown action: totally_bogus"):
            Script.from_yaml(
                "name: t\nsteps:\n  - action: repeat\n    count: 0\n"
                "    steps:\n      - action: totally_bogus\n"
            )

    def test_the_bare_string_form_is_validated_too(self):
        """`- clsoe`, the shorthand the docs invite.

        The first version of this check ran only on the mapping form, so
        the shorthand walked straight past it and a script of pure
        nonsense still reported PASSED - the same failure, one spelling
        over. A fix that lands on one path when two exist is a failed fix.
        """
        with pytest.raises(ScriptError, match="Unknown action: clsoe"):
            Script.from_yaml("name: t\nsteps:\n  - clsoe\n")

    def test_the_bare_string_form_is_validated_inside_an_untaken_branch(self):
        with pytest.raises(ScriptError, match="Unknown action: clsoe"):
            Script.from_yaml(
                "name: t\nsteps:\n  - action: if\n    condition: door_closed\n"
                "    then:\n      - close\n    else:\n      - clsoe\n"
            )

    def test_the_valid_shorthand_still_loads(self):
        script = Script.from_yaml("name: t\nsteps:\n  - close\n  - open\n")
        assert [s.action for s in script.steps] == ["close", "open"]

    def test_a_stray_block_key_is_refused_at_load(self):
        """The load check must not be weaker than the run check.

        It subtracted the block keywords for EVERY action, which excused
        them on actions that have no block: `close` with a `then:` passed
        the load and was refused at run time, and inside an untaken
        branch was never caught at all.
        """
        with pytest.raises(ScriptError, match="Unknown parameter"):
            Script.from_yaml("name: t\nsteps:\n  - action: close\n    then:\n      - open\n")

    def test_the_real_block_keywords_are_still_accepted(self):
        """`if` owns then/else and `repeat` owns steps - they are its parameters."""
        assert Script.from_yaml(
            "name: t\nsteps:\n  - action: if\n    condition: door_closed\n"
            "    then:\n      - close\n    else:\n      - open\n"
        )
        assert Script.from_yaml(
            "name: t\nsteps:\n  - action: repeat\n    times: 2\n    steps:\n      - close\n"
        )

    def test_a_load_error_names_the_step(self):
        """The executor always said which step; the load check dropped it."""
        with pytest.raises(ScriptError, match="Step 2:"):
            Script.from_yaml("name: t\nsteps:\n  - close\n  - clsoe\n")

    def test_a_non_string_action_is_named_rather_than_crashing(self):
        """`action: 42` is truthy, so it passes the missing-action check.

        It used to reach the executor and raise AttributeError on
        `.lower()`; now it is refused by name like any other action the
        DSL does not have.
        """
        with pytest.raises(ScriptError, match="Unknown action: 42"):
            Script.from_yaml("name: t\nsteps:\n  - action: 42\n")

    def test_a_block_keyword_is_not_reported_as_an_unknown_parameter(self):
        """`then`/`else`/`steps` are the blocks themselves, not stray keys."""
        script = Script.from_yaml(
            "name: t\nsteps:\n  - action: if\n    condition: door_closed\n"
            "    then:\n      - action: close\n    else:\n      - action: open\n"
        )
        assert len(script.steps) == 1

    def test_a_nested_block_becomes_script_steps(self):
        script = Script.from_yaml(
            "name: t\n"
            "steps:\n"
            "  - action: if\n"
            "    condition: door_closed\n"
            "    then:\n"
            "      - action: close\n"
        )

        assert isinstance(script.steps[0].params["then"][0], ScriptStep)
        assert script.steps[0].params["then"][0].action == "close"

    def test_a_step_without_an_action_inside_a_block_is_refused(self):
        with pytest.raises(ScriptError, match="missing 'action'"):
            Script.from_yaml(
                "name: t\nsteps:\n  - action: repeat\n    times: 1\n    steps:\n      - times: 2\n"
            )

    def test_a_block_that_is_not_a_list_is_refused_at_load(self):
        with pytest.raises(ScriptError, match="'steps' must be a list"):
            Script.from_yaml("name: t\nsteps:\n  - action: repeat\n    steps: nope\n")

    def test_the_progress_log_summarises_a_block(self):
        """The log is what a script author reads; dumping every nested
        dataclass repr buried the run in one unreadable line."""
        step = ScriptStep(
            action="repeat",
            params={"times": 2, "steps": [ScriptStep(action="close"), ScriptStep(action="open")]},
        )

        assert str(step) == "repeat(times=2, steps=[2 steps])"

    def test_a_single_step_block_is_not_pluralised(self):
        step = ScriptStep(action="if", params={"then": [ScriptStep(action="close")]})

        assert str(step) == "if(then=[1 step])"


class TestResetAction:
    async def test_a_bare_reset_restores_the_runners_initial_document(self, simulator):
        runner = ScriptRunner(simulator, initial_state_document={"settings": {"hold_time": 33}})
        simulator.state.hold_time = 1

        await runner._execute_step(ScriptStep(action="reset"))

        assert simulator.state.hold_time == 33

    async def test_without_an_initial_document_it_restores_the_defaults(self, runner, simulator):
        simulator.state.hold_time = 99

        await runner._execute_step(ScriptStep(action="reset"))

        assert simulator.state.hold_time == DoorSimulatorState().hold_time

    async def test_a_named_document_goes_through_the_handlers_loader(self, simulator):
        """The loader owns the path policy, so a script cannot reach a file
        the control channel would refuse."""
        runner = ScriptRunner(
            simulator, load_state_document=lambda name: {"settings": {"hold_time": 21}}
        )

        await runner._execute_step(ScriptStep(action="reset", params={"initial_state": "fixture"}))

        assert simulator.state.hold_time == 21

    async def test_a_loader_refusal_fails_the_step(self, simulator):
        def refuse(name):
            raise ValueError("nope")

        runner = ScriptRunner(simulator, load_state_document=refuse)

        with pytest.raises(ScriptError, match="nope"):
            await runner._execute_step(ScriptStep(action="reset", params={"initial_state": "x"}))

    async def test_a_runner_with_no_loader_says_so(self, runner, simulator):
        with pytest.raises(ScriptError, match="cannot load state documents"):
            await runner._execute_step(ScriptStep(action="reset", params={"initial_state": "x"}))


class TestStopInterruptsANestedBlock:
    """A stop between steps must reach inside a block too.

    The runner checks between steps; a nested block that ignored the
    request would run to completion after a `stop`, and `stop` would look
    like it had not registered.
    """

    async def test_a_stop_between_steps_inside_a_block_ends_it(self, runner, simulator):
        """The check is *between* the steps of one block.

        `repeat` also checks before each iteration, so only a multi-step
        block whose stop lands part-way through reaches this one - and a
        block that ran its remaining steps after a stop would make `stop`
        look like it had not registered.
        """
        runner.stop()

        with pytest.raises(ScriptError, match="Script stopped"):
            await runner._execute_block([ScriptStep(action="log", params={"message": "one"})])

    async def test_a_stop_ends_a_running_block(self, runner, simulator):
        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop()

        script = Script.from_yaml(
            "name: t\n"
            "steps:\n"
            "  - action: repeat\n"
            "    times: 50\n"
            "    steps:\n"
            "      - action: wait\n"
            "        seconds: 0.01\n"
        )
        asyncio.get_running_loop().create_task(stop_soon())

        assert await runner.run(script, verbose=False) is False


class TestIfConditionForms:
    """`if` states its condition one of three ways, and only one at a time."""

    async def test_conditions_requires_all_of_them(self, runner, simulator):
        simulator.simulate_obstruction(0)

        await runner._execute_step(
            ScriptStep(
                action="if",
                params={
                    "conditions": [
                        {"condition": "obstruction", "equals": "on"},
                        {"condition": "door_closed"},
                        {"condition": "hold_time", "equals": simulator.state.hold_time},
                    ],
                    "then": [ScriptStep(action="obstruction")],
                },
            )
        )

        assert simulator.state.obstruction_active is False

    async def test_conditions_is_false_when_one_fails(self, runner, simulator):
        """The decisive operand is the *second*: a chain that only ever ran
        with every entry true would never exercise the short-circuit."""
        await runner._execute_step(
            ScriptStep(
                action="if",
                params={
                    "conditions": ["door_closed", {"condition": "obstruction", "equals": "on"}],
                    "then": [ScriptStep(action="open")],
                    "else": [ScriptStep(action="obstruction")],
                },
            )
        )

        assert simulator.state.obstruction_active is True

    async def test_any_needs_only_one(self, runner, simulator):
        await runner._execute_step(
            ScriptStep(
                action="if",
                params={
                    "any": ["door_open", "door_closed"],
                    "then": [ScriptStep(action="obstruction")],
                },
            )
        )

        assert simulator.state.obstruction_active is True

    async def test_any_is_false_when_none_hold(self, runner, simulator):
        await runner._execute_step(
            ScriptStep(
                action="if",
                params={
                    "any": ["door_open", {"condition": "power", "equals": "off"}],
                    "then": [ScriptStep(action="obstruction")],
                    "else": [ScriptStep(action="log", params={"message": "none"})],
                },
            )
        )

        assert simulator.state.obstruction_active is False

    async def test_a_bare_string_entry_is_a_fused_shorthand(self, runner, simulator):
        """Same courtesy the step list already gives: `- close` is a step,
        so `- door_closed` is a condition."""
        assert runner._entry_holds("door_closed", "conditions") is True

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"condition": "door_closed", "conditions": ["door_closed"]},
            {"conditions": ["door_closed"], "any": ["door_closed"]},
            {"condition": "door_closed", "any": ["door_closed"]},
        ],
        ids=["none", "condition+conditions", "conditions+any", "condition+any"],
    )
    async def test_exactly_one_form_is_required(self, runner, simulator, params):
        """Two forms is two answers to one question - a typo, not a
        refinement - and none at all is a step that says nothing."""
        with pytest.raises(ScriptError, match="exactly one of"):
            await runner._execute_step(ScriptStep(action="if", params={**params, "then": []}))

    async def test_equals_beside_a_list_form_is_refused(self, runner, simulator):
        """It belongs on an entry; ignoring it would make a typo look
        accepted."""
        with pytest.raises(ScriptError, match="'equals' applies to 'condition'"):
            await runner._execute_step(
                ScriptStep(
                    action="if",
                    params={"conditions": ["door_closed"], "equals": "on", "then": []},
                )
            )

    @pytest.mark.parametrize("value", [[], "door_closed", 3], ids=["empty", "string", "int"])
    async def test_a_list_form_must_be_a_non_empty_list(self, runner, simulator, value):
        with pytest.raises(ScriptError, match="non-empty list"):
            await runner._execute_step(
                ScriptStep(action="if", params={"conditions": value, "then": []})
            )

    async def test_an_entry_that_is_neither_name_nor_mapping_is_refused(self, runner, simulator):
        with pytest.raises(ScriptError, match="condition name or a mapping"):
            await runner._execute_step(ScriptStep(action="if", params={"any": [3], "then": []}))

    async def test_an_entry_without_a_condition_is_refused(self, runner, simulator):
        with pytest.raises(ScriptError, match="has no 'condition'"):
            await runner._execute_step(
                ScriptStep(action="if", params={"conditions": [{"equals": "on"}], "then": []})
            )

    async def test_an_unknown_key_in_an_entry_is_refused(self, runner, simulator):
        with pytest.raises(ScriptError, match="Unknown key"):
            await runner._execute_step(
                ScriptStep(
                    action="if",
                    params={"conditions": [{"condition": "door_closed", "equal": "x"}], "then": []},
                )
            )

    async def test_nesting_expresses_what_flat_and_or_cannot(self, runner, simulator):
        """`(a and b) or c` needs no nested boolean syntax: blocks nest.

        Which is why there is deliberately no nested `and:`/`or:` - that is
        where a config DSL becomes a language you debug instead of use.
        """
        script = Script.from_yaml(
            "name: t\n"
            "steps:\n"
            "  - action: if\n"
            "    conditions: [door_closed, power]\n"
            "    then:\n"
            "      - action: if\n"
            "        condition: obstruction\n"
            "        equals: 'off'\n"
            "        then:\n"
            "          - action: obstruction\n"
        )

        assert await runner.run(script, verbose=False) is True
        assert simulator.state.obstruction_active is True


class TestComparisonOperators:
    """`equals` alone made most conditions unusable.

    "wait until the door has opened 100 times" or "assert the battery is
    below 75" had no spelling at all.
    """

    @pytest.mark.parametrize(
        ("operator", "limit", "holds"),
        [
            ("equals", 50, True),
            ("equals", 51, False),
            ("not_equals", 51, True),
            ("not_equals", 50, False),
            ("above", 49, True),
            ("above", 50, False),
            ("below", 51, True),
            ("below", 50, False),
            ("at_least", 50, True),
            ("at_least", 51, False),
            ("at_most", 50, True),
            ("at_most", 49, False),
        ],
    )
    async def test_each_operator_at_its_boundary(self, runner, simulator, operator, limit, holds):
        """Both sides of every boundary: `above` excludes its limit,
        `at_least` includes it, and the pairs are exact inverses."""
        simulator.state.battery_percent = 50

        assert runner._if_holds({"condition": "battery", operator: limit}) is holds

    async def test_more_than_one_comparison_is_refused(self, runner, simulator):
        """Two answers to one question is a typo, not a range."""
        with pytest.raises(ScriptError, match="more than one comparison"):
            runner._if_holds({"condition": "battery", "above": 10, "below": 90})

    async def test_a_non_boolean_condition_needs_a_comparison(self, runner, simulator):
        """`condition: battery` alone has no sensible default."""
        with pytest.raises(ScriptError, match="not a yes/no condition"):
            runner._if_holds({"condition": "battery"})

    @pytest.mark.parametrize("condition", ["door_status", "door_closed"])
    async def test_a_numeric_comparison_needs_a_number(self, runner, simulator, condition):
        """Comparing a status string against 25 has no meaning, and
        silently answering False would hide the mistake."""
        with pytest.raises(ScriptError, match="needs a numeric condition"):
            runner._if_holds({"condition": condition, "above": 25})

    async def test_position_is_the_numeric_door_condition(self, runner, simulator):
        """Mirrors PowerPetDoor.position, so the two cannot disagree."""
        assert runner._if_holds({"condition": "position", "equals": 0}) is True

        simulator.state.door_status = DOOR_STATE_KEEPUP
        assert runner._if_holds({"condition": "position", "at_least": 100}) is True

    async def test_a_boolean_condition_compares_as_a_boolean(self, runner, simulator):
        """`equals: 1` and `equals: enabled` both mean "on"; comparing the
        rendered text accepted only some spellings."""
        simulator.state.power = True

        for spelling in (True, 1, "1", "on", "true", "yes", "enabled"):
            assert runner._if_holds({"condition": "power", "equals": spelling}) is True

    async def test_a_bad_boolean_expectation_is_refused(self, runner, simulator):
        with pytest.raises(ScriptError, match="must be true or false"):
            runner._if_holds({"condition": "power", "equals": "maybe"})


class TestTheDoorToggleAction:
    """`toggle` is the door, matching the CLI."""

    async def test_it_opens_a_closed_door(self, runner, simulator):
        await runner._execute_step(ScriptStep(action="toggle"))

        assert await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

    async def test_it_closes_an_open_door(self, runner, simulator):
        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        await runner._execute_step(ScriptStep(action="toggle"))

        assert await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    async def test_it_does_nothing_mid_travel(self, runner, simulator):
        simulator.state.door_status = DOOR_STATE_RISING

        await runner._execute_step(ScriptStep(action="toggle"))

        assert simulator.state.door_status == DOOR_STATE_RISING


class TestObstructionScriptForms:
    """The script action takes the argument the CLI command takes."""

    @pytest.mark.parametrize(
        ("params", "active", "oneshot"),
        [
            ({}, True, True),
            ({"state": "on"}, True, False),
            ({"duration": 0}, True, False),
        ],
        ids=["bare-one-shot", "on-until-cleared", "zero-until-cleared"],
    )
    async def test_placing_it(self, runner, simulator, params, active, oneshot):
        await runner._execute_step(ScriptStep(action="obstruction", params=params))

        assert simulator.state.obstruction_active is active
        assert simulator.state.obstruction_oneshot is oneshot

    async def test_state_off_clears_it(self, runner, simulator):
        """The explicit clear a script needs: read out of order, "say it
        again to toggle" is not an instruction anyone can follow."""
        simulator.simulate_obstruction(0)

        await runner._execute_step(ScriptStep(action="obstruction", params={"state": "off"}))

        assert simulator.state.obstruction_active is False

    async def test_toggle_flips_it(self, runner, simulator):
        await runner._execute_step(ScriptStep(action="obstruction", params={"state": "toggle"}))
        assert simulator.state.obstruction_active is True

        await runner._execute_step(ScriptStep(action="obstruction", params={"state": "toggle"}))
        assert simulator.state.obstruction_active is False

    async def test_a_misspelled_state_is_refused(self, runner, simulator):
        with pytest.raises(ScriptError, match="expected on, off, toggle"):
            await runner._execute_step(
                ScriptStep(action="obstruction", params={"state": "nonsense"})
            )


class TestSensorScriptForms:
    """`inside`/`outside` take the same argument, and are pet presence."""

    async def test_state_on_holds_the_sensor(self, runner, simulator):
        await runner._execute_step(ScriptStep(action="inside", params={"state": "on"}))

        assert simulator.state.pet_present("inside") is True

    async def test_state_off_releases_it(self, runner, simulator):
        simulator.hold_sensor("outside", True)

        await runner._execute_step(ScriptStep(action="outside", params={"state": "off"}))

        assert simulator.state.pet_present("outside") is False

    async def test_a_duration_still_works(self, runner, simulator):
        await runner._execute_step(ScriptStep(action="inside", params={"duration": 0.05}))

        assert simulator.state.pet_present("inside") is True
        await asyncio.sleep(0.15)
        assert simulator.state.pet_present("inside") is False


class TestSimpleCommandShorthandForNewActions:
    """The one-line form, which `run` and the tests both use."""

    def test_it_parses_the_new_actions(self):
        script = Script.from_simple_commands(
            ["if door_closed", "inside on", "inside 0.5", "obstruction off", "toggle"]
        )

        assert [s.params for s in script.steps] == [
            {"condition": "door_closed"},
            {"state": "on"},
            {"duration": "0.5"},
            {"state": "off"},
            {},
        ]


class TestWaitForOnListConditions:
    """A list or a comparison has no transition to be signalled by, so it
    polls - and the error message still has to name what was waited on."""

    async def test_a_condition_list_can_be_waited_on(self, runner, simulator):
        await runner._execute_step(
            ScriptStep(
                action="wait_for",
                params={"conditions": ["door_closed", "power"], "timeout": 1},
            )
        )

    async def test_the_timeout_names_the_list_form(self, runner, simulator):
        with pytest.raises(ScriptError, match="conditions="):
            await runner._execute_step(
                ScriptStep(
                    action="wait_for",
                    params={"conditions": ["door_open"], "timeout": 0.05},
                )
            )

    async def test_the_timeout_names_the_any_form(self, runner, simulator):
        with pytest.raises(ScriptError, match="any="):
            await runner._execute_step(
                ScriptStep(action="wait_for", params={"any": ["door_open"], "timeout": 0.05})
            )

    async def test_a_numeric_comparison_polls(self, runner, simulator):
        """Not a status transition, so it cannot be signalled."""
        assert runner._status_set_for({"condition": "battery", "above": 10}) is None

    def test_a_condition_with_no_form_at_all_describes_itself(self, runner):
        """The `(none)` fallback, which only a malformed step reaches."""
        assert runner._describe_condition({}) == "(none)"

    def test_a_bare_sensor_shorthand_takes_the_default_pulse(self):
        """`inside` with no argument is a pulse, not a hold."""
        script = Script.from_simple_commands(["inside", "obstruction"])

        assert [s.params for s in script.steps] == [{}, {}]


class TestTheDoorConditionsAreSymmetric:
    """`door_closing` had no `door_opening` counterpart.

    `DOOR_STATES_OPENING` existed in `const.py` the whole time - it is
    half of what `is_open` is built from - so the asymmetry was an
    omission rather than a decision.
    """

    @pytest.mark.parametrize(
        ("status", "opening", "closing"),
        [
            (DOOR_STATE_CLOSED, False, False),
            (DOOR_STATE_RISING, True, False),
            (DOOR_STATE_SLOWING, True, False),
            (DOOR_STATE_HOLDING, False, False),
            (DOOR_STATE_KEEPUP, False, False),
            (DOOR_STATE_CLOSING, False, True),
            (DOOR_STATE_CLOSING_TOP_OPEN, False, True),
            (DOOR_STATE_CLOSING_MID_OPEN, False, True),
        ],
    )
    async def test_every_status_answers_both(self, runner, simulator, status, opening, closing):
        """The pair partitions travel: no state is both, and each of the
        five moving states is exactly one."""
        simulator.state.door_status = status

        assert runner._if_holds({"condition": "door_opening"}) is opening
        assert runner._if_holds({"condition": "door_closing"}) is closing

    async def test_waiting_for_opening_is_signalled_not_polled(self, runner, simulator):
        """`DOOR_RISING` is brief; a 50 ms poll would miss it, which is
        exactly why `door_closing` takes the deterministic path too."""
        assert runner._status_set_for({"condition": "door_opening"}) == (
            DOOR_STATE_RISING,
            DOOR_STATE_SLOWING,
        )

    async def test_it_is_in_the_published_vocabulary(self):
        assert "door_opening" in scripting.ASSERT_CONDITIONS
        assert "door_opening" in scripting.CONDITION_NAMES


class TestTheConditionVocabularyIsPinned:
    """The names themselves, as a literal.

    The error messages derive their list from this tuple, so they cannot
    catch it changing. This can: adding or removing a condition has to be
    a deliberate line in a diff, not a side effect of touching the value
    registry.
    """

    def test_the_exact_set(self):
        assert scripting.CONDITION_NAMES == (
            "ac_present",
            "auto",
            "autoretract",
            "battery",
            "battery_present",
            "charge_rate",
            "closing_mid_time",
            "closing_start_time",
            "closing_top_time",
            "cmd_lockout",
            "discharge_rate",
            "door_closed",
            "door_closing",
            "door_open",
            "door_opening",
            "door_status",
            "firmware_version",
            "hardware_version",
            "has_remote_id",
            "has_remote_key",
            "hold_time",
            "inside",
            "notified_inside_off",
            "notified_inside_on",
            "notified_low_battery",
            "notified_outside_off",
            "notified_outside_on",
            "notify_inside_off",
            "notify_inside_on",
            "notify_low_battery",
            "notify_outside_off",
            "notify_outside_on",
            "obstruction",
            "outside",
            "position",
            "power",
            "rise_time",
            "safety_lock",
            "sensor_retrigger_window",
            "sensor_trigger_voltage",
            "sleep_sensor_trigger_voltage",
            "slowing_time",
            "time",
            "timezone",
            "total_auto_retracts",
            "total_open_cycles",
        )

    def test_the_comparisons(self):
        assert scripting.COMPARISONS == (
            "equals",
            "not_equals",
            "above",
            "below",
            "at_least",
            "at_most",
        )


class TestTheNotifyAction:
    """A script that waits on a notification has to be able to enable it."""

    @pytest.mark.parametrize(
        ("name", "attribute"),
        sorted(scripting.NOTIFICATION_SETTINGS.items()),
    )
    async def test_it_switches_each_one_on(self, runner, simulator, name, attribute):
        setattr(simulator.state, attribute, False)

        await runner._execute_step(ScriptStep(action="notify", params={"name": name}))

        assert getattr(simulator.state, attribute) is True

    async def test_state_off_switches_one_back_off(self, runner, simulator):
        simulator.state.sensor_on_indoor = True

        await runner._execute_step(
            ScriptStep(action="notify", params={"name": "inside_on", "state": "off"})
        )

        assert simulator.state.sensor_on_indoor is False

    async def test_a_hyphenated_name_normalizes(self, runner, simulator):
        await runner._execute_step(ScriptStep(action="notify", params={"name": "low-battery"}))

        assert simulator.state.low_battery is True

    async def test_an_unknown_name_is_refused(self, runner, simulator):
        with pytest.raises(ScriptError, match="Unknown notification: bogus"):
            await runner._execute_step(ScriptStep(action="notify", params={"name": "bogus"}))

    async def test_the_counters_are_readable_as_conditions(self, runner, simulator):
        """Counts, so a script can wait for the *third* one."""
        simulator.state.sensor_on_indoor = True
        assert runner._if_holds({"condition": "notified_inside_on", "equals": 0}) is True

        simulator.notify("inside_on")
        simulator.notify("inside_on")

        assert runner._if_holds({"condition": "notified_inside_on", "at_least": 2}) is True
