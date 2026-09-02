# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Protocol constants for Power Pet Door communication.

This module contains constants used in the Power Pet Door network protocol.
These are independent of any home automation framework.
"""

#: Quiet time to leave after the door has spoken, before sending again.
#:
#: Measured **from the door's reply**, not from our previous send. That
#: distinction is the whole point: the client sends one message at a time
#: and waits for the answer, so a send-relative floor is already satisfied
#: by any round trip longer than itself - and stops adding any gap at all
#: exactly when the door is slowest, which is when it is struggling.
#:
#: With the send-relative floor removed, 24 rotating commands per trial,
#: three interleaved rounds:
#:
#:     reply->send gap:   0ms    5ms   10ms   20ms   25ms   50ms
#:     dropped of 72:      11      7      7      1      0      0
MINIMUM_TIME_BETWEEN_MSGS = 0.050

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
#: The field a sensor-trigger-voltage **setter** takes.
#: `SET_SENSOR_TRIGGER_VOLTAGE` and
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
#: ``DISABLE_AUTORETRACT`` leaves
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
#: The FIRST of the three closing states. The full sequence is
#: ``DOOR_IDLE -> DOOR_RISING -> DOOR_SLOWING -> DOOR_HOLDING ->
#: DOOR_CLOSING -> DOOR_CLOSING_TOP_OPEN -> DOOR_CLOSING_MID_OPEN ->
#: DOOR_CLOSED -> DOOR_IDLE``. It was missing here, so every close spent a
#: moment in ``DoorStatus.UNKNOWN`` - neither open nor closed - and logged a
#: warning.
DOOR_STATE_CLOSING = "DOOR_CLOSING"
DOOR_STATE_CLOSING_TOP_OPEN = "DOOR_CLOSING_TOP_OPEN"
DOOR_STATE_CLOSING_MID_OPEN = "DOOR_CLOSING_MID_OPEN"
#: What the door answers `GET_DOOR_STATUS` with while `power_state` is
#: false. It is a real state and not an error: the flap is down and the
#: motor will not run, so none of the nine states above can describe it.
#: Missing here, it read as `DoorStatus.UNKNOWN` - a warning on every
#: status read, and a consumer showing "unknown" for a door that is simply
#: switched off.
DOOR_STATE_POWEROFF = "DOOR_POWEROFF"

#: The door is down. ``DOOR_IDLE`` is the resting state a real door settles
#: into after ``DOOR_CLOSED``, so both mean "closed" to a caller.
#: ``DOOR_POWEROFF`` joins them because the flap really is down - a
#: powered-off door is shut and cannot open. Reporting it as neither open
#: nor closed would leave a cover entity blank for a door whose position is
#: not in doubt.
DOOR_STATES_CLOSED = frozenset({DOOR_STATE_CLOSED, DOOR_STATE_IDLE, DOOR_STATE_POWEROFF})
#: The door is up and has stopped travelling - the only states in which
#: "open" is a settled fact rather than a prediction. ``DOOR_HOLDING`` is a
#: timed open, ``DOOR_KEEPUP`` an indefinite one.
DOOR_STATES_FULLY_OPEN = frozenset({DOOR_STATE_HOLDING, DOOR_STATE_KEEPUP})
#: The door is travelling up.
DOOR_STATES_OPENING = frozenset({DOOR_STATE_RISING, DOOR_STATE_SLOWING})
#: The door is travelling down. All three, including the brief
#: ``DOOR_CLOSING`` in which the motor has started but the flap has not
#: moved.
DOOR_STATES_CLOSING = frozenset(
    {DOOR_STATE_CLOSING, DOOR_STATE_CLOSING_TOP_OPEN, DOOR_STATE_CLOSING_MID_OPEN}
)
#: Open *or* opening - what :attr:`~powerpetdoor.door.PowerPetDoor.is_open`
#: reports. Deliberately wider than :data:`DOOR_STATES_FULLY_OPEN`: a rising
#: door is not closed, so a consumer rendering a cover entity needs it to
#: read as open. Anything that acts on the door rather than describing it
#: wants :data:`DOOR_STATES_FULLY_OPEN` instead.
DOOR_STATES_OPEN = DOOR_STATES_FULLY_OPEN | DOOR_STATES_OPENING
#: How far open the door is, as a percentage, per status. Shared by
#: :attr:`~powerpetdoor.door.PowerPetDoor.position` and the simulator's
#: ``position`` script condition so the two cannot disagree about where a
#: door in a given state is.
#:
#: **Not** an ordering of the statuses: the sequence is a cycle, not a
#: line. ``DOOR_CLOSING`` is 100 because the motor has started but the flap
#: has not moved, so it shares a height with ``DOOR_HOLDING`` while coming
#: after it in time; ``DOOR_RISING`` and ``DOOR_CLOSING_MID_OPEN`` share
#: one too, going opposite ways.
DOOR_POSITIONS: dict[str, int] = {
    DOOR_STATE_IDLE: 0,
    DOOR_STATE_CLOSED: 0,
    DOOR_STATE_RISING: 33,
    DOOR_STATE_SLOWING: 66,
    DOOR_STATE_HOLDING: 100,
    DOOR_STATE_KEEPUP: 100,
    DOOR_STATE_CLOSING: 100,
    DOOR_STATE_CLOSING_TOP_OPEN: 66,
    DOOR_STATE_CLOSING_MID_OPEN: 33,
    DOOR_STATE_POWEROFF: 0,
}

# Command strings
CMD_OPEN = "OPEN"
CMD_OPEN_AND_HOLD = "OPEN_AND_HOLD"
CMD_CLOSE = "CLOSE"
CMD_GET_SETTINGS = "GET_SETTINGS"
#: Whether each sensor is **armed**. Answers with the ints ``1``/``0``,
#: unlike the two switches below, which answer with strings.
CMD_GET_SENSORS = "GET_SENSORS"
#: Whether the unit as a whole is powered - in practice, whether the motor
#: is live. With it off, every open command is refused. Answers
#: ``power_state`` as the STRING ``"true"``/``"false"``.
CMD_GET_POWER = "GET_POWER"
#: Whether **scheduling** is in force, the read side of
#: :data:`CMD_ENABLE_AUTO`/:data:`CMD_DISABLE_AUTO`. Answers
#: ``timersEnabled`` as the STRING ``"true"``/``"false"``.
CMD_GET_TIMERS_ENABLED = "GET_TIMERS_ENABLED"
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

#: Read the door's own wall clock. Undocumented by the vendor. The reply
#: carries :data:`FIELD_TIME`. The clock is read-only.
CMD_GET_TIME = "GET_TIME"

CMD_GET_SCHEDULE_LIST = "GET_SCHEDULE_LIST"
CMD_GET_SCHEDULE = "GET_SCHEDULE"
CMD_SET_SCHEDULE = "SET_SCHEDULE"
CMD_DELETE_SCHEDULE = "DELETE_SCHEDULE"

#: The only commands a real door accepts under the ``cmd`` envelope key.
#: ``{"cmd": "ENABLE_INSIDE"}`` is
#: answered ``success: "false"`` while ``{"config": "ENABLE_INSIDE"}``
#: succeeds, so every command that is not door motion - including the
#: individual setting commands - has to be sent as ``config``.
COMMAND_ENVELOPE_COMMANDS = frozenset({CMD_OPEN, CMD_OPEN_AND_HOLD, CMD_CLOSE})

# Response field names for remote/reset commands.
# The door uses `has_id`/`has_key`, NOT the camelCase
# `hasRemoteId`/`hasRemoteKey` this project guessed at
# for its first five years, which is why those readers never fired.
FIELD_HAS_REMOTE_ID = "has_id"
FIELD_HAS_REMOTE_KEY = "has_key"

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
    CMD_GET_TIMERS_ENABLED: PRIORITY_LOW,
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
    CMD_GET_SCHEDULE_LIST: PRIORITY_LOW,
    CMD_GET_SCHEDULE: PRIORITY_LOW,
    CMD_SET_SCHEDULE: PRIORITY_LOW,
    CMD_DELETE_SCHEDULE: PRIORITY_LOW,
}
