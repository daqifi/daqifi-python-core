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


@pytest.mark.parametrize("bad", [b'{"ts":1,"ai":1}', b'{"ts":1,"di":{"val":3}}', b'{"ts":1,"ai":[1]}'])
def test_a_malformed_sample_is_skipped_not_fatal(bad) -> None:
    # Audit finding on #183 (round 1): a malformed container raised and the
    # whole parse aborted; the valid sample after it was never returned.
    data = bad + b'\n{"ts":2,"analog":[3]}\n'
    samples = list(_parse(data).samples)
    assert [s.analog_values for s in samples] == [(3.0,)]


def test_a_negative_channel_number_is_skipped_not_fatal() -> None:
    # Audit finding on #183 (round 2): ch -2 passed validation and indexed
    # the calibration tuple from the end, raising IndexError outside the
    # malformed-record handler and losing the samples after it.
    data = b'{"ts":1,"ai":[{"ch":-2,"val":50}]}\n{"ts":2,"ai":[{"ch":0,"val":50}]}\n'
    options = SdCardParseOptions(
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=1, digital_port_count=0, timestamp_frequency=1000,
            resolution=100, calibration_values=((1.0, 0.0),),
        )
    )
    samples = list(_parse(data, options).samples)
    assert len(samples) == 1 and samples[0].analog_values == pytest.approx((0.5,))


@pytest.mark.parametrize("bad", [b"1e309", b"-1e309", b"NaN", b"Infinity"])
def test_a_non_finite_value_is_skipped_not_fatal(bad) -> None:
    # Audit finding on #183 (round 3): inf passed validation, then int(inf)
    # raised OverflowError outside the handler and ended the iterator.
    data = b'{"ts":1,"ai":[{"ch":0,"val":' + bad + b'}]}\n{"ts":2,"ai":[{"ch":0,"val":3}]}\n'
    samples = list(_parse(data).samples)
    assert [s.analog_values for s in samples] == [(3.0,)]


def test_a_non_finite_legacy_value_is_skipped_not_fatal() -> None:
    samples = list(_parse(b'{"ts":1,"analog":[1e309]}\n{"ts":2,"analog":[3]}\n').samples)
    assert [s.analog_values for s in samples] == [(3.0,)]


def test_deeply_nested_json_is_skipped_not_fatal() -> None:
    # Audit finding on #183 (round 3): raw_decode raised RecursionError,
    # which the decoder loop didn't catch, so the whole parse aborted.
    for opener, closer in ((b"[", b"]"), (b"{", b"}")):
        data = opener * 10000 + b"0" + closer * 10000 + b'\n{"ts":2,"analog":[3]}\n'
        samples = list(_parse(data).samples)
        assert [s.analog_values for s in samples] == [(3.0,)]


@pytest.mark.parametrize("tick", [b"1" + b"0" * 400, b"4294967296", b"true", b"-5"])
def test_an_unusable_tick_rate_falls_back_instead_of_aborting(tick) -> None:
    # Audit finding on #183 (round 4): a 401-digit tick_hz was accepted, then
    # 1.0 / tick raised OverflowError before any sample was produced.
    data = b'{"meta":{"tick_hz":' + tick + b'}}\n{"ts":1,"ai":[{"ch":0,"val":3}]}\n'
    session = _parse(data)
    assert [s.analog_values for s in session.samples] == [(3.0,)]
    assert 0 < session.device_config.timestamp_frequency <= 0xFFFFFFFF


def test_voltage_mode_values_are_kept_as_volts() -> None:
    # Audit finding on #183 (round 5): at the firmware's default precision 4
    # (JSON_Encoder.c "%.*f") ai[].val is already volts; int() truncated
    # 1.2345 to 1.0 and -0.875 to 0.0.
    data = (
        b'{"meta":{"tick_hz":42000000}}\n'
        b'{"ts":100,"ai":[{"ch":0,"val":1.2345},{"ch":1,"val":-0.8750}]}\n'
    )
    assert [s.analog_values for s in _parse(data).samples] == [(1.2345, -0.875)]


def test_voltage_mode_values_are_not_scaled_again_by_calibration() -> None:
    data = b'{"ts":100,"ai":[{"ch":0,"val":1.2345}]}\n'
    options = SdCardParseOptions(
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=1, digital_port_count=0, timestamp_frequency=1000,
            resolution=100, calibration_values=((2.0, 0.5),),
        )
    )
    assert [s.analog_values for s in _parse(data, options).samples] == [(1.2345,)]


def test_raw_mode_integers_are_still_scaled_as_codes() -> None:
    data = b'{"ts":100,"ai":[{"ch":0,"val":50}]}\n'
    options = SdCardParseOptions(
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=1, digital_port_count=0, timestamp_frequency=1000,
            resolution=100, calibration_values=((2.0, 0.0),),
        )
    )
    assert [s.analog_values for s in _parse(data, options).samples] == [pytest.approx((1.0,))]
