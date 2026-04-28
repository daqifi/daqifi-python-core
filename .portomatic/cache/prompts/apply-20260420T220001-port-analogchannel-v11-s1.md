# portomatic apply prompt

**Plan:** `20260420T220001-port-analogchannel-v11`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.11 test 1: Port AnalogChannel — impl class implementing IAnalogChannel
- classification: new-feature
- shard: Channel
- upstream_ref: e8d84af:src/Daqifi.Core/Channel/AnalogChannel.cs
- downstream_target: daqifi/channels.py

## Port conventions (project-wide)

- `event EventHandler<T>` → Single event: `Callable[[T], None]` callback slot OR `add_<event>`/`remove_<event>` if subscribers > 1. Multiple events on same interface: prefix add/remove with the event name (e.g. `add_message_received`, `remove_message_received`) and keep callback lists per-event. Mirror this pattern across all events on the same Protocol/class for consistency.
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `DateTime / DateTimeOffset` → `datetime.datetime` (use `datetime.now(timezone.utc)` for UTC equivalent of `DateTime.UtcNow`)
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
| `nameof(x)` | `"x"` (literal string — common in exception messages and validation) |
| `string.Format("{0}", x)` | `f"{x}"` (f-string) — fall back to `.format()` only when the format string is dynamic |
| `string.Join(",", items)` | `",".join(items)` when items are strings; `",".join(str(x) for x in items)` when not — Python `str.join` raises `TypeError` on non-string elements while .NET `string.Join` stringifies them. Note the order flip — separator first in C#, last in Python. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Channel;

/// <summary>
/// Represents an analog input/output channel with scaling and calibration capabilities.
/// </summary>
public class AnalogChannel : IAnalogChannel
{
    private readonly object _lock = new();
    private IDataSample? _activeSample;
    private string _name;
    private bool _isEnabled;
    private ChannelDirection _direction;
    private double _minValue;
    private double _maxValue;
    private double _calibrationM;
    private double _calibrationB;
    private double _internalScaleM;
    private double _portRange;

    /// <summary>
    /// Gets the channel number/index.
    /// </summary>
    public int ChannelNumber { get; }

    /// <summary>
    /// Gets or sets the channel name.
    /// </summary>
    public string Name
    {
        get { lock (_lock) { return _name; } }
        set { lock (_lock) { _name = value; } }
    }

    /// <summary>
    /// Gets or sets whether the channel is enabled.
    /// </summary>
    public bool IsEnabled
    {
        get { lock (_lock) { return _isEnabled; } }
        set { lock (_lock) { _isEnabled = value; } }
    }

    /// <summary>
    /// Gets the channel type (always Analog for this class).
    /// </summary>
    public ChannelType Type => ChannelType.Analog;

    /// <summary>
    /// Gets or sets the channel direction (Input or Output).
    /// </summary>
    public ChannelDirection Direction
    {
        get { lock (_lock) { return _direction; } }
        set { lock (_lock) { _direction = value; } }
    }

    /// <summary>
    /// Gets the most recent data sample received on this channel.
    /// </summary>
    public IDataSample? ActiveSample
    {
        get
        {
            lock (_lock)
            {
                return _activeSample;
            }
        }
    }

    /// <summary>
    /// Gets or sets the minimum value for the channel range.
    /// </summary>
    public double MinValue
    {
        get { lock (_lock) { return _minValue; } }
        set { lock (_lock) { _minValue = value; } }
    }

    /// <summary>
    /// Gets or sets the maximum value for the channel range.
    /// </summary>
    public double MaxValue
    {
        get { lock (_lock) { return _maxValue; } }
        set { lock (_lock) { _maxValue = value; } }
    }

    /// <summary>
    /// Gets the resolution of the ADC (e.g., 65535 for 16-bit).
    /// </summary>
    public uint Resolution { get; }

    /// <summary>
    /// Gets or sets the calibration slope (M in the scaling formula).
    /// </summary>
    public double CalibrationM
    {
        get { lock (_lock) { return _calibrationM; } }
        set { lock (_lock) { _calibrationM = value; } }
    }

    /// <summary>
    /// Gets or sets the calibration offset (B in the scaling formula).
    /// </summary>
    public double CalibrationB
    {
        get { lock (_lock) { return _calibrationB; } }
        set { lock (_lock) { _calibrationB = value; } }
    }

    /// <summary>
    /// Gets or sets the internal scale factor.
    /// </summary>
    public double InternalScaleM
    {
        get { lock (_lock) { return _internalScaleM; } }
        set { lock (_lock) { _internalScaleM = value; } }
    }

    /// <summary>
    /// Gets or sets the port range (voltage range).
    /// </summary>
    public double PortRange
    {
        get { lock (_lock) { return _portRange; } }
        set { lock (_lock) { _portRange = value; } }
    }

    /// <summary>
    /// Event raised when a new sample is received on this channel.
    /// </summary>
    public event EventHandler<SampleReceivedEventArgs>? SampleReceived;

    /// <summary>
    /// Initializes a new instance of the <see cref="AnalogChannel"/> class.
    /// </summary>
    /// <param name="channelNumber">The channel number/index.</param>
    /// <param name="resolution">The ADC resolution (e.g., 65535 for 16-bit).</param>
    public AnalogChannel(int channelNumber, uint resolution = 65535)
    {
        if (channelNumber < 0)
            throw new ArgumentOutOfRangeException(nameof(channelNumber), "Channel number must be non-negative.");

        if (resolution == 0)
            throw new ArgumentOutOfRangeException(nameof(resolution), "Resolution must be greater than zero.");

        ChannelNumber = channelNumber;
        Resolution = resolution;
        _name = $"Analog Channel {channelNumber}";
        _isEnabled = false;
        _direction = ChannelDirection.Input;
        _minValue = -10.0;
        _maxValue = 10.0;
        _calibrationM = 1.0;
        _calibrationB = 0.0;
        _internalScaleM = 1.0;
        _portRange = 1.0;
    }

    /// <summary>
    /// Converts a raw ADC value to a scaled value using the calibration parameters.
    /// Formula: ScaledValue = (RawValue / Resolution * PortRange * CalibrationM + CalibrationB) * InternalScaleM
    /// </summary>
    /// <param name="rawValue">The raw ADC value from the device.</param>
    /// <returns>The scaled value.</returns>
    public double GetScaledValue(int rawValue)
    {
        lock (_lock)
        {
            double normalized = (double)rawValue / Resolution;
            double scaled = (normalized * _portRange * _calibrationM + _calibrationB) * _internalScaleM;
            return scaled;
        }
    }

    /// <summary>
    /// Sets the active sample for this channel and triggers the SampleReceived event.
    /// </summary>
    /// <param name="value">The raw or scaled value.</param>
    /// <param name="timestamp">The timestamp when the sample was taken.</param>
    public void SetActiveSample(double value, DateTime timestamp)
    {
        var sample = new DataSample(timestamp, value);

        lock (_lock)
        {
            _activeSample = sample;
        }

        SampleReceived?.Invoke(this, new SampleReceivedEventArgs(sample));
    }

    /// <summary>
    /// Returns a string representation of the channel.
    /// </summary>
    /// <returns>The channel name.</returns>
    public override string ToString()
    {
        return Name;
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `IAnalogChannel` **[base contract]** (from `src/Daqifi.Core/Channel/IAnalogChannel.cs`)

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

- **Existing downstream implementations** (follow this pattern):
  - `class AnalogOutputChannel` at `daqifi/channels.py:13` _(by name match)_
  - `class AnalogInputChannel` at `daqifi/channels.py:116` _(by name match)_
### `ChannelDirection` (from `src/Daqifi.Core/Channel/ChannelDirection.cs`) **[already ported as `daqifi/_internal/scpi.py:17`]**

```csharp
public enum ChannelDirection
{
    /// <summary>
    /// Input channel (reads data from device).
    /// </summary>
    Input,

    /// <summary>
    /// Output channel (writes data to device).
    /// </summary>
    Output,

    /// <summary>
    /// Unknown or uninitialized direction.
    /// </summary>
    Unknown
}
```

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

### `IDataSample` (from `src/Daqifi.Core/Channel/IDataSample.cs`) **[already ported as `daqifi/_internal/comm.py:158`]**

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

### `SampleReceivedEventArgs` (from `src/Daqifi.Core/Channel/SampleReceivedEventArgs.cs`) **[already ported as `daqifi/_internal/comm.py:226`]**

```csharp
public class SampleReceivedEventArgs : EventArgs
{
    /// <summary>
    /// Gets the data sample that was received.
    /// </summary>
    public IDataSample Sample { get; }

    /// <summary>
    /// Initializes a new instance of the <see cref="SampleReceivedEventArgs"/> class.
    /// </summary>
    /// <param name="sample">The data sample that was received.</param>
    public SampleReceivedEventArgs(IDataSample sample)
    {
        Sample = sample ?? throw new ArgumentNullException(nameof(sample));
    }
}
```

### `DataSample` (from `src/Daqifi.Core/Channel/DataSample.cs`) **[already ported as `daqifi/_internal/comm.py:191`]**

```csharp
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

### `IChannel` **[parent of `IAnalogChannel`]** (from `src/Daqifi.Core/Channel/IChannel.cs`)

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

Target file has 7 top-level declarations already — extending an established module. Match its existing structure.

**Adjacent divergences in this subtree:**
- Active divergence **DIV-001** covers `src/Daqifi.Core/Channel` (same subtree): _Python port architecturally chooses to delegate scaling/calibration_ — check before porting.

**Sibling files mapped non-trivially in this subtree:**
- Sibling file `src/Daqifi.Core/Channel/IAnalogChannel.cs` → `(none)` (dropped): `Python uses duck typing; no separate interface file.` — consider whether this port belongs alongside that sibling.
- Sibling file `src/Daqifi.Core/Channel/DigitalChannel.cs` → `daqifi/channels.py` (divergent): `Same as AnalogChannel — Python DigitalIOChannel is SCPI-delegated.` — consider whether this port belongs alongside that sibling.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `Channel classes for DAQiFi Nyquist devices.`
      `Provides property-based access to analog and digital channels.`
      `Single analog output (DAC) channel.`
      `Provides voltage control and enable/disable functionality.`
      `Available on NQ3 variant only.`
-       line 191: `# Convert string values to 0/1 if needed`
            `if value.upper() in ("5V", "±5V", "+/-5V"):`
            `range_val = 0`
            `elif value.upper() in ("10V", "±10V", "+/-10V"):`
            `range_val = 1`
            `else:`
-       line 279: `# Read entire port direction (16-bit mask)`
            `response = self._comm.send_query(scpi.DigitalIO.PORT_DIRECTION_QUERY)`
            `port_dir = scpi.parse_int(response)`
            `is_output = bool(port_dir & (1 << self._channel))`
            `return "output" if is_output else "input"`
            `except Exception as e:`
-       line 305: `# Read current port direction`
            `response = self._comm.send_query(scpi.DigitalIO.PORT_DIRECTION_QUERY)`
            `port_dir = scpi.parse_int(response)`
-       line 309: `# Modify bit for this channel`
            `if value_lower == "output":`
            `port_dir |= (1 << self._channel)  # Set bit`
            `else:`
            `port_dir &= ~(1 << self._channel)  # Clear bit`
-       line 315: `# Write back entire port direction`
            `success = self._comm.send_set(scpi.DigitalIO.PORT_DIRECTION_SET, port_dir)`
            `if not success:`
            `error = self._comm.send_query(scpi.System.ERR)`
            `raise CommandError(f"Failed to set direction: {error}")`
            `except Exception as e:`
-       line 335: `# Read entire port state (16-bit mask)`
            `response = self._comm.send_query(scpi.DigitalIO.PORT_STATE_QUERY)`
            `port_state = scpi.parse_int(response)`
            `is_high = bool(port_state & (1 << self._channel))`
            `return is_high`
            `except Exception as e:`
-       line 360: `# Read current port state`
            `response = self._comm.send_query(scpi.DigitalIO.PORT_STATE_QUERY)`
            `port_state = scpi.parse_int(response)`
-       line 364: `# Modify bit for this channel`
            `if value:`
            `port_state |= (1 << self._channel)  # Set bit`
            `else:`
            `port_state &= ~(1 << self._channel)  # Clear bit`
-       line 370: `# Write back entire port state`
            `success = self._comm.send_set(scpi.DigitalIO.PORT_STATE_SET, port_state)`
            `if not success:`
            `error = self._comm.send_query(scpi.System.ERR)`
            `raise CommandError(f"Failed to set state: {error}")`
            `except Exception as e:`
- line 387: `# Channel collection classes`
      `class ChannelCollection:`
      `def __init__(self, channels: List):`
      `self._channels = channels`
      `def __getitem__(self, index: int):`
      `if 0 <= index < len(self._channels):`

**Top-level declarations** (existing module-level symbols — use as structural overview):

- `class AnalogOutputChannel`
- `class AnalogInputChannel`
- `class DigitalIOChannel`
- `class ChannelCollection`
- `class AnalogOutputs`
- `class AnalogInputs`
- `class DigitalIO`

**Nearest top-level declarations** (where new code typically lands):

- `class AnalogOutputs`
- `class AnalogInputs`
- `class DigitalIO`

_(full body elided — downstream target is 539 lines and the File regions index above already carries the structural overview; see the regions index + top-level declarations above for structural overview, and `daqifi/channels.py` for the full current file.)_

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

## Map shard `Channel` — relevant entries

- file: `src/Daqifi.Core/Channel/AnalogChannel.cs` → `daqifi/channels.py` (divergent)
  - rationale: C# AnalogChannel is a self-contained data model with local
scaling/calibration math (GetScaledValue computes (raw / resolution
* port_range * cal_m + cal_b) * internal_scale_m client-side).
Python AnalogInputChannel is a thin SCPI wrapper with no scaling
math — the device computes scaled values and the Python class
delegates everything via send_query/send_set. There is no Python
class with field-level parity to C# AnalogChannel and no plan to
add one (the Python port is intentionally device-delegated).


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
