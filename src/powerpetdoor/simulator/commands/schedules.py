# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Schedule management commands."""

from typing import TYPE_CHECKING

from ...i18n import t
from ...schedule import MAX_SCHEDULE_INDEX
from ..values import read_value
from .base import DAY_NAMES, DAY_PRESETS, ArgSpec, CommandResult, command, subcommand

if TYPE_CHECKING:
    from ..server import DoorSimulator
    from ..state import Schedule


class ScheduleCommandsMixin:
    """Mixin providing schedule management commands."""

    simulator: "DoorSimulator"
    _Schedule: type["Schedule"]

    def _format_time(self, hour: int, minute: int) -> str:
        """Format time as HH:MM."""
        return f"{hour:02d}:{minute:02d}"

    def _format_days(self, days: list[bool]) -> str:
        """Format days list as readable string."""
        if days == DAY_PRESETS["all"]:
            return "all days"
        if days == DAY_PRESETS["weekdays"]:
            return "weekdays"
        if days == DAY_PRESETS["weekends"]:
            return "weekends"
        active = [DAY_NAMES[i] for i, v in enumerate(days) if v]
        return ", ".join(active) if active else "none"

    @staticmethod
    def _format_sensor_scope(inside: bool, outside: bool) -> str:
        """Render which sensors a schedule covers.

        One helper for every surface that says it, so ``add``, ``list`` and
        the implicit-schedule line cannot drift into three spellings of the
        same concept. Plural throughout.
        """
        if inside and outside:
            return "inside and outside sensors"
        if inside:
            return "inside sensor"
        if outside:
            return "outside sensor"
        return "no sensors"

    def _format_schedule(self, schedule: "Schedule") -> str:
        """Format a schedule for display."""
        status = "enabled" if schedule.enabled else "disabled"
        days = self._format_days(schedule.days_of_week)
        time_start = self._format_time(schedule.start_hour, schedule.start_min)
        time_end = self._format_time(schedule.end_hour, schedule.end_min)
        sensor = self._format_sensor_scope(schedule.inside, schedule.outside)

        return f"  #{schedule.index}: {sensor}, {days}, {time_start}-{time_end} ({status})"

    @command("schedule", ["sched"], "Manage schedules", category="schedules")
    def schedule(self) -> CommandResult:
        """Show all schedules (default action when no subcommand given)."""
        return self.schedule_list()

    @subcommand("schedule", "list", [], "Show all schedules")
    def schedule_list(self) -> CommandResult:
        """List all schedules, showing implicit schedule if none configured."""
        schedules = self.simulator.get_schedules()
        if not schedules:
            # Show implicit schedule when none configured
            auto_status = "ON" if read_value(self.simulator.state, "auto") else "OFF"
            return CommandResult(
                True,
                t(
                    "simulator.commands.schedules.schedules_auto_mode_implicit_all",
                    "Schedules (auto mode {auto_status}):\n  (implicit): {arg0}, all days, 00:00-23:59",
                    auto_status=auto_status,
                    arg0=self._format_sensor_scope(True, True),
                ),
            )

        auto_status = "ON" if read_value(self.simulator.state, "auto") else "OFF"
        lines = [f"Schedules (auto mode {auto_status}):"]
        for idx in sorted(schedules.keys()):
            lines.append(self._format_schedule(schedules[idx]))
        return CommandResult(True, "\n".join(lines))

    @subcommand(
        "schedule",
        "add",
        [],
        "Add a new schedule",
        args=[
            ArgSpec(
                "sensor",
                "choice",
                choices=["inside", "outside", "both"],
                description="Which sensor(s) to enable",
            ),
            ArgSpec(
                "time",
                "time_range",
                description="Time window (e.g., 6:00-22:00)",
            ),
            ArgSpec(
                "days",
                "days",
                required=False,
                # A fresh list, not DAY_PRESETS["all"]: the parsed value is
                # handed straight to a Schedule and would otherwise alias the
                # shared preset.
                default=[True] * 7,
                default_display="all",
                description="Days (e.g., mon,tue,wed or all/weekdays/weekends)",
            ),
        ],
    )
    def schedule_add(
        self,
        sensor: str,
        time: tuple[int, int, int, int],
        days: list[bool],
    ) -> CommandResult:
        """Add a new schedule entry."""
        # Map sensor to inside/outside flags
        inside = sensor in ("inside", "both")
        outside = sensor in ("outside", "both")

        start_h, start_m, end_h, end_m = time

        # Find next available index. Capped at the same bound the wire path
        # enforces: an uncapped search silently created index 256, which
        # to_dict() then put on the wire and the simulator would itself
        # reject if a client sent it.
        existing = set(self.simulator.get_schedules())
        idx = 0
        while idx in existing:
            idx += 1
        if idx > MAX_SCHEDULE_INDEX:
            return CommandResult(
                False,
                t("simulator.commands.schedules.free_schedule_slots", "No free schedule slots"),
            )

        # Create schedule
        schedule = self._Schedule(
            index=idx,
            enabled=True,
            days_of_week=days,
            inside=inside,
            outside=outside,
            start_hour=start_h,
            start_min=start_m,
            end_hour=end_h,
            end_min=end_m,
        )
        self.simulator.add_schedule(schedule)

        return CommandResult(
            True,
            t(
                "simulator.commands.schedules.added_schedule",
                "Added schedule #{idx}: {arg0}, {arg1}, {arg2}-{arg3}",
                idx=idx,
                arg0=self._format_sensor_scope(inside, outside),
                arg1=self._format_days(days),
                arg2=self._format_time(start_h, start_m),
                arg3=self._format_time(end_h, end_m),
            ),
        )

    def _get_schedule(self, idx: int) -> "Schedule | CommandResult":
        """Get a schedule by index, or a failure CommandResult if not found."""
        if self.simulator.get_schedule(idx) is None:
            return CommandResult(
                False,
                t(
                    "simulator.commands.schedules.schedule_found",
                    "Schedule #{idx} not found",
                    idx=idx,
                ),
            )
        return self.simulator.get_schedules()[idx]

    @subcommand(
        "schedule",
        "clear",
        [],
        "Delete all schedules",
    )
    def schedule_clear(self) -> CommandResult:
        """Delete all schedules."""
        schedules = self.simulator.get_schedules()
        if not schedules:
            return CommandResult(
                True, t("simulator.commands.schedules.schedules_clear", "No schedules to clear")
            )

        count = len(schedules)
        self.simulator.set_schedules([])

        return CommandResult(
            True,
            t(
                "simulator.commands.schedules.cleared_schedule_s",
                "Cleared {count} schedule(s)",
                count=count,
            ),
        )

    @subcommand(
        "schedule",
        "delete",
        ["del", "rm", "remove"],
        "Delete a schedule",
        args=[
            ArgSpec(
                "index",
                "int",
                min_value=0,
                description="Schedule index",
            )
        ],
    )
    def schedule_delete(self, index: int) -> CommandResult:
        """Delete a schedule by index."""
        sched = self._get_schedule(index)
        if isinstance(sched, CommandResult):
            return sched
        self.simulator.remove_schedule(index)
        return CommandResult(
            True,
            t(
                "simulator.commands.schedules.deleted_schedule",
                "Deleted schedule #{index}",
                index=index,
            ),
        )

    @subcommand(
        "schedule",
        "enable",
        ["on"],
        "Enable a schedule",
        args=[
            ArgSpec(
                "index",
                "int",
                min_value=0,
                description="Schedule index",
            )
        ],
    )
    def schedule_enable(self, index: int) -> CommandResult:
        """Enable a schedule by index."""
        sched = self._get_schedule(index)
        if isinstance(sched, CommandResult):
            return sched
        sched.enabled = True
        self.simulator.broadcast_schedule(sched)
        return CommandResult(
            True,
            t(
                "simulator.commands.schedules.schedule_enabled",
                "Schedule #{index} enabled",
                index=index,
            ),
        )

    @subcommand(
        "schedule",
        "disable",
        ["off"],
        "Disable a schedule",
        args=[
            ArgSpec(
                "index",
                "int",
                min_value=0,
                description="Schedule index",
            )
        ],
    )
    def schedule_disable(self, index: int) -> CommandResult:
        """Disable a schedule by index."""
        sched = self._get_schedule(index)
        if isinstance(sched, CommandResult):
            return sched
        sched.enabled = False
        self.simulator.broadcast_schedule(sched)
        return CommandResult(
            True,
            t(
                "simulator.commands.schedules.schedule_disabled",
                "Schedule #{index} disabled",
                index=index,
            ),
        )

    @subcommand(
        "schedule",
        "days",
        [],
        "Set schedule days",
        args=[
            ArgSpec(
                "index",
                "int",
                min_value=0,
                description="Schedule index",
            ),
            ArgSpec(
                "days",
                "days",
                description="Days (e.g., mon,tue,wed or all/weekdays/weekends)",
            ),
        ],
    )
    def schedule_days(self, index: int, days: list[bool]) -> CommandResult:
        """Set the days for a schedule."""
        sched = self._get_schedule(index)
        if isinstance(sched, CommandResult):
            return sched
        sched.days_of_week = days
        self.simulator.broadcast_schedule(sched)
        return CommandResult(
            True,
            t(
                "simulator.commands.schedules.schedule_days",
                "Schedule #{index} days: {arg0}",
                index=index,
                arg0=self._format_days(days),
            ),
        )

    @subcommand(
        "schedule",
        "time",
        [],
        "Set schedule time window",
        args=[
            ArgSpec(
                "index",
                "int",
                min_value=0,
                description="Schedule index",
            ),
            ArgSpec(
                "time",
                "time_range",
                description="Time window (e.g., 6:00-22:00)",
            ),
        ],
    )
    def schedule_time(self, index: int, time: tuple[int, int, int, int]) -> CommandResult:
        """Set the time window for a schedule."""
        sched = self._get_schedule(index)
        if isinstance(sched, CommandResult):
            return sched
        start_h, start_m, end_h, end_m = time
        sched.start_hour = start_h
        sched.start_min = start_m
        sched.end_hour = end_h
        sched.end_min = end_m
        self.simulator.broadcast_schedule(sched)
        return CommandResult(
            True,
            t(
                "simulator.commands.schedules.schedule_time",
                "Schedule #{index} time: {arg0}-{arg1}",
                index=index,
                arg0=self._format_time(start_h, start_m),
                arg1=self._format_time(end_h, end_m),
            ),
        )
