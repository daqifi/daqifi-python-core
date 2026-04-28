# portomatic apply prompt

**Plan:** `20260420T220005-port-compositeparser-v11`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.11 test 5: Port CompositeMessageParser — nested enum + nested public class
- classification: new-feature
- shard: Communication
- upstream_ref: e8d84af:src/Daqifi.Core/Communication/Consumers/CompositeMessageParser.cs
- downstream_target: daqifi/_internal/comm.py

## Port conventions (project-wide)

- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Stream` → `typing.IO[bytes]` (or `BinaryIO`) — file-like objects with read/write
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.

## .NET helper idioms

Common BCL helpers that recur across nearly every C#→Python port. Use the listed equivalent rather than re-deriving the translation per call site:

| C# | Python equivalent |
|---|---|
| `string.IsNullOrWhiteSpace(s)` | `not s or not s.strip()` |
| `string.IsNullOrEmpty(s)` | `not s` (works for None and empty string) |
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
using Daqifi.Core.Communication.Messages;

namespace Daqifi.Core.Communication.Consumers;

/// <summary>
/// A composite message parser that attempts to parse messages using multiple parsers.
/// This allows handling different message formats (text-based SCPI and binary protobuf) in the same stream.
/// </summary>
public class CompositeMessageParser : IMessageParser<object>
{
    private readonly IMessageParser<string> _textParser;
    private readonly IMessageParser<DaqifiOutMessage> _protobufParser;

    /// <summary>
    /// Initializes a new instance of the CompositeMessageParser class.
    /// </summary>
    /// <param name="textParser">Parser for text-based messages (e.g., SCPI responses).</param>
    /// <param name="protobufParser">Parser for binary protobuf messages.</param>
    public CompositeMessageParser(
        IMessageParser<string>? textParser = null, 
        IMessageParser<DaqifiOutMessage>? protobufParser = null)
    {
        _textParser = textParser ?? new LineBasedMessageParser();
        _protobufParser = protobufParser ?? new ProtobufMessageParser();
    }

    /// <summary>
    /// Parses raw data by intelligently trying both text and protobuf parsers.
    /// Uses heuristics beyond simple null byte detection to determine message type.
    /// </summary>
    /// <param name="data">The raw data to parse.</param>
    /// <param name="consumedBytes">The number of bytes consumed from the data during parsing.</param>
    /// <returns>A collection of parsed messages of various types.</returns>
    public IEnumerable<IInboundMessage<object>> ParseMessages(byte[] data, out int consumedBytes)
    {
        var messages = new List<IInboundMessage<object>>();
        consumedBytes = 0;

        if (data.Length == 0)
            return messages;

        // Use improved heuristics to detect message type
        var messageTypeHint = DetectMessageType(data);

        if (messageTypeHint == MessageTypeHint.LikelyProtobuf)
        {
            // Try protobuf parser first
            var protobufMessages = _protobufParser.ParseMessages(data, out int protobufConsumed);
            if (protobufMessages.Any())
            {
                foreach (var msg in protobufMessages)
                {
                    messages.Add(new ObjectInboundMessage(msg.Data));
                }
                consumedBytes = protobufConsumed;
                return messages;
            }
        }

        if (messageTypeHint == MessageTypeHint.LikelyText)
        {
            // Try text parser first
            var textMessages = _textParser.ParseMessages(data, out int textConsumed);
            if (textMessages.Any())
            {
                foreach (var msg in textMessages)
                {
                    messages.Add(new ObjectInboundMessage(msg.Data));
                }
                consumedBytes = textConsumed;
                return messages;
            }
        }

        // If heuristics are uncertain or first attempt failed, try the other parser
        if (messageTypeHint != MessageTypeHint.LikelyText)
        {
            var textMessages = _textParser.ParseMessages(data, out int textConsumed);
            if (textMessages.Any())
            {
                foreach (var msg in textMessages)
                {
                    messages.Add(new ObjectInboundMessage(msg.Data));
                }
                consumedBytes = textConsumed;
                return messages;
            }
        }

        if (messageTypeHint != MessageTypeHint.LikelyProtobuf)
        {
            var protobufMessages = _protobufParser.ParseMessages(data, out int protobufConsumed);
            if (protobufMessages.Any())
            {
                foreach (var msg in protobufMessages)
                {
                    messages.Add(new ObjectInboundMessage(msg.Data));
                }
                consumedBytes = protobufConsumed;
            }
        }

        return messages;
    }

    /// <summary>
    /// Message type hints for improved detection.
    /// </summary>
    private enum MessageTypeHint
    {
        Uncertain,
        LikelyText,
        LikelyProtobuf
    }

    /// <summary>
    /// Uses multiple heuristics to detect the likely message type.
    /// Goes beyond simple null byte detection to avoid false positives.
    /// </summary>
    /// <param name="data">The data to analyze.</param>
    /// <returns>A hint about the likely message type.</returns>
    private static MessageTypeHint DetectMessageType(byte[] data)
    {
        if (data.Length == 0)
            return MessageTypeHint.Uncertain;

        // Heuristic 1: Check for printable ASCII (common in SCPI) - prioritize this
        var printableRatio = data.Count(b => b >= 32 && b <= 126) / (double)data.Length;
        if (printableRatio > 0.8) // More than 80% printable ASCII
        {
            return MessageTypeHint.LikelyText;
        }

        // Heuristic 2: Check for common text patterns (SCPI commands)
        if (data.Length > 3 && IsLikelyTextCommand(data))
        {
            return MessageTypeHint.LikelyText;
        }

        // Heuristic 3: High ratio of null bytes suggests binary
        var nullByteRatio = data.Count(b => b == 0) / (double)data.Length;
        if (nullByteRatio > 0.1) // More than 10% null bytes
        {
            return MessageTypeHint.LikelyProtobuf;
        }

        // Heuristic 4: Check for protobuf-like patterns (be more conservative)
        if (nullByteRatio > 0.05 && IsLikelyProtobufData(data)) // Only if some null bytes present
        {
            return MessageTypeHint.LikelyProtobuf;
        }

        return MessageTypeHint.Uncertain;
    }

    /// <summary>
    /// Checks if the data looks like a text command (SCPI-style).
    /// </summary>
    /// <param name="data">The data to check.</param>
    /// <returns>True if it looks like a text command.</returns>
    private static bool IsLikelyTextCommand(byte[] data)
    {
        // Check for common SCPI patterns
        var text = System.Text.Encoding.ASCII.GetString(data, 0, Math.Min(data.Length, 10));
        var fullText = System.Text.Encoding.ASCII.GetString(data);
        
        return text.StartsWith("*") || text.StartsWith("SYST") || text.StartsWith("CONF") || 
               text.StartsWith("READ", StringComparison.OrdinalIgnoreCase) ||
               fullText.EndsWith("\r\n") || fullText.EndsWith("\n");
    }

    /// <summary>
    /// Checks if the data has protobuf-like characteristics.
    /// </summary>
    /// <param name="data">The data to check.</param>
    /// <returns>True if it looks like protobuf data.</returns>
    private static bool IsLikelyProtobufData(byte[] data)
    {
        if (data.Length < 2)
            return false;

        // Protobuf messages often start with field tags (varint encoded)
        // Check for patterns that suggest protobuf field encoding
        for (int i = 0; i < Math.Min(data.Length - 1, 5); i++)
        {
            var byte1 = data[i];
            var byte2 = data[i + 1];
            
            // Look for varint patterns (field number + wire type)
            if ((byte1 & 0x07) <= 5 && // Valid wire type (0-5)
                (byte1 >> 3) > 0)      // Non-zero field number
            {
                return true;
            }
        }

        return false;
    }
}

/// <summary>
/// Simple implementation of IInboundMessage for generic object data.
/// </summary>
public class ObjectInboundMessage : IInboundMessage<object>
{
    /// <summary>
    /// Initializes a new instance of the ObjectInboundMessage class.
    /// </summary>
    /// <param name="data">The object data of the message.</param>
    public ObjectInboundMessage(object data)
    {
        Data = data;
    }

    /// <summary>
    /// Gets the object data of the message.
    /// </summary>
    public object Data { get; }
}
```

## ⚠ Multi-class upstream file (no `target_symbol` set)

The upstream file `src/Daqifi.Core/Communication/Consumers/CompositeMessageParser.cs` defines 2 top-level types: `CompositeMessageParser` (not yet ported — separate plan step recommended), `ObjectInboundMessage` (not yet ported — separate plan step recommended).

The plan step has no explicit `target_symbol`. Identify which type the step's summary refers to and port ONLY that one. Setting `target_symbol` on future steps removes this ambiguity.

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

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

- `IInboundMessage` **[base contract]** **[already ported in this target file at line 286]** — see the file body above.
- `DaqifiOutMessage` (from `src/Daqifi.Core/Communication/Messages/DaqifiOutMessage.cs`) _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Communication/Messages/DaqifiOutMessage.cs` for the full contract)_
### `MessageTypeHint` (from `src/Daqifi.Core/Communication/Consumers/CompositeMessageParser.cs`)

```csharp
    private enum MessageTypeHint
    {
        Uncertain,
        LikelyText,
        LikelyProtobuf
    }
```

## Target rationale

Target file has 23 top-level declarations already — extending an established module. Match its existing structure.

**Sibling files mapped non-trivially in this subtree:**
- Sibling file `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` → `daqifi/_internal/scpi.py` (divergent) — consider whether this port belongs alongside that sibling.

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

**Nearest top-level declarations** (where new code typically lands):

- `class PowerState`
- `class SerialInterface`
- `T (constant)`

_(full body elided — downstream target is 890 lines (over the 800-line aggressive-elide threshold) with 219 changed lines upstream; see the regions index + top-level declarations above for structural overview, and `daqifi/_internal/comm.py` for the full current file.)_

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

## Map shard `Communication` — relevant entries


### Architectural context (no exact file match)

_Nearest file mappings in this shard — read these BEFORE deciding whether to create a downstream file:_

- `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` → `daqifi/_internal/scpi.py` (divergent)

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
