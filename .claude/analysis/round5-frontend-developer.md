# Frontend Developer Analysis — Round 5

Scope: the simulator's terminal front end (`cli.py`, `ctl.py`, `prompt_common.py`,
`commands/*`) plus the library's public API and prose docs as a "developer front
end". Commit `6f2dedd`. Everything below was verified by running the shipped
binaries (`ppd-simulator`, `ppd-simulator-ctl`) against a live daemon on
127.0.0.1:3401 with `--scripts-dir`, including a pty-driven interactive session;
all spawned processes were killed.

## Summary

| Severity | Count |
|----------|-------|
| High     | 0 |
| Medium   | 1 |
| Low      | 3 |
| Trivial  | 6 |

The round-4 work holds up: H1, M1, M3, L1, L3 and T1–T5 all reproduce as fixed.
The one substantive finding is a gap **between** two of the round-4 fixes —
`ScriptQueue`'s claim tracking (M2) and the new `stop all` (L2) do not know about
each other, so `stop all` silently leaves one queued run alive in exactly the
scenario claim tracking was introduced for.

## Findings

### M1 (Medium). `stop all` does not discard a run the queue consumer has already claimed — the drop count contradicts the queue depth `list` printed one command earlier, and the "dropped" script starts running

`ScriptQueue.clear()` (`src/powerpetdoor/simulator/commands/scripts.py:70-78`)
empties `_waiting` only. `_claimed` — the entry `get()` handed to the consumer,
which is now parked on the run lock inside `ScriptRunner.run()`
(`src/powerpetdoor/simulator/cli.py:381-392`) — survives, and starts as soon as
the running script is stopped. `qsize()`/`pending()` (scripts.py:80-86) *do*
count it, so `list`, `status` and `stop all` disagree with each other in the same
breath.

The claim is only outstanding while the consumer waits for a lock somebody else
holds, i.e. while a `run <script> wait` is in flight — precisely the case round 4
added claim tracking for ("a single script waiting behind a `run ... wait`",
scripts.py:33-38).

Live repro (daemon with `--scripts-dir`, `long`/`long2`/`long3` are 30s waits):

```
$ ctl run long wait &          # holds the runner
$ ctl run long2 ; ctl run long3
OK: Queued script: Long Script B
OK: Queued script: Long Script C
$ ctl list | tail -2
Script: running "Long Script A" (2 queued)
Queued: long2, long3
$ ctl stop all
OK: Stopping script: Long Script A (dropped 1 queued)     # <- 2 were queued
$ ctl stop all                                             # immediately after
OK: Stopping script: Long Script B                         # <- B was "dropped"
$ ctl list | tail -1
Script: none running
```

Two `stop all` commands were needed to clear one running plus two queued runs,
and between them Long Script B was driving the door. With a slow script the
operator sees `Script: running "Long Script B"` seconds after being told the
queue was emptied. A `stop` issued in the window before B starts answers
`ERROR: No script is running` (exit 1) even though a run is pending, so the only
reliable recipe is poll-`list`-and-retry.

Every statement of intent says otherwise:

- `stop`'s own in-client help (`scripts.py:233-241`): *"'all' to discard every
  queued run as well"*
- `stop_script` docstring (scripts.py:249-254): *"an operator does not have to
  issue one `stop` per queued entry and guess how many are left (L2)"* — that is
  exactly what this forces
- `docs/simulator.md:242`: *"discards every run still queued, reporting how many
  were dropped"*
- `docs/simulator.md:372-374`: *"`status` and `list` report … how deep the queue
  is (including a run already taken off the queue but still waiting for the
  runner) … and `stop all` also empties the queue"* — the parenthesis names the
  entry that is not emptied
- `CHANGELOG.md:37`

Suggested fix: make the claim cancellable rather than invisible — e.g. `clear()`
returns `[*self._claimed, *self._waiting]`, records the cancelled claims in a
`_dropped` set, and the consumer checks `queue.was_dropped(script_ref)` after
`get()`/before `run()` and skips the run (releasing the claim). The reported
drop count should then match the `queued` count `status`/`list` show. Related
one-liner while in there: `stop all` with a pending-but-not-running queue
currently reports "No script is running" instead of draining it.

### L1 (Low). A normal one-shot `ctl` disconnect is logged as `[ERROR] Control client error: [Errno 32] Broken pipe`, and broadcast to every other ctl session

`ControlChannel._handle_client` catches everything into
`logger.error(f"Control client error: {e}")` (`src/powerpetdoor/simulator/cli.py:334-335`).
When a one-shot ctl reads its `OK:` line and exits while the daemon is still
emitting log lines for that command, the reader raises `BrokenPipeError` and a
completely normal hang-up is reported at ERROR.

It is not rare — it fires on essentially every one-shot command that produces
trailing log output (`run`, `stop`, script progress). Measured: 10 one-shot
commands (`run long` / `stop`, alternating) produced 10 ERROR lines.

Because `_ControlLogHandler` is installed on the root logger, the bogus ERROR is
also pushed to every *other* control client. An operator sitting in
`ppd-simulator-ctl -i` sees, with no action of their own:

```
2026-08-22 02:36:22,614 [INFO] Running queued script: Long Script A
...
2026-08-22 02:36:22,615 [ERROR] Control client error: [Errno 32] Broken pipe
2026-08-22 02:36:22,615 [INFO] Control connection closed from ('127.0.0.1', 41634)
```

An ERROR that means "the client hung up normally" trains operators to ignore the
one severity that should never be ignorable. Fix: catch
`(BrokenPipeError, ConnectionResetError, ConnectionError)` ahead of the generic
handler and log at DEBUG (or fold it into the existing INFO
`Control connection closed from …`).

### L2 (Low). In-client help for `run` advertises file paths on the channel that refuses them

`run help` over ctl (the daemon generates this from the shared `ArgSpec`, and the
daemon runs with `allow_script_paths=False`) answers:

```
$ ctl run help
OK: run <script> [wait]
Arguments:
  script: Script name or file path [required]
```

…while the very next command demonstrates the opposite:

```
$ ctl run /tmp/ppd5/scripts/quick.yaml
ERROR: Script paths are not allowed over the control channel; use a bare script name (see 'list')
```

The prose docs get this right (`docs/simulator.md:306-309`) and round 4 correctly
stopped ctl from *completing* paths (M1) — but the in-client help, which the
persona treats as the primary help surface, still points at the broken form. The
description lives in a static `ArgSpec` (`scripts.py:281-286`) while the handler
already knows `self._allow_script_paths`; `_get_arg_help`
(`commands/info.py:100-118`) could append/replace the sentence when paths are
refused (e.g. "Script name (paths are not accepted over the control channel)").

### L3 (Low). The library "developer front end" documents 72 of its 121 exported names; the timezone bridge the simulator itself uses is entirely undocumented

Checked every name in `powerpetdoor.__all__` against `README.md` and all of
`docs/*.md`: **49 of 121 exports appear nowhere in the prose docs.** Bulk protocol
constants are arguably fine (their *values* are documented in `docs/protocol.md`),
but three groups are user-facing API a developer cannot discover:

1. **Timezone helpers** (`async_init_timezone_cache`, `init_timezone_cache_sync`,
   `is_cache_initialized`, `get_available_timezones`, `get_posix_tz_string`,
   `find_iana_for_posix`, `parse_posix_tz_string`) — zero mentions. Meanwhile
   `docs/door.md:260-268` documents `await door.set_timezone("EST5EDT,M3.2.0,M11.1.0")`
   as POSIX-only, and `door.set_timezone` does no conversion
   (`src/powerpetdoor/door.py:841-851`). The simulator's own `timezone` command
   happily accepts `America/New_York` — so the *terminal* client can do something
   the *documented* library API appears not to, even though the exported helper
   that bridges it ships in the same package. Worse for a cold caller:

   ```
   >>> from powerpetdoor import get_posix_tz_string
   >>> get_posix_tz_string('America/New_York')
   None                      # silent: the cache was never initialized
   ```

   The docstring says so (`tz_utils.py:159-164`), but nothing a docs reader sees
   mentions `async_init_timezone_cache()` at all.
2. **`PRIORITY_CRITICAL/HIGH/MEDIUM/LOW`** — exported, but `docs/client.md:515-527`
   builds a `PrioritizedMessage` with a magic `priority=1` and the priority table
   below it (client.md:529-537) spells the levels out as prose numbers. The
   example should use the exported constant it ships.
3. **Six `CMD_GET_*` query commands** missing from client.md's curated import list
   (`CMD_GET_AUTORETRACT`, `CMD_GET_CMD_LOCKOUT`,
   `CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK`, `CMD_HAS_REMOTE_ID`, `CMD_HAS_REMOTE_KEY`,
   `CMD_CHECK_RESET_REASON`) — these are the read counterparts of setters the same
   list documents, so their absence reads as "you cannot query this".

Also undocumented: `week_0_mon_to_sun` / `week_0_sun_to_mon` (the day-order
converters a caller needs to map `days_of_week` onto `datetime.weekday()`) and
`schedule_entry_content_key`.

(Verified separately: every `python` code block in `README.md` and `docs/*.md`
parses, and every documented `from powerpetdoor… import …` resolves.)

### T1 (Trivial). `stop all` is not idempotent for scripted use

`ctl stop all` on an idle simulator with an empty queue returns
`ERROR: No script is running (use 'shutdown' to stop the simulator)` and exit 1.
For plain `stop` that hint is deliberate and good (round-4 T4, muscle memory from
when `stop` meant `shutdown`). For `stop all`, whose meaning is "leave nothing
running or queued", the requested state is already true, and a CI wrapper doing
`ctl stop all || fail` gets a false failure. Suggest success (`Nothing running or
queued`) for the `all` scope only.

### T2 (Trivial). `list`'s `Queued:` line prints raw references while every other line prints script names

```
0.0.0.0:3402> run ./scripts/long2.yaml
>>> Queued script: Long Script B          # name
0.0.0.0:3402> list
Script: running "Long Script A" (1 queued)
Queued: ./scripts/long2.yaml              # reference
```

`docs/simulator.md:373` says "`list` names the pending runs". For bare names the
two coincide, so this only shows up in the CLI (the only front end that can queue
a path), but the identifier switches mid-output. `ScriptQueue` could store
`(ref, name)` — `run` already loads the script and knows the name
(`scripts.py:310-313`).

### T3 (Trivial). Choice-argument completions use the value as its own description

`_get_arg_options_for_info` returns `[(c.lower(), c) for c in arg.choices]`
(`prompt_common.py:407-411`), so the completion menu shows `all → all` and
`wait → wait` while `bool_toggle` shows `on → Enable` and `days` shows
`weekdays → Monday-Friday`. Confirmed in a real pty session:

```
stop ⇥⇥
 all    all
 help   Show help for this command
```

The `ArgSpec.description` ("'all' to discard every queued run as well") is right
there and would fill the meta column usefully.

### T4 (Trivial). ctl epilog: "Plain 'run SCRIPT' … always exits 0" is not literally true

`ctl.py:679-681`. `ppd-simulator-ctl run nosuchscript` exits **1** (queueing
failed — the load happens before the enqueue, scripts.py:310). The intended
meaning is "the queued script's own result never reaches the exit code", which
`docs/simulator.md:345-348` phrases precisely ("exits 0 as soon as it is queued").
Worth borrowing that wording so the two help surfaces agree.

### T5 (Trivial). `--list-scripts` and `list` still diverge for an *empty* `--scripts-dir`

Round-4 T1 aligned the `Built-in scripts:` header, but:

```
$ ppd-simulator --list-scripts --scripts-dir /tmp/empty
Built-in scripts:
  …
Scripts from /tmp/empty:
  (none)
```

whereas the `list` command over ctl prints no `Scripts from …` line at all for
the same daemon (`scripts.py:205-209` only emits the header when `extra` is
non-empty, vs `cli.py:1012-1020` which always emits it). The startup WARNING
covers the operator who launched the daemon, but the ctl user — the one the
round-4 note says should be able to discover scripts with `list` — cannot tell
"no `--scripts-dir` configured" from "configured but empty".

### T6 (Trivial). `InteractiveSession.format_output` is still dead, and it is the only *sanitized* history-recall echo

Round 4 made `format_output` route through `render_result` (T2), but nothing in
`src/` calls it — the live recall echoes are raw f-strings at `cli.py:786` and
`ctl.py:616` (`print(f">>> {input_line.original} -> {input_line.resolved}")`),
and the class/`input_loop` docstrings (`prompt_common.py:534-535`, `757-763`)
still advertise the unused method as the way to print results. Either delete it
and fix the docstrings, or use it at the two call sites so the recall echo goes
through `sanitize_text` like every other line printed to the terminal.

## Round 4 Fix Verification

| Round 4 | Claim | Verdict |
|---------|-------|---------|
| **H1** | stop during the final step no longer reports PASSED; `wait N` interruptible | **Verified.** Single-step `wait 12` script, `stop` at T+2: `ERROR: Script FAILED: One Step Wait`, exit 1, elapsed 2.20s. |
| **M1** | ctl suppresses cwd files/dirs and path-shaped prefixes; bare names still complete; docs true | **Verified.** ctl: `run ⇥` → 7 built-ins only; `run ./`, `run ./scripts/`, `run /abs/path/`, `run scripts/lo`, `run q` (a cwd file) → nothing. CLI in the same cwd still offers `long/long2/long3/quick`, `scripts/`, `./scripts/quick.yaml`, absolute paths. Confirmed in a real pty session, not just by calling the completer. `--scripts-dir` names do not complete in ctl (ctl never learns the daemon's dir) — this is stated explicitly in `docs/simulator.md:318-322`, so the doc is accurate; an obvious future improvement is seeding ctl's completer from a `list` at session start. |
| **M2** | claim tracking keeps a dequeued-but-blocked run counted and named | **Verified for display** (`(2 queued)` + `Queued: long2, long3` while a `run … wait` held the runner), **but see M1 above**: `stop all` does not know about claims. |
| **M3 / L1** | ctl line-buffers stdout off a terminal; `cli.py:438` uses `status_print` | **Verified.** `ctl -i` with stdout redirected to a file showed queued/running/log lines within ~0.1s of each command instead of at exit. Wait-run stream split verified too: `ctl run quick wait 2>run.log` → stdout exactly `OK: Script PASSED: Quick Script`, the five `LOG:` lines in `run.log`. All four `_run_startup_scripts` progress prints now use `status_print`. |
| **L2** | `stop all` drains the queue and reports the dropped count | **Partially verified** — works when the running script came off the queue; leaks the claimed entry and under-reports when a `run … wait` holds the runner (**M1**). |
| **L3** | pending stop shows as `Script: stopping "<name>"`; repeat `stop` answers `Stop already requested for: <name>` | **Verified** via the CLI's piped path (`Script: stopping "Long Script A"` captured). Over ctl it is effectively unobservable, because `_sleep_or_stop`/`_wait_for_status` now honour a stop faster than a ctl round-trip — a good outcome, not a defect. |
| **T1** | `--list-scripts` header matches `list` | Verified (`Built-in scripts:` / `Scripts from <dir>:` both places). Empty-dir case still differs — T5. |
| **T2** | `format_output` folded onto `render_result` | Verified as sanitized; still dead — T6. |
| **T3** | "Unknown script: X. Available: …" no longer says "built-in" | Verified; the `Available:` list mixes built-ins and `--scripts-dir` names, matching `list`. |
| **T4** | better no-script-running message | Verified: `No script is running (use 'shutdown' to stop the simulator)`. |
| **T5** | `docs/simulator.md` uses booleans for schedule days | Verified (`docs/simulator.md:487-492`, consistent with `docs/door.md:352-356`). |

## Areas Reviewed With No Findings

- **Stop-state reporting and races.** `stop_script` snapshots status and calls
  `script_runner.stop()` with no `await` between, so no interleaving; `_stop_requested`
  is reset at the top of `_run_steps` before any suspension point, so a stop cannot
  leak onto the next run; `stopping` is gated on `running is not None`, so it never
  displays stale. `stop` on an already-stopping script, `stop <bogus>`
  (`'bogus' is not valid. Choose from: all` + usage), and `stop`/`stop all` against an
  idle simulator all behave sanely (modulo T1).
- **Queue accounting through `status`/`list`.** Depth, ordering, duplicates
  (12 × `quick` displayed correctly), release-on-load-failure and release-on-cancel
  paths (`cli.py:396-402`) all check out; no phantom counts observed after any run,
  stop or drop.
- **Wait-run semantics.** No `--timeout` deadline (8s silent script under a 5s
  timeout completed cleanly), case-insensitive `WAIT`, alias `r … wait`, refusal
  while busy (`ERROR: Another script is already running: Long Script A`, exit 1),
  stderr/stdout split.
- **Path restriction over the control channel.** `run /abs/path`, `run ./rel`, and
  `run ..`-style refs all rejected with the same actionable message; `run quick.yaml`
  falls through to `Unknown script … Available: …` which lists `quick`.
- **ctl help/epilog/argparse surface.** `--timeout` silence-gap wording,
  `run SCRIPT wait` and `stop` examples, `--history` note, no-args → help + exit 1,
  unknown command → exit 1, connection refused → clear message + exit 1.
- **CLI argparse gates.** `--scripts-dir` nonexistent → startup error; empty →
  WARNING; `--script`+`--daemon` mutually exclusive; mode-scoped flags rejected;
  `--oneshot` exit codes verified for pass (0), assertion failure (1) and unknown
  script (1).
- **Command surface consistency.** Swept `status`, `schedule` (add/days/time/list/
  clear, including the implicit all-day display), `notify`, `timezone`, `holdtime`,
  `battery`, `debug`, `broadcast`, `help` in both one-shot and interactive ctl and in
  the CLI; naming, ON/OFF vs enabled/disabled conventions, error text and exit codes
  are uniform, and ctl's interactive help correctly adds `exit`/`clear` and hides
  `history` when prompt_toolkit is not driving the session.
- **Interactive session mechanics** (pty-verified): prompt colouring, syntax
  highlighting, `!!` recall echo and re-execution, tab completion at argument
  positions, `Ctrl-C` behaviour, clean `exit`.
- **Sanitization of terminal output** — `render_result`, `_SanitizingFormatter`,
  `escape_message`/`unescape_message` round-trip (including backslash-first
  ordering) all still in place on every path a network- or YAML-derived string
  reaches a terminal.
- **Docs code blocks**: all `python` blocks in `README.md` and `docs/*.md` parse;
  every documented `powerpetdoor` import resolves against the installed package.
