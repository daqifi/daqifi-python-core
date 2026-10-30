"""#203: legacy NyquistDevice.

1. create_stream raised NameError for an invalid channel: device.py
   raises ConfigurationError but never imported it. On an UNKNOWN-variant
   board (no analog inputs) every create_stream hit it.
2. sd_get_file_size raised AttributeError before sd_list_files had run:
   _file_sizes was only created by the listing.

(Item 3, the README SD example, is #197's: its PR points the example at
DaqifiStreamingDevice.)
"""

import pytest

from daqifi.channels import AnalogInputChannel, AnalogInputs
from daqifi.device import NyquistDevice
from daqifi.exceptions import ConfigurationError


def _device_with_inputs(count: int) -> NyquistDevice:
    device = NyquistDevice("COM41")  # constructing doesn't open the port
    cache = [None]
    device._analog_inputs = AnalogInputs(
        [AnalogInputChannel(i, device._comm, 1, range_cache=cache) for i in range(count)]
    )
    return device


@pytest.mark.parametrize("channels", [[99], [-1], [0, 8]])
def test_an_invalid_channel_raises_configuration_error(channels) -> None:
    device = _device_with_inputs(8)
    with pytest.raises(ConfigurationError, match="Valid range: 0-7"):
        device.create_stream(channels=channels)


def test_a_device_with_no_analog_inputs_says_so() -> None:
    device = NyquistDevice("COM41")  # UNKNOWN variant, never connected
    with pytest.raises(ConfigurationError, match="no analog inputs"):
        device.create_stream(channels=[0])


def test_sd_get_file_size_before_any_listing_is_zero() -> None:
    assert NyquistDevice("COM41").sd_get_file_size("log_a.bin") == 0
