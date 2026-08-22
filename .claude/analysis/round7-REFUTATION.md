# Round 7 Refutation Pass

Adversarial re-verification of all 27 round-7 findings at commit `4958f2b`
(source tree identical to `a0194bd` for `src/`, `tests/`, `docs/`).

**Method.** Every finding was re-derived from scratch. I did not run any script
from any round-7 report and did not reuse any of their transcripts; I wrote my
own harnesses, my own mutations and my own measurements, and every number below
is output I produced on this machine. Work happened in `/tmp/refute/*` on a
`git archive HEAD` copy with `PYTHONPATH` forced at the copy's `src/` and an
import guard asserting `powerpetdoor.__file__.startswith("/tmp/refute/...")`
before pytest started, so no result can be an artifact of the editable install.
No repository file was modified (`git status` clean throughout); every daemon I
started was terminated; every scratch file lives under `/tmp` and was removed.

Baseline on the `/tmp` copy: `./run.sh -q -p no:randomly` → **2454 passed in
37.78 s**, guard line `[guard] /tmp/refute/base/src/powerpetdoor/__init__.py`.

**Mutation discipline.** 26 mutations were run, each on its own fresh copy of the
tree, each as a full suite run, and each batch carried controls that MUST be
caught. **All four controls were caught** (`MAX_INFLIGHT_FRAMES 64→4096`,
`MAX_RETAINED_PIECES 64→32`, `cli.py:517 script_delay > 0 → >= 0`,
`commands/base.py:165 int max_value > → >=`). **All 22 non-control mutations
produced exactly the outcome the test-fanatic report claimed.** That is a
22/22 + 4/4 agreement rate on independently re-derived mutants, which is the
single strongest piece of evidence in this pass that round 7 was not inventing.

**Headline result:** 23 CONFIRMED, 3 CONFIRMED-BUT-OVERSTATED, 1 REFUTED. This
is a much higher survival rate than a refutation pass usually produces, and I
want to be explicit about why rather than have it read as rubber-stamping: the
round-7 reports restricted themselves to execution-proven claims, and it shows.
Where I disagree it is about **severity calibration inside the test-fanatic
report**, not about whether the thing is real.

---

## Summary

| ID | Persona | Claimed | Verdict | Adjusted |
|---|---|---|---|---|
| B-M1 battery cached with no coercion | backend | Medium | **CONFIRMED** | Medium |
| B-L1 3 more facade listeners cache verbatim | backend | Low | **CONFIRMED** | Low |
| F-M1 `holdtime nan` corrupts after reporting ERROR | frontend | Medium | **CONFIRMED** | Medium |
| F-M2 unknown `sensor:` bypasses the gates | frontend | Medium | **CONFIRMED** | Medium |
| F-L1 Conditions table claimed to apply to `assert` | frontend | Low | **CONFIRMED** | Low |
| F-L2 first doc script example fails | frontend | Low | **CONFIRMED** | Low |
| F-L3 unknown step parameter silently ignored | frontend | Low | **CONFIRMED** | Low |
| F-L4 `inside`/`outside`/`door_closing` undocumented | frontend | Low | **CONFIRMED** | Low |
| F-L5 `--list-scripts` omits the shadow marker | frontend | Low | **CONFIRMED** | Low |
| F-L6 symlink refusal advises a form ctl rejects | frontend | Low | **CONFIRMED** | Low |
| F-T1 `protocol.md` claims `msgID` echoed on PONG | frontend | Trivial | **CONFIRMED** | Trivial |
| F-T2 three spellings of the schedule sensor scope | frontend | Trivial | **CONFIRMED** | Trivial |
| S-M1 admission unbounded per read; callback never yields | security | Medium | **CONFIRMED** | Medium |
| S-L2 write ceiling logs per message and never drops | security | Low | **CONFIRMED** | Low |
| S-L3 four unthrottled, uncapped per-frame log sites | security | Low | **CONFIRMED** | Low |
| S-I4 `reset()` clears `_paused` without resuming | security | Informational | **REFUTED** | — (no action) |
| T-M1 `timeout = 60` does not cover the fuzz suite | test | Medium | **CONFIRMED** | Medium |
| T-M2 `FrameScanner.buffer` can return truncated | test | Medium | **OVERSTATED** | Low |
| T-M3 `test_flush_restarts_the_quiet_period` unfalsifiable | test | Medium | **OVERSTATED** | Low |
| T-M4 pragma claims untestable; a test triggers it | test | Medium | **OVERSTATED** | Low |
| T-M5 comparisons never exercised at the boundary | test | Medium | **CONFIRMED** | Medium |
| T-L1 shipped resource bounds have no value pin | test | Low | **CONFIRMED** | Low |
| T-L2 `CONTROL_PORT_OFFSET` is dead code | test | Low | **CONFIRMED** | Low |
| T-L3 `find_iana_for_posix` first-match-wins untested | test | Low | **CONFIRMED** | Low |
| T-L4 the second `script_delay > 0` guard unpinned | test | Low | **CONFIRMED** | Low |
| T-L5 "may be absent" guards never seen with field absent | test | Low | **CONFIRMED** | Low |
| T-L6 `STATUS: clients=0` "no change" unasserted | test | Low | **CONFIRMED** | Low |

Counts: **CONFIRMED 23 · CONFIRMED-BUT-OVERSTATED 3 · REFUTED 1.**

---

## Verdicts

### B-M1 — `_on_battery_update` caches device values with no coercion — **CONFIRMED (Medium)**

**What I ran.** `/tmp/refute/probes/p_battery.py`: my own `asyncio.start_server`
device answering the real refresh sequence over a real TCP socket, a real
`PowerPetDoor` doing a real `connect()`, a `logging.Handler` at WARNING attached
to the `powerpetdoor` logger, and a healthy control case in the same run.

```
=== string percent: device reply {'batteryPercent': '55', 'batteryPresent': '1', 'acPresent': '1'}
  after  refresh: BatteryInfo(percent='55', present=True, ac_present=True)
  door.battery_percent -> '55' (str)
  door.battery.charging -> RAISED TypeError: '<' not supported between instances of 'str' and 'int'
  WARNING+ log records: []

=== percent absent: device reply {'batteryPresent': '1', 'acPresent': '1'}
  after  refresh: BatteryInfo(percent=None, present=True, ac_present=True)
  door.battery_percent -> None (NoneType)
  door.battery.charging -> RAISED TypeError: '<' not supported between instances of 'NoneType' and 'int'
  WARNING+ log records: []

=== acPresent unrecognized: device reply {'batteryPercent': 40, ..., 'acPresent': 'maybe'}
  after  refresh: BatteryInfo(percent=40, present=True, ac_present=None)
  door.ac_present -> None (NoneType)     door.battery.charging -> None

=== healthy control: {'batteryPercent': 40, 'batteryPresent': '1', 'acPresent': '1'}
  door.battery.charging -> True          WARNING+ log records: []
```

**Attack on the claim.** The trigger is a real, supported path: `door.connect()`
runs `refresh()` which issues `GET_DOOR_BATTERY`. No private call, no
monkeypatch. The "dead fallback" half is a plain code defect independent of any
firmware question — `_handle_battery` builds all three keys unconditionally, so
`data.get(K, self._battery.percent)` can never take its default and the cache is
overwritten with `None` instead of preserved. The `acPresent: "maybe"` row is
strictly stronger than the report needed: `make_bool` is *documented* to return
`None` for unrecognized strings (`client.py:244-248`), so this listener caching a
`None` into a field declared `bool` is reachable through the library's own
designed behaviour, not only through exotic firmware.

Fix location is correct and safe: it belongs at `door.py` (layer 1). It does not
change any byte we send, and it does not narrow what the client accepts — the
client's liberal dict is still what `send_message(..., notify=True)` resolves
with. Medium is fair: a documented public property raises, silently, and the
value flows into a Home Assistant sensor.

### B-L1 — three more facade listeners cache verbatim — **CONFIRMED (Low)**

**What I ran.** `/tmp/refute/probes/p_facade.py` (same real-socket harness, one
case per listener) plus `/tmp/refute/probes/nulldbg.py` for the null case in
isolation.

```
### control: healthy      total_open_cycles=5 (int)  total_auto_retracts=1 (int)  timezone='' (str)
### stats: strings        total_open_cycles='5' (str)  <-- declared int
                          total_auto_retracts='7' (str)  <-- declared int      WARNING+ logged: 0
### settings tz is an int timezone=5 (int)  <-- declared str                    WARNING+ logged: 0
### hw_info scalar (R6 M1 control)
   firmware_version='' (str) hardware_version='' (str) hardware_info={} (dict)
   WARNING+ logged: 2 ['Device sent a non-mapping fwInfo payload; ...', 'Ignoring non-mapping hardware info: 1.2.3']
```

The null case, isolated:

```
stats frames the device actually sent: ['{"CMD": "GET_DOOR_OPEN_STATS", ..., "totalOpenCycles": null, "totalAutoRetracts": null}']
door.total_open_cycles = None      door.total_auto_retracts = None
send_message result: {'totalOpenCycles': None, 'totalAutoRetracts': None}
```

(My first pass at this case reported `0`; that was a bug in my batching harness,
and the isolated run above is the correct result. Flagging it because I would
rather record my own error than quietly drop it.)

The `hw_info` block is the control and it behaves exactly as the round-6 M1 fix
intended, which makes the contrast the report drew a real one. Low is right —
nothing in this library does arithmetic on these values, so the damage stops at
the API boundary.

### F-M1 — `holdtime nan` reports ERROR *after* corrupting the simulator — **CONFIRMED (Medium)**

**What I ran.** Fresh daemon on 39510/39511, driven with real `ppd-simulator-ctl`
invocations, plus a raw socket against the door port.

```
--- 1 healthy          OK: Hold time: 2.0s                      rc=0
--- 2 holdtime nan     ERROR: cannot convert float NaN to integer   rc=1
--- 3 after            OK: Hold time: nans                      rc=0
```

Raw socket to the door port while `hold_time` is `nan`:

```
GET_SETTINGS    -> {"CMD": "GET_SETTINGS",  "success": "false", "reason": "Command failed", "msgID": 1}
GET_HOLD_TIME   -> {"CMD": "GET_HOLD_TIME", "success": "false", "reason": "Command failed", "msgID": 2}
GET_DOOR_STATUS -> {"CMD": "GET_DOOR_STATUS","success": "true",  "door_status": "DOOR_HOLDING"}
```

The wedge:

```
t=+2s/+4s/+6s   Door: DOOR_HOLDING
holdtime 2      OK: Hold time set to 2.0s
after restore   Door: DOOR_HOLDING   (still)
close           OK: Closing door  ->  Door: DOOR_HOLDING
```

Blast radius across the other `"float"` ArgSpecs, all `rc=0`:

```
inside nan -> OK: Inside sensor activated for nans      inside 1e400 -> OK: ... for infs
charge_rate nan -> OK: Charge rate: nan%/min            discharge_rate inf -> OK: Discharge rate: inf%/min
```

**Attack on the claim.** The two other front ends onto the same state really do
reject it, which is what makes this an inconsistency rather than a request for
new validation — I verified both myself:

```
C->D {"config":"SET_HOLD_TIME","holdTime":NaN,...}      D->C reason: "holdTime must be a finite number, got nan"
C->D {"config":"SET_HOLD_TIME","holdTime":Infinity,...} D->C reason: "holdTime must be a finite number, got inf"
C->D {"config":"GET_HOLD_TIME",...}                     D->C holdTime: 200   (state untouched)
script: set hold_time .nan  -> [ERROR] hold_time must be a finite number, got nan  >>> Script FAILED
```

**Wire check.** The proposed fix is confined to `simulator/commands/base.py`'s
`parse_arg`, which parses **operator keystrokes**, not wire fields. It changes
nothing we send and narrows nothing we accept from a peer. Passes.

One correction to the recommendation: the `"int"` half is already safe —
`int("nan")` raises `ValueError` and is caught. Only the `"float"` branch needs
the `math.isfinite` guard.

Medium is fair: a command that prints `ERROR` has already mutated state, the
damage outlives the command, and a core query answers every client
`success:false / reason:"Command failed"`.

### F-M2 — unknown `sensor:` synthesises a third sensor that bypasses the gates — **CONFIRMED (Medium)**

**What I ran.** Three scripts through `ppd-simulator --port 0 --scripts-dir ...
--script <name> --oneshot`: the typo, a control using the real sensor name under
identical state, and a control with a misspelled *action*.

```
=== typo_sensor  rc=0
  Step 3: set(name=safety_lock, value=1)
  Step 4: trigger_sensor(sensor=insde)
  Simulator: Insde sensor triggered, opening door
  Step 5: assert(condition=door_status, equals=DOOR_RISING)
  Step 6: log(message=DOOR OPENED WITH BOTH SENSORS DISABLED AND SAFETY LOCK ON)
  >>> Script PASSED: Typo Sensor Bypass

=== good_sensor  rc=0   (same state, real sensor name)
  Step 5: assert(condition=door_status, equals=DOOR_CLOSED)   <-- correctly gated
  >>> Script PASSED: Control Real Sensor

=== badaction    rc=1
  [ERROR] Script error at step 1: Unknown action: frobnicate
```

**Attack on the claim.** The control pair is decisive: identical state, identical
step, one character different, and the gates apply in one case and not the other.
A misspelled *action* fails loudly; a misspelled *sensor* passes silently and the
log capitalises the typo into something that reads like the legitimate line.
Over `ctl run <name> wait` that PASSED becomes a green CI exit code, which is the
one thing a simulator must never produce for a test that exercised nothing.
Neither half of the fix touches the wire. Medium stands.

### F-L1 — the Conditions table is documented as applying to `assert` — **CONFIRMED (Low)**

I generated one script per condition and ran each. 8 of 8 sampled rows fail;
`_assert_condition` (`scripting.py:724+`) accepts a disjoint set of 12 names.

```
door_closed      -> rc=1 Unknown assertion condition: door_closed
door_open        -> rc=1 Unknown assertion condition: door_open
door_holding     -> rc=1 Unknown assertion condition: door_holding
power_on         -> rc=1 Unknown assertion condition: power_on
auto_on          -> rc=1 Unknown assertion condition: auto_on
inside_enabled   -> rc=1 Unknown assertion condition: inside_enabled
safety_lock_on   -> rc=1 Unknown assertion condition: safety_lock_on
cmd_lockout_off  -> rc=1 Unknown assertion condition: cmd_lockout_off
control: wait_for door_closed -> rc=0 >>> Script PASSED
```

`docs/simulator.md:746` reads "Conditions are used with `wait_for` and `assert`
actions"; the follow-on table is introduced with "For `assert`, you can **also**
check these values", where "also" is wrong — it replaces rather than extends.
Docs-only fix. Low stands.

### F-L2 — the first doc script example fails when run — **CONFIRMED (Low)**

The `### Script Format` block copied verbatim, three runs:

```
run 1 rc=1 : Assertion failed at step 3: door_status: expected 'DOOR_CLOSED', got 'DOOR_HOLDING'  >>> Script FAILED
run 2 rc=1 : (identical)
run 3 rc=1 : (identical)
```

Deterministic, not a timing flake: default `hold_time` is 2.0 s and the hold
timer only starts once the door reaches `DOOR_HOLDING`, so `wait 2` is never
enough. The in-repo built-ins already show the correct shape. Docs-only. Low.

### F-L3 — an unknown step parameter is silently ignored and echoed back as accepted — **CONFIRMED (Low)**

```
typo_wait  (duration: 8)  rc=0 elapsed=1.19s  Step 1: wait(duration=8)  >>> Script PASSED
good_wait  (seconds: 8)   rc=0 elapsed=8.18s  Step 1: wait(seconds=8)   >>> Script PASSED
```

The 8× timing difference with an identical PASSED verdict is the substance, and
`duration` is a real parameter name elsewhere in this same DSL, so it is a
plausible slip rather than a contrived one. The step log rendering `step.params`
verbatim is what turns a silent no-op into an actively misleading one. Low.

### F-L4 — `inside`/`outside` actions and `door_closing` are implemented and undocumented — **CONFIRMED (Low)**

```
implemented: add_schedule assert battery close inside log obstruction open outside
             pet_off pet_on pet_presence remove_schedule set toggle trigger trigger_sensor wait wait_for
documented:  add_schedule assert battery close log obstruction open pet_off pet_presence
             remove_schedule set stderr toggle trigger_sensor wait wait_for

insideact    rc=0  Step 1: inside(duration=1.5)          >>> Script PASSED
waitclosing  rc=0  wait_for door_closing (timeout 15)    >>> Script PASSED
door_closing: scripting.py:124, scripting.py:587 — absent from docs/simulator.md
```

The aggravating detail checks out: `docs/simulator.md:683-684` already *depends*
on the undocumented actions ("Every delay a script can ask for —
`inside`/`outside` `duration`, `wait` `seconds` …"), so a reader who reaches that
note has no entry to look up. Docs-only. Low (arguably Trivial; I keep Low
because the docs already reference the missing entries).

### F-L5 — `--list-scripts` omits the shadow marker that `list` prints — **CONFIRMED (Low)**

Same daemon, same `--scripts-dir`, a `basic_cycle.yaml` shadowing the built-in:

```
ctl list           basic_cycle: Pet triggers inside sensor, ... (shadowed by /tmp/refute/sd/basic_cycle)
--list-scripts     basic_cycle: Pet triggers inside sensor, door opens, holds, then closes   <- no marker
run basic_cycle    OK: Script PASSED: SHADOWING basic_cycle    (precedence is real)
```

Both surfaces print the name twice with contradicting descriptions; only one says
which one `run` picks. Low.

### F-L6 — the symlink refusal advises a form the control channel rejects — **CONFIRMED (Low)**

```
run linked                    ERROR: ... cannot be run by name; move it into the directory or run it by path
run /tmp/refute/sd/linked.yaml ERROR: Script paths are not allowed over the control channel; use a bare script name
run ./linked.yaml              ERROR: Script paths are not allowed over the control channel; use a bare script name
```

Confirmed: over ctl the two refusals point at each other. Message-only fix, and
`describe_script_argument()` already establishes the pattern for making a string
policy-aware. Low.

### F-T1 — `protocol.md` says `msgId` is echoed as `msgID` on PONG — **CONFIRMED (Trivial)**

```
C->D: {"PING": "1710000000123", "msgId": 1, "dir": "p2d"}
D->C: {"CMD": "PONG", "PONG": "1710000000123", "success": "true", "dir": "d2p"}   (no msgID)
```

`simulator/protocol.py:520-529` returns before the `msgID` copy at :538, and
`client.py:1074` matches on the echoed token, so nothing in the project uses
`msgID` for PONG. **The fix is a doc reword only** — it does not propose changing
what the simulator sends, which is the correct side to move given that
`docs/protocol.md` is reverse-engineered. Trivial stands.

### F-T2 — three spellings of the schedule sensor scope — **CONFIRMED (Trivial)**

```
schedule add both 6:00-22:00 all -> OK: Added schedule #0: both sensor, all days, 06:00-22:00
schedule list                    ->   #0: inside+outside sensor, all days, 06:00-22:00 (enabled)
schedule clear; schedule list    ->   (implicit): both sensors, all days, 00:00-23:59
```

Cosmetic, and `both sensor` is ungrammatical. Trivial.

### S-M1 — the dispatch bound is on tasks, not on work admitted per read — **CONFIRMED (Medium)**

**Reproduction A (in-process, shipped code, `tracemalloc`).** One asyncio-sized
256 KiB read:

```
MAX_INFLIGHT_FRAMES = 64      MAX_FRAME_BACKLOG = 256
b'{}'  bytes=262144 callback_blocked= 410.5 ms backlog=131008 inflight=64 paused=True pause_reading x1 tracemalloc_peak=8.20 MB
       drained in 0.673s -> backlog=0 inflight=0 paused=False resume x1
b'{x}' bytes=262143 callback_blocked=1410.1 ms backlog=     0 inflight= 0 paused=False pause_reading x0
b'{,}' bytes=262143 callback_blocked=1414.7 ms backlog=     0 inflight= 0 paused=False pause_reading x0
```

**Reproduction B (clean timing, logging disabled, `gc.collect()` per trial, 5
trials, fresh protocol each time).**

```
SIM   b'{}'    one 256KiB read: min  61.9 ms  median  65.2 ms  max  68.2 ms  backlog left 131008
SIM   b'{x}'   one 256KiB read: min 254.1 ms  median 254.6 ms  max 257.2 ms  backlog left 0
SIM   b'{"a"}' one 256KiB read: min 159.6 ms  median 161.0 ms  max 161.6 ms  backlog left 0
CLIENT b'{}'   one 256KiB read: 66.2 ms  inflight=64 backlog=131008 paused=True pause_reading x1
```

**Reproduction C (real `ppd-simulator --daemon`, 64 sockets, one 256 KiB burst
each, then silence).**

```
baseline: daemon RSS 34.5 MB, ctl status 136 ms
attacker sent 16.78 MB over 64 conns in 0.01s, then silent
peak daemon RSS 273.7 MB (delta +239.2 MB) = 14.3x the bytes sent
ctl status: max 2188 ms, samples over 500ms: 2 (last at t+29.2s)
settled RSS 217.7 MB, ctl status 231 ms
after attacker closes: RSS 212.5 MB
```

**Attack on the claim.** The core defect reproduces exactly and independently:
`MAX_FRAME_BACKLOG = 256` is advisory (131 008 admitted, 512×), the `{}` case
holds 8.2 MB of `tracemalloc` for 256 KiB on the wire, `data_received` does not
yield for 62–254 ms, and the unparseable-frame path (`{x}`) drains the whole read
synchronously so the pause threshold never engages at all. The shipped client has
the identical shape. `+239 MB` for a 16.78 MB one-shot burst is if anything worse
than the report's `+228 MB`, and RSS had not returned even after the attacker
closed.

**One number did not reproduce at the claimed magnitude.** The report's headline
"blocks `ctl status` for 14.9 s"; I measured a max of **2 188 ms** with only 2
samples over 500 ms. Still a 16× degradation over the 136 ms baseline, and still
a real control-plane impact, but the most dramatic figure in that report is
roughly 7× larger than what I could reproduce. I would not quote it.

**Wire check on the recommendation.** Recommendation **1** (make `_pump()`
dispatch at most `max_inflight` per invocation and re-arm with
`loop.call_soon`) neither changes what we send nor narrows what we accept — it
only spreads existing work across event-loop turns. It is the cheap, high-value
part and it removes the unyielding callback on both sides. Recommendations **2
and 3** (an output budget in `FrameScanner.feed()` with a separately tracked
deferred tail) interact directly with the 64 KiB overflow rule; the report itself
flags that folding a deferred tail into `_retained` would drop connections a real
device could legitimately drive. **Those two must not be implemented as a
drive-by fix** — if they are wanted, they need their own design pass and their
own tests. Recommendation **4** (cap concurrent door connections) is a simulator
fidelity change, defensible but optional.

Medium is fair: remotely reachable, no privilege, no interaction, no malformed
input, availability-only, and the simulator is a documented development tool.

### S-L2 — the write ceiling logs per message and then does not drop the connection — **CONFIRMED (Low)**

**In-process, real socketpair, `connection_lost` wrapped to record whether it
ever fires:**

```
MAX_WRITE_BACKLOG = 1048576
fed 0.17 MB of valid GET_SETTINGS; peer never read
write buffer now 1048900 B
300 more messages -> 600 MORE ERROR records          (2 handlers x 300 = 1 record per message)
after 3s: connection_lost fired = False, write buffer still 1048900 B
distinct ERROR messages: 1 of 600
sample: 'Simulator: client is not reading its responses (1048900 bytes buffered); dropping the connection'
```

**Real daemon, attacker with `SO_RCVBUF=8192` sending valid `GET_SETTINGS`:**

```
attacker wrote 2.89 MB of VALID GET_SETTINGS, never read a byte
daemon RSS 34.5 -> 36.6 MB (+2.1 MB)
  t+1s / t+5s / t+15s / t+30s:  Clients: 1 client
"client is not reading its responses" ERROR records: 2641
daemon log grew 341627 B for 2886312 B on the wire (x0.118)
--- 8 ordinary operator commands while the attacker is idle ---
NEW unthrottled ERROR records from ordinary operator activity: 2
daemon still says: Clients: 1 client
after the attacker closes: Clients: none
```

**Attack on the claim.** All three legs hold: one record per message with zero
throttling and every record byte-identical; `close()` never completing so
`connection_lost` never arrives and the protocol stays in
`DoorSimulator.protocols`; and `ctl status` contradicting the daemon's own
"dropping the connection" ERROR for as long as the peer holds the socket. Memory
really is bounded (+2.1 MB), which is why Low and not Medium is right. One
number is softer than claimed — 8 ordinary operator commands produced 2 new
records for me rather than 6 — but the poisoning effect is present.

`transport.abort()` here discards a tail we were sending to a peer that has
declared a protocol violation; it does not change the protocol or narrow what we
accept. Passes the wire check.

### S-L3 — four unthrottled, length-uncapped per-frame log sites — **CONFIRMED (Low)**

My own harness (shipped `PowerPetDoorClient` / `DoorSimulatorProtocol`, a
`StreamHandler` with the simulator's own format, one `data_received` of packed
frames, drained):

```
SHIPPED CLIENT - client.py:1816
  {"CMD":"a","success":"false"}   wire 580000 B -> log 1860000 B (x3.21)  20000 records
  {"CMD":"a"} (success absent)    wire 220000 B -> log 1460000 B (x6.64)  20000 records
  one 60 KB reason field          wire 240164 B -> log  240428 B (x1.00)      4 records
for comparison, the sites round 6 DID throttle:
  {x} (bad_frames, throttled)     wire 180000 B -> log    6624 B (x0.04)     52 records
  {}  (bad_messages, throttled)   wire 120000 B -> log    4902 B (x0.04)     52 records
SIMULATOR
  SET_HOLD_TIME holdTime:[]       wire 320000 B -> log  824000 B (x2.58)   8000 records
  SET_SCHEDULE {"index":[]}       wire 392000 B -> log  832000 B (x2.12)   8000 records
```

These match the round-7 numbers essentially digit for digit. One record per frame
in every case, no `MAX_LOGGED_LENGTH`, and `client.py:1816` is in the shipped
library. The fix is logging-side only (`EventThrottle` + `sanitize_text(...,
MAX_LOGGED_LENGTH)`); `EventThrottle` reports the first occurrence
unconditionally, so a genuine single device error is still logged immediately.
Nothing is refused or narrowed. Low stands.

### S-I4 — `reset()` clears `_paused` without resuming the transport — **REFUTED (not a defect)**

**What I ran.** `/tmp/refute/probes/sec4.py`, a transport counting
pause/resume:

```
paused=True A.pause=1 A.resume=0 backlog=92
after reset(): paused=False A.pause=1 A.resume=0
   -> transport A left PAUSED and never resumed: True
```

So the *observation* is accurate — I reproduced the divergence.

**Why I am refuting it as an actionable finding.** The report's own
recommendation offers an alternative: "or document in the docstring that
`reset()` may only be called when the transport is being discarded." That is
already the state of the code. `framing.py:578` reads:

```python
def reset(self) -> None:
    """Drop the undispatched backlog (the connection is over).
```

"the connection is over" **is** the documented precondition, stated on the
summary line. Both call sites honour it: `client.py:1452` is inside
`disconnect()`, which closes the transport shortly after, and
`protocol.py:370` is inside `connection_lost()`, where the transport is already
gone. The report says outright that no exploit path exists and that it could not
construct one. Round 7's backend persona independently checked the real asyncio
transport and found `pause_reading`/`resume_reading`/`resume_reading`-after-
`close()` are all idempotent no-ops, so even a mismatch cannot raise.

That leaves a hypothetical about a future caller, on a class whose docstring
already forbids that call. Churning `reset()` for it buys nothing and adds a
`resume_reading()` on a transport the caller is about to drop. **No fix.**

### T-M1 — `timeout = 60` does not cover the fuzz/property suite — **CONFIRMED (Medium)**

**What I ran.** A separate tree with the project's real `pyproject.toml`, the
infinite-loop mutation `while i < n:` → `while i <= n:` at `framing.py:425`, and
two tests exercising the identical hang — one plain, one inside `@given`.

```
=== Case A: plain test (signal method, project default) ===
pytest_exit=1 elapsed=60s
FAILED tests/test_hangprobe.py::test_plain_hang - Failed: Timeout (>60.0s) fr...

=== Case B: identical hang inside @given (project default) ===
pytest_exit=137 elapsed=150s        # SIGKILL from my outer wrapper
--- output (bytes: 0) ---           # pytest produced nothing at all

=== Case C: same @given test with --timeout-method=thread ===
pytest_exit=1 elapsed=61s
    FrameScanner().feed(text)
  File ".../framing.py", line 445, in feed
    i = scanner.scan(data, i)
+++++++++++++++++++++++++++++++++++ Timeout ++++++++++++++++++++++++++++++++++++

=== Case D: @given hang under the DEFAULT addopts (-n auto / xdist) ===
pytest_exit=137 elapsed=150s
```

**Attack on the claim.** Case A is the control and proves the backstop works for
ordinary tests, so the difference is attributable to hypothesis and not to my
harness. Case D matters and the report did not run it: the gap is **not** an
artifact of `-n0`; it is present under the project's real `addopts = "-n auto"`
too. Case C confirms the one-line fix works and names the hanging line. Medium is
fair — the failure mode in CI is a silent `timeout-minutes: 30` kill naming no
test, on the weekly unseeded fuzz cron, which is precisely where a novel hang
would first appear.

### T-M2 — `FrameScanner.buffer` can return a truncated remainder — **CONFIRMED BUT OVERSTATED (Medium → Low)**

**Mutation, full suite on a fresh copy:** `if len(self._pieces) > 1:` → `> 2:`

```
OK TM2  claimed=SURVIVED  actual=SURVIVED   2454 passed in 56.36s
```

**Non-equivalence, proven by execution (mutation verified applied by grep first):**

```
pristine: pieces= ['{"a": ', '"xy']  retained= 9  buffer= '{"a": "xy'
MUTANT:   pieces= ['{"a": ', '"xy']  retained= 9  buffer= '{"a": '  len= 6
```

So the mutant is real, non-equivalent, and survives — the test gap is genuine and
the recommended one-test fix is correct.

**Why I am downgrading.** The report lists three "public consumers". I checked
all three and none of them is production logic:

- `client.py:1878` — `_buffer`, docstring `"""The framing scanner's un-parsed
  remainder (introspection hook)."""`, read only by `tests/test_client.py`.
- `simulator/protocol.py:355` — `buffer`, same docstring, same story.
- `framing.py:646` — `extract_frames` builds a **fresh** scanner and feeds once,
  so it can never hold two pieces. I confirmed this: under the mutant,
  `extract_frames('{"a": 1}{"b": ')` still returns `'{"b": '` correctly.

Nothing in `src/` makes a decision based on `.buffer`. The demonstrated blast
radius is "a test-facing introspection property would lie if someone changed the
coalesce guard". That is a legitimate Low-severity test gap, not a Medium.

### T-M3 — `test_flush_restarts_the_quiet_period` cannot fail — **CONFIRMED BUT OVERSTATED (Medium → Low)**

**Mutation (delete the line the test is named after), full suite:**

```
OK TM3  claimed=SURVIVED  actual=SURVIVED   2454 passed in 56.39s
```

**Behaviour is observable** — my own demo inserting one `clock.advance()` before
the flush:

```
PRISTINE  records after advance+flush+near-miss: ['seen 5 (5 bytes)']
MUTANT    records after advance+flush+near-miss: ['seen 5 (5 bytes)', 'seen 6 (6 bytes)']
```

**I found the test to be weaker than the report says.** Deleting the *other*
assignment in `flush()` (`self._reported = self._count`) also leaves it green:

```
applied: drop _reported update
tests/test_framing.py::...::test_flush_restarts_the_quiet_period   1 passed in 0.19s
```

So the test pins only the log record `flush()` itself emits, not either piece of
state it updates.

**Why I am downgrading anyway.** The title "cannot fail" is loose — the test can
fail for other mutations of `flush()`, and it does exercise a real path. What is
true and demonstrated is that the specific behaviour it is named for is
unpinned. That is exactly the same shape as T-L1/T-L4/T-L5/T-L6, all of which
were filed Low. Rating this one Medium is inconsistent with the report's own
scale. The fix is one line and should be done; it is a Low.

### T-M4 — a `# pragma: no cover` claims its branch is untestable — **CONFIRMED BUT OVERSTATED (Medium → Low)**

**What I ran.** I wrote my own version of the proposed test into a copy of the
tree and ran it against pristine source, then against source with the
`try/except` removed.

```
=== 1. on pristine source ===
1 passed in 0.20s
=== 3. remove the try/except ===
FAILED tests/simulator/test_refute_pragma.py::test_cleanup_swallows_a_failing_remove_reader
1 failed in 0.18s
```

So the test is real (it fails when the code it covers is removed), it is
deterministic, and it asserts precisely the contract the `except` exists for.

**Why I am downgrading.** The report says the pragma's justification is
"factually wrong". It is not. The justification reads: *"Linux selectors swallow
errors for dead fds, so this cannot be triggered deterministically."* The first
clause is true, and my test does **not** falsify it — it triggers the clause by
monkeypatching `loop.remove_reader` on the running event loop, i.e. by replacing
a stdlib API, not by driving a real selector into the failure. The honest framing
is "the clause is reachable through a test seam", not "the stated rationale is
false". Value delivered: one pragma removed and 2 statements returned to the
100 % gate, on a defensive `except: pass`. That is a Low.

### T-M5 — comparisons not exercised at the boundary; compound conditions never with the second operand decisive — **CONFIRMED (Medium)**

**What I ran.** I re-derived 9 of the listed sites as my own mutations plus one
control that must be caught, each a full suite run on a fresh copy:

```
OK M5-state-end    claimed=SURVIVED  actual=SURVIVED   2454 passed
OK M5-state-start  claimed=SURVIVED  actual=SURVIVED   2454 passed
OK M5-tzlen        claimed=SURVIVED  actual=SURVIVED   2454 passed
OK M5-sanitize     claimed=SURVIVED  actual=SURVIVED   2454 passed
OK M5-pauseat      claimed=SURVIVED  actual=SURVIVED   2454 passed
OK M5-floatmax     claimed=SURVIVED  actual=SURVIVED   2454 passed
OK M5-intmax-CTRL  claimed=CAUGHT    actual=CAUGHT     2 failed, 586 passed
OK M5-shorthand    claimed=SURVIVED  actual=SURVIVED   2454 passed
OK M5-lowbatt      claimed=SURVIVED  actual=SURVIVED   2454 passed
OK M5-pacing       claimed=SURVIVED  actual=SURVIVED   2454 passed
```

The `int`/`float` pair is the strongest evidence the technique is sound rather
than blanket: the same `>` → `>=` flip on the **int** max-value check at
`commands/base.py:165` is caught, and on the **float** check at `:177` it
survives.

**Non-equivalence at the schedule boundary, executed:**

```
midnight-crossing 22:00-06:00        normal 08:00-17:00
  21:59 -> False                       07:59 -> False
  22:00 -> True   <- unasserted        08:00 -> True
  05:59 -> True                        16:59 -> True
  06:00 -> False  <- unasserted        17:00 -> False
```

`tests/simulator/test_state.py:436` asserts only 23:00, 02:00 and 12:00 — the
interior and one far-outside point. And `M5-shorthand` survives because
`float(parts[2]) if len(parts) >= 2` would raise `IndexError` on the documented
2-word form, which no test supplies.

**One caveat I would raise.** `client.py:1595` (`diff < MINIMUM_TIME_BETWEEN_MSGS`
→ `<=`) is near-equivalent: at exactly the boundary the mutant does
`await asyncio.sleep(0)`, which yields a loop turn but changes no contract. I
would not spend a test on that one. The other eight sampled sites are genuinely
behaviour-changing at the boundary.

Medium is fair for an aggregate finding with 20+ proven sites, several of which
are user-visible contracts (schedule windows, the CLI max-value validator, the
documented script shorthand) or protocol-facing (the wire string length limit).

### T-L1 — shipped resource bounds have no test that pins their value — **CONFIRMED (Low)**

```
OK TL1a  MAX_BUFFER_SIZE 64KiB -> 63KiB           SURVIVED  2454 passed
OK TL1b  THROTTLE_QUIET_PERIOD 60 -> 86400        SURVIVED  2454 passed
OK TL1c  MAX_WRITE_BACKLOG 1MiB -> 16MiB          SURVIVED  2454 passed
OK TL1d  MIN_BLOCKED_RECHECK 0.1 -> 0.2           SURVIVED  2454 passed
OK CTRLa MAX_INFLIGHT_FRAMES 64 -> 4096           CAUGHT    1 failed, 1517 passed
OK CTRLb MAX_RETAINED_PIECES 64 -> 32             CAUGHT    1 failed, 2201 passed
```

The two controls are neighbouring constants in the same module, so this is a real
gap and not a property of the technique. Relaxing the two per-connection memory
caps that round-6 security work added by 16× leaves the suite fully green. Low.

### T-L2 — `CONTROL_PORT_OFFSET` is dead code — **CONFIRMED (Low)**

```
OK TL2a  CONTROL_PORT_OFFSET 1->2         SURVIVED  2454 passed
OK TL2b  DEFAULT_HISTORY_LIMIT 20->21     SURVIVED  2454 passed

$ grep -rn "CONTROL_PORT_OFFSET" . --include=*.py ...
src/powerpetdoor/simulator/cli.py:163:CONTROL_PORT_OFFSET = 1        (only hit outside .claude/)
AST Name nodes: 1 ['Store']                                          (zero reads)
cli.py:1085:  control_port = args.port + 1 if args.daemon == -1 else args.daemon
docs/simulator.md:97: ... (default control port: door port + 1)
```

Dead constant, inlined at the use site (a DRY-rule violation by the project's own
`CLAUDE.md`), and the user-facing default it names has no executable pin. Low.

### T-L3 — `find_iana_for_posix`'s "first match wins" is untested — **CONFIRMED (Low)**

```
OK TL3  reverse map last-match wins       SURVIVED  2454 passed

EST5EDT,M3.2.0,M11.1.0
resolves to: America/Detroit
candidates: 27 | first: America/Detroit | last: US/Michigan
```

Observable, deterministic, and a public export used to map a device-reported
POSIX TZ back to an IANA name. The suggested tzdata-robust assertion
(`== min(candidates)`) is the right shape. Low.

### T-L4 — the second `script_delay > 0` guard is still unpinned — **CONFIRMED (Low)**

```
OK TL4a  cli.py:488  i > 0 and script_delay > 0  ->  >= 0   SURVIVED  2454 passed
OK TL4b  cli.py:517  script_delay > 0            ->  >= 0   CAUGHT    1 failed, 593 passed
```

The sibling control being caught is what makes this specific rather than
generic: round 6 added the zero-delay test for the outer loop only. Low.

### T-L5 — the "may be absent on some firmware variants" guards are never seen with the field absent — **CONFIRMED (Low)**

```
OK TL5a  client.py:792  listeners and field_name in settings  -> or   SURVIVED  2454 passed
OK TL5b  client.py:843  stats_listeners[...] and FIELD in msg -> or   SURVIVED  2454 passed
```

Non-equivalence demonstrated with a registered sensor listener and a
`GET_SETTINGS` payload omitting that field:

```
PRISTINE            listener calls: []   log records: []
MUTANT (and -> or)  listener calls: []
                    log records: [('ERROR', 'Error handling GET_SETTINGS response: {...}', exc_info=True)]
```

That is the "full traceback per frame" failure mode the round-5 security work
removed elsewhere, and no test in the suite combines the two conditions. The
code's own comment at `client.py:796-797` calls out exactly this case. Low.

### T-L6 — `STATUS: clients=0` is annotated "no change" but nothing asserts it — **CONFIRMED (Low)**

```
OK TL6  ctl.py:461  new_status = count > 0  ->  >= 0    SURVIVED  2454 passed

$ grep -n "invalidate" tests/simulator/test_ctl_interactive.py tests/simulator/test_ctl.py
tests/simulator/test_ctl_interactive.py:249:  b"STATUS: clients=1\n",  # change -> invalidate
```

The only occurrence of the word is a comment; nothing observes
`interactive.invalidate()`. The test already enumerates five status shapes, so
this is assertions added to an existing scenario. Low.

---

## Recommended Fix List

Survivors only, in the order I would fix them. Nothing here changes a byte we
send to the device or narrows what we accept from it.

**Code — correctness, do first**

1. **F-M1** — reject non-finite in `parse_arg`'s `"float"` branch
   (`simulator/commands/base.py`); fixes `holdtime`, `inside`, `outside`,
   `charge_rate`, `discharge_rate` in one place. Separately make `holdtime`
   validate-then-write so no command can report `ERROR` after mutating state.
   (The `"int"` branch already rejects `nan` via `ValueError` — no change needed
   there.)
2. **B-M1** — type-guard `_on_battery_update` at the facade, mirroring
   `_on_hw_info_update`; makes the "keep the cached value" fallback actually
   work. Fix the `_on_hw_info_update` docstring's "the only device payload the
   facade retains" sentence while there.
3. **B-L1** — same guard at `_on_total_cycles_update`,
   `_on_total_retracts_update`, `_on_timezone_update`. Ships with (2).
4. **F-M2** — validate `sensor:` in `_execute_step` alongside the existing
   `Unknown action` / `Unknown setting` checks, and have
   `DoorMotionEngine.trigger_sensor` refuse an unrecognised name.
5. **S-M1, recommendation 1 only** — make `_pump()` dispatch at most
   `max_inflight` frames per invocation and re-arm via `loop.call_soon`.
   **Do not implement recommendations 2–3** (a `FrameScanner.feed()` output
   budget with a deferred tail) as part of this: they sit directly on the 64 KiB
   overflow rule and could drop connections a real device can legitimately
   drive. If wanted, they need their own design pass. Recommendation 4
   (connection cap) is optional.
6. **S-L2** — latch the drop (flag or clear `self.transport`), use
   `transport.abort()`, wrap the remaining ERROR in an `EventThrottle`.
7. **S-L3** — `EventThrottle` + `sanitize_text(..., MAX_LOGGED_LENGTH)` on
   `client.py:1816` and the three simulator rejection sites. `client.py:1816` is
   in the shipped library, so it goes ahead of the simulator three if split.
8. **F-L3** — give each script action a known-parameter set and raise
   `ScriptError` on anything else; failing that, log the parameters the step
   *used*.

**Test / CI**

9. **T-M1** — `timeout_method = "thread"` in `[tool.pytest.ini_options]`. One
   line, and it is the only thing standing between a fuzz-suite hang and a
   30-minute unattributable CI kill. Confirmed to work under the project's real
   `-n auto` addopts, not just `-n0`.
10. **T-M5** — the parametrized boundary policy: `(limit-1, limit, limit+1)` per
    numeric constant, the four edge minutes of a normal and a midnight-crossing
    schedule window, and the three 2-word script shorthand defaults. Do this
    *after* the code fixes above so the new guards get boundary tests too. Skip
    `client.py:1595`, which is near-equivalent.
11. **T-L5** — parametrized test over the seven `sensor_fields`: listener
    registered, field absent, assert no call and no logged exception.
12. **T-L1** — one value assertion per shipped bound (`MAX_BUFFER_SIZE`,
    `THROTTLE_QUIET_PERIOD`, `MAX_WRITE_BACKLOG`, `MIN_BLOCKED_RECHECK`).
13. **T-L4, T-L6, T-L3** — one targeted test each (zero `script_delay` between
    scripts; `invalidate` counter asserted per `STATUS:` line;
    `find_iana_for_posix(p) == min(candidates)`).
14. **T-M2, T-M3, T-M4** (all now Low) — the two-piece `buffer` test; one
    `clock.advance(THROTTLE_QUIET_PERIOD)` inserted before the `flush()`; the
    `remove_reader` cleanup test plus removing the `ctl.py:365` pragma.
15. **T-L2** — use `CONTROL_PORT_OFFSET` at `cli.py:1085` or delete it, then
    extend `tests/test_docs_accuracy.py` to `docs/simulator.md`'s option table.

**Docs / cosmetics — one pass**

16. **F-L1** — "Conditions are used with the `wait_for` action"; retitle the
    second table "Conditions for `assert`" and drop "also".
17. **F-L2** — replace `wait 2` + `assert door_status DOOR_CLOSED` with
    `wait_for door_closed` in both the YAML example and the
    `from_simple_commands` list.
18. **F-L4** — document `inside` / `outside` (with `duration`) and the
    `door_closing` condition.
19. **F-T1** — soften `protocol.md:175` to state the observed position and its
    uncertainty. Doc side only; the code must not move to match a
    reverse-engineered doc.
20. **F-L5** — have `--list-scripts` reuse the `list` rendering, printing the
    real path from `get_extra_script_files()`.
21. **F-L6** — make the symlink refusal's tail policy-aware, the way
    `describe_script_argument()` already is.
22. **F-T2** — render the schedule sensor scope through one helper; use the
    plural consistently.

## Discarded

- **S-I4 (`FrameDispatcher.reset()` clears `_paused` without resuming)** —
  REFUTED as actionable. The divergence is real and I reproduced it, but
  `reset()`'s docstring summary line already states the precondition ("the
  connection is over"), both in-tree call sites honour it, the report itself
  could construct no exploit path, and asyncio's selector transports make
  `pause_reading`/`resume_reading` idempotent no-ops on a closing socket. The
  recommendation's own fallback ("document in the docstring") is already
  satisfied by the code as written. No change.

**Severity adjustments (kept on the list, re-rated):** T-M2, T-M3 and T-M4 are
all real and all worth fixing, but each is a single missing or weak test with no
production consumer affected — the same class the same report filed as Low six
times over. Rating them Medium is inconsistent with its own scale.

**Numbers I would not quote:** security M1's "blocks `ctl status` for 14.9 s"
(I measured a 2 188 ms max on the same 64-socket / 16.78 MB one-shot burst, from
a 136 ms baseline), and security L2's "6 new ERROR records from 8 operator
commands" (I measured 2). Both underlying defects are confirmed; only those two
supporting figures are larger in the report than I could reproduce.
