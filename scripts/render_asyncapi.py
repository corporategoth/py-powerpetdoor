#!/usr/bin/env python3
# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Render `schemas/asyncapi.json` as browsable wiki pages.

The AsyncAPI document is the protocol's machine-readable description and
is generated from the tables the runtime reads - but a reader who wants
to know what `SET_HOLD_TIME` takes should not have to open 400 KB of
JSON, and the wiki previously offered them nothing but the file's name.

So this renders it: one index carrying every command, and a page per
area of the protocol carrying the detail. Nothing here is authored.
Every description, constraint, unit and example comes out of the
document, which comes out of the code - so a command added to the
simulator appears on the wiki without anyone remembering to write it up.

The division of labour with ``docs/protocol.md`` is deliberate and both
halves are needed. This is *reference*: what the fields are, what they
accept, what a real exchange looks like. That is prose: why the door
behaves as it does, which commands lie about succeeding, what the vendor
app gets wrong. Reference generated from code cannot hold a finding, and
prose cannot be regenerated. They link to each other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "schemas" / "asyncapi.json"

#: The index page, and the page every detail page links back to.
INDEX_PAGE = "Protocol-Reference"

#: Category -> (wiki page, heading, one-line blurb for the index).
#:
#: A partition, not a tagging: every command lands on exactly one page,
#: because a reader following a link needs one place to arrive. The
#: cross-cutting relations (`also-in-GET_SETTINGS`) stay as tags on the
#: command itself.
CATEGORIES: tuple[tuple[str, str, str, str], ...] = (
    (
        "motion",
        "Protocol-Door-Motion",
        "Door motion",
        "Moving the flap, and the status the door pushes while it moves",
    ),
    (
        "settings",
        "Protocol-Settings",
        "Settings",
        "The switches and values that configure the door",
    ),
    (
        "information",
        "Protocol-Information",
        "Information",
        "Reading the door's state, hardware and statistics",
    ),
    ("schedules", "Protocol-Schedules", "Schedules", "Reading and writing the schedule slots"),
    (
        "notifications",
        "Protocol-Notifications",
        "Notifications",
        "Which events the door announces",
    ),
)

#: The unsolicited push. Not a command, so it has no request half and no
#: operation - but it is the message a client is most likely to meet
#: unprepared, so it is documented with the commands that cause it.
PUSH_MESSAGE = "doorStatus"


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def categorise(name: str, message: dict[str, Any]) -> str:
    """Which page a command belongs on.

    Reads the tags the generator already assigns, so a new command files
    itself. The order matters: `GET_SCHEDULE_LIST` is a read *and* a
    schedule, and it belongs with the schedules.
    """
    tags = {tag["name"] for tag in message.get("tags", ())}
    if "door-motion" in tags:
        return "motion"
    if "schedule" in tags:
        return "schedules"
    if "notification" in tags:
        return "notifications"
    if "read" in tags:
        return "information"
    return "settings"


def commands_by_category(spec: dict[str, Any]) -> dict[str, list[str]]:
    """Every command name, grouped, each group sorted."""
    grouped: dict[str, list[str]] = {key: [] for key, _, _, _ in CATEGORIES}
    for name, message in spec["components"]["messages"].items():
        if name.endswith(".reply") or name == PUSH_MESSAGE:
            continue
        grouped[categorise(name, message)].append(name)
    return {key: sorted(names) for key, names in grouped.items()}


def anchor(command: str) -> str:
    """The heading anchor a wiki generates for a command's section."""
    return command.lower()


def _one_line(text: str) -> str:
    """Markdown table cells cannot hold a newline, and pipes end them."""
    return " ".join(text.split()).replace("|", "\\|")


def type_cell(schema: dict[str, Any]) -> str:
    """A field's type, with whatever actually constrains it.

    The unit belongs here rather than only in the prose: a reader
    scanning a table for `holdTime` needs to learn it is centiseconds in
    the same glance that tells them it is an integer, or they will send
    seconds.
    """
    if "const" in schema:
        return f"`{json.dumps(schema['const'])}`"
    if "enum" in schema:
        return " \\| ".join(f"`{json.dumps(v)}`" for v in schema["enum"])

    kind = schema.get("type", "any")
    if kind == "array":
        item = schema.get("items", {}).get("type")
        kind = f"array of {item}" if item else "array"
        # A fixed-length array is a different thing from a list, and
        # `daysOfWeek` is meaningless without knowing it is exactly 7.
        low, high = schema.get("minItems"), schema.get("maxItems")
        if low is not None and low == high:
            kind = f"{kind}, exactly {low}"

    parts = [kind]
    if (unit := schema.get("x-unit")) is not None:
        parts.append(f"({unit})")
    low, high = schema.get("minimum"), schema.get("maximum")
    if low is not None and high is not None:
        parts.append(f"{low}–{high}")
    elif low is not None:
        parts.append(f"≥ {low}")
    elif high is not None:
        parts.append(f"≤ {high}")
    return " ".join(parts)


def _rows(properties: dict[str, Any], required: set[str], prefix: str = "") -> list[str]:
    """Table rows for a payload's fields, nested members included.

    An object's members are rendered in the same table under a dotted
    name rather than in a table of their own. `settings` has eleven and
    `schedule` has nine; a reader comparing them wants one table, and a
    sub-table per object would put the door's most important structures
    behind an extra hop.
    """
    rows: list[str] = []
    for field, schema in properties.items():
        name = f"{prefix}{field}"
        # Required *within its parent*, the way JSON Schema means it: a
        # `hour` marked yes is mandatory whenever the time object it sits
        # in is present, not mandatory in every message.
        mark = "yes" if field in required else "—"
        description = _one_line(schema.get("description", ""))
        rows.append(f"| `{name}` | {type_cell(schema)} | {mark} | {description} |")
        if members := schema.get("properties"):
            rows += _rows(members, set(schema.get("required") or ()), f"{name}.")
        # An array's element carries its own bounds and description, and
        # dropping them is how `schedules` came to read as a list of
        # anything. Shown as `field[]`, one row, the way a member is.
        item = schema.get("items") or {}
        if item.get("description") or item.get("properties"):
            rows += _rows({"[]": item}, set(), name)
    return rows


def field_table(payload: dict[str, Any]) -> list[str]:
    """The payload's fields, in the order the document lists them."""
    properties = payload.get("properties") or {}
    if not properties:
        return []
    return [
        "| Field | Type | Required | Description |",
        "| --- | --- | --- | --- |",
        *_rows(properties, set(payload.get("required") or ())),
    ]


def _example(message: dict[str, Any]) -> list[str]:
    examples = message.get("examples") or []
    if not examples or examples[0].get("payload") is None:
        return []
    body = json.dumps(examples[0]["payload"], indent=2)
    return ["```json", body, "```"]


def render_message(message: dict[str, Any], heading: str) -> list[str]:
    """One half of an exchange: its fields, then a real one."""
    lines = [f"**{heading}**", ""]
    lines += field_table(message["payload"])
    example = _example(message)
    if example:
        lines += ["", *example]
    return lines


def render_command(command: str, spec: dict[str, Any]) -> list[str]:
    """A command's whole section: what it does, what it takes, what it answers."""
    messages = spec["components"]["messages"]
    message = messages[command]

    lines = [f"### {command}", ""]
    if description := message.get("description"):
        lines += [description, ""]
    if tags := [tag["name"] for tag in message.get("tags", ())]:
        lines += ["Tags: " + ", ".join(f"`{tag}`" for tag in tags), ""]

    lines += render_message(message, "Request")

    reply = messages.get(f"{command}.reply")
    if reply is not None:
        lines += ["", *render_message(reply, "Reply")]
    else:
        # A command with no reply half in the document is one the door
        # answers with nothing but the envelope - saying so beats an
        # empty heading a reader has to interpret.
        lines += ["", "The door answers with the envelope only."]
    return lines


def render_page(key: str, spec: dict[str, Any]) -> str:
    """One category's page."""
    _, _, heading, blurb = next(c for c in CATEGORIES if c[0] == key)
    commands = commands_by_category(spec)[key]

    lines = [
        f"# {heading}",
        "",
        blurb + ".",
        "",
        f"Generated from `schemas/asyncapi.json`. See **[{INDEX_PAGE}]({INDEX_PAGE})** for",
        "every command, and **[Protocol](Protocol)** for what a real door does",
        "with them.",
        "",
    ]
    for command in commands:
        lines += ["---", "", *render_command(command, spec), ""]

    if key == "motion":
        lines += ["---", "", *render_push(spec), ""]
    return "\n".join(lines).rstrip() + "\n"


def render_push(spec: dict[str, Any]) -> list[str]:
    """The unsolicited `DOOR_STATUS`, which no request produces."""
    message = spec["components"]["messages"][PUSH_MESSAGE]
    lines = ["### DOOR_STATUS (unsolicited)", ""]
    if description := message.get("description"):
        lines += [description, ""]
    lines += field_table(message["payload"])
    return lines


def render_index(spec: dict[str, Any]) -> str:
    """The table of contents: every command, and where it is explained."""
    messages = spec["components"]["messages"]
    grouped = commands_by_category(spec)
    total = sum(len(names) for names in grouped.values())

    lines = [
        "# Protocol reference",
        "",
        f"Every one of the {total} commands the door implements, generated from",
        "`schemas/asyncapi.json` - which is itself generated from the code, so",
        "this cannot drift from what the simulator speaks.",
        "",
        "This is the *reference*. **[Protocol](Protocol)** is the *explanation*:",
        "why the door behaves as it does, which commands accept a value and",
        "quietly discard it, and what silence means.",
        "",
        "> A reply that never arrives is a **dropped request**, not an answer.",
        "> Never read silence as success or as failure - reissue the request.",
        "",
    ]

    for key, page, heading, blurb in CATEGORIES:
        commands = grouped[key]
        lines += [
            f"## [{heading}]({page})",
            "",
            blurb + ".",
            "",
            "| Command | What it does |",
            "| --- | --- |",
        ]
        for command in commands:
            summary = _one_line(messages[command].get("description", ""))
            lines.append(f"| [`{command}`]({page}#{anchor(command)}) | {summary} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def pages(spec: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Every generated page as ``(page name, blurb, markdown)``."""
    rendered = [
        (
            INDEX_PAGE,
            "Every command, with its fields and a worked example",
            render_index(spec),
        )
    ]
    rendered += [(page, blurb, render_page(key, spec)) for key, page, _, blurb in CATEGORIES]
    return rendered


def main() -> int:
    spec = load_spec()
    for page, _, body in pages(spec):
        print(f"{page:26} {len(body.splitlines()):5} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
