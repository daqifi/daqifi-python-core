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
    # FORmat? answers "done": format_sd_card waits for the format to finish
    # before it resumes anything.
    return _fakes._make_device_with_serial_facade(
        {b"SD:LIST?": LISTING, b"SD:FORmat?": b"2,100\r\n"}, **kwargs
    )


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
        # Resumed, and confirmed: START, then the error query that checks it.
        assert commands[-2:][0].startswith("SYSTem:STReam:START")
        assert commands[-1].startswith("SYSTem:ERRor?")
        assert device.is_streaming and device.state == DeviceState.Streaming
    finally:
        device.disconnect()


def test_a_download_resumes_the_stream_it_paused() -> None:
    device = _streaming(_fakes._make_device_with_serial_facade(
        {b"SD:GET": b"abc__END_OF_FILE__\r\n"}))
    try:
        device.download_sd_card_file("log_a.bin", io.BytesIO(), expected_size=3)
        assert any(c.startswith("SYSTem:STReam:START") for c in _written(device))
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
        real_drain = device.drain_errors
        calls = []

        def _fail_on_restore(*args, **kwargs):
            # The listing's own drain works; the restore's raises.
            calls.append(1)
            if len(calls) > 1:
                raise OSError("link dropped")
            return real_drain(*args, **kwargs)

        monkeypatch.setattr(device, "drain_errors", _fail_on_restore)
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


# ---- audit r2: a restore during the format's busy window ---------------------

import time


class _BusyAfterFormat:
    """Models the firmware around an asynchronous format: for ``busy_s``
    after SD:FORmat the SD module is busy, so SD:ENAble 0 and a START (which
    would arm SD logging) are refused with -200; FORmat? says 1 while busy,
    2 after. SYSTem:ERRor? pops the queued errors."""

    def __init__(self, device, busy_s=0.3, refuse_start_always=False, format_status=2):
        self.stream = device._transport.stream
        self.busy_until = 0.0
        self.busy_s = busy_s
        self.refuse_start_always = refuse_start_always
        self.format_status = format_status
        self.errors = []
        self.refused = []
        self._write = self.stream.write
        self.stream.write = self.write

    def busy(self):
        return time.monotonic() < self.busy_until

    def write(self, data):
        b = bytes(data)
        self.stream.written.append(b)
        command = b.decode(errors="replace").strip()
        if command.startswith("SYSTem:STORage:SD:FORmat?"):
            self.stream.feed(b"1,50\r\n" if self.busy() else b"%d,100\r\n" % self.format_status)
        elif command.startswith("SYSTem:STORage:SD:FORmat"):
            self.busy_until = time.monotonic() + self.busy_s
        elif command.startswith("SYSTem:STORage:SD:ENAble 0") and self.busy():
            self.errors.append(b'-200,"Execution error"')
            self.refused.append(command)
        elif command.startswith("SYSTem:STReam:START") and (self.busy() or self.refuse_start_always):
            self.errors.append(b'-200,"Execution error"')
            self.refused.append(command)
        elif command.startswith("SYSTem:ERRor?"):
            self.stream.feed((self.errors.pop(0) if self.errors else b'0,"No error"') + b"\r\n")
        return len(b)


def test_a_format_resumes_only_after_it_completes_and_is_accepted() -> None:
    device = _streaming(_device())
    fake = _BusyAfterFormat(device, busy_s=0.3)
    try:
        device.format_sd_card()
        assert fake.refused == []  # nothing restored inside the busy window
        assert device.is_streaming and device.state == DeviceState.Streaming
    finally:
        device.disconnect()


def test_a_refused_start_leaves_the_device_ready_not_streaming() -> None:
    device = _streaming(_device())
    fake = _BusyAfterFormat(device, busy_s=0.0, refuse_start_always=True)
    try:
        device.format_sd_card()
        assert fake.refused and fake.refused[-1].startswith("SYSTem:STReam:START")
        assert not device.is_streaming and device.state != DeviceState.Streaming
        assert _written(device)[-1].startswith("SYSTem:STReam:STOP")
    finally:
        device.disconnect()


def test_a_failed_format_raises_and_doesnt_resume() -> None:
    from daqifi.exceptions import SdCardOperationException

    device = _streaming(_device())
    _BusyAfterFormat(device, busy_s=0.0, format_status=-1)
    try:
        with pytest.raises(SdCardOperationException, match="format failed"):
            device.format_sd_card()
        assert not device.is_streaming
    finally:
        device.disconnect()


# ---- audit r2: in_waiting on a descriptor >= 1024 ------------------------------


def test_in_waiting_works_on_a_high_descriptor_and_doesnt_drop_a_healthy_link() -> None:
    import socket

    resource = pytest.importorskip("resource")
    from daqifi.transport import _SocketStream

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = 1100
    if hard != resource.RLIM_INFINITY and hard <= target:
        pytest.skip("can't open a descriptor >= 1024 here")
    if soft <= target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target + 16, hard))
    a, b = socket.socketpair()
    os.dup2(a.fileno(), target)
    high = socket.socket(fileno=target)
    a.close()
    try:
        closed = []
        stream = _SocketStream(high, 0.5, 1, lambda s: closed.append(s))
        assert stream.in_waiting == 0
        assert not stream.peer_closed and not closed
        b.sendall(b"12345")
        for _ in range(50):
            if stream.in_waiting:
                break
            time.sleep(0.01)
        assert stream.in_waiting == 5
    finally:
        high.close()
        b.close()
        if soft <= target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
