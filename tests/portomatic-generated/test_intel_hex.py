"""Tests for `daqifi.firmware.intel_hex` (port of C# IntelHexParser).

Mirrors the structure of `Daqifi.Core.Tests.Firmware.IntelHexParserTests`
plus a handful of HexFileRecord shape checks.
"""
from __future__ import annotations

import pytest

from daqifi.firmware import HexFileRecord, IntelHexParser


# -- HexFileRecord -----------------------------------------------------------

class TestHexFileRecord:
    def test_construct_sets_fields(self):
        rec = HexFileRecord(address=0x1D000100, data=b"\x10\x00\x00\x00", record_type=0x00)
        assert rec.address == 0x1D000100
        assert rec.data == b"\x10\x00\x00\x00"
        assert rec.record_type == 0x00

    def test_is_frozen(self):
        rec = HexFileRecord(address=0, data=b"", record_type=0)
        with pytest.raises(Exception):
            rec.address = 1  # type: ignore[misc]

    def test_equality_value_based(self):
        a = HexFileRecord(address=1, data=b"\x00", record_type=0)
        b = HexFileRecord(address=1, data=b"\x00", record_type=0)
        c = HexFileRecord(address=2, data=b"\x00", record_type=0)
        assert a == b
        assert a != c


# -- ParseHexRecords: input validation --------------------------------------

class TestParseHexRecordsInputValidation:
    def setup_method(self):
        self.parser = IntelHexParser()

    def test_none_lines_raises(self):
        with pytest.raises(TypeError):
            self.parser.parse_hex_records(None)  # type: ignore[arg-type]

    def test_empty_iterable_returns_empty(self):
        assert self.parser.parse_hex_records([]) == []

    def test_blank_lines_skipped(self):
        assert self.parser.parse_hex_records(["", "  ", "\t"]) == []

    def test_line_without_colon_raises(self):
        with pytest.raises(ValueError):
            self.parser.parse_hex_records(["1000000000FF"])

    def test_line_with_even_char_count_raises(self):
        with pytest.raises(ValueError):
            self.parser.parse_hex_records([":100000000"])

    def test_too_short_line_raises(self):
        with pytest.raises(ValueError):
            self.parser.parse_hex_records([":0000"])


# -- ParseHexRecords: checksum validation -----------------------------------

class TestParseHexRecordsChecksum:
    def setup_method(self):
        self.parser = IntelHexParser()

    def test_invalid_checksum_raises(self):
        # Valid record is :020000041D00DD; corrupt the checksum
        with pytest.raises(ValueError, match="invalid checksum"):
            self.parser.parse_hex_records([":020000041D00AA"])

    def test_valid_checksum_does_not_raise(self):
        result = self.parser.parse_hex_records([":020000041D00DD"])
        assert len(result) == 1

    def test_invalid_hex_characters_raises(self):
        with pytest.raises(ValueError, match="invalid hex characters"):
            self.parser.parse_hex_records([":02ZZ00041D00DD"])


# -- ParseHexRecords: valid data -------------------------------------------

class TestParseHexRecordsValidData:
    def setup_method(self):
        self.parser = IntelHexParser()

    def test_valid_data_record_parses(self):
        result = self.parser.parse_hex_records(
            [":10000000AABBCCDDEEFF00112233445566778899F8"]
        )
        assert len(result) == 1
        rec = result[0]
        assert rec[0] == 0x10  # byte count
        assert rec[1] == 0x00  # address high
        assert rec[2] == 0x00  # address low
        assert rec[3] == 0x00  # record type (data)

    def test_eof_record_included(self):
        result = self.parser.parse_hex_records([":00000001FF"])
        assert len(result) == 1
        assert result[0][3] == 0x01

    def test_multiple_records_parses_all(self):
        lines = [
            ":020000041D1EBF",  # extended address 0x1D1E
            ":00000001FF",      # EOF
        ]
        result = self.parser.parse_hex_records(lines)
        # Extended-address records aren't themselves protected; only data records at those addresses.
        assert len(result) == 2


# -- ParseHexRecords: protected memory range --------------------------------

class TestParseHexRecordsProtectedRange:
    def test_data_in_protected_range_filtered(self):
        parser = IntelHexParser()
        lines = [
            ":020000041D1EBF",                              # ext addr 0x1D1E
            ":10000000AABBCCDDEEFF00112233445566778899F8",  # data at 0x1D1E0000 - protected
            ":00000001FF",                                  # EOF
        ]
        result = parser.parse_hex_records(lines)
        assert len(result) == 2
        assert result[0][3] == 0x04  # ext address survived
        assert result[1][3] == 0x01  # EOF survived

    def test_data_outside_protected_range_kept(self):
        parser = IntelHexParser()
        lines = [
            ":020000041D00DD",                              # ext addr 0x1D00
            ":10000000AABBCCDDEEFF00112233445566778899F8",  # data at 0x1D000000 - NOT protected
            ":00000001FF",                                  # EOF
        ]
        result = parser.parse_hex_records(lines)
        assert len(result) == 3

    def test_data_at_protected_boundary_end_filtered(self):
        parser = IntelHexParser()
        lines = [
            ":020000041D20BD",                              # ext addr 0x1D20
            ":10000000AABBCCDDEEFF00112233445566778899F8",  # data at 0x1D200000 - boundary
            ":00000001FF",                                  # EOF
        ]
        result = parser.parse_hex_records(lines)
        assert len(result) == 2

    def test_data_just_after_protected_range_kept(self):
        parser = IntelHexParser()
        lines = [
            ":020000041D20BD",                              # ext addr 0x1D20
            ":10000100AABBCCDDEEFF00112233445566778899F7",  # data at 0x1D200100 - outside
            ":00000001FF",                                  # EOF
        ]
        result = parser.parse_hex_records(lines)
        assert len(result) == 3

    def test_custom_protected_range(self):
        parser = IntelHexParser(0x00010000, 0x00020000)
        lines = [
            ":020000040001F9",                              # ext addr 0x0001
            ":10000000AABBCCDDEEFF00112233445566778899F8",  # data at 0x00010000 - protected
            ":020000040003F7",                              # ext addr 0x0003
            ":10000000AABBCCDDEEFF00112233445566778899F8",  # data at 0x00030000 - NOT protected
            ":00000001FF",                                  # EOF
        ]
        result = parser.parse_hex_records(lines)
        # First ext addr + second ext addr + second data + EOF = 4
        assert len(result) == 4

    def test_default_constants_match_expected(self):
        assert IntelHexParser.DEFAULT_BEGIN_PROTECTED_ADDRESS == 0x1D1E0000
        assert IntelHexParser.DEFAULT_END_PROTECTED_ADDRESS == 0x1D200000


# -- ParseRecords -----------------------------------------------------------

class TestParseRecords:
    def setup_method(self):
        self.parser = IntelHexParser()

    def test_none_lines_raises(self):
        with pytest.raises(TypeError):
            self.parser.parse_records(None)  # type: ignore[arg-type]

    def test_valid_data_record_computes_address(self):
        lines = [
            ":020000041D00DD",                              # ext addr 0x1D00
            ":10010000AABBCCDDEEFF00112233445566778899F7",  # data at offset 0x0100
        ]
        result = self.parser.parse_records(lines)
        assert len(result) == 2
        assert result[1].address == 0x1D000100
        assert result[1].record_type == 0x00

    def test_returns_correct_record_types(self):
        lines = [
            ":020000041D00DD",                              # ext addr (type 0x04)
            ":10000000AABBCCDDEEFF00112233445566778899F8",  # data    (type 0x00)
            ":00000001FF",                                  # EOF     (type 0x01)
        ]
        result = self.parser.parse_records(lines)
        assert len(result) == 3
        assert result[0].record_type == 0x04
        assert result[1].record_type == 0x00
        assert result[2].record_type == 0x01

    def test_returns_hexfilerecord_instances(self):
        result = self.parser.parse_records([":00000001FF"])
        assert isinstance(result[0], HexFileRecord)
        assert isinstance(result[0].data, bytes)


# -- Hex parsing edge cases -------------------------------------------------

class TestHexParsingEdgeCases:
    def setup_method(self):
        self.parser = IntelHexParser()

    def test_hex_parsing_conversion_correct_bytes(self):
        # :02 0000 04 1D00 DD
        result = self.parser.parse_hex_records([":020000041D00DD"])
        assert len(result) == 1
        rec = result[0]
        assert rec[0] == 0x02
        assert rec[1] == 0x00
        assert rec[2] == 0x00
        assert rec[3] == 0x04
        assert rec[4] == 0x1D
        assert rec[5] == 0x00
        assert rec[6] == 0xDD

    def test_mixed_case_hex_parses(self):
        result = self.parser.parse_hex_records([":020000041d00DD"])
        assert len(result) == 1
        assert result[0][4] == 0x1D
