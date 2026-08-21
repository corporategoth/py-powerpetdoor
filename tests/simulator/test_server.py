# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for simulator server module (server.py)."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import MagicMock

import pytest

from powerpetdoor.const import (
    CMD_DELETE_SCHEDULE,
    CMD_DISABLE_AUTO,
    CMD_DISABLE_AUTORETRACT,
    CMD_DISABLE_CMD_LOCKOUT,
    CMD_DISABLE_INSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_ENABLE_AUTO,
    CMD_ENABLE_AUTORETRACT,
    CMD_ENABLE_CMD_LOCKOUT,
    CMD_ENABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SETTINGS,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_TIMEZONE,
    DOOR_STATE_CLOSED,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    DOOR_STATUS,
    DOOR_TO_PHONE,
    FIELD_AC_PRESENT,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD,
    FIELD_CMD_LOCKOUT,
    FIELD_DIRECTION,
    FIELD_DOOR_STATUS,
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
    FIELD_FWINFO,
    FIELD_HOLD_TIME,
    FIELD_HW_REVISION,
    FIELD_HW_VERSION,
    FIELD_INDEX,
    FIELD_INSIDE,
    FIELD_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SETTINGS,
    FIELD_SUCCESS,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    NOTIFY_LOW_BATTERY,
    NOTIFY_SENSOR_INDOOR,
    SENSOR_STATE_ON,
    SUCCESS_TRUE,
)
from powerpetdoor.simulator import (
    BatteryConfig,
    DoorSimulator,
    DoorSimulatorProtocol,
    DoorSimulatorState,
    DoorTimingConfig,
    Schedule,
)
from powerpetdoor.simulator import server as server_module

FULL_CYCLE_SEQUENCE = [
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    DOOR_STATE_HOLDING,
    DOOR_STATE_CLOSING_TOP_OPEN,
    DOOR_STATE_CLOSING_MID_OPEN,
    DOOR_STATE_CLOSED,
]

OK = {FIELD_SUCCESS: SUCCESS_TRUE, FIELD_DIRECTION: DOOR_TO_PHONE}

# ============================================================================
# Test Fixtures / Helpers
# ============================================================================


@pytest.fixture
def timing_config():
    """Create a fast timing config for tests."""
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(timing_config):
    """Create and start a simulator."""
    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
def recorder(simulator):
    """Attach a recording client protocol; returns its sent-message list."""
    return attach_recorder(simulator)


def attach_recorder(sim: DoorSimulator) -> list[dict]:
    """Register a protocol with a mock transport; return decoded messages."""
    proto = DoorSimulatorProtocol(sim.state, engine=sim.engine)
    transport = MagicMock()
    transport.get_extra_info.return_value = ("127.0.0.1", 12345)
    proto.connection_made(transport)
    sim.protocols.append(proto)

    messages: list[dict] = []

    def record(data: bytes):
        messages.append(json.loads(data.decode("ascii")))

    transport.write.side_effect = record
    return messages


async def wait_for_protocols(sim: DoorSimulator, count: int, timeout: float = 2.0) -> None:
    """Yield to the loop until the server tracks exactly ``count`` clients."""
    async with asyncio.timeout(timeout):
        while len(sim.protocols) != count:
            await asyncio.sleep(0)


async def connect_client(sim: DoorSimulator):
    """Open a TCP client and wait until the server registers it."""
    port = sim.server.sockets[0].getsockname()[1]
    known = len(sim.protocols)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await wait_for_protocols(sim, known + 1)
    return reader, writer


def _parse_json_stream(data: str) -> list[dict]:
    """Parse back-to-back JSON objects from a captured stream."""
    decoder = json.JSONDecoder()
    messages = []
    pos = 0
    while pos < len(data):
        if data[pos].isspace():
            pos += 1
            continue
        msg, end = decoder.raw_decode(data, pos)
        messages.append(msg)
        pos = end
    return messages


async def read_messages_until(reader, predicate, timeout: float = 5.0) -> list[dict]:
    """Read the stream until ``predicate(messages)`` holds; return messages."""
    text = ""
    async with asyncio.timeout(timeout):
        while True:
            messages = _parse_json_stream(text)
            if predicate(messages):
                return messages
            chunk = await reader.read(65536)
            if not chunk:
                return _parse_json_stream(text)
            text += chunk.decode("ascii")


# ============================================================================
# DoorSimulator Server Tests
# ============================================================================


class TestDoorSimulator:
    """Tests for DoorSimulator server."""

    async def test_start_stop(self, timing_config):
        """Should start and stop cleanly."""
        state = DoorSimulatorState(timing=timing_config)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        assert sim.server is not None
        await sim.stop()

    async def test_stop_without_start(self, timing_config):
        """stop() on a never-started simulator is a clean no-op."""
        sim = DoorSimulator(port=0, state=DoorSimulatorState(timing=timing_config))
        await sim.stop()
        assert sim.server is None
        assert sim._battery_task is None

    async def test_listens_on_port(self, simulator):
        """Should listen on configured port."""
        port = simulator.server.sockets[0].getsockname()[1]
        assert port > 0

    async def test_open_door(self, simulator):
        """open_door with hold=True should reach and stay in KEEPUP."""
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        await simulator.open_door(hold=True)
        result = await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        assert result == DOOR_STATE_KEEPUP

    async def test_close_door(self, simulator):
        """close_door should fully close the door."""
        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        await simulator.close_door()
        result = await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)
        assert result == DOOR_STATE_CLOSED

    async def test_trigger_sensor_opens_door(self, simulator):
        """trigger_sensor should start opening the door immediately."""
        assert simulator.state.door_status == DOOR_STATE_CLOSED
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_RISING

    async def test_trigger_sensor_ignored_when_power_off(self, simulator):
        """trigger_sensor should be ignored when power is off."""
        simulator.set_power(False)
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    async def test_trigger_sensor_ignored_when_disabled(self, simulator):
        """trigger_sensor should be ignored when sensor is disabled."""
        simulator.state.inside = False
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED

    async def test_outside_sensor_ignored_with_safety_lock(self, simulator):
        """Outside sensor should be ignored when safety lock is enabled."""
        simulator.state.safety_lock = True
        simulator.trigger_sensor("outside")
        assert simulator.state.door_status == DOOR_STATE_CLOSED

        # Inside sensor should still work
        simulator.trigger_sensor("inside")
        assert simulator.state.door_status == DOOR_STATE_RISING

    def test_set_battery(self, simulator):
        """set_battery should update battery percent."""
        simulator.set_battery(50)
        assert simulator.state.battery_percent == 50

    def test_set_battery_clamps_values(self, simulator):
        """set_battery should clamp to 0-100."""
        simulator.set_battery(150)
        assert simulator.state.battery_percent == 100

        simulator.set_battery(-10)
        assert simulator.state.battery_percent == 0

    def test_set_power(self, simulator):
        """set_power should update power state."""
        simulator.set_power(False)
        assert simulator.state.power is False

        simulator.set_power(True)
        assert simulator.state.power is True

    def test_add_schedule(self, simulator):
        """add_schedule should add to state."""
        schedule = Schedule(index=5, enabled=True)
        simulator.add_schedule(schedule)
        assert 5 in simulator.state.schedules
        assert simulator.state.schedules[5] is schedule

    def test_remove_schedule(self, simulator):
        """remove_schedule should remove from state."""
        schedule = Schedule(index=3)
        simulator.add_schedule(schedule)
        assert 3 in simulator.state.schedules

        simulator.remove_schedule(3)
        assert 3 not in simulator.state.schedules

    def test_remove_schedule_unknown_index_is_noop(self, simulator, recorder):
        """Removing a nonexistent schedule neither errors nor broadcasts."""
        simulator.remove_schedule(42)
        assert recorder == []

    def test_simulate_obstruction_delegates_to_engine(self, simulator):
        """simulate_obstruction activates the inside sensor indefinitely."""
        simulator.simulate_obstruction()
        assert simulator.state.inside_sensor_active is True
        assert simulator.state.outside_sensor_active is False

    def test_set_pet_in_doorway(self, simulator):
        """set_pet_in_doorway should update inside_sensor_active state."""
        simulator.set_pet_in_doorway(True)
        assert simulator.state.inside_sensor_active is True

        simulator.set_pet_in_doorway(False)
        assert simulator.state.inside_sensor_active is False

    def test_set_pet_in_doorway_clears_outside_sensor(self, simulator):
        """Pet presence is mutually exclusive with the outside sensor."""
        simulator.state.outside_sensor_active = True
        simulator.set_pet_in_doorway(True)
        assert simulator.state.inside_sensor_active is True
        assert simulator.state.outside_sensor_active is False


# ============================================================================
# Door Operation Sequence Tests
# ============================================================================


class TestDoorOperationSequences:
    """Tests for complete door operation sequences."""

    async def test_full_open_close_cycle(self, simulator):
        """A sensor trigger runs the exact full open/close sequence."""
        seen: list[str] = []
        unsubscribe = simulator.add_status_listener(seen.append)

        simulator.trigger_sensor("inside")
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)
        unsubscribe()

        assert seen == FULL_CYCLE_SEQUENCE
        assert simulator.state.total_open_cycles == 1

    async def test_sensor_active_state(self, simulator):
        """Sensor active flags should be settable and affect is_sensor_blocking_close."""
        # Verify initial state
        assert simulator.state.inside_sensor_active is False
        assert simulator.state.outside_sensor_active is False
        assert simulator.state.is_sensor_blocking_close() is False

        # Set inside sensor active (with sensor enabled)
        simulator.state.inside_sensor_active = True
        assert simulator.state.is_sensor_blocking_close() is True

        # Clear it
        simulator.state.inside_sensor_active = False
        assert simulator.state.is_sensor_blocking_close() is False

    async def test_open_and_hold_keeps_door_open(self, simulator):
        """open_door with hold=True should keep door open indefinitely."""
        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        # KEEPUP is terminal: the sequence task has finished and the door stays
        await asyncio.gather(simulator.engine._task, return_exceptions=True)
        assert simulator.state.door_status == DOOR_STATE_KEEPUP

    async def test_wait_for_status_timeout(self, simulator):
        """wait_for_status raises TimeoutError when never reached."""
        with pytest.raises(TimeoutError):
            await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=0.05)


# ============================================================================
# Engine Path Parity Tests (M12: one engine, identical behavior)
# ============================================================================


class TestEnginePathParity:
    """Door behavior must be identical with and without a connected client."""

    async def _run_retract_cycle(self, simulator) -> list[str]:
        """Open-and-hold, close, obstruct during close, observe full sequence."""
        seen: list[str] = []
        unsubscribe = simulator.add_status_listener(seen.append)

        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)
        await simulator.close_door()
        # Pet enters the doorway while the door is closing
        simulator.trigger_sensor("inside")
        await simulator.wait_for_status(DOOR_STATE_CLOSED, timeout=5.0)

        unsubscribe()
        return seen

    async def test_retract_cycle_identical_with_and_without_client(self, timing_config):
        """The full transition sequence matches exactly on both paths."""
        state = DoorSimulatorState(timing=timing_config, hold_time=0.1)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        try:
            # Path 1: no client connected
            no_client_seen = await self._run_retract_cycle(sim)
            assert sim.state.total_auto_retracts == 1

            # Path 2: a client is connected and watches the broadcasts
            reader, writer = await connect_client(sim)
            try:
                with_client_seen = await self._run_retract_cycle(sim)
                assert sim.state.total_auto_retracts == 2

                assert with_client_seen == no_client_seen

                # The connected client observed the same status sequence
                messages = await read_messages_until(
                    reader,
                    lambda msgs: (
                        len([m for m in msgs if FIELD_DOOR_STATUS in m]) >= len(with_client_seen)
                    ),
                )
                broadcast_statuses = [
                    msg[FIELD_DOOR_STATUS] for msg in messages if FIELD_DOOR_STATUS in msg
                ]
                assert broadcast_statuses == with_client_seen
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await sim.stop()

    async def test_hold_extension_identical_with_and_without_client(self, timing_config):
        """Sensor re-trigger extends the hold deadline on both paths."""
        state = DoorSimulatorState(timing=timing_config, hold_time=10.0)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        loop = asyncio.get_running_loop()
        try:
            deadlines = []
            for connect in (False, True):
                if connect:
                    reader, writer = await connect_client(sim)

                simulator_engine = sim.engine
                sim.trigger_sensor("inside")
                await sim.wait_for_status(DOOR_STATE_HOLDING, timeout=2.0)

                simulator_engine._last_sensor_trigger = 0.0  # outside retrigger window
                sim.trigger_sensor("inside")
                deadlines.append(simulator_engine._hold_deadline - loop.time())

                # Reset for the next round
                await sim.close_door()
                await sim.wait_for_status(DOOR_STATE_CLOSED, timeout=2.0)
                if connect:
                    writer.close()
                    await writer.wait_closed()

            # Both paths granted a full fresh hold_time
            assert deadlines[0] == pytest.approx(10.0, abs=0.5)
            assert deadlines[1] == pytest.approx(10.0, abs=0.5)
        finally:
            await sim.stop()


# ============================================================================
# Battery Simulation Tests (deterministic ticks - no wall-clock waits)
# ============================================================================


def fast_battery_state(timing_config, **kwargs) -> DoorSimulatorState:
    """State with a battery config where one tick moves exactly 1%."""
    battery_config = kwargs.pop(
        "battery_config",
        BatteryConfig(charge_rate=600.0, discharge_rate=600.0, update_interval=0.1),
    )
    defaults = {"battery_percent": 50, "hold_time": 1}
    defaults.update(kwargs)
    return DoorSimulatorState(timing=timing_config, battery_config=battery_config, **defaults)


class TestBatteryConfigSetters:
    """Simple battery configuration setters."""

    def test_set_ac_present(self, simulator):
        """set_ac_present should update AC state."""
        assert simulator.state.ac_present is True
        simulator.set_ac_present(False)
        assert simulator.state.ac_present is False
        simulator.set_ac_present(True)
        assert simulator.state.ac_present is True

    def test_set_ac_present_same_value_is_noop(self, simulator, recorder):
        """Setting the current AC state must not rebroadcast."""
        simulator.set_ac_present(True)
        assert recorder == []

    def test_set_battery_present(self, simulator):
        """set_battery_present should update battery presence."""
        assert simulator.state.battery_present is True
        simulator.set_battery_present(False)
        assert simulator.state.battery_present is False
        simulator.set_battery_present(True)
        assert simulator.state.battery_present is True

    def test_set_battery_present_same_value_is_noop(self, simulator, recorder):
        """Setting the current battery presence must not rebroadcast."""
        simulator.set_battery_present(True)
        assert recorder == []

    def test_set_charge_rate(self, simulator):
        """set_charge_rate should update charge rate."""
        simulator.set_charge_rate(5.0)
        assert simulator.state.battery_config.charge_rate == 5.0
        simulator.set_charge_rate(0.0)
        assert simulator.state.battery_config.charge_rate == 0.0

    def test_set_discharge_rate(self, simulator):
        """set_discharge_rate should update discharge rate."""
        simulator.set_discharge_rate(0.5)
        assert simulator.state.battery_config.discharge_rate == 0.5
        simulator.set_discharge_rate(0.0)
        assert simulator.state.battery_config.discharge_rate == 0.0

    def test_set_charge_rate_negative_clamps_to_zero(self, simulator):
        """set_charge_rate should clamp negative values to zero."""
        simulator.set_charge_rate(-5.0)
        assert simulator.state.battery_config.charge_rate == 0.0

    def test_set_discharge_rate_negative_clamps_to_zero(self, simulator):
        """set_discharge_rate should clamp negative values to zero."""
        simulator.set_discharge_rate(-5.0)
        assert simulator.state.battery_config.discharge_rate == 0.0


class TestBatteryTick:
    """Exact arithmetic of a single battery simulation step."""

    @pytest.fixture
    def sim(self, timing_config):
        """An unstarted simulator with 1%-per-tick battery rates."""
        return DoorSimulator(port=0, state=fast_battery_state(timing_config))

    def test_charge_tick_adds_exactly_one_percent(self, sim):
        """charge_rate=600 at 0.1s interval charges exactly 1% per tick."""
        sim.state.ac_present = True
        sim._battery_tick()
        assert sim.state.battery_percent == 51

    def test_discharge_tick_removes_exactly_one_percent(self, sim):
        """discharge_rate=600 at 0.1s interval discharges exactly 1% per tick."""
        sim.state.ac_present = False
        sim._battery_tick()
        assert sim.state.battery_percent == 49

    def test_fractional_charge_accumulates(self, timing_config):
        """A 0.5%/tick charge rate reaches +1% after two ticks (carry fix).

        Regression: integer truncation used to leave charging stuck forever
        for any rate below 1% per interval.
        """
        state = fast_battery_state(
            timing_config,
            battery_config=BatteryConfig(
                charge_rate=300.0, discharge_rate=300.0, update_interval=0.1
            ),
        )
        sim = DoorSimulator(port=0, state=state)
        messages = attach_recorder(sim)

        sim._battery_tick()
        assert sim.state.battery_percent == 50  # only 0.5% accumulated
        assert messages == []  # no spurious broadcast without a change

        sim._battery_tick()
        assert sim.state.battery_percent == 51
        assert [m[FIELD_CMD] for m in messages] == [CMD_GET_DOOR_BATTERY]

    def test_fractional_discharge_accumulates(self, timing_config):
        """A 0.5%/tick discharge takes two ticks per percent (carry fix).

        Regression: truncation toward negative infinity used to make
        fractional discharge run at double speed.
        """
        state = fast_battery_state(
            timing_config,
            ac_present=False,
            battery_config=BatteryConfig(
                charge_rate=300.0, discharge_rate=300.0, update_interval=0.1
            ),
        )
        sim = DoorSimulator(port=0, state=state)

        sim._battery_tick()
        assert sim.state.battery_percent == 50
        sim._battery_tick()
        assert sim.state.battery_percent == 49

    def test_charge_caps_at_100_without_broadcast(self, sim):
        """At 100%, further charging neither changes nor broadcasts."""
        sim.state.battery_percent = 100
        messages = attach_recorder(sim)
        sim._battery_tick()
        assert sim.state.battery_percent == 100
        assert messages == []
        # The remainder is dropped so it cannot offset a later discharge
        assert sim._battery_carry == 0.0

    def test_discharge_floors_at_0_without_broadcast(self, sim):
        """At 0%, further discharge neither changes nor broadcasts."""
        sim.state.battery_percent = 0
        sim.state.ac_present = False
        messages = attach_recorder(sim)
        sim._battery_tick()
        assert sim.state.battery_percent == 0
        assert messages == []

    def test_no_change_when_battery_absent(self, sim):
        """No simulation happens while the battery is removed."""
        sim.state.battery_present = False
        sim.state.ac_present = False
        sim._battery_tick()
        assert sim.state.battery_percent == 50

    def test_zero_charge_rate_no_change(self, sim):
        """A zero charge rate disables charging."""
        sim.state.battery_config.charge_rate = 0.0
        sim.state.ac_present = True
        sim._battery_tick()
        assert sim.state.battery_percent == 50

    def test_zero_discharge_rate_no_change(self, sim):
        """A zero discharge rate disables discharging."""
        sim.state.battery_config.discharge_rate = 0.0
        sim.state.ac_present = False
        sim._battery_tick()
        assert sim.state.battery_percent == 50

    def test_tick_broadcasts_battery_status(self, sim):
        """A changed tick broadcasts the exact battery status payload."""
        messages = attach_recorder(sim)
        sim.state.ac_present = False
        sim._battery_tick()

        assert messages == [
            {
                FIELD_CMD: CMD_GET_DOOR_BATTERY,
                FIELD_BATTERY_PERCENT: 49,
                FIELD_BATTERY_PRESENT: "1",
                FIELD_AC_PRESENT: "0",
                **OK,
            }
        ]

    def test_discharge_across_threshold_sends_one_notification(self, sim):
        """Discharging 21% -> 20% emits exactly one LOW_BATTERY event."""
        sim.state.battery_percent = 21
        sim.state.ac_present = False
        sim.state.low_battery = True
        messages = attach_recorder(sim)

        sim._battery_tick()

        assert sim.state.battery_percent == 20
        low_battery = [m for m in messages if NOTIFY_LOW_BATTERY in m]
        assert low_battery == [{NOTIFY_LOW_BATTERY: ""}]

    def test_discharge_below_threshold_does_not_renotify(self, sim):
        """Ticks already below the threshold do not repeat the notification."""
        sim.state.battery_percent = 20
        sim.state.ac_present = False
        sim.state.low_battery = True
        messages = attach_recorder(sim)

        sim._battery_tick()

        assert sim.state.battery_percent == 19
        assert all(NOTIFY_LOW_BATTERY not in m for m in messages)

    def test_threshold_crossing_disabled_notification(self, sim):
        """No LOW_BATTERY event when the notification setting is off."""
        sim.state.battery_percent = 21
        sim.state.ac_present = False
        sim.state.low_battery = False
        messages = attach_recorder(sim)

        sim._battery_tick()

        assert sim.state.battery_percent == 20
        assert all(NOTIFY_LOW_BATTERY not in m for m in messages)


class TestBatteryLoop:
    """Lifecycle of the background battery simulation task."""

    async def test_loop_applies_ticks_over_the_wire(self, timing_config):
        """A running simulator discharges and broadcasts via the loop."""
        state = fast_battery_state(timing_config, ac_present=False)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        try:
            reader, writer = await connect_client(sim)
            try:
                messages = await read_messages_until(
                    reader, lambda msgs: any(FIELD_BATTERY_PERCENT in m for m in msgs)
                )
                battery = next(m for m in messages if FIELD_BATTERY_PERCENT in m)
                # The first broadcast is the first 1% step down from 50
                assert battery[FIELD_BATTERY_PERCENT] == 49
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await sim.stop()

    async def test_loop_exits_when_running_cleared_before_first_iteration(self, timing_config):
        """The loop returns immediately once _running is False."""
        sim = DoorSimulator(port=0, state=fast_battery_state(timing_config))
        await sim.start()
        sim._running = False
        await asyncio.wait_for(sim._battery_task, timeout=2.0)
        assert sim._battery_task.done()
        assert not sim._battery_task.cancelled()
        await sim.stop()

    async def test_loop_breaks_after_sleep_when_stopped(self, timing_config):
        """_running cleared mid-sleep breaks the loop without a cancel."""
        state = fast_battery_state(
            timing_config,
            battery_config=BatteryConfig(
                charge_rate=600.0, discharge_rate=600.0, update_interval=0.01
            ),
        )
        sim = DoorSimulator(port=0, state=state)
        await sim.start()
        await asyncio.sleep(0)  # let the loop enter its sleep
        sim._running = False
        await asyncio.wait_for(sim._battery_task, timeout=2.0)
        assert not sim._battery_task.cancelled()
        await sim.stop()

    async def test_loop_survives_tick_exception(self, timing_config, caplog):
        """A tick failure is logged and the loop keeps running."""
        state = fast_battery_state(
            timing_config,
            battery_config=BatteryConfig(
                charge_rate=600.0, discharge_rate=600.0, update_interval=0.01
            ),
        )
        sim = DoorSimulator(port=0, state=state)

        raised = asyncio.Event()

        def bad_tick():
            sim._battery_tick = lambda: None  # fail exactly once
            raised.set()
            raise RuntimeError("tick boom")

        sim._battery_tick = bad_tick

        with caplog.at_level(logging.ERROR, logger="powerpetdoor.simulator.server"):
            await sim.start()
            await asyncio.wait_for(raised.wait(), timeout=2.0)
            # Let the loop process the exception handler
            await asyncio.sleep(0)

        assert "Error in battery simulation" in caplog.text
        assert not sim._battery_task.done()
        await sim.stop()


# ============================================================================
# Client Connection Management Tests
# ============================================================================


class TestClientConnectionManagement:
    """Tests for client connect/disconnect and protocols list management."""

    async def test_protocols_list_starts_empty(self, simulator):
        """protocols list should be empty when no clients connected."""
        assert len(simulator.protocols) == 0

    async def test_connect_and_disconnect_callbacks(self, timing_config):
        """on_connect/on_disconnect fire as clients come and go."""
        connected = asyncio.Event()
        disconnected = asyncio.Event()
        sim = DoorSimulator(
            port=0,
            state=DoorSimulatorState(timing=timing_config),
            on_connect=connected.set,
            on_disconnect=disconnected.set,
        )
        await sim.start()
        try:
            reader, writer = await connect_client(sim)
            await asyncio.wait_for(connected.wait(), timeout=2.0)
            assert len(sim.protocols) == 1

            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(disconnected.wait(), timeout=2.0)
            assert sim.protocols == []
        finally:
            await sim.stop()

    async def test_client_connect_adds_to_protocols(self, simulator):
        """Connecting a client should add to protocols list."""
        reader, writer = await connect_client(simulator)

        assert len(simulator.protocols) == 1

        writer.close()
        await writer.wait_closed()

    async def test_client_disconnect_removes_from_protocols(self, simulator):
        """Disconnecting a client should remove from protocols list."""
        reader, writer = await connect_client(simulator)
        assert len(simulator.protocols) == 1

        writer.close()
        await writer.wait_closed()
        await wait_for_protocols(simulator, 0)

        assert len(simulator.protocols) == 0

    async def test_multiple_clients_tracked(self, simulator):
        """Multiple clients should all be tracked in protocols list."""
        clients = [await connect_client(simulator) for _ in range(3)]
        assert len(simulator.protocols) == 3

        # Disconnect one
        _, writer = clients.pop()
        writer.close()
        await writer.wait_closed()
        await wait_for_protocols(simulator, 2)

        # Cleanup remaining
        for _, writer in clients:
            writer.close()
            await writer.wait_closed()


# ============================================================================
# Broadcast Tests (exact payloads via a recording protocol)
# ============================================================================


class TestBroadcastPayloads:
    """Each broadcast helper sends the exact documented payload."""

    def test_broadcast_settings(self, simulator, recorder):
        """broadcast_settings sends the full settings dict."""
        simulator.broadcast_settings()
        assert recorder == [
            {FIELD_CMD: CMD_GET_SETTINGS, FIELD_SETTINGS: simulator.state.get_settings(), **OK}
        ]

    @pytest.mark.parametrize(
        ("enabled", "cmd", "value"),
        [
            (True, CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK, "1"),
            (False, CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK, "0"),
        ],
    )
    def test_broadcast_safety_lock(self, simulator, recorder, enabled, cmd, value):
        """broadcast_safety_lock sends the enable/disable command envelope."""
        simulator.broadcast_safety_lock(enabled)
        assert recorder == [
            {FIELD_CMD: cmd, FIELD_SETTINGS: {FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: value}, **OK}
        ]

    @pytest.mark.parametrize(
        ("enabled", "cmd", "value"),
        [(True, CMD_ENABLE_CMD_LOCKOUT, "1"), (False, CMD_DISABLE_CMD_LOCKOUT, "0")],
    )
    def test_broadcast_cmd_lockout(self, simulator, recorder, enabled, cmd, value):
        """broadcast_cmd_lockout sends the enable/disable command envelope."""
        simulator.broadcast_cmd_lockout(enabled)
        assert recorder == [{FIELD_CMD: cmd, FIELD_SETTINGS: {FIELD_CMD_LOCKOUT: value}, **OK}]

    @pytest.mark.parametrize(
        ("enabled", "cmd", "value"),
        [(True, CMD_ENABLE_AUTORETRACT, "1"), (False, CMD_DISABLE_AUTORETRACT, "0")],
    )
    def test_broadcast_autoretract(self, simulator, recorder, enabled, cmd, value):
        """broadcast_autoretract sends the enable/disable command envelope."""
        simulator.broadcast_autoretract(enabled)
        assert recorder == [{FIELD_CMD: cmd, FIELD_SETTINGS: {FIELD_AUTORETRACT: value}, **OK}]

    def test_broadcast_hold_time_sends_centiseconds(self, simulator, recorder):
        """broadcast_hold_time converts seconds to centiseconds."""
        simulator.state.hold_time = 5.0
        simulator.broadcast_hold_time()
        assert recorder == [{FIELD_CMD: CMD_SET_HOLD_TIME, FIELD_HOLD_TIME: 500, **OK}]

    def test_broadcast_timezone_posix(self, simulator, recorder, monkeypatch):
        """broadcast_timezone converts IANA to POSIX when the cache is ready."""
        monkeypatch.setattr(server_module, "is_cache_initialized", lambda: True)
        monkeypatch.setattr(
            server_module, "get_posix_tz_string", lambda tz: "EST5EDT,M3.2.0,M11.1.0"
        )
        simulator.broadcast_timezone()
        assert recorder == [{FIELD_CMD: CMD_SET_TIMEZONE, FIELD_TZ: "EST5EDT,M3.2.0,M11.1.0", **OK}]

    def test_broadcast_timezone_raw_when_cache_uninitialized(
        self, simulator, recorder, monkeypatch
    ):
        """Without the tz cache, the stored value is sent as-is."""
        monkeypatch.setattr(server_module, "is_cache_initialized", lambda: False)
        simulator.broadcast_timezone()
        assert recorder == [{FIELD_CMD: CMD_SET_TIMEZONE, FIELD_TZ: simulator.state.timezone, **OK}]

    def test_broadcast_timezone_raw_when_unconvertible(self, simulator, recorder, monkeypatch):
        """An unconvertible zone falls back to the stored value."""
        monkeypatch.setattr(server_module, "is_cache_initialized", lambda: True)
        monkeypatch.setattr(server_module, "get_posix_tz_string", lambda tz: None)
        simulator.broadcast_timezone()
        assert recorder == [{FIELD_CMD: CMD_SET_TIMEZONE, FIELD_TZ: simulator.state.timezone, **OK}]

    def test_broadcast_notification_settings(self, simulator, recorder):
        """broadcast_notification_settings sends the SET envelope."""
        simulator.broadcast_notification_settings()
        assert recorder == [
            {
                FIELD_CMD: CMD_SET_NOTIFICATIONS,
                FIELD_NOTIFICATIONS: simulator.state.get_notifications(),
                **OK,
            }
        ]

    @pytest.mark.parametrize(
        ("enabled", "cmd", "value"), [(True, CMD_POWER_ON, "1"), (False, CMD_POWER_OFF, "0")]
    )
    def test_broadcast_power(self, simulator, recorder, enabled, cmd, value):
        """broadcast_power sends the power command envelope."""
        simulator.broadcast_power(enabled)
        assert recorder == [{FIELD_CMD: cmd, FIELD_POWER: value, **OK}]

    @pytest.mark.parametrize(
        ("enabled", "cmd", "value"), [(True, CMD_ENABLE_AUTO, "1"), (False, CMD_DISABLE_AUTO, "0")]
    )
    def test_broadcast_auto(self, simulator, recorder, enabled, cmd, value):
        """broadcast_auto sends the timers command envelope."""
        simulator.broadcast_auto(enabled)
        assert recorder == [{FIELD_CMD: cmd, FIELD_AUTO: value, **OK}]

    @pytest.mark.parametrize(
        ("enabled", "cmd", "value"),
        [(True, CMD_ENABLE_INSIDE, "1"), (False, CMD_DISABLE_INSIDE, "0")],
    )
    def test_broadcast_inside_sensor(self, simulator, recorder, enabled, cmd, value):
        """broadcast_inside_sensor sends the sensor command envelope."""
        simulator.broadcast_inside_sensor(enabled)
        assert recorder == [{FIELD_CMD: cmd, FIELD_INSIDE: value, **OK}]

    @pytest.mark.parametrize(
        ("enabled", "cmd", "value"),
        [(True, CMD_ENABLE_OUTSIDE, "1"), (False, CMD_DISABLE_OUTSIDE, "0")],
    )
    def test_broadcast_outside_sensor(self, simulator, recorder, enabled, cmd, value):
        """broadcast_outside_sensor sends the sensor command envelope."""
        simulator.broadcast_outside_sensor(enabled)
        assert recorder == [{FIELD_CMD: cmd, FIELD_OUTSIDE: value, **OK}]

    def test_broadcast_hardware_info(self, simulator, recorder):
        """broadcast_hardware_info sends the firmware/hardware envelope."""
        simulator.broadcast_hardware_info()
        assert recorder == [
            {
                FIELD_CMD: CMD_GET_HW_INFO,
                FIELD_FWINFO: {
                    FIELD_FW_MAJOR: 1,
                    FIELD_FW_MINOR: 2,
                    FIELD_FW_PATCH: 3,
                    FIELD_HW_VERSION: "1",
                    FIELD_HW_REVISION: "1",
                },
                **OK,
            }
        ]

    def test_broadcast_stats(self, simulator, recorder):
        """broadcast_stats sends the open-cycle counters."""
        simulator.state.total_open_cycles = 7
        simulator.state.total_auto_retracts = 2
        simulator.broadcast_stats()
        assert recorder == [
            {
                FIELD_CMD: CMD_GET_DOOR_OPEN_STATS,
                FIELD_TOTAL_OPEN_CYCLES: 7,
                FIELD_TOTAL_AUTO_RETRACTS: 2,
                **OK,
            }
        ]

    def test_broadcast_schedules(self, simulator, recorder):
        """broadcast_schedules sends the schedule index list."""
        simulator.state.schedules[4] = Schedule(index=4, inside=True)
        simulator.broadcast_schedules()
        assert recorder == [{FIELD_CMD: CMD_GET_SCHEDULE_LIST, FIELD_SCHEDULES: [4], **OK}]

    def test_broadcast_schedule(self, simulator, recorder):
        """broadcast_schedule sends the full schedule payload."""
        schedule = Schedule(index=2, inside=True)
        simulator.broadcast_schedule(schedule)
        assert recorder == [{FIELD_CMD: CMD_SET_SCHEDULE, FIELD_SCHEDULE: schedule.to_dict(), **OK}]

    def test_broadcast_schedule_delete(self, simulator, recorder):
        """broadcast_schedule_delete sends the deleted index."""
        simulator.broadcast_schedule_delete(3)
        assert recorder == [{FIELD_CMD: CMD_DELETE_SCHEDULE, FIELD_INDEX: 3, **OK}]

    def test_broadcast_notifications(self, simulator, recorder):
        """broadcast_notifications sends the GET envelope."""
        simulator.broadcast_notifications()
        assert recorder == [
            {
                FIELD_CMD: CMD_GET_NOTIFICATIONS,
                FIELD_NOTIFICATIONS: simulator.state.get_notifications(),
                **OK,
            }
        ]

    def test_broadcast_all_sends_everything_in_order(self, simulator, recorder):
        """broadcast_all pushes the full state snapshot in a fixed order."""
        simulator.broadcast_all()
        assert [m[FIELD_CMD] for m in recorder] == [
            DOOR_STATUS,
            CMD_GET_SETTINGS,
            CMD_GET_DOOR_BATTERY,
            CMD_GET_HW_INFO,
            CMD_GET_DOOR_OPEN_STATS,
            CMD_GET_SCHEDULE_LIST,
            CMD_GET_NOTIFICATIONS,
        ]

    async def test_sensor_trigger_broadcasts_bare_notification(self, simulator, recorder):
        """An enabled sensor event broadcasts the bare notification envelope."""
        from powerpetdoor.const import FIELD_SENSOR_STATE

        simulator.state.sensor_on_indoor = True
        simulator.trigger_sensor("inside")

        notifications = [m for m in recorder if NOTIFY_SENSOR_INDOOR in m]
        assert notifications == [{NOTIFY_SENSOR_INDOOR: "", FIELD_SENSOR_STATE: SENSOR_STATE_ON}]

    def test_broadcast_battery_reports_zero_when_absent(self, simulator, recorder):
        """With no battery installed, the broadcast reports 0%."""
        simulator.state.battery_present = False
        simulator.state.battery_percent = 80
        simulator._broadcast_battery_status()
        assert recorder == [
            {
                FIELD_CMD: CMD_GET_DOOR_BATTERY,
                FIELD_BATTERY_PERCENT: 0,
                FIELD_BATTERY_PRESENT: "0",
                FIELD_AC_PRESENT: "1",
                **OK,
            }
        ]

    async def test_broadcast_settings_sends_to_all_clients(self, simulator):
        """broadcast_settings reaches every connected client."""
        clients = [await connect_client(simulator) for _ in range(2)]

        simulator.broadcast_settings()

        for reader, writer in clients:
            try:
                messages = await read_messages_until(
                    reader, lambda msgs: any(FIELD_SETTINGS in m for m in msgs)
                )
                settings = next(m for m in messages if FIELD_SETTINGS in m)
                assert settings[FIELD_CMD] == CMD_GET_SETTINGS
            finally:
                writer.close()
                await writer.wait_closed()


# ============================================================================
# Notification Event Tests (D2: bare envelope)
# ============================================================================


class TestLowBatteryNotification:
    """Low battery notifications use the bare envelope from protocol.md."""

    async def test_set_battery_crossing_threshold_sends_bare_envelope(self, simulator):
        """Crossing the threshold emits exactly {"LOW_BATTERY": ""}."""
        reader, writer = await connect_client(simulator)
        try:
            simulator.state.low_battery = True
            simulator.set_battery(15)  # 100 -> 15 crosses the 20% threshold

            messages = await read_messages_until(
                reader, lambda msgs: any(NOTIFY_LOW_BATTERY in m for m in msgs)
            )
            low_battery = [m for m in messages if NOTIFY_LOW_BATTERY in m]
            assert low_battery == [{NOTIFY_LOW_BATTERY: ""}]
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_no_notification_when_disabled(self, simulator):
        """No LOW_BATTERY event when the notification setting is off."""
        reader, writer = await connect_client(simulator)
        try:
            simulator.state.low_battery = False
            simulator.set_battery(15)

            # The battery status broadcast still arrives, but no notification
            messages = await read_messages_until(
                reader, lambda msgs: any(FIELD_BATTERY_PERCENT in m for m in msgs)
            )
            assert all(NOTIFY_LOW_BATTERY not in m for m in messages)
        finally:
            writer.close()
            await writer.wait_closed()


# ============================================================================
# Shutdown Cleanliness Tests (L14: no leaked tasks)
# ============================================================================


class TestShutdownCleanliness:
    """stop() must cancel and await every simulator-owned task."""

    async def test_stop_with_door_mid_cycle(self, timing_config):
        """Stopping mid-cycle leaves no pending engine task."""
        state = DoorSimulatorState(timing=timing_config, hold_time=1)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()

        sim.trigger_sensor("inside")
        assert sim.engine._task is not None

        await sim.stop()
        assert sim.engine._task is None
        assert sim.engine._retired == set()
        assert sim.engine._aux_tasks == set()
        assert sim._battery_task is None

    async def test_stop_with_pending_sensor_timer(self, timing_config):
        """A pending timed sensor deactivation is cancelled by stop()."""
        state = DoorSimulatorState(timing=timing_config, hold_time=1)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()

        sim.activate_sensor("inside", duration=30.0)
        assert len(sim.engine._aux_tasks) == 1
        timer_task = next(iter(sim.engine._aux_tasks))

        await sim.stop()
        assert timer_task.cancelled()
        assert sim.engine._aux_tasks == set()

    async def test_stop_with_connected_client_mid_cycle(self, timing_config):
        """Stopping with a connected client cancels protocol tasks cleanly."""
        state = DoorSimulatorState(timing=timing_config, hold_time=1)
        sim = DoorSimulator(port=0, state=state)
        await sim.start()

        reader, writer = await connect_client(sim)
        try:
            protocol = sim.protocols[0]

            sim.trigger_sensor("inside")
            await sim.stop()

            assert all(task.done() for task in protocol._tasks)
            assert sim.protocols == []
        finally:
            writer.close()
