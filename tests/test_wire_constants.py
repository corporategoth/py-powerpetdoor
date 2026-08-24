# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The wire constants, pinned by literal.

Both sides of this project - the client library and the simulator that
stands in for the device - read the *same symbol* from
:mod:`powerpetdoor.const`. Renaming a symbol, or re-spelling its value,
therefore changes what goes on the wire while every other test in this
suite stays green: `CMD_OPEN`, `FIELD_CMD_LOCKOUT` and `FIELD_AUTO` were
all free to change with nothing to catch it.

The perimeter is **derived**, not hand-listed: it is every constant in
``powerpetdoor.const`` whose value appears, quoted or in backticks, in
``docs/protocol.md``. That is checked here too, so a newly documented
constant has to be added below rather than silently escaping the pin.

``docs/protocol.md`` marks each claim **[V]** (verified against firmware
1.7.18) or **[R]** (reverse-engineered, unverified), and the **[R]** half is
*not* authority over what the firmware accepts. This module does not assert
that these values are right - only that they are what has actually been
running. Changing one must be a deliberate, visible line in a diff.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from powerpetdoor import const

PROTOCOL_MD = Path(__file__).resolve().parent.parent / "docs" / "protocol.md"

#: name -> the exact value that goes on the wire.
DOCUMENTED_WIRE_CONSTANTS = {
    "CMD_CHECK_RESET_REASON": "CHECK_RESET_REASON",
    "CMD_CLOSE": "CLOSE",
    "CMD_DELETE_SCHEDULE": "DELETE_SCHEDULE",
    "CMD_DISABLE_AUTORETRACT": "DISABLE_AUTORETRACT",
    "CMD_DISABLE_CMD_LOCKOUT": "DISABLE_CMD_LOCKOUT",
    "CMD_DISABLE_INSIDE": "DISABLE_INSIDE",
    "CMD_DISABLE_OUTSIDE": "DISABLE_OUTSIDE",
    "CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK": "DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK",
    "CMD_ENABLE_AUTORETRACT": "ENABLE_AUTORETRACT",
    "CMD_ENABLE_CMD_LOCKOUT": "ENABLE_CMD_LOCKOUT",
    "CMD_ENABLE_INSIDE": "ENABLE_INSIDE",
    "CMD_ENABLE_OUTSIDE": "ENABLE_OUTSIDE",
    "CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK": "ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK",
    "CMD_GET_AUTO": "GET_TIMERS_ENABLED",
    "CMD_GET_AUTORETRACT": "GET_AUTORETRACT",
    "CMD_GET_CMD_LOCKOUT": "GET_CMD_LOCKOUT",
    "CMD_GET_DOOR_BATTERY": "GET_DOOR_BATTERY",
    "CMD_GET_DOOR_OPEN_STATS": "GET_DOOR_OPEN_STATS",
    "CMD_GET_DOOR_STATUS": "GET_DOOR_STATUS",
    "CMD_GET_HOLD_TIME": "GET_HOLD_TIME",
    "CMD_GET_HW_INFO": "GET_HW_INFO",
    "CMD_GET_NOTIFICATIONS": "GET_NOTIFICATIONS",
    "CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK": "GET_OUTSIDE_SENSOR_SAFETY_LOCK",
    "CMD_GET_POWER": "GET_POWER",
    "CMD_GET_SCHEDULE": "GET_SCHEDULE",
    "CMD_GET_SCHEDULE_LIST": "GET_SCHEDULE_LIST",
    "CMD_GET_SENSORS": "GET_SENSORS",
    "CMD_GET_SENSOR_TRIGGER_VOLTAGE": "GET_SENSOR_TRIGGER_VOLTAGE",
    "CMD_GET_SETTINGS": "GET_SETTINGS",
    "CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE": "GET_SLEEP_SENSOR_TRIGGER_VOLTAGE",
    "CMD_GET_TIME": "GET_TIME",
    "CMD_GET_TIMEZONE": "GET_TIMEZONE",
    "CMD_HAS_REMOTE_ID": "HAS_REMOTE_ID",
    "CMD_HAS_REMOTE_KEY": "HAS_REMOTE_KEY",
    "CMD_OPEN": "OPEN",
    "CMD_OPEN_AND_HOLD": "OPEN_AND_HOLD",
    "CMD_POWER_OFF": "POWER_OFF",
    "CMD_POWER_ON": "POWER_ON",
    "CMD_SET_HOLD_TIME": "SET_HOLD_TIME",
    "CMD_SET_NOTIFICATIONS": "SET_NOTIFICATIONS",
    "CMD_SET_SCHEDULE": "SET_SCHEDULE",
    "CMD_SET_SCHEDULE_LIST": "SET_SCHEDULE_LIST",
    "CMD_SET_SENSOR_TRIGGER_VOLTAGE": "SET_SENSOR_TRIGGER_VOLTAGE",
    "CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE": "SET_SLEEP_SENSOR_TRIGGER_VOLTAGE",
    "CMD_SET_TIME": "SET_TIME",
    "CMD_SET_TIMEZONE": "SET_TIMEZONE",
    "COMMAND": "cmd",
    "CONFIG": "config",
    "DOOR_STATE_CLOSED": "DOOR_CLOSED",
    "DOOR_STATE_CLOSING_MID_OPEN": "DOOR_CLOSING_MID_OPEN",
    "DOOR_STATE_CLOSING": "DOOR_CLOSING",
    "DOOR_STATE_CLOSING_TOP_OPEN": "DOOR_CLOSING_TOP_OPEN",
    "DOOR_STATE_HOLDING": "DOOR_HOLDING",
    "DOOR_STATE_IDLE": "DOOR_IDLE",
    "DOOR_STATE_KEEPUP": "DOOR_KEEPUP",
    "DOOR_STATE_RISING": "DOOR_RISING",
    "DOOR_STATE_SLOWING": "DOOR_SLOWING",
    "DOOR_STATUS": "DOOR_STATUS",
    "DOOR_TO_PHONE": "d2p",
    "FIELD_AC_PRESENT": "acPresent",
    "FIELD_AUTO": "timersEnabled",
    "FIELD_AUTORETRACT": "doorOptions",
    "FIELD_BATTERY_PERCENT": "batteryPercent",
    "FIELD_BATTERY_PRESENT": "batteryPresent",
    "FIELD_CMD": "CMD",
    "FIELD_CMD_LOCKOUT": "allowCmdLockout",
    "FIELD_DAYSOFWEEK": "daysOfWeek",
    "FIELD_DIRECTION": "dir",
    "FIELD_DOOR_STATUS": "door_status",
    "FIELD_ENABLED": "enabled",
    "FIELD_FWINFO": "fwInfo",
    "FIELD_FW_MAJOR": "fw_maj",
    "FIELD_FW_MINOR": "fw_min",
    "FIELD_FW_PATCH": "fw_pat",
    "FIELD_HAS_REMOTE_ID": "has_id",
    "FIELD_HAS_REMOTE_KEY": "has_key",
    "FIELD_HOLD_OPEN_TIME": "holdOpenTime",
    "FIELD_HOLD_TIME": "holdTime",
    "FIELD_HOUR": "hour",
    "FIELD_HW_REVISION": "rev",
    "FIELD_HW_VERSION": "ver",
    "FIELD_INDEX": "index",
    "FIELD_INSIDE": "inside",
    "FIELD_LOW_BATTERY_NOTIFICATIONS": "lowBatteryNotificationsEnabled",
    "FIELD_MINUTE": "min",
    "FIELD_MSG_ID": "msgId",
    "FIELD_MSG_ID_RESPONSE": "msgID",
    "FIELD_NOTIFICATIONS": "notifications",
    "FIELD_OUTSIDE": "outside",
    "FIELD_OUTSIDE_SENSOR_SAFETY_LOCK": "outsideSensorSafetyLock",
    "FIELD_POWER": "power_state",
    "FIELD_REASON": "reason",
    "FIELD_RESET_REASON": "resetReason",
    "FIELD_SCHEDULE": "schedule",
    "FIELD_SCHEDULES": "schedules",
    "FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS": "sensorOffIndoorNotificationsEnabled",
    "FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS": "sensorOffOutdoorNotificationsEnabled",
    "FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS": "sensorOnIndoorNotificationsEnabled",
    "FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS": "sensorOnOutdoorNotificationsEnabled",
    "FIELD_SENSOR_STATE": "sensorState",
    "FIELD_SENSOR_TRIGGER_VOLTAGE": "sensorTriggerVoltage",
    "FIELD_SETTINGS": "settings",
    "FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE": "sleepSensorTriggerVoltage",
    "FIELD_SUCCESS": "success",
    "FIELD_TIME": "time",
    "FIELD_TOTAL_AUTO_RETRACTS": "totalAutoRetracts",
    "FIELD_TOTAL_OPEN_CYCLES": "totalOpenCycles",
    "FIELD_TZ": "tz",
    "FIELD_VOLTAGE": "voltage",
    "NOTIFY_LOW_BATTERY": "LOW_BATTERY",
    "NOTIFY_SENSOR_INDOOR": "SENSOR_INDOOR",
    "NOTIFY_SENSOR_OUTDOOR": "SENSOR_OUTDOOR",
    "PHONE_TO_DOOR": "p2d",
    "PING": "PING",
    "PONG": "PONG",
    "SENSOR_STATE_OFF": "off",
    "SENSOR_STATE_ON": "on",
    "SUCCESS_FALSE": "false",
    "SUCCESS_TRUE": "true",
    "TIME_FORMAT": "%a %b %d %H:%M:%S %Y",
}


def _documented_values() -> set[str]:
    """Every quoted or backticked token in ``docs/protocol.md``."""
    text = PROTOCOL_MD.read_text()
    return set(re.findall(r'"([^"\\\n]*)"', text)) | set(re.findall(r"`([^`\n]+)`", text))


def _wire_constants() -> dict[str, str]:
    """Every public string constant in ``powerpetdoor.const``."""
    return {
        name: value
        for name in dir(const)
        if name.isupper() and isinstance(value := getattr(const, name), str)
    }


def test_the_pinned_set_is_exactly_the_documented_set():
    """Derived, so the perimeter cannot quietly shrink.

    A constant that gains a documented value has to be pinned below; one
    that loses it has to be removed from the table on purpose.
    """
    documented = _documented_values()
    expected = {name for name, value in _wire_constants().items() if value in documented}

    assert set(DOCUMENTED_WIRE_CONSTANTS) == expected


def test_the_perimeter_covers_most_of_the_module():
    """A parser regression that matched nothing would pass the test above
    only by also emptying the table; this makes the size explicit."""
    assert len(DOCUMENTED_WIRE_CONSTANTS) >= 100
    assert len(DOCUMENTED_WIRE_CONSTANTS) <= len(_wire_constants())


@pytest.mark.parametrize(("name", "value"), sorted(DOCUMENTED_WIRE_CONSTANTS.items()))
def test_the_constant_still_spells_its_documented_value(name, value):
    assert getattr(const, name) == value
