# portomatic apply prompt

**Plan:** `20260421T050005-port-linebasedmessageparser-v13`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.13 test 5: LineBasedMessageParser (IMessageParser<string>, IEnumerable<IInboundMessage<string>>, line-ending split)
- classification: new-feature
- shard: Messaging
- upstream_ref: e8d84af:src/Daqifi.Core/Communication/Consumers/LineBasedMessageParser.cs
- downstream_target: daqifi/messaging/line_based_parser.py

## Port conventions (project-wide)

- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.

## .NET helper idioms

Common BCL helpers that recur across nearly every C#→Python port. Use the listed equivalent rather than re-deriving the translation per call site:

| C# | Python equivalent |
|---|---|
| `string.IsNullOrWhiteSpace(s)` | `not s or not s.strip()` |
| `string.IsNullOrEmpty(s)` | `not s` (works for None and empty string) |
| `string.Format("{0}", x)` | `f"{x}"` (f-string) — fall back to `.format()` only when the format string is dynamic |
| `string.Join(",", items)` | `",".join(items)` when items are strings; `",".join(str(x) for x in items)` when not — Python `str.join` raises `TypeError` on non-string elements while .NET `string.Join` stringifies them. Note the order flip — separator first in C#, last in Python. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using Daqifi.Core.Communication.Messages;
using System.Text;

namespace Daqifi.Core.Communication.Consumers;

/// <summary>
/// Message parser for line-based text protocols (like SCPI responses).
/// Splits messages on line endings and creates text-based inbound messages.
/// </summary>
public class LineBasedMessageParser : IMessageParser<string>
{
    private readonly byte[] _lineEnding;
    private readonly Encoding _encoding;

    /// <summary>
    /// Initializes a new instance of the LineBasedMessageParser class.
    /// </summary>
    /// <param name="lineEnding">The line ending to split messages on. Defaults to CRLF.</param>
    /// <param name="encoding">The text encoding to use. Defaults to UTF-8.</param>
    public LineBasedMessageParser(string lineEnding = "\r\n", Encoding? encoding = null)
    {
        _encoding = encoding ?? Encoding.UTF8;
        _lineEnding = _encoding.GetBytes(lineEnding);
    }

    /// <summary>
    /// Parses raw data into line-based text messages.
    /// </summary>
    /// <param name="data">The raw data to parse.</param>
    /// <param name="consumedBytes">The number of bytes consumed from the data during parsing.</param>
    /// <returns>A collection of parsed text messages.</returns>
    public IEnumerable<IInboundMessage<string>> ParseMessages(byte[] data, out int consumedBytes)
    {
        var messages = new List<IInboundMessage<string>>();
        consumedBytes = 0;

        if (data.Length == 0)
            return messages;

        var searchStart = 0;
        while (searchStart < data.Length)
        {
            // Find the next line ending
            var lineEndIndex = FindLineEnding(data, searchStart);
            if (lineEndIndex == -1)
            {
                // No complete line found, stop parsing
                break;
            }

            // Extract the line (excluding line ending)
            var lineLength = lineEndIndex - searchStart;
            if (lineLength > 0)
            {
                var lineData = new byte[lineLength];
                Array.Copy(data, searchStart, lineData, 0, lineLength);
                
                var messageText = _encoding.GetString(lineData);
                if (!string.IsNullOrWhiteSpace(messageText))
                {
                    messages.Add(new TextInboundMessage(messageText.Trim()));
                }
            }

            // Move past this line and its ending
            searchStart = lineEndIndex + _lineEnding.Length;
            consumedBytes = searchStart;
        }

        return messages;
    }

    /// <summary>
    /// Finds the index of the next line ending in the data.
    /// </summary>
    /// <param name="data">The data to search.</param>
    /// <param name="startIndex">The index to start searching from.</param>
    /// <returns>The index of the line ending, or -1 if not found.</returns>
    private int FindLineEnding(byte[] data, int startIndex)
    {
        for (int i = startIndex; i <= data.Length - _lineEnding.Length; i++)
        {
            bool match = true;
            for (int j = 0; j < _lineEnding.Length; j++)
            {
                if (data[i + j] != _lineEnding[j])
                {
                    match = false;
                    break;
                }
            }
            if (match)
                return i;
        }
        return -1;
    }
}

/// <summary>
/// Simple implementation of IInboundMessage for text data.
/// </summary>
public class TextInboundMessage : IInboundMessage<string>
{
    /// <summary>
    /// Initializes a new instance of the TextInboundMessage class.
    /// </summary>
    /// <param name="data">The text data of the message.</param>
    public TextInboundMessage(string data)
    {
        Data = data;
    }

    /// <summary>
    /// Gets the text data of the message.
    /// </summary>
    public string Data { get; }
}
```

## ⚠ Multi-class upstream file (no `target_symbol` set)

The upstream file `src/Daqifi.Core/Communication/Consumers/LineBasedMessageParser.cs` defines 2 top-level types: `LineBasedMessageParser` (not yet ported — separate plan step recommended), `TextInboundMessage` (already ported as `daqifi/_internal/comm.py:315`).

The plan step has no explicit `target_symbol`. Identify which type the step's summary refers to and port ONLY that one. Setting `target_symbol` on future steps removes this ambiguity.

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `IInboundMessage` **[base contract]** (from `src/Daqifi.Core/Communication/Messages/IInboundMessage.cs`) **[already ported as `daqifi/_internal/comm.py:286`]**

```csharp
public interface IInboundMessage<out T> // Using 'out' for covariance
{
    /// <summary>
    /// Gets the payload data received from the device.
    /// </summary>
    T Data { get; }
} 
```

- **Existing downstream implementations** (follow this pattern):
  - `class GenericInboundMessage` at `daqifi/_internal/comm.py:302` _(by name match)_
  - `class TextInboundMessage` at `daqifi/_internal/comm.py:315` _(by name match)_
### `IMessageParser` **[base contract]** (from `src/Daqifi.Core/Communication/Consumers/IMessageParser.cs`)

```csharp
public interface IMessageParser<T>
{
    /// <summary>
    /// Parses raw data into complete messages.
    /// </summary>
    /// <param name="data">The raw data to parse.</param>
    /// <param name="consumedBytes">The number of bytes consumed from the data during parsing.</param>
    /// <returns>A collection of parsed messages.</returns>
    IEnumerable<IInboundMessage<T>> ParseMessages(byte[] data, out int consumedBytes);
}
```

## Downstream target (current contents)

_File does not exist yet at `daqifi/messaging/line_based_parser.py` — create it._

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
