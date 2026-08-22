# Frontend Developer Analysis — Round 9

Commit under review: `145cf05` ("Round 8 fixes (refuter-approved list)").
Scope: the simulator's terminal front end (`cli.py`, `ctl.py`,
`prompt_common.py`, `commands/*`, `scripting.py`'s user-facing DSL) plus the
library's public API and `docs/` as a **developer front end**. There is no web
UI.

Every finding below was produced by running the shipped binaries
(`.venv/bin/ppd-simulator`, `.venv/bin/ppd-simulator-ctl`) or the installed
package. Commands and their real output are quoted verbatim. Nothing here
proposes a change to the device wire protocol; `docs/protocol.md` is treated as
reverse-engineered and non-authoritative throughout.

---

## Summary

| Severity | Count | Findings |
|----------|-------|----------|
| High | 1 | H1 |
| Medium | 3 | M1, M2, M3 |
| Low | 4 | L1, L2, L3, L4 |
| Trivial | 1 | T1 |

**H1** is the one that matters: Ctrl-C on the documented CI command exits **0**
and prints `>>> All scripts PASSED` for a run that stopped two thirds of the way
through — and exits 0 even when it prints `>>> All scripts FAILED`. The code
carries a comment saying the opposite, and three tests named
`test_*_keyboard_interrupt_exits_130` pass while the binary does not.

All eleven round-8 fixes verified against running binaries; none regressed. Tab
completion with 200 scripts: cold **32.7 ms**, warm **4.4 ms**, and the door
protocol's ping RTT during the Tab press peaked at **3.1 ms** — the event loop
is genuinely no longer stalled. `ctl list` RTT 18.0 ms cold / **4.9–5.0 ms**
warm. Step annotations (`note`/`comment`/`description`) work on every step while
a typo'd real parameter still fails.

---

## Findings

### H1 (High) — Ctrl-C makes `ppd-simulator --script … --oneshot` exit 0 and report `PASSED`, for a run that never reached its assertions

**Files:** `src/powerpetdoor/simulator/cli.py:874-878` (the swallowed
cancellation), `src/powerpetdoor/simulator/cli.py:525-531` (the `PASSED` line
and `script_result[0]`), `src/powerpetdoor/simulator/cli.py:1124-1131` (the exit
code that never happens).

**Reproduction**

Script — three steps; the assertion is the point of the test:

```yaml
# /tmp/ppd9/t/long.yaml
name: Long Script
steps:
  - action: log
    message: starting
  - action: wait
    seconds: 30
  - action: assert
    condition: door_status
    equals: DOOR_CLOSED
```

Real terminal, real Ctrl-C (`\x03` written into a `pty.fork()`ed tty, 4 s after
start — i.e. inside the 30 s `wait`, before the `assert`):

```
$ python3 pty_ctrl_c.py          # execs ppd-simulator --port 3990 --script /tmp/ppd9/t/long.yaml --oneshot
exit status: 0
2026-08-22 15:37:35,107 [INFO]   Step 1: log(message=starting)
2026-08-22 15:37:35,107 [INFO]   Step 2: wait(seconds=30)
>>> All scripts PASSED
```

Step 3 never ran. Exit status **0**.

Same result when the signal is delivered the way a supervisor or CI canceller
delivers it (`p.send_signal(SIGINT)`), and the daemon behaves the same way:

```
--script --oneshot, SIGINT mid-run: rc=0
   tail: ['[INFO]   [SCRIPT] starting', '[INFO]   Step 2: wait(seconds=30)',
          '[INFO] Door simulator stopped', '>>> All scripts PASSED']

--script --oneshot, SIGTERM mid-run: rc=-15
   tail: ['[INFO] Running script: Long Script', '[INFO]   Step 1: log(message=starting)',
          '[INFO]   [SCRIPT] starting', '[INFO]   Step 2: wait(seconds=30)']

--daemon, SIGINT: rc=0
   tail: ['Simulator started on 0.0.0.0:3954', 'Control channel: 127.0.0.1:3955',
          '[INFO] Door simulator stopped']
```

It is not merely that the *result* is unknown — the exit code is 0 even when the
run has already **explicitly reported failure**. Two scripts, the first one
failing, SIGINT during the second:

```
$ ppd-simulator --port 3970 -s failing.yaml -s long.yaml --oneshot   # SIGINT at t+5s
rc = 0
[ERROR] Assertion failed at step 2: door_status: expected 'DOOR_HOLDING', got 'DOOR_CLOSED'
>>> Script FAILED: Failing Script
>>> All scripts FAILED
```

`>>> All scripts FAILED` and `rc = 0` in the same run.

Control — the exit-code machinery is fine when nothing interrupts it:

```
failing -> rc=1  | >>> All scripts FAILED
passing -> rc=0  | >>> All scripts PASSED
```

And the sibling binary gets it right, which makes this an inconsistency inside
one product:

```
$ ppd-simulator-ctl -p 3811 run long wait      # SIGINT at t+4s
ctl run long wait + SIGINT -> rc = 130
   tail: ['[INFO]   Step 2: wait(seconds=30)', 'Interrupted.']
```

**Description**

`asyncio.Runner` delivers Ctrl-C by cancelling the main task. `run_simulator`
catches that cancellation and discards it:

```python
874:    # Wait for stop signal
875:    try:
876:        await stop_event.wait()
877:    except asyncio.CancelledError:
878:        pass
879:    finally:
```

so the coroutine returns normally, `asyncio.run()` returns a value instead of
re-raising `KeyboardInterrupt`, and `main()`'s handler — the one whose comment
reads *"Interrupted runs must not report success to CI (128 + SIGINT)"*
(`cli.py:1130`) — is never entered. `sys.exit(130)` never runs. Nor does
`sys.exit(0 if result else 1)`, because `run_simulator` returned before
`script_result[0]` was ever set, so `result is None` (`cli.py:1125`) and `main()`
falls off the end at 0.

Meanwhile `_run_startup_scripts` has the same swallow, and its `finally` prints
the banner regardless:

```python
525:    except asyncio.CancelledError:
526:        pass
527:    finally:
528:        script_result[0] = all_success
529:        if oneshot:
530:            status_print(f"\n>>> All scripts {'PASSED' if all_success else 'FAILED'}")
```

`all_success` is still `True` for a script that was cut off mid-way, so the
banner actively asserts a result the run never established.

The regression is invisible to the suite because the tests that claim to cover it
replace the very function that swallows the signal:

```python
# tests/simulator/test_cli.py:585
def test_keyboard_interrupt_exits_130(self, monkeypatch):
    """An interrupted run must not report success to CI."""
    async def interrupted_run(**kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "run_simulator", interrupted_run)
```

```
$ uv run pytest tests/simulator/test_cli.py tests/simulator/test_ctl.py -k keyboard_interrupt -q -n0
...                                                                      [100%]
3 passed, 191 deselected in 0.27s
```

Three green tests asserting exit 130; the binary exits 0.

**Recommendation**

1. Do not swallow the cancellation in `run_simulator`: run the cleanup in
   `finally` and re-raise (`except asyncio.CancelledError: … raise`), or set a
   flag the caller can read. Same for `_run_startup_scripts` — an interrupted run
   must not print a PASSED/FAILED verdict at all; print
   `>>> Interrupted after N of M steps` and leave the verdict unset.
2. `main()` should treat "oneshot requested but `result is None`" as a failure
   (exit 130 if interrupted, 1 otherwise) rather than falling through to 0.
   "`--oneshot` produced no result" can never legitimately mean success.
3. Install an explicit `SIGTERM` handler that sets `stop_event`, so `docker
   stop` / `systemctl stop` / a CI canceller's second signal get the same
   graceful path and a non-zero code, instead of `rc=-15` with the `finally`
   never running.
4. Replace the three monkeypatched tests with one that spawns the real binary,
   sends `SIGINT`, and asserts the exit code — the current shape cannot fail for
   the reason it exists.

---

### M1 (Medium) — the package ships fully annotated but with no `py.typed`, so every downstream consumer type-checks it as `Any`

**Files:** missing `src/powerpetdoor/py.typed`; `pyproject.toml:77-78`
(`[tool.setuptools.package-data]`, which lists only `scripts/*.yaml`).

**Reproduction**

```
$ ls /home/prez/src/pypowerpetdoor/src/powerpetdoor/py.typed
ls: cannot access '.../src/powerpetdoor/py.typed': No such file or directory
$ git ls-files | grep -i "py.typed\|MANIFEST"      # (no output)
```

Install the package the way a consumer gets it, and type-check a consumer that
contains a real bug:

```python
# /tmp/ppd9b/consumer/app.py
from powerpetdoor import PowerPetDoor

async def main() -> None:
    door = PowerPetDoor(host="10.0.0.5")
    await door.connect()
    await door.set_hold_time("banana")   # wrong: expects a float
```

```
$ python3 -m venv /tmp/ppd9b/venv
$ cp -r src/powerpetdoor "$(/tmp/ppd9b/venv/bin/python -c 'import site;print(site.getsitepackages()[0])')/"
$ mypy --config-file /tmp/ppd9b/mypy.ini --python-executable /tmp/ppd9b/venv/bin/python /tmp/ppd9b/consumer/app.py

=== AS SHIPPED (no py.typed) ===
app.py:1: error: Skipping analyzing "powerpetdoor": module is installed, but missing library stubs or py.typed marker  [import-untyped]
app.py:1: note: See https://mypy.readthedocs.io/en/stable/running_mypy.html#missing-imports
Found 1 error in 1 file (checked 1 source file)

=== after touching py.typed ===
app.py:6: error: Argument 1 to "set_hold_time" of "PowerPetDoor" has incompatible type "str"; expected "float"  [arg-type]
Found 1 error in 1 file (checked 1 source file)
```

One `touch` turns "I cannot see this library at all" into "line 6 is wrong".

**Description**

The library is the project's second front end — `ha-powerpetdoor` and the
Ostinato plugin consume it, and `docs/door.md` / `docs/client.md` document 121
exported names with types. The project type-checks itself (`mypy src`,
`warn_return_any = true`) and CI gates on it, so the annotations are known-good.
None of that value reaches a consumer: PEP 561 says an installed package's
annotations are inert unless it ships a `py.typed` marker, and this one does not.
The practical effect for a Home Assistant integration is that every
`powerpetdoor` symbol is `Any`, so `hassfest`/mypy in the downstream repo cannot
catch a single misuse of this API — including the one above, which would
otherwise be a `SET_HOLD_TIME` failure at runtime against a real door.

Note that the marker alone is not enough with this build config: `pyproject.toml`
declares an explicit `[tool.setuptools.package-data]` block, so the new file has
to be listed there or it will be present in the source tree and absent from the
wheel.

**Recommendation**

Add an empty `src/powerpetdoor/py.typed` and extend `[tool.setuptools.package-data]`:

```toml
[tool.setuptools.package-data]
"powerpetdoor" = ["py.typed"]
"powerpetdoor.simulator" = ["scripts/*.yaml", "scripts/*.yml"]
```

Pin it with a test that builds/installs and asserts the marker is importable
alongside `__init__.py`, so it cannot be dropped by a future packaging change —
the same reasoning that put `scripts/*.yaml` in that block.

---

### M2 (Medium) — `PowerPetDoor` silently queues commands issued while disconnected and replays them on the next `connect()`; the awaited variants block for the full timeout and raise a message-less `TimeoutError()`

**Files:** `src/powerpetdoor/door.py:726-736` (`open`/`open_and_hold`/`close` —
fire and forget), `src/powerpetdoor/door.py:918-932` (`set_hold_time`, and the
same shape in every other setter), `src/powerpetdoor/client.py:1876-1916`
(`send_message` enqueues unconditionally), `src/powerpetdoor/door.py:539-542`
(`connected`, which the facade never consults).

**Reproduction**

Daemon on 3800. Nothing is connected when `open_and_hold()` is called:

```
$ .venv/bin/python /tmp/ppd9b/queue_probe.py
connected: False | default_timeout: 20.0
open_and_hold() while disconnected -> returned, no error, no warning
3s later, still disconnected. status = CLOSED
after connect(): status = KEEPUP | is_open = True
set_hold_time(5) while never connected -> TimeoutError: TimeoutError() after 20.1s
```

The door physically opened and latched (`KEEPUP`) **three seconds after** the
call that requested it, as a side effect of `connect()`.

Contrast the low-level client, which gets this right — same "no connection"
condition, answered in 0.1 s with a sentence:

```
available: False effective_timeout: 4.0
future completed after 0.1s with ConnectionError: Connection closed before a response was received
after stop(): done = True | exc = Connection closed before a response was received
```

**Description**

Three separate front-end problems, all in the facade that `README.md` and
`docs/door.md` present as *the* API:

1. **No feedback, and a deferred physical action.** `open()`, `open_and_hold()`,
   `close()`, `toggle()` and `cycle()` call `send_message(..., notify=False)`,
   which enqueues and returns `None`. With no transport there is nothing to
   dequeue to, so the message sits in the priority queue until a connection
   appears — and then a pet door opens, unattended, for a request the caller was
   told nothing about. An operator UI built on this ("Open" button pressed while
   the integration is reconnecting) opens the door when the link comes back
   rather than reporting "not connected".
2. **The error, when there is one, says nothing.** Every setter is
   `await asyncio.wait_for(<future>, timeout=default_timeout)`, and
   `asyncio.wait_for` raises a bare `TimeoutError()` — literally
   `repr()` == `TimeoutError()`. A developer sees `TimeoutError:` with an empty
   message after a 20-second stall and has no way to tell "the door is wedged"
   from "you never called `connect()`".
3. **It is inconsistent with the layer beneath it.** `PowerPetDoorClient` fails in
   0.1 s with `ConnectionError: Connection closed before a response was received`
   — exactly the contract `docs/client.md:212-226` documents. The facade loses
   that diagnosis on the way up.

`connected` already exists (`door.py:539-542`); nothing in the facade reads it.

**Recommendation**

- Guard the facade at the top of each command/setter: if `not self.connected`,
  raise `ConnectionError("Not connected to <host>:<port> - call connect() first")`
  immediately rather than enqueueing. This is one shared `_require_connected()`
  helper, and it makes the fire-and-forget methods report failure at all.
- If deferred delivery is deliberate for the reconnect case, say so in
  `docs/door.md` and give it a name (`await door.open(queue_if_offline=True)`),
  so "the door opened by itself three seconds later" is a documented choice
  rather than a surprise.
- Give the timeouts a message:
  `asyncio.wait_for(...)` wrapped so the raise becomes
  `TimeoutError(f"{CMD_SET_HOLD_TIME} timed out after {t}s")`. An empty
  `TimeoutError` is the least actionable exception the API can produce.

---

### M3 (Medium) — a misspelled **top-level** script key produces a zero-step script that reports `PASSED` with exit code 0

**File:** `src/powerpetdoor/simulator/scripting.py:312-317`.

**Reproduction**

```yaml
# /tmp/ppd9b/t/typo_topkey.yaml   —  'steps' typed as 'stpes'
name: Door cycle regression test
description: the steps key is misspelled below
stpes:
  - action: log
    message: this never runs
  - action: assert
    condition: door_status
    equals: DOOR_HOLDING
```

```
$ ppd-simulator --port 3995 --script /tmp/ppd9b/t/typo_topkey.yaml --oneshot
rc=0
>>> Running script: Door cycle regression test
2026-08-22 15:42:36,016 [INFO] Running script: Door cycle regression test
>>> Script PASSED: Door cycle regression test
>>> All scripts PASSED
```

No step log, no warning, no hint that a top-level key was ignored. `PASSED`,
`rc=0`. A misspelled `name:` behaves the same way, quietly renaming the script:

```
$ ppd-simulator --port 3840 --script topkey2.yaml --oneshot     # 'name' typed as 'nmae'
>>> Running script: Unnamed Script
>>> Script PASSED: Unnamed Script
```

**Description**

`Script.from_yaml` reads exactly three top-level keys (`name`, `description`,
`steps`) with `data.get(...)` defaults and never looks at what else is in the
mapping. Every *other* misspelling class in this DSL now fails loudly, by
deliberate decision across rounds 7 and 8 — action, sensor, condition, setting,
and (round 7 L3, refined in round 8) step parameter:

```
Unknown action: opne. Use: add_schedule, assert, battery, close, …
Unknown sensor: insde. Use: inside, outside
Unknown condition: door_opne. Use: auto_off, auto_on, …
Unknown setting: holdtim. Use: auto, autoretract, battery, …
Unknown parameter(s) for wait: duration. Use: seconds (plus the annotations comment, description, note)
```

Top-level keys are the last silent class, and they have the worst blast radius:
a step-parameter typo loses one step, a `steps:` typo loses the entire test suite
while still reporting success. The exact failure mode round 7 wrote L3 about —
"a door cycle silently became a no-op that still exits 0" — reappears one level
up, and this time it is the whole file.

`steps: []` legitimately means "no steps", so the check has to be on unknown
keys, not on emptiness.

**Recommendation**

In `Script.from_yaml`, after `data` is known to be a dict:

```python
unknown = sorted(set(data) - {"name", "description", "steps"})
if unknown:
    raise ScriptError(
        f"Unknown top-level key(s): {', '.join(unknown)}. Use: description, name, steps"
    )
```

That matches the `Unknown X: y. Use: …` shape the other five errors now share
(round-8 L4), and it is a load-time failure, so `--list-scripts` and `list` will
also surface it via the existing `(Error loading: …)` path. Consider also
warning when a script has zero steps — legal, but almost never intended.

---

### L1 (Low) — every startup bind/resolve failure exits with a raw Python traceback

**Files:** `src/powerpetdoor/simulator/cli.py:684` (`await simulator.start()`),
`src/powerpetdoor/simulator/cli.py:279` (control channel `start_server`),
`src/powerpetdoor/simulator/cli.py:1103-1131` (the `try` that catches only
`KeyboardInterrupt`).

**Reproduction**

Most common daemon error there is — port already taken:

```
$ ppd-simulator --port 3800 --daemon        # another daemon already on 3800
rc=1
output lines: 30
Traceback (most recent call last):
  File "/home/prez/src/pypowerpetdoor/.venv/bin/ppd-simulator", line 10, in <module>
   [...]
    raise OSError(err.errno, msg) from None
OSError: [Errno 98] error while attempting to bind on address ('0.0.0.0', 3800): [errno 98] address already in use
```

Three more, same shape:

```
$ ppd-simulator --port 99999 --daemon -r 1
OverflowError: bind(): port must be 0-65535.

$ ppd-simulator --port -5 --daemon -r 1
OverflowError: bind(): port must be 0-65535.

$ ppd-simulator --host 300.1.1.1 --daemon -r 1
socket.gaierror: [Errno -2] Name or service not known
```

And the control channel, where the *door* port bound fine and only the derived
`--daemon` control port collided — stdout is completely empty, so nothing tells
the operator which of the two ports is the problem except the traceback's last
line:

```
$ ppd-simulator --port 3860 --daemon -r 2      # something else holds 3861
rc= 1
--- stdout ---

--- stderr tail ---
OSError: [Errno 98] error while attempting to bind on address ('127.0.0.1', 3861): [errno 98] address already in use
```

**Description**

The client half of this product handles the mirror-image failure beautifully —
`ppd-simulator-ctl` answers `Connection refused to 127.0.0.1:39999` and
`Error: Connection refused - simulator not running on 127.0.0.1:39999`. The
server half answers a 30-line traceback through asyncio internals, in which the
one useful line is last. Ports 0–65535 and "is this port free" are checkable
before anything is bound; the `--scripts-dir` typo already gets exactly that
treatment (`cli.py:1019-1020`, `parser.error("--scripts-dir …: not a
directory")`, `rc=2`), so the pattern and the precedent both exist in this file.

The tracebacks also print absolute paths from the machine that built the venv,
which is noise in a CI log and mild information disclosure in a shared one.

**Recommendation**

- Validate `--port`, `--daemon CONTROL_PORT` and `--host` in `main()` with
  `parser.error(...)` (range check; `socket.getaddrinfo` for the host) so
  nonsense values fail at `rc=2` with a usage line like every other bad argument.
- Wrap the `asyncio.run(run_simulator(...))` call in `except OSError as e:` and
  print an operator sentence naming the role of the port that failed, e.g.
  `Cannot start: door port 3800 is already in use (another simulator? use --port)`
  and `Cannot start: control port 3861 is already in use (use --daemon PORT)`.
  Exit 1. Keep the traceback behind `--debug`.

---

### L2 (Low) — an unusable `--history` path is detected, logged, and then used anyway: 4 tracebacks and 15 "Press ENTER to continue" modals in a 3-command session

**File:** `src/powerpetdoor/simulator/commands/history.py:103-107`.

**Reproduction**

`/tmp/ppd9/hdir` is a directory. `ppd-simulator --history /tmp/ppd9/hdir` under a
real PTY, driven with `battery`, `status`, `shutdown`:

```
WARNING lines: 1
2026-08-22 15:36:26,310 [WARNING] Could not create history file /tmp/ppd9/hdir: [Errno 21] Is a directory: '/tmp/ppd9/hdir'
IsADirectoryError occurrences: 4
'Press ENTER to continue' occurrences: 15
--- first traceback block ---
Traceback (most recent call last):
  File ".../prompt_toolkit/buffer.py", line 406, in load_history_done
    f.result()
  File ".../prompt_toolkit/buffer.py", line 394, in load_history
```

`ppd-simulator-ctl` shares the code and the symptom:

```
ctl: IsADirectoryError count = 3
ctl: 'Press ENTER to continue' count = 11
IsADirectoryError: [Errno 21] Is a directory: '/tmp/ppd9/hdir'
```

A path whose parent does not exist behaves the same way:

```
$ ppd-simulator --port 3983 --history /proc/1/nope
Exception [Errno 2] No such file or directory: '/proc/1/nope'
… Press ENTER to continue…  Press ENTER to continue…
```

**Description**

The code already knows the path is unusable — it catches the `OSError` and logs
a correct, well-worded warning — and then hands the same path to
`FileHistory()` regardless:

```python
103:                try:
104:                    _create_private_file(history_file)
105:                except OSError as e:
106:                    logger.warning(f"Could not create history file {history_file}: {e}")
107:                self._history = FileHistory(str(history_file))
```

`FileHistory` then raises inside the running prompt application on every load and
every store, so prompt_toolkit's unhandled-exception path dumps a traceback and
puts up a *modal* `Press ENTER to continue…` that the operator has to dismiss —
repeatedly, for the life of the session. This is the "wizards you MUST progress
through" failure mode, produced by a one-character typo in a path.

The class already has a working fallback (`InMemoryHistory`) two lines above, and
`History` already supports `"none"`, so the degraded mode is free.

**Recommendation**

Fall back on the error instead of proceeding:

```python
try:
    _create_private_file(history_file)
except OSError as e:
    logger.warning(f"Could not use history file {history_file}: {e}; history is in-memory for this session")
    self._history = InMemoryHistory()
else:
    self._history = FileHistory(str(history_file))
```

Optionally also pre-flight `--history` in both `main()`s the way `--scripts-dir`
is pre-flighted, so an obviously wrong value fails at `rc=2` before the door
server is started.

---

### L3 (Low) — the CHANGELOG guard added for round-8 L3 is scoped to `pull_request`, and every commit in this repository has landed by `push`

**File:** `.github/workflows/test.yml:62-87`.

**Reproduction**

The job's own shell, applied to `da31ae2` — the round-7 fix commit the guard was
written about — correctly catches it:

```
$ base="da31ae2^"; changed="$(git diff --name-only "$base"...da31ae2)"
$ if echo "$changed" | grep -q '^src/' && ! echo "$changed" | grep -q '^CHANGELOG\.md$'; then …
::error::src/ changed but CHANGELOG.md did not.
Changed files under src/:
src/powerpetdoor/client.py
src/powerpetdoor/door.py
src/powerpetdoor/framing.py
src/powerpetdoor/simulator/cli.py
…  (12 files)
-> job would exit 1
```

But the job is gated on an event this repository does not produce:

```yaml
# .github/workflows/test.yml
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]
…
  changelog:
    name: Changelog
    if: github.event_name == 'pull_request'
```

```
$ git log --merges --oneline | wc -l
0
$ git rev-list --count HEAD
67
$ git log --format='%s' | grep -cE '\(#[0-9]+\)$'
0
$ git remote -v
origin	git@git.neuromancy.net:pypi/powerpetdoor.git (fetch)
```

67 commits, zero merges, zero `(#N)` subject markers, and the canonical remote is
a Gitea instance whose runner is what `test.yml` describes ("The CI host is a
single shared act_runner"). The workflow's own `push:` trigger — the one that
actually fires here — is excluded by the job's `if:`.

**Description**

Round-8 L3's whole point was that a *fix commit* shipped two breaking script-DSL
changes with no CHANGELOG entry and nothing noticed. The guard that was added is
correct and would have caught it; it just does not run on the event by which that
commit, and every other commit in this history, arrived. On a squash-merge
workflow the merge-commit count would also be zero, so this is evidence rather
than proof that no PRs exist — but the `if:` demonstrably excludes `push`, which
is half of the workflow's own trigger set, and that alone is worth closing.

**Recommendation**

Drop the `if:` and pick the base ref per event:

```yaml
- name: CHANGELOG.md moves whenever src/ does
  run: |
    if [ "${{ github.event_name }}" = "pull_request" ]; then
      base="origin/${{ github.base_ref }}"; range="$base...HEAD"
    else
      range="${{ github.event.before }}..${{ github.sha }}"
    fi
    changed="$(git diff --name-only $range)"
    …
```

Keep an opt-out for genuinely non-user-visible changes (a `[skip changelog]`
token in the commit message) so the guard does not become the thing people work
around.

---

### L4 (Low) — `ppd-simulator-ctl ""` hangs for the whole `--timeout` and then blames the daemon for something the daemon deliberately did

**Files:** `src/powerpetdoor/simulator/cli.py:333-335` (blank lines discarded),
`src/powerpetdoor/simulator/ctl.py:272-277` (the message).

**Reproduction**

```
$ time ppd-simulator-ctl -p 3801 ""
Response timeout after 5.0s waiting for 127.0.0.1:3801 (the command may still be running; raise --timeout)
rc=1 elapsed=5.199612079s

$ ppd-simulator-ctl -p 3801 "   "
Response timeout after 5.0s waiting for 127.0.0.1:3801 (the command may still be running; raise --timeout)
rc=1
```

**Description**

The daemon's control handler skips empty lines by design:

```python
333:                cmd = line.decode(errors="replace").strip()
334:                if not cmd:
335:                    continue
```

so an empty or whitespace-only command can *never* be answered. ctl does not
know that, waits out the full silence budget, and then offers advice that is
guaranteed to be wrong in both halves: the command is not "still running" (it was
never a command), and raising `--timeout` only makes the hang longer. A shell
wrapper that expands an unset variable — `ppd-simulator-ctl -p $PORT "$CMD"` —
lands here immediately, and in `ctl -i` a stray Enter is already handled locally,
so this is specifically the one-shot path.

The same "no answer is coming" shape also produces an unexplained result for an
over-long line (asyncio's 64 KiB `readline` limit):

```
$ ppd-simulator-ctl -p 3801 "status xxxx…"      # 70 007 bytes
Connection closed without response from 127.0.0.1:3801
rc=1
# daemon log, broadcast to every other ctl session:
2026-08-22 15:43:02,354 [ERROR] Control client error: Separator is found, but chunk is longer than limit
```

**Recommendation**

- In `ctl.main()`, refuse an empty/whitespace-only command locally before opening
  a socket: `parser.error("empty command")` (`rc=2`), matching the existing
  no-argument path that prints the epilog and exits 1.
- If a blank line does reach the daemon, answer it rather than dropping it —
  `ERROR: Empty command` costs one line and removes an unanswerable request from
  the protocol.
- Give the reader an explicit `limit=` and convert the overflow into
  `ERROR: Command line too long (max N bytes)` at INFO, so the daemon does not
  log an asyncio internal at ERROR into every other operator's session.

---

### T1 (Trivial) — out-of-range numeric options surface as raw socket/OS errors instead of argument errors

**Files:** `src/powerpetdoor/simulator/ctl.py:712-721` (`--timeout`),
`src/powerpetdoor/simulator/ctl.py:258-260` (`sock.settimeout`),
`src/powerpetdoor/simulator/cli.py:982-988` (`--run-for`).

**Reproduction**

```
$ ppd-simulator-ctl -p 3801 -t 0 status
Error: [Errno 115] Operation now in progress
rc=1
$ ppd-simulator-ctl -p 3801 -t -1 status
Error: Timeout value out of range
rc=1
$ echo status | ppd-simulator-ctl -p 3811 -t 0 -i
Error: Connection error: [Errno 115] Operation now in progress
$ ppd-simulator --port 3880 --daemon --run-for -5
2026-08-22 15:24:22,820 [INFO] Run time (-5.0s) elapsed, shutting down
2026-08-22 15:24:22,820 [INFO] Door simulator stopped
```

**Description**

`--timeout 0` reads to a user as "no timeout"; it actually puts the socket in
non-blocking mode and yields `EINPROGRESS`, an errno no operator should have to
decode from a CLI flag. `--timeout -1` leaks `settimeout`'s own `ValueError`
text. `--run-for -5` is accepted and silently means "shut down immediately",
with a log line that reads as if five negative seconds elapsed.

**Recommendation**

Validate in the parser: `--timeout` must be `> 0` (or accept `0`/`none` as an
explicit "wait forever" and pass `None` to `settimeout`, which is what a user
typing `0` almost certainly wants); `--run-for` must be `> 0`. `parser.error`
gives all three the usage line every other bad argument already gets.

---

## Round 8 Fix Verification

All eleven items verified against running binaries. Nothing regressed.

| Item | Status | Evidence |
|------|--------|----------|
| **F-M1** — listing/completion no longer re-parses every YAML on the door server's loop | ✅ Verified, and the loop claim independently confirmed | 200-script `--scripts-dir`. In-process: `script_completer("")` **82.1 ms cold → 5.3 ms warm** (207 entries); with the cache cleared, `script_completer("bulk_12")` **7.9 ms** returning exactly the 10 matches, and `script_completer("BULK_12")` **4.3 ms** — the pre-filter is case-insensitive as `matches_completion_prefix` promises, so uppercase input is not silently starved. Under a real PTY driving `ppd-simulator --scripts-dir <200 files>`: `run ⇥` → menu rendered in **32.7 ms** cold, **4.4 ms** warm. Crucially, a door-protocol client pinging at 50 Hz *through the Tab press* measured `n=298 min=0.1ms p50=0.1ms p99=0.3ms max=3.1ms` — `ThreadedCompleter` means the emulated device is not stalled at all, which is the part the round-8 fix could only assert structurally. |
| **F-M1** — `_describe_script` memoized on `(path, mtime, size)`, still live | ✅ Verified | `list` → `edit_me: version one`; edit the file; `list` → `edit_me: version two` immediately. A newly-dropped broken file is reported on **every** listing, not just the first (`daemon warnings for broken.yaml: 2` after two `list`s) — "a parse cache, not a report cache", as the docstring claims. |
| **F-M1** — `ctl list` daemon round trip | ✅ Verified | 200 scripts, 5 consecutive runs: **18.0, 5.0, 7.8, 5.0, 4.9 ms**. |
| **F-L1** — the daemon-mode doc section's quoted strings | ✅ Verified byte-for-byte | `docs/simulator.md:319-325` quotes two refusals; live: over ctl, `run linked` → `ERROR: Script 'linked' resolves outside /tmp/ppd9/sd2 and cannot be run by name; move it into the directory (paths are not accepted over the control channel)`; `run /tmp/ppd9/out/outside.yaml` → `ERROR: Script paths are not allowed over the control channel; use a bare script name (see 'list')`; `run help` → `script: Script name (paths are not accepted over the control channel) [required]`. Locally, `--script linked` → `… move it into the directory or run it by path`, exactly as the doc's next sentence says. |
| **F-L2** — the set/toggle table matches the implementation | ✅ Verified by executing every row | All **9** documented `set` rows (`power, auto, inside, outside, autoretract, safety_lock, cmd_lockout, hold_time, battery`) ran in one script → `>>> All scripts PASSED`. All **7** documented `toggle` rows ran → `PASSED`. `toggle hold_time` and `toggle battery` both fail as documented: `Unknown setting to toggle: hold_time (that name belongs to the 'set' action). Use: auto, autoretract, cmd_lockout, inside, outside, power, safety_lock`. |
| **F-L3** — CHANGELOG covers rounds 7-8 | ✅ Verified | `[Unreleased]` carries the annotation entry (`note:`, `comment:`, `description:` … "read by nothing"), the strictness change, the shadow marker, the out-of-directory containment and the ctl `--timeout` semantics. |
| **F-L3** — a CI job fails if a fix commit skips the changelog | ⚠️ Verified present and logically correct, **but scoped out of the event it needs** | The job's shell run against `da31ae2` exits 1 with the right file list; the job's `if: github.event_name == 'pull_request'` excludes the workflow's own `push:` trigger, and this repo's 67 commits contain 0 merges and 0 `(#N)` markers. Filed as **L3**. |
| **F-L4** — `Unknown X` errors name their valid alternatives | ✅ Verified, all six | `Unknown condition: door_opne. Use: auto_off, auto_on, … safety_lock_on` (20 names) / `Unknown setting: holdtim. Use: auto, autoretract, battery, cmd_lockout, hold_time, inside, outside, power, safety_lock` / `Unknown setting to toggle: pwoer (that name belongs to the 'set' action). Use: …` / `Unknown assertion condition: door_stat. Use: … total_open_cycles` (12 names) / `Unknown sensor: insde. Use: inside, outside` / `Unknown action: opne. Use: add_schedule, assert, … wait_for` (19 names). One consistent `Use: …` shape across all six. |
| **Owner decision** — `note`/`comment`/`description` accepted on any step | ✅ Verified | A script using all three on three different actions → every step ran, `>>> Script PASSED`, `rc=0`, and the step log echoes them (`log(message=hello, note=this is a note)`, `wait(seconds=0.1, comment=settle)`, `close(description=make sure it is shut)`). |
| **Owner decision** — a typo'd *real* parameter still errors | ✅ Verified | `{action: wait, duration: 1}` → `Script error at step 1: Unknown parameter(s) for wait: duration. Use: seconds (plus the annotations comment, description, note)`, `>>> All scripts FAILED`, **rc=1**. |
| **Owner decision** — the parameter check is per-action | ✅ Verified | A no-parameter action reports its own shape rather than "Use: none": `{action: close, hold: true}` → `Unknown parameter(s) for close: hold. close takes no parameters (plus the annotations comment, description, note)`. |

---

## Areas Reviewed With No Findings

**Package export surface.** `__all__` is exactly the public namespace: 0 names in
the namespace but not in `__all__`, 0 in `__all__` but missing from the
namespace, 0 duplicates, `from powerpetdoor import *` yields 121 names,
`__version__ == "0.3.0"` matching `pyproject.toml`. Round-5 L3 is fully closed:
**0 of 121** exported names are undocumented across `docs/*.md` + `README.md`.

**`docs/client.md`'s "a `notify=True` future never hangs forever" claim.**
Executed against a client started on a dead port: `available: False`,
`effective_timeout: 4.0`, and the future completed in **0.1 s** with
`ConnectionError: Connection closed before a response was received` — the
documented behaviour, exactly. (The facade's failure to inherit it is **M2**.)

**Terminal conditions.** Same PTY session under three environments, counting SGR
sequences in the raw byte stream: `TERM=xterm-256color` → 3 distinct SGR, 2 of
them color-setting (`\x1b[0;38;5;102m`, `\x1b[0;38;5;34;1m`); `NO_COLOR=1` → 2
SGR, **0** color-setting; `TERM=dumb` → **0** SGR and the plain-input fallback.
Both accessibility escape hatches work. Neither is mentioned in
`docs/simulator.md`; not worth a finding, but a one-line note under "Interactive
Mode" would help a colour-blind or screen-reader user find them.

**Plain-stdin interactive fallback.** `printf 'status\nbattery 42\nbattery\nbogus\nshutdown\n' | ppd-simulator --port 3993`
prints the full help banner, then `0.0.0.0:3993> >>> Current State: …`,
`>>> Battery set to 42%`, `>>> Battery: 42%`,
`>>> Unknown command: bogus. Type 'help' for commands.`, `>>> Shutting down...`.
Prompt echo, sanitization and the `>>>` prefix are identical to the
prompt_toolkit path.

**ctl local/remote command split.** `ppd-simulator-ctl -p 3811 help` (daemon
generated) lists 8 groups and correctly **omits** `history`, `clear` and `exit` —
the three commands the same invocation refuses (`ERROR: Unknown command: history.
Type 'help' for commands.`, `rc=1`). Help and behaviour agree; the local
commands are available in `ctl -i`, where they belong.

**Argument validation that does exist.** `--scripts-dir /etc/passwd` → argparse
usage + `rc=2`; `--firmware 1.2` / `--firmware a.b.c` / `--hardware 1` → usage +
`rc=2`; `--daemon abc` → usage + `rc=2`; `--script nosuchscript --oneshot` →
`Unknown script: nosuchscript. Available: basic_cycle, full_test_suite,
obstruction_test, pet_presence_test, power_lockout_test, safety_lock_test,
schedule_test`, `rc=1`. The gap is bind-time values only (**L1**).

**Interruption of `ctl`.** `ppd-simulator-ctl run long wait` + SIGINT → `rc=130`
with `Interrupted.`, and the daemon's script is still stoppable afterwards
(`OK: Stopping script: Long Script`). This is the behaviour `ppd-simulator`
should have and does not (**H1**).

**Schedule and notification command surfaces.** `schedule help` lists all 8
subcommands; `schedule clear` on an empty set → `OK: No schedules to clear`
(idempotent); `schedule delete help` renders the arg table with its bound
(`index: Schedule index [required] (min: 0)`); `notify` renders all five flags,
`notify inside_on on` → `OK: Notification inside_on: ON`, `notify all on` →
`ERROR: Unknown notify subcommand: all / Available: inside_off, inside_on,
low_battery, outside_off, outside_on`. Everything is discoverable and the errors
name the alternatives. `notify` is the one command family with no bulk form while
`broadcast all` and `schedule clear` exist — a feature gap rather than a defect,
noted for the record.

**Script DSL error reporting under the new strictness.** Six distinct
misspelling classes exercised (above). `steps: []` runs and passes, which is
correct. `- action:` missing → `Step i: missing 'action' field`. Malformed YAML →
`Invalid script YAML: …` with PyYAML's line/column, surfaced identically through
`--script`, `list` and `--list-scripts`.

**Process/daemon lifecycle other than H1.** Two daemons started and stopped
cleanly over `ctl shutdown`; control connections open/close without the round-5
`Broken pipe` ERROR; `Control connection from/closed from` logged at INFO;
prompt colour flips on door-client connect/disconnect via `STATUS: clients=N`.

---

## Notes on Scope

No finding proposes a wire-protocol change. H1, L1, L2, L4 and T1 are entirely in
the CLI/ctl process layer; M1 is packaging metadata; M2 is the Python facade's
error handling above the client; M3 is the YAML DSL's loader; L3 is CI
configuration. The only wire traffic generated during this review was ordinary
`PING`/`PONG` and `CONFIG`/`COMMAND` messages used as measurement instruments
(the ping-RTT probe that proves the completer no longer stalls the loop), and
none of it required or suggested a protocol modification.
