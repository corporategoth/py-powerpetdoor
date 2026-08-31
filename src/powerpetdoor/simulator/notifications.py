# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The five notifications a Power Pet Door can raise.

The door carries a switch for each of these, readable and writable over
the wire (``sensorOnIndoorNotificationsEnabled`` and friends). The
**notification itself** is not a wire message: it reaches the owner
through the vendor's own service, not over TCP 3000, so nothing here is
ever sent to a connected client.

What the simulator can do is show that one *would* have been raised. Each
is counted, logged, and delivered to any listener - so a script can wait
on one, an operator can watch them go by, and a program can subscribe.

The ``_on``/``_off`` suffix names **whether the sensor was enabled**, not
whether it activated:

- ``inside_on`` - a pet reached the inside sensor while that sensor was on.
- ``inside_off`` - a pet reached it while the sensor was switched off,
  which is the notification's whole point: your pet tried to get out and
  could not.
"""

from __future__ import annotations

#: A pet reached a sensor that was switched on.
NOTIFY_INSIDE_ON = "inside_on"
NOTIFY_OUTSIDE_ON = "outside_on"
#: A pet reached a sensor that was switched off.
NOTIFY_INSIDE_OFF = "inside_off"
NOTIFY_OUTSIDE_OFF = "outside_off"
#: The battery crossed the low-battery threshold.
NOTIFY_LOW_BATTERY = "low_battery"

#: Every notification, in a stable order.
NOTIFICATION_NAMES: tuple[str, ...] = (
    NOTIFY_INSIDE_OFF,
    NOTIFY_INSIDE_ON,
    NOTIFY_LOW_BATTERY,
    NOTIFY_OUTSIDE_OFF,
    NOTIFY_OUTSIDE_ON,
)

#: Which :class:`~powerpetdoor.simulator.state.DoorSimulatorState` flag
#: gates each one. A notification whose switch is off is not raised at all
#: - not counted, not logged, not delivered - because that is what the
#: switch means.
NOTIFICATION_SETTINGS: dict[str, str] = {
    NOTIFY_INSIDE_ON: "sensor_on_indoor",
    NOTIFY_INSIDE_OFF: "sensor_off_indoor",
    NOTIFY_OUTSIDE_ON: "sensor_on_outdoor",
    NOTIFY_OUTSIDE_OFF: "sensor_off_outdoor",
    NOTIFY_LOW_BATTERY: "low_battery",
}


def sensor_notification(sensor: str, enabled: bool) -> str:
    """The notification a pet at ``sensor`` raises.

    Args:
        sensor: ``"inside"`` or ``"outside"``.
        enabled: Whether that sensor was switched on at the time.
    """
    suffix = "on" if enabled else "off"
    return f"{sensor}_{suffix}"
