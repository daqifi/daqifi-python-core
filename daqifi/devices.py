"""DAQiFi device classes: DaqifiDevice, DaqifiStreamingDevice, DaqifiDeviceFactory.

Ports the public Device classes from ``Daqifi.Core/Device/`` (upstream
e8d84af). Distinct from :class:`daqifi.device.NyquistDevice`, which is
a higher-level user-facing API; this module provides the lower-level
transport+protocol-driven device abstraction the C# library is built
around.

Threading model: all I/O is synchronous (project convention). The
underlying :class:`StreamMessageConsumer` runs a background daemon
thread that delivers parsed protobuf messages via callback.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import queue
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from itertools import takewhile
from typing import IO, Any, Callable, Iterator, List, Optional, Sequence, Tuple

_log = logging.getLogger(__name__)

from ._internal.comm import (
    ConnectionRetryOptions,
    DeviceMetadata,
    DeviceStatusEventArgs,
    GenericInboundMessage,
    HidDeviceInfo,
    MessageReceivedEventArgs,
    OutboundMessage,
    ScpiMessage,
    TransportStatusEventArgs,
)
from ._internal.scpi import ChannelDirection, ConnectionStatus, DeviceState, StreamInterface
from ._internal import scpi_commands
from .channels import ChannelsPopulatedEventArgs
from .communication import (
    LineBasedMessageParser,
    ProtobufMessageParser,
    StreamMessageConsumer,
)
from .enums import SdCardLogFormat, WifiMode, WifiSecurityType
from .exceptions import (
    DeliveryFaultError,
    ExchangeTimeoutError,
    DeviceNotConnectedError,
    DeviceUnresponsiveError,
    FeatureNotSupportedError,
)
from .network import NetworkConfiguration
from .protocol import ProtobufProtocolHandler
from .firmware.version import FirmwareVersion
from .sdcard import _write_all
from .sdcard import (
    _MARKER_TRAILER_MAX,
    _SizedTransfer,
    _is_marker_trailer,
)
from .sdcard import (
    END_OF_FILE_MARKER,
    TRANSFER_ERROR_MARKER,
    SdCardDownloadResult,
    SdCardFileInfo,
    SdCardTransferProgress,
    check_sd_card_listing,
    parse_sd_card_file_list,
)
from .transport import (
    HidLibraryTransport,
    SerialStreamTransport,
    TcpStreamTransport,
    UdpTransport,
)


__all__ = [
    "AnalogChannel",
    "DigitalChannel",
    "DaqifiDevice",
    "DaqifiStreamingDevice",
    "DaqifiDeviceFactory",
]


# Lightweight data-only channel records populated from protobuf status.
# The `daqifi.channels` module's classes wrap a SerialInterface for
# user-facing read/write — different shape entirely. These ports of the
# upstream `AnalogChannel` / `DigitalChannel` exist to mirror the data
# the device exposes, not to drive I/O.

from dataclasses import dataclass, replace


@dataclass
class AnalogChannel:
    """Port of upstream ``AnalogChannel`` — analog-input channel metadata."""
    index: int
    resolution: int = 65535
    name: str = ""
    direction: int = 0  # ChannelDirection.Input
    is_enabled: bool = False
    calibration_b: float = 0.0
    calibration_m: float = 1.0
    internal_scale_m: float = 1.0
    port_range: float = 1.0


@dataclass
class DigitalChannel:
    """Port of upstream ``DigitalChannel`` — digital I/O channel metadata."""
    index: int
    name: str = ""
    direction: int = 0  # ChannelDirection.Input
    is_enabled: bool = False


# Filename hygiene shared by SD-card mutating ops. Quote / newline /
# semicolon mirror the C# ``DaqifiStreamingDevice.ValidateSdCardFileName``
# guard (would let a caller smuggle SCPI tokens). Forward + back slash
# are added on the Python side to block path-traversal attempts that
# the firmware's path parser might honor — defense-in-depth, the C#
# port doesn't bother because callers there always go through a
# higher-level API.
_SD_FORBIDDEN_FILENAME_CHARS = ('"', '\n', '\r', ';', '/', '\\')


def _validate_sd_card_file_name(file_name: str) -> None:
    if file_name is None or not file_name.strip():
        raise ValueError("file_name must not be empty.")
    if file_name != file_name.strip():
        raise ValueError(
            "file_name must not have leading or trailing whitespace."
        )
    if ".." in file_name:
        raise ValueError(
            "file_name must not contain path-traversal segments ('..')."
        )
    if any(ch in file_name for ch in _SD_FORBIDDEN_FILENAME_CHARS):
        raise ValueError(
            "file_name contains invalid characters. Quotes, newlines, "
            "semicolons, and path separators are not allowed."
        )


_DEFAULT_BAUD_RATE = 9600


def _default_message_factory(payload: bytes) -> Any:
    """Decode a wire-format `DaqifiOutMessage` payload.

    Lazily imports the generated protobuf module so consumers that
    don't need protobuf decoding (tests using fake messages, callers
    that inject their own factory) don't pay the import cost.

    Parses ``bytes`` into a real :class:`DaqifiOutMessage` instance —
    this is what populates :attr:`DaqifiDevice.metadata` after
    :meth:`DaqifiDevice.initialize`. Replaces the previous
    ``lambda b: b`` passthrough that left metadata empty on hardware.
    """
    from ._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
    return DaqifiOutMessage.FromString(payload)


# ---- DaqifiDevice ------------------------------------------------------------



class _LifecycleGate:
    """Serializes connect() against disconnect() on one device.

    Port of upstream daqifi-core ``LifecycleGate``. Two threads must never
    drive the transport at once: both would find no consumer, both would
    start one, and two readers on one stream corrupt its framing. So a
    lifecycle call that finds the gate taken does not run alongside: a
    connect waits ``connect_timeout`` and then raises (nothing was opened;
    the caller can retry), a disconnect waits ``teardown_timeout`` and then
    gives up (reported to the caller; the holder is left alone). Re-entry
    from the same thread (a status handler calling disconnect() from inside
    connect()) runs nested, as it always has.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder = threading.local()

    def run(self, operation: Callable[[], None], timeout: float, fail: bool) -> bool:
        if getattr(self._holder, "inside", False):
            operation()
            return True
        if not self._lock.acquire(timeout=timeout):
            if fail:
                raise TimeoutError(
                    f"Could not start connecting within {timeout:.1f}s because "
                    "another connect or disconnect was still in progress. "
                    "Nothing was opened; retry once it has finished."
                )
            _log.error(
                "Could not take the connect/disconnect lock within %.1fs; "
                "nothing was torn down (a connect is likely still in progress).",
                timeout,
            )
            return False
        self._holder.inside = True
        try:
            operation()
        finally:
            self._holder.inside = False
            self._lock.release()
        return True

# What may follow a terminal marker at the end of a transfer (line endings
# and the console prompt); shared with the SD receiver.
_EOF_TRAILER_MAX = _MARKER_TRAILER_MAX
_is_eof_trailer = _is_marker_trailer


def _eof_marker_start(buffer: bytes, marker: bytes = END_OF_FILE_MARKER) -> int:
    """Index of ``marker`` (by default the end-of-file marker) ending
    ``buffer`` apart from a trailer of line endings and prompt, or -1."""
    position = bytes(buffer).rfind(marker)
    while position >= 0:
        if _is_eof_trailer(bytes(buffer[position + len(marker):])):
            return position
        position = bytes(buffer).rfind(marker, 0, position)
    return -1


def _ends_with_eof_marker(buffer: bytes) -> bool:
    return _eof_marker_start(buffer) >= 0


def _strip_prompt_prefixes(lines: Sequence[str]) -> List[str]:
    """Drop a ``DAQIFI>`` prompt from the head of each reply line.

    The text-command path kept "DAQIFI> log_a.bin" whole, so the first file
    of a listing came out as "DAQIFI>" (#163). Same handling as the raw
    capture path: a prompt-only line is dropped, the rest of a line kept.
    """
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("DAQIFI>"):
            stripped = stripped[len("DAQIFI>"):].lstrip()
            if not stripped:
                continue
            out.append(stripped)
        else:
            out.append(line)
    return out


# drain_errors' sentinel code for "queue not confirmed clean".
_DRAIN_INCONCLUSIVE_CODE = 99999

# Firmware that ends every SD listing with __END_OF_LIST__ (#794).
_SD_LIST_MARKER_SINCE = FirmwareVersion(3, 8, 0)
# SD file operations over WiFi/TCP (firmware #598/#599).
_SD_OVER_WIFI_SINCE = FirmwareVersion(3, 7, 0)


# A SYSTem:ERRor? reply with its description quoted, as the firmware sends
# it: a listing line can't take this form by accident ('0,logs/b.bin 4').
_QUOTED_ERROR_REPLY = re.compile(r'^\s*[+-]?\d+\s*,\s*"')


def _split_at_list_terminator(
    lines: Sequence[str], marker_expected: bool
) -> Optional[Tuple[List[str], str]]:
    """``(listing, terminator)``, where the terminator is the reply to the
    ``SYSTem:ERRor?`` sent after ``SD:LIST?``; ``None`` when there is none.
    That means the capture timed out or was cut short, and what arrived
    can't be told from an empty card (#188).

    The terminator is the last line, and it must not be mistakable for a
    listing line. On firmware that marks its listings it must directly
    follow the end-of-listing marker. On older firmware it must have the
    quoted form the firmware sends.
    """
    from ._internal import scpi as _scpi
    from .sdcard import _list_end_status

    content = [line for line in lines if line and line.strip()]
    if not content:
        return None
    reply = _scpi.find_system_error_reply([content[-1]])
    if reply is None:
        return None
    if marker_expected:
        if len(content) < 2 or _list_end_status(content[-2]) is None:
            return None
    elif not _QUOTED_ERROR_REPLY.match(content[-1].strip()):
        return None
    return content[:-1], reply


class _SessionHealthSink:
    """Forwards a consumer's read outcomes to the transport, tagged with
    the connection the consumer reads (#191)."""

    def __init__(self, transport: Any, session: Optional[int]) -> None:
        self._transport = transport
        self._session = session

    def report_io_fault(self, error: BaseException) -> None:
        self._transport.report_io_fault(error, session=self._session)

    def report_io_success(self) -> None:
        self._transport.report_io_success(session=self._session)


def _exclusive_sd_operation(method):
    """Run an SD-card operation entirely inside the text-exchange lock.

    SD operations hand the shared SPI bus to the SD card, run one or more
    exchanges, and hand it back to LAN. Another exchange interleaved with
    that (a second SD call on another thread, say) switches the bus under
    it or reads its reply off the same stream: a listing consumed a
    download's end-of-file marker, so the download hung and its file was
    truncated (#164 item 6). Holding the lock across the bus switch and the
    restore, as upstream does since daqifi-core #406/#417, makes each SD
    operation one exchange. execute_text_command calls inside it run under
    the lock already held.

    This is :meth:`DaqifiDevice.run_exclusive` plus one refusal: an SD
    operation started from inside a text CAPTURE (from a ``setup_action``)
    would switch the SPI bus out from under the reader currently on the
    stream. Note what the condition is NOT: merely holding the exchange
    lock is fine, and is how ``run_exclusive`` groups an SD listing with
    other commands.
    """

    @functools.wraps(method)
    def wrapper(self: "DaqifiDevice", *args: Any, **kwargs: Any) -> Any:
        # The capture refusal lives in _run_exclusive's entry, which every
        # path reaches, rather than being repeated here: a copy beside the
        # call is what let the ownership fast path through.
        return self._run_exclusive(
            lambda: method(self, *args, **kwargs), method.__name__
        )

    return wrapper


class DaqifiDevice:
    #: How long :meth:`send` waits for another flow's exchange.
    #:
    #: ⛔ DERIVED FROM THE LONGEST LEGITIMATE HOLDER, not chosen for a
    #: caller's patience. An SD download may hold the device for
    #: ``DaqifiStreamingDevice._SD_DOWNLOAD_TIMEOUT_S``, so any shorter
    #: bound would turn a perfectly ordinary wait behind one into an error
    #: -- a regression dressed as a safety limit. Reaching this bound
    #: therefore means the holder is STUCK, not busy, which is the only
    #: thing a timeout here can usefully distinguish. Its job is to turn an
    #: unbounded hang into a clear error, not to cap a caller's patience.
    #:
    #: ⛔ IT LIVES HERE, on the class that defines :meth:`send`, not beside
    #: the download timeout it is derived from. It was first written next
    #: to that constant on the SUBCLASS, so every blocking send on a plain
    #: ``DaqifiDevice`` raised AttributeError -- found by the tests, not by
    #: reading. The two cannot be derived from one expression across a
    #: class boundary, so the relationship is asserted instead: see
    #: test_the_send_wait_covers_the_longest_exchange.
    _SEND_EXCHANGE_WAIT_S = 30 * 60 + 60

    """Base device implementation.

    Ports the C# ``DaqifiDevice`` (``Daqifi.Core/Device/DaqifiDevice.cs``).
    Owns a transport, message producer/consumer, and protocol handler.
    Satisfies the :class:`daqifi._internal.comm.Device` Protocol.

    Subclasses (e.g. :class:`DaqifiStreamingDevice`) layer
    domain-specific behaviour on top.
    """

    # How long connect()/disconnect() wait for one another (upstream
    # LifecycleLockTimeout / TeardownLockTimeout); see _LifecycleGate.
    lifecycle_lock_timeout: float = 10.0
    teardown_lock_timeout: float = 30.0

    def __init__(
        self,
        name: str,
        transport: Optional[Any] = None,
        ip_address: Optional[str] = None,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> None:
        self._name = name
        self._ip_address = ip_address
        self._transport = transport
        self._message_factory = message_factory or _default_message_factory
        self._status = ConnectionStatus.Disconnected
        self._state = DeviceState.Disconnected
        self._metadata = DeviceMetadata()
        self._channels: List[Any] = []
        self._timestamp_frequency: int = 0

        self._consumer: Optional[StreamMessageConsumer[Any]] = None
        self._protocol_handler: Optional[ProtobufProtocolHandler] = None
        self._is_initialized = False
        self._is_disconnecting = False
        self._disposed = False

        self._status_handlers: List[
            Callable[[DeviceStatusEventArgs], None]
        ] = []
        self._message_handlers: List[
            Callable[[MessageReceivedEventArgs[Any]], None]
        ] = []
        self._channels_populated_handlers: List[
            Callable[[ChannelsPopulatedEventArgs], None]
        ] = []
        # Serializes execute_text_command calls device-wide. Plain
        # Lock (not RLock) is intentional: re-entrant calls from
        # inside a setup_action callback would corrupt the consumer
        # swap. We additionally track the thread that currently holds
        # the lock so a same-thread re-entry surfaces as an explicit
        # RuntimeError rather than wedging the caller in a deadlock —
        # same safety guarantee (the bug never silently corrupts
        # state), strictly better failure mode for debugging.
        # Mirrors the fix expected upstream in daqifi-core#186.
        # That owner field now answers "does this thread hold the lock",
        # which two rules read: who may nest an exchange, and who must
        # defer a status event instead of delivering it.
        self._text_exchange_lock = threading.Lock()
        # Guards the consumer/session/status handoff between connect(),
        # disconnect() and the transport-loss handler, which runs on the
        # transport's reader thread. Held only for those few assignments:
        # never across a callback, a transport call or a thread join, so
        # status handlers can freely wait on other threads.
        self._state_lock = threading.Lock()
        self._lifecycle_gate = _LifecycleGate()
        self._transport_session: Optional[int] = None
        # Number of the most recently started connect(); only the newest
        # call may claim or publish the device's consumer, so an older one
        # overtaken by a nested reconnect (say, from a Lost handler) steps
        # aside even when both ended up on the same transport session.
        self._connect_generation = 0
        self._text_exchange_lock_owner: Optional[int] = None
        # The thread currently inside a CAPTURE, if any -- text or raw.
        # ⛔ PER THREAD, not a device-wide flag: _exclusive_sd_operation
        # reads this BEFORE taking the lock, so a plain flag made one
        # thread's capture refuse every other thread's SD operation, and
        # shadowed the consumer-thread refusal that should fire there.
        # ⛔ ONE marker for BOTH capture kinds, set only by _capture():
        # when only the text path set it, a raw SD download looked like no
        # capture at all and its own progress callback could start a
        # listing or a second download, which then read the outer
        # download's remaining bytes and its EOF marker off the stream.
        self._capture_owner: Optional[int] = None
        # Status events raised while the exchange lock was held, delivered
        # by whoever releases it (see _deliver_status_event).
        self._pending_status_events: List[DeviceStatusEventArgs] = []
        # Set when something the device was told to do provably did not
        # happen -- today only a consumer that could not be restarted after
        # a text exchange. Read by is_streaming / start_streaming so they
        # cannot report success over a dead pipeline.
        self._delivery_fault: Optional[str] = None
        # The connect generation the fault belongs to. A replay that fails
        # on a connection that has since been replaced must not condemn the
        # NEW one, which is live and healthy.
        self._delivery_fault_generation: Optional[int] = None

        if transport is not None:
            transport.add_status_changed(self._on_transport_status_changed)

    # ---- IDevice / Device Protocol surface --------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def ip_address(self) -> Optional[str]:
        return self._ip_address

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.Connected

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def state(self) -> DeviceState:
        return self._state

    @property
    def metadata(self) -> DeviceMetadata:
        return self._metadata

    @property
    def channels(self) -> List[Any]:
        # Read-only snapshot.
        return list(self._channels)

    @property
    def timestamp_frequency(self) -> int:
        return self._timestamp_frequency

    @property
    def transport(self) -> Optional[Any]:
        """The injected transport (read-only). ``None`` for stream-only init."""
        return self._transport

    # ---- event subscription -----------------------------------------------

    def add_status_changed(
        self, handler: Callable[[DeviceStatusEventArgs], None]
    ) -> None:
        self._status_handlers.append(handler)

    def remove_status_changed(
        self, handler: Callable[[DeviceStatusEventArgs], None]
    ) -> None:
        try:
            self._status_handlers.remove(handler)
        except ValueError:
            pass

    def add_message_received(
        self, handler: Callable[[MessageReceivedEventArgs[Any]], None]
    ) -> None:
        self._message_handlers.append(handler)

    def remove_message_received(
        self, handler: Callable[[MessageReceivedEventArgs[Any]], None]
    ) -> None:
        try:
            self._message_handlers.remove(handler)
        except ValueError:
            pass

    def add_channels_populated(
        self, handler: Callable[[ChannelsPopulatedEventArgs], None]
    ) -> None:
        self._channels_populated_handlers.append(handler)

    def remove_channels_populated(
        self, handler: Callable[[ChannelsPopulatedEventArgs], None]
    ) -> None:
        try:
            self._channels_populated_handlers.remove(handler)
        except ValueError:
            pass

    # ---- lifecycle --------------------------------------------------------

    def connect(
        self, retry_options: Optional[ConnectionRetryOptions] = None,
    ) -> None:
        """Open the transport and start the message consumer.

        Raises ``TimeoutError`` if another connect()/disconnect() is still
        in progress after ``lifecycle_lock_timeout`` seconds; nothing is
        opened in that case.
        """
        self._lifecycle_gate.run(
            lambda: self._connect_unsynchronized(retry_options),
            self.lifecycle_lock_timeout,
            fail=True,
        )

    def _connect_unsynchronized(
        self, retry_options: Optional[ConnectionRetryOptions] = None,
    ) -> None:
        # Accept retry_options so callers can request resilient
        # connection behavior here rather than only at the
        # transport layer (Qodo PR 17 pass 20 #1).
        self._raise_if_disposed()
        self._set_status(ConnectionStatus.Connecting)
        self._set_state(DeviceState.Connecting)
        # The transport session this call connected, once it has, and the
        # call's generation; together they tell this call's connection
        # from a newer one.
        connected_session: Optional[int] = None
        with self._state_lock:
            self._connect_generation += 1
            generation = self._connect_generation

        try:
            # Refuse to connect without a transport. Previously this
            # would silently transition to Connected with no stream,
            # which then crashed any subsequent send/recv (Qodo PR 17
            # pass 22 #2).
            if self._transport is None:
                raise RuntimeError(
                    "No transport configured; cannot connect without "
                    "a transport-backed stream."
                )
            self._transport.connect(retry_options=retry_options)
            session = getattr(self._transport, "session", None)
            connected_session = session
            # Capture the stream once and validate it. A transport
            # that returns None from `.stream` after a successful
            # `.connect()` is broken — surface it now rather than
            # letting StreamMessageConsumer crash later (Qodo PR
            # 17 pass 4 #6).
            stream = self._transport.stream
            if stream is None:
                raise RuntimeError(
                    "Transport reports connected but provided no "
                    "stream; cannot start message consumer."
                )
            # Always rebuild the consumer on connect: on a
            # reconnect, the previous consumer references the
            # OLD (closed) stream from the prior session, so its
            # background reads would fail forever (Qodo PR 17
            # pass 11 #1). The factory call is cheap; the
            # caller's `initialize()` re-registers handlers.
            # Claimed under the lock and torn down by reference: while it
            # stops, a status handler may already have run a nested
            # connect(), and its consumer must not be the one closed here.
            # Unregistering before stopping keeps an in-flight message from
            # reaching the old session (Qodo PR 17 pass 42 #1); stopping
            # before closing avoids a zombie reader (pass 29 #3).
            with self._state_lock:
                # Superseded already? A nested connect() may have replaced
                # this call's connection since transport.connect() above;
                # the consumer now installed is that newer one's.
                if (
                    generation != self._connect_generation
                    or getattr(self._transport, "session", None) != session
                ):
                    return
                old_consumer = self._consumer
                self._consumer = None
            if old_consumer is not None:
                self._discard_consumer(old_consumer)

            # Default factory decodes bytes into a real
            # `DaqifiOutMessage` (see ``_default_message_factory``).
            # Override per-device by passing ``message_factory``
            # to the constructor — useful for tests that want
            # raw-bytes passthrough or a custom proto class.
            consumer = StreamMessageConsumer[Any](
                stream,
                ProtobufMessageParser(message_factory=self._message_factory),
                health_sink=self._transport_health_sink(session),
            )
            with self._state_lock:
                superseded = (
                    generation != self._connect_generation
                    or self._consumer is not None
                    or getattr(self._transport, "session", None) != session
                )
                if not superseded:
                    self._consumer = consumer
                    self._transport_session = session
                    # A fresh consumer has no message handlers yet; the
                    # caller's initialize() must register them again.
                    self._is_initialized = False
                    # A new connection with a fresh reader is exactly the
                    # recovery a delivery fault asks for, so clear it here
                    # -- and only here, so the fault cannot be lost by
                    # anything short of actually reconnecting.
                    self._delivery_fault = None
                    self._delivery_fault_generation = None
            if superseded:
                # A nested connect() (from a status handler, during the
                # teardown above) already established a newer connection;
                # this call's stream belongs to the one it replaced.
                try:
                    consumer.close()
                except Exception:
                    pass
                return
            try:
                consumer.start()
            except Exception:
                # The loss handler (a delayed loss of the stream reused
                # above) may have disposed this consumer before it
                # started; whatever replaced it is not ours to undo.
                with self._state_lock:
                    taken_over = self._consumer is not consumer
                if taken_over:
                    return
                raise

            # Only if this connection is still the current one, and its
            # stream still alive: the loss handler (on the reader thread)
            # may already have closed it, or the peer may have closed a
            # stream reused from the previous connect(), and "Connected"
            # must not overwrite that. Status and state change together.
            if not self._set_status(
                ConnectionStatus.Connected,
                if_consumer=consumer,
                if_stream_alive=stream,
                state=DeviceState.Connected,
            ):
                self._discard_consumer(consumer)
                with self._state_lock:
                    if self._consumer is consumer:
                        self._consumer = None
                        self._transport_session = None
                        evt = self._change_status(ConnectionStatus.Lost)
                        self._state = DeviceState.Error
                    else:
                        evt = None
                if evt is not None:
                    self._deliver_status_event(evt)
        except Exception:
            # Setup failed mid-way. Tear down anything partially
            # constructed so the transport doesn't leak file
            # descriptors and the consumer thread doesn't keep
            # spinning on a dead stream (Qodo PR 17 pass 13 #2).
            # Unless a newer connection exists (a nested connect() from a
            # status handler): that one is not this call's to tear down.
            with self._state_lock:
                superseded = generation != self._connect_generation or (
                    connected_session is not None
                    and getattr(self._transport, "session", None) != connected_session
                )
                stale_consumer = None if superseded else self._consumer
                if stale_consumer is not None:
                    self._consumer = None
            if superseded:
                raise
            if stale_consumer is not None:
                # close() alone doesn't join the daemon thread, so
                # stop it first — same pattern as disconnect() and
                # the reconnect path (Qodo PR 17 pass 34 #1).
                self._discard_consumer(stale_consumer)
            if self._transport is not None:
                try:
                    self._transport.disconnect()
                except Exception:
                    pass
            self._set_status(ConnectionStatus.Disconnected)
            self._set_state(DeviceState.Disconnected)
            raise

    def disconnect(self) -> None:
        """Stop the consumer and close the transport.

        If another connect()/disconnect() still holds the device after
        ``teardown_lock_timeout`` seconds, nothing is torn down and the
        device is only marked Disconnected (the holder is left alone).
        """
        if not self._lifecycle_gate.run(
            self._disconnect_unsynchronized, self.teardown_lock_timeout, fail=False
        ):
            self._set_status(ConnectionStatus.Disconnected)

    def _disconnect_unsynchronized(self) -> None:
        with self._state_lock:
            self._is_disconnecting = True
            # A loss event still in flight for this connection must not
            # match any later one.
            self._transport_session = None
        # Hoisted out of `try` so the `finally` block can branch on
        # whether the transport failed (Qodo PR 17 pass 26 #2).
        transport_error: Optional[Exception] = None
        try:
            if self._consumer is not None:
                # Wrap unsubscription so a buggy handler-list raises
                # don't skip the transport disconnect + consumer
                # teardown below (Qodo PR 17 pass 49 #1).
                try:
                    self._consumer.remove_message_received(self._on_inbound_message)
                except Exception:
                    pass

            # Disconnect the transport BEFORE stopping the consumer
            # so the underlying socket/stream close wakes any blocked
            # read in the consumer thread (Qodo PR 17 pass 10 #1).
            # Without this ordering, stop_safely() could time out
            # waiting for a thread blocked in stream.read().
            # Capture any failure so we still run consumer cleanup
            # below (Qodo PR 17 pass 16 #1) — without this, a
            # transport.disconnect() raise leaked the daemon thread.
            if self._transport is not None:
                try:
                    self._transport.disconnect()
                except Exception as exc:
                    # Catch Exception (not BaseException) so signals
                    # like KeyboardInterrupt / SystemExit still
                    # propagate immediately rather than being
                    # captured and re-raised after consumer cleanup
                    # (Qodo PR 17 pass 45 #1).
                    transport_error = exc

            if self._consumer is not None:
                # If stop_safely times out (consumer thread blocked
                # in a read), fall through to the harder stop() —
                # otherwise the daemon thread leaks until process
                # exit (Qodo PR 17 pass 9 #1).
                stopped = self._consumer.stop_safely()
                if stopped is False:
                    self._consumer.stop()
                # Explicitly close + clear so a stale consumer
                # doesn't linger; the next connect() rebuilds it
                # against the fresh stream (Qodo PR 17 pass 15 #1).
                try:
                    self._consumer.close()
                except Exception:
                    pass
                self._consumer = None

            # Re-surface the transport error AFTER consumer cleanup.
            if transport_error is not None:
                raise transport_error
        finally:
            # If the transport raised, leave the device in Lost/Error
            # so observers can distinguish a clean disconnect from a
            # forced one (Qodo PR 17 pass 26 #2).
            if transport_error is None:
                self._set_status(ConnectionStatus.Disconnected)
                self._set_state(DeviceState.Disconnected)
            else:
                self._set_status(ConnectionStatus.Lost)
                self._set_state(DeviceState.Error)
            self._is_initialized = False
            self._is_disconnecting = False

    def send(self, message: Any) -> None:
        """Send an outbound message.

        Mirrors the C# ``Send<T>(IOutboundMessage<T>)`` -- only
        string-typed messages are dispatched today (the rest require a
        message-producer abstraction not yet ported); other types raise
        :class:`NotImplementedError` to match upstream behaviour.

        Serialized against text exchanges (#225): while another flow runs
        one -- :meth:`execute_text_command`, :meth:`drain_errors`,
        :meth:`run_exclusive`, an SD listing or download -- this WAITS for
        it, so the command lands after that exchange rather than in the
        middle of it, where the exchange would read this command's reply as
        its own or the device would act on it between the steps of a
        sequence.

        **Returning means the bytes are on the wire.** That is the contract
        an earlier revision of this method broke by parking the command and
        replaying it later: callers that pair a send with a state change or
        a deadline all assume delivery, and every one of them had to be
        rewritten to cope. Blocking keeps the assumption true.

        Two bounds make the wait safe rather than open-ended:

        * **From any ordinary thread** it waits at most
          :attr:`_SEND_EXCHANGE_WAIT_S` and then raises
          :class:`ExchangeTimeoutError`. The bound is derived from the
          longest exchange that may legitimately hold the device, so
          reaching it means the holder is stuck rather than busy.
        * **From the message-received (consumer) thread** it does not wait
          at all: if the exchange is held it raises immediately. A send
          there could otherwise be waiting on a flow that is itself waiting
          for this very thread to stop the consumer, which is how the
          consumer ended up dead for good (#238). Refusing is the honest
          answer -- the command did not go out, and the caller is told so
          rather than being blocked or silently deferred.

        ⛔ THE CONSUMER THREAD IS NOT EXEMPT FROM SERIALIZATION, only from
        waiting. It used to send straight out from here, which let a
        message callback's command land inside a :meth:`run_exclusive`
        group (``MAXSize 1024``, the callback's ``MAXSize 2048``,
        ``MAXSize?``) and the group's own query read the callback's value
        back. Taking the lock when it is free and refusing when it is not
        closes that without ever blocking this thread.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError()
        tid = threading.get_ident()
        if self._text_exchange_lock_owner == tid:
            # Ours already: the exchange's own setup_action is what sends.
            self._write_message(message)
            return
        # ⭐ ONE acquisition rule for every write-only exchange, shared
        # with _run_owning_exchange so the two cannot drift: the consumer
        # thread try-acquires and is refused only when the exchange is
        # HELD; any other thread waits, bounded. The helper also records
        # the owner -- two rules read that (who may nest an exchange, and
        # who must defer status events) -- and releases and flushes on the
        # way out.
        with self._write_exchange_lock("send"):
            # ⛔ RE-CHECK AFTER ACQUIRING, not only on the way in. Waiting
            # here can take as long as the exchange ahead of us, and that
            # exchange may be what loses the connection: a send that
            # validated only on the way in then put its command on a link
            # already marked Lost, leaving the device Lost and Streaming at
            # once. Parking is gone but the DELAY is not, so neither is
            # this check -- any mechanism that makes a send wait moves it
            # past the checks it passed on the way in.
            #
            # ⚠ NOT `not self.is_connected`. That also catches Connecting,
            # and a send that captured the old stream while a reconnect is
            # installing the new one is DESIGNED to go ahead and fail
            # harmlessly: _shutting_down_since then spares the reconnect
            # rather than marking it Lost (test_device_lifecycle's
            # ..._spares_the_reconnect pins that, and a blunt is_connected
            # re-check broke it). What must stop a waiting send is a link
            # that is gone with nothing newer coming.
            if self._link_is_gone():
                raise DeviceNotConnectedError(
                    "The message could not be sent: the connection was lost "
                    "while it waited for another exchange to finish.",
                    is_shutting_down=bool(
                        self._disposed or self._is_disconnecting
                    ),
                )
            # ⛔ THE SAME RULE, APPLIED TO THE FAULT CHECK. The exchange we
            # just waited for may be the one that failed to restart the
            # consumer, and it records that fault on its way out -- after
            # we passed start_streaming()'s check and before we got the
            # lock. Without this the START went out and the API reported
            # streaming over a stream nothing was reading.
            #
            # It is HERE rather than in each caller because the race is not
            # start_streaming's: it belongs to the wait, so it is fixed
            # where the wait is. It is deliberately NOT on the
            # already-owns-the-exchange fast path above: that path does not
            # wait, so nothing can have changed under it, and adding it
            # there would refuse an exchange's own setup_action for a fault
            # that predates it.
            self._raise_if_delivery_faulted("send")
            self._write_message(message)

    def _write_message(self, message: Any) -> None:
        """Put ``message`` on the wire. :meth:`send`'s body, once the
        serialization question is settled.

        Named for what it does rather than for the lock state, because the
        lock state differs per caller: the exchange's owner already holds
        it, and a plain send has just taken it.

        ⛔ Refuses a link that is gone, HERE, where the write is, not at
        the callers. Two successive rewrites of the code around it lost the
        check when it lived beside them, each time putting a command onto a
        dead connection. A guard beside the thing it guards gets dropped by
        a rewrite; one inside it cannot. (An earlier revision split this
        into a bytes-taking half so a PARKED command could be held as
        immutable bytes; with the parking gone the split bought nothing but
        a second place for this guard to be lost from.)
        """
        if not isinstance(message, ScpiMessage):
            raise NotImplementedError(
                "Direct message sending without a string-typed ScpiMessage "
                "is not yet implemented."
            )
        # ONE get_bytes() call feeds the wire and nothing else now, but it
        # is still read exactly once: it is not guaranteed to be pure.
        data = message.get_bytes()
        if self._link_is_gone():
            raise DeviceNotConnectedError(
                "The message could not be sent: the connection is gone.",
                is_shutting_down=bool(
                    self._disposed or self._is_disconnecting
                ),
            )
        # Read once, under the lock connect() bumps the generation
        # with: disconnect() on another thread closes the stream under
        # no lock this path takes (upstream baabf0c), and the
        # generation tells this connection from a newer one on the
        # same transport object. connect() bumps it before installing
        # the new stream, so the pair read here is consistent.
        with self._state_lock:
            generation = self._connect_generation
            transport = self._transport
            try:
                stream = transport.stream if transport is not None else None
            except Exception:
                # A closed transport's getter raises.
                stream = None
        if stream is None:
            if self._shutting_down_since(transport, generation, None):
                raise DeviceNotConnectedError(
                    "The message could not be sent: a disconnect completed "
                    "while it was in flight.",
                    is_shutting_down=True,
                )
            raise NotImplementedError(
                "Sending without a transport-backed stream is not supported."
            )
        # Loop on partial writes — unbuffered file-like objects
        # (sock.makefile with buffering=0) may write fewer bytes
        # than requested per call. Without the loop, the tail of
        # the SCPI command can be silently dropped (Qodo PR 17
        # pass 8 #4). On any write failure, transition the device
        # to Lost/Error so callers don't keep using a zombie
        # connection (Qodo PR 17 pass 24 #2).
        try:
            view = memoryview(data)
            total_written = 0
            while total_written < len(data):
                written = stream.write(view[total_written:])
                if written is None:
                    # `RawIOBase.write` returns None when the
                    # underlying stream would block — that means
                    # zero bytes were written, NOT "wrote
                    # everything." Surface as IOError rather than
                    # silently dropping the rest of the SCPI
                    # command (Qodo PR 17 pass 11 #2).
                    raise IOError(
                        f"Stream.write() returned None at offset "
                        f"{total_written} of {len(data)}; cannot "
                        f"verify SCPI command was fully transmitted."
                    )
                if written <= 0:
                    raise IOError(
                        f"Stream write returned {written}; aborting "
                        f"after {total_written} of {len(data)} bytes."
                    )
                total_written += written
            try:
                stream.flush()
            except AttributeError:
                pass
        except Exception as exc:
            # Decided and applied under the lock connect() bumps the
            # generation with, so no disconnect/reconnect can land
            # between the check and the Lost transition.
            with self._state_lock:
                stale = self._shutting_down_since(transport, generation, stream)
                evt = None
                if not stale:
                    evt = self._change_status(ConnectionStatus.Lost)
                    self._state = DeviceState.Error
            if stale:
                # A disconnect on another thread closed the stream under
                # this write, or a reconnect has replaced it since: an
                # expected race, not a loss of the current link. The
                # current status stands (upstream baabf0c).
                raise DeviceNotConnectedError(
                    "The message could not be sent: a disconnect was in "
                    "progress and closed the connection.",
                    is_shutting_down=True,
                ) from exc
            if evt is not None:
                self._deliver_status_event(evt)
            raise

    def _shutting_down_since(
        self, transport: Any, generation: int, stream: Any
    ) -> bool:
        """Whether a disconnect, or a new connection (even on the same
        transport object), has replaced the connection a send started on.
        A link that dropped by itself (status Lost) isn't one: that send
        failure is the loss.

        Called under ``_state_lock``. The generation alone isn't enough:
        connect() bumps it before ``transport.connect()`` installs the new
        stream, so a send can capture the new generation with the old
        stream. The stream's identity settles it: the transport's current
        stream no longer being the captured one means it was replaced. The
        device fields come first, and a getter that raises (a closed
        transport) counts as replaced."""
        if (
            self._disposed
            or self._is_disconnecting
            or self._status in (ConnectionStatus.Disconnected, ConnectionStatus.Connecting)
            or self._transport is not transport
            or self._connect_generation != generation
        ):
            return True
        if stream is None:
            return False
        try:
            current = transport.stream
        except Exception:
            return True
        return current is not stream

    def _on_consumer_thread(self) -> bool:
        """Whether the calling thread is the protobuf consumer's own thread,
        i.e. whether we are inside a message-received callback."""
        consumer = self._consumer
        if consumer is None:
            return False
        return threading.current_thread() is getattr(consumer, "_thread", None)

    @contextlib.contextmanager
    def _write_exchange_lock(self, operation: str) -> Iterator[None]:
        """Hold the exchange for a WRITE-ONLY operation.

        ⛔ NOT :meth:`_exchange_lock`. That one refuses the consumer thread
        OUTRIGHT, which is right for a text exchange -- it pauses and
        restarts that very thread (#238) -- and wrong for an operation that
        only writes: such an operation is perfectly safe there, so refusing
        it when the exchange is IDLE rejects a caller the base served, and
        the audit found exactly that.

        The rule here is the one :meth:`send` uses, and both now come
        through this helper so they cannot drift:

        * the consumer thread never WAITS. It try-acquires, and is refused
          only when someone else holds the exchange -- because that holder
          may be waiting for this very thread to stop the consumer.
        * any other thread waits, bounded by
          :attr:`_SEND_EXCHANGE_WAIT_S`.

        The refusal for TEXT EXCHANGES stays unconditional and is untouched
        by this: see :meth:`_acquire_text_exchange_lock`.
        """
        if self._on_consumer_thread():
            if not self._text_exchange_lock.acquire(blocking=False):
                raise ExchangeTimeoutError(
                    f"{operation} cannot run on the protobuf consumer "
                    "thread -- that is, from a message-received callback -- "
                    "while another flow holds this device: waiting there "
                    "can deadlock, because that flow may be waiting for "
                    "this very thread to stop the consumer. Nothing was "
                    "sent. Use another thread, or group it with the other "
                    "flow inside run_exclusive()."
                )
        elif not self._text_exchange_lock.acquire(
            timeout=self._SEND_EXCHANGE_WAIT_S
        ):
            raise ExchangeTimeoutError(
                f"{operation} could not run: another flow has held this "
                f"device for more than {self._SEND_EXCHANGE_WAIT_S:g} s, "
                "which is longer than any legitimate exchange takes, so it "
                "is stuck rather than busy. Nothing was sent."
            )
        self._text_exchange_lock_owner = threading.get_ident()
        try:
            self._refuse_if_unusable(operation)
            yield
        finally:
            self._text_exchange_lock_owner = None
            self._text_exchange_lock.release()
            # Outside the lock, always: a queued status event runs user
            # callbacks. See _flush_pending_status_events.
            self._flush_pending_status_events()

    def _acquire_text_exchange_lock(self, operation: str) -> None:
        """Take the text-exchange lock, refusing outright on the consumer
        thread.

        A text exchange pauses the protobuf consumer, swaps a text reader
        onto the same stream, and restarts the consumer afterwards. Run from
        a message-received callback, it is the consumer's own thread doing
        that to itself: the stop cannot join, the restart raises inside the
        callback and is swallowed, and the call returns normally while the
        consumer thread has exited -- a later ``start_streaming()`` then
        reports streaming with nothing reading (#238).

        That is true whether or not the lock is contended, so the refusal
        must not be conditional on acquiring it. It used to be: this
        refused only when the lock was already held, which let through the
        common case of a quiet callback finding it free -- the broken case,
        not a safe one. Serializing against an exchange on another thread
        was never what made it unsafe.
        """
        if self._on_consumer_thread():
            raise RuntimeError(
                f"{operation} cannot run on the protobuf consumer thread, "
                "that is, from a message-received callback: a text exchange "
                "pauses and restarts that very thread. Call it from another "
                "thread."
            )
        self._text_exchange_lock.acquire()

    def run_exclusive(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn()`` as a single exchange, returning its result.

        No other flow's :meth:`send` or text exchange can interleave with
        it, so several commands run as one unit -- a configure-then-query
        sequence that a command in the middle would corrupt (#225). That
        holds for a send from ANY other thread: one from an ordinary thread
        waits for this group to finish, and one from the protobuf consumer
        thread is REFUSED outright rather than waiting, because waiting
        there can deadlock. (Neither held before #225: a message callback's
        send went straight out between this group's commands, and the
        group's own query read the callback's value back.)
        ``fn`` takes no arguments; use a closure or
        :func:`functools.partial`.

        Re-entrant for the thread already inside an exchange on this
        device: there, it simply runs ``fn`` under the lock that thread
        already holds.

        Raises:
            RuntimeError: when called on the message-received (protobuf
                consumer) thread, or while the device is disconnecting or
                disposed.
        """
        return self._run_exclusive(fn, "run_exclusive")

    def _run_exclusive(self, fn: Callable[[], Any], operation: str) -> Any:
        """Shared body of :meth:`run_exclusive` and
        :func:`_exclusive_sd_operation`.

        **Holding the exchange lock is the single permission to nest an
        exchange.** There is no separate SD-only permission: that was what
        made ``run_exclusive`` refuse the text queries it exists to group,
        since a grouped sequence is normally configure-then-query and only
        an SD operation was allowed the query. What forbids the nesting
        that is genuinely broken is ``_capture_owner`` -- a capture
        in progress on this thread -- not who owns the lock.

        ⛔ THE CAPTURE CHECK IS AT THE ENTRY, BEFORE EITHER PATH RUNS
        ``fn``. It used to sit only in :func:`_exclusive_sd_operation`,
        which the ownership fast path below does not pass through, so
        ``run_exclusive`` from inside a ``setup_action`` RAN the grouped
        sequence: its first command reached the device and only a later
        capture inside the group raised. A refusal that arrives after the
        device has changed state is not a refusal -- so it has to precede
        the first write, which means it belongs here, where the operation
        is entered, and nowhere else.
        """
        tid = threading.get_ident()
        if self._capture_owner == tid:
            raise RuntimeError(
                f"{operation} cannot run inside another exchange on the "
                "same device (for example from a setup_action callback)."
            )
        if self._text_exchange_lock_owner == tid:
            # Already ours: run under the lock we hold. Whoever took it
            # flushes the status-event queue on the way out.
            self._refuse_if_unusable(operation)
            return fn()
        with self._exchange_lock(operation):
            self._refuse_if_unusable(operation)
            return fn()

    def _run_owning_exchange(self, fn: Callable[[], Any], operation: str) -> Any:
        """Run ``fn`` owning the exchange, WITHOUT the capture refusal.

        ⛔ WHICH FORM A HELPER NEEDS, because getting this wrong has now
        caused a regression twice. The refusal in :meth:`_run_exclusive`
        exists to stop a CAPTURE starting inside a capture. So:

        * a helper that only WRITES (and sleeps) belongs here. It is safe
          inside a capture, and the refusal can only ever produce a false
          refusal -- it protects nothing and rejects a legitimate caller
          before anything is sent.
        * a helper that CAPTURES, or that WAITS FOR THE PROTOBUF CONSUMER,
          belongs in :meth:`_run_exclusive`. The consumer is paused during
          a capture, so such a helper cannot work there -- and a clear
          refusal is a far better outcome than the timeout it would
          otherwise hit.

        By that test: the SPI bus handoffs are here (writes only, and one
        is passed as ``pre_capture`` so it runs inside a capture by
        construction); ``update_network_configuration`` and
        ``apply_network_configuration`` are here (writes and a settle
        delay, and both are legitimately used as a ``setup_action``);
        ``initialize`` stays in :meth:`_run_exclusive` despite writing
        only, because it then WAITS for the status message that sets
        ``_metadata.serial_number``, which only the consumer thread
        delivers; and ``_exclusive_sd_operation`` and the public
        :meth:`run_exclusive` stay there because they capture, or run a
        caller's ``fn`` that may.

        Ownership is wanted in every case: a helper's commands must not be
        split by another flow, and its settle delays must measure time
        after delivery.
        """
        if self._text_exchange_lock_owner == threading.get_ident():
            self._refuse_if_unusable(operation)
            return fn()
        with self._write_exchange_lock(operation):
            return fn()

    def _refuse_if_unusable(self, operation: str) -> None:
        if self._disposed or self._is_disconnecting:
            raise RuntimeError(
                f"{operation} cannot run while the device is "
                "disconnecting or disposed."
            )

    @contextlib.contextmanager
    def _capture(self, refusal: str) -> Iterator[None]:
        """Mark a capture in progress on this thread for the body's duration.

        A capture pauses the protobuf consumer and reads the stream
        directly, so a second one started underneath it -- from a
        ``setup_action`` or a download's progress callback -- reads the
        first one's bytes. **Every capture path goes through here**, text
        and raw alike, which is the point: when the marker was set by the
        text path only, the raw download path silently had no guard, and
        the nested operation stole the outer file's data and EOF marker
        while reporting success. A new capture path cannot set it wrongly
        because it has nowhere else to set it.
        """
        tid = threading.get_ident()
        if self._capture_owner == tid:
            raise RuntimeError(refusal)
        previous = self._capture_owner
        self._capture_owner = tid
        try:
            yield
        finally:
            self._capture_owner = previous

    @contextlib.contextmanager
    def _exchange_lock(self, operation: str) -> Iterator[None]:
        """Hold the text-exchange lock for the body, then flush any status
        event the body produced.

        The one place the lock is taken and released for an exchange, so
        the flush cannot be forgotten by a new caller: a status event runs
        user callbacks and must never be delivered while the lock is held
        (see :meth:`_deliver_status_event`).
        """
        self._acquire_text_exchange_lock(operation)
        self._text_exchange_lock_owner = threading.get_ident()
        try:
            yield
        finally:
            # Release, then run user callbacks -- never the other way
            # round: a status event delivered under the lock re-enters the
            # device from a user callback (see _flush_pending_status_events).
            self._text_exchange_lock_owner = None
            self._text_exchange_lock.release()
            self._flush_pending_status_events()

    def execute_text_command(
        self,
        setup_action: Callable[[], None],
        response_timeout_ms: int = 5000,
        completion_timeout_ms: int = 1000,
        max_total_ms: Optional[int] = None,
    ) -> List[str]:
        """Run a SCPI command that expects a TEXT-mode reply, capturing
        the lines the device returns.

        After ``initialize()`` runs ``set_protobuf_stream_format()``, the
        consumer pipeline only parses length-prefixed protobuf frames —
        text replies (SD/network/info queries, ``**ERROR`` messages, etc.)
        are silently dropped by the protobuf parser. This helper pauses
        the protobuf consumer, swaps in a temporary text consumer on the
        SAME stream, runs ``setup_action()`` (typically a single
        :meth:`send` call), waits with a two-phase inactivity timeout,
        then restarts the protobuf consumer.

        Two-phase timeout (mirrors upstream
        ``ExecuteTextCommandAsync``):

        - Phase 1: wait up to ``response_timeout_ms`` for the FIRST text
          line to arrive.
        - Phase 2: once any line has arrived, wait until
          ``completion_timeout_ms`` of inactivity (no new lines) elapses
          before returning. This catches multi-line responses like
          ``SD:LIST?`` without an explicit termination marker.

        The protobuf consumer's restart is wrapped in ``finally`` so
        streaming resumes even if ``setup_action()`` raises. Closes
        Gap 1 of daqifi/daqifi-python-core#22.

        Args:
            setup_action: callable that issues the SCPI command (typically
                ``lambda: self.send(scpi_commands.<query>())``).
            response_timeout_ms: phase-1 budget for the first byte.
            completion_timeout_ms: phase-2 inactivity budget after the
                first line.
            max_total_ms: cap on the whole capture, measured from
                ``setup_action()``, so a device that keeps sending lines
                can't keep phase 2 going. ``None`` (the default) is five
                times ``response_timeout_ms``, upstream's ceiling.

        Lines may end in CRLF or a bare LF. A blank line is an answer (it
        ends phase 1: the firmware ends an empty ``SYSTem:LOG?`` with one)
        but isn't returned.

        Returns:
            List of non-blank text lines captured between ``setup_action()``
            and the inactivity timeout. Empty list when the device returned
            nothing (or only blank lines) within ``response_timeout_ms``.
        """
        tid = threading.get_ident()
        if self._text_exchange_lock_owner == tid:
            # We hold the lock already -- from run_exclusive or an SD
            # operation -- so run the capture under it. Holding the lock is
            # the whole permission; what is refused below is a capture
            # nested inside a capture, which is the case that corrupts.
            return self._run_text_capture(
                setup_action, response_timeout_ms, completion_timeout_ms,
                max_total_ms,
            )
        # All validation happens INSIDE the lock to close the TOCTOU
        # window: a competing thread can call disconnect() (which clears
        # self._consumer / self._transport without acquiring this lock)
        # while we're blocked on the lock. Re-checking after acquisition
        # guarantees the consumer, transport and stream we operate on are
        # still live.
        with self._exchange_lock("execute_text_command"):
            return self._run_text_capture(
                setup_action, response_timeout_ms, completion_timeout_ms,
                max_total_ms,
            )

    def _run_text_capture(
        self,
        setup_action: Callable[[], None],
        response_timeout_ms: int,
        completion_timeout_ms: int,
        max_total_ms: Optional[int],
    ) -> List[str]:
        """Run one text capture, refusing a capture nested inside a capture.

        ``_execute_text_command_locked`` pauses the protobuf consumer and
        swaps a text reader onto the stream. A second capture started from
        inside the first -- from its own ``setup_action`` -- would pause an
        already-paused consumer and restore it twice, which is the
        corruption the refusal exists for. Surfacing it as a RuntimeError
        rather than letting ``Lock.acquire`` deadlock keeps the caller's
        process responsive.

        Whether THIS THREAD is inside a capture, not lock ownership, is
        the discriminator: a thread can hold the lock without being inside
        a capture (``run_exclusive``, an SD operation), and those nestings
        are legitimate.
        """
        with self._capture(
            "execute_text_command is not re-entrant on the same device; "
            "do not call it from inside a setup_action callback."
        ):
            return self._execute_text_command_locked(
                setup_action, response_timeout_ms, completion_timeout_ms,
                max_total_ms,
            )

    def _execute_text_command_locked(
        self,
        setup_action: Callable[[], None],
        response_timeout_ms: int,
        completion_timeout_ms: int,
        max_total_ms: Optional[int] = None,
    ) -> List[str]:
        # Catch teardown that's in flight but hasn't yet cleared
        # self._consumer / self._transport — same TOCTOU class as
        # the pass-1 None-check, just one step earlier in the
        # lifecycle. Disconnect/close set these flags before
        # tearing the consumer down, so a competing thread that
        # passes the lock during the disconnect window is caught
        # here with a clear error rather than racing the actual
        # teardown into a partial state.
        if self._disposed or self._is_disconnecting:
            raise RuntimeError(
                "execute_text_command cannot run while the device is "
                "disconnecting or disposed."
            )
        if self._consumer is None:
            raise DeviceNotConnectedError(
                "execute_text_command requires an active consumer; "
                "call connect() first."
            )
        if self._transport is None:
            raise RuntimeError(
                "execute_text_command requires an active transport."
            )
        # The stream and the connection it belongs to are read together:
        # a reconnect while this exchange is paused below opens a newer
        # session, and the text reader's faults must count against the
        # stream it actually reads, never the newer one (#191).
        with self._state_lock:
            stream = self._transport.stream
            stream_session = self._transport_session
        if stream is None:
            raise RuntimeError(
                "Transport reports no stream; cannot pause consumer."
            )
        # On a transport that numbers its connections, no current session
        # means disconnect() is under way: the stream may be about to close,
        # and a reader on it could only be blamed on whatever connects next.
        if stream_session is None and hasattr(self._transport, "session"):
            raise RuntimeError(
                "execute_text_command cannot run while the device is "
                "disconnecting."
            )

        # A stream the text reader can't poll (no in_waiting) is read in
        # blocking chunks that return only at its read timeout: no wait
        # below may be shorter than that, or a reply on its way is cut off.
        if not hasattr(stream, "in_waiting"):
            read_timeout = getattr(stream, "timeout", None)
            if isinstance(read_timeout, (int, float)) and read_timeout > 0:
                floor_ms = int(read_timeout * 1000) + 250
                response_timeout_ms = max(response_timeout_ms, floor_ms)
                completion_timeout_ms = max(completion_timeout_ms, floor_ms)
                # An explicit max_total_ms is the caller's hard window and
                # is kept exactly: nothing back within it is the answer.

        was_running = self._consumer.is_running
        # Captured BEFORE the exchange, so a restart failure is attributed
        # to the connection that failed rather than to whatever connection
        # exists by the time the attempt finishes -- the same scoping the
        # replay faults already use.
        exchange_generation = self._connect_generation
        if was_running:
            try:
                stopped = self._consumer.stop_safely()
                if stopped is False:
                    # A SECOND bounded join, and it is load-bearing.
                    #
                    # An earlier revision dropped this, reasoning that
                    # stop() "can neither join itself nor force the exit".
                    # That is true only when the caller IS the reader: from
                    # any other thread stop() joins for another second, and
                    # that second is what lets a reader whose read has just
                    # timed out actually exit. Without it the text capture
                    # below starts while the old protobuf reader is still
                    # live, and the old reader consumes the text reply --
                    # execute_text_command returned [] instead of the
                    # device's answer. Measured, not reasoned.
                    #
                    # The comment before that called proceeding here
                    # "degraded but not catastrophic", which was also
                    # wrong: _is_running is already clear, so a restart
                    # that fails leaves NOTHING reading the stream.
                    self._consumer.stop()
            except Exception:
                # The pause is best-effort only in the sense that a broken
                # consumer must not stop the caller from capturing; a
                # consumer left unreadable is recorded at the restart below.
                pass

        captured: List[str] = []
        line_queue: "queue.Queue[str]" = queue.Queue()

        try:
            # LF-terminated replies too; blank lines count as an answer
            # (upstream a57a86a / 5a9f917) and are dropped on return.
            text_parser = LineBasedMessageParser(line_ending="\n", emit_empty_lines=True)
            # Read what's waiting: a short reply then arrives at once, not
            # after the serial port's whole read timeout.
            text_consumer = StreamMessageConsumer[str](
                stream,
                text_parser,
                health_sink=self._transport_health_sink(stream_session),
                read_available=True,
            )

            def _on_text_line(args: MessageReceivedEventArgs[str]) -> None:
                # Background-thread callback. Only push to the queue;
                # the captured list is built on the main thread from
                # the queue gets so late arrivals can't slip in past
                # the inactivity timeout.
                line_queue.put(args.message.data)

            text_consumer.add_message_received(_on_text_line)
            text_consumer.start()

            try:
                # Absolute cap, measured from setup_action: a device that
                # keeps sending lines (a stream left running) would otherwise
                # keep phase 2 going. Upstream caps every exchange at five
                # times the first-response timeout.
                if max_total_ms is None:
                    max_total_ms = response_timeout_ms * 5
                total_deadline = time.monotonic() + max_total_ms / 1000.0

                def _drain_queued() -> None:
                    while True:
                        try:
                            captured.append(line_queue.get_nowait())
                        except queue.Empty:
                            return

                setup_action()

                # Phase 1: block up to response_timeout_ms for the first
                # line. Returns immediately as soon as a line arrives,
                # so we don't waste cycles polling.
                first_wait = max(
                    0.0,
                    min(response_timeout_ms / 1000.0, total_deadline - time.monotonic()),
                )
                try:
                    captured.append(line_queue.get(timeout=first_wait))
                except queue.Empty:
                    # No data within phase-1 budget — skip phase-2
                    # entirely (nothing to wait inactivity ON).
                    return []

                # Phase 2: drain remaining lines until inactivity
                # exceeds the completion threshold. Each get blocks
                # until the next line OR the timeout — when get raises
                # queue.Empty, we've hit the inactivity gap.
                inactivity_threshold = completion_timeout_ms / 1000.0
                while True:
                    remaining = total_deadline - time.monotonic()
                    if remaining <= 0:
                        # Cap reached: keep what has already arrived.
                        _drain_queued()
                        break
                    wait = min(inactivity_threshold, remaining)
                    try:
                        captured.append(line_queue.get(timeout=wait))
                    except queue.Empty:
                        break
            finally:
                # Always tear down the text consumer.
                try:
                    stopped = text_consumer.stop_safely()
                    if stopped is False:
                        text_consumer.stop()
                except Exception:
                    pass
                try:
                    text_consumer.close()
                except Exception:
                    pass
        finally:
            # Restart the protobuf consumer so streaming resumes — even if
            # setup_action raised — unless the connection was lost.
            if was_running and self._consumer_can_restart_on(stream):
                try:
                    self._consumer.start()
                except Exception as restart_exc:
                    # ⛔ NOT swallowed any more. This is the line that made
                    # the whole failure silent: the exchange returned a
                    # plausible result while the reader was gone, and a
                    # later start_streaming() reported success with nothing
                    # on the stream. Raising here would discard the
                    # caller's captured reply and could mask setup_action's
                    # own error, so the damage goes where the misreporting
                    # calls read it instead (#238 residual).
                    self._record_delivery_fault(
                        "the message consumer could not be restarted after "
                        f"a text exchange, so nothing is reading the "
                        f"stream: {restart_exc}",
                        exchange_generation,
                    )
                    _log.warning(
                        "consumer restart failed after a text exchange; "
                        "reconnect required: %s", restart_exc,
                    )

        # Blank lines were answers, not content.
        return [line for line in captured if line.strip()]

    def _consumer_can_restart_on(self, stream: Any) -> bool:
        """False once the connection was lost during an exchange: the stream
        is dead, and a restarted reader would just poll it."""
        return (
            self._status == ConnectionStatus.Connected
            and not getattr(stream, "peer_closed", False)
        )

    def drain_errors(
        self,
        max_iterations: int = 256,
        empty_budget: int = 3,
        response_timeout_ms: int = 500,
        completion_timeout_ms: int = 200,
    ) -> List[str]:
        """Pop ``SYST:ERR?`` entries from the device until the queue
        reports ``"No error"``, ``empty_budget`` consecutive
        empty/unparseable replies arrive, or ``max_iterations`` is
        hit. Returns the popped errors.

        Mirrors upstream
        ``DaqifiDevice.DrainErrorQueueAsync(int maxIterations = 256,
        CancellationToken cancellationToken = default)`` from
        daqifi-core PR #185, with two Python-side robustness extensions
        carried over from :meth:`SerialInterface.drain_errors`:

        - ``empty_budget`` (default 3) caps consecutive empty/
          unparseable replies before bailing — avoids ~25 s stalls
          when a USB CDC timeout returns nothing within the read
          window. Upstream bails on the first empty reply (equivalent
          to ``empty_budget=1``).
        - A synthetic ``DRAIN_INCONCLUSIVE`` sentinel is appended when
          the loop exits via ``empty_budget`` or ``max_iterations``
          WITHOUT seeing ``0,"No error"`` so callers can detect "queue
          not confirmed clean."

        This is the queue-inspection counterpart to the inline last-
        command error check used elsewhere (e.g.
        :func:`daqifi._internal.scpi.contains_scpi_error`): that
        helper tells you whether the captured response from a single
        command contained an error, while this drains the whole queue
        and returns ownership of the popped entries to the caller.

        Each iteration uses :meth:`execute_text_command`, which pauses
        the protobuf consumer for the duration of the text exchange.
        Avoid calling this during active streaming. Concurrent calls
        from multiple threads are safe — :meth:`execute_text_command`
        serializes them via a per-device lock (daqifi-python-core#92).

        Args:
            max_iterations: Safety cap on the number of ``SYST:ERR?``
                queries.
            empty_budget: Tolerance for consecutive empty/unparseable
                replies before bailing.
            response_timeout_ms: Per-iteration phase-1 timeout passed
                through to :meth:`execute_text_command`.
            completion_timeout_ms: Per-iteration phase-2 inactivity
                timeout passed through to :meth:`execute_text_command`.

        Returns:
            List of error strings popped from the queue. Empty when
            the queue was already clean. May contain a trailing
            ``DRAIN_INCONCLUSIVE`` sentinel if drain exited without
            confirming ``0,"No error"``.

        Raises:
            ValueError: ``max_iterations`` or ``empty_budget`` ≤ 0.
            RuntimeError: device not connected.
        """
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if empty_budget <= 0:
            raise ValueError("empty_budget must be positive")
        if response_timeout_ms <= 0:
            raise ValueError("response_timeout_ms must be positive")
        if completion_timeout_ms <= 0:
            raise ValueError("completion_timeout_ms must be positive")
        # Match the precondition the docstring promises and the
        # convention used by send() / other DaqifiDevice methods.
        if not self.is_connected:
            raise DeviceNotConnectedError()

        from ._internal import scpi as _scpi
        from ._internal import scpi_commands as _scpi_commands

        popped: List[str] = []
        consecutive_empty = 0
        saw_no_error = False

        iterations = 0
        for _ in range(max_iterations):
            iterations += 1
            lines = self.execute_text_command(
                setup_action=lambda: self.send(_scpi_commands.get_last_error()),
                response_timeout_ms=response_timeout_ms,
                completion_timeout_ms=completion_timeout_ms,
            )
            reply = _scpi.find_system_error_reply(lines)
            if reply is None:
                consecutive_empty += 1
                if consecutive_empty >= empty_budget:
                    break
                continue

            code, desc = _scpi.parse_system_error(reply)
            if code == 0 and desc.strip().lower() == "no error":
                saw_no_error = True
                break

            consecutive_empty = 0
            popped.append(reply)

        if not saw_no_error:
            # Format the sentinel as a parseable SCPI error so callers
            # who pipe popped entries through scpi.parse_system_error
            # see a non-zero code. Without the leading numeric code,
            # parse_system_error catches the ValueError and falls back
            # to (0, "No error"), silently classifying the sentinel as
            # a clean queue and defeating its purpose entirely. (Qodo
            # /improve pass 6 importance 9 caught the silent-miss; pass
            # 7 /agentic_review caught the follow-up: -100 collided
            # with the standard SCPI "Command error" code, so callers
            # branching on the numeric code couldn't distinguish a
            # synthetic drain-incomplete marker from a real -100.)
            #
            # `99999` is clearly outside the SCPI standard error range
            # (-100 to -899 negative, 1-32767 device-specific
            # positive); no real device should ever emit it, so callers
            # can safely treat it as the synthetic sentinel marker.
            #
            # Includes actual iteration / consecutive_empty counts (not
            # just the limits) so callers can distinguish "hit
            # max_iterations with errors still streaming" from "bailed
            # early on consecutive empties."
            popped.append(
                '99999,"DRAIN_INCONCLUSIVE: queue not confirmed clean '
                f'after {iterations} iterations; consecutive_empty='
                f'{consecutive_empty}; limits: max_iterations='
                f'{max_iterations}, empty_budget={empty_budget}"'
            )

        return popped

    def initialize(self, response_timeout_s: Optional[float] = None) -> None:
        # ⛔ OWNS THE EXCHANGE FOR THE WHOLE SEQUENCE, and still does now
        # ⛔ OWNERSHIP IS NARROWED TO THE SENDS, and released before the
        # status wait. It used to span the whole call, which was a
        # parking-era need -- the deadline had to start only after the
        # commands were delivered -- and blocking send() guarantees that
        # already: returning means written.
        #
        # Holding it across the WAIT was actively harmful. The consumer
        # dispatches the status this call is waiting for, and that dispatch
        # runs the library's own channels_populated handlers; a send from
        # one of those was refused because initialize held the exchange,
        # the handler loop swallowed the refusal, and initialize reported
        # success with the command never sent. It also wedged initialize
        # for any callback that delegated a send and joined it.
        #
        # ⚠ WHAT NARROWING COSTS, stated rather than discovered later:
        # during the wait another flow may start a text capture, which
        # pauses the consumer, so the status is never dispatched and this
        # call times out. Ownership used to prevent that. The trade is a
        # LOUD failure under concurrent use in place of a SILENT wrong
        # success, which is the right direction -- and the timeout now says
        # the device is not at fault when the consumer is the thing held up.
        #
        # ⚠ A caller that wrapped initialize() in its own run_exclusive
        # still holds across the wait, by its own request: this releases
        # only the ownership it took itself.
        tid = threading.get_ident()
        if self._capture_owner == tid:
            raise RuntimeError(
                "initialize cannot run inside another exchange on the same "
                "device (for example from a setup_action callback): the "
                "protobuf consumer is paused there, so the status reply it "
                "waits for can never arrive."
            )
        if self._on_consumer_thread():
            raise RuntimeError(
                "initialize cannot run on the protobuf consumer thread, "
                "that is, from a message-received callback: it waits for a "
                "status reply that only that thread can dispatch. Call it "
                "from another thread."
            )
        self._refuse_if_unusable("initialize")
        return self._initialize_locked(response_timeout_s)

    def _initialize_locked(
        self, response_timeout_s: Optional[float] = None
    ) -> None:
        """Run the standard SCPI init sequence.

        Ports the upstream ``InitializeAsync`` (sync here per project
        convention). Wires the protocol handler, then sends the text-mode
        setup commands, then queries device info -- the protobuf consumer's
        status callback updates :attr:`metadata`. Neither whether the device
        accepted the setup commands nor, on serial, whether it reported its
        channels is checked: see #241.

        :param response_timeout_s: max time to wait for the protobuf
            ``SYSInfoPB?`` reply after sending ``get_device_info()``.
            Polled every 50 ms, so a healthy device returns well under
            a second. Defaults to 1.0 s on serial transports (matches
            historical fixed sleep) and 5.0 s on TCP. On TCP, a
            timeout raises :class:`DeviceUnresponsiveError` to surface
            the WiFi-SCPI silence pattern (#73) instead of returning
            with empty metadata; on other transports, a timeout
            currently completes silently for backwards compatibility
            with fakes that never deliver a status frame.
        """
        if response_timeout_s is not None and not response_timeout_s > 0:
            raise ValueError(
                f"response_timeout_s must be positive (got {response_timeout_s!r})."
            )
        if not self.is_connected:
            raise DeviceNotConnectedError(
                "Device must be connected before initialization."
            )
        if self._is_initialized:
            return

        # Snapshot prior state so a BaseException interrupt can restore
        # it from "Initializing" — operator may want to resume after
        # Ctrl-C without the device permanently stuck in the transient
        # init state (Qodo PR #54 /improve pass 1 importance 6).
        prev_state = self._state
        self._set_state(DeviceState.Initializing)
        # init_succeeded tracks whether we made it through the full
        # SCPI sequence. The cleanup runs in `finally` so it covers
        # BaseException (KeyboardInterrupt / SystemExit) too, not just
        # Exception. Without this, a Ctrl-C mid-init would leave the
        # handler registered and a subsequent retry would double-
        # register it. (Closes daqifi/daqifi-python-core#26 + Qodo
        # PR #54 review #2.)
        init_succeeded = False
        # Clear SN so the post-``get_device_info()`` poll waits for a
        # fresh SYSInfoPB? frame instead of breaking out instantly on a
        # value left over from a prior init on the same instance —
        # without this, a re-init against a wedged TCP listener would
        # skip the silent-init detection (Qodo PR #75 /agentic_review
        # pass 1 bug #2). disconnect() doesn't touch metadata.
        # Snapshot first so a mid-init failure can restore the previous
        # SN — leaving identity metadata cleared on a recoverable
        # failure makes retries/debugging harder (Qodo PR #75
        # /agentic_review pass 2 bug #3).
        prev_serial_number = self._metadata.serial_number
        self._metadata.serial_number = ""
        try:
            self._protocol_handler = ProtobufProtocolHandler(
                status_message_handler=self._on_status_message_received,
                stream_message_handler=self._on_stream_message_received,
            )
            if self._consumer is not None:
                self._consumer.add_message_received(self._on_inbound_message)

            # Polled (was a fixed 1.0 s sleep) so a healthy device
            # returns early — needed roughly 500 ms over USB CDC on
            # the 2026-04-23 NQ1 hardware run. TCP gets a longer
            # ceiling because the WiFi-SCPI wedge (#73) is the case
            # we want to detect distinctly from a routine slow reply.
            is_tcp = isinstance(self._transport, TcpStreamTransport)
            if response_timeout_s is None:
                response_timeout_s = 5.0 if is_tcp else 1.0

            # ⭐ THE SENDS OWN THE EXCHANGE AS ONE SEQUENCE; THE WAIT BELOW
            # DOES NOT. Another flow's command between these steps would
            # break what the later ones assume, so the grouping is still
            # wanted -- it is only the WAIT that must not hold, because the
            # consumer dispatches the reply it waits for.
            self._run_owning_exchange(
                self._initialize_send_sequence, "initialize"
            )

            # From here the exchange is FREE, so the consumer can dispatch
            # the status and the handlers it runs can send.
            deadline = time.monotonic() + response_timeout_s
            while time.monotonic() < deadline:
                if self._metadata.serial_number:
                    break
                time.sleep(0.05)
            if not self._metadata.serial_number:
                # ⛔ DO NOT BLAME THE DEVICE FOR OUR OWN WEDGE. The
                # consumer dispatches the reply this call waits for, so a
                # consumer stuck inside a message-received callback means
                # the silence is OURS. The exchange is free during this
                # wait now, so a callback's own send is no longer what
                # wedges it -- but a callback that blocks on anything else
                # still can, and the library cannot see through that.
                # What it CAN do is say where the silence comes from.
                consumer = self._consumer
                if consumer is not None and consumer.is_in_callback:
                    raise DeviceUnresponsiveError(
                        "No SYSInfoPB? reply within "
                        f"{response_timeout_s:.1f} s, and the DEVICE IS NOT "
                        "AT FAULT: this library's message consumer is "
                        "inside a message-received callback, so it cannot "
                        "dispatch the reply this call is waiting for. A "
                        "callback that hands work to another thread and "
                        "joins it will do this, because initialize() holds "
                        "the device while it waits. Return from the "
                        "callback without blocking on anything that needs "
                        "this device."
                    )
            if not self._metadata.serial_number and is_tcp:
                raise DeviceUnresponsiveError(
                    f"No SYSInfoPB? reply on TCP within "
                    f"{response_timeout_s:.1f} s — TCP socket is up "
                    f"but the device's WiFi-SCPI listener appears "
                    f"wedged. Recovery: reboot the device via USB "
                    f"(SYSTem:REboot)."
                )

            self._is_initialized = True
            self._set_state(DeviceState.Ready)
            init_succeeded = True
        except Exception:
            # Mark Error state on Exception specifically — KeyboardInterrupt
            # / SystemExit shouldn't transition to "Error" because the
            # operator may want to resume.
            self._set_state(DeviceState.Error)
            raise
        finally:
            if not init_succeeded:
                # remove_message_received is idempotent (suppresses
                # ValueError when the handler isn't subscribed) so we
                # can call it unconditionally without tracking a
                # registration flag.
                if self._consumer is not None:
                    try:
                        self._consumer.remove_message_received(self._on_inbound_message)
                    except Exception:
                        pass
                self._protocol_handler = None
                # If the state is still Initializing (BaseException
                # path — Exception path already set Error), restore
                # the prior state so an operator-driven resume isn't
                # blocked on a permanent transient state.
                if self._state == DeviceState.Initializing:
                    self._set_state(prev_state)
                # Restore the SN we cleared at the top — but only if
                # the in-flight protobuf frame didn't already deliver
                # a fresh value (we'd be clobbering the new one with
                # the old). Qodo PR #75 /agentic_review pass 2 bug #3.
                if not self._metadata.serial_number:
                    self._metadata.serial_number = prev_serial_number

    def close(self) -> None:
        if self._disposed:
            return
        try:
            self.disconnect()
        except Exception:
            pass
        if self._consumer is not None:
            try:
                self._consumer.close()
            except Exception:
                pass
        if self._transport is not None:
            # Unsubscribe transport status callback before closing —
            # otherwise a transport that outlives the device (e.g.
            # passed in externally and reused) keeps a strong ref to
            # this device's bound method, leaking it (Qodo PR 17 pass
            # 28 #1).
            try:
                self._transport.remove_status_changed(
                    self._on_transport_status_changed
                )
            except Exception:
                pass
            try:
                self._transport.close()
            except Exception:
                pass
        self._disposed = True

    # ---- channel population (called by status-message handler) ------------

    def populate_channels_from_status(self, message: Any) -> None:
        """Populate :attr:`channels` from a protobuf ``DaqifiOutMessage``.

        Mirrors upstream ``PopulateChannelsFromStatus``. The Python port
        treats ``message`` as duck-typed (the project's
        ``DaqifiOutMessage`` is :data:`typing.Any` by convention -- see
        :mod:`daqifi.protocol`). Any object with the standard protobuf
        field names works.
        """
        if message is None:
            raise TypeError("message must not be None.")

        timestamp_freq = getattr(message, "timestamp_freq", 0) \
            or getattr(message, "TimestampFreq", 0)
        # Same int-cast hardening as DeviceMetadata.update_from_protobuf
        # (Qodo PR 17 pass 43 #2 / pass 32 #1) — a non-numeric stub
        # value would have crashed the cast and dropped the rest of
        # populate_channels_from_status mid-way.
        try:
            tf_int = int(timestamp_freq)
        except (TypeError, ValueError):
            tf_int = 0
        if tf_int > 0:
            self._timestamp_frequency = tf_int

        self._channels.clear()

        analog_count = self._populate_analog_channels(message)
        digital_count = self._populate_digital_channels(message)

        # Snapshot before notifying so handlers can't mutate via _channels.
        snapshot = tuple(self._channels)
        args = ChannelsPopulatedEventArgs(
            channels=snapshot,
            analog_channel_count=analog_count,
            digital_channel_count=digital_count,
        )
        for h in list(self._channels_populated_handlers):
            try:
                h(args)
            except Exception:
                # ⛔ ISOLATED BUT NOT SILENT. One subscriber must not stop
                # the others, so the exception is still swallowed here --
                # but it used to vanish entirely, and a send refused inside
                # a handler then left initialize() reporting success with
                # the command never sent. Two signals, two channels: the
                # other handlers still run, AND the failure is reported.
                _log.warning(
                    "device %s: a channels_populated handler raised; its "
                    "work did not complete",
                    self.name, exc_info=True,
                )

    def _populate_analog_channels(self, message: Any) -> int:
        # Same int-cast hardening as the metadata path (Qodo PR 17
        # pass 44 #1 / pass 32-33 #1) — a non-numeric stub value
        # would otherwise crash here and skip channel population
        # entirely.
        raw_count = (
            getattr(message, "analog_in_port_num", 0)
            or getattr(message, "AnalogInPortNum", 0)
        )
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return 0
        cal_b = self._field_list(message, "analog_in_cal_b", "AnalogInCalB")
        cal_m = self._field_list(message, "analog_in_cal_m", "AnalogInCalM")
        int_scale = self._field_list(
            message, "analog_in_int_scale_m", "AnalogInIntScaleM"
        )
        ranges = self._field_list(
            message, "analog_in_port_range", "AnalogInPortRange"
        )
        raw_res = (
            getattr(message, "analog_in_res", 0)
            or getattr(message, "AnalogInRes", 0)
        )
        try:
            resolution = int(raw_res)
        except (TypeError, ValueError):
            resolution = 0
        if resolution <= 0:
            resolution = 65535
        # Field 22: bit n set = channel n enabled (little-endian bytes,
        # firmware int2PBByteArray). Unreported (empty) reads as disabled.
        enabled_mask = self._bit_mask(message, "analog_in_port_enabled", "AnalogInPortEnabled")

        for i in range(count):
            channel = AnalogChannel(
                index=i,
                resolution=resolution,
                name=f"AI{i}",
                calibration_b=self._with_default(cal_b, i, 0.0),
                calibration_m=self._with_default(cal_m, i, 1.0),
                internal_scale_m=self._with_default(int_scale, i, 1.0),
                port_range=self._with_default(ranges, i, 1.0),
                is_enabled=self._bit_set(enabled_mask, i),
            )
            self._channels.append(channel)
        return count

    def _populate_digital_channels(self, message: Any) -> int:
        # Same hardening as _populate_analog_channels (Qodo PR 17
        # pass 44 #1) — non-numeric stubs would crash here.
        raw_count = (
            getattr(message, "digital_port_num", 0)
            or getattr(message, "DigitalPortNum", 0)
        )
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return 0
        # Field 37: bit n set = DIO n is an input, clear = output (the
        # firmware's IsInput, little-endian bytes). A pin the mask doesn't
        # cover is reported as Input, as before (upstream 5449896).
        dir_mask = self._bit_mask(message, "digital_port_dir", "DigitalPortDir")
        for i in range(count):
            covered = dir_mask is not None and i // 8 < len(dir_mask)
            if covered and not self._bit_set(dir_mask, i):
                direction = ChannelDirection.Output
            else:
                direction = ChannelDirection.Input
            self._channels.append(
                DigitalChannel(
                    index=i, name=f"DIO{i}", direction=int(direction), is_enabled=False
                )
            )
        return count

    @staticmethod
    def _bit_mask(message: Any, snake: str, pascal: str) -> Optional[bytes]:
        """A protobuf ``bytes`` bit mask, or None when it's absent or empty
        (not reported)."""
        value = getattr(message, snake, None) or getattr(message, pascal, None)
        if isinstance(value, (bytes, bytearray, memoryview)) and len(value) > 0:
            return bytes(value)
        return None

    @staticmethod
    def _bit_set(mask: Optional[bytes], index: int) -> bool:
        """Bit ``index`` of a little-endian byte mask (byte index // 8,
        bit index % 8), as upstream reads it."""
        if mask is None or index // 8 >= len(mask):
            return False
        return bool(mask[index // 8] & (1 << (index % 8)))

    @staticmethod
    def _field_list(message: Any, snake: str, pascal: str) -> Optional[list]:
        return getattr(message, snake, None) or getattr(message, pascal, None)

    @staticmethod
    def _with_default(values, index: int, default):
        if values is None:
            return default
        try:
            return values[index]
        except (IndexError, TypeError, KeyError):
            return default

    # ---- internal callbacks -----------------------------------------------

    def _on_inbound_message(self, args: Any) -> None:
        # `args` shape mirrors MessageReceivedEventArgs; route through
        # the protocol handler if it accepts the message.
        message = getattr(args, "message", None)
        # Preserve a real inbound envelope (custom parser wrapper),
        # but DON'T treat a protobuf payload that incidentally exposes
        # a `.data` field as one — wrapping it correctly is what lets
        # ProtobufProtocolHandler.can_handle find DESCRIPTOR /
        # device_status on the inner payload (Qodo PR 17 pass 40 #1,
        # refining pass 25 #3 / pass 7 #4).
        is_probably_protobuf = (
            message is not None and hasattr(message, "DESCRIPTOR")
        )
        if (
            message is not None
            and hasattr(message, "data")
            and not is_probably_protobuf
        ):
            wrapped = message
        else:
            wrapped = GenericInboundMessage(data=message)
        if self._protocol_handler is None or not self._protocol_handler.can_handle(wrapped):
            return
        # Hot path: streaming pumps thousands of these per second.
        # Prefer the synchronous `handle()` if the handler exposes it
        # (ProtobufProtocolHandler does — see Qodo PR 17 finding 4).
        # Fall back to the async path only for handlers that don't
        # expose a sync alternative.
        sync_handle = getattr(self._protocol_handler, "handle", None)
        if callable(sync_handle):
            sync_handle(wrapped)
            return
        # The streaming pipeline is wholly synchronous in this project.
        # If a custom protocol handler exposes only the async method,
        # fail loudly so the integrator either implements `handle()`
        # or runs their own async dispatcher — silently spinning up
        # a per-message event loop (the previous behavior) was a
        # latency cliff at high stream rates (Qodo PR 17 pass 2 #2).
        raise RuntimeError(
            "Protocol handler must expose a synchronous `handle()` "
            "method. Async-only handlers are not supported in the "
            "streaming pipeline because creating an asyncio event loop "
            "per inbound message destroys throughput at >100 Hz."
        )

    def _on_status_message_received(self, message: Any) -> None:
        # Fill metadata, populate channels, fan out to subscribers.
        try:
            self._metadata.update_from_protobuf(message)
        except Exception:
            _log.warning(
                "device %s: status message did not update metadata",
                self.name, exc_info=True,
            )
        try:
            self.populate_channels_from_status(message)
        except Exception:
            _log.warning(
                "device %s: status message did not populate channels",
                self.name, exc_info=True,
            )
        self._fire_message(message)

    def _on_stream_message_received(self, message: Any) -> None:
        self._fire_message(message)

    def _fire_message(self, payload: Any) -> None:
        from datetime import datetime, timezone
        args = MessageReceivedEventArgs(
            message=GenericInboundMessage(data=payload),
            raw_data=b"",
            timestamp=datetime.now(timezone.utc),
        )
        for h in list(self._message_handlers):
            try:
                h(args)
            except Exception:
                pass

    def _transport_health_sink(self, session: Optional[int]) -> Optional[Any]:
        """A sink reporting reads on this connection's stream to the
        transport, when it can be told (``report_io_fault`` /
        ``report_io_success``): the serial transport uses that to notice an
        unplugged device (#191). Bound to ``session``, so a reader of an
        earlier connection can't count against a newer one."""
        transport = self._transport
        if callable(getattr(transport, "report_io_fault", None)) and callable(
            getattr(transport, "report_io_success", None)
        ):
            return _SessionHealthSink(transport, session)
        return None

    def _on_transport_status_changed(self, args: TransportStatusEventArgs) -> None:
        if args.is_connected:
            return
        # Report only an unexpected drop of the live connection (upstream
        # OnTransportStatusChanged): not during disconnect(), not for a
        # connection that has since been closed or replaced (args.session
        # no longer current), and not while connect() is still running,
        # which checks its own stream before publishing "Connected".
        # Nothing is torn down here; that is connect()'s and disconnect()'s
        # job, serialized by the lifecycle gate, so this handler can't race
        # either of them.
        with self._state_lock:
            if self._is_disconnecting:
                return
            if args.session is not None and args.session != self._transport_session:
                return
            consumer = self._consumer
            evt = None
            if self._status == ConnectionStatus.Connected:
                self._is_initialized = False
                evt = self._change_status(ConnectionStatus.Lost)
                self._state = DeviceState.Error
            # Otherwise the status is already decided (a failed send() set
            # Lost, or connect() is still running and checks its stream),
            # but the reader on the dead stream is stopped all the same.
        # Stop the reader spinning on a dead stream; the consumer object stays
        # for the next connect()/disconnect() to dispose of. stop_safely only
        # sets the flag when called from the reader's own thread.
        if consumer is not None:
            try:
                if consumer.stop_safely() is False:
                    consumer.stop()
            except Exception:
                pass
        if evt is not None:
            self._deliver_status_event(evt)

    def _discard_consumer(self, consumer: Any) -> None:
        try:
            consumer.remove_message_received(self._on_inbound_message)
        except Exception:
            pass
        try:
            stopped = consumer.stop_safely()
            if stopped is False:
                consumer.stop()
        except Exception:
            pass
        try:
            consumer.close()
        except Exception:
            pass

    # ---- helpers ----------------------------------------------------------

    def _set_status(
        self,
        value: ConnectionStatus,
        if_consumer: Optional[Any] = None,
        if_stream_alive: Optional[Any] = None,
        state: Optional[DeviceState] = None,
    ) -> bool:
        """Change the status (and `state`, together) and notify.

        With `if_consumer`, only while that consumer is still the device's;
        with `if_stream_alive`, only while that stream has not seen the
        peer close. Returns False, changing nothing, otherwise.
        """
        with self._state_lock:
            if if_consumer is not None and self._consumer is not if_consumer:
                return False
            if if_stream_alive is not None and getattr(
                if_stream_alive, "peer_closed", False
            ):
                return False
            # A loss reported while connecting (a serial transport counting
            # read faults) wins over "Connected" too. The transport says so,
            # not the reader: a reader paused by a text exchange isn't lost.
            if (
                if_consumer is not None
                and self._transport is not None
                and not getattr(self._transport, "is_connected", True)
            ):
                return False
            evt = self._change_status(value)
            if state is not None:
                self._state = state
        if evt is not None:
            self._deliver_status_event(evt)
        return True

    def _change_status(self, value: ConnectionStatus) -> Optional[DeviceStatusEventArgs]:
        # Caller holds _state_lock; the event is delivered after release.
        if self._status == value:
            return None
        self._status = value
        return DeviceStatusEventArgs(status=value)

    def _deliver_status_event(self, evt: DeviceStatusEventArgs) -> None:
        # ⛔ NO USER CALLBACK RUNS WHILE THIS THREAD HOLDS THE EXCHANGE
        # LOCK. A handler can do anything, including hand recovery to
        # another thread and wait for it; that thread's send() or exchange
        # needs this lock, which the handler's own caller cannot release
        # until the handler returns, and both stop. Recording the owner
        # thread cannot help -- the waiting is done by a DIFFERENT thread,
        # so no same-thread re-entry rule can see it. The only fix is not
        # to call out while holding it.
        #
        # The test is here rather than at each delivery site so that a new
        # site cannot miss it: the deferral co-occurs with the delivery.
        # Whoever releases the lock flushes the queue.
        if self._text_exchange_lock_owner == threading.get_ident():
            with self._state_lock:
                self._pending_status_events.append(evt)
            return
        # Changes are decided under the lock but delivered outside it, so
        # two threads' events can arrive out of order. One that is no
        # longer true by now (a "Connected" overtaken by "Lost") is
        # dropped rather than leave subscribers with a stale status.
        # Checked before each subscriber, not once: the status can change
        # while an earlier subscriber is still handling this event.
        for h in list(self._status_handlers):
            if evt.status != self._status:
                return
            try:
                h(evt)
            except Exception:
                pass

    def _link_is_gone(self) -> bool:
        """Whether this connection is gone with nothing newer coming.

        ⚠ Deliberately NOT ``not self.is_connected``: that is also true
        while Connecting, and a send that captured the old stream during a
        reconnect is designed to go ahead and fail harmlessly so the
        reconnect is not marked Lost (pinned by test_device_lifecycle's
        ..._spares_the_reconnect). One definition, read by both the
        post-acquire check in :meth:`send` and the write itself, so the two
        cannot drift apart. They did once: parking the sends reopened the
        very defect the post-acquire check had closed, because the replay
        went straight to the writer. Any mechanism that makes a send wait
        -- parking then, blocking now -- moves it past the checks it passed
        on the way in, which is why the check exists after the wait and
        inside the write rather than only at the entry.
        """
        return bool(
            self._disposed
            or self._is_disconnecting
            or self._status in (ConnectionStatus.Lost,
                                ConnectionStatus.Disconnected)
        )

    def _record_delivery_fault(self, detail: str, generation: int) -> None:
        """Remember that something the device was told to do never happened.

        The pipeline failing silently is the whole defect here, so a fault
        is kept on the device where the calls that would otherwise
        misreport consult it, not just written to a log nobody reads.

        Scoped to the connection that failed: a replay that fails on a
        connection already replaced must not condemn its successor, which
        is live. Recorded under ``generation``, and
        :attr:`delivery_fault` ignores a fault from an older one.
        """
        with self._state_lock:
            # ⛔ COMPARE-AND-SET. A stale drain finishing late used to
            # overwrite the CURRENT generation's fault with its own older
            # one -- and because the stored generation was then stale,
            # delivery_fault read None and start_streaming() succeeded over
            # a dead consumer. A fault may only be replaced by one from the
            # same connection or a newer one.
            stored = self._delivery_fault_generation
            if stored is not None and generation < stored:
                _log.debug(
                    "ignoring a delivery fault from generation %s; a newer "
                    "one (%s) is already recorded", generation, stored,
                )
                return
            self._delivery_fault = detail
            self._delivery_fault_generation = generation

    @property
    def delivery_fault(self) -> Optional[str]:
        """Why THIS connection can no longer be trusted to have received
        what it was told, or ``None``. Set when a queued command was
        dropped or failed after ``send()`` returned, or when the message
        consumer could not be restarted. A fault recorded against an
        earlier connection does not apply here; a successful
        :meth:`connect` also clears it."""
        with self._state_lock:
            if self._delivery_fault is None:
                return None
            if self._delivery_fault_generation != self._connect_generation:
                return None
            return self._delivery_fault

    def _raise_if_delivery_faulted(self, operation: str) -> None:
        """⛔ ON THIS CLASS, beside the fault it reads, because
        :meth:`send` -- which is also on this class -- calls it after
        acquiring the exchange.

        It lived on ``DaqifiStreamingDevice`` while only that subclass's
        ``start_streaming`` consulted it. Calling it from ``send`` without
        moving it made every send on a plain ``DaqifiDevice`` raise
        AttributeError. That is the SECOND time in two commits a member was
        left on the wrong side of this class boundary for ``send`` to
        reach, and both times the same generated test
        (``test_send_writes_scpi_to_stream``, which exercises a bare
        ``DaqifiDevice``) is what caught it rather than review.
        """
        # The property, not the field: a fault recorded against a
        # connection that has since been replaced does not apply here.
        fault = self.delivery_fault
        if fault is not None:
            raise DeliveryFaultError(
                f"{operation} cannot be trusted on this connection: "
                f"{fault}. Reconnect before continuing."
            )

    def _flush_pending_status_events(self) -> None:
        """Deliver status events queued while the exchange lock was held.

        Called by whichever frame releases the lock, in its ``finally``, so
        a failure on the way out still delivers. Never called with the lock
        held -- that is the whole point, and it is also why the drain
        terminates: nothing can queue while the lock is released, so the
        list only shrinks.
        """
        while True:
            with self._state_lock:
                if not self._pending_status_events:
                    return
                evt = self._pending_status_events.pop(0)
            self._deliver_status_event(evt)

    def _set_state(self, value: DeviceState) -> None:
        self._state = value

    def _initialize_send_sequence(self) -> None:
        """The init sequence's commands, run owning the exchange.

        Extracted so the ownership covers exactly the sends: every later
        step assumes the earlier ones took effect on a device nobody else
        has touched, which is what the grouping buys. The STATUS WAIT is
        deliberately outside it -- see :meth:`initialize`.
        """
        # Standard SCPI init sequence (mirrors upstream).
        self.send(scpi_commands.disable_device_echo())
        time.sleep(0.1)
        self.send(scpi_commands.stop_streaming())
        time.sleep(0.1)
        self.send(scpi_commands.turn_device_on())
        time.sleep(0.1)
        self.send(scpi_commands.set_protobuf_stream_format())
        time.sleep(0.1)
        # Pin the stream output to the same transport we connected over.
        # Without this, streaming bytes can be routed to the
        # last-configured interface (e.g. WiFi) and the consumer on this
        # transport sees nothing.
        interface = self._stream_interface_for_transport()
        if interface is not None:
            self.send(scpi_commands.set_stream_interface(interface))
            time.sleep(0.1)
        # Query device info -- the protobuf consumer dispatches the
        # response to OnStatusMessageReceived, which fills metadata. The
        # query goes out INSIDE the ownership; waiting for its reply does
        # not.
        self.send(scpi_commands.get_device_info())

    def _stream_interface_for_transport(self) -> Optional[StreamInterface]:
        """Pick the SCPI stream-interface enum that matches the
        connected transport, so streaming data flows back over the
        same channel the caller connected on. Returns None for
        transports we don't recognise (caller can override with an
        explicit ``set_stream_interface`` send).
        """
        if self._transport is None:
            return None
        # Lazy-import the concrete transport classes here. Module-level
        # would create a true circular import via daqifi.transport →
        # daqifi.devices (DaqifiDeviceFactory.connect_serial constructs
        # transport instances) once we move the factory back into
        # transport.py — local import keeps the option open.
        from .transport import SerialStreamTransport, TcpStreamTransport
        if isinstance(self._transport, SerialStreamTransport):
            return StreamInterface.Usb
        if isinstance(self._transport, TcpStreamTransport):
            return StreamInterface.WiFi
        return None

    def _raise_if_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("DaqifiDevice has been closed.")


# ---- DaqifiStreamingDevice ---------------------------------------------------


class DaqifiStreamingDevice(DaqifiDevice):
    """Streaming-capable DAQiFi device.

    Ports the C# ``DaqifiStreamingDevice`` (``Daqifi.Core/Device/
    DaqifiStreamingDevice.cs``). Adds streaming-frequency control,
    start/stop streaming, and SCPI-driven SD-card / network /
    LAN-chip-info facade methods on top of :class:`DaqifiDevice`.

    Text replies (SD listings, error-queue entries, LAN queries) are read
    through :meth:`execute_text_command`, which captures them without
    losing them to the protobuf consumer (#16, #22).
    """

    _WIFI_MODULE_RESTART_DELAY_S = 2.0
    _SD_INTERFACE_SETTLE_DELAY_S = 0.1
    # Total wall-clock cap for an SD-card download. Matches the C#
    # SdCardFileReceiver default — large log files at low USB CDC
    # throughput can take a while, but 30 min is the upper bound.
    _SD_DOWNLOAD_TIMEOUT_S = 30 * 60

    # Idle timeout: if no bytes arrive for this long, give up even if
    # we're still under the total cap. Catches stuck transfers that
    # never reach EOF without waiting the full 30 min.
    _SD_DOWNLOAD_IDLE_TIMEOUT_S = 30.0
    # Match the C# SdCardFileReceiver buffer size — 16 KiB is enough to
    # absorb a USB CDC burst without making the EOF-marker scan window
    # uselessly large.
    _SD_DOWNLOAD_BUFFER_BYTES = 16 * 1024
    # Idle wait between empty serial reads while accumulating raw bytes.
    _SD_DOWNLOAD_POLL_INTERVAL_S = 0.01
    # Without a listed size, how long the line must stay quiet after an
    # end-of-file marker before it is taken as the end of the file rather
    # than the marker text inside it.
    _SD_DOWNLOAD_END_SETTLE_S = 0.25
    # Hard cap for the raw-text capture path (LIST? / DELete responses).
    # A normal SD-card listing on a Nyquist is well under 8 KiB; 512 KiB
    # leaves plenty of slack while bounding memory if a noisy stream
    # never falls quiet.
    _SD_TEXT_CAPTURE_MAX_BYTES = 512 * 1024

    def __init__(
        self,
        name: str,
        transport: Optional[Any] = None,
        ip_address: Optional[str] = None,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> None:
        super().__init__(
            name=name,
            transport=transport,
            ip_address=ip_address,
            message_factory=message_factory,
        )
        self._is_streaming = False
        self._is_logging_to_sd = False
        self._streaming_frequency = 100
        # Tracks the most recently fetched SD-card file listing as
        # ``SdCardFileInfo`` records (matches the SdCardOperations
        # Protocol contract). Updated by ``get_sd_card_files`` and
        # ``delete_sd_card_file`` (which re-LISTs after the delete).
        self._sd_card_files: List[SdCardFileInfo] = []
        # Tracks the most recently APPLIED network configuration so
        # ``network_configuration`` reads the live state without
        # round-tripping the device. Updated at the end of
        # ``update_network_configuration``. Defaults to the upstream
        # parameterless-constructor shape.
        self._network_configuration = NetworkConfiguration()

    # ---- streaming API (IStreamingDevice) ---------------------------------

    @property
    def is_streaming(self) -> bool:
        """Whether this session has streaming running AND the pipeline can
        be believed. A recorded delivery fault (see
        :attr:`delivery_fault`) makes this False: the stream was started,
        but nothing is reading it or the start never reached the device,
        and answering True there is the misreport the #238 residual is
        about."""
        return self._is_streaming and self.delivery_fault is None

    @property
    def streaming_frequency(self) -> int:
        return self._streaming_frequency

    @streaming_frequency.setter
    def streaming_frequency(self, value: int) -> None:
        if value <= 0:
            raise ValueError("streaming_frequency must be positive.")
        self._streaming_frequency = int(value)

    def start_streaming(self) -> None:
        """Start streaming.

        Raises:
            DeliveryFaultError: when a command the device was told to
                execute provably did not reach it, or nothing is reading
                the stream. Streaming cannot be reported honestly over
                that, and reporting it anyway is the #238 residual: the
                call returned True and no frames ever arrived. Reconnect
                to clear it.
        """
        self._raise_if_delivery_faulted("start_streaming")

        # State moves after the command is on the wire, which is what
        # send() returning now means. An earlier revision parked the
        # command and had to bind the state to a callback, because flipping
        # the flag at call time let the wire and the library disagree: a
        # parked STOP replayed after a later START left the device stopped
        # while is_streaming read True. Blocking makes the plain order
        # correct again.
        self.send(scpi_commands.start_streaming(self._streaming_frequency))
        self._is_streaming = True
        self._set_state(DeviceState.Streaming)

    def stop_streaming(self) -> None:
        # State moves after the command is out -- see start_streaming.
        self.send(scpi_commands.stop_streaming())
        self._is_streaming = False
        if self._state == DeviceState.Streaming:
            self._set_state(DeviceState.Ready)

    # ---- USB / transport-shape helpers -----------------------------------

    @property
    def is_usb_connection(self) -> bool:
        """``True`` when the underlying transport is a serial port (USB CDC)."""
        return isinstance(self._transport, SerialStreamTransport)

    @property
    def is_logging_to_sd_card(self) -> bool:
        """Whether THIS session started an SD recording that it hasn't
        stopped. A recording started by another client, another session or
        an earlier process reads ``False``: the firmware has no query for it
        (#187)."""
        return self._is_logging_to_sd

    @property
    def sd_card_files(self) -> Sequence[SdCardFileInfo]:
        """Most recently retrieved file listing (defensive copy).

        Returns the cached :class:`SdCardFileInfo` records from the last
        :meth:`get_sd_card_files` (or :meth:`delete_sd_card_file`,
        which re-LISTs after the delete). Empty before the first call.
        """
        return tuple(self._sd_card_files)

    # ---- SD card facade (SdCardOperations Protocol impl) -----------------
    #
    # The SdCardOperations Protocol lives at daqifi.sdcard.SdCardOperations.
    # All Protocol methods below are sync — see the Protocol's module
    # docstring for the async/sync rationale.

    def enable_sd_card_logging(self) -> None:
        """Low-level helper — enable the SD storage SCPI module only.
        Use :meth:`start_sd_card_logging` for the full Protocol flow."""
        # State moves after the command is out -- see start_streaming.
        self.send(scpi_commands.enable_storage_sd())
        self._is_logging_to_sd = True
        self._forget_sd_card_sizes()

    def connect(
        self, retry_options: Optional[ConnectionRetryOptions] = None,
    ) -> None:
        # A listing from an earlier session isn't a safe download length:
        # the card may have changed while disconnected.
        self._forget_sd_card_sizes()
        super().connect(retry_options)

    def disconnect(self) -> None:
        self._forget_sd_card_sizes()
        super().disconnect()

    def _forget_sd_card_sizes(self) -> None:
        # Logging can grow a listed file, so sizes listed before or during a
        # recording aren't a safe download length; they come back with the
        # next listing taken while not logging.
        self._sd_card_files = [replace(f, size=None) for f in self._sd_card_files]

    def disable_sd_card_logging(self) -> None:
        """Low-level helper — disable the SD storage SCPI module only.
        Use :meth:`stop_sd_card_logging` for the full Protocol flow."""
        # State moves after the command is out -- see start_streaming.
        self.send(scpi_commands.disable_storage_sd())
        if self._is_logging_to_sd:
            self._forget_sd_card_sizes()
        self._is_logging_to_sd = False

    def set_sd_logging_file_name(self, file_name: str) -> None:
        self.send(scpi_commands.set_sd_logging_file_name(file_name))

    @_exclusive_sd_operation
    def request_sd_card_file_list(self) -> None:
        """Issue ``SD:LIST?`` and populate :attr:`sd_card_files` with the
        parsed result.

        Now that ``execute_text_command`` (#22) captures text replies
        without losing them to the protobuf consumer, this method
        actually updates the local ``_sd_card_files`` cache — closes
        daqifi/daqifi-python-core#16. Callers can either inspect
        ``sd_card_files`` after the call OR keep using the higher-level
        :meth:`get_sd_card_files` (which returns the list directly).

        Refused while logging to the SD card: the listing needs
        ``SD:ENAble 1``, and the firmware handles that command by setting
        the SD manager's mode to NONE, which tears down the write session.
        The firmware also refuses ``SD:LIST?`` while a write session is
        busy (#187).

        Stops any stream and doesn't resume it (see
        :meth:`_stop_streaming_for_sd`).
        """
        if not self.is_connected:
            raise DeviceNotConnectedError()
        self._ensure_sd_transfer_supported()
        if self._is_logging_to_sd:
            raise RuntimeError(
                "Cannot list SD card files while logging to the SD card; "
                "stop logging first."
            )

        self._stop_streaming_for_sd("request_sd_card_file_list")

        def _setup_list() -> None:
            self.prepare_sd_interface()
            time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)
            self.send(scpi_commands.get_sd_file_list())
            self.send(scpi_commands.get_last_error())

        try:
            # Empty the error queue first, so the listing's terminator
            # reply is the listing's own.
            self._empty_error_queue_for_listing()
            files = self._read_sd_listing(
                lambda: self.execute_text_command(
                    _setup_list,
                    response_timeout_ms=3000,
                    completion_timeout_ms=1500,
                ),
                sent=self._sd_listing_commands(),
            )
        finally:
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    _log.exception(
                        "Failed to restore LAN interface after SD list"
                    )

        self._sd_card_files = files

    def get_sd_logging_state(self) -> bool:
        """Whether THIS session has the device logging to the SD card.

        The same value as :attr:`is_logging_to_sd_card`, kept for API
        compatibility. Only recordings started through this object count: a
        recording started by another client, another session or an earlier
        process reads ``False``. It sends nothing to the device (#187):

        - the firmware has no SD logging-state query. ``SD:LOGging`` was only
          ever a setter, since renamed ``SD:FILE``, and there is no query for
          the log file name;
        - the firmware logs to SD whenever a stream starts on an interface
          other than WiFi while SD is enabled with a file name set
          (``SCPI_StartStreaming``'s ``sdLoggingRequested``), so the
          streaming interface alone can't tell;
        - the older ``SD:ENAble 1`` probe made the firmware tear down the
          very recording it was asking about, and a text query during a USB
          stream is mixed in with the binary samples.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError()
        return self._is_logging_to_sd

    def delete_sd_file(self, file_name: str) -> None:
        """Low-level helper — emit DELete only. Prefer
        :meth:`delete_sd_card_file`, which validates + re-LISTs."""
        self.send(scpi_commands.delete_sd_file(file_name))

    @_exclusive_sd_operation
    def start_sd_card_logging(
        self,
        file_name: Optional[str] = None,
        channel_mask: Optional[str] = None,
        format: SdCardLogFormat = SdCardLogFormat.PROTOBUF,
    ) -> str:
        """Begin logging to the SD card, and return the log file's name
        (the generated one when ``file_name`` isn't given).

        Mirrors the canonical C# ``StartSdCardLoggingAsync`` flow
        (``Daqifi.Core/Device/DaqifiStreamingDevice.cs``). LAN is
        disabled before SD is enabled because the SD card and WiFi/LAN
        share the SPI bus on Nyquist hardware.

        ``channel_mask`` is a decimal bitmask of the ADC channels to log
        (``"84"`` enables channels 2, 4 and 6).
        """
        if not self.is_connected:
            raise DeviceNotConnectedError()
        if not self.is_usb_connection:
            raise RuntimeError(
                "SD card logging requires a USB/serial connection: starting "
                "a logging session disables the LAN interface to give the "
                "SD card the shared SPI bus, which over a network connection "
                "would drop the link the command arrived on. Listing, "
                "downloading, deleting and formatting work over WiFi on "
                "firmware v3.7.0 and later."
            )

        ext_by_format = {
            SdCardLogFormat.PROTOBUF: ".bin",
            SdCardLogFormat.JSON: ".json",
            SdCardLogFormat.CSV: ".csv",
        }
        if file_name and file_name.strip():
            log_file_name = file_name
        else:
            log_file_name = (
                f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                f"{ext_by_format.get(format, '.bin')}"
            )
        _validate_sd_card_file_name(log_file_name)

        # Wrap the configure-then-start sequence in try/except so a
        # partial failure (e.g. set_sd_logging_file_name times out
        # after LAN was already disabled) doesn't strand the device
        # with LAN off and SD half-configured. On success we exit with
        # LAN intentionally disabled — that's the steady state for
        # SD logging and matches the C# impl's post-condition.
        try:
            self.send(scpi_commands.disable_network_lan())
            time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)

            self.send(scpi_commands.enable_storage_sd())
            time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)

            self.send(scpi_commands.set_stream_interface(StreamInterface.SdCard))
            time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)

            self.send(scpi_commands.set_sd_logging_file_name(log_file_name))
            time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)

            # SdCardLogFormat integer values map 1:1 to SYSTem:STReam:FORmat
            self.send(ScpiMessage(f"SYSTem:STReam:FORmat {int(format)}"))
            time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)

            if channel_mask and channel_mask.strip():
                self.send(scpi_commands.enable_adc_channels(channel_mask))
                time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)

            self.send(scpi_commands.start_streaming(self._streaming_frequency))
        except Exception:
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    # Best-effort recovery; surface the original error.
                    pass
            raise

        self._is_logging_to_sd = True
        self._forget_sd_card_sizes()
        self._is_streaming = True
        self._set_state(DeviceState.Streaming)
        return log_file_name

    @_exclusive_sd_operation
    def stop_sd_card_logging(self) -> None:
        """Stop the active SD-card logging session and restore LAN."""
        if not self.is_connected:
            raise DeviceNotConnectedError()

        # Defensive: always send stop streaming even if state is stale.
        self.send(scpi_commands.stop_streaming())
        self._is_streaming = False
        if self._state == DeviceState.Streaming:
            self._set_state(DeviceState.Ready)

        self.send(scpi_commands.disable_storage_sd())

        if self.is_usb_connection:
            self.send(scpi_commands.set_stream_interface(StreamInterface.Usb))

        # Re-enable LAN — start_sd_card_logging disabled it.
        self.send(scpi_commands.enable_network_lan())
        # The stream format is one global setting on the device, shared with
        # the live stream, and a JSON/CSV session changed it; the library's
        # live path decodes protobuf only (#163).
        self.send(scpi_commands.set_protobuf_stream_format())
        if self._is_logging_to_sd:
            self._forget_sd_card_sizes()
        self._is_logging_to_sd = False

    @_exclusive_sd_operation
    def delete_sd_card_file(self, file_name: str) -> None:
        """Delete ``file_name`` from the SD card and refresh the cache.

        Validates the filename, switches the SPI bus to the SD interface,
        sends the DELete + LIST? pair via the raw-capture path, and
        parses the response to refresh ``sd_card_files``. Restores LAN
        in a finally so a hard error doesn't leave the bus on the SD
        side. Stops any stream and doesn't resume it (see
        :meth:`_stop_streaming_for_sd`).

        Once the DELete is on its way, a failure short of the device's
        refusal means the file may be gone or not:
        :class:`SdCardDeleteOutcomeUnknownException`, with the cause chained.
        A failure before it is sent raises as itself: nothing was deleted.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError()
        self._ensure_sd_transfer_supported()
        if self._is_logging_to_sd:
            raise RuntimeError("Cannot delete files while logging to SD card.")
        if file_name is None or not file_name.strip():
            raise ValueError("file_name must not be empty.")
        _validate_sd_card_file_name(file_name)

        # Defensive: stop any active streaming before reusing the consumer.
        self._stop_streaming_for_sd("delete_sd_card_file")
        delete_sent = False

        def _send_delete() -> None:
            nonlocal delete_sent
            if not self.is_connected:  # nothing goes out: a definite failure
                raise DeviceNotConnectedError()
            # Before the write: one that fails part-way may still have
            # reached the device.
            delete_sent = True
            self.send(scpi_commands.delete_sd_file(file_name))

        def _capture() -> List[str]:
            return self._execute_raw_text_capture(
                lambda: (
                    self.prepare_sd_interface(),
                    time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S),
                    _send_delete(),
                    self.send(scpi_commands.get_last_error()),
                    self.send(scpi_commands.get_sd_file_list()),
                    self.send(scpi_commands.get_last_error()),
                ),
                response_timeout_s=3.0,
            )

        from .exceptions import (
            SdCardDeleteOutcomeUnknownException,
            SdCardListIncompleteException,
            SdCardOperationException,
        )

        try:
            # No retry: it would send the DELete again.
            # Empty the error queue first, so the listing's terminator
            # reply is the listing's own.
            self._empty_error_queue_for_listing()
            try:
                files = self._read_sd_listing(
                    _capture,
                    sent=self._sd_listing_commands(
                        scpi_commands.delete_sd_file(file_name)
                    ),
                    leading_reply=True,
                )
            except SdCardListIncompleteException as e:
                # The DELete was sent, but its confirmation (or the
                # listing after it) never came back complete: the file may
                # be gone or not. Say so, rather than report a failure.
                raise SdCardDeleteOutcomeUnknownException(
                    file_name, e.raw_device_response, e.reason
                ) from e
            except SdCardOperationException:
                raise  # the device's own answer
            except Exception as e:
                if not delete_sent:
                    raise
                raise SdCardDeleteOutcomeUnknownException(
                    file_name, (), str(e)
                ) from e
        finally:
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    pass

        self._sd_card_files = files
        if self._is_logging_to_sd:
            self._forget_sd_card_sizes()

    @_exclusive_sd_operation
    def format_sd_card(self) -> None:
        """Erase the entire SD card.

        Routes through ``prepare_sd_interface`` so the LAN-disable
        precondition documented on ``enable_storage_sd`` is honored
        (LAN must be off before SD takes the SPI bus).

        Stops any stream and doesn't resume it (see
        :meth:`_stop_streaming_for_sd`). The erase runs on after this
        returns, so start streaming again once it's done.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError()
        self._ensure_sd_transfer_supported()
        if self._is_logging_to_sd:
            raise RuntimeError("Cannot format SD card while logging.")

        self._stop_streaming_for_sd("format_sd_card")

        try:
            self.prepare_sd_interface()
            self.send(scpi_commands.format_sd_card())
        finally:
            # Restore LAN — same finally pattern as get/delete/download.
            # The format command continues asynchronously on the device,
            # but we don't need to hold the SPI bus for it (firmware
            # finishes the erase whether or not LAN is back on).
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    pass

    @_exclusive_sd_operation
    def get_sd_card_files(self) -> Sequence[SdCardFileInfo]:
        """Fetch + return the SD-card file listing (and refresh the cache).
        Stops any stream and doesn't resume it (see
        :meth:`_stop_streaming_for_sd`)."""
        if not self.is_connected:
            raise DeviceNotConnectedError()
        self._ensure_sd_transfer_supported()
        if self._is_logging_to_sd:
            # Listing stops the stream and hands the bus back to LAN, which
            # would end the recording; a call that looks read-only must not
            # (#163). No listing is safe during a recording (#187).
            raise RuntimeError(
                "Cannot list SD card files while logging to the SD card; "
                "stop logging first."
            )

        self._stop_streaming_for_sd("get_sd_card_files")

        def _capture() -> List[str]:
            return self._execute_raw_text_capture(
                lambda: (
                    self.prepare_sd_interface(),
                    time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S),
                    self.send(scpi_commands.get_sd_file_list()),
                    self.send(scpi_commands.get_last_error()),
                ),
                response_timeout_s=3.0,
            )

        try:
            # Empty the error queue first, so the listing's terminator
            # reply is the listing's own.
            self._empty_error_queue_for_listing()
            files = self._read_sd_listing(
                _capture,
                sent=self._sd_listing_commands(),
            )
        finally:
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    pass

        self._sd_card_files = files
        return tuple(files)

    @_exclusive_sd_operation
    def download_sd_card_file(
        self,
        file_name: str,
        destination_stream: IO[bytes],
        progress: Optional[Callable[[SdCardTransferProgress], None]] = None,
        expected_size: Optional[int] = None,
    ) -> SdCardDownloadResult:
        """Stream a file from the SD card into ``destination_stream``.

        Mirrors the canonical C# ``DownloadSdCardFileAsync`` flow:

        1. Validate connection + USB transport + filename.
        2. Stop any stream (issue daqifi-core#118 pattern). It isn't resumed
           (see :meth:`_stop_streaming_for_sd`).
        3. Pause the protobuf consumer + read raw bytes from the serial
           port until the ``__END_OF_FILE__`` marker arrives.
        4. Restore LAN interface in a ``finally`` (best-effort).

        ``destination_stream`` is any binary-writable file-like object
        (``.write(bytes) -> int``).

        ``expected_size`` is the file's size; when omitted, the size from
        the last SD listing taken while not logging is used, if it has one.
        With a size, exactly that many bytes are the file and the marker
        must follow them alone (only a line ending or prompt after it until
        the line goes quiet); a short or long transfer raises, and the
        result has ``size_verified=True``. Without one, a marker at the end
        of the transfer ends it and ``size_verified`` is False. See
        :class:`SdCardDownloadResult` for the limit of that check
        (daqifi-nyquist-firmware#1180).
        """
        if not self.is_connected:
            raise DeviceNotConnectedError()
        self._ensure_sd_transfer_supported()
        if file_name is None or not file_name.strip():
            raise ValueError("file_name must not be empty.")
        if destination_stream is None:
            raise TypeError("destination_stream must not be None.")
        _validate_sd_card_file_name(file_name)
        if self._is_logging_to_sd:
            raise RuntimeError("Cannot download files while logging to SD card.")
        if expected_size is None:
            expected_size = next(
                (f.size for f in self._sd_card_files if f.file_name == file_name), None
            )
        elif (
            not isinstance(expected_size, int) or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise ValueError(f"expected_size must be a non-negative int (got {expected_size!r})")

        self._stop_streaming_for_sd("download_sd_card_file")

        start_time = time.monotonic()
        bytes_written = 0
        try:
            bytes_written = self._execute_raw_capture(
                lambda raw_stream: self._receive_sd_file(
                    raw_stream,
                    destination_stream,
                    file_name,
                    progress,
                    expected_size,
                ),
                pre_capture=lambda: (
                    self.prepare_sd_interface(),
                    time.sleep(0.05),
                    self.send(scpi_commands.get_sd_file(file_name)),
                ),
            )
        finally:
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    pass

        elapsed = timedelta(seconds=time.monotonic() - start_time)
        return SdCardDownloadResult(
            file_name=file_name,
            file_size=bytes_written,
            duration=elapsed,
            size_verified=expected_size is not None,
        )

    def download_sd_card_file_to_temp(
        self,
        file_name: str,
        progress: Optional[Callable[[SdCardTransferProgress], None]] = None,
        expected_size: Optional[int] = None,
    ) -> SdCardDownloadResult:
        """Download to a temporary on-disk file; the result carries the path.
        ``expected_size`` is as for :meth:`download_sd_card_file`."""
        # delete=False so the path stays valid for the caller after the
        # ``with`` block — the caller is responsible for cleanup.
        with tempfile.NamedTemporaryFile(
            prefix="daqifi_sd_",
            suffix=os.path.splitext(file_name)[1] or ".bin",
            delete=False,
        ) as tmp:
            tmp_path = tmp.name

        try:
            with open(tmp_path, "wb") as out:
                base = self.download_sd_card_file(
                    file_name, out, progress, expected_size=expected_size
                )
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return SdCardDownloadResult(
            file_name=base.file_name,
            file_size=base.file_size,
            duration=base.duration,
            file_path=tmp_path,
            size_verified=base.size_verified,
        )

    # ---- raw-capture helpers (consumer-pause pattern) --------------------

    def _sd_listing_commands(self, *extra: ScpiMessage) -> List[ScpiMessage]:
        """Every command a listing exchange sends, whose echoes it drops."""
        return [
            scpi_commands.disable_network_lan(),
            scpi_commands.enable_storage_sd(),
            *extra,
            scpi_commands.get_sd_file_list(),
            scpi_commands.get_last_error(),
        ]

    def _empty_error_queue_for_listing(self) -> None:
        """Drain the error queue before a listing, so the listing's
        terminator code can be attributed to it. A drain that can't confirm
        the queue empty (``drain_errors``' inconclusive sentinel) raises
        rather than letting an old error be blamed on the listing."""
        from ._internal import scpi as _scpi
        from .exceptions import SdCardOperationException

        popped = self.drain_errors(empty_budget=1)
        if popped and _scpi.parse_system_error(popped[-1])[0] == _DRAIN_INCONCLUSIVE_CODE:
            raise SdCardOperationException(
                "The device's error queue could not be emptied before the SD "
                "listing, so its result could not be attributed.",
                popped,
                popped[-1],
            )

    def _ensure_sd_transfer_supported(self) -> None:
        """Refuse an SD file operation (list, download, delete, format) the
        firmware can't serve on this transport.

        Over USB they always work. Over WiFi/TCP they need firmware v3.7.0
        or later, which arbitrates the shared SPI bus and routes the SD reply
        back over the requesting TCP connection (firmware #598/#599,
        ``SCPIStorageSD.c``); before that the command would stall the bus.
        An unknown firmware version is refused too. Port of daqifi-core
        6baace2 / 2521fac.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError()
        if self.is_usb_connection:
            return
        version = FirmwareVersion.parse(self.metadata.firmware_version)
        if version is None or version < _SD_OVER_WIFI_SINCE:
            raise FeatureNotSupportedError(
                "SD card file operations over WiFi need firmware v3.7.0 or "
                "later; this device reports "
                f"{self.metadata.firmware_version or 'no firmware version'}. "
                "Use a USB connection, or update the firmware."
            )

    def _stop_streaming_for_sd(self, operation: str) -> None:
        """Stop the stream for an SD operation. It isn't started again: the
        caller decides when (the device can't be expected to stream while
        it's asked for other things). The device is left Ready, with a
        warning if a stream was running. The stop is sent even if the flag
        says idle (it may be stale)."""
        was_streaming = self._is_streaming
        self.stop_streaming()
        if was_streaming:
            _log.warning(
                "%s stopped streaming; it isn't resumed. Start streaming "
                "again when you're ready.",
                operation,
            )

    def _sd_listing_marker_expected(self) -> bool:
        """Whether the firmware ends every listing with ``__END_OF_LIST__``:
        since v3.8.0 (firmware #794). Unknown or older firmware is not
        expected to, and a listing without one is judged on its own."""
        version = FirmwareVersion.parse(self.metadata.firmware_version)
        return version is not None and version >= _SD_LIST_MARKER_SINCE

    def _read_sd_listing(
        self,
        capture: Callable[[], List[str]],
        sent: Sequence[ScpiMessage],
        leading_reply: bool = False,
    ) -> List[SdCardFileInfo]:
        """Run one listing capture whose last command is ``SYSTem:ERRor?``,
        and return the files (#188).

        There is no automatic retry. A timed-out attempt's tail can arrive
        during a retry, and no quiet interval proves it has finished, so the
        caller decides whether to try again.

        - Echoes of the commands in ``sent`` (with ``SYSTem:ECHO 1`` the
          device repeats each command) are dropped, and a reply glued to the
          end-of-listing marker is split off it.
        - ``leading_reply``: the delete path sends ``SYSTem:ERRor?`` straight
          after ``DELete`` too, so the first reply is the DELete's (non-zero:
          the device refused it, :class:`SdCardOperationException`), and the
          listing's terminator stays the listing's own.
        - No terminator reply means the reply never finished:
          :class:`SdCardListIncompleteException`.
        - The error queue was drained before the exchange, so a non-zero
          terminator code is this listing's failure (the firmware answers an
          aborted listing with -200).
        - Then no card, filesystem errors, a missing directory and an
          ``INCOMPLETE`` / ``FAILED`` marker raise. So does a missing
          marker on firmware that always sends one (an aborted listing has
          none). On older firmware an empty unmarked listing is an empty
          directory only with a clean (0) terminator.
        """
        from ._internal import scpi as _scpi
        from .exceptions import SdCardListIncompleteException, SdCardOperationException
        from .sdcard import (
            sd_card_listing_status,
            split_glued_error_reply,
            split_glued_list_end,
            throw_if_sd_card_reported_error,
        )

        echoes = {m.data.strip() for m in sent}
        lines = [
            line for line in _strip_prompt_prefixes(
                split_glued_list_end(
                    split_glued_error_reply(_strip_prompt_prefixes(capture()))
                )
            )
            if line.strip() and line.strip() not in echoes
        ]
        # An explicit report (no card, a directory that can't be opened)
        # comes without a marker, so it is classified before the reply's
        # ending is judged.
        throw_if_sd_card_reported_error(lines)
        if leading_reply:
            # A refusal comes twice: SCPI_ErrorPush writes "**ERROR ..." to
            # the wire ahead of the query's reply, and queues it.
            volunteered = list(
                takewhile(lambda line: _scpi.contains_scpi_error([line]), lines)
            )
            reply = _scpi.find_system_error_reply(lines[len(volunteered):][:1])
            if reply is None:
                raise SdCardListIncompleteException(
                    lines, "no reply to the SYSTem:ERRor? sent after DELete"
                )
            if volunteered or _scpi.parse_system_error(reply)[0] != 0:
                refusal = (volunteered[0] if volunteered else reply).strip()
                raise SdCardOperationException(
                    f"The device refused to delete the file: {refusal}",
                    lines,
                    refusal,
                )
            lines = lines[1:]
        marker_expected = self._sd_listing_marker_expected()
        split = _split_at_list_terminator(lines, marker_expected)
        if split is None:
            raise SdCardListIncompleteException(
                lines, "the reply ended before the device answered SYSTem:ERRor?"
            )
        listing, terminator = split
        code, _ = _scpi.parse_system_error(terminator)
        check_sd_card_listing(listing)
        if code != 0:
            raise SdCardListIncompleteException(
                lines, f"the device reported {terminator}"
            )
        # 3.8.0+ always marks a listing, so no marker means an abort. On
        # older firmware the clean terminator checked above is the evidence,
        # an empty unmarked listing included (an empty directory).
        if marker_expected and sd_card_listing_status(listing) == "unterminated":
            raise SdCardListIncompleteException(lines, "no end-of-listing marker")
        return parse_sd_card_file_list(listing)

    def _execute_raw_text_capture(
        self,
        send_action: Callable[[], Any],
        response_timeout_s: float,
    ) -> List[str]:
        """Run ``send_action`` then accumulate decoded text until idle.

        Pauses the protobuf consumer so it doesn't eat the SCPI text
        response, fires the SCPI commands, then reads from the underlying
        serial stream until ``response_timeout_s`` elapses without new
        bytes arriving. Returns the response split on CR/LF.
        """
        def _accumulate(raw_stream: Any) -> List[str]:
            buf = bytearray()
            deadline = time.monotonic() + response_timeout_s
            while time.monotonic() < deadline:
                # Defensive bound: if a noisy stream keeps trickling
                # data forever, the idle-deadline reset would never
                # fire. Cap the buffer so the capture aborts cleanly
                # instead of growing without limit.
                if len(buf) >= self._SD_TEXT_CAPTURE_MAX_BYTES:
                    raise TimeoutError(
                        f"SD-card text response exceeded "
                        f"{self._SD_TEXT_CAPTURE_MAX_BYTES} bytes; "
                        f"aborting capture (likely a stuck stream)."
                    )
                # Both transport streams have in_waiting (serial: pyserial;
                # TCP: _SocketStream). One without it fails here rather than
                # reading as a line that never has anything waiting.
                in_waiting = raw_stream.in_waiting
                if in_waiting:
                    chunk = raw_stream.read(in_waiting)
                    if chunk:
                        buf.extend(chunk)
                        deadline = time.monotonic() + response_timeout_s
                else:
                    time.sleep(self._SD_DOWNLOAD_POLL_INTERVAL_S)
            text = buf.decode("ascii", errors="ignore")
            # Split on either line terminator; drop empty entries and
            # the firmware ``DAQIFI>`` prompt that may precede the
            # first content line. C# uses ExecuteTextCommandAsync which
            # already strips the prompt — we have to do it here.
            lines: List[str] = []
            for ln in text.replace("\r", "\n").split("\n"):
                ln = ln.strip()
                if not ln:
                    continue
                # Drop a leading prompt: "DAQIFI>" or "DAQIFI> <content>".
                if ln.upper().startswith("DAQIFI>"):
                    rest = ln[len("DAQIFI>"):].lstrip()
                    if not rest:
                        continue
                    ln = rest
                lines.append(ln)
            return lines

        return self._execute_raw_capture(_accumulate, pre_capture=send_action)

    def _execute_raw_capture(
        self,
        capture_action: Callable[[Any], Any],
        pre_capture: Optional[Callable[[], Any]] = None,
    ) -> Any:
        """Pause the protobuf consumer, run ``capture_action(raw_stream)``,
        restart the consumer.

        Mirrors the C# ``ExecuteRawCaptureAsync`` pattern. ``pre_capture``
        runs *after* the consumer has paused but *before* ``capture_action``
        receives the raw stream — useful for sending SCPI commands whose
        response should not be eaten by the consumer.

        Marked as a capture for its whole duration, including
        ``capture_action``, because that is where a download's progress
        callback runs: an SD operation started there would read this
        download's remaining bytes and its EOF marker off the same stream.
        """
        with self._capture(
            "a raw SD capture cannot run inside another capture on the same "
            "device (for example from a download's progress callback)."
        ):
            return self._execute_raw_capture_marked(capture_action, pre_capture)

    def _execute_raw_capture_marked(
        self,
        capture_action: Callable[[Any], Any],
        pre_capture: Optional[Callable[[], Any]] = None,
    ) -> Any:
        if self._transport is None:
            raise RuntimeError("Device has no transport configured.")
        raw_stream = self._transport.stream
        if raw_stream is None:
            raise RuntimeError("Transport has no readable stream.")

        consumer = self._consumer
        was_running = consumer is not None and consumer.is_running
        if consumer is not None and was_running:
            stopped = consumer.stop_safely()
            if stopped is False:
                # `stop_safely` returns False for TWO reasons: called from
                # the consumer thread, or the join timed out with the
                # thread still alive. The first is refused upstream in
                # `_acquire_text_exchange_lock`, which every path here
                # passes through, so this is the timeout. Refuse it too —
                # capturing from a stream the consumer is still reading is
                # the interleaving the lock exists to prevent.
                raise RuntimeError(
                    "Cannot perform raw-capture SD-card operations: the "
                    "protobuf consumer did not stop within the timeout and "
                    "is still reading the stream."
                )
            try:
                consumer.clear_buffer()
            except Exception:
                pass

        try:
            if pre_capture is not None:
                pre_capture()
            return capture_action(raw_stream)
        finally:
            if consumer is not None and was_running:
                try:
                    consumer.clear_buffer()
                except Exception:
                    pass
            if (
                consumer is not None
                and was_running
                and self._consumer_can_restart_on(raw_stream)
            ):
                try:
                    consumer.start()
                except Exception as restart_exc:
                    # Don't raise over a successful capture (would
                    # swallow the caller's actual return value), but a
                    # silent failure leaves the protobuf pipeline dead
                    # — log loudly so the regression is visible.
                    _log.exception(
                        "raw-capture: failed to restart protobuf "
                        "consumer after SD-card op; protobuf message "
                        "dispatch will be dead until the next "
                        "reconnect. Underlying error: %s",
                        restart_exc,
                    )

    def _receive_sd_file(
        self,
        raw_stream: Any,
        destination_stream: IO[bytes],
        file_name: str,
        progress: Optional[Callable[[SdCardTransferProgress], None]],
        expected_size: Optional[int] = None,
    ) -> int:
        """Read bytes from ``raw_stream`` until ``__END_OF_FILE__``.

        With ``expected_size`` (the size SD:LIST? reported) exactly that many
        bytes are the file, whatever they contain, and the marker must follow
        them directly; a short read, a long read or a missing trailer raises.
        Without it the marker counts only at the END of the transfer (nothing
        but a line ending or prompt after it until the line goes quiet), so
        the token inside a file no longer truncates it (#163); a transfer
        that stops early can't be detected that way.

        Ports the chunk-boundary-aware EOF-marker scan from the C#
        ``SdCardFileReceiver``. Writes everything before the marker to
        ``destination_stream`` and returns the byte count. Strips a
        leading SCPI echo + ``DAQIFI>`` prompt from the head of the
        response so the destination only sees the file payload.
        """
        from .exceptions import SdCardTransferErrorException

        marker = END_OF_FILE_MARKER
        marker_len = len(marker)
        error_marker = TRANSFER_ERROR_MARKER
        error_len = len(error_marker)
        # Known size: tells the device's __TRANSFER_ERROR__ (a read failure
        # part-way through the file) from content containing its text.
        sized = _SizedTransfer(expected_size) if expected_size is not None else None
        body_done = False
        # Two deadlines: a hard total cap (so a trickling stream can't
        # run forever) AND an idle cap (so a stuck stream gives up
        # quickly without waiting the full total).
        start = time.monotonic()
        total_deadline = start + self._SD_DOWNLOAD_TIMEOUT_S
        idle_deadline = start + self._SD_DOWNLOAD_IDLE_TIMEOUT_S
        head_buf = bytearray()
        head_done = False
        bytes_written = 0
        # Keep the trailing window equal to marker_len so a marker
        # straddling chunks still gets detected.
        trailing = bytearray()

        def _strip_echo_prefix(buf: bytearray) -> bytes:
            """Drop a leading ``DAQIFI>`` prompt (head of buffer only).

            Anchored to the start so a prompt-shaped sequence inside the
            file payload (binary downloads can hit those bytes by chance)
            doesn't get mistakenly stripped — ``buf.find()`` would have
            corrupted the file in that case.
            """
            b = bytes(buf)
            # Firmware sometimes precedes the prompt with CR/LF; those are
            # dropped only together with a prompt. A file starting with
            # 0x0A or 0x0D keeps them (#163).
            lead = b[: len(b) - len(b.lstrip(b"\r\n"))]
            rest = b[len(lead):]
            for prompt in (b"DAQIFI> ", b"DAQIFI>"):
                if rest.startswith(prompt):
                    return rest[len(prompt):]
            return b

        last_data = start
        settle_s = self._SD_DOWNLOAD_END_SETTLE_S
        while True:
            now = time.monotonic()
            if now >= total_deadline:
                # As at the idle timeout: an open transfer-error candidate
                # was the device's marker.
                outcome = sized.finish() if sized is not None else None
                if outcome is not None:
                    raise SdCardTransferErrorException(file_name, outcome[1])
                raise TimeoutError(
                    f"SD card file download exceeded total cap of "
                    f"{self._SD_DOWNLOAD_TIMEOUT_S:.0f}s "
                    f"({bytes_written} bytes written, no EOF marker)."
                )
            in_waiting = raw_stream.in_waiting  # serial and TCP streams both have it
            chunk = (
                raw_stream.read(min(in_waiting, self._SD_DOWNLOAD_BUFFER_BYTES))
                if in_waiting else b""
            )
            if not chunk:
                quiet = now - last_data >= settle_s or now >= idle_deadline
                # No size: the device's read-error marker ending what came,
                # then quiet, as the end-of-file marker is taken. The bytes
                # before it are real content, but the file is incomplete
                # (firmware #725, upstream receiver). With a size, the
                # sized-transfer rules decide (below).
                error_at = (
                    _eof_marker_start(trailing, error_marker)
                    if expected_size is None and head_done else -1
                )
                if quiet and error_at >= 0:
                    payload = bytes(trailing[:error_at])
                    if payload:
                        _write_all(destination_stream, payload, total_deadline)
                        bytes_written += len(payload)
                    raise SdCardTransferErrorException(file_name, bytes_written)
                if (
                    expected_size is None
                    and head_done
                    and _ends_with_eof_marker(trailing)
                    and quiet
                ):
                    # The line went quiet with the marker at the end.
                    payload = bytes(trailing[: _eof_marker_start(trailing)])
                    if payload:
                        _write_all(destination_stream, payload, total_deadline)
                        bytes_written += len(payload)
                    if progress is not None:
                        progress(SdCardTransferProgress(bytes_written, file_name))
                    return bytes_written
                if body_done and quiet:
                    # The listed size, the exact marker, and then only a
                    # line ending or prompt before the line went quiet.
                    return bytes_written
                if now >= idle_deadline:
                    # Idle with a transfer-error candidate still open: that
                    # was the device's marker (content would have gone on).
                    outcome = sized.finish() if sized is not None else None
                    if outcome is not None:
                        raise SdCardTransferErrorException(file_name, outcome[1])
                    received = bytes_written + (len(sized.pending) if sized else 0)
                    short = (
                        f" (short read: {received} of {expected_size} bytes)"
                        if expected_size is not None and received < expected_size
                        else ""
                    )
                    raise TimeoutError(
                        f"SD card file download stalled for "
                        f"{self._SD_DOWNLOAD_IDLE_TIMEOUT_S:.0f}s with no "
                        f"data ({bytes_written} bytes written, no EOF marker){short}."
                    )
                time.sleep(self._SD_DOWNLOAD_POLL_INTERVAL_S)
                continue
            # Reset only the idle deadline on incoming data — the total
            # deadline stays fixed at start + total_timeout so a slow
            # trickle still aborts after the documented cap.
            last_data = time.monotonic()
            idle_deadline = last_data + self._SD_DOWNLOAD_IDLE_TIMEOUT_S
            # Buffer the head until we've seen enough bytes to clip the
            # echo + prompt prefix; only ~200 bytes are needed in practice.
            if not head_done:
                head_buf.extend(chunk)
                if len(head_buf) >= 256 or marker in head_buf or error_marker in head_buf:
                    chunk = _strip_echo_prefix(head_buf)
                    head_buf = bytearray()
                    head_done = True
                else:
                    continue
            if expected_size is not None:
                # Exact length known from the listing: the file is exactly
                # that many bytes, whatever they contain (the end-of-file
                # token may occur inside it), and the marker must follow
                # directly. Anything else is an error, not a success (#163).
                if body_done:
                    # Past the marker: nothing but a line ending or prompt.
                    trailing.extend(chunk)
                    if not _is_eof_trailer(bytes(trailing[marker_len:])):
                        raise IOError(
                            f"SD card file {file_name!r}: {expected_size} bytes "
                            "received but no end-of-file marker alone after them; "
                            "the file is longer than listed or the transfer is "
                            "corrupt."
                        )
                else:
                    body, outcome = sized.feed(bytes(chunk))
                    if body:
                        _write_all(destination_stream, body, total_deadline)
                        bytes_written += len(body)
                        if progress is not None:
                            progress(SdCardTransferProgress(bytes_written, file_name))
                    if outcome is not None:
                        kind, value = outcome
                        if kind == "transfer_error":
                            raise SdCardTransferErrorException(file_name, value)
                        if kind == "mismatch" or not _is_eof_trailer(value):
                            raise IOError(
                                f"SD card file {file_name!r}: {expected_size} bytes "
                                "received but no end-of-file marker alone after "
                                "them; the file is longer than listed or the "
                                "transfer is corrupt."
                            )
                        # The exact marker: the end is taken when the line
                        # goes quiet (above), so more data still fails.
                        trailing = bytearray(marker + value)
                        body_done = True
                # Time spent waiting on the destination isn't the line
                # being idle or quiet (#163 audit): restart both clocks.
                last_data = time.monotonic()
                idle_deadline = last_data + self._SD_DOWNLOAD_IDLE_TIMEOUT_S
                continue
            # No size: hold back enough to recognise a marker plus its
            # trailer at the end; everything before that is file content,
            # even if it contains the marker.
            trailing.extend(chunk)
            keep = max(marker_len, error_len) + _EOF_TRAILER_MAX
            if len(trailing) > keep:
                writable = bytes(trailing[: len(trailing) - keep])
                _write_all(destination_stream, writable, total_deadline)
                bytes_written += len(writable)
                del trailing[: len(trailing) - keep]
                if progress is not None:
                    progress(SdCardTransferProgress(bytes_written, file_name))
                last_data = time.monotonic()
                idle_deadline = last_data + self._SD_DOWNLOAD_IDLE_TIMEOUT_S

    # ---- Network facade (NetworkConfigurable Protocol impl) ---------------

    @property
    def network_configuration(self) -> NetworkConfiguration:
        """Most recently applied network configuration **as tracked by
        this Python object**.

        This is a process-local snapshot, not a live query of the
        device. It's updated at the end of every successful
        :meth:`update_network_configuration` call, mirroring the C#
        ``DaqifiStreamingDevice._networkConfiguration`` field which
        is similarly process-local.

        It does not query ``SYST:COMM:LAN:SSID? / NETTYPE? / SECURITY?``
        to rebuild the configuration from the live device, although
        :meth:`execute_text_command` could now read those replies (#16).

        Caveats — the snapshot can drift from the device's actual
        config if:

        * The device is reconfigured out-of-band (another process,
          firmware reset, factory reset).
        * The Python object is freshly constructed against an already-
          provisioned device — the snapshot defaults to the upstream
          parameterless-constructor shape rather than the device's
          stored config.

        Returns a defensive copy so callers can mutate without
        affecting the live snapshot.
        """
        return self._network_configuration.clone()

    def update_network_configuration(
        self, configuration: NetworkConfiguration
    ) -> None:
        # ⛔ OWNS THE EXCHANGE FOR ITS WHOLE BODY. Structural, not a
        # pattern fix: a public helper that sends more than one command, or
        # pairs a send with a delay or a state change, must not let another
        # flow's command land between its steps -- its later steps assume
        # the earlier ones took effect on a device nobody else has touched.
        # Re-entrant, so a caller already inside an exchange (the
        # SD operations call these) still works.
        # ⛔ OWNERSHIP WITHOUT THE NESTED-CAPTURE REFUSAL. This helper only
        # WRITES and sleeps, and it is legitimately called from inside a
        # capture -- as a setup_action, or via
        # execute_text_command(device.update_network_configuration). The
        # refusing form rejected it before it sent anything, which the base
        # did not: a regression introduced by wrapping it. See
        # _run_owning_exchange for which form a helper needs.
        return self._run_owning_exchange(
            lambda: self._update_network_configuration_locked(configuration),
            "update_network_configuration",
        )

    def _update_network_configuration_locked(
        self, configuration: NetworkConfiguration
    ) -> None:
        if configuration is None:
            raise TypeError("configuration must not be None.")
        if not self.is_connected:
            raise DeviceNotConnectedError()
        if not configuration.ssid:
            raise ValueError("WiFi SSID must be a non-empty string.")
        if (
            configuration.security_type == WifiSecurityType.WPA_PSK_PHRASE
            and not configuration.password
        ):
            raise ValueError("WPA security requires a non-empty password.")

        if self._is_streaming:
            self.stop_streaming()

        if configuration.mode == WifiMode.EXISTING_NETWORK:
            self.send(scpi_commands.set_network_wifi_mode_existing())
        elif configuration.mode == WifiMode.SELF_HOSTED:
            self.send(scpi_commands.set_network_wifi_mode_self_hosted())
        else:
            raise ValueError(f"Unsupported WiFi mode: {configuration.mode!r}")

        self.send(scpi_commands.set_network_wifi_ssid(configuration.ssid))

        if configuration.security_type == WifiSecurityType.NONE:
            self.send(scpi_commands.set_network_wifi_security_open())
        elif configuration.security_type == WifiSecurityType.WPA_PSK_PHRASE:
            self.send(scpi_commands.set_network_wifi_security_wpa())
            self.send(
                scpi_commands.set_network_wifi_password(configuration.password)
            )
        else:
            raise ValueError(
                f"Unsupported WiFi security type: {configuration.security_type!r}"
            )

        self.prepare_lan_interface()
        self.send(scpi_commands.apply_and_save_network_lan())
        time.sleep(self._WIFI_MODULE_RESTART_DELAY_S)

        self._network_configuration = configuration.clone()

    def prepare_lan_interface(self) -> None:
        # ⛔ OWNS THE EXCHANGE FOR ITS WHOLE BODY. Structural, not a
        # pattern fix: a public helper that sends more than one command, or
        # pairs a send with a delay or a state change, must not let another
        # flow's command land between its steps -- its later steps assume
        # the earlier ones took effect on a device nobody else has touched.
        # Re-entrant, so a caller already inside an exchange (the
        # SD operations call these) still works.
        return self._run_owning_exchange(
            self._prepare_lan_interface_locked, "prepare_lan_interface"
        )

    def _prepare_lan_interface_locked(self) -> None:
        self.send(scpi_commands.disable_storage_sd())
        # The state belongs to the LAST command of the pair reaching the
        # wire -- see start_streaming.
        self.send(scpi_commands.enable_network_lan())
        if self._is_logging_to_sd:
            self._forget_sd_card_sizes()
        self._is_logging_to_sd = False

    def prepare_sd_interface(self) -> None:
        # ⛔ OWNS THE EXCHANGE FOR ITS WHOLE BODY. Structural, not a
        # pattern fix: a public helper that sends more than one command, or
        # pairs a send with a delay or a state change, must not let another
        # flow's command land between its steps -- its later steps assume
        # the earlier ones took effect on a device nobody else has touched.
        # Re-entrant, so a caller already inside an exchange (the
        # SD operations call these) still works.
        return self._run_owning_exchange(
            self._prepare_sd_interface_locked, "prepare_sd_interface"
        )

    def _prepare_sd_interface_locked(self) -> None:
        if self.is_usb_connection:
            # Over WiFi/TCP (firmware v3.7.0+, #598/#599) LAN must stay
            # up: the firmware arbitrates the shared bus and routes the SD
            # reply back over the very connection, which disabling LAN
            # would drop.
            self.send(scpi_commands.disable_network_lan())
            time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)
        self.send(scpi_commands.enable_storage_sd())
        time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)

    # ---- Lower-level WiFi setters (escape hatch for fine-grained control) -

    def set_wifi_ssid(self, ssid: str) -> None:
        self.send(scpi_commands.set_network_wifi_ssid(ssid))

    def set_wifi_password(self, password: str) -> None:
        self.send(scpi_commands.set_network_wifi_password(password))

    def set_wifi_mode_self_hosted(self) -> None:
        self.send(scpi_commands.set_network_wifi_mode_self_hosted())

    def set_wifi_mode_existing(self) -> None:
        self.send(scpi_commands.set_network_wifi_mode_existing())

    def apply_network_configuration(self) -> None:
        # ⛔ OWNS THE EXCHANGE FOR ITS WHOLE BODY. Structural, not a
        # pattern fix: a public helper that sends more than one command, or
        # pairs a send with a delay or a state change, must not let another
        # flow's command land between its steps -- its later steps assume
        # the earlier ones took effect on a device nobody else has touched.
        # Re-entrant, so a caller already inside an exchange (the
        # SD operations call these) still works.
        # Same as update_network_configuration: writes and sleeps only, and
        # legitimately called inside a capture.
        return self._run_owning_exchange(
            self._apply_network_configuration_locked, "apply_network_configuration"
        )

    def _apply_network_configuration_locked(self) -> None:
        self.send(scpi_commands.apply_network_lan())
        time.sleep(self._WIFI_MODULE_RESTART_DELAY_S)

    def save_network_configuration(self) -> None:
        self.send(scpi_commands.save_network_lan())

    # ---- LAN chip info (ILanChipInfoProvider) -----------------------------

    def request_lan_chip_info(self) -> None:
        self.send(scpi_commands.get_lan_chip_info())


# ---- DaqifiDeviceFactory -----------------------------------------------------


class DaqifiDeviceFactory:
    """Convenience factory for creating + initializing devices.

    Ports the C# ``DaqifiDeviceFactory`` static class. Async methods
    collapse to sync per project convention.

    Returns a :class:`DaqifiStreamingDevice` (matches upstream -- the
    factory never returns the bare base class).
    """

    @staticmethod
    def connect_tcp(
        host: str,
        port: int,
        device_name: str = "DaqifiDevice",
        retry_options: Optional[ConnectionRetryOptions] = None,
        initialize_device: bool = True,
        message_factory: Optional[Callable[[bytes], Any]] = None,
        local_interface: Optional[str] = None,
    ) -> DaqifiStreamingDevice:
        """Connect over TCP. ``local_interface`` binds the socket to that
        local IP as its source address; it doesn't select the outgoing NIC
        (see :class:`TcpStreamTransport`)."""
        if not host or not host.strip():
            raise ValueError("Host cannot be null or empty.")
        DaqifiDeviceFactory._validate_port(port)
        transport = TcpStreamTransport(host, port, local_interface)
        return DaqifiDeviceFactory._connect_with_transport(
            transport, device_name, retry_options, initialize_device, message_factory,
        )

    @staticmethod
    def connect_serial(
        port_name: str,
        baud_rate: int = _DEFAULT_BAUD_RATE,
        device_name: str = "DaqifiDevice",
        retry_options: Optional[ConnectionRetryOptions] = None,
        initialize_device: bool = True,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> DaqifiStreamingDevice:
        if port_name is None:
            raise TypeError("port_name must not be None.")
        if not port_name.strip():
            raise ValueError("Port name cannot be empty or whitespace.")
        if baud_rate <= 0:
            raise ValueError("Baud rate must be greater than zero.")
        transport = SerialStreamTransport(port_name, baud_rate)
        return DaqifiDeviceFactory._connect_with_transport(
            transport, device_name, retry_options, initialize_device, message_factory,
        )

    @staticmethod
    def connect_from_device_info(
        device_info: Any,
        device_name: Optional[str] = None,
        retry_options: Optional[ConnectionRetryOptions] = None,
        initialize_device: bool = True,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> DaqifiStreamingDevice:
        """Dispatch to the right connect_* based on ``device_info.connection_type``.

        Ports the C# ``DaqifiDeviceFactory.ConnectFromDeviceInfo[Async]``
        — async/sync collapsed per project convention. Routes WiFi
        infos to :meth:`connect_tcp` and Serial infos to
        :meth:`connect_serial`. HID infos raise ``NotImplementedError``
        (HID is for bootloader mode only; not a regular connection
        target — same NotSupportedException semantics as upstream).

        Args:
            device_info: A ``daqifi.device_finders.DeviceInfo`` (or
                anything with the same shape: ``connection_type``,
                ``ip_address``, ``port``, ``port_name``, ``name``).
            device_name: Override for the device's display name. If
                ``None`` and ``device_info.name`` is non-empty, uses
                ``device_info.name``; otherwise falls back to the
                ``"DaqifiDevice"`` default.
            retry_options: Connection retry behavior — forwarded to
                the underlying connect_* call.
            initialize_device: If True, run ``device.initialize()``
                after connect succeeds.
            message_factory: Optional protobuf message factory to
                inject into the streaming consumer.

        Returns:
            A connected (and optionally initialized) :class:`DaqifiStreamingDevice`.

        Raises:
            TypeError: ``device_info`` is None.
            ValueError: WiFi info missing ``ip_address`` or ``port``;
                Serial info missing ``port_name``.
            NotImplementedError: ``connection_type`` is HID.
        """
        from .device_finders import ConnectionType

        if device_info is None:
            raise TypeError("device_info must not be None.")

        connection_type = getattr(device_info, "connection_type", None)
        # Resolve device name: explicit override beats device_info.name
        # which beats the connect_*-default. Matches upstream's
        # "name from discovery info if not overridden" pattern.
        resolved_name: Optional[str] = device_name
        if resolved_name is None or not str(resolved_name).strip():
            info_name = getattr(device_info, "name", "")
            if info_name and str(info_name).strip():
                resolved_name = str(info_name).strip()
        if resolved_name is None or not str(resolved_name).strip():
            resolved_name = "DaqifiDevice"

        if connection_type == ConnectionType.WIFI:
            ip = getattr(device_info, "ip_address", None)
            port = getattr(device_info, "port", None)
            if ip is None or not str(ip).strip():
                raise ValueError(
                    "device_info must have an ip_address for WiFi connections."
                )
            if port is None:
                raise ValueError(
                    "device_info must have a port for WiFi connections."
                )
            # Reject floats (silent int() truncation would target a
            # different port — e.g. 9760.9 → 9760) and non-numeric
            # strings (which would raise a confusing int() error
            # instead of our explicit message).
            if isinstance(port, bool) or not isinstance(port, int):
                if isinstance(port, str) and port.strip().lstrip("-").isdigit():
                    port_int = int(port.strip())
                else:
                    raise ValueError(
                        f"device_info.port must be an integer for WiFi "
                        f"connections (got {type(port).__name__}={port!r})."
                    )
            else:
                port_int = port
            # Strip the host before forwarding so leading/trailing
            # whitespace can't pass connect_tcp's strip-validation
            # but still reach the transport unstripped.
            # Source address of the NIC that discovered the device
            # (daqifi-core#187); the routing table still picks the outgoing
            # NIC (see TcpStreamTransport).
            local = str(getattr(device_info, "local_interface_address", "") or "").strip()
            return DaqifiDeviceFactory.connect_tcp(
                host=str(ip).strip(),
                port=port_int,
                device_name=resolved_name,
                retry_options=retry_options,
                initialize_device=initialize_device,
                message_factory=message_factory,
                local_interface=local or None,
            )

        if connection_type == ConnectionType.SERIAL:
            port_name = getattr(device_info, "port_name", None)
            # Match connect_serial's exception contract: TypeError on
            # ``None``, ValueError on empty/whitespace. A caller that
            # distinguishes "wrong type" from "wrong value" gets the
            # same surface either through the dispatcher or through
            # connect_serial directly.
            if port_name is None:
                raise TypeError(
                    "device_info.port_name must not be None for Serial connections."
                )
            if not str(port_name).strip():
                raise ValueError(
                    "device_info.port_name must not be empty for Serial connections."
                )
            return DaqifiDeviceFactory.connect_serial(
                port_name=str(port_name).strip(),
                device_name=resolved_name,
                retry_options=retry_options,
                initialize_device=initialize_device,
                message_factory=message_factory,
            )

        if connection_type == ConnectionType.HID:
            raise NotImplementedError(
                "HID device connections are not supported by the factory. "
                "HID is only used for bootloader mode."
            )

        raise NotImplementedError(
            f"Connection type {connection_type!r} is not supported."
        )

    @staticmethod
    def _validate_port(port: int) -> None:
        if port < 1 or port > 65535:
            raise ValueError("Port must be between 1 and 65535.")

    @staticmethod
    def _connect_with_transport(
        transport: Any,
        device_name: str,
        retry_options: Optional[ConnectionRetryOptions],
        initialize_device: bool,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> DaqifiStreamingDevice:
        device: Optional[DaqifiStreamingDevice] = None
        try:
            # Construct the device first so its status callbacks are
            # subscribed BEFORE the connection attempt — otherwise an
            # early status event from the transport's connect would
            # be missed. Then let device.connect() forward
            # retry_options to the transport (Qodo PR 17 pass 20 #2).
            device = DaqifiStreamingDevice(
                device_name, transport=transport, message_factory=message_factory,
            )
            device.connect(retry_options=retry_options)
            if initialize_device:
                device.initialize()
            return device
        except Exception:
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
            else:
                try:
                    transport.close()
                except Exception:
                    pass
            raise
