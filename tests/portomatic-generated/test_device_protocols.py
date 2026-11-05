"""Tests for the device + transport Protocol ports
(IDevice / IStreamingDevice / IHidDeviceEnumerator / IHidTransport /
IUdpTransport from Daqifi.Core, plus the DeviceStatusEventArgs payload).
Upstream snapshot: e8d84af."""

from datetime import timedelta

import pytest

from daqifi._internal.comm import (
    Device,
    DeviceStatusEventArgs,
    HidDeviceEnumerator,
    HidDeviceInfo,
    HidTransport,
    StreamingDevice,
    UdpEndPoint,
    UdpTransport,
)
from daqifi._internal.scpi import ConnectionStatus


pytestmark = pytest.mark.portomatic_generated


class TestDeviceStatusEventArgs:
    def test_holds_status(self):
        args = DeviceStatusEventArgs(status=ConnectionStatus.Connected)
        assert args.status is ConnectionStatus.Connected

    def test_is_mutable_dataclass(self):
        args = DeviceStatusEventArgs(status=ConnectionStatus.Disconnected)
        args.status = ConnectionStatus.Lost
        assert args.status is ConnectionStatus.Lost


class TestDeviceProtocol:
    def test_runtime_checkable_minimal_implementer(self):
        class _Stub:
            name = "x"
            ip_address = None
            is_connected = False
            status = ConnectionStatus.Disconnected

            def add_status_changed(self, h): pass
            def remove_status_changed(self, h): pass
            def add_message_received(self, h): pass
            def remove_message_received(self, h): pass
            def connect(self): pass
            def disconnect(self): pass
            def send(self, message, on_written=None): pass

        assert isinstance(_Stub(), Device)

    def test_rejects_missing_method(self):
        class _Bad:
            name = "x"
            # missing everything else

        assert not isinstance(_Bad(), Device)


class TestStreamingDeviceProtocol:
    def test_extends_device(self):
        # `issubclass` on Protocols-with-properties is rejected in 3.12+;
        # check the MRO directly instead.
        assert Device in StreamingDevice.__mro__

    def test_runtime_checkable_implementer(self):
        class _Stub:
            name = "s"
            ip_address = None
            is_connected = True
            status = ConnectionStatus.Connected
            streaming_frequency = 100
            is_streaming = False

            def add_status_changed(self, h): pass
            def remove_status_changed(self, h): pass
            def add_message_received(self, h): pass
            def remove_message_received(self, h): pass
            def connect(self): pass
            def disconnect(self): pass
            def send(self, message, on_written=None): pass
            def start_streaming(self): pass
            def stop_streaming(self): pass

        assert isinstance(_Stub(), StreamingDevice)
        assert isinstance(_Stub(), Device)


class TestHidDeviceEnumeratorProtocol:
    def test_runtime_checkable_implementer(self):
        info = HidDeviceInfo(
            vendor_id=0x04D8,
            product_id=0x000A,
            device_path="\\\\?\\hid#vid_04d8",
        )

        class _Stub:
            def enumerate(self, vendor_id=None, product_id=None):
                return [info]

        stub = _Stub()
        assert isinstance(stub, HidDeviceEnumerator)
        result = stub.enumerate(vendor_id=0x04D8, product_id=0x000A)
        assert list(result) == [info]


class TestHidTransportProtocol:
    def test_runtime_checkable_implementer(self):
        class _Stub:
            is_connected = False
            vendor_id = None
            product_id = None
            serial_number = None
            device_path = None
            read_timeout = timedelta(seconds=1)
            write_timeout = timedelta(seconds=1)

            def connect(self, vendor_id, product_id, serial_number=None): pass
            def write(self, data): pass
            def read(self, timeout=None): return b""
            def disconnect(self): pass
            def close(self): pass

        assert isinstance(_Stub(), HidTransport)


class TestUdpTransportProtocol:
    def test_endpoint_is_host_port_tuple(self):
        ep: UdpEndPoint = ("192.168.1.1", 30303)
        assert ep == ("192.168.1.1", 30303)

    def test_runtime_checkable_implementer(self):
        class _Stub:
            is_open = False
            connection_info = "udp://0.0.0.0"

            def add_status_changed(self, h): pass
            def remove_status_changed(self, h): pass
            def open(self): pass
            def close(self): pass
            def send_broadcast(self, data, port): pass
            def send_broadcast_to(self, data, end_point): pass
            def send_unicast(self, data, end_point): pass
            def receive(self, timeout=None): return None

        assert isinstance(_Stub(), UdpTransport)
