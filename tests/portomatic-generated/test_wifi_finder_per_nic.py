"""Tests for #95 — WiFiDeviceFinder multi-NIC UDP discovery.

Mirrors daqifi-core PR #179/#180. Verifies that ``discover()`` with
no injected transport enumerates real NICs and opens one transport
per NIC, broadcasting on each NIC's directed broadcast address
instead of the global ``255.255.255.255``.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from daqifi._internal.nic import NicInfo
from daqifi.device_finders import WiFiDeviceFinder


def _make_fake_transport():
    """Stub UdpTransport that records send_broadcast_to calls and
    returns no datagrams from receive (so the receive loop just
    times out)."""
    t = MagicMock()
    t.is_open = False

    def _open():
        t.is_open = True
    t.open.side_effect = _open

    def _close():
        t.is_open = False
    t.close.side_effect = _close

    # receive() returns None each call → loop drains until deadline
    t.receive.return_value = None
    return t


class TestPerNicDiscover:
    def test_one_transport_per_real_nic(self):
        """Two real NICs → two UdpTransport instances opened, each
        bound to its NIC's local IP."""
        fake_nics = [
            NicInfo(name="eth0", ip="192.168.1.42", network_prefix=24),
            NicInfo(name="wlan0", ip="10.0.0.5", network_prefix=24),
        ]
        opened: list = []
        constructor_calls: list = []

        def _UdpTransport(local_port=0, local_ip=None):
            constructor_calls.append((local_port, local_ip))
            t = _make_fake_transport()
            opened.append(t)
            return t

        with patch(
            "daqifi.device_finders.UdpTransport", side_effect=_UdpTransport
        ), patch(
            "daqifi._internal.nic.enumerate_real_nics",
            return_value=fake_nics,
        ):
            finder = WiFiDeviceFinder(discovery_port=30303)
            finder.discover(timeout=timedelta(milliseconds=200))

        # One transport per NIC — each constructed with the NIC's IP.
        assert len(constructor_calls) == 2
        assert sorted(call[1] for call in constructor_calls) == [
            "10.0.0.5", "192.168.1.42"
        ]
        for t in opened:
            t.open.assert_called_once()
            t.close.assert_called_once()

    def test_per_nic_directed_broadcast_address(self):
        """Each per-NIC transport broadcasts to that NIC's directed
        broadcast address (e.g. 192.168.1.255), NOT the global
        255.255.255.255 — that's the whole point of the multi-NIC
        port (#95 / daqifi-core #179/#180)."""
        fake_nics = [
            NicInfo(name="eth0", ip="192.168.1.42", network_prefix=24),
        ]
        sent_targets: list = []

        def _UdpTransport(local_port=0, local_ip=None):
            t = _make_fake_transport()
            t.send_broadcast_to.side_effect = (
                lambda data, end_point: sent_targets.append(end_point)
            )
            return t

        with patch(
            "daqifi.device_finders.UdpTransport", side_effect=_UdpTransport
        ), patch(
            "daqifi._internal.nic.enumerate_real_nics",
            return_value=fake_nics,
        ):
            finder = WiFiDeviceFinder(discovery_port=30303)
            finder.discover(timeout=timedelta(milliseconds=200))

        # Two queries (DAQIFI_FINDER_QUERY + NATIVE_FINDER_QUERY)
        # both targeted at the NIC's directed broadcast 192.168.1.255.
        assert sent_targets == [
            ("192.168.1.255", 30303),
            ("192.168.1.255", 30303),
        ]

    def test_no_real_nics_falls_back_to_single_socket(self):
        """When ifaddr returns nothing (uninstalled, or all adapters
        filtered out), discover() must still try a single wildcard
        broadcast — graceful degradation."""
        opened_any: list = []

        def _UdpTransport(local_port=0, local_ip=None):
            t = _make_fake_transport()
            opened_any.append((local_port, local_ip))
            return t

        with patch(
            "daqifi.device_finders.UdpTransport", side_effect=_UdpTransport
        ), patch(
            "daqifi._internal.nic.enumerate_real_nics", return_value=[]
        ):
            finder = WiFiDeviceFinder(discovery_port=30303)
            finder.discover(timeout=timedelta(milliseconds=100))

        # One single-socket transport with no local_ip pinning.
        assert len(opened_any) == 1
        assert opened_any[0] == (0, None)

    def test_one_nic_failure_does_not_kill_others(self):
        """If one NIC's transport.open() raises, the per-NIC worker
        for that NIC silently aborts — but the worker for the other
        NIC keeps running and discovery completes."""
        fake_nics = [
            NicInfo(name="eth0", ip="192.168.1.42", network_prefix=24),
            NicInfo(name="wlan0", ip="10.0.0.5", network_prefix=24),
        ]
        good_open_count = [0]

        def _UdpTransport(local_port=0, local_ip=None):
            t = _make_fake_transport()
            if local_ip == "192.168.1.42":
                # Simulate a NIC that vanished between enumeration
                # and open() — bind fails.
                t.open.side_effect = OSError("simulated bind failure")
            else:
                def _ok_open():
                    t.is_open = True
                    good_open_count[0] += 1
                t.open.side_effect = _ok_open
            return t

        with patch(
            "daqifi.device_finders.UdpTransport", side_effect=_UdpTransport
        ), patch(
            "daqifi._internal.nic.enumerate_real_nics",
            return_value=fake_nics,
        ):
            finder = WiFiDeviceFinder(discovery_port=30303)
            # Must not raise even though one NIC's open() fails.
            results = finder.discover(timeout=timedelta(milliseconds=200))
            assert results == []  # no devices, but discovery completed

        assert good_open_count[0] == 1, (
            "the surviving NIC should have opened its transport"
        )

    def test_caller_provided_transport_skips_per_nic_path(self):
        """When a caller passes udp_transport, the legacy single-
        socket path is preserved. ifaddr enumeration should not be
        called — important because tests inject mock transports
        that don't bind to real NICs."""
        fake_transport = _make_fake_transport()
        fake_transport.is_open = True

        with patch(
            "daqifi._internal.nic.enumerate_real_nics"
        ) as mock_enum:
            finder = WiFiDeviceFinder(discovery_port=30303)
            finder.discover(
                timeout=timedelta(milliseconds=100),
                udp_transport=fake_transport,
            )

        mock_enum.assert_not_called()
        # Legacy path uses send_broadcast (not send_broadcast_to).
        fake_transport.send_broadcast.assert_called()
