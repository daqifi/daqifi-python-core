"""Tests for SCPI system-error parsing + the channels.py setter helper.

Closes daqifi/daqifi-python-core#14 — setters were treating SCPI code-0
``"No error"`` as a failure indicator. After the fix, the helper parses
the same SYST:ERR? response captured by ``send_set_with_error`` and
only raises when the device reported a non-zero error code, an empty
reply (timeout), or success=True survived no-op short-circuit.
"""
import pytest

from daqifi._internal import scpi
from daqifi.channels import _check_setter_response
from daqifi.exceptions import CommandError


# ---------------------------------------------------------------------------
# parse_system_error — pure parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("response,expected", [
    ('0,"No error"',      (0, "No error")),
    ('0, "No error"',     (0, "No error")),  # tolerates whitespace
    ('-100,"Bad command"', (-100, "Bad command")),
    ('113,"Undefined header"', (113, "Undefined header")),
    ('0,""',              (0, "")),
])
def test_parse_system_error_round_trips(response, expected):
    assert scpi.parse_system_error(response) == expected


def test_parse_system_error_empty_response_falls_back_to_no_error():
    """Pure parser tolerates empty input — but the setter helper does
    NOT trust this fallback (see test_setter_helper_raises_on_empty
    below). Keeping the parser permissive lets non-setter callers (who
    treat the result advisorially) avoid wrapping every call in a
    try/except for ValueError."""
    assert scpi.parse_system_error("") == (0, "No error")
    assert scpi.parse_system_error("   ") == (0, "No error")


def test_parse_system_error_non_numeric_code_falls_back_to_no_error():
    """Mangled responses shouldn't crash callers; fall back to the
    benign state and let the setter helper detect the empty/malformed
    case separately (it pre-checks the raw response for empty)."""
    assert scpi.parse_system_error('garbage,"???"') == (0, "No error")


# ---------------------------------------------------------------------------
# _check_setter_response — channels.py helper (Qodo PR #53 review #1+2)
# ---------------------------------------------------------------------------


def test_setter_helper_returns_when_send_set_succeeded():
    """success=True short-circuits with no inspection of error_response."""
    _check_setter_response(success=True, error_response="garbage", op_description="set X")


def test_setter_helper_returns_when_device_reports_no_error():
    """Bug-fix path (#14): send_set returned False but the device
    explicitly said 0,"No error" — operation actually succeeded."""
    _check_setter_response(
        success=False, error_response='0,"No error"', op_description="set X",
    )


def test_setter_helper_raises_on_real_error():
    with pytest.raises(CommandError, match="-100"):
        _check_setter_response(
            success=False,
            error_response='-100,"Bad command"',
            op_description="set X",
        )


def test_setter_helper_raises_on_empty_response():
    """Qodo PR #53 review #2: a timeout / missing reply on the error
    query MUST NOT be silently treated as success — the device may be
    in an unknown state."""
    with pytest.raises(CommandError, match="did not respond"):
        _check_setter_response(
            success=False, error_response="", op_description="set X",
        )
    with pytest.raises(CommandError, match="did not respond"):
        _check_setter_response(
            success=False, error_response="   ", op_description="set X",
        )


def test_setter_helper_includes_description_in_error():
    with pytest.raises(CommandError) as exc:
        _check_setter_response(
            success=False,
            error_response='113,"Undefined header"',
            op_description="enable channel",
        )
    msg = str(exc.value)
    assert "enable channel" in msg
    assert "113" in msg
    assert "Undefined header" in msg


def test_setter_helper_does_not_query_error_queue():
    """Qodo PR #53 review #1: the helper must NOT call send_query on
    the error queue — that would advance the queue past the failure
    that produced success=False. The error response is passed in
    pre-captured by ``send_set_with_error``."""
    # Helper signature requires no comm object — proves it can't query.
    import inspect
    sig = inspect.signature(_check_setter_response)
    assert "comm" not in sig.parameters
    assert "error_response" in sig.parameters
