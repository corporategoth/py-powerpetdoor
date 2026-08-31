# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The published specs describe what the code actually does.

``schemas/`` holds three artifacts other tools consume: JSON Schema for
the script DSL and for state documents, and AsyncAPI 3.0 for the wire.
All three are generated from the same tables the runtime reads, so the
only way they can be wrong is if the generator has drifted from the
committed copy - or if the schema is so loose it accepts nonsense.

Both are tested here. A schema that accepts everything is worse than no
schema: it tells a script author their typo is fine.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

import powerpetdoor
from powerpetdoor.schedule import MAX_SCHEDULE_HOUR, MAX_SCHEDULE_INDEX
from powerpetdoor.simulator.notifications import NOTIFICATION_NAMES
from powerpetdoor.simulator.scripting import (
    _ACTION_PARAMS,
    ACTION_DESCRIPTIONS,
    ASSERT_CONDITIONS,
    WRITABLE,
)
from powerpetdoor.simulator.values import VALUES
from powerpetdoor.simulator.wire_values import OBJECT_FIELD_DOCS

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"
SCRIPTS_DIR = REPO_ROOT / "src" / "powerpetdoor" / "simulator" / "scripts"


def message_properties(message: dict) -> dict:
    """A message's payload properties."""
    return message.get("payload", {}).get("properties", {})


def message_required(message: dict) -> set:
    """Everything a message declares required."""
    return set(message.get("payload", {}).get("required", []))


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def script_schema() -> dict:
    return _load("script.schema.json")


@pytest.fixture(scope="module")
def state_schema() -> dict:
    return _load("state.schema.json")


@pytest.fixture(scope="module")
def meta_schema() -> dict:
    """The official AsyncAPI 3.0 schema, vendored so this runs offline."""
    return json.loads(
        (REPO_ROOT / "schemas" / "vendor" / "asyncapi-3.0.0.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def spec() -> dict:
    return _load("asyncapi.json")


@pytest.fixture(scope="module")
def script_validator(script_schema) -> Draft202012Validator:
    return Draft202012Validator(script_schema)


# ============================================================================
# The committed copies are current
# ============================================================================


class TestGeneratedFilesAreCurrent:
    """`--check` is what the pre-commit hook runs."""

    def test_nothing_has_drifted(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "generate_schemas.py"), "--check"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr


# ============================================================================
# The script schema
# ============================================================================


def _action_branches(script_schema):
    """The per-action mapping branches.

    A step is now either the mapping form or the bare-string shorthand
    (`- close`), which the runner has always taken and the schema used to
    reject, so the branches sit one level in.
    """
    forms = script_schema["$defs"]["step"]["oneOf"]
    mapping = next(form for form in forms if form.get("type") == "object")
    return mapping["oneOf"]


class TestScriptSchema:
    def test_the_bare_string_shorthand_is_accepted(self, script_schema):
        """`- close` is a legal step; the schema said otherwise.

        An editor validating against the published schema flagged a
        script the runner runs happily.
        """
        forms = script_schema["$defs"]["step"]["oneOf"]
        shorthand = next(form for form in forms if form.get("type") == "string")
        assert set(shorthand["enum"]) == set(_ACTION_PARAMS)

    def test_the_shorthand_still_refuses_a_typo(self, script_schema):
        validator = Draft202012Validator(script_schema)
        assert list(validator.iter_errors({"name": "t", "steps": ["clsoe"]}))
        assert not list(validator.iter_errors({"name": "t", "steps": ["close"]}))

    def test_it_is_a_valid_json_schema(self, script_schema):
        Draft202012Validator.check_schema(script_schema)

    def test_it_declares_the_2020_12_dialect(self, script_schema):
        assert script_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_every_action_has_a_branch(self, script_schema):
        branches = {b["title"] for b in _action_branches(script_schema)}
        assert branches == set(_ACTION_PARAMS)

    def test_every_branch_carries_its_description(self, script_schema):
        for branch in _action_branches(script_schema):
            assert branch["description"] == ACTION_DESCRIPTIONS[branch["title"]]

    def test_a_branch_offers_exactly_that_actions_parameters(self, script_schema):
        """The schema's strictness has to match the runner's."""
        for branch in _action_branches(script_schema):
            action = branch["title"]
            offered = set(branch["properties"]) - {"action", "comment", "description", "note"}
            assert offered == set(_ACTION_PARAMS[action]), action

    def test_setting_names_come_from_the_registry(self, script_schema):
        branch = next(b for b in _action_branches(script_schema) if b["title"] == "set")
        assert branch["properties"]["name"]["enum"] == list(WRITABLE)

    def test_condition_names_come_from_the_registry(self, script_schema):
        branch = next(b for b in _action_branches(script_schema) if b["title"] == "assert")
        assert branch["properties"]["condition"]["enum"] == list(ASSERT_CONDITIONS)

    def test_notification_names_come_from_the_registry(self, script_schema):
        branch = next(b for b in _action_branches(script_schema) if b["title"] == "notify")
        assert branch["properties"]["name"]["enum"] == list(NOTIFICATION_NAMES)

    @pytest.mark.parametrize("path", sorted(SCRIPTS_DIR.glob("*.yaml")), ids=lambda p: p.name)
    def test_every_shipped_script_validates(self, script_validator, path):
        """A schema the project's own scripts fail is the schema's bug."""
        errors = list(script_validator.iter_errors(yaml.safe_load(path.read_text())))
        assert errors == [], "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:3])


class TestScriptSchemaRefusesTheMistakesItIsFor:
    """The half that makes it worth publishing.

    Each of these is a mistake the runner refuses at run time. An editor
    that reads the schema should refuse it while it is being typed.
    """

    @pytest.mark.parametrize(
        ("label", "document"),
        [
            ("misspelled action", {"steps": [{"action": "trigge", "sensor": "inside"}]}),
            ("misspelled parameter", {"steps": [{"action": "wait", "second": 1}]}),
            ("parameter from another action", {"steps": [{"action": "wait", "sensor": "inside"}]}),
            ("unknown sensor", {"steps": [{"action": "trigger", "sensor": "sideways"}]}),
            ("unknown setting", {"steps": [{"action": "set", "name": "powr", "value": 1}]}),
            ("unknown condition", {"steps": [{"action": "assert", "condition": "door_ajar"}]}),
            ("schedule index out of range", {"steps": [{"action": "add_schedule", "index": 999}]}),
            ("negative wait", {"steps": [{"action": "wait", "seconds": -1}]}),
            ("battery over 100", {"steps": [{"action": "battery", "percent": 101}]}),
            ("no steps at all", {"name": "x"}),
            ("misspelled top-level key", {"steps": [], "descriptio": "x"}),
            ("step with no action", {"steps": [{"seconds": 1}]}),
        ],
    )
    def test_it_is_refused(self, script_validator, label, document):
        assert list(script_validator.iter_errors(document)), label

    @pytest.mark.parametrize(
        ("label", "document"),
        [
            ("an annotation", {"steps": [{"action": "close", "note": "settle"}]}),
            ("a bare boolean", {"steps": [{"action": "set", "name": "power", "value": False}]}),
            ("a yaml word", {"steps": [{"action": "set", "name": "power", "value": "off"}]}),
            (
                "nested blocks",
                {
                    "steps": [
                        {"action": "if", "condition": "door_open", "then": [{"action": "close"}]}
                    ]
                },
            ),
        ],
    )
    def test_it_is_accepted(self, script_validator, label, document):
        """The other side of the boundary: legal scripts must not trip."""
        errors = list(script_validator.iter_errors(document))
        assert errors == [], f"{label}: {errors[0].message if errors else ''}"


# ============================================================================
# The state schema
# ============================================================================


class TestStateSchema:
    def test_it_is_a_valid_json_schema(self, state_schema):
        Draft202012Validator.check_schema(state_schema)

    def test_it_has_a_section_per_document_section(self, state_schema):
        from powerpetdoor.simulator.state_io import DOCUMENT_SECTIONS

        assert set(state_schema["properties"]) == set(DOCUMENT_SECTIONS)

    def test_simulation_knobs_are_under_timing(self, state_schema):
        timing = state_schema["properties"]["timing"]["properties"]
        assert "rise_time" in timing
        assert all(VALUES[name].simulation_only for name in timing)

    def test_a_partial_document_is_valid(self, state_schema):
        """Sections are optional; what a document omits keeps its default."""
        v = Draft202012Validator(state_schema)
        assert list(v.iter_errors({"settings": {"power": False}})) == []

    def test_an_unknown_section_is_refused(self, state_schema):
        v = Draft202012Validator(state_schema)
        assert list(v.iter_errors({"setttings": {}}))

    def test_an_unknown_value_is_refused(self, state_schema):
        v = Draft202012Validator(state_schema)
        assert list(v.iter_errors({"settings": {"powr": False}}))

    def test_a_value_out_of_range_is_refused(self, state_schema):
        v = Draft202012Validator(state_schema)
        assert list(v.iter_errors({"battery": {"battery": 101}}))


# ============================================================================
# AsyncAPI
# ============================================================================


class TestAsyncApiIsComplete:
    """The spec describes the whole protocol, or it misleads.

    It described 18 of 43 commands once - every `GET_*`, the door motion
    commands, schedules and notifications were absent - while being
    titled "Power Pet Door" and read as though it were the protocol.
    Incompleteness is the failure mode here, not incorrectness.
    """

    def test_it_declares_asyncapi_3(self, spec):
        assert spec["asyncapi"] == "3.0.0"

    def test_it_names_the_verified_firmware(self, spec):
        assert spec["info"]["version"] == powerpetdoor.__version__

    def test_every_command_the_door_implements_is_described(self, spec):
        from powerpetdoor.simulator.protocol import CommandRegistry
        from scripts.generate_schemas import NOT_IN_SPEC

        messages = set(spec["components"]["messages"])
        missing = sorted(set(CommandRegistry._handlers) - messages - set(NOT_IN_SPEC))
        assert missing == [], f"the spec does not describe: {', '.join(missing)}"

    def test_an_excluded_command_really_is_absent(self, spec):
        from scripts.generate_schemas import NOT_IN_SPEC

        present = sorted(set(NOT_IN_SPEC) & set(spec["components"]["messages"]))
        assert present == [], f"excluded but still offered: {', '.join(present)}"

    def test_it_describes_nothing_the_door_does_not_implement(self, spec):
        """The other direction: an invented command is worse than a missing one."""
        from powerpetdoor.simulator.protocol import CommandRegistry

        # `<CMD>.reply` describes the answer to `<CMD>`, not a command.
        known = set(CommandRegistry._handlers) | {"doorStatus"}
        known |= {f"{cmd}.reply" for cmd in CommandRegistry._handlers}
        invented = sorted(set(spec["components"]["messages"]) - known)
        assert invented == [], f"the spec invents: {', '.join(invented)}"

    def test_every_message_carries_its_envelope_key(self, spec):
        """`config` versus `cmd` is the discovery a client most needs."""
        for name, message in spec["components"]["messages"].items():
            if name == "doorStatus" or name.endswith(".reply"):
                continue
            properties = message_properties(message)
            assert ("config" in properties) or ("cmd" in properties), name

    def test_door_motion_uses_the_cmd_envelope(self, spec):
        """Only door motion travels under `cmd`; the rest use `config`."""
        from powerpetdoor.const import COMMAND_ENVELOPE_COMMANDS

        for name, message in spec["components"]["messages"].items():
            if name == "doorStatus" or name.endswith(".reply"):
                continue
            key = "cmd" if name in COMMAND_ENVELOPE_COMMANDS else "config"
            assert key in message_properties(message), f"{name} should use {key}"

    def test_the_unsolicited_push_is_described_as_received(self, spec):
        """The thing an HTTP-shaped description could not say."""
        op = spec["operations"]["receiveDoorStatus"]
        assert op["action"] == "receive"
        assert {"$ref": "#/channels/door/messages/doorStatus"} in op["messages"]
        assert "msgID" in spec["components"]["messages"]["doorStatus"]["summary"]

    def test_every_command_is_an_operation_bound_to_its_reply(self, spec):
        """AsyncAPI 3.0 pairs a request with its answer via `reply`.

        A single "send" bucket listing every command leaves a reader to
        guess what comes back from what.
        """
        from powerpetdoor.simulator.protocol import CommandRegistry

        sends = {op["title"]: op for op in spec["operations"].values() if op["action"] == "send"}
        assert set(sends) == set(CommandRegistry._handlers)
        for title, op in sends.items():
            replies = spec["components"]["messages"].get(f"{title}.reply")
            if replies is not None:
                assert "reply" in op, f"{title} has a reply message but no reply binding"
                assert op["reply"]["messages"] == [
                    {"$ref": f"#/channels/door/messages/{title}.reply"}
                ]

    def test_the_spec_states_only_what_is_valid(self, spec):
        """It describes the values the door accepts, not the ones it does not.

        A schema is a contract for what to send. Cataloguing rejected
        spellings belongs in the prose, which has room to say why.
        """
        rendered = json.dumps(spec)
        assert "IANA" not in rendered
        assert "PowerPetDoor." not in rendered, "the spec should not cite library code"

    def test_it_points_at_the_prose_for_behaviour(self, spec):
        url = spec["info"]["externalDocs"]["url"]
        assert "Protocol" in url, url

    def test_the_single_connection_limit_is_stated(self, spec):
        assert "ONE connection" in spec["servers"]["door"]["description"]


class TestAsyncApiTellsTheTruthAboutWhatWorks:
    """A command that does nothing must not read as one that works.

    The generator *probes* for this rather than being told: it sends each
    command, then reads the value back through the matching `GET_*` to
    see whether the write landed. An accept-and-ignore appearing in a
    future firmware would be described without anyone having to notice it
    had to be.
    """

    def test_no_command_is_silently_ignored(self, spec):
        """Nothing currently claims to work while doing nothing."""
        ignored = [
            name
            for name, message in spec["components"]["messages"].items()
            if "Accepted and ignored" in message.get("summary", "") and not name.endswith(".reply")
        ]
        assert ignored == [], f"described as working but does nothing: {ignored}"

    @pytest.mark.parametrize(
        "command",
        ["SET_TIMEZONE", "SET_HOLD_TIME", "SET_SENSOR_TRIGGER_VOLTAGE", "SET_NOTIFICATIONS"],
    )
    def test_every_setter_was_verified_to_work(self, spec, command):
        """If one regresses to a no-op, the generator says so and this fails."""
        assert "take effect" in spec["components"]["messages"][command]["summary"], command

    def test_the_spec_describes_nothing_the_simulator_cannot_answer(self, spec):
        """A positive perimeter, rather than a list of names to avoid.

        Commands that turned out not to exist have been described here
        before. Naming them to assert their absence keeps them alive in
        the tree and grows with every mistake; requiring instead that
        every described command has a handler bounds the spec by what is
        actually implemented, and catches an invented name on the first
        run without anyone having thought of it.
        """
        from powerpetdoor.simulator.protocol import CommandRegistry

        described = {
            name
            for name in spec["components"]["messages"]
            if not name.endswith(".reply") and name != "doorStatus"
        }
        invented = sorted(described - set(CommandRegistry._handlers))
        assert invented == [], f"described but unimplemented: {invented}"


class TestExclusionsAreJustified:
    """An exclusion must not outlive the behaviour that justified it.

    Leaving a command out of the spec is a claim: that sending it is
    never correct. If a firmware revision made an excluded command work,
    the entry would silently keep hiding it - so the claim is re-derived
    from a live probe rather than trusted.
    """

    def test_every_exclusion_has_a_reason(self):
        from scripts.generate_schemas import NOT_IN_SPEC

        assert all(reason.strip() for reason in NOT_IN_SPEC.values())

    def test_every_exclusion_names_a_command_that_exists(self):
        from powerpetdoor.simulator.protocol import CommandRegistry
        from scripts.generate_schemas import NOT_IN_SPEC

        unknown = sorted(set(NOT_IN_SPEC) - set(CommandRegistry._handlers))
        assert unknown == [], f"excludes commands that do not exist: {unknown}"

    def test_every_excluded_command_is_still_broken(self):
        """The probe, not the comment, is what justifies the exclusion."""
        import asyncio
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from probe_protocol import probe_all

        from scripts.generate_schemas import NOT_IN_SPEC

        observed = asyncio.run(probe_all())
        for command in NOT_IN_SPEC:
            seen = observed[command]
            works = seen["took_effect"] is True or (
                not seen["silent"] and seen["took_effect"] is None
            )
            assert not works, (
                f"{command} is excluded from the spec but the probe says it works "
                "- remove the exclusion, or the spec is hiding a real command."
            )

    def test_the_spec_says_something_was_left_out(self):
        """A silent omission reads as completeness."""
        import json
        from pathlib import Path

        spec = json.loads(
            (Path(__file__).resolve().parent.parent / "schemas" / "asyncapi.json").read_text()
        )
        assert "deliberately absent" in spec["info"]["description"]


class TestEveryWireFieldIsDocumented:
    """A field described as `{"type": "string"}` tells a reader nothing.

    This protocol's difficulty is almost entirely in the spellings: the
    same value is an int at the top level of a reply and a `"true"`
    string inside `settings`; the hold time is centiseconds; the voltage
    setter takes `voltage` while its getter answers
    `sensorTriggerVoltage`. A schema that omits that is decoration.
    """

    def _fields(self, spec):
        for name, message in spec["components"]["messages"].items():
            for field, schema in message_properties(message).items():
                yield name, field, schema

    def test_every_field_has_a_description(self, spec):
        bare = sorted(
            {
                field
                for _, field, schema in self._fields(spec)
                if "description" not in schema and "const" not in schema
            }
        )
        assert bare == [], f"undescribed fields: {', '.join(bare)}"

    #: Integers that are ordinals or counts rather than quantities. A unit
    #: is what stops a reader sending seconds where centiseconds are
    #: wanted; there is no comparable mistake available for "which slot",
    #: and `"In slot number."` is not English.
    NOT_QUANTITIES = frozenset({"index"})

    def test_every_numeric_field_declares_its_unit(self, spec):
        """An integer on this wire is meaningless without one."""
        missing = sorted(
            {
                field
                for _, field, schema in self._fields(spec)
                if schema.get("type") == "integer"
                and "x-unit" not in schema
                and "enum" not in schema  # a 0/1 flag is not a quantity
                and field not in self.NOT_QUANTITIES
            }
        )
        assert missing == [], f"numeric fields with no unit: {', '.join(missing)}"

    def test_an_exempt_field_still_says_what_it_is(self, spec):
        """The exemption is from the unit, not from being described.

        Without this, adding a name to `NOT_QUANTITIES` would be a way to
        make an undocumented integer pass.
        """
        for field in self.NOT_QUANTITIES:
            schema = next(s for _, f, s in self._fields(spec) if f == field)
            assert schema["description"].strip()
            assert "minimum" in schema and "maximum" in schema

    def test_units_appear_in_the_description_too(self, spec):
        """`x-unit` is for tools; the rendered page is for people."""
        for name, field, schema in self._fields(spec):
            unit = schema.get("x-unit")
            if unit:
                assert unit in schema["description"], f"{name}.{field}"

    def test_a_command_argument_is_required(self, spec):
        """`SET_TIMEZONE` without `tz` merely echoes - so `tz` is required.

        The first version of this document listed only the envelope key,
        which told a client every argument was optional.
        """
        for name, message in spec["components"]["messages"].items():
            if not name.startswith("SET_") or name.endswith(".reply"):
                continue
            required = message_required(message)
            # `dir` and `msgId` belong to the envelope, not to the command.
            arguments = {
                f for f in message_properties(message) if f not in ("config", "cmd", "msgId", "dir")
            }
            assert arguments <= required, f"{name}: {sorted(arguments - required)} not required"

    def test_bounded_fields_carry_their_bounds(self, spec):
        """The bounds come from the validators that enforce them."""
        seen = {}
        for _, field, schema in self._fields(spec):
            seen.setdefault(field, schema)
        assert seen["tz"]["maxLength"] == 128
        assert seen["holdTime"]["maximum"] == 90000
        assert seen["voltage"]["maximum"] == 2**31 - 1
        assert seen["index"]["maximum"] == 255

    def test_every_field_carries_an_observed_example(self, spec):
        """Examples come from the probe, so they cannot be aspirational."""
        without = sorted(
            {
                f"{name}.{field}"
                for name, field, schema in self._fields(spec)
                if schema.get("type") in ("string", "integer")
                and "examples" not in schema
                and "const" not in schema
                and not name.endswith(".reply")
            }
        )
        assert without == [], f"no example: {', '.join(without)}"


class TestDescriptionsSayWhatTheCommandDoes:
    """A description that restates the schema is not a description.

    The first version said "Sent as `{...}`, and use GET_X to read it
    back" - which a reader could see from the payload beside it. What is
    worth writing down is what happens to the door.
    """

    def test_every_command_is_described(self):
        from powerpetdoor.simulator.protocol import CommandRegistry
        from powerpetdoor.simulator.wire_values import COMMAND_DOCS

        missing = sorted(set(CommandRegistry._handlers) - set(COMMAND_DOCS))
        assert missing == [], f"undescribed commands: {', '.join(missing)}"

    def test_no_description_merely_restates_the_wire_format(self, spec):
        """The tell is a description that quotes the envelope back."""
        for name, message in spec["components"]["messages"].items():
            description = message.get("description", "")
            assert not description.startswith("Sent as"), name
            assert "with any arguments as siblings" not in description, name

    def test_descriptions_are_substantial(self, spec):
        """A stub is worse than none - it looks answered."""
        for name, message in spec["components"]["messages"].items():
            assert len(message.get("description", "")) > 40, name

    def test_additional_properties_is_stated_not_defaulted(self, spec):
        """The door ignores unknown keys; the schema should say so, not omit it.

        `GET_SETTINGS` with unknown
        fields of assorted types was accepted and answered normally.
        """
        for name, message in spec["components"]["messages"].items():
            assert "additionalProperties" in message["payload"], (
                f"{name} leaves additionalProperties to the default"
            )

    def test_payloads_are_flat_and_titled(self, spec):
        """A composed payload renders as `<anonymous-schema-N>`.

        `allOf` with a shared `$ref` was correct and DRY, but this
        document is *generated* - so the deduplication belongs in the
        generator, where it costs nothing, rather than in output a person
        has to read.
        """
        for name, message in spec["components"]["messages"].items():
            payload = message["payload"]
            assert "allOf" not in payload, f"{name} composes its payload"
            assert payload.get("title"), f"{name} has an untitled payload"

    def test_every_message_carries_a_worked_example(self, spec):
        """Taken from a real exchange, so it cannot be aspirational."""
        for name, message in spec["components"]["messages"].items():
            if name == "doorStatus":
                continue
            examples = message.get("examples")
            assert examples, f"{name} has no example"
            payload = examples[0]["payload"]
            envelope = "cmd" if "cmd" in message["payload"]["properties"] else "config"
            key = envelope if not name.endswith(".reply") else "CMD"
            assert key in payload, f"{name} example is missing its envelope key"

    def test_direction_is_documented_but_not_required(self, spec):
        """`dir: "p2d"` is what a client sends, not what the door demands.

        Every command was probed without it and answered normally, so
        marking it required would describe a stricter door than exists.
        Omitting it entirely was worse: a reader implementing from this
        would not know the field existed, then meet it in real traffic.
        """
        for name, message in spec["components"]["messages"].items():
            if name.endswith(".reply") or name == "doorStatus":
                continue
            properties = message["payload"]["properties"]
            assert "dir" in properties, f"{name} does not mention dir"
            assert properties["dir"]["const"] == "p2d"
            assert "dir" not in message["payload"]["required"], (
                f"{name} demands dir, which the door does not"
            )

    def test_replies_carry_the_opposite_direction(self, spec):
        for name, message in spec["components"]["messages"].items():
            if not name.endswith(".reply"):
                continue
            assert message["payload"]["properties"]["dir"]["const"] == "d2p", name


class TestNothingIsSaidTwice:
    """A `const` is the value; an `examples` beside it repeats it.

    Renderers show both, so a field whose value can only ever be `p2d`
    printed `p2d` twice - once as the constraint and once as an example
    of it. The example adds nothing a reader did not already have.
    """

    @staticmethod
    def _nodes(node, path=""):
        if isinstance(node, dict):
            yield path, node
            for key, value in node.items():
                yield from TestNothingIsSaidTwice._nodes(value, f"{path}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from TestNothingIsSaidTwice._nodes(value, f"{path}/{i}")

    def test_no_const_carries_an_example_of_itself(self, spec):
        both = [p for p, n in self._nodes(spec) if "const" in n and "examples" in n]
        assert both == [], f"const and examples together at: {both[:5]}"


class TestTheSpecIsValidAsyncApi:
    """Validated against the official 3.0.0 meta-schema, offline.

    Everything else here checks that the document says the right things.
    This checks it is a legal AsyncAPI document at all - which no amount
    of hand-reading establishes, and which a renderer silently declining
    to show something does not disprove.

    The meta-schema is vendored (`schemas/vendor/`) so the suite does not
    depend on the network or on AsyncAPI's CDN.
    """

    def test_the_document_validates(self, spec, meta_schema):
        from jsonschema import Draft7Validator

        errors = sorted(Draft7Validator(meta_schema).iter_errors(spec), key=lambda e: list(e.path))
        assert errors == [], "\n".join(
            f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:5]
        )

    def test_the_worked_example_is_on_both_the_message_and_the_schema(self, spec):
        """Renderers disagree about which one they show.

        AsyncAPI puts examples on the message; JSON Schema puts them on
        the schema. Carrying both costs a few lines of generated output
        and means the example is visible whichever a reader's tool looks
        at.
        """
        for name, message in spec["components"]["messages"].items():
            if name == "doorStatus":
                continue
            assert message.get("examples"), f"{name}: no message-level example"
            assert message["payload"].get("examples"), f"{name}: no schema-level example"
            assert message["examples"][0]["payload"] == message["payload"]["examples"][0], (
                f"{name}: the two examples disagree"
            )


class TestObjectFieldsAreDocumentedToTheirMembers:
    """`type: object` and nothing else is where this spec was weakest.

    `settings`, `schedule`, `notifications` and `fwInfo` carry the
    richest and least guessable structures on this wire - eleven settings
    keys whose spellings differ from the top level, a schedule whose
    `inside` means something else than the settings `inside`. Describing
    them as "an object" told a reader nothing they could act on.

    The guard that matters is completeness: a member the simulator emits
    and nobody documented would otherwise reach the wire, the spec and
    the wiki as a bare name.
    """

    @staticmethod
    def _observed_objects(spec):
        """Every object value the probe actually saw, by field name."""
        seen: dict[str, dict] = {}
        for message in spec["components"]["messages"].values():
            for example in message.get("examples", ()):
                for field, value in (example.get("payload") or {}).items():
                    if isinstance(value, dict):
                        seen.setdefault(field, {}).update(value)
        return seen

    def test_every_emitted_member_is_documented(self, spec):
        for field, observed in self._observed_objects(spec).items():
            documented = set(OBJECT_FIELD_DOCS.get(field, {}))
            missing = sorted(set(observed) - documented)
            assert missing == [], (
                f"`{field}` emits {missing} but OBJECT_FIELD_DOCS does not "
                "describe them. Add them in wire_values.py."
            )

    def test_nothing_is_documented_that_is_never_emitted(self, spec):
        """A member removed from the wire should not linger in the docs."""
        observed = self._observed_objects(spec)
        for field, members in OBJECT_FIELD_DOCS.items():
            stale = sorted(set(members) - set(observed.get(field, {})))
            assert stale == [], f"`{field}` documents {stale}, which the door never sends"

    def test_the_members_reach_the_published_spec(self, spec):
        """Documented in the table but absent from the document is no use."""
        settings = spec["components"]["messages"]["GET_SETTINGS.reply"]["payload"]["properties"][
            "settings"
        ]
        assert set(settings["properties"]) == set(OBJECT_FIELD_DOCS["settings"])

    def test_every_member_says_something(self, spec):
        for field, members in OBJECT_FIELD_DOCS.items():
            for member, schema in members.items():
                assert schema.get("description", "").strip(), f"{field}.{member} has no description"
                assert schema.get("type"), f"{field}.{member} has no type"

    def test_a_nested_unit_becomes_x_unit(self, spec):
        """Same treatment the top level gets: machine-readable, and in prose.

        `holdOpenTime` is centiseconds exactly as `holdTime` is, and a
        reader of the nested table has no more idea than a reader of the
        outer one.
        """
        settings = spec["components"]["messages"]["GET_SETTINGS.reply"]["payload"]["properties"][
            "settings"
        ]["properties"]
        assert settings["holdOpenTime"]["x-unit"] == "centiseconds"
        assert "centiseconds" in settings["holdOpenTime"]["description"]
        assert "unit" not in settings["holdOpenTime"]

    def test_the_time_objects_nest_a_further_level(self, spec):
        """`in_start_time` is itself an object; the render walks into it."""
        schedule = spec["components"]["messages"]["GET_SCHEDULE.reply"]["payload"]["properties"][
            "schedule"
        ]["properties"]
        hour = schedule["in_start_time"]["properties"]["hour"]
        # Against the constant, not a literal 23. A literal here is
        # exactly how the published spec came to claim a bound narrower
        # than the device's own end-of-day spelling.
        assert hour["maximum"] == MAX_SCHEDULE_HOUR
        assert schedule["in_start_time"]["required"] == ["hour", "min"]

    def test_the_same_name_can_mean_different_things(self, spec):
        """`inside` is a switch in settings and a sensor selector in a schedule.

        Folding these into one table would have to pick one meaning and
        would then be wrong about the other - which is the reason
        OBJECT_FIELD_DOCS is keyed by the containing object.
        """
        assert OBJECT_FIELD_DOCS["settings"]["inside"]["type"] == "string"
        assert OBJECT_FIELD_DOCS["schedule"]["inside"]["type"] == "integer"


class TestTheProbeExercisesADoorWithSomethingInIt:
    """A default door has no schedules, so the schedule commands
    documented nothing: `GET_SCHEDULE` answered `"schedule": null` and
    `GET_SCHEDULE_LIST` answered with an empty list. Both are now probed
    against a door with a populated slot.
    """

    def test_get_schedule_returns_a_real_schedule(self, spec):
        example = spec["components"]["messages"]["GET_SCHEDULE.reply"]["examples"][0]["payload"]
        assert example["schedule"] is not None
        assert example["schedule"]["daysOfWeek"] == [0, 1, 1, 1, 1, 1, 0]

    def test_get_schedule_list_reports_the_populated_slot(self, spec):
        example = spec["components"]["messages"]["GET_SCHEDULE_LIST.reply"]["examples"][0][
            "payload"
        ]
        assert example["schedules"] == [0]

    def test_set_schedule_still_registers_as_taking_effect(self, spec):
        """The seed must differ from what `SET_SCHEDULE` writes.

        Seeding the very value the command is about to write would make
        the effect probe read before == after and report the command as
        accepted-and-ignored - a false finding about the door.
        """
        from scripts.probe_protocol import REQUEST_ARGUMENTS, probe_state

        written = REQUEST_ARGUMENTS["SET_SCHEDULE"]["schedule"]
        seeded = probe_state().schedules[0].to_dict()
        assert seeded != written


class TestNoContainerIsLeftOpaque:
    """A container that does not say what it holds describes nothing.

    `schedules` rendered as an array of *anything* because `FIELD_DOCS`
    declared `type: array` and stopped - and a declared type wins over
    the shape the probe observed, so writing the field's documentation
    was what erased its contents. The same mechanism had left `settings`,
    `schedule` and `notifications` as bare objects.

    These fail on the next container that forgets, rather than on the
    next person to notice.
    """

    @staticmethod
    def _containers(node, path=""):
        if isinstance(node, dict):
            if node.get("type") in ("array", "object"):
                yield path, node
            for key, value in node.items():
                yield from TestNoContainerIsLeftOpaque._containers(value, f"{path}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from TestNoContainerIsLeftOpaque._containers(value, f"{path}/{i}")

    def test_every_array_says_what_it_holds(self, spec):
        bare = [p for p, n in self._containers(spec) if n["type"] == "array" and "items" not in n]
        assert bare == [], f"arrays with no `items`: {bare}"

    def test_every_array_item_has_a_type(self, spec):
        untyped = [
            p
            for p, n in self._containers(spec)
            if n["type"] == "array" and not (n.get("items") or {}).get("type")
        ]
        assert untyped == [], f"arrays whose items have no type: {untyped}"

    def test_every_payload_object_says_what_it_holds(self, spec):
        """Applies to the fields, not to the envelopes that carry them."""
        bare = [
            p
            for p, n in self._containers(spec)
            if n["type"] == "object" and "properties" not in n and "/payload/properties/" in p
        ]
        assert bare == [], f"objects with no `properties`: {bare}"

    def test_the_slot_list_holds_slot_numbers(self, spec):
        """The specific regression: not schedules, and not `any`."""
        schedules = spec["components"]["messages"]["GET_SCHEDULE_LIST.reply"]["payload"][
            "properties"
        ]["schedules"]
        assert schedules["items"]["type"] == "integer"
        assert schedules["items"]["maximum"] == MAX_SCHEDULE_INDEX

    def test_the_slot_bound_is_the_one_index_uses(self, spec):
        """One slot bound, not a second copy of 255.

        An element of `schedules` and the `index` you pass to
        `GET_SCHEDULE` are the same quantity, so they cannot be allowed
        to disagree about their range.
        """
        messages = spec["components"]["messages"]
        item = messages["GET_SCHEDULE_LIST.reply"]["payload"]["properties"]["schedules"]["items"]
        index = messages["GET_SCHEDULE"]["payload"]["properties"]["index"]
        assert item["maximum"] == index["maximum"]
        assert item["minimum"] == index["minimum"]
