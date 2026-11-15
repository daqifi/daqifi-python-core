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

from _sd_echo_probe import EchoProbeFake
from daqifi._internal import scpi_commands
import daqifi.device as device_module
import daqifi.sdcard as sdcard_module
from daqifi.device import NyquistDevice
from daqifi.exceptions import SdCardTransferErrorException
from daqifi.sdcard import (
    END_OF_FILE_MARKER,
    TRANSFER_ERROR_MARKER,
    SdCardFileReceiver,
)

PROMPT = b"DAQIFI> "


class _Serial(EchoProbeFake):
    """Serial-shaped: ``in_waiting`` and never EOF, as pyserial behaves."""
    out_waiting = 0  # #263: the send path drains this queue, bounded

    def __init__(self, payload: bytes) -> None:
        self.is_open = True
        self.written: list = []
        self._buf = bytearray(payload)

    def write(self, data) -> int:
        self.written.append(bytes(data))
        self._probe_write(data)
        return len(data)

    @property
    def in_waiting(self) -> int:
        return self._probe_pending(lambda: len(self._buf))

    def read(self, size: int = 1) -> bytes:
        def _own():
            out = bytes(self._buf[:size])
            del self._buf[:size]
            return out
        return self._probe_read(size, _own)

    def reset_input_buffer(self) -> None:
        self._probe_reset()

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

    ⛔⛔ r15 -- REWRITTEN BECAUSE THE ORIGINAL WAS FLAKY IN CI, and the flake
    was NOT in the property under test. The old form drove 8 MiB against
    ``timeout=0.001`` and depended on the runner being fast enough. Two things
    were wrong with that:

      * ``_read_sd_response`` computes ``deadline = time.monotonic() +
        timeout`` and then tests ``while time.monotonic() < deadline``. With a
        1 ms allowance, ANY scheduling delay between those two statements
        means ZERO iterations and an EMPTY capture -- no partial read, no
        bytes at all. MEASURED: 20 of 400 trials returned 0 bytes under CPU
        contention (60 spinners on 20 cores), with no clock patching. CI hit
        it in 2 of 8 runs on this branch.
      * The failure was therefore NOTHING to do with 8 MiB. The size is
        load-bearing for the ORIGINAL r5 defect, not for the flake, and
        "2 MiB does not reproduce" described the defect rather than the
        runner.

    ⭐ THE FIX IS TO STOP MEASURING PARSE TIME WITH THE WALL CLOCK. The
    property is that the caller's timeout must not bound the LOCAL PASS, and
    that only needs the timeout to be small RELATIVE TO PARSE TIME -- not
    small in real time. So the allowance is a generous 5 s of real time, which
    the capture of a small payload cannot miss, while the parse is made to
    consume 60 s of VIRTUAL time. The defect's deadline (5 s) is exceeded; the
    fix's liveness bound (3600 s) is not.

    Only ``daqifi.sdcard``'s clock is virtualised. The capture in
    ``daqifi.device`` keeps the real one, so this test cannot reproduce the
    flake it replaces.
    """
    payload = b"x" * (256 * 1024)
    device = _device(payload + END_OF_FILE_MARKER)

    # ⭐ A CLOCK THAT ADVANCES WHEN IT IS READ. Nothing sleeps and nothing
    # depends on how fast this machine is: each consultation jumps VIRTUAL time
    # by 10 s, and the number of consultations is a function of payload size
    # over buffer size, not of wall time.
    #
    # ⛔ THE PATCH IS GLOBAL, NOT PER-MODULE, and sizing it wrongly reproduces
    # the very flake this replaces. `daqifi.sdcard.time`, `daqifi.device.time`
    # and the stdlib `time` are ONE module object (asserted below), so this
    # clock governs the CAPTURE as well as the parse. MEASURED call counts at
    # 256 KiB: capture 2, parse 19. Hence:
    #
    #   capture  deadline = t+10+ALLOWANCE, first check at t+20
    #            -> needs ALLOWANCE > 10 s         (60 gives 6x)
    #   parse, with the FIX     19 x 10 = 190 s < 3600 s liveness bound (19x)
    #   parse, with the DEFECT  190 s > ALLOWANCE -> raises             (3x)
    #
    # Every margin is a multiple, and all three are asserted after the run
    # rather than trusted.
    assert sdcard_module.time is device_module.time, (
        "this test's clock patch assumes ONE shared time module; if these ever "
        "differ the margins above are measuring the wrong phase")
    VIRTUAL_STEP_S = 10.0
    ALLOWANCE_S = 60.0
    LIVENESS_BOUND_S = 3600.0
    clock = {"t": 10_000.0, "calls": 0}
    real_monotonic = sdcard_module.time.monotonic

    def _advancing_clock() -> float:
        clock["calls"] += 1
        clock["t"] += VIRTUAL_STEP_S
        return clock["t"]

    sdcard_module.time.monotonic = _advancing_clock
    try:
        got = device.sd_get_file("log.csv", timeout=ALLOWANCE_S)
    finally:
        sdcard_module.time.monotonic = real_monotonic

    assert len(got) == 256 * 1024, (
        "a transfer that completed must not be failed by the local parse; "
        "got %d of %d bytes" % (len(got), 256 * 1024))

    # ⛔ SOUNDNESS CONDITIONS, both directions. This leg proves nothing unless
    # the parse outran the caller's allowance in virtual time; and it is a
    # FALSE pass if it also outran the liveness bound, because then the fix
    # would have raised too and the bytes came from somewhere else.
    elapsed = clock["t"] - 10_000.0
    assert elapsed > ALLOWANCE_S, (
        "the run consumed only %.1f s of virtual time against a %.1f s "
        "allowance, so this leg would pass WITH the defect present (clock "
        "read %d times -- did the buffer size change?)"
        % (elapsed, ALLOWANCE_S, clock["calls"]))
    assert elapsed < LIVENESS_BOUND_S, (
        "the run consumed %.1f s of virtual time, at or past the %.1f s "
        "liveness bound the fix uses -- the margin this leg rests on is gone"
        % (elapsed, LIVENESS_BOUND_S))


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
