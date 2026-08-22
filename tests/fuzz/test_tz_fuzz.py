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

import random

from hypothesis import given, settings
from hypothesis import strategies as st

import powerpetdoor.tz_utils as tz_utils

# Build the real-footer corpus once per process; the cache is also what
# production code uses, so this exercises the same data path.
tz_utils.init_timezone_cache_sync()
_REAL_POSIX_STRINGS = sorted(set(tz_utils._iana_to_posix.values()))


def _mutate_posix(posix: str, cut: int, char: str) -> str:
    """Corrupt one character of a real footer, keeping it TZ-shaped."""
    if not posix:
        return char
    index = cut % len(posix)
    return posix[:index] + char + posix[index + 1 :]


#: Real tzdata footers with a single character replaced. Unlike raw text
#: these land on both sides of the parse boundary, so the "never
#: half-parses" invariant is actually exercised (round-6 test-fanatic L4).
_MUTATED_POSIX_STRINGS = st.builds(
    _mutate_posix,
    st.sampled_from(_REAL_POSIX_STRINGS),
    st.integers(min_value=0, max_value=64),
    st.sampled_from(list(",.<>-+0123456789ABCXYZ ")),
)


class TestParsePosixTzProperties:
    """parse_posix_tz_string totality and real-tzdata robustness."""

    @settings(max_examples=200, deadline=None)
    @given(
        text=st.one_of(
            st.text(max_size=40),
            # `st.text()` essentially never parses as a POSIX TZ string, so
            # on its own the "never half-parses" half of this property ran
            # 3 times in 600 draws (round-6 test-fanatic L4). Mutating real
            # footers is what actually reaches the success path.
            _MUTATED_POSIX_STRINGS,
        )
    )
    def test_never_raises_and_never_half_parses(self, text):
        """Arbitrary text yields None or a dict with abbrev+offset set."""
        result = tz_utils.parse_posix_tz_string(text)

        if result is not None:
            assert result["raw"] == text
            assert result["std_abbrev"]  # Non-empty when parsed
            assert result["std_offset"]  # Regex requires at least one digit

    def test_the_mutation_strategy_reaches_both_sides_of_the_parse(self):
        """A property is only worth its runtime if it draws the right values.

        `st.text(max_size=40)` reached the success-path assertions in
        3 of 600 draws (0.5%), so "never half-parses" was effectively
        untested. Measured deterministically here rather than trusted
        (round-6 test-fanatic L4).
        """
        rng = random.Random(0)
        parsed = 0
        draws = 600

        for _ in range(draws):
            mutated = _mutate_posix(
                rng.choice(_REAL_POSIX_STRINGS),
                rng.randrange(0, 65),
                rng.choice(list(",.<>-+0123456789ABCXYZ ")),
            )
            result = tz_utils.parse_posix_tz_string(mutated)
            if result is not None:
                parsed += 1
                assert result["std_abbrev"]
                assert result["std_offset"]

        # Measured at 400/600; the bar is set well below that so the test
        # pins "the strategy works" rather than an exact ratio.
        assert parsed > draws // 4
        assert parsed < draws  # ...and it still draws rejects

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
