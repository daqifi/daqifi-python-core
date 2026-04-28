# portomatic apply prompt

**Plan:** `20260420T044015-port-datasample-v08`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: #15: Port DataSample concrete IDataSample impl
- classification: new-feature
- shard: Channel
- upstream_ref: e8d84af:src/Daqifi.Core/Channel/DataSample.cs
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
namespace Daqifi.Core.Channel;

/// <summary>
/// Represents a single data sample from a channel with timestamp and value.
/// </summary>
public class DataSample : IDataSample
{
    /// <summary>
    /// Gets the timestamp when the sample was taken.
    /// </summary>
    public DateTime Timestamp { get; init; }

    /// <summary>
    /// Gets or sets the value of the sample.
    /// </summary>
    public double Value { get; set; }

    /// <summary>
    /// Initializes a new instance of the <see cref="DataSample"/> class.
    /// </summary>
    public DataSample()
    {
        Timestamp = DateTime.UtcNow;
        Value = 0.0;
    }

    /// <summary>
    /// Initializes a new instance of the <see cref="DataSample"/> class with specified values.
    /// </summary>
    /// <param name="timestamp">The timestamp when the sample was taken.</param>
    /// <param name="value">The value of the sample.</param>
    public DataSample(DateTime timestamp, double value)
    {
        Timestamp = timestamp;
        Value = value;
    }

    /// <summary>
    /// Returns a string representation of the data sample.
    /// </summary>
    /// <returns>A string containing the timestamp and value.</returns>
    public override string ToString()
    {
        return $"{Timestamp:yyyy-MM-dd HH:mm:ss.fff} - {Value}";
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure.

### `IDataSample` (from `src/Daqifi.Core/Channel/IDataSample.cs`)

```csharp
public interface IDataSample
{
    /// <summary>
    /// Gets the timestamp when the sample was taken.
    /// </summary>
    DateTime Timestamp { get; }

    /// <summary>
    /// Gets or sets the value of the sample.
    /// </summary>
    double Value { get; set; }
}
```

## Target rationale

Target file already references `DataSample` — extending in place. Look for the existing reference and append your change near it.

**Adjacent divergences in this subtree:**
- Active divergence **DIV-001** covers `src/Daqifi.Core/Channel` (same subtree): _Python port architecturally chooses to delegate scaling/calibration_ — check before porting.

**Sibling files mapped non-trivially in this subtree:**
- Sibling file `src/Daqifi.Core/Channel/AnalogChannel.cs` → `daqifi/channels.py` (divergent): `C# AnalogChannel is a self-contained data model with local` — consider whether this port belongs alongside that sibling.
- Sibling file `src/Daqifi.Core/Channel/IAnalogChannel.cs` → `(none)` (dropped): `Python uses duck typing; no separate interface file.` — consider whether this port belongs alongside that sibling.
- Sibling file `src/Daqifi.Core/Channel/DigitalChannel.cs` → `daqifi/channels.py` (divergent): `Same as AnalogChannel — Python DigitalIOChannel is SCPI-delegated.` — consider whether this port belongs alongside that sibling.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `Internal serial communication layer for DAQiFi Nyquist devices.`
      `This module provides low-level SCPI command interface over serial port.`
      `Not intended for direct use - use the public API instead.`
      `Ports the C# ``TransportStatusEventArgs`` (subclass of ``EventArgs``).`
      `Python uses plain callables instead of the .NET event pattern, so the`
-       line 449: `# Wait for device to stabilize`
            `time.sleep(0.1)`
-       line 451: `# Clear any pending data`
            `self._serial.reset_input_buffer()`
            `self._serial.reset_output_buffer()`
            `return True`
            `except (serial.SerialException, OSError) as e:`
            `return False`
-       line 486: `# Send command with CR terminator`
            `cmd_bytes = (command + "\r").encode('ascii')`
            `self._serial.write(cmd_bytes)`
            `self._serial.flush()`
-       line 491: `# Wait for device to process`
            `time.sleep(delay)`
-       line 494: `# Read all available data`
            `response = b""`
            `start_time = time.time()`
            `while (time.time() - start_time) < self.timeout:`
            `if self._serial.in_waiting > 0:`
            `chunk = self._serial.read(self._serial.in_waiting)`
-           line 502: `# Give a bit more time for any trailing data`
                `time.sleep(0.05)`
                `if self._serial.in_waiting == 0:`
                `break`
                `else:`
-           line 508: `# No data yet, wait a bit`
                `time.sleep(0.01)`
-       line 511: `# Decode and clean response`
            `response_str = response.decode('ascii', errors='ignore')`
-       line 514: `# Remove echoed command if present`
            `if command in response_str:`
            `response_str = response_str.replace(command, "")`
            `response_str = response_str.replace("DAQIFI>", "")`
-       line 521: `# Clean up whitespace and control characters`
            `response_str = response_str.strip()`
            `return response_str`
            `except (serial.SerialException, OSError) as e:`
            `raise RuntimeError(f"Serial communication error: {e}")`
            `def send_query(self, query: str, delay: float = 0.1) -> str:`
-     line 541: `# Remove any surrounding quotes from response`
          `response = response.strip('"').strip("'")`
          `return response`
          `def send_set(self, command: str, value, delay: float = 0.1) -> bool:`
-     line 557: `# Format value based on type`
          `if isinstance(value, str):`
          `cmd = f"{command} {value}"`
          `elif isinstance(value, bool):`
          `cmd = f"{command} {1 if value else 0}"`
          `elif isinstance(value, (int, float)):`
-     line 569: `# Check for errors`
          `error = self.send_query("SYST:ERR?", 0.1)`
          `return "No error" in error`
          `def clear_errors(self):`
          `max_errors = 10`
          `for _ in range(max_errors):`

**Top-level declarations** (existing module-level symbols — use as structural overview):

- `class TransportStatusEventArgs`
- `class ConnectionRetryOptions`
- `class StreamTransport`
- `class DataSample`
- `class OutboundMessage`
- `class ScpiMessage`
- `class MessageProducer`
- `class MessageReceivedEventArgs`
- `class MessageConsumerErrorEventArgs`
- `class MessageConsumer`
- `class PowerState`
- `class SerialInterface`
- `T (constant)`

**Nearest top-level declarations** (where new code typically lands):

- `class PowerState`
- `class SerialInterface`
- `T (constant)`

_(downstream target is 593 lines; full body elided because the upstream change is small (46 changed lines) — see the regions index + top-level declarations above for structural overview, and `daqifi/_internal/comm.py` for the full current file.)_

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 10 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 4 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 3 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 5 usages (e.g. `daqifi/_internal/comm.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 8 usages (e.g. `daqifi/discovery.py`)

## Map shard `Channel` — relevant entries


### Architectural context (no exact file match)

_Nearest file mappings in this shard — read these BEFORE deciding whether to create a downstream file:_

- `src/Daqifi.Core/Channel/AnalogChannel.cs` → `daqifi/channels.py` (divergent)
  > C# AnalogChannel is a self-contained data model with local
  > scaling/calibration math (GetScaledValue computes (raw / resolution
  > * port_range * cal_m + cal_b) * internal_scale_m client-side).
  > Python AnalogInputChannel is a thin SCPI wrapper with no scaling
  > math — the device computes scaled values and the Python class
  > delegates everything via send_query/send_set. There is no Python
  > class with field-level parity to C# AnalogChannel and no plan to
  > add one (the Python port is intentionally device-delegated).
- `src/Daqifi.Core/Channel/IAnalogChannel.cs` → `(none)` (dropped)
  > Python uses duck typing; no separate interface file.
- `src/Daqifi.Core/Channel/DigitalChannel.cs` → `daqifi/channels.py` (divergent)
  > Same as AnalogChannel — Python DigitalIOChannel is SCPI-delegated.

**Active divergences in this area:**
- `DIV-001` (behavioral): Python port architecturally chooses to delegate scaling/calibration
to the device firmware rather than recomputing client-side. This
means C# tests like GetScaledValue_*, Constructor_*, Properties_*,
ToString_ReturnsChannelName have NO Python equivalent and should
not be ported. New C# AnalogChannel features that touch scaling
math should also be classified as `divergent` on /portomatic-check
until the Python architecture changes.


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
