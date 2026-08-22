# Backend Developer Analysis — Round 6

Scope: `src/powerpetdoor/{client.py, door.py, const.py, schedule.py, tz_utils.py,
framing.py, sanitize.py}` and `src/powerpetdoor/simulator/{server.py, protocol.py,
state.py, scripting.py, engine.py}` at commit `8a24804`. The interactive CLI
(`cli.py`, `ctl.py`, `prompt_common.py`, `commands/`) is out of scope.

Every finding below is marked **[verified at runtime]** and was reproduced with a
throwaway script run against the in-repo source. All probe scripts were deleted; no
repo file was modified (`git status` clean). Baseline on this tree:
`uv run pytest` → **2324 passed**.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 1 |
| Low      | 4 |
| Trivial  | 1 |
| **Total** | **6** |

All nine round-5 findings are fixed and hold under runtime probing. The two pieces of
machinery the brief asked me to weight hardest are correct: the `list[str]` segment
buffer is **byte-for-byte equivalent** to one-shot delivery across 40 000 randomized
chunked feeds (frames, retained remainder, `_retained`, *and* `discarded` — the T1
fix), holds its four invariants across 20 000 multi-feed sequences, and is unaffected
by `.buffer` being read between every feed. `EventThrottle`'s doubling schedule,
`flush()` and `reset()` all behave as documented, and are correctly scoped to a
connection on both sides. The schedule wire-parity change is clean: both emitters are
now type-identical field for field, and every consumer in the tree (diff, content key,
both parsers) accepts `"1"`, `1` and `true` interchangeably, so nothing downstream
moved.

The findings that remain are in the *margins* of those changes rather than in them.
The Medium is the one response sub-object the round-5 `_payload_mapping()` sweep did
not reach. Two of the Lows are the last two readers of device-supplied data in the
schedule path that rounds 4–5 hardened everywhere else. One Low is a genuine
regression introduced by the round-5 L3 fix (the 64 KiB cap no longer bounds memory),
measured both ways so the trade can be judged on numbers rather than on the memcpy
ratio alone.

---

## Findings

### Medium

#### M1. `fwInfo` is the one payload sub-object `_payload_mapping()` missed, and a non-dict value silently poisons three public properties **[verified at runtime]**

- **Files**: `src/powerpetdoor/client.py:908-913` (`_handle_hw_info`),
  `src/powerpetdoor/door.py:1319-1320` (`_on_hw_info_update`),
  `src/powerpetdoor/door.py:1217-1227` (`refresh_hardware_info`),
  `src/powerpetdoor/door.py:896-920` (`firmware_version`, `hardware_version`,
  `hardware_info`).
- **Problem**: round 5 introduced `_payload_mapping()` (`client.py:698-710`) precisely
  so a device-supplied sub-object that is not a mapping takes the graceful "Response
  missing expected field" path instead of escaping as a `TypeError`/`KeyError`. Five
  handlers were converted — `settings` ×4 and `notifications`. `fwInfo` is the sixth
  response sub-object of that shape and it was not converted:

  ```python
  if FIELD_FWINFO in msg:
      self._notify_listeners(self.hw_info_listeners, msg[FIELD_FWINFO])
      self._resolve_future(future, msg[FIELD_FWINFO])
  ```

  It is also the only one whose value is **cached** rather than merely read.
  `door._on_hw_info_update` assigns it verbatim (`self._hw_info = data`), and three
  documented public properties then treat `_hw_info` as a dict.
- **Impact**: one frame poisons the facade's cached hardware info, and the poisoning
  is *silent* on the listener path because `_notify_listeners` isolates listener
  exceptions and the assignment itself does not raise. Observed, feeding
  `{"CMD":"GET_HW_INFO","success":"true","fwInfo":"1.2.3"}` into `data_received`:

  ```
  log lines produced by the poisoning frame: []      <-- nothing at WARNING or ERROR
  door.hardware_info    -> AttributeError: 'str' object has no attribute 'copy'
  door.firmware_version -> AttributeError: 'str' object has no attribute 'get'
  door.hardware_version -> AttributeError: 'str' object has no attribute 'get'
  cached _hw_info: '1.2.3'
  ```

  The same value reached through `refresh_hardware_info()` raises
  `AttributeError: 'int' object has no attribute 'copy'` out of a documented public
  coroutine (that path *is* logged, via `refresh()`'s `_log_refresh_failures`). The
  state heals on the next well-formed `GET_HW_INFO` response — but a device that
  answers a scalar once will answer a scalar every time, so in practice it is
  permanent.
- **Why this is the same class the codebase already rejects**: round-4 M1 hardened
  `ScheduleTime.from_dict` with exactly this argument — "a device that answers
  `in_start_time: 5` (or `null`, or a list) must produce a `ValueError` the caller can
  handle, not an `AttributeError` out of a documented coroutine". This one is strictly
  worse than that case, because the bad value is *retained* and the failure surfaces
  later, in a property read, with nothing in the log tying it to the frame that caused
  it.
- **Trigger**: a nonconforming (or compromised) device. Not attacker-reachable in a
  normal LAN deployment — which is why this is Medium and not higher — but the whole
  untrusted-input program in this tree is built on that premise, and `process_message`
  documents it ("All network data is untrusted").
- **Fix**: route it through the helper that exists for it —
  `fw_info = self._payload_mapping(msg, FIELD_FWINFO)` / `if fw_info is None: return`.
  Two lines, and it makes the handler identical in shape to its five siblings. A
  defensive `isinstance(data, dict)` in `door._on_hw_info_update` would also stop the
  facade caching a scalar handed to it by a third-party client subclass.

---

### Low

#### L1. The 64 KiB framing cap no longer bounds memory: the piece list costs up to ~26x the advertised cap, and the CPU it bought is ~2x, not ~590x **[verified at runtime]**

- **File**: `src/powerpetdoor/framing.py:231-371` (`FrameScanner`), `MAX_BUFFER_SIZE`
  at `framing.py:44-46`.
- **Problem**: round-5 L3 replaced the retained remainder string with a `list[str]` of
  pieces plus a running character count. `_retained` counts *characters*, and the
  overflow check is `retained > self._max_buffer` — so the cap bounds the character
  count, not the memory. Each retained piece is a separate `str` object (~40–49 bytes
  of header each) plus a slot in the list's pointer array, and nothing ever coalesces
  them until a frame completes or the cap trips. The module docstring still advertises
  "**Bounded memory**: a hard cap on the un-parsed buffer prevents a hostile or broken
  peer from growing memory without bound", and `MAX_BUFFER_SIZE` is documented as the
  "hard cap … on the un-parsed receive buffer retained between calls".
- **Measured** (RSS delta, `/proc/self/statm`, 50 scanners each filled to just under
  the cap with one never-terminated object; the "coalesced" column is the prototype
  fix below):

  | peer chunk size | current | vs 64 KiB cap | with coalescing |
  |----------------:|--------:|--------------:|----------------:|
  | 1 B  |   517 KiB/conn | 8.1x  | 49 KiB/conn |
  | 2 B  | **1710 KiB/conn** | **26.7x** | ~cap |
  | 4 B  |   799 KiB/conn | 12.5x | ~cap |
  | 64 B |    30 KiB/conn | 0.5x  | ~cap |

  (1-byte chunks are *cheaper* than 2-byte ones because CPython caches single-character
  latin-1 strings, so only the pointer array is paid for.)
- **And the CPU win it was traded for is small.** Round 5's "~590x" was a
  *memcpy-bytes-per-delivered-byte* ratio, not a throughput ratio. Benchmarking the
  growth phase only (0 → just under the cap, no frames completing) against a faithful
  reimplementation of the pre-L3 shape — verified to frame identically on
  20 000/20 000 randomized inputs — the actual speedup is:

  | chunk | new | old | speedup |
  |------:|----:|----:|--------:|
  | 1 B   | 1.7 MB/s | 0.8 MB/s | 2.1x |
  | 2 B   | 2.9 MB/s | 1.5 MB/s | 2.0x |
  | 16 B  | 9.1 MB/s | 6.0 MB/s | 1.5x |
  | 1 KiB | 11.7 MB/s | 11.0 MB/s | 1.1x |

  Python per-call overhead dominates the memcpy at every chunk size, which is why the
  590x ratio did not translate.
- **Impact**: bounded and self-limiting per connection (overflow clears it and both
  consumers drop the connection), but a peer that dribbles *and stops just below the
  cap* holds the memory indefinitely — the simulator has no idle timeout and no
  connection cap (`server.py:162`, plain `loop.create_server`). 50 such connections
  cost 87 MB instead of the 3 MB the character budget implies.
- **Fix** (5 lines, keeps essentially all of the CPU win): coalesce when the piece list
  grows past a small bound, e.g. at the end of `feed()`,
  `if len(head) > 64: head = ["".join(head)]`. The join is O(retained) but amortized
  over 64 feeds, so per-byte cost stays ~64x below the old shape while memory returns
  to the advertised cap. Measured with that prototype: within 5–20% of current runtime
  at every chunk size, and 49 KiB/conn instead of 1.7 MiB. Whatever is chosen, the
  `MAX_BUFFER_SIZE` docstring should say which resource it actually bounds.
- **Judgment**: reported because "the cap is the defensive control and it under-delivers
  by 26x" is squarely this persona's remit, and because the numbers change how the
  round-5 trade reads. Not because I think the current behavior is dangerous.

#### L2. `door.refresh_schedules()` raises a bare `TypeError` on a scalar `GET_SCHEDULE_LIST`, and fans out per character on a string **[verified at runtime]**

- **File**: `src/powerpetdoor/door.py:1056-1067`.
- **Problem**: `indices` comes straight off the wire (`client._handle_schedule_list`
  resolves `msg[FIELD_SCHEDULES]` unmodified — correctly, it is not a mapping so
  `_payload_mapping` does not apply), is guarded only by `if not indices`, and is then
  iterated. Observed against a stubbed `send_message`:

  ```
  schedules=3          -> RAISED TypeError: 'int' object is not iterable
  schedules=1.5        -> RAISED TypeError: 'float' object is not iterable
  schedules=True       -> RAISED TypeError: 'bool' object is not iterable
  schedules='01'       -> ok, 2 schedules, 2 GET_SCHEDULE calls   (one per character)
  schedules={'0': {}}  -> ok, 1 schedule,  1 GET_SCHEDULE call    (iterates keys)
  ```

- **Impact**: an unhandled `TypeError` out of a documented public coroutine — the exact
  failure round-4 M1 eliminated one layer down, in `Schedule.from_dict`. The string
  case is arguably worse than the raise: it issues one `GET_SCHEDULE` per character
  against a single-connection device that rate-limits with
  `MINIMUM_TIME_BETWEEN_MSGS` between messages, so a 200-character reply is 200
  sequential round trips. `refresh()` does **not** call this, so `connect()` is
  unaffected.
- **Also**: `logger.warning("Timeout fetching schedule %d", idx)` (`door.py:1078`) uses
  `%d` on a value that is only an int if the device says so; a string index turns the
  timeout warning into a logging-internal formatting error on stderr.
- **Fix**: `if not isinstance(indices, list): logger.warning(...); self._schedules = []; return []`
  before the loop, and `%s` in the two log calls.

#### L3. `compute_schedule_diff()` is the last reader of a schedule `index` that does not go through a coercer; hostile indices raise `TypeError` or propagate into the entries it tells you to SET **[verified at runtime]**

- **File**: `src/powerpetdoor/schedule.py:609-650`
  (`cast(int, entry.get(FIELD_INDEX))` at lines 612 and 622).
- **Problem**: round-5 L1 hardened `schedule_entry_content_key` — the *content* half of
  this function's inputs — so that every wire spelling of every flag collapses to one
  key. The *index* half, read sixty lines below out of the same untrusted device dicts,
  is still read raw, with two `cast(int, ...)` calls telling mypy otherwise. Every other
  index reader in the tree is coerced: `door.Schedule.from_dict` and
  `state.Schedule.from_dict` (`coerce_schedule_int`), `protocol._wire_schedule_index`
  (round-4 L3), `scripting.py` (`_script_number`, round-5), `commands/schedules.py`
  (`MAX_SCHEDULE_INDEX`). This is the one that was missed.
- **Observed** with `current_schedule` entries carrying various indices:

  ```
  mixed str/int index -> TypeError: '<' not supported between instances of 'str' and 'int'
  None + int index    -> TypeError: '<' not supported between instances of 'NoneType' and 'int'
  list index          -> TypeError: unhashable type: 'list'
  float index (1.5)   -> no error; entries_to_set carries index 1.5
  string index ("0")  -> no error; entries_to_set carries index "0"
  absent index        -> no error; entries_to_set carries index None
  ```

  The first three come out of `sorted(current_indices - matched_indices)` and the
  `{... for entry in current_schedule}` set comprehension; the last three are silent —
  the caller is handed a `SET_SCHEDULE` payload with `"index": null` / `"index": "0"`,
  which the simulator rejects cleanly (`coerce_schedule_int`) and a real device
  presumably also refuses, so the sync silently fails for that entry.
- **Reachability**: the *documented* usage in `docs/door.md:571-573` is
  `current = [s.to_dict() for s in await door.refresh_schedules()]`, which round-trips
  through `Schedule.from_dict` and therefore always yields int indices — so the
  documented path is safe. The docstring, however, says "List of current schedule
  entries on device", which invites raw wire dicts, and the function is exported from
  `powerpetdoor/__init__.py` for exactly that use.
- **Fix**: read both index sites through `coerce_schedule_int(entry.get(FIELD_INDEX, 0),
  "index", MAX_SCHEDULE_INDEX)` inside a `try`, skipping (or index-0-defaulting) entries
  that fail — which also removes the two `cast()` lies. The helper is already imported
  in this module.

#### L4. `EventThrottle`'s schedule is monotonic over a connection's lifetime, so a *new* burst late in a long-lived connection is invisible **[verified at runtime]**

- **File**: `src/powerpetdoor/framing.py:65-135`; use sites
  `framing.py:260` (`FrameScanner._discards`), `client.py:332` (`_non_ascii`),
  `simulator/protocol.py:313` (`_non_ascii`).
- **Everything about the mechanism is correct.** Verified: the doubling schedule fires
  on occurrences 1, 2, 4, 8 … (13 lines for 5000 events); `flush()` emits the
  suppressed tail exactly once and is idempotent; `reset()` clears the counters and the
  next `record()` reports immediately. Scoping is right on both sides —
  `client.disconnect()` flushes *and* resets `_non_ascii` and calls
  `_scanner.reset()` (which flushes and resets `_discards`), and the simulator's
  per-connection protocol object flushes in `connection_lost()`. Verified end to end:
  10 non-ASCII chunks produce 4 ERROR lines, `disconnect()` emits
  `"… 10 chunks, 20 bytes so far on this connection"`, and the counter is left at 0/0.
- **The gap** is that the threshold only ever moves forward and has no time component,
  while `flush()` runs only at connection end. Observed:

  ```
  after 1024 events (11 log lines), a fresh burst of 1023 events -> 0 log lines
  the 2048th event finally reports: "2048 events, 2048 bytes"
  ```

  So on a connection that has been up long enough to accumulate 1024 one-off events
  (this client is designed to stay connected for months — keepalive plus reconnect only
  on failure), a device that *starts* corrupting bytes emits nothing at all for its
  first 1023 corrupted reads. Both use sites are ERROR/WARNING-level fault indicators
  (non-ASCII from the device = link or firmware corruption; non-JSON garbage = a framing
  fault), i.e. exactly the signals an operator would want to see promptly.
- **Impact**: observability only — no counter is lost, and the eventual line carries
  cumulative totals. But "hostile peer cannot amplify the log" and "a real fault is
  reported promptly" are separable goals, and the current shape only achieves the first.
- **Fix**: add an elapsed-time floor next to the count threshold — record
  `time.monotonic()` at each report and also emit when more than N seconds have passed
  since the last one (or decay `_next` back toward 1 after a quiet period). Log volume
  stays logarithmic *per burst* while each new burst gets an immediate first signal,
  which is the property the class docstring claims.

---

### Trivial

#### T1. Two script parameters bypass `scripting.py`'s own boolean coercion, so `enabled: "0"` produces an **enabled** schedule **[verified at runtime]**

`simulator/scripting.py:467` (`enabled = params.get("enabled", True)`) and
`scripting.py:420` (`hold = params.get("hold", False)`) pass raw YAML values straight
into `state.Schedule(enabled=...)` and `engine.open(hold=...)`, both of which are
declared `bool` and used with plain truthiness. The same file already has a coercer
20 lines away — `_set_value`'s `str(value).lower() in ("true", "1", "on", "yes",
"enabled")` (`scripting.py:645`) — and the library owns two more (`make_bool`,
`coerce_schedule_flag`). Observed:

```
enabled=False   -> is_day_active False, to_dict "0"
enabled="false" -> is_day_active True,  to_dict "1"     <-- silently enabled
enabled="0"     -> is_day_active True,  to_dict "1"
enabled="off"   -> is_day_active True,  to_dict "1"
```

Unquoted YAML `false` parses to a real bool, so this only bites a quoted or templated
value — but the same argument applied to the numeric parameters, and round 5 bounded
all of them (`_script_number`). Three boolean coercers for one concept is also the
"two implementations = refactor" rule in CLAUDE.md. Fix: a `_script_bool` built on
`make_bool` (fail-closed like `coerce_schedule_flag`), used by `add_schedule`, `open`
and `_set_value` alike.

---

## Round 5 Fix Verification

All nine round-5 findings re-derived from the current source. All nine hold.

**M1 — schedule emitter wire parity.** Fixed, and the parity is now total, not just for
`enabled`. `door.Schedule.to_dict()` and `simulator.state.Schedule.to_dict()` produce
**identical dicts and identical per-field Python types** for the same logical schedule
(`index` int, `enabled` `str` `"1"`, `daysOfWeek` seven ints, `inside`/`outside` real
bools, four `{hour, min}` int blocks) — checked with a type-aware comparison, not dict
equality, since `True == 1` in Python. `compress_schedule()` inherits the same types via
`schedule_template` (`schedule.py:293`), and its key set matches the template exactly.
Emit→parse→emit is a fixed point on both sides. `assert_schedule_wire_types()` in
`tests/conftest.py:319` pins all of this, including the bool-vs-int distinction.

**Did the string `enabled` break any consumer?** No — I enumerated every reader of the
field in `src/`. There are exactly six: two emitters (`"1"`/`"0"`), two parsers (both
`coerce_schedule_flag`), `schedule_entry_content_key` (`coerce_schedule_flag`), and
`state.Schedule.is_day_active` (reads the parsed `bool`). The diff path is spelling-blind
in both directions:

```
device "enabled": "1"  vs local "1"   -> ([], [])   no-op
device "enabled": true vs local "1"   -> ([], [])   no-op
device "enabled": 1    vs local "1"   -> ([], [])   no-op
```

Ordering is unaffected (`enabled` is not part of any sort key), and the diff path is
unaffected (the content key normalizes before hashing).

**L1 — `schedule_entry_content_key` normalization.** Fixed. The firmware variants the
codebase hardened against all diff to a no-op now: string day flags
`["1","0",...]` → `([], [])`, the legacy integer bitmask `0b1010101` → `([], [])`,
integer/bool `enabled` → `([], [])`. An unreadable mask (`{"a": 1}`) produces the
hashable fallback `('?', "{'a': 1}")` instead of raising out of a diffing helper.

**L2 — `SET_SCHEDULE_LIST` requires a list.** Fixed (`protocol.py:695-699`). An absent
field raises `WireValueError(f"{FIELD_SCHEDULES} is required")`, a wrong-typed one
`"must be a list, got …"`, both of which `_handle_message` turns into the standard error
envelope with the real reason. `[]` remains the explicit clear. The atomic parse-then-
store ordering is preserved.

**L3 — `list[str]` segment buffer.** Fixed, and correct (see the fuzz results below);
the memory side effect is L1 above.

**T1 — chunk-independent `discarded`.** Fixed. Across 40 000 randomized inputs fed in
1–4 byte chunks versus a single one-shot `feed()` of the same string,
`diag.discarded` matched **every time** (0 divergences) — round 5 measured a ~58%
mismatch rate on the same experiment shape. `frames`, `buffer` and `_retained` also
matched 40 000/40 000.

**T2 — `keepalive()`/`check_receipt()` via `_track_task`.** Fixed
(`client.py:1261`, `1551`, `1554`). Confirmed at runtime that a tracked task raising
is reported immediately by `_on_task_done` ("Background client task failed") rather
than at GC time. The double-cancel concern is benign: `disconnect()` cancels
`self._keepalive` explicitly *and* sweeps `_tasks`; when the keepalive task cancels
itself via `_drop_connection` the coroutine `return`s immediately afterwards, so
`_must_cancel` resolves it to *cancelled* and `_on_task_done` skips it. `check_receipt`
clears `self._check_receipt` before its trailing `await`, so the self-cancel case
cannot arise there at all.

**T3 — sorted schedule lists.** Fixed on both sides:
`state.get_schedule_list()` returns `sorted(self.schedules.keys())` (`state.py:435`) and
`door.refresh_schedules()` sorts by index before assigning (`door.py:1086`), matching
`_on_schedule_update` (`door.py:1413`). The public `door.schedules` property is now
sorted regardless of which path last touched it.

**T4 — `MAX_SCHEDULE_INDEX` import.** Fixed: `scripting.py:73` reads
`from ..schedule import MAX_SCHEDULE_INDEX`. `protocol.py:129` and
`commands/schedules.py:10` do the same; only `state.py` still imports it, for its own
use.

**T5 — overflow checked before dispatch.** Fixed (`client.py:1628-1641`). Confirmed: a
chunk carrying a complete `GET_DOOR_STATUS` response followed by an unterminated 64 KiB
object now logs
`"…disconnecting (discarding 1 complete frame(s) received in the same read)"` and never
creates the doomed task. The simulator's twin (`protocol.py:387-394`) returns before its
dispatch loop too.

---

## Areas Reviewed With No Findings

**The `list[str]` segment buffer (`framing.py`).** The specific hazards the brief named
do not occur.

- *Running length.* `_retained == sum(len(p) for p in _pieces)` held on **20 000/20 000**
  multi-feed sequences, and `_retained` never exceeded `MAX_BUFFER_SIZE` undetected.
  Reasoning matches: `retained` is zeroed exactly where `head` is zeroed (frame
  completion) and incremented exactly where `tail` is appended.
- *`consumed = 0` on resumption.* The invariant that makes it correct — "pieces are
  non-empty **iff** an object is in progress, and when they are, `pieces[0]` starts at
  that object's `{`" — held 20 000/20 000. It is also structurally guaranteed: the loop
  can only exit with `scanner.open` False after a whitespace skip, a resync, or a
  completed frame, and all three set `consumed = i = n`, so `tail` is empty; and `head`
  is always `[]` at the moment the scanner transitions closed→open within a feed.
- *`.buffer` semantics.* The property coalesces in place, so it mutates `_pieces` — but
  it preserves length and content, and `_retained` is unaffected. Peeking `.buffer`
  twice after *every* feed produced 0 divergences in frames, remainder or `_retained`
  across 20 000 chunked sequences. `feed()` aliases `self._pieces` into a local, but
  there is no reentrancy (it is synchronous with no callbacks), so the aliasing is safe.
- *Interaction with overflow.* Boundary is exact: retained 16 with `max_buffer=16` does
  not overflow, 17 does. On overflow `reset()` clears pieces, `_retained`, the brace
  state and flushes the throttle — no join is performed on the way out, so overflow is
  the cheap path. A frame completing in the same feed as an overflow is still returned
  to the caller (the client then names it in the drop log — T5).
- *`extract_frames` equivalence.* 20 000/20 000 identical frames, remainder and
  diagnostics versus a one-shot `FrameScanner.feed()`.
- *Connection reuse.* Client resets in `disconnect()`; the simulator builds one protocol
  (and therefore one scanner) per accepted connection in `protocol_factory`
  (`server.py:150`) and resets in `connection_lost`.

**`EventThrottle` state lifetime and safety.** Covered under L4 — the mechanism itself,
its flush-on-connection-end on both sides, and its per-connection scoping are all
correct; only the missing time component is reported. Thread safety is a non-issue: both
holders are event-loop-only objects (the client documents loop-thread-only access, the
simulator protocol is an asyncio `Protocol`), and `record()`/`flush()` contain no await
points, so no interleaving is possible.

**`_payload_mapping()` across the response handlers.** I enumerated every handler that
indexes a device-supplied sub-object. Five are guarded (`settings` in
`_handle_get_settings`, `_handle_safety_lock`, `_handle_cmd_lockout`,
`_handle_autoretract`; `notifications` in `_handle_notifications`) and behave correctly:
a missing or non-mapping payload returns without resolving, which `process_message`
converts to `CommandError(cmd, "Response missing expected field")` rather than a
traceback. `_handle_door_status` and `_handle_schedule_list` read a scalar and a list
respectively, so the mapping helper does not apply to them, and both use the equivalent
`if X not in msg: return` guard. `_handle_schedule` passes its payload raw, but the only
consumer (`door._on_schedule_update`) parses through `Schedule.from_dict` and catches
`ValueError`. `_handle_hw_info` is the exception — M1. The refactor changed no observable
behavior for the five converted handlers: the previous shapes raised into the outer
`except Exception` and produced `CommandError(cmd, "Malformed response")`; now they
produce `CommandError(cmd, "Response missing expected field")`. Both are typed failures
on the same future; only the reason string moved, and the traceback is gone.

**Client connection lifecycle.** Not re-litigated in depth — round 5 derived the two
safety properties of `_pending_direct_losses` and drove all five teardown paths. I
re-read the code and the reasoning still holds line for line
(`connection_made`/`_adopt_transport`/`connection_lost`/`_on_transport_lost`/
`_drop_connection`, plus `_ConnectionAttempt`'s identity checks). Nothing in the
round-5 changes touched it.

**Simulator command handlers.** Every `SET_*` still validates before mutating, and the
two round-5 gaps are closed. `_handle_set_notifications` coerces the whole set before
assigning any attribute; `_handle_set_timezone`, `_handle_set_hold_time` and both
voltage setters bound their values at the door; `_wire_schedule_index` guards
`GET_SCHEDULE`/`DELETE_SCHEDULE`; non-string `cmd` values are answered with the error
envelope without touching the string-keyed registry.

**Engine (`engine.py`).** Unchanged since round 5 and still sound: single `_run` owner
via `_dispatch_depth`/`_defer_sequence`, deferred intents rather than resolved states,
`_retired`/`_aux_tasks` discarding via done-callbacks, `stop()` cancelling the
`call_soon` handle and failing status waiters, deadline-based `_hold_open` with
`MIN_BLOCKED_RECHECK` as a floor rather than a poll interval.

**`tz_utils.py`, `sanitize.py`, `const.py`.** No change and no findings. Cache init is
double-checked under a `threading.Lock`, all blocking I/O is inside `to_thread`,
`get_available_timezones()` returns a copy, and the POSIX parse-failure path sanitizes
the device string before logging.

**`scripting.py` lifecycle.** `Script.from_yaml` validates its shape before building
steps; `ScriptRunner` serializes runs under a lock with a documented `on_start` veto;
`_sleep_or_stop`/`_wait_for_status` race the stop event and always cancel-and-gather
their helper futures; every numeric parameter is bounded and finite-checked
(`_script_number`). Only the two boolean parameters in T1 are unguarded.
