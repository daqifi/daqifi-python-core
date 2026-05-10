"""Tests for the LanChipInfoProvider Protocol port (closes #7)."""
from __future__ import annotations

from typing import Optional

import pytest

from daqifi.firmware import LanChipInfoProvider
from daqifi.firmware.lan_chip_info import LanChipInfo


pytestmark = pytest.mark.portomatic_generated


class _CompleteStub:
    def __init__(self, info: Optional[LanChipInfo] = None) -> None:
        self._info = info

    def get_lan_chip_info(self) -> Optional[LanChipInfo]:
        return self._info


class TestLanChipInfoProviderProtocol:
    def test_runtime_checkable_complete_stub_passes(self):
        assert isinstance(_CompleteStub(), LanChipInfoProvider)

    def test_incomplete_stub_fails_isinstance(self):
        class _Incomplete:
            pass

        assert not isinstance(_Incomplete(), LanChipInfoProvider)

    def test_returns_none_when_device_silent(self):
        stub = _CompleteStub()
        assert stub.get_lan_chip_info() is None

    def test_returns_lan_chip_info_when_known(self):
        info = LanChipInfo(
            chip_id="ATWINC1500",
            fw_version="19.6.5",
            build_date="2026-01-01",
        )
        stub = _CompleteStub(info=info)
        result = stub.get_lan_chip_info()
        assert result is info
        assert result.chip_id == "ATWINC1500"

    def test_module_export(self):
        from daqifi import firmware as fw_module

        assert hasattr(fw_module, "LanChipInfoProvider")
        assert fw_module.LanChipInfoProvider is LanChipInfoProvider
