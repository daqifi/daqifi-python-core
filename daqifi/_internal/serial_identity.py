"""Serial-number comparison for the identity gates in front of destructive ops.

The same 64-bit device serial reaches a developer in two spellings:

* ``metadata.serial_number`` (from the protobuf ``SYSInfoPB?`` reply) is
  **decimal**.
* ``*IDN?`` and ``SYST:SERialNUMber?`` report it in **hex**.

The gates used to compare ``DAQIFI_EXPECTED_SERIAL_NUMBER`` to the metadata
value as strings, so setting the variable to the serial ``*IDN?`` printed
refused the *correct* board and named its decimal form as if it were another
unit. A safety check that rejects the right board invites someone to disable
it, so the gates accept either spelling and their messages show both.
"""

from __future__ import annotations

import string
from typing import Any, Optional, Set

_HEX_DIGITS = frozenset(string.hexdigits)


def _readings(text: str) -> Set[int]:
    """Every integer *text* can denote as a serial: decimal and/or hex."""
    s = text.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
        return {int(s, 16)} if s and set(s) <= _HEX_DIGITS else set()
    readings = set()
    if s.isdigit():
        readings.add(int(s, 10))
    if s and set(s) <= _HEX_DIGITS:
        readings.add(int(s, 16))
    return readings


def _metadata_value(actual: Any) -> Optional[int]:
    """The metadata serial is always decimal; ``0`` / empty mean unknown."""
    try:
        value = int(str(actual).strip(), 10)
    except (TypeError, ValueError):
        return None
    return value or None


def serial_number_matches(expected: str, actual: Any) -> bool:
    """True iff *expected* (decimal or hex) names the board whose
    ``metadata.serial_number`` is *actual*.

    An all-digit *expected* is read both ways. A false accept would need
    its other reading to equal this exact board's serial, which leaves the
    gate's job intact: telling apart the handful of boards on one bench.
    """
    value = _metadata_value(actual)
    return value is not None and value in _readings(expected)


def describe_serial_number(actual: Any) -> str:
    """``metadata.serial_number`` in both spellings, for gate messages."""
    value = _metadata_value(actual)
    if value is None:
        return repr(actual)
    return f"{value} (hex {value:X})"
