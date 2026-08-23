# Wire Fidelity Audit — `2225e28` → `fdfe626`

**Method.** `2225e28` (origin/main) and HEAD were exported to two isolated `/tmp`
trees. Every probe below was run twice, once per tree, with `PYTHONPATH` forced
and `powerpetdoor.__file__` asserted to resolve under the expected tree before
any other import. Payloads were compared as **JSON text plus a recursive type
signature** (`bool:True` / `int:1` / `str:1` are three distinct leaves), never by
dict equality. Four live client↔simulator interop runs were performed over real
TCP sockets across the version boundary. All comparison work was done outside the
repo and deleted; the repo was not modified apart from this file.

---

## Verdict

**The wire is byte-identical in every direction except one, and that one is a
real regression of exactly the kind you asked me to look for.** All 98
`send_message` payloads, all 24 `Schedule.to_dict()` flag combinations, and 52 of
54 `PowerPetDoor` send-path payloads are byte-for-byte and type-for-type
identical; schedule `enabled` is still JSON `true` on the client→device
direction, framing still emits no trailing newline, command names, priorities,
the PING/PONG token contract and the hold-time centisecond arithmetic are
untouched. The exception is **`PowerPetDoor.set_notifications()`, which HEAD
changed from JSON booleans to `"1"`/`"0"` strings citing `docs/protocol.md`** —
the same reasoning, the same document and the same non-authority that produced
the round-5 schedule-`enabled` regression, except this one was never reverted,
is not in the CHANGELOG, and changes an API that shipped in tag `v0.3.0`. On the
accept side HEAD is strictly more tolerant of every response envelope the
baseline handled (the baseline raised `KeyError`/`TypeError` out of 21 of them),
but **41 schedule-payload shapes that the baseline silently accepted are now
rejected with `ValueError`**; two of those — an end time of `hour: 24` and an
absent time block on a selected sensor — are plausible enough from real firmware
to warrant a decision before hardware. Deletion safety is clean: nothing
correctness-bearing went with the ~4,900 removed lines.

---

## Part A: Wire Diff

### A.1 What we SEND

Every payload below was captured at the point the dict is handed to
`enqueue_data`, and separately as the **actual bytes passed to
`transport.write()`** through the real `dequeue_data()` → `_send_data()` path.

| Payload group | Compared | Result |
|---|---|---|
| `send_message(COMMAND, cmd)` for all 49 `CMD_*` in `const.py` | 49 | **IDENTICAL** |
| `send_message(CONFIG, cmd)` for all 49 `CMD_*` in `const.py` | 49 | **IDENTICAL** |
| `send_message(PING, token)` | 1 | **IDENTICAL** |
| `send_message(COMMAND, PONG)` | 1 | **IDENTICAL** |
| `send_message` kwargs passthrough (`index`, `holdTime`, `tz`, mixed `True`/`False`/`1`/`"1"`) | 4 | **IDENTICAL** (types preserved exactly) |
| `msgId` sequencing (4 consecutive sends) | 1 | **IDENTICAL** (`[1,2,3,4]`) |
| Priority assigned per command (`COMMAND_PRIORITIES` lookup) | 98 | **IDENTICAL** |
| Raw socket bytes: `OPEN`, `GET_SETTINGS`, `PING`, `SET_SCHEDULE`, `SET_HOLD_TIME` | 5 | **IDENTICAL** — same length, same bytes, **no trailing newline in either tree** |
| `door.open()` / `open_and_hold()` / `close()` / `toggle()` (both branches) / `cycle()` | 6 | **IDENTICAL** |
| `door.set_inside_sensor` / `set_outside_sensor` / `set_power` / `set_auto` / `set_safety_lock` / `set_autoretract` / `set_pet_proximity_keep_open`, True and False | 14 | **IDENTICAL** |
| `door.set_hold_time()` × 11 values incl. `0.01`, `1.005`, `3.999`, `655.35` | 11 | **IDENTICAL** (`int(seconds*100)` unchanged; float-representation edges identical) |
| `door.set_timezone()` | 1 | **IDENTICAL** |
| `door.get_schedule` / `set_schedule` (default, inside, outside) / `delete_schedule` | 5 | **IDENTICAL** |
| `door.refresh_schedules()` (`GET_SCHEDULE_LIST` + per-index `GET_SCHEDULE`, same order) | 3 | **IDENTICAL** |
| `door.refresh_status` / `refresh_settings` / `refresh_battery` / `refresh_stats` / `refresh_hardware_info` / `refresh` | 11 | **IDENTICAL** |
| **`door.set_notifications()` — all-None and mixed** | **2** | **DIFFERS — see A.2** |
| `Schedule.to_dict()`, all 24 combinations of `enabled` × `inside` × `outside` × 3 day masks | 24 | **IDENTICAL** — including key order; `enabled` is JSON `true`/`false` in both |
| `schedule_template` | 1 | **DIFFERS (key order only)** — see A.3 |
| `compress_schedule()`, 9 cases | 9 | **8 differ by key order only**; values and types identical in all 9 |
| `compute_schedule_diff()`, 9 cases | 9 | **4 identical; 4 differ by key order only; 1 semantic — see A.4** |

**Total: 303 payloads compared. 300 are semantically and type-identical. 1
differs in value (`SET_NOTIFICATIONS`). 1 differs in behaviour
(`compute_schedule_diff` on string day flags, in the direction of sending
*fewer* messages). 9 differ only in JSON key ordering.**

Also verified unchanged:

- **Framing on send** — both trees do `json.dumps(data).encode("ascii")` and
  write it raw. No trailing newline, no length prefix, no delimiter, in either.
- **Command names** — `const` values compared leaf by leaf: **zero changed, zero
  removed**; three names added (`FIELD_REASON`, `SENSOR_STATE_ON`,
  `SENSOR_STATE_OFF`), all new constants for existing literals.
- **Priorities** — `COMMAND_PRIORITIES` compared as a whole: identical. The diff
  in `const.py` is comment reflow only.
- **Keepalive PING/PONG** — both send `{"PING": "<round(time.time()*1000)>",
  "msgId": N, "dir": "p2d"}` and require an **exact string echo** in
  `msg["PONG"]`. HEAD adds a `self._last_ping is not None` guard and measures
  latency on `time.monotonic()` instead of re-parsing the wall-clock token; the
  **token on the wire is unchanged** and the echo comparison is still exact
  equality. A mismatched PONG is ignored by both.
- **Hold-time centiseconds** — `int(seconds * 100)` in both, verified over 12
  values including the float-representation traps (`1.005` → `100`, `3.999` →
  `399`, `655.35` → `65534` in both).
- **Notification envelope handling (client side)** — see A.5.

### A.2 `SET_NOTIFICATIONS`: booleans became strings — REGRESSION

`src/powerpetdoor/door.py:1146-1147` (HEAD), introduced in `21a463a` *(Wave 2
fixes from persona analysis round 1)*:

```python
# The wire protocol uses "1"/"0" strings (docs/protocol.md).
settings = {key: "1" if value else "0" for key, value in merged.items()}
```

| | baseline `2225e28` | HEAD |
|---|---|---|
| `set_notifications(inside_on=True, low_battery=True)` | `{"config":"SET_NOTIFICATIONS","msgId":34,"dir":"p2d","sensorOnIndoorNotificationsEnabled":true, ... ,"lowBatteryNotificationsEnabled":true}` | `... "sensorOnIndoorNotificationsEnabled":"1", ... "lowBatteryNotificationsEnabled":"1"` |
| Type of every one of the 5 flags | `bool` | `str` |

This is the same shape as the reverted round-5 change: a wire value flipped from
a JSON boolean to a `"1"`/`"0"` string on the authority of `docs/protocol.md`.
The differences from the round-5 case, and why this one still needs a decision:

- **It was not reverted.** Rounds 6–10 and the `f347321` cleanup all left it in.
- **It is not in the CHANGELOG.** The one real client→device wire change in 31
  commits is undocumented.
- **`PowerPetDoor.set_notifications` shipped in `v0.3.0`** (introduced in
  `8bc69d8`, and `v0.3.0` is a tag). Booleans are what that release sent.
- **Nothing in the tree requires the change.** HEAD's own simulator accepts
  *both* spellings — verified: a flat `true` and a flat `"1"` both produce
  `success:"true"` and the same stored state, because `_coerce_wire_flag()` goes
  through `make_bool`. The change buys nothing against our own test double.
- **Weaker evidence than the schedule case.** Unlike schedule `enabled` (which
  `docs/protocol.md` itself now marks as client→device JSON boolean "what has run
  against real hardware since v0.1.0"), `set_notifications` is a v0.3.0 facade
  method; I found no evidence it has ever run against a real door via
  `ha-powerpetdoor`, which drives `PowerPetDoorClient.send_message` directly and
  chooses its own spelling. So this is genuinely undetermined — but the
  *baseline's* spelling is the one with more standing, and HEAD changed it.

**Recommendation: revert to JSON booleans before pushing**, on the same
principle already applied to schedule `enabled` — the doc is not authority, and
the baseline is the incumbent. If the string form is wanted, add it to the
`ScheduleWireFormat`-style boundary as an explicit, documented, per-direction
decision rather than an inline dict comprehension citing the doc.

### A.3 `schedule_template` / `compress_schedule`: JSON key order changed

| | key order |
|---|---|
| baseline | `index, daysOfWeek, inside, outside, enabled, in_start_time, in_end_time, out_start_time, out_end_time` |
| HEAD | `index, enabled, daysOfWeek, inside, outside, in_start_time, in_end_time, out_start_time, out_end_time` |

All **member names, values and types are identical** (`enabled` is JSON `true` in
both; `daysOfWeek` is a list of ints `0`/`1` in both). `compress_schedule()`
output is a deep copy of the template in the baseline and a
`build_schedule_payload()` result at HEAD, so it inherits the same reordering in
8 of 9 cases.

`Schedule.to_dict()` — the ordinary `SET_SCHEDULE` path — is **unchanged in key
order as well as value**, and HEAD's new order is in fact the same as
`Schedule.to_dict()`'s always was. So the two SET_SCHEDULE producers now agree
with each other where previously they did not.

**Risk: LOW.** JSON object member order is not semantic, and the firmware
already had to accept both orders (it received `Schedule.to_dict()` in one order
and `compress_schedule()` output in the other, from v0.1.0 onward). Noted for
completeness because the emitted bytes are not identical.

### A.4 `compute_schedule_diff` on string day flags

One case changed behaviour: current `daysOfWeek: ["1",...]` vs new
`daysOfWeek: [1,...]`.

- baseline: content keys differ (`"1" != 1`) → emits **one `SET_SCHEDULE`**.
- HEAD: both coerce to `[True]*7` → recognised as unchanged → emits **nothing**.

This is the documented fix (`schedule_entry_content_key` now reads every flag
through `make_bool`). It sends *strictly fewer* messages to a rate-limited,
single-connection device, and only in the case where the firmware uses string day
flags. **Not a regression.** All other diff cases produce identical `set` and
`delete` lists.

### A.5 Simulator emissions (what a client must accept)

Every command the simulator handles was driven end-to-end and its response bytes
captured.

| Response | Result |
|---|---|
| All 16 `GET_*` responses (settings, door status, sensors, power, auto, safety lock, cmd lockout, autoretract, hw info, battery, stats, notifications, timezone, hold time, both trigger voltages) | **IDENTICAL** |
| All 14 enable/disable responses | **IDENTICAL** |
| `GET_SCHEDULE`, `GET_SCHEDULE_LIST`, `SET_SCHEDULE` (valid payload) | **IDENTICAL** |
| `HAS_REMOTE_ID`, `HAS_REMOTE_KEY`, `CHECK_RESET_REASON` | **IDENTICAL** |
| `PING` → `PONG` envelope | **IDENTICAL** |
| Unsolicited `DOOR_STATUS` broadcast | **IDENTICAL** |
| `OPEN`/`CLOSE` under power-off and command lockout (error envelope) | **IDENTICAL** |
| `DELETE_SCHEDULE` success | **DIFFERS** — HEAD adds `"index": N` (the real device echoes it). Additive; the baseline client ignores it, HEAD's client uses it. |
| Unknown command | **DIFFERS** — baseline answered `success:"true"`; HEAD answers `success:"false", reason:"Unknown command"`. Simulator-fidelity fix. |
| `SET_SCHEDULE_LIST` with no `schedules` field | **DIFFERS** — baseline wiped every schedule and said `success:"true"`; HEAD rejects. Simulator-fidelity fix. |
| **Sensor notification event** | **DIFFERS** — baseline `{"CMD":"SENSOR_INDOOR","sensorState":"on","success":"true","dir":"d2p"}`; HEAD `{"SENSOR_INDOOR":"","sensorState":"on"}` (the bare envelope `docs/protocol.md` describes). **HEAD's client accepts both forms**; the baseline client accepts neither usefully (it raises `KeyError: 'CMD'` on the bare form). Only the test double changed. |
| Notification gating: sensor under command lockout, outside sensor under safety lock, sensor with power off, sensor with notifications disabled | **IDENTICAL** — both trees emit **nothing** in all four cases. See Open Questions. |

### A.6 Newly-rejected inputs on the ACCEPT side

First, the direction that got **looser**. HEAD tolerates 21 response envelopes
that made the baseline raise out of `process_message` (`KeyError: 'CMD'`,
`KeyError: 'success'`, `TypeError: unhashable type`, `TypeError: argument of type
'int' is not iterable`, …): missing `CMD`, missing `success`, non-string `CMD`,
list `msgID`, top-level non-object, missing payload field on 8 different
handlers, scalar `settings`/`fwInfo`, partial `notifications`, partial
`settings`, bare notification envelopes. **Zero response envelopes that the
baseline handled are rejected by HEAD.** Framing is also strictly looser: HEAD
resyncs past leading/trailing garbage, whitespace and NULs where the baseline
raised `IndexError: Block does not start with '{'` and wedged its buffer forever,
and HEAD frames `{"a":"}"}` correctly where the baseline truncated it.

The narrowing is confined to **schedule payload parsing**, and it is real.

#### Client-side — `powerpetdoor.door.Schedule.from_dict` (reached from `door.get_schedule()` and `door.refresh_schedules()`)

20 shapes the baseline accepted now raise `ValueError`. Measured impact:
`get_schedule(i)` **raises** where the baseline returned a value;
`refresh_schedules()` **silently drops** the offending entry and logs (verified:
baseline returned 3 schedules, HEAD returned 1, from identical wire traffic).

| # | Rejected input | Baseline behaviour | Could real firmware send it? | Risk |
|---|---|---|---|---|
| 1 | `in_end_time: {"hour": 24, ...}` (hour > 23) | stored `hour=24` | **Plausibly yes.** `24:00` is a natural encoding for "end of day"/midnight, and it is exactly the shape a firmware would use to avoid the 23:59 gap that `f347321` documents this project itself hitting. Nothing has confirmed the device does *not* use it. | **MEDIUM** |
| 2 | `in_start_time`/`in_end_time` absent while `inside: true` (same for `out_*`/`outside`) | defaulted to `{hour:0,min:0}` | **Plausibly yes** for an unconfigured/empty slot, if the firmware lists such slots in `GET_SCHEDULE_LIST`. Less likely for a slot the device considers active. | **MEDIUM** |
| 3 | time block present but with no `hour` key (`{}` or `{"min": 5}`) | defaulted `hour=0` | Unlikely — the observed and documented shape always carries `hour`. | LOW |
| 4 | `min` > 59 | stored as-is | Unlikely. | LOW |
| 5 | `hour` < 0 | stored as-is | Very unlikely. | LOW |
| 6 | `index` > 255 | stored as-is | Very unlikely — no plausible firmware has >256 schedule slots. | LOW |
| 7 | `index` < 0 | stored as-is | Very unlikely. | LOW |
| 8 | `index` absent-as-`null` | stored `None` | Unlikely; and `None` was unusable anyway (it cannot address `SET_SCHEDULE`). | LOW |
| 9 | `index` non-numeric (list) | stored the list | No. | NEGLIGIBLE |
| 10 | `index` = `1e400`/`inf` | stored `inf` | No. | NEGLIGIBLE |
| 11 | `daysOfWeek` list of length ≠ 7 | stored the short/long list | Very unlikely — the protocol fixes 7. A 6-element list would have crashed `is_day_active` later anyway. | LOW |
| 12 | `daysOfWeek` integer bitmask outside `0..127` | `(mask >> i) & 1`, so any negative mask meant "every day on" | Unlikely, and the baseline's handling was fail-**open** on a security-relevant field. | LOW *(rejecting is safer)* |
| 13 | `daysOfWeek` as a string (`"1111111"`) | iterated the characters → 7 `True` | Unlikely. | LOW |
| 14 | a `daysOfWeek` element that is not a recognisable 0/1 flag (`null`, `[1]`, `"maybe"`) | truthiness (so `null`→False, `[1]`→True) | Unlikely. | LOW |
| 15–20 | The `hour`/`min`/`index` variants above applied to `out_*` and to the end block | as above | as above | as above |

Two further **behaviour** changes here that are not rejections but are worth
recording, because both are silent and both flip a boolean:

- `inside`/`outside`/`enabled` are now read through `make_bool`. A device
  replying `inside: "0"` was read as **True** by the baseline (`bool("0")` is
  True) and is read as **False** by HEAD. Same for `daysOfWeek: ["0",...]`:
  baseline all-True, HEAD all-False. HEAD is correct and fails closed; the
  baseline failed open. **Not a regression**, but it changes what a
  string-flag-emitting firmware would produce.
- `enabled` with an unrecognisable value (`null`, `[1]`, `"yes-ish"`) now
  fails **closed** to `False` instead of being stored raw. Read-only cache
  impact, plus it feeds `compute_schedule_diff`. LOW.
- `enabled: "true"` now reads **True** (baseline: `== "1"` → False). This is a
  loosening, in the correct direction.

#### Simulator-side (`SET_*` handlers and `simulator/state.py:Schedule.from_dict`)

21 further shapes are newly rejected here. **These govern what the simulator
accepts *from a client*, i.e. from us.** Our own client never emits any of them,
and they cannot affect a real door — they only affect the fidelity of the test
double. Verified in the interop runs: the head simulator accepted every payload
the base client sent, and vice versa.

| Rejected | Risk to real-hardware compatibility |
|---|---|
| `SET_HOLD_TIME` outside `0..90000` centiseconds, or non-numeric (incl. the string `"250"`) | **NONE** — we send `int(seconds*100)` |
| `SET_TIMEZONE` non-string or > 128 chars | **NONE** — we send a `str` |
| `SET_SENSOR_TRIGGER_VOLTAGE` / `..._SLEEP_...` outside `0..65535` or non-numeric | **NONE** — the facade has no setter for these |
| `SET_NOTIFICATIONS` field that is not a recognisable 0/1 flag (whole message rejected, never half-applied) | **NONE** — accepts `true`, `"1"`, `1` alike, so both the baseline's and HEAD's spelling pass |
| `SET_SCHEDULE_LIST` with `schedules` absent or non-list | **NONE** — the facade never sends `SET_SCHEDULE_LIST` |
| `GET_SCHEDULE` / `DELETE_SCHEDULE` with `index` not an int in `0..255` (the string `"0"` is now rejected) | **NONE** — we send `int` |
| `SET_SCHEDULE` with a payload failing the 20 rules above | **NONE** — `Schedule.to_dict()` always satisfies them |
| Unknown command answered `success:"false"` instead of `success:"true"` | **NONE** — improves fidelity |

**Count: 41 newly-rejected input shapes — 20 client-side (2 MEDIUM, 18 LOW or
below), 21 simulator-side (all NONE for hardware).**

---

## Part B: Deletion Safety

All of the following were executed against HEAD, not reasoned about.

| Check | Result |
|---|---|
| Every submodule imports (30 modules, `pkgutil.walk_packages` + `import_module`) | **PASS** — 0 failures |
| References to removed symbols (`EventThrottle`, `FrameDispatcher`, `MAX_WRITE_BACKLOG`, declined-transport counters, `generate_gaps_report`, `TESTING_GAPS`) across `src/`, `tests/`, `docs/`, `.github/`, `pyproject.toml`, `MANIFEST.in` | **PASS** — only CHANGELOG entries documenting the removal, plus one test *method name* (`test_a_declined_transport_forwards_nothing`) that exercises surviving behaviour |
| `scripts/` and `tests/TESTING_GAPS.md` referenced anywhere after removal | **PASS** — no references |
| Full suite | **PASS** — 2812 passed, 36 s |
| Coverage gate | **PASS** — 100.00% lines and branches, 6403/6403, 2264/2264 |
| `ruff check` / `ruff format --check` / `mypy src` | **PASS** — clean, 31 source files |

### Never raises out of `data_received` — both sides

15 hostile inputs, fed to **both** `PowerPetDoorClient.data_received` and
`DoorSimulatorProtocol.data_received`:

garbage bytes · brace-in-string · escaped-quote-then-brace · split frame
(halves fed separately) · all 128 non-ASCII bytes · non-ASCII inside a frame ·
malformed JSON · unterminated string · 100 NULs · 200-deep nesting ·
6000-digit integer literal · 70 KB single frame · 500 back-to-back frames ·
interleaved garbage between frames.

**Result: 0 of 30 raised. 0 dropped the link.** (The 6000-digit literal is the
one that made the baseline raise `ValueError` out of `data_received` on both
sides — HEAD catches it.)

### 64 KiB buffer cap

| | fires | disconnects | buffer cleared | reconnect scheduled |
|---|---|---|---|---|
| Client, `MAX_BUFFER_SIZE + 10` unterminated | **yes** | transport closed **and** released | **yes** (0 chars) | **yes** |
| Client, `MAX_BUFFER_SIZE - 100` unterminated | correctly does **not** fire | — | retains 65 437 chars | — |
| Simulator, `MAX_BUFFER_SIZE + 10` unterminated | **yes** | `transport.abort()` | **yes** (0 chars) | n/a |

`MAX_BUFFER_SIZE` is 65 536 and the cap is on the **un-parsed remainder**, so a
single *complete* 70 KB frame is still accepted in one read. That matches the
baseline (which had no cap at all) and is documented behaviour; noted, not a
defect.

### Live recovery over real sockets

A real simulator server, a real client, the payload injected from the server
side, then a genuine `GET_DOOR_STATUS` round trip afterwards:

| Injected | Link survived | Recovered and answered |
|---|---|---|
| garbage bytes | yes | **yes** |
| brace-in-string | yes | **yes** |
| non-ASCII bytes | yes | **yes** |
| malformed JSON | yes | **yes** |
| oversized (> 64 KiB unterminated) | dropped and reconnected within the backoff | **yes** |
| truncated frame (`{"CMD":"OP`) | yes | **no** — the stream is desynchronized until the 64 KiB cap fires |

The truncated-frame case is **identical in the baseline** (a genuinely truncated
object desynchronizes brace matching either way), except that the baseline never
recovers at all — HEAD's cap eventually resyncs it. Not a regression.

### Cross-version interop (four live runs, real TCP)

| simulator | client | outcome |
|---|---|---|
| HEAD | HEAD | 30 operations, all pass |
| **baseline** | **HEAD** | 30 operations, all pass — one benign difference (the baseline simulator silently ignores `SET_NOTIFICATIONS` entirely, so the cached notification state differs) |
| **HEAD** | **baseline** | 30 operations, all pass — identical to HEAD/HEAD apart from a 1 ms latency rounding |
| baseline | baseline | **fails** — the baseline `PowerPetDoor` builds a *private* event loop when `loop=None`, so the documented `PowerPetDoor(host)` + `await connect()` pattern raises "Future attached to a different loop". Fixed at HEAD. |

The HEAD client speaks to the baseline device emulation and the baseline client
speaks to the HEAD device emulation, both cleanly. That is the strongest
available evidence that HEAD's accept side is a superset and its emissions remain
acceptable.

**Conclusion: nothing load-bearing for correctness went with the ~4,900 removed
lines.**

---

## Part C: Shippability

**Safe to push: yes, after one change. Safe to point at real hardware: yes, after
that change, and with two schedule-parsing decisions consciously accepted.**

### Must be fixed before pushing

1. **Revert `PowerPetDoor.set_notifications()` to JSON booleans**
   (`src/powerpetdoor/door.py:1146-1147`). This is the only client→device wire
   value that changed, it changed on the authority of a document that this repo's
   own CHANGELOG and `docs/protocol.md` both state is *not* authority, it changed
   an API that shipped in `v0.3.0`, and HEAD's own simulator accepts the boolean
   form unchanged so nothing is gained. If it is kept instead, that must be a
   deliberate, recorded decision — not an inline comment citing the doc.

2. **Delete the stale CHANGELOG entry.** `CHANGELOG.md:109-111` still says:

   > `Schedule.to_dict()` and `schedule_template` emit `enabled` as the wire's
   > `"1"`/`"0"` string, matching `docs/protocol.md` and the simulator.

   **That is false at HEAD** — verified by execution, both emit JSON `true`. It
   is the round-5 change that round 6 reverted, and the entry was never removed.
   It directly contradicts the correct entry 35 lines above it
   (`CHANGELOG.md:72-82`). Left in, it is a standing invitation for the next
   maintainer to "fix" the code to match the changelog and reintroduce the exact
   regression this audit was commissioned to catch.

3. **Add the `SET_NOTIFICATIONS` decision to the CHANGELOG**, whichever way it
   goes. Right now the one real wire change in 31 commits is undocumented while a
   reverted one is documented.

### Should be decided, not necessarily blocking

4. **`hour: 24` in a schedule time block.** `coerce_schedule_int(..., 23)`
   rejects it; the baseline stored it. If the door ever encodes an end-of-day
   window that way, `get_schedule()` raises and `refresh_schedules()` silently
   drops the entry. Cheapest hedge: allow `hour == 24` on **end** blocks only
   (`in_end_time`/`out_end_time`), or clamp-with-warning instead of raising.

5. **Absent time blocks on a selected sensor.** `require_schedule_field` rejects;
   the baseline defaulted to `00:00`. Consider degrading to a warning +
   `00:00–00:00` on the *read* path (`Schedule.from_dict` in `door.py`) while
   keeping the strict rejection on the simulator's *write* path, where it is
   genuinely protecting stored state.

6. **`refresh_schedules()` drops rejected entries silently** (a WARNING in the
   log, nothing in the return value). A caller sees a shorter list with no way to
   tell "the door has 1 schedule" from "the door has 3 and we could not parse 2".
   Consider surfacing the count.

### Already verified good

- 2812 tests pass; 100.00% line and branch coverage; `ruff` and `mypy` clean.
- Working tree clean; 31 commits on top of `origin/main`; nothing pushed.
- The `async-timeout` dependency drop is real and correct (`asyncio.timeout` is
  stdlib from 3.11, which is `requires-python`).
- HEAD's `PowerPetDoor` now performs a `refresh()` after an **auto-reconnect**
  (the baseline sent nothing). Five extra `GET_*` messages per reconnect, all
  with byte-identical payloads, rate-limited by `MINIMUM_TIME_BETWEEN_MSGS`.
  Behaviourally desirable; recorded because it is new traffic.

---

## Open Questions For The Device

These cannot be settled without a real Power Pet Door. Each is listed with the
cheapest experiment that settles it.

1. **What does the firmware accept for schedule `enabled` in `SET_SCHEDULE`?**
   We send JSON `true`/`false`; `docs/protocol.md` describes `"1"`/`"0"`. The
   library has sent the boolean since v0.1.0 and HEAD still does — **no drift**.
   *Experiment:* `set_schedule(...)` with `enabled=True`, then `get_schedule()`
   and confirm the device echoes an enabled entry. Then repeat against a build
   sending `"1"`. If both work, record that and stop re-litigating it.

2. **What does the firmware accept for `SET_NOTIFICATIONS` flags?** This is the
   one place HEAD changed the wire (booleans → `"1"`/`"0"`). *Experiment:*
   `set_notifications(inside_on=True)` then `GET_NOTIFICATIONS`; confirm the
   change took. Repeat with the other spelling. Until then, ship the baseline's
   booleans — see Part C item 1.

3. **Does a sensor open the door while command lockout is enabled?** Undocumented.
   Both trees block it identically in the simulator engine — **no drift from this
   work**. *Experiment:* enable `allowCmdLockout`, wave a hand at the inside
   sensor, watch the door.

4. **Does a safety-locked sensor still emit notifications?**
   `docs/operation.md:147` says a safety-locked sensor "Still sends
   notifications", but **both** the baseline and HEAD emit nothing — verified by
   execution, identical in both, so this is a pre-existing simulator gap and
   **not drift from this work**. *Experiment:* enable
   `outsideSensorSafetyLock`, enable outdoor notifications, trigger the outside
   sensor, and watch for a `SENSOR_OUTDOOR` frame. If one arrives, the simulator
   engine's suppression is wrong and `docs/operation.md` is right.

5. **Which notification envelope does the device actually use?** The baseline
   simulator emitted `{"CMD":"SENSOR_INDOOR","sensorState":"on","success":"true",
   "dir":"d2p"}`; HEAD emits the bare `{"SENSOR_INDOOR":"","sensorState":"on"}`
   that `docs/protocol.md` describes. HEAD's client accepts **both**, so nothing
   depends on the answer — but the simulator should be made to match once it is
   known. *Experiment:* connect, trigger a sensor, capture the frame.

6. **Does any schedule time block ever carry `hour: 24`, and does the device
   ever omit a selected sensor's window?** These are the two MEDIUM-risk
   rejections in A.6. *Experiment:* `GET_SCHEDULE` every slot on a door with a
   real, user-configured schedule (including an all-day one) and dump the raw
   frames before parsing.

---

*Audit performed against `2225e28` (origin/main) and `fdfe626` (HEAD), on Python
3.13.13. All differential harnesses, both isolated source trees and the live
interop rigs were built under `/tmp/wireaudit/` and removed afterwards; the
repository was read-only apart from this file.*
