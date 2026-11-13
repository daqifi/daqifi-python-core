"""#260: the SD download decides prompt stripping from ECHO STATE, not byte
shape -- and only on a CONFIRMED silent console.

⛔ THE FIRMWARE FACT THIS RESTS ON, and #260's own text got it wrong. The
issue suggested "with echo off, nothing in the stream is a prompt, so nothing
gets stripped". That is true only of -1. `microrl.c` gates the prompt with
``if (pThis->echoOn > -1)`` and ``echoOn`` is an **int**; ``SCPI_SetEcho``
accepts only -1..1, and EVERY gate in microrl is ``> -1`` -- no ``> 0``,
``== 1``, ``!= 0`` or ``>= 1`` anywhere. So:

    -1   prompt suppressed, echo suppressed   <- the only silent state
     0   prompt printed, echo printed          <- IDENTICAL to 1 on the wire
     1   prompt printed, echo printed

``SYSTem:ECHO 0`` does NOT turn echo off. Acting on the issue's wording would
have skipped the strip on `echo 0` and leaked framing into every file, so
there is a pin for exactly that below.

⭐ WHY A CONFIRMED -1 IS SOUND: one firmware variable decides both the prompt
and the echo, so a device cannot truthfully answer -1 while printing a prompt.

⛔ WHICH CASE-D SHAPES THIS CLOSES (ruling HJ B, up to 214 bytes, scaling with
the filename):
  * CLOSED on a confirmed -1 -- the library's own state after open(). Case-D
    content comes back INTACT, including the 214-byte long-name shape.
  * REMAINS where the caller deliberately set echo 0 or 1, or where the probe
    could not establish the state. There the bytes genuinely are ambiguous.
So the residual stops being silent-by-default and becomes "only in a state the
caller chose or we could not confirm".

⚠ WHICH PINS ARE DIFFERENTIALS AND WHICH ARE NOT, because they are not the
same kind of evidence:
  * RED at e3cfbbac: the case-D-intact pins. They claim NEW behaviour.
  * GREEN at e3cfbbac by construction: "echo 0 still strips" and friends. The
    base ALWAYS strips, so these cannot be differentials; they are regression
    guards against a WRONG implementation and their teeth are shown by
    MUTATION (treat 0 as silent -> they fire), not by a cross-head run.
"""
import time
from pathlib import Path

import pytest
import serial

from _sd_echo_probe import EchoProbeFake, echo_probe_reply
from daqifi.exceptions import EchoProbeUnansweredError
from daqifi.device import NyquistDevice
from daqifi.sdcard import END_OF_FILE_MARKER

PROMPT = b"DAQIFI> "
LONG_NAME = "a" * 176 + ".bin"


def _echo_of(name: str) -> bytes:
    return ('SYSTem:STORage:SD:GET "%s"' % name).encode("ascii")


class _Port(EchoProbeFake):
    """A request/response port: answers the probe, then the GET."""

    def __init__(self, payload: bytes, echo_state=1):
        self.is_open = True
        self.written = bytearray()
        self.echo_state = echo_state
        self._payload = bytes(payload)
        self._buf = bytearray()

    def write(self, data):
        self.written.extend(data)
        if self._probe_write(data):
            return len(data)
        self._buf.extend(self._payload)
        self._payload = b""
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return self._probe_pending(lambda: len(self._buf))

    def read(self, n=1):
        def _own():
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out
        return self._probe_read(n, _own)

    def reset_input_buffer(self):
        self._probe_reset()

    def reset_output_buffer(self):
        pass


def _get(payload, echo_state=1, name="log.csv", binary=True, timeout=2.0):
    dev = NyquistDevice("COM41")
    port = _Port(payload, echo_state)
    dev._comm._serial = port
    method = dev.sd_get_file_binary if binary else dev.sd_get_file
    return method(name, timeout=timeout), port


# ---------------------------------------------------------------------------
# RED at e3cfbbac: case-D content survives on a confirmed -1
# ---------------------------------------------------------------------------

def test_case_d_content_is_intact_when_the_device_confirms_minus_one():
    """⛔ THE CLOSURE #260 BUYS. This file's own content IS the framing shape,
    which e3cfbbac strips by byte shape because it cannot know better. With -1
    confirmed, no framing is possible, so every byte is file data.
    """
    content = PROMPT + _echo_of("log.csv") + b"\r\n" + PROMPT + b"1,2\r\n"
    assert len(content) == 54
    got, _ = _get(content + END_OF_FILE_MARKER, echo_state=-1)
    assert got == content, (
        "on a confirmed -1 the whole 54 bytes are file content; got %r" % (got,))


def test_the_long_name_case_d_shape_is_intact_too():
    """⛔ THE 214-BYTE SHAPE, which is what disproved any 49-byte bound on the
    accepted loss: the matched candidate contains the echo, so the loss scales
    with the FILENAME. All 220 bytes come back on a confirmed -1.
    """
    content = _echo_of(LONG_NAME) + b"\r\nDAQIFI> " + b"DATA\r\n"
    assert len(content) == 220
    assert content.index(PROMPT) == 206, "the prompt must sit past 200 bytes"
    got, _ = _get(content + END_OF_FILE_MARKER, echo_state=-1, name=LONG_NAME)
    assert got == content, (
        "on a confirmed -1 all 220 bytes are file content; got %d" % len(got))


# ---------------------------------------------------------------------------
# GREEN at the base: regression guards, teeth shown by MUTATION
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", [0, 1])
def test_a_framed_console_still_strips(state):
    """⛔ NOT A DIFFERENTIAL -- the base always strips, so this is green there.
    It guards against an implementation that reads the issue's wording and
    treats 0 as silent. Mutate `_echo_confirmed_silent` to accept 0 and this
    fires; that is where its teeth come from.

    ⚠ 0 IS PARAMETRISED ALONGSIDE 1 DELIBERATELY. They are indistinguishable
    on the wire, so a reader who believes 0 means "off" has to confront both
    rows at once.
    """
    content = PROMPT + _echo_of("log.csv") + b"\r\n" + PROMPT + b"1,2\r\n"
    got, _ = _get(content + END_OF_FILE_MARKER, echo_state=state)
    assert got == b"1,2\r\n", (
        "echo %d frames its replies exactly like 1, so the head is still "
        "stripped; got %r" % (state, got))


# ---------------------------------------------------------------------------
# The probe is BOUNDED and FALLS BACK
# ---------------------------------------------------------------------------

class _SilentToProbe(_Port):
    """A device that never answers the probe. The file is withheld until the
    GET, exactly as a real one withholds it."""

    def write(self, data):
        self.written.extend(data)
        if echo_probe_reply(data, 1) is not None:
            self._probe_armed = False      # silent, and nothing to read
            return len(data)
        self._probe_armed = True
        self._buf.extend(self._payload)
        self._payload = b""
        return len(data)


def test_an_unanswered_probe_refuses_the_download_and_does_not_hang():
    """⛔ r2 REVERSES r1 HERE, and the reversal is the fix.

    r1 fell back and downloaded. That is unsafe whenever the reply MIGHT STILL
    ARRIVE: nothing distinguishes "never answered" from "answered slowly", so
    the GET can go out ahead of bytes still on the wire. r2 refuses instead,
    loudly, and never sends the GET.

    ⚠ A CONTRACT CHANGE against the base, which never probed. Stated in the
    PR body: every firmware this ships against registers `SYSTem:ECHO?`, so a
    non-answer means the link is broken, and a named refusal beats a silently
    corrupted file.
    """
    content = PROMPT + b"payload\r\n"
    dev = NyquistDevice("COM41")
    port = _SilentToProbe(content + END_OF_FILE_MARKER)
    dev._comm._serial = port
    t0 = time.monotonic()
    with pytest.raises(EchoProbeUnansweredError) as err:
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    elapsed = time.monotonic() - t0
    assert "download not attempted" in str(err.value)
    bound = dev._comm.ECHO_PROBE_TIMEOUT_S
    assert elapsed < bound * 4, (
        "the refusal must cost about the bound (%.2fs), not hang; took %.2fs"
        % (bound, elapsed))
    assert b"SYSTem:STORage:SD:GET" not in bytes(port.written), (
        "the GET must NOT be written when the probe did not finish")


@pytest.mark.parametrize("garbage", [
    b"\x80\x81\r\n",          # non-ASCII, terminated
    b"-1 extra\r\n",          # -1 with anything after it, terminated
    b"\r\n",                  # a bare terminator
])
def test_a_reply_not_attributable_to_this_probe_refuses(garbage):
    """⛔⛔ r4 WITHDRAWS r2's FALLBACK HALF, and the reversal is the fix.

    r2 and r3 treated a TERMINATED buffer that was not our reply as
    "complete but unparseable" and fell back to the byte-shape download. r4
    finding 3 shows why that is unsafe: a PRECEDING command's delayed CRLF is
    exactly such a buffer, and it completed the probe before our own reply had
    arrived -- so the download ran with the reply still in flight and returned
    contaminated bytes. The audit's clearest evidence: disabling the probe
    entirely gave the CORRECT payload under the same schedule.

    ⭐ "COMPLETE GARBAGE" AND "FOREIGN BYTES, OUR REPLY STILL COMING" ARE THE
    SAME BYTES. Nothing can separate them, so every one of these now means
    keep waiting and then refuse.
    """
    class _Garbage(_Port):
        def write(self, data):
            self.written.extend(data)
            if echo_probe_reply(data, 1) is not None:
                self._probe_reply = garbage
                self._probe_armed = False
                return len(data)
            self._probe_armed = True
            self._buf.extend(self._payload)
            self._payload = b""
            return len(data)

    content = PROMPT + b"payload\r\n"
    dev = NyquistDevice("COM41")
    port = _Garbage(content + END_OF_FILE_MARKER)
    dev._comm._serial = port
    with pytest.raises(EchoProbeUnansweredError):
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    assert b"SYSTem:STORage:SD:GET" not in bytes(port.written), (
        "%r is not attributable to this probe, so no GET may be written"
        % (garbage,))


class _Scheduled(_Port):
    """A port whose probe reply arrives ON A SCHEDULE -- i.e. in flight -- and
    whose FILE also arrives over time.

    ⛔ THE PAYLOAD SCHEDULE IS NOT DECORATION, and leaving it out cost me 40
    worthless pins. With the whole file readable the instant the GET lands,
    the download finishes in microseconds and a probe remainder arriving at
    120 ms is simply too late to be read -- so the pin passed against the r1
    defect it was written to catch. A real download reads over many
    iterations; that is the window a late byte lands in, so the fake has to
    have one.
    """

    #: Gap between payload chunks once the GET has been answered.
    payload_gap = 0.03
    payload_chunk = 4

    def __init__(self, payload, schedule, echo_state=-1):
        super().__init__(payload, echo_state)
        self.echo_reply_schedule = schedule
        self._released_at = None

    def write(self, data):
        self.written.extend(data)
        if self._probe_write(data):
            return len(data)
        self._released_at = time.monotonic()
        return len(data)

    def _file_available(self):
        """How much of the file has 'arrived' by now."""
        if self._released_at is None:
            return 0
        elapsed = time.monotonic() - self._released_at
        return min(len(self._payload),
                   self.payload_chunk * (1 + int(elapsed / self.payload_gap)))

    @property
    def in_waiting(self):
        return self._probe_pending(
            lambda: self._file_available() - self._sent)

    def read(self, n=1):
        def _own():
            avail = self._file_available() - self._sent
            take = min(n, max(0, avail))
            out = self._payload[self._sent:self._sent + take]
            self._sent += take
            return out
        return self._probe_read(n, _own)

    _sent = 0


SILENT_REPLY = b"-1\r\n"
FRAMED_REPLY = b"SYSTem:ECHO?\r\n1\r\nDAQIFI> "
PAYLOAD = b"payload\r\n"


@pytest.mark.parametrize("reply", [SILENT_REPLY, FRAMED_REPLY],
                         ids=["silent", "framed"])
@pytest.mark.parametrize("split_at", list(range(1, 5)) + [-1])
@pytest.mark.parametrize("remainder_at", [0.02, 0.1, 0.3, 0.6])
def test_a_split_reply_never_contaminates_the_payload(reply, split_at,
                                                      remainder_at):
    """⛔⛔ THE r1 DEFECT, PINNED ACROSS THE WHOLE SPLIT SPACE.

    r1 decided completion by QUIET TIME: two empty polls (~40 ms) and it
    called the reply done. Measured consequences, both legs of the audit:

      * ``-1`` now, ``\r\n`` at 120 ms -> "confirmed" silence, reset cleared
        an EMPTY buffer, download returned ``b'\r\npayload'``;
      * the whole ``-1\r\n`` at 650 ms -> fell back, returned
        ``b'-1\r\npayload'``.

    ``reset_input_buffer()`` cannot prevent either: it discards what is
    BUFFERED, never what is still in flight.

    ⭐ THE ASSERTION IS A DISJUNCTION, and that is the contract: either a
    CLEAN payload, or the named refusal. NEVER contaminated bytes. Which of
    the two depends on whether the remainder beats the budget, and pinning
    that split point would be pinning the timeout rather than the property.
    """
    first, rest = reply[:split_at], reply[split_at:]
    dev = NyquistDevice("COM41")
    port = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                      [(0.0, first), (remainder_at, rest)],
                      echo_state=-1)
    dev._comm._serial = port
    try:
        got = dev.sd_get_file_binary("log.csv", timeout=5.0)
    except EchoProbeUnansweredError:
        assert b"SYSTem:STORage:SD:GET" not in bytes(port.written), (
            "refused, so the GET must not have been written")
        return
    assert got == PAYLOAD, (
        "split %r/%r arriving at %.2fs contaminated the file: got %r"
        % (first, rest, remainder_at, got))


def test_a_reply_arriving_after_the_budget_refuses_rather_than_downloading():
    """⛔ CASE (b) FROM THE AUDIT, with the reply genuinely IN FLIGHT.

    r1's own late-reply pin was INERT: its reply was already QUEUED when the
    probe gave up, so `reset_input_buffer()` swept it and the pin passed while
    both real cases went undetected. Here the whole reply is scheduled for
    650 ms -- past the 500 ms budget -- so at the moment of decision it does
    not exist yet and nothing can reset it away.
    """
    dev = NyquistDevice("COM41")
    port = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                      [(0.65, SILENT_REPLY)], echo_state=-1)
    dev._comm._serial = port
    with pytest.raises(EchoProbeUnansweredError):
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    assert b"SYSTem:STORage:SD:GET" not in bytes(port.written), (
        "a reply still in flight must not be raced by a GET")


def test_a_whole_reply_inside_the_budget_still_confirms():
    """⭐ POSITIVE CONTROL. Without it every pin above is satisfied by a probe
    that refuses unconditionally."""
    dev = NyquistDevice("COM41")
    port = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                      [(0.05, SILENT_REPLY)], echo_state=-1)
    dev._comm._serial = port
    assert dev.sd_get_file_binary("log.csv", timeout=5.0) == PAYLOAD


# ---------------------------------------------------------------------------
# r3: a refused probe leaves a reply OUTSTANDING -- across calls
# ---------------------------------------------------------------------------

class _WriteRaisesAfterSending(_Scheduled):
    """A port whose write ACCEPTS the bytes and THEN raises.

    ⛔ MODELLING "SENT BUT RAISED" IS THE POINT. pyserial's POSIX write()
    checks its deadline AFTER `os.write`, so `SerialTimeoutException` can be
    raised with the query fully on the wire -- and the written byte count is
    discarded, so how much went out is unknowable. A fake that raises BEFORE
    accepting the bytes models a write that did NOT happen, which is the
    comfortable case and not this one; that mistake is the r1 instrument
    error repeated.
    """

    def write(self, data):
        self.written.extend(data)          # the bytes ARE on the wire
        probe = self._probe_write(data)
        if probe:
            raise serial.SerialTimeoutException("write timed out after sending")
        self._released_at = time.monotonic()
        return len(data)


@pytest.mark.parametrize("schedule,label", [
    ([(0.65, SILENT_REPLY)], "wholly late"),
    ([(0.0, b"-1"), (0.65, b"\r\n")], "split, remainder late"),
])
def test_a_retry_after_a_refusal_does_not_accept_the_first_reply(schedule,
                                                                 label):
    """⛔⛔ r3 FINDING 1. r2's guarantee was PER CALL, and the refusal path is
    exactly the one that leaves a reply outstanding.

    Measured on r2: the first reply is delayed past the budget, the download
    refuses, the caller retries AT ONCE, and the retry's probe completes on
    the FIRST reply -- so the GET goes out and the SECOND reply lands in the
    file as ``b'-1\r\npayload'``.

    ⭐ POISON, NOT A COUNT, and this is why. Counting outstanding replies is
    ill-defined after a PARTIAL consumption (what is owed is a remainder, not
    a whole reply), and -- decisively -- THE PORT IS SHARED: `send_command`,
    `send_query`, `query_binary`, `_send_disable_echo`, seventeen direct uses
    in device.py and `streaming.py` reading `_comm._serial` can all consume or
    interleave the outstanding bytes between two downloads. No counter the
    probe maintains can survive that. So the transport is marked unusable for
    downloads until a reopen resynchronises it.
    """
    dev = NyquistDevice("COM41")
    port = _Scheduled(PAYLOAD + END_OF_FILE_MARKER, schedule, echo_state=-1)
    dev._comm._serial = port
    with pytest.raises(EchoProbeUnansweredError):
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    # The caller retries immediately, which is the realistic thing to do.
    port._payload = PAYLOAD + END_OF_FILE_MARKER
    port._sent = 0
    with pytest.raises(EchoProbeUnansweredError) as err:
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    assert "earlier on this connection" in str(err.value), (
        "the retry must refuse because the transport is poisoned, not "
        "re-probe and accept the first reply (%s)" % label)
    assert b"SYSTem:STORage:SD:GET" not in bytes(port.written), (
        "no GET may reach the wire on either attempt")


def test_a_write_that_raises_after_sending_refuses_and_poisons():
    """⛔ r3 FINDING 2. r2 treated a probe write exception as "fall back and
    download", and the reply then landed in the file. An attempted write is
    not a write that did not happen."""
    dev = NyquistDevice("COM41")
    port = _WriteRaisesAfterSending(PAYLOAD + END_OF_FILE_MARKER,
                                    [(0.05, SILENT_REPLY)], echo_state=-1)
    dev._comm._serial = port
    with pytest.raises(EchoProbeUnansweredError) as err:
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    assert "write failed" in str(err.value)
    assert bytes(port.written).startswith(b"SYSTem:ECHO?"), (
        "this fake must model the query reaching the wire BEFORE the raise, "
        "or it is testing the comfortable case")
    assert b"SYSTem:STORage:SD:GET" not in bytes(port.written), (
        "a write that may have been sent must not be followed by a GET")


def test_the_poison_clears_only_on_a_reopen():
    """⛔ A THREE-ATTEMPT CHAIN. Under poison the shape is refuse, refuse,
    then refuse again -- not "succeed on the third". That is a deliberate
    difference from the counting design, which would have consumed three
    replies and confirmed; counting was rejected for the reasons in
    `test_a_retry_after_a_refusal_does_not_accept_the_first_reply`.

    ⭐ AND RECOVERY IS PINNED, because a poison nothing clears is a bricked
    connection rather than a safety property.
    """
    dev = NyquistDevice("COM41")
    port = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                      [(0.65, SILENT_REPLY)], echo_state=-1)
    dev._comm._serial = port
    for attempt in range(3):
        with pytest.raises(EchoProbeUnansweredError):
            dev.sd_get_file_binary("log.csv", timeout=5.0)
        port._payload = PAYLOAD + END_OF_FILE_MARKER
        port._sent = 0
    assert dev._comm._echo_probe_poisoned is True

    # ⛔ r4 finding 1: A REOPEN DOES NOT CLEAR IT. Swapping in a fresh port
    # models exactly what close()/open() achieves host-side -- and it is not
    # enough, because the device may still send the old reply.
    fresh = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                       [(0.02, SILENT_REPLY)], echo_state=-1)
    dev._comm._serial = fresh
    with pytest.raises(EchoProbeUnansweredError) as err:
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    assert "REOPEN DOES NOT CLEAR" in str(err.value), (
        "a new port must not revive downloads on a poisoned transport")

    # A NEW connection object starts clean -- the documented escape hatch, and
    # the only one. Verified per-instance so one device cannot brick another.
    other = NyquistDevice("COM42")
    other._comm._serial = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                                     [(0.02, SILENT_REPLY)], echo_state=-1)
    assert other.sd_get_file_binary("log.csv", timeout=5.0) == PAYLOAD, (
        "a fresh connection object must work; the poison is per-instance")


def test_open_does_NOT_clear_the_poison():
    """⛔⛔ r4 finding 1 REVERSES r3's pin, which asserted the opposite.

    r3 cleared the poison in `open()` and claimed "a fresh port resynchronises
    the stream by construction". That claim is FALSE: close()/open() resets
    HOST-side buffers, which discards only what has already ARRIVED, and the
    firmware's DTR handler does not cancel output the device has already
    queued. Measured on r3: a late first-probe reply satisfied the
    post-reopen probe and the download returned
    ``b'DAQIFI> SYSTem:ECHO -1\r\n-1\r\npayload'`` -- carrying the reopen's
    OWN echo-disable traffic into the file.

    ⚠ So the clearing must NOT be in open(), and the only remaining exit is a
    content-confirmed completion. The brief's other option -- clear on a reply
    "uniquely attributable to THIS probe" -- is unavailable: `SYSTem:ECHO?`
    admits no nonce, so two probes' replies are byte-identical and no reply
    can be tied to a particular one. A caller needing downloads after a
    poisoned probe must build a NEW connection object.
    """
    import inspect
    comm_cls = type(NyquistDevice("COM41")._comm)
    assert "_echo_probe_poisoned = False" not in inspect.getsource(comm_cls.open), (
        "open() must NOT clear the probe poison -- a reopen does not "
        "resynchronise a reply the device has already queued")
    probe_src = inspect.getsource(comm_cls.probe_echo_silent)
    assert "settled is not None" in probe_src, (
        "the only clearing must be the content-confirmed one in the probe's "
        "finally block")


# ---------------------------------------------------------------------------
# The probe happens before EVERY download -- never cached
# ---------------------------------------------------------------------------

def test_the_state_is_re_read_for_each_download():
    """⛔ NO CACHING. The caller can send `SYSTem:ECHO 1` down the raw path
    between two downloads; a cached -1 would then skip the strip on a framed
    stream. First download confirms -1 and keeps everything; the second finds
    1 and strips.
    """
    content = PROMPT + _echo_of("log.csv") + b"\r\n" + PROMPT + b"1,2\r\n"
    dev = NyquistDevice("COM41")
    port = _Port(content + END_OF_FILE_MARKER, echo_state=-1)
    dev._comm._serial = port
    first = dev.sd_get_file_binary("log.csv", timeout=2.0)
    assert first == content, "confirmed -1: nothing stripped"

    # The console changed underneath us. Nothing told the library.
    port.echo_state = 1
    port._payload = content + END_OF_FILE_MARKER
    second = dev.sd_get_file_binary("log.csv", timeout=2.0)
    assert second == b"1,2\r\n", (
        "the second download must re-read the state and strip; got %r"
        % (second,))
    assert port.written.count(b"SYSTem:ECHO?") == 2, (
        "one probe per download, not one per session")


def test_a_reboot_between_downloads_is_caught_with_no_other_event():
    """⛔ THE SHAPE A CACHE CANNOT SURVIVE. A device that reboots comes back at
    its per-transport default (USB 1, TCP 0) and NOTHING signals it -- no
    reconnect, no raw write, no library call. Option (b), cache-and-invalidate,
    has no event to hang an invalidation on, which is why it was rejected.
    """
    content = PROMPT + _echo_of("log.csv") + b"\r\n" + PROMPT + b"1,2\r\n"
    dev = NyquistDevice("COM41")
    port = _Port(content + END_OF_FILE_MARKER, echo_state=-1)
    dev._comm._serial = port
    assert dev.sd_get_file_binary("log.csv", timeout=2.0) == content

    port.echo_state = 1            # the reboot, and that is the ONLY event
    port._payload = content + END_OF_FILE_MARKER
    assert dev.sd_get_file_binary("log.csv", timeout=2.0) == b"1,2\r\n", (
        "a reboot between downloads must be caught by the next probe")


def test_the_probe_precedes_the_get():
    """The state must be established BEFORE the command that starts the
    stream; afterwards the device is sending and a query would interleave."""
    _, port = _get(b"payload" + END_OF_FILE_MARKER, echo_state=1)
    wire = bytes(port.written)
    assert wire.index(b"SYSTem:ECHO?") < wire.index(b"SYSTem:STORage:SD:GET")


# ---------------------------------------------------------------------------
# Condition 1: every SD fake goes through the ONE helper
# ---------------------------------------------------------------------------

def test_every_sd_fake_answers_the_probe_through_the_shared_helper():
    """⛔ ONE DEFINITION, asserted in one place.

    Eight hand-copied hooks is the duplicated-definition pattern this series
    keeps paying for (#264 is the live example). Any SD test module that
    installs a fake port must reach the shared mixin or the shared policy
    function, never its own copy of "what a device replies".
    """
    here = Path(__file__).parent
    offenders = []
    for path in sorted(here.glob("test_sd_*.py")) + [here / "test_carried_items_206.py"]:
        src = path.read_text(encoding="utf-8")
        if "_comm._serial" not in src:
            continue                      # not a port-installing module
        if "EchoProbeFake" in src or "echo_probe_reply" in src:
            continue                      # goes through the shared helper
        # ⛔ r4: ANY module that installs a port must go through the helper,
        # whether or not it mentions ECHO? itself. The first version only
        # flagged modules containing "ECHO?", so a fake that simply IGNORED
        # the probe slipped through -- and under r4 that is not benign: a port
        # without `out_waiting` cannot have its drain bounded, so every
        # download on it refuses. test_sd_size_cache_invalidation_154.py was
        # exactly that, and the full suite found it instead of this test.
        offenders.append(path.name)
    assert offenders == [], (
        "these modules answer the echo probe without the shared helper: %s"
        % offenders)


# ---------------------------------------------------------------------------
# r4: the four audit findings
# ---------------------------------------------------------------------------

def test_a_real_close_open_cycle_does_not_revive_a_poisoned_transport(
        monkeypatch):
    """⛔⛔ r4 FINDING 1, through the REAL close() and open().

    r3 cleared the poison inside `open()`. The audit showed the reopen does
    not resynchronise anything the DEVICE has queued: host-side resets discard
    only what has already ARRIVED, and the firmware's DTR handler does not
    cancel pending output. Measured on r3, the download came back as
    ``b'DAQIFI> SYSTem:ECHO -1\r\n-1\r\npayload'`` -- the reopen's own
    echo-disable traffic inside the file.

    ⚠ This drives the real methods rather than swapping a port object,
    because the defect WAS in `open()`. `serial.Serial` is patched to hand
    back a fake, which is the only part that cannot run here.
    """
    import daqifi._internal.comm as comm_mod

    made = []

    def _factory(*a, **kw):
        port = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                          [(0.65, SILENT_REPLY)], echo_state=-1)
        made.append(port)
        return port

    monkeypatch.setattr(comm_mod.serial, "Serial", _factory)
    dev = NyquistDevice("COM41")
    assert dev._comm.open() is True, "the patched open() must succeed"

    with pytest.raises(EchoProbeUnansweredError):
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    assert dev._comm._echo_probe_poisoned is True

    # The real cycle, which r3 believed was a resynchronisation.
    dev._comm.close()
    assert dev._comm.open() is True
    assert dev._comm._echo_probe_poisoned is True, (
        "close()/open() must NOT clear the poison -- it resets host buffers, "
        "not the device's output queue")
    with pytest.raises(EchoProbeUnansweredError) as err:
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    assert "REOPEN DOES NOT CLEAR" in str(err.value)
    assert not any(b"SYSTem:STORage:SD:GET" in bytes(p.written) for p in made), (
        "no GET may reach any port in this sequence")


class _BlockingFlush(_Port):
    """A port whose ``flush()`` blocks, as pyserial's really can.

    ⛔ `write_timeout` governs neither backend's flush: pyserial 3.5 polls
    `out_waiting` with NO deadline on Windows and calls `tcdrain()` on POSIX,
    and `tcdrain()` takes no timeout at all. `_drain_bounded`'s own docstring
    records the measured case -- an FTDI adapter with CTS deasserted, where
    `out_waiting` reads 0 and `tcdrain()` blocks forever.
    """

    flush_calls = 0

    def flush(self):
        type(self).flush_calls += 1
        time.sleep(5.0)


def test_the_probe_does_not_flush_and_is_bounded_even_if_flush_would_block():
    """⛔ r4 FINDING 2. r3 called `flush()` -- unbounded on both backends --
    and only THEN computed the deadline, so the budget could be spent before
    the clock started.

    Asserted two ways: the probe must not call `flush()` at all, and it must
    return within its budget even against a port whose flush would block for
    seconds. The second is what makes the first more than a style rule.
    """
    _BlockingFlush.flush_calls = 0
    dev = NyquistDevice("COM41")
    port = _BlockingFlush(PAYLOAD + END_OF_FILE_MARKER, echo_state=-1)
    dev._comm._serial = port
    bound = dev._comm.ECHO_PROBE_TIMEOUT_S

    # ⚠ SCOPED TO THE PROBE. An earlier version drove a whole download and
    # asserted zero flushes -- but `_receive_sd_file` flushes its own GET
    # write, so the count was 1 for a reason that has nothing to do with the
    # probe. A pin has to measure the thing it names.
    t0 = time.monotonic()
    assert dev._comm.probe_echo_silent() is True
    elapsed = time.monotonic() - t0
    assert _BlockingFlush.flush_calls == 0, (
        "the PROBE must not call flush(); it is unbounded on both backends")
    assert elapsed < bound * 4, (
        "the probe must stay inside its budget (%.2fs); took %.2fs"
        % (bound, elapsed))


def test_the_deadline_is_created_before_the_drain():
    """The ordering from finding 2, pinned structurally as well: a later edit
    that moves the drain ahead of the deadline reintroduces the defect without
    changing any observable behaviour on a fast port."""
    import ast
    import inspect
    import textwrap

    # ⚠ ASKED OVER THE AST. The first version compared TEXT indices and failed
    # on correct code, because `_drain_bounded` is named in a COMMENT above
    # the deadline assignment -- so the pin was measuring my own prose. That
    # is the eighth text-pin trap in this series; comments may pin the WIRING,
    # never the ORDER of statements.
    tree = ast.parse(textwrap.dedent(inspect.getsource(
        type(NyquistDevice("COM41")._comm).probe_echo_silent)))
    deadline_line = min(
        n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "deadline" for t in n.targets))
    drain_line = min(
        n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "_drain_bounded")
    assert deadline_line < drain_line, (
        "the deadline must be created BEFORE the drain (line %d vs %d), or "
        "the budget can be spent before the clock starts"
        % (deadline_line, drain_line))


PRECEDING = [
    (b"\r\n", "a bare CRLF from the previous command"),
    (b"DAQIFI> ", "a pending prompt"),
    (b"0,\"No error\"\r\n", "a previous query's answer"),
    (b"SYSTem:ECHO -1\r\n", "the open-time echo-disable echo"),
]


@pytest.mark.parametrize("foreign,label", PRECEDING,
                         ids=[x[1] for x in PRECEDING])
@pytest.mark.parametrize("arrives_at", [0.0, 0.05, 0.2])
def test_a_preceding_commands_bytes_do_not_complete_our_probe(foreign, label,
                                                              arrives_at):
    """⛔⛔ r4 FINDING 3, across when the foreign bytes land -- before, during
    and after the probe write.

    r3 called any terminated buffer "unparseable" and fell back to the
    byte-shape download on it. A preceding command's delayed CRLF is exactly
    that, and it completed the probe before our own reply arrived, so the
    download ran with the reply in flight and returned contaminated bytes.

    ⭐ THE ASSERTION IS A DISJUNCTION: a clean payload, or the refusal. Never
    contaminated bytes, and never a payload with foreign bytes glued on.
    """
    dev = NyquistDevice("COM41")
    port = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                      [(arrives_at, foreign), (arrives_at + 0.05, SILENT_REPLY)],
                      echo_state=-1)
    dev._comm._serial = port
    try:
        got = dev.sd_get_file_binary("log.csv", timeout=5.0)
    except EchoProbeUnansweredError:
        assert b"SYSTem:STORage:SD:GET" not in bytes(port.written), (
            "refused, so no GET may have been written (%s)" % label)
        return
    assert got == PAYLOAD, (
        "%s arriving at %.2fs contaminated the file: got %r"
        % (label, arrives_at, got))


class _InterruptDuringProbe(_Port):
    """Raises KeyboardInterrupt from the probe's read loop."""

    def __init__(self, payload, echo_state=-1, after=1):
        super().__init__(payload, echo_state)
        self._polls = 0
        self._after = after

    @property
    def in_waiting(self):
        self._polls += 1
        if self._polls >= self._after:
            raise KeyboardInterrupt("operator pressed Ctrl-C mid-probe")
        return self._probe_pending(lambda: len(self._buf))


def test_a_keyboard_interrupt_during_the_probe_still_poisons():
    """⛔ r4 FINDING 4. r3 set the poison only on its refusal path, so ANY
    BaseException escaping the probe left the transport marked CLEAN with a
    reply outstanding -- and the retry then accepted the earlier reply.

    ⚠ KeyboardInterrupt PROPAGATES AS ITSELF, deliberately: an operator
    pressing Ctrl-C must not be reported as a device fault. What changes is
    that the transport is marked on the way out.
    """
    dev = NyquistDevice("COM41")
    port = _InterruptDuringProbe(PAYLOAD + END_OF_FILE_MARKER)
    dev._comm._serial = port
    with pytest.raises(KeyboardInterrupt):
        dev.sd_get_file_binary("log.csv", timeout=5.0)
    assert dev._comm._echo_probe_poisoned is True, (
        "a BaseException during the probe must leave the transport marked")

    # And the retry must refuse rather than accept the earlier reply.
    dev._comm._serial = _Scheduled(PAYLOAD + END_OF_FILE_MARKER,
                                   [(0.02, SILENT_REPLY)], echo_state=-1)
    with pytest.raises(EchoProbeUnansweredError):
        dev.sd_get_file_binary("log.csv", timeout=5.0)

