"""
Channel classes for DAQiFi Nyquist devices.

Provides property-based access to analog and digital channels.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from ._internal.comm import DataSample, SerialInterface
from ._internal import scpi
from ._internal.scpi import check_setter_response
from .exceptions import CommandError, ConfigurationError


_log = logging.getLogger(__name__)


# Backward-compat alias — `_check_setter_response` was the original
# private name. Now exposed publicly via `daqifi._internal.scpi`
# so non-channel modules (e.g. NyquistDevice.power_state) can use
# the same setter-response validation without importing across a
# private (underscore-prefixed) boundary (Qodo PR #61 review #1).
_check_setter_response = check_setter_response


class _SampleReceivedMixin:
    """Per-channel SampleReceived event + ActiveSample cache surface.

    Mirrors the C# ``AnalogChannel`` / ``DigitalChannel`` events from
    ``Daqifi.Core/Channel`` (#99 group B). Hosted as a mixin because
    AnalogInputChannel and DigitalIOChannel both need the exact same
    shape — both ports of the C# IChannel surface that exposes
    ``SampleReceived`` / ``ActiveSample`` / ``SetActiveSample``.

    Lifecycle:
    - Subclass __init__ calls :meth:`_init_sample_events` to set up
      the cache + handler list.
    - The streaming pipeline calls :meth:`set_active_sample` whenever
      a new sample arrives for this channel (mirrors C#
      ``SetActiveSample``).
    - Callers register handlers via :meth:`add_sample_received` —
      each handler is invoked with the new :class:`DataSample` after
      the cache is updated.
    """

    def _init_sample_events(self) -> None:
        self._active_sample: Optional[DataSample] = None
        self._sample_received_handlers: List[Callable[[DataSample], None]] = []
        # Plain Lock would suffice given that set_active_sample
        # releases the lock before invoking handlers (so a handler
        # that calls add/remove_sample_received doesn't actually
        # contend against an already-held lock). RLock is the
        # defensive choice anyway: it costs nothing in the
        # uncontended case and keeps re-entrant register/dispatch
        # patterns safe if a future change ever moves handler
        # invocation back under the lock. Streaming pipeline
        # integration (see #107) will dispatch from a background
        # thread; user code typically registers handlers from the
        # main thread.
        self._sample_received_lock = threading.RLock()

    @property
    def active_sample(self) -> Optional[DataSample]:
        """Last :class:`DataSample` dispatched to this channel via
        :meth:`set_active_sample`, or ``None`` if no sample has been
        received yet. Mirrors C# ``IChannel.ActiveSample``.
        """
        return self._active_sample

    def add_sample_received(
        self, handler: Callable[[DataSample], None]
    ) -> None:
        """Register a handler called for each sample dispatched to
        this channel. Idempotent — registering the same handler twice
        adds it once. Mirrors C# ``SampleReceived += handler``.
        """
        with self._sample_received_lock:
            if handler not in self._sample_received_handlers:
                self._sample_received_handlers.append(handler)

    def remove_sample_received(
        self, handler: Callable[[DataSample], None]
    ) -> None:
        """Unregister a previously-added handler. No-op if the
        handler isn't currently registered. Mirrors C#
        ``SampleReceived -= handler``.
        """
        with self._sample_received_lock:
            try:
                self._sample_received_handlers.remove(handler)
            except ValueError:
                pass

    def set_active_sample(self, sample: DataSample) -> None:
        """Update the cached :attr:`active_sample` and invoke every
        registered handler with the new sample. Called by the
        streaming pipeline. Mirrors C# ``SetActiveSample(sample)``.

        Lock is held only over the cache update + handler-list
        snapshot — handler invocation runs OUTSIDE the lock so a
        slow handler can't block other threads from registering /
        unregistering handlers. Snapshot is a defensive copy so a
        handler that adds/removes from inside the callback doesn't
        break iteration (and the RLock allows the re-entrant
        add/remove from the same thread).
        """
        with self._sample_received_lock:
            self._active_sample = sample
            handlers = list(self._sample_received_handlers)
        for handler in handlers:
            try:
                handler(sample)
            except Exception:
                # One handler's failure must not break the others or
                # corrupt the cache that's already been updated. Log
                # so the failure is observable — silent swallowing
                # made bugs in user-supplied handlers invisible.
                _log.exception(
                    "SampleReceived handler %r raised on sample %r",
                    handler,
                    sample,
                )


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
        # Local cache of the last value successfully written via the
        # voltage setter — exposed as `output_value`. Mirrors the
        # client-side cache C# AnalogChannel.OutputValue keeps so
        # callers can ask "what did I last set?" without re-querying
        # the device. None until the first successful write.
        self._output_value: Optional[float] = None

    @property
    def index(self) -> int:
        """Get channel index"""
        return self._channel

    @property
    def output_value(self) -> Optional[float]:
        """Last voltage successfully written via the :attr:`voltage`
        setter, or ``None`` if no write has succeeded yet.

        Mirrors C# ``AnalogChannel.OutputValue`` — a client-side
        cache for "what did I last set?" that doesn't round-trip to
        the device. Use :attr:`voltage` if you want the device's
        current view.
        """
        return self._output_value

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

        # Update the local cache only after the device confirms the
        # write succeeded — `output_value` should never report a
        # value the device rejected.
        self._output_value = value

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


class AnalogInputChannel(_SampleReceivedMixin):
    """
    Single analog input (ADC) channel.

    Provides range configuration and enable/disable functionality.
    """

    def __init__(
        self,
        channel_id: int,
        comm: SerialInterface,
        variant: Optional[str] = None,
        *,
        range_cache: Optional[List[Optional[tuple]]] = None,
    ):
        """
        Initialize ADC channel.

        Args:
            channel_id: Channel index
            comm: Serial communication interface
            variant: Board variant ("NQ1", "NQ2", "NQ3") - optional
            range_cache: Single-element list serving as a shared
                mutable cache holder for the (min, max) range bounds.
                Range is module-level on NQ3 (AD7609 register), so
                channels constructed by the same collection share one
                cache so a `range = ...` write on any channel
                invalidates the bounds for all of them. When
                ``None`` (the default for solo construction), the
                channel gets its own private cache.
        """
        self._channel = channel_id
        self._comm = comm
        self._variant = variant
        # See `range_cache` arg above. Stored as a 1-element list so
        # writes from one channel are visible to siblings sharing the
        # same list.
        self._range_cache: List[Optional[tuple]] = (
            range_cache if range_cache is not None else [None]
        )
        self._init_sample_events()

    @property
    def index(self) -> int:
        """Get channel index"""
        return self._channel

    @property
    def min_value(self) -> float:
        """Minimum representable input voltage for this channel.

        Mirrors C# ``IAnalogChannel.MinValue``. For NQ1/NQ2 the
        MC12bADC has a fixed ±5V range so the value is hardcoded;
        for NQ3 the AD7609 range is configurable so the value is
        derived from the current :attr:`range` setting.
        """
        return self._range_bounds()[0]

    @property
    def max_value(self) -> float:
        """Maximum representable input voltage for this channel.

        Mirrors C# ``IAnalogChannel.MaxValue``. See :attr:`min_value`
        for variant-specific derivation.
        """
        return self._range_bounds()[1]

    def _range_bounds(self) -> tuple:
        """Return (min, max) volts for this channel based on variant
        and (for NQ3) the configured range. Centralizes the lookup so
        :attr:`min_value` and :attr:`max_value` stay in lock-step.

        Result is cached on the instance after the first SCPI-backed
        lookup (NQ3 / variant=None) and invalidated by the range
        setter — first access pays the SCPI cost; subsequent accesses
        are O(1) and guaranteed to return a consistent (min, max)
        snapshot even if the device range changes between two reads.

        NQ1/NQ2 → fixed ±5V (MC12bADC + AD7173 are both ±5V; no SCPI
        needed, so no cache layer either).
        NQ3 → range "0" → ±5V; range "1" → ±10V; anything else
        (e.g. "FIXED" or unparseable) falls back to the AD7609
        firmware default of ±10V.
        """
        if self._variant in ("NQ1", "NQ2"):
            return (-5.0, 5.0)
        if self._range_cache[0] is not None:
            return self._range_cache[0]
        try:
            r = self.range  # SCPI query — caller pays the cost on access
        except CommandError:
            # Range query failed; fall back to AD7609 firmware default
            # without caching — next access can retry the query.
            return (-10.0, 10.0)
        if r == "0":
            bounds = (-5.0, 5.0)
        elif r == "1":
            bounds = (-10.0, 10.0)
        else:
            # FIXED or any unrecognized response — AD7609 default.
            bounds = (-10.0, 10.0)
        self._range_cache[0] = bounds
        return bounds

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

        # Range is module-level on NQ3 — invalidate the shared cache
        # so every sibling channel constructed with the same
        # range_cache list also sees the next access re-query the
        # device. Sole-construction channels invalidate only their
        # private cache (the default range_cache=None gives each
        # channel its own list).
        self._range_cache[0] = None

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

        Note:
            Firmware syntax for the per-channel enable is
            ``CONFigure:ADC:CHANnel <ch>,<state>`` — comma-separated,
            NOT space-separated. Sending space (the previous behavior
            from before send_set_with_error was wired in) produced
            ``-101,"Invalid character"`` from the device. Closes the
            hardware-side gap that made #32's logic fix non-functional
            on real NQ1 firmware (3.4.6b1).
        """
        # Pre-format channel + value with the comma so the inner
        # ``send_set_with_error`` doesn't insert a space between them.
        cmd_value = f"{self._channel},{1 if value else 0}"
        try:
            success, error_response = self._comm.send_set_with_error(
                scpi.AnalogInput.ENABLE_CHANNEL_SET, cmd_value,
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


class DigitalIOChannel(_SampleReceivedMixin):
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
        # Local cache of the last state successfully written via the
        # state setter — exposed as `output_value`. Mirrors C#
        # DigitalChannel.OutputValue (the commanded bool, not the
        # live pin level). Only meaningful for channels configured
        # as outputs; None until the first successful write.
        self._output_value: Optional[bool] = None
        self._init_sample_events()

    @property
    def index(self) -> int:
        """Get channel index"""
        return self._channel

    @property
    def output_value(self) -> Optional[bool]:
        """Last state successfully written via the :attr:`state`
        setter, or ``None`` if no write has succeeded yet.

        Mirrors C# ``DigitalChannel.OutputValue`` — a client-side
        cache for "what did I last command?" that doesn't round-
        trip to the device. C# ``IsHigh`` (live pin readback) maps
        to the existing :attr:`state` getter, which queries the
        device on every access.
        """
        return self._output_value

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

        # Switching to input invalidates the cached commanded output
        # value — there's no commanded value on an input pin and a
        # subsequent re-flip back to output should start fresh.
        if value_lower == "input":
            self._output_value = None

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

        # Update the local cache only after the device confirms the
        # write succeeded — `output_value` should never report a
        # value the device rejected.
        self._output_value = value

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
            range: Input voltage range. Silently ignored on variants
                   with fixed-range ADCs (NQ1, NQ2 — MC12bADC).
            enabled: Enable channels after configuration

        Example:
            analog_inputs.configure_channels([0, 1, 2], range="5V", enabled=True)

        Note:
            Closes daqifi/daqifi-python-core#32 — on NQ1/NQ2 the range
            setter raises ``FeatureNotSupportedError`` (the hardware has
            a fixed input range), and the original implementation let
            that exception abort the loop without ever setting
            ``enabled``. ``configure_channels(..., enabled=True)`` was
            therefore fully unusable on the only variants we currently
            ship. Swallow the range-not-supported error so the enabled
            write always reaches the channel; callers that want strict
            range enforcement can call ``channel.range = ...`` directly.
        """
        from .exceptions import FeatureNotSupportedError

        for channel_id in channels:
            if 0 <= channel_id < len(self._channels):
                try:
                    self._channels[channel_id].range = range
                except FeatureNotSupportedError:
                    # NQ1/NQ2 fixed-range ADC — leave range alone, still apply enabled.
                    pass
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
