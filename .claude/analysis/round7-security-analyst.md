# Security Analyst Analysis — Round 7

Commit `a0194bd` ("Round 6 fixes; revert the enabled wire change; layer the wire
boundary"), working tree clean, Python 3.13.13, `.venv` as locked.

Scope note carried from the brief: the device protocol is plaintext JSON over TCP
with no authentication or encryption **by device design**. Nothing below asks for
TLS on the device protocol, and nothing below asks the code to *reject* input a
real door could legitimately send. Every recommendation is of the form "accept the
same bytes, bound the resources spent on them, and never announce an action you do
not perform".

Every number in this report came out of a harness that was actually executed on
this machine at this commit. Harnesses were written to `/tmp/r7sec/`, run, and
deleted; every spawned daemon was terminated. No repository file was modified.

---

## Summary

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 2 |
| Informational | 1 |

- **Medium 1** — `FrameDispatcher` bounds concurrent handler *tasks*, not the
  *work admitted per read*. One transport-sized read still admits 131,008
  backlogged frames (512× `MAX_FRAME_BACKLOG`) and 6.80 MB (×25.9), and the
  `data_received` callback that does it never yields: 89 ms for `{}`, 307 ms for
  `{x}`. On a real daemon a **one-shot** 16.78 MB burst across 64 sockets buys
  +228 MB RSS and blocks `ctl status` for 14.9 s, still degraded at t+90 s.
- **Low 2** — Round 6's new write ceiling logs "dropping the connection" once per
  message, unthrottled, and then does not drop it: `transport.close()` never
  completes while the peer refuses to read, so the connection, its ~1 MB buffer
  and its slot in `DoorSimulator.protocols` are held for the life of the daemon,
  `ctl status` keeps reporting the client, and every later broadcast emits another
  ERROR.
- **Low 3** — Round 6 finding 2 throttled and length-capped three per-frame log
  sites; a fourth in the **shipped library** (`client.py:1816`) and three in the
  simulator were missed. Measured ×6.64 write amplification and one unthrottled
  WARNING per frame, against ×0.04 and zero unthrottled records at the sites that
  were fixed.
- **Informational 4** — `FrameDispatcher.reset()` clears `_paused` without calling
  `resume_reading()`. Harmless at both current call sites; a latent hazard.

The round-6 fixes themselves all verified. See **Round 6 Fix Verification**.

---

## Findings

### 1. [Medium] The dispatch bound is on concurrent *tasks*, not on *work admitted per read*: one 256 KiB read still admits 131,008 frames (512× `MAX_FRAME_BACKLOG`), 6.80 MB (×25.9), in a callback that never yields — a one-shot 16.78 MB burst costs a real daemon +228 MB and 14.9 s of control-plane denial

**Files:**
- `src/powerpetdoor/framing.py:488` — `MAX_INFLIGHT_FRAMES = 64`
- `src/powerpetdoor/framing.py:492` — `MAX_FRAME_BACKLOG = 256`
- `src/powerpetdoor/framing.py:574` — `self._backlog.extend(frames)` (the whole
  read is queued *before* `_pump()` and `_update_flow()` are reached)
- `src/powerpetdoor/framing.py:588-594` — `_pump()`, whose `while` loop has no
  yield and whose `max_inflight` guard does not apply to frames for which
  `_dispatch` returns `None`
- `src/powerpetdoor/framing.py:447` — `chunk = data[consumed:i]`, the allocation
  site of the frames that are queued
- `src/powerpetdoor/client.py:1703` and
  `src/powerpetdoor/simulator/protocol.py:433` — the two `submit()` call sites

Round 6 fixed the task explosion (verified below: 131,072 tasks → 64). What it did
not do is bound the *admission* of work. `FrameScanner.feed()` frames the entire
read and returns every frame in one list; `submit()` extends the deque with all of
them and only then consults `pause_at`. So `MAX_FRAME_BACKLOG = 256` is advisory —
by the time it is read, the backlog is already whatever one read produced. And
`_pump()`'s `_inflight < _max_inflight` guard only counts frames that produce a
task, so an unparseable frame is dispatched, counted as nothing, and the loop
continues: the whole read drains synchronously inside `data_received`, with no
yield to the event loop.

The round-6 report described the `{x}` case as "inherent CPU over ~8M tiny frames".
It is not spread-out CPU — it is a single unyielding callback, and the `{}` case
(legal JSON, produces a real task) is the one that costs memory.

**Reproduction A — where the bound actually lands, with allocation attribution**

```python
# /tmp/r7sec/h3_backlog_attrib.py  (shipped DoorSimulatorProtocol, FakeTransport
# counting pause_reading; tracemalloc around one asyncio-sized read)
READ = 256 * 1024                      # asyncio _SelectorSocketTransport.max_size
p = DoorSimulatorProtocol(DoorSimulatorState()); p.connection_made(t)
payload = b"{}" * (READ // 2)
snap0 = tracemalloc.take_snapshot(); p.data_received(payload)
snap1 = tracemalloc.take_snapshot()
```

```
MAX_INFLIGHT_FRAMES = 64
MAX_FRAME_BACKLOG   = 256  (documented pause threshold)
bytes in one read   = 262144
backlog after read  = 131008   (512x the documented threshold)
inflight            = 64, paused=True, pause_reading x1

tracemalloc delta   = 6.80 MB for 262144 bytes on the wire (25.9x amplification)
       5.63 MB  framing.py:447      <- chunk = data[consumed:i]   (the frames)
       1.08 MB  framing.py:574      <- self._backlog.extend(frames)
       0.02 MB  simulator/protocol.py:453
```

**Reproduction B — the callback does not yield** (fresh scanner + dispatcher per
trial, `gc.collect()` before each, 5 trials, `/tmp/r7sec/h8_isolate.py`):

```
{x}    one 256 KiB read: callback blocked min  298.8 ms  median  307.2 ms  max  359.8 ms   backlog left 0
{}     one 256 KiB read: callback blocked min   69.6 ms  median   89.3 ms  max  101.3 ms   backlog left 131008
{,}    one 256 KiB read: callback blocked min  294.3 ms  median  294.3 ms  max  301.3 ms   backlog left 0
{"a"}  one 256 KiB read: callback blocked min  167.4 ms  median  170.1 ms  max  172.2 ms   backlog left 0
```

`backlog left 0` for `{x}` is the point: the pause threshold never engages,
because the entire read is consumed inside the callback. Re-measured with a heap
already dirty from a preceding read (`/tmp/r7sec/h7_callback_block.py`), the same
callback blocked **981.0 ms** (client) / **914.9 ms** (simulator); the 300 ms
figures above are the conservative isolated ones.

Full drain cost of one read, including the tasks (`/tmp/r7sec/h5_drain_cost.py`):

```
one 256 KiB read {}      262144 B in ->  131072 frames, drained in 0.820 s  (3.13 us/wire byte, 6.26 us/frame)
one 256 KiB read {x}     262143 B in ->   87381 frames, drained in 0.275 s  (1.05 us/wire byte, 3.15 us/frame)
one 256 KiB read {cmd}   262141 B in ->   23831 frames, drained in 0.239 s  (0.91 us/wire byte, 10.03 us/frame)
```

**Reproduction C — real daemon, one-shot burst, then silence**
(`/tmp/r7sec/h4_burst_hold.py`: `ppd-simulator --daemon`, N sockets, each writes
**one** 256 KiB burst of `{}` and never writes again; sockets stay open):

| conns | attacker wire | attacker effort | peak daemon RSS | ×wire | `ctl status` max |
|---|---|---|---|---|---|
| 8 | 2.10 MB | 0.00 s | 32.8 → 60.2 MB (**+27.4 MB**) | ×13.1 | 296 ms |
| 64 | 16.78 MB | 0.01 s | 32.8 → 260.8 MB (**+228.0 MB**) | ×13.6 | **14,884 ms** |

```
attacker sent 16.78 MB total (64 conns x 1 x 256 KiB) in 0.01s, then went silent
peak daemon RSS 260.8 MB (delta +228.0 MB) = 13.6x the bytes sent
ctl status: max 14884 ms, samples over 500 ms: 10 (last at t+29.2s)
```

Extended observation to 90 s (`/tmp/r7sec/h4b.py`, same one-shot burst):

```
ctl status: max 2587 ms, samples over 500 ms: 40 (last at t+89.2s)
settled RSS 215.1 MB (delta +182.4 MB), ctl status 1616.4 ms
```

The daemon has still not recovered 90 seconds after an attacker that spent 0.01 s
and 16.78 MB. Sustained floods scale the same way
(`/tmp/r7sec/h2_daemon_flood.py`, 15–25 s): 8 conns +54.1 MB / 175 ms; 32 conns
+110.3 MB / 782 ms; 64 conns +225.0 MB / 1,867 ms. The door port stays functional
throughout (`door responds=True`), so this is denial by latency and heap, not a
crash.

**Reproduction D — the shipped client has the same shape.** One 256 KiB read into
`PowerPetDoorClient` (`/tmp/r7sec/h1_dispatch_bound.py`):

```
bytes in       : 262144
tasks created  : 64
dispatcher     : inflight=64 backlog=131008 paused=True
transport      : pause_reading x1 resume_reading x0
RSS peak delta : 7.9 MB
```

**Attack scenario.** The simulator daemon binds `0.0.0.0:3000` by default. Any host
on the LAN opens N sockets, writes 256 KiB of `{}` on each — legal JSON, two bytes
per frame, nothing malformed, no protocol violation — and stops. 16.78 MB of
traffic delivered in 10 ms costs the daemon 228 MB of heap and makes the operator's
control channel unusable for ~15 s and degraded for over 90 s. Repeating the burst
every 30 s holds that state indefinitely for a negligible bandwidth cost, and the
per-connection cost is a constant so the total is limited only by the connection
count, which nothing caps. For the **shipped library**, the same construction from
a hostile or compromised door — or from anything that can answer on the LAN, since
the protocol has no authentication by design — buys a 89–307 ms unyielding stall of
the *host application's* event loop per 256 KiB, which for the Home Assistant
deployment target is the whole instance, not just the pet-door integration.

**Why Medium and not Low:** it is remotely reachable with no privilege, no
interaction and no malformed input; the amplification is ×13–26 in memory and
~3 µs of single-threaded event-loop time per wire byte; the announced bounds
(`MAX_FRAME_BACKLOG = 256`) are off by 512×; and it lands in the shipped library
as an unyielding callback, which is a latency property the round-6 residual note
did not capture.

**Why not High:** availability only. No confidentiality, integrity, state
corruption or privilege impact. Memory is genuinely released — the dispatcher
backlog is cleared on `connection_lost` and RSS returns after the drain — so it is
a burst-and-hold problem, not a leak, and the simulator is a documented
development tool.

**Recommendation** — bound *admitted work per callback*, in bytes, and defer rather
than reject. None of this refuses anything a real door could send; every byte is
still framed and handled, just spread across event-loop iterations.

1. Cheapest and most valuable first: make `_pump()` yield. Dispatch at most
   `max_inflight` frames per invocation and, if the backlog is still non-empty and
   nothing is in flight (the unparseable-frame path), re-arm with
   `loop.call_soon(self._pump)`. That alone removes the 89–307 ms unyielding
   callback on both sides.
2. Give `FrameScanner.feed()` an output budget in bytes (e.g. `MAX_BUFFER_SIZE`).
   When it is spent, stop scanning and retain the un-scanned tail. **Critical
   detail:** track that tail separately from the un-parsed remainder, so the
   64 KiB overflow rule keeps measuring only "bytes with no complete message in
   them". Folding a deferred tail into `_retained` would trip the overflow
   disconnect and would be a regression — it would drop connections a real device
   could legitimately drive.
3. Have `data_received` pause reading whenever the scanner holds a deferred tail,
   and have `FrameDispatcher` re-drive `feed()` from its done-callback, resuming
   only when the tail and the backlog are both empty.
4. Still open from round 6 recommendation 4: cap concurrent door connections in
   the simulator. The real device accepts one client at a time, so a small ceiling
   is *better* fidelity as well as defence in depth, and it turns the
   linear-in-sockets rows of Reproduction C into a constant.

---

### 2. [Low] The round-6 write ceiling logs "dropping the connection" once per message, unthrottled, and then does not drop it: `close()` never completes against a peer that will not read, so the connection is held for the life of the daemon and every later broadcast emits another ERROR

**Files:**
- `src/powerpetdoor/simulator/protocol.py:490-497` — the ceiling check, its
  unthrottled `logger.error`, and `self.transport.close()`
- `src/powerpetdoor/simulator/protocol.py:429` — the framing-overflow drop, same
  `close()` shape
- `src/powerpetdoor/simulator/server.py:144-148` — `handle_disconnect`, reachable
  only from `connection_lost`, which is what never arrives

The ceiling itself works: memory is bounded (verified below). Two defects sit on
top of it.

`_send()` re-evaluates the condition for every subsequent message and re-emits a
byte-identical ERROR each time, with no `EventThrottle` — the exact pattern round 6
finding 2 removed from the three sibling sites in this same class.

And `transport.close()` on an asyncio transport with a non-empty write buffer only
sets `_closing` and removes the reader; `connection_lost` is deferred until the
buffer drains. A peer holding a zero TCP window never lets it drain. So the
protocol object, its scanner, its dispatcher and the ~1 MB buffer stay live, the
protocol is never removed from `DoorSimulator.protocols`, `ctl status` keeps
reporting the client as connected, and every subsequent broadcast calls `_send()`
on it and logs another ERROR.

**Reproduction A — in-process, real socketpair** (`/tmp/r7sec/h14_close_deferred.py`;
`connection_lost` wrapped to record whether it ever fires):

```
MAX_WRITE_BACKLOG = 1048576
fed 3500 valid commands (0.16 MB); peer never read
write buffer now 1048616 B, ERROR records so far: 296
is_closing() after the ceiling fired: True
300 more messages after the 'dropping the connection' ERROR -> 300 MORE identical ERROR records (unthrottled: 1 per message)
after 7.5s: connection_lost fired = False, write buffer still 1048616 B, protocol still live = True
distinct ERROR messages: 1 of 596
sample: 'Simulator: client is not reading its responses (1048616 bytes buffered); dropping the connection'
```

Exactly 300 more messages produced exactly 300 more records: one per message,
unthrottled, all byte-identical.

**Reproduction B — real daemon** (`/tmp/r7sec/h12_ceiling_logspam.py`: attacker
sends valid `GET_SETTINGS` with `SO_RCVBUF=8192` and never reads):

```
attacker wrote 3.49 MB of VALID GET_SETTINGS, never read a byte
daemon RSS 32.8 -> 34.8 MB (+2.0 MB) [round 6, before MAX_WRITE_BACKLOG: +36.1 MB for 1.50 MB]
"client is not reading its responses" ERROR records: 1283
daemon log grew 165668 B for 3490809 B on the wire (x0.047); all records are byte-identical: 268 distinct
  t+ 1s after the ceiling fired: OK: Current State:\n  Clients: 1 client\n  Door: DOOR_CLOSED...
  t+ 5s after the ceiling fired: OK: Current State:\n  Clients: 1 client\n  Door: DOOR_CLOSED...
  t+15s after the ceiling fired: OK: Current State:\n  Clients: 1 client\n  Door: DOOR_CLOSED...
  t+30s after the ceiling fired: OK: Current State:\n  Clients: 1 client\n  Door: DOOR_CLOSED...
  after the attacker closes:    OK: Current State:\n  Clients: none\n  Door: DOOR_CLOSED...
```

**Reproduction C — the poisoning is permanent** (`/tmp/r7sec/h15_stalled_poison.py`:
after the ceiling fires the attacker goes completely idle and only ordinary
operator commands are issued over `ctl`):

```
attacker sent 3.49 MB, never read; ceiling ERRORs so far: 4694
daemon says: OK: Current State:\n  Clients: 1 client\n  Door: DOOR_CLOSED
after 8 ordinary operator commands (attacker idle): 6 NEW unthrottled ERROR records
after 5 more (still idle):                          6 NEW unthrottled ERROR records
daemon still says: OK: Current State:\n  Clients: 1 client\n  Door: DOOR_CLOSED
```

Twenty such connections held simultaneously (`/tmp/r7sec/h13_ceiling_cycles.py`):
`daemon RSS 32.9 -> 40.5 MB (+7.6 MB)`, `Clients: 20 clients`.

**Attack scenario.** An unauthenticated LAN peer connects to the simulator, sends
~3.5 MB of entirely valid `GET_SETTINGS`, stops reading, and holds the socket. It
permanently occupies a connection slot and ~1 MB of daemon write buffer, the
daemon's log is poisoned at ERROR for the rest of its life at the rate of ordinary
operator activity — fanned out to every parked `ctl -i` session by
`_ControlLogHandler` — and the operator's own `status` command contradicts the
daemon's own ERROR by continuing to report the client as connected. An operator
following the log will believe the connection was dropped when it was not.

**Why Low and not Medium:** memory is genuinely bounded (+2.0 MB for 3.49 MB, down
from round 6's +36.1 MB for 1.50 MB), byte-level log amplification is only ×0.047,
and there is no confidentiality or integrity impact. The substance is a false
operational signal, a permanently poisoned ERROR stream, and an indefinitely held
connection slot — all in a development tool.

**Recommendation:**

1. Latch the drop. Set a `_dropped` flag (or clear `self.transport`) when the
   ceiling fires, and have `_send()` return immediately thereafter, so neither
   queued frames nor later broadcasts re-check and re-log.
2. Use `transport.abort()` instead of `transport.close()`. `abort()` discards the
   buffer and delivers `connection_lost` immediately, which is what removes the
   protocol from `DoorSimulator.protocols`, clears the dispatcher backlog, frees
   the buffer and makes `ctl status` truthful. This is a declared protocol
   violation, so discarding the unsent tail is correct. The same argument applies
   to the framing-overflow drop at `protocol.py:429`.
3. Wrap the remaining ERROR in an `EventThrottle`, like the three siblings already
   held by this class. With (1) in place it should fire once per connection, but
   the throttle is the invariant, not the accident.

---

### 3. [Low] Round 6 finding 2 left four per-frame, unthrottled, length-unbounded log sites — one of them in the shipped library: ×6.64 write amplification and one WARNING per frame, against ×0.04 and zero unthrottled records at the sites that were fixed

**Files:**
- `src/powerpetdoor/client.py:1816` — `_LOGGER.warning("Error reported by device:
  %s", json.dumps(msg))` — **the shipped library**; fires for every frame carrying
  a `CMD` whose `success` is not `"true"`, i.e. the device's own normal error
  envelope
- `src/powerpetdoor/simulator/protocol.py:576-578` — `Simulator: Rejected %s: %s`,
  once per rejected `SET_*`
- `src/powerpetdoor/simulator/protocol.py:739` — `Simulator: Rejected schedule: %s`
- `src/powerpetdoor/simulator/protocol.py:781` — `Simulator: Rejected schedule
  list: %s`

All four fire once per *frame*, so they are limited by the peer's byte rate rather
than its packet rate — the exact criterion round 6 used. None carries an
`EventThrottle`. None applies `MAX_LOGGED_LENGTH`, so the record grows with the
attacker-chosen payload without bound.

**Reproduction** (`/tmp/r7sec/h17_error_reported.py` and
`/tmp/r7sec/h18_small_reject.py`: shipped `PowerPetDoorClient` /
`DoorSimulatorProtocol`, a `StreamHandler` with the same
`"%(asctime)s [%(levelname)s] %(message)s"` format the simulator installs, one
`data_received` of packed frames, drained):

```
SHIPPED CLIENT - client.py:1816 (per-frame, no throttle, no length cap)
  client   {"CMD":"a","success":"false"}      wire    580000 B -> log   1860000 B (x  3.21), 20000 unthrottled WARNING records
  client   one 60 KB reason field             wire    240184 B -> log    240428 B (x  1.00), 4 unthrottled WARNING records
  client   {"CMD":"a"} (success absent)       wire    220000 B -> log   1460000 B (x  6.64), 20000 unthrottled WARNING records

for comparison, the sites round 6 DID throttle:
  client   {x} (bad_frames, throttled)        wire    180000 B -> log      6624 B (x  0.04), 0 unthrottled WARNING records
  client   {} (bad_messages, throttled)       wire    120000 B -> log      4902 B (x  0.04), 0 unthrottled WARNING records
```

```
SIMULATOR - per-frame rejection sites, smallest triggering frames
  SET_HOLD_TIME holdTime:[]   (protocol.py:577)  frame 40 B  wire 320000 B -> log 824000 B (x2.58), 8000 records
  DELETE_SCHEDULE index:{}    (protocol.py:577)  frame 39 B  wire 312000 B -> log 816000 B (x2.62), 8000 records
  SET_SCHEDULE {"index":[]}   (protocol.py:739)  frame 49 B  wire 392000 B -> log 832000 B (x2.12), 8000 records
```

One record per frame in every case: 20,000 frames → 20,000 records; 8,000 → 8,000.
The 60 KB `reason` row is the length half: a single frame produced a single
~60 KB record, and the frame may be anything up to the 64 KiB framing cap.

**This is not an escape-injection vector.** `json.dumps` with the default
`ensure_ascii=True` escapes control characters inside strings, and the simulator
sites route through `sanitize_log_text`. Measured across 8,000 hostile frames
through the client at DEBUG and 4,000 through a live daemon
(`/tmp/r7sec/h16_controls.py`, `/tmp/r7sec/h10_r6_verify.py`): **0 raw ESC, 0 raw
BEL, 0 tracebacks, 0 loop exception-handler hits, `data_received` never raised.**
The issue is log volume and record size only.

**Attack scenario.** A hostile door — or anything answering on the LAN, the
protocol being unauthenticated by design — replies to the client with packed
11-byte `{"CMD":"a"}` envelopes. Every one produces a WARNING record in the host
application's log at ×6.64 the wire bytes, with no throttle and no ceiling, filling
the disk of whatever runs the integration. The simulator half is the same at ×2.1–2.6
with the added `_ControlLogHandler` fan-out to every parked `ctl -i` session.

**Why Low:** an order of magnitude below the ×46.3 that round 6 rated Low, and
strictly a volume/disk-pressure issue with no injection, corruption or
confidentiality impact. It is reported because it is the same defect class round 6
declared closed, and because `client.py:1816` is in the shipped library and is
reachable by the device's *ordinary* error envelope, not only by malformed input.

**Recommendation:** give each of the four sites an `EventThrottle` and
`sanitize_text(..., MAX_LOGGED_LENGTH)`, exactly as `_bad_frames` /
`_bad_messages` / `_unknown_commands` already have. `EventThrottle` reports the
first occurrence unconditionally, so a genuine single device error is still logged
immediately and in full context — nothing the device sends is refused or hidden,
only repetition is batched.

---

### 4. [Informational] `FrameDispatcher.reset()` clears `_paused` without resuming the transport

**File:** `src/powerpetdoor/framing.py:577-586`

`reset()` sets `self._paused = False` and `self._transport = None` but never calls
`transport.resume_reading()`. The flag and the transport's real state diverge.

**Reproduction** (`/tmp/r7sec/h9_dispatcher_adversarial.py`, T1, transport counting
`pause_reading`/`resume_reading`):

```
--- T1 pause/resume balance across drain, reset, and transport swap ---
paused=True A.pause=1 A.resume=0 backlog=96
drained: paused=False A.pause=1 A.resume=1 inflight=0 backlog=0  -> balanced=True
refilled: paused=True A.pause=2 A.resume=1
after reset(): paused=False inflight=4 backlog=0 A.pause=2 A.resume=1 -> transport A left PAUSED (never resumed): True
new transport B: B.pause=0 B.resume=0 inflight=4 backlog=2
final: inflight=0 (must be 0), backlog=0
```

**No exploit path exists today**, and I could not construct one. Both callers
(`client.py:1452` inside `disconnect()`, which closes the transport 30 lines later;
`protocol.py:370` inside `connection_lost()`, where the transport is already gone)
discard the transport in the same breath, so the un-resumed transport is never read
from again by anyone. Reported only because the class is reusable and a future
caller that resets a dispatcher on a *live* connection would silently stop reading
it forever.

**Recommendation:** in `reset()`, call `transport.resume_reading()` before dropping
the reference when `self._paused` is set (asyncio's selector transports make this a
no-op on an already-closing socket, so it is free), or document in the docstring
that `reset()` may only be called when the transport is being discarded.

---

## Round 6 Fix Verification

All four round-6 items were re-run at this commit. Every one holds.

**Finding 1, primary — one `asyncio.Task` per framed message, uncapped.** Fixed.
One 256 KiB read of `{}` into the shipped client
(`/tmp/r7sec/h1_dispatch_bound.py`):

| | round 6 (before) | round 7 (measured now) |
|---|---|---|
| tasks created from one 256 KiB read | 131,072 | **64** |
| live heap | 135.8 MB (client) / 145.3 MB (sim) | **7.9 MB** RSS peak |
| `pause_reading()` | never called | **1** |
| after drain | — | `inflight=0 backlog=0 paused=False`, `resume_reading x1`, tracked `_tasks` 0 |

Accounting returns exactly to zero and the transport is resumed exactly once. The
residual — what the 64 does *not* bound — is Finding 1 above.

**Finding 1, secondary — no write ceiling on the door transport.** Fixed for
memory. A peer issuing valid commands and never reading:
round 6 measured 1.50 MB of requests → **+36.1 MB** of daemon heap, unbounded and
held for the life of the socket. Now (`/tmp/r7sec/h12`, `/tmp/r7sec/h11`):
3.49 MB of requests → **+2.0 MB**, the ceiling fires at 1,048,616 B buffered, and
growth stops. The ceiling's *reporting* and its *un-performed drop* are Finding 2.

**Finding 2 — unthrottled per-frame log sites.** Fixed at the three sites that were
changed. Against a live daemon, wire bytes in vs. daemon log bytes out
(`/tmp/r7sec/h10_r6_verify.py`, 5 s of sustained flood + 3 s tail):

| attack | attacker wire | daemon log | amplification | round 6 |
|---|---|---|---|---|
| packed `{x}` (unparseable) | 4.98 MB | 0.086 MB | **×0.017** | ×46.3 |
| packed `{}` (legal, empty) | 3.93 MB | 0.000 MB | **×0.0000** | — |
| packed `{"cmd":"a"}` (unknown) | 2.88 MB | 0.008 MB | **×0.0027** | ×3.6 |
| garbage (non-JSON) | **1071.38 MB** | 0.002 MB | ×0.0000 | ×115 (r5) |
| non-ASCII bytes | 6.55 MB | 0.001 MB | ×0.0002 | ×247 (r5) |

A gigabyte of garbage now buys two kilobytes of log. The daemon log shows the
doubling schedule working as designed
(`Simulator: 1306624 JSON parse error(s) (3919872 bytes) on this connection`), and
the per-frame detail rides the same schedule. The four sites the fix did not reach
are Finding 3.

**Finding 3 — `EventThrottle`'s interval doubled without a ceiling.** Fixed, and the
new quiet period cannot be turned into an amplifier
(`/tmp/r7sec/h9_dispatcher_adversarial.py`, T4, injected deterministic clock):

```
(a) 10,000,000 events in zero simulated time -> 2453 log lines (ratio 1:4076)
(b) 1000 events paced one per quiet period (60s) over 16.7h -> 1000 log lines
    (unthrottled would be 1000; amplification 1.00x)
(c) after a 5,000,000-event burst (1232 lines), a single genuine event 61 s later
    is reported immediately: True
(d) with the schedule at its 4096 cap, the worst-case delay before the next summary
    is 1216 events (message still carries the running count/total)
```

(a) is `MAX_THROTTLE_INTERVAL` holding at exactly 4096. (b) is the attack I went
looking for — pacing events to land on the quiet path every time — and it produces
**no amplification at all** (1.00×), because the time path can fire at most once per
quiet period. (c) is `THROTTLE_QUIET_PERIOD` doing its job: a fresh burst after
silence is never invisible. (d) bounds the worst-case reporting delay, and the
summary carries running count and total so nothing is lost.

**Framing coalesce vs. the 64 KiB cap.** Holds; no smuggling
(`/tmp/r7sec/h9_dispatcher_adversarial.py`, T5):

```
overflow at 65537 retained chars (cap 65536); buffer cleared -> len=0
single 1 MiB open object: overflow=True, retained after=0
single 1048584 char complete frame in one feed: frames=1, len(frames[0])=1048584,
    overflow=False   (accepted by design: bounded by the transport read size)
```

The last row is correct behaviour, not a defect: a complete object larger than the
retention cap arriving inside one read is *accepted*. Refusing it would narrow what
we accept from the device, and the memory is already bounded by the read size.

---

## Areas Reviewed With No Findings

- **`FrameDispatcher` cannot be wedged, starved or made to leak by any input I could
  construct.** (`/tmp/r7sec/h9_dispatcher_adversarial.py`.)
  *Pause/resume balance:* across a normal fill-and-drain, `A.pause == A.resume`
  exactly. *Teardown with items in flight:* `reset()` clears the backlog and leaves
  `_inflight` alone by design; the cancelled handlers still deliver their
  done-callbacks and the count returns to 0 without ever going negative — measured
  `final: inflight=0, backlog=0`. *Reset landing before the done-callbacks, then an
  immediate resubmit on a new transport:* the stale `_inflight` briefly holds the
  slots (`inflight=4 backlog=3`, new frames not dispatched), and it self-heals one
  loop iteration later (`inflight=3 backlog=0`). *Permanent pause:* achievable only
  with a handler that never returns; `_dispatch` is `_dispatch_frame` on both sides
  and every handler awaits bounded work (`process_message` awaits at most
  `MINIMUM_TIME_BETWEEN_MSGS`; every `CommandRegistry` handler is non-blocking), so
  there is no such handler. *Negative or drifting `_inflight`:* impossible — every
  counted task delivers exactly one done-callback.
- **`EventThrottle` cannot be used to keep a channel silent or to amplify.** Covered
  in the verification section: 1:4,076 at the cap, 1.00× under quiet-period pacing,
  immediate report for a fresh event after a 5,000,000-event burst, worst-case delay
  bounded at 4,096 events with running totals carried in every summary.
- **Hostile wire input against the shipped client.** 8,000 randomized frames
  (6,237,753 B: ESC/BEL/NUL, 5,000-char strings, `inf`/`nan`/10^40, lists and dicts
  in `CMD`/`msgID`/`settings`/`fwInfo`/`schedules`/`sensorState`, valid command
  names with hostile payloads) fed through `data_received` at DEBUG:
  `data_received raised: None`, **0** loop exception-handler hits, **0** tracebacks,
  **0** raw ESC, **0** raw BEL. `_LOGGER.exception` at `client.py:1802` was not
  reached by any of them — every handler's payload access is guarded.
- **Hostile wire input against a live daemon.** 4,000 randomized frames
  (1,753,946 B, same generator, aimed at `cmd`/`config`/`PING`/`index`/`schedule`/
  `schedules`/`tz`/`holdTime`/voltages/`notifications`): **0 tracebacks, 0 "message
  handler task failed", 0 raw ESC, 0 raw BEL.** Control characters appear escaped
  (`Simulator: Unknown command: \x07bell`). The round-4 unhashable-key and
  schedule-index fixes still hold.
- **Control-channel script-path restriction.** Against a live daemon, all rejected
  with the same message and no path echoed back unsanitized:
  `run ../../etc/passwd`, `run /etc/passwd`, `run .hidden`, `run ./basic_cycle`,
  `run scripts\basic_cycle`, `run ../scripts/basic_cycle`,
  `run basic_cycle/../../../etc/passwd`. `run <ESC>[31mred` returns
  `Unknown script: \x1b[31mred` — escaped, not raw. `load /etc/shadow` is not a
  command.
- **`tz_utils` traversal.** `get_posix_tz_string` returns `None` (never raises) for
  `../../../../etc/passwd`, `/etc/passwd`, `..`, `.`, `""`,
  `America/../../etc/passwd`, 5,000 `A`s, `\x00`, `a/b/c/d/e`,
  `zoneinfo.America/New_York`.
- **Bind addresses.** Unchanged and correct: `0.0.0.0` appears only as the *door*
  server default (`server.py:116`, `cli.py:601`, `cli.py:912`) and in the
  `--control-host` help text, which carries the UNAUTHENTICATED warning. The control
  channel defaults to `127.0.0.1` (`cli.py:167`) and `cli.py:1080-1081` still
  refuses `--control-host` without `--daemon`.
- **YAML.** `scripting.py:159` remains the only entry point and uses
  `yaml.safe_load`. Repo-wide there is no `yaml.load` / `full_load` / `unsafe_load`.
- **No dangerous execution sinks.** Repo-wide grep over `src/` and `scripts/` for
  `eval(`, `exec(`, `pickle`, `subprocess`, `os.system`, `shell=True`, `__import__`:
  nothing. The only `open(` calls in `src/` are `tz_utils.py:67` (TZif resource,
  read-only) and `commands/history.py:196,213` (history truncation on an
  already-0600 file).
- **History file permissions.** `_create_private_file` (`commands/history.py:33-41`)
  still `os.open(..., O_CREAT, 0o600)` plus an unconditional `chmod`, before the
  backend touches the path; the later `open(..., "w")` truncations preserve the
  mode.
- **Dependencies.** Lock is current for August 2026 and the runtime surface is
  minimal. Runtime: `tzdata 2026.3` (data only). Extras: `pyyaml 6.0.3`
  (`safe_load` only), `prompt-toolkit 3.0.53`. Dev: `pytest 9.1.1`, `mypy 2.3.1`,
  `ruff 0.16.4`, `coverage 7.15.4`, `hypothesis 6.165.10`. Build backend still
  exactly pinned at `setuptools==84.0.0` / `wheel==0.48.0`, both above the 2024–2026
  setuptools advisories (CVE-2024-6345, CVE-2025-47273). No EOL dependency. No
  advisory applies to any pinned version.
- **CI supply chain.** Every `uses:` is SHA-pinned, including the composite action
  and the Gitea wiki-sync reusable workflow
  (`neuromancy/workflows/...@5d9eb7fb...`, the only `uses:` that receives a secret).
  No `pull_request_target`, no `workflow_run`, no `ref:`/`repository:` override on
  any checkout. No secret is interpolated into a `run:` string; `CODECOV_TOKEN` is a
  job-level `env` consumed as an action input and guarded by
  `if: ${{ env.CODECOV_TOKEN != '' }}`. `permissions:` still appears once —
  `id-token: write` on `publish` alone, with OIDC trusted publishing. The
  `TESTING_GAPS.md` commit-and-push step is fenced by
  `github.event_name == 'push'`. `uv sync --locked` everywhere, so CI can never
  silently resolve an unpinned set. Every job carries `timeout-minutes: 30`.
- **Static gates on the shipped tree.** `ruff check src` and `mypy src` both clean
  at this commit (`Success: no issues found in 31 source files`).
- **No secrets at rest or in logs.** The device protocol carries no credentials,
  keys or PII; neither the library nor the simulator stores or emits any. There is
  nothing to rotate, redact or encrypt at rest, which is why the key-management and
  redaction sections of this persona's remit remain empty for this project.
