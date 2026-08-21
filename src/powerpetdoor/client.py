# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Power Pet Door client for network communication."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import async_timeout

from .const import (
    CMD_CHECK_RESET_REASON,
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
    CMD_GET_AUTO,
    CMD_GET_AUTORETRACT,
    CMD_GET_CMD_LOCKOUT,
    CMD_GET_DOOR_BATTERY,
    CMD_GET_DOOR_OPEN_STATS,
    CMD_GET_DOOR_STATUS,
    CMD_GET_HOLD_TIME,
    CMD_GET_HW_INFO,
    CMD_GET_NOTIFICATIONS,
    CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK,
    CMD_GET_POWER,
    CMD_GET_SCHEDULE,
    CMD_GET_SCHEDULE_LIST,
    CMD_GET_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_SENSORS,
    CMD_GET_SETTINGS,
    CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_GET_TIMEZONE,
    CMD_HAS_REMOTE_ID,
    CMD_HAS_REMOTE_KEY,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_TIMEZONE,
    COMMAND,
    COMMAND_PRIORITIES,
    CONFIG,
    DOOR_STATUS,
    FIELD_AC_PRESENT,
    FIELD_AUTO,
    FIELD_AUTORETRACT,
    FIELD_BATTERY_PERCENT,
    FIELD_BATTERY_PRESENT,
    FIELD_CMD,
    FIELD_CMD_LOCKOUT,
    FIELD_DIRECTION,
    FIELD_DOOR_STATUS,
    FIELD_FWINFO,
    FIELD_HAS_REMOTE_ID,
    FIELD_HAS_REMOTE_KEY,
    FIELD_HOLD_OPEN_TIME,
    FIELD_HOLD_TIME,
    FIELD_INDEX,
    FIELD_INSIDE,
    FIELD_LOW_BATTERY_NOTIFICATIONS,
    FIELD_MSG_ID,
    FIELD_MSG_ID_RESPONSE,
    FIELD_NOTIFICATIONS,
    FIELD_OUTSIDE,
    FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
    FIELD_POWER,
    FIELD_REASON,
    FIELD_RESET_REASON,
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_STATE,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SETTINGS,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SUCCESS,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    MINIMUM_TIME_BETWEEN_MSGS,
    NOTIFY_LOW_BATTERY,
    NOTIFY_SENSOR_INDOOR,
    NOTIFY_SENSOR_OUTDOOR,
    PHONE_TO_DOOR,
    PING,
    PONG,
    PRIORITY_CRITICAL,
    PRIORITY_LOW,
    SUCCESS_TRUE,
)
from .framing import MAX_BUFFER_SIZE, extract_frames, find_frame_end

_LOGGER = logging.getLogger(__name__)

#: Maximum retry attempts before dropping a message
MAX_FAILED_MSG = 2
MAX_FAILED_PINGS = 3


class CommandError(Exception):
    """A device command failed or its response could not be processed.

    Raised into futures returned by ``send_message(..., notify=True)`` when
    the device reports failure (``success: "false"``), when a response is
    missing its success field, or when a response payload is malformed.

    Attributes:
        cmd: The command that failed, or None if the response omitted it.
        reason: The failure reason reported by the device (or a local
            description of the parse failure), or None if not provided.
    """

    def __init__(self, cmd: str | None, reason: str | None = None) -> None:
        self.cmd = cmd
        self.reason = reason
        message = f"Command {cmd or '<unknown>'} failed"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


class ResponseHandlerRegistry:
    """Registry for client response handlers.

    This provides a decorator-based system for mapping command responses
    to their handler methods, eliminating the need for large if/elif chains.
    """

    _handlers: dict[str, Callable] = {}

    @classmethod
    def handler(cls, *cmds: str):
        """Decorator to register a response handler for one or more commands.

        Args:
            cmds: One or more command strings this handler processes

        Example:
            @ResponseHandlerRegistry.handler(CMD_GET_POWER, CMD_POWER_ON, CMD_POWER_OFF)
            def _handle_power(self, msg, future):
                ...
        """

        def decorator(func):
            for cmd in cmds:
                cls._handlers[cmd] = func
            return func

        return decorator

    @classmethod
    def get(cls, cmd: str) -> Callable | None:
        """Get the handler for a command, or None if not found."""
        return cls._handlers.get(cmd)


@dataclass(order=True)
class PrioritizedMessage:
    """A message with priority for the priority queue.

    Lower priority values are processed first.
    Sequence number ensures FIFO order within the same priority level.
    """

    priority: int
    sequence: int = field(compare=True)
    data: Any = field(compare=False)


def find_end(s: str) -> int | None:
    """Find the end of a JSON object in a string.

    Thin wrapper around :func:`powerpetdoor.framing.find_frame_end`, kept
    for backwards compatibility. The scanner is string-aware (braces
    inside JSON string values are ignored) and never raises.

    Args:
        s: String potentially containing JSON object(s)

    Returns:
        Position after the closing brace of the first complete JSON object,
        or None if the string is empty, does not start with ``{``, or
        contains no complete object.
    """
    return find_frame_end(s)


def make_bool(v: str | int | bool):
    """Convert various types to boolean.

    Args:
        v: Value to convert (string, int, or bool)

    Returns:
        True for "1", "true", "yes", "on", non-zero int, or True
        False for "0", "false", "no", "off", zero, or False
        None for unrecognized strings or None input
    """
    if isinstance(v, str):
        if v.lower() in ("1", "true", "yes", "on"):
            return True
        if v.lower() in ("0", "false", "no", "off"):
            return False
        return None
    elif isinstance(v, int):
        return v != 0
    else:
        return v


class PowerPetDoorClient:
    """Client for communicating with Power Pet Door devices.

    This client handles the network protocol for Power Pet Door devices,
    including connection management, message queuing with priorities,
    keepalive/ping-pong, and callback-based event notification.

    Args:
        host: IP address or hostname of the Power Pet Door
        port: TCP port number (typically 3000)
        keepalive: Seconds between keepalive pings (0 to disable)
        timeout: Seconds to wait for responses
        reconnect: Seconds to wait before reconnecting after disconnect
        loop: Optional asyncio event loop (creates one if not provided)

    Example:
        client = PowerPetDoorClient("192.168.1.100", 3000, 30.0, 10.0, 5.0)
        client.add_listener("my_app", door_status_update=lambda s: print(f"Door: {s}"))
        client.start()
    """

    def __init__(
        self, host: str, port: int, keepalive: float, timeout: float, reconnect: float, loop=None
    ) -> None:
        self.cfg_host = host
        self.cfg_port = port
        self.cfg_keepalive = keepalive
        self.cfg_timeout = timeout
        self.cfg_reconnect = reconnect

        self._shutdown = False
        self._ownLoop = False
        self._eventLoop = None
        self._transport = None
        self._keepalive = None
        self._check_receipt = None
        self._last_ping = None
        self._last_command = None
        self._can_dequeue = False
        self._last_send = 0.0
        self._last_ping_time = 0.0
        self._failed_msg = 0
        self._failed_pings = 0
        self._buffer = ""
        self._outstanding = {}
        self._queue = queue.PriorityQueue()
        self._msg_sequence = 0  # Counter for FIFO ordering within same priority

        if loop:
            _LOGGER.info("Latching onto an existing event loop.")
            self._ownLoop = False
            self._eventLoop = loop
        else:
            self._ownLoop = True
            self._eventLoop = asyncio.new_event_loop()

        self.msgId = 1
        self.replyMsgId = None

        self.door_status_listeners: dict[str, Callable[[str], None]] = {}
        self.settings_listeners: dict[str, Callable[[dict], None]] = {}
        self.sensor_listeners: dict[str, dict[str, Callable[[str, bool | None], None]]] = {
            FIELD_POWER: {},
            FIELD_INSIDE: {},
            FIELD_OUTSIDE: {},
            FIELD_AUTO: {},
            FIELD_OUTSIDE_SENSOR_SAFETY_LOCK: {},
            FIELD_CMD_LOCKOUT: {},
            FIELD_AUTORETRACT: {},
        }
        self.notifications_listeners: dict[str, dict[str, Callable[[bool], None]]] = {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: {},
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: {},
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: {},
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: {},
            FIELD_LOW_BATTERY_NOTIFICATIONS: {},
        }
        self.stats_listeners: dict[str, dict[str, Callable[[int], None]]] = {
            FIELD_TOTAL_OPEN_CYCLES: {},
            FIELD_TOTAL_AUTO_RETRACTS: {},
        }
        self.hw_info_listeners: dict[str, Callable[[dict], None]] = {}
        self.battery_listeners: dict[str, Callable[[dict], None]] = {}

        self.timezone_listeners: dict[str, Callable[[str], None]] = {}
        self.hold_time_listeners: dict[str, Callable[[int], None]] = {}
        self.sensor_trigger_voltage_listeners: dict[str, Callable[[int], None]] = {}
        self.sleep_sensor_trigger_voltage_listeners: dict[str, Callable[[int], None]] = {}

        self.remote_id_listeners: dict[str, Callable[[bool], None]] = {}
        self.remote_key_listeners: dict[str, Callable[[bool], None]] = {}
        self.reset_reason_listeners: dict[str, Callable[[str], None]] = {}
        self.schedule_update_listeners: dict[str, Callable[[dict], None]] = {}
        self.schedule_delete_listeners: dict[str, Callable[[int], None]] = {}
        self.notification_event_listeners: dict[str, Callable[[str, str | None], None]] = {}

        self.on_connect: dict[str, Callable[[], Awaitable[None]]] = {}
        self.on_disconnect: dict[str, Callable[[], Awaitable[None]]] = {}
        self.on_ping: dict[str, Callable[[int], None]] = {}

    # Theses functions wrap asyncio but ensure the loop is correct!
    def ensure_future(self, *args: Any, **kwargs: Any):
        return asyncio.ensure_future(*args, loop=self._eventLoop, **kwargs)

    def run_coroutine_threadsafe(self, *args: Any, **kwargs: Any):
        return asyncio.run_coroutine_threadsafe(*args, loop=self._eventLoop, **kwargs)

    def add_handlers(
        self,
        name: str,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        on_ping: Callable[[int], None] | None = None,
    ) -> None:
        """Register connection lifecycle callbacks.

        Args:
            name: Unique identifier for this set of handlers
            on_connect: Called when connection is established
            on_disconnect: Called when connection is lost
            on_ping: Called with latency in ms after successful ping
        """
        if on_connect:
            self.on_connect[name] = on_connect
        if on_disconnect:
            self.on_disconnect[name] = on_disconnect
        if on_ping:
            self.on_ping[name] = on_ping

    def del_handlers(self, name: str) -> None:
        """Remove connection lifecycle callbacks by name."""
        del self.on_connect[name]
        del self.on_disconnect[name]
        del self.on_ping[name]

    def add_listener(
        self,
        name: str,
        door_status_update: Callable[[str], None] | None = None,
        settings_update: Callable[[dict], None] | None = None,
        sensor_update: dict[str, Callable[[str, bool | None], None]] | None = None,
        notifications_update: dict[str, Callable[[bool], None]] | None = None,
        stats_update: dict[str, Callable[[int], None]] | None = None,
        hw_info_update: Callable[[dict], None] | None = None,
        battery_update: Callable[[dict], None] | None = None,
        timezone_update: Callable[[str], None] | None = None,
        hold_time_update: Callable[[int], None] | None = None,
        sensor_trigger_voltage_update: Callable[[int], None] | None = None,
        sleep_sensor_trigger_voltage_update: Callable[[int], None] | None = None,
        remote_id_update: Callable[[bool], None] | None = None,
        remote_key_update: Callable[[bool], None] | None = None,
        reset_reason_update: Callable[[str], None] | None = None,
        schedule_update: Callable[[dict], None] | None = None,
        schedule_delete: Callable[[int], None] | None = None,
        notification_event: Callable[[str, str | None], None] | None = None,
    ) -> None:
        """Register callbacks for device state updates.

        Args:
            name: Unique identifier for this listener
            door_status_update: Called with door status string
            settings_update: Called with full settings dict
            sensor_update: Dict mapping sensor field (or "*" for all) to a
                callback invoked as ``callback(field, value)`` where value
                is the coerced boolean (or None if unrecognized)
            notifications_update: Dict mapping notification field to callback
            stats_update: Dict mapping stats field to callback
            hw_info_update: Called with hardware info dict
            battery_update: Called with battery status dict
            timezone_update: Called with timezone string
            hold_time_update: Called with hold time in seconds
            sensor_trigger_voltage_update: Called with trigger voltage
            sleep_sensor_trigger_voltage_update: Called with sleep trigger voltage
            remote_id_update: Called with True if device has remote ID
            remote_key_update: Called with True if device has remote key
            reset_reason_update: Called with reset reason string
            schedule_update: Called with schedule dict when schedule is added/updated
            schedule_delete: Called with schedule index when schedule is deleted
            notification_event: Called as ``callback(event, state)`` when the
                device announces an event; event is one of
                ``NOTIFY_SENSOR_INDOOR``, ``NOTIFY_SENSOR_OUTDOOR`` or
                ``NOTIFY_LOW_BATTERY`` and state is the reported
                ``sensorState`` ("on"/"off") or None if not provided
        """
        if door_status_update:
            self.door_status_listeners[name] = door_status_update
        if settings_update:
            self.settings_listeners[name] = settings_update
        if sensor_update:
            if "*" in sensor_update:
                self.sensor_listeners[FIELD_POWER][name] = sensor_update["*"]
                self.sensor_listeners[FIELD_INSIDE][name] = sensor_update["*"]
                self.sensor_listeners[FIELD_OUTSIDE][name] = sensor_update["*"]
                self.sensor_listeners[FIELD_AUTO][name] = sensor_update["*"]
                self.sensor_listeners[FIELD_OUTSIDE_SENSOR_SAFETY_LOCK][name] = sensor_update["*"]
                self.sensor_listeners[FIELD_CMD_LOCKOUT][name] = sensor_update["*"]
                self.sensor_listeners[FIELD_AUTORETRACT][name] = sensor_update["*"]
            else:
                if FIELD_POWER in sensor_update:
                    self.sensor_listeners[FIELD_POWER][name] = sensor_update[FIELD_POWER]
                if FIELD_INSIDE in sensor_update:
                    self.sensor_listeners[FIELD_INSIDE][name] = sensor_update[FIELD_INSIDE]
                if FIELD_OUTSIDE in sensor_update:
                    self.sensor_listeners[FIELD_OUTSIDE][name] = sensor_update[FIELD_OUTSIDE]
                if FIELD_AUTO in sensor_update:
                    self.sensor_listeners[FIELD_AUTO][name] = sensor_update[FIELD_AUTO]
                if FIELD_OUTSIDE_SENSOR_SAFETY_LOCK in sensor_update:
                    self.sensor_listeners[FIELD_OUTSIDE_SENSOR_SAFETY_LOCK][name] = sensor_update[
                        FIELD_OUTSIDE_SENSOR_SAFETY_LOCK
                    ]
                if FIELD_CMD_LOCKOUT in sensor_update:
                    self.sensor_listeners[FIELD_CMD_LOCKOUT][name] = sensor_update[
                        FIELD_CMD_LOCKOUT
                    ]
                if FIELD_AUTORETRACT in sensor_update:
                    self.sensor_listeners[FIELD_AUTORETRACT][name] = sensor_update[
                        FIELD_AUTORETRACT
                    ]
        if notifications_update:
            if "*" in notifications_update:
                self.notifications_listeners[FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS][name] = (
                    notifications_update["*"]
                )
                self.notifications_listeners[FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS][name] = (
                    notifications_update["*"]
                )
                self.notifications_listeners[FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS][name] = (
                    notifications_update["*"]
                )
                self.notifications_listeners[FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS][name] = (
                    notifications_update["*"]
                )
                self.notifications_listeners[FIELD_LOW_BATTERY_NOTIFICATIONS][name] = (
                    notifications_update["*"]
                )
            else:
                if FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS in notifications_update:
                    self.notifications_listeners[FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS][name] = (
                        notifications_update[FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS]
                    )
                if FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS in notifications_update:
                    self.notifications_listeners[FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS][name] = (
                        notifications_update[FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS]
                    )
                if FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS in notifications_update:
                    self.notifications_listeners[FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS][name] = (
                        notifications_update[FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS]
                    )
                if FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS in notifications_update:
                    self.notifications_listeners[FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS][name] = (
                        notifications_update[FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS]
                    )
                if FIELD_LOW_BATTERY_NOTIFICATIONS in notifications_update:
                    self.notifications_listeners[FIELD_LOW_BATTERY_NOTIFICATIONS][name] = (
                        notifications_update[FIELD_LOW_BATTERY_NOTIFICATIONS]
                    )
        if stats_update:
            if "*" in stats_update:
                self.stats_listeners[FIELD_TOTAL_OPEN_CYCLES][name] = stats_update["*"]
                self.stats_listeners[FIELD_TOTAL_AUTO_RETRACTS][name] = stats_update["*"]
            else:
                if FIELD_TOTAL_OPEN_CYCLES in stats_update:
                    self.stats_listeners[FIELD_TOTAL_OPEN_CYCLES][name] = stats_update[
                        FIELD_TOTAL_OPEN_CYCLES
                    ]
                if FIELD_TOTAL_AUTO_RETRACTS in stats_update:
                    self.stats_listeners[FIELD_TOTAL_AUTO_RETRACTS][name] = stats_update[
                        FIELD_TOTAL_AUTO_RETRACTS
                    ]
        if hw_info_update:
            self.hw_info_listeners[name] = hw_info_update
        if battery_update:
            self.battery_listeners[name] = battery_update
        if timezone_update:
            self.timezone_listeners[name] = timezone_update
        if hold_time_update:
            self.hold_time_listeners[name] = hold_time_update
        if sensor_trigger_voltage_update:
            self.sensor_trigger_voltage_listeners[name] = sensor_trigger_voltage_update
        if sleep_sensor_trigger_voltage_update:
            self.sleep_sensor_trigger_voltage_listeners[name] = sleep_sensor_trigger_voltage_update
        if remote_id_update:
            self.remote_id_listeners[name] = remote_id_update
        if remote_key_update:
            self.remote_key_listeners[name] = remote_key_update
        if reset_reason_update:
            self.reset_reason_listeners[name] = reset_reason_update
        if schedule_update:
            self.schedule_update_listeners[name] = schedule_update
        if schedule_delete:
            self.schedule_delete_listeners[name] = schedule_delete
        if notification_event:
            self.notification_event_listeners[name] = notification_event

    def del_listener(self, name: str) -> None:
        """Remove all listeners registered under a name.

        Safely removes entries - does not raise KeyError if listener wasn't added.
        """
        self.door_status_listeners.pop(name, None)
        self.settings_listeners.pop(name, None)
        self.sensor_listeners[FIELD_POWER].pop(name, None)
        self.sensor_listeners[FIELD_INSIDE].pop(name, None)
        self.sensor_listeners[FIELD_OUTSIDE].pop(name, None)
        self.sensor_listeners[FIELD_AUTO].pop(name, None)
        self.sensor_listeners[FIELD_OUTSIDE_SENSOR_SAFETY_LOCK].pop(name, None)
        self.sensor_listeners[FIELD_CMD_LOCKOUT].pop(name, None)
        self.sensor_listeners[FIELD_AUTORETRACT].pop(name, None)
        self.notifications_listeners[FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS].pop(name, None)
        self.notifications_listeners[FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS].pop(name, None)
        self.notifications_listeners[FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS].pop(name, None)
        self.notifications_listeners[FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS].pop(name, None)
        self.notifications_listeners[FIELD_LOW_BATTERY_NOTIFICATIONS].pop(name, None)
        self.stats_listeners[FIELD_TOTAL_OPEN_CYCLES].pop(name, None)
        self.stats_listeners[FIELD_TOTAL_AUTO_RETRACTS].pop(name, None)
        self.hw_info_listeners.pop(name, None)
        self.battery_listeners.pop(name, None)
        self.timezone_listeners.pop(name, None)
        self.hold_time_listeners.pop(name, None)
        self.sensor_trigger_voltage_listeners.pop(name, None)
        self.sleep_sensor_trigger_voltage_listeners.pop(name, None)
        self.remote_id_listeners.pop(name, None)
        self.remote_key_listeners.pop(name, None)
        self.reset_reason_listeners.pop(name, None)
        self.schedule_update_listeners.pop(name, None)
        self.schedule_delete_listeners.pop(name, None)
        self.notification_event_listeners.pop(name, None)

    # -------------------------------------------------------------------------
    # Listener/future dispatch helpers
    # -------------------------------------------------------------------------

    def _notify_listeners(self, listeners: dict[str, Callable], *args: Any) -> None:
        """Invoke listener callbacks, isolating each in its own try/except.

        One misbehaving listener must never prevent the remaining
        listeners from being notified (decision D3).
        """
        for name, callback in list(listeners.items()):
            try:
                callback(*args)
            except Exception:
                _LOGGER.exception("Listener %r raised while handling an update", name)

    @staticmethod
    def _resolve_future(future: asyncio.Future | None, result: Any) -> None:
        """Resolve a response future with a result, if it is still pending."""
        if future is not None and not future.done():
            future.set_result(result)

    # -------------------------------------------------------------------------
    # Response Handlers - registered via decorator pattern
    # -------------------------------------------------------------------------

    @ResponseHandlerRegistry.handler(CMD_GET_DOOR_STATUS, DOOR_STATUS)
    def _handle_door_status(self, msg: dict, future) -> None:
        """Handle door status responses."""
        status = msg[FIELD_DOOR_STATUS]
        self._notify_listeners(self.door_status_listeners, status)
        self._resolve_future(future, status)

    @ResponseHandlerRegistry.handler(CMD_GET_SETTINGS)
    def _handle_get_settings(self, msg: dict, future) -> None:
        """Handle settings response - extracts many sub-values."""
        settings = msg[FIELD_SETTINGS]
        self._notify_listeners(self.settings_listeners, settings)

        # Notify sensor listeners for fields in settings
        sensor_fields = [
            (FIELD_POWER, self.sensor_listeners[FIELD_POWER]),
            (FIELD_INSIDE, self.sensor_listeners[FIELD_INSIDE]),
            (FIELD_OUTSIDE, self.sensor_listeners[FIELD_OUTSIDE]),
            (FIELD_AUTO, self.sensor_listeners[FIELD_AUTO]),
            (
                FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
                self.sensor_listeners[FIELD_OUTSIDE_SENSOR_SAFETY_LOCK],
            ),
            (FIELD_CMD_LOCKOUT, self.sensor_listeners[FIELD_CMD_LOCKOUT]),
            (FIELD_AUTORETRACT, self.sensor_listeners[FIELD_AUTORETRACT]),
        ]
        for field_name, listeners in sensor_fields:
            if listeners and field_name in settings:
                val = make_bool(settings[field_name])
                self._notify_listeners(listeners, field_name, val)

        # Notify other listeners; these fields may be absent on some
        # firmware variants, so guard each one (never assume presence).
        if self.timezone_listeners and FIELD_TZ in settings:
            self._notify_listeners(self.timezone_listeners, settings[FIELD_TZ])
        if self.hold_time_listeners and FIELD_HOLD_OPEN_TIME in settings:
            self._notify_listeners(self.hold_time_listeners, settings[FIELD_HOLD_OPEN_TIME])
        if self.sensor_trigger_voltage_listeners and FIELD_SENSOR_TRIGGER_VOLTAGE in settings:
            self._notify_listeners(
                self.sensor_trigger_voltage_listeners, settings[FIELD_SENSOR_TRIGGER_VOLTAGE]
            )
        if (
            self.sleep_sensor_trigger_voltage_listeners
            and FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE in settings
        ):
            self._notify_listeners(
                self.sleep_sensor_trigger_voltage_listeners,
                settings[FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE],
            )

        self._resolve_future(future, settings)

    @ResponseHandlerRegistry.handler(CMD_GET_NOTIFICATIONS, CMD_SET_NOTIFICATIONS)
    def _handle_notifications(self, msg: dict, future) -> None:
        """Handle notifications response."""
        notifications = msg[FIELD_NOTIFICATIONS]
        notification_fields = [
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
            FIELD_LOW_BATTERY_NOTIFICATIONS,
        ]
        for field_name in notification_fields:
            if self.notifications_listeners[field_name] and field_name in notifications:
                val = make_bool(notifications[field_name])
                self._notify_listeners(self.notifications_listeners[field_name], val)
        self._resolve_future(future, notifications)

    @ResponseHandlerRegistry.handler(CMD_GET_DOOR_OPEN_STATS)
    def _handle_door_open_stats(self, msg: dict, future) -> None:
        """Handle door open stats response."""
        if self.stats_listeners[FIELD_TOTAL_OPEN_CYCLES] and FIELD_TOTAL_OPEN_CYCLES in msg:
            self._notify_listeners(
                self.stats_listeners[FIELD_TOTAL_OPEN_CYCLES], msg[FIELD_TOTAL_OPEN_CYCLES]
            )
        if self.stats_listeners[FIELD_TOTAL_AUTO_RETRACTS] and FIELD_TOTAL_AUTO_RETRACTS in msg:
            self._notify_listeners(
                self.stats_listeners[FIELD_TOTAL_AUTO_RETRACTS], msg[FIELD_TOTAL_AUTO_RETRACTS]
            )
        self._resolve_future(
            future,
            {
                FIELD_TOTAL_OPEN_CYCLES: msg.get(FIELD_TOTAL_OPEN_CYCLES),
                FIELD_TOTAL_AUTO_RETRACTS: msg.get(FIELD_TOTAL_AUTO_RETRACTS),
            },
        )

    @ResponseHandlerRegistry.handler(
        CMD_GET_SENSORS,
        CMD_ENABLE_INSIDE,
        CMD_DISABLE_INSIDE,
        CMD_ENABLE_OUTSIDE,
        CMD_DISABLE_OUTSIDE,
    )
    def _handle_sensors(self, msg: dict, future) -> None:
        """Handle sensor enable/disable responses."""
        fr = {}
        if FIELD_INSIDE in msg:
            val = make_bool(msg[FIELD_INSIDE])
            fr[FIELD_INSIDE] = val
            self._notify_listeners(self.sensor_listeners[FIELD_INSIDE], FIELD_INSIDE, val)
        if FIELD_OUTSIDE in msg:
            val = make_bool(msg[FIELD_OUTSIDE])
            fr[FIELD_OUTSIDE] = val
            self._notify_listeners(self.sensor_listeners[FIELD_OUTSIDE], FIELD_OUTSIDE, val)
        self._resolve_future(future, fr)

    @ResponseHandlerRegistry.handler(CMD_GET_POWER, CMD_POWER_ON, CMD_POWER_OFF)
    def _handle_power(self, msg: dict, future) -> None:
        """Handle power state responses."""
        if FIELD_POWER in msg:
            val = make_bool(msg[FIELD_POWER])
            self._notify_listeners(self.sensor_listeners[FIELD_POWER], FIELD_POWER, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_GET_AUTO, CMD_ENABLE_AUTO, CMD_DISABLE_AUTO)
    def _handle_auto(self, msg: dict, future) -> None:
        """Handle timers/auto enabled responses."""
        if FIELD_AUTO in msg:
            val = make_bool(msg[FIELD_AUTO])
            self._notify_listeners(self.sensor_listeners[FIELD_AUTO], FIELD_AUTO, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(
        CMD_GET_OUTSIDE_SENSOR_SAFETY_LOCK,
        CMD_ENABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
        CMD_DISABLE_OUTSIDE_SENSOR_SAFETY_LOCK,
    )
    def _handle_safety_lock(self, msg: dict, future) -> None:
        """Handle outside sensor safety lock responses."""
        if FIELD_SETTINGS in msg and FIELD_OUTSIDE_SENSOR_SAFETY_LOCK in msg[FIELD_SETTINGS]:
            val = make_bool(msg[FIELD_SETTINGS][FIELD_OUTSIDE_SENSOR_SAFETY_LOCK])
            self._notify_listeners(
                self.sensor_listeners[FIELD_OUTSIDE_SENSOR_SAFETY_LOCK],
                FIELD_OUTSIDE_SENSOR_SAFETY_LOCK,
                val,
            )
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(
        CMD_GET_CMD_LOCKOUT, CMD_ENABLE_CMD_LOCKOUT, CMD_DISABLE_CMD_LOCKOUT
    )
    def _handle_cmd_lockout(self, msg: dict, future) -> None:
        """Handle command lockout responses."""
        if FIELD_SETTINGS in msg and FIELD_CMD_LOCKOUT in msg[FIELD_SETTINGS]:
            val = make_bool(msg[FIELD_SETTINGS][FIELD_CMD_LOCKOUT])
            self._notify_listeners(self.sensor_listeners[FIELD_CMD_LOCKOUT], FIELD_CMD_LOCKOUT, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(
        CMD_GET_AUTORETRACT, CMD_ENABLE_AUTORETRACT, CMD_DISABLE_AUTORETRACT
    )
    def _handle_autoretract(self, msg: dict, future) -> None:
        """Handle autoretract responses."""
        if FIELD_SETTINGS in msg and FIELD_AUTORETRACT in msg[FIELD_SETTINGS]:
            val = make_bool(msg[FIELD_SETTINGS][FIELD_AUTORETRACT])
            self._notify_listeners(self.sensor_listeners[FIELD_AUTORETRACT], FIELD_AUTORETRACT, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_GET_HW_INFO)
    def _handle_hw_info(self, msg: dict, future) -> None:
        """Handle hardware info response."""
        if FIELD_FWINFO in msg:
            self._notify_listeners(self.hw_info_listeners, msg[FIELD_FWINFO])
            self._resolve_future(future, msg[FIELD_FWINFO])

    @ResponseHandlerRegistry.handler(CMD_GET_DOOR_BATTERY)
    def _handle_battery(self, msg: dict, future) -> None:
        """Handle battery status response."""
        data = {
            FIELD_BATTERY_PERCENT: msg.get(FIELD_BATTERY_PERCENT),
            FIELD_BATTERY_PRESENT: make_bool(msg.get(FIELD_BATTERY_PRESENT)),
            FIELD_AC_PRESENT: make_bool(msg.get(FIELD_AC_PRESENT)),
        }
        self._notify_listeners(self.battery_listeners, data)
        self._resolve_future(future, data)

    @ResponseHandlerRegistry.handler(CMD_GET_TIMEZONE, CMD_SET_TIMEZONE)
    def _handle_timezone(self, msg: dict, future) -> None:
        """Handle timezone responses."""
        if FIELD_TZ in msg:
            val = msg[FIELD_TZ]
            self._notify_listeners(self.timezone_listeners, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_GET_HOLD_TIME, CMD_SET_HOLD_TIME)
    def _handle_hold_time(self, msg: dict, future) -> None:
        """Handle hold time responses."""
        if FIELD_HOLD_TIME in msg:
            val = msg[FIELD_HOLD_TIME]
            self._notify_listeners(self.hold_time_listeners, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_GET_SENSOR_TRIGGER_VOLTAGE, CMD_SET_SENSOR_TRIGGER_VOLTAGE)
    def _handle_sensor_trigger_voltage(self, msg: dict, future) -> None:
        """Handle sensor trigger voltage responses."""
        if FIELD_SENSOR_TRIGGER_VOLTAGE in msg:
            val = msg[FIELD_SENSOR_TRIGGER_VOLTAGE]
            self._notify_listeners(self.sensor_trigger_voltage_listeners, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(
        CMD_GET_SLEEP_SENSOR_TRIGGER_VOLTAGE, CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE
    )
    def _handle_sleep_sensor_trigger_voltage(self, msg: dict, future) -> None:
        """Handle sleep sensor trigger voltage responses."""
        if FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE in msg:
            val = msg[FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE]
            self._notify_listeners(self.sleep_sensor_trigger_voltage_listeners, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_GET_SCHEDULE_LIST)
    def _handle_schedule_list(self, msg: dict, future) -> None:
        """Handle schedule list response."""
        self._resolve_future(future, msg[FIELD_SCHEDULES])

    @ResponseHandlerRegistry.handler(CMD_DELETE_SCHEDULE)
    def _handle_delete_schedule(self, msg: dict, future) -> None:
        """Handle delete schedule response."""
        if FIELD_INDEX in msg:
            index = msg[FIELD_INDEX]
            self._notify_listeners(self.schedule_delete_listeners, index)
            self._resolve_future(future, index)

    @ResponseHandlerRegistry.handler(CMD_GET_SCHEDULE, CMD_SET_SCHEDULE)
    def _handle_schedule(self, msg: dict, future) -> None:
        """Handle get/set schedule response."""
        if FIELD_SCHEDULE in msg:
            schedule = msg[FIELD_SCHEDULE]
            self._notify_listeners(self.schedule_update_listeners, schedule)
            self._resolve_future(future, schedule)

    @ResponseHandlerRegistry.handler(CMD_HAS_REMOTE_ID)
    def _handle_remote_id(self, msg: dict, future) -> None:
        """Handle HAS_REMOTE_ID response."""
        if FIELD_HAS_REMOTE_ID in msg:
            val = make_bool(msg[FIELD_HAS_REMOTE_ID])
            self._notify_listeners(self.remote_id_listeners, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_HAS_REMOTE_KEY)
    def _handle_remote_key(self, msg: dict, future) -> None:
        """Handle HAS_REMOTE_KEY response."""
        if FIELD_HAS_REMOTE_KEY in msg:
            val = make_bool(msg[FIELD_HAS_REMOTE_KEY])
            self._notify_listeners(self.remote_key_listeners, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_CHECK_RESET_REASON)
    def _handle_reset_reason(self, msg: dict, future) -> None:
        """Handle CHECK_RESET_REASON response."""
        if FIELD_RESET_REASON in msg:
            val = msg[FIELD_RESET_REASON]
            self._notify_listeners(self.reset_reason_listeners, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(PONG)
    def _handle_pong(self, msg: dict, future) -> None:
        """Handle PONG keepalive response."""
        if self._last_ping is not None and msg.get(PONG) == self._last_ping:
            # Latency is measured on the monotonic clock; the wire token
            # remains wall-clock milliseconds for device compatibility.
            diff = round((time.monotonic() - self._last_ping_time) * 1000)
            self._notify_listeners(self.on_ping, diff)
            self._failed_pings = 0
            self._last_ping = None

    @ResponseHandlerRegistry.handler(
        NOTIFY_SENSOR_INDOOR, NOTIFY_SENSOR_OUTDOOR, NOTIFY_LOW_BATTERY
    )
    def _handle_notification_event_cmd(self, msg: dict, future) -> None:
        """Handle CMD-style notification event envelopes.

        The documented device format is the bare envelope (see
        :meth:`_dispatch_notification_event`), but CMD-style variants
        (``{"CMD": "SENSOR_INDOOR", "success": "true", ...}``) are
        tolerated and dispatched to the same listeners (decision D2).
        """
        self._notify_listeners(
            self.notification_event_listeners, msg.get(FIELD_CMD), msg.get(FIELD_SENSOR_STATE)
        )

    def _dispatch_notification_event(self, msg: dict) -> bool:
        """Dispatch a bare notification event envelope, if this is one.

        The device announces sensor and battery events outside the normal
        command-response envelope (see docs/protocol.md "Notification
        Events"), e.g. ``{"SENSOR_INDOOR": "", "sensorState": "on"}`` or
        ``{"LOW_BATTERY": ""}`` — no ``CMD``, ``success`` or ``msgID``.

        Args:
            msg: The decoded message.

        Returns:
            True if the message was a notification event (dispatched to
            ``notification_event`` listeners), False otherwise.
        """
        for event in (NOTIFY_SENSOR_INDOOR, NOTIFY_SENSOR_OUTDOOR, NOTIFY_LOW_BATTERY):
            if event in msg:
                state = msg.get(FIELD_SENSOR_STATE)
                _LOGGER.debug("Notification event: %s (state=%s)", event, state)
                self._notify_listeners(self.notification_event_listeners, event, state)
                return True
        return False

    def start(self) -> None:
        """Start the client and initiate connection.

        If an event loop was not provided in the constructor,
        this will block until stop() is called.
        """
        self._shutdown = False
        self.ensure_future(self.connect())

        if self._ownLoop:
            _LOGGER.info("Starting up our own event loop.")
            self._eventLoop.run_forever()
            self._eventLoop.close()
            _LOGGER.info("Connection shut down.")

    def stop(self) -> None:
        """Stop the client and close connection."""
        self._shutdown = True

        _LOGGER.info("Shutting down Power Pet Door client connection...")
        self._eventLoop.call_soon_threadsafe(self.disconnect)
        if self._ownLoop:
            self._eventLoop.call_soon_threadsafe(self._eventLoop.stop)

    async def connect(self) -> None:
        """Establish connection to the device."""
        _LOGGER.info(
            str.format(
                "Started to connect to Power Pet Door... at {0}:{1}", self.cfg_host, self.cfg_port
            )
        )
        try:
            async with async_timeout.timeout(self.cfg_timeout):
                coro = self._eventLoop.create_connection(lambda: self, self.cfg_host, self.cfg_port)
                await coro
        except:
            self.handle_connect_failure()

    def connection_made(self, transport) -> None:
        """asyncio callback for a successful connection."""
        _LOGGER.info("Connection Successful!")
        self._transport = transport
        self._can_dequeue = True

        if self.cfg_keepalive:
            self._keepalive = self.ensure_future(self.keepalive())

        # Caller code
        for callback in self.on_connect.values():
            self.ensure_future(callback())

    def connection_lost(self, exc) -> None:
        """asyncio callback for connection lost."""
        self.disconnect()
        if not self._shutdown:
            _LOGGER.error("The server closed the connection. Reconnecting...")
            self.ensure_future(self.reconnect(self.cfg_reconnect))

    async def reconnect(self, delay) -> None:
        """Reconnect after a delay."""
        await asyncio.sleep(delay)
        await self.connect()

    def disconnect(self) -> None:
        """Close connection and cleanup."""
        _LOGGER.debug("Closing connection with server...")
        self._can_dequeue = False
        self._last_ping = None
        self._last_command = None
        self._last_send = 0
        self._failed_msg = 0
        self._failed_pings = 0
        self._buffer = ""
        self._queue = queue.PriorityQueue()
        self._msg_sequence = 0  # Reset sequence counter

        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        if self._check_receipt:
            self._check_receipt.cancel()
            self._check_receipt = None
        if self._transport:
            self._transport.close()
            self._transport = None

        for future in self._outstanding.values():
            future.cancel("Connection Terminated")
        self._outstanding = {}

        # Caller code
        for callback in self.on_disconnect.values():
            self.ensure_future(callback())

    def handle_connect_failure(self) -> None:
        """Handler for if we fail to connect to the power pet door."""
        if not self._shutdown:
            _LOGGER.error("Unable to connect to power pet door. Reconnecting...")
            self.disconnect()
            self.ensure_future(self.reconnect(self.cfg_reconnect))

    async def keepalive(self) -> None:
        _keepalive = self._keepalive
        await asyncio.sleep(self.cfg_keepalive)
        if _keepalive and not _keepalive.cancelled():
            if self._last_ping is not None:
                self._failed_pings += 1
                if self._failed_pings < MAX_FAILED_PINGS:
                    _LOGGER.warning(
                        f"Last PING not responded to {self._failed_pings} of {MAX_FAILED_PINGS}..."
                    )
                else:
                    _LOGGER.error(f"Last PING not responded to {self._failed_pings} times.")
                    self.disconnect()
                    return

            # The wire token stays wall-clock milliseconds (device
            # compatibility); latency is measured against the monotonic
            # clock so NTP steps cannot skew it (L11).
            self._last_ping = str(round(time.time() * 1000))
            self._last_ping_time = time.monotonic()
            self.send_message(PING, self._last_ping)

    async def check_receipt(self, rawdata) -> None:
        _check_receipt = self._check_receipt
        await asyncio.sleep(self.cfg_timeout)
        if _check_receipt and not _check_receipt.cancelled():
            self._failed_msg += 1
            if self._failed_msg < MAX_FAILED_MSG:
                _LOGGER.warning(
                    f"Did not receive a response to a {self._last_command} message in more than {self.cfg_timeout} seconds, retrying."
                )
            else:
                _LOGGER.error(
                    f"Did not receive a response to a {self._last_command} message in more than {self.cfg_timeout} seconds {self._failed_msg} times, dropped."
                )
                self._failed_msg = 0
        else:
            self._failed_msg = 0

        self._check_receipt = None
        if self._failed_msg == 0:
            await self.dequeue_data()
        else:
            await self._send_data(rawdata)

    def enqueue_data(self, data, priority: int = PRIORITY_LOW) -> None:
        """Enqueue a message with the given priority.

        Lower priority values are processed first.
        """
        msg = PrioritizedMessage(priority=priority, sequence=self._msg_sequence, data=data)
        self._msg_sequence += 1
        self._queue.put(msg)
        if self._transport and self._can_dequeue:
            self._can_dequeue = False
            self.ensure_future(self.dequeue_data())

    async def _send_data(self, rawdata) -> None:
        if not self._transport:
            _LOGGER.warning("Attempted to write to the stream without a connection active")
            return

        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        try:
            diff = time.monotonic() - self._last_send
            if diff < MINIMUM_TIME_BETWEEN_MSGS:
                await asyncio.sleep(MINIMUM_TIME_BETWEEN_MSGS - diff)
            _LOGGER.debug("TX > %s", rawdata)
            self._transport.write(rawdata)
            self._last_send = time.monotonic()

            if self.cfg_keepalive:
                self._keepalive = self.ensure_future(self.keepalive())

            if self._last_command:
                self._check_receipt = self.ensure_future(self.check_receipt(rawdata))
            else:
                await self.dequeue_data()

        except RuntimeError as err:
            _LOGGER.error("Failed to write to the stream. (%s)", err)
            self.disconnect()

    async def dequeue_data(self) -> None:
        if not self._transport:
            _LOGGER.warning("Attempted to write to the stream without a connection active")
            return

        if self._check_receipt:
            _LOGGER.warning("Attempted to send data while another message is still outstanding")
            return

        """Raw data send- just make sure it's encoded properly and logged."""
        if self._queue.empty():
            self._can_dequeue = True
            return

        try:
            prioritized_msg = self._queue.get_nowait()
            data = prioritized_msg.data  # Extract actual message data from PrioritizedMessage
            if COMMAND in data:
                self._last_command = data[COMMAND]
            elif CONFIG in data:
                self._last_command = data[CONFIG]
            elif PING in data:
                self._last_command = PONG
            else:
                _LOGGER.warning("Sending unknown command type")
                self._last_command = None

            self._failed_msg = 0
            rawdata = json.dumps(data).encode("ascii")
            await self._send_data(rawdata)

        except queue.Empty:
            _LOGGER.warning("Attempted to dequeue from an empty queue")

    def data_received(self, rawdata) -> None:
        """asyncio callback for any data received from the power pet door.

        All received bytes are untrusted. The stream is framed by the
        shared scanner (:mod:`powerpetdoor.framing`): garbage is discarded
        with a warning, malformed JSON frames are logged and skipped, and
        exceeding the un-parsed buffer cap drops the connection. This
        callback never raises on arbitrary input.
        """
        if not rawdata:
            return

        try:
            data = rawdata.decode("ascii")
        except UnicodeDecodeError as err:
            _LOGGER.error("Received non-ASCII data from device (%s); discarding chunk", err)
            return

        _LOGGER.debug("RX < %s", data)
        frames, self._buffer, diag = extract_frames(self._buffer + data)

        for frame in frames:
            try:
                msg = json.loads(frame)
            except json.JSONDecodeError as err:
                _LOGGER.error("Failed to decode JSON frame (%s): %s", err, frame)
                continue
            self.ensure_future(self.process_message(msg))

        if diag.overflow:
            _LOGGER.error(
                "Receive buffer exceeded %d bytes without a complete message; disconnecting",
                MAX_BUFFER_SIZE,
            )
            self.disconnect()

    async def process_message(self, msg) -> None:
        """Process an incoming message from the device.

        Uses the ResponseHandlerRegistry to dispatch to the appropriate
        handler. All network data is untrusted: envelope fields are read
        defensively, handler dispatch is isolated so a malformed payload
        cannot kill the receive path, and a future paired with the message
        is always completed (result or :class:`CommandError`) rather than
        left hanging.
        """
        if not isinstance(msg, dict):
            _LOGGER.warning("Ignoring non-object message from device: %r", msg)
            return

        cmd = msg.get(FIELD_CMD)

        # Device-initiated notification events use a bare envelope with no
        # CMD/success fields (docs/protocol.md "Notification Events").
        if cmd is None and self._dispatch_notification_event(msg):
            return

        success = msg.get(FIELD_SUCCESS)
        if cmd is None and success is None:
            _LOGGER.warning("Ignoring malformed message from device: %s", json.dumps(msg))
            return

        future = None
        reply_msg_id = msg.get(FIELD_MSG_ID_RESPONSE)
        if reply_msg_id is not None:
            self.replyMsgId = reply_msg_id
            outstanding = self._outstanding.get(reply_msg_id)
            if outstanding is not None and not outstanding.done():
                future = outstanding

        # Acknowledge the in-flight command so the retry timer stops, but
        # defer dequeuing the next message until after the handler has run.
        # Dequeuing awaits (rate-limit sleep), and completing the future
        # only after that await races with caller-side wait_for timeouts.
        dequeue = False
        if cmd is not None and cmd == self._last_command:
            if self._check_receipt:
                self._check_receipt.cancel()
                self._check_receipt = None
                dequeue = True
            elif self._can_dequeue:
                self._can_dequeue = False
                dequeue = True

        try:
            if success == SUCCESS_TRUE:
                # Look up handler in registry
                handler = ResponseHandlerRegistry.get(cmd)
                if handler:
                    try:
                        handler(self, msg, future)
                    except Exception:
                        _LOGGER.exception("Error handling %s response: %s", cmd, json.dumps(msg))
                        if future is not None and not future.done():
                            future.set_exception(CommandError(cmd, "Malformed response"))

                # Cancel unfulfilled futures
                if future is not None and not future.done():
                    future.cancel()
            else:
                reason = msg.get(FIELD_REASON)
                _LOGGER.warning("Error reported by device: %s", json.dumps(msg))
                if future is not None and not future.done():
                    future.set_exception(CommandError(cmd, reason))
        finally:
            if dequeue:
                await self.dequeue_data()

    def send_message(self, type: str, arg: str, notify: bool = False, **kwargs) -> None:
        """Send a message to the Power Pet Door.

        Args:
            type: Message type (COMMAND, CONFIG, or PING)
            arg: Command or config name
            notify: If True, returns a Future that resolves with the response
            **kwargs: Additional message parameters

        Returns:
            Future if notify=True, otherwise None
        """
        msg_id = self.msgId
        rv = None
        if notify:
            rv = self._eventLoop.create_future()
            self._outstanding[msg_id] = rv

            def cleanup(arg: asyncio.Future) -> None:
                del self._outstanding[msg_id]

            rv.add_done_callback(cleanup)

        # Determine priority based on message type and command
        if type == PING:
            priority = PRIORITY_CRITICAL
        else:
            priority = COMMAND_PRIORITIES.get(arg, PRIORITY_LOW)

        self.msgId += 1
        self.enqueue_data(
            {type: arg, FIELD_MSG_ID: msg_id, FIELD_DIRECTION: PHONE_TO_DOOR, **kwargs},
            priority=priority,
        )
        return rv

    @property
    def available(self) -> bool:
        """Whether the client is connected and available."""
        return self._transport and not self._transport.is_closing()

    @property
    def host(self) -> str:
        """The configured host address."""
        return self.cfg_host

    @property
    def port(self) -> int:
        """The configured port number."""
        return self.cfg_port

    @property
    def effective_timeout(self) -> float:
        """Maximum time to wait for a command response including retries.

        This is cfg_timeout * MAX_FAILED_MSG, representing the total time
        the client will try before dropping an unacknowledged message.
        """
        return self.cfg_timeout * MAX_FAILED_MSG
