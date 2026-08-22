# Security Analyst Analysis — Round 4

Scope: a fresh full-codebase security sweep of `pypowerpetdoor` at commit
`f9b9b59` — the shipped asyncio client library plus the device **simulator**
(door TCP protocol, unauthenticated control channel, YAML scripting,
interactive CLI/ctl) — after the Round-3 remediation and the subsequent
changes (`sanitize.py`, `WireValueError` + `_coerce_wire_*`, `render_result`,
the per-attempt `_ConnectionAttempt` shim, `aclose()`, control-channel
dead-writer reaping and log re-entrancy guard, ctl `LOG:` streaming to stderr,
the `stop` command, `uv sync --locked`).

Threat model (unchanged):

- **LAN attacker** who can open TCP connections to a running simulator (door
  port, default bind `0.0.0.0:3000`, and/or the daemon control port).
- **Malicious / compromised door** (or a MITM on the plaintext LAN protocol)
  that a `PowerPetDoorClient` / `PowerPetDoor` has connected to.
- **Malicious / untrusted YAML script file** fed to the simulator.

The device wire protocol is plaintext JSON-over-TCP with no auth/crypto **by
hardware design** — findings are about how the library/simulator *handle* that
untrusted data.

Every finding below was reproduced by executing the current code, over real
TCP sockets against real `ppd-simulator` / `ppd-simulator-ctl` processes where
the finding concerns process behavior. Throwaway harnesses were deleted and no
repo file was modified.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 2 |
| Informational | 3 |

**All four Round-3 findings are fixed, and the fixes are good.** Every
Round-3 reproduction now fails to reproduce (details in "Round 3 Fix
Verification"), and I could not get anything past the new `_coerce_wire_*`
validators on any path — including `SET_SCHEDULE_LIST`, the batch paths and
the operator/CLI path. The new code (connection shim, `aclose`, dead-writer
reaping, `stop`, ctl log streaming) opened **nothing**: no leak, no wedge, no
new egress.

The one substantive new finding is not a residual of anything Round 1–3
looked at. Round 1 bounded the receive buffer's **memory**; nobody has looked
at the **CPU** cost of reaching that bound, and it is quadratic. That is
Finding 1, and it is the only finding that reaches the shipped artifact with
real force. Findings 2 and 3 are the last two corners of patterns this
codebase has already established elsewhere: "guard a wire value before using
it as a dict key" (done for `msgID`, not for `CMD`/`index`) and "hold every
untrusted input channel to the same standard" (done for the wire and the CLI,
not for YAML scripts).

---

## Findings

### 1. [Medium] Quadratic re-scan of the retained frame buffer: ~1000× CPU amplification against the shipped client and the simulator's unauthenticated door port

**Files:**
- `src/powerpetdoor/framing.py:73` — `for i, c in enumerate(s)` in
  `find_frame_end`, which always scans from index 0
- `src/powerpetdoor/framing.py:141-143` — `end = find_frame_end(buffer)` /
  `if end is None: break`; the incomplete buffer is returned intact and the
  next call re-scans all of it from the start
- `src/powerpetdoor/framing.py:33` — `MAX_BUFFER_SIZE = 64 * 1024` (bounds
  memory; does **not** bound the work done getting there)
- Callers: `src/powerpetdoor/client.py:1483`
  (`frames, self._buffer, diag = extract_frames(self._buffer + decoded)`) —
  **the shipped library** — and
  `src/powerpetdoor/simulator/protocol.py:364`

`extract_frames` is called once per `data_received`. When the buffer holds no
complete object, `find_frame_end` rescans the entire retained buffer from
character 0 and returns `None`. A peer that dribbles the bytes of one
never-terminated JSON object therefore costs O(N²/C) character steps, where N
is the bytes delivered and C the bytes per `data_received` call. The attacker
chooses C.

**Attack scenario (verified over real paced TCP, both directions):**

*Simulator side* — an unauthenticated LAN peer opens the door port (default
`0.0.0.0:3000`), sends `{"a":"`, then one byte every 200 µs:

```
attacker: 15000 paced 1-byte writes (15 KB) over 3.8s wall
daemon CPU: 3.28s  ->  86% of one core, 0.219 ms CPU per attacker byte
```

Four such peers (≈10 KB/s total) pin the daemon at **96% of one core** and
inflate a legitimate client's `GET_SETTINGS` round-trip **60×**
(0.4 ms idle → 25 ms under attack).

*Client side — the shipped artifact* — a malicious/compromised door does the
same to `PowerPetDoorClient`:

```
client process CPU over 55 s of a dribbling door: 52.19 s (95% of one core)
buffered chars in client: 41513
co-resident 10 ms heartbeat lag: median=0.8 ms  p95=2.0 ms  max=11.8 ms
```

41.5 KB of traffic bought **52 CPU-seconds** — 1.26 ms of CPU per attacker
byte, i.e. a ~750 byte/s trickle pins a full core indefinitely. The victim
here is the host application's event loop: in the intended deployment that is
a Home Assistant instance, frequently on a Raspberry Pi, whose loop is shared
with every other integration. Cost scales linearly with buffer depth, so the
numbers above are *below* the worst case — at the 64 KiB cap a single
connection costs ≈60 CPU-seconds for ≈64 KB of traffic.

For calibration, the same in-process measurement across chunk sizes shows the
attacker's control of the exponent, and shows that an attacker who simply
blasts data pays nothing (which is why Round 3's flood test did not surface
this):

```
chunk=  1 -> 131.211s CPU to reach 65000 buffered chars
chunk= 16 ->   7.878s
chunk=256 ->   0.485s
```

Note the mitigation gap precisely: `MAX_BUFFER_SIZE` was added in Round 1 for
exactly this threat actor and it does its job for memory (RSS stayed flat in
all runs). It does not bound CPU, and on the client side the overflow
disconnect is followed by a reconnect whose backoff counter is reset by every
successful connection (`client.py:1173`), so the attacker gets a repeatable
duty cycle rather than a one-shot.

**Why Medium and not Low:** unauthenticated, remote, zero interaction, ~1000×
amplification, and it lands in the *shipped library* where the blast radius is
the consumer's whole event loop rather than a dev tool. **Why not High:** it
is availability-only — no confidentiality or integrity impact, no crash, and
measured co-resident task lag stayed in single-digit milliseconds; the process
degrades rather than stops. This matches the Round-1 calibration, which rated
the (memory) version of the same attack Medium.

**Recommendation:** make framing incremental instead of restarting. Carry the
scanner's state across calls — `(offset, depth, in_string, escaped)` — so each
byte is examined exactly once and the total is O(N):

- give `extract_frames` an explicit scanner-state parameter (or return it
  alongside the remainder) and have both callers thread it through their
  retained buffer;
- resume the loop at the saved offset rather than 0.

The 64 KiB cap and the `overflow` disconnect stay exactly as they are; this
only removes the rescan. A cheaper partial mitigation (bail out early when the
retained buffer contains no `}` at all) helps this specific payload but not a
peer that dribbles nested braces, so the state-carrying fix is the right one.
Worth a fuzz/property test asserting that total scan work is linear in bytes
delivered regardless of chunking.

---

### 2. [Low] Untrusted wire values used as dict keys with no hashability guard — three sites, each turning one malformed frame into a full stack trace at ERROR

**Files:**
- `src/powerpetdoor/client.py:194` — `return cls._handlers.get(cmd)` in
  `ResponseHandlerRegistry.get`, reached from `client.py:1560`
  (`handler = ResponseHandlerRegistry.get(cmd)`, where
  `cmd = msg.get(FIELD_CMD)` at `client.py:1514`) — **the shipped library**
- `src/powerpetdoor/simulator/protocol.py:603-604` — `_handle_get_schedule`:
  `index = msg.get(FIELD_INDEX)` then `index in self.state.schedules`
- `src/powerpetdoor/simulator/protocol.py:632-633` — `_handle_delete_schedule`,
  same shape
- Contrast — the pattern already established for the sibling field:
  `client.py:1533` (`if isinstance(reply_msg_id, (int, str))`, with the
  comment "anything that is not a usable dict key (a list, a dict, ...) must
  not raise here") and `protocol.py:446` (`if not isinstance(cmd, str)`)

A JSON list or object is a perfectly legal value for these fields on the wire
and an unhashable key in Python. `dict.get`/`in` raise `TypeError`, which is
handled at neither site.

**Attack scenario A — simulator (verified over real TCP):** an unauthenticated
peer sends `{"config":"GET_SCHEDULE","index":[1]}`. It gets the generic
envelope, and the daemon writes a full traceback:

```
attacker <- {"CMD":"GET_SCHEDULE","success":"false","dir":"d2p","reason":"Command failed"}
daemon log: ERROR Simulator: Error handling command GET_SCHEDULE
            Traceback (most recent call last): ... TypeError: unhashable type: 'list'
```

The `reason` is wrong (the validator layer above it has specific reasons for
exactly this class of input), and the log cost is the real issue. Measured
against a real daemon with stderr to a file:

| flood payload | attacker bytes | log written | amplification |
|---|---|---|---|
| `{"config":"GET_SETTINGS"}` (valid) | 2.59 MB | 0.00 MB | — |
| `{"cmd":"NOPE"}` (unknown, one WARNING line) | 29.52 MB | 5.75 MB | ×0.2 |
| `{"config":"GET_SCHEDULE","index":[1]}` | 1.96 MB | 27.98 MB | **×14.3** |

That is the largest write-amplification factor in the codebase, driven from an
unauthenticated port, and in daemon mode every one of those tracebacks is
*also* fanned out to every connected control-channel client by
`_ControlLogHandler` (`cli.py:187-215`). It did not wedge anything — the
control channel answered in 1.8 s throughout and RSS stayed flat, including
with three deliberately stalled ctl clients — so the impact is log/disk
volume and operator-console noise, not denial.

**Attack scenario B — the shipped client (verified in-process):** a
malicious/compromised door answers `{"CMD":["x"],"success":"true"}`. The
`TypeError` escapes `process_message` entirely and surfaces as an unhandled
background-task exception:

```
receive path alive after 500 hostile frames? True
tracebacks logged: 501
log bytes produced: 224369   ~447 bytes per hostile frame  (~10x amplification)
```

A ~40-byte frame produces a ~447-byte ERROR-level traceback in the host
application's log, with no debug configuration needed. The receive path
survives (each message is its own task, `client.py:1491`) and no raw escape
bytes leak, but this directly contradicts the contract the module documents
for itself — `data_received`: "This callback never raises on arbitrary input"
(`client.py:1462-1463`); `process_message`: "handler dispatch is isolated so a
malformed payload cannot kill the receive path" (`client.py:1505-1506`). The
`finally` block still dequeues correctly, and an outstanding future for that
`msgID` is left to the existing `check_receipt`/disconnect machinery rather
than hanging forever — so there is no wedge.

**Why Low and not Medium:** no state corruption, no wedge, no confidentiality
or integrity impact; the receive path survives in both components and both
peers stay responsive. **Why not Informational:** it is unauthenticated,
remote, needs zero interaction, is trivially repeatable, and it breaks a
guarantee the code explicitly documents and has already implemented for the
neighbouring field.

**Recommendation:** apply the `msgID` guard to its two siblings.

- `ResponseHandlerRegistry.get` already special-cases `None` — widen it to
  `if not isinstance(cmd, str): return None`, which also removes the need for
  every caller to think about it.
- In `_handle_get_schedule` / `_handle_delete_schedule`, run `index` through
  the existing `_coerce_wire_int(..., 0, MAX_SCHEDULE_INDEX)` so a bad index
  gets the same specific `WireValueError` reason that `SET_SCHEDULE` already
  returns, instead of "Command failed" plus a stack trace. That also makes
  `GET_SCHEDULE`'s and `SET_SCHEDULE`'s treatment of the same field
  consistent.

---

### 3. [Low] The YAML script channel is the one untrusted input not held to the standard the wire and CLI paths now meet — raw ANSI to the operator's terminal, and `hold_time = inf` state poisoning

**Files:**
- `src/powerpetdoor/simulator/scripting.py:539-542` — `_set_value`:
  `state.hold_time = float(value)` (unbounded), `:538`
  `state.battery_percent = int(value)` (unbounded, and it bypasses
  `DoorSimulator.set_battery`'s own `max(0, min(100, ...))` clamp at
  `server.py:663`)
- Damage surface: `src/powerpetdoor/simulator/state.py:491`
  (`FIELD_HOLD_OPEN_TIME: int(self.hold_time * 100)` in `get_settings`),
  `protocol.py:583`, `protocol.py:811`, `server.py:393`
- ANSI egress: `src/powerpetdoor/simulator/cli.py:448,452,454`
  (`status_print(f"\n>>> Running script: {script.name}")` etc. — raw to
  stdout), `scripting.py:290,292,301,306,391` (script name/description/step
  params/`log` message → `logger.info`, unsanitized at source)
- Root cause of the log half: `_SanitizingFormatter` (`cli.py:48-57`) is
  installed only on the two **interactive** paths (`cli.py:88`, `cli.py:796`).
  `--script` (headless/CI) and `--daemon` keep the plain
  `logging.basicConfig` formatter from `cli.py:969-972`.
- Contrast — the two paths that *are* bounded: `protocol.py:798-811`
  (wire `SET_HOLD_TIME`, now 0–90000 cs and finite) and
  `commands/settings.py:124-147` (CLI `holdtime`, `ArgSpec` min 0.1 max 900)

Round 3's Medium was about a wire packet poisoning `hold_time`. The fix bounds
the wire path, and the CLI path was already bounded. The script path — the
third writer of the same field — was not touched, and it is an explicitly
in-scope untrusted input.

**Attack scenario (verified end-to-end with `ppd-simulator --script <file>
--oneshot`):** the operator is handed a YAML repro script. PyYAML's
double-quoted `\e` escape produces a real ESC without any raw control byte
appearing in the file:

```yaml
name: "\e[2J\e[1;1H*** PWNED-NAME ***\a"
description: "\e[31mPWNED-DESC\e[0m"
steps:
  - action: log
    message: "\e[2JPWNED-LOG"
  - action: set
    name: hold_time
    value: "inf"
```

Captured output (`rc=0`, script reports PASSED):

```
stdout raw ESC: 4  BEL: 2
stderr raw ESC: 8  BEL: 2
  >>> Running script: \x1b[2J\x1b[1;1H*** PWNED-NAME ***\x07
  [INFO] Running script: \x1b[2J\x1b[1;1H*** PWNED-NAME ***\x07
  [INFO]   Step 1: log(message=\x1b[2JPWNED-LOG)
  [INFO]   [SCRIPT] \x1b[2JPWNED-LOG
```

Those are real ESC `[2J` (clear screen), cursor home, forged text and BEL
delivered to the operator's terminal on both streams — the same class Round 1
closed for log records and Round 3 closed for CLI command results, through the
one channel neither covered. (Raw control bytes in the YAML are rejected by
PyYAML's scanner; the `\e` escape is what makes it work, so "the file looks
clean" is not a defence.)

The same script's second step poisons state exactly as Round 3's Medium
described:

```
YAML set hold_time='inf'   -> state.hold_time=inf  GET_SETTINGS BROKEN: OverflowError
YAML set hold_time='nan'   -> state.hold_time=nan  GET_SETTINGS BROKEN: ValueError
YAML set hold_time='1e400' -> state.hold_time=inf  GET_SETTINGS BROKEN: OverflowError
YAML set battery=99999999999 -> state.battery_percent=99999999999
```

`GET_SETTINGS` — the command the shipped client issues on connect and on every
refresh — then fails for **every** client for the life of the process, and the
door parks in `DOOR_HOLDING` forever, exactly as measured in Round 3. This is
not reachable from the control channel (`_load_script_restricted` +
`_load_script_by_name` confine `run` to bare names under `--scripts-dir` and
the built-ins, re-verified below), so it needs the operator to run an
attacker-supplied file.

**Why Low and not Medium:** it requires the operator to execute an untrusted
script, at which point they have already accepted arbitrary simulated-door
behavior; damage is confined to the simulator's own state and clears on
restart. **Why not Informational:** an untrusted YAML script is a named actor
in this threat model and "here is a repro script for the bug I filed" is a
plausible workflow; terminal-escape injection is a real, already-recognized
impact class in this codebase; and this is the exact defect Round 3 rated
Medium, still reachable by a second route.

**Recommendation:**

1. Route the script writer through the same bounds as the wire writer.
   `_set_value`'s `hold_time`/`battery` cases should reuse the existing
   validators rather than raw `float()`/`int()` — reject non-finite and
   out-of-range with a `ScriptError` (which `_run_steps` already turns into a
   clean step failure), and use `simulator.set_battery()` so the 0–100 clamp
   applies. Consider auditing the rest of `_set_value`/`_toggle_value` the
   same way while there.
2. Sanitize the script channel at its source the way the protocol channel is:
   wrap `script.name`, `script.description`, `str(step)` and the `log`
   message in `sanitize_text` before they reach `logger`/`status_print`.
3. Defence in depth for anything missed: install `_SanitizingFormatter` on the
   default handler too (i.e. in `main()`'s `logging.basicConfig`, or by
   setting it on the root handler right after), so headless and daemon modes
   get the same terminal safety the interactive modes already have. That
   single change also covers any future unsanitized-at-source log site.

---

### 4. [Informational] Operator `schedule add` can allocate an index outside the range the simulator enforces on the wire

**Files:** `src/powerpetdoor/simulator/commands/schedules.py:121-125`
(`idx = 0; while idx in existing: idx += 1` — no ceiling) versus
`src/powerpetdoor/simulator/state.py:55` (`MAX_SCHEDULE_INDEX = 255`, enforced
on every wire-supplied index)

A wire peer can legitimately fill every legal slot (0–255 — that bound is what
caps schedule memory, and it works). The operator's next `schedule add` then
silently creates index **256**, which `to_dict()` puts on the wire and
`GET_SCHEDULE_LIST` returns — a value the simulator would itself reject if a
client sent it, and that real hardware would not accept. Verified:

```
operator `schedule add` with 0-255 taken -> True  Added schedule #256: inside sensor, all days, 06:00-22:00
new index: 256   (MAX_SCHEDULE_INDEX is 255)
```

**Not a vulnerability** — no memory growth (the wire bound still holds), no
privilege crossing, and the operator is trusted. It is a fidelity/consistency
gap in an availability-adjacent place. One-line fix: cap the search at
`MAX_SCHEDULE_INDEX` and return `CommandResult(False, "No free schedule
slots")` when full.

---

### 5. [Informational] `.gitea` reusable workflow is the only mutable-tag `uses:` and the only one receiving a secret

**File:** `.gitea/workflows/sync-wiki.yml:8`
(`uses: neuromancy/workflows/.gitea/workflows/sync-github-wiki.yml@v1.1.0`),
`:10` (`github_token: ${{ secrets.GH_PAT }}`)

Every other `uses:` in the repo (14 of 15) is SHA-pinned with a version
comment, and Round 3's Finding 4 is fully fixed (see verification below). This
one first-party org reusable workflow remains on a mutable git tag — the
exception Round 1 explicitly permitted — but it is worth restating with the
fact that makes it the sharpest of the remaining references: it is the *only*
`uses:` in the repo that is handed a secret. Anyone able to move the `v1.1.0`
tag in `neuromancy/workflows` gets `GH_PAT`. Pinning it to a commit SHA like
everything else costs nothing and closes it.

---

### 6. [Informational] No automated dependency-update mechanism

**Files:** absent — no `.github/dependabot.yml`, no `renovate.json`/
`.renovaterc*`, no `.pre-commit-config.yaml`

Round 3 recommended Dependabot/Renovate on `uv.lock` so that pinning does not
become staleness. Pinning happened (and is thorough); the automation did not.
The action SHAs, the `[build-system]` pins and `uv.lock` are all hand-updated.
Today that is fine — the lock was resolved essentially at head-of-index and
nothing in it is EOL or CVE-bearing — but it is now the only thing standing
between the repo and silently shipping a stale, hashed-and-pinned dependency
set. Also noted, and effectively closed rather than open: `setuptools==84.0.0`
/ `wheel==0.48.0` (`pyproject.toml:6`) are exact-pinned but do not appear in
`uv.lock` at all (uv does not lock build-backend deps), so they are the one
input to the `id-token: write` publish job that is version-pinned but not
hash-verified.

---

## Round 3 Fix Verification

**Round-3 Finding 1 (Medium) — unvalidated `SET_*` wire values: FIXED, and
the fix is thorough.** Every Round-3 reproduction now fails to reproduce, and
I could not find a bypass. Verified by re-running the exact payloads through
`DoorSimulatorProtocol` and by reading `protocol.py:152-219`:

| payload | Round-3 result | now |
|---|---|---|
| `{"cmd":"SET_HOLD_TIME","holdTime":1e400}` | stored `inf`, `GET_SETTINGS` broken forever | rejected — "holdTime must be a finite number, got inf" |
| `holdTime: NaN` | stored `nan`, door wedged in `DOOR_HOLDING` | rejected — "must be a finite number" |
| `holdTime: -1` / `90001` | stored | rejected — "must be between 0 and 90000" |
| `holdTime: "5000"` / `true` | stored | rejected — "must be a number" (bool correctly excluded despite being an `int` subclass) |
| `{"tz":["x"]}` / `{"a":1}` / `5` | `success:true`, broke every later `GET_SETTINGS` | rejected — "tz must be a string" |
| `tz` 200 chars | stored | rejected — "must be at most 128 characters" |
| `{"sensorTriggerVoltage":{"a":1}}` / `1e400` / `99999` | stored and echoed | rejected |
| `{"sleepSensorTriggerVoltage":-5}` | stored | rejected — "must be between 0 and 65535" |

State after all sixteen hostile packets: `hold_time=2.0`,
`tz='America/New_York'`, `volt=100`, `sleep=50`, notifications untouched;
`GET_SETTINGS` and `GET_HOLD_TIME` both still answer `success:true`. Nothing
was assigned before validation on any path.

`SET_NOTIFICATIONS` is genuinely atomic — verified for both the top-level and
the nested-dict forms: a batch whose last flag is malformed applies **none**
of the earlier ones and returns the specific reason. Flags are read via
`make_bool`, so `"0"` correctly means false.

`get_tzinfo` now catches `TypeError` (`state.py:527`), and
`_coerce_schedule_int` now catches `OverflowError` (`state.py:66`) — Round-3
Informational #5 is fixed too: `{"index":1e400}` now returns "Schedule index
must be a number, got inf" instead of a traceback and "Command failed".

**Wire validation could not be bypassed on any path I tried.** `SET_SCHEDULE`
rejects non-mapping payloads, out-of-range indices, malformed `daysOfWeek`
elements and non-finite hours. `SET_SCHEDULE_LIST` is atomic: a 300-entry
batch is rejected whole (on index 256) and leaves the previously-stored
schedule untouched; a valid-then-malformed pair leaves `schedules` unchanged.
The operator/CLI writers of the same state are bounded by their `ArgSpec`s
(`holdtime` 0.1–900, `battery` 0–100, `schedule *` index ≥ 0, `timezone`
IANA-membership-or-POSIX-parse). The **only** unbounded writer of `hold_time`
left is the YAML script path — Finding 3.

**Round-3 Findings 2 and 3 (Low) — terminal-escape injection: FIXED on every
egress I could find, verified end-to-end against real processes.** Driving a
real `ppd-simulator --daemon --debug` with eleven hostile payloads (JSON `\u`
escapes, raw ASCII ESC in a frame, non-ASCII bytes, ESC in `cmd`/`config`/
`msgID`/object keys/`daysOfWeek`), then asking a real control client for
`timezone` and `status`:

```
--- daemon stdout: 71 bytes,   RAW ESC=0, BEL=0
--- daemon stderr: 4870 bytes, RAW ESC=0, BEL=0
--- ctl stream:    4948 bytes, RAW ESC=0, BEL=0
OK: Timezone: \\x1b[2Jsanitycheck
LOG: ... [WARNING] Simulator: Unknown command: \\x1b[31mEVILCMD\\x1b[0m
```

Round-3 Finding 2's exact PoC against the interactive CLI (piped stdin →
`_BasicStdinInput`) is likewise dead — `render_result` catches it:

```
127.0.0.1:39521> >>> Timezone: \x1b[2J\x1b[1;1H*** PWNED ***\x07
stdout RAW ESC: 0  BEL: 0     stderr RAW ESC: 0  BEL: 0
```

Round-3 Finding 3's PoC against the client library is dead too — its
brace-balanced ESC frame now logs escaped, and 500 hostile frames of assorted
shapes produced **0** raw ESC bytes in the client's log.

**The sanitizer covers every network-derived egress.** I audited all 100+
`logger.*` call sites across the library and simulator plus every
`print`/`status_print`/`writer.write` and traced each argument to its source.
Network-derived values are sanitized *at the source* — `protocol.py` (RX/TX
debug, unknown command, rejection reasons, schedule index), `client.py:1482`
(RX debug), `client.py:1489` (JSON-decode ERROR), `client.py:990`
(notification state), `tz_utils.py:215`, `schedule.py:74-126` — and the
remaining `%s` sites carry only locally-derived data. Neighbouring sites use
`%r` or `json.dumps`, both of which escape control characters
(`client.py:1511,1523,1538,1579`, `state.py:541`, `door.py:144`).
`client.py:1565` interpolates a device-supplied `cmd` with `%s`, but that line
is only reachable when `cmd` matched a registered handler name, so it is
provably a known constant. `protocol.py:379` logs a `JSONDecodeError`, whose
`str()` never embeds input text. Three egresses are covered: control channel
(`escape_message(sanitize_text(...))`, and the sanitize-then-escape order is
correct — `\r` is escaped by the sanitizer, `\n` survives to be escaped by
`escape_message`, so no `LOG:`/`OK:`/`ERROR:` line can be forged), ctl
(`ctl.py:284,287,291,298,445,447,451`), and both CLI prompt paths. The one
input channel whose values are *not* sanitized at source is the YAML script —
Finding 3.

**Round-3 Finding 4 (Low) — unpinned CI dependencies: FIXED.** All five
install steps now use the locked form — `uv sync --locked --all-extras`
(`test.yml:54,81,109,136`, `release.yml:27`) — and there is no
`pip install`/`uv pip install` anywhere in `.github/` or `.gitea/`.
`[build-system] requires = ["setuptools==84.0.0", "wheel==0.48.0"]`
(`pyproject.toml:6`) is exactly pinned, closing the build-isolation hole in
the `id-token: write` job. `uv.lock` carries 544 `sha256` entries for 544
artifacts (no unhashed entry), all from the PyPI registry with no git/URL/path
sources. Round-3's cosmetic nit is gone too: no workflow hardcodes a tool
version any more; `ruff`/`mypy`/`pytest`/`coverage` are invoked bare from the
locked venv.

**Round-3 Informational #6 (no read backpressure) — re-examined, and I agree
it should stay unactioned.** My own measurements support the original
reasoning rather than undercutting it. A valid-command flood is self-limiting
exactly as Round 3 found (2.59 MB in → RSS +29 MB, plateauing, control channel
still answering in 2.75 s). Deliberately stalling three control clients while
flooding from the door port did *not* produce unbounded write-buffer growth
(RSS +8 MB, flat). Adding `pause_reading()`/`set_write_buffer_limits()` would
change simulator semantics for no measured benefit. Finding 1 is a different
mechanism entirely — CPU, not memory, and driven by *small* packets that
backpressure would not throttle — so it does not change this conclusion.

**Round-1/2 fixes re-verified (all still hold):** 64 KiB framing cap with
`overflow` acted on by both consumers; `find_frame_end` never raising on
non-`{` input; control channel loopback-by-default with `--control-host`
rejected without `--daemon` (`cli.py:1017-1018`); door bind `0.0.0.0`
documented with the UNAUTHENTICATED warning in the help text; script-path
restriction on the control channel; `Schedule.from_dict` validation;
`process_message` always completing paired futures; SHA-pinned actions;
history files created `0600` via `os.open(..., 0o600)` + `os.chmod`
(`commands/history.py:20-28`).

---

## Areas Reviewed With No Findings

- **The per-attempt connection shim creates no resource leak, and repeated
  declined transports cannot be used against it.** Tested three ways against a
  real listener. 300 shutdown-races-mid-connect left **0** live
  `_ConnectionAttempt` objects (`gc` scan), `_declined == 0`, and empty
  `_tasks`/`_handler_tasks`/`_outstanding`. 300 transports declined for
  shutdown, and 300 declined as second-connections, both left `_declined == 0`
  — the counter is incremented in `connection_made` and decremented in
  `connection_lost`, and asyncio guarantees the pairing, so it cannot drift.
  Through all 300 second-connection rejections the live transport stayed
  adopted and `available` stayed True: a hostile peer cannot use a rejected
  connection to tear down the real one. `_ConnectionAttempt` is
  `__slots__`-based, holds only a client reference and a transport, and is
  dropped when the transport is. Reading confirms the design: `_adopt_transport`
  aborts what it declines, `connection_lost` returns early when
  `not self._adopted`, and the superseded-transport check (`ctl.py`-style
  identity comparison at `client.py:1697-1702`) prevents a stale socket from
  closing a newer one.
- **The `stop` command and script-status reporting expose nothing and behave
  correctly.** `stop` is confined to the script (`shutdown` remains the only
  way to stop the daemon) and reports only the script's own name, which comes
  from an operator-controlled YAML file and is sanitized on the control-channel
  egress. Verified end-to-end with the real `ppd-simulator-ctl` binary against
  a real daemon: `run full_test_suite` → `status`/`list` correctly report
  `Script: running "Full Test Suite"` → `stop` returns `OK: Stopping script:
  Full Test Suite` → the daemon logs `Script error at step 4: Script stopped
  while waiting` / `Script FAILED`. With no script running, `stop` returns
  `ERROR: No script is running` **without** calling `ScriptRunner.stop()`, so
  it cannot poison a later run; `_run_steps` clears `_stop_requested` and
  `_stop_event` on entry anyway, so no stop request can leak across runs even
  under a race. `script_status()` is the single source shared by `status`,
  `list` and `stop` (M5), and its `busy` is derived from the run lock, so the
  three can never disagree. The only state it adds to the channel — a script
  name and a queue depth — is available to any caller who could already `run`
  and `shutdown`.
- **The control channel's dead-writer reaping and log re-entrancy guard work.**
  The `_broadcasting` flag in `_ControlLogHandler.emit` (`cli.py:187-215`)
  breaks the feedback loop the docstring describes; writers are discarded on
  `is_closing()` and on write failure, in the log handler and in
  `broadcast_status` alike. Repeatedly opening and abandoning control
  connections while flooding the door port produced no runaway and no
  unbounded client set. `_handle_client` bounds each line by asyncio's 64 KiB
  stream limit and closes the connection on the resulting `ValueError`.
- **ctl's `LOG:` streaming to stderr introduces no new egress risk.** Every
  line it prints goes through `sanitize_text(unescape_message(...))`
  (`ctl.py:291`), the same as the interactive path (`ctl.py:445`), and it
  writes to stderr so stdout stays clean for the single result line. The
  unbounded `sock.settimeout(None)` for a wait-run is the documented
  deliberate trade-off and lives in the operator's own process.
- **YAML loading remains safe.** `scripting.py:134` is the only YAML entry
  point and uses `yaml.safe_load`; repo-wide there is no
  `yaml.load`/`full_load`/`unsafe_load`. PyYAML's scanner additionally rejects
  raw C0 control bytes in scalars (only the `\e` *escape* gets through — see
  Finding 3), and a hostile script yields at most a `ScriptError`.
- **No dangerous execution sinks.** Repo-wide grep over `src/` for `eval(`,
  `exec(`, `pickle`, `subprocess`, `os.system`, `shell=True`, `__import__`
  returns nothing. `scripts/generate_gaps_report.py`, the only other executed
  code in CI, is likewise clean of network and process calls.
- **Script-path restriction on the control channel still holds.**
  `_load_script_restricted` rejects `/`, `\` and a leading `.`;
  `_load_script_by_name` resolves against `--scripts-dir` and requires
  `candidate.parent == base` after `.resolve()`, defeating a planted symlink.
  `script_completer` walks the CWD but executes only in the operator's ctl
  process and is unreachable over the wire.
- **Bind addresses re-checked.** The only `0.0.0.0` occurrences are the door
  server default (`server.py:116`, `cli.py:548,859`) and the `--control-host`
  help text. The control channel defaults to `127.0.0.1` and `--control-host`
  is rejected outright without `--daemon`.
- **The command/argument parser remains bounded.** `parse_args` rejects extra
  arguments, `parse_arg` enforces per-type min/max/choices before any value
  reaches state, handler lookup is `getattr` on a decorator-registered
  function name (no injection), and `escape_message`/`unescape_message` round-
  trip correctly (backslashes doubled first, split on `\\\\` first).
- **The POSIX TZ regex is not a ReDoS.** `_POSIX_TZ_RE` (`tz_utils.py:39-44`)
  is anchored, `.match`-only, and has no ambiguous nested quantifier: the
  alternation branches are disjoint (`[A-Za-z]+` vs a bracketed form requiring
  a literal `>`) and are followed by a digit class that cannot overlap them.
  The wire input feeding it is now capped at 128 characters anyway.
- **CI supply chain.** No `pull_request_target` and no `workflow_run`
  anywhere; no `ref:`/`repository:` override on any checkout, so PR runs use
  the read-only merge-commit context. No secret is interpolated into any `run:`
  shell string — `CODECOV_TOKEN` is passed as an action input and guarded by a
  non-empty check so fork PRs skip rather than fail. `permissions:` appears
  exactly once, `id-token: write` on the `publish` job alone, with
  `environment: pypi`, `needs: test`, and OIDC trusted publishing (no
  long-lived token). Every job in both workflows sets `timeout-minutes: 30`.
  The fuzz job runs on `pull_request` with `--hypothesis-seed=0` for
  determinism and unseeded on the weekly cron — the same trigger and privilege
  as the pre-existing unit matrix, so it adds no exposure.
- **Dependency currency is good.** The only runtime dependency is `tzdata`
  (pure data, 2025.3); `pyyaml` 6.0.3 and `prompt_toolkit` 3.0.52 are optional
  extras. The dev set (pytest 9.0.2, hypothesis 6.165.10, mypy 2.3.1, ruff
  0.16.4, coverage 7.15.4, pytest-xdist 3.8.0) is current — the lock's newest
  `upload-time` matches its own mtime, so it was resolved at head-of-index.
  Nothing EOL, nothing with a known CVE, 28 packages total. All declared
  specifiers are `>=` floors, but `uv.lock` + `--locked` is what CI actually
  resolves against, so the floors are not the operative control.
- **No secrets at rest or in logs.** The device protocol carries no
  credentials, keys or PII; neither the library nor the simulator stores or
  logs anything sensitive. History files — the only persisted operator data —
  are created `0600` and re-`chmod`ed regardless of umask.
