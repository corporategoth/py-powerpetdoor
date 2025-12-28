# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Settings management commands."""

import random
from typing import TYPE_CHECKING, Optional

from .base import ArgSpec, CommandResult, SubcommandInfo, command, subcommand

if TYPE_CHECKING:
    from ..server import DoorSimulator


class SettingsCommandsMixin:
    """Mixin providing settings management commands."""

    simulator: "DoorSimulator"

    def _toggle_bool(
        self, attr: str, name: str, value: Optional[bool], fmt: str = "ON|OFF",
        broadcast_func: Optional[str] = None
    ) -> CommandResult:
        """Toggle or set a boolean state attribute.

        Note: This method is also defined in ButtonCommandsMixin. Python's MRO
        ensures only one copy is used at runtime.

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

        return CommandResult(True, f"{name}: {state}")

    @command(
        "safety",
        ["s"],
        "Toggle or set outside sensor safety lock",
        category="settings",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle safety lock")],
    )
    def safety(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set safety lock."""
        return self._toggle_bool("safety_lock", "Safety lock", value,
                                 broadcast_func="broadcast_safety_lock")

    @subcommand("safety", "toggle", ["t"], "Toggle safety lock")
    def safety_toggle(self) -> CommandResult:
        """Toggle safety lock."""
        return self._toggle_bool("safety_lock", "Safety lock", None,
                                 broadcast_func="broadcast_safety_lock")

    @command(
        "lockout",
        ["l"],
        "Toggle or set command lockout",
        category="settings",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle command lockout")],
    )
    def lockout(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set command lockout."""
        return self._toggle_bool("cmd_lockout", "Command lockout", value,
                                 broadcast_func="broadcast_cmd_lockout")

    @subcommand("lockout", "toggle", ["t"], "Toggle command lockout")
    def lockout_toggle(self) -> CommandResult:
        """Toggle command lockout."""
        return self._toggle_bool("cmd_lockout", "Command lockout", None,
                                 broadcast_func="broadcast_cmd_lockout")

    @command(
        "autoretract",
        ["a"],
        "Toggle or set auto-retract",
        category="settings",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle auto-retract")],
    )
    def autoretract(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set auto-retract."""
        return self._toggle_bool("autoretract", "Auto-retract", value,
                                 broadcast_func="broadcast_autoretract")

    @subcommand("autoretract", "toggle", ["t"], "Toggle auto-retract")
    def autoretract_toggle(self) -> CommandResult:
        """Toggle auto-retract."""
        return self._toggle_bool("autoretract", "Auto-retract", None,
                                 broadcast_func="broadcast_autoretract")

    @command(
        "holdtime",
        ["t"],
        "Set hold time in seconds",
        category="settings",
        args=[
            ArgSpec(
                "seconds",
                "float",
                min_value=0.1,
                max_value=900,
                description="Hold time in seconds (0.1-900)",
            )
        ],
    )
    def holdtime(self, seconds: float) -> CommandResult:
        """Set hold time."""
        self.simulator.state.hold_time = seconds
        # Broadcast hold time change to connected PPD clients
        self.simulator.broadcast_hold_time()
        return CommandResult(True, f"Hold time set to {seconds}s")

    @command(
        "battery",
        ["b"],
        "Set battery level (random if no value)",
        category="settings",
        args=[
            ArgSpec(
                "percent",
                "int",
                required=False,
                min_value=0,
                max_value=100,
                description="Battery percentage (0-100)",
            )
        ],
    )
    def battery(self, percent: Optional[int] = None) -> CommandResult:
        """Set battery level."""
        if percent is None:
            percent = random.randint(10, 100)
        pct = max(0, min(100, percent))
        self.simulator.set_battery(pct)
        return CommandResult(True, f"Battery set to {pct}%")

    @command(
        "ac",
        [],
        "Toggle or set AC power connection",
        category="settings",
        subcommands=[
            SubcommandInfo("connect", ["c"], "Connect AC power"),
            SubcommandInfo("disconnect", ["d"], "Disconnect AC power"),
            SubcommandInfo("toggle", ["t"], "Toggle AC connection"),
        ],
    )
    def ac(self) -> CommandResult:
        """Toggle AC power connection (default action)."""
        present = not self.simulator.state.ac_present
        self.simulator.set_ac_present(present)
        state = "connected" if present else "disconnected"
        return CommandResult(True, f"AC: {state}")

    @subcommand("ac", "connect", ["c"], "Connect AC power")
    def ac_connect(self) -> CommandResult:
        """Connect AC power."""
        self.simulator.set_ac_present(True)
        return CommandResult(True, "AC: connected")

    @subcommand("ac", "disconnect", ["d"], "Disconnect AC power")
    def ac_disconnect(self) -> CommandResult:
        """Disconnect AC power."""
        self.simulator.set_ac_present(False)
        return CommandResult(True, "AC: disconnected")

    @subcommand("ac", "toggle", ["t"], "Toggle AC connection")
    def ac_toggle(self) -> CommandResult:
        """Toggle AC connection."""
        present = not self.simulator.state.ac_present
        self.simulator.set_ac_present(present)
        state = "connected" if present else "disconnected"
        return CommandResult(True, f"AC: {state}")

    @command(
        "battery_present",
        ["bp"],
        "Toggle or set battery presence",
        category="settings",
        args=[
            ArgSpec(
                "value",
                "bool_toggle",
                required=False,
                description="on/off or omit to toggle",
            )
        ],
        subcommands=[SubcommandInfo("toggle", ["t"], "Toggle battery presence")],
    )
    def battery_present(self, value: Optional[bool] = None) -> CommandResult:
        """Toggle or set battery presence."""
        if value is None:
            present = not self.simulator.state.battery_present
        else:
            present = value
        self.simulator.set_battery_present(present)
        state = "installed" if present else "removed"
        return CommandResult(True, f"Battery: {state}")

    @subcommand("battery_present", "toggle", ["t"], "Toggle battery presence")
    def battery_present_toggle(self) -> CommandResult:
        """Toggle battery presence."""
        present = not self.simulator.state.battery_present
        self.simulator.set_battery_present(present)
        state = "installed" if present else "removed"
        return CommandResult(True, f"Battery: {state}")

    @command(
        "charge_rate",
        ["cr"],
        "Set or show battery charge rate (%/min)",
        category="settings",
        args=[
            ArgSpec(
                "rate",
                "float",
                required=False,
                min_value=0,
                description="Charge rate in %/min (0 = disabled)",
            )
        ],
    )
    def charge_rate(self, rate: Optional[float] = None) -> CommandResult:
        """Set battery charge rate in percent per minute."""
        if rate is not None:
            self.simulator.set_charge_rate(rate)
            if rate == 0:
                return CommandResult(True, "Charging disabled")
            return CommandResult(True, f"Charge rate: {rate}%/min")
        else:
            current_rate = self.simulator.state.battery_config.charge_rate
            return CommandResult(True, f"Charge rate: {current_rate}%/min")

    @command(
        "discharge_rate",
        ["dcr"],
        "Set or show battery discharge rate (%/min)",
        category="settings",
        args=[
            ArgSpec(
                "rate",
                "float",
                required=False,
                min_value=0,
                description="Discharge rate in %/min (0 = disabled)",
            )
        ],
    )
    def discharge_rate(self, rate: Optional[float] = None) -> CommandResult:
        """Set battery discharge rate in percent per minute."""
        if rate is not None:
            self.simulator.set_discharge_rate(rate)
            if rate == 0:
                return CommandResult(True, "Discharging disabled")
            return CommandResult(True, f"Discharge rate: {rate}%/min")
        else:
            current_rate = self.simulator.state.battery_config.discharge_rate
            return CommandResult(True, f"Discharge rate: {current_rate}%/min")

    @command(
        "timezone",
        ["tz"],
        "Set or show timezone (IANA name or POSIX string)",
        category="settings",
        args=[
            ArgSpec(
                "tz",
                "str",
                required=False,
                description="Timezone (e.g., 'America/New_York' or 'EST5EDT,M3.2.0,M11.1.0')",
            )
        ],
    )
    def timezone(self, tz: Optional[str] = None) -> CommandResult:
        """Set or show timezone.

        Accepts either:
        - IANA timezone name (e.g., 'America/New_York')
        - POSIX TZ string (e.g., 'EST5EDT,M3.2.0,M11.1.0')
        """
        from ...tz_utils import (
            get_available_timezones,
            parse_posix_tz_string,
            get_posix_tz_string,
            is_cache_initialized,
        )

        if tz is None:
            # Show current timezone
            current = self.simulator.state.timezone
            display = current
            if is_cache_initialized():
                posix = get_posix_tz_string(current)
                if posix:
                    display = f"{current} ({posix})"
            return CommandResult(True, f"Timezone: {display}")

        # Validate and set timezone
        # Check if it's an IANA timezone
        if "/" in tz or tz in ("UTC", "GMT"):
            available = get_available_timezones()
            if available and tz not in available:
                return CommandResult(False, f"Unknown timezone: {tz}")
            self.simulator.state.timezone = tz
            # Broadcast timezone change to connected PPD clients
            self.simulator.broadcast_timezone()
            posix = get_posix_tz_string(tz) if is_cache_initialized() else None
            if posix:
                return CommandResult(True, f"Timezone set to {tz} ({posix})")
            return CommandResult(True, f"Timezone set to {tz}")

        # Try to parse as POSIX TZ string
        parsed = parse_posix_tz_string(tz)
        if parsed and parsed.get("std_abbrev"):
            # Valid POSIX format - store directly
            self.simulator.state.timezone = tz
            # Broadcast timezone change to connected PPD clients
            self.simulator.broadcast_timezone()
            return CommandResult(True, f"Timezone set to {tz}")

        return CommandResult(
            False,
            f"Invalid timezone: {tz}. Use IANA name (e.g., 'America/New_York') "
            "or POSIX string (e.g., 'EST5EDT,M3.2.0,M11.1.0')"
        )
