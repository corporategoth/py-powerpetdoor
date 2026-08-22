# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for scripts/generate_gaps_report.py.

CI runs this script on every push and auto-commits its output to
``tests/TESTING_GAPS.md`` - the project's self-reported view of its own
testing gaps. It contains real logic that can silently misreport (regex
scanning with consecutive-line grouping, range collapsing, a path-based
category split), so it is tested and covered like everything else (R3-M5).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_gaps_report.py"


def _load_module():
    """Import the script by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("generate_gaps_report", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gaps = _load_module()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway cwd with a tests/ directory, like the repo root."""
    (tmp_path / "tests").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_coverage(workspace: Path, data: dict) -> None:
    (workspace / "coverage.json").write_text(json.dumps(data))


FULL_COVERAGE = {
    "totals": {
        "percent_covered": 100.0,
        "covered_lines": 10,
        "missing_lines": 0,
        "covered_branches": 4,
        "missing_branches": 0,
    },
    "files": {
        "src/powerpetdoor/client.py": {
            "summary": {
                "percent_covered": 100.0,
                "num_statements": 10,
                "num_branches": 4,
                "covered_lines": 10,
                "missing_lines": 0,
            },
            "missing_lines": [],
            "missing_branches": [],
        }
    },
}


class TestGroupLines:
    """Consecutive line numbers collapse into ranges."""

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            ([], ""),
            ([5], "5"),
            ([5, 6, 7], "5-7"),
            ([5, 7], "5, 7"),
            ([1, 2, 4, 5, 6, 9], "1-2, 4-6, 9"),
            ([1, 3, 4, 5], "1, 3-5"),
        ],
    )
    def test_group_lines(self, lines, expected):
        assert gaps._group_lines(lines) == expected


class TestCategorize:
    """The path split decides which bucket a module is reported under."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("src/powerpetdoor/client.py", "Core Library"),
            ("src/powerpetdoor/schedule.py", "Core Library"),
            ("src/powerpetdoor/sanitize.py", "Core Library"),
            ("src/powerpetdoor/simulator/engine.py", "Simulator"),
            ("src/powerpetdoor/simulator/cli.py", "Simulator CLI"),
            ("src/powerpetdoor/simulator/ctl.py", "Simulator CLI"),
            ("src/powerpetdoor/simulator/prompt_common.py", "Simulator CLI"),
            ("src/powerpetdoor/simulator/commands/base.py", "Simulator Commands"),
            ("scripts/generate_gaps_report.py", "Build Scripts"),
        ],
    )
    def test_categorize(self, path, expected):
        assert gaps._categorize(path) == expected

    def test_every_real_source_file_lands_in_a_named_bucket(self):
        """A new module must not silently fall through to "Core Library"."""
        buckets = {
            gaps._categorize(str(path.relative_to(REPO_ROOT)))
            for path in (REPO_ROOT / "src" / "powerpetdoor").rglob("*.py")
        }
        assert buckets == {"Core Library", "Simulator", "Simulator CLI", "Simulator Commands"}


class TestCollectPragmaExclusions:
    """Pragma scanning: grouping, reason inheritance, and code extraction."""

    @staticmethod
    def _source_dir(tmp_path: Path) -> Path:
        source_dir = tmp_path / "src" / "powerpetdoor"
        source_dir.mkdir(parents=True)
        return source_dir

    def test_single_pragma_with_reason(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text(
            "x = 1\ny = 2  # pragma: no cover (defensive: cannot happen)\n"
        )

        found = gaps._collect_pragma_exclusions(source_dir)

        assert found == {
            "src/powerpetdoor/mod.py": [
                {
                    "line": 2,
                    "end_line": 2,
                    "pragma_type": "cover",
                    "reason": "defensive: cannot happen",
                    "code": "y = 2",
                }
            ]
        }

    def test_pragma_without_a_reason(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text("if x:  # pragma: no branch\n    pass\n")

        entry = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"][0]

        assert entry["pragma_type"] == "branch"
        assert entry["reason"] == ""
        assert entry["code"] == "if x:"

    def test_consecutive_same_type_pragmas_group_into_a_range(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text(
            "a = 1  # pragma: no cover\nb = 2  # pragma: no cover (the real reason)\n"
        )

        entries = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"]

        assert len(entries) == 1
        assert (entries[0]["line"], entries[0]["end_line"]) == (1, 2)
        # The later line's reason is inherited when the first had none.
        assert entries[0]["reason"] == "the real reason"

    def test_grouped_lines_keep_the_first_reason(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text(
            "a = 1  # pragma: no cover (first)\nb = 2  # pragma: no cover (second)\n"
        )

        entries = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"]

        assert len(entries) == 1
        assert entries[0]["reason"] == "first"

    def test_different_pragma_types_do_not_group(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text(
            "a = 1  # pragma: no cover\nb = 2  # pragma: no branch\n"
        )

        entries = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"]

        assert [e["pragma_type"] for e in entries] == ["cover", "branch"]

    def test_non_adjacent_pragmas_do_not_group(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text(
            "a = 1  # pragma: no cover\nspacer = 0\nb = 2  # pragma: no cover\n"
        )

        entries = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"]

        assert [(e["line"], e["end_line"]) for e in entries] == [(1, 1), (3, 3)]

    @pytest.mark.parametrize(
        ("comment", "reason"),
        [
            (
                "# pragma: no branch (defensive: enable() always installs a handler)",
                "defensive: enable() always installs a handler",
            ),
            ("# pragma: no branch (bound after start())", "bound after start()"),
            ("# pragma: no cover (a (nested) reason)", "a (nested) reason"),
        ],
        ids=["call-mid-reason", "call-at-end", "nested-parens"],
    )
    def test_reason_containing_parentheses_is_not_truncated(self, tmp_path, comment, reason):
        """Two of this project's own five pragmas hit this (R4-L2).

        The non-greedy `([^)]+)` stopped at the first `)`, so the committed
        TESTING_GAPS.md reported "defensive: enable(" and "bound after
        start(" as the project's coverage-exclusion justifications.
        """
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text(f"x = 1  {comment}\n")

        entry = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"][0]

        assert entry["reason"] == reason
        assert entry["code"] == "x = 1"

    def test_trailing_text_after_the_reason_is_not_a_reason(self, tmp_path):
        """The anchor requires the pragma comment to end the line."""
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text("x = 1  # pragma: no cover (why) and more\n")

        entry = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"][0]

        assert entry["reason"] == ""

    def test_pragma_text_inside_a_string_literal_is_not_a_pragma(self, tmp_path):
        """The generator's own report body contains these words (R4-T1).

        Scanning ``scripts/`` as well as ``src/`` means the generator scans
        itself, and a raw line regex reported its markdown output strings as
        the project's coverage exclusions.
        """
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text(
            'MESSAGE = "excluded via `# pragma: no cover` or `# pragma: no branch`"\n'
            "REAL = 1  # pragma: no cover (the only real one)\n"
        )

        entries = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"]

        assert len(entries) == 1
        assert entries[0]["line"] == 2
        assert entries[0]["reason"] == "the only real one"

    def test_untokenizable_file_reports_no_pragmas(self, tmp_path):
        """A file that will not parse is skipped rather than guessed at."""
        source_dir = self._source_dir(tmp_path)
        (source_dir / "broken.py").write_text("def f(:\n    x = 1  # pragma: no cover (nope)\n")

        assert gaps._collect_pragma_exclusions(source_dir) == {}

    def test_scripts_root_is_scanned_too(self, tmp_path):
        """scripts/ is inside the coverage gate, so its pragmas must show (R4-T1)."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "tool.py").write_text("x = 1  # pragma: no cover (build-only)\n")

        found = gaps._collect_pragma_exclusions(scripts_dir, root=tmp_path)

        assert found == {
            "scripts/tool.py": [
                {
                    "line": 1,
                    "end_line": 1,
                    "pragma_type": "cover",
                    "reason": "build-only",
                    "code": "x = 1",
                }
            ]
        }

    def test_pragma_without_a_space_does_not_crash(self, tmp_path):
        """`#pragma:` matches the regex; a literal .index() would raise."""
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text("y = 2  #pragma: no cover (tight)\n")

        entry = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"][0]

        assert entry["code"] == "y = 2"
        assert entry["reason"] == "tight"

    def test_standalone_pragma_comment_keeps_the_whole_line(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "mod.py").write_text("# pragma: no cover\n")

        entry = gaps._collect_pragma_exclusions(source_dir)["src/powerpetdoor/mod.py"][0]

        assert entry["code"] == "# pragma: no cover"

    def test_init_and_main_files_are_skipped(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "__init__.py").write_text("a = 1  # pragma: no cover\n")
        (source_dir / "__main__.py").write_text("a = 1  # pragma: no cover\n")

        assert gaps._collect_pragma_exclusions(source_dir) == {}

    def test_undecodable_file_is_skipped(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "broken.py").write_bytes(b"\xff\xfe a = 1  # pragma: no cover\n")
        (source_dir / "ok.py").write_text("a = 1  # pragma: no cover\n")

        found = gaps._collect_pragma_exclusions(source_dir)

        assert list(found) == ["src/powerpetdoor/ok.py"]

    def test_file_with_no_pragmas_is_absent(self, tmp_path):
        source_dir = self._source_dir(tmp_path)
        (source_dir / "clean.py").write_text("a = 1\nb = 2\n")

        assert gaps._collect_pragma_exclusions(source_dir) == {}


class TestMain:
    """End-to-end rendering from a synthetic coverage.json."""

    def test_missing_coverage_json_writes_an_error_document(self, workspace):
        assert gaps.main() == 1

        written = (workspace / "tests" / "TESTING_GAPS.md").read_text()
        assert "**Error**: No `coverage.json` found." in written

    def test_missing_coverage_json_in_stdout_mode(self, workspace, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])

        assert gaps.main() == 1

        assert "**Error**: No `coverage.json` found." in capsys.readouterr().out
        assert not (workspace / "tests" / "TESTING_GAPS.md").exists()

    def test_full_coverage_report(self, workspace, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py"])
        write_coverage(workspace, FULL_COVERAGE)

        assert gaps.main() == 0

        report = (workspace / "tests" / "TESTING_GAPS.md").read_text()
        assert "| Line Coverage | 100.00% |" in report
        assert "| Branch Coverage | 100.00% |" in report
        assert "| Lines Covered | 10 / 10 |" in report
        assert "| Branches Covered | 4 / 4 |" in report
        assert "## Status: 100% Coverage :green_circle:" in report
        assert "| Core Library | 1 | 100.0% | :green_circle: |" in report
        # No src/powerpetdoor tree in this workspace -> no pragma section
        assert "No `# pragma: no cover` or `# pragma: no branch` annotations found." in report

    def test_zero_branch_project_reports_100_percent(self, workspace, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py"])
        data = json.loads(json.dumps(FULL_COVERAGE))
        data["totals"]["covered_branches"] = 0
        data["totals"]["missing_branches"] = 0
        write_coverage(workspace, data)

        assert gaps.main() == 0

        report = (workspace / "tests" / "TESTING_GAPS.md").read_text()
        assert "| Branch Coverage | 100.00% |" in report

    def test_gaps_are_listed_with_ranges(self, workspace, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])
        write_coverage(
            workspace,
            {
                "totals": {
                    "percent_covered": 80.0,
                    "covered_lines": 8,
                    "missing_lines": 2,
                    "covered_branches": 3,
                    "missing_branches": 1,
                },
                "files": {
                    "src/powerpetdoor/simulator/cli.py": {
                        "summary": {
                            "percent_covered": 80.0,
                            "num_statements": 10,
                            "num_branches": 4,
                            "covered_lines": 8,
                            "missing_lines": 2,
                        },
                        "missing_lines": [10, 11, 40],
                        "missing_branches": [[10, 12]],
                    },
                    "src/powerpetdoor/simulator/commands/base.py": {
                        "summary": {
                            "percent_covered": 100.0,
                            "num_statements": 5,
                            "num_branches": 0,
                            "covered_lines": 5,
                            "missing_lines": 0,
                        },
                        "missing_lines": [],
                        "missing_branches": [],
                    },
                },
            },
        )

        assert gaps.main() == 0

        report = capsys.readouterr().out
        assert "## Current Gaps (1 files)" in report
        assert "| `simulator/cli.py` | 10 | 2 | 80.0% |" in report
        assert "**`simulator/cli.py`**: 10-11, 40" in report
        assert "| Simulator CLI | 1 (1 with gaps) | 80.0% | :yellow_circle: |" in report
        assert "| Simulator Commands | 1 | 100.0% | :green_circle: |" in report
        # The denominator is covered + missing. Using covered alone renders
        # "8 / 8" on a project with 2 uncovered lines - a full denominator
        # the report does not have (R4-L4), and only the all-zero fixture
        # asserted this row.
        assert "| Lines Covered | 8 / 10 |" in report
        assert "| Branches Covered | 3 / 4 |" in report
        assert "| Lines Missing | 2 |" in report
        # Not written to disk in --stdout mode
        assert not (workspace / "tests" / "TESTING_GAPS.md").exists()

    def test_near_complete_files_are_still_listed_as_gaps(self, workspace, monkeypatch, capsys):
        """A 99.5% file must appear in the report whose only job is gaps (R4-L4)."""
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])
        write_coverage(
            workspace,
            {
                "totals": {
                    "percent_covered": 99.5,
                    "covered_lines": 199,
                    "missing_lines": 1,
                    "covered_branches": 0,
                    "missing_branches": 0,
                },
                "files": {
                    "src/powerpetdoor/door.py": {
                        "summary": {
                            "percent_covered": 99.5,
                            "num_statements": 200,
                            "num_branches": 0,
                            "covered_lines": 199,
                            "missing_lines": 1,
                        },
                        "missing_lines": [42],
                        "missing_branches": [],
                    },
                },
            },
        )

        assert gaps.main() == 0

        report = capsys.readouterr().out
        assert "## Current Gaps (1 files)" in report
        assert "| `door.py` | 200 | 1 | 99.5% |" in report

    def test_gap_files_are_listed_worst_first(self, workspace, monkeypatch, capsys):
        """Sorting is what puts the file that needs attention at the top (R4-L4)."""
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])

        def _file(percent, statements, missing):
            return {
                "summary": {
                    "percent_covered": percent,
                    "num_statements": statements,
                    "num_branches": 0,
                    "covered_lines": statements - missing,
                    "missing_lines": missing,
                },
                "missing_lines": [1] * missing,
                "missing_branches": [],
            }

        write_coverage(
            workspace,
            {
                "totals": {
                    "percent_covered": 85.0,
                    "covered_lines": 170,
                    "missing_lines": 30,
                    "covered_branches": 0,
                    "missing_branches": 0,
                },
                "files": {
                    # Declared best-first, so only the sort can reorder them.
                    "src/powerpetdoor/door.py": _file(95.0, 100, 5),
                    "src/powerpetdoor/client.py": _file(75.0, 100, 25),
                },
            },
        )

        assert gaps.main() == 0

        report = capsys.readouterr().out
        assert report.index("| `client.py` |") < report.index("| `door.py` |")

    def test_gap_file_with_no_missing_line_numbers_is_skipped_in_detail(
        self, workspace, monkeypatch, capsys
    ):
        """A <100% file with no line list contributes no detail section."""
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])
        write_coverage(
            workspace,
            {
                "totals": {
                    "percent_covered": 90.0,
                    "covered_lines": 9,
                    "missing_lines": 1,
                    "covered_branches": 0,
                    "missing_branches": 0,
                },
                "files": {
                    "src/powerpetdoor/door.py": {
                        "summary": {
                            "percent_covered": 90.0,
                            "num_statements": 10,
                            "num_branches": 2,
                            "covered_lines": 9,
                            "missing_lines": 1,
                        },
                        "missing_lines": [],
                        "missing_branches": [],
                    }
                },
            },
        )

        assert gaps.main() == 0

        report = capsys.readouterr().out
        assert "### Missing Lines Detail" in report
        assert "**`door.py`**" not in report

    def test_empty_coverage_json_renders_a_zeroed_report(self, workspace, monkeypatch, capsys):
        """Every total is read with a default, so an empty doc must not crash."""
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])
        write_coverage(workspace, {})

        assert gaps.main() == 0

        report = capsys.readouterr().out
        assert "| Line Coverage | 0.00% |" in report
        assert "| Lines Covered | 0 / 0 |" in report

    def test_pragma_section_is_rendered_from_the_source_tree(self, workspace, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])
        write_coverage(workspace, FULL_COVERAGE)
        source_dir = workspace / "src" / "powerpetdoor"
        source_dir.mkdir(parents=True)
        (source_dir / "mod.py").write_text(
            "a = 1  # pragma: no cover (unreachable)\n"
            "b = 2  # pragma: no cover\n"
            "c = {'x': 1} or 0  # pragma: no branch\n"
        )

        assert gaps.main() == 0

        report = capsys.readouterr().out
        assert "### Pragma Exclusions" in report
        assert "**3 lines** across **1 files** in **2 annotations**" in report
        assert "#### `mod.py` (3 lines)" in report
        assert "| 1-2 | no cover | unreachable | `a = 1` |" in report
        assert "| 3 | no branch | *no reason given* | `c = {'x': 1} or 0` |" in report

    @pytest.mark.parametrize(
        ("covered", "missing", "status"),
        [
            (994, 6, ":yellow_circle:"),
            (995, 5, ":green_circle:"),
        ],
        ids=["99.4-just-below", "99.5-exactly-at"],
    )
    def test_category_status_threshold(
        self, workspace, monkeypatch, capsys, covered, missing, status
    ):
        """The green/yellow boundary is a real constant, so pin it (R5-T3).

        The fixtures only ever exercised 80% and 100%, so any threshold in
        (80, 100] rendered identically and moving it survived the suite -
        in a file that is 100%-gated precisely because it can silently
        misreport.
        """
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])
        total = covered + missing
        percent = covered / total * 100
        write_coverage(
            workspace,
            {
                "totals": {
                    "percent_covered": percent,
                    "covered_lines": covered,
                    "missing_lines": missing,
                    "covered_branches": 0,
                    "missing_branches": 0,
                },
                "files": {
                    "src/powerpetdoor/client.py": {
                        "summary": {
                            "percent_covered": percent,
                            "num_statements": total,
                            "num_branches": 0,
                            "covered_lines": covered,
                            "missing_lines": missing,
                        },
                        "missing_lines": [1],
                        "missing_branches": [],
                    },
                },
            },
        )

        assert gaps.main() == 0

        report = capsys.readouterr().out
        assert f"| Core Library | 1 (1 with gaps) | {percent:.1f}% | {status} |" in report

    def test_pipes_in_code_are_escaped_for_the_markdown_table(self, workspace, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py", "--stdout"])
        write_coverage(workspace, FULL_COVERAGE)
        source_dir = workspace / "src" / "powerpetdoor"
        source_dir.mkdir(parents=True)
        (source_dir / "mod.py").write_text("x = a | b  # pragma: no cover\n")

        assert gaps.main() == 0

        assert r"`x = a \| b`" in capsys.readouterr().out


class TestAgainstTheRealRepository:
    """The script must still describe this repository correctly."""

    def test_real_pragma_scan_matches_the_source(self):
        source_dir = REPO_ROOT / "src" / "powerpetdoor"
        found = gaps._collect_pragma_exclusions(source_dir)

        counted = sum(
            1
            for path in source_dir.rglob("*.py")
            if path.name not in ("__init__.py", "__main__.py")
            for line in path.read_text().splitlines()
            if "pragma:" in line
        )
        total = sum(e["end_line"] - e["line"] + 1 for entries in found.values() for e in entries)
        assert total == counted
        assert counted > 0

    def test_automatic_exclusions_are_rendered_from_the_live_config(self):
        """The disclosure list is derived, not hand-maintained.

        The hard-coded list carried six bullets while `pyproject.toml`
        configured seven `exclude_lines` patterns - so the artifact that
        exists to disclose the project's coverage exclusions omitted the
        one removing a whole production method from the gate (round-6
        test-fanatic M2).
        """
        omit, exclude = gaps.coverage_config()
        bullets = gaps.render_automatic_exclusions()

        assert omit, "coverage.run.omit went missing from pyproject.toml"
        assert exclude, "coverage.report.exclude_lines went missing from pyproject.toml"
        # Exactly one bullet per configured pattern, in configuration order.
        assert len(bullets) == len(omit) + len(exclude)
        for pattern, bullet in zip(omit + exclude, bullets, strict=True):
            assert bullet.startswith(f"- `{pattern}`")

    def test_every_configured_exclusion_reaches_the_rendered_report(self, workspace, monkeypatch):
        """The rendered document, not just the helper, lists every pattern."""
        write_coverage(workspace, FULL_COVERAGE)
        monkeypatch.setattr(gaps, "PYPROJECT", REPO_ROOT / "pyproject.toml")
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py"])

        gaps.main()
        rendered = (workspace / "tests" / "TESTING_GAPS.md").read_text()

        omit, exclude = gaps.coverage_config()
        for pattern in omit + exclude:
            assert f"- `{pattern}`" in rendered

    def test_coverage_config_is_empty_without_a_pyproject(self, workspace, monkeypatch):
        """Rendering must not explode outside a checkout."""
        monkeypatch.setattr(gaps, "PYPROJECT", workspace / "pyproject.toml")

        assert gaps.coverage_config() == ([], [])
        assert gaps.render_automatic_exclusions() == []


GATED_SOURCE_DIRS = (REPO_ROOT / "src" / "powerpetdoor", REPO_ROOT / "scripts")


class TestTheCoverageConfigDoesNotExcludeProse:
    """Coverage's own instrumentation, checked against the source it instruments.

    Every `exclude_lines` entry is a `re.search` against the whole source
    line, so a bare phrase matches docstrings, f-strings and dict keys as
    readily as the construct it names. That has silently removed
    statements from this project's 100% gate in three consecutive rounds:
    the bare `...` (round 6), a replacement comment that re-excluded the
    line it had just restored (round 7), and six bare phrases matching
    `generate_gaps_report.py`'s own `_EXCLUSION_NOTES` table (round 8).

    The round-7 mitigation was a comment asking humans not to write the
    phrase in prose, and it was already violated in the file whose job is
    to describe the project's exclusions. This is the executable version.
    """

    def test_no_configured_pattern_matches_prose_in_a_gated_file(self):
        _, patterns = gaps.coverage_config()
        assert patterns, "coverage.report.exclude_lines went missing from pyproject.toml"

        found = gaps.find_prose_exclusions(GATED_SOURCE_DIRS, patterns)

        assert found == [], (
            "an exclude_lines pattern matched a string literal on a line carrying a "
            "statement, silently removing it from the 100% gate: "
            + "; ".join(f"{e['file']}:{e['line']} via {e['pattern']!r}" for e in found)
        )

    def test_the_sweep_catches_the_bare_phrases_round_8_replaced(self):
        """Falsifiability: the pre-round-8 config over this repository.

        Every one of the six bullets round 8 anchored is checked against
        the exact file that motivated it - `_EXCLUSION_NOTES`, the table
        whose entire job is to *disclose* the exclusions, plus the two
        `lines.append(...)` statements that render the pragma section.
        """
        bare = [
            "pragma: no cover",
            "def __repr__",
            "raise NotImplementedError",
            "if TYPE_CHECKING:",
            "if __name__ == .__main__.:",
            "@overload",
        ]

        found = gaps.find_prose_exclusions((REPO_ROOT / "scripts",), bare)

        assert {entry["pattern"] for entry in found} == set(bare)
        assert {Path(entry["file"]).name for entry in found} == {"generate_gaps_report.py"}

    def test_the_sweep_catches_the_bare_ellipsis_round_6_replaced(self):
        """...and the first instance of the class, in the shipped library."""
        found = gaps.find_prose_exclusions((REPO_ROOT / "src" / "powerpetdoor",), ["\\.\\.\\."])

        assert found, "the bare `...` pattern used to remove 34 statements from the gate"
        assert {Path(entry["file"]).suffix for entry in found} == {".py"}

    def test_a_phrase_in_a_docstring_is_not_reported(self, tmp_path):
        """Only statements matter: coverage counts none inside a docstring."""
        module = tmp_path / "m.py"
        module.write_text('"""Mentions pragma: no cover in prose."""\n\nX = 1\n')

        assert gaps.find_prose_exclusions((tmp_path,), ["pragma: no cover"]) == []

    def test_a_phrase_in_a_statement_is_reported(self, tmp_path):
        """The hole a matched pair proved: new dead code passing at 100.00%."""
        module = tmp_path / "m.py"
        module.write_text('def unused():\n    return "mentions pragma: no cover in a string"\n')

        found = gaps.find_prose_exclusions((tmp_path,), ["pragma: no cover"])

        assert [(entry["line"], entry["pattern"]) for entry in found] == [(2, "pragma: no cover")]

    def test_a_real_pragma_comment_is_not_reported(self, tmp_path):
        """The anchored pattern's legitimate use must stay legitimate."""
        _, patterns = gaps.coverage_config()
        pragma_pattern = next(p for p in patterns if "pragma" in p)
        module = tmp_path / "m.py"
        module.write_text("X = 1  # pragma: no cover (deliberate)\n")

        assert gaps.find_prose_exclusions((tmp_path,), [pragma_pattern]) == []

    def test_an_unparseable_file_contributes_nothing(self, tmp_path):
        """Same contract as the pragma scanner's untokenizable case."""
        (tmp_path / "broken.py").write_text("def (:\n")

        assert gaps.find_prose_exclusions((tmp_path,), ["pragma: no cover"]) == []
        assert gaps._string_spans("def (:\n") == {}
        assert gaps._statement_spans("def (:\n") == []
        assert gaps._excludes_a_statement([], 1) is False

    def test_an_undecodable_file_is_skipped(self, tmp_path):
        (tmp_path / "binary.py").write_bytes(b"\xff\xfe# pragma: no cover\n")

        assert gaps.find_prose_exclusions((tmp_path,), ["pragma: no cover"]) == []

    def test_init_and_main_files_are_skipped_like_the_omit_list(self, tmp_path):
        (tmp_path / "__init__.py").write_text('X = "pragma: no cover"\n')
        (tmp_path / "__main__.py").write_text('X = "pragma: no cover"\n')

        assert gaps.find_prose_exclusions((tmp_path,), ["pragma: no cover"]) == []

    def test_the_report_discloses_the_real_perimeter(self, workspace, monkeypatch):
        """`TESTING_GAPS.md` reported the intended perimeter, not the real one."""
        write_coverage(workspace, FULL_COVERAGE)
        monkeypatch.setattr(gaps, "PYPROJECT", REPO_ROOT / "pyproject.toml")
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py"])

        gaps.main()
        rendered = (workspace / "tests" / "TESTING_GAPS.md").read_text()

        assert "### Prose-Triggered Exclusions" in rendered
        assert "None. Every `exclude_lines` pattern above matches only" in rendered

    def test_the_report_names_every_prose_exclusion_it_finds(self, workspace, monkeypatch):
        """...and it renders them, pipes escaped, when there are any.

        A regex alternation is the one thing that would break the markdown
        table it is rendered into.
        """
        write_coverage(workspace, FULL_COVERAGE)
        source_dir = workspace / "src" / "powerpetdoor"
        source_dir.mkdir(parents=True)
        (source_dir / "m.py").write_text('X = "mentions ham|eggs here"\n')
        monkeypatch.setattr(gaps, "PYPROJECT", REPO_ROOT / "pyproject.toml")
        monkeypatch.setattr(gaps, "coverage_config", lambda: ([], ["ham|eggs"]))
        monkeypatch.setattr(sys, "argv", ["generate_gaps_report.py"])

        gaps.main()
        rendered = (workspace / "tests" / "TESTING_GAPS.md").read_text()

        assert "**1 statement(s)** are excluded because an `exclude_lines` pattern" in rendered
        assert (
            '| `src/powerpetdoor/m.py` | 1 | `ham\\|eggs` | `X = "mentions ham\\|eggs here"` |'
        ) in rendered
