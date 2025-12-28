# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Schedule management commands."""

from typing import TYPE_CHECKING, Optional

from .base import ArgSpec, CommandResult, DAY_NAMES, command, subcommand

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

    def _format_days(self, days: list) -> str:
        """Format days list as readable string."""
        if days == [1, 1, 1, 1, 1, 1, 1]:
            return "all days"
        if days == [0, 1, 1, 1, 1, 1, 0]:
            return "weekdays"
        if days == [1, 0, 0, 0, 0, 0, 1]:
            return "weekends"
        active = [DAY_NAMES[i] for i, v in enumerate(days) if v]
        return ", ".join(active) if active else "none"

    def _format_schedule(self, schedule: "Schedule") -> str:
        """Format a schedule for display."""
        status = "enabled" if schedule.enabled else "disabled"
        days = self._format_days(schedule.days_of_week)
        time_start = self._format_time(schedule.start_hour, schedule.start_min)
        time_end = self._format_time(schedule.end_hour, schedule.end_min)

        # Determine sensor type
        if schedule.inside and schedule.outside:
            sensor = "inside+outside"
        elif schedule.inside:
            sensor = "inside"
        elif schedule.outside:
            sensor = "outside"
        else:
            sensor = "none"

        return (
            f"  #{schedule.index}: {sensor} sensor, {days}, "
            f"{time_start}-{time_end} ({status})"
        )

    @command("schedule", ["sched"], "Manage schedules", category="schedules")
    def schedule(self) -> CommandResult:
        """Show all schedules (default action when no subcommand given)."""
        return self.schedule_list()

    @subcommand("schedule", "list", [], "Show all schedules")
    def schedule_list(self) -> CommandResult:
        """List all schedules, showing implicit schedule if none configured."""
        schedules = self.simulator.state.schedules
        if not schedules:
            # Show implicit schedule when none configured
            auto_status = "ON" if self.simulator.state.auto else "OFF"
            return CommandResult(
                True,
                f"Schedules (auto mode {auto_status}):\n"
                "  (implicit): both sensors, all days, 00:00-23:59",
            )

        auto_status = "ON" if self.simulator.state.auto else "OFF"
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
                default=[1, 1, 1, 1, 1, 1, 1],
                description="Days (e.g., mon,tue,wed or all/weekdays/weekends)",
            ),
        ],
    )
    def schedule_add(
        self,
        sensor: str,
        time: tuple[int, int, int, int],
        days: list[int],
    ) -> CommandResult:
        """Add a new schedule entry."""
        # Map sensor to inside/outside flags
        inside = sensor in ("inside", "both")
        outside = sensor in ("outside", "both")

        start_h, start_m, end_h, end_m = time

        # Find next available index
        existing = set(self.simulator.state.schedules.keys())
        idx = 0
        while idx in existing:
            idx += 1

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
            f"Added schedule #{idx}: {sensor} sensor, {self._format_days(days)}, "
            f"{self._format_time(start_h, start_m)}-{self._format_time(end_h, end_m)}",
        )

    def _get_schedule(
        self, idx: int
    ) -> tuple[Optional["Schedule"], Optional[CommandResult]]:
        """Get a schedule by index, returning error if not found."""
        if idx not in self.simulator.state.schedules:
            return None, CommandResult(False, f"Schedule #{idx} not found")
        return self.simulator.state.schedules[idx], None

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
        sched, err = self._get_schedule(index)
        if err:
            return err
        self.simulator.remove_schedule(index)
        return CommandResult(True, f"Deleted schedule #{index}")

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
        sched, err = self._get_schedule(index)
        if err:
            return err
        sched.enabled = True
        self.simulator.broadcast_schedule(sched)
        return CommandResult(True, f"Schedule #{index} enabled")

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
        sched, err = self._get_schedule(index)
        if err:
            return err
        sched.enabled = False
        self.simulator.broadcast_schedule(sched)
        return CommandResult(True, f"Schedule #{index} disabled")

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
    def schedule_days(self, index: int, days: list[int]) -> CommandResult:
        """Set the days for a schedule."""
        sched, err = self._get_schedule(index)
        if err:
            return err
        sched.days_of_week = days
        self.simulator.broadcast_schedule(sched)
        return CommandResult(True, f"Schedule #{index} days: {self._format_days(days)}")

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
    def schedule_time(
        self, index: int, time: tuple[int, int, int, int]
    ) -> CommandResult:
        """Set the time window for a schedule."""
        sched, err = self._get_schedule(index)
        if err:
            return err
        start_h, start_m, end_h, end_m = time
        sched.start_hour = start_h
        sched.start_min = start_m
        sched.end_hour = end_h
        sched.end_min = end_m
        self.simulator.broadcast_schedule(sched)
        return CommandResult(
            True,
            f"Schedule #{index} time: "
            f"{self._format_time(start_h, start_m)}-{self._format_time(end_h, end_m)}",
        )
