# Backend Developer Analysis — Round 5

Scope: `src/powerpetdoor/{client.py, door.py, const.py, schedule.py, tz_utils.py,
framing.py, sanitize.py}` and `src/powerpetdoor/simulator/{server.py, protocol.py,
state.py, scripting.py, engine.py}` at commit `6f2dedd`. The interactive CLI
(`cli.py`, `ctl.py`, `prompt_common.py`, `commands/`) is out of scope — including
`commands/scripts.py`, where `ScriptQueue` lives.

Findings marked **[verified at runtime]** were reproduced with throwaway scripts run
against the in-repo source (scripts deleted afterwards; no repo file was modified).
Baseline health on this tree: `uv run pytest` → **2171 passed**, `ruff check src tests`
clean, `mypy src` clean (31 files).

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 1 |
| Low      | 3 |
| Trivial  | 5 |
| **Total** | **9** |

All six round-4 findings are fixed and hold under runtime probing, including the two
the brief flagged as the newest and most intricate. `_drop_connection`/`_was_connected`
fires exactly one reconnect on **all four** teardown paths (server close, keepalive
give-up, write failure, framing overflow) and zero on a post-shutdown adopt. The
`FrameScanner` state carried across `feed()` calls is chunk-invariant: 60 000 randomized
chunked-vs-one-shot comparisons produced **zero** divergence in the extracted frames or
the retained remainder, and 20 000 multi-feed sequences produced **zero** violations of
the `_scanned == len(_buffer)` / "buffer starts at the open object's `{`" invariants.
The `_pending_direct_losses` counting scheme is sound: it can mis-*attribute* a loss
under one interleaving, but it provably can never tear down a healthy transport nor
permanently swallow a real one (derivation in the verification section).

The Medium is not in any of that new machinery. It is a wire-format divergence between
the library's schedule emitter and both `docs/protocol.md` and the simulator's emitter —
the one place the round-4 T3 unification (shared *parsers*) did not reach, and one the
in-repo suite structurally cannot catch because the simulator's parser is lenient by
design.

---

## Findings

### Medium

#### M1. The library sends `enabled` as a JSON boolean; the protocol and the simulator use `"1"`/`"0"` **[verified at runtime]**

- **Files**: `src/powerpetdoor/door.py:250` (`Schedule.to_dict`),
  `src/powerpetdoor/schedule.py:273` (`schedule_template`, and therefore every
  `compress_schedule()` result) — versus
  `src/powerpetdoor/simulator/state.py:137` (`Schedule.to_dict`) and
  `docs/protocol.md:542,556` (`"enabled": "1"`, typed `"0"/"1"`).
- **Problem**: round 4's T3 unified the two schedule *parsers* on
  `coerce_schedule_flag`, so both sides now *read* `enabled` identically. The two
  *emitters* were not unified, and they disagree on exactly one field. What the
  library actually puts on the wire for `set_schedule()`:

  ```
  library  SET_SCHEDULE : {"index": 0, "enabled": true, "daysOfWeek": [1,...], "inside": true, ...}
  simulator GET_SCHEDULE: {"index": 0, "enabled": "1",  "daysOfWeek": [1,...], "inside": true, ...}
  docs/protocol.md       : "enabled" | "0"/"1" | Whether schedule is active
  ```

  Every other field matches (`daysOfWeek` ints, `inside`/`outside` bools). `index`,
  the time sub-objects — all identical. `enabled` is the sole divergence, and it is
  the field that decides whether an access-control entry is live.
- **Why the suite cannot see it**: the simulator's parser reads the flag through
  `coerce_schedule_flag` → `make_bool`, which accepts `true` and `"1"` alike, so a
  library→simulator round trip is green. Only real firmware can disagree, and the
  project's own reverse-engineered documentation says it does: the simulator "must
  stay faithful to observed device behavior" (CLAUDE.md), and it emits `"1"`.
- **Impact**: if the firmware reads `enabled` as a string (as the doc models it), a
  `SET_SCHEDULE` from this library either lands with the flag misread or is rejected
  outright — a schedule the user disabled could stay active, or vice versa. I cannot
  test against hardware, so I am reporting the divergence and the documentation that
  says which side is wrong, not a confirmed device behavior.
- **Also a rule violation**: CLAUDE.md "Component Reuse (Critical)" #4 — "the
  simulator must speak exactly the protocol the client speaks."
- **Fix**: emit `"1" if self.enabled else "0"` in `door.Schedule.to_dict()` and set
  `schedule_template[FIELD_ENABLED] = "1"`. This is risk-free in both directions:
  every consumer in the tree already accepts both spellings —
  `coerce_schedule_flag` via `make_bool`, and `schedule_entry_content_key`'s
  `if isinstance(enabled, str): enabled = enabled == "1"` — so no diff, cache or
  round trip changes behavior. `tests/test_schedule.py:873`
  (`schedule_template[FIELD_ENABLED] is True`) and the `to_dict` assertions would
  need updating to the wire spelling, which is the point of the change.

---

### Low

#### L1. `schedule_entry_content_key` reads `daysOfWeek` raw, so the firmware variant the codebase hardened against re-writes the whole schedule every sync **[verified at runtime]**

- **Files**: `src/powerpetdoor/schedule.py:490-542` (`schedule_entry_content_key`),
  compare `schedule.py:376-386` (`compress_schedule`, hardened for exactly this) and
  `schedule.py:73-89` (`coerce_schedule_day`, likewise).
- **Problem**: the codebase has twice decided that a firmware variant sending
  `daysOfWeek` as `"0"`/`"1"` *strings* is real enough to defend against — L4 changed
  `compress_schedule` from truthiness to `make_bool`, and `coerce_schedule_day`
  rejects anything that is not a recognizable flag. `schedule_entry_content_key` is
  the third reader of that field and it still does `tuple(entry.get(FIELD_DAYSOFWEEK, [0]*7))`
  with no normalization, so `("1","1",…)` and `(1,1,…)` are different keys. `enabled`
  gets half the treatment: `str` is normalized, but an integer `1`/`0` (which
  `make_bool` accepts everywhere else) is not.
- **Impact**: `compute_schedule_diff(device_entries, compress_schedule(...))` is
  public API (`__init__.py:132,166`) and is the incremental-sync path. Measured with
  a device entry carrying string day flags and an otherwise byte-identical local
  entry:

  ```
  device daysOfWeek ["1",...] -> entries_to_set = 1   (full rewrite)
  device daysOfWeek [1,...]   -> entries_to_set = 0   (correctly a no-op)
  ```

  Every entry looks changed, so every sync issues a full `SET_SCHEDULE` sweep against
  a single-connection, rate-limited device (`MINIMUM_TIME_BETWEEN_MSGS`) — the exact
  cost the function exists to avoid.
- **Fix**: build the key through the shared coercers —
  `tuple(coerce_schedule_flag(d, "daysOfWeek") for d in ...)` and
  `coerce_schedule_flag` for `enabled`/`inside`/`outside`. That also removes the
  three hand-rolled `== "1"` normalizations, which are the last places in the tree
  reading a wire flag without `make_bool`.

#### L2. `SET_SCHEDULE_LIST` wipes every schedule when the field is absent, and reports success when it is the wrong type **[verified at runtime]**

- **File**: `src/powerpetdoor/simulator/protocol.py:670-688`
- **Problem**: `schedules_data = msg.get(FIELD_SCHEDULES, [])` defaults an *absent*
  field to the empty list, which then takes the "load new schedules" branch and
  clears the store. A wrong-typed field skips the `isinstance(..., list)` branch
  entirely and falls through to the success response. Observed against a simulator
  holding schedules 0 and 1:

  ```
  {"config":"SET_SCHEDULE_LIST"}                       -> success:"true"  schedules now []   (wiped)
  {"config":"SET_SCHEDULE_LIST","schedules":{"a":1}}   -> success:"true"  schedules now [0,1] (ignored)
  {"config":"SET_SCHEDULE_LIST","schedules":"abc"}     -> success:"true"  schedules now [0,1] (ignored)
  ```

- **Impact**: contradicts the contract the rest of the module documents and enforces —
  `docs/protocol.md:448-462`, "The same validate-before-storing rule applies to every
  other `SET_*` command… A rejection answers `{"success":"false","reason":…}` and
  leaves state untouched." Here a malformed payload answers success, and a *missing*
  payload is destructive. Note the handler is already careful in the way that matters
  most (it parses the whole batch before touching state, so a partial load is
  impossible) — the gap is only in the two non-list shapes.
- **Fix**: `raise WireValueError(f"{FIELD_SCHEDULES} must be a list, got {…!r}")` when
  the value is present and not a list, and require the field rather than defaulting it
  (`require_schedule_field`-style), so "clear everything" stays an explicit
  `"schedules": []`.

#### L3. `FrameScanner.feed()` still copies the whole retained remainder per call, so a byte-at-a-time dribbler gets ~590× CPU amplification **[verified at runtime]**

- **File**: `src/powerpetdoor/framing.py:207` (`buf = self._buffer + data`)
- **Problem**: the S1 fix removed the quadratic *scan* — `_scanned` guarantees every
  character is examined once. The *copy* is still O(retained) per `feed()`: the
  remainder is re-concatenated into a fresh string on every call, and while an object
  is in progress the remainder grows to the 64 KiB cap before the overflow reset drops
  it. Average retained size over that cycle is ~32 KiB, so each delivered byte costs a
  ~32 KiB memcpy. Measured (4 MiB of peer bytes through one scanner, same machine):

  | chunk size | throughput of peer data |
  |-----------:|------------------------:|
  | 1 B        | 1.2 MB/s |
  | 2 B        | 1.9 MB/s |
  | 8 B        | 7.5 MB/s |
  | 64 B       | 53 MB/s |
  | 4 KiB      | 543 MB/s |
  | 64 KiB     | 712 MB/s |

- **Impact**: bounded, and materially cheaper for the attacker to *not* exploit — one
  core costs roughly 400 Mbit/s of wire traffic once 1-byte TCP segment overhead is
  counted, which is the same order as a plain flood. This is a different mechanism
  from the round-3 Informational #6 flood (memory, self-limiting, re-measured and
  dismissed in round 4) and from S1 (scan, fixed): it is CPU, driven by *chunking*
  rather than volume, and neither `pause_reading()` nor a write-buffer limit would
  throttle it.
- **Fix** (small, local to `framing.py`): retain the remainder as a `list[str]` of
  pieces plus a running length and `"".join()` only when a frame actually completes or
  the cap is hit, or keep a byte offset into a `bytearray` and slice instead of
  reallocating. The `_scanned`/`consumed` bookkeeping is unaffected either way.
- **Judgment**: reported because CPU efficiency on untrusted input is squarely this
  persona's remit and the fix is cheap, not because I think it is currently
  exploitable at a damaging rate.

---

### Trivial

#### T1. `FrameDiagnostics.discarded` is chunk-boundary dependent **[verified at runtime]**

`framing.py:226-235` counts an entire garbage run (including whitespace *inside* it)
via `buf.find("{", i)`, but whitespace at a resync boundary is consumed by the
`char.isspace()` branch and not counted. Which of the two a given character hits
depends on where the chunk boundary fell. Across 60 000 randomized inputs, `frames`
and the retained `buffer` matched between chunked and one-shot delivery **every
time**; `discarded` differed in ~58% of them (e.g. `' :}}\n\\{a{'` → 4 chunked vs 5
one-shot). No caller reads the field — `client.py:1566` and `protocol.py:373` only
consult `diag.overflow` — so this is purely a log-line inaccuracy. Fix, if wanted:
count `next_obj - i` only for non-whitespace, or drop the field to a bool.

#### T2. `keepalive()` and `check_receipt()` are the only client fire-and-forget tasks not routed through `_track_task`

`client.py:1203,1489,1492` use `self.ensure_future(...)` directly, so an exception
escaping either is reported by asyncio's "Task exception was never retrieved" hook at
GC time rather than immediately by `_on_task_done` — the exact failure mode
`_track_task`'s docstring (`client.py:407-419`, "instead of whenever the garbage
collector happens to reap the task") says the pattern exists to prevent. They are held
in `self._keepalive`/`self._check_receipt`, so the report is not *lost*, only late and
routed through a different logger. Both would need `transient=True` plus care that
`disconnect()`'s `_tasks` sweep does not double-cancel them (it already skips
`current`, which is the reachable case).

#### T3. `GET_SCHEDULE_LIST` returns indices in insertion order, and `door.refresh_schedules` does not sort while `_on_schedule_update` does **[verified at runtime]**

`state.py:428-430` returns `list(self.schedules.keys())` — with slots created out of
order the simulator answers `[5, 1, 3]`. `door.py:1051-1068` appends in that order and
assigns it straight to `self._schedules`, whereas `door.py:1386-1393` re-sorts by index
after every update. So the public `door.schedules` property is sorted or unsorted
depending on whether the last thing that touched it was a refresh or a push. One
`sorted()` in each place fixes both.

#### T4. `scripting.py` imports `MAX_SCHEDULE_INDEX` from `simulator.state` rather than the module that owns it

`simulator/scripting.py:73` reads `from .state import MAX_SCHEDULE_INDEX, Schedule`.
Round 4's T3 moved the constant to `powerpetdoor.schedule`; `state.py` re-exports it
only incidentally (it imports it for its own use). Componentization nit: the third
writer of a schedule index should import the bound from `..schedule` directly, so
removing the unused-looking name from `state.py`'s import list can never break the
script path.

#### T5. A complete frame extracted in the same `feed()` as an overflow is created as a task and then cancelled before it runs **[verified at runtime]**

`client.py:1566-1581` dispatches every frame with `_track_task(process_message(...))`
and only *then* checks `diag.overflow` → `_drop_connection()` → `disconnect()`, whose
`_tasks` sweep cancels the just-created tasks. Confirmed: a chunk containing a valid
`DOOR_STATUS` response followed by an unterminated 64 KiB object delivers **nothing**
to the listener. Defensible — the connection is being dropped and every outstanding
future gets `ConnectionError` — but the frame was legitimate and complete, and an
unsolicited notification event carried in it is simply lost with no log line saying
so. Moving the overflow check above the dispatch loop, or logging how many decoded
frames were discarded, would make the drop explicit.

---

## Round 4 Fix Verification

All six round-4 findings re-derived from the current source. All six hold.

**L1 — superseded transport on the public `connection_lost` path (`_pending_direct_losses`).**
Fixed, and the counting scheme is provably safe, not merely empirically so.
`client.py:1230-1249`. Runtime: three adopt/`disconnect()` cycles on the direct-wiring
path leave `_pending_direct_losses == 0` and `_declined == 0` with **zero** spurious
reconnects and exactly three `on_disconnect` dispatches; a decline racing a real loss
(`T1` adopted, `T2` declined, `T1` dies before `T2`'s abort-loss lands) still yields
exactly one reconnect and one `on_disconnect`.

I derived the two properties that matter rather than sampling them:

- *A healthy transport is never torn down.* A forward happens only when
  `_pending_direct_losses` is exactly 1 as the loss is processed — i.e. this is the
  last outstanding loss. The live transport is the most recent adopt, so its loss has
  either already arrived (ignored as superseded) or is this one. Either way the live
  transport is dead. ∎
- *A dead live transport always triggers exactly one teardown.* `_adopt_transport`
  always `abort()`s what it declines and `disconnect()` always `close()`s what it
  drops, and asyncio guarantees `connection_lost` for both, so every increment is
  eventually matched by a decrement; the last delivery forwards. ∎

The counters can mis-*attribute* which socket a given loss belonged to (a decline
consumed in place of a real loss, or vice versa) under one narrow interleaving, but
because both properties above hold regardless of attribution, the observable outcome is
identical — at most one loop iteration later. The "deliberately non-latching" design
in the docstring is the right call.

**L2 — `aclose()` pre-seeds `pending` and cancels from `finally:`.**
Fixed (`client.py:1098-1108`). Runtime: cancelling the `aclose()` task itself one loop
iteration in leaves the outstanding handler **cancelled and done**, which is exactly
what the round-4 finding said the previous shape failed to do. The un-awaited-after-
cancel gap that remains (the trailing `gather` is skipped when `aclose` is itself
cancelled) is unavoidable without shielding and is what the docstring describes.

**L3 — `_wire_schedule_index` bounds `GET_SCHEDULE`/`DELETE_SCHEDULE`.**
Fixed (`protocol.py:609-627`). Runtime: all nine hostile shapes answer the standard
error envelope with a field-naming reason and leave the store untouched — `[1]` and
`{"a":1}` ("must be a number"), `"0"` and `true` (same — correctly, since the wire type
is an integer), `-1`/`256` ("must be between 0 and 255"), `1e400`/`NaN` ("must be a
finite number"). `None`/absent still means "no index" → "Schedule not found", per the
handler's documented contract. Nothing raises; nothing produces a traceback.

**T1 — `_on_transport_lost` early-return plus the explicit `_drop_connection()`.**
Fixed (`client.py:1251-1283`), and this is the change the brief asked me to scrutinize
hardest. Every teardown path was driven at runtime with `_schedule_reconnect` counted:

| path | reconnects | `on_disconnect` | end state |
|------|-----------:|----------------:|-----------|
| server-initiated close | 1 | 1 | transport None, `_was_connected` False |
| keepalive 3-strike give-up (`client.py:1399`) | 1 | 1 | same |
| `_send_data` write failure (`client.py:1498`) | 1 | 1 | same |
| framing overflow drop (`client.py:1581`) | 1 | 1 | same |
| adopt after `shutdown()` | 0 | 0 | transport aborted |

Exactly once on every path, never twice, never zero. I also checked the two orderings
that could have broken it: the trailing `connection_lost` that each local teardown
provokes arrives with `_was_connected` already False and is correctly ignored; and the
shim's `current is None` branch (`client.py:1785-1790`) still forwards, so the
keepalive give-up path — which is precisely the case where `_transport` is None when
the loss lands — is not silently swallowed. `_schedule_reconnect` is unreachable twice
without an intervening `disconnect()` (all three call sites call it first), so at most
one reconnect task exists at any time.

Two secondary checks on the same change: `disconnect()` skipping
`asyncio.current_task()` means the keepalive coroutine cancelling itself via
`_drop_connection` is benign (the task ends cancelled, `_on_task_done` skips it); and
`data_received` is a bare loop callback where `current_task()` returns None, so the
overflow path cancels the full `_tasks` set — the only consequence of which is T5
above.

**T2 — RX `sanitize_text` guarded by `isEnabledFor(DEBUG)`.**
Fixed on both sides and symmetrically: `client.py:1564-1565` and
`protocol.py:370-371`. The simulator's TX path got the same treatment
(`protocol.py:406-407`).

**T3 — schedule coercers shared between the library and the simulator.**
Fixed for the *parsers*. `powerpetdoor/schedule.py:49-166` now owns
`MAX_SCHEDULE_INDEX` and the five `coerce_schedule_*` helpers plus
`require_schedule_field`; `door.Schedule.from_dict` (`door.py:302-349`) and
`simulator.state.Schedule.from_dict` (`state.py:202-250`) are line-for-line
equivalent through them, and the new `coerce_schedule_flag` reads
`enabled`/`inside`/`outside` fail-closed in **both**. `door.py:1372-1384` catches the
resulting `ValueError` and drops the update with a warning, so a malformed device reply
can no longer freeze the cached list silently. The one thing the unification did **not**
cover is the emitters — see M1.

---

## Areas Reviewed With No Findings

**`FrameScanner` state across calls (`framing.py`).** The specific failure modes the
brief asked about do not occur. Desync: 60 000 randomized inputs fed in 1–4 byte chunks
produce byte-identical `frames` and `buffer` versus a single one-shot `feed()` of the
same string (0 divergences). Invariants: 20 000 multi-feed sequences hold
`_scanned == len(_buffer)` always; the retained buffer is non-empty **iff** an object is
in progress, and when it is, it always starts at that object's `{` — which is what makes
`consumed = 0` correct on resumption. Leaks: `self._buffer = buf[consumed:]` is the only
assignment and `reset()` clears buffer, `_scanned` and the `_BraceScanner` together.
Mis-accounted discards across resumptions: only the diagnostic counter drifts (T1), never
the framing. The 64 KiB cap is re-evaluated after every `feed()`, so the retained
remainder can never exceed it undetected. Connection reuse: the client resets the scanner
in `disconnect()` (`client.py:1333`), and `_adopt_transport` only ever adopts when
`_transport is None` — which is only true after a `disconnect()` — so a new connection
always starts on a clean scanner; the simulator allocates one scanner per protocol
instance and `protocol_factory` builds one per accepted connection. `_BraceScanner.depth`
cannot go negative (`scan` breaks at `depth <= 0`, and a fresh scan always starts on `{`).
Overflow *is* chunk-dependent by construction — the cap is on the retained remainder —
which is documented and acted on identically by both consumers.

**Transport identity in general.** No data from a declined or superseded transport can
reach the live frame stream: `_adopt_transport` calls `abort()` synchronously inside
`connection_made` (asyncio's `_force_close` drops the reader before any
`data_received` can be delivered for that socket), and `disconnect()` calls `close()`,
which `_remove_reader`s synchronously. `_ConnectionAttempt.data_received` gates on
`_adopted` as a second layer. The `asyncio.timeout` race in `connect()` (timeout firing
after `connection_made` ran) resolves correctly: `handle_connect_failure` tears down the
just-adopted transport and schedules one reconnect, and the shim's later
`connection_lost` is ignored via `_was_connected`.

**Message lifecycle.** `_outstanding` cannot leak — `send_message` attaches a
`done_callback` that pops the entry, `check_receipt` fails the in-flight future via
`_inflight_msg_id` after `MAX_FAILED_MSG`, and `disconnect()` fails and clears the rest
with `ConnectionError`. `_queue` is bounded by the reconnect cycle (every failure path
calls `disconnect()`, which clears it). `_tasks`/`_handler_tasks` both discard in
`_on_task_done` and neither cancels the calling task. A device response that arrives in
the same read as the FIN still resolves its future: asyncio delivers `data_received` and
the EOF-driven `connection_lost` at least one loop iteration apart, and
`process_message` resolves the future in its first step, before any `await`.

**Untrusted-input handling.** Every `SET_*` in the simulator validates before mutating
(`WireValueError` raised pre-assignment), including the notification set, which coerces
all supplied flags before assigning any. `json.dumps(...).encode("ascii")` on both sides
is safe because `ensure_ascii` defaults True, so a non-ASCII value set locally cannot
raise `UnicodeEncodeError` on the send path. `_handle_message` cannot receive a non-dict
(brace-balanced frames only parse to objects or raise) and the client guards
`isinstance(msg, dict)` anyway. `ResponseHandlerRegistry.get` rejects unhashable command
values. `state.get_tzinfo()` cannot raise out of schedule evaluation.

**Engine (`engine.py`).** `_dispatch_depth`/`_defer_sequence` still guarantee a single
`_run` owner task; `_replace_sequence` is only reachable from outside the owner;
`_retired`/`_aux_tasks` discard via done-callbacks; `stop()` cancels the deferred
`call_soon` handle and fails pending status waiters. `_hold_open` is deadline-based with
`MIN_BLOCKED_RECHECK` as a floor, not a poll interval.

**Server lifecycle (`server.py`).** `stop()` iterates a copy of `self.protocols`, and
`handle_disconnect` re-checks membership, so the `connection_lost` that `aclose()`
provokes on a later iteration cannot mutate a list already cleared. `_battery_tick`'s
carry is dropped at the 0/100 rails so it cannot offset a direction change.

**`tz_utils.py`.** Cache init is double-checked under a `threading.Lock`, all blocking
I/O is inside `to_thread`, `get_available_timezones()` returns a copy, and the POSIX
parse failure path sanitizes the device-supplied string before logging.

**Read backpressure / write-buffer limits.** Not re-litigated. Round 3
(Informational #6) and round 4 both measured a valid-command flood to be self-limiting
(RSS plateaus ~+30 MB, control channel stays responsive) and concluded that adding
`pause_reading()`/`set_write_buffer_limits()` would change simulator semantics for no
measured benefit. I agree, and L3 above is explicitly a different mechanism (CPU, driven
by chunking rather than volume) that backpressure would not address.

**Out of scope, glanced at only.** `ScriptQueue` lives in `simulator/commands/scripts.py`
— the CLI, which this persona's scope excludes. Nothing about it reaches the modules
under review.
