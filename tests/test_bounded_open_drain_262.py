"""#262: open()'s echo-disable drain is bounded, and a drain that fails the
bound fails the open.

open() writes ``SYSTem:ECHO -1`` and then called ``serial.flush()``. On POSIX
that is ``tcdrain()``, which takes no timeout; on an adapter whose transmitter
has stopped (FTDI with CTS deasserted) it never returns. The
``except SerialException`` around it could not help, because a blocked call
raises nothing. connect() simply hung.

The fix reuses #259's bounded ``out_waiting`` poll (``_drain_bounded``) and
calls no ``flush()``. A drain that does not finish within
``open_drain_timeout`` releases the port and fails the open with a
``SerialPortConnectError``. That is deliberately unlike the tolerated "the
echo-disable write was lost" case: a port that is not taking bytes would stall
every later command the same way.

The fixtures are #259's ``_Port`` and ``_FlushBlocksPort``, imported rather
than copied so the two tickets pin the same surface. Every pin here is
device-free.
"""

import errno
import threading

import pytest
import serial

from daqifi._internal import comm as comm_mod
from daqifi.exceptions import SerialPortConnectError
from tests.test_connect_verify_and_bounded_close_259 import (
    _FlushBlocksPort,
    _iface,
    _Port,
)

ECHO_OFF = b"SYSTem:ECHO -1\r\n"


def _open_on_a_thread(iface, join_s=5.0):
    """Run open() where a hang FAILS the test instead of hanging the suite."""
    result = {}

    def _opener():
        try:
            result["value"] = iface.open()
        except BaseException as exc:                   # noqa: BLE001
            result["raised"] = exc

    t = threading.Thread(target=_opener, daemon=True)
    t.start()
    t.join(join_s)
    return t, result


# ===================================================================== 1
# The defect: open() must not depend on flush() returning
# =====================================================================

def test_open_returns_even_when_flush_would_block_forever():
    """⛔ FAILS on 01343229: open() enters flush(), which blocks as tcdrain()
    would, and never returns. Driven on a thread with a join deadline so the
    leg FAILS rather than hanging the suite.

    The OS queue reads 0 throughout, so this is a HEALTHY open: it must
    return True, without ever entering flush().
    """
    port = _FlushBlocksPort()
    port.arm()                       # flush() blocks from the first call
    # Not via _iface/monkeypatch: the thread may outlive this test on the
    # failing leg, and an undone monkeypatch would leave it a real Serial.
    original = comm_mod.serial.Serial
    comm_mod.serial.Serial = lambda *a, **k: port
    try:
        iface = comm_mod.SerialInterface("FAKEPORT")
        t, result = _open_on_a_thread(iface)
    finally:
        comm_mod.serial.Serial = original

    assert not t.is_alive(), (
        "open() did not return within 5s: it is blocked in an unbounded "
        "flush() after writing the echo-disable")
    assert not port.flush_entered.is_set(), "open() entered flush()"
    assert "raised" not in result, "open() raised %r" % (result.get("raised"),)
    assert result["value"] is True
    assert port.written == [ECHO_OFF]
    assert port.closed == 0, "a healthy open released its own port"


def test_a_healthy_drain_opens_without_flushing(monkeypatch):
    """Bytes that leave the OS queue within the bound are a normal open."""
    port = _Port(out_waiting=(16, 8, 0))
    iface = _iface(monkeypatch, port)
    assert iface.open() is True
    assert port.written == [ECHO_OFF]
    assert port.flushes == 0
    assert iface.last_open_error is None
    assert port.closed == 0


# ===================================================================== 2
# A drain that misses the bound fails the open and releases the port
# =====================================================================

def test_a_stuck_drain_fails_the_open_and_releases_the_port(monkeypatch):
    """⛔ FAILS on 01343229, where the fake's flush() returns and open()
    reports True on a port that never drained."""
    port = _Port(out_waiting=(64,))          # never drains
    iface = _iface(monkeypatch, port)
    iface.open_drain_timeout = 0.05
    assert iface.open() is False, "open() continued on a port not draining"
    assert port.closed == 1, "the handle was not released"
    assert iface.is_open() is False
    err = iface.last_open_error
    assert isinstance(err, SerialPortConnectError)
    assert isinstance(err.__cause__, serial.SerialTimeoutException)
    assert "64" in str(err), "the error does not say what was stuck"
    assert port.flushes == 0



class _DiesDuringTheDrain(_Port):
    """The echo-disable write lands, then the device goes away: the
    out_waiting poll raises, as a POSIX ioctl on a disconnected USB device
    does (EIO), or as pyserial wraps it."""

    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    @property
    def out_waiting(self):
        if self.written:
            raise self._exc
        return 0


@pytest.mark.parametrize("exc", [
    OSError(errno.EIO, "Input/output error"),
    serial.SerialException("device disconnected"),
], ids=["OSError-EIO", "SerialException"])
def test_a_drain_that_errors_fails_the_open_and_releases_the_port(
        monkeypatch, exc):
    """⛔ FAILS on d484fb22 (#262 r1): its handler wrapped only the timeout
    and a cancel, so an I/O error from the poll fell through to open()'s
    TOLERANT write handler, and open() returned True on a dead port with
    last_open_error unset and the handle still held. Any drain failure is
    the drain not finishing; only the write is tolerated."""
    port = _DiesDuringTheDrain(exc)
    iface = _iface(monkeypatch, port)
    assert iface.open() is False, "open() succeeded on a port that died"
    assert port.written == [ECHO_OFF]
    assert port.closed == 1, "the handle was not released"
    assert iface.is_open() is False
    err = iface.last_open_error
    assert isinstance(err, SerialPortConnectError), repr(err)
    assert err.__cause__ is exc, "the original error is not chained"
    assert port.flushes == 0

def test_a_port_without_out_waiting_fails_the_open(monkeypatch):
    """The pinned choice for a port that cannot be bounded at all.

    Its drain cannot be bounded, and falling back to an unbounded flush()
    would be the hang this ticket removes. So the open is refused, the port
    is released, and the reason names out_waiting.

    Who can reach it (pyserial 3.5): open() constructs ``serial.Serial``,
    which is ``serialposix`` or ``serialwin32`` on CPython, and both define
    out_waiting. The backends without it (``serialjava``, ``serialcli`` for
    Jython/IronPython, and the rfc2217:// and socket:// URL handlers) are not
    reachable here: the first two are not CPython, and URL handlers need
    ``serial_for_url``, which open() never calls.
    """
    port = _Port(omit_out_waiting=True)
    iface = _iface(monkeypatch, port)
    assert iface.open() is False
    assert port.closed == 1
    assert port.flushes == 0, "open() flushed a port it could not bound"
    err = iface.last_open_error
    assert isinstance(err, SerialPortConnectError)
    assert "out_waiting" in str(err)


def test_a_failed_drain_does_not_poison_a_later_open(monkeypatch):
    """last_open_error is keyed to the attempt."""
    stuck = _Port(out_waiting=(64,))
    iface = _iface(monkeypatch, stuck)
    iface.open_drain_timeout = 0.05
    assert iface.open() is False

    healthy = _Port()
    monkeypatch.setattr(comm_mod.serial, "Serial", lambda *a, **k: healthy)
    assert iface.open() is True
    assert iface.last_open_error is None


def test_cancel_during_a_stuck_drain_is_a_cancellation_not_an_error(monkeypatch):
    """The caller gave up while the drain was timing out: report it as the
    cancel branch does, with no error, and release the port."""
    cancel = threading.Event()

    class _CancelWhileStuck(_Port):
        def __getattr__(self, name):
            if name == "out_waiting":
                cancel.set()
                # Served from the raw port: the wrapper already checked.
                return 64
            return super().__getattr__(name)

    port = _CancelWhileStuck()
    iface = _iface(monkeypatch, port)
    iface.cancel_event = cancel
    iface.open_drain_timeout = 0.05
    assert iface.open() is False
    assert iface.last_open_error is None
    assert port.closed == 1


# ===================================================================== 3
# What is NOT changed
# =====================================================================

def test_a_failed_echo_disable_WRITE_is_still_tolerated(monkeypatch):
    """Only the drain became fatal. A write that times out is the existing
    "echo-disable was lost" case, which the response-stripping fallbacks
    cover, so the open still succeeds."""

    class _WriteTimesOut(_Port):
        def write(self, data):
            raise serial.SerialTimeoutException("Write timeout")

    port = _WriteTimesOut()
    iface = _iface(monkeypatch, port)
    assert iface.open() is True
    assert port.closed == 0


def test_the_default_bound_is_one_second_and_per_instance(monkeypatch):
    assert comm_mod.OPEN_DRAIN_TIMEOUT_S == 1.0
    iface = _iface(monkeypatch, _Port())
    assert iface.open_drain_timeout == 1.0
    iface.open_drain_timeout = 0.25
    assert comm_mod.SerialInterface("OTHER").open_drain_timeout == 1.0


def test_the_default_bound_is_actually_applied(monkeypatch):
    """A stuck drain at the default gives up after ~1 s, not 20 s (close()'s
    bound) and not never. Timed loosely so a slow CI host cannot flake it."""
    import time

    port = _Port(out_waiting=(64,))
    iface = _iface(monkeypatch, port)
    t0 = time.monotonic()
    assert iface.open() is False
    elapsed = time.monotonic() - t0
    assert 0.9 <= elapsed < 5.0, elapsed


# ===================================================================== 4
# connect() surfaces it
# =====================================================================

def test_connect_raises_when_the_open_drain_is_stuck(monkeypatch):
    from daqifi.device import NyquistDevice

    port = _Port(out_waiting=(64,))
    monkeypatch.setattr(comm_mod.serial, "Serial", lambda *a, **k: port)
    # raising=False: so that on a base without the name, this test reaches
    # connect() and fails on behaviour, not on the patch.
    monkeypatch.setattr(comm_mod, "OPEN_DRAIN_TIMEOUT_S", 0.05,
                        raising=False)
    with pytest.raises(SerialPortConnectError) as caught:
        NyquistDevice.connect("FAKEPORT")
    assert "64" in str(caught.value)
    assert port.written == [ECHO_OFF], "connect() wrote past the failed open"
    assert port.closed == 1
