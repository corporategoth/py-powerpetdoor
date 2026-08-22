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
    from powerpetdoor.simulator.scripting import set_extra_scripts_dir, set_script_paths_allowed

    set_extra_scripts_dir(None)
    # ctl.main() declares that this process may not run scripts by path;
    # that must not leak into a test expecting the simulator CLI's completer.
    set_script_paths_allowed(True)


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
