# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Power Pet Door Python Library.

A Python library for communicating with Power Pet Door devices over the network.

Example usage:
    from powerpetdoor import PowerPetDoorClient

    client = PowerPetDoorClient(
        host="192.168.1.100",
        port=3000,
        keepalive=30.0,
        timeout=10.0,
        reconnect=5.0
    )
    client.start()
"""

from .client import PowerPetDoorClient, PrioritizedMessage, find_end, make_bool
from .door import (
    PowerPetDoor,
    DoorStatus,
    NotificationSettings,
    BatteryInfo,
    Schedule,
    ScheduleTime,
)
from .const import (
    # Message types
    COMMAND,
    CONFIG,
    PING,
    PONG,
    DOOR_STATUS,
    # Message envelope
    FIELD_MSG_ID,
    FIELD_MSG_ID_RESPONSE,
    FIELD_DIRECTION,
    FIELD_CMD,
    PHONE_TO_DOOR,
    DOOR_TO_PHONE,
    # Commands
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_CLOSE,
    CMD_GET_SETTINGS,
    CMD_GET_SENSORS,
    CMD_GET_POWER,
    CMD_GET_AUTO,
    CMD_GET_DOOR_STATUS,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_ENABLE_INSIDE,
    CMD_DISABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_ENABLE_AUTO,
    CMD_DISABLE_AUTO,
    CMD_POWER_ON,
    CMD_POWER_OFF,
    CMD_GET_HW_INFO,
    CMD_GET_DOOR_BATTERY,
    CMD_HAS_REMOTE_ID,
    CMD_HAS_REMOTE_KEY,
    CMD_CHECK_RESET_REASON,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SCHEDULE,
    CMD_SET_SCHEDULE,
    CMD_DELETE_SCHEDULE,
    CMD_GET_TIMEZONE,
    CMD_SET_TIMEZONE,
    CMD_GET_HOLD_TIME,
    CMD_SET_HOLD_TIME,
    # Fields
    FIELD_SUCCESS,
    FIELD_DOOR_STATUS,
    FIELD_SETTINGS,
    FIELD_POWER,
    FIELD_INSIDE,
    FIELD_OUTSIDE,
    FIELD_AUTO,
    FIELD_HAS_REMOTE_ID,
    FIELD_HAS_REMOTE_KEY,
    FIELD_RESET_REASON,
    # Hardware/firmware version fields
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
    FIELD_HW_VERSION,
    FIELD_HW_REVISION,
    # Door states
    DOOR_STATE_IDLE,
    DOOR_STATE_CLOSED,
    DOOR_STATE_HOLDING,
    DOOR_STATE_KEEPUP,
    DOOR_STATE_RISING,
    DOOR_STATE_SLOWING,
    # Priorities
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    PRIORITY_LOW,
)
from .schedule import (
    compress_schedule,
    validate_schedule_entry,
    schedule_entry_content_key,
    compute_schedule_diff,
    schedule_template,
    week_0_mon_to_sun,
    week_0_sun_to_mon,
)
from .tz_utils import (
    async_init_timezone_cache,
    init_timezone_cache_sync,
    is_cache_initialized,
    get_available_timezones,
    get_posix_tz_string,
    find_iana_for_posix,
    parse_posix_tz_string,
)

__version__ = "0.3.0"
__all__ = [
    # Door facade
    "PowerPetDoor",
    "DoorStatus",
    "NotificationSettings",
    "BatteryInfo",
    "Schedule",
    "ScheduleTime",
    # Client
    "PowerPetDoorClient",
    "PrioritizedMessage",
    "find_end",
    "make_bool",
    # Schedule utilities
    "compress_schedule",
    "validate_schedule_entry",
    "schedule_entry_content_key",
    "compute_schedule_diff",
    "schedule_template",
    "week_0_mon_to_sun",
    "week_0_sun_to_mon",
    # Constants - Message types
    "COMMAND",
    "CONFIG",
    "PING",
    "PONG",
    "DOOR_STATUS",
    # Constants - Message envelope
    "FIELD_MSG_ID",
    "FIELD_MSG_ID_RESPONSE",
    "FIELD_DIRECTION",
    "FIELD_CMD",
    "PHONE_TO_DOOR",
    "DOOR_TO_PHONE",
    # Constants - Commands
    "CMD_OPEN",
    "CMD_OPEN_AND_HOLD",
    "CMD_CLOSE",
    "CMD_GET_SETTINGS",
    "CMD_GET_SENSORS",
    "CMD_GET_POWER",
    "CMD_GET_AUTO",
    "CMD_GET_DOOR_STATUS",
    "CMD_GET_DOOR_OPEN_STATS",
    "CMD_ENABLE_INSIDE",
    "CMD_DISABLE_INSIDE",
    "CMD_ENABLE_OUTSIDE",
    "CMD_DISABLE_OUTSIDE",
    "CMD_ENABLE_AUTO",
    "CMD_DISABLE_AUTO",
    "CMD_POWER_ON",
    "CMD_POWER_OFF",
    "CMD_GET_HW_INFO",
    "CMD_GET_DOOR_BATTERY",
    "CMD_HAS_REMOTE_ID",
    "CMD_HAS_REMOTE_KEY",
    "CMD_CHECK_RESET_REASON",
    "CMD_GET_SCHEDULE_LIST",
    "CMD_GET_SCHEDULE",
    "CMD_SET_SCHEDULE",
    "CMD_DELETE_SCHEDULE",
    "CMD_GET_TIMEZONE",
    "CMD_SET_TIMEZONE",
    "CMD_GET_HOLD_TIME",
    "CMD_SET_HOLD_TIME",
    # Constants - Fields
    "FIELD_SUCCESS",
    "FIELD_DOOR_STATUS",
    "FIELD_SETTINGS",
    "FIELD_POWER",
    "FIELD_INSIDE",
    "FIELD_OUTSIDE",
    "FIELD_AUTO",
    "FIELD_HAS_REMOTE_ID",
    "FIELD_HAS_REMOTE_KEY",
    "FIELD_RESET_REASON",
    # Constants - Hardware/firmware version fields
    "FIELD_FW_MAJOR",
    "FIELD_FW_MINOR",
    "FIELD_FW_PATCH",
    "FIELD_HW_VERSION",
    "FIELD_HW_REVISION",
    # Constants - Door states
    "DOOR_STATE_IDLE",
    "DOOR_STATE_CLOSED",
    "DOOR_STATE_HOLDING",
    "DOOR_STATE_KEEPUP",
    "DOOR_STATE_RISING",
    "DOOR_STATE_SLOWING",
    # Constants - Priorities
    "PRIORITY_CRITICAL",
    "PRIORITY_HIGH",
    "PRIORITY_MEDIUM",
    "PRIORITY_LOW",
    # Timezone utilities
    "async_init_timezone_cache",
    "init_timezone_cache_sync",
    "is_cache_initialized",
    "get_available_timezones",
    "get_posix_tz_string",
    "find_iana_for_posix",
    "parse_posix_tz_string",
]
