# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Documentation *correctness* pins.

``tests/test_exports.py`` proves every exported name is mentioned somewhere
in the prose. These two check what the docs *claim* against what the code
actually does, by execution:

- ``docs/protocol.md`` is the only place the keepalive frame is specified,
  this project has three independent implementations built from it, and a
  wrong example produces a hard disconnect every ~90 s that looks like a
  flaky network.
- ``docs/operation.md`` is the behavioural specification the simulator
  exists to be faithful to; its sensor-gating prose is executed against the
  shipped engine here.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from powerpetdoor.const import (
    FIELD_CMD,
    FIELD_SUCCESS,
    PING,
    PONG,
)
from powerpetdoor.simulator.protocol import DoorSimulatorProtocol
from powerpetdoor.simulator.state import DoorSimulatorState

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_MD = REPO_ROOT / "docs" / "protocol.md"
OPERATION_MD = REPO_ROOT / "docs" / "operation.md"


# ============================================================================
# Markdown helpers
# ============================================================================


def _slug(heading: str) -> str:
    """A heading reduced to the key :func:`_sections` files it under."""
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading.strip().lower()))


def _sections(path: Path) -> dict[str, str]:
    """Map slug -> body text for every heading in a markdown file.

    A section owns its subsections: ``## Message Format`` includes the
    ``### Request Format`` block under it, which is what a reader following
    the link actually sees.

    Fenced blocks are skipped when looking for headings: ``# Door protocol
    on 3000`` inside a ```` ```bash ```` block is a shell comment, not a
    heading.
    """
    lines = path.read_text().splitlines()
    sections: dict[str, list[str]] = {}
    open_sections: list[tuple[int, list[str]]] = []
    fenced = False
    for line in lines:
        if line.startswith("```"):
            fenced = not fenced
        if line.startswith("#") and not fenced:
            level = len(line) - len(line.lstrip("#"))
            heading = line.lstrip("#").strip()
            open_sections = [(lvl, body) for lvl, body in open_sections if lvl < level]
            body = sections.setdefault(_slug(heading), [])
            open_sections.append((level, body))
            continue
        for _, body in open_sections:
            body.append(line)
    return {anchor: "\n".join(body) for anchor, body in sections.items()}


def _json_blocks(text: str) -> list[dict]:
    """Every fenced ```json block in ``text``, parsed.

    Blocks that hold more than one object (the docs use that for
    alternatives) contribute each object separately.
    """
    blocks: list[dict] = []
    for body in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
        try:
            blocks.append(json.loads(body))
            continue
        except json.JSONDecodeError:
            pass
        for line in body.strip().splitlines():
            if line.strip().startswith("{"):
                blocks.append(json.loads(line.strip()))
    return blocks


# ============================================================================
# Keepalive
# ============================================================================


class TestDocumentedKeepaliveMatchesTheImplementation:
    """The documented PING/PONG frame must be the real one, both ways."""

    @pytest.fixture
    def documented(self) -> tuple[dict, dict]:
        """The request/response examples under docs/protocol.md 'Keepalive'."""
        blocks = _json_blocks(_sections(PROTOCOL_MD)["keepalive"])
        assert len(blocks) == 2, "Keepalive must document exactly one request and one response"
        return blocks[0], blocks[1]

    def test_documented_request_carries_the_token_the_client_sends(self, documented):
        """Not `{"PING": ""}`: the client sends wall-clock ms as a string."""
        request, _ = documented

        assert PING in request
        token = request[PING]
        assert isinstance(token, str)
        # `str(round(time.time() * 1000))` - digits only, 13 of them for
        # every date this millennium.
        assert token.isdigit()
        assert len(token) == 13

    def test_documented_response_echoes_the_documented_request_token(self, documented):
        """The rule the client actually enforces, stated by the example."""
        request, response = documented

        assert response[PONG] == request[PING]
        assert response[FIELD_CMD] == PONG
        assert response[FIELD_SUCCESS] == "true"

    async def test_the_client_accepts_the_documented_response(self, mock_client, documented):
        """An implementation answering exactly the documented frame works."""
        request, response = documented
        client, _, device = mock_client
        client._last_ping = request[PING]
        client._failed_pings = 2

        device.send_response_sync(response)
        await asyncio.sleep(0)

        assert client._last_ping is None
        assert client._failed_pings == 0

    async def test_the_client_rejects_the_old_empty_pong_example(self, mock_client, documented):
        """`{"PONG": ""}` is what the doc used to show; it is not a reply.

        Pinning the *negative* is the point: the doc bug was invisible
        because nothing asserted that the documented frame is the only one
        the client counts.
        """
        request, response = documented
        client, _, device = mock_client
        client._last_ping = request[PING]
        client._failed_pings = 2

        device.send_response_sync({**response, PONG: ""})
        await asyncio.sleep(0)

        assert client._last_ping == request[PING]
        assert client._failed_pings == 2

    async def test_the_simulator_echoes_the_documented_request(self, documented):
        """The simulator's reply must be the documented response, verbatim."""
        request, response = documented
        state = DoorSimulatorState()
        protocol = DoorSimulatorProtocol(state)
        written: list[dict] = []

        class Recorder:
            def get_extra_info(self, name):
                return ("127.0.0.1", 12345)

            def write(self, data):
                written.append(json.loads(data.decode("ascii")))

            def close(self):
                pass

        protocol.connection_made(Recorder())
        try:
            protocol.data_received(json.dumps(request).encode("ascii"))
            await protocol.drain()
        finally:
            protocol.connection_lost(None)

        assert len(written) == 1
        # The simulator omits `msgID` on PONG; every other documented field
        # must match exactly.
        assert {k: v for k, v in written[0].items() if k != "dir"} == {
            k: v for k, v in response.items() if k != "dir"
        }
        assert written[0]["dir"] == response["dir"]


class TestKeepaliveTokenIsGeneratedAsDocumented:
    """The generated token really is wall-clock milliseconds, as a string."""

    async def test_keepalive_emits_a_13_digit_millisecond_string(self, mock_client):
        client, transport, _ = mock_client
        client.cfg_keepalive = 0
        transport.clear()

        await client.keepalive()

        assert isinstance(client._last_ping, str)
        assert client._last_ping.isdigit()
        assert len(client._last_ping) == 13


# ============================================================================
# docs/operation.md - the behavioural specification the simulator exists to
# be faithful to
# ============================================================================


class TestOperationMdSensorGating:
    """`docs/operation.md`'s sensor-gating prose, executed.

    Its "Schedule and Sensor Interaction" and "Power and Battery" sections
    are a list of assertions; with nothing checking them, `activate_sensor`
    came to open the door outside every scheduled window while
    `trigger_sensor` refused. These run the prose against the shipped
    engine through **both** sensor entry points, because both are reachable
    from both front ends.
    """

    @staticmethod
    def _engine():
        from powerpetdoor.simulator import DoorTimingConfig
        from powerpetdoor.simulator.engine import DoorMotionEngine

        state = DoorSimulatorState(
            timing=DoorTimingConfig(rise_time=0.01, slowing_time=0.01), hold_time=0.05
        )
        return DoorMotionEngine(state), state

    def test_the_prose_still_says_what_these_tests_assert(self):
        """Pin the sentences, so rewording the spec forces the tests to move."""
        sections = _sections(OPERATION_MD)

        schedule = sections["schedule-and-sensor-interaction"]
        assert "Outside scheduled windows, sensor triggers are ignored" in schedule
        assert "Sensors only respond during their scheduled time windows" in schedule
        assert "Schedules are stored but not applied" in schedule

        power = sections["power-and-battery"]
        assert "Door will not respond to sensor triggers" in power

    @pytest.mark.parametrize(
        "trigger",
        [
            pytest.param(lambda e, s: e.trigger_sensor(s), id="trigger_sensor"),
            pytest.param(lambda e, s: e.activate_sensor(s, 5.0), id="activate_sensor"),
        ],
    )
    @pytest.mark.parametrize("sensor", ["inside", "outside"])
    async def test_outside_a_scheduled_window_a_sensor_trigger_is_ignored(self, trigger, sensor):
        """*"Outside scheduled windows, sensor triggers are ignored."*"""
        from powerpetdoor.simulator import Schedule

        engine, state = self._engine()
        try:
            state.auto = True  # timersEnabled
            state.schedules[0] = Schedule(
                index=0,
                enabled=True,
                # No scheduled day at all: outside every window, at
                # every time of day. (A window with coinciding ends is the
                # *whole* day - the end is exclusive, so an empty window
                # has no spelling.)
                days_of_week=[False] * 7,
                inside=(sensor == "inside"),
                start_hour=6,
                start_min=0,
                end_hour=22,
                end_min=0,
            )

            trigger(engine, sensor)
            await asyncio.sleep(0)

            assert state.door_status == "DOOR_CLOSED"
        finally:
            await engine.stop()

    @pytest.mark.parametrize(
        "trigger",
        [
            pytest.param(lambda e, s: e.trigger_sensor(s), id="trigger_sensor"),
            pytest.param(lambda e, s: e.activate_sensor(s, 5.0), id="activate_sensor"),
        ],
    )
    async def test_with_timers_disabled_a_stored_schedule_is_not_applied(self, trigger):
        """*"Schedules are stored but not applied."* - the control for the
        test above: the same closed window, with `auto` off, opens the door."""
        from powerpetdoor.simulator import Schedule

        engine, state = self._engine()
        try:
            state.auto = False
            state.schedules[0] = Schedule(
                index=0,
                enabled=True,
                days_of_week=[False] * 7,
                inside=True,
                start_hour=6,
                start_min=0,
                end_hour=22,
                end_min=0,
            )

            trigger(engine, "inside")
            await asyncio.sleep(0)

            assert state.door_status == "DOOR_RISING"
        finally:
            await engine.stop()

    @pytest.mark.parametrize(
        "trigger",
        [
            pytest.param(lambda e, s: e.trigger_sensor(s), id="trigger_sensor"),
            pytest.param(lambda e, s: e.activate_sensor(s, 5.0), id="activate_sensor"),
        ],
    )
    @pytest.mark.parametrize("sensor", ["inside", "outside"])
    async def test_with_power_off_the_door_does_not_respond_to_sensor_triggers(
        self, trigger, sensor
    ):
        """*"Door will not respond to sensor triggers."*"""
        engine, state = self._engine()
        try:
            state.power = False

            trigger(engine, sensor)
            await asyncio.sleep(0)

            assert state.door_status == "DOOR_CLOSED"
        finally:
            await engine.stop()

    def test_the_safety_interaction_table_matches_is_sensor_blocking_close(self):
        """*"If command lockout is ON, sensor detection never blocks door
        closing, regardless of other settings."*

        This is the one place `docs/operation.md` does pin command lockout,
        and it is about blocking the *close* - which is why the simulator's
        two sensor entry points had to be made consistent on
        `trigger_sensor`'s answer rather than on the document's.
        """
        section = " ".join(_sections(OPERATION_MD)["how-safety-features-interact"].split())
        assert (
            "If **command lockout is ON**, sensor detection never blocks door closing, "
            "regardless of other settings." in section
        )

        state = DoorSimulatorState()
        state.inside_sensor_active = True
        state.inside = True
        assert state.is_sensor_blocking_close() is True

        state.cmd_lockout = True
        assert state.is_sensor_blocking_close() is False

        state.cmd_lockout = False
        state.inside_sensor_active = False
        state.outside_sensor_active = True
        state.outside = True
        # The safety lock does NOT enter into this. It is the app's "always
        # allow pet entry inside override timers" - measured, see
        # docs/protocol.md - so it grants *entry* past the schedule and has
        # nothing to say about whether a detected pet holds the door open.
        # That is command lockout's job, checked above. This used to assert
        # the opposite, on the reading the field name invites.
        state.safety_lock = False
        assert state.is_sensor_blocking_close() is True
        state.safety_lock = True
        assert state.is_sensor_blocking_close() is True

        # ...and a disabled sensor still does not block, which is the
        # second operand of that guard and the one a lock-shaped reading
        # would have masked.
        state.outside = False
        assert state.is_sensor_blocking_close() is False


class TestReadmeLibraryTreeMatchesTheSource:
    """The README's "Library Structure" tree lists every shipped module.

    That tree is the first thing a contributor reads to find their way
    around, and it silently rotted: `i18n.py` and `locales/` were added and
    the tree was never updated, so the documented layout was missing the
    whole translation subsystem. Nothing else in the suite looks at it, so
    it could only ever be caught by someone noticing.

    Deliberately one-directional and shallow. It asserts that every
    top-level module in `src/powerpetdoor/` is MENTIONED, not that the tree
    is formatted a particular way or that every file in every subpackage is
    listed - `simulator/` is summarised on purpose and pinning its contents
    would make the doc fight every refactor. Prose-shaped assertions were
    removed from this suite once already for exactly that reason.
    """

    #: Not modules a reader needs pointed out.
    IGNORED = {"__pycache__"}

    def test_every_top_level_module_appears_in_the_tree(self):
        source = REPO_ROOT / "src" / "powerpetdoor"
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        match = re.search(r"## Library Structure\s*\n+```(.*?)```", readme, re.S)
        assert match, "README.md no longer has a fenced 'Library Structure' tree"
        tree = match.group(1)

        expected = sorted(
            entry.name
            for entry in source.iterdir()
            if entry.name not in self.IGNORED
            and (entry.suffix == ".py" or entry.is_dir() or entry.name == "py.typed")
        )
        missing = [name for name in expected if name not in tree]
        assert not missing, (
            "README.md's Library Structure tree does not mention: "
            f"{', '.join(missing)}. A reader looking for these would conclude "
            "they do not exist."
        )

    def test_the_tree_does_not_list_modules_that_are_gone(self):
        """The other direction: a removed module still documented."""
        source = REPO_ROOT / "src" / "powerpetdoor"
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"## Library Structure\s*\n+```(.*?)```", readme, re.S)
        assert match
        tree = match.group(1)

        # Only top-level entries of the tree, i.e. lines whose box-drawing
        # prefix has no leading indentation - nested lines describe
        # simulator/ internals, which this test deliberately does not police.
        listed = re.findall(r"^[├└]── ([\w.]+)", tree, re.M)
        actual = {entry.name for entry in source.iterdir()}
        stale = [name for name in listed if name.rstrip("/") not in actual]
        assert not stale, (
            f"README.md's Library Structure tree still lists: {', '.join(stale)}, "
            "which no longer exist in src/powerpetdoor/."
        )


# ============================================================================
# The reference docs are complete
# ============================================================================


class TestEveryOperatorSurfaceIsDocumented:
    """Adding a command and forgetting to document it should fail.

    These tables are deliberately *not* generated. The `inside` row
    explains pulse-versus-hold and that a held sensor is pet presence;
    the `obstruction` row explains why it toggles where the sensors
    pulse. The code's one-line description carries none of that, so
    generating the tables would delete the teaching, and moving
    paragraphs of markdown into Python string literals to avoid that
    would be worse than the duplication.

    What is worth enforcing is completeness in both directions - which
    is the mistake actually made: `trigger` existed in the script DSL
    and the control socket for a while before the prompt had it, and
    nothing said so.
    """

    #: Prompt words that are the session itself. Documented under their own
    #: headings rather than in a command table.
    SESSION_WORDS = frozenset({"exit", "help", "clear", "debug", "history"})

    @staticmethod
    def _cli_words() -> set[str]:
        import powerpetdoor.simulator.commands.handler  # noqa: F401
        from powerpetdoor.simulator.commands.base import get_command_registry

        return {info.name for info in get_command_registry().values()}

    def test_every_prompt_command_appears_in_simulator_md(self):
        doc = (REPO_ROOT / "docs" / "simulator.md").read_text(encoding="utf-8")
        missing = sorted(
            word
            for word in self._cli_words()
            if word not in self.SESSION_WORDS and f"`{word}" not in doc
        )
        assert missing == [], (
            f"docs/simulator.md does not mention: {', '.join(missing)}. "
            "A reader looking for these would conclude they do not exist."
        )

    def test_every_script_action_appears_in_scripting_md(self):
        from powerpetdoor.simulator.scripting import _ACTION_PARAMS

        doc = (REPO_ROOT / "docs" / "scripting.md").read_text(encoding="utf-8")
        missing = sorted(action for action in _ACTION_PARAMS if f"**{action}**" not in doc)
        assert missing == [], f"docs/scripting.md has no section for: {', '.join(missing)}"

    def test_scripting_md_documents_nothing_that_was_removed(self):
        """The other direction: a deleted action still documented.

        The DSL's own module docstring carried a list that named
        `pet_presence` long after it was gone, which is how a reader
        ends up writing a script against an action that does not exist.
        """
        import re

        from powerpetdoor.simulator.scripting import _ACTION_PARAMS

        doc = (REPO_ROOT / "docs" / "scripting.md").read_text(encoding="utf-8")
        documented = set(re.findall(r"^\*\*([a-z_]+)\*\*", doc, re.M))
        assert documented <= set(_ACTION_PARAMS), sorted(documented - set(_ACTION_PARAMS))

    def test_every_writable_value_is_reachable_from_the_documented_set_command(self):
        """`set` documents the registry generically, so the list must be there."""
        from powerpetdoor.simulator.values import WRITABLE

        doc = (REPO_ROOT / "docs" / "scripting.md").read_text(encoding="utf-8")
        assert "**set**" in doc
        # A spot-check across the kinds, not all 34 - the schema carries the
        # exhaustive list, and `get`/`set` reach the registry by name.
        for name in ("power", "hold_time", "timezone", "rise_time"):
            assert name in WRITABLE
            assert name in doc, name
