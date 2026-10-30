"""#200: SD device operations.

1. SD list, delete, format and download stopped a live stream and never
   restarted it; the device state still said Streaming. They now resume the
   stream they paused (upstream daqifi-core 874c5fb / 9717b32).
2. They refused WiFi outright. Firmware v3.7.0+ routes SD replies back over
   the requesting TCP connection (#598/#599, SCPIStorageSD.c), so over WiFi
   they now work on that firmware, keep LAN up, and refuse older or unknown
   firmware (upstream 6baace2 / 2521fac). Logging stays USB-only.
3. enable_adc_channels accepted a hex mask the firmware rejects
   (SCPI_ParamInt32: "FF" -> -104, previous channel set kept).
4. start_sd_card_logging generated a file name but returned None.
5. run_sd_benchmark took "bytes"; the firmware takes 1-1024 KB.
"""

import importlib.util
import io
import os

import pytest

from daqifi._internal import scpi_commands
from daqifi._internal.scpi import DeviceState

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)

LISTING = b"Daqifi/log_a.bin 3\r\n"


def _device(**kwargs):
    return _fakes._make_device_with_serial_facade({b"SD:LIST?": LISTING}, **kwargs)


def _written(device):
    return [w.decode(errors="replace").strip() for w in device._transport.stream.written]


def _streaming(device):
    device.start_streaming()
    device._transport.stream.written.clear()
    return device


# ---- 1: resume ------------------------------------------------------------


@pytest.mark.parametrize("op", ["list", "delete", "format", "request_list"])
def test_an_sd_operation_resumes_the_stream_it_paused(op) -> None:
    device = _streaming(_device())
    try:
        {
            "list": device.get_sd_card_files,
            "delete": lambda: device.delete_sd_card_file("log_a.bin"),
            "format": device.format_sd_card,
            "request_list": device.request_sd_card_file_list,
        }[op]()
        commands = _written(device)
        assert commands[0].startswith("SYSTem:STReam:STOP")
        assert commands[-1].startswith("SYSTem:STReam:START")
        assert device.is_streaming and device.state == DeviceState.Streaming
    finally:
        device.disconnect()


def test_a_download_resumes_the_stream_it_paused() -> None:
    device = _streaming(_fakes._make_device_with_serial_facade(
        {b"SD:GET": b"abc__END_OF_FILE__\r\n"}))
    try:
        device.download_sd_card_file("log_a.bin", io.BytesIO(), expected_size=3)
        assert _written(device)[-1].startswith("SYSTem:STReam:START")
        assert device.is_streaming
    finally:
        device.disconnect()


def test_no_stream_is_started_when_none_was_running() -> None:
    device = _device()
    try:
        device.get_sd_card_files()
        assert not any(c.startswith("SYSTem:STReam:START") for c in _written(device))
        assert not device.is_streaming
    finally:
        device.disconnect()


def test_a_failed_resume_leaves_the_device_marked_not_streaming(monkeypatch) -> None:
    device = _streaming(_device())
    try:
        def _fail():
            raise OSError("link dropped")

        monkeypatch.setattr(device, "start_streaming", _fail)
        files = device.get_sd_card_files()  # the listing's own outcome stands
        assert [f.file_name for f in files] == ["log_a.bin"]
        assert not device.is_streaming and device.state != DeviceState.Streaming
    finally:
        device.disconnect()


# ---- 2: WiFi on firmware >= 3.7.0 -------------------------------------------


def test_sd_listing_works_over_wifi_on_firmware_3_7_and_keeps_lan_up() -> None:
    device = _device(usb=False)
    device._metadata.firmware_version = "3.7.0"
    try:
        files = device.get_sd_card_files()
        assert [f.file_name for f in files] == ["log_a.bin"]
        assert "SYSTem:COMMunicate:LAN:ENAbled 0" not in _written(device)
    finally:
        device.disconnect()


@pytest.mark.parametrize("version", ["3.6.3", ""])
def test_sd_operations_over_wifi_refuse_older_or_unknown_firmware(version) -> None:
    from daqifi.exceptions import FeatureNotSupportedError

    device = _device(usb=False)
    device._metadata.firmware_version = version
    try:
        with pytest.raises(FeatureNotSupportedError, match="v3.7.0"):
            device.get_sd_card_files()
        assert not any("SD:" in c for c in _written(device))
    finally:
        device.disconnect()


def test_sd_logging_over_wifi_is_still_refused() -> None:
    device = _device(usb=False)
    device._metadata.firmware_version = "3.8.0"
    try:
        with pytest.raises(RuntimeError, match="USB"):
            device.start_sd_card_logging()
    finally:
        device.disconnect()


# ---- 3, 4, 5 --------------------------------------------------------------


@pytest.mark.parametrize("mask", ["FF", "ff", "0x10", "#HFF"])
def test_a_hex_channel_mask_is_refused(mask) -> None:
    with pytest.raises(ValueError, match="decimal"):
        scpi_commands.enable_adc_channels(mask)


def test_a_decimal_channel_mask_is_sent() -> None:
    assert scpi_commands.enable_adc_channels("255").data == "ENAble:VOLTage:DC 255"


def test_start_sd_card_logging_returns_the_file_name() -> None:
    device = _device()
    try:
        name = device.start_sd_card_logging(format=_fakes.SdCardLogFormat.CSV)
        assert name.startswith("log_") and name.endswith(".csv")
        assert any(name in c for c in _written(device))
    finally:
        device.disconnect()
    device = _device()
    try:
        assert device.start_sd_card_logging(file_name="run1.bin") == "run1.bin"
    finally:
        device.disconnect()


@pytest.mark.parametrize("size, ok", [(1, True), (1024, True), (0, False), (1025, False), (4096, False)])
def test_the_benchmark_size_is_1_to_1024_kb(size, ok) -> None:
    if ok:
        assert scpi_commands.run_sd_benchmark(size).data.endswith(f" {size}")
    else:
        with pytest.raises(ValueError, match="1 to 1024 KB"):
            scpi_commands.run_sd_benchmark(size)
