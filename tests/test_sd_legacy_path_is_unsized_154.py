"""Ruling FO A2: the legacy SD download drops the size logic AND the
error-marker detection, keeping the prefix-strip and the delegation.

⛔ WHY, in one line: every HIGH from r2 to r4 came from the size machinery,
and what it bought -- reading past a terminal marker that appears inside file
content -- is a capability neither #154 nor #206 asked for and the base never
had.

This file is the failing leg for each behaviour change, and it replaces
``test_sd_capture_counts_payload_only_154.py``, whose subject (a size-aware
capture loop) no longer exists.

WHAT THE REMOVAL RETIRES, by audit finding:
  r2 #1  the cached size going stale and being ENFORCED            — no size
  r2 #2  basename matching borrowing a size across directories     — no size
  r2 #3  a listed 0 failing a truthiness test                      — no size
  r3 A   framing bytes counting toward a payload-sized target      — no target
  r3 B   the grace stopping on marker-shaped content               — no grace
  r4 #1  an embedded EOF breaking before a split error marker      — no size
  r4 #2  the overlap at a join raising on a valid transfer         — no detection
  r4 #3  the preamble holding a payload on a serial-shaped source  — fixed here
"""

import io
import time

import pytest

from _sd_echo_probe import EchoProbeFake
from daqifi._internal import scpi_commands
from daqifi.device import NyquistDevice
from daqifi.exceptions import SdCardTransferErrorException
from daqifi.sdcard import (
    END_OF_FILE_MARKER,
    TRANSFER_ERROR_MARKER,
    SdCardFileReceiver,
)

PROMPT = b"DAQIFI> "


class _Serial(EchoProbeFake):
    """Serial-shaped: exposes ``in_waiting`` and never reports EOF, which is
    what pyserial does and what makes an absent marker a timeout."""
    out_waiting = 0  # #263: the send path drains this queue, bounded

    def __init__(self, *stages) -> None:
        self.is_open = True
        self.written: list = []
        self._stages = list(stages)
        self._pending = bytearray()
        # ⚠ Set at CONSTRUCTION, not on a GET. An earlier version started the
        # clock only when it saw an SD:GET, so a listing released nothing and
        # a test handing this straight to the receiver released nothing at
        # all -- both failed as "the listing did not cache" and "0 bytes",
        # which look like product defects and were mine.
        self._t0 = time.monotonic()

    def write(self, data) -> int:
        self.written.append(bytes(data))
        if self._probe_write(data):
            return len(data)
        if b"SD:GET" in bytes(data):
            # Staged delays are measured from the GET when there is one.
            self._t0 = time.monotonic()
        return len(data)

    def flush(self) -> None:
        pass

    def _release(self) -> None:
        elapsed = time.monotonic() - self._t0
        while self._stages and self._stages[0][0] <= elapsed:
            self._pending.extend(self._stages.pop(0)[1])

    @property
    def in_waiting(self) -> int:
        def _own():
            self._release()
            return len(self._pending)
        return self._probe_pending(_own)

    def read(self, n: int) -> bytes:
        def _own():
            self._release()
            out = bytes(self._pending[:n])
            del self._pending[:n]
            return out
        return self._probe_read(n, _own)


def _device(*stages) -> NyquistDevice:
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(*stages)
    return device


def _echo(name: str) -> bytes:
    return scpi_commands.get_sd_file(name).data.encode("ascii")


# -- behaviour change 1: no size, so an embedded marker truncates ----------


def test_a_marker_inside_content_truncates_there_again() -> None:
    """⛔ THE CAPABILITY BEING WITHDRAWN, pinned so nobody restores it by
    accident. A listed size let the receiver read past an ``__END_OF_FILE__``
    occurring inside file content. That is gone: the first terminal marker
    ends the file, which is what the base did.

    Retires r2 #1/#2/#3, r3 A and r4 #1 -- all of which were the machinery
    that made this case work.
    """
    name = "log.bin"
    body = b"head" + END_OF_FILE_MARKER + b"tail"
    device = _device((0.0, body + END_OF_FILE_MARKER))
    # A listing is present and says 23; it is no longer consulted.
    device._file_sizes = {name: len(body)}
    assert device.sd_get_file_binary(name, timeout=2.0) == b"head"


def test_the_listed_size_is_not_consulted_by_the_download() -> None:
    """The same point from the other side: a WRONG listed size cannot
    affect the result, because nothing reads it. This is what makes the
    eight staleness paths harmless rather than merely unlikely."""
    name = "log.bin"
    device = _device((0.0, b"abcdef" + END_OF_FILE_MARKER))
    for wrong in (0, 3, 9999):
        device._comm._serial = _Serial((0.0, b"abcdef" + END_OF_FILE_MARKER))
        device._file_sizes = {name: wrong}
        assert device.sd_get_file_binary(name, timeout=2.0) == b"abcdef", (
            f"a listed size of {wrong} changed the result"
        )


# -- behaviour change 2: no error-marker detection on this path ------------


def test_a_transfer_error_no_longer_raises_on_the_legacy_path() -> None:
    """⛔ A file may legitimately contain ``__TRANSFER_ERROR__``, so this
    path stopped classifying it. A failed transfer reads as a timeout, as it
    did before this PR; #253 is where the reason becomes available.
    """
    device = _device((0.0, b"partial" + TRANSFER_ERROR_MARKER))
    with pytest.raises(TimeoutError):
        device.sd_get_file_binary("log.bin", timeout=0.4)


def test_the_overlap_at_a_join_no_longer_raises() -> None:
    """⛔ r4 #2, RETIRED BY THE REMOVAL rather than by touching the shared
    scan. Content ending ``...__TRANSFER_ERROR`` followed by the real
    ``__END_OF_FILE__`` joins into ``__TRANSFER_ERROR__END_OF_FILE__``,
    completing the error marker by accident two characters in. Earliest-wins
    picked it and raised on a valid transfer.

    ``_find_terminal_marker`` is untouched, per the ruling -- it is shared
    with the two already-correct callers. The legacy path simply no longer
    asks it about error markers.
    """
    body = b"abc" + TRANSFER_ERROR_MARKER[:-2]      # ...__TRANSFER_ERROR
    assert not body.endswith(TRANSFER_ERROR_MARKER)
    joined = body + END_OF_FILE_MARKER
    assert TRANSFER_ERROR_MARKER in joined, "the join must form the marker"
    device = _device((0.0, joined))
    assert device.sd_get_file_binary("log.bin", timeout=2.0) == body


def test_the_modern_receiver_still_classifies_error_markers() -> None:
    """⚠ THE CONTAINMENT. Dropping detection is legacy-path only: the
    receiver's default still raises, which is what the streaming-device and
    download-to-temp callers rely on."""
    source = io.BytesIO(b"partial" + TRANSFER_ERROR_MARKER)
    with pytest.raises(SdCardTransferErrorException):
        SdCardFileReceiver(source).receive(io.BytesIO(), "log.bin", timeout_s=2.0)


# -- behaviour change 3: r4 #3, the held payload on a serial source -------


def test_a_held_payload_is_not_lost_on_a_serial_shaped_source() -> None:
    """⛔ r4 #3. The preamble skip holds bytes while a longer head candidate
    could still complete. On a BytesIO that resolves at EOF, but a
    serial-shaped source never reports EOF -- so the deadline expired with
    the whole payload still in the filter and it raised a timeout reporting
    0 bytes.

    Both the EOF and the deadline paths now release the held bytes through
    one mechanism and classify them.
    """
    name = "__END_OF_FILE__"
    stream = b'SYSTem:STORage:SD:GET "__END_OF_FILE__'
    assert _echo(name).startswith(stream), "must be a proper prefix of the echo"
    # Released at the deadline, the held bytes turn out to END with the EOF
    # marker, so the transfer completes rather than timing out: the payload
    # is recovered, not merely counted. On 7ccdc480 this raised
    # "Transport stream closed... Received 0 bytes" on a BytesIO and timed
    # out at 0 bytes on a serial-shaped source -- reporting nothing received
    # while holding the whole thing.
    out = io.BytesIO()
    written = SdCardFileReceiver(_Serial((0.0, stream))).receive(
        out, name, timeout_s=0.4, preamble=_echo(name), eof_only=True,
    )
    assert out.getvalue() == b'SYSTem:STORage:SD:GET "'
    assert written == 23


# -- the guards: what the ruling KEEPS must still work --------------------


def test_the_prefix_strip_is_kept() -> None:
    """#206 item 5 is unaffected by the removal: the echo and prompt still
    go as a PREFIX, never by searching 200 bytes."""
    name = "log.bin"
    device = _device((0.0, _echo(name) + b"\r\n" + PROMPT + b"payload"
                      + END_OF_FILE_MARKER))
    assert device.sd_get_file_binary(name, timeout=2.0) == b"payload"


def test_a_payload_containing_the_prompt_keeps_its_head() -> None:
    """The #206 defect itself: with echo off, a file containing ``DAQIFI>``
    used to lose everything before it."""
    payload = b"row,1\nnote: DAQIFI> is a prompt\nrow,2\n"
    device = _device((0.0, payload + END_OF_FILE_MARKER))
    assert device.sd_get_file_binary("log.bin", timeout=2.0) == payload


def test_the_text_path_still_keeps_its_own_whitespace_and_occurrences() -> None:
    """The `.replace()` sweeps and the `.strip()` are still gone."""
    payload = "   DAQIFI> twice: DAQIFI> here\nand data\n\n"
    device = _device((0.0, payload.encode("ascii") + END_OF_FILE_MARKER))
    assert device.sd_get_file("log.csv", timeout=2.0) == payload


def test_sd_get_file_size_still_reports_and_still_forgets() -> None:
    """The cache survives for the public getter, and still invalidates --
    it just no longer steers a download."""
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(
        (0.0, b"DAQiFi/log.bin 3\r\n__END_OF_LIST__ OK\r\n"),
    )
    device.sd_list_files()
    assert device.sd_get_file_size("DAQiFi/log.bin") == 3
    device._forget_sd_file_sizes()
    assert device.sd_get_file_size("DAQiFi/log.bin") == 0
