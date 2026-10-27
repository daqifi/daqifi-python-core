"""SD card data types ported from `Daqifi.Core.Device.SdCard`.

Includes the small value classes the parser surface needs:
- `SdCardFileInfo` — name + optional creation date for one log file
- `SdCardParseProgress` — progress callback payload
- `SdCardDeviceConfiguration` — device snapshot for parsing context
- `SdCardParseOptions` — knobs the caller passes to a parse run
- `SdCardLogEntry` / `SdCardLogSession` — one parsed sample / a
  whole parsed file
- `SdCardFileParser` — protobuf log file parser (`.bin`)
- `SdCardFileParserFactory` — picks the right parser by file extension

**Divergence from upstream:**
- `SdCardDeviceConfiguration.from_device()` is intentionally
  omitted — it depends on `DaqifiDevice`, `IAnalogChannel`,
  `ChannelType` (none of which are ported yet). Callers populate
  fields directly. Reintroduce as a separate port step once those
  upstream types land.
- `SdCardParseOptions.progress` is typed `Optional[Callable[...]]`
  rather than the C# `IProgress<T>` interface. Python idiom is a
  callable; introducing a `Protocol` would be ceremony with no win.
- ``SdCardOperations`` Protocol methods are **synchronous** rather
  than ``async`` like the C# ``Task``-returning interface. The Python
  port is sync end-to-end (project convention shared with
  ``NetworkConfigurable`` etc.); concrete implementations on
  ``DaqifiStreamingDevice`` block until the device responds. Wrap
  in a thread / ``asyncio.to_thread`` if you need non-blocking
  semantics.
- ``SdCardFileParser`` and ``SdCardFileParserFactory`` are sync —
  ``parse_stream`` / ``parse_file`` block until the whole file is
  read, and ``SdCardLogSession.samples`` is an
  ``Iterator[SdCardLogEntry]`` rather than ``IAsyncEnumerable``.
- ``SdCardFileParserFactory`` only handles the protobuf format
  today; CSV and JSON variants raise ``NotImplementedError`` until
  their parsers are ported. ``detect_format`` already returns the
  right enum for ``.csv`` / ``.json``, so callers can probe the
  format without invoking a parser.
"""
from __future__ import annotations

import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from daqifi.communication import ProtobufMessageParser
from daqifi.enums import SdCardLogFormat


__all__ = [
    "SdCardFileInfo",
    "SdCardParseProgress",
    "SdCardDeviceConfiguration",
    "SdCardParseOptions",
    "SdCardTransferProgress",
    "SdCardDownloadResult",
    "SdCardOperations",
    "SdCardLogEntry",
    "SdCardLogSession",
    "SdCardFileParser",
    "SdCardCsvFileParser",
    "SdCardJsonFileParser",
    "SdCardFileParserFactory",
    "SdCardFileReceiver",
    "parse_sd_card_file_list",
    "END_OF_FILE_MARKER",
]


# ASCII marker the firmware appends after all SD-card file bytes have
# been streamed. Mirrors the C# ``SdCardFileReceiver.EndOfFileMarker``
# constant — kept module-level so test harnesses + receiver code share
# the same bytes.
END_OF_FILE_MARKER: bytes = b"__END_OF_FILE__"


_LOG_FILENAME_RE = re.compile(r"^log_(\d{8}_\d{6})", re.IGNORECASE)
_DAQIFI_DIR_PREFIX = "daqifi/"


def _try_parse_log_filename_date(file_name: str) -> Optional[datetime]:
    """Pull the timestamp out of ``log_YYYYMMDD_HHMMSS.<ext>`` names.

    Returns ``None`` for anything else. Mirrors the C#
    ``SdCardFileListParser.TryParseDateFromLogFileName`` helper.
    """
    stem, _ = os.path.splitext(file_name)
    match = _LOG_FILENAME_RE.match(stem)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _is_scpi_error_line(line: str) -> bool:
    """Strict SCPI error format: ``**ERROR...`` or ``ERROR: ...``.

    The colon (or ``**`` prefix) distinguishes a true SCPI error from
    firmware status text like ``Error !! No SD Card Detected``, which
    should not be surfaced as ``SdCardOperationException.last_scpi_error``.
    Mirrors upstream ``IsScpiErrorLine`` from PR #182.
    """
    trimmed = line.lstrip()
    upper = trimmed.upper()
    return upper.startswith("**ERROR") or upper.startswith("ERROR:")


def _is_non_result_line(line: str) -> bool:
    """Permissive: any line that looks like a device error or status
    message, including firmware text such as ``Error !! ...``.

    Used to recognize that the parser would yield no result, without
    polluting ``last_scpi_error`` with non-SCPI text.

    Mirrors upstream ``IsNonResultLine`` from PR #182 in shape, but
    tightens the prefix list to avoid false-positives on legitimate
    filenames that happen to start with ``error`` (e.g.
    ``error_log.csv`` would otherwise be misclassified as a non-result
    line and cause SdCardOperationException to fire on a real listing).
    The accepted prefixes cover every firmware-emitted error shape
    we know about: SCPI standard ``**ERROR`` / ``ERROR:``, and the
    firmware-specific status text ``Error !! ...``. Same fix should
    land upstream — Qodo /improve pass 4 importance 9 caught the
    false-positive on PR #102.
    """
    trimmed = line.lstrip()
    upper = trimmed.upper()
    return (
        upper.startswith("**ERROR")
        or upper.startswith("ERROR:")
        or upper.startswith("ERROR !!")
    )


def throw_if_sd_card_list_error(lines):
    """Inspect the final response from a ``SYSTem:STORage:SD:LISt?``
    exchange and raise a typed exception when the device reported a real
    failure (no SD card, filesystem error, generic SCPI error).

    If any non-error/non-empty line is present, callers proceed to
    parse — even if SCPI error lines are interleaved — so a successful
    directory listing is never masked by stray transient errors.

    Mirrors upstream ``ThrowIfSdCardListError`` from
    ``Daqifi.Core.Device.DaqifiStreamingDevice`` (daqifi-core PR #182).

    Raises:
        SdCardNotPresentException: device emitted ``"No SD Card Detected"``.
        SdCardFilesystemException: device emitted
            ``"Failed to open directory ..."``.
        SdCardOperationException: any other classification fallback
            (SCPI error with no content, non-result line with no SCPI error).
    """
    from .exceptions import (
        SdCardFilesystemException,
        SdCardNotPresentException,
        SdCardOperationException,
    )

    # Defensive: callers shouldn't pass None, but treat it the same as
    # an empty list (no error to classify) rather than raising
    # TypeError from list(None).
    if not lines:
        return

    lines_list = list(lines)

    # last_scpi_error must only carry a real SCPI-formatted error so
    # callers can rely on its shape. Firmware status text
    # (``Error !! ...``) is surfaced via the exception's message and
    # raw_device_response instead.
    last_scpi_error = None
    for line in reversed(lines_list):
        if _is_scpi_error_line(line):
            last_scpi_error = line.strip()
            break

    # Specific firmware-emitted error markers take precedence over
    # generic content/error checks. They're plain text (not SCPI-shaped),
    # so a simple "is there any content line?" check would otherwise
    # miss them and pass garbage to the parser.
    for line in lines_list:
        if "no sd card detected" in line.lower():
            raise SdCardNotPresentException(lines_list, last_scpi_error)

    for line in lines_list:
        if "failed to open directory" in line.lower():
            raise SdCardFilesystemException(
                lines_list, last_scpi_error, line.strip()
            )

    # If any line looks like a real result (non-empty, not an error or
    # firmware status line), hand off to the parser. Stray interleaved
    # error lines are still parsed away by parse_sd_card_file_list.
    has_content_line = any(
        line and line.strip() and not _is_non_result_line(line)
        for line in lines_list
    )
    if has_content_line:
        return

    if last_scpi_error is not None:
        raise SdCardOperationException(
            f"The SD card list operation failed: {last_scpi_error}",
            lines_list,
            last_scpi_error,
        )

    # Defensive fallback: firmware status text (``Error !! ...``) with
    # no SCPI error and no recognized marker. Shouldn't happen for known
    # firmware paths, but surfacing it as a typed exception is far
    # better than silently returning an empty list.
    for line in lines_list:
        if line and line.strip() and _is_non_result_line(line):
            raise SdCardOperationException(
                f"The SD card list operation failed: {line.strip()}",
                lines_list,
                last_scpi_error=None,
            )

    # No error lines and no content lines — empty directory. Caller
    # continues.


def parse_sd_card_file_list(lines: Iterable[str]) -> List["SdCardFileInfo"]:
    """Parse text lines from the device's SD-card LIST? response.

    Ports ``Daqifi.Core/Device/SdCard/SdCardFileListParser.cs``. Skips
    blank lines, SCPI error responses, ``DAQIFI>`` / ``OK`` noise lines
    that may have leaked past the capture path, and entries containing
    control characters. Strips a leading ``"Daqifi/"`` directory prefix
    and any trailing size column when present.

    The DAQIFI/OK skips are defense-in-depth — the
    ``_execute_raw_text_capture`` helper already strips a leading
    ``DAQIFI>`` prompt, but this function is a public parser that may
    receive lines from arbitrary callers who didn't go through that
    helper.
    """
    out: List[SdCardFileInfo] = []
    for raw in lines:
        if raw is None:
            continue
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper in ("DAQIFI>", "OK"):
            continue
        if upper.startswith("**ERROR") or upper.startswith("ERROR"):
            continue
        # Some firmwares append "<path> <size>" — keep the first whitespace token.
        token_end = -1
        for sep in (" ", "\t"):
            idx = line.find(sep)
            if idx > 0 and (token_end < 0 or idx < token_end):
                token_end = idx
        if token_end > 0:
            line = line[:token_end]
        if line.lower().startswith(_DAQIFI_DIR_PREFIX):
            line = line[len(_DAQIFI_DIR_PREFIX):]
        file_name = os.path.basename(line.replace("\\", "/"))
        if not file_name.strip():
            continue
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in file_name):
            continue
        out.append(
            SdCardFileInfo(file_name, _try_parse_log_filename_date(file_name))
        )
    return out


@dataclass(frozen=True)
class SdCardFileInfo:
    """One file on the device SD card.

    `created_date` is parsed from the filename when available; `None`
    if the parser couldn't extract it.
    """

    file_name: str
    created_date: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.file_name is None:
            raise TypeError("file_name must not be None")
        if not self.file_name.strip():
            raise ValueError("file_name must not be empty or whitespace")


@dataclass(frozen=True)
class SdCardParseProgress:
    """Progress information emitted during SD card parsing."""

    bytes_read: int
    total_bytes: int  # -1 when unknown
    messages_read: int


@dataclass(frozen=True)
class SdCardDeviceConfiguration:
    """Device snapshot used to fill in fields missing from the SD card file.

    Container fields use ``Tuple`` rather than ``List`` so the
    "frozen" dataclass guarantee actually holds — a mutable list
    inside a frozen dataclass is a footgun (Qodo PR 17 pass 10 #6).
    Callers may pass any iterable; ``__post_init__`` coerces to
    tuples for safety.
    """

    analog_port_count: int
    digital_port_count: int
    timestamp_frequency: int
    device_serial_number: Optional[str] = None
    device_part_number: Optional[str] = None
    firmware_revision: Optional[str] = None
    # (slope, intercept) per channel
    calibration_values: Optional[Tuple[Tuple[float, float], ...]] = None
    resolution: int = 0
    port_range: Optional[Tuple[float, ...]] = None
    internal_scale_m: Optional[Tuple[float, ...]] = None

    def __post_init__(self) -> None:
        # frozen=True bans normal attribute assignment, so fall
        # through object.__setattr__ to coerce iterables → tuples.
        if self.calibration_values is not None:
            coerced = tuple(tuple(p) for p in self.calibration_values)
            # Each entry must be a (slope, intercept) pair —
            # downstream code unpacks two values (Qodo PR 17 pass 18 #2).
            if any(len(pair) != 2 for pair in coerced):
                raise ValueError(
                    "calibration_values entries must be "
                    "(slope, intercept) 2-tuples"
                )
            object.__setattr__(self, "calibration_values", coerced)
        if self.port_range is not None:
            object.__setattr__(self, "port_range", tuple(self.port_range))
        if self.internal_scale_m is not None:
            object.__setattr__(self, "internal_scale_m", tuple(self.internal_scale_m))


@dataclass
class SdCardParseOptions:
    """Knobs controlling SD card log file parsing.

    Mutable on purpose — matches the C# property-setter shape and
    lets callers configure the object stepwise.
    """

    session_start_time: Optional[datetime] = None
    buffer_size: int = 64 * 1024
    progress: Optional[Callable[[SdCardParseProgress], None]] = None
    fallback_timestamp_frequency: int = 50_000_000
    configuration_override: Optional[SdCardDeviceConfiguration] = field(default=None)


@dataclass(frozen=True)
class SdCardTransferProgress:
    """Progress payload reported during an SD card file download.

    Ports the C# ``sealed record SdCardTransferProgress`` from
    ``Daqifi.Core/Device/SdCard/SdCardTransferProgress.cs``. Frozen
    because the upstream record is immutable; the download path emits
    a fresh instance per progress tick rather than mutating one.
    """

    bytes_received: int
    file_name: str


def _write_all(stream: Any, data: bytes, deadline: Optional[float] = None) -> None:
    """Write all of ``data``: a raw sink may accept only part per call.

    ``BufferedWriter``/``BytesIO`` always take everything, but a raw
    stream's ``write`` returns how much it took, and that return value was
    ignored, so a short write lost the rest (#163). ``None`` (a non-blocking
    raw stream that couldn't write) is retried. Every retry, after a short
    write as well as after ``None``, stops at ``deadline``
    (``time.monotonic()`` value): a sink that drains slowly or not at all
    must not hold a download, and the exchange lock with it, past its
    timeout.
    """
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is not None and written <= 0:
            raise OSError(f"write accepted {written} bytes; destination is not writable")
        if written is not None:
            view = view[written:]
            if not view:
                return
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(
                "The download destination did not accept all the data before "
                "the download's deadline."
            )
        if written is None:
            time.sleep(0.001)



class SdCardFileReceiver:
    """Receives raw bytes from a transport stream during an SD-card
    file download, accumulating into a destination stream and
    detecting the ``__END_OF_FILE__`` marker that signals transfer
    completion.

    Ports ``Daqifi.Core/Device/SdCard/SdCardFileReceiver.cs``. Sync
    end-to-end per project convention (the C# original is async, but
    the device-side flow is single-threaded SCPI). The marker
    detection scans across chunk boundaries via a trailing-byte
    window, so payloads that split the marker between two reads are
    handled correctly.

    Typical usage:

    .. code-block:: python

        receiver = SdCardFileReceiver(transport_stream)
        with open("dest.bin", "wb") as out:
            n = receiver.receive(out, "log.bin", timeout_s=60.0)
        # n == bytes written to dest.bin (excludes the marker).

    Distinct from the inline EOF-marker scan in
    :meth:`DaqifiStreamingDevice.download_sd_card_file` — this class
    accepts any byte-yielding stream so it can be reused outside
    the streaming-device flow (tests, alternative transports, etc).
    """

    _DEFAULT_BUFFER_SIZE = 16 * 1024
    _DEFAULT_TIMEOUT_S = 30 * 60  # 30 minutes — matches the C# default
    _IDLE_SLEEP_S = 0.01

    def __init__(
        self,
        source_stream: Any,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
    ) -> None:
        if source_stream is None:
            raise TypeError("source_stream must not be None")
        if buffer_size <= 0:
            raise ValueError("buffer_size must be greater than zero")
        self._source = source_stream
        self._buffer_size = buffer_size
        # Bytes read past the EOF marker — preserved here so callers
        # sharing the source stream (e.g. a serial transport that
        # multiplexes file download + streaming-status frames) can
        # avoid silent data loss. Empty until ``receive`` finds the
        # marker; then exposes anything after the marker via
        # :attr:`leftover`.
        self._leftover: bytes = b""

    @property
    def leftover(self) -> bytes:
        """Bytes read past the EOF marker during the most recent
        :meth:`receive` call.

        Empty until ``receive`` finds the marker. After receive
        returns, this holds any bytes that arrived in the final
        chunk after the marker — callers that share the source
        stream with subsequent reads should prepend this to their
        next read so multi-byte protocol frames don't get split.
        """
        return self._leftover

    def receive(
        self,
        destination_stream: Any,
        file_name: str,
        progress: Optional[Callable[[SdCardTransferProgress], None]] = None,
        timeout_s: Optional[float] = None,
    ) -> int:
        """Read until the EOF marker, write everything before it to
        ``destination_stream``, and return the total bytes written.

        Args:
            destination_stream: a writable stream (must accept
                ``write(bytes)``).
            file_name: the file name — used for progress callback
                payloads.
            progress: optional callback fired after each write
                (matches the C# ``IProgress<SdCardTransferProgress>``
                pattern). Callback failures are swallowed.
            timeout_s: wall-clock cap. Defaults to 30 minutes (matches
                upstream). ``None`` uses the default; pass an
                explicit value to override.

        Returns:
            Total bytes written to ``destination_stream`` (the EOF
            marker itself is NOT written).

        Raises:
            TypeError: if ``destination_stream`` or ``file_name`` is None.
            ValueError: if ``timeout_s`` is non-positive.
            TimeoutError: if the marker isn't received within
                ``timeout_s`` OR the source stream is closed before
                the marker arrives.

        Read-loop strategy:
            Two source-stream shapes are supported. If the source
            exposes ``in_waiting`` (pyserial-style), we poll it and
            only call ``read(min(in_waiting, buffer_size))`` when
            there are pending bytes — this avoids ``read()`` blocking
            longer than the requested ``timeout_s``. When ``in_waiting``
            is 0 we sleep briefly and retry until the deadline.

            Sources without ``in_waiting`` (BytesIO and friends)
            fall back to direct ``read(buffer_size)``; an empty
            return means the stream is exhausted and we surface
            it as ``TimeoutError("closed before receiving...")``.
            BytesIO is unambiguous about EOF, but pyserial isn't,
            which is why the in_waiting branch exists.
        """
        if destination_stream is None:
            raise TypeError("destination_stream must not be None")
        if file_name is None:
            raise TypeError("file_name must not be None")

        effective_timeout = (
            timeout_s if timeout_s is not None else self._DEFAULT_TIMEOUT_S
        )
        # ``effective_timeout != effective_timeout`` is the canonical
        # NaN check that doesn't require importing math here. NaN
        # would slip past ``<= 0`` (NaN comparisons are always False)
        # but make the deadline NaN, which then makes every
        # ``time.monotonic() >= deadline`` comparison False — the
        # loop would hang forever.
        if effective_timeout != effective_timeout or effective_timeout <= 0:
            raise ValueError(
                f"timeout_s must be a positive finite number (got {effective_timeout!r})"
            )

        marker_len = len(END_OF_FILE_MARKER)
        deadline = time.monotonic() + effective_timeout
        has_in_waiting = hasattr(self._source, "in_waiting")
        # Reset leftover so a re-used receiver doesn't expose stale
        # bytes from the previous receive() call.
        self._leftover = b""

        # Trailing-byte window so the marker can be detected even
        # when split across chunk boundaries — matches the upstream
        # implementation. Sized to exactly the marker length.
        trailing = bytearray()
        total_bytes = 0

        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"SD card file download timed out after "
                    f"{effective_timeout:.3f} seconds. Received "
                    f"{total_bytes} bytes before timeout."
                )

            if has_in_waiting:
                pending = self._source.in_waiting
                if not pending:
                    time.sleep(self._IDLE_SLEEP_S)
                    continue
                read_n = min(pending, self._buffer_size)
                chunk = self._source.read(read_n)
                if not chunk:
                    # Backed-off pyserial read: in_waiting reported >0
                    # but read returned nothing. Treat as transient and
                    # let the deadline check decide when to give up.
                    time.sleep(self._IDLE_SLEEP_S)
                    continue
            else:
                # BytesIO/SpooledTemporaryFile/etc — empty read is
                # unambiguous EOF (no in-band "still open but idle"
                # state), so treat as stream-closed.
                try:
                    chunk = self._source.read(self._buffer_size)
                except socket.timeout:
                    # A TCP stream's bounded read found nothing yet; the
                    # deadline above decides when to give up.
                    continue
                if not chunk:
                    raise TimeoutError(
                        f"Transport stream closed before receiving the EOF "
                        f"marker. Received {total_bytes} bytes."
                    )

            chunk_bytes = bytes(chunk)
            found, position = _find_eof_marker(trailing, chunk_bytes)
            if found:
                # ``position`` is in the virtual [trailing + chunk] view.
                # Write everything before it; preserve everything after
                # in ``self._leftover`` so callers sharing the source
                # stream can avoid silent data loss.
                marker_end_in_combined = position + marker_len
                # Index in the chunk where the marker ends. If the
                # marker started in trailing, this is < len(chunk) when
                # the marker spans the boundary.
                marker_end_in_chunk = marker_end_in_combined - len(trailing)
                if position < len(trailing):
                    # Marker started inside the trailing buffer — only
                    # write the part of trailing before the marker.
                    if position > 0:
                        _write_all(destination_stream, bytes(trailing[:position]), deadline)
                        total_bytes += position
                else:
                    # Marker started inside the new chunk — write all
                    # of trailing + the part of chunk before the marker.
                    if trailing:
                        _write_all(destination_stream, bytes(trailing), deadline)
                        total_bytes += len(trailing)
                    marker_start_in_chunk = position - len(trailing)
                    if marker_start_in_chunk > 0:
                        _write_all(destination_stream, chunk_bytes[:marker_start_in_chunk], deadline)
                        total_bytes += marker_start_in_chunk
                # Capture any bytes after the marker for the caller.
                if 0 <= marker_end_in_chunk < len(chunk_bytes):
                    self._leftover = chunk_bytes[marker_end_in_chunk:]
                else:
                    self._leftover = b""
                _emit_transfer_progress(progress, total_bytes, file_name)
                return total_bytes

            # No marker yet — write everything except the last
            # markerLength bytes (those become the new trailing window
            # for next iteration's spanning check).
            combined_len = len(trailing) + len(chunk_bytes)
            if combined_len >= marker_len:
                safe_count = combined_len - marker_len
                if safe_count > 0:
                    from_trailing = min(safe_count, len(trailing))
                    if from_trailing > 0:
                        _write_all(destination_stream, bytes(trailing[:from_trailing]), deadline)
                        total_bytes += from_trailing
                    from_new = safe_count - from_trailing
                    if from_new > 0:
                        _write_all(destination_stream, chunk_bytes[:from_new], deadline)
                        total_bytes += from_new
                # Rebuild trailing window: last marker_len bytes of
                # the combined [trailing + chunk] sequence.
                from_new_data = min(len(chunk_bytes), marker_len)
                from_trailing_data = marker_len - from_new_data
                new_trailing = bytearray()
                if from_trailing_data > 0:
                    new_trailing.extend(
                        trailing[len(trailing) - from_trailing_data:]
                    )
                if from_new_data > 0:
                    new_trailing.extend(
                        chunk_bytes[len(chunk_bytes) - from_new_data:]
                    )
                trailing = new_trailing
            else:
                # Combined data still shorter than the marker —
                # accumulate and keep waiting.
                trailing.extend(chunk_bytes)
            _emit_transfer_progress(progress, total_bytes, file_name)


def _find_eof_marker(
    trailing: bytes, chunk: bytes
) -> Tuple[bool, int]:
    """Scan the virtual ``trailing + chunk`` buffer for the EOF marker.

    Returns ``(found, position)`` where ``position`` is the marker
    start index in the combined view. The marker can span the
    boundary, so the search starts at ``max(0, len(trailing) -
    len(marker) + 1)`` and ends at ``len(combined) - len(marker)``.
    Mirrors the upstream ``FindEndOfFileMarker``.
    """
    marker = END_OF_FILE_MARKER
    marker_len = len(marker)
    combined_len = len(trailing) + len(chunk)
    if combined_len < marker_len:
        return False, -1

    search_start = max(0, len(trailing) - marker_len + 1)
    search_end = combined_len - marker_len

    for pos in range(search_start, search_end + 1):
        match = True
        for j in range(marker_len):
            idx = pos + j
            if idx < len(trailing):
                b = trailing[idx]
            else:
                b = chunk[idx - len(trailing)]
            if b != marker[j]:
                match = False
                break
        if match:
            return True, pos
    return False, -1


def _emit_transfer_progress(
    progress: Optional[Callable[[SdCardTransferProgress], None]],
    bytes_received: int,
    file_name: str,
) -> None:
    """Best-effort SdCardTransferProgress callback. Mirrors the C#
    IProgress<T> pattern: any callback failure is swallowed so a
    buggy progress handler can't abort an otherwise-successful
    transfer."""
    if progress is None:
        return
    try:
        progress(SdCardTransferProgress(
            bytes_received=bytes_received,
            file_name=file_name,
        ))
    except Exception:
        pass


@dataclass(frozen=True)
class SdCardDownloadResult:
    """Metadata returned at the end of an SD card file download.

    Ports the C# ``sealed record SdCardDownloadResult`` from
    ``Daqifi.Core/Device/SdCard/SdCardDownloadResult.cs``. ``file_path``
    is ``None`` when the file was streamed to a caller-provided buffer
    (no on-disk artifact); set when the convenience overload writes to
    a temporary file.
    """

    file_name: str
    file_size: int
    duration: timedelta
    file_path: Optional[str] = None


@runtime_checkable
class SdCardOperations(Protocol):
    """Protocol for SD-card operations on a DAQiFi device.

    Pythonic name for the C# ``ISdCardOperations`` interface from
    ``Daqifi.Core/Device/SdCard/ISdCardOperations.cs`` (the ``I``
    prefix is dropped to match the rest of this codebase's protocols).

    All methods are **synchronous** — see module docstring. The
    download convenience overload from C# (no destination stream) is
    surfaced as :meth:`download_sd_card_file_to_temp` so the two
    overloads have distinct names — Python doesn't dispatch on
    parameter types like C# does.
    """

    @property
    def is_logging_to_sd_card(self) -> bool:
        """True while the device is actively logging to its SD card."""
        ...

    @property
    def sd_card_files(self) -> Sequence[SdCardFileInfo]:
        """Most recently retrieved file listing.

        Read-only sequence — implementations should return a tuple or
        an immutable view rather than a live list.
        """
        ...

    def get_sd_card_files(self) -> Sequence[SdCardFileInfo]:
        """Fetch and return the current list of files on the SD card.

        Raises:
            RuntimeError: if the device is not connected.
        """
        ...

    def start_sd_card_logging(
        self,
        file_name: Optional[str] = None,
        channel_mask: Optional[str] = None,
        format: SdCardLogFormat = SdCardLogFormat.PROTOBUF,
    ) -> None:
        """Begin logging to the SD card.

        Args:
            file_name: Output file name. If empty/``None``, the device
                generates a timestamped name like ``log_YYYYMMDD_HHMMSS``
                with an extension matching ``format`` (``.bin`` for
                Protobuf, ``.json`` for JSON, ``.csv`` for CSV).
            channel_mask: Optional decimal bitmask string enabling
                specific ADC channels (each bit = one channel). When
                empty/``None`` the current device channel configuration
                is used.
            format: Logging format — protobuf, JSON, or CSV.

        Raises:
            RuntimeError: if the device is not connected.
        """
        ...

    def stop_sd_card_logging(self) -> None:
        """Stop the active SD card logging session.

        Raises:
            RuntimeError: if the device is not connected.
        """
        ...

    def delete_sd_card_file(self, file_name: str) -> None:
        """Delete ``file_name`` from the SD card.

        Raises:
            RuntimeError: if the device is not connected or is currently
                logging to the SD card.
            ValueError: if ``file_name`` is empty or contains invalid
                characters.
        """
        ...

    def format_sd_card(self) -> None:
        """Erase the entire SD card.

        Raises:
            RuntimeError: if the device is not connected or is currently
                logging.
        """
        ...

    def download_sd_card_file(
        self,
        file_name: str,
        destination_stream: Any,
        progress: Optional[Callable[[SdCardTransferProgress], None]] = None,
    ) -> SdCardDownloadResult:
        """Stream a file from the device's SD card into ``destination_stream``.

        ``destination_stream`` is any binary-writable file-like object
        (anything supporting ``.write(bytes) -> int``) — keeps the
        contract duck-typed rather than tying it to ``BinaryIO``.
        ``progress``, if provided, is called with one
        :class:`SdCardTransferProgress` per chunk.

        Raises:
            RuntimeError: if the device is not connected or is not
                using a USB/serial transport.
            ValueError: if ``file_name`` is empty or invalid.
        """
        ...

    def download_sd_card_file_to_temp(
        self,
        file_name: str,
        progress: Optional[Callable[[SdCardTransferProgress], None]] = None,
    ) -> SdCardDownloadResult:
        """Download a file to a temporary on-disk location.

        Convenience overload of :meth:`download_sd_card_file` that
        manages the destination file itself; the resulting
        :class:`SdCardDownloadResult` carries the local path in
        ``file_path``. Renamed from the C# overload because Python
        can't dispatch on parameter types.

        Raises:
            RuntimeError: if the device is not connected or is not
                using a USB/serial transport.
            ValueError: if ``file_name`` is empty or invalid.
        """
        ...


@dataclass(frozen=True)
class SdCardLogEntry:
    """One sample row read from an SD-card log file.

    Ports the C# ``sealed record SdCardLogEntry`` from
    ``Daqifi.Core/Device/SdCard/SdCardLogEntry.cs``. Frozen because
    the upstream record is immutable and the parser yields a fresh
    instance per sample.
    """

    timestamp: datetime
    analog_values: Tuple[float, ...]
    digital_data: int
    analog_timestamps: Optional[Tuple[int, ...]] = None

    def __post_init__(self) -> None:
        # Coerce the iterable inputs to tuples so mutating the
        # caller's source list after construction can't change the
        # entry — matches the frozen-dataclass guarantee already
        # used by ``SdCardDeviceConfiguration``.
        object.__setattr__(self, "analog_values", tuple(self.analog_values))
        if self.analog_timestamps is not None:
            object.__setattr__(
                self, "analog_timestamps", tuple(self.analog_timestamps)
            )


class SdCardLogSession:
    """A whole parsed SD-card log file.

    Ports ``Daqifi.Core/Device/SdCard/SdCardLogSession.cs``.
    ``samples`` is an ``Iterator[SdCardLogEntry]`` (sync) rather than
    ``IAsyncEnumerable<SdCardLogEntry>`` to match this project's
    sync-end-to-end convention. Iterate to drive parsing forward.
    """

    def __init__(
        self,
        file_name: str,
        file_created_date: Optional[datetime],
        device_config: Optional[SdCardDeviceConfiguration],
        samples: Iterator[SdCardLogEntry],
    ) -> None:
        if file_name is None:
            raise TypeError("file_name must not be None")
        if samples is None:
            raise TypeError("samples must not be None")
        self.file_name = file_name
        self.file_created_date = file_created_date
        self.device_config = device_config
        self.samples = samples


def _has_stream_payload(message: Any) -> bool:
    return (
        len(message.analog_in_data) > 0
        or len(message.analog_in_data_float) > 0
        or len(message.digital_data) > 0
    )


def _strip_end_of_file_marker(data: bytes) -> bytes:
    if len(data) < len(END_OF_FILE_MARKER):
        return data
    if data.endswith(END_OF_FILE_MARKER):
        return data[: -len(END_OF_FILE_MARKER)]
    return data


def _compute_tick_delta(previous: int, current: int) -> int:
    """Tick delta between two uint32 timestamps with rollover."""
    if current >= previous:
        return current - previous
    # Rollover: ticks remaining to UINT32_MAX + current + 1
    return (0xFFFFFFFF - previous) + current + 1


def _scale_raw_analog_values(
    raw_values: Sequence[int],
    config: Optional[SdCardDeviceConfiguration],
) -> Tuple[float, ...]:
    """Apply calibration/range/scale factors to raw ADC integers.

    Formula mirrors the upstream ``SdCardFileParser.ScaleRawAnalogValues``:
    ``(raw / resolution * port_range * cal_m + cal_b) * internal_scale_m``.
    Returns the raw values cast to ``float`` when no usable config is
    available.
    """
    if config is None or config.resolution == 0:
        return tuple(float(v) for v in raw_values)

    resolution = float(config.resolution)
    cal = config.calibration_values
    port_range = config.port_range
    int_scale = config.internal_scale_m

    out: List[float] = []
    for ch, raw in enumerate(raw_values):
        cal_m, cal_b = (cal[ch] if cal is not None and ch < len(cal) else (1.0, 0.0))
        rng = (
            port_range[ch] if port_range is not None and ch < len(port_range) else 1.0
        )
        scale_m = (
            int_scale[ch] if int_scale is not None and ch < len(int_scale) else 1.0
        )
        normalized = raw / resolution
        out.append((normalized * rng * cal_m + cal_b) * scale_m)
    return tuple(out)


def _extract_device_configuration(
    status_message: Any,
) -> SdCardDeviceConfiguration:
    """Build a :class:`SdCardDeviceConfiguration` from a status frame."""
    cal_m = list(status_message.analog_in_cal_m)
    cal_b = list(status_message.analog_in_cal_b)
    calibration: Optional[Tuple[Tuple[float, float], ...]] = None
    if cal_m and cal_b:
        count = min(len(cal_m), len(cal_b))
        calibration = tuple((cal_m[i], cal_b[i]) for i in range(count))

    port_range: Optional[Tuple[float, ...]] = (
        tuple(float(v) for v in status_message.analog_in_port_range)
        if len(status_message.analog_in_port_range) > 0
        else None
    )
    internal_scale_m: Optional[Tuple[float, ...]] = (
        tuple(float(v) for v in status_message.analog_in_int_scale_m)
        if len(status_message.analog_in_int_scale_m) > 0
        else None
    )

    return SdCardDeviceConfiguration(
        analog_port_count=int(status_message.analog_in_port_num),
        digital_port_count=int(status_message.digital_port_num),
        timestamp_frequency=int(status_message.timestamp_freq),
        device_serial_number=(
            str(status_message.device_sn) if status_message.device_sn != 0 else None
        ),
        device_part_number=status_message.device_pn or None,
        firmware_revision=status_message.device_fw_rev or None,
        calibration_values=calibration,
        resolution=int(status_message.analog_in_res),
        port_range=port_range,
        internal_scale_m=internal_scale_m,
    )


def _scan_messages_for_configuration(
    messages: Sequence[Any],
) -> Optional[SdCardDeviceConfiguration]:
    """Walk every message looking for the first non-zero value of
    each config field.

    Some firmware versions embed config fields (``timestamp_freq``,
    ``device_sn``, calibration arrays, …) inside streaming data
    messages rather than writing a dedicated status header. Mirrors
    the upstream ``ScanMessagesForConfiguration`` exit-early
    behavior — we stop scanning once every tracked field has been
    populated.
    """
    timestamp_freq = 0
    analog_port_num = 0
    digital_port_num = 0
    device_sn = 0
    device_pn: Optional[str] = None
    fw_rev: Optional[str] = None
    calibration: Optional[Tuple[Tuple[float, float], ...]] = None
    resolution = 0
    port_range: Optional[Tuple[float, ...]] = None
    internal_scale_m: Optional[Tuple[float, ...]] = None

    for msg in messages:
        if timestamp_freq == 0 and msg.timestamp_freq != 0:
            timestamp_freq = int(msg.timestamp_freq)
        if analog_port_num == 0 and msg.analog_in_port_num != 0:
            analog_port_num = int(msg.analog_in_port_num)
        if digital_port_num == 0 and msg.digital_port_num != 0:
            digital_port_num = int(msg.digital_port_num)
        if device_sn == 0 and msg.device_sn != 0:
            device_sn = int(msg.device_sn)
        if device_pn is None and msg.device_pn:
            device_pn = msg.device_pn
        if fw_rev is None and msg.device_fw_rev:
            fw_rev = msg.device_fw_rev
        if (
            calibration is None
            and len(msg.analog_in_cal_m) > 0
            and len(msg.analog_in_cal_b) > 0
        ):
            count = min(len(msg.analog_in_cal_m), len(msg.analog_in_cal_b))
            calibration = tuple(
                (float(msg.analog_in_cal_m[i]), float(msg.analog_in_cal_b[i]))
                for i in range(count)
            )
        if resolution == 0 and msg.analog_in_res != 0:
            resolution = int(msg.analog_in_res)
        if port_range is None and len(msg.analog_in_port_range) > 0:
            port_range = tuple(float(v) for v in msg.analog_in_port_range)
        if internal_scale_m is None and len(msg.analog_in_int_scale_m) > 0:
            internal_scale_m = tuple(float(v) for v in msg.analog_in_int_scale_m)

        if (
            timestamp_freq != 0
            and analog_port_num != 0
            and digital_port_num != 0
            and device_sn != 0
            and device_pn is not None
            and fw_rev is not None
            and calibration is not None
            and resolution != 0
            and port_range is not None
            and internal_scale_m is not None
        ):
            break

    if (
        timestamp_freq == 0
        and analog_port_num == 0
        and digital_port_num == 0
        and device_sn == 0
        and device_pn is None
        and fw_rev is None
        and calibration is None
        and resolution == 0
        and port_range is None
        and internal_scale_m is None
    ):
        return None

    return SdCardDeviceConfiguration(
        analog_port_count=analog_port_num,
        digital_port_count=digital_port_num,
        timestamp_frequency=timestamp_freq,
        device_serial_number=str(device_sn) if device_sn != 0 else None,
        device_part_number=device_pn,
        firmware_revision=fw_rev,
        calibration_values=calibration,
        resolution=resolution,
        port_range=port_range,
        internal_scale_m=internal_scale_m,
    )


def _merge_configurations(
    primary: Optional[SdCardDeviceConfiguration],
    fallback: SdCardDeviceConfiguration,
) -> SdCardDeviceConfiguration:
    """Per-field non-zero/non-None preference of ``primary`` over
    ``fallback`` (mirrors the upstream ``MergeConfigurations``)."""
    if primary is None:
        return fallback

    return SdCardDeviceConfiguration(
        analog_port_count=(
            primary.analog_port_count
            if primary.analog_port_count != 0
            else fallback.analog_port_count
        ),
        digital_port_count=(
            primary.digital_port_count
            if primary.digital_port_count != 0
            else fallback.digital_port_count
        ),
        timestamp_frequency=(
            primary.timestamp_frequency
            if primary.timestamp_frequency != 0
            else fallback.timestamp_frequency
        ),
        device_serial_number=(
            primary.device_serial_number or fallback.device_serial_number
        ),
        device_part_number=(
            primary.device_part_number or fallback.device_part_number
        ),
        firmware_revision=primary.firmware_revision or fallback.firmware_revision,
        calibration_values=(
            primary.calibration_values
            if primary.calibration_values is not None
            else fallback.calibration_values
        ),
        resolution=primary.resolution if primary.resolution != 0 else fallback.resolution,
        port_range=(
            primary.port_range if primary.port_range is not None else fallback.port_range
        ),
        internal_scale_m=(
            primary.internal_scale_m
            if primary.internal_scale_m is not None
            else fallback.internal_scale_m
        ),
    )


def _read_all_messages(
    stream: Any,
    options: SdCardParseOptions,
) -> List[Any]:
    """Drain ``stream`` into a list of ``DaqifiOutMessage`` objects.

    Reuses :class:`ProtobufMessageParser` (the same length-prefixed
    reader the live transport uses), wired to
    ``DaqifiOutMessage.FromString``. Carries unconsumed bytes between
    chunks so a frame straddling a buffer boundary still parses.
    """
    # Lazy import — keeps this MODULE importable where the generated protobuf
    # modules aren't available (tests can monkeypatch the factory directly via
    # ``parser._message_factory``).
    #
    # ⚠ IT DOES NOT MAKE THEIR ABSENCE SURVIVABLE, and an earlier revision of
    # this comment read as though it did. The claim is true about IMPORT TIME
    # and only about import time: reaching this line without protobuf installed
    # raises ModuleNotFoundError. Deferring the import moves the failure from
    # `import daqifi` to first use of this feature, which is later and harder
    # to read, not softer.
    #
    # Four of the five deferred proto imports in this package are bare like
    # this one (here, device.py, devices.py, streaming.py); only
    # device_finders.py guards its own with try/except and a documented
    # fallback. So `protobuf` is a hard runtime requirement and is declared as
    # one in both setup.py and requirements.txt -- whose agreement is enforced
    # by tests/test_dependency_declarations.py, because it was the
    # DISAGREEMENT between them that let a broken install path ship.
    from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage

    parser = ProtobufMessageParser(message_factory=DaqifiOutMessage.FromString)
    messages: List[Any] = []
    carry = b""
    total_bytes_read = 0
    try:
        total_bytes = os.fstat(stream.fileno()).st_size
    except (AttributeError, OSError):
        total_bytes = -1

    while True:
        chunk = stream.read(options.buffer_size)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            # Text streams would otherwise blow up later in `carry +
            # chunk` with an opaque "can't concat str to bytes" — fail
            # at the source with a message that names the actual fix.
            raise TypeError(
                "file_stream must be opened in binary mode "
                "(read() must return bytes / bytearray)"
            )
        # Normalize to bytes so downstream slicing/comparisons stay
        # type-stable across iterations regardless of whether the
        # stream returns ``bytes`` or ``bytearray``.
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        total_bytes_read += len(chunk)

        combined = carry + chunk
        # Only strip the EOF marker on what is plausibly the final
        # chunk — testing every chunk would be wasteful, but the
        # upstream parser strips on every iteration as a defensive
        # measure against unexpectedly-short reads. We mirror that.
        combined = _strip_end_of_file_marker(combined)

        parsed, consumed = parser.parse_messages(combined)
        for inbound in parsed:
            messages.append(inbound.data)

        carry = combined[consumed:] if consumed < len(combined) else b""

        if options.progress is not None:
            options.progress(
                SdCardParseProgress(
                    bytes_read=total_bytes_read,
                    total_bytes=total_bytes,
                    messages_read=len(messages),
                )
            )

    if carry:
        parsed, _ = parser.parse_messages(carry)
        for inbound in parsed:
            messages.append(inbound.data)
        if options.progress is not None:
            options.progress(
                SdCardParseProgress(
                    bytes_read=total_bytes_read,
                    total_bytes=total_bytes,
                    messages_read=len(messages),
                )
            )

    return messages


def _produce_samples(
    messages: List[Any],
    start_index: int,
    anchor_time: Optional[datetime],
    tick_period: float,
    config: Optional[SdCardDeviceConfiguration],
) -> Iterator[SdCardLogEntry]:
    # Tz-naive UTC for parity with the C# port's DateTime.UtcNow.
    # ``utcnow()`` is deprecated; build the same tz-naive value via
    # ``now(timezone.utc).replace(tzinfo=None)``. If a caller hands
    # us a tz-aware anchor, normalize to UTC then drop the tzinfo so
    # ``base_time + timedelta(...)`` produces tz-naive entries that
    # compare cleanly with anything else in the parser pipeline.
    if anchor_time is None:
        base_time = datetime.now(timezone.utc).replace(tzinfo=None)
    elif anchor_time.tzinfo is not None:
        base_time = anchor_time.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        base_time = anchor_time
    previous_timestamp: Optional[int] = None
    elapsed_seconds = 0.0

    i = start_index
    while i < len(messages):
        msg = messages[i]
        if not _has_stream_payload(msg):
            i += 1
            continue

        # Merge consecutive messages sharing the same MsgTimeStamp into
        # one entry — firmware often splits analog and digital data
        # across separate frames at the same tick.
        analog_float = list(msg.analog_in_data_float)
        analog_raw = list(msg.analog_in_data)
        digital = bytes(msg.digital_data)
        analog_ts = list(msg.analog_in_data_ts)

        while i + 1 < len(messages) and messages[i + 1].msg_time_stamp == msg.msg_time_stamp:
            nxt = messages[i + 1]
            if not _has_stream_payload(nxt):
                break
            i += 1
            if not analog_float and len(nxt.analog_in_data_float) > 0:
                analog_float = list(nxt.analog_in_data_float)
            elif not analog_raw and len(nxt.analog_in_data) > 0:
                analog_raw = list(nxt.analog_in_data)
            if not digital and len(nxt.digital_data) > 0:
                digital = bytes(nxt.digital_data)
            if not analog_ts and len(nxt.analog_in_data_ts) > 0:
                analog_ts = list(nxt.analog_in_data_ts)

        # Reconstruct the absolute timestamp from the device's tick
        # counter. The first stream message anchors elapsed_seconds=0;
        # subsequent messages add (delta_ticks * tick_period). When
        # tick_period is 0 (no timestamp_freq known) every entry just
        # gets the base time — matches the upstream behavior.
        timestamp = base_time
        if msg.msg_time_stamp != 0 and tick_period > 0:
            if previous_timestamp is None:
                previous_timestamp = int(msg.msg_time_stamp)
            else:
                delta = _compute_tick_delta(previous_timestamp, int(msg.msg_time_stamp))
                elapsed_seconds += delta * tick_period
                previous_timestamp = int(msg.msg_time_stamp)
            timestamp = base_time + timedelta(seconds=elapsed_seconds)

        if analog_float:
            analog_values: Tuple[float, ...] = tuple(float(v) for v in analog_float)
        elif analog_raw:
            analog_values = _scale_raw_analog_values(analog_raw, config)
        else:
            analog_values = ()

        digital_data = 0
        if digital:
            for b_index, byte_val in enumerate(digital[:4]):
                digital_data |= (byte_val & 0xFF) << (b_index * 8)

        analog_timestamps: Optional[Tuple[int, ...]] = (
            tuple(int(v) for v in analog_ts) if analog_ts else None
        )

        yield SdCardLogEntry(
            timestamp=timestamp,
            analog_values=analog_values,
            digital_data=digital_data,
            analog_timestamps=analog_timestamps,
        )
        i += 1


class SdCardFileParser:
    """Parses SD-card ``.bin`` log files (varint-prefixed protobuf).

    Ports ``Daqifi.Core/Device/SdCard/SdCardFileParser.cs``. Sync —
    see module docstring divergence note. ``parse_stream`` reads the
    entire input before yielding samples (matches upstream, which
    materializes the message list before iterating); the returned
    :class:`SdCardLogSession` exposes samples as an
    ``Iterator[SdCardLogEntry]`` so the caller still pays per-sample
    cost lazily.
    """

    def parse_stream(
        self,
        file_stream: Any,
        file_name: str,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        if file_stream is None:
            raise TypeError("file_stream must not be None")
        if file_name is None:
            raise TypeError("file_name must not be None")

        options = options if options is not None else SdCardParseOptions()
        if options.buffer_size <= 0:
            raise ValueError("options.buffer_size must be greater than zero")

        # The filename-date regex is anchored at ``^log_`` against the
        # stem of ``file_name``. ``os.path.splitext("/tmp/log_x.bin")``
        # keeps the directory in the stem, so a path-like input would
        # silently fail to extract its embedded date. Normalize once
        # here so callers can pass either a basename or a full path.
        # Replace ``\\`` first so Windows-style paths split correctly
        # on POSIX hosts (mirrors the existing pattern in
        # parse_sd_card_file_list).
        file_name = os.path.basename(file_name.replace("\\", "/"))

        file_created_date = options.session_start_time or _try_parse_log_filename_date(
            file_name
        )

        all_messages = _read_all_messages(file_stream, options)

        config: Optional[SdCardDeviceConfiguration] = None
        stream_start_index = 0

        # First message is treated as a status frame only when it has
        # no stream payload — some firmware embeds config fields
        # inside streaming messages and we can't lose those samples.
        if all_messages and not _has_stream_payload(all_messages[0]):
            config = _extract_device_configuration(all_messages[0])
            stream_start_index = 1

        if config is None or config.timestamp_frequency == 0:
            scanned = _scan_messages_for_configuration(all_messages)
            if scanned is not None:
                config = _merge_configurations(config, scanned)

        if options.configuration_override is not None:
            # Override is INTENTIONALLY the fallback, not the primary —
            # mirrors C# SdCardFileParser.cs:82-89. The override exists
            # to fill in fields the file lacks (e.g. when a connected
            # device supplies live calibration absent from the SD log),
            # NOT to second-guess values the file already provides.
            config = _merge_configurations(config, options.configuration_override)

        timestamp_frequency = config.timestamp_frequency if config is not None else 0
        if timestamp_frequency == 0 and options.fallback_timestamp_frequency > 0:
            timestamp_frequency = options.fallback_timestamp_frequency

        tick_period = 1.0 / timestamp_frequency if timestamp_frequency > 0 else 0.0

        samples = _produce_samples(
            all_messages,
            stream_start_index,
            file_created_date,
            tick_period,
            config,
        )

        return SdCardLogSession(
            file_name=file_name,
            file_created_date=file_created_date,
            device_config=config,
            samples=samples,
        )

    def parse_file(
        self,
        file_path: str,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        if file_path is None:
            raise TypeError("file_path must not be None")

        options = options if options is not None else SdCardParseOptions()
        if options.buffer_size <= 0:
            raise ValueError("options.buffer_size must be greater than zero")

        with open(file_path, "rb") as stream:
            return self.parse_stream(stream, os.path.basename(file_path), options)


def _read_text_lines(file_stream: Any) -> List[str]:
    """Drain a text/binary stream into a list of non-empty stripped
    lines. Mirrors the C# ``StreamReader.ReadLineAsync`` loop in both
    the CSV and JSON parsers — skip whitespace-only lines, keep
    everything else verbatim.

    Accepts text streams (``io.StringIO``, ``open(path, 'r')``) and
    binary streams (``io.BytesIO``, ``open(path, 'rb')``). Binary
    payloads are decoded as UTF-8 with the BOM stripped if present
    — DAQiFi firmware writes plain ASCII so this is safe; UTF-8 is
    the strict superset that handles future i18n in headers.
    """
    if file_stream is None:
        raise TypeError("file_stream must not be None")

    raw = file_stream.read()
    if isinstance(raw, (bytes, bytearray, memoryview)):
        text = bytes(raw).decode("utf-8-sig", errors="replace")
    else:
        text = str(raw)

    lines: List[str] = []
    for line in text.splitlines():
        if line and line.strip():
            lines.append(line)
    return lines


def _resolve_session_base_time(file_created_date: Optional[datetime]) -> datetime:
    """Return the absolute base time used to anchor reconstructed
    sample timestamps. Mirrors the upstream ``baseTime`` rule: use
    the parsed/overridden file-created date when present, else the
    current UTC time. Naive UTC matches the rest of the SD-card
    code's tz-naive convention."""
    if file_created_date is not None:
        if file_created_date.tzinfo is not None:
            return file_created_date.astimezone(timezone.utc).replace(tzinfo=None)
        return file_created_date
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _emit_progress(
    progress: Optional[Callable[[SdCardParseProgress], None]],
    bytes_read: int,
    total_bytes: int,
    lines_processed: int,
) -> None:
    """Best-effort progress callback. Mirrors the C# IProgress<T>
    pattern: any callback failure is swallowed so a buggy progress
    handler can't abort an otherwise-successful parse.

    SdCardParseProgress's third field is named ``messages_read``
    after the protobuf-parser origin; here it carries the same
    meaning for CSV/JSON (one line = one row).
    """
    if progress is None:
        return
    try:
        progress(SdCardParseProgress(
            bytes_read=bytes_read,
            total_bytes=total_bytes,
            messages_read=lines_processed,
        ))
    except Exception:
        pass


def _replace_timestamp_frequency(
    config: SdCardDeviceConfiguration,
    timestamp_frequency: int,
) -> SdCardDeviceConfiguration:
    """Return a copy of ``config`` with ``timestamp_frequency``
    replaced. Used by the CSV/JSON parsers to apply
    ``options.fallback_timestamp_frequency`` AFTER the parsed-vs-
    override merge so the override gets priority over the fallback.

    Uses ``dataclasses.replace`` so any future field added to
    ``SdCardDeviceConfiguration`` is preserved automatically — a
    manual field-by-field reconstruction would silently drop new
    fields and reset them to their dataclass default.
    """
    from dataclasses import replace as _dc_replace

    return _dc_replace(config, timestamp_frequency=timestamp_frequency)


def _merge_csv_json_config(
    parsed: SdCardDeviceConfiguration,
    override: Optional[SdCardDeviceConfiguration],
) -> SdCardDeviceConfiguration:
    """Merge a parser-derived config with the caller's override. The
    parser values are primary; the override fills in zero/None gaps.
    Mirrors the C# CSV/JSON parsers' ``MergeConfiguration``.

    Distinct from :func:`_merge_configurations` (which targets the
    protobuf parser's two-way merge) because the CSV/JSON paths only
    populate a subset of fields and need to inherit the rest from
    the override."""
    if override is None:
        return parsed
    return SdCardDeviceConfiguration(
        analog_port_count=(
            parsed.analog_port_count
            if parsed.analog_port_count > 0
            else override.analog_port_count
        ),
        digital_port_count=(
            parsed.digital_port_count
            if parsed.digital_port_count > 0
            else override.digital_port_count
        ),
        timestamp_frequency=(
            parsed.timestamp_frequency
            if parsed.timestamp_frequency > 0
            else override.timestamp_frequency
        ),
        device_serial_number=(
            parsed.device_serial_number or override.device_serial_number
        ),
        device_part_number=(
            parsed.device_part_number or override.device_part_number
        ),
        firmware_revision=(
            parsed.firmware_revision or override.firmware_revision
        ),
        calibration_values=(
            parsed.calibration_values or override.calibration_values
        ),
        resolution=(
            parsed.resolution if parsed.resolution > 0 else override.resolution
        ),
        port_range=parsed.port_range or override.port_range,
        internal_scale_m=parsed.internal_scale_m or override.internal_scale_m,
    )


class SdCardCsvFileParser:
    """Parses SD-card ``.csv`` log files (DAQiFi firmware format).

    Ports ``Daqifi.Core/Device/SdCard/SdCardCsvFileParser.cs``. Sync
    end-to-end per project convention. The firmware CSV format:

    1. Up to three ``#``-prefixed comment lines carrying device
       metadata (``# Device:``, ``# Serial Number:``,
       ``# Timestamp Tick Rate:``).
    2. A column header row: ``ain0_ts,ain0_val,ain1_ts,ain1_val,...,dio_ts,dio_val``
    3. Data rows with interleaved per-channel ``timestamp,value``
       pairs. Analog values are raw ADC counts that get scaled via
       the device calibration (see :func:`_scale_raw_analog_values`).

    Like the protobuf parser, ``parse_stream`` reads the entire input
    before yielding samples; the returned :class:`SdCardLogSession`
    exposes samples as a fresh ``Iterator[SdCardLogEntry]``.
    """

    def parse_stream(
        self,
        file_stream: Any,
        file_name: str,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        if file_stream is None:
            raise TypeError("file_stream must not be None")
        if file_name is None:
            raise TypeError("file_name must not be None")

        options = options if options is not None else SdCardParseOptions()
        if options.buffer_size <= 0:
            raise ValueError("options.buffer_size must be greater than zero")

        # Same path-normalize trick as the protobuf parser so callers
        # can pass either a basename or a full path.
        file_name = os.path.basename(file_name.replace("\\", "/"))

        file_created_date = options.session_start_time or _try_parse_log_filename_date(
            file_name
        )

        lines = _read_text_lines(file_stream)
        if not lines:
            return SdCardLogSession(
                file_name=file_name,
                file_created_date=file_created_date,
                device_config=None,
                samples=iter(()),
            )

        header_config, layout = _parse_csv_header(lines, options)
        config = _merge_csv_json_config(header_config, options.configuration_override)
        # Fallback applies last, only if neither file nor override
        # supplied a tick rate — matches the protobuf parser's
        # three-tier precedence.
        if config.timestamp_frequency == 0 and options.fallback_timestamp_frequency > 0:
            config = _replace_timestamp_frequency(
                config, options.fallback_timestamp_frequency
            )

        data_start = _find_csv_data_start_index(lines)
        if data_start >= len(lines):
            return SdCardLogSession(
                file_name=file_name,
                file_created_date=file_created_date,
                device_config=config,
                samples=iter(()),
            )

        samples = _produce_csv_samples(
            lines, data_start, config, layout, file_created_date, options,
        )
        return SdCardLogSession(
            file_name=file_name,
            file_created_date=file_created_date,
            device_config=config,
            samples=samples,
        )

    def parse_file(
        self,
        file_path: str,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        if file_path is None:
            raise TypeError("file_path must not be None")
        options = options if options is not None else SdCardParseOptions()
        if options.buffer_size <= 0:
            raise ValueError("options.buffer_size must be greater than zero")
        # Open in text mode — CSV is always ASCII per the firmware
        # contract; ``newline=""`` lets the universal-newline split
        # handle CR / LF / CRLF the same way as the C# StreamReader.
        with open(file_path, "r", encoding="utf-8-sig", newline="") as stream:
            return self.parse_stream(
                stream, os.path.basename(file_path), options
            )


@dataclass(frozen=True)
class _CsvColumnLayout:
    """How the CSV column header is laid out.

    Mirrors the C# private record ``CsvColumnLayout``.
    """
    analog_pair_count: int
    has_digital_pair: bool


def _parse_csv_header(
    lines: List[str],
    options: SdCardParseOptions,  # noqa: ARG001 — kept for API consistency with the JSON parser
) -> Tuple[SdCardDeviceConfiguration, _CsvColumnLayout]:
    """Pull device metadata + column layout out of the comment +
    column-header lines. Bails on the first non-comment line that
    doesn't look like the column header (i.e. data rows start).

    Returns ``timestamp_frequency=0`` when the ``# Timestamp Tick Rate:``
    header is absent so ``_merge_csv_json_config`` correctly defers to
    ``configuration_override.timestamp_frequency`` (and finally to
    ``options.fallback_timestamp_frequency`` only if both are zero).
    Mirrors the protobuf parser's three-tier precedence.
    """
    device_name: Optional[str] = None
    serial_number: Optional[str] = None
    timestamp_freq = 0  # fallback applied AFTER override merge (see parse_stream)
    analog_count = 0
    digital_count = 0
    has_digital = False

    for line in lines:
        if line.startswith("#"):
            content = line[1:].strip()
            if content.lower().startswith("device:"):
                device_name = content[len("device:"):].strip() or None
            elif content.lower().startswith("serial number:"):
                serial_number = content[len("serial number:"):].strip() or None
            elif content.lower().startswith("timestamp tick rate:"):
                rate_str = content[len("timestamp tick rate:"):].strip()
                # Strip trailing " Hz" if present.
                space_idx = rate_str.find(" ")
                if space_idx > 0:
                    rate_str = rate_str[:space_idx]
                if rate_str.isdigit():
                    timestamp_freq = int(rate_str)
            continue
        # First non-comment line — treat it as the column header iff
        # it contains the firmware's per-channel naming convention.
        lower = line.lower()
        if "_ts," in lower or lower.startswith("ch") or lower.startswith("ain"):
            cols = line.split(",")
            total_pairs = len(cols) // 2
            for p in range(total_pairs):
                name_col = cols[p * 2].lower()
                if name_col.startswith("dio"):
                    has_digital = True
                    digital_count = 1
                else:
                    analog_count += 1
        # Whether we matched a header or hit a data row, the comment
        # block is over — bail.
        break

    config = SdCardDeviceConfiguration(
        analog_port_count=analog_count,
        digital_port_count=digital_count,
        timestamp_frequency=timestamp_freq,
        device_serial_number=serial_number,
        device_part_number=device_name,
        firmware_revision=None,
        calibration_values=None,
    )
    return config, _CsvColumnLayout(
        analog_pair_count=analog_count,
        has_digital_pair=has_digital,
    )


def _find_csv_data_start_index(lines: List[str]) -> int:
    """Return the index of the first data row, skipping comments and
    the column header. Matches the upstream ``FindDataStartIndex``."""
    for i, line in enumerate(lines):
        if line.startswith("#"):
            continue
        lower = line.lower()
        is_column_header = (
            "_ts," in lower
            or (lower.startswith("ch") and len(line) > 2 and not line[2].isdigit())
            or lower.startswith("ain")
        )
        if is_column_header:
            continue
        return i
    return len(lines)


def _try_parse_csv_data_row(
    line: str,
    layout: _CsvColumnLayout,
) -> Optional[Tuple[int, List[float], int, List[int]]]:
    """Parse one CSV data row into ``(row_timestamp, analog_values,
    digital_data, per_channel_timestamps)`` or None on malformed
    input (mirrors the upstream ``TryParseCsvDataRow`` resilience).
    """
    try:
        cols = line.split(",")
        if len(cols) < 2 or len(cols) % 2 != 0:
            return None
        total_pairs = len(cols) // 2
        if layout.analog_pair_count > 0:
            analog_pair_count = min(layout.analog_pair_count, total_pairs)
        else:
            analog_pair_count = (
                total_pairs - 1 if layout.has_digital_pair else total_pairs
            )

        analog_values: List[float] = []
        per_channel_ts: List[int] = []
        for ch in range(analog_pair_count):
            ts_col = cols[ch * 2].strip()
            val_col = cols[ch * 2 + 1].strip()
            if not ts_col.isdigit():
                return None
            ts = int(ts_col)
            try:
                val = float(val_col)
            except ValueError:
                return None
            per_channel_ts.append(ts)
            analog_values.append(val)

        digital_data = 0
        dio_timestamp = 0
        if layout.has_digital_pair and total_pairs > analog_pair_count:
            dio_index = analog_pair_count
            dio_ts_col = cols[dio_index * 2].strip()
            dio_val_col = cols[dio_index * 2 + 1].strip()
            if dio_ts_col.isdigit():
                dio_timestamp = int(dio_ts_col)
            if dio_val_col.isdigit():
                digital_data = int(dio_val_col)

        row_timestamp = per_channel_ts[0] if per_channel_ts else dio_timestamp
        if not per_channel_ts and dio_timestamp == 0 and not layout.has_digital_pair:
            return None
        return row_timestamp, analog_values, digital_data, per_channel_ts
    except Exception:
        return None


def _produce_csv_samples(
    lines: List[str],
    data_start: int,
    config: Optional[SdCardDeviceConfiguration],
    layout: _CsvColumnLayout,
    file_created_date: Optional[datetime],
    options: SdCardParseOptions,
) -> Iterator[SdCardLogEntry]:
    base_time = _resolve_session_base_time(file_created_date)
    timestamp_freq = config.timestamp_frequency if config else 0
    tick_period = 1.0 / timestamp_freq if timestamp_freq > 0 else 0.0

    previous_timestamp: Optional[int] = None
    elapsed_seconds = 0.0
    lines_processed = 0
    bytes_read = 0
    progress = options.progress
    total_bytes = sum(len(line) + 1 for line in lines)

    for i in range(data_start, len(lines)):
        line = lines[i]
        lines_processed += 1
        bytes_read += len(line) + 1

        parsed = _try_parse_csv_data_row(line, layout)
        if parsed is None:
            continue
        row_ts, raw_analog, digital_data, per_ch_ts = parsed
        analog_values = _scale_raw_analog_values(
            [int(v) for v in raw_analog], config
        )

        absolute_time = base_time
        # ``row_ts == 0`` is treated as "no timestamp" — matches the
        # protobuf parser's behavior so cross-format reconstruction
        # of the same physical log is consistent.
        if tick_period > 0 and row_ts != 0:
            if previous_timestamp is None:
                previous_timestamp = row_ts
            else:
                delta = _compute_tick_delta(previous_timestamp, row_ts)
                elapsed_seconds += delta * tick_period
                previous_timestamp = row_ts
            absolute_time = base_time + timedelta(seconds=elapsed_seconds)

        yield SdCardLogEntry(
            timestamp=absolute_time,
            analog_values=tuple(analog_values),
            digital_data=digital_data,
            analog_timestamps=tuple(per_ch_ts) if per_ch_ts else None,
        )

        if lines_processed % 100 == 0:
            _emit_progress(progress, bytes_read, total_bytes, lines_processed)

    _emit_progress(progress, bytes_read, total_bytes, lines_processed)


class SdCardJsonFileParser:
    """Parses SD-card ``.json`` log files (JSONL format).

    Ports ``Daqifi.Core/Device/SdCard/SdCardJsonFileParser.cs``. Each
    line is a JSON object: ``{"ts": <uint32>, "analog": [...],
    "digital": "<hex>"}``. Sync end-to-end per project convention.

    Configuration is *inferred* from the first line (analog channel
    count = length of the ``analog`` array); merge with
    ``options.configuration_override`` to fill in missing fields like
    calibration / resolution / port range needed for raw-to-volts
    scaling.
    """

    def parse_stream(
        self,
        file_stream: Any,
        file_name: str,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        if file_stream is None:
            raise TypeError("file_stream must not be None")
        if file_name is None:
            raise TypeError("file_name must not be None")

        options = options if options is not None else SdCardParseOptions()
        if options.buffer_size <= 0:
            raise ValueError("options.buffer_size must be greater than zero")

        file_name = os.path.basename(file_name.replace("\\", "/"))
        file_created_date = options.session_start_time or _try_parse_log_filename_date(
            file_name
        )

        lines = _read_text_lines(file_stream)
        if not lines:
            return SdCardLogSession(
                file_name=file_name,
                file_created_date=file_created_date,
                device_config=None,
                samples=iter(()),
            )

        config = _infer_json_configuration(lines[0], options)
        if config.timestamp_frequency == 0 and options.fallback_timestamp_frequency > 0:
            config = _replace_timestamp_frequency(
                config, options.fallback_timestamp_frequency
            )
        samples = _produce_json_samples(lines, config, file_created_date, options)
        return SdCardLogSession(
            file_name=file_name,
            file_created_date=file_created_date,
            device_config=config,
            samples=samples,
        )

    def parse_file(
        self,
        file_path: str,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        if file_path is None:
            raise TypeError("file_path must not be None")
        options = options if options is not None else SdCardParseOptions()
        if options.buffer_size <= 0:
            raise ValueError("options.buffer_size must be greater than zero")
        with open(file_path, "r", encoding="utf-8-sig", newline="") as stream:
            return self.parse_stream(
                stream, os.path.basename(file_path), options
            )


def _try_parse_json_line(line: str) -> Optional[Tuple[int, List[float], int]]:
    """Parse one JSONL row into ``(timestamp, analog_values,
    digital_data)`` or None on malformed input. Matches the upstream
    ``TryParseJsonLine`` — any exception → skip line."""
    import json

    try:
        obj = json.loads(line)
        if not isinstance(obj, dict):
            return None
        ts_raw = obj.get("ts")
        if not isinstance(ts_raw, int) or isinstance(ts_raw, bool) or ts_raw < 0 or ts_raw > 0xFFFFFFFF:
            return None
        analog_raw = obj.get("analog")
        if not isinstance(analog_raw, list):
            return None
        analog_values: List[float] = []
        for v in analog_raw:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            analog_values.append(float(v))
        digital_data = 0
        digital_raw = obj.get("digital")
        if isinstance(digital_raw, str) and digital_raw:
            digital_data = _parse_digital_hex_string(digital_raw)
        return ts_raw, analog_values, digital_data
    except Exception:
        return None


def _parse_digital_hex_string(hex_string: str) -> int:
    """Decode a hyphen-separated hex byte sequence (e.g. ``"AB-CD"``)
    into a uint32 with little-endian packing. Mirrors the upstream
    ``ParseDigitalHexString``: at most 4 bytes, malformed bytes
    contribute 0 to the result rather than aborting."""
    if not hex_string:
        return 0
    parts = [p for p in hex_string.split("-") if p]
    result = 0
    for i, byte_str in enumerate(parts[:4]):
        try:
            byte_val = int(byte_str, 16)
            if 0 <= byte_val <= 0xFF:
                result |= byte_val << (i * 8)
        except ValueError:
            continue
    return result


def _infer_json_configuration(
    first_line: str,
    options: SdCardParseOptions,
) -> SdCardDeviceConfiguration:
    """Infer the analog channel count from the first JSONL row, then
    merge with the caller's override (which fills in resolution /
    calibration / port_range for scaling). Mirrors the upstream
    ``InferConfiguration``.

    JSONL has no in-band tick-rate header, so the inferred config
    leaves ``timestamp_frequency=0`` and lets the merge / fallback
    chain decide the final value (override → fallback). Same
    three-tier precedence as the protobuf and CSV parsers.
    """
    parsed = _try_parse_json_line(first_line)
    analog_count = len(parsed[1]) if parsed is not None else 0
    inferred = SdCardDeviceConfiguration(
        analog_port_count=analog_count,
        digital_port_count=0,  # cannot infer from JSONL data
        timestamp_frequency=0,  # fallback applied AFTER override merge
        device_serial_number=None,
        device_part_number=None,
        firmware_revision=None,
        calibration_values=None,
    )
    return _merge_csv_json_config(inferred, options.configuration_override)


def _produce_json_samples(
    lines: List[str],
    config: SdCardDeviceConfiguration,
    file_created_date: Optional[datetime],
    options: SdCardParseOptions,
) -> Iterator[SdCardLogEntry]:
    base_time = _resolve_session_base_time(file_created_date)
    timestamp_freq = config.timestamp_frequency
    tick_period = 1.0 / timestamp_freq if timestamp_freq > 0 else 0.0

    previous_timestamp: Optional[int] = None
    elapsed_seconds = 0.0
    lines_processed = 0
    bytes_read = 0
    progress = options.progress
    total_bytes = sum(len(line) + 1 for line in lines)

    for line in lines:
        lines_processed += 1
        bytes_read += len(line) + 1

        parsed = _try_parse_json_line(line)
        if parsed is None:
            continue
        ts, raw_analog, digital_data = parsed
        analog_values = _scale_raw_analog_values(
            [int(v) for v in raw_analog], config
        )

        absolute_time = base_time
        # ``ts == 0`` is treated as "no timestamp" — matches the
        # protobuf parser so the same physical log reconstructs to
        # consistent absolute timestamps regardless of file format.
        if tick_period > 0 and ts != 0:
            if previous_timestamp is None:
                previous_timestamp = ts
            else:
                delta = _compute_tick_delta(previous_timestamp, ts)
                elapsed_seconds += delta * tick_period
                previous_timestamp = ts
            absolute_time = base_time + timedelta(seconds=elapsed_seconds)

        yield SdCardLogEntry(
            timestamp=absolute_time,
            analog_values=tuple(analog_values),
            digital_data=digital_data,
            analog_timestamps=None,  # JSON format carries one ts per row
        )

        if lines_processed % 100 == 0:
            _emit_progress(progress, bytes_read, total_bytes, lines_processed)

    _emit_progress(progress, bytes_read, total_bytes, lines_processed)


class SdCardFileParserFactory:
    """Picks the right SD-card parser by file extension.

    Ports ``Daqifi.Core/Device/SdCard/SdCardFileParserFactory.cs``.
    Wires up all three parsers (protobuf ``.bin``, JSONL ``.json``,
    DAQiFi-firmware-flavored ``.csv``); ``detect_format`` and the
    parse helpers dispatch by extension.
    """

    @staticmethod
    def detect_format(file_name: str) -> SdCardLogFormat:
        if file_name is None:
            raise TypeError("file_name must not be None")
        ext = os.path.splitext(file_name)[1].lower()
        if ext == ".bin":
            return SdCardLogFormat.PROTOBUF
        if ext == ".json":
            return SdCardLogFormat.JSON
        if ext == ".csv":
            return SdCardLogFormat.CSV
        raise ValueError(
            f"Unsupported file extension: {ext}. "
            f"Supported extensions are .bin, .json, .csv"
        )

    @staticmethod
    def parse_with_format(
        file_stream: Any,
        file_name: str,
        format: SdCardLogFormat,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        if format is SdCardLogFormat.PROTOBUF:
            return SdCardFileParser().parse_stream(file_stream, file_name, options)
        if format is SdCardLogFormat.JSON:
            return SdCardJsonFileParser().parse_stream(file_stream, file_name, options)
        if format is SdCardLogFormat.CSV:
            return SdCardCsvFileParser().parse_stream(file_stream, file_name, options)
        raise ValueError(f"Unsupported format: {format}")

    @staticmethod
    def parse_stream(
        file_stream: Any,
        file_name: str,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        format = SdCardFileParserFactory.detect_format(file_name)
        return SdCardFileParserFactory.parse_with_format(
            file_stream, file_name, format, options
        )

    @staticmethod
    def parse_file(
        file_path: str,
        options: Optional[SdCardParseOptions] = None,
    ) -> SdCardLogSession:
        if file_path is None:
            raise TypeError("file_path must not be None")
        format = SdCardFileParserFactory.detect_format(file_path)
        # Open mode depends on format — protobuf is binary, CSV/JSON
        # are text. Each parser's parse_file handles its own open(),
        # but parse_with_format takes a pre-opened stream so we need
        # to pick the right mode here.
        if format is SdCardLogFormat.PROTOBUF:
            mode, encoding = "rb", None
        else:
            mode, encoding = "r", "utf-8-sig"
        open_kwargs: Dict[str, Any] = {"mode": mode}
        if encoding is not None:
            open_kwargs["encoding"] = encoding
            open_kwargs["newline"] = ""
        with open(file_path, **open_kwargs) as stream:
            return SdCardFileParserFactory.parse_with_format(
                stream, os.path.basename(file_path), format, options
            )
