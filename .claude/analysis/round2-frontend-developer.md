# Frontend Developer Analysis — Round 2

Persona: frontend engineer; scope is the simulator terminal front end (`ppd-simulator`,
`ppd-simulator-ctl`, `prompt_common.py`, `commands/*`) plus the library's public API
surface and docs (README.md, docs/simulator.md, docs/client.md, docs/door.md) at commit
3f96bb8.

All findings verified against code and, where noted, by running the binaries: a live
daemon with `--scripts-dir`, ctl one-shot and piped `-i` sessions, piped `ppd-simulator`
interactive sessions, a raw control-channel socket client, `--oneshot` CI runs, and
direct `SimulatorCompleter` probes.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 1 |
| Medium   | 5 |
| Low      | 3 |
| Trivial  | 5 |

The terminal front end itself is in very good shape — every round-1 code fix verified
working (see below). Almost all round-2 findings are documentation drift against the
*new* surface (run-wait, pet, scripts-dir, STATUS:), plus two behavioral gaps in the
new `run ... wait` mode.

## Findings

### High

#### H1. docs/simulator.md documents wait-for-result semantics for ctl `run` without the required `wait` keyword — CI copy-paste yields false passes

**Files:** `docs/simulator.md:294` and `docs/simulator.md:298-301`; actual behavior
`src/powerpetdoor/simulator/commands/scripts.py:97-115`,
`src/powerpetdoor/simulator/cli.py:602` (queue always created)

The ctl one-shot section shows:

    ppd-simulator-ctl run basic_cycle   # Run a script (waits for the result)

and states "For `run`, the exit code reflects the **script result**: 0 if the script
passed, 1 if it failed." Verified live: `ppd-simulator-ctl -p PORT run basic_cycle`
returns `OK: Queued script: Basic Door Cycle` with **exit code 0 immediately** — the
script is queued (the daemon's script queue exists, so the handler only runs
synchronously when `wait` is passed). Only `run basic_cycle wait` waits and reflects
pass/fail in the exit code (verified: clean run → `OK: Script PASSED`, exit 0; failing
run → `ERROR: Script FAILED`, exit 1). A CI job following the doc always sees exit 0,
i.e. an aborted/failed test suite reports success. The Interactive Mode "Scripts" table
(`docs/simulator.md:230`) also documents `run <script>` with queue-only semantics and
never mentions the `wait` mode at all, even though `help` shows
`run (r, file) <script> [wait]`.

**Recommendation:** Change the example to `ppd-simulator-ctl run basic_cycle wait` and
scope the "exit code reflects the script result" sentence to `run <script> wait`
(plain `run` exit code reflects only queueing). Add a `run <script> [wait]` row (or
amend the existing one) in the Interactive Mode Scripts table describing both modes.

### Medium

#### M1. docs/simulator.md documents `pet arrive` / `pet depart` — neither exists; actual syntax is `pet [on|off]`

**Files:** `docs/simulator.md:170`; actual command
`src/powerpetdoor/simulator/commands/simulation.py:38-66`

Verified live: `pet arrive` → `ERROR: 'arrive' is not valid. Use on/off\nUsage: pet
[on|off]` (exit 1). The real interface is bare `pet` (toggle), `pet on`, `pet off`,
`pet toggle`/`pet t`, with alias `d` — the doc row shows an empty Aliases column and
invalid argument words. The error message recovers the user quickly, but the doc for
the round-1-added command is simply wrong on first use.

**Recommendation:** Rewrite the row as
`pet [on\|off]` | `d` | "Pet standing in the doorway (holds the door open); bare
toggles, `toggle`/`t` subcommand also available — same mechanism as the
`pet_presence`/`pet_on`/`pet_off` script actions".

#### M2. `run <script> wait` over ctl breaks against the 5 s default `--timeout` — reports failure while the script keeps running

**Files:** `src/powerpetdoor/simulator/ctl.py:436-441` (interactive: response wait uses
`--timeout`), `src/powerpetdoor/simulator/ctl.py:246-250` (one-shot: per-`recv`
timeout); no mention in `run` help (`commands/scripts.py:88-95`) or
`docs/simulator.md:284-330`

Verified live, both modes:

- One-shot: a script with a silent 7 s `wait` step →
  `Response timeout after 5.0s waiting for 127.0.0.1:39101`, exit 1, while the daemon
  log shows the script *completing successfully* afterwards. (Scripts that log
  continuously survive, because each `LOG:` chunk resets the recv timer —
  `full_test_suite wait` happened to pass; any silent gap > timeout fails.)
- Interactive: `run full_test_suite wait` → `>>> Response timeout` after exactly 5 s,
  guaranteed for any wait-run longer than `--timeout` (the response wait is a fixed
  `wait_for(..., timeout)`; streaming `LOG:` lines do not extend it). The eventual
  `OK:`/`ERROR:` is later discarded as a stale response by the next command, and
  `handle_result` records the command as failed in history.

So the flagship use of the new `wait` mode — running a full suite and branching on the
result — spuriously fails with default settings, the exit code says "failed" for a
passing script, and the user is never told the script is actually still running.
The interactive message (`Response timeout`) also lacks the detail of the one-shot one
(no duration, no hint about `--timeout`).

**Recommendation:** For a `run ... wait` command specifically, disable or greatly
extend the response deadline (the daemon connection is alive and streaming), or reset
the interactive response timer on any received line as one-shot mode effectively does.
At minimum: say "may still be running; increase --timeout" in the timeout message, and
document the `--timeout` interaction in `run help` and docs/simulator.md.

#### M3. `run ... wait` bypasses the script queue with no concurrency guard — overlapping scripts corrupt each other's results

**Files:** `src/powerpetdoor/simulator/commands/scripts.py:106-113` (wait path runs
directly while the queue path runs concurrently),
`src/powerpetdoor/simulator/scripting.py:232-268` (`ScriptRunner` has no lock;
`running`/`_stop_requested`/`_stop_event` are shared instance state),
`src/powerpetdoor/simulator/cli.py:326-349` (queue consumer uses the same runner)

Verified live: `ctl run basic_cycle` (queued) followed ~1 s later by
`ctl run basic_cycle wait` → `ERROR: Script FAILED: Basic Door Cycle` even though the
same script passes cleanly in isolation — the two scripts drove the door concurrently
and the wait-run's assertions failed. The same happens if two ctl clients issue
`run ... wait` simultaneously, and the shared `_stop_requested` flag means one run's
lifecycle can clobber another's. From the user's perspective a correct script
"randomly" fails depending on invisible background activity — exactly the
repeatability problem the exit codes exist to prevent.

**Recommendation:** Serialize script execution: an `asyncio.Lock` in
`ScriptRunner.run` (or route wait-runs through the queue with a completion future) so
a wait-run waits for any in-flight script, and per-run local stop state. Optionally
report "another script is running" instead of silently interleaving.

#### M4. `--scripts-dir` is undocumented, and scripts-dir scripts are invisible to `list`, unknown-script errors, and tab completion

**Files:** flag missing from `docs/simulator.md:80-96` options table (present in
`cli.py:863-869`); vague reference "resolved against the known script locations" at
`docs/simulator.md:279-282`; `src/powerpetdoor/simulator/commands/scripts.py:66-73`
(`list` shows built-ins only), `src/powerpetdoor/simulator/scripting.py:594-603`
(unknown-name error lists built-ins only), `:622+` (`script_completer` knows only
built-ins and cwd-relative files)

Verified live against a daemon started with `--scripts-dir` containing
`my_custom.yaml`:

- `ctl run my_custom wait` → `OK: Script PASSED: My Custom Script` (resolution works).
- `ctl list` → "Built-in scripts:" — `my_custom` absent.
- `ctl run bogus` → `Available: basic_cycle, ...` — `my_custom` absent.
- The path-rejection error says "use a bare script name (see 'list')" — but `list`
  will never show the very scripts that message is steering the user toward.
- Tab completion for `run ` offers only built-ins (and local YAML paths that the
  daemon's restricted mode will then reject).

So the round-1 feature works but is undiscoverable: an operator who didn't start the
daemon has no way to learn which extra scripts exist.

**Recommendation:** Add `--scripts-dir` to the options table and name it in the daemon
section. Make `list` (and the unknown-script error in `_load_script_by_name`) include
`*.yaml`/`*.yml` names from the configured scripts dir, labeled as such — that fixes
completion too for the CLI side (the completer would need handler context or a
registered scripts-dir to include them; at minimum fix `list` and the error).

#### M5. Control-channel protocol docs omit `STATUS:` lines (and backslash escaping) — a doc-following client breaks on the first line received

**Files:** `docs/simulator.md:264-268`; actual protocol
`src/powerpetdoor/simulator/cli.py:178-192` (docstring documents it correctly),
`:265-267` (STATUS sent immediately on connect), `prompt_common.py:71-77`
(escape doubles backslashes first)

The daemon-mode doc says each command gets a single `OK:`/`ERROR:` response line and
logs stream as `LOG:` lines. Verified live with a raw socket: the **first** line every
control client receives is `STATUS: clients=0`, and further `STATUS: clients=<n>`
lines arrive unsolicited on every door-client connect/disconnect. A third-party
control client written from the doc would misparse these. The doc also describes the
escaping as only "embedded newlines are escaped as `\n`", omitting that backslashes
are doubled first — a client implementing only `\n`→newline corrupts messages
containing literal backslashes.

**Recommendation:** Document the `STATUS: clients=<n>` line (sent on connect and on
each door-client connect/disconnect; used by ctl for prompt coloring) and the full
escape rule (`\` → `\\` then newline → `\n`; unescape in reverse) in the Daemon Mode
section.

### Low

#### L1. Settings table drift: `holdtime` and `battery` no longer match their round-1-improved behavior; `battery random` undocumented

**Files:** `docs/simulator.md:192-193`; actual behavior
`src/powerpetdoor/simulator/commands/settings.py:124-179`

- Doc: `holdtime <seconds>` "Set hold time (0.1–900 seconds)" — actual usage is
  `holdtime [seconds]`; bare `holdtime` shows the current value (verified live:
  `OK: Hold time: 2.0s`).
- Doc: `battery [percent]` "Set battery level 0–100 (random 10–100 if omitted)" —
  actual: bare `battery` *shows* the current level (verified: `OK: Battery: 100%`);
  randomization moved to the `battery random` subcommand (verified working), which no
  doc mentions.

This is exactly the pre-round-1 behavior being documented for the post-round-1 code —
a user following the doc will "set a random battery level" expecting mutation and get
a read-only display.

**Recommendation:** Update both rows: `holdtime [seconds]` (bare shows current),
`battery [percent]` (bare shows current) and add `battery random`.

#### L2. Mode-scoped flags are silently accepted and ignored outside their mode

**Files:** `src/powerpetdoor/simulator/cli.py:898-929` (only `--script`+`--daemon`
is validated)

`--control-host` without `--daemon`, and `--loop` / `--script-delay` /
`--oneshot` / `--wait-for-client` without `--script`, parse fine and do nothing. A
user who runs `ppd-simulator --oneshot` (no script) gets an ordinary interactive
simulator and no exit-code behavior, with no hint anything was ignored — a silent
repeatability trap in CI wrappers. (`--history` without prompt_toolkit is fine — its
help text explicitly says it is ignored.)

**Recommendation:** `parser.error()` (or at least warn) on script-scoped flags without
`--script` and `--control-host` without `--daemon`.

#### L3. `--history` documented as "only present when prompt_toolkit is installed" — the flag is now always registered

**Files:** `docs/simulator.md:94` and `docs/simulator.md:330`; actual
`src/powerpetdoor/simulator/cli.py:877-885`, `src/powerpetdoor/simulator/ctl.py:606-614`

Round 1 (T5) made the flag unconditional precisely so command lines are portable; the
argparse help now says "Requires prompt_toolkit; ignored otherwise" (verified via
`--help`). Both option tables still carry the old parenthetical, telling users the
invocation is machine-dependent when it no longer is.

**Recommendation:** Replace with "ignored when prompt_toolkit is not installed" in
both tables.

### Trivial

#### T1. Built-in script description drift in docs/simulator.md

**Files:** `docs/simulator.md:718` and `:720` vs live `list` output

`power_lockout_test`: doc "Tests that door doesn't respond when power off" vs actual
"Tests that commands are blocked when power off or lockout enabled";
`schedule_test`: doc "Tests schedule add/remove functionality" vs actual "Tests that
sensors respect schedule time windows" (a materially different test). Sync the table
with the script `description` fields (or generate it from `--list-scripts`).

#### T2. Module trees omit new modules: `framing.py` (README) and `engine.py` (README + docs/simulator.md architecture)

**Files:** `README.md:129-146`, `docs/simulator.md:822-852`;
`src/powerpetdoor/framing.py`, `src/powerpetdoor/simulator/engine.py`

Both wave-1/2 additions (shared frame scanner; door-motion state machine) are missing
from the "Library Structure"/"Architecture" trees that claim to describe the layout.

#### T3. Non-TTY fallback still emits ANSI escapes

**Files:** `src/powerpetdoor/simulator/cli.py:112-117` (`\r\033[K` in
`InteractivePrompt.clear_line`), `src/powerpetdoor/simulator/commands/control.py:88-95`
(`clear` writes `\033[2J\033[H` unconditionally)

Observed live in piped CLI output: `127.0.0.1:39201> ␛[K>>> ...`. On a pipe or
`TERM=dumb` terminal (the exact cases the round-1 fallback fix targets) these render
as garbage characters. Guard on `sys.stdout.isatty()`.

#### T4. ctl-local `history` in a non-TTY session says "Install prompt_toolkit" even when it is installed

**Files:** `src/powerpetdoor/simulator/ctl.py:116-213` (local dispatch never checks
availability), `src/powerpetdoor/simulator/commands/info.py:339-344`

In a piped `ctl -i` session the History object is None (non-TTY), so `history` returns
"History not available. Install prompt_toolkit..." — misleading when prompt_toolkit is
installed and the real cause is the non-interactive session. The CLI side of the same
situation says "Unknown command: history" instead (and hides it from help, as ctl also
does) — two different answers for the same condition. Prefer the hidden/"Unknown
command" behavior (or a message naming the real cause) in ctl too.

#### T5. Misleading warning wording: "stdin not available, running in daemon mode"

**Files:** `src/powerpetdoor/simulator/cli.py:753`

No control channel is started on this path, so it is not "daemon mode" as the docs
define it — it is simply headless. Say "running without interactive input".

## Round 1 Fix Verification

All round-1 items were re-verified against code and live binaries:

| Round-1 item | Status |
|---|---|
| H1 real flags (`--oneshot`, `--script` auto-detect) documented | Fixed — README:114-123, simulator.md:85-127, CLAUDE.md all use real flags; `--oneshot` CI run verified live (exit 0 on pass) |
| H2 simulator.md rewritten (commands, ctl, daemon, flags, architecture) | Fixed — structure/content now match the real UI; residual drift is the new findings H1/M1/M4/M5/L1/L3/T1/T2 |
| H3 package-root exports for docs/client.md imports | Fixed — all 12 formerly-missing names plus `CommandError`, `NOTIFY_*`, `Schedule`/`ScheduleTime` verified importable from `powerpetdoor` |
| H4 pet command | Fixed — `pet`/`d`, on/off/toggle verified live via ctl (docs syntax wrong: M1) |
| M1 position-aware completion | Fixed — verified via completer probes: `schedule add inside 6:00-22:00 ` → days presets+day names; `power on ` → nothing; `run basic_cycle ` → `wait`; `notify low_battery ` → on/off/help |
| M2 structured STATUS: prompt tracking | Fixed — `STATUS: clients=<n>` on connect and on door-client transitions, verified with a raw socket; ctl keys prompt color off the count (ctl.py:393-404) |
| M3 non-TTY fallback | Fixed — piped `ppd-simulator` and `ctl -i` verified clean (no prompt_toolkit warnings/garbling); minor residue T3 |
| M4 run-with-wait exit codes | Fixed — `run X wait` verified: PASSED→0, FAILED→1; new issues M2/M3 and doc issue H1 |
| L1 top-level usage for late-registered subcommands | Fixed — help shows `schedule (sched) [add\|clear\|days\|delete\|disable\|enable\|list\|time]`, `notify [...]`, `broadcast (bc) [...]` (verified live) |
| L2 subcommands hidden by arg help | Fixed — `pet help`/`battery help` show a Subcommands section (verified live) |
| L3 extra arguments silently ignored | Fixed — `close now please` / `power on off` → `Unexpected argument(s)` + usage (verified live) |
| L4 set_cli_mode(False) exit restore | Fixed — `_saved_exit_info` module ref, aliases stay a list (handler.py:38-40, 200-239) |
| L5 unescape order | Fixed — split-on-`\\\\`-first algorithm (prompt_common.py:80-87), round-trips verified by inspection |
| L6 ctl timeout message / truncated lines | Fixed — newline-terminated line parsing + `Response timeout after {t}s` (verified live) |
| L7 bare battery/holdtime show | Fixed — verified live (`Battery: 100%`, `Hold time: 2.0s`); `battery random` exists (docs stale: L1) |
| L8 basic-input disconnect race | Fixed — `_basic_readline` via `add_reader`, raced against stop_event (ctl.py:299-326, 463-496) |
| L9 history usage / raw default list | Fixed — `history (hist) [clear|N]`; `default_display="all"` used in `schedule add help` |
| L10 oneshot exit on interrupt | Fixed — `sys.exit(130)` in both binaries (cli.py:975-978, ctl.py:638-641) |
| T1 dead `interactive_mode_basic` | Fixed — removed from ctl.py |
| T2 `"str"` arg type | Fixed — timezone ArgSpec uses `"string"` (settings.py:311) |
| T3 run_simulator control_port docstring | Fixed (cli.py:535-536) |
| T4 door.md Schedule bools / on_schedule_change / hardware_version | Fixed — all present and matching door.py |
| T5 `--history` always registered | Fixed in code; docs stale (new L3) |
| T6 banner shows host:port | Fixed — `Simulator started on 127.0.0.1:39200` (verified live) |
| Security D7/D8 (loopback control default, `--control-host`, path-restricted `run`, sanitized output) | Verified — control binds 127.0.0.1, `--control-host` carries a strong warning, `run ../x` rejected over ctl with a clear message, control responses sanitized+escaped |

## Areas Reviewed With No Findings

- **cli/ctl command parity** — every daemon command (including new `pet` and
  `run ... wait`) reachable from both front ends; `exit`/`clear`/`history` correctly
  local-only in ctl and rejected by the daemon as "Unknown command"; CLI mode
  correctly re-aliases `exit`/`q`/`quit` to `shutdown` and hides the standalone
  `exit` (verified in live help output of both modes).
- **Help accuracy** — live `help` output cross-checked against every command module;
  all names, aliases, usage strings, and category groupings match the code, including
  regenerated usages for late-registered subcommands.
- **Tab completion accuracy** — probes across command, subcommand, first-arg,
  later-arg, choice, days, timezone (cache-backed), and script positions all correct;
  no stale or wrongly-positioned suggestions found (scripts-dir gap noted in M4).
- **Error message quality** — validation errors carry the offending value, the limit,
  and a usage line; unknown subcommands list alternatives; unknown scripts list
  available names; extra arguments rejected with usage. Verified live.
- **Exit codes** — ctl one-shot: 0/1 semantics verified for success, validation
  errors, unknown scripts, path rejection, connection refused; `--oneshot` verified 0
  on pass; both binaries exit 130 on SIGINT (code).
- **STATUS/prompt coloring behavior** — count-based, immune to control-client churn
  (the round-1 log-scrape bugs are gone); initial status delivered on connect.
- **Control-protocol hygiene** — sanitize-then-escape on the daemon side,
  unescape-then-sanitize on ctl; `LOG:`/`STATUS:` skipped by one-shot response
  parsing; empty-message and split-line responses handled.
- **docs/door.md** — every property, method, callback, enum member, and dataclass
  shape re-verified against door.py (including `cycle()`, `toggle()`, `latency`,
  `pet_proximity_keep_open`, `on_schedule_change`, `hardware_version`,
  `ScheduleTime`-based `Schedule`, bool `days_of_week`). Accurate.
- **docs/client.md** — constructor, lifecycle semantics (`connect` no-raise +
  `shutdown()`/`reset_shutdown()`), backoff description, `CommandError`/`TimeoutError`
  /`ConnectionError` future semantics, `(field, value)` listener signatures,
  notification-event listener, and all import blocks verified against client.py and
  the package root (imports executed successfully).
- **README** — quick-starts, schedule utilities, and the simulator CI example all
  verified (CI example run live, exit 0). Only the structure-tree omission (T2).
- **docs/simulator.md scripting section** — actions, conditions, settings, boolean
  literals, and programmatic API names (`trigger_sensor`, `set_pet_in_doorway`,
  `Script.from_simple_commands`, `CommandRegistry.handler`, simulator `Schedule`
  int-based fields) all verified against scripting.py/server.py/state.py/protocol.py.
- **History machinery** — recall, failed-command removal, alias canonicalization,
  `--history none`, and 0600 history-file permissions unchanged and coherent between
  cli and ctl.
