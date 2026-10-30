"""
Exception hierarchy for DAQiFi Python API.

Provides structured error handling for device operations.
"""

import enum
from typing import Optional, Sequence, Tuple

try:
    from serial import SerialException as _SerialException
except ImportError:  # pragma: no cover - pyserial is a dependency
    _SerialException = OSError


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


class DeviceUnresponsiveError(DAQiFiError, TimeoutError):
    """Transport connected but the device never reported itself.

    Raised by :meth:`DaqifiDevice.initialize` when no status reply
    reporting the device's channels arrives within the timeout, although
    ``SYSInfoPB?`` is re-sent every second. Over TCP this is typically
    the device's WiFi-SCPI parser wedged after cumulative
    connect/disconnect cycles (recovery: reboot it via USB,
    ``SYSTem:REboot``). A :class:`TimeoutError`, as upstream's
    ``TimeoutException``.
    """
    pass


class DeviceNotConnectedError(DAQiFiError, RuntimeError):
    """The operation needs a connected device and this one isn't.

    Also raised, with ``is_shutting_down`` set, when a disconnect on
    another thread tears the connection down while a send is in flight:
    an ordinary race for a long-lived sender, not a defect. A
    :class:`RuntimeError`, which these guards raised before.
    """

    def __init__(self, message: str = "Device is not connected.", *, is_shutting_down: bool = False):
        super().__init__(message)
        self.is_shutting_down = is_shutting_down

    def __reduce__(self):
        # Pickle (multiprocessing) rebuilds from args, which hold only the
        # message; the keyword travels in the state.
        return (type(self), self.args, self.__dict__)


class ScpiInitializationError(CommandError):
    """The device reported a SCPI error during :meth:`DaqifiDevice.initialize`
    that persisted after a retry, so it isn't in the state the init
    sequence sets (echo off, stream stopped, powered on, protobuf format).

    ``lines`` are the lines captured from the last attempt; ``error_line``
    is the one that reported the error.
    """

    def __init__(self, message: str, lines: Sequence[str], error_line: str):
        super().__init__(message)
        self.lines = tuple(lines)
        self.error_line = error_line

    def __reduce__(self):
        return (type(self), (str(self), self.lines, self.error_line), self.__dict__)


class InitializationUnconfirmedError(DeviceUnresponsiveError):
    """:meth:`DaqifiDevice.initialize` couldn't tell whether the device
    accepted its setup commands: the error queue couldn't be emptied
    first, or the reply to the ``SYSTem:ERRor?`` after them was missing or
    unreadable (stream bytes still arriving can garble it), on every
    attempt. A device that answers nothing at all ends here too, hence a
    :class:`DeviceUnresponsiveError`. Not a refusal, so the device isn't
    put in the Error state; retrying ``initialize()`` is reasonable.
    ``lines`` are the lines from the last attempt.
    """

    def __init__(self, message: str, lines: Sequence[str] = ()):
        super().__init__(message)
        self.lines = tuple(lines)

    def __reduce__(self):
        return (type(self), (str(self), self.lines), self.__dict__)


class SerialPortConnectFailure(enum.Enum):
    """Why a serial port couldn't be opened."""

    NOT_FOUND = "not_found"
    IN_USE = "in_use"
    ACCESS_DENIED = "access_denied"
    UNKNOWN = "unknown"


class SerialPortConnectError(ConnectionError, _SerialException):
    """A serial port couldn't be opened; ``reason`` says why
    (:class:`SerialPortConnectFailure`) and the message names the port and
    the cause. The original error is the ``__cause__``.

    Also a ``serial.SerialException``, which the open raised before, so
    existing handlers still catch it.
    """

    def __init__(self, port: str, reason: SerialPortConnectFailure, detail: str = ""):
        message = {
            SerialPortConnectFailure.NOT_FOUND: f"Serial port {port!r} was not found.",
            SerialPortConnectFailure.IN_USE: (
                f"Serial port {port!r} is in use by another program or connection."
            ),
            SerialPortConnectFailure.ACCESS_DENIED: (
                f"Access to serial port {port!r} is denied (on Linux, the user "
                "usually needs to be in the 'dialout' group)."
            ),
            SerialPortConnectFailure.UNKNOWN: f"Serial port {port!r} could not be opened.",
        }[reason]
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.port = port
        self.reason = reason
        self.detail = detail

    def __reduce__(self):
        return (type(self), (self.port, self.reason, self.detail), self.__dict__)


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
    #: form so ``typing.get_type_hints()`` works on Python 3.9
    #: (the project's declared minimum; PEP-604 in annotations needs 3.10).
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
        # Match raw_device_response's coercion shape on last_scpi_error
        # for symmetry: str/bytes both accepted, empty/None normalized
        # to None so the Optional[str] contract is consistent.
        if last_scpi_error is None:
            self.last_scpi_error = None
        elif isinstance(last_scpi_error, (bytes, bytearray)):
            s = bytes(last_scpi_error).decode("utf-8", errors="replace").strip()
            self.last_scpi_error = s or None
        else:
            s = str(last_scpi_error).strip()
            self.last_scpi_error = s or None


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


class SdCardDirectoryNotFoundException(SdCardFilesystemException):
    """Raised when the SD card's log directory doesn't exist: the firmware
    answered ``[Error:4]`` or ``[Error:5]Failed to open directory ...``
    (``SYS_FS_ERROR_NO_FILE`` / ``SYS_FS_ERROR_NO_PATH``), as on a new or
    freshly formatted card. A subclass of
    :class:`SdCardFilesystemException`, which it was raised as before."""


class SdCardListIncompleteException(SdCardOperationException):
    """Raised when an SD card listing can't be trusted as the whole card.

    Either the device's end-of-listing marker said so
    (``__END_OF_LIST__ INCOMPLETE`` or ``FAILED``, or a status word this
    version doesn't know), or the reply never reached its end: the
    ``SYSTem:ERRor?`` sent after the listing went unanswered, so a timed-out
    listing isn't mistaken for an empty card.
    """

    def __init__(self, raw_device_response, reason: str):
        super().__init__(
            f"The SD card listing is incomplete: {reason}",
            raw_device_response,
            None,
        )
        self.reason = reason


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
