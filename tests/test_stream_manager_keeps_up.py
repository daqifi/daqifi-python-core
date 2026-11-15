"""#192: StreamManager keeps up with a board at its configured rate.

The callback thread slept a whole sample period after every sample, and
read() slept 1 ms after every sample, so neither could keep up with a board
producing ``sample_rate`` records a second: the backlog of records produced
but not delivered grew for the whole session (~70/s at 1 kHz). Readers now
wait only when a poll found nothing.
"""

import contextlib
import io
import threading
import time

import pytest

from daqifi._internal.comm import SerialInterface
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.streaming import StreamManager

RATE = 1000
# Generous: a healthy reader holds the backlog near zero; the old pacing
# reached ~570 within the first second and grew from there.
MAX_BACKLOG = 150


class _RateSerial:
    """A board that, after START, produces one record per 1/rate s of wall
    time."""
    out_waiting = 0  # #263: the send path drains this queue, bounded

    def __init__(self, record, rate, on_start=b""):
        self.is_open = True
        self.rx = bytearray()
        self.lock = threading.Lock()
        self.record = record
        self.rate = rate
        self.on_start = on_start
        self.t0 = None
        self.produced = 0

    def _pump(self):
        if self.t0 is None:
            return
        due = int((time.monotonic() - self.t0) * self.rate)
        while self.produced < due:
            self.rx += self.record(self.produced)
            self.produced += 1

    def write(self, data):
        command = data.decode().strip()
        with self.lock:
            if command == "ENABle:VOLTage:DC?":
                self.rx += b"1\r\n"
            elif command.startswith("SYSTem:ERRor?"):
                self.rx += b'0,"No error"\r\n'
            elif "STREAM:START" in command.upper():
                self.rx += self.on_start
                self.t0 = time.monotonic()
            elif "STREAM:STOP" in command.upper():
                self._pump()
                self.t0 = None
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        with self.lock:
            self._pump()
            return len(self.rx)

    def read(self, n):
        with self.lock:
            out = bytes(self.rx[:n])
            del self.rx[:n]
            return out

    def close(self):
        self.is_open = False


def _pb(i):
    body = DaqifiOutMessage(msg_time_stamp=i + 1, analog_in_data_float=[1.0]).SerializeToString()
    return bytes([len(body)]) + body


def _json(i):
    return b'{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % (i + 1)


_FORMATS = {
    "protobuf": (0, _pb, b""),
    "json": (1, _json, b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":1000}}\n'),
}


def _manager(fake, fmt):
    comm = SerialInterface("TEST-NOT-A-PORT", timeout=0.3)
    comm._serial = fake
    return StreamManager(comm, channels=[0], sample_rate=RATE, format=fmt)


def _backlogs(fake, delivered):
    out = []
    for _ in range(3):
        time.sleep(1.0)
        fake.in_waiting  # bring the board up to now
        out.append(fake.produced - delivered())
    return out


@pytest.mark.parametrize("name", list(_FORMATS))
def test_the_callback_thread_keeps_up(name) -> None:
    fmt, record, header = _FORMATS[name]
    fake = _RateSerial(record, RATE, on_start=header)
    sm = _manager(fake, fmt)
    got = []
    with contextlib.redirect_stdout(io.StringIO()):
        sm.start(callback=got.append)
        try:
            backlogs = _backlogs(fake, lambda: len(got))
        finally:
            sm.stop()
    assert max(backlogs) < MAX_BACKLOG, backlogs


@pytest.mark.parametrize("name", list(_FORMATS))
def test_blocking_reads_keep_up(name) -> None:
    fmt, record, header = _FORMATS[name]
    fake = _RateSerial(record, RATE, on_start=header)
    sm = _manager(fake, fmt)
    count = [0]
    stop = threading.Event()

    def _reader():
        while not stop.is_set():
            count[0] += len(sm.read(num_samples=10, timeout=1.0))

    with contextlib.redirect_stdout(io.StringIO()):
        sm.start()
        thread = threading.Thread(target=_reader)
        thread.start()
        try:
            backlogs = _backlogs(fake, lambda: count[0])
        finally:
            stop.set()
            thread.join(timeout=5)
            sm.stop()
    assert max(backlogs) < MAX_BACKLOG, backlogs
