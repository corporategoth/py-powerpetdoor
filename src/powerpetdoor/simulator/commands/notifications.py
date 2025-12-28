# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Notification settings commands."""

from typing import TYPE_CHECKING, Optional

from .base import ArgSpec, CommandResult, command, subcommand

if TYPE_CHECKING:
    from ..server import DoorSimulator


class NotifyCommandsMixin:
    """Mixin providing notification settings commands."""

    simulator: "DoorSimulator"

    # Notification definitions: (subcommand_name, state_attr, description, aliases)
    _NOTIFY_DEFS = [
        ("inside_on", "sensor_on_indoor", "Notify when inside sensor triggers", []),
        ("inside_off", "sensor_off_indoor", "Notify when inside sensor stops", []),
        ("outside_on", "sensor_on_outdoor", "Notify when outside sensor triggers", []),
        ("outside_off", "sensor_off_outdoor", "Notify when outside sensor stops", []),
        ("low_battery", "low_battery", "Notify on low battery", ["low_bat", "lowbat"]),
    ]

    def _set_notify(
        self, attr: str, name: str, value: Optional[bool]
    ) -> CommandResult:
        """Toggle or set a notification attribute.

        Args:
            attr: State attribute name
            name: Display name for the notification
            value: True/False to set, or None to toggle
        """
        s = self.simulator.state

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
        lines.append(f"  inside_on:   {'ON' if s.sensor_on_indoor else 'OFF'}")
        lines.append(f"  inside_off:  {'ON' if s.sensor_off_indoor else 'OFF'}")
        lines.append(f"  outside_on:  {'ON' if s.sensor_on_outdoor else 'OFF'}")
        lines.append(f"  outside_off: {'ON' if s.sensor_off_outdoor else 'OFF'}")
        lines.append(f"  low_battery: {'ON' if s.low_battery else 'OFF'}")
        return CommandResult(True, "\n".join(lines))

    @subcommand(
        "notify",
        "inside_on",
        [],
        "Notify when inside sensor triggers",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
    )
    def notify_inside_on(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set inside sensor on notification."""
        return self._set_notify("sensor_on_indoor", "inside_on", value)

    @subcommand(
        "notify",
        "inside_off",
        [],
        "Notify when inside sensor stops",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
    )
    def notify_inside_off(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set inside sensor off notification."""
        return self._set_notify("sensor_off_indoor", "inside_off", value)

    @subcommand(
        "notify",
        "outside_on",
        [],
        "Notify when outside sensor triggers",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
    )
    def notify_outside_on(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set outside sensor on notification."""
        return self._set_notify("sensor_on_outdoor", "outside_on", value)

    @subcommand(
        "notify",
        "outside_off",
        [],
        "Notify when outside sensor stops",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
    )
    def notify_outside_off(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set outside sensor off notification."""
        return self._set_notify("sensor_off_outdoor", "outside_off", value)

    @subcommand(
        "notify",
        "low_battery",
        ["low_bat", "lowbat"],
        "Notify on low battery",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
    )
    def notify_low_battery(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set low battery notification."""
        return self._set_notify("low_battery", "low_battery", value)
