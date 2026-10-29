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


@pytest.mark.parametrize(
    "streaming, interface, expected",
    [(b"1", b"2", True), (b"1", b"3", True), (b"1", b"0", False), (b"0", b"2", False)],
)
def test_the_logging_state_comes_from_the_streaming_queries(streaming, interface, expected) -> None:
    device = _fakes._make_device_with_serial_facade(
        {b"STReam:DATA?": streaming + b"\r\n", b"STReam:INTerface?": interface + b"\r\n"}
    )
    assert device.get_sd_logging_state() is expected


def test_the_logging_state_query_leaves_the_sd_bus_alone() -> None:
    device, before = _recording_device(
        {b"STReam:DATA?": b"1\r\n", b"STReam:INTerface?": b"2\r\n"}
    )
    assert device.get_sd_logging_state() is True
    sent = b"".join(device._transport.stream.written[before:])
    assert b"SD:ENAble" not in sent and b"LAN:ENAbled" not in sent
    assert b"SD:LOGging?" not in sent


def test_an_unanswered_state_query_raises_rather_than_echoing_the_cache() -> None:
    device = _fakes._make_device_with_serial_facade()
    device._is_logging_to_sd = True
    with pytest.raises(RuntimeError, match="streaming state"):
        device.get_sd_logging_state()
