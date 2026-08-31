# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The state document: one schema for --initial-state, reset and statedoc."""

from __future__ import annotations

import json

import pytest

from powerpetdoor.simulator import state_io
from powerpetdoor.simulator.state import DoorSimulatorState
from powerpetdoor.simulator.state_io import (
    DOCUMENT_SECTIONS,
    StateDocumentError,
    apply_document,
    load_document,
    parse_document,
    state_from_document,
    state_to_document,
)


class TestRoundTrip:
    """What comes out must go back in unchanged."""

    def test_defaults_round_trip(self):
        original = state_to_document(DoorSimulatorState())

        assert state_to_document(state_from_document(original)) == original

    def test_a_modified_state_round_trips(self):
        state = DoorSimulatorState()
        state.hold_time = 12.5
        state.power = False
        state.battery_percent = 33
        state.timezone = "CET-1CEST,M3.5.0,M10.5.0/3"
        original = state_to_document(state)

        assert state_to_document(state_from_document(original)) == original

    def test_an_iana_name_in_a_document_is_normalised_to_posix(self):
        """A document may say `Europe/Berlin`; the door stores the rule.

        Storage is POSIX because the wire is, so a document written by
        hand in the readable spelling loads as the rule it means - and
        then round-trips, because there is only one stored form.
        """
        loaded = state_from_document({"settings": {"timezone": "Europe/Berlin"}})

        assert loaded.timezone == "CET-1CEST,M3.5.0,M10.5.0/3"
        document = state_to_document(loaded)
        assert state_to_document(state_from_document(document)) == document

    def test_a_document_naming_no_real_zone_is_refused(self):
        with pytest.raises(StateDocumentError, match="timezone"):
            state_from_document({"settings": {"timezone": "Middle/Earth"}})

    def test_schedules_round_trip_including_end_of_day(self):
        """24:00 is a legal end and means end-of-day, not midnight."""
        doc = {
            "schedules": [
                {
                    "index": 3,
                    "enabled": True,
                    "days_of_week": [True, False, True, False, True, False, True],
                    "inside": True,
                    "outside": False,
                    "start": "00:00",
                    "end": "24:00",
                }
            ]
        }
        state = state_from_document(doc)

        assert state_to_document(state)["schedules"] == doc["schedules"]

    def test_the_document_carries_no_live_motion_state(self):
        """Configuration only - the engine owns where the door actually is.

        A document that could name DOOR_RISING would be a way to put the
        engine in a state it cannot reach on its own.
        """
        state = DoorSimulatorState()
        state.door_status = "DOOR_KEEPUP"
        state.inside_sensor_active = True
        state.obstruction_active = True

        rendered = json.dumps(state_to_document(state))

        assert "DOOR_KEEPUP" not in rendered
        assert "obstruction" not in rendered
        assert "sensor_active" not in rendered


class TestPartialDocuments:
    """Every section and key is optional, merged over the defaults."""

    def test_one_key_leaves_everything_else_alone(self):
        state = DoorSimulatorState()

        apply_document(state, {"settings": {"hold_time": 15}})

        assert state.hold_time == 15
        assert state.power is DoorSimulatorState().power
        assert state.inside is DoorSimulatorState().inside

    def test_an_empty_document_changes_nothing(self):
        before = state_to_document(DoorSimulatorState())
        state = DoorSimulatorState()

        apply_document(state, {})

        assert state_to_document(state) == before

    @pytest.mark.parametrize("section", sorted(DOCUMENT_SECTIONS))
    def test_every_section_can_appear_alone(self, section):
        """No section may depend on another being present."""
        full = state_to_document(DoorSimulatorState())
        state = DoorSimulatorState()

        apply_document(state, {section: full[section]})

    def test_schedules_replace_rather_than_merge(self):
        """A document listing schedules describes the whole table.

        Merging per index would leave an entry behind from whatever ran
        before, so a reset to a one-entry document would not be a reset.
        """
        state = state_from_document(
            {"schedules": [{"index": 0, "inside": True}, {"index": 5, "outside": True}]}
        )
        assert sorted(state.schedules) == [0, 5]

        apply_document(state, {"schedules": [{"index": 2, "inside": True}]})

        assert sorted(state.schedules) == [2]


class TestMalformedDocumentsAreRefused:
    """A mistyped setting that silently does nothing is the failure mode
    this DSL has twice decided against."""

    def test_an_unknown_section_is_refused_and_names_the_alternatives(self):
        with pytest.raises(StateDocumentError) as error:
            apply_document(DoorSimulatorState(), {"setting": {}})

        assert "Unknown key(s) in the document: setting" in str(error.value)
        assert "settings" in str(error.value)

    def test_an_unknown_key_is_refused_and_names_the_alternatives(self):
        with pytest.raises(StateDocumentError) as error:
            apply_document(DoorSimulatorState(), {"settings": {"hold_tim": 1}})

        assert "Unknown key(s) in settings: hold_tim" in str(error.value)
        assert "hold_time" in str(error.value)

    @pytest.mark.parametrize(
        "document",
        [
            {"settings": []},
            {"battery": "12"},
            {"timing": 3},
        ],
        ids=["list", "string", "int"],
    )
    def test_a_section_that_is_not_a_mapping_is_refused(self, document):
        with pytest.raises(StateDocumentError, match="must be a mapping"):
            apply_document(DoorSimulatorState(), document)

    def test_the_document_itself_must_be_a_mapping(self):
        with pytest.raises(StateDocumentError, match="must be a mapping"):
            apply_document(DoorSimulatorState(), ["settings"])

    @pytest.mark.parametrize("value", ["inf", "-inf", "nan"], ids=["inf", "negative-inf", "nan"])
    def test_a_non_finite_number_is_refused(self, value):
        """The same bound the script channel enforces, from one place.

        `set hold_time inf` broke GET_SETTINGS for every client for the
        life of the process; a document must not be the way around it.
        """
        with pytest.raises(StateDocumentError, match="finite"):
            apply_document(DoorSimulatorState(), {"settings": {"hold_time": value}})

    def test_a_number_outside_its_range_is_refused(self):
        with pytest.raises(StateDocumentError, match="between"):
            apply_document(DoorSimulatorState(), {"battery": {"percent": 101}})

    def test_the_boundary_value_is_accepted(self):
        """The other side of the bound, which decides on its own."""
        state = state_from_document({"battery": {"percent": 100}})

        assert state.battery_percent == 100

    def test_a_non_numeric_number_is_refused(self):
        with pytest.raises(StateDocumentError, match="must be a number"):
            apply_document(DoorSimulatorState(), {"settings": {"hold_time": "soon"}})

    def test_schedules_must_be_a_list(self):
        with pytest.raises(StateDocumentError, match="schedules must be a list"):
            apply_document(DoorSimulatorState(), {"schedules": {"index": 0}})

    def test_a_schedule_without_an_index_is_refused(self):
        """The wire path refuses SET_SCHEDULE without a sibling index too."""
        with pytest.raises(StateDocumentError, match="has no index"):
            apply_document(DoorSimulatorState(), {"schedules": [{"inside": True}]})

    def test_an_unknown_schedule_key_is_refused(self):
        with pytest.raises(StateDocumentError, match="Unknown key"):
            apply_document(DoorSimulatorState(), {"schedules": [{"index": 0, "insied": True}]})

    def test_a_schedule_index_out_of_range_is_refused(self):
        with pytest.raises(StateDocumentError, match="between"):
            apply_document(DoorSimulatorState(), {"schedules": [{"index": 999}]})

    @pytest.mark.parametrize(
        "value",
        ["25:00", "12:60", "24:01", "noon", "12", "12:00:00"],
        ids=["hour", "minute", "past-end-of-day", "word", "no-colon", "seconds"],
    )
    def test_a_malformed_time_is_refused(self, value):
        with pytest.raises(StateDocumentError):
            apply_document(DoorSimulatorState(), {"schedules": [{"index": 0, "start": value}]})

    @pytest.mark.parametrize("value", ["00:00", "23:59", "24:00"])
    def test_the_legal_time_edges_are_accepted(self, value):
        """Both sides of the 24:00 boundary the schedule engine reads."""
        state = state_from_document({"schedules": [{"index": 0, "end": value}]})

        assert state.schedules[0].end_hour * 60 + state.schedules[0].end_min <= 1440

    @pytest.mark.parametrize(
        "days",
        [[], [True] * 6, [True] * 8, "everyday"],
        ids=["empty", "six", "eight", "string"],
    )
    def test_days_of_week_must_be_exactly_seven(self, days):
        with pytest.raises(StateDocumentError, match="7 values"):
            apply_document(
                DoorSimulatorState(),
                {"schedules": [{"index": 0, "days_of_week": days}]},
            )


class TestBooleansAreNotReadByTruthiness:
    """`enabled: "0"` read by truthiness produces an ENABLED entry."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("true", True),
            ("false", False),
            ("0", False),
            ("1", True),
            ("off", False),
            ("on", True),
            ("disabled", False),
            ("enabled", True),
        ],
    )
    def test_boolean_spellings(self, value, expected):
        state = state_from_document({"settings": {"power": value}})

        assert state.power is expected


class TestParsing:
    """JSON is the baseline; YAML is a convenience."""

    def test_json_parses(self):
        assert parse_document('{"settings": {"hold_time": 3}}', source="s.json", prefer_yaml=False)

    def test_malformed_json_is_refused_with_the_file_named(self):
        with pytest.raises(StateDocumentError, match="s.json is not valid"):
            parse_document("{nope", source="s.json", prefer_yaml=False)

    def test_an_empty_file_is_a_document_that_changes_nothing(self):
        """The honest spelling of "start from the defaults", not an error."""
        assert parse_document("", source="s.yaml", prefer_yaml=True) == {}

    def test_a_scalar_file_is_refused(self):
        with pytest.raises(StateDocumentError, match="must contain a mapping"):
            parse_document("3", source="s.json", prefer_yaml=False)

    @pytest.mark.skipif(not state_io.YAML_AVAILABLE, reason="PyYAML not installed")
    def test_yaml_parses(self):
        doc = parse_document("settings:\n  hold_time: 3\n", source="s.yaml", prefer_yaml=True)

        assert doc == {"settings": {"hold_time": 3}}

    @pytest.mark.skipif(not state_io.YAML_AVAILABLE, reason="PyYAML not installed")
    def test_malformed_yaml_is_refused_with_the_file_named(self):
        with pytest.raises(StateDocumentError, match="s.yaml is not valid"):
            parse_document("settings:\n  -\n bad\n", source="s.yaml", prefer_yaml=True)

    def test_yaml_without_pyyaml_says_to_use_json(self, monkeypatch):
        """PyYAML is optional, so a state file must not require it.

        JSON is also what the door itself speaks, which is why it is the
        baseline rather than the fallback.
        """
        monkeypatch.setattr(state_io, "YAML_AVAILABLE", False)

        with pytest.raises(StateDocumentError) as error:
            parse_document("settings: {}", source="s.yaml", prefer_yaml=True)

        assert "JSON" in str(error.value)
        assert "pyyaml" in str(error.value).lower()


class TestLoadingFromDisk:
    @pytest.mark.parametrize("suffix", [".json", ".yaml", ".yml"])
    def test_the_extension_chooses_the_parser(self, tmp_path, suffix):
        if suffix != ".json" and not state_io.YAML_AVAILABLE:
            pytest.skip("PyYAML not installed")
        path = tmp_path / f"state{suffix}"
        path.write_text(
            '{"settings": {"hold_time": 7}}' if suffix == ".json" else "settings:\n  hold_time: 7\n"
        )

        assert load_document(path) == {"settings": {"hold_time": 7}}

    def test_a_missing_file_names_itself(self):
        with pytest.raises(StateDocumentError, match="Cannot read state file"):
            load_document("/nonexistent/state.json")

    def test_a_directory_is_refused_rather_than_crashing(self, tmp_path):
        with pytest.raises(StateDocumentError, match="Cannot read state file"):
            load_document(tmp_path)


class TestEverySectionKeyIsIndependentlyOptional:
    """Each `if key in section` is its own branch.

    A section applier that only ever ran with every key present would
    never execute the "this key is absent" path, and a typo'd `if` there
    would apply the wrong field with the whole suite green.
    """

    @pytest.mark.parametrize("section", sorted(DOCUMENT_SECTIONS - {"schedules"}))
    def test_a_section_with_a_single_key_applies_only_that_key(self, section):
        full = state_to_document(DoorSimulatorState())[section]
        for key in full:
            state = DoorSimulatorState()
            apply_document(state, {section: {key: full[key]}})

    @pytest.mark.parametrize("section", sorted(DOCUMENT_SECTIONS - {"schedules"}))
    def test_an_empty_section_applies_nothing(self, section):
        before = state_to_document(DoorSimulatorState())
        state = DoorSimulatorState()

        apply_document(state, {section: {}})

        assert state_to_document(state) == before

    def test_a_schedule_with_only_an_index_takes_the_defaults(self):
        state = state_from_document({"schedules": [{"index": 4}]})

        assert state.schedules[4].start_hour == 0
        assert state.schedules[4].end_hour == 24

    def test_a_time_with_non_numeric_parts_is_refused(self):
        """Distinct from a malformed shape: two parts, neither a number."""
        with pytest.raises(StateDocumentError, match="must be HH:MM"):
            apply_document(DoorSimulatorState(), {"schedules": [{"index": 0, "start": "ab:cd"}]})


class TestBadBooleansAreLabelled:
    """A misspelled flag names the field it was written for."""

    def test_a_section_flag_says_where_it_was(self):
        with pytest.raises(StateDocumentError, match="settings.power"):
            apply_document(DoorSimulatorState(), {"settings": {"power": "maybe"}})

    def test_a_schedule_flag_says_where_it_was(self):
        with pytest.raises(StateDocumentError, match=r"schedules\[0\].enabled"):
            apply_document(DoorSimulatorState(), {"schedules": [{"index": 0, "enabled": "maybe"}]})

    def test_a_day_flag_says_where_it_was(self):
        with pytest.raises(StateDocumentError, match="days_of_week"):
            apply_document(
                DoorSimulatorState(),
                {"schedules": [{"index": 0, "days_of_week": ["maybe"] + [True] * 6}]},
            )
