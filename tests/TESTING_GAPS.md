# Testing Gaps Report

This file is **auto-generated** by CI after each test run. Do not edit manually.

**Last updated:** 2026-08-22 08:45 UTC

## Summary

| Metric | Value |
|--------|-------|
| Line Coverage | 100.00% |
| Branch Coverage | 100.00% |
| Lines Covered | 6,391 / 6,391 |
| Branches Covered | 2,280 / 2,280 |
| Lines Missing | 0 |

## Coverage by Category

| Category | Files | Coverage | Status |
|----------|-------|----------|--------|
| Build Scripts | 1 | 100.0% | :green_circle: |
| Core Library | 7 | 100.0% | :green_circle: |
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

**5 lines** across **2 files** in **5 annotations** are excluded via `# pragma: no cover` or `# pragma: no branch`.

#### `simulator/cli.py` (2 lines)

| Lines | Type | Reason | Code |
|-------|------|--------|------|
| 100 | no branch | defensive: enable() always installs a handler | `if self._handler:` |
| 689 | no branch | bound after start() | `if simulator.server and simulator.server.sockets:` |

#### `simulator/ctl.py` (3 lines)

| Lines | Type | Reason | Code |
|-------|------|--------|------|
| 365 | no cover | defensive: Linux selectors swallow errors for dead fds, so this cannot be triggered deterministically | `except Exception:` |
| 591 | no cover | defensive: both prompt paths signal EOF by returning None rather than raising | `except EOFError:` |
| 642 | no cover | defensive: socket_reader swallows its own cancellation; only an outer cancel landing exactly on this await would raise | `except asyncio.CancelledError:` |

