"""#193: live timestamps across the device's 32-bit tick rollover.

StreamManager converted each record's raw tick straight to seconds, so every
2^32 ticks (about 86 s at 50 MHz, 102 s at 42 MHz) timestamps jumped back to
near zero, in all three formats. Ticks are now unwrapped per session by the
nearest step. Documented limit: a gap in the device's own samples over half a
period (42.9 s at 50 MHz) reads as a step back.
"""

import importlib.util
import os

import pytest

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "stream_fakes", os.path.join(_HERE, "test_stream_manager_startup_data.py")
)
_f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_f)

TICK_HZ = 50_000_000
WRAP = 2 ** 32
# Four samples one second apart, straddling a rollover.
TICKS = [WRAP - 75_000_000, WRAP - 25_000_000, 25_000_000, 75_000_000]


def _run(fmt, on_start, n, tick_hz=None):
    sm = _f._manager(_f._FakeSerial(on_start=on_start), channels=[0], sample_rate=10, format=fmt)
    if tick_hz is not None:
        sm._tick_rate_hz = tick_hz
    sm.start()
    try:
        return sm, sm.read(num_samples=n, timeout=1.0)
    finally:
        sm.stop()


def _steps(samples):
    stamps = [s.timestamp for s in samples]
    return [round(b - a, 6) for a, b in zip(stamps, stamps[1:])]


def test_json_timestamps_keep_rising_across_a_rollover() -> None:
    meta = b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":%d}}\n' % TICK_HZ
    records = b"".join(b'{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % t for t in TICKS)
    sm, samples = _run(1, meta + records, 4)
    assert _steps(samples) == [1.0, 1.0, 1.0]
    assert sm.buffer.get_timestamps() == pytest.approx([0.0, 1.0, 2.0, 3.0])


def test_csv_timestamps_keep_rising_across_a_rollover() -> None:
    header = b"# Timestamp Tick Rate: %d Hz\nain0_ts,ain0_val\n" % TICK_HZ
    rows = b"".join(b"%d,1000\n" % t for t in TICKS)
    _, samples = _run(2, header + rows, 4)
    assert _steps(samples) == [1.0, 1.0, 1.0]


def test_protobuf_timestamps_keep_rising_across_a_rollover() -> None:
    frames = b"".join(
        _f._frame(msg_time_stamp=t, analog_in_data_float=[1.0]) for t in TICKS
    )
    _, samples = _run(0, frames, 4, tick_hz=TICK_HZ)
    assert _steps(samples) == [1.0, 1.0, 1.0]


def test_a_slightly_older_tick_steps_back_rather_than_forward_a_whole_period() -> None:
    meta = b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":%d}}\n' % TICK_HZ
    ticks = [100_000_000, 150_000_000, 125_000_000]  # a queued older sample
    records = b"".join(b'{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % t for t in ticks)
    _, samples = _run(1, meta + records, 3)
    assert _steps(samples) == [1.0, -0.5]


def test_the_first_sample_of_a_session_keeps_its_raw_time() -> None:
    meta = b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":%d}}\n' % TICK_HZ
    _, samples = _run(1, meta + b'{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % (5 * TICK_HZ), 1)
    assert samples[0].timestamp == pytest.approx(5.0)


def test_a_new_session_unwraps_from_its_own_first_tick() -> None:
    meta = b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":%d}}\n' % TICK_HZ
    fake = _f._FakeSerial(
        on_start=meta + b"".join(b'{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % t for t in TICKS)
    )
    sm = _f._manager(fake, channels=[0], sample_rate=10, format=1)
    sm.start()
    try:
        sm.read(num_samples=4, timeout=1.0)
    finally:
        sm.stop()
    # After a board restart the counter starts again near zero.
    fake.on_start = meta + b'{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % (10 * TICK_HZ)
    sm.start()
    try:
        samples = sm.read(num_samples=1, timeout=1.0)
    finally:
        sm.stop()
    assert samples[0].timestamp == pytest.approx(10.0)


# Audit r2 on #213: a consumer that pauses between read() calls. (The host
# time that audit judged was dropped in r3; the nearest-step rule needs no
# clock, and this pins that a pause can't turn into a whole period.)

class _Clock:
    """Host time that moves on by ``step`` at every port poll, as it does
    while a reader drains the port continuously."""

    def __init__(self, step=0.25):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


class _ScheduledSerial(_f._FakeSerial):
    """Releases each (host_time, bytes) once the host clock reaches it."""
    out_waiting = 0  # #263: the send path drains this queue, bounded

    def __init__(self, clock, header, schedule):
        super().__init__(on_start=header)
        self.clock = clock
        self.schedule = list(schedule)
        self.started_at = None  # host time of START; arrivals count from it

    def write(self, data):
        n = super().write(data)
        if "STREAM:START" in data.decode().strip().upper():
            self.started_at = self.clock.now
        return n

    @property
    def in_waiting(self):
        with self.lock:
            while (
                self.started_at is not None
                and self.schedule
                and self.started_at + self.schedule[0][0] <= self.clock.now
            ):
                self.rx += self.schedule.pop(0)[1]
            return len(self.rx)


def _json_parts(ticks):
    meta = b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":%d}}\n' % TICK_HZ
    return meta, [b'{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % t for t in ticks]


def _csv_parts(ticks):
    header = b"# Timestamp Tick Rate: %d Hz\nain0_ts,ain0_val\n" % TICK_HZ
    return header, [b"%d,1000\n" % t for t in ticks]


def _pb_parts(ticks):
    return b"", [_f._frame(msg_time_stamp=t, analog_in_data_float=[1.0]) for t in ticks]


_FORMATS = {
    "json": (1, _json_parts, None),
    "csv": (2, _csv_parts, None),
    "protobuf": (0, _pb_parts, TICK_HZ),
}


def _scheduled(name, ticks, arrivals, clock):
    fmt, parts, tick_hz = _FORMATS[name]
    header, records = parts(ticks)
    fake = _ScheduledSerial(clock, header, zip(arrivals, records))
    sm = _f._manager(fake, channels=[0], sample_rate=10, format=fmt)
    if tick_hz is not None:
        sm._tick_rate_hz = tick_hz
    return sm


@pytest.mark.parametrize("name", list(_FORMATS))
def test_a_paused_consumer_does_not_turn_buffered_records_into_a_gap(name) -> None:
    # Audit r2: host time measured at parse turned a 50 s pause between
    # read() calls into a whole extra period (+85.9 s). Records already
    # waiting are 1 s apart, and the poll after the pause isn't trusted.
    clock = _Clock(step=0.001)
    ticks = [WRAP - 75_000_000, WRAP - 25_000_000, 25_000_000, 75_000_000]
    sm = _scheduled(name, ticks, [0.0, 0.0, 0.0, 0.0], clock)
    sm.start()
    try:
        first = sm.read(num_samples=2, timeout=5.0)
        clock.now += 50.0  # the consumer is away; nothing polls the port
        second = sm.read(num_samples=2, timeout=5.0)
    finally:
        sm.stop()
    assert _steps(first + second) == [1.0, 1.0, 1.0]
