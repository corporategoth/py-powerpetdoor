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
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SET_HOLD_TIME,
    CMD_SET_NOTIFICATIONS,
    CMD_SET_SCHEDULE,
    CMD_SET_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    CMD_SET_TIMEZONE,
    COMMAND,
    COMMAND_ENVELOPE_COMMANDS,
    COMMAND_PRIORITIES,
    CONFIG,
    DOOR_OPTION_AUTORETRACT,
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
    FIELD_SCHEDULE,
    FIELD_SCHEDULES,
    FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS,
    FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS,
    FIELD_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SETTINGS,
    FIELD_SLEEP_SENSOR_TRIGGER_VOLTAGE,
    FIELD_SUCCESS,
    FIELD_TIME,
    FIELD_TOTAL_AUTO_RETRACTS,
    FIELD_TOTAL_OPEN_CYCLES,
    FIELD_TZ,
    FIELD_VOLTAGE,
    MINIMUM_TIME_BETWEEN_MSGS,
    PHONE_TO_DOOR,
    PING,
    PONG,
    PRIORITY_CRITICAL,
    PRIORITY_LOW,
    SUCCESS_TRUE,
)
from .framing import MAX_BUFFER_SIZE, FrameScanner, find_frame_end
from .i18n import t
from .sanitize import MAX_LOGGED_LENGTH, sanitize_field, sanitize_text

_LOGGER = logging.getLogger(__name__)

#: Maximum retry attempts before dropping a message
MAX_FAILED_MSG = 2
MAX_FAILED_PINGS = 3

#: Cap on the exponential reconnect backoff delay, in seconds
MAX_RECONNECT_DELAY = 300.0
#: Maximum fraction of the backoff delay added as random jitter, so several
#: clients do not retry in lockstep against the single-connection device.
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
            ::

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
        frame into a full traceback in the host application's log.
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


def autoretract_from_door_options(value: object) -> bool | None:
    """Read the auto-retract flag out of a ``doorOptions`` value.

    ``doorOptions`` is an integer
    **bitfield**, not the ``"0"``/``"1"`` flag this project documented for
    years. ``DISABLE_AUTORETRACT`` leaves it at ``0`` and
    ``ENABLE_AUTORETRACT`` leaves it at ``2``, so auto-retract is
    :data:`~powerpetdoor.const.DOOR_OPTION_AUTORETRACT` - **bit 1**.

    Plain truthiness happens to give the right answer on that unit only
    because ``2`` is truthy; it would misreport auto-retract as *on* the
    moment any other (still unidentified) bit is set, which is why the
    whole library reads this one field through here.

    Liberal like every other reader in this module: a JSON boolean, or a
    string that is a recognizable flag rather than a number, is answered by
    :func:`make_bool` - a firmware that replies ``true`` there is answering
    the question directly rather than handing over a bitfield.

    Args:
        value: The raw ``doorOptions`` value from the device.

    Returns:
        The auto-retract flag, or None if the value is not readable.
    """
    if isinstance(value, bool):
        return value
    try:
        bits = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return make_bool(value) if isinstance(value, str) else None
    return bool(bits & DOOR_OPTION_AUTORETRACT)


def build_set_voltage_message(millivolts: int) -> dict[str, int]:
    """Build the message fields for either sensor-trigger-voltage setter.

    Both
    ``SET_SENSOR_TRIGGER_VOLTAGE`` and
    ``SET_SLEEP_SENSOR_TRIGGER_VOLTAGE`` take their new value in a field
    named ``voltage`` - **not** the ``sensorTriggerVoltage`` /
    ``sleepSensorTriggerVoltage`` name their *getters* answer with, which
    the device rejects. The reply then echoes the getter's field name.
    That asymmetry is the entire wire shape of these two commands, so it
    lives here rather than in a caller::

        client.send_message(
            envelope_for_command(CMD_SET_SENSOR_TRIGGER_VOLTAGE),
            CMD_SET_SENSOR_TRIGGER_VOLTAGE,
            notify=True, **build_set_voltage_message(1500),
        )

    Args:
        millivolts: The trigger threshold. A typical unit ships at 2000 and
            accepted 1500/1800.

    Returns:
        ``{"voltage": <millivolts>}``, ready to splat into
        :meth:`PowerPetDoorClient.send_message`.
    """
    return {FIELD_VOLTAGE: int(millivolts)}


def envelope_for_command(cmd: str) -> str:
    """Return the envelope key ``cmd`` must be sent under.

    The two envelope keys are not
    interchangeable: ``{"cmd": "ENABLE_INSIDE"}`` is answered
    ``success: "false"`` and does nothing, while
    ``{"config": "ENABLE_INSIDE"}`` succeeds. Only door motion is a
    ``cmd``; everything else - queries, ``SET_*``, and the individual
    setting commands - is a ``config``.

    This is the single place that decision is made, so a message-level
    caller does not have to carry the table around::

        client.send_message(envelope_for_command(CMD_ENABLE_INSIDE), CMD_ENABLE_INSIDE)

    Args:
        cmd: A ``CMD_*`` command name.

    Returns:
        :data:`~powerpetdoor.const.COMMAND` or
        :data:`~powerpetdoor.const.CONFIG`.
    """
    return COMMAND if cmd in COMMAND_ENVELOPE_COMMANDS else CONFIG


def build_set_hold_time_message(seconds: float) -> dict[str, int]:
    """Build the ``SET_HOLD_TIME`` message fields for a time in seconds.

    The device counts the hold-open time in **centiseconds**, so the
    seconds a caller thinks in have to be multiplied by 100 before they go
    on the wire. That conversion is the whole wire shape of this command,
    and it lives here so a message-level caller reaches it too::

        client.send_message(
            envelope_for_command(CMD_SET_HOLD_TIME), CMD_SET_HOLD_TIME,
            notify=True, **build_set_hold_time_message(2.0),
        )

    The unit is centiseconds: ``holdOpenTime: 200`` is a 2-second hold.

    Args:
        seconds: Hold-open time in seconds. Truncated, not rounded, to
            whole centiseconds, which is what every released version has
            sent.

    Returns:
        ``{"holdTime": <centiseconds>}``, ready to splat into
        :meth:`PowerPetDoorClient.send_message`.
    """
    return {FIELD_HOLD_TIME: int(seconds * 100)}


def build_set_notifications_message(
    *,
    inside_on: bool,
    inside_off: bool,
    outside_on: bool,
    outside_off: bool,
    low_battery: bool,
) -> dict[str, dict[str, bool]]:
    """Build the ``SET_NOTIFICATIONS`` message fields.

    The device requires a nested
    ``notifications`` object carrying **all five** flags as **JSON
    booleans**. Two failure modes were observed, and the second is the
    dangerous one:

    * flat top-level fields (any value type) -> ``success: "false"``,
      nothing written;
    * nested object whose values are *strings* -> the device replies with
      the current settings and **silently writes nothing**. It looks like
      success.

    This is the single place that shape is built, so both the friendly
    facade (:meth:`powerpetdoor.door.PowerPetDoor.set_notifications`) and a
    message-level caller get it right::

        client.send_message(
            CONFIG, CMD_SET_NOTIFICATIONS, notify=True,
            **build_set_notifications_message(
                inside_on=True, inside_off=False, outside_on=True,
                outside_off=False, low_battery=True,
            ),
        )

    There is no partial form: the device is given the complete set every
    time, so a caller changing one flag must supply the other four (the
    facade merges them from its cached
    :class:`~powerpetdoor.door.NotificationSettings`).

    Returns:
        ``{"notifications": {<five flags>: bool}}``, ready to splat into
        :meth:`PowerPetDoorClient.send_message`.
    """
    return {
        FIELD_NOTIFICATIONS: {
            FIELD_SENSOR_ON_INDOOR_NOTIFICATIONS: bool(inside_on),
            FIELD_SENSOR_OFF_INDOOR_NOTIFICATIONS: bool(inside_off),
            FIELD_SENSOR_ON_OUTDOOR_NOTIFICATIONS: bool(outside_on),
            FIELD_SENSOR_OFF_OUTDOOR_NOTIFICATIONS: bool(outside_off),
            FIELD_LOW_BATTERY_NOTIFICATIONS: bool(low_battery),
        }
    }


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
        self._keepalive: asyncio.Task | None = None
        self._check_receipt: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempts = 0
        self._was_connected = False
        self._last_ping: str | None = None
        self._last_command: str | None = None
        self._can_dequeue = False
        self._last_reply = 0.0
        self._last_ping_time = 0.0
        self._failed_msg = 0
        self._failed_pings = 0
        self._inflight_msg_id: int | None = None
        #: Monotonic send time per outstanding ``msgId``, so a reply's
        #: round-trip can be timed. Monotonic, not the wall clock: an NTP
        #: step must not be able to turn a latency into a negative number.
        self._sent_at: dict[int | str, float] = {}
        # One scanner per client, carried across data_received() calls: a
        # device that dribbles the bytes of a never-terminated object must
        # not make us re-scan the retained buffer every time.
        self._scanner = FrameScanner()
        # Fire-and-forget work is tracked so failures are logged
        # immediately. _tasks is connection-scoped work that disconnect()
        # cancels; _handler_tasks holds async lifecycle handlers, which
        # must survive the teardown that triggered them.
        self._tasks: set[asyncio.Task] = set()
        self._handler_tasks: set[asyncio.Task] = set()
        # Keyed by the msgID echoed back by the device; the client always
        # sends ints, but a response may echo a string.
        self._outstanding: dict[int | str, asyncio.Future] = {}
        # Plain heapq: the client is loop-thread-only (see class docstring),
        # so a lock-based queue.PriorityQueue would only imply a thread
        # safety the rest of the class does not provide.
        self._queue: list[PrioritizedMessage] = []
        self._msg_sequence = 0  # Counter for FIFO ordering within same priority

        if loop is not None:
            _LOGGER.info(
                t(
                    "client.latching_onto_existing_event_loop",
                    "Latching onto an existing event loop.",
                )
            )
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
        self.time_listeners: dict[str, Callable[[str], None]] = {}
        self.hold_time_listeners: dict[str, Callable[[int], None]] = {}
        self.sensor_trigger_voltage_listeners: dict[str, Callable[[int], None]] = {}
        self.sleep_sensor_trigger_voltage_listeners: dict[str, Callable[[int], None]] = {}

        self.remote_id_listeners: dict[str, Callable[[bool], None]] = {}
        self.remote_key_listeners: dict[str, Callable[[bool], None]] = {}
        self.schedule_update_listeners: dict[str, Callable[[dict], None]] = {}
        self.schedule_delete_listeners: dict[str, Callable[[int], None]] = {}

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
        """Schedule fire-and-forget work as a tracked task.

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
            _LOGGER.error(
                t("client.background_client_task_failed", "Background client task failed"),
                exc_info=task.exception(),
            )

    def run_coroutine_threadsafe(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        if self._eventLoop is None:
            raise RuntimeError(
                t(
                    "client.run_coroutine_threadsafe_requires_client",
                    "run_coroutine_threadsafe() requires the client to be started first",
                )
            )
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
        registered - it does not raise KeyError.
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
        time_update: Callable[[str], None] | None = None,
        hold_time_update: Callable[[int], None] | None = None,
        sensor_trigger_voltage_update: Callable[[int], None] | None = None,
        sleep_sensor_trigger_voltage_update: Callable[[int], None] | None = None,
        remote_id_update: Callable[[bool], None] | None = None,
        remote_key_update: Callable[[bool], None] | None = None,
        schedule_update: Callable[[dict], None] | None = None,
        schedule_delete: Callable[[int], None] | None = None,
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
            time_update: Called with the door's wall-clock time as the raw
                ``asctime`` string it sends (see
                :data:`~powerpetdoor.const.TIME_FORMAT`)
            hold_time_update: Called with hold time in **centiseconds**,
                the raw device value (``PowerPetDoor`` divides by 100 to
                expose seconds)
            sensor_trigger_voltage_update: Called with trigger voltage
            sleep_sensor_trigger_voltage_update: Called with sleep trigger voltage
            remote_id_update: Called with True if device has remote ID
            remote_key_update: Called with True if device has remote key
            schedule_update: Called with schedule dict when schedule is added/updated
            schedule_delete: Called with schedule index when schedule is deleted
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
        if time_update:
            self.time_listeners[name] = time_update
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
        if schedule_update:
            self.schedule_update_listeners[name] = schedule_update
        if schedule_delete:
            self.schedule_delete_listeners[name] = schedule_delete

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
        self.time_listeners.pop(name, None)
        self.hold_time_listeners.pop(name, None)
        self.sensor_trigger_voltage_listeners.pop(name, None)
        self.sleep_sensor_trigger_voltage_listeners.pop(name, None)
        self.remote_id_listeners.pop(name, None)
        self.remote_key_listeners.pop(name, None)
        self.schedule_update_listeners.pop(name, None)
        self.schedule_delete_listeners.pop(name, None)

    # -------------------------------------------------------------------------
    # Listener/future dispatch helpers
    # -------------------------------------------------------------------------

    def _notify_listeners(self, listeners: dict[str, Callable], *args: Any) -> None:
        """Invoke listener callbacks, isolating each in its own try/except.

        One misbehaving listener must never prevent the remaining
        listeners from being notified.
        """
        for name, callback in list(listeners.items()):
            try:
                callback(*args)
            except Exception:
                _LOGGER.exception(
                    t(
                        "client.listener_raised_while_handling_update",
                        "Listener %r raised while handling an update",
                    ),
                    name,
                )

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
        graceful "Response missing expected field" path already exists.
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
        path already covered.
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
                # doorOptions is a bitfield, not a flag - see
                # `autoretract_from_door_options`.
                reader = (
                    autoretract_from_door_options if field_name == FIELD_AUTORETRACT else make_bool
                )
                self._notify_listeners(listeners, field_name, reader(settings[field_name]))

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

    @ResponseHandlerRegistry.handler(CMD_GET_TIMERS_ENABLED, CMD_ENABLE_AUTO, CMD_DISABLE_AUTO)
    def _handle_auto(self, msg: dict, future) -> None:
        """Handle timers/auto enabled responses."""
        if FIELD_AUTO in msg:
            val = make_bool(msg[FIELD_AUTO])
            self._notify_listeners(self.sensor_listeners[FIELD_AUTO], FIELD_AUTO, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(
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

    @ResponseHandlerRegistry.handler(CMD_ENABLE_CMD_LOCKOUT, CMD_DISABLE_CMD_LOCKOUT)
    def _handle_cmd_lockout(self, msg: dict, future) -> None:
        """Handle command lockout responses."""
        settings = self._payload_mapping(msg, FIELD_SETTINGS)
        if settings is not None and FIELD_CMD_LOCKOUT in settings:
            val = make_bool(settings[FIELD_CMD_LOCKOUT])
            self._notify_listeners(self.sensor_listeners[FIELD_CMD_LOCKOUT], FIELD_CMD_LOCKOUT, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_ENABLE_AUTORETRACT, CMD_DISABLE_AUTORETRACT)
    def _handle_autoretract(self, msg: dict, future) -> None:
        """Handle autoretract responses.

        The door answers ENABLE_/DISABLE_AUTORETRACT with the
        **whole** settings object rather than the single changed field, so
        this reads its one field out of whatever arrived.
        """
        settings = self._payload_mapping(msg, FIELD_SETTINGS)
        if settings is not None and FIELD_AUTORETRACT in settings:
            val = autoretract_from_door_options(settings[FIELD_AUTORETRACT])
            self._notify_listeners(self.sensor_listeners[FIELD_AUTORETRACT], FIELD_AUTORETRACT, val)
            self._resolve_future(future, val)

    @ResponseHandlerRegistry.handler(CMD_GET_HW_INFO)
    def _handle_hw_info(self, msg: dict, future) -> None:
        """Handle hardware info response.

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
            _LOGGER.warning(
                t(
                    "client.device_sent_non_mapping_payload",
                    "Device sent a non-mapping %s payload; not notifying hw_info listeners: %s",
                ),
                FIELD_FWINFO,
                sanitize_field(msg[FIELD_FWINFO], MAX_LOGGED_LENGTH),
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

    @ResponseHandlerRegistry.handler(CMD_GET_TIME)
    def _handle_time(self, msg: dict, future) -> None:
        """Handle the door's wall-clock reply.

        The value is passed through verbatim - a C ``asctime()`` string in
        the door's own timezone. It is *not* re-checked against the local
        clock: the door was observed to answer a stale frame occasionally,
        and a client is better served by seeing exactly what it said.
        """
        if FIELD_TIME in msg:
            val = msg[FIELD_TIME]
            self._notify_listeners(self.time_listeners, val)
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

        Same missing-field guard as :meth:`_handle_door_status`: a legal
        envelope without the payload field returns without resolving.
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
        or None when it was not echoed) rather than timing out.
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

    def _dispatch_handler(self, name: str, callback: Callable) -> None:
        """Invoke a lifecycle handler, supporting sync and async callables.

        Exceptions from the handler are logged, never propagated.
        """
        try:
            result = callback()
        except Exception:
            _LOGGER.exception(
                t("client.connection_handler_raised", "Connection handler %r raised"), name
            )
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
        # reported immediately rather than at GC time.
        self._track_task(self.connect())

        if self._ownLoop:
            _LOGGER.info(t("client.starting_up_our_own_event", "Starting up our own event loop."))
            self._eventLoop.run_forever()
            self._eventLoop.close()
            _LOGGER.info(t("client.connection_shut_down", "Connection shut down."))

    def stop(self) -> None:
        """Stop the client and close the connection (thread-safe).

        Marshals the shutdown onto the event loop, so it may be called
        from any thread; also stops a private loop created by start().
        From the loop thread, shutdown() may be used directly instead.
        """
        self._shutdown = True

        _LOGGER.info(
            t(
                "client.shutting_down_power_pet_door",
                "Shutting down Power Pet Door client connection...",
            )
        )
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
        """Shut down and await outstanding async lifecycle handlers.

        :meth:`disconnect` deliberately leaves async ``on_connect``/
        ``on_disconnect`` handlers running - an ``on_disconnect`` coroutine
        must survive the teardown that triggered it - which means nothing
        else ever awaits or cancels them. This is the clean teardown point
        for an embedding application: the handlers are given ``timeout``
        seconds to finish, and whatever is still running is cancelled.

        Cancelling ``aclose()`` itself does not weaken the guarantee: the
        cancel step runs from a ``finally``, so handlers are cancelled on
        the way out rather than left running un-awaited. That is the
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
                    _LOGGER.warning(
                        t(
                            "client.cancelling_connection_handler_did_finish",
                            "Cancelling a connection handler that did not finish in time",
                        )
                    )
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
        hog the device's only slot.

        On failure the error is logged and a reconnect attempt is scheduled
        with backoff; this method only raises asyncio.CancelledError. That
        includes failures ``create_connection()`` reports as something other
        than OSError - an over-long IDNA label or a lone surrogate in the
        host raises UnicodeEncodeError, and a port outside 0-65535 raises
        OverflowError. No-op after shutdown()/stop().
        """
        if self._shutdown:
            _LOGGER.debug(
                t("client.ignoring_connect_while_shut_down", "Ignoring connect() while shut down")
            )
            return
        if self._transport is not None:
            _LOGGER.warning(
                t(
                    "client.ignoring_connect_already_connected",
                    "Ignoring connect(): already connected to %s",
                ),
                self.cfg_host,
            )
            return
        if self._connecting:
            _LOGGER.warning(
                t(
                    "client.ignoring_connect_connection_attempt_already",
                    "Ignoring connect(): a connection attempt is already in progress",
                )
            )
            return
        loop = self._get_loop()
        _LOGGER.info(
            t(
                "client.started_connect_power_pet_door",
                "Started to connect to Power Pet Door... at %s:%s",
            ),
            self.cfg_host,
            self.cfg_port,
        )
        self._connecting = True
        try:
            async with asyncio.timeout(self.cfg_timeout):
                # Each attempt gets its own protocol shim so a transport this
                # client does not adopt cannot drive its connection state; see
                # _ConnectionAttempt.
                await loop.create_connection(
                    lambda: _ConnectionAttempt(self), self.cfg_host, self.cfg_port
                )
        except (OSError, TimeoutError, ValueError, OverflowError) as err:
            _LOGGER.error(
                t(
                    "client.unable_connect_power_pet_door_1",
                    "Unable to connect to Power Pet Door at %s:%s: %s",
                ),
                self.cfg_host,
                self.cfg_port,
                err,
            )
            self.handle_connect_failure()
        finally:
            self._connecting = False

    def connection_made(self, transport) -> None:
        """asyncio callback for a successful connection.

        ``PowerPetDoorClient`` is a public ``asyncio.Protocol``, so it may
        be handed to ``create_connection()`` directly. :meth:`connect`
        does not do that - it wires a :class:`_ConnectionAttempt` shim
        instead, which knows its own transport.
        """
        self._adopt_transport(transport)

    def _adopt_transport(self, transport) -> bool:
        """Take ownership of ``transport``, or decline and abort it.

        Returns:
            True if the transport was adopted and now drives client state.
        """
        if self._shutdown:
            # shutdown()/stop() landed while create_connection() was still in
            # flight. disconnect() already ran, so nothing holds a reference
            # that would ever close this socket - abandon it here instead of
            # leaving a "shut down" client keepalive-pinging the device.
            _LOGGER.info(
                t(
                    "client.discarding_connection_completed_after_shutdown",
                    "Discarding a connection that completed after shutdown",
                )
            )
            transport.abort()
            return False
        if self._transport is not None:
            # Belt and braces behind connect()'s guard: never let a second
            # transport orphan the live one.
            _LOGGER.warning(
                t(
                    "client.rejecting_second_connection_one_already",
                    "Rejecting a second connection; one is already established",
                )
            )
            transport.abort()
            return False
        _LOGGER.info(t("client.connection_successful", "Connection Successful!"))
        self._transport = transport
        self._was_connected = True
        self._reconnect_attempts = 0

        if self.cfg_keepalive:
            # No ping here. Connecting is followed by a refresh (see
            # PowerPetDoor._on_connect), and every reply is timed, so a
            # ping would duplicate a measurement already being taken. A
            # caller that connects and sends nothing gets one from this
            # timer instead.
            self._keepalive = self._track_task(self.keepalive())

        # Flush anything that was enqueued while disconnected, otherwise
        # open the dequeue gate for the next enqueue.
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
        ``create_connection()`` directly. :class:`_ConnectionAttempt` never
        comes through here: it knows its own transport and calls
        :meth:`_on_transport_lost` after checking identity.
        """
        self._on_transport_lost(exc)

    def _on_transport_lost(self, exc) -> None:
        """Handle the loss of the transport that was driving client state."""
        if not self._was_connected:
            # A local teardown already ran - an explicit disconnect(), a
            # keepalive give-up, a write failure or an overflow drop. The
            # cleanup is done and whatever reconnect those paths wanted is
            # already scheduled; asyncio simply delivers the socket's loss a
            # loop iteration later. Reporting a server-side close nobody saw
            # and burning a second reconnect would be wrong.
            _LOGGER.debug(
                t(
                    "client.ignoring_connection_lost_already_closed",
                    "Ignoring connection_lost() for an already-closed connection",
                )
            )
            return
        self.disconnect()
        if not self._shutdown:
            _LOGGER.error(
                t(
                    "client.server_closed_connection_reconnecting",
                    "The server closed the connection. Reconnecting...",
                )
            )
            self._schedule_reconnect()

    def _drop_connection(self) -> None:
        """Tear down a connection that failed on this side, then reconnect.

        The reconnect used to be an implicit side effect of the
        ``connection_lost()`` that ``disconnect()`` provokes. Scheduling it
        here makes the intent explicit, which is what lets
        :meth:`_on_transport_lost` ignore the trailing loss event instead of
        treating it as a fresh server-side close.
        """
        self.disconnect()
        if not self._shutdown:
            self._schedule_reconnect()

    def _next_reconnect_delay(self) -> float:
        """Compute the next reconnect delay.

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
        # escapes reconnect() is logged immediately.
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
        fire only if a connection actually existed.
        """
        was_connected = self._was_connected
        self._was_connected = False
        if was_connected:
            _LOGGER.debug(
                t("client.closing_connection_server", "Closing connection with server...")
            )
        self._can_dequeue = False
        self._last_ping = None
        self._last_command = None
        self._last_reply = 0.0
        self._failed_msg = 0
        self._failed_pings = 0
        self._inflight_msg_id = None
        self._sent_at.clear()
        self._scanner.reset()
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

        # Cancel fire-and-forget work still in flight. The caller's own task
        # is skipped: disconnect() is reachable from inside one (a failed
        # write), and self-cancelling it there would surface as a spurious
        # CancelledError in unrelated code.
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
            _LOGGER.error(
                t(
                    "client.unable_connect_power_pet_door",
                    "Unable to connect to power pet door. Reconnecting...",
                )
            )
            self.disconnect()
            self._schedule_reconnect()

    async def keepalive(self) -> None:
        """Ping once the link has been quiet for ``cfg_keepalive`` seconds.

        An **idle** timer, not a heartbeat: :meth:`_write_message`
        reschedules it on every send, so a PING only goes out when nothing
        else has. Traffic that is being answered already proves the
        connection is alive, and - since
        :meth:`_record_response_latency` times those answers - already
        reports latency too, so a ping on a busy link would be a frame
        spent to learn nothing.
        """
        _keepalive = self._keepalive
        await asyncio.sleep(self.cfg_keepalive)
        if _keepalive and not _keepalive.cancelled():
            self._ping_once()

    def _ping_once(self) -> bool:
        """Send one PING, first accounting for any that went unanswered.

        Returns False when the connection has been dropped for repeated
        silence.
        """
        if self._last_ping is not None:
            self._failed_pings += 1
            if self._failed_pings < MAX_FAILED_PINGS:
                _LOGGER.warning(
                    t("client.last_ping_responded", "Last PING not responded to %d of %d..."),
                    self._failed_pings,
                    MAX_FAILED_PINGS,
                )
            else:
                _LOGGER.error(
                    t(
                        "client.last_ping_responded_times",
                        "Last PING not responded to %d times.",
                    ),
                    self._failed_pings,
                )
                self._drop_connection()
                return False

        # The wire token stays wall-clock milliseconds (device
        # compatibility); latency is measured against the monotonic
        # clock so NTP steps cannot skew it.
        self._last_ping = str(round(time.time() * 1000))
        self._last_ping_time = time.monotonic()
        self.send_message(PING, self._last_ping)
        return True

    def _record_response_latency(self, reply_msg_id: object) -> None:
        """Report the round-trip of an ordinary command, as a PONG would.

        Latency used to come only from the keepalive PING, which meant it
        described a synthetic frame sent when the link was quiet rather
        than the real work. Every successful reply carries the `msgID` of
        its request, so the round-trip is already there to be read.

        Only *successful* replies, necessarily: **[V]** a failure response
        carries no `msgID` at all (see docs/protocol.md), so there is
        nothing to pair it with. A `PONG` carries none either and is timed
        separately, against the token it echoes.
        """
        if not isinstance(reply_msg_id, (int, str)):
            return
        sent_at = self._sent_at.pop(reply_msg_id, None)
        if sent_at is None:
            return
        # Anything older than the reply we just paired is unanswerable:
        # the device answers in order, so a still-pending earlier id was
        # answered with a failure (which carries no msgID) or not at all.
        self._sent_at = {k: v for k, v in self._sent_at.items() if v > sent_at}
        self._notify_listeners(self.on_ping, round((time.monotonic() - sent_at) * 1000))

    def _fail_inflight_future(self, exc: Exception) -> None:
        """Fail the future paired with the in-flight message, if any."""
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
                    t(
                        "client.did_receive_response_message_more",
                        "Did not receive a response to a %s message in more than %s seconds, retrying.",
                    ),
                    self._last_command,
                    self.cfg_timeout,
                )
            else:
                _LOGGER.error(
                    t(
                        "client.did_receive_response_message_more_1",
                        "Did not receive a response to a %s message in more than %s seconds %s times, dropped.",
                    ),
                    self._last_command,
                    self.cfg_timeout,
                    self._failed_msg,
                )
                self._failed_msg = 0
                # Fail fast: the documented `await future` pattern must not
                # hang forever on a dropped message.
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
            _LOGGER.warning(
                t(
                    "client.attempted_write_stream_without_connection",
                    "Attempted to write to the stream without a connection active",
                )
            )
            return

        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        try:
            # Quiet time since the door last spoke, not since we last
            # sent. Sends are serialized behind a reply already, so a
            # send-relative floor is satisfied by any round trip longer
            # than itself - it stops contributing precisely when the door
            # is slow, which is when it drops requests.
            diff = time.monotonic() - self._last_reply
            if diff < MINIMUM_TIME_BETWEEN_MSGS:
                await asyncio.sleep(MINIMUM_TIME_BETWEEN_MSGS - diff)
            # Re-check after the sleep: disconnect() may have run in the
            # meantime and cleared the transport.
            transport = self._transport
            if transport is None or transport.is_closing():
                _LOGGER.warning(
                    t(
                        "client.connection_closed_while_waiting_send",
                        "Connection closed while waiting to send; dropping message",
                    )
                )
                return
            _LOGGER.debug(t("client.tx", "TX > %s"), rawdata)
            transport.write(rawdata)

            if self.cfg_keepalive:
                self._keepalive = self._track_task(self.keepalive())

            if self._last_command:
                self._check_receipt = self._track_task(self.check_receipt(rawdata))
            else:
                await self.dequeue_data()

        except (RuntimeError, OSError) as err:
            _LOGGER.error(
                t("client.failed_write_stream", "Failed to write to the stream. (%s)"), err
            )
            self._drop_connection()

    async def dequeue_data(self) -> None:
        """Send the next queued message, if the connection is idle."""
        if not self._transport:
            _LOGGER.warning(
                t(
                    "client.attempted_write_stream_without_connection",
                    "Attempted to write to the stream without a connection active",
                )
            )
            return

        if self._check_receipt:
            _LOGGER.warning(
                t(
                    "client.attempted_send_data_while_another",
                    "Attempted to send data while another message is still outstanding",
                )
            )
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
            _LOGGER.warning(
                t("client.sending_unknown_command_type", "Sending unknown command type")
            )
            self._last_command = None

        # Track the in-flight msgId so check_receipt can fail its future
        # if the message is ultimately dropped.
        self._inflight_msg_id = data.get(FIELD_MSG_ID)
        msg_id = data.get(FIELD_MSG_ID)
        if isinstance(msg_id, (int, str)):
            self._sent_at[msg_id] = time.monotonic()
        self._failed_msg = 0
        rawdata = json.dumps(data).encode("ascii")
        await self._send_data(rawdata)

    def data_received(self, data) -> None:
        """asyncio callback for any data received from the power pet door.

        All received bytes are untrusted. Non-ASCII bytes are escaped
        rather than dropped, so a single bad byte corrupts only the frame
        that contains it instead of desynchronizing the stream. The stream
        is then framed by the shared scanner (:mod:`powerpetdoor.framing`):
        garbage is discarded with a warning, malformed JSON frames are
        logged and skipped, and exceeding the un-parsed buffer cap drops
        the connection. This callback never raises on arbitrary input.
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
            _LOGGER.error(
                t(
                    "client.received_non_ascii_bytes_device",
                    "Received non-ASCII bytes from device; escaped them (affected frames are dropped)",
                )
            )

        # Device bytes reach an operator's terminal through the host
        # application's log (`tail -f`, `journalctl`, `docker logs`), so ESC
        # and friends must be escaped before they get there. Guarded because
        # sanitize_text is ~20x the cost of the suppressed debug call.
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(t("client.rx", "RX < %s"), sanitize_text(decoded))
        frames, diag = self._scanner.feed(decoded)

        if diag.overflow:
            # Checked before dispatch, not after: _drop_connection() ->
            # disconnect() cancels every task in `_tasks`, so frames
            # dispatched first would be created and then killed before they
            # ran - a complete, legitimate frame vanishing with nothing in
            # the log to say so. Report the loss instead of hiding it.
            _LOGGER.error(
                t(
                    "client.receive_buffer_exceeded_bytes_without",
                    "Receive buffer exceeded %d bytes without a complete message; disconnecting (discarding %d complete frame(s) received in the same read)",
                ),
                MAX_BUFFER_SIZE,
                len(frames),
            )
            self._drop_connection()
            return

        # The door has just transmitted, so the quiet period starts here.
        self._last_reply = time.monotonic()

        for frame in frames:
            try:
                msg = json.loads(frame)
            except (ValueError, RecursionError) as err:
                # `json.JSONDecodeError` is a *subclass* of ValueError, not
                # a superset of what json.loads raises. Two shapes well
                # inside every declared bound escape it: an integer literal
                # over `sys.get_int_max_str_digits()` (4300) digits raises a
                # bare ValueError, and deep nesting raises RecursionError.
                # Both are brace-balanced and under MAX_BUFFER_SIZE, so
                # FrameScanner frames them and hands them straight here -
                # and letting either escape kills the transport from
                # `data_received`, which is a hot reconnect loop because
                # every attempt connects before dying and resets the
                # backoff.
                _LOGGER.error(
                    t("client.failed_decode_json_frame", "Failed to decode JSON frame (%s): %s"),
                    err,
                    sanitize_field(frame, MAX_LOGGED_LENGTH),
                )
                continue
            self._track_task(self.process_message(msg))

    async def process_message(self, msg) -> None:
        """Process an incoming message from the device.

        Uses the ResponseHandlerRegistry to dispatch to the appropriate
        handler. All network data is untrusted: envelope fields are read
        defensively, handler dispatch is isolated so a malformed payload
        cannot kill the receive path, and a future paired with the message
        is always completed (result or :class:`CommandError`) rather than
        left hanging.

        Args:
            msg: The decoded message.
        """
        if not isinstance(msg, dict):
            _LOGGER.warning(
                t(
                    "client.ignoring_non_object_message_device",
                    "Ignoring non-object message from device: %r",
                ),
                msg,
            )
            return

        cmd = msg.get(FIELD_CMD)
        success = msg.get(FIELD_SUCCESS)
        if cmd is None and success is None:
            _LOGGER.warning(
                t(
                    "client.ignoring_malformed_message_device",
                    "Ignoring malformed message from device: %s",
                ),
                sanitize_text(json.dumps(msg), MAX_LOGGED_LENGTH),
            )
            return

        future = None
        reply_msg_id = msg.get(FIELD_MSG_ID_RESPONSE)
        if reply_msg_id is not None:
            self.replyMsgId = reply_msg_id
            self._record_response_latency(reply_msg_id)
            # The device supplies this; anything that is not a usable dict
            # key (a list, a dict, ...) must not raise here - it simply
            # matches no outstanding future.
            if isinstance(reply_msg_id, (int, str)):
                outstanding = self._outstanding.get(reply_msg_id)
                if outstanding is not None and not outstanding.done():
                    future = outstanding
            else:
                _LOGGER.warning(
                    t(
                        "client.ignoring_unusable_msgid_device_response",
                        "Ignoring unusable msgID %s in device response; no future to resolve",
                    ),
                    sanitize_field(reply_msg_id, MAX_LOGGED_LENGTH),
                )

        # Acknowledge the in-flight command so the retry timer stops, but
        # defer dequeuing the next message until after the handler has run.
        # Dequeuing awaits (rate-limit sleep), and completing the future
        # only after that await races with caller-side wait_for timeouts.
        dequeue = False
        acknowledged_inflight = False
        if cmd is not None and cmd == self._last_command:
            if self._check_receipt:
                self._check_receipt.cancel()
                self._check_receipt = None
                dequeue = True
                acknowledged_inflight = True
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
                        _LOGGER.exception(
                            t("client.error_handling_response", "Error handling %s response: %s"),
                            cmd,
                            sanitize_text(json.dumps(msg), MAX_LOGGED_LENGTH),
                        )
                        if future is not None and not future.done():
                            future.set_exception(CommandError(cmd, "Malformed response"))
                    # A handler that ran but could not resolve its future
                    # means the payload lacked the expected field. Fail it
                    # typed - never cancel() an API caller's future.
                    if future is not None and not future.done():
                        future.set_exception(CommandError(cmd, "Response missing expected field"))
                elif future is not None and not future.done():
                    # Successful response with no specialized handler: the
                    # acknowledgment itself is the result.
                    future.set_result(msg)
            else:
                reason = msg.get(FIELD_REASON)
                _LOGGER.warning(
                    t("client.error_reported_by_device", "Error reported by device: %s"),
                    sanitize_text(json.dumps(msg), MAX_LOGGED_LENGTH),
                )
                if future is not None and not future.done():
                    future.set_exception(CommandError(cmd, reason))
                elif reply_msg_id is None and acknowledged_inflight:
                    # **Failure responses carry no msgID at all**, so they
                    # cannot be paired with
                    # a request by id. Cancelling the retry timer (above)
                    # without failing the future left the caller's `await`
                    # hanging until its own timeout - or forever, for a
                    # message-level caller that awaits the future directly.
                    # The device answers one command at a time and this
                    # response acknowledged the in-flight one, so it is that
                    # command's failure.
                    self._fail_inflight_future(CommandError(cmd, reason))
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
    reaches the client's callbacks.
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
            # client state, so there is nothing to tear down.
            return
        current = self._client._transport
        if current is not None and current is not self._transport:
            # disconnect() dropped this socket and a newer connection has
            # since been established; this event is stale.
            _LOGGER.debug(
                t(
                    "client.ignoring_connection_lost_superseded_transport",
                    "Ignoring connection_lost() from a superseded transport",
                )
            )
            return
        # Goes straight to the client's teardown: the identity checks above
        # are this shim's equivalent of the counting the public
        # connection_lost() has to do, and running both would double-count.
        self._client._on_transport_lost(exc)

    def data_received(self, data: bytes) -> None:
        """Forward received bytes, unless this transport was declined."""
        if self._adopted:
            self._client.data_received(data)
