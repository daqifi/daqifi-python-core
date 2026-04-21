"""Network address formatting helpers.

Port of `Daqifi.Core.Device.Network.NetworkAddressHelper`. Decodes
the byte-string network fields (IP address, MAC, gateway, DNS,
subnet mask) carried in a `DaqifiOutMessage` protobuf into
human-readable strings.

The C# class takes a `DaqifiOutMessage` reference and reaches into
each named field. In Python we keep both shapes:

* Field-level helpers (`format_ipv4`, `format_mac`) take raw
  `bytes` — useful when the caller already has the field.
* Message-level helpers (`get_ip_address_string`, etc.) take any
  object exposing the standard protobuf field names. This works
  with both the generated protobuf class and any duck-typed test
  fake.
"""
from __future__ import annotations

from typing import Any


__all__ = [
    "format_ipv4",
    "format_mac",
    "get_ip_address_string",
    "get_mac_address_string",
    "get_subnet_mask_string",
    "get_gateway_string",
    "get_primary_dns_string",
    "get_secondary_dns_string",
]


_IPV4_BYTE_LENGTH = 4
_MAC_BYTE_LENGTH = 6


def format_ipv4(data: bytes) -> str:
    """Format a 4-byte sequence as a dotted-decimal IPv4 string.

    Returns an empty string if `data` is not exactly 4 bytes — matches
    the C# helper's behavior of treating malformed fields as absent
    rather than raising.
    """
    if data is None or len(data) != _IPV4_BYTE_LENGTH:
        return ""
    return ".".join(str(b) for b in data)


def format_mac(data: bytes) -> str:
    """Format a 6-byte sequence as a hyphen-separated upper-hex MAC string.

    e.g. `b'\\xaa\\xbb\\xcc\\xdd\\xee\\xff'` → `"AA-BB-CC-DD-EE-FF"`.
    Returns empty string if `data` is not exactly 6 bytes.
    """
    if data is None or len(data) != _MAC_BYTE_LENGTH:
        return ""
    return "-".join(f"{b:02X}" for b in data)


def _require_message(message: Any) -> None:
    if message is None:
        raise TypeError("message must not be None")


def get_ip_address_string(message: Any) -> str:
    """Return `message.ip_addr` formatted as dotted-decimal IPv4."""
    _require_message(message)
    return format_ipv4(message.ip_addr)


def get_mac_address_string(message: Any) -> str:
    """Return `message.mac_addr` formatted as a hyphenated MAC string."""
    _require_message(message)
    return format_mac(message.mac_addr)


def get_subnet_mask_string(message: Any) -> str:
    """Return `message.net_mask` formatted as dotted-decimal IPv4."""
    _require_message(message)
    return format_ipv4(message.net_mask)


def get_gateway_string(message: Any) -> str:
    """Return `message.gateway` formatted as dotted-decimal IPv4."""
    _require_message(message)
    return format_ipv4(message.gateway)


def get_primary_dns_string(message: Any) -> str:
    """Return `message.primary_dns` formatted as dotted-decimal IPv4."""
    _require_message(message)
    return format_ipv4(message.primary_dns)


def get_secondary_dns_string(message: Any) -> str:
    """Return `message.secondary_dns` formatted as dotted-decimal IPv4."""
    _require_message(message)
    return format_ipv4(message.secondary_dns)
