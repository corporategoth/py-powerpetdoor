# Backend Developer Analysis — Round 10

Commit: `65ad86d`. Scope: `client.py`, `door.py`, `const.py`, `schedule.py`,
`tz_utils.py`, `framing.py`, `sanitize.py`,
`simulator/{server,protocol,state,scripting,engine}.py`. CLI/ctl/prompt code
excluded.

Every claim below was produced by running code against the checked-out tree.
All three proposed fixes were additionally applied to throwaway copies of the
repo (`/tmp/ppd10/repo`, `repo2`, `repo3`, since deleted) and validated with
the full deterministic suite, the fuzz suite, `--cov`, `ruff` and `mypy`
before being written down. This round deliberately went wide into code the
previous nine passes never executed — the facade's twelve flag listeners, the
script DSL's assertion and step parsers, `compute_schedule_diff`'s allocator,
and the client's reconnect/task hygiene — rather than re-treading the framer
and the receive path.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 2 |
| Low      | 1 |
| Trivial  | 0 |

All four round-9 changes verified clean; no regressions found in any of them
(see "Round 9 Fix Verification").

---

## Findings

### Finding 1 — Medium — twelve facade listeners cache whatever `make_bool` handed back, so one device frame puts a `float`/`list`/`dict` into six properties documented `-> bool` and all five `NotificationSettings` fields — permanently, and `safety_lock` fails in the permissive direction

**File:** `src/powerpetdoor/door.py:1484-1488` (`_on_settings`),
`src/powerpetdoor/door.py:1500-1527` (the seven sensor listeners),
`src/powerpetdoor/door.py:1621-1639` (the five notification listeners); root
cause at `src/powerpetdoor/client.py:257-258` (`make_bool`'s `else: return v`).

The module-level doctrine `door.py:133-161` states the rule this violates:

> **nothing enters the facade cache without a type check, and a value that
> fails it leaves the cache untouched.**

Round 7 applied that rule to battery, stats, timezone and hw_info via
`_keep_int`/`_keep_bool`/`_keep_str`. The twelve *flag* listeners were left
relying on `make_bool` returning `None` for anything it cannot read. It does
not. `make_bool` is annotated `str | int | bool | None -> bool | None`, but
the device supplies `Any`, and every other JSON type falls out of its final
`else: return v` **unchanged**:

```
   make_bool(1.0     ) -> 1.0      (float)
   make_bool(0.0     ) -> 0.0      (float)
   make_bool(3.5     ) -> 3.5      (float)
   make_bool([]      ) -> []       (list)
   make_bool({}      ) -> {}       (dict)
   make_bool(None    ) -> None     (NoneType)
   make_bool(bogus   ) -> None     (NoneType)
```

The listeners guard with `if value is not None:` — which `[]`, `{}` and `1.0`
all pass — and then assign straight into the strictly typed attribute.

`client.py:576-581` promises the opposite, and names this exact hazard:

> value is the coerced boolean, or None if the device sent a value
> ``make_bool`` does not recognize. Test for None explicitly: ``if value:``
> maps "unparseable" onto False, which for a safety lock fails in the
> permissive direction

The documented defence (`if value is not None`) does not catch it, because the
value is not `None`.

**Reproduction A — one frame into a real `PowerPetDoorClient` driving a real
`PowerPetDoor`** (`/tmp/ppd10/probe_bool_poison.py`). Case A is the most
plausible real-firmware trigger: a device that spells its 1/0 flags as JSON
numbers *with a decimal point*. `docs/protocol.md` is reverse-engineered, so
nothing rules this out.

```
  A. a firmware variant that spells its 1/0 flags as JSON *numbers with a decimal point*
    {"CMD":"GET_SETTINGS","success":"true","settings":{"power_state":1.0,"inside":0.0,"outsideSensorSafetyLock":1.0,"doorOptions":0.0,"timersEnabled":1.0,"outside":1.0,"allowCmdLockout":1.0,"sensorOnIndoorNotificationsEnabled":1.0}}
      door.power                    True   -> 1.0      (float)  <-- declared bool
      door.inside_sensor            True   -> 0.0      (float)  <-- declared bool
      door.outside_sensor           True   -> 1.0      (float)  <-- declared bool
      door.auto                     False  -> 1.0      (float)  <-- declared bool
      door.safety_lock              False  -> 1.0      (float)  <-- declared bool
      door.autoretract              True   -> 0.0      (float)  <-- declared bool
      door.pet_proximity_keep_open  False  -> False    (bool)
      after a later well-formed GET_SETTINGS for another field: {'power': 1.0, 'inside_sensor': 0.0, 'outside_sensor': 1.0, 'safety_lock': 1.0, 'autoretract': 0.0}

  B. containers / an out-of-range number
    {"CMD":"GET_SETTINGS","success":"true","settings":{"power_state":[],"inside":{},"outsideSensorSafetyLock":3.5,"sensorOnIndoorNotificationsEnabled":[]}}
      door.power                    True   -> []       (list)  <-- declared bool
      door.inside_sensor            True   -> {}       (dict)  <-- declared bool
      door.safety_lock              False  -> 3.5      (float)  <-- declared bool
      after a later well-formed GET_SETTINGS for another field: {'power': [], 'inside_sensor': {}, 'safety_lock': 3.5}

  C. the same through GET_SENSORS (a different handler)
    {"CMD":"GET_SENSORS","success":"true","inside":1.0,"outside":0.0}
      door.inside_sensor            True   -> 1.0      (float)  <-- declared bool
      door.outside_sensor           True   -> 0.0      (float)  <-- declared bool
```

The last line of each block is the point: it is **sticky**. A later
well-formed frame for a *different* field does not heal it; only a
well-formed frame for that specific field does. `pet_proximity_keep_open` is
accidentally safe because `not value` returns a real `bool`.

**Reproduction B — the notification dataclass** (`/tmp/ppd10/probe_notif.py`,
one `GET_NOTIFICATIONS` frame):

```
=== BEFORE (shipped tree) ===
    NotificationSettings.inside_on    = []     (list)   <-- dataclass declares bool
    NotificationSettings.inside_off   = 1.0    (float)  <-- dataclass declares bool
    NotificationSettings.outside_on   = {}     (dict)   <-- dataclass declares bool
    NotificationSettings.outside_off  = 0.0    (float)  <-- dataclass declares bool
    NotificationSettings.low_battery  = True   (bool)
```

**Reproduction C — the permissive-direction failure**
(`/tmp/ppd10/probe_permissive.py`): establish the truth with a good frame,
then send one frame whose flag is a JSON container.

```
=== BEFORE (shipped tree) ===
    after a good frame  : safety_lock=True
    after the bad frames: safety_lock=[] (truthiness False)
    a consumer's `if door.safety_lock:` now reads -> False
```

A known-ON outside-sensor safety lock reads as OFF, permanently, from one
frame — the failure direction `client.py:580-581` explicitly warns about.

**Why this is a layer-1 defect, not a wire question.** Nothing here proposes
changing the protocol or refusing anything on the wire. The client (layer 3)
keeps handing listeners and `notify=True` futures exactly what the device
sent; only the facade's *cache* — the strict-Python-API layer whose properties
are annotated `bool` and which the Home Assistant integration publishes —
stops storing a non-`bool`. I explicitly rejected the alternative of changing
`make_bool` itself: `compress_schedule` (`schedule.py:541`) calls it
unguarded as `make_bool(day) is True`, so a float day flag currently fails
*closed*, and widening `make_bool` would flip that to permissive. The fix
belongs in `door.py` only.

**Recommendation.** Add `_keep_flag` beside `_keep_bool` and route all twelve
listeners plus the `_on_settings` loop through it. It must **coerce** a finite
float rather than reject it — `_keep_int` sets that precedent, and simply
rejecting would freeze the cache at constructor defaults for a float-spelling
firmware, which is worse than today.

```python
def _keep_flag(value: Any, cached: bool, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value != 0.0
    _log_rejected(field_name, value, "bool")
    return cached
```

The call sites, all mechanical:

```python
# _on_settings (door.py:1484-1488)
        for field_name, attr, inverted in boolean_fields:
            if field_name in settings:
                cached = getattr(self, attr)
                value = _keep_flag(
                    make_bool(settings[field_name]),
                    (not cached) if inverted else cached,
                    field_name,
                )
                setattr(self, attr, (not value) if inverted else value)

# the six direct sensor listeners (door.py:1500-1522), e.g.
    def _on_power_update(self, field_name: str, value: bool | None) -> None:
        self._power = _keep_flag(value, self._power, field_name)

# the inverted one (door.py:1524-1527)
    def _on_cmd_lockout_update(self, field_name: str, value: bool | None) -> None:
        # Inverted logic
        self._pet_proximity_keep_open = not _keep_flag(
            value, not self._pet_proximity_keep_open, field_name
        )

# the five notification listeners (door.py:1621-1639), e.g.
    def _on_notify_inside_on(self, field_name: str, value: bool | None) -> None:
        self._notifications.inside_on = _keep_flag(value, self._notifications.inside_on, field_name)
```

Note the one behavioural side effect worth declaring: a value `make_bool`
already mapped to `None` (an unrecognized string, or an absent-but-present
`null`) now takes `_log_rejected`'s DEBUG line instead of being skipped
silently. That is per the module doctrine ("Rejections log at DEBUG, not
WARNING", `door.py:157-161`) and makes a previously invisible skip visible.

Validated in the throwaway copy:

```
  A. (float firmware)  door.power True  door.inside_sensor False  door.safety_lock True   ... all bool
     after a later well-formed GET_SETTINGS for another field: {}      <- nothing left poisoned
  B. (containers)      door.power True  door.inside_sensor True   door.safety_lock True   ... all bool, cached values kept
  C. (GET_SENSORS)     door.inside_sensor True  door.outside_sensor False                 ... all bool
```

and the permissive case becomes:

```
=== AFTER (patched copy) ===
    after the bad frames: safety_lock=True (truthiness True)
    a consumer's `if door.safety_lock:` now reads -> True
```

**Suite, lint, types — no test churn:**

```
2822 passed in 35.40s          (tests/, --ignore=tests/fuzz)
47 passed in 7.70s             (tests/fuzz)
All checks passed!             (ruff check)
Success: no issues found in 31 source files   (mypy)
```

**Coverage cost, stated plainly.** The gate is 100%; this fix lands at
99.67% for `door.py`, missing exactly one line — `return value != 0.0`, the
float-coercion branch. No shipped test sends a float flag, which is why the
defect survived nine rounds. One new test closes it.

---

### Finding 2 — Medium — nine of the twelve documented `assert` conditions cannot be used with the value spellings `docs/simulator.md` prescribes: PyYAML resolves them to native scalars and `_assert_condition` calls `.lower()` on them, so the run reports FAILED with a Python internals message for an assertion that is true

**File:** `src/powerpetdoor/simulator/scripting.py:969-970` (in
`_assert_condition`, `scripting.py:930-973`). Contract at
`docs/simulator.md`, "Conditions for `assert`".

```python
        expected_normalized = expected.upper() if condition == "door_status" else expected.lower()
        actual_normalized = actual.upper() if condition == "door_status" else actual.lower()
```

`expected` is `params.get("equals", "")` — straight out of `yaml.safe_load`.
YAML 1.1 resolves `on`/`off`/`yes`/`no`/`true`/`false` to **booleans** and
bare digits to **ints/floats**, and those are precisely the spellings the
documentation prescribes:

| Condition | Expected Values (docs/simulator.md) |
|-----------|-------------------------------------|
| `power`, `auto`, `autoretract`, `safety_lock`, `cmd_lockout` | `on`, `off` |
| `battery`, `hold_time`, `total_open_cycles`, `total_auto_retracts` | Number (e.g. `75`, `10`) |

**Reproduction — the whole published condition set, each with the documented
value and with state set so the assertion is TRUE**
(`/tmp/ppd10/probe_matrix12.py`; the probe asserts its own matrix covers every
name in `scripting.ASSERT_CONDITIONS`):

```
=== BEFORE (shipped tree, commit 65ad86d) ===
    equals: DOOR_CLOSED  -> PyYAML gives str   'DOOR_CLOSED'  assert door_status          PASSED=True
    equals: on           -> PyYAML gives bool  True           assert power                PASSED=False
    equals: on           -> PyYAML gives bool  True           assert auto                 PASSED=False
    equals: 75           -> PyYAML gives int   75             assert battery              PASSED=False
    equals: 10           -> PyYAML gives int   10             assert hold_time            PASSED=False
    equals: enabled      -> PyYAML gives str   'enabled'      assert inside               PASSED=True
    equals: enabled      -> PyYAML gives str   'enabled'      assert outside              PASSED=True
    equals: on           -> PyYAML gives bool  True           assert autoretract          PASSED=False
    equals: off          -> PyYAML gives bool  False          assert safety_lock          PASSED=False
    equals: off          -> PyYAML gives bool  False          assert cmd_lockout          PASSED=False
    equals: 0            -> PyYAML gives int   0              assert total_open_cycles    PASSED=False
    equals: 0            -> PyYAML gives int   0              assert total_auto_retracts  PASSED=False
    conditions unusable with the documented spelling: 9/12 ['power', 'auto', 'battery', 'hold_time', 'autoretract', 'safety_lock', 'cmd_lockout', 'total_open_cycles', 'total_auto_retracts']
```

Only the three conditions whose documented values happen to be non-YAML-magic
strings work: `door_status` (`DOOR_*`) and `inside`/`outside`
(`enabled`/`disabled`). Every shipped script under
`src/powerpetdoor/simulator/scripts/` asserts `door_status` and nothing else,
which is exactly why this survived nine rounds.

**End to end, through the real binary** (`ppd-simulator --oneshot`, the CI
front end), with a script that sets power off and then asserts it is off:

```
2026-08-22 19:26:32 [INFO]   Step 1: set(name=power, value=off)
2026-08-22 19:26:32 [INFO]   Step 2: assert(condition=power, equals=False)
2026-08-22 19:26:32 [ERROR] Unexpected error at step 2: 'bool' object has no attribute 'lower'
>>> Script FAILED: doc_assert

>>> All scripts FAILED
EXIT=1
```

Three things are wrong at once: the verdict is FAILED for a true assertion;
the exit code is 1, so this is a red CI build with no defect behind it; and
the only diagnostic is a Python `AttributeError` string with no hint that the
remedy is to quote the value. The progress line even renders `equals=False`,
confirming the author's `off` was silently re-interpreted.

The failure is *closed* (no false green), which is why this is Medium and not
High.

**A second facet the quoting workaround does not fix.** `hold_time`'s actual
is `str(state.hold_time)` and the field is a float everywhere else in the
project, so even `equals: "10"` fails against a hold time of `10.0`
(`"10" != "10.0"`) — the documented example is unusable in every spelling.

**Recommendation.** Have each condition declare the domain it compares in,
keeping the `condition == "..."` chain intact (two shipped tests scrape that
chain by source — `tests/simulator/test_scripting.py:1769` `_chain_names` and
`tests/test_docs_accuracy.py:631` — and both keep passing).

```python
        if condition == "door_status":
            actual, domain = state.door_status, _ASSERT_TEXT
        elif condition == "power":
            actual, domain = ("on" if state.power else "off"), _ASSERT_FLAG
        ...
        if not self._assert_matches(domain, expected, actual, condition):
            raise ScriptAssertionError(f"{condition}: expected '{expected}', got '{actual}'")

    def _assert_matches(self, domain, expected, actual, condition) -> bool:
        if domain is _ASSERT_NUMBER:
            try:
                return self._script_number(expected, condition, -math.inf, math.inf) == float(actual)
            except ScriptError:
                return False
        if domain is _ASSERT_FLAG:
            flag = self._assert_flag(expected)
            return flag is not None and flag == (actual in _ASSERT_TRUE_WORDS)
        return str(expected).upper() == actual.upper()
```

`_assert_flag` must **not** be `_script_bool`: that one fails closed, and an
unreadable expectation quietly becoming `False` would make
`assert power equals: purple` *pass* against a powered-down door. It returns
`None` for anything unreadable, which becomes an ordinary assertion failure
with the existing "expected X, got Y" message — the same class the shipped
code produces today.

Validated in the throwaway copy:

```
=== AFTER (validated patch) ===
    conditions unusable with the documented spelling: 0/12 []
```

and, checked explicitly for false passes (`/tmp/ppd10/probe_no_false_pass.py`,
11 cases including garbage expectations in both domains):

```
=== BEFORE (shipped tree) ===   wrong verdicts: 2   (two TRUE assertions reported FAILED)
=== AFTER (patched copy) ===    wrong verdicts: 0
```

**Suite, lint, types — no test churn:** `2822 passed`, `47 passed` (fuzz),
`ruff` clean, `mypy` clean.

**Coverage cost:** `scripting.py` lands at 99.35% — missing
`except ScriptError: return False` (unreadable numeric expectation), the
non-`str` arm of `_assert_flag`'s alias lookup, and its final `return None`.
Three new tests close them, and one of the three is the finding itself
(`assert power equals: off` as a YAML bool).

---

### Finding 3 — Low — `Script.from_yaml` pops `action` out of the parsed node in place, so a YAML *alias* reusing an anchored step is rejected with "missing 'action' field" for a step that plainly has one

**File:** `src/powerpetdoor/simulator/scripting.py:345`.

```python
                action = step_data.pop("action", None)
```

`yaml.safe_load` resolves an alias (`- *settle`) to *the same dict object* as
its anchor. `pop` mutates that shared object, so the key is gone for every
later use.

**Reproduction** (`/tmp/ppd10/probe_yaml_anchor.py`) — a five-step script that
reuses one anchored `wait` three times:

```yaml
steps:
  - &settle
    action: wait
    seconds: 0.01
  - action: open
  - *settle
  - action: close
  - *settle
```

```
--- yaml.safe_load sees identical objects? ---
step0 is step2: True
steps as parsed: [{'action': 'wait', 'seconds': 0.01}, {'action': 'open'}, {'action': 'wait', 'seconds': 0.01}, {'action': 'close'}, {'action': 'wait', 'seconds': 0.01}]
--- Script.from_yaml ---
ScriptError: Step 3: missing 'action' field
```

The line above the error is the whole finding: PyYAML delivered step 3 *with*
its `action`, and the parser deleted it a moment earlier while reading step 1.

Anchors and aliases are the standard YAML answer to "repeat this step", and
the DSL invites long repetitive scripts. The error is unactionable — it
describes a file that does not exist. It fails loudly rather than silently,
which is why this is Low and not Medium; the two anchors of a merge key
(`<<: *base`) are unaffected, because PyYAML builds a fresh dict for those.

A second, latent half: even when only one alias is used, `params=step_data`
hands two `ScriptStep`s the same dict object, so any future code that mutates
`step.params` would corrupt a sibling step.

**Recommendation.** Read without mutating and build `params` separately:

```python
                action = step_data.get("action")
                if not action:
                    raise ScriptError(f"Step {i}: missing 'action' field")
                params = {key: value for key, value in step_data.items() if key != "action"}
                steps.append(ScriptStep(action=action, params=params, line_number=i))
```

Validated:

```
--- Script.from_yaml ---
  step 1: action='wait' params={'seconds': 0.01}
  step 2: action='open' params={}
  step 3: action='wait' params={'seconds': 0.01}
  step 4: action='close' params={}
  step 5: action='wait' params={'seconds': 0.01}
```

**Suite:** `2822 passed`, no test churn, no coverage change (the changed lines
are on the existing hot path).

---

### Fix independence

The refuter found a load-bearing fix *order* in round 9, so this was checked
by execution rather than argued. The `door.py` change and the `scripting.py`
changes were each applied alone to a fresh copy of `65ad86d`:

```
repo3 (door.py only)       2822 passed in 45.67s
repo2 (scripting.py only)  2822 passed in 38.90s
repo  (all three together) 2822 passed in 36.26s ; tests/fuzz 47 passed
```

No ordering dependence: the two files share no changed symbol, and
`scripting.py` gains an import of `make_bool` that `door.py` already had.

---

## Round 9 Fix Verification

**1. The re-arm moved into the `finally` beside `_update_flow()`
(`framing.py:639-642`) — verified, all four raise cases plus one more.**
`/tmp/ppd10/verify_r9.py`, driving a real `FrameDispatcher` through
`submit()` (the `data_received` equivalent) and 8,000 loop turns:

```
== A. framing: raising dispatch, four cases (pause_at=256, max_inflight=64) ==
   frames=10    raise_on=1   dispatch_returns_task=False -> backlog=0 inflight=0 paused=False transport.paused=False dispatched=10 raises=['RuntimeError(escaped submit)']
   frames=1000  raise_on=1   dispatch_returns_task=False -> backlog=0 inflight=0 paused=False transport.paused=False dispatched=1000 raises=['RuntimeError(escaped submit)']
   frames=1000  raise_on=65  dispatch_returns_task=False -> backlog=0 inflight=0 paused=False transport.paused=False dispatched=1000 raises=['RuntimeError']
   frames=1000  raise_on=65  dispatch_returns_task=True  -> backlog=0 inflight=0 paused=False transport.paused=False dispatched=1000 raises=['RuntimeError']
   frames=1000  raise_on=1   dispatch_returns_task=True  -> backlog=0 inflight=0 paused=False transport.paused=False dispatched=1000 raises=['RuntimeError']
```

Round 9's wedged case (`frames=1000 raise_on=65`, previously
`backlog=934 inflight=0 paused=True` forever) now drains to zero with reading
resumed. `dispatched=1000` on a 1000-frame burst with one raise confirms the
docstring's "one frame lost *per raise*" — the raising frame was popped and
counted, and every other frame was delivered. No regression on the
non-raising path.

**2. `frame_size` plumbed into the two `process_message` throttles
(`client.py:261-275`, `1796`, `1840`, `1926`) — verified exact on every shape.**
`/tmp/ppd10/verify_r9b.py`, feeding a real `PowerPetDoorClient`:

```
  compact `{}`                                 wire 2      -> Ignored 1 malformed message(s) ... (2 bytes)
  `{}` padded with 60,000 spaces               wire 60002  -> Ignored 1 malformed message(s) ... (60002 bytes)
  compact error envelope                       wire 25     -> Device reported 1 error response(s) (25 bytes)
  error envelope padded with 60,000 spaces     wire 60031  -> Device reported 1 error response(s) (60031 bytes)
  unparseable frame padded (the control)       wire 60003  -> Failed to decode 1 JSON frame(s) ... (60003 bytes)
```

Round 9 measured `2` and `28` for the first two padded shapes (a 30,001x
under-report and a 12% over-report). Both are now exact, and the compact
error envelope now reports 25 rather than the re-serialized 28.

**3. `sensor_open_block_reason` as the one predicate for both sensor entry
points (`engine.py:366-421`, `435`, `534`) — verified, 12/12 agree.**
`/tmp/ppd10/verify_r9c.py` drives a real `DoorSimulator` through both entry
points under every gate, both sensors:

```
  gate                                   sensor    trigger_sensor  activate_sensor  agree
  baseline (everything permits)          inside            opened           opened  YES
  baseline (everything permits)          outside           opened           opened  YES
  power OFF                              inside           ignored          ignored  YES
  power OFF                              outside          ignored          ignored  YES
  cmd_lockout ON                         inside           ignored          ignored  YES
  cmd_lockout ON                         outside          ignored          ignored  YES
  sensor disabled                        inside           ignored          ignored  YES
  sensor disabled                        outside          ignored          ignored  YES
  safety_lock ON (outside only)          inside            opened           opened  YES
  safety_lock ON (outside only)          outside          ignored          ignored  YES
  auto ON + schedule window closed       inside           ignored          ignored  YES
  auto ON + schedule window closed       outside          ignored          ignored  YES
  disagreements: 0
```

The `safety_lock` row also confirms the predicate is still per-sensor rather
than global.

**4. Surrogates in `_CONTROL_CHAR_RE` with width-aware escaping
(`sanitize.py:34`, `48-58`) — verified, including the property the fix
exists for.** `/tmp/ppd10/verify_r9d.py`:

```
  NUL                          '\x00'      -> '\\x00'     utf-8 OK (4 bytes)
  ESC                          '\x1b'      -> '\\x1b'     utf-8 OK (4 bytes)
  DEL                          '\x7f'      -> '\\x7f'     utf-8 OK (4 bytes)
  C1 0x9b (CSI)                '\x9b'      -> '\\x9b'     utf-8 OK (4 bytes)
  lone hi surrogate U+D800     '\ud800'    -> '\\ud800'   utf-8 OK (6 bytes)
  lone lo surrogate U+DFFF     '\udfff'    -> '\\udfff'   utf-8 OK (6 bytes)
  tab/newline kept             'a\tb\nc'   -> 'a\tb\nc'   utf-8 OK (5 bytes)
  plain unicode kept           'café 中'    -> 'café 中'    utf-8 OK (9 bytes)
  -- the width argument --
   naive f'\\x{ord(c):02x}' would render '\\xd800' (reads as \xd8 + '00'); actual: '\\ud800'
  -- can a utf-8 handler write the record? --
   wrote: b'device said: \\ud800\n'
  -- output bound and cost (limit=200) --
   64 KiB of ESC     -> 814 chars in 79.7 us
   64 KiB of U+D800  -> 1214 chars in 87.9 us
```

Truncate-before-escape still holds; the surrogate escape is 6 chars so the
bound is `200*6 + 14 = 1214`, still constant. Nothing legitimate is
over-escaped (surrogates are never valid scalar values in text), tab/newline
and ordinary non-ASCII are untouched, and the round-9 M2 scenario — a record
a `utf-8` handler could not write — is closed.

---

## Areas Reviewed With No Findings

Each was probed by execution, not just read.

- **`compute_schedule_diff` index allocation and reuse.** Correct on the
  cases that matter: `/tmp/ppd10/probe_breadth.py` with 3 current slots
  (0,1,5) and 4 new entries (1 unchanged) produced `delete=[] set=[1, 5, 2]`
  with no duplicate index — the round-6 test-fanatic M1 regression is still
  closed — and a current entry with an unusable index is skipped
  (`delete=[2] set=[0]`). **Declined:** with more than 256 entries the fresh
  slot allocator walks past `MAX_SCHEDULE_INDEX` (300 distinct new entries
  against an empty device gave `max_index=299`), so the function can emit a
  `SET_SCHEDULE` payload this project's own parsers refuse. Reaching it needs
  the caller to request >256 schedules, or a device already holding 255; no
  real door has that many slots, so it is recorded here rather than reported.

- **`FrameScanner` under a one-byte-per-segment dribble** (the round-6
  backend L1 shape). 60,000 one-char feeds into a scanner with an object in
  progress: `0.395 s`, `retained chars=60006`, `tracemalloc delta=62,135
  bytes` against a `65,536` cap. The `MAX_RETAINED_PIECES=64` coalescing is
  still holding the memory within a small factor of the character cap.

- **`EventThrottle`'s doubling schedule and cap**, measured end to end with a
  frozen clock: `10 -> 5 records`, `1,000 -> 11`, `20,000 -> 17`,
  `200,000 -> 61`. Logarithmic below the `max_interval` cap and linear at
  `1/4096` above it, as documented.

- **Simulator `SET_HOLD_TIME` bounds and the `GET_SETTINGS` round trip**
  (`/tmp/ppd10/probe_holdtime.py`). All eleven shapes behave: `0`, `1`,
  `90000` accepted; `90001`, `-1`, `1e400`, `NaN`, `"200"`, `[]`, `true`
  rejected with a reason, state untouched, and the later `GET_SETTINGS` still
  answers `holdOpenTime=200`. No value reaches `int(hold_time * 100)` that it
  cannot represent.

- **Simulator `SET_TIMEZONE` bounds and schedule evaluation afterwards.**
  128 chars accepted / 129 rejected, lone surrogate, ANSI escape, empty
  string, int, list — every one answers normally and
  `is_sensor_allowed_by_schedule` stays total.

- **Client reconnect, task, fd and future hygiene** across 200 failed
  `connect()`s against a closed port, with work queued while disconnected
  (`/tmp/ppd10/probe_reconnect.py`):

  ```
     baseline: fds=8 tasks=1
     backoff (base 0.0001s, attempts 1..24): first=0.000110 last=8.068 cap=300.0 exceeded=[]
     after 200 failed connect()s: fds=8 tracked_tasks=1 handler_tasks=0 queue=0 outstanding=0
     50 notify futures after disconnect(): {'ConnectionError': 50}  outstanding_left=0
     after shutdown(): fds=8 tracked_tasks=0 live_asyncio_tasks=0
  ```

  No fd leak, no task leak, the queue does not grow across reconnect cycles,
  and every `notify=True` future is failed with `ConnectionError` rather than
  cancelled or left pending.

- **The `_on_hold_time_update` float boundary** that round 8's
  `maximum=_FLOAT_REPRESENTABLE_MAX` created. I specifically checked whether
  the *rejection* path can overflow: the fallback is
  `round(self._hold_time * 100)`, and `_hold_time` is only ever set from
  `centiseconds / 100.0`. At the extreme, `int(float_max)/100.0` is
  `1.7976931348623156e+306` and `x * 100` is `1.7976931348623157e+308` —
  exactly `float_info.max`, representable, so `round()` cannot raise. No
  finding.

- **`_recorded_size` / `len(frame)` units.** `frame` is the *decoded* string,
  and `data.decode("ascii", errors="backslashreplace")` expands each
  non-ASCII byte to four characters, so a frame carrying non-ASCII is
  reported ~4x its wire size. **Declined:** the error is uniform across all
  four receive-path throttles, it *over*-reports (so it cannot hide a peer),
  the true byte count is separately recorded by `_non_ascii`, and the
  escaped text is what the code actually processed.

- **`Script.from_simple_commands`** raises a bare `ValueError`/`TypeError`
  rather than `ScriptError` for `wait abc` / `add_schedule x`. **Declined:**
  it has no production caller (only `tests/` and `test_docs_accuracy.py`), so
  no front end can reach it.

- **The facade under a 22-frame adversarial reply matrix**
  (`/tmp/ppd10/probe_facade_sweep.py`): malformed schedules, non-mapping
  `fwInfo`, `NaN`/`1e400` hold times, unhashable `msgID`, a bare notification
  envelope with a container `sensorState`, a string where a schedule index
  list belongs. `exceptions escaping data_received / reaching the loop
  handler: []` — nothing escapes; the only property violations were
  Finding 1's, and `battery.charging`/`battery.discharging` never raised
  (round-7 M1 stays closed).

- **`DoorMotionEngine` hold/wake machinery and `DoorSimulator` broadcast
  loops** re-read against the new shared predicate. `activate_sensor` still
  records the sensor-active flag *before* consulting
  `sensor_open_block_reason` — deliberate and documented ("a pet standing in
  the doorway is a physical fact"), and `is_sensor_blocking_close` continues
  to apply its own separate rules. No divergence introduced by the round-9
  unification.

- **Full suites on the shipped tree and on every patched copy:**
  `2822 passed` (deterministic) and `47 passed` (fuzz) throughout.
