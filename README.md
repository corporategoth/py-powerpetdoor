# Power Pet Door Python Library

A Python library for communicating with Power Pet Door devices over the network.

## Installation

```bash
pip install powerpetdoor
```

## Usage

```python
import asyncio
from powerpetdoor import PowerPetDoorClient, COMMAND, CMD_OPEN, CMD_CLOSE

# Create a client
client = PowerPetDoorClient(
    host="192.168.1.100",
    port=3000,
    keepalive=30.0,
    timeout=10.0,
    reconnect=5.0
)

# Add a listener for door status updates
client.add_listener(
    name="my_app",
    door_status_update=lambda status: print(f"Door status: {status}")
)

# Start the client (blocks if running own event loop)
# For integration with existing event loop, pass it to the constructor
client.start()

# Send commands
client.send_message(COMMAND, CMD_OPEN)
client.send_message(COMMAND, CMD_CLOSE)

# Stop when done
client.stop()
```

## Async Usage

For use with an existing asyncio event loop:

```python
import asyncio
from powerpetdoor import PowerPetDoorClient, COMMAND, CMD_GET_SETTINGS, CONFIG

async def main():
    loop = asyncio.get_event_loop()

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

    # Send a command and wait for response
    settings = await client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
    print(f"Settings: {settings}")

    # Disconnect
    client.stop()

asyncio.run(main())
```

## Schedule Utilities

The library includes utilities for working with Power Pet Door schedules:

```python
from powerpetdoor import (
    compress_schedule,
    validate_schedule_entry,
    compute_schedule_diff,
    schedule_template,
)

# Validate a schedule entry
entry = {...}
if validate_schedule_entry(entry):
    print("Entry is valid")

# Compress multiple schedule entries
compressed = compress_schedule(schedule_list)

# Compute differences between schedules
to_delete, to_add = compute_schedule_diff(current, new)
```

## License

MIT License - see LICENSE file for details.
