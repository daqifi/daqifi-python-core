"""Network-interface enumeration for multi-NIC UDP discovery.

WiFiDeviceFinder needs to broadcast on every real LAN interface so
discovery works on hosts with multiple NICs (Wi-Fi + Ethernet, hosts
with VPN/virtual adapters that mask the real LAN). Python's stdlib
doesn't ship a clean cross-platform NIC enumerator, so we wrap
``ifaddr``.

Ports the upstream filtering shape from
``Daqifi.Core.Device.Discovery.WiFiDeviceFinder`` (daqifi-core
PR #179/#180): skip loopback, link-local APIPA, and a static set of
well-known virtual-adapter name patterns.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import List


# Substrings that mark an interface as virtual / non-LAN. Lower-case
# match against both the OS-level name and the human-readable
# "nice name". Mirrors the upstream filter set + the names commonly
# seen on Windows/WSL/macOS hosts that exercise this code path.
_VIRTUAL_NAME_HINTS: tuple = (
    "vmware", "virtualbox", "vbox",
    "hyper-v", "hyperv", "vethernet",
    "wsl", "tap", "tun", "tunnel",
    "loopback", "pseudo",
    "vpn", "tailscale", "wireguard", "zerotier",
    "docker", "containers",
    "bluetooth", "isatap", "teredo",
)


@dataclass(frozen=True)
class NicInfo:
    """One enumerated network interface usable for UDP broadcast."""

    name: str
    ip: str
    network_prefix: int

    @property
    def broadcast(self) -> str:
        """IPv4 broadcast address for this NIC's subnet, e.g.
        ``192.168.1.255`` for ``192.168.1.42/24``. Falls back to
        ``255.255.255.255`` if the prefix is somehow unparseable
        (defensive — shouldn't happen with ifaddr's int prefix)."""
        try:
            net = ipaddress.IPv4Network(
                f"{self.ip}/{self.network_prefix}", strict=False
            )
            return str(net.broadcast_address)
        except (ValueError, ipaddress.AddressValueError):
            return "255.255.255.255"


# Exact-match adapter names that are always non-LAN. Substring
# matches in _VIRTUAL_NAME_HINTS are intentionally lax (catch family
# variants like ``vEthernet (WSL)``); these are the precise loopback
# names that "loopback"-as-substring would miss because the OS-level
# name is just "lo" / "lo0".
_EXACT_NON_LAN_NAMES: frozenset = frozenset({"lo", "lo0"})


def _is_virtual(name: str, nice_name: str) -> bool:
    if name.lower() in _EXACT_NON_LAN_NAMES:
        return True
    haystack = f"{name} {nice_name}".lower()
    return any(hint in haystack for hint in _VIRTUAL_NAME_HINTS)


def _is_skippable_ip(ip: str) -> bool:
    """Filter out IPs that aren't usable for LAN discovery — loopback
    (127.0.0.0/8) and APIPA link-local (169.254.0.0/16). Both are
    valid IPv4 addresses but neither reaches devices on the LAN."""
    try:
        addr = ipaddress.IPv4Address(ip)
    except (ValueError, ipaddress.AddressValueError):
        return True
    return addr.is_loopback or addr.is_link_local


def enumerate_real_nics() -> List[NicInfo]:
    """Return one :class:`NicInfo` per real IPv4 address that's
    eligible for UDP broadcast.

    Filters: skip loopback / APIPA addresses; skip adapters whose
    name (or nice name) matches a known virtual-adapter substring
    pattern. Adapters with multiple usable IPv4 addresses contribute
    one NicInfo per address.

    Returns an empty list if ``ifaddr`` is unavailable — caller can
    fall back to single-socket broadcast in that case.
    """
    try:
        import ifaddr  # type: ignore
    except ImportError:
        return []

    out: List[NicInfo] = []
    for adapter in ifaddr.get_adapters():
        if _is_virtual(adapter.name, adapter.nice_name):
            continue
        for ip_obj in adapter.ips:
            ip_value = ip_obj.ip
            # ifaddr packs IPv6 as a (addr, flowinfo, scope) tuple
            # and IPv4 as a plain string.
            if not isinstance(ip_value, str):
                continue
            if _is_skippable_ip(ip_value):
                continue
            # Defensively parse the prefix — ifaddr normally returns
            # an int, but a future version / unusual platform could
            # surface a string or out-of-range value. Skip the
            # adapter rather than carry an invalid NicInfo into the
            # broadcast computation.
            try:
                prefix = int(ip_obj.network_prefix)
            except (TypeError, ValueError):
                continue
            if prefix < 0 or prefix > 32:
                continue
            out.append(
                NicInfo(
                    name=adapter.name,
                    ip=ip_value,
                    network_prefix=prefix,
                )
            )
    return out
