# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Tests for timezone utilities module."""
from __future__ import annotations

import pytest

import powerpetdoor.tz_utils as tz_utils


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture(scope="module", autouse=True)
def init_cache():
    """Ensure timezone cache is initialized for all tests in this module."""
    tz_utils.init_timezone_cache_sync()


@pytest.fixture
def reset_cache():
    """Reset cache state for tests that need uninitialized cache.

    Note: This is a bit hacky since we're modifying module globals,
    but it's necessary to test uninitialized cache behavior.
    """
    # Save current state
    old_initialized = tz_utils._cache_initialized
    old_timezones = tz_utils._iana_timezones
    old_iana_to_posix = tz_utils._iana_to_posix.copy()
    old_posix_to_iana = tz_utils._posix_to_iana.copy()

    # Reset to uninitialized state
    tz_utils._cache_initialized = False
    tz_utils._iana_timezones = None
    tz_utils._iana_to_posix.clear()
    tz_utils._posix_to_iana.clear()

    yield

    # Restore state
    tz_utils._cache_initialized = old_initialized
    tz_utils._iana_timezones = old_timezones
    tz_utils._iana_to_posix.update(old_iana_to_posix)
    tz_utils._posix_to_iana.update(old_posix_to_iana)


# ============================================================================
# Cache Initialization Tests
# ============================================================================

class TestCacheInitialization:
    """Tests for cache initialization functions."""

    def test_is_cache_initialized_after_sync_init(self):
        """Cache should be initialized after sync init."""
        assert tz_utils.is_cache_initialized() is True

    def test_sync_init_is_idempotent(self):
        """Calling init multiple times should be safe."""
        tz_utils.init_timezone_cache_sync()
        tz_utils.init_timezone_cache_sync()
        assert tz_utils.is_cache_initialized() is True

    @pytest.mark.asyncio
    async def test_async_init_works(self, reset_cache):
        """Async initialization should work correctly."""
        assert tz_utils.is_cache_initialized() is False
        await tz_utils.async_init_timezone_cache()
        assert tz_utils.is_cache_initialized() is True

    @pytest.mark.asyncio
    async def test_async_init_is_idempotent(self):
        """Calling async init multiple times should be safe."""
        await tz_utils.async_init_timezone_cache()
        await tz_utils.async_init_timezone_cache()
        assert tz_utils.is_cache_initialized() is True

    def test_cache_not_initialized_returns_false(self, reset_cache):
        """is_cache_initialized returns False when not initialized."""
        assert tz_utils.is_cache_initialized() is False


# ============================================================================
# Get Available Timezones Tests
# ============================================================================

class TestGetAvailableTimezones:
    """Tests for get_available_timezones function."""

    def test_returns_list(self):
        """Should return a list."""
        result = tz_utils.get_available_timezones()
        assert isinstance(result, list)

    def test_returns_many_timezones(self):
        """Should return hundreds of timezones."""
        result = tz_utils.get_available_timezones()
        assert len(result) > 400  # There are ~500+ IANA timezones

    def test_list_is_sorted(self):
        """Timezone list should be sorted alphabetically."""
        result = tz_utils.get_available_timezones()
        assert result == sorted(result)

    def test_contains_common_timezones(self):
        """Should contain well-known timezones."""
        result = tz_utils.get_available_timezones()
        assert "America/New_York" in result
        assert "America/Los_Angeles" in result
        assert "Europe/London" in result
        assert "Asia/Tokyo" in result
        assert "UTC" in result

    def test_returns_empty_when_not_initialized(self, reset_cache):
        """Should return empty list when cache not initialized."""
        result = tz_utils.get_available_timezones()
        assert result == []


# ============================================================================
# IANA to POSIX Conversion Tests
# ============================================================================

class TestGetPosixTzString:
    """Tests for get_posix_tz_string function."""

    def test_new_york_conversion(self):
        """America/New_York should convert to EST5EDT format."""
        result = tz_utils.get_posix_tz_string("America/New_York")
        assert result is not None
        assert "EST" in result
        assert "EDT" in result

    def test_los_angeles_conversion(self):
        """America/Los_Angeles should convert to PST8PDT format."""
        result = tz_utils.get_posix_tz_string("America/Los_Angeles")
        assert result is not None
        assert "PST" in result
        assert "PDT" in result

    def test_utc_conversion(self):
        """UTC should convert to simple format."""
        result = tz_utils.get_posix_tz_string("UTC")
        assert result is not None
        assert "UTC" in result

    def test_london_conversion(self):
        """Europe/London should convert to GMT/BST format."""
        result = tz_utils.get_posix_tz_string("Europe/London")
        assert result is not None
        # London uses GMT in winter and BST in summer
        assert "GMT" in result or "BST" in result

    def test_invalid_timezone_returns_none(self):
        """Invalid timezone name should return None."""
        result = tz_utils.get_posix_tz_string("Invalid/Timezone")
        assert result is None

    def test_empty_string_returns_none(self):
        """Empty string should return None."""
        result = tz_utils.get_posix_tz_string("")
        assert result is None

    def test_posix_string_format(self):
        """POSIX strings should have expected format with DST rules."""
        result = tz_utils.get_posix_tz_string("America/New_York")
        assert result is not None
        # Should contain DST transition rules like M3.2.0 (March 2nd Sunday)
        assert "M3" in result or "M11" in result

    def test_non_dst_timezone(self):
        """Timezones without DST should still work."""
        # Arizona doesn't observe DST
        result = tz_utils.get_posix_tz_string("America/Phoenix")
        assert result is not None
        # Should be MST7 without DST rules
        assert "MST" in result


# ============================================================================
# POSIX to IANA Reverse Lookup Tests
# ============================================================================

class TestFindIanaForPosix:
    """Tests for find_iana_for_posix function."""

    def test_find_eastern_timezone(self):
        """Should find an IANA timezone for EST5EDT format."""
        # Get the POSIX string for New York first
        posix = tz_utils.get_posix_tz_string("America/New_York")
        assert posix is not None

        # Reverse lookup should find a timezone
        result = tz_utils.find_iana_for_posix(posix)
        assert result is not None
        # Should be an America/* timezone with EST/EDT
        assert result.startswith("America/")

    def test_find_pacific_timezone(self):
        """Should find an IANA timezone for PST8PDT format."""
        posix = tz_utils.get_posix_tz_string("America/Los_Angeles")
        assert posix is not None

        result = tz_utils.find_iana_for_posix(posix)
        assert result is not None
        assert result.startswith("America/")

    def test_invalid_posix_returns_none(self):
        """Invalid POSIX string should return None."""
        result = tz_utils.find_iana_for_posix("INVALID123")
        assert result is None

    def test_empty_string_returns_none(self):
        """Empty string should return None."""
        result = tz_utils.find_iana_for_posix("")
        assert result is None

    def test_roundtrip_conversion(self):
        """Converting IANA->POSIX->IANA should return a valid timezone."""
        original = "America/Chicago"
        posix = tz_utils.get_posix_tz_string(original)
        assert posix is not None

        # The reverse lookup may not return the same timezone
        # (multiple IANA zones can map to same POSIX string)
        # but it should return *a* valid timezone
        result = tz_utils.find_iana_for_posix(posix)
        assert result is not None
        assert result in tz_utils.get_available_timezones()


# ============================================================================
# POSIX String Parsing Tests
# ============================================================================

class TestParsePosixTzString:
    """Tests for parse_posix_tz_string function."""

    def test_parse_est_edt(self):
        """Should parse EST5EDT,M3.2.0,M11.1.0 correctly."""
        result = tz_utils.parse_posix_tz_string("EST5EDT,M3.2.0,M11.1.0")
        assert result is not None
        assert result["std_abbrev"] == "EST"
        assert result["std_offset"] == "5"
        assert result["dst_abbrev"] == "EDT"
        assert result["dst_start"] == "M3.2.0"
        assert result["dst_end"] == "M11.1.0"
        assert result["raw"] == "EST5EDT,M3.2.0,M11.1.0"

    def test_parse_pst_pdt(self):
        """Should parse PST8PDT,M3.2.0,M11.1.0 correctly."""
        result = tz_utils.parse_posix_tz_string("PST8PDT,M3.2.0,M11.1.0")
        assert result is not None
        assert result["std_abbrev"] == "PST"
        assert result["std_offset"] == "8"
        assert result["dst_abbrev"] == "PDT"

    def test_parse_with_time_specifier(self):
        """Should parse POSIX strings with time in DST rules."""
        result = tz_utils.parse_posix_tz_string("EST5EDT,M3.2.0/2,M11.1.0/2")
        assert result is not None
        assert result["dst_start"] == "M3.2.0/2"
        assert result["dst_end"] == "M11.1.0/2"

    def test_parse_simple_timezone(self):
        """Should parse simple timezone without DST."""
        result = tz_utils.parse_posix_tz_string("UTC0")
        assert result is not None
        assert result["std_abbrev"] == "UTC"
        assert result["std_offset"] == "0"
        assert result["dst_abbrev"] is None
        assert result["dst_start"] is None
        assert result["dst_end"] is None

    def test_parse_negative_offset(self):
        """Should parse timezone with negative offset."""
        result = tz_utils.parse_posix_tz_string("IST-5:30")
        assert result is not None
        assert result["std_abbrev"] == "IST"
        assert result["std_offset"] == "-5:30"

    def test_parse_with_colon_offset(self):
        """Should parse timezone with colon in offset."""
        result = tz_utils.parse_posix_tz_string("NST3:30NDT,M3.2.0,M11.1.0")
        assert result is not None
        assert result["std_abbrev"] == "NST"
        assert result["std_offset"] == "3:30"
        assert result["dst_abbrev"] == "NDT"

    def test_parse_empty_returns_none(self):
        """Empty string should return None."""
        result = tz_utils.parse_posix_tz_string("")
        assert result is None

    def test_parse_none_returns_none(self):
        """None input should return None."""
        result = tz_utils.parse_posix_tz_string(None)
        assert result is None

    def test_raw_field_preserved(self):
        """Raw input should be preserved in result."""
        input_str = "CET-1CEST,M3.5.0,M10.5.0/3"
        result = tz_utils.parse_posix_tz_string(input_str)
        assert result is not None
        assert result["raw"] == input_str

    def test_parse_gmt_bst(self):
        """Should parse GMT0BST format (London)."""
        result = tz_utils.parse_posix_tz_string("GMT0BST,M3.5.0/1,M10.5.0")
        assert result is not None
        assert result["std_abbrev"] == "GMT"
        assert result["std_offset"] == "0"
        assert result["dst_abbrev"] == "BST"


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests for timezone utilities."""

    def test_all_timezones_have_posix(self):
        """Most timezones should have POSIX mappings."""
        timezones = tz_utils.get_available_timezones()
        with_posix = 0
        for tz in timezones:
            if tz_utils.get_posix_tz_string(tz) is not None:
                with_posix += 1

        # At least 90% of timezones should have POSIX mappings
        assert with_posix / len(timezones) > 0.9

    def test_posix_strings_are_parseable(self):
        """All POSIX strings from conversions should be parseable."""
        sample_timezones = [
            "America/New_York",
            "America/Los_Angeles",
            "Europe/London",
            "Europe/Paris",
            "Asia/Tokyo",
            "Australia/Sydney",
        ]

        for tz in sample_timezones:
            posix = tz_utils.get_posix_tz_string(tz)
            if posix:
                parsed = tz_utils.parse_posix_tz_string(posix)
                assert parsed is not None, f"Failed to parse POSIX for {tz}: {posix}"
                assert parsed["std_abbrev"] is not None
                assert parsed["std_offset"] is not None

    def test_us_timezones(self):
        """Test common US timezones."""
        us_zones = {
            "America/New_York": ("EST", "EDT"),
            "America/Chicago": ("CST", "CDT"),
            "America/Denver": ("MST", "MDT"),
            "America/Los_Angeles": ("PST", "PDT"),
        }

        for tz, (std, dst) in us_zones.items():
            posix = tz_utils.get_posix_tz_string(tz)
            assert posix is not None, f"No POSIX for {tz}"
            assert std in posix, f"{std} not in POSIX for {tz}: {posix}"
            assert dst in posix, f"{dst} not in POSIX for {tz}: {posix}"

    def test_european_timezones(self):
        """Test common European timezones."""
        result = tz_utils.get_posix_tz_string("Europe/Berlin")
        assert result is not None
        # Berlin uses CET/CEST
        parsed = tz_utils.parse_posix_tz_string(result)
        assert parsed is not None
        assert parsed["std_abbrev"] is not None

    def test_cache_consistency(self):
        """Cache should return consistent results."""
        # Multiple calls should return same results
        tz1 = tz_utils.get_available_timezones()
        tz2 = tz_utils.get_available_timezones()
        assert tz1 == tz2

        posix1 = tz_utils.get_posix_tz_string("America/New_York")
        posix2 = tz_utils.get_posix_tz_string("America/New_York")
        assert posix1 == posix2
