#!/usr/bin/env python3
"""Audit translation coverage for powerpetdoor.

Ostinato splits this across check_translations.py, find_untranslated.py and
clean_unused_translations.py, all three of which regex over HTML, Jinja and
JavaScript. This tree is pure Python, so the same three questions are
answerable exactly from the AST instead of approximately from a regex, and
they live in one script rather than three near-copies:

* **Missing** - a key some locale has no entry for. Renders English.
* **Orphaned** - an entry in a locale file for a key no source calls any
  more. Dead weight that quietly rots, and the only one worth failing on by
  default, since it means a translation is being maintained for nothing.
* **Untranslated** - user-facing text that is not wrapped in `t()` at all.
  This is the one that actually measures i18n coverage.

The source is the authority for English: `t(key, default)` carries its own
English default, so no `en_us.json` ships and there is no second place for
English to drift out of step with the code.

Usage:
    python scripts/check_translations.py [--strict] [--untranslated]
                                         [--write-catalog] [--locale de_de]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import date
from fnmatch import fnmatch
from pathlib import Path

SRC = Path("src/powerpetdoor")
LOCALES = SRC / "locales"
CATALOG = LOCALES / "messages.json"

#: Keys beginning with this are the file's header - language name,
#: translators, revision date - not translations. Mirrors
#: powerpetdoor.i18n.METADATA_PREFIX.
METADATA_PREFIX = "_"
LANGUAGE_NAME_KEY = "_language"
TRANSLATORS_KEY = "_translators"
UPDATED_KEY = "_updated"

#: Call shapes whose text reaches a person. Kept in step with the wrapping
#: codemod: anything listed here and *not* wrapped is reported by
#: --untranslated.
LOG_LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}


def iter_python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name not in {"i18n.py", "const.py"})


def extract_entries() -> dict[str, tuple[str, list[str]]]:
    """Every `t("key", "English")` as key -> (English, ["file:line", ...]).

    Locations are what a translator needs and a bare key/text mapping cannot
    give them: "Closed" is a different word depending on whether it labels a
    door, a connection or a schedule window, and the only way to tell is to
    read the call site.
    """
    entries: dict[str, tuple[str, list[str]]] = {}
    for path in iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "t" or len(node.args) < 2:
                continue
            key, default = node.args[0], node.args[1]
            if isinstance(key, ast.Constant) and isinstance(default, ast.Constant):
                if isinstance(key.value, str) and isinstance(default.value, str):
                    text, seen = entries.get(key.value, (default.value, []))
                    seen.append(f"{path}:{node.lineno}")
                    entries[key.value] = (text, seen)
    return entries


def extract_catalog() -> dict[str, str]:
    """Every `t("key", "English")` in the tree, keyed by its key."""
    return {key: text for key, (text, _where) in extract_entries().items()}


def find_untranslated() -> list[tuple[Path, int, str]]:
    """User-facing text that never made it into a `t()` call."""
    found: list[tuple[Path, int, str]] = []
    for path in iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wrapped: set[int] = set()
        for node in ast.walk(tree):
            func = getattr(node, "func", None)
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if isinstance(node, ast.Call) and name == "t":
                for arg in node.args:
                    wrapped.add(id(arg))

        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "CommandResult" and len(node.args) >= 2:
                    targets = [node.args[1]]
                elif name in ("status_print", "print") and node.args:
                    targets = [node.args[0]]
                elif name in LOG_LEVELS and isinstance(func, ast.Attribute) and node.args:
                    targets = [node.args[0]]
            elif isinstance(node, ast.Raise):
                if isinstance(node.exc, ast.Call) and node.exc.args:
                    targets = [node.exc.args[0]]

            for target in targets:
                if id(target) in wrapped:
                    continue
                if isinstance(target, ast.Call):
                    inner = target.func
                    if (getattr(inner, "id", None) or getattr(inner, "attr", None)) == "t":
                        continue
                text = None
                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    text = target.value
                elif isinstance(target, ast.JoinedStr):
                    text = "".join(
                        v.value
                        for v in target.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    )
                if text and any(ch.isalpha() for ch in text):
                    found.append((path, target.lineno, text.strip()[:70]))
    return found


def find_duplicate_text(catalog: dict[str, str]) -> tuple[list[str], list[str]]:
    """Keys sharing identical English text.

    Two flavours, and only one is a defect:

    * A `_N` suffix exists *only* because two different strings slugged to
      the same key. When the English is identical as well, the suffix is a
      collision artifact - a translator would be asked to translate the same
      sentence twice, and the two copies would drift. That fails.
    * The same sentence in two different modules is legitimate: German may
      want different wording for the client's version and the door's, and
      collapsing them would remove the translator's ability to say so. That
      is reported, not failed.
    """
    by_text: dict[str, list[str]] = {}
    for key, text in catalog.items():
        by_text.setdefault(text, []).append(key)

    artifacts: list[str] = []
    contextual: list[str] = []
    for text, keys in sorted(by_text.items()):
        if len(keys) < 2:
            continue
        for key in sorted(keys):
            match = re.match(r"^(.*)_(\d+)$", key)
            if match and catalog.get(match.group(1)) == text:
                artifacts.append(f"{key} duplicates {match.group(1)}: {text[:50]!r}")
                break
        else:
            contextual.append(f"{', '.join(sorted(keys))}: {text[:50]!r}")
    return artifacts, contextual


def load_locale(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"  {path.name}: unreadable ({err})")
        return {}
    return raw if isinstance(raw, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict", action="store_true", help="also fail when a locale is missing keys"
    )
    parser.add_argument(
        "--untranslated", action="store_true", help="report user-facing text not wrapped in t()"
    )
    parser.add_argument(
        "--write-catalog",
        action="store_true",
        help=f"regenerate {CATALOG} as the translator reference",
    )
    parser.add_argument("--locale", help="check only this locale")
    parser.add_argument(
        "--locate",
        metavar="PATTERN",
        help="show source locations for keys matching PATTERN (substring or glob)",
    )
    parser.add_argument(
        "--locations", action="store_true", help="list every key with its source locations"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit --locate/--locations output as JSON on stdout",
    )
    parser.add_argument(
        "--init-locale",
        metavar="CODE",
        help="create locales/CODE.json prefilled with the English, ready to translate",
    )
    args = parser.parse_args()

    entries = extract_entries()
    catalog = {key: text for key, (text, _where) in entries.items()}
    print(
        f"Catalogue: {len(catalog)} translatable string(s) across {len(iter_python_files())} files"
    )

    if args.locate or args.locations:
        pattern = args.locate or "*"
        matched = {
            key: value
            for key, value in entries.items()
            if fnmatch(key, pattern) or (args.locate and args.locate in key)
        }
        if args.json:
            print(
                json.dumps(
                    {k: {"text": v[0], "at": v[1]} for k, v in sorted(matched.items())},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0 if matched else 1
        print(f"\n{len(matched)} key(s) matching {pattern!r}:")
        for key, (text, where) in sorted(matched.items()):
            print(f"\n  {key}")
            print(f"    text: {text!r}")
            for location in where:
                print(f"    at:   {location}")
        return 0 if matched else 1

    if args.init_locale:
        code = args.init_locale.lower().replace("-", "_")
        LOCALES.mkdir(parents=True, exist_ok=True)
        target = LOCALES / f"{code}.json"
        if target.exists():
            print(f"  {target} already exists; not overwriting")
            return 1
        seed: dict[str, object] = {
            LANGUAGE_NAME_KEY: code.upper(),
            TRANSLATORS_KEY: ["Your Name <you@example.com>"],
            UPDATED_KEY: date.today().isoformat(),
        }
        seed.update(dict(sorted(catalog.items())))
        target.write_text(json.dumps(seed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  wrote {target} with {len(catalog)} entries to translate")
        print(f"  now fill in {LANGUAGE_NAME_KEY!r} and {TRANSLATORS_KEY!r}, then translate")
        return 0

    if args.write_catalog:
        LOCALES.mkdir(parents=True, exist_ok=True)
        CATALOG.write_text(
            json.dumps(dict(sorted(catalog.items())), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  wrote {CATALOG}")

    failures = 0

    # The committed catalogue is the translator's starting point and the
    # thing whose diff makes a reworded string visible in review, so it must
    # not be allowed to drift. Locations deliberately are *not* stored: line
    # numbers move whenever anything above them does, so a committed copy
    # would be stale by the next unrelated edit. Ask the tooling instead:
    #     check_translations.py --locations [--json]
    if not args.write_catalog:
        try:
            committed = json.loads(CATALOG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            committed = None
        if committed != dict(sorted(catalog.items())):
            print(f"\n{CATALOG} is out of date - run --write-catalog")
            failures += 1

    artifacts, contextual = find_duplicate_text(catalog)
    print(
        f"\nDuplicate text: {len(artifacts)} collision artifact(s), {len(contextual)} cross-module"
    )
    for entry in artifacts:
        print(f"  COLLISION: {entry}")
    for entry in contextual:
        print(f"  (context)  {entry}")
    failures += len(artifacts)

    print("\nLocales:")
    locale_files = sorted(p for p in LOCALES.glob("*.json") if p != CATALOG)
    if args.locale:
        locale_files = [p for p in locale_files if p.stem == args.locale]
    if not locale_files:
        print("  none shipped yet - every string renders its English default")
    for path in locale_files:
        entries = load_locale(path)
        keys = {k for k in entries if not k.startswith(METADATA_PREFIX)}
        missing = sorted(set(catalog) - keys)
        orphaned = sorted(keys - set(catalog))
        pct = 100.0 * (len(catalog) - len(missing)) / len(catalog) if catalog else 100.0
        print(
            f"  {path.stem} ({entries.get(LANGUAGE_NAME_KEY) or path.stem.upper()}): "
            f"{pct:.1f}% translated, {len(missing)} missing, {len(orphaned)} orphaned"
        )
        credited = entries.get(TRANSLATORS_KEY)
        if isinstance(credited, str):
            credited = [credited]
        credited = [c for c in (credited or []) if isinstance(c, str)]
        print(f"    translators: {', '.join(credited) if credited else 'UNATTRIBUTED'}")
        print(f"    updated:     {entries.get(UPDATED_KEY) or 'unknown'}")
        if not credited:
            print(f"    (add {TRANSLATORS_KEY!r} so the work is credited and reachable)")
        for key in orphaned[:10]:
            print(f"    orphaned: {key}")
        if orphaned:
            failures += len(orphaned)
        if missing and args.strict:
            for key in missing[:10]:
                print(f"    missing:  {key}")
            failures += len(missing)

    if args.untranslated:
        stragglers = find_untranslated()
        print(f"\nUntranslated user-facing text: {len(stragglers)}")
        for path, lineno, text in stragglers[:40]:
            print(f"  {path}:{lineno}: {text}")
        if args.strict:
            failures += len(stragglers)

    print()
    if failures:
        print(f"FAIL: {failures} translation issue(s)")
        return 1
    print("OK: no orphaned translations, no duplicate keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
