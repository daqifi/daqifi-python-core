"""B15 — ``daqifi.streaming.SampleBuffer`` slice.

SampleBuffer is a pure-data container — every method operates on
in-memory state with no I/O. So this slice is unit-testable, no
device required. The COVERAGE.md row carries the ``[~]`` marker.

Covers:

* ``add_sample`` — appends a Sample, stamps ``start_time`` on the
  first add.
* ``clear`` — empties the buffer and resets ``start_time``.
* ``__len__`` / ``__iter__`` / ``__getitem__`` — sequence
  semantics.
* ``get_channel_data`` — pulls per-channel value series.
* ``get_timestamps`` — returns timestamps offset to ``start_time``;
  empty list when the buffer is empty.
* ``to_dict`` — round-trippable dict shape.
* ``to_csv`` — CSV export with + without header.
* ``to_json`` — JSON export round-trips through ``json.load``.
* ``to_dataframe`` — pandas DataFrame export (only when pandas is
  importable; skipped otherwise).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from daqifi.streaming import Sample, SampleBuffer


def _make_sample(t: float, **channels: float) -> Sample:
    """Construct a Sample with channel ints derived from kwargs."""
    return Sample(timestamp=t, channels={int(k): v for k, v in channels.items()})


# ---- add_sample / start_time stamping -------------------------------------


def test_add_sample_appends_to_samples_list() -> None:
    """Each ``add_sample`` call appends to the samples list."""
    buf = SampleBuffer(channels=[0])
    buf.add_sample(_make_sample(1.0, **{"0": 0.5}))
    buf.add_sample(_make_sample(2.0, **{"0": 0.7}))
    assert len(buf.samples) == 2


def test_add_sample_stamps_start_time_on_first_add() -> None:
    """First ``add_sample`` sets ``start_time`` to that sample's
    timestamp; subsequent adds don't move it."""
    buf = SampleBuffer(channels=[0])
    assert buf.start_time is None
    buf.add_sample(_make_sample(7.5, **{"0": 0.1}))
    assert buf.start_time == 7.5
    buf.add_sample(_make_sample(7.6, **{"0": 0.2}))
    assert buf.start_time == 7.5


# ---- clear ---------------------------------------------------------------


def test_clear_empties_samples_and_resets_start_time() -> None:
    """``clear`` empties samples and resets ``start_time`` to None."""
    buf = SampleBuffer(channels=[0])
    buf.add_sample(_make_sample(1.0, **{"0": 0.5}))
    buf.clear()
    assert buf.samples == []
    assert buf.start_time is None


# ---- Sequence protocol ---------------------------------------------------


def test_len_reflects_sample_count() -> None:
    """``len(buf)`` reflects how many samples have been added."""
    buf = SampleBuffer(channels=[0])
    assert len(buf) == 0
    for i in range(5):
        buf.add_sample(_make_sample(float(i), **{"0": 0.0}))
    assert len(buf) == 5


def test_iter_yields_samples_in_order() -> None:
    """Iterating the buffer yields samples in insertion order."""
    buf = SampleBuffer(channels=[0])
    expected = [_make_sample(float(i), **{"0": float(i)}) for i in range(3)]
    for s in expected:
        buf.add_sample(s)
    assert list(buf) == expected


def test_getitem_indexes_into_samples() -> None:
    """``buf[i]`` returns the i-th sample."""
    buf = SampleBuffer(channels=[0])
    s0 = _make_sample(1.0, **{"0": 0.5})
    s1 = _make_sample(2.0, **{"0": 0.7})
    buf.add_sample(s0)
    buf.add_sample(s1)
    assert buf[0] is s0
    assert buf[1] is s1


# ---- get_channel_data / get_timestamps -----------------------------------


def test_get_channel_data_returns_per_channel_series() -> None:
    """``get_channel_data(ch)`` returns the value series for that
    channel across all samples in insertion order."""
    buf = SampleBuffer(channels=[0, 1])
    buf.add_sample(_make_sample(1.0, **{"0": 0.1, "1": 0.5}))
    buf.add_sample(_make_sample(2.0, **{"0": 0.2, "1": 0.6}))
    buf.add_sample(_make_sample(3.0, **{"0": 0.3, "1": 0.7}))

    assert buf.get_channel_data(0) == [0.1, 0.2, 0.3]
    assert buf.get_channel_data(1) == [0.5, 0.6, 0.7]


def test_get_channel_data_returns_nan_for_missing_channel() -> None:
    """A channel that doesn't appear in a sample reads NaN, not a fake
    0 V (#199): Sample.__getitem__ returns NaN for an absent channel."""
    import math

    buf = SampleBuffer(channels=[0, 9])
    buf.add_sample(_make_sample(1.0, **{"0": 0.1}))  # no channel 9
    (value,) = buf.get_channel_data(9)
    assert math.isnan(value)


def test_get_timestamps_returns_offset_from_start() -> None:
    """``get_timestamps`` returns timestamps offset to
    ``start_time`` (the timestamp of the first added sample)."""
    buf = SampleBuffer(channels=[0])
    buf.add_sample(_make_sample(10.0, **{"0": 0.0}))
    buf.add_sample(_make_sample(10.5, **{"0": 0.0}))
    buf.add_sample(_make_sample(11.0, **{"0": 0.0}))
    assert buf.get_timestamps() == [0.0, 0.5, 1.0]


def test_get_timestamps_returns_empty_when_buffer_empty() -> None:
    """``get_timestamps`` on an empty buffer returns ``[]`` (not
    a None propagation error from the missing ``start_time``)."""
    buf = SampleBuffer(channels=[0])
    assert buf.get_timestamps() == []


# ---- to_dict (covered alongside to_csv / to_json / to_dataframe) ---------


def test_to_dict_round_trip_shape() -> None:
    """``to_dict`` returns a dict with `timestamps`, `sample_rate`,
    `num_samples`, and one `channel_<i>` key per channel in
    ``self.channels``."""
    buf = SampleBuffer(channels=[0, 2], sample_rate=100.0)
    buf.add_sample(_make_sample(1.0, **{"0": 0.1, "2": 0.5}))
    buf.add_sample(_make_sample(1.5, **{"0": 0.2, "2": 0.6}))

    d = buf.to_dict()
    assert d["timestamps"] == [0.0, 0.5]
    assert d["sample_rate"] == 100.0
    assert d["num_samples"] == 2
    assert d["channel_0"] == [0.1, 0.2]
    assert d["channel_2"] == [0.5, 0.6]


# ---- to_csv --------------------------------------------------------------


def test_to_csv_writes_header_and_rows(tmp_path: Path) -> None:
    """``to_csv`` writes a header row followed by one row per
    sample, with channel columns ordered by channel index."""
    buf = SampleBuffer(channels=[2, 0])  # deliberately unordered
    buf.add_sample(_make_sample(1.0, **{"0": 0.1, "2": 0.5}))
    buf.add_sample(_make_sample(2.0, **{"0": 0.2, "2": 0.6}))

    out = tmp_path / "data.csv"
    buf.to_csv(str(out))

    rows = list(csv.reader(out.open()))
    # Channels are sorted in the header.
    assert rows[0] == ["timestamp", "channel_0", "channel_2"]
    # First data row: timestamp offset 0.0, then ch0=0.1, ch2=0.5.
    assert rows[1] == ["0.0", "0.1", "0.5"]
    assert rows[2] == ["1.0", "0.2", "0.6"]


def test_to_csv_omits_header_when_requested(tmp_path: Path) -> None:
    """``to_csv(include_header=False)`` skips the header row."""
    buf = SampleBuffer(channels=[0])
    buf.add_sample(_make_sample(1.0, **{"0": 0.5}))

    out = tmp_path / "data.csv"
    buf.to_csv(str(out), include_header=False)

    rows = list(csv.reader(out.open()))
    assert len(rows) == 1
    assert rows[0] == ["0.0", "0.5"]


# ---- to_json -------------------------------------------------------------


def test_to_json_round_trip(tmp_path: Path) -> None:
    """``to_json`` writes a JSON file whose content matches
    ``to_dict`` after a load round-trip."""
    buf = SampleBuffer(channels=[0, 1], sample_rate=200.0)
    buf.add_sample(_make_sample(1.0, **{"0": 0.1, "1": 0.5}))

    out = tmp_path / "data.json"
    buf.to_json(str(out))

    loaded = json.loads(out.read_text())
    assert loaded == buf.to_dict()


# ---- to_dataframe (skipped when pandas not installed) --------------------


def test_to_dataframe_returns_dataframe_when_pandas_available() -> None:
    """``to_dataframe`` returns a pandas.DataFrame whose columns
    include `timestamps` and one per channel. Skipped if pandas
    isn't installed (production declares pandas optional)."""
    pd = pytest.importorskip("pandas")
    buf = SampleBuffer(channels=[0], sample_rate=100.0)
    buf.add_sample(_make_sample(1.0, **{"0": 0.5}))
    buf.add_sample(_make_sample(2.0, **{"0": 0.7}))

    df = buf.to_dataframe()
    assert isinstance(df, pd.DataFrame)
    assert "timestamps" in df.columns
    assert "channel_0" in df.columns
    assert list(df["channel_0"]) == [0.5, 0.7]
