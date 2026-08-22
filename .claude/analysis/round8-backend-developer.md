# Backend Developer Analysis — Round 8

Commit: `da31ae2` ("Round 7 fixes (refuter-approved list only)")
Scope: `client.py`, `door.py`, `const.py`, `schedule.py`, `tz_utils.py`, `framing.py`,
`sanitize.py`, `simulator/{server,protocol,state,scripting,engine}.py`. Not the CLI.
Baseline: `pytest -q` → **2620 passed in 37.48s** (both findings below are uncovered by it).

## Summary

| Severity | Count |
|----------|-------|
| High     | 0     |
| Medium   | 1     |
| Low      | 1     |

Two findings, both proven end-to-end over a real TCP socket against the real daemon and
the real shipped client. The four pieces of machinery I was asked to weight most heavily
— `_pump()`'s yield/re-arm, the latched write-ceiling drop path, the facade `_keep_*`
guards, and `SENSOR_NAMES` gating — are **correct**; every one of them is verified clean
under execution in "Round 7 Fix Verification" below. Finding 1 is in the frame *decode*
step that `_pump()` calls, not in `_pump()` itself. Finding 2 is a residual gap in one of
the five `_keep_*` call sites, not in the helpers.

---

## Findings

### Finding 1 — Medium — `json.loads` exceptions other than `JSONDecodeError` escape `data_received()`, killing the connection and bypassing every throttle

**Files:**
- `src/powerpetdoor/client.py:1727` (`_dispatch_frame`, `except json.JSONDecodeError`)
- `src/powerpetdoor/simulator/protocol.py:462` (`_dispatch_frame`, same)

Both decoders catch exactly `json.JSONDecodeError`. `JSONDecodeError` is a *subclass* of
`ValueError`, not the other way round, so any other exception `json.loads` can raise
propagates out of `_dispatch_frame` → `FrameDispatcher._pump()` → `submit()` →
`data_received()`. asyncio's `_read_ready__data_received` treats that as
`_fatal_error()`: it logs a full traceback through the loop's default exception handler
and tears the transport down.

There are two proven triggers, and neither is exotic:

1. **A JSON integer literal with more than 4300 digits.** CPython ≥ 3.11 caps
   `int`↔`str` conversion, and the json scanner surfaces that as a bare `ValueError`.
2. **A deeply nested object (depth ≥ 9999, ~60 KB).** Raises `RecursionError`.
   Critically this is *under* `MAX_BUFFER_SIZE` (65536), so the 64 KiB framing cap never
   fires, and the frame is accepted even when delivered in 1400-byte network-sized
   pieces.

This matters more than "one connection dies", for three reasons:

- It is a **documented invariant violation**. `client.data_received` says "This callback
  never raises on arbitrary input"; `framing.py`'s module docstring says "**Never
  raises** on arbitrary input."
- It **bypasses all of the round-5/6/7 log hardening**. The traceback is emitted by
  asyncio, not by this project, so `_bad_frames` never counts it, no `EventThrottle`
  bounds it, and `sanitize_text`/`MAX_LOGGED_LENGTH` never see it. Measured below:
  0 throttled records, 1 unbounded ~1.8 KB traceback per frame.
- On the shipped client it **defeats the reconnect backoff permanently**.
  `_adopt_transport()` resets `_reconnect_attempts = 0` on every successful connect, and
  the connect *does* succeed — it is the first frame that kills it. So the exponential
  backoff never grows and the client sits in a hot loop at the base interval, forever,
  with a full traceback every cycle. That is an unrecoverable state for a library whose
  stated goal is to stay up for years.

**Reproduction**

Trigger 1, against the real simulator daemon over a real socket:

```python
# frame = b'{"config":"GET_DOOR_STATUS","msgID":1' + b"0"*4400 + b'}'
```
```
simulator listening on 41697
normal reply: {"CMD": "GET_DOOR_STATUS", "success": "true", "dir": "d2p", "door_status": "DOOR_CLOSED"}
protocols after normal cmd: 1
hostile frame bytes: 4438
ERROR asyncio: Fatal error: protocol.data_received() call failed.
protocol: <powerpetdoor.simulator.protocol.DoorSimulatorProtocol object at 0x7f2ac5e8a510>
transport: <_SelectorSocketTransport fd=9 read=polling write=<idle, bufsize=0>>
Traceback (most recent call last):
  File ".../asyncio/selector_events.py", line 1023, in _read_ready__data_received
    self._protocol.data_received(data)
  File "/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/protocol.py", line 452, in data_received
    self._dispatcher.submit(frames, self.transport)
  File "/home/prez/src/pypowerpetdoor/src/powerpetdoor/framing.py", line 577, in submit
    self._pump()
  File "/home/prez/src/pypowerpetdoor/src/powerpetdoor/framing.py", line 612, in _pump
    task = self._dispatch(self._backlog.popleft())
  File "/home/prez/src/pypowerpetdoor/src/powerpetdoor/simulator/protocol.py", line 461, in _dispatch_frame
    msg = json.loads(frame)
  ...
ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 4401 digits
INFO powerpetdoor.simulator.protocol: Simulator: Client disconnected
protocols after hostile frame: 0
socket read after hostile: b''
```

Trigger 2, nested object, delivered in 1400-byte pieces:

```
frame bytes: 59995  MAX_BUFFER_SIZE: 65536  under cap: True
normal reply ok: True
protocols: 1
protocols after nested frame: 0
socket: b''
uncaught RecursionError in log: True
Fatal error records: 1
throttled 'JSON parse error' records: 0
log bytes: 1849  amplification: 0.03x
```

Note `throttled 'JSON parse error' records: 0` in both — the project's own throttle is
never reached.

Client side, permanent hot reconnect loop (base reconnect 0.20 s, 2.2 s of runtime):

```
device connections accepted: 10
inter-reconnect gaps (s): [0.248, 0.222, 0.231, 0.227, 0.223, 0.244, 0.232, 0.205, 0.206]
_reconnect_attempts after the run: 1
-> backoff never grows: every attempt CONNECTS (resetting the counter),
   then the frame kills it. Hot loop at the base interval, forever.
```

And with 74 cycles at DEBUG, every single one produced the asyncio fatal-error record and
zero throttled records:

```
occurrences of 'Fatal error: protocol.data_received' : 74
occurrences of 'Exceeds the limit' : 74
occurrences of throttled 'Failed to decode JSON frame' : 0
```

Supporting fact (the root cause, one line):

```
BARE ValueError (not JSONDecodeError): ValueError Exceeds the limit (4300 digits) ...
is JSONDecodeError? False
```

**Recommendation**

Widen the `except` in both `_dispatch_frame` implementations. `ValueError` subsumes
`JSONDecodeError`, so this *loosens* nothing about what we accept from the device — a
frame that fails to decode already takes the throttled "bad frame" path; this just routes
the two remaining decode failures onto that same existing path instead of out of the
callback:

```python
except (ValueError, RecursionError) as err:
    if self._bad_frames.record(len(frame)):
        ...
    return None
```

This is a parser-layer (layer 3) change only: it stays liberal, it accepts strictly more
input than today without crashing, and it changes no wire spelling in either direction.
Worth a regression test pinning both triggers, next to the existing DoS-bound tests.

---

### Finding 2 — Low — `_keep_int` passes arbitrarily large integers through, and `_on_hold_time_update` then raises `OverflowError` per frame with the cache left stale

**File:** `src/powerpetdoor/door.py:162-175` (`_keep_int`), consumed at
`src/powerpetdoor/door.py:1415-1416` (`_on_hold_time_update`)

`_keep_int` deliberately returns any `int` unchanged, and its comment says so
explicitly:

> `# isinstance(int) above already returned, so an arbitrarily large integer never
> reaches math.isfinite (which would overflow).`

That reasoning is correct about `math.isfinite`, but it stops one step short: the *only*
consumer that does float arithmetic on the result is `_on_hold_time_update`, which
immediately does `centiseconds / 100.0`. `int.__truediv__(float)` converts the int to a
float first, so any integer above ~1.8e308 raises `OverflowError`.

The result is precisely the defect shape the round-7 `_keep_*` guards were introduced to
remove, and which this very method's docstring describes ("made `value / 100.0` raise
TypeError, which the client's listener isolation turned into a full traceback *per frame*
while the cache stayed silently stale"). The guard closed the `"200"` and `NaN` spellings
and left this one open. Non-finite *floats* are correctly rejected (`1e400` → `inf` →
`math.isfinite` False → cached value kept); it is only the arbitrary-precision **integer**
literal that gets through.

Boundary and minimum trigger:

```
minimum digits of a JSON integer that makes _on_hold_time_update raise: 310
minimum hostile frame: {"CMD":"GET_HOLD_TIME","success":"true","holdTime":100000...000}
frame bytes: 362
_keep_int returns it unchanged (no rejection logged): True
float(v) -> OverflowError: int too large to convert to float
```

**Reproduction**

Unit level — note the cache is left stale and the client's listener isolation converts it
into a traceback rather than surfacing it:

```
initial hold_time: 2.0
parsed type: int digits: 401
(a) RAISED: OverflowError int too large to convert to float
    hold_time after: 2.0
(b) listener isolation swallowed it: True | traceback: 1
    hold_time still stale: 2.0
```

End-to-end over a real socket, a fake device answering `GET_SETTINGS` with
`holdOpenTime` as a 401-digit integer, 20 frames, log captured at WARNING and above (i.e.
a normal production level, not DEBUG):

```
frames: 20  wire bytes: 9380
log bytes at WARNING+: 9379
tracebacks: 20
OverflowErrors: 20
amplification: 1.00x
door.hold_time (still the constructor default): 2.0
```

20 frames → 20 unthrottled full tracebacks, and the cached `hold_time` never moves.

Reachability is honest but narrow, which is why this is Low and not Medium: it needs a
device sending a 310-digit `holdTime`/`holdOpenTime`. That is much less plausible from
real firmware than the `"200"` string spelling that motivated the round-7 fix, and the
measured amplification is 1.00x, so it buys an attacker nothing. It is reported because
it is a genuine, proven residual gap in a guard that was *specifically* added to make this
class of failure impossible, and because the helper's own comment shows the case was
considered and mis-resolved.

**Recommendation**

Bound the value inside `_keep_int` rather than at the one call site, so any future
consumer inherits the fix. The natural bound is "must survive the conversion the facade's
strict types require":

```python
if isinstance(value, int):
    if -sys.float_info.max <= value <= sys.float_info.max:
        return value
    # falls through to _log_rejected / keep cached
```

This is a layer-1 (strict Python API) change only. It does not narrow what the client
accepts from the device — `client._handle_hold_time` still resolves its future with
whatever the device sent, so a `send_message(..., notify=True)` caller sees the raw value
unchanged. Only the strictly-typed facade cache refuses it, which is exactly the rule the
module header states: *"nothing enters the facade cache without a type check, and a value
that fails it leaves the cache untouched."*

---

## Round 7 Fix Verification

All verified by execution against the current tree. All clean.

**R7 backend M1/L1 — facade `_keep_*` guards on every retained device value.** Confirmed
for all six listeners (`_on_battery_update` ×3 fields, `_on_hold_time_update`,
`_on_timezone_update`, `_on_total_cycles_update`, `_on_total_retracts_update`,
`_on_hw_info_update`) across 7 malformed shapes each:

```
caches untouched by 7 bad shapes x 6 listeners: True
values: (100, True, True, 2.0, '', 0, 0, {})
strict types held: ['int', 'bool', 'bool', 'float', 'str', 'int', 'int', 'dict']
battery.charging (R7-M1 symptom) still works: False
_keep_str accepts a real tz string: EST5EDT,M3.2.0,M11.1.0
_on_hw_info_update docstring no longer claims 'only payload retained': True
```

The layering is right: `_keep_str` still accepts `"55"` as a timezone (layer 3 stays
liberal about *content*; layer 1 only enforces the *type*). The sibling-swept fifth
listener `_on_hold_time_update` is present and guarded — Finding 2 is a residual value
range gap inside it, not a missing guard. The corrected `_on_hw_info_update` docstring no
longer makes the false "only payload retained" claim.

**R7 security M1 — `_pump()` yields (max_inflight frames per invocation, re-armed via
`call_soon`); a read cannot drain synchronously.** Confirmed, plus fairness, ordering,
re-entrancy, teardown-with-pending-`call_soon`, and inflight accounting:

```
== R7-M1: _pump() yields; no synchronous drain ==
  frames in one read       : 87381
  data_received() wall time: 57.8 ms  (was ~250 ms when it drained inline)
  backlog left after return: 87317
  frames handled in callback: 64 (== MAX_INFLIGHT_FRAMES 64)
  transport paused         : 1 time(s)  (backlog > 256)
  fully drained in         : 279 ms, backlog=0, resume_reading=1

== ordering preserved across the call_soon re-arm ==
  dispatched 500 frames, in order: True

== teardown: reset() with a pending _resume_pump ==
  after submit: backlog=936 pump_scheduled=True paused=True
  after reset : backlog=0 pump_scheduled=True transport=None
  after 1 turn: backlog=0 pump_scheduled=False inflight=0 (no crash, no stall)
  new connection drained  : backlog=0 inflight=0

== inflight accounting under cancel-during-flight ==
  inflight=64 backlog=136
  after reset+cancel: inflight=0 backlog=0
```

Notes on the two things I specifically went looking for and did **not** find:
- The stale-`_pump_scheduled` case (a `reset()` while a `call_soon` is pending) is benign:
  the pending callback lands one turn later, clears the flag, and no-ops on the empty
  backlog. A subsequent connection is not starved.
- The pump invariant holds: after every `_pump()`, either the backlog is empty, or
  `_inflight == max_inflight` (so a done-callback will pump), or a re-arm is scheduled.
  There is no state in which the backlog is non-empty with nothing scheduled to drain it.
- Flow control is symmetric: exactly one `pause_reading()` and one `resume_reading()`
  across a 87,381-frame burst, and `_inflight` never went negative under
  `reset()`-plus-cancellation.

**R7 security L2 — the write ceiling latches and uses `transport.abort()`.** Confirmed;
the drop is announced exactly once, the protocol really leaves `DoorSimulator.protocols`
(so `ctl status` is truthful), and 50 subsequent `broadcast_all()` calls re-check nothing:

```
== write ceiling (latched) ==
  MAX_WRITE_BACKLOG        : 1048576
  protocols still tracked  : 0 (0 == ctl status is truthful)
  ERROR records emitted    : 1
  drop-summary records     : 1
  log bytes                : 165
  ERROR records from 50 broadcast_all() after drop: 0
```

**R7 frontend M2 — `SENSOR_NAMES` is the single sensor vocabulary in `engine.py`.**
Confirmed; every near-miss name is refused with a warning, returns `None` (preserving the
documented programmatic-API contract), moves no door and sets no sensor-active flag, and a
valid name still works:

```
SENSOR_NAMES: ('inside', 'outside')
  'insde'      -> returns None/None, status DOOR_CLOSED->DOOR_CLOSED, warned=True, active=False/False
  'INSIDE'     -> returns None/None, status DOOR_CLOSED->DOOR_CLOSED, warned=True, active=False/False
  ''           -> returns None/None, status DOOR_CLOSED->DOOR_CLOSED, warned=True, active=False/False
  'outside '   -> returns None/None, status DOOR_CLOSED->DOOR_CLOSED, warned=True, active=False/False
  'inside'     -> status now DOOR_RISING (engine still functional)
```

**R7 security L3 — four more per-frame log sites throttled.** Confirmed for the
`_bad_frames` site under a 50-frame `{x}` flood on one connection: 50 events → 13 records,
logarithmic as designed.

```
--- {x} (throttled JSON parse error)
    frame bytes  : 3   frames: 50   wire bytes: 150
    log bytes    : 1151
    amplification: 7.67x
    tracebacks   : 0
    throttled WARNs: 13
```

**R7 — DoS bound constants pinned by tests.** All present and imported cleanly:
`MAX_BUFFER_SIZE=65536`, `MAX_RETAINED_PIECES=64`, `MAX_INFLIGHT_FRAMES=64`,
`MAX_FRAME_BACKLOG=256`, `MAX_WRITE_BACKLOG=1048576`, `MAX_LOGGED_LENGTH=200`,
`MAX_SCHEDULE_INDEX=255`. Full suite green at 2620 tests.

**R7 — `parse_arg` rejects non-finite floats with validate-then-write ordering.** Lives in
`simulator/commands/base.py`, i.e. the CLI, which is outside this round's scope. Not
re-verified here.

---

## Areas Reviewed With No Findings

Each of these was probed by execution, not just read.

- **`FrameDispatcher._pump()` fairness / starvation / re-entrancy / ordering / teardown.**
  Covered above. No stall state exists; ordering survives the `call_soon` re-arm;
  `_inflight` accounting is exact under cancellation; pause/resume is symmetric. The
  transient backlog peak (~87k frames for a 256 KiB read of `{x}`) is bounded by
  `pause_reading()` at one read's worth and is unchanged from before the round-7 fix —
  `submit()` always extended the deque before pumping — so the fix introduced no memory
  regression, only a duration change while reading is already paused.
- **Latched write-ceiling drop path** (`protocol._send`/`_drop_connection`). Verified
  above. `_dropped` is set before `abort()`, is never cleared (per-connection object), and
  correctly short-circuits both queued frames and later broadcasts.
- **Facade `_keep_*` guards.** Verified above; the only gap is Finding 2. I also checked
  the other `_keep_int` consumers (`battery.percent`, `total_open_cycles`,
  `total_auto_retracts`): a large int is merely stored there, `BatteryInfo.charging`
  (`percent < 100`) works on arbitrary-precision ints, and nothing raises.
- **`_log_rejected` → `sanitize_text(value)` with a >4300-digit int** would raise on
  `str()`. **Declined as unreachable**: `json.loads` refuses such a literal before it can
  ever reach a listener (that refusal is Finding 1), and fixing Finding 1 does not make it
  reachable, because `json.loads` still refuses it. Reported here only so the next round
  does not re-derive it as a finding.
- **`FrameScanner` piece coalescing.** 80,000 characters fed as 2-char segments with the
  object never terminating: 63 retained pieces (cap 64), 15 KiB heap delta. The character
  cap is still a memory cap.
- **`EventThrottle` doubling schedule and quiet-period restart.** 2000 rapid events → 11
  reports; then 20 events after 120 s of quiet → 5 more, i.e. a fresh burst is reported
  immediately and gets its own schedule. Both bounds behave as documented.
- **`DoorMotionEngine` deferred-sequence machinery.** A status listener that commands the
  door re-entrantly produced exactly one live owner task, no duplicate consecutive status
  broadcasts, and `stop()` left zero pending tasks.
- **Client connection-identity machinery** (`_ConnectionAttempt`, `_declined`,
  `_pending_direct_losses`, `_on_transport_lost`). Exercised incidentally across ~74
  connect/teardown cycles in the Finding 1 client probe with no spurious teardown, no
  double-reconnect and no orphaned socket.
- **Reconnect backoff and jitter.** Correct in isolation; the only defect is that
  Finding 1 defeats it by making every attempt *succeed* before dying, which resets
  `_reconnect_attempts`. That is reported as part of Finding 1, not separately.
- **Schedule three-layer split** (`schedule.py`, `door.Schedule`, `state.Schedule`).
  Re-read against the hard constraint. `SCHEDULE_WIRE_TO_DEVICE.enabled=wire_json_bool`
  (client→device) vs `SCHEDULE_WIRE_FROM_DEVICE.enabled=wire_flag_string` (device→client)
  is deliberate and correctly documented as opposite directions, not twins. No finding,
  and nothing here should be "unified".
- **Simulator wire coercion** (`_coerce_wire_number`/`_int`/`_string`/`_flag`,
  `_wire_schedule_index`). Validate-before-mutate ordering holds at every `SET_*` site;
  `WireValueError` is raised before any state assignment.
- **Battery simulation carry** (`server._battery_tick`). Fractional accumulation and
  cap/floor remainder discard behave as documented.
- **`make_sensor_notification` / `state.is_sensor_allowed`** both use
  `if sensor == "inside" ... else`, so an unknown name would fall into the outside branch.
  **Declined**: both are only reachable through `engine.trigger_sensor`, which gates on
  `_known_sensor` first (verified above). Same unreachable-quirk class I declined in
  round 7.
