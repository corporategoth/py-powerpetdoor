# Power Pet Door Python Library

[![Buy Me Coffee][buymecoffee]][donation]

[![Tests][tests-shield]][tests]
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

Requires Python 3.11-3.14. **Supported platforms: Linux and macOS.** The
library itself has no platform-specific code, but the simulator's plain-stdin
prompt fallback uses `loop.add_reader()` on stdin, which Windows'
`ProactorEventLoop` does not implement, and CI runs on Linux only - so
Windows is not a claimed platform.

## Quick Start

The library provides two interfaces:

### PowerPetDoor (Recommended)

A high-level, Pythonic interface with cached state and simple methods:

```python
import asyncio
from powerpetdoor import PowerPetDoor

async def main():
    door = PowerPetDoor("192.168.1.100")
    await door.connect()

    # Read state via properties
    print(f"Door status: {door.status.name}")
    print(f"Battery: {door.battery_percent}%")

    # Control via async methods
    if door.is_closed:
        await door.open()

    await door.set_hold_time(15)
    await door.set_inside_sensor(True)

    # Register callbacks
    door.on_status_change(lambda s: print(f"Status: {s.name}"))

    await door.disconnect()

asyncio.run(main())
```

See [docs/door.md](docs/door.md) for complete documentation.

### PowerPetDoorClient (Low-Level)

For advanced use cases requiring direct protocol access:

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

    await client.connect()

    settings = await client.send_message(CONFIG, CMD_GET_SETTINGS, notify=True)
    print(f"Settings: {settings}")

    client.stop()

asyncio.run(main())
```

See [docs/client.md](docs/client.md) for complete documentation.

## Documentation

| Document | Description |
|----------|-------------|
| [docs/door.md](docs/door.md) | PowerPetDoor high-level interface |
| [docs/client.md](docs/client.md) | PowerPetDoorClient low-level interface |
| [docs/simulator.md](docs/simulator.md) | Door simulator for testing |

## Door Simulator

The library includes a full-featured door simulator for testing without hardware:

```bash
# Run interactive simulator (also available as the ppd-simulator entry point)
python -m powerpetdoor.simulator

# Run with a test script (built-in name or file path)
python -m powerpetdoor.simulator --script basic_cycle

# Run in CI/CD (exit on completion; exit code reflects pass/fail)
python -m powerpetdoor.simulator --script full_test_suite --oneshot
```

See [docs/simulator.md](docs/simulator.md) for complete documentation.

## Library Structure

```
powerpetdoor/
├── door.py            # PowerPetDoor high-level interface
├── client.py          # PowerPetDoorClient low-level client
├── const.py           # Protocol constants and commands
├── framing.py         # Shared JSON frame scanner for the wire protocol
├── sanitize.py        # Terminal-safety helper for untrusted text (shared)
├── schedule.py        # Schedule utilities
├── tz_utils.py        # Timezone utilities
└── simulator/         # Door simulator submodule
    ├── state.py           # Simulator state dataclasses
    ├── protocol.py        # Protocol handler (asyncio Protocol + command registry)
    ├── engine.py          # DoorMotionEngine (shared door-motion state machine)
    ├── server.py          # DoorSimulator server
    ├── cli.py             # ppd-simulator CLI (interactive/script/daemon modes)
    ├── ctl.py             # ppd-simulator-ctl remote-control client
    ├── prompt_common.py   # Shared prompt_toolkit machinery (highlighting, completion)
    ├── commands/          # Interactive command implementations
    ├── scripting.py       # YAML script runner
    └── scripts/           # Built-in test scripts
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
[tests-shield]: https://img.shields.io/github/actions/workflow/status/corporategoth/py-powerpetdoor/test.yml?branch=main&label=tests&style=for-the-badge
[tests]: https://github.com/corporategoth/py-powerpetdoor/actions/workflows/test.yml
[releases-shield]: https://img.shields.io/github/release/corporategoth/py-powerpetdoor.svg?style=for-the-badge
[releases]: https://github.com/corporategoth/py-powerpetdoor/releases
[pypi-shield]: https://img.shields.io/pypi/v/pypowerpetdoor.svg?style=for-the-badge
[pypi]: https://pypi.org/project/pypowerpetdoor/
[python-shield]: https://img.shields.io/pypi/pyversions/pypowerpetdoor.svg?style=for-the-badge
[hitecpet]: https://www.hitecpet.com/
[powerpetdoor]: https://www.hitecpet.com/collections/power-pet-doors3
[ha-powerpetdoor]: https://github.com/corporategoth/ha-powerpetdoor
