# PowerPetDoorClient Low-Level Interface

The `PowerPetDoorClient` class provides low-level access to Power Pet Door devices over the network. It handles:

- **Connection management**: Automatic reconnection on disconnect
- **Message queuing**: Priority-based message queue for reliable delivery
- **Keepalive**: Automatic ping/pong to maintain connection
- **Callback system**: Event-driven notifications for device state changes

> **Note**: For most use cases, consider using the higher-level [`PowerPetDoor`](door.md) class instead, which provides a simpler, more Pythonic interface with cached state.

## Table of Contents

- [Quick Start](#quick-start)
- [Constructor](#constructor)
- [Connection Management](#connection-management)
- [Sending Messages](#sending-messages)
- [Message Types and Commands](#message-types-and-commands)
- [Listeners](#listeners)
- [Connection Handlers](#connection-handlers)
- [Utility Functions](#utility-functions)

## Quick Start

### Blocking Mode

For standalone scripts where the client manages its own event loop:

```python
from powerpetdoor import PowerPetDoorClient, COMMAND, CMD_OPEN, CMD_CLOSE

# Create client
client = PowerPetDoorClient(
    host="192.168.1.100",
    port=3000,
    keepalive=30.0,
    timeout=10.0,
    reconnect=5.0
)

# Add listeners
client.add_listener(
    name="my_app",
    door_status_update=lambda status: print(f"Door: {status}")
)

# Start - this blocks until stop() is called
client.start()
```

### Async Mode

For integration with an existing asyncio event loop:

```python
import asyncio
from powerpetdoor import PowerPetDoorClient, CONFIG, CMD_GET_SETTINGS

async def main():
    loop = asyncio.get_running_loop()

    client = PowerPetDoorClient(
        host="192.168.1.100",
        port=3000,
        keepalive=30.0,
        timeout=10.0,
        reconnect=5.0,
        loop=loop
    )

    # Connect
    await client.connect()

    # Wait for connection to establish
    for _ in range(50):  # 5 seconds max
        if client.available:
            break
        await asyncio.sleep(0.1)

    # Send command and wait for response
    settings = await client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
    print(f"Settings: {settings}")

    # Cleanup
    client.stop()

asyncio.run(main())
```

## Constructor

```python
client = PowerPetDoorClient(
    host="192.168.1.100",  # IP address or hostname
    port=3000,              # TCP port (default 3000)
    keepalive=30.0,         # Seconds between keepalive pings (0 to disable)
    timeout=10.0,           # Response timeout in seconds
    reconnect=5.0,          # Reconnect delay in seconds
    loop=None,              # Optional: existing asyncio event loop
)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `host` | `str` | IP address or hostname of the Power Pet Door |
| `port` | `int` | TCP port number (typically 3000) |
| `keepalive` | `float` | Seconds between keepalive pings (0 to disable) |
| `timeout` | `float` | Seconds to wait for responses |
| `reconnect` | `float` | Seconds to wait before reconnecting after disconnect |
| `loop` | `AbstractEventLoop` | Optional asyncio event loop (creates one if not provided) |

## Connection Management

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `available` | `bool` | Whether connected and ready to send messages |
| `host` | `str` | Configured host address |
| `port` | `int` | Configured port number |

### Methods

```python
# Start client (blocks if no event loop provided)
client.start()

# Stop client and close connection
client.stop()

# Async connect (for use with existing event loop)
await client.connect()

# Close connection without stopping event loop
client.disconnect()
```

## Sending Messages

```python
# Fire-and-forget (no response)
client.send_message(COMMAND, CMD_OPEN)

# Wait for response
future = client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
result = await future

# With additional parameters
client.send_message(CONFIG, CMD_SET_HOLD_TIME, notify=True, holdTime=1500)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `type` | `str` | Message type: `COMMAND`, `CONFIG`, or `PING` |
| `arg` | `str` | Command or config name (e.g., `CMD_OPEN`) |
| `notify` | `bool` | If True, returns a Future that resolves with response |
| `**kwargs` | | Additional parameters to include in the message |

### Return Value

- If `notify=False`: Returns `None`
- If `notify=True`: Returns an `asyncio.Future` that resolves with the response

## Message Types and Commands

Import constants from the package:

```python
from powerpetdoor import (
    # Message types
    COMMAND,    # For actions (open, close, enable/disable)
    CONFIG,     # For configuration queries and updates
    PING,       # For keepalive (used internally)

    # Door control commands (use with COMMAND)
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_CLOSE,

    # Sensor commands (use with COMMAND)
    CMD_ENABLE_INSIDE,
    CMD_DISABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE,

    # Power commands (use with COMMAND)
    CMD_POWER_ON,
    CMD_POWER_OFF,

    # Auto/timer commands (use with COMMAND)
    CMD_ENABLE_AUTO,
    CMD_DISABLE_AUTO,

    # Query commands (use with CONFIG)
    CMD_GET_SETTINGS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_SENSORS,
    CMD_GET_POWER,
    CMD_GET_AUTO,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_HW_INFO,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_HOLD_TIME,
    CMD_GET_TIMEZONE,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SCHEDULE,

    # Configuration commands (use with CONFIG)
    CMD_SET_HOLD_TIME,
    CMD_SET_TIMEZONE,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_DELETE_SCHEDULE,

    # Safety commands (use with COMMAND)
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_ENABLE_AUTORETRACT,
    CMD_DISABLE_AUTORETRACT,
    CMD_ENABLE_CMD_LOCKOUT,
    CMD_DISABLE_CMD_LOCKOUT,
)
```

### Common Command Patterns

```python
# Open door (auto-closes after hold time)
client.send_message(COMMAND, CMD_OPEN)

# Open door and keep open
client.send_message(COMMAND, CMD_OPEN_AND_HOLD)

# Close door
client.send_message(COMMAND, CMD_CLOSE)

# Get current door status
status = await client.send_message(CONFIG, CMD_GET_DOOR_STATUS, notify=True)

# Get all settings
settings = await client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)

# Set hold time (in centiseconds)
await client.send_message(CONFIG, CMD_SET_HOLD_TIME, notify=True, holdTime=1500)

# Set timezone
await client.send_message(CONFIG, CMD_SET_TIMEZONE, notify=True, tz="EST5EDT,M3.2.0,M11.1.0")

# Enable/disable sensors
client.send_message(COMMAND, CMD_ENABLE_INSIDE)
client.send_message(COMMAND, CMD_DISABLE_OUTSIDE)

# Power control
client.send_message(COMMAND, CMD_POWER_ON)
client.send_message(COMMAND, CMD_POWER_OFF)
```

## Listeners

Register callbacks to receive device state updates:

```python
client.add_listener(
    name="my_app",  # Unique identifier for this listener set

    # Door status updates
    door_status_update=lambda status: print(f"Door: {status}"),

    # Full settings dict
    settings_update=lambda settings: print(f"Settings: {settings}"),

    # Individual sensor updates (by field or "*" for all)
    sensor_update={
        FIELD_POWER: lambda val: print(f"Power: {val}"),
        FIELD_INSIDE: lambda val: print(f"Inside: {val}"),
        FIELD_OUTSIDE: lambda val: print(f"Outside: {val}"),
        FIELD_AUTO: lambda val: print(f"Auto: {val}"),
    },
    # Or use "*" for all sensor fields:
    # sensor_update={"*": lambda val: print(f"Sensor: {val}")},

    # Notification settings updates
    notifications_update={
        FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: lambda val: print(f"Inside on: {val}"),
        FIELD_LOW_BATTERY_NOTIFICATIONS: lambda val: print(f"Low battery: {val}"),
    },

    # Statistics updates
    stats_update={
        FIELD_TOTAL_OPEN_CYCLES: lambda val: print(f"Cycles: {val}"),
        FIELD_TOTAL_AUTO_RETRACTS: lambda val: print(f"Retracts: {val}"),
    },

    # Other updates
    hw_info_update=lambda info: print(f"HW Info: {info}"),
    battery_update=lambda data: print(f"Battery: {data}"),
    timezone_update=lambda tz: print(f"Timezone: {tz}"),
    hold_time_update=lambda time: print(f"Hold time: {time}"),
)
```

### Removing Listeners

```python
client.del_listener("my_app")
```

### Available Listener Types

| Listener | Callback Signature | Description |
|----------|-------------------|-------------|
| `door_status_update` | `(status: str)` | Door state changes |
| `settings_update` | `(settings: dict)` | Full settings dict |
| `sensor_update` | `{field: (val: bool)}` | Sensor state changes |
| `notifications_update` | `{field: (val: bool)}` | Notification setting changes |
| `stats_update` | `{field: (val: int)}` | Statistics updates |
| `hw_info_update` | `(info: dict)` | Hardware info |
| `battery_update` | `(data: dict)` | Battery status |
| `timezone_update` | `(tz: str)` | Timezone string |
| `hold_time_update` | `(time: int)` | Hold time in centiseconds |
| `sensor_trigger_voltage_update` | `(voltage: int)` | Sensor trigger voltage |
| `sleep_sensor_trigger_voltage_update` | `(voltage: int)` | Sleep sensor voltage |
| `remote_id_update` | `(has_id: bool)` | Remote ID presence |
| `remote_key_update` | `(has_key: bool)` | Remote key presence |
| `reset_reason_update` | `(reason: str)` | Last reset reason |

## Connection Handlers

Register callbacks for connection lifecycle events:

```python
client.add_handlers(
    name="my_app",
    on_connect=lambda: print("Connected!"),
    on_disconnect=lambda: print("Disconnected!"),
    on_ping=lambda latency_ms: print(f"Ping: {latency_ms}ms"),
)

# Remove handlers
client.del_handlers("my_app")
```

| Handler | Signature | Description |
|---------|-----------|-------------|
| `on_connect` | `() -> None` or `async () -> None` | Called when connection is established |
| `on_disconnect` | `() -> None` or `async () -> None` | Called when connection is lost |
| `on_ping` | `(latency_ms: int) -> None` | Called with round-trip latency after successful ping |

## Utility Functions

### find_end

Find the end of a JSON object in a string:

```python
from powerpetdoor import find_end

data = '{"foo": "bar"}{"next": "object"}'
end = find_end(data)  # Returns 14
first_json = data[:end]  # '{"foo": "bar"}'
```

### make_bool

Convert various types to boolean:

```python
from powerpetdoor import make_bool

make_bool("1")      # True
make_bool("true")   # True
make_bool("yes")    # True
make_bool("on")     # True
make_bool("0")      # False
make_bool("false")  # False
make_bool(1)        # True
make_bool(0)        # False
```

### PrioritizedMessage

For advanced queue manipulation (rarely needed):

```python
from powerpetdoor import PrioritizedMessage

msg = PrioritizedMessage(
    priority=1,    # Lower = higher priority
    sequence=0,    # For FIFO ordering within same priority
    data={"cmd": "OPEN"}
)
```

## Message Priority

Messages are automatically prioritized:

| Priority | Message Types |
|----------|--------------|
| Critical (0) | PING/PONG keepalives |
| High (1) | Door control (OPEN, CLOSE) |
| Medium (2) | Status queries |
| Low (3) | Configuration changes |

This ensures keepalives and urgent door commands are processed before routine queries.

## Default Timing

The client uses these default timing values:

| Parameter | Default | Description |
|-----------|---------|-------------|
| Min message interval | 200ms | Delay between messages to avoid overwhelming device |
| Keepalive interval | 30s | PING/PONG frequency |
| Response timeout | 10s | Max wait for command response |
| Reconnect delay | 5s | Wait before reconnecting after disconnect |
