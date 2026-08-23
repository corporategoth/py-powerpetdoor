# Test Fanatic Analysis — Round 10

Commit `65ad86d`. 2869 tests, 6902 statements / 2448 branches, 100.00% both.

Round 9 closed the coverage-*configuration* class: `TestTheCoverageGateIsWhatItClaims`
now asserts `branch`, `source`, `omit`, `exclude_lines` and `partial_branches` by
value, `GATED_SOURCE_DIRS` is derived from `coverage.run.source` rather than
duplicated, and `partial_branches` is anchored and fed to the prose sweep. I
re-measured that machinery first (see *Round 9 Fix Verification*) and it holds.

The class did **not** end there. It moved one file over. This round's headline
finding is the fifth instance, in the file the project's own test docstring
already names as the real gate.

---

## Harness (established and falsified before any finding)

Everything below was executed. Nothing is reasoned from reading alone.

**Tree copy.** `tar -cf - . | (cd $WORK && tar -xf -)` from the repository root
for every single mutation — never a file list, so a mutation can never be
masked by a file the harness forgot to copy. 17 MB, `.git`/`.venv`/caches
excluded.

**Import forcing.** `PYTHONPATH=/tmp/r10/plug:$WORK/src:$WORK`. The venv holds
an editable `.pth` pointing at the real checkout (`__editable__.pypowerpetdoor-0.3.0.pth`
→ `/home/prez/src/pypowerpetdoor/src`), which PYTHONPATH precedes.

**Guard plugin** (`/tmp/r10/plug/guardroot.py`, loaded with `-p guardroot`)
fails the session unless `powerpetdoor.__file__` resolves under `$R10_EXPECT_ROOT`
**and** `config.rootpath == $R10_EXPECT_ROOT`.

**Guard falsified twice, before any measurement:**

```
$ cd /tmp/r10/base && PYTHONPATH=/tmp/r10/plug R10_EXPECT_ROOT=/tmp/r10/base \
    python -m pytest -p guardroot -q tests/test_exports.py -n0
INTERNALERROR> SystemExit: GUARD FAILED: powerpetdoor.__file__=
  '/home/prez/src/pypowerpetdoor/src/powerpetdoor/__init__.py' is not under '/tmp/r10/base'

$ PYTHONPATH=/tmp/r10/plug:/tmp/r10/base/src:/tmp/r10/base R10_EXPECT_ROOT=/tmp/r10/NOPE \
    python -m pytest -p guardroot -q tests/test_exports.py
INTERNALERROR> SystemExit: GUARD FAILED: powerpetdoor.__file__=
  '/tmp/r10/base/src/powerpetdoor/__init__.py' is not under '/tmp/r10/NOPE'
```

The second run also confirms the positive case: under the real (xdist) invocation
the import resolves to the copy.

**Null control** — unmodified copy, full suite: `2869 passed in 35.97s` (rc 0).
Re-run at the head of the serial batch: `2869 passed in 36.53s`.

**Control mutations (must fail).**

| control | result |
|---|---|
| `sanitize.py`: `len(value) > limit` → `>=` | `1 failed, 2868 passed` — `test_the_truncation_boundary_is_exact` |
| `schedule.py`: rename `validate_schedule_entry` | collection error, rc 2 |

**Subprocess reach.** `TestTheRealBinaryUnderSIGINT` spawns a real
`python -m powerpetdoor.simulator`. Mutation `R1` (`sys.exit(130)` → `sys.exit(0)`,
reachable *only* through that subprocess) was killed by
`test_sigint_mid_run_exits_130_and_claims_no_verdict`, so the harness's PYTHONPATH
reaches the child process too.

**Contention caveat, disclosed.** One early batch was run 2-way concurrent
*alongside* a 4-way sweep (24 xdist workers on 16 cores). The null control failed
there — `test_sigint_mid_run_exits_130_and_claims_no_verdict` — so that batch was
discarded and re-run serially. In isolation the same test passes in 0.56 s
(3/3 runs) even under a 4-way sweep; the flake needed ~6x oversubscription that
CI does not have. **I am not reporting it as a finding — it was my harness's
fault, not the suite's.** All reported survivals come from runs whose null
control was green.

**What was mutated.** 223 mutations, plus controls.

| batch | what | runs | survivors |
|---|---|---|---|
| 1 | CI/tooling configuration (`.github/`, `.gitea/`) | 8 | 8 |
| 2 | AST sweep of the 7 never-swept `simulator/commands/*` modules (all `cmp`/`bool`/`int` sites) + 40 seeded string-literal mutations across all 12 command modules | 151 | **0** |
| 3 | `pyproject.toml` instrumentation, round-9 machinery, shipped YAML scripts | 24 | 8 + 2 |
| 4 | sensor-notification gating (`R12`/`R13`) | 2 | 1 |
| 5 | `const.py` wire constants, stratified across every category in the file | 38 | **0** |

Every survivor outside batch 1 and the `pyproject.toml` half of batch 3 is either
reported below or shown to be redundant with an existing test.

---

## Summary

| Severity | Count |
|---|---|
| HIGH | 1 |
| MEDIUM | 2 |
| LOW | 3 |

- **H1** — the 100% gate CI actually enforces is one line in `test.yml` that no test reads; deleting the entire step leaves the suite green.
- **M1** — CLAUDE.md's MANDATORY Python version matrix has zero executable enforcement; six independent drifts all pass.
- **M2** — `docs/operation.md`'s Safety-Lock-vs-Disable-Sensor distinction is asserted nowhere, and the shipped engine currently contradicts it.
- **L1** — `filterwarnings = ["error"]` is the one `pytest` ini option with no pin, while its two neighbours have one.
- **L2** — two pins CLAUDE.md calls mandatory (the Gitea workflow SHA, CI's `--ignore=tests/fuzz`) are unguarded.
- **L3** — `test_script_tests_both_conditions` inspects only `set` step *names*, so the shipped command-lockout assertion can be deleted silently.

---

## Findings

### H1 — HIGH: the 100% coverage gate CI actually enforces is a line in `.github/workflows/test.yml` that no test reads — the fifth instance of the class rounds 6–9 fixed four times on the `pyproject.toml` side

**File:** `.github/workflows/test.yml:317-318`

**Reproduction**

Two independent mutations, each run against the full suite in a fresh tar-copy:

```
M1  .github/workflows/test.yml
    - run: coverage report --fail-under=100
    + run: coverage report --fail-under=50
    → SURVIVED :: 2869 passed in 35.09s   (rc 0)

M2  .github/workflows/test.yml — delete the whole step:
    -      - name: Enforce 100% coverage
    -        run: coverage report --fail-under=100
    → SURVIVED :: 2869 passed in 35.52s   (rc 0)
```

That line is the only place in the repository where a non-zero threshold is
enforced. Every other coverage invocation explicitly disables it:

```
$ grep -n 'fail-under' .github/workflows/test.yml
197:  run: pytest --ignore=tests/fuzz --tb=line -q --cov --cov-report=xml --cov-fail-under=0
285:  coverage report --show-missing --fail-under=0
318:  run: coverage report --fail-under=100
```

And the gate is genuinely load-bearing. Three statements of dead code appended to
`src/powerpetdoor/tz_utils.py` in a fresh copy, then each CI invocation in order:

```
### 1. CI unit-tests job:  pytest --ignore=tests/fuzz -q --cov --cov-fail-under=0
2822 passed in 37.93s
   exit=0
### 2. CI combine step:    coverage report --show-missing --fail-under=0
src/powerpetdoor/tz_utils.py    97   3   34   0  96.18%   229-231
TOTAL                         6906   3 2450   0  99.95%
   exit=0
### 3. CI gate step:       coverage report --fail-under=100
TOTAL                         6906   3 2450   0  99.95%
Coverage failure: total of 99.95 is less than fail-under=100.00
   exit=2
```

So: dead code ships, the suite is green, both other coverage steps are green, and
the *only* thing that fails the build is the one line nothing tests.

**Description**

Round 9's refutation established — correctly — that `pyproject.toml`'s
`fail_under = 100` does **not** guard CI, because an explicit `--fail-under` on
the command line overrides the config file. The fix documented that in the test's
own name and docstring:

> `tests/test_gaps_report.py:753-761`
> `def test_the_local_pre_commit_gate_threshold_is_100(self):`
> *"This does **not** guard CI: `.github/workflows/test.yml` runs
> `coverage report --fail-under=100`, and an explicit command-line option
> overrides the config file."*

The conclusion drawn was "so this test is about the local pre-commit gate". The
conclusion **not** drawn was "so the CI gate is now the unguarded one". Rounds 6,
7, 8 and 9 each found a way to shrink the perimeter with the build green; round 9
pinned five config dimensions by value and, in doing so, moved the last
unprotected copy of the gate into a file with no assertions on it at all.

This is not a hypothetical file, either. `tests/test_docs_accuracy.py:1051` already
opens exactly this workflow and asserts step names in it — and its docstring is
*"A declaration nobody checks is how this drifted in the first place."*

```python
def test_ci_asserts_the_built_artifacts(self):
    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "  packaging:" in workflow
    assert "The wheel carries the PEP 561 marker" in workflow
    assert "The sdist ships a suite that actually runs" in workflow
```

The gate step is simply not in that list.

Secondary, in the same two lines: the comment above the step is factually wrong
after the round-9 refutation.

```yaml
# The real gate: pyproject's fail_under=100 applies here, on the
# coverage combined across every Python version in the matrix.
- name: Enforce 100% coverage
  run: coverage report --fail-under=100
```

`pyproject`'s value does *not* apply here; the command-line flag overrides it.
The sentence directly contradicts `tests/test_gaps_report.py:757-759`.

**Recommendation**

Extend the existing workflow-reading test (or add a sibling in the same class) to
pin the gate by value, in the same shape as the packaging assertions:

```python
def test_ci_enforces_the_hundred_percent_gate(self):
    """The threshold pyproject cannot defend: `--fail-under` on the command
    line overrides the config file, so this line *is* the gate (round-9
    refutation). Deleting the step or lowering the number left all 2869
    tests green (round-10 test-fanatic H1)."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "coverage report --fail-under=100" in workflow
    assert "- name: Enforce 100% coverage" in workflow
```

and correct the comment at `test.yml:315-316` to say that the command-line
`--fail-under=100` is what applies, overriding `pyproject.toml`.

---

### M1 — MEDIUM: the "Version Matrix Maintenance (MANDATORY)" contract has no executable enforcement anywhere — six independent drifts all pass

**Files:** `.github/workflows/test.yml:23,188`, `.github/workflows/release.yml:17,45`,
`pyproject.toml:18,30,186,201`, `README.md:33`

**Reproduction**

Six mutations, each the full suite in a fresh tar-copy:

```
M3  test.yml:188     ["3.11","3.12","3.13","3.14"] → ["3.12","3.13","3.14"]   SURVIVED  2869 passed
M4  release.yml:17   ["3.11","3.12","3.13","3.14"] → ["3.12","3.13","3.14"]   SURVIVED  2869 passed
M5  test.yml:23      REFERENCE_PYTHON: "3.14" → "3.12"                        SURVIVED  2869 passed
M8  release.yml:45   publish pin  "3.14" → "3.11"                             SURVIVED  2869 passed
I3  pyproject:18     requires-python = ">=3.11" → ">=3.12"                    SURVIVED  2869 passed
I4  pyproject:30     delete "Programming Language :: Python :: 3.11"          SURVIVED  2869 passed
I5  pyproject:186    ruff  target-version = "py311" → "py313"                 SURVIVED  2869 passed
I6  pyproject:201    mypy  python_version = "3.11" → "3.13"                   SURVIVED  2869 passed
```

Eight for eight. Nothing in `tests/` reads any of them:

```
$ grep -rn '3\.14\|classifiers\|requires-python' tests/ --include='*.py'
tests/conftest.py:35:    deterministically at session end. On Python 3.14+ the implicit creation
```

(the single hit is prose in a docstring).

**Description**

`.claude/CLAUDE.md` § *Version Matrix Maintenance (MANDATORY)* states the contract
in a table:

> **When committing code, ensure all Python version references are current.**
> The project supports a declared matrix of CPython versions (currently
> **3.11–3.14**). All version references below must match that matrix exactly.
>
> | `.github/workflows/test.yml` | `REFERENCE_PYTHON` env var and `matrix.python-version` arrays |
> | `.github/workflows/release.yml` | `matrix.python-version` array and the publish job's Python pin |
> | `pyproject.toml` | `requires-python`, `Programming Language :: Python :: 3.x` classifiers |
> | `pyproject.toml` | `target-version` (ruff), `python_version` (mypy) — oldest supported Python |

Every cell in that table is a machine-checkable equality between two files, and
none of them is checked. The failure mode is not cosmetic: dropping 3.11 from
`test.yml`'s matrix while `requires-python = ">=3.11"` and the 3.11 classifier
stay put means the project ships a *tested-on* claim it no longer tests — the
exact class of "advertising something the artifact cannot do" that round 9's
sdist and `py.typed` work was raised for. `README.md:33` ("Requires Python
3.11-3.14.") is a seventh copy of the same number, also unchecked.

This is the persona's core complaint about a saturated gate: coverage is 100.00%
and says nothing at all about whether the matrix the coverage was *collected on*
matches the matrix the package claims.

**Recommendation**

One test, derived rather than duplicated — the same shape round 9 used for
`GATED_SOURCE_DIRS`. Read `requires-python` and the `Programming Language ::
Python :: 3.x` classifiers from `pyproject.toml`, build the expected matrix from
them, and assert that the two workflow matrices, `REFERENCE_PYTHON` (= max),
the publish pin (= max), ruff `target-version` and mypy `python_version` (= min)
all agree, plus the `README.md` range sentence. Put it next to
`test_the_deadlock_backstop_uses_the_thread_method` in `tests/test_docs_accuracy.py`,
which already does exactly this kind of cross-file pinning with `tomllib`.

---

### M2 — MEDIUM: `docs/operation.md`'s Safety-Lock-vs-Disable-Sensor distinction is asserted nowhere, and the shipped engine currently contradicts it

**Files:** `docs/operation.md:144-154`, `src/powerpetdoor/simulator/engine.py:435-438`

**Reproduction**

The document distinguishes the two settings *by their notification behaviour* and
says so twice:

```
docs/operation.md:144
| Feature | What It Does | Notifications | Use Case |
|---------|--------------|---------------|----------|
| **Disable Sensor** | Turns sensor OFF completely | No detection at all | ... |
| **Safety Lock** | Sensor still detects, but won't open door | Still sends notifications | ... |

docs/operation.md:154
Key difference: A disabled sensor doesn't detect anything. A safety-locked
sensor detects and can notify, but the door won't respond.
```

Against the shipped engine (probe run against the tar-copy, `notify_sensor`
recorded):

```
safety_lock ON (doc: 'Still sends notifications')  trigger_sensor  -> notify_sensor=[]  door=DOOR_CLOSED
outside DISABLED (doc: 'No detection at all')      trigger_sensor  -> notify_sensor=[]  door=DOOR_CLOSED
control: neither                                   trigger_sensor  -> notify_sensor=[('outside', 'on')]  door=DOOR_RISING
```

The two rows are indistinguishable: `trigger_sensor` returns at
`engine.py:435-438` *before* the `self._notify_sensor(sensor, SENSOR_STATE_ON)`
at `engine.py:469`.

And the suite pins neither answer. The decisive mutation — make a blocked sensor
notify anyway:

```
R12  engine.py:435-438
         logger.info("Simulator: %s sensor ignored (%s)", sensor.capitalize(), blocked)
     +   self._notify_sensor(sensor, SENSOR_STATE_ON)
         return
     → SURVIVED :: 2869 passed in 42.97s   (rc 0)
```

The control proves the notification path is otherwise well covered, so the gap is
specific to the *suppressed* case:

```
R13  engine.py:469 — drop the happy-path notify
     → killed :: 4 failed, 2865 passed
        FAILED tests/simulator/test_engine.py::TestSensorGuardsBlockBothEntryPoints::test_trigger_notifies_and_opens_when_closed
        FAILED tests/simulator/test_engine.py::TestSensorGuardsBlockBothEntryPoints::test_raising_notify_callback_does_not_block_door
        FAILED tests/simulator/test_server.py::TestBroadcastPayloads::test_sensor_trigger_broadcasts_bare_notification
        FAILED tests/simulator/test_client_integration.py::TestNotificationEvents::test_sensor_notification_round_trip
```

```
$ grep -rn 'notify_sensor' tests/ --include='*.py' | grep -v notify_sensors_changed
tests/simulator/test_engine.py:517   (retrigger window)
tests/simulator/test_engine.py:786   (happy path)
tests/simulator/test_engine.py:799   (raising callback)
```

No test sets `safety_lock` or `outside = False` and looks at notifications at all.

**Description**

Round 9 wrote the first `docs/operation.md` accuracy suite
(`TestOperationMdSensorGating`) and did it exactly right for the three schedule
sentences and the power sentence — pinning the prose *and* executing it through
both sensor entry points. The "Sensor Enable vs Safety Lock" table two sections
earlier was not included, and it is the one place in that document where a
setting is defined *by* what it does to notifications. That makes it a
behavioural specification with a testable difference and no test, which is
precisely how `activate_sensor`'s schedule gate survived nine rounds.

`R12`'s survival is the important half: the suite is currently compatible with
both answers, so whichever one the project believes, it is not defended.

**Recommendation**

I am **not** proposing a change to the wire protocol, nor to what the library
accepts from the device. The observable here is what the *simulator* emits, and
which way it should go is a device question, not a test question. Do what round 9
did for the command-lockout half of `sensor_open_block_reason`:

1. Add a test to `TestOperationMdSensorGating` that pins the *sentences* at
   `docs/operation.md:146-147,154`, so rewording the spec forces the test to move.
2. Add a parametrized test over `{safety_lock, disabled}` × `{inside, outside}` ×
   both entry points asserting the notification outcome the project decides is
   correct — one assertion, one answer.
3. Flag the disagreement for the real-device check in `tests/TESTING_GAPS.md`
   alongside the existing command-lockout note: if a real safety-locked door does
   emit `SENSOR_OUTDOOR: on`, the engine changes; if it does not, the two doc
   sentences are wrong and should be corrected. Either way the answer becomes
   executable.

---

### L1 — LOW: `filterwarnings = ["error"]` is the one `pytest` ini option with no pin, and a session fixture's entire rationale depends on it

**File:** `pyproject.toml:84`

**Reproduction**

```
I1  pyproject.toml — delete the line `filterwarnings = ["error"]`
    → SURVIVED :: 2869 passed in 35.57s   (rc 0)
```

Its two neighbours in the same table *are* pinned, in two different files:

```python
# tests/test_docs_accuracy.py:559
assert options["timeout"] == 60
assert options["timeout_method"] == "thread"

# tests/test_docs_accuracy.py:1046
assert pyproject["tool"]["pytest"]["ini_options"]["addopts"] == "-n auto"
```

**Description**

`filterwarnings = ["error"]` is what turns a `DeprecationWarning`, a
`ResourceWarning` for an unclosed transport, or an un-awaited-coroutine
`RuntimeWarning` into a build failure. Nothing else in the suite can observe the
setting — which is the exact argument
`test_the_deadlock_backstop_uses_the_thread_method` gives for asserting
`timeout_method` ("Nothing else in the suite can observe this setting, so it is
asserted here rather than left to a comment"). The same argument applies
verbatim, and the same treatment was not applied.

It is also load-bearing for existing machinery: `tests/conftest.py:24-42`'s
`_managed_main_thread_event_loop` exists *because of* it —

> "That loop is eventually garbage collected mid-session and emits an
> unclosed-loop ResourceWarning attributed to an arbitrary test — **a hard
> failure under `filterwarnings = ["error"]`**."

Delete the ini line and that fixture silently becomes decorative.

**Recommendation**

One assertion in `test_the_deadlock_backstop_uses_the_thread_method` (or a
sibling), with the rationale in the name:

```python
assert options["filterwarnings"] == ["error"]
```

---

### L2 — LOW: two pins `.claude/CLAUDE.md` declares mandatory are unguarded

**Files:** `.gitea/workflows/sync-wiki.yml:8`, `.github/workflows/test.yml:197`

**Reproduction**

```
M6  .gitea/workflows/sync-wiki.yml:8
    - uses: neuromancy/workflows/.gitea/workflows/sync-github-wiki.yml@5d9eb7fb...  # v1.1.0
    + uses: neuromancy/workflows/.gitea/workflows/sync-github-wiki.yml@main
    → SURVIVED :: 2869 passed in 35.58s   (rc 0)

M7  .github/workflows/test.yml:197
    - run: pytest --ignore=tests/fuzz --tb=line -q --cov ...
    + run: pytest --tb=line -q --cov ...
    → SURVIVED :: 2869 passed in 35.73s   (rc 0)
```

**Description**

Both are invariants CLAUDE.md states explicitly, and both are one edit away from
silently gone.

*The Gitea SHA.* CLAUDE.md § *Manually tracked pins (MANDATORY)*:

> | `neuromancy/workflows/.gitea/workflows/sync-github-wiki.yml@<sha>` |
> `.gitea/workflows/sync-wiki.yml` | Dependabot has no Gitea support. This is
> also the **only** `uses:` in the repo that receives a secret, so its SHA pin
> matters more than the rest |

A pin that automation cannot see and no test asserts is a pin maintained by
memory. A one-token edit to `@main` hands `secrets.GH_PAT` to a mutable ref, and
2869 tests stay green.

*The fuzz exclusion.* CLAUDE.md's pre-commit checklist:

> `uv run pytest --ignore=tests/fuzz --cov` **also** reaches 100% — this is what
> CI's unit matrix runs, so the deterministic suite must never lean on randomized
> fuzz coverage to pass the gate.

The sentence is true only because of that flag on `test.yml:197`. Remove it and
the combined coverage the gate reads includes hypothesis-driven lines, so the
deterministic suite may quietly stop reaching 100% without anyone finding out.
`tests/test_schedule.py:986` refers to this invocation as "exactly what CI's unit
matrix runs" — a claim about a file nothing reads.

**Recommendation**

Fold both into the workflow-pinning test suggested in H1 — they are three
assertions in the same place:

```python
assert "sync-github-wiki.yml@" in gitea and re.search(r"@[0-9a-f]{40}\b", gitea)
assert "pytest --ignore=tests/fuzz" in workflow
```

The SHA assertion should check the *shape* (40 hex) rather than the literal
value, so Dependabot-style bumps do not fight the test while a switch to a
mutable ref still fails.

---

### L3 — LOW: `test_script_tests_both_conditions` inspects only `set` step names, so the shipped command-lockout assertion can be deleted with the suite green

**File:** `tests/simulator/scripts/test_power_lockout_test.py:27-34`

**Reproduction**

```
Y3  src/powerpetdoor/simulator/scripts/power_lockout_test.yaml
    delete the trigger + assertion under command lockout, keeping the `set` step:
    -  - action: trigger_sensor
    -    sensor: inside
    -  - action: assert
    -    condition: door_status
    -    equals: DOOR_CLOSED
    → SURVIVED :: 2869 passed in 35.92s   (rc 0)
```

The three tests that should have noticed all still pass: `test_script_tests_both_conditions`
looks only at `set` step *names* (`"cmd_lockout" in names_set`), which the
mutation preserves; `test_script_passes_without_door_motion` asserts
`total_open_cycles == 0`, which is still true when nothing triggers; and
`test_no_status_broadcasts` asserts an empty status sequence, likewise.

The sibling scripts show the shape that works — both of these mutations were
caught:

```
Y4  safety_lock_test.yaml: delete the outside trigger + assertion  → killed (1 failed)
      FAILED tests/simulator/scripts/test_safety_lock_test.py::TestSafetyLockTest::test_script_tests_both_sensors
Y5  obstruction_test.yaml: delete the `set autoretract on` step     → killed (1 failed)
      FAILED tests/simulator/scripts/test_obstruction_test.py::TestObstructionTest::test_script_enables_autoretract
```

and `test_obstruction_test.py::test_script_obstructs_during_close` goes further
still, asserting the *position* of the `obstruction` action relative to its
preceding `wait_for` and that wait's `condition`.

**Description**

The behaviour itself is not at risk — the engine-level command-lockout gate is
strongly pinned (mutation `R5`, dropping `if state.cmd_lockout: return "command
lockout"`, was killed by **8** tests). But round 9's `sensor_open_block_reason`
docstring cites this YAML file as one of the two authorities for a behaviour it
explicitly flags as *not* settled by `docs/operation.md`:

> "the two are made consistent on `trigger_sensor`'s answer, which is the
> behaviour `scripts/power_lockout_test.yaml` and `test_cmd_lockout_ignores_trigger`
> have asserted since round 1."

A cited authority whose assertion can be deleted without a failure is weaker than
that sentence reads. `src/powerpetdoor/simulator/scripts/*.yaml` is a gated
location in CLAUDE.md's own test-location table, and coverage cannot see YAML at
all, so a test is the only instrument available.

**Recommendation**

Make `test_script_tests_both_conditions` assert the step *structure*, the way
`test_script_obstructs_during_close` already does in the same directory: for each
of `power` and `cmd_lockout`, assert that the `set … on/off` step is followed by a
`trigger_sensor` and then an `assert door_status == DOOR_CLOSED`.

---

## Round 9 Fix Verification

Every round-9 fix I could reach by mutation is genuinely pinned. All full-suite
runs, fresh tar-copy each, guard active, null control green.

| round-9 fix | mutation | result |
|---|---|---|
| F-H1: SIGINT exits 130, no verdict | `sys.exit(130)` → `sys.exit(0)` | **killed** — 2 failed, incl. the real-binary `test_sigint_mid_run_exits_130_and_claims_no_verdict` |
| F-H1: `--oneshot` with no verdict exits 1 | restore `if args.oneshot and result is not None:` | **killed** — `test_oneshot_without_a_verdict_exits_one`, `test_script_only_flags_accepted_with_script` |
| F-H1: cancellation re-raised | delete the `raise` after `interrupted = True` | **killed** — all 3 `test_a_cancelled_*` tests |
| F-H1: interrupted banner counts completed scripts | `{completed}` → `{run_count}` | **killed** — 3 failed |
| B-F1: both re-arm and `_update_flow()` in the `finally` | move `_schedule_pump()` back into the `try` (the round-8 form) | **killed** — `test_a_raising_dispatch_above_pause_at_still_drains` |
| T-M2: one shared sensor predicate | delete the `cmd_lockout` gate from `sensor_open_block_reason` | **killed** — 8 failed |
| T-M2: `activate_sensor`'s decisive second operand | `active = …` → `active = True` | **killed** — 1 failed |
| F-M3: unknown top-level script key rejected | add `"stpes"` to `SCRIPT_TOP_LEVEL_KEYS` | **killed** — 5 failed |
| B-F2: throttles record real frame bytes | `_recorded_size` ignores `frame_size` | **killed** — 4 failed |
| F-L2: history falls back to `InMemoryHistory` | restore `FileHistory` on the unusable path | **killed** — 2 failed |
| F-T1: `ctl --timeout` must be > 0 | `<= 0` → `< 0` | **killed** — 1 failed |
| T-H1: coverage-config values | `branch`, `source`, `omit`, `exclude_lines`, `partial_branches` | verified by reading + re-derivation; the pyproject side is now asserted by value and `GATED_SOURCE_DIRS` is derived from `coverage.run.source` (`tests/test_gaps_report.py:702`). **The gate itself moved — see H1.** |
| L1/L4: bind-time argument validation | covered by `TestBindTimeArgumentsFailAsArguments` incl. both range endpoints via `cli.MIN_PORT`/`cli.MAX_PORT` and the literal `"port must be 0-65535"` message | verified by reading; `M1`/`M2` above are unrelated |

The harness note round 9 fed back — an unescaped surrogate killing an xdist
worker so a mutation surfaced as `INTERNALERROR` rather than named failures — is
correctly closed: `tests/test_sanitize.py` now compares through `ascii()` at every
surrogate site, and my surrogate-adjacent control mutation produced a *named*
failure, not an internal error.

**One correction to the round-9 report's own framing, not a finding:** the
`fail_under` sub-claim was refuted for the right reason, but the conclusion
stopped one step short. See H1.

---

## Areas Reviewed With No Findings

### The seven never-mutation-swept `simulator/commands` modules — 151 mutations, 0 survivors

Round 9's breadth sweep named `commands/base.py`, `door.py`, `history.py`,
`schedules.py` and `settings.py`; it did not touch `info.py`, `notifications.py`,
`buttons.py`, `control.py`, `simulation.py`, `scripts.py` or `handler.py`. I swept
**every** `cmp`, `bool` and `int` site in those seven (111 mutations) plus a
seeded random sample of 40 string-literal mutations across all twelve command
modules — `random.Random(20260823)`, each literal mutated by appending `ZZQQ`
inside the quotes.

**151 run, 0 survived.** Every comparison flip, every `and`/`or` swap, every
`n → n+1`, and every sampled user-visible string — command names, help text,
column headers, `CommandResult` messages, even `"ON"` and `""` — was caught,
usually by 1-3 named tests. Examples:

```
killed  info-bool5-L99        ' or '->' and '   :: 1 failed, 1661 passed
killed  handler-cmp35-L253    ' not in '->' in ' :: 3 failed, 383 passed
killed  settings-str35-L241   '"Toggle or set battery presence"'->'…ZZQQ"'  :: 1 failed
killed  schedules-str52-L78   '"ON"'->'"ONZZQQ"'  :: 1 failed, 1661 passed
killed  base-str31-L355       '""'->'"ZZQQ"'      :: 2 failed, 393 passed
killed  door-str36-L63        '"Duration in seconds (0 = toggle)"'->'…ZZQQ"'  :: 1 failed
```

This is the strongest result I have measured on this suite in ten rounds. The
persona's specific complaint — "not just testing error codes, but also what the
text of errors are" — is fully satisfied across the simulator's command surface.

### Survivors proven harmless rather than reported

I do not report a survivor I cannot show is observable.

- **`full_test_suite.yaml:79-80`, `seconds: 1.5` → `0.01`** — SURVIVED
  (`2869 passed`), while the identical mutation in `pet_presence_test.yaml`
  is **killed** by `test_script_waits_past_hold_time_with_pet_present`
  (`waits[0] > hold_time`). The asymmetry is real but the property is not lost:
  "KEEPUP does not auto-close" is separately and directly pinned by
  `tests/simulator/test_engine.py:359-365` ("open(hold=True) ends in KEEPUP with
  the sequence task finished") and `tests/simulator/test_server.py:423-432`
  ("KEEPUP is terminal: the sequence task has finished and the door stays"). The
  YAML wait is redundant defence, so shortening it removes no unique coverage.
  Worth a one-line docstring correction only: `test_pet_presence_test.py:39`
  claims "This is the one intentional `wait` in the built-in scripts" — there are
  two (`grep -rn -A1 'action: wait$' src/powerpetdoor/simulator/scripts/`).
- **`pyproject.toml`: `[tool.coverage.run] parallel = true` → `false`** — SURVIVED.
  These are the two coverage-config dimensions round 9's
  `TestTheCoverageGateIsWhatItClaims` does not assert. I did **not** demonstrate
  an impact for either, so I am not claiming one; noted here only so the next
  round knows they were measured and left unproven.
- **`pyproject.toml`: `[tool.coverage.paths]` deleted entirely** — SURVIVED. Same
  status: all four matrix jobs run from an identically-shaped checkout path, so I
  could not construct a case where the remapping changes the combined result.
  Unproven, not reported.
- **`pyproject.toml`: `requires = ["setuptools==84.0.0", …]` → `>=`** — SURVIVED.
  The exact pin has a stated supply-chain rationale in a `pyproject.toml` comment
  ("a hijacked setuptools/wheel release would be executing in the job that signs
  and publishes the artifact") and no test. I am listing it here rather than as a
  finding because it is a security-posture pin rather than a testing-instrument
  one, and the security persona is the right owner; it is one more assertion in
  the H1/L2 workflow test if the project wants it.

### Round-9 machinery reviewed by reading, with no findings

- **`TestTheCoverageGateIsWhatItClaims` / `TestTheCoverageConfigDoesNotExcludeProse`**
  (`tests/test_gaps_report.py:705-1009`) — the literal 7-element `exclude_lines`
  assertion, the anchored `partial_branches` assertion, both falsifiability twins
  (the six bare round-8 phrases and coverage's own bare branch default), the
  exclusive-span test at `:932`, the real-pragma parametrization at `:915`, and
  `GATED_SOURCE_DIRS` derived from `coverage.run.source`. This is correct and, on
  the `pyproject.toml` side, complete.
- **`TestOperationMdSensorGating`** — nine gate×sensor cases × two entry points,
  each with a named control (`test_the_control_an_unblocked_sensor_opens_the_door`),
  plus the five reason strings pinned by value
  (`"power OFF"`, `"command lockout"`, `"disabled"`, `"safety lock"`,
  `"outside schedule"`). Correct; the gap is the *notification* dimension (M2),
  not this one.
- **`TestTheRealBinaryUnderSIGINT`** — marker-driven rather than sleep-and-hope
  (it reads stdout until `"Step 2"` appears, then signals), with a real control
  (`test_an_uninterrupted_run_still_passes_and_exits_zero`). It is the only test
  in the suite that can fail for the reason it exists. 0.56 s in isolation.
- **`TestTheReportedByteTotalsAreTheBytesTheDeviceSent`** — five parametrized
  cases including the whitespace-padded frames and a control from the sibling
  bad-frame site that reports the same padded frame correctly. Specific.
- **`TestTheRejectionLogSaysWhatWasExpected`** — asserts the full rendered message
  by equality, including the `"int of magnitude <= 1.79769e+308"` spelling, and
  pins all three `_keep_*` spellings. Exactly the "error text is part of the
  contract" standard.
- **`TestTheSourceDistributionShipsWhatItClaims`** — `test_every_test_directory_is_covered_by_the_graft`
  derives the package set from `rglob("__init__.py")` rather than hard-coding it,
  so a new test package cannot silently escape the graft.

### Test-suite hygiene

- No `assert True`, no tautologies, no `assert x in (a, b)` over contradictory
  outcomes anywhere in the round-9 additions. A scan of the diff for weak
  assertion shapes returned exactly one hit, `assert len(found) >= 2` at
  `tests/test_gaps_report.py:887` — and that test additionally asserts the file
  set and the pattern list by equality, so it is specific enough; the inequality
  is deliberate (the count is a property of prose in a file that may gain a line).
- 10 `skipif` markers, all gated on optional extras (`YAML_AVAILABLE`,
  `PROMPT_TOOLKIT_AVAILABLE`) which `[project.optional-dependencies] dev`
  declares and CI installs with `--all-extras`. A silent skip would drop coverage
  below 100% and the gate would catch it — *provided* the gate runs, which is H1.
- `tests/simulator/wire.py`'s `WireCapture` uses the production `FrameScanner`
  and does not swallow `JSONDecodeError`; `MessageCapture.wait_for_status_sequence`
  swallows only `TimeoutError` and returns the captured sequence so the caller's
  equality assertion reports the mismatch. Correct.
- `tests/conftest.py`'s `assert_schedule_wire_types` still pins both directions
  separately with the `True == 1` / `1 == True` traps closed. **I did not propose,
  and do not propose, any change to the wire shape or to what is accepted from
  the device**; the deliberate `enabled` divergence between the two directions is
  documented and correct.

### `const.py` wire constants — 38 mutations, 0 survivors

`const.py` had never been mutation-swept in nine rounds, and it is the one file
where a mutation is invisible by construction: client and simulator both read the
same symbol, so changing a value keeps the two sides agreeing with each other
while both drift away from the real device. The question is whether anything pins
a wire spelling **by literal**.

A stratified sample of 38 string constants — one or more from every category in
the file (message types, envelope fields, direction values, success spellings,
protocol field names, day/time sub-fields, door states, commands, notification
events, sensor state, remote/reset fields) — each mutated by appending `ZZQQ`
inside the quotes, each run against the full suite in a fresh tar-copy.

**38 run, 0 survived.** Every one was caught, by 1–4 named tests:

```
killed  const.py:16   COMMAND               = 'cmd'            :: 2 failed, 1194 passed
killed  const.py:24   FIELD_MSG_ID_RESPONSE = 'msgID'          :: 3 failed, 1778 passed
killed  const.py:29   PHONE_TO_DOOR         = 'p2d'            :: 2 failed, 2047 passed
killed  const.py:43   FIELD_AUTO            = 'timersEnabled'  :: 1 failed, 2182 passed
killed  const.py:46   FIELD_AUTORETRACT     = 'doorOptions'    :: 1 failed, 2182 passed
killed  const.py:56   FIELD_DAYSOFWEEK      = 'daysOfWeek'     :: 4 failed,  902 passed
killed  const.py:79   FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS     :: 2 failed, 1951 passed
killed  const.py:89   DOOR_STATE_KEEPUP     = 'DOOR_KEEPUP'    :: 3 failed, 1876 passed
killed  const.py:126  CMD_CHECK_RESET_REASON                   :: 1 failed, 2182 passed
killed  const.py:151  FIELD_SENSOR_STATE    = 'sensorState'    :: 2 failed, 1834 passed
killed  const.py:160  FIELD_RESET_REASON    = 'resetReason'    :: 2 failed, 1856 passed
== SURVIVORS: []
```

Note the shape of the kills: the ones that fail a *single* test
(`FIELD_AUTO`, `FIELD_AUTORETRACT`, `CMD_CHECK_RESET_REASON`) are caught by the
`docs/protocol.md` cross-check suite rather than by round-trip tests — i.e. the
literal is pinned against the reverse-engineered protocol record, which is the
only authority available short of a real device. That is exactly the right
instrument, and it means the wire **shape** is genuinely defended and not merely
self-consistent. **I did not propose, and do not propose, any change to it.**
