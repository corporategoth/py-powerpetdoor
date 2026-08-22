# Power Pet Door Wire Protocol

This document describes the network protocol used by Power Pet Door devices. For information about how the door operates and what settings mean, see [operation.md](operation.md).

## Table of Contents

- [Connection](#connection)
- [Message Format](#message-format)
- [Message Types](#message-types)
- [Data Formats](#data-formats)
- [Keepalive](#keepalive)
- [Commands Reference](#commands-reference)
  - [Door Control](#door-control)
  - [Sensor Control](#sensor-control)
  - [Power Control](#power-control)
  - [Safety Settings](#safety-settings)
  - [Configuration](#configuration)
  - [Query Commands](#query-commands)
  - [Schedule Commands](#schedule-commands)
  - [Diagnostic Commands](#diagnostic-commands)
- [Settings Fields](#settings-fields)
- [Notification Events](#notification-events)
- [Schedule Format](#schedule-format)
- [Door Status Values](#door-status-values)

---

## Connection

| Parameter | Value |
|-----------|-------|
| Transport | TCP |
| Default Port | 3000 |
| Encoding | JSON (ASCII) |
| Connection Limit | Single client only |
| Message Framing | Brace-matched JSON objects (no terminator) |

The door only accepts one connection at a time.

---

## Message Format

Messages are JSON objects sent back-to-back over the TCP stream. There is
**no message terminator**: neither side sends a trailing newline, and
messages may be separated by optional whitespace. Receivers must frame the
stream by scanning for balanced braces, ignoring braces that appear inside
JSON string values (including backslash-escaped quotes). A message may
arrive split across multiple TCP segments, and one segment may contain
several messages.

Robust receivers should also:

- Skip whitespace/newlines between objects.
- Discard any non-JSON bytes up to the next `{` (resynchronization).
- Cap the amount of un-parsed data they will buffer (this library uses
  64 KiB) and treat overflow as a protocol violation (drop the connection).

All messages include envelope fields for message tracking and direction.

### Envelope Fields

| Field | Type | Description |
|-------|------|-------------|
| `msgId` | int | Message ID (incrementing counter, in requests) |
| `msgID` | int | Message ID echo (in responses, note different casing) |
| `dir` | string | Direction: `"p2d"` (phone to door) or `"d2p"` (door to phone) |

### Request (client to door)

**Command format** (actions that change state):
```json
{"cmd": "COMMAND_NAME", "msgId": 1, "dir": "p2d", ...params}
```

**Config format** (queries and configuration):
```json
{"config": "COMMAND_NAME", "msgId": 2, "dir": "p2d", ...params}
```

### Response (door to client)

```json
{"CMD": "COMMAND_NAME", "msgID": 1, "dir": "d2p", "success": "true", ...response_data}
```

or on error:

```json
{"CMD": "COMMAND_NAME", "msgID": 1, "dir": "d2p", "success": "false", "reason": "error message"}
```

Note: Response includes `CMD` echoing the command name, and `success` is a string `"true"`/`"false"`.

Unknown or unsupported commands are answered with the error envelope
(`"success": "false"`, `"reason": "Unknown command"`) rather than being
silently accepted.

---

## Message Types

| Type | Field | Usage |
|------|-------|-------|
| Command | `"cmd"` | Actions: OPEN, CLOSE, ENABLE_*, DISABLE_*, POWER_* |
| Config | `"config"` | Queries and settings: GET_*, SET_* |
| Ping | `"PING"` | Keepalive request |
| Pong | `"PONG"` | Keepalive response |
| Door Status | `"DOOR_STATUS"` | Unsolicited status update |

---

## Data Formats

### Boolean Values

Most boolean settings use **string** values `"0"` and `"1"`, not JSON boolean:
```json
{"inside": "1", "outside": "0", "power_state": "1"}
```

The `success` field also uses strings: `"true"` or `"false"`.

### Timezone Format

Timezones use **POSIX format**, not IANA names:
```json
{"tz": "EST5EDT,M3.2.0,M11.1.0"}
```

Format: `STDoffset[DST[offset],start,end]`
- `EST5EDT` - Standard time is EST (UTC-5), daylight time is EDT
- `M3.2.0` - DST starts month 3 (March), week 2, day 0 (Sunday)
- `M11.1.0` - DST ends month 11 (November), week 1, day 0 (Sunday)

### Time Values

Hold time is in **centiseconds** (1/100 second):
```json
{"holdTime": 200}
```
A value of 200 means 2 seconds.

---

## Keepalive

**Request**:
```json
{"PING": "", "msgId": 1, "dir": "p2d"}
```

**Response**:
```json
{"CMD": "PONG", "PONG": "", "success": "true", "dir": "d2p"}
```

Typical interval: 30 seconds

---

## Commands Reference

> **Note**: In the examples below, envelope fields (`msgId`, `msgID`, `dir`) are omitted for brevity. See [Message Format](#message-format) for the complete structure.

### Door Control

| Command | Type | Description |
|---------|------|-------------|
| `OPEN` | cmd | Open door (auto-closes after hold time) |
| `OPEN_AND_HOLD` | cmd | Open door and keep open until CLOSE |
| `CLOSE` | cmd | Close the door |

**Request**:
```json
{"cmd": "OPEN"}
{"cmd": "OPEN_AND_HOLD"}
{"cmd": "CLOSE"}
```

**Response**:
```json
{"success": "true", "door_status": "DOOR_RISING"}
```

### Sensor Control

| Command | Type | Description |
|---------|------|-------------|
| `ENABLE_INSIDE` | cmd | Enable inside sensor |
| `DISABLE_INSIDE` | cmd | Disable inside sensor |
| `ENABLE_OUTSIDE` | cmd | Enable outside sensor |
| `DISABLE_OUTSIDE` | cmd | Disable outside sensor |
| `GET_SENSORS` | config | Get sensor states |

**Request**:
```json
{"cmd": "ENABLE_INSIDE"}
{"cmd": "DISABLE_OUTSIDE"}
{"config": "GET_SENSORS"}
```

**Response** (GET_SENSORS):
```json
{"success": "true", "inside": "1", "outside": "1"}
```

### Power Control

| Command | Type | Description |
|---------|------|-------------|
| `POWER_ON` | cmd | Turn door power on |
| `POWER_OFF` | cmd | Turn door power off |
| `GET_POWER` | config | Get power state |

**Request**:
```json
{"cmd": "POWER_ON"}
{"cmd": "POWER_OFF"}
{"config": "GET_POWER"}
```

**Response** (GET_POWER):
```json
{"success": "true", "power_state": "1"}
```

### Safety Settings

| Command | Type | Description |
|---------|------|-------------|
| `ENABLE_AUTORETRACT` | cmd | Enable auto-retract |
| `DISABLE_AUTORETRACT` | cmd | Disable auto-retract |
| `GET_AUTORETRACT` | config | Get autoretract state |
| `ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK` | cmd | Enable outside sensor safety lock |
| `DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK` | cmd | Disable outside sensor safety lock |
| `GET_OUTSIDE_SENSOR_SAFETY_LOCK` | config | Get safety lock state |
| `ENABLE_CMD_LOCKOUT` | cmd | Enable command lockout |
| `DISABLE_CMD_LOCKOUT` | cmd | Disable command lockout |
| `GET_CMD_LOCKOUT` | config | Get command lockout state |

**Request**:
```json
{"cmd": "ENABLE_AUTORETRACT"}
{"cmd": "DISABLE_AUTORETRACT"}
{"config": "GET_AUTORETRACT"}
{"cmd": "ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK"}
{"config": "GET_OUTSIDE_SENSOR_SAFETY_LOCK"}
{"cmd": "ENABLE_CMD_LOCKOUT"}
{"config": "GET_CMD_LOCKOUT"}
```

**Response**:
```json
{"success": "true", "settings": {"doorOptions": "1"}}
{"success": "true", "settings": {"outsideSensorSafetyLock": "0"}}
{"success": "true", "settings": {"allowCmdLockout": "0"}}
```

### Configuration

| Command | Type | Parameters | Description |
|---------|------|------------|-------------|
| `GET_HOLD_TIME` | config | - | Get hold time |
| `SET_HOLD_TIME` | config | `holdTime` | Set hold time (centiseconds) |
| `GET_TIMEZONE` | config | - | Get timezone |
| `SET_TIMEZONE` | config | `tz` | Set timezone (POSIX format) |
| `GET_NOTIFICATIONS` | config | - | Get notification settings |
| `SET_NOTIFICATIONS` | config | *(see below)* | Set notification settings |
| `GET_SENSOR_TRIGGER_VOLTAGE` | config | - | Get sensor trigger voltage |
| `SET_SENSOR_TRIGGER_VOLTAGE` | config | `sensorTriggerVoltage` | Set sensor trigger voltage |
| `GET_SLEEP_SENSOR_TRIGGER_VOLTAGE` | config | - | Get sleep sensor trigger voltage |
| `SET_SLEEP_SENSOR_TRIGGER_VOLTAGE` | config | `sleepSensorTriggerVoltage` | Set sleep sensor trigger voltage |

**GET_HOLD_TIME**:
```json
{"config": "GET_HOLD_TIME"}
```
Response: `{"success": "true", "holdTime": 1500}`

**SET_HOLD_TIME**:
```json
{"config": "SET_HOLD_TIME", "holdTime": 1500}
```
Note: Value is in **centiseconds** (1500 = 15 seconds)

**GET_TIMEZONE**:
```json
{"config": "GET_TIMEZONE"}
```
Response: `{"success": "true", "tz": "EST5EDT,M3.2.0,M11.1.0"}`

**SET_TIMEZONE**:
```json
{"config": "SET_TIMEZONE", "tz": "EST5EDT,M3.2.0,M11.1.0"}
```

**GET_NOTIFICATIONS**:
```json
{"config": "GET_NOTIFICATIONS"}
```

**SET_NOTIFICATIONS**:
```json
{
  "config": "SET_NOTIFICATIONS",
  "sensorOnIndoorNotificationsEnabled": "1",
  "sensorOffIndoorNotificationsEnabled": "0",
  "sensorOnOutdoorNotificationsEnabled": "1",
  "sensorOffOutdoorNotificationsEnabled": "0",
  "lowBatteryNotificationsEnabled": "1"
}
```

**GET/SET_SENSOR_TRIGGER_VOLTAGE**:
```json
{"config": "GET_SENSOR_TRIGGER_VOLTAGE"}
{"config": "SET_SENSOR_TRIGGER_VOLTAGE", "sensorTriggerVoltage": 50}
{"config": "GET_SLEEP_SENSOR_TRIGGER_VOLTAGE"}
{"config": "SET_SLEEP_SENSOR_TRIGGER_VOLTAGE", "sleepSensorTriggerVoltage": 50}
```

### Query Commands

| Command | Type | Description |
|---------|------|-------------|
| `GET_DOOR_STATUS` | config | Get current door state |
| `GET_SETTINGS` | config | Get all settings |
| `GET_HW_INFO` | config | Get hardware/firmware info |
| `GET_DOOR_BATTERY` | config | Get battery status |
| `GET_DOOR_OPEN_STATS` | config | Get open cycle and retract counts |
| `GET_TIMERS_ENABLED` | config | Get auto/schedule mode state |

**GET_DOOR_STATUS**:
```json
{"config": "GET_DOOR_STATUS"}
```
Response:
```json
{"success": "true", "door_status": "DOOR_CLOSED"}
```

**GET_SETTINGS**:
```json
{"config": "GET_SETTINGS"}
```
Response:
```json
{
  "success": "true",
  "settings": {
    "power_state": "1",
    "inside": "1",
    "outside": "1",
    "timersEnabled": "0",
    "outsideSensorSafetyLock": "0",
    "allowCmdLockout": "0",
    "doorOptions": "1",
    "holdOpenTime": 1500,
    "tz": "EST5EDT,M3.2.0,M11.1.0",
    "sensorTriggerVoltage": 50,
    "sleepSensorTriggerVoltage": 50
  }
}
```

Note: within the settings object the hold time key is `holdOpenTime`; the
dedicated `GET_HOLD_TIME`/`SET_HOLD_TIME` commands use `holdTime`. Both are
in centiseconds.

**GET_HW_INFO**:
```json
{"config": "GET_HW_INFO"}
```
Response:
```json
{
  "success": "true",
  "fwInfo": {
    "ver": "1.2.3",
    "rev": "abc123",
    "fw_maj": 1,
    "fw_min": 2,
    "fw_pat": 3
  }
}
```

**GET_DOOR_BATTERY**:
```json
{"config": "GET_DOOR_BATTERY"}
```
Response:
```json
{
  "success": "true",
  "batteryPercent": 85,
  "batteryPresent": "1",
  "acPresent": "1"
}
```

**GET_DOOR_OPEN_STATS**:
```json
{"config": "GET_DOOR_OPEN_STATS"}
```
Response:
```json
{
  "success": "true",
  "totalOpenCycles": 1234,
  "totalAutoRetracts": 5
}
```

### Schedule Commands

| Command | Type | Parameters | Description |
|---------|------|------------|-------------|
| `GET_SCHEDULE_LIST` | config | - | Get all schedules |
| `SET_SCHEDULE_LIST` | config | `schedules` | Set all schedules |
| `GET_SCHEDULE` | config | `index` | Get specific schedule |
| `SET_SCHEDULE` | config | `schedule` | Create/update schedule |
| `DELETE_SCHEDULE` | config | `index` | Delete schedule |

**Request**:
```json
{"config": "GET_SCHEDULE_LIST"}
{"config": "GET_SCHEDULE", "index": 0}
{"config": "SET_SCHEDULE", "schedule": {...}}
{"config": "DELETE_SCHEDULE", "index": 0}
```

See [Schedule Format](#schedule-format) for the schedule object structure.

The simulator validates schedules coming off the wire before storing them:
`index` must be an integer in 0-255, `daysOfWeek` must be a 7-element list of
0/1 flags (or the legacy integer bitmask), and `hour`/`min` must be integers
in 0-23 / 0-59. Day flags are read as flags, not truthily, so a string `"0"`
disables the day. When `inside` or `outside` is true the corresponding
`*_start_time`/`*_end_time` objects are **required**, each carrying an
`hour` (`min` defaults to 0): an absent window is rejected rather than
silently materialized as 06:00-22:00. A malformed schedule is rejected with
the standard error envelope (`"success": "false"` plus a `reason` naming the
offending field) and nothing is stored; `SET_SCHEDULE_LIST` rejects the whole
batch rather than loading it partially.

The same validate-before-storing rule applies to every other `SET_*` command,
so one malformed packet can never leave the simulator in a state a later
command chokes on:

| Command | Accepted values |
|---------|-----------------|
| `SET_HOLD_TIME` | a finite number of centiseconds in 0-90000 (`Infinity`/`NaN`, strings and containers are rejected) |
| `SET_TIMEZONE` | a string of at most 128 characters |
| `SET_SENSOR_TRIGGER_VOLTAGE` | a finite number in 0-65535 |
| `SET_SLEEP_SENSOR_TRIGGER_VOLTAGE` | a finite number in 0-65535 |
| `SET_NOTIFICATIONS` | each supplied field must be a 0/1 flag (`"1"`/`1`/`true` and `"0"`/`0`/`false`); one bad field rejects the whole message, so a notification set is never half-applied |

A rejection answers `{"success": "false", "reason": "<field> must be ..."}`
and leaves state untouched.

### Diagnostic Commands

| Command | Type | Description |
|---------|------|-------------|
| `HAS_REMOTE_ID` | config | Check if remote ID is set |
| `HAS_REMOTE_KEY` | config | Check if remote key is set |
| `CHECK_RESET_REASON` | config | Get last reset reason |

**Request**:
```json
{"config": "HAS_REMOTE_ID"}
{"config": "HAS_REMOTE_KEY"}
{"config": "CHECK_RESET_REASON"}
```

**Response**:
```json
{"success": "true", "hasRemoteId": "1"}
{"success": "true", "hasRemoteKey": "1"}
{"success": "true", "resetReason": "POWER_ON"}
```

---

## Settings Fields

| Field | Wire Name | Type | Description |
|-------|-----------|------|-------------|
| Power | `power_state` | "0"/"1" | Door power on/off |
| Inside Sensor | `inside` | "0"/"1" | Inside sensor enabled |
| Outside Sensor | `outside` | "0"/"1" | Outside sensor enabled |
| Timers/Auto | `timersEnabled` | "0"/"1" | Schedule mode enabled |
| Safety Lock | `outsideSensorSafetyLock` | "0"/"1" | Outside sensor safety lock |
| Command Lockout | `allowCmdLockout` | "0"/"1" | Command lockout enabled |
| Autoretract | `doorOptions` | "0"/"1" | Auto-retract on obstruction |
| Hold Time | `holdOpenTime` | int | Hold time in centiseconds (the standalone GET/SET_HOLD_TIME commands use `holdTime`) |
| Timezone | `tz` | string | POSIX timezone string |
| Sensor Voltage | `sensorTriggerVoltage` | int | Sensor threshold |
| Sleep Sensor Voltage | `sleepSensorTriggerVoltage` | int | Sleep mode sensor threshold |

---

## Notification Events

### Notification Settings Fields

| Field | Description |
|-------|-------------|
| `sensorOnIndoorNotificationsEnabled` | Inside sensor triggered |
| `sensorOffIndoorNotificationsEnabled` | Inside sensor deactivated |
| `sensorOnOutdoorNotificationsEnabled` | Outside sensor triggered |
| `sensorOffOutdoorNotificationsEnabled` | Outside sensor deactivated |
| `lowBatteryNotificationsEnabled` | Battery level low |

### Notification Messages (door to client)

Notification events are device-initiated and use a **bare envelope**:
they carry no `CMD`, `success`, or `msgID` fields. The event name appears
as a key with an empty-string value; sensor events also carry a
`sensorState` of `"on"` or `"off"`.

```json
{"SENSOR_INDOOR": "", "sensorState": "on"}
{"SENSOR_OUTDOOR": "", "sensorState": "off"}
{"LOW_BATTERY": ""}
```

Clients should also tolerate CMD-style variants of these events
(`{"CMD": "SENSOR_INDOOR", "success": "true", "sensorState": "on"}`)
without treating them as command responses.

---

## Schedule Format

```json
{
  "index": 0,
  "enabled": "1",
  "inside": true,
  "outside": false,
  "daysOfWeek": [1, 1, 1, 1, 1, 1, 1],
  "in_start_time": {"hour": 6, "min": 0},
  "in_end_time": {"hour": 22, "min": 0},
  "out_start_time": {"hour": 0, "min": 0},
  "out_end_time": {"hour": 0, "min": 0}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `index` | int | Schedule slot number (0-based) |
| `enabled` | "0"/"1" | Whether schedule is active |
| `inside` | bool | This schedule controls inside sensor |
| `outside` | bool | This schedule controls outside sensor |
| `daysOfWeek` | [int] | [Sun, Mon, Tue, Wed, Thu, Fri, Sat], 1=active |
| `in_start_time` | {hour, min} | Inside sensor start time |
| `in_end_time` | {hour, min} | Inside sensor end time |
| `out_start_time` | {hour, min} | Outside sensor start time |
| `out_end_time` | {hour, min} | Outside sensor end time |

Note: Each schedule controls ONE sensor. Set times for that sensor; the other sensor's times should be zeros.

---

## Door Status Values

| Value | Description |
|-------|-------------|
| `DOOR_IDLE` | Door is idle |
| `DOOR_CLOSED` | Door is fully closed |
| `DOOR_RISING` | Door is opening |
| `DOOR_SLOWING` | Door is slowing near top |
| `DOOR_HOLDING` | Door is open, hold timer running |
| `DOOR_KEEPUP` | Door is locked open |
| `DOOR_CLOSING_TOP_OPEN` | Door closing from fully open |
| `DOOR_CLOSING_MID_OPEN` | Door closing from mid position |

