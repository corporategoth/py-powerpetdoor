# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Documentation *correctness* pins.

``tests/test_exports.py`` proves every exported name is mentioned somewhere
in the prose. Round 6 found five defects that a presence check cannot see -
a wrong keepalive example, index links pointing at sections that document
something else, a self-contradicting priority table, a listener type that
omitted ``None``, and a push frame classified as an envelope key. Presence
is not accuracy, so these tests check what the docs *claim* against what the
code actually does, by introspection and by execution.

The keepalive pin matters most: ``docs/protocol.md`` is the only place the
frame is specified, this project has three independent implementations built
from it, and the wrong example produces a hard disconnect every ~90 s that
looks like a flaky network (round-6 frontend M1).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

from powerpetdoor import client as client_module
from powerpetdoor.client import PowerPetDoorClient
from powerpetdoor.const import (
    COMMAND_PRIORITIES,
    DOOR_STATUS,
    FIELD_CMD,
    FIELD_DOOR_STATUS,
    FIELD_SUCCESS,
    PING,
    PONG,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
)
from powerpetdoor.simulator import cli as cli_module
from powerpetdoor.simulator.commands import history as history_module
from powerpetdoor.simulator.protocol import DoorSimulatorProtocol
from powerpetdoor.simulator.state import DoorSimulatorState

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_MD = REPO_ROOT / "docs" / "protocol.md"
CLIENT_MD = REPO_ROOT / "docs" / "client.md"
SIMULATOR_MD = REPO_ROOT / "docs" / "simulator.md"


# ============================================================================
# Markdown helpers
# ============================================================================


def _github_anchor(heading: str) -> str:
    """Render a heading the way GitHub renders its own anchor.

    Each space becomes a hyphen; runs are *not* collapsed. GitHub strips
    punctuation first and hyphenates what is left character by character,
    so ``## Battery & Hardware`` is ``#battery--hardware`` - which is what
    ``docs/door.md`` links to, and what collapsing the run got wrong.
    """
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s", "-", slug)


def _sections(path: Path) -> dict[str, str]:
    """Map anchor -> body text for every heading in a markdown file.

    A section owns its subsections: ``## Message Format`` includes the
    ``### Request Format`` block under it, which is what a reader following
    the link actually sees.

    Fenced blocks are skipped when looking for headings: ``# Door protocol
    on 3000`` inside a ```` ```bash ```` block is a shell comment, and
    treating it as a heading truncated the Daemon Mode section six lines
    in - which is why nothing had ever pinned the prose underneath it.
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
            body = sections.setdefault(_github_anchor(heading), [])
            open_sections.append((level, body))
            continue
        for _, body in open_sections:
            body.append(line)
    return {anchor: "\n".join(body) for anchor, body in sections.items()}


def _cell_token(cell: str) -> str:
    """A table cell holding a single quoted/backticked token, unwrapped."""
    return cell.strip().strip("`").strip('"')


def _json_blocks(text: str) -> list[dict]:
    """Every fenced ```json block in ``text``, parsed.

    Blocks that hold more than one object (the docs use that for
    alternatives) contribute each object separately.
    """
    blocks: list[dict] = []
    for body in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
        # A five-line loop here bound a local, broke, and discarded it; no
        # branch below ever read it, so it could not influence a single
        # extracted block. Inert code in the helper that feeds the
        # keepalive pins reads like a check that is happening and is not
        # (round-8 test-fanatic L5).
        try:
            blocks.append(json.loads(body))
            continue
        except json.JSONDecodeError:
            pass
        for line in body.strip().splitlines():
            if line.strip().startswith("{"):
                blocks.append(json.loads(line.strip()))
    return blocks


def _condition_names(body: str) -> set[str]:
    """Every condition name a `condition ==` / `condition in (...)` tests.

    Extracted with `ast`, not a regex. The `wait_for` extractor knew only
    the `==` spelling while its sibling knew both, so adding a genuinely
    working condition to `_check_condition` with `condition in ("a", "b")`
    - the spelling the same file already uses for its *values* - left it
    live, undocumented, and invisible to the test (round-8 test-fanatic
    L1). Walking `Compare` nodes covers `==`, `in (...)` and `in {...}` at
    once, so the two extractors cannot diverge again.
    """
    # The caller's split leaves a method fragment that can trail a
    # decorator belonging to the *next* method, which will not parse. Keep
    # the signature line plus everything indented under it.
    lines = ("def _fragment" + body).splitlines()
    kept = [lines[0]]
    for line in lines[1:]:
        if line.strip() and not line.startswith("        "):
            break
        kept.append(line)
    module = ast.parse(textwrap.dedent("\n".join(kept)))
    names: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "condition"):
            continue
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(operator, ast.Eq) and isinstance(comparator, ast.Constant):
                names.add(comparator.value)
            elif isinstance(operator, ast.In) and isinstance(
                comparator, ast.Tuple | ast.List | ast.Set
            ):
                names.update(
                    element.value
                    for element in comparator.elts
                    if isinstance(element, ast.Constant)
                )
    return names


def _table_rows(text: str) -> list[list[str]]:
    """Every markdown table row in ``text``, as stripped cell lists.

    ``\\|`` inside a cell is a literal pipe (the listener table needs it
    for ``bool | None``), so it must not split the row.
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|- :"):
            continue
        cells = [
            cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip("|"))
        ]
        rows.append(cells)
    return rows


# ============================================================================
# Keepalive (frontend M1)
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

            def get_write_buffer_size(self):
                return 0

            def pause_reading(self):
                pass

            def resume_reading(self):
                pass

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
# Message-type table (frontend L7)
# ============================================================================


def test_message_types_table_lists_only_real_envelope_keys():
    """`DOOR_STATUS` is a CMD value, not an envelope key (frontend L7)."""
    rows = _table_rows(_sections(PROTOCOL_MD)["message-types"])
    fields = {_cell_token(row[1]) for row in rows if len(row) >= 2 and row[0] != "Type"}

    assert fields == {"cmd", "config", PING, PONG}
    assert DOOR_STATUS not in fields


def test_the_unsolicited_door_status_frame_is_documented_and_real():
    """The one push frame a client must handle unasked is specified."""
    section = _sections(PROTOCOL_MD)["unsolicited-door-status"]
    blocks = _json_blocks(section)

    assert len(blocks) == 1
    frame = blocks[0]
    assert frame[FIELD_CMD] == DOOR_STATUS
    assert FIELD_DOOR_STATUS in frame
    # And the client really does dispatch that CMD value.
    from powerpetdoor.client import ResponseHandlerRegistry

    assert ResponseHandlerRegistry.get(DOOR_STATUS) is not None


# ============================================================================
# Constant index links (frontend L4)
# ============================================================================


def test_constant_index_links_point_at_sections_that_document_them():
    """Every index row's target must actually mention the wire names.

    Two rows landed on sections that document something else entirely -
    which reads as "this constant is undocumented", and is worse than a
    dead link because nothing 404s (round-6 frontend L4).
    """
    protocol_sections = _sections(PROTOCOL_MD)
    index = _sections(CLIENT_MD)["envelope-field-and-state-constants"]
    checked = 0

    for row in _table_rows(index):
        if len(row) < 3 or row[0] == "Group":
            continue
        constants = [name.strip("`") for name in re.findall(r"`([A-Z_][A-Z0-9_]*)`", row[1])]
        anchor_match = re.search(r"\]\(protocol\.md#([\w-]+)\)", row[2])
        assert anchor_match, f"index row {row[0]!r} has no protocol.md anchor"
        anchor = anchor_match.group(1)
        assert anchor in protocol_sections, f"index row {row[0]!r} links to a missing #{anchor}"

        body = protocol_sections[anchor]
        for name in constants:
            value = getattr(__import__("powerpetdoor.const", fromlist=["x"]), name)
            assert value in body, (
                f"{name} ({value!r}) is not documented in protocol.md#{anchor}, "
                f"which is where docs/client.md sends the reader"
            )
            checked += 1

    assert checked > 20, "the index sweep stopped finding constants; the parser drifted"


# ============================================================================
# Priority table (frontend L5)
# ============================================================================


def test_priority_table_values_match_the_constants():
    rows = _table_rows(_sections(CLIENT_MD)["message-priority"])
    documented = {
        row[0].strip("`"): int(row[1]) for row in rows if len(row) >= 2 and row[1].isdigit()
    }

    assert documented == {
        "PRIORITY_CRITICAL": PRIORITY_CRITICAL,
        "PRIORITY_HIGH": PRIORITY_HIGH,
        "PRIORITY_MEDIUM": PRIORITY_MEDIUM,
        "PRIORITY_LOW": PRIORITY_LOW,
    }


def test_priority_table_rows_do_not_contradict_the_map():
    """Every command a row names must really have that row's priority.

    The MEDIUM row said "SET_*" and the LOW row said "schedule commands",
    so the table gave two answers for SET_SCHEDULE (round-6 frontend L5).
    """
    rows = _table_rows(_sections(CLIENT_MD)["message-priority"])
    named_anywhere = 0

    for row in rows:
        if len(row) < 3 or not row[1].isdigit():
            continue
        priority = int(row[1])
        for command in re.findall(r"`([A-Z][A-Z0-9_]*)`", row[2]):
            assert command in COMMAND_PRIORITIES, f"{command} is not a priority-mapped command"
            assert COMMAND_PRIORITIES[command] == priority, (
                f"docs list {command} under priority {priority}, "
                f"but it is {COMMAND_PRIORITIES[command]}"
            )
            named_anywhere += 1

    assert named_anywhere >= 8


def test_the_default_priority_is_documented():
    """A caller with a hand-rolled command name needs the fallback rule."""
    body = _sections(CLIENT_MD)["message-priority"]

    assert "COMMAND_PRIORITIES.get(arg, PRIORITY_LOW)" in body
    assert COMMAND_PRIORITIES.get("NOT_A_REAL_COMMAND", PRIORITY_LOW) == PRIORITY_LOW


# ============================================================================
# Listener table (frontend L6)
# ============================================================================


def test_listener_table_matches_add_listener_by_introspection():
    """The documented listener set and its value types come from the code.

    ``sensor_update``/``notifications_update`` were documented as
    ``val: bool`` while the real annotation is ``bool | None``, and a
    consumer writing ``if val:`` then maps "unparseable" onto False - the
    wrong way to fail for a safety lock (round-6 frontend L6).
    """
    rows = _table_rows(_sections(CLIENT_MD)["available-listener-types"])
    documented = {
        row[0].strip("`"): row[1] for row in rows if len(row) >= 2 and row[0].startswith("`")
    }

    signature = inspect.signature(PowerPetDoorClient.add_listener)
    real = {
        name
        for name in signature.parameters
        if name not in ("self", "name") and signature.parameters[name].default is None
    }

    assert set(documented) == real

    # The two dict-valued listeners really do hand out `bool | None`.
    annotations = inspect.get_annotations(PowerPetDoorClient.add_listener, eval_str=True)
    for listener in ("sensor_update", "notifications_update"):
        assert "bool | None" in documented[listener], (
            f"docs must say {listener} can deliver None; the annotation is {annotations[listener]}"
        )
    # ... which is exactly what make_bool returns for an unknown spelling.
    assert client_module.make_bool("banana") is None


def test_hold_time_units_agree_between_the_docstring_and_the_prose():
    """The source docstring said seconds; it is centiseconds (L6b).

    The IDE tooltip / ``help()`` view is the one that wins in practice, so
    it was the wrong one that a caller saw.
    """
    docstring = inspect.getdoc(PowerPetDoorClient.add_listener) or ""
    hold_time_line = next(line for line in docstring.splitlines() if "hold_time_update:" in line)

    assert "centisecond" in hold_time_line
    rows = _table_rows(_sections(CLIENT_MD)["available-listener-types"])
    prose = next(row[2] for row in rows if row[0] == "`hold_time_update`")
    assert "centiseconds" in prose


# ============================================================================
# docs/simulator.md defaults (round-7 test-fanatic L2)
# ============================================================================


def test_the_documented_control_port_offset_is_the_constant():
    """`--daemon` advertises "door port + 1"; nothing executable said so.

    `CONTROL_PORT_OFFSET` was defined and then *inlined* at the use site,
    so the name had zero readers - a DRY-rule violation by this project's
    own CLAUDE.md, and a mutation of `1` -> `2` that the whole suite could
    not see. The constant is used now; this pins the prose to it.
    """
    row = next(
        row for row in _table_rows(_sections(SIMULATOR_MD)["options"]) if "`--daemon`" in row[0]
    )

    assert f"door port + {cli_module.CONTROL_PORT_OFFSET}" in row[1]


def test_the_daemon_control_port_really_is_the_documented_offset(monkeypatch):
    """...and the prose is checked against the argument parser, by execution."""
    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_module, "run_simulator", fake_run)
    monkeypatch.setattr(sys, "argv", ["ppd-simulator", "--port", "4321", "--daemon"])

    cli_module.main()

    assert captured["control_port"] == 4321 + cli_module.CONTROL_PORT_OFFSET


def test_the_documented_history_default_is_the_constant():
    """`history [N|clear]` advertises "default 20"; nothing pinned it."""
    row = next(row for row in _table_rows(_sections(SIMULATOR_MD)["info"]) if "`history" in row[0])

    assert f"default {history_module.DEFAULT_HISTORY_LIMIT}" in row[2]


# ============================================================================
# pytest configuration pins (round-7 test-fanatic M1)
# ============================================================================


def test_the_deadlock_backstop_uses_the_thread_method():
    """`timeout = 60` alone does not cover `tests/fuzz/`.

    pytest-timeout's default "signal" method raises inside the running test
    frame; hypothesis catches whatever the body raises and re-invokes it to
    shrink, but the one-shot SIGALRM is spent, so every shrink attempt
    re-enters the hang with no alarm armed and the process spins forever.
    Measured with an infinite-loop mutation in `FrameScanner.feed`: a plain
    test failed at 60 s, the *identical* hang inside `@given` produced zero
    output in 150 s - under the project's real `-n auto` addopts as well as
    `-n0` - and `timeout_method = "thread"` failed it at 60 s naming the
    hanging line.

    Nothing else in the suite can observe this setting, so it is asserted
    here rather than left to a comment.
    """
    import tomllib

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    options = config["tool"]["pytest"]["ini_options"]

    assert options["timeout"] == 60
    assert options["timeout_method"] == "thread"


# ============================================================================
# docs/simulator.md script DSL (round-7 frontend L1 / L2 / L4)
# ============================================================================


def _yaml_blocks(text: str) -> list[str]:
    """Every ```yaml fenced block in a markdown section."""
    return re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)


async def test_the_script_format_example_actually_passes():
    """The first thing a script author copies exited 1.

    `trigger sensor` -> `wait 2` -> `assert door_status DOOR_CLOSED` is
    deterministically wrong: the default `hold_time` is 2.0 s and the hold
    timer only starts once the door *reaches* `DOOR_HOLDING`, so two
    seconds is never enough (round-7 frontend L2). Running the block is the
    only check that cannot drift.
    """
    from powerpetdoor.simulator import DoorSimulator
    from powerpetdoor.simulator.scripting import Script, ScriptRunner

    block = _yaml_blocks(_sections(SIMULATOR_MD)["script-format"])[0]
    simulator = DoorSimulator()
    try:
        assert await ScriptRunner(simulator).run(Script.from_yaml(block)) is True
    finally:
        await simulator.stop()


async def test_the_from_simple_commands_example_actually_passes():
    """Its programmatic twin, one section earlier, had the same defect."""
    from powerpetdoor.simulator import DoorSimulator
    from powerpetdoor.simulator.scripting import Script, ScriptRunner

    section = _sections(SIMULATOR_MD)["running-scripts-programmatically"]
    listed = re.search(r"from_simple_commands\(\[(.*?)\]", section, re.DOTALL)
    assert listed is not None
    commands = re.findall(r'"([^"]+)"', listed.group(1))
    assert commands  # the example must still be there

    simulator = DoorSimulator()
    try:
        script = Script.from_simple_commands(commands, name="Doc Example")
        assert await ScriptRunner(simulator).run(script) is True
    finally:
        await simulator.stop()


def test_the_wait_for_condition_table_matches_the_implementation():
    """The table was introduced as applying to `wait_for` *and* `assert`.

    All 19 of its rows fail with `assert`, whose accepting set is disjoint
    (round-7 frontend L1). `door_closing` was implemented and absent from
    the table (round-7 frontend L4).
    """
    from powerpetdoor.simulator import scripting

    rows = _table_rows(_sections(SIMULATOR_MD)["conditions-for-wait_for"])
    documented = {row[0].strip("`") for row in rows if row[0] != "Condition"}
    source = Path(scripting.__file__).read_text()
    body = source.split("def _check_condition", 1)[1].split("\n    def ", 1)[0]
    implemented = _condition_names(body)
    implemented |= set(scripting._STATUS_WAIT_CONDITIONS)

    assert documented == implemented


def test_the_assert_condition_table_matches_the_implementation():
    from powerpetdoor.simulator import scripting

    rows = _table_rows(_sections(SIMULATOR_MD)["conditions-for-assert"])
    documented = {row[0].strip("`") for row in rows if row[0] != "Condition"}
    source = Path(scripting.__file__).read_text()
    body = source.split("def _assert_condition", 1)[1].split("\n    def ", 1)[0]
    implemented = _condition_names(body)

    assert documented == implemented


def test_the_two_condition_tables_are_disjoint():
    """Which is why "you can *also* check these" was the wrong word."""
    wait_for = {
        row[0].strip("`")
        for row in _table_rows(_sections(SIMULATOR_MD)["conditions-for-wait_for"])
        if row[0] != "Condition"
    }
    asserts = {
        row[0].strip("`")
        for row in _table_rows(_sections(SIMULATOR_MD)["conditions-for-assert"])
        if row[0] != "Condition"
    }

    assert wait_for & asserts == set()


def test_every_script_action_is_documented():
    """`inside`/`outside` were implemented, *depended on* by the "Numeric
    bounds" note, and introduced nowhere (round-7 frontend L4)."""
    from powerpetdoor.simulator import scripting

    section = _sections(SIMULATOR_MD)["available-actions"]
    documented = set(re.findall(r"^\*\*([a-z_]+)\*\*", section, re.MULTILINE))
    documented |= set(re.findall(r"\*\* / \*\*([a-z_]+)\*\*", section))

    # Both directions. One-directional, the docs could invent an action
    # that does not exist - a reader copies it, the script fails with
    # "Unknown action", and nothing in CI noticed the doc was wrong
    # (round-8 test-fanatic L2). Verified equal on the current tree, so
    # tightening it is free.
    assert documented == set(scripting._ACTION_PARAMS)


def _setting_names(function_name: str) -> set[str]:
    """The setting names one of `scripting`'s `name == "..."` chains accepts."""
    from powerpetdoor.simulator import scripting

    source = Path(scripting.__file__).read_text()
    body = source.split(f"def {function_name}", 1)[1].split("\n    def ", 1)[0]
    return set(re.findall(r'name == "([a-z_]+)"', body))


def test_the_settings_table_matches_what_set_accepts():
    """The table said "Settings that can be used with `set` **and** `toggle`".

    Two of its nine rows failed with `toggle` - the two non-boolean ones,
    which the table's own `Type` column already identified, and which the
    `**toggle**` action entry two sections up already described correctly
    as "Toggle a boolean setting". Round-7 frontend L1 one screen away, and
    none of the three docs-accuracy tests round 7 added covered this table
    (round-8 frontend L2).
    """
    rows = _table_rows(_sections(SIMULATOR_MD)["settings-for-set"])
    documented = {row[0].strip("`") for row in rows if row[0] != "Setting"}

    assert documented == _setting_names("_set_value")


def test_the_settings_table_marks_exactly_the_rows_toggle_accepts():
    """`toggle` takes the boolean rows; the doc must name that set, not the table's."""
    rows = [
        row
        for row in _table_rows(_sections(SIMULATOR_MD)["settings-for-set"])
        if row[0] != "Setting"
    ]
    boolean_rows = {row[0].strip("`") for row in rows if row[1] == "boolean"}
    other_rows = {row[0].strip("`") for row in rows if row[1] != "boolean"}
    prose = _sections(SIMULATOR_MD)["settings-for-set"]

    assert boolean_rows == _setting_names("_toggle_value")
    assert other_rows == {"hold_time", "battery"}
    # The prose names both excluded rows and the error they really produce.
    for name in other_rows:
        assert f"`{name}`" in prose
    assert "Unknown setting to toggle: hold_time" in prose


def test_the_daemon_mode_prose_quotes_the_real_out_of_directory_refusal():
    """Round 7 fixed the message and left the doc that quotes it verbatim.

    The paragraph is the *control-channel* documentation and the sentence
    it quoted was the local-CLI variant, so round-7 frontend L6's
    contradiction moved from the product into the manual (round-8 frontend
    L1). Both policies are pinned, because the doc now states both.
    """
    from powerpetdoor.simulator import scripting

    # Markdown reflows, so the quoted sentence can wrap mid-phrase.
    prose = " ".join(_sections(SIMULATOR_MD)["daemon-mode"].split())
    previous = scripting._script_paths_allowed
    try:
        scripting.set_script_paths_allowed(False)
        assert scripting.describe_out_of_directory_remedy() in prose
        scripting.set_script_paths_allowed(True)
        assert scripting.describe_out_of_directory_remedy() in prose
    finally:
        scripting.set_script_paths_allowed(previous)


def test_the_daemon_mode_prose_describes_the_real_shadow_marker(tmp_path, monkeypatch):
    """`(shadowed by <dir>/<name>)` dropped the suffix; the code stopped emitting it."""
    from powerpetdoor.simulator import scripting

    builtin = tmp_path / "builtin"
    extra = tmp_path / "extra"
    builtin.mkdir()
    extra.mkdir()
    (builtin / "basic_cycle.yaml").write_text("name: B\nsteps:\n  - close\n")
    shadowing = extra / "basic_cycle.yml"
    shadowing.write_text("name: S\nsteps:\n  - close\n")
    monkeypatch.setattr(scripting, "SCRIPTS_DIR", builtin)
    scripting.set_extra_scripts_dir(extra)

    lines = scripting.render_script_listing(str(extra)).lines

    marker = f"(shadowed by {shadowing})"
    assert any(marker in line for line in lines)
    # The doc must not promise the reconstructed, suffix-less form.
    prose = " ".join(_sections(SIMULATOR_MD)["daemon-mode"].split())
    assert "(shadowed by <dir>/<name>)" not in prose
    assert "(shadowed by <path-to-the-file>)" in prose
    assert "`--list-scripts`" in prose


def test_the_changelog_records_the_breaking_script_dsl_changes():
    """Round 7's fixes broke existing user scripts and the changelog missed it.

    `CHANGELOG.md` opens with "All notable changes to this project will be
    documented in this file" and carries a detailed `[Unreleased]` section,
    so the omission was a break with the project's own practice: a user
    whose script suite started exiting 1 had the reference manual to read
    *after* working out what happened, and nothing to say it was coming
    (round-8 frontend L3). `.github/workflows/test.yml` now fails a PR that
    touches `src/` without touching the changelog; this pins the content.
    """
    from powerpetdoor.simulator import scripting

    changelog = " ".join((REPO_ROOT / "CHANGELOG.md").read_text().split())

    assert "#### Breaking (simulator script DSL)" in changelog
    # Both breaking errors, and a remedy for each.
    assert "unrecognised step parameter now fails the step" in changelog
    assert "unrecognised `sensor:` name now fails the step" in changelog
    for annotation in sorted(scripting.STEP_ANNOTATION_KEYS):
        assert f"`{annotation}:`" in changelog
    assert "*Remedy:* use `inside` or `outside`" in changelog


@pytest.mark.parametrize(
    "path",
    [PROTOCOL_MD, CLIENT_MD, SIMULATOR_MD, REPO_ROOT / "docs" / "door.md"],
    ids=lambda path: path.name,
)
def test_every_same_file_anchor_link_resolves(path):
    """A renamed heading must not leave the table of contents pointing at prose.

    Round 6 found index links resolving to sections that documented
    something else, and `docs/simulator.md` carried *two* `### Settings`
    headings, so `#settings` silently resolved to the interactive-mode one
    while the scripting table of contents meant the other. Renaming one of
    them is exactly when this breaks, and nothing checked.
    """
    text = path.read_text()
    anchors = set(_sections(path))
    # GitHub disambiguates a repeated heading as `#name-1`, `#name-2`, ...
    seen: dict[str, int] = {}
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if line.startswith("#") and not fenced:
            base = _github_anchor(line.lstrip("#"))
            count = seen.get(base, 0)
            seen[base] = count + 1
            if count:
                anchors.add(f"{base}-{count}")

    broken = [target for target in re.findall(r"\]\(#([\w-]+)\)", text) if target not in anchors]

    assert broken == []
