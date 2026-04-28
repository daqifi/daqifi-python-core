# portomatic apply prompt

**Plan:** `20260419T234106-port-analog-getscaledvalue-v05`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free

## Step

- summary: Port AnalogChannel.GetScaledValue (raw ADC -> calibrated scaled value math). Python has no such method - judgment call: extend AnalogInputChannel, new helper, or new data model.
- classification: new-feature
- shard: Channel
- upstream_ref: (unset)
- downstream_target: daqifi/channels.py

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
        Initialize DIO 
```

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
