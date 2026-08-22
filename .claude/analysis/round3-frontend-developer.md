# Frontend Developer Analysis — Round 3

Persona: frontend engineer; scope is the simulator terminal front end (`ppd-simulator`,
`ppd-simulator-ctl`, `prompt_common.py`, `commands/*`) plus the library's public API and
docs as the "developer front end" (README.md, CHANGELOG.md, docs/simulator.md,
docs/client.md, docs/door.md) at commit 3478a5b. No web UI exists.

Everything below was verified against the code **and** live binaries: a daemon started
with `--scripts-dir`, ctl one-shot and piped `-i` sessions, piped `ppd-simulator`
sessions, raw control-channel sockets, fake control servers (silent and chatty),
`--oneshot` CI runs, completer probes in both the CLI and ctl process contexts, and
execution of the documented programmatic examples. All spawned processes were killed.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 1 |
| Medium   | 5 |
| Low      | 2 |
| Trivial  | 5 |

Every round-2 fix verified working as documented (details in
[Round 2 Fix Verification](#round-2-fix-verification)) — the new `wait`/busy/argparse/
scripts-dir behavior is correct and the docs describing it are accurate, with the one
exception in M4. The High finding is a pre-existing daemon defect that only shows up
live: a log-broadcast feedback loop that floods every connected ctl session. The
remaining findings are feedback/clarity gaps around long-running scripts and non-TTY
output.

## Findings

### High

#### H1. Control-channel log broadcast is a feedback loop: one dead ctl client floods every other session with hundreds of `socket.send() raised exception.` lines

**Files:** `src/powerpetdoor/simulator/cli.py:156-180` (`_ControlLogHandler.emit`),
`src/powerpetdoor/simulator/cli.py:264-304` (writers are only reaped when the reader
task notices EOF)

`_ControlLogHandler` is installed on the **root** logger and writes every record to
every control client. `StreamWriter.write()` on a transport whose peer has gone away
does **not** raise (so the `except Exception` never fires and the dead writer is never
dropped); instead asyncio logs `socket.send() raised exception.` at WARNING once
`_conn_lost` passes its threshold (`asyncio/selector_events.py:1066`). That warning is a
root-logger record, so this handler broadcasts it — writing to the same dead transport
again, producing another warning, and so on until `_handle_client`'s `finally` removes
the writer.

Verified three independent ways against a live daemon:

- `ppd-simulator-ctl -i | head -3` (stdout closes, ctl dies of SIGPIPE) while a script
  was running: **732** warning records in ~15 ms from a script that emits ~30 real log
  lines.
- Two such clients: **487** records.
- Clean minimal repro — `ctl -i` reading from a fifo, `kill -9` while `run basic_cycle`
  streamed logs: **609** records.

The blast radius is the front end: those records are broadcast as `LOG:` lines to
**every other connected ctl session**, so an operator watching a `-i` session sees a
solid, unreadable wall of `socket.send() raised exception.` with nothing identifying
which client died (my first `ctl -i` transcript of this run is 60+ consecutive such
lines). They also land in the daemon's own log/disk. Because each surviving-but-dead
writer multiplies each record, N stale writers make it N× per record.

The trigger is completely ordinary: closing a terminal, `ctl -i | head`/`grep -m1`, a
container stop, a killed session, or a network RST.

**Recommendation:** In `_ControlLogHandler.emit`, (a) skip and `discard()` writers whose
`writer.is_closing()` is true, and (b) add a re-entrancy guard (a flag set for the
duration of `emit`) so records generated *by* the broadcast are never rebroadcast; a
`logging.Filter` dropping `asyncio`-logger records would also break the cycle.

### Medium

#### M1. `--timeout` help text still describes a command deadline; the flag now bounds a silence gap and can never fire against a chatty daemon

**Files:** `src/powerpetdoor/simulator/ctl.py:656-662` (`help="Command timeout in
seconds (default: 5)"`); correct behavior at `ctl.py:227-249` and `ctl.py:447-498`;
docs are correct at `docs/simulator.md:341-345, 377`

The round-2 change inverted what this flag means, but the only in-client description is
still "Command timeout in seconds (default: 5)". Nothing in `--help` says that the
timer is restarted by *any* received line, or that `run <script> wait` ignores it
entirely. Verified live:

- `ctl -t 1 run slow_quiet wait` against a 12 s silent script → waits the full 12 s and
  returns `OK: Script PASSED` (correct, but the opposite of what `--help` implies).
- Against a fake daemon that streams a `LOG:` line every second and never answers,
  `ctl -t 3 status` **never returns** (killed externally at 12 s, exit 124). A CI job
  with `-t 3` and no external timeout hangs forever.

For a persona-critical value ("the user should never wonder if something is still
loading"), the flag most users will reach for when a command seems stuck is documented
only in an external page, and its guarantee ("give up after N seconds") no longer
exists.

**Recommendation:** Bring the docs wording into `--help`: "Seconds of daemon *silence*
tolerated while waiting for a response (default: 5); restarted by any received line and
ignored entirely for `run <script> wait`." Consider also printing a one-line "still
waiting (Ns, daemon active)" note to stderr once the gap budget is exceeded but traffic
is flowing, so an indefinite wait is at least visible.

#### M2. Status/progress output is invisible or badly reordered whenever stdout is not a TTY (no flush on `print`)

**Files:** `src/powerpetdoor/simulator/cli.py:644-651` (startup banner),
`cli.py:380, 385, 390, 395, 405, 410-416, 427, 439` (script progress)

All human-facing status lines use bare `print()`, so on a pipe/file/journal stdout is
block-buffered while logging goes to stderr unbuffered. Verified live:

- `ppd-simulator --daemon 39301 --port 39300 > daemon.log 2>&1`: while the daemon ran,
  `grep -c "Simulator started" daemon.log` → **0**, `grep -c "Control channel"` → **0**.
  After `kill -TERM` the lines were **still** absent — the buffer dies with the process,
  so those lines are never seen at all in the normal daemon deployment.
- `ppd-simulator -s basic_cycle --oneshot 2>&1 | cat`: the entire stderr log printed
  first, then `Simulator started on 0.0.0.0:39310`, `>>> Running script: …`,
  `>>> Script PASSED: …`, `>>> All scripts PASSED` — i.e. every CI log has the progress
  narrative appended after the fact, in the wrong place. If the run is killed (CI
  timeout), the progress lines are lost entirely and the log shows no record of how far
  the suite got.
- `--wait-for-client` prints `>>> Waiting for client connection...` — with redirected
  stdout the operator never sees why the run appears to hang.

(The INFO log does carry equivalent bind information, so the daemon case is
recoverable; the ordering/loss in script mode is not.)

**Recommendation:** `print(..., flush=True)` for these status lines, or
`sys.stdout.reconfigure(line_buffering=True)` once at startup when stdout is not a TTY.

#### M3. ctl one-shot `run <script> wait` is silent for the whole run and reports failure with no detail

**Files:** `src/powerpetdoor/simulator/ctl.py:260-283` (`LOG:` lines are parsed and
discarded in one-shot mode)

The flagship CI form from `docs/simulator.md:338` (`ppd-simulator-ctl run
full_test_suite wait || echo "Tests failed!"`) prints **nothing at all** until the
script ends — verified with a 12 s script: 12 s of dead terminal, no progress, no way to
distinguish "running" from "hung" (and, per M1, no timeout will ever fire). On failure
the entire output is one line:

    ERROR: Script FAILED: Failing Script

The assertion text that says *what* failed (`Assertion failed at step 3: door_status:
expected …, got …`) exists only in the `LOG:` stream ctl just threw away, so a CI job
using the documented recipe cannot tell why the suite failed without separately fetching
the daemon's log. The interactive ctl session, by contrast, streams those lines — the
same operation gives materially different feedback in the two clients.

**Recommendation:** During a wait-run, print received `LOG:` lines to **stderr** (stdout
stays clean/scriptable) — or at minimum buffer them and dump the last N lines when the
result is `ERROR:`.

#### M4. Docs promise `--scripts-dir` scripts in tab completion "so a ctl user can discover them" — ctl's completer is daemon-blind

**Files:** `docs/simulator.md:308-311`; `src/powerpetdoor/simulator/scripting.py:696-785`
(`script_completer` reads the process-global `_extra_scripts_dir`),
`src/powerpetdoor/simulator/commands/handler.py:105-107` (only the *daemon* process ever
calls `set_extra_scripts_dir`)

The doc says the scripts show up "in `list`, in `--list-scripts`, in the 'Available:'
hint of an unknown-script error, and in tab completion, so a ctl user who did not start
the daemon can still discover them." The first three are true and verified. The fourth
is false for ctl, which is the exact audience the sentence names: ctl is a separate
process that never learns the daemon's `--scripts-dir`. Verified with completer probes:

- CLI/daemon process (after `set_extra_scripts_dir`): `run ` → builtins **+**
  `failing`, `my_custom`, `slow_quiet`. Correct.
- ctl process (fresh interpreter importing `ctl`): `run ` → builtins only, plus
  `tmpuao0hqx_/` — a cwd subdirectory offered as a script path, which the daemon then
  rejects outright ("Script paths are not allowed over the control channel"). So ctl's
  completion both hides what works and suggests what cannot work.

**Recommendation:** Either scope the claim to the CLI, or (better, and it fixes the
bogus path suggestions too) have ctl seed its completer from the daemon: issue `list` on
connect and register the returned names via `set_extra_scripts_dir`-equivalent state,
and suppress local file/directory completions in ctl since the daemon always refuses
paths.

#### M5. Serialized script runs added a "busy" state with no way to see it, wait for it, or cancel it — and `stop` means `shutdown`

**Files:** `src/powerpetdoor/simulator/scripting.py:254-281` (busy/lock),
`scripting.py:321-324` (`ScriptRunner.stop()` — **no caller anywhere in the front end**),
`src/powerpetdoor/simulator/commands/info.py:123-215` (`status` reports no script state),
`src/powerpetdoor/simulator/commands/control.py:21` (`shutdown` alias `stop`)

Round 2's serialization is the right fix, but the resulting state is invisible and
uncontrollable from every client:

- `status` (the "show me everything" command) says nothing about a running or queued
  script; verified live while a 12 s script drove the door — the operator sees the door
  moving with no explanation.
- The only signal is the failure of a competing wait-run: `ERROR: Error: Another script
  is already running: Slow Quiet Script` (verified, exits 1 in ~150 ms). There is no
  "how long has it been running", no queue depth, and no way to wait for the runner to
  free up other than polling by retrying a command that fails.
- Nothing can stop a running script: `ScriptRunner.stop()` exists but no command,
  neither CLI nor ctl, calls it. A wrong/long script must be waited out — or the daemon
  killed.
- The natural guess for aborting it, `stop`, is an **alias for `shutdown`**: a user
  trying to cancel a script kills the whole simulator (and, in the CLI, `exit`/`q`/
  `quit` do the same). That is a genuine foot-gun given the busy error now invites the
  user to do something about the in-flight script.

**Recommendation:** Add a script status line to `status` (`Script: running "<name>" (12s)`,
queue depth) and a `run stop` / `script stop` subcommand wired to `ScriptRunner.stop()`.
Include the running script's name in `list` output too. Mention in the busy error that
the run can be stopped (once such a command exists).

### Low

#### L1. A wrong `--scripts-dir` is silently ignored

**Files:** `src/powerpetdoor/simulator/cli.py:870-876`,
`src/powerpetdoor/simulator/scripting.py:630-643`
(`_script_files_in` returns `{}` for a missing directory)

Verified: `ppd-simulator --list-scripts --scripts-dir /tmp/ppdtest/nope` prints the
built-ins and *nothing else* — no error, no warning, not even a "Scripts from …:"
header with an empty list. Same in daemon mode: the daemon starts happily, and the first
sign of trouble is `ERROR: Error: Unknown built-in script: my_custom` from a ctl user
much later. Given that round 2 deliberately made mis-scoped flags loud (argparse errors
rather than silent no-ops), a typo'd or non-existent scripts directory is the one
remaining silent-misconfiguration path.

**Recommendation:** `parser.error()` when the directory does not exist, and log a
warning at startup when it exists but contains no `*.yaml`/`*.yml`. In `--list-scripts`,
print the "Scripts from DIR:" header with "(none)" so the flag's effect is always
visible.

#### L2. `--list-scripts` help text still says "built-in scripts" only

**Files:** `src/powerpetdoor/simulator/cli.py:846-848`; behavior at `cli.py:916-929`;
docs already corrected at `docs/simulator.md:94`

`--help` says "List available built-in scripts and exit" while the flag now also lists
`--scripts-dir` scripts (verified live). The docs table was fixed in round 2; the
in-binary help was not, so the two disagree.

**Recommendation:** "List runnable scripts (built-in, plus `--scripts-dir` if given) and
exit", matching the docs table.

### Trivial

#### T1. Mode-scoped rejection messages: grammar, and misleading advice for `--daemon --oneshot`

`src/powerpetdoor/simulator/cli.py:947-950` — a single flag produces
`ppd-simulator: error: --oneshot require --script` ("require" should agree with the
number of flags listed; verified live for `--oneshot`, `--loop`, `--wait-for-client`,
`--script-delay`). Also `--daemon --oneshot` answers "--oneshot require --script", but
adding `--script` then errors with "--script and --daemon are mutually exclusive" — two
round trips to learn that `--oneshot` is simply not a daemon-mode flag. Suggest
`"%s cannot be used without --script"` and, when `--daemon` is present, "…is not
available in daemon mode".

#### T2. Doubled prefix `ERROR: Error: …` on every exception-path result

`src/powerpetdoor/simulator/commands/scripts.py:145-146` (and the equivalent
`CommandResult(False, f"Error: {e}")` in `commands/handler.py:354-355, 374-375`). Live
output: `ERROR: Error: Another script is already running: Slow Quiet Script`,
`ERROR: Error: Unknown built-in script: bogus. …`,
`ERROR: Error: Script paths are not allowed over the control channel; …`. The docs quote
these messages without the inner prefix (`docs/simulator.md:237, 348`). Drop the
`Error: ` prefix in the `CommandResult` (the transport already labels failures) or use a
context-specific one.

#### T3. "Unknown **built-in** script: X. Available: …" lists built-ins *and* scripts-dir scripts

`src/powerpetdoor/simulator/scripting.py:669-679`. Verified live — the `Available:` list
correctly included `failing`, `my_custom`, `slow_quiet` (round-2 fix), but the label
still says "built-in", which reads as if the scripts-dir names in that same list were
built-ins. Suggest "Unknown script: X. Available: …".

#### T4. CHANGELOG lists a `notify` alias that does not exist (and belongs to another command)

`CHANGELOG.md:23` — "Notification commands (`notify`, `n`) in simulator CLI". `notify`
has no aliases (`commands/notifications.py:54`), and `n` is `inside_enable`; verified
live: `ppd-simulator-ctl n` → `OK: Inside sensor: disabled` (it toggled the sensor). A
user following the changelog silently changes an unrelated setting.

#### T5. Bare `ac` mutates but phrases its result exactly like the read-only displays

`src/powerpetdoor/simulator/commands/settings.py:192-197`. `ac` → `OK: AC: disconnected`
is indistinguishable in form from `battery` → `OK: Battery: 100%` and `holdtime` →
`OK: Hold time: 2.0s`, which *show* rather than change; the mutating siblings say
"Battery set to 26%", "Hold time set to 2.0s". (`ac` is a boolean, so toggling on a bare
call is the right behavior — only the wording invites "did I just change that?".)
Suggest "AC connected"/"AC disconnected" or "AC set to disconnected" for the mutating
paths.

## Round 2 Fix Verification

All nine round-2 findings, re-verified against code and live binaries:

| Round-2 item | Status |
|---|---|
| H1 docs use `run <script> wait` where exit codes matter | **Fixed** — `docs/simulator.md:323, 332-339` show `run basic_cycle wait`, scope the exit-code claim to the `wait` form, and warn that plain `run` always exits 0; the Scripts table (`:236-237`) now has separate rows for both modes. Verified live: `run my_custom wait` → exit 0, `run failing wait` → `ERROR: Script FAILED` exit 1, plain `run my_custom` → `OK: Queued script` exit 0 |
| M1 `pet [on\|off]` + alias `d` documented | **Fixed** — `docs/simulator.md:175` matches live help (`pet (d) [on|off] - Toggle or set pet presence…`); `pet`, `pet on`, `pet off`, `pet toggle`/`t` all verified live, completion offers `t, toggle, on, off, help` |
| M2 `--timeout` bounds a silence gap; `run … wait` has no deadline | **Fixed** — 12 s silent script completes under `-t 5` *and* `-t 1` (verified twice); silent fake server times out at exactly 5 s with `Response timeout after 5.0s waiting for … (the command may still be running; raise --timeout)` one-shot and `Response timeout after 5.0s of silence …` interactive. Residual: `--help` never says any of this (M1 above) |
| M3 concurrent scripts serialized; wait-run fails fast | **Fixed** — `ScriptRunner` holds an `asyncio.Lock`; with a 12 s script in flight, `run my_custom wait` failed in ~150 ms with `Another script is already running: Slow Quiet Script` (exit 1) while plain `run my_custom` queued and later ran cleanly. No interleaved-assertion failures reproducible any more. New visibility gap noted as M5 |
| M4 `--scripts-dir` documented and visible | **Fixed** for `list` (`Scripts from /tmp/ppdtest/scripts:` section), `--list-scripts`, the unknown-script `Available:` hint, `--script my_custom` by bare name, and **CLI** tab completion (probes show `my_custom`/`slow_quiet` with descriptions). Options table row added at `docs/simulator.md:97`. ctl-side completion is still daemon-blind and the doc over-claims it (M4 above) |
| M5 control protocol docs cover `STATUS:` and escaping | **Fixed and accurate** — raw socket confirms `STATUS: clients=0` as the first line, `STATUS: clients=1`/`0` on door-client connect/disconnect, `OK:`/`ERROR:` with `\n`-escaped newlines, and backslash doubling (`\weird\cmd` came back on the wire as `\\weird\\cmd`, and ctl unescaped it to `\weird\cmd`). Doc table at `:272-291` matches byte for byte |
| L1 `holdtime`/`battery` rows + `battery random` | **Fixed** — live: `holdtime` → `Hold time: 2.0s`, `holdtime 2` → `Hold time set to 2.0s`, `battery` → `Battery: 100%`, `battery random` → `Battery set to 26%`; docs rows `:197-199` match, and `battery ` completion offers `random` |
| L2 mode-scoped flags rejected | **Fixed** — `--oneshot`, `--loop`, `--wait-for-client`, `--script-delay N` without `--script` and `--control-host ADDR` without `--daemon` all now `parser.error` (exit 2), documented at `docs/simulator.md:78-80`. I found **no legitimate combination newly blocked**: none of the four script flags affects any non-script path, `--control-host` is meaningless without a control channel, `--script-delay 0` (the default) is not flagged, and `--scripts-dir` remains usable in every mode (verified `--script my_custom --scripts-dir …` and daemon+`--scripts-dir`). Wording nit: T1 |
| L3 `--history` parenthetical | **Fixed** — both docs tables say "ignored when prompt_toolkit is not installed"; matches the argparse help in both binaries |
| T1 built-in script descriptions | **Fixed** — the doc table (`:764-772`) matches live `list`/`--list-scripts` output exactly for all 7 scripts |
| T2 module trees (`framing.py`, `engine.py`) | **Fixed** — `README.md:134, 140` and `docs/simulator.md:884-887` both present |
| T3 non-TTY ANSI | **Fixed** — piped `ppd-simulator` session through `cat -v` shows no escape sequences; `clear` is a no-op off a terminal |
| T4 ctl `history` off a terminal | **Fixed** — piped `ctl -i` answers `Unknown command: history`, identical to the CLI (verified both) |
| T5 "stdin not available" wording | **Fixed** — `cli.py:760` now says "running without interactive input" |

## Areas Reviewed With No Findings

- **`wait`-keyword parsing on both sides** — `run X WAIT` (uppercase) works end to end
  (ctl's `is_wait_run` lowercases, the daemon's choice parser is case-insensitive);
  `file X wait` and `r X wait` (aliases) honored; `run X wait now` →
  `Unexpected argument(s): now` + usage; `run X waitt` →
  `'waitt' is not valid. Choose from: wait`. No ctl/daemon disagreement found in which
  commands are treated as synchronous.
- **Shutdown during a wait-run** — a `shutdown` from a second ctl client while a 12 s
  script ran exited the daemon within 1 s (no hang waiting on the script), and the
  wait-run client got `Connection closed without response from 127.0.0.1:39321` with
  exit 1 rather than a false PASS.
- **Exit codes** — one-shot ctl 0/1 verified for success, script pass/fail, busy error,
  unknown script, path rejection, extra/invalid argument, connection refused;
  `--oneshot` verified 0 on a passing scripts-dir script and 1 on a failing one and on
  an unknown script name.
- **Documented command examples** — every row of the Interactive Mode tables exercised
  live over ctl (`holdtime`, `battery`, `battery random`, `ac`, `notify low_bat [on]`,
  `schedule add/list/time/days/disable/clear`, `timezone`, `charge_rate`,
  `discharge_rate`, `pet`, `pet off`, `list`, `run`, `status`, `help`, `broadcast`,
  `debug`, `clear`, `history`); all outputs match the documented semantics.
- **Help parity across clients** — the daemon-generated help (ctl one-shot) and ctl's
  local interactive help agree except for the local-only commands (`clear`, `exit`),
  which correctly appear only in the interactive session; `run (r, file) <script>
  [wait]` usage is regenerated correctly in both.
- **Programmatic API examples in docs/simulator.md** — "Managing Schedules",
  "Triggering Events", "Modifying State" and "Running Scripts Programmatically" all
  executed successfully as written (imports, kwargs, method names, `Schedule` field
  shapes, `Script.from_simple_commands`, `get_builtin_script`).
- **Control-channel hygiene** — sanitize-then-escape daemon side, unescape-then-sanitize
  in ctl, `LOG:`/`STATUS:` skipped while waiting for a response, path traversal refused
  over the control channel with an actionable message, control channel still bound to
  loopback by default with the warning intact in `--help`.
- **Prompt/completion machinery** — `prompt_common.py` unchanged since round 2; probes
  for command, subcommand, choice, script and later-argument positions all correct
  (`run X ` → `wait`, `pet ` → toggle/t/on/off/help, `battery ` → random/help).
- **README / CHANGELOG coverage** — structure tree current, CI example verified live,
  and the Unreleased section documents the round-2 front-end changes (`run … wait`,
  `--scripts-dir`, `--control-host`, silence-gap `--timeout`, serialized runs,
  mode-scoped flag rejection, bare `battery`/`holdtime`). Only T4 is wrong.
- **docs/door.md and docs/client.md** — re-verified the round-2 additions (idempotent
  `connect()` on both layers) against `door.py`/`client.py`; the rest was verified in
  round 2 and neither doc changed materially since.
