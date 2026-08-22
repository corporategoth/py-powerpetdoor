# Frontend Developer Analysis — Round 8

Commit: `da31ae2` ("Round 7 fixes (refuter-approved list only)")

Scope: the simulator terminal front end (`cli.py`, `ctl.py`, `prompt_common.py`,
`commands/*`, `scripting.py` + the YAML script DSL) plus the library's public
API and prose docs as a "developer front end". No web UI exists.

Method — everything below was produced by running the real entry points:

- `ppd-simulator --daemon` daemons on 3801/3800 and 3821/3820 (one with
  `--scripts-dir`), driven through ~90 one-shot `ppd-simulator-ctl` invocations;
- `ppd-simulator` interactive sessions under a **real PTY** (prompt_toolkit,
  lexer, completion menu on the live path) and under piped stdin;
- ~40 headless `ppd-simulator --script ... --oneshot` runs (the CI front end),
  exit-code checked;
- raw TCP sockets against the door port (keepalive/PONG capture, and a
  round-trip latency probe used as an event-loop-block detector);
- a real `PowerPetDoor` and `PowerPetDoorClient` running the README quick-starts
  verbatim against a live simulator;
- in-process measurement of `script_completer` / `render_script_listing`;
- every fenced YAML block in `docs/simulator.md` parsed and, where it is a whole
  script, executed;
- a `git archive HEAD~1` copy at `/tmp/ppd8/prev` (read-only extraction) used as
  the "before" side of one upgrade-behaviour comparison, with an import guard
  printing the resolved `powerpetdoor.__file__`.

Baseline on this commit: `uv run pytest --ignore=tests/fuzz` →
**2576 passed in 36.10 s**; `pytest tests/test_docs_accuracy.py` → **24 passed**.
Nothing below is a pre-existing test failure.

Repo files were not modified (`git status` clean apart from another persona's
own round-8 file). Every daemon and PTY child I started was terminated; scratch
lives under `/tmp/ppd8`.

**Measurement caveat, stated up front:** two other round-8 personas were running
mutation batches on this 16-core machine throughout, at load average 7–11. Every
timing finding below therefore carries a *control measured in the same process
run*, and the headline numbers were reproduced across two separate runs minutes
apart. Absolute milliseconds will differ on an idle machine; the ratios did not.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 4 |
| Trivial | 0 |

All eight round-7 frontend items (M1, M2, L1–L6, T1, T2) verified fixed against
running binaries, plus the two cross-persona items with front-end surface
(`CONTROL_PORT_OFFSET`, the docs-accuracy additions) — details in
[Round 7 Fix Verification](#round-7-fix-verification). Nothing regressed.

The new findings cluster on the surfaces round 7 touched: the shared script
listing/completion path (M1), the prose that quotes the two strings round 7
changed (L1), the docs table one section below the one round 7 corrected (L2),
the changelog that round 6 updated and round 7 did not (L3), and the "here are
the valid values" wording that round 7 added two new spellings to (L4).

---

## Findings

### M1 (Medium) — every script listing and every Tab completion re-parses every YAML file in `--scripts-dir`, on the simulator's own event loop: one keystroke stalls the emulated door for over half a second, and the prefix you already typed is not used to narrow the work

**Where:**
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:1108-1129`
(`script_completer`'s name branch — `Script.from_file(path)` for *every* built-in
and *every* `--scripts-dir` file, with `prefix` used only to choose path-mode vs
name-mode and never to filter), and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:941-952`
(`_describe_scripts`, the same full parse) reached from
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:1025-1027`
(`render_script_listing`, the renderer round 7 introduced and both listing
surfaces now share).
The completer is installed unwrapped at
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/prompt_common.py:573`
(`completer=SimulatorCompleter()` — no `ThreadedCompleter`), and the interactive
prompt is a task on the same loop as the door server
(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/cli.py:853`,
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/cli.py:1104`).

**Reproduction**

Setup — 200 scripts of 30 steps each (1.2 KB apiece, 242 KB total) in a
`--scripts-dir`:

```
$ python -c "...write 200 s000..s199.yaml..."
created 200 scripts, total bytes 242380
```

*(a) A single Tab press stalls the door protocol server.* `/tmp/ppd8/tabprobe.py`
starts a real `ppd-simulator` under a PTY, opens a real TCP socket to the door
port, measures `GET_DOOR_STATUS` round-trip time continuously, then writes
`run s1\t` to the PTY. The second run is the control: identical harness, no
`--scripts-dir`.

```
$ python /tmp/ppd8/tabprobe.py            # run 1, load average 7.6
200 scripts:                baseline n=40 max=  1.6ms | after TAB n=67 max=639.5ms
no --scripts-dir (control): baseline n=40 max=  1.5ms | after TAB n=80 max= 11.1ms

$ python /tmp/ppd8/tabprobe.py            # run 2, load average 9.1
200 scripts:                baseline n=40 max=  5.8ms | after TAB n=45 max=1694.2ms
no --scripts-dir (control): baseline n=40 max=  1.4ms | after TAB n=79 max= 23.6ms
```

*(b) `ctl list` stalls the daemon's door server the same way.*
`/tmp/ppd8/listprobe.py` — real `ppd-simulator --daemon 3851 --port 3850
--scripts-dir /tmp/ppd8/many`, same door-port latency probe, one `ctl list`, with
`ctl status` timed in the same run as the control:

```
$ python /tmp/ppd8/listprobe.py           # run 1
baseline door RTT: n=67 max=0.4ms
`ctl list` wall time: 754ms, lines returned: 210
door RTT during/after `ctl list`: n=38 max=587.2ms
`ctl status` wall: 137ms

$ python /tmp/ppd8/listprobe.py           # run 2
baseline door RTT: n=67 max=0.4ms
`ctl list` wall time: 726ms, lines returned: 210
door RTT during/after `ctl list`: n=38 max=561.8ms
`ctl status` wall: 137ms
```

`ctl status` is 137 ms both runs (that is ctl's own process/import cost);
`ctl list` is 5.3× that, and the extra 590 ms is spent inside the daemon with the
event loop held.

*(c) The typed prefix does not narrow the work.* In-process, one call per row,
median of 5, `prefix="s000"` (which matches exactly one script):

```
no --scripts-dir (7 built-ins only)    script_completer('s000') median=   14.9 ms  candidates returned=7
10 scripts in --scripts-dir            script_completer('s000') median=   56.4 ms  candidates returned=17
25 scripts in --scripts-dir            script_completer('s000') median=  159.2 ms  candidates returned=32
50 scripts in --scripts-dir            script_completer('s000') median=  292.5 ms  candidates returned=57
100 scripts in --scripts-dir           script_completer('s000') median=  572.7 ms  candidates returned=107
200 scripts in --scripts-dir           script_completer('s000') median= 1313.6 ms  candidates returned=207
```

`candidates returned` is the whole set at every size — the completer hands
prompt_toolkit all 207 names and lets *it* do the prefix match, after paying for
207 YAML parses. Cost is linear in the directory, not in the answer.

Per-file cost, isolated:

```
Script.from_file on one 1209B script: median=4.87 ms
```

**Description.** Round 7 correctly made `--list-scripts` and `list` share one
renderer. That renderer, and the completer beside it, both answer "what can I
run?" by fully YAML-parsing every candidate file, every single time, with no
cache and no prefix filter. Three things follow:

1. **The stall lands on the emulated device.** `SimulatorCompleter` is installed
   without `ThreadedCompleter`, and `prompt_async` is awaited on the same
   `asyncio` loop that serves the door protocol, so the parse runs *in* the
   event loop. The door's round-trip time goes from 1.5 ms to 640–1694 ms
   because the operator pressed Tab. The simulator's entire job is to be a
   faithful device for library tests; a client connected during that keystroke
   sees a device that stopped answering. The same holds for the daemon: `ctl
   list` is a control-plane command and it blocks the data plane for ~590 ms.
2. **The cost is paid for answers nobody asked for.** Completing `s000` — four
   characters that already identify one file — costs exactly as much as
   completing nothing. This is the persona's "only fetch the data we need" case
   in its purest form: `if not name.startswith(prefix): continue` before the
   parse would turn 1313 ms into ~20 ms.
3. **It is not free at the default either.** With no `--scripts-dir` at all,
   every Tab still parses the 7 built-ins for 15–24 ms. That is invisible on its
   own but it is the floor the design sets.

`--scripts-dir` exists precisely so users add their own scripts, and
`docs/simulator.md:99` advertises that they then appear in `list`, in
`--list-scripts` and in tab completion — so a populated directory is the designed
case, not an exotic one. I am explicit that the severity rests on that: at the
shipped default this is a 15–24 ms nuisance, and it becomes a half-second
event-loop stall somewhere between 50 and 100 scripts.

**Recommendation.**

1. Filter by prefix **before** parsing in `script_completer`'s name branch (and
   skip the `Path.cwd()` walk when the prefix already excludes it). One `continue`
   makes the common case O(matches).
2. Memoize `_describe_scripts` on `(path, st_mtime_ns, st_size)`. Descriptions
   are what the parse is *for*, and they change only when the file does; a dict
   keyed on the stat tuple keeps the "pick up new files immediately" behaviour
   that `list` relies on.
3. Cheaper still for the description itself: `Script.from_file` parses the whole
   step list to read one `description:` field. `yaml.safe_load` on the file is
   the wrong tool for a listing; reading the top-level mapping only (or falling
   back to the name when it is expensive) removes most of the per-file cost.
4. Independently of the above, wrap `SimulatorCompleter` in prompt_toolkit's
   `ThreadedCompleter` so no future completer can block the door server from a
   keystroke. That is a one-line defensive change and it does not touch the wire.

None of this changes a byte on the wire or narrows what is accepted from a peer.

---

### L1 (Low) — the daemon-mode section of `docs/simulator.md` quotes verbatim two strings that the round-7 fixes changed, including one it quotes *as* the control-channel behaviour

**Where:** `/home/prez/src/pypowerpetdoor/docs/simulator.md:320-324`, inside the
section that opens "Over the control channel, `run` accepts only **bare script
names**" (`docs/simulator.md:306`). The strings moved at
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:875-886`
(`describe_out_of_directory_remedy`, the L6 fix) and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:1029-1033`
(`render_script_listing`'s marker, the L5 fix).

**Reproduction.** What the doc says, against what the binaries do — daemon on
3801/3800 with `--scripts-dir /tmp/ppd8/scripts`, `linked.yaml` a symlink out of
that directory, `basic_cycle.yaml` shadowing a built-in:

```
$ sed -n '317,325p' docs/simulator.md
A file in that directory that *resolves outside* it (a symlink pointing away)
is not runnable by bare name, and is not listed by `list`, `--list-scripts`,
the `Available:` hint or tab completion either — all four surfaces agree with
the loader. Naming it explicitly answers `Script '<name>' resolves outside
<dir> and cannot be run by name; move it into the directory or run it by path`.
A scripts-dir script whose name matches a built-in **shadows** it; `list`
marks the built-in `(shadowed by <dir>/<name>)` and tab completion offers the
name once.

$ ppd-simulator-ctl -p 3801 run linked
ERROR: Script 'linked' resolves outside /tmp/ppd8/scripts and cannot be run by name; move it into the directory (paths are not accepted over the control channel)

$ ppd-simulator --scripts-dir /tmp/ppd8/scripts --list-scripts | grep shadow
  basic_cycle: Pet triggers inside sensor, door opens, holds, then closes (shadowed by /tmp/ppd8/scripts/basic_cycle.yaml)

$ ppd-simulator --scripts-dir /tmp/ppd8/sd2 --list-scripts | grep shadow
  obstruction_test: Tests that door auto-retracts when obstruction detected (shadowed by /tmp/ppd8/sd2/obstruction_test.yml)
```

Three separate divergences, all in those five lines:

| Doc says | Binaries do |
|---|---|
| `...; move it into the directory or run it by path` | over ctl: `...; move it into the directory (paths are not accepted over the control channel)` |
| "`list` marks the built-in" | `list` **and** `--list-scripts` both mark it (that was the L5 fix) |
| `(shadowed by <dir>/<name>)` | `(shadowed by <dir>/<name>.yaml)` — or `.yml` |

**Description.** The first row is the worst of the three, because the paragraph
is the control-channel documentation and the sentence it quotes is now the
*local CLI* variant. Round 7's L6 finding was precisely that the ctl message sent
the operator to a form ctl refuses; the code was fixed and the doc that quotes it
was not, so the contradiction moved from the product into the manual.

The third row is the one `render_script_listing`'s own docstring calls out — it
says the reconstructed `<dir>/<name>` form "dropped the suffix, so it read like a
path but `ls` on it failed". The doc still documents the form the code was
changed to stop emitting.

Both code strings *are* pinned by tests — `tests/simulator/test_commands.py:1591`
and `:1650` assert them — so the drift is one-sided: the implementation is
guarded and the prose that quotes it is not. `tests/test_docs_accuracy.py` reads
`SIMULATOR_MD`'s `options`, `info`, `script-format`,
`running-scripts-programmatically`, `conditions-for-*` and `available-actions`
sections; the daemon-mode prose is not among them.

**Recommendation.** Update the three phrases. Then extend
`tests/test_docs_accuracy.py` with the same technique it already uses for the
condition tables: assert that the quoted refusal equals
`describe_out_of_directory_remedy()` evaluated under `_script_paths_allowed =
False`, and that the marker template matches what `render_script_listing`
produces for a synthetic shadowing file. A backticked string in a doc that names
a surface's exact output is the case that most needs an executable pin.

---

### L2 (Low) — "Settings that can be used with `set` and `toggle`" lists nine rows; two of them fail with `toggle`

**Where:** `/home/prez/src/pypowerpetdoor/docs/simulator.md:830-846` (the second
`### Settings` heading, under the Scripting section) versus
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:763-783`
(`_toggle_value`, which knows seven names) and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:728-761`
(`_set_value`, which knows all nine).

**Reproduction.** One `{action: toggle, name: <row>}` script and one
`{action: set, name: <row>, value: "1"}` script per documented row, each run
through `ppd-simulator --port 0 --scripts-dir ... --script X --oneshot`:

```
=== toggle <setting> for each of the 9 documented rows ===
power          rc=0  >>> Script PASSED: T
auto           rc=0  >>> Script PASSED: T
inside         rc=0  >>> Script PASSED: T
outside        rc=0  >>> Script PASSED: T
autoretract    rc=0  >>> Script PASSED: T
safety_lock    rc=0  >>> Script PASSED: T
cmd_lockout    rc=0  >>> Script PASSED: T
hold_time      rc=1  Unknown setting to toggle: hold_time
battery        rc=1  Unknown setting to toggle: battery

=== set <setting> for each of the 9 documented rows ===
power          rc=0  >>> Script PASSED: S
auto           rc=0  >>> Script PASSED: S
inside         rc=0  >>> Script PASSED: S
outside        rc=0  >>> Script PASSED: S
autoretract    rc=0  >>> Script PASSED: S
safety_lock    rc=0  >>> Script PASSED: S
cmd_lockout    rc=0  >>> Script PASSED: S
hold_time      rc=0  >>> Script PASSED: S
battery        rc=0  >>> Script PASSED: S
```

**Description.** This is round-7 L1 again — a table introduced as applying to two
actions when it applies fully to only one — one section below the table that was
just corrected for exactly that. The header says "Settings that can be used with
`set` **and** `toggle`", and the table's own `Type` column already gives the game
away: the two failing rows are the two non-boolean ones (`hold_time` → number,
`battery` → integer), and the `**toggle**` action entry two sections up
(`docs/simulator.md:729-735`) correctly says "Toggle a boolean setting". So the
table is the wrong part, and the information needed to fix it is already in the
table.

Round 7 added three tests to stop the condition tables drifting
(`test_the_wait_for_condition_table_matches_the_implementation`,
`test_the_assert_condition_table_matches_the_implementation`,
`test_the_two_condition_tables_are_disjoint`). None covers this table, which is
why the identical defect one screen away survived the pass.

**Recommendation.** Split the rows, or mark them: retitle to "Settings for
`set`", and add a sentence (or a column) saying `toggle` accepts the boolean rows
only. Then add the fourth docs-accuracy test in the same shape as the three round
7 added — extract the table, extract `_set_value`/`_toggle_value`'s accepted
names from source the way the existing tests do, and assert the two documented
sets against them.

---

### L3 (Low) — the round-7 fixes changed script-DSL behaviour in a way that breaks existing user scripts, and `CHANGELOG.md` has no entry for any of it

**Where:** `/home/prez/src/pypowerpetdoor/CHANGELOG.md` — untouched by `da31ae2`.
The behaviour changes are at
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:434-441`
(unknown step parameters now raise) and `:454-456` (unknown `sensor:` now raises).

**Reproduction.** `da31ae2` is the first fix commit in this series not to update
the changelog:

```
$ git show --stat HEAD -- CHANGELOG.md
(no diff — not touched)

$ git log -1 --format='%h %ad %s' --date=short -- CHANGELOG.md
a0194bd 2026-08-22 Round 6 fixes; revert the enabled wire change; layer the wire boundary

$ git log --oneline a0194bd..HEAD
da31ae2 Round 7 fixes (refuter-approved list only)
9e61383 Add round 7 adversarial refutation pass
4958f2b Add persona analysis round 7 reports (execution-proven findings only)

$ for pat in "Unknown parameter" "Unknown sensor" "shadowed by" \
             "paths are not accepted over the control channel" "inside and outside sensors"; do
    printf '%-52s -> %s\n' "$pat" "$(grep -c "$pat" CHANGELOG.md)"; done
Unknown parameter                                    -> 0
Unknown sensor                                       -> 0
shadowed by                                          -> 0
paths are not accepted over the control channel      -> 0
inside and outside sensors                           -> 0
```

The break, executed against both sides. A perfectly ordinary annotated step —
`note:` on a `wait` — is the kind of thing a YAML author writes without thinking:

```yaml
name: "Annotated user script"
steps:
  - action: log
    message: "start"
  - action: wait
    seconds: 1
    note: "let the door settle"
  - action: log
    message: "end"
```

```
$ PYTHONPATH=/tmp/ppd8/prev/src python -c "import powerpetdoor,sys; sys.stderr.write('[guard] '+powerpetdoor.__file__+'\n')"
[guard] /tmp/ppd8/prev/src/powerpetdoor/__init__.py

$ PYTHONPATH=/tmp/ppd8/prev/src python -m powerpetdoor.simulator --port 0 \
      --scripts-dir /tmp/ppd8/upgrade --script annotated --oneshot ; echo "BEFORE rc=$?"
  Step 2: wait(seconds=1, note=let the door settle)
>>> All scripts PASSED
BEFORE rc=0

$ python -m powerpetdoor.simulator --port 0 \
      --scripts-dir /tmp/ppd8/upgrade --script annotated --oneshot ; echo "AFTER rc=$?"
  Step 2: wait(seconds=1, note=let the door settle)
[ERROR] Script error at step 2: Unknown parameter(s) for wait: note. Use: seconds
>>> All scripts FAILED
AFTER  rc=1
```

**Description.** The strictness itself is right — it is round 7's F-L3/F-M2 fix
and it was refuter-approved, and `docs/simulator.md:623-628` documents it well.
The gap is the upgrade path. `CHANGELOG.md` opens with "All notable changes to
this project will be documented in this file" and the project keeps a real,
detailed one; round 6's fixes are in it in full. A user who upgrades and finds
their script suite exiting 1 has the reference manual to read *after* they have
worked out what happened, and nothing to tell them it was going to happen.

Six user-visible changes from `da31ae2` are unrecorded: unknown step parameters
now fail the run; unknown `sensor:` names now fail the run; `holdtime`/`inside`/
`outside`/`charge_rate`/`discharge_rate` now reject `nan`/`inf`; `--list-scripts`
gained the shadow marker (and the marker's format changed); the out-of-directory
refusal's advice changed over ctl; the schedule sensor scope wording changed on
two commands. The first two are breaking; the rest are the kind of thing a
release note exists for.

Nothing in `tests/` or `.github/` checks that the changelog moves, which is why
this slipped silently after four rounds of doing it correctly.

**Recommendation.** Add the entries — a `### Changed` line each for the listing,
message and wording changes, and a clearly-marked breaking pair under a
`### Changed` (or `### Fixed`, since the old behaviour silently no-op'd) note
naming both new errors and the one-line remedy ("remove the unrecognised key" /
"use `inside` or `outside`"). If it is worth the CI minute, a check that
`CHANGELOG.md` is in the diff whenever `src/` is would make the practice
self-enforcing.

---

### L4 (Low) — "here are the valid values" now has three spellings, and five of the script DSL's seven `Unknown X` errors still name none — including the one the round-7 docs fix was written about

**Where:** the two messages round 7 added,
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:440`
(`... Use: {expected}`) and `:456` (`... Use: {', '.join(SENSOR_NAMES)}`),
against their five untouched siblings at `scripting.py:561` (`Unknown action`),
`:681` (`Unknown condition`), `:760` (`Unknown setting`), `:782`
(`Unknown setting to toggle`) and `:816` (`Unknown assertion condition`) — and
against the two other spellings already shipped, `scripting.py:967`
(`Available: ...`) and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/base.py:216`
(`Choose from: ...`).

**Reproduction.** One one-step script per misspelling class, each run through
`ppd-simulator --port 0 --scripts-dir ... --script X --oneshot`:

```
e_action   Unknown action: frobnicate
e_setting  Unknown setting: powr
e_toggle   Unknown setting to toggle: powr
e_assert   Unknown assertion condition: door_closed
e_cond     Unknown condition: door_stat
e_sensor   Unknown sensor: insde. Use: inside, outside
e_param    Unknown parameter(s) for wait: duration. Use: seconds
```

The interactive/ctl front end onto the same product has a settled convention,
and it is a third one:

```
$ ppd-simulator-ctl -p 3801 schedule bogus
ERROR: Unknown schedule subcommand: bogus
Available: add, clear, days, delete, disable, enable, list, time
$ ppd-simulator-ctl -p 3801 broadcast bogus
ERROR: Unknown broadcast subcommand: bogus
Available: all, battery, hwinfo, notifications, schedules, settings, stats, status
$ ppd-simulator-ctl -p 3801 notify bogus
ERROR: Unknown notify subcommand: bogus
Available: inside_off, inside_on, low_battery, outside_off, outside_on
$ ppd-simulator-ctl -p 3801 schedule add sideways 6:00-22:00 all
ERROR: 'sideways' is not valid. Choose from: inside, outside, both
$ ppd-simulator-ctl -p 3801 stop bogus
ERROR: 'bogus' is not valid. Choose from: all
```

And an action that takes no parameters at all reads oddly:

```
n1   -> Unknown parameter(s) for close: delay. Use: none
n2   -> Unknown parameter(s) for pet_on: duration. Use: none
```

**Description.** Two separate consistency problems, both created or sharpened by
the round-7 pass.

**The spellings.** `Available: a, b, c` (CLI subcommands and unknown scripts),
`Choose from: a, b, c` (every `ArgSpec` of type `choice`) and now `Use: a, b, c`
(the two new script-DSL messages) are three renderings of one idea across one
product. `Use: none` compounds it: it reads like an instruction to pass the
literal token `none`, when it means "this action takes no parameters".

**The five that still say nothing.** `Unknown assertion condition: door_closed`
is the sharp one. Round 7's L1 wrote: *"A script author following the docs writes
`assert door_closed` (the single most natural assertion in a door simulator) and
gets a hard step failure with no pointer to the right table."* The fix chosen was
docs-only — correctly, the tables are now accurate and disjoint (verified in
[Round 7 Fix Verification](#round-7-fix-verification)) — but the message that
made it a trap is unchanged, and the author who mistypes is by definition not
reading the manual at that moment. `_assert_condition` knows all twelve accepted
names three lines above the raise, and `door_closed` is *specifically* a name the
runner recognises for the other action, so the message can be better than a list:

```
Unknown assertion condition: door_closed ('door_closed' is a wait_for condition;
assert accepts: auto, autoretract, battery, cmd_lockout, door_status, hold_time,
inside, outside, power, safety_lock, total_auto_retracts, total_open_cycles)
```

Because the DSL is the **CI** front end, these messages are what a developer sees
in a build log with no terminal to experiment in — which is exactly the context
where naming the alternatives is worth the most.

**Recommendation.** Pick one spelling — `Available: ...` has the most existing
users and reads correctly for an empty set ("Available: none") — and route all
three DSL/CLI families through one helper, the way `describe_script_argument()`
and `describe_out_of_directory_remedy()` already centralise policy-dependent
strings. Then give the five bare messages their list: `_ACTION_PARAMS.keys()` for
`Unknown action`, the `_set_value`/`_toggle_value` name sets, `_check_condition`'s
names plus `_STATUS_WAIT_CONDITIONS`, and `_assert_condition`'s twelve — every one
of them is a literal already present in the same function. Add the
cross-table hint for the `assert`/`wait_for` pair, since the disjointness the
docs now assert is exactly the confusion the message can resolve.

---

## Round 7 Fix Verification

All ten items with a front-end surface verified against running binaries.
Nothing regressed. Baseline `2576 passed`, `tests/test_docs_accuracy.py`
`24 passed`.

| Item | Status | Evidence |
|------|--------|----------|
| **F-M1** — `parse_arg` rejects non-finite across all five `"float"` commands | ✅ Verified | All ten inputs rejected, `rc=1`, with the bounds message family: `holdtime nan` / `inf` / `Infinity` / `1e400` / `-nan` / `NaN`, `inside nan`, `outside inf`, `charge_rate nan`, `discharge_rate inf` → `ERROR: '<v>' must be a finite number` + `Usage: ...`. State untouched afterwards: `holdtime` → `Hold time: 2.0s`; `broadcast settings` → `No clients connected` (the pre-existing no-client refusal), not the old `cannot convert float NaN to integer`. Bounds still hold at the edges: `holdtime 0.05` → below minimum (0.1), `holdtime 0.1` → OK, `holdtime 900` → OK, `holdtime 900.1` → above maximum (900). |
| **F-M1** — validate-then-write ordering | ✅ Verified | `settings.py:157-158` validates before the assignment; live, no `ERROR` path leaves state changed (`holdtime` re-read is unchanged after every rejected input above). The second layer's own message is `Hold time must be a finite number, got <v>` — only reachable programmatically now, which is what its docstring claims. |
| **F-M2** — unknown `sensor:` raises | ✅ Verified | `{action: trigger_sensor, sensor: insde}` with both sensors disabled and the safety lock on → `[ERROR] Script error at step 4: Unknown sensor: insde. Use: inside, outside` / `>>> Script FAILED`, `rc=1`. Control with the real name under the same state → `>>> Script PASSED`. The old run reported PASSED having opened the door through every gate; it now cannot. |
| **F-L3** — unknown step parameter raises | ✅ Verified | `{action: wait, duration: 8}` → `Unknown parameter(s) for wait: duration. Use: seconds`, `rc=1` (was: PASSED in 1.2 s). All 19 dispatched actions carry a parameter set and both drift directions are pinned (`test_every_executed_action_declares_its_parameters`, `test_every_declared_parameter_is_actually_read`). Every fenced YAML block in `docs/simulator.md` (23 of them) validated against `_ACTION_PARAMS`: **0 mismatches**. |
| **F-L5** — one renderer for `list` and `--list-scripts` | ✅ Verified byte-identical | Same daemon, same `--scripts-dir`, `basic_cycle.yaml` shadowing the built-in. `sed 's/^OK: //' ctl_list.txt \| head -n -1` vs `--list-scripts` → `diff` reports **IDENTICAL**. The marker names the real file with its suffix (`(shadowed by /tmp/ppd8/scripts/basic_cycle.yaml)`) and expresses a `.yml` shadow too (`(shadowed by /tmp/ppd8/sd2/obstruction_test.yml)`). Empty dir → `(none)` on both; no `--scripts-dir` → section omitted on both; nonexistent dir → argparse error `rc=2`. Terminal-safety parity checked with a hostile scripts-dir: an ESC in a *filename* renders `na\x1b[32mme` and an unparseable description renders identically on both surfaces. |
| **F-L6** — policy-aware out-of-directory remedy | ✅ Verified both policies | Over ctl: `run linked` → `... cannot be run by name; move it into the directory (paths are not accepted over the control channel)`; `run /tmp/.../linked.yaml` → `Script paths are not allowed over the control channel; ...` — the two refusals no longer point at each other. Local PTY-less CLI (`--port 3810 --scripts-dir ...`): `run linked` → `... move it into the directory or run it by path`, and `run /tmp/ppd8/out/outside.yaml wait` → `>>> Script PASSED: Outside Script`, so the advice is actionable exactly where it is given. (The *doc* that quotes this string was not updated — **L1**.) |
| **F-L1 / F-L4** — condition tables split, disjoint, and complete | ✅ Verified by execution, not just by the new tests | All 20 documented `wait_for` names accepted by `_check_condition`: **0 rejected**. All 12 documented `assert` names accepted by `_assert_condition`: **0 unknown**. Cross-use: **20/20** `wait_for` names rejected by `assert`, **12/12** `assert` names rejected by `wait_for` — genuinely disjoint, as `test_the_two_condition_tables_are_disjoint` asserts. `door_closing` is now a documented row. Actions: documented ∆ implemented = **∅ in both directions** (19 = 19). |
| **F-L2** — the two failing doc examples | ✅ Verified by running them | Every whole-script YAML block in `docs/simulator.md` executed on a fresh `DoorSimulator`: `block[0] name='My Test Script' steps=3 -> PASSED`. The `from_simple_commands` twin, commands scraped from the doc: `['trigger inside', 'wait_for door_closed 10', 'assert door_status DOOR_CLOSED']` → `True`. Both were `rc=1` in round 7. |
| **F-T1** — `protocol.md` keepalive `msgID` softened | ✅ Verified end-to-end | Raw socket: `C->D {"PING": "1710000000123", "msgId": 7, "dir": "p2d"}` → `D->C {"CMD": "PONG", "PONG": "1710000000123", "success": "true", "dir": "d2p"}`, `has msgID? False`. `docs/protocol.md:174-178` now reads "the echoed token is the whole correlation mechanism, and the client never reads `msgID` on a `PONG`. Whether the firmware echoes `msgId` back as `msgID` here ... is **unverified**; this project's simulator does not". Doc-side only; **no wire change was made**. |
| **F-T2** — one rendering of the schedule sensor scope | ✅ Verified | `add` echo, `list` and the implicit line now agree: `Added schedule #2: inside and outside sensors, all days, 03:00-04:00` / `#2: inside and outside sensors, ... (enabled)` / `(implicit): inside and outside sensors, all days, 00:00-23:59`, and the single-sensor forms read `inside sensor` / `outside sensor`. The ungrammatical `both sensor` is gone. |
| **T-L2 (cross-persona, front-end surface)** — `CONTROL_PORT_OFFSET` | ✅ Verified | `cli.py:1080` now uses the constant; `test_the_documented_control_port_offset_is_the_constant` and `test_the_daemon_control_port_really_is_the_documented_offset` pin `docs/simulator.md:97`'s "door port + 1" to it through the real argument parser. |
| **New: `tests/test_docs_accuracy.py` additions** | ✅ Verified | 24 passed. Independently re-derived by execution above rather than by re-reading the tests: the condition tables, the action list and both doc examples. The options table was also checked exhaustively by a separate route — every `--flag`/`-f` in `cli.py`'s parser appears in the options table and vice versa (**0 either way**), same for `ctl.py`'s five flags. |

---

## Areas Reviewed With No Findings

**Docs link integrity.** Crawled every relative markdown link in `docs/*.md`,
`README.md` and `CHANGELOG.md`, resolving anchors with GitHub's slug rules
(including the new `#conditions-for-wait_for` / `#conditions-for-assert`
targets): **0 broken links, 0 missing anchors** across all 7 files.

**Option and command surface parity.** `ppd-simulator`'s 25 documented
option spellings == the 25 in its argparse (0 either way);
`ppd-simulator-ctl`'s 11 likewise, all mentioned in prose. Every registered
command name appears in the interactive tables and vice versa (**0 either way**),
and every alias (`/`, `?`, `hist`, `sched`, and the 32 others) is documented
somewhere in `docs/simulator.md`.

**README quick-starts.** Both executed verbatim against a live simulator
(host/port swapped): `Door status: CLOSED`, `Battery: 100%`, `set_hold_time(15)`,
`set_inside_sensor(True)`, `on_status_change` registered, clean `disconnect()`;
then `send_message(CONFIG, CMD_GET_SETTINGS, notify=True)` → an 11-key settings
dict. Both printed OK, `rc=0`.

**Built-in scripts under the new strictness.** The documented CI command
`python -m powerpetdoor.simulator --script full_test_suite --oneshot` runs all 32
steps → `>>> All scripts PASSED` in 11.5 s. No built-in trips the new parameter
or sensor validation.

**Argument bounds and error paths.** ~25 bad-input cases across `holdtime`,
`battery`, `charge_rate`, `discharge_rate`, `inside`, `outside`, `notify`,
`schedule`, `stop`, `run`, `broadcast`, `ac`, `timezone`, plus `bogus_command`.
All produced a specific message and a usage line, all `rc=1`. Boundary spot
checks passed on both sides of each documented limit. Non-finite input is now
handled (F-M1). I did note that `inside`/`outside` (no `max_value`) accept
`1e300` and render `Inside sensor activated for 1e+300s`, where the script DSL
bounds the same field to 86400 — but nothing crashes, no state is corrupted, the
CLI bound is not documented, and the effect is indistinguishable from the
documented `0` = indefinite. Not worth a finding; noting it so it is on the
record.

**Script queue mechanics.** `run` ×3 → `Script: running "Long Script long1"
(2 queued)` / `Queued: Long Script long2, Quick Script`; `stop` → `Stopping
script: Long Script long1 (2 still queued; use 'stop all' to discard them)`;
`stop all` → `Stopping script: Long Script long2 (dropped 1 queued)` and
`Script: none running`; idempotent repeat → `Nothing running or queued`, `rc=0`.
Drop count matched the depth `list` printed a moment earlier, every time.

**`run ... wait` streaming and exit codes.** `ctl run quick wait 1>o 2>e`:
stdout held exactly `OK: Script PASSED: Quick Script`, stderr held the five
daemon `LOG:` lines. `ctl` no args → `rc=1` after the epilog; bad flag → `rc=2`;
one-shot against a dead port → `Connection refused to 127.0.0.1:39999`, `rc=1`;
`-i` against the same → `Error: Connection refused - simulator not running on
127.0.0.1:39999`, `rc=1`.

**Interactive session under a PTY.** Full `help` rendering with correct gating
(`history (hist) [clear|N]` present under the PTY), syntax highlighting active,
`run <TAB>` opening a completion menu that offers `basic_cycle` **once** with the
scripts-dir description (round-6 L3 holds), `linked` never offered, `hold` and
`shutdown` executing normally. The stall this exposed is **M1**; the behaviour
itself is correct.

**Notifications, schedules, timezone.** `notify` block, `notify help`, alias
resolution (`low_bat`/`lowbat` → `low_battery`), `notify bogus` → `Available:
...`, `notify inside_on maybe` → `'maybe' is not valid. Use on/off`. Full
schedule lifecycle: `add` (all three scopes, with and without days), `list`,
`clear`, `clear` on empty, and the implicit line. `timezone` bare, IANA set,
POSIX set, and `Unknown timezone: Not/AZone`.

**Terminal safety.** A `--scripts-dir` containing a filename with a raw ESC and a
YAML file whose description carries ESC + BEL: both listing surfaces render
`na\x1b[32mme` and the sanitized load error identically; nothing reached the
terminal raw (`cat -v` on both).

**Out-of-directory containment, all four surfaces.** With
`scripts/linked.yaml → /tmp/ppd8/out/outside.yaml`: absent from `ctl list`,
absent from `--list-scripts`, absent from the `Available:` hint (`Unknown script:
nosuchscript. Available: basic_cycle, full_test_suite, obstruction_test,
pet_presence_test, power_lockout_test, quick, safety_lock_test, schedule_test`),
and absent from completion. The doc sentence describing this is accurate; only
the refusal it quotes is stale (**L1**).

**Round-7 resource-bound changes, front-end impact.** The write-ceiling latch and
`transport.abort()` are confined to the door protocol
(`simulator/protocol.py:507-543`); the control channel has no such path, so
`ctl run ... wait` log streaming is unaffected — confirmed by a full wait-run with
stdout/stderr split. A real `PowerPetDoorClient` stayed connected across the
README quick-starts and a keepalive capture with no anomaly.

---

## Notes on scope

`docs/protocol.md` is treated as reverse-engineered and non-authoritative
throughout. **No finding here proposes changing the device wire protocol.** M1 is
confined to listing/completion rendering and the completer's threading; L1 and L2
are docs-only; L3 is changelog-only; L4 is operator-facing error strings in the
CLI and the script DSL, neither of which is on the wire. The one wire-adjacent
observation (`PONG` carries no `msgID`) was made to *confirm* that round 7 moved
the doc rather than the code, and it did.
