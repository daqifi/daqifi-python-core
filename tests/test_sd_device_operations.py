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
