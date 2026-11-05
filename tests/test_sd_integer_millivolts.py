"""#163 item 20 (CSV half): integer millivolt logs were read as raw codes.

The firmware writes integer millivolts at VoltagePrecision 0
(csv_encoder.c:449, value * 1000 rounded) and integer raw ADC codes in
RawOutputMode (csv_encoder.c:438, CONF:ADC:USECal 2). The file doesn't say
which (daqifi-nyquist-firmware#1179), so SdCardParseOptions.integer_units
says it, and each sample's analog_units records what was applied. The rule
is the one the JSON parser uses (_FirmwareValueReader).

Fixtures (see fixtures/README.md): a REAL pair from the bench under
SYSTem:STReam:TEST:PATtern 1 (a counter injected upstream of calibration and
of the encoder's raw/mV branch), precision 0 in both, CONF:ADC:USECal 2 vs 0
the only difference. Row i carries code i + 1 + channel in both files, so
they compare positionally: the raw file holds the codes, the mV file the
same codes converted at this board's ~1.2207 mV per code."""

import io
import os
import warnings

import pytest

from daqifi.sdcard import SdCardCsvFileParser, SdCardParseOptions

_HERE = os.path.join(os.path.dirname(__file__), "fixtures")
_LSB_MV = 1.2207  # this board's mV per code, from the pair itself


def _samples(name, integer_units=None):
    with open(os.path.join(_HERE, name), "rb") as handle:
        data = handle.read()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        session = SdCardCsvFileParser().parse_stream(
            io.BytesIO(data), "log.csv", SdCardParseOptions(integer_units=integer_units)
        )
        samples = list(session.samples)
    return samples, [w for w in caught if issubclass(w.category, UserWarning)]


def test_the_raw_pattern_file_read_as_raw_gives_the_injected_codes() -> None:
    samples, caught = _samples("sd_csv_pattern_raw.csv", "raw")
    assert len(samples) == 435 and caught == []
    for i, sample in enumerate(samples):
        assert sample.analog_values == tuple(float(i + 1 + ch) for ch in range(4))
        assert sample.analog_units == "raw"


def test_the_mv_pattern_file_read_as_millivolts_gives_volts() -> None:
    samples, caught = _samples("sd_csv_pattern_mv.csv", "millivolts")
    assert len(samples) == 455 and caught == []
    for i, sample in enumerate(samples):
        assert sample.analog_units == "millivolts"
        for ch, volts in enumerate(sample.analog_values):
            # The firmware rounds code * LSB to whole millivolts.
            assert volts * 1000 == pytest.approx((i + 1 + ch) * _LSB_MV, abs=0.51)


def test_the_two_semantics_disagree_on_the_same_injected_codes() -> None:
    # The discriminating case: the mV file read as raw codes (the default
    # assumption) is off by ~1000x against its millivolt reading, from the
    # third sample on, not by a rounding coincidence.
    as_raw, caught = _samples("sd_csv_pattern_mv.csv")
    as_mv, _ = _samples("sd_csv_pattern_mv.csv", "millivolts")
    assert len(caught) == 1 and all(s.analog_units == "raw-assumed" for s in as_raw)
    assert as_raw[200].analog_values[0] == 245.0
    assert as_mv[200].analog_values[0] == pytest.approx(0.245)
    codes, _ = _samples("sd_csv_pattern_raw.csv", "raw")
    differing = sum(
        1 for raw, mv in zip(codes, as_raw) if raw.analog_values[0] != mv.analog_values[0]
    )
    assert differing >= len(codes) - 2  # only codes 1 and 2 round to themselves


def test_decimals_in_a_millivolts_parse_are_still_volts() -> None:
    data = b"ain0_ts,ain0_val\n1,1.2345\n"
    session = SdCardCsvFileParser().parse_stream(
        io.BytesIO(data), "log.csv", SdCardParseOptions(integer_units="millivolts")
    )
    samples = list(session.samples)
    assert samples[0].analog_values == (1.2345,) and samples[0].analog_units == "volts"


def test_a_csv_row_mixing_decimals_and_integers_is_skipped() -> None:
    data = b"ain0_ts,ain0_val,ain1_ts,ain1_val\n1,1.2345,1,2\n2,0.5000,2,0.2500\n"
    samples = list(SdCardCsvFileParser().parse_stream(io.BytesIO(data), "log.csv").samples)
    assert [s.analog_values for s in samples] == [(0.5, 0.25)]


def test_a_decimal_only_csv_log_does_not_warn() -> None:
    data = b"ain0_ts,ain0_val\n1,1.2345\n2,0.0000\n"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        samples = list(SdCardCsvFileParser().parse_stream(io.BytesIO(data), "log.csv").samples)
    assert [w for w in caught if issubclass(w.category, UserWarning)] == []
    assert [s.analog_units for s in samples] == ["volts", "volts"]


def test_an_unknown_integer_units_value_is_rejected_for_csv() -> None:
    with pytest.raises(ValueError, match="integer_units"):
        SdCardCsvFileParser().parse_stream(
            io.BytesIO(b"ain0_ts,ain0_val\n1,1\n"), "log.csv",
            SdCardParseOptions(integer_units="mV"),
        )
