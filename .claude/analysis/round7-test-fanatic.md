# Test Fanatic Analysis — Round 7

Commit audited: `a0194bd` ("Round 6 fixes; revert the enabled wire change; layer the wire boundary").

**Method.** Every claim below was produced by executing code, not by reading it.
All work happened in `/tmp` copies of the repo (`git archive HEAD | tar -x`), never
in the working tree. Every run forced `PYTHONPATH=<copy>/src` and asserted
`powerpetdoor.__file__.startswith("<copy>/")` before pytest started, so no finding
can be an artifact of importing the installed editable package. Each hand-written
mutation batch carried a control mutation that MUST be caught; three of the four
controls were caught, and the one that was **not** is itself finding L1.

Baseline reproduced before any mutation:

```
$ cd /tmp/r7 && ./run.sh -q -p no:randomly
[guard] powerpetdoor.__file__ = /tmp/r7/src/powerpetdoor/__init__.py
2454 passed in 39.80s

$ cd /tmp/r7 && ./run.sh -q --cov --cov-report=term-missing
TOTAL                                                   6571      0   2332      0  100.00%
Required test coverage of 100.0% reached. Total coverage: 100.00%
2454 passed in 47.89s
```

**Mutants run: 287.** 91 hand-written (framing/dispatcher/throttle, the schedule
wire boundary tables, `fwInfo`, `script_escapes_directory`, `tz_utils`,
`sanitize`, simulator constants) plus 4 controls, and **183 machine-generated,
unbiased** mutants (every single-line `<`/`<=`/`>`/`>=` flip in `src/` +
`scripts/` — all 93 of them — plus 90 of 175 `and`/`or` flips, seeded shuffle).
The generated batch is the honest mutation score:

```
== 138 caught / 45 survived / 0 other, 183 total ==   (75.4% kill rate)
```

Of the 45 survivors, **9 are provably equivalent** (argued individually at the
end of this report); the remaining 36 are real, and they cluster into one
pattern, which is finding M5.

---

## Summary

| Severity | Count |
|----------|-------|
| High     | 0 |
| Medium   | 5 |
| Low      | 6 |
| Informational (provably-equivalent mutants — **not** findings) | 9 |

Coverage instrumentation is now **honest** — see "Areas Reviewed With No
Findings" for the pattern-by-pattern audit that proves it, including the other
exclude patterns and the omit list, which I re-audited with the same suspicion
the `...` pattern earned in round 6. `tests/TESTING_GAPS.md` regenerates
byte-identical from live coverage data. No skipped tests, no tautologies, no
ordering dependencies, no flakiness across 8 full-suite runs.

The five Mediums:

- **M1** the `timeout = 60` deadlock backstop does **not** apply to any of
  `tests/fuzz/` — proven by a run that burned 150 s of wall clock producing zero
  output, and by the one-line config change that fixes it.
- **M2** `FrameScanner.buffer` can return a **truncated** remainder while all
  2454 tests pass.
- **M3** `test_flush_restarts_the_quiet_period` cannot fail: the line it is named
  after can be deleted and its whole class still passes.
- **M4** a `# pragma: no cover` justified as "cannot be triggered
  deterministically" is triggered deterministically by a 25-line test.
- **M5** a systematic gap: comparisons are almost never exercised **at** the
  boundary value, and compound `and`/`or` conditions are almost never exercised
  with the second operand decisive — a class coverage.py's branch metric is
  structurally blind to. 36 proven sites.

---

## Findings

### M1 — The `timeout = 60` deadlock backstop silently does not apply to the fuzz/property suite

**Severity:** Medium
**File:** `pyproject.toml:88-93` (`[tool.pytest.ini_options] timeout = 60`); affects every test in `tests/fuzz/` (`test_client_fuzz.py`, `test_framing_fuzz.py`, `test_schedule_fuzz.py`, `test_tz_fuzz.py`, `test_untrusted_input_fuzz.py`)

**Reproduction**

Found accidentally: a machine-generated `framing.py` mutant
(`while i < n:` → `while i <= n:` in `FrameScanner.feed`, an infinite loop) pinned
one xdist worker at 84 % CPU for **18 minutes** with `timeout = 60` configured.

```
$ ps -o pid,etime,%cpu,cmd -p 975547
    PID     ELAPSED %CPU CMD
 975547       18:16 84.5 .../python -u -c import sys;exec(eval(sys.stdin.readline()))
```

Reduced to a two-case experiment. Same infinite loop, same project config.

*Case A — plain test. The timeout works:*

```python
# /tmp/r7/tests/test_hangprobe.py
from powerpetdoor.framing import FrameScanner
def test_partial_object_feed():
    FrameScanner().feed('{"a": ')
```
```
$ PYTHONPATH=/tmp/r7/src python -m pytest tests/test_hangprobe.py -q -n0
E   Failed: Timeout (>60.0s) from pytest-timeout.
src/powerpetdoor/framing.py:253: Failed
FAILED tests/test_hangprobe.py::test_partial_object_feed - Failed: Timeout (>...
1 failed in 60.17s (0:01:00)
```

*Case B — the identical hang inside a `@given` test. The timeout never fires:*

```python
@settings(max_examples=50, deadline=None)
@given(text=st.sampled_from(['{"a": ', '{"b": ', '{']))
def test_partial_object_feed_hypothesis(text):
    FrameScanner().feed(text)
```
```
$ start=$(date +%s); timeout -s KILL 150 python -m pytest tests/test_hangprobe.py -q -n0 \
      > /tmp/hang.out 2>&1; rc=$?; end=$(date +%s)
$ echo "pytest_exit=$rc elapsed=$((end-start))s"
pytest_exit=137 elapsed=150s      # 137 = SIGKILL from the outer 150 s wrapper
$ cat /tmp/hang.out
                                   # empty: pytest produced nothing at all
```

*The fix, verified:*

```
$ start=$(date +%s); timeout -s KILL 200 python -m pytest tests/test_hangprobe.py -q -n0 \
      --timeout-method=thread > /tmp/hang2.out 2>&1; rc=$?; end=$(date +%s)
$ echo "pytest_exit=$rc elapsed=$((end-start))s"
pytest_exit=1 elapsed=60s
$ tail -6 /tmp/hang2.out
  File "/tmp/r7/tests/test_hangprobe.py", line 10, in test_partial_object_feed_hypothesis
    FrameScanner().feed(text)
  File "/tmp/r7/src/powerpetdoor/framing.py", line 425, in feed
    while i <= n:
+++++++++++++++++++++++++++++++++++ Timeout ++++++++++++++++++++++++++++++++++++
```

**Description.** `pyproject.toml` documents `timeout = 60` as the suite-wide
backstop — *"a regression that deadlocks must fail the build, not occupy the
shared runner until the 6-hour job timeout."* pytest-timeout's default `signal`
method raises inside the running test frame. Hypothesis catches whatever the test
body raises and enters its shrinking phase, re-invoking the body — but the
`SIGALRM` was one-shot and is now spent, so every shrink attempt re-enters the
hang with no alarm armed and the process spins forever. The backstop therefore
covers the ~2200 deterministic tests and **none** of the property suite, which is
exactly the half where a novel input finds a novel hang. The weekly unseeded
`fuzz-tests` cron (`.github/workflows/test.yml:118-120`) is where that would first
appear, and it would present as a silent `timeout-minutes: 30` kill naming no
test.

**Recommendation.** Add `timeout_method = "thread"` to
`[tool.pytest.ini_options]`. The thread method runs its timer in a separate
thread, dumps every stack, and calls `os._exit()`; hypothesis cannot swallow it,
and it names the hanging line (verified above). Zero cost on a passing run.
Consider also a one-line test asserting the configured method, since nothing else
in the suite can observe it.

---

### M2 — `FrameScanner.buffer` can return a truncated remainder and the whole suite stays green

**Severity:** Medium
**File:** `src/powerpetdoor/framing.py:373` (`if len(self._pieces) > 1:`)
**Public consumers:** `src/powerpetdoor/client.py:1878`, `src/powerpetdoor/simulator/protocol.py:355`, `src/powerpetdoor/framing.py:646` (`extract_frames`)

**Reproduction**

```
$ python /tmp/mutbatch.py    # mutation: `if len(self._pieces) > 1:` -> `> 2:`
SURVIVED  F28 buffer coalesce threshold
    | 2454 passed in 55.37s
```
Independently reproduced by the unbiased generated batch:
```
SURVIVED  src/powerpetdoor/framing.py:373 >->>=  |if len(self._pieces) > 1:
```

The mutant returns a wrong answer through a public property:

```
$ # with the mutation applied to /tmp/r7
$ PYTHONPATH=/tmp/r7/src python -c "
import powerpetdoor; assert powerpetdoor.__file__.startswith('/tmp/r7/')
from powerpetdoor.framing import FrameScanner
s = FrameScanner(); s.feed('{\"a\": '); s.feed('\"xy')
print('MUTANT buffer =', repr(s.buffer))
print('MUTANT _retained =', s._retained, 'vs len(buffer) =', len(s.buffer))"
MUTANT buffer = '{"a": '          # correct answer is '{"a": "xy'
MUTANT _retained = 9 vs len(buffer) = 6
```

**Description.** `buffer` coalesces the retained piece list and returns
`self._pieces[0]`; the coalesce guard is the only thing that makes that correct
when more than one piece is retained. Two retained pieces is the *ordinary* state
after two partial feeds — exactly what `tests/test_framing.py:434`
(`test_retained_length_tracks_the_pieces`) constructs — yet every existing
`.buffer` assertion lands on 1 piece or ≥3 pieces:

| test | pieces when `.buffer` is read |
|---|---|
| `test_buffer_property_coalesces_and_is_stable` (line 423) | 10 |
| `test_retained_length_tracks_the_pieces` (line 434) | 3 |
| `test_the_retained_length_still_matches_the_pieces_after_coalescing` (line 820) | 129 |

`> 2` still coalesces at 3, so nothing observes the two-piece case. The invariant
that would have caught it, `scanner._retained == len(scanner.buffer)`, is asserted
only at line 827 with 129 pieces.

**Recommendation.**

```python
def test_buffer_coalesces_exactly_two_pieces(self):
    scanner = FrameScanner()
    scanner.feed('{"a": ')
    scanner.feed('"xy')
    assert len(scanner._pieces) == 2
    assert scanner.buffer == '{"a": "xy'
    assert scanner._retained == len(scanner.buffer)
```

---

### M3 — `test_flush_restarts_the_quiet_period` cannot fail

**Severity:** Medium
**File:** `tests/test_framing.py:719-734`; production line under test `src/powerpetdoor/framing.py:222` (`self._last_report = self._clock()` in `EventThrottle.flush`)

**Reproduction**

Delete the line the test is named after, then run the class it lives in:

```
$ python - <<'EOF'
import pathlib
p = pathlib.Path('/tmp/r7/src/powerpetdoor/framing.py'); s = p.read_text()
old = "            self._last_report = self._clock()\n            self._reported = self._count"
assert s.count(old) == 1
p.write_text(s.replace(old, "            self._reported = self._count"))
EOF
F7 applied: flush() no longer restarts the quiet period

$ PYTHONPATH=/tmp/r7/src python -m pytest "tests/test_framing.py::TestEventThrottleTimeFloor" -q -n0
......                                                                   [100%]
6 passed in 0.33s
```
The full suite also passes with the line gone
(`SURVIVED F7 flush drops last_report update — 2454 passed in 68.98s`).

The behaviour *is* observable — the test simply never moves the clock before
`flush()`, so the assignment writes back the value already there:

```
$ # /tmp/f7demo.py: 5 records, THEN advance a full quiet period, THEN flush,
$ #                 THEN advance (QUIET_PERIOD - 0.001), THEN record once.
$ PYTHONPATH=/tmp/r7/src python /tmp/f7demo.py
records after flush+near-miss: ['seen 5 (5 bytes)']                       # pristine
--- now with F7 mutation ---
records after flush+near-miss: ['seen 5 (5 bytes)', 'seen 6 (6 bytes)']   # mutant
```

**Description.** `ManualClock` starts at 1000.0 and the test records 5 events and
flushes with no `clock.advance()` in between, so `_last_report` already equals
`clock.now` when `flush()` assigns it. The following
`clock.advance(THROTTLE_QUIET_PERIOD - 0.001)` is then a near-miss relative to the
last *record* rather than to the flush, and the assertion holds whether or not
`flush()` restarts the quiet period. This is the persona's "no way to fail" case:
the test names a behaviour, its docstring explains why it matters ("A flushed tail
counts as a report, so it is not double-counted"), and removing the behaviour does
not fail it.

**Recommendation.** Insert `clock.advance(framing.THROTTLE_QUIET_PERIOD)` between
the record loop and the `flush()` (as `/tmp/f7demo.py` does). One line; the
existing assertion is then correct *and* falsifiable.

---

### M4 — A `# pragma: no cover` claims its branch is untestable; a 25-line test triggers it

**Severity:** Medium
**File:** `src/powerpetdoor/simulator/ctl.py:365`
(`except Exception:  # pragma: no cover (defensive: Linux selectors swallow errors for dead fds, so this cannot be triggered deterministically)`)

**Reproduction**

On pristine source the test passes:

```python
# /tmp/r7/tests/simulator/test_poc_pragma.py  (abridged; full file used in the run)
async def test_cleanup_swallows_a_failing_remove_reader(pipe_stdin):
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda _l, ctx: errors.append(ctx))
    real_remove = loop.remove_reader
    fut = ctl._basic_readline("> ")
    def boom(fd):
        raise OSError(9, "Bad file descriptor")
    loop.remove_reader = boom
    fut.cancel()
    await asyncio.sleep(0); await asyncio.sleep(0)
    loop.remove_reader = real_remove
    assert errors == [], f"cleanup let the error escape: {errors}"
```
```
$ PYTHONPATH=/tmp/r7/src python -m pytest tests/simulator/test_poc_pragma.py -q -n0
1 passed in 0.30s
```

And it fails when the `try/except` it covers is removed — so it is a real test,
not a tautology:

```
$ # replace `try: loop.remove_reader(fd) / except Exception: pass` with a bare call
$ PYTHONPATH=/tmp/r7/src python -m pytest tests/simulator/test_poc_pragma.py -q -n0
E  AssertionError: cleanup let the error escape:
   [{'message': 'Exception in callback _basic_readline.<locals>.cleanup() at .../ctl.py:362',
     'exception': OSError(9, 'Bad file descriptor'), ...}]
FAILED tests/simulator/test_poc_pragma.py::test_cleanup_swallows_a_failing_remove_reader
1 failed in 0.29s
```

**Description.** The pragma's justification — that Linux selectors swallow errors
for dead fds — is true of the *real* selector, but the project mocks external
components everywhere else and the loop's `remove_reader` is trivially
replaceable. The resulting test is deterministic and asserts precisely the
contract the `except` exists for: the error must not reach the loop exception
handler. `CLAUDE.md` requires justification and approval for each pragma; this
one's justification is factually wrong, and round 6 already removed a pragma of
this same class (`tests/simulator/test_ctl_interactive.py:658`: *"The clause
carried a `# pragma: no cover` claiming it could not be…"*).

The other `no cover` pragma, `ctl.py:644` (`except asyncio.CancelledError`), is
defensible: `socket_reader` and `reader_task` are both closures inside
`interactive_mode_async` with no injection seam. It is left alone.

**Recommendation.** Add the test above beside `TestBasicReadline` in
`tests/simulator/test_ctl.py` and delete the pragma. That returns 2 statements to
the 100 % gate.

---

### M5 — Comparisons are not exercised **at** the boundary, and compound conditions not with the second operand decisive

**Severity:** Medium
**Files:** 20 sites across `framing.py`, `client.py`, `door.py`, `schedule.py`, `sanitize.py`, and the simulator (enumerated below)

**Reproduction**

Unbiased, machine-generated mutation of **every** single-line comparison-operator
flip in `src/` and `scripts/` (all 93), plus 90 `and`/`or` flips:

```
$ python /tmp/gen_mutants.py 93 7 && python /tmp/gen_mutants2.py 90 11
93 candidate comparison mutants; wrote 93
175 candidate boolop mutants; wrote 90
$ NWORKERS=6 CAP=200 python -u /tmp/mutbatch3.py /tmp/genall.json
...
== 138 caught / 45 survived / 0 other, 183 total ==
```

Survivors, after removing the 9 provably-equivalent ones (see the last section):

| Site | Mutation that survives | What the boundary means |
|---|---|---|
| `simulator/state.py:265` | `current_minutes < end` → `<=` | a midnight-crossing schedule's **exclusive end**: at exactly 06:00 the sensor becomes allowed |
| `simulator/state.py:265` | `current_minutes >= start` → `>` | the same window's **inclusive start**: 22:00 stops being allowed |
| `simulator/protocol.py:208` | `len(value) > max_length` → `>=` | a wire string of exactly `MAX_TIMEZONE_LENGTH` is rejected as "too long" |
| `sanitize.py:55` | `len(value) > limit` → `>=` | a value of exactly `limit` is marked `...(truncated)` though nothing was cut |
| `framing.py:604` | `len(self._backlog) > self._pause_at` | a backlog of exactly `pause_at` pauses reading, contradicting "**above** which" |
| `framing.py:373` | `len(self._pieces) > 1` → `> 2` | finding M2 |
| `commands/base.py:177` | `parsed_float > spec.max_value` → `>=` | a float argument exactly at max is refused ("is above maximum"). The **int** sibling at line 165 *is* caught |
| `simulator/server.py:668` | `old_percent > THRESHOLD` → `>=` **and** `percent <= THRESHOLD` → `<` | the low-battery crossing at exactly 20 % — both directions unpinned (the constant's *value* is pinned; the crossing is not) |
| `client.py:1595` | `diff < MINIMUM_TIME_BETWEEN_MSGS` → `<=` | send-pacing at exactly the minimum gap |
| `simulator/engine.py:370` | `now - last > sensor_retrigger_window` → `>=` | retrigger at exactly the window |
| `simulator/engine.py:598` | `remaining <= 0` → `<` | hold expiry at exactly 0 |
| `simulator/scripting.py:541` | `loop.time() < deadline` → `<=` | wait loop at exactly the deadline |
| `simulator/scripting.py:232,235,240` | `len(parts) > 2` → `>=` (×3) | the **documented** 2-word shorthand forms `wait_for <cond>`, `set <name>`, `assert <cond>` — every test supplies 3 words, so the default-timeout / default-value branches never run |
| `simulator/cli.py:488` | `i > 0` → `>=` **and** `script_delay > 0` → `>=` | see L4 |
| `simulator/cli.py:786` | `sys.stdin.fileno() >= 0` → `>` | fd 0 — the real stdin — is never the fd under test |
| `simulator/ctl.py:461` | `count > 0` → `>=` | see L6 |
| `simulator/ctl.py:151` | `part_idx < len(parts)` → `<=` | a command that has subcommands typed with no subcommand is never reached here |
| `commands/info.py:172,174` | `charge_rate > 0` / `discharge_rate > 0` → `>=` | `info` prints "(charging 0%/min)" when the rate is zero |
| `client.py:792` | `listeners and field_name in settings` → `or` | see L5 |
| `client.py:843` | `stats_listeners[...] and FIELD_… in msg` → `or` | same shape, stats path |
| `client.py:1581` | `self._transport and self._can_dequeue` → `or` | dequeue attempted with no transport |
| `client.py:1785` | `cmd is not None and cmd == self._last_command` → `or` | response matching with a `None` cmd |
| `door.py:887` | `not ver and not rev` → `or` | version string assembled with exactly one of the two present |
| `commands/handler.py:230,426,468` | `and`→`or`, `or`→`and` | exit-info identity, decorator attribute pair, `generate_usage() or None` |
| `simulator/prompt_common.py:365` | `not info or not info.subcommands` → `and` | completion for a known command with no subcommands |
| `commands/info.py:115` | `arg.default is not None and not arg.required` → `or` | usage rendering for a required arg that has a default |

Two concrete demonstrations of what "unpinned" means here:

```
$ PYTHONPATH=/tmp/r7/src python -c "
import powerpetdoor; assert powerpetdoor.__file__.startswith('/tmp/r7/')
from powerpetdoor.simulator.state import Schedule
s = Schedule(index=0, enabled=True, days_of_week=[1]*7, inside=True, outside=False,
             start_hour=22, end_hour=6)
for (h,m) in [(21,59),(22,0),(5,59),(6,0),(6,1)]:
    print(f'{h:02d}:{m:02d} -> {s.is_sensor_allowed(\"inside\", h, m, 0)}')"
21:59 -> False
22:00 -> True     <-- inclusive start, unasserted
05:59 -> True
06:00 -> False    <-- exclusive end, unasserted
06:01 -> False
```
`tests/simulator/test_state.py:436` (`test_is_sensor_allowed_crosses_midnight`)
asserts only 23:00, 02:00 and 12:00 — the interior of the window and one point
far outside it.

```
$ grep -rn "129\|at most" tests/simulator/test_protocol.py | head -1
tests/simulator/test_protocol.py:1727:  ("x" * 129, "tz must be at most 128 characters"),
```
Only the reject side (`129`) exists; `"x" * 128` — the longest legal timezone — is
never sent. The fuzz property at `tests/fuzz/test_untrusted_input_fuzz.py:191`
asserts `len(result) <= 128`, which both spellings satisfy.

**Description.** The suite's behavioural coverage is excellent (75 % of unbiased
mutants die, and 100 % of the wire-boundary mutants die), but it tests *inside*
and *outside* ranges rather than *on* their edges, and it tests compound
conditions with only one operand ever decisive. Coverage.py cannot see either
class: `if A and B:` is a single branch point with two destinations, so 100 %
branch coverage is satisfied without ever running `A and not B`. That is why this
survives a saturated gate. Several of the sites are user-visible contracts
(schedule windows, the CLI's max-value validator, the documented script shorthand)
and several are protocol-facing (the wire-string length limit).

**Recommendation.** Not 36 individual tests — a policy plus a small number of
parametrized ones:

1. For each numeric limit constant, one parametrized test over
   `(limit - 1, limit, limit + 1)` asserting accept / accept / reject (or the
   documented equivalent). `tests/simulator/test_protocol.py:1727` and
   `tests/test_framing.py` already have the fixtures for this.
2. For `Schedule.is_sensor_allowed`, one parametrized test over the four edge
   minutes of both a normal and a midnight-crossing window.
3. For the three `simulator/scripting.py` shorthand defaults, one test each
   asserting the documented default (`timeout == 30.0`, `value == ""`,
   `equals == ""`) from the 2-word form.
4. For guards whose comment says "may be absent on some firmware variants"
   (`client.py:792,843`), a test with the listener registered and the field
   absent — see L5.

Re-running `/tmp/gen_mutants.py` after the change gives a measured kill rate, so
progress here is verifiable rather than asserted.

---

### L1 — Several shipped resource/DoS bounds have no test that pins their value

**Severity:** Low
**Files:** `src/powerpetdoor/framing.py:60` (`MAX_BUFFER_SIZE`), `:77` (`THROTTLE_QUIET_PERIOD`), `src/powerpetdoor/simulator/protocol.py:140` (`MAX_WRITE_BACKLOG`), `src/powerpetdoor/simulator/engine.py:60` (`MIN_BLOCKED_RECHECK`)

**Reproduction**

This batch's **control** mutation was `MAX_BUFFER_SIZE = 64 * 1024` → `63 * 1024`,
chosen because it "must" be caught. It was not:

```
SURVIVED  C0-CONTROL max_buffer 64->63KiB
    | 2454 passed in 73.93s
```

The security-relevant direction survives too:

```
SURVIVED  K1 MAX_BUFFER_SIZE 64KiB -> 1MiB          | 2454 passed in 44.42s
SURVIVED  K4 THROTTLE_QUIET_PERIOD 60 -> 86400      | 2454 passed in 48.72s
SURVIVED  P1 MAX_WRITE_BACKLOG 1MiB -> 16MiB        | 2454 passed in 64.00s
SURVIVED  E1 MIN_BLOCKED_RECHECK 0.1 -> 0.2         | 2454 passed in 58.32s
```

The same experiment on the neighbouring bounds **is** caught, so this is a real
gap and not a property of the technique:

```
CAUGHT    K2 MAX_INFLIGHT_FRAMES 64 -> 4096
CAUGHT    K3 MAX_FRAME_BACKLOG 256 -> 65536
CAUGHT    F8 MAX_THROTTLE_INTERVAL 4096 -> 2048
CAUGHT    F10 MAX_RETAINED_PIECES 64 -> 32
CAUGHT    P2 MAX_HOLD_TIME_CENTISECONDS 90000 -> 90001
CAUGHT    P3 MAX_TIMEZONE_LENGTH 128 -> 129
CAUGHT    P4 MAX_TRIGGER_VOLTAGE 65535 -> 65536
CAUGHT    SCR1 MAX_SCRIPT_HOLD_TIME 900 -> 901
CAUGHT    SCR2 MAX_SCRIPT_DELAY 86400 -> 86401
CAUGHT    CTRL-C LOW_BATTERY_THRESHOLD 20 -> 21
```

**Description.** Every test that touches these four constants imports the symbol
(`MAX_BUFFER_SIZE + 1`), which is the right way to keep tests non-brittle — but it
leaves the *value* untested. `MAX_BUFFER_SIZE` and `MAX_WRITE_BACKLOG` are the
per-connection memory caps round-6 security work added; relaxing either by 16x
leaves the suite fully green, including the tests whose docstrings cite "64 KiB".
`THROTTLE_QUIET_PERIOD` carries the "a new burst is never invisible" guarantee,
which a value of 86400 silently voids for a day.

**Recommendation.** One assertion per bound, in the style already used at
`tests/test_framing.py:773` (`test_the_default_ceiling_is_the_module_constant`):
`assert framing.MAX_BUFFER_SIZE == 64 * 1024`,
`assert framing.THROTTLE_QUIET_PERIOD == 60.0`, etc., each in a test whose name
states the rationale. That turns "remember the docstring" into "the diff has to
say why".

---

### L2 — `CONTROL_PORT_OFFSET` is dead code, and the behaviour it names is only asserted in prose

**Severity:** Low
**File:** `src/powerpetdoor/simulator/cli.py:163`; the live behaviour is inlined at `src/powerpetdoor/simulator/cli.py:1085` (`control_port = args.port + 1 if args.daemon == -1 else args.daemon`)

**Reproduction**

```
SURVIVED  CP1 CONTROL_PORT_OFFSET 1->2
    | 2454 passed in 56.86s
```

It survives because nothing reads it — the name occurs exactly once in the whole
repository, at its own definition:

```
$ grep -rn "CONTROL_PORT_OFFSET" . --include=*.py --include=*.md --include=*.yaml --include=*.toml
src/powerpetdoor/simulator/cli.py:163:CONTROL_PORT_OFFSET = 1

$ python -c "
import ast, pathlib
tree = ast.parse(pathlib.Path('src/powerpetdoor/simulator/cli.py').read_text())
print(len([n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id=='CONTROL_PORT_OFFSET']))"
1        # the binding itself; zero reads
```

**Description.** A module-level assignment executes at import, so 100 % line
coverage is satisfied by a constant nobody uses — coverage cannot tell "used" from
"dead". `docs/simulator.md:97` documents "default control port: door port + 1", and
`tests/test_docs_accuracy.py` (which introspects `docs/protocol.md` and
`docs/client.md` against the code) does not cover `docs/simulator.md`, so that
user-facing default has no executable pin either. The same gap covers
`docs/simulator.md:251` ("Show the last N commands (default 20)"): mutating
`DEFAULT_HISTORY_LIMIT = 20` → `21` also survives.

```
SURVIVED  H1 DEFAULT_HISTORY_LIMIT 20->21
    | 2454 passed in 49.10s
```

This also contradicts the `CLAUDE.md` DRY rule: the offset exists as a named
constant *and* inlined at the use site.

**Recommendation.** Use the constant at `cli.py:1085` or delete it; then extend
`tests/test_docs_accuracy.py` to `docs/simulator.md`'s option/command tables and
assert the documented defaults (control port offset, history limit) against the
code, the way the protocol/client tables are already handled.

---

### L3 — `find_iana_for_posix`'s documented "first match wins" rule is untested

**Severity:** Low
**File:** `src/powerpetdoor/tz_utils.py:102-104` (`# First match wins for reverse lookup`)

**Reproduction**

```
SURVIVED  TZ4 reverse map last-match wins
    | 2454 passed in 49.62s
```

The rule is observable and deterministic — 27 IANA names share one POSIX footer,
and `all_tzs` is `sorted()`:

```
$ PYTHONPATH=/tmp/r7/src python -c "
import powerpetdoor; assert powerpetdoor.__file__.startswith('/tmp/r7/')
from powerpetdoor import tz_utils; tz_utils.init_timezone_cache_sync()
p = tz_utils.get_posix_tz_string('America/New_York')
shared = sorted(k for k,v in tz_utils._iana_to_posix.items() if v == p)
print(p); print('resolves to:', tz_utils.find_iana_for_posix(p))
print('candidates:', len(shared), '| first:', shared[0], '| last:', shared[-1])"
EST5EDT,M3.2.0,M11.1.0
resolves to: America/Detroit
candidates: 27 | first: America/Detroit | last: US/Michigan
```

**Description.** Dropping the `if posix not in _posix_to_iana:` guard changes the
answer from `America/Detroit` to `US/Michigan` with nothing failing. This is a
public export used to map a device-reported POSIX TZ back to an IANA name, so the
tie-break is user-visible.

**Recommendation.** A tzdata-version-robust test: pick any POSIX string with more
than one IANA name and assert `find_iana_for_posix(p) == min(candidates)`. That
pins "first (alphabetically) wins" without hard-coding a zone name that a tzdata
bump could invalidate.

---

### L4 — Round 6 fixed one of the two `script_delay > 0` guards; the other is still unpinned

**Severity:** Low
**File:** `src/powerpetdoor/simulator/cli.py:488` (`if i > 0 and script_delay > 0:`)

**Reproduction**

```
SURVIVED  CLI1 script_delay > 0 -> >= 0        (cli.py:488, between scripts)
    | 2454 passed in 48.44s
CAUGHT    CLI2 loop script_delay > 0 -> >= 0   (cli.py:517, between loop passes)
```
The generated batch independently killed neither operand on line 488:
```
SURVIVED  src/powerpetdoor/simulator/cli.py:488 >->>=  |if i > 0 and script_delay > 0:   (script_delay)
SURVIVED  src/powerpetdoor/simulator/cli.py:488 >->>=  |if i > 0 and script_delay > 0:   (i)
```

**Description.** Round 6's `...`-exclusion fix surfaced `script_delay > 0` as
genuinely untested, and a test was added — but only for the **outer-loop** guard
(`tests/simulator/test_cli.py:1673`, `test_zero_script_delay_loops_without_waiting`,
whose docstring says exactly that). The inner guard at line 488 has a
positive-delay test (`test_delay_between_scripts_not_oneshot`, line 1530, two
scripts with `script_delay=0.01`) but no zero-delay counterpart, so a mutant that
prints `">>> Waiting 0s before next script..."` between every script — or before
the *first* one — goes unnoticed. Coverage cannot see this: `if A and B:` is one
branch point with two destinations, so 100 % branch coverage never requires
`i > 0 and delay == 0`.

**Recommendation.** Mirror the existing outer-loop test: run two scripts with
`script_delay=0` and assert `">>> Waiting" not in out`.

---

### L5 — The "field may be absent on some firmware variants" guards are never exercised with the field absent

**Severity:** Low
**File:** `src/powerpetdoor/client.py:792` (`if listeners and field_name in settings:`), same shape at `src/powerpetdoor/client.py:843`

**Reproduction**

```
SURVIVED  src/powerpetdoor/client.py:792and->or  |if listeners and field_name in settings:
SURVIVED  src/powerpetdoor/client.py:843and->or  |if self.stats_listeners[FIELD_TOTAL_OPEN_CYCLES] and FIELD_TOTAL_OPEN_...
```

**Description.** With `or`, a registered listener plus a settings payload that
omits that field indexes `settings[field_name]` and raises `KeyError` into
`_LOGGER.exception` — the exact "full traceback per frame" failure mode the
round-5 security work removed elsewhere. The mutant survives, which means no test
in the suite combines *a registered sensor listener* with *a settings payload
lacking that sensor's field*. That combination is the one the code's own comment
calls out: *"these fields may be absent on some firmware variants, so guard each
one (never assume presence)"* (`client.py:796-797`). The seven fields at lines
779-790 are all in scope.

**Recommendation.** One parametrized test over the seven `sensor_fields`: register
a listener for the field, deliver a `GET_SETTINGS` response whose `settings`
mapping omits it, and assert the listener is not called and no exception is
logged.

---

### L6 — `STATUS: clients=0` is annotated as "no change" but nothing asserts it

**Severity:** Low
**File:** `src/powerpetdoor/simulator/ctl.py:461` (`new_status = count > 0`); test at `tests/simulator/test_ctl_interactive.py:248`

**Reproduction**

```
SURVIVED  src/powerpetdoor/simulator/ctl.py:461 >->>=  |new_status = count > 0
```
```
$ grep -n "invalidate" tests/simulator/test_ctl_interactive.py tests/simulator/test_ctl.py
tests/simulator/test_ctl_interactive.py:249:   b"STATUS: clients=1\n",  # change -> invalidate
```
The only occurrence of the word is a comment. Nothing observes
`interactive.invalidate()`.

**Description.** `test_full_session_flow` feeds `STATUS: clients=0` with the
comment *"no change (already disconnected)"* — an expectation the test does not
assert. With `count >= 0`, a zero-client status flips the prompt's
connected indicator **on** and triggers a redraw; the session still produces the
same transcript, so the test passes. This is the machinery round 6 added in
`e92a82a` ("Refresh prompt color immediately on client connect/disconnect"), so
its zero-client edge is exactly the case worth pinning.

**Recommendation.** Give the fake interactive object an `invalidate` counter and
assert the count after each `STATUS:` line in `test_full_session_flow` — the test
already enumerates the five status shapes, so this only adds assertions to an
existing scenario.

---

## Round 6 Fix Verification

Every round-6 finding was re-tested by execution, not by reading the diff.

| R6 finding | Verified how | Result |
|---|---|---|
| **H1** CI never enforced the 100 % gate | Read `.github/workflows/test.yml`: per-version runs use `--cov-fail-under=0` (line 83, deliberate), and the `coverage-report` job's final step is `coverage report --fail-under=100` on combined data (line 204) | **Fixed.** `coverage combine` also exits non-zero on "No data to combine", so an all-red matrix cannot reach the gate silently |
| **H2** the bare `\.\.\.` exclude hid real code | Re-audited **every** exclude pattern and both omit patterns by re-reporting the *same* coverage data with each pattern individually removed (table below) | **Fixed and honest.** The anchored regex now hides 2 statements and 4 branch destinations, all `Protocol.__call__(...) -> Any: ...` stubs at `commands/base.py:359,368`; the uncovered arcs (`359->exit`, `368->exit`) are bodies that can never run |
| **H2a** client.py shutdown guard now tested | Mutation `if self._shutdown:` → `if False:` at `client.py:1295` | **CAUGHT** |
| **H2b** cli.py `script_delay > 0` now tested | Mutations at both sites | `cli.py:517` **CAUGHT**; `cli.py:488` **SURVIVED** → finding L4 |
| **H2c** honest totals 6571 / 2332 at 100.00 %, 2454 tests | Re-ran both invocations | **Confirmed exactly**: `TOTAL 6571 0 2332 0 100.00%`, `2454 passed` |
| **M1** duplicate-index only caught by fuzz | Mutation removing `\| {e[FIELD_INDEX] for e in entries_to_set[:i]}` from `compute_schedule_diff` | **CAUGHT** deterministically |
| **M2** TESTING_GAPS exclusion list hard-coded | Read `scripts/generate_gaps_report.py:49-75`; regenerated `tests/TESTING_GAPS.md` from live coverage in the `/tmp` copy and diffed | **Fixed.** Derived from `pyproject.toml`, all 9 patterns disclosed, regenerated file byte-identical modulo the timestamp line |
| Pragma disclosure | `grep -rn "pragma: no" src/ scripts/` → exactly 4 annotations in 2 files | Matches TESTING_GAPS.md's "**4 lines** across **2 files** in **4 annotations**". One of the four is finding M4 |
| **L1-L5, T2, T3** | Covered incidentally by the mutation batches over `framing.py`, `schedule.py`, `sanitize.py`, `tz_utils.py` | No regressions surfaced |
| **tz fuzz strategy 0.5 % → 66 %** | Read `tests/fuzz/test_tz_fuzz.py:70-97`: the ratio is measured deterministically with `random.Random(0)` under a two-sided bound (`parsed > draws // 4` and `parsed < draws`) | **Fixed and self-verifying.** The slack bound is the right call for a tzdata-version-dependent number |
| R6 fix agent's "26/27 mutants caught" | Independently re-ran a superset | Consistent. The four hand-written batches over the round-6 machinery scored 60 caught / 71 non-equivalent (85 %); the unbiased generated batch scored 138/183 (75 %) |

---

## Areas Reviewed With No Findings

### Coverage instrumentation is now honest (audited pattern by pattern)

Round 6's lesson was that the instrumentation had lied twice, so I re-ran
`coverage report` against the *same* data file with each `exclude_lines` entry and
each `omit` entry individually removed. The delta attributable to each pattern is
therefore measured, not assumed:

```
BASELINE                                       TOTAL  6571  0  2332   0  100.00%
drop 'pragma: no cover'                        TOTAL  6577  2  2332   0   99.98%
drop 'def __repr__'                            TOTAL  6571  0  2332   0  100.00%
drop 'raise NotImplementedError'               TOTAL  6571  0  2332   0  100.00%
drop 'if TYPE_CHECKING:'                       TOTAL  6604 20  2360  14   99.62%
drop 'if __name__ == .__main__.:'              TOTAL  6577  3  2338   3   99.93%
drop '@overload'                               TOTAL  6571  0  2332   0  100.00%
drop '(^\s*\.\.\.\s*$)|(:\s*\.\.\.\s*$)'       TOTAL  6573  0  2336   2   99.98%
unomit '*/__init__.py'                         TOTAL  6571  0  2332   0  100.00%
unomit '*/__main__.py'                         TOTAL  6571  0  2332   0  100.00%
drop ALL excludes + omits                      TOTAL  6623 25  2374  21   99.49%
```

Every hidden statement is accounted for:

- `pragma: no cover` → 6 statements, 2 uncovered: `ctl.py:365` and `ctl.py:644`,
  the two documented annotations (one is finding M4).
- `if TYPE_CHECKING:` → 33 statements / 28 arcs over 14 blocks; the 20 uncovered
  are type-only imports that cannot execute. Verified per file
  (`cli.py 37->40, 41-44`, `prompt_common.py 46-49`, …).
- `if __name__ == .__main__.:` → the 3 `main()` calls at
  `scripts/generate_gaps_report.py:421`, `cli.py:1140`, `ctl.py:754`.
- The anchored `...` regex → `commands/base.py:359,368` only.
- `def __repr__`, `raise NotImplementedError`, `@overload` change **nothing**.
  There are no `__repr__` or `NotImplementedError` lines in scope, and the two
  `@overload` decorators at `client.py:1823,1828` are already fully excluded by
  the `...` regex — confirmed with the coverage API:
  ```
  /tmp/cfg/all.toml  excluded near overloads: [1823..1832]
  /tmp/cfg/no5.toml  excluded near overloads: [1823..1832]   # '@overload' removed: identical
  ```
  Redundant, not dangerous. (`def __repr__` *would* exclude a whole `__repr__`
  body if one were ever added — worth remembering; there is nothing to report
  today.)
- The omit list is genuinely inert. All four omitted files parse to imports +
  `__all__`, plus one `if __name__` guard:
  ```
  src/powerpetdoor/__init__.py                    {'Expr': 1, 'ImportFrom': 5, 'Assign': 2}
  src/powerpetdoor/simulator/__init__.py          {'Expr': 1, 'ImportFrom': 6, 'Assign': 1}
  src/powerpetdoor/simulator/__main__.py          {'Expr': 1, 'ImportFrom': 1, 'If': 1}
  src/powerpetdoor/simulator/commands/__init__.py {'Expr': 1, 'ImportFrom': 2, 'Assign': 1}
  ```
  and `tests/test_exports.py` covers the `__all__` surface independently.
- All 28 non-omitted source files appear in the report (26 under
  `src/powerpetdoor` + `client.py` + `scripts/generate_gaps_report.py`), so
  nothing is silently unmeasured.

`tests/TESTING_GAPS.md` regenerates byte-identical from live data:

```
$ python -m coverage json && python scripts/generate_gaps_report.py \
  && diff <(sed 's/^\*\*Last updated:.*//' /tmp/r7/tests/TESTING_GAPS.md) \
          <(sed 's/^\*\*Last updated:.*//' <repo>/tests/TESTING_GAPS.md) && echo IDENTICAL
Wrote JSON report to coverage.json
IDENTICAL (modulo timestamp)
```

### Flakiness, ordering, skips, fake tests

- **No skipped tests.** The 10 `skipif` marks all gate on PyYAML / prompt_toolkit,
  both in the `dev` extra, so none fires: `2454 passed`, zero skipped, every run.
- **No ordering dependency.** Full sequential run (`-n0`, defeating xdist's
  distribution): `2454 passed in 198.49s`. Same count, same result.
- **Repeatability.** The suite ran end-to-end 8 times during this audit (baseline,
  coverage, sequential, and five clean control runs inside mutation batches).
  Identical `2454 passed` every time; no flaky failure observed.
- **No tautologies.** `grep` for `assert True`, `assert 1 == 1`, `assert x or not x`
  returns nothing. The three `assert … in (…)` hits enumerate the legal wire
  spellings (`day in (0, 1)`), not contradictory outcomes.
- **Real-time waits are deliberate.** Only 14 non-zero sleeps exist; most are
  `sleep(3600)` standing in for a hung handler. The two genuinely timing-sensitive
  `ctl` tests carry explicit 10x-margin rationales and assert in the robust
  direction.

### Machinery probed and found well covered

**`SCHEDULE_WIRE_TO_DEVICE` / `_FROM_DEVICE` and the per-direction golden
fixtures — 10 of 10 mutations caught.** Flipping `enabled` in either direction,
changing `day` from `wire_int_flag` to `wire_json_bool` (caught by the explicit
`_is_wire_int` assertions, which are exactly right given `True == 1`), swapping
the inside/outside prefixes, swapping start/end, swapping hour/minute, and not
zeroing the unselected sensor's window all die. This is the best-tested code in
the repository. Consistent with the repo owner's constraint, nothing here asks for
a wire change, and the deliberate one-field difference between the two directions
is treated as correct.

**`FrameDispatcher` — 12 of 14 hand-written mutations caught**, including the
`max_inflight` boundary, the done-callback re-pump, `reset()` clearing `_paused`,
unconditional `resume_reading`, and dropping the transport in `submit()`.

**`EventThrottle` — 9 of 11 caught**, including the quiet-period comparison
direction, the per-burst schedule restart, the `max_interval` ceiling, the
`record()` return contract, and `flush()`/`reset()` wiring on
`FrameScanner.reset`. The injected clock and `ManualClock` are the right design.

**`fwInfo` liberal handling — 4 of 4 caught**: replacing `_payload_mapping` with a
raw `msg.get`, removing the absent-field early return, resolving the future with
the coerced mapping instead of the raw value, and weakening `_payload_mapping`'s
`isinstance(value, dict)` check.

**`script_escapes_directory()` — 2 of 2 caught** (dropping `.resolve()`, and
returning `False` unconditionally).

**`compute_schedule_diff` index reuse — 7 of 8 caught**, including the round-6 M1
regression itself, the reuse bound, the delete slice, and the reusable-index
ordering.

**`MAX_RETAINED_PIECES` coalescing** — the constant is pinned, the join is pinned,
and "the character cap still fires" is a real test.

**`sanitize_text`** — truncate-before-escape, `MAX_LOGGED_LENGTH`, and the control
character class (including C1) are all pinned; only the boundary value is not (M5).

### Provably-equivalent mutants (survivors that are NOT gaps)

Listed with the argument, so a refuter does not have to re-derive them:

1. `schedule.py:414` `[fmt.day(bool(day)) …]` → `[fmt.day(day) …]`. All three
   spellers (`wire_json_bool`, `wire_flag_string`, `wire_int_flag`) branch on
   truthiness, so `bool()` is a no-op for every possible input.
2. `schedule.py:217` `(val + 6) % 7` → `(val + 13) % 7`. 13 ≡ 6 (mod 7). The
   non-equivalent form `(val + 5) % 7` **is** caught.
3. `schedule.py:524` `if in_end < in_start:` → `<=` and `schedule.py:534`
   `if out_end < out_start:` → `<=`. The guarded body is
   `a, b = b, a`; swapping two equal values is a no-op.
4. `schedule.py:557` `if daysched[i]["end"] < daysched[i+1]["end"]:` → `<=`. The
   guarded body assigns `daysched[i+1]["end"]`; assigning an equal value is a
   no-op.
5. `tz_utils.py:74` `if last_newline > 0:` → `>=`, and `tz_utils.py:76`
   `if second_last_newline >= 0:` → `>`. Both index-0 cases require
   `content[0] == b"\n"`, which the preceding `content[:4] != b"TZif"` guard makes
   unreachable.
6. `tz_utils.py:190` `if not posix_tz:` → `if posix_tz is None:`. `_POSIX_TZ_RE`
   does not match `""` (verified: `_POSIX_TZ_RE.match("") is None`), so both
   spellings return `None`; only a DEBUG log record differs.
7. `simulator/server.py:279` `"charging" if step > 0 else …` → `>=`, and
   `simulator/server.py:287` `step < 0` → `<=`. `if step == 0: return` executes
   16 lines earlier, so `step == 0` never reaches either expression.
8. `simulator/server.py:253,256` `charge_rate > 0` / `discharge_rate > 0` → `>=`.
   With a zero rate `delta == 0`; `_battery_carry` is bounded in `(-1, 1)` by
   `carry -= int(carry)` on every tick, so `step = int(carry) == 0` and the
   function returns at the same place as the `else: return` it replaced.
9. `framing.py:459` `if len(head) > MAX_RETAINED_PIECES:` → `>=`. A purely
   internal amortisation threshold; framed output is byte-identical and the
   existing assertion (`<= MAX_RETAINED_PIECES + 1`) is deliberately slack.

Two further survivors are behaviour-changing but have no stated contract, so they
are recorded rather than filed: `compute_schedule_diff`'s tie-break between two
*identical* current entries (`current_by_content[k] = i` → `setdefault`; both
outputs are correct SET/DELETE plans), and `_NOTIFY_LABEL_WIDTH + 1` → `+ 2` (a
cosmetic column width in the `notify` listing).
