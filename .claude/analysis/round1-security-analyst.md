# Security Analyst Analysis — Round 1

Scope: `pypowerpetdoor` — an asyncio client library for Power Pet Door devices
plus a full device **simulator** (TCP door protocol + control channel + YAML
scripting + interactive CLI/ctl).

Threat model used throughout:

- **Attacker on the LAN** who can open TCP connections to a running simulator
  (door port 3000 and/or the daemon control port).
- **Malicious / compromised door device** that a `PowerPetDoorClient` /
  `PowerPetDoor` has connected to, which returns hostile bytes.
- **Malicious / untrusted YAML script file** fed to the simulator.

The device wire protocol itself is plaintext JSON-over-TCP with no auth/crypto —
a fixed constraint of real hardware. Findings below are about how the
library/simulator *handle that reality*, not demands to encrypt the device
protocol.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 3 |
| Low | 5 |
| Informational | 1 |

- **Medium:** unbounded receive buffer (memory-exhaustion DoS); unauthenticated
  control channel bound to `0.0.0.0` by default; ANSI/control-character
  injection from network data into operator terminals.
- **Low:** client crash on non-`{` bytes from a malicious door; client trusts
  door response structure (KeyError on malformed responses); arbitrary-path YAML
  read via the control-channel `run` command; door server binds `0.0.0.0`
  without documenting the exposure; GitHub Actions pinned to mutable tags.
- **Informational:** history files written with default umask permissions.

---

## Findings

### 1. [Medium] Unbounded receive buffer — memory-exhaustion DoS from a malicious peer

**Files:**
- `src/powerpetdoor/simulator/protocol.py:194-231` (`data_received` / `_find_json_end`)
- `src/powerpetdoor/client.py:1014-1038` + `find_end` at `client.py:167-196`

Both the simulator protocol handler and the client accumulate received bytes
into an in-memory buffer (`self.buffer` / `self._buffer`) and only trim it when a
*complete* JSON object (balanced `{...}`) is framed. There is no maximum buffer
size and no cap on how long an incomplete object may remain.

In the simulator, `_find_json_end` returns `None` for input that either never
closes its braces (`"{{{{{{..."`) or does not start with `{` at all (`"AAAA..."`);
in both cases `data_received` `break`s **without clearing the buffer**, so the
data is retained and every subsequent packet is appended to it. In the client,
an all-`{` stream makes `find_end` return `None` (parens never return to 0), the
`while end:` loop never runs, and `self._buffer` grows without bound.

**Attack scenario:** An attacker on the LAN connects to the simulator's door port
(`0.0.0.0:3000`, see Finding 2/4) and streams `"{"` bytes indefinitely — or a
compromised door streams the same to a connected client. The process buffers
every byte forever, exhausting memory until it is OOM-killed. No authentication
or valid protocol handshake is required.

**Recommendation:** Impose a hard cap on buffer size (e.g. reject/disconnect once
the un-parsed buffer exceeds a few KB — real door messages are small). When the
buffer is non-empty but no object can be framed and the leading byte is not `{`,
discard up to the next `{` (or drop the connection) instead of retaining junk.
Apply the same cap symmetrically in `client.py` and `protocol.py`.

---

### 2. [Medium] Simulator control channel is unauthenticated and binds to `0.0.0.0` by default

**Files:** `src/powerpetdoor/simulator/cli.py:305-308` (control server bind),
`cli.py:269-303` (`handle_control_client`), `cli.py:127-141` / `cli.py:581-585`
(`host` defaults to `0.0.0.0`), `src/powerpetdoor/simulator/commands/control.py:21-25`
(`shutdown`).

In daemon mode the control server is bound to the **same `host` as the door
server**, which defaults to `0.0.0.0`:

```python
control_server = await asyncio.start_server(handle_control_client, host, control_port)
```

`handle_control_client` reads newline-delimited commands and dispatches them
straight into `CommandHandler.execute` with **no authentication whatsoever**. The
control surface is powerful: `shutdown` stops the daemon, plus power/lockout/
schedule/battery state changes and `run <script>` (Finding 6). While `ppd-simulator-ctl`
*defaults* to connecting on `127.0.0.1`, the *server* it talks to is exposed on
all interfaces, so any host on the LAN can drive it.

**Attack scenario:** A developer starts `ppd-simulator --daemon` on a workstation
or CI host. Anyone on the same network connects to `PORT+1` and issues
`shutdown`, flips `power off`, or runs scripts — a remote, unauthenticated
management interface. This is materially worse than the door protocol itself,
because the door protocol mirrors real, unavoidable hardware behavior whereas the
control channel is entirely our own design.

**Recommendation:** Default the control channel bind to `127.0.0.1` (decouple it
from `--host`, or add a `--control-host` defaulting to loopback). If remote
control is genuinely wanted, require an explicit opt-in flag and document that it
is unauthenticated. At minimum, document loudly that `--host 0.0.0.0` also
exposes the control port.

---

### 3. [Medium] ANSI / control-character injection from network data echoed to operator terminals

**Files:**
- `src/powerpetdoor/simulator/protocol.py:291` (`logger.warning(f"Simulator: Unknown command: {cmd}")`)
- `src/powerpetdoor/simulator/protocol.py:197` (`logger.debug(f"Simulator RX: {text}")`)
- `src/powerpetdoor/simulator/cli.py:249-267` (`ControlClientLogHandler` broadcasts `LOG: {msg}` with no escaping)
- `src/powerpetdoor/simulator/ctl.py:366-368` (`print(decoded[5:])` — raw to terminal)

Incoming door-protocol data is decoded as ASCII (`data.decode("ascii")`), which
**accepts C0 control bytes including ESC `0x1B`**. The parsed command string
(`cmd`, an attacker-controlled JSON string value) is logged verbatim at WARNING
level whenever it is unrecognized, and the raw RX text is logged at DEBUG level.
These log records are then:

- broadcast to every control client as `LOG: {msg}\n` (`ControlClientLogHandler.emit`)
  with **no escaping** (contrast the OK/ERROR path at `cli.py:287`, which at least
  escapes `\n`/`\\`), and
- printed to the interactive CLI operator's terminal via the prompt logging
  handler, and to the `ctl` operator via `print(decoded[5:])`.

Because nothing strips or escapes ESC/CSI sequences, a LAN attacker can inject
terminal escape sequences (cursor moves, screen clears, color/title changes,
and on some terminals clipboard or key-remap sequences) into the operator's
console. A JSON string value may also contain an embedded `\n`, letting the
attacker forge additional `LOG:` lines into the control protocol stream that
`ctl` reads line-by-line.

**Attack scenario:** Attacker connects to `0.0.0.0:3000` and sends
`{"cmd":"[2J[1;1H<forged text>","dir":"p2d"}`. The daemon logs
"Simulator: Unknown command: <ESC…>", broadcasts it, and the maintainer running
`ppd-simulator-ctl -i` has their terminal cleared/rewritten — a foothold for
spoofing output or worse on vulnerable terminal emulators. Reaches the WARNING
path even without debug logging enabled.

**Recommendation:** Sanitize untrusted strings before logging/printing — strip or
escape non-printable/control characters (e.g. replace bytes `< 0x20` except plain
whitespace, or `repr()` the value) at the point where network-derived data enters
a log message. Apply the same escaping the OK/ERROR path uses (extended to
control chars, not just `\n`/`\\`) to the `LOG:` broadcast, and have `ctl`
sanitize before `print`.

---

### 4. [Low] Client raises an uncaught `IndexError` on non-`{`-prefixed data from a malicious door

**File:** `src/powerpetdoor/client.py:1026` and `client.py:1038` call `find_end`
outside the surrounding `try`; `find_end` (`client.py:183-184`) raises
`IndexError("Block does not start with '{'")` when the buffer does not start with
`{`.

After a complete object is consumed, `self._buffer = self._buffer[end:]`; if the
remaining bytes begin with anything other than `{` (a stray separator, whitespace,
or attacker padding), the next `find_end` raises `IndexError`, which propagates
out of the asyncio `data_received` callback uncaught (only the `decode` step is
wrapped in `try/except`).

**Attack scenario:** A malicious/compromised door sends a single leading non-`{`
byte (e.g. `" "` or `"x"`), or a valid object followed by a separator. The
exception tears down the client's receive path/connection; the reconnect loop
re-establishes and the peer repeats, producing a persistent
disconnect/reconnect nuisance DoS. The simulator's `_find_json_end` handles this
case gracefully (returns `None`), so the asymmetry confirms the client path is a
defect.

**Recommendation:** Make `find_end` return `None` (not raise) for a non-`{`
leading byte, and have `data_received` skip/trim leading junk up to the next `{`.
Guard the `find_end` calls so malformed framing can never escape the callback.

---

### 5. [Low] Client trusts door response structure — missing fields cause uncaught `KeyError`

**File:** `src/powerpetdoor/client.py:1040-1072` (`process_message`), notably
`msg[FIELD_CMD]` at `client.py:1051` and `msg[FIELD_SUCCESS]` at `client.py:1060`,
plus the per-handler direct field accesses (e.g. `_handle_battery` reads
`msg[FIELD_BATTERY_PERCENT]` at `client.py:686`).

`process_message` indexes `msg["CMD"]` and `msg["success"]` directly with no
membership check, and handlers assume their fields are present and well-typed. A
malicious door can return a syntactically valid JSON object that omits these keys
(or supplies wrong types), producing an uncaught `KeyError`/`TypeError` inside
the `ensure_future(self.process_message(...))` task. The exception is swallowed by
the task machinery (logged by asyncio), and the corresponding outstanding future
may never be resolved, potentially hanging callers waiting on it until timeout.

**Attack scenario:** Compromised door replies to a command with `{"dir":"d2p"}`.
The client task dies before resolving the pending future; the caller's
`await asyncio.wait_for(...)` only unblocks on timeout, and repeated malformed
replies keep the receive tasks failing.

**Recommendation:** Validate response shape before dispatch: check for `CMD`/
`success` presence and expected types, log-and-drop malformed messages, and fail
the matching outstanding future with an exception so callers unblock promptly
rather than hanging to timeout.

---

### 6. [Low] Arbitrary-path YAML read via the control-channel `run` command (path traversal)

**Files:** `src/powerpetdoor/simulator/commands/scripts.py:28-34` (`load_script`),
`src/powerpetdoor/simulator/scripting.py:144-148` (`Script.from_file` →
`path.read_text()`), reachable remotely via `cli.py:285` (`cmd_handler.execute`).

`load_script` treats the argument as a filesystem path first: `Path(script_ref)`;
`if path.exists(): return Script.from_file(path)`. There is no restriction to the
bundled scripts directory, so a control client can request the daemon read **any
path on the daemon's filesystem** (e.g. `run /home/user/secret.yaml`,
`run ../../etc/something`). Combined with the unauthenticated `0.0.0.0` control
channel (Finding 2), this is remotely reachable.

Impact is bounded by `yaml.safe_load` (no code execution — see "Areas Reviewed")
and by `from_yaml` requiring a top-level mapping, so it is not a clean
arbitrary-file-read primitive; but it still lets a remote party probe for file
existence and load attacker-chosen/off-tree YAML into the running daemon, and
error text can echo parse fragments of targeted files.

**Attack scenario:** LAN attacker connected to the daemon control port issues
`run /path/to/some.yaml` to confirm a file exists and execute an off-tree script,
or to trigger YAML parsing of files outside the intended scripts directory.

**Recommendation:** Resolve built-in script names against the bundled
`scripts/` directory only; for file paths, require an explicit opt-in and confine
resolution (reject paths that escape an allowed base after `Path.resolve()`).
Given the control channel has no auth, prefer restricting `run` over the wire to
built-in names.

---

### 7. [Low] Door server binds `0.0.0.0` by default with no documented exposure warning

**Files:** `src/powerpetdoor/simulator/server.py:112-114` (`host="0.0.0.0"`
default), `src/powerpetdoor/simulator/cli.py:582-585` (`--host` default
`0.0.0.0`), `docs/simulator.md:189` (documents `0.0.0.0` without a risk note).

The simulator door server defaults to listening on all interfaces. Unlike a real
device (which must listen on the network), the simulator is a developer/testing
tool that in the common case only needs loopback. Binding `0.0.0.0` by default
maximizes the reachable surface for Findings 1 and 3 and is not accompanied by any
security note in the docs.

**Attack scenario:** A developer runs the simulator on a laptop joined to an
untrusted network (coffee shop, conference Wi-Fi); the door port — and its
unbounded-buffer and ANSI-injection issues — is reachable by anyone on that
network without them realizing it.

**Recommendation:** Default the bind to `127.0.0.1` and require `--host 0.0.0.0`
as an explicit choice, or prominently document in `docs/simulator.md` that the
default exposes the simulator (and, in daemon mode, the control port) to the
whole LAN with no authentication.

---

### 8. [Low] GitHub Actions pinned to mutable tags/branches, including the OIDC publish job

**Files:** `.github/workflows/release.yml:18-21,39-47` (`astral-sh/setup-uv@v7`,
`pypa/gh-action-pypi-publish@release/v1` in a job with `id-token: write`),
`.github/workflows/test.yml` (`actions/checkout@v4`,
`christopherhx/gitea-upload-artifact@v4`, `codecov/codecov-action@v4`),
`.github/actions/setup-uv-with-retry/action.yml` (`astral-sh/setup-uv@v7`).

All third-party actions are referenced by mutable major tags / branch refs
(`@v4`, `@v7`, `@release/v1`). The `publish` job holds `id-token: write` and can
publish to PyPI via OIDC, so a hijacked or force-moved tag on any action in that
job's graph could exfiltrate the short-lived OIDC token or ship a malicious
release. `@release/v1` is a branch (fully mutable).

**Attack scenario:** An upstream action repo/tag is compromised or a maintainer's
credentials are stolen; the moved tag runs attacker code inside the release
pipeline with token-minting permissions.

**Recommendation:** Pin third-party actions to full commit SHAs (with a comment
noting the version), especially every action reachable from the `publish` job.
Keep first-party/reusable workflows on tags if desired, but SHA-pin the
supply-chain-sensitive publish path. Consider adding Dependabot for actions to
keep SHAs current.

---

### 9. [Informational] History files written with default umask permissions

**Files:** `src/powerpetdoor/simulator/commands/history.py:154-160`,
`history.py:181`, `src/powerpetdoor/simulator/commands/info.py:350`;
paths `~/.powerpetdoor_simulator_history`, `~/.powerpetdoor_ctl_history`
(`prompt_common.py:40-41`).

Command history is written with a plain `open(filename, "w")`, so permissions
follow the process umask (typically `0644` — world-readable). The content is only
simulator/ctl command lines (no secrets or PII), so the disclosure risk is
negligible; noted for completeness since these files live in the user's home
directory. If any sensitive argument were ever added to a command, this would
warrant `0600`. No action required now.

---

## Areas Reviewed With No Findings

- **YAML loading is safe.** `scripting.py:113` uses `yaml.safe_load`; there is no
  `yaml.load`/`full_load`/`unsafe_load` anywhere. A malicious YAML script cannot
  execute code — worst case is a `ScriptError` or a benign step. (Path resolution
  for *which* file is loaded is covered by Finding 6.)
- **No dangerous execution sinks.** Repo-wide grep found no `eval`, `exec`,
  `pickle`, `subprocess`, `os.system`, or `shell=True` in `src/` or `tests/`.
- **Dependency floors carry no known CVEs.** `async-timeout>=4.0.0`,
  `tzdata>=2024.1`, `pyyaml>=6.0` (6.x is unaffected by the PyYAML `full_load`
  RCE, and safe_load is used regardless), `prompt_toolkit>=3.0.0`. No
  known-vulnerable version is admitted by these floors. (Floors are open-ended
  `>=` with no upper bound, which is normal for a library; not a vuln.)
- **PyPI publishing uses OIDC / trusted publishing.** `release.yml` publish job
  uses `permissions: id-token: write` + `pypa/gh-action-pypi-publish` with a
  dedicated `pypi` environment — no long-lived API token in secrets. Good
  practice. (The only gap is SHA-pinning the actions — Finding 8.)
- **Command-argument parsing validates types and ranges.** `commands/base.py`
  `parse_arg` enforces int/float/choice/bool/time/day types with min/max bounds
  and rejects malformed values, and `_parse_time_str` bounds hours/minutes.
- **Client output rate-limiting / no obvious injection into the wire.** Outgoing
  messages are built as dicts and `json.dumps`-encoded (`client.py:1008`), so
  client-side field values cannot break protocol framing.
- **Data-at-rest secrets / redaction.** No credentials, keys, tokens, or PII are
  stored or logged by the library or simulator; the device protocol carries none.
- **`.gitea/workflows/sync-wiki.yml`** passes `secrets.GH_PAT` to a first-party
  org reusable workflow pinned at `@v1.1.0`; no secret is echoed or exposed in
  logs. No finding.
