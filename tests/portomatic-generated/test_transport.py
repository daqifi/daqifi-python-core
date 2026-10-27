"""Tests for the four concrete transports in daqifi/transport.py.
Upstream snapshot e8d84af. Hardware-coupled probe loops are exercised
in tests/integration/; this file covers the API surface, sync I/O over
loopback (TCP, UDP), event subscription, error mapping, timeouts, and
disposal."""

from __future__ import annotations

import socket
import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from daqifi._internal.comm import (
    ConnectionRetryOptions,
    HidDeviceInfo,
    StreamTransport,
    TransportStatusEventArgs,
    UdpTransport as UdpTransportProtocol,
)
from daqifi.transport import (
    HidLibraryTransport,
    SerialStreamTransport,
    TcpStreamTransport,
    UdpTransport,
)


pytestmark = pytest.mark.portomatic_generated


# ---- TcpStreamTransport ------------------------------------------------------


@pytest.fixture
def echo_tcp_server():
    """Bind a localhost TCP listener and yield (host, port). Closes on exit."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    accepted: list = []

    def _accept():
        try:
            conn, _ = listener.accept()
            accepted.append(conn)
        except Exception:
            pass

    t = threading.Thread(target=_accept, daemon=True)
    t.start()

    yield (host, port)

    for c in accepted:
        try:
            c.close()
        except Exception:
            pass
    listener.close()
    t.join(timeout=1.0)


class TestTcpStreamTransport:
    def test_satisfies_protocol(self):
        t = TcpStreamTransport("127.0.0.1", 1)
        assert isinstance(t, StreamTransport)

    def test_initial_state(self):
        t = TcpStreamTransport("127.0.0.1", 1)
        assert not t.is_connected
        assert t.stream is None
        assert "Disconnected" in t.connection_info

    def test_connect_and_disconnect_against_real_listener(self, echo_tcp_server):
        host, port = echo_tcp_server
        t = TcpStreamTransport(host, port)

        events: list = []
        t.add_status_changed(events.append)

        t.connect()
        try:
            assert t.is_connected
            assert t.stream is not None
            assert "Disconnected" not in t.connection_info
            assert events and events[-1].is_connected is True
        finally:
            t.disconnect()
            assert not t.is_connected
            assert events[-1].is_connected is False

    def test_connect_fails_when_no_listener(self):
        # Pick an unused port reserved by binding then immediately closing.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        t = TcpStreamTransport("127.0.0.1", port)
        events: list = []
        t.add_status_changed(events.append)

        with pytest.raises(Exception):
            t.connect(retry_options=ConnectionRetryOptions(
                enabled=True, max_attempts=1, connection_timeout=0.5,
            ))

        assert events and events[-1].error is not None

    def test_remove_status_handler(self):
        t = TcpStreamTransport("127.0.0.1", 1)
        called: list = []

        def h(_args):
            called.append(1)

        t.add_status_changed(h)
        t.remove_status_changed(h)
        # Removing again is a no-op.
        t.remove_status_changed(h)
        # Force a status notification by closing while not connected.
        t.close()
        assert called == []

    def test_close_makes_subsequent_calls_raise(self):
        t = TcpStreamTransport("127.0.0.1", 1)
        t.close()
        with pytest.raises(RuntimeError, match="closed"):
            _ = t.stream


# ---- UdpTransport ------------------------------------------------------------


class TestUdpTransport:
    def test_satisfies_protocol(self):
        u = UdpTransport()
        assert isinstance(u, UdpTransportProtocol)

    def test_initial_state(self):
        u = UdpTransport()
        assert not u.is_open
        assert u.local_port == 0
        assert "Closed" in u.connection_info

    def test_open_and_close(self):
        u = UdpTransport()
        events: list = []
        u.add_status_changed(events.append)
        u.open()
        try:
            assert u.is_open
            assert u.local_port > 0
            assert events and events[-1].is_connected is True
        finally:
            u.close()
            assert not u.is_open
            assert events[-1].is_connected is False

    def test_send_unicast_and_receive(self):
        receiver = UdpTransport()
        sender = UdpTransport()
        receiver.open()
        sender.open()
        try:
            payload = b"hello daqifi"
            sender.send_unicast(payload, ("127.0.0.1", receiver.local_port))
            result = receiver.receive(timeout=timedelta(seconds=1))
            assert result is not None
            data, addr = result
            assert data == payload
            assert addr[0] == "127.0.0.1"
        finally:
            sender.close()
            receiver.close()

    def test_receive_returns_none_on_timeout(self):
        u = UdpTransport()
        u.open()
        try:
            assert u.receive(timeout=timedelta(milliseconds=50)) is None
        finally:
            u.close()

    def test_send_when_closed_raises(self):
        u = UdpTransport()
        with pytest.raises(RuntimeError, match="not open"):
            u.send_broadcast(b"x", 30303)
        with pytest.raises(RuntimeError, match="not open"):
            u.send_unicast(b"x", ("127.0.0.1", 1))


# ---- SerialStreamTransport ---------------------------------------------------


class TestSerialStreamTransport:
    def test_satisfies_protocol(self):
        t = SerialStreamTransport("COM_FAKE")
        assert isinstance(t, StreamTransport)

    def test_empty_port_name_rejected(self):
        with pytest.raises(ValueError):
            SerialStreamTransport("")

    def test_initial_state(self):
        t = SerialStreamTransport("COM_FAKE", baud_rate=115200)
        assert not t.is_connected
        assert "COM_FAKE" in t.connection_info
        assert "Disconnected" in t.connection_info

    def test_get_available_port_names_returns_list(self):
        names = SerialStreamTransport.get_available_port_names()
        assert isinstance(names, list)
        # Don't assert contents — depends on the test host.

    def test_connect_failure_fires_error_event(self):
        t = SerialStreamTransport("COM_NONEXISTENT_99999")
        events: list = []
        t.add_status_changed(events.append)
        with pytest.raises(Exception):
            t.connect(retry_options=ConnectionRetryOptions(
                enabled=True, max_attempts=1, connection_timeout=0.1,
            ))
        assert events and events[-1].error is not None


# ---- HidLibraryTransport -----------------------------------------------------


class _FakeHidDevice:
    def __init__(self, *, fail_open=False, write_returns=True,
                 read_returns=b"\x01\x02"):
        self._open = not fail_open
        self.is_connected = True
        self._write_returns = write_returns
        self._read_returns = read_returns
        self.write_calls: list = []
        self.close_called = 0

    def write(self, data, timeout_ms):
        self.write_calls.append((bytes(data), timeout_ms))
        return self._write_returns

    def read(self, timeout_ms):
        if not self._read_returns:
            raise TimeoutError("no data")
        return self._read_returns

    def close(self):
        self.close_called += 1
        self.is_connected = False


class _FakeHidPlatform:
    def __init__(self, devices, device_for_open=None, open_raises=None):
        self._devices = list(devices)
        self._device_for_open = device_for_open or _FakeHidDevice()
        self._open_raises = open_raises

    def enumerate_devices(self):
        return iter(self._devices)

    def open_device(self, info: HidDeviceInfo):
        if self._open_raises is not None:
            raise self._open_raises
        return self._device_for_open


_DAQIFI_INFO = HidDeviceInfo(
    vendor_id=0x04D8,
    product_id=0x000A,
    device_path="/dev/hidraw0",
    serial_number="SN-ABC",
    product_name="Daqifi NQ1",
)


class TestHidLibraryTransport:
    def test_initial_state(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([_DAQIFI_INFO]))
        assert not t.is_connected
        assert t.vendor_id is None
        assert t.read_timeout == timedelta(seconds=10)
        assert t.write_timeout == timedelta(seconds=10)

    def test_connect_records_device_metadata(self):
        platform = _FakeHidPlatform([_DAQIFI_INFO])
        t = HidLibraryTransport(platform=platform)
        t.connect(0x04D8, 0x000A, serial_number="SN-ABC")
        assert t.is_connected
        assert t.vendor_id == 0x04D8
        assert t.product_id == 0x000A
        assert t.serial_number == "SN-ABC"
        assert t.device_path == "/dev/hidraw0"

    def test_connect_no_match_raises(self):
        platform = _FakeHidPlatform([_DAQIFI_INFO])
        t = HidLibraryTransport(platform=platform)
        with pytest.raises(IOError, match="No HID device found"):
            t.connect(0x9999, 0xFFFF)

    def test_connect_serial_mismatch_raises(self):
        platform = _FakeHidPlatform([_DAQIFI_INFO])
        t = HidLibraryTransport(platform=platform)
        with pytest.raises(IOError, match="Serial=NOPE"):
            t.connect(0x04D8, 0x000A, serial_number="NOPE")

    def test_connect_serial_filter_is_case_insensitive(self):
        platform = _FakeHidPlatform([_DAQIFI_INFO])
        t = HidLibraryTransport(platform=platform)
        # Upstream uses StringComparison.OrdinalIgnoreCase.
        t.connect(0x04D8, 0x000A, serial_number="sn-abc")
        assert t.is_connected
        assert t.serial_number == "SN-ABC"

    def test_connect_by_path_opens_that_device_among_identical_ones(self):
        # Audit finding on #165: two bootloaders with the same VID/PID and no
        # serial string; connecting by VID/PID/serial opened the first by
        # path order, not the one the updater had selected.
        first = HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw0", None, "Bootloader")
        second = HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw1", None, "Bootloader")
        opened = []

        class _RecordingPlatform(_FakeHidPlatform):
            def open_device(self, info):
                opened.append(info.device_path)
                return super().open_device(info)

        t = HidLibraryTransport(platform=_RecordingPlatform([first, second]))
        t.connect(0x04D8, 0x003C, None, device_path="/dev/hidraw1")
        assert opened == ["/dev/hidraw1"]
        assert t.device_path == "/dev/hidraw1"

    def test_connect_by_path_accepts_the_bytes_form_hidapi_returns(self):
        # Audit finding on #165: the platform stores paths as str, so a
        # bytes path (hidapi's own form) never matched.
        info = HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw1", None, "Bootloader")
        t = HidLibraryTransport(platform=_FakeHidPlatform([info]))
        t.connect(0x04D8, 0x003C, device_path=b"/dev/hidraw1")
        assert t.device_path == "/dev/hidraw1"

    def test_connect_by_path_that_is_gone_raises(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([_DAQIFI_INFO]))
        with pytest.raises(IOError, match="Path='/dev/hidraw9'"):
            t.connect(0x04D8, 0x000A, device_path="/dev/hidraw9")

    def test_serial_transport_exposes_its_port_name(self):
        assert SerialStreamTransport("/dev/ttyACM7").port_name == "/dev/ttyACM7"

    def test_invalid_vendor_id_rejected(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([]))
        with pytest.raises(ValueError, match="vendor_id"):
            t.connect(-1, 0x000A)

    def test_invalid_product_id_rejected(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([]))
        with pytest.raises(ValueError, match="product_id"):
            t.connect(0x04D8, 0x10000)

    def test_write_succeeds(self):
        fake_dev = _FakeHidDevice()
        platform = _FakeHidPlatform([_DAQIFI_INFO], device_for_open=fake_dev)
        t = HidLibraryTransport(platform=platform)
        t.connect(0x04D8, 0x000A)
        t.write(b"abc")
        assert fake_dev.write_calls == [(b"abc", 10000)]

    def test_write_empty_rejected(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([_DAQIFI_INFO]))
        t.connect(0x04D8, 0x000A)
        with pytest.raises(ValueError, match="empty"):
            t.write(b"")

    def test_write_failure_raises(self):
        fake_dev = _FakeHidDevice(write_returns=False)
        platform = _FakeHidPlatform([_DAQIFI_INFO], device_for_open=fake_dev)
        t = HidLibraryTransport(platform=platform)
        t.connect(0x04D8, 0x000A)
        with pytest.raises(IOError, match="HID write failed"):
            t.write(b"a")

    def test_write_when_disconnected_raises(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([_DAQIFI_INFO]))
        with pytest.raises(RuntimeError, match="not connected"):
            t.write(b"a")

    def test_read_returns_bytes(self):
        fake_dev = _FakeHidDevice(read_returns=b"\xAA\xBB")
        platform = _FakeHidPlatform([_DAQIFI_INFO], device_for_open=fake_dev)
        t = HidLibraryTransport(platform=platform)
        t.connect(0x04D8, 0x000A)
        assert t.read() == b"\xAA\xBB"

    def test_read_timeout_must_be_positive(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([_DAQIFI_INFO]))
        t.connect(0x04D8, 0x000A)
        with pytest.raises(ValueError, match="greater than zero"):
            t.read(timeout=timedelta(0))

    def test_read_timeout_property_validates(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([]))
        with pytest.raises(ValueError):
            t.read_timeout = timedelta(0)

    def test_disconnect_clears_state(self):
        fake_dev = _FakeHidDevice()
        platform = _FakeHidPlatform([_DAQIFI_INFO], device_for_open=fake_dev)
        t = HidLibraryTransport(platform=platform)
        t.connect(0x04D8, 0x000A)
        t.disconnect()
        assert not t.is_connected
        assert t.vendor_id is None
        assert fake_dev.close_called == 1

    def test_close_is_idempotent(self):
        t = HidLibraryTransport(platform=_FakeHidPlatform([]))
        t.close()
        t.close()
        with pytest.raises(RuntimeError, match="closed"):
            t.connect(0x04D8, 0x000A)
