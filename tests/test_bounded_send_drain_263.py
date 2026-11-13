"""#263: every serial send drains BOUNDED, and none calls ``flush()``.

#259 bounded close() and #262 bounded open(). Six more send paths still did
``write(...)`` and then a bare ``flush()``: send_command, query_binary,
sd_get_file, sd_get_file_binary, streaming's START, and DaqifiDevice's send
when its transport stream is a serial port. pyserial 3.5's ``flush()`` is
``while self.out_waiting: time.sleep(0.05)`` on Windows and
``termios.tcdrain()`` on POSIX: no deadline either way, and write_timeout
governs neither. A port that stopped taking bytes hung the caller forever,
with nothing raised.

All six now call ``drain_output_bounded()``, the poll #259 introduced, and
nothing else.

Each site has two legs, both red at e3cfbbac:

* **flush blocks forever, queue empty** (the tcdrain / FTDI-CTS case): the
  call must finish, and must never enter ``flush()``. At e3cfbbac it enters
  ``flush()`` and hangs. The call runs on a thread with a join deadline, so
  that leg FAILS rather than hanging the suite.
* **queue never drains**: the call must raise within its bound. At e3cfbbac
  the fake's ``flush()`` is serialwin32's own loop, so it hangs too.

There is also a source pin: no ``.flush()`` under ``daqifi/`` other than the
named non-serial receivers.
"""

import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import serial

import daqifi.devices as devices_mod
from daqifi._internal.comm import SerialInterface
from daqifi._internal.scpi_commands import reboot_device
from daqifi.device import NyquistDevice
from daqifi.devices import DaqifiDevice
from daqifi.exceptions import CommandError
from daqifi.streaming import StreamManager
from tests.test_stream_manager_startup_data import _FakeSerial as _StreamSerial

BOUND_S = 0.3     # the send drain bound, shrunk so the stuck leg is quick
JOIN_S = 5.0      # a call still running after this is a hang


class _Wedge:
    """The two ways a port stops draining, armed by the first write that
    matches ``arm_on`` (None = the first write).

    ``mode="flush_blocks"``: out_waiting reads 0 and flush() never returns,
        which is tcdrain() waiting on a transmitter that will not drain.
    ``mode="stuck"``: out_waiting stays nonzero, and flush() is
        serialwin32's own body, so an unbounded flush() hangs on it too.

    out_waiting is a class-level property, as on pyserial's Serial, because
    DaqifiDevice detects a serial stream by the TYPE having one.
    """

    def _wedge_init(self, mode, arm_on=None):
        self.mode = mode
        self.arm_on = arm_on
        self.armed = False
        self.flush_entered = threading.Event()
        self._never = threading.Event()

    def _maybe_arm(self, data):
        if self.arm_on is None or self.arm_on(bytes(data)):
            self.armed = True

    @property
    def out_waiting(self):
        return 64 if (self.armed and self.mode == "stuck") else 0

    def flush(self):
        self.flush_entered.set()
        if not self.armed:
            return
        if self.mode == "flush_blocks":
            self._never.wait()
        while self.out_waiting:          # serialwin32 Serial.flush
            self._never.wait(0.05)

    def release(self):
        """Let a hung thread go once its verdict is recorded."""
        self.armed = False
        self._never.set()


class _Port(_Wedge):
    """A plain port: replies with nothing."""

    def __init__(self, mode, arm_on=None):
        self._wedge_init(mode, arm_on)
        self.is_open = True
        self.timeout = 0.05
        self.written = []

    def write(self, data):
        self.written.append(bytes(data))
        self._maybe_arm(data)
        return len(data)

    @property
    def in_waiting(self):
        return 0

    def read(self, n=1):
        return b""

    def reset_input_buffer(self):
        pass


class _StartPort(_Wedge, _StreamSerial):
    """startup_data's scripted streaming port, wedged at the START write."""

    def __init__(self, mode):
        _StreamSerial.__init__(self)
        self._wedge_init(mode, arm_on=lambda b: b"STREAM:START" in b.upper())

    def write(self, data):
        n = _StreamSerial.write(self, data)
        self._maybe_arm(data)
        return n


def _run(fn, port):
    """-> (finished, result or exception). Releases a hung call afterwards."""
    box = {}

    def target():
        try:
            box["r"] = fn()
        except BaseException as exc:                    # noqa: BLE001
            box["r"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(JOIN_S)
    finished = not t.is_alive()
    if not finished:
        port.release()
        t.join(2.0)
    return finished, box.get("r")


# ----------------------------------------------------------------- the sites

def _iface(port):
    iface = SerialInterface("FAKEPORT", timeout=0.05)
    iface._serial = port
    iface.send_drain_timeout = BOUND_S
    return iface


def _nyquist(port):
    device = NyquistDevice("COM41")           # constructing opens no port
    device._comm._serial = port
    device._comm.send_drain_timeout = BOUND_S
    return device


class _Transport:
    """DaqifiDevice's transport, whose stream IS the serial port, as
    SerialStreamTransport.stream is."""

    def __init__(self, port):
        self.stream = port
        self._connected = False

    def add_status_changed(self, h):
        pass

    def remove_status_changed(self, h):
        pass

    def connect(self, retry_options=None):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def close(self):
        self._connected = False

    @property
    def is_connected(self):
        return self._connected


def _send_command(port):
    return lambda: _iface(port).send_command("*IDN?", delay=0)


def _query_binary(port):
    return lambda: _iface(port).query_binary("SYSTem:SYSInfoPB?", timeout=0.1)


def _sd_get_file(port):
    return lambda: _nyquist(port).sd_get_file("x.csv", timeout=0.2)


def _sd_get_file_binary(port):
    return lambda: _nyquist(port).sd_get_file_binary("x.bin", timeout=0.2)


def _stream_start(port):
    def go():
        comm = SerialInterface("FAKEPORT", timeout=0.3)
        comm._serial = port
        comm.send_drain_timeout = BOUND_S
        sm = StreamManager(comm, channels=[0], sample_rate=10, format=2)
        try:
            sm.start()
        finally:
            try:
                sm.stop()
            except Exception:                           # noqa: BLE001
                pass
    return go


def _device_send(port, monkeypatch):
    # raising=False: e3cfbbac has no such name, and the leg must reach its
    # real send there rather than fail on the patch.
    monkeypatch.setattr(devices_mod, "SEND_DRAIN_TIMEOUT_S", BOUND_S,
                        raising=False)

    def go():
        d = DaqifiDevice(name="x", transport=_Transport(port))
        d.connect()
        try:
            d.send(reboot_device())
        finally:
            d.disconnect()
    return go


# (id, port factory, call factory, exception a stuck queue must raise)
SITES = [
    ("send_command", _Port, _send_command, RuntimeError),
    ("query_binary", _Port, _query_binary, RuntimeError),
    ("sd_get_file", _Port, _sd_get_file, serial.SerialTimeoutException),
    ("sd_get_file_binary", _Port, _sd_get_file_binary,
     serial.SerialTimeoutException),
    ("stream_start", _StartPort, _stream_start, CommandError),
    ("device_send", _Port, _device_send, Exception),
]
IDS = [s[0] for s in SITES]


def _call(make_call, port, monkeypatch):
    if make_call is _device_send:
        return make_call(port, monkeypatch)
    return make_call(port)


@pytest.mark.parametrize("name,make_port,make_call,stuck_raises", SITES,
                         ids=IDS)
def test_a_send_never_enters_flush(name, make_port, make_call, stuck_raises,
                                   monkeypatch):
    """flush() would block forever and the queue is empty. The call must
    finish, whatever it returns, and never enter flush().

    ⛔ FAILS on e3cfbbac: every one of these sites called flush() after its
    write, and hangs here.
    """
    port = make_port("flush_blocks")
    finished, r = _run(_call(make_call, port, monkeypatch), port)
    assert finished, f"{name}: still blocked after {JOIN_S}s (in flush())"
    assert not port.flush_entered.is_set(), f"{name}: called flush()"


@pytest.mark.parametrize("name,make_port,make_call,stuck_raises", SITES,
                         ids=IDS)
def test_a_send_on_a_port_that_never_drains_raises(name, make_port, make_call,
                                                   stuck_raises, monkeypatch):
    """The OS output queue never empties. The call must raise within its
    bound, and still without entering flush().

    ⛔ FAILS on e3cfbbac: flush() is serialwin32's loop here, which never
    exits.
    """
    port = make_port("stuck")
    finished, r = _run(_call(make_call, port, monkeypatch), port)
    assert finished, f"{name}: still blocked after {JOIN_S}s"
    assert isinstance(r, stuck_raises), f"{name}: returned {r!r}"
    assert "output buffer still holds" in str(r), f"{name}: {r!r}"
    assert not port.flush_entered.is_set(), f"{name}: called flush()"


def test_device_send_on_a_non_serial_stream_still_flushes(monkeypatch):
    """Positive control for the type check: a stream without out_waiting
    (TCP's _SocketStream) keeps its own flush()."""
    flushed = []

    class _Sock:
        def write(self, data):
            return len(data)

        def flush(self):
            flushed.append(True)

        def read(self, n=1):
            return b""

    d = DaqifiDevice(name="x", transport=_Transport(_Sock()))
    d.connect()
    try:
        d.send(reboot_device())
    finally:
        d.disconnect()
    assert flushed


# ------------------------------------------------------------ the source pin

#: The .flush() calls allowed under daqifi/, by file and receiver. None is a
#: serial port: an SD preamble parser, a guarded non-serial transport stream,
#: a download file, and a subprocess's stdin.
ALLOWED_FLUSHES = sorted([
    ("daqifi/devices.py", "stream"),
    ("daqifi/firmware/external_process.py", "stdin"),
    ("daqifi/firmware/external_process.py", "stdin"),
    ("daqifi/firmware/github_download_service.py", "fp"),
    ("daqifi/sdcard.py", "skip"),
    ("daqifi/sdcard.py", "skip"),
])


def _flush_calls(root):
    found = []
    for path in sorted(Path(root, "daqifi").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "flush" and not node.args):
                found.append((path.relative_to(root).as_posix(),
                              ast.unparse(node.func.value)))
    return sorted(found)


def test_no_serial_flush_remains_in_the_library():
    """⛔ FAILS on e3cfbbac, which also lists self._serial, ser, port and
    self._comm._serial. A new .flush() anywhere under daqifi/ fails here
    until it is either bounded or named above with its reason."""
    root = Path(__file__).resolve().parents[1]
    assert _flush_calls(root) == ALLOWED_FLUSHES


def test_the_source_pin_sees_a_serial_flush(tmp_path):
    """Positive control: the scanner reports a serial flush."""
    pkg = tmp_path / "daqifi"
    pkg.mkdir()
    (pkg / "x.py").write_text("def f(self):\n    self._serial.flush()\n")
    assert _flush_calls(tmp_path) == [("daqifi/x.py", "self._serial")]
