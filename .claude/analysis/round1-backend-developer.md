# Backend Developer Analysis — Round 1

Scope: `src/powerpetdoor/{client.py, door.py, const.py, schedule.py, tz_utils.py}` and
`src/powerpetdoor/simulator/{server.py, protocol.py, state.py, scripting.py}`, plus
`docs/{client.md, door.md, protocol.md, simulator.md}` for public-API accuracy.
The interactive CLI (`cli.py`, `ctl.py`, `prompt_common.py`, `commands/`) is out of scope.

Findings marked **[verified at runtime]** were reproduced with throwaway scripts against the
in-repo simulator/venv (scripts deleted afterwards; no repo files modified).

## Summary

| Severity | Count |
|----------|-------|
| Critical | 1 |
| High     | 4 |
| Medium   | 12 |
| Low      | 21 |
| Trivial  | 7 |
| **Total** | **45** |

---

## Findings

### Critical

#### C1. The documented default-loop usage of `PowerPetDoor`/`PowerPetDoorClient` is non-functional **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:268-274`, `src/powerpetdoor/door.py:318,328,332-339`, `docs/door.md:37,78`
- **Problem**: When `loop=None`, `PowerPetDoorClient.__init__` calls `asyncio.new_event_loop()` and binds every
  `ensure_future()` to that brand-new loop — which is *never run* unless the caller uses the blocking
  `client.start()` path. `door.py`'s constructor docstring and `docs/door.md` claim `loop=None` "uses current
  loop", and the Quick Start (`door = PowerPetDoor("192.168.1.100"); await door.connect()`) follows that.
  Reproduced against the simulator: the TCP socket actually connects, but `create_connection` awaited
  cross-loop raises a `RuntimeError` that is swallowed by the bare `except:` (see H1), the client logs
  "Unable to connect... Reconnecting", the reconnect task is scheduled onto the dead loop (never runs,
  later reported as "Task was destroyed but it is pending"), and `door.connect()` returns *silently
  unconnected* after its 5-second poll loop (`door.connected` returns `None`). Every test in the suite
  passes `loop=asyncio.get_running_loop()` explicitly, so this primary documented path has zero coverage.
- **Recommendation**: In `PowerPetDoorClient.__init__`, when `loop is None` resolve the loop lazily —
  use `asyncio.get_running_loop()` at `connect()`/`start()` time (create a private loop only in the
  blocking `start()` path). Make `door.connect()` raise on failure instead of returning silently.
  Add a test that constructs `PowerPetDoor(host)` with no loop inside a running loop.

### High

#### H1. Bare `except:` in `connect()` swallows `CancelledError` and every other `BaseException`

- **Files**: `src/powerpetdoor/client.py:830-835` (also bare `except:` at `client.py:1022`)
- **Problem**: `async def connect()` wraps `create_connection` in `try: ... except: self.handle_connect_failure()`.
  This catches `asyncio.CancelledError`, so cancelling a task that is awaiting `connect()` (e.g.
  `asyncio.wait_for(door.connect(), ...)`, or app shutdown) does not cancel it — it *schedules a reconnect*
  instead, converting cancellation into a reconnect loop. It also masked the cross-loop `RuntimeError` in C1,
  which is why C1 fails silently. The bare `except:` in `data_received` (decode path) similarly hides the real
  error class and message.
- **Recommendation**: Catch `(OSError, asyncio.TimeoutError)` explicitly, log the exception with context
  (`_LOGGER.error(..., exc_info=True)` or include `err`), and let `CancelledError` propagate. In
  `data_received`, catch `UnicodeDecodeError` and log the offending bytes.

#### H2. `stop()`/`disconnect()` cannot stop a pending reconnect — client can reconnect after shutdown

- **Files**: `src/powerpetdoor/client.py:818-825` (`stop`), `850-860` (`connection_lost`/`reconnect`),
  `827-835` (`connect`), `893-898` (`handle_connect_failure`)
- **Problem**: The `reconnect()` task created in `connection_lost`/`handle_connect_failure` is fire-and-forget —
  no reference is kept, so `stop()`/`disconnect()` cannot cancel it. Neither `reconnect()` nor `connect()`
  checks `self._shutdown`. Sequence: connection drops → reconnect task starts sleeping → caller calls
  `stop()` (sets `_shutdown`, disconnects) → sleep expires → `connect()` runs unconditionally → connection
  re-established, keepalive task restarted. On a shared event loop (the Home Assistant case) this leaves a
  live zombie connection and keepalive task after the integration is unloaded. `PowerPetDoor.disconnect()`
  has the same hole.
- **Recommendation**: Store the reconnect task (`self._reconnect_task`), cancel it in `disconnect()`/`stop()`,
  and guard `connect()`/`reconnect()` with `if self._shutdown: return`.

#### H3. Receive framing wedges the connection on any inter-message whitespace; contradicts protocol.md's newline framing **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:167-196` (`find_end`), `1014-1038` (`data_received`);
  `docs/protocol.md:36,44`
- **Problem**: `docs/protocol.md` states messages are "single-line JSON objects terminated by newline",
  yet: (a) neither client nor simulator sends a trailing newline, and (b) the client cannot *receive*
  newline-terminated (or whitespace-separated) messages at all. `find_end` raises `IndexError` when the
  buffer does not start with `{`, and the calls at `client.py:1026` and `1038` are outside any `try`.
  Reproduced: feeding `'{...} {...}'` raises `IndexError` out of `data_received`, and the residual garbage
  stays at the head of `self._buffer`, so **every subsequent `data_received` also fails** — no responses are
  processed, all in-flight commands retry and drop, until the ping-timeout path (~3× keepalive ≈ 90 s with
  defaults) finally disconnects and clears the buffer. One stray byte from the device costs minutes of
  outage per occurrence. If the real device does terminate with `\n` as documented, the client wedges on the
  first message.
- **Recommendation**: In `data_received`, strip leading whitespace/newlines before framing (`self._buffer =
  self._buffer.lstrip()`), and treat a non-`{` leading byte as a resync condition (discard up to the next `{`,
  log an error) instead of raising. Reconcile `docs/protocol.md`'s "Line Terminator" claim with what the
  device actually sends.

#### H4. Futures for dropped messages are never resolved — documented `await future` pattern hangs forever

- **Files**: `src/powerpetdoor/client.py:917-934` (`check_receipt`), `1074-1104` (`send_message`);
  `docs/client.md:144-147`
- **Problem**: When a message gets no response, `check_receipt` retries once, then logs "dropped" and moves
  on (`self._failed_msg = 0; await self.dequeue_data()`). The future stored in `self._outstanding[msgId]`
  is neither cancelled nor failed — it stays pending until the connection eventually drops. `docs/client.md`
  explicitly documents `future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True); result = await
  future` — that await hangs indefinitely on a dropped message. (`door.py` happens to escape via
  `asyncio.wait_for`, but the low-level API is the documented public surface.)
- **Recommendation**: When dropping a message, resolve its future with an exception (e.g.
  `future.set_exception(TimeoutError(f"No response to {self._last_command}"))`). Track the msgId of the
  in-flight message so `check_receipt` can find its future.

### Medium

#### M1. `disconnect()` causes `KeyError` in future done-callbacks on every disconnect with in-flight commands **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:885-887, 1092-1094`
- **Problem**: `disconnect()` cancels each outstanding future, then rebinds `self._outstanding = {}`.
  The done-callback registered in `send_message` runs `del self._outstanding[msgId]` *after* the rebind
  (done-callbacks run via `call_soon`), raising `KeyError` into the loop's exception handler for every
  in-flight command at disconnect time. Reproduced: `KeyError: 1` captured by the loop exception handler.
  This spams error logs on every reconnect cycle under load.
- **Recommendation**: Use `self._outstanding.pop(msgId, None)` in the cleanup callback, and in
  `disconnect()` iterate over `list(self._outstanding.values())` and `clear()` the same dict instead of
  rebinding it.

#### M2. Sensor-listener callback arity contradicts type annotations and docs — documented example raises `TypeError`

- **Files**: `src/powerpetdoor/client.py:281-289, 352-368` (annotations `Callable[[bool], None]`),
  `531-535, 596-606, 613-617, 624-628, 639-643, 652-656, 665-669` (invoked as `callback(field, val)`);
  `docs/client.md:276-283, 317`; `src/powerpetdoor/door.py:1145-1179`
- **Problem**: All sensor listeners are invoked with two arguments `(field, value)`, but `add_listener`'s
  type hints declare `Callable[[bool], None]` and `docs/client.md` documents one-arg lambdas
  (`FIELD_POWER: lambda val: ...`). Anyone following the docs gets `TypeError` on the first settings or
  sensor response. `door.py`'s own workaround (`def _on_power_update(self, *args): value = args[-1]` with
  the comment "Handle both (value) and (field, value) signatures") is evidence the inconsistency is known
  and being papered over.
- **Recommendation**: Pick one signature — `(field, value)` is the more useful (it enables the `"*"`
  wildcard) — fix the annotations and `docs/client.md`, and remove the `*args` shims in `door.py`.

#### M3. Exceptions in `process_message`/response handlers strand futures and vanish as unretrieved task exceptions

- **Files**: `src/powerpetdoor/client.py:1033` (handler runs in fire-and-forget task), `1040-1072`
  (`msg[FIELD_CMD]`, `msg[FIELD_SUCCESS]` unconditional), `517-552` (`_handle_get_settings` indexes
  `settings[FIELD_TZ]`, `settings[FIELD_HOLD_OPEN_TIME]`, `settings[FIELD_SENSOR_TRIGGER_VOLTAGE]`,
  `settings[FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE]` unconditionally)
- **Problem**: `process_message` is run via `ensure_future` with no exception handling. Any message missing
  `CMD` or `success` (protocol.md's own Notification Events section documents exactly such messages, e.g.
  `{"SENSOR_INDOOR": "", "sensorState": "on"}` — see M4), or a settings payload from a firmware variant
  missing `tz`/`holdOpenTime`/voltage fields, raises `KeyError` inside the task. The paired future is then
  neither resolved nor cancelled (the post-handler `future.cancel()` at line 1067 is skipped), the caller
  waits out its full timeout, and the only trace is a late "Task exception was never retrieved" log.
  Additionally, one listener callback raising aborts all remaining listeners for that message (no per-callback
  try/except, unlike `door.py`'s callback loops).
- **Recommendation**: Wrap handler dispatch in try/except that logs with the offending message and fails the
  future (`set_exception`); use `msg.get()` for envelope fields; guard optional settings fields; isolate each
  listener callback in its own try/except.

#### M4. Notification events: client can't parse the documented format, simulator sends a third format, and `holdTime` vs `holdOpenTime` disagree between docs and code

- **Files**: `docs/protocol.md:340,448` (settings uses `holdTime`), `469-473` (notification format);
  `src/powerpetdoor/const.py:61-62`; `src/powerpetdoor/simulator/state.py:352` (settings uses
  `holdOpenTime`); `src/powerpetdoor/simulator/protocol.py:814-841`, `simulator/server.py:260-270`
  (notifications sent as `{"CMD": "SENSOR_INDOOR", "success": "true", ...}`); `src/powerpetdoor/client.py:543`
- **Problem**: Three-way divergence on the wire format the project itself declares authoritative:
  1. `protocol.md` says GET_SETTINGS contains `holdTime`; client and simulator both use `holdOpenTime` —
     against a device matching the doc, `_handle_get_settings` raises `KeyError` (M3) and settings refresh
     times out.
  2. `protocol.md` documents notifications as `{"SENSOR_INDOOR": "", "sensorState": "on"}` /
     `{"LOW_BATTERY": ""}` (no `CMD`, no `success`); the simulator instead sends CMD-style messages with
     `success`. A real device following the doc would crash `process_message` (M3); the simulator format is
     merely ignored (no handler registered).
  3. The client has no handler and no listener API at all for `NOTIFY_SENSOR_INDOOR`/`NOTIFY_SENSOR_OUTDOOR`/
     `NOTIFY_LOW_BATTERY` even though `const.py` defines them — device events are silently dropped.
- **Recommendation**: Determine the real device's actual settings key and notification envelope, fix whichever
  side is wrong, make the simulator emit exactly that, add client handlers + a listener hook for the three
  notification types, and add symmetric client/simulator tests per the project's own protocol-change rule.

#### M5. Simulator replies `success: "true"` to unknown commands

- **Files**: `src/powerpetdoor/simulator/protocol.py:274, 287-293`
- **Problem**: `response` is initialized with `FIELD_SUCCESS: SUCCESS_TRUE`; when no handler is registered the
  code only logs a warning and still sends the success response. A client sending an unsupported/misspelled
  command against the simulator sees success, masking real bugs the simulator exists to catch (protocol.md
  documents a `success: "false"` + `reason` error envelope).
- **Recommendation**: For unknown commands set `FIELD_SUCCESS = SUCCESS_FALSE` and `reason = "Unknown command"`
  (matching observed device behavior if known).

#### M6. `PowerPetDoor` lifecycle is not re-entrant: private-state pokes, `KeyError` on early/double disconnect, no auto-reconnect after reconnect

- **Files**: `src/powerpetdoor/door.py:459-464`; `src/powerpetdoor/client.py:346-350`
- **Problem**: `door.disconnect()` reaches into the client's private `_shutdown` attribute (component-boundary
  violation) and calls `del_handlers`, which uses unconditional `del` — so `door.disconnect()` before
  `connect()`, or called twice, raises `KeyError`. (`add_handlers` registers conditionally, `del_handlers`
  deletes unconditionally — `add_handlers("x", on_connect=cb)` + `del_handlers("x")` raises for any client
  user too.) After `disconnect()`, a subsequent `door.connect()` leaves `_shutdown = True`, so the next
  connection loss never auto-reconnects.
- **Recommendation**: Give the client a public `shutdown()`/`async close()`; make `del_handlers` use
  `pop(name, None)`; have `door.connect()` reset the shutdown flag through that public API.

#### M7. Disconnect during the rate-limit sleep in `_send_data` raises uncaught `AttributeError`

- **Files**: `src/powerpetdoor/client.py:952-978`
- **Problem**: `_send_data` checks `self._transport`, then may `await asyncio.sleep(...)` (up to
  `MINIMUM_TIME_BETWEEN_MSGS` = 200 ms), then calls `self._transport.write(rawdata)`. If `disconnect()` runs
  during the sleep, `_transport` is `None` and `.write` raises `AttributeError`; the `except RuntimeError`
  clause does not catch it, so the send task dies with an unretrieved exception.
- **Recommendation**: Re-check `self._transport` after the sleep (capture it locally), and broaden the except
  to `(RuntimeError, AttributeError, OSError)` or check `transport.is_closing()`.

#### M8. Simulator door task cancels-and-awaits *itself* on the auto-retract path — works only by accident

- **Files**: `src/powerpetdoor/simulator/protocol.py:632-638, 694, 767-775, 783-789, 796`
- **Problem**: `close_sequence()` runs as `self._door_task`. On auto-retract it calls
  `await self._simulate_door_open()`, which does `self._door_task.cancel(); await self._door_task` — i.e. the
  running task cancels itself and then awaits itself. This only works because asyncio replaces the "Task
  cannot await on itself" `RuntimeError` with the pending `CancelledError`, which the adjacent
  `except asyncio.CancelledError: pass` swallows, leaving the task running with its cancellation silently
  consumed (no `uncancel()` — which corrupts cancel-count bookkeeping if this ever runs under
  `asyncio.timeout()` on 3.11+). Any refactor of that try/except breaks door auto-retract.
- **Recommendation**: In `_simulate_door_open`/`_simulate_door_close`, skip the cancel/await when
  `self._door_task is asyncio.current_task()`; better, have the close sequence *return* a "retract" signal
  and let a single owner task chain sequences instead of cross-cancelling.

#### M9. Simulator schedule enforcement silently falls back to UTC after a client sets a POSIX timezone

- **Files**: `src/powerpetdoor/simulator/state.py:392-400`; `src/powerpetdoor/simulator/protocol.py:564-568`
- **Problem**: `SET_TIMEZONE` stores the raw wire value. The protocol's wire format is POSIX
  (`EST5EDT,M3.2.0,M11.1.0` — protocol.md:107-115, and `door.set_timezone` docs say POSIX), but
  `is_sensor_allowed_by_schedule` feeds `state.timezone` to `zoneinfo.ZoneInfo`, which only accepts IANA
  names. After any client SET_TIMEZONE, `ZoneInfo` raises and the bare `except Exception` silently falls back
  to UTC — schedule windows are then evaluated in the wrong timezone with no log.
- **Recommendation**: On SET_TIMEZONE, reverse-map via the existing `tz_utils.find_iana_for_posix()` and store
  the IANA name (keeping the POSIX string for wire responses); log a warning when falling back to UTC.

#### M10. `door.connect()` polls, cannot report failure, and cached state goes stale after auto-reconnects

- **Files**: `src/powerpetdoor/door.py:448-457, 1220-1226`
- **Problem**: `door.connect()` busy-polls `client.available` at 100 ms for up to 5 s (real time — with a slow
  device or first reconnect delay it gives up), then returns `None` either way, so callers can't distinguish
  success from failure. Separately, the door registers `on_connect=self._on_connect`, which only invokes user
  callbacks — it never re-runs `refresh()`. After any client-level auto-reconnect, all cached properties
  (`status`, `battery`, sensors, schedules) silently serve pre-disconnect values until something external
  pushes an update.
- **Recommendation**: Replace the poll with an `asyncio.Event` set from the client's `on_connect` hook (with a
  proper timeout that raises), and schedule `refresh()` from `_on_connect` so the cache resynchronizes after
  every reconnect.

#### M11. Race: response future can be completed after `wait_for` cancellation → `InvalidStateError` in handler

- **Files**: `src/powerpetdoor/client.py:1046-1064` (with the `await self.dequeue_data()` at 1055/1058
  suspending mid-processing), `961-963` (200 ms rate-limit sleep inside that await)
- **Problem**: `process_message` selects the future (checking `.cancelled()` at line 1048), then *awaits*
  `dequeue_data()` — which can sleep up to `MINIMUM_TIME_BETWEEN_MSGS` (200 ms) sending the next queued
  message — before dispatching the handler. If the caller's `asyncio.wait_for` expires inside that window and
  cancels the future, the handler then calls `future.set_result(...)` on a cancelled future, raising
  `asyncio.InvalidStateError` inside the task (unretrieved) and aborting the rest of message processing,
  including listener notification. The window is realistic: it is exactly the timeout boundary where
  responses arrive late.
- **Recommendation**: Dispatch the handler *before* dequeuing the next message, and/or guard every
  `future.set_result`/`set_exception` with `if future and not future.done()`.

#### M12. Door state machine implemented twice (server "direct" path vs protocol path) with behavioral drift

- **Files**: `src/powerpetdoor/simulator/server.py:530-675` (`_direct_open_door`/`_direct_close_door`/
  `_check_sensor_retract`, plus `_direct_trigger_sensor:494-528`); `src/powerpetdoor/simulator/protocol.py:611-796,
  843-908` (`_simulate_door_open`/`_do_close_sequence`/`trigger_sensor`)
- **Problem**: The full open/hold/close/retract sequence exists in both files (the project's own CLAUDE.md
  rule: "Two implementations = refactor"). They have already drifted: the protocol path supports hold-time
  extension on sensor re-trigger (`self._hold_remaining`, `sensor_retrigger_window`) and sensor-during-close
  handling; the direct path uses a local `hold_remaining` variable and `_direct_trigger_sensor` has no
  HOLDING/CLOSING handling, so with no client connected a re-trigger neither extends the hold nor arms a
  retract the same way. Scripts therefore behave differently depending on whether a client happens to be
  connected (`trigger_sensor` picks the path based on `self.protocols`).
- **Recommendation**: Extract one door-motion engine (operating on `DoorSimulatorState` + a broadcast
  callback) used by both the server (no clients) and the protocol; keep exactly one hold/retract loop.

### Low

#### L1. No reconnect backoff or jitter
- **Files**: `src/powerpetdoor/client.py:855-860, 893-898`
- Fixed `cfg_reconnect` (default 5 s) forever. With an unreachable device this retries indefinitely at a
  constant rate; against the real door (single-connection device, protocol.md:35-38) several clients
  retrying in lockstep contend for the one slot. Recommend exponential backoff with jitter, capped.

#### L2. `on_disconnect` callbacks fire on every failed connect attempt, even when never connected
- **Files**: `src/powerpetdoor/client.py:893-898, 885-891`
- `handle_connect_failure` calls `disconnect()`, which unconditionally invokes all `on_disconnect`
  callbacks — an unreachable device produces a disconnect event every 5 s despite no connection ever
  existing. Track "was connected" and only notify on a real transition.

#### L3. Messages queued while disconnected are not flushed on reconnect
- **Files**: `src/powerpetdoor/client.py:837-848 (connection_made), 936-950 (enqueue_data)`
- `connection_made` sets `_can_dequeue = True` but never kicks `dequeue_data()`; anything enqueued while
  disconnected sits until the *next* `enqueue_data` call. Kick the queue in `connection_made` when non-empty.

#### L4. `available` returns `None` instead of `False`; `send_message` return annotation wrong **[verified at runtime]**
- **Files**: `src/powerpetdoor/client.py:1106-1109, 1074`
- `self._transport and not ...` yields `None` when no transport (observed: `door.connected` → `None`).
  Wrap in `bool(...)`. `send_message` is annotated `-> None` but returns a `Future` when `notify=True`;
  annotate `-> asyncio.Future | None` (docs/client.md:162-164 already describe the Future).

#### L5. JSON framing ignores braces inside string values
- **Files**: `src/powerpetdoor/client.py:186-196`; `src/powerpetdoor/simulator/protocol.py:218-231`
- Both `find_end` implementations count `{`/`}` without string awareness; any string value containing a brace
  (e.g. a device error `reason`) breaks framing. Track in-string/escape state (~6 lines) in the shared helper.

#### L6. `docs/client.md` priority table is inverted relative to the code
- **Files**: `docs/client.md:399-406` vs `src/powerpetdoor/const.py:155-217`
- Docs: "Medium (2) Status queries / Low (3) Configuration changes". Code: Medium = settings changes,
  Low = status queries/schedules. Fix the doc table.

#### L7. `docs/simulator.md` references a nonexistent extra `pypowerpetdoor[yaml]`
- **Files**: `docs/simulator.md:37`; `pyproject.toml:33-39`
- The extras are `simulator` and `interactive`; `[yaml]` does not exist, so the documented install command
  silently installs nothing extra. Change to `pypowerpetdoor[simulator]`.

#### L8. `docs/protocol.md` examples contradict its own data-format rules
- **Files**: `docs/protocol.md:78,98-103` (success/booleans are strings) vs `164,186,205,236-238,260,321,331,
  355,373,386,430-432` (`"success": true`, `"hasRemoteId": true`)
- The simulator sends strings (`"true"`, `"1"`), the client requires `success == "true"`; the doc examples
  showing JSON booleans would parse as *failures* in the client. Fix the examples.

#### L9. Cancellation is used as an application-level error signal to API callers
- **Files**: `src/powerpetdoor/client.py:885-887, 1066-1068`
- Futures are `cancel()`ed both on disconnect and when a successful response lacks the expected field.
  Callers awaiting them get `CancelledError`, indistinguishable from genuine task cancellation (and prone to
  cancelling the caller's scope in structured-concurrency contexts). Use `set_exception(ConnectionError(...))`
  / a library exception instead.

#### L10. Command failures surface as bare `Exception("Command Failed")`, discarding the device's `reason`
- **Files**: `src/powerpetdoor/client.py:1069-1072`; `docs/protocol.md:73-76`
- The error envelope includes `reason`, which is only logged, not propagated; callers cannot catch a typed
  error or see why. Define `class CommandError(Exception)` carrying `cmd` and `reason`.

#### L11. Wall-clock `time.time()` used for latency and retrigger timing
- **Files**: `src/powerpetdoor/client.py:797, 914, 961-966`; `src/powerpetdoor/simulator/protocol.py:849,880`
- NTP steps produce negative/garbage ping latencies and skewed rate-limit/retrigger windows. Use
  `time.monotonic()` for intervals (the PING token can stay wall-clock for wire compatibility).

#### L12. `tz_utils` cache init has no concurrency guard and leaks the internal list
- **Files**: `src/powerpetdoor/tz_utils.py:96-105, 124-132`
- Two concurrent `async_init_timezone_cache()` calls both run the full ~600-file scan (`_cache_initialized`
  is only set at the end). `get_available_timezones()` returns the internal `_iana_timezones` list by
  reference (callers can mutate the cache). Guard with an `asyncio.Lock`/module `threading.Lock`; return a copy
  or tuple.

#### L13. `compute_schedule_diff` mutates its `new_schedule` input
- **Files**: `src/powerpetdoor/schedule.py:362-372`
- Entries in the caller's `new_schedule` get `entry[FIELD_INDEX]` overwritten in place; the docstring doesn't
  say so. Deep-copy entries destined for `entries_to_set` (or document the mutation explicitly).

#### L14. Simulator fire-and-forget tasks are untracked; `stop()` cannot cancel them
- **Files**: `src/powerpetdoor/simulator/server.py:528, 735, 748, 764`; `src/powerpetdoor/simulator/protocol.py:211, 522, 908`
- `asyncio.create_task` results are discarded (door cycles, sensor deactivation timers, per-message handlers).
  Exceptions surface only as "Task exception was never retrieved"; `DoorSimulator.stop()` cancels only
  `protocol._door_task`, so direct-path door cycles outlive/stall shutdown ("Task was destroyed but it is
  pending"). Keep a task set (`self._tasks.add(t); t.add_done_callback(self._tasks.discard)`) and cancel in
  `stop()`.

#### L15. Simulator receive buffer never resyncs and grows without bound on garbage input
- **Files**: `src/powerpetdoor/simulator/protocol.py:194-231`
- `_find_json_end` returns `None` for a non-`{` head, the loop breaks, and the garbage stays at the head of
  `self.buffer` forever — the connection is silently wedged and the buffer grows with every byte received.
  Resync (discard to next `{`) and cap the buffer size.

#### L16. `DoorStatus.from_string` silently maps unknown states to `CLOSED`
- **Files**: `src/powerpetdoor/door.py:129-135`
- A new/unknown firmware status string would make a physically open door report `is_closed == True` with no
  log. At minimum log a warning; consider an explicit `UNKNOWN` member.

#### L17. Error logs without tracebacks / eager string formatting in hot paths
- **Files**: `src/powerpetdoor/simulator/server.py:243-244`; `src/powerpetdoor/simulator/protocol.py:213, 215-216`;
  `src/powerpetdoor/client.py:964, 977, 1019` (f-string/`str.format` into `_LOGGER.debug`)
- `logger.error(f"... {e}")` drops the traceback (use `exc_info=True`); debug-level TX/RX logs format the
  payload even when debug is disabled (use lazy `%s` args).

#### L18. `parse_posix_tz_string` cannot parse angle-bracket abbreviations
- **Files**: `src/powerpetdoor/tz_utils.py:183-193`
- Modern tzdata emits POSIX strings like `<+05>-5` / `<-03>3` for many zones; the `[A-Za-z]+` regex fails and
  the function returns a dict with all abbrev/offset fields `None`. Extend the regex to accept `<...>` forms.

#### L19. `async-timeout` dependency is unnecessary on Python ≥3.11
- **Files**: `pyproject.toml:14, 28-31`; `src/powerpetdoor/client.py:11, 831`
- With `requires-python >= 3.11`, stdlib `asyncio.timeout()` covers the single use site. Dropping the
  dependency shrinks the compatibility surface and matches the project's "dependency-light" goal.

#### L20. Half-committed thread-safety: thread-safe queue, thread-unsafe scheduling
- **Files**: `src/powerpetdoor/client.py:265 (queue.PriorityQueue), 320-324, 936-950, 1086-1094`
- The class exposes `run_coroutine_threadsafe` and uses a lock-based `queue.PriorityQueue`, implying
  cross-thread use — but `send_message`/`enqueue_data` call `create_future`/`ensure_future` directly, which
  are not thread-safe. Either document "call only from the loop thread" and switch to a plain `heapq`
  (cheaper), or route foreign-thread sends through `call_soon_threadsafe`.

#### L21. Inline protocol strings in the simulator bypass `const.py`
- **Files**: `src/powerpetdoor/simulator/server.py:252, 265, 401, 417, 428, 438, 448, 458` (literal `"CMD"`);
  `src/powerpetdoor/simulator/protocol.py:446, 450, 454` (literal `"hasRemoteId"`, `"hasRemoteKey"`,
  `"resetReason"` — `FIELD_HAS_REMOTE_ID`/`FIELD_HAS_REMOTE_KEY`/`FIELD_RESET_REASON` exist in const.py and
  the client uses them)
- Violates the project's client/simulator symmetry rule; a constant rename would silently desynchronize the
  two sides. Replace the literals with the const.py names.

### Trivial

#### T1. Loop variable `field` shadows the `dataclasses.field` import
- **Files**: `src/powerpetdoor/client.py:16, 531, 565` — rename to `field_name`.

#### T2. `DoorSimulatorState.hold_time` annotated `int` but assigned a float
- **Files**: `src/powerpetdoor/simulator/state.py:270`; `src/powerpetdoor/simulator/protocol.py:574` — annotate `float`.

#### T3. `docs/door.md` staleness
- **Files**: `docs/door.md:324, 421` (shows `days_of_week` as int list; dataclass now uses `list[bool]` —
  commit 7593a1f), `260-263` (`hardware_version` property missing from the Hardware Properties table).

#### T4. Mixed logging styles throughout client.py
- **Files**: `src/powerpetdoor/client.py:829, 907, 923, 964` — `str.format`, `.format()`, and f-strings mixed;
  standardize on lazy `%s`-style.

#### T5. `requires-python >= 3.11` vs the project's own "support all non-EOL CPython" mandate
- **Files**: `pyproject.toml:14`; `.claude/CLAUDE.md` version-matrix rules — CPython 3.10 is not EOL until
  2026-10. Either the policy or the floor should be adjusted (nothing in the code requires 3.11 except the
  optional `asyncio.timeout` swap in L19).

#### T6. 10 Hz polling hold loops with accumulated drift
- **Files**: `src/powerpetdoor/simulator/protocol.py:680-689`; `src/powerpetdoor/simulator/server.py:584-593` —
  `sleep(0.1)` decrement loops both burn wakeups and drift (decrement assumes exactly 0.1 s elapsed). An
  event/deadline-based wait would be exact and cheaper.

#### T7. `compress_schedule` requires fully-populated entries but doesn't say so
- **Files**: `src/powerpetdoor/schedule.py:130-171` — direct indexing (`sched[in_start_key][FIELD_HOUR]`)
  raises `KeyError` on sparse entries; `validate_schedule_entry` exists but is not invoked and the docstring
  doesn't state the precondition. Document it or validate up front.

---

## Areas Reviewed With No Findings

- **`schedule.py` algorithms**: `compute_schedule_diff` index reuse/lowest-unused-index assignment is correct
  (including the int-vs-bool `daysOfWeek` content keys — `hash(1) == hash(True)` makes the lookups sound);
  `compress_schedule`'s overlap merge (`>=` correctly merges adjacent windows) and day-collapse steps are
  correct; `week_0_mon_to_sun`/`week_0_sun_to_mon` are correct inverses.
- **`tz_utils` blocking-I/O discipline**: the TZif-footer POSIX extraction is correct, and all file I/O is
  properly confined to `asyncio.to_thread` / an explicitly-documented sync variant — no blocking I/O on the
  event loop in the client, door, or simulator hot paths.
- **Priority queue semantics**: `PrioritizedMessage` (priority, monotonic sequence) gives correct FIFO order
  within a priority level; PING at `PRIORITY_CRITICAL` correctly preempts queued traffic; `COMMAND_PRIORITIES`
  coverage in `const.py` is complete for all commands the client sends.
- **Keepalive/PONG matching**: `_handle_pong` token matching, failed-ping counting, and the
  `keepalive`/`check_receipt` "captured task + cancelled() re-check" pattern are sound (the defensive check is
  redundant but harmless).
- **Hold-time units**: centiseconds are handled consistently end-to-end (client `SET/GET_HOLD_TIME`,
  `settings.holdOpenTime`, door's `/100.0`, simulator's `*100`) — matching the recent centiseconds test work.
  (The *field name* discrepancy with protocol.md is M4; the unit math itself is clean.)
- **Simulator door state-machine transitions**: the reversal mappings (RISING↔CLOSING_MID_OPEN,
  SLOWING↔CLOSING_TOP_OPEN) are position-consistent in both implementations, and the already-open/
  already-closing guards prevent duplicate sequences (duplication itself is M12).
- **Optional-dependency guarding**: `prompt_toolkit` (via `PROMPT_TOOLKIT_AVAILABLE`) and `PyYAML`
  (`YAML_AVAILABLE`) are correctly guarded, so `import powerpetdoor.simulator` works with no extras installed.
- **Listener registries**: `add_listener`/`del_listener` bookkeeping is symmetric and idempotent
  (`pop(name, None)` throughout `del_listener`); no unbounded growth — re-registration under the same name
  replaces entries. (The unsafe `del` in `del_handlers` is M6.)
- **Simulator scripting engine**: step parsing, condition checks, and assertion normalization are sound;
  `ScriptRunner.run` cannot hit the `step` UnboundLocal edge (exceptions only arise inside the loop); YAML
  loading uses `safe_load`.
- **Packaging**: `package-data` correctly ships the YAML scripts; entry points resolve; coverage/mypy/ruff
  configs are coherent with the src layout; `.github/workflows/` (test.yml, release.yml) exists.
- **Memory growth in caches**: door's `_schedules` cache is bounded by device schedule slots; client listener
  dicts, `_outstanding` (with M1 fixed), and tz caches are all bounded — no unbounded history/log structures
  found in the backend.
