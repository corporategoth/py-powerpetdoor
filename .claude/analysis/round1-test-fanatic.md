# Test Fanatic Analysis — Round 1

## Summary

444 tests pass, but the suite is nowhere near the project's own bar. Total branch coverage
is **52.20%** against a configured `fail_under = 100` — `uv run pytest --cov` cannot
currently pass. The entire terminal front end (`ctl.py` **0%**, `cli.py` **4.6%**,
`prompt_common.py` **11%**, `commands/history.py` **8.7%**) is effectively untested. The
existing tests contain **two outright fake tests** (they test Python's `+=` and `dict`
operators, not production code), multiple overbroad assertions that accept several
contradictory outcomes, and **101 `asyncio.sleep()` calls** used as synchronization —
directly violating the project's own CLAUDE.md test-quality rules 1, 6, and 7.

Worse: writing the *missing* negative and fuzz tests immediately exposes real production
bugs, which I verified by execution:

1. `PowerPetDoorClient.data_received(b"garbage")` raises an unhandled `IndexError`
   (any non-`{` leading byte from the device kills the protocol callback).
2. `find_end()` counts braces inside JSON string literals, so a legal payload like
   `{"a": "}"}` corrupts framing and then crashes on the residual buffer.
3. `PowerPetDoor._on_settings()` coerces protocol strings with `bool()`, so
   `power: "0"` yields `_power = True` (`bool("0") is True`) — masked only because a
   later listener overwrites it, and inverted `cmd_lockout` handling is silently wrong.

`hypothesis` is declared as a dev dependency and used **zero** times. There are no
reconnect tests, no concurrency tests, no client-side protocol-violation tests, and the
one keepalive-failure test is fake. This suite gives false confidence.

Finding counts: **Critical: 5, High: 11, Medium: 8, Low: 5, Trivial: 2** (31 total).

---

## Findings

### Critical

---

#### C1. The 100% coverage gate is failing: the entire CLI front end is untested (ctl 0%, cli 4.6%, prompt_common 11%)

**Severity:** Critical
**Files:** `src/powerpetdoor/simulator/ctl.py` (0%), `src/powerpetdoor/simulator/cli.py` (4.6%),
`src/powerpetdoor/simulator/prompt_common.py` (11%), `pyproject.toml:84` (`fail_under = 100`)

The project's only user-facing front end has essentially no tests. Every one of these is
testable without a TTY. Required tests, per module:

**`ctl.py` — all pure/near-pure, start here (fastest coverage win):**
- `LocalCommandHandler.is_local_command()`: assert `True` for `"help"`, `"?"`, `"exit"`,
  `"clear"`, `"history"`; `False` for `"status"`, `"inside"`, `""`, `"unknowncmd"`
  (ctl.py:73–104). No I/O needed — construct `LocalCommandHandler(history=None)`.
- `LocalCommandHandler.execute()`: empty line → `(False, "Empty command")`; unknown →
  `"Unknown command: xyz"` (exact text); `"exit"` → `exit_ctl=True` and empty message
  (the `__EXIT_CTL__` marker path, ctl.py:189–191); `"history"`/`"history clear"`/
  `"history 5"` with an in-memory `History`; subcommand help path (`"history help"`);
  missing-required-arg and bad-arg paths of `_parse_args` (ctl.py:195–219) with exact
  `"Missing required argument: …\nUsage: …"` text.
- `send_command()` / `check_connection()` (ctl.py:226–303): run against a real in-process
  `asyncio.start_server` stub that replies `OK: line\n` / `ERROR: msg\n`; assert the
  unescaping of `\\n` → newline (ctl.py:267–271); assert `ConnectionRefusedError` branch
  returns `(False, "Connection refused to 127.0.0.1:<port>")` by using a closed port.
- `interactive_mode_async` `socket_reader` routing (ctl.py:349–407): feed a stub control
  server that emits `LOG: x\n`, `OK: ok\n`, `ERROR: bad\n`, `OK: Clients: none\n` and
  assert log lines print, responses land in `response_queue`, and `has_clients[0]`
  toggles on `"Client connected"` / `"Clients: none"`. Refactor recommendation: extract
  `socket_reader` and `send_command_async` from the closure into module-level
  functions/classes taking explicit state, so they are directly testable.
- `interactive_mode_basic()` (ctl.py:557–615): monkeypatch `builtins.input` with a
  scripted sequence (`["status", "exit"]` and `["shutdown"]`), stub `send_command`,
  assert loop exit conditions and `OK:`/`ERROR:` prefix stripping (ctl.py:601–608).
- `main()` argument parsing (ctl.py:630–696): monkeypatch `sys.argv`; assert no-args →
  `SystemExit(1)` with help printed; `--door-port` default = `port - 1` (ctl.py:682);
  one-shot command joins argv words.

**`cli.py`:**
- `InteractivePrompt` (cli.py:37–104): unit-test `enable()` swaps root logging handlers
  and `disable()` restores them exactly; `show()`/`clear_line()`/`output()` write the
  expected escape sequences to a captured `sys.stdout` (capsys); `output("x")` produces
  `\r\033[K` + `x\n` + prompt. `_PromptLoggingHandler.emit` clears/reprints prompt.
- `main()` (cli.py:573–744): monkeypatch `sys.argv` + stub `run_simulator`; assert
  `--script` + `--daemon` → `parser.error` (`SystemExit(2)`); `--firmware 1.2` and
  `--firmware a.b.c` → `parser.error` with the exact message (cli.py:704–711);
  `--hardware 1` → error; `--daemon` bare → `control_port == port + 1`, `--daemon 4001`
  → `4001` (cli.py:697–698); `--list-scripts` prints all built-ins and returns without
  running; `--oneshot` exit code 0/1 mapping (cli.py:739–740).
- `run_simulator` end-to-end in daemon mode (cli.py:127–570): start
  `asyncio.create_task(run_simulator(host="127.0.0.1", port=0-ish, daemon=True,
  control_port=<free port>))`, connect with `asyncio.open_connection`, send `status\n`,
  assert reply starts `OK: ` and contains `Door:`; send an unknown command, assert
  `ERROR: `; send `shutdown\n`, assert the task completes and returns `None`. This one
  test covers the control-server closure (cli.py:247–315) including the newline escaping
  at cli.py:287 (verify by sending `help`, whose multi-line output must arrive as one
  `OK:` line with `\n` escapes).
- Script mode: `run_simulator(scripts=["basic_cycle"], oneshot=True)` returns `True`;
  with a deliberately failing inline script file (tmp_path YAML asserting a wrong
  battery value) returns `False`. `--wait-for-client` path: start with
  `wait_for_client=True`, assert scripts don't start until a TCP client connects
  (cli.py:347–353). `process_script_queue`: push a name via the `script` queue, assert
  it runs (cli.py:318–336).
- The prompt_toolkit interactive branch (cli.py:428–483) can be driven with
  `prompt_toolkit.input.create_pipe_input()` + `DummyOutput` inside
  `create_app_session(...)`: send `"status\r"` then `"exit\r"`, assert `run_simulator`
  returns. If that proves too entangled, extract `interactive_input_loop` so it can be
  tested with a fake `InteractiveSession` — refactor-for-testability is acceptable;
  0% is not.

**`prompt_common.py`:**
- `init_command_sets()` / `get_commands()` / `get_aliases()` (lines 87–116): assert
  known names (`"schedule"`, `"broadcast"`, `"status"`) and aliases (`"bc"`, `"y"`,
  `"sched"`) are present, and `"on"`/`"off"` land in `_OPTIONS`.
- `SimulatorLexer.lex_document` (lines 122–254): build
  `prompt_toolkit.document.Document("schedule add inside 6:00-22:00 weekdays")` and
  assert exact token classes per word: `class:command`, `class:subcommand`,
  `class:number` for `6:00-22:00`, etc.; alias first word (`bc`) → `class:alias`;
  unknown word → plain. This is the terminal-UX equivalent of theming and it is fully
  assertable.
- `SimulatorCompleter.get_completions` (lines 256–414): `Document("sch")` completes to
  `schedule`; `Document("schedule ")` offers `add/list/delete/…` plus `help`;
  `Document("ac ")` offers `connect/disconnect/toggle` and `on/off` arg options;
  `Document("power ")` offers `on`, `off`, `toggle`, `help`.
- `InteractiveSession`: `resolve_history_recall` matrix (`!!`, `!1`, `!-1`, `!0` error,
  `!99` error text `"Only N commands in history"`, `!notanumber` passthrough);
  `handle_result` removes failed commands and canonicalizes aliases in history (verify
  via `History.get_entries()`); `format_output` recall prefix (lines 553–626);
  `create()` prompt callable returns `class:prompt.connected` vs
  `class:prompt.disconnected` depending on `is_connected` (lines 507–517).

Until these exist, the "100% goal" is fiction and `--cov` runs fail.

---

#### C2. Fake test: `test_failed_ping_increments_counter` tests the `+=` operator, not the keepalive

**Severity:** Critical
**File:** `tests/test_client.py:519–530`

```python
client._failed_pings += 1
assert client._failed_pings == initial_failed + 1
```

The test increments the counter itself and then asserts the increment happened. It
cannot fail unless CPython's integer addition breaks. The actual production logic —
`keepalive()` at `src/powerpetdoor/client.py:900–915`, which increments
`_failed_pings` when `_last_ping` is unanswered and calls `disconnect()` at
`MAX_FAILED_PINGS` — has **zero** coverage.

**Recommendation:** Delete this test and replace with real ones:
- `test_keepalive_unanswered_ping_increments_failed_pings`: create the client with a
  tiny `keepalive` (e.g. 0.01), set `_last_ping = "123"`, await `client.keepalive()`
  directly, assert `_failed_pings == 1` and a new PING was enqueued.
- `test_keepalive_disconnects_after_max_failed_pings`: set
  `_failed_pings = MAX_FAILED_PINGS - 1` and `_last_ping` pending, await `keepalive()`,
  assert `client._transport is None` and no PING was sent.
- `test_pong_resets_failed_pings`: set `_failed_pings = 2`, deliver a matching PONG via
  `device.respond_to_ping`, assert `_failed_pings == 0` (covers client.py:800).
- `test_on_ping_callback_receives_latency`: register via `add_handlers(on_ping=...)`,
  respond to a real `_last_ping` timestamp, assert the callback got a non-negative int.

---

#### C3. Fake test: `test_del_listener` never calls `del_listener`

**Severity:** Critical
**File:** `tests/test_client.py:430–444`

The test inserts into `client.door_status_listeners` directly, then executes
`del client.door_status_listeners["test_listener"]` — a plain dict deletion — and
asserts the key is gone. The production method `del_listener()`
(`src/powerpetdoor/client.py:469–500`), which must pop the name from **27 different
listener registries**, is never invoked. The in-test comment "del_listener expects all
listeners to exist" is also false: the implementation uses `.pop(name, None)`
everywhere and is explicitly documented as safe.

**Recommendation:** Replace with:
- `test_del_listener_removes_from_all_registries`: `add_listener("x",
  door_status_update=cb, sensor_update={"*": cb}, notifications_update={"*": cb},
  stats_update={"*": cb}, battery_update=cb, schedule_update=cb, schedule_delete=cb,
  ...)`, call `client.del_listener("x")`, then assert `"x"` absent from every one of the
  registries (`door_status_listeners`, all 7 `sensor_listeners` sub-dicts, all 5
  `notifications_listeners` sub-dicts, both `stats_listeners` sub-dicts, etc.).
- `test_del_listener_unknown_name_is_noop`: `del_listener("never_added")` does not raise
  and leaves other listeners intact (the documented contract).

---

#### C4. No client-side negative protocol tests — and `data_received` crashes on garbage (verified bug)

**Severity:** Critical
**Files:** `src/powerpetdoor/client.py:1014–1038` (`data_received`), `client.py:167–196`
(`find_end`), `client.py:1040–1072` (`process_message`); no corresponding tests in
`tests/test_client.py`

Verified by execution: `client.data_received(b"garbage not json")` raises an unhandled
`IndexError` from `find_end` (the bare `except:` at client.py:1022 only wraps the ASCII
decode). On a real socket this exception escapes an asyncio protocol callback: one
malformed or misaligned chunk from the device permanently poisons `_buffer` — every
subsequent `data_received` re-raises because the buffer no longer starts with `{`.
Additional unguarded paths: `process_message` does `msg[FIELD_CMD]` (client.py:1051) and
`msg[FIELD_SUCCESS]` (client.py:1060) — a JSON object missing `CMD` or `success` raises
`KeyError` inside the task. There is not a single test that feeds the client a malformed,
truncated-then-garbage, non-object, non-ASCII, or field-missing message.

**Recommendation:** Add a `TestClientProtocolViolations` class (these tests will fail
until the production code is hardened — that is the point):
- `test_garbage_bytes_do_not_raise_and_are_discarded` — `data_received(b"hello")`, no
  exception, `_buffer == ""`.
- `test_garbage_prefix_before_valid_json_recovers` — `b'junk{"success":"true","CMD":"PONG","PONG":"1"}'`.
- `test_non_ascii_bytes_skipped` — `data_received(b"\xff\xfe")` (covers client.py:1022
  branch, currently the only handled case).
- `test_balanced_braces_invalid_json_logged_and_skipped` — `b'{"a" broken}'` exercises
  the `JSONDecodeError` branch at client.py:1035 (currently uncovered).
- `test_message_missing_cmd_field_does_not_crash` and
  `test_message_missing_success_field_does_not_crash` — direct
  `await client.process_message({...})`.
- `test_failure_response_sets_future_exception` — send `success:"false"` with a matching
  `msgID`, assert the future raises `Exception("Command Failed")` (client.py:1070–1071;
  the conftest helper `respond_failure` exists for exactly this and is used by zero
  tests).
- `test_unmatched_msgid_response_ignored` — response for an unknown `msgID` must not
  raise and must not resolve other futures.
- `test_oversized_message_handling` — a 1 MB valid JSON message; define the expected
  behavior and pin it.

---

#### C5. `find_end` corrupts framing when a JSON string contains a brace (verified bug) — no fuzz/property tests exist at all

**Severity:** Critical
**Files:** `src/powerpetdoor/client.py:167–196` (`find_end`),
`src/powerpetdoor/simulator/protocol.py:218–231` (`_find_json_end` — same flaw);
`pyproject.toml:45` (hypothesis dev dep, unused — `grep -r hypothesis tests/` → 0 hits)

Verified by execution: `find_end('{"a": "}"}')` returns 8 (mid-string), the truncated
block fails `json.loads`, and the residual buffer `'"}'` then makes `find_end` raise
`IndexError` on the next call. Both the client and the simulator parser count braces
without tracking string literals. Today's protocol payloads happen not to contain braces
in strings, but timezone strings, schedule names, or any future field can — and the
failure mode is a permanent connection poison (see C4). A single hypothesis round-trip
test would have found this in seconds.

**Recommendation:** Add `tests/test_property.py` (hypothesis is already a dev dep):
- `test_framing_round_trip`:
  `@given(st.lists(st.dictionaries(st.text(min_size=1, max_size=8), st.one_of(st.text(max_size=8), st.integers(), st.booleans()), max_size=4), min_size=1, max_size=5), st.randoms())`
  — serialize each dict with `json.dumps`, concatenate, split the byte stream at random
  points, feed the chunks to `client.data_received` (with `process_message` monkeypatched
  to collect parsed dicts synchronously), assert the collected list equals the input
  list. Run the identical property against
  `DoorSimulatorProtocol.data_received`/`_find_json_end`.
- `test_find_end_never_raises_on_arbitrary_text`: `@given(st.text())` — `find_end`
  must return `None`/an index, never raise (this documents the desired contract; today
  it fails).
- `test_make_bool_total`: `@given(st.text())` result ∈ {True, False, None};
  `@given(st.integers())` result == `(v != 0)` (covers `client.py:199–219`).

---

### High

---

#### H1. `PowerPetDoor._on_settings` misparses protocol "0"/"1" strings — latent bug with no direct unit test

**Severity:** High
**File:** `src/powerpetdoor/door.py:1120–1143`; masked in
`tests/test_door.py` integration tests

Verified by execution: `door._on_settings({"power": "0", "inside": "0",
"cmd_lockout": "0"})` leaves `door.power is True`, `door.inside_sensor is True`, and
`door.pet_proximity_keep_open is False` (should be `True` — lockout `"0"` means
keep-open enabled per the inversion at door.py:1136–1137). The cause is `bool("0") is
True`. The bug is currently masked end-to-end because `client._handle_get_settings`
*also* notifies the per-field sensor listeners with `make_bool`-coerced values
(client.py:531–535) which overwrite the wrong values immediately after — a fragile
ordering dependency that no test pins down.

**Recommendation:**
- `test_on_settings_parses_protocol_string_booleans`: call `_on_settings` directly with
  all-string `"0"` values; assert every cached field is `False` (and
  `pet_proximity_keep_open is True`). This test fails today; fix `_on_settings` to use
  `make_bool`.
- `test_on_settings_cmd_lockout_inversion` with `"1"` and `"0"` explicitly.
- `test_refresh_settings_power_off_reflected`: integration — set
  `simulator.state.power = False`, `await door.refresh_settings()`, assert
  `door.power is False` (pins the end-to-end ordering so a future listener reorder
  cannot silently regress).

---

#### H2. Overbroad assertions: tests that accept multiple contradictory outcomes

**Severity:** High
**Files/lines:**
- `tests/test_door.py:285` — `assert door.status in (DoorStatus.RISING, DoorStatus.HOLDING, DoorStatus.KEEPUP)`
- `tests/simulator/test_server.py:87` — `in (DOOR_STATE_RISING, DOOR_STATE_KEEPUP)`
- `tests/simulator/test_server.py:97–102` — accepts **four** states (SLOWING, CLOSING_TOP, CLOSING_MID, CLOSED)
- `tests/simulator/test_server.py:211` and `tests/simulator/test_commands.py:208` — `assert RISING in states_seen or HOLDING in states_seen`
- `tests/simulator/test_commands.py:342` — `assert "Error" in result.message or result.success is False` (accepts either an error message **or** failure — it does not know which)
- `tests/test_tz_utils.py:157` — `assert "GMT" in result or "BST" in result`
- `tests/test_tz_utils.py:174` — `assert "M3" in result or "M11" in result`

Per the project's own CLAUDE.md rule 1: a test that accepts several outcomes does not
know the correct answer. These all stem from asserting *during* a transition instead of
waiting for a defined stable state.

**Recommendation:**
- Wait deterministically for the terminal state and assert exactly one value:
  after `open()` (no hold) with sensor cleared, the stable state is `HOLDING`; with
  `hold=True` it is `KEEPUP`; after close it is `CLOSED`. Use the status-event hook
  proposed in H3 to await "reached HOLDING" instead of sampling mid-flight.
- `test_commands.py:342`: read `ScriptsCommandsMixin.run` and assert the single true
  outcome — `result.success is False` **and** the exact message prefix (`"Error"`/
  `"Unknown script"` — whichever the code actually produces).
- `tz_utils` London: assert the exact string `get_posix_tz_string("Europe/London") ==
  "GMT0BST,M3.5.0/1,M10.5.0"` (pin the tzdata-derived value; if tzdata churn is a
  concern, assert the parsed components exactly: `std_abbrev == "GMT"`,
  `dst_abbrev == "BST"`). Same for New York: assert both `"M3.2.0"` **and**
  `"M11.1.0"` are present, not either-or.

---

#### H3. 101 `asyncio.sleep()` calls used as synchronization; no deterministic event hooks or virtual clock

**Severity:** High
**Files:** pervasive — e.g. `tests/test_door.py:279–283, 304–307, 329, 361, 595, 610`
(blind `sleep(0.2)`/`sleep(0.3)` + 5-second poll loops), `tests/simulator/test_protocol.py`
(every handler test does `await asyncio.sleep(0.05)` after `data_received`),
`tests/simulator/test_server.py:319–398` (battery tests sleep 0.3–0.5 s and assert wall-clock
drift), `tests/simulator/test_scripting.py:286` (stops a script by sleeping 0.25 s),
`tests/simulator/scripts/conftest.py:111–113` (timing config comment admits "rise_time
must be > 0.5s to pass basic_cycle assertions" — the tests are *designed* to race);
root cause in `src/powerpetdoor/simulator/protocol.py:682–689` and `server.py:585–593`
(the hold loop literally polls wall-clock in 0.1 s decrements).

CLAUDE.md rule 7 says "Never sleep-and-hope." Nearly every async test here sleeps and
hopes. Consequences: the suite is slow (worst-case poll loops of 5–10 s each), flaky on
loaded CI runners, and imprecise (see H2 — mid-transition sampling forces overbroad
assertions).

**Recommendation (infrastructure, then mechanical cleanup):**
1. Add a status-change hook to the simulator: an
   `asyncio.Event`-per-state or a `state.on_status_change(callback)` /
   `await simulator.wait_for_status(DOOR_STATE_HOLDING, timeout=2)` helper on
   `DoorSimulatorState`/`DoorSimulator`. The `_broadcast_or_send_status()` call sites
   already centralize every transition — fire the hook there.
2. In `tests/simulator/test_protocol.py`, stop sleeping after `data_received`: capture
   the task created at protocol.py:211 (or refactor `_handle_message` dispatch so tests
   can `await protocol._handle_message(msg)` directly — it is already a coroutine; call
   it directly instead of going through `data_received`).
3. Battery tests: call `_battery_simulation_loop`'s body logic deterministically or
   inject a controllable clock; asserting "percent increased after sleeping 0.3 s" is a
   race. At minimum assert exact arithmetic: with `charge_rate=600`/`update_interval=0.1`,
   one tick adds exactly 1%.
4. `test_client.py`'s `await asyncio.sleep(0.01)` (lines 462, 515, 607) → replace with
   `await asyncio.wait_for(<the actual future/event>, 1.0)`; the client already returns
   futures for exactly this purpose, and `test_client_integration.py`'s `CallbackTracker`
   (events, `wait_for`) is the right pattern — reuse it everywhere.

---

#### H4. `check_receipt` retry machinery and failure-response path: zero coverage

**Severity:** High
**File:** `src/powerpetdoor/client.py:917–934` (`check_receipt`), `client.py:952–978`
(`_send_data` retry/throttle), `client.py:1069–1071` (failure responses)

The timeout-and-retransmit logic (`MAX_FAILED_MSG`, re-sending `rawdata`, dropping after
2 failures) is completely untested, as is the `RuntimeError` → `disconnect()` branch in
`_send_data` and the `MINIMUM_TIME_BETWEEN_MSGS` pacing (client.py:961–963). The conftest
even ships `respond_failure()` and `create_mock_response()` helpers that no test calls.

**Recommendation:** With a small `cfg_timeout` (0.05 s):
- `test_check_receipt_retransmits_on_timeout`: send a COMMAND, don't respond; assert the
  same raw bytes are written twice to `MockTransport.written_data` and
  `_failed_msg == 1` after the first timeout.
- `test_check_receipt_drops_after_max_failures`: never respond; assert after
  `MAX_FAILED_MSG` timeouts the message is dropped (`_failed_msg == 0`) and the next
  queued message is dequeued.
- `test_response_cancels_check_receipt`: respond promptly; assert `_check_receipt` is
  None and no retransmit occurred.
- `test_send_data_runtime_error_disconnects`: make `MockTransport.write` raise
  `RuntimeError`; assert `disconnect()` ran (`_transport is None`).
- `test_minimum_time_between_msgs_pacing`: enqueue two messages, record
  `time.monotonic()` around the writes (or monkeypatch `asyncio.sleep` to capture the
  requested delay) and assert a `MINIMUM_TIME_BETWEEN_MSGS - diff` sleep was requested.

---

#### H5. No reconnect-behavior tests

**Severity:** High
**File:** `src/powerpetdoor/client.py:827–898` (`connect`, `connection_lost`,
`reconnect`, `handle_connect_failure`); `tests/test_client.py:550–572` only asserts
"`ensure_future` was called" without ever running the reconnect

`connect()`'s bare `except:` → `handle_connect_failure()` → `reconnect(delay)` loop has
no test. `test_connection_lost_no_reconnect_when_shutdown` (test_client.py:561–572) is
near-vacuous: it iterates `mock_ensure.call_args_list` asserting `'reconnect' not in
str(call)` — if `ensure_future` was never called the loop body never executes, and the
string check depends on coroutine repr formatting.

**Recommendation (use the real simulator — it's already in the fixture toolbox):**
- `test_client_reconnects_after_server_restart`: connect client (reconnect=0.05) to a
  simulator on a fixed free port; `await sim.stop()`; assert client becomes unavailable;
  start a new simulator on the same port; `await` an `on_connect` handler event with
  timeout; assert `client.available`.
- `test_connect_failure_schedules_retry`: point the client at a closed port with
  `reconnect=0.05`, patch `reconnect` with an `AsyncMock`, `await client.connect()`,
  assert `reconnect` was awaited with `cfg_reconnect` (replaces the string-matching
  test).
- `test_no_reconnect_after_stop`: `stop()` then force `connection_lost(None)`; assert no
  reconnect task is created (assert directly on a patched `reconnect`, not on repr
  strings).
- `test_outstanding_future_cancelled_by_disconnect_message` (exists) — extend with the
  simulator: kill the connection while a `notify=True` future is pending and assert the
  awaiting caller gets `CancelledError`, not a hang.

---

#### H6. Hypothesis is a dev dependency with zero property-based tests — concrete targets

**Severity:** High
**Files:** `pyproject.toml:45`; `src/powerpetdoor/schedule.py`, `client.py`,
`simulator/protocol.py`, `tz_utils.py`, `simulator/commands/base.py`,
`simulator/commands/history.py`

Beyond the framing property in C5, these are the highest-value targets:

- **`compress_schedule` semantic preservation + idempotence** (`schedule.py:130–268`):
  generate lists of valid entries (days ∈ {0,1}^7, times 0–23:0–59, inside/outside
  booleans). Properties: (a) `compress(compress(s)) == compress(s)`; (b) coverage
  equivalence — for every (sensor, day, minute), minute is inside some entry's window
  before iff after; (c) every output entry passes `validate_schedule_entry`; (d) output
  indices are `0..n-1`.
- **`compute_schedule_diff` correctness** (`schedule.py:316–378`): property — applying
  the returned (deletes, sets) to `current` yields a schedule whose
  `schedule_entry_content_key` multiset equals `new`'s; and no two `entries_to_set`
  receive the same index. Also a **non-mutation** check — the function currently writes
  `entry[FIELD_INDEX] = ...` into the *caller's* `new_schedule` dicts (schedule.py:365,
  372); decide whether that's contract or bug and pin it with a test (deepcopy-compare
  inputs).
- **`week_0_mon_to_sun`/`week_0_sun_to_mon`** (`schedule.py:35–56`): both directions
  are inverses over 0–6 and results always land in 0–6.
- **`parse_arg`** (`commands/base.py:80–152`): for arbitrary strings and every
  `arg_type`, returns `(value, None)` xor `(None, str)` and never raises; int/float
  min/max bounds honored; `time_range` results always satisfy 0≤h≤23, 0≤m≤59.
- **`_parse_days_str`** (`base.py:174–190`): arbitrary comma-joined day names round-trip
  to the correct 7-element mask; unknown tokens raise `ValueError` with the exact
  message.
- **`History.resolve_recall`** (`commands/history.py:216–275`): never raises for any
  `"!"+text` input; always returns one of the three documented shapes.
- **`parse_posix_tz_string`** (`tz_utils.py:151–199`): never raises for arbitrary text;
  and the exhaustive-tzdata test in M2.

---

#### H7. No concurrency tests: parallel commands, interleaved responses, multi-client races

**Severity:** High
**Files:** `src/powerpetdoor/client.py` (priority queue + `_outstanding` + single
in-flight `_last_command` design); `tests/` — nothing exercises concurrency

The client serializes sends (one outstanding command, `check_receipt` gate) while
callers may issue many concurrent `notify=True` requests. Nothing verifies interleaved
responses resolve the *right* futures, that priority actually reorders under load, or
that two clients can drive the simulator simultaneously.

**Recommendation:**
- `test_parallel_queries_resolve_correct_futures` (against the simulator):
  `asyncio.gather` 10 mixed requests (`GET_SETTINGS`, `GET_DOOR_STATUS`,
  `GET_HOLD_TIME`, `GET_DOOR_BATTERY` …) and assert each future's result type/value
  matches its own command — this catches msgId/future cross-wiring.
- `test_priority_preempts_queued_low_priority`: block dequeue, enqueue 5 LOW status
  queries then one HIGH `CMD_OPEN`, unblock, assert the transport write order puts OPEN
  before the status queries (inspect `MockTransport.get_written_messages()`).
- `test_disconnect_during_inflight_command`: issue `notify=True` command against the
  simulator, stop the simulator before responding, assert the awaiting caller receives
  `CancelledError` within `timeout` (not a hang).
- `test_two_clients_commands_do_not_cross`: two `PowerPetDoorClient`s on one simulator;
  client A sends `CMD_OPEN`, client B concurrently sends `GET_SETTINGS`; assert B's
  future resolves to a settings dict (never a door-status payload) — pins per-connection
  response routing.
- `test_sequence_counter_monotonic_under_concurrent_enqueue`: `enqueue_data` from
  multiple tasks; assert all sequence numbers unique and ordered per priority.

---

#### H8. Simulator-side protocol violations untested; unknown commands answer `success:"true"`; garbage input grows the buffer forever

**Severity:** High
**File:** `src/powerpetdoor/simulator/protocol.py:194–293`;
`tests/simulator/test_protocol.py` has no malformed-input tests

Three specific gaps:
1. `_handle_message` with an unknown command logs a warning but still sends
   `{"CMD": <unknown>, "success": "true"}` (protocol.py:287–293). A real device
   presumably does not affirm commands it doesn't implement. No test pins this either
   way.
2. `data_received` with a non-`{` payload: `_find_json_end` returns `None`, the `while`
   breaks, and the garbage stays in `self.buffer` **forever**, prefix-poisoning all
   future messages (protocol.py:201–207) — same class of bug as C4, plus unbounded
   memory growth from a hostile/broken client.
3. `msg_id` falsiness: `if msg_id:` (protocol.py:275) drops `msgID` from the response
   when a client sends `msgId: 0`, so that client's future would never resolve.

**Recommendation:**
- `test_unknown_command_response` — send `{"config": "NO_SUCH_CMD", "msgId": 7}`;
  decide the correct behavior (`success:"false"` is the defensible one), fix, and assert
  the exact response.
- `test_garbage_input_does_not_poison_buffer` — send `b"garbage"` then a valid PING;
  assert the PONG still arrives (requires fixing the buffer handling: discard up to the
  next `{`).
- `test_msgid_zero_echoed` — send `msgId: 0`, assert `msgID: 0` in the response.
- `test_set_schedule_missing_payload_fails` — `CMD_SET_SCHEDULE` without
  `FIELD_SCHEDULE` → `success:"false"` (protocol.py:415–416, currently uncovered).
- `test_get_schedule_unknown_index_fails` — exists? No: only the happy CRUD path is
  tested (`test_protocol.py:274–306`). Add: get/delete index 99 → `success:"false"`,
  `reason == "Schedule not found"` (protocol.py:404–405, 424–426).
- `test_set_schedule_list_replaces_all` — `CMD_SET_SCHEDULE_LIST` (protocol.py:428–438)
  has zero tests.
- Buffer/oversize: send a single 512 KB JSON object split across many
  `data_received` calls; assert it parses (or a documented limit rejects it).

---

#### H9. Command-mixin coverage collapse: history 8.7%, schedule 18.6%, buttons 40%, settings 43.5%, info 49.8%, base 54%, handler 64.6%

**Severity:** High
**Files:** `src/powerpetdoor/simulator/commands/{history,schedules,settings,info,buttons,base,handler}.py`;
existing `tests/simulator/test_commands.py` covers only notify/cycle/battery/broadcast/status/clear

All of these run through `CommandHandler.execute(...)` with a live simulator fixture that
already exists (`tests/simulator/test_commands.py:46–66`) — no new infrastructure needed.
Required tests (each asserting exact message text and state effect):

- **schedules.py** (18.6%): `execute("schedule")` bare → shows current schedules;
  `schedule add inside 6:00-22:00 weekdays` → entry created with
  `days_of_week == [0,1,1,1,1,1,0]`, correct start/end, message contains formatted time;
  `schedule add inside 25:00-26:00 all` → parse error text from `_parse_time_str`;
  `schedule list` empty vs populated (`_format_schedule`, `_format_days` — test "all
  days", "weekdays", single-day renderings); `schedule delete 0` on missing index →
  failure text; `schedule enable/disable <idx>` flips `enabled` and broadcasts;
  `schedule days 0 weekends`; `schedule time 0 7:30-21:15`; `schedule clear` empties
  `state.schedules`; every subcommand's `help` output.
- **history.py** (8.7%): unit-test the `History` class directly with `tmp_path` files:
  `get_entries` ordering, `remove_last_entry` updates memory **and** rewrites the file
  (read the file back, assert FileHistory `+line` format from history.py:151–161),
  `replace_last_entry`, `clear` truncates, `format_entries(limit)` numbering and
  "History (N of M commands):" header, multi-line entry handling, `execute_command`
  matrix: `None` → 20 entries, `"clear"`, `"5"`, `"0"` → "Number must be positive",
  `"abc"` → "Invalid argument: abc. Use 'clear' or a number."; `resolve_recall` full
  matrix (see prompt_common in C1); `History("none")` → InMemory; disabled behavior of
  every method when `_history is None` (returns False/"History not available…").
- **settings.py** (43.5%): `holdtime 5` sets `state.hold_time == 5.0` and broadcasts;
  `holdtime 0`/negative → ArgSpec min bound error text; `timezone` bare lists/shows;
  `timezone America/New_York` sets and broadcasts; `timezone Bogus/Zone` behavior;
  `safety on/off/toggle`, `lockout on/off/toggle`, `autoretract on/off/toggle` — each
  asserting both the state flag and the exact ON/OFF message; `battery 150` clamps;
  `battery` bare shows; `_timezone_completer` returns (name, posix) pairs.
- **buttons.py** (40%): `power on/off/toggle`, `auto on/off/toggle`,
  `inside_enable`/`outside_enable` (+`toggle` subcommand and `t` alias) — state +
  message + broadcast side effect (mock a protocol in `simulator.protocols` and assert
  `_send` payload cmd/values, not just the CommandResult).
- **base.py** (54%): direct unit tests for `parse_arg` per type including min/max
  violations with exact messages ("'x' is below minimum (1)"), `choice`
  case-insensitive canonicalization, `time_range` bad formats ("Time range must be in
  format…"), `_parse_days_str` presets and error; `ArgSpec.generate_usage` for each
  type (`<on|off>`, `[days]`); `get_canonical_command` ("bc" → "broadcast", "ac c" →
  "ac connect", "sched del 1" → "schedule delete 1", unknown → None,
  already-canonical → None); `SubcommandInfo.__post_init__` list→registry conversion.
- **info.py** (49.8%): `status` full text against a configured state (door open, power
  off, schedules present, battery 50% discharging — assert each section);
  `get_help` in interactive vs non-interactive vs cli mode (exit aliases shown/hidden);
  `broadcast hwinfo`/`stats`/`schedules`/`notifications` subcommands (only
  status/settings/battery/all are tested today); `history` command routed through
  `execute("history 5")` in interactive mode with a real `History`.
- **handler.py** (64.6%): `set_cli_mode(True)` maps `exit`/`q`/`quit` to shutdown and
  removes the standalone exit command; `set_cli_mode(False)` restores it (the registry
  mutation at handler.py:180–220 is global state — also add a fixture guard that
  restores the registry, otherwise these tests will poison each other under xdist);
  `execute("schedule bogus")` → "Unknown schedule subcommand: bogus\nAvailable: …";
  `execute("holdtime")` → missing-arg usage; handler exception → `"Error: …"`
  (handler.py:338–339); implicit `help`/`?` at every level.

---

#### H10. `door.py` public API without any tests: schedules, notifications, latency, hw/fw versions, disconnect

**Severity:** High
**File:** `src/powerpetdoor/door.py`; `tests/test_door.py` covers none of these

Untested public surface: `set_notifications()` (door.py:831–881, including the
merge-with-cached-values semantics), `get_schedule`/`set_schedule`/`delete_schedule`
(892–935), `refresh_schedules()` two-step fetch (937–979, including the empty-list
branch and per-index timeout/exception branches), `on_schedule_change` callbacks and
`_on_schedule_update`/`_on_schedule_delete` cache maintenance (1245–1272),
`latency`/`_on_ping`/reset-on-disconnect (397–404, 1228–1243), `firmware_version`/
`hardware_version` formatting including empty-dict branches (783–806),
`disconnect()` (459–464), `toggle()` while closing (no-op branch, 525–531),
`is_closing`/`position` maps (491–511), and the callback-exception logging paths
(1114–1118, 1139–1143).

**Recommendation (all against the existing simulator fixture):**
- `test_refresh_schedules_two_step_fetch`: seed `simulator.state.schedules` with 2
  entries; assert returned `Schedule` objects match (index, days, times) and
  `door.schedules` cache updated.
- `test_refresh_schedules_empty`: no schedules → returns `[]`, cache cleared.
- `test_set_schedule_roundtrip` / `test_delete_schedule_removes` /
  `test_get_schedule_by_index`.
- `test_on_schedule_change_fired_on_simulator_add` and `_delete` (use
  `CallbackTracker`-style events, not sleeps).
- `test_set_notifications_partial_update_preserves_others`: prime
  `door._notifications.inside_on = True`, call `set_notifications(low_battery=True)`,
  assert the wire payload (simulator state) kept `inside_on` True.
- `test_latency_set_by_ping_and_cleared_on_disconnect`: keepalive-enabled client,
  assert `door.latency` becomes a float ≥ 0 after a PONG, and `None` after disconnect.
- `test_firmware_version_formats` / `test_hardware_version_formats`: unit tests with
  `_hw_info` populated/empty/partial — expected exact strings ("1.2.3",
  `""`, "1 rev 2").
- `test_toggle_noop_while_closing`: set `door._status = DoorStatus.CLOSING_TOP_OPEN`,
  patch `open`/`close`, call `toggle()`, assert neither called.
- `test_status_callback_exception_does_not_break_others`: register a raising callback
  plus a good one; assert the good one still fires (door.py:1114–1118).
- `test_position_map_all_states`: parametrize all 8 `DoorStatus` values → exact
  percent.

---

#### H11. `schedule.py` negative/edge gaps: invalid entries crash `compress_schedule`; boundary times untested

**Severity:** High
**File:** `src/powerpetdoor/schedule.py:130–268`; `tests/test_schedule.py` (87% — the
missing 13% is exactly the dangerous part)

`compress_schedule` KeyErrors on any entry missing time sub-dicts (schedule.py:151–160
does unguarded `sched[...][...]` access) even though `validate_schedule_entry` exists to
catch exactly these — nothing tests the interplay or the crash. Boundary semantics also
untested: adjacent windows (end == next start, the `>=` at schedule.py:180 merges them —
assert that deliberately), zero-length windows (start == end), 23:59 endpoints,
entries with `enabled: False`, disabled-days-all-zero entries, and duplicate identical
entries collapsing to one.

**Recommendation:**
- `test_compress_rejects_or_crashes_on_invalid_entry`: today
  `compress_schedule([{FIELD_DAYSOFWEEK: [1]*7, FIELD_INSIDE: True}])` raises `KeyError`
  — decide the contract (pre-validate and skip, or raise `ValueError`), fix, and pin it.
- `test_compress_merges_adjacent_windows` (6–10 and 10–14 → one 6–14 entry; asserts the
  `>=` boundary exactly).
- `test_compress_zero_length_window`, `test_compress_2359_boundary`,
  `test_compress_duplicate_entries_collapse`, `test_compress_all_days_zero_drops_entry`.
- `test_diff_does_not_mutate_inputs` (see H6).
- `test_validate_rejects_non_dict_time_field`: `inStartTime: "6:00"` →
  exercises the `except Exception` branch at schedule.py:111–113 (currently uncovered).

---

### Medium

---

#### M1. Assertions that only prove "no exception" or "buffer empty"

**Severity:** Medium
**Files/lines:**
- `tests/test_client.py:370–378` `test_valid_json_processed` — asserts only `_buffer == ""`; a client that parsed and *dropped* every message passes.
- `tests/test_client.py:390–399` `test_multiple_messages_processed` — same; assert both messages were dispatched (patch `process_message` and count calls with payloads).
- `tests/test_client.py:591–610` `test_response_resolves_future` — asserts the future left `_outstanding` but never `future.result()`; assert the resolved settings dict value.
- `tests/test_door.py:636–642` `test_refresh_all` — comment admits "Just verify it doesn't error".
- `tests/test_door.py:533–540` `test_hold_time_get` — `assert door.hold_time > 0` after setting 15; assert `== 15.0`.
- `tests/simulator/test_protocol.py:328–339` `test_buffered_messages` — `write.call_count > 0`; assert exactly one PONG with `PONG == "test"`.
- `tests/simulator/scripts/test_basic_cycle.py:105` — `if statuses:` guard makes the final assertion vacuously skippable.

**Recommendation:** Each listed test should assert the single concrete expected value
(shown inline above). For dispatch-counting, monkeypatch `client.process_message` with a
recording coroutine — no sleeps needed.

---

#### M2. tz_utils edge cases: angle-bracket POSIX abbreviations unparseable; no-match returns a dict of Nones; statistical assertions

**Severity:** Medium
**File:** `src/powerpetdoor/tz_utils.py:151–199`; `tests/test_tz_utils.py:159–174, 328–337, 339–356`

Real tzdata emits angle-bracket abbreviations (e.g. `America/Argentina/Buenos_Aires` →
`<-03>3`); `parse_posix_tz_string`'s regex `[A-Za-z]+` cannot match them, so it returns
a dict with `std_abbrev=None` — and the integration tests dodge this by (a) sampling
only "nice" zones and (b) asserting a *statistical* `> 0.9` ratio
(test_tz_utils.py:337) and (c) hiding failures behind `if posix:`
(test_tz_utils.py:352). Also, for fully non-matching input (e.g. `"123"`), the function
returns a dict of Nones rather than the docstring's "None if parsing fails" — untested,
undefined contract.

**Recommendation:**
- `test_parse_angle_bracket_abbreviation`: `parse_posix_tz_string("<-03>3")` →
  `std_abbrev == "-03"`, `std_offset == "3"` (requires extending the regex — the test
  drives the fix).
- `test_parse_no_match_returns_none` (or pin the dict-of-Nones as contract and fix the
  docstring — pick one).
- Replace the 90% statistical test with an exhaustive one:
  `for tz in get_available_timezones(): posix = get_posix_tz_string(tz); if posix is not None: parsed must have std_abbrev and std_offset` —
  no ratio, no `if`-skip of the parse assertions; plus an explicit allowlist assertion
  for the handful of zones with no TZif footer.
- `test_extract_posix_from_tzif_bad_file`: point at a non-TZif resource → `None`
  (tz_utils.py:52–66 error branches uncovered).
- DST semantics: `test_new_york_posix_matches_zoneinfo_transitions` — parse
  `M3.2.0`/`M11.1.0` and cross-check against `zoneinfo.ZoneInfo("America/New_York")`
  utcoffset on 2026-03-08 vs 2026-03-09 (pins that the POSIX rule actually matches
  reality, i.e. the DST-transition edge case).

---

#### M3. `requires_yaml` skips have no mock-equivalent, and the YAML-unavailable code path itself is never tested

**Severity:** Medium
**Files:** `tests/simulator/test_scripting.py:37–40`, `tests/simulator/scripts/*.py`
(8 skipif sites); `src/powerpetdoor/simulator/scripting.py` (`YAML_AVAILABLE` fallback
branches)

PyYAML is a hard dev dependency, so the skips never fire in CI — but that means the
`YAML_AVAILABLE = False` branches in scripting.py (import failure, `Script.from_yaml`
error path) have zero coverage in any configuration, and if the dep ever goes missing,
8+ tests silently vanish instead of failing.

**Recommendation:** Test the no-YAML path explicitly by monkeypatching
`scripting.YAML_AVAILABLE = False` (and/or `sys.modules["yaml"] = None` with a reload in
a subprocess test): `Script.from_yaml` must raise `ScriptError` with the exact "PyYAML"
message; `get_builtin_script` behavior without YAML must be pinned. Then the skip
markers protect nothing and can stay as belt-and-braces.

---

#### M4. Dead test infrastructure signals untested scenarios

**Severity:** Medium
**File:** `tests/conftest.py:74–75` (`_auto_respond`, `_response_delay` — never used),
`conftest.py:108–115` (`respond_failure` — never called), `conftest.py:165–183`
(`create_mock_response` — never called)

Helpers built for failure-response and delayed-response testing exist and are unused —
the exact scenarios they were written for (device errors, slow devices) have no tests
(see C4/H4). Unused fixtures also rot: `MOCK_SCHEDULE_ENTRY` (conftest.py:152) still
uses the old int `daysOfWeek` shape.

**Recommendation:** Wire `respond_failure` into the C4 failure tests and
`_response_delay` into an H4 slow-device test (`test_slow_response_within_timeout_ok`),
or delete the dead code. Update `MOCK_SCHEDULE_ENTRY` when touching it.

---

#### M5. Built-in script tests are wall-clock-coupled by design

**Severity:** Medium
**Files:** `tests/simulator/scripts/conftest.py:102–118` (comment: "rise_time must be
> 0.5s to pass basic_cycle assertions"), all 7 `tests/simulator/scripts/test_*.py`

The YAML scripts encode absolute waits (`wait 0.5` then `assert door_status
DOOR_RISING`), so tests must configure the simulator to move *slower* than the script's
sleeps. That is a race with CI scheduling in the opposite direction (a stalled runner
overshoots the window and the door reaches HOLDING before the assert). Seven test files
× multi-second realistic timing also makes these the slowest tests in the suite.

**Recommendation:** Prefer `wait_for <condition> <timeout>` over `wait <n>` in the
built-in scripts (the runner already supports it, scripting.py:298–301) so assertions
key on state, not time — e.g. `wait_for door_open 5` + `assert door_status DOOR_HOLDING`
after a `wait_for` on holding. Where a script intentionally checks mid-transition state,
add a dedicated state-event (H3) instead. Keep one wall-clock script test as a smoke
test and mark it `@pytest.mark.slow` (see M8).

---

#### M6. `scripting.py` at 55.7%: loader, error, and per-action branches untested

**Severity:** Medium
**File:** `src/powerpetdoor/simulator/scripting.py`; `tests/simulator/test_scripting.py`

Untested: `Script.from_file` (path handling, missing file), verbose-mode output of
`run()` (every test passes `verbose=False`), `_wait_for_condition` for conditions other
than `door_open` (`door_closed`, status-equality conditions — enumerate whatever
`_check_condition` supports at scripting.py:367–411 and parametrize all of them),
`_set_value`/`_toggle_value` with unknown names (error path), `_assert_condition`
failure message content (`AssertionFailed` text is never asserted anywhere),
`script_completer(prefix)` filtering, and `wait` with non-numeric seconds.

**Recommendation:** Parametrized `test_check_condition_matrix[condition,state,expected]`
covering every supported condition token; `test_set_unknown_value_fails_script` with
exact error; `test_assert_failure_message_contains_expected_and_actual`;
`test_from_file_missing_raises_scripterror`; `test_script_completer_prefix_filtering`.

---

#### M7. Remaining simulator server/protocol branches with no coverage

**Severity:** Medium
**Files:** `src/powerpetdoor/simulator/server.py`, `simulator/protocol.py`,
`simulator/state.py`

Specifics: low-battery notification threshold crossing (`set_battery(21→19)` with
`low_battery` enabled → exactly one `NOTIFY_LOW_BATTERY`; disabled → none;
server.py:260–270, 818–831); untested broadcast functions (`broadcast_safety_lock`,
`broadcast_cmd_lockout`, `broadcast_autoretract`, `broadcast_timezone`,
`broadcast_hardware_info`, `broadcast_stats`, `broadcast_all` — assert exact payload
dicts via a recorded protocol `_send`); `activate_sensor` duration/toggle semantics and
`_deactivate_sensor_after` (server.py:710–774); `trigger_sensor` re-trigger-extends-hold
window (protocol.py:879–885) and sensor-during-close → auto-retract path
(protocol.py:887–902 + `total_auto_retracts` increment); door-reversal state machine
(open during CLOSING_MID → RISING, close during RISING → CLOSING_MID —
protocol.py:640–654, 727–743 — currently only reachable by timing luck, becomes
deterministic with H3's event hooks); `_send` with `transport=None` no-op
(protocol.py:237–238); `CMD_POWER_OFF` while door open closes it (protocol.py:520–522);
schedule-based sensor blocking end-to-end (state.is_sensor_allowed_by_schedule with
`auto=True` and an out-of-window schedule → `trigger_sensor` ignored); PING handled even
when power off (protocol.py:261–262 ordering).

**Recommendation:** One test per branch above; all are direct method calls plus a
recording protocol — no sockets or sleeps required except where noted.

---

#### M8. Test-infrastructure hygiene: no markers, redundant decorators, no timeout guard, registry pollution risk

**Severity:** Medium
**Files:** `pyproject.toml:69–72`, `tests/` generally

- `asyncio_mode = "auto"` is configured, yet 205 tests carry redundant
  `@pytest.mark.asyncio` decorators (30 in test_door.py alone) — noise that suggests
  copy-paste test authoring.
- No `slow`/`integration` markers: the multi-second script tests can't be excluded from
  a fast inner loop.
- No `pytest-timeout`: a regression in any poll loop's exit condition hangs CI for the
  xdist worker.
- The command registry (`commands/base.py:251`) is module-global mutable state and
  `set_cli_mode` mutates it (handler.py:180–220); under `-n auto` any new tests touching
  it need an autouse save/restore fixture or they will flake cross-worker (worth adding
  *before* H9's tests land).

**Recommendation:** register `slow` marker + apply to `tests/simulator/scripts/`; add
`pytest-timeout` with a 60 s default; drop redundant asyncio marks; add a
`restore_command_registry` fixture in `tests/simulator/conftest.py`.

---

### Low

---

#### L1. Read-back and tautology-adjacent tests

**Severity:** Low
**Files/lines:** `tests/simulator/test_commands.py:545–554` (`test_set_interactive_mode`
— setter then read the private attr), `test_commands.py:573–580`
(`test_empty_message_is_falsy` — asserts `not ""`, i.e. Python string semantics, and
duplicates the previous test), `tests/simulator/test_server.py:214–229`
(`test_sensor_active_state` — the attribute read-backs; the
`is_sensor_blocking_close()` parts are fine), `tests/simulator/test_state.py:43–52,
69–84` (dataclass "custom values" tests that assert constructor kwargs are stored —
tests the `dataclass` decorator).

**Recommendation:** Delete `test_empty_message_is_falsy`; replace
`test_set_interactive_mode` with behavior tests (already exist:
`test_clear_rejected_when_not_interactive`); keep dataclass *default* tests (they pin
contract values) but drop the custom-kwargs read-backs.

---

#### L2. Boolean `days_of_week` migration is invisible to the tests

**Severity:** Low
**Files:** `src/powerpetdoor/door.py:202` (`list[bool]`, commit 7593a1f);
`tests/test_door.py:207` (`assert schedule.days_of_week == [1, 1, 1, 1, 1, 1, 1]`),
`tests/simulator/test_state.py:99, 135, 156`

`True == 1` in Python, so every existing assertion passes whether the implementation
returns bools or ints — the migration the last commit made cannot regress detectably.

**Recommendation:** Add `assert all(isinstance(d, bool) for d in schedule.days_of_week)`
to `test_door.py` `TestSchedule.test_defaults` and to the `from_dict` tests (including
the legacy-bitmask path at door.py:266–268), and pin `to_dict` emits ints
(`assert all(isinstance(d, int) and not isinstance(d, bool) for d in ...)` if the
protocol requires literal 1/0 — read door.py:213 and decide).

---

#### L3. `del_handlers` raises `KeyError` for unknown names — asymmetric with `del_listener`, untested

**Severity:** Low
**File:** `src/powerpetdoor/client.py:346–350`

`del_handlers` uses bare `del` on three dicts (raises if the name registered only some
handlers or none); `del_listener` deliberately uses `.pop(name, None)`. Neither the
raise nor the intended contract is tested. Note `PowerPetDoor.disconnect()`
(door.py:459–464) calls `del_handlers("_door_facade")` — a second `disconnect()` call
would raise `KeyError`.

**Recommendation:** `test_del_handlers_partial_registration` and
`test_door_disconnect_twice` — pin the contract (almost certainly: make it `.pop`-safe
and assert no-raise).

---

#### L4. `client.available` returns `None`/transport-object truthiness, not `bool` — masked by the test

**Severity:** Low
**File:** `src/powerpetdoor/client.py:1106–1109`; `tests/test_client.py:170–172`

`return (self._transport and not self._transport.is_closing())` yields `None` when
disconnected; the test writes `assert not disconnected_client.available` which hides the
type. The property is annotated `-> bool`.

**Recommendation:** `assert disconnected_client.available is False` (drives a
`bool(...)` fix), plus `assert client.available is True` in the connected test.

---

#### L5. `TestKeepalive.test_ping_sends_message` hand-simulates the keepalive instead of exercising it

**Severity:** Low
**File:** `tests/test_client.py:489–501`

The test sets `_last_ping` manually and calls `send_message(PING, ...)` itself — it
verifies `send_message` can enqueue a PING, not that the keepalive timer sends one.
Superseded by the C2 replacement tests; then this becomes a duplicate of
`TestSendMessage` coverage.

**Recommendation:** Fold into C2's `test_keepalive_unanswered_ping_...` (which asserts
the PING was enqueued by `keepalive()` itself) and delete this one.

---

### Trivial

---

#### T1. CLAUDE.md references a nonexistent CI workflow file

**Severity:** Trivial
**Files:** `.claude/CLAUDE.md` (Version Matrix table: `.github/workflows/ci.yml`);
actual files are `.github/workflows/test.yml` and `release.yml`

The mandatory version-matrix checklist points contributors at a file that doesn't exist.
Update the table to `test.yml`.

---

#### T2. Duplicated test: `test_cycle_alias_y` appears twice

**Severity:** Trivial
**File:** `tests/simulator/test_commands.py:181–186` and `:330–335`

Identical body in `TestCycleCommand` and `TestAliases`. Redundancy is the one valid
deletion reason — remove one.

---

## Areas Reviewed With No Findings

- **`tests/simulator/test_state.py` — `is_sensor_blocking_close` / `is_sensor_allowed`
  matrices** (test_state.py:202–499): exemplary. Single-outcome assertions, full
  combinatorial coverage of enable/active/safety-lock/cmd-lockout interactions,
  midnight-crossing schedule windows tested with exact hours. This is the standard the
  rest of the suite should meet.
- **`tests/test_schedule.py` — diff and content-key tests** (test_schedule.py:310–481):
  specific, deterministic, index-reuse semantics pinned precisely (including the
  excess-deletion case). Only gaps are the compress negative/boundary cases (H11), not
  quality problems.
- **`tests/simulator/test_protocol.py` — hold-time centiseconds suite**
  (test_protocol.py:390–526): exact-value conversions in both directions, cross-checked
  between `GET_HOLD_TIME` and `GET_SETTINGS`. Good negative-adjacent design (fractional
  values, round-trips). Its only defect is the shared sleep-based dispatch (H3), not the
  assertions.
- **`tests/simulator/test_client_integration.py` — `CallbackTracker`**
  (test_client_integration.py:117–159): the event-based `wait_for` pattern is exactly
  the right deterministic synchronization approach; wildcard-listener and
  field/value-tuple assertions are specific. Recommended as the template for H3
  cleanup.
- **`tests/simulator/test_commands.py` — notification command tests**
  (test_commands.py:73–158): assert state effect *and* message content, including
  negative cases with exact error text ("Unknown notify subcommand", "not valid").
- **Test/prod parity of protocol constants**: `msgId` vs `msgID` response casing is
  correctly mirrored (`const.py:23–24`, conftest/response tests use the right one).
