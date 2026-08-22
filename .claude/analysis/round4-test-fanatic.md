# Test Fanatic Analysis — Round 4

## Summary

Verified by execution, not by reading.

- `uv run pytest --ignore=tests/fuzz --cov -q` (the exact CI unit-matrix
  invocation) → **1905 passed, 100.00% lines and branches**, gate satisfied
  (6,062/6,062 lines, 2,190/2,190 branches).
- `uv run pytest -q` (full, with fuzz) → **1926 passed in 17.6 s**.
- `ruff check src tests scripts`, `ruff format --check`, `mypy src` → all clean.
- **Every Round-3 finding is fixed**, and I re-proved each behavioural one by
  re-running my exact Round-3 mutations (table at the bottom). R3-M4's hang is
  now a 3.7 s named failure. All six previously-surviving mutations are caught.
- `pytest-timeout` genuinely works under `-n auto`: I fed the suite a
  deadlocked `asyncio.Event().wait()` and a blocked `threading.Event().wait()`
  and both failed at the cap with `Failed: Timeout (>5.0s) from pytest-timeout`
  plus a thread stack dump. CI jobs now carry `timeout-minutes: 30`.

Coverage is saturated and tells you nothing, so I **mutated**: **111 real
mutations** (plus one no-op control to validate the harness), aimed
overwhelmingly at the ~167 tests and the new production code the Round-3 wave
introduced. I also ran a **5-property, 14,000-example Hypothesis probe** of my
own against the new untrusted-input layer, because that layer is exactly what
property testing is for and the fuzz suite did not grow with it.

> Harness note, worth recording: my first pass of 9 mutations all "survived"
> because the venv's editable-install `.pth` shadows a `/tmp` copy of `src/`.
> Every result below was produced with `PYTHONPATH=<copy>/src` forcing the
> mutated tree, and validated against a control mutation that must fail.

| Outcome | Count |
|---|---|
| Caught (a specific named test failed) | 94 |
| Survived — meaningful | 13 |
| Survived — equivalent/harmless | 4 |

94/111 is a strong result, and the areas the Round-3 wave hardened
*deliberately* are excellent: the wire-validation layer in `protocol.py` is
**14/14**, `state.py`'s schedule validator **7/7**, the control-channel reaping
and log-feedback-loop guards **10/10**, ctl's `LOG:` streaming **4/4**, the
engine's deferred-intent rework **3/3**, script status/stop **4/5**.

The 13 meaningful survivors cluster into one theme: **fixes that were applied
to several sites but tested at only one**, and **new helpers that are 100%
covered because everything calls them but are never actually asserted on**.
One of them is a live production bug, reproduced by fuzzing.

Finding counts: **Critical: 0, High: 0, Medium: 4, Low: 6, Trivial: 3** (13 total).

---

## Findings

### Medium

---

#### R4-M1. `door.Schedule.from_dict` — the public library API that parses device schedule payloads — has neither the Round-3 hardening nor any hostile-input tests; eight distinct crash shapes, found by fuzzing in 60 seconds

**Severity:** Medium (top finding — this is a production bug, not only a test gap)
**Files:** `src/powerpetdoor/door.py:264-310` (`Schedule.from_dict`),
`src/powerpetdoor/door.py:190-192` (`ScheduleTime.from_dict`),
call sites `door.py:960` (`get_schedule`), `door.py:1025` (`get_schedules`),
`door.py:1338` (`_on_schedule_update`);
contrast `src/powerpetdoor/simulator/state.py:281-330` +
`tests/simulator/test_state.py::TestScheduleFromDictRejectsHostileInput`

Round 3's wave hardened the **simulator's** `Schedule.from_dict` thoroughly:
`_coerce_schedule_int` (now catching `OverflowError`), `_coerce_schedule_day`,
`_coerce_schedule_days`, `_require_schedule_field`, `_coerce_schedule_time`,
and ~14 tests that assert the exact rejection message for each. My mutations
confirm all of it: `s1`–`s7` are **7/7 caught**.

The **library's** twin — the one that actually consumes untrusted bytes off the
wire — got none of it. I ran a Hypothesis totality property over it
(3,000 examples of realistic protocol-shaped dicts) and it produced **8 distinct
non-`ValueError` exceptions**. Minimal reproductions, run against pristine
`HEAD`:

```
>>> Schedule.from_dict({"daysOfWeek": None})
TypeError: 'NoneType' object is not iterable
>>> Schedule.from_dict({"inside": True, "in_start_time": 5})
AttributeError: 'int' object has no attribute 'get'
>>> Schedule.from_dict({"inside": True, "in_start_time": None})
AttributeError: 'NoneType' object has no attribute 'get'
```
(plus `'list'/'bool'/'float'/'str' object has no attribute 'get'` and
`'float' object is not iterable` for the other field shapes.)

Why it matters at each call site:

- `get_schedule()` (`door.py:960`) — `Schedule.from_dict(result)` on the raw
  `CMD_GET_SCHEDULE` response. A malformed device reply raises `AttributeError`
  straight out of a documented public coroutine, not `ValueError` and not
  `CommandError`. No caller can reasonably catch that.
- `_on_schedule_update()` (`door.py:1338`) — reached from
  `client._notify_listeners`, which isolates and logs. So the exception is
  swallowed and **the cached schedule list silently goes stale** — the door
  facade reports a schedule set that no longer matches the device.
- `get_schedules()` (`door.py:1025`) — inside `except Exception: logger.exception`,
  the one site that fails loudly. Fine.

The same run also proved the rest of the layer is solid: `sanitize_text`,
`_coerce_wire_number/int/string/flag`, `simulator.state.Schedule.from_dict`,
`validate_schedule_entry` and `compress_schedule` produced **zero** unexpected
exceptions across 14,000 examples, and `sanitize_text` was idempotent and
leaked no control character in any of them.

**Recommendation:** give `door.Schedule.from_dict` the same treatment its
simulator twin got — reuse the shape (`_coerce_*` helpers raising `ValueError`
with the field named) rather than writing a third variant, per CLAUDE.md's
"two implementations = refactor". Then port
`TestScheduleFromDictRejectsHostileInput` to `tests/test_door.py`, and add the
totality property to `tests/fuzz/test_schedule_fuzz.py` (see R4-L5) so the
next field added cannot regress it.

---

#### R4-M2. `render_result()` — the Round-3 single-point sanitizer for everything printed to an operator's terminal — is never asserted to sanitize anything; removing the call survives all 1926 tests

**Severity:** Medium
**Files:** `src/powerpetdoor/simulator/prompt_common.py:56-64` (`render_result`),
sinks `cli.py:538`, `cli.py:542`, `cli.py:775`, `ctl.py:602`, `ctl.py:610`

Round 3 replaced the old `sanitize_text` in `prompt_common.py` with
`render_result`, whose entire stated purpose is:

> "Network-poisoned state can reach a command's own output (the string a
> hostile `SET_TIMEZONE` stored is echoed by the `timezone` command, for one),
> so every result printed to a terminal is sanitized here — one place to get
> it right instead of one per print site."

Mutation, run against the **whole suite**:

```python
-    return f">>> {sanitize_text(message)}"
+    return f">>> {message}"
```
→ **SURVIVED: 1926 passed in 19.6 s.**

The function is at 100% line coverage — every CLI and ctl print goes through
it — but the only property anything asserts is the `>>> ` prefix. Nothing
anywhere feeds it a control character. The real behaviour today is
`render_result("TZ: \x1b[2J\x1b[H pwned")` → `'>>> TZ: \\x1b[2J\\x1b[H pwned'`,
and that is unpinned.

Note the asymmetry that makes this a blind spot: the *library-side* sinks are
all pinned (`tests/test_sanitize.py::TestLibraryLogSinks` — my mutations `t1`,
`t2`, `t3` were each caught by a named test), and `sanitize_text` itself has 10
unit tests (`t5`, `t6` caught). It is only the front-end sink — the one whose
threat model is an actual terminal, not a log file — that has no test.

**Recommendation:** two direct unit tests next to the other `prompt_common`
tests, plus one end-to-end. The end-to-end is the valuable one and the project
already has the machinery for it:

```python
def test_render_result_escapes_control_characters():
    assert render_result("TZ: \x1b[2J") == ">>> TZ: \\x1b[2J"

def test_render_result_preserves_plain_text():
    assert render_result("AC set to connected") == ">>> AC set to connected"

# and, in test_ctl_interactive.py, drive a real poisoned value out to the
# recorder: set the daemon's timezone to "UTC\x1b[2J" over the wire, run
# `timezone` from the ctl prompt, and assert the recorded stdout contains
# "\\x1b" and not "\x1b".
```

---

#### R4-M3. The Round-3 "`make_bool`, not truthiness" day-flag fix landed at three sites and is tested at one; reverting either of the other two passes all 1926 tests

**Severity:** Medium
**Files:** `src/powerpetdoor/door.py:298` (untested),
`src/powerpetdoor/schedule.py:249` (untested),
`src/powerpetdoor/simulator/state.py:90` (tested — `_coerce_schedule_day`)

The fix's own comment states the bug it prevents: *"`bool("0")` is True, and a
firmware variant that sends `"0"`/`"1"` day flags (as it already does for
`enabled`) would otherwise expand to every day of the week."* That is an
access-control failure — a day the operator disabled becomes active.

| Site | Mutation | Full-suite result |
|---|---|---|
| `simulator/state.py:90` | `make_bool(...)` → `bool(value)` | **CAUGHT** (2 named tests) |
| `door.py:298` | `make_bool(d) is True` → `bool(d)` | **SURVIVED — 1926 passed** |
| `schedule.py:249` | `make_bool(...) is True` → truthiness | **SURVIVED — 1926 passed** |

Both untested sites change behaviour today, on pristine `HEAD`:

```
>>> Schedule.from_dict({"daysOfWeek": ["0"]*7, "inside": True}).days_of_week
[False, False, False, False, False, False, False]     # with the fix
[True,  True,  True,  True,  True,  True,  True ]     # reverted

>>> compress_schedule([{... "daysOfWeek": ["0"]*7 ...}])
[]                                                    # with the fix
[<every day of the week>]                             # reverted
```

This is the identical shape as Round 3's R3-M2 (`to_dict`'s wire ints tested on
the library side, untested on the simulator side) — with the sides swapped.
Every one of these three-site fixes needs the same three-site test.

**Recommendation:** a shared parametrised case per site, mirroring the one that
already works in `test_state.py`:

```python
@pytest.mark.parametrize("flag", ["0", 0, False])
def test_disabled_day_flags_are_read_as_disabled(flag):
    s = Schedule.from_dict({"daysOfWeek": [flag] * 7, "inside": True})
    assert s.days_of_week == [False] * 7        # bool("0") is True

def test_compress_ignores_string_zero_day_flags():
    assert compress_schedule([_entry(days=["0"] * 7)]) == []
```

---

#### R4-M4. `aclose()` — the whole Round-3 T2 fix — is unpinned at its only real call site *and* its documented `timeout` parameter is ignored undetected

**Severity:** Medium
**Files:** `src/powerpetdoor/door.py:538` (`PowerPetDoor.disconnect`),
`src/powerpetdoor/client.py:1060-1084` (`aclose`),
`tests/test_client.py:2878-2917` (`TestAclose`)

`TestAclose` has three tests and they do pin the mechanism (`a2` — dropping
`task.cancel()` — was caught, `a4` — dropping `self.shutdown()` — was caught).
What is not pinned is that anything *uses* it, or that it uses it correctly:

| Mutation | Full-suite result |
|---|---|
| `door.disconnect`: `await self._client.aclose(self.default_timeout)` → `self._client.shutdown()` (i.e. revert R3-T2 entirely) | **SURVIVED — 1926 passed** |
| `aclose`: `timeout=self.cfg_timeout if timeout is None else timeout` → `timeout=self.cfg_timeout` (ignore the argument) | **SURVIVED — 1926 passed** |
| `aclose`: `tasks = [t for t in self._handler_tasks if t is not current]` → no filter (self-wait) | **SURVIVED** |

The second one survives for an instructive reason:
`test_aclose_cancels_a_handler_that_overruns` passes `timeout=0.01`, but
`mock_client`'s `cfg_timeout` is `5.0`, so ignoring the argument just makes the
test take 5 s longer and still cancel. The argument's *value* is never observed.
The third means the "don't await yourself" guard — which would otherwise
deadlock `aclose()` called from inside an `on_disconnect` handler until the
timeout — has no test.

`door.disconnect()`'s docstring makes a promise nothing checks: *"Async
lifecycle handlers still in flight (e.g. the `on_disconnect` this call itself
triggers) are awaited, then cancelled if they overrun `default_timeout`, so
nothing outlives this call."*

**Recommendation:**
```python
async def test_door_disconnect_awaits_an_async_disconnect_handler(door):
    finished = []
    async def slow():
        await asyncio.sleep(0)
        finished.append("done")
    door._client.add_handlers("app", on_disconnect=slow)
    await door.disconnect()
    assert finished == ["done"]        # fails if disconnect() only shutdown()s

async def test_aclose_honours_its_timeout_argument(mock_client):
    """cfg_timeout is 5.0; a 0.01 s aclose must return in well under that."""
    client, _, _ = mock_client
    client.add_handlers("app", on_disconnect=lambda: asyncio.sleep(3600))
    async with asyncio.timeout(1.0):   # fails if cfg_timeout is used instead
        await client.aclose(timeout=0.01)
```

---

### Low

---

#### R4-L1. `_ConnectionAttempt.connection_lost`'s `_adopted` guard survives removal; the test named "forwards nothing at all" can only fail for the `data_received` half

**Severity:** Low
**Files:** `src/powerpetdoor/client.py:1694-1700`,
`tests/test_client.py:2647` (`test_shim_ignores_a_declined_transports_lifecycle_events`)

Of nine mutations against the new connection-identity shim, **seven were
caught** by a named test — the superseded-transport check both ways, the
`data_received` guard, the `_declined` counter increment and decrement, the
shutdown-decline branch, and `abort()` vs `close()` on the second transport.
That is good work. One meaningful survivor:

```python
     def connection_lost(self, exc):
-        if not self._adopted:
-            return
+        # (removed)
```
→ **SURVIVED — 1926 passed.**

It survives because the *other* guard immediately below (`current is not
self._transport`) masks it in the scenario the test exercises: the test
declines because the client is already connected, so `client._transport` is the
live one and the stale-transport check returns anyway. The test's docstring —
"A shim whose transport was declined forwards nothing at all" — is therefore
half-vacuous; only the `attempt.data_received(...)` line above it can fail.

The scenario where `_adopted` is the *only* guard is the shutdown decline,
where `client._transport` is `None`. Proven by execution against a pristine and
a mutated tree:

```python
c.shutdown()                     # shutdown lands mid-connect
attempt = _ConnectionAttempt(c)
attempt.connection_made(T())     # declined + aborted
c.reset_shutdown()               # app re-enables the client
attempt.connection_lost(None)    # asyncio finally delivers the aborted loss
```
```
--- pristine ---   reconnect scheduled: False
--- mutated  ---   ERROR powerpetdoor.client: The server closed the connection. Reconnecting...
                   reconnect scheduled: True
```
A bogus ERROR about a connection nobody lost, and a wasted reconnect against a
device with exactly one connection slot — from a socket the client explicitly
refused. Exactly what R3's L2/T1 fix existed to prevent.

**Recommendation:** add the missing half to the existing test (it costs three
lines and makes the docstring true), using the `disconnected_client` fixture so
`_transport` really is `None`:

```python
async def test_shim_ignores_a_shutdown_declined_transports_loss(disconnected_client, caplog):
    client = disconnected_client
    client.shutdown()
    attempt = _ConnectionAttempt(client)
    attempt.connection_made(MockTransport())     # declined by the shutdown branch
    client.reset_shutdown()
    with caplog.at_level(logging.ERROR, logger="powerpetdoor.client"):
        attempt.connection_lost(None)
    assert client._reconnect_task is None
    assert "The server closed the connection" not in caplog.text
```

---

#### R4-L2. `generate_gaps_report.py` truncates any pragma reason containing `()` — two of the five rows in the committed `tests/TESTING_GAPS.md` are wrong *right now*, and all 36 new tests use paren-free reasons

**Severity:** Low
**Files:** `scripts/generate_gaps_report.py:32-35` (the regex),
`tests/TESTING_GAPS.md` (committed, incorrect),
`tests/test_gaps_report.py:135, 165, 178, 209, 396`

The reason group is `(?:\s*\(([^)]+)\))?` — non-greedy up to the first `)`. Two
of the project's five pragmas have a reason containing `()`:

| Source | Reason in `TESTING_GAPS.md` | Actual reason |
|---|---|---|
| `cli.py:99` | `defensive: enable(` | `defensive: enable() always installs a handler` |
| `cli.py:636` | `bound after start(` | `bound after start()` |

Verified directly against the live regex. R3-M5's whole point was "the
generator of the project's self-reported gap analysis is the one thing nobody
checks"; 36 tests were added, they are good (my mutations `g1`–`g19` were
**17/20 caught**, including the `#pragma:`-without-a-space slicing bug the fix
wave found), and this defect still shipped — because every fixture reason is
paren-free (`(defensive: cannot happen)`, `(the real reason)`, `(first)`,
`(second)`, `(tight)`, `(unreachable)`).

This is documentation, not behaviour, which is why it is Low. It is still a
wrong artifact committed to the repository and presented as the project's
coverage-exclusion justification.

**Recommendation:** make the reason group balanced-paren-tolerant (`\((.+)\)$`
anchored to end-of-line is enough here, since a pragma comment is always last
on its line), and add the real-world case to the fixtures:

```python
def test_reason_containing_parentheses_is_not_truncated(tmp_path):
    src = tmp_path / "src" / "pkg"; src.mkdir(parents=True)
    (src / "m.py").write_text("x = 1  # pragma: no branch (defensive: enable() installs it)\n")
    entry = _collect_pragma_exclusions(src)["src/pkg/m.py"][0]
    assert entry["reason"] == "defensive: enable() installs it"
```

---

#### R4-L3. `WireCapture.feed`'s partial-frame carry-over — the entire reason R3-L3 created the shared helper — has no test; dropping the buffer prepend passes all 1926 tests

**Severity:** Low
**File:** `tests/simulator/wire.py:50-59`

R3-L3 was fixed well: the hand-rolled brace-depth scanner is gone, both capture
classes subclass `WireCapture`, and the production `extract_frames` does the
framing. The helper's `get_status_sequence()` `CMD == DOOR_STATUS` filter is
genuinely pinned — mutating it to match on the field instead was **caught** by
two named tests, and suppressing a real `DOOR_SLOWING` broadcast in the engine
now fails 4-5 named tests across both integration files (R3-L1/L2 verified).

But the docstring's other promise — *"Partial frames are carried over to the
next call, so a message split across reads is never lost"* — is unasserted:

```python
-  frames, self._buffer, _diag = extract_frames(self._buffer + data.decode("ascii"))
+  frames, self._buffer, _diag = extract_frames(data.decode("ascii"))
```
→ **SURVIVED — 1926 passed.**

No test ever splits a frame across two `feed()` calls, so the reassembly this
shared helper exists to provide is load-bearing for 41+ integration tests and
never exercised. Test infrastructure that silently drops messages produces
false *passes*, which is the worst failure mode available.

**Recommendation:** a handful of direct unit tests on the helper itself — it
takes plain bytes and returns plain dicts, so this is ten lines:

```python
def test_feed_reassembles_a_frame_split_across_reads():
    cap = WireCapture(None, None)
    assert cap.feed(b'{"CMD": "PO') == []
    assert cap.feed(b'NG"}') == [{"CMD": "PONG"}]

def test_feed_is_string_aware_about_braces():
    cap = WireCapture(None, None)
    assert cap.feed(b'{"CMD": "a}b"}') == [{"CMD": "a}b"}]   # the C5 defect
```

---

#### R4-L4. Three more `generate_gaps_report` survivors: the gap threshold, the gap ordering, and the "Lines Covered" denominator

**Severity:** Low
**File:** `scripts/generate_gaps_report.py:154, 201, 233`;
`tests/test_gaps_report.py:53-70, 273, 297-313, 350-366`

| Mutation | Effect if it shipped | Result |
|---|---|---|
| `total_lines = covered_lines + missing_lines` → `covered_lines` | "Lines Covered \| 5,900 / 5,900" on a project with 162 uncovered lines — the report claims a full denominator it does not have | **SURVIVED** (full suite) |
| `if file_percent < 100` → `< 99` | a file at 99.5% is silently absent from "Current Gaps" — in the report whose only job is listing gaps | **SURVIVED** |
| `files_with_gaps.sort(key=...)` → removed | worst-covered file no longer listed first | **SURVIVED** |

The first is the one that matters: the fixtures *do* contain `missing_lines: 2`
and `missing_lines: 1` in `totals` (lines 300, 353), but the only test that
asserts the rendered "Lines Covered" row uses the all-zero fixture
(`"| Lines Covered | 10 / 10 |"`, line 273). Adding the assertion to a
non-zero-gap case closes it for free.

**Recommendation:** in the existing partial-coverage test, assert the summary
row (`"| Lines Covered | 8 / 10 |"`) alongside the gap table; add a two-file
partial fixture asserting both the sort order and that a 99.5% file is listed.

---

#### R4-L5. The fuzz suite did not grow with the Round-3 wave: 21 properties, none over the new untrusted-input layer that wave introduced

**Severity:** Low
**Files:** `tests/fuzz/` (21 properties, unchanged from Round 3),
untested by property: `simulator/protocol.py:_coerce_wire_number/_int/_string/_flag`,
`simulator/state.py:Schedule.from_dict`, `door.py:Schedule.from_dict`,
`sanitize.py:sanitize_text`

The Round-3 wave added an entire validation layer for hostile wire input —
five `_coerce_*` families, a `WireValueError` envelope, a rewritten schedule
parser — and the property suite is byte-identical at 21 properties. The
existing 21 are good (I re-reviewed them: framing round-trip under arbitrary
chunking, buffer caps, non-ASCII resilience, compress
idempotence/coverage/shape/non-mutation, diff apply-equals-target, week
converter inverses, `make_bool` totality, tz parse totality — no tautologies).
They just do not cover any of the new surface.

This is not theoretical. The wave itself *fixed* a totality bug of exactly this
class — `_coerce_schedule_int` letting `OverflowError` escape from
`int(float("inf"))` — and a totality property would have found it without a
human noticing. I wrote the missing properties as a throwaway probe and they
found **R4-M1 in under sixty seconds**. Round 3 noted this gap for
`from_dict` alone and waived it; the surface is now three times larger and it
found a real bug, so it is worth writing down.

Worth stating plainly: the existing simulator-side validators are *clean* —
14,000 examples, zero unexpected exceptions from `_coerce_wire_*`,
`simulator.state.Schedule.from_dict`, `validate_schedule_entry`,
`compress_schedule` or `sanitize_text`. The properties would be regression
guards, not bug-finders, everywhere except `door.py`.

**Recommendation:** four properties, all the same shape as the existing
`test_validate_never_raises_and_returns_bool`:

```python
@given(json_value)
def test_wire_coercers_only_ever_raise_wire_value_error(v):
    for fn, args in ((_coerce_wire_number, (v, "f", 0, 90000)), ...):
        with contextlib.suppress(WireValueError):
            fn(*args)                      # anything else propagates and fails

@given(schedule_payloads())
def test_schedule_from_dict_only_ever_raises_value_error(data): ...   # both classes

@given(st.text())
def test_sanitize_leaves_no_control_character_and_is_idempotent(text):
    out = sanitize_text(text)
    assert not _CONTROL_CHAR_RE.search(out)
    assert sanitize_text(out) == out
```

---

#### R4-L6. Eleven `test_cli.py` tests call the real `cli.main()` without patching `run_simulator`; when the guard they test regresses they start a real simulator on the default port and only fail via the 60 s timeout

**Severity:** Low
**File:** `tests/simulator/test_cli.py:528, 577, 594, 602, 609, 617, 633, 648, 659, 676, 684, 696`
(the tests that do *not* go through `_run_main`, `test_cli.py:499-511`)

`_run_main` correctly monkeypatches `cli.run_simulator` before calling
`cli.main()`. Eleven tests call `cli.main()` directly, relying on an
`argparse` `parser.error(...)` or `--list-scripts` to exit first. That is true
today — and it is precisely what those tests assert, so it is exactly what a
regression breaks.

Observed, not theorised. My `cl7` mutation (removing the new `--scripts-dir`
directory check) made `test_missing_scripts_dir_is_rejected` fall through into
a real `asyncio.run(run_simulator(...))`:

```
1 failed in 20.44s     # 20 s = the pytest-timeout cap I set, not an assertion
...
WARNING  powerpetdoor.simulator.cli: No *.yaml/*.yml scripts found in .../nope
WARNING  powerpetdoor.simulator.cli: stdin not available, running without interactive input
Simulator started on ...            # a real listening server, in a unit test
```

Under the real 60 s cap and `-n auto`, a regression in the arg-parsing guards
means up to eleven workers each burn 60 s *and* each bind the default simulator
port concurrently on a shared runner. This is the R3-M4 class again — now
backstopped, so the build does go red, but slowly and with a confusing cause.

**Recommendation:** give the exit-before-run tests a fixture that makes running
an assertion failure rather than a hang — one line, and it turns a 60 s timeout
into an instant, self-describing failure:

```python
@pytest.fixture
def never_runs(monkeypatch):
    async def _boom(**kwargs):
        raise AssertionError("cli.main() reached run_simulator; it should have exited first")
    monkeypatch.setattr(cli, "run_simulator", _boom)
```

---

### Trivial

---

#### R4-T1. `_collect_pragma_exclusions` still scans only `src/powerpetdoor`, but `scripts/` is now inside the coverage gate

**Severity:** Trivial
**Files:** `scripts/generate_gaps_report.py:255`, `pyproject.toml:96`

R3-M5's fix added `scripts` to `coverage.run.source` and gave it a
`"Build Scripts"` bucket in `_categorize` (correctly — my `g4` mutation was
caught). But `source_dir = Path("src/powerpetdoor")` is still hard-coded, so a
`# pragma: no cover` added to `scripts/generate_gaps_report.py` would be
honoured by the coverage gate and **invisible** in the exclusions section of the
report. Today there are none, so nothing is wrong; it is one iteration over two
roots away from staying correct.

---

#### R4-T2. `_track_task` vs `ensure_future` at `start()` and `_schedule_reconnect()` is unpinned at both call sites

**Severity:** Trivial
**Files:** `src/powerpetdoor/client.py:1025`, `client.py:1222`

R3-L1 changed both from `ensure_future` to `_track_task` so an exception
escaping `connect()`/`reconnect()` is logged immediately rather than at GC, and
so `disconnect()` cancels an in-flight attempt. The *mechanism* is pinned
(`test_failing_task_is_logged_immediately`, `test_disconnect_cancels_in_flight_processing`),
but reverting either call site survives `test_client.py`. Low value on its own —
the observable difference needs `connect()` to raise unexpectedly, which the
new `except (OSError, TimeoutError, ValueError, OverflowError)` funnel is
designed to prevent (and *that* funnel is well tested — `x3` was caught by two
named tests including the `UnicodeEncodeError` case). Noting it only so the
inventory is complete.

---

#### R4-T3. `transport.abort()` in the shutdown-decline path is not pinned, unlike its sibling

**Severity:** Trivial
**File:** `src/powerpetdoor/client.py` (`_adopt_transport`, shutdown branch)

`test_connection_made_rejects_a_second_transport` asserts
`intruder.aborted is True`, so `abort()` → `close()` on the second-transport
path is **caught**. The shutdown path's `transport.abort()` → `close()`
**survived** — `test_shutdown_during_connect_leaves_no_live_socket` waits for
the socket to close, which `close()` also achieves. Arguably an equivalent
mutant (nothing is buffered on a just-made connection), but the sibling test
shows the one-line assertion is cheap and the intent is explicit.

---

## Round 3 Fix Verification

Every Round-3 finding re-checked against the source and, where behavioural,
**by re-running my exact Round-3 mutation**.

| # | Round-3 finding | Status |
|---|---|---|
| R3-M1 | `clear`'s terminal behaviour unasserted; both `isatty()` directions survived | **Fixed and re-proved.** `if out.isatty():` → `if True:` now fails `test_clear_writes_nothing_off_a_terminal`; → `if False:` fails `test_clear_writes_the_ansi_sequence_on_a_terminal` **and** `test_clear_falls_back_to_sys_stdout`. The tests now assert against the buffer they installed themselves, and the `sys.__stdout__ is None` fallback got its own case. The duplicate `test_clear_returns_empty_message` is gone. |
| R3-M2 | `simulator.Schedule.to_dict()` wire-int coercion bool-blind | **Fixed and re-proved.** `[1 if day else 0 ...]` → `list(...)` now fails `test_to_dict_writes_wire_ints_for_bool_days` (`type(day) is int` + a `json.dumps` assertion). |
| R3-M3 | `is_wait_run` — three parsing mutations all survived | **Fixed and re-proved.** All three now fail named parametrised cases: arity → `[run wait-False]`; alias set → `[file foo wait-True]`, `[R foo wait-True]`, `[FILE foo wait-True]`; case → `[run foo WAIT-True]`, `[run foo Wait-True]`. The 20-case table is exactly the right shape. |
| R3-M4 | Serialization regression **hung the suite**; no CI job timeout | **Fixed and re-proved.** `if not queue_if_busy and self.busy:` → `if False:` now **fails in 3.7 s** with 2 named failures instead of hanging forever. `asyncio.timeout(2.0)` bounds both offending awaits; `pytest-timeout>=2.3.0` with `timeout = 60` is the backstop and I confirmed it fires correctly under `-n auto` for both an async deadlock and a blocked thread; all five CI jobs carry `timeout-minutes: 30`. |
| R3-M5 | `generate_gaps_report.py` untested and outside coverage | **Fixed.** 36 tests, `scripts` added to `coverage.run.source` with a `[tool.coverage.paths]` entry, and a real `ValueError` bug fixed (the `#pragma:`-without-a-space slice — mutating it back is caught by `test_pragma_without_a_space_does_not_crash`). 17 of my 20 mutations against it are caught. Residual: R4-L2 (paren truncation) and R4-L4 (three weaker survivors). |
| R3-L1 | `test_client_integration.py` weakest file; count-only assertions | **Fixed and re-proved.** Suppressing the `DOOR_SLOWING` broadcast now fails 4 tests in that file (`test_open_door`, `test_door_status_callback`, `test_multiple_listeners`, …); suppressing `CLOSING_MID_OPEN` fails `test_close_door`. `CallbackTracker.get_values()`/`wait_for_sequence()` are in place and the subsumed `test_sensor_callback` is gone. The remaining `assert len(calls) > 0` lines are harmless guards immediately followed by exact `calls[0]` assertions, not overbroad acceptance. |
| R3-L2 | `TestMultiClient` / `test_close_door_sends_status_updates` unhardened | **Fixed and re-proved.** Suppressing `DOOR_SLOWING` fails 5 tests in `test_integration.py` including both multi-client tests; suppressing `CLOSING_MID_OPEN` fails `test_close_door_sends_status_updates` and `test_full_door_cycle_messages`. The `get_status_sequence()` `CMD == DOOR_STATUS` filter is itself pinned — replacing it with a field match is caught. |
| R3-L3 | Two `MessageCapture` helpers, one re-implementing the C5 framing bug | **Fixed** structurally: `tests/simulator/wire.py::WireCapture` owns framing via the production `extract_frames`, and both classes subclass it. The brace scanner is gone. Residual: the helper itself has no unit test — R4-L3. |
| R3-L4 | `OS Independent` classifier vs Linux-only CI and `add_reader` | **Fixed as scoped.** Classifier is now `POSIX :: Linux` + `MacOS`, with an in-file comment naming the exact reason (`loop.add_reader` on `ProactorEventLoop`), and a README platform note. Declining the Windows fallback because it cannot be tested on the available runner is the right call under this persona's rules — an untestable fallback is worse than an honest exclusion. |
| R3-L5 | Two wall-clock-margin tests at 2.5×–3× | **Fixed.** Both widened to 10× margins. They remain the only wall-clock-margin tests in the suite; everything else is a negative-observation window or an `asyncio.timeout` upper bound. |
| R3-T1 | `docs/simulator.md` absent from `DOC_FILES` | **Fixed.** Added, and I confirmed the list is now complete: `docs/operation.md` and `docs/protocol.md` contain zero ```` ```python ```` blocks, so there is nothing else to execute. |
| R3-T2 | `TestCliModeRestore` mutated the global registry inline | **Fixed.** `test_commands.py:747` now has an autouse guard, matching the three that already existed elsewhere. |
| R3-T3 | Fuzz job pinned to `--hypothesis-seed=0` forever | **Fixed.** A weekly `schedule: cron "17 4 * * 1"` runs the fuzz job unseeded; push/PR stays seeded for reproducibility, with a comment explaining the split. Leaving `max_examples` alone is correct — it is set per-test via `@settings`, so a profile would be a no-op. |

**The fix wave's own mutation testing, independently assessed.** It holds up
well. The areas the wave built and then self-mutated are the strongest in the
suite: `protocol.py`'s wire validation is **14/14** (bool-is-not-a-number,
`isfinite`, both range bounds, string type, both length bounds, `make_bool`
flags, the `WireValueError` reason passthrough, notification atomicity, and all
three constants); `state.py`'s schedule validator **7/7** (including the
`OverflowError` catch and both `TypeError` guards in `get_tzinfo`); the
`_ControlLogHandler` feedback-loop and reaping guards **5/5**; ctl's `LOG:`
streaming **4/4** (including "plain `run` does *not* stream" and the
sanitization of streamed lines); the engine's intent-based deferral **3/3**;
`cli.main`'s new argument validation **5/5**. Where it missed was uniformly
*outside* the diff it was reviewing: the twin implementation it did not change
(R4-M1, R4-M3), the caller of the thing it changed (R4-M4), the helper that is
only ever called indirectly (R4-M2), and its own test infrastructure (R4-L3).

---

## Areas Reviewed With No Findings

- **Pragma audit.** Exactly 5 sites in `src/`, unchanged, each with an inline
  justification I re-derived from the code. `ctl.py:568`'s `except EOFError`
  remains genuinely unreachable (both prompt paths signal EOF by returning
  `None`); `ctl.py:350` and `:619` are documented races; `cli.py:99` and `:636`
  are true post-condition invariants. None is hiding an untested branch. Zero
  pragmas in `scripts/`.
- **`tests/TESTING_GAPS.md` accuracy.** Metrics match my own coverage run
  exactly (6,062 lines / 2,190 branches / 100.00% / 0 missing). I re-derived
  every category count from the source tree by hand: Build Scripts 1, Core
  Library 7, Simulator 5, Simulator CLI 3, Simulator Commands 12 = 28 files =
  31 `.py` minus 3 `__init__.py` minus 1 `__main__.py` plus 1 script. Correct.
  The exclusion list matches `pyproject.toml`. Only the two truncated pragma
  reasons are wrong (R4-L2).
- **pytest-timeout under xdist.** Empirically verified, not assumed. Both a
  deadlocked `asyncio.Event().wait()` and a GIL-releasing
  `threading.Event().wait()` fail at the cap with a clear
  `Failed: Timeout (>Ns) from pytest-timeout` and a thread stack dump, and
  xdist reports them as ordinary failures rather than losing the worker. 60 s
  against a 17.6 s whole-suite runtime can only fire on a genuine hang.
- **`filterwarnings = ["error"]` and session hygiene.** The
  `_managed_main_thread_event_loop` session fixture is a real fix for a real
  pytest-asyncio artefact (an unclosed implicit loop GC'd mid-session,
  attributed to an arbitrary test), correctly documented, and inert on 3.14+.
  The autouse `_reset_extra_scripts_dir` finalizer runs even on failure.
- **Global-state guards.** Four now: `registry_guard` in `test_commands_base.py`
  and `test_commands_handler.py`, `_cli_registry_guard` in `test_cli.py`, and
  the new autouse guard in `test_commands.py` (R3-T2). Every file that mutates
  the global command registry has one. `set_interactive_mode` is per-handler,
  not global.
- **Ephemeral-port hygiene.** `unused_tcp_port` appears nowhere; the
  `refused_port` fixture holds a bound-not-listening socket for the test's
  whole lifetime, which both reserves the number against another xdist worker's
  `bind(0)` and yields ECONNREFUSED. The tests using it still assert the exact
  `Connection refused to 127.0.0.1:{port}` text, which is what proves the
  fixture works.
- **Skips and xfails.** Ten `skipif` markers, zero `xfail`, zero unconditional
  skips. All ten are `YAML_AVAILABLE` / `prompt_toolkit` belt-and-braces, and
  both have the mandated dependency-absent equivalents. Nothing is skipped in
  the default environment (1926 passed, 0 skipped).
- **Fake / can't-fail / read-back tests.** I enumerated all 1,679 test functions
  by AST. Zero `assert True`, zero tautologies, zero `assert x in (a, b)`
  outside `tests/fuzz/` (where "returns a bool, never raises" is the property
  under test). 54 functions have no bare `assert`: 34 are `pytest.raises`
  blocks, 6 are mock `assert_called_once_with`, and the remaining 14 are either
  documented no-raise contracts (`process_message("not a dict")`, `_send`
  without a transport, double `connection_lost`, `disconnect` before `connect`,
  doc-import `exec`) or waits bounded by `asyncio.wait_for` / `asyncio.timeout`
  / `recorder.wait_for`, where the bound is the assertion. No read-backs.
- **Sleep-as-synchronization.** 8 non-`sleep(0)` sleeps in the entire suite.
  Every one is either a deliberate scheduling yield with an explanatory comment
  ("let the task enter its poll wait"), or the *stimulus* under test (a fake
  daemon's 0.1 s inter-write gap, a 0.5 s script duration), never a "wait and
  hope the other thing finished". `asyncio.sleep(3600)` / `sleep(60)` are
  deliberate never-finishing handlers for cancellation tests.
- **Mock-testing-the-mock.** Only 30 `MagicMock()`/`AsyncMock()` constructions
  across 10 files, and they are all genuine collaborators being observed
  (`stop_callback`, `on_disconnect`, transports) rather than the subject under
  test. Integration coverage is real: the simulator is a real server on a real
  socket in `test_integration.py`, `test_client_integration.py`, and the seven
  `tests/simulator/scripts/` files.
- **Exact-message negative testing.** Consistently exact across the new files —
  the `WireValueError` reasons (`"holdTime must be a finite number, got inf"`),
  the schedule rejections (`"Schedule is missing required field 'in_start_time'"`),
  the argparse errors (`"error: --scripts-dir {path}: not a directory"`), the
  `stop`/`status` operator strings (`'  Script: running "Slow Script" (1 queued)"`),
  the full `status` render. Mutating any of these message strings is caught.
- **Round-3's new `LOG:` streaming and script status/stop.** 9 mutations, 8
  caught by named tests; the ninth (`current_script if busy else None` →
  `current_script`) is provably equivalent — `busy` is `self._lock.locked()`,
  `current_script` is assigned inside the lock with no await before it and
  cleared in a `finally` inside it, so the two are never out of step.
- **CI structure.** Four-Python matrix (3.11–3.14, matching CLAUDE.md's table
  and `pyproject.toml`), `timeout-minutes: 30` on all five jobs, a concurrency
  group with `cancel-in-progress` for the shared act_runner, `--cov-fail-under=0`
  per matrix entry with the real 100% gate applied once after `coverage
  combine`, and both suite invocations exercised separately (unit job excludes
  fuzz, fuzz job runs only fuzz) so neither can lean on the other. Build
  requirements are pinned exactly with a documented supply-chain rationale.
- **Repo hygiene.** I modified no repository file. All 111 mutations and the
  Hypothesis probe ran on copies under `/tmp` with `PYTHONPATH` forcing the
  mutated tree (the editable-install shadowing trap is real — see the Summary).
  `git status` for `src/`, `tests/` and `scripts/` is clean.
