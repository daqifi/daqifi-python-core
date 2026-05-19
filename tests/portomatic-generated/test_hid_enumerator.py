"""Tests for HidLibraryDeviceEnumerator + HidPlatform port
(daqifi/_internal/hid_enumerator.py @ upstream e8d84af)."""

import pytest

from daqifi._internal.comm import HidDeviceEnumerator, HidDeviceInfo
from daqifi._internal.hid_enumerator import (
    HidLibraryDeviceEnumerator,
    HidPlatform,
)


pytestmark = pytest.mark.portomatic_generated


class _FakePlatform:
    def __init__(self, devices):
        self._devices = list(devices)

    def enumerate_devices(self):
        return iter(self._devices)


class _BoomPlatform:
    def enumerate_devices(self):
        raise OSError("hid bus error")


_DEVICES = [
    HidDeviceInfo(
        vendor_id=0x04D8,
        product_id=0x000A,
        device_path="/dev/hidraw2",
        serial_number="SN-2",
        product_name="Daqifi NQ2",
    ),
    HidDeviceInfo(
        vendor_id=0x04D8,
        product_id=0x000A,
        device_path="/dev/hidraw0",
        serial_number="SN-0",
        product_name="Daqifi NQ1",
    ),
    HidDeviceInfo(
        vendor_id=0x1234,
        product_id=0x5678,
        device_path="/dev/hidraw9",
        serial_number=None,
        product_name="Other vendor",
    ),
]


class TestHidLibraryDeviceEnumerator:
    def test_satisfies_protocol(self):
        enum = HidLibraryDeviceEnumerator(platform=_FakePlatform([]))
        assert isinstance(enum, HidDeviceEnumerator)

    def test_returns_all_devices_unfiltered(self):
        enum = HidLibraryDeviceEnumerator(platform=_FakePlatform(_DEVICES))
        result = enum.enumerate()
        assert len(result) == 3

    def test_orders_by_device_path(self):
        enum = HidLibraryDeviceEnumerator(platform=_FakePlatform(_DEVICES))
        paths = [d.device_path for d in enum.enumerate()]
        assert paths == ["/dev/hidraw0", "/dev/hidraw2", "/dev/hidraw9"]

    def test_filters_by_vendor_id(self):
        enum = HidLibraryDeviceEnumerator(platform=_FakePlatform(_DEVICES))
        result = enum.enumerate(vendor_id=0x04D8)
        assert {d.vendor_id for d in result} == {0x04D8}
        assert len(result) == 2

    def test_filters_by_product_id(self):
        enum = HidLibraryDeviceEnumerator(platform=_FakePlatform(_DEVICES))
        result = enum.enumerate(product_id=0x5678)
        assert [d.device_path for d in result] == ["/dev/hidraw9"]

    def test_filters_by_both_vid_and_pid(self):
        enum = HidLibraryDeviceEnumerator(platform=_FakePlatform(_DEVICES))
        result = enum.enumerate(vendor_id=0x04D8, product_id=0x000A)
        assert [d.serial_number for d in result] == ["SN-0", "SN-2"]

    def test_empty_platform(self):
        enum = HidLibraryDeviceEnumerator(platform=_FakePlatform([]))
        assert list(enum.enumerate()) == []

    def test_platform_failure_wrapped_with_filter_context(self):
        enum = HidLibraryDeviceEnumerator(platform=_BoomPlatform())
        with pytest.raises(RuntimeError, match="VID=0x04D8, PID=0x000A"):
            enum.enumerate(vendor_id=0x04D8, product_id=0x000A)

    def test_platform_failure_context_no_filter(self):
        enum = HidLibraryDeviceEnumerator(platform=_BoomPlatform())
        with pytest.raises(RuntimeError) as exc:
            enum.enumerate()
        # No "VID="/"PID=" suffix when no filter supplied.
        assert "VID" not in str(exc.value)


class TestHidPlatformProtocol:
    def test_runtime_checkable(self):
        assert isinstance(_FakePlatform([]), HidPlatform)

    def test_rejects_non_implementer(self):
        class _Bad:
            pass

        assert not isinstance(_Bad(), HidPlatform)
