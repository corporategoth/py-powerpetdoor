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

For integration with an existing asyncio event loop (the client latches
onto the running loop automatically - passing `loop` is optional):

```python
import asyncio
from powerpetdoor import PowerPetDoorClient, CONFIG, CMD_GET_SETTINGS

async def main():
    client = PowerPetDoorClient(
        host="192.168.1.100",
        port=3000,
        keepalive=30.0,
        timeout=10.0,
        reconnect=5.0,
    )

    # Connect. On success client.available is True when this returns; on
    # failure the client logs the error and schedules reconnect attempts
    # in the background (it does not raise).
    await client.connect()

    if client.available:
        # Send command and wait for response
        settings = await client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
        print(f"Settings: {settings}")

    # Cleanup: stops reconnection and closes the connection
    client.shutdown()

asyncio.run(main())
```

> **Tip**: The higher-level [`PowerPetDoor`](door.md) class wraps this
> with `await door.connect()` that *does* raise `ConnectionError` on
> failure.

## Constructor

All parameters except `loop` are required (the higher-level `PowerPetDoor`
class provides defaults for them):

```python
client = PowerPetDoorClient(
    host="192.168.1.100",  # IP address or hostname
    port=3000,              # TCP port (the door listens on 3000)
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
| `reconnect` | `float` | Base delay before reconnecting after a disconnect (see [Reconnect Backoff](#reconnect-backoff)) |
| `loop` | `AbstractEventLoop` | Optional asyncio event loop. If omitted, the client latches onto the running loop at `connect()`/`start()` time; the blocking `start()` path creates a private loop only when no loop is running |

> **Thread safety**: All methods except `stop()` and
> `run_coroutine_threadsafe()` must be called from the event loop
> thread. To drive the client from another thread, wrap the call in a
> coroutine and submit it via `client.run_coroutine_threadsafe(...)`.

## Connection Management

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `available` | `bool` | Whether connected and ready to send messages |
| `host` | `str` | Configured host address |
| `port` | `int` | Configured port number |

### Methods

```python
# Start client (blocks if no event loop is provided or running)
client.start()

# Stop client and close connection (thread-safe; also stops a private loop)
client.stop()

# Async connect (for use with an existing event loop).
# Does not raise on failure: the error is logged and reconnect attempts
# are scheduled in the background. No-op after shutdown()/stop().
await client.connect()

# Shut down: cancel pending reconnects and close the connection.
# Idempotent; must be called from the event loop thread.
client.shutdown()

# Re-enable a shut-down client so connect() works again
client.reset_shutdown()

# Close the connection WITHOUT preventing an in-progress reconnect cycle
# from being scheduled by a later connection loss (prefer shutdown())
client.disconnect()
```

### Lifecycle Semantics

- `connect()` returns once the TCP connection is established
  (`client.available` is `True`) or the attempt failed (a background
  reconnect is then scheduled). It only raises `asyncio.CancelledError`.
- `connect()` is idempotent: while already connected, or while another
  `connect()` is still in flight, it logs a warning and returns. A second
  TCP connection is never opened - the device has a single connection slot,
  and a leaked socket would lock everyone else out until the device drops it.
- On connection loss the client automatically reconnects (see below)
  unless `shutdown()`/`stop()` was called.
- `shutdown()`/`stop()` cancel any pending reconnect attempt - the
  client never reconnects after shutdown.
- `disconnect()` fails all outstanding `notify=True` futures with
  `ConnectionError` and fires `on_disconnect` handlers only if a
  connection actually existed (failed connection attempts do not
  produce disconnect events).
- After `shutdown()`, call `reset_shutdown()` (or `start()`) before
  connecting again.

### Reconnect Backoff

The `reconnect` constructor argument is the *base* delay. On each
consecutive failed attempt the delay doubles, up to a cap of 300
seconds, with up to 25% random jitter added so several clients do not
retry in lockstep against the single-connection device. A successful
connection resets the backoff to the base delay.

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

### Response Errors

A `notify=True` future never hangs forever - it always completes with
one of:

- **The response payload** on success (for commands with no specialized
  response parser, e.g. `CMD_OPEN`, the raw acknowledgment message dict).
- **`CommandError`** when the device reports failure (`success:
  "false"`) or the response payload was malformed/missing its expected
  field. `CommandError` carries `cmd` and `reason` attributes (the
  device's `reason` field, when provided).
- **`TimeoutError`** when the message was dropped after exhausting
  retries (no response from the device).
- **`ConnectionError`** when the connection closed before a response
  arrived.

```python
from powerpetdoor import CommandError, CONFIG, CMD_GET_SCHEDULE

try:
    schedule = await client.send_message(CONFIG, CMD_GET_SCHEDULE, notify=True, index=99)
except CommandError as err:
    print(f"Device rejected {err.cmd}: {err.reason}")
except TimeoutError:
    print("Device did not respond")
except ConnectionError:
    print("Connection lost")
```

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

Register callbacks to receive device state updates.

The dict-based listeners (`sensor_update`, `notifications_update`,
`stats_update`) are keyed by field name (or `"*"` for all fields of that
group) and their callbacks receive two arguments: `(field, value)`. The
`field` argument identifies which field changed, which is what makes the
`"*"` wildcard useful. All other listeners receive a single value.

```python
from powerpetdoor import (
    FIELD_AUTO,
    FIELD_INSIDE,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_POWER,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
)

client.add_listener(
    name="my_app",  # Unique identifier for this listener set

    # Door status updates
    door_status_update=lambda status: print(f"Door: {status}"),

    # Full settings dict
    settings_update=lambda settings: print(f"Settings: {settings}"),

    # Individual sensor updates (by field or "*" for all)
    sensor_update={
        FIELD_POWER: lambda field, val: print(f"Power: {val}"),
        FIELD_INSIDE: lambda field, val: print(f"Inside: {val}"),
        FIELD_OUTSIDE: lambda field, val: print(f"Outside: {val}"),
        FIELD_AUTO: lambda field, val: print(f"Auto: {val}"),
    },
    # Or use "*" for all sensor fields:
    # sensor_update={"*": lambda field, val: print(f"{field}: {val}")},

    # Notification settings updates
    notifications_update={
        FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: lambda field, val: print(f"Inside on: {val}"),
        FIELD_LOW_BATTERY_NOTIFICATIONS: lambda field, val: print(f"Low battery: {val}"),
    },

    # Statistics updates
    stats_update={
        FIELD_TOTAL_OPEN_CYCLES: lambda field, val: print(f"Cycles: {val}"),
        FIELD_TOTAL_AUTO_RETRACTS: lambda field, val: print(f"Retracts: {val}"),
    },

    # Other updates
    hw_info_update=lambda info: print(f"HW Info: {info}"),
    battery_update=lambda data: print(f"Battery: {data}"),
    timezone_update=lambda tz: print(f"Timezone: {tz}"),
    hold_time_update=lambda time: print(f"Hold time: {time}"),
)
```

The `sensor_update` fields are `FIELD_POWER`, `FIELD_INSIDE`, `FIELD_OUTSIDE`,
`FIELD_AUTO`, `FIELD_OUTSIDE_SENSOR_SAFETY_LOCK`, `FIELD_CMD_LOCKOUT`, and
`FIELD_AUTORETRACT` (all exported from the package root).

### Notification Events

The device announces sensor and battery events on its own initiative
(outside the command/response flow). Register a `notification_event`
listener to receive them:

```python
from powerpetdoor import (
    NOTIFY_LOW_BATTERY,
    NOTIFY_SENSOR_INDOOR,
    NOTIFY_SENSOR_OUTDOOR,
    SENSOR_STATE_ON,
)

def on_event(event: str, state: str | None) -> None:
    if event == NOTIFY_SENSOR_INDOOR and state == SENSOR_STATE_ON:
        print("Pet detected at the indoor sensor!")
    elif event == NOTIFY_LOW_BATTERY:
        print("Battery is low")

client.add_listener("my_app", notification_event=on_event)
```

The callback receives `(event, state)`:

- `event` is one of `NOTIFY_SENSOR_INDOOR`, `NOTIFY_SENSOR_OUTDOOR`, or
  `NOTIFY_LOW_BATTERY`.
- `state` is the reported `sensorState` (`SENSOR_STATE_ON`/`SENSOR_STATE_OFF`,
  i.e. `"on"`/`"off"`), or `None` when the event carries no state
  (`NOTIFY_LOW_BATTERY`).

Both the documented bare envelope (`{"SENSOR_INDOOR": "", "sensorState":
"on"}`) and CMD-style variants are dispatched to the same listeners.

### Removing Listeners

```python
client.del_listener("my_app")
```

### Available Listener Types

| Listener | Callback Signature | Description |
|----------|-------------------|-------------|
| `door_status_update` | `(status: str)` | Door state changes |
| `settings_update` | `(settings: dict)` | Full settings dict |
| `sensor_update` | `{field: (field: str, val: bool)}` | Sensor state changes |
| `notifications_update` | `{field: (field: str, val: bool)}` | Notification setting changes |
| `stats_update` | `{field: (field: str, val: int)}` | Statistics updates |
| `hw_info_update` | `(info: dict)` | Hardware info |
| `battery_update` | `(data: dict)` | Battery status |
| `timezone_update` | `(tz: str)` | Timezone string |
| `hold_time_update` | `(time: int)` | Hold time in centiseconds |
| `sensor_trigger_voltage_update` | `(voltage: int)` | Sensor trigger voltage |
| `sleep_sensor_trigger_voltage_update` | `(voltage: int)` | Sleep sensor voltage |
| `remote_id_update` | `(has_id: bool)` | Remote ID presence |
| `remote_key_update` | `(has_key: bool)` | Remote key presence |
| `reset_reason_update` | `(reason: str)` | Last reset reason |
| `schedule_update` | `(schedule: dict)` | Schedule created or updated |
| `schedule_delete` | `(index: int)` | Schedule deleted |
| `notification_event` | `(event: str, state: str \| None)` | Device-initiated sensor/battery event |

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
| High (1) | Door control (OPEN, OPEN_AND_HOLD, CLOSE) |
| Medium (2) | Settings changes (enable/disable, power, SET_*) |
| Low (3) | Status queries (GET_*) and schedule commands |

This ensures keepalives and urgent door commands are processed before routine queries.

## Timing

| Parameter | Value | Description |
|-----------|-------|-------------|
| Min message interval | 200ms (fixed) | Delay between messages to avoid overwhelming the device |
| Keepalive interval | constructor `keepalive` | PING/PONG frequency (`PowerPetDoor` defaults to 30s) |
| Response timeout | constructor `timeout` | Max wait for a command response (`PowerPetDoor` defaults to 10s) |
| Reconnect delay | constructor `reconnect` | Base delay before reconnecting (`PowerPetDoor` defaults to 5s); doubles per failed attempt with jitter, capped at 300s |
