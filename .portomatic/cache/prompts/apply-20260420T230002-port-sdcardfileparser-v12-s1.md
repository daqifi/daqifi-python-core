# portomatic apply prompt

**Plan:** `20260420T230002-port-sdcardfileparser-v12`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.12 test 2: SdCardFileParser (heavy generics, [EnumeratorCancellation], protobuf collaborators)
- classification: new-feature
- shard: SdCard
- upstream_ref: e8d84af:src/Daqifi.Core/Device/SdCard/SdCardFileParser.cs
- downstream_target: daqifi/sdcard/file_parser.py

## Port conventions (project-wide)

- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Stream` → `typing.IO[bytes]` (or `BinaryIO`) — file-like objects with read/write
- `DateTime / DateTimeOffset` → `datetime.datetime` (use `datetime.now(timezone.utc)` for UTC equivalent of `DateTime.UtcNow`)
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.
- `IReadOnlyList<T> / IList<T>` → Read-only: `Sequence[T]` (typing — covariant, accepts tuple/list/any sequence). Mutable: `list[T]` or `MutableSequence[T]`. Use `tuple[T, ...]` instead of Sequence when the field/return is a fixed snapshot.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.
- `IAsyncEnumerable<T> + yield return` → `async def foo(...) -> AsyncIterator[T]: yield ...` (import `AsyncIterator` from `typing` or `collections.abc`). Use `async for x in foo(): ...` at call sites. Note: `yield return` becomes plain `yield` in Python — no `return` keyword.
- `[EnumeratorCancellation] CancellationToken ct` → No syntactic Python equivalent. The async-iterator consumer cancels via the enclosing `asyncio.Task` (`task.cancel()` raises `asyncio.CancelledError` at the next `await` inside the iterator). For explicit cooperative cancellation, accept an `asyncio.Event` or `asyncio.CancelledError`-aware callable parameter and check it inside the loop. Don't transliterate the attribute or the `CancellationToken` type.
- `IProgress<T> progress` → `progress: Callable[[T], None] | None = None` (a plain callback). Call as `progress(value) if progress else None`. The .NET `IProgress<T>` abstraction adds nothing in Python — a callable is enough.

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
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Daqifi.Core.Communication.Consumers;
using Daqifi.Core.Communication.Messages;

namespace Daqifi.Core.Device.SdCard;

/// <summary>
/// Parses SD card log <c>.bin</c> files containing varint32-prefixed
/// <see cref="DaqifiOutMessage"/> protobuf payloads.
/// </summary>
public sealed class SdCardFileParser
{
    /// <summary>
    /// Marker bytes written at the end of a USB-transferred file.
    /// </summary>
    private static readonly byte[] EndOfFileMarker =
        Encoding.ASCII.GetBytes("__END_OF_FILE__");

    /// <summary>
    /// Parses an SD card log file from a <see cref="Stream"/>.
    /// </summary>
    /// <param name="fileStream">A readable stream containing the binary log data.</param>
    /// <param name="fileName">The file name (used for metadata and date extraction).</param>
    /// <param name="options">Optional parse options.</param>
    /// <param name="ct">Cancellation token.</param>
    /// <returns>An <see cref="SdCardLogSession"/> providing lazy access to sample data.</returns>
    public async Task<SdCardLogSession> ParseAsync(
        Stream fileStream,
        string fileName,
        SdCardParseOptions? options = null,
        CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(fileStream);
        ArgumentNullException.ThrowIfNull(fileName);

        options ??= new SdCardParseOptions();

        if (options.BufferSize <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(options), "BufferSize must be greater than zero.");
        }

        var fileCreatedDate = options.SessionStartTime
                              ?? SdCardFileListParser.TryParseDateFromLogFileName(fileName);

        // Read all messages from the stream.
        var allMessages = await ReadAllMessagesAsync(fileStream, options, ct).ConfigureAwait(false);

        SdCardDeviceConfiguration? config = null;
        var streamStartIndex = 0;

        // First, check if the first message is a dedicated status message.
        // Some firmware versions embed config fields (e.g., AnalogInPortNum/TimestampFreq)
        // inside stream messages, so we only treat the first message as status when it
        // has no stream payload.
        if (allMessages.Count > 0 && !HasStreamPayload(allMessages[0]))
        {
            config = ExtractDeviceConfiguration(allMessages[0]);
            streamStartIndex = 1;
        }

        // If no dedicated status message was found, or the status message had no TimestampFreq,
        // scan all messages for config fields. Device firmware often embeds config fields
        // (TimestampFreq, DeviceSn, etc.) in streaming data messages rather than writing
        // a separate status header.
        if (config == null || config.TimestampFrequency == 0)
        {
            var scannedConfig = ScanMessagesForConfiguration(allMessages);
            if (scannedConfig != null)
            {
                config = MergeConfigurations(config, scannedConfig);
            }
        }

        // Apply ConfigurationOverride as a fallback for any fields not found in the file.
        // This is useful when the device is connected during download — the device's live
        // status provides calibration, resolution, and port range values that may not be
        // embedded in the SD card log file.
        if (options.ConfigurationOverride != null)
        {
            config = MergeConfigurations(config, options.ConfigurationOverride);
        }

        var timestampFrequency = config?.TimestampFrequency ?? 0u;
        if (timestampFrequency == 0 && options.FallbackTimestampFrequency > 0)
        {
            timestampFrequency = options.FallbackTimestampFrequency;
        }

        var tickPeriod = timestampFrequency > 0
            ? 1.0 / timestampFrequency
            : 0.0;

        var samples = ProduceSamples(
            allMessages,
            streamStartIndex,
            fileCreatedDate,
            tickPeriod,
            config,
            ct);

        return new SdCardLogSession(fileName, fileCreatedDate, config, samples);
    }

    /// <summary>
    /// Convenience overload that opens a file by path.
    /// </summary>
    /// <param name="filePath">Absolute or relative path to the <c>.bin</c> file.</param>
    /// <param name="options">Optional parse options.</param>
    /// <param name="ct">Cancellation token.</param>
    /// <returns>An <see cref="SdCardLogSession"/> providing lazy access to sample data.</returns>
    public async Task<SdCardLogSession> ParseFileAsync(
        string filePath,
        SdCardParseOptions? options = null,
        CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(filePath);

        options ??= new SdCardParseOptions();

        if (options.BufferSize <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(options), "BufferSize must be greater than zero.");
        }

        await using var stream = new FileStream(
            filePath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            options.BufferSize,
            useAsync: true);

        return await ParseAsync(stream, Path.GetFileName(filePath), options, ct).ConfigureAwait(false);
    }

    /// <summary>
    /// Reads and deserializes all protobuf messages from the stream.
    /// </summary>
    private static async Task<List<DaqifiOutMessage>> ReadAllMessagesAsync(
        Stream stream,
        SdCardParseOptions options,
        CancellationToken ct)
    {
        var parser = new ProtobufMessageParser();
        var messages = new List<DaqifiOutMessage>();
        var buffer = new byte[options.BufferSize];
        var carry = Array.Empty<byte>();
        long totalBytesRead = 0;
        var totalBytes = stream.CanSeek ? stream.Length : -1L;

        while (true)
        {
            ct.ThrowIfCancellationRequested();

            var bytesRead = await stream.ReadAsync(buffer, 0, buffer.Length, ct).ConfigureAwait(false);
            if (bytesRead == 0)
            {
                break;
            }

            totalBytesRead += bytesRead;

            // Combine carry-over bytes from previous chunk with new data
            var chunk = CombineBuffers(carry, buffer, bytesRead);

            // Strip end-of-file marker if present at the tail
            chunk = StripEndOfFileMarker(chunk);

            var parsed = parser.ParseMessages(chunk, out var consumed);

            foreach (var msg in parsed)
            {
                messages.Add(msg.Data);
            }

            // Carry unconsumed bytes forward
            if (consumed < chunk.Length)
            {
                carry = new byte[chunk.Length - consumed];
                Array.Copy(chunk, consumed, carry, 0, carry.Length);
            }
            else
            {
                carry = Array.Empty<byte>();
            }

            options.Progress?.Report(new SdCardParseProgress(totalBytesRead, totalBytes, messages.Count));
        }

        // Try to parse any remaining carry bytes (may contain a partial final message)
        if (carry.Length > 0)
        {
            var parsed = parser.ParseMessages(carry, out _);
            foreach (var msg in parsed)
            {
                messages.Add(msg.Data);
            }

            options.Progress?.Report(new SdCardParseProgress(totalBytesRead, totalBytes, messages.Count));
        }

        return messages;
    }

    /// <summary>
    /// Produces <see cref="SdCardLogEntry"/> samples from the parsed messages.
    /// </summary>
    private static async IAsyncEnumerable<SdCardLogEntry> ProduceSamples(
        List<DaqifiOutMessage> messages,
        int startIndex,
        DateTime? anchorTime,
        double tickPeriod,
        SdCardDeviceConfiguration? config,
        [EnumeratorCancellation] CancellationToken ct = default)
    {
        var baseTime = anchorTime ?? DateTime.UtcNow;
        uint? previousTimestamp = null;
        var elapsedSeconds = 0.0;

        for (var i = startIndex; i < messages.Count; i++)
        {
            ct.ThrowIfCancellationRequested();

            var msg = messages[i];

            // Skip non-stream messages (no analog and no digital data)
            if (msg.AnalogInData.Count == 0 &&
                msg.AnalogInDataFloat.Count == 0 &&
                msg.DigitalData.Length == 0)
            {
                continue;
            }

            // Merge consecutive messages at the same timestamp into a single entry.
            // Device firmware often sends separate analog and digital messages per sample
            // period with the same MsgTimeStamp.
            while (i + 1 < messages.Count && messages[i + 1].MsgTimeStamp == msg.MsgTimeStamp)
            {
                var next = messages[i + 1];
                if (!HasStreamPayload(next))
                {
                    break;
                }

                i++;

                // Take analog data from whichever message has it
                if (msg.AnalogInDataFloat.Count == 0 && next.AnalogInDataFloat.Count > 0)
                {
                    msg.AnalogInDataFloat.AddRange(next.AnalogInDataFloat);
                }
                else if (msg.AnalogInData.Count == 0 && next.AnalogInData.Count > 0)
                {
                    msg.AnalogInData.AddRange(next.AnalogInData);
                }

                // Take digital data from whichever message has it
                if (msg.DigitalData.Length == 0 && next.DigitalData.Length > 0)
                {
                    msg.DigitalData = next.DigitalData;
                }

                // Take per-channel timestamps from whichever message has them
                if (msg.AnalogInDataTs.Count == 0 && next.AnalogInDataTs.Count > 0)
                {
                    msg.AnalogInDataTs.AddRange(next.AnalogInDataTs);
                }
            }

            // Reconstruct timestamp
            var timestamp = baseTime;
            if (msg.MsgTimeStamp != 0 && tickPeriod > 0)
            {
                if (previousTimestamp == null)
                {
                    // First stream message — anchor to base time
                    previousTimestamp = msg.MsgTimeStamp;
                }
                else
                {
                    var delta = ComputeTickDelta(previousTimestamp.Value, msg.MsgTimeStamp);
                    elapsedSeconds += delta * tickPeriod;
                    previousTimestamp = msg.MsgTimeStamp;
                }

                timestamp = baseTime.AddSeconds(elapsedSeconds);
            }

            // Extract analog values (prefer float, fall back to scaled raw int)
            double[] analogValues;
            if (msg.AnalogInDataFloat.Count > 0)
            {
                analogValues = msg.AnalogInDataFloat.Select(v => (double)v).ToArray();
            }
            else if (msg.AnalogInData.Count > 0)
            {
                analogValues = ScaleRawAnalogValues(msg.AnalogInData, config);
            }
            else
            {
                analogValues = Array.Empty<double>();
            }

            // Extract digital data
            var digitalData = 0u;
            if (msg.DigitalData.Length > 0)
            {
                var bytes = msg.DigitalData.ToByteArray();
                for (var b = 0; b < bytes.Length && b < 4; b++)
                {
                    digitalData |= (uint)bytes[b] << (b * 8);
                }
            }

            // Per-channel timestamps
            IReadOnlyList<uint>? analogTimestamps = msg.AnalogInDataTs.Count > 0
                ? msg.AnalogInDataTs.ToArray()
                : null;
// ... [TRUNCATED: 10904 more chars (~283 lines) — see src/Daqifi.Core/Device/SdCard/SdCardFileParser.cs for the rest]
```

## ⚠ Large upstream — consider decomposing this port

`src/Daqifi.Core/Device/SdCard/SdCardFileParser.cs` is 610 lines with 12 top-level methods. A single plan step covering the whole file is rarely tractable. Consider splitting into separate plan steps using `target_symbol` to scope each step to one method or method group.

Top-level methods detected:
- `ParseAsync`
- `ParseFileAsync`
- `ReadAllMessagesAsync`
- `ProduceSamples`
- `ScaleRawAnalogValues`
- `ComputeTickDelta`
- `HasStreamPayload`
- `ScanMessagesForConfiguration`
- `MergeConfigurations`
- `ExtractDeviceConfiguration`
- `CombineBuffers`
- `StripEndOfFileMarker`

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `SdCardDeviceConfiguration` (from `src/Daqifi.Core/Device/SdCard/SdCardDeviceConfiguration.cs`)

```csharp
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

### `SdCardParseOptions` (from `src/Daqifi.Core/Device/SdCard/SdCardParseOptions.cs`)

```csharp
public sealed class SdCardParseOptions
{
    /// <summary>
    /// Gets or sets the session start time override.
    /// When set, this is used as the timestamp anchor instead of the filename-derived date.
    /// </summary>
    public DateTime? SessionStartTime { get; set; }

    /// <summary>
    /// Gets or sets the read buffer size in bytes. Default is 64 KB.
    /// </summary>
    public int BufferSize { get; set; } = 64 * 1024;

    /// <summary>
    /// Gets or sets an optional progress reporter.
    /// </summary>
    public IProgress<SdCardParseProgress>? Progress { get; set; }

    /// <summary>
    /// Gets or sets a fallback timestamp frequency (in Hz) to use when no
    /// <c>TimestampFreq</c> field is found in the file's protobuf messages.
    /// <para>
    /// Device firmware may not include <c>TimestampFreq</c> in SD card log data.
    /// When this fallback is set and the file contains no timestamp frequency,
    /// it will be used to convert raw tick deltas to elapsed time.
    /// </para>
    /// <para>
    /// This value is only used as a fallback — if the file contains a valid
    /// <c>TimestampFreq</c>, it takes precedence.
    /// </para>
    /// <para>
    /// Defaults to 50 MHz (the Nyquist device clock frequency). Set to 0 to
    /// disable the fallback entirely.
    /// </para>
    /// </summary>
    public uint FallbackTimestampFrequency { get; set; } = 50_000_000;

    /// <summary>
    /// Gets or sets a device configuration override. When set, fields from this
    /// config fill in any gaps not found in the file itself.
    /// <para>
    /// This is useful when the device is connected during download — the device's
    /// live status provides calibration, resolution, and port range values that
    /// may not be embedded in the SD card log file.
    /// </para>
    /// </summary>
    public SdCardDeviceConfiguration? ConfigurationOverride { get; set; }
}
```

### `SdCardLogSession` (from `src/Daqifi.Core/Device/SdCard/SdCardLogSession.cs`)

```csharp
public sealed class SdCardLogSession
{
    /// <summary>
    /// Gets the source file name.
    /// </summary>
    public string FileName { get; }

    /// <summary>
    /// Gets the file creation date, parsed from the filename pattern <c>log_YYYYMMDD_HHMMSS.bin</c>
    /// or provided via parse options.
    /// </summary>
    public DateTime? FileCreatedDate { get; }

    /// <summary>
    /// Gets the device configuration extracted from the first status message, if present.
    /// </summary>
    public SdCardDeviceConfiguration? DeviceConfig { get; }

    /// <summary>
    /// Gets streaming access to sample data. Samples are produced lazily as the file is read.
    /// </summary>
    public IAsyncEnumerable<SdCardLogEntry> Samples { get; }

    /// <summary>
    /// Initializes a new instance of the <see cref="SdCardLogSession"/> class.
    /// </summary>
    /// <param name="fileName">The source file name.</param>
    /// <param name="fileCreatedDate">The file creation date.</param>
    /// <param name="deviceConfig">Device configuration from status message.</param>
    /// <param name="samples">Async enumerable of parsed sample entries.</param>
    public SdCardLogSession(
        string fileName,
        DateTime? fileCreatedDate,
        SdCardDeviceConfiguration? deviceConfig,
        IAsyncEnumerable<SdCardLogEntry> samples)
    {
        FileName = fileName ?? throw new ArgumentNullException(nameof(fileName));
        Samples = samples ?? throw new ArgumentNullException(nameof(samples));
        FileCreatedDate = fileCreatedDate;
        DeviceConfig = deviceConfig;
    }
}
```

### `SdCardLogEntry` (from `src/Daqifi.Core/Device/SdCard/SdCardLogEntry.cs`)

```csharp
public sealed record SdCardLogEntry(
    DateTime Timestamp,
    IReadOnlyList<double> AnalogValues,
    uint DigitalData,
    IReadOnlyList<uint>? AnalogTimestamps);
```

### `SdCardParseProgress` (from `src/Daqifi.Core/Device/SdCard/SdCardParseProgress.cs`)

```csharp
public sealed record SdCardParseProgress(long BytesRead, long TotalBytes, int MessagesRead);
```

## Downstream target (current contents)

_File does not exist yet at `daqifi/sdcard/file_parser.py` — create it._

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
