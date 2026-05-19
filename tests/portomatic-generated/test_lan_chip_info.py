"""Tests for `daqifi.firmware.lan_chip_info` (port of C# LanChipInfo + Parser)."""
from __future__ import annotations

from daqifi.firmware import LanChipInfo
from daqifi.firmware.lan_chip_info import try_parse, try_parse_lines


class TestLanChipInfoShape:
    def test_construct_sets_fields(self):
        info = LanChipInfo(chip_id=1234, fw_version="19.5.4", build_date="Jan  8 2019")
        assert info.chip_id == 1234
        assert info.fw_version == "19.5.4"
        assert info.build_date == "Jan  8 2019"


class TestTryParse:
    def test_valid_json_returns_info(self):
        result = try_parse('{"ChipId":1234,"FwVersion":"19.5.4","BuildDate":"Jan  8 2019"}')
        assert result is not None
        assert result.chip_id == 1234
        assert result.fw_version == "19.5.4"
        assert result.build_date == "Jan  8 2019"

    def test_none_returns_none(self):
        assert try_parse(None) is None

    def test_blank_returns_none(self):
        assert try_parse("") is None
        assert try_parse("   ") is None
        assert try_parse("\n\t") is None

    def test_malformed_json_returns_none(self):
        assert try_parse("{not json}") is None
        assert try_parse('{"FwVersion":') is None

    def test_missing_fw_version_returns_none(self):
        assert try_parse('{"ChipId":1234,"BuildDate":"Jan  8 2019"}') is None

    def test_empty_fw_version_returns_none(self):
        assert try_parse('{"ChipId":1234,"FwVersion":"","BuildDate":"Jan  8 2019"}') is None

    def test_missing_chip_id_defaults_to_zero(self):
        result = try_parse('{"FwVersion":"19.5.4","BuildDate":"Jan  8 2019"}')
        assert result is not None
        assert result.chip_id == 0
        assert result.fw_version == "19.5.4"

    def test_missing_build_date_defaults_to_empty(self):
        result = try_parse('{"ChipId":1234,"FwVersion":"19.5.4"}')
        assert result is not None
        assert result.build_date == ""

    def test_non_numeric_chip_id_defaults_to_zero(self):
        result = try_parse('{"ChipId":"not-a-number","FwVersion":"19.5.4"}')
        assert result is not None
        assert result.chip_id == 0

    def test_array_root_returns_none(self):
        # The C# parser only handles JSON objects, not arrays.
        assert try_parse("[1,2,3]") is None

    def test_extra_fields_ignored(self):
        result = try_parse(
            '{"ChipId":1,"FwVersion":"v","BuildDate":"d","ExtraField":"ignored"}'
        )
        assert result is not None
        assert result.chip_id == 1


class TestTryParseLines:
    def test_returns_first_successful_parse(self):
        lines = [
            "garbage",
            "",
            '{"ChipId":1,"FwVersion":"v1","BuildDate":"d1"}',
            '{"ChipId":2,"FwVersion":"v2","BuildDate":"d2"}',
        ]
        result = try_parse_lines(lines)
        assert result is not None
        assert result.chip_id == 1
        assert result.fw_version == "v1"

    def test_empty_iterable_returns_none(self):
        assert try_parse_lines([]) is None

    def test_all_blank_returns_none(self):
        assert try_parse_lines(["", "  ", "\n"]) is None

    def test_all_garbage_returns_none(self):
        assert try_parse_lines(["abc", "def", "ghi"]) is None
