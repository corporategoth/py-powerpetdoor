# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Settings management commands."""

import random
from typing import TYPE_CHECKING

from ...i18n import t
from ...tz_utils import get_available_timezones
from ..coerce import CoercionError
from ..values import VALUES, read_value, set_named_value
from .base import (
    ArgSpec,
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


class SettingsCommandsMixin:
    """Mixin providing settings management commands."""

    simulator: "DoorSimulator"

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
                # From the registry, not restated: `set hold_time 0` is
                # accepted everywhere else, and a 0.1 floor written out
                # here made this one surface narrower than the wire, the
                # DSL and the state document.
                min_value=VALUES["hold_time"].minimum,
                max_value=VALUES["hold_time"].maximum,
                description=(
                    f"Hold time in seconds ({VALUES['hold_time'].minimum:g}-"
                    f"{VALUES['hold_time'].maximum:g}), omit to show current value"
                ),
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
                    hold_time=read_value(self.simulator.state, "hold_time"),
                ),
            )
        try:
            set_named_value(self.simulator, "hold_time", seconds)
        except CoercionError as exc:
            return CommandResult(False, str(exc))
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
                    battery_percent=read_value(self.simulator.state, "battery"),
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
            current_rate = read_value(self.simulator.state, "charge_rate")
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
            current_rate = read_value(self.simulator.state, "discharge_rate")
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
        """Set or show the timezone.

        Takes either an IANA name (`America/New_York`) or a POSIX TZ
        string (`EST5EDT,M3.2.0,M11.1.0`), because both are things an
        operator has to hand. It is **stored** as POSIX either way: the
        door speaks POSIX and nothing else, so that is what a connected
        client sees whichever spelling was typed here.

        The conversion, and the refusal of anything that is neither,
        belong to the value's setter - which a script's `set timezone`
        and a state document reach too. This only reports the result.
        """
        from ...tz_utils import find_iana_for_posix, is_cache_initialized

        if tz is None:
            stored = str(read_value(self.simulator.state, "timezone"))
            # The rule is what is stored; the name is the readable part.
            iana = find_iana_for_posix(stored) if is_cache_initialized() else None
            display = f"{stored} ({iana})" if iana else stored
            return CommandResult(
                True,
                t("simulator.commands.settings.timezone", "Timezone: {display}", display=display),
            )

        try:
            set_named_value(self.simulator, "timezone", tz)
        except CoercionError as exc:
            return CommandResult(False, str(exc))

        stored = str(read_value(self.simulator.state, "timezone"))
        if stored == tz:
            return CommandResult(
                True, t("simulator.commands.settings.timezone_set", "Timezone set to {tz}", tz=tz)
            )
        return CommandResult(
            True,
            t(
                "simulator.commands.settings.timezone_set_2",
                "Timezone set to {tz} ({posix})",
                tz=tz,
                posix=stored,
            ),
        )
