"""#206: items carried forward from #163.

5. (Not fixed here: without the device's echo state, bytes alone can't
   tell an echo from file data. These pins keep main's behaviour.)
10. start_streaming capped the rate at 1000 Hz; the firmware's ceiling is
    STREAMING_ISR_MAX_HZ (22000), with a lower per-configuration cap it
    enforces itself.
11. TimestampProcessor added each step as a whole-microsecond timedelta to
    the previous timestamp, so the rounding accumulated.
17. An unusable CSV tick-rate header ("1²", or past uint32) aborted the parse.
23. The CSV row parser's ``except Exception`` swallowed MemoryError as a
    malformed row.
"""

import io
from datetime import timedelta

import pytest

from daqifi._internal import scpi_commands
from daqifi.device import NyquistDevice
from daqifi.sdcard import SdCardCsvFileParser, SdCardParseOptions
from daqifi.timestamp import TimestampProcessor


# ---- 5 ---------------------------------------------------------------------


class _SerialWithReply:
    out_waiting = 0  # #263: the send path drains this queue, bounded
    def __init__(self, reply: bytes):
        self.is_open = True
        self.written = []
        self._reply = bytearray(reply)

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self._reply)

    def read(self, n):
        out = bytes(self._reply[:n])
        del self._reply[:n]
        return out


def _download(reply: bytes) -> bytes:
    device = NyquistDevice("COM41")  # constructing doesn't open the port
    device._comm._serial = _SerialWithReply(reply)
    return device.sd_get_file_binary("log_a.bin", timeout=2.0)


def test_a_command_shaped_payload_is_kept_with_echo_off() -> None:
    # Audit r1: echo is off after open(), so a file that happens to begin
    # with the GET command's text is file data, not an echo.
    data = b'SYSTem:STORage:SD:GET "log_a.bin" is how this file was fetched\n'
    assert _download(data + b"__END_OF_FILE__") == data


def test_a_prompt_after_cr_lf_is_stripped() -> None:
    # Real firmware framing ahead of the payload.
    assert _download(b"\r\nDAQIFI> payload__END_OF_FILE__") == b"payload"


# ---- 10 --------------------------------------------------------------------


@pytest.mark.parametrize("rate", [1, 1001, 5000, 22000])
def test_start_streaming_accepts_the_firmwares_range(rate) -> None:
    assert scpi_commands.start_streaming(rate).data == f"SYSTem:STReam:START {rate}"


@pytest.mark.parametrize("rate", [0, 22001])
def test_start_streaming_refuses_outside_it(rate) -> None:
    with pytest.raises(ValueError, match="22000"):
        scpi_commands.start_streaming(rate)


# ---- 11 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "cycles_per_step, steps",
    [(1, 32000), (75, 1000)],  # 20 ns (rounds to 0 µs); 1.5 µs (rounds to 2)
    ids=["20ns", "1.5us"],
)
def test_timestamp_rounding_doesnt_accumulate(cycles_per_step, steps) -> None:
    processor = TimestampProcessor()
    first = processor.process_timestamp("d", 0)
    ts = 0
    for _ in range(steps):
        ts += cycles_per_step
        last = processor.process_timestamp("d", ts)
    elapsed = (last.timestamp - first.timestamp) / timedelta(microseconds=1)
    assert elapsed == pytest.approx(steps * cycles_per_step * 0.02, abs=1.0)


# ---- 17 --------------------------------------------------------------------


_ROWS = "ain0_ts,ain0_val\n100,1234\n200,1235\n"


@pytest.mark.parametrize(
    "rate",
    ["1\u00b2", "9" * 400, "9" * 4301, "4294967296"],
    ids=["superscript", "400-digits", "4301-digits", "past-uint32"],
)
def test_an_unusable_tick_rate_header_reads_as_absent(rate) -> None:
    text = f"# Device: Nq1\n# Timestamp Tick Rate: {rate} Hz\n" + _ROWS
    session = SdCardCsvFileParser().parse_stream(
        io.StringIO(text), "x.csv",
        SdCardParseOptions(fallback_timestamp_frequency=1000, integer_units="raw"),
    )
    assert session.device_config.timestamp_frequency == 1000  # the fallback
    assert len(list(session.samples)) == 2


def test_a_valid_tick_rate_header_is_read() -> None:
    text = "# Timestamp Tick Rate: 50000000 Hz\n" + _ROWS
    session = SdCardCsvFileParser().parse_stream(
        io.StringIO(text), "x.csv", SdCardParseOptions(integer_units="raw")
    )
    assert session.device_config.timestamp_frequency == 50_000_000


# ---- 23 --------------------------------------------------------------------


def test_memory_error_in_a_row_isnt_a_malformed_row(monkeypatch) -> None:
    import daqifi.sdcard as sdcard

    class _OutOfMemory:
        def match(self, _text):
            raise MemoryError

    monkeypatch.setattr(sdcard, "_CSV_INTEGER_VALUE", _OutOfMemory())
    session = SdCardCsvFileParser().parse_stream(
        io.StringIO("# Timestamp Tick Rate: 1000 Hz\n" + _ROWS), "x.csv",
        SdCardParseOptions(integer_units="raw"),
    )
    with pytest.raises(MemoryError):
        list(session.samples)


# ---- 11, audit r2 ----------------------------------------------------------


def _elapsed(processor, steps, cycles_per_step, device="d"):
    first = processor.process_timestamp(device, 0)
    ts = 0
    for _ in range(steps):
        ts = (ts + cycles_per_step) & 0xFFFFFFFF
        last = processor.process_timestamp(device, ts)
    return last.timestamp - first.timestamp


def test_a_day_of_100_hz_steps_is_exactly_a_day() -> None:
    # Audit r2: a float running sum of 8,640,000 steps of 0.01 s (500,000
    # ticks at 50 MHz, through ~1000 rollovers) came to 23:59:59.999993.
    assert _elapsed(TimestampProcessor(), 8_640_000, 500_000) == timedelta(days=1)


def test_a_frequency_change_doesnt_rescale_the_time_before_it() -> None:
    processor = TimestampProcessor()
    first = processor.process_timestamp("d", 0)
    processor.process_timestamp("d", 50_000_000)  # 1 s at 50 MHz
    processor.set_timestamp_frequency("d", 1_000)
    last = processor.process_timestamp("d", 50_001_000)  # + 1 s at 1 kHz
    assert last.timestamp - first.timestamp == timedelta(seconds=2)


def test_a_step_backwards_is_subtracted_exactly() -> None:
    processor = TimestampProcessor()
    first = processor.process_timestamp("d", 1_000_000_000)
    processor.process_timestamp("d", 1_050_000_000)          # +1 s
    last = processor.process_timestamp("d", 1_025_000_000)   # -0.5 s
    assert last.timestamp - first.timestamp == timedelta(seconds=0.5)
