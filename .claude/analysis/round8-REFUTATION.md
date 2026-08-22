# Round 8 Refutation Pass

Adversarial re-verification of all 20 round-8 findings at commit `f8797e0`
(`src/`, `tests/`, `docs/`, `scripts/` identical to `da31ae2`, which the four
reports audited; `f8797e0` only adds the reports themselves).

**Method.** Every finding was re-derived from scratch. I ran none of the
round-8 scripts and reused none of their transcripts; I wrote my own harnesses,
my own mutations and my own measurements. Work happened in `/tmp/r8*` on a
`git archive HEAD` copy with `PYTHONPATH` forced at the copy's `src/` and a
pytest plugin (`-p guardplugin`) that raises unless
`powerpetdoor.__file__.startswith($R8_EXPECT_ROOT)`. The guard was itself
falsified before use:

```
$ R8_EXPECT_ROOT=/nonexistent/xyz ... pytest -p guardplugin --co tests/test_framing.py
SystemExit: GUARD FAIL: /tmp/r8work_null/src/powerpetdoor/__init__.py does not start with /nonexistent/xyz
```

so every result below provably ran against the `/tmp` copy and not the editable
install. No repository file was modified (`git status` clean throughout, checked
at the end); every daemon and PTY child I started was terminated; all scratch
lives under `/tmp`.

**Null controls.** Two unmutated copies through the identical path, first and
last: `2620 passed in 38.49s` and `2620 passed in 37.97s`.

**Mutation discipline.** 16 mutations, each on its own fresh copy, each a full
suite run (no `-k`, no subsetting). **4 controls that must be caught were all
caught**; **12 non-control mutations produced exactly the outcome the
test-fanatic report claimed.** 16/16 agreement:

```
ctrl_inflight      expect=CAUGHT   actual=CAUGHT   FAILED ...test_the_inflight_bound_is_64
tm2_ctrl_value     expect=CAUGHT   actual=CAUGHT   FAILED ...test_the_blocked_recheck_floor_is_100ms
tm3_ctrl_lower     expect=CAUGHT   actual=CAUGHT   4 failed, 2616 passed
tm5_ctrl_non_ascii expect=CAUGHT   actual=CAUGHT   FAILED ...test_disconnect_reports_the_non_ascii_total_and_resets
tm1_dedupe         expect=SURVIVED actual=SURVIVED 2620 passed
tm2_floor          expect=SURVIVED actual=SURVIVED 2620 passed
tm3_upper          expect=SURVIVED actual=SURVIVED 2620 passed
tm4_actionparams   expect=SURVIVED actual=SURVIVED 2620 passed
tm5_device_errors  expect=SURVIVED actual=SURVIVED 2620 passed
tm5_bad_messages   expect=SURVIVED actual=SURVIVED 2620 passed
tl1_cond_in_tuple  expect=SURVIVED actual=SURVIVED 2620 passed
tl2_docs_fake_action expect=SURVIVED actual=SURVIVED 2620 passed
tl3_keep_bool      expect=SURVIVED actual=SURVIVED 2620 passed
tl4_holdtime_int   expect=SURVIVED actual=SURVIVED 2620 passed
tl5_dead_loop      expect=SURVIVED actual=SURVIVED 2620 passed
null_control_2     expect=SURVIVED actual=SURVIVED 2620 passed
```

**Headline result: 13 CONFIRMED, 7 CONFIRMED-BUT-OVERSTATED, 0 REFUTED.**

I want to be explicit that a zero-refutation round is not a rubber stamp, and to
say precisely what I *did* kill, because it is not nothing:

- **The headline fix for the only High is refuted by execution.** T-H1's
  recommendation 1 (anchor the pragma pattern to `#\s*pragma:\s*no\s+cover`) is
  a **no-op on three of the four prose lines it names** — those lines literally
  contain `` `# pragma: no cover` `` in markdown backticks, so the anchored
  regex still matches. The report's own Step-3 output (`TOTAL 6662`, identical
  to baseline) is the evidence, misread as "no breakage" when it means "no
  effect". The finding stands; its fix does not.
- **Security L2's recommended constant is rejected on wire grounds.** It
  proposes bounding the shipped facade at `MAX_HOLD_TIME_CENTISECONDS = 90000`
  imported from `simulator/protocol.py`. That narrows what the library accepts
  from a real device to a reverse-engineered simulator constant, and it would be
  the first import from `simulator/` into the core library. Rejected regardless
  of technical merit.
- **Backend L1's recommendation as written breaks a shipped pinned test.**
- **Security L2's "a regression introduced by the round-7 fix" is disproven** —
  the pre-round-7 code raises the same `OverflowError`.
- **Two severities were inflated by a full step** (F-M1, T-H1) and five by one
  step, on the round-7 precedent for single-missing-test findings.
- **Half of F-L4 is discarded** as unsupported churn.
- Three supporting numbers I would not quote are listed at the end.

---

## Summary

| ID | Persona | Claimed | Verdict | Adjusted |
|---|---|---|---|---|
| B-M1 `json.loads` escape → reconnect storm | backend | Medium | **CONFIRMED** | Medium |
| B-L1 `_keep_int` unbounded → `OverflowError` per frame | backend | Low | **CONFIRMED** | Low (fix as written is wrong) |
| S-M1 same root → permanent dispatcher wedge + fd/slot leak | security | Medium | **CONFIRMED** | Medium |
| S-L2 same as B-L1 | security | Low | **CONFIRMED** | Low (recommendation rejected) |
| F-M1 listing/completion re-parses everything on the loop | frontend | Medium | **OVERSTATED** | Low |
| F-L1 daemon-mode docs quote three stale strings | frontend | Low | **CONFIRMED** | Low |
| F-L2 `set`+`toggle` table: 2 of 9 rows fail `toggle` | frontend | Low | **CONFIRMED** | Low |
| F-L3 breaking DSL change with no CHANGELOG entry | frontend | Low | **CONFIRMED** | Low |
| F-L4 three "valid values" spellings; five bare messages | frontend | Low | **OVERSTATED** | Low (half discarded) |
| T-H1 `exclude_lines` over-matches prose | test | High | **OVERSTATED** | Medium (rec 1 refuted) |
| T-M1 `_schedule_pump` dedupe unobserved | test | Medium | **OVERSTATED** | Low |
| T-M2 `MIN_BLOCKED_RECHECK`'s purpose untested | test | Medium | **OVERSTATED** | Low |
| T-M3 wire numeric inclusive upper bound unpinned | test | Medium | **CONFIRMED** | Medium |
| T-M4 `_ACTION_PARAMS` union check is blind per-action | test | Medium | **CONFIRMED** | Medium |
| T-M5 `disconnect()` flush: 2 of 4 throttles uncovered | test | Medium | **OVERSTATED** | Low |
| T-L1 `wait_for` extractor knows one spelling | test | Low | **CONFIRMED** | Low |
| T-L2 action doc check is one-directional | test | Low | **CONFIRMED** | Low |
| T-L3 battery-flag test seed is not decisive | test | Low | **CONFIRMED** | Low |
| T-L4 hold-time test seed is not decisive | test | Low | **CONFIRMED** | Low |
| T-L5 dead loop in the docs-accuracy JSON extractor | test | Low | **OVERSTATED** | Trivial |

Counts: **CONFIRMED 13 · CONFIRMED-BUT-OVERSTATED 7 · REFUTED 0.**

---

## Verdicts

### Convergence 1 — B-M1 and S-M1 (`json.loads` raises more than `JSONDecodeError`)

Both consequence claims were verified **independently of each other**, and both
hold. They are not duplicates: they are two distinct outcomes of one root cause,
selected by *where in the pump* the poisoned frame lands.

**Root cause, re-derived.**

```
int_max_str_digits = 4300
bigint : ValueError | is JSONDecodeError: False | is ValueError: True | bytes: 4434
   msg: Exceeds the limit (4300 digits) for integer string conversion: value has 4400 di
nested depth 9999 : RecursionError | is ValueError: False | is JSONDecodeError: False
   MRO: ['RecursionError', 'RuntimeError', 'Exception', 'BaseException']
min nesting depth that raises RecursionError here: 9999 -> frame bytes 19998
```

Both frames are brace-balanced and far under `MAX_BUFFER_SIZE = 65536`, so
`FrameScanner` frames them and hands them straight to `_dispatch_frame`.

**Both dispatch paths, both sides, one harness, with a control** (shipped
`PowerPetDoorClient` and `DoorSimulatorProtocol`, a transport counting
pause/resume, a loop exception handler installed):

```
[A] the offending frame ALONE (dispatched inside data_received):
  SIM bigint alone   bytes=  4335 escaped=ValueError     loop_hits=0 backlog=0 paused=False
  SIM nested alone   bytes= 20004 escaped=RecursionError loop_hits=0 backlog=0 paused=False
  CLI bigint alone   bytes=  4335 escaped=ValueError     loop_hits=0 backlog=0 paused=False
  CLI nested alone   bytes= 20004 escaped=RecursionError loop_hits=0 backlog=0 paused=False

[B] 64x {x} + frame + 300x {x} (dispatched from the round-7 call_soon re-arm):
  SIM bigint wedge   bytes=  5427 escaped=None  loop_hits=1 backlog=300 inflight=0 paused=True
      loop ctx: 'Exception in callback FrameDispatcher._resume_pump()' exc=ValueError
  CLI nested wedge   bytes= 21096 escaped=None  loop_hits=1 backlog=300 inflight=0 paused=True
      loop ctx: 'Exception in callback FrameDispatcher._resume_pump()' exc=RecursionError

[C] CONTROL: identical shape with a HARMLESS frame in the middle:
  SIM control        bytes=  1106 escaped=None  loop_hits=0 backlog=45 paused=False
  CLI control        bytes=  1106 escaped=None  loop_hits=0 backlog=45 paused=False
```

The control is what makes the attribution specific: the same 364-frame payload
with one benign frame substituted drains normally and never pauses.

#### B-M1 — escape out of `data_received` → hot reconnect loop — **CONFIRMED (Medium)**

Shipped `PowerPetDoorClient` against my own hostile `asyncio.start_server`
"door" over real TCP, base reconnect 0.2 s, keepalive 0, 5 s of runtime:

```
--- mode=alone keepalive=0.0 over 5.0s
    hostile server saw 22 client connection(s); gaps=[0.251,0.251,0.23,0.231,0.235,0.251,0.215,0.232,0.22,0.228]
    loop exception-handler hits: 22 -> 'Fatal error: protocol.data_received() call failed.'/ValueError
    client.available=False  transport is not None=False  _reconnect_attempts=1
    powerpetdoor log records=110 with traceback=0  throttled 'Failed to decode JSON frame'=0
```

`_reconnect_attempts` is **1** after 22 reconnects: every attempt *connects*
(resetting the counter) and then dies on the first frame, so the exponential
backoff never grows. That is exactly the claim. The throttled count of **0**
confirms the second half — none of this reaches the project's own log
hardening; the traceback comes from asyncio.

#### S-M1 — escape out of `_resume_pump` → permanent wedge, fd and slot leak — **CONFIRMED (Medium)**

**Permanence, in-process, with a control.** After the wedge the transport is
paused, `_inflight` is 0 (no done-callback will fire), `_pump_scheduled` was
cleared *before* `_pump()` raised, and `_update_flow()` was skipped — so nothing
can pump it:

```
=== WEDGE (poisoned) ===
  wedge after ~1 / ~10 / ~100 / ~1000 turns: backlog=300 inflight=0 paused=True pump_scheduled=False resumes=0
  wedge after 0.5s wall:                     backlog=300 paused=True resumes=0
=== CONTROL (harmless frame in the same position) ===
  ctrl  after ~10 turns: backlog=0 inflight=0 paused=False pump_scheduled=False resumes=1
```

(I checked the obvious escape hatch: injecting more frames *does* revive it — but
reading is paused, so in a real transport no more frames can arrive. The wedge
is permanent for exactly the reason claimed.)

**Real `ppd-simulator --daemon`, real sockets, with a healthy control:**

```
baseline: fds=9 RSS=32.7 MB  Clients: none
attacker: 50 conns x 5427 B = 0.271 MB in 0.00s
after   : fds=59 RSS=34.5 MB  Clients: 50 clients
wedged sockets that answered a later valid command: 0/10
healthy control reply: b'{"CMD": "GET_DOOR_STATUS", "success": "true", "dir": "d2p", ...
with healthy   : Clients: 51 clients      <- control connects
healthy gone   : Clients: 50 clients      <- control disconnects cleanly
t+ 1s / 5s / 15s / 30s after attacker CLOSED: fds=59 RSS=34.5 MB  Clients: 50 clients
daemon log bytes: 170604 ('Exception in callback' 50, 'Traceback' 50, 'Exceeds the limit' 50)
```

170,604 log bytes for 271,350 attacker bytes (×0.63) — two orders of magnitude
above the ×0.005–0.04 the throttled sites achieve. fds and connection slots are
held after the attacker walks away; the healthy control proves the counter
tracks normal traffic exactly.

**Client-side wedge, and the keepalive recovery claim.** Over real TCP:

```
--- mode=wedge keepalive=0.0 over 5.0s
    hostile server saw 1 connection; loop hits: 1 ('Exception in callback FrameDispatcher._resume_pump()')
    dispatcher: backlog=300 inflight=0 paused=True
    client.available=True  transport is not None=True     <- silently deaf
--- mode=control keepalive=0.0 over 5.0s
    loop hits: 0; backlog=0; paused=False; available=True
```

With keepalive on, it does self-heal and re-wedge, as claimed:

```
t+ 5s conns=1[0.0]            available=True backlog=300 paused=True failed_pings=1
t+10s conns=1[0.0]            available=True backlog=300 paused=True failed_pings=2
t+20s conns=2[0.0, 12.6]      available=True backlog=300 paused=True
t+30s conns=3[0.0, 12.6, 25.1] available=True backlog=300 paused=True
```

At keepalive=1.0 the cycle is ~12.5 s; at the `PowerPetDoor` default of 30 s
with `MAX_FAILED_PINGS = 3` it is ~90 s. Deaf-while-`available`-is-`True`
between recoveries is real.

**Is `except Exception` the right fix, and does it narrow anything we accept?**
No, it narrows nothing — and I verified that by building the fix and running it,
rather than reasoning about it.

- The `try` in both `_dispatch_frame` implementations wraps **only**
  `json.loads(frame)`; `self._track_task(self.process_message(msg))` is outside
  it. So a wider `except` cannot swallow a handler error.
- `asyncio.CancelledError` and `KeyboardInterrupt` are `BaseException`, so
  `except Exception` does not catch them.
- Backend's narrower `except (ValueError, RecursionError)` is sufficient and
  more legible; `except Exception` is also safe here but would additionally
  swallow `MemoryError`. I would take the narrow form.

With `except (ValueError, RecursionError)` applied to both sites:

```
[A] SIM/CLI bigint & nested alone : escaped=None  loop_hits=0 backlog=0 paused=False
[B] SIM/CLI bigint & nested wedge : escaped=None  loop_hits=0 backlog=109 paused=False (draining)
```

and on the real daemon, the same 50×5,427-byte attack:

```
after   : fds=59 Clients: 50 clients
wedged sockets that answered a later valid command: 10/10   (was 0/10)
t+ 1s..30s after attacker CLOSED: fds=9  Clients: none      (was fds=59, 50 clients forever)
'Exception in callback': 0 | 'Traceback': 0 | 'Exceeds the limit': 0
```

The frames land on the **existing** throttled path with the **existing**
doubling schedule — byte-identical treatment to `{x}`:

```
bigint (fixed path): 200 frames, 864200 wire B -> 16 records, 3710 log B, x0.004
    1 x Failed to decode 1/2/4/8/16/32/64/128 JSON frame(s)...  + 8 x detail
{x} control        : 200 frames,    600 wire B -> 16 records, 1477 log B, x2.462
    (identical record shape)
```

A legitimate frame after the poisoned burst still decodes and is handled.
Full suite on the fixed tree: **2620 passed**; `ruff check src` clean.

**Wire check: passes.** It changes no byte we send. It accepts strictly *more*
input without crashing — every frame that decodes today still decodes, and the
two that used to kill the callback now take the skip path that a malformed frame
already took. Nothing is refused that was not already refused.

**On security's recommendation 2** (defence in depth in `_pump`): moving
`self._update_flow()` into a `finally` is cheap, correct, and I would take it —
it is what turns "wedged forever" into "one frame lost" for any future raising
callback (`asyncio.create_task` on a closing loop is the remaining candidate).
Wrapping `self._dispatch(...)` in a swallowing `except` is more debatable and
should not be a drive-by; the `finally` alone removes the wedge state.

**Recommendation 3** (a property test feeding arbitrary *bytes* to
`data_received`) is correct in substance but the report's supporting sentence is
slightly wrong: `tests/fuzz/test_framing_fuzz.py:226` and `:249` **do** feed raw
bytes to `client.data_received`. They draw `_json_objects` (well-formed dicts
serialised with `json.dumps`) and `_garbage` explicitly excludes `{`, so no
generated frame can ever fail to decode. The gap is real; "the suite does not
have that test" should read "the suite has that test and its strategy cannot
produce a decode failure."

**Severity.** Medium for both. Remotely reachable, no privilege, no interaction,
no malformed framing, two documented "never raises" contracts falsified, a
documented public property (`available`) made to lie, and permanent fd/slot
leakage — but availability only, no confidentiality/integrity/state impact, and
the shipped client self-heals under the default keepalive. Security's own
"why not High" reasoning is sound and I adopt it.

### Convergence 2 — B-L1 and S-L2 (`_keep_int` type-checks but does not range-check) — **CONFIRMED (Low)**, recommendations corrected

Re-derived end-to-end through a real connected `PowerPetDoor` over a real socket
(hostile device answering `GET_HOLD_TIME` with `1` followed by 400 zeros; logger
at WARNING, i.e. a production level):

```
minimum exponent whose /100.0 overflows: 309 -> digits: 310
_keep_int returns it unchanged (no rejection): True
  v/100.0 -> OverflowError int too large to convert to float
door.hold_time after connect: 2.0
  50 frames (22650 wire bytes) -> WARNING+ records 50, with traceback 50 ['OverflowError']
  log bytes 21550 -> x0.95 amplification
  door.hold_time is still 2.0 (silently stale)
```

Genuine defect, real supported path, and the helper's own comment shows the case
was considered and mis-resolved ("an arbitrarily large integer never reaches
`math.isfinite` (which would overflow)" — true, and one step short of the
consumer that divides by `100.0`). Low is right: it needs a 310-digit `holdTime`
from a device, and ×0.95 amplification buys an attacker nothing.

**Three corrections to the reports.**

1. **Security's framing "a regression introduced by the round-7 fix" is
   disproven.** At `a0194bd` (pre-round-7) the method read:

   ```python
   def _on_hold_time_update(self, value: int) -> None:
       """Handle hold time update (value is in centiseconds)."""
       self._hold_time = value / 100.0
   ```

   which raises the identical `OverflowError`. This is a **pre-existing gap the
   round-7 guard did not close**, not a regression it introduced. Backend's
   framing ("a residual gap in a guard") is the accurate one.

2. **Security's recommended constant is rejected on wire and layering grounds.**
   `MAX_HOLD_TIME_CENTISECONDS = 90000` is defined at
   `src/powerpetdoor/simulator/protocol.py:151` and **nowhere else**. Applying
   it in `door.py` would (a) make the shipped facade silently refuse any device
   hold time above 900 s on the authority of a reverse-engineered simulator
   constant — narrowing what we accept from a real device — and (b) be the first
   `simulator/` import into the core library (`door.py` imports only `.client`,
   `.const`, `.sanitize`, `.schedule`; no non-simulator module imports
   `simulator` at all). **Do not implement this form.**

3. **Backend's recommendation as written breaks a shipped pinned test.**
   Bounding *inside* `_keep_int` at `±sys.float_info.max` for all callers
   contradicts `tests/test_door.py:2122`
   (`test_a_huge_integer_percent_does_not_overflow_the_guard`), which
   deliberately asserts `door.battery_percent == 10**400`. The fix must be an
   **optional** bound (or a `_keep_centiseconds` sibling) applied only at
   `_on_hold_time_update`, and the bound must be a float-representability limit
   — not a protocol value.

### F-M1 — every listing and every Tab re-parses every script, on the simulator's own loop — **CONFIRMED BUT OVERSTATED (Medium → Low)**

The mechanism and every measurement reproduce, on an **idle** machine (load
1.17; the report's numbers were taken at load 7–11, which is why mine are
smaller).

**Cost is linear in the directory and independent of the typed prefix**
(in-process, median of 5, my own 200-script × 30-step corpus):

```
  no --scripts-dir (7 built-ins)  completer('s000') median=  13.2 ms candidates=  7 | completer('') median=  13.2 ms
  10 scripts                      completer('s000') median=  48.5 ms candidates= 17 | completer('') median=  50.0 ms
  25 scripts                      completer('s000') median= 101.2 ms candidates= 32 | completer('') median= 101.1 ms
  50 scripts                      completer('s000') median= 187.8 ms candidates= 57 | completer('') median= 188.7 ms
  100 scripts                     completer('s000') median= 363.4 ms candidates=107 | completer('') median= 366.3 ms
  200 scripts                     completer('s000') median= 722.1 ms candidates=207 | completer('') median= 721.2 ms
  render_script_listing(200)      median=720.6 ms
  Script.from_file on one 2335B script: median=3.43 ms
  candidates matching prefix 's000': 1 of 207
```

**`ctl list` blocks the door protocol server, with two controls in the same
harness** (real `--daemon`, a real TCP socket doing continuous
`GET_DOOR_STATUS` round trips):

```
  200 scripts / ctl list:              baseline n=293 max=0.3ms med=0.1ms
     `ctl list` wall=878ms lines=210
     door RTT during/after n=107 max=727.4ms
  200 scripts / ctl status (CONTROL):  baseline max=0.4ms | `ctl status` wall=160ms | door RTT max=0.4ms
  no scripts-dir / ctl list (CONTROL): baseline max=0.4ms | `ctl list` wall=154ms  | door RTT max=10.9ms
```

**One Tab keystroke, real PTY, real `ppd-simulator`, with a control:**

```
  200 scripts        : baseline n=293 max=1.2ms | after TAB n=345 max=727.6ms
  no --scripts-dir (control): baseline n=292 max=1.2ms | after TAB n=484 max=20.1ms
```

The two controls together are decisive: the stall is not ctl's process cost
(`ctl status` is 0.4 ms of door RTT on the same daemon) and not `list` itself
(10.9 ms with no scripts-dir). `SimulatorCompleter()` really is installed
unwrapped at `prompt_common.py:573` and the prompt really is a task on the
door server's loop (`asyncio.run(run_simulator(...))`).

**Why I am downgrading.** The demonstrated worst case is a **sub-second
hiccup** on a documented development tool, requiring (a) a user directory of
100+ scripts, (b) an operator keystroke or `list`, and it causes (c) no data
loss, no state corruption, no false PASS. 727 ms is well inside the shipped
client's own 10 s timeout and 30 s keepalive, so no library consumer connected
during the stall would actually fail. Compare round 7's frontend Mediums, both
of which the refuter kept because they *corrupted state* (`holdtime nan`) or
*produced a green CI PASS for a script that tested nothing* (unknown `sensor:`).
This does neither. It should absolutely be fixed — the prefix filter is one
`continue` and turns 722 ms into ~20 ms — but Medium overstates it next to the
two remotely-reachable Mediums in this same round.

**Implementation caveats for the fix list.** The downstream filter at
`prompt_common.py:498` is `name.lower().startswith(word_before.lower())`, so a
pre-filter inside `script_completer` **must be case-insensitive** or it changes
completion behaviour for uppercase input. Recommendation 3 (read only the
top-level mapping instead of a full `Script.from_file`) changes what the listing
validates: today a malformed steps list renders `(Error loading: ...)`, and
round 7 verified terminal-safety parity on exactly that path. Do not lose it.

Recommendations 1, 2 and 4 pass the wire check trivially (listing/completion
rendering and completer threading only).

### F-L1 — the daemon-mode docs quote three strings the round-7 fixes changed — **CONFIRMED (Low)**

Against running binaries (daemon on 39670/39671, `--scripts-dir` with a symlink
out of the directory and a `basic_cycle.yaml` shadowing the built-in):

```
$ ctl run linked
ERROR: Script 'linked' resolves outside /tmp/r8fe/sd and cannot be run by name;
       move it into the directory (paths are not accepted over the control channel)
$ ppd-simulator --scripts-dir /tmp/r8fe/sd --list-scripts | grep -i shadow
  basic_cycle: ... (shadowed by /tmp/r8fe/sd/basic_cycle.yaml)
$ ctl list | grep -i shadow
  basic_cycle: ... (shadowed by /tmp/r8fe/sd/basic_cycle.yaml)
```

versus `docs/simulator.md:321-324`, which says `...; move it into the directory
or run it by path`, "`list` marks the built-in", and `(shadowed by
<dir>/<name>)`. All three diverge. The first is the worst because the paragraph
is *the control-channel documentation* and the sentence it quotes is now the
local-CLI variant — round 7's F-L6 contradiction moved from the product into the
manual. Docs-only fix. Low stands.

### F-L2 — "Settings that can be used with `set` and `toggle`": 2 of 9 rows fail `toggle` — **CONFIRMED (Low)**

One script per documented row per action, each through
`ppd-simulator --port 0 --scripts-dir ... --script X --oneshot`:

```
=== toggle ===                          === set ===
power       rc=0 PASSED                 power       rc=0 PASSED
auto        rc=0 PASSED                 auto        rc=0 PASSED
inside      rc=0 PASSED                 inside      rc=0 PASSED
outside     rc=0 PASSED                 outside     rc=0 PASSED
autoretract rc=0 PASSED                 autoretract rc=0 PASSED
safety_lock rc=0 PASSED                 safety_lock rc=0 PASSED
cmd_lockout rc=0 PASSED                 cmd_lockout rc=0 PASSED
hold_time   rc=1 Unknown setting to toggle: hold_time    hold_time rc=0 PASSED
battery     rc=1 Unknown setting to toggle: battery      battery   rc=0 PASSED
```

The two failing rows are exactly the two non-boolean ones, the table's own
`Type` column already says so, and the `**toggle**` action entry two sections
up correctly says "Toggle a boolean setting". Round-7 F-L1 one screen away, and
none of the three docs-accuracy tests round 7 added covers this table. Low.

### F-L3 — round 7's fixes break existing user scripts; `CHANGELOG.md` untouched — **CONFIRMED (Low)**

Verified by running one ordinary annotated script against both revisions, with
an import guard on each:

```
=== tree /tmp/r8prev (a0194bd) ===
[guard] /tmp/r8prev/src/powerpetdoor/__init__.py
rc=0   Step 2: wait(seconds=1, note=let the door settle)
       >>> Script PASSED: Annotated user script   >>> All scripts PASSED

=== tree /tmp/r8ref (f8797e0) ===
[guard] /tmp/r8ref/src/powerpetdoor/__init__.py
rc=1   Step 2: wait(seconds=1, note=let the door settle)
       [ERROR] Script error at step 2: Unknown parameter(s) for wait: note. Use: seconds
       >>> Script FAILED: Annotated user script   >>> All scripts FAILED
```

```
$ git log -1 --format='%h %ad %s' --date=short -- CHANGELOG.md
a0194bd 2026-08-22 Round 6 fixes; revert the enabled wire change; layer the wire boundary
$ git show --stat da31ae2 -- CHANGELOG.md     # no file stat: untouched
$ git log --oneline a0194bd..da31ae2
da31ae2 Round 7 fixes (refuter-approved list only)
9e61383 Add round 7 adversarial refutation pass
4958f2b Add persona analysis round 7 reports (execution-proven findings only)
```

The strictness itself is correct and refuter-approved; the finding is purely
about the upgrade path. `CHANGELOG.md` opens with "All notable changes to this
project will be documented in this file" and carries a detailed 351-line
`[Unreleased]` section, so the omission is a break with the project's own
practice. Low.

### F-L4 — three "valid values" spellings; five bare `Unknown X` messages — **CONFIRMED BUT OVERSTATED (Low, half discarded)**

Both halves reproduce exactly:

```
e_action    Unknown action: frobnicate
e_setting   Unknown setting: powr
e_toggle    Unknown setting to toggle: powr
e_assert    Unknown assertion condition: door_closed
e_cond      Unknown condition: door_stat
e_sensor    Unknown sensor: insde. Use: inside, outside
e_param     Unknown parameter(s) for wait: duration. Use: seconds
e_noparam   Unknown parameter(s) for close: delay. Use: none

$ ctl schedule bogus   -> Available: add, clear, days, delete, disable, enable, list, time
$ ctl broadcast bogus  -> Available: all, battery, hwinfo, notifications, schedules, settings, stats, status
$ ctl notify bogus     -> Available: inside_off, inside_on, low_battery, outside_off, outside_on
$ ctl schedule add sideways ... -> 'sideways' is not valid. Choose from: inside, outside, both
$ ctl stop bogus       -> 'bogus' is not valid. Choose from: all
```

**What survives:** the five bare messages. Every one of them has the accepted
set as a literal in the same function, the DSL is the **CI** front end (a build
log with no terminal to experiment in), and `Unknown assertion condition:
door_closed` is specifically a name the runner recognises *for the other action*
— exactly the trap round-7 F-L1 described and fixed docs-only. Concrete, cheap,
proven.

**What I discard:** "pick one spelling and route all three families through one
helper." The three spellings sit in three different contexts — subcommand
enumeration (`Available:`), `ArgSpec` choice validation (`Choose from:`), and
DSL parameter/sensor sets (`Use:`). No user confusion was demonstrated, no
documented intent to unify exists, and the change would rewrite operator strings
that `tests/simulator/test_commands.py` currently pins, for a purely stylistic
gain. `Use: none` for a no-parameter action is genuinely odd and worth
rewording on its own; the rest is churn.

### T-H1 — `exclude_lines` over-matches prose — **CONFIRMED BUT OVERSTATED (High → Medium); recommendation 1 REFUTED**

Note first: the report's Step 1 reads a "committed `coverage.json`". `coverage.json`
is **not tracked** (`git ls-files | grep -i coverage` returns nothing) — it is a
local artefact. I regenerated my own.

**The exclusions are real.** My own `--ignore=tests/fuzz --cov --cov-report=json`
run on the guarded copy:

```
scripts/generate_gaps_report.py: excluded_lines=[33, 79, 376, 407, 420, 421]
  gaps:33 : '_EXCLUSION_NOTES = {'
  gaps:79 : '"""Map line number -> (column, text) for every real comment in ``source``.'
  gaps:376: '        lines.append('
  gaps:407: '        lines.append("No `# pragma: no cover` or `# pragma: no branch` annotations found.")'
  gaps:420: 'if __name__ == "__main__":'      <- intended
  gaps:421: '    sys.exit(main())'            <- intended
```

Eight prose lines match, none of them a comment (verified by `tokenize`, not by
eyeballing):

```
'pragma: no cover'          gaps:34  [PROSE] "pragma: no cover": "Explicitly annotated lines ..."
'pragma: no cover'          gaps:83  [PROSE] literal words "# pragma: no cover" inside string literals, and a raw
'pragma: no cover'          gaps:379 [PROSE] f"are excluded via `# pragma: no cover` or `# pragma: no branch`."
'pragma: no cover'          gaps:407 [PROSE] lines.append("No `# pragma: no cover` or ...")
'def __repr__'              gaps:35  [PROSE] "def __repr__": "String representation methods",
'raise NotImplementedError' gaps:36  [PROSE] "raise NotImplementedError": "Abstract method stubs",
'if TYPE_CHECKING:'         gaps:37  [PROSE] "if TYPE_CHECKING:": "Type-checking-only imports",
'if __name__ == .__main__.:' gaps:38 [PROSE] "if __name__ == .__main__.:": "Script entry-point guards",
'@overload'                 gaps:39  [PROSE] "@overload": "Typing overload declarations",
'pragma: no cover'          ctl.py:657 [COMMENT]   <- the only genuine hit
```

**The gate is porous — matched-pair proof, my own mutants.** Two copies of the
tree, each with one never-called function appended to
`scripts/generate_gaps_report.py`, differing only in whether the returned
string contains the phrase:

```
holeA (return "...mentions pragma: no cover in a string...")
  scripts/generate_gaps_report.py  236  0  76  0  100.00%
  TOTAL                           6663  0 2368  0  100.00%   Required test coverage of 100.0% reached
  2576 passed

holeB (byte-identical control, phrase removed)
  scripts/generate_gaps_report.py  237  1  76  0   99.68%   426
  TOTAL                           6664  1 2368  0   99.99%
  FAIL Required test coverage of 100.0% not reached.
```

`scripts/` is in `coverage.run.source` (`pyproject.toml:108`) and the real gate
is `coverage report --fail-under=100` (`.github/workflows/test.yml:203-204`).
`tests/TESTING_GAPS.md:49` reports "**3 lines** across **2 files** in **3
annotations**" — pragma comments only; the three prose-triggered statement
exclusions are disclosed nowhere, and the file reports `generate_gaps_report.py`
at 100.0%.

**Recommendation 1 is refuted by execution.** I built the report's exact fix —
`"pragma: no cover"` replaced by `"#\\s*pragma:\\s*no\\s+cover"` — and ran the
full coverage gate:

```
BASELINE             num_statements=235 excluded=[33, 79, 376, 407, 420, 421]  TOTAL 6662
ANCHORED-pragma-only num_statements=235 excluded=[33, 79, 376, 407, 420, 421]  TOTAL 6662
lines newly measured after anchoring: []
```

**Zero change.** Lines 83, 379 and 407 literally contain `` `# pragma: no
cover` `` in markdown backticks, so the anchored regex still matches them; line
33 stays excluded because lines 35–39 still match the four *other* bare
patterns, which recommendation 1 anchors only to `^\s*` (which does not help a
dict-value line). The report's own Step-3 output shows `TOTAL 6662` — identical
to baseline — and reads it as "removing the over-match does not break the gate"
when it actually means "the over-match was not removed."

**A pattern that does work**, matched against the three real pragmas and the
four prose lines:

```
PATTERN            | ctl:657 | cli:100 | cli:689 | gaps:34 | gaps:83 | gaps:379 | gaps:407
current            |    Y    |    .    |    .    |    Y    |    Y    |    Y     |    Y
TF-proposed        |    Y    |    .    |    .    |    .    |    Y    |    Y     |    Y
anchored + delim   |    Y    |    Y    |    Y    |    .    |    .    |    .     |    .
```

(`#\s*pragma:\s*no\s+(cover|branch)\s*($|\()` — note the `branch` alternative
belongs in `partial_branches`, not `exclude_lines`; a cover-only variant
`#\s*pragma:\s*no\s+cover\s*($|\()` has the same discrimination and does not
disturb `cli.py:100/689`.) With that pattern plus `^\s*`-anchored structural
patterns, the three statements return and are all executed:

```
properfix excluded: [418, 419, 420, 421]  stmts 235 -> 238
newly measured & EXECUTED: [33, 376, 407]
   33 : _EXCLUSION_NOTES = {
   376: lines.append(
   407: lines.append("No `# pragma: no cover` or `# pragma: no branch` annotations found."
missing after properfix (repo-wide): {}   Required test coverage of 100.0% reached.  2576 passed
```

So the answer to "prove they are excluded, prove they are unexecuted" is: **3
executable statements are provably excluded; all 3 are in fact executed today**
(line 79 is a docstring line, not a statement — `num_statements` rises by 3, not
4). The live damage today is therefore zero. What is real is that the gate
demonstrably admits arbitrary *new* dead code (holeA/holeB), unbounded going
forward, in a repo whose 100% gate is its central quality control, for the third
round running.

**Why Medium and not High.** Nothing in the shipped library is affected; no
security, user-visible or correctness impact exists today; the three excluded
statements are covered anyway; the whole blast radius is one non-shipped
developer script. High in this project's scale has meant something worse than
that. Medium is right — the gate is porous, it is a third recurrence, and it
should be fixed promptly, but "3 currently-executed statements in a dev script
are unmeasured" is not a High.

Recommendation **2** (a test that sweeps every configured pattern over every
gated file and asserts each match is a real comment/structural line, via
`gaps._comment_tokens`, which already exists) is the right fix and is the one
that would have caught all three instances. Recommendation **3** is a good
addition. Recommendation **1** must be replaced with a pattern actually verified
against `generate_gaps_report.py`'s prose.

### T-M1 — the "only one continuation" guard is unobserved — **CONFIRMED BUT OVERSTATED (Medium → Low)**

Mutation (delete the dedupe guard, keep the flag write): **SURVIVED**,
`2620 passed`.

My own continuation counter (wrapping `loop.call_soon` and counting
`_resume_pump` arms) in exactly the scenario the test sets up:

```
--- ORIGINAL (/tmp/r8ref) ---
  continuations armed after two submits: 1  (flag after 1st=True, after 2nd=True, at end=False)
  dispatched: ['{1}','{2}','{3}','{4}','{5}','{6}']
  continuations armed by 1000 stalled reads: 1
--- MUTANT (/tmp/r8b/tm1) ---
  continuations armed after two submits: 2  (flag after 1st=True, after 2nd=True, at end=False)
  dispatched: ['{1}','{2}','{3}','{4}','{5}','{6}']
  continuations armed by 1000 stalled reads: 1000
```

All three assertions in `test_only_one_continuation_is_ever_scheduled`
(`tests/test_framing.py:1152`) hold identically at 1 and at 1000. The docstring
("must not stack call_soons") and the inline comment ("not arm a second one")
state a contract the assertions cannot reach. Real, and the one-line fix (count
the arms) is right.

**Why Low.** Round 7's refuter established the rule for exactly this shape:
*"the specific behaviour it is named for is unpinned... That is exactly the same
shape as T-L1/T-L4/T-L5/T-L6, all of which were filed Low."* This is a single
missing assertion on a four-line guard that is correct today, in a
neighbourhood the same report shows is densely mutation-covered (`m01`–`m07`
all caught). No production consumer is affected.

### T-M2 — `MIN_BLOCKED_RECHECK`'s purpose is untested — **CONFIRMED BUT OVERSTATED (Medium → Low)**

Mutation (drop the `max()` floor): **SURVIVED**, `2620 passed`. Control
(`MIN_BLOCKED_RECHECK 0.1 -> 1.0`): **CAUGHT**
(`test_the_blocked_recheck_floor_is_100ms`). So the value is pinned and the
behaviour is not, as claimed.

The busy spin, measured with **no instrumentation on the hot path** (my first
attempt wrapped `_wait_for_wake` and under-reported CPU by 30×; my second used a
self-rescheduling `call_soon` tick that itself burned a core — both discarded.
This is the clean run):

```
--- ORIGINAL ---
  hold_time=0.0 : status=DOOR_HOLDING wall 1.00s CPU   0.9 ms =  0.1% of a core
  hold_time=0.05: ...                                  0.8 ms =  0.1%
  hold_time=0.1 : ...                                  0.8 ms =  0.1%
  hold_time=2.0 : ...                                  0.2 ms =  0.0%
--- MUTANT (floor removed) ---
  hold_time=0.0 : status=DOOR_HOLDING wall 1.00s CPU 717.2 ms = 71.7% of a core
  hold_time=0.05: ...                                  1.0 ms =  0.1%
  hold_time=0.1 : ...                                  0.8 ms =  0.1%
  hold_time=2.0 : ...                                  0.2 ms =  0.0%
```

One narrowing worth recording: only `hold_time == 0.0` spins. `0.05` — below
the floor — does not, because `asyncio.timeout(0.05)` still waits. The report's
"near-zero hold_time" is really "exactly zero", which the wire coercer does
permit (`_coerce_wire_number(..., 0, MAX_HOLD_TIME_CENTISECONDS)`), so the case
is reachable.

**Why Low.** Same class as T-M1: one missing test on a guard that is correct,
in the simulator (not the shipped library), whose failure mode if regressed is
loud (72% of a core) rather than silent.

### T-M3 — the wire numeric validator's inclusive upper bound is unpinned — **CONFIRMED (Medium)**

Mutation (`<=` → `<` on the upper bound): **SURVIVED**, `2620 passed`. Control
(`<=` → `<` on the *lower* bound): **CAUGHT**, 4 failures. So this is a specific
gap, not a property of the technique.

Non-equivalence over a **real socket to a real started `DoorSimulator`**, at
`limit-1 / limit / limit+1` on all three shipped constants:

```
--- ORIGINAL ---                              --- MUTANT (<= -> <) ---
  holdTime=89999  -> ACCEPTED                   holdTime=89999  -> ACCEPTED
  holdTime=90000  -> ACCEPTED                   holdTime=90000  -> REJECTED: holdTime must be between 0 and 90000, got 90000
  holdTime=90001  -> REJECTED (correct)         holdTime=90001  -> REJECTED
  sensorTriggerVoltage=65534 -> ACCEPTED        65534 -> ACCEPTED
  sensorTriggerVoltage=65535 -> ACCEPTED        65535 -> REJECTED: ... between 0 and 65535, got 65535
  sensorTriggerVoltage=65536 -> REJECTED        65536 -> REJECTED
  index=254 -> REJECTED: Schedule not found     index=254 -> REJECTED: Schedule not found
  index=255 -> REJECTED: Schedule not found     index=255 -> REJECTED: index must be between 0 and 255, got 255
  index=256 -> REJECTED: index must be ...      index=256 -> REJECTED: index must be ...
```

The self-contradicting message ("must be between 0 and 90000, got 90000") is
the substance, and the whole suite is green.

**Why Medium stands.** This is the untrusted-input layer, three separate shipped
constants across four wire call sites, and the pin required is per-constant. Its
string sibling (`_coerce_wire_string`'s `len > max_length`) and the CLI float
`max_value` *were* pinned by round 7's boundary sweep; this one adjacent site
was missed. That is the same aggregate-boundary class round 7's refuter kept at
Medium, and it is precisely what CLAUDE.md rule 8 names. Note the fix pins what
the simulator accepts **today** — it does not change or narrow the wire
contract.

### T-M4 — `_ACTION_PARAMS` can grow a parameter its action never reads — **CONFIRMED (Medium)**

Mutation (`"open": frozenset({"hold", "duration"})`): **SURVIVED**,
`2620 passed`. User-visible consequence, executed with an import guard on each
tree:

```
--- ORIGINAL ---                              --- MUTANT ---
_ACTION_PARAMS["open"] = ['hold']             _ACTION_PARAMS["open"] = ['duration', 'hold']
>>> Script FAILED: Typo demo                  >>> Script PASSED: Typo demo
```

**The blindness is larger than the report claimed**, and it is a *present-day*
hole, not a hypothetical:

```
actions: 19
parameter names shared by >1 action: {'sensor': 2, 'duration': 2, 'condition': 2, 'name': 2, 'value': 2, 'index': 2}
actions with at least one shared parameter: 11 of 19
flattened declared set the guard test checks against:
  ['condition','duration','enabled','equals','hold','index','message','name','percent','seconds','sensor','timeout','value']
```

`test_every_declared_parameter_is_actually_read` asserts
`set().union(*_ACTION_PARAMS.values()) - read == set()` where `read` is scraped
from the *whole* `_execute_step` body. Any of those 13 names added to any of the
19 actions is undetected **today** — the check is structurally incapable of
noticing, not merely untested against one mutation.

**Why Medium stands.** The failure mode it protects against is round-7 frontend
L3/M2 restored: the progress log echoes the typo back as accepted, the parameter
does nothing, and `ctl run <name> wait` exits 0 — a green CI result for a script
that tested nothing. That is the one thing a simulator must never produce, and
the guard is presently porous for 11 of 19 actions.

### T-M5 — `disconnect()` flushes four throttles; two are uncovered — **CONFIRMED BUT OVERSTATED (Medium → Low)**

Mutations: `_device_errors` removed from the flush tuple **SURVIVED**;
`_bad_messages` removed **SURVIVED**; control `_non_ascii` removed **CAUGHT**
(`test_disconnect_reports_the_non_ascii_total_and_resets`).

Consequence, executed — three device-error frames, then `disconnect()`:

```
--- ORIGINAL ---
  device-error count before disconnect: 3 | records so far: 8
  tail summary emitted by disconnect: ['Device reported 3 error response(s) (183 bytes) on this connection']
  device-error count after disconnect (0 == reset for the next connection): 0
--- MUTANT ---
  device-error count before disconnect: 3 | records so far: 8
  tail summary emitted by disconnect: []
  device-error count after disconnect (0 == reset for the next connection): 3
```

Real, and the parametrized fix is right.

**Why Low.** Two missing assertions on a four-element tuple whose other two
elements *are* covered and whose reporting half is separately mutation-covered
(`m54`, `m56` both caught). The code is correct today; nothing is affected. Same
class as T-M1/T-M2 and as the six items round 7 filed Low.

### T-L1 — the `wait_for` doc extractor knows one of two spellings — **CONFIRMED (Low)**

Mutation (add `elif condition in ("door_ajar", "door_stuck"): return False`):
**SURVIVED**, `2620 passed`.

```
--- ORIGINAL ---
  shipped wait_for extractor (== only): 20 names
  'condition in (...)' names present in _check_condition: []
  _check_condition('door_ajar') raised: ScriptError Unknown condition: door_ajar
--- MUTANT ---
  'condition in (...)' names present in _check_condition: ['door_ajar']
  extractor sees door_ajar? False
  _check_condition('door_ajar') -> False        <- live, and invisible to the test
```

The sibling `test_the_assert_condition_table_matches_the_implementation`
(`tests/test_docs_accuracy.py:582`) already handles both spellings; the
asymmetry is itself the evidence both are expected. Low.

### T-L2 — the action doc check is one-directional — **CONFIRMED (Low)**

Mutation (invent `**totally_fake**` in `docs/simulator.md`): **SURVIVED**,
`2620 passed`. The tightened assertion is verified free on the current tree:

```
documented: 19 implemented: 19
implemented - documented: []      documented - implemented: []
assertion as shipped (impl - doc == set()): True
tightened assertion (doc == impl) would hold today: True
```

One-character change, no doc or code change required. Low.

### T-L3 — the battery-flag guard test seeds a value that is not decisive — **CONFIRMED (Low)**

Mutation (`_keep_bool` coerces ints): **SURVIVED**, `2620 passed`. Matched
decisive/non-decisive pair, run against both trees:

```
--- PRISTINE ---                                --- MUTANT (_keep_bool coerces) ---
  L3 decisive  (cached False, incoming 1): False -> PASS   L3 decisive : True  -> FAIL
  L3 as-shipped (cached True,  incoming 1): True -> PASS   L3 as-shipped: True -> PASS
```

`tests/test_door.py:2136` seeds `present=True, ac_present=True` and parametrizes
`1`; `bool(1)` is `True`, so "kept the cache" and "coerced the int" give the same
answer. The decisive version passes on pristine and fails on the mutant.
CLAUDE.md rules 8/9. Impact is limited (the client pre-coerces with `make_bool`),
which is why Low is right.

### T-L4 — the hold-time guard test seeds the one exact float — **CONFIRMED (Low)**

Mutation (`round()` → `int()` in the cached fallback): **SURVIVED**,
`2620 passed`.

```
centisecond values where int(c/100.0*100) != c: 4586 of 90001 = 5.1 %
first 10: [29, 57, 58, 113, 114, 115, 116, 201, 203, 205]
0.29 -> 28.999999999999996  int 28  round 29
4.00 -> 400.0               int 400 round 400     <- the value the test seeds
```

```
--- PRISTINE ---                          --- MUTANT (round -> int) ---
  L4 decisive  (cached 0.29): 0.29 -> PASS   L4 decisive : 0.28 -> FAIL
  L4 as-shipped (cached 4.0):  4.0 -> PASS   L4 as-shipped: 4.0 -> PASS
```

`_hold_time` is populated as `centiseconds / 100.0` from the wire, so 5.1% of
the values a device can legally send would silently rewrite the cache under the
mutant — against a listener whose entire contract is that a rejected value
leaves the cache untouched. One extra parameter on an existing test. Low.

### T-L5 — dead loop in the docs-accuracy JSON extractor — **CONFIRMED BUT OVERSTATED (Low → Trivial)**

Mutation (delete the inner loop): **SURVIVED**, `2620 passed`. Equivalence
proven by execution rather than by reading, on the section the helper actually
feeds:

```
keepalive section blocks: 2  identical with-loop vs without-loop: True
does anything below the loop read `line`? False
```

The loop binds a local, breaks, and discards it. It is genuinely inert and
deleting it is a correct cleanup.

**Why Trivial, and one methodological note.** This is an **equivalent mutant**:
the surviving mutation is *expected* and is therefore not evidence of a test
gap, which is how the finding presents it. What remains is five lines of inert
code in a test helper — no shipped code, no coverage consequence, no behaviour.
That is Trivial on this project's scale, and it belongs in a cosmetics pass.

---

## Recommended Fix List

Survivors only, in the order I would fix them. **Nothing here changes a byte we
send to the device or narrows what we accept from it** — the two
recommendations that would have are called out under Discarded.

**Code — correctness, do first**

1. **B-M1 / S-M1** — widen the `except` in both `_dispatch_frame`
   implementations (`client.py:1727`, `simulator/protocol.py:462`) to
   `except (ValueError, RecursionError) as err:`, keeping the existing throttled
   `_bad_frames` path and `return None`. Verified end to end: escape gone, wedge
   gone, daemon fds and `Clients:` return to baseline, 0 tracebacks, frames land
   on the identical doubling schedule (×0.004), full suite `2620 passed`, ruff
   clean. Add a regression test pinning both triggers (a >4300-digit integer
   literal and a ≥9999-deep nesting, the latter delivered in 1400-byte pieces so
   the 64 KiB cap is provably not what catches it) next to the existing DoS
   bound tests.
2. **B-M1 / S-M1, defence in depth** — move `self._update_flow()` into a
   `finally` in `FrameDispatcher._pump()`. Cheap, correct, and it is what turns
   "wedged forever" into "one frame lost" for any future raising callback.
   Wrapping `self._dispatch(...)` in a swallowing `except` is a separate
   judgement call; do not fold it in.
3. **B-L1 / S-L2** — give `_keep_int` an **optional** maximum (or add a
   `_keep_centiseconds` sibling) and apply it **only** at
   `_on_hold_time_update`. The bound must be a float-representability limit
   (`sys.float_info.max`), **not** `MAX_HOLD_TIME_CENTISECONDS`. Do not apply it
   globally inside `_keep_int` — `tests/test_door.py:2122` deliberately pins
   `battery_percent == 10**400`.
4. **T-H1** — fix the coverage config with a pattern **verified against
   `scripts/generate_gaps_report.py`'s prose**, not the one in the report.
   `#\s*pragma:\s*no\s+cover\s*($|\()` matches `ctl.py:657` and none of
   `gaps:34/83/379/407`; anchor the five structural patterns to `^\s*`. Then add
   recommendation 2's test (sweep every configured pattern over every gated
   file; assert each match is a real comment token via `gaps._comment_tokens`,
   or a genuine structural line). Then have `generate_gaps_report.py` disclose
   prose-triggered exclusions so `TESTING_GAPS.md` reports the gate's real
   perimeter. Confirmed by execution that the corrected config restores
   statements 33/376/407, all executed, at 100.00%.
5. **F-M1** — filter by prefix **before** parsing in `script_completer`'s name
   branch, **case-insensitively** (the downstream filter at
   `prompt_common.py:498` is `name.lower().startswith(word_before.lower())`);
   memoize `_describe_scripts` on `(path, st_mtime_ns, st_size)`; wrap
   `SimulatorCompleter` in `ThreadedCompleter`. If you also take the "read only
   the top-level mapping" optimisation, preserve the `(Error loading: ...)`
   rendering and the terminal-safety parity round 7 verified on that path.

**Test / CI**

6. **T-M4** — make `test_every_declared_parameter_is_actually_read` per-action:
   split `_execute_step` on the `elif action == "..."` chain (the same
   extraction the sibling test already performs) and assert
   `_ACTION_PARAMS[action] == {params read in that action's block}`, both
   directions. Presently blind for 11 of 19 actions.
7. **T-M3** — one parametrized test per limit on the real `SET_*` wire paths
   (`limit-1` accepted, `limit` accepted, `limit+1` rejected) for
   `MAX_HOLD_TIME_CENTISECONDS`, `MAX_TRIGGER_VOLTAGE`, `MAX_SCHEDULE_INDEX`.
8. **B-M1 / S-M1 fuzz gap** — a property test that feeds arbitrary *bytes* to
   `data_received` and asserts it never raises, with unbounded `st.integers()`
   and a deep-nesting generator. Note the suite already feeds bytes
   (`tests/fuzz/test_framing_fuzz.py:226`, `:249`); what it cannot do is
   generate a frame that fails to decode.
9. **T-M5** — parametrize `test_disconnect_flushes_the_per_frame_tails` over all
   four throttles, asserting both the tail record and `count == 0`.
10. **T-M1** — count `_resume_pump` arms in
    `test_only_one_continuation_is_ever_scheduled` (assert exactly 1 after the
    second submit), plus an at-scale case (N stalled submits ⇒ 1 continuation).
11. **T-M2** — one deterministic test at `hold_time = 0` with a blocking sensor,
    asserting a bounded wake count over a fixed number of loop turns; per rule 8
    also at `hold_time == MIN_BLOCKED_RECHECK` and just above.
12. **T-L3, T-L4** — make the two seeds decisive (`present=False,
    ac_present=False` and `_hold_time = 0.29`). Both verified to pass on
    pristine and fail on the mutant.
13. **T-L2, T-L1** — tighten the action assertion to `documented ==
    set(_ACTION_PARAMS)` (verified equal today); extract `_check_condition`'s
    names with `ast` (or the sibling's two-pattern regex) so `in (...)` is seen.
14. **F-L1** — extend `tests/test_docs_accuracy.py` to pin the three quoted
    daemon-mode strings (assert the refusal equals
    `describe_out_of_directory_remedy()` under `_script_paths_allowed = False`,
    and the marker template against `render_script_listing`'s real output).
15. **F-L2** — add the fourth docs-accuracy test in the shape of the three round
    7 added, asserting the documented `set` and `toggle` name sets against
    `_set_value` / `_toggle_value`.

**Docs / release hygiene / cosmetics — one pass**

16. **F-L3** — add the missing `CHANGELOG.md` entries for `da31ae2`, with the
    two breaking DSL changes clearly marked and the one-line remedy for each.
    A CI check that `CHANGELOG.md` moves whenever `src/` does is worth the
    minute.
17. **F-L1** — correct the three phrases at `docs/simulator.md:321-324`.
18. **F-L2** — retitle the table "Settings for `set`" and state that `toggle`
    accepts the boolean rows only.
19. **F-L4 (surviving half only)** — give the five bare `Unknown X` messages
    their accepted set (every one is a literal already in the same function),
    and add the cross-table hint for the `assert` / `wait_for` pair. Reword
    `Use: none` so it does not read as an instruction to pass `none`.
20. **T-L5** — delete the dead loop in `_json_blocks`.

## Discarded

- **T-H1 recommendation 1** (`"pragma: no cover"` → `"#\s*pragma:\s*no\s+cover"`)
  — **REFUTED by execution.** I built it and ran the gate: `num_statements=235`,
  `excluded=[33, 79, 376, 407, 420, 421]`, `TOTAL 6662` — byte-identical to
  baseline, zero lines restored. Lines 83/379/407 contain `` `# pragma: no
  cover` `` in backticks and still match; line 33 stays excluded via the four
  other bare patterns. Replaced by item 4 above.
- **S-L2's recommended constant** (`MAX_HOLD_TIME_CENTISECONDS = 90000` from
  `simulator/protocol.py`, applied in `door.py`) — **rejected on wire grounds.**
  It would narrow what the shipped library accepts from a real device to a
  reverse-engineered simulator bound, and it would be the first `simulator/`
  import into the core library (no non-simulator module imports `simulator`
  today). Rejected regardless of technical merit, per the standing rule.
- **B-L1's "bound the value inside `_keep_int`" as written** — conflicts with
  `tests/test_door.py:2122`, which deliberately asserts
  `door.battery_percent == 10**400`. Must be an optional bound at the one call
  site (item 3).
- **F-L4's "pick one spelling and route all three families through one helper"**
  — discarded as unsupported churn. `Available:` / `Choose from:` / `Use:` serve
  three different contexts (subcommand enumeration, `ArgSpec` choice validation,
  DSL parameter sets), no user confusion was demonstrated, and the change would
  rewrite operator strings that `tests/simulator/test_commands.py` pins for a
  purely stylistic gain. The five missing lists survive.
- **S-L2's framing "a regression introduced by the round-7 fix"** — disproven.
  `a0194bd`'s `_on_hold_time_update` was `self._hold_time = value / 100.0`,
  which raises the identical `OverflowError`. It is a pre-existing gap the guard
  did not close.
- **S-M1 recommendation 4** (cap concurrent door connections) — unchanged from
  round 7's assessment: defensible fidelity improvement, optional, not part of
  this fix.

**Severity adjustments (kept on the list, re-rated).** F-M1 Medium → Low
(sub-second hiccup on a dev tool, no loss, no false PASS, needs 100+ user
scripts *and* an operator keystroke; round 7's frontend Mediums both corrupted
state or produced a green PASS for nothing). T-H1 High → Medium (zero live
damage; the three excluded statements are executed today; blast radius is one
non-shipped dev script). T-M1, T-M2, T-M5 Medium → Low, and T-L5 Low → Trivial,
on the round-7 precedent for single missing assertions on guards that are
correct today with no production consumer affected. T-M3 and T-M4 stay Medium:
T-M4's guard has a *present-day* hole covering 11 of 19 actions, and T-M3 is an
aggregate boundary pin at the untrusted-input layer across three shipped
constants whose siblings were pinned and which was missed.

**Numbers I would not quote.**

- Security M1's *"34% of frames make `data_received` raise"* — that figure is a
  property of how their generator was weighted (it deliberately includes deep
  nesting and long integer literals), not of the input space. It says nothing
  about real-world likelihood and reads as if it did.
- Frontend M1's *"1694 ms"* / *"1313.6 ms"* — taken at load average 7–11. On an
  idle machine the same measurements are 727 ms and 722 ms. The ratios and the
  linearity hold exactly; the absolute figures are roughly 2× larger in the
  report than I could reproduce.
- Test-fanatic M2's *"33,874 loop iterations, 296.5 ms CPU"* — I measure a
  genuine 71.7%-of-a-core spin, so the conclusion is right, but the iteration
  count depends entirely on the counting wrapper used, and my own first two
  attempts at that measurement were both wrong for opposite reasons. I would
  quote the CPU percentage, not the iteration count.
- Test-fanatic H1's *"straight out of the committed `coverage.json`"* —
  `coverage.json` is not tracked (`git ls-files` has no match); it is a local
  artefact. The exclusions are real, but they must be re-derived, not read from
  an untracked file.
