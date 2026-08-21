# Backend Developer Analysis — Round 2

Scope: `src/powerpetdoor/{client.py, door.py, const.py, schedule.py, tz_utils.py, framing.py}`
and `src/powerpetdoor/simulator/{server.py, protocol.py, state.py, scripting.py, engine.py}`
at commit 3f96bb8. The interactive CLI (`cli.py`, `ctl.py`, `prompt_common.py`, `commands/`)
is out of scope.

Findings marked **[verified at runtime]** were reproduced with throwaway scripts against the
in-repo code (scripts deleted afterwards; no repo files modified). Baseline health confirmed:
1684 tests pass, `ruff check` clean, `mypy` clean on the current tree.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 2 |
| Low      | 6 |
| Trivial  | 2 |
| **Total** | **10** |

The round-1 rework held up well. Every round-1 High/Critical area was re-derived from the
current code and none regressed (see verification section). The two Medium findings below are
*new* hazards exposed by the round-1 refactors (the engine's status-listener hooks, and the
now-idempotent lifecycle inviting repeated `connect()` calls); the Lows are edge-hardening
gaps, not behavioral regressions.

---

## Findings

### Medium

#### M1. Engine status listeners that command the door synchronously spawn a duplicate sequence runner **[verified at runtime]**

- **Files**: `src/powerpetdoor/simulator/engine.py:234-246` (`_start_sequence`),
  `engine.py:143-158` (`_set_status`), `engine.py:436-473` (`_run`)
- **Problem**: `_set_status` fires status listeners synchronously from inside the owner task
  `_run`. If a listener calls `engine.open()`/`engine.close()` (a natural thing for a script or
  test hook registered via the public `add_status_listener`/`DoorSimulator.add_status_listener`
  API), `_start_sequence` correctly skips cancelling the current task (`task is
  asyncio.current_task()`) — but then still calls `asyncio.create_task(self._run())`, creating
  a **second** concurrent runner while the first keeps executing its loop. Reproduced: a
  listener issuing `close()` on reaching HOLDING produced duplicate transitions
  (`CLOSING_MID_OPEN` twice, `CLOSED` twice) and **`total_open_cycles` incremented twice for
  one physical cycle**. Both runners then sleep/broadcast in parallel, so connected clients see
  doubled DOOR_STATUS broadcasts and stats are corrupted. Nothing in-repo currently re-enters
  (all in-repo listeners are `seen.append`-style), so this is a latent hazard of the new
  public hook API, not a currently-firing bug.
- **Recommendation**: In `_start_sequence`, when invoked from within the owner task, defer the
  restart out of the listener call stack — e.g.
  `asyncio.get_running_loop().call_soon(self._restart, start_state, hold)` where `_restart`
  performs the cancel/replace with the normal outside-owner path — or document loudly that
  status listeners must not command the door synchronously and guard with an explicit error.
  Add a test with a re-entrant listener asserting single-run transitions and a single
  `total_open_cycles` increment.

#### M2. `connect()` while already connected opens a second device connection and permanently leaks the first **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1020-1044` (`connect` has no already-connected
  guard), `client.py:1046-1066` (`connection_made` overwrites `_transport` and `_keepalive`),
  `src/powerpetdoor/door.py:437-518` (`door.connect()` likewise unguarded)
- **Problem**: Neither `PowerPetDoorClient.connect()` nor `PowerPetDoor.connect()` checks
  whether a connection already exists. A second `connect()` (e.g. an integration retrying
  setup, or an app calling `connect()` defensively) establishes a second TCP connection;
  `connection_made` then overwrites `self._transport` and `self._keepalive`, orphaning the
  first transport and its keepalive task. Reproduced against the simulator: after a second
  `door.connect()` the simulator holds 2 connections, the old keepalive task is still alive
  but unreferenced, and after `door.disconnect()` **one connection remains open forever**
  (the leaked transport is never closed). Against the real door — a single-connection device —
  the leaked socket would hog the device's only slot, locking everyone out until the device
  drops it. This matters more now than in round 1 precisely because the reworked lifecycle
  advertises re-entrant use ("May be called again after disconnect()").
- **Recommendation**: In `client.connect()`, no-op (log a warning) when
  `self._transport is not None`; in `connection_made`, close the incoming transport if one is
  already active (belt-and-braces). In `door.connect()`, return early (or raise) when
  `self.connected` is already true. Add tests for double-`connect()` at both layers asserting
  exactly one live connection and no orphaned keepalive task.

### Low

#### L1. Unhashable `msgID` in a response kills the receive task, contradicting the untrusted-input contract **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1374-1379` (`process_message`), `client.py:1338`
  (fire-and-forget task creation)
- **Problem**: `process_message`'s docstring (and the D3 decision) promises all network data is
  handled defensively, but `self._outstanding.get(reply_msg_id)` sits *outside* every
  try/except. A message with a non-hashable `msgID` (e.g. `"msgID": [1,2]` or a dict) raises
  `TypeError` there, killing the task. Reproduced: the exception surfaces only as a late
  "Task exception was never retrieved" at GC time. No hang results (the paired future is later
  failed by the `check_receipt` timeout), but the hardened receive path has a hole and the
  failure is near-invisible.
- **Recommendation**: Guard the envelope read: only treat `reply_msg_id` as a key when
  `isinstance(reply_msg_id, (int, str))`, else log-and-ignore (matching the other malformed-
  envelope paths). One-line fix plus a malformed-msgID test.

#### L2. A single non-ASCII byte mid-frame wedges the client receive path until the 64 KiB overflow disconnect **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1323-1327` (`data_received` decode),
  `src/powerpetdoor/simulator/protocol.py:284-289` (same pattern)
- **Problem**: On `UnicodeDecodeError` the entire received chunk is discarded. If that chunk
  contained the *tail of a partially-buffered frame*, the head of `self._buffer` is left as a
  permanently incomplete JSON object — `extract_frames` then never yields another frame, so
  **every subsequent valid message goes unprocessed** while the buffer grows. Reproduced:
  after a split frame whose second half contained one `0xff` byte, three subsequent valid
  DOOR_STATUS frames were never delivered. Recovery only happens when the buffer crosses
  64 KiB (overflow → disconnect → reconnect), which with typical ~70-byte messages is
  hundreds of messages / potentially minutes of blackout. Same pattern in the simulator's
  `data_received` (there the connection simply wedges until the client gives up on pings).
- **Recommendation**: Don't discard the chunk. Decode with
  `data.decode("ascii", errors="backslashreplace")` (log a warning when replacement occurred):
  framing stays synchronized and the corrupt frame fails `json.loads` and is skipped
  individually. Apply to both client and simulator.

#### L3. Client-side fire-and-forget tasks are untracked — exceptions surface only at GC, and shutdown can't await them

- **Files**: `src/powerpetdoor/client.py:1338` (`process_message` tasks), `client.py:1060,
  1242` (dequeue kicks), `client.py:964` (`_dispatch_handler` scheduling async lifecycle
  handlers, e.g. the door's refresh-on-reconnect)
- **Problem**: The simulator side got proper task tracking in round 1
  (`protocol.py:313-323` — tracked set + a done-callback that logs failures immediately), but
  the client still uses bare `ensure_future` for per-message processing, queue kicks, and
  async lifecycle handlers. Consequences: (a) an escaping exception (see L1) is reported only
  whenever the GC collects the task, with no immediate log; (b) `disconnect()`/`shutdown()`
  cannot cancel or await in-flight processing, so loop teardown in embedding apps can emit
  "Task was destroyed but it is pending" warnings. This is asymmetric with the project's own
  simulator pattern.
- **Recommendation**: Mirror the simulator: track these tasks in a `set` with a done-callback
  that logs non-cancelled exceptions immediately; cancel/await the set in `disconnect()` (or
  at least in `shutdown()`).

#### L4. Re-activating an already-active sensor lets the stale deactivation timer cut the new duration short

- **Files**: `src/powerpetdoor/simulator/engine.py:317-384` (`activate_sensor`,
  `_deactivate_sensor_after`)
- **Problem**: `activate_sensor("inside", 5)` followed 3 s later by another
  `activate_sensor("inside", 5)` leaves two `_deactivate_sensor_after` tasks; the first fires
  at t=5 and deactivates the sensor even though the second activation should keep it active
  until t=8. (Cross-sensor overlap is safe — the mutual-exclusion clear makes the stale timer
  a no-op — it is only same-sensor re-activation that is cut short.)
- **Recommendation**: Keep a per-sensor handle to the pending deactivation task and cancel it
  on re-activation (or have the timer capture a generation counter and no-op when stale).

#### L5. `door.refresh()` / `refresh_settings()` swallow per-command failures without a trace at the door layer

- **Files**: `src/powerpetdoor/door.py:1051-1064` (`refresh`), `door.py:1088-1098`
  (`refresh_settings`)
- **Problem**: Both gather with `return_exceptions=True` and discard the results. A device
  NAK (`CommandError`) or drop during initial `connect()` leaves the affected cached
  properties at their constructor defaults while `connect()` returns success; the only
  breadcrumbs are low-level client logs, and a caller has no way to know the cache is partial
  (`refresh()` returns `None` regardless). This is a debuggability gap rather than a
  correctness bug — the client does log the underlying failure — but the door facade is the
  documented public surface.
- **Recommendation**: Iterate the gather results and `logger.warning` each exception with the
  refresh step's name; optionally return a per-step success mapping (or raise when *all*
  steps failed) so callers can detect a dead-on-arrival refresh.

#### L6. Script `set hold_time` rejects fractional values although hold_time is a float everywhere else

- **Files**: `src/powerpetdoor/simulator/scripting.py:500-501` (`_set_value`)
- **Problem**: `state.hold_time = int(value)` — `set hold_time 1.5` raises `ValueError`
  ("Unexpected error" script failure), yet `DoorSimulatorState.hold_time` is a `float`, the
  protocol carries centiseconds, and the simulator's own timing defaults are fractional.
- **Recommendation**: Use `float(value)`.

### Trivial

#### T1. `MIN_BLOCKED_RECHECK` docstrings claim it bounds staleness; it is actually a busy-loop floor

- **Files**: `src/powerpetdoor/simulator/engine.py:53-57, 486-493`
- The wait while blocked is `max(hold_time, MIN_BLOCKED_RECHECK)`, so the constant only
  prevents a zero-sleep spin when `hold_time` is ~0; an out-of-band state mutation can go
  unnoticed for up to `hold_time`, not 0.1 s. Behavior is fine (API mutations wake the loop
  explicitly); fix the two comments/docstrings to say "floor", not "bound".

#### T2. `SET_SCHEDULE` failure envelope omits `reason`

- **Files**: `src/powerpetdoor/simulator/protocol.py:534-535`
- The missing/empty-schedule error path sets `success: "false"` without a `FIELD_REASON`,
  unlike every other simulator error path (and protocol.md's error envelope). Clients get
  `CommandError("SET_SCHEDULE", None)`. Add `response[FIELD_REASON] = "Missing schedule"`.

---

## Round 1 Fix Verification

Spot-checked every High/Critical and the major Medium fixes against the current code (and,
where marked, at runtime). All hold:

- **C1/D5 (loop resolution)**: `_get_loop()` latches `asyncio.get_running_loop()` lazily
  (`client.py:365-373`); `start()` only creates a private loop when none is running
  (`client.py:974-979`). No eager `new_event_loop()` in `__init__`.
- **H1 (bare excepts)**: zero `except:` remain in `src/` (grep-verified). `connect()` catches
  `(OSError, TimeoutError)` only (`client.py:1037`), so `CancelledError` propagates.
- **H2/D6 (reconnect lifecycle)**: reconnect task is tracked (`_reconnect_task`), cancelled in
  `disconnect()` with a correct self-cancellation guard (`client.py:1131-1139`), and both
  `connect()` and `reconnect()` guard on `_shutdown` (`client.py:1027, 1096`). Public
  `shutdown()`/`reset_shutdown()` exist; `door.py` no longer pokes `_shutdown`.
- **H3/D1 (framing)**: single shared scanner (`framing.py`) used by both sides
  (`client.py:1330`, `simulator/protocol.py:294`); string-aware, whitespace-tolerant,
  resyncing, 64 KiB-capped, never raises. Overflow drops the connection on both sides.
  (Edge remaining: L2 above — the *decode* step in front of the scanner.)
- **H4 (dropped-message futures)**: `_inflight_msg_id` is tracked (`client.py:1306`) and
  `check_receipt`'s drop path fails the future with `TimeoutError`
  (`client.py:1217-1221`).
- **M1 (KeyError in done-callbacks)**: cleanup uses `pop(msgId, None)` and `disconnect()`
  fails-then-`clear()`s the same dict instead of rebinding (`client.py:1458-1459,
  1144-1149`).
- **M2/D4 (listener arity)**: annotations and docstrings now declare `(field, value)`
  throughout; door callbacks take `(field_name, value)` with no `*args` shims
  (`door.py:1187-1214`).
- **M3/D3 (defensive parsing)**: envelope fields read via `.get()`, handler dispatch wrapped
  with typed `CommandError` failure, per-listener isolation in `_notify_listeners`
  (`client.py:1395-1419, 615-625`). (Edge remaining: L1 above — msgID hashability.)
- **M4/D2 (notifications)**: simulator emits the bare envelope
  (`protocol.py:154-185`, `server.py:309-331`); client parses bare envelopes
  (`_dispatch_notification_event`) and tolerates CMD-style variants, with a
  `notification_event` listener API.
- **M5**: unknown commands answered `success: "false"` + `reason: "Unknown command"`
  (`protocol.py:395-401`), including non-string command values.
- **M8/M12 (single engine)**: one `DoorMotionEngine` drives both the protocol and no-client
  paths (`protocol.py:239-243`, `server.py:127-131`); sequences chain by loop continuation —
  no self-cancel/await anywhere. (New hazard in the listener hooks: M1 above.)
- **M9 (simulator timezone)**: `get_tzinfo()` resolves IANA or wire-POSIX via
  `find_iana_for_posix`, warns once per value on UTC fallback (`state.py:387-416`).
- **M10 (door connect/refresh)**: event-driven connect with timeout → `ConnectionError`
  including full client teardown (`door.py:499-516`); reconnects trigger `refresh()` via
  `_on_connect` when initialized (`door.py:1263-1273`).
- **M11 (late-response race)**: handler runs *before* the dequeue await; dequeue deferred to a
  `finally` (`client.py:1381-1422`); all future completions guarded by `done()` checks.
- **L1-L4, L9-L14, L16-L20, T2, T6, T7 spot-checks**: backoff+jitter with attempt reset on
  success; `on_disconnect` only after a real connection; queue flush in `connection_made`;
  `available` returns `bool`; `send_message` overloads; typed `CommandError`/`ConnectionError`
  instead of `cancel()`; monotonic clock for latency/rate-limit; tz cache lock + copies;
  `compute_schedule_diff` deep-copies; simulator task tracking (`protocol._tasks`,
  `engine._aux_tasks`/`_retired`, `server.stop()` awaits everything);
  `DoorStatus.UNKNOWN` with warning; angle-bracket POSIX abbreviations parse; `async-timeout`
  dependency gone (stdlib `asyncio.timeout`); plain `heapq` with documented loop-thread-only
  policy; deadline-based hold loop (no 10 Hz poll); `_require_complete_entry` validation
  with clear errors. All verified in the current source.
- **Baseline**: `uv run pytest` → 1684 passed; `ruff check src tests` clean; `mypy src`
  clean, on this tree.

## Areas Reviewed With No Findings

- **Client send pipeline**: single-in-flight invariant holds across `enqueue_data` /
  `dequeue_data` / `_send_data` / `check_receipt` / `process_message` — the
  `_can_dequeue`/`_check_receipt` gating admits exactly one dequeue chain; the post-sleep
  transport re-check (M7 fix) is correct; retry bookkeeping (`_failed_msg`, `MAX_FAILED_MSG`)
  and PING/PONG receipt matching (`_last_command = PONG`) are consistent with the simulator's
  reply envelope.
- **Reconnect backoff math**: exponent clamp (`min(attempts, 16)`) prevents float blowup;
  delay cap and jitter fraction are applied correctly; attempts reset on successful
  `connection_made`.
- **framing.py**: `find_frame_end` escape/string state machine is correct (checked against
  escaped quotes, braces in strings, split frames); `extract_frames` resync and overflow
  semantics are sound; buffer is bounded.
- **schedule.py**: compression (swap of inverted ranges, `>=` adjacent merge, day collapse,
  inside/outside merge) and diff (index reuse, lowest-unused-index assignment, no input
  mutation) re-verified on the current code; `_require_complete_entry` closes round-1 T7.
- **tz_utils.py**: lock-guarded one-time init, copies returned, TZif footer extraction, and
  the extended POSIX regex are all sound; no blocking I/O outside the executor paths.
- **door.py caching**: settings coercion via `make_bool` with None-preserving semantics;
  inverted `cmd_lockout` handling consistent in both the settings path and the sensor-update
  path; schedule cache update/delete/sort logic correct; `delete_schedule`'s local fallback
  when the device doesn't echo the index is idempotent with the listener path.
- **Simulator state machine semantics**: reversal mappings preserve position
  (RISING↔CLOSING_MID_OPEN, SLOWING↔CLOSING_TOP_OPEN); already-open/already-closing guards;
  auto-retract counts and hold-extension windows; `is_sensor_blocking_close` interaction with
  safety lock/cmd_lockout; power-off closing the door.
- **Simulator server**: broadcast helpers all use `const.py` names (round-1 L21 fixed);
  battery simulation carry/threshold-crossing logic correct including cap/floor remainder
  reset; `stop()` ordering (battery task → engine → protocols → server) is clean.
- **scripting.py**: step parsing, deterministic `wait_for_status` usage with stop-event
  interruption and proper waiter cleanup, condition/assert normalization — all sound;
  `ScriptRunner.run`'s `step` binding cannot be unbound where referenced.
- **const.py**: `COMMAND_PRIORITIES` still covers every command the client sends; no drift
  against `docs/protocol.md` command names.
- **Memory bounds**: `_outstanding` (self-cleaning via done-callbacks), heapq queue (cleared
  on disconnect), framing buffers (64 KiB cap), tz caches (bounded), engine waiter/listener
  lists (removed in `finally`/unsubscribe) — no unbounded growth found. The only unbounded
  structure is the simulator's `state.schedules` dict under hostile SET_SCHEDULE indices,
  which is acceptable for a test tool (noted for the security persona rather than as a
  backend finding).
