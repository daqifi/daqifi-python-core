# portomatic apply prompt

**Plan:** `20260420T230004-port-hiddevicefinder-v12`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.12 test 4: HidDeviceFinder (IDeviceFinder + IDisposable, EventHandler<T>, semaphore-coordinated async)
- classification: new-feature
- shard: Discovery
- upstream_ref: e8d84af:src/Daqifi.Core/Device/Discovery/HidDeviceFinder.cs
- downstream_target: daqifi/discovery/hid_finder.py

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `event EventHandler<T>` → Single event: `Callable[[T], None]` callback slot OR `add_<event>`/`remove_<event>` if subscribers > 1. Multiple events on same interface: prefix add/remove with the event name (e.g. `add_message_received`, `remove_message_received`) and keep callback lists per-event. Mirror this pattern across all events on the same Protocol/class for consistency.
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `TimeSpan` → `float` (seconds) or `datetime.timedelta` if duration arithmetic is needed
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `EventArgs subclass` → `@dataclass` carrying the event payload (frozen if the event payload is read-only)
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.
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

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using Daqifi.Core.Communication.Transport;

namespace Daqifi.Core.Device.Discovery;

/// <summary>
/// Discovers DAQiFi devices in USB HID bootloader mode.
/// </summary>
public class HidDeviceFinder : IDeviceFinder, IDisposable
{
    /// <summary>
    /// Default DAQiFi bootloader vendor ID.
    /// </summary>
    public const int DefaultVendorId = 0x04D8;

    /// <summary>
    /// Default DAQiFi bootloader product ID.
    /// </summary>
    public const int DefaultProductId = 0x003C;

    private readonly IHidDeviceEnumerator _hidDeviceEnumerator;
    private readonly SemaphoreSlim _discoverySemaphore = new(1, 1);
    private bool _disposed;

    /// <summary>
    /// Initializes a new finder that filters to DAQiFi bootloader VID/PID by default.
    /// </summary>
    public HidDeviceFinder()
        : this(new HidLibraryDeviceEnumerator())
    {
    }

    internal HidDeviceFinder(IHidDeviceEnumerator hidDeviceEnumerator)
    {
        _hidDeviceEnumerator = hidDeviceEnumerator
            ?? throw new ArgumentNullException(nameof(hidDeviceEnumerator));

        VendorIdFilter = DefaultVendorId;
        ProductIdFilter = DefaultProductId;
    }

    /// <summary>
    /// Gets or sets the vendor ID filter used by <see cref="DiscoverAsync(CancellationToken)"/>.
    /// </summary>
    public int? VendorIdFilter { get; set; }

    /// <summary>
    /// Gets or sets the product ID filter used by <see cref="DiscoverAsync(CancellationToken)"/>.
    /// </summary>
    public int? ProductIdFilter { get; set; }

    /// <inheritdoc />
    public event EventHandler<DeviceDiscoveredEventArgs>? DeviceDiscovered;

    /// <inheritdoc />
    public event EventHandler? DiscoveryCompleted;

    /// <inheritdoc />
    public Task<IEnumerable<IDeviceInfo>> DiscoverAsync(CancellationToken cancellationToken = default)
    {
        return DiscoverAsync(VendorIdFilter, ProductIdFilter, cancellationToken);
    }

    /// <summary>
    /// Discovers HID devices asynchronously with explicit optional VID/PID filtering.
    /// </summary>
    /// <param name="vendorIdFilter">Optional vendor ID filter.</param>
    /// <param name="productIdFilter">Optional product ID filter.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>Discovered HID devices mapped to <see cref="IDeviceInfo"/>.</returns>
    public async Task<IEnumerable<IDeviceInfo>> DiscoverAsync(
        int? vendorIdFilter,
        int? productIdFilter,
        CancellationToken cancellationToken = default)
    {
        ThrowIfDisposed();

        await _discoverySemaphore.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var hidDevices = await _hidDeviceEnumerator
                .EnumerateAsync(vendorIdFilter, productIdFilter, cancellationToken)
                .ConfigureAwait(false);

            var discoveredDevices = new List<IDeviceInfo>(hidDevices.Count);

            foreach (var hidDevice in hidDevices)
            {
                var deviceInfo = new DeviceInfo
                {
                    Name = string.IsNullOrWhiteSpace(hidDevice.ProductName)
                        ? "DAQiFi Bootloader"
                        : hidDevice.ProductName,
                    SerialNumber = hidDevice.SerialNumber ?? string.Empty,
                    FirmwareVersion = string.Empty,
                    ConnectionType = ConnectionType.Hid,
                    Type = DeviceType.Unknown,
                    DevicePath = hidDevice.DevicePath
                };

                discoveredDevices.Add(deviceInfo);
                OnDeviceDiscovered(deviceInfo);
            }

            OnDiscoveryCompleted();
            return discoveredDevices;
        }
        finally
        {
            _discoverySemaphore.Release();
        }
    }

    /// <inheritdoc />
    public async Task<IEnumerable<IDeviceInfo>> DiscoverAsync(TimeSpan timeout)
    {
        using var cts = new CancellationTokenSource(timeout);
        return await DiscoverAsync(cts.Token).ConfigureAwait(false);
    }

    /// <summary>
    /// Raises the <see cref="DeviceDiscovered"/> event.
    /// </summary>
    /// <param name="deviceInfo">The discovered device metadata.</param>
    protected virtual void OnDeviceDiscovered(IDeviceInfo deviceInfo)
    {
        DeviceDiscovered?.Invoke(this, new DeviceDiscoveredEventArgs(deviceInfo));
    }

    /// <summary>
    /// Raises the <see cref="DiscoveryCompleted"/> event.
    /// </summary>
    protected virtual void OnDiscoveryCompleted()
    {
        DiscoveryCompleted?.Invoke(this, EventArgs.Empty);
    }

    private void ThrowIfDisposed()
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(HidDeviceFinder));
        }
    }

    /// <inheritdoc />
    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _discoverySemaphore.Dispose();
        _disposed = true;
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `IDeviceFinder` **[base contract]** (from `src/Daqifi.Core/Device/Discovery/IDeviceFinder.cs`)

```csharp
public interface IDeviceFinder
{
    /// <summary>
    /// Occurs when a device is discovered.
    /// </summary>
    event EventHandler<DeviceDiscoveredEventArgs>? DeviceDiscovered;

    /// <summary>
    /// Occurs when device discovery completes.
    /// </summary>
    event EventHandler? DiscoveryCompleted;

    /// <summary>
    /// Discovers devices asynchronously with a cancellation token.
    /// </summary>
    /// <param name="cancellationToken">Cancellation token to abort the operation.</param>
    /// <returns>A task containing the collection of discovered devices.</returns>
    Task<IEnumerable<IDeviceInfo>> DiscoverAsync(CancellationToken cancellationToken = default);

    /// <summary>
    /// Discovers devices asynchronously with a timeout.
    /// </summary>
    /// <param name="timeout">The timeout for discovery.</param>
    /// <returns>A task containing the collection of discovered devices.</returns>
    Task<IEnumerable<IDeviceInfo>> DiscoverAsync(TimeSpan timeout);
}
```

- **Existing downstream implementations** (follow this pattern):
  - `class DeviceFinder` at `daqifi/discovery.py:38` _(by name match)_
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

### `DeviceType` (from `src/Daqifi.Core/Device/Discovery/IDeviceInfo.cs`) **[already ported as `daqifi/_internal/comm.py:597`]**

```csharp
public enum DeviceType
{
    /// <summary>
    /// Unknown device type.
    /// </summary>
    Unknown,

    /// <summary>
    /// Nyquist 1 device.
    /// </summary>
    Nyquist1,

    /// <summary>
    /// Nyquist 3 device.
    /// </summary>
    Nyquist3
}
```

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

### `DeviceDiscoveredEventArgs` (from `src/Daqifi.Core/Device/Discovery/DeviceDiscoveredEventArgs.cs`)

```csharp
public class DeviceDiscoveredEventArgs : EventArgs
{
    /// <summary>
    /// Initializes a new instance of the DeviceDiscoveredEventArgs class.
    /// </summary>
    /// <param name="deviceInfo">The discovered device information.</param>
    public DeviceDiscoveredEventArgs(IDeviceInfo deviceInfo)
    {
        DeviceInfo = deviceInfo ?? throw new ArgumentNullException(nameof(deviceInfo));
    }

    /// <summary>
    /// Gets the discovered device information.
    /// </summary>
    public IDeviceInfo DeviceInfo { get; }
}
```

## Downstream target (current contents)

_File does not exist yet at `daqifi/discovery/hid_finder.py` — create it._

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
