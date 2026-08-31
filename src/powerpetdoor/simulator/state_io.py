# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Reading and writing the simulator's configuration as a document.

One schema serves three callers: ``--initial-state`` at startup, ``reset``
at runtime, and the ``state`` control command that lets a remote client see
what a daemon is configured as. They were three requests; they are one
missing capability, because :class:`DoorSimulatorState` had no serializable
form at all and the only state query on the control channel emitted prose.

**JSON is the baseline, YAML is a convenience.** PyYAML is an optional
dependency here - ``scripting`` guards every use of it - and ``tzdata`` is
the only hard runtime requirement, so a state file must not be the thing
that makes PyYAML mandatory. JSON is also what the door itself speaks, so
it is the natural spelling for a document describing one.

Documents are **partial**: every section and every key is optional and is
merged over the dataclass defaults. "Like the defaults, but the battery is
at 12%" is what a fixture actually wants to say. Unknown keys are refused
rather than ignored, matching the script DSL - a mistyped setting that
silently does nothing is the failure mode this tree has twice decided
against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..i18n import t
from ..sanitize import sanitize_text
from ..schedule import MAX_SCHEDULE_INDEX, valid_schedule_time
from ..tz_utils import to_posix_tz
from .coerce import CoercionError, coerce_bool, coerce_number
from .state import BatteryConfig, DoorSimulatorState, DoorTimingConfig, Schedule

try:  # pragma: no cover - exercised by the YAML-absent test via monkeypatch
    import yaml

    YAML_AVAILABLE = True
except ImportError:  # pragma: no cover - PyYAML is an optional extra
    yaml = None  # type: ignore[assignment]
    YAML_AVAILABLE = False


class StateDocumentError(Exception):
    """A state document is malformed, or names something that is not a field."""


#: Bounds for the numeric fields a document may set. Every one is a value
#: the wire path also guards; a document must not be the way around them.
_NUMBER_BOUNDS: dict[str, tuple[float, float]] = {
    "hold_time": (0.0, 3600.0),
    # **Measured**, not guessed: the device stores a signed 32-bit value
    # and saturates there. 1023 (a 10-bit-ADC guess) rejected a document
    # describing a real door, which reports 2000.
    "sensor_trigger_voltage": (0.0, float(2**31 - 1)),
    "sleep_sensor_trigger_voltage": (0.0, float(2**31 - 1)),
    "percent": (0.0, 100.0),
    "charge_rate": (0.0, 100.0),
    "discharge_rate": (0.0, 100.0),
    "rise_time": (0.0, 60.0),
    "slowing_time": (0.0, 60.0),
    "closing_start_time": (0.0, 60.0),
    "closing_top_time": (0.0, 60.0),
    "closing_mid_time": (0.0, 60.0),
    "sensor_retrigger_window": (0.0, 60.0),
    "fw_major": (0.0, 255.0),
    "fw_minor": (0.0, 255.0),
    "fw_patch": (0.0, 255.0),
    "hw_ver": (0.0, 255.0),
    "hw_rev": (0.0, 255.0),
}

#: ``settings`` keys, and how each is coerced. These are the door's own
#: configuration - what ``GET_SETTINGS`` reports, plus the timezone.
_SETTINGS_BOOLS = (
    "power",
    "inside",
    "outside",
    "auto",
    "autoretract",
    "safety_lock",
    "cmd_lockout",
)
_SETTINGS_NUMBERS = ("hold_time", "sensor_trigger_voltage", "sleep_sensor_trigger_voltage")
_SETTINGS_INTS = ("sensor_trigger_voltage", "sleep_sensor_trigger_voltage")

_BATTERY_BOOLS = ("present", "ac_present")
_BATTERY_NUMBERS = ("percent", "charge_rate", "discharge_rate")

_TIMING_NUMBERS = (
    "rise_time",
    "slowing_time",
    "closing_start_time",
    "closing_top_time",
    "closing_mid_time",
    "sensor_retrigger_window",
)

_HARDWARE_INTS = ("fw_major", "fw_minor", "fw_patch", "hw_ver", "hw_rev")
_HARDWARE_BOOLS = ("has_remote_id", "has_remote_key")

_NOTIFICATION_BOOLS = (
    "sensor_on_indoor",
    "sensor_off_indoor",
    "sensor_on_outdoor",
    "sensor_off_outdoor",
    "low_battery",
)

_SCHEDULE_KEYS = frozenset(
    {"index", "enabled", "days_of_week", "inside", "outside", "start", "end"}
)

#: The document's top-level sections.
DOCUMENT_SECTIONS = frozenset(
    {"settings", "battery", "timing", "hardware", "notifications", "schedules"}
)


def _fail(message: str) -> StateDocumentError:
    return StateDocumentError(message)


def _reject_unknown(present: Any, known: Any, where: str) -> None:
    """Refuse keys that are not fields, naming them and the alternatives."""
    if not isinstance(present, dict):
        raise _fail(
            t(
                "simulator.state_io.section_must_be_mapping",
                "{where} must be a mapping, got {arg0}",
                where=where,
                arg0=type(present).__name__,
            )
        )
    unknown = sorted(set(present) - set(known))
    if unknown:
        raise _fail(
            t(
                "simulator.state_io.unknown_key_in_section",
                "Unknown key(s) in {where}: {arg0}. Use: {arg1}",
                where=where,
                arg0=", ".join(sanitize_text(k) for k in unknown),
                arg1=", ".join(sorted(known)),
            )
        )


def _number(section: dict, key: str, where: str) -> float:
    minimum, maximum = _NUMBER_BOUNDS[key]
    try:
        return coerce_number(section[key], f"{where}.{key}", minimum, maximum)
    except CoercionError as exc:
        raise _fail(str(exc)) from None


def _flag(section: dict, key: str, where: str) -> bool:
    """Read a boolean field, refusing anything that is not one."""
    try:
        return coerce_bool(section[key], f"{where}.{key}")
    except CoercionError as exc:
        raise _fail(str(exc)) from None


def _flag_value(value: object, where: str) -> bool:
    """Read a boolean that is not a section key, labelling any failure."""
    try:
        return coerce_bool(value, where)
    except CoercionError as exc:
        raise _fail(str(exc)) from None


def _parse_hhmm(value: object, where: str) -> tuple[int, int]:
    """Read a ``"HH:MM"`` window edge.

    ``24:00`` is legal and means end-of-day: the schedule engine is
    strictly ``start <= now < end``, so a window ending at ``23:59`` really
    does leave the sensor off for the final minute of the day.
    """
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise _fail(
            t(
                "simulator.state_io.time_must_be_hhmm",
                "{where} must be HH:MM, got {arg0!r}",
                where=where,
                arg0=sanitize_text(value),
            )
        )
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise _fail(
            t(
                "simulator.state_io.time_must_be_hhmm",
                "{where} must be HH:MM, got {arg0!r}",
                where=where,
                arg0=sanitize_text(value),
            )
        ) from None
    if not valid_schedule_time(hour, minute):
        raise _fail(
            t(
                "simulator.state_io.time_out_of_range",
                "{where} must be between 00:00 and 24:00, got {arg0!r}",
                where=where,
                arg0=sanitize_text(value),
            )
        )
    return hour, minute


def state_to_document(state: DoorSimulatorState) -> dict:
    """Serialize a state's *configuration* as a document.

    Live motion state - ``door_status``, the sensor-active flags, whether
    an obstruction is in the doorway - is owned by the running engine and
    is deliberately absent. A door does not boot mid-rise with a boot
    under it, and a document that could claim otherwise would be a way to
    put the engine in a state it cannot reach on its own.
    """
    return {
        "settings": {
            "power": state.power,
            "inside": state.inside,
            "outside": state.outside,
            "auto": state.auto,
            "autoretract": state.autoretract,
            "safety_lock": state.safety_lock,
            "cmd_lockout": state.cmd_lockout,
            "hold_time": state.hold_time,
            "timezone": state.timezone,
            "sensor_trigger_voltage": state.sensor_trigger_voltage,
            "sleep_sensor_trigger_voltage": state.sleep_sensor_trigger_voltage,
        },
        "battery": {
            "percent": state.battery_percent,
            "present": state.battery_present,
            "ac_present": state.ac_present,
            "charge_rate": state.battery_config.charge_rate,
            "discharge_rate": state.battery_config.discharge_rate,
        },
        "timing": {
            "rise_time": state.timing.rise_time,
            "slowing_time": state.timing.slowing_time,
            "closing_start_time": state.timing.closing_start_time,
            "closing_top_time": state.timing.closing_top_time,
            "closing_mid_time": state.timing.closing_mid_time,
            "sensor_retrigger_window": state.timing.sensor_retrigger_window,
        },
        "hardware": {
            "fw_major": state.fw_major,
            "fw_minor": state.fw_minor,
            "fw_patch": state.fw_patch,
            "hw_ver": state.hw_ver,
            "hw_rev": state.hw_rev,
            "has_remote_id": state.has_remote_id,
            "has_remote_key": state.has_remote_key,
        },
        "notifications": {
            "sensor_on_indoor": state.sensor_on_indoor,
            "sensor_off_indoor": state.sensor_off_indoor,
            "sensor_on_outdoor": state.sensor_on_outdoor,
            "sensor_off_outdoor": state.sensor_off_outdoor,
            "low_battery": state.low_battery,
        },
        "schedules": [
            {
                "index": s.index,
                "enabled": s.enabled,
                "days_of_week": list(s.days_of_week),
                "inside": s.inside,
                "outside": s.outside,
                "start": f"{s.start_hour:02d}:{s.start_min:02d}",
                "end": f"{s.end_hour:02d}:{s.end_min:02d}",
            }
            for s in sorted(state.schedules.values(), key=lambda s: s.index)
        ],
    }


def _apply_settings(state: DoorSimulatorState, section: dict) -> None:
    known = (*_SETTINGS_BOOLS, *_SETTINGS_NUMBERS, "timezone")
    _reject_unknown(section, known, "settings")
    for key in _SETTINGS_BOOLS:
        if key in section:
            setattr(state, key, _flag(section, key, "settings"))
    for key in _SETTINGS_NUMBERS:
        if key in section:
            number = _number(section, key, "settings")
            setattr(state, key, int(number) if key in _SETTINGS_INTS else number)
    if "timezone" in section:
        # Stored as POSIX like every other writer, so a document may say
        # `America/New_York` and the wire still only ever sees the rule.
        try:
            state.timezone = to_posix_tz(str(section["timezone"]))
        except ValueError as exc:
            raise _fail(
                t(
                    "simulator.state_io.bad_timezone",
                    "settings.timezone: {reason}",
                    reason=str(exc),
                )
            ) from None


def _apply_battery(state: DoorSimulatorState, section: dict) -> None:
    _reject_unknown(section, (*_BATTERY_BOOLS, *_BATTERY_NUMBERS), "battery")
    if "percent" in section:
        state.battery_percent = int(_number(section, "percent", "battery"))
    if "present" in section:
        state.battery_present = _flag(section, "present", "battery")
    if "ac_present" in section:
        state.ac_present = _flag(section, "ac_present", "battery")
    for key in ("charge_rate", "discharge_rate"):
        if key in section:
            setattr(state.battery_config, key, _number(section, key, "battery"))


def _apply_timing(state: DoorSimulatorState, section: dict) -> None:
    _reject_unknown(section, _TIMING_NUMBERS, "timing")
    for key in _TIMING_NUMBERS:
        if key in section:
            setattr(state.timing, key, _number(section, key, "timing"))


def _apply_hardware(state: DoorSimulatorState, section: dict) -> None:
    _reject_unknown(section, (*_HARDWARE_INTS, *_HARDWARE_BOOLS), "hardware")
    for key in _HARDWARE_INTS:
        if key in section:
            setattr(state, key, int(_number(section, key, "hardware")))
    for key in _HARDWARE_BOOLS:
        if key in section:
            setattr(state, key, _flag(section, key, "hardware"))


def _apply_notifications(state: DoorSimulatorState, section: dict) -> None:
    _reject_unknown(section, _NOTIFICATION_BOOLS, "notifications")
    for key in _NOTIFICATION_BOOLS:
        if key in section:
            setattr(state, key, _flag(section, key, "notifications"))


def _apply_schedules(state: DoorSimulatorState, entries: object) -> None:
    """Replace the schedule table wholesale.

    Not merged per index: a document listing schedules describes the whole
    table, so a reset to a two-entry document must not leave a third entry
    behind from whatever ran before it.
    """
    if not isinstance(entries, list):
        raise _fail(
            t(
                "simulator.state_io.schedules_must_be_list",
                "schedules must be a list, got {arg0}",
                arg0=type(entries).__name__,
            )
        )
    schedules: dict[int, Schedule] = {}
    for position, entry in enumerate(entries):
        where = f"schedules[{position}]"
        _reject_unknown(entry, _SCHEDULE_KEYS, where)
        if "index" not in entry:
            raise _fail(
                t(
                    "simulator.state_io.schedule_needs_index",
                    "{where} has no index",
                    where=where,
                )
            )
        index = int(_coerce_index(entry["index"], where))
        start_hour, start_min = _parse_hhmm(entry.get("start", "00:00"), f"{where}.start")
        end_hour, end_min = _parse_hhmm(entry.get("end", "24:00"), f"{where}.end")
        schedules[index] = Schedule(
            index=index,
            enabled=_flag_value(entry.get("enabled", True), f"{where}.enabled"),
            days_of_week=_coerce_days(entry.get("days_of_week", [True] * 7), where),
            inside=_flag_value(entry.get("inside", False), f"{where}.inside"),
            outside=_flag_value(entry.get("outside", False), f"{where}.outside"),
            start_hour=start_hour,
            start_min=start_min,
            end_hour=end_hour,
            end_min=end_min,
        )
    state.schedules = schedules


def _coerce_index(value: object, where: str) -> float:
    try:
        return coerce_number(value, f"{where}.index", 0, MAX_SCHEDULE_INDEX)
    except CoercionError as exc:
        raise _fail(str(exc)) from None


def _coerce_days(value: object, where: str) -> list[bool]:
    if not isinstance(value, list) or len(value) != 7:
        raise _fail(
            t(
                "simulator.state_io.days_must_be_seven",
                "{where}.days_of_week must be a list of 7 values, "
                "[Sun, Mon, Tue, Wed, Thu, Fri, Sat]",
                where=where,
            )
        )
    return [_flag_value(day, f"{where}.days_of_week") for day in value]


_SECTION_APPLIERS = {
    "settings": _apply_settings,
    "battery": _apply_battery,
    "timing": _apply_timing,
    "hardware": _apply_hardware,
    "notifications": _apply_notifications,
}


def apply_document(state: DoorSimulatorState, document: object) -> None:
    """Merge ``document`` over ``state``, in place.

    Raises:
        StateDocumentError: If the document is not a mapping, names a
            section or key that is not a field, or supplies a value the
            field will not take.
    """
    _reject_unknown(document, DOCUMENT_SECTIONS, "the document")
    assert isinstance(document, dict)  # narrowed by _reject_unknown
    for name, applier in _SECTION_APPLIERS.items():
        if name in document:
            applier(state, document[name])
    if "schedules" in document:
        _apply_schedules(state, document["schedules"])


def state_from_document(document: object) -> DoorSimulatorState:
    """Build a fresh state from defaults plus ``document``."""
    state = DoorSimulatorState(battery_config=BatteryConfig(), timing=DoorTimingConfig())
    apply_document(state, document)
    return state


def parse_document(text: str, *, source: str, prefer_yaml: bool) -> dict:
    """Parse document ``text``, as JSON or (when available) YAML.

    Args:
        text: File contents.
        source: File name, for the error message.
        prefer_yaml: Whether the extension asked for YAML.

    Raises:
        StateDocumentError: On a syntax error, or when YAML was asked for
            and PyYAML is not installed.
    """
    if prefer_yaml:
        if not YAML_AVAILABLE:
            raise _fail(
                t(
                    "simulator.state_io.yaml_unavailable",
                    "{source} is YAML, which needs PyYAML installed "
                    "(pip install pyyaml) - or write the state as JSON",
                    source=sanitize_text(source),
                )
            )
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise _fail(
                t(
                    "simulator.state_io.invalid_document",
                    "{source} is not valid: {arg0}",
                    source=sanitize_text(source),
                    arg0=sanitize_text(str(exc).replace("\n", " ")),
                )
            ) from None
    else:
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _fail(
                t(
                    "simulator.state_io.invalid_document",
                    "{source} is not valid: {arg0}",
                    source=sanitize_text(source),
                    arg0=sanitize_text(str(exc)),
                )
            ) from None

    if loaded is None:
        # An empty file is a document that changes nothing, not an error:
        # it is the honest spelling of "start from the defaults".
        return {}
    if not isinstance(loaded, dict):
        raise _fail(
            t(
                "simulator.state_io.document_must_be_mapping",
                "{source} must contain a mapping, got {arg0}",
                source=sanitize_text(source),
                arg0=type(loaded).__name__,
            )
        )
    return loaded


#: Extensions a state file may have. JSON first: it always works.
STATE_SUFFIXES = (".json", ".yaml", ".yml")


def load_document(path: str | Path) -> dict:
    """Read and parse a state document from disk.

    Raises:
        StateDocumentError: If the file cannot be read or does not parse.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise _fail(
            t(
                "simulator.state_io.cannot_read",
                "Cannot read state file {source}: {arg0}",
                source=sanitize_text(str(path)),
                arg0=sanitize_text(exc.strerror or str(exc)),
            )
        ) from None
    return parse_document(
        text, source=path.name, prefer_yaml=path.suffix.lower() in (".yaml", ".yml")
    )


def state_documents_in(states_dir: str | Path | None) -> list[str]:
    """The document names loadable by bare name from ``states_dir``.

    Only files that resolve *inside* the directory, so what is advertised
    is exactly what will load - the script listing learned this already,
    where a symlink out of the directory was listed and then refused by
    name, in a message that contradicted itself inside one line.
    """
    from .scripting import path_escapes_directory

    if not states_dir:
        return []
    directory = Path(states_dir)
    base = directory.resolve()
    names = set()
    for suffix in STATE_SUFFIXES:
        for path in directory.glob(f"*{suffix}"):
            if not path_escapes_directory(path, base):
                names.add(path.stem)
    return sorted(names)


def render_state_listing(states_dir: str | Path | None) -> list[str]:
    """Render "what can `reset` load?" for every surface that answers it.

    There are no built-in state documents - a shipped one would be
    invented device configuration rather than observed - so with no
    directory configured the listing names the missing flag instead of
    printing an empty list.
    """
    if not states_dir:
        return [
            t(
                "simulator.state_io.no_states_dir_configured",
                "No state documents (start the simulator with --states-dir)",
            )
        ]
    lines = [
        t(
            "simulator.state_io.state_documents_from",
            "State documents from {states_dir}:",
            states_dir=str(states_dir),
        )
    ]
    names = state_documents_in(states_dir)
    lines.extend(f"  {name}" for name in names)
    if not names:
        # Header even when empty, so the flag's effect is visible rather
        # than silently absent - the same rule the script listing follows.
        lines.append("  (none)")
    return lines
