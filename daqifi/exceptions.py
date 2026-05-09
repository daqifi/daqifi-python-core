"""
Exception hierarchy for DAQiFi Python API.

Provides structured error handling for device operations.
"""


class DAQiFiError(Exception):
    """Base exception for all DAQiFi errors"""
    pass


class ConnectionError(DAQiFiError):
    """Device connection failed"""
    pass


class DeviceNotFoundError(DAQiFiError):
    """Device not found during discovery"""
    pass


class CommandError(DAQiFiError):
    """SCPI command failed or returned unexpected response"""
    pass


class ConfigurationError(DAQiFiError):
    """Invalid configuration parameter"""
    pass


class FeatureNotSupportedError(DAQiFiError):
    """Feature not supported on this device variant"""
    pass


class PowerStateError(DAQiFiError):
    """Device is not in required power state for operation"""
    pass


class DeviceUnresponsiveError(DAQiFiError):
    """Transport connected but the device's SCPI listener never replied.

    Raised by :meth:`DaqifiDevice.initialize` on TCP transports when
    the protobuf SYSInfoPB? response does not arrive within the
    configured timeout. The TCP socket is healthy, but the device's
    WiFi-SCPI parser has wedged — typically after cumulative
    connect/disconnect cycles in the same session. Recovery: reboot
    the device via USB (``SYSTem:REboot``).
    """
    pass
