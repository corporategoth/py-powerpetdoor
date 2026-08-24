# Power Pet Door Wire Protocol

This document describes the network protocol used by Power Pet Door devices.
For information about how the door operates and what settings mean, see
[operation.md](operation.md).

## How to read this document

Every claim below is tagged with how it is known:

| Tag | Meaning |
|-----|---------|
| **[V]** | **Verified** against a physical Power Pet Door running firmware **1.7.18** (`fwInfo: ver=1 rev=1 fw_maj=1 fw_min=7 fw_pat=18`), by sending the frame and recording the reply |
| **[R]** | **Reverse-engineered** from observation of the vendor app or from this library's history, and **not** confirmed against hardware |

That distinction exists because this document was wrong for years in ways
nothing in the test suite could catch: it described newline-terminated
framing, `"0"`/`"1"` string flags everywhere, `SET_NOTIFICATIONS` as a set of
top-level fields, `SET_SCHEDULE` without an `index`, `doorOptions` as a
boolean, `hasRemoteId`/`hasRemoteKey` as the remote-pairing field names, and
`sensorTriggerVoltage` as the field its own setter takes. All of those were
**[R]**, all of them were believed, and every one of them is now known to be
wrong. Where a **[V]** statement and an **[R]** statement disagree, the
**[V]** one wins; nothing here may be "tidied" to make the two agree.

An **[R]** tag is not a licence to change what the library sends. Several
**[R]** spellings have been running against real doors since v0.1.0, which is
its own kind of evidence.

## Table of Contents

- [Connection](#connection)
- [Message Format](#message-format)
- [Message Types](#message-types)
- [Data Formats](#data-formats)
- [Value spellings](#value-spellings)
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
  - [The door clock](#the-door-clock)
- [Commands that do not exist](#commands-that-do-not-exist)
- [The vendor app, and why your setting changed back](#the-vendor-app-and-why-your-setting-changed-back)
- [What the app calls these settings](#what-the-app-calls-these-settings)
- [What this protocol cannot do](#what-this-protocol-cannot-do)
- [Settings Fields](#settings-fields)
- [Notification Events](#notification-events)
- [Schedule Format](#schedule-format)
- [Door Status Values](#door-status-values)

---

## Connection

| Parameter | Value | |
|-----------|-------|---|
| Transport | TCP | **[V]** |
| Default Port | 3000 | **[V]** |
| Encoding | JSON (ASCII) | **[V]** |
| Connection Limit | Single client only | **[V]** |
| Message Framing | Brace-matched JSON objects, **no terminator** | **[V]** |

### Single connection, and the field-debugging trap it creates

**[V]** The door serves **one** connection. A second connection does not get
a refusal, a reset, or an error frame — the door simply stops answering the
wire properly, and *both* sides see long waits and apparent timeouts. On an
idle, exclusive connection every command answered in **0.03–0.53 s**, so a
timeout an order of magnitude above that is almost always a second client
(another app, a stale process, a home-automation integration you forgot was
running), not a slow or broken door.

If you are debugging "the door randomly stops responding": close every other
connection first. Close cleanly between sessions.

**The simulator deliberately does not reproduce this.** It accepts many
clients and serves each one as if it were exclusive. See
[simulator.md](simulator.md) for why, and do not "fix" it.

---

## Message Format

**[V]** Messages are JSON objects sent back-to-back over the TCP stream.
There is **no message terminator**: neither side sends a trailing newline,
and a response ends at its closing `}` with nothing after it. Receivers must
frame the stream by scanning for balanced braces, ignoring braces that appear
inside JSON string values (including backslash-escaped quotes). A message may
arrive split across multiple TCP segments, and one segment may contain
several messages.

The device accepts a request with no trailing newline. **[V]**

Robust receivers should also:

- Skip whitespace/newlines between objects.
- Discard any non-JSON bytes up to the next `{` (resynchronization).
- Cap the amount of un-parsed data they will buffer (this library uses
  64 KiB) and treat overflow as a protocol violation (drop the connection).

All messages include envelope fields for message tracking and direction.

### Envelope Fields

| Field | Type | Description | |
|-------|------|-------------|---|
| `msgId` | int | Message ID (incrementing counter, in **requests**) | **[V]** |
| `msgID` | int | Message ID echo (in **responses**, note the different casing) | **[V]** |
| `dir` | string | Direction: `"p2d"` (phone to door) or `"d2p"` (door to phone) | **[V]** |

### Request (client to door)

**Command format** (door motion only — see [Message Types](#message-types)):
```json
{"cmd": "COMMAND_NAME", "msgId": 1, "dir": "p2d", ...params}
```

**Config format** (everything else):
```json
{"config": "COMMAND_NAME", "msgId": 2, "dir": "p2d", ...params}
```

### Response (door to client)

```json
{"CMD": "COMMAND_NAME", "msgID": 1, "dir": "d2p", "success": "true", ...response_data}
```

**[V]** `success` is the **string** `"true"` / `"false"`, never a JSON
boolean. `CMD` echoes the command name.

### Failure responses carry no `msgID`

**[V]** On failure the door answers:

```json
{"success": "false", "dir": "d2p", "CMD": "COMMAND_NAME"}
```

Note what is **missing**: there is no `msgID`. A client that pairs replies to
requests by id therefore cannot pair a failure with anything, and will sit
waiting for a reply that has already arrived until its own timeout fires.

A correct client needs a second rule: a response whose `CMD` matches the
command currently in flight belongs to that command whether or not it
carries an id. The door answers one command at a time, so this is
unambiguous. (This library implements exactly that; see
`PowerPetDoorClient.process_message`.)

**[R]** Whether a real door ever supplies a `reason` string is unknown — none
was observed. This project's simulator always includes one, as a debugging
aid, and clients should treat it as optional:

```json
{"CMD": "COMMAND_NAME", "dir": "d2p", "success": "false", "reason": "error message"}
```

Unknown or unsupported commands are answered with the same failure envelope
rather than being silently accepted. **[V]**

---

## Message Types

These are the top-level **envelope keys** that identify what a frame is:

| Type | Field | Usage | |
|------|-------|-------|---|
| Command | `"cmd"` | **Door motion only**: `OPEN`, `OPEN_AND_HOLD`, `CLOSE` | **[V]** |
| Config | `"config"` | **Everything else**, including the individual setting commands | **[V]** |
| Ping | `"PING"` | Keepalive request | **[V]** |
| Pong | `"PONG"` | Keepalive response | **[V]** |

### `config` vs `cmd` is not cosmetic

**[V]** The two keys are **not** interchangeable, and getting it wrong fails
silently in the sense that matters — the door answers, the answer says
`"false"`, and nothing happens:

```json
{"cmd": "ENABLE_INSIDE"}
```
→ `{"success": "false", ...}`, nothing changed.

```json
{"config": "ENABLE_INSIDE"}
```
→ `{"inside": 1, "success": "true", ...}`, sensor enabled.

Only `OPEN`, `OPEN_AND_HOLD` and `CLOSE` are accepted under `cmd`. Every
other command in this document — including `ENABLE_*`, `DISABLE_*`,
`POWER_ON` and `POWER_OFF`, which earlier versions of this document listed as
`cmd` — must be sent as `config`.

In this library the mapping lives in one place, `COMMAND_ENVELOPE_COMMANDS`
in `powerpetdoor/const.py`, reached through `envelope_for_command()`.

`DOOR_STATUS` is **not** an envelope key — it is a `CMD` *value* carried by
an unsolicited device push. See
[Unsolicited Door Status](#unsolicited-door-status).

---

## Data Formats

### Timezone Format

**[V]** Timezones use **POSIX format**, not IANA names:
```json
{"tz": "EST5EDT,M3.2.0,M11.1.0"}
```

Format: `STDoffset[DST[offset],start,end]`
- `EST5EDT` - Standard time is EST (UTC-5), daylight time is EDT
- `M3.2.0` - DST starts month 3 (March), week 2, day 0 (Sunday)
- `M11.1.0` - DST ends month 11 (November), week 1, day 0 (Sunday)

### Time Values

**[V]** Hold time is in **centiseconds** (1/100 second):
```json
{"holdTime": 200}
```
A value of 200 means 2 seconds. The probed unit was set to 200.

---

## Value spellings

**[V]** **The device is not internally consistent.** The same concept is
spelled differently depending on which command answered. This is not a
mistake in the capture and must not be normalized away — it is the reason
every reader in this project (and any other correct client) has to be
liberal, accepting `true`/`"true"`/`1`/`"1"` interchangeably for a flag.

| Where | Field(s) | Type on the wire | |
|---|---|---|---|
| `GET_SETTINGS.settings` | `inside`, `outside`, `power_state`, `timersEnabled`, `outsideSensorSafetyLock`, `allowCmdLockout` | string `"true"` / `"false"` | **[V]** |
| `GET_SETTINGS.settings` | `holdOpenTime`, `sensorTriggerVoltage`, `sleepSensorTriggerVoltage`, `doorOptions` | int | **[V]** |
| `GET_SETTINGS.settings`, `GET_TIMEZONE` | `tz` | POSIX string, **never** an IANA name | **[V]** |
| `GET_TIME` | `time` | C `asctime()` string, local to `tz` | **[V]** |
| `GET_SENSORS` | `inside`, `outside` | int `1` / `0` | **[V]** |
| individual setting reply, e.g. `ENABLE_INSIDE` | the echoed field | int `1` / `0` | **[V]** |
| `GET_SCHEDULE.schedule` | `enabled`, `inside`, `outside` | int `1` / `0` | **[V]** |
| `GET_SCHEDULE.schedule` | `daysOfWeek` | list of int `1` / `0` | **[V]** |
| `GET_NOTIFICATIONS.notifications` | all five flags | string `"true"` / `"false"` | **[V]** |
| `GET_HOLD_TIME` | `holdTime` | int (centiseconds) | **[V]** |
| `GET_DOOR_BATTERY` | `batteryPercent` | int | **[V]** |
| `GET_DOOR_BATTERY` | `acPresent`, `batteryPresent` | string `"true"` / `"false"` | **[V]** |
| `GET_HW_INFO` | `fwInfo` | object of **ints** — including `ver` and `rev` | **[V]** |
| `HAS_REMOTE_ID` / `HAS_REMOTE_KEY` | `has_id` / `has_key` | string `"true"` / `"false"` | **[V]** |
| any reply | `success` | string `"true"` / `"false"` | **[V]** |

Note that `inside` is a **string** in `GET_SETTINGS`, an **int** in
`GET_SENSORS`, and an **int** in a schedule entry. All three are the same
door, in the same session.

### `doorOptions` is a bitfield

**[V]** `doorOptions` is an **integer bitfield**, not the `"0"`/`"1"` flag
this document used to claim:

| Action | Resulting `doorOptions` |
|---|---|
| `DISABLE_AUTORETRACT` | `0` |
| `ENABLE_AUTORETRACT` | `2` |

Auto-retract is therefore **bit 1** (value 2) — exported here as
`DOOR_OPTION_AUTORETRACT`. The other bits are **unidentified**: do not read
`doorOptions == 2` as "auto-retract and nothing else", and do not read the
field by plain truthiness, which happens to work today only because `2` is
truthy and would misreport the moment any other bit is set.

**[V]** `ENABLE_AUTORETRACT` / `DISABLE_AUTORETRACT` reply with the **whole**
`settings` object, not just the field they changed.

---

## Keepalive

**[V]** The `PING` value is an **opaque correlation token** chosen by the
client (this library sends the current wall-clock time in milliseconds, as a
string). The device echoes it back verbatim as the `PONG` value.

**Request**:
```json
{"PING": "1710000000123", "msgId": 1, "dir": "p2d"}
```

**Response**:
```json
{"CMD": "PONG", "PONG": "1710000000123", "success": "true", "dir": "d2p"}
```

**[V]** **A `PONG` carries no `msgID`.** The echoed token is the whole
correlation mechanism here; a client must not expect the response id it gets
on ordinary replies.

**The `PONG` value must be the exact `PING` value.** The client compares
them and only counts an exact match as a reply; a mismatched or empty
`PONG` is counted as a failed ping, and three failures in a row drop the
connection (`MAX_FAILED_PINGS = 3`). At the default 30 s interval that is a
hard disconnect roughly every 90 seconds, reported as `Last PING not
responded to 3 times.` — so an alternate implementation that answers
`{"PONG": ""}` looks like a flaky network rather than a protocol mismatch.

Typical interval: 30 seconds

---

## Commands Reference

> **Note**: In the examples below, envelope fields (`msgId`, `msgID`, `dir`)
> are omitted for brevity. See [Message Format](#message-format) for the
> complete structure.

### Door Control

| Command | Type | Description | |
|---------|------|-------------|---|
| `OPEN` | cmd | Open door (auto-closes after hold time) | **[V]** |
| `OPEN_AND_HOLD` | cmd | Open door and keep open until CLOSE | **[V]** |
| `CLOSE` | cmd | Close the door | **[V]** |

These three are the **only** commands accepted under the `cmd` key.

**Request**:
```json
{"cmd": "OPEN"}
{"cmd": "OPEN_AND_HOLD"}
{"cmd": "CLOSE"}
```

**Response** (**[R]** — door motion was not provoked on the probed unit):
```json
{"success": "true", "door_status": "DOOR_RISING"}
```

### Sensor Control

| Command | Type | Description | |
|---------|------|-------------|---|
| `ENABLE_INSIDE` | config | Enable inside sensor | **[V]** |
| `DISABLE_INSIDE` | config | Disable inside sensor | **[V]** |
| `ENABLE_OUTSIDE` | config | Enable outside sensor | **[V]** |
| `DISABLE_OUTSIDE` | config | Disable outside sensor | **[V]** |
| `GET_SENSORS` | config | Get sensor states | **[V]** |

**Request**:
```json
{"config": "ENABLE_INSIDE"}
{"config": "DISABLE_OUTSIDE"}
{"config": "GET_SENSORS"}
```

**Response** (`ENABLE_INSIDE`, then `GET_SENSORS`) — **ints**, not strings:
```json
{"success": "true", "inside": 1}
{"success": "true", "inside": 1, "outside": 1}
```

### Power Control

| Command | Type | Description | |
|---------|------|-------------|---|
| `POWER_ON` | config | Turn door power on | **[R]** envelope inferred from the other setting commands |
| `POWER_OFF` | config | Turn door power off | **[R]** |
| `GET_POWER` | config | Get power state | **[R]** |

**Request**:
```json
{"config": "POWER_ON"}
{"config": "POWER_OFF"}
{"config": "GET_POWER"}
```

**Response** (**[R]**, spelled like the other individual setting replies):
```json
{"success": "true", "power_state": 1}
```

Power state is readable for certain from `GET_SETTINGS`, as the string
`power_state`. **[V]**

### Safety Settings

| Command | Type | Description | |
|---------|------|-------------|---|
| `ENABLE_AUTORETRACT` | config | Enable auto-retract | **[V]** |
| `DISABLE_AUTORETRACT` | config | Disable auto-retract | **[V]** |
| `ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK` | config | Enable outside sensor safety lock | **[R]** |
| `DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK` | config | Disable outside sensor safety lock | **[R]** |
| `ENABLE_CMD_LOCKOUT` | config | Enable command lockout | **[R]** |
| `DISABLE_CMD_LOCKOUT` | config | Disable command lockout | **[R]** |

The read counterparts `GET_AUTORETRACT`, `GET_OUTSIDE_SENSOR_SAFETY_LOCK` and
`GET_CMD_LOCKOUT` **do not exist** — see
[Commands that do not exist](#commands-that-do-not-exist). Read these three
settings from `GET_SETTINGS` instead.

**Request**:
```json
{"config": "ENABLE_AUTORETRACT"}
{"config": "DISABLE_AUTORETRACT"}
{"config": "ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK"}
{"config": "ENABLE_CMD_LOCKOUT"}
```

**Response** — `ENABLE_AUTORETRACT`/`DISABLE_AUTORETRACT` answer with the
**whole** settings object **[V]**; the safety-lock and command-lockout
toggles were not probed, and this library's simulator answers them with a
one-field `settings` object spelled the way `GET_SETTINGS` spells it **[R]**:
```json
{"success": "true", "settings": {"outsideSensorSafetyLock": "false"}}
{"success": "true", "settings": {"allowCmdLockout": "true"}}
```

### Configuration

| Command | Type | Parameters | Description | |
|---------|------|------------|-------------|---|
| `GET_HOLD_TIME` | config | - | Get hold time | **[V]** |
| `SET_HOLD_TIME` | config | `holdTime` | Set hold time (centiseconds) | **[V]** |
| `GET_TIMEZONE` | config | - | Get timezone | **[V]** |
| `SET_TIMEZONE` | config | `tz` | Set timezone (POSIX format) | **[V]** |
| `GET_NOTIFICATIONS` | config | - | Get notification settings | **[V]** |
| `SET_NOTIFICATIONS` | config | `notifications` | Set notification settings | **[V]** |
| `GET_SENSOR_TRIGGER_VOLTAGE` | config | - | Get sensor trigger voltage | **[V]** |
| `SET_SENSOR_TRIGGER_VOLTAGE` | config | **`voltage`** | Set sensor trigger voltage | **[V]** |
| `GET_SLEEP_SENSOR_TRIGGER_VOLTAGE` | config | - | Get sleep sensor trigger voltage | **[V]** |
| `SET_SLEEP_SENSOR_TRIGGER_VOLTAGE` | config | **`voltage`** | Set sleep sensor trigger voltage | **[V]** |

**GET_HOLD_TIME / SET_HOLD_TIME**:
```json
{"config": "GET_HOLD_TIME"}
{"config": "SET_HOLD_TIME", "holdTime": 1500}
```
Response: `{"success": "true", "holdTime": 1500}`. The value is in
**centiseconds** (1500 = 15 seconds). **[V]**

**GET_TIMEZONE / SET_TIMEZONE**:
```json
{"config": "GET_TIMEZONE"}
{"config": "SET_TIMEZONE", "tz": "EST5EDT,M3.2.0,M11.1.0"}
```
Response: `{"success": "true", "tz": "EST5EDT,M3.2.0,M11.1.0"}` **[V]**

#### The voltage setters take a different field from the one the getters answer

**[V]** This is the one place in the protocol where a setter's parameter is
*not* named after the value it sets. Both setters take **`voltage`**, and
both **reject** the getter's field name:

```json
{"config": "SET_SENSOR_TRIGGER_VOLTAGE", "voltage": 1500}
```
→ `{"sensorTriggerVoltage": 1500, "success": "true", ...}`

```json
{"config": "SET_SENSOR_TRIGGER_VOLTAGE", "sensorTriggerVoltage": 1500}
```
→ `{"success": "false", ...}` — this is what earlier versions of this
document told you to send.

The same applies to `SET_SLEEP_SENSOR_TRIGGER_VOLTAGE`. Units are
**millivolts**; the probed unit sat at 2000 for both and accepted 1500 and
1800 respectively. What they physically tune was not determined — they are
the capacitive sensor trigger thresholds, the second presumably applying in
the door's sleep/low-power state. **[R]**

In this library the asymmetry lives in one place,
`build_set_voltage_message()`.

#### SET_NOTIFICATIONS

> ### ⚠ The most dangerous shape in this protocol
>
> **[V]** `SET_NOTIFICATIONS` has **two** wrong shapes, and the second one
> reports **success** while writing nothing:
>
> | Sent | Result |
> |---|---|
> | flat top-level fields (any value type) | `success: "false"`, nothing written |
> | nested `notifications` object, values as **strings** | **`success: "true"`, the current settings echoed back, and nothing written** |
> | nested `notifications` object, values as **JSON booleans** | applied, new settings echoed back |
>
> The middle row is the trap. The reply is a normal success envelope
> carrying a full notification set, so a client that checks `success` — or
> even one that reads the echoed settings back — sees a healthy write. The
> echoed values are simply the *old* ones. Nothing in the exchange says the
> write was dropped.
>
> If your notification settings "won't stick", this is why.

The correct shape is a **nested object** carrying **all five** flags as
**JSON booleans**:

```json
{
  "config": "SET_NOTIFICATIONS",
  "notifications": {
    "sensorOnIndoorNotificationsEnabled": true,
    "sensorOffIndoorNotificationsEnabled": false,
    "sensorOnOutdoorNotificationsEnabled": true,
    "sensorOffOutdoorNotificationsEnabled": false,
    "lowBatteryNotificationsEnabled": true
  }
}
```

There is no partial form: a client changing one flag must supply the other
four. In this library the shape lives in one place,
`build_set_notifications_message()`.

Note the asymmetry with the read path: `GET_NOTIFICATIONS` answers with the
same five flags as **strings**. **[V]**

**GET_NOTIFICATIONS**:
```json
{"config": "GET_NOTIFICATIONS"}
```
Response:
```json
{
  "success": "true",
  "notifications": {
    "sensorOnIndoorNotificationsEnabled": "true",
    "sensorOffIndoorNotificationsEnabled": "false",
    "sensorOnOutdoorNotificationsEnabled": "true",
    "sensorOffOutdoorNotificationsEnabled": "false",
    "lowBatteryNotificationsEnabled": "true"
  }
}
```

### Query Commands

| Command | Type | Description | |
|---------|------|-------------|---|
| `GET_DOOR_STATUS` | config | Get current door state | **[V]** |
| `GET_SETTINGS` | config | Get all settings | **[V]** |
| `GET_HW_INFO` | config | Get hardware/firmware info | **[V]** |
| `GET_DOOR_BATTERY` | config | Get battery status | **[V]** |
| `GET_DOOR_OPEN_STATS` | config | Get open cycle and retract counts | **[V]** |

**GET_DOOR_STATUS**:
```json
{"config": "GET_DOOR_STATUS"}
```
Response:
```json
{"success": "true", "door_status": "DOOR_CLOSED"}
```

**GET_SETTINGS** — the single most useful command, because it is where the
state of every setting whose dedicated `GET_*` command does not exist can
still be read:
```json
{"config": "GET_SETTINGS"}
```
Response, spelled exactly as the probed unit answered (**[V]**; note the
mixed string/int types):
```json
{
  "success": "true",
  "settings": {
    "power_state": "true",
    "inside": "true",
    "outside": "true",
    "timersEnabled": "false",
    "outsideSensorSafetyLock": "false",
    "allowCmdLockout": "true",
    "doorOptions": 2,
    "holdOpenTime": 200,
    "tz": "EST5EDT,M3.2.0,M11.1.0",
    "sensorTriggerVoltage": 2000,
    "sleepSensorTriggerVoltage": 2000
  }
}
```

Note: within the settings object the hold time key is `holdOpenTime`; the
dedicated `GET_HOLD_TIME`/`SET_HOLD_TIME` commands use `holdTime`. Both are
in centiseconds. **[V]**

**GET_HW_INFO** — every value in `fwInfo` is an **int**:
```json
{"config": "GET_HW_INFO"}
```
Response:
```json
{
  "success": "true",
  "fwInfo": {
    "ver": 1,
    "rev": 1,
    "fw_maj": 1,
    "fw_min": 7,
    "fw_pat": 18
  }
}
```

**GET_DOOR_BATTERY**:
```json
{"config": "GET_DOOR_BATTERY"}
```
Response — `batteryPercent` is an int, the other two are `"true"`/`"false"`
strings:
```json
{
  "success": "true",
  "batteryPercent": 85,
  "batteryPresent": "true",
  "acPresent": "true"
}
```

**GET_DOOR_OPEN_STATS**:
```json
{"config": "GET_DOOR_OPEN_STATS"}
```
Response (the probed unit reported 11057 and 1910):
```json
{
  "success": "true",
  "totalOpenCycles": 1234,
  "totalAutoRetracts": 5
}
```

### Schedule Commands

| Command | Type | Parameters | Description | |
|---------|------|------------|-------------|---|
| `GET_SCHEDULE_LIST` | config | - | Get the list of populated slots | **[R]** |
| `SET_SCHEDULE_LIST` | config | `schedules` | Set all schedules | **[R]** |
| `GET_SCHEDULE` | config | `index` | Get a specific schedule | **[V]** |
| `SET_SCHEDULE` | config | **`index`** and `schedule` | Create/update a schedule | **[V]** |
| `DELETE_SCHEDULE` | config | `index` | Delete a schedule | **[R]** |

**Request**:
```json
{"config": "GET_SCHEDULE_LIST"}
{"config": "GET_SCHEDULE", "index": 0}
{"config": "SET_SCHEDULE", "index": 0, "schedule": {}}
{"config": "DELETE_SCHEDULE", "index": 0}
```

#### SET_SCHEDULE requires `index` alongside `schedule`

**[V]** The slot `index` must be sent as a **sibling** of the `schedule`
object, even though the schedule object carries an `index` of its own. A
message carrying only `schedule` is answered `success: "false"` and writes
nothing, however the entry itself is spelled.

In this library the shape lives in one place,
`build_set_schedule_message()`.

See [Schedule Format](#schedule-format) for the schedule object structure.

#### Simulator validation

The simulator validates schedules coming off the wire before storing them:
`index` must be an integer in 0-255, `daysOfWeek` must be a 7-element list of
0/1 flags (or the legacy integer bitmask), and `hour`/`min` must be integers
in 0-23 / 0-59. Day flags are read as flags, not truthily, so a string `"0"`
disables the day. When `inside` or `outside` is true the corresponding
`*_start_time`/`*_end_time` objects are **required**, each carrying an
`hour` (`min` defaults to 0): an absent window is rejected rather than
silently materialized as 06:00-22:00. A malformed schedule is rejected with
the standard error envelope and nothing is stored; `SET_SCHEDULE_LIST`
rejects the whole batch rather than loading it partially.

`SET_SCHEDULE_LIST` **requires** its `schedules` field, and it must be a list.
An absent field is rejected (`schedules is required`) rather than treated as
an empty list — otherwise a one-word packet wipes every stored schedule and
answers success — and a wrong-typed one is rejected with
`schedules must be a list, got ...`. Clearing every schedule is spelled out
explicitly as `{"config": "SET_SCHEDULE_LIST", "schedules": []}`.

The same validate-before-storing rule applies to every other `SET_*` command,
so one malformed packet can never leave the simulator in a state a later
command chokes on:

| Command | Accepted values |
|---------|-----------------|
| `SET_HOLD_TIME` | a finite number of centiseconds in 0-90000 (`Infinity`/`NaN`, strings and containers are rejected) |
| `SET_TIMEZONE` | a string of at most 128 characters |
| `SET_SENSOR_TRIGGER_VOLTAGE` | `voltage`, required, a finite number in 0-65535 |
| `SET_SLEEP_SENSOR_TRIGGER_VOLTAGE` | `voltage`, required, a finite number in 0-65535 |
| `SET_NOTIFICATIONS` | a nested `notifications` object; a flat message is rejected, and a nested one whose values are not JSON booleans is accepted-and-ignored, exactly as the device does |
| `GET_SCHEDULE`, `DELETE_SCHEDULE` | `index` (when present) must be an integer in 0-255; a container, string, boolean or out-of-range value is rejected with a reason rather than raising |
| `SET_SCHEDULE` | `index` is required alongside `schedule` |
| `SET_SCHEDULE_LIST` | `schedules` is required and must be a list; absent or wrong-typed payloads are rejected with a reason and leave the store untouched |

A rejection answers `{"success": "false", "reason": "<field> must be ..."}`
with **no** `msgID`, and leaves state untouched.

### Diagnostic Commands

| Command | Type | Description | |
|---------|------|-------------|---|
| `HAS_REMOTE_ID` | config | Check if a remote ID is paired | **[V]** |
| `HAS_REMOTE_KEY` | config | Check if a remote key is paired | **[V]** |

**Request**:
```json
{"config": "HAS_REMOTE_ID"}
{"config": "HAS_REMOTE_KEY"}
```

**Response** — **[V]** the fields are `has_id` and `has_key`, carrying
`"true"`/`"false"` strings. They are **not** `hasRemoteId`/`hasRemoteKey`,
which is what this document claimed for years and what this library read
(and therefore never found):
```json
{"success": "true", "has_id": "true"}
{"success": "true", "has_key": "true"}
```

### The door clock

| Command | Type | Description | |
|---------|------|-------------|---|
| `GET_TIME` | config | Read the door's local wall-clock time | **[V]** |

**[V]** Undocumented by the vendor, but present, and worth having: schedules
are evaluated against this clock, so it is the only way to check that a door
will fire a schedule when you expect it to.

```json
{"config": "GET_TIME"}
```
→ `{"time": "Sun Aug 23 04:34:15 2026", "success": "true", ...}`

The value is a C `asctime()` string — `"%a %b %d %H:%M:%S %Y"`, exported as
`TIME_FORMAT` — carrying **local** time in the door's configured timezone,
with no offset. It was accurate to within seconds of real local time. It
answered nothing at all on one occasion, so treat a reply as a snapshot
rather than proof of freshness.

#### The clock is read-only, and `SET_TIME` answers with silence

**[V]** There is no way to set it. `SET_CLOCK`, `SET_DATE` and `SYNC_TIME`
are all rejected normally. `SET_TIME` is different, and it is the strangest
observed behaviour in this protocol:

```json
{"config": "SET_TIME", "time": "Sun Aug 23 03:34:15 2026"}
```
→ **nothing**. No reply, no failure envelope, and no change to the clock
(tested with a value an hour off, in the door's own format, twice).

Every other rejected shape answers `success: "false"`. **A client must not
read silence as success** — it is the one command where "no error came back"
means the opposite. This project's simulator reproduces the silence.

---

## Commands that do not exist

**[V]** These five names are defined by this library but are **rejected**
(`success: "false"`) by firmware 1.7.18. The constants are kept — a different
firmware revision may implement them, and clients stay liberal about what
they can parse — but nothing should depend on them:

| Command | Read the state here instead |
|---------|-----------------------------|
| `GET_TIMERS_ENABLED` | `timersEnabled` in `GET_SETTINGS` |
| `GET_AUTORETRACT` | `doorOptions` in `GET_SETTINGS` (a bitfield) |
| `GET_CMD_LOCKOUT` | `allowCmdLockout` in `GET_SETTINGS` |
| `GET_OUTSIDE_SENSOR_SAFETY_LOCK` | `outsideSensorSafetyLock` in `GET_SETTINGS` |
| `CHECK_RESET_REASON` | nothing — no substitute exists |

`CHECK_RESET_REASON`'s response field `resetReason` is therefore **[R]** and
was never observed from any device.

This project's simulator rejects all five, so a client that depends on one
fails in tests rather than only against hardware.

---

## The vendor app, and why your setting changed back

**[V]** Established experimentally, with the door's owner driving the app:

**The vendor app does not read live state from the door. It pushes its own
cached copy.** The door was set to `allowCmdLockout: "false"` over this
protocol; the app went on displaying the setting from its stale cache, and
simply *confirming* that screen wrote `"true"` back — silently undoing the
change.

Two consequences for anyone writing a client:

1. **A setting you change can be reverted later by the app, with no
   warning and no event on the wire.** If a value keeps reappearing, the
   phone in someone's pocket is the likeliest cause.
2. **The app's display is not evidence of the door's state.** When
   comparing, read the door with `GET_SETTINGS`; do not trust the screen.

The app also holds the door's [single connection](#connection) for as long
as it is open, which is the other half of the same debugging trap.

---

## What the app calls these settings

**[V]** Proven by operating the app against a live capture, not inferred.
The wire names are not descriptive, and one of them is actively misleading:

| App setting | Wire field | Relationship |
|-------------|-----------|--------------|
| "Allow pet to keep door open" | `allowCmdLockout` | **INVERTED** — app OFF ⇒ `"true"` |
| "Always allow pet entry inside override timers" | `outsideSensorSafetyLock` | Direct |
| "Auto Retract" | `doorOptions` bit 1 | On ⇒ `2`, off ⇒ `0` |

`outsideSensorSafetyLock` is the trap: the name reads as a safety interlock
on the outside sensor, and the app presents it as *"always allow pet entry
inside override timers"* — a schedule override, not a lock. Anyone reading
the field name alone will get it backwards.

`allowCmdLockout`'s inversion is why `PowerPetDoor` exposes it as
`pet_proximity_keep_open`, the app's meaning rather than the wire's. That
mapping is now confirmed and must not be "simplified".

---

## What this protocol cannot do

**[V]** 62 further read-only command names were probed (only `GET_`, `CHECK_`
and `HAS_` prefixes, so that a hit could not be destructive). **Every one was
rejected.** That includes every spelling tried for: firmware version or
update check, OTA status, serial number, model, MAC or IP address, WiFi/SSID/
RSSI, cloud or account status, push tokens or subscriptions, logs, uptime,
temperature, motor state and door position, and diagnostics.

So, on this LAN protocol, there is **no way** to:

- query for or trigger a firmware update;
- read network configuration or signal strength;
- read a serial number or model identifier;
- subscribe to push notifications.

Push notifications reach the vendor's app by some other path — the door's own
outbound connection — which this protocol does not expose. The only
device-initiated traffic on this connection is the
[notification events](#notification-events) and
[door status pushes](#unsolicited-door-status) described below, and those
only arrive while you are connected.

---

## Settings Fields

The `settings` object returned by `GET_SETTINGS`. **[V]** for every row.

| Field | Wire Name | Type | Description |
|-------|-----------|------|-------------|
| Power | `power_state` | `"true"`/`"false"` | Door power on/off |
| Inside Sensor | `inside` | `"true"`/`"false"` | Inside sensor enabled |
| Outside Sensor | `outside` | `"true"`/`"false"` | Outside sensor enabled |
| Timers/Auto | `timersEnabled` | `"true"`/`"false"` | Schedule mode enabled |
| Safety Lock | `outsideSensorSafetyLock` | `"true"`/`"false"` | Outside sensor safety lock |
| Command Lockout | `allowCmdLockout` | `"true"`/`"false"` | Command lockout enabled |
| Door options | `doorOptions` | int bitfield | Bit 1 (`2`) is auto-retract on obstruction; other bits unidentified |
| Hold Time | `holdOpenTime` | int | Hold time in centiseconds (the standalone GET/SET_HOLD_TIME commands use `holdTime`) |
| Timezone | `tz` | string | POSIX timezone string |
| Sensor Voltage | `sensorTriggerVoltage` | int | Sensor threshold, millivolts (set via the `voltage` field) |
| Sleep Sensor Voltage | `sleepSensorTriggerVoltage` | int | Sleep mode sensor threshold, millivolts |

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

Read as strings, written as JSON booleans — see
[SET_NOTIFICATIONS](#set_notifications). **[V]**

### Notification Messages (door to client)

**[R]** Notification events are device-initiated and use a **bare envelope**:
they carry no `CMD`, `success`, or `msgID` fields. The event name appears
as a key with an empty-string value; sensor events also carry a
`sensorState` of `"on"` or `"off"`. None were provoked on the probed unit,
so the shape below remains reverse-engineered.

```json
{"SENSOR_INDOOR": "", "sensorState": "on"}
{"SENSOR_OUTDOOR": "", "sensorState": "off"}
{"LOW_BATTERY": ""}
```

Clients should also tolerate CMD-style variants of these events
(`{"CMD": "SENSOR_INDOOR", "success": "true", "sensorState": "on"}`)
without treating them as command responses.

### Unsolicited Door Status

**[R]** The device also pushes door-state changes that nobody asked for,
using the normal response envelope with `CMD: "DOOR_STATUS"` and no `msgID`:

```json
{"CMD": "DOOR_STATUS", "door_status": "DOOR_RISING", "success": "true", "dir": "d2p"}
```

`door_status` carries one of the [Door Status Values](#door-status-values).
This is the same payload key a `GET_DOOR_STATUS` *response* uses, and
clients should route both to the same handler — a client that only handles
solicited replies will miss every state change it did not request. Note
that `DOOR_STATUS` here is a `CMD` value, not an envelope key.

---

## Schedule Format

A `GET_SCHEDULE` reply, spelled as the probed unit answered (**[V]**):

```json
{
  "index": 0,
  "enabled": 1,
  "inside": 1,
  "outside": 1,
  "daysOfWeek": [1, 1, 1, 1, 1, 1, 1],
  "in_start_time": {"hour": 0, "min": 0},
  "in_end_time": {"hour": 23, "min": 59},
  "out_start_time": {"hour": 0, "min": 0},
  "out_end_time": {"hour": 23, "min": 59}
}
```

| Field | Type (device → client) | Description |
|-------|------|-------------|
| `index` | int | Schedule slot number (0-based) |
| `enabled` | int `1`/`0` | Whether schedule is active |
| `inside` | int `1`/`0` | This schedule controls the inside sensor |
| `outside` | int `1`/`0` | This schedule controls the outside sensor |
| `daysOfWeek` | [int] | [Sun, Mon, Tue, Wed, Thu, Fri, Sat], 1=active. A legacy integer bitmask (bit 0 = Sunday, 0-127) is also accepted on input; out-of-range masks are rejected rather than read modulo 7 bits, because a negative mask would otherwise activate every day |
| `in_start_time` | {hour, min} | Inside sensor start time |
| `in_end_time` | {hour, min} | Inside sensor end time |
| `out_start_time` | {hour, min} | Outside sensor start time |
| `out_end_time` | {hour, min} | Outside sensor end time |

Note: Each schedule controls ONE sensor. Set times for that sensor; the other
sensor's times should be zeros. If a payload sets *both* flags (out of spec,
but the simulator's `schedule add both` produces it), the **inside** window
wins.

### How the schedule engine actually evaluates a window

**[V] Measured against firmware 1.7.18.** The engine writes its verdict
through to the sensor enable flags, so `GET_SETTINGS` reports it live: with
`timersEnabled` on, `inside`/`outside` follow the schedule minute by minute.
That is what made the table below measurable rather than inferred.

The rule is exactly:

```
active  iff  start <= now < end        (24:00 is a legal end, meaning 1440)
if end <= start the entry is EMPTY and never fires
```

| window probed | verdict | establishes |
|---|---|---|
| `20:00`–`23:00` at 21:07 | enabled | control |
| `21:01`–`21:31` at 21:01 | enabled | **start is inclusive** |
| `20:31`–`21:01` at 21:01 | disabled | **end is exclusive** |
| `16:01`–`16:01`, `21:01`–`21:01` | disabled | **`start == end` is EMPTY** |
| `20:00`–`24:00` at 21:07 | enabled | **hour 24 is honoured as end-of-day** |
| `00:00`–`24:00` | enabled | ditto |
| `20:00`–`00:00` | disabled | **`00:00` as an end is EMPTY** |
| `00:00`–`00:00` | disabled | ditto |
| `23:00`–`21:30` on the day it names | disabled | **not a same-day wrap** |
| `23:00`–`21:30` on the following day | disabled | **not a next-day spill either** |

Three consequences that are easy to get wrong:

* **A window cannot cross midnight.** Not by wrapping within the day, and not
  by spilling into the next one. `23:00`–`01:00` is stored perfectly and
  never fires. To let a pet out overnight you need **two** entries:
  `23:00`–`24:00` on the day and `00:00`–`01:00` on the next.
* **`start == end` is not a whole day.** It is nothing. A whole day is
  `00:00`–`24:00`.
* **`00:00` is a start, never an end.** The rule is positional, not
  contextual: midnight as a START is always the first minute of a day, and
  midnight as an END is always the last. The device does not reinterpret it —
  it compares the raw numbers and the entry never fires — so this library
  rewrites a window end of `00:00` to `24:00` on the **send** path
  (`powerpetdoor.schedule.normalise_window_end`), applied to the selected
  sensor's window only and never to the all-zero filler block of the sensor
  the entry is not about.

  That makes `00:00`–`00:00` a **whole day**, which is what anyone writing
  "midnight to midnight" means. The one caveat: a door that literally holds
  `00:00`–`00:00` is gating that sensor **off** right now, so a client
  displaying it as all-day is describing intent rather than current
  behaviour. Re-saving the entry makes the door agree. The spelling only ends
  up on a door by mistake, and this library no longer emits it.

**Storage is entirely separate from evaluation.** The schedule table accepts
and echoes back anything — `22:00`–`06:00`, `22:00`–`00:00`, `09:00`–`09:00`
and `00:00`–`24:00` all round-trip byte for byte. The device stores nonsense
faithfully and simply never acts on it, so **nothing downstream catches a
malformed window**. `Schedule.validate_for_send()` is therefore the only
thing standing between a caller and a schedule that reads correctly and does
nothing; `PowerPetDoor.set_schedule()` applies it. It refuses any window whose
end does not exceed its start — `end <= start`, coinciding ends included,
because those are empty too.

### End of day is 24:00, and 23:59 is not special

**[V]** `24:00` is a legal end and the device honours it: `20:00`–`24:00`
reports the sensor enabled at 21:07, and `00:00`–`24:00` enables it outright.
It is also **preserved** — write `00:00`–`24:00` and the door reads back
`00:00`–`24:00`, unchanged. So a whole day has an unambiguous spelling and
this library uses it.

`23:59` is therefore treated as an ordinary time. Earlier versions special-
cased it as end-of-day, reasoning that the probed unit's factory schedule is
`00:00`–`23:59` on all seven days and plainly means "always". That was always
an inference, and it became an unnecessary one: since the engine is strictly
`start <= now < end` and `24:00` works, there is nothing to round up to and
nothing to guess at. A window ending at `23:59` leaves the sensor off for
that final minute, which is what such a window says.

One consequence worth stating: a factory door does have a one-minute nightly
gap, and this library reports it rather than papering over it. **This is the
last claim here that has not been probed at the boundary** — confirming it
needs the door's clock at `23:59`. If a probe ever shows the firmware
special-casing `23:59`, the tests named
`test_the_factory_spelling_really_does_stop_one_minute_short` and
`test_a_window_ending_at_2359_stops_one_minute_short_of_the_day` are the ones
that should fail.

Reading is faithful either way: an entry is never rewritten on the way
through, so a door holding `00:00`–`23:59` keeps it.

### Hazard: the engine leaves the sensor flags where it last set them

**[V]** Turning `timersEnabled` off does **not** restore the sensor flags to
what they were before a schedule disabled them. Observed directly: a probe
window of `21:01`–`21:01` left `inside=false outside=false`, and disabling
timers left them off; they had to be re-enabled explicitly.

So a schedule that never fires can leave a door's sensors switched off
permanently, even after schedules are turned off entirely. Any client that
writes schedules should be prepared to re-enable the sensors.

### The two directions do not agree

The client→device and device→client spellings of the same schedule entry
differ, and that is not a bug:

| Field | client → device (`SET_SCHEDULE`) | device → client (`GET_SCHEDULE`) |
|-------|-------------------------------|-------------------------------|
| `enabled` | JSON boolean **[R]** | int `1`/`0` **[V]** |
| `inside` | JSON boolean **[R]** | int `1`/`0` **[V]** |
| `outside` | JSON boolean **[R]** | int `1`/`0` **[V]** |
| `index`, `daysOfWeek`, `{hour, min}` | int | int **[V]** |

The client→device column is **[R]**: JSON booleans are what
`powerpetdoor.door.Schedule.to_dict()` (and every `compress_schedule()`
result) has sent to real Power Pet Doors since v0.1.0, and writes made that
way were confirmed to land. Whether the device would *also* accept ints there
was not tested, so do not "unify" the two columns.

Each field's wire spelling lives in exactly one place —
`SCHEDULE_WIRE_TO_DEVICE` / `SCHEDULE_WIRE_FROM_DEVICE` in
`powerpetdoor/schedule.py` — so a further finding is a one-line change there.

**Readers on both sides are deliberately liberal** and accept `"1"`/`1`/`true`
and `"0"`/`0`/`false` interchangeably for every flag, so an implementation
that picks either spelling interoperates. Given the device's own
inconsistency (see [Value spellings](#value-spellings)), this is a
requirement, not a courtesy.

`GET_SCHEDULE_LIST` returns slot indices sorted ascending. **[R]**

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
| `DOOR_CLOSING` | Closing has begun; the flap has not moved yet |
| `DOOR_CLOSING_TOP_OPEN` | Door closing from fully open |
| `DOOR_CLOSING_MID_OPEN` | Door closing from mid position |

**[V]** — the full sequence was measured on firmware 1.7.18 by cycling a
physical unit:

```
DOOR_IDLE -> DOOR_RISING -> DOOR_SLOWING -> DOOR_HOLDING
          -> DOOR_CLOSING -> DOOR_CLOSING_TOP_OPEN -> DOOR_CLOSING_MID_OPEN
          -> DOOR_CLOSED -> DOOR_IDLE
```

**Closing has THREE states, not two.** `DOOR_CLOSING` comes first, about
180 ms before `DOOR_CLOSING_TOP_OPEN`, and it was missing from this library
entirely — so every close briefly produced `DoorStatus.UNKNOWN` (neither open
nor closed), a status a consumer cannot render, plus a logged warning. The
simulator did not emit it either, which is precisely why no test caught it.

It is measured on **both** closing paths: after a timed hold, and after an
explicit close from `DOOR_KEEPUP`. "Only a timed open reports it" was a
plausible reading of the first measurement and is not what the door does.

`DOOR_CLOSING` means the motor has started while the flap is still up, so
`position` is 100 and `is_closing` is true. A pet detected during it must
still trigger an auto-retract — the door simply returns to holding without
having travelled.
