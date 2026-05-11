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


class SdCardOperationException(DAQiFiError):
    """Base class for SD card operation failures returned by the device.

    Mirrors upstream ``Daqifi.Core.Device.SdCard.SdCardOperationException``
    (daqifi-core PR #182). Carries the raw response lines so callers can
    surface the underlying device output to users or logs without having
    to re-parse the wire data.
    """

    #: The captured response lines from the device when the error was
    #: classified. Useful for diagnostics and for surfacing actionable
    #: detail in higher-level UIs.
    raw_device_response: tuple

    #: The last SCPI error line observed in the response (e.g.
    #: ``**ERROR: -200, "Execution error"``), or ``None`` if none was
    #: present.
    last_scpi_error: "str | None"

    def __init__(
        self,
        message: str,
        raw_device_response,
        last_scpi_error=None,
    ):
        super().__init__(message)
        self.raw_device_response = tuple(raw_device_response or ())
        self.last_scpi_error = last_scpi_error


class SdCardBusyException(SdCardOperationException):
    """Raised when an SD card operation cannot proceed because the SD
    card is busy (for example, the device is actively logging to it)."""

    def __init__(self, raw_device_response, last_scpi_error=None):
        super().__init__(
            "The SD card is busy and cannot complete the requested operation.",
            raw_device_response,
            last_scpi_error,
        )


class SdCardFilesystemException(SdCardOperationException):
    """Raised when the device's SD card filesystem cannot satisfy a
    request — for example, the directory cannot be opened because the
    card is unformatted, the filesystem is corrupt, or the path does
    not exist. Detected from the firmware's
    ``[Error:N]Failed to open directory ...`` response.
    """

    #: The raw filesystem error line emitted by the device firmware,
    #: e.g. ``[Error:3]Failed to open directory /Daqifi``.
    device_message: str

    def __init__(self, raw_device_response, last_scpi_error, device_message):
        if device_message and device_message.strip():
            msg = f"The SD card filesystem reported an error: {device_message}"
        else:
            msg = "The SD card filesystem reported an error."
        super().__init__(msg, raw_device_response, last_scpi_error)
        self.device_message = device_message


class SdCardNotPresentException(SdCardOperationException):
    """Raised when an SD card operation fails because no SD card is
    installed in the device. Detected from the firmware's
    ``No SD Card Detected`` response."""

    def __init__(self, raw_device_response, last_scpi_error=None):
        super().__init__(
            "No SD card is installed in the device.",
            raw_device_response,
            last_scpi_error,
        )
