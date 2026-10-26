"""Intel HEX file record parsing.

Port of `Daqifi.Core.Firmware.HexFileRecord` and
`Daqifi.Core.Firmware.IntelHexParser` from daqifi-core.

Parses Intel HEX format firmware images into structured records.
Supports a configurable protected memory range — records whose
data lives in that range (typically calibration data) are
filtered out so the caller doesn't reflash them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional


__all__ = ["HexFileRecord", "IntelHexParser"]


@dataclass(frozen=True)
class HexFileRecord:
    """A parsed Intel HEX record with its full 32-bit linear address."""

    address: int
    data: bytes
    record_type: int


class IntelHexParser:
    """Parses Intel HEX formatted firmware lines into records.

    Records whose data lies in the configured protected memory
    range are dropped (not returned). The default range matches
    the PIC32 calibration region used by daqifi-core.
    """

    DEFAULT_BEGIN_PROTECTED_ADDRESS: int = 0x1D1E0000
    DEFAULT_END_PROTECTED_ADDRESS: int = 0x1D200000

    def __init__(
        self,
        begin_protected_address: Optional[int] = None,
        end_protected_address: Optional[int] = None,
    ) -> None:
        self._begin_protected_address = (
            begin_protected_address
            if begin_protected_address is not None
            else self.DEFAULT_BEGIN_PROTECTED_ADDRESS
        )
        self._end_protected_address = (
            end_protected_address
            if end_protected_address is not None
            else self.DEFAULT_END_PROTECTED_ADDRESS
        )

    def parse_hex_records(self, lines: Iterable[str]) -> List[bytes]:
        """Parse HEX lines into a list of raw record byte sequences.

        Filters out records whose data is in the protected range.

        Raises:
            TypeError: if `lines` is None.
            ValueError: if a line is malformed or has an invalid checksum,
                or a data record runs into the protected range.
        """
        if lines is None:
            raise TypeError("lines must not be None")

        base_address: int = 0
        hex_records: List[bytes] = []

        for raw_line in lines:
            # Strip line terminators so callers can pass an open file
            # iterator directly (`for line in open(path):`) without
            # tripping the odd-character validation on `\r\n` endings.
            line = raw_line.strip() if raw_line else raw_line
            if not line:
                continue

            self._validate_line(line)
            hex_line = self._convert_line_to_bytes(line)
            self._validate_checksum(hex_line, line)
            self._validate_record_length(hex_line, line)

            base_address = self._update_base_address(hex_line, base_address)

            if self._is_protected_hex_record(hex_line, base_address):
                continue

            hex_records.append(hex_line)

        return hex_records

    def parse_records(self, lines: Iterable[str]) -> List[HexFileRecord]:
        """Parse HEX lines into structured records with computed addresses.

        Records whose data is in the protected range are dropped.

        Raises:
            TypeError: if `lines` is None.
            ValueError: if a line is malformed or has an invalid checksum,
                or a data record runs into the protected range.
        """
        if lines is None:
            raise TypeError("lines must not be None")

        base_address: int = 0
        records: List[HexFileRecord] = []

        for raw_line in lines:
            # Strip line terminators so callers can pass file-iterator lines
            # directly (the same fix as `parse_hex_records`).
            line = raw_line.strip() if raw_line else raw_line
            if not line:
                continue

            self._validate_line(line)
            hex_line = self._convert_line_to_bytes(line)
            self._validate_checksum(hex_line, line)
            self._validate_record_length(hex_line, line)
            record_type = hex_line[3]

            base_address = self._update_base_address(hex_line, base_address)

            if self._is_protected_hex_record(hex_line, base_address):
                continue

            offset_address = int.from_bytes(hex_line[1:3], byteorder="big")
            full_address = (base_address << 16) | offset_address

            records.append(HexFileRecord(
                address=full_address,
                data=hex_line,
                record_type=record_type,
            ))

        return records

    @staticmethod
    def _validate_line(line: str) -> None:
        if line[0] != ":":
            raise ValueError(
                f'The hex record "{line}" doesn\'t start with the colon character ":"'
            )

        if len(line) % 2 != 1:
            raise ValueError(
                f'The hex record "{line}" doesn\'t contain an odd number of characters'
            )

        if len(line) < 11:
            raise ValueError(
                f'The hex record "{line}" is too short to be a valid record'
            )

    @staticmethod
    def _validate_checksum(record: bytes, line: str) -> None:
        # Intel HEX checksum: sum of all bytes (including checksum) mod 256 must be 0.
        if sum(record) & 0xFF != 0:
            raise ValueError(f'The hex record "{line}" has an invalid checksum')

    @staticmethod
    def _validate_record_length(record: bytes, line: str) -> None:
        # Port of upstream ValidateRecordLength (daqifi-core #370). The
        # checksum alone can't catch a record whose declared byte count
        # disagrees with what it carries: ":10000000F0" declares 16 data
        # bytes, carries none, and still sums to zero.
        declared_data_length = record[0]
        actual_data_length = len(record) - 5
        if actual_data_length != declared_data_length:
            raise ValueError(
                f'The hex record "{line}" declares {declared_data_length} '
                f"data byte(s) but contains {actual_data_length}"
            )

    @staticmethod
    def _convert_line_to_bytes(line: str) -> bytes:
        try:
            return bytes.fromhex(line[1:])
        except ValueError as exc:
            raise ValueError(
                f'The hex record "{line}" contains invalid hex characters'
            ) from exc

    @staticmethod
    def _update_base_address(hex_record: bytes, current_base_address: int) -> int:
        record_type = hex_record[3]
        if record_type == 0x04:
            # Extended-linear-address record: data bytes carry the new high-16
            # base. The Intel HEX spec requires exactly 2 data bytes; reject
            # malformed records here rather than silently returning a wrong
            # base address from a slice of the wrong length.
            byte_count = hex_record[0]
            if byte_count != 2:
                raise ValueError(
                    "Extended-linear-address record must contain exactly 2 "
                    f"data bytes (got {byte_count})."
                )
            data_bytes = hex_record[4:6]
            return int.from_bytes(data_bytes, byteorder="big")
        return current_base_address

    def _is_protected_hex_record(self, hex_record: bytes, base_address: int) -> bool:
        record_type = hex_record[3]
        if record_type != 0x00:
            return False

        offset_address = int.from_bytes(hex_record[1:3], byteorder="big")
        first_address = (base_address << 16) | offset_address
        last_address = first_address + max(hex_record[0], 1) - 1

        if self._begin_protected_address <= first_address <= self._end_protected_address:
            return True
        if first_address < self._begin_protected_address <= last_address:
            # Starts outside the range but runs into it. Keeping the record
            # reflashes protected bytes and dropping it loses unprotected
            # ones, so refuse the image here, before any device I/O.
            raise ValueError(
                f"Hex record at 0x{first_address:08X}-0x{last_address:08X} "
                "runs into the protected range starting at "
                f"0x{self._begin_protected_address:08X}."
            )
        return False
