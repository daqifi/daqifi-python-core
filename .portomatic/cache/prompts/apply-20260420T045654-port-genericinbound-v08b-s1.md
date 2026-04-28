# portomatic apply prompt

**Plan:** `20260420T045654-port-genericinbound-v08b`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: #21: Port GenericInboundMessage<T> concrete generic
- classification: new-feature
- shard: Communication
- upstream_ref: e8d84af:src/Daqifi.Core/Communication/Messages/GenericInboundMessage.cs
- downstream_target: daqifi/_internal/comm.py

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

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Communication.Messages;

/// <summary>
/// Represents a generic message containing incoming data from the DAQiFi device.
/// This is a generic wrapper that can hold any type of data.
/// </summary>
/// <typeparam name="T">The type of the data payload.</typeparam>
public class GenericInboundMessage<T> : IInboundMessage<T>
{
    /// <summary>
    /// Gets the data associated with the message.
    /// </summary>
    public T Data { get; }

    /// <summary>
    /// Initializes a new instance of the <see cref="GenericInboundMessage{T}"/> class.
    /// </summary>
    /// <param name="data">The data received from the device.</param>
    public GenericInboundMessage(T data)
    {
        Data = data;
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure.

### `IInboundMessage` (from `src/Daqifi.Core/Communication/Messages/IInboundMessage.cs`)

```csharp
public interface IInboundMessage<out T> // Using 'out' for covariance
{
    /// <summary>
    /// Gets the payload data received from the device.
    /// </summary>
    T Data { get; }
} 
```

## Target rationale

Target file has 17 top-level declarations already — extending an established module. Match its existing structure.

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
-       line 592: `# Wait for device to stabilize`
            `time.sleep(0.1)`
-       line 594: `# Clear any pending data`
            `self._serial.reset_input_buffer()`
            `self._serial.reset_output_buffer()`
            `return True`
            `except (serial.SerialException, OSError) as e:`
            `return False`
-       line 629: `# Send command with CR terminator`
            `cmd_bytes = (command + "\r").encode('ascii')`
            `self._serial.write(cmd_bytes)`
            `self._serial.flush()`
-       line 634: `# Wait for device to process`
            `time.sleep(delay)`
-       line 637: `# Read all available data`
            `response = b""`
            `start_time = time.time()`
            `while (time.time() - start_time) < self.timeout:`
            `if self._serial.in_waiting > 0:`
            `chunk = self._serial.read(self._serial.in_waiting)`
-           line 645: `# Give a bit more time for any trailing data`
                `time.sleep(0.05)`
                `if self._serial.in_waiting == 0:`
                `break`
                `else:`
-           line 651: `# No data yet, wait a bit`
                `time.sleep(0.01)`
-       line 654: `# Decode and clean response`
            `response_str = response.decode('ascii', errors='ignore')`
-       line 657: `# Remove echoed command if present`
            `if command in response_str:`
            `response_str = response_str.replace(command, "")`
            `response_str = response_str.replace("DAQIFI>", "")`
-       line 664: `# Clean up whitespace and control characters`
            `response_str = response_str.strip()`
            `return response_str`
            `except (serial.SerialException, OSError) as e:`
            `raise RuntimeError(f"Serial communication error: {e}")`
            `def send_query(self, query: str, delay: float = 0.1) -> str:`
-     line 684: `# Remove any surrounding quotes from response`
          `response = response.strip('"').strip("'")`
          `return response`
          `def send_set(self, command: str, value, delay: float = 0.1) -> bool:`
-     line 700: `# Format value based on type`
          `if isinstance(value, str):`
          `cmd = f"{command} {value}"`
          `elif isinstance(value, bool):`
          `cmd = f"{command} {1 if value else 0}"`
          `elif isinstance(value, (int, float)):`
-     line 712: `# Check for errors`
          `error = self.send_query("SYST:ERR?", 0.1)`
          `return "No error" in error`
          `def clear_errors(self):`
          `max_errors = 10`
          `for _ in range(max_errors):`

**Top-level declarations** (existing module-level symbols — use as structural overview):

- `class TransportStatusEventArgs`
- `class ConnectionRetryOptions`
- `class StreamTransport`
- `class IDataSample`
- `class DataSample`
- `class SampleReceivedEventArgs`
- `class OutboundMessage`
- `class ScpiMessage`
- `class MessageProducer`
- `class MessageReceivedEventArgs`
- `class MessageConsumerErrorEventArgs`
- `class MessageConsumer`
- `class HidDeviceInfo`
- `class DeviceConnectionOptions`
- `class PowerState`
- `class SerialInterface`
- `T (constant)`

**Nearest top-level declarations** (where new code typically lands):

- `class PowerState`
- `class SerialInterface`
- `T (constant)`

_(downstream target is 736 lines; full body elided because the upstream change is small (23 changed lines) — see the regions index + top-level declarations above for structural overview, and `daqifi/_internal/comm.py` for the full current file.)_

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 10 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 9 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 3 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 8 usages (e.g. `daqifi/_internal/comm.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 14 usages (e.g. `daqifi/discovery.py`)

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
