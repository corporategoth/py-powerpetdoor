# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Pytest configuration and fixtures for Power Pet Door tests."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from powerpetdoor import PowerPetDoorClient
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

    def __init__(self, write_buffer_size: int = 0):
        self.written_data: list[bytes] = []
        self.aborted = False
        self._closing = False
        self._closed = False
        #: Flow-control calls the bounded frame dispatcher makes.
        self.reading_paused = False
        self.pause_calls = 0
        self.resume_calls = 0
        self._write_buffer_size = write_buffer_size

    def write(self, data: bytes) -> None:
        """Record written data."""
        self.written_data.append(data)

    def pause_reading(self) -> None:
        """Record backpressure applied by :class:`FrameDispatcher`."""
        self.reading_paused = True
        self.pause_calls += 1

    def resume_reading(self) -> None:
        """Record backpressure released by :class:`FrameDispatcher`."""
        self.reading_paused = False
        self.resume_calls += 1

    def get_write_buffer_size(self) -> int:
        """Bytes queued for the peer but not yet sent."""
        return self._write_buffer_size

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
# both `_dispatch_frame` implementations (round-8 backend M1 / security
# M1). Both are brace-balanced and under `MAX_BUFFER_SIZE`, so the framing
# cap is provably not what stops them - the decoder is.
#
# They live here rather than in one test module because the client and the
# simulator protocol are twins on this path and must be pinned together.


def bigint_frame() -> bytes:
    """A frame whose integer literal exceeds CPython's str->int digit cap.

    `sys.get_int_max_str_digits()` is 4300 by default and the json scanner
    surfaces the refusal as a bare ValueError, not a JSONDecodeError.
    """
    return b'{"CMD":"X","msgID":1' + b"0" * 4400 + b"}"


def nested_frame(depth: int = 9999) -> bytes:
    """A frame nested deeply enough that `json.loads` raises RecursionError."""
    return b'{"a":' * depth + b"1" + b"}" * depth


# ============================================================================
# Golden wire payloads (backend M1)
# ============================================================================

#: The schedule numbered 3 that gates the inside sensor 06:30-22:15 on
#: Sun/Tue/Thu/Sat, as the **library** puts it on the wire - i.e. the
#: ``SET_SCHEDULE`` payload ``powerpetdoor.door.Schedule.to_dict`` builds
#: and sends to the device.
#:
#: ``enabled`` is a JSON boolean here and the string ``"1"`` in
#: :data:`GOLDEN_SCHEDULE_WIRE_FROM_DEVICE`, and that difference is
#: deliberate. **These two emitters are opposite directions, not twins**:
#: the library's is client->device (what we send to firmware that has
#: accepted a JSON boolean since v0.1.0) and the simulator's is
#: device->client (what a door replies, where ``"1"`` is what was
#: observed). ``docs/protocol.md`` is reverse-engineered and is not
#: authority over what the firmware accepts, so a future round must not
#: "unify" these two payloads. Every field except ``enabled`` is identical
#: and pinned on both sides, so a divergence in any other field still
#: fails immediately.
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
#: :data:`GOLDEN_SCHEDULE_WIRE_TO_DEVICE` except for the ``enabled``
#: spelling; see that constant for why the two directions differ.
GOLDEN_SCHEDULE_WIRE_FROM_DEVICE = {**GOLDEN_SCHEDULE_WIRE_TO_DEVICE, "enabled": "1"}


def _is_wire_int(value: object) -> bool:
    """True for a JSON integer - and not for a bool, which is an int subclass."""
    return isinstance(value, int) and not isinstance(value, bool)


def assert_schedule_wire_types(payload: dict, *, enabled_type: type) -> None:
    """Assert an emitted schedule's field types, per protocol direction.

    Dict equality alone is not enough: ``True == 1`` in Python, so a
    ``daysOfWeek`` of ``[True, ...]`` compares equal to ``[1, ...]`` and an
    ``inside`` of ``1`` compares equal to ``True``. The types have to be
    asserted explicitly for the golden payload to mean anything.

    Args:
        payload: The emitted schedule dict.
        enabled_type: ``bool`` for the library's client->device emitter,
            ``str`` for the simulator's device->client emitter. The two
            directions are not required to agree (see
            :data:`GOLDEN_SCHEDULE_WIRE_TO_DEVICE`), so the expected
            spelling is passed in rather than assumed.
    """
    assert set(payload) == set(GOLDEN_SCHEDULE_WIRE_TO_DEVICE)
    assert _is_wire_int(payload["index"])
    assert isinstance(payload["enabled"], enabled_type)
    if enabled_type is str:
        assert payload["enabled"] in ("0", "1")
    else:
        assert payload["enabled"] is True or payload["enabled"] is False
    days = payload["daysOfWeek"]
    assert isinstance(days, list)
    assert len(days) == 7
    assert all(_is_wire_int(day) and day in (0, 1) for day in days)
    # JSON booleans on both sides, unlike enabled.
    assert isinstance(payload["inside"], bool)
    assert isinstance(payload["outside"], bool)
    for key in ("in_start_time", "in_end_time", "out_start_time", "out_end_time"):
        block = payload[key]
        assert isinstance(block, dict)
        assert set(block) == {"hour", "min"}
        assert _is_wire_int(block["hour"])
        assert 0 <= block["hour"] <= 23
        assert _is_wire_int(block["min"])
        assert 0 <= block["min"] <= 59
