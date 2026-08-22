# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Info and status commands."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .base import (
    ArgSpec,
    CommandInfo,
    CommandResult,
    SubcommandInfo,
    command,
    get_command_registry,
    subcommand,
)
from .history import HISTORY_UNAVAILABLE_MESSAGE, History
from .scripts import ScriptStatus, format_script_status

if TYPE_CHECKING:
    from ..server import DoorSimulator


class InfoCommandsMixin:
    """Mixin providing info and status commands."""

    simulator: "DoorSimulator"
    #: Supplied by ScriptsCommandsMixin on the full CommandHandler; `status`
    #: is never dispatched locally by ctl, whose handler lacks that mixin.
    script_status: Callable[[], "ScriptStatus"]
    _history: Any  # prompt_toolkit history object
    _interactive_mode: bool  # Whether running in interactive mode
    _cli_mode: bool  # Whether running in CLI interactive mode (vs ctl/daemon)

    def _is_history_available(self) -> bool:
        """Check if history features are available.

        Returns True when a history object has been registered, or when
        prompt_toolkit is installed AND usable for the current session
        (interactive terminal). Piped/dumb-terminal sessions fall back to
        basic input, which has no history.
        """
        if self._history is not None:
            return True
        # Deferred import to avoid a circular import at module load time
        from ..prompt_common import use_prompt_toolkit

        return use_prompt_toolkit()

    def _get_subcommand_help(
        self, info: SubcommandInfo, cmd_path: list[str], header: str | None = None
    ) -> str:
        """Generate help text for a command's subcommands.

        Args:
            info: The command/subcommand info
            cmd_path: List of command parts leading to this point (e.g., ["schedule"])
            header: Optional header line (defaults to "<command> subcommands:")

        Returns:
            Formatted help string
        """
        cmd_str = " ".join(cmd_path)
        lines = [header if header is not None else f"{cmd_str} subcommands:"]

        # Get unique subcommands (not aliases)
        seen = set()
        for sub_info in info.subcommands.values():
            if sub_info.name in seen:
                continue
            seen.add(sub_info.name)

            # Build the subcommand line
            aliases = ", ".join(sub_info.aliases) if sub_info.aliases else ""
            alias_str = f" ({aliases})" if aliases else ""
            usage_str = f" {sub_info.usage}" if sub_info.usage else ""
            desc = sub_info.description or ""

            lines.append(f"  {sub_info.name}{alias_str}{usage_str} - {desc}")

        return "\n".join(lines)

    def _get_arg_help(self, info: SubcommandInfo, cmd_path: list[str]) -> str:
        """Generate help text for a command's arguments.

        Args:
            info: The command/subcommand info with args
            cmd_path: List of command parts leading to this point

        Returns:
            Formatted help string
        """
        cmd_str = " ".join(cmd_path)
        usage = " ".join(arg.generate_usage() for arg in info.args)
        lines = [f"{cmd_str} {usage}", ""]
        lines.append(info.description or "No description.")
        lines.append("")
        lines.append("Arguments:")

        for arg in info.args:
            required = "required" if arg.required else "optional"
            desc = arg.describe() or f"{arg.arg_type} value"

            # Build constraints string
            constraints = []
            if arg.min_value is not None:
                constraints.append(f"min: {arg.min_value}")
            if arg.max_value is not None:
                constraints.append(f"max: {arg.max_value}")
            if arg.choices:
                constraints.append(f"choices: {', '.join(arg.choices)}")
            if arg.default is not None and not arg.required:
                constraints.append(f"default: {arg.default_display or arg.default}")

            constraint_str = f" ({', '.join(constraints)})" if constraints else ""
            lines.append(f"  {arg.name}: {desc} [{required}]{constraint_str}")

        # Also list registered subcommands so help doesn't hide them
        # (e.g. "power toggle" when power also takes an on/off argument)
        if info.subcommands:
            lines.append("")
            lines.append(self._get_subcommand_help(info, cmd_path, header="Subcommands:"))

        return "\n".join(lines)

    @command("status", ["state", "info", "v"], "Show current simulator state", category="info")
    def status(self) -> CommandResult:
        """Show current simulator state."""
        s = self.simulator.state
        bc = s.battery_config
        num_clients = len(self.simulator.protocols)
        # Serialized script runs made "busy" a real state; without this the
        # operator watches the door move with no explanation (M5).
        script = self.script_status()
        running_script, queued_scripts = script.running, script.queued
        data = {
            "connected_clients": num_clients,
            "running_script": running_script,
            "queued_scripts": queued_scripts,
            "door": s.door_status,
            "power": s.power,
            "auto": s.auto,
            "inside": s.inside,
            "outside": s.outside,
            "safety_lock": s.safety_lock,
            "cmd_lockout": s.cmd_lockout,
            "autoretract": s.autoretract,
            "hold_time": s.hold_time,
            "battery_percent": s.battery_percent,
            "battery_present": s.battery_present,
            "ac_present": s.ac_present,
            "charge_rate": bc.charge_rate,
            "discharge_rate": bc.discharge_rate,
            "inside_sensor_active": s.inside_sensor_active,
            "outside_sensor_active": s.outside_sensor_active,
            "schedules": list(s.schedules.keys()),
            "open_cycles": s.total_open_cycles,
            "auto_retracts": s.total_auto_retracts,
            "notify_inside_on": s.sensor_on_indoor,
            "notify_inside_off": s.sensor_off_indoor,
            "notify_outside_on": s.sensor_on_outdoor,
            "notify_outside_off": s.sensor_off_outdoor,
            "notify_low_battery": s.low_battery,
        }
        # Build battery status string
        battery_status = f"{s.battery_percent}%"
        if not s.battery_present:
            battery_status += " (no battery)"
        elif s.ac_present and bc.charge_rate > 0 and s.battery_percent < 100:
            battery_status += f" (charging {bc.charge_rate}%/min)"
        elif not s.ac_present and bc.discharge_rate > 0 and s.battery_percent > 0:
            battery_status += f" (discharging {bc.discharge_rate}%/min)"

        # Build notifications string
        notify_on = []
        if s.sensor_on_indoor:
            notify_on.append("in_on")
        if s.sensor_off_indoor:
            notify_on.append("in_off")
        if s.sensor_on_outdoor:
            notify_on.append("out_on")
        if s.sensor_off_outdoor:
            notify_on.append("out_off")
        if s.low_battery:
            notify_on.append("low_bat")
        notify_str = ", ".join(notify_on) if notify_on else "none"

        # Build sensor detection status
        sensor_active = []
        if s.inside_sensor_active:
            sensor_active.append("inside")
        if s.outside_sensor_active:
            sensor_active.append("outside")
        sensor_str = ", ".join(sensor_active) if sensor_active else "none"

        # Build clients status
        if num_clients == 0:
            clients_str = "none"
        elif num_clients == 1:
            clients_str = "1 client"
        else:
            clients_str = f"{num_clients} clients"

        lines = [
            "Current State:",
            f"  Clients: {clients_str}",
            f"  Door: {s.door_status}",
            f"  Power: {'ON' if s.power else 'OFF'}",
            f"  Auto (schedule): {'ON' if s.auto else 'OFF'}",
            f"  Inside sensor: {'enabled' if s.inside else 'disabled'}",
            f"  Outside sensor: {'enabled' if s.outside else 'disabled'}",
            f"  Safety lock: {'ON' if s.safety_lock else 'OFF'}",
            f"  Command lockout: {'ON' if s.cmd_lockout else 'OFF'}",
            f"  Auto-retract: {'ON' if s.autoretract else 'OFF'}",
            f"  Hold time: {s.hold_time}s",
            f"  Battery: {battery_status}",
            f"  AC: {'connected' if s.ac_present else 'disconnected'}",
            f"  Notifications: {notify_str}",
            f"  Sensor active: {sensor_str}",
            f"  Schedules: {list(s.schedules.keys())}",
            f"  Open cycles: {s.total_open_cycles}",
            f"  Auto-retracts: {s.total_auto_retracts}",
            f"  {format_script_status(script)}",
        ]
        return CommandResult(True, "\n".join(lines), data)

    @command("help", ["?"], "Show available commands", category="info")
    def help(self) -> CommandResult:
        """Show help for all commands."""
        return CommandResult(True, self.get_help())

    def _require_clients(self) -> CommandResult | None:
        """Check if clients are connected. Returns error result if not."""
        if not self.simulator.protocols:
            return CommandResult(False, "No clients connected")
        return None

    @command("broadcast", ["bc"], "Broadcast data to connected clients", category="info")
    def broadcast(self) -> CommandResult:
        """Show broadcast help (default action). Use 'broadcast help' for subcommands."""
        # Default action: show help for subcommands
        info = get_command_registry()["broadcast"]
        return CommandResult(True, self._get_subcommand_help(info, ["broadcast"]))

    @subcommand("broadcast", "status", [], "Broadcast door status")
    def broadcast_status(self) -> CommandResult:
        """Broadcast door status to all connected clients."""
        if err := self._require_clients():
            return err
        self.simulator._broadcast_door_status()
        return CommandResult(True, f"Broadcast status: {self.simulator.state.door_status}")

    @subcommand("broadcast", "settings", [], "Broadcast all settings")
    def broadcast_settings(self) -> CommandResult:
        """Broadcast settings to all connected clients."""
        if err := self._require_clients():
            return err
        self.simulator.broadcast_settings()
        return CommandResult(True, "Broadcast settings")

    @subcommand("broadcast", "battery", [], "Broadcast battery status")
    def broadcast_battery(self) -> CommandResult:
        """Broadcast battery status to all connected clients."""
        if err := self._require_clients():
            return err
        self.simulator._broadcast_battery_status()
        pct = self.simulator.state.battery_percent
        ac = "AC" if self.simulator.state.ac_present else "no AC"
        return CommandResult(True, f"Broadcast battery: {pct}% ({ac})")

    @subcommand("broadcast", "hwinfo", [], "Broadcast hardware info")
    def broadcast_hwinfo(self) -> CommandResult:
        """Broadcast hardware info to all connected clients."""
        if err := self._require_clients():
            return err
        self.simulator.broadcast_hardware_info()
        s = self.simulator.state
        return CommandResult(
            True,
            f"Broadcast hwinfo: fw {s.fw_major}.{s.fw_minor}.{s.fw_patch}, "
            f"hw {s.hw_ver} rev {s.hw_rev}",
        )

    @subcommand("broadcast", "stats", [], "Broadcast door statistics")
    def broadcast_stats(self) -> CommandResult:
        """Broadcast door statistics to all connected clients."""
        if err := self._require_clients():
            return err
        self.simulator.broadcast_stats()
        s = self.simulator.state
        return CommandResult(
            True,
            f"Broadcast stats: {s.total_open_cycles} cycles, {s.total_auto_retracts} retracts",
        )

    @subcommand("broadcast", "schedules", [], "Broadcast schedule list")
    def broadcast_schedules(self) -> CommandResult:
        """Broadcast schedules to all connected clients."""
        if err := self._require_clients():
            return err
        self.simulator.broadcast_schedules()
        count = len(self.simulator.state.schedules)
        return CommandResult(True, f"Broadcast schedules: {count} schedule(s)")

    @subcommand("broadcast", "notifications", [], "Broadcast notification settings")
    def broadcast_notifications(self) -> CommandResult:
        """Broadcast notification settings to all connected clients."""
        if err := self._require_clients():
            return err
        self.simulator.broadcast_notifications()
        return CommandResult(True, "Broadcast notifications")

    @subcommand("broadcast", "all", [], "Broadcast everything")
    def broadcast_all(self) -> CommandResult:
        """Broadcast all data to all connected clients."""
        if err := self._require_clients():
            return err
        self.simulator.broadcast_all()
        return CommandResult(True, "Broadcast all data")

    @command(
        "history",
        ["hist"],
        "Show or manage command history",
        category="info",
        usage="[clear|N]",
        args=[
            ArgSpec(
                "action",
                "string",
                required=False,
                choices=["clear"],
                description="'clear' to clear history, or N to show last N commands",
            )
        ],
        interactive_only=True,
        local_only=True,
    )
    def history(self, arg: str | None = None) -> CommandResult:
        """Show or manage command history.

        Subcommands:
            history         - Show last 20 commands
            history N       - Show last N commands
            history clear   - Clear command history

        Delegates to :meth:`History.execute_command`, which is the single
        implementation. This method used to re-implement clear + formatting
        inline against the raw prompt_toolkit object, and the two copies
        had already drifted on user-facing text - with the well-documented
        one being the dead copy a maintainer would edit (round-6 frontend
        T1).

        Note: Requires prompt_toolkit (install with pip install pypowerpetdoor[interactive])
        """
        if self._history is None:
            return CommandResult(False, HISTORY_UNAVAILABLE_MESSAGE)
        return History(backend=self._history).execute_command(arg)

    def get_help(self) -> str:
        """Generate help text from registered commands."""
        _command_registry = get_command_registry()

        # Group commands by category
        categories: dict[str, list[CommandInfo]] = {}
        seen = set()

        for info in _command_registry.values():
            if info.name in seen:
                continue
            # Hide interactive-only commands when not in interactive mode
            if info.interactive_only and not self._interactive_mode:
                continue
            # Hide history command when prompt_toolkit is not available
            if info.name == "history" and not self._is_history_available():
                continue
            # Hide exit command in CLI mode (exit/q/quit are aliases for shutdown there)
            if info.name == "exit" and self._cli_mode:
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
