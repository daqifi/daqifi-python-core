# portomatic apply prompt

**Plan:** `20260420T230001-port-hidlibrarytransport-v12`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.12 test 1: HidLibraryTransport (IHidTransport + IDisposable, semaphore async/sync bridge)
- classification: new-feature
- shard: Transport
- upstream_ref: e8d84af:src/Daqifi.Core/Communication/Transport/HidLibraryTransport.cs
- downstream_target: daqifi/transport/hid_transport.py

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Stream` → `typing.IO[bytes]` (or `BinaryIO`) — file-like objects with read/write
- `TimeSpan` → `float` (seconds) or `datetime.timedelta` if duration arithmetic is needed
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `InvalidOperationException` → `RuntimeError` (or a domain-specific exception when the project defines one)
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `IReadOnlyList<T> / IList<T>` → Read-only: `Sequence[T]` (typing — covariant, accepts tuple/list/any sequence). Mutable: `list[T]` or `MutableSequence[T]`. Use `tuple[T, ...]` instead of Sequence when the field/return is a fixed snapshot.
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
| `Math.Min / Math.Max / Math.Abs` | `min(...)` / `max(...)` / `abs(...)` (builtins — no import needed) |
| `Math.Pow(a, b)` | `a ** b` (operator) — `math.pow` is float-only, the operator handles ints natively |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using System.IO;

namespace Daqifi.Core.Communication.Transport;

/// <summary>
/// HID transport implementation backed by HidSharp.
/// </summary>
public sealed class HidLibraryTransport : IHidTransport
{
    private static readonly TimeSpan DefaultReadTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan DefaultWriteTimeout = TimeSpan.FromSeconds(10);

    private readonly IHidPlatform _hidPlatform;
    private readonly SemaphoreSlim _connectionLock = new(1, 1);
    private readonly SemaphoreSlim _ioLock = new(1, 1);

    private IHidTransportDevice? _connectedDevice;
    private bool _disposed;
    private TimeSpan _readTimeout = DefaultReadTimeout;
    private TimeSpan _writeTimeout = DefaultWriteTimeout;

    /// <summary>
    /// Initializes a new transport instance backed by the default HidSharp adapter.
    /// </summary>
    public HidLibraryTransport()
        : this(new HidLibraryPlatform())
    {
    }

    internal HidLibraryTransport(IHidPlatform hidPlatform)
    {
        _hidPlatform = hidPlatform ?? throw new ArgumentNullException(nameof(hidPlatform));
    }

    /// <inheritdoc />
    public bool IsConnected => !_disposed && _connectedDevice?.IsConnected == true;

    /// <inheritdoc />
    public int? VendorId { get; private set; }

    /// <inheritdoc />
    public int? ProductId { get; private set; }

    /// <inheritdoc />
    public string? SerialNumber { get; private set; }

    /// <inheritdoc />
    public string? DevicePath { get; private set; }

    /// <inheritdoc />
    public TimeSpan ReadTimeout
    {
        get => _readTimeout;
        set
        {
            ValidateTimeout(value, nameof(value), "Read timeout must be greater than zero.");
            _readTimeout = value;
        }
    }

    /// <inheritdoc />
    public TimeSpan WriteTimeout
    {
        get => _writeTimeout;
        set
        {
            ValidateTimeout(value, nameof(value), "Write timeout must be greater than zero.");
            _writeTimeout = value;
        }
    }

    /// <inheritdoc />
    public async Task ConnectAsync(
        int vendorId,
        int productId,
        string? serialNumber = null,
        CancellationToken cancellationToken = default)
    {
        ThrowIfDisposed();
        ValidateUsbIdentifier(vendorId, nameof(vendorId));
        ValidateUsbIdentifier(productId, nameof(productId));

        cancellationToken.ThrowIfCancellationRequested();

        await _connectionLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (IsConnected)
            {
                return;
            }

            var serialFilter = NormalizeSerialFilter(serialNumber);
            var device = _hidPlatform.EnumerateDevices()
                .Where(candidate => candidate.VendorId == vendorId)
                .Where(candidate => candidate.ProductId == productId)
                .Where(candidate => serialFilter == null ||
                    string.Equals(candidate.SerialNumber, serialFilter, StringComparison.OrdinalIgnoreCase))
                .OrderBy(candidate => candidate.DevicePath, StringComparer.Ordinal)
                .FirstOrDefault();

            if (device == null)
            {
                var target = serialFilter == null
                    ? $"VID=0x{vendorId:X4}, PID=0x{productId:X4}"
                    : $"VID=0x{vendorId:X4}, PID=0x{productId:X4}, Serial={serialFilter}";

                throw new IOException($"No HID device found for {target}.");
            }

            try
            {
                device.Open();
            }
            catch (Exception ex)
            {
                throw new IOException("Failed to open HID device.", ex);
            }

            if (!device.IsConnected)
            {
                device.Close();
                throw new IOException("HID device did not report a connected state after open.");
            }

            _connectedDevice = device;
            VendorId = device.VendorId;
            ProductId = device.ProductId;
            SerialNumber = device.SerialNumber;
            DevicePath = device.DevicePath;
        }
        finally
        {
            _connectionLock.Release();
        }
    }

    /// <inheritdoc />
    public void Connect(int vendorId, int productId, string? serialNumber = null)
    {
        ConnectAsync(vendorId, productId, serialNumber, CancellationToken.None)
            .GetAwaiter()
            .GetResult();
    }

    /// <inheritdoc />
    public async Task WriteAsync(byte[] data, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(data);
        if (data.Length == 0)
        {
            throw new ArgumentException("Data cannot be empty.", nameof(data));
        }

        ThrowIfDisposed();
        cancellationToken.ThrowIfCancellationRequested();

        await _ioLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var device = GetConnectedDevice();
            var timeoutMs = ToTimeoutMilliseconds(WriteTimeout);
            var successful = await device.WriteAsync(data, timeoutMs)
                .WaitAsync(cancellationToken)
                .ConfigureAwait(false);

            if (!successful)
            {
                throw new IOException("HID write failed.");
            }
        }
        finally
        {
            _ioLock.Release();
        }
    }

    /// <inheritdoc />
    public void Write(byte[] data)
    {
        WriteAsync(data, CancellationToken.None)
            .GetAwaiter()
            .GetResult();
    }

    /// <inheritdoc />
    public async Task<byte[]> ReadAsync(TimeSpan? timeout = null, CancellationToken cancellationToken = default)
    {
        ThrowIfDisposed();
        cancellationToken.ThrowIfCancellationRequested();

        var effectiveTimeout = timeout ?? ReadTimeout;
        if (effectiveTimeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout), "Timeout must be greater than zero.");
        }

        await _ioLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var device = GetConnectedDevice();
            var timeoutMs = ToTimeoutMilliseconds(effectiveTimeout);
            var result = await device.ReadAsync(timeoutMs)
                .WaitAsync(cancellationToken)
                .ConfigureAwait(false);

            return result.Status switch
            {
                HidTransportReadStatus.Success => result.Data,
                HidTransportReadStatus.TimedOut => throw new TimeoutException(
                    $"Timed out waiting for HID data after {effectiveTimeout.TotalMilliseconds:F0} ms."),
                _ => throw new IOException(result.ErrorMessage ?? "HID read failed.")
            };
        }
        finally
        {
            _ioLock.Release();
        }
    }

    /// <inheritdoc />
    public byte[] Read(TimeSpan? timeout = null)
    {
        return ReadAsync(timeout, CancellationToken.None)
            .GetAwaiter()
            .GetResult();
    }

    /// <inheritdoc />
    public async Task DisconnectAsync()
    {
        if (_disposed)
        {
            return;
        }

        await _connectionLock.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_connectedDevice == null)
            {
                return;
            }

            Exception? closeException = null;
            await _ioLock.WaitAsync().ConfigureAwait(false);
            try
            {
                _connectedDevice.Close();
            }
            catch (Exception ex)
            {
                closeException = ex;
            }
            finally
            {
                _ioLock.Release();
            }

            ClearConnectionState();

            if (closeException != null)
            {
                throw new IOException("Failed to close HID device.", closeException);
            }
        }
        finally
        {
            _connectionLock.Release();
        }
    }

    /// <inheritdoc />
    public void Disconnect()
    {
        DisconnectAsync().GetAwaiter().GetResult();
    }

    /// <inheritdoc />
    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;

        var device = _connectedDevice;
        ClearConnectionState();

        try
        {
            device?.Close();
        }
        catch
        {
            // Ignore disconnection errors during disposal.
        }

        _connectionLock.Dispose();
        _ioLock.Dispose();
    }

    private static string? NormalizeSerialFilter(string? serialNumber)
    {
        if (string.IsNullOrWhiteSpace(serialNumber))
        {
            return null;
        }

        return serialNumber.Trim();
    }

    private static void ValidateUsbIdentifier(int identifier, string parameterName)
    {
        if (identifier < 0 || identifier > 0xFFFF)
        {
            throw new ArgumentOutOfRangeException(
                parameterName,
                identifier,
                "USB identifiers must be in the range 0..65535.");
        }
    }

    private static int ToTimeoutMilliseconds(TimeSpan timeout)
    {
        ValidateTimeout(timeout, nameof(timeout), "Timeout must be greater than zero.");

        if (timeout.TotalMilliseconds > int.MaxValue)
        {
            return int.MaxValue;
        }

        return (int)Math.Ceiling(timeout.TotalMilliseconds);
    }

    private static void ValidateTimeout(TimeSpan timeout, string parameterName, string message)
    {
        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(parameterName, message);
        }
    }

    private IHidTransportDevice GetConnectedDevice()
    {
        if (_connectedDevice == null || !_connectedDevice.IsConnected)
        {
            throw new InvalidOperationException("HID transport is not connected.");
        }

        return _connectedDevice;
    }

    private void ClearConnectionState()
    {
        _connectedDevice = null;
        VendorId = null;
        ProductId = null;
        SerialNumber = null;
        DevicePath = null;
    }

    private void ThrowIfDisposed()
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(HidLibraryTransport));
        }
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `IHidTransport` **[base contract]** (from `src/Daqifi.Core/Communication/Transport/IHidTransport.cs`)

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

### `IHidPlatform` (from `src/Daqifi.Core/Communication/Transport/HidLibraryPlatform.cs`)

```csharp
internal interface IHidPlatform
{
    IReadOnlyList<IHidTransportDevice> EnumerateDevices();
}
```

### `HidTransportReadStatus` (from `src/Daqifi.Core/Communication/Transport/HidLibraryPlatform.cs`)

```csharp
internal enum HidTransportReadStatus
{
    Success,
    TimedOut,
    Error
}
```

### `IHidTransportDevice` (from `src/Daqifi.Core/Communication/Transport/HidLibraryPlatform.cs`)

```csharp
internal interface IHidTransportDevice
{
    // Implementations are not thread-safe. Callers should synchronize access.
    int VendorId { get; }
    int ProductId { get; }
    string DevicePath { get; }
    string? SerialNumber { get; }
    string? ProductName { get; }
    bool IsConnected { get; }

    void Open();
    void Close();
    bool Write(byte[] data, int timeoutMs);
    Task<bool> WriteAsync(byte[] data, int timeoutMs);
    HidTransportReadResult Read(int timeoutMs);
    Task<HidTransportReadResult> ReadAsync(int timeoutMs);
}
```

## Downstream target (current contents)

_File does not exist yet at `daqifi/transport/hid_transport.py` — create it._

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
