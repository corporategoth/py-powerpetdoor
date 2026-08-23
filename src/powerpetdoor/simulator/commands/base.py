# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Base infrastructure for command handling.

This module provides the core types, decorators, and parsing utilities
used by all command handlers.
"""

import functools
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, cast

from ...i18n import t

#: Preserves the decorated function's exact signature through @command/@subcommand
F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class CommandResult:
    """Result of executing a command."""

    success: bool
    message: str
    data: dict | None = None


class BoolToggleCommandMixin:
    """Shared toggle-or-set helper for boolean state commands.

    Inherited by both ButtonCommandsMixin and SettingsCommandsMixin so the
    toggle/set/broadcast behavior is implemented exactly once (DRY).
    """

    simulator: Any  # DoorSimulator (loose annotation avoids a circular import)

    def _toggle_bool(
        self,
        attr: str,
        name: str,
        value: bool | None,
        fmt: str = "ON|OFF",
        broadcast_func: str | None = None,
    ) -> "CommandResult":
        """Toggle or set a boolean state attribute.

        Args:
            attr: The attribute name on the state object
            name: Display name for the setting
            value: True/False to set, None to toggle
            fmt: Format string for display ("ON|OFF" or "enabled|disabled")
            broadcast_func: Name of specific broadcast method to call on simulator
                           (e.g., "broadcast_safety_lock"). If None, no broadcast.
        """
        s = self.simulator.state
        if value is None:
            current = getattr(s, attr)
            setattr(s, attr, not current)
            new_val = not current
        else:
            setattr(s, attr, value)
            new_val = value

        if fmt == "enabled|disabled":
            state = "enabled" if new_val else "disabled"
        else:
            state = "ON" if new_val else "OFF"

        # Broadcast specific setting change to connected PPD clients
        if broadcast_func:
            func = getattr(self.simulator, broadcast_func, None)
            if func:
                func(new_val)

        return CommandResult(
            True, t("simulator.commands.base.text", "{name}: {state}", name=name, state=state)
        )


@dataclass
class ArgSpec:
    """Specification for a command argument.

    Defines the type, validation, and parsing rules for an argument.

    Attributes:
        name: Argument name for error messages and usage
        arg_type: Type of argument (string, int, float, bool_toggle, choice, time_range, days)
        required: Whether the argument is required
        default: Default value when not provided
        default_display: Human-readable form of the default for help text
                        (e.g., "all" instead of a raw Python list)
        choices: Valid choices for "choice" type
        description: Help text describing this argument, or a callable
                    returning it when the text depends on runtime policy
                    (see :meth:`describe`)
        min_value: Minimum value for numeric types
        max_value: Maximum value for numeric types
        completer: Optional callable for tab completion. Can have signature:
                  - completer() -> list[tuple[str, str]] - returns all completions
                  - completer(prefix: str) -> list[tuple[str, str]] - prefix-aware (for paths)
    """

    name: str
    arg_type: str  # "string", "int", "float", "bool_toggle", "choice", "time_range", "days"
    required: bool = True
    default: Any = None
    default_display: str | None = None
    choices: list[str] | None = None  # For "choice" type
    description: str | Callable[[], str] = ""
    min_value: float | None = None  # For int/float types
    max_value: float | None = None  # For int/float types
    completer: Callable[..., list[tuple[str, str]]] | None = None

    def describe(self) -> str:
        """Resolve this argument's help text.

        A description can depend on runtime policy the way ``completer``
        already does: ``run``'s ``script`` argument accepts names *and* file
        paths on the interactive CLI, but only bare names over the control
        channel, so a fixed string would advertise a form that channel
        refuses outright.
        """
        return self.description() if callable(self.description) else self.description

    def generate_usage(self) -> str:
        """Generate usage string for this argument."""
        if self.arg_type == "choice" and self.choices:
            inner = "|".join(self.choices)
        elif self.arg_type == "bool_toggle":
            inner = "on|off"
        elif self.arg_type == "time_range":
            inner = "start-end"
        elif self.arg_type == "days":
            inner = "days"
        else:
            inner = self.name

        if self.required:
            return f"<{inner}>"
        else:
            return f"[{inner}]"


# Standard bool toggle values
_BOOL_TRUE = ("on", "true", "1", "yes")
_BOOL_FALSE = ("off", "false", "0", "no")


def parse_arg(value: str, spec: ArgSpec) -> tuple[Any, str | None]:
    """Parse and validate an argument value.

    Returns:
        (parsed_value, error_message) - error_message is None on success
    """
    if spec.arg_type == "string":
        return value, None

    elif spec.arg_type == "int":
        try:
            parsed = int(value)
            # Validate limits
            if spec.min_value is not None and parsed < spec.min_value:
                return None, f"'{value}' is below minimum ({int(spec.min_value)})"
            if spec.max_value is not None and parsed > spec.max_value:
                return None, f"'{value}' is above maximum ({int(spec.max_value)})"
            return parsed, None
        except ValueError:
            return None, f"'{value}' is not a valid integer"

    elif spec.arg_type == "float":
        try:
            parsed_float = float(value)
            # `nan`/`inf`/`Infinity`/`1e400` are legal float() parses, and
            # every bound check below is False for nan, so without this guard
            # the bounds the command's own help advertises silently do not
            # hold. One `holdtime nan` then wedges the door and makes
            # GET_SETTINGS and GET_HOLD_TIME answer every connected client
            # `success:false` for the life of the daemon. The wire path
            # (protocol._require_finite) and the script DSL
            # (ScriptRunner._script_number) already refuse these; this is the
            # third front end onto the same state, and it uses the same
            # wording they do.
            #
            # The "int" branch needs no such guard: int("nan") and
            # int("1e400") both raise ValueError and are caught below.
            if not math.isfinite(parsed_float):
                return None, f"'{value}' must be a finite number"
            # Validate limits
            if spec.min_value is not None and parsed_float < spec.min_value:
                return None, f"'{value}' is below minimum ({spec.min_value})"
            if spec.max_value is not None and parsed_float > spec.max_value:
                return None, f"'{value}' is above maximum ({spec.max_value})"
            return parsed_float, None
        except ValueError:
            return None, f"'{value}' is not a valid number"

    elif spec.arg_type == "bool_toggle":
        v = value.lower()
        if v in _BOOL_TRUE:
            return True, None
        elif v in _BOOL_FALSE:
            return False, None
        else:
            return None, f"'{value}' is not valid. Use on/off"

    elif spec.arg_type == "choice":
        v = value.lower()
        if spec.choices:
            # Return the original case from choices
            for c in spec.choices:
                if c.lower() == v:
                    return c, None
        choices_str = ", ".join(spec.choices) if spec.choices else "none"
        return None, f"'{value}' is not valid. Choose from: {choices_str}"

    elif spec.arg_type == "time_range":
        # Parse HH:MM-HH:MM or H:MM-H:MM
        if "-" not in value:
            return None, "Time range must be in format <start>-<end> (e.g., 6:00-22:00)"
        try:
            start_str, end_str = value.split("-", 1)
            start_h, start_m = _parse_time_str(start_str)
            end_h, end_m = _parse_time_str(end_str)
            return (start_h, start_m, end_h, end_m), None
        except ValueError as e:
            return None, str(e)

    elif spec.arg_type == "days":
        # Parse day names or presets
        try:
            return _parse_days_str(value), None
        except ValueError as e:
            return None, str(e)

    else:
        return value, None


def _parse_time_str(time_str: str) -> tuple[int, int]:
    """Parse time string like '6:00' or '22:30' into (hour, minute)."""
    parts = time_str.strip().replace(".", ":").split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(
            t("simulator.commands.base.invalid_time", "Invalid time: {time_str}", time_str=time_str)
        )
    return hour, minute


# Day parsing constants. Booleans, not 1/0: this is the operator-facing
# Python layer, and the 1/0 wire spelling is applied once, at the
# serialization boundary in powerpetdoor.schedule.
DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
DAY_PRESETS: dict[str, list[bool]] = {
    "all": [True] * 7,
    "weekdays": [False, True, True, True, True, True, False],  # Mon-Fri
    "weekends": [True, False, False, False, False, False, True],  # Sun, Sat
}
# Public preset names for completion/highlighting
DAY_PRESET_NAMES = tuple(DAY_PRESETS)


def _parse_days_str(days_str: str) -> list[bool]:
    """Parse days string like 'mon,tue,wed' or 'weekdays' into flags."""
    days_str = days_str.lower().strip()
    if days_str in DAY_PRESETS:
        return DAY_PRESETS[days_str].copy()

    # Start with all days off
    days = [False] * 7
    for day in days_str.split(","):
        day = day.strip()[:3]  # Take first 3 chars
        if day in DAY_NAMES:
            days[DAY_NAMES.index(day)] = True
        else:
            raise ValueError(
                t(
                    "simulator.commands.base.unknown_day_use_all_weekdays",
                    "Unknown day: {day}. Use: {arg0} or all/weekdays/weekends",
                    day=day,
                    arg0=", ".join(DAY_NAMES),
                )
            )
    return days


def parse_args(
    parts: list[str],
    arg_specs: list[ArgSpec],
    cmd_path: list[str],
) -> tuple[list, "CommandResult | None"]:
    """Parse argument parts according to ArgSpec definitions.

    Shared by the daemon CommandHandler and the ctl local command handler.

    Returns:
        (parsed_args, error) - error is None on success. Extra (unconsumed)
        arguments are an error so typos are not silently ignored.
    """
    parsed = []
    cmd_str = " ".join(cmd_path)
    usage = " ".join(spec.generate_usage() for spec in arg_specs)

    if len(parts) > len(arg_specs):
        extra = " ".join(parts[len(arg_specs) :])
        return [], CommandResult(
            False,
            t(
                "simulator.commands.base.unexpected_argument_s_usage",
                "Unexpected argument(s): {extra}\nUsage: {cmd_str} {usage}",
                extra=extra,
                cmd_str=cmd_str,
                usage=usage,
            ),
        )

    for i, spec in enumerate(arg_specs):
        if i < len(parts):
            value, error = parse_arg(parts[i], spec)
            if error:
                return [], CommandResult(
                    False,
                    t(
                        "simulator.commands.base.usage",
                        "{error}\nUsage: {cmd_str} {usage}",
                        error=error,
                        cmd_str=cmd_str,
                        usage=usage,
                    ),
                )
            parsed.append(value)
        elif spec.required:
            return [], CommandResult(
                False,
                t(
                    "simulator.commands.base.missing_required_argument_usage",
                    "Missing required argument: {name}\nUsage: {cmd_str} {usage}",
                    name=spec.name,
                    cmd_str=cmd_str,
                    usage=usage,
                ),
            )
        else:
            parsed.append(spec.default)

    return parsed, None


@dataclass
class SubcommandInfo:
    """Metadata about a command or subcommand.

    Commands and subcommands share the same structure, allowing arbitrary nesting.
    Each can have its own handler, usage, description, and nested subcommands.
    """

    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    usage: str | None = None
    handler: Callable | None = None
    args: list[ArgSpec] = field(default_factory=list)  # Argument specifications
    # Nested subcommand registry: maps name and aliases to SubcommandInfo
    subcommands: dict[str, "SubcommandInfo"] = field(default_factory=dict)
    # True when usage was auto-generated (so it may be regenerated after
    # late subcommand registration)
    auto_usage: bool = False

    def __post_init__(self):
        """Build subcommand registry if list was provided."""
        # Allow passing a list of SubcommandInfo which gets converted to registry
        if isinstance(self.subcommands, list):
            self.subcommands = _build_subcommand_registry(self.subcommands)

    def generate_usage(self) -> str:
        """Generate usage string from args or subcommands."""
        if self.args:
            return " ".join(arg.generate_usage() for arg in self.args)
        elif self.subcommands:
            names = sorted(set(info.name for info in self.subcommands.values()))
            return "[" + "|".join(names) + "]" if names else ""
        return ""


@dataclass
class CommandInfo(SubcommandInfo):
    """Metadata about a top-level command.

    Extends SubcommandInfo with category for help grouping.
    """

    category: str = "misc"
    interactive_only: bool = False  # If True, command only works in interactive mode
    local_only: bool = False  # If True, command is handled locally by ctl, not sent to daemon


class _CommandFunc(Protocol):
    """A callable carrying the metadata attached by the @command decorator."""

    _command_info: CommandInfo

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


class _SubcommandFunc(Protocol):
    """A callable carrying the metadata attached by the @subcommand decorator."""

    _subcommand_info: SubcommandInfo
    _parent_path: list[str]

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


def _build_subcommand_registry(
    subcommand_list: list[SubcommandInfo],
) -> dict[str, SubcommandInfo]:
    """Build a subcommand registry from a list of SubcommandInfo objects."""
    registry = {}
    for sub in subcommand_list:
        registry[sub.name] = sub
        for alias in sub.aliases:
            registry[alias] = sub
    return registry


# Registry of commands (populated by decorator)
_command_registry: dict[str, CommandInfo] = {}


def get_command_registry() -> dict[str, CommandInfo]:
    """Get the global command registry."""
    return _command_registry


def get_canonical_command(line: str) -> str | None:
    """Get the canonical form of a command (replace aliases with full names).

    Handles command aliases (bc -> broadcast) and subcommand aliases at any depth
    (ac c -> ac connect, schedule del -> schedule delete).

    This function recursively resolves aliases through the subcommand hierarchy.

    Returns the canonical command string if any alias was replaced,
    or None if no replacement is needed.
    """
    parts = line.split()
    if not parts:
        return None

    modified = False
    cmd = parts[0].lower()

    # Resolve command alias
    if cmd not in _command_registry:
        return None

    info = _command_registry[cmd]
    if info.name != cmd:
        parts[0] = info.name
        modified = True

    # Recursively resolve subcommand aliases
    def resolve_subcommands(subcommand_registry: dict[str, SubcommandInfo], part_idx: int) -> bool:
        """Resolve subcommand at part_idx and recurse into nested subcommands."""
        nonlocal modified

        if part_idx >= len(parts) or not subcommand_registry:
            return False

        subcmd = parts[part_idx].lower()
        if subcmd not in subcommand_registry:
            return False

        subinfo = subcommand_registry[subcmd]
        if subinfo.name != subcmd:
            parts[part_idx] = subinfo.name
            modified = True

        # Recurse into nested subcommands
        if subinfo.subcommands:
            resolve_subcommands(subinfo.subcommands, part_idx + 1)

        return True

    # Start resolving from the first subcommand (index 1)
    resolve_subcommands(info.subcommands, 1)

    return " ".join(parts) if modified else None


def _generate_usage(info: SubcommandInfo) -> str:
    """Generate usage string from args or subcommands."""
    return info.generate_usage()


def command(
    name: str,
    aliases: list[str] | None = None,
    description: str = "",
    usage: str | None = None,
    category: str = "misc",
    subcommands: list[SubcommandInfo] | None = None,
    args: list[ArgSpec] | None = None,
    interactive_only: bool = False,
    local_only: bool = False,
) -> Callable[[F], F]:
    """Decorator to register a method as a command.

    Args:
        name: Primary command name
        aliases: Alternative names/shortcuts for the command
        description: Help text for the command
        usage: Usage string - auto-generated from args/subcommands if not provided
        category: Category for grouping in help output
        subcommands: List of SubcommandInfo for subcommand definitions
        args: List of ArgSpec for argument parsing
        interactive_only: If True, command only works in interactive mode
        local_only: If True, command is handled locally by ctl, not sent to daemon
    """

    def decorator(func: F) -> F:
        subcommand_registry = _build_subcommand_registry(subcommands) if subcommands else {}

        info = CommandInfo(
            name=name,
            aliases=aliases or [],
            description=description,
            usage=usage,  # Will be auto-generated if None
            category=category,
            handler=func,
            subcommands=subcommand_registry,
            args=args or [],
            interactive_only=interactive_only,
            local_only=local_only,
        )

        # Auto-generate usage if not explicitly provided
        if info.usage is None:
            info.usage = _generate_usage(info) or None
            info.auto_usage = True

        # Register under primary name and all aliases
        _command_registry[name] = info
        for alias in info.aliases:
            _command_registry[alias] = info

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        # Function attributes are untyped in general; the Protocol cast keeps
        # the metadata attachment type-checked without an ignore.
        cast(_CommandFunc, wrapper)._command_info = info
        return cast(F, wrapper)

    return decorator


def subcommand(
    parent_path: str | list[str],
    name: str,
    aliases: list[str] | None = None,
    description: str = "",
    usage: str | None = None,
    args: list[ArgSpec] | None = None,
) -> Callable[[F], F]:
    """Decorator to register a method as a subcommand handler.

    This decorator must be applied AFTER the parent command is registered.

    Args:
        parent_path: Name of the parent command, or list of names for nested subcommands
                     e.g., "schedule" or ["schedule", "add"] for deeper nesting
        name: Subcommand name
        aliases: Alternative names/shortcuts
        description: Help text
        usage: Usage string - auto-generated from args if not provided
        args: List of ArgSpec for argument parsing
    """

    def decorator(func: F) -> F:
        # Normalize parent_path to list
        path = [parent_path] if isinstance(parent_path, str) else list(parent_path)

        # Create subcommand info
        sub_info = SubcommandInfo(
            name=name,
            aliases=aliases or [],
            description=description,
            usage=usage,
            handler=func,
            args=args or [],
        )

        # Auto-generate usage if not explicitly provided
        if sub_info.usage is None:
            sub_info.usage = _generate_usage(sub_info) or None
            sub_info.auto_usage = True

        # Will be registered later when CommandHandler binds methods.
        # Function attributes are untyped in general; the Protocol cast keeps
        # the metadata attachment type-checked without an ignore.
        sub_func = cast(_SubcommandFunc, func)
        sub_func._subcommand_info = sub_info
        sub_func._parent_path = path
        return func

    return decorator
