# Security Analyst Analysis — Round 10

Commit `65ad86d`. Read-only sweep. Every number below was produced by executing
the shipped code and was **measured at least twice** before being quoted;
where two trials disagreed the range is given rather than a single figure.

Emphasis this round was breadth into surfaces never previously attacked: the
width-aware `_escape`, the five new throttles' state lifetime, the `frame_size`
plumbing, the shared sensor predicate, the packaging surface `MANIFEST.in` now
publishes, and the hardened changelog workflow.

## Summary

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 1 |
| Informational | 3 |

The single Low is a log-record **forgery** primitive that is the exact
counterpart of round-9 M2 (log-record *destruction*): `sanitize_text` escapes
C0, C1, DEL and surrogates, but by design preserves LF — and LF in a
peer-chosen field is a whole fabricated log record in the host's logfile.

Four of the six surfaces I was asked to attack produced **nothing**, and the
measurements that establish that are recorded below rather than omitted.

---

## Findings

### L1 — Low — A peer-chosen field value forges whole log records (LF is not escaped)

**File:** `src/powerpetdoor/sanitize.py:35` (`_CONTROL_CHAR_RE`) and
`src/powerpetdoor/sanitize.py:61` (`sanitize_text`), as consumed by the
field-level sinks — measured at `src/powerpetdoor/client.py:1008` and
`src/powerpetdoor/door.py:278`.

`_CONTROL_CHAR_RE` is `[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]`. The gap at
`\x09`–`\x0a` is deliberate and documented ("except tab and newline"), and it is
*required* by one of the two sink classes — but not by the other. The
consequence is that a network peer choosing the value of `fwInfo`, `door_status`
or any similar field controls the *record structure* of the operator's log, not
just the text inside one record.

**Reproduction**

Which peer-chosen characters survive `sanitize_text` intact
(`/tmp/ppd_attack/sanitize_attacks.py`):

```
    ESC (C0)             in='\x1b[31m'       out='\\x1b[31m'          changed=True
    DEL                  in='\x7f'           out='\\x7f'              changed=True
    C1 CSI U+009B        in='\x9b'           out='\\x9b'              changed=True
    lone surrogate       in='\ud800'         out='\\ud800'            changed=True
    LF newline           in='a\nb'           out='a\nb'               changed=False
    CR                   in='a\rb'           out='a\\x0db'            changed=True
    tab                  in='a\tb'           out='a\tb'               changed=False
    U+202E RLO           in='‮'         out='‮'             changed=False
    U+2066 LRI           in='⁦'         out='⁦'             changed=False
    U+2028 LINE SEP      in=' '         out=' '             changed=False
    U+200B ZWSP          in='​'         out='​'             changed=False
    backslash literal    in='\\x1b'          out='\\x1b'              changed=False
```

CR *is* escaped (it is inside `\x0b-\x1f`); LF is not. That asymmetry is the
whole finding.

End-to-end over **real TCP**, real `PowerPetDoor.connect()` so the facade's
listeners are actually wired, records written by a real
`logging.FileHandler(encoding="utf-8")`, **3 runs per site**
(`/tmp/ppd_attack/forge_tcp.py`). A "forged" record is counted only when a
*physical line* **begins** with the attacker's own timestamp/level/logger — a
genuine line that merely contains the attacker's body text further along does
not count (my first metric made that mistake and the ESC controls caught it):

```
site                                                   run1   run2   run3   verdict
client.py:1008 fwInfo (non-mapping hardware info)         1      1      1   FORGED
door.py:279 door_status (unknown door status)             1      1      1   FORGED
door.py:1696 schedule (malformed schedule update)         0      0      0   none
client.py:1862 msgID (unusable msgID)                     0      0      0   none
CONTROL client.py:1008 fwInfo, ESC instead of LF          0      0      0   none
CONTROL door.py:279 door_status, ESC instead of LF        0      0      0   none
```

The **control rows are what make this specific**: the same frame, same site,
same handler, with `ESC` (a character the regex covers) substituted for `LF`
forges nothing. One character is the entire difference.

The resulting logfile, verbatim — the third line is entirely attacker-authored,
including its timestamp, its severity and its logger name:

```
  [GENUINE-SHAPED] 2026-08-22 23:17:55,465 WARNING powerpetdoor.client: Device sent 1 non-mapping fwInfo payload(s) (87 bytes) on this connection
  [GENUINE-SHAPED] 2026-08-22 23:17:55,465 WARNING powerpetdoor.client: Device sent a non-mapping fwInfo payload; not notifying hw_info listeners: 1.0
  [GENUINE-SHAPED] 2026-08-22 23:30:00,000 WARNING powerpetdoor.door: Door closed and secured by owner
  [GENUINE-SHAPED] 2026-08-22 23:17:55,465 ERROR powerpetdoor.client: The server closed the connection. Reconnecting...
  [GENUINE-SHAPED] 2026-08-22 23:17:55,465 WARNING powerpetdoor.door: Refresh step GET_SETTINGS failed: ConnectionError('Connection closed before a respon
```

The wire frame that produced it is 143 bytes of ordinary JSON:

```
b'{"CMD": "GET_HW_INFO", "success": "true", "fwInfo": "1.0\\n2026-08-22 23:30:00,000 WARNING powerpetdoor.door: D'...
```

The 200-character `MAX_LOGGED_LENGTH` cap does not help: a complete forged
record fits in 63 of those characters.

**Why the two sites that resisted, resisted.** `door.py:1696` and
`client.py:1862` render the peer value through `repr()`/`{value!r}` *before*
`sanitize_text` sees it, and `repr()` escapes LF. That is accidental protection,
not a control — it is the same accident that protected the surrogate sites until
round 9 removed it.

**The other half of the same root cause** (measured, same harness): `U+202E`
RIGHT-TO-LEFT OVERRIDE, `U+2066` LEFT-TO-RIGHT ISOLATE, `U+2028` LINE SEPARATOR
and `U+200B` ZERO WIDTH SPACE all pass through unchanged. These do not split a
record but they do control how one renders (the Trojan-Source / CVE-2021-42574
class), and `U+2028` is a line terminator to several log consumers.

**What is *not* affected**, verified (`/tmp/ppd_attack/final_checks.py`): the
simulator's `ctl` control channel is safe, because `escape_message` runs after
`sanitize_text` and doubles backslashes then escapes LF:

```
  escape_message(sanitize_text(v)) -> 0 raw LF(s), 1 protocol line(s)
  protocol line: 'X\\n2026-08-22 23:30:00,000 WARNING powerpetdoor.simulator: door unlocked'
```

A peer therefore cannot forge `OK:`/`ERROR:`/`LOG:` protocol lines to a parked
`ctl -i` session. It can still forge a *display* line there, and in the
simulator's own log (`sanitize_log_text(cmd, MAX_LOGGED_LENGTH)` → 1 surviving
LF, 2 physical lines).

**Attack scenario.** The device protocol is unauthenticated by device design, so
anything on the LAN can answer as the door. Answering one `GET_HW_INFO` with a
crafted `fwInfo` writes an arbitrary, correctly-formatted, correctly-attributed
record into the host application's logfile — chosen timestamp, chosen severity,
chosen logger name, chosen text. An operator investigating a door that opened at
03:00 reads a `powerpetdoor.door: Door closed and secured by owner` record that
the library never emitted; a log shipper (journald → Loki, or Home Assistant's
own log view) indexes it as a distinct event. The peer already controls the
*content* of what the library says about the door; what it gains here is the
ability to author records attributed to components and severities the protocol
does not otherwise reach.

**Why Low and not Medium.** I considered Medium for parity with round-9 M2,
which was Medium for the mirror-image defect (records *destroyed*). I did not
take it: nothing here affects availability, memory, CPU, cached state or
confidentiality; the library's behaviour is byte-for-byte unchanged; and the
damage requires a human or a parser to read the log and be misled. It is a
genuine integrity defeat of the paper trail, but a bounded one.

**Recommendation.** This narrows nothing, refuses nothing, and changes no byte
on the wire or any value the library accepts, caches or hands to listeners — it
changes only how an already-accepted value is *rendered* into a log record. That
is the same shape as the accepted round-9 surrogate fix.

The fix must **not** be applied to `sanitize_text` itself. Measured
(`/tmp/ppd_attack/notify_cost.py`), `_SanitizingFormatter.format()` at
`simulator/cli.py:58` runs `sanitize_text` over the *whole formatted record*,
and a record carrying an exception is legitimately multi-line:

```
  formatted record: 162 chars, 4 LFs, 5 physical lines
  first line : ERROR t: handler failed
  last line  : ValueError: boom
```

Escaping LF there would collapse every traceback in the simulator to one line.
So:

1. Add a sibling — e.g. `sanitize_field(text, limit=None)` — that escapes
   everything `sanitize_text` does **plus** `\n`, `\t`, and the
   bidi/format class: U+200B-U+200F, U+202A-U+202E, U+2028-U+2029,
   U+2060-U+2064, U+2066-U+2069 and U+FEFF. Reuse `_escape`, which already
   renders every one of those correctly at
   four-digit width (verified exhaustively — see "Areas Reviewed").
2. Point the **field-level** sinks at it: the ones that interpolate a single
   peer-chosen value. `client.py` 1008/1163/1793/1843/1862/1901/1929;
   `door.py` 234/278/1261/1283/1285/1446/1606/1695; `schedule.py`
   158/232/236/243/252/257/263/272/278/284/754; `tz_utils.py:215`;
   `simulator/engine.py:361`; `simulator/protocol.py` 478/497/633/634/799/845.
3. Leave `sanitize_text` in place, unchanged, for the two **record-level**
   sinks that need LF: `simulator/cli.py:58` and the `ctl.py` display path,
   where `unescape_message` deliberately restores newlines.

---

## Round 9 Fix Verification

All four round-9 items re-attacked with their original reproductions.

### S-M2 — surrogates in `_CONTROL_CHAR_RE`, width-aware `_escape` — **FIXED**

```
  _CONTROL_CHAR_RE = '[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\ud800-\\udfff]'
    U+001B: matched=True  escaped_as='\\x1b'
    U+007F: matched=True  escaped_as='\\x7f'
    U+009F: matched=True  escaped_as='\\x9f'
    U+D800: matched=True  escaped_as='\\ud800'
    U+DBFF: matched=True  escaped_as='\\udbff'
    U+DC00: matched=True  escaped_as='\\udc00'
    U+DFFF: matched=True  escaped_as='\\udfff'
```

200 hostile frames (`fwInfo` = lone surrogate), shipped client, real
`FileHandler(encoding="utf-8")`, **4 trials**:

| | logfile | records | stderr |
|---|---|---|---|
| round-9, pre-fix | **0 B** | **0** | **145 KB** |
| round 10 | 2,196 / 3,675 / 4,047 / 4,047 B | 16 / 28 / 31 / 16 | **0 / 0 / 0 / 0** |

The record count varies between trials because the throttle's 60 s quiet period
interacts with wall-clock run length; `stderr = 0` and `logfile > 0` held in
every trial. The ESC control row (a character the regex always covered) now
produces the same shape as the surrogate row — which was the point of the fix.

I could not break `_escape`. Exhaustive sweep of **all 1,114,112 code points**
through `sanitize_text` (`/tmp/ppd_attack/a1_escape.py`, 0.65 s):

```
A1.1 swept 0x110000 codepoints in 0.65s; matched=2111; failures=0
A1.2 distinct escapes=2111 collisions=0 width-errors=0
A1.5 64KiB of U+D800: in=65536 out=393216 factor=6.0x time=28.8ms
A1.5 64KiB of ESC   : in=65536 out=262144 factor=4.0x time=25.5ms
```

Zero exceptions, zero outputs that fail `.encode("utf-8")`, all 2,111 matched
code points produce `\xNN`/`\uNNNN` escapes matching
`\\x[0-9a-f]{2}|\\u[0-9a-f]{4}`, **2,111 distinct escapes with zero
collisions**, and zero width errors (the `U+D800` → `\xd800` ambiguity the fix
was written for is gone). Worst-case expansion is bounded at 6.0x.

### S-M1 — five per-frame log sites throttled and capped — **FIXED**

In-process, one frame just under the 64 KiB framing cap (`msgID` = 21,840-item
list), 2 trials:

```
  trial 1: wire=65541 records=4 longest_record=332
  trial 2: wire=65541 records=4 longest_record=332
```

**65,638 B → 332 B**, exactly as claimed.

In-process, 20,000 frames of `{"CMD":0,"msgID":[]}`, 2 trials:

```
  trial 1: wire=400000 records=60 logfile=7203 amp=x0.0180 longest=137
  trial 2: wire=400000 records=64 logfile=7692 amp=x0.0192 longest=137
```

**20,033 records → 60–64**, amplification **×5.27 → ×0.018–×0.019**.

Real TCP, hostile door flooding a real `PowerPetDoor.connect()` for 3 s, 2
trials per site:

```
  client.py:1863 msgID     trial 1: peer 10.08 MB -> log  41156 B (0.0137 MB/s) amp=x0.0041 longest=139
  client.py:1863 msgID     trial 2: peer  5.76 MB -> log  36230 B (0.0121 MB/s) amp=x0.0063 longest=139
  door.py:279  door status trial 1: peer 17.71 MB -> log  13247 B (116 records) amp=x0.00075 longest=137
  door.py:279  door status trial 2: peer 12.55 MB -> log  13750 B (120 records) amp=x0.00110 longest=159
  door.py:1696 schedule    trial 1: peer 18.98 MB -> log   2477 B ( 21 records) amp=x0.00013 longest=159
  door.py:1696 schedule    trial 2: peer 17.18 MB -> log    893 B (  7 records) amp=x0.00005 longest=159
```

Round 9 measured **4.23 MB/s** of log from this attack. It is now
**0.0121–0.0137 MB/s** — a ~330x reduction — and no record exceeds 159 bytes.

The ordering constraint the refuter insisted on (S-M2 before S-M1) is visible in
these numbers: the surrogate site is now both escaped *and* throttled, and the
two fixes compose without either regressing the other.

### B-F1 — the dispatcher wedge — **FIXED**

Both the re-arm and `_update_flow()` are in the `finally` at
`framing.py:639-642`. Verified by reading and by the 20,000-frame and
sustained-TCP floods above completing with the transport draining normally
rather than parking at `paused=True`.

### Round-9 informational 1 — the sdist ships a suite that runs — **FIXED**

Built with `uv build` and inspected. sdist = 121 entries; top level is exactly
`CHANGELOG.md docs LICENSE MANIFEST.in PKG-INFO pyproject.toml README.md
scripts setup.cfg src tests uv.lock` — no `.git`, `.github`, `.claude`,
`.hypothesis`, `.coverage`, `.idea`, `.venv`.

```
2869 tests collected in 5.95s     (from the unpacked sdist, PYTHONPATH-installed)
```

### The hardened changelog workflow (d12c693 + round 9) — **HOLDS**

`.github/workflows/test.yml:80-120`. `BASE_REF` and `PUSH_BEFORE` are passed
through `env:` and never interpolated into the script body, and every expansion
is quoted. Argument injection into `git` is blocked structurally as well as by
quoting: `base` is always either `origin/$BASE_REF` (prefixed, so it cannot
begin with `-`), a SHA already validated by `git cat-file -e`, or the literal
`HEAD^`. An unresolvable base now `exit 1`s (`:103-106`) instead of printing
"OK: 0 file(s)". The multi-commit push range uses `github.event.before` with
all-zeroes and missing-object fallbacks. No issue found.

---

## Areas Reviewed With No Findings

**The five new throttles' state lifetime — no finding.** `disconnect()`
(`client.py:1496-1505`) and `_on_disconnect()` (`door.py:1664-1666`) `flush()`
then `reset()` all nine throttles, so a peer that drops the connection gets a
fresh doubling schedule. I measured what that actually buys. Driving
`disconnect()` in a tight loop *looks* alarming (20,000 frames delivered one per
connection → 80,000 records, ×23.65) but that is not a reachable rate. Over real
TCP the reconnect delay bounds it, and `connection_made()` resets
`_reconnect_attempts` so a peer that accepts before dropping keeps the base
delay:

```
 reconnect    k  conns   wire B  records    log B   rec/s   log B/s      amp
       5.0    1      3       60       33     4440     2.7       370   74.00x
       1.0    1     11      220      125    16808    10.4      1400   76.40x
      0.25    1     43      860      481    64696    40.1      5390   75.23x
```

At the facade default (`reconnect=5.0`) that is **370 bytes/s** of log.
Decisively, I isolated the reset's own contribution by neutralising
`EventThrottle.reset` and re-running:

```
  reconnect=0.25s  reset ON : conns=29    records=479    bytes=58135
  reconnect=0.25s  reset OFF: conns=30    records=445    bytes=54570
  records per connection: 16.52 with reset vs 14.83 without
      -> reset contributes 1.68 records/connection
```

**1.68 of 16.5 records per connection.** The other ~14.8 are connection
lifecycle records ("Started to connect", "Connection Successful!", "Closing
connection with server...", six `Refresh step ... failed` records) that exist
whether or not the throttles reset. The reset is not the driver, and suppressing
the lifecycle records would mean not reporting a flapping door.

**`frame_size` plumbing — no finding; the round-9 defect is genuinely closed.**
`client.py:1796` passes `frame_size=len(frame)`. I attacked every way to
decouple the reported number from the wire (`/tmp/ppd_attack/framesize.py`):

```
=== the round-9 F2 defect itself: whitespace padding INSIDE the frame ===
  {} padded to 60,002 B with in-frame whitespace   wire=60002  reported_max=60002  wire/reported=    1.00x
  {} unpadded (control)                            wire=2      reported_max=2      wire/reported=    1.00x

=== non-ASCII: backslashreplace expands 1 wire byte to 4 frame chars ===
  {} + 2000 raw 0x80 bytes inside the frame        wire=2006   reported_max=8006   wire/reported=    0.25x

=== whitespace BETWEEN frames (consumed by the resync branch) ===
  {} then 60,000 B of inter-frame whitespace, x1   wire=60002  reported_max=2      wire/reported=30001.00x
  {} then 60,000 B of inter-frame whitespace, x20  wire=1200040 reported_max=32    wire/reported=37501.25x

=== non-JSON garbage between frames ===
  {} then 60,000 B of non-whitespace garbage       wire=60002  reported_max=60000  wire/reported=    1.00x
```

Row 1 is the round-9 defect: **30,001x under-report → 1.00x**. The inter-frame
whitespace row reproduces the same 30,001x *magnitude*, but it is not the same
defect and I am not filing it: the counter says "Ignored 1 malformed message(s)
(2 bytes)", the malformed message really is 2 bytes, and no counter in the
system claims to report total connection bytes. Whitespace between frames is a
legal separator, is consumed without retention, and is deliberately excluded
from `diag.discarded` (the round-6 T1 decision, so the total does not depend on
where the peer cuts the packet).

**Inter-frame whitespace CPU path — no finding.** `FrameScanner.feed` skips
whitespace one character at a time in a Python `while` loop, whereas
non-whitespace garbage is skipped with `str.find`/`str.split` at C speed. That
is a real ×129 per-byte difference — but it does not extend a peer's reach,
because the legal-frame path is already more expensive
(`/tmp/ppd_attack/ws_cpu.py`, 256 KiB = one asyncio read, 5 reps, min):

```
payload                                   chars    min ms   median ms      MB/s
spaces (0x20) between frames             262144     22.83       22.90      11.5
tabs (0x09) between frames               262144     23.50       23.90      11.2
newlines (0x0a) between frames           262144     22.82       23.34      11.5
U+3000 IDEOGRAPHIC SPACE                3145706      1.95        2.00    1615.8
non-whitespace garbage 'Z'               262144      0.18        0.18    1482.4
legal {} frames (control)                262144     63.55       65.63       4.1

whitespace / C-speed garbage  = x129.1 slower per byte
whitespace / legal {} frames  = x0.36
```

A peer already buys 63.55 ms per read with plain legal `{}` frames; whitespace
buys 22.83 ms. Filing the ×129 as a vulnerability would be quoting the
flattering denominator.

**The shared sensor predicate — no finding; it is not a network-facing gate.**
`sensor_open_block_reason` (`engine.py:365-421`) is called by both
`trigger_sensor` and `activate_sensor`, so the round-9 divergence is closed. I
then checked whether it gates anything an unauthenticated peer can reach
(`/tmp/ppd_attack/final_checks.py`):

```
  registered wire commands: 46
  wire commands whose handler body calls a sensor entry point: NONE
    DoorSimulatorProtocol.trigger_sensor             exists=True  registered_as_wire_command=False
    DoorSimulatorProtocol.simulate_obstruction       exists=True  registered_as_wire_command=False
```

None of the 46 `@CommandRegistry.handler` commands reaches a sensor entry point;
`trigger_sensor`/`simulate_obstruction` on the protocol object are plain
delegation methods for the local CLI and script runner. The predicate is a
fidelity control, not an authorization boundary, so its remaining documented
asymmetry (`activate_sensor` still records the sensor-active flag under command
lockout, by design — "a pet standing in the doorway is a physical fact") has no
security consequence. The `cmd_lockout` question flagged for the real-device
test is a behaviour question, not a security one.

**The packaging surface — no finding; nothing sensitive is published.** Built
sdist and wheel and swept the extracted tree (2.7 MB, 121 entries):

- Credentials/PII: `0` hits for password/secret/api-key/token/private-key/
  authorization/credential outside test fixture names (`SECRETPROBE` is a
  round-9 sanitizer test string) — `0` `/home/...` developer paths, `0` e-mail
  addresses beyond the declared author, `0` routable IPs (the only literal is
  `300.1.1.1`, an intentionally invalid address used as a CLI test fixture).
- File modes: `108` files at `-rw-r--r--`, `13` directories at `drwxr-xr-x`. No
  executable bits, nothing group- or world-writable.
- No `.git`, `.github`, `.gitea`, `.claude`, `.idea`, `.venv`, `.hypothesis`,
  `.coverage`, `.mypy_cache`, `.ruff_cache`, `.remember`.
- `graft docs` publishes `docs/protocol.md` and friends, which document an
  already-public plaintext protocol — no new disclosure.
- `include uv.lock` publishes pinned versions and hashes; that is a benefit
  (bill of materials), not a leak.
- The **wheel** is unaffected: top level is exactly `powerpetdoor/` and
  `pypowerpetdoor-0.3.0.dist-info`; `0` `tests/` or `docs/` entries; `py.typed`
  present. `include_package_data` does not drag the new grafts in.

**Deep-nesting parse/serialize asymmetry — no finding.** A frame that
`json.loads` accepts but `json.dumps`/`str()` later rejects would put a
`RecursionError` on the receive path. There is no such window
(`/tmp/ppd_attack/a2_depth.py`, `a2b_depth.py`): `json.loads` accepts to depth
**9,997** and refuses at 9,998 with `RecursionError` — which
`_dispatch_frame` explicitly catches — and at 9,997 `json.dumps(msg)`,
`str(state)` and `sanitize_text(state)` all succeed, at every extra stack depth
from +0 to +200 frames. The deepest nesting that fits one legal 64 KiB frame is
32,748, well past the refusal point.

**Eager, uncapped `sanitize_text` on the notification path — no finding.**
`client.py:1163` evaluates `sanitize_text(state)` unconditionally (unlike
`client.py:1737`, which is behind `isEnabledFor(DEBUG)`) and passes no length
cap, so the work happens even with DEBUG off. Measured with the densest legal
payload (ESC × 10,912 in a 65,509-byte frame):

```
  sanitize_text(state)           min=   3.86 ms  median=   3.91 ms
  -> 256 KiB read holds 4.0 such frames = 15.43 ms of eager sanitize per callback
  sanitize_text(list of 21825)  min=   0.95 ms
  FrameScanner.feed() on 256 KiB of legal {}  min=  81.06 ms
  -> eager-sanitize cost is x0.19 of the framing cost a peer already has
```

15.43 ms against 81.06 ms the peer already commands for the same read. Inside
the envelope; not filed.

**`sanitize_text` is not total on its argument — no finding (unreachable).**
`str(text)` at `sanitize.py:81` propagates whatever `__str__` raises
(`sanitize_text(Boom())` → `RuntimeError: boom`). Every value reaching it from
the wire is a `str`/`int`/`float`/`bool`/`None`/`list`/`dict` produced by
`json.loads`, none of which can raise in `__str__`. Negative `limit` values also
behave oddly (`limit=-1` truncates by one character) but no caller passes one.

**`_recorded_size` fallback — no finding.** `client.py:261-274`. The
`len(json.dumps(msg))` path only runs when `frame_size is None`, which on the
receive path never happens: `_dispatch_frame` is the only internal caller and
always passes `frame_size=len(frame)`. Confirmed by grep across `src/` and
`tests/`; the only `frame_size`-free callers are tests and the fuzz harness,
both of which absorb it via `**_kwargs`.

**`EventThrottle` schedule arithmetic — no finding.** `MAX_THROTTLE_INTERVAL`
4096 and `THROTTLE_QUIET_PERIOD` 60 s bound both ends. Unit sweep on one
generation with a frozen clock: 1 → 1 record, 10 → 5, 100 → 8, 1,000 → 11,
10,000 → 15. Logarithmic as designed; the time path can add at most one record
per quiet period and `time.monotonic` cannot run backwards.

**`_LOGGER.warning("...: %r", msg)` at `client.py:1825`** — unthrottled,
uncapped and un-sanitized, but unreachable from the wire: `FrameScanner` only
emits frames beginning with `{`, and `json.loads` of such a frame yields a
`dict` or raises. Reachable only by a third party calling `process_message`
directly with its own value.

**Release and CI workflows — no finding.** Every third-party action is pinned to
a full commit SHA (`actions/checkout@11d5960`, `astral-sh/setup-uv@37802ad` in
all four call sites including the composite retry action,
`christopherhx/gitea-upload-artifact@81f940d`,
`gitea-download-artifact@75635f3`, `codecov/codecov-action@b9fd7d1`,
`pypa/gh-action-pypi-publish@dc37677`, and the reusable
`neuromancy/workflows/...@5d9eb7f`). The `publish` job is `environment: pypi`
with `id-token: write` and `needs: test`, and `[build-system] requires` pins
`setuptools==84.0.0`/`wheel==0.48.0` exactly, so `uv build`'s isolated
environment cannot pull an arbitrary backend into the job that signs the
artifact.

---

### Informational

**I1 — `CODECOV_TOKEN` is job-scoped rather than step-scoped.**
`.github/workflows/test.yml:242-243` sets it in the `coverage-report` job's
`env:`, so it is present in the environment of `coverage combine`, `coverage
html/json`, `python scripts/generate_gaps_report.py` and the `git push` step,
not only the upload step. This is a deliberate trade-off — the
`if: ${{ env.CODECOV_TOKEN != '' }}` gate on the upload step requires job-level
scope — and the token is a low-value upload credential, so I am filing it as
informational only. If the gate is ever restructured (e.g. `secrets.CODECOV_TOKEN
!= ''` checked another way), move the `env:` down to the step.

**I2 — `frame_size` over-reports by up to 4x on non-ASCII input.**
`decode("ascii", errors="backslashreplace")` turns one non-ASCII wire byte into
four characters, and `len(frame)` counts characters: measured `wire=2006` →
`reported_max=8006` (0.25x). The error is in the conservative direction — it
exaggerates a hostile peer's volume rather than hiding it — and the sibling
counter `self._non_ascii.record(len(data))` records true bytes, so an operator
has an accurate number available. Not worth a code change on its own; noted so
the discrepancy is not mistaken for a regression of round-9 F2.

**I3 — escapes are ambiguous with literal backslashes.** `sanitize_text` does
not escape `\`, so a peer sending the four literal characters `\x1b` produces a
log record indistinguishable from one where a real ESC was escaped (measured:
`in='\\x1b'` → `out='\\x1b'`, unchanged). Likewise the truncation marker
`...(truncated)` is forgeable by ending a value with it. Neither injects
anything; both only let a peer make a record *look* like something happened that
did not. If L1's `sanitize_field` is implemented, doubling backslashes there
closes this at zero extra cost.
