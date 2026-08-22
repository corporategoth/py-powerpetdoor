# Frontend Developer Analysis — Round 4

Persona: frontend engineer; scope is the simulator terminal front end (`ppd-simulator`,
`ppd-simulator-ctl`, `prompt_common.py`, `commands/*`) plus the library's public API and
docs as the "developer front end" (README.md, CHANGELOG.md, docs/simulator.md,
docs/client.md, docs/door.md) at commit `f9b9b59`. No web UI exists.

Everything below was verified against the code **and** live binaries: a daemon started
with `--scripts-dir`, one-shot and piped ctl sessions, piped `ppd-simulator` sessions,
raw control-channel sockets, `--oneshot` CI runs, `--wait-for-client` runs with a real
TCP client connecting and disconnecting mid-script, SIGINT/SIGPIPE/`kill -9` teardown of
ctl clients, completer probes in the ctl process context, and execution of every
documented programmatic example. All spawned processes were killed and all temporary
files removed.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 1 |
| Medium   | 3 |
| Low      | 3 |
| Trivial  | 5 |

Every round-3 fix landed and works (details in
[Round 3 Fix Verification](#round-3-fix-verification)) — the H1 log flood is genuinely
gone (0 lines where round 3 measured 609–732), `status_print` flushing is correct,
`render_result` is used consistently, and the stderr `LOG:` streaming does not break the
documented CI recipe's stdout parsing. The findings below are all in the **new** surface:
the `stop` command has a case where it silently does nothing and reports the run as
PASSED (High), the new queue-depth indicator systematically under-reports, ctl's
completer contradicts the freshly rewritten doc sentence about it, and the flush fix
that was applied to `ppd-simulator` was not applied to `ppd-simulator-ctl` or to one
remaining progress line.

## Findings

### High

#### H1. `stop` issued during a script's **final** step is silently discarded — the run reports `Script PASSED` and exits **0**, the opposite of what the docs and CHANGELOG promise

**Files:** `src/powerpetdoor/simulator/scripting.py:294-307` (`_run_steps`: the
`_stop_requested` check is only at the *top* of each iteration; there is no check after
the loop before `return True`), `src/powerpetdoor/simulator/commands/scripts.py:128-141`
(`stop_script` returns success unconditionally once a script is running)

`docs/simulator.md:241` promises: "Stop the **running script** at its next step boundary
(**the run then reports FAILED**)", and `CHANGELOG.md:33` announces the command on the
same terms. That holds only when at least one step remains. Because the loop is

```python
for step in script.steps:
    if self._stop_requested:
        logger.info("Script stopped by request")
        return False
    await self._execute_step(step)
...
return True     # <- no _stop_requested check here
```

a stop that lands during the last step is never observed: the step finishes, the loop
ends, and the runner returns `True`.

Verified live against the daemon, three ways:

- **Single-step script** (`wait 12`): `run slow_quiet wait` started, `stop` at T+1s →
  `OK: Stopping script: Slow Quiet Script`. A second `stop` at T+7s → `OK` again.
  `status` reported `Script: running "Slow Quiet Script"` throughout. Final result:
  **`OK: Script PASSED: Slow Quiet Script`, exit 0**. The daemon log shows
  `Script 'Slow Quiet Script' completed successfully` and **no** `Script stopped by
  request` line. For a one-step script, `stop` is a complete no-op that reports success.
- **Two-step script** (`wait 1`, `wait 8`), stop at T+3s (inside the last step) →
  `OK: Stopping script: Two Step Script`, result **`OK: Script PASSED`, exit 0**.
- **Three-step control** (`wait 4` ×3), stop at T+1s (inside the first step) →
  `ERROR: Script FAILED: Three Step Script`, exit 1. Correct.

Impact is worst exactly where `run … wait` matters: a CI job (or an operator) that aborts
a run gets `OK: Script PASSED` and **exit 0**. The abort is indistinguishable from a
clean pass — a false success signal from the one command whose entire purpose is to
carry a trustworthy exit code. It also makes `stop` non-deterministic from the user's
point of view: the same command on the same script does or does not take effect depending
on where the run happens to be.

Contributing factor: plain `wait N` steps are not interruptible (`asyncio.sleep`;
`_stop_event` is only awaited by `_wait_for_status`, `scripting.py:453-473`), so the
"final step" window is as long as the final `wait`. `wait_for` steps *are* interruptible
and do fail correctly.

Existing tests cover stop-during-`wait_for` (`tests/simulator/test_scripting.py:505`) and
stop-with-a-following-step (`:528`), but never stop-during-the-last-step, which is why
this passes CI.

**Recommendation:** Re-check `self._stop_requested` after the step loop and return
`False` (logging `Script stopped by request`) if it is set; and have `stop_script` say
what it actually guarantees. Optionally make plain `wait` interruptible by racing
`asyncio.sleep(seconds)` against `self._stop_event.wait()`, which would also shrink the
window and match the `wait_for` behavior operators already see.

### Medium

#### M1. ctl's `run` tab completion offers local YAML files and directories that the daemon always refuses — and the doc sentence rewritten in round 3 now says the opposite

**Files:** `src/powerpetdoor/simulator/scripting.py:769-785` (`script_completer` always
appends cwd `*.yaml` files and subdirectories), `docs/simulator.md:316-318`

Round 3's M4 recommendation had two halves; the doc half was applied and the completer
half was not, which leaves the doc stating something false. `docs/simulator.md:316-318`
now reads:

> `ppd-simulator-ctl` is a separate process that never learns the daemon's
> `--scripts-dir`, so **its completer offers the built-in names only** — use `list` to
> see the rest.

Verified with a completer probe in a real ctl process context (`import ...simulator.ctl`
first, then `SimulatorCompleter().get_completions("run ")`), run from a directory holding
six YAML files:

```
basic_cycle … schedule_test        (the 7 built-ins)
three_step.yaml   [(local file)]
two_step.yaml     [(local file)]
failing.yaml      [(local file)]
slow_steps.yaml   [(local file)]
slow_quiet.yaml   [(local file)]
my_custom.yaml    [(local file)]
help
```

and from a parent directory: `scripts/  [(directory)]`.

Every one of those is guaranteed to fail, verified live:

- `ppd-simulator-ctl run scripts/my_custom.yaml` →
  `ERROR: Script paths are not allowed over the control channel; use a bare script name
  (see 'list')`, exit 1
- `ppd-simulator-ctl run my_custom.yaml` → `ERROR: Unknown script: my_custom.yaml.
  Available: … my_custom …`, exit 1

The second case is the sharp one: completion offers `my_custom.yaml`, which fails, while
the name that *does* work (`my_custom`, sitting right there in the error's own
`Available:` list) is the one completion cannot offer. Tab completion is actively
steering the user to the broken form of a working name.

**Recommendation:** Give `script_completer` a flag (or give ctl a wrapper completer) that
suppresses the cwd file/directory branches when script paths are not allowed, so ctl
offers only names; and drop or re-word the "built-in names only" sentence to match
whatever ships.

#### M2. The new `(N queued)` indicator under-reports by one, so the commonest case — one script waiting behind a `run … wait` — displays as "nothing pending"

**Files:** `src/powerpetdoor/simulator/commands/scripts.py:98-106` (`script_status`
reports `script_queue.qsize()`), `src/powerpetdoor/simulator/cli.py:369-392`
(`_process_script_queue` dequeues an item as soon as one exists, then blocks inside
`script_runner.run()` waiting for the lock)

The queue consumer pulls an item off the `asyncio.Queue` immediately and *then* awaits the
runner lock, so a script that is pending-but-dequeued is invisible to `qsize()`.

Verified live against the daemon:

| Situation | Actually pending | `status` / `list` reported |
|---|---|---|
| `run slow_steps wait` in flight, then 3× `run my_custom` | 3 | `Script: running "Slow Steps Script" (2 queued)` |
| `run slow_steps wait` in flight, then 1× `run my_custom` | 1 | `Script: running "Slow Steps Script"` — **no queue shown at all** |
| 3× `run slow_steps` (the running one came from the queue) | 2 | `(2 queued)` — correct |

So the count is right when the running script came out of the queue and wrong by one
whenever the runner is held by a `run … wait` (or a startup `--script`) — i.e. precisely
the CI-plus-operator scenario the feature was added for. In the single-pending case the
indicator disappears entirely, which reads as "nothing else is waiting" when something is.

`docs/simulator.md:247` and `:368` and `CHANGELOG.md:34-35` all state the count without
qualification.

There is also no way to see *what* is queued — only a number — so an operator who wants
to know whether the thing they queued five minutes ago is still pending cannot find out.

**Recommendation:** Track pending runs in the handler rather than inferring them from
`qsize()` — e.g. have `_process_script_queue` record the ref it dequeued while it waits
on the lock and add it to the count, or replace the raw `asyncio.Queue` with a small
queue object that exposes `pending_names()`. Reporting the names (`Script: running "A"
(queued: B, C)`) would close the visibility gap in the same change.

#### M3. `ppd-simulator-ctl -i` output is block-buffered off a TTY: 12 s of "live" log streaming produced **0 bytes**

**Files:** `src/powerpetdoor/simulator/ctl.py:445` (`print(...)` for `LOG:` lines),
`:425, 583, 593, 602, 610` (all session output); contrast `:290-294`, the one-shot
wait-run path, which correctly passes `flush=True`

Round 3's M2 fix introduced `cli.status_print()` for the simulator side; ctl was not
given the equivalent. `ctl -i`'s only flush is the plain-input prompt
(`_basic_readline`, `:353-354`), which is written *before* awaiting input — so while a
command is in flight nothing flushes at all.

Verified with a driver that redirected `ctl -i` stdout to a file and sampled it:

```
baseline bytes: 141
t=2s  bytes=141 INFOlines=0
t=4s  bytes=141 INFOlines=0
t=6s  bytes=141 INFOlines=0
t=8s  bytes=141 INFOlines=0
t=10s bytes=141 INFOlines=0
after completion bytes=1190 INFOlines=16
```

Twelve seconds of `run slow_steps wait` produced not one byte; all sixteen streamed
`LOG:` lines appeared at once when the next prompt flushed. `docs/simulator.md:380` says
"Daemon log output is streamed live into the session" — true on a terminal, false for
`ctl -i > session.log`, `ctl -i | tee`, `ctl -i | grep`, a container capturing stdout, or
a supervisor. It is also inconsistent with ctl's own one-shot path, which flushes each
line explicitly, and with `ppd-simulator`, which was fixed in round 3.

The persona-relevant symptom is exactly the one round 3's M2 was about: a redirected
session shows a prompt and then nothing, so "still running" and "hung" are
indistinguishable.

**Recommendation:** `sys.stdout.reconfigure(line_buffering=True)` once in
`interactive_mode_async` when stdout is not a TTY (prompt_toolkit is not driving that
case anyway), or route ctl's session output through a flushing helper as `cli.py` does.

### Low

#### L1. One progress line was missed by the flush fix, and it is the one that explains an aborted `--wait-for-client` run

**Files:** `src/powerpetdoor/simulator/cli.py:438` — bare
`print(">>> Client disconnected, stopping scripts")` inside the per-script loop; the
identical message eight lines earlier (`:428`) correctly uses `status_print`. Every other
progress line in `_run_startup_scripts` was converted.

Verified live: `ppd-simulator --port 39600 -s slow_steps -s my_custom --wait-for-client
--scripts-dir … > wfc.out`, with a real TCP client that connected and then disconnected
during script 1. The log ends at:

```
>>> Running script: Slow Steps Script
>>> Script PASSED: Slow Steps Script
```

and stops. The `>>> Client disconnected, stopping scripts` line never appears — not while
the (still-alive) process kept running, and not after `kill -TERM`, because the buffer
dies with the process. `grep -c "Client disconnected, stopping scripts" wfc.out` → **0**.
An operator reading the redirected log sees the second script simply never run, with no
stated reason. (The stderr INFO log does carry `Simulator: Client disconnected`, but not
the "stopping scripts" consequence.)

`CHANGELOG.md:69-70` claims script progress "appear[s] in redirected output (and
survive[s] SIGTERM)"; for this one line it does not.

**Recommendation:** `status_print(">>> Client disconnected, stopping scripts")` at
`cli.py:438`.

#### L2. Queued script runs can only be cancelled one at a time, by repeating `stop` and guessing how many times

**Files:** `src/powerpetdoor/simulator/commands/scripts.py:128-141`;
`src/powerpetdoor/simulator/cli.py:369-392`

`stop` stops the *current* run only; `_run_steps` resets `_stop_requested` for the next
one, so the next queued script starts immediately. There is no `stop all`, no queue
clear, and no way to remove a specific queued entry. Verified live with three queued
`slow_steps`:

```
run slow_steps ×3    ->  Script: running "Slow Steps Script" (2 queued)
stop                 ->  OK   (1.5s later) Script: running "Slow Steps Script" (1 queued)
stop                 ->  OK   (1.0s later) Script: running "Slow Steps Script"
stop                 ->  OK   (1.0s later) Script: none running
```

Three round trips, each of which has to wait for the next script to actually start before
it can be stopped — and the number of `stop`s needed is exactly the count M2 gets wrong.
The persona's bulk-actions rule ("I should never have to click through a list item by
item to perform a task on each item") is the direct match. Schedules already have
`schedule clear`; scripts have no equivalent.

**Recommendation:** Add a queue-draining form — `stop all` (or `run clear`) — that empties
the pending queue and stops the in-flight run in one command, reporting how many entries
it dropped.

#### L3. A pending stop is invisible: `stop` returns success immediately and `status` keeps saying `running`, with nothing to say the stop was registered

**Files:** `src/powerpetdoor/simulator/commands/scripts.py:27-35`
(`format_script_status` has only "none running" / "running"), `:128-141`

`stop` is inherently asynchronous — it takes effect at a step boundary, and a plain
`wait N` step is not interruptible — but nothing in the UI reflects the in-between state.
Verified live: after `stop` on a script sitting in a 12 s `wait`, `status` reported
`Script: running "Slow Quiet Script"` at T+3, T+5 and T+7, identical to the pre-stop
output, and a second `stop` returned `OK` again. The operator has no way to tell whether
the first `stop` registered, whether the script is ignoring it, or whether they typed it
into the wrong terminal — the persona's "never wondering if something worked" case. (In
the specific run above the answer was "it never registered", per H1 — which is exactly
why the missing signal matters.)

**Recommendation:** Have `script_status()` return the stop-requested flag and render
`Script: stopping "<name>"`, and have a repeat `stop` answer `Stop already requested for:
<name>` rather than a fresh success.

### Trivial

#### T1. `--list-scripts` and `list` disagree on the built-in header

`src/powerpetdoor/simulator/cli.py:978` prints `Available built-in scripts:` while
`commands/scripts.py:112` prints `Built-in scripts:`; the second header
(`Scripts from <dir>:`) is identical in both. Verified live. Two spellings of the same
list in the two places a user looks for it.

#### T2. `InteractiveSession.format_output` is a dead, unsanitized duplicate of `render_result`

`src/powerpetdoor/simulator/prompt_common.py:702-714`. Round 3 unified result rendering on
`render_result` (which sanitizes); `format_output` produces the same `>>> ` prefix without
sanitizing and now has **no caller in `src/`** — only its own class docstring
(`:535`), the `input_loop` docstring (`:759`) and two tests
(`tests/simulator/test_prompt_common.py:730,735`), which make it look supported. A future
call site would silently bypass the sanitizer the unification exists to guarantee. Fold
it into `render_result` (or delete it and update the docstrings/tests).

#### T3. ctl's `--help` epilog omits the only exit-code-bearing form and the new `stop`

`src/powerpetdoor/simulator/ctl.py:647-655`. The epilog — which is also what a bare
`ppd-simulator-ctl` prints — lists `status`, `inside`, `-i`, `shutdown`. It does not show
`run <script> wait`, despite `docs/simulator.md:341` making the point that plain `run`
always exits 0 and only the `wait` form reflects the script, nor `stop`, whose meaning
just changed. Two lines in the epilog would put the CI recipe and the breaking change in
front of the user without a doc lookup.

#### T4. `stop` with nothing running doesn't point at `shutdown`

`src/powerpetdoor/simulator/commands/scripts.py:139` returns `No script is running`
(exit 1). Given `stop` was, until this release, an alias for `shutdown`, the single most
likely reason a user types it into an idle simulator is muscle memory. Suggest
`No script is running (use 'shutdown' to stop the simulator)`. Verified live in both the
CLI and ctl. (The error/exit-1 is right — silently shutting down would be far worse.)

#### T5. `Schedule.days_of_week` is documented as ints in one doc and booleans in the other

`docs/simulator.md:482-486` uses `days_of_week=[0, 1, 1, 1, 1, 1, 0]` with the comment
"a list"; `docs/door.md:352-356, 468, 480` uses `[True, True, …]` and "a list of
booleans" for the identically named field of the library's `Schedule`. Round 1's T4 fixed
door.md; simulator.md kept the ints. Both work at runtime (verified: the simulator
example runs and round-trips as `[0, 1, 1, 1, 1, 1, 0]`), and the underlying dataclasses
differ (`door.py:212` is `list[bool]`, `simulator/state.py:202` is bare `list` defaulting
to `1`s), so this is presentation only — but a developer reading both docs gets two
answers for the same concept.

## Round 3 Fix Verification

All eight non-trivial round-3 findings plus the five trivials, re-verified against code
and live binaries:

| Round-3 item | Status |
|---|---|
| H1 control-channel log broadcast feedback loop | **Fixed** — reran the round-3 repro (ctl `-i` piped into `head -3` dying of SIGPIPE, plus a `kill -9`'d fifo-driven `ctl -i`, while `basic_cycle` streamed logs, with a third `ctl -i` observing): **0** `socket.send() raised exception.` lines in the observer session and **0** in the daemon log (round 3 measured 609–732). The observer instead sees one clean `Control client error: [Errno 32] Broken pipe` + `Control connection closed from …`. Guard verified in `cli.py:187-215` (re-entrancy flag, `is_closing()` reaping, `except` reaping) and mirrored in `broadcast_status` (`:286-300`) |
| M1 `--timeout` help describes a command deadline | **Fixed** — `ctl --help` now reads "Seconds of daemon SILENCE tolerated … Any received line restarts it … 'run <script> wait' ignores it entirely and waits as long as the script takes", matching `docs/simulator.md:397` and the live behavior |
| M2 status/progress invisible off a TTY | **Fixed for `ppd-simulator`** — daemon banner (`Simulator started on 0.0.0.0:39400`, `Control channel: 127.0.0.1:39401`) appears in a redirected log within 2 s of start; `-s basic_cycle --oneshot 2>&1 \| cat` now interleaves `>>> Running script` / `>>> Script PASSED` / `>>> All scripts PASSED` in the right places. Residuals: one missed line (L1) and ctl was not covered (M3) |
| M3 ctl one-shot wait-run silent, failure without detail | **Fixed** — `run basic_cycle wait 2>err.log` streams 13 sanitized `LOG:` lines to stderr live while stdout stays a single `OK: Script PASSED: Basic Door Cycle`. The documented recipe `run failing wait 2>run.log \|\| cat run.log` reproduces exactly, ending in `Assertion failed at step 2: door_status: expected 'DOOR_NONSENSE', got 'DOOR_CLOSED'`. Interleaving under `2>&1 \| cat` is sane (logs then result); `$(… 2>/dev/null)` captures exactly one line, so stdout parsing is intact |
| M4 docs over-claimed ctl completion of `--scripts-dir` names | **Partly fixed** — the doc now scopes the claim to the simulator CLI (`docs/simulator.md:99, 316-318`) and that part is accurate, but the replacement sentence ("its completer offers the built-in names only") is itself false (M1 above) |
| M5 busy state invisible / uncontrollable, `stop` == `shutdown` | **Mostly fixed** — `script_status()`/`format_script_status()` exist and are shared by `status` and `list`; live output `Script: none running` and `Script: running "Slow Steps Script" (2 queued)` matches `docs/simulator.md:247`. `stop` is a real command wired to `ScriptRunner.stop()`, appears in the Scripts category of both the daemon-generated and ctl-local help, completes on `sto`+Tab, and is no longer a `shutdown` alias anywhere (`shutdown` shows `(exit, q, quit)` only in the CLI). Residuals: H1 (stop dropped on the last step), M2 (queue count), L2/L3 |
| L1 wrong `--scripts-dir` silently ignored | **Fixed** — `--scripts-dir /tmp/…/nope` → `ppd-simulator: error: --scripts-dir …: not a directory`, exit 2; an existing-but-empty dir logs `WARNING No *.yaml/*.yml scripts found in …` at daemon start; `--list-scripts` prints `Scripts from <dir>:` / `  (none)` |
| L2 `--list-scripts` help said "built-in" only | **Fixed** — `--help` now says "List runnable scripts (built-in, plus --scripts-dir if given) and exit", matching `docs/simulator.md:96` |
| T1 mode-scoped flag grammar / `--daemon --oneshot` advice | **Fixed** — `--oneshot cannot be used without --script`; `--loop, --oneshot cannot be used without --script`; `--daemon --oneshot` → `--oneshot is not available in daemon mode` (one round trip, not two); `--control-host` without `--daemon` → `--control-host requires --daemon` |
| T2 doubled `ERROR: Error: …` | **Fixed** — live: `ERROR: Another script is already running: Slow Steps Script`, `ERROR: Unknown script: bogus. …`, `ERROR: Script paths are not allowed over the control channel; …`. Matches the doc quotes |
| T3 "Unknown **built-in** script" | **Fixed** — `ERROR: Unknown script: bogus. Available: basic_cycle, failing, full_test_suite, my_custom, …` (built-ins and scripts-dir names in one list, correctly labelled) |
| T4 CHANGELOG `notify` alias `n` | **Fixed** — `CHANGELOG.md:23` now reads "Notification commands (`notify`) in simulator CLI"; `n` remains `inside_enable` in help and live |
| T5 bare `ac` phrased like a read-only display | **Fixed** — `ac` → `AC set to disconnected` / `AC set to connected`, `ac connect`/`ac disconnect`/`ac toggle` all "AC set to …"; distinct from `battery` → `Battery: 100%` and `holdtime` → `Hold time: 2.0s` |

## Areas Reviewed With No Findings

- **`stop` discoverability and non-collision with `shutdown`** — `stop` appears in the
  Scripts category of the daemon-generated help (ctl one-shot), the ctl-local interactive
  help, and the CLI startup help block; `stop help` → `stop: Stop the running script`;
  `sto`+Tab completes it. A repo-wide grep found **nothing** still suggesting `stop`
  shuts anything down: `commands/control.py:21` registers `shutdown` with no aliases,
  `set_cli_mode` adds only `exit`/`q`/`quit`, and the only two "stop the daemon" strings
  (`ctl.py:374` banner, `ctl.py:652` epilog) both name `shutdown`. `docs/simulator.md:241,
  257` and `CHANGELOG.md:61-63` document the break on both sides.
- **stderr `LOG:` streaming vs the documented CI recipes** — both documented forms behave
  as written; stdout for a wait-run is exactly one line, `$(...)` capture is clean, and
  `2>&1 | cat` interleaves logs before the result. Streaming is correctly limited to
  `run … wait` (`is_wait_run`, case-insensitive, honors the `r`/`file` aliases); `stop`,
  `status`, plain `run` and unknown commands do not stream. `sock.settimeout(None)` is
  applied only after connect/send, so connection failures still time out.
- **`status_print` flushing** — verified for the daemon banner, control-channel line, the
  interactive help block, `--wait-for-client` progress, per-script `>>> Running/PASSED/
  FAILED`, `>>> Script run #N`, delay notices and `>>> All scripts PASSED`. Only
  `cli.py:438` was missed (L1).
- **`render_result` unification** — the single sanitizing renderer is used by the
  prompt_toolkit CLI loop (`cli.py:775`), `_BasicStdinInput` (`:538, 542`) and both ctl
  interactive paths (`ctl.py:602, 610`); ctl one-shot deliberately prints the raw
  `OK:`/`ERROR:` line (already sanitized in `send_command`) so stdout stays scriptable.
  Only the dead `format_output` remains (T2).
- **Exit codes** — one-shot ctl 0/1 verified for success, script pass/fail, stop-with-no-
  script, busy error, unknown script, path rejection, extra argument, unknown command and
  no-argument invocation; `--oneshot` verified 0 for a passing script and 1 for a failing
  one and an unknown name; SIGINT during a ctl interactive wait-run exits 130 cleanly with
  `Interrupted.` and leaves the daemon's script running (correct — the daemon is
  authoritative).
- **Documented command examples** — every row of the Interactive Mode tables exercised
  live over ctl, including the round-4 additions (`stop`, `list`'s trailing state line,
  `status`'s `Script:` line). `schedule clear` on an empty set answers `No schedules to
  clear` with exit 0.
- **Help parity across clients** — the daemon-generated help and ctl's local help are
  identical except for the local-only commands, and the CLI's copy correctly shows
  `shutdown (exit, q, quit)` and `clear (cls)` where ctl shows a standalone `exit`.
- **Completion machinery** — `run X ` → `wait`; `pet `, `battery `, `schedule ` and
  argument-position completions all still correct; `stop` (no args) yields nothing, as it
  should. Only the ctl path/file suggestions are wrong (M1).
- **Documented programmatic examples** — "Basic Usage", "Triggering Events", "Modifying
  State", "Managing Schedules" and "Running Scripts Programmatically" all executed as
  written. All 37 documented `from powerpetdoor… import …` names import successfully, and
  every `door.*` / `client.* `/ `simulator.*` / `runner.*` attribute used in README.md,
  docs/door.md, docs/client.md and docs/simulator.md exists on the corresponding class.
- **Control-channel hygiene** — `STATUS: clients=n` first, `LOG:`/`STATUS:` skipped while
  awaiting a response, sanitize-then-escape on the daemon side and unescape-then-sanitize
  in ctl (including the streamed `LOG:` path), loopback default with the warning intact in
  `--help`, path traversal refused with an actionable message.
- **README / CHANGELOG coverage** — module trees current, CI examples verified live, and
  the Unreleased section documents the round-3 front-end work. The only inaccuracies are
  the two consequences of findings above: `(N queued)` (M2) and "script progress … survive
  SIGTERM" (L1).
- **docs/door.md and docs/client.md** — unchanged since round 3 apart from the
  `days_of_week` wording; re-verified their public API surface by attribute check
  (no missing names). Only T5 applies.
