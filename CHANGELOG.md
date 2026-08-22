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
  (`Script: running "<name>" (N queued)`)
- ctl: `run <script> wait` streams the daemon's `LOG:` lines to stderr, so CI
  sees progress and the assertion text behind a failure

### Changed
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
- ctl `--help` describes `--timeout`'s silence-gap semantics
- Packaging: replaced the `OS Independent` classifier with explicit
  Linux/macOS classifiers — the simulator's plain-stdin prompt fallback uses
  `loop.add_reader()`, which Windows' ProactorEventLoop does not implement

### Fixed
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
  ERROR and burned a reconnect attempt
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

### Security
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
- CI and release workflows install from the committed, hashed `uv.lock`
  (`uv sync --locked`), and the build backend is pinned exactly
- Simulator history files are created with `0600` permissions
- All CI actions pinned to full commit SHAs

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
