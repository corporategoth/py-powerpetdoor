# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Pytest configuration and fixtures for Power Pet Door tests."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from powerpetdoor import PowerPetDoorClient
from powerpetdoor.const import (
    PING,
    PONG,
    FIELD_SUCCESS,
)


# ============================================================================
# Mock Transport and Protocol
# ============================================================================

class MockTransport:
    """Mock asyncio transport for network simulation."""

    def __init__(self):
        self.written_data: list[bytes] = []
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

    def get_written_messages(self) -> list[dict]:
        """Parse and return all written JSON messages."""
        messages = []
        for data in self.written_data:
            try:
                messages.append(json.loads(data.decode('ascii')))
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
        self._auto_respond = True
        self._response_delay = 0.0

    async def send_response(self, response: dict) -> None:
        """Simulate device sending a response."""
        if self._response_delay > 0:
            await asyncio.sleep(self._response_delay)
        json_data = json.dumps(response).encode('ascii')
        self.client.data_received(json_data)

    def send_response_sync(self, response: dict) -> None:
        """Synchronously send a response (for non-async contexts)."""
        json_data = json.dumps(response).encode('ascii')
        self.client.data_received(json_data)

    def respond_to_ping(self, msg_id: int, ping_value: str) -> None:
        """Send PONG response to a PING."""
        self.send_response_sync({
            FIELD_SUCCESS: "true",
            "CMD": PONG,
            PONG: ping_value,
            "msgId": msg_id
        })

    def respond_success(self, msg_id: int, cmd: str, **extra) -> None:
        """Send a generic success response."""
        response = {
            FIELD_SUCCESS: "true",
            "CMD": cmd,
            "msgId": msg_id,
            **extra
        }
        self.send_response_sync(response)

    def respond_failure(self, msg_id: int, cmd: str, error: str = "error") -> None:
        """Send a generic failure response."""
        self.send_response_sync({
            FIELD_SUCCESS: "false",
            "CMD": cmd,
            "msgId": msg_id,
            "error": error
        })


# ============================================================================
# Mock Device Responses
# ============================================================================

MOCK_DOOR_STATUS = {
    "door_status": "DOOR_CLOSED",
}

MOCK_SETTINGS = {
    "inside": True,
    "outside": True,
    "auto": False,
    "power": True,
}

MOCK_SENSORS = {
    "inside_active": True,
    "outside_active": True,
    "auto_active": False,
}

MOCK_DOOR_BATTERY = {
    "batteryPercent": 85,
    "isDischarging": False,
    "isCharging": True,
}

MOCK_HARDWARE = {
    "hwVersion": "1.0",
    "fwVersion": "2.5.0",
}

MOCK_SCHEDULE_LIST = [0, 1, 2]

MOCK_SCHEDULE_ENTRY = {
    "index": 0,
    "daysOfWeek": [1, 1, 1, 1, 1, 0, 0],  # Mon-Fri
    "inside": True,
    "outside": False,
    "enabled": True,
    "in_start_time": {"hour": 6, "min": 0},
    "in_end_time": {"hour": 20, "min": 0},
    "out_start_time": {"hour": 0, "min": 0},
    "out_end_time": {"hour": 0, "min": 0},
}


def create_mock_response(cmd: str, msg_id: int, **extra) -> dict:
    """Factory function to create mock device responses."""
    responses = {
        "DOOR_STATUS": {**MOCK_DOOR_STATUS, "CMD": "DOOR_STATUS"},
        "GET_SETTINGS": {**MOCK_SETTINGS, "CMD": "GET_SETTINGS"},
        "GET_SENSORS": {**MOCK_SENSORS, "CMD": "GET_SENSORS"},
        "DOOR_BATTERY": {**MOCK_DOOR_BATTERY, "CMD": "DOOR_BATTERY"},
        "GET_HW_INFO": {**MOCK_HARDWARE, "CMD": "GET_HW_INFO"},
        "GET_SCHEDULE_LIST": {"schedules": MOCK_SCHEDULE_LIST, "CMD": "GET_SCHEDULE_LIST"},
        "GET_SCHEDULE": {**MOCK_SCHEDULE_ENTRY, "CMD": "GET_SCHEDULE"},
    }

    base_response = responses.get(cmd, {"CMD": cmd})
    return {
        FIELD_SUCCESS: "true",
        "msgId": msg_id,
        **base_response,
        **extra
    }


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
async def mock_client(mock_transport, client_config) -> tuple[PowerPetDoorClient, MockTransport, MockDeviceProtocol]:
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
        loop=loop
    )

    # Simulate connection established
    client._transport = mock_transport
    client.connection_made(mock_transport)

    # Create device protocol helper
    device = MockDeviceProtocol(client)

    yield client, mock_transport, device

    # Cleanup: stop the client to cancel background tasks
    client.stop()

    # Cancel any remaining tasks created by this client
    if hasattr(client, '_keepalive') and client._keepalive and not client._keepalive.done():
        client._keepalive.cancel()
        try:
            await client._keepalive
        except asyncio.CancelledError:
            pass

    if hasattr(client, '_check_receipt') and client._check_receipt and not client._check_receipt.done():
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
        loop=loop
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
