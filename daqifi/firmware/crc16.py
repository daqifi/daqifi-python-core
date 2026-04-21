"""CRC-16/XMODEM checksum.

Port of `Daqifi.Core.Firmware.Crc16`. Uses the same 4-bit lookup
table to produce identical checksums to the C# implementation —
verified by parity tests against known XMODEM test vectors.
"""
from __future__ import annotations


__all__ = ["Crc16"]


_TABLE = (
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
    0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
)


class Crc16:
    """Computes a CRC-16/XMODEM checksum over a byte sequence."""

    def __init__(self, data: bytes) -> None:
        if data is None:
            raise TypeError("data must not be None")
        self._crc = self._calculate(data)

    @property
    def crc(self) -> int:
        """The full 16-bit CRC value."""
        return self._crc

    @property
    def low(self) -> int:
        """The low byte of the CRC."""
        return self._crc & 0xFF

    @property
    def high(self) -> int:
        """The high byte of the CRC."""
        return (self._crc >> 8) & 0xFF

    @staticmethod
    def _calculate(data: bytes) -> int:
        crc = 0
        for item in data:
            i = (crc >> 12) ^ (item >> 4)
            crc = (_TABLE[i & 0x0F] ^ (crc << 4)) & 0xFFFF
            i = (crc >> 12) ^ (item & 0x0F)
            crc = (_TABLE[i & 0x0F] ^ (crc << 4)) & 0xFFFF
        return crc
