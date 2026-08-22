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

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from hypothesis import assume, event, given, settings
from hypothesis import strategies as st

from powerpetdoor import PowerPetDoorClient, framing
from powerpetdoor.framing import MAX_BUFFER_SIZE, FrameScanner, extract_frames
from powerpetdoor.simulator.protocol import DoorSimulatorProtocol
from powerpetdoor.simulator.state import DoorSimulatorState

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

    def _record(msg, **_kwargs):
        # `**_kwargs` absorbs `frame_size=` (round-9 backend F2).
        received.append(msg)
        return _noop()

    client.process_message = _record
    client._track_task = lambda coro: coro.close()
    return client, received


class _FlowTransport(asyncio.Transport):
    """A transport that records the dispatcher's flow-control decisions."""

    def __init__(self) -> None:
        super().__init__()
        self.paused = False

    def pause_reading(self) -> None:
        self.paused = True

    def resume_reading(self) -> None:
        self.paused = False

    def write(self, data: bytes) -> None:
        """Answers are irrelevant here; the receive path is under test."""

    def get_write_buffer_size(self) -> int:
        return 0

    def close(self) -> None:
        """The properties never close a connection."""

    def abort(self) -> None:
        """Nor abort one."""

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, name, default=None):
        return ("127.0.0.1", 3000) if name == "peername" else default


def _capture_simulator_protocol() -> DoorSimulatorProtocol:
    """A connected `DoorSimulatorProtocol` over a flow-recording transport."""
    protocol = DoorSimulatorProtocol(DoorSimulatorState())
    protocol.connection_made(_FlowTransport())
    return protocol


@pytest.fixture(scope="module", autouse=True)
def _fuzz_event_loop():
    """One loop for the whole module's byte-feeding properties.

    `data_received` reaches `_schedule_pump`, which needs a *running*
    loop - so these properties cannot be driven the way the older ones are
    (those never stall the backlog, so the re-arm is never armed). Module
    scope keeps the per-example cost off the hot path, and closing it here
    keeps `filterwarnings = ["error"]` satisfied.
    """
    global _LOOP
    _LOOP = asyncio.new_event_loop()
    yield
    _LOOP.close()
    _LOOP = None


_LOOP: asyncio.AbstractEventLoop | None = None


def _drain(feed, dispatcher) -> None:
    """Run ``feed()`` on the module loop and let the dispatcher finish.

    "Never wedges" is precisely "a backlog that survives every turn the
    loop can give it", so the drain runs until the dispatcher is idle
    rather than for a fixed number of turns - a wedge then shows up as the
    bound being exhausted, which the caller's assertions catch.
    """

    async def _run() -> None:
        feed()
        for _ in range(20000):
            if not dispatcher.backlog and not dispatcher.inflight:
                return
            await asyncio.sleep(0)

    assert _LOOP is not None
    _LOOP.run_until_complete(asyncio.wait_for(_run(), timeout=60))


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


# ============================================================================
# Arbitrary raw bytes into data_received (round-8 backend M1 / security M1)
# ============================================================================
#
# The properties above feed *well-formed* JSON: `_json_objects` are dicts
# serialised with `json.dumps`, and `_garbage` explicitly excludes `{`, so
# no frame they generate can fail to decode. The untrusted-input suite has
# the mirror-image gap - it bounds integers at 10**9, caps `st.recursive`
# at `max_leaves=8`, and hands post-parse Python objects to handlers rather
# than JSON *text* to `data_received`.
#
# Between them, neither can produce the two shapes that made `json.loads`
# raise something other than `JSONDecodeError` and escape `_dispatch_frame`:
# an integer literal over `sys.get_int_max_str_digits()` digits (a bare
# `ValueError`) and nesting deep enough for `RecursionError`. Both are
# brace-balanced and under `MAX_BUFFER_SIZE`, so framing passes them
# straight through to the decoder.
#
# These properties close that gap on both sides of the wire.

#: `json.loads` raises RecursionError from 9999 levels of nesting, and the
#: frame stops fitting under MAX_BUFFER_SIZE (65536) past 10922 - beyond
#: that the scanner's overflow guard is what stops it, not the decoder, and
#: the property would be testing the wrong thing.
_RECURSION_DEPTH = 9999
_MAX_NESTING_DEPTH = (MAX_BUFFER_SIZE - 1) // 6

#: CPython's str->int conversion cap. Above it the json scanner surfaces a
#: bare ValueError rather than a JSONDecodeError.
_INT_DIGIT_CAP = sys.get_int_max_str_digits()


def _nested_frame(depth: int) -> bytes:
    """A brace-balanced frame nested ``depth`` levels deep."""
    return b'{"a":' * depth + b"1" + b"}" * depth


def _integer_frame(digits: int) -> bytes:
    """A frame whose only value is an integer literal of ``digits`` digits."""
    return b'{"n":1' + b"0" * (digits - 1) + b"}"


# Both generators *straddle* their threshold rather than drawing "something
# large" and hoping. Sampling the boundary explicitly is the same rule the
# unit suite follows for numeric limits (CLAUDE.md rule 8), and it is what
# makes the pathological shapes reachable at a rate worth measuring: a
# uniform draw over 1..10922 lands in the RecursionError window less than a
# tenth of the time, and hypothesis biases small.
#
# What is drawn is a *recipe*, not the bytes. A 60 KB `bytes` in a
# hypothesis repr is unreadable, slow to shrink, and (under this project's
# `filterwarnings = ["error"]`) turns hypothesis's own
# "Generating overly large repr" warning into a second failure that hides
# the first. `("nest", 9999)` shrinks to the threshold and prints as one
# line.
# Each threshold gets three branches - below, above, and the boundary
# itself - because hypothesis biases integers small, so one uniform range
# spanning the threshold would put almost every draw on the harmless side.
_nesting_depths = st.one_of(
    st.integers(min_value=1, max_value=_RECURSION_DEPTH - 1),
    st.integers(min_value=_RECURSION_DEPTH, max_value=_MAX_NESTING_DEPTH),
    st.sampled_from(
        [_RECURSION_DEPTH - 1, _RECURSION_DEPTH, _RECURSION_DEPTH + 1, _MAX_NESTING_DEPTH]
    ),
)
_digit_counts = st.one_of(
    st.integers(min_value=1, max_value=_INT_DIGIT_CAP),
    st.integers(min_value=_INT_DIGIT_CAP + 1, max_value=2 * _INT_DIGIT_CAP),
    st.sampled_from([_INT_DIGIT_CAP - 1, _INT_DIGIT_CAP, _INT_DIGIT_CAP + 1]),
)

_pathological_recipes = st.one_of(
    st.tuples(st.just("nest"), _nesting_depths),
    st.tuples(st.just("int"), _digit_counts),
    # Unbounded, so nothing about the magnitude is assumed - but recorded
    # here as what it is: in 3,000 draws `st.integers()` never exceeded 128
    # bits (39 digits), so it is provably *not* what reaches the digit cap.
    # That is the same reason the existing suites cannot: they bound the
    # value, not the literal.
    st.tuples(st.just("unbounded-int"), st.integers()),
)

_hostile_recipes = st.one_of(
    _pathological_recipes,
    st.tuples(st.just("json"), _json_objects),
    st.tuples(st.just("raw"), st.binary(max_size=64)),
    st.tuples(
        st.just("raw"),
        st.sampled_from([b"{", b"}", b"{}", b"{x}", b'{"a":', b"\xff", b"", b" "]),
    ),
)


def _materialise(recipe: tuple[str, object]) -> bytes:
    """Turn a drawn recipe into the bytes a hostile peer would write."""
    kind, value = recipe
    if kind == "nest":
        return _nested_frame(value)
    if kind == "int":
        return _integer_frame(value)
    if kind == "unbounded-int":
        return b'{"n":' + str(value).encode() + b"}"
    if kind == "json":
        return json.dumps(value).encode()
    return value


def _classify(payload: bytes) -> str:
    """Name the shape a payload reached, for the draw-rate statistics.

    Classified against the *decoded* text, because that is what reaches
    `json.loads`: both `data_received` implementations decode with
    `errors="backslashreplace"` before framing, so a raw non-ASCII byte
    never arrives at the decoder as bytes. Counting the resulting
    `UnicodeDecodeError` as a "bare ValueError" would inflate the very
    rate these statistics exist to report.
    """
    try:
        json.loads(payload.decode("ascii", errors="backslashreplace"))
    except RecursionError:
        return "decode: RecursionError"
    except json.JSONDecodeError:
        return "decode: JSONDecodeError"
    except ValueError:
        return "decode: bare ValueError"
    return "decode: ok"


class TestArbitraryBytesNeverRaiseAndNeverWedge:
    """`data_received` "never raises on arbitrary input", on both sides.

    Two documented contracts (`framing.py`'s module docstring and
    `client.data_received`'s) said so and were false. The consequences
    differed by where in the pump the poisoned frame landed - a hot
    reconnect loop from inside `data_received`, a permanently wedged
    dispatcher from inside the `call_soon` re-arm - so both the "never
    raises" and the "never wedges" halves are asserted here.
    """

    @staticmethod
    def _assert_drained(dispatcher, transport) -> None:
        """No backlog, no phantom inflight, reading not left paused."""
        assert dispatcher.backlog == 0
        assert dispatcher.inflight == 0
        assert dispatcher.paused is False
        assert transport.paused is False

    @settings(max_examples=300, deadline=None)
    @given(recipes=st.lists(_hostile_recipes, min_size=1, max_size=6))
    def test_the_client_never_raises_and_never_wedges(self, recipes):
        payloads = [_materialise(recipe) for recipe in recipes]
        for payload in payloads:
            event(_classify(payload))
        client, _ = _capture_client()
        transport = _FlowTransport()
        client._transport = transport
        client._dispatcher._transport = transport

        _drain(
            lambda: [client.data_received(payload) for payload in payloads],
            client._dispatcher,
        )

        self._assert_drained(client._dispatcher, transport)

    @settings(max_examples=300, deadline=None)
    @given(recipes=st.lists(_hostile_recipes, min_size=1, max_size=6))
    def test_the_simulator_never_raises_and_never_wedges(self, recipes):
        payloads = [_materialise(recipe) for recipe in recipes]
        for payload in payloads:
            event(_classify(payload))
        protocol = _capture_simulator_protocol()
        transport = protocol.transport

        _drain(
            lambda: [protocol.data_received(payload) for payload in payloads],
            protocol._dispatcher,
        )

        self._assert_drained(protocol._dispatcher, transport)

    @settings(max_examples=100, deadline=None)
    @given(recipe=_pathological_recipes, chunk_size=st.integers(min_value=1, max_value=4096))
    def test_a_poisoned_frame_split_across_reads_is_stopped_by_the_decoder(
        self, recipe, chunk_size
    ):
        """Delivered in network-sized pieces, the 64 KiB cap cannot be what stops it."""
        payload = _materialise(recipe)
        assume(len(payload) < MAX_BUFFER_SIZE)
        event(_classify(payload))
        client, received = _capture_client()
        transport = _FlowTransport()
        client._transport = transport
        client._dispatcher._transport = transport

        _drain(
            lambda: [
                client.data_received(payload[i : i + chunk_size])
                for i in range(0, len(payload), chunk_size)
            ],
            client._dispatcher,
        )

        self._assert_drained(client._dispatcher, transport)
        assert len(received) == (1 if _classify(payload) == "decode: ok" else 0)
