# Testing Gaps Report

This file is **auto-generated** by CI after each test run. Do not edit manually.

**Last updated:** 2026-08-22 15:33 UTC

## Summary

| Metric | Value |
|--------|-------|
| Line Coverage | 100.00% |
| Branch Coverage | 100.00% |
| Lines Covered | 6,662 / 6,662 |
| Branches Covered | 2,368 / 2,368 |
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
- `pragma: no cover` - Explicitly annotated lines (see Pragma Exclusions below)
- `def __repr__` - String representation methods
- `raise NotImplementedError` - Abstract method stubs
- `if TYPE_CHECKING:` - Type-checking-only imports
- `if __name__ == .__main__.:` - Script entry-point guards
- `@overload` - Typing overload declarations
- `(^\s*\.\.\.\s*$)|(:\s*\.\.\.\s*$)` - Ellipsis stub bodies

### Pragma Exclusions

**3 lines** across **2 files** in **3 annotations** are excluded via `# pragma: no cover` or `# pragma: no branch`.

#### `simulator/cli.py` (2 lines)

| Lines | Type | Reason | Code |
|-------|------|--------|------|
| 100 | no branch | defensive: enable() always installs a handler | `if self._handler:` |
| 689 | no branch | bound after start() | `if simulator.server and simulator.server.sockets:` |

#### `simulator/ctl.py` (1 lines)

| Lines | Type | Reason | Code |
|-------|------|--------|------|
| 657 | no cover | defensive: socket_reader swallows its own cancellation; only an outer cancel landing exactly on this await would raise | `except asyncio.CancelledError:` |

