# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Pytest configuration and fixtures for Power Pet Door tests."""

from __future__ import annotations

import asyncio
import functools
import json
import socket

import pytest

from powerpetdoor import PowerPetDoorClient, framing
from powerpetdoor.const import FIELD_SUCCESS

# ============================================================================
# Session Hygiene
# ============================================================================


@pytest.fixture(scope="session", autouse=True)
def _managed_main_thread_event_loop():
    """Give pytest-asyncio's policy juggling a loop we close ourselves.

    pytest-asyncio's ``_temporary_event_loop_policy`` (scoped-runner setup)
    calls ``asyncio.get_event_loop()``, which on Python <= 3.13 implicitly
    creates a main-thread event loop that is never closed. That loop is
    eventually garbage collected mid-session and emits an unclosed-loop
    ResourceWarning attributed to an arbitrary test - a hard failure under
    ``filterwarnings = ["error"]``. Pre-setting a loop here means the
    implicit-creation path is never taken, and this fixture closes the loop
    deterministically at session end. On Python 3.14+ the implicit creation
    path no longer exists, and this fixture is simply harmless.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    asyncio.set_event_loop(None)
    loop.close()


@pytest.fixture(autouse=True)
def _reset_extra_scripts_dir():
    """Clear the module-level --scripts-dir registration between tests.

    scripting.py publishes the configured extra scripts directory so the
    name resolver, ``list``, the unknown-script hint, and tab completion
    agree; a test that sets one must not leak it into the next.
    """
    yield
    from powerpetdoor.simulator import scripting

    scripting.set_extra_scripts_dir(None)
    # ctl.main() declares that this process may not run scripts by path;
    # that must not leak into a test expecting the simulator CLI's completer.
    scripting.set_script_paths_allowed(True)
    # The description cache is keyed on (path, st_mtime_ns, st_size), so it
    # is correct across real edits - but a test that monkeypatches
    # `Script.from_file` is asking for a parse the cache legitimately
    # skips, and a stale entry from an earlier test made that test's
    # outcome depend on ordering.
    scripting._description_cache.clear()


# ============================================================================
# Mock Transport and Protocol
# ============================================================================


class MockTransport:
    """Mock asyncio transport for network simulation."""

    def __init__(self):
        self.written_data: list[bytes] = []
        self.aborted = False
        self._closing = False
        self._closed = False

    def write(self, data: bytes) -> None:
        """Record written data."""
        self.written_data.append(data)

    def is_closing(self) -> bool:
        """Return whether transport is closing."""
        return self._closing

    def close(self) -> None:
        """Mark transport as closing."""
        self._closing = True

    def abort(self) -> None:
        """Close immediately, discarding any buffered data."""
        self.aborted = True
        self._closing = True

    def get_written_messages(self) -> list[dict]:
        """Parse and return all written JSON messages."""
        messages = []
        for data in self.written_data:
            try:
                messages.append(json.loads(data.decode("ascii")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return messages

    def get_last_message(self) -> dict | None:
        """Get the last written JSON message."""
        messages = self.get_written_messages()
        return messages[-1] if messages else None

    def clear(self) -> None:
        """Clear recorded data."""
        self.written_data.clear()


class MockDeviceProtocol:
    """Helper to simulate Power Pet Door device responses."""

    def __init__(self, client: PowerPetDoorClient):
        self.client = client

    def send_response_sync(self, response: dict) -> None:
        """Synchronously send a response (for non-async contexts)."""
        json_data = json.dumps(response).encode("ascii")
        self.client.data_received(json_data)

    def respond_success(self, msg_id: int, cmd: str, **extra) -> None:
        """Send a generic success response (msgID: the response casing)."""
        response = {FIELD_SUCCESS: "true", "CMD": cmd, "msgID": msg_id, **extra}
        self.send_response_sync(response)


# ============================================================================
# Network Fixtures
# ============================================================================


@pytest.fixture
def refused_port():
    """A TCP port that refuses connections and cannot be re-assigned.

    Binding without listening keeps the port reserved for the whole test -
    a parallel xdist worker binding port 0 can never be handed the same
    number - while connects to it still fail with ECONNREFUSED. This is
    what "bind then close" was approximating, minus the reuse race.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]
    finally:
        sock.close()


# ============================================================================
# Client Fixtures
# ============================================================================


@pytest.fixture
def mock_transport() -> MockTransport:
    """Create a mock transport."""
    return MockTransport()


@pytest.fixture
def client_config() -> dict:
    """Default client configuration."""
    return {
        "host": "192.168.1.100",
        "port": 3000,
        "timeout": 5.0,
        "reconnect": 1.0,  # Fast reconnect for tests
        "keepalive": 30.0,
    }


@pytest.fixture
async def mock_client(
    mock_transport, client_config
) -> tuple[PowerPetDoorClient, MockTransport, MockDeviceProtocol]:
    """Create a PowerPetDoorClient with mocked transport.

    Returns:
        Tuple of (client, transport, device_protocol)
    """
    loop = asyncio.get_running_loop()
    client = PowerPetDoorClient(
        host=client_config["host"],
        port=client_config["port"],
        timeout=client_config["timeout"],
        reconnect=client_config["reconnect"],
        keepalive=client_config["keepalive"],
        loop=loop,
    )

    # Simulate connection established (connection_made installs the
    # transport; assigning it first would trip the double-connect guard)
    client.connection_made(mock_transport)

    # Create device protocol helper
    device = MockDeviceProtocol(client)

    yield client, mock_transport, device

    # Cleanup: stop the client to cancel background tasks
    client.stop()

    # Cancel any remaining tasks created by this client
    if hasattr(client, "_keepalive") and client._keepalive and not client._keepalive.done():
        client._keepalive.cancel()
        try:
            await client._keepalive
        except asyncio.CancelledError:
            pass

    if (
        hasattr(client, "_check_receipt")
        and client._check_receipt
        and not client._check_receipt.done()
    ):
        client._check_receipt.cancel()
        try:
            await client._check_receipt
        except asyncio.CancelledError:
            pass

    # Allow any pending tasks to complete
    await asyncio.sleep(0)


@pytest.fixture
async def disconnected_client(client_config) -> PowerPetDoorClient:
    """Create a PowerPetDoorClient without a connection."""
    loop = asyncio.get_running_loop()
    client = PowerPetDoorClient(
        host=client_config["host"],
        port=client_config["port"],
        timeout=client_config["timeout"],
        reconnect=client_config["reconnect"],
        keepalive=client_config["keepalive"],
        loop=loop,
    )
    return client


# ============================================================================
# Utility Fixtures
# ============================================================================


@pytest.fixture
def callback_tracker() -> dict[str, list]:
    """Track callback invocations."""
    return {
        "calls": [],
        "args": [],
    }


@pytest.fixture
def make_callback(callback_tracker):
    """Factory to create tracked callbacks."""

    def factory(name: str = "callback"):
        def callback(*args, **kwargs):
            callback_tracker["calls"].append(name)
            callback_tracker["args"].append((args, kwargs))

        return callback

    return factory


@pytest.fixture
def make_async_callback(callback_tracker):
    """Factory to create tracked async callbacks."""

    def factory(name: str = "async_callback"):
        async def callback(*args, **kwargs):
            callback_tracker["calls"].append(name)
            callback_tracker["args"].append((args, kwargs))

        return callback

    return factory


# ============================================================================
# Frames that make json.loads raise something other than JSONDecodeError
# ============================================================================
#
# `json.JSONDecodeError` is a *subclass* of ValueError, so catching it is
# not the same as catching what `json.loads` raises. Two shapes escaped
# both `_dispatch_frame` implementations. Both are brace-balanced and under
# `MAX_BUFFER_SIZE`, so the framing cap is provably not what stops them -
# the decoder is.
#
# They live here rather than in one test module because the client and the
# simulator protocol are twins on this path and must be pinned together.


def bigint_frame() -> bytes:
    """A frame whose integer literal exceeds CPython's str->int digit cap.

    `sys.get_int_max_str_digits()` is 4300 by default and the json scanner
    surfaces the refusal as a bare ValueError, not a JSONDecodeError.
    """
    return b'{"CMD":"X","msgID":1' + b"0" * 4400 + b"}"


#: Bytes one nesting level costs in the payload below: `{"a":` plus its `}`.
_LEVEL_BYTES = 6

#: Deepest frame that still fits under the framing cap, so a probe never
#: builds a payload the framer would have rejected anyway.
_DEEPEST_THAT_FITS = (framing.MAX_BUFFER_SIZE - 1) // _LEVEL_BYTES


def _nesting_payload(depth: int) -> bytes:
    return b'{"a":' * depth + b"1" + b"}" * depth


def _parses(depth: int) -> bool:
    try:
        json.loads(_nesting_payload(depth))
    except RecursionError:
        return False
    return True


@functools.lru_cache(maxsize=1)
def _first_depth_that_raises() -> int | None:
    """Smallest nesting depth this interpreter refuses, or None if none fits.

    Hard-coding this is a trap, and the tree fell into it: `9999` was one
    single level above the threshold on the interpreter it was written
    against. Measured, the first depth that raises is 995 on 3.11, 9998 on
    3.12, 9999 on 3.13 and 52119 on 3.14 - the json C scanner is bounded by
    the C stack, not by `sys.setrecursionlimit`, so the number moves with
    the interpreter version, its build flags and the thread stack size. A
    literal tuned to one of them sits one level from silently passing.

    None means no nested frame under `MAX_BUFFER_SIZE` can reach the
    decoder at all: on 3.14 it takes ~305 KiB of frame, so the framing cap
    provably stops it first and this failure mode is unreachable there.
    """
    if _parses(_DEEPEST_THAT_FITS):
        return None
    lo, hi = 1, _DEEPEST_THAT_FITS
    while lo < hi:
        mid = (lo + hi) // 2
        if _parses(mid):
            lo = mid + 1
        else:
            hi = mid
    return lo


#: Whether a `RecursionError` from `json.loads` is reachable through the
#: framing layer on this interpreter. False on 3.14; see above.
NESTING_REACHES_THE_DECODER = _first_depth_that_raises() is not None

#: Skips the one parametrized case that needs a real over-deep frame. The
#: `except RecursionError` clause itself stays pinned on every interpreter
#: by the monkeypatched twin tests, which do not depend on this.
requires_reachable_nesting = pytest.mark.skipif(
    not NESTING_REACHES_THE_DECODER,
    reason=(
        "this interpreter's json.loads needs a frame larger than "
        "framing.MAX_BUFFER_SIZE to raise RecursionError, so the framing "
        "cap stops it before the decoder sees it"
    ),
)


def nested_frame() -> bytes:
    """A frame nested deeply enough that `json.loads` raises RecursionError.

    Falls back to the deepest frame that fits when the interpreter cannot
    be pushed that far, so collection still works; the tests that need it
    to actually raise carry `requires_reachable_nesting`.
    """
    return _nesting_payload(_first_depth_that_raises() or _DEEPEST_THAT_FITS)


# ============================================================================
# Golden wire payloads
# ============================================================================

#: The schedule numbered 3 that gates the inside sensor 06:30-22:15 on
#: Sun/Tue/Thu/Sat, as the **library** puts it on the wire - i.e. the
#: ``SET_SCHEDULE`` payload ``powerpetdoor.door.Schedule.to_dict`` builds
#: and sends to the device.
#:
#: ``enabled``, ``inside`` and ``outside`` are JSON booleans here and the
#: integers ``1``/``0`` in :data:`GOLDEN_SCHEDULE_WIRE_FROM_DEVICE`, and
#: that difference is deliberate. **These two emitters are opposite
#: directions, not twins**: the library's is client->device (what we send
#: to firmware that has accepted JSON booleans since v0.1.0) and the
#: simulator's is device->client (**verified against firmware 1.7.18**: a
#: GET_SCHEDULE reply carries those three as ints). These two payloads must
#: not be "unified". Every other field is identical and pinned on both
#: sides, so a divergence anywhere else still fails immediately.
GOLDEN_SCHEDULE_WIRE_TO_DEVICE = {
    "index": 3,
    "enabled": True,
    "daysOfWeek": [1, 0, 1, 0, 1, 0, 1],
    "inside": True,
    "outside": False,
    "in_start_time": {"hour": 6, "min": 30},
    "in_end_time": {"hour": 22, "min": 15},
    "out_start_time": {"hour": 0, "min": 0},
    "out_end_time": {"hour": 0, "min": 0},
}

#: The same schedule as the **simulator** emits it, i.e. the device->client
#: ``GET_SCHEDULE`` reply. Identical to
#: :data:`GOLDEN_SCHEDULE_WIRE_TO_DEVICE` except for the three flag
#: fields; see that constant for why the two directions differ.
GOLDEN_SCHEDULE_WIRE_FROM_DEVICE = {
    **GOLDEN_SCHEDULE_WIRE_TO_DEVICE,
    "enabled": 1,
    "inside": 1,
    "outside": 0,
}


def _is_wire_int(value: object) -> bool:
    """True for a JSON integer - and not for a bool, which is an int subclass."""
    return isinstance(value, int) and not isinstance(value, bool)


def assert_schedule_wire_types(payload: dict, *, flag_type: type) -> None:
    """Assert an emitted schedule's field types, per protocol direction.

    Dict equality alone is not enough: ``True == 1`` in Python, so a
    ``daysOfWeek`` of ``[True, ...]`` compares equal to ``[1, ...]`` and an
    ``inside`` of ``1`` compares equal to ``True``. The types have to be
    asserted explicitly for the golden payload to mean anything.

    Args:
        payload: The emitted schedule dict.
        flag_type: ``bool`` for the library's client->device emitter,
            ``int`` for the simulator's device->client emitter (**verified
            against firmware 1.7.18**). The two directions are not required
            to agree (see :data:`GOLDEN_SCHEDULE_WIRE_TO_DEVICE`), so the
            expected spelling is passed in rather than assumed. It governs
            exactly the three flag fields.
    """
    assert set(payload) == set(GOLDEN_SCHEDULE_WIRE_TO_DEVICE)
    assert _is_wire_int(payload["index"])
    for name in ("enabled", "inside", "outside"):
        value = payload[name]
        if flag_type is bool:
            assert value is True or value is False
        else:
            assert _is_wire_int(value)
            assert value in (0, 1)
    days = payload["daysOfWeek"]
    assert isinstance(days, list)
    assert len(days) == 7
    assert all(_is_wire_int(day) and day in (0, 1) for day in days)
    for key in ("in_start_time", "in_end_time", "out_start_time", "out_end_time"):
        block = payload[key]
        assert isinstance(block, dict)
        assert set(block) == {"hour", "min"}
        assert _is_wire_int(block["hour"])
        assert 0 <= block["hour"] <= 23
        assert _is_wire_int(block["min"])
        assert 0 <= block["min"] <= 59
