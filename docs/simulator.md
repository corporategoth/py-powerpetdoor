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
  - [Settings for `set`](#settings-for-set)
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

Mode-scoped flags are rejected outside their mode rather than silently
ignored: `--loop`, `--script-delay`, `--oneshot` and `--wait-for-client`
cannot be used without `--script` (in daemon mode, where `--script` is itself
refused, the error says so directly), and `--control-host` requires
`--daemon`.

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
| `--list-scripts`, `-l` | List runnable scripts (built-in, plus `--scripts-dir` if given) and exit |
| `--daemon`, `-D` `[CONTROL_PORT]` | Run in daemon mode with a control channel (default control port: door port + 1) |
| `--control-host` `ADDR` | Bind address for the daemon control channel (default: `127.0.0.1`). Requires `--daemon`. See the [security note](#daemon-mode) before widening this. |
| `--scripts-dir` `DIR` | Directory of extra YAML scripts runnable by bare name, in addition to the built-ins. They appear in `list`, in `--list-scripts`, in unknown-script errors, and in the *simulator CLI's* tab completion (ctl's completer cannot see them). Must exist. |
| `--run-for`, `-r` `SECONDS` | Maximum run time in seconds (`--oneshot` can exit earlier) |
| `--history` `FILE` | Prompt history file, or `none` to disable (default: `~/.powerpetdoor_simulator_history`; ignored when prompt_toolkit is not installed) |
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

# List runnable scripts (built-in, plus --scripts-dir if given)
ppd-simulator --list-scripts
```

### Exit Codes

When using `--oneshot`:

- **0**: All scripts completed successfully (all assertions passed)
- **1**: A script failed (assertion failed or error occurred), **or** the run
  produced no verdict at all
- **2**: A bad argument (argparse usage error)
- **130**: Interrupted by Ctrl-C (`128 + SIGINT`)

This makes it easy to integrate with CI/CD pipelines:

```bash
ppd-simulator -s full_test_suite --oneshot || echo "Tests failed!"
```

**An interrupted run never reports success.** Ctrl-C mid-run prints
`>>> Interrupted after N of M script(s)` — not a PASSED/FAILED verdict, because
the remaining assertions never ran — and exits 130. `ppd-simulator --daemon`
interrupted by Ctrl-C exits 130 too, matching `ppd-simulator-ctl`.

### Argument Validation

Everything bind-time is checked before anything is bound, so a bad value is an
argparse usage error at **rc 2** rather than a traceback:

- `--port`, `--daemon PORT`: must be 0-65535
- `--host`, `--control-host`: must resolve
- `--run-for`: must be greater than 0
- `--scripts-dir`: must be a directory

If a port is in range but already taken, startup prints one sentence naming the
role of the port that failed and the flag that changes it, and exits 1:

```
$ ppd-simulator --port 3000 --daemon
Cannot start: door server cannot use 0.0.0.0:3000 (error while attempting to
bind on address ('0.0.0.0', 3000): [errno 98] address already in use); change
it with --port
```

`--debug` additionally prints the traceback.

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
| `pet [on\|off]` | `d` | Pet standing in the doorway (a present pet keeps the door open). Bare `pet` toggles, and a `toggle`/`t` subcommand is also available — the same mechanism as the `pet_presence`/`pet_on`/`pet_off` script actions |

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
| `holdtime [seconds]` | `t` | Set hold time (0.1–900 seconds); bare shows the current hold time |
| `battery [percent]` | `b` | Set battery level 0–100; bare shows the current level |
| `battery random` | | Set a random battery level (10–100) |
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
| `run <script>` | `r`, `file` | Queue a script — built-in name, `--scripts-dir` name, or YAML file path. The command returns as soon as the script is queued; the PASSED/FAILED result is only logged. A queued script waits for any script already running |
| `run <script> wait` | `r`, `file` | Run the script synchronously and report `Script PASSED`/`Script FAILED` as the command result. Fails immediately with `Another script is already running: <name>` rather than queueing, so the result always belongs to the script you asked for. Over ctl this is the only form whose exit code reflects the script |
| `list` | `/`, `scripts` | List runnable scripts (built-in, plus any from `--scripts-dir` — the header for that directory is printed even when it is empty, so "not configured" and "configured but empty" are distinguishable), ending with the runner's current state and, if anything is waiting, a `Queued: <names>` line naming the pending runs |
| `stop` | | Stop the **running script** at its next step boundary (the run then reports FAILED). Leaves the queue alone — only `stop all` touches it — and says so: with runs pending the answer is `Stopping script: <name> (N still queued; use 'stop all' to discard them)`, because the observable consequence is that the *next* script immediately starts driving the door. While the request is pending, `status`/`list` show `Script: stopping "<name>"`, and a repeat `stop` answers `Stop already requested for: <name>`. With nothing running but runs still pending it reports how many are queued and points at `stop all`. Does *not* stop the simulator — use `shutdown` for that |
| `stop all` | | As `stop`, and additionally discards **every** run still queued — including one already taken off the queue but not yet started — reporting how many were dropped. The count always matches the `queued` figure `status`/`list` showed a moment earlier, and one `stop all` is always enough. Idempotent: with nothing running or queued it succeeds with `Nothing running or queued` |

### Info

| Command | Aliases | Action |
|---------|---------|--------|
| `status` | `state`, `info`, `v` | Show the full simulator state (connected clients, door, power, sensors, settings, battery, notifications, schedules, statistics) and the script runner's state (`Script: none running` / `Script: running "<name>" (N queued)`) |
| `help` | `?` | Show all available commands |
| `broadcast <what>` | `bc` | Push an unsolicited update to connected door clients. `<what>` is one of `status`, `settings`, `battery`, `hwinfo`, `stats`, `schedules`, `notifications`, `all`. Errors if no client is connected |
| `history [N\|clear]` | `hist` | Show the last N commands (default 20) or clear history. Needs an interactive terminal session with prompt_toolkit installed; otherwise the command is hidden and reported as unknown |

### Control

| Command | Aliases | Action |
|---------|---------|--------|
| `debug [on\|off]` | | Show or set debug logging |
| `shutdown` | | Stop the **simulator**. `stop` is *not* an alias for this — it stops the running script |
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
newline-terminated. Every line the daemon sends is one of four kinds:

| Line | Meaning |
|------|---------|
| `OK: <message>` | The command succeeded |
| `ERROR: <message>` | The command failed |
| `LOG: <line>` | A simulator log record, broadcast to every connected control client |
| `STATUS: clients=<n>` | Number of connected **door** clients. Sent immediately on connect, then again whenever a door client connects or disconnects. `ppd-simulator-ctl` uses it to color the prompt |

Only `OK:`/`ERROR:` are command responses; a client must skip `LOG:` and
`STATUS:` lines while waiting for one. `STATUS:` arrives before any command
is sent, so a client that assumes the first line is a response will misparse
it.

`OK:`/`ERROR:`/`LOG:` message text is escaped so one protocol line is always
one physical line: **backslashes are doubled first (`\` → `\\`), then
newlines become `\n`**. Unescape in the reverse order (split on `\\`
first, then replace `\n` with a newline inside each piece) — a client that
only handles `\n` corrupts messages containing literal backslashes, such as
Windows-style script paths.

> **Security note**: the control channel is **unauthenticated** — anyone who
> can connect to it can drive the simulator, run scripts, and shut it down.
> It therefore binds `127.0.0.1` (loopback only) by default. Pass
> `--control-host` with a wider address only on networks you trust.
>
> The door protocol server itself binds `0.0.0.0` by default because it
> emulates a LAN device — use `--host 127.0.0.1` if you do not want it
> reachable from the network.

Over the control channel, `run` accepts only **bare script names** (no path
separators or traversal). A bare name resolves against the `--scripts-dir`
directory first (if one was given), then the built-in scripts. Running an
arbitrary YAML file path is only possible locally, via the interactive CLI or
`--script`.

Start the daemon with `--scripts-dir DIR` to make your own YAML scripts
runnable by bare name. They then show up in `list`, in `--list-scripts` and in
the "Available:" hint of an unknown-script error, so a ctl user who did not
start the daemon can still discover them with `list`.

A file in that directory that *resolves outside* it (a symlink pointing away)
is not runnable by bare name, and is not listed by `list`, `--list-scripts`,
the `Available:` hint or tab completion either — all four surfaces agree with
the loader. Naming it explicitly over the control channel answers `Script
'<name>' resolves outside <dir> and cannot be run by name; move it into the
directory (paths are not accepted over the control channel)`. Locally — in the
interactive CLI or via `--script` — the same refusal ends `move it into the
directory or run it by path`, because there running it by path really is a
remedy.
A scripts-dir script whose name matches a built-in **shadows** it; both `list`
and `--list-scripts` mark the built-in `(shadowed by <path-to-the-file>)`,
naming the real file with its `.yaml`/`.yml` suffix, and tab completion offers
the name once.

Tab completion for those names works in the **simulator CLI only**.
`ppd-simulator-ctl` is a separate process that never learns the daemon's
`--scripts-dir`, so its completer offers the built-in names only — use `list`
to see the rest. ctl also does not complete local YAML files or directories,
because the daemon refuses script *paths* over the control channel; the
simulator CLI, which can run them, still does. A nonexistent `--scripts-dir` is rejected at startup, and an
existing but empty one logs a warning.

## Remote Control (ppd-simulator-ctl)

`ppd-simulator-ctl` sends commands to a running daemon's control channel.

### One-Shot Commands

```bash
ppd-simulator-ctl status                  # Show simulator state
ppd-simulator-ctl inside                  # Trigger the inside sensor
ppd-simulator-ctl holdtime 2              # Change a setting
ppd-simulator-ctl run basic_cycle wait    # Run a script and wait for its result
ppd-simulator-ctl run basic_cycle         # Queue a script (returns immediately)
ppd-simulator-ctl shutdown                # Stop the daemon
```

The exit code is **0** on success, **1** on error (unknown command,
validation failure, or connection failure), **2** on a bad argument and
**130** on Ctrl-C, so one-shot commands are scriptable.

An empty or whitespace-only command is refused locally at rc 2 (`error: empty
command`) rather than sent: the daemon has no answer for a blank line, so it
could only ever time out. `--timeout` must be greater than 0 — `run <script>
wait` is the spelling for "wait as long as it takes".

**For `run`, only `run <script> wait` reflects the script result** (0 passed,
1 failed). Plain `run <script>` just queues the script and exits **0** as soon
as it is queued — a CI job that omits `wait` always sees success, whatever the
script does. (A script that fails to *load* is still an error and exits 1: the
load happens before the enqueue.)

```bash
ppd-simulator-ctl run full_test_suite wait || echo "Tests failed!"
```

A wait-run is deliberately **not** bound by `--timeout`: the script's runtime
is unbounded and may be entirely silent, so ctl waits for as long as the
daemon connection is alive. For every other command `--timeout` bounds a *gap*
in daemon traffic rather than the total wait, so streaming `LOG:` output keeps
a long-running command alive.

While a wait-run is in flight ctl streams the daemon's `LOG:` lines to
**stderr** as they arrive, so a CI job sees progress and — when a script fails
— the assertion text that explains why. stdout carries only the single result
line, so `ppd-simulator-ctl run x wait` stays scriptable:

```bash
ppd-simulator-ctl run full_test_suite wait 2>run.log || cat run.log
```

Only one script runs at a time. A wait-run issued while another script is
running fails immediately with `Another script is already running: <name>`
instead of interleaving; a plain (queued) `run` waits its turn. `status` and
`list` report what is running and how deep the queue is (including a run
already taken off the queue but still waiting for the runner), `list` names
the pending runs, `stop` ends the running script and leaves the queue alone,
and `stop all` ends the running script *and* discards every pending run —
the claimed-but-not-started one included, so a single `stop all` always
leaves nothing running or queued.

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
| `--timeout`, `-t` `SECONDS` | Seconds of daemon silence tolerated while waiting for a response (default: 5). Restarted by any received line; ignored entirely for `run <script> wait` |
| `--history` `FILE` | History file path, or `none` to disable (default: `~/.powerpetdoor_ctl_history`; ignored when prompt_toolkit is not installed) |

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
    # days_of_week is a list of booleans: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
    # (1/0 also work; from_dict() normalizes wire data to booleans)
    schedule = Schedule(
        index=1,
        enabled=True,
        days_of_week=[False, True, True, True, True, True, False],  # Mon-Fri
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
        "wait_for door_closed 10",
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
  - action: wait_for
    condition: door_closed
    timeout: 10
  - action: assert
    condition: door_status
    equals: DOOR_CLOSED
```

Note the `wait_for` rather than a fixed `wait`: the hold timer only starts
once the door *reaches* `DOOR_HOLDING`, so a `wait` long enough today
becomes a flaky failure the moment `hold_time` changes. The built-in
scripts all use `wait_for` for this reason.

### Available Actions

Every step parameter is validated against the action's known set: an
unrecognised key fails the step with `Unknown parameter(s) for <action>:
<key>. Use: <known>`. Unknown parameters used to be ignored silently while
the progress log echoed them back as if accepted, so `wait: {duration: 8}`
(`duration` is a real parameter name — for `inside`/`outside`) waited the
1.0 s default instead of 8 and still reported PASSED.

Three keys are exempt, because annotating a step is an ordinary thing to
do and must not have to look like a typo. **`note`, `comment` and
`description` are accepted on any step and are read by nothing**:

```yaml
- action: wait
  seconds: 1
  note: let the door settle before asserting
```

They are the only exemptions. A misspelled *real* parameter still fails
loudly — that is the whole point of the check — so `duration:` on a `wait`
is an error whether or not the step also carries a `note:`.

#### Door Operations

**trigger_sensor** / **trigger**
Trigger a pet sensor to open the door.
```yaml
- action: trigger_sensor
  sensor: inside    # "inside" or "outside" - anything else fails the step
```

`sensor` is validated like every other name in this DSL: a misspelling
fails the step, and therefore the run. It used to be accepted silently and
synthesised a third "sensor" that ignored the enable flags, the safety lock
and the schedule — so a one-character typo opened the door with everything
disabled and still reported PASSED.

**inside** / **outside**
Hold one sensor active for a chosen time. `trigger_sensor` has no duration,
so these are the way to keep a sensor asserted across a door phase (the
interactive `inside` / `outside` commands do the same thing).
```yaml
- action: inside
  duration: 1.5     # Seconds (default: 0.5); 0 = toggle, on until toggled off
```
Activating one sensor clears the other — they are mutually exclusive, as on
the real door.

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

> **Numeric bounds.** Every delay a script can ask for — `inside`/`outside`
> `duration`, `wait` `seconds` and `wait_for` `timeout` — must be a finite
> number between 0 and 86400 seconds. `.inf`/`.nan` and out-of-range values
> fail the step (and therefore the run) with a message naming the field,
> rather than being handed to `asyncio.sleep`: a NaN delay used to raise
> inside a background task, so the step silently did not happen while the
> run still reported PASSED. `set hold_time` and `set battery` are bounded
> the same way (0–900 seconds and 0–100 percent), as are `add_schedule` /
> `remove_schedule` indices (0–255).

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

#### Conditions for `wait_for`

These names are accepted by the `wait_for` action **only**. `assert` uses a
separate, disjoint set — see [Conditions for `assert`](#conditions-for-assert)
below.

| Condition | Description |
|-----------|-------------|
| `door_closed` | Door is fully closed |
| `door_open` | Door is open (holding or keepup) |
| `door_rising` | Door is currently opening |
| `door_holding` | Door is open and in hold timer |
| `door_keepup` | Door is open and held indefinitely |
| `door_closing` | Door is closing (from the top or from mid-travel) |
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

#### Conditions for `assert`

`assert` does **not** accept any of the names above; it checks a value
against an expectation, using this separate set:

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

### Settings for `set`

| Setting | Type | Description |
|---------|------|-------------|
| `power` | boolean | Main power on/off |
| `auto` | boolean | Schedule/timers enabled |
| `inside` | boolean | Inside sensor enabled |
| `outside` | boolean | Outside sensor enabled |
| `autoretract` | boolean | Auto-retract on obstruction |
| `safety_lock` | boolean | Outside sensor safety lock |
| `cmd_lockout` | boolean | Command lockout |
| `hold_time` | number | Seconds door stays open (fractional values allowed, e.g. `1.5`) |
| `battery` | integer | Battery percentage (0-100) |

`toggle` accepts the **boolean** rows only — the seven from `power` through
`cmd_lockout`. `hold_time` and `battery` hold a value rather than a state,
so `toggle hold_time` fails with `Unknown setting to toggle: hold_time`; use
`set` for those two.

Boolean values accept: `true`, `false`, `on`, `off`, `yes`, `no`, `1`, `0`, `enabled`, `disabled`

### Built-in Scripts

The simulator includes several built-in test scripts:

These descriptions are the scripts' own `description` fields; `--list-scripts`
prints the authoritative list.

| Script | Description |
|--------|-------------|
| `basic_cycle` | Pet triggers inside sensor, door opens, holds, then closes |
| `obstruction_test` | Tests that door auto-retracts when obstruction detected |
| `pet_presence_test` | Tests that pet in doorway keeps the door open past its hold time |
| `power_lockout_test` | Tests that commands are blocked when power off or lockout enabled |
| `safety_lock_test` | Tests that outside sensor is blocked when safety lock enabled |
| `schedule_test` | Tests that sensors respect schedule time windows |
| `full_test_suite` | Comprehensive test of all simulator features |

List available scripts (including any from `--scripts-dir`):
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
├── engine.py        # Door-motion state machine
│   └── DoorMotionEngine   # Single open/hold/close/retract runner shared
│                          # by the protocol and no-client paths
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
│   └── ScriptRunner      # Executor (serialized: one script at a time)
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
