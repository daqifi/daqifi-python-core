"""
DAQiFi Python API - Device control for Nyquist family devices.

This package provides a clean, Pythonic interface for controlling DAQiFi
Nyquist devices (NQ1, NQ2, NQ3) via serial/USB connection.

Example:
    Basic usage:
        >>> from daqifi import NyquistDevice, DeviceFinder
        >>>
        >>> # Discover devices
        >>> devices = DeviceFinder.discover()
        >>>
        >>> # Connect to device
        >>> device = NyquistDevice.connect("/dev/ttyACM0")
        >>> device.power_up()
        >>>
        >>> # Control DAC (NQ3 only)
        >>> device.analog_outputs[0].voltage = 5.0
        >>>
        >>> # Read ADC
        >>> device.analog_inputs[0].range = "10V"
        >>> device.analog_inputs[0].enabled = True
        >>>
        >>> # Control DIO
        >>> device.digital_io[0].direction = "output"
        >>> device.digital_io[0].state = True
        >>>
        >>> # Disconnect
        >>> device.disconnect()

    Context manager:
        >>> with NyquistDevice.connect("/dev/ttyACM0") as device:
        ...     device.power_up()
        ...     device.analog_outputs[0].voltage = 5.0
        ...     # Device automatically disconnects on exit

For more information, see the documentation at:
https://github.com/daqifi/daqifi-python
"""

from .version import __version__, __author__, __license__

# Core device classes
from .device import NyquistDevice
from .discovery import DeviceFinder, DeviceInfo, BoardVariant

# Channel classes
from .channels import (
    AnalogOutputChannel,
    AnalogInputChannel,
    DigitalIOChannel,
    AnalogOutputs,
    AnalogInputs,
    DigitalIO,
)

# Streaming classes
from .streaming import (
    StreamManager,
    AsyncStreamManager,
    Sample,
    SampleBuffer,
    StreamState,
)

# Power state enum (commonly used)
from ._internal.comm import PowerState

# DataSample is the type passed to per-channel sample handlers
# (channel.add_sample_received). Re-exported from the public root so
# callers don't have to reach into _internal — surfaced by Qodo
# /agentic_review pass 1 on PR #108 (#99 group B).
from ._internal.comm import DataSample

# Exception hierarchy
from .exceptions import (
    DAQiFiError,
    ConnectionError,
    DeviceNotFoundError,
    CommandError,
    ConfigurationError,
    FeatureNotSupportedError,
    PowerStateError,
)

# Public API exports
__all__ = [
    # Version info
    '__version__',
    '__author__',
    '__license__',

    # Main classes
    'NyquistDevice',
    'DeviceFinder',
    'DeviceInfo',
    'BoardVariant',
    'PowerState',
    'DataSample',

    # Channel classes
    'AnalogOutputChannel',
    'AnalogInputChannel',
    'DigitalIOChannel',
    'AnalogOutputs',
    'AnalogInputs',
    'DigitalIO',

    # Streaming classes
    'StreamManager',
    'AsyncStreamManager',
    'Sample',
    'SampleBuffer',
    'StreamState',

    # Exceptions
    'DAQiFiError',
    'ConnectionError',
    'DeviceNotFoundError',
    'CommandError',
    'ConfigurationError',
    'FeatureNotSupportedError',
    'PowerStateError',
]
