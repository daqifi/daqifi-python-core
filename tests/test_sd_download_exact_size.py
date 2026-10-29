"""#163 item 16: a download ended at the first __END_OF_FILE__ token, so a
file containing that text was silently cut short and reported as success.

With the listed size the download is exactly that long and must end with
the marker; without it the marker counts only at the end of the transfer,
and the result says the size wasn't verified."""

import importlib.util
import io
import os

import pytest

from daqifi.sdcard import END_OF_FILE_MARKER, SdCardFileReceiver, parse_sd_card_file_list

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)

# Fixture: a file whose content contains the end-of-file token mid-file.
_CONTENT_WITH_TOKEN = b"row1\n" + END_OF_FILE_MARKER + b"\nrow2\n"
_TRAILER = b"\r\nDAQIFI> "


def _device(reply: bytes, **kwargs):
    return _fakes._make_device_with_serial_facade(
        {b"SD:GET": b"DAQIFI> " + reply}, **kwargs
    )


def test_a_file_containing_the_token_downloads_whole_with_its_listed_size() -> None:
    device = _device(_CONTENT_WITH_TOKEN + END_OF_FILE_MARKER + _TRAILER)
    sink = io.BytesIO()
    result = device.download_sd_card_file(
        "log.bin", sink, expected_size=len(_CONTENT_WITH_TOKEN)
    )
    assert sink.getvalue() == _CONTENT_WITH_TOKEN
    assert result.file_size == len(_CONTENT_WITH_TOKEN) and result.size_verified


def test_a_file_containing_the_token_downloads_whole_without_a_size() -> None:
    device = _device(_CONTENT_WITH_TOKEN + END_OF_FILE_MARKER + _TRAILER)
    sink = io.BytesIO()
    result = device.download_sd_card_file("log.bin", sink)
    assert sink.getvalue() == _CONTENT_WITH_TOKEN
    assert not result.size_verified


def test_the_listed_size_is_used_when_none_is_passed() -> None:
    device = _fakes._make_device_with_serial_facade(
        {
            b"SD:LIST?": b"log.bin %d\r\n" % len(_CONTENT_WITH_TOKEN),
            b"SD:GET": b"DAQIFI> " + _CONTENT_WITH_TOKEN + END_OF_FILE_MARKER + _TRAILER,
        }
    )
    device.request_sd_card_file_list()
    sink = io.BytesIO()
    result = device.download_sd_card_file("log.bin", sink)
    assert sink.getvalue() == _CONTENT_WITH_TOKEN and result.size_verified


def test_a_truncated_transfer_is_an_error_with_the_listed_size(monkeypatch) -> None:
    # Fixture: the transfer stops after 10 of the listed 100 bytes. A pause
    # can't be told from the end (audit r1 on #185: a file containing the
    # token paused mid-transfer), so it fails at the idle timeout.
    device = _device(b"0123456789" + END_OF_FILE_MARKER + _TRAILER)
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_IDLE_TIMEOUT_S", 0.5)
    with pytest.raises(TimeoutError, match="short read: 35 of 100"):
        device.download_sd_card_file("log.bin", io.BytesIO(), expected_size=100)


def test_a_transfer_longer_than_listed_is_an_error() -> None:
    device = _device(b"0123456789" + END_OF_FILE_MARKER + _TRAILER)
    with pytest.raises(IOError, match="no end-of-file marker"):
        device.download_sd_card_file("log.bin", io.BytesIO(), expected_size=5)


def test_a_truncated_transfer_without_a_size_is_reported_unverified() -> None:
    # Undetectable without a size; the result must not claim it was checked.
    device = _device(b"0123456789" + END_OF_FILE_MARKER + _TRAILER)
    result = device.download_sd_card_file("log.bin", io.BytesIO())
    assert result.file_size == 10 and not result.size_verified


def test_the_token_split_across_reads_is_still_content() -> None:
    device = _device(
        _CONTENT_WITH_TOKEN + END_OF_FILE_MARKER + _TRAILER, max_read_bytes=3
    )
    sink = io.BytesIO()
    device.download_sd_card_file("log.bin", sink)
    assert sink.getvalue() == _CONTENT_WITH_TOKEN


def test_starting_a_recording_forgets_listed_sizes() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"log.bin 6\r\n"})
    device.request_sd_card_file_list()
    assert device.sd_card_files[0].size == 6
    device.enable_sd_card_logging()
    assert device.sd_card_files[0].size is None


def test_the_listing_parser_reads_the_size_column() -> None:
    files = parse_sd_card_file_list(["Daqifi/log_a.bin 1234", "log_b.bin", "log_c.bin x"])
    assert [(f.file_name, f.size) for f in files] == [
        ("log_a.bin", 1234), ("log_b.bin", None), ("log_c.bin", None),
    ]


def test_the_receiver_reads_exactly_the_expected_size() -> None:
    source = io.BytesIO(_CONTENT_WITH_TOKEN + END_OF_FILE_MARKER + b"after")
    receiver = SdCardFileReceiver(source)
    sink = io.BytesIO()
    n = receiver.receive(sink, "log.bin", expected_size=len(_CONTENT_WITH_TOKEN))
    assert sink.getvalue() == _CONTENT_WITH_TOKEN and n == len(_CONTENT_WITH_TOKEN)
    assert receiver.leftover == b"after"


def test_the_receiver_rejects_a_missing_trailer() -> None:
    source = io.BytesIO(b"0123456789" + END_OF_FILE_MARKER)
    with pytest.raises(IOError, match="no end-of-file marker"):
        SdCardFileReceiver(source).receive(io.BytesIO(), "log.bin", expected_size=5)


def test_the_temp_file_download_carries_size_verified() -> None:
    device = _device(_CONTENT_WITH_TOKEN + END_OF_FILE_MARKER + _TRAILER)
    result = device.download_sd_card_file_to_temp(
        "log.bin", expected_size=len(_CONTENT_WITH_TOKEN)
    )
    try:
        with open(result.file_path, "rb") as handle:
            assert handle.read() == _CONTENT_WITH_TOKEN
        assert result.size_verified
    finally:
        os.unlink(result.file_path)


def test_a_token_followed_by_content_is_not_the_end(monkeypatch) -> None:
    # The transfer stops after a mid-file token and more content, with no
    # final marker: that is a stalled download, not a complete file.
    device = _device(b"abc" + END_OF_FILE_MARKER + b"xyz")
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_IDLE_TIMEOUT_S", 0.6)
    with pytest.raises(TimeoutError):
        device.download_sd_card_file("log.bin", io.BytesIO())


class _SlowThenFeeds(io.RawIOBase):
    """A non-blocking raw sink: accepts nothing for 0.15 s from its first
    write; once it accepts, the rest of the file arrives 40 ms later (flow
    control)."""

    def writable(self):
        return True

    def __init__(self, stream, rest):
        super().__init__()
        self._stream, self._rest = stream, rest
        self._until = None
        self.data = bytearray()

    def write(self, data):
        import threading
        import time

        if self._until is None:
            self._until = time.monotonic() + 0.15
        if time.monotonic() < self._until:
            return None
        if self._rest is not None:
            threading.Timer(0.04, self._stream.feed, args=(self._rest,)).start()
            self._rest = None
        self.data += bytes(data)
        return len(data)


def test_the_idle_clock_restarts_after_a_slow_destination_with_a_size(monkeypatch) -> None:
    device = _fakes._make_device_with_serial_facade(
        {b"SD:GET": b"DAQIFI> " + b"x" * 292}, max_read_bytes=300
    )
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_IDLE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_TIMEOUT_S", 5.0)
    sink = _SlowThenFeeds(device._transport.stream, b"x" * 308 + END_OF_FILE_MARKER)
    result = device.download_sd_card_file("log.bin", sink, expected_size=600)
    assert bytes(sink.data) == b"x" * 600 and result.size_verified


# Audit round 1 on #185.

def test_a_pause_after_an_embedded_token_is_not_a_short_read() -> None:
    # The first part ends with the token, then the line pauses 0.4 s+.
    import threading

    device = _device(b"abc" + END_OF_FILE_MARKER)
    stream = device._transport.stream
    rest = b"xyz" + END_OF_FILE_MARKER + _TRAILER
    timer = threading.Timer(0.8, stream.feed, args=(rest,))
    timer.start()
    sink = io.BytesIO()
    try:
        result = device.download_sd_card_file("log.bin", sink, expected_size=21)
    finally:
        timer.cancel()
    assert sink.getvalue() == b"abc" + END_OF_FILE_MARKER + b"xyz" and result.size_verified


def test_a_longer_file_with_the_token_at_the_listed_boundary_is_an_error() -> None:
    device = _device(b"abc" + END_OF_FILE_MARKER + b"xyz" + END_OF_FILE_MARKER + _TRAILER)
    with pytest.raises(IOError, match="longer than listed"):
        device.download_sd_card_file("log.bin", io.BytesIO(), expected_size=3)


def test_no_listing_is_taken_while_logging() -> None:
    # A listing during a recording would end it (#187), so there are no
    # sizes from one to go stale.
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"log.bin 3\r\n"})
    device._is_logging_to_sd = True
    with pytest.raises(RuntimeError, match="while logging"):
        device.request_sd_card_file_list()


def test_stopping_a_recording_forgets_listed_sizes() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"log.bin 3\r\n"})
    device.request_sd_card_file_list()
    device._is_logging_to_sd = True
    device.stop_sd_card_logging()
    assert device.sd_card_files[0].size is None


# Audit round 2 on #185.

def test_a_download_keeps_the_other_files_listed_sizes() -> None:
    # The LAN hand-back after a download cleared every listed size, so the
    # next file of the same listing lost its truncation check.
    device = _fakes._make_device_with_serial_facade(
        {
            b"SD:LIST?": b"a.bin 3\r\nb.bin 100\r\n",
            b"SD:GET": b"DAQIFI> abc" + END_OF_FILE_MARKER + _TRAILER,
        }
    )
    device.request_sd_card_file_list()
    assert device.download_sd_card_file("a.bin", io.BytesIO()).size_verified
    assert [f.size for f in device.sd_card_files] == [3, 100]


# Audit round 5 on #185.

def test_a_reconnect_forgets_listed_sizes() -> None:
    device = _fakes._make_device_with_serial_facade(
        {
            b"SD:LIST?": b"log.bin 3\r\n",
            b"SD:GET": b"DAQIFI> abcdef" + END_OF_FILE_MARKER + _TRAILER,
        }
    )
    device.request_sd_card_file_list()
    device.disconnect()
    device.connect()
    assert device.sd_card_files[0].size is None
    sink = io.BytesIO()
    result = device.download_sd_card_file("log.bin", sink)
    assert sink.getvalue() == b"abcdef" and not result.size_verified
