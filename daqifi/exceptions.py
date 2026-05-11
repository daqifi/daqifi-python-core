"""
Exception hierarchy for DAQiFi Python API.

Provides structured error handling for device operations.
"""

from typing import Optional, Tuple


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
    raw_device_response: Tuple[str, ...]

    #: The last SCPI error line observed in the response (e.g.
    #: ``**ERROR: -200, "Execution error"``), or ``None`` if none was
    #: present. Uses ``Optional[str]`` rather than the PEP-604 ``str | None``
    #: form so ``typing.get_type_hints()`` works on Python 3.8/3.9
    #: (the project's declared minimum is 3.8).
    last_scpi_error: Optional[str]

    def __init__(
        self,
        message: str,
        raw_device_response,
        last_scpi_error=None,
    ):
        super().__init__(message)
        # Defend against the foot-gun of passing a bare string instead
        # of an iterable-of-strings — `tuple("abc")` would yield
        # `('a','b','c')`. Treat str/bytes as a single-line response.
        if raw_device_response is None:
            raw = ()
        elif isinstance(raw_device_response, (str, bytes, bytearray)):
            raw = (raw_device_response,)
        else:
            raw = tuple(raw_device_response)
        # Coerce each line to str so the stored tuple matches the
        # Tuple[str, ...] type contract regardless of caller hygiene.
        normalized = []
        for line in raw:
            if line is None:
                normalized.append("")
            elif isinstance(line, (bytes, bytearray)):
                normalized.append(bytes(line).decode("utf-8", errors="replace"))
            else:
                normalized.append(str(line))
        self.raw_device_response = tuple(normalized)
        self.last_scpi_error = last_scpi_error


class SdCardBusyException(SdCardOperationException):
    """Raised when an SD card operation cannot proceed because the SD
    card is busy (for example, the device is actively logging to it).

    Defined for parity with upstream ``SdCardBusyException``. Not
    raised by :func:`daqifi.sdcard.throw_if_sd_card_list_error` (the
    LIST? response classifier) — upstream's ``ThrowIfSdCardListError``
    has the same shape (no busy detection on the LIST? path). Reserved
    for callers to raise on detected client-side busy conditions and
    for future firmware error-marker mappings.
    """

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
        # Actually coerce to str (not just .strip()): handles None,
        # bytes (decoded with replacement for invalid UTF-8), and any
        # other type a defensive caller might pass. .strip() on bytes
        # would have returned bytes, contradicting the str contract on
        # the class-level annotation. (Qodo /agentic_review pass 1
        # caught the partial coercion in the prior pass.)
        if device_message is None:
            device_message = ""
        elif isinstance(device_message, (bytes, bytearray)):
            device_message = bytes(device_message).decode("utf-8", errors="replace")
        else:
            device_message = str(device_message)
        device_message = device_message.strip()
        if device_message:
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
