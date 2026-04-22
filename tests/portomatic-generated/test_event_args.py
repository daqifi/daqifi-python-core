"""Tests for ported event-args dataclasses.

Sources at upstream e8d84af:
- Daqifi.Core/Device/ChannelsPopulatedEventArgs.cs
- Daqifi.Core/Device/Discovery/DeviceDiscoveredEventArgs.cs
"""

import pytest

from daqifi.channels import ChannelsPopulatedEventArgs
from daqifi.discovery import BoardVariant, DeviceDiscoveredEventArgs, DeviceInfo


@pytest.mark.portomatic_generated
class TestChannelsPopulatedEventArgs:
    def test_construction(self):
        ch_a, ch_b = object(), object()
        args = ChannelsPopulatedEventArgs(
            channels=(ch_a, ch_b),
            analog_channel_count=1,
            digital_channel_count=1,
        )
        assert args.channels == (ch_a, ch_b)
        assert args.analog_channel_count == 1
        assert args.digital_channel_count == 1

    def test_channels_tuple_is_immutable(self):
        args = ChannelsPopulatedEventArgs(
            channels=(),
            analog_channel_count=0,
            digital_channel_count=0,
        )
        with pytest.raises(TypeError):
            args.channels[0] = "x"  # type: ignore[index]

    def test_frozen(self):
        args = ChannelsPopulatedEventArgs(
            channels=(),
            analog_channel_count=0,
            digital_channel_count=0,
        )
        with pytest.raises(Exception):
            args.analog_channel_count = 99  # type: ignore[misc]


@pytest.mark.portomatic_generated
class TestDeviceDiscoveredEventArgs:
    def test_carries_device_info(self):
        info = DeviceInfo(port="/dev/ttyACM0", variant=BoardVariant.NQ3)
        args = DeviceDiscoveredEventArgs(device_info=info)
        assert args.device_info is info
        assert args.device_info.port == "/dev/ttyACM0"

    def test_frozen(self):
        info = DeviceInfo(port="/dev/ttyACM0")
        args = DeviceDiscoveredEventArgs(device_info=info)
        with pytest.raises(Exception):
            args.device_info = DeviceInfo(port="/dev/ttyACM1")  # type: ignore[misc]
