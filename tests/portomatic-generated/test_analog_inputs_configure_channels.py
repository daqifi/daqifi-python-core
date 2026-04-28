"""Regression for daqifi/daqifi-python-core#32: configure_channels was
fully unusable on NQ1/NQ2 because the range setter raises
FeatureNotSupportedError on those variants and aborted the loop before
enabled was set."""
from unittest.mock import MagicMock

import pytest

from daqifi.channels import AnalogInputChannel, AnalogInputs
from daqifi.exceptions import FeatureNotSupportedError


def _make_channel(variant: str | None) -> AnalogInputChannel:
    """Build an AnalogInputChannel wired to a MagicMock comm so we can
    introspect the .enabled writes."""
    comm = MagicMock()
    # send_set_with_error returns (success, error_response). The
    # default success path is True, error_response='0,"No error"'.
    comm.send_set_with_error = MagicMock(return_value=(True, '0,"No error"'))
    return AnalogInputChannel(channel_id=0, comm=comm, variant=variant)


def test_configure_channels_writes_enabled_when_range_not_supported():
    """Reproducer from #32: on NQ1, range raises FeatureNotSupportedError.
    Before the fix, that aborted the loop before enabled was written."""
    channels = [_make_channel("NQ1"), _make_channel("NQ1"), _make_channel("NQ1")]
    inputs = AnalogInputs(channels)

    inputs.configure_channels([0, 1, 2], range="10V", enabled=True)

    # Each channel must have received the ENABLE_CHANNEL_SET write
    # despite the range setter raising on NQ1.
    for ch in channels:
        ch._comm.send_set_with_error.assert_called()
        # Find the call that targeted ENABLE_CHANNEL_SET (not RANGE_SET).
        from daqifi._internal import scpi as scpi_mod
        enable_calls = [
            call for call in ch._comm.send_set_with_error.call_args_list
            if scpi_mod.AnalogInput.ENABLE_CHANNEL_SET in call.args[0]
        ]
        assert enable_calls, (
            f"channel {ch._channel} never received ENABLE_CHANNEL_SET — "
            f"the range FeatureNotSupportedError aborted the loop"
        )


def test_configure_channels_writes_range_on_nq3():
    """On NQ3 (configurable range), range write should still happen."""
    channels = [_make_channel("NQ3")]
    inputs = AnalogInputs(channels)

    inputs.configure_channels([0], range="10V", enabled=True)

    # Two writes — RANGE_SET + ENABLE_CHANNEL_SET.
    from daqifi._internal import scpi as scpi_mod
    cmds = [call.args[0] for call in channels[0]._comm.send_set_with_error.call_args_list]
    assert any(scpi_mod.AnalogInput.RANGE_SET in c for c in cmds), (
        f"NQ3 should still get RANGE_SET; cmds: {cmds!r}"
    )
    assert any(scpi_mod.AnalogInput.ENABLE_CHANNEL_SET in c for c in cmds), (
        f"NQ3 should still get ENABLE_CHANNEL_SET; cmds: {cmds!r}"
    )


def test_configure_channels_skips_out_of_range_indices():
    """Defensive: indices outside the channel collection are silently
    skipped (existing behavior, regression-protected)."""
    channels = [_make_channel("NQ1")]  # only 1 channel
    inputs = AnalogInputs(channels)

    # No raise — just skip the out-of-range index 99
    inputs.configure_channels([0, 99], range="10V", enabled=True)

    # Channel 0 still got configured.
    channels[0]._comm.send_set_with_error.assert_called()


def test_enable_setter_uses_comma_separator():
    """Closes the hardware-side gap discovered while verifying #32:
    firmware syntax for per-channel enable is
    ``CONFigure:ADC:CHANnel <ch>,<state>`` — comma-separated. The
    previous space form produced ``-101,"Invalid character"`` on real
    NQ1 firmware 3.4.6b1."""
    ch = _make_channel("NQ1")
    ch.enabled = True

    # Find the call that invoked send_set_with_error for ENABLE_CHANNEL_SET.
    from daqifi._internal import scpi as scpi_mod
    calls = ch._comm.send_set_with_error.call_args_list
    matching = [
        call for call in calls
        if call.args[0] == scpi_mod.AnalogInput.ENABLE_CHANNEL_SET
    ]
    assert matching, f"no ENABLE_CHANNEL_SET write — calls: {calls!r}"
    # The pre-formatted value contains comma between channel + state.
    sent_value = matching[0].args[1]
    assert sent_value == "0,1", (
        f"expected '0,1' (comma-separated channel+state), got: {sent_value!r}"
    )


def test_configure_channels_propagates_non_range_errors():
    """If the enabled setter itself fails, that error must propagate
    (the FeatureNotSupportedError swallow is range-specific only)."""
    channels = [_make_channel("NQ1")]
    inputs = AnalogInputs(channels)
    # Make the enabled setter fail with a real CommandError.
    from daqifi.exceptions import CommandError
    channels[0]._comm.send_set_with_error.return_value = (False, '-200,"Execution error"')

    with pytest.raises(CommandError):
        inputs.configure_channels([0], range="10V", enabled=True)
