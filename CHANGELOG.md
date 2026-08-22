# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `powerpetdoor.framing` module: a shared, string-aware JSON frame scanner used
  by both the client and the simulator (resyncs past garbage, tolerates
  whitespace between messages, caps the un-parsed buffer at 64 KiB, never raises)
- `CommandError` exception carrying the device's `cmd` and failure `reason`
- `notification_event` listener for device notification events
  (`SENSOR_INDOOR`/`SENSOR_OUTDOOR`/`LOW_BATTERY`)
- `DoorStatus.UNKNOWN` for unrecognized firmware status strings
- Public `PowerPetDoorClient.shutdown()` / `reset_shutdown()` lifecycle API
- 23 protocol constants re-exported from the package root (documented import
  examples are now verified by tests)
- `cycle()` method to `PowerPetDoor` facade for triggering sensor-like door cycles
- Battery simulation with configurable charge/discharge rates in simulator
- Notification commands (`notify`) in simulator CLI
- Simulator: `pet` command (CLI and ctl) for pet-in-doorway simulation
- Simulator: `--control-host` and `--scripts-dir` flags, `run <script> wait`
  (exit code reflects script pass/fail), and `battery random`
- Simulator: `DoorMotionEngine` with deterministic test hooks
  (`wait_for_status()`, `add_status_listener()`, `drain()`)
- pytest-xdist for parallel test execution; hypothesis property-based fuzz suite
- `PowerPetDoorClient.aclose()`: shuts down and awaits (then cancels)
  outstanding async `on_connect`/`on_disconnect` handlers, so an embedding
  application has a clean teardown point. `PowerPetDoor.disconnect()` uses it
- Simulator: `stop` command — stops the **running script** (see Changed)
- Simulator: `status` and `list` report the script runner's state
  (`Script: none running` / `Script: running "<name>" (N queued)` /
  `Script: stopping "<name>"`); `list` also names the pending runs
- Simulator: `stop all` stops the running script *and* discards every queued
  run, reporting how many were dropped
- ctl: `run <script> wait` streams the daemon's `LOG:` lines to stderr, so CI
  sees progress and the assertion text behind a failure
- Exported timezone helpers, `PRIORITY_*`, `week_0_*` and the schedule
  utilities are documented (`docs/door.md`, `docs/client.md`); a test now
  fails if any name in `__all__` appears in no prose doc

### Changed
- Schedule serialization is now a single, explicitly-marked boundary
  (`SCHEDULE_WIRE_TO_DEVICE` / `SCHEDULE_WIRE_FROM_DEVICE` in
  `powerpetdoor/schedule.py`). The Python API is strict (`enabled: bool`,
  `days_of_week: list[bool]`), the parsers stay liberal (`true`/`"1"`/`1` all
  accepted), and each field's wire spelling is one line at the boundary.
  **The client→device and device→client directions are documented separately
  and are not required to agree**: the library sends `enabled` as a JSON
  boolean (what has run against real hardware since v0.1.0) while the device is
  observed to reply `"1"`/`"0"`. `docs/protocol.md` is reverse-engineered and
  is not authority over what the firmware accepts
- Simulator: plain `stop` now says how many runs are still queued, since its
  observable consequence is that the *next* script starts driving the door
- Simulator: a `--scripts-dir` entry that resolves outside that directory is no
  longer advertised by `list`, `--list-scripts` or tab completion, and `run`
  explains the refusal instead of answering `Unknown script: X. Available:
  ..., X, ...`
- Simulator: a `--scripts-dir` script that shadows a built-in is marked as such
  in `list`, and the shadowed built-in is dropped from tab completion
- `loop=None` now resolves the running event loop lazily at connect time; the
  documented `PowerPetDoor(host)` + `await door.connect()` pattern works
- `door.connect()` waits on an event and raises on failure instead of polling
  and returning silently; auto-reconnect refreshes cached state
- Reconnect uses exponential backoff with jitter (capped), and is cancelled by
  `stop()`/`disconnect()`
- Sensor, notification, and stats listeners all receive `(field, value)`
- The client is loop-thread-only by contract (documented);
  `run_coroutine_threadsafe()` is the cross-thread entry point
- Simulator emits the documented bare notification envelopes; unknown commands
  answer `success: "false"` with a reason
- Simulator control channel binds `127.0.0.1` by default
- ctl `--timeout` now bounds a gap in daemon traffic; `run <script> wait` has no
  deadline while the connection is alive; concurrent script runs are serialized
- Built-in YAML scripts use deterministic `wait_for` conditions instead of waits
- Simulator flags scoped to a mode are rejected by argparse instead of ignored
- Bare `battery` and `holdtime` show the current value instead of mutating it
- Dropped the `async-timeout` dependency in favor of stdlib `asyncio.timeout`
- `Schedule.to_dict()` and `schedule_template` emit `enabled` as the wire's
  `"1"`/`"0"` string, matching `docs/protocol.md` and the simulator. Readers on
  both sides already accepted either spelling
- `GET_SCHEDULE_LIST` returns slot indices sorted, and
  `PowerPetDoor.refresh_schedules()` sorts the list it caches, so the public
  `door.schedules` order no longer depends on which code path last touched it
- Simulator: `list` prints the `--scripts-dir` header even when the directory
  is empty, and names queued runs instead of raw references
- Simulator: `stop all` is idempotent (succeeds with nothing running or
  queued), and a bare `stop` with runs pending reports the depth instead of
  "No script is running"
- ctl: the `run` help text and epilog describe what the control channel
  actually accepts (bare names only; a failed *load* still exits 1)
- Schedule `days_of_week` now uses list format `[Sun, Mon, Tue, Wed, Thu, Fri, Sat]` instead of bitmask
- CLI alias 'y' now used for cycle (previously 'c' conflicted with close)
- Removed duplicate 'f' alias from run command (kept 'r' and 'file')
- **Breaking (simulator CLI/ctl)**: `stop` is no longer an alias for
  `shutdown`. It now stops the *running script*; use `shutdown` (or, in the
  CLI, `exit`/`q`/`quit`) to stop the simulator
- Bare `ac` now answers "AC set to connected/disconnected" so it does not read
  like the read-only `battery`/`holdtime` displays
- Command failures no longer double their prefix (`ERROR: Error: ...`)
- "Unknown built-in script: X" is now "Unknown script: X" — the `Available:`
  list it prints includes `--scripts-dir` names too
- Simulator status/progress output is flushed, so the startup banner and script
  progress appear in redirected output (and survive SIGTERM)
- A nonexistent `--scripts-dir` is now a startup error; an empty one warns
- ctl `--help` describes `--timeout`'s silence-gap semantics, and its epilog
  now shows `run <script> wait` (the only exit-code-bearing form) and `stop`
- ctl output is line-buffered off a terminal, so `ctl -i > log`, `| tee` and
  container/supervisor capture see streamed daemon logs live instead of in one
  burst at the next prompt
- ctl no longer tab-completes local YAML files or directories for `run`: the
  daemon refuses script paths, so every one of those completions was a command
  guaranteed to fail
- `--list-scripts` and the `list` command now print the same `Built-in
  scripts:` header
- `stop` with nothing running points at `shutdown`; a repeat `stop` answers
  `Stop already requested for: <name>` rather than a fresh success
- A plain `wait N` script step is now interruptible by `stop`
- Schedule coercion helpers moved into `powerpetdoor.schedule` and are shared
  by the library's and the simulator's `Schedule.from_dict()`, so hardening
  either one hardens both
- Packaging: replaced the `OS Independent` classifier with explicit
  Linux/macOS classifiers — the simulator's plain-stdin prompt fallback uses
  `loop.add_reader()`, which Windows' ProactorEventLoop does not implement

### Fixed
- The 64 KiB framing cap bounds memory again, not just a character count: the
  retained remainder is coalesced once it grows past `MAX_RETAINED_PIECES`
  pieces (measured: 512 KiB/connection → 67 KiB at 1-byte chunks, for under 4%
  of the framing throughput)
- `GET_HW_INFO`: a non-mapping `fwInfo` is no longer handed to the dict-typed
  `hw_info_update` listeners, and `PowerPetDoor` no longer caches it — a scalar
  there made three documented public properties raise `AttributeError` with
  nothing in the log naming the frame that caused it. The value still resolves
  the caller's future unchanged
- `PowerPetDoor.refresh_schedules()` rejects a non-list `GET_SCHEDULE_LIST`
  payload instead of raising `TypeError` out of a documented coroutine, or
  issuing one `GET_SCHEDULE` per character of a string
- `compute_schedule_diff()` validates every index it reads out of the
  caller-supplied device entries, so a hostile index can no longer raise
  `TypeError` out of a public helper or produce a `SET_SCHEDULE` payload with
  `"index": null`; two brand-new entries can no longer be assigned the same slot
- Simulator scripts: `enabled: "0"` / `hold: "off"` are read as flags rather
  than by truthiness, so a quoted false no longer produces an *enabled* schedule
- Receive framing: garbage bytes raised `IndexError`, a brace inside a JSON
  string corrupted framing, and either could wedge the connection permanently
- A single undecodable byte no longer discards the chunk or desyncs framing
- `PowerPetDoor` cached `power: "0"` as powered-on (`bool("0")` is `True`)
- `connect()` while already connected leaked a second live TCP connection that
  survived `disconnect()`
- Messages dropped after retries left their futures pending forever
- `disconnect()` raised `KeyError` from future done-callbacks
- Simulator: a status listener calling back into the engine could spawn a
  duplicate motion sequence and double-count cycles
- Simulator: battery charge/discharge with fractional per-tick rates stalled or
  ran at double speed
- Simulator: `SET_NOTIFICATIONS` ignored the documented payload format;
  `DELETE_SCHEDULE` did not echo the deleted index
- Simulator: script loader errors surfaced as raw YAML/OS exceptions
- `shutdown()`/`stop()` during an in-flight `connect()` left a live,
  keepalive-pinging connection that nothing ever closed
- A connection the client declined (a second transport, or one completing
  after shutdown) delivered `connection_lost` into the live connection's
  teardown path, closing a perfectly healthy connection
- A stale `connection_lost` after `disconnect()`+`connect()` logged a bogus
  ERROR and burned a reconnect attempt. The local-failure paths (keepalive
  give-up, write failure, framing overflow) now schedule their reconnect
  explicitly instead of relying on that event
- A superseded transport's `connection_lost` tore down the healthy connection
  when `PowerPetDoorClient` was wired into `create_connection()` directly (the
  per-attempt shim was already guarded)
- `aclose()` cancelled while waiting on its handlers left them running,
  un-awaited and un-cancelled — the exact guarantee it exists to make
- `PowerPetDoor.get_schedule()`/`refresh_schedules()`/schedule updates raised
  `TypeError`/`AttributeError` on a malformed device payload instead of a
  handled `ValueError`; a malformed schedule update silently froze the cached
  schedule list
- Simulator: `stop` issued during a script's **final** step was discarded, and
  the run reported `Script PASSED` with exit code 0
- Simulator: the `(N queued)` indicator under-reported by one, so a single
  script waiting behind a `run ... wait` displayed as nothing pending
- Simulator: the `>>> Client disconnected, stopping scripts` progress line was
  not flushed, so a redirected `--wait-for-client` log simply stopped
- Simulator: the operator's `schedule add` could allocate index 256, past the
  bound the wire path enforces
- `generate_gaps_report.py` truncated any `# pragma:` reason containing
  parentheses, and did not scan `scripts/`
- `connect()` escaped with `UnicodeEncodeError`/`OverflowError` for an invalid
  host or port instead of logging and scheduling a reconnect
- Simulator: a status listener commanding the door with `hold_time` ~0 replayed
  a stale start state, re-broadcasting a status the door had moved past
- Simulator: one dead ctl client made the daemon's log broadcast feed itself,
  flooding every other session with hundreds of `socket.send() raised
  exception.` lines
- `ppd-simulator-ctl` printed a traceback on Ctrl-C instead of exiting 130
- `compute_schedule_diff()` mutated its input; `compress_schedule()` now
  validates entries instead of raising `KeyError` on sparse input
- POSIX timezone strings with angle-bracket abbreviations (`<+05>-5`) now parse
- Schedule representation to match Power Pet Door protocol format
- Battery charge simulation test reliability
- Simulator: `stop all` left the one run the consumer had already claimed, so
  clearing a running script plus N queued runs took two commands and the
  "dropped" script started running in between
- Simulator: `SET_SCHEDULE_LIST` wiped every schedule when the `schedules`
  field was absent, and reported success when it was the wrong type
- `schedule_entry_content_key()` compared `daysOfWeek` raw, so the firmware
  variant that sends `"1"`/`"0"` day flags made every entry look changed and
  turned each incremental sync into a full `SET_SCHEDULE` sweep
- `coerce_schedule_days()` read a negative legacy bitmask as "active every
  day"; out-of-range masks are now rejected
- A complete frame delivered in the same read as a buffer overflow was
  dispatched and then cancelled without a word; the drop is now reported
- Simulator: a normal one-shot ctl hang-up was logged as
  `[ERROR] Control client error: [Errno 32] Broken pipe` and broadcast to
  every other ctl session
- `InteractiveSession.format_output` was dead code, leaving the two live
  history-recall echoes as the only unsanitized terminal writes; replaced by
  `format_recall`, which both call sites use

### Security
- Bounded per-frame dispatch on both client and simulator: one `asyncio.Task`
  used to be created per framed message, synchronously, per read — one 256 KiB
  read of `{}` admitted 131,072 live tasks (~165 MB measured) before any of them
  ran. Dispatch now runs at most `MAX_INFLIGHT_FRAMES` handlers at a time and
  pauses the transport while the backlog drains (measured: 131,072 tasks /
  165.5 MB → 64 tasks / 6.5 MB; a `{"cmd":"a"}` flood that made `ctl status`
  time out at 15 s is now answered in 12 ms)
- The simulator's door transport got the write-buffer ceiling the control
  channel already had: a client that issues valid commands and never reads the
  answers is dropped instead of growing the daemon's heap without bound
  (measured: +18.1 MB → +2.2 MB for 3.2 MB of requests)
- Throttled the four per-*frame* log sites (malformed frame and malformed
  message on the client; JSON parse error and unknown command on the
  simulator). These fire at the peer's *byte* rate rather than its packet rate,
  so one 64 KiB write of `{x}` bought ×64 write amplification in a third
  party's log; now ×0.05. The echoed frame is also length-bounded
- `EventThrottle` gained an elapsed-time floor and an interval ceiling, so a
  *new* burst of events on a long-lived connection is always reported promptly
  instead of being swallowed by a threshold the connection passed hours ago
- Capped the receive buffer on both client and simulator (unbounded growth was a
  memory-exhaustion vector for a malicious peer)
- The simulator control channel is unauthenticated, so it now binds loopback by
  default and requires an explicit opt-in flag to widen
- Control-channel `run` accepts only bare script names resolved inside the
  configured script directories (path traversal)
- Control characters in network-derived data are escaped before reaching logs,
  broadcasts, or a terminal (ANSI escape injection, forged log lines)
- `Schedule.from_dict()` validates and bounds wire-supplied schedule data;
  day flags are read as flags (`"0"` means off) and a selected sensor's time
  window is required rather than defaulted to a permissive 06:00-22:00
- Every simulator `SET_*` command validates its wire value **before** storing
  it. `SET_HOLD_TIME` used to accept `Infinity`/`NaN` and `SET_TIMEZONE` any
  JSON type, either of which permanently broke `GET_SETTINGS` for every client
  (and wedged the door open) from a single unauthenticated packet
- The shipped library no longer writes raw device bytes into log records: one
  shared sanitizer (`powerpetdoor.sanitize`) now covers `client.py`,
  `schedule.py`, `tz_utils.py`, the simulator and both front ends
- The interactive simulator CLI sanitizes its own command output, which can
  carry network-poisoned state (e.g. a wire-set timezone)
- Framing now carries its scanner state across reads instead of re-scanning the
  retained buffer, removing a ~1000x CPU amplification: a peer dribbling one
  never-terminated JSON object cost O(N^2) work, enough for a ~750 byte/s
  trickle to pin a core in the host application's event loop (and in the
  simulator's unauthenticated door port)
- Untrusted values used as dict keys are guarded at the remaining two sites —
  the client's response `CMD` and the simulator's `GET_SCHEDULE`/
  `DELETE_SCHEDULE` `index` — so one malformed frame no longer produces a full
  traceback at ERROR (14x log write-amplification from an unauthenticated port)
- The YAML script channel is held to the same bounds as the wire: `set
  hold_time inf` (which permanently broke `GET_SETTINGS`) and out-of-range
  battery/index values are rejected, and script names, descriptions, step
  parameters and `log` messages are sanitized before they reach a terminal
- The sanitizing log formatter is installed on the `--script` (headless/CI) and
  `--daemon` paths too, not only the interactive ones
- CI and release workflows install from the committed, hashed `uv.lock`
  (`uv sync --locked`), and the build backend is pinned exactly
- Simulator history files are created with `0600` permissions
- All CI actions pinned to full commit SHAs
- Peer-driven log notices are aggregated per connection instead of once per
  read. A peer sending one byte per TCP segment bought one log line per byte —
  x247 write amplification with no self-limiting, in the shipped library and in
  the simulator, and 91% of a core once the control channel fanned the records
  out. Garbage discards and non-ASCII notices now report on a doubling
  schedule, so log volume is logarithmic in the peer's traffic
- Response handlers read their payload field defensively, so a legal envelope
  missing one field takes the existing "Response missing expected field" path
  instead of raising a `KeyError`/`TypeError` into a full ERROR traceback
- `_ControlLogHandler` drops records for a control client whose write buffer
  has run away, capping the daemon memory a parked `ctl -i` session can cost
- `FrameScanner` no longer re-copies its retained remainder onto every chunk
  (~590x CPU amplification at 1-byte chunks), and its discard counter is
  chunk-invariant
- The last unbounded script numerics (`inside`/`outside` `duration`, `wait`
  `seconds`, `wait_for` `timeout`) are bounded and finite-checked: `.nan`
  reached `asyncio.sleep`, raised inside a background task, and left the step
  silently skipped while the run still reported PASSED
- Dependabot now covers the composite action directory as well as the root;
  the pins no automation can reach are listed in `.claude/CLAUDE.md`

## [0.3.0] - 2025-12-27

### Added
- `PowerPetDoor` high-level facade class with cached state and callbacks
- `DoorStatus` enum for type-safe door state representation
- `NotificationSettings`, `BatteryInfo`, `Schedule`, `ScheduleTime` dataclasses
- Callback registration for status changes, settings changes, and connection events
- Comprehensive documentation for high-level API (`docs/door.md`)
- Properties for all door state (status, sensors, power, battery, etc.)
- Async methods for door control (open, close, toggle, open_and_hold)
- Sensor control methods (set_inside_sensor, set_outside_sensor)
- Safety feature controls (set_safety_lock, set_autoretract)
- Schedule management (get_schedule, set_schedule, delete_schedule)

### Fixed
- Flaky door tests in CI environment

## [0.2.0] - 2025-12-27

### Added
- Door simulator submodule for testing without hardware
- Multi-client connection support in simulator
- Simulator CLI (`ppd-simulator`) with interactive commands
- Simulator control CLI (`ppd-simulator-ctl`) for programmatic control
- Script-based testing with YAML scenario files
- Comprehensive simulator tests

### Changed
- Refactored client architecture for better separation of concerns
- Improved protocol message handling

## [0.1.0] - 2025-12-26

### Added
- Initial release of pypowerpetdoor
- `PowerPetDoorClient` for low-level TCP communication with Power Pet Door
- JSON-based command/response protocol implementation
- Door control commands (OPEN, CLOSE, OPEN_AND_HOLD)
- Settings management (power, sensors, auto mode, safety features)
- Battery and hardware information retrieval
- Schedule configuration support
- Keepalive and automatic reconnection
- Async/await interface using asyncio
- Support for Python 3.11, 3.12, 3.13, and 3.14

[Unreleased]: https://github.com/corporategoth/py-powerpetdoor/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/corporategoth/py-powerpetdoor/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/corporategoth/py-powerpetdoor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/corporategoth/py-powerpetdoor/releases/tag/v0.1.0
