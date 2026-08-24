# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.2] - 2026-08-24

Everything below was **measured against a real Power Pet Door** (firmware
1.7.18) rather than inferred from `docs/protocol.md` or from this library's
own simulator. Several long-standing beliefs turned out to be wrong, and the
simulator was wrong in the same way, so no test could have caught them.

### Fixed — schedule semantics

The schedule engine is exactly:

```
active iff start <= now < end      (24:00 is a legal end, meaning 1440)
if end <= start the entry is EMPTY and never fires
```

- **A window does NOT cross midnight.** `23:00-01:00` is stored perfectly and
  never fires — not by wrapping within the day, and not by spilling into the
  next. Measured both ways: a `23:00-21:30` entry leaves the sensor disabled
  on the day it names *and* on the day after. Overnight access needs two
  entries. This library previously reported such a window as active for
  eight hours a night that the door was refusing.
- **`start == end` is an EMPTY window, not a whole day.** `16:01-16:01` and
  `21:01-21:01` both leave the sensor disabled. A whole day is `00:00-24:00`.
- **Hour 24 is honoured and preserved.** Write `00:00-24:00` and the door
  reads back `00:00-24:00` unchanged, so end-of-day now has an unambiguous
  spelling and it is what this library emits.
- **`00:00` as an END is rewritten to `24:00` on the send path.** Midnight
  closing a window is the day's last minute; the device does not reinterpret
  it, so `20:00-00:00` was stored faithfully and never fired. The rule is
  positional — a `00:00` *start* is untouched, and so is the all-zero filler
  block of the sensor an entry is not about.
- **`23:59` is no longer special-cased** as end-of-day. That was an inference
  from the factory schedule, and an unnecessary one now that `24:00` works.
  A window ending at `23:59` really does stop one minute short, which is what
  it says.
- `set_schedule()` now **refuses a window that covers no time** (`end <=
  start` after normalisation). The door accepts such an entry, echoes it back
  unchanged and silently never acts on it, so nothing downstream could catch
  it — the schedule reads correctly and simply does not work.

### Fixed — door status

- **`DOOR_CLOSING` added.** Closing has THREE states, not two:
  `DOOR_HOLDING/KEEPUP -> DOOR_CLOSING -> DOOR_CLOSING_TOP_OPEN ->
  DOOR_CLOSING_MID_OPEN -> DOOR_CLOSED`. The first was missing entirely, so
  **every close on a real door produced `DoorStatus.UNKNOWN`** — neither open
  nor closed, `is_closing` false, `position` 0 — plus a logged warning. A
  consumer had no way to render it. Measured on both closing paths: after a
  timed hold, and after an explicit close from `KEEPUP`.
- `DoorStatus.CLOSING` reports `position` 100 and `is_closing` True: the
  motor has started while the flap is still up.

### Changed — simulator

- Emits `DOOR_CLOSING`, with `DoorTimingConfig.closing_start_time` so tests
  can compress it like every other phase. Its absence is why no test caught
  the missing state.
- `trigger_sensor` treats `DOOR_CLOSING` as a closing state, so a pet
  arriving as the motor starts retracts the door instead of falling through
  to the "door is closed, open it" path.
- Auto-retract fires from `DOOR_CLOSING`, returning to holding without the
  flap travelling; reversing an open from it returns to `HOLDING`/`KEEPUP`.
- `is_sensor_allowed` corrected to the measured rule above.
- `WHOLE_DAY_END_HOUR` / `WHOLE_DAY_END_MINUTE` (24:00) added, and the
  built-in scripts' "always" window uses them.

### Added

- `END_OF_DAY`, `MIDNIGHT`, `normalise_window_end`,
  `schedule_window_is_empty`, `window_minutes`, `DOOR_STATE_CLOSING`.
- `Schedule.validate_for_send()` and `Schedule.window_is_empty()`.

### Documented

- **A hazard, measured:** the schedule engine writes its verdict through to
  the `inside`/`outside` sensor flags, and turning `timersEnabled` off does
  **not** restore them. A schedule that never fires can leave a door's
  sensors disabled permanently, even after schedules are switched off. Any
  client that writes schedules should be prepared to re-enable them.
- `docs/protocol.md`'s status table and schedule section are now `**[V]**`
  rather than inferred, and carry the probe results.

## [0.4.1] - 2026-08-23

### Changed — breaking

- `PowerPetDoor.open()` now sends `OPEN_AND_HOLD` instead of `OPEN`, and
  `PowerPetDoor.cycle()` now sends `OPEN` instead of being an alias for
  `open()`. `PowerPetDoor.open_and_hold()` is **removed**; call `open()`.

  The wire is unchanged - both commands existed and both still do. What was
  wrong was which Python name each one answered to. `OPEN` is the *timed*
  open: the door rises, holds for `hold_time` and closes itself, which is
  what a pet triggering a sensor gets. Binding that to `open()` meant the
  library's plainest verb opened the door and then shut it again with no
  second command, so every caller that wanted "open" had to know not to call
  `open()`. Both downstream consumers had independently worked around it:
  `ha-powerpetdoor` called `open_and_hold()` from both its cover entity and
  its Open button, each with a comment explaining why, and the Ostinato
  plugin's facade did the same. When every caller has to route around a
  name, the name is the defect.

  Now `open()` opens and stays open, `close()` closes, and `cycle()` is the
  timed open under the name that already described it. `toggle()` is
  unchanged in signature and opens through `open()`, so a door toggled open
  no longer closes itself.

  Migration: `door.open_and_hold()` -> `door.open()`, and any existing call
  to `door.open()` that *wanted* the auto-closing behaviour -> `door.cycle()`.

- Simulator CLI: the `hold` command is now named `open`, with `hold` and `h`
  kept as aliases - every spelling that worked before still works, including
  the old `open` alias, which now resolves to the command it names.
- Simulator scripts: the `open` action opens and holds and no longer takes a
  `hold` parameter; the new `cycle` action is the timed open. A script step
  of `- action: open` with `hold: true` becomes plain `- action: open`, and
  one with `hold: false` becomes `- action: cycle`. Passing `hold` to `open`
  is now a validation error rather than a silent change of meaning.

### Fixed
- `powerpetdoor.__version__` reported `0.3.0` in the released 0.4.0 package.
  The wheel is named from `pyproject.toml`, so the artifact was correctly
  labelled while the package it contained misreported itself to anyone who
  asked. PyPI will not accept a re-upload of a filename it has already
  seen, so 0.4.0 cannot be replaced in place: 0.4.1 is the fix, and 0.4.0
  should be yanked.
- README's "Library Structure" tree was missing `i18n.py`, `locales/`,
  `__init__.py` and `py.typed` - the whole translation subsystem was
  undocumented in the one place a contributor looks for the layout.

### Added
- `TestTheVersionGate` - `__version__` must equal `pyproject.toml`'s version,
  and must never fall behind the newest `v*` git tag. Both directions of the
  drift that produced the bug above are now caught.
- `TestReadmeLibraryTreeMatchesTheSource` - every top-level module in
  `src/powerpetdoor/` must appear in the README tree, and the tree must not
  list modules that are gone. Deliberately shallow: `simulator/` internals
  are summarised on purpose and are not policed.

## [0.4.0] - 2026-08-23

### Added — internationalization

All user-facing text now goes through `powerpetdoor.i18n.t()`: 350 strings
across the simulator CLI, exception messages and log messages. English is
the second argument at every call site, so it lives in the source and there
is no `en_us.json` for it to drift out of step with — a key with no
translation renders its default, which is why adding a language cannot break
anything. Output with no locale selected is byte-identical to before.

Protocol text is deliberately *not* translatable: everything in `const.py`
goes to a device whose firmware cannot be changed, so it is not user-facing
text at all.

- `t()` never raises. A missing locale directory, corrupt JSON, a file that
  is not an object, or a translation whose placeholders do not match its
  call site each fall back to English rather than raising from underneath
  whatever the caller was trying to report
- It does not format unless given keyword arguments, so the `%`-style
  placeholders that the logging module applies later survive untouched
- `POWERPETDOOR_LOCALE` selects a language, falling back to `LC_ALL`,
  `LC_MESSAGES` then `LANG`; `C`/`POSIX` mean English. An unknown locale
  renders English rather than failing
- Locale files live in `src/powerpetdoor/locales/` and are declared as
  package data, so they reach a wheel. See `docs/translations.md`
- Locale files carry a `.po`-style header: `_language` (the language's own
  name), `_translators` (attribution, string or list) and `_updated`. Header
  values may be lists where a translation may not; `get_locale_metadata()`,
  `get_translators()` and `get_locale_name()` read them
- `scripts/check_translations.py` audits orphaned entries, missing
  translations, key-collision artifacts, a stale catalogue and any
  user-facing string not wrapped at all. CI and the pre-commit hooks fail on
  everything except *missing*, since a locale legitimately lags the source
- `--init-locale de_de` writes a ready-to-translate file, header stub and
  all. `--locate PATTERN` and `--locations` answer *where is this string
  used*, which is what a translator needs to word it — "Closed" differs for
  a door, a connection and a schedule window. `--json` makes that pipeable
- `messages.json` is committed (its diff shows a reworded string in review)
  and a pre-commit hook regenerates it so it cannot drift. Source locations
  are deliberately *not* committed: line numbers move on every unrelated
  edit, so a stored copy would be stale immediately

### Added — developer environment and dependency automation

- `scripts/setup-dev.sh` is the single entry point for a fresh clone: it
  syncs the extras, installs both git hook stages and reports dependency
  freshness. Hooks were the one thing a clone never got
- `.pre-commit-config.yaml` splits by stage. Commit time holds what finishes
  in seconds (ruff, mypy, translations, the 2887-test fast slice); push time
  holds the real gate (full suite at 100% line+branch, dependency freshness)
- `scripts/check_dependencies.py` reports lockfile drift, available
  upgrades, stale CI action pins and published advisories. Dependabot cannot
  do this job here: the GitHub remote is a push-mirror, so its PRs target a
  branch the next mirror sync overwrites
- `.envrc` is tracked, so `direnv allow` activates `.venv` on a fresh clone

### Fixed — CI

- `actions/checkout` v4 → v7.0.1, `astral-sh/setup-uv` v7 → v10.0.1 and
  `codecov/codecov-action` v4 → v7.0.0. The stale majors were what produced
  ten Node-version deprecation warnings on every GitHub run
- The `TESTING_GAPS.md` bot commit now only runs on the forge that is the
  source of truth. On the GitHub mirror it could never push, and produced a
  failed-step annotation on every run
- `test_notification_state_log_is_sanitized` gathered *all* of
  `client._tasks`, which includes the keepalive loop, so it awaited that
  loop's first 30 s sleep. It was 30 s of the suite's 39 s wall time and sat
  one slow runner away from the 60 s per-test timeout

### Fixed — proven against a real door (firmware 1.7.18)

A physical Power Pet Door was probed for the first time. It disproved a
number of long-held assumptions that `docs/protocol.md` had recorded as
fact, and exposed **four** ways in which this library did not work against
real hardware. None of these were catchable by the test suite before,
because the simulator implemented the same wrong assumptions.

- **`PowerPetDoor.set_notifications()` never worked against a real door.**
  The device requires a *nested* `notifications` object carrying **all
  five** flags as **JSON booleans**. It answers a flat, top-level payload —
  which is what this library sent, and what `docs/protocol.md` documented —
  with `success: "false"` and writes nothing. Worse, a nested payload whose
  values are *strings* is answered with a normal success envelope carrying
  the **current** settings and is silently not applied. The shape now lives
  in one place, `build_set_notifications_message()`
- **`PowerPetDoor.set_schedule()` never worked against a real door.** The
  device requires the slot `index` as a **sibling** of the `schedule`
  object; a message carrying only `schedule` is answered `success: "false"`
  and writes nothing, however the entry is spelled. The shape now lives in
  `build_set_schedule_message()`
- **Every individual setting command was sent under the wrong envelope
  key.** `{"cmd": "ENABLE_INSIDE"}` is answered `success: "false"` by a real
  door; only `OPEN`, `OPEN_AND_HOLD` and `CLOSE` are accepted as a `cmd`.
  `set_inside_sensor()`, `set_outside_sensor()`, `set_power()`,
  `set_auto()`, `set_safety_lock()`, `set_autoretract()` and
  `set_pet_proximity_keep_open()` all sent `cmd` and are now `config`,
  routed through the new `envelope_for_command()`
- **`doorOptions` is an integer bitfield, not a flag.** Auto-retract is
  **bit 1** (`DOOR_OPTION_AUTORETRACT`, value `2`); the field was read by
  truthiness, which happened to be right only because `2` is truthy and
  would have misreported the moment any other (still unidentified) bit was
  set. It is now read through `autoretract_from_door_options()`
- The remote-pairing replies carry **`has_id`** and **`has_key`**, not the
  `hasRemoteId`/`hasRemoteKey` this project guessed at, so
  `remote_id_update`/`remote_key_update` listeners never fired
- Both sensor-trigger-voltage setters take a field named **`voltage`**, and
  reject the `sensorTriggerVoltage`/`sleepSensorTriggerVoltage` name their
  own getters answer with. Owned by `build_set_voltage_message()`

### Added — hardware-verified simulator fidelity

The simulator's job is to be wrong in the same ways a real door is wrong, so
a client that gets a wire shape subtly wrong fails in tests rather than only
in the field. Each item below is emulated deliberately and pinned by a test:

- **Failure responses carry no `msgID`**, and neither does a `PONG`. A
  client matching replies by id cannot pair a failure with its request;
  `PowerPetDoorClient` now fails the in-flight command instead of waiting
  out its timeout
- **`cmd` vs `config` is enforced**: every non-motion command is rejected
  under `cmd` and accepted under `config`
- **`SET_NOTIFICATIONS` reproduces both failure modes**, including the
  accepted-and-silently-ignored string payload
- **`SET_SCHEDULE` without a sibling `index` is rejected**
- **The voltage setters require `voltage`** and reject the getters' names
- **Per-command value spellings are reproduced field by field** rather than
  normalized: `"true"`/`"false"` strings in `GET_SETTINGS`,
  `GET_NOTIFICATIONS` and `GET_DOOR_BATTERY`; ints in `GET_SENSORS`, the
  individual setting replies, `doorOptions` and schedule entries; an
  all-integer `fwInfo`; POSIX (never IANA) for `tz`
- **`GET_TIMERS_ENABLED`, `GET_AUTORETRACT`, `GET_CMD_LOCKOUT`,
  `GET_OUTSIDE_SENSOR_SAFETY_LOCK` and `CHECK_RESET_REASON` are rejected** —
  firmware 1.7.18 does not implement them. Their state is read from
  `GET_SETTINGS` instead. The constants are kept, since another firmware
  revision may have them
- **`SET_TIME` is answered with silence**, not a failure envelope — the one
  command where "no error came back" does not mean success
- **23:59 is end-of-day**: the device spells a full-day window
  `00:00`–`23:59`, and its final minute is inside the window
- **Deliberate divergence, documented in `docs/simulator.md`:** the
  simulator keeps multi-client support. A real door is single-connection and
  degrades into unexplained timeouts when a second client attaches; the
  simulator instead serves every client as if exclusive, because the CLI and
  `ppd-simulator-ctl` attach alongside the client under test

### Added — new API surface

- `envelope_for_command()` and `COMMAND_ENVELOPE_COMMANDS`: the single place
  that decides whether a command is a `cmd` or a `config`
- `build_set_notifications_message()`, `build_set_schedule_message()`,
  `build_set_voltage_message()`, `build_set_hold_time_message()`: every
  easy-to-get-wrong wire shape, built in one importable place so the
  message level reaches it as well as `PowerPetDoor`
- `autoretract_from_door_options()` and `DOOR_OPTION_AUTORETRACT`
- `PowerPetDoor.sensor_trigger_voltage` / `sleep_sensor_trigger_voltage`
  properties and setters — readable and settable on the device, but absent
  from the facade until now
- `PowerPetDoor.has_remote_id` / `has_remote_key` and
  `refresh_remote_info()`
- `CMD_GET_TIME`, `FIELD_TIME`, `TIME_FORMAT`, the client's `time_update`
  listener, and `PowerPetDoor.device_time` / `refresh_time()`: the door's
  own wall clock, undocumented by the vendor. Schedules are evaluated
  against it, so it is the only way to check that a door will fire a
  schedule when you expect it to. **The clock is read-only**

### Added
- `tests/test_wire_constants.py`: every constant in `powerpetdoor.const` whose
  value appears in `docs/protocol.md` is pinned by literal.
  Both sides of this project read the same symbol, so `CMD_OPEN`,
  `FIELD_CMD_LOCKOUT` and `FIELD_AUTO` could all be renamed or re-spelled with
  the whole suite green. The perimeter is derived from the document, not
  hand-listed, so a newly documented constant has to be added
- `powerpetdoor.sanitize.sanitize_field()`: `sanitize_text` plus LF, for a sink
  that renders one device-supplied field value into a log record
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
- Simulator scripts: `note:`, `comment:` and `description:` are accepted as
  documentation-only annotations on **any** step, and are read by nothing. They
  give annotated scripts a spelling that is guaranteed not to collide with a
  parameter name, now that an unrecognised parameter is an error (see Changed)
- `py.typed` (PEP 561): the library's annotations are now visible to downstream
  type checkers. Without the marker every one of the 121 exported names was
  `Any` to a consumer — `await door.set_hold_time("banana")` was silently
  accepted by mypy in a downstream repo
- A `MANIFEST.in`, so the sdist ships the whole test suite (`conftest.py`,
  `tests/__init__.py`, `tests/fuzz/`, `tests/simulator/`, `docs/`) rather than
  9 loose modules and none of the machinery to run them. `pytest` in the
  unpacked sdist now runs the same suite CI does
- CI: a `packaging` job that builds the artifacts and asserts both — the wheel
  carries `py.typed`, and the unpacked sdist's suite actually runs
- `docs/door.md`: a "Behaviour While Disconnected" section, and a warning on
  the `PowerPetDoor` class docstring. A command issued while disconnected is
  **queued and executes on the next connection** — on a physical pet door that
  needs to be conspicuous. The behaviour itself is unchanged and deliberate

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

#### Breaking (simulator CLI)
- `ppd-simulator --script … --oneshot` interrupted by Ctrl-C now exits **130**
  and prints `>>> Interrupted after N of M script(s)` instead of exiting **0**
  with `>>> All scripts PASSED`. `--oneshot` that produced no verdict at all is
  now exit 1 rather than 0. *Remedy:* none needed for an uninterrupted run;
  a wrapper that treated 0-on-interrupt as success was reading a false green
- `ppd-simulator --daemon` interrupted by Ctrl-C now exits 130 (was 0), matching
  `ppd-simulator-ctl`
- A misspelled **top-level** script key is now a load error
  (`Unknown top-level key(s): stpes. Use: description, name, steps`). `stpes:`
  used to produce a zero-step script that reported `PASSED` and exited 0 — the
  whole file silently became a no-op that still reported success.
  *Remedy:* fix the spelling; `steps: []` is still legal and still means
  "no steps"
- `ppd-simulator-ctl --timeout` must be greater than 0, and an empty or
  whitespace-only command is refused with a usage line at rc 2 instead of
  hanging for the full timeout. *Remedy:* `run <script> wait` is the spelling
  for "wait as long as it takes"
- `ppd-simulator --run-for` must be greater than 0 (`--run-for -5` used to be
  accepted and mean "shut down immediately")
- `--port`, `--daemon PORT`, `--host` and `--control-host` are validated in the
  parser: out of range or unresolvable now exits 2 with a usage line instead of
  an `OverflowError`/`gaierror` traceback

#### Breaking (simulator script DSL)

Both of these used to be silently ignored, which is what made them worth
failing on: the progress log echoed the mistake back as if it had been
accepted, and the run still exited 0.

- **An unrecognised step parameter now fails the step.** `wait: {duration: 8}`
  (`duration` is a real parameter name in this DSL — for `inside`/`outside`)
  used to log `Step 1: wait(duration=8)`, wait the 1.0 s default instead of 8,
  and report PASSED. *Remedy:* remove the unrecognised key, or — if it was a
  human annotation rather than a typo — respell it as `note:`, `comment:` or
  `description:`, which every step now accepts and nothing reads
- **An unrecognised `sensor:` name now fails the step.** The engine's gates are
  `if sensor == "inside"` / `elif sensor == "outside"`, so a one-character typo
  matched neither, skipped the enable flags, the safety lock *and* the
  schedule, and opened the door anyway — still reporting PASSED, which over
  `ctl run <name> wait` is a green CI exit code. *Remedy:* use `inside` or
  `outside`

#### Other simulator changes

- `--list-scripts` marks a shadowed built-in the way `list` already did, and
  the marker now names the real file with its `.yaml`/`.yml` suffix
  (`(shadowed by /path/to/basic_cycle.yaml)`) instead of a reconstructed
  `<dir>/<name>` that looked like a path but was not one
- The out-of-directory refusal is policy-aware: over the control channel it
  ends `move it into the directory (paths are not accepted over the control
  channel)` rather than pointing at a form the daemon refuses
- `holdtime`, `inside`, `outside`, `charge_rate` and `discharge_rate` reject
  `nan`/`inf` at the CLI, before anything is written
- One rendering of a schedule's sensor scope across `add`, `list` and the
  implicit line (`inside and outside sensors`, not `both sensor`)
- Every "unknown name" error in the script DSL now names what it *would* have
  accepted, and says so when the name belongs to the other action:
  `Unknown assertion condition: door_closed (that name belongs to the
  'wait_for' action). Use: ...`. `Use: none` for a parameterless action now
  reads `<action> takes no parameters`
- Script listing and tab completion no longer re-parse every YAML file on every
  keystroke: the typed prefix filters before the parse, descriptions are cached
  per file version, and the completer is threaded. A 200-script `--scripts-dir`
  cost ~600 ms of the door server's own event loop per Tab; it is now ~4 ms

### Removed
- The hostile-peer hardening added over the previous review rounds. This client
  dials **out** to a pet door on a home LAN and nothing connects inward; the
  simulator is a test tool. Defending against a hostile peer defended a
  scenario that does not exist, and the machinery cost far more than it bought:
  - `EventThrottle` and all ~15 throttled log sites (client, facade, simulator
    and framer). Those sites log plainly again — lazily, and still sanitized
    where the value is network-derived
  - `FrameDispatcher`: bounded in-flight tasks, the backlog,
    `pause_reading`/`resume_reading` and the `call_soon` re-arm. Both receive
    paths iterate the frames from one read and dispatch each, as before
  - The simulator's door-transport write-backlog cap (`MAX_WRITE_BACKLOG`) and
    its drop/abort machinery. The framing-overflow drop stays
  - The client's `_declined` / `_pending_direct_losses` counters. They guarded
    a path `connect()` routes around: it wires a `_ConnectionAttempt` shim per
    attempt, which knows its own transport by identity
  What is kept, because it is correctness rather than security: the 64 KiB
  receive cap (a stuck door must not exhaust memory), every "never raises on
  arbitrary input" behaviour in the framer, the client and the simulator
  protocol, `sanitize_text` on network-derived values, the control channel's
  loopback default bind, and the per-attempt transport identity
- `scripts/generate_gaps_report.py`, `tests/TESTING_GAPS.md` and the CI step
  that regenerated and committed the report. It stamped `datetime.now(UTC)`
  unconditionally, so every push produced a bot commit. `[tool.coverage.run]
  source` now covers shipping code only
- Documentation tests that linted GitHub heading anchors and asserted CI step
  names were specific English sentences. The two that check real behaviour —
  keepalive framing against `docs/protocol.md`, sensor gating against
  `docs/operation.md` — stay

### Fixed
- `get_available_timezones()` no longer advertises zones that the conversion
  helpers cannot handle. tzdata ships a `localtime` pseudo-zone whose TZif
  footer carries no POSIX rule, so `iana_to_posix()` had nothing to return for
  a name the same module had just offered as valid
- `PowerPetDoor.set_notifications()` sent `"1"`/`"0"` strings where every
  released version sent JSON booleans. Reverted: `docs/protocol.md` shows
  strings here, but it is reverse-engineered and is not authority over the
  firmware, and the change was never needed — the simulator accepted booleans
  throughout
- Reading a schedule from the device no longer refuses `hour: 24` (a natural
  end-of-day encoding) or a time block with no hour. Refusing made
  `refresh_schedules()` drop the entry silently, hiding a schedule that really
  exists on the door; the simulator still validates strictly what a client asks
  it to store
- The plain-input simulator prompt no longer busy-spins at EOF. `readline()`
  returning `""` was treated as a bare Enter, and an fd at EOF is permanently
  readable, so the reader callback re-fired forever: 98% of a core, tens of MB
  of prompt text, and a process that never exited. EOF now ends the session
  cleanly, exactly like Ctrl-D on a terminal (pipe-backed stdin only; a real
  TTY was never affected)
- `ppd-simulator` and `ppd-simulator-ctl` start on a stdin that cannot be
  polled. `/dev/null`, a regular file and the temp file bash uses for a heredoc
  make `loop.add_reader()` raise `PermissionError`, which was a 37-line
  traceback and exit code 1 before the prompt appeared. Both fall back to
  blocking reads
- The `add_schedule` script action really is 24/7. Its window was built as
  `00:00-23:59` against an *exclusive* end, so it covered 1439 of the day's
  1440 minutes and blocked both sensors for exactly the minute 23:59 — two
  schedule-script tests failed for 60 seconds a day. A schedule whose start and
  end coincide is now the whole day, which is the only spelling a true 24h
  window has with an exclusive end
- `PowerPetDoor`'s twelve flag listeners no longer cache a falsy non-boolean.
  `make_bool` returns its argument unchanged for a value it does not recognize,
  so `[]`, `{}` or `0.0` reached the facade as themselves rather than as
  `None` — and the `if value is not None` guard let them into a strictly typed
  cache, where a known-ON `safety_lock` then read `False`. (It healed on the
  next `refresh_settings()` or reconnect, but was wrong until then.)
  `make_bool` itself is deliberately unchanged: `compress_schedule` calls it
  unguarded on day flags, where "unrecognized" has to stay fail-closed
- Device-supplied field values can no longer forge log records. `sanitize_text`
  escapes CR but not LF — deliberately, because it is also applied to whole
  formatted records, where a multi-line traceback is legitimate — so a field
  carrying a newline wrote extra physical lines with a timestamp, a severity
  and a message the device chose. The new `sanitize_field()` escapes LF as well
  and is used at every single-value field sink
- Simulator scripts: 9 of the 12 documented `assert` conditions were unusable.
  PyYAML resolves `on`/`off` to booleans and bare digits to ints, and the
  comparison called `.lower()` on them, so `assert power equals: off` failed a
  **true** assertion with `AttributeError: 'bool' object has no attribute
  'lower'`. Both sides of the comparison are now rendered as text first; that
  also fixes `hold_time`, which is stored as a float and lost to `str(2.0)`
  even when the expected value was quoted
- CI: the coverage gate and the packaging, lint, format and type steps are
  pinned by a test that **parses** `.github/workflows/test.yml`. Substring
  assertions passed under three of the four ways the gate could be disabled
  (lowering `--fail-under`, `continue-on-error: true`, `if: false`)

- `ppd-simulator` no longer swallows the cancellation `asyncio.Runner` uses to
  deliver Ctrl-C, so `main()`'s `KeyboardInterrupt` handler is actually
  entered; the startup-script task is held and cancelled inside
  `run_simulator`'s cleanup, so its result is authoritative before it is read
- `sanitize_text` escapes unpaired surrogates (`\ud800`-`\udfff`) as well as
  C0/C1/DEL, with a width-aware `\xNN`/`\uNNNN` escape. A lone surrogate is
  legal JSON, arrives as pure ASCII on the wire, and cannot be encoded to
  UTF-8 — so the "sanitized" record was exactly what a
  `logging.FileHandler(encoding="utf-8")` could not write: 200 hostile frames
  produced **0** log lines and 359 KB of logging-internal tracebacks on stderr
- `PowerPetDoor`'s command timeouts carry a message. `asyncio.wait_for` raises
  a bare `TimeoutError()` whose `repr` is literally `TimeoutError()`; it now
  names the command, the wait, the endpoint, and — when there is no connection
  — that the command is queued rather than lost
- `--history <unusable path>` falls back to in-memory history instead of
  handing the same path to `FileHistory` anyway, which raised inside the
  running prompt on every load and every store and put up a modal
  "Press ENTER to continue" for the rest of the session
- A bind failure at startup prints one operator sentence naming the role of
  the port that failed (`door`/`control`) and which flag changes it, instead of
  a 30-line asyncio traceback with build-machine paths in it; the traceback
  stays available behind `--debug`. A failed control-channel bind also brings
  the door server back down
- The daemon answers a blank control-channel line (`ERROR: Empty command`)
  instead of dropping it unanswerably, and refuses an over-long line with
  `ERROR: Command line too long (max 65536 bytes)` at INFO instead of logging
  an asyncio internal at ERROR into every other operator's `ctl` session
- Simulator: `DoorMotionEngine.activate_sensor` applies the same gate
  `trigger_sensor` does. It had no command-lockout check and no schedule check,
  so a script step `- action: inside` opened the door while command lockout was
  on and while every schedule window was closed — which `docs/operation.md`
  ("Outside scheduled windows, sensor triggers are ignored") says a real door
  would not. Both now share one predicate,
  `DoorMotionEngine.sensor_open_block_reason`
- CI: the changelog guard covers a whole multi-commit push
  (`github.event.before`, not just `HEAD^`), and an unresolvable base ref now
  fails the job instead of printing `OK: 0 file(s)` and exiting 0
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
- `PowerPetDoor.hold_time` no longer goes stale on a device value it cannot
  represent: a `holdTime` above `sys.float_info.max` made `value / 100.0` raise
  `OverflowError` inside the client's listener isolation — one unthrottled
  traceback per frame, with the cached value silently unchanged. The bound is
  float representability, not a protocol ceiling: nothing a real device could
  meaningfully send is refused

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
- `data_received()` honours its documented "never raises on arbitrary input"
  contract on both the client and the simulator. `json.JSONDecodeError` is a
  *subclass* of `ValueError`, not a superset of what `json.loads` raises: an
  integer literal over 4,300 digits raises a bare `ValueError` and deep nesting
  raises `RecursionError`, and both escaped the decoder. Brace-balanced and
  well under the 64 KiB framing cap, they fatal-errored the transport — the
  client then reconnected in a hot loop forever, because every attempt
  *connects* before dying and so resets the backoff. Both frames now take the
  same bad-frame path an unparseable frame already took; nothing that decoded
  before is refused now
- Simulator: the interactive completer runs off the event loop
  (`ThreadedCompleter`), so no completer can stall the emulated door

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

[Unreleased]: https://github.com/corporategoth/py-powerpetdoor/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/corporategoth/py-powerpetdoor/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/corporategoth/py-powerpetdoor/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/corporategoth/py-powerpetdoor/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/corporategoth/py-powerpetdoor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/corporategoth/py-powerpetdoor/releases/tag/v0.1.0
