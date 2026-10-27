"""#162 items 2-4: StreamManager.stop() must always stop the device and must
never leave two acquisition threads, or deadlock when called from a callback.

The board is an in-memory pyserial stand-in driven by the real
SerialInterface; it answers the SCPI the manager sends and, once started,
emits one JSON sample per poll."""

import itertools
import threading
import time

import pytest

from daqifi._internal.comm import SerialInterface
from daqifi.streaming import StreamManager, StreamState


class _FakeSerial:
    def __init__(self, enabled_mask=1):
        self.is_open = True
        self.rx = bytearray()
        self.lock = threading.Lock()
        self.writes = []
        self.enabled_mask = enabled_mask
        self.streaming = False
        self._ts = itertools.count(1)

    def write(self, data):
        command = data.decode().strip()
        self.writes.append(command)
        with self.lock:
            if command == "ENABle:VOLTage:DC?":
                self.rx += f"{self.enabled_mask}\r\n".encode()
            elif command.startswith("SYSTem:ERRor?"):
                self.rx += b'0,"No error"\r\n'
            elif "STREAM:START" in command.upper():
                self.streaming = True
            elif "STOP" in command.upper():
                self.streaming = False
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        with self.lock:
            if self.streaming:
                self.rx += ('{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % next(self._ts)).encode()
            return len(self.rx)

    def read(self, n):
        with self.lock:
            out = bytes(self.rx[:n])
            del self.rx[:n]
            return out

    def close(self):
        self.is_open = False


def _manager():
    fake = _FakeSerial()
    comm = SerialInterface("TEST-NOT-A-PORT", timeout=0.3)
    comm._serial = fake
    return StreamManager(comm, channels=[0], sample_rate=100, format=1), fake


def _stops(fake):
    return sum("STOP" in w.upper() for w in fake.writes)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_stop_after_a_callback_error_still_stops_the_device() -> None:
    # Item 2: the error moved the state to ERROR and stop() returned early.
    stream, fake = _manager()

    def boom(sample):
        raise ValueError("boom")

    stream.start(callback=boom)
    _wait(lambda: stream.state == StreamState.ERROR)
    stream.stop()
    assert _stops(fake) == 1 and not fake.streaming
    assert stream.state == StreamState.STOPPED


def test_a_worker_outliving_stop_blocks_a_second_one() -> None:
    # Item 3: stop() dropped a thread still alive after its 2 s join, so a
    # restart ran two readers on one port.
    stream, fake = _manager()
    release = threading.Event()
    in_callback = threading.Event()

    def slow(sample):
        in_callback.set()
        release.wait(10)

    stream.start(callback=slow)
    assert in_callback.wait(5)
    stream.stop()  # returns after its 2 s join; the worker is still in the callback
    assert not fake.streaming
    with pytest.raises(RuntimeError, match="previous streaming thread"):
        stream.start(callback=slow)
    old_worker = stream._thread
    release.set()
    _wait(lambda: not old_worker.is_alive())
    stream.start(callback=lambda sample: None)  # allowed once the old worker is gone
    assert stream._thread is not old_worker
    stream.stop()


def test_stop_from_inside_the_callback_stops_the_device() -> None:
    # Item 4: stop() joined its own thread ("cannot join current thread"),
    # the state went to ERROR and STOP was never sent.
    stream, fake = _manager()
    stream.start(callback=lambda sample: stream.stop())
    _wait(lambda: _stops(fake) >= 1)
    _wait(lambda: not stream._thread or not stream._thread.is_alive())
    assert not fake.streaming
    assert stream.state == StreamState.STOPPED
    assert _stops(fake) == 1


def test_stop_sends_stop_when_the_worker_could_not_be_started(monkeypatch) -> None:
    # Audit finding on #178 (round 2): Thread.start() failing (thread quota)
    # after START reached the board left an unstarted thread behind, and
    # stop()'s join raised "cannot join thread before it is started"
    # before STOP was sent, on every attempt.
    stream, fake = _manager()

    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    with pytest.raises(Exception):
        stream.start(callback=lambda sample: None)
    monkeypatch.undo()
    assert fake.streaming  # START reached the board
    stream.stop()
    assert _stops(fake) == 1 and not fake.streaming


def test_a_restart_from_the_first_callback_cannot_add_a_second_worker() -> None:
    # Audit finding on #178 (round 3): the worker reference was published
    # after worker.start(), so a first callback calling stop() then start()
    # could see no worker and launch a second one. The reference is now
    # published first; the restart is refused while the worker (the
    # caller itself) is alive.
    stream, fake = _manager()
    outcome = []

    def first(sample):
        if outcome:
            return
        stream.stop()
        try:
            stream.start(callback=lambda s: None)
            outcome.append("restarted")
        except RuntimeError as exc:
            outcome.append(str(exc))

    stream.start(callback=first)
    _wait(lambda: outcome)
    assert "previous streaming thread" in outcome[0]
    _wait(lambda: not stream._thread or not stream._thread.is_alive())
    assert _stops(fake) == 1 and not fake.streaming


def test_the_worker_reference_is_published_before_the_worker_runs(monkeypatch) -> None:
    stream, fake = _manager()
    seen = []
    real_start = threading.Thread.start

    def checking_start(thread):
        seen.append(stream._thread is thread)
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", checking_start)
    stream.start(callback=lambda sample: None)
    monkeypatch.undo()
    stream.stop()
    assert seen and seen[0] is True
