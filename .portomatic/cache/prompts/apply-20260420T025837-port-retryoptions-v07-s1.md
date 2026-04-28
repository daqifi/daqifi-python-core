# portomatic apply prompt

**Plan:** `20260420T025837-port-retryoptions-v07`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: Port ConnectionRetryOptions: 6 props with TimeSpan defaults, 3 static factories (NoRetry/Fast/Resilient), CalculateDelay(int)->TimeSpan with exponential backoff. Tests v0.7 TimeSpan->float mapping.
- classification: new-feature
- shard: Communication
- upstream_ref: e8d84af:src/Daqifi.Core/Communication/Transport/ConnectionRetryOptions.cs
- downstream_target: daqifi/_internal/comm.py

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `event EventHandler<T>` → Single event: `Callable[[T], None]` callback slot OR `add_<event>`/`remove_<event>` if subscribers > 1. Multiple events on same interface: prefix add/remove with the event name (e.g. `add_message_received`, `remove_message_received`) and keep callback lists per-event. Mirror this pattern across all events on the same Protocol/class for consistency.
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Stream` → `typing.IO[bytes]` (or `BinaryIO`) — file-like objects with read/write
- `TimeSpan` → `float` (seconds) or `datetime.timedelta` if duration arithmetic is needed
- `DateTime / DateTimeOffset` → `datetime.datetime` (use `datetime.now(timezone.utc)` for UTC equivalent of `DateTime.UtcNow`)
- `Exception (base type or unspecified subtype)` → `Exception` (Python's base exception class) — only raise plain `Exception` when the C# code did the same; prefer a specific subtype
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `InvalidOperationException` → `RuntimeError` (or a domain-specific exception when the project defines one)
- `NotImplementedException / NotSupportedException` → `NotImplementedError`
- `EventArgs subclass` → `@dataclass` carrying the event payload (frozen if the event payload is read-only)
- `Guid` → `uuid.UUID`
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Communication.Transport;

/// <summary>
/// Configuration options for connection retry behavior with exponential backoff.
/// </summary>
public class ConnectionRetryOptions
{
    /// <summary>
    /// Gets or sets the maximum number of retry attempts. Default is 3.
    /// </summary>
    public int MaxAttempts { get; set; } = 3;

    /// <summary>
    /// Gets or sets the initial delay before the first retry attempt. Default is 1 second.
    /// </summary>
    public TimeSpan InitialDelay { get; set; } = TimeSpan.FromSeconds(1);

    /// <summary>
    /// Gets or sets the maximum delay between retry attempts. Default is 30 seconds.
    /// </summary>
    public TimeSpan MaxDelay { get; set; } = TimeSpan.FromSeconds(30);

    /// <summary>
    /// Gets or sets the backoff multiplier for exponential backoff. Default is 2.0.
    /// </summary>
    public double BackoffMultiplier { get; set; } = 2.0;

    /// <summary>
    /// Gets or sets the connection timeout for each attempt. Default is 5 seconds.
    /// </summary>
    public TimeSpan ConnectionTimeout { get; set; } = TimeSpan.FromSeconds(5);

    /// <summary>
    /// Gets or sets whether retry is enabled. Default is true.
    /// </summary>
    public bool Enabled { get; set; } = true;

    /// <summary>
    /// Creates a default configuration with retry disabled.
    /// </summary>
    public static ConnectionRetryOptions NoRetry => new() { Enabled = false, MaxAttempts = 1 };

    /// <summary>
    /// Creates a configuration optimized for fast reconnection (short delays, fewer attempts).
    /// </summary>
    public static ConnectionRetryOptions Fast => new()
    {
        MaxAttempts = 3,
        InitialDelay = TimeSpan.FromMilliseconds(500),
        MaxDelay = TimeSpan.FromSeconds(5),
        BackoffMultiplier = 1.5,
        ConnectionTimeout = TimeSpan.FromSeconds(3)
    };

    /// <summary>
    /// Creates a configuration optimized for slow/unreliable connections (longer delays, more attempts).
    /// </summary>
    public static ConnectionRetryOptions Resilient => new()
    {
        MaxAttempts = 5,
        InitialDelay = TimeSpan.FromSeconds(2),
        MaxDelay = TimeSpan.FromSeconds(60),
        BackoffMultiplier = 2.5,
        ConnectionTimeout = TimeSpan.FromSeconds(10)
    };

    /// <summary>
    /// Calculates the delay for a specific retry attempt using exponential backoff.
    /// </summary>
    /// <param name="attemptNumber">The attempt number (1-based).</param>
    /// <returns>The calculated delay for this attempt.</returns>
    public TimeSpan CalculateDelay(int attemptNumber)
    {
        if (attemptNumber <= 1)
            return TimeSpan.Zero;

        var delay = InitialDelay.TotalMilliseconds * Math.Pow(BackoffMultiplier, attemptNumber - 2);
        return TimeSpan.FromMilliseconds(Math.Min(delay, MaxDelay.TotalMilliseconds));
    }
}

```

## Target rationale

Target file already references `ConnectionRetryOptions` — extending in place. Look for the existing reference and append your change near it.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `Internal serial communication layer for DAQiFi Nyquist devices.`
      `This module provides low-level SCPI command interface over serial port.`
      `Not intended for direct use - use the public API instead.`
      `Ports the C# ``TransportStatusEventArgs`` (subclass of ``EventArgs``).`
      `Python uses plain callables instead of the .NET event pattern, so the`
-       line 418: `# Wait for device to stabilize`
            `time.sleep(0.1)`
-       line 420: `# Clear any pending data`
            `self._serial.reset_input_buffer()`
            `self._serial.reset_output_buffer()`
            `return True`
            `except (serial.SerialException, OSError) as e:`
            `return False`
-       line 455: `# Send command with CR terminator`
            `cmd_bytes = (command + "\r").encode('ascii')`
            `self._serial.write(cmd_bytes)`
            `self._serial.flush()`
-       line 460: `# Wait for device to process`
            `time.sleep(delay)`
-       line 463: `# Read all available data`
            `response = b""`
            `start_time = time.time()`
            `while (time.time() - start_time) < self.timeout:`
            `if self._serial.in_waiting > 0:`
            `chunk = self._serial.read(self._serial.in_waiting)`
-           line 471: `# Give a bit more time for any trailing data`
                `time.sleep(0.05)`
                `if self._serial.in_waiting == 0:`
                `break`
                `else:`
-           line 477: `# No data yet, wait a bit`
                `time.sleep(0.01)`
-       line 480: `# Decode and clean response`
            `response_str = response.decode('ascii', errors='ignore')`
-       line 483: `# Remove echoed command if present`
            `if command in response_str:`
            `response_str = response_str.replace(command, "")`
            `response_str = response_str.replace("DAQIFI>", "")`
-       line 490: `# Clean up whitespace and control characters`
            `response_str = response_str.strip()`
            `return response_str`
            `except (serial.SerialException, OSError) as e:`
            `raise RuntimeError(f"Serial communication error: {e}")`
            `def send_query(self, query: str, delay: float = 0.1) -> str:`
-     line 510: `# Remove any surrounding quotes from response`
          `response = response.strip('"').strip("'")`
          `return response`
          `def send_set(self, command: str, value, delay: float = 0.1) -> bool:`
-     line 526: `# Format value based on type`
          `if isinstance(value, str):`
          `cmd = f"{command} {value}"`
          `elif isinstance(value, bool):`
          `cmd = f"{command} {1 if value else 0}"`
          `elif isinstance(value, (int, float)):`
-     line 538: `# Check for errors`
          `error = self.send_query("SYST:ERR?", 0.1)`
          `return "No error" in error`
          `def clear_errors(self):`
          `max_errors = 10`
          `for _ in range(max_errors):`

**Top-level declarations** (existing module-level symbols — use as structural overview):

- `class TransportStatusEventArgs`
- `class ConnectionRetryOptions`
- `class StreamTransport`
- `class OutboundMessage`
- `class ScpiMessage`
- `class MessageProducer`
- `class MessageReceivedEventArgs`
- `class MessageConsumerErrorEventArgs`
- `class MessageConsumer`
- `class PowerState`
- `class SerialInterface`
- `T (constant)`

```python
"""
Internal serial communication layer for DAQiFi Nyquist devices.

This module provides low-level SCPI command interface over serial port.
Not intended for direct use - use the public API instead.
"""

import serial
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Callable, Generic, List, Optional, Protocol, TypeVar, runtime_checkable
from enum import IntEnum
import builtins


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
        """Return the ASCII bytes for transmission, with CRLF terminator."""
        return (self.data + "\r\n").encode("ascii")


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

    The ``timestamp`` defaults to ``datetime.utcnow()`` at construction time
    to match the upstream ``DateTime.UtcNow`` semantics.
    """

    message: T
    raw_data: bytes
    timestamp: datetime = field(default_factory=datetime.utcnow)


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
    timestamp: datetime = field(default_factory=datetime.utcnow)


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

        Returns:
            True if connection successful, False otherwise
        """
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            # Wait for device to stabilize
            time.sleep(0.1)
            # Clear any pending data
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            return True
        except (serial.SerialException, OSError) as e:
            # Return False instead of raising to allow discovery to continue
            return False

    def close(self):
        """Close serial connection"""
        if self._serial and self._serial.is_open:
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

        # Check for errors
        error = self.send_query("SYST:ERR?", 0.1)
        return "No error" in error

    def clear_errors(self):
        """Clear error queue by reading all errors"""
        max_errors = 10
        for _ in range(max_errors):
            error = self.send_query("SYST:ERR?", 0.05)
            if "No error" in error:
                break

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

```

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 9 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 4 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 3 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 5 usages (e.g. `daqifi/_internal/comm.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 8 usages (e.g. `daqifi/discovery.py`)

## Map shard `Communication` — relevant entries


### Architectural context (no exact file match)

_Nearest file mappings in this shard — read these BEFORE deciding whether to create a downstream file:_

- `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` → `daqifi/_internal/scpi.py` (divergent)

## Output requirements

- Edit the downstream file (or create it if missing) to implement the upstream change in idiomatic Python.
- Do NOT transliterate — port. Use Python idioms (dataclasses, properties, type hints, exceptions).
- Honour any active divergences listed above (don't 'fix' an intentional divergence).
- **Match the LOCAL section's convention**, not the whole file's most-common style. If the File regions index above shows multiple sections, identify which section your change belongs to and mimic THAT section's naming/formatting — different sections often use different conventions intentionally.
- For Python smoke checks, use `python3` (auto-detected for this project — venv preferred, else python3).
- After editing, run the verifier policy. Bound at N=3 reflection iterations.
- **On failure (verifier still failing after 3 attempts), STOP and report.** Do NOT call `AskUserQuestion`, do NOT file a sub-ticket yourself, do NOT keep iterating past N=3. Just include in your report:
  - The diff you applied (latest attempt)
  - The verifier output (last ~20 lines)
  - Your best guess at the failure class: `compile`, `behavioral_drift`, `missing_dep`, `flaky_test`, or `unknown`
  - The orchestrator marks the ticket `blocked`, surfaces the failure to the user, and decides skip/retry/abort.
- **DO NOT run `portomatic apply close` yourself** — the orchestrator that dispatched you owns the close step. Just report the diff you applied, whether tests passed, and any blockers. The orchestrator runs `apply close` after verifying.
