# PowerPetDoor High-Level Interface

The `PowerPetDoor` class provides a high-level, Pythonic interface to your Power Pet Door. It wraps the low-level `PowerPetDoorClient` and provides:

- **Cached state**: Properties return cached values, updated automatically via callbacks
- **Type-safe enums**: Door states are represented as `DoorStatus` enum values
- **Simple async methods**: Control the door with intuitive methods like `open()`, `close()`, `set_power(True)`
- **Automatic state sync**: State is kept in sync with the actual door via the underlying client

## Table of Contents

- [Quick Start](#quick-start)
- [Installation](#installation)
- [Connection](#connection)
- [Behaviour While Disconnected](#behaviour-while-disconnected)
- [Door Control](#door-control)
- [Door Status](#door-status)
- [Sensors](#sensors)
- [Power Control](#power-control)
- [Scheduling](#scheduling)
- [Safety Features](#safety-features)
- [Configuration](#configuration)
- [Battery & Hardware](#battery--hardware)
- [Notifications](#notifications)
- [Schedules](#schedules)
- [Callbacks](#callbacks)
- [Refreshing State](#refreshing-state)
- [Supporting Types](#supporting-types)

## Quick Start

```python
import asyncio
from powerpetdoor import PowerPetDoor, DoorStatus

async def main():
    # Create and connect
    door = PowerPetDoor("192.168.1.100")
    await door.connect()

    # Read state via properties
    print(f"Door status: {door.status.name}")
    print(f"Battery: {door.battery_percent}%")
    print(f"Inside sensor: {'enabled' if door.inside_sensor else 'disabled'}")

    # Control via async methods
    if door.is_closed:
        await door.open()

    await door.set_hold_time(15)  # 15 seconds
    await door.set_inside_sensor(True)

    # Register callbacks
    door.on_status_change(lambda s: print(f"Status changed to: {s.name}"))

    # Disconnect when done
    await door.disconnect()

asyncio.run(main())
```

## Installation

```bash
pip install pypowerpetdoor
```

## Connection

### Constructor

```python
door = PowerPetDoor(
    host="192.168.1.100",  # Required: IP address or hostname
    port=3000,              # Optional: TCP port (default 3000)
    keepalive=30.0,         # Optional: Seconds between keepalive pings
    timeout=10.0,           # Optional: Response timeout in seconds
    reconnect=5.0,          # Optional: Reconnect delay in seconds
    loop=None,              # Optional: Event loop (uses current if None)
)
```

### Connection Methods

```python
await door.connect()      # Connect and fetch initial state
await door.disconnect()   # Disconnect from the door
```

`connect()` waits (event-driven, no polling) for the connection and
performs an initial state refresh, so cached properties are valid when
it returns. It accepts an optional `timeout` keyword (defaults to
`default_timeout`) and **raises `ConnectionError`** if the connection
cannot be established in time - in that case the underlying client is
fully shut down first (no reconnect attempts keep running in the
background), so `connect()` may safely be retried.

`connect()` is idempotent: called while already connected it logs a
warning and returns without touching the connection. The device accepts a
single connection, so a defensive re-connect must never open a second
socket - that would orphan the live one and hog the device's only slot.

`disconnect()` stops automatic reconnection and closes the connection.
It is idempotent: calling it twice, or before `connect()`, is safe.
After `disconnect()`, calling `connect()` again re-arms the client.

While connected, the underlying client automatically reconnects if the
connection drops (with exponential backoff and jitter - see
[client.md](client.md#reconnect-backoff)). After every automatic
reconnect the door schedules a full `refresh()` so cached state never
silently serves stale pre-disconnect values; `on_connect`/`on_disconnect`
callbacks fire around each transition.

### Connection Properties

| Property | Type | Description |
|----------|------|-------------|
| `connected` | `bool` | Whether currently connected to the door |
| `host` | `str` | The door's IP address or hostname |
| `port` | `int` | The door's TCP port |
| `latency` | `float \| None` | Ping round-trip latency in seconds (`None` before the first ping) |

## Behaviour While Disconnected

> **A command issued while disconnected is queued, not refused, and it
> executes on the next connection.** This is a physical door: it can open
> minutes after the call that asked for it, unattended.

The fire-and-forget methods - `open()`, `open_and_hold()`, `close()`,
`toggle()`, `cycle()` - send with `notify=False`. With no transport there
is nothing to write to, so the message sits in the client's priority queue
until a connection appears, and the client flushes that queue as soon as
one does. Measured against a running door emulation:

```
connected: True | door_status: DOOR_CLOSED
device went away -> door.connected = False
open_and_hold() during the reconnect window -> returned in 0.000s, no error
device back at t+1.0s; reconnect completes
t+4.0s after the button press: connected=True  door_status=DOOR_KEEPUP
```

Four seconds after a call that reported nothing, the door was open and
latched. Nothing is lost and nothing raises; the request is simply
deferred.

**This is deliberate.** It is what makes a command survive a transient
network drop rather than vanishing, and it is implemented in
`PowerPetDoorClient` (which behaves identically if you use it directly) -
not in this facade. An operator UI built on this library must decide which
semantics it wants:

```python
# "Refuse when offline" - check first; the facade will not do it for you.
if not door.connected:
    raise ConnectionError(f"not connected to {door.host}:{door.port}")
await door.open()
```

The **awaited** methods (every setter, and the `refresh_*` family) behave
the same way underneath, but they wait: the request is queued, no response
arrives, and after `default_timeout` seconds they raise `TimeoutError`
with a message that says so:

```
TimeoutError: SET_HOLD_TIME timed out after 20.0s waiting for 192.168.1.100:3000;
not connected - the command is queued and will be sent when the connection is
next established (call connect() first to avoid this)
```

The queued command is *still queued* after that `TimeoutError`; the
timeout bounds your wait, not the command's lifetime. Call `disconnect()`
to clear the queue and fail every outstanding future with
`ConnectionError`.

## Door Control

### Control Methods

```python
await door.open()           # Open door (auto-closes after hold time)
await door.open_and_hold()  # Open and keep open until manually closed
await door.close()          # Close the door
await door.toggle()         # Open if closed, close if open
await door.cycle()          # Full door cycle (same as open, auto-closes after hold time)
```

## Door Status

### Status Properties

| Property | Type | Description |
|----------|------|-------------|
| `status` | `DoorStatus` | Current door state (enum) |
| `is_open` | `bool` | True if door is open or opening |
| `is_closed` | `bool` | True if door is fully closed |
| `is_closing` | `bool` | True if door is currently closing |
| `position` | `int` | Position percentage (0=closed, 100=open) |

### DoorStatus Enum

```python
from powerpetdoor import DoorStatus

# Available states:
DoorStatus.IDLE            # Door idle
DoorStatus.CLOSED          # Door fully closed
DoorStatus.RISING          # Door opening
DoorStatus.SLOWING         # Door slowing near top
DoorStatus.HOLDING         # Door open, holding before auto-close
DoorStatus.KEEPUP          # Door locked open (open_and_hold)
DoorStatus.CLOSING_TOP_OPEN   # Door closing from top
DoorStatus.CLOSING_MID_OPEN   # Door closing from middle
DoorStatus.UNKNOWN         # Unrecognized status string from the device
```

`DoorStatus.UNKNOWN` is reported (with a warning logged) when the
device sends a status string this library does not recognize - for
example from a newer firmware. While the status is `UNKNOWN`, `is_open`,
`is_closed`, and `is_closing` are all `False` and `position` is `0`.

## Sensors

The door has inside and outside sensors that detect pet proximity.

### Sensor Properties

| Property | Type | Description |
|----------|------|-------------|
| `inside_sensor` | `bool` | Whether inside sensor is enabled |
| `outside_sensor` | `bool` | Whether outside sensor is enabled |

### Sensor Methods

```python
await door.set_inside_sensor(True)   # Enable inside sensor
await door.set_inside_sensor(False)  # Disable inside sensor
await door.set_outside_sensor(True)  # Enable outside sensor
await door.set_outside_sensor(False) # Disable outside sensor
```

## Power Control

### Power Properties

| Property | Type | Description |
|----------|------|-------------|
| `power` | `bool` | Whether door power is on |

### Power Methods

```python
await door.set_power(True)   # Turn door power on
await door.set_power(False)  # Turn door power off
```

## Scheduling

### Auto Mode Properties

| Property | Type | Description |
|----------|------|-------------|
| `auto` | `bool` | Whether automatic scheduling is enabled |

### Auto Mode Methods

```python
await door.set_auto(True)   # Enable automatic scheduling
await door.set_auto(False)  # Disable automatic scheduling
```

When auto mode is enabled, the door follows the configured schedules.

## Safety Features

### Safety Properties

| Property | Type | Description |
|----------|------|-------------|
| `safety_lock` | `bool` | Outside sensor safety lock enabled |
| `autoretract` | `bool` | Auto-retract on obstruction enabled |
| `pet_proximity_keep_open` | `bool` | Keep door open when pet is nearby |

### Safety Methods

```python
# Outside sensor safety lock
await door.set_safety_lock(True)
await door.set_safety_lock(False)

# Auto-retract on obstruction
await door.set_autoretract(True)
await door.set_autoretract(False)

# Pet proximity keep-open (inverse of command lockout)
await door.set_pet_proximity_keep_open(True)
await door.set_pet_proximity_keep_open(False)
```

## Configuration

### Hold Time

The hold time is how long the door stays open after a sensor trigger before auto-closing.

```python
# Get current hold time (seconds)
print(f"Hold time: {door.hold_time} seconds")

# Set hold time (in seconds)
await door.set_hold_time(15.0)
```

### Timezone

The device speaks **POSIX TZ strings**, not IANA names, and
`set_timezone()` performs no conversion:

```python
# Get current timezone (POSIX format)
print(f"Timezone: {door.timezone}")

# Set timezone (POSIX format)
await door.set_timezone("EST5EDT,M3.2.0,M11.1.0")
```

To go from a familiar `America/New_York` to the string the device wants,
use the exported timezone helpers. They read the IANA database (via
`tzdata`), which means the cache must be initialized first — the lookups
return `None` until it is:

```python
from powerpetdoor import (
    async_init_timezone_cache,
    find_iana_for_posix,
    get_available_timezones,
    get_posix_tz_string,
    is_cache_initialized,
    parse_posix_tz_string,
)

# Build the cache once, off the event loop thread (all I/O is in a thread).
await async_init_timezone_cache()
assert is_cache_initialized()

posix = get_posix_tz_string("America/New_York")   # 'EST5EDT,M3.2.0,M11.1.0'
await door.set_timezone(posix)

# ... and back again, for display. The reverse map is keyed by POSIX rule,
# so the name you get back is *a* zone with those rules, not necessarily
# the one you started from - but it always round-trips to the same string.
find_iana_for_posix(posix)                        # e.g. 'America/Detroit'
get_posix_tz_string(find_iana_for_posix(posix)) == posix

get_available_timezones()[:3]                     # every IANA name, sorted
parse_posix_tz_string(posix)["std_abbrev"]        # 'EST'
```

| Helper | Purpose |
|--------|---------|
| `async_init_timezone_cache()` | Build the cache without blocking the event loop (await once at startup) |
| `init_timezone_cache_sync()` | Blocking equivalent, for non-async callers |
| `is_cache_initialized()` | Whether the lookups below will work |
| `get_available_timezones()` | Sorted list of IANA zone names (a copy) |
| `get_posix_tz_string(iana)` | IANA name -> POSIX TZ string, or `None` |
| `find_iana_for_posix(posix)` | POSIX TZ string -> an IANA name, or `None` |
| `parse_posix_tz_string(posix)` | POSIX TZ string -> `{std_abbrev, dst_abbrev, ...}`, or `None` |

The simulator's own `timezone` command accepts either spelling because it
uses exactly these helpers.

## Battery & Hardware

### Battery Properties

| Property | Type | Description |
|----------|------|-------------|
| `battery_percent` | `int` | Battery percentage (0-100) |
| `battery_present` | `bool` | Whether a battery is installed |
| `ac_present` | `bool` | Whether AC power is connected |
| `battery` | `BatteryInfo` | Full battery info object |

The `BatteryInfo` object also provides computed properties:

```python
info = door.battery
print(f"Charging: {info.charging}")      # AC present and not full
print(f"Discharging: {info.discharging}") # No AC and battery present
```

### Hardware Properties

| Property | Type | Description |
|----------|------|-------------|
| `firmware_version` | `str` | Firmware version (e.g., "1.2.3") |
| `hardware_version` | `str` | Hardware version (e.g., "1 rev 2") |
| `hardware_info` | `dict` | Full hardware info dictionary |

### Statistics Properties

| Property | Type | Description |
|----------|------|-------------|
| `total_open_cycles` | `int` | Total door open cycles |
| `total_auto_retracts` | `int` | Total auto-retractions |

## Notifications

### Notification Properties

```python
settings = door.notifications
print(f"Inside on: {settings.inside_on}")
print(f"Inside off: {settings.inside_off}")
print(f"Outside on: {settings.outside_on}")
print(f"Outside off: {settings.outside_off}")
print(f"Low battery: {settings.low_battery}")
```

### Setting Notifications

Update specific notification settings (unspecified settings remain unchanged):

```python
await door.set_notifications(
    inside_on=True,
    low_battery=True,
)
```

## Schedules

Schedules control when sensors are active during automatic mode.

### Schedule Properties

| Property | Type | Description |
|----------|------|-------------|
| `schedules` | `list[Schedule]` | Current schedule list |

### Schedule Methods

```python
# Get all schedules
await door.refresh_schedules()
for schedule in door.schedules:
    print(f"Schedule {schedule.index}: enabled={schedule.enabled}")

# Get a specific schedule
schedule = await door.get_schedule(0)

# Create/update a schedule
from powerpetdoor import Schedule, ScheduleTime

# days_of_week is a list of booleans: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
schedule = Schedule(
    index=0,
    enabled=True,
    days_of_week=[True, True, True, True, True, True, True],  # All days
    inside=True,       # This schedule controls inside sensor
    outside=False,
    start=ScheduleTime(hour=6, minute=0),
    end=ScheduleTime(hour=22, minute=0),
)
await door.set_schedule(schedule)

# Delete a schedule
await door.delete_schedule(0)
```

## Callbacks

Register callbacks to be notified of changes:

```python
# Door status changes
def on_status(status: DoorStatus):
    print(f"Door is now: {status.name}")

door.on_status_change(on_status)

# Settings changes
def on_settings(settings: dict):
    print(f"Settings updated: {settings}")

door.on_settings_change(on_settings)

# Connection events
door.on_connect(lambda: print("Connected!"))
door.on_disconnect(lambda: print("Disconnected!"))

# Schedule changes (receives the updated list of Schedule objects whenever
# a schedule is added, updated, or deleted)
def on_schedules(schedules: list[Schedule]):
    print(f"Schedules updated: {len(schedules)} entries")

door.on_schedule_change(on_schedules)
```

## Refreshing State

While state is automatically kept in sync via callbacks, you can force a refresh:

```python
# Refresh all state
await door.refresh()

# Refresh specific aspects
status = await door.refresh_status()
await door.refresh_settings()
battery = await door.refresh_battery()
await door.refresh_stats()
hw_info = await door.refresh_hardware_info()
schedules = await door.refresh_schedules()
```

`refresh()` and `refresh_settings()` never raise for a single failed step:
each step is gathered independently and a failure is logged as
`Refresh step <name> failed: ...` (logger `powerpetdoor.door`). Properties
whose refresh failed keep their previous cached value, so a device NAK or a
drop during `connect()` leaves a partial cache rather than an exception -
check the log if a property looks stale.

## Supporting Types

### DoorStatus

Enum representing door operational states. See [Door Status](#door-status) section.

### NotificationSettings

```python
@dataclass
class NotificationSettings:
    inside_on: bool = False    # Notify when inside sensor triggers
    inside_off: bool = False   # Notify when inside sensor deactivates
    outside_on: bool = False   # Notify when outside sensor triggers
    outside_off: bool = False  # Notify when outside sensor deactivates
    low_battery: bool = False  # Notify on low battery
```

### BatteryInfo

```python
@dataclass
class BatteryInfo:
    percent: int = 100      # Battery percentage (0-100)
    present: bool = True    # Whether battery is installed
    ac_present: bool = True # Whether AC power is connected

    @property
    def charging(self) -> bool: ...     # AC present and not full

    @property
    def discharging(self) -> bool: ...  # No AC and battery present
```

### Schedule and ScheduleTime

```python
@dataclass
class ScheduleTime:
    hour: int = 0    # Hour (0-23)
    minute: int = 0  # Minute (0-59)

@dataclass
class Schedule:
    index: int = 0                       # Schedule slot (0-based)
    enabled: bool = True                 # Whether schedule is active
    # [Sun, Mon, Tue, Wed, Thu, Fri, Sat], defaults to all days
    days_of_week: list[bool] = [True] * 7
    inside: bool = False                 # Controls inside sensor
    outside: bool = False                # Controls outside sensor
    start: ScheduleTime = ScheduleTime(6, 0)   # Start time for sensor
    end: ScheduleTime = ScheduleTime(22, 0)    # End time for sensor
```

### Days of Week List

Each schedule entry controls ONE sensor (inside or outside) for specific days and a time window.

```python
# days_of_week is a list of booleans: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
# True = active, False = inactive
# (the wire protocol uses 1/0; Schedule converts automatically)

ALL_DAYS  = [True, True, True, True, True, True, True]     # Every day
WEEKDAYS  = [False, True, True, True, True, True, False]   # Monday-Friday
WEEKENDS  = [True, False, False, False, False, False, True]  # Saturday-Sunday
```

The list is indexed **Sunday-first**, while `datetime.weekday()` is
Monday-first. Two exported converters bridge them, so you never have to
write the `% 7` yourself:

```python
from datetime import date

from powerpetdoor import week_0_mon_to_sun, week_0_sun_to_mon

index = week_0_mon_to_sun(date.today().weekday())  # Monday=0 -> Sunday=0
schedule.days_of_week[index]                        # active today?

week_0_sun_to_mon(index) == date.today().weekday()  # and back
```

### Schedule Utilities

Three more exported helpers support bulk schedule work. `compress_schedule`
merges overlapping/adjacent windows into the fewest entries the device
needs; `compute_schedule_diff` turns "current on device" plus "what I want"
into the minimum set of `SET_SCHEDULE`/`DELETE_SCHEDULE` calls (the device
takes one connection and rate-limits messages, so this matters);
`schedule_entry_content_key` is the content-addressed key the diff compares
on, exported for callers that want to build their own index.

```python
from powerpetdoor import (
    compress_schedule,
    compute_schedule_diff,
    schedule_entry_content_key,
    schedule_template,
    validate_schedule_entry,
)

wanted = compress_schedule([...])            # entries built from schedule_template
current = [s.to_dict() for s in await door.refresh_schedules()]
to_delete, to_set = compute_schedule_diff(current, wanted)

# Entries that already match are left alone; every flag spelling the device
# might use ("1"/1/true) collapses to the same key, so an unchanged schedule
# really does diff to nothing.
schedule_entry_content_key(current[0]) == schedule_entry_content_key(wanted[0])
```

Entries handed to `compress_schedule` must be fully populated — start from
a deep copy of `schedule_template`, which carries every field in the wire
types `docs/protocol.md` specifies. `validate_schedule_entry` checks an
entry for the required fields without raising.
