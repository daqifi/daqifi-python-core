"""Tests for SCPI system-error parsing + the channels.py setter helper.

Closes daqifi/daqifi-python-core#14 — setters were treating SCPI code-0
``"No error"`` as a failure indicator. After the fix, the setter helper
parses the response and only raises when the code is non-zero.
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


def test_parse_system_error_empty_response_treated_as_no_error():
    """Empty / whitespace responses can occur when the device echoes
    nothing back — treat as the benign no-error state rather than
    raising spuriously."""
    assert scpi.parse_system_error("") == (0, "No error")
    assert scpi.parse_system_error("   ") == (0, "No error")


def test_parse_system_error_non_numeric_code_falls_back_to_no_error():
    """Mangled responses shouldn't crash callers; fall back to the
    benign state and let the operator see the original buffer if they
    want to dig deeper."""
    assert scpi.parse_system_error('garbage,"???"') == (0, "No error")


# ---------------------------------------------------------------------------
# _check_setter_response — the channels.py helper
# ---------------------------------------------------------------------------


class _FakeComm:
    """Records send_query calls + returns canned responses."""

    def __init__(self, error_response: str = '0,"No error"'):
        self._error_response = error_response
        self.queries: list[str] = []

    def send_query(self, cmd: str) -> str:
        self.queries.append(cmd)
        return self._error_response

    def send_set(self, *args, **kwargs):
        raise NotImplementedError("test should not call send_set")


def test_setter_helper_returns_when_send_set_succeeded():
    """When success=True the helper must short-circuit without
    consulting the error queue (one fewer round-trip on the wire)."""
    comm = _FakeComm()
    _check_setter_response(comm, success=True, op_description="set X")
    assert comm.queries == []


def test_setter_helper_returns_when_error_code_is_zero():
    """The bug fix path: send_set returned False, but the device
    reports code 0 ('No error') so the operation actually succeeded.
    Must NOT raise."""
    comm = _FakeComm(error_response='0,"No error"')
    _check_setter_response(comm, success=False, op_description="set X")
    assert comm.queries == [scpi.System.ERR]


def test_setter_helper_raises_on_real_error():
    comm = _FakeComm(error_response='-100,"Bad command"')
    with pytest.raises(CommandError, match="-100"):
        _check_setter_response(comm, success=False, op_description="set X")
    assert comm.queries == [scpi.System.ERR]


def test_setter_helper_includes_description_in_error():
    comm = _FakeComm(error_response='113,"Undefined header"')
    with pytest.raises(CommandError) as exc:
        _check_setter_response(comm, success=False, op_description="enable channel")
    msg = str(exc.value)
    assert "enable channel" in msg
    assert "113" in msg
    assert "Undefined header" in msg
