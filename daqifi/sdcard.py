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
- ``SdCardFileParserFactory`` picks the protobuf, CSV or JSON parser
  (:class:`SdCardCsvFileParser`, :class:`SdCardJsonFileParser`) by
  file extension.
"""
from __future__ import annotations

import io
import math
import os
import re
import socket
import time
import warnings
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
    Union,
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
    "sd_card_listing_status",
    "check_sd_card_listing",
    "END_OF_FILE_MARKER",
    "TRANSFER_ERROR_MARKER",
    "SD_LIST_END_MARKER",
]


# ASCII marker the firmware appends after all SD-card file bytes have
# been streamed. Mirrors the C# ``SdCardFileReceiver.EndOfFileMarker``
# constant — kept module-level so test harnesses + receiver code share
# the same bytes.
END_OF_FILE_MARKER: bytes = b"__END_OF_FILE__"

# What the firmware sends INSTEAD of the end-of-file marker when reading the
# card fails part-way through a file (sd_card_manager.c, #725): the bytes
# before it are real content, but the file is incomplete. An open failure
# still ends with a bare END_OF_FILE_MARKER. Upstream: SdCardFileReceiver.
TRANSFER_ERROR_MARKER: bytes = b"__TRANSFER_ERROR__"

# A trailing window this long catches either marker split across reads.
_TERMINAL_MARKER_WINDOW = max(len(END_OF_FILE_MARKER), len(TRANSFER_ERROR_MARKER))

# What may follow a terminal marker: line endings and the console prompt,
# in any order, at most this many bytes. The firmware sends the markers
# bare (sd_card_manager.c); the console framing is tolerated.
_MARKER_TRAILER_MAX = 16


def _is_marker_trailer(trailer: bytes) -> bool:
    """True if ``trailer`` can follow a terminal marker at the end of a
    transfer: line endings and the console prompt, at most
    ``_MARKER_TRAILER_MAX`` bytes, possibly ending part-way through a
    prompt still arriving."""
    if len(trailer) > _MARKER_TRAILER_MAX:
        return False
    rest = trailer.replace(b"DAQIFI> ", b"").replace(b"DAQIFI>", b"")
    if not rest.strip(b"\r\n "):
        return True
    # A prompt still arriving at the very end ("DAQ").
    for cut in range(1, len(b"DAQIFI>")):
        if rest.endswith(b"DAQIFI>"[:cut]):
            return not rest[: len(rest) - cut].strip(b"\r\n ")
    return False


class _PreambleSkip:
    """The echoed GET command, and the prompt that may follow it, removed
    from the HEAD of a download as a PREFIX -- never by searching.

    ⛔ WHY A PREFIX. The legacy code this replaces located ``DAQIFI>``
    anywhere in the first 200 bytes and discarded everything before it, so a
    file whose own bytes contain the prompt lost its head (#206 item 5). A
    prefix test cannot do that: it only consumes bytes it has matched from
    position 0.

    ⛔ WHY BEFORE MARKER CLASSIFICATION. The echoed command line can itself
    carry a terminal marker's bytes -- a GET for a file named
    ``__TRANSFER_ERROR__.bin`` is the measured case, and on the previous head
    the receiver raised ``SdCardTransferErrorException`` for it, reporting a
    transfer that worked as a device read error. So the head has to go before
    anything looks for a marker, which is why this lives inside the receiver
    rather than in the caller.

    ⛔ EVERY CANDIDATE MUST END IN A PROMPT, and that is the whole
    discriminator. With echo **off** there is no framing at all, so an echo
    or a line ending at position 0 is just the file's first bytes -- and a
    file whose first line happens to be the GET command's own text is a real
    case, pinned by ``test_carried_items_206``'s
    ``test_a_command_shaped_payload_is_kept_with_echo_off``. The prompt is
    the one token the device emits that a payload is not expected to open
    with, so an echo is removed only when a prompt follows it and proves it
    was framing. A leading ``\\r\\n`` goes the same way: only ahead of a
    prompt.

    ⚠ RESIDUAL, stated because it cannot be decided rather than hidden: a
    payload whose own first bytes are exactly ``DAQIFI> `` is
    indistinguishable from framing and loses them. That is strictly narrower
    than what it replaces -- the search lost everything before a ``DAQIFI>``
    occurring anywhere in the first 200 bytes -- but it is not nothing.
    """

    _ENDINGS = (b"", b"\r\n", b"\n", b"\r")
    _PROMPTS = (b"DAQIFI> ", b"DAQIFI>")

    def __init__(self, expected_echo: bytes) -> None:
        echo = bytes(expected_echo)
        candidates = set()
        for lead in self._ENDINGS:
            for body in (b"", echo):
                for after in self._ENDINGS:
                    if not body and after:
                        # No echo means nothing for a second ending to
                        # follow; two endings in a row are not framing.
                        continue
                    for prompt in self._PROMPTS:
                        candidates.add(lead + body + after + prompt)
        # Longest first: `DAQIFI> ` must win over `DAQIFI>`.
        self._candidates = sorted(candidates, key=len, reverse=True)
        self._longest = len(self._candidates[0]) if self._candidates else 0
        self._buf = bytearray()
        self.done = False

    def head_span(self, buf: bytes) -> Optional[int]:
        """Length of the framing at the start of ``buf``, or ``None`` while
        that is still undecidable.

        ⛔ ONE DEFINITION OF "WHERE THE HEAD ENDS", shared with the capture
        loop in ``device.py``, which had two approximations of its own and
        both scanned or counted the ECHO AND PROMPT as if they were payload.
        The size machinery that made the counting matter is gone (ruling FO
        A2), but the scanning still does: a GET names a file, so an echoed
        command can contain a terminal marker's own bytes, and a capture
        that scans the head stops before the payload.

        0 means "no framing here" (echo off). ``None`` means the buffer is
        still a proper prefix of a longer candidate, so nothing can be
        measured from it yet.
        """
        if any(c.startswith(buf) and len(c) > len(buf) for c in self._candidates):
            return None
        matched = next((c for c in self._candidates if buf.startswith(c)), None)
        return len(matched) if matched is not None else 0

    def feed(self, chunk: bytes) -> bytes:
        """Bytes to pass downstream; may be empty while still deciding.

        Holds at most the longest candidate's length, so the wait is bounded
        by the head's own size and cannot stall a transfer.
        """
        if self.done:
            return chunk
        self._buf.extend(chunk)
        buf = bytes(self._buf)
        matched = next((c for c in self._candidates if buf.startswith(c)), None)
        # A longer candidate may still complete, so do not commit yet: with
        # the buffer at exactly `DAQIFI>` the next byte decides whether 7 or
        # 8 bytes are framing.
        if len(buf) < self._longest and any(
            c.startswith(buf) and len(c) > len(buf) for c in self._candidates
        ):
            return b""
        self.done = True
        self._buf = bytearray()
        return buf[len(matched):] if matched is not None else buf

    def flush(self) -> bytes:
        """Whatever is still held, head removed if the head is decidable.

        ⛔ CALLED WHEN NO MORE BYTES ARE COMING, and without it those bytes
        are LOST. :meth:`feed` holds while a longer candidate could still
        complete, which is correct mid-stream and wrong at the end: a whole
        payload that is a proper prefix of a candidate -- the measured case
        is a GET for a file named ``__END_OF_FILE__`` -- stayed buffered,
        the receiver saw nothing, and it reported a TIMEOUT for a transfer
        whose bytes it was holding. Deciding with what has arrived is the
        only honest answer at EOF.
        """
        if self.done:
            return b""
        buf = bytes(self._buf)
        self._buf = bytearray()
        self.done = True
        matched = next((c for c in self._candidates if buf.startswith(c)), None)
        return buf[len(matched):] if matched is not None else buf


class _SizedTransfer:
    """The body of a download whose size is known, telling the device's
    ``__TRANSFER_ERROR__`` from file content that happens to contain it.

    The boundary is decided by what starts AT it: the size bytes followed
    by an exact ``__END_OF_FILE__`` are success, whatever the content's tail
    looks like (its last bytes and the marker's leading ``__`` can spell
    ``__TRANSFER_ERROR__`` together). While the bytes past the boundary are
    still a prefix of that marker, nothing is decided.

    Before that, no byte is written from the first OPEN candidate on: a
    place the marker could start that nothing but a line ending or prompt
    follows (or a proper prefix of the marker at the end of what has
    come). A candidate followed by real content was content. Past the
    boundary, anything but the end-of-file marker: a complete transfer-error
    marker there or across the boundary is the error, else a mismatch. A
    candidate still open when the line goes idle, closes or the deadline
    passes (:meth:`finish`) is the error, with the bytes before it, unless
    the bytes past the boundary could be the start of the end-of-file
    marker (then it can't be told; the caller's timeout stands). No quiet
    window decides anything: content may pause.
    """

    def __init__(self, expected_size: int) -> None:
        self.expected = expected_size
        self.committed = 0
        self.pending = bytearray()

    def _open_candidate(self, limit: int) -> Optional[int]:
        buf = bytes(self.pending)
        marker = TRANSFER_ERROR_MARKER
        i = buf.find(marker[:1])
        while 0 <= i < limit:
            tail = buf[i:]
            if len(tail) < len(marker):
                if marker.startswith(tail):
                    return i
            elif tail.startswith(marker) and _is_marker_trailer(tail[len(marker):]):
                return i
            i = buf.find(marker[:1], i + 1)
        return None

    def feed(self, data: bytes) -> Tuple[bytes, Optional[Tuple[str, Any]]]:
        """Take ``data``; return the bytes now safe to write and an outcome:
        None (keep reading), ("eof", bytes after the marker),
        ("transfer_error", bytes before it) or ("mismatch", None)."""
        self.pending.extend(data)
        boundary = self.expected - self.committed
        undecided = True
        if len(self.pending) >= boundary:
            after = bytes(self.pending[boundary:])
            if after.startswith(END_OF_FILE_MARKER):
                # The whole body, then the exact marker: success.
                out = bytes(self.pending[:boundary])
                self.committed += boundary
                self.pending = bytearray()
                return out, ("eof", after[len(END_OF_FILE_MARKER):])
            # Empty, or a prefix of the end-of-file marker: not yet known.
            undecided = END_OF_FILE_MARKER.startswith(after)
        limit = min(boundary, len(self.pending))
        candidate = self._open_candidate(limit)
        safe = limit if candidate is None else candidate
        out = bytes(self.pending[:safe])
        del self.pending[:safe]
        self.committed += safe
        return out, (None if undecided else self._outcome())

    def _outcome(self) -> Optional[Tuple[str, Any]]:
        # Called once the bytes past the boundary are known not to be (the
        # start of) the end-of-file marker.
        tail = bytes(self.pending)
        marker = TRANSFER_ERROR_MARKER
        if tail.startswith(marker) and _is_marker_trailer(tail[len(marker):]):
            return ("transfer_error", self.committed)
        if marker.startswith(tail):
            return None  # could still complete
        return ("mismatch", None)

    def finish(self) -> Optional[Tuple[str, Any]]:
        """At idle, close or the deadline: an open complete candidate is the
        device's marker."""
        tail = bytes(self.pending)
        marker = TRANSFER_ERROR_MARKER
        if not (tail.startswith(marker) and _is_marker_trailer(tail[len(marker):])):
            return None
        boundary = self.expected - self.committed
        if len(tail) >= boundary and END_OF_FILE_MARKER.startswith(tail[boundary:]):
            # Its bytes past the boundary could be the end-of-file marker
            # starting: content followed by a cut-off marker, or the error.
            return None
        return ("transfer_error", self.committed)


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


#: The marker the firmware appends to a listing (``sd_card_manager.c``,
#: ``SD_LIST_END_OK`` / ``_INCOMPLETE`` / ``_FAILED``), followed by the
#: status word.
SD_LIST_END_MARKER = "__END_OF_LIST__"

_DIRECTORY_NOT_FOUND_RE = re.compile(
    r"\[Error:(\d+)\]\s*Failed to open directory", re.IGNORECASE
)
# SYS_FS_ERROR_NO_FILE, SYS_FS_ERROR_NO_PATH.
_DIRECTORY_NOT_FOUND_CODES = ("4", "5")


# The marker, then optionally whitespace and the status word, and then
# whatever followed it on the wire. The firmware writes the marker with no
# line ending (SD_LIST_END_OK = "\r\n__END_OF_LIST__ OK"), so the reply to
# the next command is glued to it: '__END_OF_LIST__ OK0,"No error"'.
# The firmware's own status words come first, so a glued echo
# ('__END_OF_LIST__ OKDAQIFI> SYSTem:ERRor?') doesn't run into the word.
_LIST_END_RE = re.compile(
    r"^__END_OF_LIST__(?:(\s+)(OK|INCOMPLETE|FAILED|[A-Za-z]+))?(.*)$", re.DOTALL
)


def _list_end_parts(line: str) -> Optional[Tuple[str, str]]:
    """``(status word, glued remainder)`` if ``line`` starts with the
    end-of-listing marker, else ``None``. The marker must stand alone or be
    followed by whitespace: ``__END_OF_LIST__notes.csv`` is a file."""
    match = _LIST_END_RE.match(line.strip())
    if match is None:
        return None
    if match.group(1) is None:
        return ("", "") if not match.group(3) else None
    return match.group(2), match.group(3).strip()


def _list_end_status(line: str) -> Optional[str]:
    """The status word if ``line`` is the end-of-listing marker ("" when it
    has none), else ``None``."""
    parts = _list_end_parts(line)
    return None if parts is None else parts[0]


# A SYSTem:ERRor? reply glued to the end of the line before it. The firmware
# strips the final CR/LF of a listing's last chunk (ListFilesInDirectoryChunked,
# v3.7.3 and main), so on firmware without the end-of-listing marker the
# reply lands on the last file line: 'Daqifi/log_a.bin 30,"No error"'. A
# file's size digits can't be told from a code's, so only forms that are
# unambiguous at the end of the line are split: a negative code, or 0 (or
# +0) with "No error".
_GLUED_ERROR_REPLY_RE = re.compile(r'(-\d+\s*,\s*"[^"]*"|\+?0\s*,\s*"No error")\s*$')


def split_glued_error_reply(lines: Sequence[str]) -> List[str]:
    """Split a ``SYSTem:ERRor?`` reply glued onto the end of the last line
    onto a line of its own."""
    out = list(lines)
    while out and not (out[-1] or "").strip():
        out.pop()
    if not out:
        return out
    last = out[-1]
    match = _GLUED_ERROR_REPLY_RE.search(last)
    if match and match.start() > 0 and last[:match.start()].strip():
        out[-1:] = [last[:match.start()], match.group(1)]
    return out


def throw_if_sd_card_reported_error(lines: Sequence[str]) -> None:
    """Raise for an explicit device report in a listing reply ("No SD Card
    Detected", "Failed to open directory"), before any judgement about how
    the reply ended: those replies carry no end-of-listing marker."""
    from .exceptions import (
        SdCardDirectoryNotFoundException,
        SdCardFilesystemException,
        SdCardNotPresentException,
    )

    lines_list = [ln for ln in lines if ln is not None]
    last_scpi_error = None
    for line in reversed(lines_list):
        if _is_scpi_error_line(line):
            last_scpi_error = line.strip()
            break
    for line in lines_list:
        if "no sd card detected" in line.lower():
            raise SdCardNotPresentException(lines_list, last_scpi_error)
    for line in lines_list:
        if "failed to open directory" in line.lower():
            match = _DIRECTORY_NOT_FOUND_RE.search(line)
            if match and match.group(1) in _DIRECTORY_NOT_FOUND_CODES:
                raise SdCardDirectoryNotFoundException(
                    lines_list, last_scpi_error, line.strip()
                )
            raise SdCardFilesystemException(
                lines_list, last_scpi_error, line.strip()
            )


def split_glued_list_end(lines: Iterable[str]) -> List[str]:
    """Split a reply glued onto the end-of-listing marker onto its own line,
    as it would be if the firmware ended the marker with CR/LF."""
    out: List[str] = []
    for line in lines:
        parts = _list_end_parts(line) if line is not None else None
        if parts and parts[1]:
            out.append(f"{SD_LIST_END_MARKER} {parts[0]}".rstrip())
            out.append(parts[1])
        else:
            out.append(line)
    return out


def sd_card_listing_status(lines: Iterable[str]) -> str:
    """How a listing ended, from the firmware's end-of-listing marker.

    Returns ``"complete"`` (``OK``), ``"incomplete"`` (``INCOMPLETE``, or a
    status word this version doesn't know), ``"failed"`` (``FAILED``: the
    directory couldn't be opened) or ``"unterminated"``: no marker, as from
    firmware before it had one. That isn't an error, it just can't vouch
    for the listing. A status is final once seen: a later line (an echo of
    the next command, say) doesn't undo it, though a later marker replaces
    it. Lines left over from an earlier exchange are the caller's to keep
    out (the device drains them before retrying a listing).
    """
    status = "unterminated"
    for line in lines:
        if line is None:
            continue
        word = _list_end_status(line)
        if word is None:
            continue
        status = {"OK": "complete", "FAILED": "failed"}.get(
            word.upper(), "incomplete"
        )
    return status


def check_sd_card_listing(lines: Sequence[str]) -> None:
    """Raise the typed exception a listing reply calls for, if any.

    Runs :func:`throw_if_sd_card_list_error`, then refuses a listing whose
    end-of-listing marker says it isn't the whole card
    (:class:`~daqifi.exceptions.SdCardListIncompleteException`).
    """
    from .exceptions import SdCardListIncompleteException

    throw_if_sd_card_list_error(lines)
    status = sd_card_listing_status(lines)
    if status in ("incomplete", "failed"):
        raise SdCardListIncompleteException(
            list(lines), f"the device marked the listing {status}"
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
        SdCardDirectoryNotFoundException: device emitted
            ``"[Error:4]"`` or ``"[Error:5]Failed to open directory ..."``
            (no such directory).
        SdCardFilesystemException: device emitted any other
            ``"Failed to open directory ..."``.
        SdCardOperationException: any other classification fallback
            (SCPI error with no content, non-result line with no SCPI error).
    """
    from .exceptions import (
        SdCardDirectoryNotFoundException,
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
            match = _DIRECTORY_NOT_FOUND_RE.search(line)
            if match and match.group(1) in _DIRECTORY_NOT_FOUND_CODES:
                raise SdCardDirectoryNotFoundException(
                    lines_list, last_scpi_error, line.strip()
                )
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
        # The end-of-listing marker is framing, not a file (#188).
        if _list_end_status(line) is not None:
            continue
        # The firmware lists "<path> <size>" (sd_card_manager.c) — the
        # first whitespace token is the path, a decimal second one the size.
        size: Optional[int] = None
        token_end = -1
        for sep in (" ", "\t"):
            idx = line.find(sep)
            if idx > 0 and (token_end < 0 or idx < token_end):
                token_end = idx
        if token_end > 0:
            rest = line[token_end:].split()
            if len(rest) == 1 and rest[0].isascii() and rest[0].isdigit():
                size = int(rest[0])
            line = line[:token_end]
        if line.lower().startswith(_DAQIFI_DIR_PREFIX):
            line = line[len(_DAQIFI_DIR_PREFIX):]
        file_name = os.path.basename(line.replace("\\", "/"))
        if not file_name.strip():
            continue
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in file_name):
            continue
        out.append(
            SdCardFileInfo(file_name, _try_parse_log_filename_date(file_name), size)
        )
    return out


@dataclass(frozen=True)
class SdCardFileInfo:
    """One file on the device SD card.

    `created_date` is parsed from the filename when available; `None`
    if the parser couldn't extract it. `size` is the byte count the
    listing reported, `None` if it gave none.
    """

    file_name: str
    created_date: Optional[datetime] = None
    size: Optional[int] = None

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
    # What integer analog values in a firmware log mean: "raw" ADC codes
    # (RawOutputMode) or "millivolts" (VoltagePrecision 0). The file doesn't
    # say (daqifi-nyquist-firmware#1179). None reads them as raw codes and
    # warns once per parse. Values written with decimals are volts either
    # way.
    integer_units: Optional[str] = None
    # The channel numbers of a protobuf log's analog values, in the order
    # the firmware packs them (the enabled channels, ascending). The log
    # doesn't record them, so without this calibration and range are applied
    # by position and entries carry analog_channels=None. CSV and JSON logs
    # name their channels and ignore this.
    analog_channels: Optional[Sequence[int]] = None


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
    ignored, so a short write lost the rest (#163). Only an
    ``io.RawIOBase`` uses ``None`` to mean "nothing written, it would
    block", so only there is ``None`` retried; other writers (codecs
    stream writers, many file-likes) return ``None`` after writing
    everything, and repeating the write would duplicate it. Every retry,
    after a short write or a would-block, stops at ``deadline``
    (``time.monotonic()`` value): a sink that drains slowly or not at all
    must not hold a download, and the exchange lock with it, past its
    timeout.
    """
    raw = isinstance(stream, io.RawIOBase)
    remaining = bytes(data)
    while remaining:
        written = stream.write(remaining)
        if written is None and not raw:
            return
        if written is not None:
            if not isinstance(written, int) or isinstance(written, bool):
                return  # not a byte count: the writer took it all
            if written <= 0:
                raise OSError(f"write accepted {written} bytes; destination is not writable")
            remaining = remaining[written:]
            if not remaining:
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
        expected_size: Optional[int] = None,
        preamble: Optional[bytes] = None,
        eof_only: bool = False,
    ) -> int:
        """Read until the EOF marker, write everything before it to
        ``destination_stream``, and return the total bytes written.

        With ``expected_size`` (from the SD listing) exactly that many
        bytes are the file, even if they contain the marker, and the
        marker must follow them directly; anything else raises
        ``IOError``. Without it the first marker ends the file, so a file
        containing the marker text is cut short there (#163).

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
            expected_size: the file's size, if known.
            eof_only: treat ONLY ``__END_OF_FILE__`` as terminal, so
                ``__TRANSFER_ERROR__`` is ordinary content and is never
                raised. Omitted (the default) both markers terminate, which
                is what the streaming-device and download-to-temp callers
                rely on. The legacy ``NyquistDevice`` download passes it
                because its error-marker detection was withdrawn: a file may
                legitimately contain those bytes, and on that path a false
                device-error is worse than the base's silence.
            preamble: the GET command's bytes as sent, when the caller
                shares the console with the download and so may see that
                command echoed back, followed by a prompt. Given it, that
                head is removed as a **prefix** before anything is
                classified; omitted (the default) nothing is stripped and
                this method behaves exactly as it did before the parameter
                existed, which is what the streaming-device and
                download-to-temp callers rely on.

        Returns:
            Total bytes written to ``destination_stream`` (the EOF
            marker itself is NOT written).

        Raises:
            TypeError: if ``destination_stream`` or ``file_name`` is None.
            ValueError: if ``timeout_s`` is non-positive.
            TimeoutError: if the marker isn't received within
                ``timeout_s`` OR the source stream is closed before
                the marker arrives.
            IOError: if ``expected_size`` bytes aren't followed by the
                marker.

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

        if expected_size is not None and (
            not isinstance(expected_size, int) or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise ValueError(f"expected_size must be a non-negative int (got {expected_size!r})")
        from .exceptions import SdCardTransferErrorException

        marker_len = len(END_OF_FILE_MARKER)
        error_len = len(TRANSFER_ERROR_MARKER)
        window = _TERMINAL_MARKER_WINDOW
        deadline = time.monotonic() + effective_timeout
        has_in_waiting = hasattr(self._source, "in_waiting")
        # Reset leftover so a re-used receiver doesn't expose stale
        # bytes from the previous receive() call.
        self._leftover = b""

        # Trailing-byte window so a marker can be detected even when
        # split across chunk boundaries — matches the upstream
        # implementation. Sized to the longer of the two markers.
        trailing = bytearray()
        total_bytes = 0
        # Known size: tells the device's __TRANSFER_ERROR__ from content.
        sized = _SizedTransfer(expected_size) if expected_size is not None else None
        # Opt-in, so the two callers that do not share the console with the
        # download see no change at all.
        skip = _PreambleSkip(preamble) if preamble else None

        def _transfer_error_if_open() -> None:
            outcome = sized.finish() if sized is not None else None
            if outcome is not None:
                raise SdCardTransferErrorException(file_name, outcome[1])

        # Bytes released by the preamble skip at the end of the stream, fed
        # back through the SAME classification path as a read chunk. An
        # earlier revision flushed only on the EOF branch and tried to
        # `continue` on the deadline branch, where re-entering past an
        # expired deadline discarded them -- so a serial-shaped source
        # (in_waiting) that never reports EOF raised a timeout for a whole
        # payload the skip was holding.
        injected: Optional[bytes] = None

        while True:
            if injected is not None:
                chunk = injected
                injected = None
            elif time.monotonic() >= deadline:
                if skip is not None:
                    tail = skip.flush()
                    skip = None
                    if tail:
                        # One more pass to classify what was held; the
                        # deadline is already expired, so this cannot loop.
                        injected = tail
                        continue
                _transfer_error_if_open()
                raise TimeoutError(
                    f"SD card file download timed out after "
                    f"{effective_timeout:.3f} seconds. Received "
                    f"{total_bytes} bytes before timeout."
                )

            elif has_in_waiting:
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
                    # ⛔ FLUSH THE PREAMBLE SKIP FIRST. It holds bytes while a
                    # longer head candidate could still complete, so at the
                    # end of the stream a payload that is a proper prefix of
                    # one is sitting in it; raising here reported a timeout
                    # for bytes we had. Released through `injected`, the same
                    # way the deadline branch does it, so there is ONE path
                    # from "held" to "classified" rather than two.
                    if skip is not None:
                        tail = skip.flush()
                        skip = None
                        if tail:
                            injected = tail
                            continue
                    # Closed: nothing can follow, so an open transfer-error
                    # candidate is the device's marker.
                    _transfer_error_if_open()
                    raise TimeoutError(
                        f"Transport stream closed before receiving the EOF "
                        f"marker. Received {total_bytes} bytes."
                    )
            chunk_bytes = bytes(chunk)
            if skip is not None:
                # BEFORE either classification path below, deliberately: the
                # echoed command can carry a marker's own bytes. The skip
                # holds at most the head's length, so an empty result here
                # is a bounded wait for the rest of the head, not a stall.
                chunk_bytes = skip.feed(chunk_bytes)
                if skip.done:
                    skip = None
                if not chunk_bytes:
                    continue
            if sized is not None:
                body, outcome = sized.feed(chunk_bytes)
                if body:
                    _write_all(destination_stream, body, deadline)
                    total_bytes += len(body)
                    _emit_transfer_progress(progress, total_bytes, file_name)
                if outcome is not None:
                    kind, value = outcome
                    if kind == "eof":
                        self._leftover = value
                        return total_bytes
                    if kind == "transfer_error":
                        raise SdCardTransferErrorException(file_name, value)
                    raise IOError(
                        f"SD card file {file_name!r}: {expected_size} bytes "
                        "received but no end-of-file marker after them; the "
                        "file is longer than listed or the transfer is corrupt."
                    )
                continue
            scan = _find_eof_terminal_marker if eof_only \
                else _find_terminal_marker
            found, position, found_marker = scan(trailing, chunk_bytes)
            if found:
                # ``position`` is in the virtual [trailing + chunk] view.
                # Write everything before it; preserve everything after
                # in ``self._leftover`` so callers sharing the source
                # stream can avoid silent data loss.
                marker_end_in_combined = position + len(found_marker)
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
                if found_marker == TRANSFER_ERROR_MARKER:
                    # Everything written is real content, left in the
                    # destination for a caller that can use a partial log;
                    # but the file is incomplete, and that's what the
                    # marker says (upstream SdCardFileReceiver).
                    raise SdCardTransferErrorException(file_name, total_bytes)
                return total_bytes

            # No marker yet — write everything except the last ``window``
            # bytes (those become the new trailing window for next
            # iteration's spanning check).
            combined_len = len(trailing) + len(chunk_bytes)
            if combined_len >= window:
                safe_count = combined_len - window
                if safe_count > 0:
                    from_trailing = min(safe_count, len(trailing))
                    if from_trailing > 0:
                        _write_all(destination_stream, bytes(trailing[:from_trailing]), deadline)
                        total_bytes += from_trailing
                    from_new = safe_count - from_trailing
                    if from_new > 0:
                        _write_all(destination_stream, chunk_bytes[:from_new], deadline)
                        total_bytes += from_new
                # Rebuild trailing window: last ``window`` bytes of the
                # combined [trailing + chunk] sequence.
                trailing = bytearray((bytes(trailing) + chunk_bytes)[-window:])
            else:
                # Combined data still shorter than the window —
                # accumulate and keep waiting.
                trailing.extend(chunk_bytes)
            _emit_transfer_progress(progress, total_bytes, file_name)


def _find_terminal_marker(
    trailing: bytes, chunk: bytes
) -> Tuple[bool, int, bytes]:
    """The earliest end-of-file or transfer-error marker in the virtual
    ``trailing + chunk`` buffer: ``(found, position, marker)``. Upstream
    ``FindTerminalMarker``."""
    combined = bytes(trailing) + bytes(chunk)
    best = (False, -1, b"")
    for marker in (END_OF_FILE_MARKER, TRANSFER_ERROR_MARKER):
        position = combined.find(marker)
        if position >= 0 and (not best[0] or position < best[1]):
            best = (True, position, marker)
    return best


def _find_eof_terminal_marker(
    trailing: bytes, chunk: bytes
) -> Tuple[bool, int, bytes]:
    """``_find_terminal_marker`` restricted to the END-OF-FILE marker.

    ⛔ A TWIN RATHER THAN A PARAMETER, because ``_find_terminal_marker`` is
    shared with the two already-correct download callers and the ruling
    leaves it untouched. Identical scan and identical return shape, so the
    read loop's index arithmetic is unchanged -- only the marker set
    differs.

    ⚠ Not ``_find_eof_marker`` (right above), which is dead upstream-mirror
    code with DIFFERENT scan bounds: it starts at
    ``max(0, len(trailing) - len(marker) + 1)`` on the assumption that
    ``trailing`` was already scanned. Reusing it would have changed the
    loop's semantics subtly, which is the last thing this path needs.
    """
    combined = bytes(trailing) + bytes(chunk)
    position = combined.find(END_OF_FILE_MARKER)
    return (position >= 0, position, END_OF_FILE_MARKER)


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

    ``size_verified`` True means exactly the given size arrived, the
    end-of-file marker came directly after it, and only a line ending or
    prompt followed before the line went quiet. That is as strong as the
    size: SD:GET has no positive end-of-transfer signal
    (daqifi-nyquist-firmware#1180), so a wrong size whose last bytes are
    followed by marker text inside the file, then a pause, passes too
    (recorded on #164). False means no size was given and the end was found
    only by a marker at the end of the transfer, so a transfer cut short
    can't be ruled out.
    """

    file_name: str
    file_size: int
    duration: timedelta
    file_path: Optional[str] = None
    size_verified: bool = False


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
    # How the sample's firmware values were read, so a caller can check it
    # rather than rely on a warning: "volts" (written with decimals),
    # "millivolts" or "raw" (integers, as SdCardParseOptions.integer_units
    # said), or "raw-assumed" (integers read as raw codes because nobody
    # said; that case warns). None for formats without the distinction.
    analog_units: Optional[str] = None
    # The channel number of each of analog_values, in the same order. The
    # firmware logs only enabled channels, so position i is not channel i.
    # None where the file doesn't say and the caller didn't either (a
    # protobuf log without SdCardParseOptions.analog_channels).
    analog_channels: Optional[Tuple[int, ...]] = None

    def __post_init__(self) -> None:
        # Coerce the iterable inputs to tuples so mutating the
        # caller's source list after construction can't change the
        # entry — matches the frozen-dataclass guarantee already
        # used by ``SdCardDeviceConfiguration``.
        object.__setattr__(self, "analog_values", tuple(self.analog_values))
        if self.analog_channels is not None:
            object.__setattr__(self, "analog_channels", tuple(self.analog_channels))
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
    channels: Optional[Sequence[int]] = None,
) -> Tuple[float, ...]:
    """Apply calibration/range/scale factors to raw ADC integers.

    Formula: ``raw / resolution * port_range * cal_m * internal_scale_m +
    cal_b``, as the firmware converts (``MC12bADC.c``, ``AD7609Scale.h``)
    and upstream ``SdCardAnalogScaling`` scales: ``cal_b`` is an offset in
    volts, not multiplied by ``internal_scale_m``.
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
    for position, raw in enumerate(raw_values):
        # The value's channel number, where the format names it (CSV), else
        # its position (protobuf packs enabled channels in order).
        ch = channels[position] if channels is not None and position < len(channels) else position
        cal_m, cal_b = (cal[ch] if cal is not None and 0 <= ch < len(cal) else (1.0, 0.0))
        rng = (
            port_range[ch] if port_range is not None and 0 <= ch < len(port_range) else 1.0
        )
        scale_m = (
            int_scale[ch] if int_scale is not None and 0 <= ch < len(int_scale) else 1.0
        )
        normalized = raw / resolution
        out.append(normalized * rng * cal_m * scale_m + cal_b)
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
    channels: Optional[Sequence[int]] = None,
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
    channels = tuple(channels) if channels is not None else None
    warned_unscaled = False
    warned_channel_count = False

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
            # Merge only frames that complement each other (the firmware's
            # separate analog and digital messages for one period). Two frames
            # that both carry analog, or both digital, data are two samples:
            # merging kept only the first one's values (#163).
            nxt_analog = len(nxt.analog_in_data_float) > 0 or len(nxt.analog_in_data) > 0
            if (nxt_analog and (analog_float or analog_raw)) or (
                len(nxt.digital_data) > 0 and digital
            ):
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

        count = len(analog_float) or len(analog_raw)
        sample_channels = channels
        if channels is not None and count and len(channels) != count:
            if not warned_channel_count:
                warned_channel_count = True
                warnings.warn(
                    f"SdCardParseOptions.analog_channels names {len(channels)} "
                    f"channels but a protobuf sample has {count} values; "
                    "calibration is applied by position and analog_channels "
                    "is None for such samples.",
                    UserWarning,
                    stacklevel=2,
                )
            sample_channels = None
        units: Optional[str] = None
        if analog_float:
            analog_values: Tuple[float, ...] = tuple(float(v) for v in analog_float)
            units = "volts"
        elif analog_raw:
            analog_values = _scale_raw_analog_values(analog_raw, config, sample_channels)
            units = "raw"
            if (config is None or config.resolution == 0) and not warned_unscaled:
                warned_unscaled = True
                warnings.warn(_UNSCALED_RAW_WARNING, UserWarning, stacklevel=2)
        else:
            analog_values = ()
            sample_channels = ()

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
            analog_units=units,
            analog_channels=sample_channels,
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
            options.analog_channels,
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
       pairs. Decimal values are already calibrated volts; integers are
       millivolts or raw ADC counts, as ``options.integer_units`` says,
       and raw counts are scaled via the device calibration (see
       :class:`_FirmwareValueReader`).

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
        _check_integer_units(options)

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
    # Channel number of each analog column pair, from its header name
    # ("ain7_ts" -> 7). The firmware names only enabled channels, so the
    # columns are sparse and their position is not the channel (#163).
    analog_channels: Tuple[int, ...] = ()


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
    analog_channels: List[int] = []

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
                # ASCII digits only (str.isdigit also takes "²", which
                # int() rejects), within the firmware's uint32 field (a
                # huge value overflowed later). Anything else reads as an
                # absent header: the override or fallback applies, and the
                # rest of the file still parses.
                # Length first: a uint32 has at most 10 digits, and int() of
                # a long enough string raises (Python's digit limit) before
                # any range check could run.
                if rate_str.isascii() and rate_str.isdigit() and len(rate_str) <= 10:
                    try:
                        rate = int(rate_str)
                    except ValueError:
                        rate = 0
                    if 0 < rate <= _CSV_TICK_RATE_MAX:
                        timestamp_freq = rate
            continue
        if _is_csv_header_noise(line):
            # A stray "ch" before the real header: skip it, so the header
            # and its channel layout aren't lost (upstream 9aa83f5).
            continue
        # First other line — treat it as the column header iff it has the
        # firmware's per-channel naming convention.
        if _is_csv_column_header(line):
            cols = line.split(",")
            total_pairs = len(cols) // 2
            for p in range(total_pairs):
                name_col = cols[p * 2].lower()
                if name_col.startswith("dio"):
                    has_digital = True
                    digital_count = 1
                else:
                    analog_count += 1
                    number = re.match(r"\s*(?:ain|ch)(\d+)_", name_col)
                    analog_channels.append(int(number.group(1)) if number else p)
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
        analog_channels=tuple(analog_channels),
    )


def _find_csv_data_start_index(lines: List[str]) -> int:
    """Return the index of the first data row, skipping comments and
    the column header. Matches the upstream ``FindDataStartIndex``."""
    for i, line in enumerate(lines):
        if line.startswith("#"):
            continue
        if _is_csv_column_header(line) or _is_csv_header_noise(line):
            continue
        return i
    return len(lines)


# The firmware writes the tick rate from a uint32.
_CSV_TICK_RATE_MAX = 2 ** 32 - 1


def _is_csv_column_header(line: str) -> bool:
    """A CSV column header (``ain0_ts,ain0_val,...,dio_ts,dio_val`` or a
    ``ch...`` variant). Shared by the header parser and the data-start scan
    so they agree on what a header is (upstream ``IsColumnHeaderLine``)."""
    lower = line.lower()
    return (
        "_ts," in lower
        or (lower.startswith("ch") and len(line) > 2 and not line[2].isdigit())
        or lower.startswith("ain")
    )


def _is_csv_header_noise(line: str) -> bool:
    """A line starting with "ch" that isn't a column header (a bare "ch").
    A data row starts with a numeric timestamp, so it is never data
    (upstream ``IsHeaderNoiseLine``)."""
    return line.lower().startswith("ch") and not _is_csv_column_header(line)


# An integer CSV value: optional sign, ASCII digits only.
_CSV_INTEGER_VALUE = re.compile(r"[+-]?[0-9]+\Z")
# Integers beyond this aren't exact as floats; firmware codes and
# millivolts are int32.
_CSV_INT_LIMIT = 2 ** 53


def _try_parse_csv_data_row(
    line: str,
    layout: _CsvColumnLayout,
) -> Optional[Tuple[int, List[Union[int, float]], int, List[int], List[int]]]:
    """Parse one CSV data row into ``(row_timestamp, analog_values,
    digital_data, per_channel_timestamps, channels)`` or None on malformed
    input (mirrors the upstream ``TryParseCsvDataRow`` resilience).

    An empty ``,,`` pair is a channel the firmware had no conversion for in
    that period (its ``validMask`` bit clear, ``csv_encoder.c``), and a
    DIO-only row has every analog pair empty: the channel is left out of
    the row, not the row dropped. ``channels`` names the channel of each
    value kept (its header number, else its column position).

    A value written as an integer stays an ``int`` (a raw ADC code, or
    millivolts at precision 0); one written with decimals is a ``float``,
    volts from the firmware's voltage mode (``csv_encoder.c`` ``%.*f``).
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

        analog_values: List[Union[int, float]] = []
        per_channel_ts: List[int] = []
        channels: List[int] = []
        for ch in range(analog_pair_count):
            ts_col = cols[ch * 2].strip()
            val_col = cols[ch * 2 + 1].strip()
            if not ts_col and not val_col:
                continue  # no conversion for this channel in this period
            if not ts_col.isdigit():
                return None
            ts = int(ts_col)
            if _CSV_INTEGER_VALUE.match(val_col):
                val: Union[int, float] = int(val_col)
                if abs(val) > _CSV_INT_LIMIT:
                    return None  # would overflow float() during scaling
            else:
                try:
                    val = float(val_col)
                except ValueError:
                    return None
                if not math.isfinite(val):
                    return None
            per_channel_ts.append(ts)
            analog_values.append(val)
            channels.append(
                layout.analog_channels[ch] if ch < len(layout.analog_channels) else ch
            )

        digital_data = 0
        dio_timestamp = 0
        dio_read = False
        if layout.has_digital_pair and total_pairs > analog_pair_count:
            dio_index = analog_pair_count
            dio_ts_col = cols[dio_index * 2].strip()
            dio_val_col = cols[dio_index * 2 + 1].strip()
            # A DIO read needs both its stamp and its value: a truncated
            # last row (",,1420000,") is not an all-low reading.
            if dio_ts_col.isdigit() and dio_val_col.isdigit():
                dio_timestamp = int(dio_ts_col)
                digital_data = int(dio_val_col)
                dio_read = True

        row_timestamp = per_channel_ts[0] if per_channel_ts else dio_timestamp
        if not per_channel_ts and not dio_read:
            return None  # nothing in the row: no analog value, no DIO read
        return row_timestamp, analog_values, digital_data, per_channel_ts, channels
    except MemoryError:
        # Not a malformed row: the process is out of memory.
        raise
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

    # Analog and DIO stamps come from two queues (csv_encoder.c tryWriteRow
    # peeks AInSampleList and DIOSampleList, writing each sample's own
    # stamp), so a DIO-only row can sit a little before or after the
    # analog rows around it. The JSON parser's clock places such records
    # without letting them move the analog rollover base.
    clock = _JsonClock(behind_window=timestamp_freq)
    lines_processed = 0
    bytes_read = 0
    progress = options.progress
    total_bytes = sum(len(line) + 1 for line in lines)
    reader = _FirmwareValueReader(config, options.integer_units)

    for i in range(data_start, len(lines)):
        line = lines[i]
        lines_processed += 1
        bytes_read += len(line) + 1

        parsed = _try_parse_csv_data_row(line, layout)
        if parsed is None:
            continue
        row_ts, raw_analog, digital_data, per_ch_ts, channels = parsed
        # The firmware value rule shared with the JSON parser (#163).
        read = reader.read(raw_analog, channels)
        if read is None:
            continue  # a row mixing decimals and integers isn't firmware output
        analog_values, units = read

        absolute_time = base_time
        # ``row_ts == 0`` is treated as "no timestamp" — matches the
        # protobuf parser's behavior so cross-format reconstruction
        # of the same physical log is consistent.
        if tick_period > 0 and row_ts != 0:
            tick = clock.place(row_ts, analog=bool(per_ch_ts))
            absolute_time = base_time + timedelta(seconds=tick * tick_period)

        yield SdCardLogEntry(
            timestamp=absolute_time,
            analog_values=tuple(analog_values),
            digital_data=digital_data,
            analog_timestamps=tuple(per_ch_ts) if per_ch_ts else None,
            analog_units=units,
            # Without a column header nothing names the channels.
            analog_channels=tuple(channels) if layout.analog_channels else None,
        )

        if lines_processed % 100 == 0:
            _emit_progress(progress, bytes_read, total_bytes, lines_processed)

    _emit_progress(progress, bytes_read, total_bytes, lines_processed)


class SdCardJsonFileParser:
    """Parses SD-card ``.json`` log files.

    Ports ``Daqifi.Core/Device/SdCard/SdCardJsonFileParser.cs``. The
    firmware writes a ``{"meta": ...}`` header object, then one object
    per record, each spread over several lines, with analog values under
    ``"ai"`` and DIO reads under ``"di"``. The older one-line-per-sample
    shape (``{"ts": <uint32>, "analog": [...], "digital": "<hex>"}``)
    still parses. Sync end-to-end per project convention.

    Configuration is *inferred* from the header and the first sample
    with analog values; merge with
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
        _check_integer_units(options)
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

        # The firmware writes each sample object across several lines (and a
        # {"meta":...} header first), so the file is read as a sequence of
        # JSON objects, not one object per line (#163). Objects are decoded
        # lazily from the text, never all held at once.
        text = "\n".join(lines)
        del lines
        config = _infer_json_configuration(text, options)
        if config.timestamp_frequency == 0 and options.fallback_timestamp_frequency > 0:
            config = _replace_timestamp_frequency(
                config, options.fallback_timestamp_frequency
            )
        samples = _produce_json_samples(text, config, file_created_date, options)
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


def _iter_json_objects(text: str) -> Iterator[Tuple[Any, int]]:
    """Yield ``(object, end_offset)`` for each JSON value in ``text``,
    whatever the line breaks between and inside them. Undecodable text
    (a truncated last sample, stray bytes) is skipped up to the next
    ``{``."""
    import json

    decoder = json.JSONDecoder(object_pairs_hook=_JsonObject)
    position, length = 0, len(text)
    while position < length:
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            break
        try:
            obj, end = decoder.raw_decode(text, position)
        except (ValueError, RecursionError) as error:
            if isinstance(error, RecursionError):
                # Nested too deep to decode: skip the whole run of opening
                # brackets, or every "{" in it would recurse again.
                while position < length and text[position] in "{[ \t\r\n":
                    position += 1
                position -= 1
            nxt = text.find("{", position + 1)
            if nxt < 0:
                break
            position = nxt
            continue
        yield obj, end
        position = end


def _json_meta(obj: Any) -> Optional[dict]:
    meta = obj.get("meta") if isinstance(obj, dict) else None
    return meta if isinstance(meta, dict) else None


_INTEGER_UNITS = (None, "raw", "millivolts")
_UNSCALED_RAW_WARNING = (
    "This protobuf SD log's analog values are raw ADC codes and the log "
    "carries no resolution, range or calibration, so they are returned "
    "unscaled (analog_units='raw'). Pass "
    "SdCardParseOptions(configuration_override=SdCardDeviceConfiguration(...)) "
    "with resolution, port_range and calibration_values to get volts."
)

_INTEGER_UNITS_WARNING = (
    "SD log analog values are integers, read as raw ADC codes. If the log "
    "was written at voltage precision 0 they are millivolts: pass "
    "SdCardParseOptions(integer_units='millivolts'), or integer_units='raw' "
    "to confirm and silence this (daqifi-nyquist-firmware#1179)."
)


def _check_integer_units(options: SdCardParseOptions) -> None:
    if options.integer_units not in _INTEGER_UNITS:
        raise ValueError(
            "integer_units must be None, 'raw' or 'millivolts' "
            f"(got {options.integer_units!r})"
        )


class _FirmwareValueReader:
    """The one rule for firmware CSV and JSON analog values (#163).

    The firmware writes volts with decimals at VoltagePrecision >= 1 (its
    default), integer millivolts at precision 0, and integer raw codes in
    RawOutputMode; the file doesn't say which integer form it is
    (daqifi-nyquist-firmware#1179). One instance per parse, so the
    unspecified-integers warning is emitted once per parse."""

    def __init__(
        self,
        config: Optional[SdCardDeviceConfiguration],
        integer_units: Optional[str],
        warning: str = "",
    ) -> None:
        self._config = config
        self._integer_units = integer_units
        self._warning = warning or _INTEGER_UNITS_WARNING
        self._warned = False

    def read(
        self, raw: Sequence[Union[int, float]], channels: Optional[Sequence[int]] = None
    ) -> Optional[Tuple[List[float], Optional[str]]]:
        """``(values, analog_units)``, or None for a sample mixing decimals
        and integers: the value mode is one global setting, and at
        precision >= 1 even zero is written "0.0000", so that isn't firmware
        output."""
        decimals = [isinstance(v, float) for v in raw]
        if any(decimals) and not all(decimals):
            return None
        if any(decimals):
            # Already calibrated volts.
            return [float(v) for v in raw], "volts"
        if not raw:
            return [], None
        if self._integer_units == "millivolts":
            # Already calibrated, in mV.
            return [v / 1000.0 for v in raw], "millivolts"
        if self._integer_units is None and not self._warned:
            self._warned = True
            warnings.warn(self._warning, UserWarning, stacklevel=4)
        units = "raw" if self._integer_units == "raw" else "raw-assumed"
        return _scale_raw_analog_values([int(v) for v in raw], self._config, channels), units


class _JsonObject(dict):
    """A decoded JSON object that also keeps every key/value pair in order.

    The firmware's encoder writes all samples queued at one call into one
    object as repeated ``"ts"``/``"ai"`` keys (JSON_Encoder.c's
    ``while (qSize > 0)`` loop); a plain dict keeps only the last."""

    def __init__(self, pairs: List[Tuple[str, Any]]) -> None:
        super().__init__(pairs)
        self.pairs = pairs


def _split_batched_samples(obj: Any) -> Tuple[List[Any], List[Tuple[int, int]], Any]:
    """``(samples, digital ages, reference ts)`` for one decoded object.

    A firmware object with repeated ``"ts"`` keys becomes one sample per
    ``"ts"``, each with the ``"ai"`` after it and the object's other fields;
    a single-sample object takes its fields by key. A firmware object with a
    ``"di"`` array gives its entries as ``(age, value)``, and its samples
    lose the array (an ``"ai"`` of their own, possibly empty, keeps them in
    the firmware shape). Anything else is one sample and no ages.

    ``"di"`` entries carry ages, not times: each is measured back from the
    trigger stamp at encode time, which an ADC object doesn't include. That
    is a documented firmware limitation (JSON_Encoder.c, at the di element
    writer), tracked on daqifi-nyquist-firmware#959. The object's latest
    sample stands in for it (the returned reference ts), off by (ADC ts -
    trigger stamp) when the ADC lags. Ages are uint32 differences read as
    signed int32: a read just after the trigger (age 0xFFFFFFFF) is one tick
    in the future."""
    pairs = getattr(obj, "pairs", None)
    if not pairs or (
        sum(1 for key, _ in pairs if key == "ts") < 2 and "di" not in obj
    ):
        return [obj], [], None
    shared = {key: value for key, value in pairs if key not in ("ts", "ai", "di")}
    samples: List[dict] = []
    if sum(1 for key, _ in pairs if key == "ts") == 1:
        # One sample: its fields by key, in whatever order they came.
        samples.append(dict(shared, ts=obj["ts"], ai=obj.get("ai", [])))
    else:
        # Batched: the encoder writes each "ts" before its "ai".
        for key, value in pairs:
            if key == "ts":
                samples.append(dict(shared, ts=value, ai=[]))
            elif key == "ai" and samples:
                samples[-1]["ai"] = value
    if not samples:
        return [obj], [], None  # no "ts" at all: left to the sample parser to reject
    di = obj.get("di")
    if "di" in obj and not isinstance(di, list):
        for sample in samples:
            sample["di"] = di  # malformed: left for the sample parser to reject
        return samples, [], None
    ages: List[Tuple[int, int]] = []
    for entry in di or []:
        if not isinstance(entry, dict):
            continue
        age, value = entry.get("ts"), entry.get("val")
        if _is_plain_int(age) and _is_plain_int(value):
            ages.append((_signed32(age), value & 0xFFFFFFFF))
    return samples, ages, samples[-1]["ts"]


class _JsonClock:
    """Unwrapped ticks for a JSON parse's records, from their uint32 "ts"
    (and for a CSV parse's rows, whose DIO-only rows are digital-only
    records: csv_encoder.c takes analog and DIO from separate queues).

    Anchored at tick 0 on the first record of any kind. An analog record
    moves the base: forward with rollover (as the other formats), or, when
    the base was last moved by a digital-only record, by a signed step,
    since an ADC sample queued for the next object can be older than a
    digital record stamped with the trigger (JSON_Encoder.c). A digital-only
    record moves the base only while no analog record has been seen, or if
    it is behind the latest analog tick; otherwise it is placed without
    moving it, so a digital record level with the analog base doesn't
    change how the next analog step is read.

    A digital-only record up to one second (``behind_window`` ticks) behind
    an analog base is placed behind it: its trigger stamp is ISR-captured
    and can trail an already placed, period-derived ADC stamp.

    Limits (recorded on #164, tied to daqifi-nyquist-firmware#959): after
    the last analog record, digital-only records are placed by their
    distance from it, so a digital-only stretch longer than one timer period
    (2**32 ticks, about 102 s at 42 MHz) loses its rollover count; that
    needs ADC acquisition to stop mid-session while DIO continues, which a
    session's fixed stream configuration doesn't do. And a digital-only
    record within one second of a full period after the analog base reads
    as slightly behind it instead: uint32 stamps can't tell the two apart."""

    def __init__(self, behind_window: int = 0) -> None:
        # How far (ticks) a digital-only record may sit behind an analog
        # base and still count as behind it: one second of the tick rate.
        self._behind_window = max(0, behind_window)
        self._anchored = False
        self._base_raw = 0
        self._base_tick = 0
        self._base_by_digital = False
        self._latest_analog: Optional[int] = None

    def place(self, ts: Any, analog: bool) -> Optional[int]:
        """The record's unwrapped tick; None if ``ts`` isn't an integer.

        For firmware records 0 is a real tick, time included: the one after
        a rollover on firmware before #533 (current firmware clamps stamps
        to 1). Legacy "analog" rows with ts 0 mean "no timestamp" and
        aren't placed at all."""
        if not _is_plain_int(ts):
            return None
        if not self._anchored:
            self._anchored = True
            self._move(ts, 0, by_digital=not analog)
            if analog:
                self._latest_analog = 0
            return 0
        if analog:
            step = (
                _signed32(ts - self._base_raw) if self._base_by_digital
                else _compute_tick_delta(self._base_raw, ts)
            )
            tick = self._base_tick + step
            self._move(ts, tick, by_digital=False)
            self._latest_analog = tick
            return tick
        step = _compute_tick_delta(self._base_raw, ts)
        behind = 0x100000000 - step if step else 0
        if not self._base_by_digital and 0 < behind <= self._behind_window:
            # Slightly behind an analog base: an ISR trigger stamp trailing
            # a period-derived ADC stamp, not a rollover.
            step = -behind
        tick = self._base_tick + step
        if self._latest_analog is None or tick < self._latest_analog:
            self._move(ts, tick, by_digital=True)
        return tick

    def _move(self, ts: int, tick: int, by_digital: bool) -> None:
        self._base_raw, self._base_tick, self._base_by_digital = ts, tick, by_digital


class _DigitalHistory:
    """Digital port events by unwrapped tick over one parse.

    A sample takes the latest event at or before its own tick: the port
    holds its value between reads, across objects, and a sample older than
    an event already seen (a queued ADC sample) still gets the state of its
    own time. Events are never consumed, and every event of the parse is
    loaded before any lookup (the file is already in memory)."""

    def __init__(self) -> None:
        self._events: List[Tuple[int, int, int]] = []  # (tick, order, value)
        self._order = 0

    def add(self, tick: int, value: int) -> None:
        import bisect

        self._order += 1
        bisect.insort(self._events, (tick, self._order, value))

    def at(self, tick: Optional[int]) -> int:
        import bisect

        if not self._events:
            return 0
        if tick is None:
            return 0  # no tick: no state known
        index = bisect.bisect_right(self._events, (tick, float("inf"), 0))
        return self._events[index - 1][2] if index else 0


def _signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _is_digital_only_record(obj: Any, parsed_samples: List[Any], converted: Callable[[Any], Any]) -> bool:
    """True for a firmware record of DIO reads only: one sample, in the
    firmware shape, with no analog values, and a "di" array. Such a record
    yields one sample per "di" element (one per sample period)."""
    if len(parsed_samples) != 1 or not isinstance(obj, dict) or not isinstance(obj.get("di"), list):
        return False
    sample = converted(parsed_samples[0])
    return sample is not None and sample[5]  # digital_only


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# What a malformed record can raise while being taken apart. Anything else
# (MemoryError above all) is not a bad record and must not be swallowed as
# one: under memory pressure that silently dropped samples.
_MALFORMED_JSON_ERRORS = (
    ValueError, TypeError, KeyError, IndexError, AttributeError,
    OverflowError, RecursionError,
)


def _try_parse_json_samples(
    obj: Any,
) -> Tuple[List[Tuple[int, List[Union[int, float]], Optional[List[int]], int]], List[Tuple[int, int]], Any]:
    """``(well-formed samples, digital ages, reference ts)`` in ``obj``; a
    malformed sample is skipped, and a record malformed beyond splitting
    gives nothing."""
    try:
        split, ages, reference = _split_batched_samples(obj)
    except _MALFORMED_JSON_ERRORS:
        return [], [], None
    out = []
    for sample in split:
        parsed = _try_parse_json_sample(sample)
        if parsed is not None:
            out.append(parsed)
    return out, ages, reference


def _try_parse_json_sample(
    obj: Any,
) -> Optional[Tuple[int, List[Union[int, float]], Optional[List[int]], int]]:
    """See _parse_json_sample; None for anything malformed, so one bad
    record is skipped instead of aborting the whole log."""
    try:
        return _parse_json_sample(obj)
    except _MALFORMED_JSON_ERRORS:
        return None


def _parse_json_sample(
    obj: Any,
) -> Optional[Tuple[int, List[Union[int, float]], Optional[List[int]], int]]:
    """``(timestamp, analog_values, channels, digital_data)`` from one sample
    object, or None if it isn't one.

    Two shapes: the firmware's (JSON_Encoder.c) ``{"ts":t, "ai":[{"ch":n,
    "val":v},...], "di":[{"ts":t, "mask":m, "val":b},...]}``, whose analog
    entries name their channel, and the older ``{"ts":t, "analog":[v,...],
    "digital":"AB-CD"}`` (upstream's TryParseJsonLine), positional.
    ``channels`` is None for the positional shape."""
    if not isinstance(obj, dict) or _json_meta(obj) is not None:
        return None
    ts_raw = obj.get("ts")
    if not isinstance(ts_raw, int) or isinstance(ts_raw, bool) or not 0 <= ts_raw <= 0xFFFFFFFF:
        return None
    # Kept as int or float: a decimal value is volts, an integer a code.
    analog_values: List[Union[int, float]] = []
    channels: Optional[List[int]] = None
    digital_data = 0
    if "ai" in obj or "di" in obj:
        channels = []
        # Absent means none; present must be a list (0, "" or {} isn't one).
        ai = obj.get("ai", [])
        di = obj.get("di", [])
        if not isinstance(ai, list) or not isinstance(di, list):
            return None
        for entry in ai:
            if not isinstance(entry, dict):
                return None
            ch, val = entry.get("ch"), entry.get("val")
            if (
                not isinstance(ch, int) or isinstance(ch, bool) or ch < 0
                or not _is_finite_number(val)
            ):
                return None
            channels.append(ch)
            analog_values.append(val)
        if di:
            last = di[-1]  # the latest port state in this period
            val = last.get("val") if isinstance(last, dict) else None
            if isinstance(val, int) and not isinstance(val, bool):
                digital_data = val & 0xFFFFFFFF
        return ts_raw, analog_values, channels, digital_data
    analog_raw = obj.get("analog")
    if not isinstance(analog_raw, list):
        return None
    for v in analog_raw:
        if not _is_finite_number(v):
            return None
        analog_values.append(v)
    digital_raw = obj.get("digital")
    if isinstance(digital_raw, str) and digital_raw:
        digital_data = _parse_digital_hex_string(digital_raw)
    return ts_raw, analog_values, None, digital_data


# Integers beyond this aren't exact as floats, and far beyond it overflow
# float() during scaling.
_JSON_INT_LIMIT = 2 ** 53


def _is_finite_number(value: Any) -> bool:
    # JSON gives inf for 1e309 and accepts NaN/Infinity, and integers of any
    # size; none of those survive scaling, which runs outside the
    # malformed-record handler.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return -_JSON_INT_LIMIT <= value <= _JSON_INT_LIMIT
    return math.isfinite(value)


def _try_parse_json_line(line: str) -> Optional[Tuple[int, List[float], int]]:
    """One JSON sample on one line (kept for callers of the old API)."""
    import json

    try:
        parsed = _try_parse_json_sample(json.loads(line))
    except _MALFORMED_JSON_ERRORS:
        return None
    if parsed is None:
        return None
    ts, values, _channels, digital = parsed
    return ts, [float(v) for v in values], digital


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
    text: str,
    options: SdCardParseOptions,
) -> SdCardDeviceConfiguration:
    """Device metadata from the firmware's leading {"meta":...} header
    (device name, serial, tick rate) when present, and the analog channel
    count from the first sample with analog values (digital-only records
    may come first); then merged with the caller's override,
    which supplies resolution/calibration/range for scaling. Same
    precedence as the CSV parser: file, then override, then fallback."""
    meta: dict = {}
    analog_count = 0
    for index, (obj, _end) in enumerate(_iter_json_objects(text)):
        found = _json_meta(obj) if index < 2 else None
        if found is not None:
            meta = found
            continue
        parsed, _ages, _reference = _try_parse_json_samples(obj)
        with_analog = [sample for sample in parsed if sample[1]]
        if with_analog:
            analog_count = len(with_analog[0][1])
            break
    tick = meta.get("tick_hz")
    inferred = SdCardDeviceConfiguration(
        analog_port_count=analog_count,
        digital_port_count=0,
        # The firmware's tick_hz is a uint32; anything else is a damaged
        # header, and a huge int would overflow 1.0 / tick later.
        timestamp_frequency=(
            tick if isinstance(tick, int) and not isinstance(tick, bool)
            and 0 < tick <= 0xFFFFFFFF else 0
        ),
        device_serial_number=meta.get("sn") if isinstance(meta.get("sn"), str) else None,
        device_part_number=meta.get("dev") if isinstance(meta.get("dev"), str) else None,
        firmware_revision=None,
        calibration_values=None,
    )
    return _merge_csv_json_config(inferred, options.configuration_override)


def _produce_json_samples(
    text: str,
    config: SdCardDeviceConfiguration,
    file_created_date: Optional[datetime],
    options: SdCardParseOptions,
) -> Iterator[SdCardLogEntry]:
    base_time = _resolve_session_base_time(file_created_date)
    timestamp_freq = config.timestamp_frequency
    tick_period = 1.0 / timestamp_freq if timestamp_freq > 0 else 0.0
    progress = options.progress
    reader = _FirmwareValueReader(config, options.integer_units)

    def converted(parsed):
        """``(ts, analog_values, units, channels, digital_data, digital_only)``
        for one parsed sample, or None if the firmware rule rejects it."""
        ts, raw_analog, channels, digital_data = parsed
        units: Optional[str] = None
        if channels is not None:
            # The firmware's "ai" shape: the shared firmware rule.
            read = reader.read(raw_analog, channels)
            if read is None:
                return None
            analog_values, units = read
        else:
            # The legacy "analog" shape is raw counts, as upstream scales
            # it, whatever the number syntax.
            analog_values = _scale_raw_analog_values(
                [int(v) for v in raw_analog], config, channels
            )
        digital_only = channels is not None and not raw_analog
        return ts, analog_values, units, channels, digital_data, digital_only

    # Two passes over the text, decoding lazily each time, so memory stays
    # proportional to the samples' ticks rather than to every decoded object
    # and sample. Pass 1 places every sample and loads every digital event;
    # pass 2 emits. The firmware can defer its DIO block to a later object
    # (JSON_Encoder.c), so an event can arrive after a sample it applies to.
    from array import array

    no_tick = -(2 ** 63)
    ticks = array("q")
    clock = _JsonClock(behind_window=timestamp_freq)
    digital = _DigitalHistory()
    total_bytes = 0
    for obj, end in _iter_json_objects(text):
        total_bytes = end
        parsed_samples, ages, reference = _try_parse_json_samples(obj)
        if _is_digital_only_record(obj, parsed_samples, converted):
            # One sample per sample period: each "di" element is one DIO
            # read (DIO.c pushes one per streaming tick), at the record's
            # trigger tick minus its age.
            # Its ts is an integer (the sample parser checked), so it has a tick.
            tick = clock.place(parsed_samples[0][0], analog=False)
            for age, value in ages:
                digital.add(tick - age, value)
                ticks.append(tick - age)
            continue
        # Every sample of the object is placed first: its digital ages are
        # measured back from its latest sample.
        last_ts, last_tick = None, None
        for parsed in parsed_samples:
            sample = converted(parsed)
            if sample is None:
                continue
            ts, _values, _units, channels, _digital, digital_only = sample
            if channels is None and ts == 0:
                # The legacy "analog" shape uses ts 0 for "no timestamp"
                # (as the protobuf and CSV parsers do): it doesn't move the
                # clock. In the firmware shape 0 is a real tick.
                tick = None
            else:
                tick = clock.place(ts, analog=not digital_only)
            ticks.append(no_tick if tick is None else tick)
            last_ts, last_tick = ts, tick
        if ages and last_ts == reference and last_tick is not None:
            for age, value in ages:
                digital.add(last_tick - age, value)

    lines_processed = 0
    bytes_read = 0
    index = 0
    for obj, end in _iter_json_objects(text):
        lines_processed += 1
        bytes_read = end
        parsed_samples, ages, _reference = _try_parse_json_samples(obj)
        if _is_digital_only_record(obj, parsed_samples, converted):
            # One sample per DIO read, each with its own value and time.
            for _age, value in ages:
                tick = ticks[index]
                index += 1
                absolute_time = base_time
                if tick_period > 0:
                    absolute_time = base_time + timedelta(seconds=tick * tick_period)
                yield SdCardLogEntry(
                    timestamp=absolute_time,
                    analog_values=(),
                    digital_data=value,
                    analog_timestamps=None,
                    analog_channels=(),
                )
            parsed_samples = []
        for parsed in parsed_samples:
            sample = converted(parsed)
            if sample is None:
                continue
            ts, analog_values, units, channels, digital_data, _digital_only = sample
            tick = ticks[index]
            index += 1
            tick = None if tick == no_tick else tick
            if channels is not None:
                # The port state at the sample's own time.
                digital_data = digital.at(tick)
            # A legacy row with ts 0 is "no timestamp" (no tick): session
            # start, as in the protobuf parser, so the same physical log
            # reconstructs consistently across formats. A firmware sample at
            # tick 0 (after a rollover) keeps its reconstructed time.
            absolute_time = base_time
            if tick_period > 0 and tick is not None:
                absolute_time = base_time + timedelta(seconds=tick * tick_period)
            yield SdCardLogEntry(
                timestamp=absolute_time,
                analog_values=tuple(analog_values),
                digital_data=digital_data,
                analog_timestamps=None,  # JSON format carries one ts per row
                analog_units=units,
                analog_channels=tuple(channels) if channels is not None else None,
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
