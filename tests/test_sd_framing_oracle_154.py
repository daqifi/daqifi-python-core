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

⭐ RULING GS A (operator, 2026-11-09) SETTLES THE CASE THIS FILE USED TO HOLD
OPEN, and narrows the oracle in exactly these terms:

  * branch output == base output is required ONLY for payloads with NO
    framing-like bytes, allowing for the accepted ``.strip()`` divergence;
  * framing-like bytes INSIDE a payload must be PRESERVED, and a differential
    showing the base CORRUPTED them is pinned deliberately, as the intended
    divergence;
  * no framing shape the base did not handle.

So the held case -- real framing in front of a file whose OWN first bytes are
a prompt -- is now asserted in both directions: this branch preserves the
file's bytes, and the base destroys them. An earlier revision of this
docstring said the correct answer was "the subject of ruling GS, unresolved",
and declined to encode a criterion nobody had agreed; that is no longer true.

⛔ AUDIT FINDING r6 #2 IS WITHDRAWN under the same ruling: its premise was
that the base PRESERVED framing-like payload bytes, and the base's own source
deletes ``DAQIFI>`` at EVERY offset -- ff4d699 ``daqifi/device.py:795``,
read at that revision and quoted verbatim in ``_base_transform`` below, not
recalled. So the base cannot be the oracle for this case; it is the
counter-example.

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


def _base_transform(wire: bytes, cmd: str) -> str:
    """ff4d699's own ``sd_get_file`` tail, transcribed statement for statement.

    Read out of ``git show ff4d699:daqifi/device.py`` (its lines 788-803) at
    that revision, not recalled:

        response_str = response.decode('ascii', errors='ignore')
        if cmd in response_str:
            response_str = response_str.replace(cmd, "")
        response_str = response_str.replace("DAQIFI>", "")
        response_str = response_str.replace("__END_OF_FILE__", "")
        response_str = response_str.strip()

    ⛔ EVERY ONE OF THOSE REPLACES IS UNANCHORED, which is the entire finding:
    they hit the payload exactly as readily as the framing, and the prompt one
    (``:795``) is why audit finding r6 #2's premise was false.

    ⚠ A REIMPLEMENTATION USED AS AN ORACLE CONVICTS NOTHING UNTIL IT IS PROVEN
    FAITHFUL -- `test_the_transcribed_base_reproduces_every_measured_output`
    below replays it against every base output this file had already MEASURED
    against the real ff4d699 code. Without that control this function would be
    my belief about the base wearing the base's authority.
    """
    response_str = wire.decode("ascii", errors="ignore")
    if cmd in response_str:
        response_str = response_str.replace(cmd, "")
    response_str = response_str.replace("DAQIFI>", "")
    response_str = response_str.replace("__END_OF_FILE__", "")
    return response_str.strip()


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


def test_the_transcribed_base_reproduces_every_measured_output() -> None:
    """The control for `_base_transform`: faithful, or it convicts nothing.

    Every expectation in CLEAN_SHAPES was measured by running the REAL
    ff4d699 ``sd_get_file`` against that payload. Replaying the transcription
    against the same payloads is therefore a two-sided check on the
    transcription itself -- and it is what licenses using it, below, as the
    base side of a differential that deliberately DISAGREES with this branch.
    """
    assert END_OF_FILE_MARKER == b"__END_OF_FILE__", (
        "the base deleted the literal b'__END_OF_FILE__'; if this branch's "
        "marker is no longer that string, the transcription is not comparable")
    cmd = _echo().decode("ascii")
    for name, payload, base_output in CLEAN_SHAPES:
        got = _base_transform(payload + END_OF_FILE_MARKER, cmd)
        assert got == base_output, (
            "%s: the transcribed base returned %r where the real ff4d699 was "
            "measured to return %r -- the transcription is wrong, fix it "
            "before trusting any differential built on it"
            % (name, got, base_output))
    assert _base_transform(END_OF_FILE_MARKER, cmd) == ""


# Payloads whose OWN bytes look like framing. Each is the FILE's content; the
# wire adds real framing in front of it. Ruling GS A: these must be PRESERVED.
#
# ⚠ `__END_OF_FILE__` INSIDE a payload is deliberately absent from this list.
# The read loop stops at the first marker occurrence, so such a file truncates
# in transit -- identically on the base and on this branch. That is a device
# protocol limit, not something a host-side framing fix can reach, and GS A's
# third term ("no framing shape the base did not handle") is not an invitation
# to invent one.
FRAMING_LIKE = [
    # The held case: the file's own first bytes are a prompt.
    ("payload opens with a prompt", PROMPT, b"DAQIFI> payload\r\n"),
    # Mid-content, so no leading-offset reasoning can accidentally pass it.
    ("payload contains a prompt mid-content", PROMPT, b"1,2\r\nDAQIFI> 3,4\r\n"),
    # The OTHER unanchored replace, base `device.py:791-792`: the echoed
    # command text occurring inside the file.
    ("payload contains the command echo",
     PROMPT + _echo() + b"\r\n" + PROMPT, _echo() + b"\r\n1,2\r\n"),
]


@pytest.mark.parametrize("name,framing,body",
                         FRAMING_LIKE,
                         ids=[s[0] for s in FRAMING_LIKE])
def test_framing_like_payload_bytes_survive_and_the_base_destroyed_them(
        name, framing, body) -> None:
    """Ruling GS A, both directions, in one assertion pair.

    Kept as ONE test per shape on purpose: a preservation leg and a separate
    base-corruption leg can drift apart silently, and then the differential
    stops being about the same bytes. Here the file's content is a single
    value used on both sides.
    """
    wire = framing + body + END_OF_FILE_MARKER
    want = body.decode("ascii")

    got = _get(wire)
    assert got == want, (
        "%s: this branch must return the file's own bytes verbatim. "
        "want %r, got %r" % (name, want, got))

    base = _base_transform(wire, _echo().decode("ascii"))
    assert base != want, (
        "%s: this differential asserts the base CORRUPTED these bytes, and "
        "it did not -- it returned %r, which equals the file's content. The "
        "shape is then not a counter-example and does not belong in this "
        "list." % (name, base))


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
