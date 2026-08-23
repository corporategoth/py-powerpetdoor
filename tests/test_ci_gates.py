# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The CI gates this project relies on, pinned by *parsing* the workflow.

Every check that keeps this repo honest lives in
``.github/workflows/test.yml``, and a substring assertion over the file's
text is not a pin: commenting the step out, setting ``if: false`` or adding
``continue-on-error: true`` all leave the substring present and the suite
green. Three of those four edits pass a substring check.

So these load the workflow as YAML and assert on the *resolved* step: it
exists, it runs the command it is supposed to, and nothing has been
attached to it that would stop it failing the build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _job(workflow: dict, name: str) -> dict:
    assert name in workflow["jobs"], f"the {name!r} job is gone"
    job = workflow["jobs"][name]
    # `if: false` (or any condition) on the job disables every step in it.
    assert "if" not in job or name == "coverage-report", f"the {name!r} job is conditional"
    return job


def _step(job: dict, name: str) -> dict:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}; the gate is gone or renamed")


def _assert_step_can_fail_the_build(step: dict) -> None:
    """A step that cannot fail the build is not a gate."""
    assert step.get("continue-on-error") in (None, False), (
        f"{step['name']!r} is continue-on-error, so it can never fail the build"
    )
    assert "if" not in step, f"{step['name']!r} is conditional, so it may be skipped silently"


class TestTheCoverageGate:
    """``--fail-under=100`` is the gate every other test in this repo leans on.

    Lowering the threshold, commenting the step out, marking it
    ``continue-on-error`` or gating it behind ``if: false`` each leave the
    whole suite green.
    """

    def test_the_gate_step_runs_coverage_at_100(self, workflow):
        step = _step(_job(workflow, "coverage-report"), "Enforce 100% coverage")

        assert step["run"].split() == ["coverage", "report", "--fail-under=100"]

    def test_the_gate_step_can_actually_fail_the_build(self, workflow):
        _assert_step_can_fail_the_build(
            _step(_job(workflow, "coverage-report"), "Enforce 100% coverage")
        )

    def test_the_job_holding_the_gate_still_runs_when_tests_fail(self, workflow):
        """`if: always()` is deliberate: a failing matrix must still be gated."""
        job = workflow["jobs"]["coverage-report"]

        assert job["if"] == "${{ always() }}"
        assert job["needs"] == ["unit-tests"]

    def test_the_configured_threshold_is_also_100(self):
        """The command line wins, but a lowered config makes local runs lie."""
        import tomllib

        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())

        assert pyproject["tool"]["coverage"]["report"]["fail_under"] == 100
        assert pyproject["tool"]["coverage"]["run"]["source"] == ["src/powerpetdoor"]


class TestThePackagingGate:
    """The built artifacts, asserted rather than assumed.

    The sdist used to carry 9 test modules and none of the machinery that
    runs them, and the wheel shipped a fully annotated library with no
    PEP 561 marker. Both are invisible to every other job: they are
    properties of the *built* artifact.
    """

    @pytest.mark.parametrize(
        "name",
        ["The wheel carries the PEP 561 marker", "The sdist ships a suite that actually runs"],
    )
    def test_the_packaging_steps_can_fail_the_build(self, workflow, name):
        _assert_step_can_fail_the_build(_step(_job(workflow, "packaging"), name))

    def test_the_wheel_step_looks_for_py_typed(self, workflow):
        step = _step(_job(workflow, "packaging"), "The wheel carries the PEP 561 marker")

        assert "powerpetdoor/py.typed" in step["run"]

    def test_the_sdist_step_runs_the_shipped_suite(self, workflow):
        step = _step(_job(workflow, "packaging"), "The sdist ships a suite that actually runs")

        assert "tests/conftest.py" in step["run"]
        assert "pytest" in step["run"]

    def test_the_manifest_ships_the_whole_suite(self):
        """The graft is recursive, so a directory added later is included -
        which is exactly what the default sdist heuristic did not do."""
        manifest = [
            line.strip()
            for line in (REPO_ROOT / "MANIFEST.in").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        packages = {
            path.parent.relative_to(REPO_ROOT).as_posix()
            for path in (REPO_ROOT / "tests").rglob("__init__.py")
        }

        assert "graft tests" in manifest
        assert "graft docs" in manifest
        assert "include CHANGELOG.md" in manifest
        assert packages == {"tests", "tests/fuzz", "tests/simulator", "tests/simulator/scripts"}

    def test_the_dev_extra_the_shipped_addopts_needs_is_declared(self):
        """`addopts = "-n auto"` ships in `pyproject.toml`, so a third party
        with only `pytest` cannot start at all (`unrecognized arguments: -n`)."""
        import tomllib

        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        dev = pyproject["project"]["optional-dependencies"]["dev"]

        assert pyproject["tool"]["pytest"]["ini_options"]["addopts"] == "-n auto"
        assert any(entry.startswith("pytest-xdist") for entry in dev)
        assert any(entry.startswith("pytest-asyncio") for entry in dev)
        assert any(entry.startswith("hypothesis") for entry in dev)


class TestTheStaticAnalysisGates:
    """Lint, format and types are gates too, and gates get pinned."""

    @pytest.mark.parametrize(
        ("name", "command"),
        [
            ("Check linting", ["ruff", "check", "src", "tests"]),
            ("Check formatting", ["ruff", "format", "--check", "src", "tests"]),
            ("Check types", ["mypy", "src"]),
        ],
    )
    def test_the_lint_job_runs_each_check_and_can_fail(self, workflow, name, command):
        step = _step(_job(workflow, "lint"), name)

        _assert_step_can_fail_the_build(step)
        assert step["run"].split() == command
