# portomatic apply prompt

**Plan:** `20260421T050003-port-timestampprocessor-v13`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.13 test 3: TimestampProcessor (sealed, generic interface impl, uint32 rollover detection, per-device state)
- classification: new-feature
- shard: Device
- upstream_ref: e8d84af:src/Daqifi.Core/Device/TimestampProcessor.cs
- downstream_target: daqifi/device/timestamp_processor.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `DateTime / DateTimeOffset` → `datetime.datetime` (use `datetime.now(timezone.utc)` for UTC equivalent of `DateTime.UtcNow`)
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.

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
using System.Collections.Concurrent;

namespace Daqifi.Core.Device;

/// <summary>
/// Processes device timestamps and handles uint32 rollover scenarios.
/// This class maintains per-device state to correctly calculate system timestamps
/// from device clock cycles during streaming.
/// </summary>
/// <remarks>
/// <para>
/// Device timestamps are 32-bit unsigned integers representing clock cycles.
/// At 50MHz (20ns tick period), the counter rolls over approximately every 85.9 seconds.
/// This processor detects rollover and calculates accurate elapsed time.
/// </para>
/// <para>
/// A 10-second sanity check is applied to detected rollovers. If the calculated
/// time between messages exceeds 10 seconds after rollover correction, the rollover
/// is considered a false positive (likely caused by out-of-order messages).
/// </para>
/// <para>
/// This class is thread-safe and can be used with multiple devices simultaneously.
/// </para>
/// </remarks>
public sealed class TimestampProcessor : ITimestampProcessor
{
    /// <summary>
    /// Default tick period in seconds (20 nanoseconds = 20E-9 seconds).
    /// This corresponds to a 50MHz clock.
    /// </summary>
    public const double DefaultTickPeriod = 20E-9;

    /// <summary>
    /// Maximum time in seconds between messages before a rollover is considered invalid.
    /// If a detected rollover would result in more than this time between messages,
    /// the rollover is treated as a false positive.
    /// </summary>
    private const double MaxRolloverTimeBetweenMessages = 10.0;

    private readonly ConcurrentDictionary<string, DeviceTimestampState> _deviceStates = new();

    /// <inheritdoc />
    public double TickPeriod { get; }

    /// <summary>
    /// Initializes a new instance of the <see cref="TimestampProcessor"/> class
    /// with the default tick period of 20 nanoseconds.
    /// </summary>
    public TimestampProcessor() : this(DefaultTickPeriod)
    {
    }

    /// <summary>
    /// Initializes a new instance of the <see cref="TimestampProcessor"/> class
    /// with a custom tick period.
    /// </summary>
    /// <param name="tickPeriod">The tick period in seconds.</param>
    /// <exception cref="ArgumentOutOfRangeException">
    /// Thrown when <paramref name="tickPeriod"/> is less than or equal to zero.
    /// </exception>
    public TimestampProcessor(double tickPeriod)
    {
        if (tickPeriod <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(tickPeriod), "Tick period must be greater than zero.");
        }

        TickPeriod = tickPeriod;
    }

    /// <inheritdoc />
    public TimestampResult ProcessTimestamp(string deviceId, uint deviceTimestamp)
    {
        ArgumentNullException.ThrowIfNull(deviceId);

        var state = _deviceStates.GetOrAdd(deviceId, _ => new DeviceTimestampState());

        lock (state.SyncLock)
        {
            // First message for this device
            if (!state.HasPreviousTimestamp)
            {
                var now = DateTime.Now;
                state.PreviousSystemTimestamp = now;
                state.PreviousDeviceTimestamp = deviceTimestamp;
                state.HasPreviousTimestamp = true;

                return TimestampResult.CreateFirstMessage(now, deviceTimestamp);
            }

            // Calculate clock cycles between messages, handling rollover
            var previousDeviceTimestamp = state.PreviousDeviceTimestamp;
            var rollover = previousDeviceTimestamp > deviceTimestamp;
            uint clockCyclesBetweenMessages;

            if (rollover)
            {
                // Rollover detected: timestamp wrapped from uint.MaxValue to 0
                var cyclesToMax = uint.MaxValue - previousDeviceTimestamp;
                clockCyclesBetweenMessages = cyclesToMax + deviceTimestamp;
            }
            else
            {
                clockCyclesBetweenMessages = deviceTimestamp - previousDeviceTimestamp;
            }

            var secondsBetweenMessages = clockCyclesBetweenMessages * TickPeriod;

            // Apply sanity check for false positive rollover detection
            // If we detected rollover but the time between messages is > 10 seconds,
            // it's likely a false positive (e.g., out-of-order messages)
            if (rollover && secondsBetweenMessages > MaxRolloverTimeBetweenMessages)
            {
                // Recalculate as if no rollover occurred (going backwards in time)
                clockCyclesBetweenMessages = previousDeviceTimestamp - deviceTimestamp;
                secondsBetweenMessages = clockCyclesBetweenMessages * TickPeriod * -1;
            }

            var messageTimestamp = state.PreviousSystemTimestamp.AddSeconds(secondsBetweenMessages);

            // Update state for next message
            state.PreviousSystemTimestamp = messageTimestamp;
            state.PreviousDeviceTimestamp = deviceTimestamp;

            return new TimestampResult(
                timestamp: messageTimestamp,
                wasRollover: rollover,
                clockCyclesBetweenMessages: clockCyclesBetweenMessages,
                secondsBetweenMessages: secondsBetweenMessages,
                isFirstMessage: false);
        }
    }

    /// <inheritdoc />
    public void Reset(string deviceId)
    {
        ArgumentNullException.ThrowIfNull(deviceId);
        _deviceStates.TryRemove(deviceId, out _);
    }

    /// <inheritdoc />
    public void ResetAll()
    {
        _deviceStates.Clear();
    }

    /// <summary>
    /// Internal state tracking for a single device.
    /// </summary>
    private sealed class DeviceTimestampState
    {
        public readonly object SyncLock = new();
        public DateTime PreviousSystemTimestamp;
        public uint PreviousDeviceTimestamp;
        public bool HasPreviousTimestamp;
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `ITimestampProcessor` **[base contract]** (from `src/Daqifi.Core/Device/ITimestampProcessor.cs`)

```csharp
public interface ITimestampProcessor
{
    /// <summary>
    /// Gets the tick period in seconds. The default is 20 nanoseconds (20E-9).
    /// </summary>
    double TickPeriod { get; }

    /// <summary>
    /// Processes a device timestamp and returns the calculated system timestamp.
    /// Handles uint32 rollover detection with a 10-second sanity check to avoid
    /// false positive rollover detection.
    /// </summary>
    /// <param name="deviceId">Unique identifier for the device (e.g., serial number).</param>
    /// <param name="deviceTimestamp">The raw device timestamp in clock cycles (uint32).</param>
    /// <returns>A <see cref="TimestampResult"/> containing the calculated system timestamp and metadata.</returns>
    TimestampResult ProcessTimestamp(string deviceId, uint deviceTimestamp);

    /// <summary>
    /// Resets the timestamp state for a specific device.
    /// Call this when starting a new streaming session.
    /// </summary>
    /// <param name="deviceId">Unique identifier for the device to reset.</param>
    void Reset(string deviceId);

    /// <summary>
    /// Resets all timestamp state for all devices.
    /// Call this when the processor should be completely cleared.
    /// </summary>
    void ResetAll();
}
```

**Other referenced types** (upstream definitions are large or unfindable AND no downstream port exists yet — investigate before porting if the call sites use them):
- `GetOrAdd` _(upstream definition not found)_
- `TickPeriod` _(upstream definition not found)_
- `TimestampResult` → `src/Daqifi.Core/Device/TimestampResult.cs`
- `ThrowIfNull` _(upstream definition not found)_
- `ConcurrentDictionary` _(upstream definition not found)_
- `ProcessTimestamp` _(upstream definition not found)_
- `Tick` _(upstream definition not found)_

## Downstream target (current contents)

_File does not exist yet at `daqifi/device/timestamp_processor.py` — create it._

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
