# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Protocol constants for Power Pet Door communication.

This module contains constants used in the Power Pet Door network protocol.
These are independent of any home automation framework.
"""

# Minimum time between messages to avoid overwhelming the device
MINIMUM_TIME_BETWEEN_MSGS = 0.200

# Message type identifiers
COMMAND = "cmd"
CONFIG = "config"
PING = "PING"
PONG = "PONG"
DOOR_STATUS = "DOOR_STATUS"

# Message envelope fields
FIELD_MSG_ID = "msgId"
FIELD_MSG_ID_RESPONSE = "msgID"  # Response uses different casing
FIELD_DIRECTION = "dir"
FIELD_CMD = "CMD"  # Command echo in responses

# Direction values (p2d = phone to door, d2p = door to phone)
PHONE_TO_DOOR = "p2d"
DOOR_TO_PHONE = "d2p"

# Success field values (strings, not booleans)
SUCCESS_TRUE = "true"
SUCCESS_FALSE = "false"

# Failure reason field in error responses
FIELD_REASON = "reason"

# Field names in protocol messages
FIELD_POWER = "power_state"
FIELD_INSIDE = "inside"
FIELD_OUTSIDE = "outside"
FIELD_AUTO = "timersEnabled"
FIELD_OUTSIDE_SENSOR_SAFETY_LOCK = "outsideSensorSafetyLock"
FIELD_CMD_LOCKOUT = "allowCmdLockout"
FIELD_AUTORETRACT = "doorOptions"  # An int BITFIELD - see DOOR_OPTION_AUTORETRACT
FIELD_TOTAL_OPEN_CYCLES = "totalOpenCycles"
FIELD_TOTAL_AUTO_RETRACTS = "totalAutoRetracts"
FIELD_SETTINGS = "settings"
FIELD_NOTIFICATIONS = "notifications"
FIELD_TZ = "tz"
#: The door's local wall-clock time, in its configured timezone.
FIELD_TIME = "time"
#: How :data:`FIELD_TIME` is spelled: a C ``asctime()`` string, e.g.
#: ``"Sat Aug 22 23:13:48 2026"``. Both sides of this project format and
#: parse through this one constant.
TIME_FORMAT = "%a %b %d %H:%M:%S %Y"
FIELD_SCHEDULE = "schedule"
FIELD_SCHEDULES = "schedules"
FIELD_INDEX = "index"
FIELD_ENABLED = "enabled"
FIELD_DAYSOFWEEK = "daysOfWeek"
FIELD_INSIDE_PREFIX = "in"
FIELD_OUTSIDE_PREFIX = "out"
FIELD_START_TIME_SUFFIX = "_start_time"
FIELD_END_TIME_SUFFIX = "_end_time"
FIELD_HOUR = "hour"
FIELD_MINUTE = "min"
#: The field a sensor-trigger-voltage **setter** takes. **Verified against
#: firmware 1.7.18**: `SET_SENSOR_TRIGGER_VOLTAGE` and
#: `SET_SLEEP_SENSOR_TRIGGER_VOLTAGE` require `voltage` and reject the
#: getter's field name; the reply then echoes the *getter's* field.
FIELD_VOLTAGE = "voltage"
FIELD_HOLD_TIME = "holdTime"
FIELD_HOLD_OPEN_TIME = "holdOpenTime"
FIELD_SENSOR_TRIGGER_VOLTAGE = "sensorTriggerVoltage"
FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE = "sleepSensorTriggerVoltage"
FIELD_DOOR_STATUS = "door_status"
FIELD_SUCCESS = "success"
FIELD_FWINFO = "fwInfo"
FIELD_BATTERY_PERCENT = "batteryPercent"
FIELD_BATTERY_PRESENT = "batteryPresent"
FIELD_AC_PRESENT = "acPresent"
FIELD_HW_VERSION = "ver"  # Hardware version
FIELD_HW_REVISION = "rev"  # Hardware revision
FIELD_FW_MAJOR = "fw_maj"
FIELD_FW_MINOR = "fw_min"
FIELD_FW_PATCH = "fw_pat"
#: Bit 1 of the ``doorOptions`` bitfield: auto-retract on obstruction.
#: **Verified against firmware 1.7.18**: ``DISABLE_AUTORETRACT`` leaves
#: ``doorOptions`` at the integer ``0`` and ``ENABLE_AUTORETRACT`` leaves it
#: at the integer ``2``. The other bits are unidentified, so ``2`` must not
#: be read as "auto-retract and nothing else", and the field must never be
#: read by plain truthiness.
DOOR_OPTION_AUTORETRACT = 2

FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS = "sensorOnIndoorNotificationsEnabled"
FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS = "sensorOffIndoorNotificationsEnabled"
FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS = "sensorOnOutdoorNotificationsEnabled"
FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS = "sensorOffOutdoorNotificationsEnabled"
FIELD_LOW_BATTERY_NOTIFICATIONS = "lowBatteryNotificationsEnabled"

# Door state values
DOOR_STATE_IDLE = "DOOR_IDLE"
DOOR_STATE_CLOSED = "DOOR_CLOSED"
DOOR_STATE_HOLDING = "DOOR_HOLDING"
DOOR_STATE_KEEPUP = "DOOR_KEEPUP"
DOOR_STATE_RISING = "DOOR_RISING"
DOOR_STATE_SLOWING = "DOOR_SLOWING"
DOOR_STATE_CLOSING_TOP_OPEN = "DOOR_CLOSING_TOP_OPEN"
DOOR_STATE_CLOSING_MID_OPEN = "DOOR_CLOSING_MID_OPEN"

# Command strings
CMD_OPEN = "OPEN"
CMD_OPEN_AND_HOLD = "OPEN_AND_HOLD"
CMD_CLOSE = "CLOSE"
CMD_GET_SETTINGS = "GET_SETTINGS"
CMD_GET_SENSORS = "GET_SENSORS"
CMD_GET_POWER = "GET_POWER"
CMD_GET_AUTO = "GET_TIMERS_ENABLED"
CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK = "GET_OUTSIDE_SENSOR_SAFETY_LOCK"
CMD_GET_CMD_LOCKOUT = "GET_CMD_LOCKOUT"
CMD_GET_AUTORETRACT = "GET_AUTORETRACT"
CMD_GET_DOOR_STATUS = "GET_DOOR_STATUS"
CMD_GET_DOOR_OPEN_STATS = "GET_DOOR_OPEN_STATS"
CMD_DISABLE_INSIDE = "DISABLE_INSIDE"
CMD_ENABLE_INSIDE = "ENABLE_INSIDE"
CMD_DISABLE_OUTSIDE = "DISABLE_OUTSIDE"
CMD_ENABLE_OUTSIDE = "ENABLE_OUTSIDE"
CMD_DISABLE_AUTO = "DISABLE_TIMERS"
CMD_ENABLE_AUTO = "ENABLE_TIMERS"
CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK = "DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK"
CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK = "ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK"
CMD_DISABLE_CMD_LOCKOUT = "DISABLE_CMD_LOCKOUT"
CMD_ENABLE_CMD_LOCKOUT = "ENABLE_CMD_LOCKOUT"
CMD_DISABLE_AUTORETRACT = "DISABLE_AUTORETRACT"
CMD_ENABLE_AUTORETRACT = "ENABLE_AUTORETRACT"
CMD_POWER_ON = "POWER_ON"
CMD_POWER_OFF = "POWER_OFF"
CMD_GET_HW_INFO = "GET_HW_INFO"
CMD_GET_DOOR_BATTERY = "GET_DOOR_BATTERY"
CMD_HAS_REMOTE_ID = "HAS_REMOTE_ID"
CMD_HAS_REMOTE_KEY = "HAS_REMOTE_KEY"
CMD_CHECK_RESET_REASON = "CHECK_RESET_REASON"

CMD_GET_NOTIFICATIONS = "GET_NOTIFICATIONS"
CMD_SET_NOTIFICATIONS = "SET_NOTIFICATIONS"
CMD_GET_HOLD_TIME = "GET_HOLD_TIME"
CMD_SET_HOLD_TIME = "SET_HOLD_TIME"
CMD_GET_TIMEZONE = "GET_TIMEZONE"
CMD_SET_TIMEZONE = "SET_TIMEZONE"
CMD_GET_SENSOR_TRIGGER_VOLTAGE = "GET_SENSOR_TRIGGER_VOLTAGE"
CMD_SET_SENSOR_TRIGGER_VOLTAGE = "SET_SENSOR_TRIGGER_VOLTAGE"
CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE = "GET_SLEEP_SENSOR_TRIGGER_VOLTAGE"
CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE = "SET_SLEEP_SENSOR_TRIGGER_VOLTAGE"

#: Read the door's own wall clock. **Verified against firmware 1.7.18**;
#: undocumented by the vendor. The reply carries :data:`FIELD_TIME`.
CMD_GET_TIME = "GET_TIME"
#: **Verified against firmware 1.7.18: the clock is READ-ONLY.** This name
#: is defined only so the simulator can reproduce the door's strangest
#: observed behaviour - ``SET_TIME`` is answered with *silence*, not a
#: failure envelope, where every other rejected shape answers
#: ``success: "false"``. Never send it.
CMD_SET_TIME = "SET_TIME"

CMD_GET_SCHEDULE_LIST = "GET_SCHEDULE_LIST"
CMD_SET_SCHEDULE_LIST = "SET_SCHEDULE_LIST"
CMD_GET_SCHEDULE = "GET_SCHEDULE"
CMD_SET_SCHEDULE = "SET_SCHEDULE"
CMD_DELETE_SCHEDULE = "DELETE_SCHEDULE"

#: The only commands a real door accepts under the ``cmd`` envelope key.
#: **Verified against firmware 1.7.18**: ``{"cmd": "ENABLE_INSIDE"}`` is
#: answered ``success: "false"`` while ``{"config": "ENABLE_INSIDE"}``
#: succeeds, so every command that is not door motion - including the
#: individual setting commands - has to be sent as ``config``.
COMMAND_ENVELOPE_COMMANDS = frozenset({CMD_OPEN, CMD_OPEN_AND_HOLD, CMD_CLOSE})

# Notification event types (sent by device when events occur)
NOTIFY_SENSOR_INDOOR = "SENSOR_INDOOR"
NOTIFY_SENSOR_OUTDOOR = "SENSOR_OUTDOOR"
NOTIFY_LOW_BATTERY = "LOW_BATTERY"

# Field for notification events
FIELD_SENSOR_STATE = "sensorState"  # "on" or "off"

# Sensor state values in notification events
SENSOR_STATE_ON = "on"
SENSOR_STATE_OFF = "off"

# Response field names for remote/reset commands.
# `has_id`/`has_key` are **verified against firmware 1.7.18** - the door does
# NOT use the camelCase `hasRemoteId`/`hasRemoteKey` this project guessed at
# for its first five years, which is why those readers never fired.
FIELD_HAS_REMOTE_ID = "has_id"
FIELD_HAS_REMOTE_KEY = "has_key"
#: Reverse-engineered and **unverified**: firmware 1.7.18 has no
#: CHECK_RESET_REASON command at all, so no reply carrying this field was
#: ever observed. Kept because a different firmware revision may have one.
FIELD_RESET_REASON = "resetReason"

# Message priorities (lower = higher priority)
PRIORITY_CRITICAL = 0  # Keepalive (PING/PONG)
PRIORITY_HIGH = 1  # Door commands (OPEN, CLOSE)
PRIORITY_MEDIUM = 2  # Settings changes (ENABLE/DISABLE)
PRIORITY_LOW = 3  # Status requests, schedules

# Command priority mapping
COMMAND_PRIORITIES = {
    # Critical - Keepalive
    PONG: PRIORITY_CRITICAL,
    # High - Door commands
    CMD_OPEN: PRIORITY_HIGH,
    CMD_CLOSE: PRIORITY_HIGH,
    CMD_OPEN_AND_HOLD: PRIORITY_HIGH,
    # Medium - Settings changes
    CMD_ENABLE_INSIDE: PRIORITY_MEDIUM,
    CMD_DISABLE_INSIDE: PRIORITY_MEDIUM,
    CMD_ENABLE_OUTSIDE: PRIORITY_MEDIUM,
    CMD_DISABLE_OUTSIDE: PRIORITY_MEDIUM,
    CMD_ENABLE_AUTO: PRIORITY_MEDIUM,
    CMD_DISABLE_AUTO: PRIORITY_MEDIUM,
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK: PRIORITY_MEDIUM,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK: PRIORITY_MEDIUM,
    CMD_ENABLE_CMD_LOCKOUT: PRIORITY_MEDIUM,
    CMD_DISABLE_CMD_LOCKOUT: PRIORITY_MEDIUM,
    CMD_ENABLE_AUTORETRACT: PRIORITY_MEDIUM,
    CMD_DISABLE_AUTORETRACT: PRIORITY_MEDIUM,
    CMD_POWER_ON: PRIORITY_MEDIUM,
    CMD_POWER_OFF: PRIORITY_MEDIUM,
    CMD_SET_NOTIFICATIONS: PRIORITY_MEDIUM,
    CMD_SET_HOLD_TIME: PRIORITY_MEDIUM,
    CMD_SET_TIMEZONE: PRIORITY_MEDIUM,
    CMD_SET_SENSOR_TRIGGER_VOLTAGE: PRIORITY_MEDIUM,
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE: PRIORITY_MEDIUM,
    # Low - Status requests and schedules (default for anything not listed)
    CMD_GET_DOOR_STATUS: PRIORITY_LOW,
    CMD_GET_SETTINGS: PRIORITY_LOW,
    CMD_GET_SENSORS: PRIORITY_LOW,
    CMD_GET_POWER: PRIORITY_LOW,
    CMD_GET_AUTO: PRIORITY_LOW,
    CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK: PRIORITY_LOW,
    CMD_GET_CMD_LOCKOUT: PRIORITY_LOW,
    CMD_GET_AUTORETRACT: PRIORITY_LOW,
    CMD_GET_DOOR_OPEN_STATS: PRIORITY_LOW,
    CMD_GET_HW_INFO: PRIORITY_LOW,
    CMD_GET_DOOR_BATTERY: PRIORITY_LOW,
    CMD_GET_NOTIFICATIONS: PRIORITY_LOW,
    CMD_GET_HOLD_TIME: PRIORITY_LOW,
    CMD_GET_TIMEZONE: PRIORITY_LOW,
    CMD_GET_TIME: PRIORITY_LOW,
    CMD_GET_SENSOR_TRIGGER_VOLTAGE: PRIORITY_LOW,
    CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE: PRIORITY_LOW,
    CMD_HAS_REMOTE_ID: PRIORITY_LOW,
    CMD_HAS_REMOTE_KEY: PRIORITY_LOW,
    CMD_CHECK_RESET_REASON: PRIORITY_LOW,
    CMD_GET_SCHEDULE_LIST: PRIORITY_LOW,
    CMD_SET_SCHEDULE_LIST: PRIORITY_LOW,
    CMD_GET_SCHEDULE: PRIORITY_LOW,
    CMD_SET_SCHEDULE: PRIORITY_LOW,
    CMD_DELETE_SCHEDULE: PRIORITY_LOW,
}
