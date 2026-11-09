"""The four round-5 defects on #257, each with the measurement that found it.

Ruling FZ A authorised this round. Every leg here was run against the frozen
head ``6f4d8c41`` as well as the fix, because a leg that passes on the broken
head proves nothing:

  r5 #1  device.py:875   a text download discards the payload after an
                         embedded EOF marker -- SAME on both heads, by
                         design: see the first test
  r5 #2  device.py:1009  a second absolute timeout over local parsing
                         rejected an already-completed transfer
  r5 #3  sdcard.py:179   a prompt pending before the GET echo stopped the
                         prefix filter after the prompt alone
  r5 #4  sdcard.py:1157  ``eof_only`` was ignored once ``expected_size`` was
                         supplied
"""

import io

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
    """Serial-shaped: ``in_waiting`` and never EOF, as pyserial behaves."""

    def __init__(self, payload: bytes) -> None:
        self.is_open = True
        self.written: list = []
        self._buf = bytearray(payload)

    def write(self, data) -> int:
        self.written.append(bytes(data))
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass


def _device(payload: bytes) -> NyquistDevice:
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(payload)
    return device


def _echo(name: str) -> bytes:
    return scpi_commands.get_sd_file(name).data.encode("ascii")


def test_text_truncation_at_an_embedded_marker_diverges_from_the_base() -> None:
    """⛔ PINNED AS A DIVERGENCE, NOT AS BASE PARITY -- which is what r5
    actually disproved.

    Measured on both heads with
    ``b'head__END_OF_FILE__tail__END_OF_FILE__'``:

        base        -> 'headtail'   (``.replace()`` deleted the marker's
                                     bytes at EVERY offset and kept the rest)
        this branch -> 'head'       (the first terminal marker ends the file)

    Both are lossy and neither is recoverable without a length: with no size
    there is nothing that tells a marker ending the file from the same bytes
    inside it. Ruling FO A2 chose truncation, and reading past the marker is
    the mechanism that ruling removed -- so it is deliberately NOT re-added
    to close this finding. The defect r5 found was the docstring's claim that
    truncation "preserves the base's behaviour for both legacy paths"; the
    claim is withdrawn and this test is where the real behaviour is recorded.

    ⚠ The binary sibling genuinely did match its base here -- that path was
    marker-terminated before this PR too. Only the TEXT path diverges, which
    is why the retired claim was wrong only in its "both paths" half.
    """
    body = b"head" + END_OF_FILE_MARKER + b"tail"
    device = _device(body + END_OF_FILE_MARKER)
    assert device.sd_get_file("log.csv", timeout=2.0) == "head"


def test_a_completed_text_transfer_is_not_timed_out_by_local_parsing() -> None:
    """⛔ THE CALLER'S TIMEOUT IS A TRANSPORT ALLOWANCE, NOT A PARSE BUDGET.

    The legacy text path captures the whole response first, then re-reads it
    from an in-memory buffer that cannot block. Passing the caller's timeout
    into that second pass made it a SECOND absolute deadline, so a transfer
    that had already completely succeeded raised ``TimeoutError`` whenever
    the local parse outlasted the transport allowance.

    ⚠ The 8 MiB is LOAD-BEARING and is not padding: a 2 MiB probe parses fast
    enough to finish inside the allowance and does NOT reproduce. Measured on
    the frozen head, this raises ``TimeoutError``; the base returned all
    8,388,608 characters, and so does the fix.
    """
    payload = b"x" * (8 * 1024 * 1024)
    device = _device(payload + END_OF_FILE_MARKER)
    got = device.sd_get_file("log.csv", timeout=0.001)
    assert len(got) == 8 * 1024 * 1024


def test_a_prompt_pending_before_the_echo_is_still_framing() -> None:
    """⛔ THE CONSOLE PROMPTS WHEN IT FINISHES THE *PREVIOUS* COMMAND.

    So a GET issued into an idle console arrives as prompt + echoed command +
    ending + prompt. With only an ENDING allowed in front of the echo, the
    longest matching candidate was the bare leading prompt, the filter
    latched ``done`` on it, and the echoed command plus the SECOND prompt
    were delivered as the first line of the CSV. The base returned ``1,2``.
    """
    echo = _echo("log.csv")
    raw = PROMPT + echo + b"\r\n" + PROMPT + b"1,2\r\n" + END_OF_FILE_MARKER
    destination = io.BytesIO()
    SdCardFileReceiver(io.BytesIO(raw)).receive(
        destination, "log.csv", timeout_s=1.0, preamble=echo, eof_only=True,
    )
    assert destination.getvalue() == b"1,2\r\n"


def test_eof_only_is_honoured_when_a_size_is_supplied() -> None:
    """⛔ ``eof_only`` PROMISES ``__TRANSFER_ERROR__`` IS ORDINARY CONTENT.

    It was honoured only on the unsized scan, so a sized receive still
    classified those bytes and raised ``SdCardTransferErrorException`` --
    reporting a device read error for a file whose literal content is the
    marker. The honest outcome for a transport that closes 21 bytes into a
    100-byte transfer is an incomplete-transfer timeout.
    """
    source = io.BytesIO(b"abc" + TRANSFER_ERROR_MARKER)
    try:
        SdCardFileReceiver(source).receive(
            io.BytesIO(), "log.bin", timeout_s=1.0,
            expected_size=100, eof_only=True,
        )
    except SdCardTransferErrorException as exc:  # pragma: no cover
        raise AssertionError(
            "eof_only must make __TRANSFER_ERROR__ ordinary content on the "
            "sized path too; got %r" % (exc,)
        )
    except TimeoutError:
        pass
    else:  # pragma: no cover
        raise AssertionError(
            "a transport closing 21 bytes into a 100-byte transfer must not "
            "read as a complete transfer"
        )


def test_the_error_classification_still_works_without_eof_only() -> None:
    """The opt-out must not become the default -- the two callers that never
    asked for it still get a device error on a real ``__TRANSFER_ERROR__``.

    Without this, the fix above could have been "stop classifying error
    markers at all", which would pass the leg before it while removing the
    behaviour two other callers depend on.
    """
    source = io.BytesIO(b"abc" + TRANSFER_ERROR_MARKER)
    try:
        SdCardFileReceiver(source).receive(
            io.BytesIO(), "log.bin", timeout_s=1.0, expected_size=100,
        )
    except SdCardTransferErrorException:
        pass
    else:  # pragma: no cover
        raise AssertionError(
            "a sized receive WITHOUT eof_only must still raise on the "
            "device's transfer-error marker"
        )
