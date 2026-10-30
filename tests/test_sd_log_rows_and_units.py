"""#194 items 1-5 and 7: SD log parsing.

1. A CSV row with any empty ``,,`` pair was dropped whole. The firmware
   writes an empty pair for a channel with no conversion that period
   (``validMask`` bit clear, ``csv_encoder.c``) and for every analog pair of
   a DIO-only row, so those rows' valid values were lost without a warning.
2. Protobuf ``.bin`` logs returned raw ADC codes with ``analog_units=None``
   and no warning; the log carries no resolution, range or calibration
   (``fields_sd_metadata``), so nothing said ``configuration_override`` was
   needed for volts.
3. Protobuf calibration and range were applied by packed position, not
   channel: the firmware packs only enabled channels (``NanoPB_Encoder.c``)
   and the log has no channel list, so the caller must be able to give one.
4. Parsed samples didn't say which channel each value is.
5. ``cal_b`` was multiplied by ``internal_scale_m``. The firmware adds it
   after scaling (``MC12bADC.c``, ``AD7609Scale.h``), as upstream does.
7. A stray ``ch`` line before the CSV column header hid the header (upstream
   9aa83f5).
"""

import io
import warnings

import pytest
from google.protobuf.internal.encoder import _VarintBytes

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.sdcard import (
    SdCardCsvFileParser,
    SdCardDeviceConfiguration,
    SdCardFileParser,
    SdCardJsonFileParser,
    SdCardParseOptions,
)


def _csv(data: bytes, options=None):
    return SdCardCsvFileParser().parse_stream(io.BytesIO(data), "log.csv", options)


def _bin(data: bytes, options=None):
    return SdCardFileParser().parse_stream(io.BytesIO(data), "log.bin", options)


def _wire(*messages) -> bytes:
    out = b""
    for m in messages:
        body = m.SerializeToString()
        out += _VarintBytes(len(body)) + body
    return out


def _status():
    return DaqifiOutMessage(timestamp_freq=1000, device_sn=1234567890, analog_in_port_num=8)


def _config(**fields):
    base = dict(analog_port_count=8, digital_port_count=1, timestamp_frequency=1000)
    base.update(fields)
    return SdCardDeviceConfiguration(**base)


# ---- 1: empty CSV pairs -----------------------------------------------------

_MISSES_CSV = (
    b"# Timestamp Tick Rate: 1000 Hz\n"
    b"ain0_ts,ain0_val,ain1_ts,ain1_val,dio_ts,dio_val\n"
    b"1000,0.5000,1000,0.6000,1000,3\n"
    b"2000,0.5100,,,2000,1\n"
    b",,,,3000,7\n"
    b"4000,0.5300,4000,0.6300,,\n"
)


def test_a_row_with_an_empty_pair_keeps_its_other_values() -> None:
    samples = list(_csv(_MISSES_CSV).samples)
    assert [s.analog_values for s in samples] == [(0.5, 0.6), (0.51,), (), (0.53, 0.63)]
    assert [s.analog_channels for s in samples] == [(0, 1), (0,), (), (0, 1)]
    assert [s.digital_data for s in samples] == [3, 1, 7, 0]


def test_a_dio_only_row_keeps_its_time() -> None:
    samples = list(_csv(_MISSES_CSV).samples)
    seconds = [(s.timestamp - samples[0].timestamp).total_seconds() for s in samples]
    assert seconds == [0.0, 1.0, 2.0, 3.0]


def test_a_csv_without_a_column_header_names_no_channels() -> None:
    (sample,) = _csv(b"1000,0.5,1000,0.6\n").samples
    assert sample.analog_values == (0.5, 0.6)
    assert sample.analog_channels is None


def test_a_row_with_nothing_in_it_is_still_skipped() -> None:
    data = b"ain0_ts,ain0_val,dio_ts,dio_val\n1000,0.5,1000,1\n,,,\n2000,0.6,2000,0\n"
    assert [s.analog_values for s in _csv(data).samples] == [(0.5,), (0.6,)]


def test_a_half_empty_pair_is_still_malformed() -> None:
    data = b"ain0_ts,ain0_val,ain1_ts,ain1_val\n1000,0.5,1000,\n2000,0.6,2000,0.7\n"
    assert [s.analog_values for s in _csv(data).samples] == [(0.6, 0.7)]


def test_a_missing_channel_doesnt_shift_calibration_onto_the_next() -> None:
    # Raw codes, sparse header (ain3, ain7); only ch7 is calibrated x2.
    cal = tuple((2.0, 0.0) if ch == 7 else (1.0, 0.0) for ch in range(8))
    options = SdCardParseOptions(
        integer_units="raw",
        configuration_override=_config(resolution=4096, port_range=(5.0,) * 8, calibration_values=cal),
    )
    data = b"ain3_ts,ain3_val,ain7_ts,ain7_val\n,,1000,2048\n"
    (sample,) = _csv(data, options).samples
    assert sample.analog_channels == (7,)
    assert sample.analog_values == pytest.approx((5.0,))


# ---- 2, 3, 4: protobuf units and channels -----------------------------------


def test_unscaled_protobuf_raw_codes_are_marked_and_warn() -> None:
    data = _wire(_status(), DaqifiOutMessage(msg_time_stamp=1, analog_in_data=[2048]))
    with pytest.warns(UserWarning, match="configuration_override"):
        (sample,) = _bin(data).samples
    assert sample.analog_values == (2048.0,)
    assert sample.analog_units == "raw"


def test_scaled_protobuf_raw_codes_dont_warn() -> None:
    data = _wire(_status(), DaqifiOutMessage(msg_time_stamp=1, analog_in_data=[2048]))
    options = SdCardParseOptions(configuration_override=_config(resolution=4096, port_range=(5.0,) * 8))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        (sample,) = _bin(data, options).samples
    assert sample.analog_values == pytest.approx((2.5,))
    assert sample.analog_units == "raw"


def test_protobuf_float_values_are_volts() -> None:
    data = _wire(_status(), DaqifiOutMessage(msg_time_stamp=1, analog_in_data_float=[1.25]))
    (sample,) = _bin(data).samples
    assert sample.analog_units == "volts"


def test_protobuf_calibration_follows_the_given_channels() -> None:
    # Only ch5 enabled, packed first; ch5 is calibrated x2, the rest x1.
    cal = tuple((2.0, 0.0) if ch == 5 else (1.0, 0.0) for ch in range(8))
    options = SdCardParseOptions(
        analog_channels=[5],
        configuration_override=_config(resolution=4096, port_range=(5.0,) * 8, calibration_values=cal),
    )
    data = _wire(_status(), DaqifiOutMessage(msg_time_stamp=1, analog_in_data=[2048]))
    (sample,) = _bin(data, options).samples
    assert sample.analog_values == pytest.approx((5.0,))
    assert sample.analog_channels == (5,)


def test_protobuf_samples_without_given_channels_name_none() -> None:
    data = _wire(_status(), DaqifiOutMessage(msg_time_stamp=1, analog_in_data_float=[1.0, 2.0]))
    (sample,) = _bin(data).samples
    assert sample.analog_channels is None


def test_a_channel_list_of_the_wrong_length_warns_and_is_not_applied() -> None:
    options = SdCardParseOptions(analog_channels=[1, 2, 3])
    data = _wire(_status(), DaqifiOutMessage(msg_time_stamp=1, analog_in_data_float=[1.0, 2.0]))
    with pytest.warns(UserWarning, match="analog_channels"):
        (sample,) = _bin(data, options).samples
    assert sample.analog_channels is None


def test_json_samples_name_their_channels() -> None:
    data = (
        b'{"meta":{"dev":"Nyquist 1","sn":"0","tick_hz":1000}}\n'
        b'{"ts":1000,"ai":[{"ch":3,"val":0.5},{"ch":7,"val":0.25}]}\n'
    )
    (sample,) = SdCardJsonFileParser().parse_stream(io.BytesIO(data), "log.json").samples
    assert sample.analog_channels == (3, 7)


# ---- 5: cal_b is an offset in volts -----------------------------------------


def test_the_calibration_offset_is_not_scaled_by_the_internal_scale() -> None:
    options = SdCardParseOptions(
        integer_units="raw",
        configuration_override=_config(
            resolution=4096, port_range=(5.0,) * 8,
            calibration_values=((1.0, 0.5),) * 8, internal_scale_m=(2.0,) * 8,
        ),
    )
    (sample,) = _csv(b"ain0_ts,ain0_val\n1000,2048\n", options).samples
    # 2048 / 4096 * 5 * 1 * 2 + 0.5, not (2.5 + 0.5) * 2
    assert sample.analog_values == pytest.approx((5.5,))


# ---- 7: a stray "ch" line ---------------------------------------------------


def test_a_stray_ch_line_doesnt_hide_the_column_header() -> None:
    data = (
        b"# Timestamp Tick Rate: 1000 Hz\n"
        b"ch\n"
        b"ain2_ts,ain2_val,ain5_ts,ain5_val\n"
        b"1000,0.5,1000,0.6\n"
        b"2000,0.7,2000,0.8\n"
    )
    session = _csv(data)
    samples = list(session.samples)
    assert session.device_config.analog_port_count == 2
    assert [s.analog_channels for s in samples] == [(2, 5), (2, 5)]
    assert [s.analog_values for s in samples] == [(0.5, 0.6), (0.7, 0.8)]
