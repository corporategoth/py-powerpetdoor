# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Door motion engine for the Power Pet Door simulator.

This module contains the single door-motion state machine shared by every
path that moves the simulated door:

- the protocol path (client commands like OPEN/CLOSE), and
- the no-client path (CLI commands and scripts driving ``DoorSimulator``
  directly).

Both paths drive ONE :class:`DoorMotionEngine` instance operating on a
:class:`~powerpetdoor.simulator.state.DoorSimulatorState`, so door behavior
(hold-time extension, sensor re-trigger, sensor-during-close auto-retract)
is identical with and without a connected client.

The engine runs door sequences in a single owner task. Sequences chain via
loop continuation (e.g. a close that detects an obstruction continues
directly into a re-open) instead of tasks cancelling each other, and the
hold phase uses deadline-based waits rather than polling.

Deterministic hooks for tests and scripts:

- :meth:`DoorMotionEngine.wait_for_status` awaits a door-status transition.
- :meth:`DoorMotionEngine.add_status_listener` registers a synchronous
  callback fired on every status change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

from ..const import (
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    SENSOR_STATE_ON,
)
from ..i18n import t
from ..sanitize import MAX_LOGGED_LENGTH, sanitize_text
from .state import DoorSimulatorState

logger = logging.getLogger(__name__)

#: The two sensors a real door has, and the only names the engine acts on.
#:
#: Every gate in :meth:`DoorMotionEngine.trigger_sensor` is spelled
#: ``if sensor == "inside"`` / ``elif sensor == "outside"``, so a name that
#: is neither matched none of them and fell through to the open: a
#: one-character typo in a script synthesised a third sensor that ignored
#: the enable flags, the safety lock *and* the schedule, and the run still
#: reported PASSED. Callers that want a hard failure (the script DSL) check
#: against this first; the engine itself refuses the name the same way it
#: refuses a disabled sensor, so the documented programmatic API keeps its
#: "returns None" contract.
SENSOR_NAMES = ("inside", "outside")

#: Floor (seconds) on the blocked-sensor re-check wait. While a sensor
#: blocks closing the engine waits ``max(hold_time, MIN_BLOCKED_RECHECK)``;
#: this constant only stops a near-zero ``hold_time`` from turning that
#: wait into a busy loop. Sensor changes made through the simulator APIs
#: wake the hold loop immediately, so this is not a staleness bound: an
#: out-of-band state mutation (one that bypasses the simulator APIs) can go
#: unnoticed for up to ``hold_time``.
MIN_BLOCKED_RECHECK = 0.1

#: Deferred sequence intents. A re-entrant request records what was asked
#: for, not where the door was when it was asked - see
#: :meth:`DoorMotionEngine._defer_sequence`.
_INTENT_OPEN = "open"
_INTENT_CLOSE = "close"


class DoorMotionEngine:
    """The single door open/hold/close/retract state machine.

    Args:
        state: The simulator state the engine operates on.
        broadcast_status: Called after every door-status change to push the
            new status to connected clients (may be None).
        notify_sensor: Called as ``notify_sensor(sensor, sensor_state)``
            when a sensor trigger should emit a notification event to
            connected clients (may be None).
    """

    def __init__(
        self,
        state: DoorSimulatorState,
        broadcast_status: Callable[[], None] | None = None,
        notify_sensor: Callable[[str, str], None] | None = None,
    ):
        self.state = state
        self.broadcast_status = broadcast_status
        self.notify_sensor = notify_sensor
        self._task: asyncio.Task | None = None
        self._retired: set[asyncio.Task] = set()
        self._aux_tasks: set[asyncio.Task] = set()
        self._sensor_timers: dict[str, asyncio.Task] = {}
        #: Non-zero while status callbacks are running inside the owner
        #: task; sequence starts requested from there are deferred.
        self._dispatch_depth = 0
        self._pending_sequence: tuple[str, bool] = (_INTENT_CLOSE, False)
        self._restart_handle: asyncio.Handle | None = None
        self._hold_mode = False
        self._hold_deadline = 0.0
        self._wake = asyncio.Event()
        self._status_listeners: list[Callable[[str], None]] = []
        self._status_waiters: list[tuple[frozenset[str], asyncio.Future]] = []
        self._last_sensor_trigger = 0.0

    # =========================================================================
    # Status change hooks (deterministic testing / scripting)
    # =========================================================================

    def add_status_listener(self, callback: Callable[[str], None]) -> Callable[[], None]:
        """Register a callback fired on every door-status change.

        The callback receives the new status string. Returns an unsubscribe
        function. Callback exceptions are logged and isolated.

        A callback may command the door (``open()``/``close()``): the new
        sequence is deferred until the current callback dispatch unwinds,
        so it replaces the running sequence rather than racing it.
        """
        self._status_listeners.append(callback)

        def unsubscribe() -> None:
            try:
                self._status_listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    async def wait_for_status(
        self, status: str | Iterable[str], timeout: float | None = None
    ) -> str:
        """Wait until the door status equals (one of) ``status``.

        Returns immediately if the current status already matches;
        otherwise waits for a matching transition.

        Args:
            status: A door-status string, or an iterable of acceptable ones.
            timeout: Maximum seconds to wait (None = wait forever).

        Returns:
            The matching status string.

        Raises:
            TimeoutError: If the status is not reached within ``timeout``.
        """
        statuses = frozenset((status,)) if isinstance(status, str) else frozenset(status)
        current = self.state.door_status
        if current in statuses:
            return current

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._status_waiters.append((statuses, future))
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self._status_waiters = [(s, f) for s, f in self._status_waiters if f is not future]

    def _set_status(self, status: str) -> None:
        """Set the door status, broadcast it, and fire status hooks.

        The broadcast callback and status listeners run synchronously, and
        usually inside the sequence owner task. ``_dispatch_depth`` marks
        that window so a callback that commands the door (``open()``/
        ``close()``) defers its sequence start instead of spawning a second
        runner alongside the one dispatching to it (see
        :meth:`_defer_sequence`).
        """
        self.state.door_status = status
        self._dispatch_depth += 1
        try:
            if self.broadcast_status:
                try:
                    self.broadcast_status()
                except Exception:
                    logger.exception(
                        t(
                            "simulator.engine.simulator_door_status_broadcast_failed",
                            "Simulator: door status broadcast failed",
                        )
                    )
            for callback in list(self._status_listeners):
                try:
                    callback(status)
                except Exception:
                    logger.exception(
                        t(
                            "simulator.engine.simulator_door_status_listener_failed",
                            "Simulator: door status listener failed",
                        )
                    )
        finally:
            self._dispatch_depth -= 1
        for statuses, future in list(self._status_waiters):
            if status in statuses and not future.done():
                future.set_result(status)

    # =========================================================================
    # Door commands
    # =========================================================================

    def open(self, hold: bool = False) -> bool:
        """Start (or continue) opening the door.

        State-aware behavior:
        - Already open (HOLDING/KEEPUP): do nothing.
        - Already opening (RISING/SLOWING): do nothing.
        - Closing: reverse to the equivalent opening state and continue.
        - Closed: start the full opening sequence.

        Returns True if a new sequence was started.
        """
        current = self.state.door_status

        if current in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP):
            logger.debug(
                t(
                    "simulator.engine.simulator_open_command_ignored_already",
                    "Simulator: Open command ignored (already open)",
                )
            )
            return False
        if current in (DOOR_STATE_RISING, DOOR_STATE_SLOWING):
            logger.debug(
                t(
                    "simulator.engine.simulator_open_command_ignored_already_1",
                    "Simulator: Open command ignored (already opening)",
                )
            )
            return False

        if self._defer_sequence(_INTENT_OPEN, hold):
            return True

        # CLOSING (100%)          -> HOLDING / KEEPUP (100%)
        # CLOSING_TOP_OPEN (66%)  -> SLOWING (66%)
        # CLOSING_MID_OPEN (33%)  -> RISING (33%)
        # CLOSED                  -> RISING
        if current == DOOR_STATE_CLOSING:
            # The motor has started but the flap has not moved, so the door
            # is still fully open. Reversing puts it straight back into the
            # open state rather than re-running a rise it never undid.
            start_state = DOOR_STATE_KEEPUP if hold else DOOR_STATE_HOLDING
            logger.info(
                t(
                    "simulator.engine.simulator_reversing_close_start_still_open",
                    "Simulator: Reversing close before the flap moved, still open",
                )
            )
        elif current == DOOR_STATE_CLOSING_TOP_OPEN:
            start_state = DOOR_STATE_SLOWING
            logger.info(
                t(
                    "simulator.engine.simulator_reversing_close_top_continuing",
                    "Simulator: Reversing close at top, continuing to open",
                )
            )
        elif current == DOOR_STATE_CLOSING_MID_OPEN:
            start_state = DOOR_STATE_RISING
            logger.info(
                t(
                    "simulator.engine.simulator_reversing_close_mid_continuing",
                    "Simulator: Reversing close at mid, continuing to open",
                )
            )
        else:
            start_state = DOOR_STATE_RISING

        self._replace_sequence(start_state, hold)
        return True

    def close(self) -> bool:
        """Start (or continue) closing the door.

        State-aware behavior:
        - Already closed: do nothing.
        - Already closing: do nothing.
        - Opening: reverse to the equivalent closing state and continue.
        - Open (HOLDING/KEEPUP): start the full closing sequence.

        Returns True if a new sequence was started.
        """
        current = self.state.door_status

        if current == DOOR_STATE_CLOSED:
            logger.debug(
                t(
                    "simulator.engine.simulator_close_command_ignored_already",
                    "Simulator: Close command ignored (already closed)",
                )
            )
            return False
        if current in (
            DOOR_STATE_CLOSING,
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
        ):
            logger.debug(
                t(
                    "simulator.engine.simulator_close_command_ignored_already_1",
                    "Simulator: Close command ignored (already closing)",
                )
            )
            return False

        if self._defer_sequence(_INTENT_CLOSE, False):
            return True

        # RISING (33%) -> CLOSING_MID_OPEN (33%)
        # SLOWING (66%) -> CLOSING_TOP_OPEN (66%)
        # HOLDING/KEEPUP -> CLOSING_TOP_OPEN
        if current == DOOR_STATE_RISING:
            start_state = DOOR_STATE_CLOSING_MID_OPEN
            logger.info(
                t(
                    "simulator.engine.simulator_reversing_open_rising_closing",
                    "Simulator: Reversing open at rising, closing from mid",
                )
            )
        elif current == DOOR_STATE_SLOWING:
            start_state = DOOR_STATE_CLOSING_TOP_OPEN
            logger.info(
                t(
                    "simulator.engine.simulator_reversing_open_slowing_closing",
                    "Simulator: Reversing open at slowing, closing from top",
                )
            )
        else:
            # A close from open (HOLDING or KEEPUP) starts at the top of the
            # closing sequence, which begins with the motor starting.
            start_state = DOOR_STATE_CLOSING

        self._replace_sequence(start_state, False)
        return True

    def _defer_sequence(self, intent: str, hold: bool) -> bool:
        """Record a re-entrant open/close request for later application.

        A request made from a status listener or the broadcast callback
        arrives *inside* the owner task's call stack, where the owner is
        still mid-loop and cannot be cancelled. Such re-entrant requests are
        recorded and applied from a ``call_soon`` callback instead, once the
        owner task is suspended again — so exactly one ``_run`` task can ever
        exist. Repeat requests within one dispatch coalesce (last one wins).

        What is recorded is the *intent* (open/close), never a resolved
        start state: the owner task does not always suspend between two
        ``_set_status`` calls (``_hold_open`` returns without awaiting when
        ``hold_time`` is ~0), so a state resolved at request time can be
        stale by the time it is applied, and replaying it re-broadcasts a
        status the door has already moved past.

        Returns:
            True if the request was deferred (the caller must not act now).
        """
        if not self._dispatch_depth:
            return False
        self._pending_sequence = (intent, hold)
        if self._restart_handle is None:
            self._restart_handle = asyncio.get_running_loop().call_soon(
                self._start_pending_sequence
            )
        return True

    def _start_pending_sequence(self) -> None:
        """Apply a sequence start deferred out of a status-callback stack.

        Re-invokes :meth:`open`/:meth:`close`, which re-derive the start
        state from the status the door is in *now* and correctly no-op when
        it already got there ("already closing").
        """
        self._restart_handle = None
        intent, hold = self._pending_sequence
        if intent == _INTENT_OPEN:
            self.open(hold=hold)
        else:
            self.close()

    def _replace_sequence(self, start_state: str, hold: bool) -> None:
        """Replace the current sequence task with a new one from start_state.

        Only ever called from outside the owner task (``open``/``close``
        defer re-entrant calls), so cancelling the current task is safe:
        it is suspended at an await point and can no longer change status.
        It is awaited in :meth:`stop`.
        """
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            self._retired.add(task)
            task.add_done_callback(self._retired.discard)
        self._hold_mode = hold
        self._set_status(start_state)
        self._task = asyncio.create_task(self._run())

    # =========================================================================
    # Sensor interaction
    # =========================================================================

    @staticmethod
    def _known_sensor(sensor: str) -> bool:
        """Whether ``sensor`` is a sensor this door actually has.

        Refuses in the same shape as the other gates - log and do nothing -
        so ``DoorSimulator.trigger_sensor()`` / ``activate_sensor()``, both
        documented programmatic APIs, keep returning None.
        """
        if sensor in SENSOR_NAMES:
            return True
        logger.warning(
            t(
                "simulator.engine.simulator_ignoring_unknown_sensor_use",
                "Simulator: Ignoring unknown sensor %s (use: %s)",
            ),
            sanitize_text(sensor, MAX_LOGGED_LENGTH),
            ", ".join(SENSOR_NAMES),
        )
        return False

    def sensor_open_block_reason(self, sensor: str) -> str | None:
        """Why ``sensor`` may not open the door right now, or ``None``.

        **One** predicate for both sensor entry points: power, command
        lockout, per-sensor enable, safety lock and the schedule. Do not
        re-implement it inline in :meth:`trigger_sensor` or
        :meth:`activate_sensor`: a weaker copy is reachable from both
        shipped front ends - the ``inside``/``outside`` CLI commands
        (``commands/door.py``) and the ``inside``/``outside``/``pet_presence``
        script actions (``scripting.py``) - so a script step
        ``- action: inside`` opens the door while every schedule window is
        closed.

        ``docs/operation.md`` ("Schedule and Sensor Interaction") settles the
        schedule half outright: *"Outside scheduled windows, sensor triggers
        are ignored"*. The command-lockout half is not settled by
        ``docs/operation.md``, which describes command lockout only in terms
        of blocking the door from *closing*; the two are made consistent on
        ``trigger_sensor``'s answer, which is the behaviour
        ``scripts/power_lockout_test.yaml`` and
        ``test_cmd_lockout_ignores_trigger`` assert.

        Note the scope: this gates *opening the door*, not the sensor-active
        flag. A pet standing in the doorway is a physical fact, so
        :meth:`activate_sensor` still records it and
        :meth:`DoorSimulatorState.is_sensor_blocking_close` still consults
        it under its own (documented, different) rules.

        Args:
            sensor: "inside" or "outside" (already validated by
                :meth:`_known_sensor`).

        Returns:
            A short operator-facing reason, or None when the sensor may open
            the door.
        """
        state = self.state
        if not state.power:
            return "power OFF"
        if state.cmd_lockout:
            return "command lockout"
        if sensor == "inside":
            if not state.inside:
                return "disabled"
        else:
            # `else`, not `elif sensor == "outside"`: _known_sensor has
            # already established the name is one of SENSOR_NAMES.
            if not state.outside:
                return "disabled"
            if state.safety_lock:
                return "safety lock"
        if not state.is_sensor_allowed_by_schedule(sensor):
            return "outside schedule"
        return None

    def trigger_sensor(self, sensor: str) -> None:
        """Simulate a sensor trigger (pet walking through).

        Args:
            sensor: "inside" or "outside"
        """
        if not self._known_sensor(sensor):
            return

        now = time.monotonic()
        state = self.state

        blocked = self.sensor_open_block_reason(sensor)
        if blocked is not None:
            logger.info(
                t("simulator.engine.simulator_sensor_ignored", "Simulator: %s sensor ignored (%s)"),
                sensor.capitalize(),
                blocked,
            )
            return

        # If door is already open/holding, re-trigger extends hold time
        if state.door_status in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP):
            if now - self._last_sensor_trigger > state.timing.sensor_retrigger_window:
                logger.info(
                    t(
                        "simulator.engine.simulator_sensor_re_triggered_extending",
                        "Simulator: %s sensor re-triggered, extending hold",
                    ),
                    sensor.capitalize(),
                )
                self.extend_hold()
                self._last_sensor_trigger = now
                self._notify_sensor(sensor, SENSOR_STATE_ON)
            return

        # If door is closing, activate the sensor to trigger auto-retract.
        # DOOR_CLOSING counts: the motor has started, so a pet arriving now
        # must retract the door rather than fall through to the "door is
        # closed, open it" path below.
        if state.door_status in (
            DOOR_STATE_CLOSING,
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
        ):
            logger.info(
                t(
                    "simulator.engine.simulator_sensor_during_close_activating",
                    "Simulator: %s sensor during close, activating sensor",
                ),
                sensor.capitalize(),
            )
            # Activate the appropriate sensor (mutually exclusive)
            if sensor == "inside":
                state.inside_sensor_active = True
                state.outside_sensor_active = False
            else:
                state.outside_sensor_active = True
                state.inside_sensor_active = False
            self.notify_sensors_changed()
            self._last_sensor_trigger = now
            self._notify_sensor(sensor, SENSOR_STATE_ON)
            return

        # Door is closed, trigger open
        logger.info(
            t(
                "simulator.engine.simulator_sensor_triggered_opening_door",
                "Simulator: %s sensor triggered, opening door",
            ),
            sensor.capitalize(),
        )
        self._last_sensor_trigger = now
        self._notify_sensor(sensor, SENSOR_STATE_ON)
        self.open(hold=False)

    def activate_sensor(self, sensor: str, duration: float = 0.5) -> None:
        """Activate sensor detection with optional duration.

        Args:
            sensor: "inside" or "outside"
            duration: How long sensor stays active in seconds.
                     0 = toggle mode (on indefinitely if off, off if on)
                     >0 = active for that duration then auto-deactivates

        This is mutually exclusive - activating one sensor clears the other.
        If door is closed, triggers a door cycle (respecting sensor enable
        and safety settings).
        """
        if not self._known_sensor(sensor):
            return

        state = self.state

        # Re-activating a sensor restarts its window: drop any pending
        # deactivation timer so a stale one cannot cut the new duration
        # short.
        self._cancel_sensor_timer(sensor)

        # Mutually exclusive - clear the other sensor
        if sensor == "inside":
            state.outside_sensor_active = False
            if duration == 0:
                state.inside_sensor_active = not state.inside_sensor_active
                logger.info(
                    t(
                        "simulator.engine.simulator_inside_sensor_toggle",
                        "Simulator: Inside sensor %s (toggle)",
                    ),
                    "activated" if state.inside_sensor_active else "deactivated",
                )
            else:
                state.inside_sensor_active = True
                logger.info(
                    t(
                        "simulator.engine.simulator_inside_sensor_activated_s",
                        "Simulator: Inside sensor activated for %ss",
                    ),
                    duration,
                )
                self._arm_sensor_timer(sensor, duration)
        else:
            # `else`, not `elif sensor == "outside"`: the guard above has
            # already established that the name is one of SENSOR_NAMES, so
            # an `elif` would leave a destination nothing can reach.
            state.inside_sensor_active = False
            if duration == 0:
                state.outside_sensor_active = not state.outside_sensor_active
                logger.info(
                    t(
                        "simulator.engine.simulator_outside_sensor_toggle",
                        "Simulator: Outside sensor %s (toggle)",
                    ),
                    "activated" if state.outside_sensor_active else "deactivated",
                )
            else:
                state.outside_sensor_active = True
                logger.info(
                    t(
                        "simulator.engine.simulator_outside_sensor_activated_s",
                        "Simulator: Outside sensor activated for %ss",
                    ),
                    duration,
                )
                self._arm_sensor_timer(sensor, duration)
        self.notify_sensors_changed()

        # If door is closed and sensor should trigger, open the door.
        # `active` is the second operand and it is decisive: toggling a
        # sensor *off* (duration 0) must not open the door.
        active = state.inside_sensor_active if sensor == "inside" else state.outside_sensor_active
        if state.door_status == DOOR_STATE_CLOSED and active:
            # The same gate `trigger_sensor` applies - one predicate, so the
            # two entry points cannot disagree about power, command lockout,
            # the sensor enables or the schedule window.
            blocked = self.sensor_open_block_reason(sensor)
            if blocked is None:
                logger.info(
                    t(
                        "simulator.engine.simulator_sensor_triggering_door_cycle",
                        "Simulator: %s sensor triggering door cycle",
                    ),
                    sensor.capitalize(),
                )
                self.open(hold=False)
            else:
                logger.info(
                    t(
                        "simulator.engine.simulator_sensor_ignored",
                        "Simulator: %s sensor ignored (%s)",
                    ),
                    sensor.capitalize(),
                    blocked,
                )

    def _cancel_sensor_timer(self, sensor: str) -> None:
        """Cancel a pending auto-deactivation timer for ``sensor``, if any."""
        task = self._sensor_timers.pop(sensor, None)
        if task is not None:
            task.cancel()

    def _arm_sensor_timer(self, sensor: str, duration: float) -> None:
        """Start the auto-deactivation timer for ``sensor``."""
        self._sensor_timers[sensor] = self._track_aux(
            self._deactivate_sensor_after(sensor, duration)
        )

    async def _deactivate_sensor_after(self, sensor: str, duration: float) -> None:
        """Deactivate sensor after specified duration."""
        await asyncio.sleep(duration)
        self._sensor_timers.pop(sensor, None)
        state = self.state
        if sensor == "inside" and state.inside_sensor_active:
            state.inside_sensor_active = False
            logger.info(
                t(
                    "simulator.engine.simulator_inside_sensor_deactivated_duration",
                    "Simulator: Inside sensor deactivated (duration expired)",
                )
            )
            self.notify_sensors_changed()
        elif sensor == "outside" and state.outside_sensor_active:
            state.outside_sensor_active = False
            logger.info(
                t(
                    "simulator.engine.simulator_outside_sensor_deactivated_duration",
                    "Simulator: Outside sensor deactivated (duration expired)",
                )
            )
            self.notify_sensors_changed()

    def simulate_obstruction(self) -> None:
        """Simulate obstruction detection (inside sensor active indefinitely).

        Works in any door state:
        - Closed/opening: Will prevent closing once door reaches HOLDING
        - Holding: Prevents closing
        - Closing: Triggers auto-retract if enabled
        """
        state = self.state
        # Set inside sensor active (obstruction = something in the doorway)
        state.inside_sensor_active = True
        state.outside_sensor_active = False
        self.notify_sensors_changed()

        if state.door_status == DOOR_STATE_CLOSED:
            logger.info(
                t(
                    "simulator.engine.simulator_obstruction_set_will_block",
                    "Simulator: Obstruction set (will block close when door opens)",
                )
            )
        elif state.door_status in (DOOR_STATE_RISING, DOOR_STATE_SLOWING):
            logger.info(
                t(
                    "simulator.engine.simulator_obstruction_set_will_block_1",
                    "Simulator: Obstruction set (will block close when door reaches top)",
                )
            )
        elif state.door_status in (DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP):
            logger.info(
                t(
                    "simulator.engine.simulator_obstruction_set_blocking_close",
                    "Simulator: Obstruction set (blocking close)",
                )
            )
        elif state.door_status in (
            DOOR_STATE_CLOSING,
            DOOR_STATE_CLOSING_TOP_OPEN,
            DOOR_STATE_CLOSING_MID_OPEN,
        ):
            logger.info(
                t(
                    "simulator.engine.simulator_obstruction_during_close_will",
                    "Simulator: Obstruction during close (will trigger retract)",
                )
            )
        else:
            logger.info(
                t(
                    "simulator.engine.simulator_obstruction_set_door_status",
                    "Simulator: Obstruction set (door status: %s)",
                ),
                state.door_status,
            )

    def extend_hold(self) -> None:
        """Reset the hold-open timer to a full hold_time from now."""
        self._hold_deadline = asyncio.get_running_loop().time() + float(self.state.hold_time)
        self._wake.set()

    def notify_sensors_changed(self) -> None:
        """Wake the hold loop so it re-evaluates sensor blocking state.

        Call after any change to the sensor-active flags (or settings that
        affect :meth:`DoorSimulatorState.is_sensor_blocking_close`).
        """
        self._wake.set()

    def _notify_sensor(self, sensor: str, sensor_state: str) -> None:
        """Emit a sensor notification event via the configured callback."""
        if self.notify_sensor:
            try:
                self.notify_sensor(sensor, sensor_state)
            except Exception:
                logger.exception(
                    t(
                        "simulator.engine.simulator_sensor_notification_callback_failed",
                        "Simulator: sensor notification callback failed",
                    )
                )

    # =========================================================================
    # Sequence runner (single owner task)
    # =========================================================================

    async def _run(self) -> None:
        """Advance the door from its current status to a terminal state.

        Runs as the single sequence owner task. Auto-retract chains back
        into the opening phases via loop continuation - no task ever
        cancels or awaits itself.
        """
        timing = self.state.timing
        state = self.state
        while True:
            status = state.door_status
            if status == DOOR_STATE_RISING:
                await asyncio.sleep(timing.rise_time)
                self._set_status(DOOR_STATE_SLOWING)
            elif status == DOOR_STATE_SLOWING:
                await asyncio.sleep(timing.slowing_time)
                if self._hold_mode:
                    self._set_status(DOOR_STATE_KEEPUP)
                    return
                self._set_status(DOOR_STATE_HOLDING)
            elif status == DOOR_STATE_HOLDING:
                await self._hold_open()
                self._set_status(DOOR_STATE_CLOSING)
            elif status == DOOR_STATE_CLOSING:
                # The motor has started but the flap has not moved yet.
                # Measured on a real door: HOLDING -> DOOR_CLOSING ->
                # DOOR_CLOSING_TOP_OPEN, about 180ms apart. Omitting it here
                # meant no test could catch the library not knowing the
                # state, which is exactly what happened.
                await asyncio.sleep(timing.closing_start_time)
                # A pet that steps under the door as the motor starts must
                # still trigger a retract. The flap has not moved, so the
                # door goes back to holding rather than re-opening.
                if self._auto_retract_if_blocked(DOOR_STATE_HOLDING):
                    continue
                self._set_status(DOOR_STATE_CLOSING_TOP_OPEN)
            elif status == DOOR_STATE_CLOSING_TOP_OPEN:
                await asyncio.sleep(timing.closing_top_time)
                if self._auto_retract_if_blocked(DOOR_STATE_SLOWING):
                    continue
                self._set_status(DOOR_STATE_CLOSING_MID_OPEN)
            elif status == DOOR_STATE_CLOSING_MID_OPEN:
                await asyncio.sleep(timing.closing_mid_time)
                if self._auto_retract_if_blocked(DOOR_STATE_RISING):
                    continue
                self._set_status(DOOR_STATE_CLOSED)
                state.total_open_cycles += 1
                return
            else:
                # CLOSED or KEEPUP: nothing further to run
                return

    async def _hold_open(self) -> None:
        """Hold the door open until hold_time elapses and no sensor blocks.

        Deadline-based: sleeps exactly until the hold deadline (no polling
        drift). :meth:`extend_hold` and :meth:`notify_sensors_changed` wake
        the wait early so re-triggers and sensor changes take effect
        immediately.
        """
        loop = asyncio.get_running_loop()
        self._hold_deadline = loop.time() + float(self.state.hold_time)
        while True:
            if self.state.is_sensor_blocking_close():
                # Blocked: wait for a sensor change, then grant a full
                # hold_time from when the block clears. The wait is one
                # hold_time long (MIN_BLOCKED_RECHECK is only a floor that
                # keeps a near-zero hold_time from spinning), so an
                # out-of-band mutation is noticed within one hold_time.
                logger.debug(
                    t(
                        "simulator.engine.simulator_sensor_blocking_close_resetting",
                        "Simulator: Sensor blocking close, resetting hold timer",
                    )
                )
                await self._wait_for_wake(max(float(self.state.hold_time), MIN_BLOCKED_RECHECK))
                self._hold_deadline = loop.time() + float(self.state.hold_time)
                continue
            remaining = self._hold_deadline - loop.time()
            if remaining <= 0:
                return
            await self._wait_for_wake(remaining)

    async def _wait_for_wake(self, timeout: float) -> None:
        """Wait until woken or the timeout elapses, then clear the wake flag."""
        try:
            async with asyncio.timeout(timeout):
                await self._wake.wait()
        except TimeoutError:
            pass
        self._wake.clear()

    def _auto_retract_if_blocked(self, reopen_state: str) -> bool:
        """Auto-retract during closing if a sensor blocks and it is enabled.

        Returns True if the door reversed into ``reopen_state`` (the caller
        continues the sequence loop from there).
        """
        state = self.state
        if state.is_sensor_blocking_close() and state.autoretract:
            logger.info(
                t(
                    "simulator.engine.simulator_sensor_blocking_close_auto",
                    "Simulator: Sensor blocking close! Auto-retracting...",
                )
            )
            # Clear the active sensors
            state.inside_sensor_active = False
            state.outside_sensor_active = False
            state.total_auto_retracts += 1
            self._hold_mode = False
            self._set_status(reopen_state)
            return True
        return False

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def _track_aux(self, coro) -> asyncio.Task:
        """Track a helper task so stop() can cancel and await it."""
        task = asyncio.create_task(coro)
        self._aux_tasks.add(task)
        task.add_done_callback(self._aux_tasks.discard)
        return task

    def _pending_tasks(self) -> list[asyncio.Task]:
        """All engine-owned tasks that have not completed yet."""
        return [
            task
            for task in (self._task, *self._retired, *self._aux_tasks)
            if task is not None and not task.done()
        ]

    def _cancel_deferred_restart(self) -> None:
        """Drop a sequence start deferred by a re-entrant status callback."""
        handle = self._restart_handle
        self._restart_handle = None
        if handle is not None:
            handle.cancel()

    def cancel_nowait(self) -> None:
        """Cancel all engine tasks without awaiting them.

        For synchronous contexts (e.g. ``connection_lost``); prefer
        :meth:`stop` wherever awaiting is possible.
        """
        self._cancel_deferred_restart()
        for task in self._pending_tasks():
            task.cancel()

    async def stop(self) -> None:
        """Cancel and await all engine tasks and fail pending waiters."""
        self._cancel_deferred_restart()
        tasks = self._pending_tasks()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._retired.clear()
        self._aux_tasks.clear()
        self._sensor_timers.clear()
        for _, future in self._status_waiters:
            if not future.done():
                future.cancel()
        self._status_waiters.clear()
