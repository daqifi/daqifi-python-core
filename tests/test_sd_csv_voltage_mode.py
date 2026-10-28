"""#163 item 18: SD CSV logs in the firmware's default voltage mode were
truncated to whole volts.

csv_encoder.c writes volts with N decimals (``%.*f``) at VoltagePrecision
N >= 1, which is the default (precision 4, raw mode off); integers only in
raw mode (ADC codes) or at precision 0 (millivolts). The parser int()'d
every value as a raw count, so 1.2345 V became 1.0.

The fixture is SYNTHETIC: no real default-mode CSV capture exists yet. It
follows csv_encoder.c's standard (non-compact) layout: the "# Device:",
"# Serial Number:" and "# Timestamp Tick Rate: <n> Hz" header lines, then
"ain<N>_ts,ain<N>_val" pairs and "dio_ts,dio_val", with values at
precision 4. The serial is the firmware's documented example value."""

import io

import pytest

from daqifi.sdcard import SdCardCsvFileParser, SdCardDeviceConfiguration, SdCardParseOptions

_DEFAULT_MODE_CSV = (
    b"# Device: Nyquist 1\n"
    b"# Serial Number: 0123456789ABCDEF\n"
    b"# Timestamp Tick Rate: 42000000 Hz\n"
    b"ain0_ts,ain0_val,ain1_ts,ain1_val,dio_ts,dio_val\n"
    b"420000,1.2345,420000,-0.8750,420000,0\n"
    b"840000,2.5000,840000,0.1000,840000,1\n"
)


def _parse(data: bytes, options=None):
    return SdCardCsvFileParser().parse_stream(io.BytesIO(data), "log.csv", options)


def test_default_voltage_mode_values_are_kept_as_volts() -> None:
    samples = list(_parse(_DEFAULT_MODE_CSV).samples)
    assert [s.analog_values for s in samples] == [(1.2345, -0.875), (2.5, 0.1)]


def test_volts_are_not_scaled_again_by_calibration() -> None:
    options = SdCardParseOptions(
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=2, digital_port_count=1, timestamp_frequency=42000000,
            resolution=100, calibration_values=((2.0, 0.5), (2.0, 0.5)),
        )
    )
    samples = list(_parse(_DEFAULT_MODE_CSV, options).samples)
    assert samples[0].analog_values == (1.2345, -0.875)


def test_raw_mode_integers_are_still_scaled_as_codes() -> None:
    data = (
        b"# Timestamp Tick Rate: 1000 Hz\n"
        b"ain0_ts,ain0_val\n"
        b"1,50\n"
        b"2,-50\n"
    )
    options = SdCardParseOptions(
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=1, digital_port_count=0, timestamp_frequency=1000,
            resolution=100, calibration_values=((2.0, 0.0),),
        )
    )
    samples = list(_parse(data, options).samples)
    assert [s.analog_values for s in samples] == [pytest.approx((1.0,)), pytest.approx((-1.0,))]


@pytest.mark.parametrize("bad", [b"nan", b"inf", b"-inf", b"1e309"])
def test_a_non_finite_value_skips_the_row_not_the_log(bad) -> None:
    data = b"ain0_ts,ain0_val\n1," + bad + b"\n2,0.5000\n"
    assert [s.analog_values for s in _parse(data).samples] == [(0.5,)]
