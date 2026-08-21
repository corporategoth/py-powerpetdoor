# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Hypothesis property tests for POSIX TZ string parsing (H6).

parse_posix_tz_string handles wire values from real devices, so it must
be total over arbitrary text (never raise, never return a half-parsed
dict) and it must parse every footer string real tzdata ships.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import powerpetdoor.tz_utils as tz_utils

# Build the real-footer corpus once per process; the cache is also what
# production code uses, so this exercises the same data path.
tz_utils.init_timezone_cache_sync()
_REAL_POSIX_STRINGS = sorted(set(tz_utils._iana_to_posix.values()))


class TestParsePosixTzProperties:
    """parse_posix_tz_string totality and real-tzdata robustness."""

    @settings(max_examples=200, deadline=None)
    @given(text=st.text(max_size=40))
    def test_never_raises_and_never_half_parses(self, text):
        """Arbitrary text yields None or a dict with abbrev+offset set."""
        result = tz_utils.parse_posix_tz_string(text)

        if result is not None:
            assert result["raw"] == text
            assert result["std_abbrev"]  # Non-empty when parsed
            assert result["std_offset"]  # Regex requires at least one digit

    @settings(max_examples=100, deadline=None)
    @given(posix=st.sampled_from(_REAL_POSIX_STRINGS))
    def test_every_real_tzdata_footer_parses(self, posix):
        """Every footer string shipped by tzdata parses completely."""
        result = tz_utils.parse_posix_tz_string(posix)

        assert result is not None
        assert result["raw"] == posix
        assert result["std_abbrev"]
        assert result["std_offset"]
        if "," in posix:
            # tzdata DST rules always come as a start/end pair.
            assert result["dst_start"]
            assert result["dst_end"]
