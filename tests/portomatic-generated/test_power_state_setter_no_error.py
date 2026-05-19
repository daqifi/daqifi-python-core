"""Regression for the latent NyquistDevice.power_state setter bug
surfaced by the post-merge hardware suite (2026-04-28).

Same shape as #14 (channels.py setters) but in the higher-level
NyquistDevice — the setter relied on `send_set` returning True, and
when the device replied with `0,"No error"` the setter would
double-query SYST:ERR? (advancing the queue) and raise CommandError.
The fix routes through send_command_with_error + _check_setter_response
so the captured response is the authoritative success signal."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from daqifi.device import NyquistDevice
from daqifi._internal.comm import PowerState
from daqifi.exceptions import CommandError


def _make_device_with_mock_comm(
    success_value: bool, error_response: str
) -> NyquistDevice:
    """Build a NyquistDevice whose SerialInterface is a MagicMock that
    returns ``(success_value, error_response)`` from every
    send_command_with_error call. Bypasses the connect() path so we can
    drive the setter against a controlled response."""
    dev = NyquistDevice.__new__(NyquistDevice)
    dev._comm = MagicMock()
    dev._comm.send_command_with_error = MagicMock(
        return_value=(success_value, error_response)
    )
    return dev


def test_power_state_setter_treats_no_error_as_success() -> None:
    """Even if send_command_with_error returns success=False, a
    `0,"No error"` response means the device accepted the command."""
    dev = _make_device_with_mock_comm(False, '0,"No error"')
    # Should NOT raise — the setter must treat the explicit no-error
    # response as success (closes the bug surfaced post-#56 merge).
    dev.power_state = PowerState.POWERED_UP


def test_power_state_setter_raises_on_real_error() -> None:
    """A genuine non-zero SCPI error code must still propagate."""
    dev = _make_device_with_mock_comm(
        False, '-200,"Execution error"'
    )
    with pytest.raises(CommandError, match="-200"):
        dev.power_state = PowerState.POWERED_UP


def test_power_state_setter_raises_on_empty_response() -> None:
    """An empty SYST:ERR? response (transport timeout / disconnect)
    must NOT be treated as success — the device state is unknown."""
    dev = _make_device_with_mock_comm(False, "")
    with pytest.raises(CommandError, match="did not respond"):
        dev.power_state = PowerState.POWERED_UP


def test_power_state_setter_does_not_double_query_err() -> None:
    """The setter must rely on the response captured by
    send_command_with_error and NOT issue a second SYST:ERR? query — the
    second query would advance the queue past the actual entry."""
    dev = _make_device_with_mock_comm(False, '0,"No error"')
    dev._comm.send_query = MagicMock(
        side_effect=AssertionError("setter must not call send_query")
    )
    dev.power_state = PowerState.POWERED_UP
    dev._comm.send_query.assert_not_called()
