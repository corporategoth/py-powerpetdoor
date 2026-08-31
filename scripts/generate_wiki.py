#!/usr/bin/env python3
# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Assemble the Gitea wiki from the repo's documentation.

The wiki is a *rendering*, never a source. Everything here comes from
files that already exist and are already checked:

- ``docs/*.md`` and ``CHANGELOG.md`` - written and reviewed in the repo.
- ``docs/api/_build/markdown/`` - Sphinx autodoc output, from the same
  docstrings that produce the HTML reference.

What this script actually does that neither of those can is the wiki's
own shape: a flat page namespace with no ``.md`` suffixes, an index, and
inter-page links rewritten to match. A wiki page named ``Protocol``
cannot be reached by a link to ``protocol.md``.

Publishing is the release job's business (``.github/workflows``), not
this script's - it writes a directory and stops. Run it by hand to see
exactly what would be published:

    python scripts/generate_wiki.py --out /tmp/wiki

Gitea fires ``sync-wiki.yml`` when the wiki is pushed, which carries it
to the GitHub mirror. That is the only path: the wiki is written on
Gitea, never on GitHub.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Importable both as `python scripts/generate_wiki.py` (where sys.path[0]
# is `scripts/`) and as `scripts.generate_wiki` (where it is the repo
# root). `generate_schemas.py` reaches for `src/` the same way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_asyncapi import load_spec  # noqa: E402
from render_asyncapi import pages as asyncapi_pages  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
API_MARKDOWN = REPO_ROOT / "docs" / "api" / "_build" / "markdown"

#: Source file -> wiki page name. The order is the order of the index.
#: A doc absent from here is absent from the wiki, deliberately: this is
#: the published set, and `test_wiki.py` pins that nothing in `docs/` is
#: forgotten rather than letting the list rot.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("docs/operation.md", "Operation", "What the door actually does, and why"),
    ("docs/protocol.md", "Protocol", "The wire protocol"),
    ("docs/door.md", "Door-Interface", "PowerPetDoor: the high-level interface"),
    ("docs/client.md", "Client-Interface", "PowerPetDoorClient: the protocol client"),
    ("docs/simulator.md", "Simulator", "The door simulator and its CLI"),
    ("docs/scripting.md", "Scripting", "The simulator's YAML scripting language"),
    ("docs/development.md", "Development", "How the codebase fits together"),
    ("docs/translations.md", "Translations", "The i18n catalogue"),
    ("CHANGELOG.md", "Changelog", "Release history"),
)

#: Pages rendered from `schemas/asyncapi.json` rather than from a file
#: in `docs/`. They carry the per-command reference - every field, every
#: constraint, every worked example - which `docs/protocol.md` explains
#: but deliberately does not tabulate command by command.

#: Generated API reference -> wiki page name.
API_PAGES: tuple[tuple[str, str, str], ...] = (
    ("door.md", "API-PowerPetDoor", "Generated API reference for PowerPetDoor"),
    ("client.md", "API-PowerPetDoorClient", "Generated API reference for PowerPetDoorClient"),
)


#: Every source basename -> the wiki page it became, for link rewriting.
def _link_map() -> dict[str, str]:
    mapping = {Path(src).name: page for src, page, _ in PAGES}
    # The Sphinx pages cross-reference each other by file name too.
    mapping.update({src: page for src, page, _ in API_PAGES})
    return mapping


def rewrite_links(text: str, mapping: dict[str, str]) -> str:
    """Point `foo.md` links at the wiki page `foo.md` became.

    Anchors survive: `protocol.md#the-door-clock` becomes
    `Protocol#the-door-clock`, because a wiki page keeps the heading
    anchors the markdown generated.
    """

    def replace(match: re.Match[str]) -> str:
        target, anchor = match["file"], match["anchor"] or ""
        page = mapping.get(target)
        if page is None:
            return match[0]
        return f"]({page}{anchor})"

    return re.sub(r"\]\((?P<file>[A-Za-z0-9_./-]+\.md)(?P<anchor>#[^)]*)?\)", replace, text)


def strip_repo_only_links(text: str) -> str:
    """Turn links into the source tree into plain text.

    A wiki page has no `src/` beside it, so `[schemas/](schemas/)` would
    be a dead link. The words are still useful; the link is not.
    """
    return re.sub(r"\[([^\]]+)\]\((?:\.\./)*(?:src|scripts|schemas|tests)/[^)]*\)", r"`\1`", text)


def build_api_markdown() -> bool:
    """Refresh the Sphinx markdown the wiki publishes.

    Returns False when Sphinx is unavailable, so a caller without the
    `docs` extra still gets the hand-written pages rather than nothing.
    """
    result = subprocess.run(
        [
            "uv",
            "run",
            "--extra",
            "docs",
            "sphinx-build",
            "-q",
            "-b",
            "markdown",
            str(REPO_ROOT / "docs" / "api"),
            str(API_MARKDOWN),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        print(f"  could not build the API reference: {result.stderr.strip()[:200]}")
        return False
    return True


def home_page(entries: list[tuple[str, str]]) -> str:
    """The index. Gitea shows `Home` first, so it is the landing page."""
    lines = [
        "# Power Pet Door",
        "",
        "A Python library for High Tech Pet's WiFi-enabled Power Pet Door,",
        "and a full simulator of one.",
        "",
        "> **This wiki is generated.** It is rendered from the documentation in",
        "> the repository on every release and pushed from Gitea. Edits made",
        "> here are overwritten - change `docs/` in the repository instead.",
        "",
        "## Pages",
        "",
    ]
    lines += [f"- **[{page}]({page})** — {blurb}" for page, blurb in entries]
    lines += [
        "",
        "## Machine-readable specifications",
        "",
        "Published alongside the source, generated from the same tables the",
        "runtime reads:",
        "",
        "- `schemas/script.schema.json` — JSON Schema for the scripting language",
        "- `schemas/state.schema.json` — JSON Schema for state documents",
        "- `schemas/asyncapi.json` — AsyncAPI 3.0 for the wire protocol,",
        "  rendered as **[Protocol-Reference](Protocol-Reference)**",
        "",
    ]
    return "\n".join(lines)


def generate(out: Path) -> list[str]:
    """Write the wiki into ``out``, returning the page names written."""
    out.mkdir(parents=True, exist_ok=True)
    mapping = _link_map()
    written: list[str] = []
    entries: list[tuple[str, str]] = []

    for source, page, blurb in PAGES:
        path = REPO_ROOT / source
        if not path.exists():
            print(f"  MISSING: {source}")
            continue
        text = strip_repo_only_links(rewrite_links(path.read_text(encoding="utf-8"), mapping))
        (out / f"{page}.md").write_text(text, encoding="utf-8")
        written.append(page)
        entries.append((page, blurb))

    # The protocol reference, rendered from the AsyncAPI document. Its
    # own links already name wiki pages, so it skips the rewriting the
    # `docs/` sources need.
    for page, blurb, body in asyncapi_pages(load_spec()):
        (out / f"{page}.md").write_text(body, encoding="utf-8")
        written.append(page)
        entries.append((page, blurb))

    if build_api_markdown():
        for source, page, blurb in API_PAGES:
            path = API_MARKDOWN / source
            if not path.exists():
                print(f"  MISSING: {path}")
                continue
            text = rewrite_links(path.read_text(encoding="utf-8"), mapping)
            (out / f"{page}.md").write_text(text, encoding="utf-8")
            written.append(page)
            entries.append((page, blurb))

    (out / "Home.md").write_text(home_page(entries), encoding="utf-8")
    written.append("Home")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "build" / "wiki",
        help="Directory to write the wiki into (created; existing content replaced).",
    )
    args = parser.parse_args()

    if args.out.exists():
        shutil.rmtree(args.out)
    written = generate(args.out)
    print(f"wrote {len(written)} page(s) to {args.out}")
    for page in written:
        print(f"  {page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
