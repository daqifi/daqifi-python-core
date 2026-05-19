"""Tests for daqifi._internal.nic — NIC enumeration + filtering.

Mirrors the upstream WiFiDeviceFinder NIC-filtering shape from
daqifi-core PR #179/#180. Real ifaddr.get_adapters() is mocked so
the tests run hermetically on any host.
"""
from unittest.mock import patch

import pytest

from daqifi._internal.nic import (
    NicInfo,
    _is_skippable_ip,
    _is_virtual,
    enumerate_real_nics,
)


class _FakeIP:
    def __init__(self, ip, network_prefix):
        self.ip = ip
        self.network_prefix = network_prefix


class _FakeAdapter:
    def __init__(self, name, nice_name, ips):
        self.name = name
        self.nice_name = nice_name
        self.ips = ips


# ---------------------------------------------------------------------------
# NicInfo.broadcast
# ---------------------------------------------------------------------------


class TestBroadcastComputation:
    @pytest.mark.parametrize("ip,prefix,expected", [
        ("192.168.1.42", 24, "192.168.1.255"),
        ("10.0.0.5", 8, "10.255.255.255"),
        ("172.16.0.10", 16, "172.16.255.255"),
        ("192.168.1.42", 32, "192.168.1.42"),  # /32 → host IS broadcast
    ])
    def test_subnet_broadcast(self, ip, prefix, expected):
        assert NicInfo(name="x", ip=ip, network_prefix=prefix).broadcast == expected

    def test_unparseable_falls_back_to_global_broadcast(self):
        # NicInfo accepts any int; an out-of-range prefix should fall
        # back to 255.255.255.255 rather than crash.
        nic = NicInfo(name="x", ip="192.168.1.42", network_prefix=99)
        assert nic.broadcast == "255.255.255.255"


# ---------------------------------------------------------------------------
# Skip filters
# ---------------------------------------------------------------------------


class TestSkippableIp:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "127.5.5.5",         # loopback
        "169.254.1.1", "169.254.99.99",   # APIPA link-local
    ])
    def test_skipped(self, ip):
        assert _is_skippable_ip(ip) is True

    @pytest.mark.parametrize("ip", [
        "192.168.1.42", "10.0.0.1", "172.16.5.5", "8.8.8.8",
    ])
    def test_not_skipped(self, ip):
        assert _is_skippable_ip(ip) is False

    def test_unparseable_skipped(self):
        assert _is_skippable_ip("not-an-ip") is True


class TestIsVirtual:
    @pytest.mark.parametrize("name,nice_name", [
        ("lo", "Loopback Pseudo-Interface"),
        ("lo0", "lo0"),
        ("eth5", "vEthernet (WSL)"),
        ("eth0", "VMware Network Adapter"),
        ("eth1", "Hyper-V Virtual Ethernet"),
        ("tap0", "tap0"),
        ("tun0", "tun0"),
        ("docker0", "docker0"),
        ("utun3", "Tailscale Tunnel"),
        ("eth9", "ZeroTier One [abc]"),
    ])
    def test_virtual_names_filtered(self, name, nice_name):
        assert _is_virtual(name, nice_name) is True

    @pytest.mark.parametrize("name,nice_name", [
        ("eth0", "Realtek PCIe GbE"),
        ("wlan0", "Intel Wi-Fi 6 AX200"),
        ("en0", "en0"),
        ("eth2", "eth2"),
    ])
    def test_real_lan_names_kept(self, name, nice_name):
        assert _is_virtual(name, nice_name) is False


# ---------------------------------------------------------------------------
# enumerate_real_nics — end-to-end filtering against mocked ifaddr.
# ---------------------------------------------------------------------------


class TestEnumerateRealNics:
    def _patch_adapters(self, adapters):
        return patch(
            "ifaddr.get_adapters",
            return_value=adapters,
        )

    def test_filters_loopback_adapter(self):
        adapters = [
            _FakeAdapter("lo", "lo", [_FakeIP("127.0.0.1", 8)]),
            _FakeAdapter("eth0", "eth0", [_FakeIP("192.168.1.42", 24)]),
        ]
        with self._patch_adapters(adapters):
            nics = enumerate_real_nics()
        assert [n.ip for n in nics] == ["192.168.1.42"]

    def test_filters_virtual_adapter_by_name(self):
        adapters = [
            _FakeAdapter("eth0", "VMware Network Adapter VMnet1",
                         [_FakeIP("192.168.99.1", 24)]),
            _FakeAdapter("eth1", "Realtek Family Controller",
                         [_FakeIP("192.168.1.42", 24)]),
        ]
        with self._patch_adapters(adapters):
            nics = enumerate_real_nics()
        assert [n.ip for n in nics] == ["192.168.1.42"]

    def test_filters_apipa_link_local_ip(self):
        adapters = [
            _FakeAdapter("eth0", "eth0", [_FakeIP("169.254.5.5", 16)]),
            _FakeAdapter("eth1", "eth1", [_FakeIP("192.168.1.42", 24)]),
        ]
        with self._patch_adapters(adapters):
            nics = enumerate_real_nics()
        assert [n.ip for n in nics] == ["192.168.1.42"]

    def test_skips_ipv6_addresses(self):
        # ifaddr packs IPv6 as a tuple; only IPv4 strings are kept.
        adapters = [
            _FakeAdapter(
                "eth0", "eth0",
                [
                    _FakeIP(("fe80::1234", 0, 0), 64),  # IPv6 — skipped
                    _FakeIP("192.168.1.42", 24),
                ],
            ),
        ]
        with self._patch_adapters(adapters):
            nics = enumerate_real_nics()
        assert [n.ip for n in nics] == ["192.168.1.42"]

    def test_multi_ip_adapter_yields_one_per_ip(self):
        adapters = [
            _FakeAdapter("eth0", "eth0", [
                _FakeIP("192.168.1.42", 24),
                _FakeIP("192.168.2.42", 24),
            ]),
        ]
        with self._patch_adapters(adapters):
            nics = enumerate_real_nics()
        assert sorted(n.ip for n in nics) == ["192.168.1.42", "192.168.2.42"]

    def test_returns_empty_when_ifaddr_unavailable(self):
        # Simulate ifaddr ImportError — caller falls back to single-
        # socket discovery in this case.
        with patch.dict("sys.modules", {"ifaddr": None}):
            nics = enumerate_real_nics()
        assert nics == []

    @pytest.mark.parametrize("bad_prefix", ["abc", None, object()])
    def test_unparseable_prefix_skipped(self, bad_prefix):
        """Future ifaddr versions or weird platforms could surface a
        non-int network_prefix — skip the adapter rather than crash
        downstream when broadcast computation tries to format it."""
        adapters = [
            _FakeAdapter("eth0", "eth0", [_FakeIP("192.168.1.42", bad_prefix)]),
            _FakeAdapter("eth1", "eth1", [_FakeIP("10.0.0.5", 24)]),
        ]
        with self._patch_adapters(adapters):
            nics = enumerate_real_nics()
        assert [n.ip for n in nics] == ["10.0.0.5"]

    @pytest.mark.parametrize("bad_prefix", [-1, 33, 99])
    def test_out_of_range_prefix_skipped(self, bad_prefix):
        """Prefixes outside the IPv4 0-32 range are bogus — skip."""
        adapters = [
            _FakeAdapter("eth0", "eth0", [_FakeIP("192.168.1.42", bad_prefix)]),
        ]
        with self._patch_adapters(adapters):
            nics = enumerate_real_nics()
        assert nics == []
