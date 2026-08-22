# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Power Pet Door client for network communication."""

from __future__ import annotations

import asyncio
import concurrent.futures
import heapq
import inspect
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal, overload

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
from .framing import (
    MAX_BUFFER_SIZE,
    EventThrottle,
    FrameDispatcher,
    FrameScanner,
    find_frame_end,
)
from .sanitize import MAX_LOGGED_LENGTH, sanitize_text

_LOGGER = logging.getLogger(__name__)

#: Maximum retry attempts before dropping a message
MAX_FAILED_MSG = 2
MAX_FAILED_PINGS = 3

#: Cap on the exponential reconnect backoff delay, in seconds (L1)
MAX_RECONNECT_DELAY = 300.0
#: Maximum fraction of the backoff delay added as random jitter (L1)
RECONNECT_JITTER = 0.25


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
    def get(cls, cmd: object) -> Callable | None:
        """Get the handler for a command, or None if not found.

        ``cmd`` comes straight off the wire. Anything that is not a string
        matches no handler - including the JSON containers that are not
        usable dict keys at all, which would otherwise raise ``TypeError:
        unhashable type`` out of ``process_message`` and turn one ~40-byte
        frame into a full traceback in the host application's log (D3).
        A missing CMD (None) is normal: response envelopes may omit it.
        """
        if not isinstance(cmd, str):
            return None
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


def make_bool(v: str | int | bool | None) -> bool | None:
    """Convert various types to boolean.

    Args:
        v: Value to convert (string, int, bool, or None)

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


def _recorded_size(msg, frame_size: int | None) -> int:
    """Magnitude for a per-frame throttle: real wire bytes when known.

    The fallback exists only for a direct ``process_message`` caller (a
    test, a third-party subclass) that has no frame. On the receive path
    ``frame_size`` is always supplied, so the ``json.dumps`` here does not
    run per frame - which was the other half of the defect: it was executed
    on **every** occurrence purely to measure one, and discarded on ~99.9%
    of them (32,768 serializations, ~35 ms, per 64 KiB read of ``{}``;
    ~1.2 us/call in isolation). The byte-total accuracy is the reason for
    the change, not the CPU (round-9 backend F2).
    """
    if frame_size is not None:
        return frame_size
    return len(json.dumps(msg))


class PowerPetDoorClient(asyncio.Protocol):
    """Client for communicating with Power Pet Door devices.

    This client handles the network protocol for Power Pet Door devices,
    including connection management, message queuing with priorities,
    keepalive/ping-pong, and callback-based event notification.

    Thread safety: all methods except ``stop()`` and
    ``run_coroutine_threadsafe()`` must be called from the event loop
    thread. To drive the client from another thread, wrap calls in a
    coroutine and submit it with ``run_coroutine_threadsafe()``.

    Args:
        host: IP address or hostname of the Power Pet Door
        port: TCP port number (typically 3000)
        keepalive: Seconds between keepalive pings (0 to disable)
        timeout: Seconds to wait for responses
        reconnect: Base seconds to wait before reconnecting after a
            disconnect; consecutive failures back off exponentially with
            jitter, capped at MAX_RECONNECT_DELAY
        loop: Optional asyncio event loop. If not provided, the client
            latches onto the running loop at connect()/start() time; the
            blocking start() path creates a private loop only when no
            loop is running.

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
        self._connecting = False
        self._ownLoop = False
        self._eventLoop: asyncio.AbstractEventLoop | None = None
        self._transport = None
        #: Transports connection_made() declined and aborted while this
        #: object was wired into create_connection() directly (see
        #: :meth:`connection_made`). asyncio still delivers their
        #: connection_lost() here, and it must not tear anything down.
        self._declined = 0
        #: Transports adopted through the public :meth:`connection_made`
        #: whose connection_lost() has not been delivered yet. More than
        #: one outstanding means the loss now arriving belongs to a socket
        #: that has already been superseded (L1) - asyncio passes no
        #: transport identity, so this count is the only way to tell.
        self._pending_direct_losses = 0
        self._keepalive: asyncio.Task | None = None
        self._check_receipt: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempts = 0
        self._was_connected = False
        self._last_ping: str | None = None
        self._last_command: str | None = None
        self._can_dequeue = False
        self._last_send = 0.0
        self._last_ping_time = 0.0
        self._failed_msg = 0
        self._failed_pings = 0
        self._inflight_msg_id: int | None = None
        # One scanner per client, carried across data_received() calls: a
        # peer that dribbles the bytes of a never-terminated object must not
        # make us re-scan the retained buffer every time (S1).
        self._scanner = FrameScanner()
        # A peer that speaks non-ASCII gets one notice per connection plus
        # doubling-schedule summaries, not one ERROR per data_received():
        # one byte per TCP segment used to buy x247 write amplification in
        # a third party's log (Security round-5 Finding 1).
        self._non_ascii = EventThrottle(
            _LOGGER,
            logging.ERROR,
            "Received non-ASCII bytes from device; escaped them (affected frames are "
            "dropped) - %d chunks, %d bytes so far on this connection",
        )
        # The per-*frame* twins of the counter above. Round 5 throttled the
        # per-chunk sites, which are limited by the peer's packet rate; these
        # are limited by its byte rate, so a peer can pack 21,845 three-byte
        # `{x}` frames into one 64 KiB write and buy x28 write amplification
        # in a third party's log (round-6 security finding 2).
        self._bad_frames = EventThrottle(
            _LOGGER,
            logging.ERROR,
            "Failed to decode %d JSON frame(s) from device (%d bytes) on this connection",
        )
        self._bad_messages = EventThrottle(
            _LOGGER,
            logging.WARNING,
            "Ignored %d malformed message(s) from device (%d bytes) on this connection",
        )
        # The device's own error envelope is a per-frame site too, and it
        # is reachable without any malformed input: every frame whose
        # `success` is not "true" logged an unthrottled, length-unbounded
        # WARNING (round-7 security L3).
        self._device_errors = EventThrottle(
            _LOGGER,
            logging.WARNING,
            "Device reported %d error response(s) (%d bytes) on this connection",
        )
        # The two per-frame sites the round-6/7 sweeps missed. `msgID` is
        # echoed on every response, so a peer answering `{"CMD":0,"msgID":[]}`
        # in a tight loop bought one uncapped WARNING per frame - measured at
        # 20,032 records for 20,000 frames, x5.27 the wire bytes, and a
        # single 65,638-byte record from one frame just under the framing cap
        # (round-9 security M1). The `fwInfo` site was already capped but
        # never throttled, which is the same class one control short.
        self._bad_msg_ids = EventThrottle(
            _LOGGER,
            logging.WARNING,
            "Ignored %d unusable msgID(s) in device responses (%d bytes) on this connection",
        )
        self._bad_hw_payloads = EventThrottle(
            _LOGGER,
            logging.WARNING,
            f"Device sent %d non-mapping {FIELD_FWINFO} payload(s) (%d bytes) on this connection",
        )
        # One task per framed message, created synchronously per read, was
        # unbounded: one 256 KiB read of `{}` admitted 131,072 live tasks
        # and ~135 MB of heap (round-6 security finding 1).
        self._dispatcher = FrameDispatcher(self._dispatch_frame)
        # Fire-and-forget work is tracked so failures are logged
        # immediately (L3). _tasks is connection-scoped work that
        # disconnect() cancels; _handler_tasks holds async lifecycle
        # handlers, which must survive the teardown that triggered them.
        self._tasks: set[asyncio.Task] = set()
        self._handler_tasks: set[asyncio.Task] = set()
        # Keyed by the msgID echoed back by the device; the client always
        # sends ints, but a response may echo a string (D3 tolerance).
        self._outstanding: dict[int | str, asyncio.Future] = {}
        # Plain heapq: the client is loop-thread-only (see class docstring),
        # so a lock-based queue.PriorityQueue would only imply a thread
        # safety the rest of the class does not provide (L20).
        self._queue: list[PrioritizedMessage] = []
        self._msg_sequence = 0  # Counter for FIFO ordering within same priority

        if loop is not None:
            _LOGGER.info("Latching onto an existing event loop.")
            self._eventLoop = loop

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
        self.notifications_listeners: dict[str, dict[str, Callable[[str, bool | None], None]]] = {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: {},
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: {},
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: {},
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: {},
            FIELD_LOW_BATTERY_NOTIFICATIONS: {},
        }
        self.stats_listeners: dict[str, dict[str, Callable[[str, int], None]]] = {
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

        # Lifecycle handlers may be plain callables or coroutine functions;
        # _dispatch_handler schedules any awaitable result on the loop.
        self.on_connect: dict[str, Callable[[], Awaitable[None] | None]] = {}
        self.on_disconnect: dict[str, Callable[[], Awaitable[None] | None]] = {}
        self.on_ping: dict[str, Callable[[int], None]] = {}

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Resolve the event loop, latching onto the running loop if needed.

        Raises:
            RuntimeError: If no loop was configured and none is running.
        """
        if self._eventLoop is None:
            self._eventLoop = asyncio.get_running_loop()
        return self._eventLoop

    # These functions wrap asyncio but ensure the loop is correct!
    def ensure_future(self, *args: Any, **kwargs: Any):
        return asyncio.ensure_future(*args, loop=self._get_loop(), **kwargs)

    def _track_task(self, coro: Awaitable[Any], *, transient: bool = True) -> asyncio.Task:
        """Schedule fire-and-forget work as a tracked task (L3).

        Mirrors the simulator's pattern: the task is held in a set so an
        escaping exception is reported immediately by a done-callback,
        instead of whenever the garbage collector happens to reap the task.

        Args:
            coro: The coroutine to schedule.
            transient: True for connection-scoped work (message processing,
                queue kicks) which :meth:`disconnect` cancels; False for
                lifecycle handlers, which must still run once the
                connection is gone.
        """
        task: asyncio.Task = self.ensure_future(coro)
        registry = self._tasks if transient else self._handler_tasks
        registry.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Untrack a finished task, logging any exception it escaped with."""
        self._tasks.discard(task)
        self._handler_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Background client task failed", exc_info=task.exception())

    def run_coroutine_threadsafe(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        if self._eventLoop is None:
            raise RuntimeError("run_coroutine_threadsafe() requires the client to be started first")
        return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)

    def add_handlers(
        self,
        name: str,
        on_connect: Callable[[], Awaitable[None] | None] | None = None,
        on_disconnect: Callable[[], Awaitable[None] | None] | None = None,
        on_ping: Callable[[int], None] | None = None,
    ) -> None:
        """Register connection lifecycle callbacks.

        Args:
            name: Unique identifier for this set of handlers
            on_connect: Called when connection is established (sync or async)
            on_disconnect: Called when connection is lost (sync or async)
            on_ping: Called with latency in ms after successful ping
        """
        if on_connect:
            self.on_connect[name] = on_connect
        if on_disconnect:
            self.on_disconnect[name] = on_disconnect
        if on_ping:
            self.on_ping[name] = on_ping

    def del_handlers(self, name: str) -> None:
        """Remove connection lifecycle callbacks by name.

        Safe for names that were never registered, or only partially
        registered - it does not raise KeyError (M6).
        """
        self.on_connect.pop(name, None)
        self.on_disconnect.pop(name, None)
        self.on_ping.pop(name, None)

    def add_listener(
        self,
        name: str,
        door_status_update: Callable[[str], None] | None = None,
        settings_update: Callable[[dict], None] | None = None,
        sensor_update: dict[str, Callable[[str, bool | None], None]] | None = None,
        notifications_update: dict[str, Callable[[str, bool | None], None]] | None = None,
        stats_update: dict[str, Callable[[str, int], None]] | None = None,
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
                is the coerced boolean, or None if the device sent a value
                ``make_bool`` does not recognize. Test for None explicitly:
                ``if value:`` maps "unparseable" onto False, which for a
                safety lock fails in the permissive direction
            notifications_update: Dict mapping notification field (or "*")
                to a callback invoked as ``callback(field, value)``
            stats_update: Dict mapping stats field (or "*") to a callback
                invoked as ``callback(field, value)``
            hw_info_update: Called with hardware info dict
            battery_update: Called with battery status dict
            timezone_update: Called with timezone string
            hold_time_update: Called with hold time in **centiseconds**,
                the raw device value (``PowerPetDoor`` divides by 100 to
                expose seconds)
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

    @staticmethod
    def _payload_mapping(msg: dict, key: str) -> dict | None:
        """Return ``msg[key]`` when the device sent a mapping there.

        Every sub-object in a response is device-supplied, so ``field in
        msg[key]`` raises ``TypeError`` for a scalar and ``msg[key]``
        raises ``KeyError`` when absent - both escape into
        ``_LOGGER.exception``, a full traceback per frame, where the
        graceful "Response missing expected field" path already exists
        (Security round-5 Finding 1).
        """
        value = msg.get(key)
        return value if isinstance(value, dict) else None

    # -------------------------------------------------------------------------
    # Response Handlers - registered via decorator pattern
    # -------------------------------------------------------------------------

    @ResponseHandlerRegistry.handler(CMD_GET_DOOR_STATUS, DOOR_STATUS)
    def _handle_door_status(self, msg: dict, future) -> None:
        """Handle door status responses.

        A legal envelope missing the payload field returns without
        resolving, which process_message turns into the typed
        ``CommandError("Response missing expected field")`` a few lines
        below its handler call. Indexing directly instead raised a
        ``KeyError`` into ``_LOGGER.exception`` - a full traceback per
        39-byte frame, x12.7 write amplification, for a case the graceful
        path already covered (Security round-5 Finding 1).
        """
        if FIELD_DOOR_STATUS not in msg:
            return
        status = msg[FIELD_DOOR_STATUS]
        self._notify_listeners(self.door_status_listeners, status)
        self._resolve_future(future, status)

    @ResponseHandlerRegistry.handler(CMD_GET_SETTINGS)
    def _handle_get_settings(self, msg: dict, future) -> None:
        """Handle settings response - extracts many sub-values.

        Missing or non-mapping payloads take the "Response missing
        expected field" path rather than a traceback (see
        :meth:`_handle_door_status`); ``field in settings`` raises
        ``TypeError`` for a scalar, which is the same amplification.
        """
        settings = self._payload_mapping(msg, FIELD_SETTINGS)
        if settings is None:
            return
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
        """Handle notifications response.

        Same missing/non-mapping guard as :meth:`_handle_get_settings` -
        this is the sibling site with the identical shape.
        """
        notifications = self._payload_mapping(msg, FIELD_NOTIFICATIONS)
        if notifications is None:
            return
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
                self._notify_listeners(self.notifications_listeners[field_name], field_name, val)
        self._resolve_future(future, notifications)

    @ResponseHandlerRegistry.handler(CMD_GET_DOOR_OPEN_STATS)
    def _handle_door_open_stats(self, msg: dict, future) -> None:
        """Handle door open stats response."""
        if self.stats_listeners[FIELD_TOTAL_OPEN_CYCLES] and FIELD_TOTAL_OPEN_CYCLES in msg:
            self._notify_listeners(
                self.stats_listeners[FIELD_TOTAL_OPEN_CYCLES],
                FIELD_TOTAL_OPEN_CYCLES,
                msg[FIELD_TOTAL_OPEN_CYCLES],
            )
        if self.stats_listeners[FIELD_TOTAL_AUTO_RETRACTS] and FIELD_TOTAL_AUTO_RETRACTS in msg:
            self._notify_listeners(
                self.stats_listeners[FIELD_TOTAL_AUTO_RETRACTS],
                FIELD_TOTAL_AUTO_RETRACTS,
                msg[FIELD_TOTAL_AUTO_RETRACTS],
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
        settings = self._payload_mapping(msg, FIELD_SETTINGS)
        if settings is not None and FIELD_OUTSIDE_SENSOR_SAFETY_LOCK in settings:
            val = make_bool(settings[FIELD_OUTSIDE_SENSOR_SAFETY_LOCK])
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
        settings = self._payload_mapping(msg, FIELD_SETTINGS)
        if settings is not None and FIELD_CMD_LOCKOUT in settings:
            val = make_bool(settings[FIELD_CMD_LOCKOUT])
            self._notify_listeners(self.sensor_listeners[FIELD_CMD_LOCKOUT], FIELD_CMD_LOCKOUT, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(
        CMD_GET_AUTORETRACT, CMD_ENABLE_AUTORETRACT, CMD_DISABLE_AUTORETRACT
    )
    def _handle_autoretract(self, msg: dict, future) -> None:
        """Handle autoretract responses."""
        settings = self._payload_mapping(msg, FIELD_SETTINGS)
        if settings is not None and FIELD_AUTORETRACT in settings:
            val = make_bool(settings[FIELD_AUTORETRACT])
            self._notify_listeners(self.sensor_listeners[FIELD_AUTORETRACT], FIELD_AUTORETRACT, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_GET_HW_INFO)
    def _handle_hw_info(self, msg: dict, future) -> None:
        """Handle hardware info response.

        ``fwInfo`` is the sixth response sub-object of the shape
        :meth:`_payload_mapping` exists for, and the only one whose value
        is *cached* rather than merely read - ``door._hw_info`` kept it and
        three documented public properties then treated it as a dict
        (round-6 backend M1).

        Deliberately liberal in what it accepts: whatever the device put
        there still resolves the future, so a caller using
        ``send_message(..., notify=True)`` sees exactly what the device
        said. Only the ``hw_info_update`` listeners are shielded, because
        they are declared ``Callable[[dict], None]`` and
        :meth:`_notify_listeners` would swallow their ``AttributeError``
        with nothing in the log tying it to the frame that caused it.
        """
        if FIELD_FWINFO not in msg:
            return
        fw_info = self._payload_mapping(msg, FIELD_FWINFO)
        if fw_info is not None:
            self._notify_listeners(self.hw_info_listeners, fw_info)
        else:
            # Per-frame and peer-controlled: capped since round 6, throttled
            # since round 9 (security M1) - a cap alone still buys one
            # WARNING per frame.
            rendered = sanitize_text(msg[FIELD_FWINFO], MAX_LOGGED_LENGTH)
            if self._bad_hw_payloads.record(len(rendered)):
                _LOGGER.warning(
                    "Device sent a non-mapping %s payload; not notifying hw_info listeners: %s",
                    FIELD_FWINFO,
                    rendered,
                )
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
        """Handle schedule list response.

        Same missing-field guard as :meth:`_handle_door_status` - the last
        of the four handlers that indexed its payload field directly.
        """
        if FIELD_SCHEDULES not in msg:
            return
        self._resolve_future(future, msg[FIELD_SCHEDULES])

    @ResponseHandlerRegistry.handler(CMD_DELETE_SCHEDULE)
    def _handle_delete_schedule(self, msg: dict, future) -> None:
        """Handle delete schedule response.

        Firmware that echoes the deleted index triggers the
        schedule_delete listeners; either way a successful envelope
        acknowledges the deletion, so the future resolves (with the index,
        or None when it was not echoed) rather than timing out (D3).
        """
        index = msg.get(FIELD_INDEX)
        if index is not None:
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
                _LOGGER.debug("Notification event: %s (state=%s)", event, sanitize_text(state))
                self._notify_listeners(self.notification_event_listeners, event, state)
                return True
        return False

    def _dispatch_handler(self, name: str, callback: Callable) -> None:
        """Invoke a lifecycle handler, supporting sync and async callables.

        Exceptions from the handler are logged, never propagated (D3).
        """
        try:
            result = callback()
        except Exception:
            _LOGGER.exception("Connection handler %r raised", name)
            return
        if inspect.isawaitable(result):
            self._track_task(result, transient=False)

    def start(self) -> None:
        """Start the client and initiate connection.

        If no event loop was provided in the constructor and none is
        running, a private loop is created and this call blocks until
        stop() is called.
        """
        self._shutdown = False
        if self._eventLoop is None:
            try:
                self._eventLoop = asyncio.get_running_loop()
            except RuntimeError:
                self._ownLoop = True
                self._eventLoop = asyncio.new_event_loop()

        # Tracked so a connect() that escapes with an unexpected exception is
        # reported immediately rather than at GC time (L1).
        self._track_task(self.connect())

        if self._ownLoop:
            _LOGGER.info("Starting up our own event loop.")
            self._eventLoop.run_forever()
            self._eventLoop.close()
            _LOGGER.info("Connection shut down.")

    def stop(self) -> None:
        """Stop the client and close the connection (thread-safe).

        Marshals the shutdown onto the event loop, so it may be called
        from any thread; also stops a private loop created by start().
        From the loop thread, shutdown() may be used directly instead.
        """
        self._shutdown = True

        _LOGGER.info("Shutting down Power Pet Door client connection...")
        if self._eventLoop is None:
            return
        self._eventLoop.call_soon_threadsafe(self.disconnect)
        if self._ownLoop:
            self._eventLoop.call_soon_threadsafe(self._eventLoop.stop)

    def shutdown(self) -> None:
        """Shut down the client: stop reconnecting and close the connection.

        Idempotent, and safe to call before connect(). After shutdown()
        the client stays down - connect() becomes a no-op - until
        reset_shutdown() (or start()) is called. Must be called from the
        event loop thread; use stop() from other threads.
        """
        self._shutdown = True
        self.disconnect()

    async def aclose(self, timeout: float | None = None) -> None:
        """Shut down and await outstanding async lifecycle handlers (T2).

        :meth:`disconnect` deliberately leaves async ``on_connect``/
        ``on_disconnect`` handlers running - an ``on_disconnect`` coroutine
        must survive the teardown that triggered it - which means nothing
        else ever awaits or cancels them. This is the clean teardown point
        for an embedding application: the handlers are given ``timeout``
        seconds to finish, and whatever is still running is cancelled.

        Cancelling ``aclose()`` itself does not weaken the guarantee: the
        cancel step runs from a ``finally``, so handlers are cancelled on
        the way out rather than left running un-awaited (L2). That is the
        normal shutdown shape - an embedding application wrapping
        ``door.disconnect()`` in its own deadline, or being unloaded by a
        host framework that cancels the task.

        Args:
            timeout: Seconds to wait for handlers; defaults to cfg_timeout.
        """
        self.shutdown()
        current = asyncio.current_task()
        tasks = [task for task in self._handler_tasks if task is not current]
        if not tasks:
            return
        pending: set[asyncio.Task] = set(tasks)
        try:
            _done, pending = await asyncio.wait(
                tasks, timeout=self.cfg_timeout if timeout is None else timeout
            )
        finally:
            for task in pending:
                if not task.done():
                    _LOGGER.warning("Cancelling a connection handler that did not finish in time")
                    task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def reset_shutdown(self) -> None:
        """Re-enable the client after shutdown()/stop() so it may connect again."""
        self._shutdown = False

    async def connect(self) -> None:
        """Establish connection to the device.

        Idempotent: a call made while already connected, or while another
        connect() is still in flight, is a no-op — the device accepts a
        single connection, so a second socket would orphan the first and
        hog the device's only slot (M2).

        On failure the error is logged and a reconnect attempt is scheduled
        with backoff; this method only raises asyncio.CancelledError. That
        includes failures ``create_connection()`` reports as something other
        than OSError - an over-long IDNA label or a lone surrogate in the
        host raises UnicodeEncodeError, and a port outside 0-65535 raises
        OverflowError (L1). No-op after shutdown()/stop().
        """
        if self._shutdown:
            _LOGGER.debug("Ignoring connect() while shut down")
            return
        if self._transport is not None:
            _LOGGER.warning("Ignoring connect(): already connected to %s", self.cfg_host)
            return
        if self._connecting:
            _LOGGER.warning("Ignoring connect(): a connection attempt is already in progress")
            return
        loop = self._get_loop()
        _LOGGER.info(
            "Started to connect to Power Pet Door... at %s:%s", self.cfg_host, self.cfg_port
        )
        self._connecting = True
        try:
            async with asyncio.timeout(self.cfg_timeout):
                # Each attempt gets its own protocol shim so a transport this
                # client does not adopt cannot drive its connection state
                # (M1/L2/T1) - see _ConnectionAttempt.
                await loop.create_connection(
                    lambda: _ConnectionAttempt(self), self.cfg_host, self.cfg_port
                )
        except (OSError, TimeoutError, ValueError, OverflowError) as err:
            _LOGGER.error(
                "Unable to connect to Power Pet Door at %s:%s: %s",
                self.cfg_host,
                self.cfg_port,
                err,
            )
            self.handle_connect_failure()
        finally:
            self._connecting = False

    def connection_made(self, transport) -> None:
        """asyncio callback for a successful connection.

        ``PowerPetDoorClient`` is a public ``asyncio.Protocol``, so it may be
        handed to ``create_connection()`` directly. asyncio attaches no
        identity to the ``connection_lost()`` it later delivers *on this same
        object*, so both kinds of event that must not tear down a healthy
        connection are counted here: a transport this client declined and
        aborted (L2), and one it adopted and has since replaced (L1).
        """
        if self._adopt_transport(transport):
            self._pending_direct_losses += 1
        else:
            self._declined += 1

    def _adopt_transport(self, transport) -> bool:
        """Take ownership of ``transport``, or decline and abort it.

        Returns:
            True if the transport was adopted and now drives client state.
        """
        if self._shutdown:
            # shutdown()/stop() landed while create_connection() was still in
            # flight. disconnect() already ran, so nothing holds a reference
            # that would ever close this socket - abandon it here instead of
            # leaving a "shut down" client keepalive-pinging the device (M1).
            _LOGGER.info("Discarding a connection that completed after shutdown")
            transport.abort()
            return False
        if self._transport is not None:
            # Belt and braces behind connect()'s guard: never let a second
            # transport orphan the live one (M2).
            _LOGGER.warning("Rejecting a second connection; one is already established")
            transport.abort()
            return False
        _LOGGER.info("Connection Successful!")
        self._transport = transport
        self._was_connected = True
        self._reconnect_attempts = 0

        if self.cfg_keepalive:
            self._keepalive = self._track_task(self.keepalive())

        # Flush anything that was enqueued while disconnected (L3),
        # otherwise open the dequeue gate for the next enqueue.
        if self._queue:
            self._can_dequeue = False
            self._track_task(self.dequeue_data())
        else:
            self._can_dequeue = True

        # Caller code
        for name, callback in list(self.on_connect.items()):
            self._dispatch_handler(name, callback)
        return True

    def connection_lost(self, exc) -> None:
        """asyncio callback for connection lost (direct ``Protocol`` wiring).

        This is the entry point for a client handed to
        ``create_connection()`` directly. asyncio does not say *which*
        transport was lost, so the two classes of event that must not reach
        the teardown path are counted off first - a declined transport (L2)
        and a superseded one (L1). :class:`_ConnectionAttempt` never comes
        through here: it knows its own transport and calls
        :meth:`_on_transport_lost` after making the equivalent checks by
        identity.
        """
        if self._declined:
            # A transport connection_made() refused and aborted. It never
            # drove any state, so its lifecycle event must not close the
            # connection that is actually live (L2).
            self._declined -= 1
            _LOGGER.debug("Ignoring connection_lost() from a declined transport")
            return
        superseded = self._pending_direct_losses > 1
        if self._pending_direct_losses:
            self._pending_direct_losses -= 1
        if superseded:
            # More than one adopted transport is still awaiting its loss, so
            # this one is an older socket disconnect() already dropped and a
            # newer connection has replaced. Forwarding it would close the
            # healthy transport, fail its outstanding futures and burn a
            # reconnect (L1) - the direct-wiring twin of the shim's
            # identity check.
            _LOGGER.debug("Ignoring connection_lost() from a superseded transport")
            return
        self._on_transport_lost(exc)

    def _on_transport_lost(self, exc) -> None:
        """Handle the loss of the transport that was actually driving state.

        Callers must already have established that the event belongs to the
        live connection (by identity for :class:`_ConnectionAttempt`, by
        counting for the direct ``Protocol`` path).
        """
        if not self._was_connected:
            # A local teardown already ran - an explicit disconnect(), a
            # keepalive give-up, a write failure or an overflow drop. The
            # cleanup is done and whatever reconnect those paths wanted is
            # already scheduled; asyncio simply delivers the socket's loss a
            # loop iteration later. Reporting a server-side close nobody saw
            # and burning a second reconnect is wrong (T1).
            _LOGGER.debug("Ignoring connection_lost() for an already-closed connection")
            return
        self.disconnect()
        if not self._shutdown:
            _LOGGER.error("The server closed the connection. Reconnecting...")
            self._schedule_reconnect()

    def _drop_connection(self) -> None:
        """Tear down a connection that failed on this side, then reconnect.

        The reconnect used to be an implicit side effect of the
        ``connection_lost()`` that ``disconnect()`` provokes. Scheduling it
        here makes the intent explicit, which is what lets
        :meth:`_on_transport_lost` ignore the trailing loss event instead of
        treating it as a fresh server-side close (T1).
        """
        self.disconnect()
        if not self._shutdown:
            self._schedule_reconnect()

    def _next_reconnect_delay(self) -> float:
        """Compute the next reconnect delay (L1).

        Exponential backoff starting at cfg_reconnect, doubling per
        consecutive failed attempt up to MAX_RECONNECT_DELAY, plus up to
        RECONNECT_JITTER fractional random jitter so multiple clients do
        not retry in lockstep against the single-connection device.
        """
        delay = min(
            self.cfg_reconnect * (2.0 ** min(self._reconnect_attempts, 16)), MAX_RECONNECT_DELAY
        )
        self._reconnect_attempts += 1
        return delay + random.uniform(0, delay * RECONNECT_JITTER)

    def _schedule_reconnect(self) -> None:
        """Schedule a tracked reconnect attempt (cancelled by disconnect/stop)."""
        # Tracked in _tasks as well as _reconnect_task so an exception that
        # escapes reconnect() is logged immediately (L1).
        self._reconnect_task = self._track_task(self.reconnect(self._next_reconnect_delay()))

    async def reconnect(self, delay) -> None:
        """Reconnect after a delay, unless the client has been shut down."""
        await asyncio.sleep(delay)
        if self._shutdown:
            return
        await self.connect()

    def disconnect(self) -> None:
        """Close connection and cleanup.

        Safe to call at any time - including before connect() and multiple
        times. Pending reconnect attempts and tracked background work are
        cancelled, outstanding notify futures are failed with
        ConnectionError (never cancelled, so callers can distinguish
        disconnection from task cancellation), and on_disconnect handlers
        fire only if a connection actually existed (L2).
        """
        was_connected = self._was_connected
        self._was_connected = False
        if was_connected:
            _LOGGER.debug("Closing connection with server...")
        self._can_dequeue = False
        self._last_ping = None
        self._last_command = None
        self._last_send = 0
        self._failed_msg = 0
        self._failed_pings = 0
        self._inflight_msg_id = None
        self._scanner.reset()
        # Same connection scope as the scanner: report the tail, then start
        # the next connection's count clean.
        for throttle in (
            self._non_ascii,
            self._bad_frames,
            self._bad_messages,
            self._device_errors,
            self._bad_msg_ids,
            self._bad_hw_payloads,
        ):
            throttle.flush()
            throttle.reset()
        # Undispatched frames belong to the connection that delivered them.
        self._dispatcher.reset()
        self._queue.clear()
        self._msg_sequence = 0  # Reset sequence counter

        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        if self._check_receipt:
            self._check_receipt.cancel()
            self._check_receipt = None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None

        if self._reconnect_task is not None:
            task = self._reconnect_task
            self._reconnect_task = None
            if task is not current:
                task.cancel()

        # Cancel fire-and-forget work still in flight (L3). The caller's own
        # task is skipped: disconnect() is reachable from inside one (a
        # failed write), and self-cancelling it there would surface as a
        # spurious CancelledError in unrelated code.
        for task in list(self._tasks):
            if task is not current:
                task.cancel()

        if self._transport:
            self._transport.close()
            self._transport = None

        for future in list(self._outstanding.values()):
            if not future.done():
                future.set_exception(
                    ConnectionError("Connection closed before a response was received")
                )
        self._outstanding.clear()

        # Caller code
        if was_connected:
            for name, callback in list(self.on_disconnect.items()):
                self._dispatch_handler(name, callback)

    def handle_connect_failure(self) -> None:
        """Handler for if we fail to connect to the power pet door."""
        if not self._shutdown:
            _LOGGER.error("Unable to connect to power pet door. Reconnecting...")
            self.disconnect()
            self._schedule_reconnect()

    async def keepalive(self) -> None:
        _keepalive = self._keepalive
        await asyncio.sleep(self.cfg_keepalive)
        if _keepalive and not _keepalive.cancelled():
            if self._last_ping is not None:
                self._failed_pings += 1
                if self._failed_pings < MAX_FAILED_PINGS:
                    _LOGGER.warning(
                        "Last PING not responded to %d of %d...",
                        self._failed_pings,
                        MAX_FAILED_PINGS,
                    )
                else:
                    _LOGGER.error("Last PING not responded to %d times.", self._failed_pings)
                    self._drop_connection()
                    return

            # The wire token stays wall-clock milliseconds (device
            # compatibility); latency is measured against the monotonic
            # clock so NTP steps cannot skew it (L11).
            self._last_ping = str(round(time.time() * 1000))
            self._last_ping_time = time.monotonic()
            self.send_message(PING, self._last_ping)

    def _fail_inflight_future(self, exc: Exception) -> None:
        """Fail the future paired with the in-flight message, if any (H4)."""
        if self._inflight_msg_id is None:
            return
        future = self._outstanding.get(self._inflight_msg_id)
        self._inflight_msg_id = None
        if future is not None and not future.done():
            future.set_exception(exc)

    async def check_receipt(self, rawdata) -> None:
        _check_receipt = self._check_receipt
        await asyncio.sleep(self.cfg_timeout)
        if _check_receipt and not _check_receipt.cancelled():
            self._failed_msg += 1
            if self._failed_msg < MAX_FAILED_MSG:
                _LOGGER.warning(
                    "Did not receive a response to a %s message in more than %s seconds, retrying.",
                    self._last_command,
                    self.cfg_timeout,
                )
            else:
                _LOGGER.error(
                    "Did not receive a response to a %s message in more than %s seconds %s times, dropped.",
                    self._last_command,
                    self.cfg_timeout,
                    self._failed_msg,
                )
                self._failed_msg = 0
                # Fail fast: the documented `await future` pattern must not
                # hang forever on a dropped message (H4).
                self._fail_inflight_future(
                    TimeoutError(
                        f"No response to {self._last_command} after {MAX_FAILED_MSG} attempts"
                    )
                )
        else:
            self._failed_msg = 0

        self._check_receipt = None
        if self._failed_msg == 0:
            await self.dequeue_data()
        else:
            await self._send_data(rawdata)

    def enqueue_data(self, data, priority: int = PRIORITY_LOW) -> None:
        """Enqueue a message with the given priority.

        Lower priority values are processed first. Must be called from
        the event loop thread (see class docstring).
        """
        msg = PrioritizedMessage(priority=priority, sequence=self._msg_sequence, data=data)
        self._msg_sequence += 1
        heapq.heappush(self._queue, msg)
        if self._transport and self._can_dequeue:
            self._can_dequeue = False
            self._track_task(self.dequeue_data())

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
            # Re-check after the sleep: disconnect() may have run in the
            # meantime and cleared the transport (M7).
            transport = self._transport
            if transport is None or transport.is_closing():
                _LOGGER.warning("Connection closed while waiting to send; dropping message")
                return
            _LOGGER.debug("TX > %s", rawdata)
            transport.write(rawdata)
            self._last_send = time.monotonic()

            if self.cfg_keepalive:
                self._keepalive = self._track_task(self.keepalive())

            if self._last_command:
                self._check_receipt = self._track_task(self.check_receipt(rawdata))
            else:
                await self.dequeue_data()

        except (RuntimeError, OSError) as err:
            _LOGGER.error("Failed to write to the stream. (%s)", err)
            self._drop_connection()

    async def dequeue_data(self) -> None:
        """Send the next queued message, if the connection is idle."""
        if not self._transport:
            _LOGGER.warning("Attempted to write to the stream without a connection active")
            return

        if self._check_receipt:
            _LOGGER.warning("Attempted to send data while another message is still outstanding")
            return

        if not self._queue:
            self._can_dequeue = True
            return

        prioritized_msg = heapq.heappop(self._queue)
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

        # Track the in-flight msgId so check_receipt can fail its future
        # if the message is ultimately dropped (H4).
        self._inflight_msg_id = data.get(FIELD_MSG_ID)
        self._failed_msg = 0
        rawdata = json.dumps(data).encode("ascii")
        await self._send_data(rawdata)

    def data_received(self, data) -> None:
        """asyncio callback for any data received from the power pet door.

        All received bytes are untrusted. Non-ASCII bytes are escaped
        rather than dropped, so a single bad byte corrupts only the frame
        that contains it instead of desynchronizing the stream (L2). The
        stream is then framed by the shared scanner
        (:mod:`powerpetdoor.framing`): garbage is discarded with a warning,
        malformed JSON frames are logged and skipped, and exceeding the
        un-parsed buffer cap drops the connection. This callback never
        raises on arbitrary input.
        """
        if not data:
            return

        # Decoding per chunk and dropping the chunk on error would strand a
        # half-buffered frame forever (framing never completes again until
        # the 64 KiB overflow disconnect). backslashreplace keeps every byte
        # position accounted for: the offending frame simply fails
        # json.loads and is skipped on its own.
        decoded = data.decode("ascii", errors="backslashreplace")
        if len(decoded) != len(data):
            self._non_ascii.record(len(data))

        # Device bytes reach an operator's terminal through the host
        # application's log (`tail -f`, `journalctl`, `docker logs`), so ESC
        # and friends must be escaped before they get there. Guarded like
        # the simulator's identical line: sanitize_text is ~20x the cost of
        # the suppressed logger.debug call it feeds (T2).
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("RX < %s", sanitize_text(decoded))
        frames, diag = self._scanner.feed(decoded)

        if diag.overflow:
            # Checked before dispatch, not after: _drop_connection() ->
            # disconnect() cancels every task in `_tasks`, so frames
            # dispatched first were created and then killed before they ran
            # - a complete, legitimate frame vanishing with nothing in the
            # log to say so (T5). Report the loss instead of hiding it.
            _LOGGER.error(
                "Receive buffer exceeded %d bytes without a complete message; disconnecting "
                "(discarding %d complete frame(s) received in the same read)",
                MAX_BUFFER_SIZE,
                len(frames),
            )
            self._drop_connection()
            return

        # Bounded dispatch: one task per frame, created synchronously here,
        # let one 256 KiB read of `{}` admit 131,072 live tasks and ~135 MB
        # of heap before any of them ran (round-6 security finding 1).
        self._dispatcher.submit(frames, self._transport)

    def _dispatch_frame(self, frame: str) -> asyncio.Task | None:
        """Decode one framed message and start its handler.

        Returns:
            The handler task, or None if the frame was not usable JSON.
        """
        try:
            msg = json.loads(frame)
        except (ValueError, RecursionError) as err:
            # `json.JSONDecodeError` is a *subclass* of ValueError, not a
            # superset of what json.loads raises. Two shapes well inside
            # every declared bound escape it: an integer literal over
            # `sys.get_int_max_str_digits()` (4300) digits raises a bare
            # ValueError, and deep nesting raises RecursionError. Both are
            # brace-balanced and under MAX_BUFFER_SIZE, so FrameScanner
            # frames them and hands them straight here - and the escape
            # killed the transport from `data_received` (a hot reconnect
            # loop, because every attempt connects before dying and resets
            # the backoff) or, from the `call_soon` re-arm, left the
            # dispatcher permanently wedged (round-8 backend M1 /
            # security M1). Widening the clause narrows nothing: a frame
            # that fails to decode already took this exact path.
            #
            # Throttled: this fires once per *frame*, so it is limited by
            # the peer's byte rate rather than its packet rate - `{x}` is
            # three bytes and used to buy a 135-byte ERROR, x28 write
            # amplification (round-6 security finding 2). The detail rides
            # the same doubling schedule as the summary, bounded in length
            # because the frame is peer-chosen.
            if self._bad_frames.record(len(frame)):
                _LOGGER.error(
                    "Failed to decode JSON frame (%s): %s",
                    err,
                    sanitize_text(frame, MAX_LOGGED_LENGTH),
                )
            return None
        return self._track_task(self.process_message(msg, frame_size=len(frame)))

    async def process_message(self, msg, *, frame_size: int | None = None) -> None:
        """Process an incoming message from the device.

        Uses the ResponseHandlerRegistry to dispatch to the appropriate
        handler. All network data is untrusted: envelope fields are read
        defensively, handler dispatch is isolated so a malformed payload
        cannot kill the receive path, and a future paired with the message
        is always completed (result or :class:`CommandError`) rather than
        left hanging.

        Args:
            msg: The decoded message.
            frame_size: Bytes this message occupied on the wire, for the
                two per-frame throttles below. Both used to hand
                ``len(json.dumps(msg))`` to ``record()``, which is neither
                the received size nor free: every sibling throttle on this
                path records real received bytes
                (``self._non_ascii.record(len(data))``,
                ``self._bad_frames.record(len(frame))``), and a peer that
                pads its frames controls the re-serialized size
                independently of what it actually sent - 60,002 wire bytes
                were reported as "2 bytes", a 30,001x under-report, in the
                number an operator reads to decide whether a peer is worth
                investigating (round-9 backend F2). Optional so that direct
                callers - tests, third-party subclasses - keep working.
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
            # Per-frame and peer-controlled, exactly like the JSON decode
            # failure above: `{}` is two bytes of legal JSON and bought a
            # ~100-byte WARNING each (round-6 security finding 2).
            if self._bad_messages.record(_recorded_size(msg, frame_size)):
                _LOGGER.warning(
                    "Ignoring malformed message from device: %s",
                    sanitize_text(json.dumps(msg), MAX_LOGGED_LENGTH),
                )
            return

        future = None
        reply_msg_id = msg.get(FIELD_MSG_ID_RESPONSE)
        if reply_msg_id is not None:
            self.replyMsgId = reply_msg_id
            # The device supplies this; anything that is not a usable dict
            # key (a list, a dict, ...) must not raise here - it simply
            # matches no outstanding future (D3).
            if isinstance(reply_msg_id, (int, str)):
                outstanding = self._outstanding.get(reply_msg_id)
                if outstanding is not None and not outstanding.done():
                    future = outstanding
            else:
                # Every response echoes a msgID, so this is a per-frame site
                # with a peer-chosen value: capped and throttled like its
                # four siblings in this file (round-9 security M1).
                rendered = sanitize_text(reply_msg_id, MAX_LOGGED_LENGTH)
                if self._bad_msg_ids.record(len(rendered)):
                    _LOGGER.warning(
                        "Ignoring unusable msgID %s in device response; no future to resolve",
                        rendered,
                    )

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
                        # Unreachable from any frame constructed in rounds
                        # 7-9 (every registered handler's payload access is
                        # guarded), so this is not throttled - but the frame
                        # is still peer-chosen and can run to the 64 KiB
                        # framing cap, so the *constant* is bounded like
                        # every other per-frame line (round-9 security M1,
                        # recommendation 3).
                        _LOGGER.exception(
                            "Error handling %s response: %s",
                            cmd,
                            sanitize_text(json.dumps(msg), MAX_LOGGED_LENGTH),
                        )
                        if future is not None and not future.done():
                            future.set_exception(CommandError(cmd, "Malformed response"))
                    # A handler that ran but could not resolve its future
                    # means the payload lacked the expected field. Fail it
                    # typed - never cancel() an API caller's future (L9).
                    if future is not None and not future.done():
                        future.set_exception(CommandError(cmd, "Response missing expected field"))
                elif future is not None and not future.done():
                    # Successful response with no specialized handler: the
                    # acknowledgment itself is the result.
                    future.set_result(msg)
            else:
                reason = msg.get(FIELD_REASON)
                # Fires for every frame carrying a CMD whose success is not
                # "true" - the device's *ordinary* error envelope, not just
                # malformed input - so a peer packing 11-byte {"CMD":"a"}
                # envelopes bought one unthrottled WARNING per frame at
                # x6.64 the wire bytes, in the host application's log
                # (round-7 security L3). This is the shipped library; for
                # the Home Assistant deployment target that is the whole
                # instance's log. Throttled and length-capped like the
                # three sibling sites; the first occurrence is still
                # reported immediately and in full context.
                if self._device_errors.record(_recorded_size(msg, frame_size)):
                    _LOGGER.warning(
                        "Error reported by device: %s",
                        sanitize_text(json.dumps(msg), MAX_LOGGED_LENGTH),
                    )
                if future is not None and not future.done():
                    future.set_exception(CommandError(cmd, reason))
        finally:
            if dequeue:
                await self.dequeue_data()

    @overload
    def send_message(
        self, type: str, arg: str, notify: Literal[True], **kwargs: Any
    ) -> asyncio.Future: ...

    @overload
    def send_message(
        self, type: str, arg: str, notify: Literal[False] = ..., **kwargs: Any
    ) -> None: ...

    def send_message(
        self, type: str, arg: str, notify: bool = False, **kwargs: Any
    ) -> asyncio.Future | None:
        """Send a message to the Power Pet Door.

        Args:
            type: Message type (COMMAND, CONFIG, or PING)
            arg: Command or config name
            notify: If True, returns a Future that resolves with the response
            **kwargs: Additional message parameters

        Returns:
            asyncio.Future if notify=True, otherwise None. The future
            resolves with the response payload, or raises CommandError
            (device-reported failure or malformed response), TimeoutError
            (message dropped after retries) or ConnectionError (connection
            lost before a response arrived).
        """
        msg_id = self.msgId
        rv = None
        if notify:
            rv = self._get_loop().create_future()
            self._outstanding[msg_id] = rv

            def cleanup(fut: asyncio.Future) -> None:
                self._outstanding.pop(msg_id, None)

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
    def _buffer(self) -> str:
        """The framing scanner's un-parsed remainder (introspection hook)."""
        return self._scanner.buffer

    @property
    def available(self) -> bool:
        """Whether the client is connected and available."""
        return bool(self._transport and not self._transport.is_closing())

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


class _ConnectionAttempt(asyncio.Protocol):
    """Per-attempt protocol shim for one :meth:`PowerPetDoorClient.connect`.

    :class:`PowerPetDoorClient` is itself an ``asyncio.Protocol``. Attaching
    it directly to every transport means one object receives the lifecycle
    events of every socket it was ever attached to, with no way to tell them
    apart — so a transport the client *declined* (a shutdown that landed
    mid-connect, or a second connection) delivers ``connection_lost`` into
    the live connection's teardown path, and a socket ``disconnect()`` has
    already replaced logs a bogus loss and burns a reconnect.

    Giving every attempt its own shim restores that identity. Only a
    transport the client adopted, and only while it is still the live one,
    reaches the client's callbacks (M1/L2/T1).
    """

    __slots__ = ("_adopted", "_client", "_transport")

    def __init__(self, client: PowerPetDoorClient) -> None:
        self._client = client
        self._transport: asyncio.BaseTransport | None = None
        self._adopted = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Offer the new transport to the client, which may decline it."""
        self._transport = transport
        self._adopted = self._client._adopt_transport(transport)

    def connection_lost(self, exc: Exception | None) -> None:
        """Route the loss to the client only if this transport is still its own."""
        if not self._adopted:
            # Declined and aborted in connection_made(): it never drove any
            # client state, so there is nothing to tear down (M1/L2).
            return
        current = self._client._transport
        if current is not None and current is not self._transport:
            # disconnect() dropped this socket and a newer connection has
            # since been established; this event is stale (T1).
            _LOGGER.debug("Ignoring connection_lost() from a superseded transport")
            return
        # Goes straight to the client's teardown: the identity checks above
        # are this shim's equivalent of the counting the public
        # connection_lost() has to do, and running both would double-count.
        self._client._on_transport_lost(exc)

    def data_received(self, data: bytes) -> None:
        """Forward received bytes, unless this transport was declined."""
        if self._adopted:
            self._client.data_received(data)
