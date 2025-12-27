"""CLI for Power Pet Door simulator.

This module provides the interactive command-line interface for running
and controlling the door simulator.
"""

import asyncio
import logging
import random
from pathlib import Path
from typing import Optional

from .state import Schedule
from .server import DoorSimulator

logger = logging.getLogger(__name__)


async def run_simulator_interactive(
    host: str = "0.0.0.0",
    port: int = 3000,
    script_file: Optional[str] = None,
    script_name: Optional[str] = None,
    exit_after_script: bool = False,
):
    """Run the simulator with interactive keyboard controls.

    Commands:
        Door Operations:
            i - Trigger inside sensor (pet going out)
            o - Trigger outside sensor (pet coming in)
            c - Close door immediately
            h - Open and hold (stays open until 'c')

        Physical Buttons (like on the real door):
            p - Toggle power on/off
            m - Toggle auto/tiMers (schedule enable)
            n - Toggle iNside sensor enable
            u - Toggle oUtside sensor enable

        Simulation Events:
            x - Simulate obstruction (triggers auto-retract)
            d - Toggle pet in doorway (keeps door open)

        Settings:
            s - Toggle outside sensor safety lock
            l - Toggle command lockout
            a - Toggle auto-retract
            t <seconds> - Set hold time
            b [percent] - Set battery level (random if no value)

        Schedule:
            1 - Add sample schedule #1 (all days, 6am-10pm)
            2 - Add sample schedule #2 (weekdays, 7am-6pm)
            3 - Delete schedule #1

        Scripts:
            r <name> - Run a built-in script
            f <path> - Run a script file
            / - List available built-in scripts

        Info:
            ? - Show current door state
            q - Quit

    Args:
        host: Address to bind the server
        port: Port to listen on
        script_file: Path to a script file to run on startup
        script_name: Name of a built-in script to run on startup
        exit_after_script: If True, exit after script completes
    """
    import sys

    from .scripting import Script, ScriptRunner, get_builtin_script, list_builtin_scripts

    simulator = DoorSimulator(host=host, port=port)
    await simulator.start()

    script_runner = ScriptRunner(simulator)

    print(f"Simulator started on port {port}")
    print("=" * 65)
    print("Door Operations:")
    print("  i - Trigger inside sensor     o - Trigger outside sensor")
    print("  c - Close door                h - Open and hold")
    print()
    print("Physical Buttons:")
    print("  p - Power on/off              m - Auto/tiMers (schedule)")
    print("  n - iNside sensor enable      u - oUtside sensor enable")
    print()
    print("Simulation Events:")
    print("  x - Simulate obstruction      d - Toggle pet in doorway")
    print()
    print("Settings:")
    print("  s - Safety lock               l - Command lockout")
    print("  a - Auto-retract              t <sec> - Hold time")
    print("  b [pct] - Battery level")
    print()
    print("Schedule:")
    print("  1 - Add schedule #1           2 - Add schedule #2")
    print("  3 - Delete schedule #1")
    print()
    print("Scripts:")
    print("  r <name> - Run built-in       f <path> - Run script file")
    print("  / - List built-in scripts")
    print()
    print("Info:  ? - Show state           q - Quit")
    print("=" * 65)
    print()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    script_result = [None]  # Use list to allow mutation in nested function

    # Run startup script if specified
    if script_file or script_name:
        async def run_startup_script():
            try:
                if script_file:
                    script = Script.from_file(Path(script_file))
                else:
                    script = get_builtin_script(script_name)

                print(f"\n>>> Running startup script: {script.name}")
                success = await script_runner.run(script)
                script_result[0] = success

                if exit_after_script:
                    print(f"\n>>> Script {'PASSED' if success else 'FAILED'}")
                    stop_event.set()
            except Exception as e:
                print(f"Error running startup script: {e}")
                script_result[0] = False
                if exit_after_script:
                    stop_event.set()

        asyncio.create_task(run_startup_script())

    def show_state():
        s = simulator.state
        print("\n--- Current State ---")
        print(f"  Door: {s.door_status}")
        print(f"  Power: {'ON' if s.power else 'OFF'}")
        print(f"  Auto (schedule): {'ON' if s.auto else 'OFF'}")
        print(f"  Inside sensor: {'enabled' if s.inside else 'disabled'}")
        print(f"  Outside sensor: {'enabled' if s.outside else 'disabled'}")
        print(f"  Safety lock: {'ON' if s.safety_lock else 'OFF'}")
        print(f"  Command lockout: {'ON' if s.cmd_lockout else 'OFF'}")
        print(f"  Auto-retract: {'ON' if s.autoretract else 'OFF'}")
        print(f"  Hold time: {s.hold_time}s")
        print(f"  Battery: {s.battery_percent}%")
        print(f"  Pet in doorway: {'yes' if s.pet_in_doorway else 'no'}")
        print(f"  Schedules: {list(s.schedules.keys())}")
        print(f"  Open cycles: {s.total_open_cycles}")
        print(f"  Auto-retracts: {s.total_auto_retracts}")
        print("---")

    def list_scripts():
        print("\n--- Built-in Scripts ---")
        for name, desc in list_builtin_scripts():
            print(f"  {name}: {desc}")
        print("---")

    def handle_input():
        try:
            line = sys.stdin.readline().strip()
            if not line:
                return

            cmd = line.lower()

            if cmd == "i":
                print(">>> Inside sensor triggered (pet going out)")
                simulator.trigger_sensor("inside")
            elif cmd == "o":
                print(">>> Outside sensor triggered (pet coming in)")
                simulator.trigger_sensor("outside")
            elif cmd == "c":
                print(">>> Closing door")
                asyncio.create_task(simulator.close_door())
            elif cmd == "h":
                print(">>> Opening and holding")
                asyncio.create_task(simulator.open_door(hold=True))
            elif cmd == "x":
                print(">>> Simulating obstruction")
                simulator.simulate_obstruction()
            elif cmd == "d":
                simulator.state.pet_in_doorway = not simulator.state.pet_in_doorway
                state = "present" if simulator.state.pet_in_doorway else "gone"
                print(f">>> Pet in doorway: {state}")
            elif cmd == "p":
                simulator.state.power = not simulator.state.power
                state = "ON" if simulator.state.power else "OFF"
                print(f">>> Power: {state}")
            elif cmd == "m":
                simulator.state.auto = not simulator.state.auto
                state = "ON" if simulator.state.auto else "OFF"
                print(f">>> Auto (schedule): {state}")
            elif cmd == "n":
                simulator.state.inside = not simulator.state.inside
                state = "enabled" if simulator.state.inside else "disabled"
                print(f">>> Inside sensor: {state}")
            elif cmd == "u":
                simulator.state.outside = not simulator.state.outside
                state = "enabled" if simulator.state.outside else "disabled"
                print(f">>> Outside sensor: {state}")
            elif cmd == "l":
                simulator.state.cmd_lockout = not simulator.state.cmd_lockout
                state = "ON" if simulator.state.cmd_lockout else "OFF"
                print(f">>> Command lockout: {state}")
            elif cmd == "s":
                simulator.state.safety_lock = not simulator.state.safety_lock
                state = "ON" if simulator.state.safety_lock else "OFF"
                print(f">>> Outside sensor safety lock: {state}")
            elif cmd == "a":
                simulator.state.autoretract = not simulator.state.autoretract
                state = "ON" if simulator.state.autoretract else "OFF"
                print(f">>> Auto-retract: {state}")
            elif cmd.startswith("t ") or cmd == "t":
                # Handle "t <seconds>" or just "t" for prompt
                parts = line.split(maxsplit=1)
                if len(parts) > 1 and parts[1].isdigit():
                    seconds = int(parts[1])
                    simulator.state.hold_time = seconds
                    print(f">>> Hold time set to {seconds}s")
                else:
                    print("Usage: t <seconds> (e.g., t 5)")
            elif cmd.startswith("b ") or cmd == "b":
                # Handle "b <percent>" or just "b" for random
                parts = line.split(maxsplit=1)
                if len(parts) > 1 and parts[1].isdigit():
                    pct = max(0, min(100, int(parts[1])))
                    print(f">>> Battery set to {pct}%")
                    simulator.set_battery(pct)
                else:
                    pct = random.randint(10, 100)
                    print(f">>> Battery set to {pct}% (random)")
                    simulator.set_battery(pct)
            elif cmd == "1":
                schedule = Schedule(index=1, enabled=True)
                simulator.add_schedule(schedule)
                print(">>> Added schedule #1 (all days, 6am-10pm)")
            elif cmd == "2":
                schedule = Schedule(
                    index=2,
                    enabled=True,
                    days_of_week=0b0111110,  # Mon-Fri
                    inside_start_hour=7,
                    inside_end_hour=18,
                    outside_start_hour=7,
                    outside_end_hour=18,
                )
                simulator.add_schedule(schedule)
                print(">>> Added schedule #2 (weekdays, 7am-6pm)")
            elif cmd == "3":
                simulator.remove_schedule(1)
                print(">>> Deleted schedule #1")
            elif cmd == "/":
                list_scripts()
            elif cmd.startswith("r "):
                # Run built-in script
                name = line[2:].strip()

                async def run_script():
                    try:
                        script = get_builtin_script(name)
                        await script_runner.run(script)
                    except Exception as e:
                        print(f"Error: {e}")
                asyncio.create_task(run_script())
            elif cmd.startswith("f "):
                # Run script file
                script_path = line[2:].strip()

                async def run_file():
                    try:
                        script = Script.from_file(Path(script_path))
                        await script_runner.run(script)
                    except Exception as e:
                        print(f"Error: {e}")
                asyncio.create_task(run_file())
            elif cmd == "?":
                show_state()
            elif cmd == "q":
                print(">>> Shutting down...")
                stop_event.set()
            else:
                print(f"Unknown command: {line}")
        except Exception as e:
            print(f"Error: {e}")

    # Only add stdin reader if not running in non-interactive mode
    if not exit_after_script:
        loop.add_reader(sys.stdin.fileno(), handle_input)

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        if not exit_after_script:
            loop.remove_reader(sys.stdin.fileno())
        await simulator.stop()

    return script_result[0]


def main():
    """CLI entry point for the simulator."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Power Pet Door Simulator - Fake door for testing"
    )
    parser.add_argument(
        "--host", "-H",
        default="0.0.0.0",
        help="Address to bind (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=3000,
        help="Port to listen on (default: 3000)"
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--script", "-s",
        help="Run a built-in script by name (e.g., 'basic_cycle', 'full_test_suite')"
    )
    parser.add_argument(
        "--script-file", "-f",
        help="Run a script from a YAML file"
    )
    parser.add_argument(
        "--exit-after-script", "-e",
        action="store_true",
        help="Exit after script completes (useful for CI/CD)"
    )
    parser.add_argument(
        "--list-scripts", "-l",
        action="store_true",
        help="List available built-in scripts and exit"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # List scripts and exit
    if args.list_scripts:
        from .scripting import list_builtin_scripts
        print("Available built-in scripts:")
        for name, desc in list_builtin_scripts():
            print(f"  {name}: {desc}")
        return

    try:
        result = asyncio.run(run_simulator_interactive(
            host=args.host,
            port=args.port,
            script_name=args.script,
            script_file=args.script_file,
            exit_after_script=args.exit_after_script,
        ))

        # Exit with appropriate code for CI/CD
        if args.exit_after_script and result is not None:
            sys.exit(0 if result else 1)

    except KeyboardInterrupt:
        print("\nSimulator stopped.")


if __name__ == "__main__":
    main()
