# Backend Developer Analysis — Round 9

Commit: `145cf05`. Scope: `client.py`, `door.py`, `const.py`, `schedule.py`,
`tz_utils.py`, `framing.py`, `sanitize.py`, `simulator/{server,protocol,state,
scripting,engine}.py`. CLI/ctl/prompt code excluded.

Every claim below was produced by running code against the checked-out tree.
Both proposed fixes were additionally applied to a throwaway copy of the repo
(`/tmp/ppd9/repo`, since deleted) and validated with the full deterministic
suite, `--cov`, `ruff` and `mypy` before being written down.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 0 |
| Low      | 2 |
| Trivial  | 0 |

Both findings are in code the round-8 fixes touched, which is where I was asked
to weight the pass. Neither is reachable from the wire today; both are defects
in the *guarantee* the round-8 work was written to establish, and both are the
kind that quietly re-open the original defect the next time the surrounding code
changes.

---

## Findings

### Finding 1 — Low — the round-8 `finally` restores flow control but not forward progress, so a raising dispatch still wedges the connection permanently for 99.8% of a burst drain

**File:** `src/powerpetdoor/framing.py:619-630` (`FrameDispatcher._pump`), with
the claim at `src/powerpetdoor/framing.py:617` and
`tests/test_framing.py:1245`.

Round 8 moved `_update_flow()` into a `finally` so that a `_dispatch` callback
which raises cannot leave the dispatcher in the state
`backlog > 0, inflight == 0, paused == True` — nothing in flight to deliver a
done-callback, no `call_soon` armed, and reading paused so the peer's FIN is
never read. The docstring records the intended guarantee:

> This dispatcher is a shared component and must not depend on its callback
> being total; with the ``finally`` the worst case is one frame lost.

That is true only when the backlog at the moment of the raise is at or below
`pause_at`. The re-arm — `if self._backlog and self._inflight < self._max_inflight:
self._schedule_pump()` — is still *inside* the `try`, so an exception skips it;
`_update_flow()` then runs and, because the backlog is over the threshold,
**pauses** rather than resumes. Nothing can ever pump again.

**Reproduction** (`/tmp/ppd9/probe_a2.py` — the `call_soon` re-arm path the
round-8 docstring itself names as the wedging one; the exception lands in the
loop exception handler, so asyncio never fatal-errors the transport):

```python
n = 0
def dispatch(frame):
    nonlocal n
    n += 1
    if n == 65:            # first frame of the SECOND pump (the call_soon re-arm)
        raise RuntimeError("handler blew up on the re-armed pump")
    return None            # unparseable frame: no task, so the pump re-arms

d = FrameDispatcher(dispatch)
t = FakeTransport()
d.submit([f"{{x{i}}}" for i in range(1000)], t)   # <- data_received() equivalent
for _ in range(2000):
    await asyncio.sleep(0)
```

Real output:

```
after submit(): backlog=936 inflight=0 paused=True transport.paused=True  (no exception escaped submit)
after 2000 loop turns: backlog=935 inflight=0 paused=True transport.paused=True
exceptions delivered to the loop handler: [RuntimeError('handler blew up on the re-armed pump')]
-> reading is paused, nothing is in flight, nothing is scheduled:
   _pump_scheduled=False
   backlog still holds 935 frames forever
```

The same probe with the raise on the *first* frame of a small batch reproduces
the case the shipped test pins, and shows the two branches side by side
(`/tmp/ppd9/probe_a.py`):

```
small backlog (<= pause_at), first dispatch raises:
  frames=10 raise_on=0: backlog=9 inflight=0 paused=False transport.paused=False ...
large backlog (> pause_at), first dispatch raises:
  frames=1000 raise_on=0: backlog=999 inflight=0 paused=True transport.paused=True pauses=1 resumes=0
  -> is it permanent?  simulating the peer going quiet (no more submit)
  after 1000 more loop turns: backlog=999 inflight=0 paused=True transport.paused=True
```

The branch the `finally` fixes is the rare one. Measuring a *real* client
draining the adversarial shapes this dispatcher exists for
(`/tmp/ppd9/probe_i.py`, spying on `_dispatch` in
`PowerPetDoorClient._dispatcher`):

```
  256 KiB read of '{}'  (each frame yields a task): 131072 dispatches, 130815 of them with backlog > pause_at (99.80%)   [pause_at=256]
  256 KiB read of '{x}' (each frame yields no task): 87381 dispatches, 87124 of them with backlog > pause_at (99.71%)   [pause_at=256]
```

So for a uniformly chosen frame in a one-read burst, 99.8% of the drain window
is still the wedging branch; the shipped test
`test_a_raising_dispatch_still_updates_flow_control` happens to sit in the other
0.2% because it uses `pause_at=1` and leaves exactly one frame behind.

**Reachability, stated plainly.** I could not reach this from the wire on the
current tree, and I looked: `_dispatch_frame` on both sides now wraps the only
raising statement (`json.loads`) in `except (ValueError, RecursionError)`, and
everything after it (`self.process_message(msg)` / `self._handle_message(msg)`
coroutine construction, `ensure_future`/`create_task`, `set.add`,
`add_done_callback`) cannot raise inside a `data_received` callback. The
throttle's own log path cannot raise either (`logging` swallows handler errors;
`sanitize_text` is total for a `str`). Round 8's Finding 1 was the only known
trigger and it is fixed. That is why this is Low and not Medium.

What makes it worth fixing anyway is that `FrameDispatcher` is documented as a
shared component that "must not depend on its callback being total", and the
next exception type that escapes a decoder — exactly the class of bug round 8
found — re-opens the full permanent wedge, not the "one frame lost" the
docstring and the test promise. `MemoryError` is the one non-hypothetical
member of that class, and it is most likely precisely when the backlog is at its
peak.

**Recommendation.** Move the re-arm into the `finally` alongside
`_update_flow()`; `_schedule_pump()` is already idempotent via `_pump_scheduled`,
so the normal path is unchanged and the exception path drains one frame per loop
turn instead of never.

```python
        finally:
            if self._backlog and self._inflight < self._max_inflight:
                self._schedule_pump()
            self._update_flow()
```

Validated in the throwaway copy — the same probes become:

```
after 2000 loop turns: backlog=0 inflight=0 paused=False transport.paused=False
  frames=1000 raise_on=0: backlog=0 ... dispatched_ok=999   (one frame lost, as documented)
```

and the suite stays green with no test changes:
`2678 passed`, `framing.py 100.00%` line and branch.
The docstring should also be corrected: the worst case is one frame lost *per
raise*, which is what this change actually makes true.

---

### Finding 2 — Low — the two throttled per-frame sites in `process_message` serialize every occurrence just to measure it: ~35 ms of discarded `json.dumps` per 64 KiB read, and a peer can make the reported byte total under-report its own traffic 30,001x

**File:** `src/powerpetdoor/client.py:1783-1788` (`_bad_messages`) and
`src/powerpetdoor/client.py:1854-1859` (`_device_errors`).

```python
rendered = json.dumps(msg)
if self._bad_messages.record(len(rendered)):
    _LOGGER.warning("Ignoring malformed message from device: %s",
                    sanitize_text(rendered, MAX_LOGGED_LENGTH))
```

Two problems, one cause: the message is re-serialized on *every* occurrence,
and the resulting length is used as the throttle's magnitude.

1. **The serialization is thrown away on ~99.9% of occurrences.** The whole
   point of `EventThrottle` is to make the cost of a peer-driven per-frame event
   logarithmic in the peer's traffic. These two sites made the *log volume*
   logarithmic and left the *CPU* linear, inside the host application's event
   loop.
2. **`record()` is handed the re-serialized size, not the received size.** Every
   sibling throttle on the receive path records real received bytes
   (`_non_ascii.record(len(data))` at `client.py:1688`,
   `_bad_frames.record(len(frame))` at `client.py:1748`). These two are the odd
   ones out, and the quantity they record is one a peer controls independently
   of what it actually sent.

**Reproduction A — the byte total** (`/tmp/ppd9/probe_e.py`, feeding a real
`PowerPetDoorClient` through `data_received`):

```
A. the malformed-message site (`_bad_messages`)
  compact `{}`
    wire bytes delivered: 2
    WARNING: Ignored 1 malformed message(s) from device (2 bytes) on this connection
  the same `{}` padded with 60,000 spaces
    wire bytes delivered: 60002
    WARNING: Ignored 1 malformed message(s) from device (2 bytes) on this connection
B. the device-error site (`_device_errors`)
  compact error envelope
    wire bytes delivered: 25
    WARNING: Device reported 1 error response(s) (28 bytes) on this connection
  error envelope padded with 60,000 spaces
    wire bytes delivered: 60027
    WARNING: Device reported 1 error response(s) (28 bytes) on this connection
C. for contrast, the bad-frame site (`_bad_frames`) on the same padding
  unparseable frame padded with 60,000 spaces
    wire bytes delivered: 60003
    ERROR: Failed to decode 1 JSON frame(s) from device (60003 bytes) on this connection
```

60,002 bytes on the wire reported as "2 bytes" (30,001x under-report);
60,027 reported as "28" (2,144x); and the compact envelope over-reports by 12%
(25 → 28) because `json.dumps` inserts the spaces the wire did not have. The
sibling site three code paths away gets the same padded frame exactly right.
This is the number an operator reads to decide whether a peer is worth
investigating, so a peer that pads its frames is invisible in it.

**Reproduction B — the CPU** (`/tmp/ppd9/probe_f.py`, one 64 KiB read into a
real client, counting the log records actually emitted and timing the
serialization that was performed):

```
  one 64 KiB read of b'{}'                        -> 32768 frames, 40 log records emitted (0.122%), json.dumps run 32768 times = 35.3 ms, 32728 of them discarded
  one 64 KiB read of b'{"CMD":"a","success":"0"}' -> 2621 frames, 24 log records emitted (0.916%), json.dumps run 2621 times = 3.7 ms, 2597 of them discarded
```

As a share of the whole per-frame receive path (`/tmp/ppd9/probe_d.py`, isolated
`json.dumps` cost over the measured end-to-end cost of
`data_received` → every handler task complete):

```
  error envelopes : 2621 frames in 26.7 ms  (10.18 us/frame)
  '{}' envelopes  : 32768 frames in 275.8 ms  (8.42 us/frame)
  json.dumps(error envelope) = 1.247 us/call   len(msg) = 0.021 us/call
  json.dumps('{}') = 0.869 us/call   len(msg) = 0.021 us/call
share of the whole per-frame receive path:
  error envelope: 11.7%   '{}': 10.2%
```

A/B of the whole path, 7 runs each, medians (`/tmp/ppd9/probe_d2.py`) —
reported with its variance rather than as a headline number, because the
end-to-end measurement is noisy:

```
  prez   error envelope  median 10.373 us/frame  runs=[11.03, 10.93, 10.28, 10.28, 10.37, 11.83, 10.14]
  prez   '{}'            median  9.268 us/frame  runs=[7.76, 10.45, 11.57, 9.81, 9.27, 9.0, 9.21]
  ppd9   error envelope  median  9.968 us/frame  runs=[9.29, 9.97, 8.94, 9.37, 15.97, 11.7, 10.76]
  ppd9   '{}'            median  8.340 us/frame  runs=[8.12, 9.27, 9.67, 8.4, 8.34, 7.91, 8.32]
```

The defensible claim is the isolated one: ~1 µs of pure serialization per frame
against a ~8-10 µs/frame path, i.e. ~10-12%, and 35.3 ms of it per 64 KiB read
of `{}`.

**Recommendation.** Pass the frame length the caller already has, and build the
string only when the throttle fires. `_dispatch_frame` has `len(frame)`;
`process_message` takes it as a keyword-only argument with a fallback, so direct
callers (tests, third-party subclasses) keep working:

```python
# _dispatch_frame
return self._track_task(self.process_message(msg, frame_size=len(frame)))

# process_message
async def process_message(self, msg, *, frame_size: int | None = None) -> None:
    ...
    if self._bad_messages.record(
        frame_size if frame_size is not None else len(json.dumps(msg))
    ):
        _LOGGER.warning("Ignoring malformed message from device: %s",
                        sanitize_text(json.dumps(msg), MAX_LOGGED_LENGTH))
```

(and the identical shape at the `_device_errors` site).

Validated in the throwaway copy. Byte totals become exact on every shape:

```
  the same `{}` padded with 60,000 spaces
    wire bytes delivered: 60002
    WARNING: Ignored 1 malformed message(s) from device (60002 bytes) on this connection
  error envelope padded with 60,000 spaces
    wire bytes delivered: 60027
    WARNING: Device reported 1 error response(s) (60027 bytes) on this connection
```

**This one is not free of test churn, and the diff has to own it.** Four test
harnesses monkeypatch `process_message` with a positional-only signature and
must accept `**_kwargs`
(`tests/test_client.py:1261` `_capture_messages._record`,
`tests/test_client.py:3833` `recording`,
`tests/test_client.py` `TestDecodeFailuresThatAreNotJSONDecodeError`'s lambda),
and one assertion pins the wrong number and has to be corrected:
`tests/test_client.py:4160` expects
`"Device reported 3 error response(s) (96 bytes)"` — 96 is the re-serialized
size; 87 is what the three frames actually were on the wire, and 87 is what the
fixed code reports. That assertion changing *is* the finding.

With those five edits plus Finding 1's change:

```
2678 passed in 50.94s
src/powerpetdoor/client.py    853  0  344  0  100.00%
src/powerpetdoor/framing.py   244  0   64  0  100.00%
TOTAL                        6773  0 2410  0  100.00%
All checks passed!            (ruff check)
80 files already formatted    (ruff format --check)
Success: no issues found in 31 source files   (mypy)
```

---

## Round 8 Fix Verification

**1. `except (ValueError, RecursionError)` on the throttled bad-frame path,
both sides — verified, and it swallows nothing it should not.**

`/tmp/ppd9/probe_c.py` drives the real `PowerPetDoorClient` and the real
`DoorSimulatorProtocol` through both non-`JSONDecodeError` shapes and then a
good frame:

```
=== CLIENT ===
  big-int literal (>4300 digits, bare ValueError)      closed=False aborted=False paused=False backlog=0 inflight=0
      ERROR: Failed to decode 1 JSON frame(s) from device (4408 bytes) on this connection
      ERROR: Failed to decode JSON frame (Exceeds the limit (4300 digits) for integer string conversion: value has 4400 digits; use s
  deep nesting (RecursionError)                        closed=False aborted=False paused=False backlog=0 inflight=0
      ERROR: Failed to decode 2 JSON frame(s) from device (364410 bytes) on this connection
      ERROR: Failed to decode JSON frame (maximum recursion depth exceeded while decoding a JSON object from a unicode string): {"a":
  plain bad JSON (JSONDecodeError)                     closed=False aborted=False paused=False backlog=0 inflight=0
  good frame after the bad ones                        closed=False aborted=False paused=False backlog=0 inflight=0
  door_status listener fired: ['DOOR_IDLE']   (proves the connection still works)
=== SIMULATOR ===
  big-int literal (>4300 digits, bare ValueError)      aborted=False paused=False backlog=0 bytes_out=0
      WARNING: Simulator: 1 JSON parse error(s) (4408 bytes) on this connection
  deep nesting (RecursionError)                        aborted=False paused=False backlog=0 bytes_out=0
      WARNING: Simulator: 2 JSON parse error(s) (364410 bytes) on this connection
```

The widened clause cannot swallow a programming error: the `try` body is exactly
`msg = json.loads(frame)` and nothing else. A bug *inside a response handler*
still gets a full, unthrottled traceback, which the same probe confirms by
replacing `_handle_door_status` with one that raises `AttributeError`:

```
  -- handler raises (programming error) --
      ERROR exc_info=True: Error handling GET_DOOR_STATUS response: {"CMD": "GET_DOOR_STATUS", "success": "true", "door_status": "X"}
```

I also swept every registered response handler looking for a wire-reachable way
to make one raise (that ERROR site is per-frame, unthrottled and
length-uncapped). I found none: every payload sub-object goes through
`_payload_mapping`, every field is guarded by `in`, `make_bool` is total, and
`_notify_listeners` isolates listener exceptions. No finding.

**2. `_update_flow()` in a `finally` in `_pump()` — partially verified;
see Finding 1.** The state it was written to prevent is gone for backlogs at or
below `pause_at` and still present above it.

**3. Fifty hostile connections return the daemon to baseline — verified.**
`/tmp/ppd9/probe_g.py` runs a real `DoorSimulator` on a real socket and throws
the >4300-digit literal, the 60,000-deep nesting and 20,000 `{x}` frames at it:

```
baseline: fds=9 clients=0
after 50 hostile connections: fds=9 clients=0
healthy client got: b'{"CMD": "GET_DOOR_STATUS", "success": "true", "dir": "d2p", "msgID": 1, "door_status": "DOOR_CLOSED"}'
after stop(): fds=8 clients=0
```

**4. `_keep_int(maximum=...)` at `_on_hold_time_update` — verified at the
boundary, on both signs, and the rejection log path is safe.**
`/tmp/ppd9/probe_b.py`:

```
maximum = 1.7976931348623157e+308
  int(float_max) - 1                     -> hold_time=1.7976931348623156e+306
  int(float_max)                         -> hold_time=1.7976931348623156e+306
  int(float_max) + 1                     -> hold_time=2.0        (rejected, cache kept)
  -int(float_max)                        -> hold_time=-1.7976931348623156e+306
  -int(float_max) - 1                    -> hold_time=2.0        (rejected, cache kept)
  float_max itself (float)               -> hold_time=1.7976931348623156e+306
  nan                                    -> hold_time=2.0
  inf                                    -> hold_time=2.0
  "200" (string)                         -> hold_time=2.0
  4299-digit int (rejected+logged)       -> hold_time=2.0
```

Round 8 declined `_log_rejected` → `sanitize_text` → `str(huge_int)` as
unreachable. That declination still holds, and the `maximum` bound does *not*
make it reachable, but the margin is now worth recording because the new bound
routes far more values into `_log_rejected` than before: `json.loads` accepts an
integer literal of exactly `sys.get_int_max_str_digits()` digits and refuses
4301, and `str()` uses the same limit, so the two can never disagree — including
if a host application lowers the limit, which lowers both.

```
what json.loads actually admits as an integer literal:
  4299 digits -> int, str() len=4299
  4300 digits -> int, str() len=4300
```

Note the round-8 comment says "over `sys.get_int_max_str_digits()` (4300)
digits", which is exact — 4300 is accepted, 4301 raises. No correction needed.

**5. The raw-bytes fuzz property.** `tests/fuzz/test_untrusted_input_fuzz.py`
feeds arbitrary bytes to `data_received` on both sides; the deterministic suite
plus the fuzz suite are both green on this tree.

---

## Areas Reviewed With No Findings

Each was probed by execution, not just read.

- **`_keep_int`'s other call sites** (`battery_percent`, `total_open_cycles`,
  `total_auto_retracts`) with `maximum=None`. Confirmed the round-8 decision is
  still right: a large int is only stored there, `BatteryInfo.charging`
  (`percent < 100`) is exact on arbitrary-precision ints, and nothing does float
  arithmetic on them. `_on_hold_time_update` is still the only consumer that
  divides. I deliberately did **not** propose bounding hold time at
  `MAX_HOLD_TIME_CENTISECONDS`: that is a reverse-engineered constant and the
  facade must not refuse a device value on its authority. A negative
  `holdTime` (`-500` → `hold_time == -5.0`) is accepted and cached; narrowing
  that would be narrowing what we accept from the device, so it is not reported
  as a finding.

- **`FrameDispatcher` ordering, `_inflight` accounting, `reset()` interaction.**
  Frames are dispatched strictly in order across the `call_soon` re-arm;
  `_inflight` returns to exactly 0 under cancellation; `reset()` with a pending
  continuation is safe. The only defect is Finding 1's branch.

- **The full receive path under the two adversarial reads** (`{}` and `{x}`,
  256 KiB). 131,072 and 87,381 frames drain completely, in order, with reading
  paused for 99.8% of the window and resumed exactly once at the end. No task
  leak, no lost frame.

- **Client message-queue state machine.** Traced every path that can leave
  `_can_dequeue == False` with a non-empty queue (`dequeue_data`'s two early
  returns, `_send_data`'s post-sleep transport re-check, `check_receipt`'s
  retry/drop fork, `process_message`'s `finally`). Every one is re-kicked by
  either the outstanding `check_receipt`, the response, or `disconnect()`, which
  clears the queue and fails all `_outstanding` futures with `ConnectionError`.
  Queue growth while disconnected is bounded by the reconnect cycle, because
  every failed attempt runs `handle_connect_failure()` → `disconnect()`.

- **`_LOGGER.exception("Error handling %s response: %s", cmd, json.dumps(msg))`
  at `client.py:1830`** — per-frame, unthrottled, length-uncapped, so it would be
  a fifth instance of the amplification class this project has removed four times.
  **Declined as unreachable**: I could not construct a frame that makes any
  registered handler raise (see Round 8 Fix Verification #1). Recorded here so
  the next round does not re-derive it, and so that it is on record as the site
  to re-check if a handler is ever added that indexes device data directly.

- **`sanitize_text` output bound and cost.** 64 KiB of `ESC` with
  `limit=MAX_LOGGED_LENGTH` produces exactly 814 characters (`200*4 + 14`), in
  81.7 µs, and only on occurrences the throttle actually reports. Truncate-
  before-escape holds.

- **`_POSIX_TZ_RE` and `_CONTROL_CHAR_RE` on adversarial input.** No
  catastrophic backtracking: 128 `A`s → 24 µs, `<` + 127 digits (unterminated) →
  4.5 µs, 4096 digits → 11.6 µs, 64 KiB of `<` → 228 µs. Linear.
  `parse_posix_tz_string` is a public export but is not called on device input by
  the client, the facade or the simulator protocol.

- **Simulator `SET_TIMEZONE` with hostile strings**, then `GET_SETTINGS` and a
  schedule evaluation on the stored value: lone surrogate, ANSI escape,
  128 chars (accepted), 129 chars (rejected, cache kept), empty. All five reply
  normally and `is_sensor_allowed_by_schedule` stays total — `json.dumps`'s
  `ensure_ascii` saves the `.encode("ascii")` in `_send`, and
  `zoneinfo.ZoneInfo`'s `UnicodeEncodeError` is a `ValueError` subclass, so
  `get_tzinfo`'s existing clause already catches it. The `MAX_TIMEZONE_LENGTH`
  boundary is correct at limit and limit+1.

- **`DoorSimulator`'s broadcast loops iterate `self.protocols` without copying.**
  `_send` can call `_drop_connection` → `transport.abort()`, which would mutate
  the list from `connection_lost` → `handle_disconnect`. Checked against CPython:
  `_SelectorTransport._force_close` defers `_call_connection_lost` through
  `call_soon`, so the mutation can never happen during the iteration. No finding.

- **`DoorMotionEngine`** hold/wake machinery, `extend_hold` racing a sequence
  that is not inside `_wait_for_wake` (one spurious wake, deadline recomputed),
  `_replace_sequence` retirement set, `cancel_nowait`/`stop` leaving zero pending
  tasks. Sound.

- **`tz_utils` cache initialization.** `_cache_lock` serializes builders;
  readers during a build see a partially populated dict and fall back rather than
  raise (dict reads are GIL-atomic). `get_available_timezones()` returns a copy.

- **`compute_schedule_diff` / `schedule_entry_content_key` / `compress_schedule`.**
  Re-read against the three-layer split. `SCHEDULE_WIRE_TO_DEVICE.enabled =
  wire_json_bool` (client→device) vs `SCHEDULE_WIRE_FROM_DEVICE.enabled =
  wire_flag_string` (device→client) remains correct and correctly documented as
  opposite directions. Nothing here should be unified.

- **`EventThrottle`** doubling schedule, `max_interval` cap and quiet-period
  restart — behave as documented (re-confirmed incidentally: 32,768 events
  produced 40 records, 24 for 2,621 events). The only defect touching it is what
  two callers *hand* it, which is Finding 2.
