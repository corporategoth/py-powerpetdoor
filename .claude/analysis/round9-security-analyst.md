# Security Analyst Analysis — Round 9

Commit `145cf05` ("Round 8 fixes (refuter-approved list)"), working tree clean
throughout, Python 3.13.13, `.venv` as locked.

Scope note carried from the brief: the device protocol is plaintext JSON over
TCP with no authentication or encryption **by device design**. Nothing below
asks for TLS on it. Nothing below asks the code to *reject* a byte it accepts
today: both Medium findings are about how an accepted value is **rendered into
a log record**, and both fixes leave the wire, the parse, the dispatch and the
cache byte-for-byte identical.

Every number in this report came out of a harness that was executed on this
machine at this commit. Harnesses lived in `/tmp/r9sec/`, were run, and were
deleted; every spawned daemon was terminated (`pgrep ppd-simulator` → none).
No repository file was modified (`git status --porcelain` empty at start and at
end) except this report.

Gates re-run at this commit before starting:

```
uv run pytest -q                       -> 2725 passed in 38.40s
uv run pytest tests/fuzz -q            -> 47 passed in 5.60s
uv run ruff check src tests scripts    -> All checks passed!
uv run ruff format --check ...         -> 81 files already formatted
uv run mypy src                        -> Success: no issues found in 31 source files
uv lock --upgrade --dry-run            -> Resolved 28 packages in 79ms / No lockfile changes detected
```

Breadth note: this is the ninth sweep. The wire *parse* path has been attacked
in rounds 2–8 and I found nothing new there (56 pathological shape/side
combinations, 0 escapes — see **Areas Reviewed With No Findings**). The two
Medium findings come from a surface no previous round measured: the **log-sink
boundary** — how much a peer-chosen value costs once it reaches a `logger.*`
call, and whether `sanitize_text` actually makes it writable.

---

## Summary

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 2 |
| Low | 2 |
| Informational | 1 |

- **Medium 1** — Four per-frame log sites on the **shipped client/facade** path
  have neither the `EventThrottle` nor the `MAX_LOGGED_LENGTH` cap that rounds 6
  and 7 gave to all four of their siblings. Measured at 20,000 frames:
  **20,033 records / 2,423,741 log bytes (×6.06)** against the throttled
  siblings' **33 records / 3,561 bytes (×0.016)**; a single 43,699-byte frame
  produces one **65,638-byte** log record where the capped sibling produces 293.
  Against a real TCP peer the shipped client wrote **4.23 MB/s** into its log
  file. One of the four also fires on ordinary traffic from a newer firmware.
- **Medium 2** — An unpaired surrogate (`"\ud800"` — legal JSON, accepted by
  `json.loads`, arriving as pure ASCII on the wire) is **not** matched by
  `sanitize.py`'s `_CONTROL_CHAR_RE`, so it survives the project's single
  sanitization boundary and reaches the handler, which cannot encode it. On the
  shipped client with an ordinary `logging.FileHandler(encoding="utf-8")`,
  2,000 hostile frames produced **0 bytes and 0 lines in the log file**, 2,000
  dropped records, and **3,656,000 bytes** of logging-internal tracebacks on
  stderr (×29.97). The identical frame with `ESC` instead of the surrogate
  logs perfectly (274,000 bytes, 2,000 lines, 0 dropped). An attacker deletes
  the record that documents the attack, on demand, by adding six characters.
- **Low 1** — Round 8's `finally: self._update_flow()` closes the wedge only
  when the backlog is *below* `pause_at`. Above it, a raising `_dispatch` still
  ends at `backlog=300 inflight=0 paused=True` with no pump scheduled and
  nothing that can schedule one — the exact state round 8 called "the permanent
  wedge". The shipped docstring's claim ("the worst case is one frame lost") is
  measurably false. **No in-tree input reaches it** (0/56); this is filed as an
  incomplete guard on a component documented as "must not depend on its callback
  being total", not as an exploitable defect.
- **Low 2** — `.github/workflows/test.yml:78` interpolates
  `${{ github.base_ref }}` directly into a `run:` shell string. It is the only
  `${{ github.* }}` → `run:` interpolation in the repository and it arrived in
  round 8. Injection mechanism proven; precondition is repo write access, and
  the job holds no secrets, so impact is limited — but the fix is three lines.
- **Informational 1** — The published sdist ships 9 of the repo's test modules
  and none of the machinery that makes them run (`conftest.py`, `__init__.py`,
  `tests/simulator/`, `tests/fuzz/`). `pytest` in the unpacked sdist: **1 error,
  0 tests**. A third party cannot verify this project's own security tests from
  the artifact it publishes.

All round-8 fixes verified and hold, with one qualification (Low 1). See
**Round 8 Fix Verification**.

---

## Findings

### 1. [Medium] Four per-frame log sites in the shipped library are neither throttled nor length-capped, while their four siblings are: ×6.06 write amplification and a 65,638-byte single record, in the host application's log

**Files (all on the shipped `client.py`/`door.py` path — the Home Assistant
deployment target, not the simulator):**

- `src/powerpetdoor/client.py:1803-1806` —
  `_LOGGER.warning("Ignoring unusable msgID %r in device response; no future to resolve", reply_msg_id)`
  — no throttle, no `MAX_LOGGED_LENGTH`
- `src/powerpetdoor/door.py:264` —
  `logger.warning("Unknown door status from device: %r", value)` — no throttle,
  no cap
- `src/powerpetdoor/door.py:1576` —
  `logger.warning("Ignoring malformed schedule update from device: %s", err)`
  (the `err` text embeds `{value!r}` of the untrusted payload) — no throttle,
  no cap
- `src/powerpetdoor/door.py:1494-1497` —
  `logger.warning("Ignoring non-mapping hardware info: %s", sanitize_text(data, MAX_LOGGED_LENGTH))`
  — capped, but no throttle

The four siblings that rounds 6 and 7 fixed, for contrast:
`client.py:1749-1753` (`_bad_frames`), `client.py:1785-1788` (`_bad_messages`),
`client.py:1856-1859` (`_device_errors`), plus the simulator's twins.
`client.py:1844-1853` states the doctrine this finding is about verbatim: *"a
peer packing 11-byte `{"CMD":"a"}` envelopes bought one unthrottled WARNING per
frame at x6.64 the wire bytes, in the host application's log (round-7 security
L3). This is the shipped library; for the Home Assistant deployment target that
is the whole instance's log."* `client.py:1803` is the same shape at ×6.06 and
was not fixed. `sanitize.py:28-33` states the other half: *"the constant of any
per-frame log line has to be bounded independently of how often the line
fires"*.

**Reproduction A — 20,000 frames, logger at WARNING (the Home Assistant
default), real `PowerPetDoorClient.data_received` + real `PowerPetDoor` facade**
(`/tmp/r9sec/h5_final.py`):

```
N = 20,000 frames, logger at WARNING (the Home Assistant default)
site                                    thr  cap       wire        log    recs      amp  longest
client.py:1803  unusable msgID          NO   NO      400000    2423741   20033   x6.059      126
door.py:264    unknown door status      NO   NO     1200000    1760000   20000   x1.467       87
door.py:1576   malformed schedule       NO   NO     1420000    2820000   20000   x1.986      140
client.py:1785 malformed message [r6]   yes  yes      40000       3731      33   x0.093      132
client.py:1856 device error [r7]        yes  yes     220000       3561      33   x0.016      126
client.py:1749 undecodable frame [r6]   yes  yes      60000       4789      33   x0.080      161
```

**20,033 records vs 33.** The frames are 20-byte (`{"CMD":0,"msgID":[]}`),
59-byte and 71-byte legal JSON envelopes; nothing is malformed at the framing
layer.

**Reproduction B — one frame just under the 64 KiB framing cap; is there a
length cap?** (same harness):

```
ONE frame just under the 64 KiB framing cap (is there a length cap?)
client.py:1803  msgID = 21840-item list        wire=43699   log=66055    recs=3   longest_record=65638
door.py:264    door_status = 65000 chars       wire=65059   log=65087    recs=1   longest_record=65086
door.py:1576   schedule index = 64000 chars    wire=64069   log=64139    recs=1   longest_record=64138
client.py:1856 device error, 64000 chars [r7]  wire=64026   log=416      recs=2   longest_record=293
client.py:1749 undecodable, 65000 chars [r6]   wire=65002   log=502      recs=2   longest_record=372
```

A **65,638-byte single log record** where the round-7-fixed sibling, fed a
frame of the same size, emits 293 bytes. The `%r` at `client.py:1803` and
`door.py:264` *does* neutralize C0 escapes (repr escapes them), so this is
purely volume, not ANSI injection.

**Reproduction C — real TCP, hostile "door" answering a real
`PowerPetDoorClient.connect()`, records written to a real
`logging.FileHandler`** (`/tmp/r9sec/h4_tcp_and_sim.py`):

```
=== 1. hostile door over real TCP -> shipped PowerPetDoorClient ===
  5.0s: hostile door wrote 7.25 MB of 20-byte frames
  client log file: 21153653 bytes / 174829 lines  -> 4.23 MB/s of log
  sample: 2026-08-22 15:27:13,887 WARNING powerpetdoor.client: Ignoring unusable msgID [] in device response; no future to resolve
```

174,829 records is one per frame consumed; 174,829 × 20 B = 3.50 MB consumed
against 21.15 MB written — ×6.05, matching the in-process figure. **4.23 MB/s**
into the host application's log file, sustained, from a peer on the LAN.

**Attack scenario.** The door protocol is unauthenticated by device design, so
anything on the LAN can answer as the door (round 8's finding E used exactly
this shape). A peer that answers `{"CMD":0,"msgID":[]}` in a tight loop fills
the Home Assistant instance's log — and its disk — at ~4 MB/s, while the
throttled sites right next to it in the same function cost ×0.016. Nothing
accumulates in the client and no connection is dropped, so this runs
indefinitely; it is the identical failure mode round 6 finding 2 and round 7 L3
were filed and fixed for.

`door.py:264` additionally fires **on ordinary traffic**: a firmware revision
that reports a status string this library does not know produces one uncapped
WARNING per status update, forever, on a correctly-functioning installation.
That is not an attack, it is a log flood shipped to every user of a newer door.

**Recommendation.** Apply the treatment the four siblings already have — this
narrows nothing, refuses nothing and changes no byte on the wire; it changes
only how much of an already-accepted value is written to a log record:

1. Give each site an `EventThrottle` (the client already holds `_bad_frames`,
   `_bad_messages`, `_device_errors`; add e.g. `_bad_msg_ids`, and a facade-side
   throttle for the `door.py` sites). The first occurrence is still reported
   immediately and in full, the running totals ride every summary, and
   `THROTTLE_QUIET_PERIOD` restores immediate reporting for a fresh burst — so
   `door.py:264`'s stated requirement (*"never silently claim a possibly-open
   door is closed (L16)"*) is fully preserved.
2. Replace `%r` / `%s` of the peer value with
   `sanitize_text(value, MAX_LOGGED_LENGTH)` at all four sites, so one frame can
   never buy a 65 KB record. (`sanitize_text` already escapes control
   characters, so this does not lose the ANSI protection `%r` was providing —
   but see Finding 2, which must land with it.)
3. `client.py:1830` (`_LOGGER.exception("Error handling %s response: %s", cmd, json.dumps(msg))`)
   is the same class — unthrottled, uncapped, plus a full traceback. I could not
   reach it (every registered handler's payload access is guarded; round 7
   measured the same). Capping the `json.dumps(msg)` there is cheap insurance
   and is listed for completeness, not as a proven defect.

---

### 2. [Medium] An unpaired surrogate from the wire is not neutralized by `sanitize_text`, so the log record that documents the hostile input is dropped from the log file entirely: 2,000 frames → 0 log lines and 3.66 MB of stderr tracebacks

**File:** `src/powerpetdoor/sanitize.py:23` —
`_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")`, used by
`sanitize_text` (`sanitize.py:36-57`), which the module docstring declares is
*"the single implementation used by every such sink"* and whose docstring
promises *"the result is safe to write to a log record or a terminal."*

It is not. The class covers C0, DEL and C1, and stops there. `U+D800`–`U+DFFF`
are not covered, and an unpaired surrogate cannot be encoded to UTF-8 — so the
"sanitized" string is exactly the one thing a log handler cannot write.

The value is fully reachable from the wire and arrives as **pure ASCII**: a peer
sends the six ASCII characters `\ud800` inside a JSON string; the receive path's
`data.decode("ascii", errors="backslashreplace")` passes them through unchanged;
`json.loads` turns the escape into an unpaired surrogate; the surrogate is then
handed to `sanitize_text` by any site that renders a *decoded* value (as opposed
to `%r`, which escapes it).

**Reproduction A — the regex, and both twins at scale**
(`/tmp/r9sec/h7_surrogate.py`; records go to a real
`logging.FileHandler(encoding="utf-8")` — the ordinary host-application
configuration — with stderr captured):

```
=== regex coverage ===
  _CONTROL_CHAR_RE = '[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f]'
    U+001B: matched by the sanitizer? True
    U+007F: matched by the sanitizer? True
    U+009F: matched by the sanitizer? True
    U+D800: matched by the sanitizer? False
    U+DBFF: matched by the sanitizer? False
    U+DC00: matched by the sanitizer? False
    U+DFFF: matched by the sanitizer? False

=== simulator twin (WARNING, the daemon's default) ===
  cmd = lone surrogate, 2000 frames            wire=52000  logfile=1410   lines=11   stderr=20405   dropped_records=11   stderr_amp=x0.39
  CONTROL: cmd = ESC (a covered control char)  wire=60000  logfile=2541   lines=22   stderr=0       dropped_records=0    stderr_amp=x0.00

=== shipped client + facade (WARNING) ===
  fwInfo = lone surrogate string, 2000 frames  wire=122000 logfile=0      lines=0    stderr=3656000 dropped_records=2000 stderr_amp=x29.97
  CONTROL: fwInfo = ESC string                 wire=130000 logfile=274000 lines=2000 stderr=0       dropped_records=0    stderr_amp=x0.00
```

The control rows are what make this specific: the **same frame, same site, same
handler**, with `ESC` (a character the regex does cover) instead of the
surrogate, logs 2,000 lines and 274,000 bytes with zero stderr. Swapping six
characters takes the shipped client from *2,000 records written* to **0 records
written**, and moves 3.66 MB to stderr instead — ×29.97 the wire bytes.

**Reproduction B — one frame, verbatim** (same harness):

```
  one frame, simulator                         wire=37   logfile=127  lines=1  stderr=1866  dropped_records=1  stderr_amp=x50.43
    --- Logging error ---
    Traceback (most recent call last):
      File ".../logging/__init__.py", line 1154, in emit
        stream.write(msg + self.terminator)
    UnicodeEncodeError: 'utf-8' codec can't encode character '\ud800' in position 93: surrogates not allowed
    Call stack:
      ...
```

37 wire bytes buy a 1,866-byte diagnostic. Note where it comes from: the
`logging` module's own `handleError`, which is **outside every `EventThrottle`
this project has built** — the throttle governs whether `logger.warning()` is
*called*, not what the handler does when the record turns out to be unwritable.

**Reproduction C — a real `ppd-simulator --daemon`, one hostile frame**
(`/tmp/r9sec/h8_ctl.py`, daemon output captured to a file):

```
=== ANSI/surrogate egress in the daemon log ===
  daemon log: 5701 bytes
  raw 0x1b bytes: 0                       <- the ESC path is still correct
  literal '\x1b' text: 1
  '--- Logging error ---': 1
  'UnicodeEncodeError': 1
  34| 2026-08-22 ... [WARNING] Simulator: Unknown command: \x1b[31mRED
  36| 2026-08-22 ... [WARNING] Simulator: Unknown command: \ud800SURR
  37| --- Logging error ---
  76| Arguments: ('\ud800SURR',)
```

The shipped simulator surfaces it too. The daemon's *stderr* stream survives
only because CPython gives `sys.stderr` the `backslashreplace` error handler by
default — an accident of the stream, not anything this code does; any handler
the operator configures with a file, a socket or a `QueueHandler` (which is what
Home Assistant uses) gets the Reproduction A result instead.

**Attack scenario.** `door.py:1494` (non-mapping `fwInfo`),
`door.py:1177`/`1179` (schedule index), `client.py:1124` (`sensorState`),
`door.py:233` (`_log_rejected`) and the simulator's `_report_unknown_command`
and `_rejections` sites all render a *decoded* wire value through
`sanitize_text`. An attacker who wants a probe to leave no trace in the
operator's log file appends `\ud800` to the field the log line reports —
the guard still fires, the value is still rejected, and the record that says so
never lands. That is a paper-trail defeat achieved from an unauthenticated LAN
peer with six characters, and it compounds with Finding 1: at
`door.py:1494` the site is unthrottled, so *every one* of the 2,000 records was
lost, not one in a doubling schedule.

**Recommendation.** Extend the character class to cover the surrogate range, so
surrogates are escaped exactly like every other unsafe code point:

```python
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
```

This narrows nothing and refuses nothing: the frame is still accepted, still
parsed, still dispatched, still rejected-or-cached by exactly the same code —
only the *rendering into a log record* changes, which is the sole purpose of
this function. Two details worth handling in the same change:

- The replacement is `f"\\x{ord(m.group()):02x}"`, which renders `U+D800` as
  `\xd800`. Prefer a width-aware spelling (`\\x{o:02x}` for `o <= 0xFF`,
  `\\u{o:04x}` above it) so the escape is unambiguous.
- Pin it with a boundary test at `U+D7FF` / `U+D800` / `U+DFFF` / `U+E000`, per
  the project's rule 8, and one end-to-end test asserting that a frame carrying
  `"\ud800"` produces a record that `.encode("utf-8")` accepts.

---

### 3. [Low] Round 8's `finally: _update_flow()` closes the wedge only below `pause_at`; above it the dispatcher still ends paused with nothing able to pump it, and the shipped docstring says otherwise

**File:** `src/powerpetdoor/framing.py:619-630` (`FrameDispatcher._pump`), and
the claim at `src/powerpetdoor/framing.py:609-617`: *"with the `finally` the
worst case is one frame lost."*

`_schedule_pump()` sits **inside** the `try`, so a raising `_dispatch` skips it;
only `_update_flow()` runs in the `finally`. When the backlog is above
`pause_at`, `_update_flow()`'s job is to **pause** — so the `finally` puts the
connection into precisely the state round 8 diagnosed (`backlog>0`,
`inflight=0`, `paused=True`, `_pump_scheduled` already cleared by
`_resume_pump`), with `_on_dispatched_done` unreachable (nothing in flight),
`submit` unreachable (reading paused) and `_resume_pump` unreachable (not
scheduled).

**Reproduction** (`/tmp/r9sec/h3_dispatch.py`, part A: a `FrameDispatcher` with
a deliberately non-total callback, a counting transport and a loop exception
handler installed):

```
=== A. FrameDispatcher: does the round-8 `finally` prevent the wedge? ===
    MAX_INFLIGHT_FRAMES=64 MAX_FRAME_BACKLOG=256
  backlog ABOVE pause_at after the raise     backlog=300  inflight=0 paused=True   pause_reading x1 resume_reading x0 loop_handler_hits=1 dispatched=65
      after a later submit()               backlog=0    paused=False dispatched=366
  backlog BELOW pause_at after the raise     backlog=10   inflight=0 paused=False  pause_reading x0 resume_reading x0 loop_handler_hits=1 dispatched=65
      after a later submit()               backlog=0    paused=False dispatched=76
```

The `after a later submit()` line is the harness calling `submit()` by hand to
show the state is recoverable *in principle*. A real connection cannot: reading
is paused, so no further `data_received` arrives, so `submit()` is never called
— and the peer's FIN is never read either, which is what held the fd and the
`DoorSimulator.protocols` slot in round 8.

**Reachability — this is why it is Low and not Medium.** I could not construct
any input that makes either `_dispatch_frame` raise. 28 pathological
brace-balanced shapes (4,301-digit integers in value/key/exponent/negative
position, 9,999-deep list nesting, 5,000-deep object nesting, unpaired
surrogates in `cmd`/`msgId`, `NaN`/`Infinity`, duplicate keys, a 60,000-digit
fraction, 400-deep containers in `msgId`/`cmd`/`index`/`schedules`, non-dict
schedules, dict/bigint `msgID`, a 60,000-char timezone) fed through the real
`data_received` on **both** twins:

```
=== B. can any wire shape still make _dispatch_frame RAISE? ===
  shapes tested: 28 x 2 sides = 56
  data_received escapes / stuck dispatchers : NONE
  loop exception-handler hits               : 0
```

Recursion headroom was checked too, since that is the mechanism that would do
it: `json.loads` accepts nesting to depth 9,994 at module scope, and `str()`,
`repr()`, `json.dumps()` and `f"{obj!r}"` all succeed on an object of exactly
that depth — there is no window where the parse succeeds and a downstream
render blows the stack.

**Attack scenario.** None today. What is filed is that the guard round 8 shipped
does not do what it says, on a class the module documents itself as defending
(`framing.py:614-617`: *"This dispatcher is a shared component and must not
depend on its callback being total"*), and that the next callback added to it —
or the next `json` behaviour change of the kind that produced round 8's finding
— re-opens the fd-and-slot leak rather than losing one frame.

**Recommendation.** Two lines, no behaviour change on any non-raising path:

```python
        finally:
            if self._backlog and self._inflight < self._max_inflight:
                self._schedule_pump()
            self._update_flow()
```

and drop the now-duplicated call from inside the `try`. Then correct
`framing.py:609-617` and the round-8 CHANGELOG entry, both of which currently
claim the stuck state is impossible. A test asserting
`backlog == 0 and not paused` after a raising dispatch with
`MAX_FRAME_BACKLOG + 44` frames queued would have caught this.

---

### 4. [Low] `${{ github.base_ref }}` is interpolated into a `run:` shell string — the only `${{ github.* }}` → `run:` interpolation in the repository, added in round 8

**File:** `.github/workflows/test.yml:78` (the `changelog` job added by
`145cf05`):

```yaml
        run: |
          base="origin/${{ github.base_ref }}"
```

GitHub Actions substitutes the expression into the shell script *before* bash
parses it, so the branch name is executed as shell source, not read as data.

**Reproduction** (`/tmp/r9sec/`, exact substitution semantics reproduced
locally):

```
$ git check-ref-format --branch 'main$(id)'
main$(id)                                  <- git accepts $ ( ) in a branch name

$ base_ref='main$(id > /tmp/r9sec/INJECTED.txt; echo INJECTED)'
$ bash -c "base=\"origin/$base_ref\"; echo \"base=\$base\""
base=origin/mainINJECTED
$ cat /tmp/r9sec/INJECTED.txt
uid=1000(prez) gid=1000(prez) groups=1000(prez),...
```

The command substitution ran; the file was created.

**Attack scenario, stated honestly.** `github.base_ref` is the branch a PR
*targets*, which must already exist in the base repository — so an attacker
needs push access to create the branch. The `changelog` job holds no secrets and
declares no `permissions:`, and GitHub forces a read-only token for fork PRs, so
the privilege actually gained over "already has write access" is close to zero.
This is filed because it is the single instance in the repo of a pattern rounds
6 and 7 explicitly verified as absent (*"No secret is interpolated into any
`run:` string"*), because the whole repository is otherwise SHA-pinned and
`permissions`-minimal, and because the fix is three lines.

**Recommendation.** Pass the context through the environment, where it is data:

```yaml
        env:
          BASE_REF: ${{ github.base_ref }}
        run: |
          base="origin/$BASE_REF"
```

Consider also adding `permissions: contents: read` at the top of `test.yml`;
only `release.yml` declares one today, so every `test.yml` job runs with the
repository default.

---

### 5. [Informational] The published sdist ships nine test modules and none of the machinery that runs them: `pytest` on the unpacked sdist collects 0 tests and errors

**Files:** `pyproject.toml` (no `MANIFEST.in`, no `[tool.setuptools] sdist`
configuration); `.github/workflows/release.yml:50` (`run: uv build` — this is
the artifact that is published).

**Reproduction** (`/tmp/r9sec/`; `uv build` from a clean copy of this commit,
then the sdist unpacked and run with the project's own interpreter):

```
$ tar tzf pypowerpetdoor-0.3.0.tar.gz | grep '^pypowerpetdoor-0.3.0/tests/'
tests/test_client.py  tests/test_docs_accuracy.py  tests/test_door.py
tests/test_exports.py tests/test_framing.py        tests/test_gaps_report.py
tests/test_sanitize.py tests/test_schedule.py      tests/test_tz_utils.py

$ ls repo tests/     ->  additionally: conftest.py  __init__.py  fuzz/  simulator/  TESTING_GAPS.md

$ python -m pytest -q          # inside the unpacked sdist
tests/test_client.py:79: in <module>
    from tests.conftest import bigint_frame, nested_frame
E   ModuleNotFoundError: No module named 'tests.conftest'
1 error in 1.50s
```

The wheel is clean — 44 files, source and `scripts/*.yaml` only; no `.claude/`,
no `.hypothesis/`, no `.coverage`, no `coverage.json`. So there is no
information leak; the issue is verifiability in the other direction.

**Why this is a security note and not just packaging.** Every security fix in
rounds 1–8 is asserted by a test, and the fuzz suite that reaches the round-8
decode shapes lives in `tests/fuzz/`. A downstream packager, an auditor or a
distribution's build system that takes the sdist — the normal way third parties
independently verify a Python project — cannot run a single one of them. The
sdist advertises a suite it cannot execute, which is worse than shipping none.

**Recommendation.** Pick one and state it:

- ship the whole thing (add a `MANIFEST.in` with
  `graft tests`, `include tests/TESTING_GAPS.md`, `graft docs`,
  `include CHANGELOG.md`, `include uv.lock`) so the sdist is independently
  verifiable; or
- ship none of it (`prune tests`) so the artifact does not imply a suite it
  lacks.

Either way, add a CI step that unpacks the built sdist and runs
`pytest --collect-only` (or asserts `tests/` is absent), so this cannot drift
again silently.

---

## Round 8 Fix Verification

**1. Decode escape caught at both twins — VERIFIED.**
`client.py:1727` and `simulator/protocol.py:462` both read
`except (ValueError, RecursionError) as err:`, and the `try` body is the single
statement `msg = json.loads(frame)` — so the widened clause covers the decode
and nothing else. Against a real `ppd-simulator --daemon`
(`/tmp/r9sec/h2_daemon.py`), 25 connections each carrying the round-8 wedge
payload (`{x}`×64 + poison + `{x}`×300), for both poison shapes:

```
daemon pid=2233093  door=127.0.0.1:34401  ctl=127.0.0.1:34402
baseline: fds=9 RSS=32.9 MB ctl=261ms  ['Clients: none']

[bigint(ValueError)] 25 conns x 5399 B = 0.135 MB in 0.00s
  during attack : fds=34 RSS=33.4 MB ctl=228ms ['Clients: 25 clients']
  attacker sockets answered a later valid command: 25/25
  fresh honest clients served: 10/10
  2s after attacker close: fds=9 RSS=33.5 MB ctl=153ms ['Clients: none']

[nested(RecursionError)] 25 conns x 21096 B = 0.527 MB in 0.00s
  during attack : fds=34 RSS=35.8 MB ctl=161ms ['Clients: 25 clients']
  attacker sockets answered a later valid command: 25/25
  fresh honest clients served: 10/10
  2s after attacker close: fds=9 RSS=35.8 MB ctl=204ms ['Clients: none']

final: fds=9 RSS=35.8 MB ctl=153ms  ['Clients: none']
--- daemon log: 129446 bytes
  'Traceback': 0 | 'RecursionError': 0 | 'Exception in callback': 0
  'message handler task failed': 0 | 'Error handling command': 0
  'JSON parse error': 950
```

Round 8 measured 59 fds and `Clients: 50` **forever**, with 0/10 sockets
answering. Now: fds return to the 9 they started at, the client count returns to
`none`, the attacker's own sockets stay fully functional (25/25), every honest
client is served (10/10), and there is not one traceback. The frames land on the
throttled bad-frame path (19 records per connection for 364 bad frames — the
doubling schedule).

**2. `_update_flow()` in a `finally` — VERIFIED PRESENT, PARTIALLY EFFECTIVE.**
`framing.py:629-630`. It removes the wedge when the backlog is below `pause_at`
(measured: `backlog=10 paused=False`, recovers on the next read). It does **not**
remove it above `pause_at` (measured: `backlog=300 inflight=0 paused=True`, no
pump scheduled) — filed as Finding 3. This does not weaken verification item 1:
with `_dispatch_frame` now total, nothing in-tree reaches the raising path
(0/56 shapes).

**3. Hold-time overflow bounded by `sys.float_info.max` — VERIFIED.**
`door.py:173` / `door.py:1463-1469` (`/tmp/r9sec/h4_tcp_and_sim.py`, 50 frames
each):

```
  400-digit int    escapes=None  hold_time=2.0        records=51  ERROR/CRIT=0
  4300-digit int   escapes=None  hold_time=2.0        records=51  ERROR/CRIT=0
  1e308 float      escapes=None  hold_time=1e+306     records=1   ERROR/CRIT=0
  1.8e308 -> inf   escapes=None  hold_time=1e+306     records=51  ERROR/CRIT=0
  legit 200        escapes=None  hold_time=2.0        records=1   ERROR/CRIT=0
```

Round 8 measured 50 frames → 50 full tracebacks. Now: **0 ERROR/CRITICAL
records** for every shape, no escape from `data_received`, the cache is left at
its last good value, and the rejections log at DEBUG (invisible at the WARNING
default). `1e308` — representable — is still *accepted* and cached as
`1e+306` s, so the bound is representability and not a protocol ceiling, exactly
as the fix documents. The `maximum` parameter is passed by
`_on_hold_time_update` only; I re-swept every other `_keep_*` caller and none
performs float arithmetic on the result (`battery_percent`,
`total_open_cycles`, `total_auto_retracts` store verbatim; `_on_ping`'s
`latency_ms` is computed by the client, not supplied by the device).

**4. Raw-bytes fuzz property — VERIFIED.** `tests/fuzz/test_framing_fuzz.py:352`
onwards; `st.tuples(st.just("raw"), st.binary(max_size=64))` feeds arbitrary
bytes to `data_received` on both sides. `uv run pytest tests/fuzz -q` →
**47 passed in 5.60s**.

**5. Coverage `exclude_lines` anchoring — VERIFIED.** All seven patterns in
`pyproject.toml:136-148` are anchored (`^\s*…`, `#\s*pragma…`, or the
`(^\s*\.\.\.\s*$)|(:\s*\.\.\.\s*$)` pair); no bare-phrase `re.search` remains.

**6. `ThreadedCompleter` + description cache — VERIFIED.**
`prompt_common.py:582` wraps `SimulatorCompleter` in `ThreadedCompleter`. The
cache cap is real and is enforced where it is applied
(`/tmp/r9sec/h6_breadth.py`): describing 552 distinct files leaves
`len(_description_cache) == 40`, i.e. the wholesale clear at
`MAX_DESCRIPTION_CACHE = 512` fires and the dict never exceeds the cap. The
completer touches only module-level caches and the filesystem — it mutates no
simulator state — so moving it to a worker thread introduces no shared-state
race; the one shared mutable is a `dict`, whose individual operations are atomic
in both the GIL and free-threaded CPython builds, and the only possible race
outcome is the cache briefly holding one extra entry.

**7. Script-DSL annotation keys / per-action parameter check — present**
(`scripting.py:271` `STEP_ANNOTATION_KEYS`, `scripting.py:545`); full suite
green.

**8. CHANGELOG CI check — present** (`test.yml:65-89`), and is Finding 4.

---

## Areas Reviewed With No Findings

- **Can the widened `except (ValueError, RecursionError)` swallow evidence at
  scale?** No. The `try` body is the single statement `json.loads(frame)` on
  both twins, so the clause can only ever catch a decode failure — it cannot
  mask a bug in the handler, the dispatch, or the flow control, all of which sit
  outside it. The record it produces carries `err`, so a `RecursionError` and an
  `Exceeds the limit (4300 digits)` `ValueError` remain distinguishable from an
  ordinary `JSONDecodeError` in the log. The reporting is batched, not erased:
  20,000 undecodable frames produced 33 records (×0.080), `flush()` reports the
  suppressed tail at teardown, and the totals ride every summary. Against the
  live daemon, 18,200 bad frames across 50 connections produced 950 records with
  running counts — the throttled shape, working as designed.
- **Can the `finally`-placed `_update_flow()` produce "flow control consistent
  but connection useless"?** Only via the raising path in Finding 3, which no
  input reaches. On every non-raising path the pause/resume ledger balanced
  exactly (`pause_reading x1 / resume_reading x1` across fill-and-drain), the
  backlog always drained to 0, and `paused` always returned to False —
  56 shape/side combinations, plus the four floods in Finding 1's harness, ended
  at `backlog=0 inflight=0 paused=False` with the dispatcher accepting new work.
- **Hostile wire input against both twins, everything except Findings 1 and 2.**
  28 pathological shapes × 2 sides: 0 escapes from `data_received`, 0 loop
  exception-handler hits, 0 stuck dispatchers, 0 "message handler task failed",
  0 "Error handling command". The round-2/3/4 wire guards (unhashable `msgID`,
  unhashable schedule index, every `SET_*` field, `Schedule.from_dict` on a
  non-dict) all still hold; `_handle_set_schedule_list` rejects a non-list
  atomically and the schedule store stays bounded at `MAX_SCHEDULE_INDEX + 1`
  slots because it is a dict keyed on the validated index.
- **Simulator per-frame log amplification.** Measured for completeness and
  **not filed**: the throttled paths are healthy (unknown command ×0.098 /
  20 records per 1,000 frames; rejected `SET_*` ×0.060 / 20 records; undecodable
  ×1.041 / 20 records), and the two unthrottled sites are *success* records —
  `logger.info("Simulator: Power ON")` (×2.895) and
  `"Simulator: Schedule %s saved"` (×1.636). Those are the audit trail for
  operations that actually changed state, a legitimate client drives them at the
  same rate a hostile one does, and ×2.9 is half what the client site in
  Finding 1 achieves. Throttling them would trade a paper trail for very little.
  Recorded, not filed.
- **Reconnect backoff against an accept-then-close peer.**
  `client.py:1321` resets `_reconnect_attempts` on `connection_made`, so a peer
  that accepts and immediately closes never lets the backoff grow
  (`_reconnect_attempts=1` after 12 s). Measured rate is one connection per base
  delay: **11 connections in 12.0 s at `reconnect=1.0`** (0.92/s) and
  **3 in 12.0 s at `reconnect=5.0`** (0.25/s). That is the configured floor, not
  a storm, and each attempt costs the peer a TCP handshake too. Observed, not
  filed.
- **Control-channel script restriction.** Against a live daemon with a
  `--scripts-dir` containing a symlink to `/etc/passwd`, every one refused with
  nothing echoed raw: `run ../../etc/passwd`, `run /etc/passwd`, `run .hidden`,
  `run ./ok`, `run scripts\ok`, `run ../ok`, `run ok/../../../etc/passwd` →
  *"Script paths are not allowed over the control channel"*; `run linked` →
  *"resolves outside /tmp/r9scripts… and cannot be run by name"*;
  `run <ESC>[31mred` → `Unknown script: \x1b[31mred` (escaped, and now naming
  the alternatives); `load /etc/shadow` is not a command. A 300 KB control line
  closes that one connection (`Separator is not found, and chunk exceed…`) and
  `ctl status` keeps answering.
- **ANSI injection.** 0 raw `0x1b` and 0 raw `0x07` bytes in the live daemon's
  entire output across every run in this round, including a frame whose `cmd`
  was `[31mRED` (logged as the literal text `\x1b[31mRED`).
  `_SanitizingFormatter` (`cli.py:49-58`) is installed on every root handler in
  daemon and `--script` mode as well as the interactive one. The C0/C1/DEL
  coverage is correct; the surrogate gap is Finding 2 and is a *writability*
  failure, not an escape-sequence one.
- **Bind addresses.** Unchanged and correct: `0.0.0.0` is the *door* server
  default only (`server.py:116`, `cli.py:601`, `cli.py:912`). The control
  channel defaults to `127.0.0.1` (`cli.py:167`), its help text carries the
  UNAUTHENTICATED warning, and `cli.py:1072` still refuses `--control-host`
  without `--daemon`.
- **No dangerous execution sinks.** Repo-wide grep over `src/` and `scripts/`
  for `eval(`, `exec(`, `pickle`, `subprocess`, `os.system`, `shell=True`,
  `__import__`, `marshal`, `shelve`, `yaml.load(`, `full_load`, `unsafe_load`:
  **nothing**. The only `open(` calls in `src/` are `tz_utils.py:67` (TZif
  resource, read-only), `commands/history.py:40` (`os.open(..., O_CREAT, 0o600)`)
  and `history.py:196,213` (truncation of an already-0600 file, which preserves
  the mode).
- **YAML.** `scripting.py:306` remains the only entry point and uses
  `yaml.safe_load`, wrapped in `except yaml.YAMLError`.
- **Dependencies.** `uv lock --upgrade --dry-run` → *Resolved 28 packages in
  79ms / No lockfile changes detected*: the lock is at the current resolution for
  every package. Runtime surface is one data-only package, `tzdata 2026.3`.
  Extras: `pyyaml 6.0.3` (`safe_load` only), `prompt-toolkit 3.0.53`. Dev:
  `pytest 9.1.1`, `mypy 2.3.1`, `ruff 0.16.4`, `coverage 7.15.4`,
  `hypothesis 6.165.10`. Build backend still exactly pinned at
  `setuptools==84.0.0` / `wheel==0.48.0`, both above the 2024–2026 setuptools
  advisories. No EOL dependency, no advisory applicable to any pinned version.
- **CI supply chain, everything except Finding 4.** Every `uses:` is SHA-pinned,
  including the composite action and the Gitea wiki-sync reusable workflow (the
  only `uses:` that receives a secret). No `pull_request_target`, no
  `workflow_run`, no `ref:`/`repository:` override on any checkout. `permissions:`
  appears once — `id-token: write` on `publish` alone, with `environment: pypi`,
  `needs: test` and OIDC trusted publishing. `CODECOV_TOKEN` is a job-level `env`
  consumed as an action input and guarded by `if: ${{ env.CODECOV_TOKEN != '' }}`.
  The `TESTING_GAPS.md` commit-and-push step is still fenced by
  `github.event_name == 'push'`. `uv sync --locked` everywhere. Every job carries
  `timeout-minutes: 30`.
- **Published artifact contents.** The wheel contains 44 files: source,
  `simulator/scripts/*.yaml`, LICENSE and dist-info. No `.claude/` (which holds
  nine rounds of internal security analysis), no `.hypothesis/`, no `.coverage`,
  no `coverage.json`. The sdist likewise carries no analysis or coverage data.
  The only sdist issue is the reverse one, Informational 1.
- **Recursion headroom.** `json.loads` accepts nesting to 9,994 at module scope,
  and `str()`, `repr()`, `json.dumps()` and `f"{obj!r}"` all succeed on an object
  of exactly that depth — there is no depth at which the parse succeeds and a
  downstream render raises, on either the deep `data_received` stack or the
  shallow task stack.
