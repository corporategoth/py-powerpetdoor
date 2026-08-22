# Security Analyst Analysis — Round 8

Commit `da31ae2` ("Round 7 fixes (refuter-approved list only)"), working tree
clean, Python 3.13.13, `.venv` as locked.

Scope note carried from the brief: the device protocol is plaintext JSON over TCP
with no authentication or encryption **by device design**. Nothing below asks for
TLS on it, and nothing below asks the code to *reject* input a real door could
legitimately send. Both findings are of the form "accept the same bytes, fail
safely instead of crashing" — neither narrows what we accept and neither changes
a byte we send.

Every number in this report came out of a harness that was executed on this
machine at this commit. Harnesses lived in `/tmp/r8sec/`, were run, and were
deleted; every spawned daemon was terminated. No repository file was modified
(`git status` clean throughout) except this report.

Gates re-run at this commit before starting: `2620 passed in 37.13s`,
`ruff check src tests` → *All checks passed!*, `ruff format --check` → *80 files
already formatted*, `mypy src` → *Success: no issues found in 31 source files*.

---

## Summary

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 1 |
| Informational | 0 |

- **Medium 1** — `_dispatch_frame` catches only `json.JSONDecodeError`, but
  `json.loads` raises `RecursionError` (deep nesting) and a bare `ValueError`
  (integer literal over 4,300 digits) on frames well inside every existing
  bound. Both escape. Landing inside `data_received` fatal-errors the transport;
  landing inside round 7's new `call_soon` re-arm (`FrameDispatcher._resume_pump`)
  leaves the dispatcher **permanently wedged** — transport paused, backlog
  stranded, nothing left to pump it. **5,399 bytes** per victim connection, in
  the shipped client *and* the simulator. 200 wedges cost 1.08 MB and leak 200
  fds and 200 `DoorSimulator.protocols` slots that survive the attacker closing
  the socket. On the shipped client, `client.available` still reports `True`.
- **Low 2** — Round 7's own `_keep_int` facade guard type-checks but does not
  range-check, and `_on_hold_time_update` then divides by `100.0`. A 401-digit
  `holdTime` (legal JSON, accepted by `json.loads`) raises `OverflowError` inside
  the client's listener isolation: **50 frames → 50 full tracebacks**,
  unthrottled, while `door.hold_time` stays silently stale. Same defect class,
  same method, as the `"200"` case the round-7 fix was written to close.

All six round-7 fixes verified and hold. See **Round 7 Fix Verification**.

---

## Findings

### 1. [Medium] `json.loads` raises `RecursionError` and plain `ValueError`, not only `JSONDecodeError` — both escape `_dispatch_frame`, and in round 7's new `_resume_pump` re-arm that permanently wedges the connection: 5,399 bytes buys a paused transport, a stranded backlog and a leaked slot, on both sides

**Files:**
- `src/powerpetdoor/client.py:1727` — `except json.JSONDecodeError as err:` (the
  shipped library)
- `src/powerpetdoor/simulator/protocol.py:462` — the same, in the simulator
- `src/powerpetdoor/framing.py:612` — `task = self._dispatch(self._backlog.popleft())`,
  the unguarded call; an exception here also skips
  `src/powerpetdoor/framing.py:618` (`self._update_flow()`), which is what leaves
  the transport paused
- `src/powerpetdoor/framing.py:627-629` — `_resume_pump`, round 7's re-arm; an
  exception here goes to the loop exception handler and no further pump is armed
- Contracts this violates: `src/powerpetdoor/framing.py:32`
  ("**Never raises** on arbitrary input") and
  `src/powerpetdoor/client.py:1675-1676` ("This callback never raises on
  arbitrary input")

`json.loads` has two documented failure modes that are **not** `JSONDecodeError`:

```
$ python -c "import json,sys; print(sys.get_int_max_str_digits())"
4300
4301 digits -> ValueError -> Exceeds the limit (4300 digits) for integer string conversion
             | frame bytes: 4307 | is JSONDecodeError: False
list nesting 9998 -> RecursionError | frame bytes: 20002
    exception type: (RecursionError, RuntimeError, Exception, BaseException)
```

Neither frame is refused anywhere upstream: 4,307 and 20,002 bytes are far under
`MAX_BUFFER_SIZE` (65,536) and both are brace-balanced, so `FrameScanner` frames
them correctly and hands them straight to `_dispatch_frame`.

**Reproduction A — both shapes, both dispatch paths, both sides**
(`/tmp/r8sec/h5_bigint.py`, shipped `PowerPetDoorClient` and
`DoorSimulatorProtocol`, a transport counting `pause_reading`, a loop exception
handler installed):

```
MAX_INFLIGHT_FRAMES=64 MAX_FRAME_BACKLOG=256
bigint frame = 4307 B   nested frame = 20002 B

[A] the offending frame alone (dispatched inside data_received):
  SIM bigint      escaped=ValueError      loop_hits=0  backlog=0 inflight=0 paused=False
  CLI bigint      escaped=ValueError      loop_hits=0  backlog=0 inflight=0 paused=False
  SIM nested      escaped=RecursionError  loop_hits=0  backlog=0 inflight=0 paused=False
  CLI nested      escaped=RecursionError  loop_hits=0  backlog=0 inflight=0 paused=False

[B] 64x {x} + frame + 300x {x} (dispatched from the round-7 call_soon re-arm):
      payload = 5399 bytes
  SIM bigint wedge  escaped=None  loop_hits=1 (ValueError in FrameDispatcher._resume_pump())
                                  backlog=300 inflight=0 paused=True
  CLI bigint wedge  escaped=None  loop_hits=1 (ValueError in FrameDispatcher._resume_pump())
                                  backlog=300 inflight=0 paused=True
      payload = 21094 bytes
  SIM nested wedge  escaped=None  loop_hits=1 (RecursionError in FrameDispatcher._resume_pump())
                                  backlog=300 inflight=0 paused=True
  CLI nested wedge  escaped=None  loop_hits=1 (RecursionError in FrameDispatcher._resume_pump())
                                  backlog=300 inflight=0 paused=True
```

The `[B]` construction is deliberate and cheap: 64 three-byte `{x}` frames use up
`_pump`'s per-invocation budget without producing a task (so `_inflight` stays 0),
which pushes the poisoned frame into the `call_soon` continuation; the 300
trailing `{x}` frames keep the backlog above `MAX_FRAME_BACKLOG` so the transport
stays paused. `_resume_pump` clears `_pump_scheduled` *before* calling `_pump`, so
after the exception nothing is scheduled, `_inflight` is 0 (no done-callback will
ever fire), `_update_flow()` was skipped, and **the only two things that call
`_pump` are both unreachable**. The connection is alive and permanently deaf.

**Reproduction B — real `ppd-simulator --daemon`, 20 wedges**
(`/tmp/r8sec/h2_daemon_wedge.py`; payload = 64×`{x}` + nested + 300×`{x}`):

```
baseline: RSS 32.7 MB, ctl status 0 ms
payload per connection: 21094 bytes
sent 0.422 MB over 20 conns in 0.00s
sockets still writable: 20/20; sockets that answered a later valid command: 0/20
ctl status (0 ms): Clients: 20 clients
fresh client reply: b'{"CMD": "GET_DOOR_STATUS", "success": "true", ...'
--- daemon log: 69814 bytes
RecursionError mentions: 20 | 'Exception in callback' mentions: 20 | Traceback mentions: 20
    2026-08-22 11:48:58,545 [ERROR] Exception in callback FrameDispatcher._resume_pump()
```

**Reproduction C — the leak is permanent and survives the attacker's `close()`**
(`/tmp/r8sec/h3_slotleak.py`, with a healthy client as the control):

```
baseline    : Clients: none                  RSS 32.9 MB
after wedge : Clients: 20 clients            RSS 36.1 MB   (ctl 1 ms)
healthy +1  : Clients: 21 clients        <- control: an honest client connects
healthy gone: Clients: 20 clients        <- control: and disconnects cleanly
t+ 1s after attacker CLOSED all 20 sockets: Clients: 20 clients  RSS 36.1 MB
t+ 5s                                     : Clients: 20 clients  RSS 36.1 MB
t+15s                                     : Clients: 20 clients  RSS 36.1 MB
t+30s                                     : Clients: 20 clients  RSS 36.1 MB
```

The healthy control pair is what makes this specific: the same counter tracks a
normal connect/disconnect exactly, and does not track these. Reading is paused, so
the peer's FIN is never observed, `connection_lost` never fires, and the protocol
object is never removed from `DoorSimulator.protocols`.

**Reproduction D — it scales linearly and leaks file descriptors**
(`/tmp/r8sec/h6_scale.py`, 200 connections at the cheaper 5,399-byte payload):

```
daemon RLIMIT_NOFILE soft=1048576 hard=1048576
baseline: fds=9   RSS=32.9 MB  Clients: none
attacker: 200 conns x 5399 B = 1.080 MB in 1.04s
after   : fds=209 RSS=38.3 MB ctl=1ms Clients=200 clients
attacker CLOSED all 200 sockets; 3 s later:
          fds=209 RSS=38.3 MB ctl=1ms Clients=200 clients
          honest client still served: b'{"CMD": "GET_DOOR_STATUS", "success": "true", ...'
daemon log bytes: 699454 for 1079800 attacker bytes (x0.648)
tracebacks: 200 | ValueError: 200
```

209 fds before and after the close: all 200 are held. The log cost is one **full,
unthrottled traceback per wedge** (3,497 B each, ×0.648 amplification — two orders
of magnitude above the ×0.005–0.04 the throttled sites achieve).

**Reproduction E — the shipped client, against a hostile "door" over real TCP**
(`/tmp/r8sec/h4_client_wedge.py`: `asyncio.start_server` answering a real
`PowerPetDoorClient.connect()`):

```
--- mode=alone keepalive=0.0 over 5.0s
    hostile server saw 9 client connection(s)          <- reconnect storm
    loop exception-handler hits: 9 -> 'Fatal error: protocol.data_received() call failed.'/RecursionError
    client.available = False
--- mode=wedge keepalive=0.0 over 5.0s
    hostile server saw 1 client connection(s)
    loop exception-handler hits: 1 -> 'Exception in callback FrameDispatcher._resume_pump()'/RecursionError
    dispatcher: backlog=300 inflight=0 paused=True
    client.available = True   transport is not None = True     <- silently deaf
--- mode=wedge keepalive=1.0 over 12.0s
    hostile server saw 2 client connection(s)                  <- keepalive recovers, then re-wedges
    dispatcher: backlog=300 inflight=0 paused=True
```

Two distinct client-side outcomes. With `keepalive=0` (a supported configuration)
the client is **permanently deaf while reporting `available = True`**. With the
`PowerPetDoor` default `keepalive=30.0` and `MAX_FAILED_PINGS = 3`
(`client.py:132`) it recovers after ~90 s of silence — and can be re-wedged by the
next 5,399 bytes.

**Reproduction F — a naive hostile-input fuzz finds it in a third of frames**
(`/tmp/r8sec/h15_fuzz.py`, 3,000 randomized frames per side built from ANSI/NUL
strings, 5,000-char strings, `NaN`/`Infinity`/`1e400`, deep nesting, long integer
literals, valid command names with hostile payloads):

```
client : 3000 frames, 16758949 bytes
          data_received raised: {'ValueError': 630, 'RecursionError': 400}
          loop exception-handler hits: 0
          log records 3070, with exc_info (tracebacks): 0, raw ESC 0, raw BEL 0
sim    : 3000 frames, 16555330 bytes
          data_received raised: {'ValueError': 646, 'RecursionError': 381}
          log records 3393, with exc_info (tracebacks): 0, raw ESC 0, raw BEL 0
```

34% of frames make `data_received` raise. Everything else in that corpus is clean
— 0 tracebacks, 0 raw ESC, 0 raw BEL — so this is the *only* residual escape, and
it is a large one. The in-tree fuzz suite structurally cannot reach it:
`tests/fuzz/test_untrusted_input_fuzz.py:70` bounds integers at `10**9` and
`:66-78`'s `st.recursive` has `max_leaves=8`, and every payload is built as a
**Python object handed to a handler**, never as JSON *text* fed to
`data_received`. The gap is precisely at `json.loads`.

**Attack scenario.** The simulator daemon binds `0.0.0.0:3000` by default. Any
host on the LAN opens N sockets and writes 5,399 bytes on each — brace-balanced,
well under every declared bound, containing nothing but `{x}` and one long integer
— and stops. Each socket permanently costs the daemon one fd, one protocol object,
one stranded backlog and one full traceback in the log; `ctl status` reports every
one of them as a connected client forever, including after the attacker walks
away. Nothing caps the connection count, so the operator's only remedy is
restarting the daemon.

For the **shipped library** the same construction from a hostile door — or from
anything that can answer on the LAN, the protocol being unauthenticated by design
— either (a) holds the client in a connect → `RecursionError` → fatal-error →
reconnect loop, 9 cycles per 5 s with a full traceback each, or (b) silently kills
the receive path while `client.available` keeps returning `True`. For the Home
Assistant deployment target, (b) means the pet-door integration stops updating
with the entity still shown as available.

**Why Medium and not Low:** remotely reachable, no privilege, no interaction, no
malformed framing; two orders of magnitude cheaper than the round-7 Medium
(5,399 B vs 16.78 MB); **permanent** where the round-7 Medium was transient; it
leaks fds and connection slots that survive the peer's disconnect; it makes a
documented public property (`available`) lie; and it falsifies two explicit
"never raises" contracts in the source.

**Why not High:** availability only. No confidentiality, integrity, state
corruption or privilege impact — the poisoned frame is dropped, not executed, and
no stored state is altered. The simulator is a documented development tool, and
the shipped client self-heals under the default keepalive.

**Recommendation** — fail safely; do not narrow what is accepted. Every byte of
these frames is still framed and still logged the same way an unparseable frame
is; only the crash goes away.

1. Widen both `except` clauses to `except Exception` (or at minimum
   `(json.JSONDecodeError, ValueError, RecursionError)`), keeping the existing
   throttled "unusable frame" path and `return None`. `json.JSONDecodeError` is a
   `ValueError` subclass, so the existing message can stay; add the exception type
   to the throttled detail so an operator can still tell the shapes apart. This
   changes nothing about which bytes are accepted — a frame that cannot be
   decoded was already skipped.
2. Defence in depth in `FrameDispatcher._pump`: wrap the `self._dispatch(...)`
   call so a raising handler cannot strand the backlog, and move
   `self._update_flow()` into a `finally`. The dispatcher is a shared, reusable
   component and should not depend on its callback being total. Re-arming the
   pump on that path is what turns "wedged forever" into "one frame lost".
3. Add the missing fuzz shape: a property test that feeds arbitrary *bytes* (not
   post-parse objects) to `data_received` and asserts it never raises, with
   `st.integers()` unbounded and a deep-nesting generator. That is the test that
   would have caught this, and it is the one the suite does not have.
4. Still open from rounds 6 and 7: cap concurrent door connections in the
   simulator. The real device accepts one client at a time, so a small ceiling is
   *better* fidelity as well as defence in depth, and it bounds Reproduction D.

---

### 2. [Low] Round 7's own `_keep_int` facade guard type-checks but does not range-check: a 401-digit `holdTime` overflows `centiseconds / 100.0` and produces one unthrottled traceback per frame in the shipped library, while `door.hold_time` stays silently stale

**Files:**
- `src/powerpetdoor/door.py:162-175` — `_keep_int`, which returns any
  `isinstance(value, int)` unchanged
- `src/powerpetdoor/door.py:1415-1416` — `centiseconds = _keep_int(...)` then
  `self._hold_time = centiseconds / 100.0`
- `src/powerpetdoor/client.py:719-729` — `_notify_listeners`, whose per-listener
  isolation turns the raise into `_LOGGER.exception(...)`, unthrottled, per frame
- Reached from `src/powerpetdoor/client.py:809-810` (`GET_SETTINGS` →
  `holdOpenTime`) and `client.py:1000-1002` (`GET_HOLD_TIME` → `holdTime`)

```
$ python -c "print((10**400)/100.0)"
OverflowError: int too large to convert to float
```

`_keep_int` was added by the round-7 B-M1 fix to stop the facade caching device
values verbatim. It correctly rejects `str`, `bool`, `NaN` and `Infinity` — and
correctly accepts any `int`. But Python ints are unbounded while `float` is not,
and `_on_hold_time_update` is the one retained facade value with arithmetic on it.
A 401-digit integer is legal JSON, is accepted by `json.loads` (the 4,300-digit
limit is not reached), and passes every check in `_keep_int`.

**Reproduction A — through a real connected `PowerPetDoor` over a real socket**
(`/tmp/r8sec/h19_holdtime.py`; `holdTime` = `1` followed by 400 zeros; logger at
WARNING, the level a production deployment runs at):

```
door.hold_time after connect: 2.0
2000 frames (930000 B on the wire) of a 401-digit holdTime:
   log records 2004, with full traceback: 2000 {'OverflowError': 2000}
   log bytes 992587 -> x1.07 write amplification
   door.hold_time is still 2.0 (silently stale)
```

One full traceback per frame, 2,000 for 2,000 — no `EventThrottle`, no
`MAX_LOGGED_LENGTH`, 496 log bytes per 465 wire bytes.

**Reproduction B — sweep of every numeric device field, with controls**
(`/tmp/r8sec/h20b.py`, real `asyncio.start_server` device, real `door.connect()`,
50 frames each):

```
  GET_HOLD_TIME holdTime          50 frames ->  50 records,  50 tracebacks ['OverflowError']  hold_time=2.0
  GET_SETTINGS holdOpenTime       50 frames ->  50 records,  50 tracebacks ['OverflowError']  hold_time=2.0
  GET_DOOR_BATTERY percent        50 frames ->   0 records,   0 tracebacks []
  GET_DOOR_OPEN_STATS             50 frames ->   0 records,   0 tracebacks []
  GET_SENSOR_TRIGGER_VOLTAGE      50 frames ->   0 records,   0 tracebacks []
  GET_SCHEDULE hour               50 frames ->  50 records,   0 tracebacks []   <- control: rejected cleanly
  PONG token                      50 frames ->   0 records,   0 tracebacks []
  control: holdTime 200           50 frames ->   0 records,   0 tracebacks []   <- control: healthy
```

Exactly two sites, both routing through `_on_hold_time_update`. The
`GET_SCHEDULE hour` row is the control that shows the correct shape already exists
elsewhere: `coerce_schedule_int` (`schedule.py`) range-checks and rejects with a
reason, no traceback. The other `_keep_int` consumers (`battery_percent`,
`total_open_cycles`, `total_auto_retracts`) only store and compare, so a huge int
is inert there — it is cached and published verbatim (`battery=10000…000`, 401
digits into a property annotated `int`), which is ugly but not a raise.

**Attack scenario.** A hostile door — or anything answering on the LAN, the
protocol being unauthenticated by design — replies to `GET_HOLD_TIME` with a
long-integer `holdTime`. Every reply writes a full `OverflowError` traceback into
the host application's log at ×1.07 the wire bytes, unthrottled and uncapped,
which for the Home Assistant deployment target is the whole instance's log. The
facade's cached `hold_time` never updates, so the documented property reports a
stale value with nothing in the log tying the staleness to the frame — the exact
failure mode round 5 removed elsewhere and round 7's B-M1 fix set out to close in
this very method.

**Why Low:** volume and diagnostic fidelity only. No injection (the traceback
carries no attacker string), no crash, no state corruption, no confidentiality
impact, and the library keeps running. It is filed because it is a *regression
introduced by the round-7 fix*, in the shipped library, at exactly the site whose
docstring describes the bug it is now a new instance of.

**Recommendation:** range-check where the value is used, not only its type. Give
`_keep_int` an optional `maximum` (or add a `_keep_centiseconds` sibling) and
apply the wire protocol's own ceiling —
`MAX_HOLD_TIME_CENTISECONDS = 90000`, already defined at
`simulator/protocol.py:151` and already the bound the simulator enforces on the
same field. A value outside it takes the existing "keep the cached value" path and
logs at DEBUG like every other rejection, so nothing new is refused loudly and no
frame is dropped. While there, sanity-check the other `_keep_int` call sites for
values a consumer would publish verbatim.

---

## Round 7 Fix Verification

All six round-7 items were re-derived at this commit. Every one holds.

**S-M1 rec 1 — `_pump()` now yields.** Fixed, and the effect is larger than the
commit claimed on the shape it targeted. One 256 KiB read, fresh scanner and
dispatcher per trial, `gc.collect()` before each, 5 trials
(`/tmp/r8sec/h7_pump.py`):

| unit | round 7 (refuter, before) | round 8 (measured now) |
|---|---|---|
| `{x}` SIM | 254.1 / 254.6 / 257.2 ms | **52.8 / 60.0 / 72.7 ms** (min/median/max) |
| `{,}` SIM | 1414.7 ms (dirty-heap run) | **50.9 / 51.9 / 68.8 ms** |
| `{"a"}` SIM | 159.6 / 161.0 / 161.6 ms | **36.6 / 37.3 / 37.8 ms** |
| `{}` SIM | 61.9 / 65.2 / 68.2 ms | 68.9 / 73.1 / 93.5 ms (unchanged, as expected) |
| `{x}` CLI | — | **49.4 / 52.1 / 55.4 ms** |

The behavioural change is the important half: `{x}` used to drain the whole read
synchronously (`backlog left 0`, `pause_reading x0`) so the pause threshold never
engaged. It now does:

```
b'{}'   backlog immediately after read=131008  high-water during drain=131008
        tracemalloc peak=8.47 MB  pause x1 resume x1  final backlog=0 inflight=0
b'{x}'  backlog immediately after read=87317   high-water during drain=87317
        tracemalloc peak=5.81 MB  pause x1 resume x1  final backlog=0 inflight=0
```

The disclosed tradeoff is real and is exactly one read's worth — the high-water
mark never exceeds what the single read produced, and accounting returns to zero.

Real daemon, 64 sockets × one 256 KiB `{}` burst then silence, **3 clean trials**
(`/tmp/r8sec/h9b.py`):

```
trial 1: RSS 32.9 -> peak 261.1 (+228.1 MB) | ctl max 2004 ms, >500ms: 3 | recovered 53 s after close
trial 2: RSS 32.7 -> peak 260.8 (+228.1 MB) | ctl max 1690 ms, >500ms: 4 | recovered 51 s after close
trial 3: RSS 33.0 -> peak 261.1 (+228.2 MB) | ctl max 2034 ms, >500ms: 3 | recovered 56 s after close
```

and the `{x}` shape (`/tmp/r8sec/h8_burst.py`): `+148.2 MB`, `ctl status max
1777 ms`, settled 72.6 MB, `Clients: none` promptly after close. The commit's
"8534 ms → 1768 ms" matches my `{x}` measurement (1,777 ms). The `{}` worst case is
unchanged at +228 MB, exactly as disclosed.

**Two honest notes on my own numbers.** A first, un-replicated `{}` run measured
`ctl status max 3783 ms`; three clean trials give 1,690–2,034 ms, so I would not
quote the 3,783 ms. And "RSS settles fully" is true for `{x}` but slower than that
phrase suggests for `{}`: at the moment `Clients: none` first appears (51–56 s
after the attacker closes) RSS is still 130–136 MB against a 33 MB baseline. That
is drain time plus allocator retention, not a leak — the backlog and protocol
accounting return to zero — but the operator-visible recovery for the `{}` shape
is ~1 minute, not immediate.

**Sustained flood — the new re-arm keeps memory bounded and the control plane
responsive** (`/tmp/r8sec/h18_sustained.py`, 8 connections of packed `{x}` for
30 s):

```
attacker wrote 50.9 MB
RSS trace: t+2s 44MB, t+4s 51MB, t+6s 57MB, t+8s 65MB, t+10s 70MB, t+12s 54MB,
           t+14s 52MB, t+16s 59MB, t+18s 65MB, t+20s 67MB, t+22s 70MB,
           t+24s 70MB, t+26s 70MB, t+28s 70MB, t+30s 65MB
during: ctl status 23 ms, 8 clients
8 s after the flood stops: RSS 50.4 MB
```

RSS plateaus at ~70 MB and `ctl status` stays at 23 ms throughout — against
round 7's 175–1,867 ms for sustained floods.

**S-L2 — the write ceiling latches and actually drops.** Fixed on every leg.
In-process, real socketpair, `connection_lost` wrapped
(`/tmp/r8sec/h10_ceiling.py`):

```
MAX_WRITE_BACKLOG = 1048576
one GET_SETTINGS response is 340 bytes; 3084 unread responses to reach the ceiling
ceiling fired after 3400 VALID GET_SETTINGS (0.16 MB of requests)
  _dropped latched = True; transport.is_closing() = True
  ERROR records so far: 2
  300 further _send() after the latch -> 0 further log records
  connection_lost fired: True (exc=None)
```

Real daemon, attacker with `SO_RCVBUF=2048` sending valid `GET_SETTINGS` and never
reading (`/tmp/r8sec/h18_sustained.py` part b):

```
connected: 1 client
wrote 0.54 MB of VALID requests, never read; writer saw: ConnectionResetError: [Errno 104]
daemon RSS 32.9 -> 34.3 MB; ctl status 1 ms -> none
t+5s: none   t+15s: none   t+30s: none
'not reading its responses' ERROR records total: 1
log grew 1440 B from 8 ordinary operator commands   (0 of them a drop record)
daemon log 2904 B for 540500 attacker bytes (x0.0054)
```

Every round-7 leg inverted: `ctl status` is truthful ("none", was "1 client"
forever), one ERROR record (was 1,283–2,641), ordinary operator activity produces
no new drop records (was 2–6), amplification ×0.0054 (was ×0.047–0.118). The peer
does see `ConnectionResetError` over real TCP, as the commit claimed — on a
socketpair where the peer drains first it sees a clean EOF instead, which is the
same thing arriving in a different order.

**Is `abort()` safe on every path?** Tested three
(`/tmp/r8sec/h11_abort_paths.py`, `/tmp/r8sec/h12_bcast.py`):

```
overflow path : protocols now 0 (expect 0), drop records 1
                'Simulator: receive buffer overflowed without a complete message; dropping the connection'
broadcast path: rounds=13 exception escaping the broadcast loop: None
                drop records: 5 | protocols remaining: 0 (expect 0) | loop exception-handler hits: 0
                what each peer ultimately sees: ['clean EOF'] x5
double drop   : 3 calls -> 5 ERROR records, no exception; transport.is_closing()=True
```

The broadcast case is the one that could have gone wrong — the ceiling firing for
five protocols while `broadcast_status()` iterates `self.protocols`. It does not:
`abort()` defers `connection_lost` through `call_soon`, so the list is mutated
after the iteration, all five are removed, and nothing raises.

**Is the ceiling a false positive for a legitimate slow-but-valid peer?** No path
found. It takes **3,084 unread 340-byte responses** to reach 1 MiB, and the
simulator has no request that produces a large response: `get_schedule_list()`
returns at most 256 integers (`MAX_SCHEDULE_INDEX = 255`, `schedule.py:59`),
`get_settings()` is fixed-size, and `SET_TIMEZONE` is capped at 128 characters. So
there is no small-request/large-response amplification, and the shipped client
paces one message at a time. Reaching the ceiling requires the peer to stop
reading, which is the condition the ceiling exists for. Discarding the unsent tail
with `abort()` is correct for a declared protocol violation.

**S-L3 — the four per-frame log sites are throttled and length-capped.** Fixed
(`/tmp/r8sec/h13_throttle.py`, logger at WARNING, one `data_received` of packed
frames, drained):

| attack | round 7 (before) | round 8 (measured now) |
|---|---|---|
| client `{"CMD":"a"}` (success absent) | 1,460,000 B / ×6.64 / **20,000** records | 2,826 B / **×0.013** / **32** records |
| client `{"CMD":"a","success":"false"}` | 1,860,000 B / ×3.21 / 20,000 records | 3,153 B / **×0.005** / 32 records |
| client one 60 KB `reason` field | 240,428 B / ×1.00 / 4 records | **1,136 B** / ×0.005 / 6 records |
| SIM `SET_HOLD_TIME holdTime:[]` | 824,000 B / ×2.58 / 8,000 records | 2,889 B / **×0.009** / **26** records |
| SIM `DELETE_SCHEDULE index:{}` | 816,000 B / ×2.62 / 8,000 records | 2,875 B / ×0.009 / 26 records |
| SIM `SET_SCHEDULE {"index":[]}` | 832,000 B / ×2.12 / 8,000 records | 2,902 B / ×0.007 / 26 records |
| control `{x}` (throttled in round 6) | ×0.04 / 52 records | ×0.037 / 52 records |
| control `{}` (throttled in round 6) | ×0.04 / 52 records | ×0.041 / 52 records |

The 60 KB `reason` row is the `MAX_LOGGED_LENGTH` half: 240,164 wire bytes now
produce 1,136 log bytes instead of 240,428.

**T-L1 — bound constants pinned by value.** Verified present:

```
tests/test_sanitize.py:189            MAX_LOGGED_LENGTH == 200
tests/test_framing.py:1274,1278,1282  MAX_BUFFER_SIZE == 64*1024, THROTTLE_QUIET_PERIOD == 60.0,
                                      MAX_THROTTLE_INTERVAL == 4096
tests/test_framing.py:1286,1290,1294  MAX_INFLIGHT_FRAMES == 64, MAX_FRAME_BACKLOG == 256,
                                      MAX_RETAINED_PIECES == 64
tests/simulator/test_engine.py:900    MIN_BLOCKED_RECHECK == 0.1
tests/simulator/test_protocol.py:2054-2059  MAX_WRITE_BACKLOG == 1024*1024, MAX_TIMEZONE_LENGTH == 128,
                                      MAX_HOLD_TIME_CENTISECONDS == 90000, MAX_TRIGGER_VOLTAGE == 65535
```

Two shipped bounds still have no value pin: `_ControlLogHandler.MAX_CLIENT_BACKLOG`
(`cli.py:192`) and `MAX_SCHEDULE_INDEX` (`schedule.py:59`). Both are real
resource/range caps of the same class the policy was written for. Noted for the
test persona rather than filed here.

**S-I4 (`FrameDispatcher.reset()` clears `_paused` without resuming)** — refuted in
round 7 and correctly left alone. Re-checked: still divergent, still no reachable
call site, docstring precondition unchanged. No action.

---

## Areas Reviewed With No Findings

- **`_pump`'s yield/re-arm, apart from Finding 1.** I could not starve it, wedge
  it or make the backlog grow, by any input that does not raise. The pause
  threshold engages on every frame shape now (`pause_reading x1` for `{}`, `{x}`,
  `{,}`, `{"a"}`), the backlog high-water never exceeds one read's output, the
  drain returns `inflight=0 backlog=0 paused=False` with `resume_reading x1`, and
  a 30 s / 50.9 MB sustained flood plateaus at +37 MB with `ctl status` at 23 ms.
  Re-entrancy is impossible (`create_task` never returns a done task, so
  `add_done_callback` cannot fire synchronously), and `_on_dispatched_done` cannot
  drive `_inflight` past `max_inflight` because the loop re-tests it every
  iteration.
- **`_pump_scheduled` is not cleared by `reset()`.** Observed and traced: a
  pending `_resume_pump` at teardown leaves the flag set. It cannot wedge anything
  in-tree — the pending callback still runs and clears it, and a resubmit while it
  is pending is served by that same callback. The only construction that would
  strand it is a dispatcher reused across a *closed* loop, which no shipped code
  path produces (`start()` after `stop()` on a private loop reuses the closed loop
  and fails earlier). **Not filed**, on exactly the grounds the refuter used to
  reject round 7's S-I4: a hypothetical about a caller that does not exist.
- **`_drop_connection` does not re-check `_dropped` at entry** — three successive
  calls produce five ERROR records instead of one. Unreachable today: `_send`
  latches, and asyncio delivers no further `data_received` after `abort()`. Noted,
  not filed.
- **Throttle suppression and amplification, including the new shared
  `_rejections` throttle** (`/tmp/r8sec/h14_supp.py`). The shared throttle does
  mask a *distinct* rejection's detail — after 5,000 `SET_HOLD_TIME` rejections
  (26 log records), one malformed-schedule rejection produces 0 new records. But
  the client is not misled: the wire response still carries the specific reason
  (`'Schedule index must be a number, got []'`), the count is carried in the
  summary, and the quiet period restores immediate reporting. The bounds all
  hold at this commit: 10,000,000 events in zero simulated time → 2,453 lines
  (1:4,076); a single fresh event 60 s after a 10M burst is reported immediately
  (`True`); 1,000 events paced one per quiet period → 1,000 lines
  (**1.00× amplification**, i.e. the pacing attack yields nothing). This is the
  designed and documented tradeoff, so it is recorded rather than filed.
- **Hostile wire input, everything except Finding 1.** 3,000 randomized frames
  per side (16.76 MB / 16.56 MB) at DEBUG: **0 tracebacks, 0 loop
  exception-handler hits, 0 raw ESC, 0 raw BEL** on both the shipped client and
  the shipped simulator protocol. The round-2/3/4 wire guards (unhashable
  `msgID`, unhashable schedule index, every `SET_*` field) all still hold.
- **ANSI injection.** `sanitize_text` (`sanitize.py:36-57`) truncates before
  escaping, so a 64 KiB hostile frame costs 0.031 ms and 415 output bytes instead
  of 8.51 ms and 120,000. Against a live daemon driven with `run \x1b[31mred` and
  a 300 KB control line: **0 raw ESC, 0 raw BEL** in the daemon log.
  `state.get_tzinfo`'s fallback warning uses `%r`, which escapes control
  characters by construction, and is de-duplicated per distinct value.
- **ReDoS.** `_POSIX_TZ_RE` (`tz_utils.py:39-44`) is linear — no nested quantifier
  over an ambiguous alternation. Measured: 50k alpha 1.53 ms, 50k alpha + 50k
  digits 0.22 ms, `"<"` + 50k alpha 0.25 ms, 30k `:` groups 0.00 ms, 20k comma
  rules 1.18 ms, 64 KiB mixed 0.22 ms. It is also not on the wire path — only
  `commands/settings.py:377` (operator side) calls it.
- **Path traversal.** `get_posix_tz_string` / `find_iana_for_posix` /
  `parse_posix_tz_string` return `None` and never raise for
  `../../../../etc/passwd`, `/etc/passwd`, `..`, `.`, `""`,
  `America/../../etc/passwd`, 5,000 `A`s, `\x00`, `a/b/c/d/e`,
  `zoneinfo.America/New_York`, `\x1b[31m`. Device-controlled `SET_TIMEZONE`
  reaching `zoneinfo.ZoneInfo` falls back to UTC for every one of
  `../../../../etc/passwd`, `/etc/passwd`, `..`, `""`, `\x00evil`,
  `a/../../../../../../etc/shadow`, 128 `A`s, and resolves
  `America/New_York` correctly.
- **Control-channel script restriction.** Against a live daemon, all refused with
  no traversal and nothing echoed raw: `run ../../etc/passwd`, `run /etc/passwd`,
  `run .hidden`, `run ./ok`, `run scripts\ok`, `run ../ok`,
  `run ok/../../../etc/passwd` → *"Script paths are not allowed over the control
  channel"*; `run linked` (a symlink to `/etc/passwd`) → *"resolves outside …"*;
  `run \x1b[31mred` → `Unknown script: \x1b[31mred` (escaped); `load /etc/shadow`
  is not a command. A 300 KB control line closes that one connection and the
  daemon keeps serving.
- **Bind addresses.** Unchanged and correct: `0.0.0.0` is the *door* server
  default only (`server.py:116`, `cli.py:601`, `cli.py:912`). The control channel
  defaults to `127.0.0.1` (`cli.py:167`), its help text carries the
  UNAUTHENTICATED warning, and `--control-host 0.0.0.0` without `--daemon` still
  exits with `error: --control-host requires --daemon`.
- **YAML.** `scripting.py:197` remains the only entry point and uses
  `yaml.safe_load`, wrapped in `except yaml.YAMLError`. Repo-wide there is no
  `yaml.load` / `full_load` / `unsafe_load`.
- **No dangerous execution sinks.** Repo-wide grep over `src/` and `scripts/` for
  `eval(`, `exec(`, `pickle`, `subprocess`, `os.system`, `shell=True`,
  `__import__`, `marshal`: nothing. The only `open(` calls in `src/` are
  `tz_utils.py:67` (TZif resource, read-only) and `commands/history.py:196,213`
  (truncation of an already-0600 file).
- **History file permissions.** `_create_private_file`
  (`commands/history.py:33-41`) verified under `umask 000`: `0o600` after
  creation and still `0o600` after a truncating `open(..., "w")`.
- **Dependencies.** Lock current for August 2026; runtime surface minimal.
  Runtime: `tzdata 2026.3` (data only). Extras: `pyyaml 6.0.3` (`safe_load`
  only), `prompt-toolkit 3.0.53`. Dev: `pytest 9.1.1`, `pytest-timeout 2.4.0`,
  `mypy 2.3.1`, `ruff 0.16.4`, `coverage 7.15.4`, `hypothesis 6.165.10`. Build
  backend still exactly pinned at `setuptools==84.0.0` / `wheel==0.48.0`, both
  above CVE-2024-6345 and CVE-2025-47273. No EOL dependency; no advisory applies
  to any pinned version.
- **CI supply chain.** Every `uses:` is SHA-pinned, including the composite action
  (three `astral-sh/setup-uv@37802adc`) and the Gitea wiki-sync reusable workflow
  (`neuromancy/workflows/…@5d9eb7fb`, the only `uses:` that receives a secret). No
  `pull_request_target`, no `workflow_run`, no `ref:`/`repository:` override on any
  checkout. No secret is interpolated into a `run:` string; `CODECOV_TOKEN` is a
  job-level `env` consumed as an action input and guarded by
  `if: ${{ env.CODECOV_TOKEN != '' }}`. `permissions:` appears once —
  `id-token: write` on `publish` alone, with OIDC trusted publishing and an
  `environment: pypi` gate. The `TESTING_GAPS.md` commit-and-push step is fenced by
  `github.event_name == 'push'`. `uv sync --locked` everywhere. Every job carries
  `timeout-minutes: 30`, and `timeout_method = "thread"` (round 7's T-M1 fix) is
  in `pyproject.toml`, so a fuzz hang now fails at 60 s naming the line instead of
  being killed unattributably at 30 minutes.
- **`--debug` log volume.** Measured incidentally: at DEBUG the simulator's
  `Simulator RX:` line writes every received byte (sanitized) to the log and fans
  it out to every parked `ctl -i` session — ×4.8 write amplification on the same
  corpus that costs ×0.009 at WARNING. This is an explicitly opted-into diagnostic
  mode behind `logger.isEnabledFor(logging.DEBUG)`, so it is recorded, not filed.
- **No secrets at rest or in logs.** The device protocol carries no credentials,
  keys or PII; neither the library nor the simulator stores or emits any. There is
  nothing to rotate, redact or encrypt at rest, which is why the key-management
  and redaction sections of this persona's remit remain empty for this project.
