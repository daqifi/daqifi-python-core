"""Firmware download service contract.

Port of `Daqifi.Core.Firmware.IFirmwareDownloadService` and
`FirmwareUpdateCheckResult`. The Protocol describes a service that
talks to a release repository (typically GitHub releases) to check
for and download firmware artifacts.

This module ships the abstract surface only. A concrete
`GitHubFirmwareDownloadService` implementation is tracked separately
since it depends on an HTTP client + zip handling.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, Tuple, runtime_checkable

from daqifi.firmware.release_info import FirmwareReleaseInfo
from daqifi.firmware.version import FirmwareVersion


__all__ = [
    "FirmwareUpdateCheckResult",
    "FirmwareDownloadService",
    "ProgressReporter",
]


# Pythonic name for IProgress<int> — a callable that receives integer
# percent values 0..100. Optional[ProgressReporter] models the C#
# `IProgress<int>?` parameter.
ProgressReporter = Callable[[int], None]


@dataclass(frozen=True)
class FirmwareUpdateCheckResult:
    """Result of comparing a device's firmware version against the latest release."""

    update_available: bool
    device_version: Optional[FirmwareVersion] = None
    latest_release: Optional[FirmwareReleaseInfo] = None


@runtime_checkable
class FirmwareDownloadService(Protocol):
    """Protocol for fetching firmware release info and binaries.

    Pythonic name for `IFirmwareDownloadService`. Concrete
    implementations (e.g. a GitHub-backed one) live in separate modules.
    All operations are async to mirror the C# `Task<T>` surface — Python
    callers can await them just as the C# callers `await` the Task.
    """

    async def get_latest_release(
        self,
        include_pre_release: bool = False,
    ) -> Optional[FirmwareReleaseInfo]:
        """Return the latest release info, or None if none found."""
        ...

    async def check_for_update(
        self,
        device_version_string: str,
        include_pre_release: bool = False,
    ) -> FirmwareUpdateCheckResult:
        """Compare the device's firmware version against the latest release."""
        ...

    async def download_latest_firmware(
        self,
        destination_directory: Path,
        include_pre_release: bool = False,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Path]:
        """Download the latest firmware .hex file. Returns the saved path or None."""
        ...

    async def download_firmware_by_tag(
        self,
        tag_name: str,
        destination_directory: Path,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Path]:
        """Download firmware for a specific release tag. Returns the saved path or None."""
        ...

    async def download_wifi_firmware(
        self,
        destination_directory: Path,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Tuple[Path, str]]:
        """Download + extract the WiFi firmware bundle.

        Returns (extracted_path, version) or None. ``version`` is the
        bundle's version folder (``files/winc*/<version>/``), the value
        ``update_wifi_module(firmware_version=...)`` takes, not the release
        tag; a bundle without exactly one raises RuntimeError.
        """
        ...

    async def get_latest_wifi_release(self) -> Optional[FirmwareReleaseInfo]:
        """Return the latest WiFi firmware release info, or None if none found."""
        ...

    def invalidate_cache(self) -> None:
        """Drop any cached release data so the next call re-queries the API."""
        ...
