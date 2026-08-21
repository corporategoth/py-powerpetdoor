# Test Fanatic Analysis — Round 2

## Summary

This is a different suite from Round 1. 1684 tests pass in ~25 s wall (verified by
execution), and coverage is genuinely 100.00% lines **and** branches locally
(`uv run pytest --cov`). The sleep-as-synchronization epidemic is gone: every
remaining `asyncio.sleep(0)` is a bounded yield-loop under `asyncio.timeout(...)`,
and the three real-duration sleeps in `test_engine.py` are bounded
negative-observation windows ("the door must NOT close while blocked"), which is
the correct pattern absent virtual time. The two Round-1 fake tests are gone and
replaced with real ones. The fuzz suite (20 hypothesis properties) pins real
invariants — framing round-trip under arbitrary chunking, buffer caps,
compress-schedule coverage preservation, diff-apply-equals-target, non-mutation,
totality — not vacuous properties. The formerly-0% terminal front end (ctl, cli,
prompt_common) is now the best-tested part of the project, including
prompt_toolkit sessions driven through pipe input and exact-text assertions on
every error message. All five `# pragma: no cover`/`no branch` sites are narrow,
individually justified, and accurately inventoried in TESTING_GAPS.md.

I hunted hard and found **one High, two Medium, two Low, and three Trivial**
issues. The High is real and actionable: the deterministic (non-fuzz) suite does
NOT reach 100% on its own — exactly one branch (`schedule.py` 113->117, the
happy path of validating a valid outside-enabled entry) is covered only by the
hypothesis suite, and CI's coverage gate excludes fuzz, so the next CI push will
fail the 100% gate. Verified by execution: `pytest --ignore=tests/fuzz --cov`
→ 99.99%, `FAIL Required test coverage of 100.0% not reached`.

Finding counts: **Critical: 0, High: 1, Medium: 2, Low: 2, Trivial: 3** (8 total).

---

## Findings

### High

---

#### R2-H1. The deterministic suite cannot pass the 100% gate on its own — one branch is covered only by fuzz, and CI excludes fuzz from coverage

**Severity:** High
**Files:** `src/powerpetdoor/schedule.py:113` (branch 113->117),
`tests/test_schedule.py:80-189` (`TestValidateScheduleEntry`),
`.github/workflows/test.yml:77` (`pytest --ignore=tests/fuzz ... --cov`),
`.github/workflows/test.yml:89-104` (fuzz job: no `--cov`, main-push only)

Verified by execution: `uv run pytest --ignore=tests/fuzz --cov` yields
**99.99%** — `schedule.py 113->117` missing — and the gate fails. That branch is
`validate_schedule_entry` returning `True` through the outside-sensor time
checks, i.e. **the happy path of validating a valid outside-enabled entry**.
`TestValidateScheduleEntry` has three outside *negative* tests
(test_schedule.py:146-166) but never validates a valid outside entry — the
negative tests exist and the positive one doesn't, which inverts the usual
failure mode. Today the branch is reached only by
`tests/fuzz/test_schedule_fuzz.py::test_compress_output_shape` when hypothesis
happens to generate `outside=True` entries.

Consequences, in order of severity:
1. CI's unit-test matrix runs `--ignore=tests/fuzz`, and the coverage-report job
   combines only those runs before `coverage report` (fail_under=100). The
   branch is pure dict logic — identical on 3.11–3.14 — so the combined result
   will be 99.99% and **the next CI push to main goes red**. (The committed
   TESTING_GAPS.md claims 100.00% because it was generated from a local run that
   included the fuzz suite — commit 3f96bb8, timestamps match.)
2. Even ignoring CI mechanics, a coverage gate satisfied by randomized example
   generation is not deterministic. The 100% claim must hold from the
   deterministic tests alone; fuzz coverage should be a bonus, never a
   load-bearing part of the gate.

**Recommendation:** Add to `TestValidateScheduleEntry`:
```python
def test_valid_outside_entry_passes(self, valid_schedule_entry):
    valid_schedule_entry[FIELD_INSIDE] = False
    valid_schedule_entry[FIELD_OUTSIDE] = True
    assert validate_schedule_entry(valid_schedule_entry) is True
```
(and, belt-and-braces, an inside+outside-both-valid case). Then confirm
`pytest --ignore=tests/fuzz --cov` passes at 100.00% and consider adding exactly
that command as a CI-parity check to the pre-commit checklist so the
deterministic gate can never silently lean on fuzz again.

---

### Medium

---

#### R2-M1. Surviving Round-1 overbroad assertion: `test_run_alias_r` still accepts contradictory outcomes

**Severity:** Medium
**File:** `tests/simulator/test_commands.py:344-349`

```python
result = await command_handler.execute("r nonexistent")
# Command was recognized (even if script doesn't exist)
assert "Error" in result.message or result.success is False
```

This is the one surviving instance of Round-1 H2's pattern (it was explicitly
listed there as `test_commands.py:342`) and a direct violation of CLAUDE.md test
rule 1. The single correct outcome is fully determined:
`ScriptsCommandsMixin.run` (`src/powerpetdoor/simulator/commands/scripts.py:100-116`)
catches the `ScriptError` from `get_builtin_script` and returns
`CommandResult(False, f"Error: {e}")`, and the error text is already pinned
exactly elsewhere (`tests/simulator/test_scripting.py:886`,
`tests/simulator/test_cli.py:410`).

**Recommendation:**
```python
assert result.success is False
assert result.message.startswith("Error: Unknown built-in script: nonexistent")
```

---

#### R2-M2. Fuzz suite does not run on pull requests — property regressions land on main before they are detected

**Severity:** Medium
**File:** `.github/workflows/test.yml:89-104`
(`if: github.event_name == 'push' && github.ref == 'refs/heads/main'`)

The fuzz job is gated to pushes on main, so a PR that reintroduces a framing bug
of exactly the class Round 1 found (C5: brace-in-string corruption — caught in
seconds by `test_round_trip_any_chunking`) merges green and only fails
*after* it is on main. The cost argument does not hold: the entire fuzz suite
runs in **1.8 s** with xdist (verified by execution — example counts are
deliberately bounded). Additionally `--hypothesis-seed=0` pins the seed, which
is the right call for reproducibility but means CI never explores new examples
anywhere.

**Recommendation:** Drop the `if:` gate (or extend it to `pull_request`) so fuzz
runs on every PR — 2 s is noise next to the 4-version matrix. Optionally add a
scheduled (cron) fuzz run *without* the fixed seed and with raised
`max_examples` for actual exploration.

---

### Low

---

#### R2-L1. Bind-then-close "connection refused" ports can be re-assigned under parallel xdist workers

**Severity:** Low
**Files:** `tests/simulator/test_ctl.py:163-170` (`test_connection_refused_message`),
`tests/simulator/test_ctl.py:245-251`, `tests/simulator/test_ctl_interactive.py:193-204`
(`test_connection_refused_exits_1`); same pattern via `unused_tcp_port` in
`tests/test_client.py:832` and `tests/test_door.py:757, 768`

These tests obtain a "dead" port by binding an ephemeral port, closing it, and
then connecting to it expecting refusal. With `-n auto` there is a real (rare)
race: between the close and the connect, the OS can hand the same ephemeral port
to another worker's simulator/daemon fixture (every fixture in this suite binds
port 0), turning the expected refusal into a successful connection to an
unrelated test's server and failing the exact-message assertion. Low probability,
but it is the one structural flake risk left in an otherwise
deterministically-synchronized suite.

**Recommendation:** Keep a socket **bound but not listening** for the duration of
the test: a bound-not-listening TCP port both stays reserved (no reuse by other
workers) and refuses connections with ECONNREFUSED (verified by execution on
this platform). A small `reserved_refusing_port` fixture yielding the port while
holding the bound socket covers all five sites.

---

#### R2-L2. Wire-level integration tests assert "some status updates" instead of the exact broadcast sequence

**Severity:** Low
**Files:** `tests/simulator/test_integration.py:229-230, 269-274, 291-296`

`test_open_door_sends_status_updates` ends with
`assert len(status_updates) > 0`, `test_sensor_trigger_sends_status_updates`
with `any(... != DOOR_STATE_CLOSED)`, and `test_full_door_cycle_messages` with
`len(non_closed) > 0`. These are not contradictory-outcome assertions (the
`receive_until` predicates already guarantee a matching message arrived), but
they are weak: a simulator that skipped SLOWING or broadcast states out of order
would still pass. The suite already owns the right tool —
`tests/simulator/scripts/conftest.py:101-115` (`wait_for_status_sequence`)
asserts the exact `FULL_CYCLE` list over the same wire, and
`test_engine.py:100-108` pins the exact transition sequence at engine level.

**Recommendation:** Replace the `> 0` / `any(...)` endings with exact expected
sequences (reuse the `FULL_CYCLE` constant): e.g. for the sensor-trigger cycle,
capture until CLOSED and assert
`statuses == [RISING, SLOWING, HOLDING, CLOSING_TOP, CLOSING_MID, CLOSED]`.

---

### Trivial

---

#### R2-T1. Round-1 T2 not addressed: `test_cycle_alias_y` is still duplicated

**Severity:** Trivial
**File:** `tests/simulator/test_commands.py:184-188` and `:338-342`

Identical body in `TestCycleCommand` and `TestAliases` (confirmed: 2 exact
`def test_cycle_alias_y` occurrences). Redundancy is the one valid deletion
reason — remove one.

---

#### R2-T2. ~380 redundant `@pytest.mark.asyncio` decorators remain (Round-1 M8 residue)

**Severity:** Trivial
**Files:** `tests/simulator/test_protocol.py` (92), `tests/simulator/test_commands.py`
(83), `tests/simulator/test_cli.py` (62), `tests/simulator/test_engine.py` (54),
plus 5 more files — while `tests/test_client.py`, `tests/test_door.py`, and the
newer command-mixin files correctly rely on `asyncio_mode = "auto"`.

Pure noise (the marks are harmless), but the inconsistency signals two authoring
eras and invites cargo-culting. Strip them in one mechanical pass.

---

#### R2-T3. Dataclass custom-kwargs read-back test survives in test_door.py

**Severity:** Trivial
**File:** `tests/test_door.py:165-172` (`TestNotificationSettings.test_custom_values`)

Constructs `NotificationSettings(inside_on=True, ...)` and asserts the kwargs
were stored — this tests the `@dataclass` decorator, not project code (Round-1
L1's category; the test_state.py/test_commands.py instances were removed but
this one remained). The `test_defaults` sibling is fine (it pins contract
defaults). Delete `test_custom_values` or fold it into a behavior test.

---

## Round 1 Fix Verification

| # | Round-1 finding | Status |
|---|---|---|
| C1 | CLI front end untested (ctl 0%, cli 4.6%, prompt_common 11%) | **Fixed.** All three at 100%/100%. `test_ctl.py` + `test_ctl_interactive.py` drive both prompt paths (add_reader fallback via os.pipe stdin; prompt_toolkit via `create_pipe_input` + `DummyOutput`), exact error texts, sanitization, history recall. `test_cli.py` covers main() arg matrix, daemon end-to-end over the control channel, script queue, startup scripts, InteractivePrompt handler swap/restore. `test_prompt_common.py` pins lexer token classes per word, completer position-awareness, recall matrix, history-file permissions. |
| C2 | Fake keepalive test (`+=` operator) | **Fixed.** `test_client.py:604-675`: real `keepalive()` invocations — unanswered ping increments and re-pings, `MAX_FAILED_PINGS` disconnects (transport closed, no new PING), PONG resets counter. |
| C3 | Fake `del_listener` test | **Fixed.** `test_client.py:498-555`: registers all 17 listener kinds, calls the real `del_listener`, asserts absence from every registry; unknown-name no-op pinned. |
| C4 | `data_received` crashes on garbage | **Fixed** (production + tests). New `framing.py` with `TestClientProtocolViolations` (garbage, garbage-prefix recovery, non-ASCII, malformed-JSON skip, oversized-buffer disconnect, empty chunk) plus defensive `process_message` tests (missing CMD/success, non-dict, typed `CommandError` with reason). |
| C5 | `find_end` brace-in-string corruption; zero fuzz | **Fixed.** String-aware scanner with escape handling (`test_framing.py`), plus `tests/fuzz/` round-trip-under-arbitrary-chunking properties against both `extract_frames` and the full `client.data_received` path. |
| H1 | `_on_settings` `bool("0")` bug | **Fixed.** `TestSettingsCoercion` (test_door.py:670-741): all-string "0"s → all False with `pet_proximity_keep_open is True` (inversion pinned both directions); end-to-end `refresh_settings` power-off reflection pins the ordering. |
| H2 | Overbroad multi-outcome assertions | **Mostly fixed.** Door/server/tz assertions now wait for stable states and assert single values. One survivor: **R2-M1**. |
| H3 | 101 sleeps as synchronization | **Fixed.** `wait_for_status`/status listeners on the engine, event-driven `MessageCapture`/`RecordingStdout.wait_for`, yield-loops bounded by `asyncio.timeout`. Battery tests now call `_battery_tick()` directly with exact arithmetic (`== 49`, `== 20`). Remaining real-duration sleeps (`test_engine.py:317, 627, 641`) are bounded negative-observation windows — legitimate. |
| H4 | `check_receipt` untested | **Fixed.** Retransmit-exactly-once (byte-identical), drop-after-`MAX_FAILED_MSG` fails the future with `TimeoutError`, prompt-response cancels the timer, `RuntimeError` write → disconnect, disconnect-during-rate-limit-sleep survives. |
| H5 | No reconnect tests | **Fixed.** Real TCP server restart with reconnect (event-driven), refused-connect schedules jittered backoff, backoff doubling/cap pinned numerically, stop/shutdown cancel pending reconnects (asserted on the task, not repr strings). |
| H6 | Hypothesis unused | **Fixed.** 20 property tests: framing (4+1), make_bool totality/case-insensitivity (5), compress idempotence + coverage-preservation + shape + non-mutation, diff apply-equals-target + non-mutation, week converters inverse, validate totality, tz parse totality + exhaustive real-footer corpus. Real invariants throughout. |
| H7 | No concurrency tests | **Fixed.** Parallel notify commands resolve their own futures against a serving loop; out-of-order responses; disconnect mid-flight → typed `ConnectionError`; offline-queued messages flush in priority order (PING, OPEN, GET_SETTINGS exact order). |
| H8 | Simulator protocol violations | **Fixed.** Garbage discarded without buffer poisoning (follow-up PING answered), garbage-prefix resync, non-ASCII, unknown command → `success:"false"` with reason, `msgId: 0` echoed, log sanitization of hostile command names. |
| H9 | Command-mixin coverage collapse | **Fixed.** Seven dedicated files (base 79 tests, history 56, handler, info, settings, schedules, toggles) with exact-message assertions (e.g. `"Safety lock: ON"`, full add-schedule messages, unknown-subcommand lists). |
| H10 | door.py public API gaps | **Fixed.** Two-step schedule refresh (incl. empty and per-index-failure), set/delete roundtrips, schedule-change callbacks, partial notification updates preserving cached values, latency set/cleared, fw/hw version formatting matrix, toggle-while-closing no-op, position map parametrized over all 8 states, callback isolation. |
| H11 | compress_schedule crash on invalid entries; boundaries | **Fixed.** `ValueError` contract with entry-position identification, adjacent-window merge, zero-length, 23:59, duplicates collapse, all-days-zero drop, input non-mutation. One *new* gap found adjacent to it: **R2-H1**. |
| M1 | No-exception-only asserts | **Fixed.** `_capture_messages` records exact dispatched payloads; `hold_time == 15.0`-style exact values; exactly-one-PONG assertions. Remaining assert-free tests are genuine no-raise contracts (e.g. `InvalidStateError` guards) or event-waits bounded by `asyncio.timeout` (the timeout is the assertion). |
| M2 | tz angle brackets, statistical assertions, `if posix:` skips | **Fixed.** Angle-bracket parsing implemented and tested (incl. real `Buenos_Aires == "<-03>3"`), exhaustive every-zone test with `unparseable == []` (no ratios, no guards), TZif footer error branches, zoneinfo DST cross-check on 2026 transition dates. |
| M3 | YAML skips with no no-YAML tests | **Fixed.** `YAML_AVAILABLE=False` monkeypatch tests plus a subprocess import-guard test; skip markers remain as belt-and-braces only. |
| M4 | Dead conftest helpers | **Fixed.** `_auto_respond`, `respond_failure`, `create_mock_response`, `MOCK_SCHEDULE_ENTRY` all gone; conftest is lean and fully used. |
| M5 | Wall-clock-coupled script tests | **Fixed.** Built-in scripts use `wait_for` (pinned by `test_script_has_expected_steps`: `"wait" not in actions`); tests assert exact broadcast sequences via `wait_for_status_sequence(FULL_CYCLE)` with fast timing. |
| M6 | scripting.py 55.7% | **Fixed.** 100%; condition matrix, stop-paths (before/during/between), from_file errors, assert-failure texts, completer prefix filtering, verbose mode. |
| M7 | Simulator server/protocol branches | **Fixed.** 100%; battery threshold/tick tests, broadcast payloads, reversal state machine deterministically driven through engine hooks (`test_close_reverses_rising_to_closing_mid` etc.), auto-retract chains with exact sequences. |
| M8 | No markers/timeout, registry pollution risk | **Partial.** Registry guard: **fixed** (deep snapshot/restore fixture in test_commands_handler.py, synthetic-command cleanup fixtures in test_ctl.py). Slow markers: moot — the whole suite is 25 s. `pytest-timeout`: still absent, but every wait is bounded by `asyncio.timeout`/`wait_for`; residual risk accepted. Redundant asyncio marks: **not fixed** (R2-T2). |
| L1 | Read-back tests | **Mostly fixed** (`test_empty_message_is_falsy`, `test_set_interactive_mode`, state read-backs removed). One dataclass read-back survives (R2-T3). |
| L2 | Boolean days_of_week invisible | **Fixed.** `isinstance(day, bool)` on defaults and from_dict (incl. legacy bitmask), `to_dict` wire-ints pinned (`not isinstance(d, bool)`). |
| L3 | `del_handlers` KeyError | **Fixed.** Partial-registration and never-added both no-raise; double-disconnect covered in test_door.py. |
| L4 | `available` returns None | **Fixed.** `is True` / `is False` identity assertions. |
| L5 | Hand-simulated keepalive test | **Fixed.** Folded into the real keepalive tests. |
| T1 | CLAUDE.md references ci.yml | **Fixed.** Table now points at test.yml/release.yml. |
| T2 | Duplicate `test_cycle_alias_y` | **Not fixed** (R2-T1). |

Score: 28 of 31 findings fully addressed; 2 partially (H2, M8); 1 untouched (T2).

## Areas Reviewed With No Findings

- **Pragma audit** (`src/`): exactly 5 sites — `ctl.py:319` (selector `remove_reader`
  in a done-callback: not deterministically triggerable on Linux), `ctl.py:498`
  (`EOFError` guard: both prompt paths signal EOF via `None`), `ctl.py:549`
  (outer-cancel racing the reader-task await), `cli.py:97` (`no branch`: disable
  after enable always has a handler), `cli.py:593` (`no branch`: server sockets
  bound after `start()`). Each is genuinely defensive, annotated with a specific
  reason, and TESTING_GAPS.md's pragma inventory matches the source exactly.
- **`tests/test_client.py` mutation-resistance sampling**: I traced framing
  (exact dispatched-payload lists, buffer state), priority ordering (exact
  dequeue order), receipt retry (byte-identical retransmit counts), monotonic-vs-
  wall-clock timing (a patched `time.time` recorder proves it is never called),
  future resolution (exact results, typed exceptions with cmd+reason), per-field
  listener routing (each listener's exact `(field, value)` call list), and the
  13-command parametrized missing-payload matrix. Flipping conditions in
  `client.py` framing, keepalive, receipt, or dispatch logic would be caught by a
  specific assertion, not just "some test somewhere".
- **`tests/simulator/test_engine.py`**: exact full-cycle transition sequences
  (including the 9-state auto-retract chain with same-task identity assertions),
  reversal matrix in both directions, hold-deadline arithmetic via
  `pytest.approx(loop.time() + 10.0)`, retrigger-window semantics, lifecycle
  (stop cancels tasks/waiters, resolved waiters survive). This file is now the
  standard the rest of the suite was asked to meet in Round 1 — and does.
- **Fuzz suite quality** (`tests/fuzz/`): all 20 properties are real invariants
  with bounded example counts; the client round-trip property drives the *actual*
  `data_received` path with dispatch captured synchronously. No tautological
  properties found.
- **`tests/simulator/test_ctl_interactive.py` synchronization design**: the
  `ScriptedDaemon` + `RecordingStdout.wait_for(needle, count)` harness is fully
  event-driven, including a clever stale-response test that uses a LOG marker to
  prove the stale `OK:` is queued client-side before the next command. Zero
  sleeps.
- **Exact-message negative testing**: sampled across ctl (connection refused/
  timed out/closed-without-response, each with exact text), commands
  (usage strings, unknown-subcommand lists with sorted alternatives), scripting
  (step-numbered error texts), protocol (reason fields). Consistent and specific.
- **xdist hygiene**: every server binds port 0 and reads back the bound port
  (`on_ready` callback for the daemon path); global command-registry mutation is
  guarded by deep snapshot/restore fixtures; the `importlib.reload(ctl)` test
  restores in `finally`; the session-scoped event-loop fixture documents and
  fixes a real pytest-asyncio ResourceWarning interaction under
  `filterwarnings=["error"]`. (Sole residual risk is R2-L1's port race.)
- **`tests/test_exports.py`**: doc import blocks are extracted (with a guard
  against the extractor matching nothing) and exec'd; `__all__` completeness and
  star-import resolution pinned.
- **TESTING_GAPS.md accuracy**: metrics, category table, exclusion list, and the
  per-file pragma inventory all match the actual configuration and source (with
  the caveat in R2-H1: its 100.00% was generated from a fuzz-inclusive local
  run, which the CI pipeline as committed will not reproduce).
- **CI matrix structure**: 3.11/3.12/3.13/3.14 all run the full unit suite; every
  matrix entry uploads coverage; combination happens before the gate;
  `REFERENCE_PYTHON` handling and the version-matrix files are in sync with
  CLAUDE.md's table.
