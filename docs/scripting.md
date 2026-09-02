<!-- Scripting lived in docs/simulator.md until it outgrew it. -->

# Simulator Scripting

The simulator runs YAML scripts: sequences of door events and assertions
that drive a simulated door without a human at the keyboard. They are this
project's **CI** front end - `ppd-simulator --script <name> --oneshot`
exits non-zero when an assertion fails - so the DSL is deliberately strict:
every misspelled action, parameter, sensor, setting and condition fails
loudly rather than being ignored.

See [docs/simulator.md](simulator.md) for the simulator itself, and for the
`run`/`list`/`stop` commands that drive scripts from the CLI, from
`ppd-simulator-ctl`, or over a daemon's control channel.

## Table of Contents

- [Script Format](#script-format)
- [Available Actions](#available-actions)
- [Conditions](#conditions)
- [Settings for `set`](#settings-for-set)
- [Built-in Scripts](#built-in-scripts)
- [Best Practices](#best-practices)

## Script Format

Scripts are YAML files with the following structure:

```yaml
name: "My Test Script"
description: "Description of what this script tests"
steps:
  - action: trigger
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

## Available Actions

Every step parameter is validated against the action's known set: an
unrecognised key fails the step with `Unknown parameter(s) for <action>:
<key>. Use: <known>`. This matters because parameter names are shared:
`duration` is real for `inside`/`outside` but not for `wait`, so
`wait: {duration: 8}` is a typo rather than an eight-second wait.

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

### Door Operations

**trigger**
Fire a sensor once, as a pet walking through would. Respects the sensor
enables, the safety lock, command lockout, power and the schedule.
```yaml
- action: trigger
  sensor: inside        # or outside
```

**inside** / **outside**
The sensor as a *state* rather than an event: a collar sitting in range.
Takes one argument, the same one `obstruction` takes.

| Argument | Meaning |
|----------|---------|
| *(omitted)* | a brief pulse — a pet walking past |
| `on` | held indefinitely — a pet loitering there |
| `off` | released |
| `toggle` | flip whichever it is |
| *number* | held for that many seconds (`0` = indefinitely) |

```yaml
- action: inside
  state: on
- action: outside
  duration: 2
```

A held sensor *is* pet presence: a collar sitting in range.

The two sensors are mutually exclusive: a pet cannot be on both sides of
the flap at once, so holding one releases the other.

**A held sensor still respects every gate.** With the sensor disabled, or
power off, or command lockout on, or outside a scheduled window, the pet is
*recorded* but the door stays shut — and it is admitted the moment the gate
opens, without having to re-trigger.

**open**
Open the door and hold it open — the door parks in `DOOR_KEEPUP` and stays
there until a `close` step (or the hold is otherwise broken). Takes no
parameters; for the timed open that closes itself, use `cycle`.
```yaml
- action: open
```

**cycle**
Run a full door cycle: the door rises, sits in `DOOR_HOLDING` for
`hold_time`, then closes itself — the same motion a sensor trigger
produces, but bypassing the sensor enable checks.
```yaml
- action: cycle
```

**close**
Close the door immediately.
```yaml
- action: close
```

### Simulation Events

**obstruction**
A physical blockage in the doorway — a pet with no collar, a boot, a block
of wood. **Not a sensor**: it does not stop a close from *starting*; the
door travels down and meets it at the bottom, so an obstruction placed on a
closed door simply waits for the next close.

Takes the argument `inside`/`outside` take, with one difference: a bare
`obstruction` **toggles** rather than pulsing. That difference is physical —
a pet walks past a sensor, a boot is placed.

| Argument | Meaning |
|----------|---------|
| *(omitted)* | toggle; placing a **one-shot** that the retract it causes clears |
| `on` | placed until cleared, surviving retracts |
| `off` | cleared |
| `toggle` | flip whichever it is |
| *number* | present for that many seconds (`0` = until cleared) |

```yaml
- action: obstruction
- action: obstruction
  state: off
```

With auto-retract **on**, the door reverses and counts a retract; an
until-cleared obstruction therefore opens and mostly-closes indefinitely
until something clears it. With auto-retract **off**, the motor stops and
the door rests on it — though a pet arriving still raises it, because a
door resting on a boot is still a door a collar can open.

**battery**
Set battery level.
```yaml
- action: battery
  percent: 75
```

### Timing

**wait**
Pause execution for a specified time.
```yaml
- action: wait
  seconds: 2.5
```

**wait_for**
Wait for a condition to become true (with timeout). Takes the same
`condition` + comparison `assert` does.
```yaml
- action: wait_for
  condition: door_closed     # a yes/no condition, asked bare
  timeout: 10                # Seconds (default: 30)

- action: wait_for
  condition: total_auto_retracts   # ...or a condition and a value
  equals: 3
  timeout: 30
```
By default a timeout fails the step, and so the run. `on_timeout: continue`
carries on instead — which is how a script branches on "it did not happen"
without a separate mechanism: the condition is still false afterwards, so
the next `if` sees it.
```yaml
- action: wait_for
  condition: door_open
  timeout: 5
  on_timeout: continue
- action: if
  condition: door_closed
  then:
    - action: log
      message: "the door never opened"
```

**if**
Run one block or another, on a condition.
```yaml
- action: if
  condition: obstruction
  equals: "on"
  then:
    - action: obstruction     # clear it
  else:
    - action: log
      message: "already clear"
```
Both branches are optional. Their steps are parsed when the script is
*loaded*, so a misspelled action inside an untaken `else` fails then rather
than the first time that branch happens to be reached.

The condition can be stated three ways — exactly one per step, since two
would be two answers to one question:

| Key | Meaning |
|-----|---------|
| `condition:` (+ optional `equals:`) | a single condition |
| `conditions:` | a list; **all** must hold |
| `any:` | a list; **at least one** must hold |

A list entry is either a condition name on its own, or a mapping with
`condition:` and a comparison — the same courtesy the step list gives,
where `- close` is a whole step.

```yaml
- action: if
  conditions:
    - obstruction                 # a name on its own
    - condition: door_closed      # ...or the pair form
    - condition: hold_time
      equals: 2
  then:
    - action: log
      message: "all three held"
```

**repeat**
Run a block a fixed number of times, while a condition holds, or both.

```yaml
- action: repeat
  times: 3
  steps:
    - action: cycle
    - action: wait_for
      condition: door_closed
```

A condition — in any of the three forms `if` accepts — makes it a **while**
loop, re-tested *before* each pass, so a condition already false runs the
body zero times.

```yaml
- action: repeat
  condition: obstruction
  equals: "on"
  steps:
    - action: obstruction        # clears it, ending the loop
```

Given both, it stops at whichever comes first — the useful shape for "do
this until the door settles, but never more than ten times".

```yaml
- action: repeat
  times: 10
  conditions: [door_closed, power]
  steps:
    - action: cycle
```

There is no `until:`. Every condition can be asked the other way round
with `not_equals`, so "repeat until X" is `while X not_equals on`.

`repeat` needs `times`, a condition, or both; one with neither says nothing
about what it does. `times` is bounded like every other script number, and a
condition-only loop is bounded too: reaching that backstop is an **error**,
not a quiet exit. This DSL is the CI front end, and a hang that reported
PASSED would be worse than one that merely hangs.

**reset**
Return the door to a known state — the `--initial-state` document if the
simulator was started with one, otherwise the defaults.
```yaml
- action: reset
```
With a `document`, reset to that state document instead. Names resolve
against `--states-dir` under the same path policy as running a script by
name, so a script arriving over the control channel cannot read
configuration from an arbitrary file.
```yaml
- action: reset
  document: quiet_night
```

> **Numeric bounds.** Every delay a script can ask for — `inside`/`outside`
> `duration`, `wait` `seconds` and `wait_for` `timeout` — must be a finite
> number between 0 and 86400 seconds. `.inf`/`.nan` and out-of-range values
> fail the step, and therefore the run, with a message naming the field.
> `set hold_time` and `set battery` are bounded the same way (0–900 seconds
> and 0–100 percent), as are `add_schedule` / `remove_schedule` indices
> (0–255).

### State Control

**set**
Set any named value — the same registry the CLI's `set` reaches, so
anything settable at the prompt is settable here. `name` is a value name
(`power`, `hold_time`, `timezone`, `safety_lock`, the simulation knobs
like `rise_time`, …); `schemas/script.schema.json` carries the exhaustive
list and an editor reading it will complete them for you.
```yaml
- action: set
  name: hold_time
  value: "5"
```

**notify**
Switch a notification on or off, so a script can arm one before waiting
on it. The five names are `inside_on`, `inside_off`, `outside_on`,
`outside_off` and `low_battery` — the `on`/`off` half names **whether the
sensor was enabled**, not whether it fired.
```yaml
- action: notify
  name: inside_off
  state: true
```
Omit `state:` to toggle. Equivalent to `set notify_inside_off on`, which
is the same registry entry under its full name.

**toggle**
Open the door if it is closed, close it if it is open — the same thing the
CLI's `toggle` does. **Nothing mid-travel**: an obstruction is the only
thing known to interrupt a real door in motion.
```yaml
- action: toggle
```
To invert a *setting*, use `set <name> toggle`, mirroring the CLI's
`power toggle` / `auto toggle` subcommands.

**add_schedule**
Add a schedule entry covering both sensors, every day, 24 hours
(`00:00`–`24:00`), so a script behaves the same at every time of day. Note
`24:00`, not `00:00`: coinciding ends are an EMPTY window on real firmware,
not a whole day.
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

**enable_schedule**
Switch a stored schedule on or off, the CLI's `schedule enable` /
`schedule disable`. The script fails if that slot is empty.
```yaml
- action: enable_schedule
  index: 1
  state: false
```

**clear_schedules**
Delete every schedule, the CLI's `schedule clear`.
```yaml
- action: clear_schedules
```

### Assertions & Logging

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

## Conditions

### Conditions

There is **one** vocabulary: every name below works in `assert`,
`wait_for`, `if` and `repeat` alike.

A name that reads as a yes/no question can be asked bare — `condition:
door_closed` means "is it closed". Everything else needs a comparison,
because there is no sensible default for a number or a status string.

| Condition | Kind | Notes |
|-----------|------|-------|
| `door_closed` | yes/no | Down — `DOOR_CLOSED`, `DOOR_IDLE` or `DOOR_POWEROFF` |
| `door_open` | yes/no | Up and stopped — `DOOR_HOLDING` or `DOOR_KEEPUP` |
| `door_closing` | yes/no | Travelling down, any of the three closing states |
| `door_opening` | yes/no | Travelling up — `DOOR_RISING` or `DOOR_SLOWING` |
| `door_status` | text | The exact state, e.g. `DOOR_KEEPUP`. Reads `DOOR_POWEROFF` whenever the power is off, whatever the flap was doing |
| `position` | number | 0 = closed, 100 = fully open. Mirrors `PowerPetDoor.position` |
| `power` | yes/no | |
| `auto` | yes/no | Schedules enabled |
| `inside` / `outside` | yes/no | Sensor **enabled** (not whether a pet is at it) |
| `autoretract` | yes/no | |
| `obstruction` | yes/no | A physical obstruction is in the doorway |
| `safety_lock` | yes/no | Outside sensor overrides the schedule |
| `cmd_lockout` | yes/no | Door ignores pet proximity and closes on its timer |
| `battery` | number | Percentage |
| `hold_time` | number | Seconds |
| `total_open_cycles` | number | |
| `total_auto_retracts` | number | |

A yes/no condition compares as a **boolean**, so `equals: on`,
`equals: true`, `equals: 1` and `equals: enabled` all mean the same thing.

`door_opening` and `door_closing` partition travel: no state is both, and
each of the five moving states is exactly one. `door_open` means *up and
stopped*, so it is false while the door is still rising — use `position`
if you want "not closed" regardless of direction.

Waiting on any door condition is **signalled by the engine**, not polled.
That matters: `DOOR_CLOSING` lasts about 180 ms on a real door, so a poll
would miss it on a slow runner and the script would see
`DOOR_CLOSING_TOP_OPEN` instead.

#### Comparisons

Exactly one per condition — two would be two answers to one question.

| Comparison | Meaning |
|------------|---------|
| `equals` | equal (case-insensitive for text) |
| `not_equals` | not equal |
| `above` | strictly greater — numbers only |
| `below` | strictly less — numbers only |
| `at_least` | greater or equal — numbers only |
| `at_most` | less or equal — numbers only |

```yaml
- action: wait_for
  condition: total_auto_retracts
  at_least: 3
- action: assert
  condition: battery
  below: 75
```

The four numeric comparisons refuse a non-numeric condition rather than
silently answering false, so `door_status above 25` fails loudly.

## Settings for `set`

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

## Built-in Scripts

The simulator includes several built-in test scripts:

These descriptions are the scripts' own `description` fields; `--list-scripts`
prints the authoritative list.

| Script | Description |
|--------|-------------|
| `basic_cycle` | Pet triggers inside sensor, door opens, holds, then closes |
| `obstruction_test` | Tests that door auto-retracts when it closes onto an obstruction |
| `pet_presence_test` | Tests that pet in doorway keeps the door open past its hold time |
| `power_lockout_test` | Tests that commands are blocked when power off or lockout enabled |
| `safety_lock_test` | Tests that the safety lock lets a pet in past a closed schedule window |
| `schedule_test` | Tests that sensors respect schedule time windows |
| `full_test_suite` | Comprehensive test of all simulator features |

List available scripts (including any from `--scripts-dir`):
```bash
python -m powerpetdoor.simulator --list-scripts
```

## Best Practices

### 1. Start with Known State

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

### 2. Use Appropriate Timeouts

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

### 3. Add Logging for Debugging

Use `log` actions to track progress:

```yaml
- action: log
  message: "=== Starting obstruction test ==="
- action: trigger
  sensor: inside
- action: log
  message: "Door triggered, waiting for open..."
```

### 4. Clean Up After Tests

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

### 5. Use Small Wait Times for Testing

Use short hold times to speed up tests:

```yaml
- action: set
  name: hold_time
  value: "1"    # 1 second instead of default 10
```

### 6. Test Both Success and Failure Paths

```yaml
# Test that safety lock blocks outside sensor
- action: set
  name: safety_lock
  value: "on"
- action: trigger
  sensor: outside
- action: wait
  seconds: 0.5
- action: assert
  condition: door_status
  equals: DOOR_CLOSED    # Should NOT have opened
```
