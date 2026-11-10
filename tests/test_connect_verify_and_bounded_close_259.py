"""#259: a write-free look at the device before connect(), and a bounded close().

Two defects, one file.

1. There was no way to find out WHICH board is on a path without changing it.
   ``open()`` writes ``SYSTem:ECHO -1`` before any caller can read ``*IDN?``,
   so a process that must not touch hardware until it has confirmed the
   hardware is its own had no injection point. Verifying over a separate port
   first leaves a window between that port's close and the first byte to the
   reopened one — measured at 186.5 ms on one bench, most of it ``open()``'s
   own settle — in which the path can be reassigned, so the echo-disable
   reaches a device that was never verified.

   ``connect(verify=...)`` closes the window by construction: verification and
   the first library write happen on the SAME handle.

2. ``close()`` called an unbounded ``flush()``. ``write_timeout`` does not
   govern ``flush()`` on either backend, so a device that stopped draining
   hung ``disconnect()`` indefinitely — after the work was finished and before
   the caller could report its result.

Every pin here is device-free.
"""

import threading

import pytest
import serial

from daqifi._internal import comm as comm_mod
from daqifi._internal.comm import SerialInterface
from daqifi.exceptions import DeviceVerificationError

#: What open() puts on the wire when nothing refuses it. The ``verify=None``
#: pin is keyed on this RECORDED log rather than on reading the source,
#: because the claim is about bytes, not about code shape.
BASELINE_WRITES = [b"SYSTem:ECHO -1\r\n"]


class _Port:
    """A serial port with just the surface open()/close() touch.

    ``out_waiting`` is a list consumed one reading per poll, with the last
    element sticky — so a buffer that drains and one that never does are both
    expressible. A fixture that could not produce both verdicts would prove
    nothing about a deadline.
    """

    def __init__(self, out_waiting=(0,), omit_out_waiting=False):
        self.written = []
        self.flushes = 0
        self.closed = 0
        self.is_open = True
        self.in_waiting = 0
        self._out = list(out_waiting)
        self._omit_out_waiting = omit_out_waiting

    # -- the bits open() uses ------------------------------------------
    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def flush(self):
        self.flushes += 1

    def read(self, n=1):
        return b""

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def close(self):
        self.closed += 1
        self.is_open = False

    def __getattr__(self, name):
        # out_waiting is served here so a port can genuinely LACK it, which
        # is one of the cases close() has to report rather than paper over.
        if name == "out_waiting":
            if self._omit_out_waiting:
                raise AttributeError(name)
            return self._out.pop(0) if len(self._out) > 1 else self._out[0]
        raise AttributeError(name)


def _iface(monkeypatch, port, **kw):
    monkeypatch.setattr(comm_mod.serial, "Serial", lambda *a, **k: port)
    iface = SerialInterface("FAKEPORT", **kw)
    return iface


# ===================================================================== 1
# verify=None must be byte-identical to today
# =====================================================================

def test_without_verify_the_wire_is_exactly_what_it_was(monkeypatch):
    """The compatibility promise, pinned against a RECORDED write log.

    Not by inspection: the claim is "no caller sees different bytes", and the
    only evidence for that is the bytes.
    """
    port = _Port()
    iface = _iface(monkeypatch, port)
    assert iface.verify is None            # the default
    assert iface.open() is True
    assert port.written == BASELINE_WRITES


def test_an_explicit_verify_none_is_the_same_as_no_verify(monkeypatch):
    port = _Port()
    iface = _iface(monkeypatch, port)
    iface.verify = None
    assert iface.open() is True
    assert port.written == BASELINE_WRITES


# ===================================================================== 2
# the callable runs before the library's first write, and may write itself
# =====================================================================

def test_verify_runs_before_the_library_writes_anything(monkeypatch):
    port = _Port()
    seen = {}

    def _verify(handle):
        # The log AS IT STANDS when the callable is entered. This is the
        # whole feature: nothing of ours has gone out yet.
        seen["log"] = list(handle.written)
        return True

    iface = _iface(monkeypatch, port)
    iface.verify = _verify
    assert iface.open() is True
    assert seen["log"] == [], "the library wrote before verify ran"
    assert port.written == BASELINE_WRITES


def test_verify_may_write_and_its_bytes_come_first(monkeypatch):
    """`*IDN?` is a write. "No write before verification" means no write of
    OURS -- the caller's own verification bytes are the point."""
    port = _Port()

    def _verify(handle):
        handle.write(b"*IDN?\r")
        handle.read(64)
        return True

    iface = _iface(monkeypatch, port)
    iface.verify = _verify
    assert iface.open() is True
    assert port.written == [b"*IDN?\r"] + BASELINE_WRITES


# ===================================================================== 3
# a refusal: closed, nothing written, and attributable
# =====================================================================

def test_a_falsy_verify_closes_the_port_and_writes_nothing(monkeypatch):
    port = _Port()
    iface = _iface(monkeypatch, port)
    iface.verify = lambda handle: False
    assert iface.open() is False
    assert port.written == [], "a refused device was written to"
    assert port.closed == 1, "a refused port was left open"
    assert iface._serial is None
    assert isinstance(iface.last_open_error, DeviceVerificationError)
    assert "FAKEPORT" in str(iface.last_open_error)


def test_a_raising_verify_preserves_that_exception_exactly(monkeypatch):
    """Not wrapped: _connect() re-raises last_open_error, so the caller gets
    back what its own code raised."""
    port = _Port()
    boom = RuntimeError("wrong serial number")
    iface = _iface(monkeypatch, port)

    def _verify(handle):
        raise boom

    iface.verify = _verify
    assert iface.open() is False
    assert iface.last_open_error is boom
    assert port.written == []
    assert port.closed == 1


def test_a_refusal_is_terminal_verify_runs_exactly_once(monkeypatch):
    """Nothing may retry a port the caller has just rejected."""
    port = _Port()
    calls = []
    iface = _iface(monkeypatch, port)

    def _verify(handle):
        calls.append(1)
        return False

    iface.verify = _verify
    assert iface.open() is False
    assert len(calls) == 1, "verify ran %d times" % len(calls)


# ===================================================================== 4
# last_open_error is keyed to the ATTEMPT, not the object
# =====================================================================

def test_a_refusal_does_not_poison_a_later_successful_open(monkeypatch):
    """A stale cause must not be re-raised for an unrelated later success."""
    port1 = _Port()
    iface = _iface(monkeypatch, port1)
    iface.verify = lambda handle: False
    assert iface.open() is False
    assert iface.last_open_error is not None

    port2 = _Port()
    monkeypatch.setattr(comm_mod.serial, "Serial", lambda *a, **k: port2)
    iface.verify = lambda handle: True
    assert iface.open() is True
    assert iface.last_open_error is None, (
        "a stale refusal survived into a successful open; _connect() would "
        "raise it")


# ===================================================================== 5
# cancel beats verify, in both directions
# =====================================================================

def test_cancel_during_verify_wins_over_a_raise(monkeypatch):
    port = _Port()
    ev = threading.Event()
    iface = _iface(monkeypatch, port)
    iface.cancel_event = ev

    def _verify(handle):
        ev.set()
        raise serial.SerialException("port operation after cancellation")

    iface.verify = _verify
    assert iface.open() is False
    # No error: the caller asked for this, so it is a cancellation rather
    # than a judgement about the device.
    assert iface.last_open_error is None
    assert port.written == []
    assert port.closed == 1


def test_cancel_during_verify_wins_over_a_truthy_verdict(monkeypatch):
    """The callable may finish just as the caller gives up; a truthy verdict
    must not authorise a write past a cancellation."""
    port = _Port()
    ev = threading.Event()
    iface = _iface(monkeypatch, port)
    iface.cancel_event = ev

    def _verify(handle):
        ev.set()
        return True

    iface.verify = _verify
    assert iface.open() is False
    assert iface.last_open_error is None
    assert port.written == [], "wrote after the caller had cancelled"
    assert port.closed == 1


# ===================================================================== 6
# close(): always releases the handle, always observable
# =====================================================================

def test_a_clean_close_returns_true_and_records_nothing(monkeypatch):
    port = _Port(out_waiting=(0,))
    iface = _iface(monkeypatch, port)
    assert iface.open() is True
    assert iface.close() is True
    assert iface.last_close_error is None
    assert port.closed == 1
    assert port.flushes >= 1


def test_a_stuck_drain_is_bounded_reported_and_still_releases(monkeypatch):
    """The three things that must all hold at once."""
    port = _Port(out_waiting=(64,))        # never drains
    iface = _iface(monkeypatch, port)
    assert iface.open() is True
    iface.close_drain_timeout = 0.05       # keep the pin quick
    result = iface.close()
    assert result is False, "a failed drain reported success"
    assert isinstance(iface.last_close_error, serial.SerialTimeoutException)
    assert "64" in str(iface.last_close_error)
    assert port.closed == 1, "the handle was not released"


def test_a_port_without_out_waiting_is_reported_not_flushed_unbounded(monkeypatch):
    """An unbounded wait is the defect; silently taking it again is fail-open."""
    port = _Port(omit_out_waiting=True)
    iface = _iface(monkeypatch, port)
    assert iface.open() is True
    flushes_after_open = port.flushes
    assert iface.close() is False
    assert isinstance(iface.last_close_error, serial.SerialTimeoutException)
    assert "out_waiting" in str(iface.last_close_error)
    assert port.flushes == flushes_after_open, (
        "close() flushed a port it could not bound")
    assert port.closed == 1


def test_last_close_error_is_keyed_to_the_attempt(monkeypatch):
    """Same attempt-keyed rule as last_open_error."""
    stuck = _Port(out_waiting=(64,))
    iface = _iface(monkeypatch, stuck)
    assert iface.open() is True
    iface.close_drain_timeout = 0.05
    assert iface.close() is False
    assert iface.last_close_error is not None

    clean = _Port(out_waiting=(0,))
    monkeypatch.setattr(comm_mod.serial, "Serial", lambda *a, **k: clean)
    assert iface.open() is True
    assert iface.close() is True
    assert iface.last_close_error is None, "a stale close error survived"


def test_close_never_raises_even_when_the_drain_fails(monkeypatch):
    """A dying device must not turn a FINISHED session into a traceback."""
    port = _Port(out_waiting=(64,))
    iface = _iface(monkeypatch, port)
    assert iface.open() is True
    iface.close_drain_timeout = 0.05
    try:
        iface.close()
    except BaseException as exc:                       # noqa: BLE001
        pytest.fail("close() raised %r" % (exc,))


# ===================================================================== 7
# connect() surfaces the refusal
# =====================================================================

def test_connect_raises_the_verification_error(monkeypatch):
    from daqifi.device import NyquistDevice

    port = _Port()
    monkeypatch.setattr(comm_mod.serial, "Serial", lambda *a, **k: port)
    with pytest.raises(DeviceVerificationError):
        NyquistDevice.connect("FAKEPORT", verify=lambda handle: False)
    assert port.written == [], "connect() wrote to a device it then refused"
    assert port.closed == 1


def test_connect_re_raises_the_callables_own_exception(monkeypatch):
    from daqifi.device import NyquistDevice

    port = _Port()
    boom = RuntimeError("not my board")
    monkeypatch.setattr(comm_mod.serial, "Serial", lambda *a, **k: port)

    def _verify(handle):
        raise boom

    with pytest.raises(RuntimeError) as caught:
        NyquistDevice.connect("FAKEPORT", verify=_verify)
    assert caught.value is boom
    assert port.written == []


# ===================================================================== 8
# r1 HIGH: nothing unbounded may sit between the drain and the release
# =====================================================================

class _FlushBlocksPort(_Port):
    """out_waiting is 0, and flush() never returns.

    The FTDI/CTS case: the OS queue is empty, so a bounded out_waiting poll
    is satisfied immediately, while tcdrain() is still waiting on a hardware
    transmitter that will not drain. An earlier version of close() called
    flush() at exactly this point, on the false premise that zero pending
    bytes made it safe.
    """

    def __init__(self):
        super().__init__(out_waiting=(0,))
        self.released = threading.Event()
        self.flush_entered = threading.Event()
        self._never = threading.Event()
        # ⚠ ARMED ONLY AFTER open(). open() flushes too, inside
        # _send_disable_echo, and a fixture that blocks there hangs the test
        # in SETUP -- which is not the condition under test and which made
        # this leg time out instead of failing. The fixture must express
        # exactly one thing: a flush that blocks during CLOSE.
        self.block_on_flush = False

    def arm(self):
        self.block_on_flush = True

    def flush(self):
        self.flushes += 1
        if not self.block_on_flush:
            return
        self.flush_entered.set()
        self._never.wait()          # forever, as tcdrain would

    def close(self):
        super().close()
        self.released.set()


def test_close_returns_even_when_flush_would_block_forever(monkeypatch):
    """close() must not depend on flush() returning.

    ⛔ FAILS on 46d14335, which called flush() once out_waiting hit zero: the
    close thread never returns, the handle is never released, and
    last_close_error stays None — so close_drain_timeout never fires and the
    caller is told nothing. Driven on a thread with a join deadline so this
    leg FAILS rather than hanging the suite.
    """
    port = _FlushBlocksPort()
    iface = _iface(monkeypatch, port)
    assert iface.open() is True
    port.arm()                      # only now does flush() block
    iface.close_drain_timeout = 0.05

    result = {}

    def _closer():
        try:
            result["value"] = iface.close()
        except BaseException as exc:                   # noqa: BLE001
            result["raised"] = exc

    t = threading.Thread(target=_closer, daemon=True)
    t.start()
    t.join(5.0)

    assert not t.is_alive(), (
        "close() did not return within 5s: it is blocked in an unbounded "
        "flush() after the drain, which close_drain_timeout cannot bound")
    assert port.released.is_set(), "the handle was never released"
    assert port.closed == 1
    assert "raised" not in result, "close() raised %r" % (result.get("raised"),)
    # The documented skip is observable: the drain does not call flush at
    # all, so the blocking path is never entered.
    assert not port.flush_entered.is_set(), (
        "close() entered flush(); the drain must not call it")
    assert result["value"] is True, (
        "the OS queue was empty, so this is a clean close: %r"
        % (result.get("value"),))


def test_the_drain_never_calls_flush(monkeypatch):
    """The poll IS the drain. Pinned directly, so the claim cannot drift."""
    port = _Port(out_waiting=(32, 16, 0))
    iface = _iface(monkeypatch, port)
    assert iface.open() is True
    flushes_after_open = port.flushes      # _send_disable_echo's own flush
    assert iface.close() is True
    assert port.flushes == flushes_after_open, (
        "close() flushed; tcdrain() cannot be bounded, so it must not be "
        "called between the drain and the release")
