"""Tests for the ConnectionType IntEnum (ported from
Daqifi.Core/Device/Discovery/IDeviceInfo.cs at upstream e8d84af)."""

import pytest

from daqifi._internal.comm import ConnectionType


@pytest.mark.portomatic_generated
class TestConnectionType:
    def test_values_match_upstream_ordering(self):
        assert ConnectionType.UNKNOWN.value == 0
        assert ConnectionType.WIFI.value == 1
        assert ConnectionType.SERIAL.value == 2
        assert ConnectionType.HID.value == 3

    def test_is_int_enum(self):
        assert int(ConnectionType.HID) == 3
        assert ConnectionType.WIFI == 1

    def test_lookup_by_value(self):
        assert ConnectionType(2) is ConnectionType.SERIAL

    def test_lookup_by_name(self):
        assert ConnectionType["HID"] is ConnectionType.HID

    def test_distinct_members(self):
        names = {m.name for m in ConnectionType}
        assert names == {"UNKNOWN", "WIFI", "SERIAL", "HID"}

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            ConnectionType(99)
