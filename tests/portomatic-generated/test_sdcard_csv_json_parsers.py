"""Tests for SdCardCsvFileParser + SdCardJsonFileParser (closes #7).

Pure-Python parsing — exercises the firmware CSV format (3 comment
lines + column header + interleaved ts/val pairs) and the JSONL
format (one ``{"ts":...,"analog":[...],"digital":"<hex>"}`` per
line). Hardware-independent.
"""
from __future__ import annotations

import io
import json
import math
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from daqifi.enums import SdCardLogFormat
from daqifi.sdcard import (
    SdCardCsvFileParser,
    SdCardDeviceConfiguration,
    SdCardFileParserFactory,
    SdCardJsonFileParser,
    SdCardLogEntry,
    SdCardLogSession,
    SdCardParseOptions,
    SdCardParseProgress,
    _parse_digital_hex_string,
)


pytestmark = pytest.mark.portomatic_generated


# ---- helpers ---------------------------------------------------------------


def _csv_text(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _csv_stream(*lines: str) -> io.StringIO:
    return io.StringIO(_csv_text(*lines))


def _csv_bytes_stream(*lines: str) -> io.BytesIO:
    return io.BytesIO(_csv_text(*lines).encode("utf-8"))


def _json_lines(*objs: dict) -> str:
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def _json_stream(*objs: dict) -> io.StringIO:
    return io.StringIO(_json_lines(*objs))


# ---- CSV parser ------------------------------------------------------------


class TestSdCardCsvFileParserHeader:
    def test_extracts_device_metadata_from_comments(self):
        parser = SdCardCsvFileParser()
        # Synthetic identifiers — the CSV parser only verbatim-copies
        # the strings, so values that obviously aren't real SNs are
        # fine for verifying the round-trip.
        stream = _csv_stream(
            "# Device: TestDevice",
            "# Serial Number: <placeholder-sn>",
            "# Timestamp Tick Rate: 50000000 Hz",
            "ain0_ts,ain0_val,dio_ts,dio_val",
            "100,32768,100,0",
        )
        session = parser.parse_stream(stream, "log_20260510_120000.csv")
        config = session.device_config
        assert config is not None
        assert config.device_part_number == "TestDevice"
        assert config.device_serial_number == "<placeholder-sn>"
        assert config.timestamp_frequency == 50_000_000
        # Header counted 1 analog pair + 1 digital pair.
        assert config.analog_port_count == 1
        assert config.digital_port_count == 1

    def test_handles_missing_metadata_gracefully(self):
        parser = SdCardCsvFileParser()
        stream = _csv_stream(
            "ain0_ts,ain0_val",
            "100,32768",
        )
        session = parser.parse_stream(stream, "log_x.csv")
        config = session.device_config
        assert config is not None
        assert config.device_part_number is None
        assert config.device_serial_number is None
        # No tick rate header → uses option default (50 MHz).
        assert config.timestamp_frequency == 50_000_000

    def test_uses_fallback_timestamp_frequency_when_header_absent(self):
        parser = SdCardCsvFileParser()
        opts = SdCardParseOptions(fallback_timestamp_frequency=1_000)
        stream = _csv_stream("ain0_ts,ain0_val", "100,32768")
        session = parser.parse_stream(stream, "log_x.csv", opts)
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 1_000

    def test_strips_hz_suffix_from_tick_rate(self):
        parser = SdCardCsvFileParser()
        stream = _csv_stream(
            "# Timestamp Tick Rate: 25000000 Hz",
            "ain0_ts,ain0_val",
        )
        session = parser.parse_stream(stream, "log.csv")
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 25_000_000


class TestSdCardCsvFileParserDataRows:
    def test_parses_two_channel_rows_with_scaling(self):
        parser = SdCardCsvFileParser()
        # Use a synthetic config so scaling is deterministic:
        # raw / resolution * port_range * cal_m + cal_b
        opts = SdCardParseOptions(
            configuration_override=SdCardDeviceConfiguration(
                analog_port_count=2,
                digital_port_count=0,
                timestamp_frequency=1_000,
                resolution=65535,
                port_range=(10.0, 10.0),
                calibration_values=((1.0, 0.0), (1.0, 0.0)),
                internal_scale_m=(1.0, 1.0),
            )
        )
        stream = _csv_stream(
            "# Timestamp Tick Rate: 1000 Hz",
            "ain0_ts,ain0_val,ain1_ts,ain1_val",
            "100,32768,100,16384",
            "200,32768,200,16384",
        )
        session = parser.parse_stream(stream, "log_x.csv", opts)
        entries = list(session.samples)

        assert len(entries) == 2
        # First sample: 32768/65535*10 ≈ 5.0; 16384/65535*10 ≈ 2.5.
        assert math.isclose(entries[0].analog_values[0], 5.0, rel_tol=1e-3)
        assert math.isclose(entries[0].analog_values[1], 2.5, rel_tol=1e-3)
        # Per-channel timestamps preserved.
        assert entries[0].analog_timestamps == (100, 100)
        # Tick delta from row 1 to row 2 = 100 ticks; @ 1000 Hz = 0.1s.
        delta = entries[1].timestamp - entries[0].timestamp
        assert math.isclose(delta.total_seconds(), 0.1, abs_tol=1e-6)

    def test_skips_malformed_data_rows(self):
        parser = SdCardCsvFileParser()
        stream = _csv_stream(
            "ain0_ts,ain0_val",
            "100,32768",
            "garbage,not-a-number",
            "200,32768",
            "300,32768,extra,column",  # odd-column count
        )
        session = parser.parse_stream(stream, "log.csv")
        entries = list(session.samples)
        # 4 data rows — 2 good (rows 1 + 3), 2 malformed (skipped).
        # Row 4 has 4 columns which is even, so it parses as 2 pairs
        # but the layout says only 1 analog pair → still 1 sample.
        assert len(entries) == 3

    def test_digital_only_row_uses_dio_timestamp(self):
        parser = SdCardCsvFileParser()
        stream = _csv_stream(
            "# Timestamp Tick Rate: 1000 Hz",
            "dio_ts,dio_val",
            "100,3",
            "200,5",
        )
        session = parser.parse_stream(stream, "log.csv")
        entries = list(session.samples)
        assert len(entries) == 2
        assert entries[0].digital_data == 3
        assert entries[1].digital_data == 5

    def test_empty_stream_returns_empty_session(self):
        parser = SdCardCsvFileParser()
        session = parser.parse_stream(io.StringIO(""), "log.csv")
        assert session.device_config is None
        assert list(session.samples) == []

    def test_only_header_returns_empty_samples(self):
        parser = SdCardCsvFileParser()
        stream = _csv_stream(
            "# Device: Nyquist 1",
            "ain0_ts,ain0_val",
        )
        session = parser.parse_stream(stream, "log.csv")
        # Config populated, but no data rows.
        assert session.device_config is not None
        assert list(session.samples) == []


class TestSdCardCsvFileParserStreamCompat:
    def test_accepts_bytes_stream(self):
        parser = SdCardCsvFileParser()
        stream = _csv_bytes_stream("ain0_ts,ain0_val", "100,32768")
        session = parser.parse_stream(stream, "log.csv")
        assert session.device_config is not None

    def test_strips_utf8_bom_from_bytes(self):
        parser = SdCardCsvFileParser()
        # Some firmware versions write a BOM at file start.
        body = "﻿" + _csv_text("ain0_ts,ain0_val", "100,32768")
        session = parser.parse_stream(io.BytesIO(body.encode("utf-8")), "log.csv")
        assert session.device_config is not None
        assert session.device_config.analog_port_count == 1

    def test_parse_file_round_trip(self, tmp_path: Path):
        parser = SdCardCsvFileParser()
        csv_path = tmp_path / "log_20260510_120000.csv"
        csv_path.write_text(_csv_text(
            "# Device: Nq1",
            "# Timestamp Tick Rate: 1000 Hz",
            "ain0_ts,ain0_val",
            "100,32768",
        ))
        session = parser.parse_file(str(csv_path))
        assert session.file_name == csv_path.name
        assert session.file_created_date is not None
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 1000

    def test_rejects_none_file_stream(self):
        with pytest.raises(TypeError, match="must not be None"):
            SdCardCsvFileParser().parse_stream(None, "log.csv")  # type: ignore[arg-type]

    def test_rejects_none_file_name(self):
        with pytest.raises(TypeError, match="must not be None"):
            SdCardCsvFileParser().parse_stream(_csv_stream("a,b"), None)  # type: ignore[arg-type]


class TestSdCardCsvFileParserProgress:
    def test_calls_progress_callback(self):
        parser = SdCardCsvFileParser()
        events: list[SdCardParseProgress] = []
        opts = SdCardParseOptions(progress=events.append)
        stream = _csv_stream(
            "ain0_ts,ain0_val",
            *(f"{100 * i},32768" for i in range(1, 250)),
        )
        session = parser.parse_stream(stream, "log.csv", opts)
        # Drain the iterator so progress fires.
        list(session.samples)
        assert events  # at least one callback fired
        # SdCardParseProgress.messages_read carries the line count
        # for CSV/JSON parsers (field naming inherited from the
        # protobuf parser where it's literal protobuf messages).
        assert events[-1].messages_read >= 200

    def test_progress_callback_failure_doesnt_abort_parse(self):
        parser = SdCardCsvFileParser()

        def bad_progress(_p: SdCardParseProgress) -> None:
            raise RuntimeError("intentional")

        opts = SdCardParseOptions(progress=bad_progress)
        stream = _csv_stream(
            "ain0_ts,ain0_val",
            *(f"{100 * i},32768" for i in range(1, 250)),
        )
        session = parser.parse_stream(stream, "log.csv", opts)
        # Should complete despite the bad progress callback.
        entries = list(session.samples)
        assert len(entries) == 249


# ---- JSON parser -----------------------------------------------------------


class TestSdCardJsonFileParserBasics:
    def test_parses_jsonl_with_inferred_channel_count(self):
        parser = SdCardJsonFileParser()
        opts = SdCardParseOptions(
            configuration_override=SdCardDeviceConfiguration(
                analog_port_count=0,  # let inference decide
                digital_port_count=0,
                timestamp_frequency=1000,
                resolution=65535,
                port_range=(10.0, 10.0),
                calibration_values=((1.0, 0.0), (1.0, 0.0)),
                internal_scale_m=(1.0, 1.0),
            )
        )
        stream = _json_stream(
            {"ts": 100, "analog": [32768, 16384], "digital": "03"},
            {"ts": 200, "analog": [32768, 16384], "digital": "05"},
        )
        session = parser.parse_stream(stream, "log.json", opts)
        entries = list(session.samples)
        assert len(entries) == 2
        # Inference picked up 2 analog channels from the first row.
        assert session.device_config.analog_port_count == 2
        # Scaling: 32768/65535*10 ≈ 5.0
        assert math.isclose(entries[0].analog_values[0], 5.0, rel_tol=1e-3)
        # Digital hex 03 → 0x03 → 3
        assert entries[0].digital_data == 3
        assert entries[1].digital_data == 5

    def test_parses_multibyte_digital_hex_little_endian(self):
        # "01-02" → 0x01 + (0x02 << 8) = 513
        assert _parse_digital_hex_string("01-02") == 513
        # "FF-FF-FF-FF" → 0xFFFFFFFF
        assert _parse_digital_hex_string("FF-FF-FF-FF") == 0xFFFFFFFF
        # 5+ bytes — silently capped at 4 (uint32).
        assert _parse_digital_hex_string("01-02-03-04-05") == 0x04030201
        # Empty / malformed bytes contribute 0 rather than abort.
        assert _parse_digital_hex_string("ZZ-02") == 0x0200
        assert _parse_digital_hex_string("") == 0

    def test_skips_malformed_lines(self):
        parser = SdCardJsonFileParser()
        body = (
            json.dumps({"ts": 100, "analog": [32768], "digital": "00"}) + "\n"
            "not valid json\n"
            + json.dumps({"ts": "string-ts", "analog": [32768]}) + "\n"
            + json.dumps({"ts": 200, "analog": [32768], "digital": "00"}) + "\n"
        )
        session = parser.parse_stream(io.StringIO(body), "log.json")
        entries = list(session.samples)
        assert len(entries) == 2

    def test_rejects_bool_in_analog_array(self):
        parser = SdCardJsonFileParser()
        body = json.dumps({"ts": 100, "analog": [True], "digital": "00"}) + "\n"
        session = parser.parse_stream(io.StringIO(body), "log.json")
        # bool counts as "not a number" per our explicit check.
        assert list(session.samples) == []

    def test_empty_stream_returns_empty_session(self):
        parser = SdCardJsonFileParser()
        session = parser.parse_stream(io.StringIO(""), "log.json")
        assert session.device_config is None
        assert list(session.samples) == []

    def test_parse_file_round_trip(self, tmp_path: Path):
        parser = SdCardJsonFileParser()
        json_path = tmp_path / "log_20260510_120000.json"
        json_path.write_text(
            json.dumps({"ts": 100, "analog": [32768], "digital": "01"}) + "\n"
        )
        session = parser.parse_file(str(json_path))
        assert session.file_name == json_path.name
        assert session.device_config is not None
        assert session.device_config.analog_port_count == 1


class TestThreeTierTimestampPrecedence:
    """Regression for Qodo PR #89 finding #1: tick-rate precedence
    must be parsed > override > fallback (matching the protobuf
    parser's behavior)."""

    def test_csv_override_used_when_header_lacks_tick_rate(self):
        parser = SdCardCsvFileParser()
        opts = SdCardParseOptions(
            fallback_timestamp_frequency=999_999,  # decoy — should NOT win
            configuration_override=SdCardDeviceConfiguration(
                analog_port_count=0,
                digital_port_count=0,
                timestamp_frequency=12345,  # this should win
            ),
        )
        stream = _csv_stream("ain0_ts,ain0_val", "100,32768")
        session = parser.parse_stream(stream, "log.csv", opts)
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 12345

    def test_csv_fallback_used_when_neither_header_nor_override(self):
        parser = SdCardCsvFileParser()
        opts = SdCardParseOptions(fallback_timestamp_frequency=2000)
        stream = _csv_stream("ain0_ts,ain0_val", "100,32768")
        session = parser.parse_stream(stream, "log.csv", opts)
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 2000

    def test_csv_header_beats_override_and_fallback(self):
        parser = SdCardCsvFileParser()
        opts = SdCardParseOptions(
            fallback_timestamp_frequency=999_999,
            configuration_override=SdCardDeviceConfiguration(
                analog_port_count=0,
                digital_port_count=0,
                timestamp_frequency=88_888,
            ),
        )
        stream = _csv_stream(
            "# Timestamp Tick Rate: 50000 Hz",
            "ain0_ts,ain0_val",
            "100,32768",
        )
        session = parser.parse_stream(stream, "log.csv", opts)
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 50000

    def test_json_override_used_when_format_lacks_tick_rate(self):
        parser = SdCardJsonFileParser()
        opts = SdCardParseOptions(
            fallback_timestamp_frequency=999_999,
            configuration_override=SdCardDeviceConfiguration(
                analog_port_count=0,
                digital_port_count=0,
                timestamp_frequency=4321,
            ),
        )
        stream = _json_stream(
            {"ts": 100, "analog": [32768], "digital": "00"},
        )
        session = parser.parse_stream(stream, "log.json", opts)
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 4321

    def test_json_fallback_used_when_no_override(self):
        parser = SdCardJsonFileParser()
        opts = SdCardParseOptions(fallback_timestamp_frequency=3000)
        stream = _json_stream({"ts": 100, "analog": [32768], "digital": "00"})
        session = parser.parse_stream(stream, "log.json", opts)
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 3000


class TestZeroTickTimestampMatchesProtobuf:
    """Regression for Qodo PR #89 finding #3: ts==0 must be treated
    as 'no timestamp' so cross-format reconstruction is consistent
    with the protobuf parser."""

    def test_csv_first_row_zero_ts_does_not_anchor(self):
        parser = SdCardCsvFileParser()
        opts = SdCardParseOptions(
            fallback_timestamp_frequency=1000,
        )
        stream = _csv_stream(
            "ain0_ts,ain0_val",
            "0,32768",     # ts=0 — should NOT anchor
            "1000,32768",  # this should anchor (first non-zero ts)
            "2000,32768",  # +1000 ticks → +1.0s
        )
        session = parser.parse_stream(stream, "log.csv", opts)
        entries = list(session.samples)
        assert len(entries) == 3
        # First entry uses base_time (no anchor yet).
        # Second entry anchors at ts=1000, so still base_time (first
        # non-zero observation).
        # Third entry: delta = 2000 - 1000 = 1000 ticks @ 1000 Hz = 1.0s.
        delta_third_to_second = entries[2].timestamp - entries[1].timestamp
        assert math.isclose(
            delta_third_to_second.total_seconds(), 1.0, abs_tol=1e-6
        )

    def test_json_first_row_zero_ts_does_not_anchor(self):
        parser = SdCardJsonFileParser()
        opts = SdCardParseOptions(fallback_timestamp_frequency=1000)
        stream = _json_stream(
            {"ts": 0, "analog": [32768], "digital": "00"},
            {"ts": 1000, "analog": [32768], "digital": "00"},
            {"ts": 2000, "analog": [32768], "digital": "00"},
        )
        session = parser.parse_stream(stream, "log.json", opts)
        entries = list(session.samples)
        assert len(entries) == 3
        delta = entries[2].timestamp - entries[1].timestamp
        assert math.isclose(delta.total_seconds(), 1.0, abs_tol=1e-6)


class TestSdCardFileParserFactoryDispatch:
    def test_factory_routes_csv_extension(self, tmp_path: Path):
        csv_path = tmp_path / "log.csv"
        csv_path.write_text(
            _csv_text("ain0_ts,ain0_val", "100,32768")
        )
        session = SdCardFileParserFactory.parse_file(str(csv_path))
        assert session.device_config is not None
        # parse_with_format should detect CSV explicitly too.
        with open(csv_path, "r", encoding="utf-8") as f:
            session2 = SdCardFileParserFactory.parse_with_format(
                f, "log.csv", SdCardLogFormat.CSV
            )
        assert session2.device_config is not None

    def test_factory_routes_json_extension(self, tmp_path: Path):
        json_path = tmp_path / "log.json"
        json_path.write_text(
            json.dumps({"ts": 100, "analog": [32768], "digital": "00"}) + "\n"
        )
        session = SdCardFileParserFactory.parse_file(str(json_path))
        assert session.device_config is not None
