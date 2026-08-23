#!/usr/bin/env python3
"""Report dependencies that have drifted behind, and known CVEs.

Three separate questions, deliberately not conflated:

1. Is `uv.lock` consistent with `pyproject.toml`? A mismatch means
   `uv sync --locked` would refuse, so this is always an error.
2. Would a fresh resolve pick anything newer? `uv sync` never upgrades what
   is already pinned, so without this the lock rots silently - and with it
   the DST database in `tzdata`, the library's only runtime dependency and
   the thing the whole `tz_utils`/schedule feature reads its rules from.
3. Does anything in the resolved set have a published advisory?

Only 1 and 3 fail by default. Staleness is reported but not fatal unless
`--strict`, because a transitive release landing on a Tuesday is not a
reason for every push that week to go red.

Usage:
    python scripts/check_dependencies.py [--strict] [--fix]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys

#: Advisories with no fixed version yet, or that provably cannot apply to
#: this project. Each entry needs a reason; an empty dict is the goal.
IGNORED_VULNERABILITIES: dict[str, str] = {}


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def check_lock_matches_manifest() -> bool:
    """`uv.lock` resolves what `pyproject.toml` currently declares."""
    result = run(["uv", "lock", "--check"])
    if result.returncode == 0:
        print("  lock is consistent with pyproject.toml")
        return True
    print("  lock is STALE relative to pyproject.toml - run `uv lock`")
    print(f"    {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ''}")
    return False


#: uv's explicit "nothing to do" line. Detection keys off *this* rather
#: than off the move verbs, so that a wording change in uv surfaces as a
#: false alarm rather than as a silent all-clear.
NOTHING_TO_DO = "No lockfile changes detected"

#: What uv 0.12 actually emits per moved package: `Update tzdata v2024.1 ->
#: v2026.3`. Captured from a real stale lock, because the plausible guess
#: ("Updating", "Added", "Removed") matches none of it and made this
#: function report every stale lock as current.
_MOVE = re.compile(r"^(Update|Add|Remove|Downgrade|Bump)\b")

_UNRECOGNISED = "uv reported changes in a format this script did not recognise"


def parse_upgrade_moves(text: str) -> list[str]:
    """Lines describing what a fresh resolve would move."""
    if NOTHING_TO_DO in text:
        return []
    moves = [line.strip() for line in text.splitlines() if _MOVE.match(line.strip())]
    return moves or [_UNRECOGNISED]


def check_upgrades_available(fix: bool) -> list[str]:
    """What a fresh resolve would move, without writing the lockfile."""
    result = run(["uv", "lock", "--upgrade", "--dry-run"])
    moves = parse_upgrade_moves(f"{result.stdout}\n{result.stderr}")
    if not moves:
        print("  every dependency is at its newest resolvable version")
        return []

    print(f"  {len(moves)} dependenc{'y' if len(moves) == 1 else 'ies'} could move:")
    for move in moves:
        print(f"    {move}")
    if fix:
        print("  applying with `uv lock --upgrade`...")
        upgrade = run(["uv", "lock", "--upgrade"])
        if upgrade.returncode != 0:
            print(f"    failed: {upgrade.stderr.strip()}")
        else:
            print("    done - now re-run the full suite before committing the lock")
    return moves


def check_vulnerabilities() -> list[dict] | None:
    """Published advisories against the resolved set, via pip-audit.

    Returns None when pip-audit could not be run at all, which is reported
    but not treated as a clean bill of health.
    """
    if shutil.which("uv") is None:
        print("  skipped: uv not on PATH")
        return None

    result = run(["uv", "export", "--format", "requirements-txt", "--no-hashes", "--all-extras"])
    if result.returncode != 0:
        print(f"  skipped: could not export requirements ({result.stderr.strip()})")
        return None

    audit = subprocess.run(
        ["uvx", "pip-audit", "--format", "json", "--requirement", "/dev/stdin"],
        input=result.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if audit.returncode not in (0, 1):
        print(f"  skipped: pip-audit unavailable ({audit.stderr.strip().splitlines()[-1:]})")
        return None

    try:
        report = json.loads(audit.stdout)
    except json.JSONDecodeError:
        print("  skipped: could not parse pip-audit output")
        return None

    found: list[dict] = []
    for dep in report.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            vuln_id = vuln.get("id", "?")
            if vuln_id in IGNORED_VULNERABILITIES:
                print(f"  ignoring {vuln_id}: {IGNORED_VULNERABILITIES[vuln_id]}")
                continue
            found.append({"name": dep.get("name"), "version": dep.get("version"), **vuln})

    if not found:
        print("  no known advisories against the resolved set")
    else:
        for vuln in found:
            fix = ", ".join(vuln.get("fix_versions") or []) or "no fix released"
            print(f"  {vuln['name']} {vuln['version']}: {vuln['id']} (fixed in: {fix})")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail when a newer version is merely available",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="run `uv lock --upgrade` when upgrades are available",
    )
    args = parser.parse_args()

    print("Lockfile consistency:")
    consistent = check_lock_matches_manifest()

    print("\nAvailable upgrades:")
    moves = check_upgrades_available(args.fix)

    print("\nSecurity advisories:")
    vulns = check_vulnerabilities()

    print()
    if not consistent:
        print("FAIL: uv.lock does not match pyproject.toml")
        return 1
    if vulns:
        print(f"FAIL: {len(vulns)} known advisor{'y' if len(vulns) == 1 else 'ies'}")
        return 1
    if moves and args.strict:
        print(f"FAIL (--strict): {len(moves)} dependency update(s) available")
        return 1
    if moves:
        print(f"OK, with {len(moves)} update(s) available - run with --fix to apply")
        return 0
    print("OK: dependencies are current and free of known advisories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
