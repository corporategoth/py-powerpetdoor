# Frontend Developer Analysis — Round 10

Commit `65ad86d`. Everything below was produced by running the real binaries.
Work happened in `/tmp/r10fe` on a `git archive HEAD` copy; the repository was
not modified (this file excepted). Two installations were exercised:

- **dev**: `/home/prez/src/pypowerpetdoor/.venv/bin/ppd-simulator{,-ctl}` (all
  extras present).
- **consumer**: a wheel built from this commit with `python -m build` and
  installed into an empty venv with **no extras**, which is what
  `pip install pypowerpetdoor` gives a user
  (`/tmp/r10fe/consumer/bin/…`; `pip list` → `pypowerpetdoor 0.3.0`, `tzdata`,
  `pip` — nothing else).

Every daemon, pty child and background process started here was terminated;
final sweep for `bin/ppd-simulator` returned nothing.

## Summary

| Severity | Count |
|---|---|
| High | 1 |
| Medium | 1 |
| Low | 3 |
| Trivial | 1 |
| **Total** | **6** |

- **H1** — after stdin reaches EOF the plain-input prompt busy-spins forever:
  98 % of a core, 2.2 MB/s of prompt text, never exits.
- **M1** — any *non-pollable* stdin (`/dev/null`, a file, a heredoc, a
  here-string) kills both binaries with a 37-line `PermissionError` traceback,
  identical with and without `--debug`.
- **L1** — `ppd-simulator-ctl` never received round 9's argument/connection
  error treatment its sibling did.
- **L2** — SIGTERM/SIGHUP/SIGQUIT are unhandled in both binaries; only SIGINT
  shuts down.
- **L3** — a bare `pip install pypowerpetdoor` ships a scripting front end that
  cannot work, says so 14 times in one command, and points at the wrong
  install line.
- **T1** — both ANSI-emitting sites promise dumb-terminal protection in their
  docstrings and only check `isatty()`.

H1 and M1 are two symptoms of one uncovered area (the plain-input fallback's
stdin handling) but have different mechanisms and different fixes, so they are
reported separately and each is independently reproduced.

## Findings

---

### H1 (High) — after stdin EOF the plain-input prompt spins at 98 % of a core, emitting 2.2 MB/s of prompt text, and never exits

**File**: `src/powerpetdoor/simulator/cli.py:674-687` (the reader callback),
registered at `cli.py:664` from `cli.py:976`.

**Reproduction** (dev install; the same happens on the consumer install, where
the plain path is the *only* path because prompt_toolkit is absent):

```
$ cd /tmp/r10fe && python3 - <<'EOF'
import os, subprocess, time
f=open("/tmp/r10fe/spin3.txt","w")
p=subprocess.Popen(["/home/prez/src/pypowerpetdoor/.venv/bin/ppd-simulator","-p","34361"],
                   stdin=subprocess.PIPE, stdout=f, stderr=subprocess.STDOUT)
p.stdin.write(b"status\n"); p.stdin.flush()
time.sleep(1.0)
p.stdin.close()          # EOF on the pipe, exactly like `echo status | ppd-simulator`
def ticks(pid):
    parts=open(f"/proc/{pid}/stat").read().rsplit(") ",1)[1].split()
    return int(parts[11])+int(parts[12])
time.sleep(0.5)
t0=ticks(p.pid); s0=os.path.getsize("/tmp/r10fe/spin3.txt")
time.sleep(5.0)
t1=ticks(p.pid); s1=os.path.getsize("/tmp/r10fe/spin3.txt")
hz=os.sysconf("SC_CLK_TCK")
print(f"5.0 s after stdin EOF: exited={p.poll() is not None}  CPU={(t1-t0)/hz:.2f}s "
      f"({(t1-t0)/hz/5*100:.0f}% of one core)  stdout +{s1-s0:,} bytes ({(s1-s0)/5/1e6:.1f} MB/s)")
p.kill(); p.wait(); f.close()
print("total file size:", f"{os.path.getsize('/tmp/r10fe/spin3.txt'):,}", "bytes")
EOF
5.0 s after stdin EOF: exited=False  CPU=4.90s (98% of one core)  stdout +10,813,635 bytes (2.2 MB/s)
total file size: 11,881,391 bytes
```

The piped command really did run before the spin started — the emitted file
contains the status reply, followed by nothing but prompts:

```
$ grep -a -m1 -o "Door: [A-Za-z_]*" spin.txt
Door: DOOR_CLOSED
$ head -c 100000 spin.txt | grep -ao "0.0.0.0:34121> " | wc -l
6489
```

Plain shell form, consumer install, 20 s wall clock:

```
$ echo -n "" | timeout 20 /tmp/r10fe/consumer/bin/ppd-simulator -p 34102 >/tmp/r10fe/o2.txt 2>&1
$ echo "EXIT=$?"; ls -l /tmp/r10fe/o2.txt
EXIT=124                                   # timeout had to kill it
-rw-r--r-- 1 prez prez 44040192 …          # 42 MB of prompts in 20 s
```

**Description.** `_BasicStdinInput.handle_input` treats a `readline()` of `""`
(EOF) exactly like a `readline()` of `"\n"` (the user pressed Enter):

```python
line = self._stdin.readline().strip()
if line:
    asyncio.create_task(self.process_command(line))
else:
    # Empty line (just Enter), re-show prompt
    self._prompt.show()
```

An fd at EOF is permanently readable, so `add_reader` re-invokes the callback
immediately and forever. The result is an unkillable-by-EOF process pegging a
core and writing prompt text at 2.2 MB/s — about 190 GB/day — with no error, no
message, and no exit.

The only non-interactive stdin form that works is a pipe whose feeder issues
`shutdown` before closing:

```
$ ( printf 'status\nshutdown\n'; sleep 2 ) | timeout 8 ppd-simulator -p 34294 >pipe_ok.txt 2>&1
$ echo "rc=$?"; tail -2 pipe_ok.txt
rc=0
0.0.0.0:34294> >>> Shutting down...
2026-08-22 23:08:57,238 [INFO] Door simulator stopped
```

The test suite cannot catch this because it pins the *typed blank line* and
never the EOF: `tests/simulator/test_cli.py:2009`
`test_handle_input_empty_line_reshows_prompt` uses `readline=lambda: "\n"`, and
the only test that registers a real reader
(`test_start_enables_prompt_and_registers_reader`, line 1960) removes it
immediately without ever letting it fire. The two conditions share one code
path and one assertion.

**Recommendation.** Distinguish EOF from a blank line: `readline()` returning
`""` (before `.strip()`) means the stream is finished — call `self.stop()` and
set `stop_event` (the same thing `ctl` already does at `ctl.py:359-360`, where
`fut.set_result(line if line else None)` ends the session cleanly). Add a test
whose stdin double returns `""` and asserts `handle_input` deregisters the
reader instead of calling `prompt.show()`.

---

### M1 (Medium) — `/dev/null`, a file, a heredoc or a here-string on stdin kills both binaries with a 37-line `PermissionError` traceback, and `--debug` makes no difference

**File**: `src/powerpetdoor/simulator/cli.py:664`
(`self._loop.add_reader(self._stdin.fileno(), …)`, reached from `cli.py:976`)
and `src/powerpetdoor/simulator/ctl.py:383` (same call).

**Reproduction** (dev install; identical on the consumer install):

```
$ timeout 10 ppd-simulator -p 34290 < /dev/null > a.txt 2>&1; echo "rc=$?"; tail -1 a.txt
rc=1
PermissionError: [Errno 1] Operation not permitted

$ printf 'status\n' > b_cmds.txt
$ timeout 10 ppd-simulator -p 34291 < b_cmds.txt > b.txt 2>&1; echo "rc=$?"; tail -1 b.txt
rc=1
PermissionError: [Errno 1] Operation not permitted

$ timeout 8 ppd-simulator -p 34292 > hd.txt 2>&1 <<'EOF'
status
shutdown
EOF
$ echo "rc=$?"; tail -1 hd.txt
rc=1
PermissionError: [Errno 1] Operation not permitted

$ timeout 8 ppd-simulator -p 34293 > hs.txt 2>&1 <<< "status"; echo "rc=$?"; tail -1 hs.txt
rc=1
PermissionError: [Errno 1] Operation not permitted

$ printf 'status\n' > c_cmds.txt
$ timeout 10 ppd-simulator-ctl -p 34271 -i < c_cmds.txt > c.txt 2>&1; echo "rc=$?"; tail -1 c.txt
rc=1
PermissionError: [Errno 1] Operation not permitted
```

The traceback is 37 lines and is unaffected by `--debug`:

```
$ awk '/Traceback \(most recent call last\)/,0' a.txt | wc -l
37
$ timeout 10 ppd-simulator -p 34362 --debug < /dev/null > adbg.txt 2>&1; echo "rc=$?"
rc=1
$ awk '/Traceback \(most recent call last\)/,0' adbg.txt | wc -l
37
```

It is printed *after* the banner and the first prompt, so the operator sees a
working simulator and then a crash dump:

```
$ grep -n -A3 "Traceback (most recent call last)" a.txt | head -4
55:0.0.0.0:34290> Traceback (most recent call last):
56-  File "/home/prez/src/pypowerpetdoor/.venv/bin/ppd-simulator", line 10, in <module>
57-    sys.exit(main())
…
  File ".../simulator/cli.py", line 664, in start
    self._loop.add_reader(self._stdin.fileno(), self.handle_input)
  File ".../selectors.py", line 345, in register
    self._selector.register(key.fd, poller_events)
PermissionError: [Errno 1] Operation not permitted
```

**Description.** `epoll` refuses to register regular files and `/dev/null`
(`EPERM`), which is what every non-pipe, non-TTY stdin is. The guard at
`cli.py:902-910` checks only that stdin *exists* (`os.fstat` succeeds) — a
regular file passes that test and then fails at `add_reader`.

This hits the documented Quick Start (`ppd-simulator`, docs/simulator.md line
~56) in exactly the environments where nobody attaches a terminal: a container
started without `-i` (stdin is `/dev/null`), a systemd unit with the default
`StandardInput=null`, a CI step, `cron`. It is also the *only* natural way to
script the plain prompt (`ppd-simulator < commands.txt`), which the docs
present as a first-class fallback: "Without prompt_toolkit, a plain input
prompt offers the same commands."

Round 9 established the contract that startup failures print one sentence and
hide the traceback behind `--debug` (`docs/simulator.md`: "`--debug`
additionally prints the traceback"). This startup failure is in the same
`main()` and honours neither half.

**Recommendation.** Probe the fd before committing to the reader — attempt
`add_reader` inside `try/except (PermissionError, OSError)`, and on failure
either (a) fall back to a thread-based `readline` loop, or (b) if that is more
machinery than the fallback deserves, degrade to the existing headless path
with the message already written for it: `logger.warning("stdin not available,
running without interactive input")` (`cli.py:980`) — the simulator then keeps
serving the door protocol, which is what a container or unit file wants. Do the
same at `ctl.py:383`, where the correct behaviour is more obviously to read the
file (a command list is a reasonable thing to pipe into `ctl -i`; note a *pipe*
already works and exits 0 — see "Areas Reviewed").

---

### L1 (Low) — `ppd-simulator-ctl` never got round 9's argument-validation and connection-error treatment; one failure class produces four different message shapes and `connect()` leaks

**File**: `src/powerpetdoor/simulator/ctl.py:703` (`--port` declared with no
validator), against `src/powerpetdoor/simulator/cli.py:213-230` where
`_validate_port` / `_validate_host` already exist in the same package.

**Reproduction** — the same "cannot reach the daemon" class, four shapes:

```
$ ppd-simulator-ctl --port 99999 status;            echo "rc=$?"
Error: connect(): port must be 0-65535.
rc=1
$ ppd-simulator-ctl --port -5 status;               echo "rc=$?"
Error: connect(): port must be 0-65535.
rc=1
$ ppd-simulator-ctl -H no.such.host.invalid status; echo "rc=$?"
Error: [Errno -2] Name or service not known
rc=1
$ ppd-simulator-ctl -H no.such.host.invalid -i < /dev/null; echo "rc=$?"
Error: Connection error: [Errno -2] Name or service not known
rc=1
$ ppd-simulator-ctl -p 34999 status;                echo "rc=$?"
Connection refused to 127.0.0.1:34999
rc=1
```

The sibling binary, after round 9, answers the identical mistakes like this:

```
$ ppd-simulator -p 99999
ppd-simulator: error: --port 99999: port must be 0-65535           # rc 2
$ ppd-simulator --host no-such-host.invalid -p 34260
ppd-simulator: error: --host no-such-host.invalid: Name or service not known
```

`--door-port` is not range-checked at all — it is accepted silently and only
affects the prompt string:

```
$ ppd-simulator-ctl --door-port 99999 --port 34311 status; echo "rc=$?"
  Script: none running
rc=0
```

**Description.** Five messages for one situation, from one binary, in one
release. Only `Connection refused to 127.0.0.1:34999` names the target; the
resolve failures name neither the host nor the flag that changes it; the
range errors surface the libc function name `connect()` at rc 1 instead of an
argparse usage error at rc 2. `ppd-simulator-ctl --port abc` *does* produce a
proper argparse error (`invalid int value: 'abc'`, rc 2), so the two adjacent
mistakes on the same flag land in different worlds.

**Recommendation.** Import `_validate_port` / `_validate_host` (or lift them to
a shared module) and apply them to `--port`, `--door-port` and `--host` in
`ctl`'s parser, giving rc 2 and the `--flag value: reason` wording the other
binary uses. Normalise the two runtime failures to the shape that already
works, naming the target: `Cannot reach 127.0.0.1:3001: Name or service not
known`.

---

### L2 (Low) — SIGTERM, SIGHUP and SIGQUIT are unhandled in both binaries: the process is killed by the signal with no shutdown line, while SIGINT exits 130 cleanly

**File**: `src/powerpetdoor/simulator/cli.py` — the only signal handling in the
tree is `except KeyboardInterrupt` at `cli.py:1294`; `grep -n "signal\."`
across `src/powerpetdoor/simulator/` finds no `signal.signal` /
`loop.add_signal_handler`.

**Reproduction** (harness spawns the daemon with default signal dispositions,
delivers one signal 1.5 s in, and waits with a hard bound):

```
$ python3 sigprobe.py SIGINT 1.5 15 ppd-simulator --daemon 34241 -p 34240
signal=SIGINT exit=130 elapsed=0.03s
--- last 6 lines of output ---
Simulator started on 0.0.0.0:34240
Control channel: 127.0.0.1:34241
2026-08-22 23:06:41,948 [INFO] Door simulator stopped
Simulator stopped.

$ for s in SIGTERM SIGHUP SIGQUIT; do python3 sigprobe.py $s 1.5 15 ppd-simulator --daemon 34241 -p 34240; done
signal=SIGTERM exit=-15 elapsed=0.00s
--- last 6 lines of output ---
Simulator started on 0.0.0.0:34240
Control channel: 127.0.0.1:34241          <- log simply stops here
signal=SIGHUP exit=-1 elapsed=0.00s
… same, no shutdown line …
signal=SIGQUIT exit=-3 elapsed=0.56s
… same, no shutdown line …
```

`ctl` behaves the same way:

```
ctl SIGTERM rc= -15
tail: ['… [INFO] Simulator: Inside sensor triggered, opening door',
       '… [INFO]   Step 4: wait_for(condition=door_open, timeout=5)']
```

(Verified this is a real gap and not shell inheritance: the harness uses
`subprocess.Popen`, so all dispositions are `SIG_DFL`. Backgrounded shell jobs
inherit `SIG_IGN` for SIGINT and are *not* usable for this measurement — a
`nohup … &` daemon ignored `kill -INT` entirely in an earlier attempt.)

**Description.** `--daemon` is the mode documented for unattended operation,
and every supervisor that stops an unattended process — systemd, Docker,
Kubernetes, supervisord, `kill` with no argument — sends **SIGTERM**. On
SIGTERM the log ends mid-sentence: a reader cannot tell an orderly stop from a
crash, because the "Door simulator stopped" / "Simulator stopped." pair that
SIGINT prints is exactly what distinguishes them. The project is already aware
the process dies abruptly here — `status_print`'s docstring at `cli.py:184`
cites "the buffer dies with the process on SIGTERM" as the reason it flushes —
but the shutdown itself was never wired up.

**Recommendation.** In `run_simulator`, register
`loop.add_signal_handler(signal.SIGTERM, stop_event.set)` (and SIGHUP, which
for a non-tty daemon means "terminal gone") so both take the existing graceful
path, ending with the same lines SIGINT produces and exit code `128+signum`.
Leave SIGQUIT alone — dumping core on SIGQUIT is the correct default.

---

### L3 (Low) — a bare `pip install pypowerpetdoor` ships a scripting front end that cannot work, advertises it 14 times in one command at rc 0, and points at an install line the docs do not use

**File**: `src/powerpetdoor/simulator/scripting.py:308` against
`src/powerpetdoor/simulator/commands/history.py:27`.

**Reproduction** (consumer install — wheel from this commit, no extras):

```
$ /tmp/r10fe/consumer/bin/ppd-simulator --list-scripts > bl.txt 2>&1; echo "rc=$?"
rc=0
$ grep -c "PyYAML is required" bl.txt
14
```

The 14 are seven WARNING lines followed by the same seven inline:

```
2026-… [WARNING] Failed to load script basic_cycle: PyYAML is required for script support: pip install pyyaml
…  (×7)
Built-in scripts:
  basic_cycle: (Error loading: PyYAML is required for script support: pip install pyyaml)
…  (×7)
```

Actually running one:

```
$ /tmp/r10fe/consumer/bin/ppd-simulator --script basic_cycle --oneshot -p 34101 >o1.txt 2>&1; echo "rc=$?"
rc=1
$ cat o1.txt
… Error running script 'basic_cycle': PyYAML is required for script support: pip install pyyaml
>>> All scripts FAILED
```

The sibling missing-dependency message, for the other optional extra, uses the
documented spelling:

```
$ grep -rn "pip install" src/powerpetdoor/ --include=*.py
src/powerpetdoor/simulator/commands/history.py:27:    "  pip install pypowerpetdoor[interactive]"
src/powerpetdoor/simulator/commands/info.py:357:  … (install with pip install pypowerpetdoor[interactive])
src/powerpetdoor/simulator/scripting.py:308:  … "PyYAML is required for script support: pip install pyyaml"
```

and `docs/simulator.md` (Installation) tells users `pip install
pypowerpetdoor[simulator]`.

**Description.** Three problems in one surface, all visible on a first run:

1. The guidance contradicts the docs and its own sibling message. `pip install
   pyyaml` does work, but it bypasses the declared `pyyaml>=6.0` floor and the
   extra the project asks users to install; a user who follows it will not
   have `[simulator]` recorded anywhere.
2. Fourteen copies of one sentence for one missing package. The listing already
   annotates each row; the seven WARNINGs add nothing.
3. `--list-scripts` exits **0** while reporting that none of the listed scripts
   can be loaded — a CI step that checks the exit code learns nothing.

**Recommendation.** Change `scripting.py:308` to `PyYAML is required for script
support: pip install pypowerpetdoor[simulator]`, matching `history.py:27`.
Detect the missing-PyYAML case once and print it once ("Script support
unavailable: … — install pypowerpetdoor[simulator]") instead of per script, and
let `--list-scripts` exit non-zero when every listed script failed to load.

---

### T1 (Trivial) — the two ANSI-emitting sites promise dumb-terminal protection in their docstrings and check only `isatty()`, though the `TERM == "dumb"` test already exists three files away

**File**: `src/powerpetdoor/simulator/cli.py:149`
(`if self._enabled and sys.stdout.isatty():`) and
`src/powerpetdoor/simulator/commands/control.py:104` (`if out.isatty():`),
against `src/powerpetdoor/simulator/prompt_common.py:101`
(`return os.environ.get("TERM") != "dumb"`).

**Reproduction** (real pty, `TERM=dumb`, so `use_prompt_toolkit()` returns
False and the plain prompt runs):

```
$ printf 'status\nholdtime 3\nshutdown\n' > keys2.bin
$ TERM=dumb python3 pty_drive.py 1.5 keys2.bin -- ppd-simulator -p 34301 > dumb.raw 2>&1
$ python3 -c "
data = open('dumb.raw','rb').read()
print('ESC[K count:', data.count(b'\x1b[K'))
i = data.find(b'\x1b[K'); print('context:', repr(data[i-40:i+40]))"
ESC[K count: 3
context: b'0:34301> status\r\nholdtime 3\r\nshutdown\r\n\r\x1b[K>>> Current State:\r\n  Clients: none\r\n'

$ printf 'clear\nshutdown\n' > keys3.bin
$ TERM=dumb python3 pty_drive.py 1.5 keys3.bin -- ppd-simulator -p 34302 > dumbclear.raw 2>&1
$ python3 -c "
data = open('dumbclear.raw','rb').read()
print('ESC[2J ESC[H count:', data.count(b'\x1b[2J\x1b[H'))
i = data.find(b'\x1b[2J'); print('context:', repr(data[i-30:i+30]))"
ESC[2J ESC[H count: 1
context: b'0.0.0:34302> clear\r\nshutdown\r\n\x1b[2J\x1b[H0.0.0.0:34302> \r\x1b[K>>> '
```

**Description.** Both docstrings state the intent explicitly —
`clear_line`: "Only emits the ANSI erase sequence on a real terminal: on a pipe
**or a dumb terminal** it would render as literal garbage in the output (T3)";
`clear`: "A no-op off a terminal: writing the escape sequence to a pipe **or a
dumb terminal** only injects literal garbage" — and both implement only the
pipe half. `TERM=dumb` is a real terminal by `isatty()`, so the escapes go out;
in an Emacs `M-M-x shell` buffer or a `TERM=dumb` CI shell they appear as
literal `^[[K` / `^[[2J^[[H`. The product already owns the correct predicate at
`prompt_common.py:101`, where it decides prompt_toolkit is unusable for exactly
this reason.

**Recommendation.** Extract the dumb-terminal test from `use_prompt_toolkit`
(e.g. `def _terminal_supports_ansi(stream) -> bool: return stream.isatty() and
os.environ.get("TERM") != "dumb"`) and use it at both sites, so all three
places agree on what "a terminal" means.

## Round 9 Fix Verification

All seven fixes verified by execution; both deliberate CLI behaviour changes
confirmed.

**1. Ctrl-C during `--oneshot` → 130 + "Interrupted after N of M script(s)" — VERIFIED.**

```
$ python3 sigprobe.py SIGINT 0.4 15 ppd-simulator --script full_test_suite --script basic_cycle --oneshot -p 34250
signal=SIGINT exit=130 elapsed=0.03s
--- last 6 lines of output ---
2026-08-22 23:07:16,889 [INFO]   Step 4: wait_for(condition=door_open, timeout=5)
>>> Interrupted after 0 of 2 script(s)
2026-08-22 23:07:17,126 [INFO] Door simulator stopped
Simulator stopped.
```

No `PASSED` verdict is produced and the exit code is 130, as documented.

**2. Unknown top-level script keys rejected — VERIFIED.**

```
$ cat myscripts/typo.yaml
name: Typo Script
description: has a misspelled top-level key
stepz:
  - action: cycle
$ ppd-simulator --scripts-dir myscripts -s typo --oneshot -p 34281 </dev/null >typo.out 2>&1; echo "rc=$?"
rc=1
$ grep Unknown typo.out
Error running script 'typo': Unknown top-level key(s): stepz. Use: description, name, steps
```

**3. `py.typed` ships — VERIFIED, and effective.** Present in wheel, sdist and
the installed tree, and mypy resolves real types for a downstream consumer:

```
$ unzip -l dist/pypowerpetdoor-0.3.0-py3-none-any.whl | grep py.typed
        0  2026-08-22 23:22   powerpetdoor/py.typed
$ tar tzf dist/pypowerpetdoor-0.3.0.tar.gz | grep py.typed
pypowerpetdoor-0.3.0/src/powerpetdoor/py.typed
$ ls -l /tmp/r10fe/consumer/lib/python3.14/site-packages/powerpetdoor/py.typed
-rw-r--r-- 1 prez prez 0 …

$ /tmp/r10fe/consumer/bin/python -m mypy --no-error-summary consumer_proj/use_api.py
consumer_proj/use_api.py:5: error: "PowerPetDoor" has no attribute "async_get_status"  [attr-defined]
consumer_proj/use_api.py:6: note: Revealed type is "powerpetdoor.door.DoorStatus"
consumer_proj/use_api.py:7: note: Revealed type is "Any"
```

`DoorStatus` resolves to `powerpetdoor.door.DoorStatus` rather than `Any`, and
the deliberately invented method name is caught — i.e. the marker is doing its
job, not merely present.

**4. One-sentence startup bind/resolve failures — VERIFIED.**

```
$ ppd-simulator -p 34110 < cmds.txt            # port already held
Cannot start: door server cannot use 0.0.0.0:34110 (error while attempting to bind on address ('0.0.0.0', 34110): [errno 98] address already in use); change it with --port
$ ppd-simulator --host no-such-host.invalid -p 34260
ppd-simulator: error: --host no-such-host.invalid: Name or service not known
$ ppd-simulator --control-host 300.1.1.1 --daemon 34226 -p 34225
ppd-simulator: error: --control-host 300.1.1.1: Name or service not known
```

(Note M1 above: a *different* startup failure on the same path still prints a
raw 37-line traceback.)

**5. Blank `ctl` command refused locally — VERIFIED, 0.15 s wall including
interpreter start, no daemon round trip.**

```
$ time (ppd-simulator-ctl -p 34271 "" 2>&1 | tail -1)
ppd-simulator-ctl: error: empty command
… 0.12s user 0.03s system 98% cpu 0.153 total
$ time (ppd-simulator-ctl -p 34271 "   " 2>&1 | tail -1)
ppd-simulator-ctl: error: empty command
… 0.13s user 0.03s system 98% cpu 0.157 total
```

**6. `--timeout` / `--run-for` range-checked — VERIFIED** (and `--port`,
`--scripts-dir`, `--firmware`, `--hardware`, `--host` with them):

```
$ ppd-simulator-ctl --timeout 0 status
ppd-simulator-ctl: error: --timeout 0: must be greater than 0 (use 'run <script> wait' to wait as long as a script takes)
$ ppd-simulator-ctl --timeout -3 status
ppd-simulator-ctl: error: --timeout -3: must be greater than 0 (…)
$ ppd-simulator --run-for -1 -p 34221
ppd-simulator: error: --run-for -1: must be greater than 0
$ ppd-simulator -p 99999
ppd-simulator: error: --port 99999: port must be 0-65535
$ ppd-simulator -p -5
ppd-simulator: error: --port -5: port must be 0-65535
$ ppd-simulator --scripts-dir /nonexistent/dir -l
ppd-simulator: error: --scripts-dir /nonexistent/dir: not a directory
$ ppd-simulator -f "not.a.version" …
ppd-simulator: error: Firmware version must contain only numbers (e.g., '1.2.3')
$ ppd-simulator --hardware "x" …
ppd-simulator: error: Hardware version must be in format ver.rev (e.g., '1.1')
```

**7. Unusable `--history` falls back to in-memory — VERIFIED** (real pty,
prompt_toolkit active, full three-command session, exit 0, one warning, no
traceback and no modal):

```
$ printf 'status\nholdtime 5\nexit\n' > keys1.bin
$ python3 pty_drive.py 1.5 keys1.bin -- ppd-simulator -p 34280 --history /etc
…
2026-08-22 23:08:03,833 [WARNING] Could not use history file /etc: [Errno 21] Is a directory: '/etc'; history is in-memory for this session
… 0.0.0.0:34280> status … >>> Current State: …
… >>> Hold time set to 5.0s
… >>> Shutting down...
===== child exit: 0 · captured 3765 bytes =====
```

**Deliberate outcomes — both confirmed, neither treated as a defect.**

```
$ python3 sigprobe.py SIGINT 1.5 15 ppd-simulator --daemon 34241 -p 34240
signal=SIGINT exit=130 elapsed=0.03s      # --daemon Ctrl-C → 130

$ ppd-simulator -s basic_cycle --loop --oneshot --run-for 2 -p 34320 </dev/null >loopcut.txt 2>&1; echo "rc=$?"
rc=1
$ grep -nE "elapsed|Interrupted" loopcut.txt
17:2026-08-22 23:12:54,720 [INFO] Run time (2.0s) elapsed, shutting down
19:>>> Interrupted after 0 of 1 script(s)
```

The cut-off run establishes no verdict and exits 1, with the cause named on the
preceding log line. The queue-and-replay-while-disconnected design was not
re-litigated.

## Areas Reviewed With No Findings

- **README examples, run verbatim from the bare wheel install** against a live
  simulator. Both work.
  `PowerPetDoor`: `Door status: CLOSED` / `Battery: 100%`, rc 0, and the
  simulator logged `Hold time set to 15.0s`.
  `PowerPetDoorClient`: `Settings: {'power_state': '1', … 'holdOpenTime': 1500,
  …}`, rc 0. A follow-up probe confirmed `await door.open()` really drives the
  door (`before: status= CLOSED is_closed= True` → `after : status= RISING
  is_closed= False`; `ctl status` independently showed `Door: DOOR_RISING`,
  `Hold time: 15.0s`) — the absence of an open log line is just the simulator's
  log level, not a silent no-op.
- **`python -m powerpetdoor.simulator` equivalence** claimed by the README:
  identical output to `ppd-simulator` on the consumer install.
- **Package metadata as a consumer receives it**: name/version, both console
  script entry points, `Description-Content-Type: text/markdown`, and
  `Requires-Dist` carrying the `interactive` / `simulator` / `dev` extras
  correctly. Wheel and sdist both build clean; the sdist carries `docs/` and
  `tests/`.
- **Argument-parsing corners** beyond the range checks: `--daemon abc`
  (`invalid int value: 'abc'`), ambiguous abbreviation (`--scr` →
  `ambiguous option: --scr could match --script, --script-delay,
  --scripts-dir`), unambiguous abbreviation (`--his none -l` works), unknown
  script name (lists all seven available), `--port abc` on `ctl` (rc 2).
- **`ctl` protocol limits and hostile input**: a 70 KB command line is refused
  with `ERROR: Command line too long (max 65536 bytes)`; control characters in
  an argument come back sanitized —
  `ERROR: '\x07\x1b[31mRED' is not a valid number` followed by
  `Usage: holdtime [seconds]`.
- **`ctl -i` when the daemon dies underneath it**: prints
  `>>> Simulator disconnected.` and exits 0 within the drain window. No hang,
  no traceback.
- **`ctl -i` with piped stdin**: EOF ends the session cleanly (rc 0) after
  executing the piped command — the failure mode of H1 is specific to
  `cli.py`, and `ctl.py:359-360` shows the shape the fix should take.
- **SIGPIPE**: `ppd-simulator --list-scripts | head -2` exits 0 with no
  `BrokenPipeError` noise.
- **Plain-input fallback on a real terminal** (`TERM=dumb`, pty): full
  `status` / `holdtime 3` / `shutdown` session works, results render correctly,
  exit 0. Only the escape-sequence emission (T1) is wrong there.
- **Script-DSL error quality** for a wrong action name: `Unknown action: cycle.
  Use: add_schedule, assert, battery, close, inside, log, obstruction, open,
  outside, pet_off, pet_on, pet_presence, remove_schedule, set, toggle,
  trigger, trigger_sensor, wait, wait_for` — names the offender and every
  alternative.
- **Public API surface**: `__all__` (121 names) imports cleanly from the bare
  wheel install; `py.typed` verified effective (above). The documentation-
  coverage gap reported as round-9 L3 was not re-derived and is not re-counted
  here.
