"""Hardware-test utilities ported from daqifi-python-test-suite.

These two classes are extracted (not vendored) from
``/daqifi-python-test-suite/test_harness.py``. The originals are proven
in production (26+ scripts) and capture two non-obvious requirements:

1. **FastReader** — drains the serial port at 1 ms intervals. USB CDC
   streaming requires the host to read at >=1 ms intervals; slow polling
   (e.g. 500 ms) lets the firmware-side USB circular buffer fill, which
   triggers packet drops *on the firmware*. A/B-tested: slow reader =
   1.1 MB drops, fast reader = 0 drops.

2. **StreamingMeasurement** — opens a PC-controlled measurement window
   that excludes SCPI-command latency, so reported throughput reflects
   actual streaming, not setup-and-teardown overhead.

Kept intentionally small + dependency-free so this module can be
imported by any ``tests/integration/hardware/test_*.py`` without
pulling the broader test suite into the python-core repo.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class FastReader:
    """Background thread that continuously drains a serial stream.

    Mirrors `daqifi-python-test-suite.test_harness.FastReader` —
    1 ms poll interval, daemon thread, idempotent start/stop.

    Usage::

        reader = FastReader(serial_obj)
        reader.start()
        # ... streaming happens, reader.bytes_received increments ...
        reader.stop()

    The serial object must expose ``in_waiting`` and ``read(n)``.
    """

    POLL_INTERVAL_S = 0.001

    def __init__(self, serial_obj):
        self._serial = serial_obj
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.bytes_received = 0
        self._on_chunk: Optional[Callable[[bytes], None]] = None

    def start(self, on_chunk: Optional[Callable[[bytes], None]] = None) -> None:
        if self._running:
            return
        self._running = True
        self.bytes_received = 0
        self._on_chunk = on_chunk
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="FastReader")
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            # Only drop the reference if the thread actually stopped —
            # if join() timed out, keeping the handle lets a caller
            # inspect is_alive() / retry the join instead of leaking
            # the thread silently.
            if not self._thread.is_alive():
                self._thread = None

    def _loop(self) -> None:
        while self._running:
            try:
                n = self._serial.in_waiting
                if n > 0:
                    data = self._serial.read(n)
                    self.bytes_received += len(data)
                    if self._on_chunk is not None:
                        try:
                            self._on_chunk(bytes(data))
                        except Exception:
                            # Reader thread must not die on callback errors.
                            pass
                else:
                    time.sleep(self.POLL_INTERVAL_S)
            except Exception:
                # Hardware errors are surfaced by the caller observing
                # bytes_received plateau; don't crash the daemon.
                time.sleep(0.01)


class StreamingMeasurement:
    """PC-controlled measurement window that excludes SCPI latency.

    Mirrors `daqifi-python-test-suite.test_harness.StreamingMeasurement`.

    The pattern:

    1. Caller sends ``SYST:StartStreamData <freq>`` (outside the window)
    2. Caller calls ``open_window()`` — PC clock starts
    3. Caller polls ``record_chunk(data)`` for each batch of bytes read
       (or attaches via ``FastReader(on_chunk=measurement.record_chunk)``)
    4. After ``duration`` seconds the caller calls ``close_window()``
    5. ``pc_kbps`` / ``pc_sps`` reflect actual throughput during the window

    Sample counting is opt-in: pass ``count_pb_frames=True`` to count
    protobuf length-delimited frames, otherwise only byte counts are
    populated.
    """

    def __init__(self, count_pb_frames: bool = True):
        self._count_pb = count_pb_frames
        self._measuring = False
        self._lock = threading.Lock()
        self._leftover = bytearray()

        # Public counters
        self.bytes_total = 0
        self.bytes_window = 0
        self.frames_window = 0
        self.window_started_at: Optional[float] = None
        self.window_ended_at: Optional[float] = None
        self.first_byte_at: Optional[float] = None

    @property
    def window_duration(self) -> float:
        if self.window_started_at is None or self.window_ended_at is None:
            return 0.0
        return self.window_ended_at - self.window_started_at

    @property
    def pc_kbps(self) -> float:
        d = self.window_duration
        return (self.bytes_window / d / 1024.0) if d > 0 else 0.0

    @property
    def pc_sps(self) -> float:
        d = self.window_duration
        return (self.frames_window / d) if d > 0 else 0.0

    def open_window(self) -> None:
        with self._lock:
            self.window_started_at = time.monotonic()
            self._measuring = True

    def close_window(self) -> None:
        with self._lock:
            self.window_ended_at = time.monotonic()
            self._measuring = False

    def record_chunk(self, data: bytes) -> None:
        """Hand-off from the reader. Thread-safe."""
        with self._lock:
            self.bytes_total += len(data)
            if self.first_byte_at is None and len(data) > 0:
                self.first_byte_at = time.monotonic()
            if self._measuring:
                self.bytes_window += len(data)
                if self._count_pb:
                    self._leftover.extend(data)
                    frames, leftover = _count_pb_frames(bytes(self._leftover))
                    self.frames_window += frames
                    self._leftover = bytearray(leftover)


def _count_pb_frames(buf: bytes) -> tuple[int, bytes]:
    """Count complete protobuf length-delimited frames in ``buf``.

    Returns ``(frame_count, leftover_bytes)``. Leftover is the trailing
    partial-frame bytes that the next call should prepend.

    Mirrors `StreamingMeasurement._count_pb_frames` from the test suite.
    """
    frames = 0
    i = 0
    n = len(buf)
    while i < n:
        length = 0
        shift = 0
        varint_start = i
        varint_bytes = 0
        while i < n:
            b = buf[i]
            i += 1
            varint_bytes += 1
            if varint_bytes > 10:
                # Corrupt varint (protobuf spec max is 10 bytes for uint64).
                return frames, buf[varint_start:]
            length |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        else:
            # Ran out of bytes mid-varint.
            return frames, buf[varint_start:]
        if length == 0:
            continue
        if i + length > n:
            return frames, buf[varint_start:]
        i += length
        frames += 1
    return frames, b""
