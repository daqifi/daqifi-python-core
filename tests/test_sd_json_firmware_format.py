"""#163: the SD JSON parser must read what the firmware writes.

The firmware (JSON_Encoder.c) writes a {"meta":...} header object, then one
sample object per period spread over several lines, with
"ai":[{"ch":n,"val":v},...]. The parser expected one object per line with an
"analog" key, so a real log parsed to no samples at all."""

import io
import os

import pytest

from daqifi.sdcard import SdCardDeviceConfiguration, SdCardJsonFileParser, SdCardParseOptions

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sd_json_firmware_sample.json")


def _parse(data: bytes, options=None):
    return SdCardJsonFileParser().parse_stream(io.BytesIO(data), "log.json", options)


def test_a_real_firmware_json_log_parses_every_sample() -> None:
    with open(_FIXTURE, "rb") as handle:
        session = _parse(handle.read())
    samples = list(session.samples)
    assert len(samples) == 216
    assert all(len(s.analog_values) == 4 for s in samples)
    assert samples[0].analog_values == (0.0, 2.0, 0.0, 0.0)
    assert session.device_config.timestamp_frequency == 42000000
    assert session.device_config.device_part_number == "Nyquist 1"
    # ts 2103674123 -> 2104094123 is 420000 ticks = 10 ms at 42 MHz
    step = (samples[1].timestamp - samples[0].timestamp).total_seconds()
    assert step == pytest.approx(0.01)


def test_firmware_json_calibration_follows_the_named_channel() -> None:
    data = (
        b'{"meta":{"dev":"Nyquist 1","sn":"0123456789ABCDEF","tick_hz":1000}}\n'
        b'{\n"ts":1000,\n"ai":[\n{"ch":7, "val":50}\n]\n}\n'
    )
    calibration = tuple((2.0 if ch == 7 else 1.0, 0.0) for ch in range(8))
    options = SdCardParseOptions(
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=8, digital_port_count=0, timestamp_frequency=1000,
            calibration_values=calibration, resolution=100,
            port_range=(100.0,) * 8, internal_scale_m=(1.0,) * 8,
        )
    )
    sample = list(_parse(data, options).samples)[0]
    assert sample.analog_values == pytest.approx((100.0,))


def test_firmware_json_digital_list_gives_the_latest_port_state() -> None:
    data = b'{"ts":10, "di":[{"ts":9, "mask":15, "val":3},{"ts":10, "mask":15, "val":5}]}\n'
    sample = list(_parse(data).samples)[0]
    assert sample.digital_data == 5


def test_the_legacy_one_line_format_still_parses() -> None:
    data = b'{"ts":1,"analog":[1,2],"digital":"05"}\n{"ts":2,"analog":[3,4]}\n'
    samples = list(_parse(data).samples)
    assert [s.analog_values for s in samples] == [(1.0, 2.0), (3.0, 4.0)]


def test_a_truncated_last_sample_is_dropped_not_fatal() -> None:
    data = b'{"ts":1,"ai":[{"ch":0, "val":1}]}\n{\n"ts":2,\n"ai":[\n{"ch":0'
    samples = list(_parse(data).samples)
    assert [s.analog_values for s in samples] == [(1.0,)]
