"""#195: CSV export.

1. Relative time was always written to 3 decimals (1 ms), so samples above
   1 kHz got repeated labels. It's now an option, 0 to 7 decimals.
2. Progress fired only when the running count landed exactly on a multiple
   of 5000; it grows a whole timestamp at a time, so with 7 channels 70,000
   samples reported [50, 100]. It now reports each time a multiple is passed.
3. Formula neutralisation skipped only space and tab before "=" etc., and a
   field with whitespace at its edges wasn't quoted (upstream 4f3766f).
4. (Deferred to #235: an SD-log SampleSource needs the channel identities
   #217 carries through the parser.)
"""

import datetime as dt
import io

import pytest

from daqifi._internal.scpi import ChannelType
from daqifi.export import ChannelDescriptor, CsvExporter, CsvExportOptions, SampleRow


class _Source:
    def __init__(self, channels, rows):
        self._channels = channels
        self._rows = rows

    def get_channels(self):
        return self._channels

    def get_sample_count(self):
        return len(self._rows)

    def stream_samples(self, cancel_event=None):
        return iter(self._rows)


def _channel(name):
    return ChannelDescriptor("Nq1", "1234", name, ChannelType.Analog)


def _export(source, options=None, progress=None):
    out = io.StringIO()
    CsvExporter().export(source, out, options, progress=progress)
    return out.getvalue().splitlines()


# ---- 1 ---------------------------------------------------------------------


def _two_khz():
    ch = _channel("AI0")
    # 2 kHz: one sample every 5000 ticks (0.5 ms).
    return _Source([ch], [SampleRow(i * 5000, ch.key, float(i)) for i in range(5)])


def test_relative_time_can_resolve_fast_samples() -> None:
    lines = _export(_two_khz(), CsvExportOptions(use_relative_time=True, relative_time_decimals=6))
    labels = [line.split(",")[0] for line in lines[1:]]
    assert labels == ["0.000000", "0.000500", "0.001000", "0.001500", "0.002000"]


def test_relative_time_defaults_to_three_decimals() -> None:
    lines = _export(_two_khz(), CsvExportOptions(use_relative_time=True))
    assert [line.split(",")[0] for line in lines[1:3]] == ["0.000", "0.001"]


@pytest.mark.parametrize("decimals", [-1, 8, 2.5, True])
def test_an_invalid_decimal_count_is_refused(decimals) -> None:
    with pytest.raises(ValueError, match="relative_time_decimals"):
        _export(_two_khz(), CsvExportOptions(relative_time_decimals=decimals))


# ---- 2 ---------------------------------------------------------------------


def test_progress_fires_as_multiples_of_5000_are_passed() -> None:
    channels = [_channel(f"AI{i}") for i in range(7)]
    rows = [
        SampleRow(t, ch.key, 1.0) for t in range(10_000) for ch in channels
    ]  # 70,000 samples, 7 per timestamp
    reports = []
    _export(_Source(channels, rows), progress=reports.append)
    assert len(reports) >= 13  # one per 5000 passed, then 100
    assert reports == sorted(reports)
    assert reports[-1] == 100
    assert max(reports[:-1]) <= 99


# ---- 3 ---------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["\u00a0", "\u2009", "\u2028", " \t"], ids=["nbsp", "thin", "u2028", "space-tab"])
def test_a_formula_behind_any_whitespace_is_neutralised(prefix) -> None:
    # The header field is the whole key, device name first.
    channel = ChannelDescriptor(prefix + "=SUM(A1)", "1234", "AI0", ChannelType.Analog)
    out = io.StringIO()
    CsvExporter().export(_Source([channel], []), out)
    header = out.getvalue().split("\n")[0]  # not splitlines(): U+2028 is a line break to it
    assert "'" + prefix + "=SUM(A1)" in header


def test_a_field_with_edge_whitespace_is_quoted() -> None:
    header = _export(_Source([_channel("AI0 ")], []))[0]
    assert header.endswith('"Nq1:1234:AI0 "')


# ---- audit r1 --------------------------------------------------------------


def test_positional_export_options_keep_their_meaning() -> None:
    # CsvExportOptions(delimiter, use_relative_time, average_window): the new
    # field went last, so a window of 2 still averages.
    ch = _channel("AI0")
    source = _Source([ch], [SampleRow(0, ch.key, 2.0), SampleRow(10_000_000, ch.key, 4.0)])
    lines = _export(source, CsvExportOptions(",", True, 2))
    assert lines[1:] == ["1.000,3"]
