"""FirmwareRevision + parse_firmware_string coverage (closes #9).

The Nyquist firmware (3.4.6b1) reports its version as the hyphenated
``<group>-<id>`` shape ``01-02`` rather than SemVer. FirmwareVersion
correctly rejects this — daqifi-core's C# parser does too — so we
introduced FirmwareRevision for the hyphenated path and a top-level
``parse_firmware_string`` selector that picks the right type.
"""
from __future__ import annotations

import pytest

from daqifi.firmware import (
    FirmwareRevision,
    FirmwareVersion,
    parse_firmware_string,
)


# ---------------------------------------------------------------------------
# FirmwareRevision.parse — happy path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inp,expected", [
    ("01-02", (1, 2)),
    ("00-00", (0, 0)),
    ("10-99", (10, 99)),
    ("1-2", (1, 2)),
    ("  01-02  ", (1, 2)),
])
def test_revision_parse_happy(inp, expected):
    rev = FirmwareRevision.parse(inp)
    assert rev is not None, f"failed to parse {inp!r}"
    assert (rev.group, rev.revision) == expected


@pytest.mark.parametrize("inp", [
    None,
    "",
    "   ",
    "1.2.3",
    "v1.2.3",
    "01_02",
    "01-",
    "-02",
    "01-02-03",
    "abc-def",
    "01-02b",
])
def test_revision_parse_rejects_non_hyphenated(inp):
    assert FirmwareRevision.parse(inp) is None


def test_revision_str_pads_to_two_digits():
    assert str(FirmwareRevision(1, 2)) == "01-02"
    assert str(FirmwareRevision(10, 99)) == "10-99"
    assert str(FirmwareRevision(0, 0)) == "00-00"


def test_revision_ordering():
    assert FirmwareRevision(1, 1) < FirmwareRevision(1, 2)
    assert FirmwareRevision(1, 9) < FirmwareRevision(2, 0)
    assert FirmwareRevision(2, 0) > FirmwareRevision(1, 9)
    assert FirmwareRevision(1, 2) == FirmwareRevision(1, 2)


def test_revision_not_comparable_to_firmware_version():
    """Cross-type comparison returns NotImplemented and Python raises TypeError."""
    rev = FirmwareRevision(1, 2)
    ver = FirmwareVersion(1, 0, 0)
    with pytest.raises(TypeError):
        _ = rev < ver
    with pytest.raises(TypeError):
        _ = ver < rev


# ---------------------------------------------------------------------------
# parse_firmware_string — picks the right type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inp,expected_type,expected_fields", [
    # Hyphenated revision
    ("01-02", FirmwareRevision, {"group": 1, "revision": 2}),
    ("00-01", FirmwareRevision, {"group": 0, "revision": 1}),
    # SemVer with leading v
    ("v3.2.0", FirmwareVersion, {"major": 3, "minor": 2, "patch": 0}),
    ("3.4.6b1", FirmwareVersion, {"major": 3, "minor": 4, "patch": 6, "pre_label": "b", "pre_number": 1}),
    # Bare ints / dotted pieces
    ("3", FirmwareVersion, {"major": 3, "minor": 0, "patch": 0}),
    ("3.2", FirmwareVersion, {"major": 3, "minor": 2, "patch": 0}),
])
def test_parse_firmware_string_picks_correct_type(inp, expected_type, expected_fields):
    parsed = parse_firmware_string(inp)
    assert parsed is not None, f"failed to parse {inp!r}"
    assert isinstance(parsed, expected_type)
    for field, value in expected_fields.items():
        assert getattr(parsed, field) == value


@pytest.mark.parametrize("inp", [None, "", "   ", "garbage", "1.2.3.4-extra-junk"])
def test_parse_firmware_string_returns_none_on_unparseable(inp):
    assert parse_firmware_string(inp) is None


def test_revision_form_takes_precedence_over_semver():
    """``01-02`` is hyphenated — must NOT fall through to FirmwareVersion."""
    parsed = parse_firmware_string("01-02")
    assert isinstance(parsed, FirmwareRevision)
