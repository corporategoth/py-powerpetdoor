# Security Analyst Analysis — Round 6

Scope: a fresh full-codebase security sweep of `pypowerpetdoor` at commit
`8a24804` — the shipped asyncio client library plus the device **simulator**
(door TCP protocol, unauthenticated control channel, YAML scripting,
interactive CLI/ctl) — after the Round-5 remediation (`EventThrottle`,
`FrameScanner`'s segment-list remainder, `_payload_mapping`,
`_ControlLogHandler.MAX_CLIENT_BACKLOG`, `_script_number` on the four delay
values, `directories:` in Dependabot, the CLAUDE.md manual-pin table,
`uv lock --upgrade`, chunk-independent `discarded`) and the new `ScriptQueue`
cancellable claims / `on_start` veto.

Threat model (unchanged):

- **LAN attacker** who can open TCP connections to a running simulator (door
  port, default bind `0.0.0.0:3000`, and/or the daemon control port).
- **Malicious / compromised door** (or a MITM on the plaintext LAN protocol)
  that a `PowerPetDoorClient` / `PowerPetDoor` has connected to.
- **Malicious / untrusted YAML script file** fed to the simulator.

The device wire protocol is plaintext JSON-over-TCP with no auth/crypto **by
hardware design** — findings are about how the library/simulator *handle* that
untrusted data.

Everything below was measured by executing the current code — over real TCP
against real `ppd-simulator` / `ppd-simulator-ctl` processes, and against a
real `PowerPetDoorClient` connected to a hostile "door", wherever the claim
concerns process behavior. Throwaway harnesses were deleted, spawned processes
killed, and no repo file was modified.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 1 |
| Informational | 3 |

**Every Round-5 item is fixed, and Finding 1 of Round 5 is fixed at the
mechanism level rather than at the benchmark.** The ×247 non-ASCII dribble is
now ×0.2 and the ×115 garbage dribble is ×0.1, measured against the shipped
client with the same harness; over real TCP the unpaced garbage flood that used
to produce 1.75 MB/s of log now produces 1,734 bytes total while the daemon
absorbs 2.58 GB at 516 MB/s. I could not use `EventThrottle` to *suppress*
anything: the first occurrence of every event type is always reported, the
throttles are per-event-type so nothing is conflated, the per-connection reset
makes a reconnect loop **louder** rather than silent, and the unthrottled
overflow ERROR still fires after 10,000 inflating events. The segment-list
remainder changes neither cap enforcement nor what can be smuggled (10,534
non-overflow differential cases, byte-exact; retained accounting exact in all
12,000). `MAX_CLIENT_BACKLOG` is measured on the right quantity and a slow
reader cannot get past it. The `on_start` veto neither leaks nor wedges under
16 forced vetoes.

The two findings below are what the Round-5 fix left standing, and both are
pre-existing rather than new. Round 5 removed the *per-chunk* cost of a hostile
packet; the remaining amplification is **per-frame**, and per-frame is the
worse shape, because it is limited by the attacker's byte rate rather than by
their packet rate. Finding 1 (Medium) is the memory half: one `asyncio.Task`
is created per framed message, synchronously, before any of them runs — 256 KiB
of packed `{}` becomes 131,072 live tasks and ~145 MB of heap, in the shipped
library as well as the simulator. Finding 2 (Low) is the log half, the same
class Round 5 already fixed once, at the three sites the fix did not reach.

---

## Findings

### 1. [Medium] One `asyncio.Task` per framed message, created synchronously per read and uncapped: 256 KiB of 2-byte frames = 131,072 live tasks / ~145 MB of heap, in the shipped client and the simulator

**Files:**
- `src/powerpetdoor/client.py:1649` — `self._track_task(self.process_message(msg))`,
  once per frame in the `for frame in frames:` loop — **the shipped library**
- `src/powerpetdoor/simulator/protocol.py:402` — `self._create_task(self._handle_message(msg))`,
  the simulator's twin
- Enabling conditions (simulator): `src/powerpetdoor/simulator/server.py:157`
  (`self.protocols.append(protocol)` — no connection cap) and the absence of any
  `transport.pause_reading()` call anywhere in the repo
- Secondary instance: `src/powerpetdoor/simulator/protocol.py:416-422` (`_send`
  writes every response with no `get_write_buffer_size()` check — the door
  transport never got the ceiling the control channel got in Round 5)

`data_received` frames the whole read and then creates one task per frame
*before returning*, so none of them has run yet when the last one is created.
asyncio's socket transport reads up to `max_size = 262144` bytes per callback,
and the cheapest legal frame is two bytes (`{}`). One read therefore admits
131,072 tasks in a single synchronous callback. Nothing bounds the count, the
byte cost per task, or the number of connections doing it at once.

**Measured in-process** (`tracemalloc`, live bytes after the callback returns):

| target | read | frame | tasks created | live heap | ratio |
|---|---|---|---|---|---|
| `DoorSimulatorProtocol` | 262,144 B | `{}` | **131,072** | 145.3 MB | **×554** |
| `PowerPetDoorClient` | 262,144 B | `{}` | **131,072** | 135.8 MB | **×518** |
| `DoorSimulatorProtocol` | 65,536 B | `{}` | 32,768 | 37.4 MB | ×570 |
| `PowerPetDoorClient` | 262,143 B | `{"a":1}` | 37,449 | 44.2 MB | ×169 |

**Measured against a real `ppd-simulator --daemon`** (RSS sampled at 20 Hz,
6 s of packed `{}` per connection):

| connections | attacker wire | daemon RSS | growth | ratio |
|---|---|---|---|---|
| 1 | 3.67 MB | 79.0 → 310.7 MB | +231.6 MB | ×63 |
| 4 | 12.32 MB | 79.3 → 970.6 MB | +891.3 MB | ×72 |
| 8 | 23.86 MB | 79.5 → **1877.2 MB** | **+1797.6 MB** | ×75 |

Linear in connection count, and there is no connection cap: 8 sockets and
24 MB of traffic bought 1.8 GB of daemon heap in six seconds. This machine had
the RAM to survive it; a container memory limit or a Pi does not.

**Measured against the shipped client over real TCP** — a hostile server that
completes the connection and then writes packed frames to a real
`PowerPetDoorClient`:

| frame flooded | door sent | client RSS | peak pending tasks |
|---|---|---|---|
| `{}` | 2.88 MB | 22 → 156 MB (**+135 MB, ×47**) | 107,239 |
| `{"a":1}` | 3.93 MB | 22 → 99 MB (+78 MB, ×20) | 30,721 |
| `{"CMD":"GET_SETTINGS","success":"true"}` | 17.82 MB | 22 → 42 MB (+20 MB) | 6,723 |
| `{x}` (unparseable — no task) | 3.93 MB | 22 → 25 MB (+3 MB) | 1 |

The last two rows are the calibration: the cost tracks *frames per read*, not
bytes, and a frame that fails `json.loads` never reaches the dispatcher, so it
costs nothing here (it costs in Finding 2 instead).

The secondary instance is the same missing backpressure on the write side.
A client that issues **valid** commands and never reads the answers made the
daemon buffer them without bound: 1.50 MB of `{"config":"GET_SETTINGS"}` →
RSS 79.2 → 115.3 MB (+36.1 MB, ×24), held for as long as the socket stayed
open and released on close. The control channel got `MAX_CLIENT_BACKLOG` in
Round 5; the door transport has no equivalent.

Collateral, from the same runs: under a `{"cmd":"a"}` flood the daemon spent
119.75 s of CPU over 125 s (95.7% of a core) and a fresh `ctl` connection
**timed out after 15 s** trying to run `status`. Round 5 recorded the daemon
staying responsive throughout its attacks; it no longer does under this one.

**Why Medium and not Low:** it terminates the host process rather than degrading
it, and the simulator half is unbounded — nothing caps connections, nothing
pauses reading, and the growth is linear in sockets. It needs no privilege, no
interaction, no timing and no malformed input: `{}` is legal JSON. Round 4
rated a CPU-only quadratic blowup Medium; an OOM is strictly worse in impact.
It also lands in the *shipped library*, whose deployment target is Home
Assistant on a small board, where +135 MB of recurring spike plus a saturated
event loop is felt by the whole host application rather than by the pet-door
integration.

**Why not High:** availability only — no confidentiality or integrity impact,
no state corruption, no privilege gain, and nothing persists. The memory is
reclaimed as soon as the tasks drain (verified: RSS returns to baseline when the
peer stops or disconnects), so it is a sustained-flood problem, not a leak. The
shipped-library half is *bounded* at roughly one read's worth (~150 MB) because
a client holds exactly one connection — it recurs, but it does not accumulate.
And the simulator is a development/test tool whose LAN exposure is a documented
default, not a shipped service.

**Recommendation:** bound the work one read may admit. In rough order of
directness:

1. Cap frames dispatched per `data_received` (a few hundred is far above any
   real device's burst), retain the rest in the scanner's remainder, and
   `transport.pause_reading()` until the backlog drains, resuming from the
   done-callback. This one change fixes both sides, and it is the only one that
   also bounds the *transient* peak rather than the steady state.
2. Or replace one-task-per-frame with a bounded `asyncio.Queue` drained by a
   single per-connection consumer task, and treat overflow the way the 64 KiB
   framing cap is already treated — drop the connection as a protocol
   violation. Handlers already run without ordering guarantees, so serializing
   them costs nothing semantically.
3. Simulator, for the secondary instance: apply the `MAX_CLIENT_BACKLOG` idea
   to `_send` as well — check `self.transport.get_write_buffer_size()` and drop
   a door client that is not reading its own responses. The control channel's
   version of this is already written and measured (see the verification
   section); this is the same three lines on the other transport.
4. Simulator, defence in depth: a cap on concurrent door connections. The real
   device accepts one client at a time, so a small ceiling is also better
   fidelity, and it turns the linear-in-sockets row of the table above into a
   constant.

---

### 2. [Low] Three per-frame log sites still fire on peer-controlled conditions with no throttle: ×46 write amplification at the attacker's byte rate, in the shipped client and the simulator

**Files:**
- `src/powerpetdoor/client.py:1647` — `_LOGGER.error("Failed to decode JSON
  frame (%s): %s", err, sanitize_text(frame))`, once per malformed frame, at
  ERROR, and it echoes the frame — **the shipped library**
- `src/powerpetdoor/client.py:1674` — `_LOGGER.warning("Ignoring malformed
  message from device: %s", json.dumps(msg))`, once per legal-JSON-but-empty
  frame
- `src/powerpetdoor/simulator/protocol.py:400` — `logger.warning("Simulator:
  JSON parse error: %s", err)`, the simulator's twin of the first
- `src/powerpetdoor/simulator/protocol.py:468,488` — `logger.warning("Simulator:
  Unknown command: %s", ...)`, once per unknown-command frame

This is the class Round 5 filed as Finding 1 and the project fixed with
`EventThrottle`. The fix was applied to the three *per-chunk* sites (framing
garbage, client non-ASCII, protocol non-ASCII) and is verified effective below.
These four are the *per-frame* sites, and none of them is throttled.

The shape matters more than the constant. Round 5's attack was limited by the
attacker's **packet** rate — one log line per TCP segment, so 20,000 pps bought
4.9 MB/s. These are limited by the attacker's **byte** rate: `{x}` is three
bytes and buys a 135-byte ERROR, so a peer can pack 21,845 of them into one
64 KiB write.

**Measured against a real `ppd-simulator --daemon`** (fresh daemon per attack,
log drained to quiescence before measuring):

| attack | attacker wire | log written | amplification | CPU | log rate |
|---|---|---|---|---|---|
| packed `{x}` | 2.69 MB | **124.5 MB** (1.2 M records) | **×46.3** | 21.6 s / 26.3 s (82% core) | 4.73 MB/s |
| packed `{x}` (repeat, 5 s) | 3.60 MB | **167.0 MB** (1,201,482 records) | ×46.3 | — | 5.8 MB/s sustained for 17 s *after* the attacker stopped |
| packed `{"cmd":"a"}` | 2.82 MB | 10.3 MB | ×3.6 | 119.8 s / 125.1 s (96% core) | — |
| packed `{}` | 3.60 MB | 161 B | ×0.0 | — | — |
| packed valid `GET_SETTINGS` | 3.21 MB | 85 B | ×0.0 | — | — |

**Measured against the shipped client over real TCP** (hostile door, real
`FileHandler` at WARNING):

| frame flooded | door sent | log written | records | amplification |
|---|---|---|---|---|
| `{x}` | 3.93 MB | **111.5 MB** | 679,594 | **×28.3** |
| `{}` | 2.88 MB | 53.9 MB | 550,091 | ×18.7 |
| `{"CMD":"GET_SETTINGS","success":"true"}` | 17.82 MB | 103 B | 1 | ×0.0 |

In-process with the Round-5 harness (30,000 chunks, WARNING), `{x}` is ×54.7
and `{}` is ×49.0, and packing 2,048 `{}` frames into each 4 KB read still gives
×49.0 — i.e. the ratio does not decay when the attacker uses realistic reads,
which is exactly what distinguishes it from the packet-rate attack.

The last row of each table is the Round-5 fix working: a legal envelope missing
its payload field produced a traceback per frame in Round 5 (×12.7); it now
produces one line in total.

**Why Low and not Medium:** availability-only, no confidentiality or integrity
impact, no wedge, and it stops the instant the peer stops. It is the same class
this project rated Low in Round 4 (×14.3) and Round 5 (×247), and consistency
with that calibration matters more than the fact that the achievable *rate* is
1–2 orders of magnitude higher here (4.73 MB/s sustained versus Round 5's
1.75 MB/s peak). The disk component is bounded in practice by log rotation on
the deployment targets. It is also strictly smaller than Finding 1, which the
same traffic triggers.

**Why not Informational:** unauthenticated, remote, zero interaction, trivially
repeatable, and it lands in a third party's log (Home Assistant on a Pi, SD
card). The audit dimension from Round 5 applies with more force: a peer that
can push 4.7 MB/s of its own noise can roll every other record out of a
size-capped journal in seconds. The daemon also kept writing for 17 s after the
attacker disconnected, so the flood outlives the connection that caused it.

**Recommendation:** the instrument already exists and is already held
per-connection. Give `FrameScanner`'s owner two more `EventThrottle`s — one for
"malformed frame" and one for "unknown command" — and flush them from the same
`disconnect()` / `connection_lost()` hooks that already flush `_non_ascii`. The
first occurrence still reports immediately, so nothing an operator needs is
lost. While there: `client.py:1647` echoes the whole frame at ERROR; a bounded
prefix (say 200 characters) would cut the constant substantially on its own,
since the frame is attacker-chosen and can be arbitrarily long up to the 64 KiB
cap.

---

### 3. [Informational] `EventThrottle`'s reporting interval doubles without a ceiling, so an ongoing attack on a long-lived connection eventually reports once per 2^k events

**File:** `src/powerpetdoor/framing.py:111-122` (`record`: `self._next *= 2`,
no upper bound), with `flush` at `:124-128`.

This is the answer to the assigned question "can `EventThrottle` be abused to
suppress evidence of an attack?" — the short answer is **no**, and this is the
one residual worth writing down so the next reader does not re-derive it.

What I verified, by execution:

- **The first occurrence of every event type is always reported.** `_next`
  starts at 1 and `reset()` restores it, so there is no state in which an event
  fires silently. Confirmed at every boundary: 100,000 events → 17 records,
  firing at exactly 1, 2, 4, …, 65,536.
- **Distinct event types are never conflated.** Each site owns its own
  `EventThrottle` instance (`FrameScanner._discards`, `client._non_ascii`,
  `protocol._non_ascii`), so inflating one cannot mute another. Explicitly
  tested: after 10,000 garbage feeds (14 records), a buffer overflow on the same
  scanner still produced its **unthrottled** ERROR plus the flushed garbage
  total — 2 records, neither suppressed.
- **A reconnect loop is louder, not quieter.** `reset()` restores immediate
  reporting, so every new connection reports its first event. Measured against
  a real daemon: 1,756 connect / 1-garbage-byte / close cycles in 5 s produced
  440,756 bytes of log — ×251 on the payload byte, but only ~88 KB/s, because
  it is bounded by the connection rate and each cycle costs the attacker a full
  TCP handshake. That is ~50× below Finding 2's flood and is inherent to any
  TCP server that logs connections.
- **The suppressed tail is never lost.** `flush()` emits it once and is
  idempotent (a second call adds nothing). Every client teardown path funnels
  through `disconnect()` (`client.py:1391-1395`) — `connection_lost` →
  `_on_transport_lost` → `disconnect`, `_drop_connection` → `disconnect`,
  keepalive give-up and explicit `stop()` likewise — and the simulator flushes
  in `connection_lost` (`protocol.py:340-341`).

What remains: on a connection that never ends, the cadence degrades without
bound. After 2^20 events the next report is at 2^21 (verified: 2^20 events →
21 records). The shipped client holds one connection to a door for weeks, so an
operator watching a compromised door that dribbles garbage sees the reports
thin out indefinitely while the counters sit in memory. Nothing is *hidden* —
the running totals are in the next report and in the flush — but "when did the
rate change?" becomes unanswerable. A one-line ceiling (`self._next = min(
self._next * 2, 4096)`) or a wall-clock floor on the reporting interval would
keep the log volume logarithmic where it matters and bounded where it does not.

Two related notes, neither a problem today:

- `extract_frames` (`framing.py:405`) constructs a fresh `FrameScanner`, and
  therefore a fresh `EventThrottle`, per call — 50 calls with garbage produce 50
  records. It is used only by tests and by its own docstring's "one-shot"
  contract, and the docstring already tells receivers to hold a `FrameScanner`
  instead; worth keeping that way.
- The unthrottled `_LOGGER.error` for buffer overflow (`framing.py:364`) is
  correct as-is: it costs the attacker 64 KiB per line, and it is the one
  framing event that ends the connection.

---

### 4. [Informational] The control channel has no client cap, so `MAX_CLIENT_BACKLOG` bounds memory *per client* but not in total

**File:** `src/powerpetdoor/simulator/cli.py:192,219` (the 1 MiB ceiling),
`:265` (`self.clients: set[asyncio.StreamWriter] = set()` — unbounded), `:279`
(`asyncio.start_server` with no connection limit).

Round 5's Finding 1 recommendation 4 is implemented and works (measurements in
the verification section). The ceiling is per writer, though, and the number of
writers is unbounded, so the daemon's worst case is *N* × 1 MiB where *N* is the
number of `ctl` sessions an attacker can open. This is gated by the control
channel's default bind of `127.0.0.1` and by `--control-host requires --daemon`
(`cli.py:1080-1081`), both re-verified, so reaching it requires the operator to
have deliberately exposed the channel with `--control-host 0.0.0.0` — which the
help text already labels UNAUTHENTICATED. Noted for completeness rather than as
something to change: anyone who can open control connections can also
`shutdown`, so it is not a privilege boundary.

---

### 5. [Informational] Supply chain: the lock is at head with zero advisories, and two new *binary* dev dependencies entered the tree with mypy 2.x

`uv.lock`, `pyproject.toml` `[build-system]`.

Round-5 Finding 3 is fully closed. Checked against PyPI and OSV.dev today
(2026-08-22):

- **All 27 locked packages plus `setuptools==84.0.0` / `wheel==0.48.0` are at
  the current PyPI latest.** Nothing is behind. `tzdata` — the library's only
  runtime dependency, and the one Round 5 called out at three releases stale —
  is at 2026.3, the head.
- **OSV.dev returns an empty `vulns` list for every pinned package/version
  pair.** Zero advisories, including for all eight packages the `uv lock
  --upgrade` moved.
- **Nothing yanked, nothing EOL.** Worth recording: `pytest-timeout` 2.5.0 *is*
  yanked upstream ("accidental breaking change"), which is why 2.4.0 is still
  index head — the lock is correct and should not be "upgraded" if a tool
  offers it. `sortedcontainers` 2.4.0 (2021, transitive under `hypothesis`) is
  the one dormant link in the tree; pure Python, dev-only, no advisories ever.
- The pins sit above five recent fixes that are directly relevant to this
  repo's build story: `wheel` GHSA-8rrh-rw8j-w5fx (HIGH, path traversal, fixed
  0.46.2), `setuptools` GHSA-h35f-9h28-mq5c (MODERATE, sdist exclusion bypass,
  fixed 83.0.0) and GHSA-5rjg-fvgr-3xxf (HIGH, path traversal, fixed 78.1.1),
  `pytest` GHSA-6w46-j5rx-g56g, `pygments` GHSA-5239-wwwm-4pmq. The
  `pyproject.toml` comment about build isolation pulling setuptools/wheel into
  the `id-token: write` job is exactly the exposure those two describe, and the
  exact pins are above both fixes.

The note worth carrying forward: **`ast-serialize` 0.8.0 and `librt` 0.15.0 are
new, and they ship compiled C extension wheels that are imported at type-check
time.** They are legitimate — `uv tree` shows both reached only via `mypy
v2.3.1`, mypy's own `pyproject.toml` at tag v2.3.1 declares both, and PEP 740
attestations on PyPI confirm they were published by Trusted Publishing from
`mypyc/ast_serialize` and `mypyc/librt`, the same GitHub org that owns `mypyc`
(better-attested than mypy itself, which carries no attestation). But they are
young (created 2025-09 and 2026-02), `Development Status :: 3 - Alpha`, and they
entered the tree as a side effect of the mypy 1.x → 2.x jump rather than as a
decision. They are dev-only and never reach a shipped artifact — the published
wheel depends on `tzdata>=2024.1` alone — so this is a BOM/provenance note for
the audit trail, not a request to change anything.

---

## Round 5 Fix Verification

**Round-5 Finding 1 (Low) — per-chunk log amplification: FIXED at the mechanism
level, on both sides, and the fix cannot be defeated by chunking, reconnecting
or event interleaving.**

*Shipped client, in-process, the Round-5 harness verbatim* (real
`logging.FileHandler`, level WARNING, 30,000 chunks each):

| chunk fed per `data_received` | wire | log bytes | records | amplification | Round 5 |
|---|---|---|---|---|---|
| `b"\x80"` (non-ASCII) | 30,000 | 4,698 | 30 | **×0.2** | ×247 |
| `b"x"` (garbage) | 30,000 | 1,940 | 15 | **×0.1** | ×115 |
| `b" "` (whitespace — control) | 30,000 | 0 | 0 | — | — |

CPU/byte fell with it: 31.3 → 1.90 µs for the non-ASCII dribble and 21.2 →
1.18 µs for garbage, against a 0.74 µs/byte whitespace floor. The 30-of-31
µs/byte that Round 5 attributed to the two log calls is gone.

*Real `ppd-simulator --daemon`, real TCP:*

| attack | attacker wire | log written | amplification | Round 5 |
|---|---|---|---|---|
| 1-byte non-ASCII, 200 µs pace | 19,351 B | 3,632 B (40 lines) | **×0.2** | ×180 |
| 1-byte garbage, 200 µs pace | 19,129 B | 1,869 B (24 lines) | **×0.1** | ×94 |
| 1-byte garbage, unpaced | **2.58 GB** at 516 MB/s | 1,734 B | ×0.0000007 | ×14 @ 1.75 MB/s |

The unpaced row is the one that closes it: the daemon absorbed 2.58 GB in five
seconds for 4.23 s of CPU (1.6 ns/byte) and 1,734 bytes of log.

*Throttle mechanics, verified by execution:* the doubling schedule fires at
exactly 1, 2, 4, …, 65,536 (100,000 events → 17 records); 10,000 garbage
`feed()` calls → 14 records; 1,000 events → 10 records; `flush()` emits the
tail exactly once and a second call is a no-op; `reset()` restores immediate
reporting; interleaving 20,000 valid frames with the garbage does not reset
`_next` (15 records for 20,000 events). The suppression analysis is Finding 3.

*Round-5 recommendation 3 (the seven unguarded payload indexings): FIXED.*
`_payload_mapping` (`client.py:699-710`) is used at all four mapping sites and
the remaining `msg[...]` reads are inside `if X in msg:` guards
(`client.py:813,819,843,847,856,864,911,929,937,945,955,967,988,996,1004,1012`).
`{"CMD":"GET_SETTINGS","success":"true"}` produced ×12.7 and a traceback per
frame in Round 5; 30,000 of them now produce **0 bytes** of log in-process, and
17.82 MB of them over real TCP produce **103 bytes / 1 line**.
`{"CMD":"GET_DOOR_STATUS","success":"true"}` likewise. A sweep of **6,000
randomized hostile frames** (recursive JSON values, `nan`/`inf`/10^40, 5,000-char
strings, ESC/BEL payloads, list/dict `CMD`, unusable `msgID`, non-dict payloads)
through the client with **every** listener registered and root level DEBUG:
**0 unhandled loop exceptions, 0 tracebacks, 0 raw ESC, 0 raw BEL**, and the
receive path kept dispatching afterwards.

*Round-5 recommendation 4 (`MAX_CLIENT_BACKLOG`): FIXED, and it is measured on
the right quantity.* `writer.transport.get_write_buffer_size()` is asyncio's
count of not-yet-sent bytes, which is precisely the daemon-side heap a stalled
reader owns. Under a `{x}` flood against a real daemon:

| ctl clients | daemon peak RSS growth | slow clients received | stalled client after resuming |
|---|---|---|---|
| none | +5.8 MB | — | — |
| 3 stalled (never read) | **+6.9 MB** | — | 196,608 B (connection kept) |
| 3 slow (4 KB / 50 ms) | +7.1 MB | 7.09 MB total | — |

Round 5 measured +0.16 MB/s with no bound in sight and 91% of a core; the
growth is now ~1.1 MB total for three stalled clients and it plateaus. **A slow
reader cannot amplify**: it is served normally while it keeps up, and the
ceiling applies the moment it does not. Records are dropped, not the connection
— a stalled client that resumes reading still receives.

**Round-5 Finding 2 (Informational) — unbounded script delays: FIXED.**
`inside`/`outside` `duration`, `wait` `seconds` and `wait_for` `timeout` all go
through `_script_number(..., 0, MAX_SCRIPT_DELAY)` (`scripting.py:407-409,
414-416, 427-429, 438-440`), the same helper `hold_time`, `battery`,
`add_schedule` and `remove_schedule` use.

**Round-5 Finding 3 (Informational) — dependency automation: FIXED.**
`.github/dependabot.yml` uses `directories:` with both `/` and
`/.github/actions/setup-uv-with-retry`, so the three `astral-sh/setup-uv` SHAs
in the composite action are covered. CLAUDE.md carries the "Manually tracked
pins (MANDATORY)" table naming the `.gitea` SHA and `uv.lock` transitives, with
the `tzdata` rationale spelled out. `uv lock --upgrade` has been run and the
lock is at head — details and the advisory scan are Finding 5. All **15 of 15**
`uses:` in the repo remain SHA-pinned with a version comment, including
`.gitea/workflows/sync-wiki.yml:8` at `5d9eb7fb…` — the only `uses:` that
receives a secret.

**Round-5 Finding 4 (Informational) — chunking-dependent `discarded`: FIXED.**
The resync branch now counts `sum(map(len, data[i:end].split()))`
(`framing.py:339`). Round 5's minimal case is gone:

```
stream '000000000000000 ' fed whole      -> discarded = 15   (was 16)
same stream cut before the trailing space -> discarded = 15
```

16,000 randomized chunkings of 4,000 hostile streams: **0 cases where the total
depended on where the peer cut the packet.**

**Round-4 S5 (Informational) — `.gitea` mutable tag: still FIXED**, pinned by
the coordinator to `5d9eb7fbdc4212eea790375b2ec82f421a30037a # v1.1.0`.

**New code — `FrameScanner`'s segment-list remainder: no change to cap
enforcement, and nothing can be smuggled.** 12,000 differential cases across
three cap values (`64`, `512`, `65536`), streams over the alphabet
`{}"\ \n\t abc:,10\x00\x7f` with up to 8 random cut points, comparing a chunked
`FrameScanner` against a one-shot `extract_frames` of the same stream, with
these invariants asserted after **every** `feed`:

- `self._retained == sum(len(p) for p in self._pieces) == len(self.buffer)` —
  the incremental accounting the cap is checked against never drifts from the
  actual segment list
- reading the `buffer` property (which coalesces the list in place) changes
  neither the total nor the accounting
- `len(buffer) <= max_buffer`
- `buffer != "" ⟹ scanner.open and buffer[0] == "{"` — the retained pieces
  always start at the in-progress object's opening brace, which is what makes
  `consumed = 0` correct on resume
- every delivered frame has balanced, string-aware braces and starts/ends with
  `{`/`}`

Result: **0 invariant violations in 12,000 cases**, and frames, remainder and
`overflow` were **byte-identical to the one-shot in all 10,534 cases where no
overflow occurred**. The 1,466 divergences all involve overflow at `cap=64` and
are divergence *by construction* — the cap applies to the retained remainder, so
where the cap fires depends on chunking; in every one of them the chunked
scanner was the more conservative of the two or delivered only genuinely
complete sub-cap frames.

The one documented edge is unchanged and quantified: the largest frame
deliverable is `cap + chunk_size`, exactly.

| cap | chunk | largest frame delivered | `cap + chunk` |
|---|---|---|---|
| 64 | 8 | 72 | 72 |
| 64 | 64 | 128 | 128 |
| 64 | 1024 | 1018 | 1088 |
| 65536 | 262144 | 259,520 | 327,680 |

Over real TCP the chunk is bounded by asyncio's 256 KiB read, so the transient
worst case per connection is 64 KiB + 256 KiB — the same bound Round 5
established. Targeted attempts: a frame split across feeds larger than the cap
is **not** smuggled (`overflow=True`, `frames=[]`); garbage is never retained
(1,000 garbage bytes + `{ab` retains exactly `{ab`); 100,000 whitespace
characters retain nothing and never overflow; the byte that crosses the cap
trips overflow and clears; feeding a continuation after an ignored overflow
yields only well-formed frames.

**New code — `ScriptQueue` cancellable claims and the `on_start` veto: no leak,
no wedge, no side effect.** The veto path (`cli.py:412-425`, `scripts.py:108-117`,
`scripting.py:317-319`) was forced 16 times against a real daemon by holding the
run lock with a synchronous `run longwait wait`, queueing a marker script (which
the consumer claims and parks on the lock), then issuing `stop all`:

```
list (claim parked)  -> Script: running "Long Wait" (1 queued) | Queued: Marker Script
stop all             -> OK: Stopping script: Long Wait (dropped 1 queued)
daemon log           -> Dropped queued script: 16   Running queued script: 0
marker's side effect -> hold_time still 2.0 (the vetoed script never executed)
after 16 vetoes      -> queue empty, "Script: none running"
runner still usable  -> OK: Script PASSED: Basic Door Cycle
tracebacks: 0   "Task exception was never retrieved": 0
```

A separate 60-iteration hammer (`run` + 3 queued + `stop all` at 20 different
delays) gave 60/60 `OK`, 0 tracebacks, an empty queue and a working runner
afterwards. `release()` is idempotent and `start()` calls it before the
`finally:` in `_process_script_queue` does, so no claim is double-removed and
none leaks. The only thing the veto path logs is the script's own name, through
`sanitize_text` — no path, no queue internals, nothing a caller who could `run`
did not already supply.

---

## Areas Reviewed With No Findings

- **`EventThrottle` cannot suppress, conflate or be reset into silence.**
  Covered in detail in Finding 3; the summary is that the first occurrence of
  every event type is unconditionally reported, throttles are per-site so
  inflating one cannot mute another, the unthrottled overflow ERROR still fires
  after 10,000 inflating events, `flush()` is idempotent and reachable from
  every teardown path on both sides, and a reconnect loop produces *more* log,
  not less.
- **Hostile wire input against the simulator, over real TCP.** 3,000 randomized
  hostile frames (recursive values under `cmd`/`config`/`PING` and thirteen
  payload fields, including `nan`/`inf`/10^30, 3,000-char strings, ESC/BEL,
  list/dict commands): **0 daemon tracebacks, 0 raw ESC, 0 raw BEL, 0 "message
  handler task failed"**. The Round-4 S2 fix (unhashable wire values as dict
  keys) and the Round-4 S4 schedule-index cap both hold.
- **Both schedule parsers and the tz helpers survive hostile input, and the new
  `"1"/"0"` flag emission is exact.** 20,000 randomized hostile schedule
  entries through `powerpetdoor.door.Schedule.from_dict` and the simulator's
  `state.Schedule.from_dict` + `to_dict`, plus 6,000 through
  `get_posix_tz_string` / `find_iana_for_posix`: **0 unexpected exceptions**
  (only the documented `ValueError`/`TypeError`/`KeyError`), and every emitted
  `enabled` was exactly `"1"` or `"0"` — never `True`, never `1`, never a
  string that `make_bool` would read as truthy by accident. Simulator → library
  round trip verified at indices 0, 1, 254 and 255.
- **`_extract_posix_from_tzif` cannot be walked out of the `tzdata` package.**
  `tz_utils.py:47-84` splits an IANA name on `/`, turns all but the last
  segment into a dotted package name and passes the last segment to
  `importlib.resources.files(package).joinpath(...)`. The resource name is by
  construction free of `/`, a traversal attempt makes `files()` raise
  `ModuleNotFoundError`, `".."` resolves to a directory whose `open()` raises,
  and the whole body is wrapped in `except Exception: return None`. The content
  is additionally rejected unless it starts with `TZif`.
- **No dangerous execution sinks.** Repo-wide grep over `src/` and `scripts/`
  for `eval(`, `exec(`, `pickle`, `subprocess`, `os.system`, `shell=True`,
  `__import__`, `marshal`, `shelve`: nothing.
- **YAML loading remains safe.** `scripting.py:153` is the only YAML entry point
  and uses `yaml.safe_load`; repo-wide there is no
  `yaml.load`/`full_load`/`unsafe_load`.
- **Terminal-escape sanitization still covers every egress.** Across every live
  run in this round — 3,000 hostile daemon frames, 6,000 hostile client frames
  at DEBUG, garbage and non-ASCII floods, 1,756 connect/close cycles, 16 script
  vetoes, ctl sessions — **0 raw ESC and 0 raw BEL** were observed on any
  stream. `sanitize.py`'s single `_CONTROL_CHAR_RE` covers C0 (except tab and
  newline), DEL and C1, and remains the only implementation.
- **The control-channel script-path restriction still holds.** Against a real
  daemon: `run ../../etc/passwd`, `run /etc/passwd`, `run .hidden`,
  `run ./basic_cycle` and `run scripts\basic_cycle` all return `ERROR: Script
  paths are not allowed over the control channel; use a bare script name (see
  'list')`. `_load_script_by_name` resolves against `--scripts-dir` and requires
  `candidate.parent == base` after `.resolve()`, defeating a planted symlink.
- **Bind addresses re-checked.** The only `0.0.0.0` occurrences remain the door
  server default (`server.py:116`, `cli.py:601,912`) and the `--control-host`
  help text, which carries the UNAUTHENTICATED warning. The control channel
  defaults to `127.0.0.1` and `cli.py:1080-1081` still refuses `--control-host`
  without `--daemon`. (Unrelated observation, not a security issue: `--port 0`
  is accepted and the startup banner then prints `listening on 127.0.0.1:0`
  rather than the bound port, so a script that greps the banner cannot find the
  ephemeral port.)
- **CI supply chain.** No `pull_request_target`, no `workflow_run`, no
  `ref:`/`repository:` override on any checkout. No secret is interpolated into
  any `run:` string; `CODECOV_TOKEN` is a job-level `env` consumed as an action
  input and guarded by `if: ${{ env.CODECOV_TOKEN != '' }}`. `permissions:`
  appears once — `id-token: write` on the `publish` job alone, with
  `environment: pypi`, `needs: test` and OIDC trusted publishing. The
  `TESTING_GAPS.md` commit-and-push step is fenced by
  `github.event_name == 'push'`, so a fork PR (which runs under `pull_request`
  with a read-only token) cannot reach it. Every job sets `timeout-minutes: 30`.
  `[build-system] requires = ["setuptools==84.0.0", "wheel==0.48.0"]` is still
  exactly pinned, and both pins sit above the 2025/2026 path-traversal and
  sdist-exclusion advisories for those projects.
- **History files are still owner-only.** `_create_private_file`
  (`commands/history.py:20-28`) creates with `os.open(..., 0o600)` and
  re-`chmod`s regardless of umask, before `FileHistory` touches the path. The
  later `open(filename, "w")` truncations (`history.py:174,199`,
  `info.py:366`) preserve the existing mode.
- **Fire-and-forget task tracking does not leak.** `_track_task` /
  `_create_task` register a done-callback that discards the task
  (`client.py:437-439`, `protocol.py:411-414`), so the tracking sets drain as
  the tasks finish; the growth measured in Finding 1 is live work in flight,
  not retained corpses, and RSS returns to baseline when the peer stops.
- **No secrets at rest or in logs.** The device protocol carries no
  credentials, keys or PII; neither the library nor the simulator stores or
  logs anything sensitive. There is nothing to rotate, redact or encrypt at
  rest — the encryption/key-management sections of this persona's brief remain
  not-applicable to this codebase, for the same reason as in rounds 1–5.
