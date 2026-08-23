# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Settings management commands."""

import math
import random
from typing import TYPE_CHECKING

from ...i18n import t
from ...tz_utils import get_available_timezones
from .base import (
    ArgSpec,
    BoolToggleCommandMixin,
    CommandResult,
    SubcommandInfo,
    command,
    subcommand,
)


def _timezone_completer() -> list[tuple[str, str]]:
    """Return list of (timezone_name, description) for tab completion."""
    timezones = get_available_timezones()
    if not timezones:
        return []
    # Return timezone names with empty descriptions (too many for descriptions)
    return [(tz, "") for tz in timezones]


if TYPE_CHECKING:
    from ..server import DoorSimulator


class SettingsCommandsMixin(BoolToggleCommandMixin):
    """Mixin providing settings management commands."""

    simulator: "DoorSimulator"

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
    def safety(self, value: bool | None = None) -> CommandResult:
        """Toggle or set safety lock."""
        return self._toggle_bool(
            "safety_lock", "Safety lock", value, broadcast_func="broadcast_safety_lock"
        )

    @subcommand("safety", "toggle", ["t"], "Toggle safety lock")
    def safety_toggle(self) -> CommandResult:
        """Toggle safety lock."""
        return self._toggle_bool(
            "safety_lock", "Safety lock", None, broadcast_func="broadcast_safety_lock"
        )

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
    def lockout(self, value: bool | None = None) -> CommandResult:
        """Toggle or set command lockout."""
        return self._toggle_bool(
            "cmd_lockout", "Command lockout", value, broadcast_func="broadcast_cmd_lockout"
        )

    @subcommand("lockout", "toggle", ["t"], "Toggle command lockout")
    def lockout_toggle(self) -> CommandResult:
        """Toggle command lockout."""
        return self._toggle_bool(
            "cmd_lockout", "Command lockout", None, broadcast_func="broadcast_cmd_lockout"
        )

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
    def autoretract(self, value: bool | None = None) -> CommandResult:
        """Toggle or set auto-retract."""
        return self._toggle_bool(
            "autoretract", "Auto-retract", value, broadcast_func="broadcast_autoretract"
        )

    @subcommand("autoretract", "toggle", ["t"], "Toggle auto-retract")
    def autoretract_toggle(self) -> CommandResult:
        """Toggle auto-retract."""
        return self._toggle_bool(
            "autoretract", "Auto-retract", None, broadcast_func="broadcast_autoretract"
        )

    @command(
        "holdtime",
        ["t"],
        "Set or show hold time in seconds",
        category="settings",
        args=[
            ArgSpec(
                "seconds",
                "float",
                required=False,
                min_value=0.1,
                max_value=900,
                description="Hold time in seconds (0.1-900), omit to show current value",
            )
        ],
    )
    def holdtime(self, seconds: float | None = None) -> CommandResult:
        """Set or show hold time.

        Validates *before* writing. The broadcast that follows converts to
        centiseconds (``int(hold_time * 100)``), so an unrepresentable value
        raised out of the handler *after* the assignment: the command printed
        ERROR having already corrupted the state it reported failing on, and
        every later ``GET_SETTINGS`` / ``GET_HOLD_TIME`` answered
        ``success:false``. ``parse_arg`` refuses non-finite input on the
        operator path, so this is the second layer - it also covers direct
        programmatic callers of the command mixin.
        """
        if seconds is None:
            return CommandResult(
                True,
                t(
                    "simulator.commands.settings.hold_time_s",
                    "Hold time: {hold_time}s",
                    hold_time=self.simulator.state.hold_time,
                ),
            )
        if not math.isfinite(seconds):
            return CommandResult(
                False,
                t(
                    "simulator.commands.settings.hold_time_must_finite_number",
                    "Hold time must be a finite number, got {seconds}",
                    seconds=seconds,
                ),
            )
        self.simulator.state.hold_time = seconds
        # Broadcast hold time change to connected PPD clients
        self.simulator.broadcast_hold_time()
        return CommandResult(
            True,
            t(
                "simulator.commands.settings.hold_time_set_s",
                "Hold time set to {seconds}s",
                seconds=seconds,
            ),
        )

    @command(
        "battery",
        ["b"],
        "Set or show battery level",
        category="settings",
        args=[
            ArgSpec(
                "percent",
                "int",
                required=False,
                min_value=0,
                max_value=100,
                description="Battery percentage (0-100), omit to show current value",
            )
        ],
        subcommands=[SubcommandInfo("random", [], "Set a random battery level (10-100)")],
    )
    def battery(self, percent: int | None = None) -> CommandResult:
        """Set or show battery level."""
        if percent is None:
            return CommandResult(
                True,
                t(
                    "simulator.commands.settings.battery_2",
                    "Battery: {battery_percent}%",
                    battery_percent=self.simulator.state.battery_percent,
                ),
            )
        pct = max(0, min(100, percent))
        self.simulator.set_battery(pct)
        return CommandResult(
            True, t("simulator.commands.settings.battery_set", "Battery set to {pct}%", pct=pct)
        )

    @subcommand("battery", "random", [], "Set a random battery level (10-100)")
    def battery_random(self) -> CommandResult:
        """Set a random battery level."""
        pct = random.randint(10, 100)
        self.simulator.set_battery(pct)
        return CommandResult(
            True, t("simulator.commands.settings.battery_set", "Battery set to {pct}%", pct=pct)
        )

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
        """Toggle AC power connection (default action).

        Phrased as "AC set to ..." rather than "AC: ...": the latter is how
        the read-only displays (`battery`, `holdtime`) phrase themselves, so
        a bare `ac` would otherwise look like it was showing rather than
        changing.
        """
        present = not self.simulator.state.ac_present
        self.simulator.set_ac_present(present)
        state = "connected" if present else "disconnected"
        return CommandResult(
            True, t("simulator.commands.settings.ac_set", "AC set to {state}", state=state)
        )

    @subcommand("ac", "connect", ["c"], "Connect AC power")
    def ac_connect(self) -> CommandResult:
        """Connect AC power."""
        self.simulator.set_ac_present(True)
        return CommandResult(
            True, t("simulator.commands.settings.ac_set_connected", "AC set to connected")
        )

    @subcommand("ac", "disconnect", ["d"], "Disconnect AC power")
    def ac_disconnect(self) -> CommandResult:
        """Disconnect AC power."""
        self.simulator.set_ac_present(False)
        return CommandResult(
            True, t("simulator.commands.settings.ac_set_disconnected", "AC set to disconnected")
        )

    @subcommand("ac", "toggle", ["t"], "Toggle AC connection")
    def ac_toggle(self) -> CommandResult:
        """Toggle AC connection."""
        present = not self.simulator.state.ac_present
        self.simulator.set_ac_present(present)
        state = "connected" if present else "disconnected"
        return CommandResult(
            True, t("simulator.commands.settings.ac_set", "AC set to {state}", state=state)
        )

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
    def battery_present(self, value: bool | None = None) -> CommandResult:
        """Toggle or set battery presence."""
        if value is None:
            present = not self.simulator.state.battery_present
        else:
            present = value
        self.simulator.set_battery_present(present)
        state = "installed" if present else "removed"
        return CommandResult(
            True, t("simulator.commands.settings.battery", "Battery: {state}", state=state)
        )

    @subcommand("battery_present", "toggle", ["t"], "Toggle battery presence")
    def battery_present_toggle(self) -> CommandResult:
        """Toggle battery presence."""
        present = not self.simulator.state.battery_present
        self.simulator.set_battery_present(present)
        state = "installed" if present else "removed"
        return CommandResult(
            True, t("simulator.commands.settings.battery", "Battery: {state}", state=state)
        )

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
    def charge_rate(self, rate: float | None = None) -> CommandResult:
        """Set battery charge rate in percent per minute."""
        if rate is not None:
            self.simulator.set_charge_rate(rate)
            if rate == 0:
                return CommandResult(
                    True, t("simulator.commands.settings.charging_disabled", "Charging disabled")
                )
            return CommandResult(
                True,
                t(
                    "simulator.commands.settings.charge_rate_min",
                    "Charge rate: {rate}%/min",
                    rate=rate,
                ),
            )
        else:
            current_rate = self.simulator.state.battery_config.charge_rate
            return CommandResult(
                True,
                t(
                    "simulator.commands.settings.charge_rate_min_1",
                    "Charge rate: {current_rate}%/min",
                    current_rate=current_rate,
                ),
            )

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
    def discharge_rate(self, rate: float | None = None) -> CommandResult:
        """Set battery discharge rate in percent per minute."""
        if rate is not None:
            self.simulator.set_discharge_rate(rate)
            if rate == 0:
                return CommandResult(
                    True,
                    t("simulator.commands.settings.discharging_disabled", "Discharging disabled"),
                )
            return CommandResult(
                True,
                t(
                    "simulator.commands.settings.discharge_rate_min",
                    "Discharge rate: {rate}%/min",
                    rate=rate,
                ),
            )
        else:
            current_rate = self.simulator.state.battery_config.discharge_rate
            return CommandResult(
                True,
                t(
                    "simulator.commands.settings.discharge_rate_min_1",
                    "Discharge rate: {current_rate}%/min",
                    current_rate=current_rate,
                ),
            )

    @command(
        "timezone",
        ["tz"],
        "Set or show timezone (IANA name or POSIX string)",
        category="settings",
        args=[
            ArgSpec(
                "tz",
                "string",
                required=False,
                description="Timezone (e.g., 'America/New_York' or 'EST5EDT,M3.2.0,M11.1.0')",
                completer=_timezone_completer,
            )
        ],
    )
    def timezone(self, tz: str | None = None) -> CommandResult:
        """Set or show timezone.

        Accepts either:
        - IANA timezone name (e.g., 'America/New_York')
        - POSIX TZ string (e.g., 'EST5EDT,M3.2.0,M11.1.0')
        """
        from ...tz_utils import (
            get_available_timezones,
            get_posix_tz_string,
            is_cache_initialized,
            parse_posix_tz_string,
        )

        if tz is None:
            # Show current timezone
            current = self.simulator.state.timezone
            display = current
            if is_cache_initialized():
                posix = get_posix_tz_string(current)
                if posix:
                    display = f"{current} ({posix})"
            return CommandResult(
                True,
                t("simulator.commands.settings.timezone", "Timezone: {display}", display=display),
            )

        # Validate and set timezone
        # Check if it's an IANA timezone
        if "/" in tz or tz in ("UTC", "GMT"):
            available = get_available_timezones()
            if available and tz not in available:
                return CommandResult(
                    False,
                    t(
                        "simulator.commands.settings.unknown_timezone",
                        "Unknown timezone: {tz}",
                        tz=tz,
                    ),
                )
            self.simulator.state.timezone = tz
            # Broadcast timezone change to connected PPD clients
            self.simulator.broadcast_timezone()
            posix = get_posix_tz_string(tz) if is_cache_initialized() else None
            if posix:
                return CommandResult(
                    True,
                    t(
                        "simulator.commands.settings.timezone_set_2",
                        "Timezone set to {tz} ({posix})",
                        tz=tz,
                        posix=posix,
                    ),
                )
            return CommandResult(
                True, t("simulator.commands.settings.timezone_set", "Timezone set to {tz}", tz=tz)
            )

        # Try to parse as POSIX TZ string
        parsed = parse_posix_tz_string(tz)
        if parsed and parsed.get("std_abbrev"):
            # Valid POSIX format - store directly
            self.simulator.state.timezone = tz
            # Broadcast timezone change to connected PPD clients
            self.simulator.broadcast_timezone()
            return CommandResult(
                True, t("simulator.commands.settings.timezone_set", "Timezone set to {tz}", tz=tz)
            )

        return CommandResult(
            False,
            t(
                "simulator.commands.settings.invalid_timezone_use_iana_name",
                "Invalid timezone: {tz}. Use IANA name (e.g., 'America/New_York') or POSIX string (e.g., 'EST5EDT,M3.2.0,M11.1.0')",
                tz=tz,
            ),
        )
