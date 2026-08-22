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

import asyncio
import inspect
import json
import re
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
from powerpetdoor.simulator.protocol import DoorSimulatorProtocol
from powerpetdoor.simulator.state import DoorSimulatorState

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_MD = REPO_ROOT / "docs" / "protocol.md"
CLIENT_MD = REPO_ROOT / "docs" / "client.md"


# ============================================================================
# Markdown helpers
# ============================================================================


def _github_anchor(heading: str) -> str:
    """Render a heading the way GitHub renders its own anchor."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s+", "-", slug)


def _sections(path: Path) -> dict[str, str]:
    """Map anchor -> body text for every heading in a markdown file.

    A section owns its subsections: ``## Message Format`` includes the
    ``### Request Format`` block under it, which is what a reader following
    the link actually sees.
    """
    lines = path.read_text().splitlines()
    sections: dict[str, list[str]] = {}
    open_sections: list[tuple[int, list[str]]] = []
    for line in lines:
        if line.startswith("#"):
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
        for line in body.strip().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                # Part of a pretty-printed multi-line object; handled below.
                break
        try:
            blocks.append(json.loads(body))
            continue
        except json.JSONDecodeError:
            pass
        for line in body.strip().splitlines():
            if line.strip().startswith("{"):
                blocks.append(json.loads(line.strip()))
    return blocks


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
