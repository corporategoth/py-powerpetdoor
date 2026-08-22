# Frontend Developer Analysis — Round 6

Commit: `8a24804` (Round 5 fixes from persona analysis)
Scope: the simulator terminal front end (`cli.py`, `ctl.py`, `prompt_common.py`,
`commands/*`, `scripting.py`) plus the library's public API and prose docs as a
"developer front end". No web UI exists.

Method: read the whole front end, then **live-tested both binaries** — a
`--daemon --scripts-dir` daemon driven through ~50 one-shot `ppd-simulator-ctl`
invocations and raw control-channel sessions, plus piped-stdin `ppd-simulator`
and `ppd-simulator-ctl -i` sessions, plus in-process introspection of the
tab-completer. Every finding below was reproduced against a running binary or
against executed library code, not inferred from reading.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 6 |
| Trivial | 2 |

The **QueuedScript / veto machinery (round-5 M1) is correct** under every
ordering I could construct, including the claim-window race it exists to close.
The single Medium is a wrong wire example in `docs/protocol.md`; the Lows are
split between three narrow simulator-UX gaps and four doc-accuracy defects in
the round-5 L3 additions.

---

## Findings

### M1 (Medium) — `docs/protocol.md` documents the keepalive payload as an empty string; the real contract requires an echoed token

**Where:** `/home/prez/src/pypowerpetdoor/docs/protocol.md:146-158`

```json
**Request**:  {"PING": "", "msgId": 1, "dir": "p2d"}
**Response**: {"CMD": "PONG", "PONG": "", "success": "true", "dir": "d2p"}
```

**Truth:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/client.py:1467-1469`
sends a wall-clock-milliseconds token as the `PING` value
(`self._last_ping = str(round(time.time() * 1000))`), and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/client.py:1017-1022` accepts a
PONG **only** when `msg.get(PONG) == self._last_ping`. The simulator gets this
right (`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/protocol.py:445-449`
echoes `PONG: msg[PING]`), so the doc is the odd one out.

**Impact.** This is the only place the keepalive frame is documented, and it is
the contract for anyone writing a second implementation — which this project
explicitly has (`ha-powerpetdoor`, the Ostinato plugin, and any alternate
simulator). A responder built literally from that example never matches, so
`_failed_pings` climbs and `keepalive()`
(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/client.py:1447-1462`) drops the
connection after `MAX_FAILED_PINGS = 3` — i.e. a hard disconnect every ~90s at
the default interval, reported as `Last PING not responded to 3 times.` even
though the peer *is* answering every ping. That is the worst possible failure
shape: the doc is wrong, the symptom points at the network, and the actual rule
(echo the token verbatim) appears nowhere.

Two smaller things in the same neighbourhood: the request example omits that
`msgId` must be echoed back for correlation, and nothing states that the ping
token is opaque to the device (any value works as long as it comes back).

**Suggested fix:** show a real token and say the response must echo it, e.g.
`{"PING": "1710000000123", "msgId": 1, "dir": "p2d"}` /
`{"CMD": "PONG", "PONG": "1710000000123", "success": "true", "dir": "d2p"}`,
with one sentence: *"The `PONG` value must be the exact `PING` value; a
mismatched or empty `PONG` is counted as a failed ping and three in a row drop
the connection."*

---

### L1 (Low) — a symlinked script inside `--scripts-dir` is advertised in three places and refused by the fourth, with a self-contradictory error

**Where:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/scripts.py:231-240`
(`_load_script_by_name`, the `candidate.parent == base` guard) vs
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:786-793`
(`_script_files_in`, a plain glob) and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:820-833`
(`get_builtin_script`'s `Available:` hint).

Reproduced with `ln -s /tmp/ppdout/outside.yaml /tmp/ppdscripts/linked.yaml`:

```
$ ppd-simulator-ctl -p 3901 list
  ...
  linked: Lives outside the scripts dir          <- listed, description read from the target
$ ppd-simulator-ctl -p 3901 run linked
ERROR: Unknown script: linked. Available: basic_cycle, failing, full_test_suite,
       linked, long1, long2, ...                 <- refused, and listed as available
```

Tab completion offers it too (verified in-process:
`run <TAB>` → `('linked', 'Lives outside the scripts dir')`), and
`--list-scripts` prints it.

Three surfaces say the script exists — one of them by *reading it* to print its
description — and the fourth says `Unknown script` while re-printing the name in
its own `Available:` list. The operator is told the thing both does and does not
exist, with no hint that a path policy is involved and no way to act on it.
Contrast the sibling refusal on the ctl path, which is explicit and actionable:
`Script paths are not allowed over the control channel; use a bare script name
(see 'list')`.

It is also inconsistent with the CLI's own policy. In the interactive CLI, where
`_allow_script_paths` is True, `run /tmp/ppdout/outside.yaml wait` runs happily
(verified: `>>> Script PASSED: Outside Script`) while `run linked` — the *same
file*, reached through the directory the operator explicitly configured — is
refused. The guard buys nothing there; it only costs usability. Over ctl the
guard does matter, but then the right answer is a message that says so.

Nothing in `docs/simulator.md:306-323` mentions the restriction.

**Suggested fix:** either resolve symlinks whose target the operator configured
(the base-dir check is redundant on the CLI path, and on the ctl path the bare
name has already been validated as separator-free), or, if the refusal stays,
make it explain itself — `Script 'linked' is a symlink out of <dir> and cannot
be run by name` — and drop such entries from `list`, `--list-scripts`, the
completer and the `Available:` hint so all four surfaces agree.

**Judgement on the "deliberately left cosmetic item":** the `list` entry alone
would be defensible. The `Unknown script: linked. Available: ..., linked, ...`
error is not — a message that contradicts itself inside one line is a real
defect, not cosmetics.

---

### L2 (Low) — `stop` never mentions the queue, and a repeated `stop` silently chews through it

**Where:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/scripts.py:348-354`

Reproduced on one control-channel session:

```
'run long1'  -> OK: Queued script: Long Script A
'run long2'  -> OK: Queued script: Long Script B
'run quick'  -> OK: Queued script: Quick Script
  list: Script: running "Long Script A" (2 queued) | Queued: Long Script B, Quick Script
'stop'       -> OK: Stopping script: Long Script A
  list: Script: running "Long Script B" (1 queued) | Queued: Quick Script
'stop'       -> OK: Stopping script: Long Script B
  list: Script: none running
'stop'       -> ERROR: No script is running (use 'shutdown' to stop the simulator)
```

Three `stop` commands consumed a three-deep queue, and **no `stop` response ever
mentioned that a queue existed.** Two problems:

1. **Clarity.** The `stop all` path reports `(dropped N queued)`; plain `stop`
   reports nothing, even though the observable consequence of `stop` on a
   non-empty queue is that *a different script immediately starts driving the
   door*. A one-shot `ctl stop` prints its `OK:` line and exits — the only
   notice that the next run began is a daemon `LOG:` line the one-shot client
   never displays. The operator's mental model after `OK: Stopping script: Long
   Script A` is "the door is now idle"; it isn't.

2. **Repeatability / idempotency.** `docs/simulator.md:241` promises "a repeat
   `stop` answers `Stop already requested for: <name>`". That guarantee holds
   only while the request is still pending, and since round 5 made `wait` steps
   interruptible (`_sleep_or_stop`) the pending window is usually sub-millisecond.
   In practice a repeat `stop` is destructive: it kills the *next* script. The
   persona's idempotency rule ("if I restart a task after starting an action, I
   don't start a second action") is violated for the commonest reason anyone
   re-issues `stop` — checking whether the first one landed.

The message is never *false* (it always names the script it actually stopped),
which is why this is Low rather than Medium.

**Suggested fix:** mirror `stop all`'s suffix on plain `stop` when the queue is
non-empty — `Stopping script: Long Script A (2 still queued; use 'stop all' to
discard them)`. That closes both gaps with one string and needs no behaviour
change.

---

### L3 (Low) — a `--scripts-dir` script that shadows a built-in produces ambiguous `list` and completion output

**Where:** `/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/scripts.py:257-291`
(`list_scripts` concatenates the two sets) and
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/scripting.py:913-929`
(the completer does the same).

Reproduced by dropping a `basic_cycle.yaml` into the scripts dir:

```
$ ctl list | grep basic
  basic_cycle: Pet triggers inside sensor, door opens, holds, then closes
  basic_cycle: This shadows the built-in
$ ctl run basic_cycle wait
OK: Script PASSED: SHADOWING basic_cycle
$ # completer:
[('basic_cycle', 'Pet triggers inside sensor, ...'), ('basic_cycle', 'This shadows the built-in')]
```

`list` prints the same name twice with two different descriptions and no marker
of which one `run` will pick; tab completion offers the identical string twice,
so it cannot be used to disambiguate either. Over ctl the built-in becomes
genuinely unreachable — paths are refused, so there is no second way to name it.

The *precedence* is documented (`docs/simulator.md:307-308`: scripts-dir first,
then built-ins) — credit where due — but the front end's own output does not
reflect it. This is a plausible accident (copy a built-in into your scripts dir
to tweak it), not a contrived one.

**Suggested fix:** mark the loser in both surfaces, e.g. print
`basic_cycle: Pet triggers... (shadowed by /tmp/ppdscripts/basic_cycle.yaml)`
under the built-in header and suppress the duplicate completion, or warn once at
startup alongside the existing empty-directory warning.

---

### L4 (Low) — `docs/client.md` constant index links two rows to sections that do not document those constants

**Where:** `/home/prez/src/pypowerpetdoor/docs/client.md:344-359`

The index opens with an explicit promise — *"Their values and semantics are
documented in docs/protocol.md — this is the name-to-group index"* — and two
rows do not keep it:

| Row | Doc link | What is actually there |
|-----|----------|------------------------|
| `docs/client.md:357` "Hardware / firmware" → `[Hardware Info](protocol.md#settings-fields)` | `docs/protocol.md:496-510` | The Settings Fields table has no `ver` / `rev` / `fw_maj` / `fw_min` / `fw_pat` rows at all. Those wire names (`const.py:74-78`) appear only in the `GET_HW_INFO` example at `docs/protocol.md:371-387`, whose anchor is `#query-commands`. The link *text* ("Hardware Info") also matches no heading in the file. |
| `docs/client.md:355` "Status payload" → `[Door Status Values](protocol.md#door-status-values)` | `docs/protocol.md:589-600` | A bare list of `DOOR_*` values. It mentions neither the `door_status` key nor `sensorState`. `FIELD_SENSOR_STATE` (`const.py:151`, `"sensorState"`, domain `"on"`/`"off"`) is documented under Notification Messages, `docs/protocol.md:526-537`. |

Both anchors resolve, so nothing 404s — the reader simply lands somewhere that
does not answer the question, which is worse than a dead link because it looks
like the constant is undocumented.

**Suggested fix:** retarget to `#query-commands` and
`#notification-messages-door-to-client` respectively, and add `ver`/`rev`/`fw_*`
rows to the Settings Fields table (or rename the link text to match the real
destination).

---

### L5 (Low) — the `PRIORITY_*` table contradicts itself and the implementation

**Where:** `/home/prez/src/pypowerpetdoor/docs/client.md:566-572`

```
| PRIORITY_MEDIUM | 2 | Settings changes (enable/disable, power, SET_*) |
| PRIORITY_LOW    | 3 | Status queries (GET_*) and schedule commands     |
```

`CMD_SET_SCHEDULE`, `CMD_SET_SCHEDULE_LIST` and `CMD_DELETE_SCHEDULE` are all
`PRIORITY_LOW` (`/home/prez/src/pypowerpetdoor/src/powerpetdoor/const.py:216-220`),
so the two rows disagree about `SET_SCHEDULE` — one table, two answers. Only
`SET_NOTIFICATIONS`, `SET_HOLD_TIME`, `SET_TIMEZONE` and the two
`SET_*_TRIGGER_VOLTAGE` commands are MEDIUM (`const.py:191-195`).

The four numeric values are correct. Also unstated anywhere: the fallback rule
`COMMAND_PRIORITIES.get(arg, PRIORITY_LOW)`
(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/client.py:1780`) — anything not
in the map is LOW, which is exactly what a caller passing a hand-rolled command
needs to know.

**Suggested fix:** change the MEDIUM row to "Settings changes (enable/disable,
power, `SET_NOTIFICATIONS`/`SET_HOLD_TIME`/`SET_TIMEZONE`/`SET_*_TRIGGER_VOLTAGE`)"
and add the default-LOW sentence.

---

### L6 (Low) — `add_listener`'s callback contract is documented wrongly in two places

**(a) Value type.** `/home/prez/src/pypowerpetdoor/docs/client.md:474-475`
documents `sensor_update` and `notifications_update` as
`{field: (field: str, val: bool)}`. The real signature is
`dict[str, Callable[[str, bool | None], None]]`
(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/client.py:486-487`), and the
source docstring says so — *"the coerced boolean (or None if unrecognized)"*
(`client.py:508-510`), because `make_bool` returns `None` for any string outside
its accepted set (`client.py:248`). A consumer who writes `if val:` on the
documented type silently maps "device sent something we could not parse" onto
"False", which for a safety lock or command lockout is the wrong way to fail.

**(b) Hold-time units, in the source docstring.**
`/home/prez/src/pypowerpetdoor/src/powerpetdoor/client.py:518` says
*"hold_time_update: Called with hold time in **seconds**"*. It is centiseconds:
`client.py:770-771` and `client.py:935-939` forward the raw device value, which
is exactly why `PowerPetDoor` divides by 100
(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/door.py:1318-1320`). Here the
prose doc is right (`docs/client.md:480` says centiseconds) and the docstring is
wrong — so the two developer-facing surfaces disagree, and the one that wins in
an IDE tooltip / `help()` is the wrong one. A caller trusting it displays "200
seconds" for a 2-second hold.

**Suggested fix:** `val: bool | None` in the table with a one-line note, and
`seconds` → `centiseconds` in the docstring.

---

### L7 (Low) — `DOOR_STATUS` is listed as a message *type*, and the frame it names is documented nowhere

**Where:** `/home/prez/src/pypowerpetdoor/docs/protocol.md:101-109`

The Message Types table's "Field" column lists `"cmd"`, `"config"`, `"PING"`,
`"PONG"` and `"DOOR_STATUS"`. The first three genuinely are envelope keys.
`DOOR_STATUS` is not — it is a **`CMD` value**. The real unsolicited frame is

```json
{"CMD": "DOOR_STATUS", "door_status": "DOOR_RISING", "success": "true", "dir": "d2p"}
```

(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/protocol.py:946-955`,
consumed at `/home/prez/src/pypowerpetdoor/src/powerpetdoor/client.py:716` via
`@ResponseHandlerRegistry.handler(CMD_GET_DOOR_STATUS, DOOR_STATUS)`), and that
shape appears nowhere in `protocol.md` — the only `door_status` payloads shown
(`protocol.md:183,340`) are *responses to a request*, not pushes. So the one
device-initiated frame a client must handle without having asked for it is both
mis-classified and unspecified.

By contrast the sibling push frames — the notification events — are documented
properly at `protocol.md:526-543`, including the bare-envelope rule and the
CMD-style variant. `DOOR_STATUS` deserves the same paragraph.

**Suggested fix:** move the row out of the envelope-key table into a short
"Unsolicited door status" block next to Notification Messages, with the real
JSON.

---

### T1 (Trivial) — the `History` class carries a dead duplicate of the live `history` command, and the two disagree

`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/history.py`
exposes `execute_command()`, `format_entries()`, `clear()` and `get_entries()`.
Production code calls **none of them** — only tests do
(`tests/simulator/test_commands_history.py`). The live `history` command
re-implements clear + formatting inline against the raw prompt_toolkit object
(`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/info.py:341-399`).
Production uses only `remove_last_entry` / `replace_last_entry` / `resolve_recall`
(`prompt_common.py:695-706`, `662`).

The two copies have already drifted on user-facing text — `History` answers
`"History not available (install prompt_toolkit)"` and `"Error clearing
history"`, the live command answers `"History not available. Install
prompt_toolkit for history support:\n  pip install
pypowerpetdoor[interactive]"` and `"Error clearing history: {e}"`. No user sees
the difference today (the `History` copy is unreachable), which is why this is
Trivial — but it is a trap: the well-documented class with the full docstring is
the one a maintainer will edit, and editing it changes nothing. This is also
exactly the "two implementations = refactor" rule in `CLAUDE.md`.

**Suggested fix:** have `InfoCommandsMixin.history()` delegate to
`self._history_obj.execute_command(arg)` (it is already stored, `handler.py:190`)
and delete the inline copy, keeping whichever message text is preferred.

### T2 (Trivial) — `_NOTIFY_DEFS` is dead

`/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/commands/notifications.py:22-28`
defines a five-entry `(subcommand, attr, description, aliases)` table that is
referenced nowhere in `src/` or `tests/`. The same five definitions are spelled
out a second time in the `@subcommand` decorators and a third time in
`notify()`'s hand-padded display block. Either drive the decorators/display from
the table or delete it.

---

## Round 5 Fix Verification

All eleven round-5 items verified against running binaries.

| Item | Status | Evidence |
|------|--------|----------|
| **M1** — `QueuedScript`, cancellable claims, `on_start` veto, one `stop all` | ✅ **Verified, thoroughly** | See the dedicated section below. |
| **L1** — `ConnectionError` caught ahead of the generic handler, logged at DEBUG | ✅ Verified | **0** `[ERROR]` lines in the daemon log across a session with **44** control connections (one-shot `ctl` hangs up mid-write on essentially all of them). Also 0 `[WARNING]`. |
| **L2** — callable `ArgSpec.description`; path policy single-sourced in `CommandHandler.__init__` | ✅ Verified | `ctl run help` → `script: Script name (paths are not accepted over the control channel) [required]`; the same `run help` typed into the interactive CLI → `script: Script name or file path [required]`. Confirmed the daemon sets the module flag itself via `set_script_paths_allowed(allow_script_paths)` (`handler.py:113`), so the daemon's own help now tracks its own policy rather than ctl's. `ctl run ./x.yaml` → `Script paths are not allowed over the control channel; use a bare script name (see 'list')` — help and behaviour agree. |
| **L3** — undocumented exports 49/121 → 0/121, pinned by test | ⚠️ **Presence verified, accuracy partial** | `tests/test_exports.py` passes (32 passed). I additionally checked the pin's strength: no export is documented *only* as a substring of a longer token (regex word-boundary sweep over `README.md` + `docs/*.md` — zero hits), so the plain `in` check is not being satisfied accidentally. **However**, correctness of the new prose is not pinned, and findings **M1, L4, L5, L6, L7** above are all defects *inside* the L3 additions. Verified-correct by execution: the whole `docs/door.md:278-303` timezone block (`get_posix_tz_string("America/New_York")` → `'EST5EDT,M3.2.0,M11.1.0'`, `find_iana_for_posix` → `'America/Detroit'`, the round-trip identity, `parse_posix_tz_string(...)["std_abbrev"]` → `'EST'`), the four `PRIORITY_*` values, `week_0_*` semantics, `PrioritizedMessage`, `CommandError.cmd/.reason`, `DoorStatus` mapping, and every `CMD_*`/`FIELD_*`/`DOOR_STATE_*` wire value against `const.py`. See also the caveat below. |
| **T1** — `stop all` idempotent | ✅ Verified | `ctl stop all` on an idle daemon → `OK: Nothing running or queued`, rc=0, repeatable (ran it back to back). `ctl stop` on the same idle daemon still correctly fails with rc=1 and the `shutdown` hint. |
| **T2** — `Queued:` prints display names | ✅ Verified | `Queued: Long Script B, Long Script B, Quick Script` — names, not `./scripts/long2.yaml`, and duplicates preserved as distinct entries. |
| **T3** — completions use `ArgSpec.describe()` | ✅ Verified in-process | `stop <TAB>` → `[('all', "'all' to discard every queued run as well"), ('help', ...)]`; `history <TAB>` → `[('clear', "'clear' to clear history, or N to show last N commands"), ...]`. No value-echoed-at-itself meta remains. |
| **T4** — ctl epilog | ✅ Verified | `ppd-simulator-ctl` with no args prints the six examples plus the exit-code paragraph and exits 1. |
| **T5** — empty `--scripts-dir` header | ✅ Verified | Both `--list-scripts` and the `list` command print `Scripts from /tmp/emptyscripts:` followed by `  (none)`; startup additionally logs `[WARNING] No *.yaml/*.yml scripts found in /tmp/emptyscripts`. |
| **T6** — `format_recall` used at both live call sites | ✅ Verified by inspection | `cli.py:819` and `ctl.py:616` both call `session.format_recall(input_line)`; `prompt_common.py:708-726` routes through `render_result`, so both echoes are sanitized. No raw f-string terminal writes remain on those paths. |

### M1 in depth — the QueuedScript / veto machinery

This was the round's main focus. Every ordering behaved correctly.

- **Queue depth and names are accurate while running.** `run long1; run long2;
  run long2; run quick` → `Script: running "Long Script A" (3 queued)` /
  `Queued: Long Script B, Long Script B, Quick Script`. `status` agrees with
  `list` on the same line, and the two `Long Script B` entries stay distinct
  (identity, not equality) exactly as `QueuedScript`'s docstring claims.
- **The claim window is real and correctly counted.** With `run long1 wait`
  holding the run lock, a subsequent plain `run quick` is claimed by the consumer
  and parks on the lock; `list` correctly reported `(1 queued)` /
  `Queued: Quick Script` for a run that a bare `asyncio.Queue.qsize()` would have
  reported as zero.
- **The veto fires and is audible.** `stop all` in that window →
  `OK: Stopping script: Long Script A (dropped 1 queued)`; the daemon logged
  `Dropped queued script: Quick Script`; the script's own log line
  (`[SCRIPT] quick ran`) **never appeared** — grep count 0. The abandoned run
  executed zero steps and, correctly, produced no `Script FAILED:` line
  (`cli.py:426` gates on `entry.cancelled`).
- **Counts always match, and one `stop all` is always enough.** Four back-to-back
  trials of "queue 5, `list`, `stop all`, `list`": every trial reported
  `running "Long Script A" (4 queued)` then
  `Stopping script: Long Script A (dropped 4 queued)` then `Script: none
  running`. Never a leaked run, never a second `stop all` needed, drop count
  always equal to the depth `list` had just printed.
- **Ordering of `stop all` relative to a claim is safe by construction.**
  `clear()` marks claimed entries cancelled and `start()` re-checks the flag
  after `release()`, both synchronously, so there is no interleaving point
  between them; and `_process_script_queue`'s `finally: release(entry)` covers
  the load-failure path that never reaches `on_start`.
- **`stop all` with nothing running but a claimed entry** returns
  `Dropped N queued script(s)` — I could not reach this branch from the outside
  (the consumer only claims once the previous run has fully released the lock,
  so `busy` is essentially always True whenever a claim is outstanding), but it
  is correct defensively.
- **`run X wait` still refuses rather than queues.** With `long1` running,
  `ctl run long2 wait` → `ERROR: Another script is already running: Long Script A`,
  rc=1, immediately.

The only wart on this surface is L2 above, and it is about the *plain* `stop`
message, not the queue mechanics.

### Caveat on the L3 pin

`test_every_exported_name_appears_in_the_prose_docs` is a substring membership
test. I confirmed it is not currently passing by accident (no export is only
present as part of a longer identifier), but it cannot tell a *correct*
description from a wrong one, and it cannot tell a description from a bare
mention. 65 of the 121 exports occur exactly once in the whole corpus, and a
subset of those are name-only entries in the index table — `DOOR_STATUS`,
`PONG`, `FIELD_SETTINGS` (only inside a code sample at
`docs/simulator.md:957`), the six envelope `FIELD_*`, `DOOR_TO_PHONE` /
`PHONE_TO_DOOR`, the eight `DOOR_STATE_*` (name-to-value mapping stated
nowhere), and ~29 `CMD_*` that appear only inside the import block at
`docs/client.md:245-307`. That last group is the weakest: `CMD_GET_AUTO ==
"GET_TIMERS_ENABLED"`, `CMD_ENABLE_AUTO == "ENABLE_TIMERS"`, `CMD_DISABLE_AUTO
== "DISABLE_TIMERS"` (`const.py:102,112-113`) are non-obvious renames, and no
doc connects the constant name to the wire string, so a reader looking at
`protocol.md`'s `GET_TIMERS_ENABLED` row cannot find the import that produces
it. Not raised as a separate finding — the L3 goal (nothing undocumented) was
met — but the natural round-7 follow-up is a name→value column in the constant
index, which would also close L4.

---

## Areas Reviewed With No Findings

- **`stop` / `stop all` message matrix.** Every branch of `stop_script`
  exercised live: nothing running (`shutdown` hint, rc=1), nothing running +
  `all` (idempotent success), running (names the script), running + `all`
  (`(dropped N queued)`), repeat while a stop is genuinely pending
  (`Stop already requested for:`). All accurate for the state they describe.
- **`run` argument handling.** Missing arg, extra arg, unknown script, path over
  ctl — all four produce a correct message plus `Usage: run <script> [wait]`,
  rc=1. The `Available:` hint correctly merges built-ins and `--scripts-dir` and
  no longer says "built-in".
- **ctl ↔ CLI command parity.** `ctl -i help` and CLI `help` render the same
  eight categories with the same aliases and usage strings; the only differences
  are the intended ones (`exit (q, quit)` as a local ctl command vs. an alias for
  `shutdown` in CLI mode; `history` hidden on both when stdin is a pipe). No
  command reachable from one client and not the other — the "no hidden APIs"
  rule holds. `debug` correctly goes to the daemon rather than being handled
  locally.
- **Exit codes.** `ctl <cmd>` rc mirrors `OK:`/`ERROR:`; `ctl run X wait` rc
  mirrors PASSED/FAILED; connection refused → rc=1 with a clear message; no args
  → help + rc=1; documented `--oneshot` 0/1 contract matches `cli.py:1130-1131`;
  SIGINT → 130 on both binaries.
- **Streaming / latency.** `run X wait` over one-shot ctl streams daemon `LOG:`
  lines to stderr while the script runs and keeps stdout to the single result
  line; the silence-based timeout is restarted by any received line and disabled
  entirely for wait-runs. Observed live during a 30s script.
- **Sanitization of operator-facing text.** Every terminal write on both binaries
  goes through `render_result` / `sanitize_text` / `_SanitizingFormatter`,
  including the `!!`/`!n` echo and script-derived names and descriptions.
- **Tab completion and highlighting.** Argument-position awareness
  (`consumed_args` indexing), path-shaped prefixes suppressed under ctl's policy
  (`run ./` → `[]`), day presets, bool toggles, timezone completer, subcommand
  recursion. No stale or misleading suggestion found other than L1/L3.
- **`--scripts-dir` validation.** Nonexistent → `parser.error` at startup;
  existing but empty → warning + `(none)` in both listings.
- **Mode-scoped flag rejection.** `--loop` / `--script-delay` / `--oneshot` /
  `--wait-for-client` without `--script`, and `--control-host` without
  `--daemon`, all rejected with mode-appropriate wording.
- **Script queue lifecycle under mutation.** Rewriting a queued script's YAML
  after `run` but before it starts does not change what runs — the consumer
  loads at claim time — so a queued run is reproducible.
- **`schedule` command family.** Full subcommand set including `clear` (the bulk
  action the persona asks for), consistent `#index` phrasing, `_get_schedule`
  error path, implicit-schedule display when none configured.
- **Settings / buttons / notifications / door commands.** Consistent
  toggle-or-set semantics, consistent "set" vs "show" phrasing (`AC set to
  connected` vs `Battery: 100%`), shared `_toggle_bool` helper, broadcast
  guarded by `_require_clients`.
- **Prompt behaviour.** Connect/disconnect invalidates the CLI prompt colour
  immediately; ctl colours from the structured `STATUS: clients=N` line rather
  than scraping logs; the ANSI erase in `clear_line`/`clear` is TTY-gated.
- **Backpressure and resource use.** `_ControlLogHandler` drops records for a
  client above `MAX_CLIENT_BACKLOG`, reaps closing writers, and refuses
  re-entry; `ScriptQueue` holds only refs and names.
- **`docs/simulator.md`.** Spot-checked ~25 concrete claims against live output
  (option table, script table, `stop`/`stop all` semantics, exit codes, empty
  `--scripts-dir` header, ctl completer limitation, scripts-dir precedence) —
  all accurate. The front-end doc is in good shape; the doc findings above are
  all in `client.md` / `protocol.md` / `door.md`.

---

## Cleanup

All simulator processes I started (ports 3900/3901, 3910, 3911, 3920/3921) were
shut down and verified gone; the scratch script directories under `/tmp` were
removed. `git status` is clean — no repo file was modified. (Unrelated
`ppd-simulator` processes on ports 14100/14104 belong to a concurrently running
agent and were left alone.)
