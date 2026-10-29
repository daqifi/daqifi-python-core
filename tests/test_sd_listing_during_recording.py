"""#187: no SD listing or state query may end an active recording.

The firmware's ``SD:ENAble`` handler sets the SD manager's mode to NONE on
both branches (SCPIStorageSD.c, SCPI_StorageSDEnableSet), which tears down a
write session, and ``SD:LIST?`` is refused while one is busy. The firmware
has no ``SD:LOGging?`` query; the logging state is ``SYSTem:STReam:DATA?``
plus ``SYSTem:STReam:INTerface?``.
"""

import importlib.util
import os

import pytest

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


def _recording_device(response_map=None):
    device = _fakes._make_device_with_serial_facade(response_map)
    device.start_sd_card_logging("active.bin")
    return device, len(device._transport.stream.written)


def test_the_text_listing_is_refused_during_a_recording() -> None:
    device, before = _recording_device({b"SD:LIST?": b"active.bin\r\n"})
    with pytest.raises(RuntimeError, match="while logging"):
        device.request_sd_card_file_list()
    assert device._transport.stream.written[before:] == []
    assert device.is_logging_to_sd_card


def test_the_listing_advice_no_longer_points_at_another_listing() -> None:
    device, _ = _recording_device()
    with pytest.raises(RuntimeError) as excinfo:
        device.get_sd_card_files()
    assert "request_sd_card_file_list" not in str(excinfo.value)


# A protobuf frame captured from a USB+SD stream (interface 3). Its digital
# bytes are ffff; a text reply that lands right after it fuses into one line.
_FRAME = bytes.fromhex("1208c6ecd8e3041201002a020000aa0202ffff")
_STREAM_REPLIES = {
    b"STReam:DATA?": _FRAME + b"1\r\n" + _FRAME,
    b"STReam:INTerface?": _FRAME + b"0\r\n" + _FRAME,
}


def test_the_logging_state_during_a_usb_and_sd_stream_sends_nothing() -> None:
    # Audit r1: a text query during a live binary stream fused replies with
    # frames, raised on valid answers and could hold the exchange lock.
    device, before = _recording_device(_STREAM_REPLIES)
    assert device.get_sd_logging_state() is True
    assert device._transport.stream.written[before:] == []


def test_a_recording_on_the_usb_interface_still_reads_as_logging() -> None:
    # Audit r1: the firmware logs to SD on any non-WiFi interface when SD is
    # enabled with a file name (SCPI_StartStreaming, sdLoggingRequested), so
    # INTerface? 0 can be a recording.
    device, _ = _recording_device(
        {b"STReam:DATA?": b"1\r\n", b"STReam:INTerface?": b"0\r\n"}
    )
    assert device.get_sd_logging_state() is True


def test_stopping_right_after_a_state_query_is_not_blocked() -> None:
    device, _ = _recording_device(_STREAM_REPLIES)
    device.get_sd_logging_state()
    device.stop_sd_card_logging()
    assert not device.is_logging_to_sd_card


def test_not_logging_reads_false_without_asking() -> None:
    device = _fakes._make_device_with_serial_facade(_STREAM_REPLIES)
    before = len(device._transport.stream.written)
    assert device.get_sd_logging_state() is False
    assert device._transport.stream.written[before:] == []
