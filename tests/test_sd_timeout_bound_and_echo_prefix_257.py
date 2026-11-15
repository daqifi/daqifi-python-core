"""Ruling HW A: the non-finite timeout is refused, and the echo-prefix stall is
pinned AS IT BEHAVES TODAY (#154, #206, #257 r13).

⛔ THE CASE BEING PINNED, and why it is ACCEPTED rather than fixed.
`_PreambleSkip.head_span` answers None while the buffer is a proper PREFIX of a
candidate, and `_read_sd_response` gates the marker scan on the head being
resolved. So a payload that is itself a prefix of the echoed command keeps the
head unresolved and the marker it has already received is never looked for.

Reaching it needs TWO coincidences, not one: the content must be exactly the
echo's prefix AND the filename must itself begin with `__END_OF_FILE__`, so
that the marker's bytes CONTINUE the echo rather than breaking the prefix. With
an ordinary filename the first marker byte diverges and there is no stall at
all -- `test_an_ordinary_filename_does_not_stall` is that control.

⛔ WHY NOT FIXED: THE TWO STATES ARE BYTE-IDENTICAL. Both of these present
`b'SYSTem:STORage:SD:GET "__END_OF_FILE__'` to `head_span`:

    (a) a COMPLETE payload whose content is the echo's 23-byte prefix
    (b) a PARTIAL ECHO for that same file, with the rest still in flight

(a) must stop and (b) must keep waiting, so no rule reading only the buffer can
separate them. Letting the marker end the capture on an unresolved head -- the
obvious fix -- was measured: it ends the stall and makes (b) return the 23-byte
ECHO FRAGMENT in place of the real file. That is the silent-short-file
direction, and `test_a_real_transfer_is_not_truncated_to_the_echo_fragment`
below is red for it.

⭐ THE COST TODAY IS LATENCY, NOT CORRECTNESS: the stalled call waits out the
caller's timeout and then returns the CORRECT bytes. The real discriminator is
out of band -- the device's ECHO STATE, which this module never reads -- and
that is python-core #260's job, not this PR's.
"""
import time

import pytest

from daqifi.device import NyquistDevice
from daqifi.sdcard import END_OF_FILE_MARKER

ECHO_PREFIX = b'SYSTem:STORage:SD:GET "'
MARKER_NAME = "__END_OF_FILE__"
FULL_ECHO = ('SYSTem:STORage:SD:GET "%s"' % MARKER_NAME).encode("ascii")


class _ScriptedSerial:
    """Hands out queued chunks; a ``None`` entry is one SILENT poll.

    A silent poll is how "the rest of the framing is still in flight" is
    expressed without sleeping: `in_waiting` reports 0 once, the loop takes its
    idle branch, and the next chunk arrives on a later pass.
    """
    out_waiting = 0  # #263: the send path drains this queue, bounded

    def __init__(self, chunks):
        self._q = list(chunks)
        self.is_open = True
        self.timeout = 0.1
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        if self._q and self._q[0] is None:
            self._q.pop(0)
            return 0
        return len(self._q[0]) if self._q else 0

    def read(self, n: int = 1) -> bytes:
        return self._q.pop(0) if self._q else b""

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False

    @property
    def unread(self) -> int:
        return sum(len(c) for c in self._q if c)


def _device(chunks):
    dev = NyquistDevice("COM41")
    ser = _ScriptedSerial(chunks)
    dev._comm._serial = ser
    return dev, ser


# --------------------------------------------------------------------------
# Ruling HW A, part 1: the non-finite timeout is a USAGE error at BOTH entries
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["sd_get_file", "sd_get_file_binary"])
@pytest.mark.parametrize(
    "bad", [float("inf"), float("-inf"), float("nan"), "soon", None])
def test_a_non_finite_timeout_is_refused_at_both_entries(entry, bad):
    """⛔ BOTH entries, because both reach the same unbounded wait.

    `inf` cannot terminate the loop at all. `nan` is worse than a hang: every
    comparison against it is False, so the loop never runs and the call returns
    EMPTY -- a silent wrong answer. Both are refused by name rather than
    clamped, because substituting a deadline the caller did not ask for is
    invisible to them.
    """
    dev, ser = _device([ECHO_PREFIX + END_OF_FILE_MARKER])
    with pytest.raises(ValueError) as exc:
        getattr(dev, entry)("log.csv", timeout=bad)
    assert "timeout" in str(exc.value)
    assert entry in str(exc.value)


@pytest.mark.parametrize("entry", ["sd_get_file", "sd_get_file_binary"])
def test_the_refusal_happens_before_the_get_is_written(entry):
    """⛔ THE POSITION OF THE CHECK IS PART OF THE FIX.

    Both entries write the GET and only then read. A guard placed after the
    write would refuse the call having ALREADY set the device streaming a file
    nobody will read -- the refusal would leave the bench worse off than no
    check at all. So the assertion is on the WIRE: nothing was sent.
    """
    dev, ser = _device([ECHO_PREFIX + END_OF_FILE_MARKER])
    with pytest.raises(ValueError):
        getattr(dev, entry)("log.csv", timeout=float("inf"))
    assert bytes(ser.written) == b"", (
        "the GET was written before the timeout was validated"
    )


@pytest.mark.parametrize("entry", ["sd_get_file", "sd_get_file_binary"])
def test_the_unbounded_case_itself_is_refused(entry):
    """⛔⛔ THE CASE THE RULING IS ACTUALLY ABOUT, not just a type check.

    The parametrised tests above use an ordinary filename, which does NOT
    stall -- so without the guard they fail on "did not raise" and return
    promptly. This one combines `inf` with the STALLING shape (content = the
    echo's prefix, file named `__END_OF_FILE__`), which is the only
    configuration where the loop genuinely cannot terminate.

    ⚠ WITH the guard this returns instantly, which is what makes it safe to
    run in CI. WITHOUT it, this node HANGS FOREVER -- confirmed under an
    external bound while measuring the red side, and the reason the guard is a
    refusal rather than a clamp. Do not "simplify" this to drop the filename.
    """
    dev, ser = _device([ECHO_PREFIX + END_OF_FILE_MARKER])
    with pytest.raises(ValueError):
        getattr(dev, entry)(MARKER_NAME, timeout=float("inf"))


@pytest.mark.parametrize("entry", ["sd_get_file", "sd_get_file_binary"])
def test_a_finite_timeout_is_still_accepted(entry):
    """⭐ POSITIVE CONTROL. Without it the guard could reject everything and
    every test above would still pass."""
    dev, ser = _device([ECHO_PREFIX + END_OF_FILE_MARKER])
    got = getattr(dev, entry)("log.csv", timeout=1.0)
    assert got in (ECHO_PREFIX, ECHO_PREFIX.decode("ascii"))
    assert bytes(ser.written).startswith(b"SYSTem:STORage:SD:GET")


# --------------------------------------------------------------------------
# Ruling HW A, part 2: the stall is pinned AS IT IS, and the fragment is not
# --------------------------------------------------------------------------

def test_an_ordinary_filename_does_not_stall():
    """⭐ THE CONTROL THAT BOUNDS THE CASE. The audit's repro was reported as
    "content equal to the echo prefix", which is NOT sufficient: with an
    ordinary filename the marker's first byte breaks the prefix, `head_span`
    answers 0, and the transfer returns at once. Measured here so the
    documented case cannot quietly widen.
    """
    dev, ser = _device([ECHO_PREFIX + END_OF_FILE_MARKER])
    t0 = time.monotonic()
    got = dev.sd_get_file_binary("log.csv", timeout=1.0)
    assert got == ECHO_PREFIX
    assert time.monotonic() - t0 < 0.5, "an ordinary filename must not stall"


def test_the_echo_prefix_case_returns_the_correct_bytes_after_a_stall():
    """⛔ THE ACCEPTED CASE, PINNED ON ITS OUTCOME (ruling HW A).

    Content is the echo's 23-byte prefix and the file is named
    `__END_OF_FILE__`, so `content + marker` stays a proper prefix of the
    candidate and the head never resolves. The call therefore waits out the
    caller's timeout -- that is the accepted cost -- and then returns the
    CORRECT content.

    ⭐ THE ASSERTION IS ON THE BYTES, NOT ON THE DELAY. A future change that
    ends the stall while still returning these bytes is an IMPROVEMENT and must
    not turn this red; one that returns a fragment, or nothing, is the
    regression this exists to catch. The elapsed time is asserted only to be
    BOUNDED, which is what the timeout guard above guarantees.
    """
    dev, ser = _device([ECHO_PREFIX + END_OF_FILE_MARKER])
    t0 = time.monotonic()
    got = dev.sd_get_file_binary(MARKER_NAME, timeout=0.5)
    elapsed = time.monotonic() - t0
    assert got == ECHO_PREFIX, (
        "the accepted case must still return the file's real content; got %r"
        % (got,)
    )
    assert elapsed < 30.0, "the wait must be bounded by the caller's timeout"


def test_a_real_transfer_is_not_truncated_to_the_echo_fragment():
    """⛔⛔ THE REGRESSION GUARD, and the reason the stall was not "fixed".

    Same first 38 bytes as the test above -- byte-identical, which is the whole
    problem -- but here they are a PARTIAL ECHO and the real payload follows.
    This must return the payload.

    Measured against the obvious fix (end the capture on a marker while the
    head is unresolved): it returns `b'SYSTem:STORage:SD:GET "'`, the echo
    fragment, in place of the file. A silent short file is the direction ruling
    GV A rejected in favour of visible, recoverable framing leaks, so any
    change that trades the stall for this turns this test red.
    """
    dev, ser = _device([
        FULL_ECHO[:38],                     # == ECHO_PREFIX + END_OF_FILE_MARKER
        None,                               # a silent poll: the rest is in flight
        FULL_ECHO[38:] + b"\r\nDAQIFI> ",   # the echo completes, prompt follows
        b"REAL,DATA\r\n" + END_OF_FILE_MARKER,
    ])
    got = dev.sd_get_file_binary(MARKER_NAME, timeout=1.0)
    assert got == b"REAL,DATA\r\n", (
        "a real transfer was truncated to the echo fragment; got %r" % (got,)
    )


def test_the_two_states_really_are_byte_identical():
    """⭐ THE SOUNDNESS CONDITION FOR THE PAIR ABOVE.

    The two tests only constrain each other if the bytes `head_span` sees are
    the same in both. Asserted rather than assumed: if they ever diverge, the
    pair silently stops being a dilemma and the "cannot be fixed here"
    reasoning in this module's docstring no longer follows.
    """
    assert ECHO_PREFIX + END_OF_FILE_MARKER == FULL_ECHO[:38]
