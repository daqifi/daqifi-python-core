"""#154: the firmware ends an SD:GET that fails part-way through with
``__TRANSFER_ERROR__`` instead of ``__END_OF_FILE__`` (sd_card_manager.c,
#725). No reader recognised it, so a failed transfer waited out its whole
timeout (30 s, or 30 min for the receiver) and the marker could land in the
caller's file. Each reader now stops there and raises
SdCardTransferErrorException, keeping the bytes before the marker, which are
real content (upstream SdCardFileReceiver).
"""

import importlib.util
import io
import os
import pickle
import threading
import time

import pytest

import daqifi.exceptions as exc_mod
import daqifi.sdcard as sdcard

TE = b"__TRANSFER_ERROR__"
EOF = b"__END_OF_FILE__"


def _transfer_error_type():
    return getattr(exc_mod, "SdCardTransferErrorException", None)


def _assert_transfer_error(excinfo, received):
    assert type(excinfo.value).__name__ == "SdCardTransferErrorException"
    assert excinfo.value.bytes_received == received


# ---- SdCardFileReceiver ------------------------------------------------------


@pytest.mark.parametrize("buffer_size", [4096, 5], ids=["one-read", "split-marker"])
def test_the_receiver_stops_at_a_transfer_error(buffer_size) -> None:
    dest = io.BytesIO()
    receiver = sdcard.SdCardFileReceiver(io.BytesIO(b"partial log" + TE), buffer_size=buffer_size)
    with pytest.raises(Exception) as excinfo:
        receiver.receive(dest, "log_a.bin", timeout_s=5)
    _assert_transfer_error(excinfo, 11)
    assert dest.getvalue() == b"partial log"


def test_with_a_known_size_a_transfer_error_isnt_written_as_content() -> None:
    dest = io.BytesIO()
    receiver = sdcard.SdCardFileReceiver(io.BytesIO(b"partial log" + TE))
    with pytest.raises(Exception) as excinfo:
        receiver.receive(dest, "log_a.bin", timeout_s=5, expected_size=1000)
    _assert_transfer_error(excinfo, 11)
    assert dest.getvalue() == b"partial log"


class _OpenStream:
    """Stays open after its data (pyserial-like: in_waiting, no EOF)."""

    def __init__(self, data: bytes):
        self._data = bytearray(data)

    @property
    def in_waiting(self):
        return len(self._data)

    def read(self, n):
        out = bytes(self._data[:n])
        del self._data[:n]
        return out


def test_with_a_known_size_a_transfer_error_then_quiet_ends_the_wait(monkeypatch) -> None:
    monkeypatch.setattr(sdcard, "_TRANSFER_ERROR_SETTLE_S", 0.2)
    dest = io.BytesIO()
    receiver = sdcard.SdCardFileReceiver(_OpenStream(b"partial log" + TE))
    began = time.monotonic()
    with pytest.raises(Exception) as excinfo:
        receiver.receive(dest, "log_a.bin", timeout_s=10, expected_size=1000)
    assert time.monotonic() - began < 5
    _assert_transfer_error(excinfo, 11)
    assert dest.getvalue() == b"partial log"


def test_with_a_known_size_marker_text_inside_the_file_is_content() -> None:
    # The listed size says the marker text is part of the file.
    body = b"before " + TE + b" after"
    dest = io.BytesIO()
    receiver = sdcard.SdCardFileReceiver(io.BytesIO(body + EOF))
    assert receiver.receive(dest, "log_a.bin", timeout_s=5, expected_size=len(body)) == len(body)
    assert dest.getvalue() == body


@pytest.mark.parametrize("expected_size", [None, 9], ids=["no-size", "sized"])
def test_a_clean_transfer_still_completes(expected_size) -> None:
    dest = io.BytesIO()
    receiver = sdcard.SdCardFileReceiver(io.BytesIO(b"whole log" + EOF))
    assert receiver.receive(dest, "log_a.bin", timeout_s=5, expected_size=expected_size) == 9
    assert dest.getvalue() == b"whole log"


# ---- DaqifiStreamingDevice.download_sd_card_file ----------------------------


_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


@pytest.mark.parametrize("expected_size", [None, 1000], ids=["no-size", "sized"])
def test_download_sd_card_file_stops_at_a_transfer_error(expected_size) -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:GET": b"partial log" + TE + b"\r\n"})
    device._SD_DOWNLOAD_IDLE_TIMEOUT_S = 3.0  # the old path waits this out
    dest = io.BytesIO()
    try:
        began = time.monotonic()
        with pytest.raises(Exception) as excinfo:
            device.download_sd_card_file("log_a.bin", dest, expected_size=expected_size)
        assert time.monotonic() - began < 2.5
        _assert_transfer_error(excinfo, 11)
        assert dest.getvalue() == b"partial log"
    finally:
        device.disconnect()


def test_download_sd_card_file_still_completes_a_clean_transfer() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:GET": b"whole log" + EOF + b"\r\n"})
    dest = io.BytesIO()
    try:
        result = device.download_sd_card_file("log_a.bin", dest, expected_size=9)
        assert result.file_size == 9 and dest.getvalue() == b"whole log"
    finally:
        device.disconnect()


# ---- legacy NyquistDevice --------------------------------------------------


class _SerialWithReply:
    def __init__(self, reply: bytes):
        self.is_open = True
        self._reply = bytearray(reply)

    def write(self, data):
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


@pytest.mark.parametrize("method", ["sd_get_file_binary", "sd_get_file"])
def test_the_legacy_downloads_stop_at_a_transfer_error(method) -> None:
    from daqifi.device import NyquistDevice

    device = NyquistDevice("COM41")  # constructing doesn't open the port
    device._comm._serial = _SerialWithReply(b"partial log" + TE)
    began = time.monotonic()
    with pytest.raises(Exception) as excinfo:
        getattr(device, method)("log_a.bin", timeout=1.0)
    assert type(excinfo.value).__name__ == "SdCardTransferErrorException"
    assert time.monotonic() - began < 0.9


# ---- the exception ---------------------------------------------------------


def test_the_exception_carries_its_facts_and_pickles() -> None:
    error_type = _transfer_error_type()
    assert error_type is not None
    error = error_type("log_a.bin", 11)
    assert isinstance(error, exc_mod.SdCardOperationException)
    copy = pickle.loads(pickle.dumps(error))
    assert (copy.file_name, copy.bytes_received, str(copy)) == ("log_a.bin", 11, str(error))
