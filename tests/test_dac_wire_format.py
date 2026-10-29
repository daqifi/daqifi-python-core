"""#189: the NQ3 DAC commands the firmware accepts.

Firmware main registers only ``SOURce:VOLTage:LEVel`` (``SCPI_DACVoltageSet``,
documented as ``SOURce:VOLTage:LEVel ${CH},${VOLTAGE}`` in SCPIDAC.h) and its
query for the DAC. Its libscpi answers ``SOUR:VOLT:LEV 0 5.0`` with -101
Invalid character, while ``SOUR:VOLT:LEV 0,5.0`` parses. ``ENAble:SOURce:DC``
is no longer registered. The line is checked as the real
``SerialInterface.send_set_with_error`` formats it, not as the test would.
"""

import functools
from unittest.mock import MagicMock

import pytest

from daqifi._internal.comm import SerialInterface
from daqifi.channels import AnalogOutputChannel, AnalogOutputs
from daqifi.exceptions import ConfigurationError, FeatureNotSupportedError


def _channel(channel_id=0):
    comm = MagicMock()
    comm.send_command_with_error = MagicMock(return_value=(True, '0,"No error"'))
    comm.send_set_with_error = functools.partial(SerialInterface.send_set_with_error, comm)
    return AnalogOutputChannel(channel_id=channel_id, comm=comm), comm


def _lines(comm):
    return [c.args[0] for c in comm.send_command_with_error.call_args_list]


@pytest.mark.parametrize(
    "channel_id, volts, line",
    [(0, 5.0, "SOUR:VOLT:LEV 0,5.0"), (7, 3.3, "SOUR:VOLT:LEV 7,3.3"), (2, 10, "SOUR:VOLT:LEV 2,10")],
)
def test_the_voltage_line_separates_channel_and_value_with_a_comma(channel_id, volts, line) -> None:
    channel, comm = _channel(channel_id)
    channel.voltage = volts
    assert _lines(comm) == [line]


def test_the_collection_writes_each_channel_in_the_same_form() -> None:
    channels = [_channel(i) for i in range(3)]
    AnalogOutputs([c for c, _ in channels]).set_voltages({0: 1.5, 2: 0.0})
    assert [_lines(comm) for _, comm in channels] == [["SOUR:VOLT:LEV 0,1.5"], [], ["SOUR:VOLT:LEV 2,0.0"]]


def test_a_bool_is_not_a_voltage() -> None:
    channel, comm = _channel()
    with pytest.raises(ConfigurationError):
        channel.voltage = True
    assert _lines(comm) == []


@pytest.mark.parametrize("action", ["get", "set_true", "set_false"])
def test_the_removed_dac_enable_raises_and_sends_nothing(action) -> None:
    channel, comm = _channel()
    with pytest.raises(FeatureNotSupportedError, match="voltage to 0.0"):
        if action == "get":
            channel.enabled
        else:
            channel.enabled = action == "set_true"
    assert _lines(comm) == [] and comm.send_query.call_count == 0


@pytest.mark.parametrize("method", ["enable_all", "disable_all"])
def test_the_collection_enable_helpers_raise_and_send_nothing(method) -> None:
    channel, comm = _channel()
    with pytest.raises(FeatureNotSupportedError):
        getattr(AnalogOutputs([channel]), method)()
    assert _lines(comm) == []
