"""
Exception hierarchy for DAQiFi Python API.

Provides structured error handling for device operations.
"""

import enum
from typing import Optional, Tuple

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


class DeviceVerificationError(ConnectionError):
    """A ``verify=`` callable refused the device on the freshly opened port.

    Raised by ``NyquistDevice.connect(..., verify=...)`` when the callable
    returns a falsy value — i.e. it reached a decision and the answer was no.
    When the callable raises instead, THAT exception is preserved and
    re-raised unchanged; this type is only for a refusal with no exception of
    its own, so a caller always has something typed to catch.

    A ``ConnectionError``, not a ``SerialPortConnectError``: nothing went
    wrong with the port. It opened, the caller looked at what was behind it,
    and declined. The port is closed and the library wrote nothing to the
    device — see ``SerialInterface.open``.
    """

    def __init__(self, port: str, reason: Optional[str] = None):
        self.port = port
        self.reason = reason
        detail = f": {reason}" if reason else ""
        super().__init__(
            f"the verify callable refused the device on {port}{detail}; "
            f"the port was closed and nothing was written to it"
        )


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
    reporting the device's channels arrives within the timeout of its
    ``SYSInfoPB?``. Over TCP this is typically
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


class SerialPortConnectFailure(enum.Enum):
    """Why a serial port couldn't be opened."""

    NOT_FOUND = "not_found"
    IN_USE = "in_use"
    ACCESS_DENIED = "access_denied"
    UNKNOWN = "unknown"


class DeliveryFaultError(DAQiFiError, RuntimeError):
    """Something the device was told to do provably did not happen.

    Raised by operations that would otherwise report success over a
    pipeline known to be broken -- today that means a message consumer
    that could not be restarted after a text exchange, leaving nothing
    reading the stream. A ``RuntimeError``, like the other not-usable
    guards here. The condition is sticky by design -- it describes a
    connection that can no longer be trusted -- and a successful
    ``connect()`` clears it.
    """


class ExchangeTimeoutError(DAQiFiError, TimeoutError):
    """A send could not get exclusive use of the device in time.

    ``send()`` serialises against text exchanges, so it waits while
    another flow holds the device. This is raised when that wait cannot
    end well, and in both cases **the command was not sent**:

    * from an ordinary thread, the holder has held the device for longer
      than any legitimate exchange takes, so it is stuck rather than busy;
    * from the message-received (consumer) thread, the device is held
      right now -- and that thread never waits, because the holder may be
      waiting for it to stop the consumer, which would deadlock.

    A ``TimeoutError``, like :class:`DeviceUnresponsiveError`, so callers
    that already treat timeouts as retryable need no new except clause.
    """


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


class EchoProbeUnansweredError(SdCardOperationException):
    """The echo probe got no COMPLETE reply, so no download was attempted.

    ⛔ #260 r2. This is raised INSTEAD of downloading, and that is the point.
    The probe asks ``SYSTem:ECHO?`` before the GET to learn whether the device
    frames its replies. If the reply is absent, or arrives only partly inside
    the budget, then bytes of it MAY STILL BE IN FLIGHT -- and sending the GET
    then puts them at the front of the file. `reset_input_buffer()` cannot
    prevent that: it discards what is already BUFFERED, not what the device
    has yet to send.

    ⚠ FALLING BACK IS NOT SAFE HERE, which is the correction r1 earned. A
    half-arrived reply cannot be told from a finished one by waiting, so
    "give up and download anyway" returns a file with ``-1\r\n`` or
    ``\r\n`` glued to its front. Measured on r1: a reply split as ``-1``
    then ``\r\n`` at 120 ms produced ``b'\r\npayload'``, and a whole reply
    arriving at 650 ms produced ``b'-1\r\npayload'``.

    ⚠ THIS IS A CONTRACT CHANGE and it is deliberate. The base never probed,
    so a link that cannot answer ``SYSTem:ECHO?`` used to download anyway.
    Every firmware this library ships against registers that query, so a
    non-answer means the link is broken -- and a loud refusal beats a silently
    corrupted file.

    ⭐ A COMPLETE reply that merely does not PARSE is a different case and does
    NOT raise: it leaves nothing in flight, so the download proceeds on the
    byte-shape path exactly as before.

    Subclasses :class:`SdCardOperationException` so callers that already
    handle "my SD download failed" keep working, even though the fault is in
    the link rather than the card.

    ⛔ IT TAKES JUST A MESSAGE, and that override is not cosmetic. The base
    requires ``raw_device_response``, which is meaningless here -- the whole
    complaint is that the device did not respond. Raising it with only a
    message threw ``TypeError`` instead, and the caller's narrowed
    ``except`` swallowed that and DOWNLOADED ANYWAY, turning the r2 fix back
    into the r1 defect. A pin caught it; the lesson is that a refusal path is
    only as reliable as its own constructor.
    """

    def __init__(self, message: str, raw_device_response=(),
                 last_scpi_error=None):
        super().__init__(message, raw_device_response, last_scpi_error)


class SdCardTransferErrorException(SdCardOperationException):
    """The device reported a read error part-way through sending an SD card
    file, ending the transfer with ``__TRANSFER_ERROR__`` instead of
    ``__END_OF_FILE__`` (firmware #725). The ``bytes_received`` bytes already
    written to the destination are genuine file content, but the file is
    incomplete. Port of upstream ``SdCardTransferErrorException`` (#154).
    """

    def __init__(self, file_name: str, bytes_received: int):
        super().__init__(
            f"The device reported a read error while sending SD card file "
            f"{file_name!r} and ended the transfer after {bytes_received} bytes. "
            "Those bytes are genuine file content, but the file is incomplete; "
            "retry the download, and if it keeps failing the card or that file "
            "may be damaged.",
            (),
        )
        self.file_name = file_name
        self.bytes_received = bytes_received

    def __reduce__(self):
        # Pickle (multiprocessing) rebuilds from these, not the message.
        return (type(self), (self.file_name, self.bytes_received), self.__dict__)


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


class SdCardDeleteOutcomeUnknownException(SdCardListIncompleteException):
    """Raised when an SD card delete was sent but its outcome couldn't be
    confirmed: the reply after ``DELete`` never came back complete, or a
    transport error came after it was sent (a timeout, a dropped link; the
    cause is chained). The file may already be gone; list the card to find
    out. A delete the device refused, or a failure before ``DELete`` was
    sent, raises a definite error instead.

    A subclass of :class:`SdCardListIncompleteException`, which the delete
    raised in this case before, so existing handlers still catch it.
    """

    def __init__(self, file_name: str, raw_device_response, reason: str):
        SdCardOperationException.__init__(
            self,
            f"Deleting {file_name!r} from the SD card couldn't be confirmed "
            f"({reason}); the file may already be gone. List the card to "
            "check.",
            raw_device_response,
            None,
        )
        self.reason = reason
        self.file_name = file_name


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
