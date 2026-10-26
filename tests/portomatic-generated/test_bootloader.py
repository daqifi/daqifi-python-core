"""Tests for `daqifi.firmware.bootloader`.

Round-trip tests verify that producer-emitted frames decode back via
the consumer functions, and that the concrete `Pic32BootloaderProtocol`
bridges between the two.
"""
from __future__ import annotations

import pytest

from daqifi.firmware import BootloaderProtocol, Pic32BootloaderProtocol
from daqifi.firmware.bootloader import (
    _construct_data_packet,
    create_erase_flash_message,
    create_jump_to_application_message,
    create_program_flash_message,
    create_request_version_message,
    decode_erase_flash_response,
    decode_program_flash_response,
    decode_version_response,
    split_oversized_records,
)


_SOH = 0x01
_EOT = 0x04
_DLE = 0x10


def _reply(*payload: int) -> bytes:
    # The bootloader frames its replies exactly as the host frames requests:
    # SOH escaped(payload + CRC-16 lo/hi) EOT (firmware bootloader.c,
    # SEND_RESPONSE state).
    return _construct_data_packet(bytes(payload))


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
        assert decode_version_response(b"\x00" + _reply(0x01, 0x02, 0x05)[1:]) == "Error"

    def test_valid_response_returns_major_minor(self):
        assert decode_version_response(_reply(0x01, 0x02, 0x05)) == "2.5"

    def test_valid_response_with_dle_escaped_major(self):
        # Major byte happens to equal DLE so it's escaped; consumer should unescape.
        data = _reply(0x01, _DLE, 0x05)
        assert bytes([_DLE, _DLE]) in data
        assert decode_version_response(data) == "16.5"

    def test_hid_report_padding_after_eot_is_ignored(self):
        assert decode_version_response(_reply(0x01, 0x02, 0x05) + b"\x00" * 40) == "2.5"

    def test_unframed_reply_returns_error(self):
        # Audit finding: b"\x01\x02" used to decode as version "0.0".
        assert decode_version_response(bytes([_SOH, 0x02])) == "Error"
        assert decode_version_response(bytes([_SOH, _DLE, 0x01, 0x02, 0x05])) == "Error"

    def test_reply_to_another_command_returns_error(self):
        # A stale erase acknowledgment must not pass as a version reply.
        assert decode_version_response(_reply(0x02)) == "Error"

    def test_bad_crc_returns_error(self):
        data = bytearray(_reply(0x01, 0x02, 0x05))
        data[-2] ^= 0xFF
        assert decode_version_response(bytes(data)) == "Error"

    def test_missing_eot_returns_error(self):
        assert decode_version_response(_reply(0x01, 0x02, 0x05)[:-1]) == "Error"

    def test_dle_at_end_of_minor_returns_error(self):
        # Qodo PR #11 finding S2: an escape marker without a following
        # payload byte must be reported as malformed framing rather than
        # silently treated as the value byte.
        data = bytes([_SOH, _DLE, 0x01, 0x02, _DLE])
        assert decode_version_response(data) == "Error"


class TestDecodeProgramFlashResponse:
    def test_valid_ack(self):
        assert decode_program_flash_response(_reply(0x03)) is True

    def test_wrong_command_byte(self):
        assert decode_program_flash_response(_reply(0x02)) is False

    def test_missing_soh(self):
        assert decode_program_flash_response(b"\x00" + _reply(0x03)[1:]) is False

    def test_short_data(self):
        assert decode_program_flash_response(b"") is False
        assert decode_program_flash_response(b"\x01") is False

    def test_none_data(self):
        assert decode_program_flash_response(None) is False  # type: ignore[arg-type]

    def test_unframed_ack_is_rejected(self):
        # Audit finding: SOH + command with no CRC or EOT used to pass.
        assert decode_program_flash_response(bytes([_SOH, 0x03])) is False


class TestDecodeEraseFlashResponse:
    def test_valid_ack(self):
        assert decode_erase_flash_response(_reply(0x02)) is True

    def test_wrong_command_byte(self):
        assert decode_erase_flash_response(_reply(0x03)) is False

    def test_missing_soh(self):
        assert decode_erase_flash_response(b"\x00" + _reply(0x02)[1:]) is False

    def test_unframed_ack_is_rejected(self):
        assert decode_erase_flash_response(bytes([_SOH, 0x02])) is False


def _data_record(offset: int, data: bytes) -> bytes:
    body = bytes([len(data), offset >> 8, offset & 0xFF, 0x00]) + data
    return body + bytes([(-sum(body)) & 0xFF])


def _hex_line(record: bytes) -> str:
    return ":" + record.hex().upper()


class TestSplitOversizedRecords:
    def test_record_that_fits_one_report_passes_through(self):
        record = _data_record(0, b"\x01" * 16)
        assert len(create_program_flash_message(record)) <= 64
        assert split_oversized_records([record]) == [record]

    def test_escape_heavy_record_is_split_into_frames_that_fit(self):
        # Audit finding: 32 bytes of 0x01 frame to 75 bytes, which the HID
        # transport refuses after flash has already been erased.
        record = _data_record(0x0100, b"\x01" * 32)
        assert len(create_program_flash_message(record)) > 64
        pieces = split_oversized_records([record])
        assert pieces == [
            _data_record(0x0100, b"\x01" * 16),
            _data_record(0x0110, b"\x01" * 16),
        ]
        assert all(len(create_program_flash_message(p)) <= 64 for p in pieces)

    def test_split_preserves_every_byte_at_its_address(self):
        data = bytes(range(0x10, 0x10 + 40)) + b"\x10" * 8
        record = _data_record(0x2000, data)
        pieces = split_oversized_records([record])
        rebuilt = {}
        for piece in pieces:
            offset = int.from_bytes(piece[1:3], "big")
            for i, byte in enumerate(piece[4:-1]):
                rebuilt[offset + i] = byte
            assert sum(piece) & 0xFF == 0
        assert rebuilt == {0x2000 + i: b for i, b in enumerate(data)}

    def test_non_data_records_pass_through(self):
        ela = bytes.fromhex("020000041D00DD")
        eof = bytes.fromhex("00000001FF")
        assert split_oversized_records([ela, eof]) == [ela, eof]

    def test_record_running_past_its_segment_is_refused(self):
        record = _data_record(0xFFF0, b"\x01" * 32)
        with pytest.raises(ValueError, match="64 KiB segment"):
            split_oversized_records([record])


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
        assert proto.decode_version_response(_reply(0x01, 0x01, 0x02)) == "1.2"
        assert proto.decode_program_flash_response(_reply(0x03)) is True
        assert proto.decode_erase_flash_response(_reply(0x02)) is True

    def test_parse_hex_file(self):
        proto = Pic32BootloaderProtocol()
        data = _data_record(0, b"\xAA\xBB")
        records = proto.parse_hex_file([_hex_line(data), ":00000001FF"])
        assert records == [data, bytes.fromhex("00000001FF")]

    def test_parse_hex_file_rejects_an_image_without_data(self):
        # Audit finding: an EOF-only image passed the byte-count check and
        # the update erased flash with nothing to program.
        proto = Pic32BootloaderProtocol()
        with pytest.raises(ValueError, match="no data records"):
            proto.parse_hex_file([":00000001FF"])

    def test_parse_hex_file_rejects_an_image_whose_data_is_all_protected(self):
        proto = Pic32BootloaderProtocol(0x10000, 0x1FFFF)
        lines = [":020000040001F9", _hex_line(_data_record(0, b"\x01\x02")), ":00000001FF"]
        with pytest.raises(ValueError, match="no data records"):
            proto.parse_hex_file(lines)

    def test_parse_hex_file_splits_records_too_large_for_one_report(self):
        proto = Pic32BootloaderProtocol()
        records = proto.parse_hex_file(
            [_hex_line(_data_record(0, b"\x01" * 32)), ":00000001FF"]
        )
        assert [r[0] for r in records] == [16, 16, 0]

    def test_custom_protected_range(self):
        proto = Pic32BootloaderProtocol(0x10000, 0x20000)
        lines = [
            ":020000040001F9",
            _hex_line(_data_record(0, b"\x01")),
            ":020000040003F7",
            _hex_line(_data_record(0, b"\x02")),
            ":00000001FF",
        ]
        records = proto.parse_hex_file(lines)
        assert _data_record(0, b"\x01") not in records
        assert _data_record(0, b"\x02") in records
