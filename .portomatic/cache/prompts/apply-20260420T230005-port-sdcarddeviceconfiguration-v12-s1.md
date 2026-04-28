# portomatic apply prompt

**Plan:** `20260420T230005-port-sdcarddeviceconfiguration-v12`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.12 test 5: SdCardDeviceConfiguration (sealed record, IReadOnlyList<(double,double)>, FromDevice<T> factory)
- classification: new-feature
- shard: SdCard
- upstream_ref: e8d84af:src/Daqifi.Core/Device/SdCard/SdCardDeviceConfiguration.cs
- downstream_target: daqifi/sdcard/device_configuration.py

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
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
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.
- `IReadOnlyList<T> / IList<T>` → Read-only: `Sequence[T]` (typing — covariant, accepts tuple/list/any sequence). Mutable: `list[T]` or `MutableSequence[T]`. Use `tuple[T, ...]` instead of Sequence when the field/return is a fixed snapshot.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.
- `[EnumeratorCancellation] CancellationToken ct` → No syntactic Python equivalent. The async-iterator consumer cancels via the enclosing `asyncio.Task` (`task.cancel()` raises `asyncio.CancelledError` at the next `await` inside the iterator). For explicit cooperative cancellation, accept an `asyncio.Event` or `asyncio.CancelledError`-aware callable parameter and check it inside the loop. Don't transliterate the attribute or the `CancellationToken` type.
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
| `Convert.ToInt32 / ToDouble / ToBoolean` | For non-null inputs: `int(...)` / `float(...)` / `bool(...)`. Caveats: `bool('false')` is `True` in Python (any non-empty string is truthy — use `s.lower() == 'true'`); and `Convert.ToInt32(null)` returns `0` in C# while `int(None)` raises `TypeError` — use `0 if x is None else int(x)` when the upstream value can be null. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using System.Collections.Generic;
using System.Linq;
using Daqifi.Core.Channel;

namespace Daqifi.Core.Device.SdCard;

/// <summary>
/// Device context extracted from a status message in an SD card log file.
/// </summary>
/// <param name="AnalogPortCount">Number of analog input ports.</param>
/// <param name="DigitalPortCount">Number of digital ports.</param>
/// <param name="TimestampFrequency">Clock frequency for tick-to-time conversion.</param>
/// <param name="DeviceSerialNumber">Device serial number, if present.</param>
/// <param name="DevicePartNumber">Device part number, if present.</param>
/// <param name="FirmwareRevision">Firmware revision string, if present.</param>
/// <param name="CalibrationValues">Per-channel calibration slope/intercept pairs, if present.</param>
/// <param name="Resolution">ADC resolution (e.g., 65535 for 16-bit), used for raw ADC scaling.</param>
/// <param name="PortRange">Per-channel voltage port range values, if present.</param>
/// <param name="InternalScaleM">Per-channel internal scale factors, if present.</param>
public sealed record SdCardDeviceConfiguration(
    int AnalogPortCount,
    int DigitalPortCount,
    uint TimestampFrequency,
    string? DeviceSerialNumber,
    string? DevicePartNumber,
    string? FirmwareRevision,
    IReadOnlyList<(double Slope, double Intercept)>? CalibrationValues,
    uint Resolution = 0,
    IReadOnlyList<double>? PortRange = null,
    IReadOnlyList<double>? InternalScaleM = null)
{
    /// <summary>
    /// Creates an <see cref="SdCardDeviceConfiguration"/> from a connected device's
    /// channel configuration. This captures the calibration, resolution, port range,
    /// and internal scale values needed to convert raw ADC data in SD card log files.
    /// </summary>
    /// <param name="device">A connected and initialized device.</param>
    /// <returns>A configuration snapshot, or <c>null</c> if the device has no analog channels.</returns>
    public static SdCardDeviceConfiguration? FromDevice(DaqifiDevice device)
    {
        var analogChannels = device.Channels.OfType<IAnalogChannel>().ToList();
        if (analogChannels.Count == 0)
        {
            return null;
        }

        var digitalCount = device.Channels.Count(c => c.Type == ChannelType.Digital);
        var firstAnalog = analogChannels[0];

        return new SdCardDeviceConfiguration(
            AnalogPortCount: analogChannels.Count,
            DigitalPortCount: digitalCount,
            TimestampFrequency: 0, // Let the parser use file-embedded or fallback frequency
            DeviceSerialNumber: device.Metadata.SerialNumber,
            DevicePartNumber: device.Metadata.PartNumber,
            FirmwareRevision: device.Metadata.FirmwareVersion,
            CalibrationValues: analogChannels
                .Select(ch => (ch.CalibrationM, ch.CalibrationB))
                .ToArray(),
            Resolution: firstAnalog.Resolution,
            PortRange: analogChannels.Select(ch => ch.PortRange).ToArray(),
            InternalScaleM: analogChannels.Select(ch => ch.InternalScaleM).ToArray());
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `ChannelType` (from `src/Daqifi.Core/Channel/ChannelType.cs`) **[already ported as `daqifi/_internal/scpi.py:11`]**

```csharp
public enum ChannelType
{
    /// <summary>
    /// Digital channel (binary on/off).
    /// </summary>
    Digital,

    /// <summary>
    /// Analog channel (continuous value).
    /// </summary>
    Analog
}
```

### `IAnalogChannel` (from `src/Daqifi.Core/Channel/IAnalogChannel.cs`)

```csharp
public interface IAnalogChannel : IChannel
{
    /// <summary>
    /// Gets the minimum value for the channel range.
    /// </summary>
    double MinValue { get; set; }

    /// <summary>
    /// Gets the maximum value for the channel range.
    /// </summary>
    double MaxValue { get; set; }

    /// <summary>
    /// Gets the resolution of the ADC (e.g., 65535 for 16-bit).
    /// </summary>
    uint Resolution { get; }

    /// <summary>
    /// Gets or sets the calibration slope (M in the scaling formula).
    /// </summary>
    double CalibrationM { get; set; }

    /// <summary>
    /// Gets or sets the calibration offset (B in the scaling formula).
    /// </summary>
    double CalibrationB { get; set; }

    /// <summary>
    /// Gets or sets the internal scale factor.
    /// </summary>
    double InternalScaleM { get; set; }

    /// <summary>
    /// Gets or sets the port range (voltage range).
    /// </summary>
    double PortRange { get; set; }

    /// <summary>
    /// Converts a raw ADC value to a scaled value using the calibration parameters.
    /// Formula: ScaledValue = (RawValue / Resolution * PortRange * CalibrationM + CalibrationB) * InternalScaleM
    /// </summary>
    /// <param name="rawValue">The raw ADC value from the device.</param>
    /// <returns>The scaled value.</returns>
    double GetScaledValue(int rawValue);
}
```

## Downstream target (current contents)

_File does not exist yet at `daqifi/sdcard/device_configuration.py` — create it._

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
