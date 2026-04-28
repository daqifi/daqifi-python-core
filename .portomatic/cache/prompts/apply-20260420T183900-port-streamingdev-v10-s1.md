# portomatic apply prompt

**Plan:** `20260420T183900-port-streamingdev-v10`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: #34: Port DaqifiStreamingDevice — main streaming device facade
- classification: new-feature
- shard: Device
- upstream_ref: e8d84af:src/Daqifi.Core/Device/DaqifiStreamingDevice.cs
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
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `InvalidOperationException` → `RuntimeError` (or a domain-specific exception when the project defines one)
- `NotImplementedException / NotSupportedException` → `NotImplementedError`
- `EventArgs subclass` → `@dataclass` carrying the event payload (frozen if the event payload is read-only)
- `Guid` → `uuid.UUID`
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.

## .NET helper idioms

Common BCL helpers that recur across nearly every C#→Python port. Use the listed equivalent rather than re-deriving the translation per call site:

| C# | Python equivalent |
|---|---|
| `string.IsNullOrWhiteSpace(s)` | `not s or not s.strip()` |
| `string.IsNullOrEmpty(s)` | `not s` (works for None and empty string) |
| `nameof(x)` | `"x"` (literal string — common in exception messages and validation) |
| `string.Format("{0}", x)` | `f"{x}"` (f-string) — fall back to `.format()` only when the format string is dynamic |
| `string.Join(",", items)` | `",".join(items)` when items are strings; `",".join(str(x) for x in items)` when not — Python `str.join` raises `TypeError` on non-string elements while .NET `string.Join` stringifies them. Note the order flip — separator first in C#, last in Python. |
| `Math.Min / Math.Max / Math.Abs` | `min(...)` / `max(...)` / `abs(...)` (builtins — no import needed) |
| `Math.Pow(a, b)` | `a ** b` (operator) — `math.pow` is float-only, the operator handles ints natively |
| `Guid.NewGuid()` | `uuid.uuid4()` |
| `Convert.ToInt32 / ToDouble / ToBoolean` | For non-null inputs: `int(...)` / `float(...)` / `bool(...)`. Caveats: `bool('false')` is `True` in Python (any non-empty string is truthy — use `s.lower() == 'true'`); and `Convert.ToInt32(null)` returns `0` in C# while `int(None)` raises `TypeError` — use `0 if x is None else int(x)` when the upstream value can be null. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using Daqifi.Core.Communication;
using Daqifi.Core.Communication.Messages;
using Daqifi.Core.Communication.Producers;
using Daqifi.Core.Communication.Transport;
using Daqifi.Core.Device.Network;
using Daqifi.Core.Device.SdCard;
using Daqifi.Core.Firmware;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Threading;
using System.Threading.Tasks;

#nullable enable

namespace Daqifi.Core.Device
{
    /// <summary>
    /// Represents a DAQiFi device that supports data streaming functionality.
    /// Extends the base DaqifiDevice with streaming-specific operations.
    /// </summary>
    public class DaqifiStreamingDevice : DaqifiDevice, IStreamingDevice, INetworkConfigurable, ISdCardOperations, ILanChipInfoProvider
    {
        /// <summary>
        /// The delay in milliseconds to wait for the WiFi module to restart after applying configuration.
        /// </summary>
        private const int WIFI_MODULE_RESTART_DELAY_MS = 2000;

        /// <summary>
        /// The delay in milliseconds to wait after switching between LAN and SD card interfaces.
        /// The SD card and LAN share the SPI bus, so a settle period is needed for the device
        /// firmware to complete the interface switch before sending further commands.
        /// </summary>
        private const int SD_INTERFACE_SETTLE_DELAY_MS = 100;

        /// <summary>
        /// Maximum number of retry attempts for SD card list operations that receive transient
        /// SCPI errors (e.g., -200 Execution error) due to interface-switch timing.
        /// </summary>
        private const int SD_LIST_MAX_RETRIES = 1;

        private bool _isLoggingToSdCard;
        private IReadOnlyList<SdCardFileInfo> _sdCardFiles = Array.Empty<SdCardFileInfo>();

        /// <summary>
        /// Gets a value indicating whether the device is currently streaming data.
        /// </summary>
        public bool IsStreaming { get; private set; }

        /// <summary>
        /// Gets or sets the streaming frequency in Hz (samples per second).
        /// </summary>
        public int StreamingFrequency { get; set; }

        /// <summary>
        /// Gets a value indicating whether the device is currently logging data to the SD card.
        /// </summary>
        public bool IsLoggingToSdCard => _isLoggingToSdCard;

        /// <summary>
        /// Gets a value indicating whether the device is connected over USB (serial transport).
        /// SD card file downloads require a USB connection because the SD card and WiFi/LAN share the SPI bus.
        /// </summary>
        public virtual bool IsUsbConnection => Transport is SerialStreamTransport;

        /// <summary>
        /// Gets the most recently retrieved list of files on the SD card.
        /// </summary>
        public IReadOnlyList<SdCardFileInfo> SdCardFiles => _sdCardFiles;

        private readonly NetworkConfiguration _networkConfiguration = new NetworkConfiguration();

        /// <summary>
        /// Gets a copy of the current network configuration.
        /// </summary>
        /// <remarks>
        /// Returns a clone to prevent external modification. Use <see cref="UpdateNetworkConfigurationAsync"/>
        /// to change the device's network configuration.
        /// </remarks>
        public NetworkConfiguration NetworkConfiguration => _networkConfiguration.Clone();

        /// <summary>
        /// Initializes a new instance of the <see cref="DaqifiStreamingDevice"/> class.
        /// </summary>
        /// <param name="name">The name of the device.</param>
        /// <param name="ipAddress">The IP address of the device, if known.</param>
        public DaqifiStreamingDevice(string name, IPAddress? ipAddress = null) : base(name, ipAddress)
        {
            StreamingFrequency = 100;
        }

        /// <summary>
        /// Initializes a new instance of the <see cref="DaqifiStreamingDevice"/> class with a transport.
        /// </summary>
        /// <param name="name">The name of the device.</param>
        /// <param name="transport">The transport for device communication.</param>
        public DaqifiStreamingDevice(string name, IStreamTransport transport) : base(name, transport)
        {
            StreamingFrequency = 100;
        }

        /// <summary>
        /// Initializes the device with the standard SCPI sequence and, for USB/serial connections,
        /// additionally sets the streaming interface to USB so data is routed to the serial consumer
        /// rather than to a previously-configured WiFi destination.
        /// </summary>
        /// <remarks>
        /// The DAQiFi firmware persists the last configured stream interface across sessions.
        /// If the device was previously set to stream to WiFi (<c>SYSTem:STReam:INTerface 1</c>),
        /// it will continue sending data over WiFi even when connected via USB — causing the serial
        /// consumer to receive nothing. Sending <c>SYSTem:STReam:INTerface 0</c> during USB
        /// initialization ensures data flows to the serial port.
        /// </remarks>
        /// <returns>A task representing the asynchronous initialization operation.</returns>
        public override async Task InitializeAsync()
        {
            // Capture pre-init state so we can skip the USB step on repeated calls
            // (base.InitializeAsync() guards with _isInitialized and returns early on repeats).
            var needsUsbInit = IsUsbConnection && State != DeviceState.Ready;

            await base.InitializeAsync();

            if (needsUsbInit)
            {
                // Direct streaming to the USB interface. Uses ExecuteTextCommandAsync so the
                // command is sent in text mode (protobuf consumer temporarily stopped) and any
                // SCPI error response is captured rather than garbling the protobuf stream.
                var lines = await ExecuteTextCommandAsync(
                    () => Send(ScpiMessageProducer.SetStreamInterface(StreamInterface.Usb)),
                    responseTimeoutMs: 500,
                    cancellationToken: CancellationToken.None);

                if (ContainsScpiError(lines))
                {
                    throw new InvalidOperationException(
                        "Device returned a SCPI error while setting stream interface to USB.");
                }
            }
        }

        /// <summary>
        /// Starts streaming data from the device at the configured frequency.
        /// </summary>
        /// <exception cref="InvalidOperationException">Thrown when the device is not connected.</exception>
        public void StartStreaming()
        {
            if (!IsConnected)
            {
                throw new InvalidOperationException("Device is not connected.");
            }

            if (IsStreaming) return;

            IsStreaming = true;
            Send(ScpiMessageProducer.StartStreaming(StreamingFrequency));
        }

        /// <summary>
        /// Stops streaming data from the device.
        /// </summary>
        /// <exception cref="InvalidOperationException">Thrown when the device is not connected.</exception>
        public void StopStreaming()
        {
            if (!IsConnected)
            {
                throw new InvalidOperationException("Device is not connected.");
            }

            if (!IsStreaming) return;

            IsStreaming = false;
            Send(ScpiMessageProducer.StopStreaming);
        }

        /// <summary>
        /// Updates the device network configuration with the specified settings.
        /// </summary>
        /// <param name="configuration">The new network configuration to apply.</param>
        /// <param name="cancellationToken">A cancellation token to observe while waiting for the task to complete.</param>
        /// <returns>A task that represents the asynchronous operation.</returns>
        /// <exception cref="InvalidOperationException">Thrown when the device is not connected.</exception>
        /// <exception cref="ArgumentNullException">Thrown when <paramref name="configuration"/> is null.</exception>
        /// <exception cref="ArgumentOutOfRangeException">Thrown when an unsupported WiFi mode or security type is specified.</exception>
        /// <exception cref="OperationCanceledException">Thrown when the operation is canceled.</exception>
        public async Task UpdateNetworkConfigurationAsync(NetworkConfiguration configuration, CancellationToken cancellationToken = default)
        {
            if (configuration == null)
            {
                throw new ArgumentNullException(nameof(configuration));
            }

            cancellationToken.ThrowIfCancellationRequested();

            if (!IsConnected)
            {
                throw new InvalidOperationException("Device is not connected.");
            }

            // Stop streaming if active
            if (IsStreaming)
            {
                StopStreaming();
            }

            // Set WiFi mode
            switch (configuration.Mode)
            {
                case WifiMode.ExistingNetwork:
                    Send(ScpiMessageProducer.SetNetworkWifiModeExisting);
                    break;
                case WifiMode.SelfHosted:
                    Send(ScpiMessageProducer.SetNetworkWifiModeSelfHosted);
                    break;
                default:
                    throw new ArgumentOutOfRangeException(nameof(configuration), configuration.Mode, "Unsupported WiFi mode.");
            }

            // Set SSID
            Send(ScpiMessageProducer.SetNetworkWifiSsid(configuration.Ssid));

            // Set security type and password
            switch (configuration.SecurityType)
            {
                case WifiSecurityType.None:
                    Send(ScpiMessageProducer.SetNetworkWifiSecurityOpen);
                    break;
                case WifiSecurityType.WpaPskPhrase:
                    Send(ScpiMessageProducer.SetNetworkWifiSecurityWpa);
                    Send(ScpiMessageProducer.SetNetworkWifiPassword(configuration.Password));
                    break;
                default:
                    throw new ArgumentOutOfRangeException(nameof(configuration), configuration.SecurityType, "Unsupported WiFi security type.");
            }

            // Apply configuration
            Send(ScpiMessageProducer.ApplyNetworkLan);

            // Wait for WiFi module to restart
            await Task.Delay(WIFI_MODULE_RESTART_DELAY_MS, cancellationToken);

            // Re-enable LAN interface (SD card and LAN share SPI bus)
            PrepareLanInterface();

            // Save configuration to persist across restarts
            Send(ScpiMessageProducer.SaveNetworkLan);

            // Update local configuration
            _networkConfiguration.Mode = configuration.Mode;
            _networkConfiguration.SecurityType = configuration.SecurityType;
            _networkConfiguration.Ssid = configuration.Ssid;
            _networkConfiguration.Password = configuration.Password;
        }

        /// <summary>
        /// Prepares the SD card interface for use by disabling the LAN interface.
        /// </summary>
        /// <exception cref="InvalidOperationException">Thrown when the device is not connected.</exception>
        public void PrepareSdInterface()
        {
            if (!IsConnected)
            {
                throw new InvalidOperationException("Device is not connected.");
            }

            Send(ScpiMessageProducer.DisableNetworkLan);
            Send(ScpiMessageProducer.EnableStorageSd);
        }

        /// <summary>
        /// Prepares the LAN interface for use by disabling the SD card interface.
        /// </summary>
        /// <exception cref="InvalidOperationException">Thrown when the device is not connected.</exception>
        public void PrepareLan
```

## ⚠ Large upstream — consider decomposing this port

`src/Daqifi.Core/Device/DaqifiStreamingDevice.cs` is 770 lines with 16 top-level methods. A single plan step covering the whole file is rarely tractable. Consider splitting into separate plan steps using `target_symbol` to scope each step to one method or method group.

Top-level methods detected:
- `DaqifiStreamingDevice`
- `InitializeAsync`
- `StartStreaming`
- `StopStreaming`
- `UpdateNetworkConfigurationAsync`
- `PrepareSdInterface`
- `PrepareLanInterface`
- `GetSdCardFilesAsync`
- `StartSdCardLoggingAsync`
- `StopSdCardLoggingAsync`
- `DeleteSdCardFileAsync`
- `FormatSdCardAsync`
- `DownloadSdCardFileAsync`
- `ContainsScpiError`
- `ValidateSdCardFileName`
- _(+1 more)_

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

- `DaqifiDevice` **[base contract]** (from `src/Daqifi.Core/Device/DaqifiDevice.cs`) _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Device/DaqifiDevice.cs` for the full contract)_
### `ILanChipInfoProvider` **[base contract]** (from `src/Daqifi.Core/Firmware/ILanChipInfoProvider.cs`)

```csharp
public interface ILanChipInfoProvider
{
    /// <summary>
    /// Queries the device for its current WiFi module chip information.
    /// </summary>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>
    /// The parsed <see cref="LanChipInfo"/>, or <see langword="null"/> if the device
    /// did not return a recognizable response.
    /// </returns>
    Task<LanChipInfo?> GetLanChipInfoAsync(CancellationToken cancellationToken = default);
}
```

- `INetworkConfigurable` **[base contract]** (from `src/Daqifi.Core/Device/Network/INetworkConfigurable.cs`) _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Device/Network/INetworkConfigurable.cs` for the full contract)_
- `ISdCardOperations` **[base contract]** (from `src/Daqifi.Core/Device/SdCard/ISdCardOperations.cs`) _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Device/SdCard/ISdCardOperations.cs` for the full contract)_
### `IStreamingDevice` **[base contract]** (from `src/Daqifi.Core/Device/IStreamingDevice.cs`)

```csharp
    public interface IStreamingDevice : IDevice
    {
        /// <summary>
        /// Gets or sets the streaming frequency in Hz.
        /// </summary>
        int StreamingFrequency { get; set; }

        /// <summary>
        /// Gets a value indicating whether the device is currently streaming data.
        /// </summary>
        bool IsStreaming { get; }

        /// <summary>
        /// Starts streaming data from the device.
        /// </summary>
        void StartStreaming();

        /// <summary>
        /// Stops streaming data from the device.
        /// </summary>
        void StopStreaming();
    }
```

## Target rationale

Target file has 23 top-level declarations already — extending an established module. Match its existing structure.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `Internal serial communication layer for DAQiFi Nyquist devices.`
      `This module provides low-level SCPI command interface over serial port.`
      `Not intended for direct use - use the public API instead.`
      `Ports the C# ``TransportStatusEventArgs`` (subclass of ``EventArgs``).`
      `Python uses plain callables instead of the .NET event pattern, so the`
-     line 649: `# All Nyquist variants currently share the same capability set`
          `nyquist_caps = dict(`
          `supports_streaming=True,`
          `has_sd_card=True,`
          `has_wifi=True,`
          `has_usb=True,`
-       line 746: `# Wait for device to stabilize`
            `time.sleep(0.1)`
-       line 748: `# Clear any pending data`
            `self._serial.reset_input_buffer()`
            `self._serial.reset_output_buffer()`
            `return True`
            `except (serial.SerialException, OSError) as e:`
            `return False`
-       line 783: `# Send command with CR terminator`
            `cmd_bytes = (command + "\r").encode('ascii')`
            `self._serial.write(cmd_bytes)`
            `self._serial.flush()`
-       line 788: `# Wait for device to process`
            `time.sleep(delay)`
-       line 791: `# Read all available data`
            `response = b""`
            `start_time = time.time()`
            `while (time.time() - start_time) < self.timeout:`
            `if self._serial.in_waiting > 0:`
            `chunk = self._serial.read(self._serial.in_waiting)`
-           line 799: `# Give a bit more time for any trailing data`
                `time.sleep(0.05)`
                `if self._serial.in_waiting == 0:`
                `break`
                `else:`
-           line 805: `# No data yet, wait a bit`
                `time.sleep(0.01)`
-       line 808: `# Decode and clean response`
            `response_str = response.decode('ascii', errors='ignore')`
-       line 811: `# Remove echoed command if present`
            `if command in response_str:`
            `response_str = response_str.replace(command, "")`
            `response_str = response_str.replace("DAQIFI>", "")`
-       line 818: `# Clean up whitespace and control characters`
            `response_str = response_str.strip()`
            `return response_str`
            `except (serial.SerialException, OSError) as e:`
            `raise RuntimeError(f"Serial communication error: {e}")`
            `def send_query(self, query: str, delay: float = 0.1) -> str:`
-     line 838: `# Remove any surrounding quotes from response`
          `response = response.strip('"').strip("'")`
          `return response`
          `def send_set(self, command: str, value, delay: float = 0.1) -> bool:`
-     line 854: `# Format value based on type`
          `if isinstance(value, str):`
          `cmd = f"{command} {value}"`
          `elif isinstance(value, bool):`
          `cmd = f"{command} {1 if value else 0}"`
          `elif isinstance(value, (int, float)):`
-     line 866: `# Check for errors`
          `error = self.send_query("SYST:ERR?", 0.1)`
          `return "No error" in error`
          `def clear_errors(self):`
          `max_errors = 10`
          `for _ in range(max_errors):`

**Top-level declarations** (existing module-level symbols — use as structural overview):

_Type definitions:_
- `class TransportStatusEventArgs`
- `class ConnectionRetryOptions`
- `class StreamTransport`
- `class IDataSample`
- `class DataSample`
- `class SampleReceivedEventArgs`
- `class OutboundMessage`
- `class ScpiMessage`
- `class IInboundMessage`
- `class GenericInboundMessage`
- `class TextInboundMessage`
- `class MessageProducer`
- `class MessageReceivedEventArgs`
- `class MessageConsumerErrorEventArgs`
- `class MessageConsumer`
- `class HidDeviceInfo`
- `class DeviceConnectionOptions`
- `class DeviceType`
- `class DeviceCapabilities`
- `class DeviceMetadata`
- `class PowerState`

_Implementations:_
- `class SerialInterface`

_Type aliases:_
- `T (TypeVar/alias)`

```python
"""
Internal serial communication layer for DAQiFi Nyquist devices.

This module provides low-level SCPI command interface over serial port.
Not intended for direct use - use the public API instead.
"""

import serial
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    @property
    def value(self) -> float:
        ...


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
        """Return the ASCII bytes for transmission, with CRLF terminator."""
        return (self.data + "\r\n").encode("ascii")


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


@dataclass
class DeviceMetadata:
    """Metadata and configuration information for a DAQiFi device.

    Ports the C# ``DeviceMetadata`` class from
    ``Daqifi.Core/Device/DeviceMetadata.cs``. Twelve mutable fields with
    sensible empty/zero defaults; the ``capabilities`` field is wired to
    a fresh :class:`DeviceCapabilities` instance via ``default_factory``
    to avoid the classic shared-mutable-default trap.

    **Divergence from upstream (UpdateFromProtobuf omitted):** the C#
    class exposes an ``UpdateFromProtobuf(DaqifiOutMessage)`` method that
    conditionally copies fields from a protobuf message and derives
    ``device_type``/``capabilities`` via ``DeviceTypeDetector`` plus
    ``NetworkAddressHelper``. None of those upstream types
    (``DaqifiOutMessage``, ``DeviceTypeDetector``, ``NetworkAddressHelper``)
    are ported yet, so the method is intentionally omitted -- adding it
    here would require dragging in the full protobuf surface and the
    network/detector helpers. Reintroduce as a separate plan step once
    those dependencies land; until then, callers populate fields
    directly (which is what the protobuf bridge will eventually do
    internally anyway).
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

- `@staticmethod`: 10 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 10 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 4 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 9 usages (e.g. `daqifi/_internal/comm.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 18 usages (e.g. `daqifi/discovery.py`)

## Research hits (cached docs containing the shard name)

- `scpi_protocol/ieee-488-2-scpi-command-parsing-rules.md:15`: test fails, but functional behavior on a compliant device is identical.

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
