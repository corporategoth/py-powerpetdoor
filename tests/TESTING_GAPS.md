# Testing Gaps Report

This file is **auto-generated** by CI after each test run. Do not edit manually.

**Last updated:** 2026-08-24 02:26 UTC

## Summary

| Metric | Value |
|--------|-------|
| Line Coverage | 100.00% |
| Branch Coverage | 100.00% |
| Lines Covered | 6,599 / 6,599 |
| Branches Covered | 2,300 / 2,300 |
| Lines Missing | 0 |

## Coverage by Category

| Category | Files | Coverage | Status |
|----------|-------|----------|--------|
| Core Library | 8 | 100.0% | :green_circle: |
| Simulator | 5 | 100.0% | :green_circle: |
| Simulator CLI | 3 | 100.0% | :green_circle: |
| Simulator Commands | 12 | 100.0% | :green_circle: |

## Status: 100% Coverage :green_circle:

All code is covered by tests. No gaps to report.

## Coverage Exclusions

### Gate Configuration

What the 100% gate measures, read from `pyproject.toml` (`tests/test_gaps_report.py` asserts each value):

- Measured roots (`coverage.run.source`): `src/powerpetdoor`
- Branch coverage (`coverage.run.branch`): `true`
- Gate threshold (`coverage.report.fail_under`): `100`

### Automatic Exclusions

The following are excluded from coverage by configuration (`pyproject.toml`):

- `*/__init__.py` - Package init files
- `*/__main__.py` - Entry point files
- `#\s*pragma:\s*no\s+cover\s*($|\()` - Explicitly annotated lines (see Pragma Exclusions below)
- `^\s*def __repr__` - String representation methods
- `^\s*raise NotImplementedError` - Abstract method stubs
- `^\s*if TYPE_CHECKING:` - Type-checking-only imports
- `^\s*if __name__ == .__main__.:` - Script entry-point guards
- `^\s*@overload\s*$` - Typing overload declarations
- `(^\s*\.\.\.\s*$)|(:\s*\.\.\.\s*$)` - Ellipsis stub bodies
- `#\s*pragma:\s*no\s+branch\s*($|\()` - Explicitly annotated partial branches (see Pragma Exclusions below)

### Prose-Triggered Exclusions

None. Every `exclude_lines` and `partial_branches` pattern above matches only the construct it names, never a string literal on a line carrying a statement.

### Pragma Exclusions

**3 lines** across **2 files** in **3 annotations** are excluded via `# pragma: no cover` or `# pragma: no branch`.

#### `simulator/cli.py` (2 lines)

| Lines | Type | Reason | Code |
|-------|------|--------|------|
| 128 | no branch | defensive: enable() always installs a handler | `if self._handler:` |
| 962 | no branch | bound after start() | `if simulator.server and simulator.server.sockets:` |

#### `simulator/ctl.py` (1 lines)

| Lines | Type | Reason | Code |
|-------|------|--------|------|
| 675 | no cover | defensive: socket_reader swallows its own cancellation; only an outer cancel landing exactly on this await would raise | `except asyncio.CancelledError:` |
