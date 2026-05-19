"""Tests for DaqifiDeviceFactory.connect_from_device_info (closes #7).

Ports the C# ``DaqifiDeviceFactory.ConnectFromDeviceInfo[Async]``
dispatcher: routes a :class:`DeviceInfo` from discovery to the
appropriate connect_* call based on ``connection_type`` (WiFi →
TCP, Serial → serial, HID → NotImplementedError).

Coverage focuses on the dispatch + validation logic; the actual
TCP/Serial connect behavior is exercised by the existing
connect_tcp / connect_serial tests.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from daqifi.devices import DaqifiDeviceFactory, DaqifiStreamingDevice
from daqifi.device_finders import ConnectionType, DeviceInfo


pytestmark = pytest.mark.portomatic_generated


class TestConnectFromDeviceInfoWifiDispatch:
    def test_wifi_routes_to_connect_tcp(self):
        info = DeviceInfo(
            name="DAQiFi-TEST",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            port=9760,
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            mock_tcp.return_value = "fake-device"
            result = DaqifiDeviceFactory.connect_from_device_info(info)

        mock_tcp.assert_called_once()
        kwargs = mock_tcp.call_args.kwargs
        assert kwargs["host"] == "198.51.100.5"
        assert kwargs["port"] == 9760
        # Name comes from device_info.name when no override given.
        assert kwargs["device_name"] == "DAQiFi-TEST"
        assert result == "fake-device"

    def test_wifi_explicit_device_name_overrides_info_name(self):
        info = DeviceInfo(
            name="DAQiFi-TEST",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            port=9760,
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            DaqifiDeviceFactory.connect_from_device_info(
                info, device_name="explicit-override"
            )
        assert mock_tcp.call_args.kwargs["device_name"] == "explicit-override"

    def test_wifi_falls_back_to_default_when_info_name_blank(self):
        info = DeviceInfo(
            name="",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            port=9760,
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            DaqifiDeviceFactory.connect_from_device_info(info)
        assert mock_tcp.call_args.kwargs["device_name"] == "DaqifiDevice"

    def test_wifi_missing_ip_raises_valueerror(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address=None,
            port=9760,
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            with pytest.raises(ValueError, match="ip_address"):
                DaqifiDeviceFactory.connect_from_device_info(info)
            mock_tcp.assert_not_called()

    def test_wifi_missing_port_raises_valueerror(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            port=None,
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            with pytest.raises(ValueError, match="port for WiFi"):
                DaqifiDeviceFactory.connect_from_device_info(info)
            mock_tcp.assert_not_called()

    def test_wifi_whitespace_only_ip_raises_valueerror(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address="   ",
            port=9760,
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            with pytest.raises(ValueError, match="ip_address"):
                DaqifiDeviceFactory.connect_from_device_info(info)
            mock_tcp.assert_not_called()

    def test_wifi_float_port_raises_valueerror(self):
        """``int(9760.9)`` would silently truncate to 9760, targeting
        a different port than the caller supplied. Surface as a
        descriptive ValueError instead."""
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            port=9760.9,  # type: ignore[arg-type]
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            with pytest.raises(ValueError, match="port must be an integer"):
                DaqifiDeviceFactory.connect_from_device_info(info)
            mock_tcp.assert_not_called()

    def test_wifi_string_port_accepted_when_numeric(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            port="9760",  # type: ignore[arg-type]
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            DaqifiDeviceFactory.connect_from_device_info(info)
        assert mock_tcp.call_args.kwargs["port"] == 9760

    def test_wifi_string_port_rejected_when_garbage(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            port="9760a",  # type: ignore[arg-type]
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            with pytest.raises(ValueError, match="port must be an integer"):
                DaqifiDeviceFactory.connect_from_device_info(info)
            mock_tcp.assert_not_called()

    def test_wifi_strips_whitespace_before_forwarding(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address="  198.51.100.5  ",
            port=9760,
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            DaqifiDeviceFactory.connect_from_device_info(info)
        assert mock_tcp.call_args.kwargs["host"] == "198.51.100.5"


class TestConnectFromDeviceInfoSerialDispatch:
    def test_serial_routes_to_connect_serial(self):
        info = DeviceInfo(
            name="DAQiFi-SERIAL",
            connection_type=ConnectionType.SERIAL,
            port_name="<placeholder-serial-port>",
        )
        with patch.object(DaqifiDeviceFactory, "connect_serial") as mock_serial:
            mock_serial.return_value = "fake-serial-device"
            result = DaqifiDeviceFactory.connect_from_device_info(info)

        mock_serial.assert_called_once()
        kwargs = mock_serial.call_args.kwargs
        assert kwargs["port_name"] == "<placeholder-serial-port>"
        assert kwargs["device_name"] == "DAQiFi-SERIAL"
        assert result == "fake-serial-device"

    def test_serial_none_port_name_raises_typeerror(self):
        """Match connect_serial's contract: ``port_name=None`` is a
        TypeError (programmer error), distinct from empty-string
        ValueError (configuration error)."""
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.SERIAL,
            port_name=None,
        )
        with patch.object(DaqifiDeviceFactory, "connect_serial") as mock_serial:
            with pytest.raises(TypeError, match="must not be None"):
                DaqifiDeviceFactory.connect_from_device_info(info)
            mock_serial.assert_not_called()

    def test_serial_empty_port_name_raises_valueerror(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.SERIAL,
            port_name="   ",
        )
        with patch.object(DaqifiDeviceFactory, "connect_serial") as mock_serial:
            with pytest.raises(ValueError, match="must not be empty"):
                DaqifiDeviceFactory.connect_from_device_info(info)
            mock_serial.assert_not_called()

    def test_serial_strips_whitespace_before_forwarding(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.SERIAL,
            port_name="  /placeholder/port  ",
        )
        with patch.object(DaqifiDeviceFactory, "connect_serial") as mock_serial:
            DaqifiDeviceFactory.connect_from_device_info(info)
        assert mock_serial.call_args.kwargs["port_name"] == "/placeholder/port"


class TestConnectFromDeviceInfoUnsupported:
    def test_hid_raises_notimplementederror(self):
        """HID is for bootloader mode only — matches C#
        NotSupportedException semantics."""
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.HID,
            device_path="<placeholder-hid-path>",
        )
        with pytest.raises(NotImplementedError, match="HID"):
            DaqifiDeviceFactory.connect_from_device_info(info)

    def test_unknown_connection_type_raises_notimplementederror(self):
        # Use a frozen dataclass-like object with a bogus connection_type
        # to exercise the catch-all branch.
        class FakeInfo:
            connection_type = "ftdi-magic"
            name = ""

        with pytest.raises(NotImplementedError, match="not supported"):
            DaqifiDeviceFactory.connect_from_device_info(FakeInfo())

    def test_none_device_info_raises_typeerror(self):
        with pytest.raises(TypeError, match="must not be None"):
            DaqifiDeviceFactory.connect_from_device_info(None)


class TestConnectFromDeviceInfoOptionsForwarding:
    """Verify retry/initialize/factory kwargs reach the underlying
    connect_* unchanged."""

    def test_forwards_retry_initialize_message_factory(self):
        from daqifi._internal.comm import ConnectionRetryOptions

        retry = ConnectionRetryOptions(enabled=False)
        factory_fn = lambda b: object()
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            port=9760,
        )
        with patch.object(DaqifiDeviceFactory, "connect_tcp") as mock_tcp:
            DaqifiDeviceFactory.connect_from_device_info(
                info,
                retry_options=retry,
                initialize_device=False,
                message_factory=factory_fn,
            )
        kwargs = mock_tcp.call_args.kwargs
        assert kwargs["retry_options"] is retry
        assert kwargs["initialize_device"] is False
        assert kwargs["message_factory"] is factory_fn
