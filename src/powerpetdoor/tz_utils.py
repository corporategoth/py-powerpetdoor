# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Timezone utilities for Power Pet Door.

Provides functions to convert IANA timezone names to POSIX TZ strings
using the tzdata package's TZif files via Python's importlib.resources API.

IMPORTANT: All file I/O is done during async_init_timezone_cache() which
uses asyncio.to_thread for non-blocking execution. After initialization,
all lookups are from in-memory caches with no blocking I/O.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading

from .i18n import t
from .sanitize import sanitize_field

_LOGGER = logging.getLogger(__name__)

# Module-level caches - populated by async_init_timezone_cache()
_iana_timezones: list[str] | None = None
_iana_to_posix: dict[str, str] = {}
_posix_to_iana: dict[str, str] = {}
_cache_initialized: bool = False
# Serializes cache initialization so concurrent initializers cannot run
# the (expensive) tzdata scan twice.
_cache_lock = threading.Lock()

# POSIX TZ abbreviations are either alphabetic (EST) or, in modern tzdata,
# angle-bracket-quoted alphanumeric/sign forms such as <+05> or <-03>.
_POSIX_ABBREV = r"[A-Za-z]+|<[A-Za-z0-9+\-]+>"
_POSIX_TZ_RE = re.compile(
    rf"^({_POSIX_ABBREV})"
    r"([+-]?\d+(?::\d+(?::\d+)?)?)"
    rf"(?:({_POSIX_ABBREV})"
    r"([+-]?\d+(?::\d+(?::\d+)?)?)?)?"
)


def _extract_posix_from_tzif(iana_timezone: str) -> str | None:
    """Extract the POSIX TZ string from a timezone's TZif file.

    This function does blocking I/O and should only be called from an executor.
    """
    try:
        import importlib.resources

        parts = iana_timezone.split("/")

        package = "tzdata.zoneinfo"
        if len(parts) > 1:
            package = f"tzdata.zoneinfo.{'.'.join(parts[:-1])}"
            resource_name = parts[-1]
        else:
            resource_name = parts[0]

        with importlib.resources.as_file(
            importlib.resources.files(package).joinpath(resource_name)
        ) as path:
            with open(path, "rb") as f:
                content = f.read()

        if content[:4] != b"TZif":
            return None

        last_newline = content.rfind(b"\n")
        if last_newline > 0:
            second_last_newline = content.rfind(b"\n", 0, last_newline)
            if second_last_newline >= 0:
                posix_tz = content[second_last_newline + 1 : last_newline].decode("ascii")
                if posix_tz:
                    return posix_tz

        return None

    except Exception:
        return None


def _build_timezone_caches() -> None:
    """Build all timezone caches. Blocking I/O - call from executor only."""
    global _iana_timezones, _iana_to_posix, _posix_to_iana, _cache_initialized

    from zoneinfo import available_timezones

    # Every name we publish must be a usable IANA zone, because callers set
    # the door's timezone from this list. `available_timezones()` also
    # surfaces whatever else the host's tzdata tree contains: a system with
    # /usr/share/zoneinfo/localtime (CI containers have one) yields
    # "localtime", which is a symlink to the local zone rather than a zone,
    # has no TZif footer, and would be offered to a user as a choice that
    # cannot be applied. Drop anything with no POSIX string rather than
    # advertise it.
    all_tzs = sorted(available_timezones())

    usable: list[str] = []
    for tz_name in all_tzs:
        posix = _extract_posix_from_tzif(tz_name)
        if not posix:
            _LOGGER.debug(
                t(
                    "tz_utils.skipping_posix_rule_tzif_footer",
                    "Skipping %s: no POSIX rule in its TZif footer",
                ),
                tz_name,
            )
            continue
        usable.append(tz_name)
        _iana_to_posix[tz_name] = posix
        # First match wins for reverse lookup
        if posix not in _posix_to_iana:
            _posix_to_iana[posix] = tz_name

    _iana_timezones = usable

    _cache_initialized = True
    _LOGGER.debug(
        t(
            "tz_utils.timezone_cache_initialized_timezones_posix",
            "Timezone cache initialized: %d timezones, %d POSIX mappings",
        ),
        len(_iana_timezones),
        len(_iana_to_posix),
    )


async def async_init_timezone_cache() -> None:
    """Initialize timezone caches in a thread (non-blocking).

    Uses asyncio.to_thread to run blocking I/O in a thread pool.
    Must be called once before using other functions. Safe to call
    concurrently: the tzdata scan runs at most once.
    """
    if _cache_initialized:
        return

    await asyncio.to_thread(init_timezone_cache_sync)


def init_timezone_cache_sync() -> None:
    """Initialize timezone caches synchronously (blocking).

    Use async_init_timezone_cache() for non-blocking initialization.
    Safe to call concurrently: the tzdata scan runs at most once.
    """
    if _cache_initialized:
        return

    with _cache_lock:
        if _cache_initialized:
            return
        _build_timezone_caches()


def is_cache_initialized() -> bool:
    """Check if the timezone cache has been initialized."""
    return _cache_initialized


def get_available_timezones() -> list[str]:
    """Get sorted list of available IANA timezone names.

    Returns a copy - mutating the returned list does not affect the
    internal cache. Returns empty list if cache not initialized.
    """
    if _iana_timezones is None:
        _LOGGER.warning(
            t(
                "tz_utils.timezone_cache_initialized_returning_empty",
                "Timezone cache not initialized, returning empty list",
            )
        )
        return []
    return list(_iana_timezones)


def get_posix_tz_string(iana_timezone: str) -> str | None:
    """Get POSIX TZ string for an IANA timezone name.

    Returns None if not found or cache not initialized.
    """
    return _iana_to_posix.get(iana_timezone)


def find_iana_for_posix(posix_tz: str) -> str | None:
    """Find an IANA timezone name for a given POSIX TZ string.

    Returns None if not found or cache not initialized.
    """
    return _posix_to_iana.get(posix_tz)


def parse_posix_tz_string(posix_tz: str) -> dict | None:
    """Parse a POSIX TZ string into its components.

    Handles both alphabetic abbreviations (``EST5EDT,M3.2.0,M11.1.0``)
    and the angle-bracket forms modern tzdata emits for numeric zone
    names (``<+05>-5``, ``<-03>3``); the brackets are stripped from the
    returned abbreviations.

    Args:
        posix_tz: POSIX TZ string (e.g., 'EST5EDT,M3.2.0,M11.1.0')

    Returns:
        Dictionary with parsed components, or None if the input is empty
        or no valid abbreviation/offset could be parsed.
    """
    if not posix_tz:
        return None

    result = {
        "raw": posix_tz,
        "std_abbrev": None,
        "std_offset": None,
        "dst_abbrev": None,
        "dst_offset": None,
        "dst_start": None,
        "dst_end": None,
    }

    if "," in posix_tz:
        tz_part, rules = posix_tz.split(",", 1)
        rule_parts = rules.split(",")
        if len(rule_parts) >= 2:
            result["dst_start"] = rule_parts[0]
            result["dst_end"] = rule_parts[1]
    else:
        tz_part = posix_tz

    match = _POSIX_TZ_RE.match(tz_part)
    if not match:
        # Device-supplied: never log it raw (ANSI injection into host logs).
        _LOGGER.debug(
            t("tz_utils.could_parse_posix_tz_string", "Could not parse POSIX TZ string: %s"),
            sanitize_field(posix_tz),
        )
        return None

    result["std_abbrev"] = match.group(1).strip("<>")
    result["std_offset"] = match.group(2)
    dst_abbrev = match.group(3)
    result["dst_abbrev"] = dst_abbrev.strip("<>") if dst_abbrev else None
    result["dst_offset"] = match.group(4)

    return result
