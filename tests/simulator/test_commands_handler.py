# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for command dispatch and registration (commands/handler.py).

Covers execute() dispatch edge cases, CLI-mode registry manipulation,
subcommand registration (instance and module-level), and error handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import powerpetdoor.simulator.commands.handler as handler_mod
from powerpetdoor.simulator import (
    DoorSimulator,
    DoorSimulatorState,
    DoorTimingConfig,
)
from powerpetdoor.simulator.commands import CommandHandler
from powerpetdoor.simulator.commands.base import (
    CommandInfo,
    CommandResult,
    SubcommandInfo,
    get_command_registry,
    subcommand,
)
from powerpetdoor.simulator.commands.handler import register_all_subcommands
from powerpetdoor.simulator.commands.history import History
from powerpetdoor.simulator.scripting import ScriptRunner

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def timing_config():
    return DoorTimingConfig(
        rise_time=0.05,
        default_hold_time=1,
        slowing_time=0.02,
        closing_start_time=0.02,
        closing_top_time=0.02,
        closing_mid_time=0.02,
        sensor_retrigger_window=0.1,
    )


@pytest.fixture
async def simulator(timing_config):
    state = DoorSimulatorState(timing=timing_config, hold_time=1)
    sim = DoorSimulator(port=0, state=state)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
def command_handler(simulator):
    return CommandHandler(
        simulator=simulator,
        script_runner=ScriptRunner(simulator),
        stop_callback=MagicMock(),
    )


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
    saved_exit = handler_mod._saved_exit_info
    yield registry
    registry.clear()
    for key, (info, info_snap) in snap.items():
        _restore_info(info, info_snap)
        registry[key] = info
    handler_mod._saved_exit_info = saved_exit


# ============================================================================
# execute() dispatch edge cases
# ============================================================================


class TestExecuteDispatch:
    async def test_empty_command(self, command_handler):
        result = await command_handler.execute("")
        assert result.success is False
        assert result.message == "Empty command"

    async def test_unknown_command(self, command_handler):
        result = await command_handler.execute("frobnicate now")
        assert result.success is False
        assert result.message == "Unknown command: frobnicate. Type 'help' for commands."

    async def test_unknown_subcommand_lists_available(self, command_handler):
        result = await command_handler.execute("schedule bogus")
        assert result.success is False
        assert result.message == (
            "Unknown schedule subcommand: bogus\n"
            "Available: add, clear, days, delete, disable, enable, list, time"
        )

    async def test_local_only_rejected_on_daemon_control_port(
        self, command_handler, registry_guard
    ):
        registry_guard["zlocal"] = CommandInfo(
            name="zlocal",
            handler=lambda self: CommandResult(True, "ran"),
            local_only=True,
        )
        # Daemon control port: neither CLI nor interactive mode
        result = await command_handler.execute("zlocal")
        assert result.success is False
        assert result.message == "Unknown command: zlocal. Type 'help' for commands."

    async def test_local_only_allowed_in_cli_and_interactive_modes(
        self, command_handler, registry_guard
    ):
        def zlocal_handler(*args):
            return CommandResult(True, "ran locally")

        zlocal_handler.__name__ = "zlocal_handler"
        command_handler.zlocal_handler = zlocal_handler
        registry_guard["zlocal"] = CommandInfo(
            name="zlocal", handler=zlocal_handler, local_only=True
        )

        command_handler.set_cli_mode(True)
        result = await command_handler.execute("zlocal")
        assert result.success is True
        assert result.message == "ran locally"
        command_handler.set_cli_mode(False)

        command_handler.set_interactive_mode(True)
        result = await command_handler.execute("zlocal")
        assert result.success is True

    async def test_command_without_handler(self, command_handler, registry_guard):
        registry_guard["zghost"] = CommandInfo(name="zghost", handler=None)
        result = await command_handler.execute("zghost")
        assert result.success is False
        assert result.message == "No handler for: zghost"

    async def test_subcommand_without_handler_treated_as_argument(
        self, command_handler, registry_guard
    ):
        def zparent_handler(*args):
            return CommandResult(True, "parent ran")

        zparent_handler.__name__ = "zparent_handler"
        command_handler.zparent_handler = zparent_handler
        registry_guard["zparent"] = CommandInfo(
            name="zparent",
            handler=zparent_handler,
            subcommands={"sub": SubcommandInfo("sub")},  # No handler bound
        )

        # The handler-less subcommand stops descent; with no args declared the
        # leftover part is rejected rather than silently dropped
        result = await command_handler.execute("zparent sub")
        assert result.success is False
        assert result.message == "Unexpected argument(s): sub\nUsage: zparent"

        # The parent itself still executes normally
        result = await command_handler.execute("zparent")
        assert result.success is True
        assert result.message == "parent ran"

    async def test_arg_handler_exception_reported(self, command_handler, monkeypatch):
        def boom(seconds=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(command_handler, "holdtime", boom)
        result = await command_handler.execute("holdtime 5")
        assert result.success is False
        assert result.message == "boom"

    async def test_no_arg_handler_exception_reported(self, command_handler, monkeypatch):
        def boom():
            raise RuntimeError("kaboom")

        monkeypatch.setattr(command_handler, "close", boom)
        result = await command_handler.execute("close")
        assert result.success is False
        assert result.message == "kaboom"

    async def test_async_no_arg_handler_awaited(self, command_handler, monkeypatch):
        async def async_close():
            return CommandResult(True, "async closed")

        monkeypatch.setattr(command_handler, "close", async_close)
        result = await command_handler.execute("close")
        assert result.success is True
        assert result.message == "async closed"

    async def test_async_arg_handler_exception_reported(self, command_handler, monkeypatch):
        async def async_boom(script_ref, mode=None):
            raise RuntimeError("async boom")

        monkeypatch.setattr(command_handler, "run", async_boom)
        result = await command_handler.execute("run something")
        assert result.success is False
        assert result.message == "async boom"

    async def test_implicit_subcommand_help_without_args(self, command_handler):
        result = await command_handler.execute("schedule ?")
        assert result.success is True
        assert result.message.startswith("schedule subcommands:")

    async def test_implicit_arg_help_with_args(self, command_handler):
        result = await command_handler.execute("power ?")
        assert result.success is True
        assert result.message.startswith("power [on|off]")


# ============================================================================
# set_history
# ============================================================================


class TestSetHistory:
    async def test_history_available_after_set_history(self, command_handler, monkeypatch):
        # Even when prompt_toolkit cannot drive the session, a registered
        # History object makes the history command available
        import powerpetdoor.simulator.prompt_common as prompt_common

        monkeypatch.setattr(prompt_common, "use_prompt_toolkit", lambda: False)
        command_handler.set_interactive_mode(True)

        history = History()
        command_handler.set_history(history)
        assert command_handler._history is history.prompt_toolkit_history
        assert command_handler._history_obj is history

        history.prompt_toolkit_history.append_string("status")
        result = await command_handler.execute("history")
        assert result.success is True
        assert result.message == "History (1 of 1 commands):\n      1  status"


# ============================================================================
# set_cli_mode registry manipulation
# ============================================================================


class TestSetCliMode:
    async def test_missing_shutdown_leaves_registry_untouched(
        self, command_handler, registry_guard
    ):
        exit_info = registry_guard["exit"]
        del registry_guard["shutdown"]
        del registry_guard["stop"]

        command_handler.set_cli_mode(True)
        # Early return: exit was not removed, no shutdown aliases added
        assert registry_guard["exit"] is exit_info
        assert "shutdown" not in registry_guard

        # But CLI mode is still set, so help hides the exit command
        command_handler.set_interactive_mode(True)
        result = await command_handler.execute("help")
        assert result.success is True
        assert "exit (q, quit)" not in result.message

    async def test_enable_twice_is_idempotent(self, command_handler, registry_guard):
        command_handler.set_cli_mode(True)
        command_handler.set_cli_mode(True)

        shutdown_info = registry_guard["shutdown"]
        assert registry_guard["exit"] is shutdown_info
        assert registry_guard["q"] is shutdown_info
        assert registry_guard["quit"] is shutdown_info
        # No duplicate aliases accumulated
        assert shutdown_info.aliases.count("exit") == 1
        assert shutdown_info.aliases.count("q") == 1
        assert shutdown_info.aliases.count("quit") == 1

    async def test_disable_without_enable_is_a_noop(self, command_handler, registry_guard):
        handler_mod._saved_exit_info = None
        exit_info = registry_guard["exit"]
        shutdown_aliases = list(registry_guard["shutdown"].aliases)

        command_handler.set_cli_mode(False)
        assert registry_guard["exit"] is exit_info
        assert registry_guard["q"] is exit_info
        assert registry_guard["quit"] is exit_info
        assert registry_guard["shutdown"].aliases == shutdown_aliases

    async def test_enable_skips_foreign_alias_binding(self, command_handler, registry_guard):
        # 'q' was rebound to some other command; enabling CLI mode must not
        # delete that binding while removing the exit command
        registry_guard["q"] = registry_guard["close"]

        command_handler.set_cli_mode(True)
        # exit/q/quit now alias shutdown (q was overwritten by the alias add)
        assert registry_guard["exit"] is registry_guard["shutdown"]
        assert registry_guard["q"] is registry_guard["shutdown"]

    async def test_disable_skips_foreign_alias_binding(self, command_handler, registry_guard):
        command_handler.set_cli_mode(True)
        # 'quit' gets rebound to another command while in CLI mode
        registry_guard["quit"] = registry_guard["close"]

        command_handler.set_cli_mode(False)
        # The foreign binding is preserved; exit is restored under its
        # remaining names
        assert registry_guard["quit"] is registry_guard["close"]
        assert registry_guard["exit"].name == "exit"
        assert registry_guard["q"] is registry_guard["exit"]


# ============================================================================
# get_help category handling (empty categories are skipped)
# ============================================================================


class TestGetHelpCategories:
    async def test_empty_category_omitted(self, command_handler, registry_guard):
        # Remove every simulation-category command from the registry
        for key in [k for k, info in registry_guard.items() if info.category == "simulation"]:
            del registry_guard[key]

        result = await command_handler.execute("help")
        assert result.success is True
        assert "Simulation:" not in result.message
        assert "Door Operations:" in result.message


# ============================================================================
# Instance subcommand registration (warning paths)
# ============================================================================


class TestInstanceSubcommandRegistration:
    async def test_unknown_parent_logs_warning(self, simulator, registry_guard, caplog):
        class OrphanHandler(CommandHandler):
            @subcommand("znoparent", "zorphan", description="Orphan")
            def zorphan(self):
                return CommandResult(True, "orphan")

        with caplog.at_level("WARNING", logger="powerpetdoor.simulator.commands.handler"):
            OrphanHandler(
                simulator=simulator,
                script_runner=ScriptRunner(simulator),
                stop_callback=MagicMock(),
            )
        assert "Parent command 'znoparent' not found for subcommand 'zorphan'" in caplog.text
        assert "znoparent" not in registry_guard

    async def test_missing_nested_path_logs_warning(self, simulator, registry_guard, caplog):
        class BadPathHandler(CommandHandler):
            @subcommand(["schedule", "zmissing"], "zbadsub", description="Bad path")
            def zbadsub(self):
                return CommandResult(True, "bad")

        with caplog.at_level("WARNING", logger="powerpetdoor.simulator.commands.handler"):
            BadPathHandler(
                simulator=simulator,
                script_runner=ScriptRunner(simulator),
                stop_callback=MagicMock(),
            )
        assert "Subcommand 'zmissing' not found in path ['schedule'] for 'zbadsub'" in caplog.text
        assert "zbadsub" not in registry_guard["schedule"].subcommands

    async def test_nested_parent_path_registers_subsubcommand(self, simulator, registry_guard):
        class DeepHandler(CommandHandler):
            @subcommand(["schedule", "add"], "zdeep", description="Nested sub-subcommand")
            def zdeep(self):
                return CommandResult(True, "deep ran")

        DeepHandler(
            simulator=simulator,
            script_runner=ScriptRunner(simulator),
            stop_callback=MagicMock(),
        )
        add_info = registry_guard["schedule"].subcommands["add"]
        assert "zdeep" in add_info.subcommands
        assert add_info.subcommands["zdeep"].description == "Nested sub-subcommand"

    async def test_parent_with_explicit_usage_not_regenerated(self, simulator, registry_guard):
        class HistorySubHandler(CommandHandler):
            @subcommand("history", "zhsub", description="Extra history sub")
            def zhsub(self):
                return CommandResult(True, "zhsub ran")

        HistorySubHandler(
            simulator=simulator,
            script_runner=ScriptRunner(simulator),
            stop_callback=MagicMock(),
        )
        history_info = registry_guard["history"]
        assert "zhsub" in history_info.subcommands
        # Explicit usage (auto_usage=False) is never overwritten
        assert history_info.usage == "[clear|N]"


# ============================================================================
# Module-level register_all_subcommands
# ============================================================================


class TestRegisterAllSubcommands:
    async def test_rerun_is_idempotent(self, registry_guard):
        schedule_add = registry_guard["schedule"].subcommands["add"]
        register_all_subcommands()
        # Already-registered subcommands are skipped, not replaced
        assert registry_guard["schedule"].subcommands["add"] is schedule_add

    async def test_skips_bad_metadata_and_non_callables(self, registry_guard):
        def zorphan_func(self):
            return CommandResult(True, "orphan")

        zorphan_func._subcommand_info = SubcommandInfo("zorphan2")
        zorphan_func._parent_path = ["znoparent2"]

        def zbadpath_func(self):
            return CommandResult(True, "bad")

        zbadpath_func._subcommand_info = SubcommandInfo("zbadsub2")
        zbadpath_func._parent_path = ["schedule", "zmissing2"]

        def zhistory_func(self):
            return CommandResult(True, "hist sub")

        zhistory_func._subcommand_info = SubcommandInfo("zhsub2")
        zhistory_func._parent_path = ["history"]

        def zdeep_func(self):
            return CommandResult(True, "deep")

        zdeep_func._subcommand_info = SubcommandInfo("zdeep2")
        zdeep_func._parent_path = ["schedule", "add"]

        CommandHandler.zdata_attr = 42  # Non-callable, skipped
        CommandHandler.zorphan_func = zorphan_func
        CommandHandler.zbadpath_func = zbadpath_func
        CommandHandler.zhistory_func = zhistory_func
        CommandHandler.zdeep_func = zdeep_func
        try:
            register_all_subcommands()

            assert "znoparent2" not in registry_guard
            assert "zbadsub2" not in registry_guard["schedule"].subcommands
            # The valid ones registered; explicit usage untouched
            assert "zhsub2" in registry_guard["history"].subcommands
            assert registry_guard["history"].usage == "[clear|N]"
            assert "zdeep2" in registry_guard["schedule"].subcommands["add"].subcommands
        finally:
            del CommandHandler.zdata_attr
            del CommandHandler.zorphan_func
            del CommandHandler.zbadpath_func
            del CommandHandler.zhistory_func
            del CommandHandler.zdeep_func


class TestNoTwoCommandsClaimTheSameWord:
    """A duplicate name or alias is resolved silently, in registration order.

    ``toggle`` was written with the alias ``t``, which ``holdtime`` already
    owned. Nothing complained: the registry is a flat dict, so the second
    registration simply lost, ``toggle`` had no short form, and typing ``t``
    set the hold time. Every other clash class in this tree fails loudly,
    and coverage cannot see this one at all - both commands are reachable by
    their full names, so every existing test still passed.
    """

    def test_every_command_word_is_claimed_exactly_once(self):
        registry = get_command_registry()

        claims: dict[str, list[str]] = {}
        for name, info in registry.items():
            # The registry stores each alias as its own key pointing at the
            # same CommandInfo, so walk the declared aliases instead.
            for word in (info.name, *info.aliases):
                claims.setdefault(word, [])
                if info.name not in claims[word]:
                    claims[word].append(info.name)
            assert name in (info.name, *info.aliases)

        contested = {word: owners for word, owners in claims.items() if len(owners) > 1}
        assert contested == {}
