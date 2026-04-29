"""
Internal serial communication layer for DAQiFi Nyquist devices.

This module provides low-level SCPI command interface over serial port.
Not intended for direct use - use the public API instead.
"""

import serial
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import IO, Any, Callable, Generic, List, Optional, Protocol, Sequence, Tuple, TypeVar, runtime_checkable
from enum import IntEnum
import builtins

from .scpi import ConnectionStatus


T = TypeVar("T")


@dataclass
class TransportStatusEventArgs:
    """Payload for :class:`StreamTransport` status-change callbacks.

    Ports the C# ``TransportStatusEventArgs`` (subclass of ``EventArgs``).
    Python uses plain callables instead of the .NET event pattern, so the
    sender argument is dropped -- subscribers already know who they
    subscribed to.
    """

    is_connected: bool
    connection_info: str
    error: Optional[BaseException] = None


@dataclass
class ConnectionRetryOptions:
    """Retry/backoff configuration for :meth:`StreamTransport.connect`.

    Ports the C# ``ConnectionRetryOptions``. Durations are floats in
    seconds (Pythonic) instead of ``System.TimeSpan``.
    """

    max_attempts: int = 3
    initial_delay: float = 1.0          # seconds
    max_delay: float = 30.0             # seconds
    backoff_multiplier: float = 2.0
    connection_timeout: float = 5.0     # seconds
    enabled: bool = True

    @classmethod
    def no_retry(cls) -> "ConnectionRetryOptions":
        """Equivalent to C# ``ConnectionRetryOptions.NoRetry``."""
        return cls(enabled=False, max_attempts=1)

    @classmethod
    def fast(cls) -> "ConnectionRetryOptions":
        """Equivalent to C# ``ConnectionRetryOptions.Fast``."""
        return cls(
            max_attempts=3,
            initial_delay=0.5,
            max_delay=5.0,
            backoff_multiplier=1.5,
            connection_timeout=3.0,
        )

    @classmethod
    def resilient(cls) -> "ConnectionRetryOptions":
        """Equivalent to C# ``ConnectionRetryOptions.Resilient``."""
        return cls(
            max_attempts=5,
            initial_delay=2.0,
            max_delay=60.0,
            backoff_multiplier=2.5,
            connection_timeout=10.0,
        )

    def calculate_delay(self, attempt_number: int) -> float:
        """Exponential-backoff delay (seconds) for a 1-based attempt number."""
        if attempt_number <= 1:
            return 0.0
        delay = self.initial_delay * (self.backoff_multiplier ** (attempt_number - 2))
        return min(delay, self.max_delay)


@runtime_checkable
class StreamTransport(Protocol):
    """Structural protocol for a stream-based transport.

    Ports the C# ``IStreamTransport`` interface. A transport abstracts
    the underlying connection (TCP, UDP, serial, ...) and exposes a
    file-like ``stream`` for message producers and consumers.

    **Divergence from upstream (sync only):** the C# interface declares
    both sync (``Connect``/``Disconnect``) and async (``ConnectAsync``/
    ``DisconnectAsync``) entry points. The Python project is wholly
    synchronous (``pyserial`` blocks, no ``asyncio`` elsewhere in the
    package), so per the project's port convention -- *Task<T> -> sync
    return when the project doesn't use asyncio* -- only the sync pair
    is exposed. If/when an asyncio variant is added, mirror these as
    ``connect_async``/``disconnect_async`` returning
    ``Awaitable[None]``.

    **Lifecycle:** ``IDisposable`` maps to :meth:`close` (matching
    :class:`SerialInterface`/:class:`MessageProducer` in this module);
    implementations may additionally provide ``__enter__``/``__exit__``
    for ``with`` blocks.

    **Status notifications:** the upstream ``event
    EventHandler<TransportStatusEventArgs>`` becomes a list of plain
    callbacks via :meth:`add_status_changed` / :meth:`remove_status_changed`.

    Properties
    ----------
    stream:
        File-like object used for read/write. ``None`` until connected.
    is_connected:
        ``True`` while the transport is connected.
    connection_info:
        Human-readable description of the underlying connection.
    """

    @property
    def stream(self) -> Optional[IO[bytes]]:
        ...

    @property
    def is_connected(self) -> bool:
        ...

    @property
    def connection_info(self) -> str:
        ...

    def add_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        ...

    def remove_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        ...

    def connect(self, retry_options: Optional[ConnectionRetryOptions] = None) -> None:
        """Open the transport. ``retry_options=None`` means a single attempt."""
        ...

    def disconnect(self) -> None:
        """Close the transport."""
        ...

    def close(self) -> None:
        """Release any owned resources (Pythonic ``IDisposable.Dispose``)."""
        ...


@runtime_checkable
class IDataSample(Protocol):
    """Structural protocol for a single data sample from a channel.

    Ports the C# ``IDataSample`` interface from
    ``Daqifi.Core/Channel/IDataSample.cs``. A sample carries a timestamp
    plus a numeric value.

    **Read/write semantics:** Python ``Protocol`` cannot easily express
    "getter-only" vs "getter+setter" the way C# can. By convention here:

    - ``timestamp`` ports the upstream ``DateTime Timestamp { get; }``
      (read-only) and is exposed as a ``@property`` getter only.
    - ``value`` ports the upstream ``double Value { get; set; }``
      (read/write); implementations should expose it as a settable
      attribute (either a plain instance attribute or a ``@property``
      with a paired setter).

    ``DateTime`` maps to :class:`datetime.datetime` per the project
    BCL mapping.

    The concrete implementation in this module is :class:`DataSample`.
    """

    @property
    def timestamp(self) -> datetime:
        ...

    # Plain attribute (not a property) so type checkers recognize the
    # writable shape promised by the docstring above. A property-only
    # declaration is implicitly read-only to a Protocol consumer
    # (Qodo PR 17 pass 30 #1).
    value: float


@dataclass
class DataSample:
    """Concrete data sample carrying a timestamp and numeric value.

    Ports the C# ``DataSample`` class from
    ``Daqifi.Core/Channel/DataSample.cs``, which implements ``IDataSample``.
    Structurally satisfies the :class:`IDataSample` Protocol.

    Upstream exposes two constructors -- a parameterless one defaulting
    ``Timestamp`` to ``DateTime.UtcNow`` and ``Value`` to ``0.0``, and an
    explicit one. Python collapses both into a single ``@dataclass`` with
    ``timestamp`` defaulting to ``datetime.now(timezone.utc)`` (the project
    BCL mapping for ``DateTime.UtcNow``) and ``value`` defaulting to
    ``0.0``.

    Per the protocol contract, ``timestamp`` is conceptually read-only
    (the upstream property is ``init``-only) while ``value`` is mutable
    (upstream is ``get; set;``). The dataclass is intentionally not
    ``frozen`` so callers can rebind ``value``; treat ``timestamp`` as
    immutable by convention.
    """

    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    value: float = 0.0

    def __str__(self) -> str:
        # Match upstream ToString format: "yyyy-MM-dd HH:mm:ss.fff - value"
        # Python ``%f`` is microseconds (6 digits); slice to milliseconds
        # for parity with C# ``fff``.
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return f"{ts} - {self.value}"


@dataclass(frozen=True)
class SampleReceivedEventArgs:
    """Payload for channel ``SampleReceived`` callbacks.

    Ports the C# ``SampleReceivedEventArgs`` (subclass of ``EventArgs``)
    from ``Daqifi.Core/Channel/SampleReceivedEventArgs.cs``. Per the
    project port convention -- *EventArgs subclass -> @dataclass (frozen
    if the event payload is read-only)* -- and because the upstream
    ``Sample`` is getter-only (set once in the constructor, never
    reassigned), this dataclass is ``frozen=True``.

    The C# ctor enforces non-null via ``ArgumentNullException``; per
    project convention that maps to ``ValueError`` (a value-domain
    contract violation, not a type mismatch).

    The field is typed against the :class:`IDataSample` Protocol rather
    than the concrete :class:`DataSample` class, so any structurally
    conforming sample (e.g. a future channel-specific subclass) is
    accepted.
    """

    sample: "IDataSample"

    def __post_init__(self) -> None:
        if self.sample is None:
            raise ValueError("Sample cannot be None.")


@runtime_checkable
class OutboundMessage(Protocol[T]):
    """Structural protocol for a message destined for a DAQiFi device.

    Mirrors the C# ``IOutboundMessage<T>`` contract: a payload accessor plus
    a serialization hook used by producers to obtain wire bytes.
    """

    data: T

    def get_bytes(self) -> bytes:
        """Return the wire-format byte payload for transmission."""
        ...


@dataclass
class ScpiMessage:
    """Outbound SCPI (Standard Commands for Programmable Instruments) message.

    Concrete implementation of the :class:`OutboundMessage` Protocol carrying a
    SCPI command string. Ports the C# ``ScpiMessage`` class. ``get_bytes()``
    returns the ASCII-encoded command terminated by ``\\r\\n``, matching the
    upstream wire format.
    """

    data: str

    def get_bytes(self) -> bytes:
        """Return the ASCII bytes for transmission, with CRLF terminator.

        Strips a pre-existing trailing CRLF so callers passing
        ``"*IDN?\\r\\n"`` don't double-terminate, and surfaces a clear
        :class:`ValueError` for non-ASCII payloads instead of leaking
        :class:`UnicodeEncodeError` from deep in the encode call.
        """
        command = self.data.rstrip("\r\n") + "\r\n"
        try:
            return command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("SCPI command must be ASCII.") from exc


@runtime_checkable
class MessageParser(Protocol[T]):
    """Structural protocol for message parsers.

    Ports the C# ``IMessageParser<T>`` interface from
    ``Daqifi.Core/Communication/Consumers/IMessageParser.cs``. A parser
    consumes raw bytes and returns parsed messages plus the number of
    bytes consumed. The ``out int consumedBytes`` upstream parameter
    becomes the second tuple element since Python has no ``out`` params
    (mirrors :class:`LineBasedMessageParser`).
    """

    def parse_messages(
        self, data: bytes
    ) -> Tuple[List["IInboundMessage[T]"], int]:
        ...


@runtime_checkable
class IInboundMessage(Protocol[T]):
    """Structural protocol for a message containing data received from the device.

    Mirrors the C# ``IInboundMessage<out T>`` contract: a single read-only
    payload accessor. The C# interface uses ``out`` for covariance; Python's
    structural typing handles variance at type-check time, so no special
    annotation is required at runtime.

    Mirrors the shape of :class:`OutboundMessage` (the inbound counterpart);
    concrete implementations expose ``data`` as a plain attribute or property.
    """

    data: T


@dataclass
class GenericInboundMessage(Generic[T]):
    """Generic concrete :class:`IInboundMessage` carrying any payload type.

    Ports the C# ``GenericInboundMessage<T>`` class. A thin generic wrapper
    around a single ``data`` field that structurally satisfies the
    :class:`IInboundMessage` Protocol. Useful when consumers need a
    type-tagged inbound envelope but no extra parsing/serialization logic.
    """

    data: T


@dataclass
class TextInboundMessage:
    """Inbound text message (concrete :class:`IInboundMessage` ``[str]``).

    Ports the C# ``TextInboundMessage`` class. Defined upstream as a
    sibling of ``LineBasedMessageParser`` in the same source file; ported
    here in isolation since the parser itself is not yet ported. The class
    structurally satisfies the :class:`IInboundMessage` Protocol when ``T``
    is bound to ``str``; equivalent to ``GenericInboundMessage[str]`` but
    kept as a distinct named type to mirror upstream semantics (the parser
    constructs ``new TextInboundMessage(...)`` explicitly).
    """

    data: str


@runtime_checkable
class MessageProducer(Protocol[T]):
    """Structural protocol for background message producers.

    Ports the C# ``IMessageProducer<T>`` interface. A producer owns the
    lifecycle of a queued, asynchronous send pipeline to a device. The
    Python project currently ships only the synchronous
    :class:`SerialInterface`; this protocol defines the contract that future
    threaded/async producers should satisfy. Structural typing means
    implementations need not inherit from this class -- duck typing is
    sufficient.

    Methods
    -------
    start():
        Begin background message processing.
    stop():
        Stop immediately, discarding any queued messages.
    stop_safely(timeout_ms=1000):
        Stop after draining the queue (or until timeout). Returns True
        when the queue drained cleanly, False on timeout.
    send(message):
        Enqueue an :class:`OutboundMessage` for transmission.
    close():
        Release any owned resources (the Pythonic equivalent of the
        upstream's ``IDisposable``).

    Properties
    ----------
    queued_message_count:
        Number of messages currently awaiting send.
    is_running:
        True while the producer's background processing is active.
    """

    @property
    def queued_message_count(self) -> int:
        ...

    @property
    def is_running(self) -> bool:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def stop_safely(self, timeout_ms: int = 1000) -> bool:
        ...

    def send(self, message: OutboundMessage[T]) -> None:
        ...

    def close(self) -> None:
        ...


@dataclass
class MessageReceivedEventArgs(Generic[T]):
    """Payload for :class:`MessageConsumer` ``MessageReceived`` callbacks.

    Ports the C# ``MessageReceivedEventArgs<T>`` (subclass of ``EventArgs``).
    Python uses plain callables instead of the .NET event pattern, so the
    sender argument is dropped -- subscribers already know who they
    subscribed to.

    The ``message`` field carries the parsed inbound message payload (the
    upstream type is ``IInboundMessage<T>``; an inbound-message Protocol is
    not yet ported, so the field is typed as the generic ``T`` payload --
    swap to the Protocol once it lands).

    The ``timestamp`` defaults to ``datetime.now(timezone.utc)`` at
    construction time so the value is timezone-aware (matches the upstream
    ``DateTime.UtcNow`` semantics; `utcnow()` returns a naive datetime and
    is deprecated in Python 3.12+).
    """

    message: T
    raw_data: bytes
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MessageConsumerErrorEventArgs:
    """Payload for :class:`MessageConsumer` ``ErrorOccurred`` callbacks.

    Ports the C# ``MessageConsumerErrorEventArgs`` (subclass of ``EventArgs``).
    The C# ctor enforces a non-null ``error``; Python uses the type hint
    plus normal call-site discipline (raise ``TypeError`` only if a caller
    passes ``None``).
    """

    error: BaseException
    raw_data: Optional[bytes] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@runtime_checkable
class MessageConsumer(Protocol[T]):
    """Structural protocol for background message consumers.

    Ports the C# ``IMessageConsumer<T>`` interface. A consumer owns the
    lifecycle of a queued, asynchronous *receive* pipeline: it reads bytes
    from a transport, parses them into ``T``-typed messages, and notifies
    subscribers. The Python project currently ships only the synchronous
    :class:`SerialInterface`; this protocol defines the contract that future
    threaded/async consumers should satisfy.

    **Lifecycle:** ``IDisposable`` maps to :meth:`close` (matching
    :class:`StreamTransport`/:class:`MessageProducer` in this module);
    implementations may additionally provide ``__enter__``/``__exit__``
    for ``with`` blocks.

    **Event notifications:** the upstream ``event EventHandler<...>``
    members become add/remove callback pairs, mirroring
    :class:`StreamTransport` (``add_status_changed`` / ``remove_status_changed``):

    - ``MessageReceived`` -> :meth:`add_message_received` /
      :meth:`remove_message_received`
    - ``ErrorOccurred`` -> :meth:`add_error_occurred` /
      :meth:`remove_error_occurred`

    Methods
    -------
    start():
        Begin background message reading.
    stop():
        Stop immediately, discarding any in-flight processing.
    stop_safely(timeout_ms=1000):
        Stop after letting current processing complete (or until timeout).
        Returns True when stopped cleanly, False on timeout.
    clear_buffer():
        Drop any buffered bytes from the underlying stream and internal
        buffers (useful after a WiFi reconnect that left stale data).
    close():
        Release any owned resources (Pythonic ``IDisposable.Dispose``).

    Properties
    ----------
    is_running:
        True while the consumer's background processing is active.
    queued_message_count:
        Number of messages currently awaiting processing.
    """

    @property
    def is_running(self) -> bool:
        ...

    @property
    def queued_message_count(self) -> int:
        ...

    def add_message_received(
        self, handler: Callable[[MessageReceivedEventArgs[T]], None]
    ) -> None:
        ...

    def remove_message_received(
        self, handler: Callable[[MessageReceivedEventArgs[T]], None]
    ) -> None:
        ...

    def add_error_occurred(
        self, handler: Callable[[MessageConsumerErrorEventArgs], None]
    ) -> None:
        ...

    def remove_error_occurred(
        self, handler: Callable[[MessageConsumerErrorEventArgs], None]
    ) -> None:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def stop_safely(self, timeout_ms: int = 1000) -> bool:
        ...

    def clear_buffer(self) -> None:
        ...

    def close(self) -> None:
        ...


@dataclass
class DeviceStatusEventArgs:
    """Payload for :class:`Device` ``StatusChanged`` callbacks.

    Ports the C# ``DeviceStatusEventArgs`` (subclass of ``EventArgs``).
    Python uses plain callables instead of the .NET event pattern, so
    the sender argument is dropped -- subscribers already know who they
    subscribed to (mirrors :class:`TransportStatusEventArgs`).
    """

    status: ConnectionStatus


@runtime_checkable
class Device(Protocol):
    """Structural protocol for all DAQiFi devices.

    Ports the C# ``IDevice`` interface from
    ``Daqifi.Core/Device/IDevice.cs``. The base contract every device --
    serial, USB-HID, TCP, UDP -- satisfies. Implementations expose
    identity (name, ip), connection lifecycle, status notifications, and
    outbound messaging.

    **Lifecycle:** the upstream ``Connect``/``Disconnect`` are sync;
    Python keeps the same pair (the project is wholly synchronous --
    see :class:`StreamTransport` for the rationale).

    **Status notifications:** the upstream ``event
    EventHandler<DeviceStatusEventArgs>`` becomes a list of plain
    callbacks via :meth:`add_status_changed` / :meth:`remove_status_changed`,
    matching the established pattern across this module.

    **Message receipt:** the upstream ``event
    EventHandler<MessageReceivedEventArgs>`` becomes
    :meth:`add_message_received` / :meth:`remove_message_received`.
    """

    @property
    def name(self) -> str:
        ...

    @property
    def ip_address(self) -> Optional[str]:
        """IPv4 address string, or ``None`` for non-network transports."""
        ...

    @property
    def is_connected(self) -> bool:
        ...

    @property
    def status(self) -> ConnectionStatus:
        ...

    def add_status_changed(
        self, handler: Callable[[DeviceStatusEventArgs], None]
    ) -> None:
        ...

    def remove_status_changed(
        self, handler: Callable[[DeviceStatusEventArgs], None]
    ) -> None:
        ...

    def add_message_received(
        self, handler: Callable[[MessageReceivedEventArgs[Any]], None]
    ) -> None:
        ...

    def remove_message_received(
        self, handler: Callable[[MessageReceivedEventArgs[Any]], None]
    ) -> None:
        ...

    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...

    def send(self, message: "OutboundMessage[Any]") -> None:
        ...


@runtime_checkable
class StreamingDevice(Device, Protocol):
    """Structural protocol for a device that supports data streaming.

    Ports the C# ``IStreamingDevice`` interface from
    ``Daqifi.Core/Device/IStreamingDevice.cs`` (extends ``IDevice``).
    Adds a streaming-frequency control plus start/stop hooks for the
    sample stream.
    """

    @property
    def streaming_frequency(self) -> int:
        """Streaming frequency in Hz."""
        ...

    @streaming_frequency.setter
    def streaming_frequency(self, value: int) -> None:
        ...

    @property
    def is_streaming(self) -> bool:
        ...

    def start_streaming(self) -> None:
        ...

    def stop_streaming(self) -> None:
        ...


@runtime_checkable
class HidDeviceEnumerator(Protocol):
    """Structural protocol for HID device discovery.

    Ports the C# ``IHidDeviceEnumerator`` interface from
    ``Daqifi.Core/Communication/Transport/IHidDeviceEnumerator.cs``.
    Used by bootloader/firmware-update flows to find candidate USB-HID
    devices.

    **Sync only:** the upstream signature is ``Task<IReadOnlyList<...>>``;
    per the project convention (no asyncio) this is exposed as a sync
    method returning a ``Sequence[HidDeviceInfo]``.
    """

    def enumerate(
        self,
        vendor_id: Optional[int] = None,
        product_id: Optional[int] = None,
    ) -> "Sequence[HidDeviceInfo]":
        ...


@runtime_checkable
class HidTransport(Protocol):
    """Structural protocol for USB HID bootloader communication.

    Ports the C# ``IHidTransport`` interface from
    ``Daqifi.Core/Communication/Transport/IHidTransport.cs``. Provides
    connect/disconnect, raw read/write of HID report payloads, and
    timeouts.

    **Sync only:** upstream declares paired sync + async APIs; per the
    project convention only the sync pair is exposed (mirrors
    :class:`StreamTransport`).

    **Lifecycle:** ``IDisposable`` -> :meth:`close`.

    **Timeouts:** ``TimeSpan`` -> :class:`datetime.timedelta` (the
    Pythonic equivalent that preserves microsecond precision).
    """

    @property
    def is_connected(self) -> bool:
        ...

    @property
    def vendor_id(self) -> Optional[int]:
        """USB VID of the connected device, or ``None`` when disconnected."""
        ...

    @property
    def product_id(self) -> Optional[int]:
        """USB PID of the connected device, or ``None`` when disconnected."""
        ...

    @property
    def serial_number(self) -> Optional[str]:
        ...

    @property
    def device_path(self) -> Optional[str]:
        ...

    @property
    def read_timeout(self) -> timedelta:
        ...

    @read_timeout.setter
    def read_timeout(self, value: timedelta) -> None:
        ...

    @property
    def write_timeout(self) -> timedelta:
        ...

    @write_timeout.setter
    def write_timeout(self, value: timedelta) -> None:
        ...

    def connect(
        self,
        vendor_id: int,
        product_id: int,
        serial_number: Optional[str] = None,
    ) -> None:
        ...

    def write(self, data: bytes) -> None:
        ...

    def read(self, timeout: Optional[timedelta] = None) -> bytes:
        ...

    def disconnect(self) -> None:
        ...

    def close(self) -> None:
        ...


# UDP endpoint as a plain ``(host, port)`` tuple -- the Pythonic
# equivalent of ``System.Net.IPEndPoint`` (matches socket-module
# conventions; e.g. ``socket.sendto(data, (host, port))``).
UdpEndPoint = Tuple[str, int]


@runtime_checkable
class UdpTransport(Protocol):
    """Structural protocol for UDP broadcast/unicast transport.

    Ports the C# ``IUdpTransport`` interface from
    ``Daqifi.Core/Communication/Transport/IUdpTransport.cs``. Used
    primarily for device discovery and UDP-based messaging.

    **Sync only:** upstream declares paired sync + async APIs; per the
    project convention only the sync pair is exposed.

    **Endpoints:** upstream uses ``System.Net.IPEndPoint``; the Python
    port follows the standard library's ``(host, port)`` tuple
    convention (see :data:`UdpEndPoint`).

    **Status notifications:** ``event
    EventHandler<TransportStatusEventArgs>`` -> add/remove pair.

    **Lifecycle:** ``IDisposable`` -> :meth:`close`.
    """

    @property
    def is_open(self) -> bool:
        ...

    @property
    def connection_info(self) -> str:
        ...

    def add_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        ...

    def remove_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        ...

    def open(self) -> None:
        ...

    def close(self) -> None:
        ...

    def send_broadcast(self, data: bytes, port: int) -> None:
        """Broadcast ``data`` to ``port`` on the default broadcast address."""
        ...

    def send_broadcast_to(self, data: bytes, end_point: UdpEndPoint) -> None:
        """Broadcast ``data`` to a specific endpoint.

        Mirrors the C# overload ``SendBroadcastAsync(byte[], IPEndPoint)``.
        Python doesn't dispatch on argument types, so the targeted
        variant gets a distinct name.
        """
        ...

    def send_unicast(self, data: bytes, end_point: UdpEndPoint) -> None:
        ...

    def receive(
        self, timeout: Optional[timedelta] = None
    ) -> Optional[Tuple[bytes, UdpEndPoint]]:
        """Receive one datagram. Returns ``None`` on timeout."""
        ...


@dataclass(frozen=True)
class HidDeviceInfo:
    """Metadata for a discovered HID device.

    Ports the C# ``HidDeviceInfo`` sealed class from
    ``Daqifi.Core/Communication/Transport/HidDeviceInfo.cs``. All fields
    are init-only upstream (private setters), so the Python port is a
    ``frozen=True`` dataclass -- attempts to mutate after construction
    raise ``FrozenInstanceError`` (matching upstream semantics).

    Validation: ``device_path`` must be non-empty and non-whitespace
    (mirroring the C# ``ArgumentException`` thrown when
    ``string.IsNullOrWhiteSpace(devicePath)`` is true). Per project
    convention, ``ArgumentException`` -> ``ValueError``.
    """

    vendor_id: int
    product_id: int
    device_path: str
    serial_number: Optional[str] = None
    product_name: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.device_path or not self.device_path.strip():
            raise ValueError("Device path cannot be null or empty.")

    def __str__(self) -> str:
        serial_part = (
            self.serial_number
            if self.serial_number and self.serial_number.strip()
            else "unknown"
        )
        return (
            f"VID=0x{self.vendor_id:04X}, PID=0x{self.product_id:04X}, "
            f"Serial={serial_part}, Path={self.device_path}"
        )


@dataclass
class DeviceConnectionOptions:
    """Configuration options for device connection behavior.

    Ports the C# ``DeviceConnectionOptions`` class from
    ``Daqifi.Core/Device/DeviceConnectionOptions.cs``. Settings class with
    mutable fields plus a trio of named presets exposed as ``@classmethod``
    factories.

    The C# presets are read-only static *properties* of the form
    ``=> new() {...}`` -- each access constructs a fresh instance. Per the
    project port convention for that pattern, they map to ``@classmethod``
    factories (NOT module-level singletons, which would share mutable
    state across callers).

    ``connection_retry`` is ``None`` by default, meaning the transport's
    own default behavior applies (matching the upstream nullable
    ``ConnectionRetryOptions?``).
    """

    device_name: str = "DAQiFi Device"
    connection_retry: Optional[ConnectionRetryOptions] = None
    initialize_device: bool = True

    @classmethod
    def default(cls) -> "DeviceConnectionOptions":
        """Default configuration: no transport retry override, init enabled."""
        return cls()

    @classmethod
    def fast(cls) -> "DeviceConnectionOptions":
        """Optimized for fast connections (uses ``ConnectionRetryOptions.fast``)."""
        return cls(connection_retry=ConnectionRetryOptions.fast())

    @classmethod
    def resilient(cls) -> "DeviceConnectionOptions":
        """Optimized for slow/unreliable connections (uses ``ConnectionRetryOptions.resilient``)."""
        return cls(connection_retry=ConnectionRetryOptions.resilient())


class DeviceType(IntEnum):
    """DAQiFi device family identifier.

    Stub-port of the C# ``DeviceType`` enum from
    ``Daqifi.Core/Device/DeviceType.cs`` (not yet ported as its own module
    -- inlined here per the project's transitive-un-ported-type convention
    until a dedicated home is created). Matches the upstream integer values
    so wire-format/stored representations stay compatible.

    TODO(port): move to a dedicated ``daqifi/_internal/device.py`` once
    the Device shard grows beyond comm-coupled types.
    """

    UNKNOWN = 0
    NYQUIST_1 = 1
    NYQUIST_2 = 2
    NYQUIST_3 = 3


class ConnectionType(IntEnum):
    """Transport over which a discovered device speaks.

    Ports the C# ``ConnectionType`` enum from
    ``Daqifi.Core/Device/Discovery/IDeviceInfo.cs``. ``IntEnum`` matches
    the local convention established by :class:`DeviceType`.
    """

    UNKNOWN = 0
    WIFI = 1     # WiFi/TCP connection
    SERIAL = 2   # Serial/USB connection
    HID = 3      # USB HID (bootloader mode)


@dataclass
class DeviceCapabilities:
    """Capabilities and features available on a DAQiFi device.

    Ports the C# ``DeviceCapabilities`` class from
    ``Daqifi.Core/Device/DeviceCapabilities.cs``. Mutable settings record
    with per-device-type presets exposed via the :meth:`from_device_type`
    classmethod factory (the Pythonic equivalent of the upstream
    ``static FromDeviceType`` switch-expression).

    Defaults match the C# parameterless constructor: streaming on,
    everything else off/zero, sampling rate 1000 Hz.
    """

    supports_streaming: bool = True
    has_sd_card: bool = False
    has_wifi: bool = False
    has_usb: bool = False
    analog_input_channels: int = 0
    analog_output_channels: int = 0
    digital_channels: int = 0
    max_sampling_rate: int = 1000

    @classmethod
    def from_device_type(cls, device_type: DeviceType) -> "DeviceCapabilities":
        """Create capabilities for a known device type.

        Pythonic port of the C# ``static FromDeviceType`` switch-expression.
        Uses a dict dispatch keyed on :class:`DeviceType`; unknown types
        fall back to the parameterless defaults (matching the upstream
        ``_ => new DeviceCapabilities()`` discard arm).
        """

        # All Nyquist variants currently share the same capability set
        # (matches upstream); kept as separate entries so future divergence
        # is a one-line change rather than a structural refactor.
        nyquist_caps = dict(
            supports_streaming=True,
            has_sd_card=True,
            has_wifi=True,
            has_usb=True,
            max_sampling_rate=1000,
        )
        presets = {
            DeviceType.NYQUIST_1: nyquist_caps,
            DeviceType.NYQUIST_2: nyquist_caps,
            DeviceType.NYQUIST_3: nyquist_caps,
        }
        return cls(**presets.get(device_type, {}))


def detect_device_type_from_part_number(part_number: Optional[str]) -> DeviceType:
    """Detect the device type from a part-number string.

    Port of `Daqifi.Core.Device.DeviceTypeDetector.DetectFromPartNumber`.
    Returns `DeviceType.UNKNOWN` for missing, blank, or unrecognized values.
    """
    if not part_number or not part_number.strip():
        return DeviceType.UNKNOWN
    return {
        "nq1": DeviceType.NYQUIST_1,
        "nq2": DeviceType.NYQUIST_2,
        "nq3": DeviceType.NYQUIST_3,
    }.get(part_number.strip().lower(), DeviceType.UNKNOWN)


@dataclass
class DeviceMetadata:
    """Metadata and configuration information for a DAQiFi device.

    Ports the C# ``DeviceMetadata`` class from
    ``Daqifi.Core/Device/DeviceMetadata.cs``. Twelve mutable fields with
    sensible empty/zero defaults; the ``capabilities`` field is wired to
    a fresh :class:`DeviceCapabilities` instance via ``default_factory``
    to avoid the classic shared-mutable-default trap.

    Includes a Pythonic port of ``UpdateFromProtobuf`` (as
    :meth:`update_from_protobuf`) that takes any duck-typed object
    exposing the protobuf field names. Compatible with both the
    generated google.protobuf class and test fakes.
    """

    part_number: str = ""
    serial_number: str = ""
    firmware_version: str = ""
    hardware_revision: str = ""
    device_type: DeviceType = DeviceType.UNKNOWN
    capabilities: DeviceCapabilities = field(default_factory=DeviceCapabilities)
    ip_address: str = ""
    mac_address: str = ""
    ssid: str = ""
    host_name: str = ""
    device_port: int = 0
    wifi_security_mode: int = 0
    wifi_infrastructure_mode: int = 0

    def update_from_protobuf(self, message: Any) -> None:
        """Update fields from a `DaqifiOutMessage`-shaped object.

        Conditionally copies any non-empty / non-zero fields from
        the given protobuf message. Derives `device_type` and
        `capabilities` from `device_pn` when present; decodes
        IP and MAC byte fields via :mod:`daqifi.network`.

        Mirrors the C# `DeviceMetadata.UpdateFromProtobuf`. Imports
        the network helpers lazily so this module stays free of
        cross-package import cycles.
        """
        from daqifi.network import get_ip_address_string, get_mac_address_string

        if message is None:
            raise TypeError("message must not be None")

        device_pn = getattr(message, "device_pn", "")
        if device_pn and device_pn.strip():
            self.part_number = device_pn
            self.device_type = detect_device_type_from_part_number(device_pn)
            self.capabilities = DeviceCapabilities.from_device_type(self.device_type)

        # Protobuf carries device_sn as a numeric uint, but test
        # doubles and a future schema change might pass a string.
        # `device_sn != 0` evaluates to True for an empty string,
        # which would clobber a previously-discovered serial with
        # `""` (Qodo PR 17 pass 23 #1).
        device_sn = getattr(message, "device_sn", 0)
        if isinstance(device_sn, str):
            sn_str = device_sn.strip()
            if sn_str:
                self.serial_number = sn_str
        else:
            try:
                sn_int = int(device_sn)
            except (TypeError, ValueError):
                sn_int = 0
            if sn_int != 0:
                self.serial_number = str(sn_int)

        device_fw_rev = getattr(message, "device_fw_rev", "")
        if device_fw_rev and device_fw_rev.strip():
            self.firmware_version = device_fw_rev

        device_hw_rev = getattr(message, "device_hw_rev", "")
        if device_hw_rev and device_hw_rev.strip():
            self.hardware_revision = device_hw_rev

        ssid = getattr(message, "ssid", "")
        if ssid and ssid.strip():
            self.ssid = ssid

        host_name = getattr(message, "host_name", "")
        if host_name and host_name.strip():
            self.host_name = host_name

        # Tolerate non-numeric / out-of-range values from a stub or
        # an unexpected protobuf shape — a failed cast on one field
        # shouldn't drop the rest of the metadata update (Qodo PR 17
        # pass 32 #1).
        device_port = getattr(message, "device_port", 0)
        try:
            port_int = int(device_port)
        except (TypeError, ValueError):
            port_int = 0
        if 1 <= port_int <= 65535:
            self.device_port = port_int

        wifi_security_mode = getattr(message, "wifi_security_mode", 0)
        try:
            sec_int = int(wifi_security_mode)
        except (TypeError, ValueError):
            sec_int = 0
        if sec_int > 0:
            self.wifi_security_mode = sec_int

        wifi_inf_mode = getattr(message, "wifi_inf_mode", 0)
        try:
            inf_int = int(wifi_inf_mode)
        except (TypeError, ValueError):
            inf_int = 0
        if inf_int > 0:
            self.wifi_infrastructure_mode = inf_int

        # IP / MAC decoding can raise on missing or oddly-typed
        # protobuf byte fields. Catch so a single decoder failure
        # doesn't drop the rest of the metadata update (Qodo PR 17
        # pass 10 #5).
        try:
            ip = get_ip_address_string(message)
        except Exception:
            ip = ""
        if ip:
            self.ip_address = ip

        try:
            mac = get_mac_address_string(message)
        except Exception:
            mac = ""
        if mac:
            self.mac_address = mac

        # Same hardening as the connection-info fields above (Qodo PR
        # 17 pass 33 #1 / pass 32 #1) — non-numeric values would
        # raise on the `> 0` comparison or the int() cast and drop
        # the rest of the metadata update.
        analog_in_port_num = getattr(message, "analog_in_port_num", 0)
        try:
            ai_int = int(analog_in_port_num)
        except (TypeError, ValueError):
            ai_int = 0
        if ai_int > 0:
            self.capabilities.analog_input_channels = ai_int

        analog_out_port_num = getattr(message, "analog_out_port_num", 0)
        try:
            ao_int = int(analog_out_port_num)
        except (TypeError, ValueError):
            ao_int = 0
        if ao_int > 0:
            self.capabilities.analog_output_channels = ao_int

        digital_port_num = getattr(message, "digital_port_num", 0)
        try:
            d_int = int(digital_port_num)
        except (TypeError, ValueError):
            d_int = 0
        if d_int > 0:
            self.capabilities.digital_channels = d_int


class PowerState(IntEnum):
    """Device power states (matches firmware PowerApi.h)"""
    STANDBY = 0            # MCU on if USB powered, off if battery powered
    POWERED_UP = 1         # Board fully powered (all modules active)
    POWERED_UP_EXT_DOWN = 2  # Partial power, external power disabled (low battery mode)


class SerialInterface:
    """Low-level serial communication with Nyquist devices"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        """
        Initialize serial connection parameters.

        Args:
            port: Serial port path (e.g., /dev/ttyACM0 on Linux, COM3 on Windows)
            baudrate: Serial baud rate (default 115200)
            timeout: Read timeout in seconds
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None

    def open(self) -> bool:
        """
        Open serial connection.

        Sends ``SYSTem:ECHO -1`` after the port stabilizes so the
        firmware suppresses both command-echo and the ``DAQIFI>``
        prompt for the rest of the session. This matches the .NET core
        (``DisableDeviceEcho``) initialization pattern and eliminates
        the heuristic prompt/echo stripping every receive method
        otherwise has to do (closes daqifi/daqifi-python-core#2).

        The existing string-replace fallbacks in ``send_command()`` /
        ``sd_get_file*`` are intentionally retained as defense-in-depth
        for transports where the echo-disable command itself is lost
        (a flaky pre-stable USB CDC enumeration, for example).

        Returns:
            True if connection successful, False otherwise.
        """
        # Pick a finite POSITIVE write_timeout — the open-time echo-
        # disable write must NEVER block forever on a flaky USB CDC
        # enumeration, but timeout=0 (non-blocking write) and
        # timeout<0 (invalid) would both break the disable path.
        # (Qodo PR #55 /improve pass 1 importance 7 + pass 3
        # importance 7 — clamp to positive.)
        write_timeout = 1.0
        if self.timeout is not None and self.timeout > 0:
            write_timeout = self.timeout
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=write_timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            # Wait for device to stabilize
            time.sleep(0.1)
            # Clear any pending data
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            # Disable echo + prompt for the rest of the session.
            # Failure here doesn't abort the connection — the existing
            # response-stripping fallbacks compensate; we just won't
            # get the cleaner binary stream the disable affords.
            # Catch specific I/O exceptions (not bare Exception) so a
            # programmer error inside _send_disable_echo isn't silently
            # swallowed (Qodo PR #55 /improve pass 1 importance 7).
            try:
                self._send_disable_echo()
            except (serial.SerialException, OSError):
                pass
            return True
        except (serial.SerialException, OSError) as e:
            # Return False instead of raising to allow discovery to continue
            return False

    def _send_disable_echo(self) -> None:
        """Send ``SYSTem:ECHO -1`` raw, bypassing send_command's
        echo-strip path so the response (if any) is discarded.

        Kept private + bare-bones because at open() time the device
        may still be in the slow USB-CDC settling window where
        send_command's read-loop would burn the full timeout.

        Drain strategy (Qodo PR #55 review #1): after the initial 100ms
        settle, poll in_waiting in a short loop and read whatever's
        there until the buffer stays empty for two consecutive polls
        OR a 500ms deadline elapses. Then call reset_input_buffer()
        as belt-and-suspenders so a discovery flow's *IDN? right
        after open() can't pick up stale echo bytes.
        """
        if self._serial is None or not self._serial.is_open:
            return
        # Use \r\n to match ScpiMessage.get_bytes() — the canonical
        # wire format. send_command() uses bare \r for legacy
        # compatibility but new writes should use the canonical form
        # (Qodo PR #55 /improve pass 4 importance 7).
        self._serial.write(b"SYSTem:ECHO -1\r\n")
        self._serial.flush()
        time.sleep(0.1)
        # Drain loop — bytes can arrive after the initial settle if the
        # firmware is still flushing the echo from the disable command
        # itself. Stable-empty for 2 consecutive polls (or 500 ms total)
        # before stopping.
        # Monotonic clock for the deadline so a system clock adjustment
        # doesn't make the loop run forever or terminate immediately
        # (Qodo PR #55 /improve pass 5 importance 6).
        deadline = time.monotonic() + 0.5
        empty_polls = 0
        while time.monotonic() < deadline and empty_polls < 2:
            try:
                pending = self._serial.in_waiting
            except (serial.SerialException, OSError):
                break
            if pending:
                try:
                    self._serial.read(pending)
                except (serial.SerialException, OSError):
                    break
                empty_polls = 0
            else:
                empty_polls += 1
            time.sleep(0.02)
        # Belt-and-suspenders: explicit input-buffer reset so any
        # bytes still in flight when we stopped polling don't bleed
        # into the next caller's read (e.g. discovery's *IDN?).
        try:
            if hasattr(self._serial, "reset_input_buffer"):
                self._serial.reset_input_buffer()
        except (serial.SerialException, OSError):
            pass

    def close(self):
        """Close serial connection. Restores echo to terminal-friendly
        defaults (SYSTem:ECHO 1) before closing so a human opening the
        port in a serial terminal afterward sees the prompt."""
        if self._serial and self._serial.is_open:
            try:
                self._serial.write(b"SYSTem:ECHO 1\r\n")
                self._serial.flush()
                time.sleep(0.05)
            except (serial.SerialException, OSError):
                # Best-effort; the underlying port may already be gone.
                pass
            self._serial.close()

    def is_open(self) -> bool:
        """Check if serial connection is open"""
        return self._serial is not None and self._serial.is_open

    def send_command(self, command: str, delay: float = 0.1) -> str:
        """
        Send SCPI command and read response.

        Args:
            command: SCPI command string (without terminator)
            delay: Delay in seconds before reading response

        Returns:
            Response string (stripped of whitespace)

        Raises:
            RuntimeError: If serial port not open or communication fails
        """
        if not self.is_open():
            raise RuntimeError("Serial port not open")

        try:
            # Send command with CR terminator
            cmd_bytes = (command + "\r").encode('ascii')
            self._serial.write(cmd_bytes)
            self._serial.flush()

            # Wait for device to process
            time.sleep(delay)

            # Read all available data
            response = b""
            start_time = time.time()

            while (time.time() - start_time) < self.timeout:
                if self._serial.in_waiting > 0:
                    chunk = self._serial.read(self._serial.in_waiting)
                    response += chunk
                    # Give a bit more time for any trailing data
                    time.sleep(0.05)
                    # If nothing more comes, we're done
                    if self._serial.in_waiting == 0:
                        break
                else:
                    # No data yet, wait a bit
                    time.sleep(0.01)

            # Decode and clean response
            response_str = response.decode('ascii', errors='ignore')

            # Remove echoed command if present
            if command in response_str:
                response_str = response_str.replace(command, "")

            # Remove DAQIFI> prompt if present
            response_str = response_str.replace("DAQIFI>", "")

            # Clean up whitespace and control characters
            response_str = response_str.strip()

            return response_str

        except (serial.SerialException, OSError) as e:
            raise RuntimeError(f"Serial communication error: {e}")

    def send_query(self, query: str, delay: float = 0.1) -> str:
        """
        Send SCPI query (command ending with '?') and return response.

        Args:
            query: SCPI query string (should end with '?')
            delay: Delay in seconds before reading response

        Returns:
            Response value (stripped of whitespace and quotes)
        """
        response = self.send_command(query, delay)
        # Remove any surrounding quotes from response
        response = response.strip('"').strip("'")
        return response

    def send_set(self, command: str, value, delay: float = 0.1) -> bool:
        """
        Send SCPI set command with value.

        Args:
            command: SCPI command string (without value)
            value: Value to set
            delay: Delay in seconds before reading response

        Returns:
            True if command succeeded (no error), False otherwise
        """
        # Delegate to send_set_with_error; collapse to legacy bool return.
        success, _err = self.send_set_with_error(command, value, delay)
        return success

    def send_set_with_error(
        self, command: str, value, delay: float = 0.1
    ) -> tuple[bool, str]:
        """Send SCPI set command and return (success, raw_error_response).

        Each ``SYST:ERR?`` query advances the device's error queue, so a
        caller that needs to inspect the error after a failed set MUST
        use the response captured here — re-querying would read the next
        queue entry and likely see ``0,"No error"``, silently swallowing
        the real failure (closes Qodo PR #53 review #1).

        Returns:
            ``(success, error_response)`` where ``success`` is True iff
            the response contained ``"No error"`` (legacy semantics) and
            ``error_response`` is the raw ``SYST:ERR?`` reply for callers
            that want to parse code + description.
        """
        # Format value based on type
        if isinstance(value, str):
            cmd = f"{command} {value}"
        elif isinstance(value, bool):
            cmd = f"{command} {1 if value else 0}"
        elif isinstance(value, (int, float)):
            cmd = f"{command} {value}"
        else:
            cmd = f"{command} {str(value)}"

        self.send_command(cmd, delay)

        # Read the error queue exactly once — repeated queries advance
        # it. The raw response is returned so callers can parse it
        # without re-reading.
        error = self.send_query("SYST:ERR?", 0.1)
        return ("No error" in error, error)

    def drain_errors(
        self, max_iterations: int = 256, empty_budget: int = 3
    ) -> list[str]:
        """Pop ``SYST:ERR?`` until the queue reports ``"No error"``,
        ``empty_budget`` consecutive empty/unparseable replies arrive,
        or ``max_iterations`` is hit. Returns the popped errors.

        Closes Gap 2 of daqifi/daqifi-python-core#22: the prior
        ``clear_errors()`` discarded errors silently and capped at 10.
        On a device exercised by another session (or one that's
        crashed mid-stream) >10 errors can be queued — observed on
        COM7, ~12 errors had to drain before ``StartStreamData``
        would succeed.

        ``empty_budget`` (default 3) is the **fail-fast** escape
        hatch surfaced by Qodo PR #62 review #1: if the device
        intermittently returns empty replies (USB CDC timeout, no
        bytes within the read window), the prior unconditional
        ``max_iterations`` cap could stall a caller for ~25s
        (256 × 0.1s send_query delay) waiting for a response that
        never comes. Bailing after N consecutive unproductive reads
        bounds the worst case at ~0.3s while still draining all
        errors when the device IS responsive.

        A reply is "unproductive" if it's empty/whitespace OR
        doesn't match the SCPI ``<int>,"<msg>"`` shape that
        firmware uses for both error AND no-error responses —
        i.e. the device gave us nothing we can act on.
        """
        popped: list[str] = []
        consecutive_empty = 0
        for _ in range(max_iterations):
            error = self.send_query("SYST:ERR?", 0.05)
            stripped = error.strip() if error else ""
            if not stripped:
                consecutive_empty += 1
                if consecutive_empty >= empty_budget:
                    break
                continue
            if "No error" in stripped:
                return popped
            # Treat replies that don't have the SCPI error shape
            # (no comma between code and message) as unproductive
            # too — they're likely echo collisions or noise, not
            # real error entries to track.
            if "," not in stripped:
                consecutive_empty += 1
                if consecutive_empty >= empty_budget:
                    break
                continue
            consecutive_empty = 0
            popped.append(error)
        return popped

    def clear_errors(self) -> None:
        """Backwards-compatible shim — discards drained errors.

        Prefer :meth:`drain_errors` for new code so the popped error
        list is available for logging/raising.
        """
        self.drain_errors()

    def reset(self):
        """Send *RST command to reset device"""
        self.send_command("*RST", delay=0.5)
        self.clear_errors()

    def __enter__(self):
        """Context manager entry"""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()
