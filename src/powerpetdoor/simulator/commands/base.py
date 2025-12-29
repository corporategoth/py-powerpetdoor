# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Base infrastructure for command handling.

This module provides the core types, decorators, and parsing utilities
used by all command handlers.
"""

import functools
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class CommandResult:
    """Result of executing a command."""

    success: bool
    message: str
    data: Optional[dict] = None


@dataclass
class ArgSpec:
    """Specification for a command argument.

    Defines the type, validation, and parsing rules for an argument.

    Attributes:
        name: Argument name for error messages and usage
        arg_type: Type of argument (string, int, float, bool_toggle, choice, time_range, days)
        required: Whether the argument is required
        default: Default value when not provided
        choices: Valid choices for "choice" type
        description: Help text describing this argument
        min_value: Minimum value for numeric types
        max_value: Maximum value for numeric types
        completer: Optional callable for tab completion. Can have signature:
                  - completer() -> list[tuple[str, str]] - returns all completions
                  - completer(prefix: str) -> list[tuple[str, str]] - prefix-aware (for paths)
    """

    name: str
    arg_type: str  # "string", "int", "float", "bool_toggle", "choice", "time_range", "days"
    required: bool = True
    default: any = None
    choices: Optional[list[str]] = None  # For "choice" type
    description: str = ""
    min_value: Optional[float] = None  # For int/float types
    max_value: Optional[float] = None  # For int/float types
    completer: Optional[Callable[..., list[tuple[str, str]]]] = None

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


def parse_arg(value: str, spec: ArgSpec) -> tuple[any, Optional[str]]:
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
            parsed = float(value)
            # Validate limits
            if spec.min_value is not None and parsed < spec.min_value:
                return None, f"'{value}' is below minimum ({spec.min_value})"
            if spec.max_value is not None and parsed > spec.max_value:
                return None, f"'{value}' is above maximum ({spec.max_value})"
            return parsed, None
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
        if spec.choices and v in [c.lower() for c in spec.choices]:
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
        raise ValueError(f"Invalid time: {time_str}")
    return hour, minute


# Day parsing constants
DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_DAY_PRESETS = {
    "all": [1, 1, 1, 1, 1, 1, 1],
    "weekdays": [0, 1, 1, 1, 1, 1, 0],  # Mon-Fri
    "weekends": [1, 0, 0, 0, 0, 0, 1],  # Sun, Sat
}


def _parse_days_str(days_str: str) -> list[int]:
    """Parse days string like 'mon,tue,wed' or 'weekdays' into list."""
    days_str = days_str.lower().strip()
    if days_str in _DAY_PRESETS:
        return _DAY_PRESETS[days_str].copy()

    # Start with all days off
    days = [0, 0, 0, 0, 0, 0, 0]
    for day in days_str.split(","):
        day = day.strip()[:3]  # Take first 3 chars
        if day in DAY_NAMES:
            days[DAY_NAMES.index(day)] = 1
        else:
            raise ValueError(
                f"Unknown day: {day}. Use: {', '.join(DAY_NAMES)} or all/weekdays/weekends"
            )
    return days


@dataclass
class SubcommandInfo:
    """Metadata about a command or subcommand.

    Commands and subcommands share the same structure, allowing arbitrary nesting.
    Each can have its own handler, usage, description, and nested subcommands.
    """

    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    usage: Optional[str] = None
    handler: Optional[Callable] = None
    args: list[ArgSpec] = field(default_factory=list)  # Argument specifications
    # Nested subcommand registry: maps name and aliases to SubcommandInfo
    subcommands: dict[str, "SubcommandInfo"] = field(default_factory=dict)

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
    def resolve_subcommands(
        subcommand_registry: dict[str, SubcommandInfo], part_idx: int
    ) -> bool:
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
    aliases: Optional[list[str]] = None,
    description: str = "",
    usage: Optional[str] = None,
    category: str = "misc",
    subcommands: Optional[list[SubcommandInfo]] = None,
    args: Optional[list[ArgSpec]] = None,
    interactive_only: bool = False,
    local_only: bool = False,
):
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

    def decorator(func: Callable) -> Callable:
        subcommand_registry = (
            _build_subcommand_registry(subcommands) if subcommands else {}
        )

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

        # Register under primary name and all aliases
        _command_registry[name] = info
        for alias in info.aliases:
            _command_registry[alias] = info

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper._command_info = info
        return wrapper

    return decorator


def subcommand(
    parent_path: str | list[str],
    name: str,
    aliases: Optional[list[str]] = None,
    description: str = "",
    usage: Optional[str] = None,
    args: Optional[list[ArgSpec]] = None,
):
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

    def decorator(func: Callable) -> Callable:
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

        # Will be registered later when CommandHandler binds methods
        func._subcommand_info = sub_info
        func._parent_path = path
        return func

    return decorator
