# Test Fanatic Analysis — Round 5

## Summary

Everything below was verified by execution on `/tmp` copies, never on the repo.

**Baseline re-measured, not taken on trust:**

- `pytest -q` → **2171 passed in 27.8 s**.
- `pytest -q --cov` → **2171 passed, 100.00 %** (`6279/6279` lines,
  `2254/2254` branches).
- `pytest -q --ignore=tests/fuzz --cov` (the exact CI unit-matrix invocation) →
  **2134 passed, 100.00 % lines and branches**, same totals. Both invocations
  match the committed `tests/TESTING_GAPS.md` numbers exactly.
- `pytest -q -n0 -p no:cacheprovider` (serial, no xdist, no cache) →
  **2171 passed in 128 s**. No order- or parallelism-dependent test: the suite
  gives the same result distributed across workers and run start-to-finish in
  one process.
- `tests/TESTING_GAPS.md` is **accurate**: 5 pragma lines across 2 files is the
  real count (`grep` agrees), the per-category file counts (Core Library 7,
  Simulator 5, Simulator CLI 3, Simulator Commands 12, Build Scripts 1) are all
  correct, and every pragma reason renders in full — including the two with
  `()` inside them that R4-L2 was about.
- All five `# pragma` sites in `src/` re-read and judged: three in `ctl.py` are
  genuinely undrivable defensive handlers, two `no branch` in `cli.py` guard
  conditions that are provably always true after the preceding line. All are
  honestly justified.

Coverage is saturated and says nothing, so I **mutated**. Harness validated
first, exactly as round 4 was told to: `PYTHONPATH=<copy>/src` forcing the
mutated tree (verified by printing `powerpetdoor.__file__`), plus a control
mutation that must fail and did.

**123 real mutations executed** (2 more were malformed anchors and are excluded
from the counts), concentrated on the ~245 tests and the production machinery
round 4 introduced. Survivors were then **re-run against the entire suite**
before being reported, and the interesting ones were reproduced outside pytest
against the pristine source to prove they are not equivalent mutants.

| Outcome | Executions | Distinct mutations |
|---|---|---|
| Caught (a specific named test failed) | 88 | 88 |
| Detected only as a hang (no named failure) | 1 | 1 |
| Survived — meaningful | 26 | **21** |
| Survived — equivalent / unreachable | 8 | 5 |

(The execution count exceeds the distinct count because every survivor was
re-run a second time against the whole suite with no test selection.)

The hardened-on-purpose areas are excellent and I want to say so plainly:
**`framing.py` caught 14 of 16** and the two survivors are provably equivalent
(shown below); **the shared schedule coercers 12/12**; **`protocol.py`'s wire
coercers 6/6**; **`scripting.py`'s `set_script_paths_allowed` 3/3**; the
`_ConnectionAttempt` shim's identity checks 4/4; `_drop_connection` 3/3;
`_was_connected` 4/4; `ScriptQueue`'s claim accounting 11/14;
`generate_gaps_report`'s pragma scanner 8/10. Removing `sanitize_text` from
`render_result` is caught, so R4-M2 really is closed.

The 21 distinct meaningful survivors collapse into **13 findings**, clustering
into two themes, both of them "the fix is right, the test only proves the
shallowest case":

1. **Counters and defaults are tested at depth 1.** Every guard that counts
   (`_declined`, `_pending_direct_losses`) or defaults (`daysOfWeek`) is proven
   for exactly one value; replacing a decrement with a reset, or flipping a
   default, survives all 2171 tests while reintroducing the precise bug the
   guard exists for.
2. **The new fuzz properties are shaped so their post-conditions almost never
   run.** One of the 14 literally cannot fail. Measured, not guessed.

Finding counts: **Critical: 0, High: 0, Medium: 3, Low: 5, Trivial: 5**
(13 total).

---

## Findings

### Medium

---

#### R5-M1. `_declined` and `_pending_direct_losses` are only ever exercised at depth 1 and 2; turning either decrement into a reset survives all 2171 tests and reintroduces the exact R4-L1/L2 teardown-of-a-healthy-transport bug

**Severity:** Medium
**Files:** `src/powerpetdoor/client.py:1230-1249`;
`tests/test_client.py:2741-2754, 2832-2938`

Two mutations, both **SURVIVED the full suite** (confirmed on a second run with
no test selection at all):

| Mutation | Result |
|---|---|
| `self._declined -= 1` → `self._declined = 0` | **SURVIVED** |
| `self._pending_direct_losses -= 1` → `self._pending_direct_losses = 0` | **SURVIVED** |

These are not equivalent mutants. I reproduced both outside pytest against the
pristine tree and against the mutant:

*Two declined transports outstanding* (a live connection, then two rejected
`connection_made` calls, then their two `connection_lost` callbacks):

```
pristine:  declined=2 → loss1: declined=1, live=0 → loss2: declined=0, live=0   (healthy socket survives)
mutant:    declined=2 → loss1: declined=0        → loss2: falls through to _on_transport_lost
                                                    → _was_connected is True → tears the live socket down
```

*Three adopted transports outstanding* (`connect → disconnect` twice, then a
third connect, before asyncio delivers any of the losses):

```
pristine:  pending=3 → loss1: pending=2, live=3 → loss2: pending=1, live=3      (transport 3 survives)
mutant:    pending=3 → loss1: pending=0, live=3 → loss2: superseded=False
                                                    → disconnect()s transport 3 and burns a reconnect
```

That second sequence is exactly the failure R4-L1 was written to prevent, and
against a device that accepts **one** connection, burning a reconnect is not
cosmetic.

The existing tests cover the counters only at the depth where a decrement and a
reset are indistinguishable: `test_a_real_loss_after_a_declined_one_still_reconnects`
uses one declined transport, and `test_direct_path_ignores_a_superseded_transports_loss`
uses two adopted ones. Nothing exercises `_declined == 2` or
`_pending_direct_losses == 3`.

**Recommendation:** two tests, mirroring the two sequences above, asserting
that after *all* the stale losses land `client._transport is` the newest
transport, `client.available is True`, `client._reconnect_task is None`, and
that `"The server closed the connection"` never appears in the log. Both are
pure-sync `MockTransport` tests — under ten lines each.

---

#### R5-M2. The `--scripts-dir` containment check has no test at all; removing it lets a **bare script name over the unauthenticated control channel** follow a symlink out of the scripts directory

**Severity:** Medium
**Files:** `src/powerpetdoor/simulator/commands/scripts.py:172-181`;
`tests/simulator/test_commands.py:1252-1303`

```python
candidate = (base / f"{name}{suffix}").resolve()
# Belt and braces: never follow a resolved path out of the base dir
if candidate.is_file() and candidate.parent == base:
```

Dropping `and candidate.parent == base` **SURVIVED** the whole suite.

`TestScriptPathRestrictions` is thorough about the *lexical* rejections —
absolute paths, `../../etc/passwd`, backslashes, `.`-prefixed names — but every
one of those is stopped earlier, by `_load_script_restricted`. Nothing tests
the check that actually needs `resolve()`: a **bare, lexically innocent name**
whose resolved target is outside `base`. I demonstrated it:

```
scripts_dir/evil.yaml -> /tmp/r5probe/outside/secret.yaml   (symlink)

pristine, allow_script_paths=False:  load_script("evil")  →  ScriptError: Unknown script: evil
mutant,   allow_script_paths=False:  load_script("evil")  →  loads SECRET
```

`run evil` is accepted over the control channel (no slash, no dot, no
backslash), so the only thing between a symlink in the operator's scripts
directory and arbitrary YAML being parsed as a script is a line with zero test
coverage. The unrestricted path is worse still: `_load_script_by_name("../outside/secret")`
is reachable whenever `Path(script_ref).exists()` is False, and the mutant
loads it.

**Recommendation:** two tests against `restricted_handler`, both with a real
`tmp_path` scripts dir — one where `<dir>/evil.yaml` is a symlink to a file
outside it, one calling `_load_script_by_name("../outside/x")` directly — each
asserting `ScriptError`/`Unknown script` rather than a load. A `.yml` variant
costs one more parametrize entry and pins the loop's second suffix too.

---

#### R5-M3. The 14 new untrusted-input properties are shaped so their post-conditions almost never run — and one of them literally cannot fail

**Severity:** Medium
**File:** `tests/fuzz/test_untrusted_input_fuzz.py:51-88, 127-161, 201-217`

Round 4 asked for property coverage of the untrusted-input layer and got it.
But a property is only worth its runtime if it *draws* the values it is written
for, and these mostly do not. Three findings, all measured:

**(a) The `inf` totality hole the file's own docstring cites is not exercised.**
`coerce_schedule_int`'s `except (TypeError, ValueError, OverflowError)` exists
because `int(float("inf"))` raises `OverflowError` — the docstring says so in
as many words. Removing `OverflowError` from that tuple **SURVIVED the entire
fuzz suite**. I measured why:

| Draw from `_json_values` (600 examples) | Hits |
|---|---|
| top-level float of any kind | 9 |
| top-level **non-finite** float | **0** |
| non-finite float anywhere in the structure | 13 |
| `_time_payloads` with a non-finite `hour` | **0** |

`st.recursive` with `max_leaves=8` spends its budget on containers, and
non-finite floats are a rare draw inside a 5-way `one_of`. In 1000 examples I
never once saw the value the property exists to feed in.

**(b) `test_no_control_character_survives_and_it_is_idempotent` cannot fail.**
It checks `sanitize_text`'s output with `sanitize_text`'s *own*
`_CONTROL_CHAR_RE`. I narrowed the regex to `[\x00]` — so ESC, CSI and DEL now
pass through verbatim — and the property still passed, because it then searched
the output for `\x00` and found none. The deterministic `tests/test_sanitize.py`
catches this; the property that claims to guarantee it never could. By this
project's own rule ("no tests that cannot fail") that is a fake test.

**(c) The `in (True, False)` assertions do not pin the bool contract.**
`1 in (True, False)` is `True` in Python. Making `coerce_schedule_day` return
`int(flag)` **SURVIVED** — including `test_days_coercer_is_total`, which *does*
assert `all(isinstance(day, bool) ...)`, because that assertion is only reached
when `coerce_schedule_days` succeeds, and it succeeds on **18/600** draws — all
of them through the integer-bitmask branch. `_json_values` essentially never
produces a 7-element list of valid flags, so the protocol's actual `daysOfWeek`
shape is never parsed successfully by the property at all.

For contrast, the two parser properties are healthy: `Schedule.from_dict`
succeeds on ~45 % of draws, so their post-conditions genuinely run.

**Recommendation:**
1. Add a pathological leaf to the strategy and mix it in explicitly:
   `_pathological = st.sampled_from([float("inf"), float("-inf"), float("nan"), 1e400, -(2**64), 2**64])`,
   used as `st.one_of(_json_values, _pathological)` for the four scalar
   coercers. This makes (a) a real guarantee.
2. Give the coercers a *well-shaped* strategy alongside the hostile one —
   `st.lists(st.sampled_from([0, 1, "0", "1", True, False]), min_size=7, max_size=7)`
   for `daysOfWeek`, `st.fixed_dictionaries({"hour": st.integers(0, 23), "min": st.integers(0, 59)})`
   for times — so the success-path post-conditions actually execute.
3. Replace `x in (True, False)` with `isinstance(x, bool)` in all five places,
   and assert the declared range on the number coercers
   (`0 <= result <= 90000`), which is currently only type-checked.
4. Assert the sanitizer property against a literal control-character set that
   is *not* the module's own regex (e.g. `all(chr(c) not in out for c in [*range(9), 11, ..., 0x7f])`),
   plus the fail-closed direction: every escaped char appears as `\xNN`.

---

### Low

---

#### R5-L1. Both schedule parsers default an absent `daysOfWeek` to *all seven days on*, and that default is pinned on neither side; a negative bitmask silently activates all seven too

**Severity:** Low
**Files:** `src/powerpetdoor/door.py:312`,
`src/powerpetdoor/simulator/state.py:208`, `src/powerpetdoor/schedule.py:92-106`;
`tests/test_door.py:326-333`, `tests/simulator/test_state.py:521-526`

Four surviving mutations, all confirmed against the full suite:

| Mutation | Result |
|---|---|
| `door.py` default `[1,1,1,1,1,1,1]` → `[0,0,0,0,0,0,0]` | **SURVIVED** |
| `state.py` default `[1,1,1,1,1,1,1]` → `[0,0,0,0,0,0,0]` | **SURVIVED** |
| `coerce_schedule_days`: add `if value < 0 or value > 127: raise ValueError` | **SURVIVED** |
| `door.py` inside/outside prefix precedence swapped | **SURVIVED** (see R5-T4) |

Two tests do parse a payload with no `daysOfWeek` —
`test_from_dict_no_sensor_defaults_midnight` (`from_dict({})`) and
`test_entry_for_neither_sensor_keeps_the_placeholder_window`
(`from_dict({"index": 4})`) — but neither looks at `days_of_week`, so the
default is entirely unobserved on both sides.

The direction of the default matters. `coerce_schedule_flag`'s docstring states
the doctrine — *"an unreadable flag must never grant access"* — and the day mask
breaks it twice:

```
coerce_schedule_days(-1)    -> [True, True, True, True, True, True, True]   # every negative int
coerce_schedule_days(True)  -> [True, False, False, False, False, False, False]  # bool is an int
Schedule.from_dict({... no daysOfWeek ...}).days_of_week -> [True] * 7
```

The bitmask branch has exactly **one** happy-path test per side
(`0b0011111` in `test_state.py:187`, `62` in `test_door.py:318`) and no
boundary or negative case — no `0`, no `-1`, no `127`, no `128`, no `2**64`,
no `True`/`False`. That the range check above breaks nothing proves the whole
input domain is unconstrained by tests.

**Recommendation:** assert `days_of_week == [True] * 7` in the two
absent-field tests (both sides), and parametrize the bitmask branch over
`0 → all False`, `127 → all True`, `128 → all False`, `-1`, `True`, `False`.
Then decide the `-1` question deliberately: either document that a bitmask is
read modulo its low 7 bits, or reject `value < 0 or value > 127` the way every
other coercer rejects out-of-range input — and pin whichever you choose.

---

#### R5-L2. Nothing tests that a bare `stop` leaves the queue alone — the entire distinction `stop all` exists to draw

**Severity:** Low
**Files:** `src/powerpetdoor/simulator/commands/scripts.py:257-258`;
`tests/simulator/test_commands.py:1213-1249`

```python
if scope == STOP_ALL_KEYWORD and self.script_queue:
    dropped = self.script_queue.clear()
```

Deleting `scope == STOP_ALL_KEYWORD and` **SURVIVED the full suite** — i.e. a
plain `stop` silently draining every queued run is indistinguishable from
correct behaviour to all 2171 tests.

`TestScriptBusyVisibility` has three `stop all` tests (queue drained, drained
with nothing running, nothing at all) and `test_stop_rejects_an_unknown_scope`,
but no test issues a bare `stop` while runs are queued. The feature's whole
premise — that `stop` stops the running script and *only* `stop all` touches
the queue — is unasserted.

**Recommendation:** one test: start a blocking script, queue two runs, execute
`stop`, then assert `result.message == "Stopping script: Slow Script"`
(no `(dropped N queued)` suffix) **and** `script_queue.qsize() == 2`. The exact
message matters here — `test_stop_all_drains_the_queue_in_one_command` already
asserts the full string on the other branch, so this stays symmetric.

---

#### R5-L3. The `on_start=` release — the substance of the M2 claim-accounting fix — is unverified; the test named after it cannot tell the two release sites apart

**Severity:** Low
**Files:** `src/powerpetdoor/simulator/cli.py:391-400`;
`tests/simulator/test_cli.py:1207-1245`

Dropping `on_start=lambda: script_queue.release(script_ref)` from the queue
consumer **SURVIVED**. `test_claim_is_released_only_once_the_run_starts` reads
as if it pins this, but it cannot: its stub calls `on_start()` and then
immediately returns, so by the time `await started.wait()` resumes the test the
consumer has already run its `finally: script_queue.release(...)`. The final
`assert queue.qsize() == 0` is satisfied by the `finally` alone.

What the test *does* prove — that a dequeued-but-not-started run still counts
(`depth_while_blocked == [1]`, `pending() == ["waiting"]`) — is real and
valuable. Only the "released on start, not on finish" half is vacuous.

**Recommendation:** record the depth inside the stub immediately after
`on_start()` and assert it there:

```python
async def run(script, on_start=None):
    depth_while_blocked.append(queue.qsize())
    await release.wait()
    if on_start is not None:
        on_start()
    depth_after_start.append(queue.qsize())   # 0 only if on_start released it
    started.set()
    return True
...
assert depth_after_start == [0]
```

With `on_start` dropped this reads `[1]` and fails by name.

---

#### R5-L4. Six defensive branches whose *only* observable is a warning are covered but not asserted; four of the tests that reach them have no assertion at all

**Severity:** Low
**Files:** `src/powerpetdoor/client.py:1482, 1594, 1606`,
`framing.py:245-248`, `tz_utils.py:154`, `simulator/protocol.py:650`;
`tests/test_client.py:1357-1367, 1704-1708`

Deleting the log call (keeping the control flow) survived in six places:

| Site | Message deleted | Result |
|---|---|---|
| `client.process_message` | `Ignoring non-object message from device: %r` | **SURVIVED** |
| `client.process_message` | `Ignoring malformed message from device: %s` | **SURVIVED** |
| `client._send_data` | `Connection closed while waiting to send; dropping message` | **SURVIVED** |
| `framing.FrameScanner.feed` | `Discarded %d bytes of non-JSON garbage...` | **SURVIVED** |
| `tz_utils` | `Timezone cache not initialized, returning empty list` | **SURVIVED** |
| `protocol._handle_set_schedule` | `Simulator: Rejected schedule: %s` | **SURVIVED** |

For the first two the log is not merely *an* observable, it is the **only**
one — and the tests that reach them contain no assertions whatsoever:

```python
async def test_non_dict_message_dropped(self, mock_client):
    """A non-dict message is dropped quietly."""
    client, _, _ = mock_client
    await client.process_message("not a dict")     # nothing is asserted
```

A test that can only fail by raising is not testing "dropped quietly"; it is
testing "did not explode". An operator debugging a misbehaving door has exactly
one signal that a frame was thrown away, and its wording is unpinned. This
project already asserts message *text* elsewhere (`"Rejecting a second
connection" in caplog.text`, `"Received non-ASCII bytes from device" in
caplog.text`) — these six sites are the ones that missed out.

There are 13 assertion-free tests in total; the other nine are legitimate
"must not raise" totality checks with a genuinely different point
(`ctl._enable_line_buffering(object())` and friends), and I am not raising those.

**Recommendation:** add `caplog` and one substring assertion to each of the six.
For the two `process_message` cases also assert `client._tasks == set()`, which
pins "dropped" rather than merely "logged".

---

#### R5-L5. Three `tz_utils` tests accept hundreds of different answers where an exact, tzdata-stable invariant is available

**Severity:** Low
**File:** `tests/test_tz_utils.py:239-256, 522-529`

```python
result = tz_utils.find_iana_for_posix(posix)
assert result is not None
assert result.startswith("America/")
```

`find_iana_for_posix` is a dict lookup, so this has one deterministic answer
per tzdata build — and it is not the one the test name implies. On this machine:

```
America/New_York    -> EST5EDT,M3.2.0,M11.1.0        -> America/Detroit
America/Los_Angeles -> PST8PDT,M3.2.0,M11.1.0        -> America/Ensenada
Europe/Berlin       -> CET-1CEST,M3.5.0,M10.5.0/3    -> Africa/Ceuta
```

`startswith("America/")` accepts roughly 150 zones and would pass for
`America/Argentina/Buenos_Aires`. The hedge against tzdata drift is
understandable, but it is unnecessary — the *round trip* is exact and stable:
`get_posix_tz_string(find_iana_for_posix(p)) == p` holds for every zone I
checked, and it is precisely the invariant the reverse map is supposed to
provide.

`test_european_timezones` is weaker still: its docstring says "Berlin uses
CET/CEST" and its only assertion is `parsed["std_abbrev"] is not None`. Its US
sibling twelve lines above does assert `std in posix` and `dst in posix`.

**Recommendation:** replace the `startswith` assertions with
`assert tz_utils.get_posix_tz_string(result) == posix`, and in
`test_european_timezones` assert `parsed["std_abbrev"] == "CET"` and
`parsed["dst_abbrev"] == "CEST"` — stable for decades, and what the docstring
already claims.

---

### Trivial

---

#### R5-T1. Three unguarded spin-waits in `TestProcessScriptQueue` turn a regression into a 60 s hang instead of a named failure

**Severity:** Trivial
**File:** `tests/simulator/test_cli.py:1201, 1238, 1269`

Ten `while <cond>: await asyncio.sleep(0)` loops exist in the suite. Seven are
wrapped in `async with asyncio.timeout(...)`; the three in
`TestProcessScriptQueue` are not. I hit this for real: my mutation removing the
consumer's `finally: script_queue.release(script_ref)` made
`while queue.qsize(): await asyncio.sleep(0)` (line 1269) spin until my
harness's 600 s cap — the only run in 123 mutations that did not produce a
result. In CI it would surface as a `pytest-timeout` kill, which is exactly the
failure mode R3-M4 and R4-M4 were about.

**Recommendation:** wrap all three in `async with asyncio.timeout(5):`, matching
the seven that already are.

---

#### R5-T2. `total_pragma_entries` in `generate_gaps_report.py` is computed and never read

**Severity:** Trivial
**File:** `scripts/generate_gaps_report.py:323, 325`

Initialised and `+=`-accumulated, then never used in any output line —
`grep -c` finds exactly two occurrences. Ruff's F841 does not flag it because
the augmented assignment counts as a read. Deleting the `+=` line **SURVIVED**
(deleting the initialiser is caught, by `NameError`). Two lines inside the
100 % gate that the gate cannot say anything about.

**Recommendation:** either render it (`"**N lines** across **M files** in **K
annotations**"`) or delete both lines.

---

#### R5-T3. The report's green/yellow category threshold is untested

**Severity:** Trivial
**File:** `scripts/generate_gaps_report.py:265`; `tests/test_gaps_report.py:419`

`cat_pct >= 99.5` → `cat_pct >= 95.0` **SURVIVED**. The fixtures exercise only
80 % (yellow) and 100 % (green), so any threshold in `(80, 100]` renders
identically. Cosmetic, but it is a number in a file that is 100 %-gated
precisely because "it contains real logic that can silently misreport".

**Recommendation:** one fixture at 99.0 % asserting `:yellow_circle:` and one at
99.6 % asserting `:green_circle:`.

---

#### R5-T4. The inside/outside prefix precedence is untested on both parsers

**Severity:** Trivial
**Files:** `src/powerpetdoor/door.py:317-322`,
`src/powerpetdoor/simulator/state.py:214-219`

Swapping `if inside: ... elif outside:` to `if outside: ... elif inside:`
**SURVIVED the full suite on both parsers**. It only matters for a payload with
both flags set *and* different `in_*`/`out_*` windows, which
`docs/protocol.md` declares out of spec ("Each schedule controls ONE sensor").
But `schedule add both` produces both-flag entries in-project, so the branch is
live, and both twins would drift silently.

**Recommendation:** one shared-shape test per side: both flags true, `in_*` =
06:00–07:00, `out_*` = 20:00–21:00, assert the inside window wins. Cheap, and it
documents a rule that is currently only implied by statement order.

---

#### R5-T5. Two test docstrings/comments state things that are not true

**Severity:** Trivial
**Files:** `tests/test_client.py:2943`, `tests/simulator/test_state.py:187-200`

- `test_shim_ignores_a_shutdown_declined_transports_loss` says *"The shim's
  `_adopted` guard is the only guard on this path (R4-L1)"*. It is not: I
  removed the `_adopted` check alone and the full suite still passed, because
  `_on_transport_lost`'s `_was_connected` early return also covers this path
  (`client.shutdown()` on a never-connected client leaves `_was_connected`
  False). A future maintainer reading that docstring would conclude the
  `_was_connected` return is redundant here, which is backwards.
- `test_from_dict_legacy_bitmask` asserts `days_of_week == [1, 1, 1, 1, 1, 0, 0]`
  with the comment *"converts to list [1, 1, 1, 1, 1, 0, 0]"*. `from_dict`
  returns real `bool`s (via `coerce_schedule_days`), and `True == 1`, so the
  assertion accepts either representation. Its library-side twin
  (`test_door.py:318`) asserts `all(isinstance(day, bool) ...)`. Given that
  R4-M3/T3 were specifically about "normalised to a real bool", the simulator
  side should say so too.

**Recommendation:** correct the first docstring to name both guards (the
non-vacuity argument still stands — see the verification section); add
`assert all(isinstance(day, bool) for day in schedule.days_of_week)` to the
second and fix its comment.

---

## Round 4 Fix Verification

Every round-4 finding re-checked by re-running the mutation that originally
found it, or by reading the code the fix produced.

| Finding | Status |
|---|---|
| **R4-M1** — `door.Schedule/ScheduleTime.from_dict` rebuilt on the shared coercers | **Fixed, and strongly.** All 12 of my mutations against `schedule.py`'s coercers and all 9 against `door.Schedule.from_dict` behaved: range checks, the `OverflowError` catch, `make_bool`-not-truthiness, `require_schedule_field`, the 7-element length check, the `hour`-is-required rule and the hour/minute bounds are each caught by a named test. 30 hostile-input cases with exact `match=` strings. The `_inside_payload` helper keeps the library and simulator suites symmetric. |
| **R4-M2** — `render_result` never asserted to sanitise | **Fixed.** `return f">>> {sanitize_text(message)}"` → `f">>> {message}"` is **caught**. |
| **R4-M3** — day-flag `make_bool` pinned at one of three sites | **Fixed.** Now a single shared `coerce_schedule_day`, and `flag = bool(value)` in place of `make_bool(...)` is caught at every entry point (12 + 13 + 12 parametrized cases). |
| **R4-M4** — `aclose()` and its timeout | **Fixed.** `test_aclose_honours_its_timeout_argument` plus three siblings; `door.disconnect()` routes through `aclose(self.default_timeout)`. |
| **R4-L1** — `_adopted` guard survives removal | **Confirmed equivalent for `connection_lost`, and I agree with the reasoning — with one correction.** Removing `if not self._adopted: return` alone still passes the full suite, and my own analysis says it must: a shutdown-declined transport leaves `client._transport is None` and `_was_connected` False, so `_on_transport_lost`'s early return catches it; a second-connection decline leaves `client._transport` pointing at the live socket, so the identity check catches it. The double mutation the fix agent used is the right non-vacuity proof. **However, "and vice versa" is not right**: `_was_connected` is *not* subsumed by `_adopted` — it is the only guard for an *adopted* transport lost after a local teardown, and removing it alone **is** caught (`test_disconnect_then_connect_does_not_report_a_server_close`). The relationship is one-way. Also note `_adopted` is *not* redundant in `data_received` — dropping it there is caught by `test_shim_ignores_a_declined_transports_lifecycle_events`. So keeping the field is right; only the test docstring overstates it (R5-T5). |
| **R4-L2** — gaps-report pragma reason truncation | **Fixed twice over, and the fix is well tested.** 8 of my 10 mutations against the scanner are caught, including the anchored `reason_re`, the `tokenize`-vs-regex choice (`COMMENT` → `COMMENT or STRING` is caught), the tokenize-failure fallback, the grouping predicate and the `code_before_pragma` slice. The committed `TESTING_GAPS.md` renders both paren-containing reasons in full. |
| **R4-L3** — `WireCapture` framing untested | **Fixed.** `tests/simulator/test_wire.py` — 9 tests including byte-at-a-time reassembly and brace-in-string; `WireCapture` now uses the production `FrameScanner`, so the helper 40+ integration tests depend on is itself pinned. |
| **R4-L4** — gap threshold / ordering / denominator | **Fixed for the three named cases** (`test_near_complete_files_are_still_listed_as_gaps`, `test_gap_files_are_listed_worst_first`, the `Lines Covered` row). The *category* status threshold is a different constant and is still unpinned — R5-T3. |
| **R4-L5** — fuzz suite did not grow | **Fixed in coverage, weak in power.** The 14 new properties exist and 7 of my 11 fuzz-only mutations are caught by them. Three are not, for the reasons in R5-M3. |
| **R4-L6** — 11 `test_cli.py` tests reaching the real `run_simulator` | **Fixed.** All 19 `cli.main()` call sites are inside `TestMainArguments`, whose autouse `never_runs` fixture makes reaching `run_simulator` an immediate `AssertionError`. |
| **R4-T1** — `scripts/` outside the pragma scan | **Fixed.** `main()` scans both roots; `test_scripts_root_is_scanned_too` pins it, and dropping the `root=` parameter is caught. |
| **R4-T2** — `_track_task` vs `ensure_future` | **Fixed.** Pinned at both call sites. |
| **R4-T3** — `transport.abort()` in the shutdown-decline path | **Fixed.** `assert refused.aborted is True` with an explicit "abort(), not close()" comment. |
| **Harness discipline** | **Heeded and correct.** I re-derived it independently: without `PYTHONPATH=<copy>/src` the venv's editable `.pth` shadows the `/tmp` tree and every mutation "survives". My control mutation was killed before I trusted a single result. |

---

## Areas Reviewed With No Findings

- **`framing.py` / `FrameScanner` state carrying — 14/14 caught.** Every piece
  of the resumability contract is pinned by a named test: not persisting
  `_scanned`, not persisting `depth`/`in_string`/`escaped` across `feed()`
  calls, the escape-inside-string state machine, the resync byte accounting
  (both the "no next `{`" and "next `{` found" arms), whitespace consumption,
  the `>` vs `>=` overflow boundary, the reset-on-overflow, `find_frame_end`'s
  `s[0] != "{"` precondition and its `scanner.open` check. The two survivors
  are provably equivalent and I am not reporting them: `depth <= 0` vs
  `depth == 0` (depth can never be negative at that point — `scan()` is only
  entered at a `{` or with `depth > 0`), and the `consumed = i` before
  `scanner.scan` (by the loop's own invariant `consumed == i` already holds at
  every path that reaches it). The `TestScannerLinearityProperties` pair, which
  counts characters examined, is the best property in the suite.
- **Shared schedule coercers — 12/12 caught**, and `protocol.py`'s wire
  coercers **6/6** (`bool`-is-not-a-number, `math.isfinite`, both range arms,
  the string length cap, `make_bool`-not-truthiness, the `int()` truncation).
- **`_drop_connection` / `_was_connected` — 7/7 caught.** Renaming
  `_drop_connection`, dropping its `disconnect()`, dropping its reconnect,
  making its `_shutdown` check unconditional, dropping `_was_connected = True`
  on adopt, forcing `was_connected = True` in `disconnect()`, dropping the
  reset, and forcing the `_on_transport_lost` early return — every one fails a
  named test.
- **`_ConnectionAttempt` identity checks — 4/4 caught** (`is not` → `is`,
  removing the check, removing the `data_received` guard, and the
  second-connection decline returning `True`).
- **`set_script_paths_allowed` — 3/3 caught**, including the ctl entry point
  and both completer branches.
- **`ScriptQueue` claim accounting — 11/14 caught**, covering `qsize()`
  including claims, `pending()` ordering, `clear()` sparing claims, the arrival
  `Event.clear()`, `is_wait_run`'s arity, the wait-run-never-queues rule, the
  `stop already requested` branch, the dropped-count suffix and the
  `format_script_status` queued suffix. `release()` → `_claimed.clear()`
  survives but is unreachable: there is exactly one consumer, so `_claimed`
  never holds more than one entry.
- **Autouse fixture interactions — no leaks found.** I traced all four:
  `_managed_main_thread_event_loop` (session), `_reset_extra_scripts_dir`
  (resets both module globals after every test), `_command_registry_guard` in
  `test_cli.py`, `cli_mode_guard` in `test_commands.py`, and the explicit
  `registry_guard` in `test_commands_handler.py` — which correctly saves and
  restores `handler_mod._saved_exit_info` as well as the registry dict. Every
  `set_cli_mode(True)` in the suite is inside one of those guards. The three
  are independent (different files, different scopes) and do not interact.
- **Order and parallelism independence.** `pytest-randomly` is not installed,
  so within a worker the order is fixed — but xdist's `--dist load` already
  varies which tests share a process, and a full serial `-n0` run passes
  identically (2171/2171). Combined with the fixture audit above, I found no
  cross-test state leak.
- **Port and xdist hygiene.** Every server binds port 0 and reads back
  `getsockname()`; the five hard-coded `port=3000` sites never connect
  (mock transport / constructor-only). The `refused_port` fixture holds its
  binding for the test's lifetime, so a parallel worker cannot be handed the
  same number. `framing._BraceScanner.scan` is monkeypatched in the fuzz suite
  but restored in a `finally`, and xdist workers are separate processes.
- **Wall-clock timing.** Only two `asyncio.sleep` values above 0 exist in the
  whole suite, both in `test_ctl.py`, both with a documented ≥10× margin and a
  comment explaining the ratio. Everything else is event- or yield-driven.
- **Skips.** Ten `skipif`s, all of them optional-dependency gates
  (`YAML_AVAILABLE`, `PROMPT_TOOLKIT_AVAILABLE`); both extras are in the `dev`
  group so all ten run in CI. No unconditional skips, no `xfail`.
- **Fake tests.** No `assert True`, no tautologies, no read-back-what-you-set.
  The only `assert x in (a, b)` forms are in the fuzz suite and are totality
  assertions (R5-M3 addresses their strength, not their honesty).
- **`# pragma` audit.** Five lines, two files, each with a specific reason, and
  the two `no branch` sites guard conditions that are dead by construction —
  a defensible choice either way.
- **CI matrix.** Python 3.11–3.14 matches `pyproject.toml`'s
  `requires-python`, classifiers, ruff `target-version` and mypy
  `python_version`; `REFERENCE_PYTHON` is 3.14; every job carries
  `timeout-minutes: 30`. The `Operating System :: MacOS` classifier is still
  untested by CI, but round 3/4 reviewed that deliberately and I am not
  reopening it.
