"""#163 items 2, 3, 4, 6 and 14: SD device operations."""

import importlib.util
import io
import os

import pytest

from daqifi.sdcard import END_OF_FILE_MARKER, _write_all

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


def _written(device) -> bytes:
    return b"".join(device._transport.stream.written)


def test_listing_while_logging_is_refused_instead_of_ending_the_recording() -> None:
    # Item 2: get_sd_card_files sent STOP and SD:ENAble 0, ending the session.
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"a.bin\r\n"})
    device._is_logging_to_sd = True
    before = len(device._transport.stream.written)
    with pytest.raises(RuntimeError, match="while logging"):
        device.get_sd_card_files()
    assert device._transport.stream.written[before:] == []
    assert device.is_logging_to_sd_card


def test_stopping_sd_logging_restores_the_protobuf_stream_format() -> None:
    # Item 3: a JSON/CSV session left the device's global format changed, so
    # the live protobuf stream decoded nothing afterwards.
    device = _fakes._make_device_with_serial_facade()
    device.stop_sd_card_logging()
    assert b"SYSTem:STReam:FORmat 0" in _written(device)


@pytest.mark.parametrize("payload", [bytes.fromhex("0a088080011a040000c03f"), b"\r\nABC"])
def test_a_download_keeps_leading_cr_lf_bytes_of_the_file(payload) -> None:
    # Item 4: CR/LF were stripped from the head of every download, prompt or not.
    device = _fakes._make_device_with_serial_facade({b"SD:GET": payload + END_OF_FILE_MARKER})
    destination = io.BytesIO()
    device.download_sd_card_file("log.bin", destination)
    assert destination.getvalue() == payload


def test_a_prompt_after_cr_lf_is_still_stripped() -> None:
    device = _fakes._make_device_with_serial_facade(
        {b"SD:GET": b"\r\nDAQIFI> payload" + END_OF_FILE_MARKER}
    )
    destination = io.BytesIO()
    device.download_sd_card_file("log.bin", destination)
    assert destination.getvalue() == b"payload"


class _TwoBytesAtATime:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        chunk = bytes(data[:2])
        self.data += chunk
        return len(chunk)


def test_short_writes_are_completed() -> None:
    # Item 6: write()'s return value was ignored; a raw sink taking 2 bytes
    # per call ended with only the first 2.
    sink = _TwoBytesAtATime()
    _write_all(sink, b"abcdefgh")
    assert bytes(sink.data) == b"abcdefgh"


def test_a_download_into_a_short_writing_sink_is_complete() -> None:
    device = _fakes._make_device_with_serial_facade(
        {b"SD:GET": b"DAQIFI> abcdefgh" + END_OF_FILE_MARKER}
    )
    sink = _TwoBytesAtATime()
    result = device.download_sd_card_file("log.bin", sink)
    assert bytes(sink.data) == b"abcdefgh" and result.file_size == 8


def test_the_text_listing_keeps_a_file_named_after_the_prompt() -> None:
    # Item 14: "DAQIFI> log_a.bin" was listed as "DAQIFI>".
    device = _fakes._make_device_with_serial_facade(
        {b"SD:LIST?": b"DAQIFI> log_a.bin\r\nlog_b.bin\r\n"}
    )
    device.request_sd_card_file_list()
    assert [f.file_name for f in device.sd_card_files] == ["log_a.bin", "log_b.bin"]


class _NeverDrains(io.RawIOBase):
    def writable(self):
        return True

    def write(self, data):
        return None  # a non-blocking raw sink whose reader never drains


def test_a_sink_that_never_drains_fails_at_the_receive_deadline() -> None:
    # Audit finding on #182 (round 1): _write_all retried None forever,
    # bypassing the receive timeout.
    import time

    from daqifi.sdcard import SdCardFileReceiver

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        SdCardFileReceiver(io.BytesIO(b"abc" + END_OF_FILE_MARKER)).receive(
            _NeverDrains(), "log.bin", timeout_s=0.05
        )
    assert time.monotonic() - start < 2.0


def test_a_device_download_into_a_sink_that_never_drains_times_out(monkeypatch) -> None:
    import time

    device = _fakes._make_device_with_serial_facade(
        {b"SD:GET": b"DAQIFI> abcdefgh" + END_OF_FILE_MARKER}
    )
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_TIMEOUT_S", 0.2)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        device.download_sd_card_file("log.bin", _NeverDrains())
    assert time.monotonic() - start < 5.0
    assert not device._text_exchange_lock.locked()


class _SlowOneByte:
    def write(self, data):
        import time

        time.sleep(0.01)
        return 1  # drains, but one byte per call


def test_a_slowly_draining_sink_fails_at_the_receive_deadline() -> None:
    # Audit finding on #182 (round 2): the deadline was checked only after
    # None, so a sink that kept accepting one byte ran past it.
    import time

    from daqifi.sdcard import SdCardFileReceiver

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        SdCardFileReceiver(io.BytesIO(b"x" * 30 + END_OF_FILE_MARKER)).receive(
            _SlowOneByte(), "log.bin", timeout_s=0.05
        )
    assert time.monotonic() - start < 0.25


class _BackpressuredSink(io.RawIOBase):
    """A non-blocking raw sink: accepts nothing for ``delay`` seconds from
    its first write, then everything."""

    def writable(self):
        return True

    def __init__(self, delay):
        self._delay = delay
        self._until = None
        self.data = bytearray()

    def write(self, data):
        import time

        if self._until is None:
            self._until = time.monotonic() + self._delay
        if time.monotonic() < self._until:
            return None
        self.data += bytes(data)
        return len(data)


def test_destination_backpressure_does_not_count_as_an_idle_line(monkeypatch) -> None:
    # Audit finding on #182 (round 4): time spent retrying the destination
    # used up the serial idle timeout, so a download failed with the rest of
    # the file, EOF marker included, already buffered.
    device = _fakes._make_device_with_serial_facade(
        {b"SD:GET": b"DAQIFI> " + b"x" * 600 + END_OF_FILE_MARKER}, max_read_bytes=300
    )
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_IDLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_TIMEOUT_S", 5.0)
    sink = _BackpressuredSink(0.10)
    result = device.download_sd_card_file("log.bin", sink)
    assert bytes(sink.data) == b"x" * 600 and result.file_size == 600


def test_the_idle_clock_restarts_after_a_slow_destination(monkeypatch) -> None:
    # Flow control: the device sends the rest only once the host has caught
    # up, so the line is empty for a moment after a slow write. That moment
    # must be measured from the end of the write.
    import threading

    device = _fakes._make_device_with_serial_facade(
        {b"SD:GET": b"DAQIFI> " + b"x" * 292}, max_read_bytes=300
    )
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_IDLE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(type(device), "_SD_DOWNLOAD_TIMEOUT_S", 5.0)
    stream = device._transport.stream

    class _SinkThenRest(_BackpressuredSink):
        fed = False

        def write(self, data):
            written = super().write(data)
            if written and not self.fed:
                self.fed = True
                rest = b"x" * 308 + END_OF_FILE_MARKER
                threading.Timer(0.04, stream.feed, args=(rest,)).start()
            return written

    sink = _SinkThenRest(0.15)
    result = device.download_sd_card_file("log.bin", sink)
    assert bytes(sink.data) == b"x" * 600 and result.file_size == 600


def test_a_writer_returning_none_after_writing_is_not_written_twice() -> None:
    # Audit finding on #182 (round 5): codecs stream writers return None
    # after writing everything; retrying None repeated the payload until the
    # deadline. Only io.RawIOBase uses None to mean "would block".
    import codecs

    from daqifi.sdcard import SdCardFileReceiver

    out = io.BytesIO()
    sink = codecs.getwriter("hex_codec")(out)
    n = SdCardFileReceiver(io.BytesIO(b"abc" + END_OF_FILE_MARKER)).receive(
        sink, "log.bin", timeout_s=0.5
    )
    assert n == 3 and out.getvalue() == b"616263"
