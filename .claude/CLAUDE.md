# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

pypowerpetdoor is a Python library for communicating with Power Pet Door
WiFi-enabled pet doors (High Tech Pet). It is a dependency-light asyncio
library with three parts:

- **`door.py`** — `PowerPetDoor`, the high-level Pythonic interface (cached
  state, properties, async control methods, callbacks).
- **`client.py`** — `PowerPetDoorClient`, the low-level protocol client
  (connection lifecycle, keepalive, reconnect, message framing/dispatch).
- **`simulator/`** — a full door simulator (server, YAML scripting, and an
  interactive CLI). The simulator CLI (`ppd-simulator`) and its remote-control
  companion (`ppd-simulator-ctl`) are the ONLY user-facing front end in this
  project. There is no web UI.

Supporting modules: `const.py` (protocol constants), `schedule.py` (schedule
compress/validate/diff utilities), `tz_utils.py` (timezone helpers).

The wire protocol is documented in `docs/protocol.md` and door behavior in
`docs/operation.md`. Consult these before changing protocol handling — the
protocol is reverse-engineered from a real device and the simulator must stay
faithful to observed device behavior.

Related repos: `ha-powerpetdoor` (Home Assistant integration built on this
library), `git.neuromancy.net/pypi/ostinato-powerpetdoor` (Ostinato plugin).

## Gitea is the source of truth. NEVER write to GitHub. (MANDATORY)

`origin` is **Gitea** (`git.neuromancy.net`). GitHub is a **push mirror**,
and a mirror is downstream: anything created there is either overwritten by
the next sync or stranded forever.

**Never, on GitHub:** create or edit a release, push, merge a pull request,
commit, edit the wiki, or move a tag. Not with `gh`, not through the web UI,
not through the API.

**Reading GitHub is fine and often necessary** - that is where users file
issues and pull requests, and `gh issue view` / `gh pr view` are the right
tools for it. The prohibition is on *writing*.

### Why - this has already gone wrong

A push mirror carries git refs only: "branches, tags, and commits", per
Gitea's own docs. A **release is a database object on each forge, not git
data**, so it can never cross that way.

pypowerpetdoor v0.4.1 was released by creating the release **on GitHub**.
The tag existed on both sides, so it looked fine - but Gitea had no release
object, the release-sync webhook had nothing to fire on, and the two forges
disagreed until it was recreated by hand. v0.4.0, cut correctly on Gitea,
appears on both at the same timestamp. That is the difference.

There is a second, sharper reason for pypowerpetdoor specifically:
`.github/workflows/release.yml` triggers on `release: published`. What
publishes to PyPI is therefore the **GitHub release object**. Cutting a
release on Gitea drives that chain correctly through the webhook; cutting it
on GitHub skips Gitea entirely and leaves the source of truth behind.

### How to cut a release

1. Tag and push to Gitea.
2. Create the release **in Gitea** - its UI, its API, or
   `tea releases create --repo <owner>/<repo> --tag vX.Y.Z --title ... --note ...`.
3. Let the webhook carry it to GitHub. Do not create it there yourself.

## Component Reuse (Critical)

**Before writing any code, check for existing implementations.** This codebase
emphasizes DRY principles. Duplicating functionality is strongly discouraged.

1. **Always search first**: before adding a helper, search `const.py`,
   `schedule.py`, `tz_utils.py`, `simulator/prompt_common.py`, and
   `simulator/commands/base.py` for an existing implementation.
2. **Two implementations = refactor**: if you find two or more similar
   implementations (even with minor variations), refactor into a shared helper.
3. **Extend, don't duplicate**: if an existing helper almost fits, extend it
   with parameters rather than creating a new one.
4. **Client/simulator symmetry**: the simulator must speak exactly the protocol
   the client speaks. Shared constants live in `const.py` — never inline
   protocol strings in either side.

## Development Commands

```bash
# Install with development dependencies (uv preferred)
uv sync --all-extras
# Or with pip:
pip install -e ".[dev]"

# Run tests (pytest-xdist parallel by default via addopts)
uv run pytest

# Run a single test file
uv run pytest tests/test_client.py

# Run tests with coverage (fails under 100%)
uv run pytest --cov

# Linting / formatting / types
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src

# Run the simulator interactively
uv run ppd-simulator

# Run a simulator script headless (CI style; exit code reflects pass/fail)
uv run python -m powerpetdoor.simulator --script full_test_suite --oneshot
```

## Version Matrix Maintenance (MANDATORY)

**When committing code, ensure all Python version references are current.**
The project supports a declared matrix of CPython versions (currently
**3.11–3.14**). All version references below must match that matrix exactly.

### Files that must stay in sync

| File | What to update |
|------|---------------|
| `.github/workflows/test.yml` | `REFERENCE_PYTHON` env var and `matrix.python-version` arrays |
| `.github/workflows/release.yml` | `matrix.python-version` array and the publish job's Python pin |
| `pyproject.toml` | `requires-python`, `Programming Language :: Python :: 3.x` classifiers |
| `pyproject.toml` | `target-version` (ruff), `python_version` (mypy) — oldest supported Python |

### Rules

1. **Remove versions the project drops** promptly (typically at EOL) — don't
   test against runtimes outside the declared matrix
2. **Add new stable versions** to the matrix promptly when they reach their
   first stable release
3. **Minimum Python version** settings track the oldest version in the
   declared matrix
4. **`REFERENCE_PYTHON`** is the latest stable Python; it controls which matrix
   entry uploads coverage data and which version runs single-version jobs (lint,
   coverage-report)

### Manually tracked pins (MANDATORY)

`.github/dependabot.yml` covers `github-actions` (root workflows **and** the
composite action directory) plus `uv`. Two things sit outside anything
automation can reach, so they rot silently unless a human moves them. Check
them whenever you touch CI, and at least once per release:

| Pin | Where | Why automation cannot see it |
|-----|-------|------------------------------|
| `neuromancy/workflows/.gitea/workflows/sync-github-wiki.yml@<sha>` | `.gitea/workflows/sync-wiki.yml` | Dependabot has no Gitea support. This is also the **only** `uses:` in the repo that receives a secret, so its SHA pin matters more than the rest |
| Transitive versions in `uv.lock` | `uv.lock` | `uv sync` never upgrades what is already pinned, and Dependabot's grouping moves direct dependencies. Run `uv lock --upgrade` periodically (the weekly fuzz cron is a natural home) and re-run the full suite |

`tzdata` deserves specific attention: it is the library's only runtime
dependency and the whole `tz_utils`/schedule feature reads IANA rules out of
it, so a stale lock means CI and local dev test against an old DST database.
Consumers resolve it fresh (`dependencies = ["tzdata>=2024.1"]`), so this is a
testing-coverage gap rather than a shipping one - but it is still the pin most
worth watching.

## Testing Requirements (MANDATORY)

**Every code change MUST include corresponding tests.** This is non-negotiable.

### Rules for Code Changes

1. **New Features**: Must have unit tests covering:
   - Happy path (normal operation)
   - Edge cases (boundary values, empty inputs)
   - Error cases (invalid inputs, failures)
   - At least one negative test per public function

2. **Bug Fixes**: Must include:
   - A test that reproduces the bug (fails without the fix)
   - The fix that makes the test pass
   - Regression tests if applicable

3. **Refactoring**: Must:
   - Maintain existing test coverage (no reduction)
   - Update tests if behavior changes
   - Add tests for any new code paths

4. **Protocol Changes**: Must include:
   - Tests on both sides (client sends / simulator handles, and vice versa)
   - An update to `docs/protocol.md` if the wire format changed

### Pre-Commit Checklist

Before committing, verify:
- [ ] `uv run pytest` passes
- [ ] `uv run pytest --cov` shows 100% for modified files
- [ ] `uv run pytest --ignore=tests/fuzz --cov` **also** reaches 100% — this is
      what CI's unit matrix runs, so the deterministic suite must never lean on
      randomized fuzz coverage to pass the gate
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` pass
- [ ] `uv run mypy src` passes
- [ ] New code has corresponding tests following existing patterns in `tests/`

### Test Locations

| Code Location | Test Location |
|---------------|---------------|
| `src/powerpetdoor/client.py` | `tests/test_client.py` |
| `src/powerpetdoor/door.py` | `tests/test_door.py` |
| `src/powerpetdoor/schedule.py` | `tests/test_schedule.py` |
| `src/powerpetdoor/tz_utils.py` | `tests/test_tz_utils.py` |
| `src/powerpetdoor/simulator/` | `tests/simulator/` |
| `src/powerpetdoor/simulator/scripts/*.yaml` | `tests/simulator/scripts/` |

### Coverage Requirements

- **100% line coverage** for all new code
- **100% branch coverage** for all new code
- `# pragma: no cover` requires justification and approval

### Test Quality Rules (Critical)

**Every test must have a single, deterministic expected outcome.** These rules
are non-negotiable:

1. **Tests must be specific**: Each test must assert exactly ONE expected
   result. If you find yourself writing `assert x in (a, b)` for contradictory
   outcomes, the test is wrong. Either the test setup is incomplete, the test
   doesn't understand the actual behavior, or the production code has a bug
   that needs fixing.

2. **Never accept contradictory outcomes**: A test that accepts both success
   AND failure is definitionally wrong — it means you don't know what the code
   actually does. Read the code, then assert the one correct outcome.

3. **Know the answer before running the test**: If you don't know what value to
   expect, read the production code (and `docs/protocol.md` /
   `docs/operation.md`) until you do. Only then write a definitive assertion.

4. **Fix tests properly, never hunt for results**: When a test fails,
   investigate WHY. Don't just change the expected value to match what
   happened. The failure might indicate missing setup, a wrong assumption, or
   an actual bug in production code.

5. **Never remove tests to "fix" failures**: Deleting a failing test is NOT
   fixing it — it's hiding the problem and leaving code untested. The ONLY
   valid reason to remove a test is complete redundancy with another test.
   Difficulty or complexity is NEVER a valid reason to remove a test.

6. **No fake tests**: No `assert True`, no tautologies, no tests that cannot
   fail, no tests that merely read back a value they just set.

7. **Async determinism**: Never sleep-and-hope. Use the simulator's virtual
   time / event hooks, `asyncio.Event`s, or awaited futures so tests are
   deterministic on slow CI runners.

8. **Assert at a boundary that decides something**: where a limit gates
   real behaviour (the schedule window's inclusive start and exclusive
   end, a wire-string length limit, the low-battery crossing), assert on
   both sides of it. Coverage cannot see this class at all. Do NOT sweep
   every numeric constant in the tree for its own sake — a boundary test
   that pins no behaviour is noise.

9. **Make the second operand of a compound condition decisive**: `if A and
   B:` is a single branch point with two destinations, so 100% branch
   coverage is reached without ever running `A and not B`. Any test for a
   compound guard must include the case where the *second* operand is the
   one that decides — the guards whose comment says a field "may be
   absent" need a test with the field actually absent.

10. **Pin the wire, by literal**: both sides of this project read the same
    symbol from `const.py`, so renaming a constant or re-spelling its
    value changes what goes on the wire with the whole suite green.
    `tests/test_wire_constants.py` derives the perimeter from
    `docs/protocol.md` and pins each value literally; a newly documented
    constant has to be added there. This does *not* extend to internal
    resource bounds — pin those only where a test asserts the behaviour
    the bound produces.

### Git Usage Rules (Critical)

**Never use git commands to revert uncommitted changes.**

1. **No `git checkout` / `git restore` to undo changes**: they destroy ALL
   uncommitted changes in the file, including work you intended to keep.
2. **Manual fixes only**: fix mistakes by editing the file.
3. **Git revert only when explicitly requested** by the user.

## Threat Model (read before "hardening" anything)

The client dials **out** to a pet door on a home LAN; nothing connects
inward. The simulator is a test tool. Defending against a *hostile peer*
therefore defends a scenario that does not exist, and machinery that does
so has been removed from this tree once already — do not reintroduce it
(log throttling, bounded in-flight dispatch, transport backpressure,
write-backlog caps).

What stays, because it is correctness rather than security:

- The 64 KiB receive cap: a stuck or malfunctioning door must not exhaust
  memory.
- "Never raises on arbitrary input" in `framing.py`, `client.py` and
  `simulator/protocol.py` — garbage bytes, a brace inside a string, split
  frames, non-ASCII, `except (ValueError, RecursionError)` on decode. A
  real door motivated these.
- `sanitize_text`/`sanitize_field` on every network-derived value that
  reaches a log or a terminal.
- The control channel's `127.0.0.1` default bind.
