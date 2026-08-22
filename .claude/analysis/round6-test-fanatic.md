# Test Fanatic Analysis — Round 6

Commit audited: `8a24804`. Baseline reproduced before any analysis: **2324 passed in 34.93 s**
(`uv run pytest -q`), ruff/mypy clean, 100.00 % line+branch per `coverage report`.

Method: coverage is saturated, so this round is almost entirely **mutation testing**, run on a
`/tmp` copy of the tree (`/tmp/ppd-audit`) with a `PYTHONPATH` guard that aborts the run unless
`powerpetdoor.__file__` resolves under `/tmp/ppd-audit/src`. The guard was proved twice: once
positively (a `raise RuntimeError` at import time trips it) and once with a control mutation
(`MAX_SCHEDULE_INDEX = 255 → 254`, caught by `tests/simulator/test_protocol.py`). **60 mutations**
were evaluated: **50 caught, 5 provably equivalent, 5 genuine survivors** covering 4 distinct
gaps. Nothing in the repo was modified.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 2 |
| Medium | 2 |
| Low | 5 |
| Trivia | 3 |

The suite itself is in excellent shape — every question posed to this round about the *tests*
came back positive (golden wire covers every field on both emitters, `EventThrottle` really does
distinguish the doubling schedule from "always"/"once", the `on_start` veto proves zero steps
executed, the R5 fuzz rebuild measurably works). Both High findings are about the **coverage
gate**, not the tests: the 100 % invariant this project is built around is (a) not enforced by any
CI job and (b) measured against a silently reduced denominator.

---

## Findings

### H1 — No CI job enforces the 100 % coverage gate; every invocation overrides it to zero

`pyproject.toml:105` sets `fail_under = 100`. Every place CI could apply it explicitly turns it
off:

| Location | Command |
|---|---|
| `.github/workflows/test.yml:83` (unit matrix) | `pytest --ignore=tests/fuzz --tb=line -q --cov --cov-report=xml **--cov-fail-under=0**` |
| `.github/workflows/test.yml:171` (combine job) | `coverage report --show-missing **--fail-under=0**` |
| `.github/workflows/release.yml:29` | `pytest --ignore=tests/fuzz -q` (no coverage at all) |

`scripts/generate_gaps_report.py` is a reporter, not a gate — `main()` returns `0`
unconditionally, including when `files_with_gaps` is non-empty (it just renders a
"## Current Gaps" table). There is no `codecov.yml`, and the Codecov upload is
`if: env.CODECOV_TOKEN != ''` with `fail_ci_if_error: false`, so it cannot block either.

The comment at `test.yml:63-65` states the intent plainly — *"coverage is combined across ALL
supported versions **before the 100 % gate**"* — but the gate was never wired up. Coverage can
fall to any value and all jobs stay green; the invariant is currently enforced only by a
developer remembering the pre-commit checklist in `CLAUDE.md`.

**Fix:** `--cov-fail-under=0` on the *matrix legs* is correct (per-version coverage is legitimately
incomplete — that is the whole reason for the combine job). The combine step is where the gate
belongs: drop `--fail-under=0` from `test.yml:171`, or add an explicit
`coverage report --fail-under=100` step after it so the failure message names the gate.

---

### H2 — The `\.\.\.` exclusion pattern removes 34 statements and 28 branch destinations from the gate, and two real branch gaps are hiding in them

`pyproject.toml` `exclude_lines` contains `"\\.\\.\\."`. Coverage applies `exclude_lines` as a
bare `re.search` against each source line, so this matches **any line containing three dots** —
log strings, status prints, comments, and type annotations — not just Ellipsis stub bodies.

Measured by running `coverage report` twice against the *same* data file, changing only that one
pattern:

```
with    "\.\.\."  →  TOTAL 6391 stmts, 2280 branches, 100.00 %
without "\.\.\."  →  TOTAL 6425 stmts, 2308 branches,  99.95 %, 4 partial branches
```

Enumerated with `Coverage.analysis2()`, the lines excluded *only* by this pattern include:

- **the entire `ScriptRunner._wait_for_status` method — `simulator/scripting.py:544-564`, 21 lines
  and 8 branch destinations** — because its multi-line `def` signature contains
  `statuses: tuple[str, ...]`. A whole production method with two `raise` sites sits outside the
  gate.
- `simulator/scripting.py:112` — the `_STATUS_WAIT_CONDITIONS` dict literal.
- `simulator/commands/control.py:33` — `return CommandResult(True, "Shutting down...")`.
- `simulator/cli.py:461, 489, 518` — the three `status_print` calls in the script-driver loop.
- `client.py:1113, 1197, 1327, 1383, 1443, 1454` — six logging statements.
- `simulator/commands/schedules.py:10`, `simulator/commands/settings.py:11, 331` — imports.
- `simulator/engine.py:619`, `simulator/state.py:253, 263`, `framing.py:66`, `schedule.py:511`,
  `client.py:135, 167, 1032, 1682`, `simulator/commands/scripts.py:56, 161, 403`, plus the four
  genuinely-intended stubs in `simulator/commands/base.py:18, 113, 357, 366`.

Two of the hidden branch outcomes are not merely unmeasured, they are **genuinely untested**.
Both mutants survived the *full* 2324-test suite:

| Mutation | Result |
|---|---|
| `client.py:1442` `if not self._shutdown:` → `if True:` | **SURVIVED** (2324 passed) |
| `cli.py:517` `if script_delay > 0:` → `if script_delay >= 0:` | **SURVIVED** (2324 passed) |

- `handle_connect_failure()` is the funnel every connect failure goes through
  (`client.py:1216`). Nothing asserts that a failure landing *after* `shutdown()` does **not**
  log an error, disconnect and schedule a reconnect. `test_reconnect_skips_connect_when_shutdown`
  (test_client.py:829) covers the *reconnect task*, not this guard.
- `--script-delay 0` combined with `--loop` never exercises the "don't wait, just continue"
  path in the outer loop.

**Fix (verified):** replace the pattern with `(^\s*\.\.\.\s*$)|(:\s*\.\.\.\s*$)`. Scanned across
`src/` and `scripts/`, that regex matches exactly the five intended stub bodies
(`client.py:175, 1740, 1745`, `commands/base.py:357, 366`) and nothing else. Then add the two
missing tests; the coverage gate will demand them anyway once the pattern is tightened.

---

### M1 — A duplicate-index regression in `compute_schedule_diff` is invisible to the deterministic suite

`schedule.py:646`:

```python
used_indices = (
    matched_indices
    | set(reusable_indices[:i])
    | {e.get(FIELD_INDEX) for e in entries_to_set[:i]}   # ← mutated to set()
)
```

| Run | Result |
|---|---|
| `pytest tests/` (full) | CAUGHT — by exactly one test: `tests/fuzz/test_schedule_fuzz.py::TestDiffProperties::test_applying_diff_yields_target_content` |
| `pytest tests/ --ignore=tests/fuzz` | **SURVIVED** (2281 passed) |

CI's unit matrix runs `--ignore=tests/fuzz`, and `CLAUDE.md`'s own checklist requires the
deterministic suite to stand alone. Concrete repro of the bug the mutant introduces:

```
current = [Sun@0]
new     = [Sun (matches @0), Mon (new), Tue (new)]
correct → Mon@1, Tue@2
mutant  → Mon@1, Tue@1     # two SET_SCHEDULE entries share an index; one silently overwrites the other
```

The reason it slips through: `TestComputeScheduleDiff::test_new_entry_skips_indices_still_in_use`
(test_schedule.py) has exactly **one** brand-new entry, so the "second new entry must not reuse the
first's freshly-assigned index" rule is never exercised deterministically. One added test with two
brand-new entries closes it.

---

### M2 — `tests/TESTING_GAPS.md` under-reports the configured exclusions, and no test pins the list

`scripts/generate_gaps_report.py:313-318` hard-codes six bullets under "Automatic Exclusions".
`pyproject.toml` configures **seven** `exclude_lines` patterns. Missing from the report:

- `if __name__ == .__main__.:`
- `\.\.\.` — i.e. the pattern from H2, the one currently removing a whole method from the gate.

So the project's own gap-disclosure artifact does not disclose the exclusion that matters most.
None of the 32 tests in `tests/test_gaps_report.py` compares the rendered list against
`[tool.coverage.report].exclude_lines`; the generator can drift from the config indefinitely.
(Everything else in TESTING_GAPS.md checks out: the 6,391/2,280 figures match `coverage report`
exactly, and all five pragmas are reproduced with correct line numbers, types and reasons.)

**Fix:** read `exclude_lines` out of `pyproject.toml` and render it, plus a test asserting the
rendered bullets are exactly the configured patterns.

---

### L1 — The docs-coverage test is substring-gameable (demonstrated)

`tests/test_exports.py::TestEveryExportIsDocumented::test_every_exported_name_appears_in_the_prose_docs`
checks `name not in text` against the concatenated doc corpus. Substring, not token:

- 15 of the 121 exports are strict substrings of another export — `CMD_OPEN` < `CMD_OPEN_AND_HOLD`,
  `Schedule` < `ScheduleTime`, `PowerPetDoor` < `PowerPetDoorClient`, `FIELD_AUTO` <
  `FIELD_AUTORETRACT`, `DOOR_STATUS` < `FIELD_DOOR_STATUS`, `CMD_GET_SCHEDULE` <
  `CMD_GET_SCHEDULE_LIST`, `FIELD_MSG_ID` < `FIELD_MSG_ID_RESPONSE`, …
- **Demonstrated on a `/tmp` copy:** rewriting every standalone `FIELD_AUTO` in the doc corpus to
  `FIELD_AUTORETRACT` leaves `FIELD_AUTO` appearing nowhere as a token — and all 32 tests in
  `test_exports.py` still pass.

I checked all 121 names against a word-boundary regex: today **every** export does appear
standalone somewhere, so this is a latent hole rather than an active mis-report. Fix is one
regex: `re.search(rf'(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])', text)`.

---

### L2 — The `x in (True, False)` anti-pattern R5-M3(c) removed still lives in two fuzz tests

| Location | Assertion |
|---|---|
| `tests/fuzz/test_schedule_fuzz.py:259` | `assert validate_schedule_entry(entry) in (True, False)` — in a test named `test_validate_never_raises_and_returns_bool` |
| `tests/fuzz/test_client_fuzz.py:27` | `assert make_bool(text) in (True, False, None)` — docstring says "maps to **exactly** True, False or None" |

`1 in (True, False)` is `True` in Python, which is precisely the reasoning round 5 wrote into
`tests/fuzz/test_untrusted_input_fuzz.py:241`'s docstring when it replaced the same pattern there.
Neither of these pins the type its name promises. The bool contract *is* pinned elsewhere
(`assert validate_schedule_entry(...) is True/False` appears ~18 times in `tests/test_schedule.py`;
`make_bool(...) is True/False` in the sibling fuzz tests), so this is a consistency/naming defect
rather than a hole — but it is the exact pattern the persona rules forbid, left in two files.

---

### L3 — `_wait_for_status`'s "stopped" vs "timeout" error text is unasserted

Two independent mutants of `simulator/scripting.py:560-561` survive the full suite:

| Mutation | Result |
|---|---|
| `if stopper in done:` → never fires | **SURVIVED** (2324 passed) — control falls through to the timeout raise |
| `raise ScriptError("Script stopped while waiting")` → `raise ScriptError(f"Timeout waiting for condition: {condition}")` | **SURVIVED** (2324 passed) |

An operator who types `stop` during a `wait door_open` step would be told
*"Timeout waiting for condition: door_open"* — a misleading diagnosis — and no test would notice.
The two tests that do assert `"Script stopped while waiting"`
(`test_scripting.py:489, 495`) both reach the *other* two raise sites (`scripting.py:523` and
`:537`, the pre-check and the polling path). `test_engine_stop_fails_status_wait` reaches this
site but only asserts that the run fails, not what it says.

The persona rule is explicit: test the error *text*, not just the error type. Note that this
method is also the one H2 removes from the coverage gate — the two findings compound.

---

### L4 — Two property families are near-vacuous on their success path (measured)

Round 5's R5-M3 lesson ("a property is only worth its runtime if it draws the values it exists
for") was applied to the schedule/sanitize strategies but not to these. Measured over 600 draws
each, using the repo's own strategies:

| Property | Draws reaching the assertions |
|---|---|
| `tests/fuzz/test_tz_fuzz.py::test_never_raises_and_never_half_parses` | **3 / 600** (0.5 %) — `st.text(max_size=40)` essentially never parses as a POSIX TZ string, so the "never half-parses" half of the name is untested |
| `TestWireCoercerTotality::test_int_coercer_only_raises_wire_value_error` | 6 / 600 |
| `…_number_coercer…` | 7 / 600 |
| `…_string_coercer…` | 9 / 600 |
| `…_flag_coercer…` | 16 / 600 |
| `TestScheduleCoercerTotality::test_int_coercer_is_total` | 11 / 600 |

In every case the *totality* half (raises only the declared type) runs 600/600, and the invariants
are separately pinned by deterministic tests — `test_tz_utils.py::test_every_timezone_has_parseable_posix`
walks every zone tzdata ships, with no sampling. So this is redundancy, not a hole. It is listed
because the same measurement is what justified rebuilding the schedule strategies last round, and
because the fix is cheap: mix `_REAL_POSIX_STRINGS` (and mutated variants of them) into the tz
strategy, and add `_well_shaped_*` feeds for the four wire coercers.

For contrast, the R5-rebuilt strategies measure healthy on the same harness: `_schedule_payloads`
parses on **both** sides 257/600 (243 of them non-empty dicts), `_time_payloads` 102/600,
`_well_shaped_days` / `_well_shaped_time` / `_well_shaped_bitmask` 100 %.

---

### L5 — A small cluster of assertions that accept more than the one correct answer

Found by an AST + regex sweep of all 47 test files (28 k lines). Zero tautologies, zero
`assert True`, zero tests that mock their own subject — these are the only real hits:

| Location | Assertion | Why it is weak |
|---|---|---|
| `tests/simulator/test_engine.py:275` | `assert engine.open() is True` | Runs **inside a status listener**, and `simulator/engine.py:183` wraps listener calls in `except Exception: logger.exception(...)`. `AssertionError` is an `Exception`, so this assertion cannot fail the test directly — it only surfaces indirectly via `sampled_inside_dispatch == [...]` at line 290, with a confusing message. |
| `tests/simulator/test_ctl.py:168` | `assert line == b"run full_test_suite wait\n"` | Runs in an `asyncio.start_server` connected-callback; an `AssertionError` there goes to the loop exception handler, not the test. Also guarded by `if line:`. |
| `tests/simulator/test_scripting.py:1190-1195` | `test_all_builtin_scripts_parse` asserts only `script.name is not None` and `len(script.steps) > 0` | Claims "all built-in scripts parse without errors" but pins nothing about the parse; also loops `list_builtin_scripts()` with no non-emptiness guard in this test, so it would pass vacuously if discovery broke. |
| `tests/simulator/test_cli.py:2110` | `assert out.count(prompt_text) >= 3` | The comment on the same line enumerates exactly three prompts ("initial, after empty line, after output"), so `== 3` is knowable and a duplicated-prompt regression passes. |
| `tests/test_schedule.py:697` | `assert key is not None` | The content key for a fixed literal entry is exactly knowable. |
| `tests/simulator/test_server.py:216` | `assert sim.server is not None` (sole assertion in `test_start_stop`) | Does not check the server is listening; `test_listens_on_port` does, so this one is pure redundancy. |
| `tests/simulator/test_commands_info.py:226, 244` | `assert mock_client._send.called` | Accepts any payload. Adequate in context (the same tests pin `result.message` exactly, and the payloads are pinned in `test_protocol.py`), but it is not a single-outcome assertion. |

I checked the one that looked most consequential — `tests/simulator/test_client_integration.py:282`,
where `assert isinstance(result, dict)` is the *only* assertion in `test_get_hw_info` — by
mutation, and it is **covered elsewhere**: both dropping `FIELD_HW_REVISION` from the response and
emptying `FIELD_FWINFO` entirely are caught by `test_protocol.py::TestGetCommandHandlers::test_get_hw_info`
(and the latter also by `test_door.py::test_refresh_all`). So that one is a weak assertion, not a
hole; it should still pin the five fields the way its sibling battery test at line 273 pins three.

---

### T1 — `EventThrottle.reset()`'s `self._reported = 0` is unobservable (equivalent mutant)

Deleting that line survives all 2324 tests, and correctly so: after `reset()`, `_next == 1`, so the
very next `record()` always logs and immediately rewrites `_reported`. No sequence of
`record`/`flush`/`reset` can distinguish the two versions. Harmless defensive symmetry with
`__init__`; noted only so a future mutation-testing pass does not chase it.

### T2 — Three provably dead sub-expressions in `compute_schedule_diff` (equivalent mutants)

All three survive because they cannot affect the result, not because a test is missing:

- `set(reusable_indices[:i])` (`schedule.py:645`) — this line only runs in the `else` branch, where
  `i >= len(reusable_indices)`, so the slice is always the whole list.
- `or new_index in current_indices` (`schedule.py:648`) — `used_indices ⊇ matched ∪ reusable =
  current_indices` in that branch, so the clause can never be the deciding one.
- `if len(entries_to_set) < len(reusable_indices):` (`schedule.py:653`) — the slice on the next line
  already yields `[]` whenever the condition is false.

Confirmed by targeted mutants (`SD-3`, `SD-4`, `SD-5`, all SURVIVED) and by the complementary
mutant that *is* caught (`sorted(...)` → `sorted(..., reverse=True)`, caught by
`test_more_current_than_new_deletes_excess`, so index ordering **is** pinned). Worth a
simplification pass rather than new tests.

### T3 — One pragma justification is inconsistent with a tested sibling

`simulator/ctl.py:591`'s `except EOFError` carries *"defensive: both prompt paths signal EOF by
returning None rather than raising"*. That is accurate (`prompt_common.py:741` swallows
prompt_toolkit's `EOFError`, `_basic_readline` returns `None`) — but the structurally identical
`except EOFError` at `prompt_common.py:801` has **no** pragma and is covered, by a test that makes
the prompt raise. The same technique applies here, so "cannot be triggered deterministically" is
overstated. Either cover it the same way or delete the clause as unreachable.

---

## Round 5 Fix Verification

Every claim re-verified by mutation on the `/tmp` tree, not by reading the diff.

| R5 item | How verified | Result |
|---|---|---|
| **R5-M1** `_declined` / `_pending_direct_losses` decrement | `-= 1` → `= 0` on each; `superseded = ... > 1` → `>= 1` | 3/3 CAUGHT — `test_two_declined_transports_are_counted_off_one_at_a_time`, `test_three_adopted_transports_leave_the_newest_alive`, `test_connection_lost_triggers_disconnect` |
| **R5-M2** symlink containment (unauthenticated control channel) | drop `and candidate.parent == base`; drop `script_ref.startswith(".")` | 2/2 CAUGHT — `test_restricted_refuses_a_symlink_out_of_the_scripts_dir[.yaml]`, `test_restricted_rejects_hidden_names` + `test_dotfile_rejected` |
| **R5-M3(a)(c)** rebuilt strategies | re-measured over 600 draws with the repo's own strategies | Healthy: both-parsers-succeed 257/600, time payloads 102/600, well-shaped days/time 100 %. (Two *unrebuilt* families are still thin — see L4.) |
| **R5-M3(b)** `sanitize_text` checked against an independent codepoint set | narrow `_CONTROL_CHAR_RE` to `[\x00]` | CAUGHT by 6 tests — the fail-closed direction works exactly as documented |
| **Golden wire fixtures** — *does it catch every field, or only `enabled`?* | 13 type mutations: `index`→str, `enabled`→bool, `daysOfWeek`→bools, `inside`/`outside`→ints, `{hour,min}`→str, zero-block ints→bools, on **both** emitters, including the `outside=True` branch the golden payload does not exercise | **13/13 CAUGHT.** Every field, both sides. `assert_schedule_wire_types`'s `_is_wire_int` really does defeat `True == 1` |
| **`EventThrottle`** — *can the tests tell doubling from "always" / "once"?* | `_next *= 2` → `+= 1` (log always), → `*= 1000000` (log once), `>=` → `>` (skip the first), drop `_reported` write, `flush` `>` → `>=`, `reset` drops `_next`, `total +=` → `=`, `record(amount=1)` → `0` | **8/8 CAUGHT** — the exact-list assertion in `test_reports_on_a_doubling_schedule` (`["seen 1"…"seen 16"]` from 16 records) is what does it. The 9th, `reset()` dropping `_reported`, is an equivalent mutant (T1) |
| **`on_start` veto** — *does it prove zero steps ran, or just a return value?* | ignore the veto; make the veto return `True` | 2/2 CAUGHT. `test_on_start_returning_false_abandons_the_run` runs a `set power off` script and asserts `simulator.state.power is True` afterwards — a real side-effect check — plus `current_script is None` and `busy is False`. `test_on_start_fires_once_the_run_lock_is_held` pins `seen == [True]`, so exactly one call with the lock held |
| **Segment-list `FrameScanner`** | `retained +=` → `=`; overflow `>` → `>=`; garbage count includes whitespace; disable piece coalescing; don't clear `head` after a frame | 5/5 CAUGHT, incl. `test_dribbled_frame_is_scanned_once_per_byte` and `test_retained_length_tracks_the_pieces` |
| **`_payload_mapping()`** | drop the `isinstance(value, dict)` check; `msg.get(key, {})` | 2/2 CAUGHT by `TestProcessMessageDefensive` |
| **`QueuedScript` / `ScriptQueue`** | `start()` ignores `cancelled`; `pending()` order swap; `qsize()` drops the claim | 3/3 CAUGHT |
| **`test_every_exported_name_appears_in_the_prose_docs`** — *meaningful or gameable?* | rewrote the doc corpus so `FIELD_AUTO` survives only inside `FIELD_AUTORETRACT` | **Gameable** — see L1. Meaningful today (all 121 names appear standalone), but the check is substring, not token |
| **Infrastructure** | full suite run serially (`-n0 -p no:cacheprovider`) from a clean copy | 2324 passed in 160 s — identical result to `-n auto` (35 s). No ordering or xdist dependence, no flakes across 4 full runs |

---

## Areas Reviewed With No Findings

- **Skipped tests:** zero skips in a dev environment. The five `skipif` markers are optional-extra
  probes (`PyYAML`, `prompt_toolkit`) that CI always installs; the persona's "no skips" rule holds.
- **Fake tests:** zero. No `assert True`, no `assert x == x`, no tautologies, no test that mocks its
  own subject (26 name-overlap candidates checked — all collaborator doubles). The 44
  read-back-shaped candidates all have production calls between the write and the read.
- **`# pragma` in `src/`:** exactly 5 (`cli.py:100, 689` `no branch`; `ctl.py:365, 591, 642`
  `no cover`), all with written justifications, all reproduced correctly in TESTING_GAPS.md with
  line numbers, types and code. Only T3 quibbles with one justification.
- **Timing / flakiness:** the only real-time sleeps are `test_ctl.py:171` (0.5 s vs a 0.05 s
  timeout) and `:221` (0.1 s gaps vs a 1.0 s budget) — both 10× margins with the reasoning written
  into the docstrings. Everything else is `asyncio.sleep(0)` loop yields, `asyncio.Event`s or
  awaited futures. `timeout = 60` and `filterwarnings = ["error"]` are both in force.
- **Error text:** the CLI/ctl surface asserts exact strings throughout
  (`result.message == "Invalid argument: zap. Use 'clear' or a number."` and friends), not just
  success flags. L3 is the one place a message is left unpinned.
- **`compute_schedule_diff` correctness beyond M1:** index reuse, ordering, deletion of leftovers
  and input immutability are all pinned (`SD-1b`, `SD-2`, `SD-6` all caught).
- **Client receive path:** every framing mutant was caught, several by the client-level tests
  rather than the framing unit tests, which is the right layering.
- **`tests/TESTING_GAPS.md` numbers:** 6,391 / 2,280 and the per-category table match
  `coverage report` exactly. Only the exclusions prose is wrong (M2).
