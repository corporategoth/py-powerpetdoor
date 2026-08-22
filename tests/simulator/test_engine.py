# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the shared door-motion engine (engine.py)."""

from __future__ import annotations

import asyncio

import pytest

from powerpetdoor.const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    SENSOR_STATE_ON,
)
from powerpetdoor.simulator import DoorSimulatorState, DoorTimingConfig
from powerpetdoor.simulator.engine import DoorMotionEngine

FULL_OPEN_CLOSE_SEQUENCE = [
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSED,
]


@pytest.fixture
def timing_config():
    """Create a fast timing config for tests."""
    return DoorTimingConfig(
        rise_time=0.03,
        default_hold_time=1,
        slowing_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
def state(timing_config):
    """Create a test state with fast timing and a short hold."""
    return DoorSimulatorState(timing=timing_config, hold_time=0.05)


@pytest.fixture
async def engine(state):
    """Create an engine (stopped after the test)."""
    eng = DoorMotionEngine(state)
    yield eng
    await eng.stop()


class TestStatusHooks:
    """wait_for_status and status listeners (D9 deterministic hooks)."""

    async def test_wait_for_status_returns_immediately_on_match(self, engine, state):
        """No wait when the door is already in the requested state."""
        assert state.door_status == DOOR_STATE_CLOSED
        result = await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=0.001)
        assert result == DOOR_STATE_CLOSED

    async def test_wait_for_status_wakes_on_transition(self, engine, state):
        """A waiter subscribed before the transition is woken by it."""
        waiter = asyncio.ensure_future(engine.wait_for_status(DOOR_STATE_RISING))
        await asyncio.sleep(0)  # let the waiter subscribe

        engine.open()

        assert await asyncio.wait_for(waiter, timeout=1.0) == DOOR_STATE_RISING

    async def test_wait_for_status_accepts_multiple_statuses(self, engine, state):
        """An iterable of acceptable statuses matches any of them."""
        engine.open(hold=True)
        result = await engine.wait_for_status((DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP), timeout=2.0)
        assert result == DOOR_STATE_KEEPUP

    async def test_wait_for_status_times_out(self, engine):
        """The wait raises TimeoutError when the status is never reached."""
        with pytest.raises(TimeoutError):
            await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=0.05)
        # The failed waiter is cleaned up
        assert engine._status_waiters == []

    async def test_status_listener_sees_every_transition(self, engine, state):
        """A status listener records the exact full-cycle sequence."""
        seen: list[str] = []
        unsubscribe = engine.add_status_listener(seen.append)

        engine.open()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

        assert seen == FULL_OPEN_CLOSE_SEQUENCE
        unsubscribe()

    async def test_status_listener_unsubscribe(self, engine, state):
        """After unsubscribing, the listener no longer fires."""
        seen: list[str] = []
        unsubscribe = engine.add_status_listener(seen.append)
        unsubscribe()
        unsubscribe()  # double-unsubscribe is a no-op

        engine.open()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
        assert seen == []

    async def test_raising_listener_does_not_break_others(self, engine, state):
        """One raising listener must not stop other listeners or the door."""

        def bad_listener(status: str) -> None:
            raise RuntimeError("boom")

        seen: list[str] = []
        engine.add_status_listener(bad_listener)
        engine.add_status_listener(seen.append)

        engine.open()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
        assert seen == FULL_OPEN_CLOSE_SEQUENCE

    async def test_raising_broadcast_does_not_stop_door(self, state):
        """A broadcast callback failure must not kill the sequence task."""

        def bad_broadcast() -> None:
            raise RuntimeError("broadcast down")

        engine = DoorMotionEngine(state, broadcast_status=bad_broadcast)
        try:
            engine.open()
            result = await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
            assert result == DOOR_STATE_CLOSED
        finally:
            await engine.stop()


class TestReentrantStatusListeners:
    """A status listener may command the door without duplicating the runner.

    ``_set_status`` fires listeners synchronously inside the sequence owner
    task, so a listener calling ``open()``/``close()`` used to leave the
    original ``_run`` task looping alongside a freshly created one (M1):
    doubled transitions and a doubled ``total_open_cycles``.
    """

    async def test_close_from_holding_listener_runs_one_sequence(self, engine, state):
        """close() from a HOLDING listener replaces (not duplicates) the run."""
        seen: list[str] = []

        def closer(status: str) -> None:
            seen.append(status)
            if status == DOOR_STATE_HOLDING:
                engine.close()

        engine.add_status_listener(closer)

        engine.open()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

        assert seen == FULL_OPEN_CLOSE_SEQUENCE
        assert state.total_open_cycles == 1
        assert len(engine._pending_tasks()) == 0

    async def test_two_reentrant_requests_in_one_dispatch_coalesce(self, engine, state):
        """Two listeners commanding the same close start exactly one sequence."""
        seen: list[str] = []
        engine.add_status_listener(seen.append)

        def closer(status: str) -> None:
            if status == DOOR_STATE_HOLDING:
                engine.close()

        engine.add_status_listener(closer)
        engine.add_status_listener(closer)

        engine.open()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

        # A second deferred restart would re-emit CLOSING_TOP_OPEN.
        assert seen == FULL_OPEN_CLOSE_SEQUENCE
        assert state.total_open_cycles == 1

    async def test_close_from_the_opening_listener_reverses_once(self, engine, state):
        """A listener commanding close() on the very first status change."""
        seen: list[str] = []

        def closer(status: str) -> None:
            seen.append(status)
            if status == DOOR_STATE_RISING:
                engine.close()

        engine.add_status_listener(closer)

        engine.open()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

        assert seen == [DOOR_STATE_RISING, DOOR_STATE_CLOSING_MID_OPEN, DOOR_STATE_CLOSED]
        assert state.total_open_cycles == 1
        assert len(engine._pending_tasks()) == 0

    async def test_stop_drops_a_deferred_sequence_start(self, state):
        """stop() before the loop applies the deferred restart starts nothing."""
        engine = DoorMotionEngine(state)
        engine.add_status_listener(
            lambda status: engine.close() if status == DOOR_STATE_RISING else None
        )

        engine.open()
        assert engine._restart_handle is not None

        await engine.stop()

        assert engine._restart_handle is None
        assert engine._task is None
        await asyncio.sleep(0)  # a surviving call_soon would fire here
        assert engine._task is None
        assert state.door_status == DOOR_STATE_RISING

    async def test_cancel_nowait_drops_a_deferred_sequence_start(self, state):
        """cancel_nowait() also drops the deferred restart."""
        engine = DoorMotionEngine(state)
        engine.add_status_listener(
            lambda status: engine.close() if status == DOOR_STATE_RISING else None
        )

        engine.open()
        task = engine._task
        engine.cancel_nowait()

        assert engine._restart_handle is None
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert state.door_status == DOOR_STATE_RISING
        await engine.stop()


class TestOpenClose:
    """State-aware open/close behavior."""

    async def test_full_cycle_transitions(self, engine, state):
        """A plain open runs the complete cycle and counts it."""
        assert engine.open() is True
        assert state.door_status == DOOR_STATE_RISING

        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
        assert state.total_open_cycles == 1

    async def test_open_hold_reaches_keepup_and_stays(self, engine, state):
        """open(hold=True) ends in KEEPUP with the sequence task finished."""
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        # The sequence task is done - KEEPUP is a terminal state
        await asyncio.gather(engine._task, return_exceptions=True)
        assert state.door_status == DOOR_STATE_KEEPUP

    async def test_open_noop_when_open_or_opening(self, engine, state):
        """open() is a no-op in HOLDING/KEEPUP/RISING/SLOWING."""
        engine.open(hold=True)
        assert engine.open() is False  # RISING -> no-op

        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        assert engine.open() is False  # KEEPUP -> no-op
        assert state.door_status == DOOR_STATE_KEEPUP

    async def test_close_noop_when_closed(self, engine, state):
        """close() is a no-op when the door is closed."""
        assert engine.close() is False
        assert state.door_status == DOOR_STATE_CLOSED

    async def test_close_from_keepup_runs_close_sequence(self, engine, state):
        """close() from KEEPUP walks CLOSING_TOP -> CLOSING_MID -> CLOSED."""
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        seen: list[str] = []
        engine.add_status_listener(seen.append)
        assert engine.close() is True

        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)
        assert seen == [
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
            DOOR_STATE_CLOSED,
        ]

    async def test_close_noop_when_already_closing(self, engine, state):
        """close() is a no-op while the door is already closing."""
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        assert engine.close() is True
        assert state.door_status == DOOR_STATE_CLOSING_TOP_OPEN

        assert engine.close() is False  # already closing -> no-op
        assert state.door_status == DOOR_STATE_CLOSING_TOP_OPEN
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    async def test_run_started_in_terminal_state_exits(self, engine, state):
        """A sequence started from a terminal state performs no motion."""
        assert state.door_status == DOOR_STATE_CLOSED
        await asyncio.wait_for(engine._run(), timeout=1.0)
        assert state.door_status == DOOR_STATE_CLOSED
        assert state.total_open_cycles == 0

    async def test_close_reverses_rising_to_closing_mid(self, engine, state):
        """close() while RISING reverses position-consistently to CLOSING_MID."""
        engine.open()
        assert state.door_status == DOOR_STATE_RISING

        engine.close()
        assert state.door_status == DOOR_STATE_CLOSING_MID_OPEN
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    async def test_close_reverses_slowing_to_closing_top(self, engine, state):
        """close() while SLOWING reverses position-consistently to CLOSING_TOP."""
        engine.open()
        await engine.wait_for_status(DOOR_STATE_SLOWING, timeout=2.0)

        engine.close()
        assert state.door_status == DOOR_STATE_CLOSING_TOP_OPEN
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    async def test_open_reverses_closing_top_to_slowing(self, engine, state):
        """open() while CLOSING_TOP reverses position-consistently to SLOWING."""
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        engine.close()
        assert state.door_status == DOOR_STATE_CLOSING_TOP_OPEN

        engine.open()
        assert state.door_status == DOOR_STATE_SLOWING
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

    async def test_open_reverses_closing_mid_to_rising(self, engine, state):
        """open() while CLOSING_MID reverses position-consistently to RISING."""
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        engine.close()
        waiter = asyncio.ensure_future(engine.wait_for_status(DOOR_STATE_CLOSING_MID_OPEN))
        await asyncio.wait_for(waiter, timeout=2.0)

        engine.open()
        assert state.door_status == DOOR_STATE_RISING
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

    async def test_reversal_retires_old_task_without_warnings(self, engine, state):
        """Replacing a running sequence retires (and later awaits) the old task."""
        engine.open()
        first_task = engine._task
        engine.close()  # reversal replaces the task

        assert engine._task is not first_task
        await asyncio.gather(first_task, return_exceptions=True)
        assert first_task.cancelled()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)


class TestHoldBehavior:
    """Deadline-based hold-open behavior (T6) and sensor blocking."""

    async def test_hold_expires_and_door_closes(self, engine, state):
        """With no sensors active, HOLDING transitions to closing on its own."""
        engine.open()
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)
        assert state.door_status == DOOR_STATE_CLOSED

    async def test_extend_hold_pushes_deadline(self, engine, state):
        """extend_hold grants a full hold_time from now."""
        state.hold_time = 10.0  # long, so the deadline is clearly visible
        engine.open()
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

        loop = asyncio.get_running_loop()
        engine.extend_hold()
        assert engine._hold_deadline == pytest.approx(loop.time() + 10.0, abs=0.5)

    async def test_blocking_sensor_keeps_door_open(self, engine, state):
        """An active enabled sensor prevents the hold from expiring."""
        engine.simulate_obstruction()
        engine.open()
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

        # Wait well past the hold time - the door must still be open
        await asyncio.sleep(float(state.hold_time) * 4)
        assert state.door_status == DOOR_STATE_HOLDING

        # Clearing the sensor (with notification) lets the door close
        state.inside_sensor_active = False
        engine.notify_sensors_changed()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    async def test_trigger_sensor_while_holding_extends_hold(self, engine, state):
        """A sensor re-trigger during HOLDING resets the hold deadline."""
        state.hold_time = 10.0
        engine.open()
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

        loop = asyncio.get_running_loop()
        engine._last_sensor_trigger = 0.0  # outside the retrigger window
        engine.trigger_sensor("inside")
        assert engine._hold_deadline == pytest.approx(loop.time() + 10.0, abs=0.5)

    async def test_retrigger_within_window_is_ignored(self, engine, state):
        """Re-triggers inside the retrigger window do not notify again."""
        notifications: list[tuple[str, str]] = []
        engine.notify_sensor = lambda sensor, st: notifications.append((sensor, st))

        engine.open()
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

        engine._last_sensor_trigger = 0.0
        engine.trigger_sensor("inside")  # extends, notifies
        engine.trigger_sensor("inside")  # within window - ignored
        assert notifications == [("inside", SENSOR_STATE_ON)]


class TestAutoRetract:
    """Sensor-during-close auto-retract (M8: no self-cancel)."""

    async def test_sensor_during_close_causes_retract(self, engine, state):
        """A blocking sensor during closing reverses the door (auto-retract)."""
        seen: list[str] = []
        engine.add_status_listener(seen.append)

        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        engine.close()
        assert state.door_status == DOOR_STATE_CLOSING_TOP_OPEN

        # Pet in the doorway while closing
        engine.trigger_sensor("inside")
        assert state.inside_sensor_active is True

        # The SAME sequence task chains into the re-open and full cycle
        task = engine._task
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

        assert engine._task is task
        assert not task.cancelled()
        assert state.total_auto_retracts == 1
        assert state.inside_sensor_active is False
        # Full observed sequence: keepup-open, close attempt, retract, close
        assert seen == [
            DOOR_STATE_RISING,
            DOOR_STATE_SLOWING,
            DOOR_STATE_KEEPUP,
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_SLOWING,
            DOOR_STATE_HOLDING,
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
            DOOR_STATE_CLOSED,
        ]

    async def test_retract_from_closing_mid_reopens_from_rising(self, engine, state):
        """A retract detected after CLOSING_MID re-opens via RISING."""
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        engine.close()
        await engine.wait_for_status(DOOR_STATE_CLOSING_MID_OPEN, timeout=2.0)

        engine.trigger_sensor("inside")
        await engine.wait_for_status(DOOR_STATE_RISING, timeout=2.0)
        assert state.total_auto_retracts == 1
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

    async def test_outside_sensor_during_close_causes_retract(self, engine, state):
        """An outside trigger during closing activates it (inside cleared)."""
        state.inside_sensor_active = True  # must be displaced by the outside trigger
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        state.inside_sensor_active = False
        engine.close()
        assert state.door_status == DOOR_STATE_CLOSING_TOP_OPEN

        engine.trigger_sensor("outside")
        assert state.outside_sensor_active is True
        assert state.inside_sensor_active is False

        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
        assert state.total_auto_retracts == 1

    async def test_no_retract_when_autoretract_disabled(self, engine, state):
        """With autoretract off, a blocking sensor does not reverse the door."""
        state.autoretract = False
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        engine.close()

        engine.trigger_sensor("inside")
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)
        assert state.total_auto_retracts == 0


class TestTriggerSensorGuards:
    """trigger_sensor pre-condition checks."""

    async def test_power_off_ignores_trigger(self, engine, state):
        """No door motion when power is off."""
        state.power = False
        engine.trigger_sensor("inside")
        assert state.door_status == DOOR_STATE_CLOSED
        assert engine._task is None

    async def test_cmd_lockout_ignores_trigger(self, engine, state):
        """No door motion when command lockout is enabled."""
        state.cmd_lockout = True
        engine.trigger_sensor("inside")
        assert state.door_status == DOOR_STATE_CLOSED

    async def test_disabled_sensor_ignores_trigger(self, engine, state):
        """No door motion when the triggering sensor is disabled."""
        state.inside = False
        engine.trigger_sensor("inside")
        assert state.door_status == DOOR_STATE_CLOSED

    async def test_disabled_outside_sensor_ignores_trigger(self, engine, state):
        """No door motion when the outside sensor is disabled."""
        state.outside = False
        engine.trigger_sensor("outside")
        assert state.door_status == DOOR_STATE_CLOSED

    async def test_safety_lock_ignores_outside_trigger(self, engine, state):
        """Safety lock blocks the outside sensor only."""
        state.safety_lock = True
        engine.trigger_sensor("outside")
        assert state.door_status == DOOR_STATE_CLOSED

        engine.trigger_sensor("inside")
        assert state.door_status == DOOR_STATE_RISING

    async def test_out_of_schedule_trigger_is_ignored(self, engine, state):
        """With timers on, a trigger outside every schedule window is ignored."""
        from powerpetdoor.simulator import Schedule

        state.auto = True
        # A zero-length window never allows the sensor
        state.schedules[0] = Schedule(
            index=0,
            enabled=True,
            days_of_week=[1, 1, 1, 1, 1, 1, 1],
            inside=True,
            start_hour=0,
            start_min=0,
            end_hour=0,
            end_min=0,
        )
        engine.trigger_sensor("inside")
        assert state.door_status == DOOR_STATE_CLOSED
        assert engine._task is None

    async def test_trigger_notifies_and_opens_when_closed(self, engine, state):
        """A trigger from CLOSED emits a notification and opens the door."""
        notifications: list[tuple[str, str]] = []
        engine.notify_sensor = lambda sensor, st: notifications.append((sensor, st))

        engine.trigger_sensor("outside")
        assert state.door_status == DOOR_STATE_RISING
        assert notifications == [("outside", SENSOR_STATE_ON)]

    async def test_raising_notify_callback_does_not_block_door(self, engine, state, caplog):
        """A crashing notification callback is logged; the door still opens."""
        import logging

        def bad_notify(sensor: str, sensor_state: str) -> None:
            raise RuntimeError("notify down")

        engine.notify_sensor = bad_notify
        with caplog.at_level(logging.ERROR, logger="powerpetdoor.simulator.engine"):
            engine.trigger_sensor("inside")

        assert state.door_status == DOOR_STATE_RISING
        assert "sensor notification callback failed" in caplog.text


class TestActivateSensor:
    """activate_sensor toggle/duration semantics."""

    async def test_toggle_mode_flips_and_is_exclusive(self, engine, state):
        """Duration 0 toggles; activating one sensor clears the other."""
        state.power = False  # avoid door motion; only flags are under test
        engine.activate_sensor("inside", 0)
        assert state.inside_sensor_active is True

        engine.activate_sensor("outside", 0)
        assert state.outside_sensor_active is True
        assert state.inside_sensor_active is False

        engine.activate_sensor("outside", 0)
        assert state.outside_sensor_active is False

    async def test_duration_deactivates_after_expiry(self, engine, state):
        """A timed activation auto-deactivates after the duration."""
        state.power = False
        engine.activate_sensor("inside", 0.02)
        assert state.inside_sensor_active is True

        # The deactivation task is tracked; wait for it directly
        assert len(engine._aux_tasks) == 1
        await asyncio.gather(*engine._aux_tasks)
        assert state.inside_sensor_active is False

    async def test_outside_duration_deactivates_after_expiry(self, engine, state):
        """A timed outside activation auto-deactivates after the duration."""
        state.power = False
        engine.activate_sensor("outside", 0.02)
        assert state.outside_sensor_active is True

        assert len(engine._aux_tasks) == 1
        await asyncio.gather(*engine._aux_tasks)
        assert state.outside_sensor_active is False

    async def test_activation_with_door_open_does_not_retrigger(self, engine, state):
        """Activating a sensor while the door is up starts no new cycle."""
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        engine.activate_sensor("inside", 0)
        assert state.inside_sensor_active is True
        assert state.door_status == DOOR_STATE_KEEPUP

    async def test_reactivation_cancels_the_stale_deactivation_timer(self, engine, state):
        """Re-activating a sensor must not inherit the old expiry (L4)."""
        state.power = False
        engine.activate_sensor("inside", 0.02)
        stale = engine._sensor_timers["inside"]

        engine.activate_sensor("inside", 5.0)
        fresh = engine._sensor_timers["inside"]

        assert fresh is not stale
        await asyncio.gather(stale, return_exceptions=True)
        # A cancelled timer can never run its deactivation body.
        assert stale.cancelled()
        assert state.inside_sensor_active is True
        assert fresh.done() is False

    async def test_expiry_after_manual_clear_is_a_noop(self, engine, state):
        """The deactivation timer does nothing if the sensor was cleared."""
        state.power = False
        engine.activate_sensor("inside", 0.02)
        state.inside_sensor_active = False  # cleared out-of-band before expiry

        await asyncio.gather(*engine._aux_tasks)
        assert state.inside_sensor_active is False
        assert state.outside_sensor_active is False

    async def test_unknown_sensor_name_changes_nothing(self, engine, state):
        """An unrecognized sensor name leaves both flags untouched."""
        engine.activate_sensor("bogus", 0)
        assert state.inside_sensor_active is False
        assert state.outside_sensor_active is False
        assert state.door_status == DOOR_STATE_CLOSED

    async def test_activation_triggers_door_cycle_when_closed(self, engine, state):
        """Activating an enabled sensor with the door closed opens the door."""
        engine.activate_sensor("inside", 0)
        assert state.door_status == DOOR_STATE_RISING
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

    async def test_outside_activation_triggers_door_cycle_when_closed(self, engine, state):
        """An enabled, unlocked outside sensor also opens the closed door."""
        engine.activate_sensor("outside", 0)
        assert state.door_status == DOOR_STATE_RISING
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)


class TestSimulateObstruction:
    """simulate_obstruction state-dependent behavior."""

    async def test_obstruction_while_closed_sets_sensor(self, engine, state):
        """From CLOSED, the obstruction arms the inside sensor."""
        engine.simulate_obstruction()
        assert state.inside_sensor_active is True
        assert state.outside_sensor_active is False
        assert state.door_status == DOOR_STATE_CLOSED

    async def test_obstruction_while_opening_blocks_later_close(self, engine, state):
        """From RISING, the obstruction keeps the door open at the top."""
        engine.open()
        assert state.door_status == DOOR_STATE_RISING
        engine.simulate_obstruction()

        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)
        # Well past hold time the door is still blocked open
        await asyncio.sleep(float(state.hold_time) * 4)
        assert state.door_status == DOOR_STATE_HOLDING

        state.inside_sensor_active = False
        engine.notify_sensors_changed()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    async def test_obstruction_while_holding_blocks_close(self, engine, state):
        """From HOLDING, the obstruction prevents the close."""
        engine.open()
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)
        engine.simulate_obstruction()

        await asyncio.sleep(float(state.hold_time) * 4)
        assert state.door_status == DOOR_STATE_HOLDING

        state.inside_sensor_active = False
        engine.notify_sensors_changed()
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)

    async def test_obstruction_while_closing_triggers_retract(self, engine, state):
        """From CLOSING, the obstruction causes an auto-retract."""
        engine.open(hold=True)
        await engine.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        engine.close()
        assert state.door_status == DOOR_STATE_CLOSING_TOP_OPEN

        engine.simulate_obstruction()
        await engine.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)
        assert state.total_auto_retracts == 1
        await engine.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

    async def test_obstruction_with_unexpected_status_still_sets_sensor(
        self, engine, state, caplog
    ):
        """An unrecognized door status still arms the sensor (logged fallback)."""
        import logging

        state.door_status = "DOOR_UNKNOWN_STATE"
        with caplog.at_level(logging.INFO, logger="powerpetdoor.simulator.engine"):
            engine.simulate_obstruction()

        assert state.inside_sensor_active is True
        assert "door status: DOOR_UNKNOWN_STATE" in caplog.text
        state.door_status = DOOR_STATE_CLOSED  # restore for teardown


class TestLifecycle:
    """Engine task lifecycle (L14)."""

    async def test_stop_cancels_running_sequence(self, state):
        """stop() cancels and awaits the sequence task."""
        engine = DoorMotionEngine(state)
        engine.open()
        task = engine._task

        await engine.stop()
        assert task.cancelled()
        assert engine._task is None
        assert engine._retired == set()
        assert engine._aux_tasks == set()

    async def test_stop_cancels_pending_waiters(self, state):
        """stop() cancels outstanding wait_for_status waiters."""
        engine = DoorMotionEngine(state)
        waiter = asyncio.ensure_future(engine.wait_for_status(DOOR_STATE_KEEPUP))
        await asyncio.sleep(0)

        await engine.stop()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    async def test_cancel_nowait_cancels_without_awaiting(self, state):
        """cancel_nowait marks all tasks cancelled for sync contexts."""
        engine = DoorMotionEngine(state)
        engine.open()
        task = engine._task

        engine.cancel_nowait()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()

    async def test_stop_leaves_already_resolved_waiter_intact(self, state):
        """stop() does not cancel a waiter whose status already arrived."""
        engine = DoorMotionEngine(state)
        waiter = asyncio.ensure_future(engine.wait_for_status(DOOR_STATE_RISING))
        await asyncio.sleep(0)  # subscribe

        # Resolve the waiter without starting a sequence task, so it is
        # still registered (and already done) when stop() sweeps waiters.
        engine._set_status(DOOR_STATE_RISING)
        await engine.stop()

        assert await waiter == DOOR_STATE_RISING
