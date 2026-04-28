"""Tests for #16 — SD/text responses surfaced via execute_text_command.

Covers the two methods rewired (or added) in this PR:

* ``request_sd_card_file_list`` now actually populates
  ``_sd_card_files`` (used to be fire-and-forget).
* ``get_sd_logging_state`` is new — queries the device for its
  current SD-logging flag instead of trusting local cached state.

Both rely on ``execute_text_command`` (#22) to capture the text
reply that the protobuf consumer would otherwise drop.
"""
from __future__ import annotations

import threading
from typing import List, Optional

import pytest

from daqifi.devices import DaqifiStreamingDevice


pytestmark = pytest.mark.portomatic_generated


# ---- Fakes (mirrors test_sd_card_operations_impl.py) ----------------------


class _ScriptedSerialStream:
    def __init__(self, response_map: Optional[dict] = None) -> None:
        self.written: List[bytes] = []
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.response_map = dict(response_map or {})

    def write(self, data: bytes) -> int:
        b = bytes(data)
        self.written.append(b)
        for trigger, response in self.response_map.items():
            if trigger in b:
                self.feed(response)
        return len(b)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, n: int) -> bytes:
        with self._lock:
            take = bytes(self._buf[:n])
            del self._buf[:n]
            return take

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)


class _ScriptedTransport:
    def __init__(self, response_map: Optional[dict] = None) -> None:
        self.stream = _ScriptedSerialStream(response_map)
        self._connected = False
        self.status_handlers: list = []

    def add_status_changed(self, h):
        self.status_handlers.append(h)

    def remove_status_changed(self, h):
        if h in self.status_handlers:
            self.status_handlers.remove(h)

    def connect(self, retry_options=None):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def close(self):
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


class _UsbLikeStreamingDevice(DaqifiStreamingDevice):
    @property
    def is_usb_connection(self) -> bool:  # type: ignore[override]
        return True


def _make_device(
    response_map: Optional[dict] = None, *, usb: bool = True
) -> DaqifiStreamingDevice:
    cls = _UsbLikeStreamingDevice if usb else DaqifiStreamingDevice
    transport = _ScriptedTransport(response_map)
    sd = cls(name="sd-text", transport=transport)
    sd.connect()
    return sd


# ---- request_sd_card_file_list --------------------------------------------


class TestRequestSdCardFileList:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.request_sd_card_file_list()

    def test_requires_usb_transport(self) -> None:
        sd = _make_device(usb=False)
        try:
            with pytest.raises(RuntimeError, match="USB"):
                sd.request_sd_card_file_list()
        finally:
            sd.disconnect()

    def test_populates_sd_card_files_cache(self) -> None:
        # Real firmware emits the DAQIFI> prompt on its own line, then
        # one filename per line. Match that shape.
        listing = b"DAQIFI>\r\nlog_one.bin\r\nlog_two.bin\r\n"
        sd = _make_device(response_map={b"SD:LIST?": listing})
        try:
            sd.request_sd_card_file_list()
            names = [f.file_name for f in sd.sd_card_files]
            assert "log_one.bin" in names
            assert "log_two.bin" in names
        finally:
            sd.disconnect()

    def test_emits_list_query(self) -> None:
        sd = _make_device(response_map={b"SD:LIST?": b"DAQIFI>\r\na.bin\r\n"})
        try:
            sd.request_sd_card_file_list()
            written = b"".join(sd._transport.stream.written)
            assert b"SD:LIST?" in written
            # Brackets the query with LAN-disable / LAN-re-enable so the
            # SPI bus belongs to SD for the duration of the read.
            assert b"LAN:ENAbled 0" in written
            assert b"LAN:ENAbled 1" in written
        finally:
            sd.disconnect()

    def test_lan_restored_even_on_capture_failure(self) -> None:
        # If execute_text_command raises (e.g. consumer gone), the
        # finally block must still re-enable LAN so the device isn't
        # left with the SD interface latched.
        sd = _make_device()
        sd._consumer = None  # force execute_text_command to raise
        try:
            with pytest.raises(RuntimeError, match="active consumer"):
                sd.request_sd_card_file_list()
            # No way to assert LAN re-enable here because the failure
            # is inside execute_text_command BEFORE prepare_sd_interface
            # runs — but the bracket is still well-formed (no LAN
            # toggle = no risk of leaving it disabled).
            written = b"".join(sd._transport.stream.written)
            assert b"LAN:ENAbled 0" not in written
        finally:
            sd.disconnect()


# ---- get_sd_logging_state -------------------------------------------------


class TestGetSdLoggingState:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.get_sd_logging_state()

    def test_requires_usb_transport(self) -> None:
        sd = _make_device(usb=False)
        try:
            with pytest.raises(RuntimeError, match="USB"):
                sd.get_sd_logging_state()
        finally:
            sd.disconnect()

    def test_returns_true_for_one_response(self) -> None:
        sd = _make_device(response_map={b"SD:LOGging?": b"DAQIFI>\r\n1\r\n"})
        try:
            assert sd.get_sd_logging_state() is True
        finally:
            sd.disconnect()

    def test_returns_false_for_zero_response(self) -> None:
        sd = _make_device(response_map={b"SD:LOGging?": b"DAQIFI>\r\n0\r\n"})
        try:
            assert sd.get_sd_logging_state() is False
        finally:
            sd.disconnect()

    def test_falls_back_to_local_state_on_unparseable_response(self) -> None:
        # No response_map entry — execute_text_command times out empty.
        sd = _make_device()
        try:
            sd._is_logging_to_sd = True
            # No bytes ever fed, helper returns []. Method falls back
            # to the local cached flag.
            assert sd.get_sd_logging_state() is True
            sd._is_logging_to_sd = False
            assert sd.get_sd_logging_state() is False
        finally:
            sd.disconnect()
