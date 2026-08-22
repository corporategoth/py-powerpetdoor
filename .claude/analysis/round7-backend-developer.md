# Backend Developer Analysis — Round 7

Scope: `src/powerpetdoor/{client.py, door.py, const.py, schedule.py, tz_utils.py,
framing.py, sanitize.py}` and `src/powerpetdoor/simulator/{server.py, protocol.py,
state.py, scripting.py, engine.py}` at commit `a0194bd`. The interactive CLI
(`cli.py`, `ctl.py`, `prompt_common.py`, `commands/`) is out of scope.

Ground rules I held myself to: every finding below was **reproduced by running
code against the in-repo source**, and the transcript pasted under
**Reproduction** is the real output of the command shown. Nothing is reported on
the strength of reading alone. No file in the repo was modified (`git status`
clean apart from another persona's report); every probe script was written under
`/tmp/ppdprobe` and deleted, and no process was left running.

Baseline on this tree: `uv run pytest -q -p no:randomly` → **2454 passed** in 35.6 s.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 1 |
| Low      | 1 |
| Trivial  | 0 |
| **Total** | **2** |

All six round-6 findings are fixed and hold under runtime probing, and the three
pieces of machinery the brief asked me to weight hardest — `FrameDispatcher`,
`EventThrottle`'s clock/quiet-period, and the `SCHEDULE_WIRE_TO_DEVICE` /
`_FROM_DEVICE` split — are **correct**. I fuzzed the dispatcher 7 000 randomized
sequences (frame conservation, in-flight bound, pause/resume bookkeeping,
transport churn), measured the burst it was built for (65 live tasks and 8.2 MB
instead of 131 073 tasks and ~145 MB), and confirmed the wire split is exactly one
field wide and round-trips through a real simulator over a real socket. I found
nothing wrong in any of them.

Both findings are in the same place, and it is not the new machinery: the
**facade's cached state**. `door.py` is layer 1 (strict Python types), the client
is layer 3 (correctly liberal), and the five listeners that carry a device value
across that boundary are not consistent about coercing. Round 6 M1 fixed one of
them (`_on_hw_info_update`) and its fix docstring asserts that `_hw_info` is
"the only device payload the facade *retains*" — which is what appears to have
stopped the sweep there. It retains four more. The Medium is the one whose
retained poison makes a documented public property **raise**, silently. The Low is
the type-contract half of the same root cause.

---

## Findings

### Medium

#### M1. `_on_battery_update` caches device values with no coercion: `door.battery_percent` returns a `str`/`None` from a property documented `-> int`, and `door.battery.charging` raises `TypeError` — with nothing logged

- **Files**: `src/powerpetdoor/door.py:1308-1314` (`_on_battery_update`),
  `src/powerpetdoor/door.py:171-186` (`BatteryInfo`, `charging`, `discharging`),
  `src/powerpetdoor/door.py:846-864` (`battery_percent`, `battery_present`,
  `ac_present`, `battery`), `src/powerpetdoor/door.py:1184-1194`
  (`refresh_battery`), `src/powerpetdoor/client.py:970-979` (`_handle_battery`).

**Reproduction**

`/tmp/ppdprobe/REPRO_battery.py` — a real `PowerPetDoor` doing a real
`connect()` over a real TCP socket, against a device whose `GET_DOOR_BATTERY`
reply spells `batteryPercent` as a string. (`docs/protocol.md:414-419` shows that
object's other two fields, `batteryPresent` and `acPresent`, already **are**
`"1"`/`"0"` strings, which is what makes this spelling plausible rather than
exotic.)

```python
REPLIES = {
    "GET_DOOR_STATUS": {"door_status": "DOOR_IDLE"},
    "GET_SETTINGS": {"settings": {"power": "1"}},
    # a firmware variant that spells the level like its two siblings:
    "GET_DOOR_BATTERY": {"batteryPercent": "55", "batteryPresent": "1", "acPresent": "1"},
    "GET_DOOR_OPEN_STATS": {"totalOpenCycles": 5, "totalAutoRetracts": 1},
    "GET_HW_INFO": {"fwInfo": {"fw_major": 1}},
    "GET_NOTIFICATIONS": {"notifications": {}},
}
# ... asyncio server replying with that; then:
door = PowerPetDoor("127.0.0.1", port=port, keepalive=0, timeout=2.0)
await door.connect()                       # runs refresh() -> GET_DOOR_BATTERY
```

```
$ uv run python /tmp/ppdprobe/REPRO_battery.py
cached BatteryInfo before connect: BatteryInfo(percent=100, present=True, ac_present=True)
cached BatteryInfo after  refresh: BatteryInfo(percent='55', present=True, ac_present=True)
door.battery_percent  -> '55'   (documented '-> int', 0-100)
door.battery_present  -> True   (documented '-> bool')
door.ac_present       -> True   (documented '-> bool')
door.battery.charging -> RAISED TypeError: '<' not supported between instances of 'str' and 'int'
WARNING-or-worse log records emitted by the library: []
```

Same script with the field **absent** (`{"batteryPresent": "1", "acPresent": "1"}`)
— i.e. the case `_on_battery_update`'s `data.get(K, self._battery.percent)`
default was clearly written to handle:

```
cached BatteryInfo after  refresh: BatteryInfo(percent=None, present=True, ac_present=True)
door.battery_percent  -> None   (documented '-> int', 0-100)
door.battery.charging -> RAISED TypeError: '<' not supported between instances of 'NoneType' and 'int'
WARNING-or-worse log records emitted by the library: []
```

And with `acPresent` set to a string `make_bool` does not recognize
(`"maybe"` — a value the client is *designed* to turn into `None`,
`client.py:238-258`), from `/tmp/ppdprobe/p3b_battery.py`:

```
=== acPresent unrecognized string ===  device reply: {"batteryPercent": 40, "batteryPresent": "1", "acPresent": "maybe"}
  cached BatteryInfo: BatteryInfo(percent=40, present=True, ac_present=None)
  door.ac_present       = None     type=NoneType
  battery.charging      = None     type=NoneType
```

Independently reachable by undirected fuzzing — 300 sessions against a device
emitting random junk frames (`/tmp/ppdprobe/p13_hostile_device.py`), reading every
documented property after every session:

```
300 hostile-device sessions: rss delta=1480 KiB  fd delta=1  tasks=1  unhandled-loop-exc=0
exceptions escaping documented API surfaces:
   refresh_schedules:TimeoutError                 300
   refresh_battery:TimeoutError                   300
   refresh_hardware_info:TimeoutError             300
   refresh_stats:TimeoutError                     300
   battery.charging:TypeError                       5
```

`battery.charging` is the **only** non-`TimeoutError` escape in that entire sweep.

**Description**

`client._handle_battery` (layer 3) is correct as written — it stays liberal and
hands the facade whatever the device said:

```python
data = {
    FIELD_BATTERY_PERCENT: msg.get(FIELD_BATTERY_PERCENT),
    FIELD_BATTERY_PRESENT: make_bool(msg.get(FIELD_BATTERY_PRESENT)),
    FIELD_AC_PRESENT: make_bool(msg.get(FIELD_AC_PRESENT)),
}
```

`door._on_battery_update` (layer 1) then assigns those values straight into a
dataclass declared `percent: int`, `present: bool`, `ac_present: bool`:

```python
self._battery = BatteryInfo(
    percent=data.get(FIELD_BATTERY_PERCENT, self._battery.percent),
    present=data.get(FIELD_BATTERY_PRESENT, self._battery.present),
    ac_present=data.get(FIELD_AC_PRESENT, self._battery.ac_present),
)
```

Three separate problems compound here:

1. **The intended fallback is dead code.** The `dict.get(key, cached)` defaults
   read as "keep the previously cached value if the device did not report this
   one". They can never fire: `_handle_battery` builds the dict with all three
   keys *always present*, holding `None` when the device omitted the field. So
   the "keep the cache" path is unreachable and the cache is overwritten with
   `None` instead. This half is a plain code defect, independent of any firmware
   question.
2. **`make_bool` is documented to return `None`** for values it does not
   recognize (`client.py:244-248`), and `_on_battery_update` is the **one**
   facade listener that does not guard for it. Its seven siblings
   (`_on_power_update`, `_on_inside_update`, `_on_outside_update`,
   `_on_auto_update`, `_on_safety_lock_update`, `_on_autoretract_update`,
   `_on_cmd_lockout_update`, `door.py:1279-1306`) all say
   `if value is not None:` before caching, and `_on_settings` does the same
   inline. This one does not.
3. **The failure is retained and silent.** Nothing raises inside the listener, so
   `_notify_listeners`' isolation never triggers and no log record is produced
   (measured: 0 records at WARNING or above). The `TypeError` surfaces later, in
   a property read, with nothing tying it to the frame that caused it — and a
   device that spells the field one way once will spell it that way every time,
   so it does not heal.

This is the *identical* shape to round-6 M1 (`fwInfo` cached into `_hw_info`,
poisoning three public properties silently) and round-4 M1 (`AttributeError`
escaping a documented coroutine instead of a handleable `ValueError`). The
round-6 fix even landed the right guard one method away — `_on_hw_info_update`
at `door.py:1323-1345` validates and logs before caching — but its docstring
says:

> Guarded because this is the only device payload the facade *retains*

which is not true (`_battery`, `_total_open_cycles`, `_total_auto_retracts`,
`_timezone` are all retained), and is presumably why the sweep stopped there.

Blast radius beyond the library: `refresh_battery()` is a documented public
coroutine returning `BatteryInfo`, and the `ha-powerpetdoor` integration reads
`battery_percent` for a sensor entity, so a `str`/`None` there lands in a
consumer that will do arithmetic on it.

**Recommendation**

Fix at the facade (layer 1), not at the client (layer 3) — the client's dict is
also the `send_message(..., notify=True)` result for `GET_DOOR_BATTERY`, so
narrowing it would change a public result shape for no benefit. Mirror what
`_on_hw_info_update` already does:

```python
def _on_battery_update(self, data: dict[str, Any]) -> None:
    percent = data.get(FIELD_BATTERY_PERCENT)
    present = data.get(FIELD_BATTERY_PRESENT)
    ac_present = data.get(FIELD_AC_PRESENT)
    self._battery = BatteryInfo(
        percent=int(percent) if isinstance(percent, (int, float))
                and not isinstance(percent, bool) else self._battery.percent,
        present=present if isinstance(present, bool) else self._battery.present,
        ac_present=ac_present if isinstance(ac_present, bool) else self._battery.ac_present,
    )
```

That makes the documented "keep the cached value" fallback actually work, keeps
`BatteryInfo`'s declared types honest, and matches the `if value is not None:`
convention its seven siblings already follow. Log once (throttled or at DEBUG)
when a field is dropped so the operator has something to correlate. While there,
fix the `_on_hw_info_update` docstring's "the only device payload the facade
retains" claim — that sentence is what makes the next sweep stop early too.
A regression test belongs in `tests/test_door.py` for each of
`batteryPercent` string / absent / `null` and `acPresent` unrecognized.

---

### Low

#### L1. Three more facade listeners cache device values verbatim into strictly-typed attributes, so `total_open_cycles`, `total_auto_retracts` and `timezone` can return the wrong Python type — silently

- **Files**: `src/powerpetdoor/door.py:1346-1350`
  (`_on_total_cycles_update`, `_on_total_retracts_update`),
  `src/powerpetdoor/door.py:1320-1321` (`_on_timezone_update`);
  properties at `door.py:900-907` (`-> int`) and `door.py:825-828` (`-> str`).
  Sources: `client.py:841-864` (`_handle_door_open_stats`) and
  `client.py:790-793` (`tz` out of `GET_SETTINGS`).

**Reproduction**

`/tmp/ppdprobe/p14_facade_cache.py` — same real-socket harness as M1, one case per
listener, checking the value against the type the property documents and counting
WARNING-or-worse records:

```
$ uv run python /tmp/ppdprobe/p14_facade_cache.py

### battery: percent omitted, acPresent=1
   battery_percent=None (NoneType)  <-- declared int
   WARNING+ logged: 0 []

### stats: strings
   total_open_cycles='5' (str)  <-- declared int | total_auto_retracts='7' (str)  <-- declared int
   WARNING+ logged: 0 []

### stats: nulls
   total_open_cycles=None (NoneType)  <-- declared int | total_auto_retracts=None (NoneType)  <-- declared int
   WARNING+ logged: 0 []

### settings tz is an int
   timezone=5 (int)  <-- declared str
   WARNING+ logged: 0 []

### hw_info: scalar (R6 M1 fix)
   firmware_version='' (str) | hardware_version='' (str) | hardware_info={} (dict)
   WARNING+ logged: 2 ['Device sent a non-mapping fwInfo payload; not notifying hw_info listen', 'Ignoring non-mapping hardware info: 1.2.3']
```

The last block is the round-6 M1 fix working exactly as intended — a non-mapping
payload is refused, logged twice, and the three properties keep valid values. It
is included as the control: the same input class produces a clean, logged refusal
for `fwInfo` and a silent type violation for the other four.

**Description**

Same root cause as M1, without the raise. `_handle_door_open_stats` correctly
passes `msg[FIELD_TOTAL_OPEN_CYCLES]` through untouched (layer 3 stays liberal),
and `_handle_get_settings` does the same for `tz`; the facade's listeners are
declared `(field_name: str, value: int)` / `(value: str)` and simply assign. No
coercion, no `isinstance` guard, no log line.

Nothing in this library then does arithmetic on those values, which is why this is
Low and not Medium — the damage is entirely at the API boundary. But
`PowerPetDoor` is the documented strict-typed layer, `total_open_cycles` and
`total_auto_retracts` are annotated `-> int` and `timezone` `-> str`, and a
consumer (the Home Assistant integration publishes these as sensor states) is
entitled to rely on that. The current behavior also means a device that reports a
garbage counter overwrites a previously good cached one with garbage, rather than
keeping the last good value — the same "the fallback never fires" shape as M1.

**Recommendation**

One shared guard, applied at the three sites:

```python
def _on_total_cycles_update(self, field_name: str, value: int) -> None:
    if isinstance(value, int) and not isinstance(value, bool):
        self._total_open_cycles = value
```

…and the `str` equivalent for `_on_timezone_update`. That restores the
`if value is not None:` convention the seven sensor listeners already follow, and
makes every value the facade *retains* pass through a type check exactly once —
which is the invariant round-6 M1 was reaching for. Worth stating as a rule in
the `PowerPetDoor` class docstring: *nothing enters the facade cache without a
type check*, since this is now the fifth instance of the same miss.

---

## Round 6 Fix Verification

All six round-6 backend findings re-derived from the current source by execution.
All six hold.

**M1 — `fwInfo` routed through `_payload_mapping()`.** Fixed
(`client.py:944-968`, `door.py:1323-1345`, `door.py:1207-1226`). A scalar or list
`fwInfo` now produces two WARNING records and leaves `firmware_version`,
`hardware_version` and `hardware_info` at valid values (see the L1 transcript's
last block). The future is still resolved with the raw device value, so the
liberal contract for `send_message(..., notify=True)` is intact. Across the
16-case hostile-response sweep in `/tmp/ppdprobe/p4_sweep.py`, `fwInfo scalar`,
`fwInfo list` and `hw fields list-valued` all report **clean**.

**L1 — `MAX_RETAINED_PIECES` makes the 64 KiB char cap a memory cap.** Fixed.
50 scanners each dribbled to just under the cap with one never-terminated object
(`/tmp/ppdprobe/p7_r6verify.py`, RSS via `/proc/self/statm`):

```
chunk=1    retained=65529 chars  held=   68.2 KiB/conn  = 1.07x the 64 KiB cap  pieces=57
chunk=2    retained=65529 chars  held=    2.6 KiB/conn  = 0.04x the 64 KiB cap  pieces=61
chunk=4    retained=65529 chars  held=    0.6 KiB/conn  = 0.01x the 64 KiB cap  pieces=63
chunk=64   retained=65529 chars  held=    2.6 KiB/conn  = 0.04x the 64 KiB cap  pieces=64
```

Round 6 measured **26.7x** the cap at 2-byte chunks; it is now 1.07x at worst
(the sub-1x rows are allocator reuse, not a real saving). The coalesce boundary is
exact and the running-length invariant survives it
(`/tmp/ppdprobe/p15_framing_disp.py`):

```
3. after 64 feeds: pieces=64 (coalesce at >64)
   after one more feed: pieces=1 retained=65
   invariant _retained == sum(len(pieces)) holds
```

**L2 — `refresh_schedules()` rejects non-list payloads.** Fixed
(`door.py:1039-1049`). Every scalar shape now returns `[]` with a sanitized
warning and issues **zero** `GET_SCHEDULE` round trips — the string case used to
issue one per character (`/tmp/ppdprobe/p9_r6rest.py`, counting real requests
arriving at the fake device):

```
  schedules=3                        -> 0 schedules   GET_SCHEDULE round trips: 0
  schedules=1.5                      -> 0 schedules   GET_SCHEDULE round trips: 0
  schedules=True                     -> 0 schedules   GET_SCHEDULE round trips: 0
  schedules='0123456789'             -> 0 schedules   GET_SCHEDULE round trips: 0
  schedules={'0': {}}                -> 0 schedules   GET_SCHEDULE round trips: 0
  schedules=[0]                      -> 1 schedules   GET_SCHEDULE round trips: 1
  schedules=None                     -> 0 schedules   GET_SCHEDULE round trips: 0
  schedules=[]                       -> 0 schedules   GET_SCHEDULE round trips: 0
```

`logger.warning("Timeout fetching schedule %s", sanitize_text(idx))` is `%s` now,
so a string index no longer turns the warning into a logging-internal formatting
error.

**L3 — `compute_schedule_diff()` coerces indices and never emits null.** Fixed
(`schedule.py:745-758`). No input raises, and every emitted `index` is a real
`int` — asserted, not eyeballed:

```
  current indices ['0', 1]                 -> delete=[1] set-indices=[0]
  current indices [None, 2]                -> delete=[] set-indices=[2]
  current indices [[1]]                    -> delete=[] set-indices=[0]
  current indices [1.5]                    -> delete=[] set-indices=[1]
  current indices [-1]                     -> delete=[] set-indices=[0]
  current indices [9999]                   -> delete=[] set-indices=[0]
```

(`assert all(isinstance(e["index"], int) and not isinstance(e["index"], bool) ...)`
passed on every row.) Unusable indices are dropped with a warning instead of
raising `TypeError` out of a public export.

**L4 — `EventThrottle` quiet period, clock injection and interval ceiling.**
Fixed (`framing.py:96-233`). Driven with an injected clock
(`/tmp/ppdprobe/p6_throttle.py`):

```
1. frozen clock, 4096 events -> reports at: [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096] ... total 13
2. 400k events: reports=109  max gap=4096  (ceiling=4096)
3. after 1024 events + 60s quiet, next record reported: True
   subsequent reports within the new burst (offsets): [1, 3, 7, 15]
5. flush emits tail once: first=1 second=0 ; count=3 total=3
   after reset: count=0 total=0 ; next record reports=True
6. backwards clock, 50 events -> 6 log lines
```

Row 3 is the round-6 L4 gap closed: the fresh burst that used to emit **0** lines
for its first 1023 events now reports immediately and restarts its own doubling
schedule. Row 2 is the round-6 security informational: the gap is capped at 4 096
instead of growing to 2^k. Row 6 confirms a clock that goes backwards degrades to
the pure count schedule rather than firing every time.

Worst-case amplification through the time path measured directly: pacing events
at exactly one third of the quiet period gives 5 001 lines for 10 000 events — but
that is **~2 lines per 60 s per connection** in absolute terms, because the peer
has to space the events 20 s apart to get them. Bounded; not reportable.

**T1 — `_script_bool` in `scripting.py`.** Fixed (`scripting.py:646-674`, used at
`:426` for `hold` and `:473` for `enabled`, and by `_set_value`):

```
  'false'      -> False        '0'          -> False        'off'        -> False
  'no'         -> False        'disabled'   -> False        'enabled'    -> True
  'bogus'      -> False        None         -> False        []           -> False
```

`enabled: "0"` now produces a **disabled** schedule. Fail-closed on unknown
values, built on the library's `coerce_schedule_flag`, with the script-only
`enabled`/`disabled` spellings layered on top rather than widened into the
protocol parsers — the right side of the layer split.

**Round-6 security finding 1 — `FrameDispatcher`.** Fixed, and measured against
the exact attack it was built for. One 256 KiB read of `{}` into the real
client's `data_received` (`/tmp/ppdprobe/p8_burst.py`, `tracemalloc`):

```
one 256 KiB read of '{}' -> frames dispatched so far: inflight=64 backlog=131008 paused=True
transport.pause_reading() calls: 1
heap held right after the callback: 6.8 MB
live asyncio tasks: 65
after drain: backlog=0 inflight=0 paused=False resumes=1 loop-iterations=4096
tracemalloc peak over the whole episode: 8.2 MB
```

65 live tasks instead of 131 073, 8.2 MB peak instead of the ~145 MB round 6
measured — a 17x reduction — with the transport paused exactly once, resumed
exactly once, and the backlog fully drained. The residual 6.8 MB is the frame
list from that single read; `pause_reading()` correctly prevents a second one, so
the transient peak is bounded by the read size. The module docstring claims
exactly that and no more, so it is accurate.

---

## Areas Reviewed With No Findings

**`FrameDispatcher` — backlog, pause/resume, task accounting, teardown.** Fuzzed
7 000 randomized sequences across two harnesses, with `max_inflight` and
`pause_at` drawn from `{1,2,4,8,16,32,64,256}` and dispatch returning `None`
(unparseable frame) 20% of the time.

- *Frame conservation and the in-flight bound.* 3 000 sequences
  (`/tmp/ppdprobe/p2_disp_fuzz.py`): the sequence of frames handed to `dispatch`
  equalled the sequence submitted, **in order**, every time; `inflight` never
  exceeded `max_inflight` at any observation point; the backlog always drained to
  zero; `paused` and the transport's own flag were both False at the end. Output:
  `OK 3000 randomized dispatcher sequences; max observed inflight: 64`.
- *Transport churn.* 4 000 sequences (`/tmp/ppdprobe/p17_disp_none.py`) rotating
  between two transports and `None`, with `reset()` fired at random points:
  `inflight` always returned to 0, never went negative, and `paused` was always
  False afterwards. A `None` transport correctly disables flow control without
  stranding the `_paused` flag.
- *`reset()` deliberately leaves `_inflight` alone.* Confirmed correct: the
  running handlers still deliver their done-callbacks, which is what returns the
  count to zero. Verified over 120 real connect/refresh/disconnect cycles (below)
  that this never stalls a subsequent connection.
- *Pause/resume bookkeeping vs. a real transport.* `reset()` clears `_paused`
  without calling `resume_reading()`, so a caller who reset a *live* connection
  would leave the socket unread — demonstrated in the churn harness. It is not
  reachable in-tree: both call sites tear the socket down immediately
  (`client.disconnect()` closes the transport 30 lines later, `client.py:1481`;
  the simulator resets inside `connection_lost()`, `protocol.py:370`), and
  `reset()`'s docstring states the precondition ("the connection is over"). Also
  bounded by asyncio itself — I checked the real transport rather than assuming
  (`/tmp/ppdprobe/p18_asyncio_flow.py`): `pause_reading`, `resume_reading` and
  `resume_reading` after `close()` are all idempotent no-ops on
  `_SelectorSocketTransport`, so even a bookkeeping mismatch cannot raise. Not a
  finding; a one-line comment naming the precondition would be cheap insurance.
- *Interaction with the scanner and the overflow path.* Over 4 000 randomized
  streams (objects with `}{` inside string values, interleaved whitespace and
  garbage), the frames actually reaching `dispatch` were **identical** whether the
  stream was delivered in one `data_received` or dribbled 1–7 bytes at a time —
  17 993 frames, 0 divergences (`/tmp/ppdprobe/p15_framing_disp.py`). The
  round-6 T5 ordering still holds: a complete frame plus an unterminated 64 KiB
  object in one read dispatches **0** frames, closes the transport, and leaves the
  backlog empty — the overflow check runs before `submit()`, so no doomed task is
  ever created.
- *A stalled dispatcher.* Cannot occur through `_dispatch` returning `None`:
  `_pump`'s loop condition tests `_inflight`, which `None` does not increment, so
  a run of unparseable frames drains synchronously. The simulator's `drain()`
  test hook correctly yields when `_tasks` is empty but the backlog is not (the
  two done-callbacks are separate `call_soon` handles), so it cannot spin.

**`EventThrottle`.** Covered under round-6 L4 above; the mechanism, the injected
clock, `flush()`/`reset()` and the per-connection scoping on both sides are all
correct. Worst-case time-path amplification measured at ~2 lines/minute/connection.
Thread safety remains a non-issue: both holders are event-loop-only and neither
`record()` nor `flush()` contains an await point.

**The wire boundary (`SCHEDULE_WIRE_TO_DEVICE` / `_FROM_DEVICE`).** Correct, and
correctly *not* symmetric. Verified end to end through a real simulator over a
real socket (`/tmp/ppdprobe/p16_wire.py`):

```
client->device to_dict : {"index": 1, "enabled": false, "daysOfWeek": [1,0,1,0,1,0,1], "inside": true, ...}
device->client to_dict : {"index": 1, "enabled": "0",   "daysOfWeek": [1,0,1,0,1,0,1], "inside": true, ...}
differing fields       : {'enabled'}
  SET enabled=True  -> simulator stored enabled=True  -> GET back enabled=True   window 06:30-22:15  ok=True
  SET enabled=False -> simulator stored enabled=False -> GET back enabled=False  window 06:30-22:15  ok=True
  diff(parsed round-trip , desired) -> delete=[] set=0  (no-op expected)
  diff(raw device dicts  , desired) -> delete=[] set=0  (no-op expected)
```

Exactly one field differs between the two directions, which is what the constants
document. The reverted JSON-boolean `enabled` round-trips correctly in both
directions, and — the thing that actually matters operationally — the diff is a
**no-op across the spelling mismatch**, both against parsed schedules and against
raw device dicts, so incremental sync does not degenerate into a full
`SET_SCHEDULE` sweep against a rate-limited, single-connection device. I did not
propose any change to what is sent on the wire.

**Stability / leak soak.** 120 full `connect() → refresh() → refresh_schedules()
→ open() → close() → disconnect()` cycles against the real simulator in one
process (`/tmp/ppdprobe/p11_soak.py`):

```
iter  tasks  rssKiB  fds  sim.protocols  aux  retired  sensortimers
  20      2   32668   10              1    0        0             0
  40      2   32720   10              1    0        0             0
 120      2   32792   10              1    0        0             0
```

Flat task count, flat fd count, ~1.2 KiB/iteration RSS drift (allocator noise),
and the engine's `_aux_tasks` / `_retired` / `_sensor_timers` sets all empty. The
`sim.protocols == 1` reading is the just-closed connection awaiting its
`connection_lost`, not an accumulation — it does not grow.

**Hostile-peer survivability, both directions.** 200 connections of random bytes
drawn from a JSON-flavoured alphabet into the real simulator
(`/tmp/ppdprobe/p12_e2e_fuzz.py`): server still serving, `protocols=0`, 848 KiB
RSS delta, **0 fd delta, 0 unhandled loop exceptions**, and a real
`PowerPetDoor` connected afterwards and refreshed successfully. 300 sessions of
random junk frames from a hostile device into the real client
(`/tmp/ppdprobe/p13_hostile_device.py`): 1 480 KiB RSS delta, 1 task, 0 unhandled
loop exceptions, and the only exception types escaping the documented API were
`TimeoutError` — plus the `battery.charging` `TypeError` reported as M1.

**Write-side backpressure (simulator).** A client that issues valid
`GET_SETTINGS` commands and never reads (`/tmp/ppdprobe/p19b.py`): 5.0 MiB of
requests accepted by the kernel produced a **1 048 864-byte** write buffer against
`MAX_WRITE_BACKLOG = 1048576`, the transport already closing, 2 380 KiB of daemon
RSS, and full cleanup (`protocols=0`) once the peer closed. Bounded, and the
dispatcher does not compound it — `backlog=0 inflight=0 paused=False` throughout,
because the requests arrive slower than they are handled. An earlier version of
this probe using a *blocking* socket hung on `sendall`, which is the read-side
backpressure working as intended.

**`schedule.py` beyond the wire tables.** `compress_schedule()` still validates
every entry up front (`_require_complete_entry`), reads day flags through
`make_bool` rather than truthiness, keeps booleans in memory and applies the 1/0
spelling once at the boundary, and does not mutate its input.
`schedule_entry_content_key()` normalizes every flag through the shared coercers.
`compute_schedule_diff()`'s new-index allocation correctly accounts for slots
already handed to earlier brand-new entries in the same loop (the round-6
test-fanatic M1 fix), so two new entries can no longer collide on one index.

**`tz_utils.py`, `sanitize.py`, `const.py`.** Unchanged in substance and no
findings. Cache init is still double-checked under a `threading.Lock`, all
blocking I/O is inside `to_thread`, `get_available_timezones()` returns a copy,
and `parse_posix_tz_string` sanitizes the device string before logging.
`sanitize_text`'s new `limit` parameter truncates **before** the regex runs, so
the per-frame log sites cost a bounded amount of both volume and regex work.

**Simulator engine and command handlers.** Re-read; the round-5/6 reasoning holds
line for line (single `_run` owner via `_dispatch_depth`/`_defer_sequence`,
deferred intents rather than resolved states, done-callback discarding for
`_retired`/`_aux_tasks`, deadline-based `_hold_open` with `MIN_BLOCKED_RECHECK`
as a floor). Every `SET_*` still validates before mutating. Nothing new to report,
and nothing I could make misbehave through the fuzzers above.
