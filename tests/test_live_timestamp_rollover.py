"""#193: live timestamps across the device's 32-bit tick rollover.

StreamManager converted each record's raw tick straight to seconds, so every
2^32 ticks (about 86 s at 50 MHz, 102 s at 42 MHz) timestamps jumped back to
near zero, in all three formats. Ticks are now unwrapped per session.
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


def _run(fmt, on_start, n, tick_hz=None, host_times=None):
    sm = _f._manager(_f._FakeSerial(on_start=on_start), channels=[0], sample_rate=10, format=fmt)
    if tick_hz is not None:
        sm._tick_rate_hz = tick_hz
    if host_times is not None:
        # When each sample was received on the host (one value per sample).
        times = iter(host_times)
        sm._host_clock = lambda: next(times)
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


# Audit r1 on #213: a gap longer than half the counter period.

def _json(ticks):
    meta = b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":%d}}\n' % TICK_HZ
    return meta + b"".join(b'{"ts":%d,"ai":[{"ch":0,"val":1000}]}\n' % t for t in ticks)


def _csv(ticks):
    header = b"# Timestamp Tick Rate: %d Hz\nain0_ts,ain0_val\n" % TICK_HZ
    return header + b"".join(b"%d,1000\n" % t for t in ticks)


def _pb(ticks):
    return b"".join(_f._frame(msg_time_stamp=t, analog_in_data_float=[1.0]) for t in ticks)


_FORMATS = {
    "json": (1, _json, None),
    "csv": (2, _csv, None),
    "protobuf": (0, _pb, TICK_HZ),
}


@pytest.mark.parametrize("name", list(_FORMATS))
def test_a_50_second_gap_is_a_50_second_step(name) -> None:
    # The signed-distance rule read a forward gap over half a period
    # (42.9 s at 50 MHz) as a step back: [1.0, -34.9, -33.9].
    fmt, build, tick_hz = _FORMATS[name]
    ticks = [50_000_000, 100_000_000, 2_600_000_000, 2_650_000_000]
    _, samples = _run(fmt, build(ticks), 4, tick_hz=tick_hz, host_times=[0.0, 1.0, 51.0, 52.0])
    assert _steps(samples) == [1.0, 50.0, 1.0]


@pytest.mark.parametrize("name", list(_FORMATS))
def test_a_gap_of_several_periods_is_counted(name) -> None:
    fmt, build, tick_hz = _FORMATS[name]
    gap_ticks = 200 * TICK_HZ  # 200 s: more than two whole periods
    ticks = [50_000_000, (50_000_000 + gap_ticks) % WRAP]
    _, samples = _run(fmt, build(ticks), 2, tick_hz=tick_hz, host_times=[0.0, 200.2])
    assert _steps(samples) == [200.0]


def test_an_older_tick_received_later_still_steps_back() -> None:
    ticks = [100_000_000, 150_000_000, 125_000_000]
    _, samples = _run(1, _json(ticks), 3, host_times=[0.0, 1.0, 1.01])
    assert _steps(samples) == [1.0, -0.5]
