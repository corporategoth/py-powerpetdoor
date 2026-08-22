# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Hypothesis property tests for wire framing.

These pin the framing contract (decision D1): any list of JSON objects,
delivered with any chunking and any non-JSON garbage between them, must
come back out as exactly the same objects; the un-parsed buffer must stay
bounded; and the scanner must never raise on arbitrary input.

The example counts are deliberately bounded so the whole suite stays fast.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from powerpetdoor import PowerPetDoorClient, framing
from powerpetdoor.framing import MAX_BUFFER_SIZE, FrameScanner, extract_frames

# JSON payloads kept small: framing behavior does not depend on payload
# size, and small examples keep the suite fast.
_json_values = st.one_of(
    st.text(max_size=8),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.booleans(),
    st.none(),
)
_json_objects = st.dictionaries(st.text(min_size=1, max_size=8), _json_values, max_size=4)

# Garbage must not contain '{' (a '{' legitimately starts a new frame) —
# anything else, including '}', quotes, and whitespace, is fair game.
_garbage = st.text(
    alphabet=st.characters(blacklist_characters="{", blacklist_categories=("Cs",)), max_size=10
)

# Individual bytes that cannot be decoded as ASCII.
_non_ascii_bytes = st.integers(min_value=0x80, max_value=0xFF)


def _feed_chunks(chunks: list[str]) -> tuple[list[str], str, bool]:
    """Feed chunks through extract_frames the way a receiver would."""
    buffer = ""
    collected: list[str] = []
    overflow = False
    for chunk in chunks:
        frames, buffer, diag = extract_frames(buffer + chunk)
        collected.extend(frames)
        overflow = overflow or diag.overflow
        assert len(buffer) <= MAX_BUFFER_SIZE
    return collected, buffer, overflow


def _split_at(stream, cuts: list[int]) -> list:
    """Split a str or bytes stream into chunks at the given cut points."""
    chunks = []
    prev = 0
    for cut in [*sorted(cuts), len(stream)]:
        chunks.append(stream[prev:cut])
        prev = cut
    return chunks


def _capture_client() -> tuple[PowerPetDoorClient, list[dict]]:
    """Build a client whose dispatched messages are recorded synchronously."""
    client = PowerPetDoorClient(
        host="127.0.0.1",
        port=3000,
        keepalive=30.0,
        timeout=5.0,
        reconnect=5.0,
        # Any truthy object prevents the constructor from creating a
        # private event loop; dispatch is captured synchronously below.
        loop=SimpleNamespace(),
    )
    received: list[dict] = []

    async def _noop() -> None:
        pass

    def _record(msg):
        received.append(msg)
        return _noop()

    client.process_message = _record
    client._track_task = lambda coro: coro.close()
    return client, received


class TestFramingProperties:
    """Property tests for the shared frame scanner."""

    @settings(max_examples=100, deadline=None)
    @given(objs=st.lists(_json_objects, min_size=1, max_size=5), data=st.data())
    def test_round_trip_any_chunking(self, objs, data):
        """Any JSON objects with any chunking come back out identically."""
        stream = "".join(json.dumps(o) for o in objs)
        cuts = data.draw(st.lists(st.integers(min_value=0, max_value=len(stream)), max_size=8))
        collected, buffer, overflow = _feed_chunks(_split_at(stream, cuts))

        assert [json.loads(f) for f in collected] == objs
        assert buffer == ""
        assert overflow is False

    @settings(max_examples=100, deadline=None)
    @given(
        objs=st.lists(_json_objects, min_size=1, max_size=4),
        junk=st.lists(_garbage, min_size=1, max_size=5),
    )
    def test_garbage_injection_resync(self, objs, junk):
        """Garbage injected between objects never loses a valid frame."""
        parts = []
        for i, obj in enumerate(objs):
            parts.append(junk[i % len(junk)])
            parts.append(json.dumps(obj))
        stream = "".join(parts)

        collected, buffer, overflow = _feed_chunks([stream])

        assert [json.loads(f) for f in collected] == objs
        assert buffer == ""
        assert overflow is False

    @settings(max_examples=200, deadline=None)
    @given(text=st.text(max_size=200))
    def test_never_raises_and_bounds_buffer(self, text):
        """extract_frames never raises and never retains more than the cap."""
        frames, remainder, diag = extract_frames(text, max_buffer=64)

        assert isinstance(remainder, str)
        assert len(remainder) <= 64
        for frame in frames:
            assert frame.startswith("{")
            assert frame.endswith("}")

    @settings(max_examples=20, deadline=None)
    @given(extra=st.integers(min_value=1, max_value=4096))
    def test_buffer_cap_enforced(self, extra):
        """An unbounded non-closing stream is cleared once it exceeds the cap."""
        frames, remainder, diag = extract_frames("{" * (MAX_BUFFER_SIZE + extra))

        assert frames == []
        assert remainder == ""
        assert diag.overflow is True


class TestScannerLinearityProperties:
    """Total scan work is linear in bytes delivered, whatever the chunking.

    ``MAX_BUFFER_SIZE`` bounds the *memory* a dribbling peer can cost; it
    does not bound the CPU spent reaching that bound. Re-scanning the
    retained buffer on every ``data_received`` made that quadratic, and the
    attacker picks the chunk size, so the attacker picks the exponent (S1).
    """

    @staticmethod
    def _feed_with_counter(chunks: list[str]) -> tuple[list[str], int]:
        """Feed chunks through a FrameScanner, counting characters examined."""
        examined = 0
        original = framing._BraceScanner.scan
        scanner = FrameScanner()
        collected: list[str] = []
        try:

            def counting_scan(self, s, start):
                nonlocal examined
                end = original(self, s, start)
                examined += end - start
                return end

            framing._BraceScanner.scan = counting_scan
            for chunk in chunks:
                frames, _diag = scanner.feed(chunk)
                collected.extend(frames)
        finally:
            framing._BraceScanner.scan = original
        return collected, examined

    @settings(max_examples=50, deadline=None)
    @given(
        objs=st.lists(_json_objects, min_size=1, max_size=4),
        data=st.data(),
    )
    def test_complete_stream_is_examined_once_per_character(self, objs, data):
        stream = "".join(json.dumps(o) for o in objs)
        cuts = data.draw(st.lists(st.integers(min_value=0, max_value=len(stream)), max_size=8))

        collected, examined = self._feed_with_counter(_split_at(stream, cuts))

        assert [json.loads(f) for f in collected] == objs
        # Whitespace and garbage are skipped without entering the scanner,
        # so the bound is "at most once", never "more than once".
        assert examined <= len(stream)

    @settings(max_examples=25, deadline=None)
    @given(
        body=st.text(
            alphabet=st.characters(blacklist_characters='{}"\\', blacklist_categories=("Cs",)),
            min_size=64,
            max_size=400,
        ),
        chunk_size=st.integers(min_value=1, max_value=32),
    )
    def test_unterminated_object_costs_one_pass_at_any_chunk_size(self, body, chunk_size):
        """The attack payload: an object that never closes, dribbled in."""
        payload = "{" + body
        chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]

        collected, examined = self._feed_with_counter(chunks)

        assert collected == []
        assert examined == len(payload)


class TestClientFramingProperties:
    """The full client receive path honors the same framing round trip."""

    @settings(max_examples=50, deadline=None)
    @given(objs=st.lists(_json_objects, min_size=1, max_size=4), data=st.data())
    def test_data_received_round_trip_any_chunking(self, objs, data):
        """client.data_received reassembles any chunking into the same messages."""
        client, received = _capture_client()

        stream = "".join(json.dumps(o) for o in objs)
        cuts = data.draw(st.lists(st.integers(min_value=0, max_value=len(stream)), max_size=6))
        for chunk in _split_at(stream, cuts):
            client.data_received(chunk.encode("ascii"))

        assert received == objs
        assert client._buffer == ""

    @settings(max_examples=50, deadline=None)
    @given(
        objs=st.lists(_json_objects, min_size=1, max_size=4),
        bad=st.lists(_non_ascii_bytes, min_size=1, max_size=4),
        data=st.data(),
    )
    def test_non_ascii_bytes_between_frames_lose_nothing(self, objs, bad, data):
        """Non-ASCII bytes between frames must not desync the stream (L2).

        Dropping the whole chunk on UnicodeDecodeError stranded whatever
        was already buffered, so every later message went unprocessed until
        the 64 KiB overflow disconnect.
        """
        client, received = _capture_client()

        parts: list[bytes] = []
        for obj in objs:
            parts.append(bytes(bad))
            parts.append(json.dumps(obj).encode("ascii"))
        stream = b"".join(parts)

        cuts = data.draw(st.lists(st.integers(min_value=0, max_value=len(stream)), max_size=6))
        for chunk in _split_at(stream, cuts):
            client.data_received(chunk)

        assert received == objs
        assert client._buffer == ""
