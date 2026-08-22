# Test Fanatic Analysis — Round 8

Commit audited: `da31ae2` ("Round 7 fixes (refuter-approved list only)").
Baseline as found: 2620 tests, 100.00% lines + branches on both invocations,
ruff/mypy clean.

## Method (and the harness guard)

Every claim below was produced by executing a mutant of the tree, not by
reading it. The harness:

1. **Pristine copy, nothing omitted.** The whole working tree (minus `.git`,
   `.venv`, caches and coverage artefacts) is copied to `/tmp/r8base` with
   `tar`, so `README.md`, `docs/`, `pyproject.toml` and `tests/TESTING_GAPS.md`
   are all present. Round 7's fix agent found that an incomplete copy list makes
   every mutant look "CAUGHT" via collection errors; a `tar` of the tree cannot
   drift the way an enumerated list can.
2. **Import root asserted inside the pytest process.** `/tmp/r8plugin/guardplugin.py`
   is loaded with `-p guardplugin` and raises unless
   `powerpetdoor.__file__.startswith($R8_EXPECT_ROOT)`; it prints the resolved
   path to stderr on every worker. `PYTHONPATH=$WORK/src` is forced.
   Every result below carries a `[guard] powerpetdoor.__file__=/tmp/r8work/<name>/...`
   line; runs without it are discarded as harness failures.
3. **Null control.** An *unmutated* copy is run through the identical path,
   first and last:

   ```
   $ cp -a /tmp/r8base /tmp/r8work/null && /tmp/r8/run.sh /tmp/r8work/null -q
   2620 passed in 39.20s
   $ cp -a /tmp/r8base /tmp/r8work/null2 && /tmp/r8/run.sh /tmp/r8work/null2 -q
   2620 passed in 46.99s
   ```

4. **Control mutation that must fail.**

   ```
   $ /tmp/r8/mut.sh control_must_fail src/powerpetdoor/framing.py \
       "MAX_INFLIGHT_FRAMES = 64" "MAX_INFLIGHT_FRAMES = 63"
   [guard] powerpetdoor.__file__=/tmp/r8work/control_must_fail/src/powerpetdoor/__init__.py
   FAILED tests/test_framing.py::TestShippedResourceBoundsHaveTheirValuesPinned::test_the_inflight_bound_is_64
   1 failed, 2417 passed in 48.55s
   ```

61 mutations were run against the **full** suite (no `-k`, no subsetting).
14 survived; 3 of those are argued equivalent below and are not reported.

## Summary

| Severity | Count |
|----------|-------|
| High     | 1 |
| Medium   | 5 |
| Low      | 5 |
| **Total**| **11** |

- **H1** — `coverage.report.exclude_lines` still over-matches prose: 3 shipped
  statements are outside the 100% gate today, and brand-new never-executed code
  passes the gate at 100.00%. Third instance of the class (bare `...`, round 6;
  the prose re-exclusion the round-7 fix agent self-caught; this).
- **M1** — `FrameDispatcher._schedule_pump`'s one-continuation guard is not
  observed by the test named after it (1 → 1000 armed continuations, suite green).
- **M2** — `MIN_BLOCKED_RECHECK`'s *purpose* is untested: removing the floor
  busy-spins the engine (2 → 33,874 loop iterations, 0.3 ms → 296.5 ms CPU per
  0.30 s window) with the suite green.
- **M3** — `_coerce_wire_number`'s inclusive upper bound is unpinned: `<=` → `<`
  rejects exactly the documented maximum on four wire fields, suite green.
- **M4** — `_ACTION_PARAMS` can gain a parameter its action never reads; a typo'd
  script then reports PASSED, which is the exact round-7 frontend L3 defect.
- **M5** — `disconnect()` flushes four throttles; only two of them are covered.
  Dropping `_device_errors` or `_bad_messages` loses the tail and leaks the count
  into the next connection, suite green.

## Findings

---

### H1 (High) — `exclude_lines` is a bare-phrase `re.search`, and it is silently removing shipped statements from the 100% gate

**File:** `/home/prez/src/pypowerpetdoor/pyproject.toml:117-131`
(`[tool.coverage.report] exclude_lines`), affecting
`/home/prez/src/pypowerpetdoor/scripts/generate_gaps_report.py:33`, `:376`, `:407`.

**Reproduction**

Step 1 — what the gate is excluding *today*, straight out of the committed
`coverage.json`:

```
$ .venv/bin/python -c "
import json
d = json.load(open('coverage.json'))
print('scripts/generate_gaps_report.py:', d['files']['scripts/generate_gaps_report.py']['excluded_lines'])"
scripts/generate_gaps_report.py: [33, 79, 376, 407, 420, 421]
```

`420, 421` is the intended `if __name__ == "__main__":` guard. The other four are
not intended. Matching each configured pattern against every gated source line
shows why:

```
$ .venv/bin/python - <<'EOF'
import re, tomllib, pathlib
pats = tomllib.loads(pathlib.Path('pyproject.toml').read_text())['tool']['coverage']['report']['exclude_lines']
roots = list(pathlib.Path('src/powerpetdoor').rglob('*.py')) + list(pathlib.Path('scripts').rglob('*.py'))
for p in pats:
    rx = re.compile(p)
    for f in roots:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if rx.search(line):
                print(f"{p!r:28} {f}:{i} | {line.strip()[:90]}")
EOF
'pragma: no cover'           src/powerpetdoor/simulator/ctl.py:657 | except asyncio.CancelledError:  # pragma: no cover (defensive: ...
'pragma: no cover'           scripts/generate_gaps_report.py:34  | "pragma: no cover": "Explicitly annotated lines (see Pragma Exclusions below)",
'pragma: no cover'           scripts/generate_gaps_report.py:83  | literal words "# pragma: no cover" inside string literals, and a raw
'pragma: no cover'           scripts/generate_gaps_report.py:379 | f"are excluded via `# pragma: no cover` or `# pragma: no branch`."
'pragma: no cover'           scripts/generate_gaps_report.py:407 | lines.append("No `# pragma: no cover` or `# pragma: no branch` annotations found.")
'def __repr__'               scripts/generate_gaps_report.py:35  | "def __repr__": "String representation methods",
'raise NotImplementedError'  scripts/generate_gaps_report.py:36  | "raise NotImplementedError": "Abstract method stubs",
'if TYPE_CHECKING:'          scripts/generate_gaps_report.py:37  | "if TYPE_CHECKING:": "Type-checking-only imports",
'@overload'                  scripts/generate_gaps_report.py:39  | "@overload": "Typing overload declarations",
```

Six of the seven patterns match the `_EXCLUSION_NOTES` dict — the table whose
entire job is to *disclose* the exclusions — collapsing to the statement at
line 33. Lines 379 and 407 are the two `lines.append(...)` statements that render
the Pragma Exclusions section (statement starts 376 and 407).

Step 2 — the matched pair that proves the gate is porous. Two identical mutants,
each adding one never-called function to `scripts/generate_gaps_report.py`; they
differ only in whether the returned string contains the phrase.

```
# MUTANT A - the returned string mentions the phrase in prose
def _audit_hole_never_called() -> str:
    """A statement no test executes, on a line holding the phrase in prose."""
    return "this line mentions pragma: no cover in a string, not a comment"

$ /tmp/r8/run.sh /tmp/r8work/covhole -q --cov --cov-report=term-missing
scripts/generate_gaps_report.py                          236      0     76      0  100.00%
TOTAL                                                   6663      0   2368      0  100.00%
Required test coverage of 100.0% reached. Total coverage: 100.00%
2620 passed in 48.09s
```

```
# MUTANT B - byte-identical except the phrase is gone from the string
def _audit_hole_never_called() -> str:
    """Identical control, with the phrase removed from the returned string."""
    return "this line mentions nothing special in a string, not a comment"

$ /tmp/r8/run.sh /tmp/r8work/covhole_ctl -q --cov --cov-report=term-missing
scripts/generate_gaps_report.py                          237      1     76      0   99.68%   194
TOTAL                                                   6664      1   2368      0   99.99%
FAIL Required test coverage of 100.0% not reached. Total coverage: 99.99%
2620 passed in 45.63s
```

Step 3 — the three currently-excluded statements are, today, still exercised by
tests, so removing the over-match does not break the gate:

```
# exclude_lines' "pragma: no cover" replaced by "#\s*pragma:\s*no\s+cover"
$ /tmp/r8/run.sh /tmp/r8work/covfix -q --cov --cov-report=term-missing
TOTAL                                                   6662      0   2368      0  100.00%
Required test coverage of 100.0% reached. Total coverage: 100.00%
2620 passed in 55.99s
```

**Description**

`coverage.py` applies each `exclude_lines` entry as `re.search` against the whole
source line, so a bare phrase matches comments, docstrings, f-strings and dict
keys equally. Round 6 (test-fanatic H2) removed exactly this defect from the
bare `...` pattern and anchored *that one*. The remaining six were left bare.
The round-7 fix agent then hit the same shape a second time — its own replacement
comment wrote the literal phrase in prose and silently re-excluded the line it
had just restored — and mitigated it with a human convention: a comment in
`src/powerpetdoor/simulator/ctl.py:375-378` saying "Do not write the exclusion
phrase itself in prose here".

That convention is already violated, in the file whose purpose is to describe the
project's coverage exclusions, and `scripts/` is inside `coverage.run.source` and
inside the CI gate (`.github/workflows/test.yml:203-204`,
`coverage report --fail-under=100`). Three real statements are outside the gate
right now, and mutant A shows the hole admits arbitrary new dead code.

Two aggravating details:

- The hole is unbounded going forward. Any future line in `src/` or `scripts/`
  containing `def __repr__`, `raise NotImplementedError`, `if TYPE_CHECKING:`,
  `@overload` or the pragma phrase *anywhere* — an error message, a log string,
  a docstring — silently leaves the gate. `def __repr__` and
  `raise NotImplementedError` in a message string are entirely plausible.
- `tests/TESTING_GAPS.md`, the project's own disclosure artefact, says
  "**3 lines** across **2 files** in **3 annotations** are excluded". That count
  covers pragma *comments* only. The three prose-triggered exclusions are
  disclosed nowhere, so the artefact under-reports the gate's real perimeter.

**Recommendation**

1. Anchor every pattern that is meant to match a *comment* to a comment:
   `"#\\s*pragma:\\s*no\\s+cover"`. Anchor the structural ones to the start of a
   line: `"^\\s*@overload\\s*$"`, `"^\\s*if TYPE_CHECKING:"`,
   `"^\\s*def __repr__"`, `"^\\s*raise NotImplementedError"`,
   `"^\\s*if __name__ == .__main__.:"`. Verified above that the suite still
   reaches 100.00% with the pragma pattern anchored, so this is a no-op on the
   current tree.
2. Add the test that would have caught all three instances of this class, in
   `tests/test_gaps_report.py::TestAgainstTheRealRepository`: compile each
   configured `exclude_lines` pattern, sweep every gated source file, and assert
   that every match is a real exclusion — i.e. that the matched line is a comment
   token (via `gaps._comment_tokens`, which already exists and already solves
   this problem for the report) or a genuine `@overload`/`TYPE_CHECKING`/stub
   line. Coverage's own instrumentation is the one thing in this repo that no
   test currently checks against the source it instruments.
3. Have `generate_gaps_report.py` disclose the prose-triggered exclusions too, so
   `TESTING_GAPS.md` reports the gate's real perimeter rather than its intended one.

---

### M1 (Medium) — the "only one continuation" guard is unobserved; the test named after it cannot fail on it

**File:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/framing.py:620-625`
(`FrameDispatcher._schedule_pump`);
test at `/home/prez/src/pypowerpetdoor/tests/test_framing.py:1152-1174`
(`test_only_one_continuation_is_ever_scheduled`).

**Reproduction**

Mutation — delete the dedupe guard, keeping the flag write:

```python
    def _schedule_pump(self) -> None:
        self._pump_scheduled = True
        asyncio.get_running_loop().call_soon(self._resume_pump)
```

```
$ /tmp/r8/batch.py  # m05_no_dedupe_schedule
=== m05_no_dedupe_schedule: SURVIVOR (rc=0)
    2620 passed in 45.20s
```

The behaviour the test's name and docstring claim to pin, measured by counting
`loop.call_soon(self._resume_pump)` in exactly the scenario that test sets up
(`submit(["{1}".."{4}"])` then `submit(["{5}","{6}"])`, `max_inflight=2`):

```
--- ORIGINAL ---
framing from: /tmp/r8base/src/powerpetdoor/framing.py
continuations armed after two submits: 1
dispatched: ['{1}', '{2}', '{3}', '{4}', '{5}', '{6}']
_pump_scheduled at end: False
--- MUTANT m05 (dedupe guard removed) ---
framing from: /tmp/r8work/m05_no_dedupe_schedule/src/powerpetdoor/framing.py
continuations armed after two submits: 2
dispatched: ['{1}', '{2}', '{3}', '{4}', '{5}', '{6}']
_pump_scheduled at end: False
```

All three of the test's assertions (`_pump_scheduled is True` after each submit,
the dispatch order, `_pump_scheduled is False` at the end) hold identically at
1 and at 2. At the scale a hostile peer produces:

```
# 1000 reads of 100 unparseable frames each, all inside one loop turn
--- ORIGINAL ---   continuations armed by 1000 stalled reads: 1
--- MUTANT ---     continuations armed by 1000 stalled reads: 1000
```

**Description**

`_pump_scheduled` is the only thing bounding how many `call_soon` continuations
a stalled backlog can arm. Without it the loop's ready queue grows linearly with
the peer's *read* count — which is the per-read unbounded scheduling that round-7
security M1 introduced `_pump()` to remove in the first place. The test written
to pin it observes the *flag*, and the flag is `True` whether one continuation is
pending or a thousand, so the assertion is satisfied by the defect. This is the
same shape as the round-7 `flush()` finding: a test whose stated contract is not
reachable from its assertions.

**Recommendation**

Count the continuations, not the flag. In
`test_only_one_continuation_is_ever_scheduled`, wrap `loop.call_soon` (or count
`_resume_pump` invocations) and assert exactly `1` after the second `submit()`,
keeping the existing order/flag assertions as they are. A companion case at scale
(N stalled submits ⇒ 1 continuation) makes the bound explicit.

---

### M2 (Medium) — `MIN_BLOCKED_RECHECK` exists to stop a busy-spin, and nothing tests the spin

**File:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/engine.py:634`
(and the constant at `:74`).

**Reproduction**

Mutation — drop the floor, keeping everything else:

```python
await self._wait_for_wake(float(self.state.hold_time))
```

```
=== m22_no_min_recheck_floor: SURVIVOR (rc=0)
    2620 passed in ...
```

(For contrast, `MIN_BLOCKED_RECHECK = 0.1 -> 1.0` **is** caught:
`=== m21_min_blocked_recheck: caught (rc=1)`. The constant's *value* is pinned;
its *purpose* is not.)

Driving the scenario the floor exists for — `hold_time = 0` (the wire coercer's
declared minimum, `_coerce_wire_number(..., 0, MAX_HOLD_TIME_CENTISECONDS)`) with
the inside sensor blocking close:

```
--- ORIGINAL ---
engine: /tmp/r8base/src/powerpetdoor/simulator/engine.py MIN_BLOCKED_RECHECK = 0.1
door status: DOOR_HOLDING
_wait_for_wake calls during 0.30 s of a blocked hold: 2
CPU burned in that window: 0.3 ms
--- MUTANT m22 (floor removed) ---
engine: /tmp/r8work/m22_no_min_recheck_floor/src/powerpetdoor/simulator/engine.py
door status: DOOR_HOLDING
_wait_for_wake calls during 0.30 s of a blocked hold: 33874
CPU burned in that window: 296.5 ms
```

296.5 ms of CPU in a 300 ms window is ~99% of a core, held for as long as the pet
stands in the doorway.

**Description**

`_hold_open()`'s blocked branch is a `while True` whose only yield is
`_wait_for_wake(max(hold_time, MIN_BLOCKED_RECHECK))`. With `hold_time = 0` and
no floor, `asyncio.timeout(0)` returns immediately and the loop spins. The comment
at `engine.py:630-632` states the floor's job exactly ("keeps a near-zero
hold_time from spinning"), so the contract is written down; it simply has no test.
Every test in the suite uses the 2.0 s default hold time, which is 20× the floor,
so the `max()` never selects the floor operand — a clean instance of CLAUDE.md
rule 9 (the second operand of the guard is never the deciding one).

**Recommendation**

Add one deterministic test in `tests/simulator/test_engine.py`: `hold_time = 0`,
inside sensor active, door in `DOOR_HOLDING`, then assert a *bounded* number of
`_wait_for_wake` calls (or wake-loop iterations) over a fixed number of event-loop
turns. Instrument the count rather than the wall clock so it stays deterministic
under `-n auto`. Per rule 8, also assert at `hold_time == MIN_BLOCKED_RECHECK`
and just above it, so both sides of the `max()` are exercised.

---

### M3 (Medium) — the wire numeric validator's inclusive upper bound is unpinned on all four fields that use it

**File:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/protocol.py:186`
(`_coerce_wire_number`), reached by `:764` (`index`), `:990` (`holdOpenTime`),
`:1043` and `:1054` (sensor trigger voltages).

**Reproduction**

Mutation — make the upper bound exclusive:

```python
    if not minimum <= value < maximum:
```

```
=== m42_wire_number_range_open: SURVIVOR (rc=0)
    2620 passed in ...
```

(The *lower* bound is pinned: `minimum <= value` → `minimum < value` gives
`=== m70_wire_number_lower_open: caught (rc=1)`. The neighbouring string limit is
pinned too: `=== m41_wire_string_len_ge: caught (rc=1)`. So this is a genuine gap,
not a property of the technique.)

What the mutant does to real wire values:

```
--- ORIGINAL ---
  holdOpenTime=90000 (exactly the documented maximum) -> ACCEPTED as 90000.0
  sensorTriggerVoltage=65535 (exactly the documented maximum) -> ACCEPTED as 65535.0
  index=255 (exactly the documented maximum) -> ACCEPTED as 255.0
--- MUTANT m42 (<= -> <) ---
  holdOpenTime=90000 -> REJECTED: holdOpenTime must be between 0 and 90000, got 90000
  sensorTriggerVoltage=65535 -> REJECTED: sensorTriggerVoltage must be between 0 and 65535, got 65535
  index=255 -> REJECTED: index must be between 0 and 255, got 255
```

The error message contradicts itself — "must be between 0 and 90000, got 90000" —
and the whole suite is green.

**Description**

This is the untrusted-input layer, the single most consequential validator in the
project, and round 7's 36-site boundary sweep pinned its *string* sibling
(`_coerce_wire_string`'s `len(value) > max_length`) and the CLI's float
`max_value`, but not `_coerce_wire_number`'s numeric ceiling. No test anywhere
sends `MAX_HOLD_TIME_CENTISECONDS`, `MAX_TRIGGER_VOLTAGE` or `MAX_SCHEDULE_INDEX`
*exactly*. Note this is a pin of what the simulator already accepts today — it
does not change or narrow the wire contract.

**Recommendation**

One parametrized test per limit in `tests/simulator/test_protocol.py`, asserting
`limit - 1` accepted, `limit` accepted, `limit + 1` rejected, for
`MAX_HOLD_TIME_CENTISECONDS`, `MAX_TRIGGER_VOLTAGE` and `MAX_SCHEDULE_INDEX` on
the real `SET_*` wire paths (not just the helper), matching CLAUDE.md rule 8's
"prefer one parametrized test per limit" guidance.

---

### M4 (Medium) — `_ACTION_PARAMS` can grow a parameter its action never reads, and a typo'd script goes back to reporting PASSED

**File:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:143-163`;
guard tests at
`/home/prez/src/pypowerpetdoor/tests/simulator/test_scripting.py:1729` and `:1744`.

**Reproduction**

Mutation — one action's declared set gains a parameter it does not read:

```python
    "open": frozenset({"hold", "duration"}),
```

```
=== m23_action_params_extra_key: SURVIVOR (rc=0)
    2620 passed in ...
```

The user-visible consequence, executed:

```yaml
name: Typo demo
steps:
  - action: open
    duration: 5
```

```
--- ORIGINAL ---
Script error at step 1: Unknown parameter(s) for open: duration. Use: hold
_ACTION_PARAMS["open"] = ['hold']
script result: False
--- MUTANT m23 ---
_ACTION_PARAMS["open"] = ['duration', 'hold']
script result: True   (the typo was silently accepted)
```

**Description**

`test_every_declared_parameter_is_actually_read` computes
`declared = set().union(*_ACTION_PARAMS.values())` and `read` from *every*
`params.get("...")` in the whole `_execute_step` body, then asserts
`declared - read == set()`. Both sides are flattened across all actions, so a
parameter that is legitimately read by *some other* action satisfies the check for
an action that never reads it. `duration` is read by `inside`/`outside`;
`seconds` by `wait`; `value` by `set` and `battery`; `name` by `set` and `toggle`;
`index` by `add_schedule` and `remove_schedule` — five of the nineteen actions'
parameter names are already shared, so the union check is blind across most of
the table.

The failure mode is precisely round-7 frontend L3, restored: the progress log
echoes `open(duration=5)` back as accepted, the parameter does nothing, and the
script exits 0. Over `ctl run <name> wait` that is a green CI result for a script
that tested nothing.

**Recommendation**

Make the check per-action. Split the `_execute_step` body on the
`elif action == "..."` chain (the same source extraction
`test_every_executed_action_declares_its_parameters` already performs) and assert,
for each action, `_ACTION_PARAMS[action] == {params read inside that action's
block}`. Both directions, so the table can neither lose a real parameter nor gain
a fictional one.

---

### M5 (Medium) — `disconnect()` flushes four throttles; two of them have no test, and dropping either loses the tail and leaks the count

**File:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/client.py:1457-1466`;
test at `/home/prez/src/pypowerpetdoor/tests/test_client.py:4021`
(`test_disconnect_flushes_the_per_frame_tails`).

**Reproduction**

Two mutations, each removing one throttle from the flush/reset loop:

```
=== m55_device_errors_not_flushed: SURVIVOR (rc=0)   # self._device_errors removed
    2620 passed in ...
=== m71_bad_messages_not_flushed:  SURVIVOR (rc=0)   # self._bad_messages removed
    2620 passed in ...
```

The two that *are* covered fail immediately, confirming the technique:

```
=== m72_non_ascii_not_flushed: caught (rc=1)         # self._non_ascii removed
```
(and `_bad_frames` is what `test_disconnect_flushes_the_per_frame_tails` drives).

The consequence of the surviving mutation, executed — three device-error frames,
then `disconnect()`:

```
--- ORIGINAL ---
device-error count before disconnect: 3 | records so far: 4
tail summary emitted by disconnect: ['Device reported 3 error response(s) (96 bytes) on this connection']
device-error count after disconnect (0 == reset for the next connection): 0
--- MUTANT m55 ---
device-error count before disconnect: 3 | records so far: 4
tail summary emitted by disconnect: []
device-error count after disconnect (0 == reset for the next connection): 3
```

**Description**

`_device_errors` is the throttle round 7 added (security L3) and `_bad_messages`
is round 6's; both were added to the flush tuple without a matching disconnect
test, so the tuple has two covered members and two decorative ones. Losing the
flush means the suppressed tail of a burst is never reported — the counts are
"batched, never lost" per `EventThrottle`'s own docstring, and that promise is
what breaks. Losing the `reset()` carries the count across a reconnect, so the
doubling schedule resumes far along and a *fresh* burst on the new connection is
under-reported — the round-6 backend L4 defect the quiet period was introduced to
fix, re-opened on the newest throttle.

This is the recurring "the twin that was not changed" shape the fuzz suite's own
docstring names (`tests/fuzz/test_untrusted_input_fuzz.py:296-297`).

**Recommendation**

Parametrize `test_disconnect_flushes_the_per_frame_tails` over all four throttles
— each with the input that drives it (`b"\xff"` for `_non_ascii`, `b"{x}"` for
`_bad_frames`, `b"{}"` for `_bad_messages`, an `success:"false"` envelope for
`_device_errors`) — asserting both the tail record and `count == 0` afterwards.
That makes the loop's membership the thing under test rather than one element of it.

---

### L1 (Low) — the `wait_for` condition doc-extractor only understands one of the two spellings its own file uses

**File:** `/home/prez/src/pypowerpetdoor/tests/test_docs_accuracy.py:554-570`
(`test_the_wait_for_condition_table_matches_the_implementation`), vs its sibling
at `:573-583` which handles both spellings.

**Reproduction**

Mutation — add a genuinely working condition to `_check_condition` using the
`in (...)` spelling the sibling extractor already anticipates:

```python
        elif condition in ("door_ajar", "door_stuck"):
            return False
```

```
=== d1_new_condition_in_tuple_form: SURVIVOR (rc=0)
    2620 passed in ...
```

The condition really is live, and the extractor really is blind to it:

```
--- ORIGINAL ---
_check_condition('door_ajar') raised: ScriptError Unknown condition: door_ajar
extractor sees door_ajar? False
--- MUTANT d1 ---
_check_condition('door_ajar') -> False
extractor sees door_ajar? False
```

Falsifiability of the same test in the other direction is intact — a doc-side
change *is* caught:

```
=== x2_docs_remove_door_closing_row: caught (rc=1)
    FAILED tests/test_docs_accuracy.py::test_the_wait_for_condition_table_matches_the_implementation
=== d4_docs_typo_a_condition: caught (rc=1)
=== d3_new_action_in_execute_step: caught (rc=1)
```

**Description**

`test_the_wait_for_condition_table_matches_the_implementation` extracts with
`re.findall(r'condition == "([a-z_]+)"', body)`; the sibling
`test_the_assert_condition_table_matches_the_implementation` additionally uses
`re.findall(r'condition in \("([a-z_]+)"', body)`. The asymmetry is itself the
evidence that both spellings are expected in this codebase — and `_check_condition`
already uses `in (...)` for its *values* (`state.door_status in (...)`), so the
next condition added there is as likely to be written that way as not.

**Recommendation**

Extract with `ast` rather than a regex: walk the function's `Compare` nodes whose
left operand is the `condition` name and collect every string constant on the
right (covering `==`, `in (...)` and `in {...}` at once). Failing that, use the
sibling's two-pattern regex in both tests so they cannot diverge.

---

### L2 (Low) — `test_every_script_action_is_documented` is one-directional, so the docs can invent an action

**File:** `/home/prez/src/pypowerpetdoor/tests/test_docs_accuracy.py:602-611`.

**Reproduction**

Mutation — document an action that does not exist, in `docs/simulator.md`:

```markdown
**totally_fake**

Not a real action at all.
```

```
=== d2_docs_invent_an_action: SURVIVOR (rc=0)
    2620 passed in ...
```

The two sets are exactly equal on the current tree, so tightening the assertion
is free and non-brittle:

```
documented:  ['add_schedule','assert','battery','close','inside','log','obstruction','open','outside','pet_off','pet_on','pet_presence','remove_schedule','set','toggle','trigger','trigger_sensor','wait','wait_for']
implemented: ['add_schedule','assert','battery','close','inside','log','obstruction','open','outside','pet_off','pet_on','pet_presence','remove_schedule','set','toggle','trigger','trigger_sensor','wait','wait_for']
implemented - documented: []
documented - implemented: []
```

**Description**

The assertion is `set(scripting._ACTION_PARAMS) - documented == set()`. A doc that
introduces an action the DSL does not implement is precisely the class of defect
this file exists for (round-6 frontend L4/L7, round-7 frontend L4) — a reader
copies it, the script fails with "Unknown action", and nothing in CI noticed the
doc was wrong.

**Recommendation**

Change to `assert documented == set(scripting._ACTION_PARAMS)`. Verified equal
above, so no doc or code change is required to adopt it.

---

### L3 (Low) — the battery-flag guard test seeds a cached value that is not decisive

**File:** `/home/prez/src/pypowerpetdoor/tests/test_door.py:2131-2144`
(`test_a_non_bool_battery_flag_keeps_the_cached_value`), guarding
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/door.py:178-183` (`_keep_bool`).

**Reproduction**

Mutation — `_keep_bool` coerces ints instead of keeping the cache:

```python
    if isinstance(value, (bool, int)):
        return bool(value)
```

```
=== m13_keep_bool_coerces: SURVIVOR (rc=0)
    2620 passed in 48.12s
```

The parameter list already includes the deciding input (`1`), but the fixture
seeds `present=True, ac_present=True` — and `bool(1)` is `True`, so "kept the
cache" and "coerced the int" produce the same answer. Making the cached value
decisive catches it, and the decisive version still passes on the unmutated tree:

```
# door._battery = BatteryInfo(percent=42, present=False, ac_present=False)
# assert door.battery_present is False
--- against MUTANT m13 ---
E       assert True is False
FAILED tests/test_door.py::TestFacadeCacheIsTypeGuarded::test_a_non_bool_battery_flag_keeps_the_cached_value[int]
1 failed, 3 passed in 0.39s
--- the same decisive test against the UNMUTATED tree ---
4 passed in 0.37s
```

**Description**

The class is CLAUDE.md rule 8/9: the second operand has to be the deciding one.
Impact is limited — the shipped client pre-coerces both flags with `make_bool`
(`client.py:983-984`), so an `int` only reaches `_keep_bool` through a
third-party subclass calling the listener directly, which is exactly the
"defence in depth" the round-7 docstring claims. The *claim* is what is
untested, not a shipped path; hence Low. The sibling parameters
(`_keep_int`, `_keep_str`) are all decisive — this is the one exception.

**Recommendation**

Seed `present=False, ac_present=False` and assert `is False`, or parametrize
`(value, cached)` so `cached != bool(value)` for every truthy input.

---

### L4 (Low) — `_on_hold_time_update`'s "keep the cached value" is lossy under a plausible edit, and the test cannot see it

**File:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/door.py:1415`; test at
`/home/prez/src/pypowerpetdoor/tests/test_door.py:2192-2203`.

**Reproduction**

Mutation — `round()` becomes `int()` in the cached fallback:

```python
    centiseconds = _keep_int(value, int(self._hold_time * 100), "hold_time")
```

```
=== m15_hold_time_cached_wrong: SURVIVOR (rc=0)
    2620 passed in ...
```

The test seeds `door._hold_time = 4.0`, and `4.0 * 100 == 400.0` exactly, so both
spellings round-trip identically. They do not for most values:

```
$ .venv/bin/python -c "
bad=[c for c in range(0,90001) if int(c/100.0*100)!=c]
print('centisecond values where int() != round():', len(bad), 'of 90001')
print('first 15:', bad[:15])
print('example: hold_time=0.29 ->', 0.29*100, 'int', int(0.29*100), 'round', round(0.29*100))"
centisecond values where int() != round(): 4586 of 90001
first 15: [29, 57, 58, 113, 114, 115, 116, 201, 203, 205, 207, 226, 228, 230, 232]
example: hold_time=0.29 -> 28.999999999999996 int 28 round 29
```

`_hold_time` is populated as `centiseconds / 100.0` from the wire, so 5.1% of the
values the device can legally send land on a float whose `int(x * 100)` is one
centisecond low. Under the mutant, a *bad* frame arriving while `hold_time`
is 0.29 s silently rewrites the cache to 0.28 s — the listener's whole contract is
that a rejected value leaves the cache untouched.

**Description**

Unlike its siblings (`_keep_str`'s timezone test seeds a string that no coercion
could reproduce, `_keep_int`'s counters seed 11/3), this test's seed is the one
float in range for which the round-trip is exact. Correct code is in place; only
the pin is missing.

**Recommendation**

Seed `door._hold_time = 0.29` (or parametrize over a few values from the 4,586)
and assert `door.hold_time == 0.29` after the rejected update. One extra
parameter on the existing test.

---

### L5 (Low) — dead loop in the docs-accuracy JSON extractor

**File:** `/home/prez/src/pypowerpetdoor/tests/test_docs_accuracy.py:107-111`.

**Reproduction**

```python
    for body in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
        for line in body.strip().splitlines():      # <- these five lines
            line = line.strip()                     #    have no effect
            if not line.startswith("{"):
                # Part of a pretty-printed multi-line object; handled below.
                break
        try:
            blocks.append(json.loads(body))
```

Mutation — delete the inner loop entirely:

```
=== x1_json_blocks_dead_loop: SURVIVOR (rc=0)
    2620 passed in 49.07s
```

**Description**

The loop binds a local, breaks, and discards it; no branch below reads `line`, so
it cannot influence a single extracted block. The comment ("handled below")
suggests it was meant to set a flag that was never written. It is inert code
inside the helper that feeds the keepalive pins — the highest-stakes doc
assertions in the repo — where an inert construct reads like a check that is
happening and is not.

**Recommendation**

Delete the loop (the `try: json.loads(body) / except: per-line` fallback already
implements the documented behaviour), or, if the multi-object case is meant to be
distinguished, make it set a flag the code below actually reads.

---

## Round 7 Fix Verification

All eight round-7 fix claims were re-verified by execution against `da31ae2`.

| Round-7 item | Verified how | Result |
|---|---|---|
| `timeout_method = "thread"` fires and names the hanging line | Injected an infinite loop into `_BraceScanner.scan` (`i += 1` → `i += 0`) and ran `tests/fuzz/test_framing_fuzz.py -n0 --timeout=20` under both methods | **Confirmed.** `thread`: failed at 20.6 s with a full stack naming `framing.py:282 while i < n` and `+++ Timeout +++`. `signal`: identical hang produced **zero output in the full 150 s** wall-clock cap. The claim is not just repeated in a comment, it reproduces. |
| The setting is pinned executably | `tests/test_docs_accuracy.py:480` reads `pyproject.toml` via `tomllib` and asserts `timeout == 60` and `timeout_method == "thread"` | Present and non-tautological. |
| `FrameScanner.buffer` truncation | `framing.py:369-376` coalesces in place; `MAX_RETAINED_PIECES = 64` → `65` is caught (`m58`) | Fixed and pinned. |
| The unfalsifiable `flush()` test | `tests/test_framing.py:737-786` now advances the injected clock before flushing and pins both `_last_report` and `_reported`; `m37` (drop `flush`'s `_last_report` write) is caught | Fixed. |
| `ctl.py:365` pragma | Pragma gone; the honest half of the rationale kept as a comment at `ctl.py:365-378`, with the `loop.remove_reader` seam test. `coverage.json` shows `ctl.py` excluding only `[47, 48, 657, 658, 766, 767]` — the `TYPE_CHECKING` block, the remaining documented pragma, and the `__main__` guard | Fixed. The comment even warns about the prose re-exclusion the fix agent self-caught — see H1 for why that warning needs to become a test. |
| Boundary tests at the 36 proven sites | Spot-checked by mutation: schedule window edges (`test_state.py:454-499`), sanitize truncation (`m39` `>` → `>=` caught, `m38` truncate-after-escape caught), wire string length (`m41` caught), `_ControlLogHandler.MAX_CLIENT_BACKLOG` (`m40` caught), simulator write ceiling (`m18` caught) | Confirmed. One adjacent site was missed — see M3. |
| CLAUDE.md rules 8-10 | Present at `.claude/CLAUDE.md`, "Test Quality Rules (Critical)" 8, 9, 10 | Present. Findings M2, M3, L3, L4 are all instances of rules 8/9 at sites the sweep did not reach. |
| DoS bound constants pinned (incl. `MAX_WRITE_BACKLOG`, `MIN_BLOCKED_RECHECK`) | `TestShippedResourceBoundsHaveTheirValuesPinned` (`tests/test_framing.py:1244+`); the control mutation `MAX_INFLIGHT_FRAMES 64 → 63` is caught by name, and `m21` (`MIN_BLOCKED_RECHECK 0.1 → 1.0`), `m58`, `m59` (`THROTTLE_QUIET_PERIOD`) are all caught | Confirmed. `MIN_BLOCKED_RECHECK`'s *value* is pinned; its *behaviour* is not (M2). |
| "2620 tests, 100.00% both invocations" | Re-run under the guarded harness | Confirmed: `2620 passed` (full) and `2576 passed`, `TOTAL 6662 0 2368 0 100.00%`, "Required test coverage of 100.0% reached" under `--ignore=tests/fuzz --cov`. |
| `TESTING_GAPS.md` accuracy | Regenerated with `python scripts/generate_gaps_report.py --stdout` and diffed against the committed file | **Byte-identical modulo the timestamp line.** The three disclosed pragmas (`cli.py:100`, `cli.py:689`, `ctl.py:657`) all exist at those line numbers with those texts. The file is accurate for what it measures; see H1 for what it does not measure. |

## Areas Reviewed With No Findings

**Mutants that survived but are argued equivalent (not reported):**

- `m08` — `_on_dispatched_done` calling `_schedule_pump()` instead of `_pump()`.
  Every frame is still dispatched, in order, under the same `max_inflight` and
  the same pause/resume thresholds; only the dispatch of the next frame moves one
  loop turn later. No contract in the class docstring distinguishes them.
- `m33` — `render_script_listing`'s `builtin = list(...)` losing its defensive
  copy. The function only iterates `builtin`, and the sole caller
  (`commands/scripts.py:291`) passes a freshly built list, so no aliasing is
  observable.
- `m43` — `_coerce_wire_flag` widening its `isinstance` gate to include `float`.
  `make_bool(1.0)` falls through to `else: return v` and returns the float
  unchanged, which the following `isinstance(flag, bool)` check rejects anyway, so
  the outcome is identical for every float.

**Mutated and correctly caught (61 mutations, 47 caught):**

- `FrameDispatcher._pump()` yield/re-arm — budget doubling, removing the budget
  entirely, re-arm with either operand dropped, `_update_flow` skipped after a
  re-arm, the latch not clearing, and sourcing the budget from `pause_at` were
  all caught (`m01`-`m04`, `m06`, `m07`, `m60`), several by
  `test_a_full_inflight_bound_does_not_arm_a_continuation` and
  `test_an_unparseable_burst_now_reaches_the_pause_threshold` specifically. Only
  the dedupe guard (M1) is unobserved.
- `_keep_int` / `_keep_str` — bool leaking through, the finiteness check
  inverted, floats refused, and a wrong fallback value are all caught, with
  decisive parametrized inputs (`m10`-`m12`, `m14`).
- The latched write ceiling — dropping the latch check, never setting the latch,
  moving the boundary to `>=`, and `abort()` → `close()` are each caught
  (`m16`-`m19`), including `test_the_ceiling_is_a_boundary_not_a_hair_trigger`
  asserting at exactly `MAX_WRITE_BACKLOG`.
- `SENSOR_NAMES` — both adding a name and removing one are caught (`m20`, `m57`).
- `_ACTION_PARAMS` enforcement — `_ACTION_PARAMS.get(action) or None`, which would
  disable validation for the five actions with an *empty* parameter set, is
  caught (`m24`); so is a table entry that changes an error message (`d6`). Only
  the per-action union blindness (M4) survives.
- `render_script_listing` — the shadow marker reconstructing `<dir>/<name>`
  instead of the real path, the `(none)` line, and suppressing the header for an
  empty directory are all caught (`m30`-`m32`).
- `EventThrottle` — quiet-period comparison `>=` → `>`, the uncapped doubling
  step, an off-by-one in `_next`, `flush()` not restarting the quiet period, and a
  10× `THROTTLE_QUIET_PERIOD` are all caught (`m34`-`m37`, `m59`).
- `sanitize_text` — truncate-before-escape ordering and the `>` → `>=` boundary
  are both caught (`m38`, `m39`).
- Round-7 operator-input fixes — `parse_arg`'s `math.isfinite` guard, `holdtime`'s
  validate-then-write ordering, `_format_sensor_scope`'s branches and the
  policy-aware out-of-directory remedy string are all caught (`m50`-`m53`, `m61`).
- The `_device_errors` throttle's *reporting* half — removing the throttle
  condition and removing the length cap are both caught (`m54`, `m56`). Only its
  disconnect flush (M5) is uncovered.
- Doc source-extraction falsifiability — a new action in `_execute_step`, a
  typo'd condition row, a removed condition row, and a new `_assert_condition`
  form are all caught (`d3`, `d4`, `d5`, `x2`). Only the `in (...)` spelling in
  `_check_condition` (L1) and the one-directional action check (L2) survive.

**Reviewed by inspection, no finding:**

- **Skips.** Ten `skipif`s, all guarding optional extras (`PyYAML`,
  `prompt_toolkit`), both of which are `dev` dependencies — 0 skipped in every
  run recorded here (`2620 passed`, no "skipped"). No `xfail`, no `pytest.skip`
  in a test body.
- **Flakiness.** Only three real-time sleeps exist in the whole suite
  (`tests/simulator/test_ctl.py:175`, `:226`, `tests/test_door.py:1960`). Each is
  either a 10× margin with the rationale written down, a poll loop bounded by
  `asyncio.timeout`, or a case whose assertion cannot flip on a slow runner
  (the handler never replies). Everything else waits on events, futures or
  `sleep(0)` turn-yields. `filterwarnings = ["error"]` is on, and the
  session-scoped loop fixture explains and closes the one loop that would
  otherwise emit a ResourceWarning.
- **Fuzz quality.** The strategies do draw the values they exist for:
  `_pathological` is mixed into every scalar coercer, `_well_shaped_*` feed the
  success paths so post-conditions actually run, and `_CONTROL_CODEPOINTS` is an
  independent definition rather than the production regex (which is what made the
  round-5 sanitize property unfalsifiable). The `assert x in (...)` occurrences
  in `tests/fuzz/test_untrusted_input_fuzz.py:348-349` and `tests/conftest.py:369`
  are wire-shape assertions over a drawn input, not contradictory outcomes.
  `test_both_emitters_agree_on_every_field_except_enabled` correctly excludes
  `enabled` and pins the two directions separately — that separation must not be
  "unified" by a later round.
- **Pragmas.** Three remain in production code, all disclosed in
  `TESTING_GAPS.md`. `cli.py:100` and `cli.py:689` are `no branch` with stated
  invariants. `ctl.py:657` is the last `no cover`, and unlike round-7 M4's case
  there is **no injectable seam**: `socket_reader` is a closure defined inside
  `interactive_mode_async` (`ctl.py:444`), not a module-level or stdlib symbol a
  test can replace, so "replace the API the clause depends on" does not apply
  here without a production refactor. Its rationale is specific and honest.
  Not reported.
- **`omit` list.** `*/__init__.py` and `*/__main__.py` contain nothing but
  imports plus `__version__`/`__all__` (checked with `ast`: `ImportFrom`,
  `Assign`, `Expr` only), and `tests/test_exports.py` covers `__all__`
  separately. The omit is legitimate.
- **CI gate.** The `--cov-fail-under=0` in the unit matrix and `--fail-under=0`
  in the combine step are deliberate (per-version data is partial); the real gate
  is `coverage report --fail-under=100` at `.github/workflows/test.yml:203-204`,
  after `coverage combine` across all four interpreters. It is enforced — which
  is exactly why H1 matters.
- **`test_gaps_report.py`.** The tokenize-based pragma scanner is well tested
  including the "phrase inside a string literal is not a pragma" case
  (`:242`), the `scripts/` root (`:268`), reasons containing parentheses
  (`:218`), and untokenizable/undecodable files. Its blind spot is not the
  scanner but the coverage config it does not check (H1).
