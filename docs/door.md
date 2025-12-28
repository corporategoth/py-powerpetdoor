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

### Connection Properties

| Property | Type | Description |
|----------|------|-------------|
| `connected` | `bool` | Whether currently connected to the door |
| `host` | `str` | The door's IP address or hostname |
| `port` | `int` | The door's TCP port |

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
```

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

```python
# Get current timezone (POSIX format)
print(f"Timezone: {door.timezone}")

# Set timezone (POSIX format)
await door.set_timezone("EST5EDT,M3.2.0,M11.1.0")
```

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

# days_of_week is a list: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
schedule = Schedule(
    index=0,
    enabled=True,
    days_of_week=[1, 1, 1, 1, 1, 1, 1],  # All days
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
    index: int = 0                              # Schedule slot (0-based)
    enabled: bool = True                        # Whether schedule is active
    days_of_week: list = [1,1,1,1,1,1,1]        # [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
    inside: bool = False                        # Controls inside sensor
    outside: bool = False                       # Controls outside sensor
    start: ScheduleTime                         # Start time for sensor
    end: ScheduleTime                           # End time for sensor
```

### Days of Week List

Each schedule entry controls ONE sensor (inside or outside) for specific days and a time window.

```python
# days_of_week is a list: [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
# 1 = active, 0 = inactive

ALL_DAYS  = [1, 1, 1, 1, 1, 1, 1]  # Every day
WEEKDAYS  = [0, 1, 1, 1, 1, 1, 0]  # Monday-Friday
WEEKENDS  = [1, 0, 0, 0, 0, 0, 1]  # Saturday-Sunday
```
