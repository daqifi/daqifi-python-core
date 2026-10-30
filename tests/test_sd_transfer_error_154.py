"""#154: the firmware ends an SD:GET that fails part-way through with
``__TRANSFER_ERROR__`` instead of ``__END_OF_FILE__`` (sd_card_manager.c,
#725; both markers are sent bare). No reader recognised it, so a failed
transfer waited out its whole timeout and the marker could land in the
caller's file. SdCardFileReceiver and download_sd_card_file now stop there
and raise SdCardTransferErrorException, keeping the bytes before the marker,
which are real content (upstream SdCardFileReceiver).

Without a size, the earliest marker ends the transfer. With a size, content
may contain the marker text, so no byte is written from an open candidate on;
a candidate followed by content was content; only an exact end-of-file
marker at the boundary is success; a candidate still open at idle/close/
deadline is the error. No quiet window decides anything (audit r1).

The legacy NyquistDevice downloads are unchanged (item 5 dropped; see #154).
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


def _assert_transfer_error(excinfo, received):
    assert type(excinfo.value).__name__ == "SdCardTransferErrorException", repr(excinfo.value)
    assert excinfo.value.bytes_received == received


class _OpenStream:
    """Stays open after its data (pyserial-like: in_waiting, no EOF); more
    can be fed later."""

    def __init__(self, data: bytes = b""):
        self._data = bytearray(data)
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._data.extend(data)

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._data)

    def read(self, n):
        with self._lock:
            out = bytes(self._data[:n])
            del self._data[:n]
            return out


def _later(delay, fn, *args):
    timer = threading.Timer(delay, fn, args)
    timer.daemon = True
    timer.start()
    return timer


# ---- SdCardFileReceiver ------------------------------------------------------


def _receive(source, expected_size=None, timeout_s=5.0, buffer_size=4096):
    dest = io.BytesIO()
    receiver = sdcard.SdCardFileReceiver(source, buffer_size=buffer_size)
    try:
        result = receiver.receive(dest, "log.bin", timeout_s=timeout_s, expected_size=expected_size)
        return result, dest.getvalue(), None
    except Exception as e:
        return None, dest.getvalue(), e


@pytest.mark.parametrize("buffer_size", [4096, 5], ids=["one-read", "split-marker"])
def test_the_receiver_stops_at_a_transfer_error(buffer_size) -> None:
    _, written, error = _receive(io.BytesIO(b"partial log" + TE), buffer_size=buffer_size)
    assert type(error).__name__ == "SdCardTransferErrorException" and error.bytes_received == 11
    assert written == b"partial log"


@pytest.mark.parametrize("expected_size", [12, 20, 1000])
def test_a_marker_near_or_across_the_size_boundary_is_the_error(expected_size) -> None:
    # Audit r1: with 12 or 20, the marker crossed the boundary and its bytes
    # were written as content before an OSError/TimeoutError.
    _, written, error = _receive(io.BytesIO(b"partial log" + TE), expected_size=expected_size)
    assert type(error).__name__ == "SdCardTransferErrorException" and error.bytes_received == 11
    assert written == b"partial log"


def test_a_line_ending_after_the_marker_still_reads_as_the_error() -> None:
    # Audit r1: CRLF after the marker defeated detection and wrote
    # "partial log__" to the file.
    _, written, error = _receive(io.BytesIO(b"partial log" + TE + b"\r\n"), expected_size=1000)
    assert type(error).__name__ == "SdCardTransferErrorException" and error.bytes_received == 11
    assert written == b"partial log"


def test_on_an_open_port_the_error_surfaces_at_the_deadline_typed() -> None:
    # No quiet window: an open candidate is the error when the wait ends.
    began = time.monotonic()
    _, written, error = _receive(_OpenStream(b"partial log" + TE + b"\r\n"), expected_size=1000, timeout_s=1.0)
    assert time.monotonic() - began < 4
    assert type(error).__name__ == "SdCardTransferErrorException" and error.bytes_received == 11
    assert written == b"partial log"


def test_marker_text_in_a_sized_file_is_content_even_after_a_pause() -> None:
    # Audit r1: a pause after the marker text used to abort a valid file.
    body = b"abc" + TE + b"xyz"
    source = _OpenStream(b"abc" + TE)
    _later(2.5, source.feed, b"xyz" + EOF)
    result, written, error = _receive(source, expected_size=len(body), timeout_s=10)
    assert error is None, repr(error)
    assert (result, written) == (len(body), body)


@pytest.mark.parametrize("expected_size", [None, 9], ids=["no-size", "sized"])
def test_a_clean_transfer_still_completes(expected_size) -> None:
    result, written, error = _receive(io.BytesIO(b"whole log" + EOF), expected_size=expected_size)
    assert error is None and (result, written) == (9, b"whole log")


# ---- DaqifiStreamingDevice.download_sd_card_file ----------------------------


_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


def _device(reply, idle_s=1.5):
    device = _fakes._make_device_with_serial_facade({b"SD:GET": reply})
    device._SD_DOWNLOAD_IDLE_TIMEOUT_S = idle_s
    return device


def _download(device, expected_size, after=None):
    """``after``: (delay, bytes) fed that long after SD:GET goes out, as the
    device would send them."""
    dest = io.BytesIO()
    stream = device._transport.stream
    real_write = stream.write

    def _write(data):
        result = real_write(data)
        if b"SD:GET" in bytes(data):
            for delay, more in after or ():
                _later(delay, stream.feed, more)
        return result

    stream.write = _write
    try:
        result = device.download_sd_card_file("log.bin", dest, expected_size=expected_size)
        return result, dest.getvalue(), None
    except Exception as e:
        return None, dest.getvalue(), e
    finally:
        device.disconnect()


@pytest.mark.parametrize("expected_size", [None, 1000], ids=["no-size", "sized"])
def test_download_sd_card_file_stops_at_a_transfer_error(expected_size) -> None:
    _, written, error = _download(_device(b"partial log" + TE + b"\r\n"), expected_size)
    assert type(error).__name__ == "SdCardTransferErrorException" and error.bytes_received == 11
    assert written == b"partial log"


@pytest.mark.parametrize("expected_size", [12, 20])
def test_download_a_marker_across_the_boundary_is_the_error(expected_size) -> None:
    _, written, error = _download(_device(b"partial log" + TE), expected_size)
    assert type(error).__name__ == "SdCardTransferErrorException" and error.bytes_received == 11
    assert written == b"partial log"


def test_download_a_partial_marker_completing_later_is_the_error_not_success() -> None:
    # Audit r1: 300 bytes + "__TRANSFER_ERRO", then "R__" after 0.5 s,
    # returned size_verified=True.
    result, written, error = _download(
        _device(b"x" * 300 + TE[:15]), 300, after=[(0.5, TE[15:])]
    )
    assert result is None
    assert type(error).__name__ == "SdCardTransferErrorException" and error.bytes_received == 300


def test_download_a_partial_marker_that_never_completes_is_not_success() -> None:
    result, written, error = _download(_device(b"A" * 300 + TE[:15]), 300)
    assert result is None
    assert isinstance(error, TimeoutError), repr(error)


@pytest.mark.parametrize(
    "first, later, expected",
    [
        (b"x" * 300 + TE, b"xyz" + EOF, b"x" * 300 + TE + b"xyz"),   # 318 then 0.5 s
        (b"abc" + TE, b"xyz" + EOF, b"abc" + TE + b"xyz"),          # 0.8 s pause
    ],
    ids=["321-byte", "24-byte"],
)
def test_download_marker_text_in_a_sized_file_is_content_after_a_pause(first, later, expected) -> None:
    result, written, error = _download(
        _device(first, idle_s=3.0), len(expected), after=[(0.8, later)]
    )
    assert error is None, repr(error)
    assert result.file_size == len(expected) and result.size_verified
    assert written == expected


def test_download_sd_card_file_still_completes_a_clean_transfer() -> None:
    result, written, error = _download(_device(b"whole log" + EOF + b"\r\n"), 9)
    assert error is None and result.file_size == 9 and written == b"whole log"


# ---- legacy NyquistDevice: unchanged -------------------------------------


class _SerialWithReply:
    def __init__(self, *reads: bytes):
        self.is_open = True
        self._reads = [bytearray(r) for r in reads]

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self._reads[0]) if self._reads else 0

    def read(self, n):
        out = bytes(self._reads[0][:n])
        del self._reads[0][:n]
        if not self._reads[0]:
            self._reads.pop(0)
        return out


def test_the_legacy_download_isnt_stopped_by_an_echoed_filename() -> None:
    # Audit r1: an echoed command naming the marker stopped the legacy
    # read early. The legacy path is unchanged: it reads to the EOF marker.
    from daqifi._internal import scpi_commands
    from daqifi.device import NyquistDevice

    name = "__TRANSFER_ERROR__.bin"
    echo = scpi_commands.get_sd_file(name).data.encode("ascii")
    device = NyquistDevice("COM41")
    device._comm._serial = _SerialWithReply(echo + b"\r\nDAQIFI> abc", b"def" + EOF)
    assert device.sd_get_file_binary(name, timeout=2.0) == b"abcdef"


# ---- the exception ---------------------------------------------------------


def test_the_exception_carries_its_facts_and_pickles() -> None:
    error_type = getattr(exc_mod, "SdCardTransferErrorException", None)
    assert error_type is not None
    error = error_type("log_a.bin", 11)
    assert isinstance(error, exc_mod.SdCardOperationException)
    copy = pickle.loads(pickle.dumps(error))
    assert (copy.file_name, copy.bytes_received, str(copy)) == ("log_a.bin", 11, str(error))
