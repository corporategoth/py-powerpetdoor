# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Notification settings commands."""

from typing import TYPE_CHECKING

from .base import ArgSpec, CommandResult, command, subcommand

if TYPE_CHECKING:
    from ..server import DoorSimulator

#: ``(subcommand, state attribute, description, aliases)`` - the single
#: source of truth for the ``notify`` subcommands. The decorators, the
#: setter and ``notify``'s display all read it, so adding a notification is
#: one row. Do not respell these definitions in the decorators or the
#: display block - keep both reading this table.
_NOTIFY_DEFS: tuple[tuple[str, str, str, list[str]], ...] = (
    ("inside_on", "sensor_on_indoor", "Notify when inside sensor triggers", []),
    ("inside_off", "sensor_off_indoor", "Notify when inside sensor stops", []),
    ("outside_on", "sensor_on_outdoor", "Notify when outside sensor triggers", []),
    ("outside_off", "sensor_off_outdoor", "Notify when outside sensor stops", []),
    ("low_battery", "low_battery", "Notify on low battery", ["low_bat", "lowbat"]),
)

_NOTIFY_ATTR = {name: attr for name, attr, _, _ in _NOTIFY_DEFS}
_NOTIFY_DESC = {name: desc for name, _, desc, _ in _NOTIFY_DEFS}
_NOTIFY_ALIASES = {name: aliases for name, _, _, aliases in _NOTIFY_DEFS}
#: Label column width for the ``notify`` display, derived rather than hand-padded.
_NOTIFY_LABEL_WIDTH = max(len(name) for name in _NOTIFY_ATTR) + 1


def _notify_value_arg() -> list[ArgSpec]:
    """The ``on|off`` argument every notify subcommand takes."""
    return [
        ArgSpec(
            "value",
            "bool_toggle",
            required=False,
            description="on/off or omit to toggle",
        )
    ]


class NotifyCommandsMixin:
    """Mixin providing notification settings commands."""

    simulator: "DoorSimulator"

    def _set_notify(self, name: str, value: bool | None) -> CommandResult:
        """Toggle or set a notification attribute.

        Args:
            name: Subcommand name, which is also the display name and the
                key into :data:`_NOTIFY_DEFS`.
            value: True/False to set, or None to toggle
        """
        s = self.simulator.state
        attr = _NOTIFY_ATTR[name]

        if value is None:
            # Toggle
            current = getattr(s, attr)
            setattr(s, attr, not current)
            new_state = "ON" if not current else "OFF"
        else:
            setattr(s, attr, value)
            new_state = "ON" if value else "OFF"

        # Broadcast notification settings change to connected PPD clients
        self.simulator.broadcast_notification_settings()

        return CommandResult(True, f"Notification {name}: {new_state}")

    @command("notify", [], "Manage notification settings", category="settings")
    def notify(self) -> CommandResult:
        """Show all notification settings (default action)."""
        s = self.simulator.state
        lines = ["Notifications:"]
        for name, attr, _, _ in _NOTIFY_DEFS:
            state = "ON" if getattr(s, attr) else "OFF"
            lines.append(f"  {name + ':':<{_NOTIFY_LABEL_WIDTH}} {state}")
        return CommandResult(True, "\n".join(lines))

    @subcommand(
        "notify",
        "inside_on",
        _NOTIFY_ALIASES["inside_on"],
        _NOTIFY_DESC["inside_on"],
        args=_notify_value_arg(),
    )
    def notify_inside_on(self, value: bool | None = None) -> CommandResult:
        """Toggle or set inside sensor on notification."""
        return self._set_notify("inside_on", value)

    @subcommand(
        "notify",
        "inside_off",
        _NOTIFY_ALIASES["inside_off"],
        _NOTIFY_DESC["inside_off"],
        args=_notify_value_arg(),
    )
    def notify_inside_off(self, value: bool | None = None) -> CommandResult:
        """Toggle or set inside sensor off notification."""
        return self._set_notify("inside_off", value)

    @subcommand(
        "notify",
        "outside_on",
        _NOTIFY_ALIASES["outside_on"],
        _NOTIFY_DESC["outside_on"],
        args=_notify_value_arg(),
    )
    def notify_outside_on(self, value: bool | None = None) -> CommandResult:
        """Toggle or set outside sensor on notification."""
        return self._set_notify("outside_on", value)

    @subcommand(
        "notify",
        "outside_off",
        _NOTIFY_ALIASES["outside_off"],
        _NOTIFY_DESC["outside_off"],
        args=_notify_value_arg(),
    )
    def notify_outside_off(self, value: bool | None = None) -> CommandResult:
        """Toggle or set outside sensor off notification."""
        return self._set_notify("outside_off", value)

    @subcommand(
        "notify",
        "low_battery",
        _NOTIFY_ALIASES["low_battery"],
        _NOTIFY_DESC["low_battery"],
        args=_notify_value_arg(),
    )
    def notify_low_battery(self, value: bool | None = None) -> CommandResult:
        """Toggle or set low battery notification."""
        return self._set_notify("low_battery", value)
