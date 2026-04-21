"""Tests for `daqifi.network` (port of C# NetworkAddressHelper)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from daqifi.network import (
    format_ipv4,
    format_mac,
    get_gateway_string,
    get_ip_address_string,
    get_mac_address_string,
    get_primary_dns_string,
    get_secondary_dns_string,
    get_subnet_mask_string,
)


# Duck-typed stand-in for DaqifiOutMessage. The real protobuf class
# exposes the same attribute names with `bytes` values.
@dataclass
class FakeMessage:
    ip_addr: bytes = b""
    mac_addr: bytes = b""
    net_mask: bytes = b""
    gateway: bytes = b""
    primary_dns: bytes = b""
    secondary_dns: bytes = b""


# -- format_ipv4 ------------------------------------------------------------

class TestFormatIpv4:
    def test_valid_four_bytes(self):
        assert format_ipv4(b"\xc0\xa8\x01\x64") == "192.168.1.100"

    def test_zero_address(self):
        assert format_ipv4(b"\x00\x00\x00\x00") == "0.0.0.0"

    def test_broadcast_address(self):
        assert format_ipv4(b"\xff\xff\xff\xff") == "255.255.255.255"

    def test_empty_returns_empty_string(self):
        assert format_ipv4(b"") == ""

    def test_too_short_returns_empty_string(self):
        assert format_ipv4(b"\x01\x02\x03") == ""

    def test_too_long_returns_empty_string(self):
        assert format_ipv4(b"\x01\x02\x03\x04\x05") == ""

    def test_none_returns_empty_string(self):
        assert format_ipv4(None) == ""  # type: ignore[arg-type]


# -- format_mac -------------------------------------------------------------

class TestFormatMac:
    def test_valid_six_bytes(self):
        assert format_mac(b"\xaa\xbb\xcc\xdd\xee\xff") == "AA-BB-CC-DD-EE-FF"

    def test_zero_mac(self):
        assert format_mac(b"\x00\x00\x00\x00\x00\x00") == "00-00-00-00-00-00"

    def test_lowercase_input_yields_uppercase_hex(self):
        # Bytes themselves don't have case, but verify hex formatting is upper.
        result = format_mac(b"\x0a\x0b\x0c\x0d\x0e\x0f")
        assert result == "0A-0B-0C-0D-0E-0F"
        assert result == result.upper()

    def test_empty_returns_empty_string(self):
        assert format_mac(b"") == ""

    def test_too_short_returns_empty_string(self):
        assert format_mac(b"\x01\x02\x03\x04\x05") == ""

    def test_too_long_returns_empty_string(self):
        assert format_mac(b"\x01\x02\x03\x04\x05\x06\x07") == ""

    def test_none_returns_empty_string(self):
        assert format_mac(None) == ""  # type: ignore[arg-type]


# -- get_*_string --------------------------------------------------------

class TestMessageHelpers:
    def test_get_ip_address_string(self):
        msg = FakeMessage(ip_addr=b"\x0a\x00\x00\x01")
        assert get_ip_address_string(msg) == "10.0.0.1"

    def test_get_ip_address_string_empty_field(self):
        msg = FakeMessage(ip_addr=b"")
        assert get_ip_address_string(msg) == ""

    def test_get_mac_address_string(self):
        msg = FakeMessage(mac_addr=b"\x12\x34\x56\x78\x9a\xbc")
        assert get_mac_address_string(msg) == "12-34-56-78-9A-BC"

    def test_get_mac_address_string_empty_field(self):
        msg = FakeMessage(mac_addr=b"")
        assert get_mac_address_string(msg) == ""

    def test_get_subnet_mask_string(self):
        msg = FakeMessage(net_mask=b"\xff\xff\xff\x00")
        assert get_subnet_mask_string(msg) == "255.255.255.0"

    def test_get_gateway_string(self):
        msg = FakeMessage(gateway=b"\xc0\xa8\x01\x01")
        assert get_gateway_string(msg) == "192.168.1.1"

    def test_get_primary_dns_string(self):
        msg = FakeMessage(primary_dns=b"\x08\x08\x08\x08")
        assert get_primary_dns_string(msg) == "8.8.8.8"

    def test_get_secondary_dns_string(self):
        msg = FakeMessage(secondary_dns=b"\x01\x01\x01\x01")
        assert get_secondary_dns_string(msg) == "1.1.1.1"

    def test_none_message_raises(self):
        for fn in (
            get_ip_address_string,
            get_mac_address_string,
            get_subnet_mask_string,
            get_gateway_string,
            get_primary_dns_string,
            get_secondary_dns_string,
        ):
            with pytest.raises(TypeError):
                fn(None)
