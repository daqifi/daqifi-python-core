# portomatic apply prompt

**Plan:** `20260420T071500-port-channelspop-v09`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: #25: Port ChannelsPopulatedEventArgs
- classification: new-feature
- shard: Device
- upstream_ref: e8d84af:src/Daqifi.Core/Device/ChannelsPopulatedEventArgs.cs
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
using System;
using System.Collections.Generic;
using Daqifi.Core.Channel;

namespace Daqifi.Core.Device
{
    /// <summary>
    /// Provides data for the <see cref="DaqifiDevice.ChannelsPopulated"/> event.
    /// </summary>
    public class ChannelsPopulatedEventArgs : EventArgs
    {
        /// <summary>
        /// Gets the list of channels that were populated.
        /// </summary>
        public IReadOnlyList<IChannel> Channels { get; }

        /// <summary>
        /// Gets the number of analog channels that were populated.
        /// </summary>
        public int AnalogChannelCount { get; }

        /// <summary>
        /// Gets the number of digital channels that were populated.
        /// </summary>
        public int DigitalChannelCount { get; }

        /// <summary>
        /// Initializes a new instance of the <see cref="ChannelsPopulatedEventArgs"/> class.
        /// </summary>
        /// <param name="channels">The list of populated channels.</param>
        /// <param name="analogChannelCount">The number of analog channels populated.</param>
        /// <param name="digitalChannelCount">The number of digital channels populated.</param>
        public ChannelsPopulatedEventArgs(IReadOnlyList<IChannel> channels, int analogChannelCount, int digitalChannelCount)
        {
            Channels = channels;
            AnalogChannelCount = analogChannelCount;
            DigitalChannelCount = digitalChannelCount;
        }
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create.

### `IChannel` (from `src/Daqifi.Core/Channel/IChannel.cs`)

```csharp
public interface IChannel
{
    /// <summary>
    /// Gets the channel number/index.
    /// </summary>
    int ChannelNumber { get; }

    /// <summary>
    /// Gets or sets the channel name.
    /// </summary>
    string Name { get; set; }

    /// <summary>
    /// Gets or sets whether the channel is enabled.
    /// </summary>
    bool IsEnabled { get; set; }

    /// <summary>
    /// Gets the channel type (Analog or Digital).
    /// </summary>
    ChannelType Type { get; }

    /// <summary>
    /// Gets or sets the channel direction (Input or Output).
    /// </summary>
    ChannelDirection Direction { get; set; }

    /// <summary>
    /// Gets the most recent data sample received on this channel.
    /// </summary>
    IDataSample? ActiveSample { get; }

    /// <summary>
    /// Event raised when a new sample is received on this channel.
    /// </summary>
    event EventHandler<SampleReceivedEventArgs>? SampleReceived;

    /// <summary>
    /// Sets the active sample for this channel and triggers the SampleReceived event.
    /// </summary>
    /// <param name="value">The raw or scaled value.</param>
    /// <param name="timestamp">The timestamp when the sample was taken.</param>
    void SetActiveSample(double value, DateTime timestamp);
}
```

## Target rationale

Target file has 23 top-level declarations already — extending an established module. Match its existing structure.

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

_Module constants:_
- `T (constant)`

**Nearest top-level declarations** (where new code typically lands):

- `class PowerState`
- `class SerialInterface`
- `T (constant)`

_(full body elided — downstream target is 890 lines and the upstream change is small (40 changed lines); see the regions index + top-level declarations above for structural overview, and `daqifi/_internal/comm.py` for the full current file.)_

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
