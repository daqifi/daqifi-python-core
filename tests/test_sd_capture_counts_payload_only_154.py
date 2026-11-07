"""r3's two HIGHs, both in the capture loop r3 itself added.

One root cause, two symptoms: the loop measured ``len(out)`` and scanned for
terminal markers using its own approximations of "where the framing ends",
instead of the definition the receiver strips with. So ~41 bytes of echo and
prompt counted toward a PAYLOAD-sized target, and marker-shaped CONTENT could
end the capture before the listed size was reached.

``_PreambleSkip.head_span`` is now that single definition, and while a known
size is unmet nothing but the caller's own timeout ends the capture.
"""

import time

import pytest

from daqifi._internal import scpi_commands
from daqifi.device import NyquistDevice
from daqifi.sdcard import END_OF_FILE_MARKER, TRANSFER_ERROR_MARKER

PROMPT = b"DAQIFI> "


class _Serial:
    """Serves one GET reply, optionally in timed stages."""

    def __init__(self, *stages) -> None:
        # stages: (delay_before, bytes) pairs
        self.is_open = True
        self.written: list = []
        self._stages = list(stages)
        self._pending = bytearray()
        self._opened_at = None

    def write(self, data) -> int:
        self.written.append(bytes(data))
        if b"SD:GET" in bytes(data):
            self._opened_at = time.monotonic()
        return len(data)

    def flush(self) -> None:
        pass

    def _release(self) -> None:
        if self._opened_at is None:
            return
        elapsed = time.monotonic() - self._opened_at
        while self._stages and self._stages[0][0] <= elapsed:
            self._pending.extend(self._stages.pop(0)[1])

    @property
    def in_waiting(self) -> int:
        self._release()
        return len(self._pending)

    def read(self, n: int) -> bytes:
        self._release()
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out


def _device(*stages) -> NyquistDevice:
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(*stages)
    return device


def _echo(name: str) -> bytes:
    return scpi_commands.get_sd_file(name).data.encode("ascii")


# -- A: framing bytes counted toward a payload-sized target ----------------


def test_the_framing_does_not_count_toward_the_listed_size() -> None:
    """⛔ r3 HIGH A, codex's payload. With echo on, ~41 bytes of echo and
    prompt satisfied a 23-byte payload target early. The capture stopped at
    the marker INSIDE the content, the sized interpretation then failed, and
    the hint fallback silently returned b'head' -- leaving the rest on the
    wire. The size exists precisely to read past that marker.
    """
    name = "log.bin"
    body = b"head" + END_OF_FILE_MARKER + b"tail"
    assert len(body) == 23
    framing = _echo(name) + b"\r\n" + PROMPT
    assert len(framing) == 41, "the audit's framing length"
    # ⚠ DELIVERED IN STAGES, and it has to be. A single chunk hands the
    # capture every byte before it evaluates its stop rule, so nothing is
    # lost and the defect cannot appear -- my first version of this test
    # did exactly that and PASSED on the broken head. The payload-only
    # target is 38 bytes (23 + the terminator); the framing alone is 41, so
    # the broken rule is already satisfied when the first content arrives.
    device = _device(
        (0.0, framing),
        (0.0, b"head" + END_OF_FILE_MARKER),
        (0.4, b"tail" + END_OF_FILE_MARKER),
    )
    device._file_sizes = {name: len(body)}
    assert device.sd_get_file_binary(name, timeout=5.0) == body


def test_with_echo_off_the_payload_still_starts_at_zero() -> None:
    """The same counting, with no framing at all: head_span returns 0 and
    the target means what it says."""
    name = "log.bin"
    body = b"head" + END_OF_FILE_MARKER + b"tail"
    device = _device(
        (0.0, b"head" + END_OF_FILE_MARKER),
        (0.4, b"tail" + END_OF_FILE_MARKER),
    )
    device._file_sizes = {name: len(body)}
    assert device.sd_get_file_binary(name, timeout=5.0) == body


# -- B: the grace fired on marker-shaped content before the target ---------


def test_a_pause_inside_the_payload_does_not_end_the_capture() -> None:
    """⛔ r3 HIGH B, codex's timing. ``abc__TRANSFER_ERROR__`` arrives, then
    a pause longer than the 0.3 s grace, then ``xyz__END_OF_FILE__``. The
    grace stopped the capture on content that merely LOOKS like a terminator
    and it raised SdCardTransferErrorException, where the base returns all
    24 bytes.

    While a listed size is unmet the payload is still arriving, whatever the
    bytes look like, so only the caller's timeout may end the capture.
    """
    name = "log.bin"
    body = b"abc" + TRANSFER_ERROR_MARKER + b"xyz"
    assert len(body) == 24
    device = _device(
        (0.0, b"abc" + TRANSFER_ERROR_MARKER),
        (0.6, b"xyz" + END_OF_FILE_MARKER),
    )
    device._file_sizes = {name: len(body)}
    assert device.sd_get_file_binary(name, timeout=5.0) == body


def test_an_embedded_eof_marker_with_a_pause_is_not_a_short_read() -> None:
    """The same shape with ``__END_OF_FILE__`` embedded instead, where the
    symptom was a SILENT short read of b'abc' rather than an exception."""
    name = "log.bin"
    body = b"abc" + END_OF_FILE_MARKER + b"xyz"
    device = _device(
        (0.0, b"abc" + END_OF_FILE_MARKER),
        (0.6, b"xyz" + END_OF_FILE_MARKER),
    )
    device._file_sizes = {name: len(body)}
    assert device.sd_get_file_binary(name, timeout=5.0) == body


# -- the guards: what the narrowing must NOT have cost ---------------------


def test_an_unsized_download_still_stops_at_the_first_marker() -> None:
    """With no listed size there is nothing to wait for, so the first EOF
    marker still ends the capture immediately -- the base behaviour, and
    what keeps an ordinary download fast."""
    device = _device((0.0, b"payload" + END_OF_FILE_MARKER))
    started = time.monotonic()
    assert device.sd_get_file_binary("log.bin", timeout=5.0) == b"payload"
    assert time.monotonic() - started < 1.0, (
        "an unsized download waited for something"
    )


def test_a_stale_larger_size_now_costs_the_TIMEOUT_not_the_grace() -> None:
    """⚠ THE PRICE OF FIXING HIGH B, and r3 pinned the opposite.

    r3 made a stale LARGER size fall back after a 0.3 s grace. That is the
    SAME OBSERVATION as codex's scenario B -- a terminal marker present, the
    payload below the listed size, the line quiet -- and the two cannot be
    told apart, because content is allowed to contain either marker. One of
    them has to lose: stopping early returns a silently short file, waiting
    returns the right bytes slowly. So the capture waits.

    Correctness is preserved; promptness is not. A stale larger size now
    costs the caller's timeout.
    """
    name = "log.bin"
    device = _device((0.0, b"abcdef" + END_OF_FILE_MARKER))
    device._file_sizes = {name: 9999}
    started = time.monotonic()
    assert device.sd_get_file_binary(name, timeout=1.0) == b"abcdef"
    elapsed = time.monotonic() - started
    assert elapsed >= 1.0, (
        "it returned before the timeout, so something still ends a capture "
        "early while a listed size is unmet"
    )
