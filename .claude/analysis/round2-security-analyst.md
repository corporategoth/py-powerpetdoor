# Security Analyst Analysis — Round 2

Scope: a fresh full-codebase security sweep of `pypowerpetdoor` at commit
`3f96bb8` — the asyncio client library plus the device **simulator** (TCP door
protocol + unauthenticated control channel + YAML scripting + interactive
CLI/ctl), after the large Round-1 remediation and the subsequent refactors
(shared `framing.py`, `DoorMotionEngine`, CLI testability, STATUS broadcast).

Threat model (unchanged from Round 1):

- **LAN attacker** who can open TCP connections to a running simulator (door
  port 3000 and/or the daemon control port).
- **Malicious / compromised door** that a `PowerPetDoorClient` /
  `PowerPetDoor` has connected to and which returns hostile bytes.
- **Malicious / untrusted YAML script file** fed to the simulator.

The device wire protocol is plaintext JSON-over-TCP with no auth/crypto **by
hardware design** — findings are about how the library/simulator *handle* that
untrusted data, not demands to encrypt the device protocol.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 1 |
| Informational | 1 |

The Round-1 remediation holds up under re-review: every Round-1 finding is
genuinely fixed in the current code (verified by reading and by running the
current code against the original attack inputs). The refactors did **not**
reopen old holes or add new unauthenticated listening surface. One residual
untrusted-input gap remains on the simulator side — the mirror image of
Round-1 finding #5, which was fixed only on the client side — plus one
informational note.

---

## Findings

### 1. [Low] `Schedule.from_dict` trusts wire-supplied structure — a malicious client can plant a schedule that later raises `IndexError`/`TypeError` on evaluation

**Files:**
- `src/powerpetdoor/simulator/state.py:170-205` (`Schedule.from_dict` — no
  validation of `daysOfWeek` length/type, time-field types, or `index` type)
- `src/powerpetdoor/simulator/state.py:207-251` (`is_day_active` indexes
  `self.days_of_week[day_index]`; `is_sensor_allowed` does
  `self.start_hour * 60 + self.start_min`)
- Reached via `src/powerpetdoor/simulator/protocol.py:526-535`
  (`_handle_set_schedule` → `Schedule.from_dict(schedule_data)`, wire command
  `CMD_SET_SCHEDULE`) and evaluated later at
  `src/powerpetdoor/simulator/engine.py:281`
  (`state.is_sensor_allowed_by_schedule(sensor)`).

`Schedule.from_dict` copies attacker-controlled JSON straight into the dataclass
with `data.get(...)` and no shape/range checks:

```python
days = data.get(FIELD_DAYSOFWEEK, [1, 1, 1, 1, 1, 1, 1])
if isinstance(days, int):
    days = [(days >> i) & 1 for i in range(7)]
# ... stored as-is otherwise (any length, any element type)
start_hour=start.get(FIELD_HOUR, 6),   # may be a string, etc.
```

A door-protocol client can send `CMD_SET_SCHEDULE` with, e.g.,
`{"schedule": {"index": 0, "inside": true, "daysOfWeek": [1]}}`. The malformed
schedule is stored successfully (the `SET_SCHEDULE` response is a normal
success). Later, when a sensor trigger causes schedule evaluation with
`auto` enabled (the default) and at least one schedule present,
`is_day_active(weekday)` indexes `days_of_week[day_index]` with `day_index` up
to 6 and raises `IndexError`. A non-numeric `hour`/`minute` similarly yields a
`TypeError` in the minutes arithmetic. **Verified at runtime:**
`Schedule.from_dict({"index":0,"inside":True,"daysOfWeek":[1]}).is_day_active(5)`
raises `IndexError: list index out of range`.

**Attack scenario:** A LAN attacker connects to the simulator door port
(`0.0.0.0:3000` by default) and issues a `SET_SCHEDULE` with a truncated
`daysOfWeek` (or a string `hour`). The daemon accepts it. When the operator (or
a running script) subsequently triggers a sensor, the sensor action silently
fails and a traceback is logged instead of the door cycling — a quiet,
persistent denial of the sensor-trigger feature (for the affected day/time)
that survives until schedules are cleared.

**Impact is contained** and that is why this is Low, not higher:
- Schedule *evaluation* is only reached through `engine.trigger_sensor` /
  `activate_sensor`, which are driven from the **operator side** (simulator
  CLI/simulation commands, scripts). A connected door-protocol client cannot
  itself invoke schedule evaluation — its `OPEN`/`CLOSE` handlers never touch
  it — so the exception fires on operator action, not on the attacker's socket.
- Every trigger entry point is wrapped in a `try/except`
  (`CommandHandler.execute` at `handler.py:341-346`,
  `ScriptRunner.run`/`_execute_step` at `scripting.py:276-278`), so the
  exception degrades one action and is logged; it never crashes the daemon,
  leaks data, or executes code.
- This affects only the **simulator** (a developer/test tool), not the client
  library shipped to Home Assistant.

**Recommendation:** Validate in `Schedule.from_dict`: coerce `daysOfWeek` to a
list of exactly 7 ints (pad/truncate/reject), and coerce `index`/`hour`/`minute`
to bounded ints (reject or clamp otherwise). This is the simulator-side twin of
Round-1 finding #5 (client structure validation) and closes the last untrusted
`SET_SCHEDULE` path. Cheap and localized.

---

### 2. [Informational] Door-protocol server still binds `0.0.0.0` by default (now documented)

**Files:** `src/powerpetdoor/simulator/server.py:116`,
`src/powerpetdoor/simulator/cli.py:505,803`, documented at
`docs/simulator.md:82,270-276`.

The door-protocol TCP server still defaults to `--host 0.0.0.0`. This is a
deliberate, defensible choice: the simulator emulates a real LAN device whose
plaintext protocol is unauthenticated by hardware design (out of scope per the
threat model), and the exposure is now **explicitly documented** with a
security note (`docs/simulator.md:270-276`) that tells operators to pass
`--host 127.0.0.1` to restrict it. Critically, the powerful, *our-own-design*
control channel — the surface Round-1 flagged as materially worse than the
device protocol — now defaults to loopback (`127.0.0.1`) with an opt-in
`--control-host` and a loud unauthenticated-warning. No action required; noted
only for completeness. (Round-1 finding #7 recommended "default loopback **or**
document loudly"; the documentation route was taken.)

---

## Round 1 Fix Verification

Each Round-1 finding was re-checked against the current code (read +, where
useful, executed). All hold.

| R1 | Finding | Status | Evidence in current code |
|----|---------|--------|--------------------------|
| 1 | [Med] Unbounded receive buffer (memory DoS) | **Fixed** | Shared `framing.py:extract_frames` enforces `MAX_BUFFER_SIZE = 64*1024` (`framing.py:33,153-159`) and sets `diag.overflow`. Both consumers act on it: client disconnects (`client.py:1340-1345`), simulator drops the connection (`protocol.py:295-303`). Ran an all-`{` 100k stream: `overflow=True`, remainder cleared to 0. |
| 2 | [Med] Control channel unauth + binds `0.0.0.0` | **Fixed** | `DEFAULT_CONTROL_HOST = "127.0.0.1"` (`cli.py:148`); `--control-host` is a separate opt-in flag decoupled from `--host`, with an UNAUTHENTICATED warning in help (`cli.py:854-862`) and in `docs/simulator.md:270-276`. |
| 3 | [Med] ANSI/control-char injection into operator terminals | **Fixed** | Sanitized at the log **source** (`protocol.py:141-151` `sanitize_log_text`, applied to every network-derived log: RX/TX/unknown-cmd/schedule at `protocol.py:292,329,377,397,406,533,544`) **and** at terminal **egress** (`cli.py:_SanitizingFormatter`, `_ControlLogHandler.emit` uses `escape_message(sanitize_text(...))` at `cli.py:164`; OK/ERROR at `cli.py:280`; ctl unescapes+sanitizes LOG/OK/ERROR at `ctl.py:408,410,414`). Newlines escaped so no forged `LOG:` lines. |
| 4 | [Low] Client `IndexError` on non-`{` data | **Fixed** | `find_frame_end` returns `None` (never raises) for non-`{` leading bytes (`framing.py:67-68`); `extract_frames` resyncs by discarding up to the next `{` (`framing.py:130-139`). Ran `find_frame_end("hello")` → `None`; garbage-prefixed stream framed correctly. |
| 5 | [Low] Client trusts response structure (KeyError/hang) | **Fixed** | `process_message` guards `isinstance(dict)`, reads `msg.get(FIELD_CMD)`/`.get(FIELD_SUCCESS)`, drops malformed envelopes, isolates handler exceptions, and **always** completes the paired future with `CommandError` rather than hanging (`client.py:1347-1422`). (Simulator-side twin remains — Finding 1 above.) |
| 6 | [Low] Arbitrary-path YAML read via control `run` | **Fixed** | Daemon sets `allow_script_paths=False` (`cli.py:607-614`); `_load_script_restricted` rejects `/`, `\`, and leading `.` (`scripts.py:46-53`); `_load_script_by_name` resolves against the base dir and enforces `candidate.parent == base` after `.resolve()` (defeats symlink/`..` escape) (`scripts.py:55-64`). Ran probes `/etc/passwd`, `../../etc/passwd`, `..`, `.ssh/config`, `foo/bar`, `back\slash` — **all blocked** with `ValueError`. The `run ... wait` path also flows through `load_script`, so it is equally restricted. |
| 7 | [Low] Door server `0.0.0.0` undocumented | **Fixed** (documented) | Security note added at `docs/simulator.md:270-276`; see Informational #2. |
| 8 | [Low] GitHub Actions on mutable tags | **Fixed** | All third-party actions SHA-pinned. Publish/OIDC path verified upstream: `actions/checkout@11d5960…`=v4.4.0, `astral-sh/setup-uv@37802ad…`=v7.6.0, `pypa/gh-action-pypi-publish@dc37677…`=v1.14.2. Test-path pins also resolve: `codecov/codecov-action@b9fd7d1…`=v4.6.0, gitea artifact actions pinned to SHAs. Composite `setup-uv-with-retry` pins the SHA too. Only the **first-party** org reusable workflow stays on `@v1.1.0` (`.gitea/workflows/sync-wiki.yml`) — exactly the exception Round-1 permitted. |
| 9 | [Info] History files default umask | **Fixed** | `_create_private_file` (`history.py:20-28`) `os.open(..., 0o600)` + `os.chmod(..., 0o600)` before `FileHistory` touches the file (`history.py:79-85`), tightening existing files too. |

## Areas Reviewed With No Findings

- **`async-timeout` removal is clean.** No `async-timeout`/`async_timeout`
  reference remains in `src/`, `pyproject.toml`, or `uv.lock`. `client.py:1035`
  uses the stdlib `asyncio.timeout`, consistent with `requires-python >= 3.11`.
  No stale import, no dead dependency.
- **YAML loading is safe.** `scripting.py:134` uses `yaml.safe_load`; repo-wide
  there is no `yaml.load`/`full_load`/`unsafe_load`. A malicious script yields at
  most a `ScriptError`, never code execution. `from_yaml` requires a top-level
  mapping and validates step shapes (`scripting.py:137-158`).
- **No dangerous execution sinks.** Repo-wide grep for `eval(`, `exec(`,
  `pickle`, `subprocess`, `os.system`, `shell=True`, `__import__` in `src/`
  returns nothing.
- **STATUS broadcast leaks nothing sensitive.** `ControlChannel.broadcast_status`
  emits only `STATUS: clients=<n>` (an integer count) (`cli.py:246-257`); ctl
  parses it with a guarded `int()` (`ctl.py:393-404`). No PII, no secrets.
- **DoorMotionEngine hooks are safe.** Status listeners and `wait_for` waiters
  are isolated with `try/except` and never propagate caller exceptions
  (`engine.py:143-158`); tasks are tracked, cancelled, and awaited on stop
  (`engine.py:537-567`). `hold_time` flows into deadline math but is bounded by
  the CLI arg spec (0.1–900s, `settings.py:124-147`) and, from the wire
  (`SET_HOLD_TIME` centiseconds), only affects the simulator's own timers.
- **Client cannot inject into the wire.** Outgoing messages are built as dicts
  and `json.dumps(...).encode("ascii")` (`client.py:1308`); field values cannot
  break framing.
- **Outbound framing / decode is defensive.** Both sides decode with
  `data.decode("ascii")` guarded by `try/except UnicodeDecodeError`
  (`client.py:1323-1327`, `protocol.py:284-289`); non-ASCII chunks are dropped,
  not fatal.
- **Command-argument parsing enforces types/ranges** (`commands/base.py`
  int/float/choice/bool/time/day with min/max), so operator/ctl input is bounded
  before it reaches state.
- **No secrets at rest or in logs.** The device protocol carries no
  credentials/keys/PII; nothing sensitive is stored or logged by library or
  simulator. CI publishing uses OIDC trusted publishing (no long-lived PyPI
  token in secrets); `CODECOV_TOKEN`/`GH_PAT` are referenced but never echoed.
