# portomatic apply prompt

**Plan:** `20260420T230003-port-firmwareupdateservice-v12`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.12 test 3: FirmwareUpdateService (6+ collaborators, AllowedTransitions dict, StateChanged event, ~1157 lines)
- classification: new-feature
- shard: Firmware
- upstream_ref: e8d84af:src/Daqifi.Core/Firmware/FirmwareUpdateService.cs
- downstream_target: daqifi/firmware/update_service.py

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `event EventHandler<T>` → Single event: `Callable[[T], None]` callback slot OR `add_<event>`/`remove_<event>` if subscribers > 1. Multiple events on same interface: prefix add/remove with the event name (e.g. `add_message_received`, `remove_message_received`) and keep callback lists per-event. Mirror this pattern across all events on the same Protocol/class for consistency.
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `TimeSpan` → `float` (seconds) or `datetime.timedelta` if duration arithmetic is needed
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `InvalidOperationException` → `RuntimeError` (or a domain-specific exception when the project defines one)
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `IReadOnlyList<T> / IList<T>` → Read-only: `Sequence[T]` (typing — covariant, accepts tuple/list/any sequence). Mutable: `list[T]` or `MutableSequence[T]`. Use `tuple[T, ...]` instead of Sequence when the field/return is a fixed snapshot.
- `IReadOnlyDictionary<K,V> / IDictionary<K,V>` → Read-only: `Mapping[K, V]` (typing). Mutable: `dict[K, V]` or `MutableMapping[K, V]`.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.
- `[EnumeratorCancellation] CancellationToken ct` → No syntactic Python equivalent. The async-iterator consumer cancels via the enclosing `asyncio.Task` (`task.cancel()` raises `asyncio.CancelledError` at the next `await` inside the iterator). For explicit cooperative cancellation, accept an `asyncio.Event` or `asyncio.CancelledError`-aware callable parameter and check it inside the loop. Don't transliterate the attribute or the `CancellationToken` type.
- `IProgress<T> progress` → `progress: Callable[[T], None] | None = None` (a plain callback). Call as `progress(value) if progress else None`. The .NET `IProgress<T>` abstraction adds nothing in Python — a callable is enough.
- `ILogger<TCategory> logger` → `logger: logging.Logger`, typically initialized with `logging.getLogger(__name__)` (the .NET generic-category parameter `TCategory` is the Python equivalent of the module/class name in the logger name). Calls map: `LogInformation` → `info`, `LogWarning` → `warning`, `LogError` → `error`, `LogDebug` → `debug`. Structured-logging placeholders (`{Foo}`) become f-strings or %-format args.
- `Func<T1,...,Tn,R> / Action<T> / Func<R>` → `Callable[[T1, ..., Tn], R]` (from `typing`). `Action<T>` → `Callable[[T], None]`. `Func<R>` (no params) → `Callable[[], R]`. For optional callbacks use `Callable[..., R] | None = None`.

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

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using Daqifi.Core.Communication.Producers;
using Daqifi.Core.Communication.Transport;
using Daqifi.Core.Device;
using Microsoft.Extensions.Logging;

namespace Daqifi.Core.Firmware;

/// <summary>
/// Default firmware update orchestration service for PIC32 and WiFi update flows.
/// </summary>
public sealed class FirmwareUpdateService : IFirmwareUpdateService, IDisposable
{
    private static readonly Regex PercentRegex = new(
        @"(?<percent>\d{1,3})\s*%",
        RegexOptions.Compiled | RegexOptions.CultureInvariant);

    private static readonly IReadOnlyDictionary<FirmwareUpdateState, IReadOnlySet<FirmwareUpdateState>> AllowedTransitions
        = new Dictionary<FirmwareUpdateState, IReadOnlySet<FirmwareUpdateState>>
        {
            [FirmwareUpdateState.Idle] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.PreparingDevice,
                FirmwareUpdateState.Complete,
                FirmwareUpdateState.Failed
            },
            [FirmwareUpdateState.PreparingDevice] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.WaitingForBootloader,
                FirmwareUpdateState.Programming,
                FirmwareUpdateState.Failed
            },
            [FirmwareUpdateState.WaitingForBootloader] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.Connecting,
                FirmwareUpdateState.Failed
            },
            [FirmwareUpdateState.Connecting] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.ErasingFlash,
                FirmwareUpdateState.Failed
            },
            [FirmwareUpdateState.ErasingFlash] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.Programming,
                FirmwareUpdateState.Failed
            },
            [FirmwareUpdateState.Programming] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.Verifying,
                FirmwareUpdateState.JumpingToApp,
                FirmwareUpdateState.Failed
            },
            [FirmwareUpdateState.Verifying] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.JumpingToApp,
                FirmwareUpdateState.Complete,
                FirmwareUpdateState.Failed
            },
            [FirmwareUpdateState.JumpingToApp] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.Complete,
                FirmwareUpdateState.Failed
            },
            [FirmwareUpdateState.Complete] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.Idle
            },
            [FirmwareUpdateState.Failed] = new HashSet<FirmwareUpdateState>
            {
                FirmwareUpdateState.Idle
            }
        };

    private readonly SemaphoreSlim _operationLock = new(1, 1);
    private readonly IHidTransport _hidTransport;
    private readonly IExternalProcessRunner _externalProcessRunner;
    private readonly ILogger<FirmwareUpdateService> _logger;
    private readonly IBootloaderProtocol _bootloaderProtocol;
    private readonly IHidDeviceEnumerator _hidDeviceEnumerator;
    private readonly FirmwareUpdateServiceOptions _options;

    private string _currentOperation = "Idle";
    private double _lastReportedPercent;
    private int _bootloaderPollAttempts;
    private Exception? _lastBootloaderEnumerationError;
    private bool _disposed;

    /// <summary>
    /// Initializes a new firmware update service.
    /// </summary>
    public FirmwareUpdateService(
        IHidTransport hidTransport,
        IFirmwareDownloadService firmwareDownloadService,
        IExternalProcessRunner externalProcessRunner,
        ILogger<FirmwareUpdateService> logger,
        IBootloaderProtocol? bootloaderProtocol = null,
        IHidDeviceEnumerator? hidDeviceEnumerator = null,
        FirmwareUpdateServiceOptions? options = null)
    {
        _hidTransport = hidTransport ?? throw new ArgumentNullException(nameof(hidTransport));
        FirmwareDownloadService = firmwareDownloadService ?? throw new ArgumentNullException(nameof(firmwareDownloadService));
        _externalProcessRunner = externalProcessRunner ?? throw new ArgumentNullException(nameof(externalProcessRunner));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        _bootloaderProtocol = bootloaderProtocol ?? new Pic32BootloaderProtocol();
        _hidDeviceEnumerator = hidDeviceEnumerator ?? new HidLibraryDeviceEnumerator();
        _options = options ?? new FirmwareUpdateServiceOptions();
        _options.Validate();
    }

    /// <summary>
    /// Gets the composed firmware download service for callers that coordinate
    /// firmware acquisition and update orchestration from a shared service graph.
    /// </summary>
    public IFirmwareDownloadService FirmwareDownloadService { get; }

    /// <inheritdoc />
    public FirmwareUpdateState CurrentState { get; private set; } = FirmwareUpdateState.Idle;

    /// <inheritdoc />
    public event EventHandler<FirmwareUpdateStateChangedEventArgs>? StateChanged;

    /// <inheritdoc />
    public async Task UpdateFirmwareAsync(
        IStreamingDevice device,
        string hexFilePath,
        IProgress<FirmwareUpdateProgress>? progress = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(device);

        if (string.IsNullOrWhiteSpace(hexFilePath))
        {
            throw new ArgumentException("HEX file path cannot be empty.", nameof(hexFilePath));
        }

        if (!File.Exists(hexFilePath))
        {
            throw new FileNotFoundException("Firmware HEX file was not found.", hexFilePath);
        }

        var hexRecords = ParseHexRecords(hexFilePath);
        var totalBytes = hexRecords.Sum(record => (long)record.Length);
        if (totalBytes <= 0)
        {
            throw new InvalidDataException("Firmware HEX file did not contain any writable records.");
        }

        await RunExclusiveAsync(
            ct => RunPic32UpdateAsync(device, hexRecords, totalBytes, progress, ct),
            cancellationToken).ConfigureAwait(false);
    }

    /// <inheritdoc />
    public async Task UpdateWifiModuleAsync(
        IStreamingDevice device,
        string firmwarePath,
        IProgress<FirmwareUpdateProgress>? progress = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(device);

        if (string.IsNullOrWhiteSpace(firmwarePath))
        {
            throw new ArgumentException("WiFi firmware path cannot be empty.", nameof(firmwarePath));
        }

        var pathExists = File.Exists(firmwarePath) || Directory.Exists(firmwarePath);
        if (!pathExists)
        {
            throw new FileNotFoundException("WiFi firmware path was not found.", firmwarePath);
        }

        await RunExclusiveAsync(
            ct => RunWifiUpdateAsync(device, firmwarePath, progress, ct),
            cancellationToken).ConfigureAwait(false);
    }

    private async Task RunExclusiveAsync(
        Func<CancellationToken, Task> operation,
        CancellationToken cancellationToken)
    {
        ThrowIfDisposed();

        await _operationLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ResetIfTerminalState();

            if (CurrentState != FirmwareUpdateState.Idle)
            {
                throw new InvalidOperationException(
                    $"Cannot start firmware update while service is in state {CurrentState}.");
            }

            _lastReportedPercent = 0;
            _bootloaderPollAttempts = 0;
            _lastBootloaderEnumerationError = null;
            await operation(cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _operationLock.Release();
        }
    }

    private async Task RunPic32UpdateAsync(
        IStreamingDevice device,
        IReadOnlyList<byte[]> hexRecords,
        long totalBytes,
        IProgress<FirmwareUpdateProgress>? progress,
        CancellationToken cancellationToken)
    {
        try
        {
            TransitionToState(FirmwareUpdateState.PreparingDevice, "Preparing device for PIC32 firmware update.");
            ReportProgress(progress, FirmwareUpdateState.PreparingDevice, 0, _currentOperation, 0, totalBytes);

            await ExecuteWithStateTimeoutAsync(
                FirmwareUpdateState.PreparingDevice,
                "prepare the device for bootloader mode",
                async stateToken =>
                {
                    EnsureDeviceConnected(device);

                    if (device.IsStreaming)
                    {
                        device.StopStreaming();
                    }

                    device.Send(ScpiMessageProducer.ForceBootloader);
                    await Task.Delay(_options.PostForceBootDelay, stateToken).ConfigureAwait(false);
                    device.Disconnect();
                },
                cancellationToken).ConfigureAwait(false);

            TransitionToState(FirmwareUpdateState.WaitingForBootloader, "Waiting for HID bootloader device.");
            ReportProgress(progress, FirmwareUpdateState.WaitingForBootloader, 5, _currentOperation, 0, totalBytes);

            var hidDevice = await ExecuteWithStateTimeoutAsync(
                FirmwareUpdateState.WaitingForBootloader,
                "wait for HID bootloader enumeration",
                WaitForBootloaderDeviceAsync,
                cancellationToken).ConfigureAwait(false);

            TransitionToState(FirmwareUpdateState.Connecting, "Connecting to HID bootloader.");
            ReportProgress(progress, FirmwareUpdateState.Connecting, 10, _currentOperation, 0, totalBytes);

            await ExecuteWithStateTimeoutAsync(
                FirmwareUpdateState.Connecting,
                "connect HID transport",
                ct => ConnectToBootloaderWithRetryAsync(hidDevice, ct),
                cancellationToken).ConfigureAwait(false);

            var version = await ExecuteWithStateTimeoutAsync(
                FirmwareUpdateState.Connecting,
                "request bootloader version",
                RequestBootloaderVersionAsync,
                cancellationToken).ConfigureAwait(false);

            _logger.LogInformation("Bootloader version response: {BootloaderVersion}", version);

            TransitionToState(FirmwareUpdateState.ErasingFlash, "Erasing PIC32 flash.");
            ReportProgress(progress, FirmwareUpdateState.ErasingFlash, 15, _currentOperation, 0, totalBytes);

            await ExecuteWithStateTimeoutAsync(
                FirmwareUpdateState.ErasingFlash,
                "erase flash",
                EraseFlashWithRetryAsync,
                cancellationToken).ConfigureAwait(false);

            TransitionToState(FirmwareUpdateState.Programming, "Programming flash records.");
            ReportProgress(progress, FirmwareUpdateState.Programming, 20, _currentOperation, 0, totalBytes);

            await ExecuteWithStateTimeoutAsync(
                FirmwareUpdateState.Programming,
                "program flash records",
                ct => ProgramFlashAsync(hexRecords, totalBytes, progress, ct),
                cancellationToken).ConfigureAwait(false);

            TransitionToState(FirmwareUpdateState.Verifying, "Verifying bootloader response.");
            ReportProgress(progress, FirmwareUpdateState.Verifying, 92, _currentOperation, totalBytes, totalBytes);

            var verificationVersion = await ExecuteWithStateTimeoutAsync(
                FirmwareUpdateState.Verifying,
                "verify bootloader response",
                RequestBootloaderVersionAsync,
                cancellationToken).ConfigureAwait(false);
// ... [TRUNCATED: 31315 more chars (~865 lines) — see src/Daqifi.Core/Firmware/FirmwareUpdateService.cs for the rest]
```

## ⚠ Large upstream — consider decomposing this port

`src/Daqifi.Core/Firmware/FirmwareUpdateService.cs` is 1156 lines with 35 top-level methods. A single plan step covering the whole file is rarely tractable. Consider splitting into separate plan steps using `target_symbol` to scope each step to one method or method group.

Top-level methods detected:
- `UpdateFirmwareAsync`
- `UpdateWifiModuleAsync`
- `RunExclusiveAsync`
- `RunPic32UpdateAsync`
- `RunWifiUpdateAsync`
- `IsWifiFirmwareUpToDateAsync`
- `IsWifiVersionCurrent`
- `BuildWifiProcessRequest`
- `ContainsWifiProgrammingFailure`
- `ResolveWifiToolPath`
- `ResolveWifiPort`
- `QuoteArgument`
- `ParseHexRecords`
- `WaitForBootloaderDeviceAsync`
- `ConnectToBootloaderWithRetryAsync`
- _(+20 more)_

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `IFirmwareUpdateService` **[base contract]** (from `src/Daqifi.Core/Firmware/IFirmwareUpdateService.cs`)

```csharp
public interface IFirmwareUpdateService
{
    /// <summary>
    /// Gets the current update state.
    /// </summary>
    FirmwareUpdateState CurrentState { get; }

    /// <summary>
    /// Raised whenever the update state transitions.
    /// Handlers should not synchronously invoke update operations on this same service instance.
    /// </summary>
    event EventHandler<FirmwareUpdateStateChangedEventArgs>? StateChanged;

    /// <summary>
    /// Executes a PIC32 firmware update through bootloader mode using a local HEX file.
    /// </summary>
    /// <param name="device">The connected streaming device to update.</param>
    /// <param name="hexFilePath">Path to an Intel HEX firmware file.</param>
    /// <param name="progress">Optional progress reporter.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    Task UpdateFirmwareAsync(
        IStreamingDevice device,
        string hexFilePath,
        IProgress<FirmwareUpdateProgress>? progress = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Executes a WiFi module update using an external flashing tool.
    /// </summary>
    /// <param name="device">The connected streaming device to update.</param>
    /// <param name="firmwarePath">Path to a WiFi tool executable/script or directory containing it.</param>
    /// <param name="progress">Optional progress reporter.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    Task UpdateWifiModuleAsync(
        IStreamingDevice device,
        string firmwarePath,
        IProgress<FirmwareUpdateProgress>? progress = null,
        CancellationToken cancellationToken = default);
}
```

- `FirmwareUpdateServiceOptions` (from `src/Daqifi.Core/Firmware/FirmwareUpdateServiceOptions.cs`) _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Firmware/FirmwareUpdateServiceOptions.cs` for the full contract)_
### `IBootloaderProtocol` (from `src/Daqifi.Core/Firmware/IBootloaderProtocol.cs`)

```csharp





public interface IBootloaderProtocol
{
    byte[] CreateRequestVersionMessage();
    byte[] CreateEraseFlashMessage();
    byte[] CreateProgramFlashMessage(byte[] hexRecord);
    byte[] CreateJumpToApplicationMessage();
    string DecodeVersionResponse(byte[] data);
    bool DecodeProgramFlashResponse(byte[] data);
    bool DecodeEraseFlashResponse(byte[] data);
    List<byte[]> ParseHexFile(string[] hexFileLines);
}
```

### `IExternalProcessRunner` (from `src/Daqifi.Core/Firmware/IExternalProcessRunner.cs`)

```csharp
public interface IExternalProcessRunner
{
    /// <summary>
    /// Executes an external process request.
    /// </summary>
    /// <param name="request">Process execution request.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>Captured execution result.</returns>
    Task<ExternalProcessResult> RunAsync(
        ExternalProcessRequest request,
        CancellationToken cancellationToken = default);
}
```

### `IFirmwareDownloadService` (from `src/Daqifi.Core/Firmware/IFirmwareDownloadService.cs`)

```csharp




public interface IFirmwareDownloadService
{
    Task<FirmwareReleaseInfo?> GetLatestReleaseAsync(
        bool includePreRelease = false,
        CancellationToken cancellationToken = default);
    Task<FirmwareUpdateCheckResult> CheckForUpdateAsync(
        string deviceVersionString,
        bool includePreRelease = false,
        CancellationToken cancellationToken = default);
    Task<string?> DownloadLatestFirmwareAsync(
        string destinationDirectory,
        bool includePreRelease = false,
        IProgress<int>? progress = null,
        CancellationToken cancellationToken = default);
    Task<string?> DownloadFirmwareByTagAsync(
        string tagName,
        string destinationDirectory,
        IProgress<int>? progress = null,
        CancellationToken cancellationToken = default);
    Task<(string ExtractedPath, string Version)?> DownloadWifiFirmwareAsync(
        string destinationDirectory,
        IProgress<int>? progress = null,
        CancellationToken cancellationToken = default);
    Task<FirmwareReleaseInfo?> GetLatestWifiReleaseAsync(
        CancellationToken cancellationToken = default);
    void InvalidateCache();
}
```

### `IHidDeviceEnumerator` (from `src/Daqifi.Core/Communication/Transport/IHidDeviceEnumerator.cs`)

```csharp
public interface IHidDeviceEnumerator
{
    /// <summary>
    /// Enumerates HID devices, optionally filtered by vendor/product identifiers.
    /// </summary>
    /// <param name="vendorId">Optional vendor ID filter.</param>
    /// <param name="productId">Optional product ID filter.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>A list of discovered HID devices.</returns>
    Task<IReadOnlyList<HidDeviceInfo>> EnumerateAsync(
        int? vendorId = null,
        int? productId = null,
        CancellationToken cancellationToken = default);
}
```

### `IHidTransport` (from `src/Daqifi.Core/Communication/Transport/IHidTransport.cs`)

```csharp




public interface IHidTransport : IDisposable
{
    bool IsConnected { get; }
    int? VendorId { get; }
    int? ProductId { get; }
    string? SerialNumber { get; }
    string? DevicePath { get; }
    TimeSpan ReadTimeout { get; set; }
    TimeSpan WriteTimeout { get; set; }
    Task ConnectAsync(
        int vendorId,
        int productId,
        string? serialNumber = null,
        CancellationToken cancellationToken = default);
    void Connect(int vendorId, int productId, string? serialNumber = null);
    Task WriteAsync(byte[] data, CancellationToken cancellationToken = default);
    void Write(byte[] data);
    Task<byte[]> ReadAsync(TimeSpan? timeout = null, CancellationToken cancellationToken = default);
    byte[] Read(TimeSpan? timeout = null);
    Task DisconnectAsync();
    void Disconnect();
}
```

- `FirmwareUpdateState` (from `src/Daqifi.Core/Firmware/FirmwareUpdateState.cs`) _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Firmware/FirmwareUpdateState.cs` for the full contract)_
- `HidDeviceInfo` (from `src/Daqifi.Core/Communication/Transport/HidDeviceInfo.cs`) **[already ported as `daqifi/_internal/comm.py:520`]** _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Communication/Transport/HidDeviceInfo.cs` for the full contract)_
### `IStreamingDevice` (from `src/Daqifi.Core/Device/IStreamingDevice.cs`)

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

### `FirmwareUpdateProgress` (from `src/Daqifi.Core/Firmware/FirmwareUpdateProgress.cs`)

```csharp
public sealed class FirmwareUpdateProgress
{
    /// <summary>
    /// Gets or sets the active firmware update state.
    /// </summary>
    public FirmwareUpdateState State { get; set; }

    /// <summary>
    /// Gets or sets the completion percentage in the range 0-100.
    /// </summary>
    public double PercentComplete { get; set; }

    /// <summary>
    /// Gets or sets a human-readable description of the current operation.
    /// </summary>
    public string CurrentOperation { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the number of bytes written so far.
    /// </summary>
    public long BytesWritten { get; set; }

    /// <summary>
    /// Gets or sets the total number of bytes expected for the operation.
    /// </summary>
    public long TotalBytes { get; set; }
}
```

### `FirmwareUpdateException` (from `src/Daqifi.Core/Firmware/FirmwareUpdateException.cs`)

```csharp
public class FirmwareUpdateException : Exception
{
    /// <summary>
    /// Initializes a new firmware update exception.
    /// </summary>
    /// <param name="failedState">The state where the failure occurred.</param>
    /// <param name="operation">The operation that was attempted.</param>
    /// <param name="message">The failure message.</param>
    /// <param name="recoveryGuidance">Optional user-facing recovery guidance.</param>
    /// <param name="innerException">Optional inner exception.</param>
    public FirmwareUpdateException(
        FirmwareUpdateState failedState,
        string operation,
        string message,
        string? recoveryGuidance = null,
        Exception? innerException = null)
        : base(message, innerException)
    {
        FailedState = failedState;
        Operation = operation ?? string.Empty;
        RecoveryGuidance = recoveryGuidance;
    }

    /// <summary>
    /// Gets the state where the failure occurred.
    /// </summary>
    public FirmwareUpdateState FailedState { get; }

    /// <summary>
    /// Gets the operation that was attempted when the failure occurred.
    /// </summary>
    public string Operation { get; }

    /// <summary>
    /// Gets optional guidance that can help recover from this failure.
    /// </summary>
    public string? RecoveryGuidance { get; }
}
```

### `ExternalProcessRequest` (from `src/Daqifi.Core/Firmware/ExternalProcessRequest.cs`)

```csharp
public sealed class ExternalProcessRequest
{
    /// <summary>
    /// Gets or sets the executable to start.
    /// </summary>
    public string FileName { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets command-line arguments.
    /// </summary>
    public string Arguments { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets an optional process working directory.
    /// </summary>
    public string? WorkingDirectory { get; set; }

    /// <summary>
    /// Gets or sets the maximum execution duration.
    /// </summary>
    public TimeSpan Timeout { get; set; } = TimeSpan.FromMinutes(5);

    /// <summary>
    /// Gets or sets a callback for each stdout line.
    /// </summary>
    public Action<string>? OnStandardOutputLine { get; set; }

    /// <summary>
    /// Gets or sets a callback for each stderr line.
    /// </summary>
    public Action<string>? OnStandardErrorLine { get; set; }

    /// <summary>
    /// Gets or sets a responder callback that can write to stdin.
    /// Return <c>null</c> for no input.
    /// </summary>
    public Func<string, string?>? StandardInputResponseFactory { get; set; }
}
```

## Downstream target (current contents)

_File does not exist yet at `daqifi/firmware/update_service.py` — create it._

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
