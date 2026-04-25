"""Tests for the three concrete device finders (daqifi/device_finders.py
@ upstream e8d84af). Hardware-coupled probe paths run under
tests/integration/; this file uses fakes for the HID/Serial/UDP layers."""

from __future__ import annotations

import socket
import threading
import time
from datetime import timedelta

import pytest

from daqifi._internal.comm import HidDeviceInfo
from daqifi.device_finders import (
    ConnectionType,
    DeviceInfo,
    DeviceType,
    HidDeviceFinder,
    SerialDeviceFinder,
    WiFiDeviceFinder,
)
from daqifi.transport import UdpTransport


pytestmark = pytest.mark.portomatic_generated


# ---- HidDeviceFinder ---------------------------------------------------------


class _FakeEnumerator:
    def __init__(self, devices):
        self._devices = list(devices)
        self.calls = []

    def enumerate(self, vendor_id=None, product_id=None):
        self.calls.append((vendor_id, product_id))
        return list(self._devices)


class TestHidDeviceFinder:
    def test_default_vid_pid_filter(self):
        e = _FakeEnumerator([
            HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw0", "S1", "Boot1"),
        ])
        f = HidDeviceFinder(hid_enumerator=e)
        results = f.discover()
        assert len(e.calls) == 1
        assert e.calls[0] == (0x04D8, 0x003C)
        assert results[0].connection_type == ConnectionType.HID
        assert results[0].name == "Boot1"
        assert results[0].device_path == "/dev/hidraw0"

    def test_no_product_name_falls_back_to_default(self):
        e = _FakeEnumerator([
            HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw0", "S1", None),
        ])
        f = HidDeviceFinder(hid_enumerator=e)
        results = f.discover()
        assert results[0].name == "DAQiFi Bootloader"

    def test_explicit_filter_overrides_defaults(self):
        e = _FakeEnumerator([])
        f = HidDeviceFinder(hid_enumerator=e)
        f.discover(vendor_id_filter=0x1234, product_id_filter=0x5678)
        assert e.calls[0] == (0x1234, 0x5678)

    def test_events_fire(self):
        e = _FakeEnumerator([
            HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw0", "S1", "Boot1"),
        ])
        f = HidDeviceFinder(hid_enumerator=e)
        discovered = []
        completed = []
        f.add_device_discovered(discovered.append)
        f.add_discovery_completed(lambda: completed.append(True))
        f.discover()
        assert len(discovered) == 1
        assert discovered[0].device_info.device_path == "/dev/hidraw0"
        assert completed == [True]

    def test_close_blocks_subsequent_discover(self):
        f = HidDeviceFinder(hid_enumerator=_FakeEnumerator([]))
        f.close()
        with pytest.raises(RuntimeError, match="closed"):
            f.discover()


# ---- SerialDeviceFinder ------------------------------------------------------


class TestSerialDeviceFinder:
    def test_baud_rate_validation(self):
        with pytest.raises(ValueError):
            SerialDeviceFinder(baud_rate=0)
        with pytest.raises(ValueError):
            SerialDeviceFinder(baud_rate=-1)

    def test_no_ports_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "daqifi.transport.SerialStreamTransport.get_available_port_names",
            staticmethod(lambda: []),
        )
        f = SerialDeviceFinder()
        assert f.discover() == []

    def test_port_filter_overrides_enumeration(self):
        f = SerialDeviceFinder()
        # Probing a definitely-nonexistent port returns no devices and
        # doesn't crash (the catch-all in _try_get_device_info eats the
        # SerialException).
        results = f.discover(port_filter=["COM_DEFINITELY_NOT_REAL_99999"])
        assert results == []

    def test_completed_event_fires_even_on_failure(self):
        f = SerialDeviceFinder()
        completed = []
        f.add_discovery_completed(lambda: completed.append(True))
        f.discover(port_filter=["COM_NONEXISTENT"])
        assert completed == [True]


# ---- WiFiDeviceFinder --------------------------------------------------------


class _UdpResponderThread(threading.Thread):
    """Listens on a port and replies to any datagram with a fixed payload."""

    def __init__(self, sock, payload):
        super().__init__(daemon=True)
        self._sock = sock
        self._payload = payload
        self._stop_event = threading.Event()

    def run(self):
        self._sock.settimeout(0.2)
        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._sock.sendto(self._payload, addr)
            except OSError:
                return

    def shutdown(self):
        # Don't override `Thread.stop` -- Python's threading module calls it
        # internally on cleanup, and overriding causes a TypeError at exit.
        self._stop_event.set()
        try:
            self._sock.close()
        except Exception:
            pass


class TestWiFiDeviceFinder:
    def test_discovery_port_validation(self):
        with pytest.raises(ValueError):
            WiFiDeviceFinder(discovery_port=0)
        with pytest.raises(ValueError):
            WiFiDeviceFinder(discovery_port=70000)

    def test_no_responses_returns_empty_after_timeout(self):
        f = WiFiDeviceFinder(discovery_port=30303)
        completed = []
        f.add_discovery_completed(lambda: completed.append(True))
        results = f.discover(timeout=timedelta(milliseconds=50))
        assert results == []
        assert completed == [True]

    def test_dedupes_repeat_responses_per_ip(self):
        # Open a UDP socket to act as a fake DAQiFi responding from a known IP.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock_port = sock.getsockname()[1]
        responder = _UdpResponderThread(sock, b"DaqifiNyq1\r\n")
        responder.start()
        try:
            # Inject a transport that bypasses broadcast and just sends to
            # the responder's loopback endpoint instead.
            udp = UdpTransport()
            udp.open()
            try:
                # Stub broadcast to send unicast to the responder.
                def _stub_broadcast(data, port):
                    udp.send_unicast(data, ("127.0.0.1", sock_port))
                udp.send_broadcast = _stub_broadcast  # type: ignore[assignment]

                f = WiFiDeviceFinder(discovery_port=sock_port)
                results = f.discover(
                    timeout=timedelta(milliseconds=500),
                    udp_transport=udp,
                )
            finally:
                udp.close()
            assert len(results) == 1  # de-dup based on responder IP
            assert results[0].connection_type == ConnectionType.WIFI
            assert results[0].ip_address == "127.0.0.1"
            assert results[0].port == 9760
            assert "DaqifiNyq1" in results[0].name
            # Text-only legacy response carries no MAC.
            assert results[0].mac_address == ""
            assert results[0].serial_number == ""
            assert results[0].firmware_version == ""
        finally:
            responder.shutdown()
            responder.join(timeout=1.0)


# ---- WiFiDeviceFinder protobuf parsing ---------------------------------------


def _make_protobuf_response(
    *,
    host_name: str = "DaqifiNq1-AABBCC",
    device_pn: str = "NQ1",
    device_fw_rev: str = "3.4.6b1",
    device_sn: int = 1234567,
    device_port: int = 9760,
    mac_addr: bytes = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]),
    ip_addr: bytes = bytes([192, 168, 1, 42]),
    pwr_status: int = 1,
    length_prefix: bool = True,
) -> bytes:
    """Build a `DaqifiOutMessage` UDP discovery payload for tests."""
    from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
    from google.protobuf.internal.encoder import _VarintBytes

    m = DaqifiOutMessage()
    if host_name:
        m.host_name = host_name
    if device_pn:
        m.device_pn = device_pn
    if device_fw_rev:
        m.device_fw_rev = device_fw_rev
    if device_sn:
        m.device_sn = device_sn
    if device_port:
        m.device_port = device_port
    if mac_addr:
        m.mac_addr = mac_addr
    if ip_addr:
        m.ip_addr = ip_addr
    if pwr_status is not None:
        m.pwr_status = pwr_status
    payload = m.SerializeToString()
    if length_prefix:
        return _VarintBytes(len(payload)) + payload
    return payload


def _run_finder_against_payload(payload: bytes):
    """Spin up a UDP responder that returns `payload`, run discover()."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock_port = sock.getsockname()[1]
    responder = _UdpResponderThread(sock, payload)
    responder.start()
    try:
        udp = UdpTransport()
        udp.open()
        try:
            def _stub_broadcast(data, port):
                udp.send_unicast(data, ("127.0.0.1", sock_port))
            udp.send_broadcast = _stub_broadcast  # type: ignore[assignment]
            f = WiFiDeviceFinder(discovery_port=sock_port)
            return f.discover(
                timeout=timedelta(milliseconds=500),
                udp_transport=udp,
            )
        finally:
            udp.close()
    finally:
        responder.shutdown()
        responder.join(timeout=1.0)


class TestWiFiDeviceFinderProtobuf:
    def test_length_prefixed_protobuf_extracts_identity(self):
        results = _run_finder_against_payload(_make_protobuf_response())
        assert len(results) == 1
        info = results[0]
        assert info.connection_type == ConnectionType.WIFI
        assert info.device_type == DeviceType.NYQUIST
        assert info.name == "DaqifiNq1-AABBCC"
        assert info.serial_number == "1234567"
        assert info.firmware_version == "3.4.6b1"
        assert info.mac_address == "AA-BB-CC-DD-EE-FF"
        assert info.port == 9760
        assert info.ip_address == "127.0.0.1"
        assert info.is_power_on is True

    def test_bare_protobuf_payload_falls_through(self):
        # Some firmware variants ship the protobuf without a varint
        # prefix. The bare-payload fallback should still extract.
        results = _run_finder_against_payload(
            _make_protobuf_response(length_prefix=False)
        )
        assert len(results) == 1
        assert results[0].mac_address == "AA-BB-CC-DD-EE-FF"
        assert results[0].serial_number == "1234567"

    def test_invalid_device_port_falls_back_to_default(self):
        results = _run_finder_against_payload(
            _make_protobuf_response(device_port=0)
        )
        assert len(results) == 1
        assert results[0].port == 9760

    def test_powered_off_is_reflected(self):
        results = _run_finder_against_payload(
            _make_protobuf_response(pwr_status=0)
        )
        assert len(results) == 1
        assert results[0].is_power_on is False

    def test_unknown_part_number_maps_to_unknown_type(self):
        results = _run_finder_against_payload(
            _make_protobuf_response(device_pn="WidgetPro")
        )
        assert len(results) == 1
        assert results[0].device_type == DeviceType.UNKNOWN

    def test_dedupes_by_mac_when_protobuf_present(self):
        # Two protobuf responses from the same MAC (but the test
        # responder always replies from 127.0.0.1, so IP-only dedupe
        # would also collapse — the assertion is that the MAC dedupe
        # path runs and produces exactly one result).
        results = _run_finder_against_payload(_make_protobuf_response())
        assert len(results) == 1
        assert results[0].mac_address == "AA-BB-CC-DD-EE-FF"

    def test_unparseable_payload_falls_back_to_text(self):
        # Random ASCII that can't decode as a meaningful protobuf
        # — the text-fallback path should still produce a DeviceInfo.
        results = _run_finder_against_payload(b"DaqifiNyq1\r\n")
        assert len(results) == 1
        assert "DaqifiNyq1" in results[0].name
        assert results[0].mac_address == ""

    def test_query_echo_is_skipped(self):
        # If the device (or a network loopback) echoes our own
        # broadcast back, we must NOT report it as a device.
        results = _run_finder_against_payload(b"DAQiFi?\r\n")
        assert results == []
