# Frontend Developer Analysis — Round 7

Commit: `a0194bd` ("Round 6 fixes; revert the enabled wire change; layer the wire
boundary")

Scope: the simulator terminal front end (`cli.py`, `ctl.py`, `prompt_common.py`,
`commands/*`, `scripting.py` + the YAML script DSL) plus the library's public
API and prose docs as a "developer front end". No web UI exists.

Method: read the whole front end, then **live-tested both binaries**. Everything
below was produced by running the real entry points:

- three `ppd-simulator --daemon` daemons (ports 3900/3901, 3930/3931,
  3950/3951), one with `--scripts-dir /tmp/ppd7/scripts`, driven through ~120
  one-shot `ppd-simulator-ctl` invocations;
- `ppd-simulator-ctl -i` and `ppd-simulator` interactive sessions driven under a
  **real PTY** (so prompt_toolkit, history and the `history` command are on the
  live path), plus piped-stdin sessions for the plain-input fallback;
- ~25 headless `ppd-simulator --script ... --oneshot` runs (the CI front end),
  timed and exit-code-checked;
- raw TCP sockets against both the door port and the control port, and a real
  `PowerPetDoorClient` against a capture server, to read actual wire frames;
- in-process introspection of `SimulatorCompleter` under both path policies;
- the README, `docs/simulator.md` and `docs/door.md` code examples executed
  verbatim.

Baseline: `uv run pytest --ignore=tests/fuzz` → **2410 passed** on this commit,
so nothing below is a pre-existing test failure.

Repo files were not modified. All spawned daemons and clients were shut down.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 2 |
| Low | 6 |
| Trivial | 2 |

All twelve round-6 items verified fixed against running binaries (details in
[Round 6 Fix Verification](#round-6-fix-verification)). The new findings cluster
in two places round 6 did not touch: **numeric argument parsing on the operator
command path** (M1) and **the YAML script DSL and its documentation** (M2, L1–L4).

---

## Findings

### M1 (Medium) — a non-finite numeric argument is parsed as valid: `holdtime nan` reports `ERROR` *after* corrupting the simulator, wedging the door and breaking `GET_SETTINGS` for every client

**Where:**
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/base.py:171-181`
(`parse_arg`'s `"float"` branch — `float(value)` with only `<`/`>` bound checks,
both of which are `False` for `nan`), and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/settings.py:140-147`
(`holdtime` writes `state.hold_time` *before* the broadcast that then raises).
The raiser is
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/server.py:390-394`
(`int(self.state.hold_time * 100)`).

**Reproduction** (fresh daemon on 3950/3951, one real `PowerPetDoorClient`
connected):

```
$ ppd-simulator --daemon 3951 --port 3950 &
$ python holdclient.py &          # a real PowerPetDoorClient on 127.0.0.1:3950

=== 1. healthy daemon, client connected ===
$ ppd-simulator-ctl -p 3951 status | head -2
OK: Current State:
  Clients: 1 client
$ ppd-simulator-ctl -p 3951 holdtime
OK: Hold time: 2.0s
$ ppd-simulator-ctl -p 3951 broadcast settings
OK: Broadcast settings
$ ppd-simulator-ctl -p 3951 broadcast all
OK: Broadcast all data

=== 2. operator typos a non-finite hold time (the command REPORTS FAILURE) ===
$ ppd-simulator-ctl -p 3951 holdtime nan
ERROR: cannot convert float NaN to integer
rc=1

=== 3. every subsequent settings broadcast is now broken ===
$ ppd-simulator-ctl -p 3951 holdtime
OK: Hold time: nans
$ ppd-simulator-ctl -p 3951 broadcast settings
ERROR: cannot convert float NaN to integer
rc=1
$ ppd-simulator-ctl -p 3951 broadcast all
ERROR: cannot convert float NaN to integer
rc=1
```

The **device protocol itself** is broken for every connected client — raw
socket against the door port while `hold_time` is `nan`:

```
GET_SETTINGS   -> {"CMD": "GET_SETTINGS", "success": "false", "dir": "d2p", "reason": "Command failed", "msgID": 1}
GET_HOLD_TIME  -> {"CMD": "GET_HOLD_TIME", "success": "false", "dir": "d2p", "reason": "Command failed", "msgID": 2}
GET_DOOR_STATUS-> {"CMD": "GET_DOOR_STATUS", "success": "true", "dir": "d2p", "msgID": 3, "door_status": "DOOR_CLOSED"}
```

daemon log:

```
  File ".../simulator/protocol.py", line 680, in _handle_get_hold_time
    response[FIELD_HOLD_TIME] = int(self.state.hold_time * 100)
ValueError: cannot convert float NaN to integer
```

And the door wedges. On a second daemon (3930/3931), after the same failed
`holdtime nan`:

```
$ ppd-simulator-ctl -p 3931 inside
OK: Inside sensor activated for 0.5s
t=2s    Door: DOOR_HOLDING
t=4s    Door: DOOR_HOLDING
...
t=12s   Door: DOOR_HOLDING
$ ppd-simulator-ctl -p 3931 holdtime 2      # "fix" it
OK: Hold time set to 2.0s
after holdtime 2 restore, t=15s  Door: DOOR_HOLDING     <- still stuck
$ ppd-simulator-ctl -p 3931 close            # only an explicit close recovers
OK: Closing door
  Door: DOOR_CLOSED
```

**The other two front ends onto the same state both get this right.** Wire path,
same daemon:

```
C->D: {"config":"SET_HOLD_TIME","holdTime":NaN,"msgId":1,"dir":"p2d"}
D->C: {"CMD":"SET_HOLD_TIME","success":"false","reason":"holdTime must be a finite number, got nan","msgID":1}
C->D: {"config":"SET_HOLD_TIME","holdTime":Infinity,"msgId":2,"dir":"p2d"}
D->C: {"CMD":"SET_HOLD_TIME","success":"false","reason":"holdTime must be a finite number, got inf","msgID":2}
C->D: {"config":"GET_HOLD_TIME","msgId":3,"dir":"p2d"}
D->C: {"CMD":"GET_HOLD_TIME","success":"true","msgID":3,"holdTime":200}   <- state untouched
```

Script DSL path:

```
$ ppd-simulator --port 0 --script nanhold.yaml --oneshot     # set hold_time: .nan
[ERROR] Script error at step 1: hold_time must be a finite number, got nan
>>> Script FAILED: NaN hold_time
```

**Blast radius beyond `holdtime`.** Every `"float"` ArgSpec is affected. These
all return `rc=0`:

```
$ ppd-simulator-ctl -p 3901 inside nan
OK: Inside sensor activated for nans
$ ppd-simulator-ctl -p 3901 outside nan
OK: Outside sensor activated for nans
$ ppd-simulator-ctl -p 3901 inside Infinity
OK: Inside sensor activated for infs
$ ppd-simulator-ctl -p 3901 inside 1e400
OK: Inside sensor activated for infs
$ ppd-simulator-ctl -p 3901 charge_rate nan
OK: Charge rate: nan%/min
$ ppd-simulator-ctl -p 3901 discharge_rate inf
OK: Discharge rate: inf%/min
```

`discharge_rate nan` then silently disables the battery model — AC disconnected
for 4 s with a nan discharge rate leaves `Battery: 100%`. The same values on the
script path are all refused: `duration must be a finite number, got nan`.

**Description.** `parse_arg` accepts `nan`/`inf`/`Infinity`/`1e400` because
`nan < min` and `nan > max` are both `False` and `float("inf")` is a legal
parse. The bounds documented in the command's own help (`holdtime` says
"0.1-900") therefore do not hold. Three things then go wrong at once:

1. **A command that reports `ERROR` has already mutated state.** `holdtime`
   assigns before it broadcasts, so the failure is reported *after* the write.
   That breaks the persona's repeatability rule outright: an operator who sees
   `ERROR` and retries is retrying against a simulator that has already changed
   underneath them.
2. **The damage outlives the command.** `broadcast settings` / `broadcast all`
   fail forever after; `GET_SETTINGS` and `GET_HOLD_TIME` answer every client
   `success:false / reason:"Command failed"`. The simulator's entire job is to
   be a faithful device for library tests, and one mistyped operator command
   turns it into a device that fails a core query with a generic reason and no
   clue in the client-visible payload.
3. **The message explains nothing.** `cannot convert float NaN to integer` is a
   raw CPython exception string surfaced by `handler.py:360-363`; it names
   neither the command, nor the field, nor the rule. Compare the two paths that
   do it right: `holdTime must be a finite number, got nan`.

`status`/`holdtime` render the corruption as `Hold time: nans`, which is not a
value an operator can read.

**Recommendation.** Reject non-finite numerics in `parse_arg` for both `"float"`
and `"int"`, using the wording the other two paths already use — e.g. in the
`"float"` branch after `float(value)`:

```python
if not math.isfinite(parsed_float):
    return None, f"'{value}' is not a finite number"
```

That is one place and it fixes `holdtime`, `inside`, `outside`, `charge_rate`
and `discharge_rate` together, and makes the three front ends agree. Separately,
`holdtime` should validate-then-write (or broadcast-then-commit) so no command
can report `ERROR` after mutating state; and `handler.py`'s bare `str(e)` catch
would read far better as `f"{' '.join(cmd_path)}: {e}"` so an unexpected
exception at least names its command.

---

### M2 (Medium) — the script DSL validates every other name but silently accepts an unknown `sensor:`, and the resulting "sensor" bypasses the enable and safety-lock gates

**Where:**
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:395-397`
(`sensor = params.get("sensor", "inside")`, handed straight to
`simulator.trigger_sensor`) and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/engine.py:335-372`
(the gates are `if sensor == "inside"` / `elif sensor == "outside"`, so an
unknown name matches neither).

**Reproduction.** Every other misspelling class in the DSL fails loudly; the
sensor name does not. Seven one-line scripts, each run through
`ppd-simulator --port 0 --script X --oneshot`:

```
badaction    -> rc=1  [ERROR] Script error at step 1: Unknown action: frobnicate
badset       -> rc=1  [ERROR] Script error at step 1: Unknown setting: powr
badtoggle    -> rc=1  [ERROR] Script error at step 1: Unknown setting to toggle: powr
badassert    -> rc=1  [ERROR] Script error at step 1: Unknown assertion condition: door_stat
badwaitfor   -> rc=1  [ERROR] Script error at step 1: Unknown condition: door_clsed
badsensor    -> rc=0  >>> Script PASSED: Bad sensor
badparam     -> rc=0  >>> Script PASSED: Bad param key
```

`badsensor.yaml` is `{action: trigger_sensor, sensor: insde}`. The log presents
it as a legitimate accepted sensor:

```
[INFO]   Step 2: trigger_sensor(sensor=insde)
[INFO] Simulator: Insde sensor triggered, opening door
```

And it ignores the gates the real sensors obey:

```yaml
name: "Typo Sensor Bypass"
steps:
  - {action: set, name: inside,      value: "0"}
  - {action: set, name: outside,     value: "0"}
  - {action: set, name: safety_lock, value: "1"}
  - {action: trigger_sensor, sensor: insde}
  - {action: assert, condition: door_status, equals: DOOR_RISING}
  - {action: log, message: "DOOR OPENED WITH BOTH SENSORS DISABLED AND SAFETY LOCK ON"}
```

```
$ ppd-simulator --port 0 --script typo_sensor2.yaml --oneshot
[INFO]   Step 4: trigger_sensor(sensor=insde)
[INFO] Simulator: Insde sensor triggered, opening door
[INFO]   Step 5: assert(condition=door_status, equals=DOOR_RISING)
[INFO]   [SCRIPT] DOOR OPENED WITH BOTH SENSORS DISABLED AND SAFETY LOCK ON
>>> Script PASSED: Typo Sensor Bypass
```

**Description.** `sensor:` is the only user-supplied *name* in the DSL that is
not validated, and it fails in the worst direction: instead of erroring it
synthesises a third sensor that is not subject to `state.inside`,
`state.outside`, `state.safety_lock` or `is_sensor_allowed_by_schedule()`. The
built-in `safety_lock_test.yaml` and `power_lockout_test.yaml` exist precisely
to prove those gates hold; a one-character typo in a derived script produces a
run that exercises none of them and still reports PASSED. Over ctl (`run <name>
wait`) that PASSED becomes a green CI exit code.

The log line makes it worse rather than better: `Simulator: Insde sensor
triggered, opening door` capitalises the typo and reads exactly like the
legitimate `Simulator: Inside sensor triggered, opening door`, so nothing in
the output invites suspicion.

**Recommendation.** Validate in `_execute_step` alongside the existing
`Unknown action` / `Unknown setting` checks:

```python
sensor = params.get("sensor", "inside")
if sensor not in ("inside", "outside"):
    raise ScriptError(f"Unknown sensor: {sensor}. Use: inside, outside")
```

`DoorMotionEngine.trigger_sensor` should also refuse an unrecognised name rather
than falling through the gates, since it is reachable from
`server.trigger_sensor()` in the documented programmatic API
(`docs/simulator.md:453`).

---

### L1 (Low) — `docs/simulator.md` says the Conditions table applies to `assert`; all 19 of its rows fail with `assert`

**Where:** `/home/prez/src/pypowerpetdoor/docs/simulator.md:744-763` ("### Conditions",
"Conditions are used with `wait_for` and `assert` actions:") versus
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:724+`
(`_assert_condition`, which knows a completely different set of names).

**Reproduction.** One script per documented condition, each
`{action: assert, condition: <row>, equals: "true"}`, run through
`ppd-simulator --port 0 --script ac.yaml --oneshot`:

```
door_closed          Unknown assertion condition: door_closed
door_open            Unknown assertion condition: door_open
door_rising          Unknown assertion condition: door_rising
door_holding         Unknown assertion condition: door_holding
door_keepup          Unknown assertion condition: door_keepup
power_on             Unknown assertion condition: power_on
power_off            Unknown assertion condition: power_off
auto_on              Unknown assertion condition: auto_on
auto_off             Unknown assertion condition: auto_off
inside_enabled       Unknown assertion condition: inside_enabled
inside_disabled      Unknown assertion condition: inside_disabled
outside_enabled      Unknown assertion condition: outside_enabled
outside_disabled     Unknown assertion condition: outside_disabled
autoretract_on       Unknown assertion condition: autoretract_on
autoretract_off      Unknown assertion condition: autoretract_off
safety_lock_on       Unknown assertion condition: safety_lock_on
safety_lock_off      Unknown assertion condition: safety_lock_off
cmd_lockout_on       Unknown assertion condition: cmd_lockout_on
cmd_lockout_off      Unknown assertion condition: cmd_lockout_off
```

19 of 19 fail. Full failure of the first one:

```
$ ppd-simulator --port 0 --script assertcond.yaml --oneshot
[ERROR] Script error at step 1: Unknown assertion condition: door_closed
>>> Script FAILED: assert with a documented condition
>>> All scripts FAILED
```

**Description.** The sentence introducing the table promises both actions; only
`wait_for` accepts any of it. The `assert`-accepting names are in the *second*
table ("For `assert`, you can also check these values") — the word "also" says
that table extends the first, when in fact it replaces it. A script author
following the docs writes `assert door_closed` (the single most natural
assertion in a door simulator) and gets a hard step failure with no pointer to
the right table.

Verified the second table is itself accurate: `_assert_condition` accepts
exactly `auto, autoretract, battery, cmd_lockout, door_status, hold_time,
inside, outside, power, safety_lock, total_auto_retracts, total_open_cycles` —
the twelve rows listed, no more, no fewer.

**Recommendation.** Change line 746 to "Conditions are used with the `wait_for`
action:" and retitle the second table "Conditions for `assert`" (dropping
"also"). Optionally teach `_assert_condition` to fall back to the boolean
conditions, which would make the docs true as written and is arguably what a
reader expects.

---

### L2 (Low) — the first script example in the docs fails when you run it, and its programmatic twin does too

**Where:** `/home/prez/src/pypowerpetdoor/docs/simulator.md:602-614` ("### Script
Format — Scripts are YAML files with the following structure") and
`/home/prez/src/pypowerpetdoor/docs/simulator.md:541-546` (the
`Script.from_simple_commands` example).

**Reproduction.** The YAML block copied verbatim into `docfmt.yaml`:

```
$ ppd-simulator --port 0 --script /tmp/ppd7/docfmt.yaml --oneshot
[ERROR] Assertion failed at step 3: door_status: expected 'DOOR_CLOSED', got 'DOOR_HOLDING'
>>> Script FAILED: My Test Script
>>> All scripts FAILED
exit code = 1
```

Repeated three times, identical every run. The programmatic example, on a
freshly started `DoorSimulator` with nothing else touched:

```
$ python docex2.py
A) builtin basic_cycle on a fresh simulator: True
Assertion failed at step 3: door_status: expected 'DOOR_CLOSED', got 'DOOR_HOLDING'
B) doc's from_simple_commands example on a fresh simulator: False
   door now: DOOR_HOLDING  hold_time: 2.0
```

Also three-for-three deterministic. (`A)` is the control: a real built-in script
passes on the same fresh simulator, so the failure is the example, not the
environment.)

**Description.** Both examples are `trigger sensor` → `wait 2` → `assert
door_status DOOR_CLOSED`. The default `hold_time` is 2.0 s and the hold timer
only starts once the door *reaches* `DOOR_HOLDING`, so two seconds is never
enough — the door is still `DOOR_HOLDING` when the assertion runs. These are the
first two things a script author copies, and they exit 1. The built-in scripts
show the correct shape (`basic_cycle.yaml` uses `wait_for: door_holding` then
`wait_for: door_closed`), so the fix already exists in the repo.

**Recommendation.** Replace the `wait`/`assert` pair in both examples with the
pattern the built-ins use:

```yaml
  - action: wait_for
    condition: door_closed
    timeout: 10
```

and `"wait_for door_closed 10"` in the `from_simple_commands` list. Consider a
docs test that runs every fenced YAML script block in `simulator.md` and asserts
it passes — the machinery (`tests/test_docs_accuracy.py`, `_json_blocks`)
already exists for JSON.

---

### L3 (Low) — an unknown step parameter is silently ignored, and the step log echoes it back as if it had been accepted

**Where:**
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:176-181`
(`params=step_data` — whatever keys the YAML had) and the `params.get(...)`
lookups throughout `_execute_step`
(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:388-500`).

**Reproduction.** `duration` is a real parameter name in this DSL (for the
`inside`/`outside` actions); `wait` wants `seconds`. Two otherwise identical
scripts:

```
### typo_wait (duration: 8) ###
elapsed: 1.209684321
[INFO]   Step 1: wait(duration=8)
[INFO]   Step 2: log(message=done)
>>> Script PASSED: Typo Wait

### good_wait (seconds: 8) ###
elapsed: 8.191055293
[INFO]   Step 1: wait(seconds=8)
[INFO]   Step 2: log(message=done)
>>> Script PASSED: Good Wait
```

The typo'd script waited **1.0 s** instead of 8, passed, and exited 0.

**Description.** No step parameter is validated. That alone would be a Feedback
gap; what makes it a trap is that the progress log renders `step.params`
verbatim — `Step 1: wait(duration=8)` — so the one piece of output the author
inspects actively confirms the parameter was accepted. The observable effect is
a timing change (here 8× shorter), which in a script written to wait out a door
cycle silently converts a real test into a no-op that still reports PASSED.

I found this by accident: my own first pass at building long-running test
scripts used `duration:` and the queue drained in a second instead of eight.

**Recommendation.** Give each action a known-parameter set and raise
`ScriptError(f"Unknown parameter for {action}: {key}")` on anything else, the
way `Unknown action` / `Unknown setting` / `Unknown assertion condition` already
work. A cheaper interim step is to log the parameters the step *used* rather
than the ones the file supplied, so `wait(duration=8)` becomes
`wait(seconds=1.0)` and the discrepancy is visible.

---

### L4 (Low) — the `inside` / `outside` script actions and the `door_closing` condition are implemented, referenced in passing, and documented nowhere

**Where:** implemented at
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:411-423`
(`inside`, `outside`) and in `_wait_for_condition`
(`door_closing`). `docs/simulator.md:615-741` ("Available Actions") lists
`trigger_sensor`/`trigger`, `open`, `close`, `obstruction`,
`pet_presence`/`pet_on`, `pet_off`, `battery`, `wait`, `wait_for`, `set`,
`toggle`, `assert`, `log`, `add_schedule`, `remove_schedule` — and neither
`inside` nor `outside`. `docs/simulator.md:744-763` ("Conditions") omits
`door_closing`.

**Reproduction.** Implemented-vs-documented diff, taken from the source and the
markdown:

```
$ grep -oP 'action == "\K[a-z_]+' src/powerpetdoor/simulator/scripting.py | sort -u
add_schedule assert battery close inside log obstruction open outside
pet_off pet_on pet_presence remove_schedule set toggle trigger trigger_sensor wait wait_for

$ grep -oP '^\*\*\K[a-z_]+(?=\*\*)' docs/simulator.md | sort -u
add_schedule assert battery close log obstruction open pet_off pet_presence
remove_schedule set stderr toggle trigger_sensor wait wait_for
```

Both undocumented features work:

```
$ ppd-simulator --port 0 --script insideact.yaml --oneshot   # action: inside, duration: 1.5
[INFO]   Step 1: inside(duration=1.5)
[INFO]   Step 2: wait_for(condition=door_holding, timeout=5)
[INFO]   Step 3: log(message=inside action worked)
>>> Script PASSED: inside action

$ ppd-simulator --port 0 --script waitclosing.yaml --oneshot  # wait_for: door_closing
[INFO]   Step 3: log(message=door_closing condition works)
>>> Script PASSED: wait_for door_closing (undocumented)
```

**Description.** `docs/simulator.md:683-684` — the "Numeric bounds" note added in
an earlier round — says *"Every delay a script can ask for — `inside`/`outside`
`duration`, `wait` `seconds` and `wait_for` `timeout`"*, so the docs already
depend on actions they never introduce. A reader who finds that sentence has no
entry to look up, and the documented `trigger_sensor` has no duration parameter
at all, so the only way to hold a sensor active for a chosen time in a script is
the undocumented action. `door_closing` is likewise the only condition that can
observe the closing phase, which is what an auto-retract test needs.

**Recommendation.** Add `**inside** / **outside**` under "Door Operations" with
the `duration` parameter (`0` = indefinite, matching the interactive commands
already documented at `docs/simulator.md:166-167`), and a `door_closing` row to
the Conditions table.

---

### L5 (Low) — `--list-scripts` and the `list` command print the same list, but only one of them marks a shadowed built-in

**Where:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/cli.py:1034-1053`
(`--list-scripts`, two plain loops) versus
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/scripts.py:289-295`
(`list_scripts`, which computes the marker).

**Reproduction.** Same daemon, same `--scripts-dir`, with a
`basic_cycle.yaml` in it that shadows the built-in:

```
$ ppd-simulator-ctl -p 3901 list
OK: Built-in scripts:
  basic_cycle: Pet triggers inside sensor, door opens, holds, then closes (shadowed by /tmp/ppd7/scripts/basic_cycle)
  full_test_suite: ...
Scripts from /tmp/ppd7/scripts:
  basic_cycle: This shadows the built-in
  ...

$ ppd-simulator --scripts-dir /tmp/ppd7/scripts --list-scripts
Built-in scripts:
  basic_cycle: Pet triggers inside sensor, door opens, holds, then closes      <- no marker
  full_test_suite: ...
Scripts from /tmp/ppd7/scripts:
  basic_cycle: This shadows the built-in
  ...
```

That the precedence is real, confirmed over ctl:

```
$ ppd-simulator-ctl -p 3901 run basic_cycle wait
OK: Script PASSED: SHADOWING basic_cycle
```

**Description.** `cli.py:1037-1039` carries the comment *"Same header the `list`
command prints (T1): two spellings of the same list in the two places a user
looks for it is a needless inconsistency"* — so the two surfaces are explicitly
intended to agree, and the round-6 L3 fix landed in only one of them.
`--list-scripts` is the pre-flight "what can I run?" command (it does not need a
daemon), so it is the surface most likely to be consulted first, and it shows
the name twice with two contradicting descriptions and no indication which one
`run` picks. `docs/simulator.md:322-323` documents the marker as a `list`-only
behaviour, so the docs are accurate — the inconsistency is in the product.

Two smaller things in the same string: the marker renders
`(shadowed by /tmp/ppd7/scripts/basic_cycle)` with no `.yaml` suffix, so it
looks like a path but `ls` on it fails; and the shadowing file could equally be
`.yml`, which the marker cannot express.

**Recommendation.** Have `--list-scripts` reuse `ScriptsCommandsMixin`'s
rendering (or at minimum apply the same marker), and print the real path from
`get_extra_script_files()[name]` instead of reconstructing `<dir>/<name>`.

---

### L6 (Low) — the symlink refusal advises "run it by path", which is exactly what the control channel refuses

**Where:**
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/scripts.py:256-259`
(the message) versus
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/scripts.py:227-233`
(`_load_script_restricted`, active whenever `allow_script_paths` is False — i.e.
on every daemon, which is the only place ctl can talk to).

**Reproduction.** `scripts/linked.yaml` is a symlink to
`/tmp/ppd7/out/outside.yaml`:

```
$ ppd-simulator-ctl -p 3901 run linked
ERROR: Script 'linked' resolves outside /tmp/ppd7/scripts and cannot be run by name; move it into the directory or run it by path

$ ppd-simulator-ctl -p 3901 run /tmp/ppd7/scripts/linked.yaml
ERROR: Script paths are not allowed over the control channel; use a bare script name (see 'list')
rc=1
$ ppd-simulator-ctl -p 3901 run ./linked.yaml
ERROR: Script paths are not allowed over the control channel; use a bare script name (see 'list')
rc=1
```

The same advice *is* actionable on the interactive CLI (PTY session against
`ppd-simulator --port 3940 --scripts-dir /tmp/ppd7/scripts`):

```
0.0.0.0:3940> run linked
>>> Script 'linked' resolves outside /tmp/ppd7/scripts and cannot be run by name; move it into the directory or run it by path
0.0.0.0:3940> run /tmp/ppd7/out/outside.yaml wait
>>> Script PASSED: Outside Script
```

**Description.** The round-6 L1 fix is otherwise excellent — the file is now
absent from `list`, `--list-scripts`, the `Available:` hint and tab completion,
and the error explains itself instead of contradicting itself. The remaining gap
is the last clause: over ctl the operator is sent to a form the very next line
of code rejects, and the two refusals then point at each other. `ArgSpec` already
has the machinery to make a string depend on the path policy
(`describe_script_argument` at `scripting.py:802-812`, added in round 5 for
exactly this reason).

**Recommendation.** Make the tail policy-aware, the same way
`describe_script_argument()` is — `... move it into the directory` over the
control channel, `... move it into the directory or run it by path` locally.

---

### T1 (Trivial) — `docs/protocol.md`'s Keepalive section claims `msgId` is echoed as `msgID`; the project's own device implementation does not, and the doc-accuracy test excludes that field to accommodate it

**Where:** `/home/prez/src/pypowerpetdoor/docs/protocol.md:175` — *"`msgId` should
be echoed back as `msgID` like any other response."* Added by the round-6 M1
fix. The simulator returns before the `msgID` copy
(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/protocol.py:520-528`,
`return` at 529, `msgID` assigned at 538).

**Reproduction.** Raw socket to the simulator's door port:

```
C->D: {"PING": "1710000000123", "msgId": 7, "dir": "p2d"}
D->C: {"CMD": "PONG", "PONG": "1710000000123", "success": "true", "dir": "d2p"}
has msgID?  False
```

`tests/test_docs_accuracy.py:236-241` already knows:

```python
# The simulator omits `msgID` on PONG; every other documented field
# must match exactly.
assert {k: v for k, v in written[0].items() if k != "dir"} == { ... }
```

**Description.** The section's stated audience is "anyone writing a second
implementation", and this is the one sentence in it that the repo contradicts. A
reader who correlates PONGs by `msgID` gets nothing back from the only device
implementation available to them. The client does not use `msgID` for PONG at
all — it matches on the echoed token (`client.py:1074`) — so the sentence is not
describing a requirement either. Whether real firmware echoes it is unknown, and
per the repo's constraint the code must not change to match a reverse-engineered
doc; so the doc is the side to soften.

**Recommendation.** Reword to state the observed position and its uncertainty,
e.g. *"`msgId` is not required for correlation — the echoed token is the whole
mechanism — and the simulator does not echo it on `PONG`. Whether the firmware
does is unverified."*

---

### T2 (Trivial) — three spellings of the same schedule sensor scope in one command family

**Where:**
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/schedules.py:152`
(`add`'s echo, uses the raw `sensor` choice), `:47-54` (`_format_schedule`,
joins with `+`), `:70-74` (the implicit-schedule line).

**Reproduction.**

```
$ ppd-simulator-ctl -p 3901 schedule add both 6:00-22:00 all
OK: Added schedule #0: both sensor, all days, 06:00-22:00
$ ppd-simulator-ctl -p 3901 schedule list
OK: Schedules (auto mode ON):
  #0: inside+outside sensor, all days, 06:00-22:00 (enabled)
$ ppd-simulator-ctl -p 3901 schedule clear && ppd-simulator-ctl -p 3901 schedule list
OK: Schedules (auto mode ON):
  (implicit): both sensors, all days, 00:00-23:59
```

`both sensor` / `inside+outside sensor` / `both sensors` — three renderings of
one concept across two commands, and `both sensor` is not grammatical.

**Recommendation.** Render the scope through one helper (the `add` echo should
call `_format_schedule`'s sensor logic, or vice versa) and use the plural
consistently.

---

## Round 6 Fix Verification

All twelve round-6 items verified against running binaries. Nothing regressed.

| Item | Status | Evidence |
|------|--------|----------|
| **M1** — keepalive documented as token+echo (docs fixed, code untouched) | ✅ Verified end-to-end | Captured the real client frames against a capture server: `{"PING": "1787397986509", "msgId": 1, "dir": "p2d"}` / `{"CMD": "PONG", "PONG": "1787397986509", "success": "true", "dir": "d2p"}` — byte-shape identical to `protocol.md:159/164`. `MAX_FAILED_PINGS = 3` and the string `Last PING not responded to %d times.` both present in `client.py`; `PowerPetDoor`'s `keepalive` default is `30.0` (`door.py:367`), matching "Typical interval: 30 seconds". `PING` is special-cased to `PRIORITY_CRITICAL` at `client.py:1861-1864`, so the `PRIORITY_CRITICAL` row is right even though only `PONG` is in `COMMAND_PRIORITIES`. One caveat: the new `msgId`/`msgID` sentence — see **T1**. |
| **M1 code untouched** | ✅ Verified | The simulator still echoes `PONG: msg[PING]` and the client still matches on the token. No wire change was made. |
| **L7** — `DOOR_STATUS` moved to its own "Unsolicited Door Status" section | ✅ Verified | `protocol.md:107-114` — the Message Types table now holds exactly `cmd`, `config`, `PING`, `PONG`, with an explicit "not an envelope key" note; `protocol.md:562-575` carries the real frame. Pinned by `test_message_types_table_lists_only_real_envelope_keys` (passes). |
| **L4** — client.md index links retargeted | ✅ Verified | `client.md:355` now → `#unsolicited-door-status`, `:356` → `#notification-messages-door-to-client`, `:357` → `#query-commands` with the text "(the `GET_HW_INFO` `fwInfo` object)". A full crawl of every relative markdown link in `docs/*.md` + `README.md` + `CHANGELOG.md`, resolving anchors with GitHub's slug rules, reports **0 broken links and 0 missing anchors**. |
| **L5** — PRIORITY table corrected | ✅ Verified by execution | Dumped `COMMAND_PRIORITIES`: MEDIUM is exactly `DISABLE_*`/`ENABLE_*`/`POWER_*` plus `SET_HOLD_TIME`, `SET_NOTIFICATIONS`, `SET_SENSOR_TRIGGER_VOLTAGE`, `SET_SLEEP_SENSOR_TRIGGER_VOLTAGE`, `SET_TIMEZONE`; LOW contains `SET_SCHEDULE`, `SET_SCHEDULE_LIST`, `DELETE_SCHEDULE` and every `GET_*`. Matches `client.md:571-572` exactly. `COMMAND_PRIORITIES.get("NOT_A_COMMAND", PRIORITY_LOW)` → `3`, matching the new default-LOW paragraph. |
| **L6(a)** — listener table shows `bool \| None` | ✅ Verified | `client.md:474-475` now reads `{field: (field: str, val: bool \| None)}` with the explanatory paragraph at `:483-486`. |
| **L6(b)** — centiseconds docstring | ✅ Verified by execution | `PowerPetDoorClient.add_listener.__doc__` contains `hold_time_update: Called with hold time in **centiseconds**,` — the word "seconds" no longer appears standalone. Agrees with `client.md:480`. |
| **L1** — symlink containment via `script_escapes_directory()` | ✅ Verified, all four surfaces | With `scripts/linked.yaml → /tmp/ppd7/out/outside.yaml`: `ctl list` omits it; `ppd-simulator --scripts-dir ... --list-scripts` omits it; `run nosuchscript` → `Available: basic_cycle, failing, full_test_suite, long1, long2, obstruction_test, pet_presence_test, power_lockout_test, quick, safety_lock_test, schedule_test` (no `linked`); the in-process completer returns `[]` for `run link`. `run linked` now answers `Script 'linked' resolves outside /tmp/ppd7/scripts and cannot be run by name; ...` — self-explaining, no contradiction. The one remaining rough edge is the closing advice over ctl (**L6** above). |
| **L2** — plain `stop` names the queue depth | ✅ Verified | Queued three 30-second scripts: `list` → `Script: running "Long Script long1" (2 queued)` / `Queued: Long Script long2, Quick Script`; `stop` → `OK: Stopping script: Long Script long1 (2 still queued; use 'stop all' to discard them)`; second `stop` → `... Long Script long2 (1 still queued; ...)`; third → `ERROR: No script is running (use 'shutdown' to stop the simulator)`. `stop all` on the same setup → `Stopping script: Long Script long1 (dropped 2 queued)` and `Script: none running`; idempotent repeat → `OK: Nothing running or queued`, rc=0. |
| **L3** — shadowed built-ins marked in `list`, deduped in completion | ⚠️ **Verified for `list` and completion; `--list-scripts` was not fixed** | `ctl list` → `basic_cycle: Pet triggers... (shadowed by /tmp/ppd7/scripts/basic_cycle)`; the completer offers `basic_cycle` exactly once, with the scripts-dir description. `--list-scripts` still prints both without a marker — see **L5**. |
| **T1** — `history()` delegates to one message set | ✅ Verified under a PTY | `InfoCommandsMixin.history` is a 3-line delegate to `History(backend=...).execute_command()` and `HISTORY_UNAVAILABLE_MESSAGE` is a single module constant. Live PTY `ctl -i` session: `history` → `History (4 of 4 commands):` (the failed `bogus` correctly absent), `!!` → `>>> !! -> history` with the entry rewritten to `history` on the next listing, `history 2` → `History (2 of 6 commands):`, `history clear` → `History cleared`, `history abc` → `Invalid argument: abc. Use 'clear' or a number.`, `history 0` → `Number must be positive`. |
| **T2** — `_NOTIFY_DEFS` drives decorators/setter/display | ✅ Verified | `notifications.py:20-32` derives `_NOTIFY_ATTR`, `_NOTIFY_DESC`, `_NOTIFY_ALIASES` and `_NOTIFY_LABEL_WIDTH` from the table. Live: `notify help` lists all five with `low_battery (low_bat, lowbat)`; `notify low_bat off` and `notify lowbat on` both resolve to `low_battery`; `notify` renders the derived-width block; `notify bogus` → `Unknown notify subcommand: bogus / Available: inside_off, inside_on, low_battery, outside_off, outside_on`. |
| **New: schedule wire-boundary table** (`protocol.md:610-634`) | ✅ Verified by execution | `SCHEDULE_WIRE_TO_DEVICE.enabled` is `wire_json_bool`, `SCHEDULE_WIRE_FROM_DEVICE.enabled` is `wire_flag_string` — the two named constants the doc points at both exist. `door.Schedule.to_dict()` → `"enabled": true` (bool); the simulator's `Schedule.to_dict()` → `"enabled": "1"` (str); every other field identical in both (`index` int, `daysOfWeek` `[0,1,1,1,1,1,0]`, `inside`/`outside` JSON bools, `{hour,min}` ints). Reader liberality confirmed: `door.Schedule.from_dict` maps `"1"`/`1`/`True` → `True` and `"0"`/`0`/`False` → `False`. |
| **New: `FrameDispatcher` / throttle quiet period** | ✅ No front-end regression observed | A real `PowerPetDoorClient` connected to the simulator throughout: README's two quick-starts, `docs/simulator.md`'s "Integration with Client Testing", and a keepalive capture run all behaved normally. `2410 passed` on the deterministic suite. |

---

## Areas Reviewed With No Findings

**Docs link integrity.** Crawled every relative markdown link in `docs/*.md`,
`README.md` and `CHANGELOG.md` (GitHub slug rules for anchors): **0 broken
files, 0 missing anchors**. Earlier hits on
`#command-lockout--pet-proximity-keep-open` and `#battery--hardware` were
artifacts of my first slugger collapsing runs of whitespace; with GitHub's
one-hyphen-per-space rule both resolve.

**Documented API surface.** Every `door.<name>` and `client.<name>` referenced
anywhere in the docs exists on the real classes (only false positives:
`docs/door.md`, `docs/client.md`, `door.py`, `client.py`, `door.Schedule` — file
and module references). Every `from powerpetdoor... import ...` in the docs
resolves, including `from powerpetdoor.simulator import DoorSimulator, Script,
ScriptRunner, get_builtin_script` and the 40-name constant block at
`client.md:245`.

**README quick-starts.** Both executed verbatim (host/port swapped for an
ephemeral simulator): `Door status: CLOSED`, `Battery: 100%`, `set_hold_time(15)`
→ `holdOpenTime: 1500` centiseconds on the wire, `on_status_change` registered,
`await client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)` returned the
full settings dict. Both printed "example OK".

**`docs/simulator.md` programmatic examples.** "Basic Usage", "Triggering
Events" (all 13 calls), "Modifying State" (all 8 assignments), "Managing
Schedules" (`Schedule(index=..., start_hour=7, end_hour=18)` — the simulator's
`Schedule`, which does take those kwargs), "Running Scripts Programmatically"
(`get_builtin_script("basic_cycle")` → `True` on a fresh simulator) and
"Integration with Client Testing" all executed successfully. Only the
`from_simple_commands` snippet fails — reported as **L2**.

**`docs/door.md` Schedule example** uses `Schedule(..., start=ScheduleTime(hour=6,
minute=0), ...)` — matching `inspect.signature(door.Schedule.__init__)` exactly.

**Command help, aliases and coverage.** `help` compared across all three
surfaces: daemon-over-ctl (no `clear`/`exit`/`history`), `ctl -i` piped (adds
`clear`, `exit (q, quit)`), `ctl -i` under a PTY (additionally `history (hist)
[clear|N]`, correctly gated on `use_prompt_toolkit()`), and `ppd-simulator`
interactive (shows `shutdown (exit, q, quit)` and hides the separate `exit`).
Every gating rule in `get_help()` behaved as written.

**Argument help and error paths.** `help` on 6 commands, and ~20 bad-input
cases: `battery 150` → `'150' is above maximum (100)` + usage; `battery -5`,
`battery abc`, `holdtime 0.05`, `holdtime 1000`, `holdtime abc`, `charge_rate
-1`, `discharge_rate abc`, `outside -3`, `notify inside_on maybe` →
`'maybe' is not valid. Use on/off`, `bogus_command` → `Unknown command:
bogus_command. Type 'help' for commands.`, `schedule add` with a wrong arg order
→ `Unexpected argument(s): weekdays` + usage, `schedule time 1 ...` on a missing
index → `Schedule #1 not found`. All correct, all with usage lines. (Non-finite
input is the exception — **M1**.)

**Toggle-vs-show consistency.** Ran all ten `[on|off]` commands twice each:
`autoretract`, `lockout`, `safety`, `power`, `auto`, `inside_enable`,
`outside_enable`, `battery_present`, `pet` all toggle; `debug` shows. That one
difference is spelled out in `debug`'s own arg help ("omit to show current
state"), so it is documented rather than surprising.

**Schedules.** Full lifecycle over ctl: `add` (with and without days), `list`,
`time`, `days`, `disable`/`enable`, `delete`, `clear`, plus `clear` on an empty
set (`No schedules to clear`) and the implicit-schedule line. All correct except
the wording nit in **T2**.

**Tab completion.** In-process against `SimulatorCompleter` under both policies.
CLI policy: `run ` offers built-ins + scripts-dir with real descriptions,
`basic_cycle` once, `linked` never; `stop ` → `[('all', "'all' to discard every
queued run as well"), ('help', ...)]`; `history ` → the ArgSpec description, not
the value echoed at itself; `schedule `, `notify `, `ac ` all list unique names
plus `Alias for X` metas. ctl policy: `run ./` and any path-shaped prefix → `[]`;
a real ctl-configured process offers built-ins only, matching
`docs/simulator.md:325-328`. Timezone completion with the cache initialised:
`timezone ` → 599 zones, `timezone Amer` → 169, `timezone America/New` → exactly
`America/New_York`, `tz EST` → `EST`, `EST5EDT`.

**Exit codes.** `ctl` no args → 1 (after printing the epilog with its six
examples and the plain-`run` caveat); bad flag → 2; unknown script → 1; plain
`run failing` → 0 (documented); `run failing wait` → 1; `run quick wait` → 0;
`ppd-simulator --script <failing> --oneshot` → 1. All match
`docs/simulator.md:344-380`.

**Wait-run log streaming.** `ctl run basic_cycle wait 1>out 2>err`: stdout held
exactly `OK: Script PASSED: SHADOWING basic_cycle`; stderr held the five daemon
`LOG:` lines. Scriptable exactly as `docs/simulator.md:369-375` promises.

**Connection error paths.** One-shot against a dead port → `Connection refused
to 127.0.0.1:39999`, rc=1; `-i` against the same → `Error: Connection refused -
simulator not running on 127.0.0.1:39999`, rc=1. Both correct; the phrasings
differ but the interactive one is strictly more informative, so I am not
reporting it.

**Broadcast gating.** All eight `broadcast` subcommands with a real client
attached returned accurate confirmations (`Broadcast hwinfo: fw 1.2.3, hw 1 rev
1`, `Broadcast stats: 1 cycles, 0 retracts`, ...); with no client, `broadcast
all` → `ERROR: No clients connected`, rc=1.

**Terminal-safety plumbing.** `render_result`/`sanitize_text` on every result
path, `escape_message`/`unescape_message` on the control protocol, the
`_SanitizingFormatter` on all logging handlers, and `clear`'s TTY guard all
still hold; no raw f-string terminal writes remain on the live echo paths
(`cli.py:819`, `ctl.py:618` both route through `session.format_recall`).

**Script queue mechanics** (round-5 M1 / round-6 M1 territory): queue depth and
names accurate while running, `stop all` drop count always equal to the depth
`list` printed a moment earlier, one `stop all` always sufficient, and a vetoed
run executes zero steps and emits no `Script FAILED:` line.

---

## Notes on scope

`docs/protocol.md` is treated as reverse-engineered and non-authoritative
throughout. **No finding here proposes changing the device wire protocol.** M1's
recommendation is confined to the operator command parser, which is not on the
wire; M2's is confined to the script DSL and the motion engine's internal
sensor dispatch. T1 recommends softening a doc sentence, not changing the code.
`door.Schedule.to_dict()` and the simulator's `to_dict()` were checked as
opposite directions of the boundary, exactly as `protocol.md:610-634` frames
them, and both were found to match the documented table.
