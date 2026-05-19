"""Tests for the StreamInterface IntEnum (ported from
Daqifi.Core/Communication/StreamInterface.cs at upstream e8d84af)."""

import pytest

from daqifi._internal.scpi import StreamInterface


@pytest.mark.portomatic_generated
class TestStreamInterface:
    def test_values_match_upstream(self):
        assert StreamInterface.Usb.value == 0
        assert StreamInterface.WiFi.value == 1
        assert StreamInterface.SdCard.value == 2
        assert StreamInterface.All.value == 3

    def test_is_int_enum(self):
        assert int(StreamInterface.Usb) == 0
        assert StreamInterface.WiFi == 1

    def test_lookup_by_value(self):
        assert StreamInterface(2) is StreamInterface.SdCard

    def test_lookup_by_name(self):
        assert StreamInterface["All"] is StreamInterface.All

    def test_distinct_members(self):
        values = {m.value for m in StreamInterface}
        assert values == {0, 1, 2, 3}

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            StreamInterface(99)
