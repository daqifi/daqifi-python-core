"""#222: live JSON/CSV streaming read firmware volts as millivolts.

The firmware writes each analog value as volts with VoltagePrecision decimals
(JSON_Encoder.c: "{\"ch\":%u, \"val\":%.*f}", precision 4 by default; CSV the
same through its fixed-point formatter); integers only at precision 0
(millivolts) or in raw mode (ADC codes). The live JSON parser divided every
value by 1000 (2.5 V read as 0.0025 V), and the live CSV parser int()'d it
(a decimal dropped the channel). Both now follow the SD-log parsers' rule
(_FirmwareValueReader): decimals are volts; integers follow integer_units.
"""

import importlib.util
import os
import warnings

import pytest

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "stream_fakes", os.path.join(_HERE, "test_stream_manager_startup_data.py")
)
_f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_f)

META = b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":1000}}\n'
CSV_HEAD = b"# Timestamp Tick Rate: 1000 Hz\nain0_ts,ain0_val\n"


def _read(data, fmt=1, n=1, **kwargs):
    sm = _f._manager(_f._FakeSerial(on_start=data), channels=[0], sample_rate=10, format=fmt, **kwargs)
    sm.start()
    try:
        return sm.read(num_samples=n, timeout=1.0)
    finally:
        sm.stop()


def test_json_decimal_values_are_volts() -> None:
    (sample,) = _read(META + b'{"ts":1000,"ai":[{"ch":0, "val":2.5000}]}\n')
    assert sample.channels == {0: 2.5}
    assert sample.units == "volts"


def test_csv_decimal_values_are_volts() -> None:
    (sample,) = _read(CSV_HEAD + b"1000,2.5000\n", fmt=2)
    assert sample.channels == {0: 2.5}
    assert sample.units == "volts"


def test_json_integer_millivolts_when_said() -> None:
    (sample,) = _read(META + b'{"ts":1000,"ai":[{"ch":0, "val":2500}]}\n', integer_units="millivolts")
    assert sample.channels == {0: 2.5}
    assert sample.units == "millivolts"


def test_csv_integer_millivolts_when_said() -> None:
    (sample,) = _read(CSV_HEAD + b"1000,2500\n", fmt=2, integer_units="millivolts")
    assert sample.channels == {0: 2.5}


def test_unspecified_integers_are_raw_codes_and_warn_once() -> None:
    data = META + b'{"ts":1000,"ai":[{"ch":0, "val":2048}]}\n{"ts":1100,"ai":[{"ch":0, "val":2049}]}\n'
    with pytest.warns(UserWarning, match="integer_units") as caught:
        samples = _read(data, n=2)
    assert [s.channels for s in samples] == [{0: 2048.0}, {0: 2049.0}]
    assert {s.units for s in samples} == {"raw-assumed"}
    assert len([w for w in caught if "integer_units" in str(w.message)]) == 1


def test_raw_integers_when_said_dont_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        (sample,) = _read(META + b'{"ts":1000,"ai":[{"ch":0, "val":2048}]}\n', integer_units="raw")
    assert sample.channels == {0: 2048.0} and sample.units == "raw"


def test_a_record_mixing_decimals_and_integers_is_skipped() -> None:
    data = (META + b'{"ts":1000,"ai":[{"ch":0, "val":2.5000},{"ch":1, "val":3}]}\n'
            b'{"ts":1100,"ai":[{"ch":0, "val":2.6000}]}\n')
    (sample,) = _read(data)
    assert sample.channels == {0: 2.6}


def test_integer_units_is_validated() -> None:
    from daqifi.streaming import StreamManager

    with pytest.raises(ValueError, match="integer_units"):
        StreamManager(_f._FakeSerial(), channels=[0], integer_units="volts")


def test_the_async_manager_passes_integer_units_through() -> None:
    from daqifi.streaming import AsyncStreamManager

    stream = AsyncStreamManager(_f._FakeSerial(), channels=[0], integer_units="millivolts")
    assert stream._stream._integer_units == "millivolts"
