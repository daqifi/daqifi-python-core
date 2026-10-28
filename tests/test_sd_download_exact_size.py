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


def test_a_truncated_transfer_is_an_error_with_the_listed_size() -> None:
    # Fixture: the transfer stops after 10 of the listed 100 bytes.
    import time

    device = _device(b"0123456789" + END_OF_FILE_MARKER + _TRAILER)
    start = time.monotonic()
    with pytest.raises(IOError, match="short read"):
        device.download_sd_card_file("log.bin", io.BytesIO(), expected_size=100)
    # Recognised when the line goes quiet, not after the 30 s idle timeout.
    assert time.monotonic() - start < 5.0


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
