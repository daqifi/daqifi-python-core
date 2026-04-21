"""PIC32 bootloader protocol — message framing + concrete protocol implementation.

Port of `Daqifi.Core.Firmware.IBootloaderProtocol`,
`Pic32BootloaderProtocol`, `Pic32BootloaderMessageProducer`, and
`Pic32BootloaderMessageConsumer`.

The C# split into Producer / Consumer / Protocol classes maps to:
- module-level `create_*_message` and `decode_*_response` functions
  (the static producer / consumer)
- `BootloaderProtocol` Protocol class — the abstract surface
- `Pic32BootloaderProtocol` concrete implementation — wires the
  module functions to the Protocol contract and bundles in an
  `IntelHexParser` for hex-file input.
"""
from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from daqifi.firmware.crc16 import Crc16
from daqifi.firmware.intel_hex import IntelHexParser


__all__ = [
    "BootloaderProtocol",
    "Pic32BootloaderProtocol",
    "create_request_version_message",
    "create_erase_flash_message",
    "create_program_flash_message",
    "create_jump_to_application_message",
    "decode_version_response",
    "decode_program_flash_response",
    "decode_erase_flash_response",
]


_START_OF_HEADER = 0x01
_END_OF_TRANSMISSION = 0x04
_DATA_LINK_ESCAPE = 0x10
_REQUEST_VERSION_COMMAND = 0x01
_ERASE_FLASH_COMMAND = 0x02
_PROGRAM_FLASH_COMMAND = 0x03
_JUMP_TO_APPLICATION_COMMAND = 0x05


# -- producer (port of static Pic32BootloaderMessageProducer) -----------

def create_request_version_message() -> bytes:
    """Frame a `request bootloader version` message."""
    return _construct_data_packet(bytes([_REQUEST_VERSION_COMMAND]))


def create_erase_flash_message() -> bytes:
    """Frame an `erase flash` message."""
    return _construct_data_packet(bytes([_ERASE_FLASH_COMMAND]))


def create_program_flash_message(hex_record: bytes) -> bytes:
    """Frame a `program flash` message carrying one hex record.

    Raises:
        TypeError: if `hex_record` is None.
    """
    if hex_record is None:
        raise TypeError("hex_record must not be None")
    command = bytes([_PROGRAM_FLASH_COMMAND]) + bytes(hex_record)
    return _construct_data_packet(command)


def create_jump_to_application_message() -> bytes:
    """Frame a `jump from bootloader to application` message."""
    return _construct_data_packet(bytes([_JUMP_TO_APPLICATION_COMMAND]))


def _construct_data_packet(command: bytes) -> bytes:
    crc = Crc16(command)
    command_and_crc = command + bytes([crc.low, crc.high])

    packet = bytearray()
    packet.append(_START_OF_HEADER)
    for item in command_and_crc:
        if item in (_START_OF_HEADER, _END_OF_TRANSMISSION, _DATA_LINK_ESCAPE):
            packet.append(_DATA_LINK_ESCAPE)
        packet.append(item)
    packet.append(_END_OF_TRANSMISSION)
    return bytes(packet)


# -- consumer (port of static Pic32BootloaderMessageConsumer) -----------

def decode_version_response(data: bytes) -> str:
    """Decode the bootloader version response.

    Returns a `"<major>.<minor>"` version string, or `"Error"` if the
    framing is invalid.
    """
    major_version = 0
    minor_version = 0

    if data is None or len(data) < 2:
        return "Error"
    if data[0] != _START_OF_HEADER:
        return "Error"

    # The command byte (0x01) matches SOH so it's DLE-escaped on the wire.
    # Minimum valid version response: SOH + DLE + cmd + major + minor (5 bytes).
    if (
        len(data) >= 5
        and data[1] == _DATA_LINK_ESCAPE
        and data[2] == _REQUEST_VERSION_COMMAND
    ):
        pointer = 3
        if pointer < len(data):
            if data[pointer] == _DATA_LINK_ESCAPE and pointer + 1 < len(data):
                pointer += 1
            major_version = data[pointer]
            pointer += 1

        if pointer < len(data):
            if data[pointer] == _DATA_LINK_ESCAPE and pointer + 1 < len(data):
                pointer += 1
            minor_version = data[pointer]

    return f"{major_version}.{minor_version}"


def decode_program_flash_response(data: bytes) -> bool:
    """True iff `data` is a valid program-flash acknowledgment."""
    if data is None or len(data) < 2:
        return False
    if data[0] != _START_OF_HEADER:
        return False
    return data[1] == _PROGRAM_FLASH_COMMAND


def decode_erase_flash_response(data: bytes) -> bool:
    """True iff `data` is a valid erase-flash acknowledgment."""
    if data is None or len(data) < 2:
        return False
    if data[0] != _START_OF_HEADER:
        return False
    return data[1] == _ERASE_FLASH_COMMAND


# -- protocol surface ----------------------------------------------------

@runtime_checkable
class BootloaderProtocol(Protocol):
    """Transport-agnostic PIC32 bootloader operations."""

    def create_request_version_message(self) -> bytes: ...
    def create_erase_flash_message(self) -> bytes: ...
    def create_program_flash_message(self, hex_record: bytes) -> bytes: ...
    def create_jump_to_application_message(self) -> bytes: ...
    def decode_version_response(self, data: bytes) -> str: ...
    def decode_program_flash_response(self, data: bytes) -> bool: ...
    def decode_erase_flash_response(self, data: bytes) -> bool: ...
    def parse_hex_file(self, hex_file_lines: List[str]) -> List[bytes]: ...


class Pic32BootloaderProtocol:
    """Concrete `BootloaderProtocol` for the PIC32 family.

    Delegates message framing to the module-level producer/consumer
    functions and bundles an `IntelHexParser` for hex-file input.
    """

    def __init__(
        self,
        begin_protected_address: Optional[int] = None,
        end_protected_address: Optional[int] = None,
    ) -> None:
        self._hex_parser = IntelHexParser(begin_protected_address, end_protected_address)

    def create_request_version_message(self) -> bytes:
        return create_request_version_message()

    def create_erase_flash_message(self) -> bytes:
        return create_erase_flash_message()

    def create_program_flash_message(self, hex_record: bytes) -> bytes:
        return create_program_flash_message(hex_record)

    def create_jump_to_application_message(self) -> bytes:
        return create_jump_to_application_message()

    def decode_version_response(self, data: bytes) -> str:
        return decode_version_response(data)

    def decode_program_flash_response(self, data: bytes) -> bool:
        return decode_program_flash_response(data)

    def decode_erase_flash_response(self, data: bytes) -> bool:
        return decode_erase_flash_response(data)

    def parse_hex_file(self, hex_file_lines: List[str]) -> List[bytes]:
        return self._hex_parser.parse_hex_records(hex_file_lines)
