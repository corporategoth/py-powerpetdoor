# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Hypothesis property tests for client value coercion (H6).

make_bool coerces untrusted wire values; it must be total (never raise)
and deterministic across the documented input domains.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from powerpetdoor import make_bool


class TestMakeBoolProperties:
    """make_bool is total over strings, ints and booleans."""

    @settings(max_examples=200, deadline=None)
    @given(text=st.text(max_size=12))
    def test_text_maps_into_tristate(self, text):
        """Any string maps to exactly True, False or None - never raises.

        Asserted by identity: `1 in (True, False, None)` is True in Python,
        so membership does not pin the type the docstring promises
        (round-6 test-fanatic L2).
        """
        result = make_bool(text)
        assert result is True or result is False or result is None

    @given(token=st.sampled_from(["1", "true", "yes", "on"]), data=st.data())
    def test_true_tokens_in_any_case(self, token, data):
        """Recognized true tokens parse case-insensitively."""
        variant = "".join(c.upper() if data.draw(st.booleans()) else c for c in token)
        assert make_bool(variant) is True

    @given(token=st.sampled_from(["0", "false", "no", "off"]), data=st.data())
    def test_false_tokens_in_any_case(self, token, data):
        """Recognized false tokens parse case-insensitively."""
        variant = "".join(c.upper() if data.draw(st.booleans()) else c for c in token)
        assert make_bool(variant) is False

    @given(value=st.integers())
    def test_integers_map_to_nonzero_test(self, value):
        """Any int coerces to (value != 0)."""
        assert make_bool(value) is (value != 0)

    @given(value=st.booleans())
    def test_booleans_pass_through(self, value):
        """Booleans are returned unchanged."""
        assert make_bool(value) is value
