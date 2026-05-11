"""Tests for ticket #99 group C — output-channel state readback.

Adds client-side caching of the last-written output value on
:class:`AnalogOutputChannel` and :class:`DigitalIOChannel` so callers
can ask "what did I last set?" without round-tripping to the device.

Mirrors the C# ``OutputValue`` pattern from
``Daqifi.Core.Channel.DigitalChannel``. C# ``IsHigh`` (live pin
readback) is already covered by the existing ``state`` getter, which
queries the device on every access — confirmed below.
"""
from unittest.mock import MagicMock

import pytest

from daqifi._internal import scpi as _scpi
from daqifi.channels import AnalogOutputChannel, DigitalIOChannel
from daqifi.exceptions import CommandError, ConfigurationError


def _make_analog_output(channel_id: int = 0) -> AnalogOutputChannel:
    comm = MagicMock()
    comm.send_set_with_error = MagicMock(return_value=(True, '0,"No error"'))
    comm.send_query = MagicMock(return_value="0")
    return AnalogOutputChannel(channel_id=channel_id, comm=comm)


def _make_digital_io(channel_id: int = 0) -> DigitalIOChannel:
    comm = MagicMock()
    comm.send_set_with_error = MagicMock(return_value=(True, '0,"No error"'))

    # Route by exact SCPI constant rather than substring — keeps the
    # mock robust against future renames of either query and avoids
    # accidentally matching unrelated commands that happen to share
    # a substring.
    def _reply(q: str) -> str:
        if q == _scpi.DigitalIO.PORT_DIRECTION_QUERY:
            return "1"  # channel 0 is output
        if q == _scpi.DigitalIO.PORT_STATE_QUERY:
            return "0"  # channel 0 low
        return "0"

    comm.send_query = MagicMock(side_effect=_reply)
    return DigitalIOChannel(channel_id=channel_id, comm=comm)


# ---------------------------------------------------------------------------
# AnalogOutputChannel.output_value
# ---------------------------------------------------------------------------


class TestAnalogOutputValueCache:
    def test_initially_none(self):
        ch = _make_analog_output()
        assert ch.output_value is None

    def test_set_voltage_populates_cache(self):
        ch = _make_analog_output()
        ch.voltage = 3.5
        assert ch.output_value == 3.5

    def test_overwrite_replaces_cached_value(self):
        ch = _make_analog_output()
        ch.voltage = 2.0
        ch.voltage = 7.5
        assert ch.output_value == 7.5

    def test_failed_write_does_not_update_cache(self):
        """If the device rejects the write (success=False), the cache
        must not advance — `output_value` would otherwise lie about
        what the device actually has."""
        ch = _make_analog_output()
        ch.voltage = 1.0  # successful write seeds the cache
        ch._comm.send_set_with_error = MagicMock(
            return_value=(False, '-200,"Execution error"')
        )
        with pytest.raises(CommandError):
            ch.voltage = 9.0
        assert ch.output_value == 1.0  # unchanged

    def test_validation_failure_does_not_touch_cache(self):
        """ConfigurationError raised before the SCPI write also leaves
        the cache untouched."""
        ch = _make_analog_output()
        ch.voltage = 4.0
        with pytest.raises(ConfigurationError):
            ch.voltage = 99.0  # out of 0..10 range
        assert ch.output_value == 4.0

    def test_output_value_does_not_query_device(self):
        """`output_value` must be cache-only (no SCPI)."""
        ch = _make_analog_output()
        ch.voltage = 5.5
        ch._comm.send_query.reset_mock()
        _ = ch.output_value
        assert ch._comm.send_query.call_count == 0


# ---------------------------------------------------------------------------
# DigitalIOChannel.output_value
# ---------------------------------------------------------------------------


class TestDigitalOutputValueCache:
    def test_initially_none(self):
        ch = _make_digital_io()
        assert ch.output_value is None

    def test_set_state_populates_cache(self):
        ch = _make_digital_io()
        ch.state = True
        assert ch.output_value is True
        ch.state = False
        assert ch.output_value is False

    def test_failed_write_does_not_update_cache(self):
        ch = _make_digital_io()
        ch.state = True  # successful write seeds the cache
        # Now make the SCPI set fail, but keep the direction query
        # working so `state =` still reaches the SCPI write.
        original_query = ch._comm.send_query
        ch._comm.send_set_with_error = MagicMock(
            return_value=(False, '-200,"Execution error"')
        )
        with pytest.raises(CommandError):
            ch.state = False
        assert ch.output_value is True  # unchanged
        # Sanity: query was reachable, so the failure happened on the
        # SCPI set, not before it.
        assert original_query.call_count >= 1

    def test_input_direction_blocks_state_setter_and_cache(self):
        """If the channel is currently configured as input, the state
        setter raises ConfigurationError before any SCPI write — and
        the cache should not advance."""
        ch = _make_digital_io()
        # Override direction query to report "input"
        ch._comm.send_query = MagicMock(side_effect=lambda q: "0")
        with pytest.raises(ConfigurationError, match="input channel"):
            ch.state = True
        assert ch.output_value is None

    def test_output_value_does_not_query_device(self):
        ch = _make_digital_io()
        ch.state = True
        ch._comm.send_query.reset_mock()
        _ = ch.output_value
        assert ch._comm.send_query.call_count == 0

    def test_switching_to_input_invalidates_cache(self):
        """Switching the channel direction to 'input' clears the
        cached commanded output value — there's nothing meaningful to
        report for an input pin, and a subsequent re-flip back to
        output should start fresh."""
        ch = _make_digital_io()
        ch.state = True
        assert ch.output_value is True

        ch.direction = "input"
        assert ch.output_value is None

    def test_switching_back_to_output_does_not_resurrect_cache(self):
        ch = _make_digital_io()
        ch.state = True
        ch.direction = "input"
        ch.direction = "output"
        # Switching back to output is not itself a write, so the
        # cache stays None until the next state setter call.
        assert ch.output_value is None


# ---------------------------------------------------------------------------
# Confirm `state` getter still queries device — not subsumed by
# output_value cache (C# IsHigh equivalence).
# ---------------------------------------------------------------------------


def test_loose_no_error_substring_no_longer_advances_cache():
    """Regression for the bug Qodo /agentic_review pass 1 caught:
    pre-fix, send_command_with_error returned success=True for any
    response containing the substring "No error" — including
    "120,\"No error in stream\"" which is a real device error.
    The cache update would then advance on a failed write. Post-fix,
    success is computed from the parsed SCPI code so this case
    correctly returns success=False, the setter raises CommandError,
    and the cache stays put."""
    ch = _make_analog_output()
    ch.voltage = 3.0  # seed cache with a real successful write
    # Now simulate the loose-substring pathological case: success=False
    # because the new send_command_with_error parses code 120 != 0.
    # _check_setter_response then sees success=False AND a non-zero
    # code in the response → raises.
    ch._comm.send_set_with_error = MagicMock(
        return_value=(False, '120,"No error in stream"')
    )
    with pytest.raises(CommandError, match="120"):
        ch.voltage = 7.5
    assert ch.output_value == 3.0  # cache untouched


def test_state_getter_still_queries_device_after_output_value_cache_added():
    """C# ``IsHigh`` reflects live pin level; the Python equivalent is
    the existing `state` getter (queries the device every time). Adding
    `output_value` must not change that — the two answer different
    questions."""
    ch = _make_digital_io()
    ch.state = True  # output_value cache now True
    # Make the device report low — i.e., pin readback differs from
    # commanded state (e.g. an output's drive lost).
    ch._comm.send_query = MagicMock(side_effect=lambda q: "0")
    assert ch.state is False, "state getter must reflect device, not cache"
    assert ch.output_value is True, "output_value cache must reflect commanded value"
