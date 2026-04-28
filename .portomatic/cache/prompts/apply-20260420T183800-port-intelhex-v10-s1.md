# portomatic apply prompt

**Plan:** `20260420T183800-port-intelhex-v10`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: #33: Port IntelHexParser — Intel HEX format parser
- classification: new-feature
- shard: Firmware
- upstream_ref: e8d84af:src/Daqifi.Core/Firmware/IntelHexParser.cs
- downstream_target: daqifi/_internal/firmware.py

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
using System.Globalization;

namespace Daqifi.Core.Firmware;

/// <summary>
/// Parses Intel HEX format files into structured records, with support for
/// memory protection ranges to skip calibration data regions.
/// </summary>
public class IntelHexParser
{
    /// <summary>
    /// Default protected memory range start address (calibration data).
    /// </summary>
    public const uint DEFAULT_BEGIN_PROTECTED_ADDRESS = 0x1D1E0000;

    /// <summary>
    /// Default protected memory range end address (calibration data).
    /// </summary>
    public const uint DEFAULT_END_PROTECTED_ADDRESS = 0x1D200000;

    private readonly uint _beginProtectedAddress;
    private readonly uint _endProtectedAddress;

    /// <summary>
    /// Creates a new Intel HEX parser with the default protected memory range.
    /// </summary>
    public IntelHexParser()
        : this(DEFAULT_BEGIN_PROTECTED_ADDRESS, DEFAULT_END_PROTECTED_ADDRESS)
    {
    }

    /// <summary>
    /// Creates a new Intel HEX parser with a custom protected memory range.
    /// </summary>
    /// <param name="beginProtectedAddress">Start of the protected memory range.</param>
    /// <param name="endProtectedAddress">End of the protected memory range.</param>
    public IntelHexParser(uint beginProtectedAddress, uint endProtectedAddress)
    {
        _beginProtectedAddress = beginProtectedAddress;
        _endProtectedAddress = endProtectedAddress;
    }

    /// <summary>
    /// Parses Intel HEX formatted lines into a list of raw hex record byte arrays,
    /// filtering out records in the protected memory range.
    /// </summary>
    /// <param name="lines">The lines from the HEX file (each starting with ':').</param>
    /// <returns>A list of byte arrays, each representing one hex record.</returns>
    /// <exception cref="ArgumentNullException">Thrown when <paramref name="lines"/> is null.</exception>
    /// <exception cref="InvalidDataException">Thrown when a line is malformed or has an invalid checksum.</exception>
    public List<byte[]> ParseHexRecords(string[] lines)
    {
        ArgumentNullException.ThrowIfNull(lines);

        ushort baseAddress = 0;
        var hexRecords = new List<byte[]>();

        foreach (var line in lines)
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            ValidateLine(line);

            var hexLine = ConvertLineToBytes(line);
            ValidateChecksum(hexLine, line);

            baseAddress = UpdateBaseAddress(hexLine, baseAddress);

            if (IsProtectedHexRecord(hexLine, baseAddress))
            {
                continue;
            }

            hexRecords.Add(hexLine);
        }

        return hexRecords;
    }

    /// <summary>
    /// Parses Intel HEX formatted lines into structured records with full addresses.
    /// Records in the protected memory range are filtered out.
    /// </summary>
    /// <param name="lines">The lines from the HEX file (each starting with ':').</param>
    /// <returns>A list of <see cref="HexFileRecord"/> with computed addresses.</returns>
    /// <exception cref="ArgumentNullException">Thrown when <paramref name="lines"/> is null.</exception>
    /// <exception cref="InvalidDataException">Thrown when a line is malformed or has an invalid checksum.</exception>
    public List<HexFileRecord> ParseRecords(string[] lines)
    {
        ArgumentNullException.ThrowIfNull(lines);

        ushort baseAddress = 0;
        var records = new List<HexFileRecord>();

        foreach (var line in lines)
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            ValidateLine(line);

            var hexLine = ConvertLineToBytes(line);
            ValidateChecksum(hexLine, line);
            var recordType = hexLine[3];

            baseAddress = UpdateBaseAddress(hexLine, baseAddress);

            if (IsProtectedHexRecord(hexLine, baseAddress))
            {
                continue;
            }

            var offsetAddressArray = hexLine.Skip(1).Take(2).ToArray();
            if (BitConverter.IsLittleEndian) Array.Reverse(offsetAddressArray);
            var offsetAddress = BitConverter.ToUInt16(offsetAddressArray, 0);
            var fullAddress = ((uint)baseAddress << 16) | offsetAddress;

            records.Add(new HexFileRecord(fullAddress, hexLine, recordType));
        }

        return records;
    }

    private static void ValidateLine(string line)
    {
        if (line[0] != ':')
        {
            throw new InvalidDataException(
                $"The hex record \"{line}\" doesn't start with the colon character \":\"");
        }

        if (line.Length % 2 != 1)
        {
            throw new InvalidDataException(
                $"The hex record \"{line}\" doesn't contain an odd number of characters");
        }

        if (line.Length < 11)
        {
            throw new InvalidDataException(
                $"The hex record \"{line}\" is too short to be a valid record");
        }
    }

    private static void ValidateChecksum(byte[] record, string line)
    {
        byte sum = 0;
        foreach (var b in record)
        {
            sum += b;
        }

        if (sum != 0)
        {
            throw new InvalidDataException(
                $"The hex record \"{line}\" has an invalid checksum");
        }
    }

    private static byte[] ConvertLineToBytes(string line)
    {
        var hexLine = new byte[(line.Length - 1) / 2];

        for (var i = 1; i < line.Length; i += 2)
        {
            var hex = line.Substring(i, 2);
            try
            {
                hexLine[(i - 1) / 2] = byte.Parse(hex, NumberStyles.HexNumber);
            }
            catch (FormatException ex)
            {
                throw new InvalidDataException(
                    $"The hex record \"{line}\" contains invalid hex characters \"{hex}\"", ex);
            }
        }

        return hexLine;
    }

    private static ushort UpdateBaseAddress(byte[] hexRecord, ushort currentBaseAddress)
    {
        var recordType = hexRecord[3];
        if (recordType == 0x04)
        {
            var dataArray = hexRecord.Skip(4).Take(hexRecord.Length - 5).ToArray();
            if (BitConverter.IsLittleEndian) Array.Reverse(dataArray);
            return BitConverter.ToUInt16(dataArray, 0);
        }
        return currentBaseAddress;
    }

    private bool IsProtectedHexRecord(byte[] hexRecord, ushort baseAddress)
    {
        var recordType = hexRecord[3];

        if (recordType == 0x00)
        {
            var offsetAddressArray = hexRecord.Skip(1).Take(2).ToArray();
            if (BitConverter.IsLittleEndian) Array.Reverse(offsetAddressArray);
            var offsetAddress = BitConverter.ToUInt16(offsetAddressArray, 0);
            var hexRecordAddress = ((uint)baseAddress << 16) | offsetAddress;

            if (hexRecordAddress >= _beginProtectedAddress && hexRecordAddress <= _endProtectedAddress)
            {
                return true;
            }
        }

        return false;
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `HexFileRecord` (from `src/Daqifi.Core/Firmware/HexFileRecord.cs`)

```csharp
public class HexFileRecord
{
    /// <summary>
    /// Gets the full 32-bit linear address for this record.
    /// </summary>
    public uint Address { get; }

    /// <summary>
    /// Gets the raw record bytes (byte count, address, type, data, checksum).
    /// </summary>
    public byte[] Data { get; }

    /// <summary>
    /// Gets the record type (0x00 = data, 0x01 = EOF, 0x04 = extended linear address).
    /// </summary>
    public byte RecordType { get; }

    /// <summary>
    /// Creates a new HEX file record.
    /// </summary>
    /// <param name="address">The full 32-bit linear address.</param>
    /// <param name="data">The raw record bytes.</param>
    /// <param name="recordType">The record type byte.</param>
    public HexFileRecord(uint address, byte[] data, byte recordType)
    {
        Address = address;
        Data = data;
        RecordType = recordType;
    }
}
```

**Other referenced types** (upstream definitions are large or unfindable AND no downstream port exists yet — investigate before porting if the call sites use them):
- `IsProtectedHexRecord` _(upstream definition not found)_
- `ConvertLineToBytes` _(upstream definition not found)_
- `NumberStyles` _(upstream definition not found)_
- `ToUInt16` _(upstream definition not found)_
- `UpdateBaseAddress` _(upstream definition not found)_
- `Parse` _(upstream definition not found)_
- `End` _(upstream definition not found)_
- `Reverse` _(upstream definition not found)_
- `FormatException` _(upstream definition not found)_
- `ValidateLine` _(upstream definition not found)_

## Downstream target (current contents)

_File does not exist yet at `daqifi/_internal/firmware.py` — create it._

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
