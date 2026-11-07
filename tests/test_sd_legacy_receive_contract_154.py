"""WP4: the legacy SD receive path reimplemented the transfer contract.

ROOT CAUSE: ``NyquistDevice.sd_get_file`` / ``sd_get_file_binary`` carry
their own copy of the SD receive contract instead of using the one
implementation that encodes it (``SdCardFileReceiver``). Every test here is
an instance of that duplication costing something, and each fails on the
WP4 base.

⛔ WHAT MAKES THIS ORDER-CRITICAL rather than cosmetic: the echoed command
line can itself contain a terminal marker's bytes -- a file named
``__TRANSFER_ERROR__.bin`` is the case already pinned by
``test_sd_transfer_error_154.py``. So the head has to be removed BEFORE any
marker classification, which is why the receiver gained a preamble
parameter rather than the caller stripping afterwards.
"""

import io

import pytest

from daqifi._internal import scpi_commands
from daqifi.device import NyquistDevice
from daqifi.exceptions import SdCardTransferErrorException
from daqifi.sdcard import (
    END_OF_FILE_MARKER,
    TRANSFER_ERROR_MARKER,
    SdCardFileReceiver,
)

PROMPT = b"DAQIFI> "


class _Serial:
    """pyserial-shaped fake: ``in_waiting`` plus a draining ``read``.

    Stays "open" after its bytes run out, which is what pyserial does and
    what makes an absent terminal marker a timeout rather than an EOF.
    """

    def __init__(self, *chunks: bytes) -> None:
        self.is_open = True
        self.written: list = []
        self._data = bytearray(b"".join(chunks))

    def write(self, data) -> int:
        self.written.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        return len(self._data)

    def read(self, n: int) -> bytes:
        out = bytes(self._data[:n])
        del self._data[:n]
        return out


def _device(*chunks: bytes, sizes=None) -> NyquistDevice:
    device = NyquistDevice("COM41")          # constructing opens no port
    device._comm._serial = _Serial(*chunks)
    if sizes:
        device._file_sizes = dict(sizes)
    return device


def _echo_of(name: str) -> bytes:
    return scpi_commands.get_sd_file(name).data.encode("ascii")


# -- instance 1: the head is stripped by SEARCH, so content loses its head --


def test_a_binary_payload_containing_the_prompt_keeps_its_head() -> None:
    """``find`` locates ``DAQIFI>`` ANYWHERE in the first 200 bytes and
    discards everything before it. With echo off there is no prompt at the
    head, so a file whose own bytes contain the prompt string loses
    everything up to it.

    The existing #206 tests pass today only because their payloads do not
    contain ``DAQIFI>``; this is the uncovered case.
    """
    payload = b"row,1\nnote: the console prints DAQIFI> as a prompt\nrow,2\n"
    assert b"DAQIFI>" in payload[:200]
    device = _device(payload + END_OF_FILE_MARKER)
    assert device.sd_get_file_binary("log_a.bin", timeout=2.0) == payload


def test_a_text_payload_containing_the_prompt_keeps_every_occurrence() -> None:
    """The text sibling sweeps with ``.replace()``, which removes EVERY
    occurrence at any offset rather than one prefix."""
    payload = "row one\nDAQIFI> appears twice: DAQIFI> here\nand real data\n"
    device = _device(payload.encode("ascii") + END_OF_FILE_MARKER)
    assert device.sd_get_file("log_a.csv", timeout=2.0) == payload


def test_a_payload_opening_with_the_prompt_loses_it_and_that_is_documented() -> None:
    """⚠ THE RESIDUAL, pinned so it cannot be discovered later as a
    surprise. With echo off there is no framing, so a payload whose own
    first bytes are exactly ``DAQIFI> `` is indistinguishable from a prompt
    and is consumed.

    An earlier draft of this file asserted the opposite and demanded
    something no prefix rule can deliver. What the fix does buy is the
    bound: only an occurrence at offset 0 is at risk, where the search this
    replaces lost everything before a ``DAQIFI>`` anywhere in the first 200
    bytes -- including, as the test above shows, one in the middle of a row.
    """
    payload = b"DAQIFI> this file opens with the prompt text\n"
    device = _device(payload + END_OF_FILE_MARKER)
    assert device.sd_get_file_binary("log_a.bin", timeout=2.0) == (
        b"this file opens with the prompt text\n"
    )


def test_a_text_payload_containing_the_command_keeps_it() -> None:
    """``.replace(cmd, "")`` removes the command text from the payload
    itself, not just from an echo at the head."""
    cmd = scpi_commands.get_sd_file("log_a.csv").data
    payload = f"this file records how it was fetched: {cmd}\n"
    device = _device(payload.encode("ascii") + END_OF_FILE_MARKER)
    assert device.sd_get_file("log_a.csv", timeout=2.0) == payload


def test_a_text_payload_keeps_its_own_leading_and_trailing_whitespace() -> None:
    """The text path finishes with ``.strip()``, which is not part of any
    transfer contract -- it eats the file's own first and last bytes."""
    payload = "   indented first line\nlast line\n\n"
    device = _device(payload.encode("ascii") + END_OF_FILE_MARKER)
    assert device.sd_get_file("log_a.csv", timeout=2.0) == payload


# -- instance 2: the EOF marker is found by SEARCH, so content truncates ----


def test_a_binary_payload_containing_the_eof_marker_truncates_there() -> None:
    """⚠ WITHDRAWN BY RULING FO A2, and this test is inverted rather than
    deleted so the limitation is pinned.

    The listed size used to let the receiver read past an
    ``__END_OF_FILE__`` occurring inside file content. That machinery
    produced every HIGH from r2 to r4 and delivered a capability neither
    #154 nor #206 asked for, so it is gone: the first terminal marker ends
    the file, which is exactly what the base did.
    """
    payload = b"head" + END_OF_FILE_MARKER + b"tail"
    device = _device(
        payload + END_OF_FILE_MARKER,
        sizes={"log_a.bin": len(payload)},   # present, and not consulted
    )
    assert device.sd_get_file_binary("log_a.bin", timeout=2.0) == b"head"



def test_a_transfer_error_times_out_rather_than_raising() -> None:
    """⚠ WITHDRAWN BY RULING FO A2 on this path only. ``__TRANSFER_ERROR__``
    detection is gone from the legacy download because a file may
    legitimately contain those bytes, and the overlap at a join -- content
    ending ``...__TRANSFER_ERROR`` plus the real ``__END_OF_FILE__`` --
    completes the marker by accident and raised on a VALID transfer (r4 #2).

    Fixing that in the shared scan would have reached the two
    already-correct callers, so the legacy path simply stopped asking. A
    failed transfer times out, as it did before this PR. The reason becomes
    available in #253, from the error queue.
    """
    device = _device(b"partial" + TRANSFER_ERROR_MARKER)
    with pytest.raises(TimeoutError):
        device.sd_get_file_binary("log_a.bin", timeout=0.4)



def test_the_text_path_also_times_out_on_a_transfer_error() -> None:
    """The text sibling, same withdrawal."""
    device = _device(b"partial" + TRANSFER_ERROR_MARKER)
    with pytest.raises(TimeoutError):
        device.sd_get_file("log_a.csv", timeout=0.4)



def test_the_echoed_command_and_prompt_are_still_stripped() -> None:
    """The head that really is a head still goes. Mirrors the framing in
    test_carried_items_206's prompt test and the legacy test in
    test_sd_transfer_error_154."""
    device = _device(_echo_of("log_a.bin") + b"\r\n" + PROMPT + b"payload"
                     + END_OF_FILE_MARKER)
    assert device.sd_get_file_binary("log_a.bin", timeout=2.0) == b"payload"


def test_a_leading_cr_lf_before_the_prompt_is_tolerated() -> None:
    """Pinned by test_carried_items_206.py::test_a_prompt_after_cr_lf_is_stripped
    -- the prefix test has to tolerate leading line endings, not just the
    echo."""
    device = _device(b"\r\n" + PROMPT + b"payload" + END_OF_FILE_MARKER)
    assert device.sd_get_file_binary("log_a.bin", timeout=2.0) == b"payload"


def test_an_echoed_command_naming_the_error_marker_is_not_a_transfer_error() -> None:
    """⛔ THE ORDER-CRITICAL CASE. The echo of a GET for
    ``__TRANSFER_ERROR__.bin`` carries the error marker's bytes. If the head
    reaches marker classification, the device's own echo reads as a device
    read error: a transfer that worked is reported as a hardware failure.
    """
    name = "__TRANSFER_ERROR__.bin"
    device = _device(_echo_of(name) + b"\r\n" + PROMPT + b"abc"
                     + END_OF_FILE_MARKER)
    assert device.sd_get_file_binary(name, timeout=2.0) == b"abc"


def test_the_head_strip_never_reaches_past_the_head() -> None:
    """One prompt at the head is removed; a second one inside the payload
    is content. A search-based strip cannot tell these apart."""
    device = _device(PROMPT + b"a" + PROMPT + b"b" + END_OF_FILE_MARKER)
    assert device.sd_get_file_binary("log_a.bin", timeout=2.0) == b"a" + PROMPT + b"b"


# -- containment: the receiver's default path is unchanged -----------------


def test_the_receivers_default_path_does_not_strip_a_preamble() -> None:
    """⛔ CONTAINMENT, and a CHARACTERIZATION test: the preamble skip is
    opt-in, so the two already-correct callers see exactly what they see
    today. This pins the default's behaviour on a stream that starts with an
    echoed ``__TRANSFER_ERROR__.bin`` command -- asserting what the base
    does, not what it ought to do, so a change to the default breaks here
    rather than in one of those callers.
    """
    name = "__TRANSFER_ERROR__.bin"
    source = _Serial(_echo_of(name) + b"\r\n" + PROMPT + b"abc"
                     + END_OF_FILE_MARKER)
    out = io.BytesIO()
    receiver = SdCardFileReceiver(source)
    # On the base the echoed marker text classifies as the device's error.
    with pytest.raises(SdCardTransferErrorException):
        receiver.receive(out, name, timeout_s=2.0)


def test_the_preamble_skip_is_prefix_only_in_the_receiver() -> None:
    """The receiver's own view of the same rule: given the preamble, the
    head goes and nothing else is examined for it."""
    name = "log_a.bin"
    body = b"x" + PROMPT + b"y"
    source = _Serial(_echo_of(name) + b"\r\n" + PROMPT + body
                     + END_OF_FILE_MARKER)
    out = io.BytesIO()
    receiver = SdCardFileReceiver(source)
    written = receiver.receive(
        out, name, timeout_s=2.0, preamble=_echo_of(name),
    )
    assert out.getvalue() == body
    assert written == len(body)


@pytest.mark.parametrize("split_at", [1, 4, 9, 17, 30])
def test_the_preamble_is_skipped_however_the_head_is_chunked(split_at) -> None:
    """The head arrives in whatever pieces the transport chooses, so the
    decision cannot depend on one chunk holding all of it."""
    name = "log_a.bin"
    head = _echo_of(name) + b"\r\n" + PROMPT
    stream = head + b"payload" + END_OF_FILE_MARKER
    source = _Serial(stream[:split_at], stream[split_at:])
    out = io.BytesIO()
    SdCardFileReceiver(source).receive(
        out, name, timeout_s=2.0, preamble=_echo_of(name),
    )
    assert out.getvalue() == b"payload"
