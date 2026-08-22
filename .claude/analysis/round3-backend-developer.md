# Backend Developer Analysis — Round 3

Scope: `src/powerpetdoor/{client.py, door.py, const.py, schedule.py, tz_utils.py, framing.py}`
and `src/powerpetdoor/simulator/{server.py, protocol.py, state.py, scripting.py, engine.py}`
at commit `3478a5b`. The interactive CLI (`cli.py`, `ctl.py`, `prompt_common.py`,
`commands/`) is out of scope.

Findings marked **[verified at runtime]** were reproduced with throwaway scripts run
against the in-repo source (scripts deleted afterwards; no repo file was modified).
Baseline health on this tree: `uv run pytest` → **1759 passed**.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 1 |
| Low      | 5 |
| Trivial  | 2 |
| **Total** | **8** |

The round-2 fixes are structurally sound: the engine's dispatch-depth bracketing does
prevent duplicate `_run` tasks, `_connecting` is cleared on every path, the task-tracking
sets never leak and never cancel the calling task, and `Schedule.from_dict` accepts every
payload the library itself emits. The findings below are edge cases *around* those fixes,
not regressions of them — with one exception (L2), where the new belt-and-braces guard in
`connection_made` is actively harmful if it ever fires.

The one Medium is a pre-existing lifecycle hole that the round-2 `connect()` work made
easier to reach in reasoning about, but did not introduce: `connection_made()` still does
not consult `_shutdown`.

---

## Findings

### Medium

#### M1. `shutdown()`/`stop()` during an in-flight `connect()` leaves a live, keepalive-pinging connection that nothing ever closes **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1096-1122` (`connection_made`),
  `client.py:1041-1050` (`shutdown`), `client.py:1025-1039` (`stop`),
  `client.py:1056-1094` (`connect`)
- **Problem**: `connect()` checks `self._shutdown` only at entry. `connection_made()`
  checks nothing. So if `shutdown()` (or the thread-safe `stop()`) is called while
  `create_connection()` is still in flight, the connect completes afterwards, and
  `connection_made()` unconditionally installs the transport, starts the keepalive task,
  flushes the queue and fires `on_connect` handlers. The client is now fully connected
  with `_shutdown == True` and no one holding a reference to tear it down —
  `disconnect()` already ran *before* the transport existed.

  Reproduced against a local TCP server: after
  `t = ensure_future(c.connect()); await sleep(0); c.shutdown(); await t`, the client
  reports `_shutdown = True`, `available = True`, an active keepalive task, and the
  server sees **one live connection**. Left running, the log fills with
  `Last PING not responded to 1 of 3...` — i.e. a "shut down" client is actively
  pinging the device. Against a real, responsive door the socket never closes at all
  (the ping succeeds, so the 3-strike disconnect never triggers); with
  `keepalive = 0` it never closes under any circumstances. On a
  single-connection device this permanently occupies the only slot.

  Reachable from documented API use: `client.shutdown()` is documented as
  "safe to call before connect()", `stop()` is the documented cross-thread teardown
  (an embedding app shutting down during startup hits this directly), and
  `door.connect(timeout=T)` with `T` shorter than the client's `cfg_timeout` calls
  `self._client.shutdown()` from its timeout path (`door.py:515-523`) while the
  client's own `create_connection` is still running.
- **Recommendation**: In `connection_made()`, before installing anything:
  `if self._shutdown: _LOGGER.info(...); transport.abort(); return` — but see L2, the
  rejection must not route `connection_lost` back into the live-connection teardown
  path (there is no live connection in this case, so a plain guarded return plus
  `transport.abort()` is safe *once* L2 is fixed; today the two interact). Cleanest
  combined fix: give each `connect()` attempt its own protocol shim (or an attempt
  generation counter) so a transport the client has decided not to adopt cannot drive
  `connection_lost`. Add tests for `shutdown()`-during-`connect()` at both the client
  and door layers asserting `available is False` and zero live server-side connections.

### Low

#### L1. `connect()` escapes with `UnicodeEncodeError`/`OverflowError` on an invalid host or port — no reconnect, no log, contract violated **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1085` (`except (OSError, TimeoutError)`),
  `client.py:1063-1066` (docstring: "this method only raises asyncio.CancelledError"),
  `client.py:1017` / `client.py:1147` (untracked `ensure_future` of `connect`/`reconnect`)
- **Problem**: `loop.create_connection()` does not raise `OSError` for every bad input.
  Verified: host `"a"*64 + ".example"` → `UnicodeEncodeError: 'idna' codec ... label too
  long`; host with a lone surrogate → `UnicodeEncodeError`; port `99999` →
  `OverflowError: connect(): port must be 0-65535.`. None of these are caught, so
  `handle_connect_failure()` never runs: **no error is logged, no reconnect is scheduled,
  and the client is dead forever**. In the `start()` and `reconnect()` paths the task
  simply dies and the traceback only surfaces at GC as "Task exception was never
  retrieved" (those two `ensure_future` calls are the last untracked ones left after the
  round-2 L3 work). At the door layer, `door.connect()` propagates the raw
  `OverflowError`/`UnicodeEncodeError` instead of the documented `ConnectionError`, and
  leaves `_door_facade` listeners/handlers registered and `reset_shutdown()` applied.
- **Recommendation**: Broaden the catch to `except (OSError, TimeoutError, ValueError)`
  (`UnicodeEncodeError` and `OverflowError` are both `ValueError` subclasses) — or catch
  `Exception` and re-raise `CancelledError` — so every connect failure funnels through
  `handle_connect_failure()`. Route the `start()`/`_schedule_reconnect()` tasks through
  `_track_task` so an escaping exception is logged immediately. Tests: invalid host label
  and out-of-range port, asserting a reconnect is scheduled and `door.connect()` raises
  `ConnectionError`.

#### L2. The round-2 "reject a second connection" guard tears down the *healthy* connection when it fires **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1098-1103` (`connection_made`),
  `client.py:1124-1129` (`connection_lost`)
- **Problem**: The client *is* the `asyncio.Protocol` (`create_connection(lambda: self,
  ...)`), so one protocol object is shared by every transport it is attached to. When
  `connection_made()` rejects an intruding transport with `transport.close()`, asyncio
  delivers `connection_lost(None)` **on the same object** a tick later — which calls
  `self.disconnect()`, closing the live transport, failing all outstanding futures,
  firing `on_disconnect`, and scheduling a reconnect. Reproduced: with a healthy
  connection established, a second `create_connection(lambda: client, ...)` produced
  `WARNING Rejecting a second connection` immediately followed by
  `ERROR The server closed the connection. Reconnecting...` and left
  `available = False`, `_transport = None`.

  Not reachable through the library's own APIs at HEAD (`connect()`'s `_transport` /
  `_connecting` guards close the door on it), so this is a latent trap rather than a
  live bug — but `PowerPetDoorClient` is a public `asyncio.Protocol` subclass and the
  guard exists precisely for the case someone wires it into `create_connection`
  themselves. As written, the "belt and braces" makes that case strictly worse than no
  guard at all, and it also blocks the clean fix for M1.
- **Recommendation**: Do not let a transport the client declined drive
  `connection_lost`. Either (a) keep a small `set` of rejected transports and have
  `connection_lost` return early while `self._transport is None and rejected` is
  outstanding, (b) track an attempt generation and ignore lost-events from stale
  generations, or (c) use a one-shot throwaway `asyncio.Protocol` per connect attempt
  that hands the transport to the client only if it is still wanted. Add a test that a
  rejected second transport leaves the first connection `available` and fires no
  `on_disconnect`.

#### L3. The engine's deferred sequence replays a *stale* `start_state` when two status transitions happen without an intervening await **[verified at runtime]**

- **Files**: `src/powerpetdoor/simulator/engine.py:259-299` (`_start_sequence`,
  `_start_pending_sequence`, `_replace_sequence`), `engine.py:546-570` (`_hold_open`)
- **Problem**: `_start_sequence()` captures the resolved `start_state` at *request* time
  and applies it later from `call_soon`. That is safe as long as the owner task suspends
  after each `_set_status`, which it does — except in `_hold_open()`: when
  `state.hold_time` is ~0 the deadline has already passed on the first iteration, so
  `_hold_open` returns **without awaiting**, and `_run` performs a second `_set_status`
  in the same synchronous block. A listener that commanded the door from the first
  transition then has its stale state applied on top of the second.

  Reproduced with a listener that calls `close()` on `DOOR_HOLDING`:
  - `hold_time = 0.2` → `RISING, SLOWING, HOLDING, CLOSING_TOP_OPEN, CLOSING_MID_OPEN, CLOSED` (correct)
  - `hold_time = 0.0` → `RISING, SLOWING, HOLDING, CLOSING_TOP_OPEN, **CLOSING_TOP_OPEN**, CLOSING_MID_OPEN, CLOSED`

  Effect: a duplicate `DOOR_STATUS` broadcast to every connected client, a duplicate
  status-listener callback for a status that never changed, and the closing-top phase
  timer restarted from scratch. `total_open_cycles` stays correct (1) and no duplicate
  `_run` task is created, so the round-2 M1 fix itself holds.
- **Recommendation**: Defer the *intent*, not the resolved state: record
  `("open", hold)` / `("close", None)` and have `_start_pending_sequence()` re-invoke
  `open()`/`close()`, which re-derive `start_state` from the *current* status and
  correctly no-op when the door already got there ("already closing"). Add a
  `hold_time = 0` re-entrant-listener test asserting no repeated status value.

#### L4. `daysOfWeek` string elements are read truthily, so `"0"` enables the day

- **Files**: `src/powerpetdoor/simulator/state.py:72-86` (`_coerce_schedule_days`),
  `src/powerpetdoor/door.py:288-295` (`Schedule.from_dict`),
  `src/powerpetdoor/schedule.py:232-239` (`compress_schedule` expand step)
- **Problem**: All three sites use plain truthiness (`bool(day)` / `if
  sched[FIELD_DAYSOFWEEK][day]`). Verified: `daysOfWeek: ["1","0","1","0","1","0","1"]`
  parses to all seven days active in the simulator's `Schedule.from_dict`. This is
  exactly the `bool("0") is True` trap the codebase guards against everywhere else —
  `make_bool()` exists for it and `door._on_settings` carries an explicit comment about
  it — and the schedule object already carries `enabled` as a `"0"`/`"1"` **string** on
  the wire, which makes a string-valued `daysOfWeek` a plausible firmware variant.
  `docs/protocol.md` documents ints, so this is hardening rather than a live bug, but the
  failure mode (an access-control schedule silently becoming 7-days-a-week) is the worst
  possible direction to fail in.
- **Recommendation**: Run each element through `make_bool()`-equivalent coercion (or
  reject non-`(int, bool)` elements outright, matching the documented format) in
  `_coerce_schedule_days`; mirror the same coercion in `door.Schedule.from_dict` and in
  `compress_schedule`'s expand step. One test per site with `["1","0",...]`.

#### L5. `Schedule.from_dict` fabricates a 06:00–22:00 window for a missing time object instead of rejecting it

- **Files**: `src/powerpetdoor/simulator/state.py:89-100` (`_coerce_schedule_time`),
  `state.py:253-259` (`from_dict`), `src/powerpetdoor/simulator/protocol.py:528-546`
  (`_handle_set_schedule`)
- **Problem**: The new validator hardens *malformed* values (`hour: 24`, `min: 60`,
  `index: 300` are all correctly rejected — verified) but *invents* missing required
  ones: `{"index": 0, "inside": true, "daysOfWeek": [1]*7}` with no `in_start_time` /
  `in_end_time` is accepted and stored as a 06:00–22:00 window, as is
  `in_start_time: {}`. The simulator then echoes `schedule.to_dict()` back in the
  `SET_SCHEDULE` response, so the client's `_handle_schedule` caches a schedule the
  caller never sent. For an entry whose whole purpose is to gate sensor access, silently
  materializing a 16-hour permissive window out of an absent field is the wrong default.
  The library's own `door.Schedule.to_dict()` always emits all four time objects, so this
  only affects third-party/hand-rolled payloads.
- **Recommendation**: When `inside` (or `outside`) is true, require the corresponding
  `*_start_time`/`*_end_time` objects and raise `ValueError` when absent, the same way a
  bad hour is handled; keep the `default_hour` fallback only for the
  neither-sensor-selected case. Add a `SET_SCHEDULE` test asserting the error envelope
  for `inside: true` with no times.

### Trivial

#### T1. A stale `connection_lost` after `disconnect()`+`connect()` logs a misleading ERROR and burns a reconnect task **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1124-1129` (`connection_lost`),
  `client.py:1206-1208` (`disconnect` clears `_transport` before `connection_lost` runs)
- `disconnect()` clears `self._transport` immediately, but asyncio delivers
  `connection_lost` for that socket on a later loop iteration. If the caller reconnects
  in the meantime (`door.disconnect()` then `door.connect()` — which calls
  `reset_shutdown()`, so the `_shutdown` guard no longer suppresses it), the stale
  callback logs `ERROR The server closed the connection. Reconnecting...` for a
  connection nobody lost and schedules a reconnect that later no-ops with
  `WARNING Ignoring connect(): already connected`. Reproduced end to end; the connection
  ends up healthy and `_reconnect_attempts` is reset by the successful
  `connection_made`, so the damage is purely two misleading log lines (one at ERROR) per
  reconnect cycle plus a wasted task. Fix by having `connection_lost` ignore the event
  when it does not correspond to the current transport (the same generation/identity
  tracking L2 needs).

#### T2. Async lifecycle-handler tasks have no cancellation path at all

- **Files**: `src/powerpetdoor/client.py:388-413` (`_track_task`, `_handler_tasks`),
  `client.py:1198-1204` (`disconnect` cancels only `_tasks`)
- `_handler_tasks` is deliberately excluded from `disconnect()`'s cancellation so an
  `on_disconnect` coroutine survives the teardown that triggered it — correct. But
  nothing else ever cancels or awaits that set either, including `shutdown()`. A
  long-running or wedged async `on_connect`/`on_disconnect` handler outlives the client
  with no API to stop it, and the set grows by one per reconnect while it does. Consider
  a `shutdown()`-time (or explicit `aclose()`) step that awaits `_handler_tasks` with a
  bounded timeout and then cancels the remainder, so embedding apps have a clean
  teardown point.

---

## Round 2 Fix Verification

All ten round-2 findings were re-derived from the current source; each fix is present and
behaves as intended. Specific verification of the four areas called out for scrutiny:

- **M1 — engine dispatch-depth bracketing.** `_dispatch_depth` is incremented/decremented
  in a `try/finally` around both the broadcast callback and the status listeners
  (`engine.py:167-180`), so it cannot leak even if a callback raises. `_start_sequence`
  defers to a single coalesced `call_soon` (`engine.py:269-276`) and `_replace_sequence`
  is only ever reached from outside the owner task. **Verified at runtime**: a listener
  issuing `close()` on `DOOR_HOLDING` now yields exactly one clean
  RISING→…→CLOSED sequence, `total_open_cycles == 1`, zero pending/retired tasks
  afterwards — the round-2 duplicate-runner and doubled-cycle-count corruption is gone.
  Ordering is correct in the nested case too: `_replace_sequence`'s own `_set_status` can
  itself schedule a deferral, and because `create_task` enqueues *after* the `call_soon`,
  the deferral wins (last-one-wins, as documented). `stop()`/`cancel_nowait()` both call
  `_cancel_deferred_restart()` first, so a pending restart cannot resurrect a stopped
  engine; `_restart_handle` is `None` afterwards and `_pending_sequence` is inert without
  it. Only gap found: the captured `start_state` can go stale (L3).
- **M2 — `connect()` guards.** `_connecting` is set synchronously before the first await
  and cleared in a `finally`, so it is cleared on the success, `OSError`/`TimeoutError`,
  `CancelledError` *and* unexpected-exception paths — verified for all of them, including
  the L1 exceptions. `_get_loop()` is called before `_connecting = True`, so a
  `RuntimeError` there cannot strand the flag. **No legitimate reconnect is blocked**:
  `connection_lost` → `disconnect()` clears `_transport` before `_schedule_reconnect()`,
  a `connect()` issued while a reconnect is merely *sleeping* proceeds normally, and a
  `connect()` issued while another is genuinely in flight correctly defers to it. The
  `disconnect()`→`connect()`-in-one-tick case produces log noise but still ends
  connected (T1). Two residual gaps: `connection_made` ignores `_shutdown` (M1) and its
  second-transport rejection is self-destructive (L2).
- **L3 — task-tracking sets.** `_on_task_done` discards from both sets unconditionally
  and guards `task.exception()` behind `not task.cancelled()`, so no set leaks and no
  `CancelledError` is re-raised out of the callback. `disconnect()` skips
  `asyncio.current_task()` when cancelling `_tasks`, and the `RuntimeError` guard around
  `current_task()` covers the no-loop case — verified by inspection of every reachable
  self-cancel path (`_send_data`'s write-failure `disconnect()`, `data_received`'s
  overflow `disconnect()`, a listener calling `disconnect()` from inside
  `process_message`). `_handler_tasks` correctly survives `disconnect()` so
  `door._on_connect`'s reconnect refresh completes; its only issue is that nothing
  cancels it either (T2). Futures cancelled mid-`process_message` are still completed —
  `disconnect()` cancels tasks first and fails the remaining `_outstanding` futures with
  `ConnectionError` afterwards, so no caller is left hanging.
- **New `Schedule` validation.** It accepts **everything the library itself emits**:
  `door.Schedule.to_dict()` round-trips through `simulator.Schedule.from_dict()` with
  times, days, index and `enabled` intact, and `simulator.Schedule` survives a
  `to_dict()`→`from_dict()` round trip by equality. The documented canonical payload from
  `docs/protocol.md` parses unchanged, as do string hours/minutes, float hours, bool day
  lists, `enabled` as `"1"`/`"0"`, and outside-only entries. Rejections are all genuinely
  out-of-range (`hour 24`, `min 60`, `index 300`) and match the documented 0-23 / 0-59 /
  0-255 contract, and `docs/protocol.md:437-444` documents the new rejection behavior.
  Two coercion gaps, not rejection gaps: L4 (string day elements) and L5 (missing time
  objects defaulted rather than rejected).

The remaining round-2 fixes were spot-checked and hold: non-`(int|str)` `msgID` logs a
warning and resolves no future (`client.py:1454-1462`); per-frame lenient decoding with
`errors="backslashreplace"` on both sides (`client.py:1397-1401`,
`protocol.py:289-291`) keeps framing synchronized and correctly detects escaping via the
length comparison; per-sensor deactivation timers cancel on re-activation
(`engine.py:431-441`); `_log_refresh_failures` reports each failed step by name
(`door.py:1059-1070`) — observed firing correctly in a live probe; `set hold_time` uses
`float(value)` (`scripting.py:542`); `SET_SCHEDULE`'s missing-schedule path now sets
`FIELD_REASON` (`protocol.py:533`); the `MIN_BLOCKED_RECHECK` docstrings now say "floor"
(`engine.py:53-60`); and `door.connect()` is idempotent (`door.py:458-460`).

## Areas Reviewed With No Findings

- **framing.py** — `find_frame_end`'s escape/string state machine, `extract_frames`
  resync/overflow semantics and the 64 KiB cap are all correct and total (never raises).
  Both callers handle `diag.overflow` by dropping the connection. Re-verified against
  split frames, braces inside strings, escaped quotes and pure-garbage input.
- **Client send pipeline** — the single-in-flight invariant across `enqueue_data` /
  `dequeue_data` / `_send_data` / `check_receipt` / `process_message` still holds with the
  tracked-task change. `check_receipt` nulls `self._check_receipt` *before* its awaits, so
  `disconnect()` can never self-cancel it; the post-sleep transport re-check, the
  `_failed_msg`/`MAX_FAILED_MSG` retry bookkeeping and PING/PONG receipt matching are
  consistent with the simulator's reply envelope.
- **Reconnect backoff math** — exponent clamp, delay cap, jitter fraction and
  attempt-reset-on-`connection_made` are all correct; `min(attempts, 16)` prevents float
  blowup.
- **schedule.py** — compression (inverted-range swap, `>=` adjacent merge, day collapse,
  inside/outside merge) and `compute_schedule_diff` (index reuse, lowest-unused-index
  assignment, deep copies, no input mutation) re-verified; `_require_complete_entry`
  gives clear, positional error messages.
- **tz_utils.py** — double-checked-locking init, copies returned from
  `get_available_timezones`, TZif footer extraction, and the angle-bracket-aware POSIX
  regex are sound; no blocking I/O outside `asyncio.to_thread`.
- **door.py caching** — settings coercion via `make_bool` with None-preserving semantics,
  the inverted `cmd_lockout` handling in both the settings and sensor-update paths, the
  schedule cache update/delete/sort logic, and `delete_schedule`'s idempotent local
  fallback are all correct.
- **Simulator state machine semantics** — position-preserving reversal mappings
  (RISING↔CLOSING_MID_OPEN, SLOWING↔CLOSING_TOP_OPEN), already-open/already-closing
  guards, auto-retract counting and hold-extension windows, `is_sensor_blocking_close`'s
  interaction with safety lock and `cmd_lockout`, and power-off closing the door.
- **Engine hold loop** — deadline-based waiting with `_wake` cleared inside
  `_wait_for_wake`, so a stale wake costs at most one extra iteration and can never
  busy-loop; negative/zero `hold_time` terminates immediately rather than spinning;
  `asyncio.Event()` is loop-agnostic on the supported 3.11+ matrix, so constructing an
  engine outside a running loop is safe.
- **Simulator server** — `stop()` ordering (battery task → engine → protocols → server),
  `_battery_tick` carry/threshold-crossing logic including the cap/floor remainder reset,
  and the `_owns_engine` split that keeps `protocol.aclose()` from stopping the shared
  server engine.
- **scripting.py** — step parsing, `_wait_for_status`'s waiter/stopper cleanup and
  stop-event interruption, condition/assert normalization.
- **const.py** — `COMMAND_PRIORITIES` still covers every command the client sends; no
  drift against `docs/protocol.md` command names.
- **Memory bounds** — `_outstanding` (self-cleaning done-callbacks), the heapq queue
  (cleared on disconnect), framing buffers (64 KiB cap), tz caches (bounded),
  `_tasks`/`_handler_tasks`/`_aux_tasks`/`_retired`/`_sensor_timers` (all discarded by
  done-callbacks), engine waiter/listener lists (removed in `finally`/unsubscribe). The
  only unbounded structure is `state.schedules`, now bounded to 256 slots by
  `MAX_SCHEDULE_INDEX`.
