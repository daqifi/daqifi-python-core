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

⛔ Each expected value has exactly ONE reading, never two. ``0x`` or any of
a-f means hex; all digits means decimal. An earlier revision read an
all-digit value both ways, which let one setting authorize two different
boards (``1234567890123456`` is also the hex spelling of another valid
serial). A hex serial that happens to be all digits must carry ``0x``;
without it the gate refuses, and its message shows both spellings so the fix
is obvious. Refusing the right board is recoverable; resetting the wrong one
is not.
"""

from __future__ import annotations

import string
from typing import Any, Optional

_HEX_DIGITS = frozenset(string.hexdigits)


def _expected_value(text: str) -> Optional[int]:
    """The one serial *text* names: hex if ``0x``-prefixed or it contains
    a-f, decimal if all digits, otherwise none."""
    s = text.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
        return int(s, 16) if s and set(s) <= _HEX_DIGITS else None
    if s.isdigit():
        return int(s, 10)
    if s and set(s) <= _HEX_DIGITS:
        return int(s, 16)
    return None


def _metadata_value(actual: Any) -> Optional[int]:
    """The metadata serial is always decimal; ``0`` / empty mean unknown."""
    try:
        value = int(str(actual).strip(), 10)
    except (TypeError, ValueError):
        return None
    return value or None


def serial_number_matches(expected: str, actual: Any) -> bool:
    """True iff *expected* (decimal or hex, see the module docstring for
    which) names the board whose ``metadata.serial_number`` is *actual*."""
    value = _metadata_value(actual)
    return value is not None and value == _expected_value(expected)


def describe_serial_number(actual: Any) -> str:
    """``metadata.serial_number`` in both spellings, for gate messages."""
    value = _metadata_value(actual)
    if value is None:
        return repr(actual)
    return f"{value} (hex {value:X})"
