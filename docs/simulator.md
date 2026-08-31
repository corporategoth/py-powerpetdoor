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
- [Scripting System](#scripting-system) — see [docs/scripting.md](scripting.md)
- [Architecture](#architecture)
- [Fidelity to the real device](#fidelity-to-the-real-device)

## Fidelity to the real device

The simulator's job is to be **wrong in the same ways a real Power Pet Door
is wrong**, so a client that gets a wire shape subtly wrong fails in the
test suite rather than only against hardware. Everything in
[docs/protocol.md](protocol.md) marked is reproduced here, including
the parts that look like bugs:

- failure responses carry **no** `msgID`, and neither does a `PONG`;
- only `OPEN`/`OPEN_AND_HOLD`/`CLOSE` are accepted under `cmd`; every other
  command must be a `config`;
- `SET_NOTIFICATIONS` rejects a flat payload, and **accepts-and-ignores** a
  nested one whose values are strings;
- `SET_SCHEDULE` without a sibling `index` is rejected;
- the voltage setters take `voltage` and reject the getter's field name;
- the per-command value spellings are reproduced field by field
  (`"true"`/`"false"` strings in `GET_SETTINGS`, ints in `GET_SENSORS`),
  rather than normalized.

### The one deliberate divergence: multiple clients

**A real door serves exactly one connection.** A second connection is not
refused — the door simply stops answering the wire properly and both
clients see unexplained timeouts (see
[Connection](protocol.md#connection)).

**The simulator does not reproduce that, on purpose.** It accepts as many
clients as connect and serves each one as if it were exclusive. That is a
tool decision, not an oversight:

- the interactive CLI and `ppd-simulator-ctl` are frequently attached at
  the same time as the client under test, and contention emulation would
  make the simulator's own front end the thing that breaks;
- a test suite that runs cases in parallel against one daemon needs every
  connection served;
- "degrade into unexplained timeouts" is untestable by construction — a
  test for it would be a test for a hang.

There is no option to turn this off, and no `--max-clients`: the simulator
always serves every client. Field-debug single-connection symptoms against
the real door, with
[the protocol note](protocol.md#single-connection-and-the-field-debugging-trap-it-creates)
in hand.

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
| `--initial-state` `FILE` | State document applied over the defaults at startup, and restored by a bare `reset`. JSON always; YAML when PyYAML is installed. `--firmware`/`--hardware` win over it. Must parse. |
| `--states-dir` `DIR` | Directory of state documents `reset` can load by bare name. Must exist. |
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
- `--states-dir`: must be a directory
- `--initial-state`: must exist and parse as a state document

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
| `inside [on\|off\|toggle\|secs]` | `i` | Inside sensor. Bare = a brief pulse (a pet walking past); `on` holds it (a pet loitering); `off` releases; `toggle` flips; a number is seconds (`0` = indefinitely). A held sensor **is** pet presence: a collar sitting in range |
| `outside [on\|off\|toggle\|secs]` | `o` | Outside sensor; identical argument. Mutually exclusive with `inside` — a pet cannot be on both sides at once |
| `trigger <inside\|outside>` | `tr` | A pet **walks through** the sensor rather than standing at it. Extends the hold on an open door and retracts a closing one, where presence does neither, and leaves no pet behind |
| `open` | `hold`, `h` | Open the door and hold it open until something closes it |
| `cycle` | `y` | Full door cycle — open, hold for `hold_time`, close (like pressing the door button; bypasses sensor enable checks) |
| `close` | `c` | Close the door |
| `toggle` | `tg` | Open the door if closed, close it if open. Mirrors `PowerPetDoor.toggle()`, including its no-op mid-travel: nothing but an obstruction is known to interrupt a real door in motion |

### Simulation Events

| Command | Aliases | Action |
|---------|---------|--------|
| `obstruction [on\|off\|toggle\|secs]` | `x` | A physical blockage. **Not a sensor** — it does not stop a close from starting; the door travels down and meets it at the bottom, so one placed on a closed door waits for the next close. Same argument as the sensors, except a bare `obstruction` **toggles** rather than pulsing: a pet walks past a sensor, a boot is placed. Bare places a one-shot the retract clears; `on`/`0` leave it until cleared |

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
| `safety [on\|off]` | `s` | The app's *\"always allow pet entry inside override timers\"*. ON lets the outside sensor open the door **regardless of the schedule** — it grants entry, it does not deny it. The wire name (`outsideSensorSafetyLock`) reads like the opposite; see [operation.md](operation.md#outside-sensor-safety-lock) |
| `lockout [on\|off]` | `l` | The app's *\"allow pet to keep door open\"*, **inverted**: ON means the lockout is in force, so the door ignores a nearby pet and closes on its timer. OFF means a detected pet holds it open |
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
| `list [scripts\|states]` | `/`, `scripts` | Bare `list` and `list scripts` list runnable scripts (built-in, plus any from `--scripts-dir` — the header for that directory is printed even when it is empty, so "not configured" and "configured but empty" are distinguishable), ending with the runner's current state and, if anything is waiting, a `Queued: <names>` line naming the pending runs |
| `list states` | | State documents `reset` can load by bare name, from `--states-dir`. There are no built-in ones — a shipped state document would be invented device configuration rather than observed — so without the flag the listing says which flag is missing. Only documents that resolve *inside* the directory are advertised, so what is listed is exactly what `reset` accepts |
| `stop` | | Stop the **running script** at its next step boundary (the run then reports FAILED). Leaves the queue alone — only `stop all` touches it — and says so: with runs pending the answer is `Stopping script: <name> (N still queued; use 'stop all' to discard them)`, because the observable consequence is that the *next* script immediately starts driving the door. While the request is pending, `status`/`list` show `Script: stopping "<name>"`, and a repeat `stop` answers `Stop already requested for: <name>`. With nothing running but runs still pending it reports how many are queued and points at `stop all`. Does *not* stop the simulator — use `shutdown` for that |
| `stop all` | | As `stop`, and additionally discards **every** run still queued — including one already taken off the queue but not yet started — reporting how many were dropped. The count always matches the `queued` figure `status`/`list` showed a moment earlier, and one `stop all` is always enough. Idempotent: with nothing running or queued it succeeds with `Nothing running or queued` |

### Info

| Command | Aliases | Action |
|---------|---------|--------|
| `get [name]` | | Show one value, or every value split into **Door** (what a real door has) and **Simulation** (the simulator's own knobs — flap timings, battery rates). Reaches everything `set` reaches, plus the read-only ones (`door_status`, `position`, `time`) |
| `set <name> <value>` | | Change any named value. `toggle` as the value inverts a yes/no one. The named commands below (`power`, `safety`, …) are the same values under shorter words; this reaches the ones that have no word of their own |
| `status` | `state`, `info`, `v` | Show the full simulator state (connected clients, door, power, sensors, settings, battery, notifications, schedules, statistics) and the script runner's state (`Script: none running` / `Script: running "<name>" (N queued)`) |
| `help` | `?` | Show all available commands |
| `broadcast <what>` | `bc` | Push an unsolicited update to connected door clients. `<what>` is one of `status`, `settings`, `battery`, `hwinfo`, `stats`, `schedules`, `notifications`, `all`. Errors if no client is connected |
| `history [N\|clear]` | `hist` | Show the last N commands (default 20) or clear history. Needs an interactive terminal session with prompt_toolkit installed; otherwise the command is hidden and reported as unknown |

### Control

| Command | Aliases | Action |
|---------|---------|--------|
| `debug [on\|off]` | | Show or set debug logging |
| `shutdown` | | Stop the **simulator**. `stop` is *not* an alias for this — it stops the running script |
| `reset [document]` | | Return the door to a known state: the `--initial-state` document if one was given, otherwise the defaults. With an argument, to that state document instead. Stops the door, parks it closed, clears the sensors, any obstruction and the statistics, then broadcasts everything |
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
    simulator.trigger_sensor("inside")  # Pet going out
    simulator.trigger_sensor("outside")  # Pet coming in

    # Direct door control
    await simulator.open_door()  # Open normally
    await simulator.open_door(hold=True)  # Open and keep up
    await simulator.close_door()  # Close immediately

    # Simulate events
    simulator.simulate_obstruction()  # One-shot: retract clears it
    simulator.simulate_obstruction(0)  # Stays until cleared
    simulator.clear_obstruction()  # Remove it
    simulator.set_pet_in_doorway(True)  # Pet blocking door
    simulator.set_pet_in_doorway(False)  # Pet moved away

    # Battery simulation
    simulator.set_battery(75)  # Set to 75%
    simulator.set_ac_present(True)  # AC power connected
    simulator.set_ac_present(False)  # AC power disconnected
    simulator.set_battery_present(True)  # Battery installed
    simulator.set_battery_present(False)  # Battery removed
    simulator.set_charge_rate(1.0)  # 1%/min charge rate
    simulator.set_discharge_rate(0.1)  # 0.1%/min discharge rate
```

### Modifying State

```python
async def modify_state(simulator):
    state = simulator.state

    # Power and sensors
    state.power = True
    state.inside = True  # Enable inside sensor
    state.outside = True  # Enable outside sensor

    # Safety features
    state.safety_lock = False  # Outside sensor safety lock
    state.cmd_lockout = False  # Command lockout
    state.autoretract = True  # Auto-retract on obstruction

    # Timing
    state.hold_time = 10  # Seconds door stays open
```

### Driving a simulator you own, or a daemon you do not

`simulator_control` gives one interface over both: a simulator started in
this process, or an existing `--daemon` reached over its control port. The
same test code runs against either.

```python
from powerpetdoor.simulator import simulator_control

# A simulator of our own, on the door port
async with simulator_control(port=3000) as sim:
    await sim.open()
    await sim.run_script("obstruction_test", wait=True)

# ...or a running daemon, on its CONTROL port
async with simulator_control("127.0.0.1", 3001, remote=True) as sim:
    await sim.obstruction()
    await sim.reset()
```

**The shared surface is commands, not state.** The local half exposes the
live `sim.simulator.state`, because it is the same object in the same
process. The remote half has no state accessor at all: over a socket a
state could only ever be a copy, and making a stale copy *look* live would
be the same bug the vendor app has — see
[The vendor app](protocol.md#the-vendor-app-and-why-your-setting-changed-back).

To assert against a remote door, run a script — `run <name> wait` reports
PASSED/FAILED over the channel, and `assert` steps are where this project
already puts assertions.

**Three lifecycle verbs**, for the same reason `stop` and `shutdown` are
already distinct:

| Method | Local | Remote |
|--------|-------|--------|
| `stop_script()` | stops the running script | sends `stop` |
| `close()` | stops the simulator it owns | **disconnects only** |
| `shutdown()` | stops the simulator | sends `shutdown` — ends the daemon |

Leaving the `async with` block calls `close()`, so a client that merely
dialled a daemon never stops it on the way out.

### State Documents

The simulator's configuration serializes to a document — the schema
`--initial-state` and `reset` share.

```python
from powerpetdoor.simulator import state_to_document, apply_document

document = state_to_document(simulator.state)  # snapshot the config
apply_document(simulator.state, {"settings": {"hold_time": 15}})
```

Documents are **partial**: every section and key is optional and merges
over the defaults, so a fixture says only what it cares about. Unknown keys
are refused rather than ignored, and `schedules` replaces the whole table
rather than merging per index — a reset to a one-entry document must not
leave a second entry behind.

Live motion state (`door_status`, the sensor flags, whether an obstruction
is present) is deliberately **not** in the document: the engine owns it, and
a document that could name `DOOR_RISING` would be a way to put the engine
in a state it cannot reach on its own.

```json
{
  "settings": {"hold_time": 15, "power": true, "timezone": "America/New_York"},
  "battery": {"percent": 40, "ac_present": false},
  "schedules": [
    {"index": 0, "enabled": true, "inside": true, "start": "07:00", "end": "19:00"}
  ]
}
```

JSON always works. YAML works when PyYAML is installed — it is an optional
dependency, so a state file must not be the thing that makes it mandatory,
and JSON is what the door itself speaks anyway.

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
        inside=True,  # This schedule controls inside sensor
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
    script = Script.from_simple_commands(
        [
            "trigger inside",
            "wait_for door_closed 10",
            "assert door_status DOOR_CLOSED",
        ],
        name="Quick Test",
    )
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
        host="127.0.0.1", port=3000, keepalive=30.0, timeout=10.0, reconnect=5.0, loop=loop
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

The simulator runs YAML scripts to drive a door through a sequence of
events and assertions, unattended. That is a language of its own - actions,
conditions, `if`/`else`, `repeat`, assertions - so it has its own document:

**→ [docs/scripting.md](scripting.md)**

The commands that *run* scripts (`run`, `list`, `stop`) are documented with
the rest of the simulator's commands, [above](#scripts).

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
