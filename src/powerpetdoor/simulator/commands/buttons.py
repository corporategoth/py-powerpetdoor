# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Physical button toggle commands."""

from typing import TYPE_CHECKING, Optional

from .base import ArgSpec, CommandResult, SubcommandInfo, command, subcommand

if TYPE_CHECKING:
    from ..server import DoorSimulator


class ButtonCommandsMixin:
    """Mixin providing physical button toggle commands."""

    simulator: "DoorSimulator"

    def _toggle_bool(
        self, attr: str, name: str, value: Optional[bool], fmt: str = "ON|OFF",
        broadcast_func: Optional[str] = None
    ) -> CommandResult:
        """Toggle or set a boolean state attribute.

        Args:
            attr: State attribute name
            name: Display name for the setting
            value: True/False to set, or None to toggle
            fmt: Format for display - "ON|OFF" or "enabled|disabled"
            broadcast_func: Name of specific broadcast method to call on simulator
                           (e.g., "broadcast_power"). If None, no broadcast.
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

        return CommandResult(True, f"{name}: {state}")

    @command(
        "power",
        ["p"],
        "Toggle or set power",
        category="buttons",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle power state")],
    )
    def power(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set power state."""
        return self._toggle_bool("power", "Power", value,
                                 broadcast_func="broadcast_power")

    @subcommand("power", "toggle", ["t"], "Toggle power state")
    def power_toggle(self) -> CommandResult:
        """Toggle power state."""
        return self._toggle_bool("power", "Power", None,
                                 broadcast_func="broadcast_power")

    @command(
        "auto",
        ["m"],
        "Toggle or set auto/schedule mode",
        category="buttons",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle auto mode")],
    )
    def auto(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set auto (schedule) mode."""
        return self._toggle_bool("auto", "Auto (schedule)", value,
                                 broadcast_func="broadcast_auto")

    @subcommand("auto", "toggle", ["t"], "Toggle auto mode")
    def auto_toggle(self) -> CommandResult:
        """Toggle auto mode."""
        return self._toggle_bool("auto", "Auto (schedule)", None,
                                 broadcast_func="broadcast_auto")

    @command(
        "inside_enable",
        ["n"],
        "Toggle or set inside sensor enable",
        category="buttons",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle inside sensor enable")],
    )
    def inside_enable(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set inside sensor enable."""
        return self._toggle_bool("inside", "Inside sensor", value, "enabled|disabled",
                                 broadcast_func="broadcast_inside_sensor")

    @subcommand("inside_enable", "toggle", ["t"], "Toggle inside sensor enable")
    def inside_enable_toggle(self) -> CommandResult:
        """Toggle inside sensor enable."""
        return self._toggle_bool("inside", "Inside sensor", None, "enabled|disabled",
                                 broadcast_func="broadcast_inside_sensor")

    @command(
        "outside_enable",
        ["u"],
        "Toggle or set outside sensor enable",
        category="buttons",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle outside sensor enable")],
    )
    def outside_enable(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set outside sensor enable."""
        return self._toggle_bool("outside", "Outside sensor", value, "enabled|disabled",
                                 broadcast_func="broadcast_outside_sensor")

    @subcommand("outside_enable", "toggle", ["t"], "Toggle outside sensor enable")
    def outside_enable_toggle(self) -> CommandResult:
        """Toggle outside sensor enable."""
        return self._toggle_bool("outside", "Outside sensor", None, "enabled|disabled",
                                 broadcast_func="broadcast_outside_sensor")
