# Security Analyst Analysis — Round 5

Scope: a fresh full-codebase security sweep of `pypowerpetdoor` at commit
`6f2dedd` — the shipped asyncio client library plus the device **simulator**
(door TCP protocol, unauthenticated control channel, YAML scripting,
interactive CLI/ctl) — after the Round-4 remediation (`FrameScanner` /
`_BraceScanner`, `_wire_schedule_index`, `ResponseHandlerRegistry.get`
isinstance guard, `_script_number`, script sanitization at source,
`_SanitizingFormatter` on `--script`/`--daemon`, schedule-slot cap,
`.github/dependabot.yml`) and the subsequent SHA pin of the `.gitea` reusable
workflow.

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
against real `ppd-simulator` / `ppd-simulator-ctl` processes where the claim
concerns process behavior. Throwaway harnesses were deleted, spawned processes
killed, and no repo file was modified.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 1 |
| Informational | 3 |

**All six Round-4 findings are fixed, and the two substantive ones are fixed
properly rather than papered over.** S1 is gone at the mechanism level, not
just at the benchmark: the crafted dribble payload now costs the daemon the
*same* as sending literal spaces (24.0 vs 22.7 µs/byte over real TCP), which is
the only measurement that proves the re-scan is gone rather than merely
cheaper. I could not desync `FrameScanner`, smuggle a frame past the cap, or
find a payload shape that beats one-character-per-character (4,000 randomized
differential cases plus six hand-built adversarial shapes; details in
"Round 4 Fix Verification").

The one new finding is what the S1 fix left standing. With the framing cost
removed, the dominant cost of a hostile packet is now **logging** it, and two
log sites fire once per `data_received` on a peer-controlled condition with no
rate limit. That is Finding 1, and it reaches the shipped library. It is the
same class as Round-4's S2 (which was Low at ×14.3 amplification), 17× larger,
and unlike S2 it never self-limits.

---

## Findings

### 1. [Low] Two per-chunk log sites fire on peer-controlled conditions with no rate limit: ×115–247 write amplification, no self-limiting, in the shipped client and the simulator

**Files:**
- `src/powerpetdoor/framing.py:245-248` — `if diag.discarded: _LOGGER.warning(...)`,
  once per `feed()` that discarded anything
- `src/powerpetdoor/client.py:1554-1557` — `if len(decoded) != len(data):
  _LOGGER.error("Received non-ASCII bytes from device; escaped them ...")`,
  once per `data_received` — **the shipped library**, at ERROR
- `src/powerpetdoor/simulator/protocol.py:367-368` — the simulator's twin
  (`logger.warning("Simulator: escaped non-ASCII bytes in %d received bytes")`)
- Secondary site: `src/powerpetdoor/client.py:1647-1650` —
  `_LOGGER.exception("Error handling %s response: %s", ...)`, a full traceback
  per malformed-but-plausible frame, because `_handle_get_settings`
  (`client.py:702`) and `_handle_door_status` (`client.py:694`) index
  `msg[FIELD_SETTINGS]` / `msg[FIELD_DOOR_STATUS]` directly
- Fan-out multiplier: `src/powerpetdoor/simulator/cli.py:188-216`
  (`_ControlLogHandler.emit` re-formats and broadcasts every root record to
  every control client, with no `drain` and no write-buffer limit)

A peer that sends one byte per TCP segment gets one log line per byte. `0x80`
gets **two** (the non-ASCII notice, then the `\x80` escape text is garbage to
the framer, so the discard warning fires as well). Neither site has a rate
limit, a dedup, or a per-connection cap, and — unlike the 64 KiB overflow path
— **nothing accumulates**, so the connection is never dropped and the attack
runs indefinitely at zero cost to the attacker.

**Measured against the shipped client** (`PowerPetDoorClient.data_received`,
real `logging.FileHandler`, level WARNING, 30,000 chunks each):

| chunk fed per `data_received` | CPU/byte | log bytes/byte |
|---|---|---|
| `b"\x80"` (non-ASCII) | 31.3 µs | **247** |
| `b"x"` (garbage) | 21.2 µs | **115** |
| `b" "` (whitespace — same chunking, no log) | 1.06 µs | 0 |
| valid frames in 4 KB reads | 0.11 µs | 0 |

The whitespace row is the control: identical chunking, identical framing work,
no log. **30 of the 31 µs/byte and all of the log volume come from the two log
calls.** A door dribbling at 5,000 packets/s costs its client 16% of a core and
1.2 MB/s of log; at 20,000 pps, 63% of a core and 4.9 MB/s.

**Measured against a real `ppd-simulator --daemon`** (5 s per attack, log to a
file, `/proc/<pid>/stat` for CPU):

| attack | attacker wire | log written | amplification | log rate |
|---|---|---|---|---|
| 1-byte non-ASCII, 200 µs pace | 21.8 KB | 3.92 MB | **×180** | 675 KB/s |
| 1-byte garbage, 200 µs pace | 22.9 KB | 2.15 MB | ×94 | 370 KB/s |
| 1-byte garbage, unpaced | 725 KB | 10.16 MB | ×14 | **1.75 MB/s** |
| packed `{"cmd":"NOPE"}` frames | 5.07 MB | 3.56 MB | ×0.7 | 654 KB/s |
| packed `{"config":"GET_SETTINGS"}` (valid) | 2.78 MB | 0.00 MB | — | — |

The last two rows are the calibration Round 4 already had: flooding *frames* is
a poor log attack because frames cost bytes. Flooding *packets* is a good one,
because the log line is per packet.

With three control-channel clients attached and not reading (the normal state
of a `ctl -i` session parked in a terminal), the same 4.5 KB/s attack costs the
daemon **22.72 s of CPU over 25 s wall — 91% of one core** (0.2 ms/byte, an
order of magnitude above the 61 µs/byte with no ctl clients), because every
record is re-formatted, sanitized, escaped and written to each stalled
transport. Daemon RSS grew 33 → 37 MB over those 25 s (~0.16 MB/s, tracking the
log rate) with no bound in sight, and 3.96 MB of log was produced from 114 KB
of attacker traffic. The daemon stayed responsive throughout (a fresh `ctl`
connection answered `status` in 0.00 s).

The secondary site is smaller but the same shape: `{"CMD":"GET_SETTINGS",
"success":"true"}` — 39 bytes, a legal envelope missing one field — produces a
full ERROR traceback per frame, ×12.7. Measured in-process, 2,000 frames →
994,000 bytes of log, 2,000 tracebacks. `{"CMD":"GET_DOOR_STATUS",
"success":"true"}` is ×12.1. This is the *unintended* branch: the code
immediately below it (`client.py:1651-1655`) already has a graceful "Response
missing expected field" path for exactly this case, which the `KeyError` jumps
over.

**Why Low and not Medium:** availability-only. No confidentiality or integrity
impact, no state corruption, no wedge; both components stop the instant the
peer stops; both peers stayed responsive in every run. The CPU component is
roughly a 3× multiplier on a floor that any 1-byte-packet peer imposes anyway
(a whitespace dribble already costs the daemon 22.7 µs/byte over real TCP), so
logging worsens a packet-rate attack rather than creating one, and it is ~40×
below the S1 bug that was rated Medium. The disk component is bounded in
practice by log rotation on the deployment targets. This is also the same class
this reviewer rated Low in Round 4 at ×14.3, and consistency with that
calibration matters more than the larger constant.

**Why not Informational:** unauthenticated, remote, zero interaction, trivially
repeatable, and it lands in the *shipped library* where the log is a third
party's (Home Assistant on a Pi, SD card). ×247 is the largest amplification
factor now in the codebase — 17× the ×14.3 that this project already judged
worth fixing — and it is the only untrusted-input path with no self-limiting
whatever. It also has an audit dimension: a remote peer that can push 1.75 MB/s
of its own noise can roll every other record out of a size-capped journal at
will.

**Recommendation:** rate-limit or aggregate, rather than suppress. Both sites
already have a natural per-connection home.

1. `FrameScanner` holds per-connection state — accumulate `discarded` there and
   log once per connection (first occurrence), then a total on `reset()`/
   overflow, instead of once per `feed`. The diagnostics return value is
   unchanged, so callers and tests are unaffected.
2. Same for the non-ASCII notice: it is per-connection information ("this peer
   is not speaking ASCII"), not per-chunk. Log it once per connection in both
   `client.py` and `protocol.py` and count the rest.
3. For the secondary site, read the two fields with `.get()` (or
   `require`-style) so a missing field takes the existing
   `CommandError("Response missing expected field")` path instead of raising a
   `KeyError` into `_LOGGER.exception`. The traceback carries no information a
   one-line warning naming the field would not.
4. Optional, defence in depth for the fan-out: `_ControlLogHandler.emit` could
   drop records for a writer whose `transport.get_write_buffer_size()` exceeds
   a threshold, which also caps the RSS growth measured above.

---

### 2. [Informational] `inside`/`outside`/`wait`/`wait_for` are the last unbounded numeric script values; `.nan` yields an unhandled-task traceback and a silently skipped step that still reports PASSED

**Files:** `src/powerpetdoor/simulator/scripting.py:391` and `:397`
(`duration = float(params.get("duration", 0.5))`), `:407`
(`seconds = float(params.get("seconds", 1.0))`), `:416`
(`timeout = float(params.get("timeout", 30.0))`) — versus `_script_number`
(`scripting.py:593-614`), which `hold_time`, `battery`, `add_schedule` and
`remove_schedule` now go through.

Round 4's S3 bounded every script value that can reach *wire-visible state*.
These four are the remainder. Verified with real `ppd-simulator --script
<file> --oneshot` runs:

```
duration: .inf  -> "Inside sensor activated for infs"; timer never fires; rc=0 PASSED
duration: .nan  -> asyncio.sleep raises ValueError("Invalid delay: NaN") inside
                   engine._deactivate_sensor_after; unhandled-task traceback;
                   sensor stays active forever; rc=0 PASSED
wait: .nan      -> returns after ~1.0s; script continues; rc=0 PASSED
wait_for timeout: .nan -> "Timeout waiting for condition"; rc=1 FAILED (clean)
```

**Not a vulnerability.** The damage does not reach the wire: the affected state
is `inside_sensor_active` (a bool), and the end state is identical to the
documented `pet_presence` action, so a hostile script gains nothing it could
not ask for directly. `asyncio` itself rejects NaN delays, so the event loop's
timer heap is never handed a NaN — the worry that motivated checking this is
closed. What remains is a false green (a step whose effect silently did not
happen while the run reports PASSED, which is the CI signal Round 4's H1 was
about) and a stack trace in the operator's log. One-line fix: run all four
through `_script_number(..., 0, <ceiling>)` for the same treatment
`hold_time` gets.

---

### 3. [Informational] The new Dependabot config cannot see the composite action or the `.gitea` workflow, and `uv.lock` is already 8 packages behind — `tzdata`, the only runtime dependency, by three releases

**Files:** `.github/dependabot.yml` (`github-actions`, `directory: "/"`),
`.github/actions/setup-uv-with-retry/action.yml:20,32,42` (three
`astral-sh/setup-uv@37802adc…` pins), `.gitea/workflows/sync-wiki.yml:8`,
`uv.lock`

Round-4's S6 fix is real and well-scoped, but two of the repo's pinned
references sit outside what it can reach:

- Dependabot's `github-actions` ecosystem scans `.github/workflows/` plus
  `action.yml` in the configured directory; it does **not** descend into
  composite actions in subdirectories. dependabot-core issue #7495
  ("Dependabot doesn't update composite action file") is closed **as not
  planned**, so this is settled behavior, not a bug awaiting a fix. The three
  `setup-uv` pins in `.github/actions/setup-uv-with-retry/action.yml` are
  therefore not covered. Fix: add that path to the config (a second
  `github-actions` entry with `directory: "/.github/actions/setup-uv-with-retry"`,
  or a `directories:` list).
- `.gitea/workflows/` is outside Dependabot's reach entirely. Now that S5's fix
  has pinned it to a SHA, that pin is a manual-update item by construction —
  worth stating explicitly so it does not silently rot, since it is still the
  only `uses:` in the repo that receives a secret.

Checked against PyPI today, the lock is partly stale despite having been
written 2026-08-21:

```
tzdata            2025.3  -> 2026.3   (released 2026-07-10; 3 releases behind)
wcwidth           0.2.14  -> 0.8.2
packaging         25.0    -> 26.3
pygments          2.19.2  -> 2.21.0
pytest            9.0.2   -> 9.1.1
pytest-asyncio    1.3.0   -> 1.4.0
prompt-toolkit    3.0.52  -> 3.0.53
typing-extensions 4.15.0  -> 4.16.0
(20 of 28 packages are at head)
```

Round 4 inferred "resolved at head-of-index" from the newest `upload-time` in
the lock; that heuristic reads the newest *added* package, not the oldest
un-refreshed one. `uv sync` does not upgrade what is already pinned, so entries
drift until something forces a re-resolve.

**No CVEs and nothing EOL.** `pyyaml` 6.0.3 carries no direct advisory
(2026's PyYAML-adjacent CVE, CVE-2026-24009, is an *unsafe-loader* bug in
Docling; this project uses `yaml.safe_load` exclusively — re-verified below).
The one with functional weight is `tzdata`: it is the library's only runtime
dependency and the whole `tz_utils`/schedule feature reads IANA rules from it.
Impact is limited because `dependencies = ["tzdata>=2024.1"]` means library
*consumers* resolve it fresh — only CI and local dev run on the ~8-month-old
database, so a 2026 DST rule change is untested rather than shipped. Worth a
`uv lock --upgrade` (the weekly cron job that already runs the fuzz suite is a
natural home) as the belt-and-braces for transitive pins Dependabot's
direct-dependency grouping may not move.

---

### 4. [Informational] `FrameDiagnostics.discarded` is chunking-dependent — whitespace inside a garbage run is counted or not depending on where the peer cuts the packet

**File:** `src/powerpetdoor/framing.py:228-235` — the resync branch counts
`next_obj - i` (or `n - i`) bytes, which includes any whitespace between the
garbage and the next `{`; the whitespace *skip* branch at `:221-225` does not
count.

Found while checking the assigned question "can a peer make discarded-byte
accounting drift?". It can, by exactly the number of whitespace characters that
land inside a garbage run. Minimal case, from a differential harness over 4,000
random hostile streams:

```
stream '000000000000000 ' fed whole      -> discarded = 16
same stream cut before the trailing space -> discarded = 15
```

**Not a vulnerability, and not new** — the pre-`FrameScanner` `extract_frames`
resynced the same way. The counter feeds one warning message and nothing else:
no threshold, no disconnect, no metric, no test asserts an exact total across
chunkings. Frames, remainder and `overflow` are all **bit-identical** across
every chunking tried, which is the property that actually matters and which
holds. Noted only so the next reader does not re-derive it; if it is ever
tightened, counting only non-whitespace in the resync branch makes the counter
chunking-invariant.

---

## Round 4 Fix Verification

**Round-4 S1 (Medium) — quadratic framing re-scan: FIXED at the mechanism
level, and I could not defeat it.** Three independent kinds of evidence:

*1. Character accounting is exact, for every payload shape I could construct.*
Instrumenting `_BraceScanner.scan` with a character counter, 64 KB of each
adversarial shape delivered **one byte per call**:

| payload (1-byte chunks) | bytes | characters examined | CPU |
|---|---|---|---|
| `{"a":"bbbb…` (the S1 repro) | 41,513 | 41,513 | 0.095 s |
| `{{{{{…` (deep nesting) | 65,000 | 65,000 | 0.165 s |
| `{"xxxx…` (string-heavy) | 64,993 | 64,993 | 0.171 s |
| `{"\\\\\\\\…` (escape-heavy) | 64,002 | 64,002 | 0.158 s |
| `{""""…` (quote toggling) | 64,001 | 64,001 | 0.156 s |
| `{"{}{}{}…` (braces inside a string) | 64,002 | 64,002 | 0.141 s |
| `{}{}{}…` (32,000 tiny frames) | 64,000 | 64,000 | 0.101 s |
| `x{}x{}…` (interleaved garbage) | 63,000 | 42,000 | 0.100 s |
| `xxxx…` (pure garbage) | 65,000 | **0** | 0.085 s |

Every row is `examined == len(payload)` exactly (garbage and whitespace are
skipped without entering the scanner, so they are *below* one pass). Round 4's
counter went 8,030,028 → 4,007 on its repro; the same repro at full buffer
depth is 65,001 examinations where the old code needed ~2.1 billion. The chunk
sweep that used to show the attacker choosing the exponent is flat:
`chunk=1 → 0.156 s`, `chunk=16 → 0.019 s`, `chunk=256 → 0.009 s` for the same
65,000 bytes — the residual is per-call overhead and the `buffer + data`
concatenation, not re-scanning.

*2. Over real TCP, the crafted payload costs the same as literal whitespace.*
This is the measurement that matters, because it isolates framing from the
per-packet floor. Real `ppd-simulator --daemon`, 15,000 one-byte writes paced
200 µs apart (Round 4's exact repro pacing), daemon CPU from `/proc`:

```
unterminated-object dribble   0.360 s   24.00 us/byte   <- Round 4: 219 us/byte
whitespace dribble (no-op)    0.340 s   22.67 us/byte   <- the floor
deep-nesting dribble          0.420 s   28.00 us/byte
unpaced (coalesced)           0.030 s    2.00 us/byte
```

The attacker's payload is within 1.3 µs/byte of sending spaces. There is no
framing amplification left to find. Client-side, in-process: dribbling
1.82 µs/byte versus 1.13 µs/byte for *valid frames delivered one byte at a
time* — a 1.6× ratio, against Round 4's 1,260 µs/byte.

*3. The carried state cannot be desynced, and nothing gets past the cap.*
4,000 randomized differential cases (alphabet `{}"\ \n abc:,10`, up to 10
random cut points per stream) comparing a chunked `FrameScanner` against a
one-shot `extract_frames` of the same stream: **frames, remainder and
`overflow` identical in every case**, with these invariants asserted after
every `feed`:

- `_scanned == len(buffer)` (the retained prefix is provably never re-examined)
- `buffer != "" ⟹ scanner.open and buffer[0] == "{"` (the retained buffer
  always starts at the in-progress object's opening brace, so `consumed = 0` on
  resume is correct)
- `len(buffer) <= max_buffer`

2,000 further cases at `max_buffer=32` (to exercise overflow constantly) held
the same invariants and produced only balanced-brace frames. Targeted attempts:
a frame split across feeds larger than the cap is **not** smuggled through
(`overflow=True`, `frames=[]`); a completed frame in the same feed as an
overflow is delivered and the overflow still reported; feeding an attacker's
continuation after an ignored overflow yields only well-formed frames (the
reset discards to the next `{`). Over real TCP the simulator drops the
connection at ~69,632 unterminated bytes, and a 200 KB / 400 KB single-write
frame is dropped rather than accepted — the kernel splits it across reads, so
the retained buffer trips the cap first. On the client, overflow →
`_drop_connection()` → `disconnect()` → `_scanner.reset()` (`client.py:1333`),
and a reconnect starts clean: verified that a half-frame left before an
overflow cannot merge with the next connection's first frame.

Both consumers hold one scanner per connection (`client.py:327`,
`protocol.py:308`), and the simulator's is per-`DoorSimulatorProtocol`
instance, so there is no cross-connection state at all.

**Round-4 S2 (Low) — unhashable wire values as dict keys: FIXED at all three
sites.** Against a real daemon over real TCP:

```
{"config":"GET_SCHEDULE","index":[1]}       -> reason "index must be a number, got [1]"
{"config":"GET_SCHEDULE","index":{"a":1}}   -> reason "index must be a number, got {'a': 1}"
{"cmd":"DELETE_SCHEDULE","index":[1,2]}     -> reason "index must be a number, got [1, 2]"
{"cmd":"DELETE_SCHEDULE","index":{"k":"v"}} -> reason "index must be a number, got {'k': 'v'}"
{"config":"GET_SCHEDULE","index":1e400}     -> reason "index must be a finite number, got inf"
{"config":"GET_SCHEDULE","index":"3"}       -> reason "index must be a number, got '3'"
{"config":"GET_SCHEDULE","index":256}       -> reason "index must be between 0 and 255, got 256"
daemon tracebacks: 0     raw ESC in daemon output: 0
```

The reasons are now specific rather than "Command failed", and the ×14.3
traceback flood is gone. Client-side, 24 hostile frames (list/dict/int/null
`CMD`, unusable `msgID`, missing payloads, non-dict `settings`, malformed
`schedules`, a 5,000-char timezone, a bare array) produced **0 unhandled task
exceptions**, 0 raw ESC/BEL, and the receive path kept dispatching afterwards.

**Round-4 S3 (Low) — YAML script channel: FIXED, end to end.** The exact PoC
from Round 4, run through real `ppd-simulator --script <file> --oneshot`:

```
stdout: ESC=0 BEL=0     stderr: ESC=0 BEL=0     rc=1  (was rc=0 PASSED)
>>> Running script: \x1b[2J\x1b[1;1H*** PWNED-NAME ***\x07
[INFO]   [SCRIPT] \x1b[2JPWNED-LOG
[ERROR] Script error at step 2: hold_time must be a finite number, got inf
```

Round 4 measured 4 ESC + 2 BEL on stdout and 8 + 2 on stderr with `rc=0
PASSED`; both streams are now clean and the run fails as it should. Escapes in
*step parameters* (not just name/description/log) are covered too — a script
with `\e` in a `set` name and an `assert` condition rendered as
`set(name=\x1b[31mbogus\x1b[0m, value=\x1b[2Jx)` with 0 raw ESC. `1e400`,
`nan`, `99999999999` battery and out-of-range schedule indices are all rejected
by `_script_number`, and `battery` now goes through `simulator.set_battery()`
so the 0–100 clamp applies.

**Round-4 S4 (Informational) — schedule index cap: FIXED.** With all 256 legal
slots filled from the wire:

```
schedule add inside 06:00-22:00   -> ERROR: No free schedule slots
schedule list                     -> 256 entries, max index 255
schedule remove 10                -> OK
schedule add inside 06:00-22:00   -> OK: Added schedule #10   (reuses the freed slot)
```

Index 256 can no longer be created; 0 tracebacks, 0 ESC in the daemon log.

**Round-4 S5 (Informational) — mutable-tag reusable workflow: FIXED.**
`.gitea/workflows/sync-wiki.yml:8` is now
`neuromancy/workflows/.gitea/workflows/sync-github-wiki.yml@5d9eb7fbdc4212eea790375b2ec82f421a30037a # v1.1.0`.
All **15 of 15** `uses:` in the repo are SHA-pinned with a version comment; the
only `uses:` receiving a secret is no longer movable by a tag push. (Its
ongoing maintenance is Finding 3.)

**Round-4 S6 (Informational) — no dependency automation: FIXED, with two gaps
outside its reach (Finding 3).** `.github/dependabot.yml` covers
`github-actions` and `uv` weekly with sensible grouping and commit prefixes.
All five install steps still use `uv sync --locked --all-extras`
(`test.yml:54,81,109,136`, `release.yml:27`); no `pip install` anywhere in
`.github/` or `.gitea/`; `uv.lock` carries 544 `sha256` hashes for 544
artifacts, 28 packages, all from the PyPI registry, one editable source (the
project itself), no git/URL/path sources.

---

## Areas Reviewed With No Findings

- **`FrameScanner`'s carried state is sound, and the one asymmetry left in the
  connection shim is unreachable.** `_ConnectionAttempt.data_received`
  (`client.py:1796-1799`) forwards on `self._adopted` alone, while
  `connection_lost` (`:1785-1790`) additionally checks transport identity. I
  tried to turn that into a cross-connection scanner desync and could not:
  `_transport` is only ever replaced after `disconnect()` has called
  `transport.close()`, and asyncio removes the socket reader synchronously in
  `close()`, so an adopted-then-superseded transport can never deliver another
  `data_received`. `disconnect()` also calls `_scanner.reset()`
  (`client.py:1333`) before any new connection can be adopted. Adding the same
  identity check to `data_received` would cost one line and make the invariant
  local rather than argued, but nothing is exploitable today.
- **`stop all` / `ScriptQueue` expose nothing new over the control channel.**
  Verified end to end against a real daemon: `run full_test_suite` then three
  `run basic_cycle` → `list` reports `Queued: basic_cycle, basic_cycle,
  basic_cycle`; `stop all` → `OK: Stopping script: Full Test Suite (dropped 3
  queued)`; `list` then shows `Script: stopping "Full Test Suite"` and an empty
  queue; the daemon logs `Script error at step 4: Script stopped while
  waiting`. On an idle simulator both `stop` and `stop all` return `ERROR: No
  script is running (use 'shutdown' to stop the simulator)` **without** calling
  `ScriptRunner.stop()`, so neither can poison a later run. The only state the
  queue adds to the channel is script names and a depth, all available to any
  caller who could already `run` and `shutdown`. `ScriptQueue.put` is unbounded,
  but a caller who can queue can also `shutdown`, so it is not a privilege
  boundary. `clear()` deliberately spares the claimed entry (the run that has
  already started), which matches what `stop` does to it.
- **The control-channel script-path restriction still holds.** Against the real
  daemon: `run ../../etc/passwd`, `run /etc/passwd` and `run .hidden` all
  return `ERROR: Script paths are not allowed over the control channel; use a
  bare script name (see 'list')`. `_load_script_by_name` resolves against
  `--scripts-dir` and requires `candidate.parent == base` after `.resolve()`,
  defeating a planted symlink. ctl now calls `set_script_paths_allowed(False)`
  at startup (`ctl.py:721-726`), so its completer no longer offers paths the
  daemon refuses.
- **Both schedule parsers and the tz helpers survive hostile input.** 16,000
  randomized payloads (recursive JSON values including `nan`/`inf`/10^30
  integers/nested containers under every schedule key) through
  `powerpetdoor.door.Schedule.from_dict`, the simulator's
  `state.Schedule.from_dict` + `to_dict`, `get_posix_tz_string` and
  `find_iana_for_posix`: **0 unexpected exceptions** (only the documented
  `ValueError`/`TypeError`). The Round-4 move of the coercers into
  `powerpetdoor.schedule` is pure code motion — I diffed the bodies; nothing
  was loosened, and both parsers now share one implementation. Simulator →
  library round trip at `MAX_SCHEDULE_INDEX` is clean.
- **The 64 KiB cap does what it claims, and its one documented edge is
  harmless.** The cap is on the *retained* buffer, so a frame that completes
  inside a single `feed` can exceed it (208 bytes with a 64-byte cap, in a unit
  test). Over real TCP that ceiling is the transport read size — asyncio's
  socket transport reads at most 256 KB per callback — and a 200 KB or 400 KB
  single-write frame was **dropped**, not accepted, because the kernel split it
  across reads and the retained buffer tripped the cap first. Transient
  worst-case memory per connection is bounded by cap + read size.
- **Terminal-escape sanitization still covers every egress.** Re-audited the
  `logger.*` / `print` / `status_print` / `writer.write` sites and traced
  arguments to their sources. Across all live runs in this round — hostile wire
  payloads, garbage floods, non-ASCII floods, the YAML PoC on both the
  `--script` and `--daemon` paths, ctl sessions — **0 raw ESC and 0 BEL** were
  observed on any stream. `_SanitizingFormatter` is now installed in `main()`
  (`cli.py:997-998`) as well as on the two interactive paths, so headless and
  daemon modes are covered by construction. `format_output`
  (`prompt_common.py:713-718`) now routes the history-recall line through
  `render_result` too, closing the second path.
- **YAML loading remains safe.** `scripting.py:142` is the only YAML entry point
  and uses `yaml.safe_load`; repo-wide there is no
  `yaml.load`/`full_load`/`unsafe_load`. This matters more than usual this
  year: 2026's PyYAML-adjacent RCE (CVE-2026-24009, Docling) is precisely an
  unsafe-loader bug, and this project is not exposed to it.
- **No dangerous execution sinks.** Repo-wide grep over `src/` and `scripts/`
  for `eval(`, `exec(`, `pickle`, `subprocess`, `os.system`, `shell=True`,
  `__import__`, `marshal`, `shelve`: nothing.
  `scripts/generate_gaps_report.py` — the only other code CI executes — reads
  `coverage.json` and writes `tests/TESTING_GAPS.md`, with no network or
  process calls.
- **Bind addresses re-checked.** The only `0.0.0.0` occurrences remain the door
  server default (`server.py:116`, `cli.py:568,879`) and the `--control-host`
  help text, which carries the UNAUTHENTICATED warning. The control channel
  defaults to `127.0.0.1`, and `ppd-simulator --control-host 0.0.0.0` without
  `--daemon` still exits with `error: --control-host requires --daemon`.
- **CI supply chain.** No `pull_request_target`, no `workflow_run`, no
  `ref:`/`repository:` override on any checkout. No secret is interpolated into
  any `run:` string; `CODECOV_TOKEN` is an action input guarded by a non-empty
  check. `permissions:` appears once — `id-token: write` on the `publish` job
  alone, with `environment: pypi`, `needs: test`, and OIDC trusted publishing.
  Every job sets `timeout-minutes: 30`. `[build-system] requires =
  ["setuptools==84.0.0", "wheel==0.48.0"]` is still exactly pinned, which is
  what keeps build isolation from pulling an unpinned backend into the signing
  job.
- **History files are still owner-only.** `_create_private_file`
  (`commands/history.py:20-28`) creates with `os.open(..., 0o600)` and
  re-`chmod`s regardless of umask, before `FileHistory` touches the path. The
  later `open(filename, "w")` truncations (`history.py:174,199`,
  `info.py:366`) preserve the existing mode; they could only create a 0644 file
  if it were deleted mid-session, and the content is simulator commands, not
  secrets.
- **No secrets at rest or in logs.** The device protocol carries no
  credentials, keys or PII; neither the library nor the simulator stores or
  logs anything sensitive. There is nothing to rotate, redact, or encrypt at
  rest — the encryption/key-management sections of this persona's brief remain
  not-applicable to this codebase, for the same reason as in rounds 1–4.
