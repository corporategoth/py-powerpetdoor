# Development

How this codebase is put together, and where to look for things.

For what the door does, read [operation.md](operation.md); for what goes
over the wire, [protocol.md](protocol.md). This file is about the code.

## The two halves

```
powerpetdoor/                 the library — talks to a real door
├── client.py                 connection lifecycle, framing, dispatch
├── door.py                   the Pythonic facade over the client
├── const.py                  the wire vocabulary, shared by both halves
├── schedule.py               schedule compress/validate/diff
├── framing.py                brace-matched JSON frame extraction
├── sanitize.py               terminal-safety for network-derived text
└── tz_utils.py               IANA ↔ POSIX timezone helpers

powerpetdoor/simulator/       the simulator — pretends to be one
├── state.py       storage    plain fields, plus a few predicates
├── engine.py      core       door motion, sensors, obstruction
├── values.py      core       THE VALUE REGISTRY (read this first)
├── server.py      core       the simulator object every surface holds
├── notifications.py core     the five notifications and their switches
├── state_io.py    core       state documents (--initial-state, reset)
├── coerce.py                 shared input coercion
├── wire_values.py interface  how a value looks on TCP 3000
├── protocol.py    interface  the wire
├── commands/      interface  the prompt (and, through it, ctl)
├── scripting.py   interface  the YAML script DSL
├── control.py     interface  the programmatic/control-socket client
├── cli.py, ctl.py            the two front ends
└── prompt_common.py          what those two share
```

`const.py` is the only thing both halves import. Nothing in the library
imports the simulator — a test asserts this, because a simulator-only
constant living in `const.py` once shipped to consumers as public API.

## One core, several interfaces

The rule the simulator is built on:

> Behaviour belongs to the core. An interface decides *how to say it*,
> never *what it means*.

The prompt, the script DSL, the control socket and the wire are four ways
to reach one simulator. Each translates at its own edge — the wire says
`"true"`, the prompt says `ON`, and both mean the same boolean — but none
of them reaches around the core to storage, and none of them implements
behaviour a sibling also implements.

This is not decoration. Every one of these was a real defect first:
`inside_enable on` at the prompt left a waiting pet outside while
`ENABLE_INSIDE` off the wire let it in; `power off` dropped the flap on
the wire and not at the prompt; `SET_TIMEZONE` echoed an IANA name while
every other command answered POSIX.

### The value registry

`values.py` holds every value the simulator can show or change — 37 of
them, 34 writable. One row declares how a value reads, how it writes,
what it accepts, and what side effects it has:

```python
"power": ValueSpec("bool", lambda s: s.power, "Main power", _power),
```

Everything else is derived from that row:

| Surface | Reaches it via | Generated from the row? |
|---------|----------------|-------------------------|
| Prompt  | `get`/`set`, plus a named word (`power`, `safety`, …) | the named words, from `SWITCH_COMMANDS` |
| Script  | `set:` / `assert:` / `wait_for:` | yes — the condition vocabulary is `VALUE_NAMES` |
| Control | `execute("set …")` | yes |
| Wire    | its command pair, if it has one | yes — from `WIRE_VALUES` |

**Adding a value is one row.** It becomes gettable, settable, assertable
and tab-completable everywhere at once. Give it a `WIRE_VALUES` row too
and it gains its wire command pair, its response payload and its
broadcast — the handlers are generated.

`simulation_only=True` marks the simulator's own knobs (flap timings,
battery rates). They are deliberately unreachable from the wire; a real
door has no such field, and `_apply_value` refuses them.

### Reading and writing

```python
read_value(state, "hold_time")  # every surface reads this way
set_named_value(sim, "hold_time", 12)  # …and writes this way
toggle_named_value(sim, "power")
```

`set_named_value` resolves the name, refuses an unknown or read-only one,
coerces the value, applies it, and announces it. **Side effects always
run**; only the announce is per-source, because a wire command answers in
its own response and must not also broadcast.

Do not read a value off the state object. A drift guard fails the build
if an interface module does — the point is that a value which grows
tracing, moves storage, or starts proxying real hardware changes in one
place.

### Actions and schedules

Same shape, without a registry:

- Door: `open_door`, `close_door`, `toggle_door` on `DoorSimulator`.
  `toggle_door` has no wire counterpart — the protocol has no toggle.
- Sensors: `_set_pet_present` in the engine is the one place presence
  begins. It owns mutual exclusion and reports whether the pet *arrived*,
  which is what raises a notification.
- Schedules: `add_schedule`, `remove_schedule`, `set_schedules`,
  `get_schedules`, `get_schedule`.

### Output

`send_to_clients(cmd, payload)` is the one place a message reaches a
client. `broadcast_value(name)` builds one from the wire table, so what a
client is told spontaneously and what it is told on request cannot
disagree.

Side effects do not live here. A broadcast that also re-evaluated the
sensors meant suppressing the broadcast silently skipped the door's
response to the change.

## Where to add things

| You want to… | Do this |
|--------------|---------|
| Add a door setting | One `VALUES` row. Add a `WIRE_VALUES` row if a real door carries it, and a `SWITCH_COMMANDS` row if it deserves its own prompt word |
| Add a wire command | A handler in `protocol.py` — but check first whether it is a value, in which case the pair is generated |
| Add a prompt command | A `@command` in `commands/`, unless it is a value |
| Add a script action | `_ACTION_PARAMS` plus a branch in `_execute_action`. It needs a prompt equivalent, or `TestSurfaceParity` fails |
| Change a wire spelling | You almost certainly should not — see below |

## The wire is fixed

The protocol is reverse-engineered and validated against real hardware. It is not ours to tidy. Where the door is
inconsistent — `inside` is the int `1` at the top level of a reply and
the string `"true"` inside `settings`; `holdTime` on the way in,
`holdOpenTime` on the way out — the simulator reproduces the
inconsistency, because a client that gets it wrong should fail here
rather than only against hardware.

`tests/test_wire_constants.py` pins every wire value by literal. Both
halves read the same symbol from `const.py`, so renaming a constant or
re-spelling its value changes what goes on the wire with the whole suite
green — that file is the check.

Translation between the core and the wire belongs in `wire_values.py`.
That is the difference between an interface layer doing its job and a
core that knows about TCP 3000.

## Guards

These run at every commit (`architecture` in `.pre-commit-config.yaml`):

| Guard | In | Catches |
|-------|-----|---------|
| Shared paths | `test_shared_paths.py` | An interface reading state directly, or writing without the registry |
| Wire table | `test_shared_paths.py` | A response and its broadcast disagreeing |
| Surface parity | `test_shared_paths.py` | A capability one surface can reach and another cannot |
| Wire constants | `test_wire_constants.py` | A wire value changing spelling |

A guard exempts things deliberately — session-only prompt words, script
language keywords, capabilities the two surfaces spell differently. Those
exemption lists are themselves checked: a stale entry hides a real gap.

## Working here

```bash
uv sync --all-extras
./scripts/setup-dev.sh          # installs the pre-commit and pre-push hooks

uv run pytest                    # parallel by default
uv run pytest --cov              # 100% line AND branch, or it fails
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src

uv run ppd-simulator              # interactive
uv run python -m powerpetdoor.simulator --script full_test_suite --oneshot
```

Coverage is not the bar — it cannot see a boundary that was never
exercised on both sides, or a compound condition whose second operand
never decided anything. The rules in `.claude/CLAUDE.md` under **Test
Quality Rules** are the bar.

When you fix something, look for its twins. Most defects in this
codebase have been one of several copies, and fixing the copy you found
leaves the others.
