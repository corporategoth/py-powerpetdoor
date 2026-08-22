# Test Fanatic Analysis — Round 3

## Summary

Verified by execution, not by reading: `uv run pytest --ignore=tests/fuzz --cov -q`
(the exact CI unit-matrix invocation) → **1738 passed, 100.00% lines and
branches**, gate satisfied. `uv run pytest -q` (full, with fuzz) → **1759 passed
in 25.9 s**. `ruff check`, `ruff format --check`, `mypy src` all clean. Every
Round-2 finding is fixed and I re-proved each one. The five `# pragma` sites are
unchanged, individually justified, and `tests/TESTING_GAPS.md` matches the
actual coverage run and the actual source exactly (5,712/5,712 lines,
2,074/2,074 branches, 5 pragmas across 2 files — I diffed all of it).

Because coverage is saturated, coverage tells you nothing more. So I ran **39
real mutations** (plus one no-op control mutant to validate the harness) against
the code Round 2 changed, executing the relevant test files for each. Results:

| Outcome | Count |
|---|---|
| Caught (a specific test failed) | 30 |
| Survived — meaningful | 6 |
| Survived — equivalent/harmless | 2 |
| Detected but **hangs the suite instead of failing** | 1 |

The 30 catches are genuinely specific: the deferred-sequence mechanism,
coalescing, `stop()`/`cancel_nowait()` teardown, both `connect()` guards, the
second-transport rejection, msgID type tolerance, per-frame lenient decoding on
both sides, schedule range/shape/bitmask/default validation, `SET_SCHEDULE_LIST`
atomicity, the ctl silence-timeout state machine, the scripts-dir registration,
and the fractional `hold_time` all fail a named assertion when broken. That is a
strong suite.

The six meaningful survivors and the hang are the findings below. None is
catastrophic; all are real, all are reproduced by execution.

Finding counts: **Critical: 0, High: 0, Medium: 5, Low: 5, Trivial: 3** (13 total).

---

## Findings

### Medium

---

#### R3-M1. `clear`'s entire terminal behavior is unasserted — the "off a terminal" test cannot fail, and both directions of the new `isatty()` guard survive mutation

**Severity:** Medium
**Files:** `tests/simulator/test_commands.py:479` (`test_clear_off_a_terminal_emits_no_escape_sequence`),
`tests/simulator/test_commands.py:470` (`test_clear_works_in_interactive_mode`),
`tests/simulator/test_commands.py:1043` (`test_clear_returns_empty_message`),
`src/powerpetdoor/simulator/commands/control.py:96`

Round 2's front-end fix added `if out.isatty():` around the ANSI clear sequence,
where `out = sys.__stdout__ if sys.__stdout__ else sys.stdout`. Two mutations,
both **SURVIVED** the full `test_commands.py` + `test_ctl.py` run:

- `if out.isatty():` → `if True:` (always emits ANSI, even into a pipe — the
  exact bug the fix was for) → 145 passed.
- `if out.isatty():` → `if False:` (never emits ANSI, `clear` does nothing at
  all) → 145 passed.

Root cause of the first: `capsys` patches `sys.stdout`, **not** `sys.__stdout__`,
so `assert capsys.readouterr().out == ""` is vacuous — it is true whether or not
the escape sequence was written. I proved this in isolation with a standalone
test that writes `\033[2J\033[H` to `sys.__stdout__` and still sees
`capsys.readouterr().out == ""` (passes). The test can never fail.

Root cause of the second: `test_clear_works_in_interactive_mode` installs a
`_TtyStdout()` (a `StringIO` that claims `isatty()`) as `sys.__stdout__` — it
owns the buffer — but only asserts `result.success is True` and
`result.message == ""`. It never looks at what was written to the buffer it
installed.

Additionally `test_clear_returns_empty_message` (line 1043) is a byte-for-byte
duplicate of `test_clear_works_in_interactive_mode`: same `set_interactive_mode(True)`,
same `_TtyStdout` monkeypatch, same two assertions. Redundancy is the one valid
deletion reason.

**Recommendation:**
```python
async def test_clear_writes_the_ansi_sequence_on_a_terminal(self, command_handler, monkeypatch):
    command_handler.set_interactive_mode(True)
    tty = _TtyStdout()
    monkeypatch.setattr(sys, "__stdout__", tty)
    assert (await command_handler.execute("clear")).success is True
    assert tty.getvalue() == "\033[2J\033[H"

async def test_clear_writes_nothing_off_a_terminal(self, command_handler, monkeypatch):
    command_handler.set_interactive_mode(True)
    pipe = io.StringIO()          # isatty() -> False
    monkeypatch.setattr(sys, "__stdout__", pipe)
    assert (await command_handler.execute("clear")).success is True
    assert pipe.getvalue() == ""
```
and delete `test_clear_returns_empty_message`. (Note the contrast: `cli.py`'s
sibling guard `if self._enabled and sys.stdout.isatty():` *is* correctly tested —
mutation M35 was caught — because it writes to `sys.stdout`, which `capsys`
really does capture.)

---

#### R3-M2. The simulator's `Schedule.to_dict()` wire-int coercion is unasserted — `True == 1` makes the only test bool-blind, and the whole 1759-test suite passes with the fix reverted

**Severity:** Medium
**Files:** `tests/simulator/test_state.py:208` (`test_to_dict_writes_wire_ints_for_bool_days`),
`src/powerpetdoor/simulator/state.py:176`

Round 2 changed the simulator's `Schedule.to_dict()` from
`self.days_of_week.copy()` to `[1 if day else 0 for day in self.days_of_week]`
precisely so booleans in memory serialize as protocol `1`/`0` on the wire. The
only test is:

```python
schedule = Schedule(index=0, inside=True, days_of_week=[True, False] * 3 + [True])
assert schedule.to_dict()["daysOfWeek"] == [1, 0, 1, 0, 1, 0, 1]
```

In Python `True == 1` and `False == 0`, so this assertion passes identically for
`[True, False, True, ...]`. Verified by execution: I reverted the line to
`list(self.days_of_week)` and ran the **entire suite** — `1759 passed in 25.92s`.
Nothing anywhere catches it. The regression is a real wire-format change:
`json.dumps` would emit `"daysOfWeek": [true, false, ...]` where `docs/protocol.md`
and the real device use `1`/`0`.

The library-side `Schedule` (a different class, `powerpetdoor/door.py`) *is*
correctly pinned at `tests/test_door.py:271` with
`all(isinstance(day, int) and not isinstance(day, bool) for day in d["daysOfWeek"])`.
The simulator-side twin never got the same treatment.

**Recommendation:** copy the library-side assertion into
`tests/simulator/test_state.py`:
```python
days = schedule.to_dict()["daysOfWeek"]
assert days == [1, 0, 1, 0, 1, 0, 1]
assert all(type(d) is int for d in days)   # True is not 1 on the wire
```
and consider asserting the serialized form (`json.dumps(...)` contains `[1, 0,`)
in one protocol-level test, since the wire is what actually matters.

---

#### R3-M3. `is_wait_run` — the helper that removes ctl's response deadline — has no direct tests; three separate mutations of its parsing all survive

**Severity:** Medium
**Files:** `src/powerpetdoor/simulator/commands/scripts.py:27` (`is_wait_run`),
tests: only indirect via `tests/simulator/test_ctl.py:167` and
`tests/simulator/test_ctl_interactive.py:318`

`is_wait_run` decides whether ctl waits **with no timeout at all**
(`sock.settimeout(None)` in one-shot mode; `await_response(None)` interactively).
Getting it wrong either hangs the operator indefinitely or times out mid-script.
It has four distinct behaviours — arity, alias set, command case, keyword case —
and every test that touches it uses the single literal string
`"run full_test_suite wait"`. Three mutations, each run against
`test_ctl.py` + `test_ctl_interactive.py` + `test_commands.py`, all **SURVIVED**:

| Mutation | Effect if it shipped | Result |
|---|---|---|
| `len(parts) >= 3` → `>= 2` | `run wait` (a script *named* `wait`) is treated as a synchronous run and waits forever | 162 passed |
| `parts[0].lower() in (RUN_COMMAND, *RUN_ALIASES)` → `== RUN_COMMAND` | `r foo wait` / `file foo wait` time out at `--timeout` mid-script | 162 passed |
| `parts[-1].lower() == RUN_WAIT_KEYWORD` → `parts[-1] == RUN_WAIT_KEYWORD` | `run foo WAIT` is accepted by the daemon (`parse_arg` choice matching *is* case-insensitive, `commands/base.py:180-185`) but ctl applies the short deadline | 73 passed |

100% branch coverage does not help here: the whole function is one boolean
expression, so short-circuit paths are not separate coverage arcs.

**Recommendation:** a small table-driven unit test next to the other
`commands/scripts.py` tests — it costs ten lines and closes all three:
```python
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("run foo wait", True), ("r foo wait", True), ("file foo wait", True),
        ("RUN foo WAIT", True), ("run foo/bar.yaml wait", True),
        ("run foo", False), ("run wait", False), ("wait", False),
        ("status wait", False), ("run foo wait extra", False), ("", False),
    ],
)
def test_is_wait_run(line, expected):
    assert is_wait_run(line) is expected
```

---

#### R3-M4. A plausible regression in the new script-serialization lock **hangs the suite forever** instead of failing — and no job in CI has a timeout

**Severity:** Medium
**Files:** `src/powerpetdoor/simulator/scripting.py:274`,
`tests/simulator/test_scripting.py:1067` (`test_wait_run_is_refused_while_another_script_runs`),
`tests/simulator/test_commands.py:877` (`test_run_wait_is_refused_while_another_script_runs`),
`pyproject.toml:68-72`, `.github/workflows/test.yml` (no `timeout-minutes` anywhere)

Verified by execution. Mutating the round-2 fast-fail guard
`if not queue_if_busy and self.busy:` → `if False:` makes
`TestScriptSerialization` **hang**: I ran it under `timeout 120` and it produced
no output and never completed. The reason is structural:

```python
first = asyncio.ensure_future(runner.run(self._script("First")))   # blocks on `release`
await entered.wait()
with pytest.raises(ScriptError, match="Another script is already running: First"):
    await runner.run(self._script("Second"), queue_if_busy=False)  # <- unbounded
release.set()                                                      # never reached
```
Without the guard the second `run` waits on `self._lock`, which is only released
by `first`, which is only released by `release.set()` on the line after. Deadlock.

The suite has no `pytest-timeout` (confirmed: not installed, not in `[dev]`, no
`timeout` setting in `pyproject.toml`) and **no `timeout-minutes` on any CI job**
(confirmed by grep across both workflow files), so the default 6-hour job timeout
applies — on a host the workflow itself documents as "a single shared act_runner".
A regression here converts a 2-second red build into a 6-hour blocked runner with
zero diagnostic output.

This is the same residual risk Round 2's M8 accepted ("every wait is bounded by
`asyncio.timeout`/`wait_for`; residual risk accepted") — except it is no longer
theoretical: the round-2 fix wave introduced two tests that violate that
invariant. A related, milder instance: mutating `_track_task`'s `transient`
default made `tests/test_client.py` take **61 s** to fail (it does fail), because
`test_disconnect_cancels_in_flight_processing` parks on `await asyncio.sleep(60)`.

**Recommendation:** two independent fixes, both cheap:
1. Bound the assertion itself — `async with asyncio.timeout(2.0):` around the
   `pytest.raises` block (or `await asyncio.wait_for(runner.run(...), 2.0)`), so
   the regression fails in 2 s with a clear `TimeoutError`. Same for the
   `test_commands.py` twin.
2. Add `pytest-timeout` to `[dev]` with `timeout = 60` (and/or
   `timeout-minutes: 15` on the CI jobs) as the backstop. The whole suite is
   26 s; a 60 s per-test cap can only ever fire on a genuine hang.

---

#### R3-M5. `scripts/generate_gaps_report.py` — 321 lines of production-shaped Python, zero tests, excluded from coverage, and it generates the artifact the project uses to claim 100%

**Severity:** Medium
**Files:** `scripts/generate_gaps_report.py` (untested),
`pyproject.toml:74-81` (`source = ["src/powerpetdoor"]`),
`.github/workflows/test.yml:159-171`

CI runs this script on every push and **auto-commits its output** to
`tests/TESTING_GAPS.md`. It is the project's self-reported view of its own
testing gaps. It is also the only Python in the repository with no tests at all,
and `coverage.run.source` scopes coverage to `src/powerpetdoor`, so its 0% never
shows up anywhere.

It is not glue — it contains real logic that can silently misreport:

- `_collect_pragma_exclusions` (lines 25-92): a regex over source files with
  consecutive-line grouping, reason inheritance from the first line of a group,
  and `code[: code.index("# pragma:")]` slicing. A pragma written as
  `#pragma: no cover` (no space) parses in the regex but raises `ValueError` on
  the `.index("# pragma:")` slice. A pragma on a continuation line of a
  multi-line statement reports a misleading `code` field.
- `_group_lines` (95-108): range-collapsing arithmetic.
- `_categorize` (111-121): a hardcoded three-way path split that silently
  mis-buckets any new module (e.g. a new `simulator/*.py` lands in "Simulator",
  a new top-level module lands in "Core Library" by fallthrough).
- `main()` (124+): the `no coverage.json` error path writes an error document
  *over* `tests/TESTING_GAPS.md` and returns 1 — but the CI step that runs it is
  not `continue-on-error`, so that path is at least loud. It is still untested.

Round 2's High finding was, at root, "TESTING_GAPS.md said 100% when the gate
would have failed." The generator of that file is the one thing nobody checks.

**Recommendation:** add `tests/test_gaps_report.py` covering the three pure
helpers (they take plain inputs and return plain outputs — trivial to test),
plus one end-to-end test that feeds a synthetic `coverage.json` and asserts the
rendered markdown. Add `scripts` to `coverage.run.source` (or add a second
`source` entry) so the file participates in the 100% gate like everything else.

---

### Low

---

#### R3-L1. `test_client_integration.py` is the weakest file in the suite — one overbroad multi-outcome assertion and five tests whose only assertion is a count

**Severity:** Low
**File:** `tests/simulator/test_client_integration.py:267-273, 363-364, 376-377, 481-482, 611-612, 281-282`

This file was essentially untouched by the Round-2 wave (its only change was
stripping `asyncio` marks) and it still carries the Round-1 H2 / Round-2 L2
pattern that was fixed everywhere else.

The clearest violation, `test_open_door` (267-273):
```python
calls = tracker.get_calls("door_status")
assert len(calls) > 0
statuses = [c[0] for c in calls]
assert any(s in (DOOR_STATE_RISING, DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP) for s in statuses)
```
Three acceptable outcomes for a fully deterministic sequence. The first
broadcast after `CMD_OPEN` is always `DOOR_RISING` (`engine._replace_sequence`
calls `_set_status(start_state)` synchronously), so the single correct assertion
is `statuses[0] == DOOR_STATE_RISING`.

Tests whose *only* assertion is a nonzero count, with no assertion on the value:
`test_door_status_callback` (364), `test_sensor_callback` (377),
`test_multiple_listeners` (481-482), `test_sensor_trigger_full_cycle`
(612: `assert len(calls) >= 2  # At least RISING and HOLDING`).

Verified by execution: I suppressed only the `DOOR_SLOWING` broadcast in
`engine._set_status` and ran both integration files — the three round-2-hardened
tests in `test_integration.py` failed; **every test in
`test_client_integration.py` passed**. A simulator that skips a state entirely
is invisible to this file.

Two smaller items in the same file: `test_sensor_callback` (366-377) is strictly
subsumed by `test_sensor_callback_receives_field_and_value` (379-394) — identical
setup, weaker assertions; and `test_close_door` (275-289) builds a tracker and
registers a `door_status_update` listener at lines 281-282 that is never read.

**Recommendation:** the file already has the right tool available
(`simulator.wait_for_status`). Replace the count assertions with exact first-status
or exact-sequence assertions, delete the subsumed `test_sensor_callback`, and drop
the dead tracker in `test_close_door`.

---

#### R3-L2. Round-2 L2 was applied to only half of `test_integration.py`; `test_close_door_sends_status_updates` asserts nothing about status updates

**Severity:** Low
**File:** `tests/simulator/test_integration.py:247-268, 295-332, 334-375`

`TestDoorOperationMessages` was correctly hardened to exact sequences via
`receive_status_sequence(FULL_CYCLE)` — verified, it catches a dropped state.
The rest of the file was not:

- `test_close_door_sends_status_updates` (247-268) — despite the name, its only
  assertions are `close_response is not None` and `success == "true"`. The
  `receive_until(... == DOOR_STATE_CLOSED)` above them returns on timeout without
  failing. **Verified by execution:** with *all* door-status broadcasts suppressed
  in the engine, this test still passes. It cannot fail for the thing it is named
  after (and in the broken case it silently burns its full 3 s timeout).
- `TestMultiClient` (295-375) — `receive_until` predicates are
  `RISING or HOLDING` and `RISING or HOLDING or KEEPUP`, and the assertions are
  `assert len(cap1.find_status_updates()) > 0`. Note also that
  `find_status_updates()` matches *any* message carrying `doorStatus`, including
  command responses, so in `test_command_from_one_client_broadcasts` the
  cap1-side check would be satisfied by the `CMD_OPEN` response alone.

**Recommendation:** use the same `receive_status_sequence(OPENING_SEQUENCE)`
helper for both multi-client captures and for the close test (expected
`[CLOSING_TOP_OPEN, CLOSING_MID_OPEN, CLOSED]` from KEEPUP), and switch the
multi-client assertions to `get_status_sequence()` (which correctly filters on
`CMD == DOOR_STATUS`) rather than `find_status_updates()`.

---

#### R3-L3. Two `MessageCapture` helpers; the one in `test_integration.py` re-implements framing with the exact algorithm Round 1 removed from production as a bug

**Severity:** Low
**Files:** `tests/simulator/test_integration.py:78-185` vs
`tests/simulator/scripts/conftest.py:50-129`

`tests/simulator/scripts/conftest.py` gets this right — its `_listen_loop` uses
the production `extract_frames` scanner. `tests/simulator/test_integration.py`
hand-rolls a brace-depth counter (`_parse_messages`, lines 126-150) that is not
string-aware:

```python
for i, c in enumerate(data[pos:], pos):
    if c == "{": depth += 1
    elif c == "}": depth -= 1
```

This is precisely the C5 defect Round 1 found in `client.find_end` and fixed with
the string-aware `framing.extract_frames`. Any simulator payload with a brace
inside a JSON string value would be mis-framed here — and the helper swallows
`json.JSONDecodeError` silently (line 147-148), so the message would just
disappear and the surviving `> 0` assertions (R3-L2) would not notice. It also
duplicates ~70 lines of an existing helper, against CLAUDE.md's "two
implementations = refactor" rule.

**Recommendation:** delete `_parse_messages` and reuse `extract_frames`, exactly
as `scripts/conftest.py` does; better still, hoist one `MessageCapture` into a
shared conftest and delete the second class.

---

#### R3-L4. CI is Linux-only while the package declares OS-independence and the terminal front end uses a Linux/macOS-only asyncio API

**Severity:** Low
**Files:** `.github/workflows/test.yml` (`runs-on: ubuntu-latest` × 5),
`.github/workflows/release.yml` (× 2), `pyproject.toml:23`
(`Operating System :: OS Independent`), `src/powerpetdoor/simulator/ctl.py:342`,
`src/powerpetdoor/simulator/cli.py:468`

Both fallback prompt paths call `loop.add_reader(fd, ...)` on a stdin file
descriptor. That is unsupported on Windows' `ProactorEventLoop` (raises
`NotImplementedError`) — i.e. `ppd-simulator` and `ppd-simulator-ctl` would fail
at the prompt on the platform the metadata advertises. Several test fixtures are
equally POSIX-only (`os.pipe` + `os.fdopen` stdin replacement in
`test_ctl_interactive.py:150-156` and `test_ctl.py`).

Nothing in CI would ever notice: the Python matrix is 3.11–3.14 but the OS matrix
is a single `ubuntu-latest`. This is the one axis of the persona's mandate
("should function identically across these various platforms") with no coverage
at all, and neither Round 1 nor Round 2 raised it.

**Recommendation:** either (a) add `os: [ubuntu-latest, macos-latest, windows-latest]`
to the `unit-tests` matrix for at least the reference Python — it will
immediately tell you the truth — or (b) if Windows is explicitly out of scope,
drop the `OS Independent` classifier, state the supported platforms in the
README, and add a `sys.platform`-guarded fallback (a reader thread) with a test
that exercises it. Silence on this axis is the only unacceptable option.

---

#### R3-L5. The two new "streaming keeps the connection alive" tests are the suite's only wall-clock-margin tests, at 2.5×–3×

**Severity:** Low
**Files:** `tests/simulator/test_ctl_interactive.py:345-373`
(`test_streaming_logs_extend_the_response_deadline`),
`tests/simulator/test_ctl.py:203-226`
(`test_streaming_output_extends_the_response_deadline`)

Both prove "a gap shorter than the timeout does not trip it" by having a fake
daemon `await asyncio.sleep(0.1)` between writes while the client's silence
timeout is 0.25 s (interactive) / 0.3 s (one-shot). The margin is 2.5×/3× on an
event loop that, under `-n auto` on a loaded shared runner, can easily be
delayed 150 ms between a `sleep` expiring and the write landing. These are the
only two places left in the suite where a slow machine can produce a *false
failure* (every other timing test is a negative-observation window or an
`asyncio.timeout` upper bound, which fail-safe in the other direction).

Note the sibling tests are already safe: `test_wait_run_ignores_the_response_timeout`
sleeps 0.5 s against a 0.05 s timeout (10× the wrong way — a slow runner only
makes it *more* conclusive), and the pure timeout tests assert that a timeout
*did* happen.

**Recommendation:** widen the margin rather than the sleep — keep the 0.1 s gaps
and raise the client timeout to 1.0 s (10× margin, same total runtime), or make
the gaps event-driven: have the daemon wait on an `asyncio.Event` the test sets
after observing each `LOG:` line in the recorder, so the test controls the
interleaving instead of the clock.

---

### Trivial

---

#### R3-T1. `docs/simulator.md` has a `from powerpetdoor import ...` example that `test_exports.py` does not check

**Severity:** Trivial
**Files:** `tests/test_exports.py:25-29` (`DOC_FILES`), `docs/simulator.md:515`

`DOC_FILES` lists `README.md`, `docs/client.md`, `docs/door.md`.
`docs/simulator.md:515-516` contains
`from powerpetdoor import PowerPetDoorClient, COMMAND, CMD_OPEN` /
`from powerpetdoor.simulator import DoorSimulator` — exactly the kind of example
`TestDocImports` exists to keep executable, and it is the doc that changed most
in Round 2 (120 lines). Add it to `DOC_FILES`; the existing
`test_doc_contains_import_examples` guard means it cannot silently match nothing.

---

#### R3-T2. `TestCliModeRestore` mutates the global command registry inline with no guard fixture

**Severity:** Trivial
**File:** `tests/simulator/test_commands.py:720-750`

`test_exit_restored_after_cli_mode` calls `set_cli_mode(True)`, asserts, then
calls `set_cli_mode(False)` in the test body. If any assertion between them fails
(e.g. line 733), the global registry stays in CLI mode and every later test in
that xdist worker sees a corrupted registry — a cascade of misleading failures
around the one real one. The project already has three separate guards for
exactly this (`registry_guard` in `test_commands_base.py:64` and
`test_commands_handler.py:92`, `_cli_registry_guard` in `test_cli.py:44`);
`test_commands.py` just does not use one. Move the restore into a fixture
(`try/finally` at minimum).

---

#### R3-T3. The fuzz job is pinned to `--hypothesis-seed=0` forever, so it explores exactly one example set for the life of the project

**Severity:** Trivial
**File:** `.github/workflows/test.yml:106`

Round 2's M2 recommendation was implemented in full for the important half (fuzz
now runs on every push and PR — confirmed, the `if:` gate is gone and replaced by
an explanatory comment). The optional half was not: with a fixed seed, a
`.hypothesis` database that is never persisted between runs, and bounded
`max_examples`, CI replays the *same* 20-odd examples per property on every run.
The properties are good; their exploratory value is currently zero. A weekly
`schedule:` job without `--hypothesis-seed` and with `max_examples` raised (via
`--hypothesis-profile` or `HYPOTHESIS_MAX_EXAMPLES`) costs ~nothing and restores
it. Keep the seeded run on PRs for reproducibility.

---

## Round 2 Fix Verification

Every Round-2 finding re-checked against the source and, where behavioral,
against execution.

| # | Round-2 finding | Status |
|---|---|---|
| R2-H1 | Deterministic suite could not reach 100% without fuzz (`schedule.py` 113->117) | **Fixed and proven.** `pytest --ignore=tests/fuzz --cov -q` → 1738 passed, **100.00%** lines and branches, "Required test coverage of 100.0% reached". Two new deterministic tests (`test_schedule.py:102` valid-outside-only, `:123` inside+outside) reach the branch. The CI-parity command is now item 3 of CLAUDE.md's pre-commit checklist. |
| R2-M1 | `test_run_alias_r` accepted contradictory outcomes | **Fixed.** `test_commands.py` now asserts `result.success is False` and `result.message.startswith("Error: Unknown built-in script: nonexistent")`. I re-grepped the whole suite: the only remaining `assert x in (a, b)` forms are in `tests/fuzz/` (`make_bool(text) in (True, False, None)`, `validate_schedule_entry(entry) in (True, False)`), where "returns a bool, never raises" *is* the property under test. Correct. |
| R2-M2 | Fuzz job main-push-only | **Fixed.** The `if: github.event_name == 'push' && github.ref == 'refs/heads/main'` gate is gone; the job now runs on every push and PR, with a comment explaining why. (Seed still pinned — R3-T3.) |
| R2-L1 | Bind-then-close refused ports racy under xdist | **Fixed.** `refused_port` fixture added (`conftest.py:126-140`), holding a bound-not-listening socket for the test's lifetime. All five sites converted; `unused_tcp_port` no longer appears anywhere in `tests/` (verified by grep), and the affected tests still assert the exact `Connection refused to 127.0.0.1:{port}` text. |
| R2-L2 | Wire tests asserted "some status updates" | **Half fixed.** `TestDoorOperationMessages` now asserts exact sequences via the new `get_status_sequence()`/`receive_status_sequence()` (which correctly filters `CMD == DOOR_STATUS`) — I confirmed by mutation that these three tests catch a dropped `SLOWING` broadcast. `TestMultiClient` and `test_close_door_sends_status_updates` were left on the old pattern → **R3-L2**; the sibling file `test_client_integration.py` likewise → **R3-L1**. |
| R2-T1 | Duplicate `test_cycle_alias_y` | **Fixed.** Exactly one definition remains. |
| R2-T2 | ~380 redundant `@pytest.mark.asyncio` | **Fixed.** Zero `@pytest.mark.asyncio` decorators remain anywhere in `tests/`; `asyncio_mode = "auto"` carries all of them. |
| R2-T3 | Dataclass read-back `test_custom_values` | **Fixed properly.** Deleted, and replaced by `test_door.py:1010` `test_custom_cached_settings_drive_the_wire_payload`, which pins the same five fields through the merge that consumes them and asserts the exact wire dict. This is the right shape of replacement. |

Round-2 fix wave, independently assessed: of the ~76 tests added for new
behavior, the engine re-entrancy set (`TestReentrantStatusListeners`), the
connect-idempotence set, the msgID-robustness set, the per-frame decode tests on
both sides, the schedule-validation set (exact `FIELD_REASON` text + "nothing was
stored" + atomicity), and the ctl silence-timeout set are all mutation-resistant —
I broke each mechanism and a named assertion failed. The weak spots introduced by
the wave are `is_wait_run` (R3-M3), `clear` (R3-M1), `to_dict` days (R3-M2), and
the two unbounded-await tests (R3-M4).

## Areas Reviewed With No Findings

- **Pragma audit.** Exactly 5 sites, unchanged from Round 2, and
  `TESTING_GAPS.md`'s per-file inventory matches the source line-for-line
  (`cli.py` 97/598 `no branch`, `ctl.py` 337/555/606 `no cover`). I re-derived
  each justification from the code: `ctl.py:555`'s `except EOFError` is genuinely
  unreachable — `InteractiveSession.prompt_async` catches `EOFError` and returns
  `None` (`prompt_common.py:733-734`) and `_basic_readline` returns `None` on EOF,
  so both prompt paths signal EOF by value. (One could argue for deleting the dead
  handler rather than pragma-ing it; the pragma at least carries the reason.)
  `cli.py:598` and `cli.py:97` are true post-condition invariants. `ctl.py:337`
  and `:606` are defensive races. All five are legitimate.
- **`tests/TESTING_GAPS.md` accuracy.** Metrics (5,712 lines / 2,074 branches /
  100.00%) match my own coverage run exactly; the four category buckets match the
  actual file counts (Core 6, Simulator 5, Simulator CLI 3, Simulator Commands
  12); the exclusion list matches `pyproject.toml`; the pragma table matches the
  source. It reflects reality. (The *generator* is untested — R3-M5.)
- **Engine deferred-sequence mechanism.** Five mutations, five catches:
  no-defer (2 failures), no-coalesce, `stop()` not dropping the deferred restart,
  `cancel_nowait()` likewise, and `_dispatch_depth` never decrementing (6
  failures). `test_stop_drops_a_deferred_sequence_start` asserting
  `_restart_handle is not None` *before* `stop()` is exactly the right white-box
  assertion for a mechanism whose visible symptom is a duplicated task.
- **`connect()` guards, both layers.** `client.connect()` transport guard,
  `client.connect()` `_connecting` guard, `connection_made` second-transport
  rejection, the `transport.close()` inside it, and `door.connect()`
  idempotence — all five caught, each by a test that also proves the *device*
  only ever saw one connection (`len(accepted) == 1`) rather than just checking a
  log line.
- **Per-frame lenient decoding.** Caught on both client and simulator by
  deterministic split-frame tests, and backed by a genuine hypothesis property
  (`test_non_ascii_bytes_between_frames_lose_nothing`) that drives the real
  `data_received` with arbitrary chunking and arbitrary high bytes.
- **Schedule validation (`Schedule.from_dict`).** Range checks, 7-element
  `daysOfWeek` shape, legacy-bitmask bit order, `enabled` `"1"`-string handling,
  default times, and `SET_SCHEDULE_LIST` atomicity are each caught by a mutation,
  and the protocol-level tests assert the exact `FIELD_REASON` string, the echoed
  `msgID`, *and* that `state.schedules` was left untouched. This is the best-tested
  new code in the wave. (The one gap: no hypothesis property over `from_dict`
  itself — a "either raises `ValueError` or produces a schedule whose
  `is_day_active`/`is_sensor_allowed` never raise" property would be the natural
  complement to the existing `validate_schedule_entry` totality property. Not
  worth a finding on its own given the exhaustive deterministic matrix.)
- **ctl wait-timeout semantics.** The `await_response` state machine is
  well-tested: reordering the response/stop checks caused 6 failures, and removing
  `activity.clear()` was caught. The disconnect-while-waiting case is covered, and
  the `start_session` fixture cancels leaked session tasks on teardown so a failing
  assertion cannot hang cleanup.
- **`refused_port` fixture design.** Bind-without-listen both reserves the port
  against another worker's `bind(0)` and yields ECONNREFUSED; the socket is held
  for the whole test and closed in `finally`. Correct, and the tests that use it
  still assert the exact error text, which is what proves the fixture works.
- **Module-level scripts-dir registration + autouse reset.** The global is only
  written by `CommandHandler.__init__` and `cli.main`, the autouse
  `_reset_extra_scripts_dir` finalizer runs even on failure, and removing
  `set_extra_scripts_dir(scripts_dir)` from the handler was caught by 3 failures
  (list output, unknown-script hint, completion) — the three consumers the fix was
  written for.
- **Fuzz suite.** 21 properties, all real invariants (framing round-trip under
  arbitrary chunking, buffer caps, non-ASCII resilience, compress
  idempotence/coverage-preservation/shape/non-mutation, diff apply-equals-target,
  week-converter inverses, `make_bool` totality and case-insensitivity, tz parse
  totality). No tautological properties. Example counts are bounded and the whole
  fuzz suite runs in ~2 s.
- **Skips.** Ten `skipif` markers, all belt-and-braces for optional extras
  (`YAML_AVAILABLE`, `prompt_toolkit`), and both have the mandated
  dependency-absent equivalents (`YAML_AVAILABLE=False` monkeypatch tests plus a
  subprocess import-guard test; `TestWithoutPromptToolkit` with
  `importlib.reload`). Nothing is skipped in the default environment.
- **Assertion-free tests.** I enumerated all 14 by AST. Every one is either a
  genuine no-raise contract (`process_message("not a dict")`, `_send` without a
  transport, double `connection_lost`, `disconnect` before `connect`, doc-import
  `exec`) or an event-wait bounded by `asyncio.timeout`/`wait_for`, where the
  timeout is the assertion. No fake tests, no `assert True`, no read-backs.
- **Exact-message negative testing.** ~1,580 exact-equality assertions across the
  suite, including full multi-line usage strings, sorted alternative lists, and
  step-numbered script errors. Sampled the new files (`test_commands_settings.py`,
  `test_commands_info.py`, `test_ctl.py` local dispatch) — consistently exact,
  never substring-only where an exact string is knowable.
- **CI structure.** Four-version matrix, every entry uploads coverage,
  `--cov-fail-under=0` per entry with the real gate applied once after
  `coverage combine` (`coverage report` at the end inherits `fail_under = 100`).
  Both invocations of the suite are exercised in CI (unit job excludes fuzz, fuzz
  job runs only fuzz), so neither can silently lean on the other. Version-matrix
  files are in sync with CLAUDE.md's table.
- **Repo hygiene.** I made no modifications to any repository file; all mutation
  testing was done on copies under `/tmp`. `git status` for `src/` and `tests/` is
  clean.
