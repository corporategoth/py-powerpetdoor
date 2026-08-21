# Frontend Developer Analysis — Round 1

Persona: frontend engineer; scope for this project is the simulator terminal front end
(`ppd-simulator` interactive CLI, `ppd-simulator-ctl`, shared prompt machinery, command
modules' user-facing output) plus the library's public API surface as documented in
README.md, docs/door.md, docs/client.md, and docs/simulator.md.

All findings below were verified by reading the code and, where noted, by running the
actual binaries (`ppd-simulator --daemon` + `ppd-simulator-ctl` one-shot/interactive,
piped-stdin runs, prompt_toolkit-suppressed runs, and direct `SimulatorCompleter` probes).

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 4 |
| Medium   | 4 |
| Low      | 10 |
| Trivial  | 6 |

## Findings

### High

#### H1. Documented CLI flags `--exit-after-script`, `-e`, and `--script-file` do not exist

**Files:** `README.md:118-122`, `docs/simulator.md:71-93`, `.claude/CLAUDE.md:73`; actual flags in `src/powerpetdoor/simulator/cli.py:578-668`

Verified by running them — all three abort with an argparse error:

- `ppd-simulator --script basic_cycle --exit-after-script` → `error: unrecognized arguments: --exit-after-script`
- `ppd-simulator -s basic_cycle -e` → `error: unrecognized arguments: -e`
- `python -m powerpetdoor.simulator --script-file /tmp/x.yaml` → `error: unrecognized arguments: --script-file`

The real interface is `--oneshot` (no short flag) and `--script`/`-s` (auto-detects file
paths vs. built-in names). The "Exit Codes" section of docs/simulator.md (lines 83-93) and
the README's "Run in CI/CD" example are the exact snippets a CI user copy-pastes, and they
fail immediately. `.claude/CLAUDE.md:73` carries the same stale flag.

**Recommendation:** Replace `--exit-after-script`/`-e` with `--oneshot` and delete
`--script-file` (fold into the `--script` description) in README.md, docs/simulator.md,
and .claude/CLAUDE.md. Consider adding `-e` as a short alias for `--oneshot` in cli.py if
backward compatibility with the old docs is desired.

#### H2. docs/simulator.md "Interactive Mode" documents a UI that no longer exists; ctl and daemon mode are undocumented

**Files:** `docs/simulator.md:95-177, 663-690`; actual commands in `src/powerpetdoor/simulator/commands/*.py`

Verified against the live `help` output and the command registry:

- Keys `1`, `2`, `3` ("Add sample schedule #1/#2", "Delete schedule #1", lines 158-162) do
  not exist. The real interface is the `schedule` command suite (`schedule add/clear/delete/
  enable/disable/days/time`), which is not documented at all.
- Key `d` "Toggle pet in doorway" (line 124) does not exist as a command (see H4).
- Line 176 says "`?` or `status` — Show current door state"; `?` is actually an alias for
  `help` (info.py:212). The status aliases are `state`, `info`, `v`.
- Entire command families are missing from the docs: `timezone`, `broadcast`, `debug`,
  `history`, `clear`, `list`, `schedule ...`, and the implicit `help`/`?` subcommand system.
- The `ppd-simulator` / `ppd-simulator-ctl` entry points (pyproject.toml:60-61), `--daemon`
  mode, the control port, and the entire remote-control workflow are absent from
  docs/simulator.md — the ctl tool has zero documentation anywhere in docs/.
- CLI flags `--loop`, `--script-delay`, `--wait-for-client`, `--run-for`, `--daemon`,
  `--firmware`, `--hardware`, `--history` are undocumented.
- Architecture section (line 681) lists `run_simulator_interactive()`; the actual function
  is `run_simulator()` (cli.py:127). The `commands/`, `ctl.py`, and `prompt_common.py`
  modules are missing from the module tree.

**Recommendation:** Rewrite the Interactive Mode section from the current `help` output
(the tables are otherwise close — door ops, buttons, settings, battery, notify, scripts
all match), add a "Remote control (ppd-simulator-ctl)" section covering daemon mode,
control port, one-shot vs. `-i`, and exit codes, and refresh the flag list and
architecture diagram.

#### H3. docs/client.md import examples raise ImportError — 12 documented names are not exported from the package root

**Files:** `docs/client.md:207-225, 285-295`; exports in `src/powerpetdoor/__init__.py`

Verified at runtime: `CMD_GET_NOTIFICATIONS`, `CMD_SET_NOTIFICATIONS`,
`CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK`, `CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK`,
`CMD_ENABLE_AUTORETRACT`, `CMD_DISABLE_AUTORETRACT`, `CMD_ENABLE_CMD_LOCKOUT`,
`CMD_DISABLE_CMD_LOCKOUT`, `FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS`,
`FIELD_LOW_BATTERY_NOTIFICATIONS`, `FIELD_TOTAL_OPEN_CYCLES`, `FIELD_TOTAL_AUTO_RETRACTS`
are all `False` for `hasattr(powerpetdoor, ...)` but `True` for `powerpetdoor.const`.
The doc's "Message Types and Commands" block is a single `from powerpetdoor import (...)`
statement, so copy-pasting it fails wholesale, and the Listeners example (lines 287-294)
uses the `FIELD_*` names with no import shown at all.

**Recommendation:** Either add these commonly needed command/field constants to
`powerpetdoor/__init__.py` (`__all__` + import) — the better fix given the safety-lock,
autoretract, and notification commands are core device features — or change the doc
examples to `from powerpetdoor.const import (...)`.

#### H4. Pet-in-doorway simulation is unreachable from the CLI/ctl ("no hidden APIs" violation)

**Files:** `src/powerpetdoor/simulator/server.py:776` (`set_pet_in_doorway`),
`src/powerpetdoor/simulator/scripting.py:268-275` (`pet_presence`/`pet_on`/`pet_off`
actions), `src/powerpetdoor/simulator/commands/simulation.py` (only `obstruction`);
documented at `docs/simulator.md:124, 221-222`

Pet presence (door held open by a pet standing in the doorway) can be triggered
programmatically and from YAML scripts, and docs/simulator.md claims a `d` key toggles it
interactively — but no command exists in any command mixin, so neither the interactive CLI
nor ctl can exercise it. There is even a built-in `pet_presence_test` script for the
feature. This is the only simulator event with script/API support but no terminal command.

**Recommendation:** Add a `pet [on|off]` command (aliases `d`, matching the documented
key) to `SimulationCommandsMixin` calling `simulator.set_pet_in_doorway()`, following the
`bool_toggle` pattern used by `power`/`safety`. Then the docs claim becomes true.

### Medium

#### M1. Tab completion only ever completes the first argument — wrong suggestions for later argument positions

**Files:** `src/powerpetdoor/simulator/prompt_common.py:323-354` (`_get_arg_options_for_info`
reads `info.args[0]` only), `:365-414` (`get_completions` never counts consumed args)

Verified with `SimulatorCompleter` probes:

- `schedule add inside 6:00-22:00 <TAB>` → suggests `inside, outside, both, help`
  (the sensor choices again) instead of the days presets for the third argument.
- `schedule add inside <TAB>` → same wrong sensor choices for the time argument.
- `power on <TAB>` → suggests `t, toggle, on, off, help` even though the single argument
  is already consumed.

Additionally, the `days` arg type has no choices/completer, so `all`/`weekdays`/`weekends`
are never offered even in the correct position (they're also absent from `_OPTIONS`, so
the lexer never highlights them: `prompt_common.py:70-104`).

**Recommendation:** In `get_completions`, count words consumed beyond the command path and
index into `info.args[position]`; return nothing (or the arg's completer/choices) for that
position, and stop offering subcommands/first-arg options once an argument has been
consumed. Give the `days` type a completer returning `all/weekdays/weekends` plus day
names.

#### M2. ctl prompt color and status tracking scrapes log text and reacts to the wrong events

**Files:** `src/powerpetdoor/simulator/ctl.py:369-381`; log sources
`src/powerpetdoor/simulator/cli.py:274, 303` and
`src/powerpetdoor/simulator/protocol.py:184-188`

`socket_reader` flips `has_clients` on substring matches: `"Client connected"`,
`"Client disconnected"`, and `"connection closed"` (case-insensitive). The daemon logs
`"Control connection closed from ..."` (cli.py:303) whenever any control client
disconnects — including ctl's own `check_connection` probe at startup (observed live: the
first line printed in a ctl session is this log). So:

- When a second ctl session (or any probe/health check) disconnects, every other ctl's
  prompt turns gray ("disconnected") even though a door client is still connected.
- The code comment at ctl.py:376-378 admits multiple door clients aren't tracked: one
  door client disconnecting marks the prompt disconnected while others remain.

**Recommendation:** Stop scraping human-readable log lines. Have the control server emit a
structured status line (e.g., `STATUS: clients=<n>`) on door-client connect/disconnect —
the daemon already has `on_connect`/`on_disconnect` callbacks wired in cli.py:187-195 —
and key the prompt color off that count.

#### M3. Non-TTY stdin degrades badly: prompt_toolkit warning and garbled output instead of falling back to basic input

**Files:** `src/powerpetdoor/simulator/prompt_common.py:471-485` (PromptSession created
whenever prompt_toolkit imports), `src/powerpetdoor/simulator/cli.py:418-427` (checks
`fstat` but not `isatty`), `src/powerpetdoor/simulator/ctl.py:339-344`

Verified by piping stdin to both tools: `printf 'status\nexit\n' | ppd-simulator-ctl -i`
prints `Warning: Input is not a terminal (fd=0).` followed by repeated, misaligned prompts
(`127.0.0.1:39170> exit                     127.0.0.1:39170> exit`) and blank padding
lines. Both binaries already have perfectly good basic-input fallbacks (cli.py:485-523,
ctl.py:491-496) — they're just only used when prompt_toolkit is not installed, not when
the terminal is dumb/non-interactive.

**Recommendation:** Gate the prompt_toolkit session on `sys.stdin.isatty()` (and/or
`TERM=dumb`) in `InteractiveSession.__init__`/`create`, so piped/dumb-terminal sessions
use the plain-input path. This also makes scripted `ctl -i` transcripts clean.

#### M4. `run` via ctl one-shot cannot report script pass/fail — exit code only reflects queueing

**Files:** `src/powerpetdoor/simulator/commands/scripts.py:59-72` (queues when
`script_queue` is set), `src/powerpetdoor/simulator/cli.py:213-221` (queue always created),
`:317-338` (results only go to the daemon log), `src/powerpetdoor/simulator/ctl.py:689-693`

Verified live: `ppd-simulator-ctl -p PORT run basic_cycle` returns
`OK: Queued script: Basic Door Cycle` with exit code 0 immediately; the PASSED/FAILED
outcome appears only as a LOG line on the daemon, which one-shot mode never sees (the
socket closes after the first `OK:`). For the task's stated scripting use case ("exit
codes from ctl for scripting"), a CI job cannot use ctl to run a script against a live
daemon and branch on the result — only `ppd-simulator --script ... --oneshot` (a fresh
simulator) can.

**Recommendation:** Add a way to run synchronously through the control port — e.g.,
`run <script> wait` (or make one-shot ctl `run` bypass the queue via a
`ScriptRunner.run` await) so the `OK:`/`ERROR:` response and exit code reflect the actual
script result.

### Low

#### L1. Top-level help omits subcommand hints for late-registered subcommands (inconsistent with `ac`)

**Files:** `src/powerpetdoor/simulator/commands/base.py:365-367` (usage generated at
decoration time), `src/powerpetdoor/simulator/commands/handler.py:385-446` (subcommands
attached afterwards)

Observed in live `help` output: `ac [connect|disconnect|toggle]` shows its subcommands
(declared inline in `@command`), but `schedule (sched) - Manage schedules`,
`notify - Manage notification settings`, and `broadcast (bc) - ...` show no usage at all
because their subcommands are registered via `@subcommand` after usage was auto-generated.
A user scanning help has no cue that these have subcommands.

**Recommendation:** Regenerate `info.usage` in `register_all_subcommands()` /
`_register_subcommand_handlers()` after attaching subcommands (call
`info.generate_usage()` when `usage` was auto-generated).

#### L2. `<command> help` hides registered subcommands when the command also takes args

**Files:** `src/powerpetdoor/simulator/commands/handler.py:277-286` (args take precedence
in implicit help), e.g. `src/powerpetdoor/simulator/commands/buttons.py:57-81`

Verified: `power help` prints only `power [on|off]` and the `value` argument; the
registered `power toggle` (alias `t`) subcommand is never mentioned. Same for `auto`,
`inside_enable`, `outside_enable`, `safety`, `lockout`, `autoretract`, `battery_present`.
Tab completion offers `toggle` but help denies its existence.

**Recommendation:** In the implicit-help branch, append a "Subcommands:" section when
`info.subcommands` is non-empty even if `info.args` is set.

#### L3. Extra arguments are silently ignored

**Files:** `src/powerpetdoor/simulator/commands/handler.py:351-382` (`_parse_args` never
checks `len(parts) > len(arg_specs)`), `:274-311` (commands with no args ignore all parts)

Verified live: `close now please` → `OK: Closing door`; `power on off extra` → sets power
on and ignores the rest. Typos like `holdtime 5 3` (meant `5.3`) or
`schedule delete 0 1` (hoping to delete two) succeed while doing something narrower than
the user asked, with no warning.

**Recommendation:** Return an error (or at least append a warning to the result message)
when unconsumed argument parts remain: `Unexpected argument(s): ... \nUsage: ...`.

#### L4. `set_cli_mode(False)` permanently deregisters `exit`, `q`, and `quit`

**Files:** `src/powerpetdoor/simulator/commands/handler.py:170-220`

Verified live: after `set_cli_mode(True)` then `set_cli_mode(False)`, none of
`exit`/`q`/`quit` remain in the registry. The restore block (lines 216-220) is guarded by
`if "exit" in _command_registry:` — but `"exit"` was just deleted by the loop above it, so
the restore never runs, and it wouldn't restore the primary `"exit"` name anyway (it only
iterates `exit_info.aliases`). Latent today (cli.py only ever turns CLI mode on), but any
embedder or test toggling it breaks the ctl-side exit commands. Also note
`shutdown_info.aliases` mutates from `list` to `tuple` (lines 205, 211-213), violating the
dataclass's declared type.

**Recommendation:** Keep a module-level reference to the exit `CommandInfo` (not a
registry lookup post-deletion), restore `"exit"` plus its aliases from it, and keep
`aliases` a list.

#### L5. Control-protocol escaping is not correctly reversible (unescape order bug)

**Files:** escape `src/powerpetdoor/simulator/cli.py:287`; unescape
`src/powerpetdoor/simulator/ctl.py:267, 270, 384, 398`

Escaping does `\ → \\` then newline `→ \n` (correct order), but unescaping does
`\n → newline` **before** `\\ → \`. A message containing a literal backslash-n sequence
(e.g., an error echoing a Windows-style path `scripts\new.yaml`, escaped to `\\n`) is
unescaped to backslash+newline instead of `\n` — corrupted output. Low because such
messages are rare, but every ctl response passes through this code.

**Recommendation:** Unescape with a single left-to-right pass (or
`msg.encode().decode('unicode_escape')`-style two-token scan): split on `\\\\` first,
replace `\\n` within the segments, then rejoin with `\\`.

#### L6. One-shot ctl prints nothing on response timeout, and can return a truncated response line

**Files:** `src/powerpetdoor/simulator/ctl.py:244-277`

Two issues in `send_command`:
1. If the daemon accepts the connection but sends no `OK:`/`ERROR:` line within the
   timeout, the inner `except socket.timeout: break` falls through to
   `return (False, "")` — the user gets an empty line and exit code 1 with no explanation
   (contrast: connect timeout gets "Connection timed out to host:port").
2. The parser scans `decoded.split("\n")` after every `recv(4096)` and returns the first
   line starting with `OK:`/`ERROR:` — without requiring the line to be
   newline-terminated. A response longer than one TCP segment (help is ~1.6 KB; large
   schedule lists grow unbounded) can be returned truncated mid-line.

**Recommendation:** Return an explicit `"Response timeout after {timeout}s"` message on
recv timeout, and only treat a line as complete when the buffer contains its trailing
`\n`.

#### L7. Inconsistent no-argument semantics: `battery` mutates to a random value; `holdtime` cannot show at all

**Files:** `src/powerpetdoor/simulator/commands/settings.py:171-193` (`battery`),
`:149-169` (`holdtime`), vs. `charge_rate`/`discharge_rate`/`timezone`/`debug`/`notify`
which display current state when called bare

Value-style commands follow a "bare = show current value" convention (`charge_rate`,
`discharge_rate`, `timezone`, `debug`, `notify`) — except `battery`, where a bare
invocation *sets a random level* (a user checking the level changes it), and `holdtime`,
where a bare invocation errors (`Missing required argument`) with no way to view the
current value except full `status`. Repeatability/consistency issue; the random behavior
is at least documented in its description.

**Recommendation:** Make bare `battery` and bare `holdtime` show the current value; move
randomization to an explicit `battery random`.

#### L8. ctl basic-input fallback doesn't notice daemon disconnect until Enter is pressed

**Files:** `src/powerpetdoor/simulator/ctl.py:491-496` vs. the prompt_toolkit path
`:464-489`

The prompt_toolkit path races `prompt_async()` against `stop_event` so a daemon shutdown
immediately ends the session. The fallback path awaits
`run_in_executor(None, input, ...)` directly with no race — after the daemon dies (e.g.,
another client sent `shutdown`), the user sits at a live-looking prompt until they press
Enter, and only then sees the failure. Inconsistent behavior between the two modes.

**Recommendation:** Wrap the executor future in the same `asyncio.wait(...,
FIRST_COMPLETED)` race used by the prompt_toolkit path (the pending `input()` thread can
be abandoned on exit).

#### L9. Help-text polish: opaque `[arg]` usage and raw Python list leaked as a default

**Files:** `src/powerpetdoor/simulator/commands/info.py:308-324` (`history` ArgSpec named
"arg"), `:106-112` (`default:` rendered with `str()`),
`src/powerpetdoor/simulator/commands/schedules.py:101-107`

Observed live: help shows `history (hist) [arg]` (compare `battery (b) [percent]`), and
`schedule add help` prints `days: ... (default: [1, 1, 1, 1, 1, 1, 1])` — an internal
Python list where the user-facing vocabulary is `all`.

**Recommendation:** Name the history arg something meaningful (`[clear|N]` via
usage/choices), and let ArgSpec carry a display default (e.g., `default_display="all"`)
used by `_get_arg_help`.

#### L10. `--oneshot` CI runs exit 0 when interrupted

**Files:** `src/powerpetdoor/simulator/cli.py:721-743`

In `main()`, `KeyboardInterrupt` prints "Simulator stopped." and returns normally, so a
SIGINT during a `--oneshot` script run (whose whole purpose is a meaningful exit code,
per the `sys.exit(0 if result else 1)` at line 740) yields exit code 0 — an aborted test
run reports success to CI.

**Recommendation:** In the `KeyboardInterrupt` handler, `sys.exit(130)` (or at least
exit 1 when `--oneshot` was requested).

### Trivial

#### T1. Dead code: `interactive_mode_basic` in ctl.py is never called

**Files:** `src/powerpetdoor/simulator/ctl.py:557-615`

`interactive_mode()` (line 618) always dispatches to `interactive_mode_async`, which has
its own internal fallback. The 59-line `interactive_mode_basic` duplicates that logic
(with drift: it strips `OK:`/`ERROR:` prefixes differently and special-cases `shutdown`)
and is unreachable. Remove it or wire it up as the non-TTY path (see M3).

#### T2. `timezone` ArgSpec uses undeclared arg type `"str"`

**Files:** `src/powerpetdoor/simulator/commands/settings.py:326`,
`src/powerpetdoor/simulator/commands/base.py:47` (documented types include `"string"`,
not `"str"`)

Works only because `parse_arg` has a permissive `else` fallthrough (base.py:151-152).
Change to `"string"` so validation/completion logic treats it uniformly.

#### T3. `run_simulator` docstring claims control port defaults on in script mode

**Files:** `src/powerpetdoor/simulator/cli.py:155` vs. `:689-700`

Docstring: "control_port: Port for control commands (default: port + 1 in daemon/script
mode)" — `main()` only assigns a control port in daemon mode; script mode gets none.
Fix the docstring (or actually enable the control port for script runs).

#### T4. docs/door.md minor drift: `days_of_week` shown as ints; `on_schedule_change` and `hardware_version` undocumented

**Files:** `docs/door.md:320-329, 419-426, 336-356`; `src/powerpetdoor/door.py:186-208,
793, 1001`

`Schedule.days_of_week` is now `list[bool]` (recent change); docs still show
`[1, 1, 1, 1, 1, 1, 1]` / "1 = active" (functionally equivalent, but the doc no longer
matches the dataclass). The Callbacks section omits `on_schedule_change`, and the
Hardware Properties table omits the `hardware_version` property. Everything else in
door.md was verified accurate against door.py.

#### T5. `--history` flag exists only when prompt_toolkit is installed

**Files:** `src/powerpetdoor/simulator/cli.py:651-658`, `src/powerpetdoor/simulator/ctl.py:668-674`

The same command line (`ppd-simulator --history none`) is valid on one machine and an
argparse error (`unrecognized arguments`) on another depending on an optional dependency.
Registering the flag unconditionally and ignoring it (with a note) when prompt_toolkit is
missing would make invocations portable.

#### T6. Startup banner omits the bound host

**Files:** `src/powerpetdoor/simulator/cli.py:233`

`Simulator started on port {port}` — when `--host` is non-default the user must read the
earlier log line to know the bind address. Print `{host}:{port}` for symmetry with the
prompt.

## Areas Reviewed With No Findings

- **ctl one-shot exit codes for normal commands** — verified live: success → 0, unknown
  command / validation error / connection refused → 1; response printed with `OK:`/`ERROR:`
  prefix (ctl.py:689-693). Works well for scripting apart from M4.
- **Error message quality for bad input** — verified live: type errors, min/max violations,
  and missing arguments all include the offending value, the limit, and a usage line
  (`'10000' is above maximum (900)\nUsage: holdtime <seconds>`); unknown subcommands list
  the available ones (`Unknown ac subcommand: bogus / Available: connect, disconnect,
  toggle`); unknown script names list all available scripts. Excellent, actionable errors.
- **cli/ctl command parity** — every daemon command is reachable from both front ends; the
  only local ctl commands (`exit`, `clear`, `history`) are correctly interactive_only +
  local_only, hidden from daemon help, and rejected by the daemon with a consistent
  "Unknown command" message. `exit`/`q`/`quit` correctly become `shutdown` aliases in CLI
  mode (verified in live help output) and remain "exit the client" in ctl.
- **Help visibility rules** — daemon help hides interactive-only commands; ctl-local help
  correctly shows `history`, `clear`, `exit`; fallback (no prompt_toolkit) CLI correctly
  hides `history` from help and from execution. Verified live in all three modes.
- **History machinery** — `!!`/`!n`/`!-n` recall, failed-command removal, and
  alias-to-canonical rewriting in `InteractiveSession.handle_result` /
  `History.resolve_recall` are coherent between cli and ctl; `--history none` cleanly
  degrades to in-memory history.
- **cli.py prompt connection coloring** — uses direct `on_connect`/`on_disconnect`
  callbacks plus `invalidate()` (cli.py:187-195), not log scraping; correct (contrast M2).
- **prompt_toolkit-absent operation** — verified live with the import suppressed: CLI
  falls back to a plain prompt, commands execute, `q` shuts down cleanly; ctl similarly
  functional.
- **docs/simulator.md scripting section** — all documented actions (`trigger_sensor`,
  `open`, `close`, `obstruction`, `pet_on/off`, `battery`, `wait`, `wait_for`, `set`,
  `toggle`, `add_schedule`, `remove_schedule`, `assert`, `log`), condition names, settings
  list, boolean literal list, and the built-in script table were verified against
  scripting.py and the scripts/ directory — accurate.
- **README quick-start examples** — the `PowerPetDoor` and `PowerPetDoorClient` examples
  and the schedule-utilities snippet were verified against door.py/client.py/schedule.py
  exports and signatures (including `compute_schedule_diff`'s two-tuple return) — all
  importable and correct. The only README defect is the CI flag (H1).
- **docs/client.md non-import content** — constructor parameters, listener/handler
  registration API (`add_listener`/`del_listener`/`add_handlers`/`del_handlers`),
  `send_message(notify=True)` future semantics, `PrioritizedMessage` fields, and
  `find_end`/`make_bool` all verified against client.py.
- **Output formatting** — status display, schedule formatting (`#0: inside sensor,
  weekdays, 07:00-18:00 (enabled)`), notification table alignment, and units (`s`,
  `%/min`, `%`) are consistent across commands.
- **Syntax highlighting lexer** — command/alias/subcommand/option/number classification
  verified against the registry traversal logic, including `help`/`?`
  pseudo-subcommands; no misclassification found (days presets aside, noted in M1).
