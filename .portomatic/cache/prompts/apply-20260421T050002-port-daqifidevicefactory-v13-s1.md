# portomatic apply prompt

**Plan:** `20260421T050002-port-daqifidevicefactory-v13`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.13 test 2: DaqifiDeviceFactory (public static class, all-static methods, no instance)
- classification: new-feature
- shard: Device
- upstream_ref: e8d84af:src/Daqifi.Core/Device/DaqifiDeviceFactory.cs
- downstream_target: daqifi/device_factory.py

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
- `event EventHandler<T>` → Single event: `Callable[[T], None]` callback slot OR `add_<event>`/`remove_<event>` if subscribers > 1. Multiple events on same interface: prefix add/remove with the event name (e.g. `add_message_received`, `remove_message_received`) and keep callback lists per-event. Mirror this pattern across all events on the same Protocol/class for consistency.
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Stream` → `typing.IO[bytes]` (or `BinaryIO`) — file-like objects with read/write
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `NotImplementedException / NotSupportedException` → `NotImplementedError`
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.
- `[EnumeratorCancellation] CancellationToken ct` → No syntactic Python equivalent. The async-iterator consumer cancels via the enclosing `asyncio.Task` (`task.cancel()` raises `asyncio.CancelledError` at the next `await` inside the iterator). For explicit cooperative cancellation, accept an `asyncio.Event` or `asyncio.CancelledError`-aware callable parameter and check it inside the loop. Don't transliterate the attribute or the `CancellationToken` type.

## .NET helper idioms

Common BCL helpers that recur across nearly every C#→Python port. Use the listed equivalent rather than re-deriving the translation per call site:

| C# | Python equivalent |
|---|---|
| `string.IsNullOrWhiteSpace(s)` | `not s or not s.strip()` |
| `string.IsNullOrEmpty(s)` | `not s` (works for None and empty string) |
| `nameof(x)` | `"x"` (literal string — common in exception messages and validation) |
| `string.Format("{0}", x)` | `f"{x}"` (f-string) — fall back to `.format()` only when the format string is dynamic |
| `string.Join(",", items)` | `",".join(items)` when items are strings; `",".join(str(x) for x in items)` when not — Python `str.join` raises `TypeError` on non-string elements while .NET `string.Join` stringifies them. Note the order flip — separator first in C#, last in Python. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using System.Net;
using Daqifi.Core.Communication.Transport;
using Daqifi.Core.Device.Discovery;

#nullable enable

namespace Daqifi.Core.Device;

/// <summary>
/// Default baud rate for serial connections.
/// </summary>
internal static class SerialDefaults
{
    public const int BaudRate = 9600;
}

/// <summary>
/// Factory class for creating and connecting to DAQiFi devices with a simplified API.
/// Provides a single-call connection interface that handles transport creation, connection,
/// and device initialization.
/// </summary>
public static class DaqifiDeviceFactory
{
    /// <summary>
    /// Connects to a DAQiFi device over TCP asynchronously using a hostname.
    /// </summary>
    /// <param name="host">The hostname or IP address string to connect to.</param>
    /// <param name="port">The TCP port to connect to.</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <param name="cancellationToken">A cancellation token to cancel the operation.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when host is null or empty.</exception>
    /// <exception cref="ArgumentOutOfRangeException">Thrown when port is not between 1 and 65535.</exception>
    /// <exception cref="OperationCanceledException">Thrown when the operation is cancelled.</exception>
    public static async Task<DaqifiDevice> ConnectTcpAsync(
        string host,
        int port,
        DeviceConnectionOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(host))
        {
            throw new ArgumentNullException(nameof(host), "Host cannot be null or empty.");
        }

        ValidatePort(port);

        cancellationToken.ThrowIfCancellationRequested();

        var effectiveOptions = options ?? DeviceConnectionOptions.Default;
        var transport = new TcpStreamTransport(host, port);

        return await ConnectWithTransportAsync(transport, effectiveOptions, cancellationToken)
            .ConfigureAwait(false);
    }

    /// <summary>
    /// Connects to a DAQiFi device over TCP asynchronously using an IP address.
    /// </summary>
    /// <param name="ipAddress">The IP address to connect to.</param>
    /// <param name="port">The TCP port to connect to.</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <param name="cancellationToken">A cancellation token to cancel the operation.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when ipAddress is null.</exception>
    /// <exception cref="ArgumentOutOfRangeException">Thrown when port is not between 1 and 65535.</exception>
    /// <exception cref="OperationCanceledException">Thrown when the operation is cancelled.</exception>
    public static async Task<DaqifiDevice> ConnectTcpAsync(
        IPAddress ipAddress,
        int port,
        DeviceConnectionOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        if (ipAddress == null)
        {
            throw new ArgumentNullException(nameof(ipAddress));
        }

        ValidatePort(port);

        cancellationToken.ThrowIfCancellationRequested();

        var effectiveOptions = options ?? DeviceConnectionOptions.Default;
        var transport = new TcpStreamTransport(ipAddress, port);

        return await ConnectWithTransportAsync(transport, effectiveOptions, cancellationToken)
            .ConfigureAwait(false);
    }

    /// <summary>
    /// Connects to a DAQiFi device over TCP synchronously using a hostname.
    /// </summary>
    /// <param name="host">The hostname or IP address string to connect to.</param>
    /// <param name="port">The TCP port to connect to.</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when host is null or empty.</exception>
    public static DaqifiDevice ConnectTcp(
        string host,
        int port,
        DeviceConnectionOptions? options = null)
    {
        return ConnectTcpAsync(host, port, options, CancellationToken.None).GetAwaiter().GetResult();
    }

    /// <summary>
    /// Connects to a DAQiFi device over TCP synchronously using an IP address.
    /// </summary>
    /// <param name="ipAddress">The IP address to connect to.</param>
    /// <param name="port">The TCP port to connect to.</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when ipAddress is null.</exception>
    public static DaqifiDevice ConnectTcp(
        IPAddress ipAddress,
        int port,
        DeviceConnectionOptions? options = null)
    {
        return ConnectTcpAsync(ipAddress, port, options, CancellationToken.None).GetAwaiter().GetResult();
    }

    /// <summary>
    /// Connects to a DAQiFi device over a serial port asynchronously using the default baud rate (9600).
    /// </summary>
    /// <param name="portName">The name of the serial port (e.g., "COM3", "/dev/ttyUSB0").</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <param name="cancellationToken">A cancellation token to cancel the operation.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when portName is null or empty.</exception>
    /// <exception cref="OperationCanceledException">Thrown when the operation is cancelled.</exception>
    public static Task<DaqifiDevice> ConnectSerialAsync(
        string portName,
        DeviceConnectionOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        return ConnectSerialAsync(portName, SerialDefaults.BaudRate, options, cancellationToken);
    }

    /// <summary>
    /// Connects to a DAQiFi device over a serial port asynchronously with a specified baud rate.
    /// </summary>
    /// <param name="portName">The name of the serial port (e.g., "COM3", "/dev/ttyUSB0").</param>
    /// <param name="baudRate">The baud rate for the serial connection.</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <param name="cancellationToken">A cancellation token to cancel the operation.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when portName is null or empty.</exception>
    /// <exception cref="ArgumentOutOfRangeException">Thrown when baudRate is less than or equal to zero.</exception>
    /// <exception cref="OperationCanceledException">Thrown when the operation is cancelled.</exception>
    public static async Task<DaqifiDevice> ConnectSerialAsync(
        string portName,
        int baudRate,
        DeviceConnectionOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        if (portName == null)
        {
            throw new ArgumentNullException(nameof(portName));
        }

        if (string.IsNullOrWhiteSpace(portName))
        {
            throw new ArgumentException("Port name cannot be empty or whitespace.", nameof(portName));
        }

        if (baudRate <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(baudRate), baudRate, "Baud rate must be greater than zero.");
        }

        cancellationToken.ThrowIfCancellationRequested();

        var effectiveOptions = options ?? DeviceConnectionOptions.Default;
        var transport = new SerialStreamTransport(portName, baudRate);

        return await ConnectWithTransportAsync(transport, effectiveOptions, cancellationToken)
            .ConfigureAwait(false);
    }

    /// <summary>
    /// Connects to a DAQiFi device over a serial port synchronously using the default baud rate (9600).
    /// </summary>
    /// <param name="portName">The name of the serial port (e.g., "COM3", "/dev/ttyUSB0").</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when portName is null or empty.</exception>
    public static DaqifiDevice ConnectSerial(
        string portName,
        DeviceConnectionOptions? options = null)
    {
        return ConnectSerialAsync(portName, SerialDefaults.BaudRate, options, CancellationToken.None).GetAwaiter().GetResult();
    }

    /// <summary>
    /// Connects to a DAQiFi device over a serial port synchronously with a specified baud rate.
    /// </summary>
    /// <param name="portName">The name of the serial port (e.g., "COM3", "/dev/ttyUSB0").</param>
    /// <param name="baudRate">The baud rate for the serial connection.</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when portName is null or empty.</exception>
    /// <exception cref="ArgumentOutOfRangeException">Thrown when baudRate is less than or equal to zero.</exception>
    public static DaqifiDevice ConnectSerial(
        string portName,
        int baudRate,
        DeviceConnectionOptions? options = null)
    {
        return ConnectSerialAsync(portName, baudRate, options, CancellationToken.None).GetAwaiter().GetResult();
    }

    /// <summary>
    /// Connects to a DAQiFi device asynchronously using device discovery information.
    /// </summary>
    /// <param name="deviceInfo">The device information from discovery.</param>
    /// <param name="options">Optional connection options. If null, uses default options.</param>
    /// <param name="cancellationToken">A cancellation token to cancel the operation.</param>
    /// <returns>A connected and optionally initialized <see cref="DaqifiDevice"/>.</returns>
    /// <exception cref="ArgumentNullException">Thrown when deviceInfo is null.</exception>
    /// <exception cref="ArgumentException">Thrown when deviceInfo is missing required fields (IPAddress or Port for WiFi connections).</exception>
    /// <exception cref="NotSupportedException">Thrown when the connection type is not supported (e.g., Serial, HID).</exception>
    /// <exception cref="OperationCanceledException">Thrown when the operation is cancelled.</exception>
    public static async Task<DaqifiDevice> ConnectFromDeviceInfoAsync(
        IDeviceInfo deviceInfo,
        DeviceConnectionOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        if (deviceInfo == null)
        {
            throw new ArgumentNullException(nameof(deviceInfo));
        }

        cancellationToken.ThrowIfCancellationRequested();

        // Handle connection type
        switch (deviceInfo.ConnectionType)
        {
            case ConnectionType.WiFi:
                return await ConnectWiFiDeviceAsync(deviceInfo, options, cancellationToken)
                    .ConfigureAwait(false);

            case ConnectionType.Serial:
                return await ConnectSerialDeviceAsync(deviceInfo, options, cancellationToken)
                    .ConfigureAwait(false);

            case ConnectionType.Hid:
                throw new NotSupportedException(
// ... [TRUNCATED: 6450 more chars (~171 lines) — see src/Daqifi.Core/Device/DaqifiDeviceFactory.cs for the rest]
```

## ⚠ Multi-class upstream file (no `target_symbol` set)

The upstream file `src/Daqifi.Core/Device/DaqifiDeviceFactory.cs` defines 2 top-level types: `SerialDefaults` (not yet ported — separate plan step recommended), `DaqifiDeviceFactory` (not yet ported — separate plan step recommended).

The plan step has no explicit `target_symbol`. Identify which type the step's summary refers to and port ONLY that one. Setting `target_symbol` on future steps removes this ambiguity.

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `ConnectionType` (from `src/Daqifi.Core/Device/Discovery/IDeviceInfo.cs`)

```csharp
public enum ConnectionType
{
    /// <summary>
    /// Unknown connection type.
    /// </summary>
    Unknown,

    /// <summary>
    /// WiFi/TCP connection.
    /// </summary>
    WiFi,

    /// <summary>
    /// Serial/USB connection.
    /// </summary>
    Serial,

    /// <summary>
    /// USB HID connection (bootloader mode).
    /// </summary>
    Hid
}
```

### `DeviceConnectionOptions` (from `src/Daqifi.Core/Device/DeviceConnectionOptions.cs`) **[already ported as `daqifi/_internal/comm.py:558`]**

```csharp
public class DeviceConnectionOptions
{
    /// <summary>
    /// Gets or sets the device name. Default is "DAQiFi Device".
    /// </summary>
    public string DeviceName { get; set; } = "DAQiFi Device";

    /// <summary>
    /// Gets or sets the connection retry options for transport-level retry behavior.
    /// Default is null, which uses the transport's default behavior.
    /// </summary>
    public ConnectionRetryOptions? ConnectionRetry { get; set; }

    /// <summary>
    /// Gets or sets a value indicating whether to run device initialization after connection.
    /// When true, <see cref="DaqifiDevice.InitializeAsync"/> is called after connecting.
    /// Default is true.
    /// </summary>
    public bool InitializeDevice { get; set; } = true;

    /// <summary>
    /// Creates a default configuration with default retry behavior and device initialization enabled.
    /// </summary>
    public static DeviceConnectionOptions Default => new();

    /// <summary>
    /// Creates a configuration optimized for fast connections with minimal retries.
    /// Uses <see cref="ConnectionRetryOptions.Fast"/> for transport retry behavior.
    /// </summary>
    public static DeviceConnectionOptions Fast => new()
    {
        ConnectionRetry = ConnectionRetryOptions.Fast
    };

    /// <summary>
    /// Creates a configuration optimized for slow or unreliable connections.
    /// Uses <see cref="ConnectionRetryOptions.Resilient"/> for transport retry behavior.
    /// </summary>
    public static DeviceConnectionOptions Resilient => new()
    {
        ConnectionRetry = ConnectionRetryOptions.Resilient
    };
}
```

- `DeviceInfo` (from `src/Daqifi.Core/Device/Discovery/DeviceInfo.cs`) **[already ported as `daqifi/discovery.py:23`]** _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Device/Discovery/DeviceInfo.cs` for the full contract)_
## Downstream target (current contents)

_File does not exist yet at `daqifi/device_factory.py` — create it._

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
