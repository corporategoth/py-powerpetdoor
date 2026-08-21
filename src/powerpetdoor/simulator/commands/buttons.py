# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Physical button toggle commands."""

from typing import TYPE_CHECKING

from .base import (
    ArgSpec,
    BoolToggleCommandMixin,
    CommandResult,
    SubcommandInfo,
    command,
    subcommand,
)

if TYPE_CHECKING:
    from ..server import DoorSimulator


class ButtonCommandsMixin(BoolToggleCommandMixin):
    """Mixin providing physical button toggle commands."""

    simulator: "DoorSimulator"

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
    def power(self, value: bool | None = None) -> CommandResult:
        """Toggle or set power state."""
        return self._toggle_bool("power", "Power", value, broadcast_func="broadcast_power")

    @subcommand("power", "toggle", ["t"], "Toggle power state")
    def power_toggle(self) -> CommandResult:
        """Toggle power state."""
        return self._toggle_bool("power", "Power", None, broadcast_func="broadcast_power")

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
    def auto(self, value: bool | None = None) -> CommandResult:
        """Toggle or set auto (schedule) mode."""
        return self._toggle_bool("auto", "Auto (schedule)", value, broadcast_func="broadcast_auto")

    @subcommand("auto", "toggle", ["t"], "Toggle auto mode")
    def auto_toggle(self) -> CommandResult:
        """Toggle auto mode."""
        return self._toggle_bool("auto", "Auto (schedule)", None, broadcast_func="broadcast_auto")

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
    def inside_enable(self, value: bool | None = None) -> CommandResult:
        """Toggle or set inside sensor enable."""
        return self._toggle_bool(
            "inside",
            "Inside sensor",
            value,
            "enabled|disabled",
            broadcast_func="broadcast_inside_sensor",
        )

    @subcommand("inside_enable", "toggle", ["t"], "Toggle inside sensor enable")
    def inside_enable_toggle(self) -> CommandResult:
        """Toggle inside sensor enable."""
        return self._toggle_bool(
            "inside",
            "Inside sensor",
            None,
            "enabled|disabled",
            broadcast_func="broadcast_inside_sensor",
        )

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
    def outside_enable(self, value: bool | None = None) -> CommandResult:
        """Toggle or set outside sensor enable."""
        return self._toggle_bool(
            "outside",
            "Outside sensor",
            value,
            "enabled|disabled",
            broadcast_func="broadcast_outside_sensor",
        )

    @subcommand("outside_enable", "toggle", ["t"], "Toggle outside sensor enable")
    def outside_enable_toggle(self) -> CommandResult:
        """Toggle outside sensor enable."""
        return self._toggle_bool(
            "outside",
            "Outside sensor",
            None,
            "enabled|disabled",
            broadcast_func="broadcast_outside_sensor",
        )
