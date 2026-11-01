"""CSV export for logging sessions.

Ports ``Daqifi.Core.Logging.Export`` (daqifi-core PR #167) to Python.
Centralizes the namespace under ``daqifi.export`` rather than
``daqifi.logging.export`` because the latter would shadow the
stdlib :mod:`logging` module on imports — a sharp foot-gun for
callers.

Public surface mirrors upstream:

- :class:`ChannelDescriptor` — channel identity (device + serial + name + kind)
- :class:`SampleRow` — one timestamped channel value
- :class:`SampleSource` — Protocol that abstracts the data origin
- :class:`CsvExportOptions` — formatting controls
- :class:`CsvExporter` — writes CSV to any text-writable stream

Design adaptations vs upstream:

- Async → sync per project convention (``async Task ExportAsync`` becomes
  ``def export``; ``IAsyncEnumerable<SampleRow>`` becomes
  ``Iterator[SampleRow]``).
- C# ``IProgress<int>`` becomes ``Optional[Callable[[int], None]]``.
- C# ``CancellationToken`` becomes ``Optional[threading.Event]`` (same
  pattern used by :class:`DefaultFirmwareUpdateService`).
- C# ``record`` types become frozen ``@dataclass`` instances.
- C# ``DateTime.Ticks`` are 100-nanosecond intervals since
  ``0001-01-01 00:00:00 UTC``; preserved as ``int`` on the wire and
  converted to ISO 8601 via the .NET epoch for the absolute-time
  output mode.
"""

from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass
from typing import IO, Callable, Iterator, List, Optional, Protocol, runtime_checkable

from ._internal.scpi import ChannelType


# ``DateTime.MinValue`` in .NET is 0001-01-01 00:00:00.
_DOTNET_EPOCH = _dt.datetime(1, 1, 1, tzinfo=_dt.timezone.utc)
# C# ``TimeSpan.TicksPerSecond`` — one tick is 100 ns.
_TICKS_PER_SECOND = 10_000_000
# .NET ``DateTime.MaxValue.Ticks`` — 9999-12-31 23:59:59.9999999.
_DOTNET_MAX_TICKS = 3_155_378_975_999_999_999


@dataclass(frozen=True)
class ChannelDescriptor:
    """Identifies a channel by device and channel name.

    Mirrors upstream ``ChannelDescriptor``. The composite :attr:`key`
    is the join used in CSV column headers and as the lookup key in
    :class:`SampleRow`.
    """

    device_name: str
    device_serial_no: str
    channel_name: str
    channel_type: ChannelType

    @property
    def key(self) -> str:
        """The composite key used to identify this channel in CSV
        headers and sample rows. Format:
        ``{device_name}:{device_serial_no}:{channel_name}``.
        """
        return f"{self.device_name}:{self.device_serial_no}:{self.channel_name}"


@dataclass(frozen=True)
class SampleRow:
    """A single data point — one channel value at one point in time.

    Mirrors upstream ``SampleRow``.
    """

    #: .NET ``DateTime.Ticks`` value for this sample (100-nanosecond
    #: intervals since 0001-01-01 UTC). The stream from
    #: :meth:`SampleSource.stream_samples` must be ordered by this
    #: value ascending.
    timestamp_ticks: int

    #: The composite channel identifier — must match a
    #: :attr:`ChannelDescriptor.key` returned by
    #: :meth:`SampleSource.get_channels`.
    channel_key: str

    #: The numeric value of the sample.
    value: float


@runtime_checkable
class SampleSource(Protocol):
    """Protocol for the data source feeding a CSV export.

    Mirrors upstream ``ISampleSource``. Hides storage details (SQL
    backends, SD-card files, in-memory buffers, etc.) from the
    exporter — implementers only need to expose channel metadata, a
    sample count for progress reporting, and an ordered stream of
    samples.

    Defined as a :class:`typing.Protocol` so type checkers (mypy,
    pyright) accept any duck-typed object that exposes the three
    methods, with no inheritance required. The ``@runtime_checkable``
    decorator also lets callers use ``isinstance`` checks if they
    need a runtime guard. Mirrors C# ``interface ISampleSource`` in
    spirit (a structural contract) without C#'s nominal-typing
    requirement that implementers explicitly declare conformance.
    """

    def get_channels(self) -> List[ChannelDescriptor]:
        """Return the ordered list of channels present in this source.

        The order determines column order in the exported CSV.
        """
        ...

    def get_sample_count(self) -> int:
        """Return the total number of sample rows, used for progress
        reporting. May return ``0`` if the count is unavailable.

        Sync per project convention (upstream is async because EF Core
        backends may need a round-trip; sync callers can offload to a
        thread if needed).
        """
        ...

    def stream_samples(
        self, cancel_event: Optional[threading.Event] = None
    ) -> Iterator[SampleRow]:
        """Yield all samples in ascending :attr:`SampleRow.timestamp_ticks`
        order.

        ``cancel_event`` lets a caller signal mid-stream
        cancellation cooperatively (mirrors C# ``CancellationToken``).
        """
        ...


@dataclass(frozen=True)
class CsvExportOptions:
    """Controls how :class:`CsvExporter` formats a CSV export.

    Mirrors upstream ``CsvExportOptions``.
    """

    #: Field delimiter. Defaults to ``","``.
    delimiter: str = ","

    #: When ``True``, the time column contains seconds elapsed since
    #: the first sample, to :attr:`relative_time_decimals` places. When
    #: ``False``, it contains an ISO 8601 round-trip timestamp.
    use_relative_time: bool = False

    #: When ``None``, every sample is written as its own row (grouped
    #: by timestamp tick). When set to a positive integer ``N``,
    #: samples are accumulated in a rolling window of ``N`` total
    #: samples (across all channels) and one averaged row is written
    #: per window.
    average_window: Optional[int] = None
    #: Decimal places of the relative time column, 0 to 7 (7 is the tick
    #: resolution, 100 ns). The default 3 (1 ms, as upstream) gives
    #: samples above 1 kHz repeated labels (2 kHz: 0.000, 0.001, 0.001,
    #: ...); use 6 or 7 for fast streams (#195). Last, so existing
    #: positional calls keep their meaning.
    relative_time_decimals: int = 3


class CsvExporter:
    """Exports timestamped channel samples to CSV.

    Mirrors upstream ``CsvExporter``. Storage concerns are delegated
    to a :class:`SampleSource` instance; this class contains only
    row-formatting and timestamp logic.
    """

    _PROGRESS_REPORT_EVERY = 5000

    def export(
        self,
        source: SampleSource,
        writer: IO[str],
        options: Optional[CsvExportOptions] = None,
        progress: Optional[Callable[[int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Export the samples provided by ``source`` as CSV to
        ``writer``.

        Args:
            source: The data source being exported.
            writer: The destination text stream. Caller closes it.
            options: Formatting options. Defaults to
                ``CsvExportOptions()``.
            progress: Optional callable receiving values in [0, 100].
                Reported periodically as samples are processed; always
                called with ``100`` on completion.
            cancel_event: Optional ``threading.Event`` checked between
                samples — set it to abort the export mid-stream.

        Raises:
            ValueError: when ``options.average_window`` is set to a
                value less than or equal to zero (mirrors upstream's
                ``ArgumentOutOfRangeException``).
        """
        if options is None:
            options = CsvExportOptions()

        if options.average_window is not None and options.average_window <= 0:
            raise ValueError(
                f"options.average_window must be greater than zero "
                f"(got {options.average_window})"
            )
        if (
            isinstance(options.relative_time_decimals, bool)
            or not isinstance(options.relative_time_decimals, int)
            or not 0 <= options.relative_time_decimals <= 7
        ):
            raise ValueError(
                f"options.relative_time_decimals must be an int from 0 to 7 "
                f"(got {options.relative_time_decimals!r})"
            )
        if (
            len(options.delimiter) != 1
            or options.delimiter in "\r\n"
            or options.delimiter == '"'
        ):
            # The double-quote is reserved as the RFC 4180 quoting
            # character used by `_escape_csv_field`; using it as the
            # delimiter would produce ambiguous, unparseable output.
            raise ValueError(
                f"options.delimiter must be a single character that is "
                f"not a newline or double-quote (got {options.delimiter!r})"
            )

        _check_cancel(cancel_event)

        channels = list(source.get_channels())
        if not channels:
            # Always finalize progress so callers (e.g. UI progress
            # bars) don't stall at <100% when the export is a no-op.
            # Internal consistency with the success paths below; the
            # same gap exists in upstream daqifi-core PR #167.
            if progress is not None:
                progress(100)
            return

        channel_keys = [c.key for c in channels]
        self._write_header(writer, channel_keys, options)

        total_samples = source.get_sample_count() if progress is not None else 0

        if options.average_window is not None:
            self._export_averaged(
                source,
                writer,
                options,
                channel_keys,
                total_samples,
                progress,
                cancel_event,
            )
        else:
            self._export_all_samples(
                source,
                writer,
                options,
                channel_keys,
                total_samples,
                progress,
                cancel_event,
            )

    @staticmethod
    def _write_header(
        writer: IO[str],
        channel_keys: List[str],
        options: CsvExportOptions,
    ) -> None:
        time_header = "Relative Time (s)" if options.use_relative_time else "Time"
        writer.write(_escape_csv_field(time_header, options.delimiter))
        for key in channel_keys:
            writer.write(options.delimiter)
            writer.write(_escape_csv_field(key, options.delimiter))
        writer.write("\n")

    def _export_all_samples(
        self,
        source: SampleSource,
        writer: IO[str],
        options: CsvExportOptions,
        channel_keys: List[str],
        total_samples: int,
        progress: Optional[Callable[[int], None]],
        cancel_event: Optional[threading.Event],
    ) -> None:
        first_ticks: Optional[int] = None
        current_tick: Optional[int] = None
        bucket: List[SampleRow] = []
        processed = 0
        reported = 0

        for sample in source.stream_samples(cancel_event):
            _check_cancel(cancel_event)
            if first_ticks is None:
                first_ticks = sample.timestamp_ticks

            if current_tick is not None and sample.timestamp_ticks != current_tick:
                self._write_timestamp_row(
                    writer, bucket, channel_keys, first_ticks, options
                )
                processed += len(bucket)
                reported = self._report_progress(progress, processed, total_samples, reported)
                bucket = []

            current_tick = sample.timestamp_ticks
            bucket.append(sample)

        if bucket:
            assert first_ticks is not None  # bucket non-empty implies we set it
            self._write_timestamp_row(
                writer, bucket, channel_keys, first_ticks, options
            )

        if progress is not None:
            progress(100)

    @staticmethod
    def _write_timestamp_row(
        writer: IO[str],
        bucket: List[SampleRow],
        channel_keys: List[str],
        first_ticks: int,
        options: CsvExportOptions,
    ) -> None:
        ticks = bucket[0].timestamp_ticks
        timestamp = _format_timestamp(
            ticks, first_ticks, options.use_relative_time, options.relative_time_decimals
        )
        parts: List[str] = [
            _escape_csv_field(timestamp, options.delimiter, formula_safe=False)
        ]

        # Last write wins for the same key within a bucket — mirrors
        # upstream's Dictionary[key] = value pattern.
        lookup = {row.channel_key: row.value for row in bucket}
        for key in channel_keys:
            parts.append(options.delimiter)
            if key in lookup:
                parts.append(
                    _escape_csv_field(
                        _format_value(lookup[key]),
                        options.delimiter,
                        formula_safe=False,
                    )
                )

        parts.append("\n")
        writer.write("".join(parts))

    def _export_averaged(
        self,
        source: SampleSource,
        writer: IO[str],
        options: CsvExportOptions,
        channel_keys: List[str],
        total_samples: int,
        progress: Optional[Callable[[int], None]],
        cancel_event: Optional[threading.Event],
    ) -> None:
        assert options.average_window is not None  # validated by caller
        window = options.average_window
        totals = {k: 0.0 for k in channel_keys}
        counts = {k: 0 for k in channel_keys}
        first_ticks: Optional[int] = None
        last_tick = 0
        window_count = 0
        processed = 0
        reported = 0

        for sample in source.stream_samples(cancel_event):
            _check_cancel(cancel_event)
            if first_ticks is None:
                first_ticks = sample.timestamp_ticks
            last_tick = sample.timestamp_ticks

            if sample.channel_key in totals:
                totals[sample.channel_key] += sample.value
                counts[sample.channel_key] += 1

            window_count += 1
            processed += 1

            if window_count >= window:
                self._write_averaged_row(
                    writer,
                    channel_keys,
                    totals,
                    counts,
                    last_tick,
                    first_ticks,
                    options,
                )
                for key in channel_keys:
                    totals[key] = 0.0
                    counts[key] = 0
                window_count = 0

                reported = self._report_progress(progress, processed, total_samples, reported)

        # Flush any trailing samples that didn't fill a complete window.
        if window_count > 0 and first_ticks is not None:
            self._write_averaged_row(
                writer,
                channel_keys,
                totals,
                counts,
                last_tick,
                first_ticks,
                options,
            )

        if progress is not None:
            progress(100)

    @staticmethod
    def _write_averaged_row(
        writer: IO[str],
        channel_keys: List[str],
        totals: dict,
        counts: dict,
        last_tick: int,
        first_ticks: int,
        options: CsvExportOptions,
    ) -> None:
        timestamp = _format_timestamp(
            last_tick, first_ticks, options.use_relative_time, options.relative_time_decimals
        )
        parts: List[str] = [
            _escape_csv_field(timestamp, options.delimiter, formula_safe=False)
        ]
        for key in channel_keys:
            parts.append(options.delimiter)
            if counts[key] > 0:
                avg = totals[key] / counts[key]
                parts.append(
                    _escape_csv_field(
                        _format_value(avg), options.delimiter, formula_safe=False
                    )
                )
        parts.append("\n")
        writer.write("".join(parts))

    @classmethod
    def _report_progress(
        cls,
        progress: Optional[Callable[[int], None]],
        processed: int,
        total: int,
        reported: int,
    ) -> int:
        """Report (capped at 99) each time ``processed`` passes another
        multiple of 5000, and return how many multiples have been passed.

        Upstream reports only when the count lands exactly on a multiple,
        but it grows by a whole timestamp's samples at a time, so with 7
        channels 70,000 samples reported only twice (#195)."""
        crossed = processed // cls._PROGRESS_REPORT_EVERY
        if progress is None or total <= 0:
            return crossed
        if crossed > reported:
            progress(min(99, int((processed / total) * 100)))
        return crossed


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    """Raise :class:`InterruptedError` if ``cancel_event`` is set.

    Mirrors C# ``CancellationToken.ThrowIfCancellationRequested()``.
    The Python equivalent for cooperative cancellation in this
    codebase is :class:`threading.Event` (see
    :class:`DefaultFirmwareUpdateService`); raising
    :class:`InterruptedError` lets callers catch a stdlib exception
    rather than a project-specific one.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("CSV export was cancelled by caller.")


def _format_timestamp(
    ticks: int, first_ticks: int, use_relative_time: bool, relative_decimals: int = 3
) -> str:
    """Format a tick value as an ISO 8601 absolute string or as
    seconds elapsed since ``first_ticks``.

    Mirrors upstream ``FormatTimestamp``. Ticks outside the .NET
    ``DateTime`` valid range render as ``INVALID({ticks})`` in both
    modes — same defensive shape as upstream.
    """
    # Treat negative ticks as invalid; ticks==0 is .NET DateTime.MinValue
    # (0001-01-01 00:00:00 UTC) — a legal value the formatter handles
    # below. Upstream daqifi-core PR #167 has a `ticks <= 0` check that
    # incorrectly classifies MinValue as invalid; filed as
    # daqifi-core#192.
    if ticks < 0 or ticks > _DOTNET_MAX_TICKS:
        return f"INVALID({ticks})"

    if use_relative_time:
        seconds = (ticks - first_ticks) / _TICKS_PER_SECOND
        return f"{seconds:.{relative_decimals}f}"

    # ISO 8601 round-trip (matches C# DateTime.ToString("O") shape:
    # `YYYY-MM-DDTHH:MM:SS.fffffffZ` for UTC, with 7-digit fractional
    # seconds — Python's isoformat gives 6 digits, so we hand-format
    # the .NET-compatible 7-digit form.)
    delta_ticks = ticks  # ticks since 0001-01-01 UTC
    seconds_total = delta_ticks // _TICKS_PER_SECOND
    sub_ticks = delta_ticks % _TICKS_PER_SECOND  # 0..9_999_999
    base = _DOTNET_EPOCH + _dt.timedelta(seconds=seconds_total)
    return (
        f"{base.year:04d}-{base.month:02d}-{base.day:02d}T"
        f"{base.hour:02d}:{base.minute:02d}:{base.second:02d}"
        f".{sub_ticks:07d}Z"
    )


def _escape_csv_field(value: str, delimiter: str, *, formula_safe: bool = True) -> str:
    """RFC 4180 quoting + optional spreadsheet formula-injection
    neutralization.

    Two scopes:

    - **Header fields** (default, ``formula_safe=True``) — channel
      names are user-controllable and may begin with ``=`` / ``+`` /
      ``-`` / ``@`` (or those characters preceded by whitespace),
      which Excel / LibreOffice / Google Sheets interpret as
      formulas (a published CVE class). The leading-quote prefix
      (``'``) forces text mode in spreadsheet readers.
    - **Data fields** (``formula_safe=False``) — timestamps and
      numeric values are internally generated by ``_format_timestamp``
      and ``_format_value``, so formula-injection mitigation does not
      apply. Critically, this avoids prepending ``'`` to negative
      numeric values like ``-1.5`` (whose leading ``-`` is a sign,
      not a formula).

    RFC 4180 quoting (delimiter / ``"`` / CR / LF inside the field
    triggers wrap-in-quotes plus quote-doubling) applies in both
    scopes — necessary so timestamps containing ``:`` or ``.`` stay
    a single field when those characters are picked as delimiters.

    Same hardening should land upstream — see daqifi-core#193.
    """
    if formula_safe and value:
        # Neutralize spreadsheet formula injection BEFORE the quote
        # decision so the inserted leading-quote is itself eligible
        # for quoting if needed. Strip leading whitespace before the
        # formula-char check — spreadsheets ignore leading whitespace
        # when detecting a formula (e.g. " =SUM(A1)" still evaluates).
        # Any Unicode whitespace, not just space and tab: injection
        # payloads put NBSP, thin spaces or U+2028 before "=" and
        # spreadsheets still evaluate the cell (upstream 4f3766f).
        leading = value.lstrip()
        if leading and leading[0] in ("=", "+", "-", "@"):
            value = "'" + value
    # Leading or trailing whitespace is quoted too: many readers (Excel,
    # Google Sheets, pandas) trim unquoted whitespace and lose it.
    must_quote = (bool(value) and (value[0].isspace() or value[-1].isspace())) or any(
        ch in value for ch in (delimiter, '"', "\r", "\n")
    )
    if must_quote:
        value = value.replace('"', '""')
        return f'"{value}"'
    return value


def _format_value(value: float) -> str:
    """Format a float matching C# ``"G"`` (general) culture-invariant
    semantics for ``double``: at least 15 significant digits, dropping
    trailing zeros. Python's ``repr(float)`` already emits the shortest
    round-trippable representation under IEEE-754, which matches C#
    ``G17`` round-trip semantics; ``"G"`` defaults to 15 digits for
    ``double``. We use ``f"{value:.15g}"`` for the closest tested
    parity. Notable difference vs the prior ``f"{value:g}"`` (which
    defaulted to 6 digits and would silently round high-precision
    samples).
    """
    return f"{value:.15g}"


__all__ = [
    "ChannelDescriptor",
    "SampleRow",
    "SampleSource",
    "CsvExportOptions",
    "CsvExporter",
]
