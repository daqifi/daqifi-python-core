"""Tests for #98 — Logging/Export module port (daqifi-core PR #167).

Mirrors the scope of CsvExporterTests.cs: header formatting,
timestamp modes, missing-data handling, multi-tick grouping,
average-window aggregation, progress reporting, cancellation, and
edge cases.
"""
from __future__ import annotations

import datetime as _dt
import io
import threading
from typing import Iterator, List, Optional

import pytest

from daqifi._internal.scpi import ChannelType
from daqifi.export import (
    ChannelDescriptor,
    CsvExporter,
    CsvExportOptions,
    SampleRow,
    SampleSource,
)


pytestmark = pytest.mark.portomatic_generated


# Channel fixtures matching the upstream CsvExporterTests setup.
CH1 = ChannelDescriptor("DevA", "SN001", "Channel1", ChannelType.Analog)
CH2 = ChannelDescriptor("DevA", "SN001", "Channel2", ChannelType.Analog)
CH_DIG = ChannelDescriptor("DevB", "SN002", "Digital1", ChannelType.Digital)


# .NET Ticks for 2024-01-01 00:00:00 UTC. Computed exactly: ticks
# since 0001-01-01 = (2024-01-01 - 0001-01-01) seconds * 10_000_000.
_DOTNET_EPOCH = _dt.datetime(1, 1, 1, tzinfo=_dt.timezone.utc)
_T0_DT = _dt.datetime(2024, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
T0 = int((_T0_DT - _DOTNET_EPOCH).total_seconds()) * 10_000_000
T1 = T0 + 10_000_000  # +1 second
T2 = T0 + 20_000_000  # +2 seconds


class _InMemorySampleSource(SampleSource):
    """Mirrors upstream InMemorySampleSource — order samples by tick
    on construction; sample-count is just len(samples)."""

    def __init__(
        self,
        channels: List[ChannelDescriptor],
        samples: List[SampleRow],
    ):
        self._channels = list(channels)
        self._samples = sorted(samples, key=lambda s: s.timestamp_ticks)

    def get_channels(self) -> List[ChannelDescriptor]:
        return self._channels

    def get_sample_count(self) -> int:
        return len(self._samples)

    def stream_samples(
        self, cancel_event: Optional[threading.Event] = None
    ) -> Iterator[SampleRow]:
        for s in self._samples:
            yield s


def _export_to_lines(
    source: SampleSource,
    options: Optional[CsvExportOptions] = None,
    progress=None,
    cancel_event: Optional[threading.Event] = None,
):
    """Run the exporter against a StringIO and split into trimmed lines."""
    sw = io.StringIO()
    CsvExporter().export(source, sw, options or CsvExportOptions(), progress, cancel_event)
    content = sw.getvalue()
    lines = [ln.rstrip("\r") for ln in content.split("\n") if ln]
    return lines, (lines[0] if lines else "")


# ---------------------------------------------------------------------------
# ChannelDescriptor / SampleRow shape
# ---------------------------------------------------------------------------


class TestChannelDescriptor:
    def test_key_format(self):
        assert CH1.key == "DevA:SN001:Channel1"

    def test_key_includes_channel_type_neutral(self):
        # Key only includes device + serial + name; type is metadata.
        assert ":Channel1" in CH1.key
        assert "Analog" not in CH1.key

    def test_records_are_frozen(self):
        with pytest.raises(Exception):
            CH1.device_name = "x"  # type: ignore[misc]


class TestSampleRow:
    def test_fields(self):
        s = SampleRow(T0, CH1.key, 1.5)
        assert s.timestamp_ticks == T0
        assert s.channel_key == CH1.key
        assert s.value == 1.5

    def test_records_are_frozen(self):
        s = SampleRow(T0, CH1.key, 1.5)
        with pytest.raises(Exception):
            s.value = 2.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


class TestHeader:
    def test_absolute_time_writes_time_header(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        _, header = _export_to_lines(source)
        assert header.startswith("Time,")

    def test_relative_time_writes_relative_time_header(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        _, header = _export_to_lines(source, CsvExportOptions(use_relative_time=True))
        assert header.startswith("Relative Time (s),")

    def test_multiple_channels_writes_all_keys_in_header(self):
        source = _InMemorySampleSource([CH1, CH2], [SampleRow(T0, CH1.key, 1.0)])
        _, header = _export_to_lines(source)
        assert CH1.key in header
        assert CH2.key in header

    def test_no_channels_writes_nothing(self):
        source = _InMemorySampleSource([], [])
        sw = io.StringIO()
        CsvExporter().export(source, sw, CsvExportOptions())
        assert sw.getvalue() == ""

    def test_no_channels_still_finalizes_progress(self):
        # Regression: a no-op export with a progress callback should
        # still finalize at 100% so UI bars don't stall.
        source = _InMemorySampleSource([], [])
        sw = io.StringIO()
        reports = []
        CsvExporter().export(
            source, sw, CsvExportOptions(), progress=lambda p: reports.append(p)
        )
        assert reports == [100]

    def test_custom_delimiter_used_in_header(self):
        source = _InMemorySampleSource([CH1, CH2], [SampleRow(T0, CH1.key, 1.0)])
        _, header = _export_to_lines(source, CsvExportOptions(delimiter=";"))
        assert header == f"Time;{CH1.key};{CH2.key}"


# ---------------------------------------------------------------------------
# Absolute vs relative timestamps
# ---------------------------------------------------------------------------


class TestTimestampFormats:
    def test_absolute_time_iso_8601_round_trip(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 42.0)])
        lines, _ = _export_to_lines(source)
        data = lines[1]
        # T0 = 2024-01-01 00:00:00 UTC → "2024-01-01T00:00:00.0000000Z"
        assert data.startswith("2024-01-01T00:00:00.0000000Z,")

    def test_relative_time_first_row_is_zero(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        lines, _ = _export_to_lines(source, CsvExportOptions(use_relative_time=True))
        assert lines[1].startswith("0.000,")

    def test_relative_time_subsequent_rows_show_elapsed_seconds(self):
        source = _InMemorySampleSource(
            [CH1],
            [SampleRow(T0, CH1.key, 1.0), SampleRow(T1, CH1.key, 2.0)],
        )
        lines, _ = _export_to_lines(source, CsvExportOptions(use_relative_time=True))
        assert lines[1].startswith("0.000,")
        assert lines[2].startswith("1.000,")

    def test_invalid_ticks_render_as_invalid_marker_absolute(self):
        source = _InMemorySampleSource([CH1], [SampleRow(-1, CH1.key, 1.0)])
        lines, _ = _export_to_lines(source)
        assert lines[1].startswith("INVALID(-1),")

    def test_invalid_ticks_render_as_invalid_marker_relative(self):
        source = _InMemorySampleSource([CH1], [SampleRow(-5, CH1.key, 1.0)])
        lines, _ = _export_to_lines(source, CsvExportOptions(use_relative_time=True))
        assert lines[1].startswith("INVALID(-5),")

    def test_zero_ticks_is_valid_datetime_min_value(self):
        # ticks==0 is .NET DateTime.MinValue (0001-01-01 00:00:00 UTC),
        # NOT INVALID. Upstream daqifi-core PR #167 has the same off-by-
        # one bug (`<= 0` instead of `< 0`); filed as daqifi-core#192.
        source = _InMemorySampleSource([CH1], [SampleRow(0, CH1.key, 1.0)])
        lines, _ = _export_to_lines(source)
        assert lines[1].startswith("0001-01-01T00:00:00.0000000Z,")

    def test_high_precision_value_round_trips(self):
        # Regression: prior `f"{value:g}"` rounded to 6 sig digits,
        # losing precision on real-world high-precision samples.
        # Now uses `:.15g` to match C# "G" default for double.
        precise = 3.141592653589793
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, precise)])
        lines, _ = _export_to_lines(source)
        # Last column = the value; full 15 sig digits preserved.
        assert lines[1].endswith(",3.14159265358979")


# ---------------------------------------------------------------------------
# Multi-tick grouping (one row per distinct timestamp)
# ---------------------------------------------------------------------------


class TestTimestampGrouping:
    def test_samples_with_same_tick_emit_single_row(self):
        source = _InMemorySampleSource(
            [CH1, CH2],
            [
                SampleRow(T0, CH1.key, 1.0),
                SampleRow(T0, CH2.key, 2.0),
            ],
        )
        lines, _ = _export_to_lines(source)
        # One header + one data row.
        assert len(lines) == 2
        assert "1," in lines[1] or lines[1].endswith(",1,2")
        assert lines[1].endswith(",1,2")

    def test_samples_with_distinct_ticks_emit_separate_rows(self):
        source = _InMemorySampleSource(
            [CH1, CH2],
            [
                SampleRow(T0, CH1.key, 1.0),
                SampleRow(T0, CH2.key, 2.0),
                SampleRow(T1, CH1.key, 3.0),
                SampleRow(T1, CH2.key, 4.0),
            ],
        )
        lines, _ = _export_to_lines(source)
        # Header + 2 data rows.
        assert len(lines) == 3
        assert lines[1].endswith(",1,2")
        assert lines[2].endswith(",3,4")

    def test_missing_channel_value_renders_as_empty_field(self):
        source = _InMemorySampleSource(
            [CH1, CH2],
            [
                SampleRow(T0, CH1.key, 1.0),  # CH2 has no value at T0
                SampleRow(T1, CH2.key, 2.0),  # CH1 has no value at T1
            ],
        )
        lines, _ = _export_to_lines(source)
        assert lines[1].endswith(",1,")  # empty CH2 column
        assert lines[2].endswith(",,2")  # empty CH1 column

    def test_unknown_channel_key_in_sample_is_silently_dropped(self):
        # Sample for a channel not in the source's channel list — value
        # is simply not emitted (no extra column).
        source = _InMemorySampleSource(
            [CH1],
            [
                SampleRow(T0, CH1.key, 1.0),
                SampleRow(T0, "ghost:channel", 99.0),
            ],
        )
        lines, _ = _export_to_lines(source)
        assert lines[1].endswith(",1")


# ---------------------------------------------------------------------------
# Average-window aggregation
# ---------------------------------------------------------------------------


class TestAverageWindow:
    def test_window_of_1_yields_one_row_per_sample(self):
        source = _InMemorySampleSource(
            [CH1],
            [
                SampleRow(T0, CH1.key, 1.0),
                SampleRow(T1, CH1.key, 3.0),
                SampleRow(T2, CH1.key, 5.0),
            ],
        )
        lines, _ = _export_to_lines(source, CsvExportOptions(average_window=1))
        # Header + 3 data rows.
        assert len(lines) == 4
        assert lines[1].endswith(",1")
        assert lines[2].endswith(",3")
        assert lines[3].endswith(",5")

    def test_window_averages_per_channel(self):
        source = _InMemorySampleSource(
            [CH1, CH2],
            [
                SampleRow(T0, CH1.key, 1.0),
                SampleRow(T0, CH2.key, 10.0),
                SampleRow(T1, CH1.key, 3.0),
                SampleRow(T1, CH2.key, 20.0),
            ],
        )
        # 4 samples / window=4 → one averaged row.
        lines, _ = _export_to_lines(source, CsvExportOptions(average_window=4))
        assert len(lines) == 2
        # CH1 avg = (1+3)/2 = 2; CH2 avg = (10+20)/2 = 15.
        assert lines[1].endswith(",2,15")

    def test_trailing_partial_window_is_flushed(self):
        source = _InMemorySampleSource(
            [CH1],
            [
                SampleRow(T0, CH1.key, 2.0),
                SampleRow(T1, CH1.key, 4.0),
                SampleRow(T2, CH1.key, 6.0),
            ],
        )
        # window=2 → first row is avg of (2,4)=3; trailing (6,) flushed
        # as avg of (6,)=6.
        lines, _ = _export_to_lines(source, CsvExportOptions(average_window=2))
        assert len(lines) == 3
        assert lines[1].endswith(",3")
        assert lines[2].endswith(",6")

    def test_zero_window_raises(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        sw = io.StringIO()
        with pytest.raises(ValueError, match="average_window"):
            CsvExporter().export(
                source, sw, CsvExportOptions(average_window=0)
            )

    def test_negative_window_raises(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        sw = io.StringIO()
        with pytest.raises(ValueError, match="average_window"):
            CsvExporter().export(
                source, sw, CsvExportOptions(average_window=-3)
            )

    def test_channel_with_no_samples_in_window_renders_empty(self):
        source = _InMemorySampleSource(
            [CH1, CH2],
            [
                SampleRow(T0, CH1.key, 1.0),
                SampleRow(T1, CH1.key, 3.0),
            ],
        )
        lines, _ = _export_to_lines(source, CsvExportOptions(average_window=2))
        # CH2 had 0 samples → empty field.
        assert lines[1].endswith(",2,")


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


class TestProgress:
    def test_progress_always_reports_100_on_completion_all_samples(self):
        source = _InMemorySampleSource(
            [CH1], [SampleRow(T0 + i * 10_000_000, CH1.key, float(i)) for i in range(3)]
        )
        reports = []
        _export_to_lines(source, progress=lambda p: reports.append(p))
        assert reports[-1] == 100

    def test_progress_always_reports_100_on_completion_averaged(self):
        source = _InMemorySampleSource(
            [CH1], [SampleRow(T0 + i * 10_000_000, CH1.key, float(i)) for i in range(3)]
        )
        reports = []
        _export_to_lines(
            source,
            CsvExportOptions(average_window=2),
            progress=lambda p: reports.append(p),
        )
        assert reports[-1] == 100

    def test_progress_skipped_when_callback_is_none(self):
        # No callback → exporter doesn't call get_sample_count.
        called = {"count": 0}

        class _CountingSource(_InMemorySampleSource):
            def get_sample_count(self):
                called["count"] += 1
                return super().get_sample_count()

        source = _CountingSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        _export_to_lines(source)
        assert called["count"] == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_pre_set_cancel_event_aborts_immediately(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        ev = threading.Event()
        ev.set()
        sw = io.StringIO()
        with pytest.raises(InterruptedError):
            CsvExporter().export(source, sw, CsvExportOptions(), cancel_event=ev)

    def test_mid_stream_cancel_aborts_loop(self):
        # Source that sets the event after the first yielded sample.
        ev = threading.Event()

        class _CancellingSource(_InMemorySampleSource):
            def stream_samples(self, cancel_event=None):
                for i, s in enumerate(self._samples):
                    yield s
                    if i == 0:
                        ev.set()

        source = _CancellingSource(
            [CH1],
            [
                SampleRow(T0, CH1.key, 1.0),
                SampleRow(T1, CH1.key, 2.0),
                SampleRow(T2, CH1.key, 3.0),
            ],
        )
        sw = io.StringIO()
        with pytest.raises(InterruptedError):
            CsvExporter().export(source, sw, CsvExportOptions(), cancel_event=ev)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestCsvHeaderEscaping:
    def test_channel_name_containing_delimiter_is_quoted(self):
        ch = ChannelDescriptor("DevA", "SN001", "name,with,commas", ChannelType.Analog)
        source = _InMemorySampleSource([ch], [SampleRow(T0, ch.key, 1.0)])
        _, header = _export_to_lines(source)
        # The escaped key appears as a quoted field — original commas
        # don't break column alignment.
        assert '"DevA:SN001:name,with,commas"' in header

    def test_channel_name_containing_quote_doubled_inside_quotes(self):
        ch = ChannelDescriptor("DevA", "SN001", 'name"with"quote', ChannelType.Analog)
        source = _InMemorySampleSource([ch], [SampleRow(T0, ch.key, 1.0)])
        _, header = _export_to_lines(source)
        # RFC 4180: literal " becomes ""; whole field is quoted.
        assert '"DevA:SN001:name""with""quote"' in header

    def test_formula_leading_character_is_neutralized(self):
        # When the *device name* (which is the first component of the
        # composite key, and therefore the start of the CSV field)
        # begins with =/+/-/@, the protection prefixes a literal '.
        # Formula chars deeper in the key (channel name) don't trigger
        # the protection because they're already preceded by the
        # device:serial: prefix and so cannot be interpreted as a
        # formula by spreadsheet apps.
        ch = ChannelDescriptor("=DevA", "SN001", "Channel1", ChannelType.Analog)
        source = _InMemorySampleSource([ch], [SampleRow(T0, ch.key, 1.0)])
        _, header = _export_to_lines(source)
        assert "'=DevA:SN001:Channel1" in header

    def test_whitespace_prefixed_formula_still_neutralized(self):
        # " =SUM(A1)" — leading space bypasses a naive value[0] check
        # but spreadsheets still interpret it as a formula. The escape
        # function strips leading whitespace before the formula-char
        # check so this case is caught too.
        ch = ChannelDescriptor(" =DevA", "SN001", "Channel1", ChannelType.Analog)
        source = _InMemorySampleSource([ch], [SampleRow(T0, ch.key, 1.0)])
        _, header = _export_to_lines(source)
        # Field is prefixed with literal ' to force text mode in
        # Excel/Sheets/LibreOffice; whitespace is preserved.
        assert "' =DevA:SN001:Channel1" in header

    def test_tab_prefixed_formula_still_neutralized(self):
        ch = ChannelDescriptor("\t=DevA", "SN001", "Channel1", ChannelType.Analog)
        source = _InMemorySampleSource([ch], [SampleRow(T0, ch.key, 1.0)])
        _, header = _export_to_lines(source)
        assert "'\t=DevA:SN001:Channel1" in header

    def test_no_escaping_for_normal_keys(self):
        # Normal channel keys pass through unchanged.
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        _, header = _export_to_lines(source)
        assert "DevA:SN001:Channel1" in header
        assert '"' not in header  # no quoting needed


class TestDelimiterValidation:
    def test_empty_delimiter_raises(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        sw = io.StringIO()
        with pytest.raises(ValueError, match="delimiter"):
            CsvExporter().export(source, sw, CsvExportOptions(delimiter=""))

    def test_multi_char_delimiter_raises(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        sw = io.StringIO()
        with pytest.raises(ValueError, match="delimiter"):
            CsvExporter().export(source, sw, CsvExportOptions(delimiter=";;"))

    def test_newline_delimiter_raises(self):
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        sw = io.StringIO()
        with pytest.raises(ValueError, match="delimiter"):
            CsvExporter().export(source, sw, CsvExportOptions(delimiter="\n"))

    def test_double_quote_delimiter_raises(self):
        # The " character is reserved as the RFC 4180 quoting char by
        # _escape_csv_field; allowing it as the delimiter would produce
        # ambiguous output.
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        sw = io.StringIO()
        with pytest.raises(ValueError, match="delimiter"):
            CsvExporter().export(source, sw, CsvExportOptions(delimiter='"'))


class TestDataRowEscaping:
    def test_negative_values_not_quoted_or_apostrophe_prefixed(self):
        # Regression: pass-7 fix applied formula-injection mitigation
        # to data values, which incorrectly prefixed negative numbers
        # (whose leading '-' is a sign, not a formula char) with `'`.
        # Data fields use formula_safe=False so this no longer happens.
        source = _InMemorySampleSource(
            [CH1],
            [SampleRow(T0, CH1.key, -1.5), SampleRow(T0 + 10_000_000, CH1.key, -0.001)],
        )
        sw = io.StringIO()
        CsvExporter().export(source, sw, CsvExportOptions(use_relative_time=True))
        body = sw.getvalue().splitlines()
        # Rows must be plain floats, no quoting, no apostrophe prefix.
        assert body[1] == "0.000,-1.5"
        assert body[2] == "1.000,-0.001"

    def test_colon_delimiter_quotes_iso_timestamp(self):
        # ISO 8601 absolute timestamps inherently contain ':' — when
        # the user picks ':' as a delimiter, the timestamp field must
        # be RFC 4180 quoted so it stays a single CSV field.
        source = _InMemorySampleSource([CH1], [SampleRow(T0, CH1.key, 1.0)])
        sw = io.StringIO()
        CsvExporter().export(source, sw, CsvExportOptions(delimiter=":"))
        # Header has 'Time' (no quoting needed) then ':' then quoted
        # composite key (which contains ':'). Body row's timestamp
        # field must be quoted because it contains ':'.
        body_lines = sw.getvalue().splitlines()
        assert len(body_lines) == 2
        # Body row should look like: "2026-...Z":1
        assert body_lines[1].startswith('"')
        assert '":' in body_lines[1]  # quoted timestamp followed by delim

    def test_dot_delimiter_quotes_relative_timestamp_and_value(self):
        # Relative timestamps and float values both contain '.'; when
        # '.' is the delimiter both must be quoted.
        source = _InMemorySampleSource(
            [CH1],
            [SampleRow(T0, CH1.key, 0.5), SampleRow(T0 + 10_000_000, CH1.key, 1.5)],
        )
        sw = io.StringIO()
        CsvExporter().export(
            source,
            sw,
            CsvExportOptions(delimiter=".", use_relative_time=True),
        )
        body = sw.getvalue().splitlines()
        # First data row: relative time "0.000" and value "0.5" both
        # contain '.', so both must be quoted.
        assert body[1] == '"0.000"."0.5"'
        assert body[2] == '"1.000"."1.5"'


class TestSampleSourceProtocol:
    def test_duck_typed_object_is_runtime_isinstance(self):
        # SampleSource is @runtime_checkable so a duck-typed object
        # passes isinstance() without subclassing.
        class _Quack:
            def get_channels(self):
                return [CH1]

            def get_sample_count(self):
                return 0

            def stream_samples(self, cancel_event=None):
                return iter([])

        assert isinstance(_Quack(), SampleSource)

    def test_missing_method_fails_isinstance(self):
        class _Incomplete:
            def get_channels(self):
                return []

        assert not isinstance(_Incomplete(), SampleSource)

    def test_duck_typed_source_works_with_exporter(self):
        # No inheritance from SampleSource — pure duck typing.
        class _Quack:
            def get_channels(self):
                return [CH1]

            def get_sample_count(self):
                return 1

            def stream_samples(self, cancel_event=None):
                yield SampleRow(T0, CH1.key, 5.0)

        sw = io.StringIO()
        CsvExporter().export(_Quack(), sw, CsvExportOptions())
        # Header + one data row.
        lines = [ln for ln in sw.getvalue().split("\n") if ln]
        assert len(lines) == 2
        assert lines[1].endswith(",5")


class TestEdgeCases:
    def test_empty_samples_writes_only_header(self):
        source = _InMemorySampleSource([CH1], [])
        lines, header = _export_to_lines(source)
        assert lines == [header]

    def test_digital_channel_passes_through(self):
        source = _InMemorySampleSource(
            [CH_DIG], [SampleRow(T0, CH_DIG.key, 1.0)]
        )
        lines, header = _export_to_lines(source)
        assert CH_DIG.key in header
        assert lines[1].endswith(",1")

    def test_relative_time_three_decimal_precision(self):
        # Half-second offset → 0.500
        source = _InMemorySampleSource(
            [CH1],
            [
                SampleRow(T0, CH1.key, 1.0),
                SampleRow(T0 + 5_000_000, CH1.key, 2.0),  # +0.5 s
            ],
        )
        lines, _ = _export_to_lines(source, CsvExportOptions(use_relative_time=True))
        assert lines[2].startswith("0.500,")
