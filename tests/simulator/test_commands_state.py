# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The `reset` and `statedoc` commands, and the path policy they inherit."""

from __future__ import annotations

import json

import pytest

from powerpetdoor.const import DOOR_STATE_CLOSED, DOOR_STATE_KEEPUP
from powerpetdoor.simulator import scripting
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.commands.state import describe_state_argument
from powerpetdoor.simulator.scripting import ScriptRunner
from powerpetdoor.simulator.server import DoorSimulator
from powerpetdoor.simulator.state import DoorSimulatorState, DoorTimingConfig

FAST = DoorTimingConfig(
    rise_time=0.05,
    slowing_time=0.02,
    closing_start_time=0.02,
    closing_top_time=0.02,
    closing_mid_time=0.02,
)

INITIAL = {
    "settings": {"hold_time": 42, "power": False, "safety_lock": True},
    "battery": {"percent": 17},
    "schedules": [{"index": 0, "inside": True, "start": "07:00", "end": "19:00"}],
}


@pytest.fixture
async def simulator():
    state = DoorSimulatorState(timing=FAST, hold_time=0.05)
    sim = DoorSimulator(host="127.0.0.1", port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
def states_dir(tmp_path):
    (tmp_path / "fixture.json").write_text(json.dumps(INITIAL))
    return tmp_path


def make_handler(simulator, states_dir=None, initial=None, allow_paths=True):
    runner = ScriptRunner(simulator, initial_state_document=initial)
    handler = CommandHandler(
        simulator,
        runner,
        lambda: None,
        allow_script_paths=allow_paths,
        states_dir=str(states_dir) if states_dir else None,
        initial_state_document=initial,
    )
    runner.load_state_document = handler.load_state_document
    return handler


class TestResetWithoutAnInitialState:
    async def test_reset_restores_the_defaults(self, simulator):
        handler = make_handler(simulator)
        simulator.state.hold_time = 99
        simulator.state.power = False

        result = await handler.execute("reset")

        assert result.success is True
        assert result.message == "Reset to defaults"
        assert simulator.state.hold_time == DoorSimulatorState().hold_time
        assert simulator.state.power is True

    async def test_reset_parks_a_moving_door_closed(self, simulator):
        """The engine's own bookkeeping must not survive the reset."""
        handler = make_handler(simulator)
        await simulator.open_door(hold=True)
        await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)

        await handler.execute("reset")

        assert simulator.state.door_status == DOOR_STATE_CLOSED

    async def test_reset_clears_sensors_and_obstructions(self, simulator):
        handler = make_handler(simulator)
        simulator.simulate_obstruction(0)
        simulator.activate_sensor("inside", 0)

        await handler.execute("reset")

        assert simulator.state.obstruction_active is False
        assert simulator.state.inside_sensor_active is False

    async def test_reset_clears_the_statistics(self, simulator):
        """Counts are state, not configuration.

        A reset that left totalOpenCycles behind would make test isolation
        quietly wrong - each test would inherit the last one's tally.
        """
        handler = make_handler(simulator)
        simulator.state.total_open_cycles = 7
        simulator.state.total_auto_retracts = 3

        await handler.execute("reset")

        assert simulator.state.total_open_cycles == 0
        assert simulator.state.total_auto_retracts == 0

    async def test_the_door_still_works_after_a_reset(self, simulator):
        """The engine is re-armed, not merely stopped."""
        handler = make_handler(simulator)
        await handler.execute("reset")

        await simulator.open_door(hold=True)

        assert await simulator.wait_for_status(DOOR_STATE_KEEPUP, timeout=2.0)


class TestResetWithAnInitialState:
    async def test_a_bare_reset_returns_to_the_initial_document(self, simulator):
        handler = make_handler(simulator, initial=INITIAL)
        simulator.state.hold_time = 1
        simulator.state.power = True

        result = await handler.execute("reset")

        assert result.message == "Reset to initial state"
        assert simulator.state.hold_time == 42
        assert simulator.state.power is False
        assert sorted(simulator.state.schedules) == [0]

    async def test_reset_by_name_loads_from_the_states_directory(self, simulator, states_dir):
        handler = make_handler(simulator, states_dir=states_dir)

        result = await handler.execute("reset fixture")

        assert result.success is True
        assert result.message == "Reset to state document: fixture"
        assert simulator.state.hold_time == 42

    async def test_an_unknown_name_is_refused(self, simulator, states_dir):
        handler = make_handler(simulator, states_dir=states_dir)

        result = await handler.execute("reset nope")

        assert result.success is False
        assert "Unknown state document: nope" in result.message

    async def test_without_a_states_dir_the_message_says_so(self, simulator):
        """The operator needs to know a flag is missing, not just a name."""
        handler = make_handler(simulator)

        result = await handler.execute("reset fixture")

        assert result.success is False
        assert "--states-dir" in result.message

    async def test_a_malformed_document_fails_the_command_not_the_daemon(
        self, simulator, states_dir
    ):
        (states_dir / "broken.json").write_text("{nope")
        handler = make_handler(simulator, states_dir=states_dir)

        result = await handler.execute("reset broken")

        assert result.success is False
        assert "not valid" in result.message


class TestStatePathPolicy:
    """`reset` inherits the script path policy rather than inventing one.

    The control channel is unauthenticated; a `reset` that could name any
    file on the host would reopen in a new place the hole
    `_load_script_restricted` closes for scripts.
    """

    async def test_locally_a_path_works(self, simulator, states_dir):
        handler = make_handler(simulator, allow_paths=True)

        result = await handler.execute(f"reset {states_dir / 'fixture.json'}")

        assert result.success is True
        assert simulator.state.hold_time == 42

    @pytest.mark.parametrize(
        "ref",
        ["/etc/passwd", "../outside.json", "./local.json", "sub\\dir.json"],
        ids=["absolute", "traversal", "dot-relative", "backslash"],
    )
    async def test_over_the_control_channel_a_path_is_refused(self, simulator, ref):
        handler = make_handler(simulator, allow_paths=False)

        result = await handler.execute(f"reset {ref}")

        assert result.success is False
        assert "not allowed over the control channel" in result.message

    async def test_over_the_control_channel_a_bare_name_still_works(self, simulator, states_dir):
        """The refusal must leave a usable form, not just say no."""
        handler = make_handler(simulator, states_dir=states_dir, allow_paths=False)

        result = await handler.execute("reset fixture")

        assert result.success is True

    async def test_a_name_resolving_outside_the_directory_is_refused(
        self, simulator, states_dir, tmp_path
    ):
        """A symlink out of the states dir is the same escape as a path."""
        outside = tmp_path.parent / "escaped.json"
        outside.write_text(json.dumps(INITIAL))
        try:
            (states_dir / "linked.json").symlink_to(outside)
        except OSError:  # pragma: no cover - platforms without symlinks
            pytest.skip("symlinks unavailable")
        handler = make_handler(simulator, states_dir=states_dir, allow_paths=False)

        result = await handler.execute("reset linked")

        assert result.success is False
        assert "resolves outside" in result.message

    def test_the_help_text_follows_the_policy(self, monkeypatch):
        """Advertising a path on the channel that refuses it points the
        operator at a form the next line of code rejects."""
        monkeypatch.setattr(scripting, "_script_paths_allowed", True)
        assert "file path" in describe_state_argument()

        monkeypatch.setattr(scripting, "_script_paths_allowed", False)
        assert "paths are not accepted" in describe_state_argument()


class TestListStates:
    """`list states` makes --states-dir contents discoverable.

    Without it a bare `reset <name>` only works if you already know the
    name - there are no built-in state documents to fall back on.
    """

    async def test_it_lists_the_documents(self, simulator, states_dir):
        (states_dir / "quiet_night.json").write_text("{}")
        handler = make_handler(simulator, states_dir=states_dir)

        result = await handler.execute("list states")

        assert result.success is True
        assert "fixture" in result.message
        assert "quiet_night" in result.message
        assert result.data == {"states": ["fixture", "quiet_night"]}

    async def test_without_a_states_dir_it_names_the_missing_flag(self, simulator):
        handler = make_handler(simulator)

        result = await handler.execute("list states")

        assert result.success is True
        assert "--states-dir" in result.message

    async def test_an_empty_directory_still_shows_the_header(self, simulator, tmp_path):
        """The flag's effect must be visible, not silently absent - the
        same rule `list scripts` follows."""
        handler = make_handler(simulator, states_dir=tmp_path)

        result = await handler.execute("list states")

        assert str(tmp_path) in result.message
        assert "(none)" in result.message

    async def test_a_document_resolving_outside_is_not_advertised(
        self, simulator, states_dir, tmp_path
    ):
        """What is listed must be exactly what `reset` will accept.

        The script listing learned this already: a symlink out of the
        directory was advertised and then refused by name, in a message
        that contradicted itself inside one line.
        """
        outside = tmp_path.parent / "escaped_listing.json"
        outside.write_text("{}")
        try:
            (states_dir / "linked.json").symlink_to(outside)
        except OSError:  # pragma: no cover - platforms without symlinks
            pytest.skip("symlinks unavailable")
        handler = make_handler(simulator, states_dir=states_dir)

        assert "linked" not in (await handler.execute("list states")).message

    def test_the_lister_is_empty_without_a_directory(self):
        """The guard `list states` relies on, reachable on its own.

        The lister lives in `state_io` so `ppd-simulator --list-states`
        and the `list states` command cannot disagree about what `reset`
        will accept.
        """
        from powerpetdoor.simulator.state_io import state_documents_in

        assert state_documents_in(None) == []
        assert state_documents_in("") == []

    async def test_bare_list_still_means_scripts(self, simulator, states_dir):
        handler = make_handler(simulator, states_dir=states_dir)

        assert (await handler.execute("list")).message.startswith("Built-in scripts:")
        assert (await handler.execute("list scripts")).message.startswith("Built-in scripts:")
