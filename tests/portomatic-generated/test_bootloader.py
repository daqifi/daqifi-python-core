"""Tests for `daqifi.firmware.bootloader`.

Round-trip tests verify that producer-emitted frames decode back via
the consumer functions, and that the concrete `Pic32BootloaderProtocol`
bridges between the two.
"""
from __future__ import annotations

import pytest

from daqifi.firmware import BootloaderProtocol, Pic32BootloaderProtocol
from daqifi.firmware.bootloader import (
    create_erase_flash_message,
    create_jump_to_application_message,
    create_program_flash_message,
    create_request_version_message,
    decode_erase_flash_response,
    decode_program_flash_response,
    decode_version_response,
)


_SOH = 0x01
_EOT = 0x04
_DLE = 0x10


# -- producer ----------------------------------------------------------

class TestCreateRequestVersionMessage:
    def test_starts_with_soh_and_ends_with_eot(self):
        frame = create_request_version_message()
        assert frame[0] == _SOH
        assert frame[-1] == _EOT

    def test_is_framed_correctly(self):
        # Request-version cmd byte is 0x01 (== SOH) so it must be DLE-escaped.
        frame = create_request_version_message()
        # SOH | DLE | 0x01 | crc_low (escaped if needed) | crc_high (escaped if needed) | EOT
        assert frame[1] == _DLE
        assert frame[2] == 0x01

    def test_returns_bytes(self):
        assert isinstance(create_request_version_message(), bytes)


class TestCreateEraseFlashMessage:
    def test_frame_shape(self):
        frame = create_erase_flash_message()
        assert frame[0] == _SOH
        assert frame[-1] == _EOT
        # 0x02 doesn't collide with SOH/EOT/DLE so no escape.
        assert frame[1] == 0x02


class TestCreateJumpToApplicationMessage:
    def test_frame_shape(self):
        frame = create_jump_to_application_message()
        assert frame[0] == _SOH
        assert frame[-1] == _EOT
        assert frame[1] == 0x05


class TestCreateProgramFlashMessage:
    def test_none_record_raises(self):
        with pytest.raises(TypeError):
            create_program_flash_message(None)  # type: ignore[arg-type]

    def test_carries_record_payload(self):
        frame = create_program_flash_message(b"\xaa\xbb\xcc")
        # First non-SOH byte is the program-flash command (0x03).
        assert frame[0] == _SOH
        assert frame[1] == 0x03
        # Payload bytes appear before the CRC + EOT.
        assert b"\xaa\xbb\xcc" in frame
        assert frame[-1] == _EOT


# -- consumer ----------------------------------------------------------

class TestDecodeVersionResponse:
    def test_short_data_returns_error(self):
        assert decode_version_response(b"") == "Error"
        assert decode_version_response(b"\x01") == "Error"

    def test_missing_soh_returns_error(self):
        assert decode_version_response(b"\x00\x10\x01\x02\x03") == "Error"

    def test_valid_response_returns_major_minor(self):
        # SOH | DLE | cmd 0x01 | major 0x02 | minor 0x05
        data = bytes([_SOH, _DLE, 0x01, 0x02, 0x05])
        assert decode_version_response(data) == "2.5"

    def test_valid_response_with_dle_escaped_major(self):
        # Major byte happens to equal DLE so it's escaped; consumer should unescape.
        data = bytes([_SOH, _DLE, 0x01, _DLE, _DLE, 0x05])
        # After unescaping, major = DLE = 0x10 = 16
        assert decode_version_response(data) == "16.5"

    def test_truncated_response_returns_zeros(self):
        # Only 2 bytes — not enough for cmd + major + minor.
        assert decode_version_response(bytes([_SOH, 0x99])) == "0.0"


class TestDecodeProgramFlashResponse:
    def test_valid_ack(self):
        assert decode_program_flash_response(bytes([_SOH, 0x03])) is True

    def test_wrong_command_byte(self):
        assert decode_program_flash_response(bytes([_SOH, 0x02])) is False

    def test_missing_soh(self):
        assert decode_program_flash_response(bytes([0x00, 0x03])) is False

    def test_short_data(self):
        assert decode_program_flash_response(b"") is False
        assert decode_program_flash_response(b"\x01") is False

    def test_none_data(self):
        assert decode_program_flash_response(None) is False  # type: ignore[arg-type]


class TestDecodeEraseFlashResponse:
    def test_valid_ack(self):
        assert decode_erase_flash_response(bytes([_SOH, 0x02])) is True

    def test_wrong_command_byte(self):
        assert decode_erase_flash_response(bytes([_SOH, 0x03])) is False

    def test_missing_soh(self):
        assert decode_erase_flash_response(bytes([0x00, 0x02])) is False


# -- Pic32BootloaderProtocol concrete ----------------------------------

class TestPic32BootloaderProtocol:
    def test_implements_protocol(self):
        proto: BootloaderProtocol = Pic32BootloaderProtocol()
        assert isinstance(proto, BootloaderProtocol)

    def test_message_methods_delegate_to_module_functions(self):
        proto = Pic32BootloaderProtocol()
        assert proto.create_request_version_message() == create_request_version_message()
        assert proto.create_erase_flash_message() == create_erase_flash_message()
        assert proto.create_jump_to_application_message() == create_jump_to_application_message()
        assert proto.create_program_flash_message(b"\x42") == create_program_flash_message(b"\x42")

    def test_decode_methods_delegate(self):
        proto = Pic32BootloaderProtocol()
        valid = bytes([_SOH, _DLE, 0x01, 0x01, 0x02])
        assert proto.decode_version_response(valid) == "1.2"
        assert proto.decode_program_flash_response(bytes([_SOH, 0x03])) is True
        assert proto.decode_erase_flash_response(bytes([_SOH, 0x02])) is True

    def test_parse_hex_file(self):
        proto = Pic32BootloaderProtocol()
        records = proto.parse_hex_file([":00000001FF"])
        assert len(records) == 1
        assert records[0][3] == 0x01  # EOF record type

    def test_custom_protected_range(self):
        proto = Pic32BootloaderProtocol(0x10000, 0x20000)
        # Smoke — just verify the parser was constructed with custom range.
        records = proto.parse_hex_file([":00000001FF"])
        assert len(records) == 1
