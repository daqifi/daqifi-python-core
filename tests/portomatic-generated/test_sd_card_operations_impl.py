"""Tests for the SdCardOperations Protocol concrete impl on
DaqifiStreamingDevice (closes daqifi-python-core#34).

Covers:
* Protocol structural conformance via ``isinstance``
* SCPI dispatch shape for the lifecycle ops (start/stop/format)
* Filename validation (rejects characters that could smuggle SCPI)
* Raw-capture path: list/delete/download go through the consumer-pause
  helper rather than relying on the protobuf consumer to surface
  text-shaped responses
* End-of-file marker detection on the download path, including the
  case where the marker straddles a chunk boundary
* :func:`download_sd_card_file_to_temp` wraps the byte-stream overload
  and creates a real on-disk artifact whose path is returned

Hardware-coupled paths (real wire timing, real file content) live in
``tests/integration/hardware/test_sd*.py``."""
from __future__ import annotations

import os
import threading
from io import BytesIO
from typing import Iterable, List, Optional

import pytest

from daqifi.devices import (
    DaqifiStreamingDevice,
    _validate_sd_card_file_name,
)
from daqifi.enums import SdCardLogFormat
from daqifi.sdcard import (
    END_OF_FILE_MARKER,
    SdCardDownloadResult,
    SdCardFileInfo,
    SdCardOperations,
    SdCardTransferProgress,
    parse_sd_card_file_list,
)


pytestmark = pytest.mark.portomatic_generated


# ---- Fakes -----------------------------------------------------------------


class _ScriptedSerialStream:
    """Stand-in for ``serial.Serial`` that emits pre-recorded responses
    when matching SCPI bytes are written. Tracks every byte written for
    SCPI assertions.

    ``response_map`` maps a ``bytes`` substring (the SCPI trigger) to
    the ``bytes`` payload the firmware would emit in response. Triggers
    are checked against every ``write()`` call. We deliberately do
    **not** pre-load anything into the read buffer at construction
    time, because the consumer thread that ``DaqifiDevice.connect()``
    spins up would race in and drain it before the test's SD-card op
    has paused the consumer for raw capture.
    """

    def __init__(
        self,
        response_map: Optional[dict] = None,
        *,
        max_read_bytes: Optional[int] = None,
    ) -> None:
        self.written: List[bytes] = []
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.response_map = dict(response_map or {})
        # Cap bytes returned per ``read()`` call so tests can force
        # multi-chunk delivery of a single response (exercises the
        # trailing-window EOF-marker straddle path).
        self._max_read_bytes = max_read_bytes

    # transport stream API
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
            avail = len(self._buf)
            if self._max_read_bytes is not None:
                return min(avail, self._max_read_bytes)
            return avail

    def read(self, n: int) -> bytes:
        with self._lock:
            limit = n if self._max_read_bytes is None else min(n, self._max_read_bytes)
            take = bytes(self._buf[:limit])
            del self._buf[:limit]
            return take

    # helper for tests that need to inject more data mid-flight
    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)


class _ScriptedTransport:
    """Test-only transport wrapping a ``_ScriptedSerialStream``. Mimics
    ``SerialStreamTransport`` enough that ``DaqifiStreamingDevice``
    treats it as a USB connection."""

    def __init__(
        self,
        response_map: Optional[dict] = None,
        *,
        max_read_bytes: Optional[int] = None,
    ) -> None:
        self.stream = _ScriptedSerialStream(
            response_map, max_read_bytes=max_read_bytes
        )
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
    """``DaqifiStreamingDevice`` whose ``is_usb_connection`` always
    returns True, regardless of the transport's class identity.

    The production check is ``isinstance(self._transport,
    SerialStreamTransport)``, which a test fake can't satisfy without
    constructing a real serial port. Subclassing keeps the rest of the
    SD-card flow under test.
    """

    @property
    def is_usb_connection(self) -> bool:  # type: ignore[override]
        return True


def _make_device_with_serial_facade(
    response_map: Optional[dict] = None,
    *,
    usb: bool = True,
    max_read_bytes: Optional[int] = None,
) -> DaqifiStreamingDevice:
    """Build a connected streaming device backed by a scripted transport.

    ``usb=True`` (default) returns a subclass that reports USB-shaped
    transport so the SD-card-only ops don't trip the USB guard.
    ``response_map`` lets tests pre-register firmware responses keyed
    by SCPI substring trigger. ``max_read_bytes`` caps bytes returned
    per ``read()`` (forces multi-chunk delivery for marker-straddle
    tests).
    """
    cls = _UsbLikeStreamingDevice if usb else DaqifiStreamingDevice
    transport = _ScriptedTransport(response_map, max_read_bytes=max_read_bytes)
    sd = cls(name="sd-test", transport=transport)
    sd.connect()
    return sd


# ---- Protocol structural conformance --------------------------------------


def test_streaming_device_satisfies_sd_card_operations_protocol() -> None:
    sd = DaqifiStreamingDevice(name="x", transport=None)
    assert isinstance(sd, SdCardOperations)


# ---- Filename validation ---------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ['has"quote.bin', "has\nnewline.bin", "has\rreturn.bin", "has;semi.bin"],
)
def test_validate_sd_card_file_name_rejects_smuggle_chars(bad_name: str) -> None:
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_sd_card_file_name(bad_name)


def test_validate_sd_card_file_name_accepts_normal_names() -> None:
    _validate_sd_card_file_name("log_20260101_120000.bin")
    _validate_sd_card_file_name("data.csv")


# ---- start_sd_card_logging -------------------------------------------------


class TestStartSdCardLogging:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.start_sd_card_logging()

    def test_dispatches_scpi_sequence_with_explicit_name(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            sd.start_sd_card_logging(file_name="run_a.bin")
            written = b"".join(sd._transport.stream.written)
            assert b"LAN:ENAbled 0" in written
            assert b"SD:ENAble 1" in written
            assert b"SYSTem:STReam:INTerface 2" in written
            assert b'SD:FILE "run_a.bin"' in written
            assert b"SYSTem:STReam:FORmat 0" in written
            assert b"StartStreamData" in written
            assert sd.is_logging_to_sd_card is True
            assert sd.is_streaming is True
        finally:
            sd.disconnect()

    def test_generates_timestamped_name_when_omitted(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            sd.start_sd_card_logging(format=SdCardLogFormat.JSON)
            written = b"".join(sd._transport.stream.written)
            # SD:FILE "log_YYYYMMDD_HHMMSS.json"
            assert b'SD:FILE "log_' in written
            assert b'.json"' in written
        finally:
            sd.disconnect()

    def test_channel_mask_emits_enable_adc_channels(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            sd.start_sd_card_logging(file_name="r.bin", channel_mask="3")
            written = b"".join(sd._transport.stream.written)
            assert b"ENAble:VOLTage:DC 3" in written
        finally:
            sd.disconnect()

    def test_rollback_restores_lan_on_partial_failure(self) -> None:
        # If a SCPI step in the middle of the configure-then-start
        # sequence raises, LAN must be re-enabled before the exception
        # propagates (Qodo PR #41 finding 8).
        sd = _make_device_with_serial_facade()
        try:
            # Inject a failure between the LAN-disable and the start.
            real_send = sd.send
            seen: list = []

            def failing_send(msg) -> None:
                seen.append(getattr(msg, "data", str(msg)))
                if "SD:FILE" in seen[-1]:
                    raise RuntimeError("simulated mid-sequence failure")
                real_send(msg)

            sd.send = failing_send  # type: ignore[assignment]
            with pytest.raises(RuntimeError, match="simulated"):
                sd.start_sd_card_logging(file_name="rollback.bin")

            written = b"".join(sd._transport.stream.written)
            # LAN was disabled, then re-enabled on rollback.
            assert b"LAN:ENAbled 0" in written
            assert b"LAN:ENAbled 1" in written
            # State flags remained in their pre-call state.
            assert sd.is_logging_to_sd_card is False
            assert sd.is_streaming is False
        finally:
            sd.disconnect()


# ---- stop_sd_card_logging --------------------------------------------------


class TestStopSdCardLogging:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.stop_sd_card_logging()

    def test_dispatches_full_teardown_sequence(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            sd.start_sd_card_logging(file_name="r.bin")
            sd._transport.stream.written.clear()
            sd.stop_sd_card_logging()
            written = b"".join(sd._transport.stream.written)
            assert b"StopStreamData" in written
            assert b"SD:ENAble 0" in written
            assert b"SYSTem:STReam:INTerface 0" in written
            assert b"LAN:ENAbled 1" in written
            assert sd.is_logging_to_sd_card is False
            assert sd.is_streaming is False
        finally:
            sd.disconnect()


# ---- format_sd_card --------------------------------------------------------


class TestFormatSdCard:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.format_sd_card()

    def test_requires_usb_transport(self) -> None:
        # Format disables LAN to take the SPI bus — refuse over LAN
        # so the call doesn't sever its own control connection.
        sd = _make_device_with_serial_facade(usb=False)
        try:
            with pytest.raises(RuntimeError, match="USB"):
                sd.format_sd_card()
        finally:
            sd.disconnect()

    def test_blocked_while_logging(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            sd._is_logging_to_sd = True
            with pytest.raises(RuntimeError, match="while logging"):
                sd.format_sd_card()
        finally:
            sd.disconnect()

    def test_dispatches_format_command_with_lan_disable_first(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            sd.format_sd_card()
            written = b"".join(sd._transport.stream.written)
            # Must disable LAN before enabling SD (precondition on
            # enable_storage_sd) — Qodo PR #41 finding 7.
            lan_off = written.find(b"LAN:ENAbled 0")
            sd_on = written.find(b"SD:ENAble 1")
            sd_format = written.find(b"SD:FORmat")
            assert lan_off >= 0 and sd_on > lan_off and sd_format > sd_on
        finally:
            sd.disconnect()


# ---- delete_sd_card_file ---------------------------------------------------


class TestDeleteSdCardFile:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.delete_sd_card_file("x.bin")

    def test_requires_usb_transport(self) -> None:
        # Delete uses prepare_sd_interface (disables LAN) — Qodo
        # PR #41 finding 4.
        sd = _make_device_with_serial_facade(usb=False)
        try:
            with pytest.raises(RuntimeError, match="USB"):
                sd.delete_sd_card_file("x.bin")
        finally:
            sd.disconnect()

    def test_blocked_while_logging(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            sd._is_logging_to_sd = True
            with pytest.raises(RuntimeError, match="while logging"):
                sd.delete_sd_card_file("x.bin")
        finally:
            sd.disconnect()

    def test_empty_filename_raises(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            with pytest.raises(ValueError, match="must not be empty"):
                sd.delete_sd_card_file("")
        finally:
            sd.disconnect()

    def test_invalid_filename_raises(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            with pytest.raises(ValueError, match="invalid characters"):
                sd.delete_sd_card_file('bad"name.bin')
        finally:
            sd.disconnect()

    def test_refreshes_cache_from_post_delete_listing(self) -> None:
        # After SD:LIST? lands, the firmware replies with the listing.
        # We register the response in the trigger map so the bytes
        # only enter the read buffer after the consumer is paused.
        listing = b"\r\nDAQIFI> log_one.bin\r\nlog_two.bin\r\n"
        sd = _make_device_with_serial_facade(
            response_map={b"SD:LIST?": listing}
        )
        try:
            sd.delete_sd_card_file("log_old.bin")
            assert [f.file_name for f in sd.sd_card_files] == [
                "log_one.bin",
                "log_two.bin",
            ]
            written = b"".join(sd._transport.stream.written)
            assert b'SD:DELete "log_old.bin"' in written
            assert b"SD:LIST?" in written
        finally:
            sd.disconnect()


# ---- get_sd_card_files -----------------------------------------------------


class TestGetSdCardFiles:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.get_sd_card_files()

    def test_requires_usb_transport(self) -> None:
        # get_sd_card_files uses prepare_sd_interface (disables LAN)
        # — Qodo PR #41 finding 4.
        sd = _make_device_with_serial_facade(usb=False)
        try:
            with pytest.raises(RuntimeError, match="USB"):
                sd.get_sd_card_files()
        finally:
            sd.disconnect()

    def test_returns_parsed_list_and_caches(self) -> None:
        listing = b"DAQIFI> log_20260101_010101.bin\r\nlog_20260101_020202.bin\r\n"
        sd = _make_device_with_serial_facade(
            response_map={b"SD:LIST?": listing}
        )
        try:
            files = sd.get_sd_card_files()
            names = [f.file_name for f in files]
            assert "log_20260101_010101.bin" in names
            assert "log_20260101_020202.bin" in names
            # Cached on the instance
            assert sd.sd_card_files == tuple(files)
        finally:
            sd.disconnect()


# ---- download_sd_card_file -------------------------------------------------


class TestDownloadSdCardFile:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.download_sd_card_file("x.bin", BytesIO())

    def test_requires_usb_transport(self) -> None:
        # The base DaqifiStreamingDevice's is_usb_connection is False
        # for any non-SerialStreamTransport transport.
        sd = _make_device_with_serial_facade(usb=False)
        try:
            with pytest.raises(RuntimeError, match="USB"):
                sd.download_sd_card_file("x.bin", BytesIO())
        finally:
            sd.disconnect()

    def test_empty_filename_raises(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            with pytest.raises(ValueError, match="must not be empty"):
                sd.download_sd_card_file("", BytesIO())
        finally:
            sd.disconnect()

    def test_destination_stream_required(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            with pytest.raises(TypeError):
                sd.download_sd_card_file("x.bin", None)  # type: ignore[arg-type]
        finally:
            sd.disconnect()

    def test_blocked_while_logging(self) -> None:
        sd = _make_device_with_serial_facade()
        try:
            sd._is_logging_to_sd = True
            with pytest.raises(RuntimeError, match="while logging"):
                sd.download_sd_card_file("x.bin", BytesIO())
        finally:
            sd.disconnect()

    def test_streams_payload_until_eof_marker(self) -> None:
        payload = b"hello world this is the file body"
        framed = b"DAQIFI> " + payload + END_OF_FILE_MARKER + b"\r\n"
        sd = _make_device_with_serial_facade(
            response_map={b"SD:GET": framed}
        )
        dest = BytesIO()
        progress_calls: list = []
        try:
            result = sd.download_sd_card_file(
                "log_x.bin",
                dest,
                progress=lambda p: progress_calls.append(p),
            )
        finally:
            sd.disconnect()
        assert dest.getvalue() == payload
        assert isinstance(result, SdCardDownloadResult)
        assert result.file_name == "log_x.bin"
        assert result.file_size == len(payload)
        assert result.file_path is None
        assert progress_calls  # at least one progress event
        assert progress_calls[-1].bytes_received == len(payload)
        assert progress_calls[-1].file_name == "log_x.bin"

    def test_idle_timeout_aborts_when_no_bytes_arrive(self) -> None:
        # No response_map → no bytes ever flow back. Drop the idle
        # timeout so the test exits in <1s rather than 30s.
        sd = _make_device_with_serial_facade()
        sd._SD_DOWNLOAD_IDLE_TIMEOUT_S = 0.5
        try:
            with pytest.raises(TimeoutError, match="stalled"):
                sd.download_sd_card_file("log_idle.bin", BytesIO())
        finally:
            sd.disconnect()

    def test_total_cap_aborts_even_with_continuous_drip(self) -> None:
        # Continuous data without an EOF marker must still fall over
        # once the total cap elapses (Qodo PR #41 finding 5).
        from io import BytesIO

        # Trigger fires a single drip; subsequent idle reads see nothing.
        # Use a short total cap + idle cap > 0 to give the loop one
        # iteration of "data arrived" before the total cap expires.
        sd = _make_device_with_serial_facade(
            response_map={b"SD:GET": b"DAQIFI> trickle-without-eof"}
        )
        sd._SD_DOWNLOAD_TIMEOUT_S = 0.3
        sd._SD_DOWNLOAD_IDLE_TIMEOUT_S = 5.0
        try:
            with pytest.raises(TimeoutError):
                sd.download_sd_card_file("log_drip.bin", BytesIO())
        finally:
            sd.disconnect()

    def test_binary_payload_containing_daqifi_prompt_bytes_not_corrupted(self) -> None:
        # Binary payloads can incidentally contain the ASCII bytes that
        # spell ``DAQIFI>``. The prompt-strip must only fire at the head
        # of the response, never search the body (Qodo PR #41 pass 3 #3).
        evil_payload = (
            b"\x00\x01\x02DAQIFI> middle\x03\x04"
            + b"more body bytes "
            + b"DAQIFI>"
            + b"\x05\x06\x07"
        )
        framed = b"DAQIFI> " + evil_payload + END_OF_FILE_MARKER
        sd = _make_device_with_serial_facade(
            response_map={b"SD:GET": framed}
        )
        dest = BytesIO()
        try:
            result = sd.download_sd_card_file("log_evil.bin", dest)
        finally:
            sd.disconnect()
        assert dest.getvalue() == evil_payload
        assert result.file_size == len(evil_payload)

    def test_eof_marker_split_across_chunks(self) -> None:
        payload = b"abcdefghij"
        # Concatenate head + tail; cap reads to 8 bytes so the receiver
        # observes the marker straddling its trailing window.
        framed = b"DAQIFI> " + payload + END_OF_FILE_MARKER + b"\r\n"
        sd = _make_device_with_serial_facade(
            response_map={b"SD:GET": framed},
            max_read_bytes=8,
        )
        dest = BytesIO()
        try:
            result = sd.download_sd_card_file("log_split.bin", dest)
        finally:
            sd.disconnect()
        assert dest.getvalue() == payload
        assert result.file_size == len(payload)


# ---- download_sd_card_file_to_temp ----------------------------------------


class TestDownloadSdCardFileToTemp:
    def test_writes_to_temp_path_and_reports_it(self) -> None:
        payload = b"temp-file-payload"
        framed = b"DAQIFI> " + payload + END_OF_FILE_MARKER
        sd = _make_device_with_serial_facade(
            response_map={b"SD:GET": framed}
        )
        try:
            result = sd.download_sd_card_file_to_temp("log_t.bin")
        finally:
            sd.disconnect()
        assert result.file_path is not None
        try:
            assert os.path.exists(result.file_path)
            with open(result.file_path, "rb") as f:
                assert f.read() == payload
        finally:
            if result.file_path and os.path.exists(result.file_path):
                os.unlink(result.file_path)


# ---- parse_sd_card_file_list ----------------------------------------------


class TestParseSdCardFileList:
    def test_parses_simple_list(self) -> None:
        out = parse_sd_card_file_list(["a.bin", "b.bin"])
        assert [f.file_name for f in out] == ["a.bin", "b.bin"]

    def test_strips_daqifi_directory_prefix(self) -> None:
        out = parse_sd_card_file_list(["Daqifi/log_20260101_010101.bin"])
        assert out[0].file_name == "log_20260101_010101.bin"
        assert out[0].created_date is not None

    def test_skips_blank_and_error_lines(self) -> None:
        out = parse_sd_card_file_list(["", "  ", "**ERROR: -200", "good.bin"])
        assert [f.file_name for f in out] == ["good.bin"]

    def test_keeps_first_token_when_size_present(self) -> None:
        out = parse_sd_card_file_list(["data.bin 4096"])
        assert out[0].file_name == "data.bin"

    def test_drops_control_character_entries(self) -> None:
        out = parse_sd_card_file_list(["good.bin", "bad\x01.bin"])
        assert [f.file_name for f in out] == ["good.bin"]

    def test_parses_log_file_date(self) -> None:
        out = parse_sd_card_file_list(["log_20260102_030405.bin"])
        assert out[0].created_date is not None
        assert out[0].created_date.year == 2026
        assert out[0].created_date.month == 1
        assert out[0].created_date.day == 2
        assert out[0].created_date.hour == 3
        assert out[0].created_date.minute == 4
        assert out[0].created_date.second == 5

    def test_unknown_format_no_date(self) -> None:
        out = parse_sd_card_file_list(["misc.csv"])
        assert out[0].created_date is None
