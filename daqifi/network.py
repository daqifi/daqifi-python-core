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

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from daqifi.enums import WifiMode, WifiSecurityType


__all__ = [
    "format_ipv4",
    "format_mac",
    "get_ip_address_string",
    "get_mac_address_string",
    "get_subnet_mask_string",
    "get_gateway_string",
    "get_primary_dns_string",
    "get_secondary_dns_string",
    "NetworkConfiguration",
    "NetworkConfigurable",
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


@dataclass
class NetworkConfiguration:
    """Network configuration settings for a DAQiFi device.

    Ports the C# ``NetworkConfiguration`` class from
    ``Daqifi.Core/Device/Network/NetworkConfiguration.cs``. Mutable —
    the upstream type exposes ``get; set;`` properties so callers can
    build the configuration up stepwise before calling
    ``UpdateNetworkConfigurationAsync``.

    Defaults match the upstream parameterless constructor:
    ``SelfHosted`` mode + ``WpaPskPhrase`` security + empty SSID +
    empty password.
    """

    mode: WifiMode = WifiMode.SELF_HOSTED
    security_type: WifiSecurityType = WifiSecurityType.WPA_PSK_PHRASE
    ssid: str = ""
    password: str = ""

    def clone(self) -> "NetworkConfiguration":
        """Return a new ``NetworkConfiguration`` with the same field values.

        Mirrors the C# ``Clone()`` method — a shallow copy that lets
        callers stage edits without mutating the live device config.
        """
        return NetworkConfiguration(
            mode=self.mode,
            security_type=self.security_type,
            ssid=self.ssid,
            password=self.password,
        )


@runtime_checkable
class NetworkConfigurable(Protocol):
    """Protocol for devices that support network configuration.

    Pythonic name for the C# ``INetworkConfigurable`` interface from
    ``Daqifi.Core/Device/Network/INetworkConfigurable.cs`` (the ``I``
    prefix is dropped to match the rest of this codebase's protocols).

    The C# method ``UpdateNetworkConfigurationAsync`` returns ``Task``;
    the Python port collapses async to sync to match the rest of the
    facade surface (``set_wifi_*``, ``apply_network_configuration``,
    etc. are all sync). Callers that want async dispatch can wrap with
    ``asyncio.to_thread``.
    """

    @property
    def network_configuration(self) -> NetworkConfiguration:
        """Most recently applied network configuration on the device."""
        ...

    def update_network_configuration(
        self,
        configuration: NetworkConfiguration,
    ) -> None:
        """Apply ``configuration`` to the device.

        Stops any active streaming, sends the WiFi mode + SSID + security
        type + password commands, applies the LAN settings, waits for
        the WiFi module to restart, re-enables LAN, then persists the
        configuration so it survives a reboot.

        Note: SD card and LAN share the same SPI bus on the hardware —
        the implementation handles the interface switching automatically.
        """
        ...

    def prepare_sd_interface(self) -> None:
        """Disable LAN so the SD card can use the shared SPI bus."""
        ...

    def prepare_lan_interface(self) -> None:
        """Disable the SD card so LAN can use the shared SPI bus."""
        ...
