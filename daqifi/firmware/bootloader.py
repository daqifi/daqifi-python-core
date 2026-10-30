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

from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable

from daqifi.firmware.crc16 import Crc16
from daqifi.firmware.intel_hex import HexFileRecord, IntelHexParser


__all__ = [
    "BootloaderProtocol",
    "FlashCrcRegion",
    "Pic32BootloaderProtocol",
    "create_request_version_message",
    "create_erase_flash_message",
    "create_program_flash_message",
    "create_jump_to_application_message",
    "create_read_crc_message",
    "decode_read_crc_response",
    "decode_version_response",
    "decode_program_flash_response",
    "decode_erase_flash_response",
    "split_oversized_records",
]


_START_OF_HEADER = 0x01
_END_OF_TRANSMISSION = 0x04
_DATA_LINK_ESCAPE = 0x10
_REQUEST_VERSION_COMMAND = 0x01
_ERASE_FLASH_COMMAND = 0x02
_PROGRAM_FLASH_COMMAND = 0x03
_READ_CRC_COMMAND = 0x04
_JUMP_TO_APPLICATION_COMMAND = 0x05

# One HID output report carries 64 bytes of frame (transport.py prepends the
# report-ID byte).
_HID_REPORT_PAYLOAD_LENGTH = 64
_SPLIT_RECORD_DATA_LENGTH = 16


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


def create_read_crc_message(address: int, length: int) -> bytes:
    """Frame a `read flash CRC` message for ``length`` bytes at ``address``.

    ``address`` is the KSEG0 address (physical | 0x80000000): the bootloader
    reads ``KVA0_TO_KVA1(Address)`` (datastream.c, READ_CRC). Both fields
    go little-endian, as the bootloader memcpys them.
    """
    command = (
        bytes([_READ_CRC_COMMAND])
        + int(address).to_bytes(4, "little")
        + int(length).to_bytes(4, "little")
    )
    return _construct_data_packet(command)


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

def _decode_frame(data: bytes) -> Optional[bytes]:
    """Unescape one bootloader response and check its CRC.

    The bootloader answers every command with
    ``SOH escaped(payload + crc_lo + crc_hi) EOT`` (bootloader.c, the
    SEND_RESPONSE state), the same framing ``_construct_data_packet`` sends.
    Returns the payload (command byte first, CRC removed), or None when the
    frame is truncated, unterminated or fails its CRC. Bytes after the EOT
    are HID report padding and are ignored.
    """
    if not data or data[0] != _START_OF_HEADER:
        return None
    unescaped = bytearray()
    index = 1
    while index < len(data):
        byte = data[index]
        if byte == _DATA_LINK_ESCAPE:
            index += 1
            if index >= len(data):
                return None
            unescaped.append(data[index])
        elif byte == _END_OF_TRANSMISSION:
            break
        elif byte == _START_OF_HEADER:
            return None
        else:
            unescaped.append(byte)
        index += 1
    else:
        return None  # no EOT
    if len(unescaped) < 3:
        return None
    payload, crc = bytes(unescaped[:-2]), unescaped[-2:]
    expected = Crc16(payload)
    if crc[0] != expected.low or crc[1] != expected.high:
        return None
    return payload


def decode_version_response(data: bytes) -> str:
    """Decode the bootloader version response.

    Returns a `"<major>.<minor>"` version string, or `"Error"` if the
    frame is malformed, fails its CRC, or answers a different command.
    """
    payload = _decode_frame(data)
    if payload is None or len(payload) < 3 or payload[0] != _REQUEST_VERSION_COMMAND:
        return "Error"
    return f"{payload[1]}.{payload[2]}"


def _is_ack_for(data: bytes, command: int) -> bool:
    payload = _decode_frame(data)
    return payload is not None and payload[0] == command


def decode_program_flash_response(data: bytes) -> bool:
    """True iff `data` is a valid program-flash acknowledgment."""
    return _is_ack_for(data, _PROGRAM_FLASH_COMMAND)


def decode_erase_flash_response(data: bytes) -> bool:
    """True iff `data` is a valid erase-flash acknowledgment."""
    return _is_ack_for(data, _ERASE_FLASH_COMMAND)


def decode_read_crc_response(data: bytes) -> Optional[int]:
    """The flash CRC-16 a `read flash CRC` response reports, or None when the
    frame is malformed, fails its CRC, answers a different command or isn't
    exactly the command byte and the CRC (datastream.c, READ_CRC)."""
    payload = _decode_frame(data)
    if payload is None or len(payload) != 3 or payload[0] != _READ_CRC_COMMAND:
        return None
    return payload[1] | (payload[2] << 8)


def split_oversized_records(
    hex_records: List[bytes], max_frame_length: int = _HID_REPORT_PAYLOAD_LENGTH
) -> List[bytes]:
    """Split data records whose program frame would not fit one HID report.

    DLE escaping can double a record: 32 bytes of 0x01 frame to 75 bytes,
    and the HID transport refuses anything over one 64-byte report. Rather
    than rely on the bootloader reassembling a frame across reports, which
    nothing here can verify, such records are re-encoded as consecutive
    16-byte records with their own checksums. A 16-byte record frames to at
    most 50 bytes. Records that already fit pass through unchanged.

    Raises:
        ValueError: if a record to be split runs past the end of its
            64 KiB segment.
    """
    result: List[bytes] = []
    for record in hex_records:
        if (
            record[3] != 0x00
            or len(create_program_flash_message(record)) <= max_frame_length
        ):
            result.append(record)
            continue
        offset = int.from_bytes(record[1:3], byteorder="big")
        data = record[4:-1]
        if offset + len(data) > 0x10000:
            raise ValueError(
                f"Hex data record at offset 0x{offset:04X} runs past the end "
                "of its 64 KiB segment."
            )
        for start in range(0, len(data), _SPLIT_RECORD_DATA_LENGTH):
            chunk = data[start:start + _SPLIT_RECORD_DATA_LENGTH]
            address = offset + start
            body = bytes([len(chunk), address >> 8, address & 0xFF, 0x00]) + chunk
            result.append(body + bytes([(-sum(body)) & 0xFF]))
    return result


@dataclass(frozen=True)
class FlashCrcRegion:
    """One contiguous run of programmed flash, checked after programming by
    asking the bootloader for its CRC-16 (``READ_CRC``) and comparing.

    ``address`` is the KSEG0 address to send; ``expected_crc`` is computed
    over the image's bytes with the bootloader's CRC (``APP_CalculateCrc``,
    the same CRC-16 that frames every message)."""

    address: int
    length: int
    expected_crc: int


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

    Beyond the `BootloaderProtocol` surface it can verify a flash
    (`create_read_crc_message`, `decode_read_crc_response`,
    `compute_crc_regions`); the update service uses them when present.
    """

    # The flash the bootloader erases and programs: the lower program-flash
    # panel, physical 0x1D000000-0x1D0FFFFF. Since daqifi-nyquist-firmware
    # #909 ERASE_FLASH clears only that panel (nvm.c APP_FlashErase), and
    # APP_ProgramHexRecord skips any byte outside APP_FLASH_BASE_ADDRESS..
    # APP_FLASH_END_ADDRESS. Data elsewhere (the upper panel, boot flash)
    # would be silently dropped or written over unerased flash, so an image
    # holding any is refused before anything is sent; the firmware's own
    # release script applies the same rule to every release image.
    APP_FLASH_START: int = 0x1D000000
    APP_FLASH_END: int = 0x1D100000  # exclusive
    _KSEG0: int = 0x80000000
    # The word the bootloader checks to decide whether to start an
    # application: it stays in bootloader mode only while it reads
    # 0xFFFFFFFF (app.c, APP_RESET_ADDRESS = APP_FLASH_BASE_ADDRESS).
    APP_RESET_ADDRESS: int = 0x9D000000
    # Image regions closer than this merge into one CRC window, the bytes
    # between them expected erased (0xFF): ERASE blanks the panel, and the
    # bootloader pads a partial word with 0xFF (APP_ProgramHexRecord). It
    # bounds the CRC exchanges for a sparse image: windows are at least this
    # far apart, so at most ~1,000 in the 1 MiB panel.
    CRC_MERGE_GAP: int = 1024

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

    def create_read_crc_message(self, address: int, length: int) -> bytes:
        return create_read_crc_message(address, length)

    def decode_read_crc_response(self, data: bytes) -> Optional[int]:
        return decode_read_crc_response(data)

    def parse_hex_file(self, hex_file_lines: List[str]) -> List[bytes]:
        """Parse an image into records ready to frame, one HID report each.

        Raises:
            ValueError: if the image is malformed, contains no data record
                to program, or has data outside the flash the bootloader
                erases (`APP_FLASH_START`..`APP_FLASH_END`). An image of
                only address and EOF records (or one whose data all lies in
                the protected range) would otherwise pass the byte-count
                check and erase flash with nothing to replace it.
        """
        records = self._data_checked_records(hex_file_lines)
        return split_oversized_records([record.data for record in records])

    def compute_crc_regions(self, hex_file_lines: List[str]) -> List[FlashCrcRegion]:
        """The image's programmed bytes as contiguous regions, each with the
        CRC the bootloader should report for it after programming.

        Data records are ordered by address and merged where they touch or
        are less than `CRC_MERGE_GAP` apart; the bytes of such a gap count
        as 0xFF, so verification also checks they stayed erased. A larger
        gap (or an overlap) starts a new region. Protected-range records are
        left out, as they are left out of programming.
        """
        data_records = sorted(
            (
                r for r in self._data_checked_records(hex_file_lines)
                if r.record_type == 0x00 and r.data[0] > 0
            ),
            key=lambda r: r.address,
        )
        regions: List[FlashCrcRegion] = []
        buffer = bytearray()
        start = next_address = 0
        for record in data_records:
            if buffer and next_address < record.address < next_address + self.CRC_MERGE_GAP:
                buffer += b"\xff" * (record.address - next_address)
                next_address = record.address
            if buffer and record.address != next_address:
                regions.append(self._region(start, buffer))
                buffer = bytearray()
            if not buffer:
                start = record.address
            length = record.data[0]
            buffer += record.data[4:4 + length]
            next_address = record.address + length
        if buffer:
            regions.append(self._region(start, buffer))
        return regions

    def reset_vector_region(self) -> FlashCrcRegion:
        """The bootloader's start-or-stay word, with the CRC it gives when
        erased: an erase isn't confirmed until this reads back blank."""
        return FlashCrcRegion(
            address=self.APP_RESET_ADDRESS,
            length=4,
            expected_crc=Crc16(b"\xff" * 4).crc,
        )

    def _region(self, start: int, data: bytes) -> FlashCrcRegion:
        return FlashCrcRegion(
            address=start | self._KSEG0,
            length=len(data),
            expected_crc=Crc16(bytes(data)).crc,
        )

    def _data_checked_records(self, hex_file_lines: List[str]) -> List[HexFileRecord]:
        records = self._hex_parser.parse_records(hex_file_lines)
        # The address model here knows data (00), end of file (01) and
        # extended linear address (04) records, all an XC32 image has. The
        # bootloader also reads 02 as a segment base and resets its base on
        # 01, 03 and 05 (nvm.c APP_ProgramHexRecord), so with any other type,
        # or a record after the end of file, the addresses checked here could
        # differ from the ones written. Such an image is refused.
        seen_end = False
        for record in records:
            if seen_end:
                raise ValueError(
                    "Firmware HEX file has records after its end-of-file record."
                )
            if record.record_type not in (0x00, 0x01, 0x04):
                raise ValueError(
                    f"Firmware HEX file has a type-{record.record_type:02X} record, "
                    "which this updater doesn't model; only data, end-of-file "
                    "and extended linear address records are accepted."
                )
            seen_end = record.record_type == 0x01
        data_records = [r for r in records if r.record_type == 0x00 and r.data[0] > 0]
        if not data_records:
            raise ValueError(
                "Firmware HEX file contains no data records to program."
            )
        for record in data_records:
            first = record.address
            end = first + record.data[0]
            if first < self.APP_FLASH_START or end > self.APP_FLASH_END:
                raise ValueError(
                    f"Firmware HEX file has data at 0x{first:08X}-0x{end - 1:08X}, "
                    "outside the flash the bootloader erases "
                    f"(0x{self.APP_FLASH_START:08X}-0x{self.APP_FLASH_END - 1:08X}); "
                    "it would be skipped or written over unerased flash. This "
                    "is not a bootloader-linked application image."
                )
        return records
