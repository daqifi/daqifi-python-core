"""Tests for NyquistDevice.metadata + refresh_metadata (closes #74).

The facade-level accessor exposes the protobuf-populated SN (and the
rest of the SystInfoPB-carried metadata) so callers can identity-gate
destructive operations like ``*RST`` without falling back to *IDN?'s
``"0"`` placeholder.
"""
from __future__ import annotations

from unittest.mock import MagicMock

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
    the real ``_connect`` flow. Sets ``_connected = True`` so the
    refresh_metadata pre-check passes."""
    dev = NyquistDevice("/dev/ttyACM0")
    dev._comm = MagicMock()
    dev._connected = True
    dev._variant = BoardVariant.NQ1
    return dev


class TestMetadataProperty:
    def test_returns_devicemetadata_instance(self):
        dev = _make_device_with_mock_comm()
        assert isinstance(dev.metadata, DeviceMetadata)

    def test_default_metadata_is_empty(self):
        """Before any refresh, metadata fields are at the dataclass
        defaults — empty strings, zero device_port, etc. (Verifies
        the property doesn't auto-trigger a refresh on plain access;
        the auto-refresh fires inside ``_connect`` and explicit calls
        to ``refresh_metadata`` only.)"""
        dev = _make_device_with_mock_comm()
        md = dev.metadata
        assert md.serial_number == ""
        assert md.part_number == ""
        assert md.device_port == 0

    def test_metadata_property_returns_same_object(self):
        """The property is a thin accessor — repeat access returns the
        same cached object so mutations from refresh_metadata are
        visible."""
        dev = _make_device_with_mock_comm()
        assert dev.metadata is dev.metadata


class TestRefreshMetadata:
    def test_populates_from_real_protobuf_round_trip(self):
        """End-to-end: build a real DaqifiOutMessage with the
        canonical SN of our test rig, serialize + length-prefix it,
        feed it through query_binary, and verify the cached metadata
        carries every populated field."""
        dev = _make_device_with_mock_comm()
        msg = DaqifiOutMessage()
        msg.device_pn = "Nq1"
        msg.device_sn = 9090696306837482193
        msg.device_fw_rev = "3.4.6b1"
        msg.device_hw_rev = "2.0.0"
        msg.host_name = "DAQiFi-A781"
        msg.ssid = "Tesla"
        msg.device_port = 9760
        msg.ip_addr = bytes([192, 168, 1, 60])
        msg.mac_addr = bytes([0xF8, 0xF0, 0x05, 0xA4, 0xA7, 0x81])
        dev._comm.query_binary.return_value = _length_prefixed(msg)

        result = dev.refresh_metadata(timeout=2.0)

        assert result is dev.metadata
        assert dev.metadata.serial_number == "9090696306837482193"
        assert dev.metadata.part_number == "Nq1"
        assert dev.metadata.firmware_version == "3.4.6b1"
        assert dev.metadata.hardware_revision == "2.0.0"
        assert dev.metadata.host_name == "DAQiFi-A781"
        assert dev.metadata.ssid == "Tesla"
        assert dev.metadata.device_port == 9760
        assert dev.metadata.ip_address == "192.168.1.60"
        assert dev.metadata.mac_address == "F8-F0-05-A4-A7-81"

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

    def test_repeat_refresh_overlays_new_fields_without_clobbering_old(self):
        """``update_from_protobuf`` only writes non-empty fields, so a
        second protobuf reply that only carries the SN should leave a
        previously-populated SSID intact. Smoke-test that contract
        holds through the facade."""
        dev = _make_device_with_mock_comm()
        first = DaqifiOutMessage()
        first.device_pn = "Nq1"
        first.device_sn = 111
        first.ssid = "Tesla"
        dev._comm.query_binary.return_value = _length_prefixed(first)
        dev.refresh_metadata()
        assert dev.metadata.ssid == "Tesla"

        second = DaqifiOutMessage()
        second.device_sn = 222  # only SN, no SSID
        dev._comm.query_binary.return_value = _length_prefixed(second)
        dev.refresh_metadata()
        assert dev.metadata.serial_number == "222"
        assert dev.metadata.ssid == "Tesla"


class TestMetadataIsDistinctFromIdnSerialNumber:
    def test_idn_serial_number_property_unchanged(self):
        """``serial_number`` (the existing property) must still return
        the *IDN? field — the new accessor adds a parallel surface,
        not a replacement."""
        dev = _make_device_with_mock_comm()
        dev._device_info["serial_number"] = "0"  # firmware 3.4.6b1 placeholder

        msg = DaqifiOutMessage()
        msg.device_sn = 9090696306837482193
        dev._comm.query_binary.return_value = _length_prefixed(msg)
        dev.refresh_metadata()

        assert dev.serial_number == "0"  # IDN field — unchanged
        assert dev.metadata.serial_number == "9090696306837482193"  # protobuf SN
