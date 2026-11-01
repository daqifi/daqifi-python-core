"""Enum types ported from daqifi-core.

Bundled into one module since they're small, related to one another
through the device-protocol layer, and have no behavior beyond
identity. Each maps a C# enum 1:1 with `IntEnum` so callers can
both compare numerically and serialize to the wire-protocol
integers.
"""
from __future__ import annotations

from enum import IntEnum


__all__ = [
    "WifiMode",
    "WifiSecurityType",
    "FirmwareUpdateState",
    "ProtobufMessageType",
    "SdCardLogFormat",
]


class WifiMode(IntEnum):
    """WiFi operating mode for the device."""

    EXISTING_NETWORK = 1  # Device joins an external AP as a client.
    SELF_HOSTED = 4       # Device is its own AP.


class WifiSecurityType(IntEnum):
    """WiFi security type for network connections."""

    NONE = 0           # Open network, no security.
    WPA_PSK_PHRASE = 3  # WPA/WPA2 Personal with pre-shared key.


class FirmwareUpdateState(IntEnum):
    """Firmware update lifecycle states emitted by the update service."""

    IDLE = 0
    PREPARING_DEVICE = 1
    WAITING_FOR_BOOTLOADER = 2
    CONNECTING = 3
    ERASING_FLASH = 4
    PROGRAMMING = 5
    VERIFYING = 6
    JUMPING_TO_APP = 7
    COMPLETE = 8
    FAILED = 9
    # A PIC32 update that failed while erasing, programming or verifying
    # re-erases the flash (CLEANING_UP), so the board isn't left with a
    # half-written application. RECOVERED: the re-erase worked.
    CLEANING_UP = 10
    RECOVERED = 11


class ProtobufMessageType(IntEnum):
    """Type tag for protobuf messages received from a DAQiFi device."""

    UNKNOWN = 0
    STATUS = 1
    STREAM = 2
    SD_CARD = 3
    ERROR = 4


class SdCardLogFormat(IntEnum):
    """SD-card logging format. Values match `SYSTem:STReam:FORmat`."""

    PROTOBUF = 0  # Binary protobuf (.bin), default — most compact.
    JSON = 1      # JSON text (.json), human-readable.
    CSV = 2       # CSV text (.csv), spreadsheet-friendly.
