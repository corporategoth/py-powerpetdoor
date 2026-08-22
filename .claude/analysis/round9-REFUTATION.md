# Round 9 Refutation Pass

Adversarial re-verification of all 23 round-9 findings at commit `04b9c96`.

**Method.** Every finding was re-derived from scratch. I ran none of the round-9
harnesses and reused none of their transcripts; I wrote my own probes, my own
mutations and took my own measurements. Work happened under `/tmp/r9ref` on a
`git archive HEAD` copy, with `PYTHONPATH` forced at the copy's `src/` and a
pytest plugin (`-p guardplugin`) that raises unless
`powerpetdoor.__file__.startswith($R9_EXPECT_ROOT)`. The guard was falsified
before use:

```
$ R9_EXPECT_ROOT=/nonexistent/xyz ... pytest -p guardplugin --co tests/test_framing.py
INTERNALERROR> SystemExit: R9 GUARD FAIL: /tmp/r9ref/null/src/powerpetdoor/__init__.py
              does not start with '/nonexistent/xyz'
```

No repository file was modified (`git status --porcelain` empty at start and at
end) except this report. Every daemon and PTY child I started was terminated
(`pgrep -af ppd-simulator` → nothing).

**Controls.**

| Control | Expectation | Result |
|---|---|---|
| Null control (pristine copy) | must pass | `2725 passed in 49.21s` |
| Null control 2 (comment-only edit in `door.py`) | must pass | `2725 passed in 43.86s` |
| Control mutation `centiseconds / 100.0` → `/ 1000.0` | must fail | **`15 failed, 2710 passed`** |
| Control mutation `_keep_int` `<= maximum` → `< maximum` | must fail | **`1 failed, 2724 passed`** |
| Control mutation `framing.py:627` `<` → `<=` | must fail | **`1 failed, 2724 passed`** |

**Mutation discipline.** 12 mutations, each on its own fresh copy, each a full
un-subsetted suite run. 3 controls that must be caught were all caught; 8
non-control mutations produced exactly the outcome the test-fanatic report
claimed; 1 candidate fix ran green. **12/12 agreement with the report.**

**Headline result: 13 CONFIRMED · 8 CONFIRMED-BUT-OVERSTATED · 0 fully REFUTED ·
2 already fixed (fix verified).**

A zero-whole-finding-refutation round is not a rubber stamp. What I *did* kill,
by execution:

- **One quarter of the coverage High is refuted.** T-H1's `e03`
  (`fail_under = 100 → 50`) does **not** weaken CI: the real gate is
  `coverage report --fail-under=100`, an explicit CLI flag that overrides
  `pyproject.toml`. Proven on a 67 %-coverage control — config-only exits 0, the
  CLI flag exits 2. The other four edits do get past the real gate at 100.00 %.
- **Frontend M1's packaging half is refuted by a clean build with a control.**
  `py.typed` lands in the wheel *without* being added to
  `[tool.setuptools.package-data]`; an arbitrary control file in the same
  directory does not. The recommended `pyproject.toml` edit is unnecessary and
  its stated rationale is false.
- **Frontend M2's third sub-claim is refuted.** The low-level client does **not**
  "get this right": `PowerPetDoorClient.send_message` queues and replays
  identically, and a `notify=True` future issued while never connected stays
  **pending indefinitely** — it fails in 0.1 s only because the report's probe
  called `stop()`. The facade loses nothing on the way up.
- **Frontend H1's "three tests" is corrected to one.** Only
  `test_cli.py::test_keyboard_interrupt_exits_130` is vacuous; the two `ctl`
  tests assert an exit code the `ctl` binary really produces (verified: 130 on a
  real pty). H1's recommendation 3 (SIGTERM) is an enhancement, not a defect:
  SIGTERM already yields **143**, which is not a false green.
- **Two severities cut by a full step** (F-M2, T-M1) and **three by one**
  (F-H1, T-H1, T-M3).
- **Security M2's strongest sentence is qualified**: under CPython's default
  `logging.raiseExceptions = True` the hostile value *is* echoed to stderr
  (`Arguments: ('\ud800SECRETPROBE',)`), so it is relocated, not deleted. It is
  genuinely deleted only when `raiseExceptions` is `False` (0 bytes anywhere).
- **A new ordering constraint proven by execution:** S-M1's recommendation to
  replace `%r` with `sanitize_text` at `client.py:1803` / `door.py:264` would
  *regress* those sites unless S-M2 lands first — `repr()` escapes a surrogate
  and encodes fine, `sanitize_text` as shipped does not.
- Six supporting numbers I would not quote are listed at the end.

---

## Summary

| ID | Persona | Claimed | Verdict | Adjusted |
|---|---|---|---|---|
| B-F1 `finally` restores flow control but not forward progress | backend | Low | **CONFIRMED** | Low |
| B-F2 `json.dumps` per frame; byte totals under-report | backend | Low | **OVERSTATED** | Low (CPU half down-weighted) |
| S-M1 four per-frame log sites neither throttled nor capped | security | Medium | **CONFIRMED** | Medium |
| S-M2 unpaired surrogate survives `sanitize_text` | security | Medium | **OVERSTATED** | Medium |
| S-L1 same as B-F1 | security | Low | **CONFIRMED** | Low |
| S-L2 `${{ github.base_ref }}` → `run:` injection | security | Low | **ALREADY FIXED — verified** | — |
| S-I1 sdist ships tests without the machinery to run them | security | Info | **OVERSTATED** | Informational |
| F-H1 Ctrl-C → exit 0 and `All scripts PASSED` | frontend | High | **OVERSTATED** | Medium |
| F-M1 no `py.typed`; downstream sees `Any` | frontend | Medium | **CONFIRMED** (packaging half REFUTED) | Medium |
| F-M2 facade queues and replays commands on reconnect | frontend | Medium | **OVERSTATED** (sub-claim 3 REFUTED) | Low |
| F-M3 misspelled top-level key → 0 steps, `PASSED`, rc 0 | frontend | Medium | **CONFIRMED** | Medium |
| F-L1 startup bind/resolve failures print raw tracebacks | frontend | Low | **CONFIRMED** | Low |
| F-L2 unusable `--history` is detected, logged, then used | frontend | Low | **CONFIRMED** | Low |
| F-L3 changelog guard scoped out of `push` | frontend | Low | **ALREADY FIXED — verified** | — |
| F-L4 `ctl ""` hangs for the whole `--timeout` | frontend | Low | **CONFIRMED** | Low |
| F-T1 out-of-range numeric options leak OS errors | frontend | Trivial | **CONFIRMED** | Trivial |
| T-H1 the coverage config has no test | test | High | **OVERSTATED** (`e03` REFUTED) | Medium |
| T-M1 `partial_branches` unconfigured/unanchored | test | Medium | **OVERSTATED** | Low |
| T-M2 `activate_sensor`'s gate untested, and it diverges | test | Medium | **CONFIRMED** | Medium |
| T-M3 `_keep_int`'s magnitude bound pinned on one side | test | Medium | **OVERSTATED** | Low |
| T-L1 `max_inflight` cap unpinned on the re-entrant path | test | Low | **CONFIRMED** | Low |
| T-L2 `_log_rejected`'s "expected" text unasserted | test | Low | **CONFIRMED** | Low |
| T-L3 `st_size` key component / exclusive span end unpinned | test | Low | **CONFIRMED** | Low |

Counts: **CONFIRMED 13 · CONFIRMED-BUT-OVERSTATED 8 · REFUTED 0 · already
fixed 2.**

---

## Verdicts

### Coordinator's already-applied fix (`d12c693`) — S-L2 and F-L3 — **VERIFIED, correct, with two residual notes**

Both halves were re-derived, each against a control.

**Injection (S-L2).** Matched pair, run from the repo root:

```
### the FIXED form (BASE_REF via env)
$ BASE_REF='main$(id > /tmp/r9ref/wf/INJECTED.txt; echo PWNED)' bash script.sh
OK: 0 file(s) under src/ changed
INJECTED.txt exists? NO

### the PRE-FIX form, for contrast (direct interpolation)
$ bash -c "base=\"origin/$REF\"; echo \"base=\$base\""
base=origin/mainPWNED
INJECTED.txt exists? YES
```

The command substitution ran under the old form and created the file; under the
new form it does not. Correct.

**Never-firing gate (F-L3).** The job's shell, `BASE_REF` unset (the push path),
run at each commit in its own clone:

```
da31ae2    rc=1  ::error::src/ changed but CHANGELOG.md did not.   <- the motivating commit
145cf05    rc=0  OK: 6 file(s) under src/ changed                  <- CHANGELOG moved with it
d12c693    rc=0  OK: 0 file(s) under src/ changed
2225e28    rc=1  ::error::src/ changed but CHANGELOG.md did not.
7593a1f    rc=1  ::error::src/ changed but CHANGELOG.md did not.
```

The guard now fires on the event this repository actually uses, and it catches
the exact commit round-8 L3 was written about. Correct.

**Two residual notes, filed as notes and not as findings.**

1. `HEAD^` inspects only the **tip commit** of a push. The frontend's
   recommendation was `${{ github.event.before }}..${{ github.sha }}`, which
   covers the whole push. For this repo's one-commit-per-push history the two are
   equivalent; on a multi-commit push a `src/` change in a non-tip commit is
   missed.
2. If `$base` does not resolve, both `git diff` invocations fail, `changed` is
   empty and the job prints `OK: 0 file(s) under src/ changed` and exits **0** —
   a silent pass. That is visible in the injection transcript above.

### Convergence — B-F1 and S-L1 (`finally` restores flow control, not forward progress) — **CONFIRMED (Low)**

Structurally verified first: at `framing.py:627-630`, `_schedule_pump()` is
inside the `try` and only `_update_flow()` is in the `finally`.

My own probe, four cases with a control, `MAX_INFLIGHT_FRAMES=64`,
`MAX_FRAME_BACKLOG(pause_at)=256`, a counting transport and a loop exception
handler installed:

```
--- A. small backlog (<= pause_at), first dispatch raises ---
  small: submit escaped=RuntimeError  backlog=9  inflight=0 paused=False
  small: after 3000 loop turns:       backlog=9  inflight=0 paused=False  _pump_scheduled=False
--- B. large backlog (> pause_at), first dispatch raises ---
  large: submit escaped=RuntimeError  backlog=999 inflight=0 paused=True
  large: after 3000 loop turns:       backlog=999 inflight=0 paused=True   _pump_scheduled=False
--- C. large backlog, raise on frame 65 (the call_soon re-armed pump) ---
  rearm: submit escaped=None          backlog=936 inflight=0 paused=True
  rearm: after 3000 loop turns:       backlog=934 inflight=0 paused=True   _pump_scheduled=False
         loop-handler exceptions=['RuntimeError']
--- D. CONTROL: large backlog, NO raise at all ---
  ctrl : after 3000 loop turns:       backlog=0   inflight=0 paused=False  pauses=1 resumes=1
```

Case C is the wedge: no exception escapes `data_received`, reading is paused,
nothing is in flight, nothing is scheduled, and 934 frames sit there forever.
Case D — the identical 1000-frame shape with the raise removed — drains
completely. The `finally` did not remove the state round 8 named; it removed it
only below `pause_at`, and even there (case A) the backlog does not drain, it
merely waits for the next read.

I also confirmed the shipped test sits in the harmless branch:
`tests/test_framing.py` builds the raising-dispatch case with
`FrameDispatcher(dispatch, max_inflight=4, pause_at=1)` and six frames — exactly
the small-backlog case.

**Reachability — independently re-derived, and it holds.** My own sweep, 28
pathological brace-balanced shapes (>4300-digit integers in value/key/exponent/
negative position, 9999-deep lists, 5000-deep dicts, lone surrogates in
`CMD`/`msgID`, `NaN`/`Infinity`, duplicate keys, a 60 000-digit fraction,
unhashable `msgID` and schedule index, a 60 000-char timezone, a 60 000-char
`door_status`, top-level non-objects) delivered in the round-8 wedge shape
(`{x}`×64 + payload + `{x}`×300) to the **real** `data_received` on both twins:

```
shapes tested: 28 x 2 sides = 56
escapes from data_received : 0
stuck dispatchers          : 0
```

**The fix is validated.** Moving the re-arm into the `finally` on a throwaway
copy:

```
--- A/B/C/D, fixed tree ---
  small: after 3000 loop turns: backlog=0 inflight=0 paused=False   dispatched=10
  large: after 3000 loop turns: backlog=0 inflight=0 paused=False   dispatched=1000
  rearm: after 3000 loop turns: backlog=0 inflight=0 paused=False   dispatched=1000
  ctrl : after 3000 loop turns: backlog=0 inflight=0 paused=False   dispatched=1000
```

full suite `2725 passed`; CI gate `TOTAL 6775 0 2410 0 100.00%`, exit 0; `ruff
check`/`format` clean; `mypy src` clean. One frame lost per raise, which is what
the docstring promises and what the change actually makes true.

**Wire check: passes.** Flow-control scheduling only; no byte we send changes,
nothing we accept is narrowed.

**Low is right for both.** Not reachable from any input I could construct
(0/56), and the only known trigger was round 8's, which is fixed. What is real
is that the shipped docstring at `framing.py:609-617` and the round-8 CHANGELOG
entry both claim a guarantee the code does not provide, on a component that
documents itself as "must not depend on its callback being total".

**One number I would keep but re-frame.** B-F1's "99.8 % of a burst drain is the
wedging branch" is arithmetically right (a one-read burst puts every frame in the
backlog at once, so only the last 256 dispatches see `backlog <= pause_at`), but
it is a conditional statistic — *given* a raise, which today cannot happen. It
describes the blast radius of a future regression, not present exposure.

### B-F2 — `json.dumps` on every occurrence; the byte total under-reports — **CONFIRMED BUT OVERSTATED (Low)**

**The byte-total half reproduces exactly, with a decisive control** (real
`PowerPetDoorClient.data_received`, running totals read from `disconnect()`'s
flush):

```
A. the malformed-message site (_bad_messages)
  compact `{}`                              wire=    2  -> Ignored 1 malformed message(s) from device (2 bytes)
  the same `{}` padded with 60,000 spaces   wire=60002  -> Ignored 1 malformed message(s) from device (2 bytes)
B. the device-error site (_device_errors)
  compact error envelope                    wire=   25  -> Device reported 1 error response(s) (28 bytes)
  error envelope padded with 60,000 spaces  wire=60025  -> Device reported 1 error response(s) (28 bytes)
C. CONTROL: the bad-frame site (_bad_frames) on the same padding
  unparseable frame padded, 60,000 spaces   wire=60003  -> Failed to decode 1 JSON frame(s) from device (60003 bytes)
```

The control is what makes it specific: the sibling three code paths away reports
the padded frame exactly right. 60 002 wire bytes reported as "2", and a compact
25-byte envelope over-reported as "28" because `json.dumps` inserts spaces the
wire did not have. That is an operator-facing number that a peer controls
independently of what it sent.

**The CPU half is down-weighted.** The isolated cost reproduces:

```
  json.dumps({})              = 1.170 us/call     len(msg) = 0.038 us/call
  json.dumps(error envelope)  = 1.251 us/call     len(msg) = 0.031 us/call
  -> 32,768 frames in one 64 KiB read of b'{}'  = ~38 ms of discarded serialization
```

but the report's headline framing ("~10–12 % of the whole per-frame receive
path") rests on an end-to-end A/B its own text flags as noisy (`7.76`–`11.83
µs/frame` across seven runs of the same configuration). I would quote the
isolated microseconds and the 38 ms per 64 KiB read, and not the percentage.

**The test churn the report owns is real.** `tests/test_client.py:4160` pins
`"Device reported 3 error response(s) (96 bytes) on this connection"`; 96 is the
re-serialized size, and that assertion has to move under the fix.

**Wire check: passes.** A log number becomes accurate; nothing sent or accepted
changes. Low stands — this is diagnostic accuracy, not a security hole, and the
CPU it saves is dwarfed by S-M1's unthrottled sites in the same function.

### S-M1 — four per-frame log sites neither throttled nor capped — **CONFIRMED (Medium)**

My own harness, a **really connected** `PowerPetDoor` facade over a real
`DoorSimulator` socket, hostile frames injected through the live client's
`data_received`, records counted at a `logging.Handler` with a standard
formatter, logger at WARNING (the Home Assistant default):

```
=== N=20000 frames ===
  client.py:1803 unusable msgID   [NO thr/cap] wire=  460000 log= 2423614 recs= 20032 amp=x5.269 longest=126
  door.py:264 unknown door status [NO thr/cap] wire= 1620000 log= 2080000 recs= 20000 amp=x1.284 longest=103
  door.py:1576 malformed schedule [NO thr/cap] wire= 1460000 log= 2860000 recs= 20000 amp=x1.959 longest=142
  client.py:1785 malformed msg     [r6 thr+cap] wire=   40000 log=    3598 recs=    32 amp=x0.090 longest=132
  client.py:1856 device error      [r7 thr+cap] wire=  560000 log=    1175 recs=    10 amp=x0.002 longest=126

=== ONE frame just under the 64 KiB framing cap ===
  client.py:1803  msgID = 21840-item list      wire=65541  recs=1  longest_record=65638
  door.py:264    door_status = 65000 chars     wire=65064  recs=1  longest_record=65086
  client.py:1856 device error, 64000 chars     wire=64027  recs=0  (throttle already saturated)
```

**20 032 / 20 000 / 20 000 records against 32 and 10 for the throttled
siblings**, and a **65 638-byte single log record** from one frame where the
round-7-fixed sibling caps at 293. The fourth site (`door.py:1494`, capped but
unthrottled) is confirmed separately in the S-M2 harness: 200 frames → 200
records.

`door.py:264` firing on ordinary traffic is confirmed by construction — it is
inside `DoorStatus.from_value`, so a firmware revision reporting a status string
this library does not know produces one uncapped WARNING per status update on a
correctly-functioning installation.

**Medium stands.** Identical class, identical mechanism and identical
reachability to round-6 finding 2 and round-7 L3, both of which this project
filed and fixed at Medium: unauthenticated LAN peer, no interaction, availability
impact on the host application's log and disk, no confidentiality or state
impact.

**One correction and one new constraint.**

- I measure **×5.27**, not ×6.06, for the `msgID` site. The ratio is a function
  of frame size (their frame was 20 bytes, mine 23 after `json.dumps`
  round-tripping). I would quote the record count and the 65 638-byte record,
  both of which reproduce exactly, rather than a single amplification figure.
- **Recommendation 2 must not land before S-M2.** Proven by execution:

  ```
  repr('\ud800BAD')                     -> "'\\ud800BAD'"   encodes to utf-8: OK
  sanitize_text('\ud800BAD', 200)       -> '\ud800BAD'      encodes to utf-8: FAILS
  ```

  Swapping `%r` for `sanitize_text` at `client.py:1803` and `door.py:264` would
  take two sites that are currently *safe* against a surrogate and make them
  unwritable. The security report flags this in a parenthetical; it is a hard
  ordering constraint.

### S-M2 — an unpaired surrogate survives `sanitize_text` — **CONFIRMED BUT OVERSTATED (Medium)**

**Regex coverage and reachability**, re-derived:

```
  U+001B: matched? True    U+D7FF: matched? False   U+D800: matched? False
  U+007F: matched? True    U+DFFF: matched? False   U+E000: matched? False
  U+009F: matched? True

  wire bytes are pure ASCII: True  (b'{"CMD":"GET_SETTINGS","fwInfo":"\\ud800BAD"}')
  json.loads -> '\ud800BAD' | is surrogate: True
  sanitize_text -> '\ud800BAD'
  .encode('utf-8') FAILS: 'utf-8' codec can't encode character '\ud800' ...
```

**End to end through the shipped facade**, `door.py:1494`, real
`logging.FileHandler(encoding="utf-8")`, **matched pair**:

```
  CONTROL: ESC string        wire= 13600  logfile= 17400 lines=200 stderr=     0 dropped=0
  lone surrogate string      wire= 13000  logfile=     0 lines=  0 stderr=359400 dropped=200
```

Same site, same handler, same frame shape — six characters take the shipped
client from 200 records written to **zero**, and move 359 KB to stderr (×27.6 the
wire bytes), from a code path that is **outside every `EventThrottle` this
project has built**. Confirmed.

**Where it is overstated.** "An attacker deletes the record that documents the
attack" needs a qualifier. Under CPython's default `logging.raiseExceptions =
True`, `handleError` echoes the payload:

```
  UnicodeEncodeError: 'utf-8' codec can't encode character '\ud800' in position 36
  Message: 'Ignoring non-mapping hardware info: %s'
  Arguments: ('\ud800SECRETPROBE',)
  contains 'SECRETPROBE': True
```

so the evidence is *relocated* to stderr, not erased — unless the operator sets
`raiseExceptions = False`, which is common in production and which I measured
directly:

```
  --- with logging.raiseExceptions = False ---
  stderr bytes: 0 | logfile bytes: 0
```

There the record genuinely vanishes with nothing anywhere. **Medium stands**: the
log *file* — the operator's paper trail — loses the record in both configurations,
and the unthrottled ×27.6 stderr amplification is a real resource fault on its own.

**The fix is validated.** `[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]` with a
width-aware replacement, on a throwaway copy:

```
  U+001B -> '\\x1b'   U+D7FF -> '퟿' (untouched)   U+D800 -> '\\ud800'
  U+009F -> '\\x9f'   U+E000 -> '' (untouched)   U+DFFF -> '\\udfff'
  encodes to utf-8 OK: True
```

full suite `2725 passed`; `ruff check src tests scripts` clean; `ruff format
--check` clean (`81 files already formatted`); `mypy src` clean.

**Wire check: passes.** The frame is still accepted, parsed, dispatched and
rejected-or-cached by exactly the same code; only the rendering into a log record
changes.

### S-I1 — the sdist ships tests it cannot run — **CONFIRMED BUT OVERSTATED (Informational)**

`uv build` from a clean copy of this commit, sdist unpacked:

```
$ ls -a tests/
test_client.py  test_docs_accuracy.py  test_door.py  test_exports.py  test_framing.py
test_gaps_report.py  test_sanitize.py  test_schedule.py  test_tz_utils.py
```

No `conftest.py`, no `__init__.py`, no `fuzz/`, no `simulator/`, no
`TESTING_GAPS.md`. Confirmed.

**The stated measurement is not reproducible.** The report says `pytest` in the
unpacked sdist gives "1 error, 0 tests". I get, with this project's interpreter
(which has `powerpetdoor` installed):

```
25 failed, 181 passed, 44 errors in 2.85s
```

and with a clean interpreter that has only `pytest` (i.e. what a third party
actually has), `pytest` does not start at all — the shipped `pyproject.toml`
carries `addopts = "-n auto"` and the sdist declares no test dependencies:

```
ERROR: usage: python -m pytest [options] ...
python -m pytest: error: unrecognized arguments: -n
  inifile: .../pypowerpetdoor-0.3.0/pyproject.toml
```

The substance holds — a third party cannot run this project's tests from the
artifact it publishes — but the specific "1 error, 0 tests" figure is an artefact
of one environment, and the *first* obstacle is the missing test extra, not the
missing `conftest.py`. Informational is right; either recommendation (graft
`tests/` plus the machinery and the test deps, or `prune tests`) closes it.

### F-H1 — Ctrl-C makes `--oneshot` exit 0 and report `PASSED` — **CONFIRMED BUT OVERSTATED (High → Medium)**

**Both halves reproduce on a real TTY** (`pty.fork()`, `\x03` written into the
tty 4–5 s after start; my harness's exit-status capture was falsified against a
known-nonzero control first).

Half (a) — a run cut off before its assertion:

```
2026-08-22 16:17:51 [INFO]   Step 1: log(message=starting)
2026-08-22 16:17:51 [INFO]   Step 2: wait(seconds=30)
^C2026-08-22 16:17:55 [INFO] Door simulator stopped
>>> All scripts PASSED
exited rc = 0
```

Step 3 (`assert`) never ran. Half (b) — an already-FAILED run:

```
[ERROR] Assertion failed at step 2: door_status: expected 'DOOR_HOLDING', got 'DOOR_CLOSED'
>>> Script FAILED: Failing Script
...
^C >>> All scripts FAILED
exited rc = 0
```

`>>> All scripts FAILED` and rc 0 in the same run. Control (no interrupt):
`failing -> rc=1`, `passing -> rc=0`. Programmatic `SIGINT` (not a tty) is
identical: `rc= 0`, `>>> All scripts PASSED`.

**Mechanism, re-derived.** The startup-script task is a bare
`asyncio.create_task` with no reference held. `run_simulator` swallows its own
cancellation and returns `script_result[0]`, which is still `None` because the
script task has not been cancelled yet; `asyncio.run`'s shutdown then cancels it,
whose `finally` sets the flag and prints the banner — *after* `main()` has
already read `None`. So `if args.oneshot and result is not None:` is false and
`main()` falls off the end at 0.

**Two corrections.**

1. **"Three tests pass while the binary does not" is one test.** Only
   `tests/simulator/test_cli.py:585` monkeypatches `cli.run_simulator` and is
   therefore vacuous. The two `ctl` tests monkeypatch `ctl.send_command` /
   `ctl.interactive_mode_async`, and the `ctl` binary genuinely behaves as they
   assert — verified on a real pty:
   ```
   $ ppd-simulator-ctl -p 3812 run long wait        # SIGINT at t+4s
   ^C Interrupted.
   exited rc = 130
   ```
2. **Recommendation 3 (SIGTERM) is an enhancement, not a proven defect.**
   SIGTERM already yields **143** (`128+15`), which is not a false green; the
   `finally` not running is untidy, not incorrect.

**Recommendation 2 is safe.** I checked the one way it could bite: `--oneshot`
without `--script` is already rejected by argparse (`ppd-simulator: error:
--oneshot cannot be used without --script`, rc 2), so "`--oneshot` produced no
result" has no legitimate meaning today.

**Why Medium and not High.** The defect is real, on the documented CI command,
with a code comment claiming the opposite and a vacuous test claiming coverage —
it should be at the top of the fix list. But it requires a **SIGINT
specifically**, and the two common CI interrupt paths do not produce a false
green: a cancelled GitHub Actions job is recorded as cancelled by the CI system
regardless of exit code, and a `timeout`-wrapped run yields 143. There is no data
loss, no state corruption, and no wrong result on any uninterrupted run. High in
this project's scale has meant worse (round 8's only High was cut to Medium for
less). Medium, first on the list.

### F-M1 — no `py.typed`; downstream type-checks the library as `Any` — **CONFIRMED (Medium); the packaging half REFUTED**

Installed-package reproduction, my own venv, mypy with `--python-executable`:

```
=== AS SHIPPED (no py.typed) ===
consumer/app.py:1: error: Skipping analyzing "powerpetdoor": module is installed,
    but missing library stubs or py.typed marker  [import-untyped]
=== AFTER touching py.typed ===
consumer/app.py:7: error: Argument 1 to "set_hold_time" of "PowerPetDoor" has
    incompatible type "str"; expected "float"  [arg-type]
```

Exactly as claimed: one `touch` turns "I cannot see this library at all" into
"line 7 is wrong". (My first attempt used `MYPYPATH`, which treats the package as
*source* and needs no marker — both runs reported the arg-type error. That
measurement is invalid and I discarded it; only the installed-package form tests
PEP 561.)

**The packaging caveat is refuted by a clean build with a control.** The report
says the marker "has to be listed [in `[tool.setuptools.package-data]`] or it
will be present in the source tree and absent from the wheel." It is not:

```
$ rm -rf dist build src/*.egg-info && uv build
$ unzip -l dist/*.whl | grep -E "py.typed|zzz_control"
        0  2026-08-22 20:28   powerpetdoor/py.typed
$ unzip -l dist/*.whl | grep -c zzz_control
0
```

`py.typed` is included automatically; an arbitrary control file
(`zzz_control.txt`) placed in the same package directory is **not**. So the
recommended `pyproject.toml` edit is unnecessary and its rationale is false.
(Adding it is harmless; the *reason* given for it must not be repeated.)

**Medium stands.** No runtime or security impact, but it silently disables — for
100 % of downstream consumers, 100 % of the time — a quality control this project
pays for (`mypy src`, `warn_return_any = true`, a CI gate) and documents across
121 exported names. The fix is one empty file.

### F-M2 — the facade queues commands issued while disconnected and replays them — **CONFIRMED BUT OVERSTATED (Medium → Low); sub-claim 3 REFUTED**

**The behaviour is real, and I reproduced it in the *realistic* scenario the
report describes** (not just the never-connected one), against a real
`DoorSimulator` on a real socket:

```
connected: True | sim door_status: DOOR_CLOSED
after device went away: door.connected = False
open_and_hold() during reconnect window -> returned in 0.000s, no error
device back at t+1.0s; waiting for reconnect...
t+4.0s after the button press: connected=True sim door_status=DOOR_KEEPUP
```

A pet door physically opened and latched four seconds after a call that reported
nothing. The never-connected variant reproduces too (`open_and_hold()` →
`None`; 3 s later still `DOOR_CLOSED`; `connect()` → `DOOR_KEEPUP`), and
`set_hold_time` while disconnected raises a literal `TimeoutError()` with an
empty message after the full timeout.

**But it is deliberate, in-code-documented client behaviour**, added as a round-1
fix and still carrying its finding reference (`client.py`, `_adopt_transport`):

```python
# Flush anything that was enqueued while disconnected (L3),
# otherwise open the dequeue gate for the next enqueue.
if self._queue:
    self._can_dequeue = False
    self._track_task(self.dequeue_data())
```

```
$ git log -S "Flush anything that was enqueued while disconnected" --oneline -- src/powerpetdoor/client.py
21a463a Wave 2 fixes from persona analysis round 1
```

**Sub-claim 3 is refuted by execution.** The report asserts the low-level client
"gets this right" and that "the facade loses that diagnosis on the way up". Both
halves are wrong:

```
A. client.send_message(COMMAND, OPEN_AND_HOLD) while disconnected -> returned, queue len: 1
   2s later sim door_status: DOOR_CLOSED
   after client.connect(): sim door_status: DOOR_KEEPUP
B. notify=True future while never connected: STILL PENDING after 6.0s (done=False)
   after stop(): done = True | exc = ConnectionError('Connection closed before a response was received')
```

The client's fire-and-forget path is *identical* (it is literally the same
`send_message` the facade calls), and the `notify=True` future does **not**
resolve in 0.1 s — it stays pending indefinitely. The report's "0.1 s
ConnectionError" contrast is an artefact of its probe calling `stop()`; that is
`disconnect()` failing outstanding futures, not a disconnected-state diagnosis.

**Why Low.** The mechanism is real and safety-relevant, but it is intentional
queue-on-reconnect semantics, not a defect, and the headline remedy ("raise
`ConnectionError` instead of enqueueing") would *remove* a behaviour the client
was deliberately given and would be a breaking change for `ha-powerpetdoor`. What
survives is the part nobody disputes: **it is undocumented at the facade level**
(`docs/door.md` says nothing about it), and `asyncio.wait_for`'s bare
`TimeoutError()` is the least actionable exception this API can produce. Those
are a docs change and a message string.

### F-M3 — a misspelled top-level script key yields a zero-step `PASSED` — **CONFIRMED (Medium)**

```
### misspelled steps: ('stpes')
>>> Running script: Door cycle regression test
>>> Script PASSED: Door cycle regression test
>>> All scripts PASSED
real rc=0
### misspelled name: ('nmae')
>>> Running script: Unnamed Script
>>> Script PASSED: Unnamed Script
### CONTROL: correct script
>>> Running script: Fine
[INFO]   Step 1: log(message=hi)
>>> Script PASSED: Fine
```

No step log, no warning, rc 0. The control shows what a real run looks like.
**Medium stands** — this is the class round-7 F-L3 and round-8 T-M4 were both
kept at Medium for: *a green CI PASS for a script that tested nothing*. Here the
blast radius is the whole file rather than one step, and every other misspelling
class in this DSL (action, sensor, condition, setting, step parameter) now fails
loudly by deliberate decision.

### F-L1 — startup bind/resolve failures print raw tracebacks — **CONFIRMED (Low)**

```
$ ppd-simulator --port 3800 --daemon      # another process holds 3800
rc=1  lines=30
Traceback (most recent call last):
  File "/home/prez/src/pypowerpetdoor/.venv/bin/ppd-simulator", line 10, in <module>
...
OSError: [Errno 98] error while attempting to bind on address ('0.0.0.0', 3800): address already in use

$ ppd-simulator --port 99999 --daemon -r 1   -> OverflowError: bind(): port must be 0-65535.
$ ppd-simulator --host 300.1.1.1 --daemon -r 1 -> socket.gaierror: [Errno -2] Name or service not known
```

30 lines, the one useful line last, with absolute build-machine paths. The
`--scripts-dir` precedent (`parser.error(...)`, rc 2) is in the same file. Low.

### F-L2 — an unusable `--history` path is detected, logged, then used anyway — **CONFIRMED (Low)**

Real PTY, `--history <a directory>`, three commands, **with a control**:

```
--- /tmp/r9ref/fe/hdir (a directory) ---
WARNING lines: 1
   [WARNING] Could not create history file /tmp/r9ref/fe/hdir: [Errno 21] Is a directory
IsADirectoryError occurrences: 1
'Press ENTER to continue' occurrences: 3
Traceback occurrences: 1
--- CONTROL: a usable history path ---
WARNING lines: 0   IsADirectoryError: 0   'Press ENTER to continue': 0   Traceback: 0
```

The code catches the `OSError`, writes a correct warning, and then hands the same
path to `FileHistory()` two lines later. Confirmed, with the control isolating
the cause exactly. **My counts are lower than the report's** (1 traceback / 3
modals vs their 4 / 15) — the count scales with session length, so I would quote
"at least one traceback and one modal per session, repeatedly" rather than their
figures.

### F-L4 — `ctl ""` hangs for the whole `--timeout` and then blames the daemon — **CONFIRMED (Low)**

```
$ ppd-simulator-ctl -p 3802 ""
Response timeout after 5.0s waiting for 127.0.0.1:3802 (the command may still be running; raise --timeout)
elapsed=5.157s
$ ppd-simulator-ctl -p 3802 "   "     elapsed=5.149s   (same message)
### CONTROL: a real command
$ ppd-simulator-ctl -p 3802 "status"  elapsed=0.141s   OK: Current State: ...
```

The daemon skips blank lines by design (`if not cmd: continue`), so no answer can
ever come; the advice is wrong in both halves. The over-long-line variant
reproduces too (`Connection closed without response`). Low.

### F-T1 — out-of-range numeric options surface as OS errors — **CONFIRMED (Trivial)**

```
$ ppd-simulator-ctl -p 39999 -t 0 status   -> Error: [Errno 115] Operation now in progress
$ ppd-simulator-ctl -p 39999 -t -1 status  -> Error: Timeout value out of range
$ ppd-simulator --port 3880 --daemon 3881 --run-for -5
   [INFO] Run time (-5.0s) elapsed, shutting down
```

All three reproduce verbatim. Trivial.

### T-H1 — the coverage configuration has no test — **CONFIRMED BUT OVERSTATED (High → Medium); `e03` REFUTED**

I performed all five `pyproject.toml` edits independently and ran each through
**the real CI gate**, which is `coverage report --fail-under=100`
(`.github/workflows/test.yml`), not `pytest --cov`:

```
baseline    TOTAL 6775  0  2410  0  100.00%     GATE EXIT=0
e01 omit += tz_utils        TOTAL 6682  0  2378  0  100.00%   GATE EXIT=0   (-93 stmts, -32 branches)
e02 source drops "scripts"  TOTAL 6465  0  2302  0  100.00%   GATE EXIT=0   (-310 stmts, -108 branches)
e03 fail_under 100 -> 50    TOTAL 6775  0  2410  0  100.00%   GATE EXIT=0   (nothing removed)
e04 branch = false          TOTAL 6775  0          100.00%   GATE EXIT=0   (Branch/BrPart columns GONE)
e05 exclude "^\s*def _keep_int"  TOTAL 6764  0  2402  0  100.00%   GATE EXIT=0  (-11 stmts, -8 branches)
```

`2678 passed` in every case; `src/` and `tests/` byte-identical throughout.

**e04 is exactly as bad as claimed and is the one that matters.** `branch =
false` stops measuring all **2 410 branch destinations**, deletes the Branch and
BrPart columns from the report, and the real CI gate still prints `100.00%` and
exits 0. Every "100 % branch coverage" claim in `CLAUDE.md`, `README.md` and
`tests/TESTING_GAPS.md` becomes false from a one-word edit. e01, e02 and e05
remove measurable code from the gate exactly as measured.

**e03 is REFUTED as a way past CI.** The gate is invoked as `coverage report
--fail-under=100`, and a command-line option overrides the config file. Proven on
a synthetic 67 %-coverage control:

```
$ coverage report                      # config fail_under = 50
TOTAL          6      2    67%
real exit code with config only:            0
$ coverage report --fail-under=100     # the CI invocation
Coverage failure: total of 67 is less than fail-under=100
real exit code with --fail-under=100: 2
```

So e03 weakens only a *local* `pytest --cov` run — which is a real gate
(CLAUDE.md's pre-commit checklist mandates it), but not the one the report
claims. Four of five sub-claims stand.

**The existing guard is as weak as described.** `tests/test_gaps_report.py:668`
asserts only `assert omit, "coverage.run.omit went missing from pyproject.toml"`
— non-emptiness — and nothing anywhere reads `branch`, `fail_under` or `source`.
`GATED_SOURCE_DIRS` is hard-coded at `tests/test_gaps_report.py:696`, so e02's
drift (sweep scans a directory the gate no longer measures) is real.

**Why Medium.** Nothing is weakened today; the gate is at 100.00 % with the full
2 410 branches measured. This is the same "porous gate, zero live damage" shape
round 8's T-H1 was cut from High to Medium for — and round 8's had *more* live
effect (three statements actually excluded). Medium, and it should be fixed
promptly: e04 is a genuinely silent one-word disarm of the project's central
quality control.

### T-M1 — `partial_branches` unconfigured, unanchored, undisclosed — **CONFIRMED BUT OVERSTATED (Medium → Low)**

Every structural claim reproduces:

```
$ coverage.config.CoverageConfig()
partial_list        : ['#\\s*(pragma|PRAGMA)[:\\s]?\\s*(no|NO)\\s*(branch|BRANCH)']
partial_always_list : ['while (True|1|False|0):', 'if (True|1|False|0):']
$ grep -n partial pyproject.toml   -> (absent)
```

**Matched pair, my own fixture** — two identical functions differing only in a
string literal, `branch = True`:

```
=== DEFAULT partial_branches (what this project has today) ===
pkg/m.py    8   0   4   1   92%   8->10
=== ANCHORED partial_branches ===
pkg/m.py    8   0   4   2   83%   2->4, 8->10
```

Line 2's missing `for`→exit arc is silently forgiven because the phrase appears
inside a string on that line. Real.

**The sweep is blind to it, and works the moment it is given the pattern:**

```
sweep with exclude_lines only   : []
sweep WITH the branch pattern   : scripts/generate_gaps_report.py 550 | f"are excluded via `# pragma: no cover` or `# pragma: no branch`."
sweep WITH the branch pattern   : scripts/generate_gaps_report.py 578 | lines.append("No `# pragma: no cover` or `# pragma: no branch` annotations found.")
sweep WITH the ANCHORED pattern : []
```

`coverage_config()` returns only `run.omit` and `report.exclude_lines`.

**Why Low.** I measured the live damage rather than assuming it, by running the
full CI gate with `partial_branches` anchored:

```
pb_anchored  TOTAL 6775  0  2410  0  100.00%   GATE EXIT=0
baseline     TOTAL 6775  0  2410  0  100.00%   GATE EXIT=0
```

**Byte-identical.** Neither prose line is a branch point, so nothing is forgiven
today and the fix restores nothing. This is a purely latent hole with zero
current effect — strictly less live damage than round 8's T-H1, which the same
persona filed as High and which was cut to Medium. Low, and worth fixing because
the fix is free and the class has recurred four times.

### T-M2 — `activate_sensor`'s gate is untested, and it diverges from its sibling — **CONFIRMED (Medium)**

Both mutations survive the full suite:

```
tm2_a02  should_trigger = state.power and state.inside   ->  ... or ...            2725 passed
tm2_b03  should_trigger = state.power and state.outside and not state.safety_lock
                                                          ->  drop safety_lock     2725 passed
```

and the divergence is real, measured on the shipped tree by driving both entry
points from identical preconditions:

```
  trigger_sensor (inside)  power OFF                    -> DOOR_CLOSED
  activate_sensor(inside)  power OFF                    -> DOOR_CLOSED
  trigger_sensor (inside)  inside sensor disabled       -> DOOR_CLOSED
  activate_sensor(inside)  inside sensor disabled       -> DOOR_CLOSED
  trigger_sensor (inside)  cmd_lockout ON               -> DOOR_CLOSED
  activate_sensor(inside)  cmd_lockout ON               -> DOOR_RISING     <-- diverges
  trigger_sensor (outside) safety_lock ON               -> DOOR_CLOSED
  activate_sensor(outside) safety_lock ON               -> DOOR_CLOSED
  trigger_sensor (inside)  auto ON, zero-length window  -> DOOR_CLOSED
  activate_sensor(inside)  auto ON, zero-length window  -> DOOR_RISING     <-- diverges
```

Reachable from both shipped front ends — `commands/door.py:45,74` (the `inside`
and `outside` CLI commands) and `scripting.py:583,594,601` (the `inside`,
`outside` and `pet_presence` DSL actions) — and `docs/operation.md:249` states
the device behaviour: *"Outside scheduled windows, sensor triggers are ignored"*.

**Medium stands.** This is not merely an untested guard: it is an untested guard
that disagrees with the project's own behavioural specification, on paths both
user-facing front ends drive, in a simulator whose entire purpose is fidelity.
The test is unconditionally right (CLAUDE.md rule 9: no operand of either
compound guard is ever the decisive one). **Which** way the divergence resolves is
a fidelity decision for the owner, and the parametrized test forces it into the
diff rather than leaving it implicit — that is the finding's value.

### T-M3 — `_keep_int`'s magnitude bound is pinned only on its positive side — **CONFIRMED BUT OVERSTATED (Medium → Low)**

```
tm3_m03  -maximum <= coerced <= maximum  ->  coerced <= maximum      2725 passed  (SURVIVED)
tm3_ctrl -maximum <= coerced <= maximum  ->  -maximum <= coerced < maximum
                                                                     1 failed, 2724 passed  (CAUGHT)
```

The control localises the gap exactly. The dropped half is load-bearing:

```
SHIPPED                        hold_time after -(10**400): 2.0   (rejected, cache kept)
MUTANT (dropped -maximum <=)   hold_time after -(10**400): RAISED OverflowError: int too large to convert to float
```

**Why Low.** This is one parametrization on one bound at one call site, on a
guard that is correct today, with no production consumer affected — the shape
round 7 and round 8 both filed at Low for single missing assertions. Round 8's
T-M3 was kept at Medium because it spanned three shipped constants across four
wire call sites; this does not. CLAUDE.md rule 8 is exactly right about it, and
the fix is four parameters.

### T-L1 — the `max_inflight` cap is unpinned on the re-entrant path — **CONFIRMED (Low)**

```
tl1_c07  while budget and ... self._inflight <  self._max_inflight:  ->  <=   2725 passed  (SURVIVED)
tl1_ctrl if self._backlog and self._inflight <  self._max_inflight:  ->  <=   1 failed     (CAUGHT)
```

Direct probe, `max_inflight=4`, 50 frames, handlers gated on an `asyncio.Event`:

```
SHIPPED:  after first submit: inflight=4 ... PEAK inflight = 4 -> bound respected: True
MUTANT :  after first submit: inflight=4 ... PEAK inflight = 5 -> bound respected: False
```

The `budget` counter caps the *first* pump regardless of the comparison, so
`test_concurrency_is_bounded_by_max_inflight`'s post-submit assertion cannot see
it; the comparison only becomes decisive when `_on_dispatched_done` re-enters
`_pump`, which is the steady state of a busy connection. Low.

### T-L2 — `_log_rejected`'s "expected" text is unasserted — **CONFIRMED (Low)**

```
tl2_m04  drop the `"int" if maximum is None else f"int of magnitude <= {maximum:g}"` ternary
         2725 passed  (SURVIVED)
```

Nothing in the suite asserts the third argument of `_log_rejected` for any field;
the only assertion on this line anywhere is the substring `"keeping the cached
value"`, which is identical for every rejection reason. Under the mutation a
`-10**400` rejected for magnitude logs `expected int` — for a value that *is* an
`int`. Low.

### T-L3 — two round-8 boundaries are unpinned — **CONFIRMED (Low)**

```
tl3_m14  scripting.py:1100  key = (path, st_mtime_ns, st_size) -> (..., 0)     2725 passed (SURVIVED)
tl3_m23  generate_gaps_report.py:223  start <= match.start() <  end  ->  <=    2725 passed (SURVIVED)
```

`st_size` is observable and deterministically testable (`os.utime` forces an
identical `st_mtime_ns`):

```
SHIPPED   first: ('FIRST', None)   same mtime_ns, different size -> ('SECOND-and-longer', None)
MUTANT    first: ('FIRST', None)   same mtime_ns, different size -> ('FIRST', None)   <- stale
          sizes: 37 49 | mtimes equal: True
```

Both are correct as written and both unasserted. **The span-end half is the
weaker of the two** — the report says so itself: making the end inclusive turns
the sweep into a false-positive reporter, so the failure mode is a spurious red
build rather than a silent one. Low for the pair; the `st_size` test is the one
worth writing first.

---

## Recommended Fix List

Survivors only, in the order I would fix them. **Nothing here changes a byte we
send to the device or narrows what we accept from it.**

**Correctness — do first**

1. **F-H1** — stop swallowing the cancellation. Do not let `_run_startup_scripts`
   print a PASSED/FAILED verdict for an interrupted run (print
   `>>> Interrupted after N of M steps` and leave the verdict unset), and make
   `main()` treat "`--oneshot` requested but `result is None`" as a failure
   (130 if interrupted, 1 otherwise). Verified safe: `--oneshot` without
   `--script` is already rejected at rc 2, so that state has no legitimate
   meaning. Replace `tests/simulator/test_cli.py::test_keyboard_interrupt_exits_130`
   with one that spawns the real binary and sends `SIGINT` — the two `ctl` tests
   are fine and should be left alone. Recommendation 3 (an explicit SIGTERM
   handler) is optional tidiness, not a defect: SIGTERM already yields 143.

2. **S-M2** — extend `_CONTROL_CHAR_RE` to
   `[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]` with a width-aware replacement
   (`\xNN` at or below 0xFF, `\uNNNN` above). Verified: boundaries at U+D7FF /
   U+D800 / U+DFFF / U+E000 behave, the result encodes to UTF-8, `2725 passed`,
   ruff + mypy clean. Pin it with the four-point boundary test and one end-to-end
   test asserting the record `.encode("utf-8")`s. **This must land before item 3.**

3. **S-M1** — give the four sites the treatment their four siblings already have:
   an `EventThrottle` each, and `sanitize_text(value, MAX_LOGGED_LENGTH)` in
   place of `%r`/`%s`. **Ordering is load-bearing**: I proved by execution that
   `repr()` currently protects `client.py:1803` and `door.py:264` against a
   surrogate and `sanitize_text` (pre-item-2) does not, so swapping them first
   would regress those sites. Capping `client.py:1830`'s `json.dumps(msg)` is
   cheap insurance and is not a proven defect.

4. **B-F1 / S-L1** — move the re-arm into the `finally` alongside
   `_update_flow()`. Validated end to end on a throwaway copy: wedge gone in
   every case, control unchanged, `2725 passed`, CI gate `6775/2410 100.00%`,
   ruff + mypy clean. Correct `framing.py:609-617` and the round-8 CHANGELOG
   entry, both of which claim a guarantee the code does not provide. Add the
   regression test the finding names (`MAX_FRAME_BACKLOG + 44` frames queued, a
   raising dispatch on the re-armed pump, assert `backlog == 0 and not paused`).

5. **F-M3** — reject unknown top-level keys in `Script.from_yaml`, in the
   `Unknown X: y. Use: …` shape the other five errors already share. Load-time
   failure, so `--list-scripts` and `list` surface it through the existing
   `(Error loading: …)` path.

**Coverage / test infrastructure**

6. **T-H1** — add a config-integrity test that asserts the *values* of
   `branch`, `source` and `omit` (and `exclude_lines` as a literal seven-element
   list), and derive `GATED_SOURCE_DIRS` from `coverage.run.source` rather than
   hard-coding it. **Do not** justify a `fail_under` assertion by claiming it
   guards CI — CI passes `--fail-under=100` explicitly and that overrides the
   config; assert it because CLAUDE.md's pre-commit checklist depends on it, and
   say so in the test name. Render `branch`, `fail_under` and `source` into
   `TESTING_GAPS.md`.

7. **T-M2** — parametrize the existing gating tests over **both** entry points
   and all four gates, then decide the `cmd_lockout` / schedule divergence
   explicitly and refactor the two guards into one predicate. Add an
   `operation.md` accuracy suite; the sensor-gating table there is a list of
   assertions waiting to be written.

8. **T-M1** — set `partial_branches = ["#\\s*pragma:\\s*no\\s+branch\\s*($|\\()"]`
   (verified: gate unchanged at `6775/2410 100.00%`, both real annotations still
   match, both prose matches gone), extend `coverage_config()` to return it, feed
   it to `find_prose_exclusions`, and add the falsifiability twin using
   `generate_gaps_report.py:550` as the fixture.

9. **T-M3** — extend the representability-bound test into a four-point
   parametrization (`+limit`, `+limit+1`, `-limit`, `-limit-1`) and add
   `-(10**400)` alongside `10**400` in the bad-value parametrization.

10. **T-L1** — one test measuring peak `inflight` across re-entrant pumps
    (release a single handler, assert `inflight <= max_inflight` on every loop
    turn until drained).

11. **T-L2** — assert the full rendered `_log_rejected` message, one case per
    `expected` spelling.

12. **T-L3** — `test_a_same_mtime_edit_still_reparses` using `os.utime(path,
    ns=...)` (the higher-value half), plus the `X = "a"if TYPE_CHECKING else "b"`
    span-end fixture.

**Library / packaging**

13. **F-M1** — add an empty `src/powerpetdoor/py.typed`. **Do not** add it to
    `[tool.setuptools.package-data]` on the grounds the report gives — setuptools
    already includes it (proven with a control). If you add a test, assert the
    marker is present in the built wheel, which is the thing that actually
    matters.

14. **B-F2** — pass the received frame length to `_bad_messages.record()` and
    `_device_errors.record()` instead of `len(json.dumps(msg))`, and build the
    string only when the throttle fires. Own the test churn: four harnesses need
    `**_kwargs`, and `tests/test_client.py:4160`'s `(96 bytes)` becomes `(87
    bytes)` — that assertion changing *is* the finding. Justify it on byte-total
    accuracy; the CPU saving is ~1.2 µs/frame and is not the reason.

**CLI ergonomics**

15. **F-L2** — fall back to `InMemoryHistory()` in the `except OSError` branch
    instead of proceeding to `FileHistory` with a path already known unusable.
16. **F-L1** — validate `--port`, the `--daemon` control port and `--host` with
    `parser.error(...)`; wrap the `asyncio.run(...)` in `except OSError` and
    print an operator sentence naming which port failed. Keep the traceback
    behind `--debug`.
17. **F-L4** — refuse an empty/whitespace-only command in `ctl.main()` before
    opening a socket; answer a blank line at the daemon rather than dropping it;
    give the control reader an explicit `limit=` and a bounded error message.
18. **F-T1** — validate `--timeout > 0` (or accept `0`/`none` as "wait forever"
    and pass `None`) and `--run-for > 0` in the parser.

**Docs / hygiene**

19. **F-M2 (surviving half only)** — document the queue-on-reconnect behaviour in
    `docs/door.md` and name it, and give the facade's `wait_for` timeouts a
    message (`TimeoutError(f"{CMD} timed out after {t}s")`). Do **not** make the
    facade raise `ConnectionError` when disconnected — that removes a deliberate
    round-1 behaviour the client, not the facade, implements.
20. **S-I1** — pick one (`graft tests` plus `conftest.py`, `tests/__init__.py`,
    `tests/fuzz/`, `tests/simulator/` **and** the test extras the shipped
    `addopts = "-n auto"` needs; or `prune tests`) and add a CI step that unpacks
    the built sdist and asserts the choice.
21. **`d12c693` follow-ups** (notes, not findings) — consider `${{
    github.event.before }}..${{ github.sha }}` so a multi-commit push is fully
    covered, and make an unresolvable `$base` fail the job instead of printing
    `OK: 0 file(s)` and exiting 0.

## Discarded

- **T-H1's `e03` (`fail_under = 100 → 50`) as a CI weakening** — **REFUTED by
  execution.** The real gate is `coverage report --fail-under=100`; a CLI option
  overrides the config file. Control at 67 % coverage: config-only exits **0**,
  `--fail-under=100` exits **2**. e03 weakens only local `pytest --cov`.
- **F-M1's `[tool.setuptools.package-data]` edit and its rationale** — **REFUTED
  by a clean build with a control.** `py.typed` is in the wheel without it;
  `zzz_control.txt` in the same directory is not. The marker is not "present in
  the source tree and absent from the wheel".
- **F-M2's sub-claim 3 ("the low-level client gets this right; the facade loses
  the diagnosis")** — **REFUTED by execution.** `client.send_message` queues and
  replays identically, and a `notify=True` future issued while never connected
  stays pending indefinitely (measured: still pending at 6.0 s). The 0.1 s
  `ConnectionError` in the report is `stop()`'s doing.
- **F-M2's headline remedy ("raise `ConnectionError` instead of enqueueing")** —
  rejected as a design change, not a bug fix. The flush-on-connect is deliberate
  (`_adopt_transport`, "Flush anything that was enqueued while disconnected
  (L3)", `21a463a`) and removing it is breaking for `ha-powerpetdoor`.
- **F-H1's "three tests pass while the binary does not"** — corrected to one.
  The two `ctl` tests assert an exit code the `ctl` binary really produces
  (verified 130 on a real pty).
- **F-H1's recommendation 3 (SIGTERM handler) as a defect** — SIGTERM already
  exits **143**; there is no false green on that path. Enhancement only.
- **S-M2's "an attacker deletes the record that documents the attack"** —
  qualified. Under default `raiseExceptions` the payload is echoed to stderr
  (`Arguments: ('\ud800SECRETPROBE',)`); it vanishes entirely only when
  `raiseExceptions` is `False`. The finding survives on the log *file* and on the
  ×27.6 unthrottled stderr amplification.
- **B-F2's CPU justification as the headline** — the isolated 1.2 µs/call
  reproduces, the "10–12 % of the whole receive path" rests on a measurement the
  report itself reports as noisy. The byte-total half is the finding.

## Numbers I would not quote

- **S-M1's "×6.06"** — I measure ×5.27; the ratio is a function of frame size,
  not of the site. The record counts (20 032 / 20 000 / 20 000 against 32 and 10)
  and the 65 638-byte single record reproduce exactly and are the defensible
  figures.
- **S-M1's "4.23 MB/s"** — an over-TCP derivative of the same ratio; the
  per-frame record count carries the same conclusion without a rate that depends
  on the peer's link and the frame chosen.
- **S-I1's "1 error, 0 tests"** — not reproducible. With the package installed I
  get `25 failed, 181 passed, 44 errors`; with a clean interpreter `pytest` will
  not start at all (`unrecognized arguments: -n`).
- **F-L2's "4 tracebacks and 15 'Press ENTER to continue' in a 3-command
  session"** — I measure 1 and 3 for the same three commands. The count scales
  with session length; the control (zero of each on a usable path) is the part
  worth quoting.
- **B-F1's "99.8 % of a burst drain"** — arithmetically correct but conditional
  on a raise that no input can currently produce. It describes the blast radius
  of a future regression, not present exposure.
- **T-M1's framing as an active hole** — the report is honest that nothing is
  lost today; I confirmed it by running the gate with `partial_branches`
  anchored and getting a byte-identical `6775 / 2410 / 100.00%`.
