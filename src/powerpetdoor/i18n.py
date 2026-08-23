"""Internationalization for user-facing text.

Translation happens through :func:`t`, which takes a stable dotted key and
the English source text as its default. The default is what ships, so a key
with no entry in any locale file still renders exactly the English it
replaced - that is what lets this be introduced across an existing tree
without changing a single byte of observable output.

Three properties this module owes the rest of the project:

* **It never raises.** A missing locale directory, a corrupt JSON file, a
  translation carrying a stray brace - each falls back to the English
  default rather than turning a cosmetic problem into a crash. The same
  rule the wire parsers follow, for the same reason: this sits underneath
  error reporting, so it must not be able to *become* the error.
* **It does not format unless asked.** Most log call sites in this project
  use lazy ``%``-style formatting, and the logging module applies that
  itself, later, with the record's args. :func:`t` therefore returns the
  format string untouched when no keyword arguments are passed, so
  ``_LOGGER.warning(t(key, "saw %s"), value)`` keeps working unchanged.
* **English is the default locale.** Nothing changes for a caller who never
  sets one.

Protocol text is deliberately *not* translatable. Everything in
``const.py`` goes on the wire to a device whose firmware cannot be changed,
so it is not user-facing text at all - see the "Never change the device
protocol" rule in the project guidance.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

#: Where the shipped locale files live. Packaged via `package-data` in
#: pyproject.toml, so this resolves inside a wheel as well as in a checkout.
LOCALES_DIR = Path(__file__).parent / "locales"

#: The locale whose text is embedded in the source as `t()` defaults.
DEFAULT_LOCALE = "en_us"

#: Consulted at import time so a CLI user can pick a language without the
#: program offering a flag for it.
LOCALE_ENV_VAR = "POWERPETDOOR_LOCALE"

#: Keys beginning with this are file metadata, not translations: who
#: translated it, when, and what the language calls itself. They are the
#: JSON equivalent of a gettext `.po` header, and they are the reason
#: metadata is allowed to hold values that are not strings (a translator
#: list) where a translation is not.
METADATA_PREFIX = "_"

#: Metadata key holding the language's own name for itself.
LANGUAGE_NAME_KEY = "_language"

#: Metadata key holding attribution: a string, or a list of them, each
#: conventionally `Name <email>`.
TRANSLATORS_KEY = "_translators"

#: Metadata key holding the date the translation was last revised, ISO 8601.
UPDATED_KEY = "_updated"

#: The extracted English catalogue that scripts/check_translations.py writes
#: for translators. It lives beside the locale files but is not one - listing
#: it would advertise a language called "messages".
CATALOG_STEM = "messages"

_translations: dict[str, dict[str, str]] = {}
_metadata: dict[str, dict[str, object]] = {}
_current_locale: str = DEFAULT_LOCALE

#: Every (locale, key) that fell back to its English default. Read by
#: scripts/check_translations.py to report what a locale is missing without
#: having to re-derive the call sites.
_missing_keys: set[tuple[str, str]] = set()


def normalize_locale(locale: str) -> str:
    """Fold a locale name to the spelling used for file names.

    Accepts what the environment actually hands over - ``en_US.UTF-8``,
    ``en-US``, ``EN_us`` - and answers ``en_us``.
    """
    name = locale.strip().split(".")[0].split("@")[0]
    return name.replace("-", "_").lower()


def get_available_locales() -> list[str]:
    """Locale codes with a shipped translation file, always including English."""
    found = {DEFAULT_LOCALE}
    try:
        for path in LOCALES_DIR.glob("*.json"):
            if path.stem != CATALOG_STEM:
                found.add(path.stem)
    except OSError as err:
        _LOGGER.debug("Could not list locales in %s: %s", LOCALES_DIR, err)
    return sorted(found)


def load_locale(locale: str) -> dict[str, str]:
    """Load and cache one locale's mapping, or an empty one if unusable."""
    normalized = normalize_locale(locale)
    cached = _translations.get(normalized)
    if cached is not None:
        return cached

    path = LOCALES_DIR / f"{normalized}.json"
    table: dict[str, str] = {}
    meta: dict[str, object] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if normalized != DEFAULT_LOCALE:
            _LOGGER.warning("No translation file for locale %r; using English", normalized)
    except (OSError, ValueError) as err:
        # A corrupt or unreadable locale file must not take the program with
        # it: every `t()` call would be raising from underneath whatever the
        # caller was actually trying to report.
        _LOGGER.warning("Ignoring unusable translation file %s: %s", path, err)
    else:
        if isinstance(raw, dict):
            meta = {k: v for k, v in raw.items() if k.startswith(METADATA_PREFIX)}
            entries = {k: v for k, v in raw.items() if not k.startswith(METADATA_PREFIX)}
            table = {key: value for key, value in entries.items() if isinstance(value, str)}
            dropped = len(entries) - len(table)
            if dropped:
                _LOGGER.warning("Ignored %d non-string entr(y/ies) in %s", dropped, path)
        else:
            _LOGGER.warning("Translation file %s is not a JSON object; ignoring", path)

    _translations[normalized] = table
    _metadata[normalized] = meta
    return table


def set_locale(locale: str) -> str:
    """Select the locale subsequent :func:`t` calls translate into.

    Returns the normalized name actually in effect. An unknown locale is
    accepted and simply renders English, so a typo degrades rather than
    fails.
    """
    global _current_locale
    _current_locale = normalize_locale(locale)
    load_locale(_current_locale)
    return _current_locale


def get_locale() -> str:
    """The locale currently in effect."""
    return _current_locale


def get_locale_metadata(locale: str) -> dict[str, object]:
    """The locale file's header: language name, translators, revision date."""
    normalized = normalize_locale(locale)
    load_locale(normalized)
    return dict(_metadata.get(normalized, {}))


def get_locale_name(locale: str) -> str:
    """Human-readable name for a locale, or its code upper-cased."""
    name = get_locale_metadata(locale).get(LANGUAGE_NAME_KEY)
    return name if isinstance(name, str) else normalize_locale(locale).upper()


def get_translators(locale: str) -> list[str]:
    """Who translated this locale. Accepts a single string or a list."""
    credited = get_locale_metadata(locale).get(TRANSLATORS_KEY)
    if isinstance(credited, str):
        return [credited]
    if isinstance(credited, list):
        return [entry for entry in credited if isinstance(entry, str)]
    return []


def t(key: str, default: str, /, **kwargs: object) -> str:
    """Translate ``key``, falling back to ``default``.

    ``default`` is required rather than optional: a key with no default is a
    key that renders as a dotted identifier the moment a locale file is
    incomplete, which is worse than the English it was meant to replace.

    Keyword arguments are applied with :meth:`str.format`. With none given
    the string is returned untouched, so ``%``-style logging placeholders
    survive for the logging module to apply later.
    """
    table = load_locale(_current_locale)
    text = table.get(key)
    if text is None:
        if _current_locale != DEFAULT_LOCALE:
            _missing_keys.add((_current_locale, key))
        text = default

    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError) as err:
        # A translation whose placeholders do not match the call site is a
        # bug in the *translation*; render the English rather than raising
        # from inside whatever this text was describing.
        _LOGGER.warning("Translation %r for %r does not fit its arguments: %s", text, key, err)
        try:
            return default.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return default


def missing_keys() -> set[tuple[str, str]]:
    """(locale, key) pairs that have fallen back to English so far."""
    return set(_missing_keys)


def reset_for_testing() -> None:
    """Drop cached locales, the selected locale and the missing-key record."""
    global _current_locale
    _translations.clear()
    _metadata.clear()
    _missing_keys.clear()
    _current_locale = DEFAULT_LOCALE


def _locale_from_environment() -> str:
    """POWERPETDOOR_LOCALE, else the standard locale vars, else English."""
    for var in (LOCALE_ENV_VAR, "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value and normalize_locale(value) not in ("c", "posix", ""):
            return normalize_locale(value)
    return DEFAULT_LOCALE


_current_locale = _locale_from_environment()
