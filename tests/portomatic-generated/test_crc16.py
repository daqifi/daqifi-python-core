"""Tests for `daqifi.firmware.crc16` (port of C# Crc16)."""
from __future__ import annotations

import pytest

from daqifi.firmware import Crc16


class TestCrc16:
    def test_none_data_raises(self):
        with pytest.raises(TypeError):
            Crc16(None)  # type: ignore[arg-type]

    def test_empty_bytes_yields_zero(self):
        c = Crc16(b"")
        assert c.crc == 0
        assert c.low == 0
        assert c.high == 0

    def test_single_byte(self):
        # CRC-16/XMODEM of a single 0x00 byte == 0
        c = Crc16(b"\x00")
        assert c.crc == 0

    def test_known_xmodem_vector_a(self):
        # CRC-16/XMODEM of "A" = 0x58E5
        c = Crc16(b"A")
        assert c.crc == 0x58E5
        assert c.low == 0xE5
        assert c.high == 0x58

    def test_known_xmodem_vector_123456789(self):
        # Standard CRC-16/XMODEM check value for "123456789" is 0x31C3.
        c = Crc16(b"123456789")
        assert c.crc == 0x31C3

    def test_low_high_decompose(self):
        c = Crc16(b"\xff\xff\xff\xff")
        assert c.low == c.crc & 0xFF
        assert c.high == (c.crc >> 8) & 0xFF
        assert c.crc == (c.high << 8) | c.low

    def test_repeated_call_consistent(self):
        a = Crc16(b"hello").crc
        b = Crc16(b"hello").crc
        assert a == b

    def test_different_inputs_produce_different_crc(self):
        a = Crc16(b"hello").crc
        b = Crc16(b"world").crc
        assert a != b

    def test_long_input_does_not_overflow(self):
        c = Crc16(bytes(1024))  # 1KB of zeros
        # Within 16 bits — must be < 65536
        assert 0 <= c.crc <= 0xFFFF
