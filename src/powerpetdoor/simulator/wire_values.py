# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""How each registry value appears on the wire.

The wire is one *interface* to the simulator's values, and this is the
whole of what makes it different from the prompt: which command carries a
value, and what shape the payload is. The value itself - how it reads,
how it writes, what it accepts, what side effects it has - lives in
:mod:`powerpetdoor.simulator.values` and is not restated here.

Two things read this table, and they used to be written out separately:

- The **response** a command answers with (:mod:`.protocol`).
- The **broadcast** an unsolicited change sends (:mod:`.server`).

They are the same payload - the door
answers `ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK` with exactly what it later
pushes - so writing them twice was two chances to disagree.

Only device values appear here. The simulation's own knobs (flap timings,
battery rates) have no wire spelling, and
:meth:`~powerpetdoor.simulator.protocol.DoorSimulatorProtocol._apply_value`
refuses them.

Everything in this module reads a value through
:data:`~powerpetdoor.simulator.values.VALUES`, never off the state object.
The wire's own spellings - ``"true"``/``"false"`` strings here, ``1``/``0``
there, centiseconds for a hold time - are this layer's *translation*, and
translating is exactly what an interface layer is for. Reaching past the
accessor to read the attribute would mean a value that grew tracing, moved
to different storage, or started proxying real hardware kept working
everywhere except the wire.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..const import (
    CMD_CLOSE,
    CMD_DELETE_SCHEDULE,
    CMD_DISABLE_AUTO,
    CMD_DISABLE_AUTORETRACT,
    CMD_DISABLE_CMD_LOCKOUT,
    CMD_DISABLE_INSIDE,
    CMD_DISABLE_OUTSIDE,
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_ENABLE_AUTO,
    CMD_ENABLE_AUTORETRACT,
    CMD_ENABLE_CMD_LOCKOUT,
    CMD_ENABLE_INSIDE,
    CMD_ENABLE_OUTSIDE,
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HOLD_TIME,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_POWER,
    CMD_GET_SCHEDULE,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_SENSORS,
    CMD_GET_SETTINGS,
    CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_TIME,
    CMD_GET_TIMERS_ENABLED,
    CMD_GET_TIMEZONE,
    CMD_HAS_REMOTE_ID,
    CMD_HAS_REMOTE_KEY,
    CMD_OPEN,
    CMD_OPEN_AND_HOLD,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_TIMEZONE,
    DOOR_OPTION_AUTORETRACT,
    DOOR_POSITIONS,
    FIELD_AC_PRESENT,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD_LOCKOUT,
    FIELD_DAYSOFWEEK,
    FIELD_DOOR_STATUS,
    FIELD_ENABLED,
    FIELD_END_TIME_SUFFIX,
    FIELD_FW_MAJOR,
    FIELD_FW_MINOR,
    FIELD_FW_PATCH,
    FIELD_FWINFO,
    FIELD_HAS_REMOTE_ID,
    FIELD_HAS_REMOTE_KEY,
    FIELD_HOLD_OPEN_TIME,
    FIELD_HOLD_TIME,
    FIELD_HOUR,
    FIELD_HW_REVISION,
    FIELD_HW_VERSION,
    FIELD_INDEX,
    FIELD_INSIDE,
    FIELD_INSIDE_PREFIX,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_MINUTE,
    FIELD_MSG_ID,
    FIELD_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_PREFIX,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_REASON,
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SETTINGS,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_START_TIME_SUFFIX,
    FIELD_SUCCESS,
    FIELD_TIME,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    FIELD_VOLTAGE,
    PING,
    SUCCESS_FALSE,
    SUCCESS_TRUE,
)
from ..schedule import (
    MAX_SCHEDULE_HOUR,
    MAX_SCHEDULE_INDEX,
    MAX_SCHEDULE_MINUTE,
    wire_bool_string,
    wire_int_flag,
)
from .notifications import (
    NOTIFICATION_NAMES,
    NOTIFY_INSIDE_OFF,
    NOTIFY_INSIDE_ON,
    NOTIFY_LOW_BATTERY,
    NOTIFY_OUTSIDE_OFF,
    NOTIFY_OUTSIDE_ON,
)
from .values import VALUES
from .values import read_value as read

#: Notification name -> the field the door reports it under.
NOTIFICATION_FIELDS: dict[str, str] = {
    NOTIFY_INSIDE_ON: FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    NOTIFY_INSIDE_OFF: FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    NOTIFY_OUTSIDE_ON: FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    NOTIFY_OUTSIDE_OFF: FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    NOTIFY_LOW_BATTERY: FIELD_LOW_BATTERY_NOTIFICATIONS,
}

if TYPE_CHECKING:
    from .state import DoorSimulatorState


#: Wire units per registry unit, where the two differ. The door carries
#: the hold time in centiseconds; every other layer works in seconds.
CENTISECONDS_PER_SECOND = 100

#: Wire field -> the registry value it carries, and its scale.
#:
#: **Bounds are derived, never re-declared.** What a value accepts is
#: :data:`~powerpetdoor.simulator.values.VALUES`'s to say, exactly as how
#: it is read and written is - and a bound written out a second time here
#: is a bound that can disagree with the one actually enforced. Two
#: already had: ``totalOpenCycles`` and ``totalAutoRetracts`` documented
#: no maximum at all while the registry capped both at 2**31-1.
#:
#: `test_shared_paths.py` pins that every numeric wire field either
#: appears here or is a value the registry does not hold.
WIRE_BOUNDS: dict[str, tuple[str, int]] = {
    FIELD_HOLD_TIME: ("hold_time", CENTISECONDS_PER_SECOND),
    FIELD_HOLD_OPEN_TIME: ("hold_time", CENTISECONDS_PER_SECOND),
    FIELD_VOLTAGE: ("sensor_trigger_voltage", 1),
    FIELD_SENSOR_TRIGGER_VOLTAGE: ("sensor_trigger_voltage", 1),
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE: ("sleep_sensor_trigger_voltage", 1),
    FIELD_BATTERY_PERCENT: ("battery", 1),
    FIELD_TOTAL_OPEN_CYCLES: ("total_open_cycles", 1),
    FIELD_TOTAL_AUTO_RETRACTS: ("total_auto_retracts", 1),
}


def wire_bounds(field: str) -> dict[str, int]:
    """The bounds a wire field advertises, taken from the registry."""
    name, scale = WIRE_BOUNDS[field]
    spec = VALUES[name]
    return {
        "minimum": int(spec.minimum * scale),
        "maximum": int(spec.maximum * scale),
    }


#: Widest hold time (centiseconds) accepted from the wire. Derived from
#: the operator-side ceiling rather than restated: the two are the same
#: limit in different units, and 90000 written out here could outlive a
#: change to the 900 s it is meant to track.
MAX_HOLD_TIME_CENTISECONDS = wire_bounds(FIELD_HOLD_TIME)["maximum"]

#: Longest ``SET_TIMEZONE`` string accepted from the wire. Real POSIX TZ
#: strings and IANA names are far shorter than this.
MAX_TIMEZONE_LENGTH = 128

#: Widest sensor trigger voltage the device stores (millivolts).
#:
#: The field is a signed 32-bit
#: integer: ``2147483647`` is stored verbatim, and ``4294967295`` is
#: accepted and read back as ``2147483647`` - it **saturates**, it is not
#: refused. 65535 was this project's guess and it was far too low.
MAX_TRIGGER_VOLTAGE = wire_bounds(FIELD_VOLTAGE)["maximum"]

#: **Measured**: ``voltage: 0`` is answered ``success: "true"`` and then
#: silently ignored - the stored value does not change. Confirmed by
#: priming the door to 500, sending 0, and reading back 500.
#:
#: This is one of the door's accept-and-ignores, alongside a nested
#: ``SET_NOTIFICATIONS`` payload of strings. A client must not read
#: success as "it took".
IGNORED_TRIGGER_VOLTAGE = 0

#: What each command *does*, in the terms someone integrating cares
#: about. Not how it is spelled - the schema says that - but what happens
#: to the door, and what to watch out for.
#:
#: `test_schemas.py` pins that every command the door implements has an
#: entry, so a new one cannot ship undescribed.
COMMAND_DOCS: dict[str, str] = {
    CMD_OPEN: (
        "Open the flap and let it close again on its own timer. The door "
        "answers immediately with the state it has entered, not the state it "
        "will reach - expect a sequence of unsolicited status pushes as it "
        "travels."
    ),
    CMD_OPEN_AND_HOLD: (
        "Open the flap and hold it open until something closes it. Unlike "
        "OPEN, no timer will bring it down."
    ),
    CMD_CLOSE: (
        "Close the flap. A pet detected on the way down retracts it again when auto-retract is on."
    ),
    CMD_GET_DOOR_STATUS: (
        "Where the flap is in its travel. The same value arrives unprompted "
        "whenever it changes, so a client that only polls this is doing work "
        "the door already does for it."
    ),
    CMD_GET_SETTINGS: (
        "Every configurable value in one call - power, both sensors, "
        "schedules on/off, the safety lock, command lockout, auto-retract, "
        "timezone, hold time and both trigger voltages. Cheaper than the "
        "individual getters and the only way to read several of them, since "
        "their dedicated GET_ commands do not exist."
    ),
    CMD_GET_SENSORS: (
        "Whether each proximity sensor is switched on. This reports the "
        "*enables*, not whether a pet is standing at one."
    ),
    CMD_GET_POWER: (
        "Whether the unit is powered - in practice whether the motor is "
        "live. With it off the door refuses to move. Answers `power_state` "
        'as the string `"true"`/`"false"`, where `GET_SENSORS` answers with '
        "ints."
    ),
    CMD_GET_TIMERS_ENABLED: (
        "Whether **scheduling** is in force - the read side of "
        "`ENABLE_TIMERS`/`DISABLE_TIMERS`. This is a different thing from "
        "the sensor enables `GET_SENSORS` reports and from the unit power "
        'in `GET_POWER`. Answers `timersEnabled` as the string `"true"`/'
        '`"false"`.'
    ),
    CMD_POWER_ON: (
        "Switch the door on so it responds to its sensors and schedules again. "
        "The flap is not moved by this."
    ),
    CMD_POWER_OFF: (
        "Switch the door off. An open flap closes, and the door stops "
        "responding to pets until it is switched back on."
    ),
    CMD_ENABLE_INSIDE: (
        "Let the indoor sensor open the door for a pet wanting out. A pet "
        "already waiting there is admitted as soon as this takes effect."
    ),
    CMD_DISABLE_INSIDE: (
        "Stop the indoor sensor opening the door. A pet already waiting there "
        "stays shut in until it is switched back on."
    ),
    CMD_ENABLE_OUTSIDE: (
        "Let the outdoor sensor open the door for a pet wanting in, subject to "
        "the schedule unless the safety lock overrides it."
    ),
    CMD_DISABLE_OUTSIDE: (
        "Stop the outdoor sensor opening the door, shutting a pet out until it is switched back on."
    ),
    CMD_ENABLE_AUTO: (
        "Apply the stored schedules. The vendor app calls this *timers*. With "
        "it off the sensors work around the clock."
    ),
    CMD_DISABLE_AUTO: (
        "Ignore the stored schedules entirely; the sensors then work at any hour of any day."
    ),
    CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK: (
        "Always allow a pet in, whatever the schedule says. The vendor app "
        "calls this *always allow pet entry inside override timers*. Despite "
        "the wire name it grants entry rather than denying it."
    ),
    CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK: (
        "Let the schedule govern the outdoor sensor again, so a pet outside "
        "its window is not let in."
    ),
    CMD_ENABLE_CMD_LOCKOUT: (
        "Stop the door holding itself open for a pet lingering nearby; it "
        "closes on its timer regardless. The vendor app spells this as "
        "turning *allow pet to keep door open* off."
    ),
    CMD_DISABLE_CMD_LOCKOUT: (
        "Let a pet lingering in the doorway hold the door open past its timer."
    ),
    CMD_ENABLE_AUTORETRACT: (
        "Reopen the flap if it meets something on the way down. Answers with "
        "the whole settings object rather than the one field."
    ),
    CMD_DISABLE_AUTORETRACT: (
        "Let the flap close against an obstruction. Answers with the whole settings object."
    ),
    CMD_GET_HOLD_TIME: ("How long the door stays open before closing itself, in centiseconds."),
    CMD_SET_HOLD_TIME: (
        "Set how long the door stays open before closing itself. Sent in "
        "centiseconds; GET_SETTINGS reports the same value as `holdOpenTime`."
    ),
    CMD_GET_TIMEZONE: (
        "The zone the door evaluates schedules against. A door in the wrong "
        "zone opens on the wrong schedule with nothing else to show for it."
    ),
    CMD_SET_TIMEZONE: (
        "Set the zone schedules are evaluated against. Takes a POSIX rule, "
        "which is also what every reader answers with."
    ),
    CMD_GET_TIME: (
        "The door's own clock. Read-only, and the only way to confirm a "
        "schedule will fire when you expect it to."
    ),
    CMD_GET_SENSOR_TRIGGER_VOLTAGE: (
        "The threshold used to keep noticing a collar once the door is awake."
    ),
    CMD_SET_SENSOR_TRIGGER_VOLTAGE: (
        "Set how strong a collar signal must be to count as a pet. Raising it "
        "makes the door less eager; lowering it catches a weak collar."
    ),
    CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE: (
        "The threshold used to notice a collar while the door is closed, as "
        "distinct from the one used once it is awake."
    ),
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE: (
        "Set how strong a collar signal must be to wake a closed door. Takes "
        "`voltage`, while its getter answers `sleepSensorTriggerVoltage`."
    ),
    CMD_GET_DOOR_BATTERY: (
        "Charge, whether a battery is fitted, and whether mains is connected. "
        "Reports 0% when no battery is present rather than omitting the field."
    ),
    CMD_GET_DOOR_OPEN_STATS: (
        "Lifetime counters: completed cycles, and times the flap reversed on an obstruction."
    ),
    CMD_GET_HW_INFO: (
        "Firmware and hardware versions, delivered as five separate integers "
        "rather than a version string."
    ),
    CMD_HAS_REMOTE_ID: (
        "Whether a remote control is paired. Answers under `has_id`, not the "
        "camelCase spelling the rest of the protocol would suggest."
    ),
    CMD_HAS_REMOTE_KEY: ("Whether a remote key is paired. Answers under `has_key`."),
    CMD_GET_NOTIFICATIONS: (
        "Which of the five notification switches are on. The notifications "
        "themselves are delivered by the vendor's service, not on this "
        "connection."
    ),
    CMD_SET_NOTIFICATIONS: (
        "Turn notification switches on or off. The flags must be JSON "
        "booleans: a nested object of strings is answered with success and "
        "silently discarded, so read the values back."
    ),
    CMD_GET_SCHEDULE_LIST: (
        "Which schedule slots are populated. Returns slot numbers, not the "
        "schedules - read each one with GET_SCHEDULE."
    ),
    CMD_GET_SCHEDULE: (
        "One stored schedule: which days it covers, the window within those "
        "days, and which sensors it governs."
    ),
    CMD_SET_SCHEDULE: (
        "Create or replace one schedule slot. The slot number goes beside the "
        "schedule object, not only inside it."
    ),
    CMD_DELETE_SCHEDULE: (
        "Empty one schedule slot. There is no bulk clear; delete each slot "
        "GET_SCHEDULE_LIST reports."
    ),
    PING: (
        "Round-trip check. The door echoes the token, which is how a client "
        "measures latency and notices a connection that has gone quiet."
    ),
}


#: What each wire field *is*, so a reader does not have to guess from
#: `{"type": "string"}`. Bounds come from the validators that enforce them
#: the validators that enforce them, so a bound cannot be documented at a
#: value the door does not actually apply.
#:
#: `test_schemas.py::TestEveryWireFieldIsDocumented` pins that every field
#: the probe observes has an entry - a new field cannot arrive undescribed.
#:
#: This lives here, in the code, rather than in the generator that reads
#: it: a description maintained in `scripts/` is a third place the wire
#: is described, alongside the constants and docs/protocol.md.
FIELD_DOCS: dict[str, dict[str, Any]] = {
    FIELD_MSG_ID: {
        "type": "string",
        # Declared rather than taken from the probe: the probe numbers its
        # requests, so an observed value would differ per command and read
        # as though the number mattered. It does not - any token will do,
        # so long as the reply's `msgID` is matched back to it.
        "examples": ["1"],
        "description": (
            "Correlation token, echoed back as `msgID` - note the capital D. "
            "Any value will do; match the reply back to the request that "
            "used it."
        ),
    },
    FIELD_TZ: {
        "type": "string",
        "maxLength": MAX_TIMEZONE_LENGTH,
        "pattern": r"^[A-Za-z<>+\-0-9]{3,}[+-]?\d",
        "description": (
            "A POSIX TZ string: a standard-time abbreviation and offset, "
            "optionally followed by a daylight abbreviation and the rule for "
            "when it applies."
        ),
    },
    FIELD_HOLD_TIME: {
        "type": "integer",
        "unit": "centiseconds",
        **wire_bounds(FIELD_HOLD_TIME),
        "description": (
            "How long the door stays open, in **centiseconds** - 1500 is 15 "
            "seconds. `GET_SETTINGS` reports the same value under "
            "`holdOpenTime`."
        ),
    },
    FIELD_VOLTAGE: {
        "type": "integer",
        "unit": "millivolts",
        **wire_bounds(FIELD_VOLTAGE),
        "description": (
            "Collar detection threshold; a typical unit ships at 2000. "
            "Stored as a signed 32-bit value, so larger numbers saturate at "
            f"the maximum. A value of {IGNORED_TRIGGER_VOLTAGE} is answered "
            "with success and leaves the stored value unchanged, so read it "
            "back to confirm. The reply reports it as `sensorTriggerVoltage`."
        ),
    },
    FIELD_INDEX: {
        "type": "integer",
        # No `unit`: that field is for real units (centiseconds,
        # millivolts) that a reader would otherwise get wrong. A slot
        # number is a plain ordinal, and "In slot number." is not English.
        "minimum": 0,
        "maximum": MAX_SCHEDULE_INDEX,
        "description": (
            "Schedule slot. `SET_SCHEDULE` needs it as a **sibling** of "
            "`schedule`, even though the schedule object carries one too."
        ),
    },
    FIELD_SCHEDULE: {
        "type": "object",
        "description": (
            'One schedule. Times are `{"hour": h, "min": m}`; a window '
            "ending at its start is empty, not all-day."
        ),
    },
    FIELD_SCHEDULES: {
        "type": "array",
        # Without `items` this rendered as an array of anything, which is
        # the one thing it is not: declaring the type here overrode what
        # the probe observed, so the field's own documentation erased the
        # shape. An element is a slot index, bounded by the same constant
        # as `index` rather than by a second copy of 255.
        "items": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_SCHEDULE_INDEX,
            "description": "A populated schedule slot.",
        },
        "description": (
            "In a `GET_SCHEDULE_LIST` reply, the **slot numbers** that are "
            "populated - not the schedules. Read each with `GET_SCHEDULE`."
        ),
    },
    FIELD_NOTIFICATIONS: {
        "type": "object",
        "description": (
            "The five notification switches. On the way **in** they must be "
            "JSON booleans; a nested object of strings is accepted and "
            "silently ignored. On the way **out** they are "
            '`"true"`/`"false"` strings.'
        ),
    },
    FIELD_DOOR_STATUS: {
        "type": "string",
        "enum": sorted(DOOR_POSITIONS),
        "description": (
            "How far through its travel the flap is. Arrives both as a reply "
            "and **unsolicited**, with no `msgID`; route both to one handler."
        ),
    },
    FIELD_SUCCESS: {
        "type": "string",
        "enum": [SUCCESS_TRUE, SUCCESS_FALSE],
        "description": (
            'A string, not a boolean. Never read `"true"` as "the value took" '
            "- several commands are accepted and ignored. Read it back."
        ),
    },
    FIELD_REASON: {
        "type": "string",
        "description": 'Why a `success: "false"` failed. Absent on an unknown command.',
    },
    FIELD_SETTINGS: {
        "type": "object",
        "description": (
            "The whole settings object. Note the spellings differ from the "
            'top level: the flags here are `"true"`/`"false"` **strings** '
            "where a top-level reply uses the ints `1`/`0`, and `holdOpenTime` "
            "is this object's name for `holdTime`."
        ),
    },
    FIELD_INSIDE: {
        "type": "integer",
        "enum": [0, 1],
        "description": (
            "Inside sensor enable, as an int at the top level of a reply. "
            'Inside a `settings` object the same value is the string `"true"`.'
        ),
    },
    FIELD_OUTSIDE: {
        "type": "integer",
        "enum": [0, 1],
        "description": "Outside sensor enable. Same int/string split as `inside`.",
    },
    FIELD_POWER: {
        "type": "integer",
        "enum": [0, 1],
        "description": "Main power. Same int/string split as `inside`.",
    },
    FIELD_AUTO: {
        "type": "integer",
        "enum": [0, 1],
        "description": (
            "Whether schedules are applied - the vendor app calls this "
            "*timers*. Same int/string split as `inside`."
        ),
    },
    FIELD_TIME: {
        "type": "string",
        # Declared, not observed: the clock is the one field whose value
        # changes between two runs of the generator, which would make the
        # committed document differ from a fresh one every second.
        "examples": ["Mon Aug 31 10:54:28 2026"],
        "description": (
            "The door's clock as a C `asctime` string, in its own timezone "
            "and with no offset. **Read-only** - there is no way to set it."
        ),
    },
    FIELD_FWINFO: {
        "type": "object",
        "description": (
            "Firmware and hardware versions, as five separate integers "
            "(`fw_maj`, `fw_min`, `fw_pat`, `ver`, `rev`)."
        ),
    },
    FIELD_BATTERY_PERCENT: {
        "type": "integer",
        "unit": "percent",
        **wire_bounds(FIELD_BATTERY_PERCENT),
        "description": "Battery charge. Reported as 0 when no battery is fitted.",
    },
    FIELD_BATTERY_PRESENT: {
        "type": "string",
        "enum": [SUCCESS_TRUE, SUCCESS_FALSE],
        "description": (
            "Whether a battery is fitted - a string, while `batteryPercent` beside it is an int."
        ),
    },
    FIELD_AC_PRESENT: {
        "type": "string",
        "enum": [SUCCESS_TRUE, SUCCESS_FALSE],
        "description": "Whether mains power is connected. A string, as `batteryPresent` is.",
    },
    FIELD_SENSOR_TRIGGER_VOLTAGE: {
        "type": "integer",
        "unit": "millivolts",
        "description": (
            "The stored collar threshold, in millivolts. This is the "
            "**getter's** name; the setter takes `voltage`."
        ),
    },
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE: {
        "type": "integer",
        "unit": "millivolts",
        "description": (
            "Sleep-mode collar threshold, in millivolts. Same getter/setter "
            "name split: the setter takes `voltage`."
        ),
    },
    FIELD_TOTAL_OPEN_CYCLES: {
        "type": "integer",
        "unit": "cycles",
        **wire_bounds(FIELD_TOTAL_OPEN_CYCLES),
        "description": "Completed open/close cycles since the counter was last cleared.",
    },
    FIELD_TOTAL_AUTO_RETRACTS: {
        "type": "integer",
        "unit": "retractions",
        **wire_bounds(FIELD_TOTAL_AUTO_RETRACTS),
        "description": "Times the door reversed on meeting an obstruction.",
    },
    FIELD_HAS_REMOTE_ID: {
        "type": "string",
        "enum": [SUCCESS_TRUE, SUCCESS_FALSE],
        "description": ("Whether a remote ID is paired. The field is spelled `has_id`."),
    },
    FIELD_HAS_REMOTE_KEY: {
        "type": "string",
        "enum": [SUCCESS_TRUE, SUCCESS_FALSE],
        "description": "Whether a remote key is paired. The field is `has_key`.",
    },
}


#: A ``"true"``/``"false"`` flag as it appears *inside* the ``settings``
#: object. Deliberately not shared with the top level: the same switch is
#: reported as the int ``1``/``0`` there, and a reader who assumes one
#: spelling from the other gets it wrong.
def _settings_flag(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "enum": [SUCCESS_TRUE, SUCCESS_FALSE],
        "description": description,
    }


def _notification_flag(event: str) -> dict[str, Any]:
    return _settings_flag(
        f"Whether the door announces {event}. Reported as a string; "
        "`SET_NOTIFICATIONS` demands a JSON boolean on the way in."
    )


#: One ``{"hour": h, "min": m}``. Four of these make a schedule.
_TIME_OF_DAY: dict[str, Any] = {
    "type": "object",
    "properties": {
        FIELD_HOUR: {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_SCHEDULE_HOUR,
            "description": (
                f"Hour, 24-hour clock, in the door's own timezone. **{MAX_SCHEDULE_HOUR} "
                "is legal and means the end of the day** - a window of "
                "`20:00-24:00` reports the sensor "
                f"enabled at 21:07. Hour {MAX_SCHEDULE_HOUR} is only a time when "
                "the minute is `0`."
            ),
        },
        FIELD_MINUTE: {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_SCHEDULE_MINUTE,
            "description": (
                f"Minute past the hour. Must be `0` when the hour is {MAX_SCHEDULE_HOUR}."
            ),
        },
    },
    "required": [FIELD_HOUR, FIELD_MINUTE],
}


def _window(side: str, edge: str) -> dict[str, Any]:
    return {
        **_TIME_OF_DAY,
        "description": (
            f"When the {side} sensor's window {edge}. A window whose end "
            "equals its start is **empty**, not all-day."
        ),
    }


#: The members of every object-valued field on the wire.
#:
#: Without this an object is documented as `type: object` and nothing
#: else - which is precisely backwards, because `settings`, `schedule` and
#: `notifications` carry the richest and least guessable structures in the
#: protocol. The same key means different things in different objects
#: (`inside` is a switch in `settings` and *which sensor a window governs*
#: in `schedule`), and the same switch is spelled differently at the top
#: level than nested, so these cannot be folded into :data:`FIELD_DOCS`.
#:
#: `tests/test_schemas.py` pins that every member the simulator actually
#: emits appears here, so a new settings key cannot ship undocumented.
OBJECT_FIELD_DOCS: dict[str, dict[str, dict[str, Any]]] = {
    FIELD_SETTINGS: {
        FIELD_POWER: _settings_flag(
            "Whether the door has power to its motor. Off means every open "
            "command is refused and the flap is down."
        ),
        FIELD_INSIDE: _settings_flag("Whether the indoor sensor may open the door."),
        FIELD_OUTSIDE: _settings_flag("Whether the outdoor sensor may open the door."),
        FIELD_AUTO: _settings_flag(
            "Whether the schedules are in force. The door's name for this is `timersEnabled`."
        ),
        FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: _settings_flag(
            "Whether the outdoor sensor is ignored while the door is closed, "
            "so a pet outside cannot let itself in."
        ),
        FIELD_CMD_LOCKOUT: _settings_flag(
            "Whether the door refuses remote open commands. The name reads "
            "backwards: `true` means commands ARE locked out."
        ),
        FIELD_AUTORETRACT: {
            "type": "integer",
            "description": (
                "A **bitfield**, not a flag. Bit 1 "
                f"(`{DOOR_OPTION_AUTORETRACT}`) is auto-retract on "
                "obstruction; the other bits are unidentified. Test the bit - "
                "never read this by truthiness."
            ),
        },
        FIELD_TZ: dict(FIELD_DOCS[FIELD_TZ]),
        FIELD_HOLD_OPEN_TIME: {
            "type": "integer",
            "unit": "centiseconds",
            **wire_bounds(FIELD_HOLD_OPEN_TIME),
            "description": (
                "This object's name for `holdTime`: how long the door stays "
                "open, in **centiseconds**."
            ),
        },
        FIELD_SENSOR_TRIGGER_VOLTAGE: {
            "type": "integer",
            "unit": "millivolts",
            **wire_bounds(FIELD_SENSOR_TRIGGER_VOLTAGE),
            "description": "Collar detection threshold while the door is awake.",
        },
        FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE: {
            "type": "integer",
            "unit": "millivolts",
            **wire_bounds(FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE),
            "description": "Collar detection threshold while the door is asleep.",
        },
    },
    FIELD_NOTIFICATIONS: {
        FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: _notification_flag(
            "a pet arriving at the indoor sensor"
        ),
        FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: _notification_flag(
            "a pet leaving the indoor sensor"
        ),
        FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: _notification_flag(
            "a pet arriving at the outdoor sensor"
        ),
        FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: _notification_flag(
            "a pet leaving the outdoor sensor"
        ),
        FIELD_LOW_BATTERY_NOTIFICATIONS: _notification_flag("the battery running low"),
    },
    FIELD_SCHEDULE: {
        FIELD_INDEX: {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_SCHEDULE_INDEX,
            "description": (
                "Which slot this schedule occupies. `SET_SCHEDULE` needs it "
                "here **and** as a sibling of `schedule`."
            ),
        },
        FIELD_ENABLED: {
            "type": "integer",
            "enum": [0, 1],
            "description": "Whether this slot is in force. An int here, not a string.",
        },
        FIELD_DAYSOFWEEK: {
            "type": "array",
            "items": {"type": "integer", "enum": [0, 1]},
            "minItems": 7,
            "maxItems": 7,
            "description": (
                "Seven flags, **Sunday first**, `1` for a day the schedule "
                "applies. A legacy integer bitmask with bit 0 = Sunday is "
                "also accepted on input."
            ),
        },
        FIELD_INSIDE: {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "Whether this schedule governs the **indoor** sensor. Note "
                "this is not the `settings` flag of the same name."
            ),
        },
        FIELD_OUTSIDE: {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "Whether this schedule governs the **outdoor** sensor. Note "
                "this is not the `settings` flag of the same name."
            ),
        },
        FIELD_INSIDE_PREFIX + FIELD_START_TIME_SUFFIX: _window("indoor", "opens"),
        FIELD_INSIDE_PREFIX + FIELD_END_TIME_SUFFIX: _window("indoor", "closes"),
        FIELD_OUTSIDE_PREFIX + FIELD_START_TIME_SUFFIX: _window("outdoor", "opens"),
        FIELD_OUTSIDE_PREFIX + FIELD_END_TIME_SUFFIX: _window("outdoor", "closes"),
    },
    FIELD_FWINFO: {
        FIELD_FW_MAJOR: {"type": "integer", "description": "Firmware major version."},
        FIELD_FW_MINOR: {"type": "integer", "description": "Firmware minor version."},
        FIELD_FW_PATCH: {"type": "integer", "description": "Firmware patch version."},
        FIELD_HW_VERSION: {"type": "integer", "description": "Hardware version."},
        FIELD_HW_REVISION: {"type": "integer", "description": "Hardware revision."},
    },
}


@dataclass(frozen=True)
class WireValue:
    """One value's wire spelling.

    Attributes:
        enable: The command that sets it - ``CMD_ENABLE_*`` for a switch,
            ``CMD_SET_*`` for a value.
        disable: The command that clears a switch, or ``None`` for a value
            that is set rather than toggled.
        payload: Builds the response/broadcast body from the state. Reads
            the state rather than taking the value, because that is what
            the door does: it answers with what it *stored*, which for a
            saturating or ignored write is not what it was sent.
        getter: The ``GET_*`` command that reads this value back, where
            the door has one that reads *only* this value. The reply is
            the payload, so naming the command here is all a handler
            needs - see ``_getter_handler``. ``None`` where no such
            command exists, or where the getter answers with more than
            this one value (`GET_SENSORS` reports both sensors).
    """

    enable: str
    payload: Callable[[DoorSimulatorState], dict[str, Any]]
    disable: str | None = None
    getter: str | None = None

    def command_for(self, value: Any) -> str:
        """The command name a change to ``value`` is announced under."""
        if self.disable is None:
            return self.enable
        return self.enable if value else self.disable


#: Getters that report several registry values at once, so they cannot be
#: any one value's :attr:`WireValue.getter` - but whose values `GET_SETTINGS`
#: reports all the same, which is what the cross-reference tag is for.
MULTI_VALUE_GETTERS: dict[str, tuple[str, ...]] = {
    CMD_GET_SENSORS: ("inside", "outside"),
}


def _flag_field(field: str, name: str):
    """A top-level ``0``/``1`` field, as the sensor arming uses."""
    return lambda s: {field: wire_int_flag(read(s, name))}


def _string_flag_field(field: str, name: str):
    """A top-level ``"true"``/``"false"`` field.

    Not every top-level flag is an int. The door answers `GET_SENSORS`
    with ints and `GET_POWER`/`GET_TIMERS_ENABLED` with strings, and
    spells both as strings again inside `settings`. Whether a field is
    one or the other is the field's own business, not a rule.
    """
    return lambda s: {field: wire_bool_string(read(s, name))}


def _plain_field(field: str, render: Callable[[DoorSimulatorState], Any]):
    """A top-level field carrying a translated value."""
    return lambda s: {field: render(s)}


def _whole_settings(s: DoorSimulatorState) -> dict[str, Any]:
    """The whole settings object.

    The auto-retract pair answers
    with every setting, not just the one it changed.
    """
    return {FIELD_SETTINGS: settings_payload(s)}


def wire_timezone(state: DoorSimulatorState) -> str:
    """The timezone as the door puts it on the wire: **POSIX**.

    The door answers ``EST5EDT,M3.2.0,M11.1.0``, never an IANA name.

    No conversion happens here. The value is stored as POSIX by the one
    setter that writes it, so `GET_TIMEZONE`, `GET_SETTINGS`, the
    `SET_TIMEZONE` reply and the broadcast are all reading the same
    stored string rather than four chances to convert it differently.
    """
    return str(read(state, "timezone"))


def hold_time_centiseconds(state: DoorSimulatorState) -> int:
    """The hold time as the wire carries it: centiseconds, not seconds."""
    return int(read(state, "hold_time") * 100)


def settings_payload(state: DoorSimulatorState) -> dict[str, Any]:
    """The whole ``settings`` object, in the door's own spellings.

    Field by field: the six flags are
    ``"true"``/``"false"`` STRINGS - not the ``1``/``0`` ints the same
    values take at the top level of a reply - ``doorOptions``,
    ``holdOpenTime`` and the two voltages are INTS, and ``tz`` is a POSIX
    string. Same key set the real unit returned, in the same spellings.
    """
    return {
        FIELD_POWER: wire_bool_string(read(state, "power")),
        FIELD_INSIDE: wire_bool_string(read(state, "inside")),
        FIELD_OUTSIDE: wire_bool_string(read(state, "outside")),
        FIELD_AUTO: wire_bool_string(read(state, "auto")),
        FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: wire_bool_string(read(state, "safety_lock")),
        FIELD_CMD_LOCKOUT: wire_bool_string(read(state, "cmd_lockout")),
        # A BITFIELD, not a flag:
        # DISABLE_AUTORETRACT leaves this 0 and ENABLE_AUTORETRACT leaves
        # it 2. Other bits exist but are unidentified, so the simulator
        # sets only the one it knows.
        FIELD_AUTORETRACT: DOOR_OPTION_AUTORETRACT if read(state, "autoretract") else 0,
        FIELD_TZ: wire_timezone(state),
        FIELD_HOLD_OPEN_TIME: hold_time_centiseconds(state),
        FIELD_SENSOR_TRIGGER_VOLTAGE: read(state, "sensor_trigger_voltage"),
        FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE: read(state, "sleep_sensor_trigger_voltage"),
    }


def notifications_payload(state: DoorSimulatorState) -> dict[str, Any]:
    """The five notification switches, as the door reports them.

    All five are ``"true"``/``"false"`` **strings**. Note the asymmetry
    with the write path:
    ``SET_NOTIFICATIONS`` demands JSON *booleans* and silently ignores
    strings.
    """
    return {
        NOTIFICATION_FIELDS[name]: wire_bool_string(read(state, f"notify_{name}"))
        for name in NOTIFICATION_NAMES
    }


#: Value name -> wire spelling. Every key is a name in
#: :data:`~powerpetdoor.simulator.values.VALUES`; ``tests`` pins that.
WIRE_VALUES: dict[str, WireValue] = {
    "inside": WireValue(CMD_ENABLE_INSIDE, _flag_field(FIELD_INSIDE, "inside"), CMD_DISABLE_INSIDE),
    "outside": WireValue(
        CMD_ENABLE_OUTSIDE, _flag_field(FIELD_OUTSIDE, "outside"), CMD_DISABLE_OUTSIDE
    ),
    "auto": WireValue(
        CMD_ENABLE_AUTO,
        _string_flag_field(FIELD_AUTO, "auto"),
        CMD_DISABLE_AUTO,
        getter=CMD_GET_TIMERS_ENABLED,
    ),
    "power": WireValue(
        CMD_POWER_ON,
        _string_flag_field(FIELD_POWER, "power"),
        CMD_POWER_OFF,
        getter=CMD_GET_POWER,
    ),
    # All three answer with the WHOLE settings object, not the one field
    # they changed. These are also the three settings with no getter of
    # their own, which makes the fat reply the only read they offer.
    "safety_lock": WireValue(
        CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
        _whole_settings,
        CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    ),
    "cmd_lockout": WireValue(
        CMD_ENABLE_CMD_LOCKOUT,
        _whole_settings,
        CMD_DISABLE_CMD_LOCKOUT,
    ),
    "autoretract": WireValue(CMD_ENABLE_AUTORETRACT, _whole_settings, CMD_DISABLE_AUTORETRACT),
    "hold_time": WireValue(
        CMD_SET_HOLD_TIME,
        # Centiseconds on the wire, seconds in the state.
        _plain_field(FIELD_HOLD_TIME, hold_time_centiseconds),
        getter=CMD_GET_HOLD_TIME,
    ),
    "timezone": WireValue(
        CMD_SET_TIMEZONE,
        # The POSIX form, which is what a real door answers with.
        _plain_field(FIELD_TZ, wire_timezone),
        getter=CMD_GET_TIMEZONE,
    ),
    "sensor_trigger_voltage": WireValue(
        CMD_SET_SENSOR_TRIGGER_VOLTAGE,
        # The reply echoes the GETTER's field name, not the setter's.
        _plain_field(FIELD_SENSOR_TRIGGER_VOLTAGE, lambda s: read(s, "sensor_trigger_voltage")),
        getter=CMD_GET_SENSOR_TRIGGER_VOLTAGE,
    ),
    "sleep_sensor_trigger_voltage": WireValue(
        CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
        _plain_field(
            FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE, lambda s: read(s, "sleep_sensor_trigger_voltage")
        ),
        getter=CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    ),
}

#: The switches, in a stable order - the ones with an enable/disable pair.
WIRE_SWITCHES: tuple[str, ...] = tuple(
    sorted(n for n, w in WIRE_VALUES.items() if w.disable is not None)
)

__all__ = [
    "WIRE_SWITCHES",
    "WIRE_VALUES",
    "WireValue",
    "hold_time_centiseconds",
    "notifications_payload",
    "read",
    "settings_payload",
    "wire_timezone",
]
