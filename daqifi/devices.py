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

import functools
import logging
import os
import queue
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import IO, Any, Callable, List, Optional, Sequence

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
from ._internal.scpi import ConnectionStatus, DeviceState, StreamInterface
from ._internal import scpi_commands
from .channels import ChannelsPopulatedEventArgs
from .communication import (
    LineBasedMessageParser,
    ProtobufMessageParser,
    StreamMessageConsumer,
)
from .enums import SdCardLogFormat, WifiMode, WifiSecurityType
from .exceptions import DeviceUnresponsiveError
from .network import NetworkConfiguration
from .protocol import ProtobufProtocolHandler
from .sdcard import _write_all
from .sdcard import (
    END_OF_FILE_MARKER,
    SdCardDownloadResult,
    SdCardFileInfo,
    SdCardTransferProgress,
    parse_sd_card_file_list,
    throw_if_sd_card_list_error,
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

# What may follow the end-of-file marker at the end of a transfer: line
# endings and the console prompt, in any order, at most this many bytes.
_EOF_TRAILER_MAX = 16


def _is_eof_trailer(trailer: bytes) -> bool:
    """True if ``trailer`` can follow the end-of-file marker at the end of
    a transfer: line endings and the console prompt, at most
    ``_EOF_TRAILER_MAX`` bytes, possibly ending part-way through a prompt
    still arriving."""
    if len(trailer) > _EOF_TRAILER_MAX:
        return False
    rest = trailer.replace(b"DAQIFI> ", b"").replace(b"DAQIFI>", b"")
    stripped = rest.rstrip(b"\r\n ")
    if not stripped.strip(b"\r\n "):
        return True
    # A prompt still arriving at the very end ("DAQ").
    for cut in range(1, len(b"DAQIFI>")):
        if rest.endswith(b"DAQIFI>"[:cut]):
            return not rest[: len(rest) - cut].strip(b"\r\n ")
    return False


def _eof_marker_start(buffer: bytes) -> int:
    """Index of an end-of-file marker that ends ``buffer`` apart from a
    trailer of line endings and prompt, or -1."""
    marker = END_OF_FILE_MARKER
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
    """

    @functools.wraps(method)
    def wrapper(self: "DaqifiDevice", *args: Any, **kwargs: Any) -> Any:
        tid = threading.get_ident()
        if self._text_exchange_lock_owner == tid:
            raise RuntimeError(
                f"{method.__name__} cannot run inside another exchange on the "
                "same device (for example from a setup_action callback)."
            )
        self._acquire_text_exchange_lock(method.__name__)
        try:
            self._text_exchange_lock_owner = tid
            self._sd_operation_owner = tid
            try:
                if self._disposed or self._is_disconnecting:
                    raise RuntimeError(
                        f"{method.__name__} cannot run while the device is "
                        "disconnecting or disposed."
                    )
                return method(self, *args, **kwargs)
            finally:
                self._sd_operation_owner = None
                self._text_exchange_lock_owner = None
        finally:
            self._text_exchange_lock.release()

    return wrapper


class DaqifiDevice:
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
        # Thread running an _exclusive_sd_operation (it holds the lock), and
        # whether one of its text exchanges is in progress.
        self._sd_operation_owner: Optional[int] = None
        self._sd_text_exchange_active = False

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
        """
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")
        if isinstance(message, ScpiMessage):
            data = message.get_bytes()
            stream = self._transport.stream if self._transport is not None else None
            if stream is None:
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
            except Exception:
                self._set_status(ConnectionStatus.Lost)
                self._set_state(DeviceState.Error)
                raise
        else:
            raise NotImplementedError(
                "Direct message sending without a string-typed ScpiMessage is "
                "not yet implemented."
            )

    def _acquire_text_exchange_lock(self, operation: str) -> None:
        """Take the text-exchange lock; from the consumer thread, only if free.

        From a message-received callback the lock's holder may be waiting
        for this very thread to stop the consumer: waiting here would stall
        both, the stop would time out and the consumer would be left dead.
        """
        consumer = self._consumer
        if consumer is not None and (
            threading.current_thread() is getattr(consumer, "_thread", None)
        ):
            if not self._text_exchange_lock.acquire(blocking=False):
                raise RuntimeError(
                    f"{operation} cannot run from a message-received callback "
                    "while another exchange is in progress on the same "
                    "device; call it from another thread."
                )
        else:
            self._text_exchange_lock.acquire()

    def execute_text_command(
        self,
        setup_action: Callable[[], None],
        response_timeout_ms: int = 5000,
        completion_timeout_ms: int = 1000,
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

        Returns:
            List of text lines captured between ``setup_action()`` and the
            inactivity timeout. Empty list when the device returned
            nothing within ``response_timeout_ms``.
        """
        tid = threading.get_ident()
        if (
            self._sd_operation_owner == tid
            and self._text_exchange_lock_owner == tid
            and not self._sd_text_exchange_active
        ):
            # Inside an SD operation, which already holds the lock.
            self._sd_text_exchange_active = True
            try:
                return self._execute_text_command_locked(
                    setup_action, response_timeout_ms, completion_timeout_ms
                )
            finally:
                self._sd_text_exchange_active = False
        if self._text_exchange_lock_owner == tid:
            # Re-entrant call from inside a setup_action callback.
            # Surfacing as RuntimeError instead of letting Lock.acquire
            # deadlock keeps the caller's process responsive while
            # still preventing the consumer-swap corruption the lock
            # exists to guard against.
            raise RuntimeError(
                "execute_text_command is not re-entrant on the same device; "
                "do not call it from inside a setup_action callback."
            )

        self._acquire_text_exchange_lock("execute_text_command")
        try:
            # All validation happens INSIDE the lock to close the
            # TOCTOU window: a competing thread can call disconnect()
            # (which clears self._consumer / self._transport without
            # acquiring this lock) while we're blocked on the lock.
            # Re-checking after acquisition guarantees the consumer,
            # transport, and stream we operate on are still live.
            self._text_exchange_lock_owner = tid
            try:
                return self._execute_text_command_locked(
                    setup_action, response_timeout_ms, completion_timeout_ms
                )
            finally:
                self._text_exchange_lock_owner = None
        finally:
            self._text_exchange_lock.release()

    def _execute_text_command_locked(
        self,
        setup_action: Callable[[], None],
        response_timeout_ms: int,
        completion_timeout_ms: int,
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
            raise RuntimeError(
                "execute_text_command requires an active consumer; "
                "call connect() first."
            )
        if self._transport is None:
            raise RuntimeError(
                "execute_text_command requires an active transport."
            )
        stream = self._transport.stream
        if stream is None:
            raise RuntimeError(
                "Transport reports no stream; cannot pause consumer."
            )

        was_running = self._consumer.is_running
        if was_running:
            try:
                stopped = self._consumer.stop_safely()
                if stopped is False:
                    self._consumer.stop()
            except Exception:
                # Best-effort pause; if we can't stop the protobuf
                # consumer we'll race it for stream bytes (degraded
                # but not catastrophic).
                pass

        captured: List[str] = []
        line_queue: "queue.Queue[str]" = queue.Queue()

        try:
            text_parser = LineBasedMessageParser(line_ending="\r\n")
            text_consumer = StreamMessageConsumer[str](stream, text_parser)

            def _on_text_line(args: MessageReceivedEventArgs[str]) -> None:
                # Background-thread callback. Only push to the queue;
                # the captured list is built on the main thread from
                # the queue gets so late arrivals can't slip in past
                # the inactivity timeout.
                line_queue.put(args.message.data)

            text_consumer.add_message_received(_on_text_line)
            text_consumer.start()

            try:
                setup_action()

                # Phase 1: block up to response_timeout_ms for the first
                # line. Returns immediately as soon as a line arrives,
                # so we don't waste cycles polling.
                try:
                    captured.append(
                        line_queue.get(timeout=response_timeout_ms / 1000.0)
                    )
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
                    try:
                        captured.append(
                            line_queue.get(timeout=inactivity_threshold)
                        )
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
            # setup_action raised — unless the connection was lost during
            # the exchange: the stream is dead, and a restarted reader
            # would just poll it.
            if (
                was_running
                and self._status == ConnectionStatus.Connected
                and not getattr(stream, "peer_closed", False)
            ):
                try:
                    self._consumer.start()
                except Exception:
                    pass

        return captured

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
            raise RuntimeError("Device is not connected.")

        from ._internal import scpi as _scpi
        from ._internal import scpi_commands as _scpi_commands

        popped: List[str] = []
        consecutive_empty = 0
        saw_no_error = False

        # A real SCPI error reply is ``<int>,"<msg>"``. Strip a leading
        # ``DAQIFI>`` prompt if present so noisy streams (where the
        # prompt prefixes the reply or arrives on its own line) don't
        # falsely look like garbage. (Qodo /agentic_review on PR #101
        # caught this — the prior code only inspected the first
        # non-empty line, so a ``DAQIFI>`` prefix could push the real
        # reply past the budget.)
        def _find_scpi_reply(lines):
            for raw in lines:
                candidate = raw.strip()
                if not candidate:
                    continue
                # Case-insensitive prompt strip — firmware variants may
                # emit `Daqifi>` / `daqifi>` instead of the canonical
                # `DAQIFI>`. (Qodo /agentic_review on PR #101 caught
                # the case-sensitive original would treat those as
                # garbage and burn the empty_budget.)
                if candidate.upper().startswith("DAQIFI>"):
                    candidate = candidate[len("DAQIFI>"):].strip()
                    if not candidate:
                        continue
                code_str, sep, rest = candidate.partition(",")
                if not sep:
                    continue
                try:
                    code = int(code_str.strip())
                except ValueError:
                    continue
                rest = rest.strip()
                # The reply must have non-empty content after the
                # comma — accept whether or not the description is
                # quoted. SerialInterface.drain_errors and
                # scpi.parse_system_error both accept unquoted
                # descriptions (firmware variants exist that omit
                # quotes), and an earlier strict-quote requirement
                # here was found to silently reject real unquoted
                # errors like ``-200,Execution error`` — see Qodo
                # /agentic_review on PR #101 (the same review that
                # earlier *suggested* the strict-quote check; this
                # is the corrected position).
                if not rest:
                    continue
                # Special-case the empty-queue terminator so
                # slightly-nonconforming firmware that returns
                # ``0,No error`` (description unquoted) is still
                # normalized to the canonical quoted form so the
                # downstream parser sees a consistent shape.
                if code == 0 and rest.replace('"', "").strip().lower() == "no error":
                    return f'{code},"No error"'
                return candidate
            return None

        iterations = 0
        for _ in range(max_iterations):
            iterations += 1
            lines = self.execute_text_command(
                setup_action=lambda: self.send(_scpi_commands.get_last_error()),
                response_timeout_ms=response_timeout_ms,
                completion_timeout_ms=completion_timeout_ms,
            )
            reply = _find_scpi_reply(lines)
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
        """Run the standard SCPI init sequence.

        Ports the upstream ``InitializeAsync`` (sync here per project
        convention). Wires the protocol handler before sending the
        text-mode setup commands, then queries device info -- the
        protobuf consumer's status callback updates :attr:`metadata`.

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
        if not self.is_connected:
            raise RuntimeError(
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

            # Standard SCPI init sequence (mirrors upstream).
            self.send(scpi_commands.disable_device_echo())
            time.sleep(0.1)
            self.send(scpi_commands.stop_streaming())
            time.sleep(0.1)
            self.send(scpi_commands.turn_device_on())
            time.sleep(0.1)
            self.send(scpi_commands.set_protobuf_stream_format())
            time.sleep(0.1)
            # Pin the stream output to the same transport we connected
            # over. Without this, streaming bytes can be routed to the
            # last-configured interface (e.g. WiFi) and the consumer on
            # this transport sees nothing.
            interface = self._stream_interface_for_transport()
            if interface is not None:
                self.send(scpi_commands.set_stream_interface(interface))
                time.sleep(0.1)

            # Query device info -- the protobuf consumer dispatches the
            # response to OnStatusMessageReceived which fills metadata.
            # Polled (was a fixed 1.0 s sleep) so a healthy device
            # returns early — needed roughly 500 ms over USB CDC on
            # the 2026-04-23 NQ1 hardware run. TCP gets a longer
            # ceiling because the WiFi-SCPI wedge (#73) is the case
            # we want to detect distinctly from a routine slow reply.
            is_tcp = isinstance(self._transport, TcpStreamTransport)
            if response_timeout_s is None:
                response_timeout_s = 5.0 if is_tcp else 1.0
            self.send(scpi_commands.get_device_info())
            deadline = time.monotonic() + response_timeout_s
            while time.monotonic() < deadline:
                if self._metadata.serial_number:
                    break
                time.sleep(0.05)
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
                pass

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

        for i in range(count):
            channel = AnalogChannel(
                index=i,
                resolution=resolution,
                name=f"AI{i}",
                calibration_b=self._with_default(cal_b, i, 0.0),
                calibration_m=self._with_default(cal_m, i, 1.0),
                internal_scale_m=self._with_default(int_scale, i, 1.0),
                port_range=self._with_default(ranges, i, 1.0),
                is_enabled=False,
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
        for i in range(count):
            self._channels.append(
                DigitalChannel(index=i, name=f"DIO{i}", is_enabled=False)
            )
        return count

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
            pass
        try:
            self.populate_channels_from_status(message)
        except Exception:
            pass
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

    def _set_state(self, value: DeviceState) -> None:
        self._state = value

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

    SD-card list/download, network configuration apply, and similar
    multi-step operations are intentionally simpler than upstream --
    they send the corresponding SCPI command and rely on the consumer
    to surface the response. Hardware-coupled retry/timeout polish
    lives in :mod:`daqifi.device.NyquistDevice` for now.
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
        return self._is_streaming

    @property
    def streaming_frequency(self) -> int:
        return self._streaming_frequency

    @streaming_frequency.setter
    def streaming_frequency(self, value: int) -> None:
        if value <= 0:
            raise ValueError("streaming_frequency must be positive.")
        self._streaming_frequency = int(value)

    def start_streaming(self) -> None:
        self.send(scpi_commands.start_streaming(self._streaming_frequency))
        self._is_streaming = True
        self._set_state(DeviceState.Streaming)

    def stop_streaming(self) -> None:
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
        """
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")
        if not self.is_usb_connection:
            raise RuntimeError(
                "SD card operations require a USB/serial connection. "
                "The SD card and WiFi/LAN share the SPI bus, so SD "
                "operations cannot be performed over a network connection."
            )
        if self._is_logging_to_sd:
            raise RuntimeError(
                "Cannot list SD card files while logging to the SD card; "
                "stop logging first."
            )

        def _setup_list() -> None:
            self.prepare_sd_interface()
            time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)
            self.send(scpi_commands.get_sd_file_list())

        try:
            lines = self.execute_text_command(
                _setup_list,
                response_timeout_ms=3000,
                completion_timeout_ms=1500,
            )
        finally:
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    _log.exception(
                        "Failed to restore LAN interface after SD list"
                    )

        # Classify firmware error states (no card / filesystem failure /
        # generic SCPI error) BEFORE handing to the parser, so callers
        # get a typed exception instead of an empty list. Mirrors
        # upstream daqifi-core PR #182.
        lines = _strip_prompt_prefixes(lines)
        throw_if_sd_card_list_error(lines)
        self._sd_card_files = parse_sd_card_file_list(lines)

    def get_sd_logging_state(self) -> bool:
        """Ask the device whether it is logging to the SD card.

        Returns ``True`` when the device is streaming (``SYSTem:STReam:DATA?``
        answers 1) to an interface that includes the SD card
        (``SYSTem:STReam:INTerface?`` answers 2, SD, or 3, USB+SD).

        Distinct from the local :attr:`is_logging_to_sd_card` property,
        which reflects what THIS Python session has commanded —
        ``get_sd_logging_state`` asks the DEVICE.

        Neither query touches the SD/LAN bus, so an active recording is
        left alone. The firmware has no SD logging-state query of its own:
        ``SD:LOGging`` was only ever a setter, since renamed ``SD:FILE``
        (#187).

        Raises:
            RuntimeError: not connected, or the device's replies could not
                be parsed.
        """
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")

        def _setup_query() -> None:
            self.send(scpi_commands.get_streaming_state())
            self.send(scpi_commands.get_stream_interface())

        lines = self.execute_text_command(
            _setup_query,
            response_timeout_ms=2000,
            completion_timeout_ms=500,
        )
        values = [
            int(stripped)
            for stripped in (ln.strip() for ln in _strip_prompt_prefixes(lines))
            if stripped.isascii() and stripped.isdigit()
        ]
        if len(values) != 2:
            raise RuntimeError(
                "Could not read the device's streaming state: expected two "
                f"integer replies, got {lines!r}."
            )
        streaming, interface = values
        return streaming == 1 and interface in (
            int(StreamInterface.SdCard),
            int(StreamInterface.All),
        )

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
    ) -> None:
        """Begin logging to the SD card.

        Mirrors the canonical C# ``StartSdCardLoggingAsync`` flow
        (``Daqifi.Core/Device/DaqifiStreamingDevice.cs``). LAN is
        disabled before SD is enabled because the SD card and WiFi/LAN
        share the SPI bus on Nyquist hardware.
        """
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")
        if not self.is_usb_connection:
            raise RuntimeError(
                "SD card logging requires a USB/serial connection. "
                "The SD card and WiFi/LAN share the SPI bus, so SD "
                "operations cannot be performed over a network connection."
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

    @_exclusive_sd_operation
    def stop_sd_card_logging(self) -> None:
        """Stop the active SD-card logging session and restore LAN."""
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")

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
        side.
        """
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")
        # SD ops disable LAN to grab the shared SPI bus — refusing on
        # non-USB transports keeps the call from severing the caller's
        # control connection mid-operation.
        if not self.is_usb_connection:
            raise RuntimeError(
                "SD card operations require a USB/serial connection. "
                "The SD card and WiFi/LAN share the SPI bus, so SD "
                "operations cannot be performed over a network connection."
            )
        if self._is_logging_to_sd:
            raise RuntimeError("Cannot delete files while logging to SD card.")
        if file_name is None or not file_name.strip():
            raise ValueError("file_name must not be empty.")
        _validate_sd_card_file_name(file_name)

        # Defensive: stop any active streaming before reusing the consumer.
        self.send(scpi_commands.stop_streaming())
        self._is_streaming = False

        try:
            lines = self._execute_raw_text_capture(
                lambda: (
                    self.prepare_sd_interface(),
                    time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S),
                    self.send(scpi_commands.delete_sd_file(file_name)),
                    self.send(scpi_commands.get_sd_file_list()),
                ),
                response_timeout_s=3.0,
            )
        finally:
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    pass

        throw_if_sd_card_list_error(lines)
        self._sd_card_files = parse_sd_card_file_list(lines)
        if self._is_logging_to_sd:
            self._forget_sd_card_sizes()

    @_exclusive_sd_operation
    def format_sd_card(self) -> None:
        """Erase the entire SD card.

        Routes through ``prepare_sd_interface`` so the LAN-disable
        precondition documented on ``enable_storage_sd`` is honored
        (LAN must be off before SD takes the SPI bus).
        """
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")
        if not self.is_usb_connection:
            raise RuntimeError(
                "SD card operations require a USB/serial connection. "
                "The SD card and WiFi/LAN share the SPI bus, so SD "
                "operations cannot be performed over a network connection."
            )
        if self._is_logging_to_sd:
            raise RuntimeError("Cannot format SD card while logging.")

        self.send(scpi_commands.stop_streaming())
        self._is_streaming = False

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
        """Fetch + return the SD-card file listing (and refresh the cache)."""
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")
        if not self.is_usb_connection:
            raise RuntimeError(
                "SD card operations require a USB/serial connection. "
                "The SD card and WiFi/LAN share the SPI bus, so SD "
                "operations cannot be performed over a network connection."
            )
        if self._is_logging_to_sd:
            # Listing stops the stream and hands the bus back to LAN, which
            # would end the recording; a call that looks read-only must not
            # (#163). No listing is safe during a recording (#187).
            raise RuntimeError(
                "Cannot list SD card files while logging to the SD card; "
                "stop logging first."
            )

        self.send(scpi_commands.stop_streaming())
        self._is_streaming = False

        try:
            lines = self._execute_raw_text_capture(
                lambda: (
                    self.prepare_sd_interface(),
                    time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S),
                    self.send(scpi_commands.get_sd_file_list()),
                ),
                response_timeout_s=3.0,
            )
        finally:
            if self.is_connected:
                try:
                    self.prepare_lan_interface()
                except Exception:
                    pass

        throw_if_sd_card_list_error(lines)
        files = parse_sd_card_file_list(lines)
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
        2. Stop streaming defensively (issue daqifi-core#118 pattern).
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
            raise RuntimeError("Device is not connected.")
        if not self.is_usb_connection:
            raise RuntimeError(
                "SD card file download is only supported over USB "
                "(serial) connections. The SD card and WiFi/LAN share "
                "the SPI bus, so file downloads require a USB connection."
            )
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

        self.send(scpi_commands.stop_streaming())
        self._is_streaming = False

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
                in_waiting = getattr(raw_stream, "in_waiting", 0) or 0
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
        """
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
                # `stop_safely` returns False when called from inside
                # the consumer thread itself (it can't `.join()` on
                # itself). Calling `.stop()` here would also try to
                # join and deadlock. Refuse instead — raw-capture
                # SD-card ops can't run from a consumer callback.
                raise RuntimeError(
                    "Cannot perform raw-capture SD-card operations "
                    "from inside the protobuf consumer thread or its "
                    "message-received callbacks."
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
        marker = END_OF_FILE_MARKER
        marker_len = len(marker)
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
                raise TimeoutError(
                    f"SD card file download exceeded total cap of "
                    f"{self._SD_DOWNLOAD_TIMEOUT_S:.0f}s "
                    f"({bytes_written} bytes written, no EOF marker)."
                )
            in_waiting = getattr(raw_stream, "in_waiting", 0) or 0
            chunk = (
                raw_stream.read(min(in_waiting, self._SD_DOWNLOAD_BUFFER_BYTES))
                if in_waiting else b""
            )
            if not chunk:
                if (
                    expected_size is None
                    and head_done
                    and _ends_with_eof_marker(trailing)
                    and (now - last_data >= settle_s or now >= idle_deadline)
                ):
                    # The line went quiet with the marker at the end.
                    payload = bytes(trailing[: _eof_marker_start(trailing)])
                    if payload:
                        _write_all(destination_stream, payload, total_deadline)
                        bytes_written += len(payload)
                    if progress is not None:
                        progress(SdCardTransferProgress(bytes_written, file_name))
                    return bytes_written
                if (
                    expected_size is not None
                    and bytes_written == expected_size
                    and len(trailing) >= marker_len
                    and (now - last_data >= settle_s or now >= idle_deadline)
                ):
                    # The listed size, the marker, and then only a line
                    # ending or prompt before the line went quiet.
                    return bytes_written
                if now >= idle_deadline:
                    short = (
                        f" (short read: {bytes_written} of {expected_size} bytes)"
                        if expected_size is not None and bytes_written < expected_size
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
                if len(head_buf) >= 256 or marker in head_buf:
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
                data = bytes(chunk)
                remaining = expected_size - bytes_written
                if remaining > 0:
                    body = data[:remaining]
                    _write_all(destination_stream, body, total_deadline)
                    bytes_written += len(body)
                    data = data[remaining:]
                    if progress is not None:
                        progress(SdCardTransferProgress(bytes_written, file_name))
                if bytes_written == expected_size:
                    trailing.extend(data)
                    # The marker must follow directly, then nothing but a
                    # line ending or prompt: more data means the file is
                    # longer than listed, even if the marker text happens
                    # to sit at the listed boundary. The end is taken when
                    # the line goes quiet (above).
                    if len(trailing) >= marker_len and (
                        bytes(trailing[:marker_len]) != marker
                        or not _is_eof_trailer(bytes(trailing[marker_len:]))
                    ):
                        raise IOError(
                            f"SD card file {file_name!r}: {expected_size} bytes "
                            "received but no end-of-file marker alone after them; "
                            "the file is longer than listed or the transfer is "
                            "corrupt."
                        )
                # Time spent waiting on the destination isn't the line
                # being idle or quiet (#163 audit): restart both clocks.
                last_data = time.monotonic()
                idle_deadline = last_data + self._SD_DOWNLOAD_IDLE_TIMEOUT_S
                continue
            # No size: hold back enough to recognise a marker plus its
            # trailer at the end; everything before that is file content,
            # even if it contains the marker.
            trailing.extend(chunk)
            keep = marker_len + _EOF_TRAILER_MAX
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

        ``DaqifiStreamingDevice`` cannot currently surface text-shaped
        SCPI query replies (the streaming-device's protobuf consumer
        eats them — see daqifi-python-core#16), so we cannot
        round-trip ``SYST:COMM:LAN:SSID? / NETTYPE? / SECURITY?`` to
        rebuild the configuration from the live device state. Once
        #16 is resolved, this property should be reworked to query
        and reconcile.

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
        """Apply ``configuration`` to the device end-to-end.

        Mirrors the canonical C# ``UpdateNetworkConfigurationAsync``
        sequence (``Daqifi.Core/Device/DaqifiStreamingDevice.cs``):

        1. Stop streaming if active (avoids racing the WiFi module restart)
        2. Set WiFi mode (SCPI ``...:NETType``)
        3. Set SSID
        4. Set security type + password (security FIRST so the
           password write has a security context)
        5. Apply (firmware schedules the WiFi module restart)
        6. Wait ``_WIFI_MODULE_RESTART_DELAY_S`` for the module to come back
        7. ``prepare_lan_interface`` — DisableStorageSd + EnableNetworkLan.
           SD card and LAN module SHARE THE SPI BUS on Nyquist hardware;
           this hands the bus to LAN so the WiFi chip can scan + associate.
           Without this step, the WiFi module reports ``Disconnected``
           with ``SignalStrength=0`` even when the config is correct
           (verified on COM7 fw 3.4.6b1, 2026-04-26).
        8. Save (persist across reboots)
        9. Update the locally-tracked ``_network_configuration`` snapshot.

        Raises:
            TypeError: if ``configuration`` is None.
            ValueError: if ``configuration.mode`` or
                ``configuration.security_type`` is unsupported.
            RuntimeError: if the device is not connected.
        """
        if configuration is None:
            raise TypeError("configuration must not be None.")
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")
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

        self.send(scpi_commands.apply_network_lan())
        time.sleep(self._WIFI_MODULE_RESTART_DELAY_S)

        self.prepare_lan_interface()

        self.send(scpi_commands.save_network_lan())

        self._network_configuration = configuration.clone()

    def prepare_lan_interface(self) -> None:
        """Hand the shared SPI bus to the LAN module.

        SD card and LAN share the same SPI bus on Nyquist hardware
        and can't both drive it simultaneously. Call this before any
        network operation that requires the WiFi module to be active
        (scan, associate, transmit). ``update_network_configuration``
        calls it automatically after ``APPLY``.

        Side effect — the SD card SCPI module is disabled, which also
        stops any active SD logging on the device. We update
        ``_is_logging_to_sd`` to keep ``is_logging_to_sd_card`` in
        sync with the firmware's actual state.
        """
        self.send(scpi_commands.disable_storage_sd())
        self.send(scpi_commands.enable_network_lan())
        if self._is_logging_to_sd:
            self._forget_sd_card_sizes()
        self._is_logging_to_sd = False

    def prepare_sd_interface(self) -> None:
        """Hand the shared SPI bus to the SD card.

        Inverse of :meth:`prepare_lan_interface`. Call before any SD
        card operation. Note: while the SD card has the bus, WiFi will
        report ``Disconnected`` until ``prepare_lan_interface`` is
        called again.

        Sleeps ``_SD_INTERFACE_SETTLE_DELAY_S`` between the LAN-disable
        and SD-enable commands so the firmware's SPI-bus arbiter has
        time to release+acquire the bus. Without the gap, the very
        next SD-card SCPI command can hit firmware error -200
        (Execution error). Centralizing the sleep here means callers
        like :meth:`format_sd_card` get the right timing for free.

        Side effect — re-enables the SD card SCPI module. This does
        NOT automatically resume an SD logging session; callers must
        explicitly call :meth:`enable_sd_card_logging` to begin
        logging again. The ``_is_logging_to_sd`` flag is updated on
        that call, not here, since enabling the SD interface alone
        doesn't restart logging on the firmware side.
        """
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
        """Apply pending LAN config; firmware then restarts the WiFi module.

        Lower-level than :meth:`update_network_configuration` — does not
        send the SET commands or call :meth:`prepare_lan_interface`.
        Use :meth:`update_network_configuration` for the full canonical
        sequence."""
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
    ) -> DaqifiStreamingDevice:
        if not host or not host.strip():
            raise ValueError("Host cannot be null or empty.")
        DaqifiDeviceFactory._validate_port(port)
        transport = TcpStreamTransport(host, port)
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
            return DaqifiDeviceFactory.connect_tcp(
                host=str(ip).strip(),
                port=port_int,
                device_name=resolved_name,
                retry_options=retry_options,
                initialize_device=initialize_device,
                message_factory=message_factory,
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
