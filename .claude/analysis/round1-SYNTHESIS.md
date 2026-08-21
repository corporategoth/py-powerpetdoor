# Round 1 Synthesis — Adjudicated Decisions and Fix Plan

Consolidates the four round-1 persona reports (backend-developer, frontend-developer,
security-analyst, test-fanatic). Every fix agent MUST follow the decisions below —
they resolve conflicts between reports and set project-wide direction.

## Totals

| Persona | Critical | High | Medium | Low | Trivial/Info |
|---------|----------|------|--------|-----|--------------|
| Backend | 1 | 4 | 12 | 21 | 7 |
| Frontend | 0 | 4 | 4 | 10 | 6 |
| Security | 0 | 0 | 3 | 5 | 1 |
| Test Fanatic | 5 | 11 | 8 | 5 | 2 |

## Adjudicated Cross-Cutting Decisions

### D1. Wire framing (backend H3/L5/L15; security 1/4; test C4/C5/H8)
Create ONE shared frame scanner in a new module `src/powerpetdoor/framing.py`, used by
both `client.py` and `simulator/protocol.py`:
- String-aware brace matching (braces inside JSON strings, incl. escape handling).
- Tolerates leading whitespace/newlines between messages.
- Resyncs on garbage: discards non-`{` prefix up to the next `{`, logs a warning.
- Hard cap on the un-parsed buffer (64 KiB). On overflow: log + clear buffer +
  (client) drop connection / (simulator) drop that client connection.
- Never raises on arbitrary input.
`docs/protocol.md`'s "newline-terminated" framing claim is corrected to describe
reality: messages are JSON objects, optionally separated by whitespace; receivers
must frame by brace matching.

### D2. Protocol ground truth (backend M4/M5/L8)
Where the battle-tested client and the simulator AGREE and `docs/protocol.md`
disagrees, the CODE is ground truth (it runs against real devices via
ha-powerpetdoor) — fix the doc. Specifically:
- Settings key is `holdOpenTime` → fix protocol.md (`holdTime` claim is wrong).
- Booleans/success on the wire are STRINGS (`"true"`, `"1"`) → fix protocol.md's
  examples that show bare JSON booleans.
Notifications (three-way divergence): the client must parse BOTH the protocol.md
bare envelope (`{"SENSOR_INDOOR": "", "sensorState": "on"}`, no CMD/success) AND
tolerate CMD-style without crashing; it gains a notification listener API. The
simulator emits the protocol.md-documented bare format (docs are the spec for
device-observed behavior the client itself doesn't generate). Unknown commands get
`success: "false"` + `reason` per protocol.md's error envelope.

### D3. Defensive message parsing (backend M3; security 5)
ALL network data is untrusted. `process_message` and every handler use `.get()` for
envelope/optional fields, wrap handler dispatch in try/except that logs and FAILS the
paired future (`set_exception`), and isolate each listener callback in its own
try/except. Missing/malformed fields must never kill the processing task or strand a
future.

### D4. Sensor-listener callback signature (backend M2)
Standardize on `(field, value)`. Fix `client.py` type annotations, fix
`docs/client.md` examples, remove the `*args` shims in `door.py`.

### D5. Event loop handling (backend C1/H1)
`loop=None` resolves lazily via `asyncio.get_running_loop()` at `connect()`/
`start()` time; only the blocking `start()` path may create a private loop. No bare
`except:` anywhere — catch specific exceptions, let `CancelledError` propagate,
always log with the exception (`exc_info` where a traceback helps).

### D6. Client lifecycle (backend H2/H4/M1/M6/M7/M10/L1-L4)
Reconnect task is tracked and cancelled on `stop()`/`disconnect()`; `connect()`/
`reconnect()` guard on `_shutdown`. Dropped-message futures get
`set_exception(TimeoutError(...))`; disconnect failures use a typed exception, not
`cancel()`. Client gains public `shutdown()` API; `door.py` stops poking `_shutdown`
and `del_handlers` uses `pop(..., None)`. `door.connect()` uses an event (not
polling) and raises `ConnectionError`/`TimeoutError` on failure; reconnects trigger a
`refresh()` so cache never serves stale state.

### D7. Simulator network exposure (security 2/7)
The control channel (daemon mode) binds `127.0.0.1` by DEFAULT; an explicit
`--control-host` flag is required to widen it (documented with a warning). The door
server keeps `0.0.0.0` (it simulates a LAN device) but the docs state the exposure
plainly. Control-channel `run` accepts only bare script names (no path separators,
no traversal) resolved against the built-in scripts dir + the configured script dir;
arbitrary paths remain available only to the local interactive CLI (security 6).

### D8. Terminal output hygiene (security 3)
Any network-derived string that reaches a terminal (logs at WARNING+, `LOG:`
broadcast lines, ctl/cli prints) is sanitized of C0/C1 control characters (incl.
ESC) first. Newlines in network data must not be able to forge extra log/protocol
lines.

### D9. Deterministic testing hooks (test H3/M5)
The simulator gains first-class event hooks (e.g. status-change callbacks/awaitable
events) so tests synchronize on state transitions instead of `asyncio.sleep()`.
Existing sleep-based test waits are migrated as tests are touched; NEW tests must
not sleep-and-hope.

### D10. Python support matrix (backend T5)
Project decision: 3.11–3.14 (per user). CLAUDE.md's version rule is scoped to "the
declared support matrix" rather than "all non-EOL CPython". `async-timeout` dep is
dropped in favor of stdlib `asyncio.timeout` (backend L19).

### D11. CI action pinning (security 8)
Third-party/remote actions in our workflows are pinned to full commit SHAs (with
version comments). Handled directly by the coordinator, not an agent.

## Fix Waves (file ownership — no two concurrent agents share a file)

- **Wave 1a — Client framing/parsing**: `framing.py` (new), `client.py` (framing,
  process_message, notifications), `const.py`, `docs/protocol.md`, framing tests +
  `tests/fuzz/` hypothesis suite. (D1, D2, D3, D4-annotations, security 1/4/5,
  backend H3/M3/M11/L5/L8/L10/L11-client/L17-client/T1, test C4/C5, part of H6.)
- **Wave 1b — Terminal front end**: `cli.py`, `ctl.py`, `prompt_common.py`,
  `commands/*`. (D7, D8-ctl-side, frontend H4/M1/M2/M3/M4/L1-L10/T1/T2/T3/T5/T6,
  security 2/3/6.)
- **Wave 1c — Docs & exports**: `README.md`, `docs/simulator.md`, `docs/client.md`,
  `docs/door.md`, `src/powerpetdoor/__init__.py`, `.claude/CLAUDE.md` fixes.
  (Frontend H1/H2/H3, backend L6/L7/T3, test T1.)
- **Wave 2a — Client/door lifecycle**: `client.py`, `door.py`, `docs/door.md`,
  `docs/client.md` lifecycle sections. (D5, D6, backend C1/H1/H2/H4/M1/M2-shims/
  M6/M7/M10/L1/L2/L3/L4/L9/L19/L20/T4, test C2/C3/H1/H4/H5.)
- **Wave 2b — Simulator core**: `server.py`, `protocol.py`, `state.py`,
  `scripting.py`. (D1-integration, D2-emit, D8-server-side, D9, backend M5/M8/M9/
  M12/L14/L15/L21/T2/T6, security 3-server-side, test H8.)
- **Wave 3 — Coverage to 100%** (parallel by module group): commands/*, cli+
  prompt_common, ctl, core-library gaps + test-quality fixes + fuzz expansion.
- **Wave 4 — mypy + final lint + gates.**
- **Wave 5 — Persona round 2** (repeat until no non-trivial findings, zero security
  findings).
