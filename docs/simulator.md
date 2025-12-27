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

For YAML script support, install with the optional dependency:

```bash
pip install pypowerpetdoor[yaml]
# or
pip install pyyaml
```

## Quick Start

Start the simulator on the default port (3000):

```bash
python -m powerpetdoor.simulator
```

Then connect your client to `localhost:3000`.

## Command Line Usage

### Basic Options

```bash
# Start on a specific port
python -m powerpetdoor.simulator --port 3001

# Bind to a specific address
python -m powerpetdoor.simulator --host 127.0.0.1

# Enable debug logging
python -m powerpetdoor.simulator --debug
```

### Running Scripts

```bash
# Run a built-in script interactively
python -m powerpetdoor.simulator --script basic_cycle

# Run a script from a file
python -m powerpetdoor.simulator --script-file /path/to/my_test.yaml

# Run script and exit (useful for CI/CD)
python -m powerpetdoor.simulator --script full_test_suite --exit-after-script

# List available built-in scripts
python -m powerpetdoor.simulator --list-scripts
```

### Exit Codes

When using `--exit-after-script`:
- **0**: Script completed successfully (all assertions passed)
- **1**: Script failed (assertion failed or error occurred)

This makes it easy to integrate with CI/CD pipelines:

```bash
python -m powerpetdoor.simulator -s full_test_suite -e || echo "Tests failed!"
```

## Interactive Mode

When running without `--exit-after-script`, the simulator provides an interactive keyboard interface:

### Door Operations

| Key | Action |
|-----|--------|
| `i` | Trigger inside sensor (pet going out) |
| `o` | Trigger outside sensor (pet coming in) |
| `c` | Close door immediately |
| `h` | Open and hold (stays open until 'c') |

### Physical Buttons

These simulate the physical buttons on the door unit:

| Key | Action |
|-----|--------|
| `p` | Toggle power on/off |
| `m` | Toggle auto/tiMers (schedule enable) |
| `n` | Toggle iNside sensor enable |
| `u` | Toggle oUtside sensor enable |

### Simulation Events

| Key | Action |
|-----|--------|
| `x` | Simulate obstruction (triggers auto-retract) |
| `d` | Toggle pet in doorway (keeps door open) |

### Settings

| Key | Action |
|-----|--------|
| `s` | Toggle outside sensor safety lock |
| `l` | Toggle command lockout |
| `a` | Toggle auto-retract |
| `t <sec>` | Set hold time (e.g., `t 5`) |
| `b [pct]` | Set battery level (random if no value) |

### Schedules

| Key | Action |
|-----|--------|
| `1` | Add sample schedule #1 (all days, 6am-10pm) |
| `2` | Add sample schedule #2 (weekdays, 7am-6pm) |
| `3` | Delete schedule #1 |

### Scripts

| Command | Action |
|---------|--------|
| `r <name>` | Run a built-in script by name |
| `f <path>` | Run a script from a file |
| `/` | List available built-in scripts |

### Info

| Key | Action |
|-----|--------|
| `?` | Show current door state |
| `q` | Quit simulator |

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
    schedule = Schedule(
        index=1,
        enabled=True,
        days_of_week=0b0111110,   # Mon-Fri (bit 0 = Sunday)
        inside_start_hour=7,
        inside_end_hour=18,
        outside_start_hour=7,
        outside_end_hour=18,
    )

    # Add and remove schedules
    simulator.add_schedule(schedule)
    simulator.remove_schedule(1)
```

### Running Scripts Programmatically

```python
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
    script = Script.from_file("/path/to/my_test.yaml")
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
    loop = asyncio.get_event_loop()
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
│   ├── Schedule           # Schedule entry
│   └── DoorSimulatorState # Full door state
├── protocol.py      # Protocol handler
│   ├── CommandRegistry    # @handler decorator registry
│   └── DoorSimulatorProtocol  # asyncio Protocol
├── server.py        # Main simulator class
│   └── DoorSimulator      # Server lifecycle & control
├── cli.py           # Command-line interface
│   ├── run_simulator_interactive()
│   └── main()
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
