# Power Pet Door Python Library

[![Buy Me Coffee][buymecoffee]][donation]

[![Github Release][releases-shield]][releases]
[![Github Activity][commits-shield]][commits]
[![License][license-shield]][license]

[![PyPI][pypi-shield]][pypi]
[![Python Versions][python-shield]][pypi]

A Python library for communicating with [Power Pet Door][powerpetdoor] WiFi-enabled pet doors made by [High Tech Pet][hitecpet].

<p align="center">
  <a href="https://www.hitecpet.com/collections/power-pet-doors3">
    <img src="https://www.hitecpet.com/cdn/shop/files/PX-2_with_Logo_2.png?v=1717017660&width=400" alt="Power Pet Door" width="300">
  </a>
</p>

## Disclaimer

**This library is NOT authorized, endorsed, or supported by High Tech Pet Products, Inc.**

This is an independent, community-developed project. No contributions, financial or otherwise, have been received from High Tech Pet. If you need official support for your Power Pet Door, please contact [High Tech Pet][hitecpet] directly.

## Installation

```bash
pip install pypowerpetdoor
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

## Door Simulator

The library includes a full-featured door simulator for testing clients without real hardware. The simulator speaks the same protocol as the real device and supports all commands.

### Quick Start

```bash
# Run the interactive simulator
python -m powerpetdoor.simulator

# Run with a test script
python -m powerpetdoor.simulator --script basic_cycle

# Run script and exit (for CI/CD)
python -m powerpetdoor.simulator --script full_test_suite --exit-after-script
```

### Programmatic Usage

```python
import asyncio
from powerpetdoor.simulator import DoorSimulator

async def main():
    simulator = DoorSimulator(host="0.0.0.0", port=3000)
    await simulator.start()

    # Trigger events programmatically
    simulator.trigger_sensor("inside")
    await asyncio.sleep(5)
    await simulator.close_door()

    await simulator.stop()

asyncio.run(main())
```

For complete documentation including scripting syntax and all available commands, see [docs/SIMULATOR.md](docs/SIMULATOR.md).

## Library Structure

```
powerpetdoor/
├── client.py          # PowerPetDoorClient - main client class
├── const.py           # Protocol constants and commands
├── schedule.py        # Schedule utilities
├── tz_utils.py        # Timezone utilities
└── simulator/         # Door simulator submodule
    ├── state.py       # Simulator state dataclasses
    ├── protocol.py    # Command handler registry
    ├── server.py      # DoorSimulator server
    ├── cli.py         # Interactive CLI
    ├── scripting.py   # YAML script runner
    └── scripts/       # Built-in test scripts
```

## Related Projects

- [ha-powerpetdoor][ha-powerpetdoor] - Home Assistant integration for Power Pet Door

## License

MIT License - see LICENSE file for details.

<!---->

***

[buymecoffee]: https://cdn.buymeacoffee.com/buttons/default-orange.png
[donation]: https://buymeacoffee.com/corporategoth
[commits-shield]: https://img.shields.io/github/commit-activity/y/corporategoth/py-powerpetdoor.svg?style=for-the-badge
[commits]: https://github.com/corporategoth/py-powerpetdoor/commits/main
[license]: https://github.com/corporategoth/py-powerpetdoor/blob/main/LICENSE
[license-shield]: https://img.shields.io/github/license/corporategoth/py-powerpetdoor.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/corporategoth/py-powerpetdoor.svg?style=for-the-badge
[releases]: https://github.com/corporategoth/py-powerpetdoor/releases
[pypi-shield]: https://img.shields.io/pypi/v/pypowerpetdoor.svg?style=for-the-badge
[pypi]: https://pypi.org/project/pypowerpetdoor/
[python-shield]: https://img.shields.io/pypi/pyversions/pypowerpetdoor.svg?style=for-the-badge
[hitecpet]: https://www.hitecpet.com/
[powerpetdoor]: https://www.hitecpet.com/collections/power-pet-doors3
[ha-powerpetdoor]: https://github.com/corporategoth/ha-powerpetdoor
