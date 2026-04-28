# portomatic apply prompt

**Plan:** `20260420T012438-port-digitalchannel-v06`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free

## Step

- summary: Port DigitalChannel: thread-safe data model with ChannelNumber, Name, IsEnabled, Direction, OutputValue, IsHigh, SampleReceived event, SetActiveSample method
- classification: new-feature
- shard: Channel
- upstream_ref: e8d84af:src/Daqifi.Core/Channel/DigitalChannel.cs
- downstream_target: daqifi/channels.py
- notes:
  - patch-author honored DIV-001 (Channel shard) - DigitalChannel mirrors AnalogChannel divergence: Python uses SCPI-wrapper DigitalIOChannel, C# uses thread-safe data model. No code change.

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `event EventHandler<T>` → `Callable[[T], None]` callback list, or pub/sub helper if observers > 1
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Channel;

/// <summary>
/// Represents a digital input/output channel.
/// </summary>
public class DigitalChannel : IDigitalChannel
{
    private readonly object _lock = new();
    private IDataSample? _activeSample;
    private string _name;
    private bool _isEnabled;
    private ChannelDirection _direction;
    private bool _outputValue;

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
    /// Gets the channel type (always Digital for this class).
    /// </summary>
    public ChannelType Type => ChannelType.Digital;

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
    /// Gets or sets the output value for digital output channels (true = high, false = low).
    /// </summary>
    public bool OutputValue
    {
        get { lock (_lock) { return _outputValue; } }
        set
        {
            lock (_lock)
            {
                _outputValue = value;
                // When output value changes, we could trigger an event or callback
                // to notify the device to update the physical output
            }
        }
    }

    /// <summary>
    /// Gets whether the digital input is currently high (true) or low (false).
    /// </summary>
    public bool IsHigh
    {
        get
        {
            lock (_lock)
            {
                return _activeSample?.Value > 0.5;
            }
        }
    }

    /// <summary>
    /// Event raised when a new sample is received on this channel.
    /// </summary>
    public event EventHandler<SampleReceivedEventArgs>? SampleReceived;

    /// <summary>
    /// Initializes a new instance of the <see cref="DigitalChannel"/> class.
    /// </summary>
    /// <param name="channelNumber">The channel number/index.</param>
    public DigitalChannel(int channelNumber)
    {
        if (channelNumber < 0)
            throw new ArgumentOutOfRangeException(nameof(channelNumber), "Channel number must be non-negative.");

        ChannelNumber = channelNumber;
        _name = $"Digital Channel {channelNumber}";
        _isEnabled = false;
        _direction = ChannelDirection.Input;
        _outputValue = false;
    }

    /// <summary>
    /// Sets the active sample for this channel and triggers the SampleReceived event.
    /// </summary>
    /// <param name="value">The value (typically 0 for low, 1 for high).</param>
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

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure.

### `ChannelDirection` (from `src/Daqifi.Core/Channel/ChannelDirection.cs`)

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

### `ChannelType` (from `src/Daqifi.Core/Channel/ChannelType.cs`)

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

## Target rationale

Target file has 7 top-level declarations already — extending an established module. Match its existing structure.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

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

```python
"""
Channel classes for DAQiFi Nyquist devices.

Provides property-based access to analog and digital channels.
"""

from typing import Dict, List, Optional, Any
from ._internal.comm import SerialInterface
from ._internal import scpi
from .exceptions import CommandError, ConfigurationError


class AnalogOutputChannel:
    """
    Single analog output (DAC) channel.

    Provides voltage control and enable/disable functionality.
    Available on NQ3 variant only.
    """

    def __init__(self, channel_id: int, comm: SerialInterface):
        """
        Initialize DAC channel.

        Args:
            channel_id: Channel index (0-7 for NQ3)
            comm: Serial communication interface
        """
        self._channel = channel_id
        self._comm = comm

    @property
    def index(self) -> int:
        """Get channel index"""
        return self._channel

    @property
    def voltage(self) -> float:
        """
        Get configured output voltage.

        Returns:
            Voltage in volts

        Raises:
            CommandError: If query fails
        """
        try:
            response = self._comm.send_query(f"{scpi.AnalogOutput.VOLTAGE_QUERY} {self._channel}")
            return scpi.parse_float(response)
        except Exception as e:
            raise CommandError(f"Failed to read voltage on channel {self._channel}: {e}")

    @voltage.setter
    def voltage(self, value: float):
        """
        Set output voltage.

        Args:
            value: Voltage in volts (0.0 to 10.0)

        Raises:
            ConfigurationError: If voltage out of range
            CommandError: If command fails
        """
        if not 0.0 <= value <= 10.0:
            raise ConfigurationError(f"Voltage must be between 0.0 and 10.0V, got {value}V")

        try:
            success = self._comm.send_set(f"{scpi.AnalogOutput.VOLTAGE_SET} {self._channel}", value)
            if not success:
                error = self._comm.send_query(scpi.System.ERR)
                raise CommandError(f"Failed to set voltage: {error}")
        except Exception as e:
            raise CommandError(f"Failed to set voltage on channel {self._channel}: {e}")

    @property
    def enabled(self) -> bool:
        """
        Get channel enable state.

        Returns:
            True if channel enabled, False otherwise
        """
        try:
            response = self._comm.send_query(f"{scpi.AnalogOutput.ENABLE_QUERY} {self._channel}")
            return scpi.parse_bool(response)
        except Exception as e:
            raise CommandError(f"Failed to read enable state on channel {self._channel}: {e}")

    @enabled.setter
    def enabled(self, value: bool):
        """
        Enable or disable channel output.

        Args:
            value: True to enable, False to disable
        """
        try:
            success = self._comm.send_set(f"{scpi.AnalogOutput.ENABLE_SET} {self._channel}", value)
            if not success:
                error = self._comm.send_query(scpi.System.ERR)
                raise CommandError(f"Failed to set enable state: {error}")
        except Exception as e:
            raise CommandError(f"Failed to set enable state on channel {self._channel}: {e}")

    def __repr__(self) -> str:
        try:
            v = self.voltage
            e = self.enabled
            return f"AnalogOutputChannel({self._channel}, voltage={v:.3f}V, enabled={e})"
        except:
            return f"AnalogOutputChannel({self._channel})"


class AnalogInputChannel:
    """
    Single analog input (ADC) channel.

    Provides range configuration and enable/disable functionality.
    """

    def __init__(self, channel_id: int, comm: SerialInterface, variant: Optional[str] = None):
        """
        Initialize ADC channel.

        Args:
            channel_id: Channel index
            comm: Serial communication interface
            variant: Board variant ("NQ1", "NQ2", "NQ3") - optional
        """
        self._channel = channel_id
        self._comm = comm
        self._variant = variant

    @property
    def index(self) -> int:
        """Get channel index"""
        return self._channel

    @property
    def range(self) -> str:
        """
        Get configured input range.

        Note:
        - NQ1/NQ2: Range is fixed (not configurable)
        - NQ3: Range is module-level (applies to all AD7609 channels)

        Returns:
            Range string: "0" for ±5V, "1" for ±10V, or "FIXED" for NQ1/NQ2
        """
        # NQ1/NQ2: Range is not configurable (MC12bADC has fixed range)
        if self._variant in ("NQ1", "NQ2"):
            return "FIXED"

        # NQ3: Range is module-level (AD7609)
        try:
            response = self._comm.send_query(scpi.AnalogInput.RANGE_QUERY)
            return response.strip()
        except Exception as e:
            raise CommandError(f"Failed to read range: {e}")

    @range.setter
    def range(self, value: str):
        """
        Set input range.

        Note:
        - NQ1/NQ2: Range is not configurable - raises FeatureNotSupportedError
        - NQ3: Range is module-level (applies to all AD7609 channels)

        Args:
            value: Range value: "0" for ±5V, "1" for ±10V

        Raises:
            FeatureNotSupportedError: If board doesn't support configurable range
            CommandError: If command fails
        """
        from .exceptions import FeatureNotSupportedError

        # NQ1/NQ2: Range is not configurable
        if self._variant in ("NQ1", "NQ2"):
            raise FeatureNotSupportedError(
                f"Range configuration not available on {self._variant}. "
                "MC12bADC has fixed input range."
            )

        # NQ3: Set module-level range
        try:
            # Convert string values to 0/1 if needed
            if value.upper() in ("5V", "±5V", "+/-5V"):
                range_val = 0
            elif value.upper() in ("10V", "±10V", "+/-10V"):
                range_val = 1
            else:
                range_val = int(value)

            success = self._comm.send_set(scpi.AnalogInput.RANGE_SET, range_val)
            if not success:
                error = self._comm.send_query(scpi.System.ERR)
                raise CommandError(f"Failed to set range: {error}")
        except FeatureNotSupportedError:
            raise
        except Exception as e:
            raise CommandError(f"Failed to set range: {e}")

    @property
    def enabled(self) -> bool:
        """
        Get channel enable state.

        Returns:
            True if channel enabled, False otherwise
        """
        try:
            response = self._comm.send_query(f"{scpi.AnalogInput.ENABLE_CHANNEL_QUERY} {self._channel}")
            return scpi.parse_bool(response)
        except Exception as e:
            raise CommandError(f"Failed to read enable state on channel {self._channel}: {e}")

    @enabled.setter
    def enabled(self, value: bool):
        """
        Enable or disable channel.

        Args:
            value: True to enable, False to disable
        """
        try:
            success = self._comm.send_set(f"{scpi.AnalogInput.ENABLE_CHANNEL_SET} {self._channel}", value)
            if not success:
                error = self._comm.send_query(scpi.System.ERR)
                raise CommandError(f"Failed to set enable state: {error}")
        except Exception as e:
            raise CommandError(f"Failed to set enable state on channel {self._channel}: {e}")

    def __repr__(self) -> str:
        try:
            r = self.range
            e = self.enabled
            return f"AnalogInputChannel({self._channel}, range={r}, enabled={e})"
        except:
            return f"AnalogInputChannel({self._channel})"


class DigitalIOChannel:
    """
    Single digital I/O channel.

    Provides direction control and state read/write.
    """

    def __init__(self, channel_id: int, comm: SerialInterface):
        """
        Initialize DIO channel.

        Args:
            channel_id: Channel index (0-15)
            comm: Serial communication interface
        """
        self._channel = channel_id
        self._comm = comm

    @property
    def index(self) -> int:
        """Get channel index"""
        return self._channel

    @property
    def direction(self) -> str:
        """
        Get channel direction.

        Returns:
            "input" or "output"
        """
        try:
            # Read entire port direction (16-bit mask)
            response = self._comm.send_query(scpi.DigitalIO.PORT_DIRECTION_QUERY)
            port_dir = scpi.parse_int(response)

            # Check bit for this channel (1=output, 0=input)
            is_output = bool(port_dir & (1 << self._channel))
            return "output" if is_output else "input"
        except Exception as e:
            raise CommandError(f"Failed to read direction on channel {self._channel}: {e}")

    @direction.setter
    def direction(self, value: str):
        """
        Set channel direction.

        Args:
            value: "input" or "output"

        Raises:
            ConfigurationError: If invalid direction
        """
        value_lower = value.lower()
        if value_lower not in ("input", "output"):
            raise ConfigurationError(f"Direction must be 'input' or 'output', got '{value}'")

        try:
            # Read current port direction
            response = self._comm.send_query(scpi.DigitalIO.PORT_DIRECTION_QUERY)
            port_dir = scpi.parse_int(response)

            # Modify bit for this channel
            if value_lower == "output":
                port_dir |= (1 << self._channel)  # Set bit
            else:
                port_dir &= ~(1 << self._channel)  # Clear bit

            # Write back entire port direction
            success = self._comm.send_set(scpi.DigitalIO.PORT_DIRECTION_SET, port_dir)
            if not success:
                error = self._comm.send_query(scpi.System.ERR)
                raise CommandError(f"Failed to set direction: {error}")
        except Exception as e:
            raise CommandError(f"Failed to set direction on channel {self._channel}: {e}")

    @property
    def state(self) -> bool:
        """
        Get channel state.

        For output channels, returns configured output state.
        For input channels, returns current input state.

        Returns:
            True for high, False for low
        """
        try:
            # Read entire port state (16-bit mask)
            response = self._comm.send_query(scpi.DigitalIO.PORT_STATE_QUERY)
            port_state = scpi.parse_int(response)

            # Check bit for this channel (1=high, 0=low)
            is_high = bool(port_state & (1 << self._channel))
            return is_high
        except Exception as e:
            raise CommandError(f"Failed to read state on channel {self._channel}: {e}")

    @state.setter
    def state(self, value: bool):
        """
        Set output state (only valid for output channels).

        Args:
            value: True for high, False for low

        Raises:
            ConfigurationError: If channel is configured as input
        """
        if self.direction != "output":
            raise ConfigurationError(f"Cannot set state on input channel {self._channel}")

        try:
            # Read current port state
            response = self._comm.send_query(scpi.DigitalIO.PORT_STATE_QUERY)
            port_state = scpi.parse_int(response)

            # Modify bit for this channel
            if value:
                port_state |= (1 << self._channel)  # Set bit
            else:
                port_state &= ~(1 << self._channel)  # Clear bit

            # Write back entire port state
            success = self._comm.send_set(scpi.DigitalIO.PORT_STATE_SET, port_state)
            if not success:
                error = self._comm.send_query(scpi.System.ERR)
                raise CommandError(f"Failed to set state: {error}")
        except Exception as e:
            raise CommandError(f"Failed to set state on channel {self._channel}: {e}")

    def __repr__(self) -> str:
        try:
            d = self.direction
            s = self.state
            return f"DigitalIOChannel({self._channel}, direction={d}, state={s})"
        except:
            return f"DigitalIOChannel({self._channel})"


# Channel collection classes
class ChannelCollection:
    """Base class for channel collections"""

    def __init__(self, channels: List):
        """Initialize collection with list of channels"""
        self._channels = channels

    def __getitem__(self, index: int):
        """Get channel by index"""
        if 0 <= index < len(self._channels):
            return self._channels[index]
        raise IndexError(f"Channel index {index} out of range (0-{len(self._channels)-1})")

    def __len__(self) -> int:
        """Get number of channels"""
        return len(self._channels)

    def __iter__(self):
        """Iterate over channels"""
        return iter(self._channels)

    @property
    def channels(self) -> List:
        """Get list of all channels"""
        return self._channels.copy()


class AnalogOutputs(ChannelCollection):
    """Collection of analog output (DAC) channels"""

    def set_voltages(self, voltages: Dict[int, float]):
        """
        Set multiple channel voltages at once.

        Args:
            voltages: Dictionary mapping channel index to voltage

        Example:
            analog_outputs.set_voltages({0: 5.0, 1: 3.3, 2: 1.8})
        """
        for channel_id, voltage in voltages.items():
            if 0 <= channel_id < len(self._channels):
                self._channels[channel_id].voltage = voltage

    def enable_all(self):
        """Enable all channels"""
        for channel in self._channels:
            channel.enabled = True

    def disable_all(self):
        """Disable all channels"""
        for channel in self._channels:
            channel.enabled = False

    def __repr__(self) -> str:
        return f"AnalogOutputs({len(self._channels)} channels)"


class AnalogInputs(ChannelCollection):
    """Collection of analog input (ADC) channels"""

    def configure_channels(self, channels: List[int], range: str = "10V", enabled: bool = True):
        """
        Configure multiple channels with same settings.

        Args:
            channels: List of channel indices to configure
            range: Input voltage range
            enabled: Enable channels after configuration

        Example:
            analog_inputs.configure_channels([0, 1, 2], range="5V", enabled=True)
        """
        for channel_id in channels:
            if 0 <= channel_id < len(self._channels):
                self._channels[channel_id].range = range
                self._channels[channel_id].enabled = enabled

    def enable_all(self):
        """Enable all channels"""
        for channel in self._channels:
            channel.enabled = True

    def disable_all(self):
        """Disable all channels"""
        for channel in self._channels:
            channel.enabled = False

    def __repr__(self) -> str:
        return f"AnalogInputs({len(self._channels)} channels)"


class DigitalIO(ChannelCollection):
    """Collection of digital I/O channels"""

    def configure_outputs(self, channels: List[int]):
        """
        Configure channels as outputs.

        Args:
            channels: List of channel indices to configure as outputs

        Example:
            digital_io.configure_outputs([0, 1, 2])
        """
        for channel_id in channels:
            if 0 <= channel_id < len(self._channels):
                self._channels[channel_id].direction = "output"

    def configure_inputs(self, channels: List[int]):
        """
        Configure channels as inputs.

        Args:
            channels: List of channel indices to configure as inputs

        Example:
            digital_io.configure_inputs([3, 4, 5])
        """
        for channel_id in channels:
            if 0 <= channel_id < len(self._channels):
                self._channels[channel_id].direction = "input"

    def set_states(self, states: Dict[int, bool]):
        """
        Set multiple channel states at once.

        Args:
            states: Dictionary mapping channel index to state (True=high, False=low)

        Example:
            digital_io.set_states({0: True, 1: False, 2: True})
        """
        for channel_id, state in states.items():
            if 0 <= channel_id < len(self._channels):
                self._channels[channel_id].state = state

    def read_all(self) -> Dict[int, bool]:
        """
        Read all channel states.

        Returns:
            Dictionary mapping channel index to state

        Example:
            states = digital_io.read_all()
            print(f"Channel 0: {states[0]}")
        """
        return {ch.index: ch.state for ch in self._channels}

    def __repr__(self) -> str:
        return f"DigitalIO({len(self._channels)} channels)"

```

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 7 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 1 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 2 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.

## Map shard `Channel` — relevant entries

- file: `src/Daqifi.Core/Channel/DigitalChannel.cs` → `daqifi/channels.py` (divergent)
  - rationale: Same as AnalogChannel — Python DigitalIOChannel is SCPI-delegated.

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
