"""#200: SD device operations.

1. SD list, delete, format and download stopped a live stream and never
   restarted it, while the device state still said Streaming. They still
   don't restart it (operator ruling: stopping is the user's call for
   anything beyond a health query), but they now leave the device Ready and
   log that they stopped it. A transport error after DELete is sent is
   SdCardDeleteOutcomeUnknownException; one before it stays definite.
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
import logging
import os
import threading
import time

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


def _device(response_map=None, **kwargs):
    responses = {b"SD:LIST?": LISTING}
    responses.update(response_map or {})
    return _fakes._make_device_with_serial_facade(responses, **kwargs)


def _written(device):
    return [w.decode(errors="replace").strip() for w in device._transport.stream.written]


def _streaming(device):
    device.start_streaming()
    device._transport.stream.written.clear()
    return device


# ---- 1: stop, don't resume ---------------------------------------------------


_SD_GET = {b"SD:GET": b"abc__END_OF_FILE__\r\n"}


def _sd_operation(device, op):
    return {
        "list": device.get_sd_card_files,
        "request_list": device.request_sd_card_file_list,
        "delete": lambda: device.delete_sd_card_file("log_a.bin"),
        "download": lambda: device.download_sd_card_file("log_a.bin", io.BytesIO(), expected_size=3),
        "format": device.format_sd_card,
    }[op]()


_ALL_SD_OPERATIONS = ["list", "request_list", "delete", "download", "format"]


@pytest.mark.parametrize("op", _ALL_SD_OPERATIONS)
def test_an_sd_operation_stops_the_stream_and_doesnt_resume_it(op, caplog) -> None:
    device = _streaming(_device(_SD_GET))
    try:
        with caplog.at_level(logging.WARNING, logger="daqifi.devices"):
            _sd_operation(device, op)
        commands = _written(device)
        assert commands[0].startswith("SYSTem:STReam:STOP")
        assert not any(c.startswith("SYSTem:STReam:START") for c in commands)
        assert not device.is_streaming and device.state == DeviceState.Ready
        assert any("isn't resumed" in r.getMessage() for r in caplog.records)
    finally:
        device.disconnect()


@pytest.mark.parametrize("op", _ALL_SD_OPERATIONS)
def test_an_sd_operation_with_no_stream_running_says_nothing(op, caplog) -> None:
    device = _device(_SD_GET)
    try:
        with caplog.at_level(logging.WARNING, logger="daqifi.devices"):
            _sd_operation(device, op)
        assert not any(c.startswith("SYSTem:STReam:START") for c in _written(device))
        assert not any("isn't resumed" in r.getMessage() for r in caplog.records)
        assert not device.is_streaming
    finally:
        device.disconnect()


def test_a_format_doesnt_poll_the_format_status() -> None:
    device = _device()
    try:
        device.format_sd_card()
        commands = _written(device)
        assert any(c.startswith("SYSTem:STORage:SD:FORmat") for c in commands)
        assert not any("FORmat?" in c for c in commands)
    finally:
        device.disconnect()


# ---- 1: a delete's outcome ------------------------------------------------------


class _FailsOnWrite:
    """The link fails on the first write of a command starting with
    ``prefix``, once ``behind`` has been written if given. With
    ``after_bytes``, those bytes of it go out first."""

    def __init__(self, device, prefix, error, after_bytes=0, behind=None):
        self.stream = device._transport.stream
        self.prefix, self.error, self.after_bytes = prefix, error, after_bytes
        self.behind_seen = behind is None
        self.behind = behind
        self.armed = True
        self._write = self.stream.write
        self.stream.write = self.write

    def write(self, data):
        data = bytes(data)
        if self.armed and self.behind_seen and data.startswith(self.prefix):
            self.armed = False
            if self.after_bytes:
                self._write(data[:self.after_bytes])
            raise self.error
        if self.behind is not None and data.startswith(self.behind):
            self.behind_seen = True
        return self._write(data)


_DELETE = b"SYSTem:STORage:SD:DELete"
_ERROR_QUERY = b"SYSTem:ERRor?"


def _assert_outcome_unknown(excinfo, cause):
    assert excinfo.value.__cause__ is cause
    assert excinfo.value.file_name == "log_a.bin"


def test_a_write_failing_after_delete_is_sent_is_outcome_unknown() -> None:
    from daqifi.exceptions import SdCardDeleteOutcomeUnknownException

    device = _device()
    error = OSError("link dropped")
    # The SYSTem:ERRor? right behind the DELete.
    fake = _FailsOnWrite(device, _ERROR_QUERY, error, behind=_DELETE)
    try:
        with pytest.raises(SdCardDeleteOutcomeUnknownException) as excinfo:
            device.delete_sd_card_file("log_a.bin")
        _assert_outcome_unknown(excinfo, error)
        assert not fake.armed
    finally:
        device.disconnect()


def test_a_read_failing_after_delete_is_sent_is_outcome_unknown() -> None:
    from daqifi.exceptions import SdCardDeleteOutcomeUnknownException

    device = _device()
    stream = device._transport.stream
    error = OSError("link dropped")
    real_write = stream.write

    def _write(data):
        result = real_write(data)
        if bytes(data).startswith(_DELETE):
            def _read(*_args):
                raise error

            stream.read = _read
        return result

    stream.write = _write
    try:
        with pytest.raises(SdCardDeleteOutcomeUnknownException) as excinfo:
            device.delete_sd_card_file("log_a.bin")
        _assert_outcome_unknown(excinfo, error)
    finally:
        del stream.read
        device.disconnect()


@pytest.mark.parametrize("after_bytes", [0, 10], ids=["nothing-out", "part-way"])
def test_a_write_of_delete_that_fails_is_outcome_unknown(after_bytes) -> None:
    # A failed write may have reached the device, at least in part.
    from daqifi.exceptions import SdCardDeleteOutcomeUnknownException

    device = _device()
    error = OSError("write failed")
    _FailsOnWrite(device, _DELETE, error, after_bytes)
    try:
        with pytest.raises(SdCardDeleteOutcomeUnknownException) as excinfo:
            device.delete_sd_card_file("log_a.bin")
        _assert_outcome_unknown(excinfo, error)
    finally:
        device.disconnect()


@pytest.mark.parametrize(
    "error", [OSError("link dropped"), TimeoutError("write timed out")], ids=["oserror", "timeout"]
)
def test_a_transport_error_before_delete_is_sent_stays_definite(error) -> None:
    # A TimeoutError here read as "the file may already be gone"; nothing was sent.
    device = _device()
    _FailsOnWrite(device, b"SYSTem:STORage:SD:ENAble", error)
    try:
        with pytest.raises(type(error)) as excinfo:
            device.delete_sd_card_file("log_a.bin")
        assert excinfo.value is error
        assert not any("DELete" in c for c in _written(device))
    finally:
        device.disconnect()


def test_a_link_lost_before_delete_goes_out_stays_definite(monkeypatch) -> None:
    device = _device()
    real_prepare = device.prepare_sd_interface

    def _prepare_then_lose_the_link():
        real_prepare()
        monkeypatch.setattr(type(device), "is_connected", property(lambda _self: False))

    device.prepare_sd_interface = _prepare_then_lose_the_link
    try:
        with pytest.raises(RuntimeError, match="not connected"):
            device.delete_sd_card_file("log_a.bin")
        assert not any("DELete" in c for c in _written(device))
    finally:
        monkeypatch.undo()
        device.disconnect()


def test_a_delete_the_device_answered_keeps_its_own_error() -> None:
    from daqifi.exceptions import SdCardDeleteOutcomeUnknownException, SdCardNotPresentException

    device = _device({_DELETE: b"No SD Card Detected\r\n"})
    try:
        with pytest.raises(SdCardNotPresentException) as excinfo:
            device.delete_sd_card_file("log_a.bin")
        assert not isinstance(excinfo.value, SdCardDeleteOutcomeUnknownException)
    finally:
        device.disconnect()


class _RefusesDelete:
    """The DELete is refused: its SYSTem:ERRor? answers ``error`` (ahead of
    it, ``volunteered``, as the firmware's SCPI_ErrorPush writes), then the
    listing and a clean terminator. The file is still there."""

    def __init__(self, device, error=b'-200,"Execution error"', volunteered=b""):
        self.stream = device._transport.stream
        self.error, self.volunteered = error, volunteered
        self.deleted, self.answered = False, False
        self._write = self.stream.write
        self.stream.write = self.write

    def write(self, data):
        data = bytes(data)
        if data.startswith(_DELETE):
            self.deleted = True
            self.stream.written.append(data)
            self.stream.feed(self.volunteered)
            return len(data)
        if self.deleted and not self.answered and data.startswith(_ERROR_QUERY):
            self.answered = True
            self.stream.written.append(data)
            self.stream.feed(self.error + b"\r\n")
            return len(data)
        return self._write(data)


@pytest.mark.parametrize(
    "volunteered",
    [b"", b'**ERROR: -200, "Execution error"\r\n'],
    ids=["queued", "also-volunteered"],
)
def test_a_refused_delete_raises_a_definite_error(volunteered) -> None:
    # Audit r8: the first reply's code was thrown away, so this returned None
    # with the file still there.
    from daqifi.exceptions import SdCardDeleteOutcomeUnknownException, SdCardOperationException

    device = _device()
    fake = _RefusesDelete(device, volunteered=volunteered)
    try:
        with pytest.raises(SdCardOperationException, match="-200") as excinfo:
            device.delete_sd_card_file("log_a.bin")
        assert not isinstance(excinfo.value, SdCardDeleteOutcomeUnknownException)
        assert fake.answered
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


# ---- audit r4: a failed download comes back at once ---------------------------


class _FullDisk(io.RawIOBase):
    def writable(self):
        return True

    def write(self, data):
        raise OSError(28, "No space left on device")


class _EndlessGet:
    """After SD:GET, sends 512-byte CR LF-terminated file lines every 10 ms
    until closed: a long file the device keeps sending."""

    def __init__(self, device):
        self.stream = device._transport.stream
        self.sending = threading.Event()
        self.done = threading.Event()
        self._write = self.stream.write
        self.stream.write = self.write
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def write(self, data):
        if b"SD:GET" in bytes(data):
            self.sending.set()
        return self._write(data)

    def _pump(self):
        line = b"x" * 510 + b"\r\n"
        while not self.done.is_set():
            if self.sending.is_set():
                self.stream.feed(line)
            time.sleep(0.01)

    def close(self):
        self.done.set()
        self.thread.join(2)


def test_a_failed_download_raises_promptly() -> None:
    # The destination fails mid-GET while the device goes on sending the
    # file: the error must come back at once.
    device = _streaming(_device())
    feed = _EndlessGet(device)
    outcome = {}

    def _run():
        try:
            device.download_sd_card_file("log_a.bin", _FullDisk(), expected_size=10**9)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    try:
        worker.start()
        worker.join(8)  # bounds the test
        assert not worker.is_alive(), "the failed download never returned"
        assert isinstance(outcome.get("error"), OSError), outcome
        assert not any(c.startswith("SYSTem:STReam:START") for c in _written(device))
        assert not device.is_streaming and device.state != DeviceState.Streaming
    finally:
        feed.close()
        worker.join(5)
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
