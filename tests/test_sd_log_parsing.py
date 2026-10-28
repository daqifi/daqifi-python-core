"""#163 items 1 and 8: SD log parsing.

Item 1: consecutive frames with the same timestamp were merged even when
each carried its own analog data, keeping only the first one's values.
Item 8: CSV calibration was looked up by column position, not by the ainN
channel the column names (the firmware writes only enabled channels)."""

import io

import pytest
from google.protobuf.internal.encoder import _VarintBytes

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.sdcard import (
    SdCardCsvFileParser,
    SdCardDeviceConfiguration,
    SdCardFileParser,
    SdCardParseOptions,
)


def _wire(*messages) -> bytes:
    out = b""
    for m in messages:
        body = m.SerializeToString()
        out += _VarintBytes(len(body)) + body
    return out


def _status():
    return DaqifiOutMessage(timestamp_freq=1000, device_sn=1234567890, analog_in_port_num=2)


def test_two_analog_frames_at_one_timestamp_are_two_samples() -> None:
    wire = _wire(
        _status(),
        DaqifiOutMessage(msg_time_stamp=123, analog_in_data_float=[1.0]),
        DaqifiOutMessage(msg_time_stamp=123, analog_in_data_float=[2.0]),
    )
    samples = list(SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin").samples)
    assert [s.analog_values for s in samples] == [(1.0,), (2.0,)]


def test_complementary_analog_and_digital_frames_still_merge() -> None:
    wire = _wire(
        _status(),
        DaqifiOutMessage(msg_time_stamp=123, analog_in_data_float=[1.0]),
        DaqifiOutMessage(msg_time_stamp=123, digital_data=b"\x05"),
    )
    samples = list(SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin").samples)
    assert len(samples) == 1
    assert samples[0].analog_values == (1.0,)
    assert samples[0].digital_data == 5


def test_csv_calibration_follows_the_named_channel() -> None:
    # Only ain7 enabled; channel 7 has cal_m = 2. Column position 0 used
    # channel 0's (1.0, 0.0) and gave 50.0 instead of 100.0.
    csv = (
        "# Device: Nyquist NQ1\n"
        "# Timestamp Tick Rate: 1000 Hz\n"
        "ain7_ts,ain7_val\n"
        "1000,50\n"
    ).encode()
    calibration = tuple((2.0 if ch == 7 else 1.0, 0.0) for ch in range(8))
    options = SdCardParseOptions(
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=8,
            digital_port_count=0,
            timestamp_frequency=1000,
            calibration_values=calibration,
            resolution=100,
            port_range=(100.0,) * 8,
            internal_scale_m=(1.0,) * 8,
        )
    )
    session = SdCardCsvFileParser().parse_stream(io.BytesIO(csv), "log.csv", options)
    sample = list(session.samples)[0]
    assert sample.analog_values == pytest.approx((100.0,))
