"""#199: smaller live-streaming defects.

1. A digital-only JSON record ({"ts":..,"di":[..]}, sent per DIO read when DIO
   streaming is on) became an empty Sample, which get_channel_data() and the
   exports filled in as a fake 0 V reading.
2. Every JSON/CSV record was printed to stdout (1 kHz -> 1000 lines/s).
3. The sample buffer grew without limit in callback and read() modes.
4. read()'s timeout was per sample (read(40, timeout=0.05) took 2 s to fail),
   and read_all() had no timeout at all on a stalled stream.
5. One malformed JSON object discarded every good object buffered behind it.
"""

import importlib.util
import math
import os
import time

import pytest

from daqifi.streaming import Sample, SampleBuffer

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "stream_fakes", os.path.join(_HERE, "test_stream_manager_startup_data.py")
)
_f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_f)

META = b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":1000}}\n'


def _ai(ts, mv):
    return b'{"ts":%d,"ai":[{"ch":0,"val":%d}]}\n' % (ts, mv)


def _di(ts):
    return b'{"ts":%d,"di":[{"ts":%d,"mask":15,"val":3}]}\n' % (ts, ts)


def _manager(data, **kwargs):
    sm = _f._manager(_f._FakeSerial(on_start=data), channels=[0], sample_rate=10, format=1, **kwargs)
    sm.start()
    return sm


# ---- 1 ---------------------------------------------------------------------


def test_a_digital_only_record_is_not_a_fake_zero_volt_sample() -> None:
    sm = _manager(META + _ai(1000, 2500) + _di(1200) + _ai(2000, 2500))
    try:
        samples = sm.read(num_samples=2, timeout=1.0)
    finally:
        sm.stop()
    assert [s.channels for s in samples] == [{0: 2.5}, {0: 2.5}]
    assert sm.buffer.get_channel_data(0) == [2.5, 2.5]


def test_a_channel_missing_from_a_sample_reads_nan() -> None:
    assert math.isnan(Sample(timestamp=0.0, channels={0: 1.0})[3])


# ---- 2 ---------------------------------------------------------------------


def test_streaming_writes_nothing_to_stdout(capsys) -> None:
    sm = _manager(META + _ai(1000, 2500) + _ai(1100, 2600))
    try:
        sm.read(num_samples=2, timeout=1.0)
    finally:
        sm.stop()
    assert capsys.readouterr().out == ""


# ---- 3 ---------------------------------------------------------------------


def test_the_stream_buffer_is_bounded_and_keeps_the_newest() -> None:
    data = META + b"".join(_ai(1000 + i, i) for i in range(60))
    sm = _manager(data, buffer_size=10)
    try:
        sm.read(num_samples=60, timeout=2.0)
    finally:
        sm.stop()
    assert 10 <= len(sm.buffer) <= 11
    assert sm.buffer[-1].channels == {0: 0.059}


def test_a_sample_buffer_trims_in_chunks_and_keeps_the_newest() -> None:
    buf = SampleBuffer(channels=[0], max_samples=100)
    for i in range(1000):
        buf.add_sample(Sample(timestamp=float(i), channels={0: float(i)}))
        assert len(buf) <= 110
    assert buf[-1].timestamp == 999.0
    assert buf.start_time == 0.0  # timestamps stay relative to the session start


def test_buffer_size_none_keeps_everything() -> None:
    buf = SampleBuffer(channels=[0], max_samples=None)
    for i in range(500):
        buf.add_sample(Sample(timestamp=float(i), channels={0: 0.0}))
    assert len(buf) == 500


# ---- 4 ---------------------------------------------------------------------


def test_read_timeout_is_a_total_not_per_sample() -> None:
    sm = _manager(META)  # silent after the header
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            sm.read(num_samples=40, timeout=0.05)
        assert time.monotonic() - started < 0.5
    finally:
        sm.stop()


def test_read_all_returns_after_its_duration_on_a_stalled_stream() -> None:
    sm = _manager(META)
    try:
        started = time.monotonic()
        buffer = sm.read_all(duration=0.2)
        assert time.monotonic() - started < 1.0
        assert len(buffer) == 0
    finally:
        sm.stop()


def test_read_all_returns_the_samples_it_read() -> None:
    sm = _manager(META + _ai(1000, 1000) + _ai(1100, 2000))
    try:
        buffer = sm.read_all(duration=0.3)
    finally:
        sm.stop()
    assert [s.channels for s in buffer] == [{0: 1.0}, {0: 2.0}]


# ---- 5 ---------------------------------------------------------------------


def test_a_malformed_record_doesnt_discard_the_good_ones_behind_it() -> None:
    bad = b'{"ts":1050,"ai":[{"ch":0,"val":}]}\n'
    sm = _manager(META + _ai(1000, 1000) + bad + _ai(1100, 2000) + _ai(1200, 3000))
    try:
        samples = sm.read(num_samples=3, timeout=1.0)
    finally:
        sm.stop()
    assert [s.channels for s in samples] == [{0: 1.0}, {0: 2.0}, {0: 3.0}]


# ---- round 2 ------------------------------------------------------------


def test_a_queued_backlog_is_read_within_the_default_deadline() -> None:
    # 3000 records already waiting at 10 kHz: the default deadline (2x their
    # duration + 1 s = 1.6 s) must cover taking them, so read() mustn't
    # sleep between samples that are already there.
    data = META + b"".join(_ai(1000 + i, 1000) for i in range(3000))
    sm = _f._manager(_f._FakeSerial(on_start=data), channels=[0], sample_rate=10000, format=1)
    sm.start()
    try:
        samples = sm.read(num_samples=3000)
    finally:
        sm.stop()
    assert len(samples) == 3000


def test_the_async_read_passes_its_timeout_through() -> None:
    import asyncio

    from daqifi.streaming import AsyncStreamManager

    stream = AsyncStreamManager.__new__(AsyncStreamManager)
    stream._stream = _manager(META)  # silent after the header
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            asyncio.run(stream.read(num_samples=40, timeout=0.05))
        assert time.monotonic() - started < 0.5
    finally:
        stream._stream.stop()


def test_an_export_never_pairs_a_value_with_another_samples_timestamp() -> None:
    # A producer adds (and trims) while to_csv runs: every row's value must
    # be its own timestamp. Each sample's value equals its timestamp.
    import csv
    import sys
    import tempfile
    import threading

    buf = SampleBuffer(channels=[0], max_samples=200)
    for i in range(220):
        buf.add_sample(Sample(timestamp=float(i), channels={0: float(i)}))
    stop = threading.Event()

    def producer():
        i = 220
        while not stop.is_set():
            buf.add_sample(Sample(timestamp=float(i), channels={0: float(i)}))
            i += 1

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    bad = []
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.csv")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not bad:
                buf.to_csv(path)
                with open(path) as f:
                    rows = list(csv.reader(f))[1:]
                bad = [r for r in rows if float(r[0]) != float(r[1])]
    finally:
        stop.set()
        thread.join(2)
        sys.setswitchinterval(old)
    assert bad == []


def test_read_all_keeps_channels_a_csv_header_names_later() -> None:
    data = (b"# Timestamp Tick Rate: 1000 Hz\n" b"ain3_ts,ain3_val\n"
            b"1000,2500\n" b"1100,2600\n")
    sm = _f._manager(_f._FakeSerial(on_start=data), channels=[0], sample_rate=10, format=2)
    sm.start()
    try:
        buffer = sm.read_all(duration=0.3)
    finally:
        sm.stop()
    assert buffer.channels == [3]
    assert buffer.to_dict()["channel_3"] == [2.5, 2.6]


def test_to_json_writes_a_missing_value_as_null_and_stays_strict_json(tmp_path) -> None:
    import json

    buf = SampleBuffer(channels=[0, 1])
    buf.add_sample(Sample(timestamp=0.0, channels={0: 1.0, 1: 2.0}))
    buf.add_sample(Sample(timestamp=0.1, channels={0: 1.5}))  # no channel 1
    path = tmp_path / "x.json"
    buf.to_json(str(path))

    def _reject(constant):
        raise ValueError(f"non-standard JSON constant {constant}")

    data = json.loads(path.read_text(), parse_constant=_reject)
    assert data["channel_1"] == [2.0, None]
    assert math.isnan(buf.to_dict()["channel_1"][1])  # numeric access keeps NaN
