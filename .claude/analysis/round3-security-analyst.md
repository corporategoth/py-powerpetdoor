# Security Analyst Analysis — Round 3

Scope: a fresh full-codebase security sweep of `pypowerpetdoor` at commit
`3478a5b` — the asyncio client library plus the device **simulator** (door TCP
protocol + unauthenticated control channel + YAML scripting + interactive
CLI/ctl), after the Round-2 remediation and the subsequent changes (per-frame
`backslashreplace` decoding, non-`(int|str)` msgID handling, client task
tracking, `connect()` idempotence guards, ctl silence-gap timeout semantics,
serialized script runs, `--scripts-dir` enumeration).

Threat model (unchanged):

- **LAN attacker** who can open TCP connections to a running simulator (door
  port 3000 and/or the daemon control port).
- **Malicious / compromised door** that a `PowerPetDoorClient` /
  `PowerPetDoor` has connected to and which returns hostile bytes.
- **Malicious / untrusted YAML script file** fed to the simulator.

The device wire protocol is plaintext JSON-over-TCP with no auth/crypto **by
hardware design** — findings are about how the library/simulator *handle* that
untrusted data, not demands to encrypt the device protocol.

Every finding below was confirmed by executing the current code (in-process
harnesses plus real TCP sockets against a real `ppd-simulator` process).

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 3 |
| Informational | 2 |

The Round-2 fix (`Schedule.from_dict`) holds and is well-built — I could not
get a malformed schedule past it. The newer changes did **not** open anything:
`backslashreplace` decoding introduces **no** new injection vector (see
"Areas Reviewed"), the unbounded `run … wait` wait is client-local and does not
hold daemon resources, `--scripts-dir` enumeration neither leaks beyond the
already-omnipotent control channel nor allows escape, and the new task sets /
connect guards are wedge-free.

What Round 2 did *not* do is generalize its own lesson. The validation added to
`SET_SCHEDULE` was not applied to the sibling `SET_*` handlers, and the
terminal-sanitization added in Round 1 was applied to the log path and the ctl
path but not to the interactive CLI's own result-printing path or to the client
library's loggers. Findings 1–3 are all residuals of that pattern.

---

## Findings

### 1. [Medium] Unvalidated `SET_*` wire values permanently poison simulator state — one packet disables `GET_SETTINGS` for every client and wedges the door open

**Files:**
- `src/powerpetdoor/simulator/protocol.py:712-719` (`_handle_set_hold_time`:
  `self.state.hold_time = msg[FIELD_HOLD_TIME] / 100.0`)
- `src/powerpetdoor/simulator/protocol.py:704-710` (`_handle_set_timezone`:
  `self.state.timezone = msg[FIELD_TZ]`)
- Damage surfaces at `src/powerpetdoor/simulator/state.py:439`
  (`get_settings`: `int(self.hold_time * 100)`),
  `state.py:425-428` (`get_posix_tz_string(self.timezone)`),
  `state.py:458-487` (`get_tzinfo`: `zoneinfo.ZoneInfo(self.timezone)`, whose
  `except` clause catches only `ZoneInfoNotFoundError`/`ValueError`),
  `src/powerpetdoor/simulator/engine.py:555,564-565` (`float(self.state.hold_time)`),
  `src/powerpetdoor/simulator/server.py:393` (`broadcast_hold_time`)

`Schedule.from_dict` is now rigorously validated (Round 2). Its siblings are
not: `SET_HOLD_TIME` and `SET_TIMEZONE` copy attacker-controlled JSON straight
into long-lived state, and the value survives the attacker's disconnect.

JSON permits `Infinity`/`NaN` (Python's `json.loads` accepts both the literals
and `1e400`), so `holdTime: 1e400` divides cleanly to `inf`, is **assigned**,
and only then blows up on the response line. `SET_TIMEZONE` accepts any JSON
type at all, including a list/dict, and answers `success: true`.

**Attack scenario (verified end-to-end over real TCP against `ppd-simulator
--host 127.0.0.1 --port 39361 --daemon 39362`):** the attacker opens the door
port (default bind `0.0.0.0:3000`), sends **one** message, and disconnects:

```
attacker -> {"cmd":"SET_HOLD_TIME","holdTime":1e400}
attacker <- {"CMD":"SET_HOLD_TIME","success":"false",...,"reason":"Command failed"}
```

A *different*, innocent client connecting afterwards gets:

```
victim -> {"config":"GET_SETTINGS"}   victim <- {"CMD":"GET_SETTINGS","success":"false",...,"reason":"Command failed"}
victim -> {"config":"GET_HOLD_TIME"}  victim <- {"CMD":"GET_HOLD_TIME","success":"false",...,"reason":"Command failed"}
```

…for the lifetime of the process. `state.hold_time` is now `inf`, so every
later `int(self.hold_time * 100)` raises `OverflowError` (`NaN` raises
`ValueError`), which the generic `except Exception` in `_handle_message`
converts into "Command failed". `GET_SETTINGS` is the command the shipped
client library issues on connect and on every settings refresh, so the
simulator becomes useless to `PowerPetDoorClient` / Home Assistant integration
testing. The operator-side `broadcast settings` / `broadcast hold_time` CLI
commands break the same way.

The door is wedged too: with `hold_time = inf` or `nan`, `_hold_open()`'s
`remaining <= 0` test is never true (all NaN comparisons are false; `inf` never
elapses), so the door parks in `DOOR_HOLDING` forever. Measured directly:
`hold=0.05 -> DOOR_CLOSED`, `hold=inf -> DOOR_HOLDING`, `hold=nan ->
DOOR_HOLDING` after 1s, with no CPU spin (0.00s CPU — it is a hang, not a busy
loop).

`SET_TIMEZONE` gives the same result by a different route:

| payload | `SET_TIMEZONE` reply | later `GET_SETTINGS` | schedule evaluation |
|---|---|---|---|
| `{"tz":["x"]}` | `success:true` | **"Command failed"** forever | `TypeError: unhashable type: 'list'` |
| `{"tz":{"a":1}}` | `success:true` | **"Command failed"** forever | `TypeError: unhashable type: 'dict'` |
| `{"tz":5}` | `success:true` | succeeds | `TypeError: expected str, bytes or os.PathLike object, not int` |

The `TypeError` from `get_tzinfo()` is *not* caught by its
`except (ZoneInfoNotFoundError, ValueError)`, so it propagates out of
`is_sensor_allowed_by_schedule` on every sensor trigger — exactly the failure
mode Round 2's finding #1 closed for schedules, reopened through the timezone
field.

Note the asymmetry that makes this clearly a bug rather than a design choice:
the *operator* path for the same value **is** validated — the CLI `timezone`
command checks IANA membership or parses the POSIX string before storing
(`commands/settings.py:344-368`). Only the unauthenticated wire path skips it.

**Why Medium and not higher:** the blast radius is the simulator, a
developer/test tool — no credentials, no PII, no confidentiality or integrity
impact, and a restart clears it. **Why not Low:** it is unauthenticated, a
single packet, needs zero operator interaction, persists after the attacker
leaves, denies the tool's core query command to *all* clients, and additionally
disables the simulated door. That matches the Round-1 calibration, which rated
the (also simulator-side, also availability-only) receive-buffer DoS Medium.

**Recommendation:** apply the `Schedule.from_dict` pattern to the remaining
`SET_*` handlers — a small `_coerce_*` helper per field, rejecting with the
standard error envelope instead of storing:

- `SET_HOLD_TIME`: require a finite number in the device's plausible range
  (e.g. 0–90000 centiseconds); reject `inf`/`nan`/non-numeric. Validate
  **before** assigning.
- `SET_TIMEZONE`: require `isinstance(value, str)` (and ideally a length cap);
  reject anything else.
- `SET_SENSOR_TRIGGER_VOLTAGE` / `SET_SLEEP_SENSOR_TRIGGER_VOLTAGE`: today they
  accept and echo arbitrary JSON (verified: `{"sensorTriggerVoltage":{"a":1}}`
  is stored and echoed with `success:true`). Nothing does arithmetic on them so
  they are currently inert, but they are the same latent trap — bound them to
  ints while you are in there.
- Defensively, widen `DoorSimulatorState.get_tzinfo`'s `except` to include
  `TypeError` so a bad value can never crash schedule evaluation.

---

### 2. [Low] Terminal-escape injection into the interactive simulator CLI via network-poisoned state (Round-1 #3 residual)

**Files:**
- `src/powerpetdoor/simulator/cli.py:722` (`print(f">>> {result.message}")`,
  prompt_toolkit interactive loop) and `cli.py:497-501`
  (`_BasicStdinInput.process_command`, plain-stdin fallback) — neither applies
  `sanitize_text`
- Source of the tainted value: `src/powerpetdoor/simulator/protocol.py:709`
  (`SET_TIMEZONE` stores the wire string verbatim), rendered by
  `src/powerpetdoor/simulator/commands/settings.py:333-340`
  (`CommandResult(True, f"Timezone: {display}")`)

Round 1 fixed ANSI injection at the **log** source (`sanitize_log_text`), at
the CLI **formatter** (`_SanitizingFormatter`), on the **control-channel**
egress (`cli.py:285`, `escape_message(sanitize_text(result.message))`) and in
**ctl** (`ctl.py:432-438`). The one output path left unsanitized is the
interactive CLI printing its own command results — and that path can carry
network-derived state.

**Attack scenario (verified end-to-end against a real `ppd-simulator`
process):** the attacker connects to the door port and sends a `SET_TIMEZONE`
whose control characters are supplied as JSON `\u` escapes (so no raw control
bytes ever appear on the wire, and the `backslashreplace` decoder is not
involved at all):

```json
{"cmd":"SET_TIMEZONE","tz":"[2J[1;1H*** PWNED ***"}
```

The simulator answers `success: true`. When the operator later types `timezone`
(or the alias `tz`) at the interactive prompt, the captured stdout contains
literally:

```
127.0.0.1:39325> >>> Timezone: \x1b[2J\x1b[1;1H*** PWNED ***\x07
```

i.e. a real ESC `[2J` (clear screen), cursor home, forged text, and a BEL —
delivered to the operator's terminal. The same print path would carry any other
network-poisoned string a command echoes, so this widens as soon as another
wire-settable string is surfaced by a command.

Rated Low rather than Medium because it needs the operator to run a specific
command afterwards; the *automatic* path (log records) is already sanitized.

**Recommendation:** sanitize at the same place the control channel already
does. Wrap both interactive prints in `sanitize_text(...)` — e.g.
`print(f">>> {sanitize_text(result.message)}")` in `interactive_input_loop`
and in `_BasicStdinInput.process_command` (both the `print` and the
`prompt.output` branch). Better still, sanitize once inside a small
`_render_result()` helper shared by the CLI, ctl-local and control-channel
paths so the next output site cannot forget. (Fixing Finding 1's `SET_TIMEZONE`
type check does not close this — a *string* timezone can still carry ESC.)

---

### 3. [Low] Client library logs raw device-supplied bytes unsanitized, including at ERROR level (Round-1 #3's twin on the shipped side)

**Files:**
- `src/powerpetdoor/client.py:1410` —
  `_LOGGER.error("Failed to decode JSON frame (%s): %s", err, frame)` (**ERROR**
  level: on by default in essentially every consumer)
- `src/powerpetdoor/client.py:1403` — `_LOGGER.debug("RX < %s", decoded)`
- `src/powerpetdoor/tz_utils.py:212` —
  `_LOGGER.debug("Could not parse POSIX TZ string: %s", posix_tz)` (device-supplied)
- `src/powerpetdoor/schedule.py:99,114` — `%s` of a device-supplied time value

The simulator has `sanitize_log_text` and applies it to every network-derived
log record. The **library** — the artifact actually shipped to Home Assistant —
has no equivalent. C0 control characters (notably ESC `0x1b`) are valid ASCII,
so they pass the decoder untouched and land verbatim in the host application's
log.

**Attack scenario (verified at runtime):** a malicious/compromised door (or a
MITM on the plaintext LAN protocol — in scope per the threat model) sends a
brace-balanced but invalid frame containing escape sequences. Captured records
from `powerpetdoor.client`:

```
DEBUG 'RX < {\x1b[2J\x1b[1;1H*** PWNED ***\x07}'
ERROR 'Failed to decode JSON frame (Expecting property name enclosed in double
       quotes: line 1 column 2 (char 1)): {\x1b[2J\x1b[1;1H*** PWNED ***\x07}'
```

(The repr above is Python's; the record's actual message holds the raw bytes.)
Anyone reading that log on a terminal — `tail -f home-assistant.log`,
`journalctl -u home-assistant`, `docker logs` — gets the escapes executed by
their emulator. No operator interaction beyond viewing logs, and it fires at
ERROR level, which needs no debug configuration.

Note the neighbouring lines are already safe and show the intended pattern:
`json.dumps(msg)` (client.py:1444, 1486, 1500) escapes control characters, and
`%r` (client.py:1432, 1459) uses `repr`. Only the raw-`%s` sites leak.

**Recommendation:** give the library the sanitizer the simulator already has —
ideally by promoting one implementation to a shared module (there are currently
two copies, `protocol.py:144` and `prompt_common.py:61`, with a comment
acknowledging the duplication) and applying it to every device-derived `%s`
argument in `client.py`, `tz_utils.py` and `schedule.py`. The cheapest
equivalent is to switch those four sites to `%r`.

---

### 4. [Low] CI and release workflows resolve dependencies unpinned despite a fully hashed `uv.lock`

**Files:** `.github/workflows/test.yml:75,104,123` and
`.github/workflows/release.yml:26,45` (`uv pip install -e ".[dev]"`,
`uv build`); `uv.lock` (present, complete, **542** `sha256` entries);
`pyproject.toml:1-3` (`requires = ["setuptools>=61.0", "wheel"]`)

Round 1 closed the mutable-action-tag hole and every third-party action is now
SHA-pinned (re-verified below). The other half of the supply chain is still
floating: no workflow uses the committed lockfile. Every CI run — including the
`release.yml` path that publishes to PyPI — resolves `pytest`, `pytest-xdist`,
`hypothesis`, `mypy`, `ruff`, `pyyaml`, `prompt_toolkit` and their transitive
dependencies fresh from PyPI with no version pin and no hash check. The
`publish` job additionally runs `uv build`, whose default build isolation
installs `setuptools>=61.0` and `wheel` from PyPI at build time, inside the one
job that holds `id-token: write`.

**Attack scenario:** a compromised maintainer account or a hijacked release of
any of those packages (or of a transitive dependency such as `execnet`,
`pluggy`, `coverage`) ships a malicious `setup.py`/import-time payload. On the
next PR or push it executes on the shared `act_runner` host with the workflow's
token. In the `release.yml` publish job the same trick applied to `setuptools`
or `wheel` runs *inside the artifact build*, producing a poisoned wheel that is
then signed and published via trusted publishing (OIDC) — the pinning applied
to actions is bypassed entirely because the compromise enters through PyPI, not
GitHub.

**Recommendation:** use the lockfile that is already committed and hashed:
replace `uv pip install -e ".[dev]"` with `uv sync --locked --all-extras`
(which fails loudly if the lock drifts) in `test.yml` and `release.yml`. For
the publish job, either pin the build backend exactly in
`[build-system] requires` or build in a locked environment
(`uv build --no-build-isolation` with the backend installed from the lock).
Consider Dependabot/Renovate on `uv.lock` so pinning does not mean staleness —
the current resolved set (pyyaml 6.0.3, tzdata 2025.3, prompt-toolkit 3.0.52)
is up to date and should stay that way.

---

### 5. [Informational] `_coerce_schedule_int` does not catch `OverflowError`, so `Infinity` is rejected via the generic handler with a traceback

**File:** `src/powerpetdoor/simulator/state.py:57-69` (`except (TypeError, ValueError)`)

`int(float("inf"))` raises `OverflowError`, which is in neither
`_coerce_schedule_int`'s except tuple nor `_handle_set_schedule`'s
`except ValueError` (`protocol.py:537`). Verified:

```
{"cmd":"SET_SCHEDULE","schedule":{"index":1e400,"inside":true}}
  -> {"CMD":"SET_SCHEDULE","success":"false",...,"reason":"Command failed"}
```

**This is not a vulnerability** — the schedule is still rejected, state stays
clean (`schedules == {}` afterwards), and `SET_SCHEDULE_LIST` remains atomic
because the exception escapes the list comprehension before `.clear()`. The
only costs are a wrong-looking reason string and a full stack trace in the
operator's log for a value the validator meant to reject cleanly. One-word fix:
`except (TypeError, ValueError, OverflowError)`. (`NaN` is already handled —
`int(nan)` raises `ValueError`, giving the correct "must be a number" reason.)

---

### 6. [Informational] No read backpressure or write-buffer limit on the simulator's door port

**Files:** `src/powerpetdoor/simulator/protocol.py:307-320` (one
`asyncio.create_task` per received frame; no `transport.pause_reading()`),
`protocol.py:327-333` (`_send` writes with no `pause_writing` handling or
`set_write_buffer_limits`), `src/powerpetdoor/simulator/server.py:150-166` (no
cap on concurrent connections)

Round 1 bounded the *read* buffer (64 KiB, enforced by `framing.py` and acted
on by both sides). The other directions are unbounded: a flooding peer spawns
one handler task per frame with no backpressure, and responses queue in the
transport's write buffer without limit, giving roughly 12× byte amplification
(a 25-byte `GET_SETTINGS` yields a ~300-byte reply).

Measured against a real daemon: a client that floods `GET_SETTINGS` and never
reads pushed 2.9 MB before TCP backpressure stalled it, driving the simulator's
RSS from 33 MB to 63 MB (~+30 MB), after which growth **plateaued** and the
process stayed alive and responsive. So the effect is self-limiting in
practice — the event loop's own throughput throttles the attacker long before
memory becomes critical. Reported for completeness rather than as a defect;
if it is ever worth hardening, `transport.set_write_buffer_limits()` plus
`pause_reading()` while a backlog exists (and optionally a connection cap —
real hardware accepts a single connection) would close it.

---

## Round 2 Fix Verification

**Round-2 Finding 1 (Low) — `Schedule.from_dict` trusts wire structure:
FIXED, and the fix is solid.** Verified by reading
(`state.py:57-100,223-276`) and by executing the current code against
adversarial payloads. Every input below was exercised through
`Schedule.from_dict` followed by `is_day_active(0..6)`, `is_sensor_allowed`
and `to_dict()` — the exact evaluation paths that used to raise:

| Payload | Result |
|---|---|
| `daysOfWeek: [1]` (Round-2's original PoC) | rejected — "must be a list of 7 values" |
| `daysOfWeek: "aaaaaaa"` / `{}` | rejected |
| `daysOfWeek: 127` (legacy bitmask) | accepted → 7 booleans |
| `index: -1` / `256` | rejected — "must be between 0 and 255" |
| `index: NaN` | rejected — "must be a number" |
| `index: "0"` / `True` | accepted, coerced to `0` / `1` |
| `in_start_time: {"hour": 99}` / `{"min": 60}` / `{"hour": -1}` | rejected with per-field range messages |
| `in_start_time: {"hour": "x"}` / `{"hour": [1]}` | rejected — "must be a number" |
| `in_start_time: "x"` (non-mapping) | rejected — "must be an object" |
| `"not a dict"` (non-mapping payload) | rejected — "Schedule must be an object" |
| `index: 1e400` | rejected, but via `OverflowError` → see Informational #5 |

Protocol-level behavior is correct too: `SET_SCHEDULE` returns the error
envelope carrying the specific reason, `SET_SCHEDULE_LIST` rejects the **whole
batch** atomically (a valid entry followed by a malformed one leaves
`schedules == {}` and `GET_SCHEDULE_LIST` returns `[]`), and rejection reasons
are passed through `sanitize_log_text` before logging (`protocol.py:540,569`).
No stored schedule can now raise during evaluation.

**Round-2 Finding 2 (Informational) — door server binds `0.0.0.0`:
ACCEPTED, no action taken, none expected.** Still `server.py:116` /
`cli.py:810`, still documented; the powerful control channel still defaults to
`127.0.0.1` with `--control-host` gated behind `--daemon` (`cli.py:947-948`)
and an UNAUTHENTICATED warning in the help text.

**Round-1 fixes re-verified (all still hold):** 64 KiB framing cap with
`overflow` acted on by both consumers; control channel loopback-by-default;
sanitization at the log source, the CLI formatter, the control-channel egress
and in ctl; `find_frame_end` never raising on non-`{` input; `process_message`
always completing paired futures; script-path restriction on the control
channel (`_load_script_restricted` + `candidate.parent == base` after
`.resolve()`); SHA-pinned actions; history files created `0o600` via
`os.open(..., 0o600)` + `os.chmod` (`commands/history.py:20-28`).

---

## Areas Reviewed With No Findings

- **`backslashreplace` decoding creates no new injection vector.** This was the
  headline question and the answer is a clean no. `errors="backslashreplace"`
  only rewrites bytes ≥ `0x80`, and it rewrites them into *printable ASCII*
  (`\xNN`). Every injection-capable character (C0 controls, and ESC `0x1b`
  above all) is valid ASCII and passed through the decoder before this change
  too — so the change strictly *narrows* the byte set reaching a log sink. My
  ANSI PoCs (Findings 2 and 3) deliberately use JSON `` escapes and raw
  ASCII ESC respectively; neither depends on the new decoder. It does mean the
  client now logs escaped garbage where it previously dropped the whole chunk,
  which exercises the unsanitized sinks of Finding 3 more often — but it did
  not create them.
- **`backslashreplace` does not desynchronize framing.** The replacement emits
  `\xNN` (one backslash followed by three non-backslash characters), so it can
  never leave a lone backslash immediately before a `"` and confuse
  `find_frame_end`'s escape tracking. Verified: `b'{"a":"\xff\xfe"}{"b":1}'`
  and `b'{"a":"x\xff"}{"b":2}'` both frame into exactly two frames with an
  empty remainder. The affected frame simply fails `json.loads` and is skipped,
  exactly as the comment claims. The one side effect is a 4× character
  inflation of non-ASCII input against the 64 KiB cap — which makes the
  overflow disconnect trigger *sooner*, i.e. in the defender's favour.
- **The unbounded `run … wait` deadline does not enable a resource-holding DoS
  on the control channel.** The unbounded wait lives entirely in the *ctl
  client* (`ctl.py:255-256` `sock.settimeout(None)`; `ctl.py:498`
  `await_response(None)`), not in the daemon. On the daemon side each control
  connection is its own `_handle_client` task (`cli.py:264-304`) and
  `CommandHandler.execute` takes no cross-connection lock, so a long wait-run
  blocks only the issuing connection; other clients keep working. `run … wait`
  passes `queue_if_busy=False`, so a second wait-run fails immediately
  ("Another script is already running") rather than stacking — it cannot be
  used to pile up waiters. `reader.readline()` is bounded by asyncio's 64 KiB
  stream limit, and a `ValueError` from an over-long line is caught and closes
  the connection. The residual is that a ctl blocked in `recv` with no deadline
  would hang on a black-holed (no-FIN) connection; that is the operator's own
  process, Ctrl-C escapes it, and it is a documented deliberate trade-off.
- **`--scripts-dir` enumeration leaks nothing and allows no escape.**
  `_script_files_in` uses a non-recursive `glob("*.yaml"/"*.yml")` — no
  traversal — and `list` is reachable only from the control channel, which by
  design can already `run` scripts and `shutdown` the daemon, so exposing
  script *names* crosses no privilege boundary. `run` still resolves through
  `_load_script_restricted` (rejects `/`, `\`, leading `.`) and
  `_load_script_by_name` (`candidate.resolve()` then `candidate.parent == base`),
  which also defeats a symlink planted inside the scripts dir. `script_completer`
  does walk the CWD, but it executes in the *ctl* process for the local
  operator, and `_get_arg_help` — the only completion-adjacent thing reachable
  over the wire — never calls it. (A symlink already planted in `--scripts-dir`
  would be followed by `list`'s `Script.from_file` and could echo YAML parse
  errors quoting file content; that requires local write access to a directory
  the operator explicitly configured, and grants nothing the caller could not
  get with `run`.)
- **New task sets and connect guards create no wedge or lockout.**
  `connect()`'s `_connecting` flag is set immediately before the `try` and
  cleared in `finally`, so no cancellation path can leave it stuck true; every
  `await` that could be cancelled is inside the `try`. `connection_made`'s
  second-transport rejection closes only the new transport and leaves the live
  one untouched. `disconnect()` unconditionally clears `_transport`,
  `_can_dequeue`, `_check_receipt`, `_keepalive` and the queue, and
  `connection_made` re-derives `_can_dequeue` from the queue state, so no flag
  survives a reconnect in a blocking position. `_track_task`/`_on_task_done`
  (client) and `_create_task`/`_on_task_done` (simulator) both discard on
  completion — no unbounded set growth — and both log escaped exceptions
  instead of swallowing them. A device that never answers is handled by
  `check_receipt` (fails the future after `MAX_FAILED_MSG`, then dequeues) and
  by keepalive (`disconnect()` after `MAX_FAILED_PINGS`); neither leaves the
  send path permanently blocked.
- **Non-`(int|str)` msgID handling is correct.** `client.py:1454-1462` guards
  the dict lookup with `isinstance(reply_msg_id, (int, str))` and logs the
  reject with `%r` (which escapes control characters), so an unhashable
  `msgID` neither raises nor injects.
- **YAML loading remains safe.** `scripting.py:134` is the only YAML entry
  point and uses `yaml.safe_load`; repo-wide there is no
  `yaml.load`/`full_load`/`unsafe_load`. A hostile script yields at most a
  `ScriptError`, and `from_yaml` requires a top-level mapping.
- **No dangerous execution sinks.** Repo-wide grep over `src/` for `eval(`,
  `exec(`, `pickle`, `subprocess`, `os.system`, `shell=True`, `__import__`
  returns nothing.
- **Bind addresses re-checked.** The only `0.0.0.0` occurrences are the door
  server default (`server.py:116`, `cli.py:510,810`) and the `--control-host`
  help text. The control channel defaults to `127.0.0.1`, and `--control-host`
  is rejected outright without `--daemon` (`cli.py:947-948`).
- **CI supply chain (pinning half) intact.** `actions/checkout@11d5960…`,
  `astral-sh/setup-uv@37802ad…` (including all three retries in the composite
  action), `codecov/codecov-action@b9fd7d1…`, both gitea artifact actions and
  `pypa/gh-action-pypi-publish@dc37677…` are SHA-pinned with version comments.
  The only tag reference is the first-party org reusable workflow
  (`.gitea/workflows/sync-wiki.yml@v1.1.0`) — the exception Round 1 permitted.
  Publishing uses OIDC trusted publishing with `id-token: write` scoped to the
  `publish` job and a `pypi` environment; no long-lived PyPI token exists.
  `CODECOV_TOKEN`/`GH_PAT` are referenced but never echoed, and the Codecov
  step is guarded by a non-empty check so fork PRs (which get no secrets) skip
  it rather than fail. See Finding 4 for the unpinned *dependency* half.
- **The fuzz job now running on PRs is a net positive, not a new risk.** It
  uses the same `pull_request` trigger as the unit matrix (not
  `pull_request_target`, so no secrets are exposed to fork code), pins
  `--hypothesis-seed=0` for determinism, and adds no new privileges. It runs
  untrusted PR code exactly as the pre-existing unit-test job already does.
- **Dependency currency is good.** The only runtime dependency is `tzdata`
  (pure data, 2025.3); `pyyaml` (6.0.3) and `prompt_toolkit` (3.0.52) are
  optional extras. The dev set (pytest 9.0.2, hypothesis 6.165.10, mypy 2.3.1,
  ruff 0.16.4, coverage 7.15.4) is current, and `uv.lock` carries sha256 hashes
  for every artifact. Nothing EOL, nothing with a known CVE, minimal attack
  surface. (Cosmetic: `test.yml:47` pins `ruff==0.14.13` while the lock
  resolves 0.16.4 — a consistency nit, not a security issue.)
- **No secrets at rest or in logs.** The device protocol carries no
  credentials, keys or PII; neither the library nor the simulator stores or
  logs anything sensitive. History files (the only persisted operator data) are
  created `0600`.
- **Operator/ctl input remains bounded.** `parse_args` rejects extra arguments
  and `parse_arg` enforces per-type min/max/choices before any value reaches
  state, and the control protocol's `escape_message`/`unescape_message` pair
  round-trips correctly (backslashes doubled first, split on `\\\\` first) so
  no message can forge an extra `LOG:`/`OK:`/`ERROR:` line.
