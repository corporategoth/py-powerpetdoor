# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Unit tests for the command framework base module (commands/base.py).

Covers argument parsing (parse_arg / parse_args), time and day string
parsing, usage generation, subcommand registries, canonical command
resolution, and the command/subcommand decorators.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# Importing the handler module populates the global command registry and
# registers all subcommands (needed for get_canonical_command tests).
import powerpetdoor.simulator.commands.handler  # noqa: F401
from powerpetdoor.simulator.commands.base import (
    DAY_NAMES,
    DAY_PRESET_NAMES,
    ArgSpec,
    BoolToggleCommandMixin,
    CommandInfo,
    SubcommandInfo,
    _parse_days_str,
    _parse_time_str,
    command,
    get_canonical_command,
    get_command_registry,
    parse_arg,
    parse_args,
    subcommand,
)

# ============================================================================
# Registry guard (this file registers temporary commands)
# ============================================================================


def _snapshot_info(info):
    return (
        list(info.aliases),
        info.usage,
        info.auto_usage,
        {key: (sub, _snapshot_info(sub)) for key, sub in info.subcommands.items()},
    )


def _restore_info(info, snap):
    aliases, usage, auto_usage, subs = snap
    info.aliases = aliases
    info.usage = usage
    info.auto_usage = auto_usage
    info.subcommands.clear()
    for key, (sub, sub_snap) in subs.items():
        _restore_info(sub, sub_snap)
        info.subcommands[key] = sub


@pytest.fixture
def registry_guard():
    """Snapshot and fully restore the global command registry around a test."""
    registry = get_command_registry()
    snap = {key: (info, _snapshot_info(info)) for key, info in registry.items()}
    yield registry
    registry.clear()
    for key, (info, info_snap) in snap.items():
        _restore_info(info, info_snap)
        registry[key] = info


# ============================================================================
# parse_arg: string / passthrough
# ============================================================================


class TestParseArgString:
    def test_string_returned_verbatim(self):
        value, error = parse_arg("Hello/World", ArgSpec("name", "string"))
        assert value == "Hello/World"
        assert error is None

    def test_unknown_arg_type_passes_through(self):
        value, error = parse_arg("raw", ArgSpec("name", "mystery_type"))
        assert value == "raw"
        assert error is None


# ============================================================================
# parse_arg: int
# ============================================================================


class TestParseArgInt:
    def test_valid(self):
        assert parse_arg("42", ArgSpec("n", "int")) == (42, None)

    def test_below_minimum(self):
        value, error = parse_arg("-1", ArgSpec("n", "int", min_value=0))
        assert value is None
        assert error == "'-1' is below minimum (0)"

    def test_above_maximum(self):
        value, error = parse_arg("150", ArgSpec("n", "int", max_value=100))
        assert value is None
        assert error == "'150' is above maximum (100)"

    def test_boundary_values_accepted(self):
        spec = ArgSpec("n", "int", min_value=0, max_value=100)
        assert parse_arg("0", spec) == (0, None)
        assert parse_arg("100", spec) == (100, None)

    def test_not_an_integer(self):
        value, error = parse_arg("abc", ArgSpec("n", "int"))
        assert value is None
        assert error == "'abc' is not a valid integer"


# ============================================================================
# parse_arg: float
# ============================================================================


class TestParseArgFloat:
    def test_valid(self):
        assert parse_arg("2.5", ArgSpec("n", "float")) == (2.5, None)

    def test_below_minimum(self):
        value, error = parse_arg("0.05", ArgSpec("n", "float", min_value=0.1))
        assert value is None
        assert error == "'0.05' is below minimum (0.1)"

    def test_above_maximum(self):
        value, error = parse_arg("901", ArgSpec("n", "float", max_value=900))
        assert value is None
        assert error == "'901' is above maximum (900)"

    def test_not_a_number(self):
        value, error = parse_arg("xyz", ArgSpec("n", "float"))
        assert value is None
        assert error == "'xyz' is not a valid number"


# ============================================================================
# parse_arg: bool_toggle
# ============================================================================


class TestParseArgBoolToggle:
    @pytest.mark.parametrize("raw", ["on", "true", "1", "yes", "ON", "True"])
    def test_true_synonyms(self, raw):
        assert parse_arg(raw, ArgSpec("v", "bool_toggle")) == (True, None)

    @pytest.mark.parametrize("raw", ["off", "false", "0", "no", "OFF", "No"])
    def test_false_synonyms(self, raw):
        assert parse_arg(raw, ArgSpec("v", "bool_toggle")) == (False, None)

    def test_invalid(self):
        value, error = parse_arg("maybe", ArgSpec("v", "bool_toggle"))
        assert value is None
        assert error == "'maybe' is not valid. Use on/off"


# ============================================================================
# parse_arg: choice
# ============================================================================


class TestParseArgChoice:
    SPEC = ArgSpec("sensor", "choice", choices=["inside", "outside", "both"])

    def test_first_choice_canonicalized(self):
        assert parse_arg("Inside", self.SPEC) == ("inside", None)

    def test_non_first_choice_canonicalized(self):
        assert parse_arg("OUTSIDE", self.SPEC) == ("outside", None)

    def test_invalid_choice(self):
        value, error = parse_arg("sideways", self.SPEC)
        assert value is None
        assert error == "'sideways' is not valid. Choose from: inside, outside, both"

    def test_no_choices_defined(self):
        value, error = parse_arg("x", ArgSpec("c", "choice"))
        assert value is None
        assert error == "'x' is not valid. Choose from: none"


# ============================================================================
# parse_arg: time_range
# ============================================================================


class TestParseArgTimeRange:
    SPEC = ArgSpec("time", "time_range")

    def test_valid(self):
        assert parse_arg("6:00-22:00", self.SPEC) == ((6, 0, 22, 0), None)

    def test_hour_only(self):
        assert parse_arg("6-22", self.SPEC) == ((6, 0, 22, 0), None)

    def test_dot_separator(self):
        assert parse_arg("6.30-7.45", self.SPEC) == ((6, 30, 7, 45), None)

    def test_missing_dash(self):
        value, error = parse_arg("6:00", self.SPEC)
        assert value is None
        assert error == "Time range must be in format <start>-<end> (e.g., 6:00-22:00)"

    def test_start_hour_out_of_range(self):
        value, error = parse_arg("25:00-26:00", self.SPEC)
        assert value is None
        assert error == "Invalid time: 25:00"

    def test_end_minute_out_of_range(self):
        value, error = parse_arg("6:00-7:61", self.SPEC)
        assert value is None
        assert error == "Invalid time: 7:61"

    def test_non_numeric_hour(self):
        value, error = parse_arg("ab:00-7:00", self.SPEC)
        assert value is None
        assert error.startswith("invalid literal for int()")


class TestParseTimeStr:
    def test_hour_only_defaults_minute_zero(self):
        assert _parse_time_str("6") == (6, 0)

    def test_leading_zero_and_whitespace(self):
        assert _parse_time_str(" 06:05 ") == (6, 5)

    def test_midnight_and_last_minute(self):
        assert _parse_time_str("0:00") == (0, 0)
        assert _parse_time_str("23:59") == (23, 59)

    def test_hour_out_of_range(self):
        with pytest.raises(ValueError, match="Invalid time: 24"):
            _parse_time_str("24")

    def test_minute_out_of_range(self):
        with pytest.raises(ValueError, match="Invalid time: 12:60"):
            _parse_time_str("12:60")


# ============================================================================
# parse_arg: days / _parse_days_str
# ============================================================================


class TestParseDaysStr:
    def test_preset_all(self):
        assert _parse_days_str("all") == [1, 1, 1, 1, 1, 1, 1]

    def test_preset_weekdays(self):
        assert _parse_days_str("weekdays") == [0, 1, 1, 1, 1, 1, 0]

    def test_preset_weekends(self):
        assert _parse_days_str("weekends") == [1, 0, 0, 0, 0, 0, 1]

    def test_preset_returns_copy(self):
        first = _parse_days_str("all")
        first[0] = 0
        assert _parse_days_str("all") == [1, 1, 1, 1, 1, 1, 1]

    def test_day_names(self):
        assert _parse_days_str("mon,tue,wed") == [0, 1, 1, 1, 0, 0, 0]

    def test_full_names_truncated_and_case_insensitive(self):
        assert _parse_days_str("SUNDAY") == [1, 0, 0, 0, 0, 0, 0]

    def test_spaces_around_names(self):
        assert _parse_days_str("sun, sat") == [1, 0, 0, 0, 0, 0, 1]

    def test_unknown_day(self):
        with pytest.raises(ValueError) as exc:
            _parse_days_str("funday")
        assert str(exc.value) == (
            "Unknown day: fun. Use: sun, mon, tue, wed, thu, fri, sat or all/weekdays/weekends"
        )

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match="Unknown day: "):
            _parse_days_str("")

    def test_parse_arg_days_success(self):
        assert parse_arg("weekends", ArgSpec("days", "days")) == ([1, 0, 0, 0, 0, 0, 1], None)

    def test_parse_arg_days_error(self):
        value, error = parse_arg("blursday", ArgSpec("days", "days"))
        assert value is None
        assert error == (
            "Unknown day: blu. Use: sun, mon, tue, wed, thu, fri, sat or all/weekdays/weekends"
        )

    def test_preset_names_exported(self):
        assert DAY_PRESET_NAMES == ("all", "weekdays", "weekends")

    def test_day_names_order(self):
        assert DAY_NAMES == ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


# ============================================================================
# ArgSpec.generate_usage
# ============================================================================


class TestArgSpecUsage:
    def test_choice(self):
        spec = ArgSpec("sensor", "choice", choices=["inside", "outside"])
        assert spec.generate_usage() == "<inside|outside>"

    def test_bool_toggle_optional(self):
        assert ArgSpec("value", "bool_toggle", required=False).generate_usage() == "[on|off]"

    def test_time_range(self):
        assert ArgSpec("time", "time_range").generate_usage() == "<start-end>"

    def test_days_optional(self):
        assert ArgSpec("days", "days", required=False).generate_usage() == "[days]"

    def test_string_uses_name(self):
        assert ArgSpec("script", "string").generate_usage() == "<script>"

    def test_choice_without_choices_falls_back_to_name(self):
        assert ArgSpec("mode", "choice").generate_usage() == "<mode>"


# ============================================================================
# parse_args (multi-argument parsing)
# ============================================================================


class TestParseArgs:
    SPECS = [
        ArgSpec("sensor", "choice", choices=["inside", "outside"]),
        ArgSpec("count", "int", required=False, default=1),
    ]

    def test_all_args_parsed(self):
        parsed, error = parse_args(["inside", "5"], self.SPECS, ["cmd"])
        assert error is None
        assert parsed == ["inside", 5]

    def test_optional_default_filled(self):
        parsed, error = parse_args(["outside"], self.SPECS, ["cmd"])
        assert error is None
        assert parsed == ["outside", 1]

    def test_extra_arguments_rejected(self):
        parsed, error = parse_args(["inside", "5", "junk", "more"], self.SPECS, ["cmd", "sub"])
        assert parsed == []
        assert error.success is False
        assert error.message == (
            "Unexpected argument(s): junk more\nUsage: cmd sub <inside|outside> [count]"
        )

    def test_missing_required_rejected(self):
        parsed, error = parse_args([], self.SPECS, ["cmd"])
        assert parsed == []
        assert error.success is False
        assert error.message == (
            "Missing required argument: sensor\nUsage: cmd <inside|outside> [count]"
        )

    def test_value_error_includes_usage(self):
        parsed, error = parse_args(["bogus"], self.SPECS, ["cmd"])
        assert parsed == []
        assert error.success is False
        assert error.message == (
            "'bogus' is not valid. Choose from: inside, outside\n"
            "Usage: cmd <inside|outside> [count]"
        )


# ============================================================================
# SubcommandInfo / CommandInfo
# ============================================================================


class TestSubcommandInfo:
    def test_post_init_converts_list_to_registry(self):
        subs = [
            SubcommandInfo("alpha", ["a"]),
            SubcommandInfo("beta"),
        ]
        info = SubcommandInfo("parent", subcommands=subs)
        assert set(info.subcommands) == {"alpha", "a", "beta"}
        assert info.subcommands["a"] is info.subcommands["alpha"]

    def test_post_init_keeps_dict(self):
        registry = {"x": SubcommandInfo("x")}
        info = SubcommandInfo("parent", subcommands=registry)
        assert info.subcommands is registry

    def test_generate_usage_prefers_args(self):
        info = SubcommandInfo(
            "cmd",
            args=[ArgSpec("v", "bool_toggle")],
            subcommands=[SubcommandInfo("toggle")],
        )
        assert info.generate_usage() == "<on|off>"

    def test_generate_usage_from_subcommands_sorted_unique(self):
        info = SubcommandInfo(
            "cmd",
            subcommands=[SubcommandInfo("zeta", ["z"]), SubcommandInfo("alpha")],
        )
        assert info.generate_usage() == "[alpha|zeta]"

    def test_generate_usage_empty(self):
        assert SubcommandInfo("cmd").generate_usage() == ""

    def test_command_info_defaults(self):
        info = CommandInfo("cmd")
        assert info.category == "misc"
        assert info.interactive_only is False
        assert info.local_only is False


# ============================================================================
# get_canonical_command
# ============================================================================


class TestGetCanonicalCommand:
    def test_command_alias_resolved(self):
        assert get_canonical_command("bc") == "broadcast"

    def test_subcommand_alias_resolved(self):
        assert get_canonical_command("ac c") == "ac connect"

    def test_command_and_subcommand_aliases_resolved(self):
        assert get_canonical_command("sched del 1") == "schedule delete 1"

    def test_command_alias_with_canonical_subcommand(self):
        assert get_canonical_command("sched list") == "schedule list"

    def test_single_letter_alias(self):
        assert get_canonical_command("y") == "cycle"

    def test_nested_notify_alias(self):
        assert get_canonical_command("notify lowbat on") == "notify low_battery on"

    def test_already_canonical_returns_none(self):
        assert get_canonical_command("schedule delete 1") is None

    def test_unknown_command_returns_none(self):
        assert get_canonical_command("frobnicate now") is None

    def test_empty_line_returns_none(self):
        assert get_canonical_command("") is None

    def test_unknown_subcommand_leaves_rest_untouched(self):
        assert get_canonical_command("sched bogus") == "schedule bogus"

    def test_deeply_nested_aliases_resolved(self, registry_guard):
        """Alias resolution recurses through arbitrarily nested subcommands."""
        inner = SubcommandInfo("innermost", ["im"], handler=lambda self: None)
        middle = SubcommandInfo("middle", ["md"], handler=lambda self: None, subcommands=[inner])
        info = CommandInfo(name="zcanon", aliases=["zc"], subcommands=[middle])
        registry_guard["zcanon"] = info
        registry_guard["zc"] = info

        assert get_canonical_command("zc md im") == "zcanon middle innermost"
        assert get_canonical_command("zcanon middle innermost") is None


# ============================================================================
# BoolToggleCommandMixin (shared toggle helper)
# ============================================================================


class _Toggler(BoolToggleCommandMixin):
    def __init__(self, simulator):
        self.simulator = simulator


class TestBoolToggleCommandMixin:
    @pytest.fixture
    def toggler(self):
        broadcasts = []
        simulator = SimpleNamespace(
            state=SimpleNamespace(flag=False),
            broadcast_flag=broadcasts.append,
        )
        return _Toggler(simulator), broadcasts

    def test_set_without_broadcast_func(self, toggler):
        instance, broadcasts = toggler
        result = instance._toggle_bool("flag", "Flag", True)
        assert result.success is True
        assert result.message == "Flag: ON"
        assert instance.simulator.state.flag is True
        assert broadcasts == []

    def test_toggle_with_broadcast(self, toggler):
        instance, broadcasts = toggler
        result = instance._toggle_bool("flag", "Flag", None, broadcast_func="broadcast_flag")
        assert result.message == "Flag: ON"
        assert broadcasts == [True]

        result = instance._toggle_bool("flag", "Flag", None, broadcast_func="broadcast_flag")
        assert result.message == "Flag: OFF"
        assert broadcasts == [True, False]

    def test_missing_broadcast_method_tolerated(self, toggler):
        instance, broadcasts = toggler
        result = instance._toggle_bool("flag", "Flag", True, broadcast_func="broadcast_missing")
        assert result.success is True
        assert result.message == "Flag: ON"
        assert broadcasts == []

    def test_enabled_disabled_format(self, toggler):
        instance, _ = toggler
        result = instance._toggle_bool("flag", "Flag", False, fmt="enabled|disabled")
        assert result.message == "Flag: disabled"


# ============================================================================
# command / subcommand decorators
# ============================================================================


class TestCommandDecorator:
    def test_registers_name_and_aliases_with_auto_usage(self, registry_guard):
        @command("ztestcmd", aliases=["ztc"], args=[ArgSpec("v", "bool_toggle")])
        def handler(self, value):
            return value

        registry = get_command_registry()
        info = registry["ztestcmd"]
        assert registry["ztc"] is info
        assert info.usage == "<on|off>"
        assert info.auto_usage is True
        # The wrapper still calls through to the original function
        assert handler(None, True) is True
        assert handler._command_info is info

    def test_explicit_usage_not_regenerated(self, registry_guard):
        @command("ztestcmd2", usage="[custom]")
        def handler(self):
            return None

        info = get_command_registry()["ztestcmd2"]
        assert info.usage == "[custom]"
        assert info.auto_usage is False


class TestSubcommandDecorator:
    def test_metadata_attached_with_auto_usage(self):
        @subcommand("zparent", "zsub", aliases=["zs"], args=[ArgSpec("n", "int")])
        def handler(self, n):
            return n

        assert handler._parent_path == ["zparent"]
        info = handler._subcommand_info
        assert info.name == "zsub"
        assert info.aliases == ["zs"]
        assert info.usage == "<n>"
        assert info.auto_usage is True

    def test_explicit_usage_preserved(self):
        @subcommand(["zparent", "zchild"], "zsub2", usage="[explicit]")
        def handler(self):
            return None

        assert handler._parent_path == ["zparent", "zchild"]
        assert handler._subcommand_info.usage == "[explicit]"
        assert handler._subcommand_info.auto_usage is False
