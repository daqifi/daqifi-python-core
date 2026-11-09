"""The base implementation is the oracle for console framing (#154, #206).

⛔ WHY AN ORACLE AND NOT MORE CASES. r5's framing fix, r6's fix for that fix,
and r7's fix for that one were each written against the shapes the previous
audit named, and each introduced a new divergence from the base. The acceptance
criterion for r7 is therefore not "my cases pass" but "the branch agrees with
the base on every shape the base handled, and introduces none".

The base is ff4d699 (the merge-base), whose ``sd_get_file`` predates
``_PreambleSkip`` entirely:

    response_str = response_str.replace(cmd, "")
    response_str = response_str.replace("DAQIFI>", "")
    response_str = response_str.replace("__END_OF_FILE__", "")
    response_str = response_str.strip()

⚠ THE BASE IS NOT A VALID ORACLE EVERYWHERE, and that is measured rather than
assumed. It deletes every occurrence of those three tokens at ANY offset, so
for a payload containing framing-like bytes it CORRUPTS the content -- which is
the defect this PR exists to remove. So it is the oracle for payloads that
contain no framing-like bytes, and a deliberate counter-example for payloads
that do.

⚠ Two divergences from the base are intended and were accepted in r1/r2:

  * the base ``.strip()``ed the result, eating the file's own leading and
    trailing whitespace. Removed, so the clean shapes differ by exactly that.
  * the base deleted the command text and the EOF marker at every offset.
    Removed.

⚠ ONE CASE IS DELIBERATELY NOT ASSERTED HERE: a payload whose own bytes are
framing-like (``b'DAQIFI> DAQIFI> payload'``). Measured, the base returns
``'payload'`` -- it deleted both prompts -- and this branch returns
``'DAQIFI> payload'``. Which of those is correct is the subject of ruling GS,
unresolved when this was written, so this file asserts nothing about it rather
than encoding a criterion nobody has agreed. The structural invariant that
matters either way -- prompt-led framing must contain an echo -- IS asserted,
below.

BASE OUTPUTS BELOW ARE MEASURED, not predicted: each was obtained by running
ff4d699's own ``sd_get_file`` against the same payload through the same fake
serial. Re-measure before changing one.
"""

import pytest

from daqifi._internal import scpi_commands
from daqifi.device import NyquistDevice
from daqifi.sdcard import END_OF_FILE_MARKER, _PreambleSkip

PROMPT = b"DAQIFI> "
BODY = b"1,2\r\n"


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


def _echo() -> bytes:
    return scpi_commands.get_sd_file("log.csv").data.encode("ascii")


def _get(payload: bytes) -> str:
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(payload)
    return device.sd_get_file("log.csv", timeout=1.0)


# (name, payload, base output as MEASURED at ff4d699)
#
# Every one of these carries a payload with no framing-like bytes, so the base
# handled it correctly and its output is the standard. The branch must agree
# modulo the accepted `.strip()` removal, which is why the expectation is
# `base + the payload's own trailing CRLF`.
CLEAN_SHAPES = [
    ("no framing", BODY, "1,2"),
    ("bare prompt", PROMPT + BODY, "1,2"),
    ("echo + ending + prompt", _echo() + b"\r\n" + PROMPT + BODY, "1,2"),
    ("prompt + echo + ending + prompt",
     PROMPT + _echo() + b"\r\n" + PROMPT + BODY, "1,2"),
    # r6 #1: a CRLF arriving before the pending prompt. The base handled it;
    # r6 did not, and stopped filtering after the leading prompt alone.
    ("CRLF + prompt + echo + ending + prompt",
     b"\r\n" + PROMPT + _echo() + b"\r\n" + PROMPT + BODY, "1,2"),
]


@pytest.mark.parametrize("name,payload,base_output",
                         CLEAN_SHAPES,
                         ids=[s[0] for s in CLEAN_SHAPES])
def test_clean_payload_matches_the_base(name, payload, base_output) -> None:
    """The branch agrees with the base, modulo the accepted `.strip()`."""
    got = _get(payload + END_OF_FILE_MARKER)
    assert got.strip() == base_output, (
        "%s: the base returned %r and this returned %r. Only the `.strip()` "
        "removal may differ." % (name, base_output, got))
    # And the whitespace the base ate is present, which is the r1 change.
    assert got == base_output + "\r\n"


def test_an_empty_payload_is_empty() -> None:
    """Measured at ff4d699: ''. Nothing to strip, nothing to frame."""
    assert _get(END_OF_FILE_MARKER) == ""


def test_the_two_prompt_candidate_is_not_framing() -> None:
    """⛔ r6's regression, pinned structurally rather than by example.

    r5 allowed a prompt to lead the framing, and r6 let that lead pair with an
    EMPTY body -- building the candidate ``b'DAQIFI> DAQIFI> '``, which
    longest-match then stripped off a file whose own first bytes are a prompt.

    The invariant is structural: **prompt-led framing must contain a command
    echo.** A prompt followed by a prompt contains no command, so it cannot be
    the framing around one. Asserting the candidate's absence is stronger than
    any number of payload examples, because it holds for inputs nobody
    enumerated.
    """
    skip = _PreambleSkip(_echo())
    two_prompts = [c for c in skip._candidates
                   if c.count(b"DAQIFI>") > 1 and _echo() not in c]
    assert two_prompts == [], (
        "these candidates contain two prompts and no echo, so they are not "
        "framing: %r" % (two_prompts[:3],)
    )


def test_head_span_over_every_shape() -> None:
    """One bounded head span per shape, and `None` only while undecidable."""
    skip = _PreambleSkip(_echo())
    assert skip.head_span(b"1,2\r\n") == 0            # echo off
    assert skip.head_span(b"") is None                # still arriving
    assert skip.head_span(PROMPT + b"1,2") == len(PROMPT)
    full = PROMPT + _echo() + b"\r\n" + PROMPT
    assert skip.head_span(full + b"1,2") == len(full)
    crlf = b"\r\n" + full
    assert skip.head_span(crlf + b"1,2") == len(crlf)
