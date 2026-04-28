"""
Channel classes for DAQiFi Nyquist devices.

Provides property-based access to analog and digital channels.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from ._internal.comm import SerialInterface
from ._internal import scpi
from .exceptions import CommandError, ConfigurationError


def _check_setter_response(
    success: bool, error_response: str, op_description: str
) -> None:
    """Resolve a setter outcome from the error response captured by
    ``SerialInterface.send_set_with_error``.

    Some setters return ``success=False`` even on a successful write —
    typically because the firmware doesn't echo an explicit OK. The
    authoritative signal is the SYST:ERR? response captured DURING
    ``send_set_with_error`` — a second SYST:ERR? query would advance
    the device's error queue past the actual error and silently
    swallow it (Qodo PR #53 review #1).

    Three outcomes:
    * ``error_response`` parses as code 0 → success (closes #14).
    * Empty / whitespace-only → raise. A timeout / missing reply on
      the error query MUST NOT be treated as success (Qodo PR #53
      review #2 — would leave the device in an unknown state).
    * Any non-zero code → raise with the parsed (code, description).
    """
    if success:
        return
    if not error_response or not error_response.strip():
        raise CommandError(
            f"Failed to {op_description}: device did not respond to error query "
            "(possible timeout or transport disconnect)"
        )
    code, desc = scpi.parse_system_error(error_response)
    if code == 0:
        # send_set returned False but the device explicitly reports no
        # error — the operation actually succeeded. Bug-fix path.
        return
    raise CommandError(f"Failed to {op_description}: {code},{desc!r}")


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
            success, error_response = self._comm.send_set_with_error(
                f"{scpi.AnalogOutput.VOLTAGE_SET} {self._channel}", value,
            )
            _check_setter_response(success, error_response, "set voltage")
        except CommandError as e:
            raise CommandError(
                f"Failed to set voltage on channel {self._channel}: {e}"
            ) from e
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
            success, error_response = self._comm.send_set_with_error(
                f"{scpi.AnalogOutput.ENABLE_SET} {self._channel}", value,
            )
            _check_setter_response(success, error_response, "set enable state")
        except CommandError as e:
            raise CommandError(
                f"Failed to set enable state on channel {self._channel}: {e}"
            ) from e
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

            success, error_response = self._comm.send_set_with_error(
                scpi.AnalogInput.RANGE_SET, range_val,
            )
            _check_setter_response(success, error_response, "set range")
        except FeatureNotSupportedError:
            raise
        except CommandError as e:
            # No per-channel context (range is module-level on NQ3)
            # so the inner message already says everything useful;
            # just preserve the chain.
            raise CommandError(f"Failed to set range: {e}") from e
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
            success, error_response = self._comm.send_set_with_error(
                f"{scpi.AnalogInput.ENABLE_CHANNEL_SET} {self._channel}", value,
            )
            _check_setter_response(success, error_response, "set enable state")
        except CommandError as e:
            raise CommandError(
                f"Failed to set enable state on channel {self._channel}: {e}"
            ) from e
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
            success, error_response = self._comm.send_set_with_error(
                scpi.DigitalIO.PORT_DIRECTION_SET, port_dir,
            )
            _check_setter_response(success, error_response, "set direction")
        except CommandError as e:
            raise CommandError(
                f"Failed to set direction on channel {self._channel}: {e}"
            ) from e
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
            success, error_response = self._comm.send_set_with_error(
                scpi.DigitalIO.PORT_STATE_SET, port_state,
            )
            _check_setter_response(success, error_response, "set state")
        except CommandError as e:
            raise CommandError(
                f"Failed to set state on channel {self._channel}: {e}"
            ) from e
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


@dataclass(frozen=True)
class ChannelsPopulatedEventArgs:
    """Payload for the ``channels populated`` event.

    Ports the C# ``ChannelsPopulatedEventArgs`` (subclass of
    ``EventArgs``) from ``Daqifi.Core/Device/ChannelsPopulatedEventArgs.cs``.
    Per the project convention, ``EventArgs`` -> frozen dataclass.

    ``channels`` is a tuple (immutable) — the upstream type uses
    ``IReadOnlyList<IChannel>`` and frozen dataclasses can't carry a
    mutable list field without breaking immutability semantics.
    """

    channels: Tuple[Any, ...]
    analog_channel_count: int
    digital_channel_count: int
