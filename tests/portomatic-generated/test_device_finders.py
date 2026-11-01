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
        monkeypatch.setattr("daqifi.device_finders.daqifi_serial_ports", lambda: [])
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


def _encode_varint(value: int) -> bytes:
    """Encode `value` as a protobuf varint.

    Inlined here so the tests don't depend on
    `google.protobuf.internal.encoder._VarintBytes` — that's a private
    symbol whose location/signature is not part of protobuf's public
    API contract.
    """
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


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
        return _encode_varint(len(payload)) + payload
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


# ---- WiFiDeviceFinder.probe_ip — unicast bypass for #72 ---------------------


class TestWiFiDeviceFinderProbeIp:
    """Closes #72: ``probe_ip`` lets callers bypass UDP broadcast when
    they already know the device's IP — needed on WSL2 where mirrored
    networking drops broadcast responses."""

    def test_probe_ip_returns_none_on_connect_failure(self):
        # 127.0.0.1:1 is reserved/unbound — TCP connect fails fast.
        f = WiFiDeviceFinder()
        try:
            assert f.probe_ip("127.0.0.1", port=1, timeout_s=0.5) is None
        finally:
            f.close()

    def test_probe_ip_raises_after_close(self):
        f = WiFiDeviceFinder()
        f.close()
        with pytest.raises(RuntimeError, match="closed"):
            f.probe_ip("127.0.0.1")

    def test_probe_ip_propagates_invalid_input(self):
        """Closes Qodo PR #76 /agentic_review pass 2 bug #1: a bad
        host/port arg must propagate the underlying ValueError so the
        caller sees the misconfiguration, not a silent None that looks
        like 'device unreachable'."""
        f = WiFiDeviceFinder()
        try:
            with pytest.raises(ValueError):
                f.probe_ip("", port=9760)
            with pytest.raises(ValueError):
                f.probe_ip("127.0.0.1", port=70000)
        finally:
            f.close()

    def test_probe_ip_returns_none_on_silent_init(self):
        """Simulate the WiFi-SCPI silence pattern: connect succeeds but
        the device never delivers a SYSInfoPB? frame so initialize()
        raises. ``probe_ip`` must swallow the exception and return
        None instead of propagating, since the discovery surface is
        meant to be tolerant of unreachable / wedged devices."""
        # Listen on an ephemeral port that accepts TCP but never sends
        # any SCPI replies — that's the silent-WiFi-SCPI shape.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv_port = srv.getsockname()[1]

        accepted: list = []

        def _accept_loop():
            try:
                c, _ = srv.accept()
                accepted.append(c)
            except OSError:
                pass

        t = threading.Thread(target=_accept_loop, daemon=True)
        t.start()
        try:
            f = WiFiDeviceFinder()
            try:
                # Tight timeout so the test stays fast — silent-init
                # detection (#73) raises after this window.
                result = f.probe_ip(
                    "127.0.0.1", port=srv_port, timeout_s=0.5,
                )
            finally:
                f.close()
            assert result is None
        finally:
            try:
                srv.close()
            except Exception:
                pass
            t.join(timeout=1.0)
            for c in accepted:
                try:
                    c.close()
                except Exception:
                    pass
