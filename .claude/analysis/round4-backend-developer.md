# Backend Developer Analysis — Round 4

Scope: `src/powerpetdoor/{client.py, door.py, const.py, schedule.py, tz_utils.py,
framing.py, sanitize.py}` and `src/powerpetdoor/simulator/{server.py, protocol.py,
state.py, scripting.py, engine.py}` at commit `f9b9b59`. The interactive CLI
(`cli.py`, `ctl.py`, `prompt_common.py`, `commands/`) is out of scope.

Findings marked **[verified at runtime]** were reproduced with throwaway scripts run
against the in-repo source (scripts deleted afterwards; no repo file was modified).
Baseline health on this tree: `uv run pytest` → **1926 passed**.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 0 |
| Low      | 3 |
| Trivial  | 3 |
| **Total** | **6** |

All eight round-3 findings were re-derived from the current source. Six are fully
fixed and hold under runtime probing. Two are fixed **only for the path the fix
touched**, leaving the other half of the same defect in place:

- **L2** (a declined/stale transport tearing down the healthy connection) is fixed
  for the `_ConnectionAttempt` shim, but `PowerPetDoorClient.connection_lost` — the
  public `asyncio.Protocol` entry point the `_declined` counter exists to serve —
  still has no transport identity at all, so the *superseded*-transport half of the
  bug is intact there (**L1** below).
- **T2** (`aclose()`) does what it says on the happy path, but a cancellation of
  `aclose()` itself skips the cancel step entirely — the one guarantee the method
  was added to provide (**L2** below).

The connection-identity machinery is otherwise sound: I could not find a race between
decline and adopt, the `_declined` counter cannot drift into swallowing a real
disconnect, and the keepalive-failure reconnect (`_transport is None`) still works
because the shim deliberately forwards in that case. The wire-validation helpers
accept everything the library itself emits, and reject only what `docs/protocol.md`
now documents as rejected.

---

## Findings

### Low

#### L1. The public `asyncio.Protocol` path still lets a superseded transport tear down the healthy connection — round-3 L2 was fixed only for the shim **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1191-1203` (`PowerPetDoorClient.connection_lost`),
  `client.py:1138-1149` (`connection_made`), compare `client.py:1691-1703`
  (`_ConnectionAttempt.connection_lost`)
- **Problem**: `_ConnectionAttempt.connection_lost` performs two checks — "was this
  transport adopted?" and "is it still the client's current transport?". The client's
  own `connection_lost` performs only the first, via the `_declined` counter. It has
  no way to know *which* transport was lost (asyncio does not pass one), so a stale
  loss from a transport `disconnect()` already replaced is treated as the live
  connection dying.

  Reproduced side by side, identical event sequence, one through the shim and one
  through direct wiring:

  ```
  shim:   transport=B  available=True   reconnect=None    (DEBUG "superseded transport")
  direct: transport=None available=False reconnect=<Task pending>  B_closed=True
          (ERROR "The server closed the connection. Reconnecting...")
  ```

  So on the direct path the *healthy* transport B is closed, `on_disconnect` fires,
  outstanding futures are failed with `ConnectionError`, and a reconnect is burned —
  which is exactly the failure mode round-3 L2 described, just reached through
  "superseded" instead of "declined".

  Reachability: it needs two transports alive at once on a directly-wired client. That
  is not artificial — asyncio only defers `connection_lost` past the next loop
  iteration when the closed transport still has a non-empty write buffer
  (`_SelectorTransport.close()` skips the `call_soon` when `self._buffer` is
  non-empty), which is the ordinary state of a socket to a peer that has stopped
  reading. It is the same window the shim's guard exists to cover. Nothing in the
  library reaches it, but `PowerPetDoorClient` is a documented public
  `asyncio.Protocol` and the `_declined` bookkeeping (`client.py:296-300`) is there
  solely to make that use safe.
- **Recommendation**: Split the two callers. Give the shim a private
  `client._on_transport_lost(exc)` (today's body of `connection_lost`), and leave the
  public `connection_lost` to guard the direct path with the same "is a newer
  transport live?" test the shim uses — e.g. count transports the client adopted
  directly and closed itself, and return early while `self._transport is not None`
  (the `is not None` condition preserves the keepalive-failure reconnect, where
  `_transport` is already `None` and the event *must* be forwarded). Alternatively,
  document direct wiring as unsupported and drop `_declined` entirely. Add the
  mirror of `test_shim_ignores_a_superseded_transports_loss` for the direct path.

#### L2. `aclose()` cancelled mid-wait leaves the handlers running — the exact guarantee it was added to make **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1060-1084` (`aclose`),
  `src/powerpetdoor/door.py:531-543` (`PowerPetDoor.disconnect`)
- **Problem**: `aclose()` does `await asyncio.wait(tasks, timeout=...)` and only then
  cancels whatever is `pending`. If the caller's own task is cancelled while that
  `wait` is suspended, `asyncio.wait` re-raises `CancelledError` and the cancel loop
  and the trailing `gather` never run — so every outstanding lifecycle handler is
  left running, un-awaited and un-cancelled. Verified: with one async `on_disconnect`
  sleeping 5 s and `async with asyncio.timeout(0.1): await client.aclose(timeout=5.0)`,
  after the timeout fired `client._handler_tasks` still held **1 task, `done=False`**,
  and the handler recorded neither "finished" nor "cancelled".

  Reachable exactly the way an embedding application shuts down: `door.disconnect()`
  is `async` and awaits up to `default_timeout` (4 s at the library defaults, 20 s at
  the `PowerPetDoor` defaults), so wrapping it in an outer shutdown deadline — or
  being inside a task the host framework cancels during unload — is the normal case,
  not an exotic one. The docstrings on both `aclose()` ("whatever is still running is
  cancelled") and `door.disconnect()` ("nothing outlives this call (T2)") state a
  guarantee the code does not make.
- **Recommendation**: Move the cancel step into a `finally:` so it runs on the
  cancelled path too:
  ```python
  pending: set[asyncio.Task] = set(tasks)
  try:
      _done, pending = await asyncio.wait(tasks, timeout=...)
  finally:
      for task in pending:
          if not task.done():
              _LOGGER.warning("Cancelling connection handler %r ...", task.get_name())
              task.cancel()
  await asyncio.gather(*pending, return_exceptions=True)
  ```
  Add a test that cancels `aclose()` mid-wait and asserts every handler task ends up
  cancelled.

#### L3. `GET_SCHEDULE`/`DELETE_SCHEDULE` never validate the untrusted `index`; one packet yields an unhandled `TypeError`, a stack trace at ERROR, and a useless error reason **[verified at runtime]**

- **Files**: `src/powerpetdoor/simulator/protocol.py:601-608` (`_handle_get_schedule`),
  `protocol.py:630-640` (`_handle_delete_schedule`), `protocol.py:483-485`
  (the generic `except Exception` fallback)
- **Problem**: Round 3's hardening pass gave every `SET_*` field a `_coerce_wire_*`
  validator that rejects before mutating, and gave `Schedule.from_dict` a full
  validator — but the `index` field of the two index-addressed schedule commands is
  still used raw as a dict key. `index in self.state.schedules` raises `TypeError:
  unhashable type` for any JSON container. Verified with
  `{"config":"GET_SCHEDULE","index":[1,2]}` and
  `{"config":"DELETE_SCHEDULE","index":{"a":1}}`:

  ```
  ERROR Simulator: Error handling command GET_SCHEDULE
  Traceback (most recent call last):
    File ".../simulator/protocol.py", line 604, in _handle_get_schedule
      if index is not None and index in self.state.schedules:
  TypeError: unhashable type: 'list'
  RESP: {"CMD":"GET_SCHEDULE","success":"false","dir":"d2p","reason":"Command failed"}
  ```

  Two consequences, both of the kind `WireValueError` was introduced to eliminate:
  a legitimate client gets `"Command failed"` instead of a reason it can act on, and
  an unauthenticated peer can emit one full Python traceback per packet into the
  operator's log at ERROR — unbounded, and precisely the log-flooding sink the
  `sanitize_text` work was protecting. (Nothing is corrupted: the raise happens
  before any state change.) Secondary, milder: a float index (`0.0`) or `true`
  silently aliases integer key `0`/`1`.
- **Recommendation**: Run `index` through `_coerce_wire_int(msg[FIELD_INDEX], FIELD_INDEX,
  0, MAX_SCHEDULE_INDEX)` in both handlers (and reject `bool`, as
  `_coerce_wire_number` already does), so a bad index produces the standard
  `WireValueError` envelope with a real reason and a single `WARNING` line. Add a
  `SET_SCHEDULE`-style rejection test for `GET_SCHEDULE`/`DELETE_SCHEDULE` with a
  list, a dict, a float and an out-of-range index, and document the rule in
  `docs/protocol.md`'s validation table alongside the `SET_*` entries.

### Trivial

#### T1. The round-3 T1 guard does not fire in the ordering a real event loop produces; the bogus ERROR and wasted reconnect are still there **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:1697-1702` (`_ConnectionAttempt.connection_lost`),
  `tests/test_client.py:2678-2706` (`test_shim_ignores_a_superseded_transports_loss`)
- The `current is not None and current is not self._transport` guard only helps when
  the *new* transport is adopted before the old one's `connection_lost` is delivered.
  With a real socket the opposite happens: `transport.close()` schedules
  `_call_connection_lost` via `call_soon`, which runs on the very next loop iteration,
  long before a TCP `create_connection` completes. Reproduced against a local server —
  `client.disconnect()` immediately followed by `await client.connect()` still logs
  `ERROR The server closed the connection. Reconnecting...` and leaves a pending
  `reconnect()` task that later no-ops with `WARNING Ignoring connect(): already
  connected`. The regression test passes only because it drives
  `second.connection_made(...)` by hand *before* `first.connection_lost(None)`, an
  ordering the loop will not produce here.

  The guard is not dead code — it is exactly right for the delayed-delivery case (a
  closed transport with a non-empty write buffer, see L1) — it just does not cover
  the case T1 was written about. Damage is unchanged from round 3: two misleading log
  lines (one at ERROR) and one wasted task per cycle; `_reconnect_attempts` is reset
  correctly by the subsequent `connection_made`, and no duplicate connection results.
  Note that the `PowerPetDoor` facade is not affected — `aclose()` awaits the
  `on_disconnect` handler, which gives the loop the turn it needs to consume the
  stale loss while `_shutdown` is still `True`. Consider either accepting this
  explicitly in the docstring or suppressing the reconnect when
  `self._was_connected` is already `False` at `connection_lost` time (i.e. a
  `disconnect()` has already run), and fixing the test to assert the real ordering.

#### T2. `sanitize_text()` runs on every received frame even when DEBUG logging is off

- **Files**: `src/powerpetdoor/client.py:1482`, compare
  `src/powerpetdoor/simulator/protocol.py:361-362`
- `_LOGGER.debug("RX < %s", sanitize_text(decoded))` evaluates the full regex
  substitution and `str()` before the logger can discard the record. The simulator's
  identical line is guarded by `if logger.isEnabledFor(logging.DEBUG)`; the library
  side — the one that runs unattended for years inside Home Assistant — is not.
  Measured: 2.66 µs per 282-char settings frame (62.8 µs at 11 KB) against 0.12 µs for
  a suppressed `logger.debug` alone, i.e. ~22× the cost of the call it is feeding.
  Absolute cost is small at this traffic level, so this is consistency and hygiene
  rather than a real load problem. Wrap it in the same `isEnabledFor` guard the
  simulator uses (`client.py:990` is on a rare path and is fine as-is).

#### T3. `enabled` is still read with a bespoke `== "1"` while its sibling `daysOfWeek` flags now go through `make_bool` **[verified at runtime]**

- **Files**: `src/powerpetdoor/simulator/state.py:316-320`,
  `src/powerpetdoor/door.py:300-303`, compare `state.py:77-93`
  (`_coerce_schedule_day`) and `door.py:294-298`
- Round 3's L4 fix routes every `daysOfWeek` element through `make_bool` with the
  comment "read each flag the way every other wire flag is read" — but the flag
  sitting next to it in the same object still uses `enabled == "1"`. Verified: for
  `enabled` in `{"true", "yes", "on", "TRUE"}` both `simulator.Schedule.from_dict`
  and `door.Schedule.from_dict` return `False` where `make_bool` returns `True`. The
  documented wire value is `"1"`/`"0"` and this direction fails *closed* (a schedule
  becomes inactive rather than active), so it is hardening, not a live bug — but the
  asymmetry is now the odd one out. Minor companion nit at the same site:
  `door.Schedule.from_dict` passes an integer `enabled` through unchanged, so a
  field declared `enabled: bool` holds `1`/`0` and `to_dict()` emits ints; harmless
  today (`1 == True` keeps `schedule_entry_content_key` stable) but worth
  normalizing with the same `make_bool` call.

---

## Round 3 Fix Verification

All eight round-3 findings re-derived from the current source and exercised at
runtime where behavior was in question.

- **M1 — `shutdown()` during an in-flight `connect()`. FIXED.** `_adopt_transport`
  (`client.py:1150-1189`) checks `_shutdown` first and `transport.abort()`s. Verified
  against a local server: after `t = ensure_future(c.connect()); await sleep(0);
  c.shutdown(); await t` the client reports `_shutdown=True`, `available=False`,
  `_transport=None`, no keepalive task, and the server sees **zero** live
  connections, with a single `INFO Discarding a connection that completed after
  shutdown`. The door-layer route (`door.connect(timeout=T)` timing out and calling
  `client.shutdown()` mid-connect) inherits the same guard.
- **L1 — non-`OSError` connect failures. FIXED.** The widened
  `except (OSError, TimeoutError, ValueError, OverflowError)` covers all three round-3
  reproducers; verified that an over-long IDNA label, a lone-surrogate host and port
  `99999` each return normally from `connect()` with a reconnect scheduled and nothing
  raised. (`OverflowError` is an `ArithmeticError`, not a `ValueError` — listing it
  explicitly, as the code does, is required; round 3's recommendation was wrong on
  that point.) `start()` and `_schedule_reconnect()` now go through `_track_task`, so
  an escaping exception is logged by `_on_task_done` immediately. The last untracked
  fire-and-forget tasks are `_keepalive` and `_check_receipt` (`client.py:1176`,
  `1409`, `1412`), both cancelled by `disconnect()` and both narrow enough that this
  is not worth changing.
- **L2 — declined transport tearing down the live one. FIXED for the shim; the
  direct-wiring half is not (L1 above).** Verified with a real second
  `create_connection(lambda: client, ...)` against a live connection: `WARNING
  Rejecting a second connection` followed by `DEBUG Ignoring connection_lost() from a
  declined transport`, with the first connection left `available=True` and the server
  still reporting one live socket. I could not make `_declined` drift: `abort()` on a
  transport whose `_conn_lost` is still 0 always schedules exactly one
  `connection_lost`, so the count of ignore-decisions always equals the number of
  declined transports; the worst case is mis-attribution between two simultaneous
  losses, which produces the same net teardown.
- **L3 — engine replaying a stale `start_state`. FIXED.** `_defer_sequence` records
  `(_INTENT_OPEN|_INTENT_CLOSE, hold)` and `_start_pending_sequence` re-invokes
  `open()`/`close()`, which re-derive from the current status. Verified with a
  listener issuing `close()` on `DOOR_HOLDING`: both `hold_time=0.2` **and**
  `hold_time=0.0` now produce `RISING, SLOWING, HOLDING, CLOSING_TOP_OPEN,
  CLOSING_MID_OPEN, CLOSED` with no repeated status and `total_open_cycles == 1`.
  The re-entrant guard ordering is right — `open()`/`close()` evaluate their
  already-open/already-closing guards *before* `_defer_sequence`, so a redundant
  re-entrant request is dropped rather than deferred.
- **L4 — truthy `daysOfWeek` elements. FIXED at all three sites.**
  `["1","0","1","0","1","0","1"]` now yields `[True, False, True, False, True, False,
  True]` from `simulator.Schedule.from_dict`, from `door.Schedule.from_dict`, and
  through `compress_schedule`'s expand step (`[1,0,1,0,1,0,1]` on the way back out).
  Unrecognized elements are rejected (simulator) or fail closed (library).
- **L5 — fabricated 06:00–22:00 window. FIXED.** `{"index":1,"inside":true,
  "daysOfWeek":[1]*7}` with no times now answers `{"success":"false","reason":
  "Schedule is missing required field 'in_start_time'"}`. The neither-sensor-selected
  case still gets the harmless placeholder window, as intended.
- **T1 — stale `connection_lost` after `disconnect()`+`connect()`. PARTIALLY FIXED**
  — see T1 above.
- **T2 — no cancellation path for async lifecycle handlers. FIXED on the happy
  path**, with the cancellation gap in L2 above. Verified: `aclose()` awaits an async
  `on_disconnect` to completion, and cancels one that overruns. The snapshot is taken
  after `shutdown()`, so the `on_disconnect` tasks `disconnect()` itself creates are
  always included. Two non-findings I checked and discarded: handler tasks *spawned
  during* the wait are not covered (verified, but unreachable through any public API
  — `_dispatch_handler` only runs from `_adopt_transport` and `disconnect()`), and
  `aclose()` returns while the transient `_tasks` it cancelled are still in
  `cancelling` state (verified, but `run_until_complete(aclose())` reaps them in the
  same loop turn, so no "Task was destroyed but it is pending" warning results).

## Areas Reviewed With No Findings

- **Connection-identity machinery, exhaustively.** Beyond the round-3 verification
  above: the decline/adopt path has no race (`_adopt_transport` is fully synchronous
  inside `connection_made`, so `_shutdown` and `_transport` cannot change under it);
  the shim correctly *forwards* when `_transport is None`, which is what keeps the
  keepalive 3-strike path reconnecting (verified end to end — a client with
  `keepalive=0.05` against a silent server disconnects and comes back
  `available=True` with one live socket); `_ConnectionAttempt.__slots__` is genuinely
  effective (instances have no `__dict__`); the shim's `data_received` gate means a
  declined transport cannot inject frames (verified, `_tasks` stays empty); and
  `disconnect()`'s cancellation of `_tasks` cannot self-cancel an in-flight
  `connect()` reached from `reconnect()` because `asyncio.current_task()` is skipped.
  The one asyncio-level wart I found — `connect()`'s `asyncio.timeout` firing in the
  same loop iteration that `connection_made` adopts the transport, producing a
  connect/disconnect pair before recovering — needs the timeout to expire within a
  single `_run_once` of connection completion and self-heals, so it is not worth
  reporting.
- **Wire-validation helpers vs. what real peers send.** Round-tripped every payload
  the library itself emits through the new validators: `door.Schedule.to_dict()`
  (inside-only, outside-only, both, neither), `simulator.Schedule.to_dict()`, and
  `compress_schedule()` output all parse unchanged, as does the canonical
  `docs/protocol.md` payload. `holdTime` as the `int` centiseconds `door.set_hold_time`
  produces, `sensorTriggerVoltage`/`sleepSensorTriggerVoltage` as ints, `tz` as a
  POSIX string (real strings are ≪128 chars), and `SET_NOTIFICATIONS` as the `"1"`/`"0"`
  strings `door.set_notifications` builds are all accepted. The rejections
  (`"1500"` as a string, `Infinity`/`NaN`, containers, `holdTime > 90000`, empty flags)
  are all documented in `docs/protocol.md:454-458`, and nothing in this library, in
  `door.py`, or in `schedule.py` emits any of them. `_coerce_wire_flag` correctly
  routes through `make_bool`, so `1`/`"1"`/`true` are interchangeable exactly as the
  client reads them. `_coerce_schedule_int` catching `OverflowError` alongside
  `ValueError`/`TypeError` is right — `int(float("inf"))` raises the former.
- **`aclose()` semantics** beyond L2/T2 above: the empty-`tasks` early return avoids
  `asyncio.wait`'s `ValueError`; `current` is filtered so calling `aclose()` from
  inside a handler cannot deadlock; a second `aclose()` is a clean no-op; and the
  `door.disconnect()` → `aclose()` → `shutdown()` → `disconnect()` chain fails the
  outstanding futures *before* the handler wait, so an `_on_connect` reconnect-refresh
  in flight fails fast with `ConnectionError` rather than burning `default_timeout`
  (probed: `door.disconnect()` immediately after an auto-reconnect returned in 0.00 s).
- **framing.py** — `find_frame_end`'s escape/string state machine, `extract_frames`
  resync/overflow semantics and the 64 KiB cap re-verified as total and correct; both
  callers drop the connection on `diag.overflow`. Unchanged since round 3.
- **Client send pipeline** — the single-in-flight invariant across `enqueue_data` /
  `dequeue_data` / `_send_data` / `check_receipt` / `process_message` still holds;
  `check_receipt` nulls `self._check_receipt` before its awaits; the post-sleep
  transport re-check, the `_failed_msg`/`MAX_FAILED_MSG` bookkeeping and PING/PONG
  receipt matching are consistent with the simulator's reply envelope.
- **Reconnect backoff math** — exponent clamp, 300 s cap, jitter fraction and
  attempt-reset-on-adopt all correct; the stale-loss path of T1 increments
  `_reconnect_attempts` but the subsequent successful adopt resets it, so no drift.
- **schedule.py** — compression (inverted-range swap, `>=` adjacent merge, day
  collapse, inside/outside merge) and `compute_schedule_diff` (index reuse,
  lowest-unused-index assignment, deep copies, no input mutation) re-verified;
  `compress_schedule` output is always template-complete, so it survives the new
  required-time rule in `Schedule.from_dict`. `schedule_entry_content_key` is stable
  across the `enabled` int/bool inconsistency noted in T3.
- **tz_utils.py** — double-checked locking, copies returned from
  `get_available_timezones`, TZif footer extraction, and the angle-bracket-aware POSIX
  regex are sound. `_cache_initialized` is set only after the caches are fully
  populated and every consumer guards on `is_cache_initialized()`, so a concurrent
  reader cannot see a partial cache. No blocking I/O outside `asyncio.to_thread`.
- **door.py caching** — settings coercion via `make_bool` with None-preserving
  semantics, the inverted `cmd_lockout` handling, the schedule cache
  update/delete/sort logic, `delete_schedule`'s idempotent local fallback, and
  `_log_refresh_failures`' per-step reporting all still correct.
- **Simulator state machine and engine** — position-preserving reversal mappings,
  already-open/already-closing guards, auto-retract counting, hold-extension windows,
  `is_sensor_blocking_close`'s interaction with safety lock and `cmd_lockout`, the
  deadline-based hold loop (a stale wake costs at most one extra iteration; zero and
  negative `hold_time` terminate rather than spin), and `_cancel_deferred_restart`
  being called first by both `stop()` and `cancel_nowait()`.
- **Simulator server** — `stop()` ordering (battery task → engine → protocols →
  server), `_battery_tick` carry/threshold-crossing logic including the cap/floor
  remainder reset, and the `_owns_engine` split that keeps `protocol.aclose()` from
  stopping the shared server engine.
- **scripting.py** — step parsing, `_wait_for_status`'s waiter/stopper cleanup and
  stop-event interruption, condition/assert normalization. `set hold_time` and
  `add_schedule`'s `index` are unbounded (a script can park `state.hold_time` at
  `inf`, or allocate a schedule slot above `MAX_SCHEDULE_INDEX` that the wire path
  would reject), but scripts are operator-supplied local files, not a network input,
  so this is inside the trust boundary and not reported.
- **sanitize.py** — the character class covers C0 (except tab/newline), DEL and C1;
  it accepts non-`str` input via `str()`, is total, and cannot raise. Correctly placed
  in the library package so `client.py`/`schedule.py`/`tz_utils.py` can use it without
  pulling in the simulator front end. Only note is the unguarded hot-path call (T2).
- **const.py** — unchanged since round 3; `COMMAND_PRIORITIES` still covers every
  command the client sends, and `FIELD_MSG_ID`/`FIELD_MSG_ID_RESPONSE` casing
  asymmetry is intentional and matches `docs/protocol.md`.
- **Memory bounds** — `_outstanding` (self-cleaning done-callbacks), the heapq queue
  (cleared on disconnect), framing buffers (64 KiB cap), tz caches (bounded),
  `_tasks`/`_handler_tasks`/`_aux_tasks`/`_retired`/`_sensor_timers` (all discarded by
  done-callbacks), engine waiter/listener lists (removed in `finally`/unsubscribe),
  `state.schedules` (256 slots via `MAX_SCHEDULE_INDEX`). Each `connect()` allocates a
  fresh `_ConnectionAttempt`, which is released as soon as its transport is dropped —
  no accumulation across reconnect cycles.
