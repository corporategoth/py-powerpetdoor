"""Unified command handler for the Power Pet Door simulator.

This module provides a command dispatcher that can be used by:
- Interactive keyboard input
- Control port connections
- Scripts
- Direct Python API calls
"""

import asyncio
import functools
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .server import DoorSimulator
    from .scripting import ScriptRunner

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result of executing a command."""
    success: bool
    message: str
    data: Optional[dict] = None


@dataclass
class CommandInfo:
    """Metadata about a registered command."""
    name: str
    aliases: list[str]
    description: str
    usage: Optional[str]
    category: str
    handler: Callable


# Registry of commands (populated by decorator)
_command_registry: dict[str, CommandInfo] = {}


def command(
    name: str,
    aliases: Optional[list[str]] = None,
    description: str = "",
    usage: Optional[str] = None,
    category: str = "misc",
):
    """Decorator to register a method as a command.

    Args:
        name: Primary command name
        aliases: Alternative names/shortcuts for the command
        description: Help text for the command
        usage: Usage string (e.g., "<seconds>")
        category: Category for grouping in help output
    """
    def decorator(func: Callable) -> Callable:
        info = CommandInfo(
            name=name,
            aliases=aliases or [],
            description=description,
            usage=usage,
            category=category,
            handler=func,
        )
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


class CommandHandler:
    """Handles commands for the simulator.

    Provides a unified interface for controlling the simulator from
    interactive input, control port, or scripts.

    Commands can be invoked:
    - Via execute() with a command string
    - Directly as methods (e.g., handler.inside(), handler.power(True))
    """

    def __init__(
        self,
        simulator: "DoorSimulator",
        script_runner: "ScriptRunner",
        stop_callback: Callable[[], None],
        script_queue: Optional[asyncio.Queue] = None,
    ):
        """Initialize the command handler.

        Args:
            simulator: The door simulator instance
            script_runner: The script runner instance
            stop_callback: Function to call to stop the simulator
            script_queue: Optional queue for queueing scripts
        """
        self.simulator = simulator
        self.script_runner = script_runner
        self.stop_callback = stop_callback
        self.script_queue = script_queue

        # Import here to avoid circular imports
        from .scripting import Script, get_builtin_script, list_builtin_scripts
        from .state import Schedule
        self._Script = Script
        self._get_builtin_script = get_builtin_script
        self._list_builtin_scripts = list_builtin_scripts
        self._Schedule = Schedule

    def load_script(self, script_ref: str):
        """Load a script - auto-detect if it's a file path or built-in name."""
        path = Path(script_ref)
        if path.exists():
            return self._Script.from_file(path)
        else:
            return self._get_builtin_script(script_ref)

    async def execute(self, command_str: str) -> CommandResult:
        """Execute a command string and return the result.

        Args:
            command_str: The command string to execute (e.g., "inside", "power on")

        Returns:
            CommandResult with success status and message
        """
        if not command_str:
            return CommandResult(False, "Empty command")

        parts = command_str.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else None

        # Look up command in registry
        if cmd not in _command_registry:
            return CommandResult(False, f"Unknown command: {cmd}. Type 'help' for commands.")

        info = _command_registry[cmd]
        handler = getattr(self, info.handler.__name__)

        # Call the handler
        try:
            result = handler(arg)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as e:
            return CommandResult(False, f"Error: {e}")

    # -------------------------------------------------------------------------
    # Door Operations
    # -------------------------------------------------------------------------

    @command("inside", ["i"], "Trigger inside sensor (pet going out)", category="door")
    def inside(self, arg: Optional[str] = None) -> CommandResult:
        """Trigger the inside sensor."""
        self.simulator.trigger_sensor("inside")
        return CommandResult(True, "Inside sensor triggered (pet going out)")

    @command("outside", ["o"], "Trigger outside sensor (pet coming in)", category="door")
    def outside(self, arg: Optional[str] = None) -> CommandResult:
        """Trigger the outside sensor."""
        self.simulator.trigger_sensor("outside")
        return CommandResult(True, "Outside sensor triggered (pet coming in)")

    @command("close", ["c"], "Close the door", category="door")
    def close(self, arg: Optional[str] = None) -> CommandResult:
        """Close the door."""
        asyncio.create_task(self.simulator.close_door())
        return CommandResult(True, "Closing door")

    @command("hold", ["h", "open"], "Open and hold the door", category="door")
    def hold(self, arg: Optional[str] = None) -> CommandResult:
        """Open the door and hold it open."""
        asyncio.create_task(self.simulator.open_door(hold=True))
        return CommandResult(True, "Opening and holding")

    # -------------------------------------------------------------------------
    # Simulation Events
    # -------------------------------------------------------------------------

    @command("obstruction", ["x"], "Simulate obstruction (triggers auto-retract)", category="simulation")
    def obstruction(self, arg: Optional[str] = None) -> CommandResult:
        """Simulate an obstruction."""
        self.simulator.simulate_obstruction()
        return CommandResult(True, "Simulating obstruction")

    @command("pet", ["d"], "Toggle pet in doorway", category="simulation")
    def pet(self, arg: Optional[str] = None) -> CommandResult:
        """Toggle pet presence in doorway."""
        self.simulator.state.pet_in_doorway = not self.simulator.state.pet_in_doorway
        state = "present" if self.simulator.state.pet_in_doorway else "gone"
        return CommandResult(True, f"Pet in doorway: {state}")

    # -------------------------------------------------------------------------
    # Physical Button Toggles
    # -------------------------------------------------------------------------

    @command("power", ["p"], "Toggle or set power", "[on|off]", category="buttons")
    def power(self, arg: Optional[str] = None) -> CommandResult:
        """Toggle or set power state."""
        if arg is not None:
            self.simulator.state.power = arg.lower() in ("on", "true", "1", "yes")
        else:
            self.simulator.state.power = not self.simulator.state.power
        state = "ON" if self.simulator.state.power else "OFF"
        return CommandResult(True, f"Power: {state}")

    @command("auto", ["m"], "Toggle or set auto/schedule mode", "[on|off]", category="buttons")
    def auto(self, arg: Optional[str] = None) -> CommandResult:
        """Toggle or set auto (schedule) mode."""
        if arg is not None:
            self.simulator.state.auto = arg.lower() in ("on", "true", "1", "yes")
        else:
            self.simulator.state.auto = not self.simulator.state.auto
        state = "ON" if self.simulator.state.auto else "OFF"
        return CommandResult(True, f"Auto (schedule): {state}")

    @command("inside_enable", ["n"], "Toggle or set inside sensor enable", "[on|off]", category="buttons")
    def inside_enable(self, arg: Optional[str] = None) -> CommandResult:
        """Toggle or set inside sensor enable."""
        if arg is not None:
            self.simulator.state.inside = arg.lower() in ("on", "true", "1", "yes")
        else:
            self.simulator.state.inside = not self.simulator.state.inside
        state = "enabled" if self.simulator.state.inside else "disabled"
        return CommandResult(True, f"Inside sensor: {state}")

    @command("outside_enable", ["u"], "Toggle or set outside sensor enable", "[on|off]", category="buttons")
    def outside_enable(self, arg: Optional[str] = None) -> CommandResult:
        """Toggle or set outside sensor enable."""
        if arg is not None:
            self.simulator.state.outside = arg.lower() in ("on", "true", "1", "yes")
        else:
            self.simulator.state.outside = not self.simulator.state.outside
        state = "enabled" if self.simulator.state.outside else "disabled"
        return CommandResult(True, f"Outside sensor: {state}")

    # -------------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------------

    @command("safety", ["s"], "Toggle or set outside sensor safety lock", "[on|off]", category="settings")
    def safety(self, arg: Optional[str] = None) -> CommandResult:
        """Toggle or set safety lock."""
        if arg is not None:
            self.simulator.state.safety_lock = arg.lower() in ("on", "true", "1", "yes")
        else:
            self.simulator.state.safety_lock = not self.simulator.state.safety_lock
        state = "ON" if self.simulator.state.safety_lock else "OFF"
        return CommandResult(True, f"Safety lock: {state}")

    @command("lockout", ["l"], "Toggle or set command lockout", "[on|off]", category="settings")
    def lockout(self, arg: Optional[str] = None) -> CommandResult:
        """Toggle or set command lockout."""
        if arg is not None:
            self.simulator.state.cmd_lockout = arg.lower() in ("on", "true", "1", "yes")
        else:
            self.simulator.state.cmd_lockout = not self.simulator.state.cmd_lockout
        state = "ON" if self.simulator.state.cmd_lockout else "OFF"
        return CommandResult(True, f"Command lockout: {state}")

    @command("autoretract", ["a"], "Toggle or set auto-retract", "[on|off]", category="settings")
    def autoretract(self, arg: Optional[str] = None) -> CommandResult:
        """Toggle or set auto-retract."""
        if arg is not None:
            self.simulator.state.autoretract = arg.lower() in ("on", "true", "1", "yes")
        else:
            self.simulator.state.autoretract = not self.simulator.state.autoretract
        state = "ON" if self.simulator.state.autoretract else "OFF"
        return CommandResult(True, f"Auto-retract: {state}")

    @command("holdtime", ["t"], "Set hold time in seconds", "<seconds>", category="settings")
    def holdtime(self, arg: Optional[str] = None) -> CommandResult:
        """Set hold time."""
        if arg and arg.isdigit():
            seconds = int(arg)
            self.simulator.state.hold_time = seconds
            return CommandResult(True, f"Hold time set to {seconds}s")
        else:
            return CommandResult(False, "Usage: holdtime <seconds>")

    @command("battery", ["b"], "Set battery level (random if no value)", "[percent]", category="settings")
    def battery(self, arg: Optional[str] = None) -> CommandResult:
        """Set battery level."""
        if arg and arg.isdigit():
            pct = max(0, min(100, int(arg)))
        else:
            pct = random.randint(10, 100)
        self.simulator.set_battery(pct)
        return CommandResult(True, f"Battery set to {pct}%")

    # -------------------------------------------------------------------------
    # Schedules
    # -------------------------------------------------------------------------

    # Day name mappings (index in protocol list: [Sun, Mon, Tue, Wed, Thu, Fri, Sat])
    _DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    _DAY_PRESETS = {
        "all": [1, 1, 1, 1, 1, 1, 1],
        "weekdays": [0, 1, 1, 1, 1, 1, 0],  # Mon-Fri
        "weekends": [1, 0, 0, 0, 0, 0, 1],  # Sun, Sat
    }

    def _format_time(self, hour: int, minute: int) -> str:
        """Format time as HH:MM."""
        return f"{hour:02d}:{minute:02d}"

    def _parse_time(self, time_str: str) -> tuple[int, int]:
        """Parse time string like '6:00' or '22:30' into (hour, minute)."""
        parts = time_str.replace(".", ":").split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid time: {time_str}")
        return hour, minute

    def _parse_time_range(self, time_range: str) -> tuple[int, int, int, int]:
        """Parse time range like '6:00-22:00' into (start_h, start_m, end_h, end_m)."""
        if "-" not in time_range:
            raise ValueError("Time range must be in format <start>-<end>")
        start_str, end_str = time_range.split("-", 1)
        start_h, start_m = self._parse_time(start_str)
        end_h, end_m = self._parse_time(end_str)
        return start_h, start_m, end_h, end_m

    def _parse_days(self, days_str: str) -> list:
        """Parse days string like 'mon,tue,wed' or 'weekdays' into list."""
        days_str = days_str.lower().strip()
        if days_str in self._DAY_PRESETS:
            return self._DAY_PRESETS[days_str].copy()

        # Start with all days off
        days = [0, 0, 0, 0, 0, 0, 0]
        for day in days_str.split(","):
            day = day.strip()[:3]  # Take first 3 chars
            if day in self._DAY_NAMES:
                days[self._DAY_NAMES.index(day)] = 1
            else:
                raise ValueError(f"Unknown day: {day}")
        return days

    def _format_days(self, days: list) -> str:
        """Format days list as readable string."""
        if days == [1, 1, 1, 1, 1, 1, 1]:
            return "all days"
        if days == [0, 1, 1, 1, 1, 1, 0]:
            return "weekdays"
        if days == [1, 0, 0, 0, 0, 0, 1]:
            return "weekends"
        active = [self._DAY_NAMES[i] for i, v in enumerate(days) if v]
        return ", ".join(active) if active else "none"

    def _format_schedule(self, schedule) -> str:
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

    @command("schedule", ["sched"], "Manage schedules", "[subcommand]", category="schedules")
    def schedule(self, arg: Optional[str] = None) -> CommandResult:
        """Show or manage schedules.

        Each schedule entry controls ONE sensor (inside or outside) for specific
        days and a time window.

        Subcommands:
            schedule                              - Show all schedules
            schedule add <sensor> <time> [days]   - Add schedule (sensor: inside/outside)
            schedule del <index>                  - Delete schedule by index
            schedule on/off <index>               - Enable/disable schedule
            schedule days <index> <days>          - Set days (mon,tue,wed / weekdays / all)
            schedule time <index> <start>-<end>   - Set time window

        Examples:
            schedule add inside 6:00-20:00            - Inside sensor, all days
            schedule add outside 8:00-18:00 weekdays  - Outside sensor, Mon-Fri
            schedule add inside 7:00-22:00 mon,wed,fri
            schedule del 0
            schedule days 1 weekends
            schedule time 0 9:00-17:00
        """
        if not arg:
            # Show all schedules
            schedules = self.simulator.state.schedules
            if not schedules:
                return CommandResult(True, "No schedules configured")
            lines = ["Schedules:"]
            for idx in sorted(schedules.keys()):
                lines.append(self._format_schedule(schedules[idx]))
            return CommandResult(True, "\n".join(lines))

        parts = arg.split()
        subcmd = parts[0].lower()
        subargs = parts[1:]

        if subcmd == "add":
            return self._schedule_add(subargs)

        elif subcmd in ("del", "delete", "rm", "remove"):
            if not subargs or not subargs[0].isdigit():
                return CommandResult(False, "Usage: schedule del <index>")
            idx = int(subargs[0])
            if idx not in self.simulator.state.schedules:
                return CommandResult(False, f"Schedule #{idx} not found")
            self.simulator.remove_schedule(idx)
            return CommandResult(True, f"Deleted schedule #{idx}")

        elif subcmd in ("on", "enable"):
            if not subargs or not subargs[0].isdigit():
                return CommandResult(False, "Usage: schedule on <index>")
            idx = int(subargs[0])
            if idx not in self.simulator.state.schedules:
                return CommandResult(False, f"Schedule #{idx} not found")
            self.simulator.state.schedules[idx].enabled = True
            return CommandResult(True, f"Schedule #{idx} enabled")

        elif subcmd in ("off", "disable"):
            if not subargs or not subargs[0].isdigit():
                return CommandResult(False, "Usage: schedule off <index>")
            idx = int(subargs[0])
            if idx not in self.simulator.state.schedules:
                return CommandResult(False, f"Schedule #{idx} not found")
            self.simulator.state.schedules[idx].enabled = False
            return CommandResult(True, f"Schedule #{idx} disabled")

        elif subcmd == "days":
            if len(subargs) < 2:
                return CommandResult(False, "Usage: schedule days <index> <days>")
            if not subargs[0].isdigit():
                return CommandResult(False, "Usage: schedule days <index> <days>")
            idx = int(subargs[0])
            if idx not in self.simulator.state.schedules:
                return CommandResult(False, f"Schedule #{idx} not found")
            try:
                days = self._parse_days(subargs[1])
            except ValueError as e:
                return CommandResult(False, str(e))
            self.simulator.state.schedules[idx].days_of_week = days
            return CommandResult(True, f"Schedule #{idx} days: {self._format_days(days)}")

        elif subcmd == "time":
            if len(subargs) < 2:
                return CommandResult(False, "Usage: schedule time <index> <start>-<end>")
            if not subargs[0].isdigit():
                return CommandResult(False, "Usage: schedule time <index> <start>-<end>")
            idx = int(subargs[0])
            if idx not in self.simulator.state.schedules:
                return CommandResult(False, f"Schedule #{idx} not found")
            try:
                start_h, start_m, end_h, end_m = self._parse_time_range(subargs[1])
            except ValueError as e:
                return CommandResult(False, str(e))
            sched = self.simulator.state.schedules[idx]
            sched.start_hour = start_h
            sched.start_min = start_m
            sched.end_hour = end_h
            sched.end_min = end_m
            return CommandResult(True,
                f"Schedule #{idx} time: {self._format_time(start_h, start_m)}-{self._format_time(end_h, end_m)}")

        else:
            return CommandResult(False,
                "Usage: schedule [add|del|on|off|days|time] <args>\n"
                "  add <sensor> <time> [days] - Add schedule\n"
                "  del <index>                - Delete schedule\n"
                "  on/off <index>             - Enable/disable\n"
                "  days <index> <days>        - Set days\n"
                "  time <index> <start>-<end> - Set time window")

    def _schedule_add(self, args: list) -> CommandResult:
        """Add a new schedule entry."""
        if len(args) < 2:
            return CommandResult(False,
                "Usage: schedule add <sensor> <start>-<end> [days]\n"
                "  sensor: inside, outside, or both\n"
                "  days: all, weekdays, weekends, or mon,tue,wed,...")

        sensor = args[0].lower()
        time_range = args[1]
        days_str = args[2] if len(args) > 2 else "all"

        # Validate sensor
        if sensor == "inside":
            inside, outside = True, False
        elif sensor == "outside":
            inside, outside = False, True
        elif sensor == "both":
            inside, outside = True, True
        else:
            return CommandResult(False, f"Unknown sensor: {sensor}. Use inside, outside, or both")

        # Parse time
        try:
            start_h, start_m, end_h, end_m = self._parse_time_range(time_range)
        except ValueError as e:
            return CommandResult(False, str(e))

        # Parse days
        try:
            days = self._parse_days(days_str)
        except ValueError as e:
            return CommandResult(False, str(e))

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

        return CommandResult(True,
            f"Added schedule #{idx}: {sensor} sensor, {self._format_days(days)}, "
            f"{self._format_time(start_h, start_m)}-{self._format_time(end_h, end_m)}")

    # -------------------------------------------------------------------------
    # Scripts
    # -------------------------------------------------------------------------

    @command("list", ["/", "scripts"], "List built-in scripts", category="scripts")
    def list_scripts(self, arg: Optional[str] = None) -> CommandResult:
        """List available built-in scripts."""
        scripts = list(self._list_builtin_scripts())
        lines = ["Built-in scripts:"]
        for name, desc in scripts:
            lines.append(f"  {name}: {desc}")
        return CommandResult(True, "\n".join(lines), {"scripts": scripts})

    @command("run", ["r", "f", "file"], "Run a script", "<script>", category="scripts")
    async def run(self, arg: Optional[str] = None) -> CommandResult:
        """Run a script (built-in name or file path)."""
        if not arg:
            return CommandResult(False, "Usage: run <script>")
        try:
            script = self.load_script(arg)
            if self.script_queue:
                await self.script_queue.put(arg)
                return CommandResult(True, f"Queued script: {script.name}")
            else:
                # Run directly
                success = await self.script_runner.run(script)
                status = "PASSED" if success else "FAILED"
                return CommandResult(success, f"Script {status}: {script.name}")
        except Exception as e:
            return CommandResult(False, f"Error: {e}")

    # -------------------------------------------------------------------------
    # Info
    # -------------------------------------------------------------------------

    @command("status", ["?", "state"], "Show current simulator state", category="info")
    def status(self, arg: Optional[str] = None) -> CommandResult:
        """Show current simulator state."""
        s = self.simulator.state
        data = {
            "door": s.door_status,
            "power": s.power,
            "auto": s.auto,
            "inside": s.inside,
            "outside": s.outside,
            "safety_lock": s.safety_lock,
            "cmd_lockout": s.cmd_lockout,
            "autoretract": s.autoretract,
            "hold_time": s.hold_time,
            "battery": s.battery_percent,
            "pet_in_doorway": s.pet_in_doorway,
            "schedules": list(s.schedules.keys()),
            "open_cycles": s.total_open_cycles,
            "auto_retracts": s.total_auto_retracts,
        }
        lines = [
            "Current State:",
            f"  Door: {s.door_status}",
            f"  Power: {'ON' if s.power else 'OFF'}",
            f"  Auto (schedule): {'ON' if s.auto else 'OFF'}",
            f"  Inside sensor: {'enabled' if s.inside else 'disabled'}",
            f"  Outside sensor: {'enabled' if s.outside else 'disabled'}",
            f"  Safety lock: {'ON' if s.safety_lock else 'OFF'}",
            f"  Command lockout: {'ON' if s.cmd_lockout else 'OFF'}",
            f"  Auto-retract: {'ON' if s.autoretract else 'OFF'}",
            f"  Hold time: {s.hold_time}s",
            f"  Battery: {s.battery_percent}%",
            f"  Pet in doorway: {'yes' if s.pet_in_doorway else 'no'}",
            f"  Schedules: {list(s.schedules.keys())}",
            f"  Open cycles: {s.total_open_cycles}",
            f"  Auto-retracts: {s.total_auto_retracts}",
        ]
        return CommandResult(True, "\n".join(lines), data)

    @command("help", ["?help"], "Show available commands", category="info")
    def help(self, arg: Optional[str] = None) -> CommandResult:
        """Show help for all commands."""
        return CommandResult(True, self.get_help())

    # -------------------------------------------------------------------------
    # Control
    # -------------------------------------------------------------------------

    @command("exit", ["q", "quit", "stop"], "Shutdown the simulator", category="control")
    def exit(self, arg: Optional[str] = None) -> CommandResult:
        """Shutdown the simulator."""
        self.stop_callback()
        return CommandResult(True, "Shutting down...")

    # -------------------------------------------------------------------------
    # Help Generation
    # -------------------------------------------------------------------------

    def get_help(self) -> str:
        """Generate help text from registered commands."""
        # Group commands by category
        categories: dict[str, list[CommandInfo]] = {}
        seen = set()

        for info in _command_registry.values():
            if info.name in seen:
                continue
            seen.add(info.name)

            if info.category not in categories:
                categories[info.category] = []
            categories[info.category].append(info)

        # Define category order and display names
        category_order = [
            ("door", "Door Operations"),
            ("simulation", "Simulation"),
            ("buttons", "Physical Buttons"),
            ("settings", "Settings"),
            ("schedules", "Schedules"),
            ("scripts", "Scripts"),
            ("info", "Info"),
            ("control", "Control"),
        ]

        lines = ["Commands:"]
        for cat_key, cat_name in category_order:
            if cat_key not in categories:
                continue
            lines.append(f"\n{cat_name}:")
            for info in sorted(categories[cat_key], key=lambda x: x.name):
                aliases = ", ".join(info.aliases) if info.aliases else ""
                alias_str = f" ({aliases})" if aliases else ""
                usage_str = f" {info.usage}" if info.usage else ""
                lines.append(f"  {info.name}{alias_str}{usage_str} - {info.description}")

        return "\n".join(lines)
