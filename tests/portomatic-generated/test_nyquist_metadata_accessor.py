"""Tests for NyquistDevice.metadata + refresh_metadata (closes #74).

The facade-level accessor exposes the protobuf-populated SN (and the
rest of the SystInfoPB-carried metadata) so callers can identity-gate
destructive operations like ``*RST`` without falling back to *IDN?'s
``"0"`` placeholder.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from daqifi._internal.comm import DeviceMetadata
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.device import NyquistDevice
from daqifi.discovery import BoardVariant
from daqifi.exceptions import CommandError, ConnectionError as DAQiFiConnectionError


pytestmark = pytest.mark.portomatic_generated


def _length_prefix(n: int) -> bytes:
    """Encode ``n`` as a protobuf-style varint (uint32)."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _length_prefixed(message: DaqifiOutMessage) -> bytes:
    """Serialize ``message`` and prepend its varint length — matches
    the wire format the device sends and that ``ProtobufMessageParser``
    expects."""
    payload = message.SerializeToString()
    return _length_prefix(len(payload)) + payload


def _make_device_with_mock_comm() -> NyquistDevice:
    """Build a NyquistDevice with a MagicMock SerialInterface and skip
    the real ``_connect`` flow. Sets ``_connected = True`` AND has the
    fake ``is_open()`` return True, so ``is_connected`` evaluates True
    and the refresh_metadata precondition passes."""
    dev = NyquistDevice("/dev/ttyACM0")
    dev._comm = MagicMock()
    dev._comm.is_open.return_value = True
    dev._connected = True
    dev._variant = BoardVariant.NQ1
    return dev


class TestMetadataProperty:
    def test_returns_devicemetadata_instance(self):
        dev = _make_device_with_mock_comm()
        assert isinstance(dev.metadata, DeviceMetadata)

    def test_default_metadata_is_empty(self):
        """Before any refresh, metadata fields are at the dataclass
        defaults — empty strings, zero device_port, etc. The property
        is a pure accessor, so accessing it does NOT trigger a
        SYSInfoPB? round-trip; auto-refresh only fires inside
        ``_connect``, explicit refresh via ``refresh_metadata``."""
        dev = _make_device_with_mock_comm()
        md = dev.metadata
        assert md.serial_number == ""
        assert md.part_number == ""
        assert md.device_port == 0
        # Pure accessor — no I/O attempted.
        dev._comm.query_binary.assert_not_called()

    def test_metadata_property_returns_same_object(self):
        """The property is a thin accessor — repeat access returns the
        same cached object so mutations from refresh_metadata are
        visible."""
        dev = _make_device_with_mock_comm()
        assert dev.metadata is dev.metadata


class TestRefreshMetadata:
    def test_populates_from_real_protobuf_round_trip(self):
        """End-to-end: build a real DaqifiOutMessage, serialize +
        length-prefix it, feed it through query_binary, and verify
        the cached metadata carries every populated field.

        Identifiers are deliberately synthetic — RFC 5737 IP, an IANA-
        reserved locally-administered MAC, and a 19-digit non-real
        SN. No workstation-specific values committed (PR Compliance
        ID 5).
        """
        dev = _make_device_with_mock_comm()
        msg = DaqifiOutMessage()
        msg.device_pn = "Nq1"
        msg.device_sn = 1234567890123456789
        msg.device_fw_rev = "9.9.9-test"
        msg.device_hw_rev = "0.0.0-test"
        msg.host_name = "DAQiFi-TEST"
        msg.ssid = "test-ssid"
        msg.device_port = 9760  # protocol port — not workstation-specific
        msg.ip_addr = bytes([192, 0, 2, 1])  # RFC 5737 TEST-NET-1
        msg.mac_addr = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])
        dev._comm.query_binary.return_value = _length_prefixed(msg)

        result = dev.refresh_metadata(timeout=2.0)

        assert result is dev.metadata
        assert dev.metadata.serial_number == "1234567890123456789"
        assert dev.metadata.part_number == "Nq1"
        assert dev.metadata.firmware_version == "9.9.9-test"
        assert dev.metadata.hardware_revision == "0.0.0-test"
        assert dev.metadata.host_name == "DAQiFi-TEST"
        assert dev.metadata.ssid == "test-ssid"
        assert dev.metadata.device_port == 9760
        assert dev.metadata.ip_address == "192.0.2.1"
        assert dev.metadata.mac_address == "02-00-00-00-00-01"

    def test_passes_timeout_to_query_binary(self):
        dev = _make_device_with_mock_comm()
        msg = DaqifiOutMessage()
        msg.device_sn = 12345
        dev._comm.query_binary.return_value = _length_prefixed(msg)

        dev.refresh_metadata(timeout=5.5)

        # Default kwarg form: timeout=5.5
        _args, kwargs = dev._comm.query_binary.call_args
        assert kwargs["timeout"] == 5.5

    def test_uses_sysinfopb_query(self):
        dev = _make_device_with_mock_comm()
        msg = DaqifiOutMessage()
        msg.device_sn = 1
        dev._comm.query_binary.return_value = _length_prefixed(msg)

        dev.refresh_metadata()

        sent_query = dev._comm.query_binary.call_args[0][0]
        assert sent_query == "SYSTem:SYSInfoPB?"

    def test_raises_command_error_on_empty_response(self):
        """A device that doesn't reply within the timeout returns
        empty bytes — surface that as ``CommandError`` rather than
        silently leaving the cached metadata empty."""
        dev = _make_device_with_mock_comm()
        dev._comm.query_binary.return_value = b""

        with pytest.raises(CommandError, match="no parseable protobuf"):
            dev.refresh_metadata(timeout=0.1)

    def test_raises_command_error_on_unparseable_response(self):
        """Garbage bytes that don't decode as a length-prefixed
        protobuf — same surface as the empty-response case."""
        dev = _make_device_with_mock_comm()
        # Pure ASCII text — no valid varint length prefix that yields
        # a parseable DaqifiOutMessage payload.
        dev._comm.query_binary.return_value = b"not a protobuf reply"

        with pytest.raises(CommandError, match="no parseable protobuf"):
            dev.refresh_metadata()

    def test_raises_when_disconnected(self):
        dev = _make_device_with_mock_comm()
        dev._connected = False

        with pytest.raises(DAQiFiConnectionError, match="not connected"):
            dev.refresh_metadata()
        # Must short-circuit before any I/O attempt.
        dev._comm.query_binary.assert_not_called()

    def test_raises_when_serial_handle_closed(self):
        """``_connected`` may still be True after a USB drop while
        the underlying serial handle is closed. Use the public
        ``is_connected`` (which checks both) so this case surfaces
        as the documented connection error, not an opaque
        RuntimeError from query_binary."""
        dev = _make_device_with_mock_comm()
        # _connected stays True; is_open() returns False to simulate
        # USB drop after a successful connect.
        dev._comm.is_open.return_value = False

        with pytest.raises(DAQiFiConnectionError, match="not connected"):
            dev.refresh_metadata()
        dev._comm.query_binary.assert_not_called()

    def test_wraps_runtime_error_as_connection_error(self):
        """If query_binary itself raises RuntimeError mid-call (port
        was closed between is_connected check and write), the caller
        should still see DAQiFiConnectionError per the docstring."""
        dev = _make_device_with_mock_comm()
        dev._comm.query_binary.side_effect = RuntimeError("Serial port not open")

        with pytest.raises(DAQiFiConnectionError, match="Cannot refresh metadata"):
            dev.refresh_metadata()

    def test_no_metadata_frame_raises_command_error(self):
        """If the buffer contained ONLY streaming frames (no
        SYSInfoPB? reply) — e.g. SYSInfoPB? was lost on the wire
        but background streaming was active — refresh_metadata
        must raise CommandError so callers can detect the silent
        failure (and identity-gate tests can skip safely instead
        of proceeding with stale metadata).

        Cache stays unchanged — the destructive operation gate
        relies on this so a failed refresh doesn't leave a stale
        SN that would compare-equal to ``DAQIFI_EXPECTED_SERIAL_NUMBER``.
        """
        dev = _make_device_with_mock_comm()
        # Pre-populate cache so we can detect non-mutation.
        dev._metadata.serial_number = "999999"
        dev._metadata.host_name = "previously-cached"
        # Buffer contains only a stream frame (no device_pn/device_sn).
        stream = DaqifiOutMessage()
        stream.msg_time_stamp = 12345
        dev._comm.query_binary.return_value = _length_prefixed(stream)

        with pytest.raises(CommandError, match="none of which carried"):
            dev.refresh_metadata()

        # Cache preserved — the failed refresh didn't clobber it.
        assert dev.metadata.serial_number == "999999"
        assert dev.metadata.host_name == "previously-cached"

    def test_skips_trailing_streaming_frames_to_find_metadata(self):
        """If query_binary's buffer contains a SYSInfoPB? reply
        followed by background-streaming frames (which carry
        sample/timestamp data but NOT device_pn/device_sn),
        refresh_metadata must walk back to the SYSInfoPB frame
        instead of feeding the stream frame to update_from_protobuf
        (which would no-op and leave metadata stale).

        Regression for Qodo PR #85 /agentic_review pass 7 finding #2.
        """
        dev = _make_device_with_mock_comm()
        # First frame: real SYSInfoPB? metadata reply.
        meta = DaqifiOutMessage()
        meta.device_pn = "Nq1"
        meta.device_sn = 1234567890123456789
        meta.host_name = "DAQiFi-TEST"
        # Second frame: a streaming-shaped frame (no device_pn/device_sn).
        stream = DaqifiOutMessage()
        stream.msg_time_stamp = 12345
        # Concatenate length-prefixed frames in arrival order.
        dev._comm.query_binary.return_value = (
            _length_prefixed(meta) + _length_prefixed(stream)
        )

        dev.refresh_metadata()

        # Must apply the metadata frame, NOT the trailing stream frame.
        assert dev.metadata.serial_number == "1234567890123456789"
        assert dev.metadata.part_number == "Nq1"
        assert dev.metadata.host_name == "DAQiFi-TEST"

    def test_repeat_refresh_overlays_new_fields_without_clobbering_old(self):
        """``update_from_protobuf`` only writes non-empty fields, so a
        second protobuf reply that only carries the SN should leave a
        previously-populated SSID intact. Smoke-test that contract
        holds through the facade."""
        dev = _make_device_with_mock_comm()
        first = DaqifiOutMessage()
        first.device_pn = "Nq1"
        first.device_sn = 111
        first.ssid = "test-ssid"
        dev._comm.query_binary.return_value = _length_prefixed(first)
        dev.refresh_metadata()
        assert dev.metadata.ssid == "test-ssid"

        second = DaqifiOutMessage()
        second.device_sn = 222  # only SN, no SSID
        dev._comm.query_binary.return_value = _length_prefixed(second)
        dev.refresh_metadata()
        assert dev.metadata.serial_number == "222"
        assert dev.metadata.ssid == "test-ssid"


class TestConnectIsResilientToMetadataFailure:
    """Regression tests: the best-effort metadata refresh in
    _connect() must NOT abort an otherwise-successful connect, no
    matter which subclass refresh_metadata raises (CommandError,
    DAQiFiConnectionError, or a bare RuntimeError if a future
    backend skips the wrap)."""

    def _make_connectable_device(
        self, refresh_side_effect: Exception
    ) -> NyquistDevice:
        """Build a NyquistDevice whose mocked SerialInterface accepts
        open() and *IDN? but raises ``refresh_side_effect`` from
        refresh_metadata's underlying call — so we exercise the
        full _connect path through to the SYSInfoPB? swallow."""
        dev = NyquistDevice("/dev/ttyACM0")
        dev._comm = MagicMock()
        dev._comm.is_open.return_value = True
        dev._comm.open.return_value = True
        dev._comm.send_query.return_value = "DAQiFi,Nq1,0,01-02"
        dev._comm.query_binary.side_effect = refresh_side_effect
        return dev

    def test_connect_survives_command_error_during_refresh(self):
        dev = self._make_connectable_device(
            refresh_side_effect=Exception("forces empty bytes path")
        )
        # Make query_binary return empty bytes → CommandError
        dev._comm.query_binary.side_effect = None
        dev._comm.query_binary.return_value = b""
        dev._connect()
        assert dev.is_connected is True
        assert dev.metadata.serial_number == ""  # unrefreshed default
        # Observability: the swallowed CommandError is exposed for
        # later inspection.
        assert isinstance(dev.last_metadata_refresh_error, CommandError)

    def test_connect_survives_connection_error_during_refresh(self):
        """The query_binary RuntimeError-to-DAQiFiConnectionError wrap
        must NOT cause _connect to bail out with 'Failed to initialize
        device'. Regression for Qodo PR #85 /improve pass 2 importance 9."""
        dev = self._make_connectable_device(
            refresh_side_effect=RuntimeError("Serial port not open")
        )
        dev._connect()
        assert dev.is_connected is True
        assert dev.metadata.serial_number == ""
        # Observability: refresh_metadata translates RuntimeError to
        # DAQiFiConnectionError, and that's what we capture.
        assert isinstance(dev.last_metadata_refresh_error, DAQiFiConnectionError)

    def test_connect_survives_runtime_error_during_refresh(self):
        """Defensive: if a future backend skips the RuntimeError wrap,
        the bare RuntimeError is still in the swallow list."""
        dev = NyquistDevice("/dev/ttyACM0")
        dev._comm = MagicMock()
        dev._comm.is_open.return_value = True
        dev._comm.open.return_value = True
        dev._comm.send_query.return_value = "DAQiFi,Nq1,0,01-02"
        # Patch refresh_metadata to raise raw RuntimeError; patch.object
        # auto-restores the original on context exit (idiomatic vs
        # manual save/restore + finally).
        with patch.object(
            NyquistDevice,
            "refresh_metadata",
            MagicMock(side_effect=RuntimeError("simulated low-level failure")),
        ):
            dev._connect()
            assert dev.is_connected is True
            assert isinstance(dev.last_metadata_refresh_error, RuntimeError)

    def test_successful_refresh_clears_last_error(self):
        """If the connect-time refresh succeeds, the observability
        slot should be ``None`` — distinguishes "never failed" from
        "captured an error that was later forgotten"."""
        dev = NyquistDevice("/dev/ttyACM0")
        dev._comm = MagicMock()
        dev._comm.is_open.return_value = True
        dev._comm.open.return_value = True
        dev._comm.send_query.return_value = "DAQiFi,Nq1,0,01-02"
        msg = DaqifiOutMessage()
        msg.device_sn = 1234567890123456789
        dev._comm.query_binary.return_value = _length_prefixed(msg)

        dev._connect()

        assert dev.is_connected is True
        assert dev.metadata.serial_number == "1234567890123456789"
        assert dev.last_metadata_refresh_error is None


class TestMetadataIsDistinctFromIdnSerialNumber:
    def test_idn_serial_number_property_unchanged(self):
        """``serial_number`` (the existing property) must still return
        the *IDN? field — the new accessor adds a parallel surface,
        not a replacement."""
        dev = _make_device_with_mock_comm()
        dev._device_info["serial_number"] = "0"  # firmware 3.4.6b1 placeholder

        msg = DaqifiOutMessage()
        msg.device_sn = 1234567890123456789  # synthetic, not workstation-specific
        dev._comm.query_binary.return_value = _length_prefixed(msg)
        dev.refresh_metadata()

        assert dev.serial_number == "0"  # IDN field — unchanged
        assert dev.metadata.serial_number == "1234567890123456789"  # protobuf SN
