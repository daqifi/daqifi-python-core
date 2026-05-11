"""Tests for #96 — SD card typed exceptions (port of daqifi-core PR #182).

Covers:
- The 4 new exception classes in daqifi.exceptions (shape, hierarchy, fields).
- The throw_if_sd_card_list_error classifier (no-card / filesystem / generic /
  empty / pass-through cases).
- request_sd_card_file_list raises the right typed exception when the
  device emits one of the recognized error markers.
"""
from __future__ import annotations

import threading

import pytest

from daqifi.exceptions import (
    DAQiFiError,
    SdCardBusyException,
    SdCardFilesystemException,
    SdCardNotPresentException,
    SdCardOperationException,
)
from daqifi.sdcard import throw_if_sd_card_list_error


pytestmark = pytest.mark.portomatic_generated


# ---------------------------------------------------------------------------
# Exception class shape + hierarchy
# ---------------------------------------------------------------------------


class TestSdCardOperationExceptionHierarchy:
    def test_base_is_daqifi_error(self):
        e = SdCardOperationException("msg", ["line"])
        assert isinstance(e, DAQiFiError)

    def test_busy_extends_base(self):
        e = SdCardBusyException(["line"])
        assert isinstance(e, SdCardOperationException)

    def test_filesystem_extends_base(self):
        e = SdCardFilesystemException(["line"], None, "[Error:3] Failed to open directory /Daqifi")
        assert isinstance(e, SdCardOperationException)

    def test_not_present_extends_base(self):
        e = SdCardNotPresentException(["line"])
        assert isinstance(e, SdCardOperationException)

    def test_base_carries_raw_response_and_scpi_error(self):
        e = SdCardOperationException(
            "boom",
            ["line one", "line two"],
            last_scpi_error='**ERROR: -200, "Execution error"',
        )
        assert e.raw_device_response == ("line one", "line two")
        assert e.last_scpi_error == '**ERROR: -200, "Execution error"'
        assert str(e) == "boom"

    def test_base_handles_none_raw_response(self):
        # Defensive: tuple() of None should yield empty tuple, not raise.
        e = SdCardOperationException("x", None)
        assert e.raw_device_response == ()
        assert e.last_scpi_error is None

    def test_filesystem_message_includes_device_message(self):
        e = SdCardFilesystemException(
            [],
            None,
            "[Error:3]Failed to open directory /Daqifi",
        )
        assert "[Error:3]Failed to open directory /Daqifi" in str(e)
        assert e.device_message == "[Error:3]Failed to open directory /Daqifi"

    def test_filesystem_message_falls_back_when_device_message_blank(self):
        e = SdCardFilesystemException([], None, "")
        assert str(e) == "The SD card filesystem reported an error."

    def test_busy_message_is_descriptive(self):
        e = SdCardBusyException([])
        assert "busy" in str(e).lower()

    def test_not_present_message_is_descriptive(self):
        e = SdCardNotPresentException([])
        assert "no sd card" in str(e).lower()


# ---------------------------------------------------------------------------
# throw_if_sd_card_list_error classifier
# ---------------------------------------------------------------------------


class TestThrowIfSdCardListError:
    def test_empty_lines_no_raise(self):
        # No errors and no content → empty directory; classifier returns.
        throw_if_sd_card_list_error([])

    def test_blank_lines_only_no_raise(self):
        throw_if_sd_card_list_error(["", "   ", "\t"])

    def test_passes_when_content_lines_present(self):
        # File listing with normal entries — classifier doesn't raise.
        throw_if_sd_card_list_error([
            "file1.csv,1024",
            "file2.csv,2048",
        ])

    def test_passes_with_interleaved_scpi_error_and_content(self):
        # Interleaved transient errors must not mask a real listing.
        throw_if_sd_card_list_error([
            '**ERROR: -200, "Execution error"',
            "file1.csv,1024",
        ])

    def test_no_sd_card_detected_raises_not_present(self):
        with pytest.raises(SdCardNotPresentException) as ei:
            throw_if_sd_card_list_error([
                "Error !! No SD Card Detected",
            ])
        assert ei.value.raw_device_response == ("Error !! No SD Card Detected",)
        # 'Error !! ...' is NOT a SCPI error (no colon, no '**' prefix).
        assert ei.value.last_scpi_error is None

    def test_no_sd_card_detected_case_insensitive(self):
        with pytest.raises(SdCardNotPresentException):
            throw_if_sd_card_list_error(["no sd card detected"])

    def test_failed_to_open_directory_raises_filesystem(self):
        with pytest.raises(SdCardFilesystemException) as ei:
            throw_if_sd_card_list_error([
                "[Error:3]Failed to open directory /Daqifi",
            ])
        assert "[Error:3]Failed to open directory /Daqifi" in ei.value.device_message
        assert ei.value.last_scpi_error is None

    def test_filesystem_carries_through_scpi_error(self):
        with pytest.raises(SdCardFilesystemException) as ei:
            throw_if_sd_card_list_error([
                '**ERROR: -200, "Execution error"',
                "[Error:3]Failed to open directory /Daqifi",
            ])
        assert ei.value.last_scpi_error == '**ERROR: -200, "Execution error"'

    def test_scpi_error_only_raises_generic(self):
        with pytest.raises(SdCardOperationException) as ei:
            throw_if_sd_card_list_error([
                '**ERROR: -200, "Execution error"',
            ])
        # Specific subclasses must NOT match the generic case
        assert not isinstance(ei.value, SdCardNotPresentException)
        assert not isinstance(ei.value, SdCardFilesystemException)
        assert ei.value.last_scpi_error == '**ERROR: -200, "Execution error"'

    def test_non_result_line_with_no_scpi_error_raises_generic(self):
        # Defensive fallback: 'Error !! ...' status text but no SCPI error
        # and no recognized marker.
        with pytest.raises(SdCardOperationException) as ei:
            throw_if_sd_card_list_error([
                "Error !! Unexpected device state",
            ])
        assert ei.value.last_scpi_error is None
        assert "Unexpected device state" in str(ei.value)

    def test_no_sd_card_takes_precedence_over_scpi_error(self):
        # If both 'No SD Card Detected' AND a SCPI error are present,
        # the typed NotPresent exception wins (with the SCPI error
        # surfaced via last_scpi_error).
        with pytest.raises(SdCardNotPresentException) as ei:
            throw_if_sd_card_list_error([
                '**ERROR: -200, "Execution error"',
                "Error !! No SD Card Detected",
            ])
        assert ei.value.last_scpi_error == '**ERROR: -200, "Execution error"'

    def test_picks_LAST_scpi_error_when_multiple(self):
        # Per upstream convention, last_scpi_error is the LAST matching
        # SCPI line (most recent error wins).
        with pytest.raises(SdCardOperationException) as ei:
            throw_if_sd_card_list_error([
                '**ERROR: -100, "Older"',
                '**ERROR: -200, "Newer"',
            ])
        assert ei.value.last_scpi_error == '**ERROR: -200, "Newer"'


# ---------------------------------------------------------------------------
# Integration: request_sd_card_file_list raises typed exceptions.
# Reuses the _ControlledStream + _FakeTransport pattern.
# ---------------------------------------------------------------------------


class _ControlledStream:
    def __init__(self):
        self._read_buf = bytearray()
        self.written = bytearray()
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._read_buf.extend(data)

    def read(self, n: int = -1) -> bytes:
        with self._lock:
            if n < 0:
                n = len(self._read_buf)
            chunk = bytes(self._read_buf[:n])
            del self._read_buf[:n]
            return chunk

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._read_buf)

    def close(self) -> None:
        pass


class _FakeSerialTransport:
    """Mimics SerialStreamTransport so is_usb_connection returns True
    on the device facade (the SD operations require USB, not TCP)."""

    def __init__(self):
        self._stream = _ControlledStream()
        self._connected = False
        self._status_handlers = []

    @property
    def stream(self):
        return self._stream

    @property
    def is_connected(self):
        return self._connected

    def add_status_changed(self, h):
        self._status_handlers.append(h)

    def remove_status_changed(self, h):
        if h in self._status_handlers:
            self._status_handlers.remove(h)

    def connect(self, retry_options=None):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def close(self):
        self._connected = False


def _make_streaming_dev_with_canned_reply(reply_bytes: bytes):
    """Build a connected DaqifiStreamingDevice whose request_sd_card_file_list
    will see ``reply_bytes`` as the captured SD:LIST? response.

    Uses a test-specific subclass to override ``is_usb_connection``
    (which checks isinstance against SerialStreamTransport in
    production) so the fake transport satisfies the SD-operations
    USB precondition. Subclassing keeps the override scoped to this
    test's instances — patching the production class would leak
    across tests. (Qodo /improve pass 1 importance 8 caught the
    earlier monkey-patch approach.)
    """
    from daqifi.devices import DaqifiStreamingDevice

    class _UsbStreamingDevice(DaqifiStreamingDevice):
        @property
        def is_usb_connection(self) -> bool:  # type: ignore[override]
            return True

    t = _FakeSerialTransport()
    dev = _UsbStreamingDevice(name="d", transport=t)
    dev.connect()

    # Patch send so each setup_action triggers feeding the canned reply.
    stream = t._stream
    enqueued = {"value": False}

    def fake_send(message):
        if not enqueued["value"]:
            stream.feed(reply_bytes)
            enqueued["value"] = True

    dev.send = fake_send

    # prepare_sd_interface / prepare_lan_interface are no-ops in this fake.
    dev.prepare_sd_interface = lambda: None
    dev.prepare_lan_interface = lambda: None

    return dev


def test_request_sd_card_file_list_raises_not_present():
    dev = _make_streaming_dev_with_canned_reply(
        b"Error !! No SD Card Detected\r\n"
    )
    with pytest.raises(SdCardNotPresentException):
        dev.request_sd_card_file_list()
    try:
        dev.close()
    except Exception:
        pass


def test_request_sd_card_file_list_raises_filesystem():
    dev = _make_streaming_dev_with_canned_reply(
        b"[Error:3]Failed to open directory /Daqifi\r\n"
    )
    with pytest.raises(SdCardFilesystemException) as ei:
        dev.request_sd_card_file_list()
    assert "Failed to open directory" in ei.value.device_message
    try:
        dev.close()
    except Exception:
        pass


def test_request_sd_card_file_list_passes_normal_listing():
    dev = _make_streaming_dev_with_canned_reply(
        b"file1.csv,1024\r\nfile2.csv,2048\r\n"
    )
    dev.request_sd_card_file_list()
    # Listing populated; no exception raised.
    assert len(dev.sd_card_files) == 2
    try:
        dev.close()
    except Exception:
        pass
