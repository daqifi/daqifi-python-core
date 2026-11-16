"""#260 r6: the echo state is SET before a download, never QUERIED.

⛔⛔ WHY THE QUERY HAD TO GO. ``SYSTem:ECHO?`` admits no nonce, so two
probes' replies are byte-identical and no reply can be tied to the probe
that asked for it. r2-r5 answered that with a per-object "poison" flag, and
the r5 audit broke it in one line: **poison is host-side per-object state,
and the stale bytes live on the DEVICE.** A new connection object, a new
process or a different client starts with a clean flag and takes the
previous object's reply as its own confirmation.

Worse, the r5 refusal ENDED WITH "Use a NEW connection object." -- it
advised the one action that turns a loud refusal into a silently corrupted
file. That is the whole class of defect, not an instance of it.

⭐ A SET HAS NO REPLY TO MISATTRIBUTE. ``SYSTem:ECHO -1`` tells the device
what to do instead of asking what it is doing, so after it the state is
known BY CONSTRUCTION. The ticket suggested a query; the ticket's GOAL is
the echo state, and a set reaches the state.

What is left to get right is the SETTER'S OWN framing: if echo is on when
the setter lands, the device echoes the setter line and may emit a prompt.
Those are EXACT KNOWN BYTES -- which is the property the probe's ``-1\r\n``
never had -- so they are consumed by CONTENT, and anything of them still in
flight when the GET is answered is strippable as a known preamble rather
than guessed at.

The two tests marked FINDING 1 below are the r5 audit's schedules, and both
are red on d8930bad.
"""

import time

import pytest

import daqifi._internal.comm as comm_mod
from daqifi.device import NyquistDevice

from pathlib import Path

import serial

from daqifi.sdcard import END_OF_FILE_MARKER
from daqifi.exceptions import EchoSetUnconfirmedError
from tests._sd_echo_setter import EchoSetterFake

# ⛔ SIZED TO THE REPLY WINDOW, and this is the whole difference between a
# reproduction and a green test. At 9 bytes the download finishes in ~0.18 s
# -- long before the next probe reply is due at +0.65 s -- so BOTH
# finding-1 legs passed on d8930bad against the defect the r5 audit found by
# reading. A real SD file takes seconds, and the late reply lands INSIDE
# that window. 34 chunks x 0.03 s = ~1.02 s of download.
PAYLOAD = b"".join(b"%03d," % i for i in range(34))      # 136 bytes
SILENT_REPLY = b"-1\r\n"
PROMPT = b"DAQIFI> "
SETTER = b"SYST:ECHO -1"
SET_FRAMED = b"SYST:ECHO 1"
OPEN_SETTER = b"SYSTem:ECHO -1"      # what open() writes


class _Device(EchoSetterFake):
    """A port modelling ONE device-side output stream.

    ⛔ THIS IS THE FAKE THE r5 PINS COULD NOT BE WRITTEN WITH, and that is
    why the r5 audit found by reading what my tests could not reach.
    ``EchoProbeFake``'s schedule is keyed on ``_probe_sent_at`` and its
    reply buffer is CLEARED by the next probe write, so "probe 1's reply is
    still in flight when probe 2 is written" -- the exact finding-1 state --
    cannot be expressed in it at all.

    Here the device owns a single FIFO of ``(due_at, bytes)``. Nothing the
    host does removes an entry: bytes already queued on the device go out
    when their time comes, whoever is listening and whatever object is now
    holding the port. A host-side flag cannot touch this queue, which is
    precisely the audit's point.
    """

    #: Seconds between a command landing and its reply being put on the wire.
    reply_delay = 0.65
    payload_gap = 0.03
    payload_chunk = 4

    def __init__(self, payload=PAYLOAD + END_OF_FILE_MARKER, echo_state=-1,
                 reply_delay=None):
        if reply_delay is not None:
            self.reply_delay = reply_delay
        self.is_open = True
        self.written = bytearray()
        self.echo_state = echo_state
        self._payload = bytes(payload)
        self._queue = []          # [(due_at, bytes)] -- the DEVICE's output
        self._buf = bytearray()   # what has reached the wire
        self.probe_writes = 0
        self.setter_writes = 0
        self.framed_writes = 0
        self.get_writes = 0

    # -- device side ------------------------------------------------------

    def seed_foreign(self, data, delay):
        """Put bytes on the device that THIS client never asked for.

        ⛔ THE POST-(A) FORM OF THE r5 FINDING. Once nothing queries, this
        library cannot leave a reply in flight at all -- so the hazard that
        remains is a reply left by SOMETHING ELSE: an older version of this
        library, another client, a serial terminal someone typed in. A
        host-side flag could never have covered those either, which was the
        audit's actual point.
        """
        self._enqueue(data, delay)

    def _enqueue(self, data, delay):
        self._queue.append((time.monotonic() + delay, bytes(data)))

    def _pump(self):
        now = time.monotonic()
        keep = []
        for due, data in self._queue:
            if due <= now:
                self._buf.extend(data)
            else:
                keep.append((due, data))
        self._queue = keep

    def write(self, data):
        self.written.extend(data)
        text = bytes(data)
        if b"SYSTem:ECHO?" in text:
            # A QUERY: the device will answer, late. Nothing the host does
            # next cancels it.
            self.probe_writes += 1
            self._enqueue(SILENT_REPLY if self.echo_state == -1
                          else b"SYSTem:ECHO?" + b"\r\n1\r\n" + PROMPT,
                          self.reply_delay)
            return len(data)
        if OPEN_SETTER in text:
            # open()'s own echo-disable. Deliberately a DIFFERENT command
            # from the download's marker, because that is the collision
            # being pinned: same effect on the device, different bytes on
            # the wire.
            if self.echo_state != -1:
                self._enqueue(OPEN_SETTER + b"\r\n" + PROMPT,
                              self.reply_delay)
            self.echo_state = -1
            return len(data)
        if SETTER in text:
            # A SET. Replies only by ECHOING, and only if echo is on NOW --
            # microrl gates on the state as the characters arrive --
            # and afterwards the state is -1. Exact, known bytes either way.
            self.setter_writes += 1
            if self.echo_state != -1:
                self._enqueue(SETTER + b"\r\n" + PROMPT, self.reply_delay)
            self.echo_state = -1
            return len(data)
        if SET_FRAMED in text:
            # Echo ON. Echoed only if echo was ALREADY on, for the same
            # reason; after it the next line is certain to be echoed.
            self.framed_writes += 1
            if self.echo_state != -1:
                self._enqueue(SET_FRAMED + b"\r\n" + PROMPT,
                              self.reply_delay)
            self.echo_state = 1
            return len(data)
        if b"SD:GET" in text.upper():
            self.get_writes += 1
            for i in range(0, len(self._payload), self.payload_chunk):
                self._enqueue(self._payload[i:i + self.payload_chunk],
                              self.payload_gap * (i // self.payload_chunk))
        return len(data)

    # -- host side --------------------------------------------------------

    @property
    def in_waiting(self):
        self._pump()
        return len(self._buf)

    def read(self, n=1):
        self._pump()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def reset_input_buffer(self):
        # Discards what ARRIVED. The queue is untouched, because a host
        # cannot un-send what the device has already decided to send.
        self._buf.clear()

    def reset_output_buffer(self):
        # ⚠ PRESENT BECAUSE THE REAL open() CALLS IT. Without it the
        # reconnect leg below died on an AttributeError, i.e. it was an
        # INERT reproduction: red, but for a deficiency of the fake rather
        # than the defect under test.
        pass

    def flush(self):
        pass


def _fresh_device(port):
    dev = NyquistDevice("COM41")
    dev._comm._serial = port
    return dev


def _download(dev, port):
    """-> (result or None, exception or None).

    ⚠ THE BINARY VARIANT ON PURPOSE. `sd_get_file` returns `str`, and
    `bytes(a_str)` raises "string argument without an encoding" -- which is
    what the first version of these tests actually failed on, making both
    finding-1 legs red for a reason that had nothing to do with the stale
    reply. A byte-exact claim needs a byte-returning call.
    """
    try:
        return dev.sd_get_file_binary("x.bin", timeout=2.0), None
    except Exception as exc:                                  # noqa: BLE001
        return None, exc


def _assert_clean_or_refused(got, exc, port, label):
    """The ONLY two acceptable outcomes. Never a prepend."""
    if exc is not None:
        return
    assert got is not None, f"{label}: neither a result nor an exception"
    body = bytes(got)
    assert body == PAYLOAD, (
        "%s: the download must equal the payload or refuse, never prepend. "
        "Got %r (device emitted %d probe repl%s)"
        % (label, body, port.probe_writes,
           "y" if port.probe_writes == 1 else "ies"))


# ---------------------------------------------------------------------------
# r5 audit, FINDING 1 -- both schedules. Red on d8930bad.
# ---------------------------------------------------------------------------

# ⛔ SWEPT, NOT HAND-PICKED. At 0.65 s the reconnect leg passed and the
# new-object leg failed; a single chosen latency would therefore have
# "shown" whichever answer I happened to pick. The device's reply latency
# is not ours to choose, so the invariant is asserted across the range
# where a reply outlives the 0.5 s probe budget.
# Latencies either side of ECHO_SET_TIMEOUT_S (2.0 s): the first three are
# devices that answer in time, the last is one that does not. The invariant
# is the same for all four -- clean payload or a refusal, never corruption --
# and only the branch taken differs.
LATENCIES = [0.65, 0.9, 1.2, 2.5]


@pytest.mark.parametrize("latency", LATENCIES,
                         ids=["%gs" % x for x in LATENCIES])
@pytest.mark.parametrize("echo_state", [-1, 1], ids=["silent", "framed"])
def test_a_stale_foreign_reply_does_not_corrupt_the_download(latency,
                                                             echo_state):
    """⛔⛔ r5 FINDING 1, RESTATED FOR A LIBRARY THAT NEVER QUERIES.

    The audit's schedule was: our own probe times out, its `-1\r\n` is
    still on the device, and the next connection object takes that reply as
    its own confirmation. Under (A) the first half cannot happen -- nothing
    asks -- so the test seeds the same stale `-1\r\n` from OUTSIDE, which
    is the only way it can exist now: an older client, another process, a
    human at a terminal.

    ⚠ MY FIRST VERSION OF THIS TEST ENCODED THE PROBE DESIGN IN ITS
    PREMISE -- it asserted "the first download must refuse" before getting
    to the point, which is only true of the machinery being removed. A
    reproduction that assumes the implementation cannot survive replacing
    it.

    The marker the setter pair guarantees is what makes this winnable: the
    scan discards everything ahead of our own echo, stale bytes included.
    """
    port = _Device(echo_state=echo_state, reply_delay=latency)
    port.seed_foreign(SILENT_REPLY, latency)

    dev = _fresh_device(port)
    got, exc = _download(dev, port)
    _assert_clean_or_refused(got, exc, port, "stale foreign reply")


@pytest.mark.parametrize("latency", LATENCIES,
                         ids=["%gs" % x for x in LATENCIES])
def test_a_reconnect_through_open_does_not_inherit_a_stale_reply(latency,
                                                                 monkeypatch):
    """⛔⛔ r5 FINDING 1, schedule 2: the same stale bytes, but reached
    through the REAL open(), so open()'s own `SYSTem:ECHO -1` traffic is in
    the stream as well. r5 returned the reconnect traffic plus a second
    `-1\r\n` prepended to the file.
    """
    port = _Device(echo_state=1, reply_delay=latency)
    port.seed_foreign(SILENT_REPLY, latency)

    monkeypatch.setattr(comm_mod.serial, "Serial", lambda *a, **kw: port)
    dev = NyquistDevice("COM41")
    assert dev._comm.open() is True, "the reopen must succeed"
    got, exc = _download(dev, port)
    _assert_clean_or_refused(got, exc, port, "reconnect")


# ---------------------------------------------------------------------------
# What (A) must buy: no query on the wire at all.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("echo_state", [-1, 0, 1],
                         ids=["silent", "echo-0", "framed"])
def test_a_download_sets_the_echo_state_and_never_queries_it(echo_state):
    """⛔ THE QUERY MUST BE GONE FROM THE WIRE, from every starting state.

    A set has no reply to misattribute, so this is the assertion that keeps
    the whole finding-1 class closed rather than patched: not "the probe
    handles the stale reply correctly" but "nothing asks a question whose
    answer could be stale".
    """
    port = _Device(echo_state=echo_state)
    dev = _fresh_device(port)
    got, exc = _download(dev, port)

    assert port.probe_writes == 0, (
        "no %r may reach the wire: its reply cannot be attributed to the "
        "probe that asked. Wrote %r" % (b"SYSTem:ECHO?", bytes(port.written)))
    assert exc is None, "the download must succeed from echo_state=%r: %r" % (
        echo_state, exc)
    assert bytes(got) == PAYLOAD, (
        "echo_state=%r: got %r" % (echo_state, bytes(got)))


def test_the_setter_is_written_before_the_get(monkeypatch):
    """Order on the wire, not just presence: the state must be set BEFORE
    the GET, or the GET is the thing that gets echoed."""
    port = _Device(echo_state=1)
    dev = _fresh_device(port)
    _download(dev, port)
    wire = bytes(port.written)
    assert SETTER in wire, "the setter must reach the wire: %r" % wire
    assert b"SD:GET" in wire.upper(), "the GET must reach the wire: %r" % wire
    assert wire.index(SETTER) < wire.upper().index(b"SD:GET"), (
        "the setter must precede the GET: %r" % wire)


def test_the_download_marker_cannot_be_forged_by_open_or_close():
    """⛔ COUPLING, so the collision cannot come back silently.

    The download's marker is the SHORT spelling precisely so that open()'s
    and close()'s own echo traffic cannot look like it. If someone later
    changes any of those three to agree, this fails here rather than
    showing up as a corrupted file in a measurement months later.
    """
    from daqifi._internal.comm import SerialInterface as SI
    marker = SI._ECHO_SET_SILENT
    for other in (OPEN_SETTER, b"SYSTem:ECHO 1", SI._ECHO_SET_FRAMED):
        if other == marker:
            continue
        assert marker not in other + b"\r\n" + PROMPT, (
            "the download marker %r must not appear in other echo traffic "
            "%r, or the download will synchronise on the wrong one"
            % (marker, other))


# ---------------------------------------------------------------------------
# PORTED FROM test_sd_echo_state_not_byte_shape_260.py, which r6 deleted.
#
# ⛔ WHAT WAS DELETED AND WHY. That module had 76 tests and 14 of them asked
# whether a `SYSTem:ECHO?` reply could be attributed to the probe that asked
# for it -- an unanswered probe, a non-attributable reply, a 40-row split
# table, the poison's lifecycle, a reopen, a KeyboardInterrupt mid-probe.
# The r5 audit established that the question itself is unanswerable, so
# those pins went with the machinery rather than being rewritten around it.
#
# The pins below are the ones whose subject SURVIVES a set, and they are
# #260's actual acceptance criteria -- losing them would leave the ticket
# with no test for its own purpose.
# ---------------------------------------------------------------------------

LONG_NAME = "a" * 176 + ".bin"


def _echo_of(name):
    return ('SYSTem:STORage:SD:GET "%s"' % name).encode("ascii")


class _Simple(EchoSetterFake):
    """Request/response: answer the setter, then serve the file at once."""

    def __init__(self, payload, echo_state=1):
        self.is_open = True
        self.written = bytearray()
        self.echo_state = echo_state
        self._payload = bytes(payload)
        self._buf = bytearray()

    def write(self, data):
        self.written.extend(data)
        if self._console_write(data):
            return len(data)
        self._buf.extend(self._payload)
        self._payload = b""
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return self._console_pending(lambda: len(self._buf))

    def read(self, n=1):
        def _own():
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out
        return self._console_read(n, _own)

    def reset_input_buffer(self):
        self._console_reset()


def _get(payload, echo_state=1, name="log.csv", binary=True, timeout=2.0):
    dev = NyquistDevice("COM41")
    port = _Simple(payload, echo_state)
    dev._comm._serial = port
    method = dev.sd_get_file_binary if binary else dev.sd_get_file
    return method(name, timeout=timeout), port


@pytest.mark.parametrize("state", [-1, 0, 1],
                         ids=["silent", "echo-0", "framed"])
def test_case_d_content_is_intact_whatever_the_console_was_doing(state):
    """⛔⛔ THE CLOSURE #260 BUYS, AND (A) MADE IT UNCONDITIONAL.

    This file's own content IS the framing shape, which the pre-#260 head
    strips by byte shape because it cannot know better. Under the probe it
    survived only when the device HAPPENED to answer -1; a framed console
    still lost 49 of its 54 bytes. A set does not observe the state, it
    establishes it -- so the console is silent before the GET from ANY
    starting point and all 54 bytes are file data in every row.

    ⚠ 0 IS PARAMETRISED ALONGSIDE 1 DELIBERATELY. #260's issue text says
    `SYSTem:ECHO 0` turns echo off; the firmware disagrees -- every microrl
    gate is `echoOn > -1` and `SCPI_SetEcho` accepts only -1..1, so 0 and 1
    are indistinguishable on the wire. A reader who believes 0 means "off"
    has to confront both rows at once.
    """
    content = PROMPT + _echo_of("log.csv") + b"\r\n" + PROMPT + b"1,2\r\n"
    assert len(content) == 54
    got, _ = _get(content + END_OF_FILE_MARKER, echo_state=state)
    assert got == content, (
        "echo_state=%r: the setter silences the console first, so all 54 "
        "bytes are file content; got %r" % (state, got))


@pytest.mark.parametrize("state", [-1, 1], ids=["silent", "framed"])
def test_the_long_name_case_d_shape_is_intact_too(state):
    """⛔ THE 214-BYTE SHAPE, which is what disproved any 49-byte bound on
    the accepted loss: the matched candidate contains the echo, so the loss
    scales with the FILENAME. All 220 bytes come back.
    """
    content = _echo_of(LONG_NAME) + b"\r\nDAQIFI> " + b"DATA\r\n"
    assert len(content) == 220
    assert content.index(PROMPT) == 206, "the prompt must sit past 200 bytes"
    got, _ = _get(content + END_OF_FILE_MARKER, echo_state=state,
                  name=LONG_NAME)
    assert got == content, (
        "echo_state=%r: all 220 bytes are file content; got %d of them"
        % (state, len(got)))


def test_the_state_is_set_for_every_download_not_once_per_session():
    """⛔ NO CACHING, and under (A) no OBSERVING either.

    The caller can send `SYSTem:ECHO 1` down the raw path between two
    downloads, and a device that reboots comes back at its per-transport
    default with NOTHING signalling it -- no reconnect, no raw write, no
    library call. That is the shape a cache cannot survive, and it is why
    cache-and-invalidate was rejected: there is no event to hang the
    invalidation on.

    ⭐ Where the probe had to NOTICE the change, the setter CORRECTS it: the
    second download is intact too, rather than merely being stripped
    correctly.
    """
    content = PROMPT + _echo_of("log.csv") + b"\r\n" + PROMPT + b"1,2\r\n"
    dev = NyquistDevice("COM41")
    port = _Simple(content + END_OF_FILE_MARKER, echo_state=-1)
    dev._comm._serial = port
    assert dev.sd_get_file_binary("log.csv", timeout=2.0) == content

    # The console changed underneath us -- a raw write, or a reboot. This is
    # the ONLY event.
    port.echo_state = 1
    port._payload = content + END_OF_FILE_MARKER
    assert dev.sd_get_file_binary("log.csv", timeout=2.0) == content, (
        "the second download must set the state again, not assume it")
    assert bytes(port.written).count(SETTER) == 2, (
        "one setter per download, not one per session: wrote %r"
        % bytes(port.written))


class _BlockingFlush(_Simple):
    """A port whose ``flush()`` blocks, as pyserial's really can.

    ⛔ `write_timeout` governs neither backend's flush: pyserial 3.5 polls
    `out_waiting` with NO deadline on Windows and calls `tcdrain()` on
    POSIX, and `tcdrain()` takes no timeout at all.
    """

    flush_calls = 0

    def flush(self):
        type(self).flush_calls += 1
        time.sleep(5.0)


def test_the_setter_does_not_flush_and_is_bounded_even_if_flush_would_block():
    """⛔ r4 FINDING 2, carried forward to the setter. Asserted two ways: it
    must not call `flush()` at all, and it must return within its budget
    against a port whose flush would block for seconds. The second is what
    makes the first more than a style rule.

    ⚠ SCOPED TO THE SETTER, not to a whole download -- `_receive_sd_file`
    drains its own GET write, so counting flushes across a download measures
    something else.
    """
    _BlockingFlush.flush_calls = 0
    dev = NyquistDevice("COM41")
    dev._comm._serial = _BlockingFlush(PAYLOAD + END_OF_FILE_MARKER,
                                       echo_state=1)
    bound = dev._comm.ECHO_SET_TIMEOUT_S

    t0 = time.monotonic()
    dev._comm.silence_echo_for_download()
    elapsed = time.monotonic() - t0
    assert _BlockingFlush.flush_calls == 0, (
        "the setter must not call flush(); it is unbounded on both backends")
    assert elapsed < bound * 4, (
        "the setter must stay inside its budget (%.2fs); took %.2fs"
        % (bound, elapsed))


class _NeverDrains(_Simple):
    """A port whose OS output queue never empties, as #263's does.

    ``out_waiting`` is a property, as on pyserial's Serial, and it stays
    nonzero: the bounded drain can only end by raising.
    """

    @property
    def out_waiting(self):
        return 7


def test_a_setter_whose_drain_never_completes_reports_the_port_fault():
    """⛔ A PORT FAULT PROPAGATES AS A PORT FAULT (pc#267's contract, ruled
    in r5): a stuck output queue is a property of the port, not of the echo
    setter, and all six of #263's send sites must report it identically so
    callers keep ONE handler.

    ⚠ The refusal information does not live in the type, so it is asserted
    where it does live: no GET reaches the wire.
    """
    dev = NyquistDevice("COM41")
    port = _NeverDrains(PAYLOAD + END_OF_FILE_MARKER, echo_state=1)
    dev._comm._serial = port
    bound = dev._comm.ECHO_SET_TIMEOUT_S

    t0 = time.monotonic()
    with pytest.raises(serial.SerialTimeoutException) as err:
        dev._comm.silence_echo_for_download()
    assert time.monotonic() - t0 < bound * 4, "the drain must be BOUNDED"
    assert "output buffer still holds" in str(err.value)

    fresh = NyquistDevice("COM41")
    fresh_port = _NeverDrains(PAYLOAD + END_OF_FILE_MARKER, echo_state=1)
    fresh._comm._serial = fresh_port
    with pytest.raises(serial.SerialTimeoutException):
        fresh.sd_get_file_binary("x.bin", timeout=1.0)
    assert b"SD:GET" not in bytes(fresh_port.written).upper(), (
        "a port that cannot be drained must not be sent a GET: %r"
        % bytes(fresh_port.written))


def test_the_deadline_is_created_before_the_drain():
    """The ordering from r4 finding 2, pinned structurally: a later edit that
    moves the drain ahead of the deadline reintroduces the defect without
    changing any observable behaviour on a fast port.

    ⚠ ASKED OVER THE AST. An earlier version compared TEXT indices and
    failed on correct code, because the drain is named in a COMMENT above
    the deadline assignment -- so the pin was measuring prose. Comments may
    pin the WIRING, never the ORDER of statements.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(
        type(NyquistDevice("COM41")._comm).silence_echo_for_download)))
    deadline_line = min(
        n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "deadline" for t in n.targets))
    drain_lines = [
        n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == "drain_output_bounded"
             or getattr(n.func, "attr", None) in ("drain_output_bounded",
                                                  "_drain_bounded"))]
    assert drain_lines, (
        "no bounded drain found in silence_echo_for_download -- the setter "
        "must drain its own write, and this pin cannot judge an ordering "
        "that is absent")
    assert deadline_line < min(drain_lines), (
        "the deadline must be created BEFORE the drain (line %d vs %d), or "
        "the budget can be spent before the clock starts"
        % (deadline_line, min(drain_lines)))


def _handles_the_setter(src):
    """None if ``src`` installs no port; else True/False for whether it
    handles the download's echo setter.

    Extracted so the RULE can be tested on inputs it must accept and
    inputs it must REFUSE, independently of the tree it happens to be run
    over. A classifier only exercised on a clean tree cannot be
    distinguished from one that returns "clean" unconditionally.
    """
    if "_comm._serial" not in src:
        return None
    return ("EchoSetterFake" in src or "echo_setter_reply" in src
            or "transport_cannot_silence" in src)


def test_the_setter_detector_refuses_a_module_that_ignores_the_setter():
    """⛔⛔ THE NEGATIVE CONTROL, and it exists because the mutation found
    the gap.

    Replacing the classifier's condition with "always handled" left the
    sweep below GREEN: with no offender anywhere in the tree, "every module
    handles it" and "I never checked" produce the same empty list, and a
    count of modules examined does not separate them either. The only thing
    that does is an input the rule MUST reject.
    """
    installs_and_ignores = "device._comm._serial = _MyFake()\n"
    assert _handles_the_setter(installs_and_ignores) is False, (
        "a module that installs a port and never mentions the helper must "
        "be flagged -- this is the case the sweep cannot supply itself")

    for ok in ("device._comm._serial = _F()\nclass _F(EchoSetterFake): pass",
               "device._comm._serial = _F()\ntransport_cannot_silence(dev)",
               "device._comm._serial = _F()\nx = echo_setter_reply(b'', 1)"):
        assert _handles_the_setter(ok) is True, (
            "a module that reaches the helper must NOT be flagged: %r" % ok)

    assert _handles_the_setter("nothing to do with ports") is None, (
        "a module that installs no port is out of scope, not an offender")


def test_every_sd_fake_answers_the_setter_through_the_shared_helper():
    """⛔ ONE DEFINITION, asserted in one place.

    Any module that installs a fake port must either reach the shared mixin
    or declare, via `transport_cannot_silence`, that its transport has no
    echo setter at all. What it must not do is install a port and leave the
    setter unanswered: such a fake makes every download refuse.

    ⛔⛔ AND THERE IS NO EXEMPTION LIST ANY MORE. r5's finding 2 was an
    exemption I wrote: #263's drain module installed a port, answered
    nothing, and all six of its sd_get_file cases failed inside the echo
    traffic without ever reaching either GET write -- so an unbounded
    flush() on both GET drains still passed. The module was "exempt" and
    its pins were inert. Opting out of the setter is now something a module
    does IN CODE, where it also takes effect, rather than something this
    test agrees to overlook.
    """
    here = Path(__file__).parent
    offenders = [p.name for p in sorted(here.glob("test_*.py"))
                 if _handles_the_setter(p.read_text(encoding="utf-8")) is False]
    examined = [p.name for p in sorted(here.glob("test_*.py"))
                if _handles_the_setter(p.read_text(encoding="utf-8")) is not None]

    # ⛔⛔ A POSITIVE CONTROL, because an empty `offenders` has two causes and
    # "nothing is wrong" is only one of them. Found by mutation: replacing
    # the condition above with `if True: continue` skips every module, so
    # `offenders` is empty and this test PASSED while checking nothing. A
    # detector's zero proves nothing without evidence it ran.
    assert len(examined) >= 10, (
        "this test examined only %d port-installing module(s) (%s) -- it is "
        "meant to sweep the whole SD suite, so a count this low means the "
        "scan is broken, not that the suite is clean"
        % (len(examined), examined))

    assert offenders == [], (
        "these modules install a port without handling the echo setter, so "
        "every download in them refuses: %s" % offenders)


def test_a_marker_that_misses_its_budget_refuses_and_sends_no_get():
    """⛔ THE NEW REFUSAL PATH, PINNED DIRECTLY.

    The latency sweep above accepts "clean payload OR any refusal", which is
    the right invariant for it but says nothing about WHICH refusal or what
    reached the wire. r5's finding 2 and its M3 mutation are both the same
    lesson: a refusal path with no pin of its own is a refusal path that can
    be swallowed without a single test noticing.

    A device slower than the budget is the one case where the marker is
    known to be coming and known not to be here yet. Proceeding would put
    our own echo inside the file, so the download must not be attempted at
    all.
    """
    port = _Device(echo_state=1,
                   reply_delay=_comm_budget() + 0.5)
    dev = _fresh_device(port)
    with pytest.raises(EchoSetUnconfirmedError) as err:
        dev.sd_get_file_binary("x.bin", timeout=3.0)

    assert b"SD:GET" not in bytes(port.written).upper(), (
        "nothing may be downloaded when the marker is still in flight: %r"
        % bytes(port.written))
    # ⛔ AND IT MUST NOT ADVISE RECONNECTING. r5's message ended "Use a NEW
    # connection object", which is the one action that defeats the
    # protection -- the bytes are queued on the DEVICE. This asserts the
    # advice, because the advice was the defect.
    text = str(err.value)
    assert "Do NOT reconnect" in text, (
        "the refusal must not send the caller to a new object: %r" % text)


def _comm_budget():
    from daqifi._internal.comm import SerialInterface
    return SerialInterface.ECHO_SET_TIMEOUT_S
