# Test Fanatic Analysis — Round 9

Commit audited: `145cf05` ("Round 8 fixes (refuter-approved list)"), working tree clean.

## Harness (established and falsified before any finding)

| Step | Command / result |
|---|---|
| Tree copy | `tar --exclude=.git … -cf /tmp/r9/base.tar .` then extracted per run (whole tree, no file enumeration) |
| Forced import root | `PYTHONPATH=$W/src:/tmp/r9` (the editable `.pth` in `.venv` is a plain path entry, so `PYTHONPATH` wins) |
| Guard plugin | `/tmp/r9/guard_plugin.py` asserts `powerpetdoor.__file__` is under `$R9_EXPECT_ROOT` in `pytest_configure` |
| **Guard falsified** | With `PYTHONPATH=/tmp/r9` (no work `src`): `INTERNALERROR> SystemExit: R9 GUARD FAIL: powerpetdoor.__file__=/home/prez/src/pypowerpetdoor/src/powerpetdoor/__init__.py not under /tmp/r9/work` — the guard *can* fail |
| Baseline | `2725 passed in 44.29s`, `R9 GUARD OK: /tmp/r9/work/src/powerpetdoor/__init__.py` |
| **Control mutation** (must fail) | `door.py`: `centiseconds / 100.0` → `/ 1000.0` → `15 failed, 2710 passed` ✅ |
| **Null control** (must pass) | comment-only insertion in `door.py` → `2725 passed` ✅ |
| Second null control | `e01`–`e05` are pyproject-only edits that leave `src/` byte-identical; all `2725 passed`, confirming the runner is not spuriously failing |

**110 mutants executed against the full 2725-test suite** (27 hand-targeted at round-8
machinery, 40 AST-generated operator mutations sampled from 487 sites, 6 targeted at
`activate_sensor`, 25 AST-generated in the library core, 7 targeted at framing/DSL
constants, 5 at the coverage config), **plus 5 of those re-run under the real
`pytest --cov` gate**. 94 died. 16 survived; 6 of the survivors are proven equivalent
and are listed as such, not as findings.

Both CI invocations independently re-measured in a clean copy:
`pytest --cov` → `6775 stmts / 2410 branches, 100.00%, 2725 passed`;
`pytest --ignore=tests/fuzz --cov` → `6775 / 2410, 100.00%, 2678 passed`.

## Summary

| Severity | Count |
|---|---|
| High | 1 |
| Medium | 3 |
| Low | 3 |
| **Total** | **7** |

- **H1** — The coverage configuration itself has no test: `branch`, `fail_under`,
  `source` and `omit` can each be silently weakened and `pytest --cov` still reports
  100.00%.
- **M1** — Coverage's *branch* exclusion regex (`partial_branches`) is unconfigured,
  unanchored, undisclosed, and invisible to round 8's prose sweep — the fourth
  instance of the class the last three rounds fixed.
- **M2** — `DoorMotionEngine.activate_sensor`'s entire gating guard is untested: every
  operand of both compound conditions can be deleted with the suite green.
- **M3** — `_keep_int`'s `maximum` guard is pinned only on its positive side; the
  negative half is what stops a `-10**400` from raising `OverflowError`.
- **L1** — `FrameDispatcher._pump`'s `max_inflight` cap is unpinned on the re-entrant
  path (peak `inflight` reaches 5 with `max_inflight=4`, suite green).
- **L2** — The `_log_rejected` "expected" text for the bounded case is unasserted.
- **L3** — Two boundaries in the new prose-sweep tooling are unpinned (`st_size` in the
  description-cache key; the exclusive span end in `find_prose_exclusions`).

---

## Findings

### H1 — HIGH: the coverage configuration is the one piece of test infrastructure with no test; four independent ways to silently shrink the gate all pass at 100.00%

**File:** `pyproject.toml:105-147` (`[tool.coverage.run]` / `[tool.coverage.report]`),
guarded — partially — by `tests/test_gaps_report.py:696-719`

**Reproduction**

Five single-line edits to `pyproject.toml`. `src/` and `tests/` are byte-identical in
every case, so these double as null controls for the runner. Each was run against the
full suite *and* against the real CI gate (`pytest --cov`, which is what
`.github/workflows/test.yml` runs):

```
e01  omit += "*/tz_utils.py"
e02  source = ["src/powerpetdoor"]        (drops "scripts")
e03  fail_under = 100 -> 50
e04  branch = true -> false
e05  exclude_lines += "^\\s*def _keep_int"
```

Plain suite (`-n 3`, 4 slots in parallel):

```
e01      2725 passed in 85.39s
e02      2725 passed in 85.35s
e03      2725 passed in 85.32s
e04      2725 passed in 85.34s
e05      2725 passed in 85.28s
```

Under the real gate (`pytest --cov`):

```
--- e01
TOTAL                                                   6682      0   2378      0  100.00%
Required test coverage of 100.0% reached. Total coverage: 100.00%
2725 passed in 104.17s
--- e02
TOTAL                                                   6465      0   2302      0  100.00%
Required test coverage of 100.0% reached. Total coverage: 100.00%
2725 passed in 103.60s
--- e03
TOTAL                                                   6775      0   2410      0  100.00%
Required test coverage of 50.0% reached. Total coverage: 100.00%
2725 passed in 104.20s
--- e04
TOTAL                                                   6775      0  100.00%
Required test coverage of 100.0% reached. Total coverage: 100.00%
2725 passed in 103.98s
--- e05
TOTAL                                                   6764      0   2402      0  100.00%
Required test coverage of 100.0% reached. Total coverage: 100.00%
2725 passed in 104.18s
```

Baseline for comparison: `6775 stmts / 2410 branches`.

**Description**

Every one of these is a green build:

- **e01** removes 93 statements and 32 branches from the gate by adding one glob to
  `omit`. Nothing asserts what `omit` contains. `tests/test_gaps_report.py:668` asserts
  only `assert omit, "coverage.run.omit went missing from pyproject.toml"` — i.e. that
  it is *non-empty*, which an attacker-shaped or merely careless edit trivially
  satisfies.
- **e02** removes 310 statements and 108 branches by deleting `"scripts"` from
  `source`. The config's own comment at `pyproject.toml:105-107` says *"`scripts/` is in
  scope too: generate_gaps_report.py produces the artifact this project uses to report
  its own testing gaps, so it must participate in the 100% gate like everything else."*
  That sentence is currently an aspiration with nothing enforcing it. Note the
  asymmetry: `tests/test_gaps_report.py:696` hard-codes
  `GATED_SOURCE_DIRS = (REPO_ROOT / "src" / "powerpetdoor", REPO_ROOT / "scripts")`,
  so the prose sweep would keep scanning a directory the gate no longer measures — the
  two would drift silently in the direction that looks safe.
- **e03** turns the gate off in all but name. The report banner changes to `Required
  test coverage of 50.0%` and the build stays green because coverage is currently 100%;
  the weakening only surfaces the day someone actually needs it not to.
- **e04** is the worst of the five: `branch = false` stops measuring **all 2410 branch
  destinations** and the report line loses its Branch/BrPart columns entirely, while
  still printing `100.00%`. Every "100% branch coverage" claim in `CLAUDE.md`,
  `README.md` and `tests/TESTING_GAPS.md` becomes false with a one-word edit that no
  test and no reviewer diff-signal catches.
- **e05** shows the round-8 sweep's blind spot from the other side. `find_prose_exclusions`
  reports a pattern only when the match lands *inside a string literal*; a new
  `exclude_lines` entry that matches real **code** — which is what an exclusion pattern
  is supposed to do — is by construction invisible to it. `^\s*def _keep_int` excludes
  the whole function from the gate and the sweep test passes, because the match is on
  code, not prose.

This is the same class the last three rounds chased, one level up: rounds 6-8 fixed
*which lines* the configured patterns match; nobody pinned *what the configuration is*.
The prose sweep is a good test of one failure mode and it is currently the only test
the configuration has.

**Recommendation**

Add a config-integrity test alongside `TestTheCoverageConfigDoesNotExcludeProse` that
reads `pyproject.toml` and asserts the *values*, not their non-emptiness:

```python
class TestTheCoverageGateIsWhatItClaims:
    def test_branch_coverage_is_on(self):     # e04
    def test_fail_under_is_100(self):         # e03
    def test_source_is_exactly_the_two_gated_roots(self):   # e02
    def test_omit_is_exactly_init_and_main(self):           # e01
    def test_exclude_lines_is_exactly_the_seven_vetted_patterns(self):  # e05
```

The last one is the important one: make the seven patterns a literal in the test, so
adding an eighth is a two-file diff with a rationale rather than a one-line change that
quietly removes a function. Derive `GATED_SOURCE_DIRS` in `tests/test_gaps_report.py`
from `coverage.run.source` rather than hard-coding it, so e02's drift is impossible.
`generate_gaps_report.py` should render `branch`, `fail_under` and `source` into
`TESTING_GAPS.md` next to the existing `omit`/`exclude_lines` bullets — the artifact
whose stated job is disclosing the perimeter currently discloses two of its six
dimensions.

---

### M1 — MEDIUM: the *branch* exclusion regex is unconfigured, unanchored, undisclosed, and structurally invisible to round 8's sweep — the fourth instance of the class

**File:** `pyproject.toml:113-147` (`partial_branches` absent from `[tool.coverage.report]`);
guard at `tests/test_gaps_report.py:715-724`; disclosure at
`scripts/generate_gaps_report.py:70-80` (`render_automatic_exclusions`)

**Reproduction**

Coverage's defaults, read from the installed library:

```
$ .venv/bin/python -c "from coverage.config import CoverageConfig; c=CoverageConfig(); print(c.partial_list); print(c.partial_always_list)"
['#\\s*(pragma|PRAGMA)[:\\s]?\\s*(no|NO)\\s*(branch|BRANCH)']
['while (True|1|False|0):', 'if (True|1|False|0):']
```

`partial_branches` is not set in `pyproject.toml`, so that bare, unanchored
`re.search` is live. Matched pair, using **this project's exact `exclude_lines`** in a
`.coveragerc` so the effect provably comes from the branch half:

```python
# /tmp/r9/pb/pkg/m.py — two identical functions; only the string differs
def with_prose(items):
    for item in items or ["see # pragma: no branch in the docs"]:
        break
    return 1


def without_prose(items):
    for item in items or ["see nothing special in the docs"]:
        break
    return 1
```

```
$ python -m coverage run runme.py && python -m coverage report -m
Name              Stmts   Miss Branch BrPart   Cover   Missing
--------------------------------------------------------------
pkg/__init__.py       0      0      0      0 100.00%
pkg/m.py              8      0      4      1  91.67%   8->10
--------------------------------------------------------------
TOTAL                 8      0      4      1  91.67%
```

Both loops always `break`, so both `for`→exit arcs are unreached. Only line 8
(`without_prose`) is reported. Line 2's missing arc was silently forgiven because the
phrase appears **inside a string literal** on that line.

The round-8 guard cannot see it — it is called with `exclude_lines` only:

```
$ ... gaps.find_prose_exclusions((Path("/tmp/r9/pb/pkg"),), excl)
[]
$ ... gaps.find_prose_exclusions((Path("/tmp/r9/pb/pkg"),), excl + [r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(branch|BRANCH)"])
[{'file': '/tmp/r9/pb/pkg/m.py', 'line': 2, 'pattern': '#\\s*(pragma|PRAGMA)...',
  'code': 'for item in items or ["see # pragma: no branch in the docs"]:'}]
```

The machinery works perfectly the moment it is handed the branch pattern; it is simply
never handed it (`tests/test_gaps_report.py:716`, `_, patterns = gaps.coverage_config()`
— and `coverage_config()` at `scripts/generate_gaps_report.py:52-66` reads only
`run.omit` and `report.exclude_lines`).

Scanning the real tree with the branch pattern finds two live prose matches today:

```
'#\\s*(pragma|PRAGMA)[:\\s]?\\s*(no|NO)\\s*(branch|BRANCH)': 4 matching lines
    ('src/powerpetdoor/simulator/cli.py', 100, 'if self._handler:  # pragma: no branch (...)')     <- real
    ('src/powerpetdoor/simulator/cli.py', 689, 'if simulator.server and ...  # pragma: no branch') <- real
    ('scripts/generate_gaps_report.py', 550, 'f"are excluded via `# pragma: no cover` or `# pragma: no branch`."')   <- PROSE
    ('scripts/generate_gaps_report.py', 578, 'lines.append("No `# pragma: no cover` or `# pragma: no branch` annotations found.")') <- PROSE
```

**Neither prose line is currently a branch point, so nothing is lost today.** I verified
this rather than assuming it — the measured perimeter is unchanged. This is a latent
hole, not an active one; it becomes active the first time one of those strings ends up
on a line with a conditional, which is exactly how rounds 6, 7 and 8 happened.

**Description**

The project has now hardened the same mechanism three times, and each fix covered
`exclude_lines` only. `partial_branches` is the identical mechanism applied to branch
arcs, in a project whose headline claim is *100% branch coverage of 2410 destinations*,
and it is currently:

1. **unanchored** — coverage's default matches the phrase anywhere on the line,
   including inside a string, which is precisely the defect round 8 anchored away on the
   `exclude_lines` side;
2. **invisible to the guard** — `find_prose_exclusions` is a general-purpose scanner
   that is simply not given these patterns;
3. **undisclosed** — `render_automatic_exclusions()` emits bullets for `omit` and
   `exclude_lines`; `tests/TESTING_GAPS.md` lists the two real `# pragma: no branch`
   *annotations* (via the tokenize scan at `scripts/generate_gaps_report.py:240`) but
   never the *regex* that creates them, nor `partial_always` (`while (True|1|False|0):`,
   which currently forgives 10 `while True:` lines — all legitimate, all undisclosed).

**Recommendation**

Three small changes, all inside existing machinery:

1. Set `partial_branches` explicitly in `[tool.coverage.report]`, anchored the same way
   round 8 anchored the pragma pattern:
   `"#\\s*pragma:\\s*no\\s+branch\\s*($|\\()"`. That single change removes both current
   prose matches (verified: neither line has the phrase followed by end-of-line or `(`).
2. Extend `coverage_config()` to return `partial_branches` too, and feed it to
   `find_prose_exclusions` in `test_no_configured_pattern_matches_prose_in_a_gated_file`.
   Add the falsifiability twin the round-8 tests already model — assert that the *bare*
   coverage default is caught by the sweep, using `generate_gaps_report.py:550` as the
   fixture, matching `test_the_sweep_catches_the_bare_phrases_round_8_replaced`.
3. Render `partial_branches` and coverage's fixed `partial_always` list into the
   "Automatic Exclusions" section of `TESTING_GAPS.md`, so the branch perimeter is
   disclosed alongside the line perimeter.

---

### M2 — MEDIUM: `activate_sensor`'s gating guard is entirely untested — every operand of both compound conditions can be deleted with the suite green, and two of the gates already disagree with its sibling

**File:** `src/powerpetdoor/simulator/engine.py:488-499` (the `should_trigger` block in
`DoorMotionEngine.activate_sensor`); untested despite
`tests/simulator/test_engine.py:622-641` appearing to cover exactly this

**Reproduction**

Seven mutations of the two guard lines, each run against the full suite:

```
a02  L492  should_trigger = state.power and state.inside   ->  ... or ...          2725 passed
b01  L492  -> should_trigger = state.power                                          2725 passed
b02  L492  -> should_trigger = state.inside                                         2725 passed
b03  L495  -> should_trigger = state.power and state.outside                        2725 passed
b04  L495  -> should_trigger = state.power and not state.safety_lock                2725 passed
b05  L495  -> should_trigger = state.outside and not state.safety_lock              2725 passed
b06  L495  -> should_trigger = state.power or state.outside and not state.safety_lock  2725 passed
```

Every operand of both guards is removable. Direct probe (shipped tree vs the `a02` tree,
same script, `PYTHONPATH` forced at each root):

```
SHIPPED:
  activate_sensor: power OFF / inside enabled  -> door_status=DOOR_CLOSED
  trigger_sensor : power OFF / inside enabled  -> door_status=DOOR_CLOSED
  activate_sensor: power ON  / inside disabled -> door_status=DOOR_CLOSED
  trigger_sensor : power ON  / inside disabled -> door_status=DOOR_CLOSED
  activate_sensor: cmd_lockout ON, both enabled -> door_status=DOOR_RISING
  trigger_sensor : cmd_lockout ON, both enabled -> door_status=DOOR_CLOSED
MUTATED (and -> or on engine.py:492):
  activate_sensor: power OFF / inside enabled  -> door_status=DOOR_RISING
  trigger_sensor : power OFF / inside enabled  -> door_status=DOOR_CLOSED
  activate_sensor: power ON  / inside disabled -> door_status=DOOR_RISING
  trigger_sensor : power ON  / inside disabled -> door_status=DOOR_CLOSED
```

And, on the shipped tree, with `auto = True` and a zero-length inside window
(the exact fixture `test_out_of_schedule_trigger_is_ignored` builds):

```
  trigger_sensor  out-of-schedule -> DOOR_CLOSED
  activate_sensor out-of-schedule -> DOOR_RISING
```

**Description**

`DoorMotionEngine` has two sensor entry points with two *different* gates:

- `trigger_sensor` (engine.py:366) has five explicit early returns — power,
  `cmd_lockout`, per-sensor enable, `safety_lock`, `is_sensor_allowed_by_schedule`.
- `activate_sensor` (engine.py:434) re-implements a weaker subset inline as
  `should_trigger = state.power and state.inside` / `state.power and state.outside and
  not state.safety_lock` — no `cmd_lockout`, no schedule.

Every negative test in the suite exercises the first
(`test_disabled_sensor_ignores_trigger`, `test_disabled_outside_sensor_ignores_trigger`,
`test_safety_lock_ignores_outside_trigger`, `test_cmd_lockout_ignores_trigger`,
`test_out_of_schedule_trigger_is_ignored` — all `engine.trigger_sensor(...)`). The
`activate_sensor` tests (`test_engine.py:688-780`) assert only toggle/duration
mechanics and never look at `door_status`. Coverage is 100% because the *positive*
destination runs; CLAUDE.md rule 9 is the exact diagnosis — the guard is one branch
point with two destinations and no operand is ever the decisive one.

`activate_sensor` is not an obscure API. It is what the simulator's user-facing
`inside`/`outside` CLI commands call (`simulator/commands/door.py:45,74`) and what the
script DSL's `inside`/`outside`/`pet_presence` actions call
(`simulator/scripting.py:583,594,601`). So the divergence is reachable from the two
front ends the project ships: a CI script step `- action: inside` opens the door while
command lockout is on and while every schedule window is closed, and
`docs/operation.md` ("Schedule and Sensor Interaction": *"Outside scheduled windows,
sensor triggers are ignored"*) says a real door would not. Which of the two paths is
right is a fidelity question — but **no test asserts either answer**, which is why the
disagreement has survived nine rounds.

Note the related structural gap: `tests/test_docs_accuracy.py` contains exactly two
test classes, both about `docs/protocol.md`'s keepalive section. `docs/operation.md` —
the behavioural specification the simulator exists to be faithful to — has no accuracy
tests at all.

**Recommendation**

Parametrize the existing gating tests over *both* entry points rather than adding a
second copy:

```python
@pytest.mark.parametrize("trigger", [
    pytest.param(lambda e: e.trigger_sensor("inside"), id="trigger_sensor"),
    pytest.param(lambda e: e.activate_sensor("inside", 5.0), id="activate_sensor"),
])
@pytest.mark.parametrize("gate", ["power", "inside", "cmd_lockout", "schedule"])
async def test_the_inside_sensor_gate_blocks_both_entry_points(engine, state, trigger, gate):
```

That makes each operand decisive (rule 9) and forces the `cmd_lockout`/schedule
divergence to be resolved in the diff rather than left implicit. The two guards should
then be refactored into one shared predicate — `CLAUDE.md`'s "two implementations =
refactor" rule applies, and one predicate is one thing to test. Separately, add an
`operation.md` accuracy suite: the sensor-gating table there is a list of assertions
waiting to be written.

---

### M3 — MEDIUM: `_keep_int`'s representability bound is pinned only on its positive side; the negative half is what stops the exact `OverflowError` round 8 fixed

**File:** `src/powerpetdoor/door.py:205`; test at
`tests/test_door.py:2237-2259` (`test_an_unrepresentable_hold_time_is_rejected_at_the_representability_bound`)

**Reproduction**

```
m03  door.py:205  -maximum <= coerced <= maximum   ->   coerced <= maximum
     2725 passed in 76.72s   PYTEST_EXIT=0
```

The dropped half is load-bearing:

```
$ .venv/bin/python  # shipped vs. the mutated predicate, same input
shipped negative bigint -> 2.0                      # rejected, cache kept
mutated -> OverflowError: int too large to convert to float
```

(`huge_neg = -(10**400)`; the shipped `_on_hold_time_update` keeps the cached
`_hold_time`, the mutated one lets it through to `centiseconds / 100.0`.)

The related positive-side mutation is caught, which is what makes the asymmetry a test
gap rather than dead code:

```
m01  door.py:205  -maximum <= coerced <= maximum  ->  -maximum <= coerced < maximum
     1 failed, 2724 passed
```

**Description**

The boundary test asserts `limit` (accepted) and `limit + 1` (rejected) where
`limit = int(sys.float_info.max)`. `-limit` and `-limit - 1` are never asserted. The
parametrized bad-value test at `tests/test_door.py:2204-2230` includes `10**400` but
not `-10**400`. CLAUDE.md rule 8 asks for `limit - 1`, `limit`, `limit + 1`; a
*magnitude* bound has six such points and three are missing — and the missing three are
the ones guarding the exact failure (`OverflowError` out of `value / 100.0`, one
unthrottled traceback per frame with the cache left stale) that round-8 backend L1 /
security L2 was raised for. A device sending a negative arbitrary-precision integer is
no less plausible than a positive one; `docs/protocol.md` is reverse-engineered and
constrains neither.

**Recommendation**

Extend the existing test into a parametrized six-point sweep — the CLAUDE.md rule
already prefers one parametrized test per limit over three separate ones:

```python
@pytest.mark.parametrize(
    ("value", "accepted"),
    [(limit, True), (limit + 1, False), (-limit, True), (-limit - 1, False)],
    ids=["+limit", "+limit+1", "-limit", "-limit-1"],
)
```

and add `-(10**400)` alongside `10**400` in the `test_a_bad_hold_time_keeps_the_cached_value`
parametrization.

---

### L1 — LOW: `FrameDispatcher`'s `max_inflight` cap is asserted only on the first pump; the re-entrant path can exceed it

**File:** `src/powerpetdoor/framing.py:621`; test at
`tests/test_framing.py:973` (`test_concurrency_is_bounded_by_max_inflight`)

**Reproduction**

```
c07  framing.py:621  while budget and self._backlog and self._inflight < self._max_inflight:
                     ->  ... self._inflight <= self._max_inflight:
     2725 passed in 90.70s   PYTEST_EXIT=0
```

The sibling comparison one line down *is* pinned, which localises the gap exactly:

```
d01  framing.py:627  if self._backlog and self._inflight < self._max_inflight:  -> <=
     1 failed, 2462 passed
d02  framing.py:620  budget = self._max_inflight  ->  self._max_inflight + 1
     1 failed, 2451 passed
```

Direct probe (`max_inflight=4`, 50 frames, handlers gated on an `asyncio.Event`, one
released so a done-callback re-enters `_pump`):

```
SHIPPED:
  after first submit: inflight = 4 (max_inflight=4)
  after loop turn:   inflight = 4
  after loop turn:   inflight = 4
  PEAK inflight = 4 -> bound respected: True
MUTATED (framing.py:621 '<' -> '<='):
  after first submit: inflight = 4 (max_inflight=4)
  after loop turn:   inflight = 4
  after loop turn:   inflight = 5
  PEAK inflight = 5 -> bound respected: False
```

**Description**

`test_concurrency_is_bounded_by_max_inflight` submits 50 frames and asserts
`dispatcher.inflight == 4`. That assertion cannot see this comparison: on the *first*
`_pump` the separate `budget = self._max_inflight` counter caps the loop at 4 iterations
regardless of what the `_inflight` comparison says. The comparison only becomes decisive
when `_pump` is re-entered from `_on_dispatched_done` with `_inflight` already at the
cap — which is the steady state of a busy connection, and the only state in which the
concurrency bound actually does any work. `MAX_INFLIGHT_FRAMES` is a DoS bound
(`d03`: changing `64` → `128` is caught, so rule 10 is satisfied for the *value*); its
*enforcement* on the hot path is not.

**Recommendation**

Add one test to the existing `max_inflight` class that measures the peak across
re-entrant pumps rather than after the first submit — release a single handler, then
assert `dispatcher.inflight <= max_inflight` on every loop turn until drained. Name it
for the rationale (`test_the_inflight_cap_still_holds_when_a_done_callback_repumps`) so
the `budget`-vs-`_inflight` distinction is recorded.

---

### L2 — LOW: the rejection log's "expected" text is unasserted, so the bounded case can report a misleading type

**File:** `src/powerpetdoor/door.py:207-209`

**Reproduction**

```
m04  door.py:208
     field_name, value, "int" if maximum is None else f"int of magnitude <= {maximum:g}"
     ->  field_name, value, "int"
     2725 passed in 76.71s   PYTEST_EXIT=0
```

Nothing in the suite asserts the third argument of `_log_rejected` for any field. The
only assertion on this log line anywhere is the substring
`"keeping the cached value"` (`tests/test_door.py:2111` and `:2259`), which is
identical for every rejection reason.

**Description**

`_log_rejected` formats `"Ignoring %s from device for %s (expected %s); keeping the
cached value"`. Under the mutation, a `-10**400` rejected for magnitude logs
`expected int` — for a value that *is* an `int`. That is an actively misleading
diagnostic on the one path an operator would reach for when a firmware variant is
misbehaving, and the ternary that produces the correct text was added deliberately in
round 8. The persona standard is explicit that error *text* is part of the contract, not
just the code path.

**Recommendation**

Extend the two existing `caplog` assertions to match the full rendered message, and add
one case per `expected` spelling (`int`, `int of magnitude <= 1.79769e+308`, `bool`,
`str`) — four small assertions that also pin `sanitize_text(value, MAX_LOGGED_LENGTH)`
being applied to the value.

---

### L3 — LOW: two boundaries in the new round-8 tooling are unpinned

**Files:** `src/powerpetdoor/simulator/scripting.py:1100`;
`scripts/generate_gaps_report.py:223`

**Reproduction**

```
m14  scripting.py:1100  key = (str(path), stat.st_mtime_ns, stat.st_size)
                        ->  key = (str(path), stat.st_mtime_ns, 0)
     2725 passed in 76.04s   PYTEST_EXIT=0

m23  generate_gaps_report.py:223
     if not any(start <= match.start() < end  for start, end in spans[number]):
     ->  if not any(start <= match.start() <= end for start, end in spans[number]):
     2725 passed in 75.77s   PYTEST_EXIT=0
```

`st_size` is observable and deterministically testable (`os.utime` forces an identical
`st_mtime_ns`):

```
first: ('FIRST', None)
same mtime_ns, different size -> ('SECOND-and-longer', None)
sizes: 37 49 mtimes equal: True
```

With `st_size` dropped from the key the second call returns the stale `('FIRST', None)`.

The exclusive span end is observable too — a match starting exactly at a string
literal's `end_col_offset` is *code*, not prose:

```python
# _string_spans -> {2: [(4, 7), (29, 32)]}
TYPE_CHECKING = True
X = "a"if TYPE_CHECKING else "b"
```
```
SHIPPED (exclusive end): []
MUTATED (inclusive end): [{'line': 2, 'pattern': 'if TYPE_CHECKING',
                           'code': 'X = "a"if TYPE_CHECKING else "b"'}]
```

**Description**

Both are correct as written and both are unasserted. The cache key's `st_size` component
is real cache-invalidation logic — filesystems with coarse `mtime` granularity, and
`os.utime`-preserving tooling, are exactly the cases it exists for — and it is the kind
of thing a future "simplify the key" change would delete with the suite green. The
exclusive span end is what separates a prose match from a code match at the boundary;
making it inclusive turns the sweep into a false-positive reporter, and since
`test_no_configured_pattern_matches_prose_in_a_gated_file` asserts `found == []`, that
failure mode is a spurious red build rather than a silent one — still worth one test
each, per CLAUDE.md rule 8.

**Recommendation**

Two tests, both cheap:

- in `tests/simulator/test_scripting.py`, a `test_a_same_mtime_edit_still_reparses`
  using `os.utime(path, ns=(atime_ns, mtime_ns))` to force the collision;
- in `tests/test_gaps_report.py::TestTheCoverageConfigDoesNotExcludeProse`, a
  `test_a_match_beginning_where_a_string_ends_is_code_not_prose` using the
  `X = "a"if TYPE_CHECKING else "b"` fixture above.

---

## Round 8 Fix Verification

Every round-8 fix was re-verified by mutation against the full suite, not by reading.

| Round-8 fix | Mutation | Result |
|---|---|---|
| Widened `except (ValueError, RecursionError)` — client | revert to `except json.JSONDecodeError` | **10 failed** ✅ |
| Widened `except` — simulator twin | revert to `except json.JSONDecodeError` | **9 failed** ✅ |
| `RecursionError` specifically — client | `except ValueError` (drop `RecursionError`) | **7 failed** ✅ |
| `RecursionError` specifically — simulator | `except ValueError` | **6 failed** ✅ (incl. `test_a_legitimate_command_after_a_poisoned_frame_is_still_answered`) |
| `_update_flow()` in a `finally` | restore the pre-fix sequential form verbatim | **1 failed** ✅ |
| `_keep_int(maximum=…)` guard | remove the bound entirely | **3 failed** ✅ |
| `_keep_int` upper boundary | `<= maximum` → `< maximum` | **1 failed** ✅ |
| `_keep_int` *lower* boundary | drop `-maximum <=` | **PASSED** ❌ → M3 |
| `_keep_int` rejection message | drop the ternary | **PASSED** ❌ → L2 |
| `_describe_script` memoisation | remove the cache lookup | **1 failed** ✅ |
| `MAX_DESCRIPTION_CACHE` value (rule 10) | `512` → `1024` | **1 failed** ✅ |
| Cache-clear boundary | `>=` → `>` | **1 failed** ✅ |
| Cache key freshness | `(path, 0, 0)` | **3 failed** ✅ |
| Cache key `st_size` component | `(path, mtime_ns, 0)` | **PASSED** ❌ → L3 |
| `matches_completion_prefix` case-insensitivity | make it case-sensitive | **1 failed** ✅ |
| `matches_completion_prefix` filtering | `return True` | **4 failed** ✅ |
| `ThreadedCompleter` wrapper | unwrap to `SimulatorCompleter()` | **1 failed** ✅ |
| `STEP_ANNOTATION_KEYS` allowance | remove `- STEP_ANNOTATION_KEYS` | **5 failed** ✅ |
| `STEP_ANNOTATION_KEYS` membership | drop `"comment"` | **failed** ✅ |
| Per-action `_ACTION_PARAMS` | revert to the union check | **6 failed** ✅ |
| `_ACTION_PARAMS["wait"]` contents | add a bogus `"duration"` | **1 failed** ✅ |
| `_ACTION_PARAMS["assert"]` contents | add a bogus `"expected"` | **1 failed** ✅ |
| `_ACTION_PARAMS["obstruction"]` (empty set) | add a bogus `"duration"` | **1 failed** ✅ |
| `_other_table_hint` cross-reference | always return `""` | **3 failed** ✅ |
| Anchored `exclude_lines` (the H1 fix itself) | measured, see below | ✅ |
| Prose sweep `__init__`/`__main__` skip | remove the skip | **1 failed** ✅ |
| Prose sweep innermost-statement pick | `min` → `max` | **1 failed** ✅ |
| Prose sweep docstring exemption | `return True` | **2 failed** ✅ |
| Prose sweep span-end exclusivity | `<` → `<=` | **PASSED** ❌ → L3 |
| Raw-bytes fuzz property | (killed by the four `except` mutations above) | ✅ |

**The round-8 anchoring is real and I re-measured it.** Parsing every gated file with
`coverage.parser.PythonParser` per pattern:

```
28 gated files
'#\s*pragma:\s*no\s+cover\s*($|\()' : 2 statements  (ctl.py 657-658, both real annotations)
'^\s*def __repr__'                  : 0
'^\s*raise NotImplementedError'     : 0
'^\s*if TYPE_CHECKING:'             : 33 (13 files, all TYPE_CHECKING import blocks)
'^\s*if __name__ == .__main__.:'    : 6  (3 entry-point guards)
'^\s*@overload\s*$'                 : 4  (client.py overload stubs)
'(^\s*\.\.\.\s*$)|(:\s*\.\.\.\s*$)' : 6
ALL                                 : 47 statements
```

Every one of the 47 is the construct the pattern names. Zero prose-triggered exclusions
remain in the `exclude_lines` half — the round-8 fix did what it claimed, and unlike
round 7's it is not a convention. `find_prose_exclusions` correctly returns `[]` for the
real tree and correctly reports both falsifiability fixtures.

The `omit` list also hides nothing: I read all four omitted files. `src/powerpetdoor/__init__.py`
(284 lines), `simulator/__init__.py`, `simulator/commands/__init__.py` are docstring +
re-export only; `simulator/__main__.py` is an 11-line entry guard.

Both CI invocations independently reproduced at **100.00%, 6775 statements / 2410
branches** (2725 and 2678 tests respectively) in a clean tar-extracted copy with the
import root forced and the guard green.

---

## Areas Reviewed With No Findings

### Equivalent mutants — survivors proven harmless rather than reported

I do not report a survivor I cannot show is observable. Six survivors are equivalent:

- **`scripting.py:1340`** `matches_completion_prefix(subdir.name + "/", prefix)` →
  without the `"/"`. **Provably equivalent**: `script_completer` routes any prefix
  containing `/` or `\` to the `search_dir` branch (scripting.py:1270-1280), so in the
  `else` branch `prefix` never contains `/`. For such a prefix,
  `(s + "/").startswith(p) ⟺ s.startswith(p)` — if `len(p) <= len(s)` both reduce to
  `s[:len(p)] == p`; if `len(p) > len(s)` the only way the first could differ is
  `p == s + "/"`, which requires a `/` in `p`.
- **`generate_gaps_report.py:120`** dropping `ast.JoinedStr` from `_string_spans`.
  Executed on **both** Python 3.11 and 3.13 over f-string fixtures
  (`f"pragma: no cover {y}"`, `f"{y} if TYPE_CHECKING: yes"`): identical results on
  both interpreters, because the inner `Constant` nodes already carry a covering span on
  3.12+ and inherit the `JoinedStr`'s span on 3.11. The branch is defensive, not
  load-bearing. (The docstring's justification — "f-strings tokenize differently across
  the supported interpreters" — describes `tokenize`, which this function does not use.)
- **`generate_gaps_report.py:127`** `assert node.end_lineno is not None and
  node.end_col_offset is not None` → `or`. Both operands are always true for anything
  `ast.parse` produces, as the adjacent comment states.
- **`commands/handler.py:136`** `hasattr(func, "_subcommand_info") and
  hasattr(func, "_parent_path")` → `or`. `@subcommand` sets both attributes together
  (`commands/base.py:577-578`), so "exactly one present" is unconstructible.
- **`schedule.py:524`** `in_end < in_start` → `<=`. At equality the guarded body is
  `in_start, in_end = in_end, in_start` — swapping equal values is the identity.
- **`server.py:287`** `step < 0` → `<=`. `if step == 0: return` at server.py:264
  guarantees `step != 0` at that point.
- **`tz_utils.py:76`** `second_last_newline >= 0` → `> 0`. Index 0 is unreachable:
  `content[:4] != b"TZif"` returns `None` at tz_utils.py:70, so byte 0 is `T`.

### Round-8 machinery with no findings beyond those above

- **The raw-bytes fuzz property** (`tests/fuzz/test_framing_fuzz.py:479-570`) is a real
  oracle, not a smoke test: it asserts no raise, `backlog == 0`, `inflight == 0`,
  `paused is False` on both dispatcher and transport, and — in the chunked variant —
  `len(received) == (1 if _classify(payload) == "decode: ok" else 0)`. All four
  `except`-narrowing mutations were killed by it and by the unit twins. The recipe-based
  draw (straddling `sys.get_int_max_str_digits()` and the recursion depth with an
  explicit boundary `sampled_from`) is the right shape for CLAUDE.md rule 8 under
  hypothesis's small-integer bias.
- **`ThreadedCompleter`, memoised `_describe_script`, `matches_completion_prefix`** —
  all pinned; 6 of 7 mutations killed.
- **Per-action `_ACTION_PARAMS`** — pinned per action, including empty-set actions;
  the union regression is caught (6 failures) and three separate per-action content
  mutations are each caught.
- **`finally`-placed `_update_flow()`** — restoring the pre-fix form verbatim fails.
- **Anchored `exclude_lines`** — re-measured statement-by-statement (above); the sweep
  and its two falsifiability tests behave correctly.

### Breadth sweep — 65 AST-generated operator mutations across the library

Sampled from 487 candidate sites: 40 uniformly at random over all gated files
(seeded, `random.Random(20260822)`) and 25 stratified 3-per-file across `client.py`,
`door.py`, `framing.py`, `schedule.py`, `sanitize.py`, `state.py`, `server.py`,
`protocol.py`, `tz_utils.py`, `commands/*`. **59 killed, 6 survived, and 5 of the 6 are
the proven equivalents listed above** — the sixth is L1. That is a genuinely strong
result for a suite of this size; specifically confirmed killed:

- `sanitize.py:55` — both the `>` boundary and the `and` short-circuit;
- `schedule.py:240,254,573` — the "may be absent" field guards, decisive on the second
  operand (CLAUDE.md rule 9);
- `tz_utils.py:58,70` — the TZif magic check and the path-split branch;
- `state.py:249,265,357` — the schedule-window inclusive start / exclusive end
  (`>=`/`<`/`or`) and the sensor-blocking predicate;
- `server.py:668,677` — the low-battery crossing on both sides;
- `framing.py:282,651` — the scanner index bound and the `pause_at` threshold;
- `commands/base.py:164,193` — the `min_value`/`max_value` validators including the
  `is not None` short-circuit;
- `client.py:942,1800,1831,1906` — the settings-field guards, outstanding-future
  guards, and the PING type check;
- `commands/history.py:180,306`, `commands/schedules.py:32,143`,
  `commands/door.py:46,75`, `commands/settings.py:366`, `ctl.py:143`,
  `prompt_common.py:613,631`, `protocol.py:248,256,440,492,561`, `cli.py:488`,
  `engine.py:658`, `scripting.py:382,761,772,776,788,889,919,923,933`,
  `door.py:1123`, `generate_gaps_report.py:339,342`.

### Shipped-constant pinning (CLAUDE.md rule 10)

Confirmed pinned by mutation: `MAX_INFLIGHT_FRAMES = 64` → `128` (caught),
`MAX_FRAME_BACKLOG = 256` → `512` (caught), `MAX_DESCRIPTION_CACHE = 512` → `1024`
(caught). Round 7's four relaxed-by-16x constants are now genuinely defended.

### Test-suite hygiene

- No skipped tests, no `xfail`, no `assert True`, no `assert x in (a, b)` accepting
  contradictory outcomes were found in the round-8 additions.
- `filterwarnings = ["error"]`, `timeout = 60`, `timeout_method = "thread"` all still
  in force; the thread method's rationale (hypothesis swallowing SIGALRM) still holds.
- `tests/conftest.py`'s `_reset_extra_scripts_dir` correctly clears
  `scripting._description_cache` between tests — without it the new memoisation would
  make outcomes order-dependent, and the fixture's comment says exactly that.
- The `GOLDEN_SCHEDULE_WIRE_TO_DEVICE` / `GOLDEN_SCHEDULE_WIRE_FROM_DEVICE` pair and
  `assert_schedule_wire_types` correctly pin the two *directions* separately, including
  the `True == 1` / `1 == True` type traps. **I did not propose, and do not propose, any
  change to the wire shape**; the deliberate `enabled` divergence is documented and
  correct.
