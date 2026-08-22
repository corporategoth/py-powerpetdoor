# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for the package-root public API surface (powerpetdoor.__all__).

These tests pin two contracts:

1. Every name listed in ``powerpetdoor.__all__`` actually resolves on the
   package root (no dangling ``__all__`` entries, no missing re-exports).
2. Every ``from powerpetdoor import ...`` code example in the documentation
   is executable, so the docs cannot drift from the real exports.
"""

from pathlib import Path

import pytest

import powerpetdoor

REPO_ROOT = Path(__file__).resolve().parent.parent

# Documentation files whose import examples must stay executable.
DOC_FILES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "client.md",
    REPO_ROOT / "docs" / "door.md",
    REPO_ROOT / "docs" / "simulator.md",
]


def _strip_comment(line: str) -> str:
    """Remove a trailing ``# ...`` comment from an import line."""
    return line.split("#", 1)[0].rstrip()


def _doc_import_statements(path: Path) -> list[str]:
    """Extract all ``from powerpetdoor import ...`` statements from a doc file.

    Handles both single-line imports and parenthesized multi-line imports,
    including inline ``#`` comments in the examples.
    """
    statements = []
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("from powerpetdoor import"):
            statement = _strip_comment(line)
            if statement.endswith("("):
                # Multi-line import: accumulate until the closing paren
                parts = [statement]
                i += 1
                while i < len(lines) and lines[i].strip() != ")":
                    stripped = _strip_comment(lines[i])
                    if stripped.strip():
                        parts.append(stripped)
                    i += 1
                parts.append(")")
                statement = "\n".join(parts)
            statements.append(statement)
        i += 1
    return statements


class TestAllExports:
    """The __all__ list must be accurate and complete."""

    def test_all_names_resolve_on_package_root(self):
        missing = [name for name in powerpetdoor.__all__ if not hasattr(powerpetdoor, name)]
        assert missing == []

    def test_all_has_no_duplicates(self):
        names = list(powerpetdoor.__all__)
        assert len(names) == len(set(names))

    def test_star_import_provides_all_names(self):
        namespace: dict = {}
        exec("from powerpetdoor import *", namespace)
        missing = [name for name in powerpetdoor.__all__ if name not in namespace]
        assert missing == []

    def test_documented_protocol_constants_are_exported(self):
        """The constants referenced by docs/client.md must be in __all__."""
        documented = [
            "CMD_GET_NOTIFICATIONS",
            "CMD_SET_NOTIFICATIONS",
            "CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK",
            "CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK",
            "CMD_ENABLE_AUTORETRACT",
            "CMD_DISABLE_AUTORETRACT",
            "CMD_ENABLE_CMD_LOCKOUT",
            "CMD_DISABLE_CMD_LOCKOUT",
            "FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS",
            "FIELD_LOW_BATTERY_NOTIFICATIONS",
            "FIELD_TOTAL_OPEN_CYCLES",
            "FIELD_TOTAL_AUTO_RETRACTS",
        ]
        missing = [name for name in documented if name not in powerpetdoor.__all__]
        assert missing == []

    def test_notification_and_error_exports(self):
        """CommandError and the notification-event constants are exported.

        Wave 1a handoff: the notification listener API and typed command
        errors are public API, so their names must be importable from the
        package root.
        """
        required = [
            "CommandError",
            "FIELD_REASON",
            "FIELD_SENSOR_STATE",
            "NOTIFY_LOW_BATTERY",
            "NOTIFY_SENSOR_INDOOR",
            "NOTIFY_SENSOR_OUTDOOR",
            "SENSOR_STATE_OFF",
            "SENSOR_STATE_ON",
        ]
        missing = [name for name in required if name not in powerpetdoor.__all__]
        assert missing == []
        unresolvable = [name for name in required if not hasattr(powerpetdoor, name)]
        assert unresolvable == []

    def test_command_error_carries_cmd_and_reason(self):
        """CommandError exposes cmd and reason and renders both."""
        err = powerpetdoor.CommandError("OPEN", "Door is locked")
        assert err.cmd == "OPEN"
        assert err.reason == "Door is locked"
        assert "OPEN" in str(err)
        assert "Door is locked" in str(err)


class TestDocImports:
    """Every documented package-root import block must execute."""

    @pytest.mark.parametrize("doc_path", DOC_FILES, ids=lambda p: p.name)
    def test_doc_contains_import_examples(self, doc_path):
        # Guard against the extractor silently matching nothing after a doc rewrite.
        assert _doc_import_statements(doc_path), f"No import examples found in {doc_path}"

    @pytest.mark.parametrize(
        ("doc_path", "statement"),
        [(path, statement) for path in DOC_FILES for statement in _doc_import_statements(path)],
        ids=lambda value: value.name if isinstance(value, Path) else value.split("\n")[0][:60],
    )
    def test_doc_import_statement_executes(self, doc_path, statement):
        namespace: dict = {}
        exec(statement, namespace)  # Raises ImportError on any stale name
