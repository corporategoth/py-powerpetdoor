# Power Pet Door Simulator

The Power Pet Door simulator is a full-featured testing tool that emulates the behavior of a real Power Pet Door device. It speaks the same network protocol and responds to all commands, making it ideal for:

- **Development**: Test client code without physical hardware
- **Automated Testing**: Run reproducible test scenarios in CI/CD pipelines
- **Demos & Training**: Demonstrate door behavior without a real device
- **Integration Testing**: Verify Home Assistant or other integrations

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Command Line Usage](#command-line-usage)
- [Interactive Mode](#interactive-mode)
- [Daemon Mode](#daemon-mode)
- [Remote Control (ppd-simulator-ctl)](#remote-control-ppd-simulator-ctl)
- [Programmatic Usage](#programmatic-usage)
- [Scripting System](#scripting-system)
  - [Script Format](#script-format)
  - [Available Actions](#available-actions)
  - [Conditions](#conditions)
  - [Settings](#settings)
  - [Built-in Scripts](#built-in-scripts)
  - [Best Practices](#best-practices)
- [Architecture](#architecture)

## Installation

The simulator is included with the `pypowerpetdoor` package:

```bash
pip install pypowerpetdoor
```

Two optional extras enhance it:

```bash
# YAML script support (PyYAML)
pip install pypowerpetdoor[simulator]

# Enhanced interactive prompt: syntax highlighting, tab completion,
# persistent history (prompt_toolkit)
pip install pypowerpetdoor[interactive]

# Or both
pip install "pypowerpetdoor[simulator,interactive]"
```

Without the `simulator` extra, the YAML scripting features are unavailable;
without the `interactive` extra, the interactive prompt falls back to a plain
input loop with the same commands.

## Quick Start

Start the simulator on the default port (3000):

```bash
ppd-simulator
# equivalently:
python -m powerpetdoor.simulator
```

Then connect your client to `localhost:3000`.

## Command Line Usage

`ppd-simulator` has three modes:

- **Interactive** (default): a command prompt for driving the simulator by hand.
- **Script mode** (`--script`): runs one or more test scripts, then keeps
  serving (or exits, with `--oneshot`).
- **Daemon mode** (`--daemon`): headless, controlled remotely through a
  control channel (see [Daemon Mode](#daemon-mode)).

`--script` and `--daemon` are mutually exclusive.

### Options

| Option | Description |
|--------|-------------|
| `--host`, `-H` `ADDR` | Door-server bind address (default: `0.0.0.0` — all interfaces; the simulator emulates a LAN device). Use `127.0.0.1` to restrict to loopback. |
| `--port`, `-p` `PORT` | Door protocol port (default: 3000) |
| `--debug`, `-d` | Enable debug logging |
| `--script`, `-s` `SCRIPT` | Run a script — built-in name or file path, auto-detected. Repeatable to run scripts in sequence. Implies non-interactive mode. |
| `--loop` | Run scripts continuously in a loop |
| `--script-delay` `SECONDS` | Delay between scripts and loop iterations (default: 0) |
| `--oneshot` | Exit after scripts complete (useful for CI/CD). Takes precedence over `--run-for`. |
| `--wait-for-client`, `-w` | Wait for a client to connect before starting scripts; scripts stop if the client disconnects |
| `--list-scripts`, `-l` | List available built-in scripts and exit |
| `--daemon`, `-D` `[CONTROL_PORT]` | Run in daemon mode with a control channel (default control port: door port + 1) |
| `--control-host` `ADDR` | Bind address for the daemon control channel (default: `127.0.0.1`). See the [security note](#daemon-mode) before widening this. |
| `--run-for`, `-r` `SECONDS` | Maximum run time in seconds (`--oneshot` can exit earlier) |
| `--history` `FILE` | Prompt history file, or `none` to disable (default: `~/.powerpetdoor_simulator_history`; only present when prompt_toolkit is installed) |
| `--firmware`, `-f` `VERSION` | Firmware version to report as `major.minor.patch` (default: 1.2.3) |
| `--hardware` `VERSION` | Hardware version to report as `ver.rev` (default: 1.1) |

### Running Scripts

```bash
# Run a built-in script (then stay running)
ppd-simulator --script basic_cycle

# Run a script from a file (auto-detected as a path)
ppd-simulator --script /path/to/my_test.yaml

# Run several scripts in sequence, looping, with a delay between runs
ppd-simulator -s basic_cycle -s obstruction_test --loop --script-delay 2

# Run a script suite and exit (useful for CI/CD)
ppd-simulator --script full_test_suite --oneshot

# List available built-in scripts
ppd-simulator --list-scripts
```

### Exit Codes

When using `--oneshot`:

- **0**: All scripts completed successfully (all assertions passed)
- **1**: A script failed (assertion failed or error occurred)

This makes it easy to integrate with CI/CD pipelines:

```bash
ppd-simulator -s full_test_suite --oneshot || echo "Tests failed!"
```

## Interactive Mode

When started without `--script` or `--daemon`, the simulator presents a
command prompt. Commands are typed words (with short aliases), not single
keystrokes — press Enter to execute.

With prompt_toolkit installed (`pip install pypowerpetdoor[interactive]`) the
prompt provides:

- **Syntax highlighting**: commands in green, subcommands in blue, `on`/`off`
  options in orange, numbers in purple
- **Tab completion** for commands, subcommands, and argument choices
- **Persistent history** (`~/.powerpetdoor_simulator_history`, configurable
  via `--history`), with `!!` / `!n` / `!-n` recall, reverse search, and
  auto-suggestions. Failed commands are dropped from history and aliases are
  recorded in canonical form.
- A `host:port>` prompt that is **white while a door client is connected**
  and gray otherwise

Without prompt_toolkit, a plain input prompt offers the same commands.

Type `help` (or `?`) at any time for the full command list, and
`<command> help` (e.g., `schedule add help`) for details on a command's
arguments and subcommands. Extra arguments in `[brackets]` are optional.

### Door Operations

| Command | Aliases | Action |
|---------|---------|--------|
| `inside [duration]` | `i` | Activate the inside sensor (pet going out). `duration` is seconds active (default 0.5); `0` = toggle on/off indefinitely |
| `outside [duration]` | `o` | Activate the outside sensor (pet coming in); same `duration` semantics |
| `cycle` | `y` | Full door cycle — open, hold, close (like pressing the door button; bypasses sensor enable checks) |
| `close` | `c` | Close the door |
| `hold` | `h`, `open` | Open the door and hold it open |

### Simulation Events

| Command | Aliases | Action |
|---------|---------|--------|
| `obstruction` | `x` | Simulate an obstruction during close (triggers auto-retract if enabled) |
| `pet arrive` / `pet depart` | | Simulate a pet standing in (or leaving) the doorway. A present pet keeps the door open — the same mechanism as the `pet_presence`/`pet_on`/`pet_off` script actions |

### Physical Buttons

These simulate the physical buttons on the door unit. Each accepts `on`/`off`
as an argument, or toggles when called bare (a `toggle`/`t` subcommand is also
available):

| Command | Aliases | Action |
|---------|---------|--------|
| `power [on\|off]` | `p` | Main power |
| `auto [on\|off]` | `m` | Auto/schedule mode (timers) |
| `inside_enable [on\|off]` | `n` | Inside sensor enable |
| `outside_enable [on\|off]` | `u` | Outside sensor enable |

### Settings

| Command | Aliases | Action |
|---------|---------|--------|
| `safety [on\|off]` | `s` | Outside sensor safety lock (toggle if bare) |
| `lockout [on\|off]` | `l` | Command lockout (toggle if bare) |
| `autoretract [on\|off]` | `a` | Auto-retract on obstruction (toggle if bare) |
| `holdtime <seconds>` | `t` | Set hold time (0.1–900 seconds) |
| `battery [percent]` | `b` | Set battery level 0–100 (random 10–100 if omitted) |
| `ac [connect\|disconnect\|toggle]` | | AC power connection (toggle if bare) |
| `battery_present [on\|off]` | `bp` | Battery installed/removed (toggle if bare) |
| `charge_rate [rate]` | `cr` | Set battery charge rate in %/min (`0` disables); bare shows the current rate |
| `discharge_rate [rate]` | `dcr` | Set battery discharge rate in %/min (`0` disables); bare shows the current rate |
| `timezone [tz]` | `tz` | Set the timezone (IANA name like `America/New_York` or POSIX string like `EST5EDT,M3.2.0,M11.1.0`); bare shows the current timezone |

### Notifications

| Command | Action |
|---------|--------|
| `notify` | Show all notification settings |
| `notify <name>` | Toggle a notification setting |
| `notify <name> on\|off` | Set a notification setting |

Available notification names: `inside_on`, `inside_off`, `outside_on`,
`outside_off`, `low_battery` (aliases `low_bat`, `lowbat`).

### Schedules

The `schedule` command (alias `sched`) manages schedule entries:

| Command | Action |
|---------|--------|
| `schedule` or `schedule list` | Show all schedules (shows the implicit all-day schedule when none are configured) |
| `schedule add <inside\|outside\|both> <time> [days]` | Add a schedule, e.g. `schedule add inside 6:00-22:00 weekdays`. `days` is a comma list of day names (`mon,tue,wed`) or a preset (`all`/`weekdays`/`weekends`, default `all`) |
| `schedule delete <index>` | Delete a schedule (aliases `del`, `rm`, `remove`) |
| `schedule clear` | Delete all schedules |
| `schedule enable <index>` | Enable a schedule (alias `on`) |
| `schedule disable <index>` | Disable a schedule (alias `off`) |
| `schedule days <index> <days>` | Change a schedule's days |
| `schedule time <index> <time>` | Change a schedule's time window, e.g. `schedule time 0 7:30-21:15` |

### Scripts

| Command | Aliases | Action |
|---------|---------|--------|
| `run <script>` | `r`, `file` | Run a script — built-in name or YAML file path. Scripts are queued and run in the background; the PASSED/FAILED result is logged |
| `list` | `/`, `scripts` | List available built-in scripts |

### Info

| Command | Aliases | Action |
|---------|---------|--------|
| `status` | `state`, `info`, `v` | Show the full simulator state (connected clients, door, power, sensors, settings, battery, notifications, schedules, statistics) |
| `help` | `?` | Show all available commands |
| `broadcast <what>` | `bc` | Push an unsolicited update to connected door clients. `<what>` is one of `status`, `settings`, `battery`, `hwinfo`, `stats`, `schedules`, `notifications`, `all`. Errors if no client is connected |
| `history [N\|clear]` | `hist` | Show the last N commands (default 20) or clear history (interactive prompt only; requires prompt_toolkit) |

### Control

| Command | Aliases | Action |
|---------|---------|--------|
| `debug [on\|off]` | | Show or set debug logging |
| `shutdown` | `stop` | Stop the simulator |
| `exit` | `q`, `quit` | In the interactive CLI these are aliases for `shutdown` |
| `clear` | `cls` | Clear the screen (interactive prompt only) |

## Daemon Mode

`--daemon` runs the simulator headless, with a plain-text **control channel**
for remote management (used by `ppd-simulator-ctl`):

```bash
# Door protocol on 3000, control channel on 3001 (door port + 1)
ppd-simulator --daemon

# Explicit control port
ppd-simulator --daemon 4001
```

The control channel accepts the same commands as the interactive prompt,
newline-terminated. Each command gets a single response line — `OK: <message>`
or `ERROR: <message>` (embedded newlines are escaped as `\n`) — and simulator
log output is streamed to every connected control client as `LOG: <line>`
messages.

> **Security note**: the control channel is **unauthenticated** — anyone who
> can connect to it can drive the simulator, run scripts, and shut it down.
> It therefore binds `127.0.0.1` (loopback only) by default. Pass
> `--control-host` with a wider address only on networks you trust.
>
> The door protocol server itself binds `0.0.0.0` by default because it
> emulates a LAN device — use `--host 127.0.0.1` if you do not want it
> reachable from the network.

Over the control channel, `run` accepts only **bare script names** (no path
separators or traversal), resolved against the known script locations.
Running an arbitrary YAML file path is only possible locally, via the
interactive CLI or `--script`.

## Remote Control (ppd-simulator-ctl)

`ppd-simulator-ctl` sends commands to a running daemon's control channel.

### One-Shot Commands

```bash
ppd-simulator-ctl status            # Show simulator state
ppd-simulator-ctl inside            # Trigger the inside sensor
ppd-simulator-ctl holdtime 2        # Change a setting
ppd-simulator-ctl run basic_cycle   # Run a script (waits for the result)
ppd-simulator-ctl shutdown          # Stop the daemon
```

The exit code is **0** on success and **1** on error (unknown command,
validation failure, or connection failure), so one-shot commands are
scriptable. For `run`, the exit code reflects the **script result**: 0 if the
script passed, 1 if it failed.

### Interactive Mode

```bash
ppd-simulator-ctl -i
```

Interactive mode keeps a persistent connection and provides the same prompt
experience as the simulator CLI (syntax highlighting, tab completion, history
with `!!`/`!n`/`!-n` recall — stored separately in
`~/.powerpetdoor_ctl_history`). Daemon log output is streamed live into the
session, and the prompt is colored by the daemon's door-client connection
status.

A few commands are handled locally by ctl rather than sent to the daemon:
`help`/`?` (ctl's own help), `exit` (aliases `q`, `quit` — leaves ctl without
stopping the daemon), `clear`/`cls`, and `history`. Use `shutdown` to stop the
daemon itself.

### Options

| Option | Description |
|--------|-------------|
| `--host`, `-H` `ADDR` | Simulator host (default: `127.0.0.1`) |
| `--port`, `-p` `PORT` | Control port (default: 3001) |
| `--door-port`, `-d` `PORT` | Door port shown in the prompt (default: control port − 1) |
| `--interactive`, `-i` | Run in interactive mode |
| `--timeout`, `-t` `SECONDS` | Command timeout (default: 5) |
| `--history` `FILE` | History file path, or `none` to disable (default: `~/.powerpetdoor_ctl_history`; only present when prompt_toolkit is installed) |

## Programmatic Usage

### Basic Usage

```python
import asyncio
from powerpetdoor.simulator import DoorSimulator

async def main():
    # Create and start the simulator
    simulator = DoorSimulator(host="0.0.0.0", port=3000)
    await simulator.start()

    print(f"Simulator running on port 3000")
    print(f"Door status: {simulator.state.door_status}")

    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await simulator.stop()

asyncio.run(main())
```

### Triggering Events

```python
async def demo_events(simulator):
    # Trigger sensors
    simulator.trigger_sensor("inside")   # Pet going out
    simulator.trigger_sensor("outside")  # Pet coming in

    # Direct door control
    await simulator.open_door()           # Open normally
    await simulator.open_door(hold=True)  # Open and keep up
    await simulator.close_door()          # Close immediately

    # Simulate events
    simulator.simulate_obstruction()      # Trigger auto-retract
    simulator.set_pet_in_doorway(True)    # Pet blocking door
    simulator.set_pet_in_doorway(False)   # Pet moved away

    # Battery simulation
    simulator.set_battery(75)             # Set to 75%
    simulator.set_ac_present(True)        # AC power connected
    simulator.set_ac_present(False)       # AC power disconnected
    simulator.set_battery_present(True)   # Battery installed
    simulator.set_battery_present(False)  # Battery removed
    simulator.set_charge_rate(1.0)        # 1%/min charge rate
    simulator.set_discharge_rate(0.1)     # 0.1%/min discharge rate
```

### Modifying State

```python
async def modify_state(simulator):
    state = simulator.state

    # Power and sensors
    state.power = True
    state.inside = True           # Enable inside sensor
    state.outside = True          # Enable outside sensor

    # Safety features
    state.safety_lock = False     # Outside sensor safety lock
    state.cmd_lockout = False     # Command lockout
    state.autoretract = True      # Auto-retract on obstruction

    # Timing
    state.hold_time = 10          # Seconds door stays open
```

### Managing Schedules

```python
from powerpetdoor.simulator import Schedule

async def manage_schedules(simulator):
    # Create a schedule (weekdays 7am-6pm)
    # days_of_week is a list: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
    schedule = Schedule(
        index=1,
        enabled=True,
        days_of_week=[0, 1, 1, 1, 1, 1, 0],  # Mon-Fri
        inside=True,       # This schedule controls inside sensor
        outside=False,
        start_hour=7,
        end_hour=18,
    )

    # Add and remove schedules
    simulator.add_schedule(schedule)
    simulator.remove_schedule(1)
```

### Running Scripts Programmatically

```python
from pathlib import Path

from powerpetdoor.simulator import (
    DoorSimulator,
    Script,
    ScriptRunner,
    get_builtin_script,
)

async def run_tests(simulator):
    runner = ScriptRunner(simulator)

    # Run a built-in script
    script = get_builtin_script("basic_cycle")
    success = await runner.run(script)
    print(f"Test {'passed' if success else 'failed'}")

    # Run a custom script from YAML
    script = Script.from_file(Path("/path/to/my_test.yaml"))
    success = await runner.run(script)

    # Create a script programmatically
    script = Script.from_simple_commands([
        "trigger inside",
        "wait 2",
        "assert door_status DOOR_CLOSED",
    ], name="Quick Test")
    success = await runner.run(script)
```

### Integration with Client Testing

```python
import asyncio
from powerpetdoor import PowerPetDoorClient, COMMAND, CMD_OPEN
from powerpetdoor.simulator import DoorSimulator

async def test_client():
    # Start simulator
    simulator = DoorSimulator(port=3000)
    await simulator.start()

    # Create client
    loop = asyncio.get_running_loop()
    client = PowerPetDoorClient(
        host="127.0.0.1",
        port=3000,
        keepalive=30.0,
        timeout=10.0,
        reconnect=5.0,
        loop=loop
    )

    # Track door status
    status_received = asyncio.Event()
    def on_status(status):
        print(f"Door status: {status}")
        status_received.set()

    client.add_listener("test", door_status_update=on_status)

    # Connect and send command
    await client.connect()
    client.send_message(COMMAND, CMD_OPEN)

    # Wait for response
    await asyncio.wait_for(status_received.wait(), timeout=5.0)

    # Cleanup
    client.stop()
    await simulator.stop()

asyncio.run(test_client())
```

## Scripting System

The simulator includes a YAML-based scripting system for defining repeatable test scenarios.

### Script Format

Scripts are YAML files with the following structure:

```yaml
name: "My Test Script"
description: "Description of what this script tests"
steps:
  - action: trigger_sensor
    sensor: inside
  - action: wait
    seconds: 2
  - action: assert
    condition: door_status
    equals: DOOR_CLOSED
```

### Available Actions

#### Door Operations

**trigger_sensor** / **trigger**
Trigger a pet sensor to open the door.
```yaml
- action: trigger_sensor
  sensor: inside    # "inside" or "outside"
```

**open**
Open the door directly.
```yaml
- action: open
  hold: false       # Optional: true for "open and hold"
```

**close**
Close the door immediately.
```yaml
- action: close
```

#### Simulation Events

**obstruction**
Simulate an obstruction during door close (triggers auto-retract if enabled).
```yaml
- action: obstruction
```

**pet_presence** / **pet_on**
Set pet as present in doorway (prevents door from closing).
```yaml
- action: pet_on
```

**pet_off**
Clear pet presence.
```yaml
- action: pet_off
```

**battery**
Set battery level.
```yaml
- action: battery
  percent: 75
```

#### Timing

**wait**
Pause execution for a specified time.
```yaml
- action: wait
  seconds: 2.5
```

**wait_for**
Wait for a condition to become true (with timeout).
```yaml
- action: wait_for
  condition: door_closed
  timeout: 10        # Seconds (default: 30)
```

#### State Control

**set**
Set a simulator state value.
```yaml
- action: set
  name: hold_time
  value: "5"
```

**toggle**
Toggle a boolean setting.
```yaml
- action: toggle
  name: power
```

#### Schedules

**add_schedule**
Add a schedule entry.
```yaml
- action: add_schedule
  index: 1
  enabled: true
```

**remove_schedule**
Remove a schedule entry.
```yaml
- action: remove_schedule
  index: 1
```

#### Assertions & Logging

**assert**
Assert that a condition equals an expected value. Script fails if assertion fails.
```yaml
- action: assert
  condition: door_status
  equals: DOOR_CLOSED
```

**log**
Print a message to the log.
```yaml
- action: log
  message: "Test step completed"
```

### Conditions

Conditions are used with `wait_for` and `assert` actions:

| Condition | Description |
|-----------|-------------|
| `door_closed` | Door is fully closed |
| `door_open` | Door is open (holding or keepup) |
| `door_rising` | Door is currently opening |
| `door_holding` | Door is open and in hold timer |
| `door_keepup` | Door is open and held indefinitely |
| `power_on` | Power is enabled |
| `power_off` | Power is disabled |
| `auto_on` | Schedule/timers are enabled |
| `auto_off` | Schedule/timers are disabled |
| `inside_enabled` | Inside sensor is enabled |
| `inside_disabled` | Inside sensor is disabled |
| `outside_enabled` | Outside sensor is enabled |
| `outside_disabled` | Outside sensor is disabled |
| `autoretract_on` | Auto-retract is enabled |
| `autoretract_off` | Auto-retract is disabled |
| `safety_lock_on` | Outside sensor safety lock is on |
| `safety_lock_off` | Outside sensor safety lock is off |
| `cmd_lockout_on` | Command lockout is on |
| `cmd_lockout_off` | Command lockout is off |

For `assert`, you can also check these values:

| Condition | Expected Values |
|-----------|-----------------|
| `door_status` | `DOOR_CLOSED`, `DOOR_RISING`, `DOOR_HOLDING`, `DOOR_KEEPUP`, etc. |
| `power` | `on`, `off` |
| `auto` | `on`, `off` |
| `battery` | Number (e.g., `75`) |
| `hold_time` | Number in seconds (e.g., `10`) |
| `inside` | `enabled`, `disabled` |
| `outside` | `enabled`, `disabled` |
| `autoretract` | `on`, `off` |
| `safety_lock` | `on`, `off` |
| `cmd_lockout` | `on`, `off` |
| `total_open_cycles` | Number |
| `total_auto_retracts` | Number |

### Settings

Settings that can be used with `set` and `toggle`:

| Setting | Type | Description |
|---------|------|-------------|
| `power` | boolean | Main power on/off |
| `auto` | boolean | Schedule/timers enabled |
| `inside` | boolean | Inside sensor enabled |
| `outside` | boolean | Outside sensor enabled |
| `autoretract` | boolean | Auto-retract on obstruction |
| `safety_lock` | boolean | Outside sensor safety lock |
| `cmd_lockout` | boolean | Command lockout |
| `hold_time` | integer | Seconds door stays open |
| `battery` | integer | Battery percentage (0-100) |

Boolean values accept: `true`, `false`, `on`, `off`, `yes`, `no`, `1`, `0`, `enabled`, `disabled`

### Built-in Scripts

The simulator includes several built-in test scripts:

| Script | Description |
|--------|-------------|
| `basic_cycle` | Pet triggers inside sensor, door opens, holds, then closes |
| `obstruction_test` | Tests auto-retract when obstruction detected |
| `pet_presence_test` | Tests that pet in doorway keeps door open |
| `power_lockout_test` | Tests that door doesn't respond when power off |
| `safety_lock_test` | Tests outside sensor safety lock feature |
| `schedule_test` | Tests schedule add/remove functionality |
| `full_test_suite` | Comprehensive test of all simulator features |

List available scripts:
```bash
python -m powerpetdoor.simulator --list-scripts
```

### Best Practices

#### 1. Start with Known State

Always set up the initial state before testing:

```yaml
steps:
  # Ensure clean starting state
  - action: set
    name: power
    value: "on"
  - action: set
    name: hold_time
    value: "2"
  - action: assert
    condition: door_status
    equals: DOOR_CLOSED
```

#### 2. Use Appropriate Timeouts

Set realistic timeouts for `wait_for` based on the action:

```yaml
# Door operations are fast
- action: wait_for
  condition: door_rising
  timeout: 2

# Full cycles take longer
- action: wait_for
  condition: door_closed
  timeout: 15
```

#### 3. Add Logging for Debugging

Use `log` actions to track progress:

```yaml
- action: log
  message: "=== Starting obstruction test ==="
- action: trigger_sensor
  sensor: inside
- action: log
  message: "Door triggered, waiting for open..."
```

#### 4. Clean Up After Tests

Reset state at the end of tests:

```yaml
# At end of test
- action: close
- action: set
  name: power
  value: "on"
- action: set
  name: safety_lock
  value: "off"
```

#### 5. Use Small Wait Times for Testing

Use short hold times to speed up tests:

```yaml
- action: set
  name: hold_time
  value: "1"    # 1 second instead of default 10
```

#### 6. Test Both Success and Failure Paths

```yaml
# Test that safety lock blocks outside sensor
- action: set
  name: safety_lock
  value: "on"
- action: trigger_sensor
  sensor: outside
- action: wait
  seconds: 0.5
- action: assert
  condition: door_status
  equals: DOOR_CLOSED    # Should NOT have opened
```

## Architecture

The simulator is organized into several modules:

```
powerpetdoor/simulator/
├── __init__.py      # Public API exports
├── state.py         # State dataclasses
│   ├── DoorTimingConfig   # Timing configuration
│   ├── BatteryConfig      # Battery simulation configuration
│   ├── Schedule           # Schedule entry
│   └── DoorSimulatorState # Full door state
├── protocol.py      # Protocol handler
│   ├── CommandRegistry    # @handler decorator registry
│   └── DoorSimulatorProtocol  # asyncio Protocol
├── server.py        # Main simulator class
│   └── DoorSimulator      # Server lifecycle & control
├── cli.py           # ppd-simulator command-line interface
│   ├── run_simulator()    # Interactive/script/daemon runner
│   └── main()             # Entry point / argument parsing
├── ctl.py           # ppd-simulator-ctl remote-control client
├── prompt_common.py # Shared prompt machinery (lexer, completer, sessions)
├── commands/        # Interactive command implementations
│   ├── handler.py         # CommandHandler dispatch
│   ├── base.py            # @command/@subcommand registry, ArgSpec parsing
│   └── ...                # door, buttons, settings, schedules, scripts, info, ...
├── scripting.py     # Script execution
│   ├── Script            # Script container
│   ├── ScriptStep        # Single step
│   └── ScriptRunner      # Executor
└── scripts/         # Built-in YAML scripts
    ├── basic_cycle.yaml
    ├── full_test_suite.yaml
    └── ...
```

### Command Registry Pattern

The simulator uses a decorator-based registry for clean command dispatch:

```python
from powerpetdoor.simulator import CommandRegistry

class MyProtocol:
    @CommandRegistry.handler(CMD_GET_SETTINGS)
    async def _handle_get_settings(self, msg, response):
        response[FIELD_SETTINGS] = self.state.get_settings()
```

This pattern replaces large if/elif chains with organized, self-documenting handlers.
