# Power Pet Door Operation Guide

This document describes how the Power Pet Door operates, its features, and behavioral quirks. For the wire protocol details (field names, JSON syntax, commands), see [protocol.md](protocol.md).

## Table of Contents

- [Overview](#overview)
- [Door Operation](#door-operation)
  - [Door States](#door-states)
  - [Opening Sequence](#opening-sequence)
  - [Closing Sequence](#closing-sequence)
  - [Hold Timer](#hold-timer)
  - [Keep-Up Mode](#keep-up-mode)
- [Sensors](#sensors)
  - [Collar Sensors](#collar-sensors)
  - [Sensor Detection](#sensor-detection)
  - [Sensor Enable vs Safety Lock](#sensor-enable-vs-safety-lock)
  - [Sensor Trigger Voltage](#sensor-trigger-voltage)
- [Safety Features](#safety-features)
  - [Autoretract](#autoretract)
  - [Outside Sensor Safety Lock](#outside-sensor-safety-lock)
  - [Command Lockout / Pet Proximity Keep-Open](#command-lockout--pet-proximity-keep-open)
  - [How Safety Features Interact](#how-safety-features-interact)
- [Schedules](#schedules)
  - [Schedule Behavior](#schedule-behavior)
  - [Schedule and Sensor Interaction](#schedule-and-sensor-interaction)
- [Power and Battery](#power-and-battery)
- [Door Position Mapping](#door-position-mapping)
- [Quirks and Edge Cases](#quirks-and-edge-cases)
- [Unknown Features](#unknown-features)

---

## Overview

The Power Pet Door is a motorized pet door that opens and closes automatically when it detects a pet wearing a compatible ultrasonic collar. Key characteristics:

- **Motorized door panel** that raises (opens) and lowers (closes)
- **Inside and outside collar sensors** for pet detection
- **Automatic scheduling** to control when sensors are active
- **Safety features** including auto-retract on obstruction
- **Battery backup** with AC power primary
- **Single-connection limit** - only one client can connect at a time

The door connects to WiFi and exposes a TCP server (typically port 3000). If the mobile app is connected, Home Assistant cannot connect, and vice versa.

---

## Door Operation

### Door States

The door moves through a sequence of states during operation:

| State | Description |
|-------|-------------|
| **Closed** | Door is fully closed (normal resting position) |
| **Rising** | Motor is lifting the door panel |
| **Slowing** | Motor is slowing as door approaches fully open |
| **Holding** | Door is fully open, countdown timer running |
| **Keep Up** | Door is locked open (manual hold mode) |
| **Closing Top** | Door is descending from fully open |
| **Closing Mid** | Door is descending through middle position |

### Opening Sequence

When a pet triggers a sensor or an OPEN command is sent:

```
CLOSED → RISING → SLOWING → HOLDING
```

1. **RISING**: Motor lifts the door panel upward
2. **SLOWING**: Motor decelerates as door approaches the top
3. **HOLDING**: Door is fully open, hold timer begins countdown

The transition from RISING to SLOWING happens near the top position to prevent slamming.

### Closing Sequence

After the hold timer expires (or CLOSE command received):

```
HOLDING → CLOSING_TOP_OPEN → CLOSING_MID_OPEN → CLOSED
```

1. **CLOSING_TOP_OPEN**: Door begins descending from fully open
2. **CLOSING_MID_OPEN**: Door continues through the middle position
3. **CLOSED**: Door reaches the closed position

The two-phase closing allows for obstruction detection at different heights.

### Hold Timer

When the door opens:
- The hold timer starts counting down from `hold_time` seconds
- Default is typically 2 seconds
- The door's own interface allows configuration to 4 or 6 seconds
- The client library allows a wider range (fractions of a second to any high value)
- When timer expires, door begins closing sequence

The hold timer can be affected by:
- **Pet proximity keep-open**: If a sensor detects a pet, timer resets
- **Command lockout**: If enabled, sensor detection does NOT reset timer

### Keep-Up Mode

The OPEN_AND_HOLD command puts the door in "keep up" mode:
- Door stays open indefinitely
- Ignores hold timer
- Only closes when explicit CLOSE command is sent
- Useful for extended outdoor access periods

---

## Sensors

### Collar Sensors

The door has two ultrasonic sensors:

| Sensor | Location | Purpose |
|--------|----------|---------|
| **Inside** | Interior side | Detects pets approaching from inside |
| **Outside** | Exterior side | Detects pets approaching from outside |

Sensors detect compatible ultrasonic pet collars. When a pet with a collar approaches, the sensor detects the collar's signal.

### Sensor Detection

When a sensor detects a pet:

1. If the sensor is **enabled**, door opens (if closed)
2. Door enters HOLDING state
3. Hold timer starts countdown
4. If pet remains detected AND pet proximity keep-open is active:
   - Hold timer keeps resetting
   - Door won't close while pet is near

### Sensor Enable vs Safety Lock

There are two different ways to prevent a sensor from opening the door:

| Feature | What It Does | Notifications | Use Case |
|---------|--------------|---------------|----------|
| **Disable Sensor** | Turns sensor OFF completely | No detection at all | Permanently disable (no collar on that side) |
| **Safety Lock** | Sensor still detects, but won't open door | Still sends notifications | Temporarily prevent access while monitoring |

**Example scenarios:**

- **Disable outside sensor**: You only have inside collars, no pets will ever approach from outside
- **Safety lock outside**: Keep pets inside at night, but still get alerts if something triggers the sensor

Key difference: A disabled sensor doesn't detect anything. A safety-locked sensor detects and can notify, but the door won't respond.

### Sensor Trigger Voltage

Two voltage threshold settings control sensor sensitivity:

| Setting | When Used | Purpose |
|---------|-----------|---------|
| **Sensor Trigger Voltage** | While door is open | Detection threshold for pet proximity (keeps door open or triggers re-open) |
| **Sleep Sensor Trigger Voltage** | While door is closed | Detection threshold for initial collar detection |

These control the sensitivity of ultrasonic collar detection. Higher values may require stronger collar signals (closer pet).

---

## Safety Features

### Autoretract

When enabled, the door automatically reopens if it encounters an obstruction while closing.

| State | Behavior |
|-------|----------|
| **Enabled** | Door reopens on obstruction, increments retract counter |
| **Disabled** | Motor stops driving the door; gravity takes over |

When autoretract is disabled and an obstruction occurs, the door doesn't actively try to retract - it simply stops motor control and allows gravity to determine what happens (the door may rest on the obstruction or slide down).

This prevents injury to pets or damage to the door mechanism. The `totalAutoRetracts` counter tracks how many times auto-retract has occurred.

### Outside Sensor Safety Lock

The vendor app calls this *"always allow pet entry inside override timers"*
— a schedule override, not a lock on the outside sensor. The wire name
(`outsideSensorSafetyLock`) reads like the opposite, so trust the polarity
rather than the name; see
[What the app calls these settings](protocol.md#what-the-app-calls-these-settings).

**It grants entry; it does not deny it.** When enabled, the outside sensor
opens the door **regardless of the schedule** — that is the whole point of
"override timers".

| State | Behavior |
|-------|----------|
| **Enabled** | Outside sensor opens the door even outside a scheduled window |
| **Disabled** | Outside sensor obeys the schedule like the inside one |

**Use cases:**
- Let a pet back in at any hour, while the schedule still governs the inside sensor
- Keep timed access for outgoing trips without stranding a pet outside

**Confirmed by direct measurement**: with the app switch off the door
reports `outsideSensorSafetyLock: "false"`, and with it on, `"true"`. See
[What the app calls these settings](protocol.md#what-the-app-calls-these-settings)
for the full capture.

**Still unsettled:** whether an enabled lock also overrides a *disabled*
outside sensor, or only the schedule. The app says "override timers", so
the simulator overrides the schedule only. Probing that needs hardware:
disable the outside sensor, enable the lock, and trigger it.

### Command Lockout / Pet Proximity Keep-Open

The vendor app calls this *"Allow pet to keep door open"*, and the app switch
is the **inverse** of the wire field — app OFF means
`allowCmdLockout: "true"`. That inversion was confirmed by driving the app
against a live capture, so the facade's `pet_proximity_keep_open` (which
follows the app, not the wire) is correct and must not be "simplified".

This setting has an inverse relationship that can be confusing:

| Setting | Name | Effect |
|---------|------|--------|
| `allowCmdLockout = true` | Command lockout ON | Door closes on timer, ignores pet proximity |
| `allowCmdLockout = false` | Pet proximity keep-open ON | Door stays open while pet detected |

**With pet proximity keep-open active** (lockout OFF):
- If sensor detects pet during hold timer, timer resets
- Door won't close until pet moves away
- Prevents door from closing on a pet in the doorway

**With command lockout active** (lockout ON):
- Sensor detection doesn't affect hold timer
- Door follows normal countdown and closes
- Pet may need to re-trigger sensor if door closes

### How Safety Features Interact

When a sensor is active and detecting something:

1. **Inside sensor active**: Blocks door closing if:
   - Inside sensor is enabled (`inside = true`)
   - Command lockout is OFF (`allowCmdLockout = false`)

2. **Outside sensor active**: Blocks door closing if:
   - Outside sensor is enabled (`outside = true`)
   - Command lockout is OFF (`allowCmdLockout = false`)

If **command lockout is ON**, sensor detection never blocks door closing, regardless of other settings.

The safety lock does not appear in that list. It grants **entry** past the
schedule and says nothing about whether a detected pet holds the door open —
that is command lockout's job, above.

---

## Schedules

### Schedule Behavior

Schedules control when sensors are active during automatic mode (`timersEnabled = true`).

Each schedule entry specifies:
- Which sensor it controls (inside OR outside, not both)
- Which days of the week it's active
- Start and end time for the active window

### Schedule and Sensor Interaction

When schedules are enabled (`timersEnabled = true`):
- Sensors only respond during their scheduled time windows
- Outside scheduled windows, sensor triggers are ignored
- Multiple schedules can overlap for complex patterns

When schedules are disabled (`timersEnabled = false`):
- Sensors respond based only on their enable state
- Schedules are stored but not applied

**Example**: A schedule for inside sensor from 6:00 to 22:00 means pets can only exit during those hours (assuming inside sensor opens door for exit).

**Window boundaries** — measured, not inferred. The
engine is exactly `start <= now < end`, so the start minute is included and
the end minute excluded: `6:00`–`22:00` covers 6:00 through 21:59.

Three things follow:

- **A window cannot cross midnight.** An end earlier than the start does
  *not* wrap. `23:00`–`01:00` is stored perfectly and never fires — not on
  the day it names, and not on the next. Overnight access needs **two**
  entries: `23:00`–`24:00` on the day and `00:00`–`01:00` on the next.
- **A window whose start and end coincide is empty**, not the whole day. A
  whole day is `00:00`–`24:00`.
- **`00:00` as an END always means the end of the day.** The rule is
  positional — midnight opening a window is the day's first minute, midnight
  closing one is its last. The device does not reinterpret it, so this
  library rewrites it to `24:00` when sending.

An entry that should gate nothing is still best spelled `enabled: false`: it
says so, where an empty window merely behaves that way.

**`23:59` is not special.** It is the last real minute of the day, and a
window ending there leaves the sensor off for that minute. The end of the day
is `24:00`, which the device is measured to honour and to preserve. A factory
door spells its full-day windows `00:00`–`23:59` and so does have a
one-minute nightly gap; this library reports that rather than papering over
it. See
[End of day is 24:00, and 23:59 is not special](protocol.md#end-of-day-is-2400-and-2359-is-not-special).

**Hazard**: the engine writes its verdict through to the sensor enable flags,
and turning `timersEnabled` off does *not* put them back. A schedule that
never fires can leave the door's sensors disabled even after schedules are
switched off. Re-enable them explicitly.

---

## Power and Battery

The door has AC power with battery backup:

| State | Behavior |
|-------|----------|
| **AC present, battery present** | Normal operation, battery charging |
| **AC absent, battery present** | Running on battery backup |
| **Battery low** | Low battery notification sent |
| **Power OFF** | Door unresponsive to triggers/commands, WiFi stays active |

When power is OFF:
- Door will not respond to sensor triggers
- Door will not respond to OPEN/CLOSE commands
- WiFi connection remains active for monitoring

---

## Door Position Mapping

For home automation integrations, door states map to position percentages:

| State | Position | Description |
|-------|----------|-------------|
| Closed | 0% | Fully closed |
| Rising | 33% | Opening |
| Slowing | 66% | Near top |
| Holding | 100% | Fully open |
| Keep Up | 100% | Fully open (locked) |
| Closing Top | 66% | Closing from top |
| Closing Mid | 33% | Closing through middle |

This allows cover entities to display approximate door position.

---

## Quirks and Edge Cases

### Single Connection Limit

The door only accepts one TCP connection. If the mobile app is connected, other clients (like Home Assistant) cannot connect. The first client to connect has exclusive access.

### Hold Time Units

Hold time is stored in **centiseconds** (1/100 second), not seconds. A value of 1500 means 15 seconds.

### State Transition Timing

During door movement, state changes happen at physical positions, not fixed times. Timing depends on motor speed and any resistance encountered.

### Obstruction During Opening

What a real door does if it meets an obstruction on the way *up* has not been
observed. The simulator models obstructions on the closing path only, where
the behaviour is known, and ignores them while the door is rising.

---

## Unknown Features

### Remote ID and Key

The `HAS_REMOTE_ID` and `HAS_REMOTE_KEY` diagnostic commands check for the presence of pairing credentials. These are likely used for mobile app pairing - when you pair the door with the Power Pet Door mobile app, these IDs are set.

If you have additional information about these features, contributions are welcome.
