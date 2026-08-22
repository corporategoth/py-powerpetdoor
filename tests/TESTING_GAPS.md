# Testing Gaps Report

This file is **auto-generated** by CI after each test run. Do not edit manually.

**Last updated:** 2026-08-22 00:00 UTC

## Summary

| Metric | Value |
|--------|-------|
| Line Coverage | 100.00% |
| Branch Coverage | 100.00% |
| Lines Covered | 5,712 / 5,712 |
| Branches Covered | 2,074 / 2,074 |
| Lines Missing | 0 |

## Coverage by Category

| Category | Files | Coverage | Status |
|----------|-------|----------|--------|
| Core Library | 6 | 100.0% | :green_circle: |
| Simulator | 5 | 100.0% | :green_circle: |
| Simulator CLI | 3 | 100.0% | :green_circle: |
| Simulator Commands | 12 | 100.0% | :green_circle: |

## Status: 100% Coverage :green_circle:

All code is covered by tests. No gaps to report.

## Coverage Exclusions

### Automatic Exclusions

The following are excluded from coverage by configuration (`pyproject.toml`):

- `*/__init__.py` - Package init files
- `*/__main__.py` - Entry point files
- `def __repr__` - String representation methods
- `raise NotImplementedError` - Abstract method stubs
- `if TYPE_CHECKING:` - Type-checking-only imports
- `@overload` - Typing overload declarations

### Pragma Exclusions

**5 lines** across **2 files** are excluded via `# pragma: no cover` or `# pragma: no branch`.

#### `simulator/cli.py` (2 lines)

| Lines | Type | Reason | Code |
|-------|------|--------|------|
| 97 | no branch | defensive: enable( | `if self._handler:` |
| 598 | no branch | bound after start( | `if simulator.server and simulator.server.sockets:` |

#### `simulator/ctl.py` (3 lines)

| Lines | Type | Reason | Code |
|-------|------|--------|------|
| 337 | no cover | defensive: Linux selectors swallow errors for dead fds, so this cannot be triggered deterministically | `except Exception:` |
| 555 | no cover | defensive: both prompt paths signal EOF by returning None rather than raising | `except EOFError:` |
| 606 | no cover | defensive: socket_reader swallows its own cancellation; only an outer cancel landing exactly on this await would raise | `except asyncio.CancelledError:` |

