#!/usr/bin/env bash
# Development environment setup for pypowerpetdoor.
#
# Idempotent: safe to re-run after pulling a change to
# .pre-commit-config.yaml or pyproject.toml.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "Step 1: Installing the project and its dev extras..."
echo "----------------------------------------------------"
if command -v uv &> /dev/null; then
    uv sync --all-extras
    RUN="uv run"
else
    echo "uv not found (https://docs.astral.sh/uv/); falling back to pip."
    pip install -e ".[dev,simulator,interactive]"
    RUN=""
fi

echo
echo "Step 2: Installing git hooks..."
echo "-------------------------------"
# Hooks are the only part of this that a fresh clone does NOT get for free:
# .pre-commit-config.yaml is checked in, but nothing runs it until these two
# commands have been run once in the working copy.
if $RUN pre-commit --version &> /dev/null; then
    $RUN pre-commit install
    $RUN pre-commit install --hook-type pre-push
    echo "Installed: pre-commit (lint, format, types, fast tests)"
    echo "           pre-push   (full suite + 100% coverage, dependency freshness)"
else
    echo "Warning: pre-commit not available."
    echo "  With uv:  uv sync --all-extras   (it is in the dev extra)"
    echo "  With pip: pip install pre-commit"
fi

echo
echo "Step 3: Checking dependency freshness..."
echo "----------------------------------------"
$RUN python scripts/check_dependencies.py || true

echo
echo "Done. Useful commands:"
echo "  uv run pytest                          Run the test suite"
echo "  uv run pytest --cov                    ...with the 100% coverage gate"
echo "  uv run ruff check src tests            Lint"
echo "  uv run mypy src                        Type-check"
echo "  uv run ppd-simulator                   Run the door simulator"
echo "  pre-commit run --all-files             Run every hook over the tree"
echo "  python scripts/check_dependencies.py --fix   Apply available updates"
echo
echo "Optional: 'direnv allow' activates .venv automatically on cd (.envrc)."
