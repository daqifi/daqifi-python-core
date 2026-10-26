"""The identity gates in front of destructive ops accept a serial in either
spelling. ``metadata.serial_number`` is decimal and ``*IDN?`` reports hex, and
comparing the two as strings refused the correct board.

Every serial here is synthetic. Never put a real board's serial in a test.
"""
from __future__ import annotations

import pytest

from daqifi._internal.serial_identity import (
    describe_serial_number,
    serial_number_matches,
)

SN = 0x0123456789ABCDEF  # synthetic
METADATA = str(SN)  # how metadata.serial_number carries it: decimal


@pytest.mark.parametrize(
    "expected",
    [
        str(SN),  # decimal, as metadata.serial_number has it
        f"{SN:X}",  # hex, as *IDN? reports it
        f"{SN:x}",
        f"0x{SN:X}",
        f"  {SN:X}\n",  # pasted with whitespace
    ],
)
def test_either_spelling_of_the_right_board_matches(expected):
    assert serial_number_matches(expected, METADATA)


@pytest.mark.parametrize(
    "expected",
    [str(SN + 1), f"{SN + 1:X}", f"0x{SN + 1:X}", "", "0x", "not-a-serial"],
)
def test_another_board_or_junk_does_not_match(expected):
    assert not serial_number_matches(expected, METADATA)


@pytest.mark.parametrize("actual", ["", "0", None, "Unknown"])
def test_unknown_metadata_never_matches(actual):
    # "0" is the *IDN? placeholder some firmware reports; an empty or
    # placeholder serial must not satisfy a gate, even for expected "0".
    assert not serial_number_matches("0", actual)
    assert not serial_number_matches(str(SN), actual)


def test_an_all_digit_value_names_one_board_not_two():
    # Audit finding on the first revision: "1234567890123456" read both ways
    # authorized two different boards. All digits now means decimal only.
    digits = "1234567890123456"
    decimal_board = digits
    hex_board = str(int(digits, 16))  # another valid uint64 serial
    assert serial_number_matches(digits, decimal_board)
    assert not serial_number_matches(digits, hex_board)
    # The hex reading has to be asked for explicitly.
    assert serial_number_matches("0x" + digits, hex_board)
    assert not serial_number_matches("0x" + digits, decimal_board)


def test_metadata_is_read_as_decimal_only():
    # The hex reading of the decimal string is some other number; the
    # metadata side must not be reinterpreted, or a hex expected value could
    # match a board whose decimal serial merely shares its digits.
    assert not serial_number_matches(METADATA, f"{SN:X}")


def test_description_shows_both_spellings():
    assert describe_serial_number(METADATA) == f"{SN} (hex {SN:X})"
    assert describe_serial_number("") == "''"
