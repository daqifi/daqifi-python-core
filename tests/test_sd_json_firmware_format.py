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
    # di "ts" values are ages back from the trigger stamp, oldest element
    # first (JSON_Encoder.c pops the queue in order), so ages decrease.
    data = b'{"ts":10, "di":[{"ts":10, "mask":15, "val":3},{"ts":9, "mask":15, "val":5}]}\n'
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


def test_legacy_decimal_values_are_still_raw_counts() -> None:
    # Audit finding on #183 (round 6): the volts rule reached the legacy
    # "analog" shape, which upstream scales as raw counts, so 50.0 came out
    # as 50.0 instead of the calibrated 1.0.
    options = SdCardParseOptions(
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=1, digital_port_count=0, timestamp_frequency=1000,
            resolution=100, calibration_values=((2.0, 0.0),),
        )
    )
    samples = list(_parse(b'{"ts":1,"analog":[50.0]}\n', options).samples)
    assert [s.analog_values for s in samples] == [pytest.approx((1.0,))]


@pytest.mark.parametrize(
    "record",
    [
        b'{"ts":1,"analog":[1' + b"0" * 400 + b"]}",
        b'{"ts":1,"ai":[{"ch":0,"val":1' + b"0" * 400 + b"}]}",
    ],
)
def test_an_oversized_integer_is_skipped_not_fatal(record) -> None:
    # Audit finding on #183 (round 6): a 401-digit integer passed validation,
    # then float() overflowed during scaling, outside the handler.
    samples = list(_parse(record + b'\n{"ts":2,"analog":[3]}\n').samples)
    assert [s.analog_values for s in samples] == [(3.0,)]


# Audit round 7 on #183, and #163 item 20's JSON half.

def test_samples_batched_in_one_object_are_all_kept() -> None:
    # JSON_Encoder.c's while (qSize > 0) loop writes every sample queued at
    # one call into ONE object as repeated "ts"/"ai" keys; json keeps only
    # the last of each. SYNTHETIC: the real 200 Hz capture has one sample
    # per object, so it can't exercise this.
    batched = (
        b'{\n"ts":100,\n"ai":[\n{"ch":0, "val":1.0000}\n],\n'
        b'"ts":110,\n"ai":[\n{"ch":0, "val":2.0000}\n],\n'
        b'"ts":120,\n"ai":[\n{"ch":0, "val":3.0000}\n]\n}\n'
    )
    data = b'{"meta":{"tick_hz":1000}}\n' + batched
    samples = list(_parse(data).samples)
    assert len(samples) == batched.count(b'"ts"') == 3
    assert [s.analog_values for s in samples] == [(1.0,), (2.0,), (3.0,)]
    assert [(s.timestamp - samples[0].timestamp).total_seconds() for s in samples] == [
        0.0, pytest.approx(0.010), pytest.approx(0.020)
    ]


_INTEGER_LOG = b'{"ts":1,"ai":[{"ch":0,"val":1234}]}\n{"ts":2,"ai":[{"ch":0,"val":-56}]}\n'


def _calibrated(integer_units=None):
    return SdCardParseOptions(
        integer_units=integer_units,
        configuration_override=SdCardDeviceConfiguration(
            analog_port_count=1, digital_port_count=0, timestamp_frequency=1000,
            resolution=100, calibration_values=((2.0, 0.0),),
        ),
    )


def _samples_and_warnings(data, options=None):
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        samples = list(_parse(data, options).samples)
    return samples, [w for w in caught if issubclass(w.category, UserWarning)]


def test_millivolt_integers_are_read_as_volts_without_calibration() -> None:
    samples, caught = _samples_and_warnings(_INTEGER_LOG, _calibrated("millivolts"))
    assert [s.analog_values for s in samples] == [(1.234,), (-0.056,)]
    assert [s.analog_units for s in samples] == ["millivolts"] * 2
    assert caught == []


def test_decimals_in_a_millivolts_parse_are_still_volts() -> None:
    data = b'{"ts":1,"ai":[{"ch":0,"val":1.2345}]}\n'
    samples, _ = _samples_and_warnings(data, _calibrated("millivolts"))
    assert [s.analog_values for s in samples] == [(1.2345,)]
    assert samples[0].analog_units == "volts"


def test_unspecified_integers_warn_exactly_once_per_parse() -> None:
    samples, caught = _samples_and_warnings(_INTEGER_LOG, _calibrated())
    assert len(caught) == 1 and "integer_units" in str(caught[0].message)
    assert [s.analog_units for s in samples] == ["raw-assumed"] * 2
    assert samples[0].analog_values == pytest.approx((24.68,))
    _, again = _samples_and_warnings(_INTEGER_LOG, _calibrated())
    assert len(again) == 1  # once per parse, not once per process


def test_explicit_raw_is_silent_and_says_raw() -> None:
    samples, caught = _samples_and_warnings(_INTEGER_LOG, _calibrated("raw"))
    assert caught == []
    assert [s.analog_units for s in samples] == ["raw"] * 2
    assert samples[0].analog_values == pytest.approx((24.68,))


def test_a_decimal_only_log_does_not_warn() -> None:
    data = b'{"ts":1,"ai":[{"ch":0,"val":1.2345}]}\n{"ts":2,"ai":[{"ch":0,"val":0.0000}]}\n'
    samples, caught = _samples_and_warnings(data)
    assert caught == []
    assert [s.analog_units for s in samples] == ["volts"] * 2


def test_a_sample_mixing_decimals_and_integers_is_skipped() -> None:
    # The value mode is global, and precision >= 1 writes zero as "0.0000":
    # a mixed sample isn't firmware output, so it's dropped rather than
    # tagged by whichever value came first.
    data = (
        b'{"ts":1,"ai":[{"ch":0,"val":1.2345},{"ch":1,"val":2}]}\n'
        b'{"ts":2,"ai":[{"ch":0,"val":0.5000},{"ch":1,"val":0.2500}]}\n'
    )
    samples, _ = _samples_and_warnings(data)
    assert [s.analog_values for s in samples] == [(0.5, 0.25)]


def test_an_unknown_integer_units_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="integer_units"):
        _parse(_INTEGER_LOG, SdCardParseOptions(integer_units="volts"))


def test_legacy_samples_carry_no_units_and_do_not_warn() -> None:
    samples, caught = _samples_and_warnings(b'{"ts":1,"analog":[50]}\n')
    assert caught == [] and samples[0].analog_units is None


def test_batched_samples_take_the_digital_state_of_their_own_time() -> None:
    # Audit finding on #183 (round 8): every batched sample took the
    # object's LAST "di" state. di "ts" values are ages back from the
    # trigger stamp; the latest sample's "ts" stands in for it. SYNTHETIC.
    data = (
        b'{"meta":{"tick_hz":1000}}\n'
        b'{"di":[{"ts":10,"mask":15,"val":3},{"ts":0,"mask":15,"val":5}],'
        b'"ts":100,"ai":[{"ch":0,"val":1.0000}],"ts":110,"ai":[{"ch":0,"val":2.0000}]}\n'
    )
    samples = list(_parse(data).samples)
    assert [s.digital_data for s in samples] == [3, 5]


def test_a_single_sample_object_still_takes_the_latest_digital_state() -> None:
    data = b'{"di":[{"ts":10,"mask":15,"val":3},{"ts":0,"mask":15,"val":5}],"ts":110,"ai":[{"ch":0,"val":2.0000}]}\n'
    assert [s.digital_data for s in _parse(data).samples] == [5]


@pytest.mark.parametrize("first, second, ages", [(4294967290, 4, (10, 0)), (4294967286, 10, (20, 0))])
def test_batched_digital_states_across_the_timer_rollover(first, second, ages) -> None:
    # Audit finding on #183 (round 9): reference - age wrapped negative and
    # compared below the earlier sample's wrapped "ts". SYNTHETIC.
    data = (
        b'{"di":[{"ts":%d,"mask":15,"val":3},{"ts":%d,"mask":15,"val":5}],'
        b'"ts":%d,"ai":[{"ch":0,"val":1.0000}],"ts":%d,"ai":[{"ch":0,"val":2.0000}]}\n'
        % (ages[0], ages[1], first, second)
    )
    assert [s.digital_data for s in _parse(data).samples] == [3, 5]


def test_a_digital_read_just_after_the_trigger_is_not_given_to_earlier_samples() -> None:
    # Audit finding on #183 (round 10): a DIO read one tick after the
    # trigger has age 0xFFFFFFFF (-1); read unsigned it looked ancient and
    # leaked into earlier samples. SYNTHETIC.
    data = (
        b'{"di":[{"ts":9,"mask":15,"val":3},{"ts":4294967295,"mask":15,"val":5}],'
        b'"ts":100,"ai":[{"ch":0,"val":1.0000}],"ts":110,"ai":[{"ch":0,"val":2.0000}]}\n'
    )
    assert [s.digital_data for s in _parse(data).samples] == [0, 3]


def test_a_single_sample_object_ignores_a_digital_read_after_the_trigger() -> None:
    # Audit finding on #183 (round 11): single-sample objects skipped the
    # age placement and took di[-1], a read one tick after the trigger.
    data = (
        b'{"di":[{"ts":9,"mask":15,"val":3},{"ts":4294967295,"mask":15,"val":5}],'
        b'"ts":110,"ai":[{"ch":0,"val":2.0000}]}\n'
    )
    assert [s.digital_data for s in _parse(data).samples] == [3]


@pytest.mark.parametrize("record", [b'{"di":[]}', b'{"di":[{"ts":1,"mask":1,"val":1}]}', b'{"di":"x","ts":1}'])
def test_a_digital_object_without_a_usable_timestamp_is_skipped_not_fatal(record) -> None:
    # Audit finding on #183 (round 12): a "di" object with no top-level "ts"
    # raised IndexError outside the malformed-record handler.
    samples = list(_parse(record + b'\n{"ts":2,"analog":[3]}\n').samples)
    assert samples[-1].analog_values == (3.0,)


# Audit round 13 on #183: one digital timeline per parse.

def test_the_digital_state_carries_across_objects() -> None:
    # The port held 5; the second object's only read is one tick after its
    # trigger. The state at tick 110 is still 5, not an invented 0.
    data = (
        b'{"ts":100,"ai":[{"ch":0,"val":1.0000}],"di":[{"ts":0,"mask":15,"val":5}]}\n'
        b'{"ts":110,"ai":[{"ch":0,"val":2.0000}],"di":[{"ts":4294967295,"mask":15,"val":5}]}\n'
    )
    assert [s.digital_data for s in _parse(data).samples] == [5, 5]


def test_a_future_digital_read_applies_to_the_next_sample() -> None:
    data = (
        b'{"ts":110,"ai":[{"ch":0,"val":1.0000}],"di":[{"ts":4294967295,"mask":15,"val":7}]}\n'
        b'{"ts":120,"ai":[{"ch":0,"val":2.0000}]}\n'
    )
    assert [s.digital_data for s in _parse(data).samples] == [0, 7]


def test_a_digital_only_sample_with_only_a_future_read_is_kept() -> None:
    data = b'{"ts":110,"di":[{"ts":4294967295,"mask":15,"val":5}]}\n'
    samples = list(_parse(data).samples)
    assert len(samples) == 1 and samples[0].analog_values == ()
