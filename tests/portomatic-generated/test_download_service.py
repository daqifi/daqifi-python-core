"""Tests for `daqifi.firmware.download_service`.

The Protocol is abstract — these tests cover the value type
(`FirmwareUpdateCheckResult`) and verify a minimal Fake implementation
satisfies the Protocol contract.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, Tuple

import pytest

from daqifi.firmware import (
    FirmwareDownloadService,
    FirmwareReleaseInfo,
    FirmwareUpdateCheckResult,
    FirmwareVersion,
    ProgressReporter,
)


class TestFirmwareUpdateCheckResult:
    def test_minimum_required_field(self):
        r = FirmwareUpdateCheckResult(update_available=False)
        assert r.update_available is False
        assert r.device_version is None
        assert r.latest_release is None

    def test_full_fields(self):
        v = FirmwareVersion.parse("1.2.3")
        release = FirmwareReleaseInfo(version=v, tag_name="v1.2.3", is_pre_release=False)
        r = FirmwareUpdateCheckResult(
            update_available=True,
            device_version=v,
            latest_release=release,
        )
        assert r.update_available is True
        assert r.device_version == v
        assert r.latest_release == release

    def test_is_frozen(self):
        r = FirmwareUpdateCheckResult(update_available=False)
        with pytest.raises(Exception):
            r.update_available = True  # type: ignore[misc]


class _FakeDownloadService:
    """Minimal in-memory FirmwareDownloadService for protocol-conformance testing."""

    def __init__(self) -> None:
        self.cache_invalidations = 0
        v = FirmwareVersion.parse("1.0.0")
        self._release = FirmwareReleaseInfo(version=v, tag_name="v1.0.0", is_pre_release=False)
        self._wifi_release = FirmwareReleaseInfo(
            version=FirmwareVersion.parse("19.5.4"),
            tag_name="wifi-v19.5.4",
            is_pre_release=False,
        )

    async def get_latest_release(
        self, include_pre_release: bool = False,
    ) -> Optional[FirmwareReleaseInfo]:
        return self._release

    async def check_for_update(
        self,
        device_version_string: str,
        include_pre_release: bool = False,
    ) -> FirmwareUpdateCheckResult:
        device = FirmwareVersion.parse(device_version_string)
        return FirmwareUpdateCheckResult(
            update_available=device != self._release.version,
            device_version=device,
            latest_release=self._release,
        )

    async def download_latest_firmware(
        self,
        destination_directory: Path,
        include_pre_release: bool = False,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Path]:
        if progress is not None:
            progress(50)
            progress(100)
        return destination_directory / "firmware.hex"

    async def download_firmware_by_tag(
        self,
        tag_name: str,
        destination_directory: Path,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Path]:
        return destination_directory / f"{tag_name}.hex"

    async def download_wifi_firmware(
        self,
        destination_directory: Path,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Tuple[Path, str]]:
        return (destination_directory / "wifi", "19.5.4")

    async def get_latest_wifi_release(self) -> Optional[FirmwareReleaseInfo]:
        return self._wifi_release

    def invalidate_cache(self) -> None:
        self.cache_invalidations += 1


class TestFirmwareDownloadServiceProtocol:
    def test_fake_satisfies_protocol(self):
        service: FirmwareDownloadService = _FakeDownloadService()
        assert isinstance(service, FirmwareDownloadService)

    def test_get_latest_release(self):
        service = _FakeDownloadService()
        result = asyncio.run(service.get_latest_release())
        assert result is not None
        assert result.tag_name == "v1.0.0"

    def test_check_for_update_when_device_outdated(self):
        service = _FakeDownloadService()
        result = asyncio.run(service.check_for_update("0.9.0"))
        assert result.update_available is True
        assert result.device_version is not None
        assert result.latest_release is not None

    def test_check_for_update_when_device_current(self):
        service = _FakeDownloadService()
        result = asyncio.run(service.check_for_update("1.0.0"))
        assert result.update_available is False

    def test_download_latest_firmware_emits_progress(self):
        service = _FakeDownloadService()
        captured: list[int] = []
        result = asyncio.run(service.download_latest_firmware(
            Path("/tmp/x"),
            progress=captured.append,
        ))
        assert result == Path("/tmp/x/firmware.hex")
        assert captured == [50, 100]

    def test_download_by_tag(self):
        service = _FakeDownloadService()
        result = asyncio.run(service.download_firmware_by_tag("v2.0", Path("/tmp")))
        assert result == Path("/tmp/v2.0.hex")

    def test_download_wifi_firmware(self):
        service = _FakeDownloadService()
        result = asyncio.run(service.download_wifi_firmware(Path("/tmp")))
        assert result is not None
        path, version = result
        assert path == Path("/tmp/wifi")
        assert version == "19.5.4"

    def test_get_latest_wifi_release(self):
        service = _FakeDownloadService()
        result = asyncio.run(service.get_latest_wifi_release())
        assert result is not None
        assert result.tag_name == "wifi-v19.5.4"

    def test_invalidate_cache(self):
        service = _FakeDownloadService()
        assert service.cache_invalidations == 0
        service.invalidate_cache()
        service.invalidate_cache()
        assert service.cache_invalidations == 2

    def test_progress_optional(self):
        service = _FakeDownloadService()
        # Should not raise when progress is None.
        result = asyncio.run(service.download_latest_firmware(Path("/tmp")))
        assert result == Path("/tmp/firmware.hex")
