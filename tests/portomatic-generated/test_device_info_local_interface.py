"""Tests for DeviceInfo.local_interface_address (closes #7).

Adds the C# ``IDeviceInfo.LocalInterfaceAddress`` surface to the
Python ``DeviceInfo`` dataclass so callers in multi-homed setups
can route follow-up TCP connections back through the same NIC the
discovery broadcast came in on.

The current Python WiFi finder uses a single-socket UDP broadcast
model and doesn't yet auto-populate this field, so coverage here is
focused on the dataclass contract — the field accepts strings, has
the right default, and serializes alongside the other identifying
fields.

Identifiers are deliberately synthetic — IPs come from the RFC
5737 documentation ranges (``192.0.2.0/24``, ``198.51.100.0/24``)
and port paths are generic placeholders. No workstation-specific
values, per CONTRIBUTING.md "Pull requests".
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from daqifi.device_finders import (
    ConnectionType,
    DeviceInfo,
    DeviceType,
)


pytestmark = pytest.mark.portomatic_generated


class TestDeviceInfoLocalInterfaceAddress:
    def test_default_is_empty_string(self):
        """Field defaults to ``""`` so existing call sites that
        construct DeviceInfo without this field don't need updating."""
        info = DeviceInfo(name="x")
        assert info.local_interface_address == ""

    def test_accepts_dotted_decimal_ipv4(self):
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            local_interface_address="192.0.2.42",
        )
        assert info.local_interface_address == "192.0.2.42"

    def test_dataclass_is_still_frozen(self):
        """Adding the field must not break frozen-dataclass semantics —
        DeviceInfo is constructed once during discovery and treated as
        an immutable value object. ``FrozenInstanceError`` is the
        precise exception dataclasses raises on mutation."""
        info = DeviceInfo(name="x")
        with pytest.raises(FrozenInstanceError):
            info.local_interface_address = "192.0.2.1"  # type: ignore[misc]

    def test_field_independent_of_ip_address(self):
        """``ip_address`` is the device's IP; ``local_interface_address``
        is the workstation NIC IP that received the broadcast. They
        should be settable independently and not coupled."""
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.WIFI,
            ip_address="198.51.100.5",
            local_interface_address="192.0.2.42",
        )
        assert info.ip_address == "198.51.100.5"
        assert info.local_interface_address == "192.0.2.42"

    def test_does_not_affect_serial_devices(self):
        """The field is meaningful only for WiFi finders; a serial-
        connected DeviceInfo should be constructable without it and
        leave it at the empty default."""
        info = DeviceInfo(
            name="x",
            connection_type=ConnectionType.SERIAL,
            port_name="<placeholder-serial-port>",
        )
        assert info.local_interface_address == ""
        assert info.connection_type == ConnectionType.SERIAL


class TestTcpStreamTransportHostname:
    """Verifies the new public ``hostname`` accessor on
    TcpStreamTransport (mirrors C# ``TcpStreamTransport.Hostname``)."""

    def test_hostname_returns_constructor_value(self):
        from daqifi.transport import TcpStreamTransport

        t = TcpStreamTransport(host="device.example.test", port=9760)
        assert t.hostname == "device.example.test"

    def test_hostname_works_with_ip_string(self):
        from daqifi.transport import TcpStreamTransport

        t = TcpStreamTransport(host="192.0.2.10", port=9760)
        assert t.hostname == "192.0.2.10"

    def test_hostname_is_read_only(self):
        from daqifi.transport import TcpStreamTransport

        t = TcpStreamTransport(host="device.example.test", port=9760)
        with pytest.raises(AttributeError):
            t.hostname = "other.example.test"  # type: ignore[misc]
