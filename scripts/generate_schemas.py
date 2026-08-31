#!/usr/bin/env python3
# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Emit the machine-readable specs, from the tables that already define them.

Three artifacts, all in open formats other tools already understand:

``schemas/script.schema.json``
    JSON Schema (2020-12) for the YAML script DSL. Point an editor at it
    and a script author gets completion and inline errors - see
    docs/scripting.md.

``schemas/state.schema.json``
    JSON Schema for the state documents ``--initial-state`` and ``reset``
    read.

``schemas/asyncapi.json``
    AsyncAPI 3.0 for the door protocol on TCP 3000. Message-driven rather
    than request/response, which is what the door actually is: it pushes
    door-status changes nobody asked for.

Every one of these is *derived*. The action table, the value registry and
the wire table are the source; nothing here restates them. Run with
``--check`` to fail when the committed files have drifted, which is what
the pre-commit hook does.

What is deliberately NOT generated: docs/protocol.md. Its value is the
seventeen firmware-verified findings in its prose - accept-and-ignore,
32-bit saturation, the envelope-key discovery - and a schema has nowhere
to put those. The specs describe the shape; the prose says what a real
door does with it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import powerpetdoor  # noqa: E402
from powerpetdoor import const  # noqa: E402
from powerpetdoor.schedule import MAX_SCHEDULE_INDEX  # noqa: E402
from powerpetdoor.simulator.notifications import NOTIFICATION_NAMES  # noqa: E402
from powerpetdoor.simulator.scripting import (  # noqa: E402
    _ACTION_PARAMS,
    ACTION_DESCRIPTIONS,
    ASSERT_CONDITIONS,
    COMPARISONS,
    MAX_SCRIPT_DELAY,
    MAX_SCRIPT_REPEAT,
    ON_TIMEOUT_CHOICES,
    PARAM_DESCRIPTIONS,
    STEP_ANNOTATION_KEYS,
    WRITABLE,
)
from powerpetdoor.simulator.values import VALUES  # noqa: E402
from powerpetdoor.simulator.wire_values import (  # noqa: E402
    COMMAND_DOCS,
    FIELD_DOCS,
    MULTI_VALUE_GETTERS,
    OBJECT_FIELD_DOCS,
    WIRE_VALUES,
)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from probe_protocol import (  # noqa: E402
    ENVELOPE_FIELDS,
    json_type,
    probe_all,
)

SCHEMA_DIR = REPO_ROOT / "schemas"
SCHEMA_VERSION = "https://json-schema.org/draft/2020-12/schema"
#: Where the published copies live. `$id` is the schema's canonical
#: identifier and what a `$ref` resolves against, so a wrong path is a
#: 404 for every consumer. The repo is `powerpetdoor`; the *package*
#: is `pypowerpetdoor`, which is what this had.
BASE_URL = "https://git.neuromancy.net/pypi/powerpetdoor/raw/branch/main/schemas"

#: A yes/no the DSL accepts in any of its spellings. PyYAML resolves the
#: bare words to real booleans, so both have to be legal here.
BOOLEAN = {
    "oneOf": [
        {"type": "boolean"},
        {"enum": ["on", "off", "yes", "no", "true", "false", "enabled", "disabled", "1", "0"]},
    ]
}

#: Parameter name -> its shape. Shared across actions, as the names are.
PARAM_SCHEMAS: dict[str, dict[str, Any]] = {
    "sensor": {"enum": ["inside", "outside"]},
    "state": dict(BOOLEAN),
    "duration": {"type": "number", "minimum": 0},
    "seconds": {"type": "number", "minimum": 0, "maximum": MAX_SCRIPT_DELAY},
    "timeout": {"type": "number", "minimum": 0, "maximum": MAX_SCRIPT_DELAY},
    "on_timeout": {"enum": list(ON_TIMEOUT_CHOICES)},
    "index": {"type": "integer", "minimum": 0, "maximum": MAX_SCHEDULE_INDEX},
    "enabled": dict(BOOLEAN),
    "percent": {"type": "integer", "minimum": 0, "maximum": 100},
    "message": {"type": "string"},
    "times": {"type": "integer", "minimum": 0, "maximum": MAX_SCRIPT_REPEAT},
    "condition": {"enum": list(ASSERT_CONDITIONS)},
    "conditions": {"type": "array", "items": {"enum": list(ASSERT_CONDITIONS)}},
    "any": {"type": "array", "items": {"enum": list(ASSERT_CONDITIONS)}},
    "initial_state": {"type": "string"},
    "steps": {"type": "array", "items": {"$ref": "#/$defs/step"}},
    "then": {"type": "array", "items": {"$ref": "#/$defs/step"}},
    "else": {"type": "array", "items": {"$ref": "#/$defs/step"}},
}

#: Where an action gives a shared parameter name a narrower meaning than
#: :data:`PARAM_SCHEMAS` can express on its own.
PER_ACTION_PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("set", "name"): {"enum": list(WRITABLE)},
    ("set", "value"): {
        "description": "The new value. `toggle` inverts a yes/no one.",
        "oneOf": [{"type": ["string", "number", "boolean"]}],
    },
    ("notify", "name"): {"enum": list(NOTIFICATION_NAMES)},
    ("battery", "value"): {"type": "integer", "minimum": 0, "maximum": 100},
}


def _param_schema(action: str, name: str) -> dict[str, Any]:
    """One parameter's shape, with its description folded in."""
    schema = dict(PER_ACTION_PARAMS.get((action, name)) or PARAM_SCHEMAS.get(name) or {})
    if name in COMPARISONS:
        schema.setdefault("description", PARAM_DESCRIPTIONS[name])
    elif name in PARAM_DESCRIPTIONS:
        schema.setdefault("description", PARAM_DESCRIPTIONS[name])
    return schema


def script_schema() -> dict[str, Any]:
    """JSON Schema for a YAML script.

    One branch per action, keyed on ``action``, so an editor offers only
    the parameters that action takes - which is also exactly what the
    runner enforces.
    """
    branches = []
    for action in sorted(_ACTION_PARAMS):
        properties = {name: _param_schema(action, name) for name in sorted(_ACTION_PARAMS[action])}
        properties["action"] = {"const": action}
        for key in sorted(STEP_ANNOTATION_KEYS):
            properties[key] = {
                "type": "string",
                "description": "Free-text annotation; read by nothing.",
            }
        branches.append(
            {
                "title": action,
                "description": ACTION_DESCRIPTIONS[action],
                "properties": properties,
                "required": ["action"],
                "additionalProperties": False,
            }
        )

    return {
        "$schema": SCHEMA_VERSION,
        "$id": f"{BASE_URL}/script.schema.json",
        "title": "Power Pet Door simulator script",
        "description": (
            "A YAML script for the Power Pet Door simulator. Generated from the "
            "runner's own action table - see docs/scripting.md."
        ),
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Shown while the script runs."},
            "description": {"type": "string", "description": "What the script demonstrates."},
            "steps": {"type": "array", "items": {"$ref": "#/$defs/step"}},
        },
        "required": ["steps"],
        "additionalProperties": False,
        "$defs": {
            "step": {
                "type": "object",
                "required": ["action"],
                "oneOf": branches,
            }
        },
    }


def state_schema() -> dict[str, Any]:
    """JSON Schema for a state document.

    Sections mirror the document's own, and each carries the values that
    belong to it - so the settings section offers exactly the settings.
    """

    def section(names: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {name: _value_schema(name) for name in sorted(names) if name in VALUES},
            "additionalProperties": False,
        }

    timing = [n for n in VALUES if VALUES[n].simulation_only and n.endswith(("_time", "_window"))]
    battery = ["battery", "battery_present", "ac_present", "charge_rate", "discharge_rate"]
    hardware = ["firmware_version", "hardware_version", "has_remote_id", "has_remote_key"]
    accounted = set(timing) | set(battery) | set(hardware)
    settings = [
        n
        for n in VALUES
        if VALUES[n].writable and n not in accounted and not n.startswith("notify_")
    ]

    return {
        "$schema": SCHEMA_VERSION,
        "$id": f"{BASE_URL}/state.schema.json",
        "title": "Power Pet Door simulator state document",
        "description": (
            "A state document for --initial-state, or for `reset <name>`. Every "
            "section is optional; what it omits keeps its default."
        ),
        "type": "object",
        "properties": {
            "settings": section(settings),
            "battery": section(battery),
            "hardware": section(hardware),
            "timing": section(timing),
            "notifications": section([n for n in VALUES if n.startswith("notify_")]),
            "schedules": {
                "type": "array",
                "description": "Replaces the whole schedule table.",
                "items": {"$ref": "#/$defs/schedule"},
            },
        },
        "additionalProperties": False,
        "$defs": {
            "schedule": {
                "type": "object",
                "properties": {
                    "index": PARAM_SCHEMAS["index"],
                    "enabled": dict(BOOLEAN),
                    "inside": dict(BOOLEAN),
                    "outside": dict(BOOLEAN),
                    "days_of_week": {
                        "type": "array",
                        "items": {"type": "boolean"},
                        "minItems": 7,
                        "maxItems": 7,
                    },
                    "start": {"type": "string", "pattern": r"^\d{1,2}:\d{2}$"},
                    "end": {"type": "string", "pattern": r"^\d{1,2}:\d{2}$"},
                },
                "additionalProperties": False,
            }
        },
    }


def _value_schema(name: str) -> dict[str, Any]:
    """One registry value, as a state document carries it."""
    spec = VALUES[name]
    schema: dict[str, Any] = {"description": spec.description}
    if spec.kind == "bool":
        schema.update(BOOLEAN)
    elif spec.kind in ("int", "number"):
        schema["type"] = "integer" if spec.kind == "int" else "number"
        schema["minimum"] = spec.minimum
        if spec.maximum:
            schema["maximum"] = spec.maximum
    else:
        schema["type"] = "string"
    return schema


#: Commands the simulator implements but the published spec must not
#: advertise, and why. A spec describes what a client should *send*; a
#: command that cannot work is not that.
#:
#: Empty, and that is the goal: a command the door does not implement is
#: removed from the simulator entirely rather than described and excluded.
#:
#: Each reason is checked against a live probe by
#: `test_schemas.py::TestExclusionsAreJustified`, so an entry cannot
#: outlive the behaviour that justified it.
NOT_IN_SPEC: dict[str, str] = {}


#: Request fields that belong to the envelope rather than to any one
#: command, so they are never listed as that command's arguments.
_ENVELOPE_REQUEST_FIELDS = frozenset({const.FIELD_MSG_ID, const.FIELD_DIRECTION})


def _request_envelope() -> dict[str, Any]:
    """Fields every request carries besides its command."""
    return {
        const.FIELD_MSG_ID: dict(FIELD_DOCS[const.FIELD_MSG_ID]),
        # No `examples` beside a `const`: the const IS the value, and a
        # renderer that shows both prints it twice.
        const.FIELD_DIRECTION: {
            "const": const.PHONE_TO_DOOR,
            "description": (
                "Phone to door. Optional - the door answers a request without "
                "it - but every client observed sends it, and it is the "
                "counterpart of the `d2p` on every reply."
            ),
        },
    }


def _reply_envelope() -> dict[str, Any]:
    """Fields every reply carries besides its own payload."""
    return {
        const.FIELD_SUCCESS: _field_schema(const.FIELD_SUCCESS, None),
        const.FIELD_DIRECTION: {
            "const": const.DOOR_TO_PHONE,
            "description": "Door to phone; the only direction a reply travels.",
        },
        const.FIELD_MSG_ID_RESPONSE: {
            "type": "string",
            "description": (
                "The `msgId` from the request, echoed back - note the capital "
                "D. Absent on an unsolicited push."
            ),
        },
    }


def _stable(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """An observed message with its self-changing fields pinned.

    The examples are real exchanges, which is what makes them worth
    having - but the door's clock differs between two runs of this
    generator, so a committed document would never match a freshly
    rendered one. A field that declares its own example uses that.
    """
    if payload is None:
        return None
    return {
        field: (FIELD_DOCS[field]["examples"][0] if _declares_example(field) else value)
        for field, value in payload.items()
    }


def _declares_example(field: str) -> bool:
    return "examples" in FIELD_DOCS.get(field, {})


def _field_schema(field: str, observed: Any) -> dict[str, Any]:
    """One field: described, constrained, with the value the probe saw.

    A numeric field on this wire is meaningless without its unit - the
    hold time is centiseconds and the thresholds are millivolts, and both
    look like ordinary integers. The unit is carried as `x-unit` so a
    generator can read it, and repeated in the description so a person
    can.
    """
    schema = dict(FIELD_DOCS.get(field) or json_type(observed))
    unit = schema.pop("unit", None)
    if unit:
        schema["x-unit"] = unit
        if unit not in schema.get("description", ""):
            schema["description"] = f"{schema.get('description', '').rstrip()} In {unit}.".strip()
    # An object is where this protocol keeps its least guessable
    # structure, so `type: object` and nothing else is the one place a
    # description most needs to go deeper.
    if members := OBJECT_FIELD_DOCS.get(field):
        schema["properties"] = {member: _member_schema(spec) for member, spec in members.items()}
    # An observed value is the better example - it is real - but a field
    # that changes on its own (the clock) declares one instead, or the
    # generated document would never match a freshly generated one.
    if "examples" not in schema and observed is not None and not isinstance(observed, dict | list):
        schema["examples"] = [observed]
    return schema


def _member_schema(declared: dict[str, Any]) -> dict[str, Any]:
    """One member of an object field, with its unit moved to `x-unit`.

    Same treatment the top-level fields get, applied one level down -
    `holdOpenTime` is as much in centiseconds as `holdTime` is, and a
    reader of the nested table has no more idea than a reader of the
    outer one.
    """
    schema = dict(declared)
    unit = schema.pop("unit", None)
    if unit:
        schema["x-unit"] = unit
        if unit not in schema.get("description", ""):
            schema["description"] = f"{schema.get('description', '').rstrip()} In {unit}.".strip()
    if "properties" in schema:
        schema["properties"] = {
            member: _member_schema(spec) for member, spec in schema["properties"].items()
        }
    return schema


#: The wiki page each area of the protocol is explained on. Generated
#: from the same docs, so a link here lands on prose rather than on a
#: schema a reader has already got open.
WIKI = "https://git.neuromancy.net/pypi/powerpetdoor/wiki"


def _tags(command: str) -> list[dict[str, str]]:
    """Group a command so a reader can find its relatives.

    The useful relations here are not alphabetical: a setter and the
    getter that reads it back, and the aggregate that reports the same
    value among a dozen others.
    """
    tags: list[dict[str, str]] = []
    if command.startswith("GET_") or command.startswith("HAS_"):
        tags.append({"name": "read"})
    else:
        tags.append({"name": "write"})
    if command in const.COMMAND_ENVELOPE_COMMANDS:
        tags.append({"name": "door-motion"})
    if "SCHEDULE" in command:
        tags.append({"name": "schedule"})
    if "NOTIFICATION" in command:
        tags.append({"name": "notification"})
    # The value this command carries is also reported by GET_SETTINGS,
    # which is the single most useful cross-reference in this protocol:
    # a client polling settings does not need the individual getter.
    for name, wire in WIRE_VALUES.items():
        # The getter counts as much as the setters do: `timersEnabled` is
        # in the settings object, so a client polling `GET_SETTINGS` does
        # not need `GET_TIMERS_ENABLED` either.
        if command in (wire.enable, wire.disable, wire.getter) and name in _IN_SETTINGS:
            tags.append({"name": "also-in-GET_SETTINGS"})
            break
    else:
        # A getter reporting several values at once is nobody's `getter`,
        # but `GET_SETTINGS` still carries what it reports.
        if any(v in _IN_SETTINGS for v in MULTI_VALUE_GETTERS.get(command, ())):
            tags.append({"name": "also-in-GET_SETTINGS"})
    return tags


#: Values that `GET_SETTINGS` reports alongside everything else.
_IN_SETTINGS = frozenset(
    {
        "power",
        "inside",
        "outside",
        "auto",
        "safety_lock",
        "cmd_lockout",
        "autoretract",
        "timezone",
        "hold_time",
        "sensor_trigger_voltage",
        "sleep_sensor_trigger_voltage",
    }
)


def _message_description(command: str, seen: dict[str, Any]) -> str:
    """What the command does, then anything surprising about it.

    The description says what happens to the *door*. How the message is
    spelled is the schema's job, and repeating it here was telling a
    reader what they could already see.
    """
    parts = [COMMAND_DOCS[command]]
    if seen["took_effect"] is False:
        parts.append(
            f"The reply reports success, but `{seen['read_back']}` still reports "
            "the previous value afterwards."
        )
    return " ".join(parts)


def _command_summary(seen: dict[str, Any]) -> str:
    """One line describing a command, from what the probe saw.

    The effect clause is the important half. A client told only that a
    command answered `success: "true"` will assume it worked; told that
    the value does not change afterwards, it will not send it.
    """
    parts = [f"Sent under the `{seen['envelope']}` envelope key."]
    if seen["silent"]:
        parts.append("Answered with silence - not even a failure.")
    if seen["took_effect"] is False:
        parts.append(
            f"**Accepted and ignored**: `{seen['read_back']}` reports the same value "
            "afterwards, so the write does nothing."
        )
    elif seen["took_effect"] is True:
        parts.append(f"Verified to take effect - `{seen['read_back']}` reflects it.")
    return " ".join(parts)


def asyncapi() -> dict[str, Any]:
    """AsyncAPI 3.0 for the door protocol, from a live probe.

    Message-driven rather than request/response on purpose: the door
    pushes status changes nobody asked for, which an HTTP-shaped
    description has no way to say.

    Every message here was **observed**, not declared: the generator
    starts a simulator, sends all 43 commands, and records what came
    back. The previous hand-written version described 18 of them and read
    as though it described the protocol.

    What is deliberately absent is *behaviour* - the accept-and-ignore
    replies, the saturation, the single-connection degradation. A shape
    cannot carry those; docs/protocol.md does.
    """
    observed = asyncio.run(probe_all())

    messages: dict[str, Any] = {}
    for command, seen in sorted(observed.items()):
        if command in NOT_IN_SPEC:
            continue
        reply = seen["reply"] or {}
        body = {k: v for k, v in reply.items() if k not in ENVELOPE_FIELDS}
        # Everything the command carries beyond the envelope is required:
        # a `SET_TIMEZONE` without `tz` merely echoes the current value.
        payload_required = [seen["envelope"]] + sorted(
            f for f in seen["request"] if f not in _ENVELOPE_REQUEST_FIELDS
        )
        messages[command] = {
            "name": command,
            "title": command,
            "summary": _command_summary(seen),
            "description": _message_description(command, seen),
            "traits": [{"$ref": "#/components/messageTraits/request"}],
            "tags": _tags(command),
            "externalDocs": {
                "description": "What a real door does with this",
                "url": f"{WIKI}/Protocol",
            },
            "payload": {
                "type": "object",
                "title": f"{command} request",
                # unknown keys are accepted
                # and ignored, so a schema that forbade them would describe a
                # stricter door than exists. A typo is still caught, by
                # `required`.
                "additionalProperties": True,
                "properties": {
                    seen["envelope"]: {
                        "const": command,
                        "description": (
                            "Names the command. Door motion uses `cmd`; "
                            "everything else uses `config`."
                        ),
                    },
                    **_request_envelope(),
                    **{
                        f: _field_schema(f, v)
                        for f, v in seen["request"].items()
                        if f not in _ENVELOPE_REQUEST_FIELDS
                    },
                },
                "required": payload_required,
                # The same example as below. AsyncAPI carries examples on
                # the message; JSON Schema carries them on the schema, and
                # a given renderer shows one or the other.
                "examples": [_stable({seen["envelope"]: command, **seen["request"]})],
            },
            "examples": [
                {
                    "name": "request",
                    "summary": f"A complete {command}, as sent.",
                    "payload": _stable({seen["envelope"]: command, **seen["request"]}),
                }
            ],
        }
        if body:
            messages[f"{command}.reply"] = {
                "name": f"{command}.reply",
                "title": f"{command} reply",
                "summary": f"What the door answers a `{command}` with.",
                "description": (
                    f"{COMMAND_DOCS[command]} This is what the door sends back. "
                    "A reply that never arrives means the request was dropped "
                    "rather than refused, so reissue it."
                ),
                "traits": [{"$ref": "#/components/messageTraits/reply"}],
                "tags": _tags(command),
                "payload": {
                    "type": "object",
                    "title": f"{command} reply",
                    # Permissive on purpose: a firmware revision that adds a
                    # field should not make every existing client reject the
                    # reply.
                    "additionalProperties": True,
                    "properties": {
                        const.FIELD_CMD: {
                            "const": command,
                            "description": "The command being answered.",
                        },
                        **_reply_envelope(),
                        **{f: _field_schema(f, v) for f, v in body.items()},
                    },
                    "required": [const.FIELD_CMD, const.FIELD_SUCCESS],
                    "examples": [_stable(seen["reply"])],
                },
                "examples": [
                    {
                        "name": "reply",
                        "summary": f"A complete {command} reply, as received.",
                        "payload": _stable(seen["reply"]),
                    }
                ],
            }

    return {
        "asyncapi": "3.0.0",
        "info": {
            "title": "Power Pet Door",
            # The version of this document, which ships with the library.
            # Not a firmware version: the protocol has none of its own.
            "version": powerpetdoor.__version__,
            "description": (
                "The wire protocol a High Tech Pet Power Pet Door speaks on TCP 3000. "
                "Reverse-engineered, and generated "
                "by exercising the simulator - so every message shape here is one the "
                "simulator actually produced.\n\n"
                "This describes the *shapes*. What a real door does with them - the "
                "commands it accepts and silently ignores, the values it saturates "
                "rather than refuses, why door motion uses a different envelope key - "
                "is in docs/protocol.md, and no schema has anywhere to put it.\n\n"
                "Commands the door accepts but cannot act on are deliberately "
                "absent - see docs/protocol.md for those; sending one is never "
                "correct."
            ),
            "license": {"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
            "externalDocs": {
                "description": "Protocol notes, with the firmware-verified behaviour",
                "url": f"{WIKI}/Protocol",
            },
            "tags": [
                {"name": "read", "description": "Reports a value; changes nothing."},
                {"name": "write", "description": "Changes a value."},
                {
                    "name": "door-motion",
                    "description": "Moves the flap; uses the `cmd` envelope key.",
                },
                {"name": "schedule", "description": "Schedule slots."},
                {"name": "notification", "description": "The notification switches."},
                {
                    "name": "also-in-GET_SETTINGS",
                    "description": (
                        "This value is also reported by GET_SETTINGS, so one call "
                        "can replace several individual getters."
                    ),
                },
            ],
        },
        "defaultContentType": "application/json",
        "servers": {
            "door": {
                "host": "192.168.0.1:3000",
                "protocol": "tcp",
                "description": (
                    "A door on the local network. It serves ONE connection; a second "
                    "degrades into unexplained timeouts."
                ),
            }
        },
        "channels": {
            "door": {
                "address": "/",
                "title": "Door connection",
                "description": (
                    "A single bidirectional TCP stream. Messages are brace-matched "
                    "JSON objects with no terminator between them."
                ),
                "messages": {
                    **{name: {"$ref": f"#/components/messages/{name}"} for name in messages},
                    "doorStatus": {"$ref": "#/components/messages/doorStatus"},
                },
            }
        },
        # AsyncAPI 3.0 binds a request to its answer with `reply` on the
        # operation, so there is one operation per command rather than a
        # single "send" bucket that leaves a reader to guess what comes
        # back.
        "operations": {
            **{
                f"send{command.title().replace('_', '')}": {
                    "action": "send",
                    "channel": {"$ref": "#/channels/door"},
                    "title": command,
                    "summary": messages[command]["summary"],
                    "messages": [{"$ref": f"#/channels/door/messages/{command}"}],
                    **(
                        {
                            "reply": {
                                "channel": {"$ref": "#/channels/door"},
                                "messages": [{"$ref": f"#/channels/door/messages/{command}.reply"}],
                            }
                        }
                        if f"{command}.reply" in messages
                        else {}
                    ),
                }
                for command in sorted(observed)
                if command in messages
            },
            "receiveDoorStatus": {
                "action": "receive",
                "channel": {"$ref": "#/channels/door"},
                "title": "Unsolicited door status",
                "summary": (
                    "The door reports its own state changes without being asked. "
                    "These carry no msgID; route them to the same handler as a "
                    "GET_DOOR_STATUS reply."
                ),
                "messages": [{"$ref": "#/channels/door/messages/doorStatus"}],
            },
        },
        "components": {
            # `correlationId` is what pairs a reply with its request:
            # `msgId` goes out, `msgID` comes back.
            "messageTraits": {
                "request": {
                    "contentType": "application/json",
                    "correlationId": {
                        "description": "Pairs a request with its reply.",
                        "location": f"$message.payload#/{const.FIELD_MSG_ID}",
                    },
                },
                "reply": {
                    "contentType": "application/json",
                    "correlationId": {
                        "description": "Echoes the `msgId` of the request it answers.",
                        "location": f"$message.payload#/{const.FIELD_MSG_ID_RESPONSE}",
                    },
                },
            },
            "messages": {
                **messages,
                "doorStatus": {
                    "name": "DOOR_STATUS",
                    "title": "DOOR_STATUS",
                    "summary": "Unsolicited door-state change. Carries no msgID.",
                    "description": (
                        "The door reports its own movement as it happens, without "
                        "being asked - so a client that opens the flap sees it "
                        "travel rather than having to poll. Carries no `msgID`, "
                        "because it answers nothing. Route it to the same handler "
                        "as a GET_DOOR_STATUS reply; the payload is identical."
                    ),
                    "tags": [{"name": "read"}],
                    "externalDocs": {
                        "description": "What a real door does with this",
                        "url": f"{WIKI}/Protocol",
                    },
                    "contentType": "application/json",
                    "payload": {
                        "type": "object",
                        "title": "DOOR_STATUS push",
                        "additionalProperties": True,
                        "properties": {
                            const.FIELD_CMD: {
                                "const": const.DOOR_STATUS,
                                "description": "Always DOOR_STATUS for an unsolicited push.",
                            },
                            const.FIELD_DOOR_STATUS: {
                                "enum": sorted(const.DOOR_POSITIONS),
                                "description": "How far through its travel the flap is.",
                            },
                            const.FIELD_SUCCESS: {
                                **FIELD_DOCS[const.FIELD_SUCCESS],
                                "examples": [const.SUCCESS_TRUE],
                            },
                            const.FIELD_DIRECTION: {"const": const.DOOR_TO_PHONE},
                        },
                    },
                },
            },
        },
    }


ARTIFACTS = {
    "script.schema.json": script_schema,
    "state.schema.json": state_schema,
    "asyncapi.json": asyncapi,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if a committed artifact has drifted, instead of rewriting it.",
    )
    args = parser.parse_args()

    SCHEMA_DIR.mkdir(exist_ok=True)
    stale = []
    for filename, build in ARTIFACTS.items():
        path = SCHEMA_DIR / filename
        rendered = json.dumps(build(), indent=2, sort_keys=False) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                stale.append(filename)
        else:
            path.write_text(rendered, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO_ROOT)}")

    if stale:
        print(
            "These are generated from the code and have drifted: "
            + ", ".join(stale)
            + "\nRun: uv run python scripts/generate_schemas.py",
            file=sys.stderr,
        )
        return 1
    if args.check:
        print("schemas are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
