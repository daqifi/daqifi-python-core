"""
Internal serial communication layer for DAQiFi Nyquist devices.

This module provides low-level SCPI command interface over serial port.
Not intended for direct use - use the public API instead.
"""

import errno
import math
import random
import re
import serial
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import IO, Any, Callable, Generic, List, Optional, Protocol, Sequence, Tuple, TypeVar, Union, runtime_checkable
from enum import IntEnum
import builtins

from .scpi import ConnectionStatus


T = TypeVar("T")

#: Deadline for SerialInterface.close()'s output drain, in seconds (#259).
#:
#: ⛔ close() used to call serial.flush() with no bound at all, and
#: write_timeout does not govern flush(): pyserial 3.5 polls out_waiting with
#: no deadline on Windows and calls tcdrain() on POSIX, neither of which takes
#: a timeout. So a device that stopped draining hung disconnect() forever,
#: AFTER the session's work was finished and before the caller could report
#: its result.
#:
#: Generous on purpose. A real post-soak USB-CDC drain on this hardware has
#: measured over 5 s and under 20 s, so a short bound would report a LIVE
#: device as wedged — which is the failure this bound exists to avoid
#: creating, not one to trade for. Per-instance via
#: SerialInterface.close_drain_timeout.
CLOSE_DRAIN_TIMEOUT_S = 20.0

#: Deadline for open()'s echo-disable drain, in seconds (#262).
#:
#: open() used to call serial.flush() after writing ``SYSTem:ECHO -1``, the
#: same unbounded wait close() had (see CLOSE_DRAIN_TIMEOUT_S), and the
#: ``except SerialException`` around it could not help: a blocked tcdrain()
#: raises nothing, so connect() simply never returned.
#:
#: Sized for what open() writes, not for close()'s post-soak case: 16 bytes
#: into a freshly reset output buffer. That is ~1.4 ms on the wire at
#: 115200 baud and ~17 ms even at 9600, and a USB-CDC port hands it to the
#: host stack in one transfer. 1 s is a margin of 60x over the slowest of
#: those, while still turning a wedged adapter into a prompt connect failure
#: rather than a hang. Per-instance via SerialInterface.open_drain_timeout.
OPEN_DRAIN_TIMEOUT_S = 1.0

#: Deadline for the drain after a command write, in seconds (#263).
#:
#: send_command(), query_binary(), the SD file getters and streaming's START
#: wrote a command and then called serial.flush(), the same unbounded wait
#: close() (#259) and open() (#262) had: a port that stopped taking bytes
#: blocked the caller forever, with nothing raised. They now use the same
#: bounded poll, drain_output_bounded().
#:
#: A command is tens of bytes, which a USB-CDC port hands to the host stack
#: in one transfer and a 9600-baud UART sends in well under 0.1 s. 5 s is
#: generous for that while still turning a wedged port into an error the
#: caller sees, rather than a hang. Per-instance via
#: SerialInterface.send_drain_timeout.
SEND_DRAIN_TIMEOUT_S = 5.0


def _echo_spelling_family():
    """The 1024 spellings of one command: 256 case variants x 4 zero forms.

    ⛔ BITS MAP ONTO LETTER POSITIONS ONLY. An earlier revision enumerated
    every character of `SYST:ECHO`, so one bit toggled the `:` (a no-op)
    and the last letter never toggled at all -- 1024 entries holding only
    512 distinct values, each duplicated. The symptom was obscure: a single
    written line matched TWO family members, so every "how many spellings
    did this use" count doubled. Distinctness is asserted below rather than
    left to the reader of the comprehension.
    """
    base = "SYST:ECHO"
    letters = [i for i, c in enumerate(base) if c.isalpha()]
    out = []
    for bits in range(1 << len(letters)):
        chars = list(base)
        for k, i in enumerate(letters):
            if (bits >> k) & 1:
                chars[i] = chars[i].lower()
        head = "".join(chars).encode("ascii")
        for z in range(4):
            out.append(head + b" -" + b"0" * z + b"1")
    assert len(set(out)) == len(out), "duplicate spellings in the family"
    return tuple(out)


def drain_output_bounded(port, deadline_s: float) -> None:
    """Wait, BOUNDED, for ``port``'s OS output queue to empty. No ``flush()``.

    The one implementation of the bounded drain (#259, #262, #263); see
    SerialInterface._drain_bounded for why there is no ``flush()`` after it.
    Takes the port explicitly so a caller holding its own reference
    (query_binary, the SD getters, streaming's START) drains THAT port.

    Raises ``serial.SerialTimeoutException`` if the queue does not empty in
    time, or if the port exposes no ``out_waiting`` (an unbounded flush would
    be the fail-open).
    """
    end = time.monotonic() + deadline_s
    try:
        pending = port.out_waiting
    except (AttributeError, NotImplementedError) as exc:
        raise serial.SerialTimeoutException(
            "this port exposes no out_waiting, so the drain cannot be "
            "bounded; refusing an unbounded flush"
        ) from exc
    while pending:
        if time.monotonic() >= end:
            raise serial.SerialTimeoutException(
                f"output buffer still holds {pending} byte(s) after "
                f"{deadline_s:.1f}s"
            )
        time.sleep(0.01)
        pending = port.out_waiting


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
    # Which connection the event is about, for transports that number them
    # (TcpStreamTransport.session). None means "the current one".
    session: Optional[int] = None


@dataclass
class ConnectionRetryOptions:
    """Retry/backoff configuration for :meth:`StreamTransport.connect`.

    Ports the C# ``ConnectionRetryOptions``. Durations are floats in
    seconds (Pythonic) instead of ``System.TimeSpan``.

    Values are checked when set, at construction or later, as upstream's
    setters check them (daqifi-core 14fb907): at least one attempt,
    non-negative delays, a backoff multiplier of at least 1, and a
    connection timeout of at least 1 ms. An invalid value raises
    :class:`ValueError` (a non-number, :class:`TypeError`) instead of
    being retried with backoff or skipping every attempt.
    """

    max_attempts: int = 3
    initial_delay: float = 1.0          # seconds
    max_delay: float = 30.0             # seconds
    backoff_multiplier: float = 2.0
    connection_timeout: float = 5.0     # seconds
    enabled: bool = True

    def __setattr__(self, name: str, value: Any) -> None:
        _validate_retry_option(name, value)
        super().__setattr__(name, value)

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


_NOT_FOUND_ERRNOS = {errno.ENOENT, errno.ENODEV, errno.ENXIO}
_IN_USE_ERRNOS = {errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK}
_DENIED_ERRNOS = {errno.EACCES, errno.EPERM}


def classify_serial_open_error(exc: BaseException, platform: Optional[str] = None):
    """Why a serial port didn't open, from the error the open raised
    (port of daqifi-core da74a4c's classification, on what pyserial
    reports).

    POSIX pyserial carries the ``errno``: ENOENT/ENODEV/ENXIO is a
    missing port; EBUSY, or EAGAIN from the exclusive lock, is a port in
    use; EACCES/EPERM is a permission gate (``dialout``). Windows pyserial
    carries no errno, only the ``WinError`` text: "cannot find the file"
    is a missing port, and "Access is denied" is a port another program
    holds (Windows has no permission gate on COM ports). Anything else is
    UNKNOWN. The error's cause and context are searched too.
    """
    from ..exceptions import SerialPortConnectFailure as F

    platform = platform or sys.platform
    seen = set()
    chain = []
    node: Optional[BaseException] = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        node = node.__cause__ or node.__context__
    for err in chain:
        if isinstance(err, FileNotFoundError):
            return F.NOT_FOUND
        code = getattr(err, "errno", None)
        if isinstance(code, int):
            if code in _NOT_FOUND_ERRNOS:
                return F.NOT_FOUND
            if code in _IN_USE_ERRNOS:
                return F.IN_USE
            if code in _DENIED_ERRNOS:
                return F.IN_USE if platform.startswith("win") else F.ACCESS_DENIED
        text = str(err).lower()
        if "cannot find the file" in text or "filenotfounderror" in text:
            return F.NOT_FOUND
        if "exclusively lock" in text or "resource busy" in text:
            return F.IN_USE
        if "access is denied" in text or "permissionerror" in text:
            return F.IN_USE if platform.startswith("win") else F.ACCESS_DENIED
    return F.UNKNOWN


def serial_open_error(port: str, exc: BaseException):
    """A :class:`SerialPortConnectError` for ``exc``, raised opening ``port``."""
    from ..exceptions import SerialPortConnectError

    return SerialPortConnectError(port, classify_serial_open_error(exc), str(exc))


def _validate_retry_option(name: str, value: Any) -> None:
    if name == "enabled":
        return
    if name not in (
        "max_attempts", "initial_delay", "max_delay", "backoff_multiplier",
        "connection_timeout",
    ):
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number (got {value!r}).")
    if name == "max_attempts":
        if not isinstance(value, int) or value < 1:
            raise ValueError(
                f"max_attempts must be an integer of at least 1 (got {value!r}): "
                "at least one connection attempt must be allowed."
            )
        return
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"{name} must be finite (got {value!r}).")
    if name in ("initial_delay", "max_delay") and value < 0:
        raise ValueError(f"{name} must not be negative (got {value!r}).")
    if name == "backoff_multiplier" and value < 1:
        raise ValueError(f"backoff_multiplier must be at least 1 (got {value!r}).")
    if name == "connection_timeout" and value < 0.001:
        raise ValueError(
            f"connection_timeout must be at least 1 ms (got {value!r} s)."
        )


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
        device_path: Optional[Union[str, bytes]] = None,
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


class PortCancelled(serial.SerialException):
    """The port's user was cancelled (given up on); nothing more may be done
    on the port but closing it."""


class _OpenDrainFailed(serial.SerialException):
    """open()'s echo-disable drain did not finish (#262): it missed its
    bound, the port failed under it (an I/O error), or the caller cancelled
    during it.

    Private: open() turns it into a refused open, with a
    SerialPortConnectError in ``last_open_error`` (or no error, for a
    cancel). It exists only so open() can tell a drain that never finished
    (fatal) from a failed write (tolerated), which share
    SerialTimeoutException otherwise.
    """


class CancellablePort:
    """A serial port that refuses every operation but ``close`` once
    ``cancel_event`` is set.

    A discovery probe abandoned after its hard timeout stops at its next
    operation: attribute reads and writes are checked, and a method is
    checked again when it is called, not only when it is looked up (in
    ``port.read(port.in_waiting)`` the ``read`` is resolved before
    ``in_waiting`` runs). The first refused operation raises
    :class:`PortCancelled`, a SerialException, which the probes already
    handle by stopping.

    This keeps an abandoned probe from acting on the board. It can't reach
    into the port's constructor or into a call already running; what stops
    such a call from touching a newer session's data is that python-core
    opens ports exclusively (see :class:`SerialInterface`), so no newer
    python-core session can open the port while the probe holds it.
    """

    _ALWAYS_ALLOWED = frozenset({"close", "is_open"})

    def __init__(self, port: Any, cancel_event: threading.Event) -> None:
        object.__setattr__(self, "_port", port)
        object.__setattr__(self, "_cancel_event", cancel_event)

    def _check(self, name: str) -> None:
        if name not in self._ALWAYS_ALLOWED and self._cancel_event.is_set():
            raise PortCancelled(f"port operation {name!r} after cancellation")

    def __getattr__(self, name: str) -> Any:
        self._check(name)
        value = getattr(self._port, name)
        if not callable(value):
            return value

        def _checked(*args: Any, **kwargs: Any) -> Any:
            self._check(name)
            return value(*args, **kwargs)

        return _checked

    def __setattr__(self, name: str, value: Any) -> None:
        self._check(name)
        setattr(self._port, name, value)


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
        # Why the last open() failed, as a SerialPortConnectError (open()
        # returns False so discovery can go on probing).
        self.last_open_error: Optional[BaseException] = None
        # Set by a caller that has given up on this interface (a discovery
        # probe past its hard timeout): from then on the port is used for
        # nothing but closing (see CancellablePort), so a late open or a
        # stalled call can't disturb a newer session on it.
        self.cancel_event: Optional[threading.Event] = None
        # Called by open() on the freshly opened handle BEFORE this library
        # writes anything of its own, so a caller can confirm the device is
        # the one it meant to address and refuse without touching it. Set the
        # same way cancel_event is — an optional attribute, not a constructor
        # parameter — because that is the shape the one existing write-free
        # lever already uses. See open() for the contract (#259).
        self.verify: Optional[Callable[[Any], bool]] = None
        # Why the last close() could not drain, if it could not. close()
        # never raises: a dying device must not turn a finished session into
        # a teardown traceback. The fault is recorded here and close()
        # returns False, so a caller that cares can tell (#259).
        self.last_close_error: Optional[BaseException] = None
        #: Deadline for close()'s drain, in seconds. Generous on purpose: a
        #: real post-soak USB-CDC drain on this hardware has measured over
        #: 5 s and under 20 s, so a short bound would report a LIVE device as
        #: wedged — the failure this bound exists to avoid creating.
        self.close_drain_timeout: float = CLOSE_DRAIN_TIMEOUT_S
        #: Deadline for open()'s echo-disable drain, in seconds. A drain that
        #: misses it fails the open and releases the port (#262).
        self.open_drain_timeout: float = OPEN_DRAIN_TIMEOUT_S
        #: Deadline for the drain after each command write, in seconds. A
        #: drain that misses it raises instead of hanging the caller (#263).
        self.send_drain_timeout: float = SEND_DRAIN_TIMEOUT_S
        #: Echo-setter spellings written on THIS connection whose echo has not
        #: been accounted for. A new attempt never reuses one.
        #:
        #: ⛔ PER INSTANCE, AND IT HAS TO BE. The poison flag this replaces
        #: was a CLASS attribute and that was safe, because `self.x = v`
        #: REBINDS onto the instance. A set is MUTATED instead:
        #: `self.x.add(v)` would reach the one class-level object, so every
        #: SerialInterface in the process would share a single pool and one
        #: device's unconfirmed attempts would exhaust another's family and
        #: refuse its downloads.
        #: `test_two_connections_do_not_share_the_spelling_pool` pins it.
        #: spelling -> how many copies we have WRITTEN whose complete echo
        #: we have not yet observed. A spelling is drawable only at zero,
        #: and a key at zero is deleted so `in`, `len` and `== {}` all read
        #: naturally.
        #:
        #: ⛔ r14 AUDIT, HIGH: A COUNT, NOT A SET. Reserve-on-write made
        #: multiplicity real -- an attempt writes a spelling and a caller
        #: writes the same text, or two callers do -- and a set cannot
        #: represent "owed twice". Both partial release paths then freed the
        #: spelling on seeing ONE copy while another was still queued, and
        #: the next draw synced on the duplicate: measured
        #: b'SYST:ECHO -1\r\nabc'.
        #:
        #: ⚠ Success still clears EVERYTHING and needs no counts: FIFO says
        #: every earlier copy is behind our marker or never coming. The
        #: counts exist only to make the two PARTIAL release paths honest.
        self._echo_spellings_outstanding: dict = {}
        #: At most one `(spelling, expected_remaining_suffix)` from an
        #: attempt whose marker was cut by its own deadline.
        #:
        #: ⛔ r13 AUDIT, MEDIUM, and INTRODUCED by r13's simplification. If
        #: every echo's CR lands inside the budget and its LF outside, each
        #: attempt discards a partial marker and the next consumes the
        #: orphan LF without releasing anything -- so every spelling goes
        #: stale and the connection refuses FOREVER on a healthy device.
        #:
        #: ⚠ ONE, NOT A GENERAL TAIL. The carried tail is what r9-r12 kept
        #: breaking; this remembers a single expected continuation, and FIFO
        #: guarantees it is the very next thing in the stream. An older
        #: partial is ahead of it in FIFO and is covered by the next
        #: success's clear.
        self._echo_pending_partial = None
        #: Tail of the previous write, so a spelling split across two
        #: write() calls is still reserved.
        self._echo_write_carry: bytes = b""

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _refuse_open(self, error: Optional[BaseException]) -> bool:
        """Close the port, write nothing more, and report the refusal.

        The shape is lifted from open()'s own cancel branch, which already
        had it right: close the handle, drop it, return False. Factored out
        so the verify paths cannot drift from the cancel path.
        """
        try:
            if self._serial is not None:
                self._serial.close()
        except (serial.SerialException, OSError):
            # The handle is being abandoned either way; a failure to close it
            # must not mask the refusal that got us here.
            pass
        self._serial = None
        self.last_open_error = error
        return False

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

        The port is opened exclusively (``exclusive=True``: an advisory
        ``flock`` on POSIX; Windows COM ports are exclusive anyway), so a
        second python-core session can't open it meanwhile, and one that
        tries gets an error rather than a share of this one's data.

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
        self.last_open_error = None
        # ⛔ A REOPEN RESYNCHRONISES NOTHING, and this is kept because it is
        # WHY the download no longer probes at all. close()/open() resets
        # HOST-side buffers, which discards only what has already ARRIVED;
        # replies the device has already queued survive, and the firmware's
        # DTR handler does not cancel pending output.
        #
        # r2-r5 carried a per-object "poison" flag to refuse downloads after
        # an unaccounted-for probe, and open() deliberately did not clear it.
        # The r5 audit showed the flag could not work at all: it is
        # per-OBJECT and the stale bytes are on the DEVICE, so a new
        # connection object -- which r5's own refusal message advised --
        # starts clean and adopts them. Measured: the file came back with
        # b'-1\r\n' spliced into its middle.
        #
        # So there is nothing here to clear any more. The download SETS the
        # echo state and synchronises on its own echo, which is exactly
        # known bytes; see silence_echo_for_download().
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=write_timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                exclusive=True,
            )
            if self.cancel_event is not None:
                self._serial = CancellablePort(self._serial, self.cancel_event)
            if self._cancelled():
                # Opened only after the caller gave up: close, send nothing.
                self._serial.close()
                self._serial = None
                return False
            # Wait for device to stabilize
            time.sleep(0.1)
            # Clear any pending data
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            # ---- #259: the caller's own look at the device, before ours ----
            # Runs here and not earlier: the settle and the buffer resets
            # above touch no device state (they clear HOST-side buffers), so
            # the callable gets a stabilised port and a clean buffer. Runs
            # here and not later because the next statement is the first byte
            # this library puts on the wire.
            #
            # ⚠ THE CALLABLE MAY WRITE, and that is not a contradiction.
            # `*IDN?` is a write. What this guarantees is that the LIBRARY
            # writes nothing on its own behalf until the caller has decided;
            # the caller's own verification bytes are the whole point.
            #
            # It is handed `self._serial` POST-WRAP, i.e. the same object the
            # library will use. With cancel_event set that is a
            # CancellablePort, so a verify that stalls is still cancellable
            # rather than defeating the one lever that stops an abandoned
            # probe.
            if self.verify is not None:
                try:
                    verdict = self.verify(self._serial)
                except BaseException as verify_exc:   # noqa: BLE001
                    # CANCEL WINS. If the event was set while the callable
                    # ran, its next port operation raised PortCancelled, and
                    # that is a cancellation rather than a judgement about
                    # the device — report it as the cancel branch does, with
                    # no error, because the caller asked for this.
                    if self._cancelled():
                        return self._refuse_open(None)
                    # Otherwise the callable's own exception is preserved
                    # EXACTLY, not wrapped: _connect() re-raises
                    # last_open_error, so the caller gets what its own code
                    # raised. Deliberately NOT allowed to reach the
                    # `except (SerialException, OSError)` below, which would
                    # turn a considered refusal into a port-open failure.
                    return self._refuse_open(verify_exc)
                # Checked again after a CLEAN return, too: the callable may
                # have finished just as the caller gave up, and a truthy
                # verdict must not authorise a write past a cancellation.
                if self._cancelled():
                    return self._refuse_open(None)
                if not verdict:
                    # Imported here, not at module scope: this file imports
                    # from ..exceptions lazily throughout (see
                    # serial_open_error) to keep the import graph acyclic.
                    from ..exceptions import DeviceVerificationError
                    return self._refuse_open(
                        DeviceVerificationError(self.port))
            # Disable echo + prompt for the rest of the session.
            # A failed WRITE doesn't abort the connection — the existing
            # response-stripping fallbacks compensate; we just won't
            # get the cleaner binary stream the disable affords.
            # Catch specific I/O exceptions (not bare Exception) so a
            # programmer error inside _send_disable_echo isn't silently
            # swallowed (Qodo PR #55 /improve pass 1 importance 7).
            #
            # ⛔ #262: A DRAIN THAT DOES NOT FINISH FAILS THE OPEN. It is
            # not the tolerable "echo-disable was lost" case above: it means
            # the port is not taking bytes, so every later command would
            # stall the same way. Release the handle and report it, as the
            # other refusals do — never continue into a session on it.
            try:
                self._send_disable_echo()
            except _OpenDrainFailed as drain_exc:
                if self._cancelled():
                    return self._refuse_open(None)
                cause = drain_exc.__cause__ or drain_exc
                error = serial_open_error(self.port, cause)
                error.__cause__ = cause
                return self._refuse_open(error)
            except (serial.SerialException, OSError):
                pass
            return True
        except (serial.SerialException, OSError) as e:
            # Return False instead of raising to allow discovery to continue;
            # keep the cause for callers that report it.
            error = serial_open_error(self.port, e)
            error.__cause__ = e
            self.last_open_error = error
            # Close a port that opened before the failure (a cancelled one
            # included), rather than leave its handle behind.
            if self._serial is not None:
                try:
                    self._serial.close()
                except (serial.SerialException, OSError):
                    pass
                self._serial = None
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
        self._write_to_port(b"SYSTem:ECHO -1\r\n")
        # #262: bounded, and NO flush(). flush() is tcdrain() on POSIX and
        # takes no timeout, so it hung connect() forever on a port that had
        # stopped draining; see _drain_bounded. Its failure is re-raised as
        # _OpenDrainFailed so open() can tell it from a failed write.
        # PortCancelled is included because the poll, unlike the one flush()
        # call it replaces, re-checks a CancellablePort on every iteration:
        # a cancel mid-drain must reach open()'s cancel handling, not be
        # swallowed as a lost echo-disable and reported as a good open.
        # ANY failure of the drain fails the open, not only the timeout: an
        # out_waiting poll on a port that went away mid-open raises
        # OSError(EIO) or a SerialException, and leaving those to open()'s
        # tolerant write handler returned True on a dead port with the handle
        # still held (#262 r1). Only the WRITE above stays tolerated.
        # OSError covers all of them: in pyserial 3.5 SerialException (and so
        # SerialTimeoutException and PortCancelled) derives from IOError.
        try:
            self._drain_bounded(self.open_drain_timeout)
        except OSError as exc:
            raise _OpenDrainFailed(str(exc)) from exc
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

    #: How long :meth:`silence_echo_for_download` waits for the setter's
    #: own echo before REFUSING the download.
    #:
    #: ⚠ Sized for a device, not for a unit test. The probe's 0.5 s was a
    #: budget to give up and fall back in; this is a budget to declare the
    #: link unresponsive in, and a device that answers in 0.65 s is normal.
    #: Measured consequence of the old value: every download against such a
    #: device refused.
    ECHO_SET_TIMEOUT_S = 2.0

    #: ⛔ A SET, NOT A QUERY, and that is the r6 redesign in one line.
    #: `SYSTem:ECHO?` admits no nonce, so two probes' replies are
    #: byte-identical and NO reply can be tied to the probe that asked for
    #: it. r2-r5 tried to cover that with a host-side per-object "poison"
    #: flag; the r5 audit killed it in one sentence -- the flag is
    #: per-object and the stale bytes live on the DEVICE, so a new
    #: connection object, a new process or a different client inherits the
    #: previous object's reply as its own confirmation. Measured on
    #: d8930bad: the file came back as
    #: b'000,001,...,016,-1\r\n017,...' -- the reply SPLICED INTO THE
    #: MIDDLE of a data file, which is worse than a visible prepend.
    #:
    #: A set has no reply to misattribute. After it the state is known by
    #: construction, so there is nothing to attribute and nothing to poison.
    #: ⛔ THE SHORT FORM, AND THAT IS THE DISCRIMINATOR. open() writes the
    #: FULL `SYSTem:ECHO -1` and close() writes `SYSTem:ECHO 1`, so if the
    #: download used the full spelling too its marker would be
    #: byte-identical to open()'s echo -- and the download would
    #: synchronise on OPEN'S marker and leave its own in flight. Measured
    #: on exactly that collision: a connect immediately followed by a
    #: download returned
    #: b'000,...,004,SYSTem:ECHO -1\r\nDAQIFI> 005,...'.
    #:
    #: Legal per the SCPI abbreviation rule: `SYSTem` truncates at its first
    #: lowercase to `SYST`, and `ECHO` is all-caps so it has exactly one
    #: spelling. The echo is verbatim, so the short form comes back short
    #: and nothing this library writes elsewhere can forge it.
    #: `test_the_download_marker_cannot_be_forged_by_open_or_close` pins the
    #: distinctness, so changing open()'s spelling fails loudly here rather
    #: than silently recreating the collision.
    #: The family's first member, kept as a name for the pins that compare
    #: the download's marker against open()'s and close()'s traffic. Every
    #: member shares this prefix, so distinctness proven for it holds for
    #: all of them.
    _ECHO_SET_SILENT = b"SYST:ECHO -1"
    _ECHO_PROMPT = b"DAQIFI> "

    #: ⛔ WRITTEN FIRST, AND ONLY TO GUARANTEE THE MARKER. With echo already
    #: silent, `SYSTem:ECHO -1` produces NOTHING -- no echo, no reply -- so
    #: there is no known byte to synchronise on and foreign bytes still in
    #: flight (another client's `SYSTem:ECHO?` reply, an older version of
    #: this library) land inside the file. Measured: a stale `-1\r\n` due
    #: 0.65 s out arrived 150 ms into the payload.
    #:
    #: Turning echo ON first makes the next line's echo CERTAIN, because
    #: microrl gates on the state at the time the characters arrive. So the
    #: pair always produces one exactly-known marker, in every starting
    #: state, and everything ahead of it can be discarded.
    #:
    #: ⚠ `SYSTem:ECHO 1` is NOT a substring of `SYSTem:ECHO -1` (the `-`
    #: separates them), so scanning for the second never matches the first.
    _ECHO_SET_FRAMED = b"SYST:ECHO 1"


    #: ⛔ ONE VALUE, EIGHT DISTINCT MARKERS. `-1`, `-01`, `-001`, ... all set
    #: the console silent, and the device echoes whichever spelling it was
    #: sent, VERBATIM -- so the marker carries a per-attempt nonce even
    #: though `SYSTem:ECHO` has no nonce field. That is what closes the r6
    #: HIGH: a retry waits for ITS spelling instead of matching the previous
    #: attempt's identical echo.
    #:
    #: ⚠ BOUNDED ON PURPOSE. Each unconfirmed attempt consumes one spelling
    #: and does not give it back, because its echo may still arrive. When
    #: all eight are outstanding the next call REFUSES rather than wrapping
    #: onto `-1` again, since reusing an outstanding spelling is precisely
    #: the defect. Eight is a budget, not a guarantee; a caller retrying
    #: forever on a mute device is told so rather than silently losing the
    #: protection.
    #:
    #: ⚠ No member may be a substring of another (with its terminator), or
    #: a scan for one would match inside another;
    #: `test_the_spelling_family_members_cannot_match_inside_one_another`
    #: pins that, and
    #: `test_the_download_marker_cannot_be_forged_by_open_or_close`
    #: pins distinctness from open()'s full `SYSTem:ECHO -1`.
    #: ⛔ ONE VALUE, 1024 DISTINCT MARKERS. Every member sets the console
    #: silent, and the device echoes whichever spelling it was sent
    #: VERBATIM -- so the marker carries a per-attempt nonce although
    #: `SYSTem:ECHO` has no nonce field.
    #:
    #: Two independent axes, both bench-verified on this lane's board
    #: (2026-11-16, no flash):
    #:   * CASE. libscpi's `compareStr` is case-insensitive, and microrl
    #:     echoes what it was typed. `sYsT:eChO -01` came back as exactly
    #:     `sYsT:eChO -01`, `SYST:ECHO?` then read -1, and `SYST:ERR?`
    #:     stayed `0,"No error"`. Eight letters in `SYST:ECHO` give 256
    #:     spellings of one command.
    #:   * LEADING ZEROS in the parameter. `strtol(str, &endptr, 10)` under
    #:     `DaqifiIntTokenFullyConsumed` accepts `-1`, `-01`, `-001`, ...
    #:     all as -1; the 139-byte form was verified earlier.
    #:
    #: 256 x 4 = 1024, at 12-15 bytes each -- far inside the firmware's
    #: MEASURED 243 B single-command ceiling (the 512 B
    #: SCPI_INPUT_BUFFER_LENGTH is not the binding limit; a longer command
    #: is rejected as INPUT_BUFFER_OVERRUN before the callback runs).
    #:
    #: ⚠ NO MEMBER MAY MATCH INSIDE ANOTHER, or a scan would sync on the
    #: wrong attempt. Checked EXHAUSTIVELY over all 1024x1024 pairs with
    #: terminator and prompt by
    #: `test_the_spelling_family_members_cannot_match_inside_one_another`,
    #: not argued from the construction.
    _ECHO_SET_SILENT_FAMILY = _echo_spelling_family()
    _ECHO_FAMILY_SET = frozenset(_ECHO_SET_SILENT_FAMILY)

    #: How much the exhaustion-path drain may read before giving up, so a
    #: continuously streaming console cannot spin it.
    _ECHO_READ_CAP = 4096

    #: ⛔ NOT `random`. A random draw does NOT establish ownership of a
    #: spelling -- two objects can still pick the same one, which is
    #: exactly the k/1024 residual under ruling IG A. What it does is make
    #: that agreement 1-in-N instead of CERTAIN, which a deterministic rule
    #: (lowest free index, r7) made it. `random`'s default instance is seeded
    #: from entropy but is still a user-visible global that application code
    #: can reseed (`random.seed(0)` in a test harness would make every
    #: object pick alike again, which is exactly the r7 defect). A
    #: SystemRandom reads the OS entropy source on every call and cannot be
    #: seeded.
    _echo_rng = random.SystemRandom()

    #: Matches exactly the family: `SYST:ECHO` in any case, a space, `-`,
    #: zero to three zeros, `1`. Used to spot OUR OWN marker text inside
    #: bytes this object is about to write.
    _ECHO_FAMILY_RE = re.compile(rb"SYST:ECHO -0{0,3}1", re.IGNORECASE)

    def _reserve_spelling(self, spelling) -> None:
        """One more copy of ``spelling`` is owed an echo."""
        self._echo_spellings_outstanding[spelling] = (
            self._echo_spellings_outstanding.get(spelling, 0) + 1)

    def _observe_spelling(self, spelling, times: int = 1) -> None:
        """``times`` complete echoes of ``spelling`` have been observed.

        ⚠ DECREMENT BY AT MOST WHAT IS OWED. `times` comes from COUNTING
        TEXT, and this family admits no nonce -- that is the whole #260
        lesson -- so copies another object wrote, or copies that are file
        payload (pc#268), are INDISTINGUISHABLE from ours. An over-count is
        therefore possible, and it has only two possible treatments:

        * clamped, as here: the key is deleted and the spelling becomes
          drawable, so a later draw may synchronise on a copy still in
          flight. That is the no-nonce residual already documented under
          ruling IG A -- bounded, understood, and it re-rolls on refusal.
        * left to go NEGATIVE: the count becomes a LIE about how many
          echoes are owed, and it then misleads in BOTH directions. While
          negative it withholds a spelling although nothing is owed, and a
          foreign echo per spelling would leave no draw able to produce the
          success that clears the pool -- the r14 item 2 defect, refusing
          forever on a healthy device. And because a later write of the
          same spelling INCREMENTS, the count climbs back through zero
          while a FRESH copy is genuinely owed, releasing it early: the
          exact hazard this method exists to prevent.

        So clamping is not the lesser of two releases -- the negative
        variant releases early too, and loses the pool as well. Neither
        branch is a fix for attribution, because counting text cannot
        attribute; only a nonce could, and `SYSTem:ECHO?` has none.
        """
        have = self._echo_spellings_outstanding.get(spelling)
        if have is None:
            return
        if have > times:
            self._echo_spellings_outstanding[spelling] = have - times
        else:
            del self._echo_spellings_outstanding[spelling]

    def _write_to_port(self, data) -> None:
        """Write to the port, RESERVING any of our marker spellings that
        appear in the bytes.

        ⛔⛔ r13 AUDIT, HIGH: A CALLER'S OWN COMMAND CAN CARRY OUR MARKER.
        `send_command('SYST:ECHO -1')` returns before its echo arrives, the
        download then draws `-1` and synchronises on the CALLER's echo:
        measured b'SYST:ECHO -1\r\nabc'. The echo is real device output we
        never observed, so no amount of reading discipline sees it.

        ⭐ But we do not need to see it. We know we WROTE it, and that is
        enough: FIFO puts that echo AHEAD of our next marker, so marking the
        spelling stale excludes it from the draw and makes the collision
        impossible. The next success clears it, as with any stale spelling.
        
        ⚠ RESERVE BEFORE THE WRITE, and do not release if the write raises:
        pyserial discards the written byte count on a write timeout, so
        0..len bytes may have landed and the echo may still come.

        ⭐⭐ WHY THIS CLOSES WHERE THE READ FUNNEL COULD NOT. r9-r12 tried to
        attribute every byte READ from a shared stream, and that rule could
        not be closed because readers live in other modules and the device
        can destroy bytes before anyone reads them. WRITES are all ours:
        they are few, they are enumerable, and nothing else puts them on the
        wire. A rule over our own writes is checkable; a rule over everyone
        else's reads was not.
        """
        blob = bytes(data)
        # ⛔ SCAN ACROSS THE WRITE BOUNDARY. The regex runs per write, so a
        # caller writing "SYST:EC" then "HO -1\r\n" puts a contiguous line
        # on the device -- echoed as a family member -- while neither write
        # contains one. Carrying the last (longest member - 1) bytes makes
        # the scan see the join. The carry is bytes WE wrote, so it costs
        # nothing to keep and nothing to trust.
        probe = self._echo_write_carry + blob
        base = len(self._echo_write_carry)
        for m in self._ECHO_FAMILY_RE.finditer(probe):
            member = m.group(0)
            if member in self._ECHO_FAMILY_SET and m.end() > base:
                # `m.end() > base` so a member wholly inside the carry is
                # not counted twice: it was already reserved last time.
                self._reserve_spelling(member)
        keep = max(len(x) for x in self._ECHO_SET_SILENT_FAMILY) - 1
        self._echo_write_carry = probe[-keep:] if keep else b""
        self._serial.write(blob)

    def _next_echo_spelling(self):
        """A spelling drawn UNIFORMLY AT RANDOM from those not marked stale,
        or None if every one is.

        ⛔ NOT the lowest free index (r7 audit): a fresh object has an empty
        stale set, so a deterministic rule makes every object in the fleet
        choose alike and the cross-object collision stops being 1-in-N.
        """
        free = [sp for sp in self._ECHO_SET_SILENT_FAMILY
                if sp not in self._echo_spellings_outstanding]
        if not free:
            return None
        return self._echo_rng.choice(free)

    def _reclaim_stale_by_text(self, blob: bytes) -> None:
        """Un-stale every spelling whose echo appears in ``blob``.

        ⚠ USED ONLY ON THE EXHAUSTION PATH. Releasing by text everywhere was
        the r9-r12 machinery -- a carried tail, a filter, a funnel every
        reader had to go through -- and every round since r8 broke some
        corner of it: duplicates, partial copies, a 4 KiB window, readers in
        other modules that never funnelled. A rule that depends on every
        reader of a shared stream forever cannot be closed.

        It survives here because exhaustion is the one place with nothing to
        lose: the pool is empty, the alternative is refusing, and whatever
        has already arrived is free information.
        """
        for member in self._ECHO_SET_SILENT_FAMILY:
            seen_copies = blob.count(member + b"\r\n")
            if seen_copies:
                # One decrement per COPY observed, so a spelling owed twice
                # stays undrawable until both echoes have been seen.
                self._observe_spelling(member, seen_copies)

    def _reclaim_arrived_echoes(self) -> None:
        """Read what has ALREADY arrived, attribute it, and never wait.

        Bounded so a continuously streaming console cannot spin it, and
        non-blocking because the caller's deadline has not started yet: a
        device that is genuinely mute still refuses.
        """
        if self._serial is None or not getattr(self._serial, "is_open", False):
            return
        seen = bytearray()
        while len(seen) < self._ECHO_READ_CAP:
            try:
                waiting = self._serial.in_waiting
            except (serial.SerialException, OSError):
                break
            if not waiting:
                break
            chunk = self._serial.read(min(waiting,
                                          self._ECHO_READ_CAP - len(seen)))
            if not chunk:
                break
            seen.extend(chunk)
        blob = bytes(seen)
        # ⛔ r14 AUDIT, MEDIUM: HONOUR THE PENDING PARTIAL HERE TOO. After N
        # timeouts with the last marker cut at its CR, the LF arrives and
        # the device recovers -- but this reader only looked for COMPLETE
        # markers, so it consumed the LF, freed nothing, and the connection
        # refused forever on a healthy device. The scan honours the partial;
        # so must the one reader that runs when the pool is empty.
        if self._echo_pending_partial is not None and blob:
            prev, suffix = self._echo_pending_partial
            if blob.startswith(suffix):
                self._observe_spelling(prev)
                blob = blob[len(suffix):]
                self._echo_pending_partial = None
            elif not suffix.startswith(blob):
                # FIFO would have put the continuation first, so it is
                # never coming; stop expecting it.
                self._echo_pending_partial = None
        self._reclaim_stale_by_text(blob)

    def silence_echo_for_download(self) -> bytes:
        """Set the console silent before a download. Returns the framing
        bytes that may still appear at the head of the next response.

        ⛔ WHY A SET AND NOT A PROBE (#260, audit r5). A probe asked
        `SYSTem:ECHO?` and tried to decide from the reply's CONTENT whether
        that reply was its own. It cannot be done: the query takes no nonce,
        so every probe's reply is the same four bytes.

        ⛔ WHY A PER-ATTEMPT SPELLING (audit r6). One spelling made every
        attempt's marker identical, so a retry synchronised on the previous
        attempt's echo and sent the GET with its own still in flight. Each
        attempt now draws a spelling no stale echo can match.

        ⭐⭐ WHY SUCCESS CLEARS EVERYTHING, which is what let r13 delete the
        release-by-text machinery. THE DEVICE IS FIFO, verified rather than
        assumed:

        * `UsbCdc.c` feeds microrl one character at a time, microrl echoes
          each character as it is inserted, and `SCPI_Input` runs
          SYNCHRONOUSLY from `microrl_commandComplete` on the newline -- so
          a line's echo and its reply are appended before the next line's
          echo.
        * echo and SCPI replies share ONE circular buffer per transport
          (`UsbCdc_WriteToBuffer` -> `CircularBuf_AddBytes`, under the
          client's write mutex), appended in call order. There is no second
          path that could overtake it.
        * measured on this lane's board: three lines in one write came back
          as echo(1), prompt, echo(2), REPLY(2), prompt, echo(3) -- exactly
          input order.

        So when THIS attempt's marker is in hand, every earlier OFF line's
        echo is already behind it in the stream or is never coming (its line
        was never echoed, or a partial write concatenated into a garbage
        line that was echoed before ours). Nothing earlier can still be
        pending, so the whole stale set can be dropped.

        ⚠ TWO CAVEATS FROM THE SOURCE CHECK, both safe for this argument.
        `UsbCdc_WriteToBuffer` is non-blocking and all-or-nothing, so output
        can be DROPPED when the buffer is full -- a loss, not a reorder, and
        "never coming" already covers it. And because the echo is written
        per character, another writer on the same buffer (streaming, logs,
        the wifi bridge) can interleave BETWEEN characters, so a marker can
        arrive split by foreign bytes. That is also not a reorder: the scan
        simply fails to match and the attempt refuses, which is the loud
        direction.

        ⚠ WITHIN ONE OBJECT THIS IS DETERMINISTIC -- but only while no
        EARLIER RESPONSE is still in flight. A previous GET's payload is
        file data, which can contain any spelling, so a download that timed
        out with bytes still arriving can feed the next scan a marker out of
        DATA. That is pre-existing and broader before this change, and is
        tracked in pc#268.

        ⚠ WHAT REMAINS, STATED EXACTLY:

        * CROSS-OBJECT (ruling IG A, k/1024 per attempt): a new object
          cannot know another object's stale spellings, so it may draw one
          whose echo is in flight. A refusal consumes that spelling, so a
          retry re-rolls.
        * BYTES CARRYING OUR MARKER THAT THIS OBJECT NEVER OBSERVED AND
          NEVER WROTE (pc#268): an earlier GET's payload, which is file
          data and can contain any spelling -- or all of them, so N buys
          nothing there. Measured PRE-EXISTING and broader before this
          change: on main the stale bytes come back for ORDINARY file
          content.
        * WRITES FROM OUTSIDE THIS MODULE, which the write funnel cannot
          see: `device.py` writes the GET itself (two sites), and
          `devices.py` / `sdcard.py` write to a transport stream. None of
          them carries setter text today, so none reserves anything; if one
          ever did, its echo would be unobserved in the same way.

        Everything a caller writes THROUGH this module is covered, because
        the write funnel reserves our marker text before it reaches the
        wire.

        A port fault propagates unwrapped, per pc#267's contract.
        """
        from ..exceptions import EchoSetUnconfirmedError

        spelling = self._next_echo_spelling()
        if spelling is None:
            # Nothing left to draw: read what has already landed, since an
            # arrived echo frees its spelling and refusing without looking
            # made an exhausted pool permanent (r9 audit).
            self._reclaim_arrived_echoes()
            spelling = self._next_echo_spelling()
        if spelling is None:
            raise EchoSetUnconfirmedError(
                "all %d echo-setter spellings are stale on this connection: "
                "each was written and its echo has not been seen, so any of "
                "them could still be matched by an echo in flight. No "
                "download attempted. A spelling is cleared when a later "
                "attempt succeeds, or when its echo is read. Retrying on "
                "this connection is what can clear them; opening a new "
                "connection cannot, because the bytes are queued on the "
                "device." % len(self._ECHO_SET_SILENT_FAMILY)
            )

        framing = spelling + b"\r\n"
        if self._serial is None or not getattr(self._serial, "is_open", False):
            return framing

        # ⛔ THE DEADLINE IS CREATED BEFORE THE DRAIN (r4 finding 2):
        # `write_timeout` governs neither backend's flush, so a drain placed
        # ahead of the clock can spend the whole budget before it starts.
        deadline = time.monotonic() + self.ECHO_SET_TIMEOUT_S

        # Echo ON, then OFF in the drawn spelling. microrl gates the echo on
        # the state as the characters arrive, so the second line's echo is
        # certain from any starting state.
        self._write_to_port(self._ECHO_SET_FRAMED + b"\r\n")
        # ⚠ THE RESERVATION NOW FALLS OUT OF THE WRITE FUNNEL, which marks
        # any family spelling in the bytes stale BEFORE writing them. The
        # r11 semantics are preserved exactly: the ON line carries no family
        # member so a failed ON write reserves nothing, and the OFF line
        # reserves before it is attempted so a failed OFF write keeps it.
        self._write_to_port(framing)
        drain_output_bounded(
            self._serial, max(0.0, deadline - time.monotonic()))

        # Read until THIS attempt's marker is in hand. A do-while: read
        # first, check the clock after, so a reply already waiting is never
        # missed by a timer that expired first.
        seen = bytearray()
        pending = self._echo_pending_partial
        while True:
            try:
                waiting = self._serial.in_waiting
            except (serial.SerialException, OSError):
                # ⚠ NARROW. A bare `except Exception` here would swallow
                # cancellation and programming errors alike; a dead port is
                # a port fault and belongs to the caller.
                raise
            if waiting:
                seen.extend(self._serial.read(waiting))
            if pending is not None and seen:
                prev, suffix = pending
                if bytes(seen).startswith(suffix):
                    # The cut marker completed exactly where FIFO says it
                    # must: release that spelling and drop its remainder.
                    self._observe_spelling(prev)
                    del seen[:len(suffix)]
                    pending = self._echo_pending_partial = None
                elif not suffix.startswith(bytes(seen)):
                    # The stream continued with something else, so the
                    # continuation is never coming -- FIFO would have put it
                    # first. Stop expecting it; the spelling stays stale
                    # until a success clears it.
                    pending = self._echo_pending_partial = None
            if framing in seen:
                # ⭐ FIFO: our marker is in hand, so nothing written earlier
                # is still pending. Everything ahead of it in the stream was
                # pre-GET console traffic and is dropped with `seen`.
                self._echo_spellings_outstanding.clear()
                return b""
            if time.monotonic() >= deadline:
                # If our own marker was cut by this deadline, remember just
                # the expected remainder so the NEXT attempt can finish it
                # and release this spelling.
                tail = bytes(seen)
                cut = 0
                for k in range(1, len(framing)):
                    if tail.endswith(framing[:k]):
                        cut = k
                self._echo_pending_partial = (
                    (spelling, framing[cut:]) if cut else None)
                # ⛔ REFUSE, DO NOT PROCEED. The marker is GUARANTEED to be
                # coming, so "not here yet" means "still in flight" and
                # never "nothing was coming" -- the ambiguity a query could
                # not resolve. Proceeding puts our own echo inside the file.
                raise EchoSetUnconfirmedError(
                    "the echo setter's own echo (%r) did not arrive within "
                    "%.2fs, so it is still in flight and would land inside "
                    "the downloaded file; no download attempted. The next "
                    "attempt on this connection draws a different spelling "
                    "and drops this one as pre-marker bytes; a successful "
                    "attempt clears every stale spelling. Opening a new "
                    "connection does not help: the bytes are queued on the "
                    "device, which a new object cannot discard."
                    % (bytes(spelling), self.ECHO_SET_TIMEOUT_S)
                )
            time.sleep(0.005)

    def _drain_bounded(self, deadline_s: float) -> None:
        """Wait, BOUNDED, for the OS output queue to empty. No ``flush()``.

        Raises ``serial.SerialTimeoutException`` if it does not empty in
        time. close() records that and still releases the handle (#259);
        open() releases the handle and fails the open (#262).

        ⛔ THE BOUND GOES WHERE THE UNBOUNDED WAIT IS. ``write_timeout`` does
        not govern ``flush()`` on either backend — pyserial 3.5 polls
        ``out_waiting`` with no deadline on Windows and calls ``tcdrain()``
        on POSIX — so lowering it could never have bounded this.

        ⛔⛔ AND NOTHING UNBOUNDED MAY SIT BETWEEN THE DRAIN AND THE RELEASE,
        which is where an earlier version of this function went wrong. It
        polled ``out_waiting`` to zero and THEN called ``flush()``, with a
        comment claiming that zero pending bytes made the flush safe. **That
        claim is false.** ``out_waiting`` is the OS queue count;
        ``tcdrain()`` waits for the HARDWARE TRANSMITTER, and Linux's
        usb-serial (``generic.c``) keeps those two separate. On an FTDI
        adapter with RTS/CTS where CTS is deasserted, the bytes sit in the
        adapter's FIFO, ``out_waiting`` reads 0, and ``tcdrain()`` blocks
        forever — so ``close_drain_timeout`` never fired, the handle was
        never released and ``last_close_error`` stayed None. Exactly the
        defect this function exists to remove, one layer down (#259 r1).

        So the poll IS the drain, and there is no flush. ⚠ What that
        honestly costs: when this returns, the OS queue is empty, but bytes
        may still be in the adapter's FIFO or the UART shift register. There
        is no bounded way to wait for those — ``tcdrain()`` takes no timeout
        — and the alternative is a call that can never return. For close()'s
        purpose (a courtesy ``SYSTem:ECHO 1`` on the way out) an unbounded
        wait is the worse trade. For open()'s ``SYSTem:ECHO -1`` it is too:
        bytes still in the adapter go out ahead of the next command anyway,
        and open()'s own settle-and-drain reads whatever echo they produce.

        A port that exposes no ``out_waiting`` cannot be bounded at all, and
        that is reported by raising rather than by falling through to an
        unbounded ``flush()``: silently taking the unbounded wait again
        would be the fail-open.

        The poll itself is drain_output_bounded() (#263), shared with every
        other send path.
        """
        drain_output_bounded(self._serial, deadline_s)

    def close(self) -> bool:
        """Close the serial connection. True if it closed cleanly.

        Restores echo to terminal-friendly defaults (SYSTem:ECHO 1) before
        closing so a human opening the port in a serial terminal afterward
        sees the prompt.

        ⛔ #259: THE DRAIN IS BOUNDED, AND THIS STILL DOES NOT RAISE. The
        flush used to be unbounded, so a device that stopped draining hung
        disconnect() indefinitely. It is bounded now — but making close()
        raise would trade a hang for a different defect: a dying device
        turning a FINISHED session into a teardown traceback, after the work
        is done and before the caller can report its result. That is the
        shape this change exists to remove, not to relocate.

        So the outcome is: **the handle is ALWAYS released, and the failure
        is always observable.** A drain that expires sets
        ``last_close_error`` and makes this return False; the port is closed
        either way. Silence is the one thing it does not do — returning
        normally after a failed drain would be a verified-false reported as
        success.

        ⚠ WHAT "DRAINED" MEANS HERE, stated rather than implied: the OS
        output queue emptied. It does NOT mean the bytes left the hardware.
        There is no ``flush()`` on this path at all — see _drain_bounded for
        why ``tcdrain()`` cannot be bounded and what an earlier version got
        wrong by calling it once ``out_waiting`` hit zero. Bytes may remain
        in a USB adapter's FIFO when this returns True.
        """
        self.last_close_error = None
        if not (self._serial and self._serial.is_open):
            return True
        clean = True
        try:
            # Refused by CancellablePort once the caller has given up.
            self._write_to_port(b"SYSTem:ECHO 1\r\n")
            self._drain_bounded(self.close_drain_timeout)
            time.sleep(0.05)
        except (serial.SerialException, OSError) as exc:
            # Best-effort on the wire; the underlying port may already be
            # gone. But RECORDED, not swallowed.
            self.last_close_error = exc
            clean = False
        self._serial.close()
        return clean

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
            # Through the write funnel: a caller's own command may carry our
            # marker text, and we must reserve it before it reaches the wire.
            self._write_to_port(cmd_bytes)
            # #263: bounded, no flush(). A stall raises SerialTimeoutException,
            # which the handler below reports as a RuntimeError.
            self._drain_bounded(self.send_drain_timeout)

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

    def query_binary(
        self,
        query: str,
        timeout: float = 2.0,
        idle_settle: float = 0.1,
    ) -> bytes:
        """Send a SCPI query and return the raw response bytes.

        Use this for protobuf-typed queries (e.g. ``SYSTem:SYSInfoPB?``)
        where the device replies with binary that must NOT be ASCII-
        decoded or whitespace-stripped. Reads until either ``timeout``
        seconds elapse OR the device stops sending bytes for
        ``idle_settle`` seconds (whichever comes first).

        Resets the input buffer before writing so stale bytes from a
        prior text query can't bleed into the binary response.

        Args:
            query: SCPI query string (without terminator)
            timeout: Total deadline in seconds.
            idle_settle: Stop early after this many seconds of no
                inbound bytes once at least one byte has arrived.

        Returns:
            Raw response bytes (may be empty if the device didn't
            reply within ``timeout``).

        Raises:
            RuntimeError: If serial port not open or I/O fails.
        """
        if not self.is_open():
            raise RuntimeError("Serial port not open")
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0 (got {timeout!r})")
        if idle_settle < 0:
            raise ValueError(f"idle_settle must be >= 0 (got {idle_settle!r})")

        # Cache the serial handle in a local so a concurrent close()
        # mid-call can't trip an AttributeError on ``self._serial`` —
        # we either hold a valid reference for the whole call or we
        # already failed the is_open() guard above.
        ser = self._serial
        if ser is None:
            # Defensive — close() could have nulled _serial between
            # the is_open() check above and this read. Surface the
            # same error the guard would have.
            raise RuntimeError("Serial port not open")

        # Force non-blocking reads for the duration so a single
        # ``read(pending)`` cannot exceed our wall-clock deadline by
        # falling back to the port's pre-configured ``timeout``.
        # Restored in ``finally`` so callers see no side-effect on
        # subsequent ``send_command`` reads. ``None`` (blocking) is
        # a legitimate prior value, so use a separate ``timeout_changed``
        # flag rather than ``prev is not None`` to decide whether to
        # restore — otherwise a port left in blocking mode by the
        # caller would silently flip to non-blocking.
        prev_serial_timeout = getattr(ser, "timeout", None)
        timeout_changed = False
        try:
            try:
                ser.timeout = 0
                timeout_changed = True
            except (serial.SerialException, OSError, ValueError, AttributeError):
                # Some pyserial backends don't support runtime
                # mutation; fall back to relying on `in_waiting`
                # being accurate (the loop already gates on it).
                pass
            try:
                ser.reset_input_buffer()
                # Use \r\n to match ScpiMessage.get_bytes() — the canonical
                # wire format. send_command() uses bare \r for legacy
                # compatibility but binary queries expect the canonical form.
                self._write_to_port((query + "\r\n").encode("ascii"))
                # #263: bounded, no flush(); a stall is reported as the
                # RuntimeError below.
                drain_output_bounded(ser, self.send_drain_timeout)

                received = bytearray()
                deadline = time.monotonic() + timeout
                last_byte_time: Optional[float] = None
                while time.monotonic() < deadline:
                    pending = ser.in_waiting
                    if pending:
                        chunk = ser.read(pending)
                        if chunk:
                            received.extend(chunk)
                            last_byte_time = time.monotonic()
                            # Got bytes — try to drain more before checking
                            # idle-settle so a multi-chunk reply doesn't
                            # break out of the loop on the first quiet poll.
                            continue
                    # No bytes this iteration (either ``pending == 0`` or
                    # ``read()`` returned empty even though in_waiting was
                    # non-zero — a corner case some pyserial backends hit
                    # under USB CDC contention). Check the idle-settle
                    # early-exit condition uniformly here.
                    #
                    # ``idle_settle == 0`` disables the early-exit entirely;
                    # the loop then waits for the full timeout (useful when
                    # the caller wants to keep accumulating bytes that may
                    # come in bursts longer than 100ms).
                    if (
                        received
                        and idle_settle > 0
                        and last_byte_time is not None
                        and (time.monotonic() - last_byte_time) >= idle_settle
                    ):
                        break
                    time.sleep(0.01)
                return bytes(received)
            except (serial.SerialException, OSError) as e:
                raise RuntimeError(f"Serial communication error: {e}")
        finally:
            if timeout_changed:
                try:
                    ser.timeout = prev_serial_timeout
                except (serial.SerialException, OSError, ValueError, AttributeError):
                    pass

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

        return self.send_command_with_error(cmd, delay)

    def send_command_with_error(
        self, command: str, delay: float = 0.1
    ) -> tuple[bool, str]:
        """Send a fully-formed SCPI command and return (success, raw_error).

        Same atomic error-capture semantics as :meth:`send_set_with_error`
        but accepts a complete command string — lets callers pass the
        output of a ``scpi_commands`` factory directly without splitting
        the base command from the value.

        ``success`` is True only when the SYST:ERR? response parses to
        SCPI code 0. The earlier loose substring check (``"No error" in
        error``) misclassified responses like ``120,"No error in stream"``
        as success because the substring matched the descriptive text
        even though the numeric code was non-zero. Surfaced by Qodo
        /agentic_review pass 1 on PR #105.
        """
        self.send_command(command, delay)
        error = self.send_query("SYSTem:ERRor?", 0.1)
        # Parse the leading numeric code; only code==0 is success. A
        # missing / unparseable code is NOT success — the caller should
        # see the raw response in error_response and decide.
        success = False
        raw = (error or "").strip()
        if raw:
            code_str, _, _ = raw.partition(",")
            try:
                success = int(code_str.strip()) == 0
            except ValueError:
                success = False
        return (success, error)

    def drain_errors(
        self, max_iterations: int = 256, empty_budget: int = 3
    ) -> list[str]:
        """Pop ``SYST:ERR?`` until the queue reports ``"No error"``,
        ``empty_budget`` consecutive empty/unparseable replies arrive,
        or ``max_iterations`` is hit. Returns the popped errors.

        Closes Gap 2 of daqifi/daqifi-python-core#22: the prior
        ``clear_errors()`` discarded errors silently and capped at 10.
        On a device exercised by another session (or one that's
        crashed mid-stream) >10 errors can be queued — observed in
        the wild, ~12 errors had to drain before
        ``SYSTem:STReam:START`` would succeed.

        ``empty_budget`` (default 3) is the **fail-fast** escape
        hatch surfaced by Qodo PR #62 review #1: if the device
        intermittently returns empty replies (USB CDC timeout, no
        bytes within the read window), the prior unconditional
        ``max_iterations`` cap could stall a caller for ~25s
        (256 × 0.1s send_query delay) waiting for a response that
        never comes. Bailing after N consecutive unproductive reads
        bounds the worst case at ~0.3s while still draining all
        errors when the device IS responsive.

        Indeterminate-state signal — if we exit via empty_budget
        OR max_iterations WITHOUT seeing ``0,"No error"``, append
        a synthetic ``DRAIN_INCONCLUSIVE`` sentinel so callers
        can detect "queue not confirmed clean" instead of treating
        empty `popped` as success. Mirrors the conftest.py pattern
        used by the integration error_drainer (Qodo PR #64 review
        finding 1).

        Reply parsing is strict — uses :func:`scpi.parse_system_error`
        to require the canonical ``<int>,"<msg>"`` shape, and the
        no-error check matches ONLY ``code == 0 AND
        description == "No error"`` (case/whitespace tolerant).
        Loose substring checks against ``"No error"`` would falsely
        terminate on payloads like ``120,"No error in stream"``
        (Qodo PR #64 review finding 3).
        """
        from .scpi import parse_system_error

        popped: list[str] = []
        consecutive_empty = 0
        saw_no_error = False
        for _ in range(max_iterations):
            error = self.send_query("SYSTem:ERRor?", 0.05)
            stripped = error.strip() if error else ""
            if not stripped:
                consecutive_empty += 1
                if consecutive_empty >= empty_budget:
                    break
                continue
            # Strict shape check: parse_system_error returns
            # (0, "No error") for unrecognized/malformed input, so
            # we can't trust its return alone — re-validate the raw
            # shape (numeric code + comma + payload) before parsing.
            code_str, sep, _ = stripped.partition(",")
            if not sep:
                # No comma — definitely not a SCPI error reply.
                consecutive_empty += 1
                if consecutive_empty >= empty_budget:
                    break
                continue
            try:
                int(code_str.strip())
            except ValueError:
                # Code field isn't an int — likely echo collision.
                consecutive_empty += 1
                if consecutive_empty >= empty_budget:
                    break
                continue
            code, desc = parse_system_error(stripped)
            if code == 0 and desc.strip().lower() == "no error":
                saw_no_error = True
                break
            consecutive_empty = 0
            popped.append(stripped)
        if not saw_no_error:
            # Format the sentinel as a parseable SCPI error so callers
            # who pipe popped entries through scpi.parse_system_error
            # see a non-zero code. `99999` is clearly outside SCPI's
            # standard error range (-100 to -899 negative, 1-32767
            # positive device-specific) so it can't collide with any
            # real device error code — callers branching on numeric
            # code can safely treat it as the synthetic sentinel.
            # (Surfaced via Qodo /improve pass 6 on PR #101 (silent-
            # miss when the sentinel was unparseable as int); the
            # follow-up Qodo /agentic_review pass 7 caught a -100
            # collision with the standard "Command error" code, hence
            # the move to 99999. Same fix applied to the parallel
            # DaqifiDevice.drain_errors there.)
            popped.append(
                '99999,"DRAIN_INCONCLUSIVE: did not observe 0,\\"No '
                'error\\" before giving up"'
            )
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
